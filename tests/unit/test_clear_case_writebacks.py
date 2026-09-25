"""ops/clear_case_writebacks.py against a fake pyTigerGraph connection (no workspace)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ops import clear_case_writebacks as ccw

ROOT = Path(__file__).resolve().parents[2]


class FakeConn:
    """The three write-back types plus rows that must survive (another agent case, a closed case)."""

    def __init__(self, stuck: set[str] | None = None):
        self.stuck = stuck or set()
        self.deleted: list[tuple[str, list[str]]] = []
        self.v = {
            "AgentCase": {"AC-HHG-011": {"source_case_id": "HHG-011", "status": "escalated"},
                          "AC-HHG-019": {"source_case_id": "HHG-019", "status": "open"},
                          "AC-HHG-0190": {"source_case_id": "HHG-0190", "status": "open"},     # not an exam id
                          "AC-OTHER": {"source_case_id": "OTHER", "status": "open"}},
            "CaseEvent": {f"AC-HHG-011-{i:03d}": {"case_id": "AC-HHG-011", "kind": "evidence"} for i in range(1, 9)}
            | {"AC-OTHER-001": {"case_id": "AC-OTHER", "kind": "evidence"}},
            "Approval": {"AP-AC-HHG-011-BLOCK_CARD": {"case_id": "AC-HHG-011", "action": "BLOCK_CARD", "status": "pending"},
                         "AP-AC-OTHER-BLOCK_CARD": {"case_id": "AC-OTHER", "action": "BLOCK_CARD", "status": "pending"}},
            "ClosedCase": {"CC-2649": {}},
        }

    def getVertices(self, vertexType, select="", where="", limit=None, **_):
        keep = [k for k in select.split(",") if k]
        return [{"v_id": i, "v_type": vertexType, "attributes": {k: a[k] for k in keep if k in a}}
                for i, a in self.v[vertexType].items()]

    def delVerticesById(self, vertexType, vertexIds, permanent=False, timeout=0):
        assert vertexType in ccw.WRITEBACK_TYPES
        self.deleted.append((vertexType, list(vertexIds)))
        n = 0
        for i in vertexIds:
            if i not in self.stuck:
                n += self.v[vertexType].pop(i, None) is not None
        return n


def test_dry_run_lists_only_exam_writebacks_and_deletes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    conn = FakeConn()
    assert ccw.main(["--run-id", "dry"], conn=conn) == 0
    assert conn.deleted == []
    rec = json.loads((tmp_path / "dry" / "stale_writebacks.json").read_text())
    ids = [r["id"] for r in rec["found"]]
    assert set(ids) == {f"AC-HHG-011-{i:03d}" for i in range(1, 9)} | {"AP-AC-HHG-011-BLOCK_CARD", "AC-HHG-011", "AC-HHG-019"}
    assert rec["counts"] == {"CaseEvent": 8, "Approval": 1, "AgentCase": 2} and rec["applied"] is False
    assert [r["vertex_type"] for r in rec["found"]][-1] == "AgentCase"          # children listed (and deleted) first


def test_apply_deletes_exactly_the_listed_ids_and_confirms_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    conn = FakeConn()
    assert ccw.main(["--apply", "--run-id", "clean"], conn=conn) == 0
    assert [t for t, _ in conn.deleted] == ["CaseEvent", "Approval", "AgentCase"]
    assert set(conn.v["AgentCase"]) == {"AC-HHG-0190", "AC-OTHER"}
    assert set(conn.v["CaseEvent"]) == {"AC-OTHER-001"} and set(conn.v["Approval"]) == {"AP-AC-OTHER-BLOCK_CARD"}
    assert conn.v["ClosedCase"] == {"CC-2649": {}}
    rec = json.loads((tmp_path / "clean" / "stale_writebacks.json").read_text())
    assert rec["deleted"] == 11 and rec["remaining"] == []


def test_apply_fails_when_a_vertex_survives(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    conn = FakeConn(stuck={"AP-AC-HHG-011-BLOCK_CARD"})
    assert ccw.main(["--apply", "--run-id", "stuck"], conn=conn) == 1
    rec = json.loads((tmp_path / "stuck" / "stale_writebacks.json").read_text())
    assert [r["id"] for r in rec["remaining"]] == ["AP-AC-HHG-011-BLOCK_CARD"]


def test_case_subset_and_non_exam_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    conn = FakeConn()
    assert ccw.main(["--apply", "--cases", "HHG-019", "--run-id", "one"], conn=conn) == 0
    assert "AC-HHG-011" in conn.v["AgentCase"] and "AC-HHG-019" not in conn.v["AgentCase"]
    assert ccw.main(["--cases", "OTHER"], conn=FakeConn()) == 2


def test_connects_through_ops_ensure_awake(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    called = []
    monkeypatch.setattr("ops.ensure_awake.tg_connection", lambda *a, **k: called.append(1) or FakeConn())
    assert ccw.main(["--run-id", "conn"]) == 0 and called == [1]


@pytest.mark.parametrize("vtype, vid", [("AgentCase", "AC-HHG-001"), ("CaseEvent", "AC-HHG-020-014"), ("Approval", "AP-AC-HHG-014-FILE_REPORT")])
def test_id_shapes_match_the_schema_contract(vtype, vid):
    assert ccw.belongs(vtype, vid, {}, ccw.EXAM_CASES)


def test_writeback_types_are_what_the_agent_writes():
    """contracts/schema.md documents the three agent-written vertex types and their id shapes; agent/persist.py writes
    AgentCase by upsertVertex and CaseEvent / Approval through append_case_event / record_approval."""
    schema = (ROOT / "contracts" / "schema.md").read_text()
    for vtype, example in (("AgentCase", "AC-HHG-001"), ("CaseEvent", "AC-HHG-001-007"), ("Approval", "AP-AC-HHG-014-FILE_REPORT")):
        row = re.search(rf"^\| `{vtype}` \| `id` \(`([^`]+)`\)", schema, re.M)
        assert row and row.group(1) == example, vtype
        assert ccw.belongs(vtype, example, {}, ccw.EXAM_CASES)
    persist = (ROOT / "agent" / "persist.py").read_text()
    assert 'upsertVertex), "AgentCase"' in persist
    assert '"append_case_event"' in persist and '"record_approval"' in persist
    assert set(re.findall(r'"HAS_(?:EVENT|APPROVAL)": \("AgentCase", "(\w+)"\)', persist)) == {"CaseEvent", "Approval"}
