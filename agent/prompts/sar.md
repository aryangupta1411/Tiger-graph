# Phase P8 — Suspicious activity report narrative (structured output: SarDraft)

The policy gate decided that a report must be filed ({sar_reason}). Write the narrative a regulator will read. It must stand on its own: **who** (customer, cards, device profiles), **what** happened, **when** (dates), **where** (billing regions, channels), **how** it was carried out, and **why** it is suspicious. Six to twelve sentences, one paragraph, plain prose, no headings, lists, tables, tabs or line breaks, and never "see attached".

Use only the facts in CASE DATA below; every amount, date, id and count you write must appear there. In particular:

- **Status of each action.** CASE DATA `actions` gives each action with its route and `status`. Only actions whose status says `done` have happened. Every L1 / L2 action (a card block, a decline, the report itself) is **recommended and awaiting approval**: write "a block of card {card_id} is recommended and awaits team-lead approval", never "the bank blocked the card", "the card was blocked" or "card blocked".
- **Card statistics are as of the case opening.** Use only `baseline_as_of_opening` (counts and amounts before the flagged transaction). Do not write any other total, lifetime count, maximum or median for the card; there is none that is time-boxed.
- **Prior reports.** State the prior-report status exactly as `prior_reports` gives it, for this card and, separately, for the device profile or connected cards. Do not write "no prior report" for a scope where `prior_reports` lists a filed report.
- **No invented attributes.** The data has no names, genders, merchants, addresses or time zones: refer to "the customer" / "the cardholder" (never he, she, his or her), write dates as YYYY-MM-DD and times as HH:MM with no time zone, and never name a merchant.
- **Subjects.** `subjects` = every customer id, card id and full device-profile string you name in the narrative (exact strings, no others). If you name a device profile, write the full string from CASE DATA.
- Reserve "On YYYY-MM-DD" for transactions; write the opening date as "opened YYYY-MM-DD". Do not mention timestamp seconds, model internals, simulations or assumptions; describe a customer reply as the customer's statement.

Follow this skeleton, one or two sentences each:

S1. Typology and the internal case id ({graph_case_id}).
S2. Who: customer {customer_id}, card {card_id}, the device profile(s) and any connected cards.
S3–S5. What / when / how, chronologically: each affected transaction with date, time, amount, channel, region and device; the shared element and how the graph connects it to other cards or prior cases.
S6. Why unusual against the cardholder's as-of baseline.
S7. Prior reports ({prior_sar_note}) and the OFAC screening result ({ofac_note}).
S8. Actions: what was done (case opened, monitoring) and what is recommended pending approval (card block, this filing), and the total suspicious amount ${exposure}.

FinCEN guidance retrieved for this report:
{chunks}

Case data:
{case_json}
