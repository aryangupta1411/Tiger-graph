"""Case view: evidence with source badges and refs, evidence families, probability
pre/post, initial vs final actions with route badges and rule chips, the
counterfactual branch, evidence requests, SAR with validator badges, similar
cases, phase timeline and the tool trace.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ui import common  # noqa: E402

st.set_page_config(page_title="Case", page_icon=common.PAGE_ICONS["case"], layout="wide")
common.inject_css()

runs = common.list_runs()
run_id = st.session_state.get("run_id") or (runs[0] if runs else None)
qp = st.query_params
pack_ids = [m["case_id"] for m in common.load_case_pack()]

top1, top2 = st.columns([1, 3])
with top1:
    run_id = st.selectbox("Run", runs, index=runs.index(run_id) if run_id in runs else 0) if runs else None
    st.session_state["run_id"] = run_id
    case_id = common.case_select(st, pack_ids, key="case_page_case", preferred=qp.get("case"))

if not run_id:
    st.warning("No run selected — `make mock-run` or `make bench` first.")
    st.stop()

run = common.load_case_run(run_id, case_id)
meta = common.case_meta(case_id)
ans = run.answer
if ans is None:
    promoted = common.load_promoted_answer(case_id)
    if promoted:
        st.info(f"No answer.json in run `{run_id}` for {case_id}; showing the promoted file cases/{case_id}.json")
        ans = promoted
    else:
        st.error(f"{case_id} has no answer in run `{run_id}` (calls so far: {len(run.calls)}).")
        st.stop()

case = ans["case"]
nba = ans["next_best_actions"]
sar = ans["sar"]
pre, post = common.probability_pre_post(run)
fam_f, fam_l = common.families(run)

with top2:
    common.page_header(case_id, f"{meta.get('trigger_type','')} · card {meta.get('card_id','')} · opened {meta.get('opened_at','')}")
    st.markdown(
        " ".join(
            [
                common.badge(f"verdict {case['verdict']}", common.VERDICT_COLORS.get(case["verdict"], "gray")),
                common.badge(f"status {case['status']}", common.STATUS_COLORS.get(case["status"], "gray")),
                common.badge(f"pattern {case['pattern']}", "primary"),
                common.badge(f"exposure {common.money(case['exposure_usd'])}", "blue"),
                common.badge("SAR" if sar["file"] else "no SAR", "red" if sar["file"] else "gray"),
                common.badge(f"graph {case.get('graph_case_id') or '—'}", "green" if case.get("written_to_graph") else "gray"),
            ]
        )
    )
    st.info(meta.get("trigger_text", ""))

m1, m2, m3, m4 = st.columns(4)
m1.metric("Probability (pre-evidence)", common.pct(pre))
m2.metric("Probability (post-evidence)", common.pct(post), delta=None if pre is None or post is None else f"{100*(post-pre):+.0f} pts")
m3.metric("Evidence families for fraud", len(fam_f), help=", ".join(common.FAMILY_LABELS.get(f, f) for f in fam_f) or "none")
m4.metric("Families for legitimacy", len(fam_l), help=", ".join(common.FAMILY_LABELS.get(f, f) for f in fam_l) or "none")
m5, m6, m7 = st.columns(3)
m5.metric("Tool calls", ans.get("tool_calls", 0))
m6.metric("Tokens", f"{ans.get('tokens', 0):,}")
m7.metric("Latency", f"{ans.get('latency_s', 0):.0f}s")

st.markdown(f"**Summary.** {case.get('summary','')}")
if case["pattern"] == "undocumented":
    st.warning(f"**Undocumented pattern (R9).** {case.get('pattern_description','')}")

tabs = st.tabs(["Evidence", "Families & score", "Actions: initial → final", "Evidence requests", "SAR", "Similar cases", "Timeline", "Tool trace", "Raw JSON"])

# ---------------------------------------------------------------- Evidence
with tabs[0]:
    st.caption("Every claim carries a source badge, the query/section it came from (`ref`) and the ids it rests on. Ids are real dataset ids; agent-written case ids never appear in `entity_ids` .")
    for i, ev in enumerate(case.get("evidence", []), 1):
        fam = common.family_of_evidence(ev)
        with st.container(border=True):
            c1, c2 = st.columns([5, 2])
            c1.markdown(f"{common.source_badge(ev.get('source',''))} {common.badge(common.FAMILY_LABELS.get(fam, fam), 'gray')}  **{i}.** {ev.get('claim','')}")
            c2.code(ev.get("ref", ""), language=None)
            if ev.get("entity_ids"):
                st.markdown(" ".join(f"`{e}`" for e in ev["entity_ids"][:40]) + (f" … +{len(ev['entity_ids'])-40}" if len(ev["entity_ids"]) > 40 else ""))
    e1, e2, e3 = st.columns(3)
    e1.markdown("**Affected transactions** (ts ≤ opened_at)")
    e1.write(case.get("affected_txn_ids", []))
    e1.caption(f"first suspicious: `{case.get('first_suspicious_txn_id') or '—'}`")
    e2.markdown(f"**Connected cards** ({len(case.get('connected_card_ids', []))})")
    e2.write(case.get("connected_card_ids", []))
    e3.markdown("**Connected device profiles**")
    for d in case.get("connected_device_profiles", []):
        e3.code(d, language=None)

# ---------------------------------------------------------------- Families & score
with tabs[1]:
    p3 = common.phase_payload(run, "P3")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Independent evidence families** (policy §6 needs ≥ 2 to stop; R1 fires below 0.70 with < 2)")
        for f in common.FAMILY_ORDER:
            if f == "document":
                continue
            mark = common.badge("fraud", "red") if f in fam_f else (common.badge("legit", "green") if f in fam_l else common.badge("—", "gray"))
            st.markdown(f"- {common.FAMILY_LABELS[f]}: {mark}")
        st.markdown(f"**Stop reason:** {ans.get('stop_reason','')}")
    with c2:
        st.markdown("**Scorecard (engine/scorecard.py)**")
        if p3:
            st.markdown(f"- case-memory scorer `cms_p` = {p3.get('cms_p')} → calibrated `cal_p` = {p3.get('cal_p')}")
            st.markdown(f"- engine probability `p_engine` = {p3.get('p_engine')}")
            adj = p3.get("adjustments") or []
            if adj:
                st.table([{"adjustment": a[0], "log-odds": a[1]} if isinstance(a, (list, tuple)) else a for a in adj])
            flags = {k: v for k, v in (p3.get("flags") or {}).items() if v}
            if flags:
                st.markdown("**Flags:** " + " ".join(common.badge(k, "orange") for k in flags))
        else:
            st.caption("No scorecard phase in this run (mock replay); the probability above comes from the answer file.")
        p4 = common.phase_payload(run, "P4")
        if p4.get("calibration_basis"):
            st.markdown(f"**Calibration basis (LLM, clamped to p_engine ± 0.10):** {p4['calibration_basis']}")

# ---------------------------------------------------------------- Actions
with tabs[2]:
    added, removed, kept = common.actions_diff(nba["initial"], nba["final"])
    c1, c2 = st.columns(2)

    def render(col, title, actions, highlight: set[str], colour: str):
        col.markdown(f"#### {title}")
        for a in actions:
            chips = " ".join(common.badge(r, "violet") for r in common.rule_chips(a.get("reason", "")))
            mark = common.badge("changed", colour) if a.get("action") in highlight else ""
            with col.container(border=True):
                st.markdown(f"**{a.get('action')}** {common.route_badge(a.get('route',''))} {chips} {mark}")
                st.caption(a.get("reason", ""))

    render(c1, "Initial (before any evidence request)", nba["initial"], set(removed), "red")
    render(c2, "Final (after the assumed response)", nba["final"], set(added), "green")
    st.markdown(f"**What changed:** {nba.get('what_changed','')}")
    if added or removed:
        st.markdown(f"added: {', '.join(added) or '—'} · removed: {', '.join(removed) or '—'} · kept: {', '.join(kept) or '—'}")
    cf = common.counterfactual(run)
    br = common.voi_branches(run)
    with st.container(border=True):
        st.markdown("**Counterfactual (value-of-information check, engine/voi.py)**")
        if br:
            for k, v in br.items():
                acts = [x.get("action") if isinstance(x, dict) else x for x in (v or [])]
                st.markdown(f"- had the reply been **{k}** → {', '.join(acts) or '—'}")
        st.markdown(cf or "No request was made: no admissible request could change the action set.")
    st.caption("Routes: auto = agent may execute · L1 = team lead · L2 = fraud manager (Policy §2). L1/L2 items land in the Approvals inbox.")

# ---------------------------------------------------------------- Evidence requests
with tabs[3]:
    if not ans.get("evidence_requests"):
        st.info("No evidence was requested (stop rule met before any request, or zero value of information).")
    for i, r in enumerate(ans.get("evidence_requests", []), 1):
        with st.container(border=True):
            st.markdown(f"**{i}. {r.get('type')}** — asked after step {r.get('asked_after_step')}")
            st.markdown(f"Assumed response: _{r.get('assumed_response','')}_")
    p6 = common.phase_payload(run, "P6")
    if p6.get("voi"):
        st.json(p6["voi"], expanded=False)

# ---------------------------------------------------------------- SAR
with tabs[4]:
    checks = common.sar_checks(ans, meta)
    p8 = common.phase_payload(run, "P8")
    for chk in (p8.get("validator") or {}).get("checks", []) or []:
        checks.append((f"validator: {chk.get('name','')}", bool(chk.get("ok")), chk.get("detail", "")))
    st.markdown(" ".join(common.badge(("✓ " if ok else "✗ ") + name, "green" if ok else "red") for name, ok, _ in checks))
    st.markdown(f"**File:** {sar['file']} — {sar.get('reason','')}")
    if sar["file"]:
        st.markdown(sar.get("narrative", ""))
        s1, s2, s3 = st.columns(3)
        s1.markdown("**Subjects**")
        s1.write(sar.get("subjects", []))
        s2.metric("Total amount", common.money(sar.get("total_amount_usd")))
        s3.markdown(f"**Activity dates** {' → '.join(sar.get('activity_dates', []))}")
        with st.expander("Validator details"):
            st.table([{"check": n, "ok": ok, "detail": d} for n, ok, d in checks])
        if run.sar_md:
            st.download_button("Download sar.md", run.sar_md, file_name=f"{case_id}-sar.md")

# ---------------------------------------------------------------- Similar cases
with tabs[5]:
    st.caption("Closed-case ids retrieved as memory (structural candidate set → TigerVector rerank, diversified). Agent-written cases are named in the summary and stored as CASE_SIMILAR_TO edges.")
    st.write(case.get("similar_prior_cases", []))
    p1 = common.phase_payload(run, "P1")
    if p1.get("similar_cases"):
        st.dataframe(p1["similar_cases"], width="stretch", hide_index=True)

# ---------------------------------------------------------------- Timeline
with tabs[6]:
    if not run.phases:
        st.info("No phases.jsonl for this case.")
    for ph in run.phases:
        with st.expander(f"{ph.get('phase','')} · {ph.get('name','')} · {ph.get('at','')}", expanded=False):
            st.json(ph.get("payload", {}), expanded=False)

# ---------------------------------------------------------------- Tool trace
with tabs[7]:
    stats = common.tool_stats(run)
    st.markdown(f"**{stats['tool_calls']}** graph/retrieval calls · **{stats['llm_calls']}** LLM calls · **{stats['tokens']:,}** tokens · tools {stats['tool_latency_s']}s · LLM {stats['llm_latency_s']}s")
    st.dataframe(
        [
            {
                "seq": c.get("seq"),
                "phase": c.get("phase"),
                "kind": c.get("kind", "tool"),
                "name": c.get("name"),
                "params / model": json.dumps(c.get("params"), ensure_ascii=False) if c.get("kind", "tool") == "tool" else c.get("model", ""),
                "latency_s": c.get("latency_s"),
                "bytes / tokens": str(c.get("result_bytes", "")) if c.get("kind", "tool") == "tool" else f"{c.get('input_tokens',0)}+{c.get('output_tokens',0)}",
                "ok": c.get("ok", True),
            }
            for c in run.calls
        ],
        width="stretch",
        hide_index=True,
    )

# ---------------------------------------------------------------- Raw
with tabs[8]:
    st.json(ans, expanded=False)
    st.download_button("Download answer.json", json.dumps(ans, indent=2, ensure_ascii=False), file_name=f"{case_id}.json", mime="application/json")
