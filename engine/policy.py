"""Policy gate — PLAN §4.7. The YAML (policy/fraud_policy.yaml) is the policy; this module evaluates it.

admissible(ctx, sc, stage) -> {"required", "allowed", "forbidden", "routes", "citations", "fired"}
check(actions, ctx, sc, stage) -> [violations]
order(actions) -> actions sorted by the YAML ordering
route(action, exposure) -> auto | L1 | L2
sar_required(sc) -> (file, reason)

Conditions are named booleans computed from (ctx, sc, stage) in conditions(); a rule fires when all
its `when` names hold; `not_<name>` negates. Fired rules contribute require / require_one_of / forbid /
add_if; the deterministic recommendation is required ∪ (one representative of each require_one_of),
minus forbidden, ordered. Conflicts between a require and a forbid are resolved in favour of the
forbid (R1 / R7 / the uncertain band protect the customer) and reported in `citations["_conflicts"]`.

Citations and `fired` name the README rule or section each YAML rule implements (its `cite`, e.g. `§3b`
for the fraud band, `R4` for the uncertain final set), never an internal key such as `fraud_band`.

R2 (a customer denial) is an action rule that applies from intake at any verdict, unless R7 matches
(`r2_denial`): the README §3b worked example has R1 govern BEFORE a denial and R2 once the customer has
denied, and a customer_report trigger is the denial itself. An in-person denial is settled at intake
(`settled_pre`): the customer has already answered, so there is no customer_validation to ask.
"""
from __future__ import annotations

import functools

import yaml

from engine import config
from engine.types import CaseContext, Scorecard


@functools.lru_cache(maxsize=1)
def params(path: str | None = None) -> dict:
    return yaml.safe_load(open(path or config.POLICY_YAML))


def route(action: str, exposure: float) -> str:
    P = params()
    if action == "BLOCK_CARD":
        return "L2" if exposure > P["routes"]["block_l2_exposure"] else "L1"
    for r in ("auto", "L1", "L2"):
        if action in P["routes"][r]:
            return r
    raise ValueError(f"unknown action {action}")


def order(actions: list[dict]) -> list[dict]:
    idx = {a: i for i, a in enumerate(params()["ordering"])}
    return sorted(actions, key=lambda a: idx.get(a["action"], 99))


# ----------------------------------------------------------------------------- conditions
def r2_denial(sc: Scorecard) -> bool:
    """R2 applies: the customer denied the transaction (the customer_report trigger, or a denial returned by a
    customer_validation request), the charge does not match the customer's own recurring pattern (R7 is the only
    exception) and the customer has not since withdrawn the dispute. A failed step-up on a fraud verdict keeps the
    earlier R2 path (`denial ∧ verdict_fraud` in conditions)."""
    f = sc.flags
    outcome = f.get("outcome", "")
    if f.get("recurring_match"):
        return False
    pattern_hit = bool(f.get("ring_hit") or f.get("burst_hit"))
    if outcome in ("confirm", "pass") and not pattern_hit:
        return False
    return bool(f.get("denial")) or outcome == "deny"


def denial_settles(sc: Scorecard) -> bool:
    """§6 test 2 at intake: an in-person R2 denial needs no request — the customer already answered the
    customer_validation question in the trigger, and step-up authentication is an online possession check."""
    return r2_denial(sc) and not sc.flags.get("outcome") and sc.flags.get("channel") != "online"


def settled_pre(sc: Scorecard) -> bool:
    """§6 test 1 before any request (or §6 test 2 on an in-person customer denial, `denial_settles`). With the
    verify_before_close house rule the low side never settles pre-evidence (a legit-leaning alert is verified
    first, as every cleared alert in history was)."""
    P = params()
    T, E = P["thresholds"], P["engine"]
    high = sc.p_engine >= T["stop_high"] and len(sc.families_fraud) >= T["stop_min_families"]
    low = sc.p_engine <= T["stop_low"] and len(sc.families_legit) >= T["stop_min_families"] and not E.get("verify_before_close", True)
    return bool(high or low or denial_settles(sc))


