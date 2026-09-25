"""RUN_MODE=mock fixtures: a FakeSession that answers `tigergraph__run_installed_query`
with canned printed JSON in the exact contract shapes (contracts §A), formatted the
way tigergraph-mcp's `format_success()` formats a real result (```json ToolResponse```
followed by the human-readable copy), so the parser in `mcp_client.run_query` is
exercised for real.

The HHG-014 fixture reproduces the ring facts established on the real data (PLAN §2.2
#7, §4.3 D): the exact profile string, 19 pre-open / 27 wave cards, the two ring
transactions on the card before opening, the four undocumented closed cases.
Other cases get generic empty-but-well-formed results so the dry run never crashes.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

RING = "SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080"
WAVE_PRE_OPEN = [f"C{n:05d}-K1" for n in (255, 1935, 3551, 3744, 4311, 6197, 6617, 7485, 7762, 8112, 9174, 9354, 9733, 9998, 10350, 10955, 11468, 11687, 12033)]
WAVE_POST_OPEN = [f"C{n:05d}-K2" for n in (12132, 12395, 12574, 12900, 1002, 2044, 3090, 4077)]
WAVE_ALL = WAVE_PRE_OPEN + WAVE_POST_OPEN
CLOSED_RING = ["CC-2649", "CC-2971", "CC-2985", "CC-3035"]

HHG014 = {
    "case_context": {
        "txn": {"id": "3478561", "ts": "2016-11-22 16:11:00", "amt": 74.96, "product_cd": "C", "channel": "online", "addr1": "191.0", "addr2": "87",
                "p_email": "yahoo.com", "r_email": "yahoo.com", "risk_score": 0.05, "has_identity": True, "device_new": "New", "proxy": "IP_PROXY:ANONYMOUS",
                "device_type": "mobile", "device_id": RING, "cms_p": 0.08, "card_seq": 41, "prior_in_region": 38, "prior_on_dev": 1, "prior_pem": 39,
                "prior_pcd": 2, "prior_med_amt": 59.0, "prior_max_amt": 226.0, "ring_hit": True, "burst_id": ""},
        "card": {"id": "C13487-K1", "card_type": "debit", "card_network": "visa", "modal_region": "191.0", "n_txns": 43, "ring_id": RING},
        "customer": {"id": "C13487", "n_cards": 1},
        "device": {"id": RING, "is_strong": True, "n_cards_alltime": 52, "n_cards_30d": 28, "n_proxy": 114, "n_fraud_cases": 4},
    },
    "card_profile": {
        "card": {"id": "C13487-K1", "customer_id": "C13487", "n_txns": 43, "median_amt": 59.0, "max_amt": 226.0, "n_online": 6, "n_in_person": 37,
                 "modal_region": "191.0", "n_regions": 2, "ring_id": RING, "n_prior_fraud_cases": 0, "n_prior_cleared_cases": 0},
        "last30": {"n": 5, "sum_amt": 402.11, "n_online": 3, "n_regions": 1, "n_devices": 2, "n_products": 2},
        "regions": [{"addr1": "191.0", "n": 38, "days": 30, "first_ts": "2016-07-03 10:02:11", "last_ts": "2016-11-21 18:40:02"}],
        "devices": [{"device_id": RING, "n": 2, "first_ts": "2016-11-15 12:20:00", "is_strong": True},
                    {"device_id": "Windows | Windows 10 | chrome 63.0 | 1920x1080", "n": 3, "first_ts": "2016-08-11 09:12:44", "is_strong": False}],
        "emails": [{"domain": "yahoo.com", "n": 40}],
        "other_cards": [],
        "prior_closed_cases": [],
        "prior_agent_cases": [],
    },
    "card_window": {
        "txns": [
            {"id": "3459920", "ts": "2016-11-15 12:20:00", "amt": 112.37, "product_cd": "C", "channel": "online", "addr1": "191.0", "p_email": "yahoo.com", "r_email": "yahoo.com", "risk_score": 0.07, "cms_p": 0.06, "device_id": RING, "device_new": "New", "proxy": "IP_PROXY:ANONYMOUS", "gap_seconds": 0},
            {"id": "3471102", "ts": "2016-11-20 09:15:31", "amt": 41.2, "product_cd": "W", "channel": "in_person", "addr1": "191.0", "p_email": "", "r_email": "", "risk_score": 0.03, "cms_p": 0.01, "device_id": "", "device_new": "", "proxy": "", "gap_seconds": 421531},
            {"id": "3478561", "ts": "2016-11-22 16:11:00", "amt": 74.96, "product_cd": "C", "channel": "online", "addr1": "191.0", "p_email": "yahoo.com", "r_email": "yahoo.com", "risk_score": 0.05, "cms_p": 0.08, "device_id": RING, "device_new": "New", "proxy": "IP_PROXY:ANONYMOUS", "gap_seconds": 197729},
        ],
        "summary": {"n": 3, "sum_amt": 228.53, "n_online": 2, "n_in_person": 1},
    },
    "prior_cases_for_customer": {"closed_cases": [], "agent_cases": []},
    "device_history": {"prior_n": 1, "first_ts": "2016-11-15 12:20:00", "device_new_values": ["New"]},
    "device_neighbors": {
        "device": {"id": RING, "is_strong": True, "n_cards_alltime": 52, "n_cards_30d": 28, "n_proxy": 114, "n_fraud_cases": 4},
        "cards": [{"card_id": c, "customer_id": c.split("-")[0], "n_txns": 1, "sum_amt": 88.0, "n_proxy": 1, "n_new": 1, "first_ts": "2016-11-03 08:00:00", "last_ts": "2016-11-21 22:00:00"} for c in WAVE_PRE_OPEN],
        "closed_cases": [{"id": cc, "card_id": card, "pattern": "undocumented", "outcome": "confirmed_fraud", "report_filed": True}
                         for cc, card in zip(CLOSED_RING, ["C03528-K1", "C00611-K1", "C05120-K2", "C07903-K1"])],
        "agent_cases": [],
    },
    "shared_origin_scan": {"devices": [{"device_id": RING, "is_strong": True, "n_cards_30d": 28, "n_fraud_cases": 4, "n_txns_on_card": 2}],
                           "recipient_emails": [{"domain": "yahoo.com", "n_cards_30d": 3100, "n_fraud_cases": 210}],
                           "region_cluster_30d": "[]"},
    "episode_candidates": {"chain": [
        {"id": "3459920", "ts": "2016-11-15 12:20:00", "amt": 112.37, "product_cd": "C", "channel": "online", "addr1": "191.0", "p_email": "yahoo.com", "device_id": RING, "device_new": "New", "cms_p": 0.06, "sig_match": True},
        {"id": "3478561", "ts": "2016-11-22 16:11:00", "amt": 74.96, "product_cd": "C", "channel": "online", "addr1": "191.0", "p_email": "yahoo.com", "device_id": RING, "device_new": "New", "cms_p": 0.08, "sig_match": True},
    ]},
    "ring_profile": {
        "ring_id": RING,
        "device": {"id": RING, "is_strong": True, "n_cards_alltime": 52, "n_cards_30d": 28, "n_proxy": 114, "n_fraud_cases": 4},
        "wave_cards": list(WAVE_ALL),
        "pre_open_cards": list(WAVE_PRE_OPEN),
        "card_txns_on_ring": [{"id": "3459920", "ts": "2016-11-15 12:20:00", "amt": 112.37}, {"id": "3478561", "ts": "2016-11-22 16:11:00", "amt": 74.96}],
        "closed_cases": [{"id": cc, "card_id": card, "pattern": "undocumented", "outcome": "confirmed_fraud", "report_filed": True}
                         for cc, card in zip(CLOSED_RING, ["C03528-K1", "C00611-K1", "C05120-K2", "C07903-K1"])],
    },
    "card_testing_check": {"run": {}, "larger_purchase": {}, "cleared_over_big": False, "chain": {"n_members": 2, "n_small": 0}},
    "under_threshold_burst": {"bursts": [], "lookalike_cards": []},
    "recurring_charge_check": {"groups": [], "total_n": 0},
    "region_history": {"region": {"addr1": "191.0", "prior_n": 38, "prior_days": 30, "first_ts": "2016-07-03 10:02:11", "last_ts": "2016-11-21 18:40:02", "share": 0.9},
                       "modal_region": "191.0", "n_regions": 2, "home_activity_48h": {"n_home": 1, "n_other": 0}, "hint": "home"},
    "email_neighbors": {"cards": [], "closed_cases": []},
    "post_open_activity": {"txns": [{"id": "3488102", "ts": "2016-11-26 11:03:00", "amt": 252.28, "channel": "online", "addr1": "191.0", "device_new": "New", "cms_p": 0.09}], "summary": {"n": 1, "sum_amt": 252.28}},
    "similar_prior_cases": {"cases": [
        {"id": "CC-2649", "kind": "closed", "outcome_or_verdict": "confirmed_fraud", "pattern": "undocumented", "exposure_usd": 390.04, "opened_at": "2016-08-19 10:00:00", "distance": 0.12, "overlap_reasons": ["same device profile", "pattern undocumented"]},
        {"id": "CC-2971", "kind": "closed", "outcome_or_verdict": "confirmed_fraud", "pattern": "undocumented", "exposure_usd": 211.5, "opened_at": "2016-08-30 14:00:00", "distance": 0.13, "overlap_reasons": ["same device profile"]},
        {"id": "CC-2985", "kind": "closed", "outcome_or_verdict": "confirmed_fraud", "pattern": "undocumented", "exposure_usd": 175.2, "opened_at": "2016-09-01 09:00:00", "distance": 0.14, "overlap_reasons": ["same device profile"]},
        {"id": "CC-3035", "kind": "closed", "outcome_or_verdict": "confirmed_fraud", "pattern": "undocumented", "exposure_usd": 302.9, "opened_at": "2016-09-04 16:00:00", "distance": 0.15, "overlap_reasons": ["same device profile"]},
        {"id": "CC-0873", "kind": "closed", "outcome_or_verdict": "cleared", "pattern": "none", "exposure_usd": 0.0, "opened_at": "2016-08-02 12:00:00", "distance": 0.41, "overlap_reasons": ["diversity: cleared"]},
    ]},
    "grounding_chunks": {"chunks": [
        {"id": "fraud_policy#R6", "doc_id": "fraud_policy", "section": "R6", "page": 1, "kind": "policy", "text": "R6. Shared origin. When several cards show fraud from the same device profile, the same billing region, or the same recipient email in one window, name the shared element, recommend CREATE_CASE and FILE_REPORT, and MONITOR_CONNECTED_CARDS for every card that shares it.", "distance": 0.10},
        {"id": "fraud_policy#R9", "doc_id": "fraud_policy", "section": "R9", "page": 1, "kind": "policy", "text": "R9. Undocumented patterns. When activity fits none of the known patterns but the evidence shows coordinated or repeated abuse across customers, recommend CREATE_CASE, FILE_REPORT, and ESCALATE_TO_ANALYST, and describe the pattern in your own words.", "distance": 0.12},
        {"id": "fraud_policy#3a", "doc_id": "fraud_policy", "section": "3a", "page": 1, "kind": "policy", "text": "3a. File a report when fraud is confirmed or strongly suspected and at least one of these holds: exposure exceeds $1,000; the activity connects to a shared device profile, a shared region cluster, or another customer's fraud; the pattern is coordinated or undocumented.", "distance": 0.13},
        {"id": "sar_guidance#p04-when", "doc_id": "sar_guidance", "section": "The narrative: who, what, when, where, why, how", "page": 4, "kind": "regulation", "text": "The narrative should identify the essential elements of information — who? what? when? where? why? how? — describing the suspicious activity, including the instruments used, dates, amounts, locations and the reason the activity is unusual for the customer.", "distance": 0.18},
        {"id": "sar_guidance#p06-how", "doc_id": "sar_guidance", "section": "How was the activity conducted", "page": 6, "kind": "regulation", "text": "Describe how the suspicious activity was conducted, the method of operation, any instruments or devices used, and follow-up actions the institution has taken.", "distance": 0.21},
    ]},
    "open_case": {"ok": True, "id": "AC-HHG-014"},
    "append_case_event": {"ok": True, "id": "AC-HHG-014-001"},
    "close_case": {"ok": True, "id": "AC-HHG-014"},
    "record_approval": {"ok": True, "id": "AP-AC-HHG-014-FILE_REPORT"},
}

GENERIC = {
    "case_context": {"txn": {}, "card": {}, "customer": {}, "device": {}},
    "card_profile": {"card": {}, "last30": {}, "regions": [], "devices": [], "emails": [], "other_cards": [], "prior_closed_cases": [], "prior_agent_cases": []},
    "card_window": {"txns": [], "summary": {"n": 0, "sum_amt": 0.0, "n_online": 0, "n_in_person": 0}},
    "region_history": {"region": {}, "modal_region": "", "n_regions": 0, "home_activity_48h": {"n_home": 0, "n_other": 0}, "hint": "new"},
    "device_history": {"prior_n": 0, "first_ts": "", "device_new_values": []},
    "device_neighbors": {"device": {}, "cards": [], "closed_cases": [], "agent_cases": []},
    "email_neighbors": {"cards": [], "closed_cases": []},
    "shared_origin_scan": {"devices": [], "recipient_emails": [], "region_cluster_30d": "[]"},
    "card_testing_check": {"run": {}, "larger_purchase": {}, "cleared_over_big": False, "chain": {"n_members": 0, "n_small": 0}},
    "under_threshold_burst": {"bursts": [], "lookalike_cards": []},
    "recurring_charge_check": {"groups": [], "total_n": 0},
    "episode_candidates": {"chain": []},
    "prior_cases_for_customer": {"closed_cases": [], "agent_cases": []},
    "ring_profile": {"ring_id": "", "device": {}, "wave_cards": [], "pre_open_cards": [], "card_txns_on_ring": [], "closed_cases": []},
    "case_subgraph": {"nodes": [], "edges": []},
    "post_open_activity": {"txns": [], "summary": {"n": 0, "sum_amt": 0.0}},
    "similar_prior_cases": {"cases": []},
    "grounding_chunks": {"chunks": []},
    "open_case": {"ok": True, "id": ""}, "append_case_event": {"ok": True, "id": ""},
    "close_case": {"ok": True, "id": ""}, "record_approval": {"ok": True, "id": ""},
}

FIXTURES: dict[str, dict[str, dict]] = {"HHG-014": HHG014}


def format_like_mcp(query_name: str, params: dict, printed: dict) -> str:
    """Reproduce tigergraph_mcp.response_formatter.format_success() for run_installed_query."""
    data = {"query_name": query_name, "parameters": params, "result": [printed]}
    resp = {"success": True, "operation": "run_installed_query", "data": data,
            "summary": f"Success: Query '{query_name}' executed successfully",
            "metadata": {"graph_name": "FraudGraph", "execution_mode": "installed"},
            "suggestions": ["Tip: Installed queries are much faster than interpreted queries"],
            "timestamp": "2026-09-19T00:00:00Z"}
    json_output = json.dumps(resp, indent=2)
    text = f"**{resp['summary']}**\n\n**Data:**\n```json\n{json.dumps(data, indent=2, default=str)}\n```\n"
    return f"```json\n{json_output}\n```\n\n{text}"


def _duck_facts():
    """engine.facts_duckdb.DuckFacts when the engine module and its facts DB exist (the
    DuckDB implementation of every installed-query contract), else None."""
    try:
        from engine import config as ecfg
        from engine.facts_duckdb import DuckFacts
        if not ecfg.FACTS_DB.exists():
            return None
        return DuckFacts()
    except Exception:
        return None


class FakeSession:
    """Stand-in for mcp.ClientSession in RUN_MODE=mock.

    Read queries are answered by engine.facts_duckdb.DuckFacts (real data, every case)
    when available, else by the hand fixtures above (HHG-014) / GENERIC shapes.
    """

    def __init__(self, case_id: str = "HHG-014", use_duckdb: bool = True) -> None:
        self.case_id = case_id
        self.calls: list[tuple[str, dict]] = []
        self.fx = _duck_facts() if use_duckdb else None

    def use_case(self, case_id: str) -> None:
        self.case_id = case_id

    def tools(self) -> list[Any]:
        return [SimpleNamespace(name="tigergraph__run_installed_query", description="fake", input_schema={}),
                SimpleNamespace(name="tigergraph__add_edges", description="fake", input_schema={})]

    async def initialize(self):
        return None

    async def list_tools(self):
        return SimpleNamespace(tools=self.tools())

    async def call_tool(self, name: str, arguments: dict | None = None, **_: Any):
        arguments = arguments or {}
        self.calls.append((name, arguments))
        if name == "tigergraph__run_installed_query":
            q = arguments.get("query_name", "")
            fx = FIXTURES.get(self.case_id, {})
            printed = None
            if self.fx is not None and hasattr(self.fx, q) and q not in ("open_case", "append_case_event", "close_case", "record_approval"):
                try:
                    printed = getattr(self.fx, q)(**arguments.get("params", {}))
                except Exception as e:  # fall through to the fixture / generic shape
                    printed = fx.get(q, GENERIC.get(q))
                    printed = dict(printed or {}, _duckdb_error=str(e)[:200]) if printed is not None else None
                if q == "grounding_chunks" and printed is not None and not printed.get("chunks"):
                    printed = fx.get(q) or HHG014["grounding_chunks"]   # DuckFacts has no PolicyChunk vectors
            if printed is None:
                printed = fx.get(q, GENERIC.get(q))
            if printed is None:
                text = f"```json\n{json.dumps({'success': False, 'operation': 'run_installed_query', 'summary': 'Failed', 'error': f'query {q} not installed'})}\n```"
                return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], is_error=False, structured_content=None)
            if q == "device_neighbors" and self.case_id == "HHG-014" and arguments.get("params", {}).get("to_ts", "") > "2016-11-22 20:11:00":
                printed = dict(printed, cards=[{"card_id": c, "customer_id": c.split("-")[0], "n_txns": 1, "sum_amt": 90.0, "n_proxy": 1, "n_new": 1, "first_ts": "2016-11-03 08:00:00", "last_ts": "2016-12-20 22:00:00"} for c in WAVE_ALL])
            text = format_like_mcp(q, arguments.get("params", {}), printed)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], is_error=False, structured_content=None)
        if name in ("tigergraph__add_edges", "tigergraph__add_edge", "tigergraph__add_node", "tigergraph__add_nodes", "tigergraph__get_vector_index_status"):
            payload = {"success": True, "operation": name.replace("tigergraph__", ""), "summary": "ok", "data": {"accepted": len(arguments.get("edges", [])) or 1, "status": "Ready_for_query"}}
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=f"```json\n{json.dumps(payload)}\n```")], is_error=False, structured_content=None)
        payload = {"success": False, "operation": name, "summary": "not served", "error": f"Tool '{name}' is not available on this server."}
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=f"```json\n{json.dumps(payload)}\n```")], is_error=False, structured_content=None)
