"""ops/clear_case_writebacks.py — list, and with --apply delete, the agent's own graph write-backs for the exam cases.

A re-run upserts the same AgentCase id but never removes what a superseded run wrote: CaseEvents past the new run's
last seq, Approvals for actions it no longer recommends (a stale pending AP-AC-HHG-011-BLOCK_CARD), and the edges a
different verdict drew (CASE_CONNECTED_TO on AC-HHG-019). This clears them before a clean live pass.

What the agent writes (agent/persist.py; ids from contracts/schema.md) and so what this script may touch:
  AgentCase  AC-<case>                    one upsertVertex per case; its CASE_* / HAS_* edges go with the vertex
  CaseEvent  AC-<case>-NNN                append_case_event, case_id = AC-<case>
  Approval   AP-AC-<case>-<ACTION>        record_approval (agent P10 and the UI), case_id = AC-<case>
A vertex belongs to an exam case when its id has that shape or its case_id attribute is AC-<case>. Nothing else
(ClosedCase, Card, Transaction, DeviceProfile, PolicyChunk, loaded edges, other AgentCases) is read for deletion.

    uv run python -m ops.clear_case_writebacks                      # dry run: list + runs/<run>/stale_writebacks.json
    uv run python -m ops.clear_case_writebacks --apply              # delete exactly the listed ids, then re-list
    uv run python -m ops.clear_case_writebacks --cases HHG-011 HHG-019 --run-id clean-2

Exit 0 on a dry run, or when nothing is left after --apply; 1 when a listed vertex survives the delete.
The local UI approvals queue (ui/approvals.sqlite) is not the graph and is not touched.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from ops.console import Col, Table, detail, fail, header, ok, summary, warn

EXAM_CASES = [f"HHG-{i:03d}" for i in range(1, 21)]
# delete order: children first, then the AgentCase vertex (which takes every attached edge with it)
WRITEBACK_TYPES = ("CaseEvent", "Approval", "AgentCase")
# attributes read for the listing (contracts/schema.md); AgentCase's note_emb vector is never fetched
SELECT = {"CaseEvent": "case_id,kind", "Approval": "case_id,action,status", "AgentCase": "source_case_id,status"}


def id_patterns(cases: list[str]) -> dict[str, re.Pattern]:
    alt = "|".join(re.escape(c) for c in cases)
    return {
        "AgentCase": re.compile(rf"AC-(?:{alt})"),
        "CaseEvent": re.compile(rf"AC-(?:{alt})-\d{{3}}"),
        "Approval": re.compile(rf"AP-AC-(?:{alt})-[A-Z_]+"),
    }


def belongs(vtype: str, vid: str, attrs: dict, cases: list[str], pats: dict[str, re.Pattern] | None = None) -> bool:
    pats = pats or id_patterns(cases)
    if pats[vtype].fullmatch(vid):
        return True
    return vtype != "AgentCase" and str(attrs.get("case_id", "")) in {f"AC-{c}" for c in cases}


def list_writebacks(conn, cases: list[str]) -> list[dict]:
    """Every existing AgentCase / CaseEvent / Approval vertex of the given exam cases, children first."""
    from ops.ensure_awake import ensure_awake

    pats = id_patterns(cases)
    out: list[dict] = []
    for vtype in WRITEBACK_TYPES:
        rows = ensure_awake()(conn.getVertices)(vtype, select=SELECT[vtype]) or []
        for r in rows:
            vid, attrs = str(r.get("v_id", "")), r.get("attributes", {}) or {}
            if belongs(vtype, vid, attrs, cases, pats):
                case_ref = attrs.get("case_id") or (vid if vtype == "AgentCase" else "")
                out.append({"vertex_type": vtype, "id": vid, "case_id": case_ref,
                            "status": attrs.get("status", ""), "action": attrs.get("action", attrs.get("kind", ""))})
    return out


def delete_writebacks(conn, rows: list[dict]) -> int:
    from ops.ensure_awake import ensure_awake

    n = 0
    for vtype in WRITEBACK_TYPES:
        ids = [r["id"] for r in rows if r["vertex_type"] == vtype]
        for i in range(0, len(ids), 500):
            n += int(ensure_awake()(conn.delVerticesById)(vtype, ids[i:i + 500]) or 0)
    return n


def _table(rows: list[dict], title: str) -> Table:
    t = Table(Col("vertex type", max_width=10), Col("id", max_width=34), Col("case_id", max_width=12),
              Col("kind / action", max_width=22), Col("status", max_width=10), title=title)
    for r in rows:
        t.add_row(r["vertex_type"], r["id"], r["case_id"], r["action"], r["status"])
    return t


def _counts(rows: list[dict]) -> dict[str, int]:
    return {vt: sum(1 for r in rows if r["vertex_type"] == vt) for vt in WRITEBACK_TYPES}


def main(argv: list[str] | None = None, conn=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true", help="delete the listed vertices (default: dry run, list only)")
    ap.add_argument("--cases", nargs="+", default=EXAM_CASES, help="exam case ids (default: HHG-001..HHG-020)")
    ap.add_argument("--run-id", default="", help="runs/<run-id>/stale_writebacks.json (default: clear-writebacks-<timestamp>)")
    args = ap.parse_args(argv)
    cases = [c.strip() for c in args.cases if c.strip()]
    bad = [c for c in cases if c not in EXAM_CASES]
    if bad:
        fail(f"not exam case ids: {', '.join(bad)} (this tool only clears HHG-001..HHG-020)")
        return 2
    run_id = args.run_id or f"clear-writebacks-{datetime.now():%Y%m%d-%H%M%S}"
    out_path = Path(os.getenv("RUNS_DIR", "runs")) / run_id / "stale_writebacks.json"
    header("ops.clear_case_writebacks", "the agent's own AgentCase / CaseEvent / Approval write-backs for the exam cases",
           {"mode": "APPLY (delete)" if args.apply else "dry run (list only)", "cases": len(cases), "list file": str(out_path)})
    if conn is None:
        from ops.ensure_awake import tg_connection

        conn = tg_connection()
    rows = list_writebacks(conn, cases)
    _table(rows, "write-backs found").print()
    record = {"generated_at": datetime.now().isoformat(timespec="seconds"), "cases": cases, "applied": bool(args.apply),
              "found": rows, "counts": _counts(rows)}
    if not args.apply:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(record, indent=2))
        detail("dry run: nothing deleted; re-run with --apply (make clear-writebacks APPLY=1) to delete exactly these ids")
        summary("dry run", {**_counts(rows), "total": len(rows), "list": str(out_path)}, status="ok")
        return 0
    deleted = delete_writebacks(conn, rows) if rows else 0
    left = list_writebacks(conn, cases)
    record.update({"deleted": deleted, "remaining": left})
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(record, indent=2))
    if left:
        _table(left, "still present after delete").print()
        fail(f"{len(left)} write-back vertices still present")
    elif deleted != len(rows):
        warn(f"deleted {deleted} of {len(rows)} listed vertices, but none remain")
    else:
        ok("no agent write-backs left for these cases")
    summary("cleared" if not left else "clear incomplete",
            {"listed": len(rows), "deleted": deleted, "remaining": len(left), "list": str(out_path)},
            status="ok" if not left else "fail")
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main())
