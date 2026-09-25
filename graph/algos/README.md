# graph/algos

| File | Origin | Syntax | Run by |
|---|---|---|---|
| `tg_wcc.gsql` | fetched verbatim 2026-09-19 from `https://raw.githubusercontent.com/tigergraph/gsql-graph-algorithms/tg_4.2.5_dev/algorithms/Community/connected_components/weakly_connected_components/standard/tg_wcc.gsql` (`make algos-fetch` re-downloads it from that same upstream URL; MIT-licensed, tigergraph/gsql-graph-algorithms) | V1 | `run_algos.py` → `Card.wcc_id` |
| `tg_degree_cent.gsql` | fetched verbatim from `.../tg_4.2.5_dev/algorithms/Centrality/degree/unweighted/tg_degree_cent.gsql` | V1 | `run_algos.py` → `Card.device_degree` (fallback `set_device_degree`) |
| `project_shares_device.gsql` | ours | v3 | `run_algos.py` → `SHARES_DEVICE` edges over strong profiles |
| `clear_shares_device.gsql` | ours | v2 | `run_algos.py` before re-projecting |
| `set_device_degree.gsql` | ours | v3 | exact INT degree + `wcc_id = -1` for isolated cards |
| `wcc_summary.gsql` | ours | v3 | component-size report for the blog |

`tg_pagerank_pers` (PLAN stretch) is not fetched: `Card.fraud_ppr` stays 0 until it is.
