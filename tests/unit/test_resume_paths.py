"""Savanna auto-resume on the sync paths (offline: every transport is a fake, nothing reaches a workspace).

F2 graph.tg.gsql retries an HTML start page instead of reading it as a successful statement.
F3 graph/install_all.connect + _gsql retry getToken / conn.gsql; an auth error fails fast.
F6 agent/persist.py retries the AgentCase upsert; an auth error is not retried.
F7 agent/bench.ensure_awake fails fast on auth, retries a 502 once.
F8 rag/load_vectors.ensure_awake delegates to the shared ops.ensure_awake policy.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
from pyTigerGraph.common.exception import TigerGraphException

import graph.tg as graph_tg
import ops.ensure_awake as ops_ea

HTML = "<!DOCTYPE html><p>Starting workspace</p>"


def _http_error(status: int) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    resp.url = "https://example.invalid/x"
    return requests.HTTPError(f"{status} Error for url: {resp.url}", response=resp)


@pytest.fixture
def slept(monkeypatch):
    """time.sleep recorder: ensure_awake (both copies) looks time.sleep up at call time."""
    rec: list[float] = []
    monkeypatch.setattr(time, "sleep", rec.append)
    return rec


def _raise_then(values: list):
    """A callable that raises / returns the items of `values` in order."""
    seq = list(values)
    calls = {"n": 0}

    def fn(*_a, **_k):
        calls["n"] += 1
        v = seq.pop(0)
        if isinstance(v, BaseException):
            raise v
        return v

    fn.calls = calls
    return fn


# ------------------------------------------------------------------------------------------------ F2
def test_graph_gsql_retries_html_reply(slept):
    fake = SimpleNamespace(gsql=_raise_then([HTML, HTML, "Successfully created queries"]))
    reply = graph_tg.gsql(fake, "USE GRAPH FraudGraph\nLS", quiet=True)
    assert reply == "Successfully created queries"
    assert fake.gsql.calls["n"] == 3
    assert slept == [1, 2]


def test_html_guard_passes_normal_replies():
    assert graph_tg.html_guard("Successfully created queries") == "Successfully created queries"
    with pytest.raises(ConnectionError):
        graph_tg.html_guard("  \n<HTML><body>resuming</body></HTML>")


# ------------------------------------------------------------------------------------------------ F3
class _FakeConn:
    get_token = None
    gsql_fn = None

    def __init__(self, *a, **k):
        self.headers = None

    def getToken(self, secret):
        return type(self).get_token(secret)

    def customizeHeader(self, **k):
        self.headers = k

    def gsql(self, text):
        return type(self).gsql_fn(text)


@pytest.fixture
def install_all(monkeypatch):
    import graph.install_all as ia

    monkeypatch.setattr(ia, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr(ia, "TigerGraphConnection", _FakeConn)
    monkeypatch.setenv("TG_HOST", "https://example.invalid")
    monkeypatch.setenv("TG_SECRET", "not-a-real-secret")
    return ia


def test_install_all_connect_and_gsql_survive_resume(install_all, slept):
    _FakeConn.get_token = _raise_then([_http_error(502), ("tok", 0, "")])
    _FakeConn.gsql_fn = _raise_then([HTML, "Successfully created query card_window"])
    conn = install_all.connect()
    assert _FakeConn.get_token.calls["n"] == 2
    assert conn.headers == {"timeout": 600_000, "responseSize": 64_000_000}
    reply = install_all._gsql(conn, "USE GRAPH FraudGraph\nCREATE OR REPLACE QUERY card_window() {}")
    assert reply.startswith("Successfully created")
    assert install_all.gsql_ok(reply)
    assert _FakeConn.gsql_fn.calls["n"] == 2
    assert slept == [1, 1]


def test_install_all_connect_auth_error_fails_fast(install_all, slept):
    _FakeConn.get_token = _raise_then([_http_error(401)])
    with pytest.raises(requests.HTTPError):
        install_all.connect()
    assert _FakeConn.get_token.calls["n"] == 1
    assert slept == []


def test_install_all_connect_empty_secret(install_all, monkeypatch, slept):
    monkeypatch.setenv("TG_SECRET", "")
    _FakeConn.get_token = _raise_then([AssertionError("no request with an empty secret")])
    with pytest.raises(RuntimeError, match="TG_SECRET is empty"):
        install_all.connect()
    assert _FakeConn.get_token.calls["n"] == 0


# ------------------------------------------------------------------------------------------------ F6
def _persist_pm(conn, log):
    from agent.mock_fixtures import FakeSession

    return SimpleNamespace(
        s=SimpleNamespace(mock=False), graph_case_id="AC-HHG-TEST", log=log, run_id="t",
        E=SimpleNamespace(status=SimpleNamespace(derive=lambda *a: "open")), events=[],
        session=FakeSession(use_duckdb=False), tg_conn_factory=lambda: conn,
    )


def _persist_args():
    ctx = SimpleNamespace(case_id="HHG-TEST", customer_id="C00001", card_id="C00001-K1", trigger_type="risk_score",
                          opened_at="2016-11-22 20:11:00")
    sc = SimpleNamespace(flags={}, verdict="fraud", p_engine=0.9, pattern="card_testing", pattern_description="",
                         exposure_usd=10.0, episode_ids=[], connected_card_ids=[], connected_device_profiles=[],
                         similar_prior_cases=[], chain=[])
    return ctx, sc, [], [], [], {"file": False, "narrative": ""}, {}, []


@pytest.fixture
def persist(monkeypatch):
    from agent import persist as P

    monkeypatch.setattr(P.tools_local, "embed_document", lambda text: [0.1] * 8)
    monkeypatch.setattr(P, "embed_text_for_case", lambda *a, **k: "note")
    monkeypatch.setenv("TG_RESUME_MAX_WAIT_S", "2")
    return P


def _upsert_record(log):
    return [c for c in log.record_tool.call_args_list if c.args[0] == "pytg:upsertVertex(AgentCase)"]


def test_persist_upsert_survives_resume(persist, slept):
    conn = SimpleNamespace(upsertVertex=_raise_then([_http_error(502), 1]), getVectorStatus=lambda *a: True)
    log = MagicMock()
    written = asyncio.run(persist.persist_case(_persist_pm(conn, log), *_persist_args()))
    assert written is True
    assert conn.upsertVertex.calls["n"] == 2
    rec = _upsert_record(log)
    assert len(rec) == 1 and rec[0].kwargs["ok"] is True
    assert slept == [1]


def test_persist_upsert_auth_error_not_retried(persist, slept):
    conn = SimpleNamespace(upsertVertex=_raise_then([_http_error(401)]), getVectorStatus=lambda *a: True)
    log = MagicMock()
    written = asyncio.run(persist.persist_case(_persist_pm(conn, log), *_persist_args()))
    assert written is False
    assert conn.upsertVertex.calls["n"] == 1
    assert _upsert_record(log)[0].kwargs["ok"] is False
    assert slept == []


# ------------------------------------------------------------------------------------------------ F7
def test_bench_ensure_awake_auth_fails_fast(monkeypatch, slept):
    from agent import bench

    conn = SimpleNamespace(getVer=_raise_then([TigerGraphException("Authentication failed.")]))
    monkeypatch.setattr(ops_ea, "tg_connection", lambda *a, **k: conn)
    t0 = time.monotonic()
    with pytest.raises(TigerGraphException):
        bench.ensure_awake(SimpleNamespace(mock=False))
    assert time.monotonic() - t0 < 1.0
    assert conn.getVer.calls["n"] == 1
    assert slept == []


def test_bench_ensure_awake_retries_502_once(monkeypatch, slept):
    from agent import bench

    conn = SimpleNamespace(getVer=_raise_then([_http_error(502), "4.2.5"]))
    monkeypatch.setattr(ops_ea, "tg_connection", lambda *a, **k: conn)
    assert bench.ensure_awake(SimpleNamespace(mock=False)) is None
    assert conn.getVer.calls["n"] == 2
    assert slept == [1]


def test_bench_ensure_awake_mock_is_noop(monkeypatch):
    from agent import bench

    monkeypatch.setattr(ops_ea, "tg_connection", lambda *a, **k: pytest.fail("mock mode must not connect"))
    bench.ensure_awake(SimpleNamespace(mock=True))


# ------------------------------------------------------------------------------------------------ F8
def test_load_vectors_ensure_awake_uses_shared_policy(slept):
    from rag import load_vectors

    fn = _raise_then([_http_error(502), _http_error(502), "ok"])
    assert load_vectors.ensure_awake(fn, "a", attempts=1) == "ok"   # attempts is ignored
    assert fn.calls["n"] == 3
    assert slept == [1, 2]


def test_load_vectors_ensure_awake_auth_fails_fast(slept):
    from rag import load_vectors

    fn = _raise_then([TigerGraphException("Authentication failed.")])
    with pytest.raises(TigerGraphException):
        load_vectors.ensure_awake(fn)
    assert fn.calls["n"] == 1
    assert slept == []


def test_load_vectors_ensure_awake_respects_budget(monkeypatch, slept):
    from rag import load_vectors

    monkeypatch.setenv("TG_RESUME_MAX_WAIT_S", "3")

    def down():
        raise _http_error(503)

    with pytest.raises(requests.HTTPError):
        load_vectors.ensure_awake(down)
    assert sum(slept) <= 3
