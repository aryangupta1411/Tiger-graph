"""Offline guards for the read-query time-box and bounds (no workspace).

  * episode_candidates.gsql keeps the same chain bounds as engine/facts_duckdb.py (the engine of record), so the live
    chain reaches HHG-018's first suspicious transaction 3485990 (97 rows before the flag; the old 85-row cap cut it);
  * card_profile / case_context print the card's history counts over ts <= as_of, not the all-time Card attributes
    (HHG-014: 73 transactions / max $225.94 at opened_at, not 85 / $252.28);
  * memory queries never return the case's own AgentCase or a later one (strict opened_at < as_of / < to_ts).

The DuckDB checks run the checked-in expected/<query>.sql mirrors on expected/_model.duckdb, built locally by
run_expected.py --rebuild; skipped when it is absent.

    uv run pytest tests/unit/test_graph_queries_asof.py -q
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
QUERIES = ROOT / "graph" / "queries"
EXPECTED = QUERIES / "expected"
MODEL = EXPECTED / "_model.duckdb"


def _code(name: str) -> str:
    """Query text without // comments and /* */ blocks."""
    text = (QUERIES / f"{name}.gsql").read_text()
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in text.splitlines())


# ------------------------------------------------------------------ static
def test_episode_candidates_bounds_match_duckfacts():
    fd = (ROOT / "engine" / "facts_duckdb.py").read_text()
    span = int(re.search(r"^CHAIN_SPAN_DAYS\s*=\s*(\d+)", fd, re.M).group(1))
    rows = int(re.search(r"^CHAIN_MAX_ROWS\s*=\s*(\d+)", fd, re.M).group(1))
    code = _code("episode_candidates")
    assert f"@@t_e - {span} * 86400" in code and f"@@t_e + {span} * 86400" in code
    assert f"IF hi - lo + 1 > {rows} THEN" in code
    assert f"ai - {rows // 2}" in code and f"ai + {rows // 2}" in code
    assert "> 85" not in code and "45 * 86400" not in code


def test_card_profile_history_fields_are_time_boxed():
    code = _code("card_profile")
    row = code[code.index("@@card += CardRow("):]
    row = row[: row.index(");")]
    for loaded in ("s.n_txns", "s.first_ts", "s.last_ts", "s.median_amt", "s.p90_amt", "s.max_amt", "s.max_in_person_amt",
                   "s.n_online", "s.n_in_person", "s.n_regions", "s.n_devices_seen"):
        assert loaded not in row, f"card_profile prints the all-time Card attribute {loaded}"
    assert "@@h_n" in row and "@@h_max" in row and "@@h_med" in row


def test_case_context_card_n_txns_is_time_boxed():
    code = _code("case_context")
    row = code[code.index("@@card += CardRow("):]
    assert "c.n_txns" not in row[: row.index(");")] and "@@card_n" in row


@pytest.mark.parametrize("name", ["similar_prior_cases", "similar_prior_cases_agent", "similar_cases_structural", "card_profile",
                                  "prior_cases_for_customer"])
def test_agent_case_memory_is_strictly_before_as_of(name):
    code = _code(name)
    assert "a.opened_at <= as_of" not in code
    assert "a.opened_at < as_of" in code


def test_device_neighbors_agent_cases_before_to_ts():
    code = _code("device_neighbors")
    block = code[code.index("A = SELECT a FROM (k:C)-[:REV_CASE_ON_CARD]->(a:AgentCase)"):]
    assert "WHERE a.opened_at < to_ts" in block[: block.index(";")]


# ------------------------------------------------------------------ DuckDB mirrors
@pytest.fixture(scope="module")
def con():
    duckdb = pytest.importorskip("duckdb")
    if not MODEL.exists():
        pytest.skip("graph/queries/expected/_model.duckdb absent")
    c = duckdb.connect(str(MODEL), read_only=True)
    yield c
    c.close()


def _run(con, name: str, params: dict) -> dict:
    sql = (EXPECTED / f"{name}.sql").read_text()
    code = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
    used = {k: v for k, v in params.items() if re.search(rf"\${k}\b", code)}
    cur = con.execute(sql, used)
    return dict(zip([d[0] for d in cur.description], cur.fetchone()))


def test_hhg018_chain_reaches_first_suspicious(con):
    out = _run(con, "episode_candidates", {"t": "3491361", "as_of": "2016-11-27 14:41:26", "gap_h": 48})
    ids = [r["id"] for r in out["chain"]]
    assert {"3485990", "3490180", "3491361"} <= set(ids)
    assert len(ids) <= 201


def test_hhg014_card_history_at_opened_at(con):
    out = _run(con, "card_profile", {"c": "C13487-K1", "as_of": "2016-11-22 20:11:00"})
    card = out["card"]
    assert card["n_txns"] == 73 and abs(card["max_amt"] - 225.94) < 1e-6
    assert card["last_ts"] <= "2016-11-22 20:11:00"
    ctx = _run(con, "case_context", {"t": "3478561", "as_of": "2016-11-22 20:11:00"})
    assert ctx["card"]["n_txns"] == 73
