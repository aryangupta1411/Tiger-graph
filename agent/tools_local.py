"""Harness-side retrieval tools (contracts §C `agent/tools_local.py`).

- `find_similar_cases(query_text, ctx, k=8)` embeds the query with the configured
  embedding backend (`EMBED_BACKEND=local`, the default, runs sentence-transformers on
  this machine and needs no API key; `voyage` uses the paid API) and calls the installed
  `similar_prior_cases` query (structural candidate set → vectorSearch → diversified).
  The EMBED_DIM-float vector never crosses the LLM transcript.
- `grounding_chunks(query_text, doc_filter="", kind_filter="", k=5)` → PolicyChunk rows.
- `ofac_screen(name)` → fuzzy lookup over data/ofac/sdn.csv (+ alt.csv) with rapidfuzz.

The MCP session and tool context are bound with `bind(session, tctx)` (contextvars)
because the contract signatures carry neither. In RUN_MODE=mock (or with
EMBED_BACKEND=voyage and no VOYAGE_API_KEY) the embedding is a deterministic
pseudo-vector so the pipeline runs offline; the query then returns whatever the
FakeSession fixture holds. Both real backends go through rag.embed.Embedder, so the
harness and the loaded vectors are guaranteed to be in the same space (and share its
sqlite cache).
"""
from __future__ import annotations

import contextvars
import csv
import hashlib
import json
import math
import time
from typing import Any

from agent.config import SETTINGS
from agent.mcp_client import GRAPH_UNAVAILABLE_REPLY, GraphUnavailable, ToolContext, harness_query, mark_graph_unavailable, run_query
from agent.runlog import current_runlog

_session: contextvars.ContextVar[Any] = contextvars.ContextVar("hhgoa_mcp_session", default=None)
_tctx: contextvars.ContextVar[ToolContext | None] = contextvars.ContextVar("hhgoa_tool_ctx", default=None)


def bind(session: Any, tctx: ToolContext) -> None:
    _session.set(session)
    _tctx.set(tctx)


# ---------------------------------------------------------------- embeddings

_embedder = None


def _pseudo_vector(text: str, dim: int) -> list[float]:
    """Deterministic unit vector from a hash — offline stand-in, same dimension."""
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    vals = []
    for i in range(dim):
        h = hashlib.sha256(seed + i.to_bytes(4, "big")).digest()
        vals.append(int.from_bytes(h[:4], "big") / 2**32 - 0.5)
    norm = math.sqrt(sum(v * v for v in vals)) or 1.0
    return [round(v / norm, 6) for v in vals]


def _offline() -> bool:
    """True when no real backend is reachable: RUN_MODE=mock, or the paid backend without its key."""
    return bool(SETTINGS.mock) or (SETTINGS.embed_backend == "voyage" and not SETTINGS.voyage_api_key)


def _get_embedder():
    """rag.embed.Embedder for the configured backend - the same class (and cache, and query/document
    asymmetry) that produced data/out/vec_*.psv, so the harness query vector lands in the same space."""
    global _embedder
    if _embedder is None:
        from rag.embed import Embedder
        _embedder = Embedder(model=SETTINGS.embed_model, dim=SETTINGS.embed_dim,
                             mode="live", backend=SETTINGS.embed_backend)
    return _embedder


def embed_query(text: str) -> list[float]:
    """EMBED_DIM-d query vector: local encode_query / BGE query prefix, or Voyage input_type='query'."""
    if _offline():
        return _pseudo_vector(text, SETTINGS.embed_dim)
    return _get_embedder().embed_query(text)


def embed_document(text: str) -> list[float]:
    """EMBED_DIM-d document vector (local encode_document, or Voyage input_type='document')."""
    if _offline():
        return _pseudo_vector(text, SETTINGS.embed_dim)
    return _get_embedder().embed_documents([text])[0]


# ---------------------------------------------------------------- retrievers

