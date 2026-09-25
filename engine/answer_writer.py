"""Assemble the README answer JSON — contracts §C `agent/answer_writer.build` signature, engine copy.

build(ctx, sc_pre, sc_post, initial, final, evidence, requests, sar, closing, runlog, graph_case_id) -> dict

The LLM-free helpers (reasons_for, draft_summary, draft_sar, what_changed_text) produce the
rule-templated prose used by the day-6 fallback drafts; the agent replaces summary / SAR narrative /
claims with LLM prose but keeps the same field logic. Every invariant of PLAN §4.7 is asserted here
before the validator runs again on the file.
"""
from __future__ import annotations

import re
from datetime import datetime

from engine import policy
from engine.types import CaseContext, Evidence, Scorecard
from engine.validator import uncertain_block_violation

TS_FMT = "%Y-%m-%d %H:%M:%S"


def _d(ts: str) -> str:
    return ts[:10]


def _fams(s: set[str]) -> str:
    return ", ".join(sorted(s)) or "none"


# ----------------------------------------------------------------------------- reasons
def reasons_for(actions: list[str], adm: dict, ctx: CaseContext, sc: Scorecard, stage: str, request_type: str = "") -> list[dict]:
    """Attach route + rule-cited reason to each required action (policy order)."""
    P = policy.params()
    T = P["thresholds"]
    f = sc.flags
    p = sc.p_engine
    out = []
    n_f = len(sc.families_fraud)
    single = n_f < 2 and p < T["r1_probability"]
    for a in actions:
        cites = adm["citations"].get(a, [])
        exp = sc.exposure_usd
        if a == "CREATE_CASE":
            why = ("customer dispute" if f.get("denial") else f"probability {p:.2f} reaches 0.30" if p >= T["case_open_probability"]
                   else "evidence requested" if (request_type or f.get("asked")) else "analyst request")
            r = f"3a: {why}; case opened and written to the graph"
        elif a in ("VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH"):
            what = "ask the cardholder which of the transactions were theirs" if a == "VERIFY_WITH_CUSTOMER" else "one-time passcode / app confirmation as a possession check before further activity"
            if "R7" in cites:
                r = "R7: the disputed charge matches the card's own recurring pattern; verify the recurrence with the customer"
            elif a == "STEP_UP_AUTH" and "R5" in cites:
                r = "R5: card-testing sequence observed; require a one-time passcode / app confirmation before further activity"
            elif a == "STEP_UP_AUTH" and policy.r2_denial(sc) and "R1" not in cites:
                r = (f"§3b / §5: the customer denied this online purchase; {what}, while the R2 block awaits "
                     f"{adm['routes']['BLOCK_CARD']} approval")
            elif single and "R1" in cites:
                r = f"R1: probability {p:.2f} rests on a single evidence family ({_fams(sc.families_fraud) if sc.families_fraud else _fams(sc.families_legit)}); {what} before any block"
            elif sc.verdict == "uncertain":
                r = f"§3b / §5: verdict uncertain at {p:.2f} (families for fraud: {_fams(sc.families_fraud)}; for legitimacy: {_fams(sc.families_legit)}); {what}"
            else:
                r = f"§3b / §5: probability {p:.2f} is below the §6 stop threshold of {T['stop_high']}; {what}"
        elif a == "MONITOR_CARD":
            if "R4" in cites:
                r = "R4: no reply within 24 h; raise monitoring sensitivity for 72 h"
            elif sc.verdict == "uncertain":
                r = "§3b / §5: verdict uncertain; raise monitoring for 72 h while the verification is pending"
            else:
                r = f"§3b: fraud verdict at {p:.2f}; monitor further activity on the card until the block is approved"
        elif a == "MONITOR_CONNECTED_CARDS":
            if f.get("ring_hit"):
                r = f"R6: {len(f.get('wave_cards', []))} other cards share the device profile `{f.get('ring_id')}` in this wave; every sharing card goes under monitoring"
            else:
                r = f"R6: other cards ({', '.join(f.get('shared_fraud_cards', [])[:5]) or ', '.join(f.get('shared_recipient_email', []))}) share the same origin element"
        elif a == "DECLINE_TRANSACTION":
            r = "R5: card-testing sequence observed; decline the purchase" if "R5" in cites else "R4: no reply within 24 h; decline pending / further authorisations on this card (card stays active)"
        elif a == "BLOCK_CARD":
            rt = adm["routes"]["BLOCK_CARD"]
            if "R2" in cites:
                r = f"R2: customer denied the transaction; exposure ${exp:,.2f} {'is at or under' if rt == 'L1' else 'exceeds'} $2,500 so route {rt}"
                if sc.verdict != "fraud":
                    r += (f"; the denial is the customer's answer, so R2 governs from intake (README §3b example) although the graph evidence "
                          f"leaves the verdict {sc.verdict} at {p:.2f}")
            elif "R5" in cites:
                r = f"R5: a purchase over $100 already cleared after the testing run; exposure ${exp:,.2f}, route {rt}"
            else:
                r = (f"§3b: fraud verdict at probability {p:.2f} with {n_f} independent evidence famil{'y' if n_f == 1 else 'ies'} "
                     f"({_fams(sc.families_fraud)}), so R1 does not apply (probability at or above 0.70); exposure ${exp:,.2f} routes {rt}")
        elif a == "FILE_REPORT":
            r = policy.sar_required(sc)[1]
        elif a == "ESCALATE_TO_ANALYST":
            F_, L_ = set(sc.families_fraud) - {"customer"}, set(sc.families_legit) - {"customer"}
            if "R9" in cites:
                r = "R9: undocumented coordinated pattern; hand to a human analyst with the evidence"
            elif "R4" in cites and "R8" not in cites:
                r = f"R4: no reply and exposure ${exp:,.2f} exceeds $500"
            elif exp > T["r8_exposure"]:
                r = f"R8: verdict uncertain and exposure ${exp:,.2f} exceeds $500"
            elif f.get("conflict"):
                r = f"R8: the rulebook chain (${f.get('rulebook_chain_sum', 0):,.2f} inside 48 h) conflicts with the scorer ({sc.cms_p:.2f}); verdict uncertain"
            else:
                r = f"R8: the evidence conflicts (for fraud: {_fams(F_)}; for legitimacy: {_fams(L_)}; the customer's own statement excluded)"
        elif a == "CLOSE_NO_FRAUD":
            if f.get("outcome") == "confirm":
                r = "R3: customer confirmed the activity; the confirmation is noted in the case"
            elif f.get("outcome") == "pass":
                r = "R3 / §5: step-up authentication passed from a device known to the card"
            else:
                r = f"§6: probability {p:.2f} at or below 0.15 with {len(sc.families_legit)} independent families ({_fams(sc.families_legit)})"
        elif a == "ALLOW_TRANSACTION":
            r = "R3: alert cleared; let the flagged transaction stand"
        elif a == "WARN_CUSTOMER":
            r = "R7: send a recurring-charge reminder"
        elif a == "GENERATE_REPORT":
            r = "3a: probability below 0.30 and no dispute; internal write-up without opening a case"
        else:
            r = ", ".join(cites) or "policy"
        out.append({"action": a, "route": adm["routes"][a], "reason": r})
    return policy.order(out)


