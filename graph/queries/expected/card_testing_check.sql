-- card_testing_check(c, as_of, small, window_min, min_n, big, lookahead_h) — params $c, $as_of, $small, $window_min, $min_n, $big, $lookahead_h
WITH sm AS (SELECT id, ts, ts_str, amt FROM g_txn WHERE card_id = $c AND ts <= $as_of::TIMESTAMP AND channel = 'online' AND amt <= $small),
     runs AS (SELECT a.id start_id, a.ts start_ts, count(*) n, list(b.id ORDER BY b.ts, b.id::BIGINT) ids, list(b.amt ORDER BY b.ts, b.id::BIGINT) amts,
                     min(b.ts_str) s_ts, max(b.ts_str) e_ts, max(b.ts) end_ts, arg_max(b.id, b.ts) end_id
              FROM sm a JOIN sm b ON b.ts >= a.ts AND (b.ts > a.ts OR b.id::BIGINT >= a.id::BIGINT) AND date_diff('second', a.ts, b.ts) <= $window_min * 60
              GROUP BY 1, 2),
     best AS (SELECT * FROM runs WHERE n >= $min_n ORDER BY n DESC, start_ts DESC LIMIT 1),
     larger AS (SELECT id, amt, ts_str ts FROM g_txn WHERE card_id = $c AND channel = 'online' AND amt > $small AND ts <= $as_of::TIMESTAMP
                AND ts > (SELECT end_ts FROM best) AND ts <= (SELECT end_ts FROM best) + to_hours($lookahead_h::INT) ORDER BY amt DESC LIMIT 1),
     anchor AS (SELECT coalesce((SELECT end_id FROM best), (SELECT id FROM g_txn WHERE card_id = $c AND ts <= $as_of::TIMESTAMP ORDER BY ts DESC, id::BIGINT DESC LIMIT 1)) id,
                       coalesce((SELECT end_ts FROM best), (SELECT max(ts) FROM g_txn WHERE card_id = $c AND ts <= $as_of::TIMESTAMP)) ts),
     ev AS (SELECT id, ts, channel, amt, date_diff('second', lag(ts) OVER (ORDER BY ts, id::BIGINT), ts) gap
            FROM g_txn WHERE card_id = $c AND ts <= $as_of::TIMESTAMP AND ts >= (SELECT ts FROM anchor) - INTERVAL 45 DAY),
     grp AS (SELECT *, sum(CASE WHEN gap IS NULL OR gap > 48*3600 THEN 1 ELSE 0 END) OVER (ORDER BY ts, id::BIGINT) g FROM ev),
     chain AS (SELECT * FROM grp WHERE g = (SELECT g FROM grp WHERE id = (SELECT id FROM anchor)))
SELECT (SELECT coalesce(list(struct_pack(start_id, ids, amts, start_ts := s_ts, end_ts := e_ts)), []) FROM best) AS run,
       coalesce((SELECT struct_pack(amt, id, ts) FROM larger), struct_pack(amt := 0.0, id := '', ts := '')) AS larger_purchase,
       coalesce((SELECT amt > $big FROM larger), false) AS cleared_over_big,
       (SELECT struct_pack(n_members := count(*), n_small := count(*) FILTER (WHERE channel = 'online' AND amt <= $small)) FROM chain) AS chain;
