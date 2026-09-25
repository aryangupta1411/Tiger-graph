"""Load vector files into TigerGraph and wait for the HNSW index.

    uv run python -m rag.load_vectors --all                           # ClosedCase + PolicyChunk from data/out
    uv run python -m rag.load_vectors --vertex ClosedCase --attr note_emb --psv data/out/vec_ClosedCase.psv
    uv run python -m rag.load_vectors --status                        # print /vector/status for all three attrs
    uv run python -m rag.load_vectors --ensure-schema                 # runs graph/schema_change_vectors.gsql (GLOBAL job) if attrs missing
    uv run python -m rag.load_vectors --dry-run --all                 # print the GSQL, touch nothing

Mechanics (verified against pyTigerGraph 2.0.4 source in mcpvenv and ecosys VectorSearch.md, 2026-09-19):
  * Loading job:  CREATE LOADING JOB load_vec_<vertex>_<attr> FOR GRAPH FraudGraph {
                    DEFINE FILENAME f;
                    LOAD f TO VECTOR ATTRIBUTE <attr> ON VERTEX <vertex> VALUES ($0, SPLIT($1, ",")) USING SEPARATOR="|", HEADER="false";
                  }
    (identical to what tigergraph-mcp's load_vectors_from_csv generates).
  * Upload+run:   conn.runLoadingJobWithFile(filePath, fileTag="f", jobName, sep="|", eol="\\n", timeout=600000, sizeLimit=128000000)
                  -> POST /restpp/ddl/FraudGraph?tag=<job>&filename=f&sep=|&eol=\\n with GSQL-TIMEOUT / RESPONSE-LIMIT headers;
                  the whole file is read into memory, so files are split into <= 40 MB parts (Savanna's Nginx body cap
                  is 200 MB by default; the day-0 150 MB POST test decides whether parts can grow).
  * Vectors load onto EXISTING vertices: the ClosedCase / PolicyChunk vertex loads must run first (graph/load_all.py).
  * Index status:  conn.getVectorIndexStatus(vertexType=, vectorName=) -> GET /restpp/vector/status/FraudGraph/<vertex>/<attr>
                  -> {"NeedRebuildServers": [...]} ; empty list == Ready_for_query (this is what tigergraph-mcp's
                  get_vector_index_status reports). conn.getVectorStatus(vertex, attr) returns the same as a bool.
  * Single-case upsert (P10 persist): conn.upsertVertex("AgentCase", id, {..attrs.., "note_emb": [floats]}) - pyTigerGraph
                  wraps every attribute as {"value": v}, which is the documented REST JSON for vector attributes.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

from ops.console import Col, Table, detail, fail, header, joinlist, ok, progress, rule, step, summary, warn

from . import config

PART_BYTES = 40 * 1024 * 1024

# The vector attributes are added by the GLOBAL schema-change job in graph/schema_change_vectors.gsql (the vertex
# types are created at USE GLOBAL, and "local schema change jobs cannot alter global vertex types" - gsql-ref 4.2).
# `--ensure-schema` sends that file verbatim; there is no second copy of the DDL here.
SCHEMA_CHANGE_FILE = Path(os.environ.get("HHGOA_SCHEMA_CHANGE_VECTORS", config.REPO_ROOT / "graph" / "schema_change_vectors.gsql"))


DIMENSION_RE = re.compile(r"DIMENSION\s*=\s*\d+")


def render_dimension(text: str, dim: int = 0) -> str:
    """Rewrite every `DIMENSION=<n>` in the schema-change DDL to the configured EMBED_DIM, so switching
    embedding model/backend (e.g. BAAI/bge-small-en-v1.5 at 384-d) needs no edit to the .gsql file.
    The file keeps the default (1024) as its literal, which is what a human reads."""
    return DIMENSION_RE.sub(f"DIMENSION={dim or config.EMBED_DIM}", text)


def schema_change_gsql(path: Path = SCHEMA_CHANGE_FILE, dim: int = 0) -> str:
    """Text of graph/schema_change_vectors.gsql without its `//` comment lines, with DIMENSION rendered
    from config.EMBED_DIM (what conn.gsql() receives)."""
    if not path.exists():
        raise FileNotFoundError(f"{path} not found - the graph module owns the vector DDL (graph/schema_change_vectors.gsql)")
    lines = [ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("//")]
    text = "\n".join(lines).strip() + "\n"
    assert "GLOBAL SCHEMA_CHANGE JOB" in text and "ADD VECTOR ATTRIBUTE" in text, f"{path} is not the vector schema-change job"
    return render_dimension(text, dim)


def loading_job_gsql(vertex: str, attr: str, graph: str = "FraudGraph") -> tuple[str, str]:
    job = f"load_vec_{vertex}_{attr}"
    gsql = (f"USE GRAPH {graph}\n"
            f"CREATE LOADING JOB {job} FOR GRAPH {graph} {{\n"
            f"  DEFINE FILENAME f;\n"
            f"  LOAD f TO VECTOR ATTRIBUTE {attr} ON VERTEX {vertex} VALUES ($0, SPLIT($1, \",\")) "
            f"USING SEPARATOR=\"|\", HEADER=\"false\";\n"
            f"}}\n")
    return job, gsql


def connect():
    """Sync pyTigerGraph connection for offline scripts (Savanna: host https://<id>.i.tgcloud.io + Database Secret)."""
    from pyTigerGraph import TigerGraphConnection

    if not config.TG_HOST or not config.TG_SECRET:
        raise RuntimeError("TG_HOST / TG_SECRET not set")
    conn = TigerGraphConnection(host=config.TG_HOST, graphname=config.TG_GRAPHNAME, gsqlSecret=config.TG_SECRET)
    conn.getToken(config.TG_SECRET)
    conn.customizeHeader(timeout=config.TG_LOAD_TIMEOUT_MS, responseSize=64_000_000)
    return conn


def ensure_awake(fn, *args, attempts: int = 8, **kwargs):
    """Call ``fn(*args, **kwargs)`` through Savanna auto-resume with the shared ``ops.ensure_awake`` policy.

    Same classifier (502/503/504, connection errors, timeouts, an HTML start page; never 401/403/auth) and the
    same 1, 2, 4 ... 60 s backoff bounded by TG_RESUME_MAX_WAIT_S (240 s) as every other target.
    ``attempts`` is IGNORED: it is kept only so the existing call sites keep their signature.
    """
    from ops.ensure_awake import ensure_awake as _ea

    def _on_retry(attempt: int, delay: float, exc: BaseException) -> None:
        warn(f"{type(exc).__name__} from TigerGraph (Savanna auto-resume); retry {attempt} in {delay:.0f}s")
        detail(str(exc)[:160])

    return _ea(on_retry=_on_retry)(fn)(*args, **kwargs)


def split_psv(path: Path, part_bytes: int = PART_BYTES) -> list[Path]:
    if path.stat().st_size <= part_bytes:
        return [path]
    parts: list[Path] = []
    buf, size, k = [], 0, 0
    with path.open() as f:
        for line in f:
            if size + len(line) > part_bytes and buf:
                p = path.with_suffix(f".part{k:02d}.psv")
                p.write_text("".join(buf))
                parts.append(p)
                buf, size, k = [], 0, k + 1
            buf.append(line)
            size += len(line)
    if buf:
        p = path.with_suffix(f".part{k:02d}.psv")
        p.write_text("".join(buf))
        parts.append(p)
    return parts


def vector_attrs_present(conn, vertex: str) -> list[str]:
    """Names of vector attributes on `vertex` (pyTigerGraph getVertexVectors; falls back to LS text)."""
    try:
        vecs = conn.getVertexVectors(vertex)
        return [v.get("Name") or v.get("name") for v in vecs] if isinstance(vecs, list) else []
    except Exception:  # noqa: BLE001
        txt = str(conn.gsql(f"USE GRAPH {config.TG_GRAPHNAME}\nLS"))
        return [a for a in config.VECTOR_ATTRS.values() if f"VECTOR ATTRIBUTE {a}" in txt or f"{a}(" in txt]


def ensure_schema(conn, dry_run: bool = False) -> None:
    step(f"checking the {len(config.VECTOR_ATTRS)} vector attributes against the live schema")
    missing = [(v, a) for v, a in config.VECTOR_ATTRS.items() if a not in vector_attrs_present(conn, v)]
    t = Table(Col("vertex", max_width=14), Col("attribute", max_width=12), Col("dim", align="right", width=6),
              Col("present", width=7, align="center"), title="vector attributes (graph/schema_change_vectors.gsql)")
    for v, a in config.VECTOR_ATTRS.items():
        here = (v, a) not in missing
        t.add_row(v, a, config.EMBED_DIM, "yes" if here else "no", style=None if here else "yellow")
    t.print()
    if not missing:
        ok("every vector attribute is already on the graph; nothing to do")
        return
    warn(f"{len(missing)} attribute(s) missing; sending {SCHEMA_CHANGE_FILE}")
    gsql = schema_change_gsql()
    rule("GLOBAL SCHEMA_CHANGE JOB")
    print(gsql)                       # verbatim DDL: kept copy-pasteable, never indented or truncated
    rule()
    if dry_run:
        ok("dry run: nothing was sent")
        return
    for line in str(conn.gsql(gsql)).splitlines():
        detail(line)
    ok("schema change applied")


def index_status(conn, vertex: str, attr: str) -> dict:
    """{"ready": bool, "need_rebuild": [...]} from GET /restpp/vector/status/<graph>/<vertex>/<attr>."""
    res = conn.getVectorIndexStatus(vertexType=vertex, vectorName=attr)
    need = res.get("NeedRebuildServers", []) if isinstance(res, dict) else []
    return {"ready": len(need) == 0, "need_rebuild": need, "raw": res}


def wait_index_ready(conn, vertex: str, attr: str, timeout_s: int = 600, poll_s: float = 3.0) -> bool:
    """Poll /vector/status until the HNSW index is Ready_for_query. Shows the rebuild rather than going silent."""
    t0 = time.time()
    step(f"waiting for the HNSW index on {vertex}.{attr} (polling /vector/status every {poll_s:.0f}s, "
         f"timeout {timeout_s}s)")
    while time.time() - t0 < timeout_s:
        st = ensure_awake(index_status, conn, vertex, attr)
        if st["ready"]:
            ok(f"{vertex}.{attr} Ready_for_query after {time.time() - t0:.0f}s")
            return True
        detail(f"Rebuild_processing on {len(st['need_rebuild'])} server(s): "
               f"{joinlist(st['need_rebuild'])} ({time.time() - t0:.0f}s elapsed)")
        time.sleep(poll_s)
    fail(f"{vertex}.{attr} still rebuilding after {timeout_s}s")
    return False


def load_psv(conn, psv: Path, vertex: str, attr: str, dry_run: bool = False) -> dict:
    """Create (or replace) the loading job, upload every part, return the summed loading statistics."""
    job, gsql = loading_job_gsql(vertex, attr, config.TG_GRAPHNAME)
    if dry_run:
        rule(f"{vertex}.{attr} loading job")
        print(gsql)                   # verbatim GSQL: kept copy-pasteable
        detail(f"runLoadingJobWithFile({psv}, 'f', '{job}', sep='|', eol='\\n', "
               f"timeout={config.TG_LOAD_TIMEOUT_MS}, sizeLimit=128000000)")
        return {"dry_run": True}
    step(f"creating loading job {job}")
    try:
        conn.dropLoadingJob(job)
    except Exception:  # noqa: BLE001 - first run
        pass
    r = conn.gsql(gsql)
    if "error" in str(r).lower() and "Successfully created" not in str(r):
        raise RuntimeError(f"loading job create failed: {r}")
    total = {"parts": 0, "valid": 0, "invalid": 0}
    parts = split_psv(psv)
    t = Table(
        Col("part", max_width=26),
        Col("MB", align="right", width=8),
        Col("valid", align="right", width=10),
        Col("invalid", align="right", width=8),
        Col("seconds", align="right", width=8),
        title=f"{vertex}.{attr} <- {psv.name} ({len(parts)} part(s), <= {PART_BYTES // (1024 * 1024)} MB each)",
    )
    with progress(f"uploading {vertex}.{attr}", total=len(parts)) as p:
        for part in parts:
            t0 = time.time()
            res = ensure_awake(conn.runLoadingJobWithFile, str(part), "f", job, sep="|", eol="\n",
                               timeout=config.TG_LOAD_TIMEOUT_MS, sizeLimit=128_000_000)
            stats = (res[0] if isinstance(res, list) and res else res) or {}
            # TigerGraph 4.2.5 nests these under statistics.parsingStatistics.objectLevel.* rather than the
            # flat statistics.* some docs and earlier versions show (same trap as graph/load_all.py
            # load_result_stats); accept either. A VECTOR attribute load reports under objectLevel.embedding
            # (typeName "Vertex:attr", e.g. "PolicyChunk:emb") rather than objectLevel.vertex, which a plain
            # vertex/edge CSV load uses -- read both so this works for either loading job.
            raw_stats = stats.get("statistics", {}) if isinstance(stats, dict) else {}
            parsing = raw_stats.get("parsingStatistics")
            object_level = parsing["objectLevel"] if parsing else raw_stats
            vstats = object_level.get("vertex", []) + object_level.get("embedding", [])
            valid = sum(int(v.get("validObject", 0)) for v in vstats)
            invalid = sum(int(v.get("invalidAttribute", 0)) + int(v.get("noIdFound", 0)) for v in vstats)
            total["parts"] += 1
            total["valid"] += valid
            total["invalid"] += invalid
            t.add_row(part.name, f"{part.stat().st_size / 1e6:,.1f}", f"{valid:,}", f"{invalid:,}",
                      f"{time.time() - t0:.1f}", style="red" if invalid else None)
            p.advance()
    t.print()
    if total["invalid"]:
        warn(f"{total['invalid']:,} row(s) rejected (invalidAttribute / noIdFound) - "
             "vectors only load onto vertices that already exist")
    else:
        ok(f"{total['valid']:,} vectors loaded onto {vertex}.{attr}, 0 rejected")
    return total


def upsert_case_vector(conn, case_id: str, vector: list[float], attributes: dict) -> int:
    """P10 persist: AgentCase attributes AND note_emb in one upsertVertex call (REST JSON {"note_emb": {"value": [...]}})."""
    if len(vector) != config.EMBED_DIM:
        raise ValueError(f"vector has {len(vector)} dims, expected {config.EMBED_DIM}")
    attrs = dict(attributes)
    attrs["note_emb"] = vector
    return ensure_awake(conn.upsertVertex, "AgentCase", case_id, attrs)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vertex")
    ap.add_argument("--attr")
    ap.add_argument("--psv", type=Path)
    ap.add_argument("--all", action="store_true", help="ClosedCase.note_emb + PolicyChunk.emb from data/out")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--ensure-schema", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-wait", action="store_true")
    a = ap.parse_args(argv)

    jobs: list[tuple[str, str, Path]] = []
    if a.all:
        jobs = [("ClosedCase", "note_emb", config.OUT_DIR / "vec_ClosedCase.psv"),
                ("PolicyChunk", "emb", config.OUT_DIR / "vec_PolicyChunk.psv")]
    elif a.vertex and a.attr and a.psv:
        jobs = [(a.vertex, a.attr, a.psv)]

    mode = ("status" if a.status else "ensure-schema" if a.ensure_schema else
            "dry run" if a.dry_run else "load")
    header("rag.load_vectors",
           "load vec_*.psv into TigerGraph and wait for the HNSW index",
           {"graph": config.TG_GRAPHNAME, "host": config.TG_HOST or "(TG_HOST not set)",
            "mode": mode, "dim": config.EMBED_DIM,
            "jobs": joinlist([f"{v}.{at}" for v, at, _ in jobs], empty="-"),
            "wait for index": "no" if a.no_wait else "yes",
            "load timeout": f"{config.TG_LOAD_TIMEOUT_MS} ms"})

    if a.dry_run and not a.status and not a.ensure_schema:
        for v, at, p in jobs:
            load_psv(None, p, v, at, dry_run=True)
        summary("rag.load_vectors dry run", {"jobs": len(jobs), "sent to the graph": "nothing"})
        return 0

    step(f"connecting to {config.TG_GRAPHNAME} (retries through Savanna auto-resume)")
    conn = ensure_awake(connect)
    ok("connected")
    if a.ensure_schema:
        ensure_schema(conn, a.dry_run)
    if a.status:
        t = Table(Col("vertex", max_width=14), Col("attribute", max_width=12),
                  Col("index", width=18, align="center"), Col("rebuilding on", max_width=30),
                  title="/vector/status")
        not_ready = 0
        for v, at in config.VECTOR_ATTRS.items():
            st = ensure_awake(index_status, conn, v, at)
            not_ready += 0 if st["ready"] else 1
            t.add_row(v, at, "Ready_for_query" if st["ready"] else "Rebuild_processing",
                      joinlist(st["need_rebuild"]), style=None if st["ready"] else "yellow")
        t.print()
        summary("vector index status",
                {"attributes": len(config.VECTOR_ATTRS), "ready": len(config.VECTOR_ATTRS) - not_ready,
                 "rebuilding": not_ready},
                status="ok" if not not_ready else "warn")
        return 0
    loaded = {"valid": 0, "invalid": 0, "parts": 0}
    for v, at, p in jobs:
        if not p.exists():
            fail(f"missing {p}")
            detail("run `python -m rag.embed --input <jsonl> --out <psv>` first")
            summary("rag.load_vectors failed", {"missing": str(p)}, status="fail")
            return 1
        rule(f"{v}.{at}")
        total = load_psv(conn, p, v, at)
        for k in loaded:
            loaded[k] += int(total.get(k, 0) or 0)
        if not a.no_wait:
            wait_index_ready(conn, v, at)
    summary("rag.load_vectors complete",
            {"attributes loaded": len(jobs), "file parts": loaded["parts"],
             "vectors loaded": f"{loaded['valid']:,}", "rejected": f"{loaded['invalid']:,}",
             "index": "not waited for (--no-wait)" if a.no_wait else "Ready_for_query"},
            status="ok" if not loaded["invalid"] else "warn")
    return 0


if __name__ == "__main__":
    sys.exit(main())
