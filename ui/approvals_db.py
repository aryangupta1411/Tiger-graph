"""Approval inbox storage (PLAN §4.7, §5 item 1).

``engine.policy.authorize()`` executes ``auto`` actions and queues every ``L1`` /
``L2`` action here as ``pending`` (the answer file records the recommendation,
never the execution). The Streamlit approvals page lets a team lead / fraud
manager approve or reject; a decision is then written to the graph through the
harness-only writer ``record_approval`` (contract 2A) plus an ``append_case_event``
of kind ``approval``, using pyTigerGraph.

Schema (exactly as specified for module G):
    approvals(id, case_id, action, route, reason, status, decided_by, decided_at)

``case_id`` is the source case id (HHG-014); the graph side uses
``AC-<case_id>`` for the AgentCase key and ``AP-AC-<case_id>-<ACTION>`` for the
Approval vertex (contract 1).

Python API (used by the agent):
    from ui.approvals_db import enqueue
    enqueue("HHG-014", "FILE_REPORT", "L2", "3a and R9: ...")
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT NOT NULL,
    action      TEXT NOT NULL,
    route       TEXT NOT NULL CHECK (route IN ('L1', 'L2')),
    reason      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_by  TEXT,
    decided_at  TEXT,
    UNIQUE (case_id, action)
);
"""

STATUSES = ("pending", "approved", "rejected")
COLUMNS = ("id", "case_id", "action", "route", "reason", "status", "decided_by", "decided_at")

# CaseEvent.seq for approval events: agent phases use 1..99, approvals 500 + row id (id is 3-digit padded).
APPROVAL_EVENT_SEQ_BASE = 500

# decided_by is free text from the approvals page and the inbox is shared by every visitor of a
# deployment, so it is capped here as well as in the widget.
DECIDED_BY_MAX = 64


def default_path() -> Path:
    return Path(os.getenv("APPROVALS_DB", str(Path(__file__).resolve().parent / "approvals.sqlite")))


def connect(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    p = Path(path) if path else default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def init_db(path: str | os.PathLike | None = None) -> Path:
    con = connect(path)
    con.close()
    return Path(path) if path else default_path()


def enqueue(case_id: str, action: str, route: str, reason: str, path: str | os.PathLike | None = None) -> int:
    """Queue an L1/L2 recommendation as pending. Idempotent per (case_id, action): returns the existing row id."""
    if route not in ("L1", "L2"):
        raise ValueError(f"only L1/L2 actions need approval, got {route!r} for {action}")
    con = connect(path)
    try:
        with con:
            con.execute(
                "INSERT OR IGNORE INTO approvals(case_id, action, route, reason) VALUES (?, ?, ?, ?)",
                (case_id, action, route, reason or ""),
            )
            row = con.execute("SELECT id FROM approvals WHERE case_id = ? AND action = ?", (case_id, action)).fetchone()
            return int(row["id"])
    finally:
        con.close()


def enqueue_from_answer(answer: dict, path: str | os.PathLike | None = None) -> list[int]:
    """Queue every L1/L2 action in ``next_best_actions.final`` of an answer file."""
    ids = []
    for a in answer.get("next_best_actions", {}).get("final", []):
        if a.get("route") in ("L1", "L2"):
            ids.append(enqueue(answer["case_id"], a["action"], a["route"], a.get("reason", ""), path))
    return ids


def rows(status: str | None = None, path: str | os.PathLike | None = None) -> list[dict]:
    con = connect(path)
    try:
        if status:
            cur = con.execute("SELECT * FROM approvals WHERE status = ? ORDER BY id", (status,))
        else:
            cur = con.execute("SELECT * FROM approvals ORDER BY id")
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def get(approval_id: int, path: str | os.PathLike | None = None) -> dict | None:
    con = connect(path)
    try:
        r = con.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def decide(approval_id: int, status: str, decided_by: str, path: str | os.PathLike | None = None, now: str | None = None) -> dict:
    """Mark a row approved/rejected; returns the updated row."""
    if status not in ("approved", "rejected"):
        raise ValueError(status)
    when = now or dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
    if decided_by is not None:
        decided_by = str(decided_by)[:DECIDED_BY_MAX]
    con = connect(path)
    try:
        with con:
            con.execute("UPDATE approvals SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?", (status, decided_by, when, approval_id))
        r = con.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if r is None:
            raise KeyError(approval_id)
        return dict(r)
    finally:
        con.close()


def reopen(approval_id: int, path: str | os.PathLike | None = None) -> None:
    con = connect(path)
    try:
        with con:
            con.execute("UPDATE approvals SET status = 'pending', decided_by = NULL, decided_at = NULL WHERE id = ?", (approval_id,))
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Graph write-through (live mode)                                             #
# --------------------------------------------------------------------------- #
def graph_case_id(case_id: str) -> str:
    return case_id if case_id.startswith("AC-") else f"AC-{case_id}"


def approval_vertex_id(case_id: str, action: str) -> str:
    return f"AP-{graph_case_id(case_id)}-{action}"


def write_to_graph(conn, row: dict) -> dict:
    """Write a decided approval to the graph through the harness-only writers (contract 2A).

    record_approval(case_id, action, route, status, decided_by, decided_at, reason) upserts the
    Approval vertex ``AP-AC-<case>-<ACTION>`` and the HAS_APPROVAL edge; append_case_event adds a
    CaseEvent of kind ``approval`` so the timeline shows the human decision.
    Returns {"approval": <printed>, "event": <printed>}; raises on failure (the page shows it).
    """
    from ops.ensure_awake import run_query

    gid = graph_case_id(row["case_id"])
    decided_at = row.get("decided_at") or dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
    ap_params = {
        "case_id": gid,
        "action": row["action"],
        "route": row["route"],
        "status": row["status"],
        "decided_by": row.get("decided_by") or "ui",
        "decided_at": decided_at,
        "reason": row.get("reason") or "",
    }
    approval = run_query(conn, "record_approval", ap_params, timeout_ms=30_000)
    event_payload = json.dumps({"approval_id": approval_vertex_id(row["case_id"], row["action"]), **ap_params}, ensure_ascii=False)
    event = run_query(
        conn,
        "append_case_event",
        {"case_id": gid, "seq": APPROVAL_EVENT_SEQ_BASE + int(row["id"]), "kind": "approval", "payload": event_payload},
        timeout_ms=30_000,
    )
    return {"approval": approval, "event": event}
