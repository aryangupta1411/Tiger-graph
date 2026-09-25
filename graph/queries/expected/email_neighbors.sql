-- email_neighbors(e, c, from_ts, to_ts) — params $e, $c, $from_ts, $to_ts
WITH w AS (SELECT * FROM g_txn WHERE r_email = $e AND card_id <> $c AND ts >= $from_ts::TIMESTAMP AND ts <= $to_ts::TIMESTAMP),
     cards AS (SELECT card_id, count(*) n_txns FROM w GROUP BY 1 ORDER BY n_txns DESC, card_id LIMIT 60)
SELECT (SELECT coalesce(list(struct_pack(card_id, n_txns) ORDER BY n_txns DESC, card_id), []) FROM cards) AS cards,
       (SELECT coalesce(list(struct_pack(id, card_id, pattern) ORDER BY id), []) FROM (SELECT * FROM g_cc WHERE card_id IN (SELECT card_id FROM w) ORDER BY id LIMIT 60)) AS closed_cases;
