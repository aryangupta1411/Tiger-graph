"""Deterministic scorecard — PLAN §4.5 (calibrated probability, evidence families, bands) plus the
pattern / episode / exposure / connected-entity derivations of §4.4.

compute(ctx, facts, evidence) -> Scorecard
post_evidence(sc, outcome)    -> Scorecard      outcome ∈ confirm | deny | no_reply | pass | fail | inconclusive | info

The engine reads ONLY the Facts dict (the §A query contracts) — never DuckDB or the graph directly —
so the same code runs on the live MCP backend and on engine/facts_duckdb.py.

Evidence families (§4.5): history (card history & baseline), device (device / ring / shared origin),
memory (prior cases, similar cases, scorer), customer (customer / analyst reply). Every evidence item
carries a direction and a weight; a family is "for fraud" when its net weight is > 0 and "for
legitimacy" when < 0. Documents ground rules and are never evidence of fraud.
"""

from __future__ import annotations

import copy
import json
import math
from datetime import datetime

import numpy as np

from engine import config, pattern_rule
from engine import episode as episode_rule
from engine.policy import params as policy_params
from engine.types import CaseContext, Evidence, Facts, Scorecard

TS_FMT = "%Y-%m-%d %H:%M:%S"
_CAL: dict | None = None


def _dt(s: str) -> datetime:
    return datetime.strptime(s, TS_FMT)


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _expit(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def calibrate(cms_p: float) -> float:
    """Isotonic calibration of the ETL scorer: data/models/isotonic.json ({"x": [...], "y": [...]} fitted by
    etl/cms_train.py on October out-of-fold predictions; applied with numpy.interp) — decision D7."""
    global _CAL
    if _CAL is None:
        raw = json.load(open(config.CALIBRATION_JSON))
        _CAL = raw.get("calibration", raw)  # {"x", "y"} at the top level (ETL) or under "calibration" (legacy)
    if cms_p is None or cms_p < 0:
        return 0.05  # scorer missing: use the Jul-Oct base rate for a flagged transaction
    return float(np.interp(cms_p, _CAL["x"], _CAL["y"]))


def _q(name: str, **params) -> str:
    return f"query:{name}(" + ", ".join(f"{k}={v}" for k, v in params.items()) + ")"


# ----------------------------------------------------------------------------- detectors
def _ring(ctx: CaseContext, facts: Facts, txn: dict) -> dict:
    """Strict ring test for the 0.90 floor: a strong profile that is an anonymous-proxy wave
    (n_proxy > 0 and the card's own ring transactions are anonymous-proxy) with either >= 2
    confirmed-fraud cases or >= 5 cards in a 30-day window, and the flagged transaction or a chain
    member on it. Card.ring_id alone (schema rule: strong AND (>= 2 fraud cases OR proxy share >= 0.9))
    is broader (1,149 cards) and only marks the candidate profile."""
    rp = facts.get("ring_profile", {}) or {}
    dev = rp.get("device") or {}
    ring_id = rp.get("ring_id", "")
    mine = rp.get("card_txns_on_ring", []) or []
    out = {
        "ring_hit": False,
        "ring_id": ring_id,
        "ring_txn_ids": [str(m["id"]) for m in mine],
        "ring_rows": mine,
        "wave_cards": rp.get("wave_cards", []) or [],
        "pre_open_cards": rp.get("pre_open_cards", []) or [],
        "ring_cases": [c["id"] for c in rp.get("closed_cases", []) or []],
    }
    if not ring_id or not dev or not dev.get("is_strong"):
        return out
    anon = txn["device_id"] == ring_id and txn["proxy"] == config.ANON_PROXY
    if not anon:
        for w in facts.get("card_window", {}).get("txns", []):
            if w["device_id"] == ring_id and w["proxy"] == config.ANON_PROXY and str(w["id"]) in out["ring_txn_ids"]:
                anon = True
    wave = int(dev.get("n_fraud_cases", 0)) >= 2 or int(dev.get("n_cards_30d", 0)) >= 5
    recent = any(abs((_dt(m["ts"]) - _dt(txn["ts"])).days) <= 30 for m in mine)
    out["ring_hit"] = bool(anon and wave and int(dev.get("n_proxy", 0)) > 0 and recent)
    return out


def _burst(facts: Facts, txn: dict) -> dict:
    ub = facts.get("under_threshold_burst", {}) or {}
    for b in ub.get("bursts", []) or []:
        ids = [str(i) for i in b["ids"]]
        if str(txn["id"]) in ids or abs((_dt(b["end_ts"]) - _dt(txn["ts"])).total_seconds()) <= 48 * 3600:
            return {"burst_hit": True, "burst_txn_ids": ids, "burst": b, "burst_lookalikes": ub.get("lookalike_cards", []) or []}
    return {"burst_hit": False, "burst_txn_ids": [], "burst": {}, "burst_lookalikes": ub.get("lookalike_cards", []) or []}


def _r5(facts: Facts, txn: dict) -> dict:
    ct = dict(facts.get("card_testing_check", {}) or {})
    run = ct.get("run") or {}
    if isinstance(run, list):  # live GroupByAccum prints run as a one-element list; the harness unwraps it (M7), be defensive
        run = run[0] if run else {}
    ct["run"] = run
    if not run or not run.get("ids"):
        return {"r5_run": False, "card_testing_run_ids": [], "r5_cleared_over_big": False, "r5": ct}
    ids = [str(i) for i in run["ids"]]
    feeds = str(txn["id"]) in ids or 0 <= (_dt(txn["ts"]) - _dt(run["end_ts"])).total_seconds() <= config.CARD_TESTING_LOOKAHEAD_H * 3600
    return {"r5_run": bool(feeds), "card_testing_run_ids": ids if feeds else [], "r5_cleared_over_big": bool(ct.get("cleared_over_big")) and feeds, "r5": ct}


def _r7(facts: Facts, txn: dict, chain: list[dict], P: dict) -> dict:
    g = P["r7_gate"]
    rc = facts.get("recurring_charge_check", {}) or {}
    place = txn["addr1"] if txn["channel"] == "in_person" else txn["p_email"]
    grp = next((x for x in rc.get("groups", []) or [] if str(x["place"]) == str(place)), None)
    neighbour_hi = any(str(m["id"]) != str(txn["id"]) and float(m.get("cms_p", -1)) >= g["max_neighbour_cms"] and abs((_dt(m["ts"]) - _dt(txn["ts"])).total_seconds()) <= 48 * 3600 for m in chain)
    fires = bool(
        grp
        and grp["n"] >= g["min_n"]
        and g["median_gap_days"][0] <= grp["median_gap_days"] <= g["median_gap_days"][1]
        and 0 <= grp["gap_cv"] <= g["max_gap_cv"]
        and float(txn["cms_p"]) < g["max_cms"]
        and not neighbour_hi
    )
    return {"recurring_match": fires, "recurring_group": grp, "recurring_total_n": int(rc.get("total_n", 0) or 0), "recurring_neighbour_hi": neighbour_hi, "recurring_place": place}


def _cleared_template_matches(case: dict, txn: dict, rh: dict) -> bool:
    """PLAN §4.5: the -0.5 cleared precedent applies to a prior cleared alert *of the same template*.
    cleared_travel   <-> a billing-region alert (the flagged transaction is outside the card's modal region)
    cleared_new_phone <-> an online purchase from a device marked New for the account
    cleared_amount   <-> an amount above the card's prior maximum
    A row without template_id (a backend that does not print the additive key) matches, i.e. the pre-fix behaviour."""
    tpl = case.get("template_id")
    if tpl is None:
        return True
    if tpl == "cleared_travel":
        return bool(txn.get("addr1")) and rh.get("hint", "new") != "home"
    if tpl == "cleared_new_phone":
        return txn.get("channel") == "online" and txn.get("device_new") == "New"
    if tpl == "cleared_amount":
        pmax = float(txn.get("prior_max_amt", -1))
        return pmax > 0 and float(txn["amt"]) > pmax
    return False


# ----------------------------------------------------------------------------- the scorecard
def compute(ctx: CaseContext, facts: Facts, evidence: list[Evidence]) -> Scorecard:
    P = policy_params()["engine"]
    cc_ = facts["case_context"]
    txn, card, dev = cc_["txn"], cc_["card"], cc_.get("device") or {}
    prof = facts.get("card_profile", {}) or {}
    pcard = prof.get("card", {}) or {}
    chain = list((facts.get("episode_candidates", {}) or {}).get("chain", []) or [])
    rh = facts.get("region_history", {}) or {}
    modal = rh.get("modal_region") or card.get("modal_region", "")
    win = (facts.get("card_window", {}) or {}).get("txns", []) or []
    dn = facts.get("device_neighbors", {}) or {}
    dh = facts.get("device_history", {}) or {}
    pc = (facts.get("prior_cases_for_customer", {}) or {}).get("closed_cases", []) or []
    sim = (facts.get("similar_prior_cases", {}) or {}).get("cases", []) or []
    sos = facts.get("shared_origin_scan", {}) or {}
    t_ts = _dt(txn["ts"])
    as_of = _dt(ctx.opened_at)
    amt = float(txn["amt"])
    cms_p = float(txn["cms_p"])
    cal_p = calibrate(cms_p)
    denial = ctx.trigger_type == "customer_report"
    analyst = ctx.trigger_type == "analyst_request"
    risk_alert = ctx.trigger_type == "risk_score"
    flags: dict = {
        "denial": denial,
        "analyst_request": analyst,
        "risk_alert": risk_alert,
        "trigger_type": ctx.trigger_type,
        "flagged_ts": txn["ts"],
        "flagged_txn_id": str(txn["id"]),
        "flagged_amt": amt,
        "flagged_addr1": txn["addr1"],
        "flagged_device_id": txn["device_id"],
        "flagged_device_new": txn["device_new"],
        # cards on the flagged profile in device_neighbors' window (30 days before the flag up to as_of; the card itself
        # included), the count the evidence cites; the profile's all-time n_cards reaches past opened_at (D-MISC HHG-019)
        "flagged_device_cards": len(dn.get("cards", []) or []),
        "flagged_device_strong": bool(dev.get("is_strong")),
        "channel": txn["channel"],
        "modal_region": modal,
        "card_n_txns": int(card.get("n_txns", 0) or 0),
        "two_cards_confirmed_fraud": False,
        "outcome": "",
        "asked": False,
        "pending": False,
    }
    flags.update(_ring(ctx, facts, txn))
    flags.update(_burst(facts, txn))
    flags.update(_r5(facts, txn))
    flags.update(_r7(facts, txn, chain, P))
    flags["scorer_unreliable"] = int(card.get("n_txns", 0)) > config.SCORER_UNRELIABLE_CARD_TXNS or int(txn.get("card_seq", 0)) < config.SCORER_UNRELIABLE_MIN_SEQ
    led: list[Evidence] = list(evidence)
    cid = ctx.card_id

    # ---------------------------------------------------------------- history family
    seq = int(txn.get("card_seq", 0))
    pmax, pmed = float(txn.get("prior_max_amt", -1)), float(txn.get("prior_med_amt", -1))
    if seq >= 10 and pmax > 0:
        ratio = amt / pmax
        if ratio > 2.0:
            led.append(
                Evidence(
                    f"Amount ${amt:,.2f} is {ratio:.1f}x the card's prior maximum (${pmax:,.2f}); prior median ${pmed:,.2f} over {seq - 1} transactions",
                    "graph",
                    _q("case_context", txn=txn["id"]),
                    [txn["id"], cid],
                    "history",
                    "fraud",
                    1.0,
                )
            )
        elif ratio > 1.0:
            led.append(
                Evidence(
                    f"Amount ${amt:,.2f} exceeds the card's prior maximum (${pmax:,.2f}); prior median ${pmed:,.2f}",
                    "graph",
                    _q("case_context", txn=txn["id"]),
                    [txn["id"], cid],
                    "history",
                    "fraud",
                    0.5,
                )
            )
        elif pmed > 0 and amt <= max(3 * pmed, 0.0) and amt <= pmax:
            led.append(
                Evidence(
                    f"Amount ${amt:,.2f} sits inside the card's baseline (prior median ${pmed:,.2f}, prior maximum ${pmax:,.2f} over {seq - 1} transactions)",
                    "graph",
                    _q("case_context", txn=txn["id"]),
                    [txn["id"], cid],
                    "history",
                    "legit",
                    0.7,
                )
            )
    # dormancy: gap to the previous transaction from card_window
    wrow = next((w for w in win if str(w["id"]) == str(txn["id"])), None)
    gap_s = int(wrow["gap_seconds"]) if wrow and wrow.get("gap_seconds") is not None else -1
    if seq >= 10 and gap_s > 14 * 86400:
        led.append(Evidence(f"Card was dormant for {gap_s // 86400} days before the flagged transaction", "graph", _q("card_window", card=cid, hours=72), [txn["id"], cid], "history", "fraud", 0.7))
        flags["dormant_days"] = gap_s // 86400
    # region (in-person)
    reg = rh.get("region") or {}
    n_regions = int(rh.get("n_regions", 0) or 0)
    roaming = n_regions >= 20
    flags["roaming_card"] = roaming
    if txn["channel"] == "in_person" and txn["addr1"]:
        hint = rh.get("hint", "new")
        ha = rh.get("home_activity_48h") or {}
        ref = _q("region_history", card=cid, addr1=txn["addr1"])
        if hint == "home":
            led.append(
                Evidence(
                    f"Billing region {txn['addr1']} is the card's modal in-person region ({reg.get('prior_n', 0)} prior purchases on {reg.get('prior_days', 0)} days)",
                    "graph",
                    ref,
                    [txn["id"], cid],
                    "history",
                    "legit",
                    0.7,
                )
            )
        elif hint == "known":
            led.append(
                Evidence(
                    f"Billing region {txn['addr1']} is known to the card: {reg.get('prior_n', 0)} prior purchases on {reg.get('prior_days', 0)} days since {reg.get('first_ts', '')[:10]} (modal region {modal}, {n_regions} regions used)",
                    "graph",
                    ref,
                    [txn["id"], cid],
                    "history",
                    "legit",
                    0.7,
                )
            )
        elif roaming:
            led.append(
                Evidence(
                    f"Billing region {txn['addr1']} is {hint} for the card, but the card roams across {n_regions} regions, so region novelty carries no weight",
                    "graph",
                    ref,
                    [txn["id"], cid],
                    "history",
                    "neutral",
                    0.0,
                )
            )
        else:
            w = 0.8 if hint == "new" else 0.4
            led.append(
                Evidence(
                    f"Billing region {txn['addr1']} is {hint} for the card ({reg.get('prior_n', 0)} prior purchases; modal region {modal}, {n_regions} regions used)",
                    "graph",
                    ref,
                    [txn["id"], cid],
                    "history",
                    "fraud",
                    w,
                )
            )
            if int(ha.get("n_home", 0)) > 0:
                led.append(
                    Evidence(
                        f"Home-region activity continued within 48 h ({ha.get('n_home')} in {modal}) while the card was used in {txn['addr1']}", "graph", ref, [txn["id"], cid], "history", "fraud", 0.4
                    )
                )
    # online region (weak: billing region on an online purchase)
    if txn["channel"] == "online" and txn["addr1"] and seq >= 20 and not roaming:
        hint = rh.get("hint", "new")
        ref = _q("region_history", card=cid, addr1=txn["addr1"])
        if hint in ("home", "known"):
            led.append(
                Evidence(
                    f"Billing region {txn['addr1']} on the online purchase is the card's {'modal' if hint == 'home' else 'known'} region ({reg.get('prior_n', 0)} prior purchases on {reg.get('prior_days', 0)} days)",
                    "graph",
                    ref,
                    [txn["id"], cid],
                    "history",
                    "legit",
                    0.4,
                )
            )
        elif hint == "new":
            led.append(
                Evidence(
                    f"Billing region {txn['addr1']} on the online purchase has never been used by this card ({n_regions} regions used before)", "graph", ref, [txn["id"], cid], "history", "fraud", 0.4
                )
            )
    # online velocity / novelty
    if txn["channel"] == "online":
        n48 = sum(1 for w in win if w["channel"] == "online" and 0 <= (t_ts - _dt(w["ts"])).total_seconds() <= 48 * 3600 and str(w["id"]) != str(txn["id"]))
        n_online, n_txns = int(pcard.get("n_online", 0) or 0), int(pcard.get("n_txns", 1) or 1)
        if n48 >= 2 and n_online <= 0.1 * n_txns and seq >= 10:
            led.append(
                Evidence(
                    f"{n48 + 1} online purchases within 48 h on a card that is {100 - 100 * n_online / n_txns:.0f}% in-person",
                    "graph",
                    _q("card_window", card=cid, hours=72),
                    [txn["id"], cid],
                    "history",
                    "fraud",
                    0.5,
                )
            )
        if seq >= 20 and int(txn.get("prior_pcd", 0)) == 0:
            led.append(Evidence(f"First use of product code {txn['product_cd']} on this card", "graph", _q("case_context", txn=txn["id"]), [txn["id"]], "history", "fraud", 0.3))
        if seq >= 20 and txn["p_email"] and int(txn.get("prior_pem", 0)) == 0:
            led.append(Evidence(f"Purchaser email domain {txn['p_email']} never seen on this card before", "graph", _q("case_context", txn=txn["id"]), [txn["id"]], "history", "fraud", 0.3))
        elif txn["p_email"] and int(txn.get("prior_pem", 0)) >= 3:
            led.append(
                Evidence(f"Purchaser email domain {txn['p_email']} used {txn['prior_pem']} times before on this card", "graph", _q("case_context", txn=txn["id"]), [txn["id"]], "history", "legit", 0.3)
            )
        if not txn["has_identity"]:
            led.append(Evidence("No identity record for this online transaction: the device is unknown, not new", "graph", _q("case_context", txn=txn["id"]), [txn["id"]], "history", "neutral", 0.0))
    # burst / R5 / R7 / recurrence
    if flags["burst_hit"]:
        b = flags["burst"]
        led.append(
            Evidence(
                f"{len(b['ids'])} online purchases of ${min(b['amts']):,.2f}-${max(b['amts']):,.2f} (all just under $500) within {int((_dt(b['end_ts']) - _dt(b['start_ts'])).total_seconds() // 60)} minutes: the under-threshold burst shape; the card had {max(seq - len(b['ids']), 0)} prior transactions",
                "graph",
                _q("under_threshold_burst", card=cid),
                [str(i) for i in b["ids"]] + [cid],
                "history",
                "fraud",
                1.5,
            )
        )
    if flags["r5_run"]:
        r = flags["r5"]["run"]
        led.append(
            Evidence(
                f"{len(r['ids'])} online authorisations under ${config.CARD_TESTING_SMALL:.0f} within {config.CARD_TESTING_WINDOW_MIN} minutes ({r['start_ts']} to {r['end_ts']}) followed by a larger purchase: card-testing sequence (R5)",
                "graph",
                _q("card_testing_check", card=cid),
                [str(i) for i in r["ids"]] + [txn["id"]],
                "history",
                "fraud",
                1.2,
            )
        )
    if flags["recurring_match"]:
        g = flags["recurring_group"]
        led.append(
            Evidence(
                f"Disputed charge matches the card's own recurring pattern: {g['n']} prior purchases of ${amt:,.2f} +-1% under product code {txn['product_cd']} at {flags['recurring_place']} every {g['median_gap_days']:.0f} days (inferred from product code, amount, place and cadence; the data has no merchant field)",
                "graph",
                _q("recurring_charge_check", txn=txn["id"]),
                [txn["id"], cid],
                "history",
                "legit",
                1.5,
            )
        )
    elif flags["recurring_total_n"] >= 5:
        g = flags["recurring_group"] or {}
        led.append(
            Evidence(
                f"${amt:,.2f} +-1% under product code {txn['product_cd']} is a recurring price point on this card: {flags['recurring_total_n']} prior purchases"
                + (f", {g['n']} of them at {flags['recurring_place']} (median gap {g['median_gap_days']:.1f} days, gap CV {g['gap_cv']:.2f}; R7's monthly-cadence test fails)" if g else ""),
                "graph",
                _q("recurring_charge_check", txn=txn["id"]),
                [txn["id"], cid],
                "history",
                "legit",
                0.6,
            )
        )
    # neighbours scored by the case-memory scorer inside the chain
    hi_nb = [m for m in chain if str(m["id"]) != str(txn["id"]) and float(m.get("cms_p", -1)) >= 0.5 and abs((_dt(m["ts"]) - t_ts).total_seconds()) <= 48 * 3600]
    if hi_nb:
        m = max(hi_nb, key=lambda x: float(x["cms_p"]))
        led.append(
            Evidence(
                f"A second transaction in the same 48 h window ({m['id']}, ${float(m['amt']):,.2f}, {m['channel']}"
                + (f", region {m['addr1']}" if m.get("addr1") else "")
                + f") scores {float(m['cms_p']):.2f} on the case-memory scorer",
                "graph",
                _q("episode_candidates", txn=txn["id"]),
                [str(x["id"]) for x in hi_nb] + [txn["id"]],
                "history",
                "fraud",
                0.6,
            )
        )
    # rulebook chain (conflict override candidates)
    raw48 = [m for m in chain if abs((_dt(m["ts"]) - t_ts).total_seconds()) <= 48 * 3600]
    in_person = [m for m in raw48 if m["channel"] == "in_person"]
    online = [m for m in raw48 if m["channel"] == "online"]
    med_all = float(pcard.get("median_amt", 0) or 0)
    max_ip_asof = float(pcard.get("max_in_person_amt", 0) or 0)
    ip_max = max((float(m["amt"]) for m in in_person), default=0.0)
    # clause A: a mixed-channel spree whose in-person members hold the card's in-person record and dwarf its median
    spree = bool(in_person and online and med_all > 0 and ip_max > 3 * med_all and ip_max >= max_ip_asof - 0.01 and sum(float(m["amt"]) for m in raw48) > P["conflict_min_exposure"])
    # clause B: a New device from a never-seen region; the online New-device activity itself must exceed the threshold
    new_dev_new_region = bool(
        txn["channel"] == "online" and txn["device_new"] == "New" and int(txn.get("prior_on_dev", 0)) == 0 and txn["addr1"] and int(txn.get("prior_in_region", 0)) == 0 and seq >= 10
    )
    new_dev_sum = sum(float(m["amt"]) for m in online if m.get("device_new") == "New")
    flags["rulebook_chain_ids"] = [str(m["id"]) for m in raw48]
    flags["rulebook_chain_sum"] = round(sum(float(m["amt"]) for m in raw48), 2)
    if spree:
        led.append(
            Evidence(
                f"Mixed-channel chain of {len(raw48)} transactions worth ${flags['rulebook_chain_sum']:,.2f} inside 48 h: in-person purchases up to ${max(float(m['amt']) for m in in_person):,.2f} against a card median of ${med_all:,.2f}, then online activity",
                "graph",
                _q("episode_candidates", txn=txn["id"]),
                [str(m["id"]) for m in raw48],
                "history",
                "fraud",
                0.8,
            )
        )
    if new_dev_new_region:
        led.append(
            Evidence(
                f"Online purchase of ${amt:,.2f} from a device marked New for this account in billing region {txn['addr1']} the card has never used",
                "graph",
                _q("case_context", txn=txn["id"]),
                [txn["id"], cid],
                "history",
                "fraud",
                0.8,
            )
        )
    flags["conflict_chain"] = bool(
        cms_p < P["conflict_cms"] and ((spree and flags["rulebook_chain_sum"] > P["conflict_min_exposure"]) or (new_dev_new_region and new_dev_sum > P["conflict_min_exposure"]))
    )
    # The mixed-channel spree that triggers the conflict override is the candidate episode itself (PLAN §4.3 B / §4.8: a failed
    # step-up on HHG-015 "confirms the mixed-channel episode"), so its members are pattern members of the episode — like a
    # burst or an R5 run — instead of being dropped by the uncertain-band cms >= 0.5 filter. Switchable in the YAML.
    flags["conflict_chain_ids"] = [str(m["id"]) for m in raw48] if (flags["conflict_chain"] and spree and P.get("conflict_chain_joins_episode", True)) else []

    # ---------------------------------------------------------------- device family
    dev_id = txn["device_id"]
    if flags["ring_hit"]:
        d = (facts.get("ring_profile") or {}).get("device") or {}
        led.append(
            Evidence(
                f"Device profile `{flags['ring_id']}` behind an anonymous proxy is a strong profile shared by {d.get('n_cards_alltime')} cards ({d.get('n_cards_30d')} in one 30-day window, {len(flags['pre_open_cards'])} other cards active before this case opened, {len(flags['wave_cards'])} in the current wave) with {d.get('n_fraud_cases')} prior confirmed-fraud cases",
                "graph",
                _q("ring_profile", card=cid),
                [flags["ring_id"], cid] + list(flags["pre_open_cards"]),
                "device",
                "fraud",
                2.0,
            )
        )
    shared_fraud_cards: list[str] = []
    if dev_id and dev.get("is_strong"):
        in_win = {c["card_id"] for c in dn.get("cards", []) or [] if c["card_id"] != cid}
        shared_fraud_cards = sorted({c["card_id"] for c in dn.get("closed_cases", []) or [] if c["card_id"] in in_win and c["outcome"] == "confirmed_fraud"})
        if len(shared_fraud_cards) >= 2 and not flags["ring_hit"]:
            led.append(
                Evidence(
                    f"Strong device profile `{dev_id}` ({dev.get('n_cards_alltime')} cards all-time) carries confirmed fraud on {len(shared_fraud_cards)} other cards within +-30 days",
                    "graph",
                    _q("device_neighbors", device=dev_id),
                    [dev_id] + shared_fraud_cards,
                    "device",
                    "fraud",
                    1.5,
                )
            )
    flags["shared_fraud_cards"] = shared_fraud_cards
    flags["shared_device_fraud"] = len(shared_fraud_cards) >= 2 or flags["ring_hit"]
    prior_n = int(dh.get("prior_n", 0) or 0)
    flags["device_known"] = bool(dev_id) and (prior_n >= 3 or (txn["device_new"] == "Found" and prior_n >= 1))
    if dev_id and not flags["ring_hit"]:
        n_cards = int(dev.get("n_cards_alltime", 0) or 0)
        if flags["device_known"]:
            led.append(
                Evidence(
                    f"Device profile `{dev_id}` is known to this card: {prior_n} prior uses since {dh.get('first_ts', '')[:10]} (id_15 values seen: {', '.join(sorted(dh.get('device_new_values', []) or [])) or 'n/a'})",
                    "graph",
                    _q("device_history", card=cid, device=dev_id),
                    [dev_id, cid],
                    "device",
                    "legit",
                    0.6,
                )
            )
        elif txn["device_new"] == "New":
            note = f"Device profile `{dev_id}` is marked New for this account"
            if n_cards > config.STRONG_MAX_CARDS_ALLTIME:
                note += f"; the profile is generic ({n_cards} cards use it) and is not a shared-origin signal"
            note += "; a New device is not by itself a fraud signal in this history (cleared 'new phone' alerts are New-device ones)"
            if txn["proxy"] in (config.ANON_PROXY, "IP_PROXY:HIDDEN"):
                led.append(Evidence(note + f", but it sits behind {txn['proxy']}", "graph", _q("case_context", txn=txn["id"]), [dev_id, txn["id"]], "device", "fraud", 0.5))
            else:
                led.append(Evidence(note, "graph", _q("case_context", txn=txn["id"]), [dev_id, txn["id"]], "device", "neutral", 0.0))
        elif txn["proxy"] == config.ANON_PROXY:
            led.append(Evidence(f"Transaction routed through {txn['proxy']}", "graph", _q("case_context", txn=txn["id"]), [txn["id"]], "device", "fraud", 0.4))
    # R6 recipient-email clause: only a rare recipient domain (<= 40 other cards in 30 d) shared with >= 2 other cards' fraud
    rem_fraud = [e for e in sos.get("recipient_emails", []) or [] if int(e.get("n_fraud_cases", 0)) >= 2 and 0 < int(e.get("n_cards_30d", 0)) <= 40]
    flags["shared_recipient_email"] = [e["domain"] for e in rem_fraud]
    if rem_fraud:
        e = rem_fraud[0]
        led.append(
            Evidence(
                f"Recipient email domain {e['domain']} on this card's recent online purchases appears in {e['n_fraud_cases']} other cards' confirmed-fraud cases within 30 days",
                "graph",
                _q("shared_origin_scan", card=cid),
                [e["domain"], cid],
                "device",
                "fraud",
                1.0,
            )
        )

    # ---------------------------------------------------------------- memory family
    sc_note = f"Case-memory scorer (LightGBM over the bank's 5,565 closed cases; October holdout AUC 0.956) puts the flagged transaction at {cms_p:.2f} (calibrated {cal_p:.2f}); the real-time risk score was {txn['risk_score']:.2f}"
    if flags["scorer_unreliable"]:
        sc_note += f"; the scorer is unreliable here (card has {card.get('n_txns')} transactions, position {seq})"
    if cal_p >= 0.5 and not flags["scorer_unreliable"]:
        led.append(Evidence(sc_note, "graph", _q("case_context", txn=txn["id"]), [txn["id"]], "memory", "fraud", 1.5 if cal_p >= 0.85 else 1.0))
    elif cal_p <= 0.10:
        led.append(Evidence(sc_note, "graph", _q("case_context", txn=txn["id"]), [txn["id"]], "memory", "legit", 0.3 if flags["scorer_unreliable"] else 0.8))
    else:
        led.append(Evidence(sc_note, "graph", _q("case_context", txn=txn["id"]), [txn["id"]], "memory", "neutral", 0.0))
    same_card_fraud = [c for c in pc if c["card_id"] == cid and c["outcome"] == "confirmed_fraud"]
    same_card_cleared = [c for c in pc if c["card_id"] == cid and c["outcome"] == "cleared"]
    other_card_fraud = [c for c in pc if c["card_id"] != cid and c["outcome"] == "confirmed_fraud"]
    fam_online = {"card_testing", "card_not_present_fraud", "card_not_present_new_device"}
    # PLAN §4.5: "recent card-testing / CNP case on the card +0.5" — online-family cases only. Prior out-of-region /
    # account-takeover cases are weaker memory (many sit on roaming cards whose alerts are later cleared as travel).
    recent = [c for c in same_card_fraud if (as_of - _dt(c["opened_at"])).days <= P["recent_case_days"] and c["pattern"] in fam_online]
    flags["recent_same_channel_case"] = bool(recent)
    prior_ato = [c for c in same_card_fraud if c["pattern"] == "account_takeover"]
    flags["prior_fraud_case_ids"] = [c["id"] for c in same_card_fraud]
    flags["prior_cleared_case_ids"] = [c["id"] for c in same_card_cleared]
    if same_card_fraud:
        pats = sorted({c["pattern"] for c in same_card_fraud})
        led.append(
            Evidence(
                f"{len(same_card_fraud)} prior confirmed-fraud case(s) on this card ({', '.join(pats)}), most recent {max(c['opened_at'] for c in same_card_fraud)[:10]}"
                + (f"; {len(recent)} within {P['recent_case_days']} days on the same channel" if recent else ""),
                "graph",
                _q("prior_cases_for_customer", customer=ctx.customer_id),
                [c["id"] for c in same_card_fraud][:12] + [cid],
                "memory",
                "fraud",
                0.8 if recent else 0.4,
            )
        )
    if prior_ato and txn["channel"] == "in_person" and rh.get("hint") == "home":
        led.append(
            Evidence(
                f"The card's {len(prior_ato)} prior account-takeover case(s) were confirmed in its modal region, so an in-region purchase is also the card's established fraud shape",
                "graph",
                _q("prior_cases_for_customer", customer=ctx.customer_id),
                [c["id"] for c in prior_ato][:6] + [cid],
                "history",
                "fraud",
                0.5,
            )
        )
    ct_cases = [c for c in same_card_fraud if c["pattern"] == "card_testing"]
    n_small_48 = sum(1 for m in raw48 if m["channel"] == "online" and float(m["amt"]) < config.CARD_TESTING_SMALL)
    if ct_cases and n_small_48 >= 1:
        led.append(
            Evidence(
                f"Card testing is a live alternative pattern: {n_small_48} online authorisation(s) under $5 inside 48 h and {len(ct_cases)} prior card-testing case(s) on this card ({', '.join(c['id'] for c in ct_cases[:4])})",
                "graph",
                _q("card_testing_check", card=cid),
                [c["id"] for c in ct_cases[:4]] + [cid],
                "memory",
                "fraud",
                0.3,
            )
        )
    cleared_same_tpl = [c for c in same_card_cleared if _cleared_template_matches(c, txn, rh)]
    flags["cleared_precedent_case_ids"] = [c["id"] for c in cleared_same_tpl]
    if same_card_cleared and risk_alert:
        tpl_note = (
            "; the cleared alert had the same shape as this one"
            if cleared_same_tpl
            else "; that alert was a different shape (" + ", ".join(sorted({str(c.get("template_id", "")).replace("cleared_", "") for c in same_card_cleared})) + "), so it is a weak precedent"
        )
        led.append(
            Evidence(
                f"{len(same_card_cleared)} prior risk-score alert(s) on this card were cleared after the customer confirmed the activity ({', '.join(c['id'] for c in same_card_cleared)})" + tpl_note,
                "graph",
                _q("prior_cases_for_customer", customer=ctx.customer_id),
                [c["id"] for c in same_card_cleared] + [cid],
                "memory",
                "legit",
                0.7 if cleared_same_tpl else 0.3,
            )
        )
    if other_card_fraud:
        led.append(
            Evidence(
                f"Customer's other card(s) carry {len(other_card_fraud)} confirmed-fraud case(s): {', '.join(sorted({c['card_id'] for c in other_card_fraud}))}",
                "graph",
                _q("prior_cases_for_customer", customer=ctx.customer_id),
                [c["id"] for c in other_card_fraud][:6],
                "memory",
                "neutral",
                0.0,
            )
        )
    if flags["ring_hit"] and flags["ring_cases"]:
        led.append(
            Evidence(
                f"The ring profile appears in {len(flags['ring_cases'])} closed cases confirmed as undocumented fraud in August-September ({', '.join(flags['ring_cases'])}), each listing the other wave cards as connected",
                "graph",
                _q("similar_prior_cases", device=flags["ring_id"]),
                list(flags["ring_cases"]),
                "memory",
                "fraud",
                1.0,
            )
        )
    dev_cleared = [s for s in sim if "same_device" in s.get("overlap_reasons", []) and s["outcome_or_verdict"] == "cleared"]
    dev_fraud = [s for s in sim if "same_device" in s.get("overlap_reasons", []) and s["outcome_or_verdict"] == "confirmed_fraud"]
    if txn["device_new"] == "New" and len(dev_cleared) >= 2 and not flags["ring_hit"]:
        led.append(
            Evidence(
                f"The same device profile appears in {len(dev_cleared)} closed alerts on other cards that were cleared as 'new phone' ({', '.join(s['id'] for s in dev_cleared[:4])})",
                "graph",
                _q("similar_prior_cases", device=dev_id),
                [s["id"] for s in dev_cleared[:4]],
                "memory",
                "legit",
                0.5,
            )
        )
    if dev.get("is_strong") and len(dev_fraud) >= 1 and not flags["ring_hit"]:
        led.append(
            Evidence(
                f"The strong device profile appears in {len(dev_fraud)} closed confirmed-fraud case(s) on other cards ({', '.join(s['id'] for s in dev_fraud[:4])})",
                "graph",
                _q("similar_prior_cases", device=dev_id),
                [s["id"] for s in dev_fraud[:4]],
                "memory",
                "fraud",
                0.6,
            )
        )

    # ---------------------------------------------------------------- customer / analyst family
    if denial:
        led.append(
            Evidence(
                f"Customer {ctx.customer_id} reports they did not make the ${amt:,.2f} purchase {txn['id']} (the denial is evidence: 4,665/4,665 customer-reported closed cases were confirmed fraud, 0 denials were cleared)",
                "customer",
                "trigger",
                [txn["id"], ctx.customer_id],
                "customer",
                "fraud",
                1.5,
            )
        )
    if analyst:
        led.append(Evidence("Analyst request: several cards this month show purchases from the same unusual device profile", "customer", "trigger", [txn["id"], cid], "customer", "neutral", 0.0))

    # ---------------------------------------------------------------- families and adjustments
    fam_f, fam_l = _families(led)
    adj: list[tuple[str, float]] = []
    if flags["r5_run"]:
        adj.append(("R5 card-testing run feeding the flagged purchase", P["card_testing_bump"]))
    if flags["recurring_match"]:
        adj.append(("R7 recurring-charge match", P["recurring_bump"]))
    if cleared_same_tpl and risk_alert:
        adj.append(("prior cleared alert of the same template on the card", P["cleared_precedent_bump"]))
    if recent:
        adj.append(("recent card-testing / CNP case on the card", P["recent_case_bump"]))
    if len(shared_fraud_cards) >= 2 and not flags["ring_hit"]:
        adj.append(("strong shared profile with >= 2 other fraud cards within +-30 d", P["shared_profile_bump"]))
    if denial and cal_p >= 0.5 and not flags["recurring_match"]:
        adj.append(("customer denial corroborated by the scorer (calibrated >= 0.5)", P["denial_bump_when_scorer_agrees"]))
    total = max(-P["max_abs_adjustment"], min(P["max_abs_adjustment"], sum(a for _, a in adj)))
    p1 = _expit(_logit(cal_p) + total)
    if flags["scorer_unreliable"]:
        p1 = 0.5 + (1 - P["scorer_unreliable_shrink"]) * (p1 - 0.5)
        adj.append(("scorer-unreliable shrink toward 0.5", 0.0))
    flags["conflict"] = flags["conflict_chain"]
    if flags["conflict_chain"]:
        lo, hi = P["conflict_clamp"]
        p1 = min(max(p1, lo), hi)
        adj.append((f"conflict override: rulebook chain ${flags['rulebook_chain_sum']:,.2f} vs scorer {cms_p:.2f} → clamp [{lo}, {hi}]", 0.0))
    floor = 0.0
    if flags["ring_hit"]:
        floor = P["ring_floor"]
        adj.append(("anonymous-proxy device ring → floor", floor))
    elif flags["burst_hit"] and denial:
        floor = P["burst_with_denial_floor"]
        adj.append(("under-$500 burst with a customer denial → floor", floor))
    elif flags["burst_hit"]:
        floor = P["burst_alone_floor"]
        adj.append(("under-$500 burst without a denial → floor", floor))
    elif denial and not flags["recurring_match"]:
        floor = P["denial_floor"]
        adj.append(("customer denial not explained by R7 → floor", floor))
    p = round(max(p1, floor), 4)
    flags["floor"] = floor
    flags["families_conflict"] = _families_conflict(fam_f, fam_l)
    verdict = _verdict(p, fam_f, flags, P)

    sc = Scorecard(
        cms_p=round(cms_p, 4),
        cal_p=round(cal_p, 4),
        adjustments=adj,
        p_engine=p,
        families_fraud=fam_f,
        families_legit=fam_l,
        flags=flags,
        chain=chain,
        episode_ids=[],
        first_suspicious_txn_id="",
        exposure_usd=0.0,
        connected_card_ids=[],
        connected_device_profiles=[],
        pattern="none",
        pattern_description="",
        verdict=verdict,
        similar_prior_cases=_similar(sim, flags, cid),
        evidence=led,
    )
    _derive_episode(sc, txn)
    return sc


def _net(led: list[Evidence], family: str) -> float:
    return sum(e.weight if e.direction == "fraud" else -e.weight if e.direction == "legit" else 0.0 for e in led if e.family == family)


def _families(led: list[Evidence]) -> tuple[set[str], set[str]]:
    """A family counts for fraud / legitimacy only when its net weight clears `family_strength`."""
    thr = float(policy_params()["engine"].get("family_strength", 0.5))
    fams = {e.family for e in led if e.family in ("history", "device", "memory", "customer")}
    f = {x for x in fams if _net(led, x) >= thr}
    lg = {x for x in fams if _net(led, x) <= -thr}
    return f, lg


def _families_conflict(fam_f: set[str], fam_l: set[str]) -> bool:
    """D3: the evidence conflicts when at least one fraud family AND at least one legitimacy family are in the
    ledger, the customer's own statement excluded (it is what a verification re-asks about)."""
    return bool((set(fam_f) - {"customer"}) and (set(fam_l) - {"customer"}))


def _verdict(p: float, fam_f: set[str], flags: dict, P: dict) -> str:
    """D1: fraud at p >= fraud_band on the probability alone (the two-family requirement belongs to the §6 stop
    test at stop_high, policy.settled_pre); legitimate at p <= legit_band (or after a confirmation, post_evidence)."""
    if p >= P["fraud_band"]:
        return "fraud"
    if p <= P["legit_band"]:
        return "legitimate"
    return "uncertain"


def _similar(sim: list[dict], flags: dict, cid: str) -> list[str]:
    ids: list[str] = []
    for s in sim:
        if s["id"].startswith("CC-") and s["id"] not in ids:
            ids.append(s["id"])
    for r in flags.get("ring_cases", []):
        if r not in ids:
            ids.append(r)
    if flags.get("ring_hit"):
        ids = list(flags["ring_cases"]) + [i for i in ids if i not in flags["ring_cases"]]
    return ids[:6]


def _derive_episode(sc: Scorecard, txn: dict) -> None:
    """Pattern, episode, exposure and connected entities for the scorecard's current verdict."""
    f = sc.flags
    extra_rows = []
    if f.get("ring_hit"):
        extra_rows = [
            {
                "id": str(r["id"]),
                "ts": r["ts"],
                "amt": float(r["amt"]),
                "channel": "online",
                "addr1": "",
                "device_new": "New",
                "product_cd": txn.get("product_cd", ""),
                "p_email": "",
                "device_id": f["ring_id"],
                "cms_p": -1.0,
                "sig_match": True,
            }
            for r in f["ring_rows"]
        ]
    ep_flags = {
        **f,
        "pattern_member_rows": extra_rows,
        "flagged_ts": txn["ts"],
        "ring_txn_ids": f["ring_txn_ids"] if f.get("ring_hit") else [],
        "burst_txn_ids": f["burst_txn_ids"] if f.get("burst_hit") else [],
        "card_testing_run_ids": f["card_testing_run_ids"] if f.get("r5_run") else [],
        "conflict_chain_ids": f.get("conflict_chain_ids", []) if f.get("conflict_chain") else [],
    }
    ids = episode_rule.members(sc.chain, txn["id"], sc.verdict, ep_flags)
    by_id = {str(m["id"]): m for m in sc.chain}
    for r in extra_rows:
        by_id.setdefault(r["id"], r)
    rows = [by_id[i] for i in ids if i in by_id]
    if sc.verdict == "legitimate":
        sc.episode_ids, sc.first_suspicious_txn_id, sc.exposure_usd, sc.pattern, sc.pattern_description = [], "", 0.0, "none", ""
        sc.connected_card_ids, sc.connected_device_profiles = [], []
        f.update(pattern_rule.episode_flags([], f.get("modal_region", "")))
        f["card_testing_chain"] = False
        return
    f.update(pattern_rule.episode_flags(rows, f.get("modal_region", "")))
    channels = {m.get("channel") for m in rows}
    n_small = sum(1 for m in rows if m.get("channel") == "online" and float(m.get("amt", 0)) < config.CARD_TESTING_SMALL)
    f["card_testing_chain"] = bool(channels == {"online"} and len(rows) >= 5 and n_small >= 1)
    sc.pattern = pattern_rule.label(rows, f.get("modal_region", ""), f)
    sc.episode_ids = ids
    sc.first_suspicious_txn_id = ids[0] if ids else ""
    sc.exposure_usd = episode_rule.exposure(sc.chain, ids, extra_rows)
    f["candidate_chain_ids"] = [str(m["id"]) for m in sc.chain if float(m.get("cms_p", -1)) >= episode_rule.MIN_CMS or m.get("sig_match")]
    if f.get("ring_hit"):
        sc.connected_card_ids = list(f["wave_cards"])  # D6: the wave cluster (27 for HHG-014), not the all-time card set
        sc.connected_device_profiles = [f["ring_id"]]
        sc.pattern_description = (
            f"Anonymous-proxy device ring: {len(f['wave_cards']) + 1} cards in the current wave ({len(f.get('pre_open_cards', []))} of them active before this case opened) share one exact device profile "
            f"`{f['ring_id']}` routed through {config.ANON_PROXY}, each making small online purchases marked New for the account. "
            f"The same profile drove {len(f['ring_cases'])} closed cases in August-September that analysts confirmed as undocumented fraud. "
            f"Found by following FROM_DEVICE from the flagged transaction to the profile and back out to every other card on it."
        )
    elif f.get("burst_hit"):
        b = f["burst"]
        look = f.get("burst_lookalikes", [])
        sc.connected_card_ids = []  # closed burst cases list no connected cards; look-alikes are named here
        sc.connected_device_profiles = []
        sc.pattern_description = (
            f"Under-threshold burst: {len(b['ids'])} online purchases each between $450 and $499.99 (${sum(b['amts']):,.2f} in total) inside "
            f"{int((_dt(b['end_ts']) - _dt(b['start_ts'])).total_seconds() // 60)} minutes on a card with almost no online history, split across {len(b['device_ids'])} New device profiles"
            + (f" and {len(b['emails'])} purchaser email domains" if len(b.get("emails", [])) > 1 else "")
            + f". Amounts sit just under a $500 authorisation threshold. {len(look)} other cards show the identical shape within 30 days ({', '.join(look[:12])}); "
            f"five closed cases with this shape were confirmed as undocumented fraud and reported."
        )
    elif f.get("shared_device_fraud") and f.get("shared_fraud_cards"):
        sc.connected_card_ids = list(f["shared_fraud_cards"])
        sc.connected_device_profiles = [txn["device_id"]]
    else:
        sc.connected_card_ids, sc.connected_device_profiles = [], []
    if sc.pattern != "undocumented":
        sc.pattern_description = ""


def post_evidence(sc: Scorecard, outcome: str) -> Scorecard:
    """Apply an assumed reply (PLAN §4.5 post-evidence moves) and re-derive verdict / episode / pattern."""
    P = policy_params()["engine"]
    PE = P["post_evidence"]
    new = copy.deepcopy(sc)
    txn_id = new.flags.get("flagged_txn_id") or (new.episode_ids[0] if new.episode_ids else "")
    ref = f"evidence_request:{new.flags.get('request_seq', 1)}"
    fam_before = set(new.families_fraud) - {"customer"}
    p = new.p_engine
    # D10: a confirmation / passed step-up never clears a ring or burst hit — the pattern floor is re-applied and R3 does
    # not fire (policy.conditions `customer_confirmed`); the alert stays in its band and the request is treated as unsettled.
    pattern_hit = bool(new.flags.get("ring_hit") or new.flags.get("burst_hit"))
    if outcome == "confirm":
        new.evidence.append(Evidence("Customer confirmed the activity when asked (assumed reply)", "customer", ref, [], "customer", "legit", 2.0))
        if pattern_hit:
            p = max(p, float(new.flags.get("floor", 0.0)))
            new.adjustments.append(("customer confirmation cannot clear a ring / burst hit → floor kept", float(new.flags.get("floor", 0.0))))
        else:
            p = min(p, PE["confirm_ceiling"])
            new.adjustments.append(("customer confirmation → ceiling", PE["confirm_ceiling"]))
    elif outcome == "deny":
        new.evidence.append(Evidence("Customer denied the purchases when asked and still has the card (assumed reply)", "customer", ref, [], "customer", "fraud", 1.5))
        p = _expit(_logit(p) + PE["deny_bump"])
        new.adjustments.append(("customer denial through a request", PE["deny_bump"]))
        if fam_before:
            p = max(p, PE["deny_floor_with_second_family"])
            new.adjustments.append(("denial with a second fraud family → floor", PE["deny_floor_with_second_family"]))
    elif outcome == "pass":
        new.evidence.append(Evidence("Step-up authentication passed (assumed reply)", "customer", ref, [], "customer", "legit", 1.0))
        p = _expit(_logit(p) + PE["pass_bump"])
        new.adjustments.append(("step-up passed", PE["pass_bump"]))
        if pattern_hit:
            p = max(p, float(new.flags.get("floor", 0.0)))
            new.adjustments.append(("a passed step-up cannot clear a ring / burst hit → floor kept", float(new.flags.get("floor", 0.0))))
        else:
            # R3 / §5 closes the alert on a passed possession check (simulated only for risk-score triggers with a known
            # device and F' <= 1, D5); the file's verdict must then be legitimate (D10: CLOSE_NO_FRAUD ⇒ legitimate), so the
            # probability is capped at the legitimate band like a confirmation is capped at confirm_ceiling.
            p = min(p, PE.get("pass_ceiling", P["legit_band"]))
            new.adjustments.append(("step-up passed → ceiling", PE.get("pass_ceiling", P["legit_band"])))
    elif outcome == "fail":
        new.evidence.append(Evidence("Step-up authentication failed (assumed reply)", "customer", ref, [], "customer", "fraud", 1.0))
        p = _expit(_logit(p) + PE["fail_bump"])
        new.adjustments.append(("step-up failed", PE["fail_bump"]))
    elif outcome == "info":
        new.evidence.append(Evidence("Analyst information received (assumed reply); it did not change the action set", "customer", ref, [], "customer", "neutral", 0.0))
    else:  # no_reply / inconclusive
        new.evidence.append(Evidence("No reply within 24 hours / verification not completed (assumed); probability unchanged", "customer", ref, [], "customer", "neutral", 0.0))
    new.p_engine = round(p, 4)
    new.families_fraud, new.families_legit = _families(new.evidence)
    new.flags["outcome"] = outcome
    new.flags["pending"] = outcome in ("no_reply", "inconclusive") or (outcome in ("confirm", "pass") and pattern_hit)
    new.flags["families_conflict"] = _families_conflict(new.families_fraud, new.families_legit)
    new.verdict = _verdict(new.p_engine, new.families_fraud, new.flags, P)
    if outcome in ("confirm", "pass") and not pattern_hit:
        new.verdict = "legitimate"  # R3 (confirmation) / R3-§5 (passed possession check) close the alert
    elif outcome in ("no_reply", "inconclusive") and new.verdict == "legitimate":
        new.verdict = "uncertain"  # nothing settled it: the alert stays open under R4, never closed as legitimate
    txn = {"id": txn_id, "ts": new.flags.get("flagged_ts", ""), "product_cd": "", "device_id": new.flags.get("flagged_device_id", "")}
    _derive_episode(new, txn)
    return new
