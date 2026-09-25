"""ui/common.py + ops/make_mock_run.py: run loading, derived views, SAR badges, mock subgraph,
and the Streamlit pages rendering without exceptions (streamlit.testing AppTest)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ops import make_mock_run
from ui import common

FIX_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "answers"


@pytest.fixture
def mock_run(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    written = make_mock_run.build(str(FIX_DIR), "t1", str(runs), str(common.ROOT / "data" / "raw" / "case_pack.csv"))
    assert written == ["HHG-014"]
    monkeypatch.setenv("RUNS_DIR", str(runs))
    monkeypatch.setenv("RUN_MODE", "mock")
    monkeypatch.setenv("APPROVALS_DB", str(tmp_path / "approvals.sqlite"))
    return runs


def test_case_pack_embedded_matches_readme():
    pack = common.load_case_pack()
    assert len(pack) == 20
    assert pack[0]["case_id"] == "HHG-017" and pack[-1]["case_id"] == "HHG-004"  # opened_at order
    m = common.case_meta("HHG-014")
    assert m["card_id"] == "C13487-K1" and m["flagged_txn_id"] == "3478561" and m["trigger_type"] == "analyst_request"


def test_run_loading_and_derived_views(mock_run):
    assert common.list_runs() == ["t1"]
    assert common.list_cases_in_run("t1") == ["HHG-014"]
    run = common.load_case_run("t1", "HHG-014")
    assert run.ok and len(run.phases) == 11 and len(run.calls) >= 9
    pre, post = common.probability_pre_post(run)
    assert pre == 0.9 and post == 0.9
    ff, fl = common.families(run)
    assert set(ff) >= {"device", "memory", "history"} and fl == []
    assert common.counterfactual(run) == ""  # nothing asked
    stats = common.tool_stats(run)
    assert stats["tool_calls"] == 9 and stats["llm_calls"] == 6 and stats["tokens"] > 0
    assert "Suspicious Activity Report" in run.sar_md


def test_rule_chips_and_actions_diff():
    assert common.rule_chips("R2 and R5: denied; 3a holds; policy 6 test 1") == ["R2", "R5", "3a", "§6"]
    assert common.rule_chips("R10 applies, r1 does not") == ["R10", "R1"]
    added, removed, kept = common.actions_diff([{"action": "VERIFY_WITH_CUSTOMER"}, {"action": "CREATE_CASE"}], [{"action": "CREATE_CASE"}, {"action": "BLOCK_CARD"}])
    assert (added, removed, kept) == (["BLOCK_CARD"], ["VERIFY_WITH_CUSTOMER"], ["CREATE_CASE"])


def test_sar_checks_on_sample():
    """sar_checks delegates to rag.validate_sar (D10): one badge per code, ids resolved against the answer's own lists."""
    ans = json.loads((FIX_DIR / "HHG-014.sample.json").read_text())
    checks = common.sar_checks(ans, common.case_meta("HHG-014"))
    failed = [(n, d) for n, ok, d in checks if not ok]
    assert failed == [], failed
    assert len(checks) == len(common.SAR_CHECK_LABELS)
    neg = dict(ans, sar={"file": False, "reason": "3a not met", "narrative": "", "subjects": [], "total_amount_usd": 0, "activity_dates": []})
    neg["next_best_actions"] = {"initial": [], "final": [{"action": "CLOSE_NO_FRAUD", "route": "auto", "reason": "R3"}], "what_changed": "nothing"}
    assert all(ok for _, ok, _ in common.sar_checks(neg))
    bad = dict(ans, sar=dict(ans["sar"], total_amount_usd=1.0))
    assert any(n.startswith("total_amount_usd") and not ok for n, ok, _ in common.sar_checks(bad))


def test_subgraph_from_answer():
    ans = json.loads((FIX_DIR / "HHG-014.sample.json").read_text())
    sub = common.subgraph_from_answer(ans, common.case_meta("HHG-014"))
    types = {common.vtype(n) for n in sub["nodes"]}
    assert {"Customer", "Card", "Transaction", "DeviceProfile", "ClosedCase", "AgentCase"} <= types
    assert all("vtype" in n for n in sub["nodes"]) and all("etype" in e for e in sub["edges"])  # case_subgraph keys
    assert common.vtype({"type": "Card"}) == "Card" and common.etype({"type": "MADE"}) == "MADE"   # fallback
    assert sum(1 for n in sub["nodes"] if common.vtype(n) == "Card") == 28  # the card + 27 connected
    assert sum(1 for e in sub["edges"] if common.etype(e) == "SHARES_DEVICE") == 27
    ids = {n["id"] for n in sub["nodes"]}
    assert all(e["src"] in ids and e["dst"] in ids for e in sub["edges"])


PAGES = ["ui/app.py", "ui/pages/1_queue.py", "ui/pages/2_case.py", "ui/pages/3_approvals.py", "ui/pages/4_graph.py", "ui/pages/5_runs.py"]


@pytest.mark.parametrize("page", PAGES)
def test_pages_render_without_exceptions(mock_run, page):
    pytest.importorskip("streamlit", reason="AppTest needs streamlit (a project dependency; CI installs it)")
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(common.ROOT / page), default_timeout=60)
    at.session_state["run_id"] = "t1"
    at.session_state["case_id"] = "HHG-014"
    at.run()
    assert not at.exception, [e.value for e in at.exception]
