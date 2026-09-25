"""Harness-side retrieval wrappers (the vector never crosses the MCP transcript; the harness embeds the
query text and calls the installed query with the 1024-float list).

    from rag.retrieval import similar_prior_cases, grounding_chunks, case_query_text
    rows = similar_prior_cases(run, embedder, query_text, card_id=..., customer_id=..., device_id=...,
                               addr1=..., pattern_sig=..., as_of="2016-11-22 20:11:00", k=8)

`run(name, params) -> dict` is injected: agent/mcp_client.run_query (MCP, logged as a tool call) or a
pyTigerGraph `lambda n, p: conn.runInstalledQuery(n, p, timeout=120000)` for offline checks. Both return
the installed query's printed JSON; TigerGraph prints vertex rows as {"v_id", "v_type", "attributes": {...}}
and this module flattens them into the contract rows of interfaces.md section A.

Fallback ladder for similar_prior_cases:
  1. `similar_prior_cases` (two-type vectorSearch with mixed candidate_set)      - preferred
  2. `similar_prior_cases_closed` + `similar_prior_cases_agent`, merged here     - if (1) failed to install
  3. `similar_cases_structural`                                                  - if the HNSW index is rebuilding
     (index_ready() is False) or vectorSearch errors at run time
The ladder is chosen per call; which rung answered is returned in `meta["source"]` for the run log.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable

from .closedcase_embed_text import mask

RunFn = Callable[[str, dict], dict]
CLEARED = {"cleared", "legitimate"}


# ----------------------------------------------------------------------------- flattening

def _first_key(res: dict, key: str) -> list:
    """Printed JSON from pyTigerGraph is a list of {key: value} dicts; the MCP wrapper merges them into one
    dict. Accept both."""
    if isinstance(res, dict):
        return res.get(key, []) or []
    if isinstance(res, list):
        for part in res:
            if isinstance(part, dict) and key in part:
                return part[key] or []
    return []


def flatten_rows(rows: Iterable[dict]) -> list[dict]:
    """{"v_id", "v_type", "attributes": {"@kind": ..}} -> {"id": .., "kind": .., ...} (strips the '@')."""
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        row = {"id": r.get("v_id", r.get("id", ""))}
        for k, v in (r.get("attributes") or {}).items():
            row[k.lstrip("@")] = v
        for k, v in r.items():
            if k not in ("v_id", "v_type", "attributes", "id"):
                row.setdefault(k.lstrip("@"), v)
        out.append(row)
    return out


def _norm_case_row(row: dict) -> dict:
    return {
        "id": row.get("id", ""),
        "kind": row.get("kind", "closed" if str(row.get("id", "")).startswith("CC-") else "agent"),
        "outcome_or_verdict": row.get("outcome_or_verdict", ""),
        "pattern": row.get("pattern", ""),
        "exposure_usd": float(row.get("exposure_usd", 0.0) or 0.0),
        "opened_at": row.get("opened_at", ""),
        "distance": float(row.get("distance", 1.0) if row.get("distance") is not None else 1.0),
        "overlap_reasons": sorted(row.get("overlap_reasons", []) or []),
    }


def timebox_cases(rows: list[dict], as_of: str, exclude_ids: Iterable[str] = ()) -> list[dict]:
    """Case memory never includes the case itself or the future: drop the given ids (the case's own AC-<case_id>
    from an earlier run) and every agent case opened at or after `as_of`. Enforced here whatever the installed
    query's own filter does (the GSQL twins use `opened_at <= as_of`, which admits a re-run's own write-back)."""
    ex = set(exclude_ids)
    out = []
    for r in rows:
        rid = str(r.get("id", ""))
        agent = r.get("kind") == "agent" or rid.startswith("AC-")
        if rid in ex or (agent and as_of and str(r.get("opened_at", ""))[:19] >= as_of[:19] and r.get("opened_at")):
            continue
        out.append(r)
    return out


def diversify(rows: list[dict], k: int) -> list[dict]:
    """Nearest k, but guarantee one cleared/legitimate case when any exists (same rule as the GSQL)."""
    rows = sorted(rows, key=lambda r: (r["distance"], r["id"]))
    top = rows[:k]
    if k > 1 and not any(r["outcome_or_verdict"] in CLEARED for r in top):
        clear = [r for r in rows if r["outcome_or_verdict"] in CLEARED]
        if clear:
            top = top[: k - 1] + [clear[0]]
    return sorted(top, key=lambda r: (r["distance"], r["id"]))


# ----------------------------------------------------------------------------- query text

def case_query_text(pattern_sig: str, trigger_type: str, device_desc: str, region: str, narrative: str) -> str:
    """Build the query in the same masked 'language' as ClosedCase.embed_text so the vector lands near the
    right template family. `narrative` is the engine's one-paragraph description of what the graph shows
    (amounts/ids are masked here, so it is safe to pass raw evidence text)."""
    outcome = "confirmed_fraud" if trigger_type == "customer_report" else "unknown"
    # same shape and mask as etl.parse_closed_cases.embed_text (ClosedCase.embed_text): "pattern: X | outcome: Y |
    # device: Z | region: R | <masked note>" - the ETL uses 'none' for an absent device/region
    return (f"pattern: {pattern_sig or 'unknown'} | outcome: {outcome} | device: {device_desc or 'none'} | "
            f"region: {region or 'none'} | {mask(narrative)}")


# ----------------------------------------------------------------------------- similar_prior_cases

def _vector_error(e: Exception) -> bool:
    s = str(e).lower()
    return any(t in s for t in ("vectorsearch", "vector", "not installed", "does not exist", "unknown query", "rebuild"))


def similar_prior_cases(run: RunFn, embedder, query_text: str, *, card_id: str = "", customer_id: str = "",
                        device_id: str = "", addr1: str = "", pattern_sig: str = "", as_of: str, k: int = 8,
                        index_ready: Callable[[], bool] | None = None, prefer: str = "hybrid",
                        exclude_ids: Iterable[str] = ()) -> dict:
    """Returns {"cases": [contract rows], "meta": {"source": ..., "k": k}}. Agent cases opened at/after `as_of` and
    `exclude_ids` (the case's own AC- id) are dropped before diversification (timebox_cases)."""
    def pick(rows: list[dict]) -> list[dict]:
        return diversify(timebox_cases([_norm_case_row(r) for r in rows], as_of, exclude_ids), k)

    struct_params = {"k": k, "card_id": card_id, "customer_id": customer_id, "device_id": device_id,
                     "addr1": addr1, "pattern_sig": pattern_sig, "as_of": as_of}
    if prefer != "structural" and (index_ready is None or index_ready()):
        q = embedder.embed_query(query_text)
        params = {"q": q, **struct_params}
        if prefer in ("hybrid", "auto"):
            try:
                rows = flatten_rows(_first_key(run("similar_prior_cases", params), "cases"))
                return {"cases": pick(rows), "meta": {"source": "similar_prior_cases", "k": k}}
            except Exception as e:  # noqa: BLE001
                if not _vector_error(e):
                    raise
        try:
            rows = flatten_rows(_first_key(run("similar_prior_cases_closed", params), "cases"))
            try:
                rows += flatten_rows(_first_key(run("similar_prior_cases_agent", params), "cases"))
            except Exception as e:  # noqa: BLE001 - agent twin may not exist yet
                if not _vector_error(e):
                    raise
            return {"cases": pick(rows), "meta": {"source": "two_query", "k": k}}
        except Exception as e:  # noqa: BLE001
            if not _vector_error(e):
                raise
    rows = flatten_rows(_first_key(run("similar_cases_structural", struct_params), "cases"))
    return {"cases": pick(rows), "meta": {"source": "similar_cases_structural", "k": k}}


# ----------------------------------------------------------------------------- grounding_chunks

def grounding_chunks(run: RunFn, embedder, query_text: str, *, doc_filter: str = "", kind_filter: str = "",
                     k: int = 5) -> dict:
    q = embedder.embed_query(query_text)
    rows = flatten_rows(_first_key(run("grounding_chunks", {"q": q, "k": k, "doc_filter": doc_filter,
                                                             "kind_filter": kind_filter}), "chunks"))
    chunks = [{"id": r.get("id", ""), "doc_id": r.get("doc_id", ""), "section": r.get("section", ""),
               "page": int(r.get("page", 0) or 0), "kind": r.get("kind", ""), "text": r.get("text", ""),
               "distance": float(r.get("distance", 1.0) if r.get("distance") is not None else 1.0)} for r in rows]
    chunks.sort(key=lambda c: c["distance"])
    return {"chunks": chunks[:k]}


def evidence_ref(chunk: dict) -> str:
    """The `ref` string for a source=document evidence item: '<doc_id>#<section> p.<page>'."""
    page = f" p.{chunk['page']}" if chunk.get("page") else ""
    return f"{chunk['doc_id']}#{chunk['section']}{page}"
