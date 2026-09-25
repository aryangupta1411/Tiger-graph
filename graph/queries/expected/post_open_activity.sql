-- post_open_activity(c, opened_at, days) — params $c, $opened_at, $days
WITH w AS (SELECT * FROM g_txn WHERE card_id = $c AND ts > $opened_at::TIMESTAMP AND ts <= $opened_at::TIMESTAMP + to_days($days::INT))
SELECT (SELECT coalesce(list(struct_pack(ts := ts_str, id, amt, channel, addr1, device_new, cms_p) ORDER BY ts_str, id), []) FROM (SELECT * FROM w ORDER BY ts, id::BIGINT LIMIT 100)) AS txns,
       (SELECT struct_pack(n := count(*), sum_amt := round(coalesce(sum(amt), 0), 2)) FROM w) AS summary;
