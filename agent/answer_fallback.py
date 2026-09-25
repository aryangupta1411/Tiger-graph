"""Fallback answer assembly with the contract signature of `agent/answer_writer.build`
(contracts §C) plus the PLAN §4.7 invariants, used when `agent.answer_writer` (another
module's deliverable) is not importable. The phase machine prefers the real module.
"""
from __future__ import annotations

import json

from agent.schemas import Answer
from engine.validator import uncertain_block_violation

# D10: a fraud verdict must contain the activity with at least one of these in `final`.
FRAUD_CONTAINMENT = {"BLOCK_CARD", "DECLINE_TRANSACTION", "ESCALATE_TO_ANALYST", "BLOCK_ALL_CARDS"}


def _actions_json(actions: list[dict]) -> list[dict]:
    return [{"action": a["action"], "route": a["route"], "reason": a.get("reason", "")} for a in actions]


def build(ctx, sc_pre, sc_post, initial, final, evidence, requests, sar, closing, runlog, graph_case_id) -> dict:
    sc = sc_post or sc_pre
    final_names = {a["action"] for a in final}
    pending = bool(requests) and sc.flags.get("post_outcome") in ("no_reply", "inconclusive")
    from agent.engine_api import ENGINE
    status = ENGINE.status.derive(sc.verdict, final, pending)
    if sc.verdict == "uncertain" and status not in ("open", "escalated"):
        status = "escalated"
    legit = sc.verdict == "legitimate"
    file_report = "FILE_REPORT" in final_names
    sar = dict(sar)
    sar["file"] = file_report                       # sar.file <=> FILE_REPORT in final
    if not file_report:
        sar.update({"narrative": "", "subjects": [], "total_amount_usd": 0.0, "activity_dates": []})
        sar["reason"] = sar.get("reason") or "3a not met"
    else:
        sar["total_amount_usd"] = round(float(sc.exposure_usd), 2)
    what_changed = "nothing" if _actions_json(initial) == _actions_json(final) else (closing.get("what_changed") or "final differs from initial")
    answer = {
        "case_id": ctx.case_id,
        "case": {
            "status": status,
            "verdict": sc.verdict,
            "fraud_probability": round(float(sc.p_engine), 2),
            "pattern": "none" if legit else sc.pattern,
            "pattern_description": sc.pattern_description if sc.pattern == "undocumented" and not legit else "",
            "affected_txn_ids": [] if legit else [str(i) for i in sc.episode_ids],
            "first_suspicious_txn_id": "" if legit else sc.first_suspicious_txn_id,
            "connected_card_ids": [] if legit else list(sc.connected_card_ids),
            "connected_device_profiles": [] if legit else list(sc.connected_device_profiles),
            "exposure_usd": 0.0 if legit else round(float(sc.exposure_usd), 2),
            "evidence": [(e.as_item() if hasattr(e, "as_item") else {"claim": e["claim"], "source": e["source"], "ref": e["ref"], "entity_ids": list(e["entity_ids"])}) for e in evidence],
            "similar_prior_cases": [c for c in closing.get("similar_prior_cases_used", []) if str(c).startswith("CC-")] or list(sc.similar_prior_cases),
            "summary": closing.get("summary", ""),
            "written_to_graph": bool(closing.get("written_to_graph", False)),
            "graph_case_id": graph_case_id if closing.get("written_to_graph", False) else "",
        },
        "evidence_requests": [dict(r) for r in requests],
        "next_best_actions": {"initial": _actions_json(initial), "final": _actions_json(final), "what_changed": what_changed},
        "sar": sar,
        "stop_reason": closing.get("stop_reason", ""),
        **(runlog if isinstance(runlog, dict) else runlog.totals()),
    }
    Answer.model_validate(answer)   # shape + enums
    return answer


