"""graph/load_all.py — FraudGraph DDL, loading jobs, chunk upload and exact count assertions (PLAN §3.6, day 2).

    uv run python graph/load_all.py                     # ensure schema, (re)create jobs, upload every file, assert counts
    uv run python graph/load_all.py --schema-only       # day-2 step 1: graph/schema.gsql only (skipped when FraudGraph exists)
    uv run python graph/load_all.py --jobs-only         # drop + re-create the 18 loading jobs, nothing else
    uv run python graph/load_all.py --only txn next     # a subset (file stems of contracts/csv_columns.yaml; txn = all chunks)
    uv run python graph/load_all.py --only document policy_chunk chunk_of about --skip-counts   # day 7, rag/chunk.py output
    uv run python graph/load_all.py --expected          # offline: the counts the graph must reach, computed from data/out
    uv run python graph/load_all.py --sep '|#|'         # separator fallback (module A open issue 1): re-encode + rewrite jobs
    uv run python graph/load_all.py --drop-all --yes    # USE GLOBAL DROP ALL first (rebuild.py --from schema on a stale workspace)

Mechanics (pyTigerGraph 2.0.4, read in scratchpad/ptg and the installed package):
  * DDL and jobs go through conn.gsql() = POST /gsql/v1/statements with Basic auth "__GSQL__secret:<secret>", so the
    schema stage works before FraudGraph exists and before any token is minted (graph/tg.py connect(token=False)).
  * every file: conn.runLoadingJobWithFile(path, "f", job, sep=SEP, eol="\\n", timeout=600000, sizeLimit=128000000)
    -> POST /restpp/ddl/FraudGraph?tag=<job>&filename=f&sep=<SEP>&eol=<EOL> with GSQL-TIMEOUT=600000 (ms; timeout=0
    would mean the 16 s server default) and RESPONSE-LIMIT headers; the file is read whole into memory (largest chunk
    45 MiB; Savanna's Nginx.ClientMaxBodySize is 200 MB by default). The reply's `statistics` block is checked:
    validLine == lines in the file, rejectLine / notEnoughToken / oversizeToken / invalidJson == 0, and per type
    noIdFound / invalidAttribute / invalidPrimaryId == 0 (failedConditionLine is expected: WHERE guards on the edges).
  * ensure_awake() (graph/tg.py) retries 502/503/504 and connection errors while a suspended workspace resumes; a
    retried load re-upserts the same rows, so retries are safe.
  * loading jobs are DROPped and re-CREATEd on every run: ALTER VERTEX (graph/schema_change_vectors.gsql) marks
    the jobs that load an altered type `disabled` (gsql-ref 4.2, Modifying a graph schema), so a reload after the
    vectors stage must not assume the day-2 jobs still work.
  * the four GraphRAG files (rag/chunk.py: `,`-separated `"`-quoted, csv.QUOTE_ALL) and fraud_pattern.csv when
    rag/chunk.py overwrote the ETL's tab version are re-encoded losslessly to the load separator under
    data/out/_enc/ before upload (sniffed per file: first line starts with `"` and has no tab); every other file
    is uploaded as written by etl/export_graph_csvs.py.
  * expected counts come from the files themselves (distinct ids, distinct edge pairs, non-empty foreign keys of
    the transaction chunks), are cross-checked with data/out/manifest.json, and compared with getVertexCount /
    getEdgeCount after the load. Missing GraphRAG files (before day 7) are skipped with a warning; missing ETL
    files are an error.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # repo root, for ops.console
from check_contracts import FILE_TO_JOB, RAG_COLUMNS, _strip_comments  # noqa: E402
from check_contracts import YAML as CSV_YAML  # noqa: E402
from tg import DATA_OUT, GRAPH_DIR, backoff_delays, connect, ensure_awake, gsql, is_transient  # noqa: E402

from ops.console import Col, Table, detail, fail, header, joinlist, ok, step, summary, warn  # noqa: E402

GRAPH = "FraudGraph"
SCHEMA_GSQL = GRAPH_DIR / "schema.gsql"
JOBS_GSQL = GRAPH_DIR / "loading_jobs.gsql"
LOAD_TIMEOUT_MS = 600_000
SIZE_LIMIT = 128_000_000
FILE_TAG = "f"
EOL = "\n"

# Dependency order: vertex files, then the transaction chunks (Transaction + its five edges), then the edge files.
VERTEX_STEMS = ["customer", "card", "device_profile", "email_domain", "billing_region", "fraud_pattern",
                "closed_case", "document", "policy_chunk"]
TXN_STEM = "txn"
EDGE_STEMS = ["owns", "next", "involves", "on_card", "connected_to", "matches", "chunk_of", "about"]
ORDER = VERTEX_STEMS + [TXN_STEM] + EDGE_STEMS
RAG_STEMS = {"document", "policy_chunk", "chunk_of", "about"}          # written on day 7 by rag/chunk.py
RAG_VERTEX_TYPES = {"document": "Document", "policy_chunk": "PolicyChunk"}
RAG_EDGE_TYPES = {"chunk_of": "CHUNK_OF", "about": "ABOUT"}
# Edge types load_txn creates from a non-empty column (csv_columns.yaml `edges_from_columns`).
TXN_EDGE_COLUMNS = {"FROM_DEVICE": "device_id", "PURCHASER_EMAIL": "p_email", "RECIPIENT_EMAIL": "r_email", "BILLED_IN": "addr1"}
# check_contracts keys the transaction file by its yaml stem `txn_NNN`; the plan uses `txn`.
JOB_FOR = {**FILE_TO_JOB, TXN_STEM: FILE_TO_JOB["txn_NNN"]}


@dataclass
class FileSpec:
    stem: str
    job: str
    columns: list[str]
    vertex: str | None                 # vertex type the file creates (None for edge-only files)
    edge: str | None                   # edge type for edge-only files
    paths: list[Path] = field(default_factory=list)


# ------------------------------------------------------------------------------------------ plan --------------
def load_spec(yaml_path: Path = CSV_YAML) -> dict:
    return yaml.safe_load(Path(yaml_path).read_text())


def file_plan(spec: dict, data_out: Path = DATA_OUT, only: list[str] | None = None) -> list[FileSpec]:
    """One FileSpec per yaml file (plus the four rag files) in load order; paths resolved under data_out."""
    files = {Path(k).stem.replace("_NNN", ""): v for k, v in spec["files"].items()}
    for stem, cols in RAG_COLUMNS.items():
        files.setdefault(stem, {"loads": [], "columns": [{"name": c} for c in cols]})
    plan: list[FileSpec] = []
    for stem in ORDER:
        if only and stem not in only:
            continue
        f = files[stem]
        cols = [c["name"] for c in f["columns"]]
        loads = list(f.get("loads") or [])
        vertex = RAG_VERTEX_TYPES.get(stem) or next((v for v in loads if not v.isupper()), None)
        edge = None if stem == TXN_STEM else (RAG_EDGE_TYPES.get(stem) or next((e for e in loads if e.isupper()), None))
        if stem == TXN_STEM:
            paths = sorted(p for p in Path(data_out).glob("txn_*.csv") if re.fullmatch(r"txn_\d{3}\.csv", p.name))
        else:
            paths = [Path(data_out) / f"{stem}.csv"]
        plan.append(FileSpec(stem, JOB_FOR[stem], cols, vertex, edge, paths))
    if only:
        unknown = sorted(set(only) - set(ORDER))
        if unknown:
            raise SystemExit(f"--only: unknown file stem(s) {joinlist(unknown)}; choose from {joinlist(ORDER)}")
    return plan


def _n(value) -> str:
    """A count as a right-alignable, thousands-separated cell: 590742 -> '590,742'."""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def check_paths(plan: list[FileSpec], *, quiet: bool = False) -> list[FileSpec]:
    """Drop rag files that do not exist yet (day 7); fail on a missing ETL file.

    `quiet` keeps stdout empty for `--expected`, whose stdout is parsed as JSON.
    """
    kept = []
    for fs in plan:
        missing = [p for p in fs.paths if not p.exists()]
        if not fs.paths or missing:
            if fs.stem in RAG_STEMS:
                if not quiet:
                    warn(f"{fs.stem}: {joinlist([p.name for p in missing], empty='no file')} not written yet (rag/chunk.py, day 7) - skipped")
                continue
            raise SystemExit(f"{fs.stem}: missing {joinlist([p.name for p in missing], empty='every chunk')} - run etl.export_graph_csvs first")
        kept.append(fs)
    return kept


# ------------------------------------------------------------------------------------------ files -------------
def sniff_quoted_csv(path: Path) -> bool:
    with Path(path).open("rb") as fh:
        first = fh.read(65536).split(b"\n", 1)[0]
    return first.startswith(b'"') and b"\t" not in first


def count_lines(path: Path) -> int:
    n = 0
    with Path(path).open("rb") as fh:
        while chunk := fh.read(1 << 20):
            n += chunk.count(b"\n")
    return n


def rows(path: Path):
    """Yield the fields of every row, whichever of the two on-disk encodings the file uses."""
    path = Path(path)
    if sniff_quoted_csv(path):
        with path.open(newline="", encoding="utf-8") as fh:
            yield from csv.reader(fh)
    else:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                yield line.rstrip("\n").split("\t")


def encode_for_load(path: Path, sep: str, enc_dir: Path) -> tuple[Path, int]:
    """(path to upload, line count). Files already in the load encoding are uploaded in place; quoted CSV and
    non-tab separators are re-encoded under enc_dir with the same lossless rule as the ETL (no sep / newline)."""
    path = Path(path)
    if sep == "\t" and not sniff_quoted_csv(path):
        return path, count_lines(path)
    enc_dir.mkdir(parents=True, exist_ok=True)
    out = enc_dir / path.name
    n = 0
    with out.open("w", encoding="utf-8", newline="") as dst:
        for row in rows(path):
            for v in row:
                if sep in v or "\n" in v or "\r" in v:
                    raise ValueError(f"{path.name}: field {v[:60]!r} contains the separator or a newline; not loadable")
            dst.write(sep.join(row) + EOL)
            n += 1
    return out, n


def expected_counts(plan: list[FileSpec]) -> dict[str, int]:
    """Vertex and edge counts the graph must show after loading exactly these files."""
    exp: dict[str, int] = {}
    for fs in plan:
        if fs.stem == TXN_STEM:
            idx = {c: i for i, c in enumerate(fs.columns)}
            ids: set[str] = set()
            edge_n = dict.fromkeys(TXN_EDGE_COLUMNS, 0)
            for p in fs.paths:
                for row in rows(p):
                    ids.add(row[0])
                    for e, col in TXN_EDGE_COLUMNS.items():
                        if row[idx[col]] != "":
                            edge_n[e] += 1
            exp["Transaction"] = len(ids)
            exp["MADE"] = len(ids)
            exp.update(edge_n)
        elif fs.vertex:
            exp[fs.vertex] = len({row[0] for p in fs.paths for row in rows(p)})
        else:
            exp[fs.edge] = len({(row[0], row[1]) for p in fs.paths for row in rows(p)})
    return exp


def manifest_mismatches(plan: list[FileSpec], data_out: Path) -> list[str]:
    """Row counts in data/out/manifest.json (etl/export_graph_csvs.py) vs the files on disk."""
    mf = Path(data_out) / "manifest.json"
    if not mf.exists():
        return [f"{mf} not found (etl/export_graph_csvs.py writes it)"]
    man = json.loads(mf.read_text())
    out = []
    for fs in plan:
        for p in fs.paths:
            m = man.get(p.name)
            if m is None:
                continue
            n = count_lines(p)
            if m["rows"] != n or m["columns"] != len(fs.columns):
                out.append(f"{p.name}: manifest rows={m['rows']} cols={m['columns']} vs file rows={n} cols={len(fs.columns)}")
    return out


# ------------------------------------------------------------------------------------------ server -------------
def graph_exists(conn) -> bool:
    @ensure_awake()
    def _probe():
        try:
            return len(conn.getVertexTypes(force=True)) > 0
        except Exception as exc:  # noqa: BLE001 - "graph does not exist" is the expected answer on a fresh workspace
            if is_transient(exc):
                raise
            return False

    return _probe()


def apply_schema(conn, drop_all: bool = False) -> bool:
    """Run graph/schema.gsql. Returns False when FraudGraph already exists and nothing was done."""
    if drop_all:
        warn("USE GLOBAL / DROP ALL - every vertex, edge, job and query on the workspace")
        gsql(conn, "USE GLOBAL\nDROP ALL", ok_if=("everything is dropped", "successfully"))
        ok("workspace dropped")
    elif graph_exists(conn):
        ok(f"{GRAPH} already exists - schema.gsql skipped")
        detail("use --drop-all --yes to start over")
        return False
    step(f"applying {SCHEMA_GSQL.name}")
    t0 = time.time()
    gsql(conn, SCHEMA_GSQL.read_text())
    ok(f"schema applied in {time.time() - t0:.0f}s")
    return True


def jobs_text(sep: str = "\t") -> str:
    text = JOBS_GSQL.read_text()
    if sep != "\t":
        text = text.replace('SEPARATOR="\\t"', f'SEPARATOR="{sep}"')
    return text


def job_names(text: str) -> list[str]:
    """Job names in statement order (comments stripped first: the header says "one CREATE LOADING JOB per ...")."""
    return re.findall(r"CREATE LOADING JOB (\w+)", _strip_comments(text))


def create_jobs(conn, sep: str = "\t") -> list[str]:
    text = jobs_text(sep)
    names = job_names(text)
    step(f"re-creating {len(names)} loading jobs (separator {sep!r})")
    for job in names:
        gsql(conn, f"USE GRAPH {GRAPH}\nDROP JOB {job}",
             ok_if=("does not exist", "dropped", "could not be found"), quiet=True)
    reply = gsql(conn, text, ok_if=("successfully created loading jobs",))
    created = {n.strip() for m in re.findall(r"Successfully created loading jobs: \[(.*?)\]", reply) for n in m.split(",")}
    missing = [n for n in names if n not in created]
    if missing:
        raise RuntimeError(f"loading jobs not created: {missing}\n{reply[-2000:]}")
    ok(f"{len(names)} loading jobs created")
    return names


def load_result_stats(res) -> dict:
    """Flatten the /ddl reply (a list of {sourceFileName, statistics}) into one dict of counters.

    TigerGraph 4.2.5's REST++ nests the counters under statistics.parsingStatistics.{fileLevel,objectLevel}
    rather than the flat statistics.{validLine,vertex,...} shape some docs and earlier versions show; accept
    either so this keeps working across versions.
    """
    counters = ("validLine", "rejectLine", "failedConditionLine", "notEnoughToken", "invalidJson", "oversizeToken")
    out: dict = {k: 0 for k in counters}
    out["objects"] = {}
    for it in res if isinstance(res, list) else [res]:
        st = it.get("statistics", {}) if isinstance(it, dict) else {}
        parsing = st.get("parsingStatistics")
        file_level = parsing["fileLevel"] if parsing else st
        object_level = parsing["objectLevel"] if parsing else st
        for k in counters:
            out[k] += int(file_level.get(k, 0) or 0)
        for kind in ("vertex", "edge"):
            for o in object_level.get(kind, []) or []:
                d = out["objects"].setdefault(o.get("typeName", "?"), {"validObject": 0, "noIdFound": 0, "invalidAttribute": 0, "invalidPrimaryId": 0})
                for k in d:
                    d[k] += int(o.get(k, 0) or 0)
    return out


def check_stats(stats: dict, n_lines: int) -> list[str]:
    problems = []
    if stats["validLine"] != n_lines:
        problems.append(f"validLine {stats['validLine']} != {n_lines} lines in the file")
    for k in ("rejectLine", "notEnoughToken", "invalidJson", "oversizeToken"):
        if stats[k]:
            problems.append(f"{k}={stats[k]}")
    for name, d in stats["objects"].items():
        for k in ("noIdFound", "invalidAttribute", "invalidPrimaryId"):
            if d.get(k):
                problems.append(f"{name}.{k}={d[k]}")
    return problems


def load_file(conn, job: str, path: Path, sep: str, n_lines: int) -> dict:
    @ensure_awake()
    def _run():
        return conn.runLoadingJobWithFile(str(path), FILE_TAG, job, sep=sep, eol=EOL, timeout=LOAD_TIMEOUT_MS, sizeLimit=SIZE_LIMIT)

    size = Path(path).stat().st_size
    t0 = time.time()
    res = _run()
    dt = time.time() - t0
    if res is None:
        raise RuntimeError(f"{path.name}: runLoadingJobWithFile returned None (unreadable file?)")
    stats = load_result_stats(res)
    stats.update(job=job, file=path.name, bytes=size, seconds=round(dt, 1))
    problems = check_stats(stats, n_lines)
    if problems:
        raise RuntimeError(f"{path.name} ({job}): {'; '.join(problems)}\nraw: {json.dumps(res)[:1500]}")
    return stats


def vertex_count(conn, vtype: str) -> int:
    @ensure_awake()
    def _run():
        return conn.getVertexCount(vtype, realtime=True)

    return int(_run())


def edge_count(conn, etype: str) -> int:
    @ensure_awake()
    def _run():
        return conn.getEdgeCount(etype)

    r = _run()
    if isinstance(r, dict):
        r = r.get(etype, next(iter(r.values()), 0))
    return int(r)


def verify_counts(conn, expected: dict[str, int]) -> tuple[dict[str, int], list[str]]:
    """Ask the graph for every count and compare. Rendering is main()'s job (one table, after the loop)."""
    actual: dict[str, int] = {}
    mism = []
    for name, want in expected.items():
        got = edge_count(conn, name) if name.isupper() else vertex_count(conn, name)
        actual[name] = got
        if got != want:
            mism.append(f"{name}: expected {want}, got {got}")
    return actual, mism


