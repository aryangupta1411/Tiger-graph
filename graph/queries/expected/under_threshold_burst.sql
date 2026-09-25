-- under_threshold_burst(c, as_of) — params $c, $as_of
WITH t AS (SELECT * FROM g_txn WHERE card_id = $c AND ts <= $as_of::TIMESTAMP AND burst_id <> '')
SELECT (SELECT coalesce(list(struct_pack(burst_id, ids, amts, start_ts, end_ts, device_ids, emails, regions) ORDER BY burst_id), []) FROM (
          SELECT burst_id, list(id ORDER BY ts, id::BIGINT) ids, list(amt ORDER BY ts, id::BIGINT) amts, min(ts_str) start_ts, max(ts_str) end_ts,
                 list(DISTINCT device_id) device_ids, list(DISTINCT p_email) emails, list(DISTINCT addr1) regions FROM t GROUP BY 1)) AS bursts,
       (SELECT from_json(burst_lookalike_ids, '["VARCHAR"]') FROM g_card WHERE id = $c) AS lookalike_cards;
