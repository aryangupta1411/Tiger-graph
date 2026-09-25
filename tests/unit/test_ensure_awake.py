"""ops/ensure_awake.py: retry only on transient errors, bounded backoff, sync + async."""
from __future__ import annotations

import asyncio

import pytest

from ops.ensure_awake import backoff_delays, ensure_awake, is_transient


def test_backoff_is_geometric_capped_and_bounded():
    d = list(backoff_delays(240))
    assert d[:6] == [1, 2, 4, 8, 16, 32]
    assert max(d) <= 60
    assert abs(sum(d) - 240) < 1e-9


def test_transient_classification():
    assert is_transient(ConnectionError("Connection refused"))
    assert is_transient(RuntimeError("502 Bad Gateway"))
    assert is_transient(Exception("HTTPSConnectionPool: Max retries exceeded"))
    assert not is_transient(ValueError("GSQL syntax error"))
    assert not is_transient(FileNotFoundError("x.csv"))
    assert not is_transient(RuntimeError("401 Unauthorized"))


def test_sync_retries_then_succeeds():
    calls = {"n": 0}
    slept: list[float] = []

    @ensure_awake(max_wait_s=30, sleep=slept.append)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("502 Bad Gateway: workspace is resuming")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3
    assert slept == [1, 2]


def test_sync_gives_up_after_budget():
    slept: list[float] = []

    @ensure_awake(max_wait_s=3, sleep=slept.append)
    def always_down():
        raise ConnectionError("503 Service Unavailable")

    with pytest.raises(ConnectionError):
        always_down()
    assert sum(slept) <= 3


def test_non_transient_raises_immediately():
    slept: list[float] = []

    @ensure_awake(max_wait_s=30, sleep=slept.append)
    def bad():
        raise ValueError("bad params")

    with pytest.raises(ValueError):
        bad()
    assert slept == []


def test_async_retries(monkeypatch):
    calls = {"n": 0}
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    @ensure_awake(max_wait_s=30)
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise TimeoutError("read timed out")
        return 42

    assert asyncio.run(flaky()) == 42
    assert slept == [1]


# ---------------------------------------------------------------------------------------------------------- #
# F1: one classifier, run against BOTH copies (ops/ensure_awake.py and graph/tg.py must stay identical).      #
# ---------------------------------------------------------------------------------------------------------- #
import requests  # noqa: E402
from pyTigerGraph.common.exception import TigerGraphException  # noqa: E402

import graph.tg as graph_tg  # noqa: E402
import ops.ensure_awake as ops_ea  # noqa: E402

CLASSIFIERS = pytest.mark.parametrize("classify", [ops_ea.is_transient, graph_tg.is_transient], ids=["ops", "graph"])


def _http_error(status: int) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    resp.url = "https://example.invalid/restpp/query/FraudGraph/x"
    return requests.HTTPError(f"{status} Error for url: {resp.url}", response=resp)


TRANSIENT_CASES = [
    TigerGraphException("Cannot parse json: <!DOCTYPE html><title>Starting workspace</title>"),
    TigerGraphException("Cannot parse json: <html><body>resuming</body></html>", "500"),
    _http_error(502),
    _http_error(504),
    RuntimeError("502, message='Bad Gateway', url='https://x'"),
    RuntimeError("502 Server Error: Bad Gateway for url: https://x"),
    RuntimeError("HTTP 503"),
]
NOT_TRANSIENT_CASES = [
    RuntimeError("vertex 3503211 does not exist"),
    RuntimeError("customer C15034 not found"),
    RuntimeError("amount 0.502 is below the floor"),
    TigerGraphException("Authentication failed.", "REST-10016"),
    TigerGraphException("Cannot parse json: not json and not html"),
    _http_error(401),
    _http_error(403),
    RuntimeError("401 Unauthorized"),
]


@CLASSIFIERS
@pytest.mark.parametrize("exc", TRANSIENT_CASES, ids=lambda e: str(e)[:40])
def test_transient_cases_both_copies(classify, exc):
    assert classify(exc) is True


@CLASSIFIERS
@pytest.mark.parametrize("exc", NOT_TRANSIENT_CASES, ids=lambda e: str(e)[:40])
def test_not_transient_cases_both_copies(classify, exc):
    assert classify(exc) is False


def test_both_copies_share_markers():
    assert set(ops_ea.TRANSIENT_MARKERS) == set(graph_tg.TRANSIENT_MARKERS)
    assert ops_ea._STATUS_RX.pattern == graph_tg._STATUS_RX.pattern
    assert not {"502", "503", "504"} & set(ops_ea.TRANSIENT_MARKERS)


def test_auth_error_is_not_retried_both_copies():
    for deco in (ops_ea.ensure_awake, graph_tg.ensure_awake):
        slept: list[float] = []
        calls = {"n": 0}

        @deco(max_wait_s=30, sleep=slept.append)
        def auth(calls=calls):
            calls["n"] += 1
            raise TigerGraphException("Authentication failed.", "REST-10016")

        with pytest.raises(TigerGraphException):
            auth()
        assert calls["n"] == 1 and slept == []


def test_default_sleep_is_looked_up_at_call_time(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(ops_ea.time, "sleep", slept.append)
    calls = {"n": 0}

    @ops_ea.ensure_awake(max_wait_s=10)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _http_error(502)
        return "ok"

    assert flaky() == "ok" and slept == [1]


class _NeverConstruct:
    built = 0

    def __init__(self, *a, **k):
        _NeverConstruct.built += 1
        raise AssertionError("TigerGraphConnection must not be constructed with an empty secret")


@pytest.mark.parametrize("secret", ["", "   "])
def test_empty_secret_fails_before_any_request(monkeypatch, secret):
    import dotenv
    import pyTigerGraph

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(pyTigerGraph, "TigerGraphConnection", _NeverConstruct)
    monkeypatch.setenv("TG_HOST", "https://example.invalid")
    monkeypatch.setenv("TG_SECRET", secret)
    _NeverConstruct.built = 0
    with pytest.raises(RuntimeError, match="TG_SECRET is empty"):
        ops_ea.tg_connection()
    with pytest.raises(RuntimeError, match="TG_SECRET is empty"):
        graph_tg.connect()
    with pytest.raises(RuntimeError, match="TG_SECRET is empty"):
        graph_tg.connect(token=False)
    assert _NeverConstruct.built == 0