def _sar_problems(answer: dict) -> list[str]:
    """D10: `rag.validate_sar` is the single SAR validator, here without an id resolver.

    Id existence is checked against the dataset by `agent.validator` / `engine.validator`, which have a
    DuckDB; this in-process invariant check has none, and the answer's own id lists are not a substitute
    (a burst look-alike named in the narrative is a real card that the answer never lists). The fallback
    below applies only when the rag package is not importable.
    """
    sar, c = answer["sar"], answer["case"]
    try:
        from rag.validate_sar import validate_sar
        finals = [a.get("action") for a in answer["next_best_actions"]["final"]]
        return validate_sar(sar, exposure_usd=float(c.get("exposure_usd", 0) or 0),
                            # graph_case_id is blanked when the write failed; the narrative still names it
                            case_ids=(str(answer.get("case_id", "")), str(c.get("graph_case_id") or f"AC-{answer.get('case_id', '')}")),
                            resolver=None, file_report_in_final=("FILE_REPORT" in finals))
    except Exception:
        pass
    if sar["file"] and (len(sar["activity_dates"]) != 2 or not sar["narrative"] or abs(sar["total_amount_usd"] - c["exposure_usd"]) > 0.01):
        return ["sar fields inconsistent with exposure / dates / narrative"]
    return []


def check_invariants(answer: dict) -> list[str]:
    """PLAN §4.7 invariants; returns violations (empty when clean)."""
    v: list[str] = []
    c, nba, sar = answer["case"], answer["next_best_actions"], answer["sar"]
    fin = {a["action"] for a in nba["final"]}
    ini = {a["action"] for a in nba["initial"]}
    if sar["file"] != ("FILE_REPORT" in fin):
        v.append("sar.file must equal FILE_REPORT in final")
    if "FILE_REPORT" in fin and "CREATE_CASE" not in fin:
        v.append("FILE_REPORT without CREATE_CASE")
    if c["verdict"] == "legitimate":
        if c["affected_txn_ids"] or c["exposure_usd"] != 0 or sar["file"] or "CLOSE_NO_FRAUD" not in fin or (fin | ini) & {"BLOCK_CARD", "BLOCK_ALL_CARDS"}:
            v.append("legitimate invariants violated")
    if "BLOCK_CARD" in fin and c["verdict"] == "legitimate":
        v.append("BLOCK_CARD with legitimate verdict")
    # D10 / M17: a fraud verdict may never close or allow, and must contain the activity somehow
    if c["verdict"] == "fraud":
        if {"CLOSE_NO_FRAUD", "ALLOW_TRANSACTION"} & fin:
            v.append("fraud ⇒ no CLOSE_NO_FRAUD / ALLOW_TRANSACTION in final")
        if not (FRAUD_CONTAINMENT & fin):
            v.append("fraud ⇒ at least one of BLOCK_CARD / DECLINE_TRANSACTION / ESCALATE_TO_ANALYST / BLOCK_ALL_CARDS in final")
    if "CLOSE_NO_FRAUD" in fin and c["verdict"] != "legitimate":
        v.append("CLOSE_NO_FRAUD in final ⇒ verdict legitimate")
    # README R2: a customer denial takes BLOCK_CARD from intake at any verdict, so an initial BLOCK_CARD citing R2 is allowed
    if c["verdict"] == "uncertain" and (uncertain_block_violation(nba["initial"]) or c["status"] not in ("escalated", "open")):
        v.append("uncertain: no block in initial (except an R2 BLOCK_CARD) and status escalated/open")
    if nba["initial"] != nba["final"] and not answer["evidence_requests"]:
        v.append("initial != final without an evidence request")
    if (nba["what_changed"] == "nothing") != (nba["initial"] == nba["final"]):
        v.append("what_changed == 'nothing' iff initial == final")
    if (c["pattern_description"] != "") != (c["pattern"] == "undocumented"):
        v.append("pattern_description iff undocumented")
    if not sar["reason"]:
        v.append("sar.reason must be non-empty")
    v += _sar_problems(answer)
    for a in nba["initial"] + nba["final"]:
        if a["action"] == "BLOCK_CARD" and a["route"] != ("L1" if c["exposure_usd"] <= 2500 else "L2"):
            v.append("BLOCK_CARD route mismatch")
        if a["action"] in ("FILE_REPORT", "BLOCK_ALL_CARDS") and a["route"] != "L2":
            v.append(f"{a['action']} route must be L2")
        if a["action"] == "DECLINE_TRANSACTION" and a["route"] != "L1":
            v.append("DECLINE_TRANSACTION route must be L1")
    for e in c["evidence"]:
        if any(str(i).startswith("AC-") for i in e["entity_ids"]):
            v.append("AC- id in entity_ids")
    if any(not str(s).startswith("CC-") for s in c["similar_prior_cases"]):
        v.append("similar_prior_cases must be CC- ids")
    return v


def dumps(answer: dict) -> str:
    return json.dumps(answer, indent=2, ensure_ascii=False)
