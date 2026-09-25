"""etl/card_rule.py — the card_id derivation rule (contracts/schema.md, "Derivation rules").

    card_id = customer_id || '-K' || dense_rank() OVER (PARTITION BY customer_id ORDER BY coalesce(card6, ''))

computed over DISTINCT (customer_id, card6).  NULL card6 sorts first (coalesce to '').
This is the only derivation that reproduces 100 % of the 14,975 labelled (transaction -> card)
pairs in closed_cases_history.csv + case_pack.csv (see tests/unit/test_card_rule.py).

Usage (library):
    from etl.card_rule import build_cardmap, CARDMAP_SQL
    build_cardmap(con)            # creates/replaces table `cardmap` in the DuckDB connection
Usage (CLI):
    python -m etl.card_rule data/hhgoa.duckdb      # builds cardmap + prints the regression score
"""
from __future__ import annotations

import sys

import duckdb

# One row per (customer_id, card6) pair.  `k6` keeps the coalesced card6 so joins from `tx`
# can use coalesce(card6,'') = k6 without NULL-equality problems.
CARDMAP_SQL = """
CREATE OR REPLACE TABLE cardmap AS
SELECT customer_id,
       coalesce(card6, '')                                   AS k6,
       coalesce(card6, 'unknown')                            AS card_type,
       customer_id || '-K' ||
         dense_rank() OVER (PARTITION BY customer_id ORDER BY coalesce(card6, ''))::VARCHAR AS card_id
FROM (SELECT DISTINCT customer_id, card6 FROM tx)
"""

# Regression: every labelled pair must be reproduced.
CHECK_SQL = """
SELECT count(*)                       AS n_pairs,
       sum((p.card_id = m.card_id)::INT) AS n_ok
FROM pairs p
JOIN tx t USING (TransactionID)
JOIN cardmap m ON m.customer_id = t.customer_id AND m.k6 = coalesce(t.card6, '')
"""


def build_cardmap(con: duckdb.DuckDBPyConnection) -> int:
    """Create table `cardmap` (customer_id, k6, card_type, card_id). Returns the number of cards."""
    con.execute(CARDMAP_SQL)
    return con.execute("SELECT count(*) FROM cardmap").fetchone()[0]


def check_rule(con: duckdb.DuckDBPyConnection) -> tuple[int, int]:
    """Return (n_pairs, n_ok) over the labelled pairs (closed cases + case pack)."""
    n, ok = con.execute(CHECK_SQL).fetchone()
    return int(n), int(ok)


def card_ids_exist(con: duckdb.DuckDBPyConnection, ids: list[str]) -> set[str]:
    """Subset of `ids` that exist in cardmap (used by the validator and tests)."""
    if not ids:
        return set()
    rows = con.execute(
        "SELECT card_id FROM cardmap WHERE card_id IN (SELECT unnest(?::VARCHAR[]))", [ids]
    ).fetchall()
    return {r[0] for r in rows}


if __name__ == "__main__":  # pragma: no cover
    from ops.console import Col, Table, header, summary
    from ops.console import ok as ok_line

    db = sys.argv[1] if len(sys.argv) > 1 else "data/hhgoa.duckdb"
    con = duckdb.connect(db)
    header("etl.card_rule",
           "derive card_id and replay it against every labelled (transaction -> card) pair",
           {"db": db, "rule": "customer_id || '-K' || dense_rank() over (customer_id ORDER BY card6)"})
    n_cards = build_cardmap(con)
    n, n_ok = check_rule(con)
    t = Table(
        Col("check", max_width=34),
        Col("expected", align="right", width=10),
        Col("actual", align="right", width=10),
        Col("result", width=6, align="center"),
        title="card rule regression (contracts/schema.md)",
    )
    t.add_row("labelled pairs reproduced", f"{n:,}", f"{n_ok:,}", "PASS" if n == n_ok else "FAIL",
              style=None if n == n_ok else "red")
    t.add_row("cards in cardmap", f"{n_cards:,}", f"{n_cards:,}", "PASS")
    t.print()
    if n == n_ok:
        ok_line(f"card rule reproduces {n_ok:,}/{n:,} labelled pairs (100.0%)")
    summary("etl.card_rule complete",
            {"cards": f"{n_cards:,}", "labelled pairs": f"{n:,}",
             "reproduced": f"{n_ok:,}", "miss": f"{n - n_ok:,}"},
            status="ok" if n == n_ok else "fail")
    sys.exit(0 if n == n_ok else 1)
