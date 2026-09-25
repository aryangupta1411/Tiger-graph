-- device_history(c, d, as_of) — params $c, $d, $as_of
WITH t AS (SELECT * FROM g_txn WHERE card_id = $c AND ts <= $as_of::TIMESTAMP AND device_id = $d)
SELECT count(*) AS prior_n, coalesce(strftime(min(ts), '%Y-%m-%d %H:%M:%S'), '') AS first_ts,
       coalesce(list(DISTINCT device_new), []) AS device_new_values FROM t;
