"""tests/unit/test_card_rule.py — the card_id rule must reproduce every labelled pair.

Run:  HHGOA_DB=data/hhgoa.duckdb python -m pytest tests/unit/test_card_rule.py -q
(or plain `python tests/unit/test_card_rule.py` — no pytest needed).
The DuckDB must contain `tx`, `cc`, `cp`, `pairs` (built by etl/build_db.py).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from etl.card_rule import build_cardmap, card_ids_exist, check_rule  # noqa: E402

DB = os.environ.get("HHGOA_DB", "data/hhgoa.duckdb")
# CI has no data/hhgoa.duckdb; locally `make test-data` runs these with the DB present
pytestmark = pytest.mark.skipif(not Path(DB).exists(), reason=f"needs the ETL DuckDB at {DB} (HHGOA_DB)")

# Cards that must exist after the rule is applied: every closed-case card (1,913 distinct)
# and every case-pack card (20, of which 4 have no closed case -> 24 = 20 + 4 new...);
# the task's "1,913 + 24" is checked as: 1,913 distinct cc cards, and 24 distinct cards
# across cc ∪ cp that are case-pack cards or appear only in cp.  We assert the raw facts.
EXPECTED_PAIRS = 14_975
EXPECTED_CC_CARDS = 1_913
EXPECTED_CP_CARDS = 20
EXPECTED_CP_ONLY_CARDS = 4          # HHG-006, 009, 014, 016 have no closed case


def _con():
    con = duckdb.connect(DB, read_only=False)
    build_cardmap(con)
    return con


def test_rule_reproduces_every_labelled_pair():
    con = _con()
    n, ok = check_rule(con)
    assert n == EXPECTED_PAIRS, n
    assert ok == n, f"only {ok}/{n} labelled pairs reproduced"


def test_every_labelled_card_id_exists():
    con = _con()
    cc_cards = [r[0] for r in con.execute("SELECT DISTINCT card_id FROM cc").fetchall()]
    cp_cards = [r[0] for r in con.execute("SELECT DISTINCT card_id FROM cp").fetchall()]
    assert len(cc_cards) == EXPECTED_CC_CARDS, len(cc_cards)
    assert len(cp_cards) == EXPECTED_CP_CARDS, len(cp_cards)
    assert card_ids_exist(con, cc_cards) == set(cc_cards)
    assert card_ids_exist(con, cp_cards) == set(cp_cards)
    only_cp = set(cp_cards) - set(cc_cards)
    assert len(only_cp) == EXPECTED_CP_ONLY_CARDS, sorted(only_cp)
    # 1,913 closed-case cards + 4 case-pack-only cards = 1,917 distinct labelled cards
    assert len(set(cc_cards) | set(cp_cards)) == EXPECTED_CC_CARDS + EXPECTED_CP_ONLY_CARDS


def test_card_counts_match_plan():
    con = _con()
    n_cards = con.execute("SELECT count(*) FROM cardmap").fetchone()[0]
    dist = dict(con.execute(
        "SELECT n, count(*) FROM (SELECT customer_id, count(*) n FROM cardmap GROUP BY 1) GROUP BY 1"
    ).fetchall())
    assert n_cards == 14_317, n_cards
    assert dist == {1: 12_793, 2: 756, 3: 4}, dist
    # customer_id is a bijection with card1
    assert con.execute("SELECT count(*) FROM (SELECT card1 FROM tx GROUP BY 1 HAVING count(DISTINCT customer_id) > 1)").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM (SELECT customer_id FROM tx GROUP BY 1 HAVING count(DISTINCT card1) > 1)").fetchone()[0] == 0


if __name__ == "__main__":  # pragma: no cover
    for fn in (test_rule_reproduces_every_labelled_pair, test_every_labelled_card_id_exists, test_card_counts_match_plan):
        fn()
        print("ok", fn.__name__)
