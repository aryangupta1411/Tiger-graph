-- card_window(c, from_ts, to_ts, max_rows) — params $c, $from_ts, $to_ts, $max_rows
WITH seq AS (SELECT *, coalesce(date_diff('second', lag(ts) OVER (PARTITION BY card_id ORDER BY ts, id::BIGINT), ts), -1) gap_seconds
             FROM g_txn WHERE card_id = $c),
     w AS (SELECT * FROM seq WHERE ts >= $from_ts::TIMESTAMP AND ts <= $to_ts::TIMESTAMP)
SELECT
  (SELECT list(struct_pack(id, ts := ts_str, amt, product_cd, channel, addr1, p_email, r_email, risk_score, cms_p, device_id, device_new, proxy, gap_seconds) ORDER BY ts_str, id)
     FROM (SELECT * FROM w ORDER BY ts_str DESC, id DESC LIMIT $max_rows)) AS txns,
  (SELECT struct_pack(n := count(*), sum_amt := round(coalesce(sum(amt), 0), 2), n_online := count(*) FILTER (WHERE channel = 'online'),
                      n_in_person := count(*) FILTER (WHERE channel = 'in_person')) FROM w) AS summary;
