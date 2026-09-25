"""graph/check_contracts.py — offline consistency check between contracts/csv_columns.yaml, graph/schema.gsql
and graph/loading_jobs.gsql. Runs in CI (tests/unit/test_graph_contracts.py) and before every load.

    uv run python graph/check_contracts.py            # exit 1 on any mismatch

Checks
  1. every vertex in csv_columns.yaml `loads:` exists in schema.gsql with attribute count == column count - 1
     (id) and, for the vertex files, the yaml column NAMES equal the schema attribute names in order
     (the only allowed rename is proxy -> proxy_type, a DDL reserved word);
  2. every loading job in loading_jobs.gsql that loads a yaml file references only $0..$(ncols-1), and its
     TO VERTEX VALUES has exactly ncols entries;
  3. the edge guards in loading_jobs.gsql match `edges_from_columns` (same source/target columns, WHERE on
     the same column);
  4. every yaml file has a job, every job uses the same USING clause, no QUOTE clause anywhere;
  5. schema names are not GSQL reserved words (pyTigerGraph 2.0.4 keyword list == gsql-ref 4.2 appendix).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))                       # repo root: these scripts run as plain files, not as a package

from ops.console import Col, Table, detail, fail, header, ok  # noqa: E402

_CANDIDATES = [Path(p) for p in (os.environ.get("HHGOA_CSV_COLUMNS", ""),) if p] + [REPO / "contracts" / "csv_columns.yaml", REPO.parent / "contracts" / "csv_columns.yaml"]
YAML = next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[-1])
SCHEMA = HERE / "schema.gsql"
JOBS = HERE / "loading_jobs.gsql"

RENAMES = {"proxy": "proxy_type"}  # contract attribute -> DDL attribute (PROXY is reserved)
USING = 'USING SEPARATOR="\\t", HEADER="false", EOL="\\n"'

RESERVED = set("""ACCUM ADD ALL ALLOCATE ALTER AND ANY AS ASC AVG BAG BATCH BETWEEN BIGINT BLOB BOOL BOOLEAN BOTH BREAK BY CALL
CASCADE CASE CATCH CHAR CHARACTER CHECK CLOB COALESCE COMPRESS CONST CONSTRAINT CONTINUE COST COUNT CREATE CURRENT_DATE
CURRENT_TIME CURRENT_TIMESTAMP CURSOR KAFKA S3 DATETIME DATETIME_ADD DATETIME_SUB DAY DATETIME_DIFF DATETIME_TO_EPOCH
DATETIME_FORMAT DECIMAL DECLARE DELETE DESC DISTRIBUTED DO DOUBLE DROP EDGE ELSE ELSEIF EPOCH_TO_DATETIME END ESCAPE
EXCEPTION EXISTS FALSE FILE FILTER FIXED_BINARY FLOAT FOR FOREACH FROM GLOBAL GRANTS GRAPH GROUP GROUPBYACCUM HAVING HOUR
HEADER HEAPACCUM IF IGNORE IN INDEX INPUT_LINE_FILTER INSERT INT INTERSECT INT8 INT16 INT32 INT32_T INT64_T INTEGER
INTERPRET INTO IS ISEMPTY JOB JOIN JSONARRAY JSONOBJECT KEY LEADING LIKE LIMIT LIST LOAD LOADACCUM LOG LONG MAP MINUTE
NOBODY NOT NOW NULL OFFSET ON OPENCYPHER OR ORDER PINNED POLICY POST_ACCUM POST-ACCUM PRIMARY PRIMARY_ID PRINT PROXY QUERY
QUIT RAISE RANGE REDUCE REPLACE RESET_COLLECTION_ACCUM RETURN RETURNS ROW SAMPLE SECOND SELECT SELECTVERTEX SET STATIC
STRING SUM TARGET TEMP_TABLE THEN TO TO_CSV TO_DATETIME TRAILING TRANSLATESQL TRIM TRUE TRY TUPLE TYPE TYPEDEF UINT UINT8
UINT16 UINT32 UINT8_T UINT32_T UINT64_T UNION UPDATE UPSERT USING VALUES VERTEX WHEN WHERE WHILE WITH GSQL_SYS_TAG
_INTERNAL_ATTR_TAG""".split())


def _strip_comments(text: str) -> str:
    return "\n".join(line.split("//")[0] for line in text.splitlines())


def parse_schema(text: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """{vertex: [attr names in order]} and {edge: [attr names]} from schema.gsql."""
    text = _strip_comments(text)
    vertices: dict[str, list[str]] = {}
    for m in re.finditer(r"CREATE VERTEX (\w+)\s*\((.*?)\)\s*WITH", text, re.S):
        body = m.group(2)
        names = []
        for part in body.split(","):
            part = part.strip()
            if not part:
                continue
            if part.upper().startswith("PRIMARY_ID"):
                names.append(part.split()[1])
            else:
                names.append(part.split()[0])
        vertices[m.group(1)] = names
    edges: dict[str, list[str]] = {}
    for m in re.finditer(r"CREATE (?:DIRECTED|UNDIRECTED) EDGE (\w+)\s*\((.*?)\)", text, re.S):
        attrs = [p.strip().split()[0] for p in m.group(2).split(",") if p.strip() and not p.strip().upper().startswith(("FROM", "TO"))]
        edges[m.group(1)] = attrs
    return vertices, edges


def parse_jobs(text: str) -> dict[str, dict]:
    """{job: {"vertex": (name, [cols]), "edges": {name: (src, dst, attrs, where)}, "using": [..]}}"""
    text = _strip_comments(text)
    jobs: dict[str, dict] = {}
    for m in re.finditer(r"CREATE LOADING JOB (\w+) FOR GRAPH \w+\s*\{(.*?)\n\}", text, re.S):
        body = m.group(2)
        info: dict = {"vertex": None, "edges": {}, "using": re.findall(r"USING [^;]+", body), "max_col": -1}
        for c in re.findall(r"\$(\d+)", body):
            info["max_col"] = max(info["max_col"], int(c))
        vm = re.search(r"TO VERTEX (\w+) VALUES \((.*?)\)", body, re.S)
        if vm:
            info["vertex"] = (vm.group(1), [c.strip() for c in vm.group(2).split(",")])
        for em in re.finditer(r"TO EDGE (\w+)\s+VALUES \((.*?)\)\s*(WHERE [^,\n]+)?", body, re.S):
            cols = [c.strip() for c in em.group(2).split(",")]
            info["edges"][em.group(1)] = (cols[0], cols[1], cols[2:], (em.group(3) or "").strip())
        jobs[m.group(1)] = info
    return jobs


FILE_TO_JOB = {
    "customer": "load_customer", "card": "load_card", "device_profile": "load_device_profile",
    "email_domain": "load_email_domain", "billing_region": "load_billing_region", "fraud_pattern": "load_fraud_pattern",
    "closed_case": "load_closed_case", "txn_NNN": "load_txn", "owns": "load_owns", "next": "load_next",
    "involves": "load_involves", "on_card": "load_on_card", "connected_to": "load_connected_to", "matches": "load_matches",
    # GraphRAG files (rag/chunk.py): id,doc_id,section,page,kind,text / id,title,url,sha256,kind / chunk_id,doc_id / chunk_id,pattern_id
    "document": "load_document", "policy_chunk": "load_policy_chunk", "chunk_of": "load_chunk_of", "about": "load_about",
}
RAG_COLUMNS = {
    "document": ["id", "title", "url", "sha256", "kind"],
    "policy_chunk": ["id", "doc_id", "section", "page", "kind", "text"],
    "chunk_of": ["chunk_id", "doc_id"],
    "about": ["chunk_id", "pattern_id"],
}


def main() -> int:
    header(
        "graph.check_contracts",
        "offline consistency check: csv_columns.yaml vs schema.gsql vs loading_jobs.gsql",
        {"contracts": YAML, "schema": SCHEMA.name, "jobs": JOBS.name},
    )
    spec = yaml.safe_load(YAML.read_text())
    vertices, edges = parse_schema(SCHEMA.read_text())
    jobs = parse_jobs(JOBS.read_text())
    errors: list[str] = []

    # 5. reserved words
    for name in list(vertices) + list(edges) + [a for v in vertices.values() for a in v] + [a for e in edges.values() for a in e]:
        if name.upper() in RESERVED:
            errors.append(f"reserved word used as a name: {name}")

    files = {Path(k).stem: v for k, v in spec["files"].items()}
    for stem, cols in RAG_COLUMNS.items():
        files[stem] = {"loads": [], "columns": [{"name": c} for c in cols]}

    for stem, f in files.items():
        cols = [c["name"] for c in f["columns"]]
        job = FILE_TO_JOB.get(stem)
        if job is None or job not in jobs:
            errors.append(f"{stem}: no loading job ({job})")
            continue
        j = jobs[job]
        if j["max_col"] != len(cols) - 1:
            errors.append(f"{job}: references $0..${j['max_col']} but {stem} has {len(cols)} columns")
        for u in j["using"]:
            if u.strip() != USING:
                errors.append(f"{job}: USING clause '{u.strip()}' != '{USING}'")
            if "QUOTE" in u.upper():
                errors.append(f"{job}: QUOTE clause is not allowed (TSV, no escape character)")
        loads = f.get("loads") or []
        vtype = next((v for v in loads if v in vertices), None)
        if stem in RAG_COLUMNS and stem in ("document", "policy_chunk"):
            vtype = {"document": "Document", "policy_chunk": "PolicyChunk"}[stem]
        if vtype:
            if not j["vertex"] or j["vertex"][0] != vtype:
                errors.append(f"{job}: expected TO VERTEX {vtype}")
            else:
                vals = j["vertex"][1]
                if vals != [f"${i}" for i in range(len(cols))]:
                    errors.append(f"{job}: VALUES must be $0..${len(cols)-1} in order, got {vals}")
                schema_attrs = vertices[vtype]
                want = [RENAMES.get(c, c) for c in cols]
                if schema_attrs != want:
                    errors.append(f"{vtype}: schema attrs {schema_attrs} != csv columns {want}")
        # 3. edge guards
        for ename, rule in (f.get("edges_from_columns") or {}).items():
            if ename not in j["edges"]:
                errors.append(f"{job}: missing TO EDGE {ename}")
                continue
            src, dst, attrs, where = j["edges"][ename]
            m = re.match(r'FROM \$(\w+) TO \$(\w+)(?: VALUES\(([^)]*)\))?(?: WHERE \$(\w+) != "")?', rule)
            if not m:
                errors.append(f"{stem}: cannot parse edge rule {rule}")
                continue
            want_src, want_dst, want_attrs, want_where = m.group(1), m.group(2), m.group(3), m.group(4)
            idx = {c: i for i, c in enumerate(cols)}
            if src != f"${idx[want_src]}" or dst != f"${idx[want_dst]}":
                errors.append(f"{job}.{ename}: FROM/TO {src},{dst} != ${idx[want_src]},${idx[want_dst]}")
            if want_attrs:
                want_a = [f"${idx[a.strip().lstrip('$')]}" for a in want_attrs.split(",")]
                if attrs != want_a:
                    errors.append(f"{job}.{ename}: attrs {attrs} != {want_a}")
                if len(attrs) != len(edges.get(ename, [])):
                    errors.append(f"{ename}: {len(attrs)} attrs loaded, schema has {len(edges.get(ename, []))}")
            if want_where:
                if f"gsql_is_not_empty_string(${idx[want_where]})" not in where:
                    errors.append(f"{job}.{ename}: WHERE guard should test ${idx[want_where]} ({want_where}), got '{where}'")
            elif where:
                errors.append(f"{job}.{ename}: unexpected WHERE {where}")
        # edge-only files: two id columns (+ attrs) must match the schema edge
        if not vtype and not f.get("edges_from_columns"):
            ename = (loads or [None])[0] or {"chunk_of": "CHUNK_OF", "about": "ABOUT"}[stem]
            if ename not in j["edges"]:
                errors.append(f"{job}: expected TO EDGE {ename}")
            else:
                src, dst, attrs, where = j["edges"][ename]
                if (src, dst) != ("$0", "$1") or attrs != [f"${i}" for i in range(2, len(cols))] or where:
                    errors.append(f"{job}.{ename}: VALUES should be $0,$1{',' if len(cols) > 2 else ''}{','.join(f'${i}' for i in range(2, len(cols)))}")
                if len(attrs) != len(edges.get(ename, [])):
                    errors.append(f"{ename}: {len(attrs)} attrs loaded, schema has {len(edges.get(ename, []))}")

    # every schema vertex that the ETL/RAG files load has a job; the run-time vertices do not
    runtime_only = {"AgentCase", "CaseEvent", "Approval"}
    loaded = {j["vertex"][0] for j in jobs.values() if j["vertex"]}
    for v in vertices:
        if v not in loaded and v not in runtime_only:
            errors.append(f"vertex {v} has no loading job")

    t = Table(
        Col("artefact", max_width=22),
        Col("count", align="right", width=6),
        Col("source", max_width=30),
        Col("checked", max_width=56),
        title="contract surfaces",
    )
    t.add_row("vertex types", len(vertices), SCHEMA.name, "names, attribute order, reserved words")
    t.add_row("edge types", len(edges), SCHEMA.name, "names, attribute count, reserved words")
    t.add_row("loading jobs", len(jobs), JOBS.name, "$0..$n, TO VERTEX/EDGE values, USING, no QUOTE")
    t.add_row("csv files", len(files), YAML.name, "column names == schema attributes, edge guards")
    t.print()

    if errors:
        fail(f"{len(errors)} mismatch(es) between the contracts")
        for e in errors:
            detail(e)
        return 1
    # The trailing token is a contract: tests/unit/test_graph_contracts.py asserts stdout ends with "OK".
    ok("schema, loading jobs and csv_columns.yaml all agree - OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
