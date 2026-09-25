"""Savanna auto-resume hygiene (PLAN §3.6): one shared retry decorator.

Savanna Free-tier workspaces auto-stop when idle and auto-start on the first
request, which is *dropped* (502 Bad Gateway / connection error) while the
workspace resumes (research/gap2 "Auto-resume drops the first request").
Every component that talks to TigerGraph wraps its calls with
``@ensure_awake()`` — the loader (graph/load_all.py), the agent's MCP session
(agent/mcp_client.py), keep_alive.py and the Streamlit UI (ui/common.py).

Backoff: 1, 2, 4, 8, 16, 32, 60, 60 ... seconds, until ``max_wait_s``
(default 240 s = 4 min, PLAN §3.6) is exhausted. Only transient errors are
retried; a GSQL compile error or a 401 is raised immediately.

Usage::

    from ops.ensure_awake import ensure_awake, warm_up, tg_connection

    conn = tg_connection()            # from .env: TG_HOST / TG_GRAPHNAME / TG_SECRET
    warm_up(conn)                     # blocks until getVer() answers (workspace awake)

    @ensure_awake()
    def load_chunk(path): ...

    @ensure_awake(max_wait_s=120)
    async def call_tool(session, name, params): ...
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from typing import Any

log = logging.getLogger("ops.ensure_awake")

# Substrings (lower-cased) that identify a resuming / unreachable workspace.
# The bare status codes are NOT here: "503" would match dataset ids such as 3503211 or C15034, and a
# non-transient error quoting one would then be retried for the whole resume budget. See _STATUS_RX.
TRANSIENT_MARKERS: tuple[str, ...] = (
    "bad gateway",
    "service unavailable",
    "gateway time-out",
    "gateway timeout",
    "connection refused",
    "connection reset",
    "connection aborted",
    "remote end closed",
    "temporarily unavailable",
    "max retries exceeded",
    "read timed out",
    "timed out",
    "workspace is resuming",
    "workspace is not ready",
    "server disconnected",
    "cannot connect to host",
)

# A stand-alone 502 / 503 / 504 ("502 Server Error", "502, message='Bad Gateway'", "HTTP 503"), never a
# digit run inside an id or a number ("3503211", "C15034", "0.502", "502ms").
_STATUS_RX = re.compile(r"(?<![\w.])50[234](?![\w.])")

# Errors that must never be retried even though they subclass OSError.
_NON_TRANSIENT_OS = (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError)


def is_transient(exc: BaseException) -> bool:
    """True when ``exc`` looks like a resuming / unreachable Savanna workspace."""
    if isinstance(exc, _NON_TRANSIENT_OS):
        return False
    try:  # requests (pyTigerGraph sync client)
        import requests  # type: ignore

        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            return True
        if isinstance(exc, requests.HTTPError):
            resp = getattr(exc, "response", None)
            status = getattr(resp, "status_code", 0) if resp is not None else 0
            if status:  # a real status decides: 401 / 403 / 404 are never retried, whatever the text says
                return status in (502, 503, 504)
    except ImportError:  # pragma: no cover
        pass
    try:  # aiohttp (pyTigerGraph async client used by tigergraph-mcp)
        import aiohttp  # type: ignore

        if isinstance(exc, (aiohttp.ClientConnectionError, aiohttp.ServerDisconnectedError)):
            return True
        if isinstance(exc, aiohttp.ClientResponseError) and exc.status:
            return exc.status in (502, 503, 504)
    except ImportError:  # pragma: no cover
        pass
    if isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError)):
        return True
    return _transient_text(str(exc))


def _transient_text(text: str) -> bool:
    """The message-based half of ``is_transient`` (401 / 403 / auth errors are never transient)."""
    msg = text.lower()
    # pyTigerGraph common/base.py raises TigerGraphException("Cannot parse json: " + body) when a 2xx reply is
    # not JSON - a resuming workspace's HTML start page. str(exc) of a TigerGraphException(msg, code) is a
    # tuple repr, hence the "('cannot parse json" form.
    if (msg.startswith("cannot parse json") or "('cannot parse json" in msg) and ("<html" in msg or "<!doctype" in msg):
        return True
    if _STATUS_RX.search(msg):
        return True
    return any(marker in msg for marker in TRANSIENT_MARKERS)


def backoff_delays(max_wait_s: float, first: float = 1.0, cap: float = 60.0) -> Iterator[float]:
    """Yield sleep lengths 1, 2, 4 ... capped at ``cap``, whose sum never exceeds ``max_wait_s``."""
    total = 0.0
    delay = first
    while total < max_wait_s:
        step = min(delay, cap, max_wait_s - total)
        if step <= 0:
            return
        yield step
        total += step
        delay = min(delay * 2, cap)


def ensure_awake(
    max_wait_s: float | None = None,
    on_retry: Callable[[int, float, BaseException], None] | None = None,
    sleep: Callable[[float], Any] | None = None,
):
    """Decorator: retry a sync or async callable while the workspace resumes.

    ``max_wait_s`` defaults to ``TG_RESUME_MAX_WAIT_S`` from the environment (240).
    ``on_retry(attempt, delay, exc)`` is called before each sleep (the UI shows a spinner).
    ``sleep`` is injectable for tests (sync path only; async uses ``asyncio.sleep``); when omitted,
    ``time.sleep`` is looked up at call time so a monkeypatched ``time.sleep`` takes effect.
    """
    budget = float(os.getenv("TG_RESUME_MAX_WAIT_S", "240")) if max_wait_s is None else float(max_wait_s)

    def decorate(fn: Callable):
        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                attempt = 0
                delays = backoff_delays(budget)
                while True:
                    try:
                        return await fn(*args, **kwargs)
                    except BaseException as exc:  # classified below; non-transient errors re-raise
                        if not is_transient(exc):
                            raise
                        delay = next(delays, None)
                        if delay is None:
                            log.error("%s: workspace still unreachable after %.0fs: %s", fn.__name__, budget, exc)
                            raise
                        attempt += 1
                        log.warning("%s: transient error (%s); retry %d in %.0fs", fn.__name__, exc, attempt, delay)
                        if on_retry:
                            on_retry(attempt, delay, exc)
                        await asyncio.sleep(delay)

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            attempt = 0
            delays = backoff_delays(budget)
            while True:
                try:
                    return fn(*args, **kwargs)
                except BaseException as exc:  # classified below; non-transient errors re-raise
                    if not is_transient(exc):
                        raise
                    delay = next(delays, None)
                    if delay is None:
                        log.error("%s: workspace still unreachable after %.0fs: %s", fn.__name__, budget, exc)
                        raise
                    attempt += 1
                    log.warning("%s: transient error (%s); retry %d in %.0fs", fn.__name__, exc, attempt, delay)
                    if on_retry:
                        on_retry(attempt, delay, exc)
                    (sleep or time.sleep)(delay)

        return sync_wrapper

    return decorate


# --------------------------------------------------------------------------- #
# Connection helpers shared by ops/, graph/ and ui/ (the agent uses the MCP).  #
# --------------------------------------------------------------------------- #
def tg_connection(graphname: str | None = None, timeout_ms: int | None = None):
    """Build a sync pyTigerGraph 2.0.4 connection from ``.env`` and fetch a token.

    Env: TG_HOST (https://<workspace>.i.tgcloud.io), TG_GRAPHNAME (FraudGraph),
    TG_SECRET (Savanna Database Secret), TG_QUERY_TIMEOUT_MS (120000).
    pyTigerGraph sets tgCloud=True and port 443 itself when the host contains "tgcloud".
    """
    from dotenv import load_dotenv
    from pyTigerGraph import TigerGraphConnection

    load_dotenv()
    host = os.environ["TG_HOST"]
    graph = graphname or os.getenv("TG_GRAPHNAME", "FraudGraph")
    secret = os.environ["TG_SECRET"]
    require_secret(secret)
    conn = TigerGraphConnection(host=host, graphname=graph, gsqlSecret=secret)
    ms = int(os.getenv("TG_QUERY_TIMEOUT_MS", "120000")) if timeout_ms is None else int(timeout_ms)
    # Connection-wide GSQL-TIMEOUT / RESPONSE-LIMIT headers (pyTigerGraph merges them into every request).
    conn.customizeHeader(timeout=ms, responseSize=64_000_000)
    _get_token(conn, secret)
    return conn


EMPTY_SECRET_MSG = "TG_SECRET is empty in .env - paste the Savanna Database Secret"


def require_secret(secret: str | None) -> str:
    """Fail before any network call: an empty secret would wake (and bill) a suspended workspace for a 401."""
    if not (secret or "").strip():
        raise RuntimeError(EMPTY_SECRET_MSG)
    return secret  # type: ignore[return-value]


@ensure_awake()
def _get_token(conn, secret: str) -> None:
    conn.getToken(secret)


@ensure_awake()
def warm_up(conn) -> str:
    """Block until the workspace answers a cheap request; returns the product version string."""
    ver = conn.getVer()
    log.info("TigerGraph awake, version %s", ver)
    return ver


def run_query(conn, name: str, params: dict | None = None, timeout_ms: int | None = None) -> list:
    """``runInstalledQuery`` with retry and an explicit GSQL-TIMEOUT (ms)."""
    ms = int(os.getenv("TG_QUERY_TIMEOUT_MS", "120000")) if timeout_ms is None else int(timeout_ms)

    @ensure_awake()
    def _run():
        return conn.runInstalledQuery(name, params or {}, timeout=ms, usePost=True)

    return _run()
