"""Build the engine's facts DuckDB from the ETL DuckDB (integration glue, Module H).

engine/facts_duckdb.py (mock backend, validator db, engine.run_cases, engine.replay) reads the tables
    txc(TransactionID, id, ..., cms_p, card_seq, prior_*, ring_hit, burst_id)   one row per Transaction vertex
    card_feat, device_profile                                                  as the ETL writes them
    customer_feat(id, n_cards, n_txns)
    cc(case_id, ..., report_filed BOOL, opened_at TIMESTAMP), cc_txn(case_id, TransactionID)
    cp(case_id, opened_at TIMESTAMP, ..., risk_score DOUBLE)
The ETL (etl/features.py, etl/parse_closed_cases.py, etl/cms_train.py) writes the same facts under
different names/shapes into data/hhgoa.duckdb: txc (raw attributes) + txn_feat (derived), closed_case_parsed,
closed_case_txn, cc / cp as all-VARCHAR, and no customer_feat. This script materialises the engine's shape
from the ETL's tables so ONE derivation pipeline (the ETL, the authority of contracts/schema.md) feeds the
graph, the mock backend and the validator.

    uv run python -m engine.facts_from_etl --etl data/hhgoa.duckdb --out data/hhgoa_engine.duckdb
    ENGINE_FACTS_DB=data/hhgoa_engine.duckdb uv run python -m engine.run_cases

Verified 2026-09-19 on impl/code/etl/hhgoa_work.duckdb: 20/20 exam cases validate (engine.run_cases) on the
resulting file in 1.4 s; decisions identical to the engine's own sidecar except probability values, which now
come from the ETL scorer (data/models/isotonic.json must be the calibrator).
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import duckdb

from ops.console import Col, Table, fail, header, ok, step, summary

SQL = [
    """CREATE OR REPLACE TABLE txc AS
       SELECT t.TransactionID, t.TransactionID::VARCHAR AS id, t.card_id, t.customer_id, t.ts, t.ts_str, t.amt, t.product_cd, t.channel,
              t.addr1, t.addr2, t.dist1, t.dist2, t.p_email, t.r_email, t.risk_score, t.has_identity, t.device_new, t.proxy, t.device_type,
              t.device_id, f.cms_p, f.card_seq, f.prior_in_region, f.prior_on_dev, f.prior_pem, f.prior_pcd, f.prior_med_amt, f.prior_max_amt,
              f.ring_hit, f.burst_id
       FROM etl.txc t JOIN etl.txn_feat f USING (TransactionID)""",
    "CREATE OR REPLACE TABLE card_feat AS SELECT * FROM etl.card_feat",
    "CREATE OR REPLACE TABLE device_profile AS SELECT * FROM etl.device_profile",
    "CREATE OR REPLACE TABLE customer_feat AS SELECT customer_id AS id, count(*) AS n_cards, sum(n_txns) AS n_txns FROM etl.card_feat GROUP BY 1",
    """CREATE OR REPLACE TABLE cc AS
       SELECT p.id AS case_id, p.customer_id, p.card_id, p.opened_at, p.closed_at, p.outcome, p.pattern, p.first_fraud_txn_id,
              r.txn_ids, p.n_txns, p.exposure_usd, coalesce(r.connected_card_ids, '') AS connected_card_ids, p.actions_taken,
              p.report_filed, p.analyst_notes
       FROM etl.closed_case_parsed p JOIN etl.cc r ON r.case_id = p.id""",
    "CREATE OR REPLACE TABLE cc_txn AS SELECT case_id, TransactionID FROM etl.closed_case_txn",
    """CREATE OR REPLACE TABLE cp AS
       SELECT case_id, opened_at::TIMESTAMP AS opened_at, trigger_type, trigger_text, flagged_txn_id, card_id, customer_id,
              TRY_CAST(risk_score AS DOUBLE) AS risk_score
       FROM etl.cp""",
]

EXPECTED = {"txc": 590_742, "card_feat": 14_317, "device_profile": 9_705, "customer_feat": 13_553, "cc": 5_565, "cc_txn": 14_955, "cp": 20}


def build(etl_db: str, out: str) -> dict[str, int]:
    if os.path.exists(out):
        os.remove(out)
    con = duckdb.connect(out)
    con.execute(f"ATTACH '{etl_db}' AS etl (READ_ONLY)")
    for stmt in SQL:
        con.execute(stmt)
    counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in EXPECTED}
    con.execute("DETACH etl")
    con.close()
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--etl", default=os.getenv("HHGOA_DB", "data/hhgoa.duckdb"))
    ap.add_argument("--out", default=os.getenv("ENGINE_FACTS_DB", "data/hhgoa_engine.duckdb"))
    a = ap.parse_args(argv)
    t0 = time.time()
    header("engine.facts_from_etl", "materialise the engine's facts DuckDB from the ETL tables (decision D7)",
           {"etl db": a.etl, "out db": a.out, "tables": len(SQL)})
    step(f"building {len(SQL)} tables from {a.etl}")
    counts = build(a.etl, a.out)
    t = Table(Col("table", max_width=16), Col("rows", align="right", max_width=11), Col("expected", align="right", max_width=11),
              Col("result", max_width=8), title="engine facts DB contents")
    bad = []
    for name, n in counts.items():
        good = n == EXPECTED[name]
        bad += [] if good else [name]
        t.add_row(name, f"{n:,}", f"{EXPECTED[name]:,}", "ok" if good else "MISMATCH", style="green" if good else "bold red")
    t.print()
    for name in bad:
        fail(f"{name}: {counts[name]:,} rows, expected {EXPECTED[name]:,}")
    if not bad:
        ok("every table matches its expected row count")
    size_mb = os.path.getsize(a.out) / 1e6 if os.path.exists(a.out) else 0
    summary("facts DB written",
            {"path": a.out, "size": f"{size_mb:,.1f} MB", "tables": len(counts),
             "rows": f"{sum(counts.values()):,}", "mismatched": ", ".join(bad) or "none",
             "wall time": f"{time.time() - t0:.1f}s"},
            status="fail" if bad else "ok")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