async def _run(name: str, params: dict) -> dict:
    session, tctx = _session.get(), _tctx.get()
    if session is None:
        raise RuntimeError("tools_local.bind(session, tctx) was not called")
    if tctx is not None:
        return await harness_query(session, tctx, name, params)
    return await run_query(session, name, params)


async def find_similar_cases(query_text: str, ctx: Any, k: int = 8, device_id: str = "", addr1: str = "",
                             pattern_sig: str = "") -> dict:
    """Structural + vector retrieval of prior cases (closed and agent-written), diversified."""
    vec = embed_query(query_text)
    params = {"q": vec, "k": int(k), "card_id": ctx.card_id, "customer_id": ctx.customer_id,
              "device_id": device_id or "", "addr1": addr1 or "", "pattern_sig": pattern_sig or "", "as_of": ctx.opened_at}
    res = await _run("similar_prior_cases", params)
    return {"cases": list(res.get("cases", []) or [])}


async def grounding_chunks(query_text: str, doc_filter: str = "", kind_filter: str = "", k: int = 5) -> dict:
    """Policy / pattern / regulation chunks for rule citations and the SAR draft."""
    vec = embed_query(query_text)
    res = await _run("grounding_chunks", {"q": vec, "k": int(k), "doc_filter": doc_filter or "", "kind_filter": kind_filter or ""})
    return {"chunks": list(res.get("chunks", []) or [])}


# ---------------------------------------------------------------- OFAC

_SDN_COLS = ["ent_num", "SDN_Name", "SDN_Type", "Program", "Title", "Call_Sign", "Vess_type", "Tonnage", "GRT", "Vess_flag", "Vess_owner", "Remarks"]
_ALT_COLS = ["ent_num", "alt_num", "alt_type", "alt_name", "alt_remarks"]
_sdn_cache: list[tuple[str, str, str]] | None = None


def _load_sdn() -> list[tuple[str, str, str]]:
    """[(name, ent_num, type)] from sdn.csv and alt.csv (OFAC CSV, no header, '-0-' = empty)."""
    global _sdn_cache
    if _sdn_cache is not None:
        return _sdn_cache
    rows: list[tuple[str, str, str]] = []
    sdn = SETTINGS.ofac_dir / "sdn.csv"
    alt = SETTINGS.ofac_dir / "alt.csv"
    if sdn.exists():
        with sdn.open(encoding="latin-1", newline="") as fh:
            for r in csv.reader(fh):
                if len(r) >= 3 and r[1] not in ("", "-0-"):
                    rows.append((r[1].strip(), r[0].strip(), r[2].strip()))
    if alt.exists():
        with alt.open(encoding="latin-1", newline="") as fh:
            for r in csv.reader(fh):
                if len(r) >= 4 and r[3] not in ("", "-0-"):
                    rows.append((r[3].strip(), r[0].strip(), "alt:" + r[2].strip()))
    _sdn_cache = rows
    return rows


def ofac_screen(name: str, threshold: int = 90) -> dict:
    """Fuzzy OFAC SDN screening. Anonymised ids (C01234) never match; the SAR states the result."""
    t0 = time.time()
    rows = _load_sdn()
    log = current_runlog.get()
    if not rows:
        out = {"name": name, "screened": False, "hit": False, "matches": [], "threshold": threshold,
               "note": "OFAC SDN list not present at data/ofac/sdn.csv; screening not performed"}
        if log:
            log.record_tool("ofac_screen", {"name": name}, time.time() - t0, len(json.dumps(out)), caller="harness")
        return out
    try:
        from rapidfuzz import fuzz, process
        hits = process.extract(name, [r[0] for r in rows], scorer=fuzz.token_sort_ratio, limit=5, score_cutoff=threshold)
        matches = [{"name": rows[idx][0], "score": round(float(score), 1), "ent_num": rows[idx][1], "type": rows[idx][2]}
                   for _, score, idx in hits]
    except ImportError:
        matches = [{"name": r[0], "score": 100.0, "ent_num": r[1], "type": r[2]} for r in rows if r[0].lower() == name.lower()]
    out = {"name": name, "screened": True, "hit": bool(matches), "matches": matches, "threshold": threshold,
           "note": f"screened against {len(rows)} SDN/alt names (rapidfuzz token_sort_ratio >= {threshold})"}
    if log:
        log.record_tool("ofac_screen", {"name": name}, time.time() - t0, len(json.dumps(out)), caller="harness")
    return out


