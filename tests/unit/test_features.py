"""tests/unit/test_features.py — the facts PLAN.md builds on must hold in the derived tables.

Run after etl/features.py (+ etl/cms_train.py for the cms tests):
    HHGOA_DB=data/hhgoa.duckdb python -m pytest tests/unit/test_features.py -q
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import duckdb
import pytest

DB = os.environ.get("HHGOA_DB", "data/hhgoa.duckdb")
# CI has no data/hhgoa.duckdb; locally `make test-data` runs these with the DB present
pytestmark = pytest.mark.skipif(not Path(DB).exists(), reason=f"needs the ETL DuckDB at {DB} (HHGOA_DB)")
RING = "SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080"
HHG014_OPEN = "2016-11-22 20:11:00"


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect(DB, read_only=True)
    yield c
    c.close()


def one(con, sql, *args):
    return con.execute(sql, list(args)).fetchone()


def test_row_counts(con):
    assert one(con, "SELECT count(*) FROM txc")[0] == 590_742
    assert one(con, "SELECT count(*) FROM txn_feat")[0] == 590_742
    assert one(con, "SELECT count(*) FROM card_feat")[0] == 14_317
    assert one(con, "SELECT count(*) FROM device_profile")[0] == 9_705
    assert one(con, "SELECT count(*) FROM txc WHERE device_id <> ''")[0] == 144_432 - 3_648   # FROM_DEVICE edges


def test_ring_profile_survives_filter(con):
    """The exact ring string is strong (52 cards all-time, 28 in its busiest 30 days, 100 % proxy, 4 closed cases)."""
    r = one(con, "SELECT is_strong, n_cards_alltime, n_cards_30d, n_txns, n_proxy, n_fraud_cases FROM device_profile WHERE id = ?", RING)
    assert r == (True, 52, 28, 114, 114, 4), r
    other_nov = one(con, "SELECT count(DISTINCT card_id) FROM txc WHERE device_id = ? AND ts >= '2016-11-01' AND card_id <> 'C13487-K1'", RING)[0]
    pre_open = one(con, "SELECT count(DISTINCT card_id) FROM txc WHERE device_id = ? AND ts BETWEEN '2016-11-01' AND ? AND card_id <> 'C13487-K1'", RING, HHG014_OPEN)[0]
    assert (other_nov, pre_open) == (27, 19)
    assert one(con, "SELECT count(*) FROM card_feat WHERE ring_id = ?", RING)[0] == 52
    assert one(con, "SELECT ring_id FROM card_feat WHERE id = 'C13487-K1'")[0] == RING
    # HHG-014: two ring transactions before opening, one after
    rows = con.execute("SELECT TransactionID, amt FROM txc t JOIN txn_feat f USING (TransactionID) WHERE t.card_id = 'C13487-K1' AND f.ring_hit ORDER BY t.ts").fetchall()
    assert [r[0] for r in rows] == [3460634, 3478561, 3489320], rows   # 3489320 ($252.28, Nov 26) is post-open
    assert round(rows[0][1] + rows[1][1], 2) == 187.33


def test_generic_profiles_are_not_strong(con):
    for pid in ("NULL | NULL | chrome 66.0 | NULL", "Windows | Windows 10 | chrome 63.0 | 1920x1080", "Windows | NULL | edge 16.0 | NULL"):
        assert one(con, "SELECT is_strong FROM device_profile WHERE id = ?", pid)[0] is False, pid


def test_burst_detector(con):
    assert one(con, "SELECT count(DISTINCT card_id) FROM burst_member WHERE ts >= '2016-11-01'")[0] == 12
    rows = con.execute("SELECT TransactionID, burst_id, amt FROM burst_member WHERE card_id = 'C07297-K1' ORDER BY ts").fetchall()
    assert [r[0] for r in rows] == [3476602, 3476633, 3476665, 3476682]
    assert {r[1] for r in rows} == {"C07297-K1#3476602"}
    assert round(sum(r[2] for r in rows), 2) == 1906.07
    look = json.loads(one(con, "SELECT burst_lookalike_ids FROM card_feat WHERE id = 'C07297-K1'")[0])
    # the 11 other Nov-Dec burst cards + C06208-K1 (October burst inside the ±30-day window)
    assert len(look) == 12 and "C07297-K1" not in look and "C06208-K1" in look


def test_modal_region_in_person_only(con):
    assert one(con, "SELECT modal_region FROM card_feat WHERE id = 'C09933-K2'")[0] == "264.0"
    assert one(con, "SELECT modal_region FROM card_feat WHERE id = 'C08623-K2'")[0] == "299.0"
    assert one(con, "SELECT modal_region FROM card_feat WHERE id = 'C02354-K2'")[0] == "325.0"


def test_prior_features_are_leakage_free(con):
    assert one(con, """SELECT count(*) FROM txn_feat WHERE card_seq = 1 AND (prior_in_region <> 0 OR prior_on_dev <> 0
                       OR prior_pem <> 0 OR prior_pcd <> 0 OR prior_med_amt <> -1 OR prior_max_amt <> -1)""")[0] == 0
    assert one(con, """SELECT count(*) FROM (SELECT prior_max_amt, lag(prior_max_amt) OVER (PARTITION BY card_id ORDER BY ts, TransactionID) p
                       FROM txn_feat) WHERE p IS NOT NULL AND p > prior_max_amt""")[0] == 0
    assert one(con, "SELECT count(*) FROM txn_feat WHERE gap_seconds = 0")[0] == 157      # same-second ties -> TransactionID tiebreak


def test_cms_scores_present(con):
    if one(con, "SELECT count(*) FROM txn_feat WHERE cms_p IS NULL")[0]:
        pytest.skip("cms_train.py not run yet")
    folds = dict(con.execute("SELECT cms_fold, count(*) FROM txn_feat GROUP BY 1").fetchall())
    assert set(folds) == {"oof-2016-07", "oof-2016-08", "oof-2016-09", "oof-2016-10", "final"}
    assert folds["final"] == 173_338
    p = one(con, "SELECT cms_p FROM txn_feat WHERE TransactionID = 3514948")[0]   # HHG-007 flagged: scorer high
    assert p > 0.8
    p = one(con, "SELECT cms_p FROM txn_feat WHERE TransactionID = 3506725")[0]   # HHG-010 calibration trap: scorer low
    assert p < 0.1


def test_closed_case_parse(con):
    t = dict(con.execute("SELECT template_id, count(*) FROM closed_case_parsed GROUP BY 1").fetchall())
    assert t == {"fraud_reported": 4656, "cleared_travel": 716, "cleared_new_phone": 158, "cleared_amount": 26, "undoc_ring": 4, "undoc_burst": 5}
    assert one(con, "SELECT count(*) FROM closed_case_txn")[0] == 14_955
    assert one(con, "SELECT count(*) FROM closed_case_conn")[0] == 92          # 4 ring cases x 23 connected cards
    e = one(con, "SELECT embed_text FROM closed_case_parsed WHERE id = 'CC-0001'")[0]
    assert e.startswith("pattern: card_not_present_fraud | outcome: confirmed_fraud | device: none | region: none | Case <CASE>: cardholder <ID>")
    assert "$" not in e and "CC-0001" not in e
