-- recurring_charge_check(t, tol, as_of) — params $t, $tol, $as_of
WITH f AS (SELECT * FROM g_txn WHERE id = $t),
     m AS (SELECT x.*, CASE WHEN x.channel = 'in_person' THEN x.addr1 ELSE x.p_email END place FROM g_txn x, f
           WHERE x.card_id = f.card_id AND x.ts <= $as_of::TIMESTAMP AND x.id <> f.id AND x.product_cd = f.product_cd AND abs(x.amt - f.amt) <= $tol * f.amt),
     g AS (SELECT *, date_diff('second', lag(ts) OVER (PARTITION BY place ORDER BY ts, id::BIGINT), ts) / 86400.0 gap FROM m),
     grp AS (SELECT place, count(*) n,
                    coalesce(list_sort(list(gap) FILTER (WHERE gap IS NOT NULL))[CAST(floor((count(*) - 2) / 2.0) AS INT) + 1], 0) median_gap_days,
                    coalesce(stddev_pop(gap) / nullif(avg(gap), 0), 0) gap_cv,
                    min(ts_str) first_ts, max(ts_str) last_ts
             FROM g GROUP BY 1)
SELECT (SELECT coalesce(list(struct_pack(place, n, median_gap_days := round(median_gap_days, 4), gap_cv := round(gap_cv, 4), first_ts, last_ts) ORDER BY place), []) FROM grp) AS groups,
       (SELECT coalesce(sum(n), 0) FROM grp) AS total_n;
