"""Module B offline tests (no workspace): contracts, the loader's plan / encoding / expected counts / statistics
checks, an end-to-end load_all.main() against a fake pyTigerGraph connection that behaves like RESTPP /ddl, the
day-0 smoke file staying in sync with schema.gsql, and the rebuild stage order.

    uv run pytest tests/unit/test_graph_contracts.py -q
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "graph"))

import check_contracts  # noqa: E402
import load_all  # noqa: E402
import rebuild  # noqa: E402
from check_contracts import parse_jobs, parse_schema  # noqa: E402

SCHEMA = (ROOT / "graph" / "schema.gsql").read_text()
JOBS = (ROOT / "graph" / "loading_jobs.gsql").read_text()
DAY0 = (ROOT / "graph" / "day0_smoke.gsql").read_text()


# ------------------------------------------------------------------------------ contracts ---------------------
def test_check_contracts_passes(capsys):
    assert check_contracts.main() == 0
    assert capsys.readouterr().out.strip().endswith("OK")


def test_every_schema_name_in_day0_smoke():
    vertices, edges = parse_schema(SCHEMA)
    for v in vertices:
        assert re.search(rf"CREATE VERTEX {v} \(", DAY0), v
    for e in edges:
        assert re.search(rf"CREATE (DIRECTED|UNDIRECTED) EDGE {e} \(", DAY0), e
    assert "CREATE GRAPH SmokeGraph (*)" in DAY0 and "FraudGraph" not in DAY0.split("// ==== STEP probe_reserved_proxy")[0].split("// ==== STEP ddl_all_names")[1]
    steps = re.findall(r"// ==== STEP (\w+)", DAY0)
    assert steps == ["ddl_all_names", "probe_reserved_proxy", "vectors_global_job", "two_type_vector_query", "loading_jobs",
                     "gdbms_algo_import", "gdbms_algo_call", "drop_all"]
    assert "vectorSearch({ClosedCase.note_emb, AgentCase.note_emb}" in DAY0
    assert "IMPORT PACKAGE GDBMS_ALGO" in DAY0


def test_rebuild_stage_order():
    assert rebuild.STAGES == ["schema", "load", "vectors", "algos", "install", "describe"]
    assert rebuild.STAGES.index("load") < rebuild.STAGES.index("vectors") < rebuild.STAGES.index("install")


def test_schema_change_text_filters_missing_only():
    text = rebuild.schema_change_text({"AgentCase": "note_emb"})
    assert "ALTER VERTEX AgentCase" in text and "ALTER VERTEX ClosedCase" not in text and "ALTER VERTEX PolicyChunk" not in text
    assert "RUN GLOBAL SCHEMA_CHANGE JOB add_vectors" in text


# ------------------------------------------------------------------------------ fixture data/out --------------
def _val(col: dict, i: int) -> str:
    t, name = col.get("type", "STRING"), col["name"]
    if t == "INT":
        return str(i)
    if t == "FLOAT":
        return f"{i}.50"
    if t == "BOOL":
        return "true" if i % 2 else "false"
    if t == "DATETIME":
        return f"2016-11-22 20:1{i % 10}:00"
    if name in ("home_regions", "c_counts", "d_deltas", "m_flags", "known_device_ids", "recurring_amounts", "region_cluster_30d", "usual_products", "burst_lookalike_ids"):
        return '{"k":"v,with comma","q":"it\'s \\"q\\""}'
    return f"{name}_{i}"


@pytest.fixture
def data_out(tmp_path: Path) -> Path:
    spec = load_all.load_spec()
    cols = {Path(k).stem.replace("_NNN", ""): [c for c in v["columns"]] for k, v in spec["files"].items()}
    out = tmp_path / "out"
    out.mkdir()
    rows: dict[str, list[list[str]]] = {}
    ids = {"customer": ["C00001", "C00002"], "card": ["C00001-K1", "C00002-K1"], "device_profile": ["SM-X | Android | chrome 62.0 | 1920x1080"],
           "email_domain": ["gmail.com", "hotmail.com"], "billing_region": ["444.0"], "fraud_pattern": ["card_testing", "none"],
           "closed_case": ["CC-0001"]}
    for stem, idlist in ids.items():
        rows[stem] = [[idlist[i] if c["name"] == "id" else _val(c, i) for c in cols[stem]] for i in range(len(idlist))]
    # transactions: 4 rows; foreign keys blank in some rows so the guarded edges differ from the row count
    txn = []
    fk = [("SM-X | Android | chrome 62.0 | 1920x1080", "gmail.com", "hotmail.com", "444.0"), ("", "gmail.com", "", "444.0"),
          ("SM-X | Android | chrome 62.0 | 1920x1080", "", "", ""), ("SM-X | Android | chrome 62.0 | 1920x1080", "hotmail.com", "gmail.com", "444.0")]
    for i in range(4):
        r = []
        for c in cols["txn"]:
            n = c["name"]
            if n == "id":
                r.append(str(3000001 + i))
            elif n == "card_id":
                r.append("C00001-K1" if i < 3 else "C00002-K1")
            elif n == "customer_id":
                r.append("C00001" if i < 3 else "C00002")
            elif n in ("device_id", "p_email", "r_email", "addr1"):
                r.append(fk[i][("device_id", "p_email", "r_email", "addr1").index(n)])
            else:
                r.append(_val(c, i))
        txn.append(r)
    rows["txn"] = txn
    rows["owns"] = [["C00001", "C00001-K1"], ["C00002", "C00002-K1"]]
    rows["next"] = [["3000001", "3000002", "60"], ["3000002", "3000003", "120"], ["3000002", "3000003", "120"]]   # duplicate pair
    rows["involves"] = [["CC-0001", "3000001"], ["CC-0001", "3000002"]]
    rows["on_card"] = [["CC-0001", "C00001-K1"]]
    rows["connected_to"] = [["CC-0001", "C00002-K1"]]
    rows["matches"] = [["CC-0001", "card_testing"]]
    manifest = {}
    for stem, rs in rows.items():
        if stem == "txn":
            for k, part in enumerate((rs[:2], rs[2:])):
                p = out / f"txn_{k:03d}.csv"
                p.write_text("".join("\t".join(r) + "\n" for r in part), encoding="utf-8")
                manifest[p.name] = {"rows": len(part), "bytes": p.stat().st_size, "columns": len(cols["txn"])}
        else:
            p = out / f"{stem}.csv"
            p.write_text("".join("\t".join(r) + "\n" for r in rs), encoding="utf-8")
            manifest[p.name] = {"rows": len(rs), "bytes": p.stat().st_size, "columns": len(cols[stem])}
    (out / "manifest.json").write_text(json.dumps(manifest))
    # rag/chunk.py output: `,`-separated, `"`-quoted (csv.QUOTE_ALL); fraud_pattern.csv overwritten in that format too
    rag = {"document": [["fraud_policy", "HHGOA Fraud Policy v1.0", "https://x/y", "abc", "policy"]],
           "policy_chunk": [["policy#R1", "fraud_policy", "R1", "0", "policy", 'Rule "R1": verify, don\'t block; a,b'],
                            ["policy#R2", "fraud_policy", "R2", "0", "policy", "Rule R2"]],
           "chunk_of": [["policy#R1", "fraud_policy"], ["policy#R2", "fraud_policy"]],
           "about": [["policy#R1", "card_testing"], ["policy#R2", "none"]],
           "fraud_pattern": [["card_testing", "three tiny auths, then a \"big\" one", "R5"], ["none", "cleared, it's fine", "R3"]]}
    for stem, rs in rag.items():
        with (out / f"{stem}.csv").open("w", newline="") as fh:
            w = csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator="\n")
            w.writerows(rs)
    return out


EXPECTED = {"Customer": 2, "Card": 2, "DeviceProfile": 1, "EmailDomain": 2, "BillingRegion": 1, "FraudPattern": 2, "ClosedCase": 1,
            "Document": 1, "PolicyChunk": 2, "Transaction": 4, "MADE": 4, "FROM_DEVICE": 3, "PURCHASER_EMAIL": 3, "RECIPIENT_EMAIL": 2,
            "BILLED_IN": 3, "OWNS": 2, "NEXT": 2, "INVOLVES": 2, "ON_CARD": 1, "CONNECTED_TO": 1, "MATCHES": 1, "CHUNK_OF": 2, "ABOUT": 2}


def test_plan_counts_and_manifest(data_out):
    plan = load_all.check_paths(load_all.file_plan(load_all.load_spec(), data_out))
    assert [fs.stem for fs in plan] == load_all.ORDER
    assert [p.name for p in next(fs for fs in plan if fs.stem == "txn").paths] == ["txn_000.csv", "txn_001.csv"]
    assert load_all.expected_counts(plan) == EXPECTED
    assert load_all.manifest_mismatches(plan, data_out) == []


def test_missing_rag_files_are_skipped_but_etl_files_required(data_out):
    (data_out / "about.csv").unlink()
    plan = load_all.check_paths(load_all.file_plan(load_all.load_spec(), data_out))
    assert "about" not in [fs.stem for fs in plan]
    (data_out / "next.csv").unlink()
    with pytest.raises(SystemExit):
        load_all.check_paths(load_all.file_plan(load_all.load_spec(), data_out))


def test_encode_quoted_csv_to_tsv_is_lossless(data_out, tmp_path):
    src = data_out / "policy_chunk.csv"
    assert load_all.sniff_quoted_csv(src) and not load_all.sniff_quoted_csv(data_out / "customer.csv")
    up, n = load_all.encode_for_load(src, "\t", tmp_path / "enc")
    assert n == 2 and up != src
    lines = up.read_text(encoding="utf-8").split("\n")
    assert lines[0].split("\t")[5] == 'Rule "R1": verify, don\'t block; a,b'
    same, n2 = load_all.encode_for_load(data_out / "customer.csv", "\t", tmp_path / "enc")
    assert same == data_out / "customer.csv" and n2 == 2
    alt, _ = load_all.encode_for_load(data_out / "customer.csv", "|#|", tmp_path / "enc2")
    assert alt.read_text().startswith("C00001|#|")
    assert 'SEPARATOR="|#|"' in load_all.jobs_text("|#|") and 'SEPARATOR="\\t"' not in load_all.jobs_text("|#|")
    assert len(load_all.job_names(load_all.jobs_text())) == 18


def test_load_result_stats_and_checks():
    res = [{"sourceFileName": "Online_POST", "statistics": {
        "validLine": 3, "rejectLine": 0, "failedConditionLine": 2, "notEnoughToken": 0, "invalidJson": 0, "oversizeToken": 0,
        "vertex": [{"typeName": "Transaction", "validObject": 3, "noIdFound": 0, "invalidAttribute": 0, "invalidPrimaryId": 0}],
        "edge": [{"typeName": "MADE", "validObject": 3, "noIdFound": 0, "invalidAttribute": 0, "invalidPrimaryId": 0},
                 {"typeName": "FROM_DEVICE", "validObject": 1, "noIdFound": 0, "invalidAttribute": 0, "invalidPrimaryId": 0}]}}]
    st = load_all.load_result_stats(res)
    assert st["validLine"] == 3 and st["failedConditionLine"] == 2 and st["objects"]["FROM_DEVICE"]["validObject"] == 1
    assert load_all.check_stats(st, 3) == []
    assert load_all.check_stats(st, 4) == ["validLine 3 != 4 lines in the file"]
    res[0]["statistics"]["rejectLine"] = 1
    res[0]["statistics"]["vertex"][0]["invalidAttribute"] = 2
    st = load_all.load_result_stats(res)
    assert load_all.check_stats(st, 3) == ["rejectLine=1", "Transaction.invalidAttribute=2"]


# ------------------------------------------------------------------------------ fake RESTPP ------------------
class FakeConn:
    """Enough of pyTigerGraph to load: /gsql statements, POST /ddl parsing by the job's VALUES/WHERE, /builtins counts.
    Edge loads auto-create endpoint vertices (VERTEX_MUST_EXIST default false), like the real loader."""

    def __init__(self):
        self.graph = False
        self.vertices: dict[str, set] = defaultdict(set)
        self.edges: dict[str, set] = defaultdict(set)
        self.jobs: dict[str, dict] = {}
        self.endpoints = {m.group(2): (m.group(3), m.group(4)) for m in re.finditer(r"CREATE (DIRECTED|UNDIRECTED) EDGE (\w+) \(FROM (\w+), TO (\w+)", SCHEMA)}
        self.log: list[str] = []
        self.seps: set[str] = set()

    def getVertexTypes(self, force=False):
        if not self.graph:
            raise Exception("Graph 'FraudGraph' does not exist.")
        return list(parse_schema(SCHEMA)[0])

    def gsql(self, text):
        self.log.append(text)
        if "DROP ALL" in text:
            self.__init__()
            return "Everything is dropped."
        if "CREATE GRAPH" in text:
            self.graph = True
            return "Successfully created vertex types: [Customer].\nThe graph FraudGraph is created."
        if "DROP JOB" in text:
            job = text.split("DROP JOB ")[1].strip()
            return f"Successfully dropped jobs on the graph 'FraudGraph': [{job}]." if self.jobs.pop(job, None) else f"Semantic Check Fails: The job {job} does not exist!"
        if "CREATE LOADING JOB" in text:
            self.jobs.update(parse_jobs(text))
            return f"Successfully created loading jobs: [{', '.join(load_all.job_names(text))}]."
        return "ok"

    def runLoadingJobWithFile(self, path, tag, job, sep=None, eol=None, timeout=0, sizeLimit=0):
        assert tag == "f" and timeout == 600000 and sizeLimit == 128000000 and eol == "\n"
        self.seps.add(sep)
        info = self.jobs[job]
        lines = Path(path).read_text(encoding="utf-8").split("\n")
        lines = lines[:-1] if lines and lines[-1] == "" else lines
        vst: dict[str, int] = defaultdict(int)
        est: dict[str, int] = defaultdict(int)
        for line in lines:
            f = line.split(sep)
            if info["vertex"]:
                vt, cols = info["vertex"]
                assert len(f) == len(cols), (job, len(f), len(cols))
                self.vertices[vt].add(f[int(cols[0][1:])])
                vst[vt] += 1
            for ename, (src, dst, _attrs, where) in info["edges"].items():
                m = re.search(r"gsql_is_not_empty_string\(\$(\d+)\)", where)
                if m and f[int(m.group(1))] == "":
                    continue
                s, d = f[int(src[1:])], f[int(dst[1:])]
                self.edges[ename].add((s, d))
                st, dt = self.endpoints[ename]
                self.vertices[st].add(s)
                self.vertices[dt].add(d)
                est[ename] += 1
        stats = {"validLine": len(lines), "rejectLine": 0, "failedConditionLine": 0, "notEnoughToken": 0, "invalidJson": 0, "oversizeToken": 0,
                 "vertex": [{"typeName": k, "validObject": v} for k, v in vst.items()], "edge": [{"typeName": k, "validObject": v} for k, v in est.items()]}
        return [{"sourceFileName": "Online_POST", "statistics": stats}]

    def getVertexCount(self, vt, where="", realtime=False):
        return len(self.vertices[vt])

    def getEdgeCount(self, et):
        return {et: len(self.edges[et])}


def test_load_all_end_to_end_against_fake_restpp(data_out, tmp_path, monkeypatch):
    fake = FakeConn()
    rep = tmp_path / "report.json"
    rc = load_all.main(["--data-out", str(data_out), "--report", str(rep)], connect_fn=lambda **kw: fake)
    assert rc == 0
    assert fake.graph and len(fake.jobs) == 18 and fake.seps == {"\t"}
    report = json.loads(rep.read_text())
    assert report["mismatches"] == [] and report["actual"] == EXPECTED
    assert len(report["files"]) == 19    # 9 vertex + 2 txn chunks + 8 edge files
    assert sum(1 for t in fake.log if "DROP JOB" in t) == 18
    # second run: schema skipped (graph exists), jobs dropped and re-created, counts unchanged
    rc = load_all.main(["--data-out", str(data_out)], connect_fn=lambda **kw: fake)
    assert rc == 0 and sum(1 for t in fake.log if "CREATE GRAPH" in t) == 1


def test_load_all_separator_fallback(data_out, tmp_path):
    fake = FakeConn()
    rc = load_all.main(["--data-out", str(data_out), "--sep", "|#|"], connect_fn=lambda **kw: fake)
    assert rc == 0 and fake.seps == {"|#|"}
    assert any('SEPARATOR="|#|"' in t for t in fake.log)
    assert (data_out / "_enc" / "txn_000.csv").exists() and (data_out / "_enc" / "policy_chunk.csv").exists()


def test_load_all_subset_and_count_mismatch(data_out, capsys):
    fake = FakeConn()
    assert load_all.main(["--data-out", str(data_out), "--only", "customer", "card"], connect_fn=lambda **kw: fake) == 0
    assert fake.getVertexCount("Customer") == 2 and fake.getVertexCount("Transaction") == 0
    # a mismatch is an exit code 1, not a silent warning
    fake.vertices["Card"].add("C99999-K9")
    assert load_all.main(["--data-out", str(data_out), "--only", "card"], connect_fn=lambda **kw: fake) == 1


def test_expected_flag_is_offline(data_out, capsys):
    assert load_all.main(["--data-out", str(data_out), "--expected"], connect_fn=None) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["expected"] == EXPECTED and out["manifest_mismatches"] == []


def test_loading_jobs_use_yaml_format():
    spec = yaml.safe_load((ROOT / "contracts" / "csv_columns.yaml").read_text())
    assert spec["format"]["separator"] == "\t" and spec["format"]["quote"] == "none" and spec["format"]["header"] is False
    code = check_contracts._strip_comments(JOBS)                      # the header comment says "NO QUOTE clause"
    assert code.count('USING SEPARATOR="\\t", HEADER="false", EOL="\\n"') == 18 and "QUOTE" not in code


# ------------------------------------------------------------------------------ query library (fix pass) --------
QUERIES = ROOT / "graph" / "queries"
RAG_QUERIES = {"similar_prior_cases", "similar_prior_cases_closed", "similar_prior_cases_agent", "similar_cases_structural", "grounding_chunks"}
# Harness-only in the yaml = the RAG set plus the two reads the model must never hold as a tool:
# post_open_activity (monitoring note, must not reach exposure) and case_subgraph (UI rendering).
HARNESS_ONLY = RAG_QUERIES | {"post_open_activity", "case_subgraph"}
WRITERS = {"open_case", "append_case_event", "close_case", "record_approval"}


def test_query_descriptions_yaml_is_the_single_registry():
    """M19 / D11: one yaml, 25 entries = 14 typed model tools + 4 writers + 7 harness-only; no fragment."""
    y = yaml.safe_load((ROOT / "mcp" / "query_descriptions.yaml").read_text())["queries"]
    files = {p.stem for p in QUERIES.glob("*.gsql")} - {"install_all"}
    assert set(y) == files and len(y) == 25
    assert not (ROOT / "mcp" / "query_descriptions_rag.yaml").exists()
    assert {q for q, s in y.items() if s.get("harness_only")} == HARNESS_ONLY
    assert len([q for q, s in y.items() if not s.get("harness_only") and not s.get("writer")]) == 14
    assert {q for q, s in y.items() if s.get("writer")} == WRITERS
    for q, spec in y.items():
        assert list(spec["parameters"]) == list(spec["example"]), q
        assert spec.get("prints"), q
    assert y["ring_profile"]["prints"] == ["ring_id", "device", "wave_cards", "pre_open_cards", "n_cards_alltime", "card_txns_on_ring", "closed_cases"]
    assert "event_at" in y["append_case_event"]["parameters"]


def test_agent_loader_sees_rag_queries_as_harness_only():
    """agent.mcp_client.load_query_descriptions merges the yaml over its built-in table; every RAG query must stay out of the LLM tool set."""
    pytest.importorskip("anthropic")
    sys.path.insert(0, str(ROOT))
    from agent.mcp_client import load_query_descriptions

    d = load_query_descriptions(ROOT / "mcp" / "query_descriptions.yaml")
    assert RAG_QUERIES <= set(d) and all(d[q]["harness_only"] for q in RAG_QUERIES)
    llm = {q for q, s in d.items() if not s.get("harness_only") and not s.get("writer")}
    assert not (llm & HARNESS_ONLY) and "case_context" in llm and "ring_profile" in llm and len(llm) == 14


def test_gsql_lint_passes():
    """Every alias.attribute in graph/queries resolves against schema.gsql; no `.proxy` (stored as proxy_type, M1)."""
    sys.path.insert(0, str(ROOT))
    from qa import lint_gsql

    assert lint_gsql.main() == 0
    for f in ("card_window", "case_context", "device_neighbors"):
        text = (QUERIES / f"{f}.gsql").read_text()
        assert not re.search(r"\b[a-z]\.proxy\b", text) and "proxy_type" in text          # attribute renamed ...
    for f in ("card_window", "case_context"):
        assert "STRING proxy," in (QUERIES / f"{f}.gsql").read_text()                    # ... printed key kept
    # the lint really catches the regression: a `.proxy` reference is a problem
    import os
    import tempfile
    bad = (QUERIES / "device_neighbors.gsql").read_text().replace("t.proxy_type", "t.proxy")
    with tempfile.NamedTemporaryFile("w", suffix=".gsql", delete=False) as fh:
        fh.write(bad)
    try:
        from check_contracts import parse_schema as _ps
        v, e = _ps(SCHEMA)
        probs = lint_gsql.lint_file(fh.name, v, e)
    finally:
        os.unlink(fh.name)
    assert len(probs) == 1 and "t.proxy is not an attribute" in probs[0] and "proxy_type" in probs[0]


def test_ring_profile_prints_wave_rows_and_closed_case_rows():
    """D6 / M6: wave = +-30 d of the card's own ring transactions, n_cards_alltime printed, closed_cases as tuple rows."""
    text = (QUERIES / "ring_profile.gsql").read_text()
    assert "TYPEDEF TUPLE<STRING id, STRING card_id, STRING pattern, STRING outcome, BOOL report_filed> ClosedRow" in text
    assert "SetAccum<ClosedRow> @@closed_cases" in text and "ClosedRow(p.id, p.card_id, p.pattern, p.outcome, p.report_filed)" in text
    assert "30 * 86400" in text and "@@n_cards_alltime AS n_cards_alltime" in text
    exp = json.loads((QUERIES / "expected" / "ring_profile__HHG-014.json").read_text())["expected"]
    assert (len(exp["wave_cards"]), len(exp["pre_open_cards"]), exp["n_cards_alltime"]) == (27, 19, 52)
    assert [c["id"] for c in exp["closed_cases"]] == ["CC-2649", "CC-2971", "CC-2985", "CC-3035"]
    assert set(exp["closed_cases"][0]) == {"id", "card_id", "pattern", "outcome", "report_filed"}
    assert set(exp["pre_open_cards"]) <= set(exp["wave_cards"])


def test_closed_case_rows_print_template_id():
    """D8: card_profile.prior_closed_cases[] and prior_cases_for_customer.closed_cases[] carry template_id."""
    for f, key in (("card_profile", "prior_closed_cases"), ("prior_cases_for_customer", "closed_cases")):
        assert "STRING template_id> ClosedRow" in (QUERIES / f"{f}.gsql").read_text()
        for exp_file in QUERIES.glob(f"expected/{f}__*.json"):
            rows = json.loads(exp_file.read_text())["expected"][key]
            assert rows and all("template_id" in r for r in rows), exp_file.name
    assert "sig_match" in (QUERIES / "episode_candidates.gsql").read_text() and "same_region" not in (QUERIES / "episode_candidates.gsql").read_text()
