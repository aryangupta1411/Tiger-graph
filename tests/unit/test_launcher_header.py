"""The launcher must give every MCP-invoked query a real GSQL-TIMEOUT / RESPONSE-LIMIT
(PLAN §3.6 unit test), and the tool filter must keep run_installed_query while
removing the destructive tools by name. No network: the connection object is built
but never used.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "mcp" / "tg_mcp_launcher.py"

# M10: the whole file needs the tigergraph-mcp server package (and pyTigerGraph through it). CI installs the
# agent extra; a bare checkout does not, and the suite must stay green there. `make test-agent` keeps it
# mandatory locally. No network is used either way: the connection object is built but never called.
pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("tigergraph_mcp") is None or not LAUNCHER.exists(),
    reason="needs the tigergraph-mcp package and mcp/tg_mcp_launcher.py",
)


def _load_launcher():
    spec = importlib.util.spec_from_file_location("tg_mcp_launcher", LAUNCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from agent.mcp_client import launcher_env
    env = launcher_env(read_only=False)
    for k, v in env.items():
        if k.startswith("TG_"):
            monkeypatch.setenv(k, v)
    monkeypatch.setenv("TG_HOST", "https://example.i.tgcloud.io")
    monkeypatch.setenv("TG_GRAPHNAME", "FraudGraph")
    monkeypatch.setenv("TG_SECRET", "not-a-real-secret")
    monkeypatch.setenv("TG_QUERY_TIMEOUT_MS", "120000")
    monkeypatch.setenv("TG_RESPONSE_LIMIT_BYTES", "64000000")
    yield
    from tigergraph_mcp import ConnectionManager
    ConnectionManager._connection_pool.clear()
    ConnectionManager.set_default_connection(None)


def test_launcher_sets_connection_wide_headers():
    from tigergraph_mcp import ConnectionManager, get_connection
    mod = _load_launcher()
    conn = asyncio.run(mod.prepare())
    assert ConnectionManager._connection_pool["default"] is conn
    got = get_connection()                       # what every MCP tool calls
    assert got is conn
    assert got.responseConfigHeader["GSQL-TIMEOUT"] == "120000"
    assert got.responseConfigHeader["RESPONSE-LIMIT"] == "64000000"
    assert got.graphname == "FraudGraph"
    # the header is merged into every request by pyTigerGraph's _prep_req(authMode, headers, url, method, data)
    prepped = got._prep_req("token", None, "https://example.i.tgcloud.io/restpp/echo", "GET", None)
    headers = prepped[0] if isinstance(prepped, tuple) else prepped
    assert headers.get("GSQL-TIMEOUT") == "120000"
    assert headers.get("RESPONSE-LIMIT") == "64000000"


def test_tool_filter_keeps_run_installed_query_and_blocks_destructive():
    from tigergraph_mcp import tool_filter
    from tigergraph_mcp.tools import get_all_tools

    from agent.mcp_client import read_allowlist
    tool_filter.configure()   # from TG_ALLOWED_TOOLS / TG_BLOCKED_TOOLS set by the fixture
    served = {t.name for t in get_all_tools()}
    al = read_allowlist()
    assert "tigergraph__run_installed_query" in served
    for w in al["write"]:
        assert f"tigergraph__{w}" in served
    for b in al["blocked"]:
        assert f"tigergraph__{b}" not in served
    assert "tigergraph__gsql" not in served and "tigergraph__run_query" not in served


def test_read_only_session_has_no_writers(monkeypatch):
    from tigergraph_mcp import tool_filter
    from tigergraph_mcp.tools import get_all_tools

    from agent.mcp_client import launcher_env
    env = launcher_env(read_only=True)
    monkeypatch.setenv("TG_ALLOWED_TOOLS", env["TG_ALLOWED_TOOLS"])
    monkeypatch.setenv("TG_BLOCKED_TOOLS", env["TG_BLOCKED_TOOLS"])
    tool_filter.configure()
    served = {t.name for t in get_all_tools()}
    assert "tigergraph__run_installed_query" in served
    assert not any(n.startswith("tigergraph__add_") for n in served)


def test_destructive_selector_would_remove_run_installed_query():
    """Documents why the allowlist is by name: `destructive` includes run_installed_query."""
    from tigergraph_mcp.tool_annotations import DESTRUCTIVE
    assert "tigergraph__run_installed_query" in DESTRUCTIVE
