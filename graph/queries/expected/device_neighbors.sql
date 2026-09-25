-- device_neighbors(d, from_ts, to_ts, max_cards) — params $d, $from_ts, $to_ts, $max_cards
WITH dev AS (SELECT * FROM g_dev WHERE id = $d),
     strong AS (SELECT coalesce((SELECT is_strong FROM dev), false) s),
     w AS (SELECT * FROM g_txn WHERE device_id = $d AND ts >= $from_ts::TIMESTAMP AND ts <= $to_ts::TIMESTAMP AND (SELECT s FROM strong)),
     cards AS (SELECT card_id, any_value(customer_id) customer_id, count(*) n_txns, round(sum(amt), 2) sum_amt,
                      count(*) FILTER (WHERE proxy <> '') n_proxy, count(*) FILTER (WHERE device_new = 'New') n_new,
                      strftime(min(ts), '%Y-%m-%d %H:%M:%S') first_ts, strftime(max(ts), '%Y-%m-%d %H:%M:%S') last_ts
               FROM w GROUP BY 1 ORDER BY n_txns DESC, card_id LIMIT $max_cards)
SELECT
  (SELECT struct_pack(id, is_strong, n_cards_alltime, n_cards_30d, n_proxy, n_fraud_cases) FROM dev) AS device,
  (SELECT coalesce(list(struct_pack(card_id, customer_id, n_txns, sum_amt, n_proxy, n_new, first_ts, last_ts) ORDER BY n_txns DESC, card_id), []) FROM cards) AS cards,
  -- closed cases whose INVOLVES transactions used THIS profile, all-time (engine/facts_duckdb.py, the engine of record,
  -- and graph/queries/device_neighbors.gsql). Not every case on the neighbour cards: that let a hub card's unrelated
  -- fraud history read as "this device carries fraud".
  (SELECT coalesce(list(struct_pack(id, card_id, pattern, outcome, report_filed) ORDER BY id), []) FROM (
     SELECT * FROM g_cc WHERE (SELECT s FROM strong)
       AND id IN (SELECT x.case_id FROM g_cc_txn x JOIN g_txn t ON t.id = x.txn_id WHERE t.device_id = $d)
     ORDER BY id LIMIT 60)) AS closed_cases,
  [] AS agent_cases,
  (SELECT count(DISTINCT card_id) FROM w) AS _n_cards_in_window;
