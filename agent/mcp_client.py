"""MCP client for the agent (contracts §C `agent/mcp_client.py`).

- `open_session(read_only)` spawns `mcp/tg_mcp_launcher.py` over stdio through the
  official `mcp` ClientSession (mcp 2.2.0) and lists the served tools.
- `make_query_tools(...)` turns the installed read queries described in
  `mcp/query_descriptions.yaml` into typed Anthropic tools
  (`anthropic.lib.tools.BetaAsyncFunctionTool`, anthropic 1.7.0) so the model picks
  a query BY NAME with a schema-checked parameter set. The raw
  `tigergraph__run_installed_query` tool is never given to the model.
- `run_query(session, name, params)` calls `tigergraph__run_installed_query`, parses
  the MCP formatter's ```json block, merges the printed JSON objects into one dict,
  and logs the call to the active RunLog. A transient failure (Savanna auto-resume:
  502/503/504, connection error, HTML start page, or the empty error of a stalled
  aiohttp request) is retried with `ops.ensure_awake.backoff_delays` for up to
  TG_RESUME_MAX_WAIT_S; when the budget runs out it raises `GraphUnavailable`, which
  the phase machine never treats as evidence. `call_tool_with_resume` does the same
  for the non-query tools (`tigergraph__add_edges`).
- Every tool result becomes `Evidence` rows through `evidence_from_result()` (ids
  extracted by code, never by the model).

Parameter encoding (contracts §A): VERTEX<T> → {"id": "..."}; DATETIME → "YYYY-MM-DD HH:MM:SS";
INT/FLOAT/STRING/BOOL → JSON scalars; LIST<FLOAT> → JSON array.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import sys
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.config import SETTINGS, Settings
from agent.engine_api import Evidence
from agent.runlog import current_runlog

RUN_INSTALLED = "tigergraph__run_installed_query"

# ---------------------------------------------------------------- query registry

# Fallback description of the installed read queries (contracts §A). The file
# mcp/query_descriptions.yaml (written by the queries module) overrides/extends it.
_DT = "DATETIME"
BUILTIN_QUERIES: dict[str, dict] = {
    "case_context": {"description": "First look at the flagged transaction: its attributes (amount, channel, region, emails, device_new, proxy, cms_p, prior_* baselines, ring_hit, burst_id), the card (modal region, n_txns, ring_id), the customer and the device profile (strength, fan-out, fraud cases).",
                     "parameters": {"t": {"type": "VERTEX<Transaction>", "description": "flagged transaction", "example": {"id": "3478561"}},
                                    "as_of": {"type": _DT, "description": "opened_at; only ts <= as_of is visible", "example": "2016-11-22 20:11:00"}}},
    "card_profile": {"description": "Card baseline attributes, last-30-day aggregates, top regions/devices/emails, the customer's other cards, prior ClosedCase and AgentCase records on the card.",
                     "parameters": {"c": {"type": "VERTEX<Card>", "description": "card", "example": {"id": "C13487-K1"}},
                                    "as_of": {"type": _DT, "description": "opened_at", "example": "2016-11-22 20:11:00"}}},
    "card_window": {"description": "Ordered transactions on the card between from_ts and to_ts (last max_rows): amount, product, channel, region, emails, risk_score, cms_p, device profile, device_new, proxy, gap_seconds; plus a summary.",
                    "parameters": {"c": {"type": "VERTEX<Card>", "description": "card", "example": {"id": "C13487-K1"}},
                                   "from_ts": {"type": _DT, "description": "window start", "example": "2016-11-19 20:11:00"},
                                   "to_ts": {"type": _DT, "description": "window end (clamped to opened_at)", "example": "2016-11-22 20:11:00"},
                                   "max_rows": {"type": "INT", "description": "row cap", "example": 60}}},
    "region_history": {"description": "How well the card knows billing region addr1: prior count/days, first/last, share, modal region, n_regions, home activity within 48h, hint home/known/rare/new.",
                       "parameters": {"c": {"type": "VERTEX<Card>", "description": "card", "example": {"id": "C12382-K1"}},
                                      "addr1": {"type": "STRING", "description": "billing region code as in the data, e.g. '444.0'", "example": "444.0"},
                                      "as_of": {"type": _DT, "description": "opened_at", "example": "2016-12-05 01:55:28"}}},
    "device_history": {"description": "Prior uses of a device profile on this card: prior_n, first_ts, device_new values seen. New phone vs new fraudster.",
                       "parameters": {"c": {"type": "VERTEX<Card>", "description": "card", "example": {"id": "C02923-K1"}},
                                      "d": {"type": "VERTEX<DeviceProfile>", "description": "device profile id (4 fields joined by ' | ')", "example": {"id": "iOS Device | iOS 11.1.2 | mobile safari 11.0 | 2208x1242"}},
                                      "as_of": {"type": _DT, "description": "opened_at", "example": "2016-12-08 03:38:37"}}},
    "device_neighbors": {"description": "Other cards on a device profile in a time window with their txn counts, proxy/new counts, and closed/agent cases touching them. Returns only the device summary when the profile is not strong.",
                         "parameters": {"d": {"type": "VERTEX<DeviceProfile>", "description": "device profile id", "example": {"id": "SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080"}},
                                        "from_ts": {"type": _DT, "description": "window start", "example": "2016-11-01 00:00:00"},
                                        "to_ts": {"type": _DT, "description": "window end", "example": "2016-11-22 20:11:00"},
                                        "max_cards": {"type": "INT", "description": "card cap", "example": 60}}},
    "email_neighbors": {"description": "Other cards sharing a recipient/purchaser email domain with this card in a window, and fraud cases among them (R6 recipient-email clause).",
                        "parameters": {"e": {"type": "VERTEX<EmailDomain>", "description": "email domain", "example": {"id": "anonymous.com"}},
                                       "c": {"type": "VERTEX<Card>", "description": "card", "example": {"id": "C10434-K1"}},
                                       "from_ts": {"type": _DT, "description": "window start", "example": "2016-11-02 00:00:00"},
                                       "to_ts": {"type": _DT, "description": "window end", "example": "2016-12-02 18:18:27"}}},
    "shared_origin_scan": {"description": "Strong device profiles and recipient email domains on the card's recent online transactions with fan-out and fraud-case counts; region_cluster_30d from the card (R6 / R2 shared clause).",
                           "parameters": {"c": {"type": "VERTEX<Card>", "description": "card", "example": {"id": "C13487-K1"}},
                                          "as_of": {"type": _DT, "description": "opened_at", "example": "2016-11-22 20:11:00"},
                                          "days": {"type": "INT", "description": "lookback days", "example": 30}}},
    "card_testing_check": {"description": "R5 detector: best run of >= min_n small online auths within window_min minutes followed by a larger purchase within lookahead_h; whether a > big purchase cleared; chain size and sub-$5 count.",
                           "parameters": {"c": {"type": "VERTEX<Card>", "description": "card", "example": {"id": "C11923-K2"}},
                                          "as_of": {"type": _DT, "description": "opened_at", "example": "2016-12-29 06:27:44"},
                                          "small": {"type": "FLOAT", "description": "small auth threshold", "example": 5.0},
                                          "window_min": {"type": "INT", "description": "window minutes", "example": 60},
                                          "min_n": {"type": "INT", "description": "min small auths", "example": 3},
                                          "big": {"type": "FLOAT", "description": "larger purchase threshold", "example": 100.0},
                                          "lookahead_h": {"type": "INT", "description": "hours after the run", "example": 48}}},
    "under_threshold_burst": {"description": "Just-under-$500 online bursts on the card (>= 4 x $450-499.99 within 40 min) with members, devices, emails, regions and look-alike cards.",
                              "parameters": {"c": {"type": "VERTEX<Card>", "description": "card", "example": {"id": "C07297-K1"}},
                                             "as_of": {"type": _DT, "description": "opened_at", "example": "2016-11-22 02:30:00"}}},
    "recurring_charge_check": {"description": "R7 detector: prior same-amount (±tol) same-product transactions per place (addr1 in person, purchaser email online) with median gap and gap CV in days.",
                               "parameters": {"t": {"type": "VERTEX<Transaction>", "description": "disputed transaction", "example": {"id": "3530164"}},
                                              "tol": {"type": "FLOAT", "description": "amount tolerance fraction", "example": 0.01},
                                              "as_of": {"type": _DT, "description": "opened_at", "example": "2016-12-10 15:01:21"}}},
    "episode_candidates": {"description": "The <= gap_h-hour-gap chain on the card containing the transaction (ts <= as_of) with cms_p, device, email, amount and signature-match flags: the candidate fraud episode.",
                           "parameters": {"t": {"type": "VERTEX<Transaction>", "description": "flagged transaction", "example": {"id": "3478561"}},
                                          "as_of": {"type": _DT, "description": "opened_at", "example": "2016-11-22 20:11:00"},
                                          "gap_h": {"type": "INT", "description": "max gap hours", "example": 48}}},
    "prior_cases_for_customer": {"description": "All closed and agent-written cases on all of the customer's cards (case memory).",
                                 "parameters": {"cu": {"type": "VERTEX<Customer>", "description": "customer", "example": {"id": "C13487"}},
                                                "as_of": {"type": _DT, "description": "opened_at", "example": "2016-11-22 20:11:00"}}},
    "ring_profile": {"description": "Profile-centric ring facts for the card: ring_id, the device summary, all wave cards (no time box), pre-open cards, the card's own transactions on the ring profile, closed cases on it.",
                     "parameters": {"c": {"type": "VERTEX<Card>", "description": "card", "example": {"id": "C13487-K1"}},
                                    "as_of": {"type": _DT, "description": "opened_at", "example": "2016-11-22 20:11:00"}}},
    # harness-only (not exposed to the model as tools)
    "post_open_activity": {"harness_only": True, "description": "Activity after opening (monitoring note, never exposure).",
                           "parameters": {"c": {"type": "VERTEX<Card>", "example": {"id": "C13487-K1"}}, "opened_at": {"type": _DT, "example": "2016-11-22 20:11:00"}, "days": {"type": "INT", "example": 7}}},
    "case_subgraph": {"harness_only": True, "description": "UI subgraph.", "parameters": {"c": {"type": "VERTEX<Card>"}, "as_of": {"type": _DT}, "hours": {"type": "INT"}}},
    "similar_prior_cases": {"harness_only": True, "description": "Vector + structural similar cases.", "parameters": {"q": {"type": "LIST<FLOAT>"}, "k": {"type": "INT"}, "card_id": {"type": "STRING"}, "customer_id": {"type": "STRING"}, "device_id": {"type": "STRING"}, "addr1": {"type": "STRING"}, "pattern_sig": {"type": "STRING"}, "as_of": {"type": _DT}}},
    "grounding_chunks": {"harness_only": True, "description": "Policy / regulation chunks.", "parameters": {"q": {"type": "LIST<FLOAT>"}, "k": {"type": "INT"}, "doc_filter": {"type": "STRING"}, "kind_filter": {"type": "STRING"}}},
    "open_case": {"harness_only": True, "writer": True, "description": "Writer.", "parameters": {}},
    # M8: the installed signature is append_case_event(case_id, seq, kind, event_at, payload) — `event_at` fills CaseEvent.at
    "append_case_event": {"harness_only": True, "writer": True, "description": "Writer.", "parameters": {"case_id": {"type": "STRING"}, "seq": {"type": "INT"}, "kind": {"type": "STRING"}, "event_at": {"type": _DT}, "payload": {"type": "STRING"}}},
    "close_case": {"harness_only": True, "writer": True, "description": "Writer.", "parameters": {}},
    "record_approval": {"harness_only": True, "writer": True, "description": "Writer.", "parameters": {}},
}

# Which evidence family each query feeds (PLAN §4.5).
QUERY_FAMILY: dict[str, str] = {
    "case_context": "history", "card_profile": "history", "card_window": "history", "region_history": "history",
    "card_testing_check": "history", "under_threshold_burst": "history", "recurring_charge_check": "history",
    "episode_candidates": "history", "post_open_activity": "history",
    "device_history": "device", "device_neighbors": "device", "email_neighbors": "device",
    "shared_origin_scan": "device", "ring_profile": "device",
    "prior_cases_for_customer": "memory", "similar_prior_cases": "memory",
    "grounding_chunks": "document", "find_similar_cases": "memory",
}

# Parameters the harness clamps to opened_at so no evidence leaks past it.
_UPPER_BOUND_PARAMS = {"as_of", "to_ts"}


def _split_type(spec_type: str) -> tuple[str, str]:
    """'VERTEX<Card> — the card as {"id": ...}' -> ('VERTEX<Card>', 'the card as ...')."""
    for sep in (" \u2014 ", " — ", " - ", ": "):
        if sep in spec_type:
            t, d = spec_type.split(sep, 1)
            return t.strip(), d.strip()
    parts = spec_type.split(None, 1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 and not spec_type.strip().upper().startswith(("LIST<", "SET<", "MAP<")) else (spec_type.strip(), "")


def load_query_descriptions(path: Path | None = None) -> dict[str, dict]:
    """Merge mcp/query_descriptions.yaml over the built-in table, defensively.

    Accepted shapes (the queries module writes the second):
      {query_name: {description, parameters: {name: {type, description, example}}}}
      {version, graph, queries: {query_name: {description, parameters: {name: 'TYPE — description'},
                                              example: {name: value}, writer: bool, mandatory: bool}}}
    Anything malformed is ignored with the built-in entry kept. Queries unknown to the
    built-in table default to harness_only (never exposed to the model unless the YAML
    says `llm: true`).
    """
    merged: dict[str, dict] = {k: {"description": v.get("description", ""), "parameters": dict(v.get("parameters", {})),
                                   "harness_only": v.get("harness_only", False), "writer": v.get("writer", False),
                                   "mandatory": k in ("case_context", "card_profile", "card_window", "prior_cases_for_customer")}
                               for k, v in BUILTIN_QUERIES.items()}
    path = path or SETTINGS.query_descriptions_yaml
    if not path or not Path(path).exists():
        return merged
    try:
        import yaml
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except Exception:
        return merged
    if isinstance(raw, dict) and isinstance(raw.get("queries"), dict):
        raw = raw["queries"]
    if not isinstance(raw, dict):
        return merged
    for name, spec in raw.items():
        if not isinstance(name, str) or not isinstance(spec, dict):
            continue
        entry = merged.setdefault(name, {"description": "", "parameters": {}, "harness_only": True, "writer": False, "mandatory": False})
        if isinstance(spec.get("description"), str) and spec["description"].strip():
            entry["description"] = " ".join(spec["description"].split())
        params = spec.get("parameters") or spec.get("params") or {}
        examples = spec.get("example") if isinstance(spec.get("example"), dict) else {}
        if isinstance(params, dict):
            clean: dict[str, dict] = {}
            for pname, pspec in params.items():
                if isinstance(pspec, str):
                    t, d = _split_type(pspec)
                    pspec = {"type": t, "description": d}
                if not isinstance(pspec, dict):
                    continue
                t = str(pspec.get("type", "STRING"))
                d = str(pspec.get("description", "") or "")
                if not d:
                    t, d = _split_type(t)
                clean[str(pname)] = {"type": t, "description": d, "example": pspec.get("example", examples.get(str(pname)))}
            if clean:
                entry["parameters"] = clean
        for flag in ("harness_only", "writer", "mandatory"):
            if flag in spec:
                entry[flag] = bool(spec[flag])
        if spec.get("llm") is True:
            entry["harness_only"] = False
    return merged


def _json_type(gsql_type: str) -> dict:
    t = gsql_type.strip().upper()
    if t.startswith("VERTEX"):
        return {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False}
    if t.startswith("SET<VERTEX") or t.startswith("LIST<VERTEX"):
        return {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False}}
    if t in ("INT", "UINT"):
        return {"type": "integer"}
    if t in ("FLOAT", "DOUBLE"):
        return {"type": "number"}
    if t == "BOOL":
        return {"type": "boolean"}
    if t.startswith("LIST<") or t.startswith("SET<"):
        inner = t[t.index("<") + 1:-1]
        return {"type": "array", "items": _json_type(inner)}
    return {"type": "string"}   # STRING, DATETIME ('YYYY-MM-DD HH:MM:SS')


def input_schema_for(spec: dict) -> dict:
    props: dict[str, dict] = {}
    for pname, pspec in spec.get("parameters", {}).items():
        js = _json_type(pspec.get("type", "STRING"))
        desc = pspec.get("description", "") or ""
        if pspec.get("type", "").upper() == "DATETIME":
            desc = (desc + " Format 'YYYY-MM-DD HH:MM:SS'.").strip()
        if pspec.get("example") is not None:
            desc = (desc + f" Example: {json.dumps(pspec['example'])}").strip()
        if desc:
            js["description"] = desc
        props[pname] = js
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


# ---------------------------------------------------------------- allowlist + session

def read_allowlist(path: Path | None = None) -> dict[str, list[str]]:
    path = path or SETTINGS.tools_allowlist
    sections: dict[str, list[str]] = {"read": [], "write": [], "blocked": []}
    current = "read"
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("[") and s.endswith("]"):
            current = s[1:-1]
            sections.setdefault(current, [])
            continue
        sections[current].append(s)
    return sections


def launcher_env(read_only: bool, settings: Settings = SETTINGS) -> dict[str, str]:
    al = read_allowlist(settings.tools_allowlist)
    allowed = al["read"] + ([] if read_only else al["write"])
    env = {**os.environ}
    env.update({
        "TG_HOST": settings.tg_host, "TG_GRAPHNAME": settings.tg_graphname,
        "TG_QUERY_TIMEOUT_MS": str(settings.tg_query_timeout_ms),
        "TG_RESPONSE_LIMIT_BYTES": str(settings.tg_response_limit_bytes),
        "TG_ALLOWED_TOOLS": ",".join(allowed), "TG_BLOCKED_TOOLS": ",".join(al["blocked"]),
        "TG_LOG_TOOL_CALLS": os.environ.get("TG_LOG_TOOL_CALLS", "1"),
    })
    if settings.tg_secret:
        env["TG_SECRET"] = settings.tg_secret
    return env


@asynccontextmanager
async def open_session(read_only: bool, settings: Settings = SETTINGS) -> AsyncIterator[tuple[Any, list[Any]]]:
    """Spawn the launcher over stdio and yield `(ClientSession, [mcp.types.Tool])`.

    read_only=True serves only the [read] tools (LLM-facing / UI sessions);
    read_only=False adds the [write] tools for the harness (persist).
    In RUN_MODE=mock a FakeSession backed by agent/mock_fixtures.py is yielded instead.
    """
    if settings.mock:
        from agent.mock_fixtures import FakeSession
        session = FakeSession()
        yield session, session.tools()
        return
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from mcp import ClientSession

    params = StdioServerParameters(
        command=sys.executable, args=[str(settings.launcher_path)], env=launcher_env(read_only, settings),
        cwd=str(settings.launcher_path.parent.parent),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = (await session.list_tools()).tools
            names = {t.name for t in listed}
            if RUN_INSTALLED not in names:
                raise RuntimeError(f"{RUN_INSTALLED} is not served; served={sorted(names)}")
            yield session, list(listed)


# ---------------------------------------------------------------- run_query

class QueryError(RuntimeError):
    pass


class GraphUnavailable(QueryError):
    """The workspace stayed unreachable for the whole TG_RESUME_MAX_WAIT_S budget (not a query error)."""


# What a P2 tool returns to the model when the workspace is unreachable (never evidence; the case fails after P2).
GRAPH_UNAVAILABLE_REPLY = json.dumps({"error": "GRAPH TEMPORARILY UNAVAILABLE - not evidence; do not draw conclusions from this call"})


def mark_graph_unavailable(tctx: Any, name: str) -> str:
    """Flag the case (PhaseMachine._run raises after P2), refund the budget unit, return the model-facing reply."""
    tctx.graph_unavailable = tctx.graph_unavailable or name
    tctx.budget.used = max(0, tctx.budget.used - 1)
    return GRAPH_UNAVAILABLE_REPLY


# Injectable for tests; the resume loop awaits it between attempts.
_sleep = asyncio.sleep
_clock = time.monotonic      # injectable for tests (the resume loop's wall-clock cap)


def _transient_payload(payload: dict) -> bool:
    """True when an MCP ToolResponse failure looks like a resuming / unreachable workspace.

    tigergraph-mcp's format_error never raises: it puts str(exc) in `error`. An aiohttp total timeout
    stringifies to '' (error_code OPERATION_ERROR), so an empty error is transient too. error_code alone
    is not used: CONNECTION_ERROR is also set for any text that merely contains "timeout".
    """
    from ops.ensure_awake import is_transient

    text = str(payload.get("error") or "")
    return not text.strip() or is_transient(RuntimeError(text))


def _resume_budget() -> float:
    return float(os.getenv("TG_RESUME_MAX_WAIT_S", "240"))


async def _resume_loop(attempt_fn: Callable[[], Any], what: str) -> Any:
    """Run `attempt_fn()` (a coroutine factory that raises `_Transient` or a real error) under the resume backoff."""
    from ops.ensure_awake import backoff_delays, is_transient

    log = current_runlog.get()
    budget = _resume_budget()
    delays = backoff_delays(budget)
    # Wall-clock cap as well: backoff_delays only counts the sleeps, and a stalled attempt waits the full aiohttp
    # deadline (GSQL-TIMEOUT + 30 s = 150 s) before it comes back as error '' - without this a stalled connection
    # could hold one query for ~9 x 150 s. A resume answers 502 fast, so this never cuts a genuine resume short.
    t_start = _clock()
    n = 0
    while True:
        try:
            return await attempt_fn()
        except _Transient as t:
            text = t.text
        except QueryError:
            raise
        except Exception as e:
            if not is_transient(e):
                raise
            text = str(e)
        delay = next(delays, None)
        if delay is None or _clock() - t_start >= budget:
            raise GraphUnavailable(text or "empty error (connection stalled)")
        n += 1
        if log is not None:
            log.note("graph transient, retrying", query=what, attempt=n, delay_s=delay, error=text[:200])
        await _sleep(delay)


class _Transient(Exception):
    """Internal: one attempt failed with a transient MCP payload."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