def counts_table(expected: dict[str, int], actual: dict[str, int]) -> Table:
    """expected-vs-actual with a pass mark per vertex/edge type (590,742 / 576,425 / 140,784 / 14,955)."""
    t = Table(
        Col("type", max_width=18),
        Col("kind", width=6),
        Col("expected", align="right", width=11),
        Col("actual", align="right", width=11),
        Col("diff", align="right", width=9),
        Col("ok", width=8, align="center"),
        title=f"counts asserted against the graph ({len(expected)} types)",
    )
    for name, want in expected.items():
        got = actual.get(name, 0)
        good = got == want
        t.add_row(name, "edge" if name.isupper() else "vertex", _n(want), _n(got),
                  "-" if good else _n(got - want), "PASS" if good else "MISMATCH",
                  style=None if good else "red")
    return t


# ------------------------------------------------------------------------------------------ main --------------
def main(argv: list[str] | None = None, connect_fn=connect) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--schema-only", action="store_true")
    ap.add_argument("--jobs-only", action="store_true")
    ap.add_argument("--only", nargs="*", default=None, help=f"file stems to load, from {ORDER}")
    ap.add_argument("--expected", action="store_true", help="print expected counts (offline) and exit")
    ap.add_argument("--sep", default=None, help="load separator (default: contracts/csv_columns.yaml format.separator)")
    ap.add_argument("--drop-all", action="store_true")
    ap.add_argument("--yes", action="store_true", help="do not ask before DROP ALL")
    ap.add_argument("--skip-counts", action="store_true")
    ap.add_argument("--data-out", type=Path, default=DATA_OUT)
    ap.add_argument("--report", type=Path, default=None, help="write per-file statistics + counts as JSON")
    args = ap.parse_args(argv)
    # graph/tg.py echoes every GSQL reply at INFO; that echo is the wall of text this output replaces.
    # WARNING keeps the ensure_awake resume retries and hard errors visible on stderr.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    spec = load_spec()
    sep = args.sep or spec["format"]["separator"]

    if args.expected:
        # --expected is a MACHINE contract: stdout must stay parseable JSON and nothing else
        # (tests/unit/test_graph_contracts.py::test_expected_flag_is_offline does json.loads on it).
        plan = check_paths(file_plan(spec, args.data_out, args.only), quiet=True)
        t0 = time.time()
        exp = expected_counts(plan)
        print(json.dumps({"expected": exp, "manifest_mismatches": manifest_mismatches(plan, args.data_out),
                          "files": {p.name: count_lines(p) for fs in plan for p in fs.paths},
                          "seconds": round(time.time() - t0, 1)}, indent=1))
        return 0

    stages = ["schema"] if args.schema_only else (["jobs"] if args.jobs_only else ["schema", "jobs", "load"] + ([] if args.skip_counts else ["counts"]))
    header(
        "graph.load_all",
        f"{GRAPH} DDL, loading jobs, chunk upload and exact count assertions",
        {"graph": GRAPH, "data out": args.data_out, "separator": repr(sep), "stages": " -> ".join(stages),
         "only": joinlist(args.only, empty="every file"), "timeout": f"{LOAD_TIMEOUT_MS // 1000}s per file",
         "report": args.report or "-"},
    )
    plan = check_paths(file_plan(spec, args.data_out, args.only))

    if args.drop_all and not args.yes:
        if input("DROP ALL on the workspace - every vertex, edge, job and query. Type YES to continue: ").strip() != "YES":
            return 2

    conn = connect_fn(timeout_ms=LOAD_TIMEOUT_MS, token=not args.schema_only)
    if not args.jobs_only:
        apply_schema(conn, args.drop_all)
    if args.schema_only:
        summary("schema stage complete", {"graph": GRAPH, "schema": SCHEMA_GSQL.name, "next": "graph/load_all.py (jobs + upload)"})
        return 0
    jobs = create_jobs(conn, sep)
    if args.jobs_only:
        summary("jobs stage complete", {"graph": GRAPH, "loading jobs": len(jobs), "separator": repr(sep)})
        return 0

    mismatches = manifest_mismatches(plan, args.data_out)
    for m in mismatches:
        warn(f"manifest: {m}")

    report: dict = {"separator": sep, "files": {}, "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    uploads = [(fs, p) for fs in plan for p in fs.paths]
    total_mb = sum(p.stat().st_size for _, p in uploads) / 1e6
    step(f"uploading {len(uploads)} files ({total_mb:,.1f} MB) through POST /restpp/ddl")
    loaded = Table(
        Col("#", align="right", width=3),
        Col("file", max_width=18),
        Col("job", max_width=20),
        Col("rows", align="right", width=9),
        Col("MB", align="right", width=7),
        Col("sec", align="right", width=7),
        Col("rows/s", align="right", width=9),
        Col("objects created", max_width=60),
        title="loaded files, in dependency order",
    )
    t0 = time.time()
    enc_dir = Path(args.data_out) / "_enc"
    for i, (fs, p) in enumerate(uploads, 1):
        up, n = encode_for_load(p, sep, enc_dir)
        mb = up.stat().st_size / 1e6
        # one line BEFORE each upload: a 45 MB chunk can take a minute and this is the only sign of life.
        step(f"[{i:>2}/{len(uploads)}] {p.name} -> {fs.job}  {_n(n)} rows, {mb:,.1f} MB")
        st = load_file(conn, fs.job, up, sep, n)
        report["files"][p.name] = st
        loaded.add_row(i, p.name, fs.job, _n(n), f"{mb:,.1f}", f"{st['seconds']:.1f}",
                       _n(n / st["seconds"]) if st["seconds"] else "-",
                       joinlist([f"{k}={_n(v['validObject'])}" for k, v in st["objects"].items()], sep=" ", max_items=4))
    report["load_seconds"] = round(time.time() - t0, 1)
    loaded.print()
    ok(f"loaded {len(report['files'])} files ({total_mb:,.1f} MB) in {report['load_seconds']:.1f}s")

    rc = 0
    expected: dict[str, int] = {}
    if not args.skip_counts:
        step("verifying vertex and edge counts")
        expected = expected_counts(plan)
        # RESTPP's edge counts lag the bulk load by tens of seconds (vertex counts do not); a mismatch
        # right after loading is usually that catching up, not a real gap, so settle with backoff before
        # asserting. Real failures still show up: they don't move between retries.
        actual, mism = verify_counts(conn, expected)
        for delay in backoff_delays(90, first=5.0, cap=20.0):
            if not mism:
                break
            detail(f"{len(mism)} count(s) still settling, rechecking in {delay:.0f}s")
            time.sleep(delay)
            actual, mism = verify_counts(conn, expected)
        report.update(expected=expected, actual=actual, mismatches=mism)
        counts_table(expected, actual).print()
        if mism:
            rc = 1
            fail(f"{len(mism)} of {len(expected)} counts do not match")
            for m in mism:
                detail(m)
        else:
            ok(f"all {len(expected)} counts match")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=1))
    summary(
        "load complete" if rc == 0 else "load finished with count mismatches",
        {"files": len(report["files"]), "megabytes": f"{total_mb:,.1f}", "load seconds": f"{report['load_seconds']:.0f}",
         "types asserted": len(expected) if expected else "skipped",
         "count mismatches": len(report.get("mismatches", [])) if not args.skip_counts else "-",
         "manifest warnings": len(mismatches), "report": args.report or "-"},
        status="ok" if rc == 0 else "fail",
    )
    return rc


if __name__ == "__main__":
    sys.exit(main())