def conditions(ctx: CaseContext, sc: Scorecard, stage: str) -> dict:
    P = params()
    T = P["thresholds"]
    f = sc.flags
    p = sc.p_engine
    outcome = f.get("outcome", "")
    asked = bool(f.get("asked")) or outcome != ""
    n_f = len(sc.families_fraud)
    pattern_hit = bool(f.get("ring_hit") or f.get("burst_hit"))
    # D10: a confirmation / passed step-up never clears a ring or burst hit — R3 does not fire and the request is unsettled
    cleared = outcome in ("confirm", "pass") and not pattern_hit
    settled = settled_pre(sc) or outcome in ("deny", "fail") or cleared or (asked and outcome in ("no_reply", "inconclusive", "info")) \
        or (asked and outcome in ("confirm", "pass") and pattern_hit)
    sar, _ = sar_required(sc)
    # D3: conflict = at least one fraud family AND at least one legitimacy family in the ledger (customer statement excluded),
    # or the rulebook-chain-vs-scorer override (flags.conflict). R8 fires only when uncertain AND (exposure > $500 OR conflict).
    F_, L_ = set(sc.families_fraud) - {"customer"}, set(sc.families_legit) - {"customer"}
    conflict = bool(f.get("conflict") or (F_ and L_))
    c = {
        "stage_initial": stage == "initial", "stage_final": stage == "final", "requested": asked,
        "verdict_fraud": sc.verdict == "fraud", "verdict_uncertain": sc.verdict == "uncertain", "verdict_legit": sc.verdict == "legitimate",
        "p_below_r1": p < T["r1_probability"], "p_below_stop_high": p < T["stop_high"],
        "single_signal": n_f < 2,
        "settled": bool(settled), "case_open": p >= T["case_open_probability"] or f.get("denial", False) or asked or ctx.trigger_type == "analyst_request",
        "denial": bool(f.get("denial")) or outcome == "deny" or outcome == "fail",
        # README R2 + §3b example: a customer denial (not R7) takes R2 at any verdict; a failed step-up on a fraud verdict keeps R2 too
        "r2_denial": r2_denial(sc) or ((bool(f.get("denial")) or outcome in ("deny", "fail")) and sc.verdict == "fraud"),
        "customer_confirmed": cleared,
        "no_reply": outcome in ("no_reply", "inconclusive") or (outcome in ("confirm", "pass") and pattern_hit),
        "risk_alert": ctx.trigger_type == "risk_score", "analyst_request": ctx.trigger_type == "analyst_request",
        "exposure_gt_500": sc.exposure_usd > T["r4_escalate_exposure"],
        "exposure_gt_1000": sc.exposure_usd > T["r2_report_exposure"],
        "sar_required": sar,
        "shared_origin": bool(f.get("ring_hit") or f.get("shared_device_fraud") or f.get("shared_recipient_email")),
        "r5_run": bool(f.get("r5_run")), "r5_cleared_over_big": bool(f.get("r5_cleared_over_big")),
        "recurring_match": bool(f.get("recurring_match")),
        "undocumented": sc.pattern == "undocumented",
        "conflict": conflict,
        "r8_trigger": sc.verdict == "uncertain" and (sc.exposure_usd > T["r8_exposure"] or conflict),
        "two_cards_confirmed_fraud": bool(f.get("two_cards_confirmed_fraud")),
        "channel_online": f.get("channel") == "online",
        "block_allowed": not (sc.verdict == "uncertain" and stage == "initial") and not (p < T["r1_probability"] and n_f < 2 and not settled),
    }
    c["not_case_open"] = not c["case_open"]
    for k in list(c):
        c.setdefault("not_" + k, not c[k])
    c["_request_type"] = f.get("request_type", "")
    return c


