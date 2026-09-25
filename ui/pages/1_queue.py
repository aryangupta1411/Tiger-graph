"""Queue: the 20 exam cases (case_pack.csv) with the state each one reached in the selected run.

"Investigate" runs the agent for one case (live: `python -m agent.bench`), or
replays the recorded tool calls from the run log (mock / demo fallback).
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import common  # noqa: E402

st.set_page_config(page_title="Queue", page_icon=common.PAGE_ICONS["queue"], layout="wide")
common.inject_css()
common.page_header("Case queue", "The 20 exam cases and the state each one reached in the selected run.")

run_id = st.session_state.get("run_id") or (common.list_runs() or [None])[0]
st.session_state["run_id"] = run_id
pack = common.load_case_pack()

f1, f2, f3 = st.columns([1, 1, 2])
trigger = f1.multiselect("Trigger", ["risk_score", "customer_report", "analyst_request"], default=[])
only_unrun = f2.checkbox("Only cases without an answer in this run", value=False)
f3.caption(f"Run **{run_id or '—'}** · cases run in `opened_at` order so later cases can retrieve earlier agent-written ones.")

rows = []
for m in pack:
    cr = common.load_case_run(run_id, m["case_id"]) if run_id else None
    a = cr.answer if cr else None
    if trigger and m["trigger_type"] not in trigger:
        continue
    if only_unrun and a:
        continue
    pre, post = common.probability_pre_post(cr) if cr else (None, None)
    rows.append(
        {
            "case_id": m["case_id"],
            "opened_at": m["opened_at"],
            "trigger": m["trigger_type"],
            "card": m["card_id"],
            "flagged_txn": m["flagged_txn_id"],
            "risk": m.get("risk_score") or "",
            "verdict": a["case"]["verdict"] if a else "",
            "status": a["case"]["status"] if a else ("running" if cr and cr.calls else "not run"),
            "p_pre": common.pct(pre) if a else "",
            "p_post": common.pct(post) if a else "",
            "pattern": a["case"]["pattern"] if a else "",
            "exposure": common.money(a["case"]["exposure_usd"]) if a else "",
            "asked": len(a["evidence_requests"]) if a else 0,
            "changed": (a["next_best_actions"]["what_changed"] != "nothing") if a else False,
            "SAR": a["sar"]["file"] if a else False,
            "final actions": ", ".join(x["action"] for x in a["next_best_actions"]["final"]) if a else "",
        }
    )
st.dataframe(rows, width="stretch", hide_index=True)

st.divider()
left, right = st.columns([1, 2])
with left:
    case_id = common.case_select(st, [m["case_id"] for m in pack], key="queue_case")
    meta = common.case_meta(case_id)
    st.markdown(f"**{meta.get('trigger_type','')}** · opened `{meta.get('opened_at','')}`")
    st.info(meta.get("trigger_text", ""))
    common.link_to("pages/2_case.py", label="Open the case view", icon=common.PAGE_ICONS["case"])

with right:
    mode = "mock" if common.read_only() else common.run_mode()  # read-only deployment: replay, never spawn agent.bench
    st.markdown("**Investigate** — " + ("runs the agent live through the TigerGraph MCP tools" if mode == "live" else "replays the recorded tool trace (mock / demo fallback)"))
    speed = st.slider("Replay speed (s per call)", 0.0, 2.0, 0.4, 0.1)
    if st.button("▶ Investigate", type="primary", width="stretch"):
        box = st.status(f"Investigating {case_id}…", expanded=True)
        if mode == "live":
            new_run = f"ui-{time.strftime('%Y%m%d-%H%M%S')}"
            cmd = [sys.executable, "-m", "agent.bench", "--cases", case_id, "--run-id", new_run]
            box.write(f"`{' '.join(cmd)}`")
            proc = subprocess.Popen(cmd, cwd=str(common.ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            seen = 0
            while proc.poll() is None:
                cr = common.load_case_run(new_run, case_id)
                for c in cr.calls[seen:]:
                    box.write(f"`{c.get('phase','')}` **{c.get('kind','tool')}** `{c.get('name')}` {c.get('params','') if c.get('kind','tool')=='tool' else ''} — {c.get('latency_s','?')}s")
                seen = len(cr.calls)
                time.sleep(1)
            out = proc.stdout.read() if proc.stdout else ""
            if proc.returncode == 0:
                st.session_state["run_id"] = new_run
                box.update(label=f"Done — run {new_run}", state="complete")
            else:
                box.update(label="agent.bench failed", state="error")
                st.code(out[-4000:])
        else:
            cr = common.load_case_run(run_id, case_id) if run_id else None
            if not cr or not cr.calls:
                box.update(label="No recorded trace for this case in the selected run (make mock-run first)", state="error")
            else:
                for c in cr.calls:
                    if c.get("kind") == "llm":
                        box.write(f"`{c.get('phase','')}` {common.badge('LLM', 'violet')} **{c.get('name')}** ({c.get('model','')}) — {c.get('input_tokens',0)}+{c.get('output_tokens',0)} tokens, {c.get('latency_s','?')}s")
                    else:
                        box.write(f"`{c.get('phase','')}` {common.badge('TOOL', 'blue')} **tigergraph__run_installed_query** `{c.get('name')}` `{c.get('params',{})}` — {c.get('result_bytes','?')} B, {c.get('latency_s','?')}s")
                    time.sleep(speed)
                for ph in cr.phases:
                    if ph.get("phase") in ("P4", "P5", "P6", "P7"):
                        box.write(f"**{ph.get('phase')} {ph.get('name')}** → `{str(ph.get('payload'))[:300]}`")
                box.update(label=f"Replayed {len(cr.calls)} calls for {case_id}", state="complete")
                common.link_to("pages/2_case.py", label="See the evidence and the recommendation", icon=common.PAGE_ICONS["case"])
