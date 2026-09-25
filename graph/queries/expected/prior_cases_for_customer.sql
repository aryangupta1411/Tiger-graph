-- prior_cases_for_customer(cu, as_of) — params $cu, $as_of
SELECT (SELECT coalesce(list(struct_pack(opened_at := strftime(opened_at, '%Y-%m-%d %H:%M:%S'), id, card_id, outcome, pattern, exposure_usd, report_filed, n_txns, template_id) ORDER BY opened_at DESC, id), [])
          FROM (SELECT * FROM g_cc WHERE customer_id = $cu AND opened_at <= $as_of::TIMESTAMP ORDER BY opened_at DESC, id LIMIT 40)) AS closed_cases,
       [] AS agent_cases;
