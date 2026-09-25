"""agent/llm.py — the LLM backend seam (LLM_BACKEND = cli | api | mock).

No network and no CLI subprocess: these tests cover selection, the usage shim that feeds
`RunLog.record_llm`, and the fact that the cli backend wraps the *same* typed tool objects the
api backend runs, so a graph call is recorded identically whichever backend is in play.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent import llm  # noqa: E402
from agent.config import Settings  # noqa: E402
from agent.runlog import RunLog  # noqa: E402


def test_resolve_backend_defaults_to_cli_and_to_mock_under_run_mode_mock():
    assert llm.resolve_backend(Settings(run_mode="live", llm_backend="")) == "cli"
    assert llm.resolve_backend(Settings(run_mode="mock", llm_backend="")) == "mock"
    # explicit wins both ways: a live model over mock (DuckDB) facts, or the API SDK
    assert llm.resolve_backend(Settings(run_mode="mock", llm_backend="cli")) == "cli"
    assert llm.resolve_backend(Settings(run_mode="live", llm_backend="API")) == "api"
    assert llm.resolve_backend(Settings(run_mode="live", llm_backend="mock")) == "mock"


def test_make_backend_mock_needs_no_api_key():
    b = llm.make_backend(Settings(run_mode="mock", llm_backend="mock", anthropic_api_key=""))
    assert isinstance(b, llm.ApiBackend) and b.name == "mock"
    assert type(b.client).__name__ == "FakeAnthropic"


def test_make_backend_cli_is_the_default_and_builds_no_anthropic_client():
    b = llm.make_backend(Settings(run_mode="live", llm_backend="", anthropic_api_key=""))
    assert isinstance(b, llm.CliBackend) and b.name == "cli"
    assert not hasattr(b, "client")


def test_wrap_client_passes_a_backend_through_and_adapts_a_raw_client():
    b = llm.CliBackend(Settings())
    assert llm.wrap_client(b) is b
    raw = object()
    assert isinstance(llm.wrap_client(raw, Settings(run_mode="mock")), llm.ApiBackend)


def test_usage_shim_feeds_runlog_record_llm(tmp_path):
    class R:
        usage = {"input_tokens": 7, "output_tokens": 11, "cache_read_input_tokens": 100,
                 "cache_creation_input_tokens": 5}
        session_id = "s-1"
        stop_reason = "end_turn"

    log = RunLog("r", "HHG-014", tmp_path / "r" / "HHG-014")
    log.record_llm("P4", llm._usage_response("P4", R(), "claude-sonnet-5"))
    assert log.tokens == 7 + 11 + 100 + 5
    assert log.cache_read_tokens == 100 and log.llm_calls == 1


def test_usage_shim_records_zero_when_the_cli_reports_nothing(tmp_path):
    class R:
        usage = None
        session_id = ""
        stop_reason = None

    log = RunLog("r", "HHG-014", tmp_path / "r2" / "HHG-014")
    log.record_llm("P9", llm._usage_response("P9", R(), "m"))
    assert log.tokens == 0


def test_sdk_tool_wraps_the_same_typed_tool_body():
    from anthropic.lib.tools import BetaAsyncFunctionTool

    async def region_history(c: str) -> str:
        return '{"rows": []}'

    t = BetaAsyncFunctionTool(region_history, name="region_history", description="d",
                              input_schema={"type": "object", "properties": {"c": {"type": "string"}},
                                            "required": ["c"], "additionalProperties": False})
    wrapped = llm._sdk_tool(t)
    assert wrapped.name == "region_history"
    out = asyncio.run(wrapped.handler({"c": "C1"}))
    assert out["content"][0]["text"] == '{"rows": []}'


def test_sdk_tool_turns_a_failure_into_a_tool_result_instead_of_killing_the_loop():
    from anthropic.lib.tools import BetaAsyncFunctionTool

    async def ring_profile(c: str) -> str:
        raise RuntimeError("boom")

    t = BetaAsyncFunctionTool(ring_profile, name="ring_profile", description="d",
                              input_schema={"type": "object", "properties": {"c": {"type": "string"}},
                                            "required": ["c"], "additionalProperties": False})
    out = asyncio.run(llm._sdk_tool(t).handler({"c": "C1"}))
    assert "ring_profile failed" in out["content"][0]["text"]
