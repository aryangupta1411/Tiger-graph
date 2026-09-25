# Phase {phase} — Next best action ({stage}) (structured output: ActionChoice)

Choose the recommendation for this stage from the admissible set below. The policy gate computed it from the current scorecard and the Fraud Policy; you may not add a forbidden action or omit a required one. Give each action its route exactly as listed and a one-sentence reason that **starts with the Fraud Policy rule given for that action in RULE TO CITE** (for example `R2: customer denied; exposure $166.97 ≤ $2,500`, `3a: exposure $1,906.07 > $1,000 and undocumented pattern`, `R8: evidence conflicts (recurrence vs. the card's fraud history in region 126.0)`). If the admissible set's `citations` or `fired` lists show a policy-gate label (`fraud_band`, `uncertain_initial`, `uncertain_final`, `legit_band`, `3a_case`, ...), never write it in a reason — RULE TO CITE gives the README rule it implements. Cite R1 only when the probability is below 0.70 with a single evidence family; when a verification is still asked at 0.70 ≤ p < 0.85 cite §3b / §5. A reason describes an L1 / L2 action as recommended (it waits for approval), never as done. Order the actions by what happens first.

Optional actions from the `allowed` list should be added only when a cited claim supports them (for example `MONITOR_CARD` while a reply is pending, `GENERATE_REPORT` when no case was opened and p < 0.30, `WARN_CUSTOMER` under R7).

Stage: {stage}
Probability now: {p} (verdict {verdict}, families for fraud: {families_fraud}; for legitimacy: {families_legit}; exposure ${exposure})
{post_evidence_note}

RULE TO CITE (per allowed action):
{rule_hints}

ADMISSIBLE SET:
{admissible_json}