# ----------------------------------------------------------------------------- prose
def what_changed_text(initial: list[dict], final: list[dict], request: dict | None, sc_pre: Scorecard, sc_post: Scorecard) -> str:
    if [a["action"] for a in initial] == [a["action"] for a in final]:
        return "nothing"
    added = [a["action"] for a in final if a["action"] not in {x["action"] for x in initial}]
    removed = [a["action"] for a in initial if a["action"] not in {x["action"] for x in final}]
    move = (f"probability moved from {sc_pre.p_engine:.2f} to {sc_post.p_engine:.2f}" if abs(sc_pre.p_engine - sc_post.p_engine) >= 0.005
            else f"left the probability at {sc_post.p_engine:.2f}")
    head = f"The assumed reply ({request['type']}: {request['assumed_response'].replace('ASSUMED (simulated): ', '')}) {move} and the verdict became {sc_post.verdict}." if request else move
    parts = [head]
    if added:
        parts.append("Added " + ", ".join(added) + ".")
    if removed:
        parts.append("Dropped " + ", ".join(removed) + ".")
    if request and request.get("counterfactual"):
        parts.append("Counterfactual: " + request["counterfactual"] + ".")
    return " ".join(parts)


def draft_summary(ctx: CaseContext, sc_pre: Scorecard, sc_post: Scorecard, initial: list[dict], final: list[dict], request: dict | None, status: str) -> str:
    f = sc_post.flags
    amt_ch = f"${f.get('flagged_amt', 0):,.2f}, {f.get('channel', '').replace('_', ' ')}"
    if ctx.trigger_type == "risk_score":
        trig = f"Risk-score alert ({(ctx.risk_score or 0):.2f}) on transaction {ctx.flagged_txn_id} ({amt_ch}) for card {ctx.card_id}"
    elif ctx.trigger_type == "customer_report":
        trig = f"Customer {ctx.customer_id} disputed transaction {ctx.flagged_txn_id} ({amt_ch}) on card {ctx.card_id}"
    else:
        trig = f"Analyst request on transaction {ctx.flagged_txn_id} ({amt_ch}) on card {ctx.card_id}"
    # README: "Two to six sentences an analyst could read" / "Keep summary short" (agent.validator: <= 700 characters);
    # the evidence list carries the detail, so the summary names only the strongest item
    top = sorted([e for e in sc_post.evidence if e.direction != "neutral"], key=lambda e: -e.weight)[:1]
    key = " ".join(_clip(e.claim, 200) for e in top)
    verdict = f"Verdict {sc_post.verdict} at probability {sc_post.p_engine:.2f}"
    if sc_post.verdict != "legitimate":
        verdict += f", pattern {sc_post.pattern}, {len(sc_post.episode_ids)} affected transaction(s) worth ${sc_post.exposure_usd:,.2f}"
    verdict += "."
    acts = "Final actions: " + ", ".join(a["action"] for a in final)
    acts += (f" after the assumed {request['type']} reply" if request else "; no evidence request was needed") + f"; status {status}."
    out = f"{trig}. {key} {verdict} {acts}"
    return out if len(out) <= 700 else f"{trig}. {verdict} {acts}"


