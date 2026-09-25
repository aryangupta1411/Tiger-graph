"""graph/tg.py — the one place the offline graph scripts get a pyTigerGraph 2.0.4 connection from.

    from graph.tg import connect, ensure_awake, gsql, run_query, REPO, DATA_OUT

* connect()      TigerGraphConnection(host, graphname, gsqlSecret) + getToken(secret) + customizeHeader(...)
                 (pyTigerGraph sets tgCloud=True / port 443 itself when the host contains "tgcloud").
* ensure_awake() retry decorator for a resuming Savanna workspace: 502/503/504, connection errors, read
                 timeouts and an HTML start page in place of JSON are retried with 1, 2, 4, ... 60 s sleeps until
                 TG_RESUME_MAX_WAIT_S (240 s). GSQL compile errors, 401/403, file errors are raised immediately.
* gsql()         conn.gsql(text) + error detection: pyTigerGraph only raises on a few statement kinds
                 (_parse_gsql: CREATE VERTEX / EDGE / GRAPH / LOADING JOB, RUN LOADING JOB); everything else
                 (SCHEMA_CHANGE, INSTALL, DROP, RUN QUERY) comes back as text, so we grep it. An HTML page in
                 place of the reply (a resuming workspace) is raised as ConnectionError and retried.
* run_query()    runInstalledQuery(name, params, timeout=<ms>, usePost=True) under ensure_awake().

ops/ensure_awake.py (module G) implements the same decorator for the agent and UI; the graph scripts carry
their own copy so `uv run python graph/load_all.py` works in a checkout that has only graph/ + data/.
Keep is_transient / _transient_text / backoff_delays identical in both files (tests/unit/test_ensure_awake.py
runs the same classification cases against both).
"""
from __future__ import annotations

import functools
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

log = logging.getLogger("graph.tg")

REPO = Path(os.environ.get("HHGOA_REPO_ROOT", Path(__file__).resolve().parent.parent))
DATA_OUT = Path(os.environ.get("HHGOA_DATA_OUT", REPO / "data" / "out"))
GRAPH_DIR = Path(__file__).resolve().parent

# No bare "502"/"503"/"504" here: they would match dataset ids such as 3503211 or C15034 (see _STATUS_RX).
TRANSIENT_MARKERS = (
    "bad gateway", "service unavailable", "gateway time-out", "gateway timeout",
    "connection refused", "connection reset", "connection aborted", "remote end closed",
    "temporarily unavailable", "max retries exceeded", "read timed out", "timed out",
    "workspace is resuming", "workspace is not ready", "server disconnected", "cannot connect to host",
)
# A stand-alone 502 / 503 / 504 ("502 Server Error", "502, message='Bad Gateway'", "HTTP 503"), never a
# digit run inside an id or a number ("3503211", "C15034", "0.502", "502ms").
_STATUS_RX = re.compile(r"(?<![\w.])50[234](?![\w.])")
_NON_TRANSIENT_OS = (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError)

# Substrings that mark a failed GSQL statement in the server's text reply.
GSQL_ERROR_MARKERS = (
    "syntax error", "semantic check fails", "semantic check error", "encountered \"", "failed to", "error:",
    "does not exist", "is not defined", "not found", "cannot find", "permission denied", "unauthorized",
    "is used by", "already exists", "invalid", "unsupported", "exception",
)
# ... except when the reply also says one of these (e.g. DROP QUERY on a missing query is fine).
GSQL_OK_MARKERS = ("successfully", "is successful", "query installation finished", "installed",
                   "the job add_vectors", "schema change job", "dropped", "graph fraudgraph")


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, _NON_TRANSIENT_OS):
        return False
    try:
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
    try:  # aiohttp, when installed (same classification as ops/ensure_awake.py)
        import aiohttp  # type: ignore

        if isinstance(exc, (aiohttp.ClientConnectionError, aiohttp.ServerDisconnectedError)):
            return True
        if isinstance(exc, aiohttp.ClientResponseError) and exc.status:
            return exc.status in (502, 503, 504)
    except ImportError:  # pragma: no cover
        pass
    if isinstance(exc, (ConnectionError, TimeoutError)):
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
    total, delay = 0.0, first
    while total < max_wait_s:
        step = min(delay, cap, max_wait_s - total)
        if step <= 0:
            return
        yield step
        total += step
        delay = min(delay * 2, cap)


