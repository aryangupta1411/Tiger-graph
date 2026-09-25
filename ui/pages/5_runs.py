"""Runs: tokens, tool calls and latency per case for a run; run manifest; and
agreement between two runs on verdict / pattern / final actions.
"""
from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import common  # noqa: E402

st.set_page_config(page_title="Runs", page_icon=common.PAGE_ICONS["runs"], layout="wide")
common.inject_css()
common.page_header("Runs")

runs = common.list_runs()
if not runs:
    st.warning("No runs.")
    st.stop()
run_id = st.selectbox("Run", runs, index=runs.index(st.session_state.get("run_id")) if st.session_state.get("run_id") in runs else 0)
st.session_state["run_id"] = run_id

rows = []
for cid in common.list_cases_in_run(run_id):
    cr = common.load_case_run(run_id, cid)
    s = common.tool_stats(cr)
    a = cr.answer or {}
    rows.append({"case_id": cid, "tool_calls": s["tool_calls"], "llm_calls": s["llm_calls"], "tokens": s["tokens"], "latency_s": round(s["latency_s"], 1), "tool_latency_s": s["tool_latency_s"], "llm_latency_s": s["llm_latency_s"], "verdict": a.get("case", {}).get("verdict", ""), "pattern": a.get("case", {}).get("pattern", ""), "p": a.get("case", {}).get("fraud_probability", None), "asked": len(a.get("evidence_requests", [])), "sar": a.get("sar", {}).get("file", None)})

if rows:
    tot_calls = sum(r["tool_calls"] for r in rows)
    tot_tok = sum(r["tokens"] for r in rows)
    tot_lat = sum(r["latency_s"] for r in rows)
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Cases", len(rows))
    k2.metric("Tool calls (total / mean)", f"{tot_calls} / {tot_calls/len(rows):.1f}")
    k3.metric("Tokens (total / mean)", f"{tot_tok:,} / {tot_tok/len(rows):,.0f}")
    k4.metric("Latency s (total / mean)", f"{tot_lat:.0f} / {tot_lat/len(rows):.1f}")
    st.dataframe(rows, width="stretch", hide_index=True)
    ch1, ch2, ch3 = st.columns(3)
    ch1.bar_chart({r["case_id"]: r["tool_calls"] for r in rows}, height=220)
    ch1.caption("tool calls per case")
    ch2.bar_chart({r["case_id"]: r["tokens"] for r in rows}, height=220)
    ch2.caption("tokens per case (mostly prompt-cache writes and reads)")
    ch3.bar_chart({r["case_id"]: r["latency_s"] for r in rows}, height=220)
    ch3.caption("wall-clock seconds per case")

st.subheader("Run manifest")
st.json(common.run_manifest(run_id) or {"note": "no run_manifest.json — agent/bench.py writes it (model, prompt sha256s, mode, created_at)"}, expanded=False)

st.subheader("Agreement between two runs")
other = st.selectbox("Compare with", [r for r in runs if r != run_id] or ["—"])
if other != "—":
    agree = []
    for cid in common.list_cases_in_run(run_id):
        a = common.load_case_run(run_id, cid).answer
        b = common.load_case_run(other, cid).answer
        if not a or not b:
            continue
        fa = sorted(x["action"] for x in a["next_best_actions"]["final"])
        fb = sorted(x["action"] for x in b["next_best_actions"]["final"])
        agree.append({"case_id": cid, "verdict": a["case"]["verdict"] == b["case"]["verdict"], "pattern": a["case"]["pattern"] == b["case"]["pattern"], "final actions": fa == fb, "sar": a["sar"]["file"] == b["sar"]["file"], "|Δp|": round(abs(float(a["case"]["fraud_probability"]) - float(b["case"]["fraud_probability"])), 2)})
    if agree:
        n = len(agree)
        st.markdown(f"verdict **{100*sum(r['verdict'] for r in agree)/n:.0f}%** · pattern **{100*sum(r['pattern'] for r in agree)/n:.0f}%** · final actions **{100*sum(r['final actions'] for r in agree)/n:.0f}%** · SAR **{100*sum(r['sar'] for r in agree)/n:.0f}%**")
        st.dataframe(agree, width="stretch", hide_index=True)
    else:
        st.info("No case is present in both runs.")
