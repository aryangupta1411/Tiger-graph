"""mcp/normalize.py — turns the raw `results` array of an installed query into the flat dict the contracts
describe (interfaces.md §A). agent/mcp_client.run_query MUST pass every tigergraph__run_installed_query
result through `normalize()` before it reaches the engine or the LLM.

Rules (deterministic, no schema knowledge needed):
  1. `results` is a list of PRINT objects; their keys are merged into one dict (later keys win).
  2. A printed VERTEX SET (list of {"v_id", "v_type", "attributes"}) is replaced by the list of its
     `attributes` dicts with "id" set to v_id — none of the module-C queries print vertex sets today, but
     the rule keeps the LLM-facing shape stable if a query is rewritten that way.
  3. A string value that looks like a JSON array/object under a key ending in `_ids`, `_cards`, or named
     `region_cluster_30d` / `lookalike_cards` is json-decoded (fallback for the parse_json_array path).
  4. Keys documented as singleton objects that a query prints as a one-element list (only
     card_testing_check.run) are unwrapped by the engine, not here.
  5. card_testing_check.gsql prints the contract key `run` under the name `best_run` (`run` is a GSQL
     reserved word and cannot be a PRINT alias); normalize() renames it back to `run` so every caller sees
     the contract key and nothing downstream needs to know about the GSQL-side workaround.
The MCP formatter echoes the payload twice (once in the model_dump JSON, once in the "Data" block);
mcp_client extracts the first JSON block and reads `data.results` (or `data.result.results` when the
response wraps pyTigerGraph's list) before calling normalize().
"""
from __future__ import annotations

import json
from typing import Any

_JSONISH_KEYS = ("region_cluster_30d", "lookalike_cards", "burst_lookalike_ids", "home_regions",
                 "usual_products", "known_device_ids", "recurring_amounts", "initial_actions", "final_actions")


def _is_vertex(o: Any) -> bool:
    return isinstance(o, dict) and "v_id" in o and "v_type" in o and "attributes" in o


def _attr_key(k: str) -> str:
    """`PRINT res[res.text, res.@distance] AS chunks` comes back from JSON API v2 with attribute keys
    `res.text` / `res.@distance`, not `text` / `distance`. Strip the `<alias>.` prefix and the `@` so rows
    match the contract names. Schema attribute names never contain a dot, so this cannot clip a real key.
    Without it, every live grounding chunk read as text="" and every similar case as distance=1.0 with
    no outcome or pattern -- silently, because the readers use .get() with defaults."""
    if "." in k:
        k = k.split(".", 1)[1]
    return k[1:] if k.startswith("@") else k


def _vertex_row(x: dict) -> dict:
    return dict({_attr_key(k): _walk(_attr_key(k), v) for k, v in x["attributes"].items()}, id=x["v_id"])


def _maybe_json(key: str, v: Any) -> Any:
    if isinstance(v, str) and (key in _JSONISH_KEYS or key.endswith("_ids")) and v[:1] in "[{":
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def _walk(key: str, v: Any) -> Any:
    if isinstance(v, list):
        if v and all(_is_vertex(x) for x in v):
            return [_vertex_row(x) for x in v]
        return [_walk(key, x) for x in v]
    if _is_vertex(v):
        return _vertex_row(v)
    if isinstance(v, dict):
        return {k: _walk(k, x) for k, x in v.items()}
    return _maybe_json(key, v)


def normalize(results: list[dict]) -> dict:
    """Merge the PRINT objects of one installed-query response into a single flat dict."""
    out: dict[str, Any] = {}
    for block in results or []:
        if not isinstance(block, dict):
            continue
        for k, v in block.items():
            out[k] = _walk(k, v)
    # `run` is a GSQL reserved word and cannot be used as a PRINT alias, so
    # graph/queries/card_testing_check.gsql prints the contract key `run` as `best_run` instead (see the
    # comment on its PRINT line). Rename it back here -- the one place every caller of normalize() goes
    # through, including tests/live/test_query_contracts.py, which reads normalize() directly and never
    # sees agent/mcp_client.py's own belt-and-suspenders rename in _unwrap_singletons.
    if "best_run" in out and "run" not in out:
        out["run"] = out.pop("best_run")
    return out


def unwrap_singleton(d: dict, key: str) -> dict:
    """card_testing_check.run is printed as a one-element list (GroupByAccum); return the object or {}."""
    v = d.get(key)
    if isinstance(v, list):
        return v[0] if v else {}
    return v or {}