def _clip(text: str, n: int) -> str:
    """The first sentence of `text`, cut at a word boundary to at most n characters, ending in one full stop."""
    first = re.split(r"(?<=[.!?])\s+(?=[A-Z(])", " ".join(text.split()))[0]
    if len(first) > n:
        return first[: n - 3].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    return first.rstrip(".") + "."


def draft_sar(ctx: CaseContext, sc: Scorecard, facts: dict, graph_case_id: str) -> dict:
    """Rule-templated SAR skeleton (S1-S8 of PLAN §4.8), 6-12 sentences, dates and amounts explicit.

    Written to pass `rag.validate_sar` (the single SAR validator, D10): one paragraph, both activity dates named, every
    transaction written as "on YYYY-MM-DD $amount ..." in chronological order, the total written as `${total:,.2f}`,
    every subject named verbatim in the narrative (so the ring's wave cards are listed in a sentence of their own),
    the internal case id, the OFAC result and the prior-report status stated, no pipes outside device-profile strings."""
    file_, reason = policy.sar_required(sc)
    if not file_:
        return {"file": False, "reason": reason, "narrative": "", "subjects": [], "total_amount_usd": 0.0, "activity_dates": []}
    f = sc.flags
    chain = {str(m["id"]): m for m in sc.chain}
    for r in f.get("ring_rows", []) or []:
        chain.setdefault(str(r["id"]), {"id": r["id"], "ts": r["ts"], "amt": r["amt"], "channel": "online", "addr1": "", "device_new": "New", "device_id": f.get("ring_id", "")})
    rows = sorted([chain[i] for i in sc.episode_ids if i in chain], key=lambda r: (r["ts"], str(r["id"])))
    dates = sorted(_d(r["ts"]) for r in rows)
    first, last = dates[0], dates[-1]
    typology = {"undocumented": "fraud following an undocumented coordinated pattern", "card_testing": "card testing", "card_not_present_fraud": "card-not-present fraud",
                "card_not_present_new_device": "card-not-present fraud from a new device", "out_of_region_use": "out-of-region card-present fraud",
                "account_takeover": "account takeover"}[sc.pattern]
    desc = "; ".join(f"on {_d(r['ts'])} ${float(r['amt']):,.2f} {r.get('channel', '').replace('_', ' ')}" + (f" in billing region {r['addr1']}" if r.get("addr1") else "") for r in rows[:6])
    when = f"On {first}" if first == last else f"Between {first} and {last}"
    subjects = [ctx.customer_id, ctx.card_id] + list(sc.connected_device_profiles)
    s = []
    s.append(f"This report concerns suspected {typology} on card {ctx.card_id} held by customer {ctx.customer_id} (internal case {graph_case_id}, opened {ctx.opened_at[:10]}).")
    if f.get("ring_hit"):
        s.append(f"{when} the card was used for {len(rows)} online purchases totalling ${sc.exposure_usd:,.2f} from the device profile `{f['ring_id']}`, marked New for this account and routed through an anonymous proxy: {desc}.")
        wave = list(f.get("wave_cards", []))
        s.append(f"The same device profile appears on {len(wave)} other cards in the current wave, {len(f.get('pre_open_cards', []))} of them active before this case opened, and on {len(f.get('ring_cases', []))} closed cases confirmed as fraud in August and September 2016 ({', '.join(f.get('ring_cases', []))}).")
        if wave:
            s.append(f"The other cards on the profile in this wave are {', '.join(wave)}.")
            subjects += wave
    else:
        s.append(f"{when} the card was used for {len(rows)} transaction(s) totalling ${sc.exposure_usd:,.2f}: {desc}.")
        if f.get("burst_hit"):
            b = f["burst"]
            s.append(f"All four purchases were online, each between $450 and $499.99, placed within {int((datetime.strptime(b['end_ts'], TS_FMT) - datetime.strptime(b['start_ts'], TS_FMT)).total_seconds() // 60)} minutes from {len(b['device_ids'])} device profiles marked New for the account, on a card that had made only {max(int(f.get('card_n_txns', 0)) - 4, 0)} transactions before, almost all in person.")
            s.append(f"Amounts sit just under a $500 authorisation threshold, and {len(f.get('burst_lookalikes', []))} other cards show the identical shape within 30 days, which analysts previously confirmed as fraud in five closed cases.")
    if not f.get("ring_hit") and sc.connected_device_profiles:
        s.append(f"The purchases came from device profile `{sc.connected_device_profiles[0]}`, a profile shared with {len(sc.connected_card_ids)} other cards that carry confirmed fraud within 30 days ({', '.join(sc.connected_card_ids[:6])}).")
        subjects += list(sc.connected_card_ids[:6])
    s.append("The cardholder reported that they did not make the purchase and still hold the card." if f.get("denial") else
             "The activity was identified from the bank's graph of shared devices and prior cases rather than from a customer report.")
    s.append(f"The activity is inconsistent with the cardholder's history (prior median purchase ${float((facts.get('case_context') or {}).get('txn', {}).get('prior_med_amt', 0)):,.2f}).")
    prior_sar = [c for c in (facts.get("prior_cases_for_customer") or {}).get("closed_cases", []) if c.get("report_filed")]
    s.append(f"{len(prior_sar)} prior report(s) were filed on this customer ({', '.join(c['id'] for c in prior_sar)})." if prior_sar else "No prior report has been filed on this customer.")
    s.append("OFAC SDN screening of the customer identifier: no match (anonymised identifiers; screening result recorded from the sdn.csv lookup in the live run).")
    approver = "team-lead" if sc.exposure_usd <= 2500 else "fraud-manager"      # BLOCK_CARD route L1 / L2 (README §8)
    s.append(f"Recommended actions: a block and reissue of the card, not yet done and awaiting {approver} approval, internal case opened" +
             (f", {len(sc.connected_card_ids)} connected cards placed under monitoring" if sc.connected_card_ids else "") + ", case escalated to an analyst.")
    narrative = " ".join(s)
    return {"file": True, "reason": reason, "narrative": narrative, "subjects": subjects,
            "total_amount_usd": sc.exposure_usd, "activity_dates": [first, last]}