def ensure_awake(max_wait_s: float | None = None, sleep: Callable[[float], Any] | None = None):
    """Retry a sync callable while the Savanna workspace auto-resumes (first request is dropped with 502).

    ``sleep`` defaults to ``time.sleep`` looked up at call time (so a monkeypatched ``time.sleep`` applies)."""
    budget = float(os.getenv("TG_RESUME_MAX_WAIT_S", "240")) if max_wait_s is None else float(max_wait_s)

    def decorate(fn: Callable):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            delays = backoff_delays(budget)
            attempt = 0
            while True:
                try:
                    return fn(*args, **kwargs)
                except BaseException as exc:  # noqa: BLE001 - classified by is_transient
                    if not is_transient(exc):
                        raise
                    delay = next(delays, None)
                    if delay is None:
                        log.error("%s: workspace still unreachable after %.0fs: %s", fn.__name__, budget, exc)
                        raise
                    attempt += 1
                    log.warning("%s: transient error (%s); retry %d in %.0fs", fn.__name__, str(exc)[:160], attempt, delay)
                    (sleep or time.sleep)(delay)

        return wrapper

    return decorate


def env(name: str, default: str | None = None) -> str:
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO / ".env")
    except ImportError:  # pragma: no cover
        pass
    v = os.environ.get(name, default)
    if v is None:
        raise RuntimeError(f"{name} is not set (see .env.example)")
    return v


EMPTY_SECRET_MSG = "TG_SECRET is empty in .env - paste the Savanna Database Secret"


def require_secret(secret: str | None) -> str:
    """Fail before any network call: an empty secret would wake (and bill) a suspended workspace for a 401."""
    if not (secret or "").strip():
        raise RuntimeError(EMPTY_SECRET_MSG)
    return secret  # type: ignore[return-value]


@ensure_awake()
def _token(conn, secret: str) -> None:
    conn.getToken(secret)


def connect(graphname: str | None = None, timeout_ms: int | None = None, response_bytes: int = 64_000_000,
            token: bool = True):
    """pyTigerGraph 2.0.4 sync connection from .env (TG_HOST, TG_GRAPHNAME, TG_SECRET) with a token.

    timeout_ms goes into the connection-wide GSQL-TIMEOUT header (loads/algos: 600000; queries: 120000).
    token=False skips getToken(): conn.gsql() authenticates with Basic "__GSQL__secret:<secret>" (pyTigerGraph
    common/base.py), so DDL runs before the graph exists; REST++ calls (loads, counts, queries) need the token.
    """
    from pyTigerGraph import TigerGraphConnection

    host = env("TG_HOST")
    graph = graphname or env("TG_GRAPHNAME", "FraudGraph")
    secret = env("TG_SECRET")
    require_secret(secret)          # before any request, token=False included
    conn = TigerGraphConnection(host=host, graphname=graph, gsqlSecret=secret)
    ms = int(env("TG_LOAD_TIMEOUT_MS", "600000")) if timeout_ms is None else int(timeout_ms)
    conn.customizeHeader(timeout=ms, responseSize=response_bytes)
    if token:
        _token(conn, secret)
    return conn


@ensure_awake()
def version(conn) -> str:
    return conn.getVer()


def gsql_failed(reply: str) -> bool:
    """True when a GSQL text reply looks like an error (pyTigerGraph does not raise for most statements)."""
    low = str(reply).lower()
    if any(m in low for m in GSQL_ERROR_MARKERS) and not any(m in low for m in GSQL_OK_MARKERS):
        return True
    # a mixed reply (some statements ok, one failed) still contains the hard markers
    return "syntax error" in low or "semantic check" in low or "failed to" in low


def html_guard(reply: Any, endpoint: str = "/gsql/v1/statements") -> Any:
    """conn.gsql() uses skipCheck=True / jsonResponse=False, so a resuming workspace's HTML start page comes back
    as the GSQL "reply" and would read as a successful statement. Raise it as a (transient) ConnectionError."""
    if str(reply).lstrip()[:200].lower().startswith(("<!doctype", "<html")):
        raise ConnectionError(f"workspace is resuming: HTML reply from {endpoint}")
    return reply


def gsql(conn, text: str, ok_if: tuple[str, ...] = (), quiet: bool = False) -> str:
    """Run GSQL text with resume-retry and fail loudly on a compile/DDL error."""

    @ensure_awake()
    def _run():
        return html_guard(conn.gsql(text))

    reply = str(_run())
    if not quiet:
        log.info("gsql> %s\n%s", text.strip().splitlines()[0][:100], reply[-1200:])
    if any(k.lower() in reply.lower() for k in ok_if):
        return reply
    if gsql_failed(reply):
        raise RuntimeError(f"GSQL error:\n{reply[-3000:]}")
    return reply


def run_query(conn, name: str, params: dict | None = None, timeout_ms: int = 600_000) -> list:
    @ensure_awake()
    def _run():
        return conn.runInstalledQuery(name, params or {}, timeout=timeout_ms, usePost=True)

    return _run()


def merged(results: list) -> dict:
    """Merge the list of PRINT objects returned by runInstalledQuery into one dict."""
    out: dict = {}
    for r in results or []:
        if isinstance(r, dict):
            out.update(r)
    return out
