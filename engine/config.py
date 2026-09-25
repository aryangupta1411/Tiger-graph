"""Paths and constants shared by the engine package.

Everything is overridable through environment variables so the same code runs in the repo
(`data/` layout, contracts §D) and in the scratchpad used to build this module.

  ENGINE_DATA_DIR   the repo's data/ folder: models/isotonic.json (the ETL calibrator, decision D7),
                    models/lgb_fraud.txt, raw/ and out/               (default: <repo>/data)
  ENGINE_FACTS_DB   the facts DuckDB written by `python -m engine.facts_from_etl` — tables txc /
                    card_feat / device_profile / customer_feat / cc / cc_txn / cp — the mock backend,
                    the validator's id resolver and the replay all read it
                    (default: data/hhgoa_engine.duckdb, falling back to engine/out/hhgoa_engine.duckdb)
  ENGINE_OUT_DIR    where drafts, the expectation sheet and replay metrics are written
  ENGINE_POLICY_YAML the policy file (default: policy/fraud_policy.yaml)

The ETL scorer (etl/cms_train.py -> Transaction.cms_p, data/models/isotonic.json) is the only scorer;
the engine's former sidecar builder / OOF scorer / calibration.json live in impl/attic (D7).
"""
from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DATA_DIR = Path(os.environ.get("ENGINE_DATA_DIR", str(REPO / "data")))
OUT_DIR = Path(os.environ.get("ENGINE_OUT_DIR", str(HERE / "out")))
POLICY_YAML = Path(os.environ.get("ENGINE_POLICY_YAML", str(REPO / "policy" / "fraud_policy.yaml")))
CALIBRATION_JSON = Path(os.environ.get("ENGINE_CALIBRATION_JSON", str(DATA_DIR / "models" / "isotonic.json")))


def _facts_db() -> Path:
    env = os.environ.get("ENGINE_FACTS_DB")
    if env:
        return Path(env)
    repo_layout = DATA_DIR / "hhgoa_engine.duckdb"
    return repo_layout if repo_layout.exists() else HERE / "out" / "hhgoa_engine.duckdb"


FACTS_DB = _facts_db()

# The exact seeded ring profile string (dataset spelling, never re-typed elsewhere).
RING_PROFILE = "SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080"
ANON_PROXY = "IP_PROXY:ANONYMOUS"
DATASET_EPOCH = "2016-07-02 00:00:00"

# Amount / time constants used by more than one module.
EPISODE_GAP_H = 48
BURST_MIN_AMT, BURST_MAX_AMT, BURST_WINDOW_MIN, BURST_MIN_N = 450.0, 499.99, 40, 4
CARD_TESTING_SMALL, CARD_TESTING_WINDOW_MIN, CARD_TESTING_MIN_N, CARD_TESTING_BIG, CARD_TESTING_LOOKAHEAD_H = 5.0, 60, 3, 100.0, 48
STRONG_MAX_CARDS_ALLTIME, STRONG_MIN_CARDS_30D, STRONG_MAX_CARDS_30D = 60, 2, 40
SCORER_UNRELIABLE_CARD_TXNS, SCORER_UNRELIABLE_MIN_SEQ = 3000, 5


def ensure_dirs() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
