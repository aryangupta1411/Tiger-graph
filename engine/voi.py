"""Value-of-information check — PLAN §4.6 / §5 item 5.

should_ask(ctx, sc, request_type) -> (ask, branches)

A request is worth making only if at least one assumed outcome changes the FINAL action set.
branches = {outcome: [action names]} for every outcome the request type can produce, computed by
running policy.admissible on the post-evidence scorecard of each branch. The stop rule (§6 test 1)
is checked by the caller before this: a settled case never asks. A customer_validation request is never
made on an R2 denial (the customer already answered it in the trigger).
"""
from __future__ import annotations

from engine import policy, scorecard
from engine.types import CaseContext, Scorecard

OUTCOMES = {"customer_validation": ["confirm", "deny", "no_reply"],
            "step_up_auth": ["pass", "fail", "inconclusive"],
            "analyst_info": ["info"]}


def request_type_for(ctx: CaseContext, sc: Scorecard) -> str:
    """Which request the policy calls for: analyst_info on analyst triggers, step-up online when the
    device / scorer conflict or the case sits in the fraud or uncertain band, otherwise a customer validation."""
    f = sc.flags
    if ctx.trigger_type == "analyst_request":
        return "analyst_info"
    if f.get("channel") == "online" and (f.get("denial") or f.get("conflict") or (sc.verdict == "fraud" and f.get("flagged_device_new") == "New")
                                         or (sc.verdict == "uncertain" and f.get("denial"))):
        return "step_up_auth"
    return "customer_validation"


def should_ask(ctx: CaseContext, sc: Scorecard, request_type: str) -> tuple[bool, dict]:
    # R2: a customer who has denied the charge has already answered the customer_validation question; asking it again
    # cannot change the R2 action set (only R7, a recurring-charge match, re-asks — r2_denial is False there).
    if request_type == "customer_validation" and policy.r2_denial(sc) and not sc.flags.get("outcome"):
        return False, {}
    branches: dict[str, list[str]] = {}
    for outcome in OUTCOMES[request_type]:
        post = scorecard.post_evidence(sc, outcome)
        post.flags["asked"] = True
        branches[outcome] = policy.admissible(ctx, post, "final")["required"]
    base = policy.admissible(ctx, sc, "initial")["required"]
    ask = any(set(b) != set(base) for b in branches.values()) and len({tuple(sorted(b)) for b in branches.values()}) > 1
    return ask, branches
