-- case_context(t, as_of) — DuckDB equivalent (params: $t, $as_of)
WITH f AS (SELECT * FROM g_txn WHERE id = $t)
SELECT
  (SELECT struct_pack(id, ts := ts_str, amt, product_cd, channel, addr1, addr2, p_email, r_email, risk_score, has_identity,
                      device_new, proxy, device_type, device_id, cms_p, card_seq, prior_in_region, prior_on_dev, prior_pem,
                      prior_pcd, prior_med_amt, prior_max_amt, ring_hit, burst_id) FROM f) AS txn,
  (SELECT struct_pack(id, card_type, card_network, modal_region,
                      n_txns := (SELECT count(*) FROM g_txn x WHERE x.card_id = c.id AND x.ts <= $as_of::TIMESTAMP), ring_id)
   FROM g_card c WHERE id = (SELECT card_id FROM f)) AS card,   -- n_txns time-boxed to ts <= as_of (case_context.gsql)
  (SELECT struct_pack(id, n_cards) FROM g_customer WHERE id = (SELECT customer_id FROM f)) AS customer,
  coalesce((SELECT struct_pack(id, is_strong, n_cards_alltime, n_cards_30d, n_proxy, n_fraud_cases) FROM g_dev WHERE id = (SELECT device_id FROM f)),
           struct_pack(id := '', is_strong := false, n_cards_alltime := 0, n_cards_30d := 0, n_proxy := 0, n_fraud_cases := 0)) AS device;