async def call_tool_with_resume(session: Any, tool: str, args: dict) -> Any:
    """`session.call_tool(tool, args)` with the resume retry, for the non-query tools (P10 add_edges).

    Returns the raw MCP result of the first non-transient attempt (success or a real failure); raises
    GraphUnavailable when the workspace stays unreachable. The writers are keyed upserts, so a re-send is safe.
    """

    async def attempt() -> Any:
        res = await session.call_tool(tool, args)
        text = _result_text(res)
        try:
            payload = parse_tool_text(text)
        except QueryError:
            return res
        if isinstance(payload, dict) and payload.get("success") is False and _transient_payload(payload):
            raise _Transient(str(payload.get("error") or ""))
        return res

    return await _resume_loop(attempt, tool)


_FENCE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


def parse_tool_text(text: str) -> dict:
    """Parse the tigergraph-mcp formatter output (first ```json block = ToolResponse)."""
    m = _FENCE.search(text)
    if not m:
        try:
            return json.loads(text)
        except Exception as e:
            raise QueryError(f"unparseable MCP tool result: {text[:300]!r}") from e
    return json.loads(m.group(1))


_normalize = None
_normalize_mod = None


def _load_normalize():
    """mcp/normalize.py (queries module) loaded by file path — `import mcp.normalize` would
    shadow the `mcp` SDK package, so the folder stays a plain directory and is loaded by path
    (never import it as a package, and never add mcp/__init__.py)."""
    global _normalize, _normalize_mod
    if _normalize is not None:
        return _normalize
    import importlib.util
    path = SETTINGS.launcher_path.parent / "normalize.py"
    if path.exists():
        spec = importlib.util.spec_from_file_location("hhgoa_mcp_normalize", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _normalize_mod = mod
        _normalize = getattr(mod, "normalize", False)
    else:
        _normalize = False
    return _normalize


def merge_printed(results: Any, qname: str = "") -> dict:
    """Merge the list of PRINT objects returned by RESTPP into one dict (contracts §A).

    Uses mcp/normalize.py (vertex-set flattening, JSON-string decoding) when present, then unwraps the
    one-element-list PRINTs the contract reads as objects (`card_testing_check.run`, M7).
    """
    if isinstance(results, dict) and isinstance(results.get("results"), list):
        results = results["results"]
    norm = _load_normalize()
    if norm:
        merged = norm(results if isinstance(results, list) else [results] if isinstance(results, dict) else [])
    else:
        merged = {}
        if isinstance(results, dict):
            results = [results]
        for obj in results or []:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    merged[k] = v
    return _unwrap_singletons(qname, merged)


# PRINT keys a GroupByAccum prints as a one-element list although the contract (and every consumer) reads a
# single object: card_testing_check.run (contracts §A). Keyed on the CONTRACT name:
# mcp.normalize.normalize() already renames the GSQL-side `best_run` to `run` before merge_printed() gets
# here, so `merged` holds `run` on the normal path.
_SINGLETON_KEYS: dict[str, tuple[str, ...]] = {"card_testing_check": ("run",)}

# `run` is a GSQL reserved word and cannot be used as a PRINT alias (graph/queries/card_testing_check.gsql
# aliases it `best_run` instead); mcp.normalize.normalize() renames it back to `run` for every caller,
# including tests/live/test_query_contracts.py which calls normalize() directly. This is only a fallback for
# merge_printed()'s manual-merge branch (mcp/normalize.py failed to import), which never calls normalize()
# and so never sees that rename.
_RENAME_KEYS: dict[str, dict[str, str]] = {"card_testing_check": {"best_run": "run"}}


def _unwrap_singletons(qname: str, merged: dict) -> dict:
    """`card_testing_check` prints `run` (renamed from `best_run`, see above) as
    `[{start_id, ids, amts, start_ts, end_ts}]`; unwrap it to the object the contract reads, exactly as
    `mcp/normalize.unwrap_singleton` does."""
    for gsql_name, contract_name in _RENAME_KEYS.get(qname or "", {}).items():
        if gsql_name in merged and contract_name not in merged:
            merged[contract_name] = merged.pop(gsql_name)
    keys = _SINGLETON_KEYS.get(qname or "")
    if not keys:
        return merged
    unwrap = getattr(_normalize_mod, "unwrap_singleton", None) if _normalize_mod else None
    for k in keys:
        if k in merged:
            merged[k] = unwrap(merged, k) if callable(unwrap) else (merged[k][0] if isinstance(merged[k], list) and merged[k]
                                                                    else {} if isinstance(merged[k], list) else merged[k])
    return merged


def _result_text(res: Any) -> str:
    parts = []
    for c in getattr(res, "content", []) or []:
        t = getattr(c, "text", None)
        if t:
            parts.append(t)
    return "\n".join(parts)


async def run_query(session: Any, name: str, params: dict) -> dict:
    """Run an installed query through the MCP and return its printed JSON merged into one dict."""
    log = current_runlog.get()
    t0 = time.time()
    ok, err, size = True, "", 0

    async def attempt() -> dict:
        nonlocal size
        res = await session.call_tool(RUN_INSTALLED, {"query_name": name, "params": params})
        text = _result_text(res)
        size = len(text.encode("utf-8"))
        if getattr(res, "is_error", False):
            if text.strip() and _transient_payload({"error": text}):
                raise _Transient(text[:500])
            raise QueryError(text[:500])
        payload = parse_tool_text(text)
        if not payload.get("success", False):
            if _transient_payload(payload):
                raise _Transient(str(payload.get("error") or ""))
            raise QueryError(payload.get("error") or payload.get("summary") or "query failed")
        data = payload.get("data") or {}
        return merge_printed(data.get("result"), name)

    try:
        return await _resume_loop(attempt, name)
    except Exception as e:
        ok, err = False, str(e)
        raise
    finally:
        if log is not None:
            log.record_tool(f"query:{name}", params, time.time() - t0, size, ok=ok, error=err,
                            caller=current_caller.get())


current_caller: contextvars.ContextVar[str] = contextvars.ContextVar("hhgoa_caller", default="harness")


# ---------------------------------------------------------------- evidence extraction

_TXN = re.compile(r"\b3[0-9]{6}\b")            # TransactionIDs are 3000001..3590742
_CARD = re.compile(r"\bC\d{5}-K\d\b")
_CUST = re.compile(r"\bC\d{5}\b(?!-K)")
_CC = re.compile(r"\bCC-\d{4}\b")
_DEVICE_KEYS = {"device_id", "ring_id"}


def extract_ids(obj: Any, out: list[str] | None = None, key: str = "") -> list[str]:
    """Collect dataset ids (transactions, cards, customers, closed cases, device profiles)."""
    out = [] if out is None else out

    def add(x: str):
        if x and x not in out and not x.startswith("AC-"):
            out.append(x)

    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _DEVICE_KEYS and isinstance(v, str) and " | " in v:
                add(v)
            elif k == "id" and isinstance(v, str) and " | " in v:
                add(v)
            elif k in ("device_ids", "connected_device_profiles") and isinstance(v, list):
                for s in v:
                    if isinstance(s, str) and " | " in s:
                        add(s)
            extract_ids(v, out, k)
    elif isinstance(obj, list):
        for v in obj:
            extract_ids(v, out, key)
    elif isinstance(obj, str):
        if key in ("id", "ids", "first_fraud_txn_id", "txn_id", "card_id", "customer_id", "case_id") or key.endswith("_ids") or key.endswith("_id"):
            for rx in (_TXN, _CARD, _CC):
                for m in rx.findall(obj):
                    add(m)
            for m in _CUST.findall(obj):
                if m + "-K" not in obj:
                    add(m)
    elif isinstance(obj, int) and key in ("id", "ids", "first_fraud_txn_id", "txn_id"):
        s = str(obj)
        if _TXN.fullmatch(s):
            add(s)
    return out


def summarize_result(name: str, result: dict) -> str:
    """One-line, code-generated claim describing what the query returned."""
    bits = []
    for k, v in result.items():
        if isinstance(v, list):
            bits.append(f"{k}: {len(v)} rows")
        elif isinstance(v, dict):
            inner = ", ".join(f"{ik}={iv}" for ik, iv in list(v.items())[:6] if not isinstance(iv, (list, dict)))
            bits.append(f"{k}{{{inner}}}" if inner else f"{k}: {len(v)} keys")
        else:
            bits.append(f"{k}={v}")
    return f"{name} returned " + "; ".join(bits)[:600]


def format_ref(name: str, params: dict) -> str:
    def fmt(v: Any) -> str:
        if isinstance(v, dict) and "id" in v:
            return str(v["id"])
        if isinstance(v, list):
            return f"[{len(v)} items]"
        return str(v)
    return f"query:{name}(" + ", ".join(f"{k}={fmt(v)}" for k, v in params.items()) + ")"


def evidence_from_result(name: str, params: dict, result: dict, source: str = "graph") -> Evidence:
    return Evidence(
        claim=summarize_result(name, result), source=source, ref=format_ref(name, params),
        entity_ids=extract_ids(result)[:60], family=QUERY_FAMILY.get(name, "history"), direction="neutral",
    )


# ---------------------------------------------------------------- typed tools

@dataclass
class ToolBudget:
    limit: int
    used: int = 0

    def take(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


@dataclass
class ToolContext:
    """Shared state the typed tools write into during P2 (owned by the phase machine)."""
    opened_at: str
    facts: dict[str, dict] = field(default_factory=dict)
    facts_all: dict[str, list[dict]] = field(default_factory=dict)
    ledger: list = field(default_factory=list)
    budget: ToolBudget = field(default_factory=lambda: ToolBudget(SETTINGS.max_tool_calls))
    max_chars: int = SETTINGS.tool_result_max_chars
    on_result: Callable[[str, dict, dict], None] | None = None
    graph_unavailable: str = ''       # name of the P2 query the resume budget ran out on (the phase machine fails the case)
    # memory time-box (client side, whatever the installed query does): AgentCase rows with one of these ids, or
    # opened at/after `opened_at`, are dropped from every result before the model or the engine sees them
    exclude_case_ids: frozenset = field(default_factory=frozenset)


# ---------------------------------------------------------------- memory time-box

# Card attributes the ETL precomputed over all six months include activity AFTER opened_at. The fixed card_profile /
# case_context queries recompute the history fields as of `as_of`; an installed query that predates that fix is
# detected by card.last_ts > opened_at (impossible for a time-boxed result) and its history fields are withheld from
# the model's view (the engine still gets the raw facts). known_device_ids / recurring_amounts are never recomputed.
WHOLE_HISTORY_CARD_FIELDS = ("n_txns", "last_ts", "median_amt", "p90_amt", "max_amt", "max_in_person_amt", "n_online",
                             "n_in_person", "n_regions", "n_devices_seen")
ALWAYS_ALL_TIME_CARD_FIELDS = ("known_device_ids", "recurring_amounts")
WHOLE_HISTORY_NOTE = ("card statistics that include activity after opened_at are withheld; use case_context.txn.card_seq - 1 "
                      "(prior transactions), prior_med_amt, prior_max_amt and card_profile regions / devices / last30, all as of opened_at")


def card_profile_stale(result: Any, opened_at: str) -> bool:
    """True when a card_profile result carries whole-history card statistics (an installed query without the as-of fix)."""
    card = (result or {}).get("card") if isinstance(result, dict) else None
    last = str((card or {}).get("last_ts") or "") if isinstance(card, dict) else ""
    return bool(last and opened_at and last[:19] > opened_at[:19])


def _row_field(row: dict, key: str) -> Any:
    if key in row:
        return row[key]
    attrs = row.get("attributes") or {}
    return attrs.get(key, attrs.get("@" + key))


def _is_agent_row(row: dict) -> bool:
    rid = str(row.get("id") or row.get("v_id") or row.get("case_id") or "")
    kind = str(_row_field(row, "kind") or "")
    return rid.startswith("AC-") or kind == "agent" or row.get("v_type") == "AgentCase"


def memory_timebox(result: Any, opened_at: str, exclude_ids: frozenset | set = frozenset()) -> tuple[Any, list[str]]:
    """Drop AgentCase rows that are the case itself (`exclude_ids`, e.g. AC-HHG-014 from an earlier run) or were
    opened at/after `opened_at`, from every list in a query result. Returns (result, dropped ids)."""
    dropped: list[str] = []

    def keep(row: Any) -> bool:
        if not isinstance(row, dict) or not _is_agent_row(row):
            return True
        rid = str(row.get("id") or row.get("v_id") or row.get("case_id") or "")
        opened = str(_row_field(row, "opened_at") or "")
        if rid in exclude_ids or (opened and opened_at and opened[:19] >= opened_at[:19]):
            dropped.append(rid)
            return False
        return True

    def walk(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in o if keep(v)]
        return o

    return walk(result), dropped


def llm_view(qname: str, result: dict, opened_at: str = "", facts: dict | None = None) -> dict:
    """The model's copy of a result: card statistics that are not as of opened_at withheld (see above)."""
    if qname not in ("card_profile", "case_context") or not isinstance(result, dict):
        return result
    card = result.get("card")
    if not isinstance(card, dict):
        return result
    if qname == "card_profile":
        hidden = [k for k in ALWAYS_ALL_TIME_CARD_FIELDS if k in card]
        if card_profile_stale(result, opened_at):
            hidden += [k for k in WHOLE_HISTORY_CARD_FIELDS if k in card]
    else:   # case_context.card.n_txns: trusted only when it agrees with a time-boxed card_profile
        prof = (facts or {}).get("card_profile") or {}
        ok = (isinstance(prof.get("card"), dict) and not card_profile_stale(prof, opened_at)
              and prof["card"].get("n_txns") == card.get("n_txns"))
        hidden = [] if ok or "n_txns" not in card else ["n_txns"]
    if not hidden:
        return result
    out = dict(result)
    out["card"] = {k: v for k, v in card.items() if k not in hidden}
    out["card"]["_note"] = WHOLE_HISTORY_NOTE
    return out


def clamp_params(params: dict, opened_at: str) -> tuple[dict, list[str]]:
    """Clamp as_of / to_ts to opened_at so no evidence leaks past the case opening."""
    notes = []
    out = dict(params)
    for k in _UPPER_BOUND_PARAMS:
        v = out.get(k)
        if isinstance(v, str) and v > opened_at:
            out[k] = opened_at
            notes.append(f"{k} clamped to opened_at {opened_at}")
    return out, notes


def compact_json(obj: Any, max_chars: int) -> str:
    s = json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str)
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + f'... [truncated {len(s) - max_chars} chars; ask a narrower query]'


def make_query_tools(session: Any, tctx: ToolContext, descriptions: dict[str, dict] | None = None,
                     include: list[str] | None = None) -> list[Any]:
    """Typed Anthropic tools over the installed read queries (one tool per query)."""
    from anthropic.lib.tools import BetaAsyncFunctionTool

    descriptions = descriptions or load_query_descriptions()
    tools: list[Any] = []
    for qname, spec in descriptions.items():
        if spec.get("harness_only") or spec.get("writer"):
            continue
        if include is not None and qname not in include:
            continue
        tools.append(_make_tool(BetaAsyncFunctionTool, session, tctx, qname, spec))
    return tools


def _make_tool(cls, session: Any, tctx: ToolContext, qname: str, spec: dict):
    async def _run(**params: Any) -> str:
        return await call_query_tool(session, tctx, qname, params)

    _run.__name__ = qname
    _run.__doc__ = spec.get("description", "")
    desc = spec.get("description", "") + " Every result is time-boxed to ts <= opened_at."
    return cls(_run, name=qname, description=desc, input_schema=input_schema_for(spec))


async def call_query_tool(session: Any, tctx: ToolContext, qname: str, params: dict) -> str:
    """Body of every typed tool: budget, clamp, run, record facts + evidence, return compact JSON."""
    if tctx.graph_unavailable:  # the case already failed on an unreachable workspace: do not spin another budget
        return GRAPH_UNAVAILABLE_REPLY
    if not tctx.budget.take():
        return json.dumps({"error": "tool budget exhausted", "hint": "You have used every allowed graph call. Stop investigating and give your assessment from the evidence collected."})
    params, notes = clamp_params(params, tctx.opened_at)
    token = current_caller.set("llm")
    try:
        result = await run_query(session, qname, params)
    except GraphUnavailable:
        # not evidence: refund the unit, flag the case (PhaseMachine._run raises after P2), tell the model plainly
        return mark_graph_unavailable(tctx, qname)
    except Exception as e:
        return json.dumps({"error": f"{qname} failed: {str(e)[:400]}", "hint": "Check the parameter encoding (VERTEX → {\"id\": ...}, DATETIME → 'YYYY-MM-DD HH:MM:SS') or choose another query."})
    finally:
        current_caller.reset(token)
    result = _timebox(tctx, qname, result)
    record_result(tctx, qname, params, result)
    payload = {"query": qname, "params": params, "result": llm_view(qname, result, tctx.opened_at, tctx.facts)}
    if notes:
        payload["notes"] = notes
    return compact_json(payload, tctx.max_chars)


def record_result(tctx: ToolContext, qname: str, params: dict, result: dict) -> Evidence:
    tctx.facts_all.setdefault(qname, []).append(result)
    if qname not in tctx.facts:
        tctx.facts[qname] = result           # first call wins (the harness's mandatory call)
    ev = evidence_from_result(qname, params, result)
    tctx.ledger.append(ev)
    if tctx.on_result:
        tctx.on_result(qname, params, result)
    return ev


async def harness_query(session: Any, tctx: ToolContext, qname: str, params: dict) -> dict:
    """Mandatory / engine re-run query by the harness (counted in tool_calls, logged as caller=harness)."""
    params, _ = clamp_params(params, tctx.opened_at)
    result = _timebox(tctx, qname, await run_query(session, qname, params))
    record_result(tctx, qname, params, result)
    return result


def _timebox(tctx: ToolContext, qname: str, result: Any) -> Any:
    out, dropped = memory_timebox(result, tctx.opened_at, tctx.exclude_case_ids)
    log = current_runlog.get()
    if dropped and log is not None:
        log.note(f"memory time-box: dropped agent cases from {qname}", dropped=sorted(set(dropped)))
    return out
