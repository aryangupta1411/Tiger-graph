"""Contract-exact stub of the engine interfaces (contracts §B) used when the real
`engine/` package is not importable (RUN_MODE=mock, CI, or before Module D lands).

It implements the decision rules of PLAN §4.4–§4.7 in compact form so the phase
machine can be exercised end to end; the real engine is the version of record and
`agent/engine_api.py` prefers it whenever `engine.scorecard` imports.

Names, dataclasses and signatures are EXACTLY those of contracts §B.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field

# ---------------------------------------------------------------- engine/types.py


@dataclass
class CaseContext:
    case_id: str
    trigger_type: str
    trigger_text: str
    flagged_txn_id: str
    card_id: str
    customer_id: str
    opened_at: str  # 'YYYY-MM-DD HH:MM:SS'
    risk_score: float | None


@dataclass
class Evidence:  # one row of the ledger; also the answer file's evidence item
    claim: str
    source: str  # graph | document | customer | external
    ref: str
    entity_ids: list[str]
    family: str  # history | device | memory | customer | document
    direction: str  # fraud | legit | neutral
    weight: float = 1.0

    def as_item(self) -> dict:
        return {"claim": self.claim, "source": self.source, "ref": self.ref, "entity_ids": list(self.entity_ids)}


@dataclass
class Scorecard:
    cms_p: float
    cal_p: float
    adjustments: list[tuple[str, float]]
    p_engine: float
    families_fraud: set[str]
    families_legit: set[str]
    flags: dict
    chain: list[dict]
    episode_ids: list[str]
    first_suspicious_txn_id: str
    exposure_usd: float
    connected_card_ids: list[str]
    connected_device_profiles: list[str]
    pattern: str
    pattern_description: str
    verdict: str  # fraud | legitimate | uncertain
    similar_prior_cases: list[str] = field(default_factory=list)


Facts = dict[str, dict]


# ---------------------------------------------------------------- helpers


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def _cal(cms_p: float) -> float:
    """Stand-in for the isotonic calibration table (PLAN §4.1 #4 bins)."""
    if cms_p < 0.05:
        return 0.007 + cms_p * 0.5
    if cms_p < 0.30:
        return 0.03 + (cms_p - 0.05) * 1.2
    if cms_p < 0.50:
        return 0.33 + (cms_p - 0.30) * 0.65
    if cms_p < 0.85:
        return 0.46 + (cms_p - 0.50) * 0.9
    return 0.78 + (cms_p - 0.85) * 1.0


def _verdict(p: float) -> str:
    if p >= 0.70:
        return "fraud"
    if p <= 0.15:
        return "legitimate"
    return "uncertain"


# ---------------------------------------------------------------- engine/pattern_rule.py


def label(chain_members: list[dict], card_modal_region: str, flags: dict) -> str:
    """Detectors first, then the generator convention (PLAN §4.4)."""
    if flags.get("ring_hit") or flags.get("burst_hit"):
        return "undocumented"
    if not chain_members:
        return "card_not_present_fraud"
    channels = {m.get("channel", "") for m in chain_members}
    if flags.get("card_testing_chain") or (channels == {"online"} and len(chain_members) >= 5 and any(float(m.get("amt", 0)) < 5 for m in chain_members)):
        return "card_testing"
    if "online" in channels and "in_person" in channels:
        return "account_takeover"
    if channels == {"in_person"}:
        if all(str(m.get("addr1", "")) == str(card_modal_region) for m in chain_members):
            return "account_takeover"
        return "out_of_region_use"
    if any(m.get("device_new") == "New" for m in chain_members):
        return "card_not_present_new_device"
    return "card_not_present_fraud"


# ---------------------------------------------------------------- engine/episode.py


def members(chain: list[dict], flagged_id: str, verdict: str, flags: dict) -> list[str]:
    if verdict == "legitimate":
        return []
    threshold = 0.5 if verdict == "uncertain" else 0.30
    out: list[str] = []
    for m in chain:
        tid = str(m.get("id", ""))
        keep = (
            tid == str(flagged_id)
            or float(m.get("cms_p", 0) or 0) >= threshold
            or bool(m.get("sig_match"))
            or (flags.get("ring_hit") and m.get("device_id") == flags.get("ring_id"))
            or (flags.get("burst_hit") and tid in set(map(str, flags.get("burst_member_ids", []))))
            or (flags.get("card_testing_chain") and tid in set(map(str, flags.get("card_testing_ids", []))))
        )
        if keep and tid and tid not in out:
            out.append(tid)
    if str(flagged_id) not in out:
        out.append(str(flagged_id))
    return out


# ---------------------------------------------------------------- engine/scorecard.py


def compute(ctx: CaseContext, facts: Facts, evidence: list[Evidence]) -> Scorecard:
    cc = facts.get("case_context", {}) or {}
    txn = cc.get("txn", {}) or {}
    card = cc.get("card", {}) or {}
    device = cc.get("device", {}) or {}
    chain = list((facts.get("episode_candidates", {}) or {}).get("chain", []) or [])
    if not chain and txn:
        chain = [dict(txn, sig_match=False)]
    cms_p = float(txn.get("cms_p", 0.0) or 0.0)
    cal_p = _cal(cms_p)
    adjustments: list[tuple[str, float]] = []
    flags: dict = {
        "ring_hit": bool(txn.get("ring_hit")) or bool(card.get("ring_id")),
        "ring_id": card.get("ring_id", "") or "",
        "burst_hit": bool(txn.get("burst_id")),
        "burst_member_ids": [],
        "card_testing_chain": False,
        "card_testing_ids": [],
        "recurring_match": False,
        "denial": ctx.trigger_type == "customer_report",
        "conflict": False,
        "scorer_unreliable": int(card.get("n_txns", 0) or 0) > 3000 or int(txn.get("card_seq", 99) or 99) < 5,
        "mixed_channel": len({m.get("channel") for m in chain}) > 1,
        "any_new_member": any(m.get("device_new") == "New" for m in chain),
        "all_in_modal_region": bool(chain) and all(str(m.get("addr1")) == str(card.get("modal_region")) for m in chain),
        "device_known": int((facts.get("device_history", {}) or {}).get("prior_n", 0) or 0) > 0,
        "new_device": txn.get("device_new") == "New",
    }
    bursts = (facts.get("under_threshold_burst", {}) or {}).get("bursts", []) or []
    for b in bursts:
        flags["burst_member_ids"] += [str(i) for i in b.get("ids", [])]
    ct = facts.get("card_testing_check", {}) or {}
    run = ct.get("run", {}) or {}
    if run.get("ids") and int((ct.get("chain", {}) or {}).get("n_members", 0) or 0) >= 5 and int((ct.get("chain", {}) or {}).get("n_small", 0) or 0) >= 1:
        flags["card_testing_chain"] = True
        flags["card_testing_ids"] = [str(i) for i in run.get("ids", [])]
    flags["card_testing_r5"] = bool(run.get("ids"))
    flags["card_testing_cleared_big"] = bool(ct.get("cleared_over_big"))
    rc = facts.get("recurring_charge_check", {}) or {}
    for g in rc.get("groups", []) or []:
        if int(g.get("n", 0)) >= 5 and 5 <= float(g.get("median_gap_days", 0) or 0) <= 35 and float(g.get("gap_cv", 9) or 9) <= 0.6 and cms_p < 0.30:
            flags["recurring_match"] = True

    # families (PLAN §4.5)
    fam_f: set[str] = set()
    fam_l: set[str] = set()
    for e in evidence:
        if e.direction == "fraud":
            fam_f.add(e.family)
        elif e.direction == "legit":
            fam_l.add(e.family)
    if cms_p >= 0.5:
        fam_f.add("memory")
    if cms_p <= 0.10:
        fam_l.add("memory")
    if flags["denial"]:
        fam_f.add("customer")
    strong_shared = bool(device.get("is_strong")) and int(device.get("n_fraud_cases", 0) or 0) >= 2
    if flags["ring_hit"] or strong_shared:
        fam_f.add("device")
    prior = (facts.get("prior_cases_for_customer", {}) or {}).get("closed_cases", []) or []
    if any(c.get("outcome") == "confirmed_fraud" for c in prior):
        fam_f.add("memory")
    if any(c.get("outcome") == "cleared" for c in prior):
        fam_l.add("memory")
    prior_max = float(txn.get("prior_max_amt", 0) or 0)
    if prior_max and float(txn.get("amt", 0) or 0) > 2 * prior_max:
        fam_f.add("history")
    rh = facts.get("region_history", {}) or {}
    if rh.get("hint") in ("home", "known"):
        fam_l.add("history")
    if rh.get("hint") == "new" and txn.get("channel") == "in_person":
        fam_f.add("history")

    # adjustments
    x = _logit(cal_p)
    if flags["ring_hit"]:
        adjustments.append(("ring profile floor 0.90", 0.0))
    if flags["burst_hit"] and flags["denial"]:
        adjustments.append(("under-$500 burst with denial floor 0.90", 0.0))
    if flags["card_testing_chain"]:
        adjustments.append(("card-testing chain", 1.4))
        x += 1.4
    if flags["recurring_match"]:
        adjustments.append(("R7 recurring match", -1.2))
        x -= 1.2
    if strong_shared:
        adjustments.append(("strong shared profile with >=2 fraud cards", 1.4))
        x += 1.4
    if any(c.get("outcome") == "cleared" for c in prior):
        adjustments.append(("prior cleared alert on card", -0.5))
        x -= 0.5
    if any(c.get("outcome") == "confirmed_fraud" and c.get("pattern") in ("card_testing", "card_not_present_fraud", "card_not_present_new_device") for c in prior):
        adjustments.append(("recent CNP/card-testing case on card", 0.5))
        x += 0.5
    p = _sigmoid(max(min(x, _logit(cal_p) + 2.2), _logit(cal_p) - 2.2))
    if flags["scorer_unreliable"]:
        p = 0.5 + 0.7 * (p - 0.5)
        adjustments.append(("scorer unreliable: shrink 30% to 0.5", 0.0))
    if flags["denial"] and not flags["recurring_match"]:
        p = max(p, 0.55)
        adjustments.append(("customer denial floor 0.55", 0.0))
    if flags["ring_hit"] or (flags["burst_hit"] and flags["denial"]):
        p = max(p, 0.90)
    elif flags["burst_hit"]:
        p = max(p, 0.55)
    # conflict override
    chain_value = sum(abs(float(m.get("amt", 0) or 0)) for m in chain)
    if cms_p < 0.30 and chain_value > 500 and (flags["mixed_channel"] or (flags["new_device"] and rh.get("hint") == "new")) and not flags["ring_hit"] and not flags["burst_hit"]:
        p = min(max(p, 0.30), 0.50)
        flags["conflict"] = True
        adjustments.append(("conflict override: rulebook chain vs scorer", 0.0))
    if fam_f and fam_l and not flags["ring_hit"]:
        flags["conflict"] = flags["conflict"] or (0.30 <= p < 0.70)
    p = round(min(max(p, 0.01), 0.99), 3)

    verdict = _verdict(p)
    if flags["denial"] and verdict == "legitimate":
        verdict = "uncertain"
    pattern = label(chain, str(card.get("modal_region", "")), flags) if verdict != "legitimate" else "none"
    ep = members(chain, ctx.flagged_txn_id, verdict, flags)
    amt_by_id = {str(m.get("id")): abs(float(m.get("amt", 0) or 0)) for m in chain}
    if str(ctx.flagged_txn_id) not in amt_by_id and txn:
        amt_by_id[str(ctx.flagged_txn_id)] = abs(float(txn.get("amt", 0) or 0))
    exposure = round(sum(amt_by_id.get(i, 0.0) for i in ep), 2)
    ts_by_id = {str(m.get("id")): m.get("ts", "") for m in chain}
    first = min(ep, key=lambda i: ts_by_id.get(i, "9999")) if ep else ""

    connected: list[str] = []
    profiles: list[str] = []
    rp = facts.get("ring_profile", {}) or {}
    dn = facts.get("device_neighbors", {}) or {}
    if flags["ring_hit"]:
        for c in rp.get("wave_cards", []) or dn.get("cards", []) or []:
            cid = c.get("card_id") if isinstance(c, dict) else c
            if cid and cid != ctx.card_id and cid not in connected:
                connected.append(cid)
        if flags["ring_id"]:
            profiles.append(flags["ring_id"])
    elif strong_shared and device.get("id"):
        profiles.append(device["id"])
        for c in dn.get("cards", []) or []:
            cid = c.get("card_id")
            if cid and cid != ctx.card_id and cid not in connected:
                connected.append(cid)
    if verdict == "legitimate":
        connected, profiles = [], []
    pattern_description = ""
    if pattern == "undocumented":
        if flags["ring_hit"]:
            pattern_description = (
                f"Anonymous-proxy device ring: the strong device profile `{flags['ring_id']}` behind an anonymous proxy "
                f"is used across {len(connected)} other cards in the same wave, all online product code C. "
                "Found by following FROM_DEVICE from the flagged transaction to the profile and back to every card on it."
            )
        else:
            pattern_description = (
                "Just-under-$500 online burst: four purchases of $450–$499.99 within 40 minutes on a card with little "
                "prior online history, a shape shared by other cards in the same weeks. Found by the precomputed burst "
                "attribute and the card window."
            )
    sims = [c["id"] for c in ((facts.get("similar_prior_cases", {}) or {}).get("cases", []) or []) if str(c.get("id", "")).startswith("CC-")]
    return Scorecard(
        cms_p=cms_p,
        cal_p=round(cal_p, 3),
        adjustments=adjustments,
        p_engine=p,
        families_fraud=fam_f,
        families_legit=fam_l,
        flags=flags,
        chain=chain,
        episode_ids=ep,
        first_suspicious_txn_id=first,
        exposure_usd=exposure,
        connected_card_ids=connected,
        connected_device_profiles=profiles,
        pattern=pattern,
        pattern_description=pattern_description,
        verdict=verdict,
        similar_prior_cases=sims[:6],
    )


def post_evidence(sc: Scorecard, outcome: str) -> Scorecard:
    """outcome ∈ confirm | deny | no_reply | pass | fail | inconclusive | info (PLAN §4.5)."""
    sc2 = copy.deepcopy(sc)
    p = sc.p_engine
    if outcome == "confirm":
        p = min(p, 0.10)
        sc2.families_legit.add("customer")
        sc2.adjustments.append(("customer confirmed: ceiling 0.10", 0.0))
    elif outcome == "deny":
        second = bool(sc.families_fraud - {"customer"})
        p = _sigmoid(_logit(p) + 1.1)
        if second:
            p = max(p, 0.80)
        sc2.families_fraud.add("customer")
        sc2.adjustments.append(("customer denied", 1.1))
    elif outcome == "pass":
        p = _sigmoid(_logit(p) - 1.0)
        sc2.families_legit.add("customer")
        sc2.adjustments.append(("step-up passed", -1.0))
    elif outcome == "fail":
        p = _sigmoid(_logit(p) + 0.7)
        sc2.families_fraud.add("customer")
        sc2.adjustments.append(("step-up failed", 0.7))
    else:
        sc2.adjustments.append((f"{outcome}: unchanged", 0.0))
    if sc.flags.get("ring_hit"):
        p = max(p, 0.90)
    sc2.p_engine = round(min(max(p, 0.01), 0.99), 3)
    sc2.verdict = _verdict(sc2.p_engine)
    if outcome in ("no_reply", "inconclusive") and sc.verdict == "uncertain":
        sc2.verdict = "uncertain"
    if sc2.verdict == "legitimate":
        sc2.pattern, sc2.pattern_description = "none", ""
        sc2.episode_ids, sc2.first_suspicious_txn_id, sc2.exposure_usd = [], "", 0.0
        sc2.connected_card_ids, sc2.connected_device_profiles = [], []
    elif sc.verdict != sc2.verdict or sc2.verdict == "fraud":
        sc2.episode_ids = members(sc.chain, sc.episode_ids[0] if sc.episode_ids else "", sc2.verdict, sc.flags) if sc.chain else sc.episode_ids
        amt = {str(m.get("id")): abs(float(m.get("amt", 0) or 0)) for m in sc.chain}
        sc2.exposure_usd = round(sum(amt.get(i, 0.0) for i in sc2.episode_ids), 2) or sc.exposure_usd
        if sc2.pattern == "none":
            sc2.pattern = label(sc.chain, "", sc.flags)
    return sc2


# ---------------------------------------------------------------- engine/policy.py

_BLOCKS = {"BLOCK_CARD", "BLOCK_ALL_CARDS"}
_ORDER = [
    "CREATE_CASE",
    "VERIFY_WITH_CUSTOMER",
    "STEP_UP_AUTH",
    "WARN_CUSTOMER",
    "MONITOR_CARD",
    "MONITOR_CONNECTED_CARDS",
    "DECLINE_TRANSACTION",
    "ALLOW_TRANSACTION",
    "BLOCK_CARD",
    "BLOCK_ALL_CARDS",
    "GENERATE_REPORT",
    "FILE_REPORT",
    "ESCALATE_TO_ANALYST",
    "CLOSE_NO_FRAUD",
]


def route(action: str, exposure: float) -> str:
    if action == "BLOCK_CARD":
        return "L1" if exposure <= 2500 else "L2"
    if action == "DECLINE_TRANSACTION":
        return "L1"
    if action in ("BLOCK_ALL_CARDS", "FILE_REPORT"):
        return "L2"
    return "auto"


def sar_required(sc: Scorecard) -> tuple[bool, str]:
    f = sc.flags
    strong = f.get("ring_hit") or ("device" in sc.families_fraud and bool(sc.connected_device_profiles))
    if sc.verdict != "fraud":
        return False, f"3a not met: verdict {sc.verdict} (fraud not confirmed or strongly suspected); exposure ${sc.exposure_usd:,.2f}"
    reasons = []
    if sc.exposure_usd > 1000:
        reasons.append(f"exposure ${sc.exposure_usd:,.2f} > $1,000")
    if strong:
        reasons.append("shared device profile with other cards' fraud")
    if sc.pattern == "undocumented":
        reasons.append("undocumented / coordinated pattern (R9)")
    if reasons:
        return True, "3a: fraud strongly suspected and " + "; ".join(reasons)
    prof = sc.connected_device_profiles[0] if sc.connected_device_profiles else ""
    return False, (
        f"3a not met: exposure ${sc.exposure_usd:,.2f} <= $1,000; no strong shared device profile{(' (' + prof + ')') if prof else ''} "
        "with other confirmed-fraud cards; no other customer's fraud connected; pattern documented"
    )


def admissible(ctx, sc: Scorecard, stage: str) -> dict:
    p, v, f = sc.p_engine, sc.verdict, sc.flags
    fam = len(sc.families_fraud)
    required: list[str] = []
    forbidden: set[str] = set()
    cit: dict[str, list[str]] = {}
    exposure = sc.exposure_usd
    denial = f.get("denial")
    post = stage == "final"
    post_outcome = f.get("post_outcome", "")

    def req(a: str, *rules: str):
        if a not in required:
            required.append(a)
        cit.setdefault(a, []).extend(r for r in rules if r not in cit.get(a, []))

    if p >= 0.30 or denial or v == "uncertain":
        req("CREATE_CASE", "3a")
    if post and post_outcome == "confirm":
        req("CLOSE_NO_FRAUD", "R3")
        if ctx.trigger_type == "risk_score":
            req("ALLOW_TRANSACTION", "R3")
        forbidden |= _BLOCKS | {"FILE_REPORT", "DECLINE_TRANSACTION", "ESCALATE_TO_ANALYST"}
    elif v == "legitimate":
        req("CLOSE_NO_FRAUD", "R3" if post else "§6")
        if ctx.trigger_type == "risk_score":
            req("ALLOW_TRANSACTION", "§6")
        forbidden |= _BLOCKS | {"FILE_REPORT", "DECLINE_TRANSACTION", "ESCALATE_TO_ANALYST"}
    elif v == "fraud":
        if f.get("recurring_match") and not post:
            req("VERIFY_WITH_CUSTOMER", "R7")
            req("WARN_CUSTOMER", "R7")
            forbidden |= _BLOCKS
        else:
            req("BLOCK_CARD", "R2" if denial or post_outcome in ("deny", "fail") else "§3b")
            file_, _ = sar_required(sc)
            if file_:
                req("FILE_REPORT", "3a" + ("/R6" if f.get("ring_hit") else "") + ("/R9" if sc.pattern == "undocumented" else ""))
            if f.get("ring_hit") or sc.connected_card_ids:
                req("MONITOR_CONNECTED_CARDS", "R6")
            if sc.pattern == "undocumented":
                req("ESCALATE_TO_ANALYST", "R9")
            if f.get("card_testing_r5"):
                req("DECLINE_TRANSACTION", "R5")
                req("STEP_UP_AUTH", "R5")
            if p < 0.85 and not post and fam < 3 and not f.get("ring_hit"):
                req("STEP_UP_AUTH" if ctx.trigger_type != "risk_score" or f.get("new_device") else "VERIFY_WITH_CUSTOMER", "§3b")
    else:  # uncertain
        if post and post_outcome in ("no_reply", "inconclusive"):
            req("MONITOR_CARD", "R4")
            req("DECLINE_TRANSACTION", "R4")
            if exposure > 500 or f.get("conflict"):
                req("ESCALATE_TO_ANALYST", "R4/R8")
            forbidden |= _BLOCKS
        elif post and post_outcome in ("deny", "fail"):
            req("BLOCK_CARD", "R2")
            req("MONITOR_CARD", "R8")
            if exposure > 500 or f.get("conflict"):
                req("ESCALATE_TO_ANALYST", "R8")
        else:
            if f.get("recurring_match"):
                req("VERIFY_WITH_CUSTOMER", "R7")
                req("WARN_CUSTOMER", "R7")
            else:
                online = (sc.chain[-1].get("channel") if sc.chain else "") == "online"
                req("STEP_UP_AUTH" if online else "VERIFY_WITH_CUSTOMER", "R1" if (p < 0.70 and fam < 2) else "§5")
            req("MONITOR_CARD", "R8")
            if exposure > 500 or f.get("conflict") or denial:
                req("ESCALATE_TO_ANALYST", "R8")
            forbidden |= _BLOCKS | {"FILE_REPORT"}
    if p < 0.70 and fam < 2:
        forbidden |= _BLOCKS  # R1
    if not f.get("two_cards_confirmed"):
        forbidden.add("BLOCK_ALL_CARDS")  # R10
    allowed = [a for a in _ORDER if a not in forbidden and a not in required]
    if v != "legitimate":
        allowed = [a for a in allowed if a not in ("CLOSE_NO_FRAUD", "ALLOW_TRANSACTION")]
    routes = {a: route(a, exposure) for a in _ORDER}
    return {"required": required, "allowed": allowed, "forbidden": sorted(forbidden), "routes": routes, "citations": cit}


def check(actions: list[dict], ctx, sc: Scorecard, stage: str) -> list[str]:
    adm = admissible(ctx, sc, stage)
    names = [a["action"] for a in actions]
    v: list[str] = []
    for r in adm["required"]:
        if r not in names:
            v.append(f"missing required action {r} ({'/'.join(adm['citations'].get(r, []))})")
    for a in actions:
        if a["action"] in adm["forbidden"]:
            v.append(f"forbidden action {a['action']} at stage {stage}")
        if a.get("route") != adm["routes"].get(a["action"]):
            v.append(f"wrong route for {a['action']}: {a.get('route')} != {adm['routes'].get(a['action'])}")
        if "R1" in a.get("reason", "") and not (sc.p_engine < 0.70 and len(sc.families_fraud) < 2):
            v.append(f"R1 cited on {a['action']} but p={sc.p_engine} / families={len(sc.families_fraud)}")
    if "FILE_REPORT" in names and "CREATE_CASE" not in names:
        v.append("FILE_REPORT without CREATE_CASE (3a)")
    return v


def order(actions: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for a in sorted(actions, key=lambda a: _ORDER.index(a["action"]) if a["action"] in _ORDER else 99):
        if a["action"] not in seen:
            seen.add(a["action"])
            out.append(a)
    return out


# ---------------------------------------------------------------- engine/voi.py


def _action_set(ctx, sc: Scorecard, outcome: str) -> list[str]:
    sc2 = post_evidence(sc, outcome)
    sc2.flags["post_outcome"] = outcome
    return sorted(admissible(ctx, sc2, "final")["required"])


def should_ask(ctx, sc: Scorecard, request_type: str) -> tuple[bool, dict]:
    if request_type == "customer_validation":
        branches = {"confirm": _action_set(ctx, sc, "confirm"), "deny": _action_set(ctx, sc, "deny"), "no_reply": _action_set(ctx, sc, "no_reply")}
    elif request_type == "step_up_auth":
        branches = {"pass": _action_set(ctx, sc, "pass"), "fail": _action_set(ctx, sc, "fail"), "no_reply": _action_set(ctx, sc, "no_reply")}
    else:
        branches = {"info": _action_set(ctx, sc, "info")}
    distinct = {tuple(b) for b in branches.values()}
    settled = (sc.p_engine >= 0.85 or sc.p_engine <= 0.15) and len(sc.families_fraud | sc.families_legit) >= 2
    ask = len(distinct) > 1 and not settled and not sc.flags.get("ring_hit")
    return ask, branches


# ---------------------------------------------------------------- engine/simulator.py

_TEMPLATES = {
    "travel": "Customer confirmed the purchase: they were travelling to billing region {addr1} at the time.",
    "phone": "Customer confirmed the purchase from a new phone; the device is theirs.",
    "amount": "Customer confirmed an unusual amount consistent with their stated intent (a planned large purchase).",
}


def reply(request_type: str, ctx, sc: Scorecard) -> dict:
    F, L = len(sc.families_fraud), len(sc.families_legit)
    prefix = "ASSUMED (simulated): "
    flagged = next((m for m in sc.chain if str(m.get("id")) == str(ctx.flagged_txn_id)), sc.chain[-1] if sc.chain else {})
    if request_type == "customer_validation":
        if L >= 2 and F <= 1:
            if flagged.get("channel") == "in_person":
                text, cf = _TEMPLATES["travel"].format(addr1=flagged.get("addr1", "")), "had the customer denied, final would be BLOCK_CARD (L1) + CREATE_CASE under R2"
            elif sc.flags.get("new_device"):
                text, cf = _TEMPLATES["phone"], "had the customer denied, final would be BLOCK_CARD (L1) + CREATE_CASE under R2"
            else:
                text, cf = _TEMPLATES["amount"], "had the customer denied, final would be BLOCK_CARD (L1) + CREATE_CASE under R2"
            return {"assumed_response": prefix + text, "outcome": "confirm", "counterfactual": cf}
        if F >= 2:
            return {
                "assumed_response": prefix + "Customer states they did not make these purchases and still has the card.",
                "outcome": "deny",
                "counterfactual": "had the customer confirmed, final would be CLOSE_NO_FRAUD under R3",
            }
        return {
            "assumed_response": prefix + "No reply from the customer within 24 hours.",
            "outcome": "no_reply",
            "counterfactual": "a confirmation would close the case under R3; a denial would trigger BLOCK_CARD + CREATE_CASE under R2",
        }
    if request_type == "step_up_auth":
        if sc.flags.get("device_known") and F <= 1:
            return {
                "assumed_response": prefix + "Step-up authentication passed: the cardholder approved the one-time passcode on a device known to the account.",
                "outcome": "pass",
                "counterfactual": "a failed step-up would add BLOCK_CARD (L1) under R2",
            }
        if F >= 2:
            return {
                "assumed_response": prefix + "Step-up authentication failed: the passcode was not confirmed and the device is unknown to this account.",
                "outcome": "fail",
                "counterfactual": "a passed step-up would lower the probability and remove the block",
            }
        return {
            "assumed_response": prefix + "Step-up authentication not completed within the window.",
            "outcome": "inconclusive",
            "counterfactual": "pass → CLOSE_NO_FRAUD path; fail → BLOCK_CARD (L1) under R2",
        }
    n = len(sc.connected_card_ids)
    return {
        "assumed_response": prefix + f"Analyst note: {n} other cardholders share the named element in this window; prior closed cases on it: {', '.join(sc.similar_prior_cases[:4]) or 'none'}.",
        "outcome": "info",
        "counterfactual": "informational; the action set does not depend on the reply",
    }


# ---------------------------------------------------------------- engine/status.py


def derive(verdict: str, final_actions: list[dict], pending: bool) -> str:
    names = {a["action"] for a in final_actions}
    if pending:
        return "open"
    if "ESCALATE_TO_ANALYST" in names:
        return "escalated"
    if verdict == "fraud":
        return "closed_fraud"
    if verdict == "legitimate":
        return "closed_legitimate"
    return "escalated"


# ---------------------------------------------------------------- engine/stop.py


def stop_reason(sc_pre: Scorecard, sc_post: Scorecard | None, asked: bool, voi_zero: bool) -> str:
    sc = sc_post or sc_pre
    fams = len(sc.families_fraud | sc.families_legit)
    if (sc.p_engine >= 0.85 or sc.p_engine <= 0.15) and fams >= 2:
        return (
            f"Policy §6 test 1: fraud probability {sc.p_engine:.2f} is {'at or above 0.85' if sc.p_engine >= 0.85 else 'at or below 0.15'} "
            f"with {fams} independent evidence families ({', '.join(sorted(sc.families_fraud | sc.families_legit))})."
        )
    if asked and sc_post is not None and sc_post.verdict != "uncertain":
        return "Policy §6 test 2: the verification response settled the question."
    if asked and sc_post is not None:
        return (
            "Policy §6 test 3: the assumed reply was inconclusive and no further admissible request would change the action set; "
            "the case is escalated to an analyst under R8 rather than investigated further."
        )
    if voi_zero:
        return "Policy §6 test 3: no admissible evidence request could change the recommended action set, so further steps would not change the decision."
    return "Policy §6 test 3: further steps are unlikely to change the decision."
