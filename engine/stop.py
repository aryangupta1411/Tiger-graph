"""Stop rule — policy §6, PLAN §4.5.

stop_reason(sc_pre, sc_post, asked, voi_zero) -> str naming the §6 test that fired

  test 1: p >= 0.85 or p <= 0.15 with >= 2 independent evidence families
  test 2: a verification response settled the question
  test 3: further steps would not change the decision (value of information zero / reply pending
          and the R4 set already applies)
"""
from __future__ import annotations

from engine.policy import params
from engine.types import Scorecard


def settled_pre(sc: Scorecard) -> bool:
    from engine.policy import settled_pre as _sp
    return _sp(sc)


def stop_reason(sc_pre: Scorecard, sc_post: Scorecard | None, asked: bool, voi_zero: bool) -> str:
    T = params()["thresholds"]
    def fam(s):
        return ", ".join(sorted(s.families_fraud if s.p_engine >= 0.5 else s.families_legit))

    if not asked:
        if sc_pre.p_engine >= T["stop_high"] and len(sc_pre.families_fraud) >= T["stop_min_families"]:
            return (f"Policy §6 test 1: fraud probability {sc_pre.p_engine:.2f} is at or above {T['stop_high']} with {len(sc_pre.families_fraud)} independent "
                    f"evidence families ({fam(sc_pre)}); no request could change the action set, so the initial recommendation is final.")
        if sc_pre.p_engine <= T["stop_low"] and len(sc_pre.families_legit) >= T["stop_min_families"]:
            return (f"Policy §6 test 1: fraud probability {sc_pre.p_engine:.2f} is at or below {T['stop_low']} with {len(sc_pre.families_legit)} independent "
                    f"evidence families ({fam(sc_pre)}); further steps would not change the decision.")
        from engine.policy import denial_settles
        if denial_settles(sc_pre):
            return ("Policy §6 test 2: the customer's own denial in the trigger is the verification response, and R2 fixes the action set "
                    "(BLOCK_CARD and CREATE_CASE, as in the README §3b example once a customer denies); re-asking the customer would repeat "
                    "the question already answered and step-up authentication does not apply to a card-present purchase, so no evidence request was made.")
        return "Policy §6 test 3: no admissible evidence request could change the recommended actions (value of information zero), so the investigation stopped at a defensible decision."
    assert sc_post is not None
    out = sc_post.flags.get("outcome", "")
    if out in ("confirm", "deny", "pass", "fail"):
        what = {"confirm": "customer confirmation", "deny": "customer denial", "pass": "passed step-up authentication", "fail": "failed step-up authentication"}[out]
        tail = ""
        if sc_post.p_engine >= T["stop_high"] and len(sc_post.families_fraud) >= T["stop_min_families"]:
            tail = f" Probability {sc_post.p_engine:.2f} with {len(sc_post.families_fraud)} families also meets §6 test 1."
        elif sc_post.p_engine <= T["stop_low"] and len(sc_post.families_legit) >= T["stop_min_families"]:
            tail = f" Probability {sc_post.p_engine:.2f} with {len(sc_post.families_legit)} families also meets §6 test 1."
        return f"Policy §6 test 2: the assumed {what} settled the question.{tail}"
    if out == "info":
        return "Policy §6 test 2/3: the analyst reply confirmed the graph evidence and no further step would change the actions."
    if out in ("confirm", "pass") and (sc_post.flags.get("ring_hit") or sc_post.flags.get("burst_hit")):
        return ("Policy §6 test 3: the assumed customer reply cannot clear a coordinated ring / burst pattern (R3 does not apply); the pattern "
                "floor keeps the verdict, so no further automated step would change the recommendation.")
    if sc_post.verdict == "fraud":
        return (f"Policy §6 test 3: the verification was not completed within the window and the probability stayed at {sc_post.p_engine:.2f}; "
                "the fraud-band actions (block pending approval) already apply, so no further automated step would change the recommendation.")
    escalated = "the case is escalated to an analyst (R8)" if sc_post.flags.get("escalated") else "the case stays open for the reply"
    from engine.policy import r2_denial
    block = "R2's block on the customer's denial stands pending approval, " if r2_denial(sc_post) else ""
    return (f"Policy §6 test 3: the verification was not answered within the window; {block}R4 applies (monitor + decline pending authorisations) and {escalated}, "
            "so no further automated step would change the recommendation until a reply arrives.")
