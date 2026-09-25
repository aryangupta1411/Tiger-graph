"""P10 — persist the case as memory (PLAN §4.2 P10, §3.6).

1. One pyTigerGraph `upsertVertex("AgentCase", id, attributes + note_emb)` call: the
   REST upsert accepts the vector as a plain list (`{"note_emb": {"value": [...]}}`),
   so attributes and the embedding land in one request (pyTigerGraph 2.0.4
   `upsertVertex` docstring example: `"embedding": [0.1, -0.2, 3.1e-2]`).
2. The MCP writers for events and approvals (`append_case_event`, `record_approval`
   via tigergraph__run_installed_query) and the edges via `tigergraph__add_edges`
   — so "the agent writes the case through MCP" is literally true and logged.
3. Poll the HNSW index (`GET /restpp/vector/status/{graph}/AgentCase/note_emb` via
   pyTigerGraph `getVectorStatus`) so the next case can retrieve this one.

In RUN_MODE=mock the pyTigerGraph part is replaced by a fake connection.

Savanna auto-resume: the upsert and the index poll run under `ops.ensure_awake.ensure_awake()`, the MCP
writers under `mcp_client.run_query`'s resume loop and the edges under `call_tool_with_resume`. Every write
is a keyed upsert (fixed AgentCase id, event seq, approval id), so a re-send after a dropped request is safe.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from agent import tools_local
from agent.config import SETTINGS
from agent.mcp_client import call_tool_with_resume, current_caller, run_query
from ops.ensure_awake import ensure_awake

EDGE_TYPES = {  # edge -> (source type, target type)
    "CASE_INVOLVES": ("AgentCase", "Transaction"), "CASE_ON_CARD": ("AgentCase", "Card"),
    "CASE_CONNECTED_TO": ("AgentCase", "Card"), "CASE_DEVICE": ("AgentCase", "DeviceProfile"),
    "CASE_SIMILAR_TO": ("AgentCase", "ClosedCase"), "CASE_SIMILAR_TO_AGENT": ("AgentCase", "AgentCase"),
    "CASE_CITES": ("AgentCase", "PolicyChunk"), "CASE_MATCHES": ("AgentCase", "FraudPattern"),
    "HAS_EVENT": ("AgentCase", "CaseEvent"), "HAS_APPROVAL": ("AgentCase", "Approval"),
}


class FakeTGConnection:
    """Stand-in for pyTigerGraph.TigerGraphConnection in RUN_MODE=mock."""
    upserts: list[tuple[str, str, dict]] = []

    def upsertVertex(self, vertexType: str, vertexId: str, attributes: dict | None = None) -> int:
        FakeTGConnection.upserts.append((vertexType, vertexId, attributes or {}))
        return 1

    def getVectorStatus(self, vertexType: str, vectorName: str = "") -> bool:
        return True

    def echo(self) -> str:
        return "Hello GSQL"


def tg_connection(settings=SETTINGS):
    if settings.mock:
        return FakeTGConnection()
    from pyTigerGraph import TigerGraphConnection
    return TigerGraphConnection(host=settings.tg_host, graphname=settings.tg_graphname, gsqlSecret=settings.tg_secret,
                                tgCloud=settings.tg_host.startswith("https://"), restppPort="9000", gsPort="14240")


def embed_text_for_case(ctx, sc, closing: dict, sar: dict, initial=(), final=(), requests=()) -> str:
    """AgentCase note text in the ClosedCase `embed_text` style so agent cases live in one space.

    D11 / M11: the phrasing is `rag.closedcase_embed_text.agent_case_embed_text` (which is the ETL's
    `closed_case_parsed.embed_text` template), never a third local format. The local template below is
    only the fallback for when the rag package is not importable.
    """
    dev = (sc.connected_device_profiles or [""])[0]
    region = (sc.chain[-1].get("addr1") if sc.chain else "") or ""
    answer_like = {"case": {"verdict": sc.verdict, "pattern": sc.pattern, "pattern_description": sc.pattern_description,
                            "summary": closing.get("summary", "")},
                   "evidence_requests": [dict(r) for r in requests],
                   "next_best_actions": {"final": [dict(a) for a in final]}}
    try:
        from rag.closedcase_embed_text import agent_case_embed_text
        return agent_case_embed_text(answer_like, device_profile=dev, region=str(region))
    except Exception:
        return (f"pattern: {sc.pattern} | outcome: {sc.verdict} | device: {dev or 'none'} | region: {region} | "
                f"trigger: {ctx.trigger_type} | channel: {(sc.chain[-1].get('channel') if sc.chain else '')} | "
                f"{closing.get('summary', '')}")


async def persist_case(pm: Any, ctx, sc, initial: list[dict], final: list[dict], requests: list[dict], sar: dict,
                       closing: dict, evidence: list[dict]) -> bool:
    """Write AgentCase (+vector), CaseEvents, Approvals and edges. Returns written_to_graph."""
    token = current_caller.set("persist")
    try:
        return await _persist(pm, ctx, sc, initial, final, requests, sar, closing, evidence)
    finally:
        current_caller.reset(token)


async def _persist(pm: Any, ctx, sc, initial: list[dict], final: list[dict], requests: list[dict], sar: dict,
                   closing: dict, evidence: list[dict]) -> bool:
    s = pm.s
    case_id = pm.graph_case_id
    log = pm.log
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    pending = bool(requests) and sc.flags.get("post_outcome") in ("no_reply", "inconclusive")
    status = pm.E.status.derive(sc.verdict, final, pending)
    attrs = {
        "source_case_id": ctx.case_id, "customer_id": ctx.customer_id, "card_id": ctx.card_id, "trigger_type": ctx.trigger_type,
        "opened_at": ctx.opened_at, "updated_at": now, "status": status, "verdict": sc.verdict, "fraud_probability": float(sc.p_engine),
        "pattern": sc.pattern, "pattern_description": sc.pattern_description, "exposure_usd": float(sc.exposure_usd),
        "summary": closing.get("summary", ""), "sar_filed": bool(sar.get("file")), "sar_narrative": sar.get("narrative", ""),
        "initial_actions": json.dumps(initial, sort_keys=True), "final_actions": json.dumps(final, sort_keys=True),
        "what_changed": closing.get("what_changed", "nothing"), "stop_reason": closing.get("stop_reason", ""), "run_id": pm.run_id,
    }
    t0 = time.time()
    written = False
    # 1. single pyTigerGraph upsert with the embedding
    try:
        vec = tools_local.embed_document(embed_text_for_case(ctx, sc, closing, sar, initial, final, requests))
        conn = pm.tg_conn_factory() if pm.tg_conn_factory else tg_connection(s)
        accepted = await asyncio.to_thread(ensure_awake()(conn.upsertVertex), "AgentCase", case_id, {**attrs, "note_emb": vec})
        written = int(accepted or 0) >= 1
        log.record_tool("pytg:upsertVertex(AgentCase)", {"id": case_id, "n_attrs": len(attrs), "dim": len(vec)}, time.time() - t0, 0, ok=written, caller="persist")
    except Exception as e:
        log.record_tool("pytg:upsertVertex(AgentCase)", {"id": case_id}, time.time() - t0, 0, ok=False, error=str(e), caller="persist")
        log.note("AgentCase upsert failed", error=str(e)[:300])
    # 2. events + approvals through the MCP writers
    seq = 0
    event_ids: list[str] = []
    for ev in pm.events + [{"kind": "executed", "payload": [a for a in final if a["route"] == "auto"]}]:
        seq += 1
        eid = f"{case_id}-{seq:03d}"
        try:
            # M8: append_case_event(case_id, seq, kind, event_at, payload) — event_at is a required DATETIME
            await run_query(pm.session, "append_case_event", {"case_id": case_id, "seq": seq, "kind": ev["kind"], "event_at": now,
                                                              "payload": json.dumps(ev["payload"], sort_keys=True, default=str)[:60000]})
            event_ids.append(eid)
        except Exception as e:
            log.note(f"append_case_event {eid} failed", error=str(e)[:200])
    approval_ids: list[str] = []
    for a in final:
        if a["route"] in ("L1", "L2"):
            aid = f"AP-{case_id}-{a['action']}"
            try:
                # M8: record_approval(case_id, action, route, status, decided_by, decided_at, reason) — the query
                # derives the Approval id itself ("AP-" + case_id + "-" + action); there is no approval_id parameter
                await run_query(pm.session, "record_approval", {"case_id": case_id, "action": a["action"], "route": a["route"],
                                                                "status": "pending", "decided_by": "", "decided_at": ctx.opened_at, "reason": a.get("reason", "")})
                approval_ids.append(aid)
            except Exception as e:
                log.note(f"record_approval {aid} failed", error=str(e)[:200])
    # 3. edges through tigergraph__add_edges (batched by edge type; same src/tgt types per batch)
    edges: dict[str, list[dict]] = {
        "CASE_ON_CARD": [{"target_id": ctx.card_id}],
        "CASE_INVOLVES": [{"target_id": i} for i in sc.episode_ids],
        "CASE_CONNECTED_TO": [{"target_id": c} for c in sc.connected_card_ids[:100]],
        "CASE_DEVICE": [{"target_id": d} for d in sc.connected_device_profiles],
        "CASE_SIMILAR_TO": [{"target_id": c, "score": 1.0} for c in (closing.get("similar_prior_cases_used") or sc.similar_prior_cases) if str(c).startswith("CC-")],
        "CASE_MATCHES": [{"target_id": sc.pattern}],
        "HAS_EVENT": [{"target_id": e} for e in event_ids],
        "HAS_APPROVAL": [{"target_id": a} for a in approval_ids],
    }
    for etype, rows in edges.items():
        if not rows:
            continue
        src_t, tgt_t = EDGE_TYPES[etype]
        payload = [{"source_id": case_id, "source_type": src_t, "target_type": tgt_t, **r} for r in rows]
        t1 = time.time()
        try:
            res = await call_tool_with_resume(pm.session, "tigergraph__add_edges", {"edge_type": etype, "edges": payload})
            ok = not getattr(res, "is_error", False) and '"success": true' in "".join(getattr(c, "text", "") for c in res.content).lower()
            log.record_tool(f"mcp:add_edges({etype})", {"n": len(payload)}, time.time() - t1, 0, ok=ok, caller="persist")
        except Exception as e:
            log.record_tool(f"mcp:add_edges({etype})", {"n": len(payload)}, time.time() - t1, 0, ok=False, error=str(e), caller="persist")
    # 4. index poll so the next case can retrieve this one
    if written:
        try:
            conn = pm.tg_conn_factory() if pm.tg_conn_factory else tg_connection(s)
            deadline = time.time() + 60
            ready = False
            while time.time() < deadline:
                ready = await asyncio.to_thread(ensure_awake()(conn.getVectorStatus), "AgentCase", "note_emb")
                if ready:
                    break
                await asyncio.sleep(2)
            log.note("vector index status after persist", ready=ready)
        except Exception as e:
            log.note("vector index poll failed", error=str(e)[:200])
    log.record_phase("P10", {"written_to_graph": written, "events": len(event_ids), "approvals": approval_ids, "status": status})
    return written
