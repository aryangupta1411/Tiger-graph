"""Deterministic, evidence-family-driven reply simulation — PLAN §4.6 as settled by decision D5 (sim_hybrid).

reply(request_type, ctx, sc) -> {"assumed_response": str, "outcome": str, "counterfactual": str}

F' / L' = the evidence families for fraud / legitimacy counted by engine/scorecard.py, EXCLUDING the customer's
own trigger statement (for a dispute the denial is what the request re-asks about). p_pre = sc.p_engine, the
probability before the request.

  Risk-score triggers
    customer_validation : confirm when p_pre < 0.5 AND L' >= 1 AND F' <= 1 (text from the matching cleared-case
                          template: travel / new phone / unusual amount); deny when F' >= 2; otherwise no reply (R4)
    step_up_auth        : pass when the device is known to the card AND F' <= 1; fail when F' >= 2; otherwise
                          not completed within the window (R4)
  Disputes (customer_report)
    step_up_auth        : fail when F' >= 2 (memory counts); otherwise inconclusive (R4) — a dispute never "passes"
    customer_validation : confirm only under R7 (the charge matches the card's own recurring pattern) with F' <= 1;
                          deny when F' >= 2; otherwise no reply — a dispute never "confirms" without R7
  analyst_info          : deterministic summary from ring_profile / burst prevalence (unchanged)

The counterfactual names the branch not taken.
"""
from __future__ import annotations

from engine.policy import params
from engine.types import CaseContext, Scorecard


def reply(request_type: str, ctx: CaseContext, sc: Scorecard) -> dict:
    S = params()["simulator"]
    f = sc.flags
    F = len(set(sc.families_fraud) - {"customer"})
    L = len(set(sc.families_legit) - {"customer"})
    dispute = bool(f.get("denial"))
    p_pre = float(sc.p_engine)
    amt = float(f.get("flagged_amt", 0.0))
    if request_type == "customer_validation":
        T = S["customer_validation"]
        if dispute:
            if f.get("recurring_match") and F <= 1:
                return {"assumed_response": T["confirm_recurring"].format(amt=amt), "outcome": "confirm",
                        "counterfactual": "had the customer maintained the dispute, R2 would have applied: BLOCK_CARD (L1) and CREATE_CASE"}
            if F >= 2:
                return {"assumed_response": T["deny"], "outcome": "deny",
                        "counterfactual": "had the customer recognised the purchase, R3 would have applied: CLOSE_NO_FRAUD"}
            return {"assumed_response": T["no_reply"], "outcome": "no_reply",
                    "counterfactual": "a confirmation would have closed the alert (R3); a renewed denial would have led to BLOCK_CARD and CREATE_CASE (R2)"}
        if p_pre < 0.5 and L >= 1 and F <= 1:
            if f.get("channel") == "in_person":
                text = T["confirm_travel"].format(addr1=f.get("flagged_addr1", ""), amt=amt)
            elif f.get("flagged_device_new") == "New":
                text = T["confirm_new_phone"].format(amt=amt)
            else:
                text = T["confirm_amount"].format(amt=amt)
            return {"assumed_response": text, "outcome": "confirm",
                    "counterfactual": "had the customer denied the purchase, R2 would have applied: BLOCK_CARD (L1) and CREATE_CASE"}
        if F >= 2:
            return {"assumed_response": T["deny"], "outcome": "deny",
                    "counterfactual": "had the customer confirmed the purchase, R3 would have applied: CLOSE_NO_FRAUD"}
        return {"assumed_response": T["no_reply"], "outcome": "no_reply",
                "counterfactual": "a confirmation would have closed the alert (R3); a denial would have led to BLOCK_CARD and CREATE_CASE (R2)"}
    if request_type == "step_up_auth":
        T = S["step_up_auth"]
        known = bool(f.get("device_known"))
        if F >= 2:
            return {"assumed_response": T["fail"], "outcome": "fail",
                    "counterfactual": "had the step-up passed, the alert would have closed as legitimate (R3)"}
        if not dispute and known and F <= 1:
            return {"assumed_response": T["pass"], "outcome": "pass",
                    "counterfactual": "had the step-up failed, R2 would have applied: BLOCK_CARD (L1)"}
        return {"assumed_response": T["inconclusive"], "outcome": "inconclusive",
                "counterfactual": "a passed step-up would have closed the alert (R3); a failed one would have led to BLOCK_CARD (R2)"}
    # analyst_info
    if f.get("ring_hit"):
        summary = (f"{len(f.get('wave_cards', []))} other cardholders on device profile `{f.get('ring_id')}` in the current wave, "
                   f"{len(f.get('pre_open_cards', []))} of them active before this case opened; {len(f.get('ring_cases', []))} closed as undocumented fraud in August-September")
    elif f.get("burst_hit"):
        summary = f"{len(f.get('burst_lookalikes', []))} other cards show the same under-$500 burst shape within 30 days"
    else:
        summary = "no additional linkage beyond what the graph already shows"
    return {"assumed_response": S["analyst_info"]["info"].format(summary=summary), "outcome": "info",
            "counterfactual": "the analyst reply confirms the graph evidence; it does not change the action set"}
