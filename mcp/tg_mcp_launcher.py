"""tigergraph-mcp 1.0.3 stdio launcher with a real query timeout.

Why this file exists (PLAN §3.6, research gap3): the MCP's `run_installed_query`
handler is literally `await conn.runInstalledQuery(query_name, params or {})`, so
no GSQL-TIMEOUT / RESPONSE-LIMIT header is ever sent and the server's 16 s default
applies to every agent query. pyTigerGraph has a connection-wide default header
(`conn.responseConfigHeader`, set by `await conn.customizeHeader(...)`, merged into
every request by `_prep_req`). tigergraph-mcp never calls it, but
`ConnectionManager._connection_pool` is a plain class-level dict and
`_build_or_reuse()` returns `pool[profile]` when present, so pre-populating the
pool with a customised `AsyncTigerGraphConnection` gives every MCP-invoked query
the timeout we want without forking the package.

Run:   uv run python mcp/tg_mcp_launcher.py           (stdio; the agent spawns it)
Env:   TG_HOST, TG_GRAPHNAME, TG_SECRET (or TG_USERNAME/TG_PASSWORD/TG_API_TOKEN),
       TG_QUERY_TIMEOUT_MS (default 120000), TG_RESPONSE_LIMIT_BYTES (default 64000000),
       TG_ALLOWED_TOOLS / TG_BLOCKED_TOOLS (see mcp/tools_allowlist.txt), TG_LOG_TOOL_CALLS.

Verified against tigergraph-mcp 1.0.3 source: tigergraph_mcp/__init__.py exports
`serve`, `ConnectionManager`, `get_connection`; connection_manager.py defines
`ConnectionManager._connection_pool: Dict[str, AsyncTigerGraphConnection] = {}` and
`set_default_connection()`; tool_filter.configure() reads TG_ALLOWED_TOOLS /
TG_BLOCKED_TOOLS; server.serve("stdio") runs `_serve_stdio()` and closes the pool.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path


def _load_env() -> None:
    """Load .env from the repo root (and cwd) without overriding real env vars."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv is a tigergraph-mcp dependency, but be safe
        return
    here = Path(__file__).resolve().parent
    for candidate in (here.parent / ".env", Path.cwd() / ".env"):
        if candidate.exists():
            load_dotenv(candidate, override=False)


def build_connection():
    """Build the AsyncTigerGraphConnection the MCP tools will share.

    Mirrors `_build_or_reuse()` in tigergraph_mcp/connection_manager.py so the
    connection is configured exactly as the server would have built it.
    """
    from pyTigerGraph import AsyncTigerGraphConnection

    host = os.environ.get("TG_HOST", "http://127.0.0.1")
    graphname = os.environ.get("TG_GRAPHNAME", "FraudGraph")
    secret = os.environ.get("TG_SECRET", "")
    return AsyncTigerGraphConnection(
        host=host,
        graphname=graphname,
        username=os.environ.get("TG_USERNAME", "tigergraph"),
        password=os.environ.get("TG_PASSWORD", "tigergraph"),
        gsqlSecret=secret,
        apiToken=os.environ.get("TG_API_TOKEN", ""),
        jwtToken=os.environ.get("TG_JWT_TOKEN", ""),
        restppPort=os.environ.get("TG_RESTPP_PORT", "9000"),
        gsPort=os.environ.get("TG_GS_PORT", "14240"),
        sslPort=os.environ.get("TG_SSL_PORT", "443"),
        tgCloud=os.environ.get("TG_TGCLOUD", "true" if host.startswith("https://") else "false").lower() == "true",
        certPath=os.environ.get("TG_CERT_PATH") or None,
    )


async def prepare(timeout_ms: int | None = None, response_bytes: int | None = None):
    """Create the connection, set the headers and register it with the MCP.

    Returns the connection so a unit test can assert
    `get_connection().responseConfigHeader["GSQL-TIMEOUT"] == "120000"`.
    """
    from tigergraph_mcp import ConnectionManager, tool_filter
    from tigergraph_mcp.tools import get_all_tools

    timeout_ms = int(timeout_ms if timeout_ms is not None else os.environ.get("TG_QUERY_TIMEOUT_MS", "120000"))
    response_bytes = int(
        response_bytes if response_bytes is not None else os.environ.get("TG_RESPONSE_LIMIT_BYTES", "64000000")
    )

    # Discover profiles the same way main.py does (also loads .env through dotenv).
    ConnectionManager.load_profiles()

    conn = build_connection()
    # Connection-wide GSQL-TIMEOUT (ms) + RESPONSE-LIMIT (bytes) on EVERY request the
    # MCP makes through this connection. The aiohttp deadline becomes timeout/1000 + 30 s.
    await conn.customizeHeader(timeout=timeout_ms, responseSize=response_bytes)

    # Pre-populate the pool: _build_or_reuse() returns pool["default"] when present.
    ConnectionManager._connection_pool["default"] = conn
    ConnectionManager.set_default_connection(conn)

    # Server-side tool selection by NAME (never `destructive`, which removes
    # run_installed_query). Honours TG_ALLOWED_TOOLS / TG_BLOCKED_TOOLS.
    tool_filter.configure()
    served = get_all_tools()  # validates selectors at startup like main.py does
    if not served:
        raise SystemExit("The configured tool selection leaves no tools to serve.")
    logging.getLogger(__name__).info(
        "tg_mcp_launcher: serving %d tools, GSQL-TIMEOUT=%s RESPONSE-LIMIT=%s",
        len(served), conn.responseConfigHeader.get("GSQL-TIMEOUT"), conn.responseConfigHeader.get("RESPONSE-LIMIT"),
    )
    return conn


async def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)
    _load_env()
    from tigergraph_mcp import call_log, serve

    call_log.configure()  # TG_LOG_TOOL_CALLS=1 -> one stderr line per tool call
    await prepare()
    await serve(transport="stdio")


if __name__ == "__main__":
    asyncio.run(main())