# ---------------------------------------------------------------- LLM-facing wrappers

def make_local_tools(ctx: Any, tctx: ToolContext) -> list[Any]:
    """`find_similar_cases` and `grounding_chunks` as typed tools the model may call in P2."""
    from anthropic.lib.tools import BetaAsyncFunctionTool

    async def _find_similar_cases(query_text: str, device_id: str = "", addr1: str = "", pattern_sig: str = "") -> str:
        if tctx.graph_unavailable:
            return GRAPH_UNAVAILABLE_REPLY
        if not tctx.budget.take():
            return json.dumps({"error": "tool budget exhausted"})
        try:
            res = await find_similar_cases(query_text, ctx, k=8, device_id=device_id, addr1=addr1, pattern_sig=pattern_sig)
        except GraphUnavailable:        # same contract as call_query_tool: flag the case, refund, never evidence
            return mark_graph_unavailable(tctx, "find_similar_cases")
        return json.dumps(res, default=str)[: tctx.max_chars]

    async def _grounding_chunks(query_text: str, doc_filter: str = "", kind_filter: str = "") -> str:
        if tctx.graph_unavailable:
            return GRAPH_UNAVAILABLE_REPLY
        if not tctx.budget.take():
            return json.dumps({"error": "tool budget exhausted"})
        try:
            res = await grounding_chunks(query_text, doc_filter=doc_filter, kind_filter=kind_filter, k=5)
        except GraphUnavailable:
            return mark_graph_unavailable(tctx, "grounding_chunks")
        return json.dumps(res, default=str)[: tctx.max_chars]

    t1 = BetaAsyncFunctionTool(
        _find_similar_cases, name="find_similar_cases",
        description=("Case memory: retrieve prior closed cases (CC-xxxx) and earlier agent cases similar to this one — "
                     "structural first (same card / customer / device profile / region / pattern within 120 days), then "
                     "vector similarity over masked analyst notes, diversified so at least one cleared case appears when available. "
                     "Returns cases with outcome, pattern, exposure, opened_at, distance and overlap_reasons."),
        input_schema={"type": "object", "properties": {
            "query_text": {"type": "string", "description": "Natural-language description of the activity (pattern, channel, device, region, amounts masked)."},
            "device_id": {"type": "string", "description": "Device profile id to match structurally, or ''."},
            "addr1": {"type": "string", "description": "Billing region to match structurally, or ''."},
            "pattern_sig": {"type": "string", "description": "Candidate pattern name, or ''."}},
            "required": ["query_text", "device_id", "addr1", "pattern_sig"], "additionalProperties": False})
    t2 = BetaAsyncFunctionTool(
        _grounding_chunks, name="grounding_chunks",
        description=("GraphRAG grounding: retrieve policy rules (R1–R10, 3a, 3b, §5, §6), README pattern descriptions and "
                     "regulatory excerpts (FinCEN SAR guidance, FFIEC, FATF) as chunks with document, section and page for citation."),
        input_schema={"type": "object", "properties": {
            "query_text": {"type": "string", "description": "What you need grounded, e.g. 'when must a SAR be filed for shared device fraud'."},
            "doc_filter": {"type": "string", "description": "Document id to restrict to (e.g. 'sar_guidance', 'fraud_policy'), or ''."},
            "kind_filter": {"type": "string", "description": "policy | pattern | regulation | ''."}},
            "required": ["query_text", "doc_filter", "kind_filter"], "additionalProperties": False})
    return [t1, t2]
