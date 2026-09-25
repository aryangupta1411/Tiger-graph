-- case_subgraph(c, as_of, hours) — params $c, $as_of, $hours (counts only; the UI consumes the live graph)
WITH t AS (SELECT * FROM g_txn WHERE card_id = $c AND ts > $as_of::TIMESTAMP - to_hours($hours::INT) AND ts <= $as_of::TIMESTAMP ORDER BY ts DESC, id::BIGINT DESC LIMIT 60)
SELECT 2 + (SELECT count(*) FROM t) + (SELECT count(DISTINCT device_id) FROM t WHERE device_id <> '') + (SELECT count(DISTINCT p_email) FROM t WHERE p_email <> '')
         + (SELECT count(DISTINCT addr1) FROM t WHERE addr1 <> '') + (SELECT count(*) FROM g_cc WHERE card_id = $c AND opened_at <= $as_of::TIMESTAMP) AS n_nodes,
       1 + (SELECT count(*) FROM t) + (SELECT count(*) FROM t WHERE device_id <> '') + (SELECT count(*) FROM t WHERE p_email <> '')
         + (SELECT count(*) FROM t WHERE addr1 <> '') + (SELECT count(*) FROM g_cc WHERE card_id = $c AND opened_at <= $as_of::TIMESTAMP) AS n_edges;
