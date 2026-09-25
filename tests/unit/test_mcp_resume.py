"""Savanna auto-resume on the agent's MCP path (F4 run_query retry, F5 phase machine never scores missing facts).

The failure payloads are built with tigergraph-mcp's own `format_error`, exactly as its run_installed_query tool
returns them (it never raises; str(exc) goes into `error`). Everything is offline: a stub session, RUN_MODE=mock.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from tigergraph_mcp.response_formatter import format_error
from yarl import URL

from agent import mcp_client
from agent.mcp_client import GraphUnavailable, QueryError, ToolContext, call_query_tool, call_tool_with_resume, run_query


def _aiohttp_502() -> aiohttp.ClientResponseError:
    ri = aiohttp.RequestInfo(URL("https://x"), "POST", CIMultiDictProxy(CIMultiDict()), URL("https://x"))
    return aiohttp.ClientResponseError(ri, (), status=502, message="Bad Gateway")


def _result(content) -> SimpleNamespace:
    return SimpleNamespace(content=content, is_error=False, structured_content=None)


def err_502():
    return _result(format_error("run_installed_query", _aiohttp_502(), {}))


def err_text(exc: BaseException):
    return _result(format_error("run_installed_query", exc, {}))


def ok_result(printed: list):
    payload = {"success": True, "operation": "run_installed_query", "summary": "ok", "data": {"result": printed}}
    return _result([SimpleNamespace(type="text", text=f"```json\n{json.dumps(payload)}\n```")])


class StubSession:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def call_tool(self, name, arguments=None, **_):
        self.calls += 1
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(r, BaseException):
            raise r
        return r


@pytest.fixture
def no_sleep(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(mcp_client, "_sleep", fake_sleep)
    return slept


def test_format_error_payload_shape():
    """The two shapes the classifier has to read (checked against the installed tigergraph-mcp)."""
    p = mcp_client.parse_tool_text(err_502().content[0].text)
    assert p["success"] is False and p["error"].startswith("502, message='Bad Gateway'")
    p = mcp_client.parse_tool_text(err_text(TimeoutError()).content[0].text)
    assert p["error"] == "" and p["error_code"] == "OPERATION_ERROR"


# ------------------------------------------------------------------------------------------------ F4
def test_run_query_retries_502_then_succeeds(no_sleep):
    s = StubSession([err_502(), err_502(), ok_result([{"a": 1}, {"b": [2]}])])
    out = asyncio.run(run_query(s, "card_profile", {}))
    assert out == {"a": 1, "b": [2]}
    assert s.calls == 3 and no_sleep == [1, 2]


def test_run_query_retries_empty_error(no_sleep):
    s = StubSession([err_text(TimeoutError()), ok_result([{"a": 1}])])
    assert asyncio.run(run_query(s, "card_profile", {})) == {"a": 1}
    assert s.calls == 2


def test_run_query_retries_transient_exception_from_call_tool(no_sleep):
    s = StubSession([ConnectionError("server disconnected"), ok_result([{"a": 1}])])
    assert asyncio.run(run_query(s, "card_profile", {})) == {"a": 1}
    assert s.calls == 2


@pytest.mark.parametrize("exc", [RuntimeError("vertex 3503211 does not exist"),
                                 RuntimeError("('Authentication failed.', 'REST-10016')")])
def test_run_query_non_transient_raises_after_one_call(no_sleep, exc):
    s = StubSession([err_text(exc)])
    with pytest.raises(QueryError) as ei:
        asyncio.run(run_query(s, "card_profile", {}))
    assert not isinstance(ei.value, GraphUnavailable)
    assert s.calls == 1 and no_sleep == []


def test_run_query_gives_up_with_graph_unavailable(monkeypatch, no_sleep):
    monkeypatch.setenv("TG_RESUME_MAX_WAIT_S", "3")
    s = StubSession([err_502()])
    with pytest.raises(GraphUnavailable, match="502"):
        asyncio.run(run_query(s, "card_profile", {}))
    assert sum(no_sleep) <= 3 and s.calls == len(no_sleep) + 1


def test_run_query_records_tool_once(monkeypatch, no_sleep):
    log = SimpleNamespace(rows=[], notes=[])
    log.record_tool = lambda *a, **k: log.rows.append((a, k))
    log.note = lambda text, **k: log.notes.append((text, k))
    token = mcp_client.current_runlog.set(log)
    try:
        asyncio.run(run_query(StubSession([err_502(), ok_result([{"a": 1}])]), "card_profile", {}))
    finally:
        mcp_client.current_runlog.reset(token)
    assert len(log.rows) == 1 and log.rows[0][1]["ok"] is True
    assert log.notes[0][0] == "graph transient, retrying" and log.notes[0][1]["attempt"] == 1


def test_call_tool_with_resume(monkeypatch, no_sleep):
    ok = _result([SimpleNamespace(type="text", text='```json\n{"success": true, "data": {"accepted": 1}}\n```')])
    s = StubSession([err_502(), ok])
    assert asyncio.run(call_tool_with_resume(s, "tigergraph__add_edges", {})) is ok
    assert s.calls == 2
    monkeypatch.setenv("TG_RESUME_MAX_WAIT_S", "1")
    with pytest.raises(GraphUnavailable):
        asyncio.run(call_tool_with_resume(StubSession([err_502()]), "tigergraph__add_edges", {}))


# ------------------------------------------------------------------------------------------------ F5
def test_call_query_tool_graph_unavailable_is_not_evidence(monkeypatch, no_sleep):
    monkeypatch.setenv("TG_RESUME_MAX_WAIT_S", "1")
    tctx = ToolContext(opened_at="2016-11-22 20:11:00")
    tctx.budget.limit = 5
    out = asyncio.run(call_query_tool(StubSession([err_502()]), tctx, "shared_origin_scan", {}))
    assert tctx.graph_unavailable == "shared_origin_scan"
    assert tctx.budget.used == 0
    assert "parameter encoding" not in out and "GRAPH TEMPORARILY UNAVAILABLE" in out
    assert tctx.facts == {} and tctx.ledger == []


def test_call_query_tool_real_error_keeps_hint(no_sleep):
    tctx = ToolContext(opened_at="2016-11-22 20:11:00")
    out = asyncio.run(call_query_tool(StubSession([err_text(RuntimeError("vertex 3503211 does not exist"))]), tctx, "card_profile", {}))
    assert "parameter encoding" in out and tctx.graph_unavailable == "" and tctx.budget.used == 1


@pytest.fixture
def mock_run(monkeypatch, tmp_path):
    """One full phase-machine run of HHG-014 in RUN_MODE=mock (FakeSession + FakeAnthropic + FakeTGConnection)."""
    monkeypatch.setenv("RUN_MODE", "mock")
    monkeypatch.setenv("LLM_BACKEND", "mock")
    monkeypatch.setenv("TG_RESUME_MAX_WAIT_S", "1")
    from agent import tools_local
    from agent.bench import load_case_pack, make_client
    from agent.config import SETTINGS
    from agent.mock_fixtures import FakeSession
    from agent.phase_machine import PhaseMachine
    from agent.runlog import RunLog

    settings = replace(SETTINGS, run_mode="mock", llm_backend="mock")
    monkeypatch.setattr(tools_local, "SETTINGS", settings)
    if not settings.case_pack_csv.exists():
        pytest.skip("case pack not present")
    ctx = {c.case_id: c for c in load_case_pack(settings.case_pack_csv)}["HHG-014"]

    def run(failing: str, payload) -> tuple[object, object]:
        class Session(FakeSession):
            async def call_tool(self, name, arguments=None, **kw):
                if name == mcp_client.RUN_INSTALLED and (arguments or {}).get("query_name") == failing:
                    self.calls.append((name, arguments))
                    return payload
                return await super().call_tool(name, arguments, **kw)

        pm = PhaseMachine(make_client(settings), Session("HHG-014"), "t", settings)
        log = RunLog("t", ctx.case_id, tmp_path / ctx.case_id)
        try:
            return asyncio.run(pm.run_case(ctx, log)), log
        except Exception as e:  # noqa: BLE001
            return e, log

    return run


def test_phase_machine_fails_loudly_when_graph_unavailable(mock_run, no_sleep):
    got, log = mock_run("shared_origin_scan", err_502())
    assert isinstance(got, GraphUnavailable), got
    assert not (log.root / "answer.json").exists()


def test_phase_machine_non_transient_error_still_completes(mock_run, no_sleep):
    got, log = mock_run("shared_origin_scan", err_text(RuntimeError("vertex 3503211 does not exist")))
    assert isinstance(got, dict), got
    assert (log.root / "answer.json").exists()
    assert no_sleep == []


# ------------------------------------------------------------------------------------------------ checker follow-ups
def test_resume_loop_wall_clock_cap(monkeypatch, no_sleep):
    """A stalled attempt (error '' after the full aiohttp deadline) counts against the budget in wall-clock time,
    not only through the sleeps: one 150 s stall already exceeds TG_RESUME_MAX_WAIT_S=120, so no second attempt."""
    monkeypatch.setenv("TG_RESUME_MAX_WAIT_S", "120")
    now = [0.0]
    monkeypatch.setattr(mcp_client, "_clock", lambda: now[0])

    class Stalling(StubSession):
        async def call_tool(self, name, arguments=None, **kw):
            now[0] += 150.0                      # each attempt burns the aiohttp deadline
            return await super().call_tool(name, arguments, **kw)

    s = Stalling([err_text(TimeoutError())])
    with pytest.raises(GraphUnavailable, match="connection stalled"):
        asyncio.run(run_query(s, "card_profile", {}))
    assert s.calls == 1 and no_sleep == []


def test_call_query_tool_short_circuits_once_graph_unavailable(no_sleep):
    """After one GraphUnavailable the case is already lost: later P2 calls must not each spin the resume budget."""
    tctx = ToolContext(opened_at="2016-11-22 20:11:00")
    tctx.budget.limit = 5
    tctx.graph_unavailable = "shared_origin_scan"
    s = StubSession([ok_result([{"a": 1}])])
    out = asyncio.run(call_query_tool(s, tctx, "card_profile", {}))
    assert "GRAPH TEMPORARILY UNAVAILABLE" in out and s.calls == 0 and tctx.budget.used == 0 and tctx.facts == {}


def test_local_tools_flag_graph_unavailable(monkeypatch, no_sleep):
    """find_similar_cases / grounding_chunks (P2 local tools) follow the call_query_tool contract: flag the case,
    refund the unit, never hand the failure to the model as an ordinary result."""
    monkeypatch.setenv("TG_RESUME_MAX_WAIT_S", "1")
    from agent import tools_local

    tctx = ToolContext(opened_at="2016-11-22 20:11:00")
    tctx.budget.limit = 5
    s = StubSession([err_502()])
    tools_local.bind(s, tctx)
    monkeypatch.setattr(tools_local, "embed_query", lambda text: [0.0] * 4)
    ctx = SimpleNamespace(case_id="HHG-014", card_id="C1-K1", customer_id="C1", opened_at="2016-11-22 20:11:00")
    tools = {t.name: t for t in tools_local.make_local_tools(ctx, tctx)}
    out = asyncio.run(tools["find_similar_cases"].call({"query_text": "x", "device_id": "", "addr1": "", "pattern_sig": ""}))
    assert "GRAPH TEMPORARILY UNAVAILABLE" in str(out)
    assert tctx.graph_unavailable == "find_similar_cases" and tctx.budget.used == 0
    calls = s.calls
    out = asyncio.run(tools["grounding_chunks"].call({"query_text": "x", "doc_filter": "", "kind_filter": ""}))
    assert "GRAPH TEMPORARILY UNAVAILABLE" in str(out) and s.calls == calls   # short-circuited, no new call


def test_phase_machine_open_case_graph_unavailable_fails_fast(mock_run, no_sleep):
    """P0: the open_case writer no longer swallows GraphUnavailable (it would then spin again on case_context)."""
    got, log = mock_run("open_case", err_502())
    assert isinstance(got, GraphUnavailable), got
    assert not (log.root / "answer.json").exists()


def test_bench_stops_after_graph_unavailable(monkeypatch, tmp_path):
    """A workspace that stays down fails one case, then the run stops (every later case would spin the same budget)."""
    from agent import bench
    from agent.phase_machine import PhaseMachine

    settings = replace(bench.SETTINGS, run_mode="mock", llm_backend="mock", runs_dir=tmp_path)
    if not settings.case_pack_csv.exists():
        pytest.skip("case pack not present")
    monkeypatch.setattr(bench, "SETTINGS", settings)
    monkeypatch.setenv("RUN_MODE", "mock")
    monkeypatch.setenv("LLM_BACKEND", "mock")
    seen: list[str] = []

    async def down(self, ctx, log):
        seen.append(ctx.case_id)
        raise GraphUnavailable("502, message='Bad Gateway'")

    monkeypatch.setattr(PhaseMachine, "run_case", down)
    rc = bench.main(["--run-id", "t-down", "--cases", "all", "--mock", "--llm-backend", "mock"])
    assert rc == 1 and len(seen) == 1
