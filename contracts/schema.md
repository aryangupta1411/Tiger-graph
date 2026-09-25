# Contract — graph schema

*Authoritative contract. A module may not rename anything here without updating this file.*

Graph name: `FraudGraph`. All ids are STRING primary keys with `primary_id_as_attribute="true"`. No vertex/edge/attribute name may be a GSQL reserved word (`Case`, `Order`, `Group`, `Type`, `Index`, `Key`, `Date`, `Time` are avoided). Datetimes are `DATETIME` loaded from `YYYY-MM-DD HH:MM:SS` strings; a `*_str` copy keeps the original text where the answer file needs it. Missing strings load as `""`, missing numbers as `-1` unless stated.

### Vertices

| Vertex | PRIMARY_ID | Attributes (name TYPE) |
|---|---|---|
| `Customer` | `id` (`C01234`) | `n_cards INT, n_txns INT, n_closed_cases INT` |
| `Card` | `id` (`C01234-K1`) | `customer_id STRING, card_type STRING, card_network STRING, n_txns INT, first_ts DATETIME, last_ts DATETIME, median_amt FLOAT, p90_amt FLOAT, max_amt FLOAT, max_in_person_amt FLOAT, n_online INT, n_in_person INT, modal_region STRING, n_regions INT, home_regions STRING, usual_products STRING, usual_p_email STRING, n_devices_seen INT, known_device_ids STRING, recurring_amounts STRING, n_prior_fraud_cases INT, n_prior_cleared_cases INT, ring_id STRING, burst_lookalike_ids STRING, region_cluster_30d STRING, wcc_id INT, device_degree INT, fraud_ppr FLOAT` (JSON strings for `home_regions`, `usual_products`, `known_device_ids`, `recurring_amounts`, `burst_lookalike_ids`, `region_cluster_30d`) |
| `Transaction` | `id` (`3514030`) | `card_id STRING, customer_id STRING, ts DATETIME, ts_str STRING, amt FLOAT, product_cd STRING, channel STRING, addr1 STRING, addr2 STRING, dist1 FLOAT, dist2 FLOAT, p_email STRING, r_email STRING, risk_score FLOAT, has_identity BOOL, device_new STRING, proxy_type STRING, device_type STRING, device_id STRING, c_counts STRING, d_deltas STRING, m_flags STRING, cms_p FLOAT, card_seq INT, prior_in_region INT, prior_on_dev INT, prior_pem INT, prior_pcd INT, prior_med_amt FLOAT, prior_max_amt FLOAT, ring_hit BOOL, burst_id STRING` (`device_new` ∈ {`New`,`Found`,`Unknown`,`""`}; `proxy_type` = id_23 or `""`, **printed by every query under the contract key `proxy`** — `PROXY` is a 4.2 DDL reserved word, so the stored attribute is `proxy_type` (M1/D8); `device_id` = the DeviceProfile id or `""`) |
| `DeviceProfile` | `id` (`DeviceInfo \| id_30 \| id_31 \| id_33`, missing parts literally `NULL`) | `device_info STRING, os STRING, browser STRING, screen STRING, n_txns INT, n_cards_alltime INT, n_cards_30d INT, n_proxy INT, first_seen DATETIME, last_seen DATETIME, n_fraud_cases INT, is_strong BOOL` |
| `EmailDomain` | `id` (`hotmail.com`) | `n_txns INT, n_cards INT` |
| `BillingRegion` | `id` (`444.0`, raw string) | `country STRING, n_txns INT, n_cards INT` |
| `ClosedCase` | `id` (`CC-0001`) | `customer_id STRING, card_id STRING, opened_at DATETIME, closed_at DATETIME, outcome STRING, pattern STRING, first_fraud_txn_id STRING, n_txns INT, exposure_usd FLOAT, actions_taken STRING, report_filed BOOL, analyst_notes STRING, template_id STRING, note_region STRING, note_device STRING, note_evidence_type STRING, embed_text STRING` + VECTOR `note_emb(DIMENSION=1024, METRIC="COSINE")` |
| `AgentCase` | `id` (`AC-HHG-001`) | `source_case_id STRING, customer_id STRING, card_id STRING, trigger_type STRING, opened_at DATETIME, updated_at DATETIME, status STRING, verdict STRING, fraud_probability FLOAT, pattern STRING, pattern_description STRING, exposure_usd FLOAT, summary STRING, sar_filed BOOL, sar_narrative STRING, initial_actions STRING, final_actions STRING, what_changed STRING, stop_reason STRING, run_id STRING` + VECTOR `note_emb(DIMENSION=1024, METRIC="COSINE")` |
| `CaseEvent` | `id` (`AC-HHG-001-007`) | `case_id STRING, seq INT, at DATETIME, kind STRING, payload STRING` (`kind` ∈ evidence, assessment, nba_initial, evidence_request, assumed_response, nba_final, approval, sar, policy_override, executed) |
| `Approval` | `id` (`AP-AC-HHG-014-FILE_REPORT`) | `case_id STRING, action STRING, route STRING, status STRING, decided_by STRING, decided_at DATETIME, reason STRING` |
| `Document` | `id` (`sar_guidance`) | `title STRING, url STRING, sha256 STRING, kind STRING` |
| `PolicyChunk` | `id` (`sar_guidance#p04-when`) | `doc_id STRING, section STRING, page INT, kind STRING, text STRING` + VECTOR `emb(DIMENSION=1024, METRIC="COSINE")` (`kind` ∈ policy, pattern, regulation) |
| `FraudPattern` | `id` (`card_testing` … `undocumented`, `none`) | `description STRING, rule_refs STRING` |

