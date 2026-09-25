# Phase P9 — Explain (structured output: Closing)

Write the case summary an analyst reads first. The README asks for two to six sentences and says: keep the summary short, the evidence list carries the detail. Write **two to four sentences, under 550 characters**: the verdict and probability, the pattern (in your own words if undocumented), the episode and exposure, the one or two decisive facts, what was asked and assumed (if anything), and the recommended action with its approval route. Do not restate the stop reason, list every action, or repeat the evidence list. If the case could reasonably be read the other way (for example dispute-as-fraud versus recurring charge), give the alternative reading in one clause. Name a prior case (`CC-2649`) only when it decided the outcome.

Facts to respect: an L1 / L2 action (a card block, a decline, a report) is recommended and awaits approval — never write that it was done. `affected_txn_ids` is the engine's candidate episode and `exposure_usd` is its sum: never describe a member of it as normal or legitimate use (if some members look ordinary, say the episode is scoped by the chain rule and that is why exposure includes them). Card statistics come only from as-of-opening facts; never quote a card's lifetime totals.

`stop_reason` must name the policy §6 test that fired, in one or two sentences; the harness's own determination is: "{engine_stop_reason}". Keep that meaning; you may make it more specific.

`similar_prior_cases_used`: the `CC-` ids you actually relied on (3–6, from the retrieved list only), or empty.

Final case state:
{final_json}
