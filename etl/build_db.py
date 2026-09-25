"""etl/build_db.py — raw CSVs -> DuckDB (contracts §D: tables tx, tx_raw, idn, cc, cp, pairs, cardmap).

    python -m etl.build_db --raw data/raw --db data/hhgoa.duckdb

tx_raw keeps all 397 columns as VARCHAR (fidelity; the scorer reads V/C/D from it),
tx is the typed slim view used everywhere else, `pairs` is the labelled (case, transaction)
set used by the card-rule regression test, and `cardmap` is the card_id rule (etl/card_rule.py).
Runtime on an M-series Mac: ~20 s.
"""
from __future__ import annotations

import argparse
import os
import time

import duckdb

from etl.card_rule import build_cardmap, check_rule
from ops.console import Col, Table, fail, header, ok, step, summary

# The dataset's hard counts (PLAN §3.2 / Makefile `db`). Displayed as expected-vs-actual and asserted below.
EXPECTED = {"tx": 590_742, "idn": 144_432, "cc": 5_565, "cp": 20, "cardmap": 14_317}
EXPECTED_PAIRS = 14_975

TX_COLS = """
  TransactionID::BIGINT AS TransactionID,
  TransactionDT::BIGINT AS TransactionDT,
  TransactionAmt::DOUBLE AS TransactionAmt,
  ProductCD, card1, card2, card3, card4, card5, card6, addr1, addr2, dist1, dist2, P_emaildomain, R_emaildomain,
  C1,C2,C3,C4,C5,C6,C7,C8,C9,C10,C11,C12,C13,C14,
  D1,D2,D3,D4,D5,D6,D7,D8,D9,D10,D11,D12,D13,D14,D15,
  M1,M2,M3,M4,M5,M6,M7,M8,M9,
  customer_id, ts::TIMESTAMP AS ts, ts AS ts_str, channel, risk_score::DOUBLE AS risk_score,
  row_number() OVER () AS file_row
"""


def build(raw: str, db: str) -> None:
    t0 = time.time()
    header("etl.build_db", "raw CSVs -> DuckDB (tx_raw, tx, idn, cc, cp, pairs, cardmap)",
           {"raw": raw, "db": db, "threads": 8, "memory limit": "8GB"})
    con = duckdb.connect(db)
    con.execute("PRAGMA threads=8; SET memory_limit='8GB';")
    f = lambda name: os.path.join(raw, name)  # noqa: E731
    step("reading transactions.csv (397 columns, all VARCHAR)")
    con.execute(f"""
        CREATE OR REPLACE TABLE tx_raw AS
        SELECT * FROM read_csv('{f("transactions.csv")}', header=true, all_varchar=true, sample_size=-1, quote='"', escape='"')
    """)
    ok(f"tx_raw {con.execute('SELECT count(*) FROM tx_raw').fetchone()[0]:,} rows in {time.time() - t0:.0f}s")
    step("typed slim view tx")
    con.execute(f"CREATE OR REPLACE TABLE tx AS SELECT {TX_COLS} FROM tx_raw")
    step("reading identity.csv")
    con.execute(f"""
        CREATE OR REPLACE TABLE idn AS
        SELECT * FROM read_csv('{f("identity.csv")}', header=true, all_varchar=true, sample_size=-1)
    """)
    con.execute("ALTER TABLE idn ALTER TransactionID TYPE BIGINT")
    step("reading closed_cases_history.csv and case_pack.csv")
    con.execute(f"CREATE OR REPLACE TABLE cc AS SELECT * FROM read_csv('{f('closed_cases_history.csv')}', header=true, all_varchar=true, sample_size=-1)")
    con.execute(f"CREATE OR REPLACE TABLE cp AS SELECT * FROM read_csv('{f('case_pack.csv')}', header=true, all_varchar=true, sample_size=-1)")
    step("labelled (case, transaction) pairs")
    con.execute("""
        CREATE OR REPLACE TABLE pairs AS
        SELECT case_id, card_id, customer_id, unnest(string_split(txn_ids, '|'))::BIGINT AS TransactionID, 'closed' AS src FROM cc
        UNION ALL
        SELECT case_id, card_id, customer_id, flagged_txn_id::BIGINT, 'casepack' FROM cp
    """)
    step("card_id rule -> cardmap")
    n_cards = build_cardmap(con)
    n, n_ok = check_rule(con)
    counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("tx", "idn", "cc", "cp", "pairs", "cardmap")}

    t = Table(
        Col("table / check", max_width=28),
        Col("expected", align="right", width=10),
        Col("actual", align="right", width=14),
        Col("result", width=6, align="center"),
        title="dataset counts (the project's core correctness claims)",
    )
    bad = []
    for name, got in counts.items():
        want = EXPECTED.get(name)
        good = want is None or got == want
        if not good:
            bad.append(f"{name}: expected {want:,}, got {got:,}")
        t.add_row(name, f"{want:,}" if want is not None else "-", f"{got:,}",
                  "-" if want is None else "PASS" if good else "FAIL",
                  style=None if good else "red")
    rule_ok = n == n_ok == EXPECTED_PAIRS
    if not rule_ok:
        bad.append(f"card rule: expected {EXPECTED_PAIRS:,}/{EXPECTED_PAIRS:,}, got {n_ok:,}/{n:,}")
    t.add_row("card rule pairs reproduced", f"{EXPECTED_PAIRS:,}", f"{n_ok:,} / {n:,}",
              "PASS" if rule_ok else "FAIL", style=None if rule_ok else "red")
    t.print()
    for b in bad:
        fail(b)
    if not bad:
        ok("every dataset count matches the contract")

    summary("etl.build_db complete",
            {"db": db, "tables": ", ".join(counts), "card rule": f"{n_ok:,}/{n:,}",
             "cards": f"{n_cards:,}", "elapsed": f"{time.time() - t0:.0f}s"},
            status="ok" if not bad else "fail")
    assert counts["tx"] == 590_742 and counts["idn"] == 144_432 and counts["cc"] == 5_565 and counts["cp"] == 20
    assert n == n_ok == 14_975 and n_cards == 14_317
    con.close()


if __name__ == "__main__":  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--db", default="data/hhgoa.duckdb")
    a = ap.parse_args()
    build(a.raw, a.db)
