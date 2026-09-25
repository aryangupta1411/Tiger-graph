-- _model.sql — DuckDB mirror of the FraudGraph attributes the queries read.
-- Built once by run_expected.py into _model.duckdb from the ETL DuckDB (data/hhgoa.duckdb, read-only): the
-- tables etl/features.py, etl/parse_closed_cases.py and etl/cms_train.py write (txc + txn_feat, card_feat,
-- device_profile, closed_case_parsed, closed_case_txn, cp). Nothing is re-derived here (decision D7: the ETL is
-- the only derivation pipeline and the only scorer) — the graph is loaded from the same tables through
-- etl/export_graph_csvs.py, so every expectation file is what the installed query must print on the loaded graph.
-- Empty-string / -1 conventions follow the export (STR = coalesce(x, ''), missing floats -1).

CREATE OR REPLACE TABLE g_txn AS
SELECT t.TransactionID::VARCHAR                     AS id,
       t.card_id, t.customer_id, t.ts, t.ts_str, t.amt, t.product_cd, t.channel,
       coalesce(t.addr1, '')                        AS addr1,
       coalesce(t.addr2, '')                        AS addr2,
       coalesce(t.dist1, -1)                        AS dist1,
       coalesce(t.dist2, -1)                        AS dist2,
       coalesce(t.p_email, '')                      AS p_email,
       coalesce(t.r_email, '')                      AS r_email,
       t.risk_score, t.has_identity,
       coalesce(t.device_new, '')                   AS device_new,
       coalesce(t.proxy, '')                        AS proxy,        -- graph attribute proxy_type, printed as `proxy`
       coalesce(t.device_type, '')                  AS device_type,
       coalesce(t.device_id, '')                    AS device_id,    -- '' for all-NULL identity rows (no FROM_DEVICE edge)
       t.card4, t.card6,
       round(f.cms_p, 6)                            AS cms_p,        -- ETL scorer (etl/cms_train.py), exported with 6 decimals
       f.card_seq, f.prior_in_region, f.prior_on_dev, f.prior_pem, f.prior_pcd,
       coalesce(f.prior_med_amt, -1)                AS prior_med_amt,
       coalesce(f.prior_max_amt, -1)                AS prior_max_amt,
       f.ring_hit,
       coalesce(f.burst_id, '')                     AS burst_id,
       f.gap_seconds
FROM hh.txc t JOIN hh.txn_feat f USING (TransactionID);

-- closed cases: parsed rows (template_id, note_* and embed_text come from etl/parse_closed_cases.py) + the raw id lists
CREATE OR REPLACE TABLE g_cc AS
SELECT p.id, p.customer_id, p.card_id, p.opened_at, p.closed_at, p.outcome, p.pattern, p.first_fraud_txn_id,
       p.n_txns::INT AS n_txns, p.exposure_usd::DOUBLE AS exposure_usd, p.actions_taken, p.report_filed::BOOLEAN AS report_filed,
       p.analyst_notes, p.template_id, p.note_region, p.note_device, p.note_evidence_type, p.embed_text,
       string_split(r.txn_ids, '|') AS txn_ids,
       CASE WHEN coalesce(r.connected_card_ids, '') = '' THEN [] ELSE string_split(r.connected_card_ids, '|') END AS connected_card_ids
FROM hh.closed_case_parsed p JOIN hh.cc r ON r.case_id = p.id;
CREATE OR REPLACE TABLE g_cc_txn AS
SELECT x.case_id, c.card_id, c.outcome, x.TransactionID::VARCHAR AS txn_id
FROM hh.closed_case_txn x JOIN g_cc c ON c.id = x.case_id;

-- device profiles exactly as loaded (schema.md is_strong rule applied by the ETL)
CREATE OR REPLACE TABLE g_dev AS SELECT * FROM hh.device_profile;

-- cards exactly as loaded (every Card attribute incl. the JSON strings, ring_id, burst_lookalike_ids, region_cluster_30d)
CREATE OR REPLACE TABLE g_card AS SELECT * FROM hh.card_feat;

CREATE OR REPLACE TABLE g_customer AS
SELECT customer_id AS id, count(DISTINCT card_id) n_cards, count(*) n_txns FROM g_txn GROUP BY 1;
CREATE OR REPLACE TABLE g_cp AS
SELECT case_id, opened_at::TIMESTAMP opened_at, trigger_type, flagged_txn_id, card_id, customer_id FROM hh.cp;
