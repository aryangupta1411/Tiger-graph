"""Approval inbox: every L1 / L2 recommendation the policy
gate queued, with Approve / Reject. A decision is written to the graph through
the harness-only writers `record_approval` + `append_case_event` via pyTigerGraph
(live mode); in mock mode it is recorded locally and the page says so.
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import approvals_db, common  # noqa: E402

st.set_page_config(page_title="Approvals", page_icon=common.PAGE_ICONS["approvals"], layout="wide")
common.inject_css()
common.page_header("Approval inbox")
mode = common.run_mode()
graph_writes = mode == "live" and not common.read_only()  # DEPLOY_READONLY=1 keeps decisions local even in live mode
st.caption(
    "Policy §2: the agent executes `auto` actions itself; `L1` (team lead: DECLINE_TRANSACTION, BLOCK_CARD ≤ \\$2,500) and "
    "`L2` (fraud manager: BLOCK_CARD > \\$2,500, BLOCK_ALL_CARDS, FILE_REPORT) wait here. "
    + ("Decisions write an `Approval` vertex and a `CaseEvent(kind=approval)` to FraudGraph." if graph_writes
       else "**Read-only deployment:** decisions are recorded locally only (shared by everyone using this deployment) and never written to FraudGraph." if common.read_only()
       else "**Mock mode:** decisions are stored in ui/approvals.sqlite only.")
)

run_id = st.session_state.get("run_id") or (common.list_runs() or [None])[0]
c1, c2, c3 = st.columns([2, 2, 3])
decided_by = c1.text_input("Decided by", value=st.session_state.get("decided_by", "team-lead@bank"), max_chars=approvals_db.DECIDED_BY_MAX, help="Recorded in Approval.decided_by")
st.session_state["decided_by"] = decided_by
if c2.button(f"Import L1/L2 actions from run `{run_id}`", disabled=not run_id, help="The agent's authorize() normally enqueues these itself; this re-scans answer.json files."):
    n = 0
    for cid in common.list_cases_in_run(run_id):
        a = common.load_case_run(run_id, cid).answer
        if a:
            n += len(approvals_db.enqueue_from_answer(a))
    st.success(f"Queued/refreshed {n} L1/L2 actions from run {run_id}")
show = c3.radio("Show", ["pending", "approved", "rejected", "all"], horizontal=True)

rows = approvals_db.rows(None if show == "all" else show)
if not rows:
    st.info("Nothing here.")
    st.stop()

pending = sum(1 for r in approvals_db.rows("pending"))
st.metric("Pending approvals", pending)


def decide(row: dict, status: str) -> None:
    updated = approvals_db.decide(row["id"], status, decided_by)
    if graph_writes:
        try:
            conn = common.graph_conn()
            out = approvals_db.write_to_graph(conn, updated)
            st.toast(f"{row['case_id']} {row['action']}: {status} → written to graph ({approvals_db.approval_vertex_id(row['case_id'], row['action'])})")
            st.session_state[f"graph_ok_{row['id']}"] = out
        except Exception as exc:  # noqa: BLE001 - surface to the analyst, keep the local decision
            st.session_state[f"graph_err_{row['id']}"] = str(exc)
    else:
        st.toast(f"{row['case_id']} {row['action']}: {status} ({'read-only' if common.read_only() else 'mock'}: local only)")


for r in rows:
    with st.container(border=True):
        a, b, c = st.columns([5, 2, 2])
        chips = " ".join(common.badge(x, "violet") for x in common.rule_chips(r.get("reason", "")))
        a.markdown(f"**{r['case_id']}** · **{r['action']}** {common.route_badge(r['route'])} {chips} · {common.badge(r['status'], {'pending':'orange','approved':'green','rejected':'red'}[r['status']])}")
        a.caption(r.get("reason", ""))
        if r["status"] != "pending":
            who = str(r.get("decided_by") or "").replace("`", "'")  # visitor-typed: inline code, so no links / images / colors
            a.caption(f"decided by `{who}` at {r.get('decided_at')} · graph id `{approvals_db.approval_vertex_id(r['case_id'], r['action'])}`")
        if err := st.session_state.get(f"graph_err_{r['id']}"):
            a.error(f"graph write failed: {err}")
            if a.button("Retry graph write", key=f"retry_{r['id']}"):
                try:
                    approvals_db.write_to_graph(common.graph_conn(), r)
                    st.session_state.pop(f"graph_err_{r['id']}", None)
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.session_state[f"graph_err_{r['id']}"] = str(exc)
        if r["status"] == "pending":
            if b.button("Approve", key=f"ok_{r['id']}", type="primary", width="stretch"):
                decide(r, "approved")
                st.rerun()
            if c.button("Reject", key=f"no_{r['id']}", width="stretch"):
                decide(r, "rejected")
                st.rerun()
        else:
            if b.button("Reopen", key=f"re_{r['id']}", width="stretch"):
                approvals_db.reopen(r["id"])
                st.rerun()
        common.link_to("pages/2_case.py", label="Open case", icon=common.PAGE_ICONS["case"])

with st.expander("Raw table (ui/approvals.sqlite)"):
    st.dataframe(approvals_db.rows(), width="stretch", hide_index=True)