### Edges (directed ones `WITH REVERSE_EDGE="REV_<NAME>"`)

| Edge | FROM → TO | Attributes |
|---|---|---|
| `OWNS` | Customer → Card | — |
| `MADE` | Card → Transaction | — |
| `NEXT` | Transaction → Transaction | `gap_seconds INT` (order by ts, TransactionID tiebreak) |
| `FROM_DEVICE` | Transaction → DeviceProfile | `device_new STRING, proxy_type STRING` (printed as `proxy`; no edge for all-NULL identity rows) |
| `PURCHASER_EMAIL` | Transaction → EmailDomain | — |
| `RECIPIENT_EMAIL` | Transaction → EmailDomain | — |
| `BILLED_IN` | Transaction → BillingRegion | — |
| `INVOLVES` | ClosedCase → Transaction | — |
| `ON_CARD` | ClosedCase → Card | — |
| `CONNECTED_TO` | ClosedCase → Card | — |
| `MATCHES` | ClosedCase → FraudPattern | — |
| `CASE_INVOLVES` | AgentCase → Transaction | — |
| `CASE_ON_CARD` | AgentCase → Card | — |
| `CASE_CONNECTED_TO` | AgentCase → Card | — |
| `CASE_DEVICE` | AgentCase → DeviceProfile | — |
| `CASE_SIMILAR_TO` | AgentCase → ClosedCase | `score FLOAT` |
| `CASE_SIMILAR_TO_AGENT` | AgentCase → AgentCase | `score FLOAT` |
| `CASE_CITES` | AgentCase → PolicyChunk | — |
| `CASE_MATCHES` | AgentCase → FraudPattern | — |
| `HAS_EVENT` | AgentCase → CaseEvent | — |
| `HAS_APPROVAL` | AgentCase → Approval | — |
| `ABOUT` | PolicyChunk → FraudPattern | — |
| `CHUNK_OF` | PolicyChunk → Document | — |
| `SHARES_DEVICE` (UNDIRECTED) | Card — Card | `n_shared INT, weight FLOAT, via STRING` |

### Derivation rules (ETL, authoritative)

- `card_id = customer_id || '-K' || dense_rank() OVER (PARTITION BY customer_id ORDER BY coalesce(card6,''))` computed over DISTINCT (customer_id, card6). `card_type = coalesce(card6,'unknown')`, `card_network = coalesce(card4,'unknown')`.
- `DeviceProfile.id = coalesce(DeviceInfo,'NULL') || ' | ' || coalesce(id_30,'NULL') || ' | ' || coalesce(id_31,'NULL') || ' | ' || coalesce(id_33,'NULL')`; rows with all four NULL get no `FROM_DEVICE` edge and `Transaction.device_id = ""`.
- `is_strong = device_info != 'NULL' AND (number of non-NULL fields ≥ 3) AND n_cards_alltime ≤ 60 AND max over 30-day windows of distinct cards ∈ [2, 40]`.
- `Card.ring_id` = id of a strong DeviceProfile on the card's online transactions that has `n_fraud_cases ≥ 2` OR proxy share (`n_proxy / n_txns`) ≥ 0.9; else `""`. Ties are broken by **anonymous-proxy share ≥ 0.9 first**, then `n_fraud_cases`, then the card's transaction count on the profile, then the profile id. The rule is deliberately broad — it fires on **1,157** cards, while the exact ring profile carries **52**; the ring that matters is identified by the engine's strict anonymous-proxy test (`engine.scorecard._ring`), not by `ring_id` alone (open issue O10). `Transaction.ring_hit = (device_id == card.ring_id AND device_id != "")`.
- `Transaction.burst_id` = `card_id || '#' || first TransactionID of the burst` when the transaction belongs to a run of ≥ 4 online transactions with `450 ≤ amt ≤ 499.99` inside any 40-minute window on the card; else `""`. `Card.burst_lookalike_ids` = JSON list of other cards with a burst within ±30 days of the card's own burst.
- `Card.modal_region` = the most frequent `addr1` among the card's **in-person** transactions (ties → earliest seen); for `C13487-K1` it is `272.0`. `Card.region_cluster_30d` = JSON list of `{addr1, n_txns_card, n_other_cards_30d, n_cards_with_fraud_case_30d}` for regions the card used in its last 30 days (`n_cards_with_fraud_case_30d` is 0 for every Nov–Dec window in this dataset — open issue O11).
- `Card.recurring_amounts` = JSON list of `{amt, product_cd, place, n, median_gap_days, gap_cv}` for (amount ±1 %, product, addr1 or p_email) groups with n ≥ 5, **capped at 50 groups** (n desc, then amt). `Card.known_device_ids` is **capped at 200 ids** (n desc, then first_seen, then id).
- `Transaction.card_seq` = 1-based position of the transaction on its card by (ts, TransactionID); `prior_*` = counts/aggregates over the card's earlier transactions only (no leakage).
- `ClosedCase.embed_text` = template-normalised note with amounts, scores, ids and dates masked, prefixed by `pattern: X | outcome: Y | device: Z | region: R`.
- `AgentCase.id = 'AC-' || case_id`; `CaseEvent.id = case_id || '-' || lpad(seq,3,'0')`.


---