def _pick_one(options: list[str], c: dict, chosen: set[str]) -> str:
    """Representative for require_one_of: keep an already-chosen option; else the action matching the evidence
    request the harness is making (flags.request_type, see engine/voi.request_type_for); else STEP_UP_AUTH
    online in the uncertain / dispute / conflict / fraud band and VERIFY_WITH_CUSTOMER otherwise."""
    for o in options:
        if o in chosen:
            return o
    want = {"step_up_auth": "STEP_UP_AUTH", "customer_validation": "VERIFY_WITH_CUSTOMER"}.get(c.get("_request_type", ""))
    if want in options:
        return want
    if "STEP_UP_AUTH" in options and c.get("channel_online") and (c.get("verdict_uncertain") or c.get("denial") or c.get("conflict") or c.get("verdict_fraud")):
        return "STEP_UP_AUTH"
    return "VERIFY_WITH_CUSTOMER" if "VERIFY_WITH_CUSTOMER" in options else options[0]


def cite(name: str, branch: dict | None = None) -> str:
    """The README rule / section a YAML rule (or one of its add_if branches) implements (D-REASON)."""
    rule = params()["rules"][name]
    return str((branch or {}).get("cite") or rule.get("cite") or name)


def admissible(ctx: CaseContext, sc: Scorecard, stage: str) -> dict:
    P = params()
    c = conditions(ctx, sc, stage)
    required: dict[str, list[str]] = {}
    forbidden: dict[str, list[str]] = {}
    one_of: list[tuple[str, list[str]]] = []
    fired: list[str] = []
    for name, rule in P["rules"].items():
        if not all(c.get(w, False) for w in rule.get("when", [])):
            continue
        label = cite(name)
        if label not in fired:
            fired.append(label)
        for a in rule.get("require", []):
            required.setdefault(a, []).append(label)
        if rule.get("require_one_of"):
            one_of.append((label, rule["require_one_of"]))
        for a in rule.get("forbid", []):
            forbidden.setdefault(a, []).append(label)
        for extra in rule.get("add_if", []):
            if all(c.get(w, False) for w in extra.get("when", [])):
                blabel = cite(name, extra)
                for a in extra.get("actions", []):
                    required.setdefault(a, []).append(blabel)
                if extra.get("actions_one_of"):
                    one_of.append((blabel, extra["actions_one_of"]))
    chosen = set(required)
    for name, opts in one_of:
        a = _pick_one(opts, c, chosen)
        required.setdefault(a, []).append(name)
        chosen.add(a)
    conflicts = {a: (required[a], forbidden[a]) for a in required if a in forbidden}
    for a in conflicts:
        required.pop(a)
    allowed = [a for a in P["actions"] if a not in forbidden]
    routes = {a: route(a, sc.exposure_usd) for a in P["actions"]}
    citations = {a: sorted(set(v)) for a, v in required.items()}
    citations["_forbidden"] = {a: sorted(set(v)) for a, v in forbidden.items()}
    citations["_conflicts"] = {a: {"required_by": r, "forbidden_by": fb} for a, (r, fb) in conflicts.items()}
    return {"required": order([{"action": a} for a in required]) and [x["action"] for x in order([{"action": a} for a in required])],
            "allowed": allowed, "forbidden": sorted(forbidden), "routes": routes, "citations": citations, "fired": fired, "conditions": c}