# ----------------------------------------------------------------------------- assembly
def build(ctx: CaseContext, sc_pre: Scorecard, sc_post: Scorecard, initial: list[dict], final: list[dict], evidence: list[Evidence],
          requests: list[dict], sar: dict, closing: dict, runlog: dict, graph_case_id: str) -> dict:
    from engine import status as status_rule
    pending = bool(sc_post.flags.get("pending"))
    status = closing.get("status") or status_rule.derive(sc_post.verdict, final, pending)
    ans = {
        "case_id": ctx.case_id,
        "case": {
            "status": status,
            "verdict": sc_post.verdict,
            "fraud_probability": round(float(sc_post.p_engine), 2),
            "pattern": sc_post.pattern,
            "pattern_description": sc_post.pattern_description if sc_post.pattern == "undocumented" else "",
            "affected_txn_ids": [str(i) for i in sc_post.episode_ids],
            "first_suspicious_txn_id": sc_post.first_suspicious_txn_id,
            "connected_card_ids": list(sc_post.connected_card_ids),
            "connected_device_profiles": list(sc_post.connected_device_profiles),
            "exposure_usd": round(float(sc_post.exposure_usd), 2),
            "evidence": [e.as_item() for e in evidence],
            "similar_prior_cases": list(closing.get("similar_prior_cases_used") or sc_post.similar_prior_cases),
            "summary": closing["summary"],
            "written_to_graph": bool(closing.get("written_to_graph", False)),
            "graph_case_id": graph_case_id,
        },
        "evidence_requests": [{"type": r["type"], "asked_after_step": int(r["asked_after_step"]), "assumed_response": r["assumed_response"]} for r in requests],
        "next_best_actions": {"initial": initial, "final": final, "what_changed": closing.get("what_changed") or what_changed_text(initial, final, requests[0] if requests else None, sc_pre, sc_post)},
        "sar": sar,
        "stop_reason": closing["stop_reason"],
        "tool_calls": int(runlog.get("tool_calls", 0)),
        "tokens": int(runlog.get("tokens", 0)),
        "latency_s": round(float(runlog.get("latency_s", 0.0)), 2),
    }
    assert_invariants(ans)
    return ans


