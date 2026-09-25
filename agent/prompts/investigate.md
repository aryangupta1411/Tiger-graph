# Phase P2 — Investigate

You have a budget of at most {max_tool_calls} graph or retrieval calls for the whole investigation; the harness has already run the mandatory set (`case_context`, `card_profile`, `card_window` over the last 72 hours, `prior_cases_for_customer`, `find_similar_cases`, `grounding_chunks`) and their results are in the brief above. Use the remaining calls for the playbook queries that can change the decision, in the order given, and stop as soon as the §6 stop rule is met or a further query could not change the actions. Do not repeat a query with the same parameters. Parameters: `VERTEX` → `{"id": "..."}`, `DATETIME` → `"YYYY-MM-DD HH:MM:SS"`, and never a timestamp later than `opened_at`.

When you are done, reply with a short plain-text note (three to eight sentences) listing the evidence families you found for fraud and for legitimacy, the candidate pattern, which transactions form the candidate episode, and what evidence request (if any) could still change the action set. The harness computes the scorecard next; do not output JSON here.

## Playbook A — `risk_score` trigger, in-person transaction

1. `region_history(card, addr1)` for the flagged region: prior count / days / first / last, the card's modal in-person region, `n_regions`, whether home activity continued within ±48 h, and the hint `home` / `known` / `rare` / `new`.
2. `card_window(card, opened_at − 72 h, opened_at)` for roaming, other high-`cms_p` transactions, any online activity (mixed channel), and the amount against the baseline in the brief.
3. `prior_cases_for_customer` is already in the brief: look for prior out-of-region / account-takeover cases and "confirmed travel" clears.
4. `episode_candidates(txn)` when anything in the window looks related.
5. Travel versus clone for rare / new regions: several consecutive days in the region with home activity absent → trip; a single purchase in a new region while home purchases continue the same day → clone. Roaming cards (dozens of regions) get no region-novelty weight.
6. Pattern if fraud: every in-person member in the modal region → `account_takeover`; otherwise `out_of_region_use`.
7. A card with many prior confirmed fraud cases in this very region, a high-`cms_p` neighbour and a high `cms_p` on the flagged transaction is in the fraud band even in its home region; do not pre-judge it legitimate.

## Playbook B — `risk_score` trigger, online transaction

1. `device_history(card, device)` — prior uses of this profile on the card, first seen, `device_new` values seen.
2. `device_neighbors(device, opened_at − 30 d, opened_at)` **only if the profile is strong** (`device.is_strong` in the brief); a generic profile is never shared-origin evidence.
3. `card_window(card, opened_at − 48 h, opened_at)` — 2–4 online in 48 h, amount against history, first use of product code or email domain, mixed channel.
4. `card_testing_check(card)` — R5 shape; `under_threshold_burst(card)` — the just-under-$500 shape; `recurring_charge_check(txn)` when the amount recurs; `shared_origin_scan(card)` for strong profiles and recipient-email fan-out; `episode_candidates(txn)` for the candidate episode.
5. Pattern if fraud: ring or burst → `undocumented`; card-testing chain → `card_testing`; mixed channel in the chain → `account_takeover`; any `New` member → `card_not_present_new_device`; otherwise `card_not_present_fraud` (no identity record ⇒ device unknown; `Unknown` / empty are neutral).
6. Calibration traps: a high risk score on a home-region purchase from a generic New profile with a prior cleared "confirmed travel" or "new phone" case is legitimate-leaning — expect a low probability and a verification, not a block. A rulebook chain (in-person spree above the prior in-person maximum, then an online purchase from a New device in a never-seen region) with a very low `cms_p` is a **conflict**: the harness makes it `uncertain` with `STEP_UP_AUTH` and R8 escalation; say so.

## Playbook C — `customer_report` trigger (the customer denies the flagged transaction)

1. The denial is evidence #1 (`source: customer`, `ref: trigger`) and the harness floors the probability at 0.55 (no denial was ever cleared in the bank's history) unless the R7 gate fires. The denial is the trigger, so **R2 applies from intake**: README 3b resolves R1 versus R2 — R1 governs *before* a denial, and once the customer denies, the actions become `BLOCK_CARD`, `CREATE_CASE` and possibly `FILE_REPORT`. R7 (the charge matches the customer's own recurring pattern) is the only exception: then do not block.
2. Run `recurring_charge_check(txn)` **first**. R7 gate: ≥ 5 prior purchases within ±1 % of the amount, same product code, same billing region (in person) or purchaser email domain (online), median gap 5–35 days with gap CV ≤ 0.6, `cms_p` < 0.30 and no ≤ 48 h chain member with `cms_p` ≥ 0.5. There is no merchant field: recurrence is inferred from product code, amount, place and cadence, and the claim must say so.
3. Then `episode_candidates(txn)`, `card_window(card, opened_at − 48 h, opened_at)`, `device_history` and, for online, `device_neighbors` (strong profiles only), `card_testing_check`, `under_threshold_burst`, `shared_origin_scan`.
4. R2 (no R7 match) → `CREATE_CASE`, `BLOCK_CARD` (L1 ≤ $2,500 else L2), `FILE_REPORT` (L2) when exposure > $1,000 or the case connects to a shared device profile or another card's fraud (3a), `MONITOR_CONNECTED_CARDS` when a strong shared element exists. Never ask the customer who has just denied the charge to confirm it; a step-up or analyst request is only for the scope (which other transactions belong to the episode).
5. The probability still has to be honest: a denial with a weak graph picture can stay `uncertain` (0.55 ≤ p < 0.70); R8 then adds `ESCALATE_TO_ANALYST` when exposure > $500 or the evidence conflicts. The admissible set the harness gives you is authoritative for the exact actions.
6. A card with thousands of transactions or a very short history makes the scorer unreliable; say so and lean on the graph facts.

## Playbook D — `analyst_request` trigger (an analyst names a shared element)

1. `case_context` (in the brief) → is the flagged transaction on a strong profile, behind a proxy, with `ring_hit`?
2. `device_neighbors(device, month start, opened_at)` → the cards active on the profile before opening (name them); `device_neighbors(device, month start, month end)` → the whole wave (the closed ring cases list every card of the wave, including cards first seen after the case opened, so `connected_card_ids` is wave-scoped while `affected_txn_ids` stays time-boxed).
3. `ring_profile(card)` → the profile-centric ring facts: proxy share, fraud-case count, the card's own transactions on the profile, pre-open vs wave cards.
4. `find_similar_cases` (in the brief) → the closed cases on the same profile; cite their ids and outcomes.
5. Verdict `fraud` with two independent families (device ring + prior undocumented cases) meets §6 test 1 before any request: the initial set is already final — `CREATE_CASE`, `BLOCK_CARD` (L1 when exposure ≤ $2,500; "R6 shared origin confirmed on this card; R1 not applicable"), `FILE_REPORT` (L2, R6 / R9 / 3a), `MONITOR_CONNECTED_CARDS` (R6), `ESCALATE_TO_ANALYST` (R9); no evidence request; `what_changed` is "nothing"; status `escalated`.
6. `affected_txn_ids` = the card's own transactions on the ring profile with `ts ≤ opened_at`; a later purchase is a monitoring note. `connected_device_profiles` = the exact profile string.
