-- episode_candidates(t, as_of, gap_h) — params $t, $as_of, $gap_h
-- Bounds = the GSQL = engine/facts_duckdb.py DuckFacts._chain: rows within +-7 days of t (and ts <= as_of); a chain of
-- more than 200 members keeps the 100 rows before t and the 100 after it.
WITH f AS (SELECT t.*, coalesce((SELECT is_strong FROM g_dev d WHERE d.id = t.device_id), false) strong FROM g_txn t WHERE id = $t),
     x AS (SELECT x.*, date_diff('second', lag(x.ts) OVER (ORDER BY x.ts, x.id::BIGINT), x.ts) gap,
                  ((f.strong AND x.device_id <> '' AND x.device_id = f.device_id)
                   OR (x.p_email <> '' AND x.p_email = f.p_email AND x.product_cd = f.product_cd AND abs(x.amt - f.amt) <= 0.05 * f.amt
                       AND abs(date_diff('second', f.ts, x.ts)) <= 7200)) sig_match,
                  row_number() OVER (ORDER BY x.ts, x.id::BIGINT) rn
           FROM g_txn x, f WHERE x.card_id = f.card_id AND x.ts <= $as_of::TIMESTAMP
                               AND x.ts >= f.ts - INTERVAL 7 DAY AND x.ts <= f.ts + INTERVAL 7 DAY),
     grp AS (SELECT *, sum(CASE WHEN gap IS NULL OR gap > $gap_h * 3600 THEN 1 ELSE 0 END) OVER (ORDER BY ts, id::BIGINT) g FROM x),
     ai AS (SELECT rn FROM grp WHERE id = $t),
     chain AS (SELECT * FROM grp WHERE g = (SELECT g FROM grp WHERE id = $t)),
     bounds AS (SELECT min(rn) lo, max(rn) hi, count(*) n FROM chain),
     win AS (SELECT * FROM chain WHERE (SELECT n FROM bounds) <= 200 OR (rn >= (SELECT rn FROM ai) - 100 AND rn <= (SELECT rn FROM ai) + 100))
SELECT (SELECT coalesce(list(struct_pack(id, ts := ts_str, amt, product_cd, channel, addr1, p_email, device_id, device_new, cms_p, sig_match) ORDER BY ts_str, id), []) FROM win) AS chain,
       (SELECT n FROM bounds) AS _chain_len;