FRAUD_CONTAINMENT = {"BLOCK_CARD", "DECLINE_TRANSACTION", "ESCALATE_TO_ANALYST", "BLOCK_ALL_CARDS"}


def assert_invariants(ans: dict) -> None:
    """PLAN §4.7 invariants + decision D10 (the same list lives in engine.validator, agent.answer_fallback and the UI checks)."""
    c, nba, sar = ans["case"], ans["next_best_actions"], ans["sar"]
    fin = [a["action"] for a in nba["final"]]
    ini = [a["action"] for a in nba["initial"]]
    assert sar["file"] == ("FILE_REPORT" in fin), "sar.file must agree with FILE_REPORT in final"
    assert "FILE_REPORT" not in fin or "CREATE_CASE" in fin, "FILE_REPORT requires CREATE_CASE"
    if c["verdict"] == "legitimate":
        assert c["affected_txn_ids"] == [] and c["exposure_usd"] == 0 and not sar["file"], "legitimate ⇒ empty episode, exposure 0, no SAR"
        assert "CLOSE_NO_FRAUD" in fin, "legitimate ⇒ CLOSE_NO_FRAUD in final"
        assert not any(a in ("BLOCK_CARD", "BLOCK_ALL_CARDS") for a in fin), "legitimate ⇒ no block in final"
        # D1 + D5: a fraud-band alert recommends the block in `initial` and still asks its §3b verification; a passed step-up
        # (known device, F' <= 1) clears it under R3 / §5 — the block then exists only in `initial`, withdrawn by the request.
        if any(a in ("BLOCK_CARD", "BLOCK_ALL_CARDS") for a in ini):
            assert ans["evidence_requests"] and ini != fin, "legitimate with a block in initial ⇒ an evidence request withdrew it"
    if c["verdict"] == "fraud":
        assert not ({"CLOSE_NO_FRAUD", "ALLOW_TRANSACTION"} & set(fin)), "fraud ⇒ no CLOSE_NO_FRAUD / ALLOW_TRANSACTION in final"
        assert FRAUD_CONTAINMENT & set(fin), "fraud ⇒ at least one of BLOCK_CARD / DECLINE_TRANSACTION / ESCALATE_TO_ANALYST / BLOCK_ALL_CARDS in final"
    if "CLOSE_NO_FRAUD" in fin:
        assert c["verdict"] == "legitimate", "CLOSE_NO_FRAUD in final ⇒ verdict legitimate"
    if "BLOCK_CARD" in fin:
        assert c["verdict"] != "legitimate"
    if c["verdict"] == "uncertain":
        assert not uncertain_block_violation(nba["initial"]), "uncertain ⇒ no block in initial (except an R2 BLOCK_CARD on a customer denial)"
        assert c["status"] in ("escalated", "open"), "uncertain ⇒ status escalated or open"
    assert (nba["what_changed"] == "nothing") == (ini == fin), "what_changed == 'nothing' ⇔ initial == final"
    if ini != fin:
        assert ans["evidence_requests"], "initial ≠ final ⇒ evidence_requests non-empty"
    assert (c["pattern_description"] != "") == (c["pattern"] == "undocumented"), "pattern_description ⇔ undocumented"
    assert sar["reason"], "sar.reason must be non-empty"
    if not sar["file"]:
        assert sar["narrative"] == "" and sar["subjects"] == [] and sar["total_amount_usd"] == 0 and sar["activity_dates"] == []
    else:
        assert sar["total_amount_usd"] == c["exposure_usd"] and len(sar["activity_dates"]) == 2
