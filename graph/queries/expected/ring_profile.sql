-- ring_profile(c, as_of) — params $c, $as_of  (decision D6 wave semantics, mirrored from ring_profile.gsql)
WITH s AS (SELECT * FROM g_card WHERE id = $c), rid AS (SELECT ring_id FROM s),
     d AS (SELECT * FROM g_dev WHERE id = (SELECT ring_id FROM rid) AND (SELECT ring_id FROM rid) <> ''),
     u AS (SELECT * FROM g_txn WHERE device_id = (SELECT ring_id FROM rid) AND (SELECT ring_id FROM rid) <> ''),
     own AS (SELECT * FROM u WHERE card_id = $c AND ts <= $as_of::TIMESTAMP),
     -- wave anchor: this card's ring transactions <= as_of, or all of them when none precede as_of
     anchor AS (SELECT CASE WHEN (SELECT count(*) FROM own) > 0 THEN (SELECT min(ts) FROM own) ELSE (SELECT min(ts) FROM u WHERE card_id = $c) END - INTERVAL 30 DAY AS lo,
                       CASE WHEN (SELECT count(*) FROM own) > 0 THEN (SELECT max(ts) FROM own) ELSE (SELECT max(ts) FROM u WHERE card_id = $c) END + INTERVAL 30 DAY AS hi),
     w AS (SELECT * FROM u WHERE card_id <> $c AND ts >= (SELECT lo FROM anchor) AND ts <= (SELECT hi FROM anchor))
SELECT (SELECT ring_id FROM rid) AS ring_id,
       coalesce((SELECT struct_pack(id, is_strong, n_cards_alltime, n_cards_30d, n_proxy, n_fraud_cases) FROM d), struct_pack(id := '', is_strong := false, n_cards_alltime := 0, n_cards_30d := 0, n_proxy := 0, n_fraud_cases := 0)) AS device,
       (SELECT coalesce(list(DISTINCT card_id ORDER BY card_id), []) FROM w) AS wave_cards,
       (SELECT coalesce(list(DISTINCT card_id ORDER BY card_id), []) FROM w WHERE ts <= $as_of::TIMESTAMP) AS pre_open_cards,
       coalesce((SELECT n_cards_alltime FROM d), 0) AS n_cards_alltime,
       (SELECT coalesce(list(struct_pack(ts := ts_str, id, amt) ORDER BY ts_str, id), []) FROM own) AS card_txns_on_ring,
       (SELECT coalesce(list(struct_pack(id := case_id, card_id, pattern, outcome, report_filed) ORDER BY case_id), []) FROM (
          SELECT DISTINCT x.case_id, x.card_id, c.pattern, x.outcome, c.report_filed
          FROM g_cc_txn x JOIN g_cc c ON c.id = x.case_id WHERE x.txn_id IN (SELECT id FROM u))) AS closed_cases;
