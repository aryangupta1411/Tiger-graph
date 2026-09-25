"""ui/approvals_db.py: schema, idempotent enqueue, decisions, graph ids, writer params."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ui import approvals_db as adb

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "answers" / "HHG-014.sample.json"


@pytest.fixture
def db(tmp_path):
    return tmp_path / "approvals.sqlite"


def test_schema_columns(db):
    adb.init_db(db)
    cols = [r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(approvals)")]
    assert cols == list(adb.COLUMNS)


def test_enqueue_idempotent_and_route_check(db):
    a = adb.enqueue("HHG-014", "FILE_REPORT", "L2", "3a", db)
    b = adb.enqueue("HHG-014", "FILE_REPORT", "L2", "3a again", db)
    assert a == b
    assert [r["status"] for r in adb.rows(path=db)] == ["pending"]
    with pytest.raises(ValueError):
        adb.enqueue("HHG-014", "CREATE_CASE", "auto", "", db)


def test_enqueue_from_sample_answer(db):
    ans = json.loads(FIX.read_text())
    ids = adb.enqueue_from_answer(ans, db)
    rows = adb.rows("pending", db)
    assert len(ids) == 2
    assert {(r["action"], r["route"]) for r in rows} == {("BLOCK_CARD", "L1"), ("FILE_REPORT", "L2")}


def test_decide_and_reopen(db):
    i = adb.enqueue("HHG-006", "BLOCK_CARD", "L1", "R2", db)
    row = adb.decide(i, "approved", "lead@bank", db, now="2026-09-20 10:00:00")
    assert row["status"] == "approved" and row["decided_by"] == "lead@bank" and row["decided_at"] == "2026-09-20 10:00:00"
    assert adb.rows("pending", db) == []
    adb.reopen(i, db)
    assert adb.rows("pending", db)[0]["id"] == i
    with pytest.raises(ValueError):
        adb.decide(i, "maybe", "x", db)


def test_graph_ids():
    assert adb.graph_case_id("HHG-014") == "AC-HHG-014"
    assert adb.graph_case_id("AC-HHG-014") == "AC-HHG-014"
    assert adb.approval_vertex_id("HHG-014", "FILE_REPORT") == "AP-AC-HHG-014-FILE_REPORT"


def test_write_to_graph_calls_writers_with_contract_params(db, monkeypatch):
    i = adb.enqueue("HHG-014", "FILE_REPORT", "L2", "3a and R9", db)
    row = adb.decide(i, "approved", "manager@bank", db, now="2026-09-20 10:00:00")
    calls = []

    def fake_run_query(conn, name, params, timeout_ms=None):
        calls.append((name, params))
        return [{"ok": True, "id": params.get("case_id")}]

    import ops.ensure_awake as ea

    monkeypatch.setattr(ea, "run_query", fake_run_query)
    out = adb.write_to_graph(object(), row)
    assert out["approval"][0]["ok"]
    (n1, p1), (n2, p2) = calls
    assert n1 == "record_approval"
    assert p1 == {"case_id": "AC-HHG-014", "action": "FILE_REPORT", "route": "L2", "status": "approved", "decided_by": "manager@bank", "decided_at": "2026-09-20 10:00:00", "reason": "3a and R9"}
    assert n2 == "append_case_event"
    assert p2["case_id"] == "AC-HHG-014" and p2["kind"] == "approval" and p2["seq"] == adb.APPROVAL_EVENT_SEQ_BASE + i
    assert json.loads(p2["payload"])["approval_id"] == "AP-AC-HHG-014-FILE_REPORT"
