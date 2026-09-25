"""tests/live/test_query_contracts.py — runs every read query on the live workspace through pyTigerGraph with
the parameters of expected/run_expected.py, normalises the result with mcp/normalize.py and compares it with
the DuckDB expectation file by key: exact keys, list lengths, and the scalar / id fields the engine relies on.
Requires .env with TG_HOST / TG_SECRET; skipped when absent (unit CI never touches the workspace).

    uv run pytest tests/live -q -k contracts
"""
import glob
import importlib.util
import json
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "graph"))

# A fresh `pytest` process never sees `.env` on its own -- nothing else imported here loads it (agent/config.py
# and rag/config.py do, but this file imports neither) -- so the skipif below always fired even with a real
# TG_HOST/TG_SECRET on disk. Same trap as rag/config.py; load once, never overriding a real env var.
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(ROOT, ".env"), override=False)
except ImportError:
    pass

# mcp/ is a plain directory next to the `mcp` SDK package; with the SDK installed `import mcp.normalize` resolves to
# the SDK and fails, so load the harness module by path exactly like agent/mcp_client.py does (never add mcp/__init__.py).
_spec = importlib.util.spec_from_file_location("hhgoa_normalize", os.path.join(ROOT, "mcp", "normalize.py"))
_normalize = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_normalize)
normalize, unwrap_singleton = _normalize.normalize, _normalize.unwrap_singleton

EXP = os.path.join(ROOT, "graph", "queries", "expected")
pytestmark = pytest.mark.skipif(not os.environ.get("TG_HOST"), reason="no live workspace configured")

# keys whose values are compared exactly (after normalisation); lists compare length only unless listed here
EXACT = {
    "case_context": ["txn.id", "txn.amt", "txn.channel", "txn.device_id", "card.id", "card.modal_region", "customer.id", "device.id", "device.is_strong"],
    "card_profile": ["card.id", "card.n_txns", "last30.n", "regions[0].addr1", "regions[0].n"],
    "card_window": ["summary.n", "summary.sum_amt", "txns[-1].id"],
    "region_history": ["region.prior_n", "region.prior_days", "modal_region", "n_regions", "hint"],
    "device_history": ["prior_n", "first_ts"],
    "device_neighbors": ["device.id", "device.is_strong", "device.n_cards_alltime"],
    "email_neighbors": [],
    "shared_origin_scan": ["devices[0].device_id", "devices[0].n_fraud_cases"],
    "card_testing_check": ["cleared_over_big", "larger_purchase.id", "chain.n_small"],
    "under_threshold_burst": ["bursts[0].burst_id", "bursts[0].ids"],
    "recurring_charge_check": ["total_n"],
    "episode_candidates": [],
    "prior_cases_for_customer": ["closed_cases[0].id"],
    # closed_cases[0].* is deliberately NOT checked exact: contracts/interfaces.md documents closed_cases as
    # "rows, not ids" with no ordering promise, and ring_profile.gsql accumulates them into a SetAccum (no
    # stable order across a reload) -- verified live: a fresh graph load returns the identical 4-row set for
    # HHG-014, just in a different order (CC-3035/2971/2985/2649 vs the fixture's 2649/2971/2985/3035).
    "ring_profile": ["ring_id", "device.n_cards_alltime", "n_cards_alltime", "wave_cards", "pre_open_cards"],
    "post_open_activity": ["summary.n", "summary.sum_amt"],
}


def _get(d, path):
    cur = d
    for part in path.replace("]", "").split("."):
        if "[" in part:
            k, i = part.split("[")
            cur = cur[k][int(i)]
        else:
            cur = cur[part]
    return cur


@pytest.fixture(scope="module")
def conn():
    from graph.install_all import connect
    from graph.tg import version

    conn = connect()
    version(conn)   # untimed warm call: a just-resumed workspace must not count against the latency assert
    return conn


def _wrap(params):
    out = {}
    for k, v in params.items():
        out[k] = {"id": v} if k in ("t", "c", "d", "e", "cu", "ac") else v
    return out


@pytest.mark.parametrize("path", sorted(glob.glob(os.path.join(EXP, "*__*.json"))))
def test_contract(conn, path):
    spec = json.load(open(path))
    q, expected = spec["query"], spec["expected"]
    if q == "case_subgraph":
        pytest.skip("UI-only; counts checked by eye")
    from graph.tg import ensure_awake

    t = time.time()
    raw = ensure_awake()(conn.runInstalledQuery)(q, _wrap(spec["params"]), timeout=120_000)
    latency = time.time() - t
    got = normalize(raw)
    assert latency < 5.0, f"{q} took {latency:.1f}s"
    exp_keys = {k for k in expected if not k.startswith("_")}
    assert exp_keys <= set(got), f"{q}: missing keys {exp_keys - set(got)}"
    if q == "card_testing_check":
        got["run"] = [unwrap_singleton(got, "run")] if got.get("run") else []
    for k in exp_keys:
        if k == "agent_cases":   # the agent's own write-backs: grows as live runs persist AgentCase; the DuckDB fixture has none
            continue
        if isinstance(expected[k], list):   # wave_cards / pre_open_cards are exact since D6 (27 / 19 for HHG-014)
            assert len(got[k]) == len(expected[k]), f"{q}.{k}: {len(got[k])} rows vs {len(expected[k])}"
        if isinstance(expected[k], list) and expected[k] and isinstance(expected[k][0], dict):
            assert set(expected[k][0]) <= set(got[k][0]), f"{q}.{k}: row keys differ"
    for p in EXACT.get(q, []):
        e, g = _get(expected, p), _get(got, p)
        if isinstance(e, float):
            assert abs(e - g) < 1e-6, f"{q}.{p}: {g} != {e}"
        elif isinstance(e, list):
            assert sorted(e) == sorted(g), f"{q}.{p}"
        else:
            assert e == g, f"{q}.{p}: {g!r} != {e!r}"