def check(actions: list[dict], ctx: CaseContext, sc: Scorecard, stage: str) -> list[str]:
    """Violations of the policy for a proposed action list (LLM- or engine-proposed)."""
    P = params()
    adm = admissible(ctx, sc, stage)
    names = [a["action"] for a in actions]
    v: list[str] = []
    for a in actions:
        if a["action"] not in P["actions"]:
            v.append(f"unknown action {a['action']}")
            continue
        if a["action"] in adm["forbidden"]:
            v.append(f"{a['action']} is forbidden at stage {stage} by {adm['citations']['_forbidden'][a['action']]}")
        exp_route = route(a["action"], sc.exposure_usd)
        if a.get("route") != exp_route:
            v.append(f"{a['action']} must route {exp_route} at exposure {sc.exposure_usd} (got {a.get('route')})")
        if not a.get("reason"):
            v.append(f"{a['action']} has no reason")
        import re as _re
        if _re.search(r"\bR1\b(?!\s*(not|does not|is not|n/a))", a.get("reason") or "") and not (sc.p_engine < P["thresholds"]["r1_probability"] and len(sc.families_fraud) < 2):
            v.append(f"{a['action']} cites R1 but p={sc.p_engine} / families={len(sc.families_fraud)} do not satisfy R1")
    for a in adm["required"]:
        if a not in names:
            v.append(f"{a} is required at stage {stage} by {adm['citations'].get(a)}")
    if "FILE_REPORT" in names and "CREATE_CASE" not in names:
        v.append("FILE_REPORT without CREATE_CASE (3a: a report always has a case behind it)")
    if len(names) != len(set(names)):
        v.append("duplicate actions")
    if [a["action"] for a in order(actions)] != names:
        v.append("actions are not in policy order")
    if "BLOCK_ALL_CARDS" in names and not sc.flags.get("two_cards_confirmed_fraud"):
        v.append("R10: BLOCK_ALL_CARDS without two cards with confirmed fraud")
    return v


def sar_required(sc: Scorecard) -> tuple[bool, str]:
    """3a: file only when fraud is confirmed / strongly suspected AND a listed link holds."""
    P = params()
    S = P["sar"]["reason_templates"]
    T = P["thresholds"]
    f = sc.flags
    exp = sc.exposure_usd
    if sc.verdict != "fraud":
        # R2: "Add FILE_REPORT if exposure exceeds $1,000" — the customer's denial is the strong suspicion 3a asks for, so an
        # R2 case files on exposure even while the graph evidence leaves the verdict uncertain (the bank filed on every
        # customer-reported case above $1,000). The shared-device / other-card clause keeps the ring-grade reading below.
        if not r2_denial(sc):
            return False, S["not_fraud"].format(verdict=sc.verdict, exposure=exp)
        if exp > T["r2_report_exposure"]:
            return True, S["file_r2_exposure"].format(exposure=exp)
        return False, S["not_met_r2"].format(exposure=exp, device_clause=_device_clause(sc))
    if f.get("ring_hit"):
        return True, S["file_ring"].format(profile=f["ring_id"], n_cards=len(f.get("wave_cards", [])))
    if sc.pattern == "undocumented":
        return True, S["file_undocumented"].format(description=(sc.pattern_description.split(":")[0] or "undocumented pattern"))
    if exp > T["r2_report_exposure"]:
        return True, S["file_exposure"].format(exposure=exp)
    if f.get("shared_device_fraud") and f.get("shared_fraud_cards") and not P["sar"].get("shared_device_requires_ring", True):
        return True, S["file_shared_device"].format(profile=f.get("flagged_device_id", ""), n_cards=len(f["shared_fraud_cards"]), n_fraud=len(f["shared_fraud_cards"]))
    if f.get("shared_recipient_email") and P["sar"].get("recipient_email_files", False):
        return True, S["file_other_customer"].format(cases=", ".join(f["shared_recipient_email"]))
    return False, S["not_met"].format(exposure=exp, device_clause=_device_clause(sc))


def _device_clause(sc: Scorecard) -> str:
    S = params()["sar"]["reason_templates"]
    f = sc.flags
    dev_id = f.get("flagged_device_id", "")
    if f.get("shared_fraud_cards"):
        return (f"the strong profile `{dev_id}` is shared with {len(f['shared_fraud_cards'])} other cards' fraud (monitored under R6) but is not a "
                f"ring-grade link: the bank's 397 filed reports were all exposure > $1,000 or the anonymous-proxy ring")
    if dev_id:
        new_word = "New " if f.get("flagged_device_new") == "New" else ""
        if not f.get("flagged_device_strong"):
            return S["device_common"].format(new_word=new_word, profile=dev_id)
        return S["device_generic"].format(new_word=new_word, profile=dev_id, n_cards=f.get("flagged_device_cards", 0))
    return S["device_none"]
