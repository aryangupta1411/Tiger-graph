-- shared_origin_scan(c, as_of, days) — params $c, $as_of, $days
WITH t AS (SELECT * FROM g_txn WHERE card_id = $c AND channel = 'online' AND ts > $as_of::TIMESTAMP - to_days($days::INT) AND ts <= $as_of::TIMESTAMP),
     devs AS (SELECT t.device_id, any_value(d.is_strong) is_strong, any_value(d.n_cards_30d) n_cards_30d, any_value(d.n_fraud_cases) n_fraud_cases, count(*) n_txns_on_card
              FROM t JOIN g_dev d ON d.id = t.device_id GROUP BY 1 ORDER BY n_txns_on_card DESC, device_id LIMIT 20),
     doms AS (SELECT DISTINCT r_email AS "domain" FROM t WHERE r_email <> ''),
     em AS (SELECT d."domain" AS "domain",
                   (SELECT count(DISTINCT card_id) FROM g_txn u WHERE u.r_email = d."domain" AND u.card_id <> $c AND u.ts > $as_of::TIMESTAMP - INTERVAL 30 DAY AND u.ts <= $as_of::TIMESTAMP) n_cards_30d,
                   (SELECT count(DISTINCT x.case_id) FROM g_txn u JOIN g_cc_txn x ON x.txn_id = u.id AND x.outcome = 'confirmed_fraud' WHERE u.r_email = d."domain" AND u.card_id <> $c AND u.ts > $as_of::TIMESTAMP - INTERVAL 30 DAY AND u.ts <= $as_of::TIMESTAMP) n_fraud_cases
            FROM doms d ORDER BY n_cards_30d DESC, "domain" LIMIT 50)  -- 30-day window, other cards, up to 50: engine/facts_duckdb.py + the GSQL
SELECT (SELECT coalesce(list(struct_pack(device_id, is_strong, n_cards_30d, n_fraud_cases, n_txns_on_card) ORDER BY n_txns_on_card DESC, device_id), []) FROM devs) AS devices,
       (SELECT coalesce(list(struct_pack("domain", n_cards_30d, n_fraud_cases) ORDER BY n_cards_30d DESC, "domain"), []) FROM em) AS recipient_emails,
       (SELECT region_cluster_30d FROM g_card WHERE id = $c) AS region_cluster_30d;
