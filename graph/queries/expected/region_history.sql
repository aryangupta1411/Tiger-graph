-- region_history(c, addr1, as_of) — params $c, $addr1, $as_of
WITH t AS (SELECT * FROM g_txn WHERE card_id = $c AND ts <= $as_of::TIMESTAMP AND addr1 <> ''),
     modal AS (SELECT modal_region FROM g_card WHERE id = $c),
     r AS (SELECT count(*) prior_n, count(DISTINCT ts::DATE) prior_days,
                  coalesce(strftime(min(ts), '%Y-%m-%d %H:%M:%S'), '') first_ts, coalesce(strftime(max(ts), '%Y-%m-%d %H:%M:%S'), '') last_ts
           FROM t WHERE addr1 = $addr1),
     tot AS (SELECT count(*) total_n, count(DISTINCT addr1) n_regions FROM t),
     h48 AS (SELECT count(*) FILTER (WHERE addr1 = (SELECT modal_region FROM modal)) n_home,
                    count(*) FILTER (WHERE addr1 <> (SELECT modal_region FROM modal)) n_other
             FROM t WHERE ts >= $as_of::TIMESTAMP - INTERVAL 48 HOUR)
SELECT
  (SELECT struct_pack(addr1 := $addr1, prior_n, prior_days, first_ts, last_ts, share := CASE WHEN (SELECT total_n FROM tot) > 0 THEN round(prior_n * 1.0 / (SELECT total_n FROM tot), 4) ELSE 0 END) FROM r) AS region,
  (SELECT modal_region FROM modal) AS modal_region,
  (SELECT n_regions FROM tot) AS n_regions,
  (SELECT struct_pack(n_home, n_other) FROM h48) AS home_activity_48h,
  (SELECT CASE WHEN $addr1 = (SELECT modal_region FROM modal) AND (SELECT modal_region FROM modal) <> '' THEN 'home'
               WHEN prior_days >= 3 OR prior_n >= 5 THEN 'known' WHEN prior_n >= 1 THEN 'rare' ELSE 'new' END FROM r) AS hint;
