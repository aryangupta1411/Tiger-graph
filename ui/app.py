"""Analyst dashboard entrypoint.

Run from the repo root:  uv run streamlit run ui/app.py   (or `make ui`)
Pages live in ui/pages/ (Streamlit `pages/` convention: the numeric prefix
orders the sidebar and is stripped from the label, underscores become spaces).

Sidebar state shared by every page: st.session_state["run_id"] (which run's
files to read) and st.session_state["case_id"] (last opened case).
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root on sys.path (streamlit adds ui/ only)

from ui import common  # noqa: E402

st.set_page_config(page_title="HHGOA Fraud Investigator", page_icon=common.PAGE_ICONS["app"], layout="wide")
common.inject_css()


def sidebar() -> str | None:
    """Run selector shared by all pages; returns the selected run id (or None)."""
    runs = common.list_runs()
    with st.sidebar:
        if not runs:
            st.warning("No runs under `runs/`. Create one with `make mock-run` (from cases/ or the sample answer) or `make bench`.")
            st.session_state.setdefault("run_id", None)
            return None
        default = st.session_state.get("run_id") or runs[0]
        idx = runs.index(default) if default in runs else 0
        run_id = st.selectbox("Run", runs, index=idx, help="runs/<run_id>/ — newest first")
        st.session_state["run_id"] = run_id
        man = common.run_manifest(run_id)
        if man and man.get("model"):
            started = str(man.get("started") or man.get("first_started") or "")[:16].replace("T", " ")
            st.caption(f"model `{man['model']}`" + (f" · {started}" if started else ""))
        return run_id


def main() -> None:
    run_id = sidebar()
    st.title("Agentic Fraud Investigation on TigerGraph")
    st.caption("Trigger → investigate through MCP tools → evidence ledger → calibrated probability → policy gate → evidence request → final action → SAR → case written to the graph.")

    if not run_id:
        st.info("Pick or create a run to see the queue, cases, approvals, the case subgraph and run metrics.")
        return

    cases = common.list_cases_in_run(run_id)
    answers = [common.load_case_run(run_id, c).answer for c in cases]
    answers = [a for a in answers if a]
    verdicts = Counter(a["case"]["verdict"] for a in answers)
    statuses = Counter(a["case"]["status"] for a in answers)
    sars = sum(1 for a in answers if a["sar"]["file"])
    changed = sum(1 for a in answers if a["next_best_actions"]["what_changed"] != "nothing")
    asked = sum(1 for a in answers if a["evidence_requests"])
    written = sum(1 for a in answers if a["case"].get("written_to_graph"))

    r1c1, r1c2, r1c3 = st.columns(3)
    r1c1.metric("Cases in run", f"{len(answers)} / {len(common.load_case_pack())}")
    r1c2.metric("Fraud / legitimate / uncertain", f"{verdicts.get('fraud',0)} · {verdicts.get('legitimate',0)} · {verdicts.get('uncertain',0)}")
    r1c3.metric("SARs recommended", sars)
    r2c1, r2c2, r2c3 = st.columns(3)
    r2c1.metric("Evidence requested", asked)
    r2c2.metric("Recommendation changed", changed)
    r2c3.metric("Written to graph", written)

    st.markdown("#### Status mix")
    if statuses:
        import pandas as pd

        st.bar_chart(pd.Series(statuses, name="cases"), height=200, horizontal=True)
    else:
        st.caption("No answers in this run yet.")

    st.markdown("#### Where to go")
    common.link_to("pages/1_queue.py", label="Queue — the 20 exam cases and their state", icon=common.PAGE_ICONS["queue"])
    common.link_to("pages/2_case.py", label="Case — evidence, families, initial vs final, counterfactual, SAR", icon=common.PAGE_ICONS["case"])
    common.link_to("pages/3_approvals.py", label="Approvals — L1/L2 inbox writing Approval vertices", icon=common.PAGE_ICONS["approvals"])
    common.link_to("pages/4_graph.py", label="Graph — case subgraph from `case_subgraph`", icon=common.PAGE_ICONS["graph"])
    common.link_to("pages/5_runs.py", label="Runs — tokens, tool calls, latency, agreement", icon=common.PAGE_ICONS["runs"])

    with st.expander("How this run was produced"):
        st.json(common.run_manifest(run_id) or {"note": "no run_manifest.json"})


main()
