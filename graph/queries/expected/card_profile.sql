-- card_profile(c, as_of) — DuckDB equivalent (params: $c, $as_of). g_card is the ETL card_feat table, so card{}
-- carries every Card attribute incl. the JSON strings (known_device_ids capped at 200, recurring_amounts at 50).
-- The history fields (n_txns, first_ts, last_ts, median_amt, p90_amt, max_amt, max_in_person_amt, n_online, n_in_person,
-- n_regions, n_devices_seen) are recomputed from the card's transactions ts <= as_of (card_profile.gsql header).
WITH s AS (SELECT * FROM g_card WHERE id = $c),
     t AS (SELECT * FROM g_txn WHERE card_id = $c AND ts <= $as_of::TIMESTAMP),
     l30 AS (SELECT * FROM t WHERE ts > $as_of::TIMESTAMP - INTERVAL 30 DAY),
     h AS (SELECT count(*) n_txns, coalesce(strftime(min(ts), '%Y-%m-%d %H:%M:%S'), '') first_ts,
                  coalesce(strftime(max(ts), '%Y-%m-%d %H:%M:%S'), '') last_ts,
                  coalesce(quantile_cont(amt, 0.5), 0) median_amt, coalesce(quantile_cont(amt, 0.9), 0) p90_amt, coalesce(max(amt), 0) max_amt,
                  coalesce(max(amt) FILTER (WHERE channel = 'in_person'), -1) max_in_person_amt,
                  count(*) FILTER (WHERE channel = 'online') n_online, count(*) FILTER (WHERE channel = 'in_person') n_in_person,
                  count(DISTINCT addr1) FILTER (WHERE addr1 <> '') n_regions,
                  count(DISTINCT device_id) FILTER (WHERE device_id <> '') n_devices_seen FROM t)
SELECT
  (SELECT struct_pack(id := s.id, customer_id := s.customer_id, card_type := s.card_type, card_network := s.card_network,
                      n_txns := h.n_txns, first_ts := h.first_ts, last_ts := h.last_ts, median_amt := h.median_amt, p90_amt := h.p90_amt,
                      max_amt := h.max_amt, max_in_person_amt := h.max_in_person_amt, n_online := h.n_online, n_in_person := h.n_in_person,
                      modal_region := s.modal_region, n_regions := h.n_regions, home_regions := s.home_regions, usual_products := s.usual_products,
                      usual_p_email := s.usual_p_email, n_devices_seen := h.n_devices_seen, known_device_ids := s.known_device_ids,
                      recurring_amounts := s.recurring_amounts, n_prior_fraud_cases := s.n_prior_fraud_cases,
                      n_prior_cleared_cases := s.n_prior_cleared_cases, ring_id := s.ring_id, burst_lookalike_ids := s.burst_lookalike_ids,
                      region_cluster_30d := s.region_cluster_30d, wcc_id := s.wcc_id, device_degree := s.device_degree,
                      fraud_ppr := s.fraud_ppr::DOUBLE) FROM s, h) AS card,
  (SELECT struct_pack(n := count(*), sum_amt := round(coalesce(sum(amt), 0), 2), n_online := count(*) FILTER (WHERE channel = 'online'),
                      n_regions := count(DISTINCT addr1) FILTER (WHERE addr1 <> ''), n_devices := count(DISTINCT device_id) FILTER (WHERE device_id <> ''),
                      n_products := count(DISTINCT product_cd)) FROM l30) AS last30,
  (SELECT list(struct_pack(addr1, n, "days", first_ts, last_ts) ORDER BY n DESC, addr1) FROM (
     SELECT addr1, count(*) n, count(DISTINCT ts::DATE) AS "days", strftime(min(ts), '%Y-%m-%d %H:%M:%S') first_ts, strftime(max(ts), '%Y-%m-%d %H:%M:%S') last_ts
     FROM t WHERE addr1 <> '' GROUP BY 1 ORDER BY n DESC, addr1 LIMIT 5)) AS regions,
  (SELECT list(struct_pack(device_id, n, first_ts, is_strong) ORDER BY n DESC, device_id) FROM (
     SELECT t.device_id, count(*) n, strftime(min(t.ts), '%Y-%m-%d %H:%M:%S') first_ts, any_value(d.is_strong) is_strong
     FROM t JOIN g_dev d ON d.id = t.device_id GROUP BY 1 ORDER BY n DESC, device_id LIMIT 5)) AS devices,
  (SELECT list(struct_pack("domain", n) ORDER BY n DESC, "domain") FROM (
     SELECT p_email AS "domain", count(*) n FROM t WHERE p_email <> '' GROUP BY 1 ORDER BY n DESC, "domain" LIMIT 10)) AS emails,
  (SELECT coalesce(list(struct_pack(id, card_type, n_txns)), []) FROM g_card WHERE customer_id = (SELECT customer_id FROM s) AND id <> $c) AS other_cards,
  (SELECT list(struct_pack(id, opened_at := strftime(opened_at, '%Y-%m-%d %H:%M:%S'), outcome, pattern, exposure_usd, report_filed, template_id) ORDER BY opened_at DESC, id) FROM (
     SELECT * FROM g_cc WHERE card_id = $c AND opened_at <= $as_of::TIMESTAMP ORDER BY opened_at DESC, id LIMIT 30)) AS prior_closed_cases,
  [] AS prior_agent_cases;
