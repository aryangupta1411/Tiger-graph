"""Case status — PLAN §4.4.

derive(verdict, final_actions, pending) -> open | closed_fraud | closed_legitimate | escalated

  escalated  when ESCALATE_TO_ANALYST is in the final actions (R8 / R9)
  open       when a reply is still pending / inconclusive, or the verdict is uncertain without an escalation
  closed_*   otherwise, by verdict
Invariant: verdict == uncertain ⇒ status ∈ {escalated, open}.
"""
from __future__ import annotations


def derive(verdict: str, final_actions: list[dict], pending: bool) -> str:
    names = {a["action"] for a in final_actions}
    if "ESCALATE_TO_ANALYST" in names:
        return "escalated"
    if pending or verdict == "uncertain":
        return "open"
    if verdict == "fraud":
        return "closed_fraud"
    return "closed_legitimate"
