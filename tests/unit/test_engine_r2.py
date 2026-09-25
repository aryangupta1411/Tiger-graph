"""R2 from intake, rule-id citations and the out_of_region_use convention (engine fix pass, D-R2 / D-REASON / D-LABEL).

README R2: "Customer denies the transaction. Recommend BLOCK_CARD and CREATE_CASE. Add FILE_REPORT if exposure exceeds
$1,000 or the case connects to a shared device profile or another card's fraud." The README §3b example has R1 govern
BEFORE a denial and R2 once the customer denies; a customer_report trigger IS the denial, so R2 applies from intake at
any verdict, unless R7 (the disputed charge matches the customer's own recurring pattern) applies.

The policy-table tests need no data; the exam-pack tests need the facts DB and are skipped without it (CI, M10).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent.validator import reason_problems
from engine import answer_writer, config, policy, stop, validator, voi
from engine.types import CaseContext, Scorecard

CTX = CaseContext("T-1", "risk_score", "", "1", "C00001-K1", "C00001", "2016-12-01 00:00:00", 0.8)
DISP = CaseContext("T-2", "customer_report", "", "1", "C00001-K1", "C00001", "2016-12-01 00:00:00", None)
RULE_ID = re.compile(r"^(R([1-9]|10)|3a|§\d[a-z]?)$")          # README rule number or README section, nothing internal
INTERNAL = re.compile(r"fraud_band|uncertain_initial|uncertain_final|legit_band|legit_leaning|3a_case|3a_report")
needs_db = pytest.mark.skipif(not Path(config.FACTS_DB).exists(), reason=f"needs the facts DB ({config.FACTS_DB})")


def _sc(p, fams_f, fams_l=(), verdict="uncertain", exposure=100.0, **flags) -> Scorecard:
    base = {"denial": False, "outcome": "", "asked": False, "channel": "online", "ring_hit": False, "burst_hit": False,
            "shared_device_fraud": False, "shared_recipient_email": [], "r5_run": False, "recurring_match": False, "conflict": False}
    base.update(flags)
    return Scorecard(cms_p=p, cal_p=p, adjustments=[], p_engine=p, families_fraud=set(fams_f), families_legit=set(fams_l), flags=base, chain=[],
                     episode_ids=["1"], first_suspicious_txn_id="1", exposure_usd=exposure, connected_card_ids=[], connected_device_profiles=[],
                     pattern="card_not_present_fraud", pattern_description="", verdict=verdict, similar_prior_cases=[])


# ----------------------------------------------------------------------------- R2 at intake
def test_in_person_denial_blocks_at_intake_without_asking_the_customer_again():
    sc = _sc(0.55, ["customer"], ["history"], denial=True, channel="in_person")
    assert policy.r2_denial(sc) and policy.settled_pre(sc) and stop.settled_pre(sc)
    adm = policy.admissible(DISP, sc, "initial")
    assert adm["required"] == ["CREATE_CASE", "BLOCK_CARD"] and adm["routes"]["BLOCK_CARD"] == "L1"
    assert "R2" in adm["citations"]["BLOCK_CARD"] and "R1" not in adm["fired"] and "BLOCK_CARD" not in adm["forbidden"]
    assert "VERIFY_WITH_CUSTOMER" not in adm["required"]
    assert voi.should_ask(DISP, sc, "customer_validation") == (False, {})     # the customer already answered
    assert policy.check(answer_writer.reasons_for(adm["required"], adm, DISP, sc, "initial"), DISP, sc, "final") == []   # final == initial
    assert stop.stop_reason(sc, None, False, True).startswith("Policy §6 test 2")


def test_online_denial_blocks_at_intake_and_steps_up():
    sc = _sc(0.55, ["customer"], [], denial=True, channel="online")
    assert not policy.settled_pre(sc)
    adm = policy.admissible(DISP, sc, "initial")
    assert adm["required"] == ["CREATE_CASE", "STEP_UP_AUTH", "BLOCK_CARD"] and "MONITOR_CARD" not in adm["required"]
    ask, branches = voi.should_ask(DISP, sc, voi.request_type_for(DISP, sc))
    assert ask and "BLOCK_CARD" in branches["fail"] and "BLOCK_CARD" in branches["inconclusive"]
    post = _sc(0.55, ["customer"], [], denial=True, outcome="inconclusive", asked=True)
    fin = policy.admissible(DISP, post, "final")
    assert fin["required"] == ["CREATE_CASE", "MONITOR_CARD", "DECLINE_TRANSACTION", "BLOCK_CARD"]
    assert "R2's block" in stop.stop_reason(sc, post, True, False)


def test_r2_route_report_and_r8_on_a_denial():
    adm = policy.admissible(DISP, _sc(0.55, ["customer"], [], denial=True, channel="in_person", exposure=2600.0), "initial")
    assert adm["routes"]["BLOCK_CARD"] == "L2" and "FILE_REPORT" in adm["required"] and adm["routes"]["FILE_REPORT"] == "L2"
    assert "ESCALATE_TO_ANALYST" in adm["required"]                                            # R8: uncertain and exposure > $500
    file_, reason = policy.sar_required(_sc(0.55, ["customer"], [], denial=True, exposure=1000.01))
    assert file_ and reason.startswith("R2 / 3a")
    file_, reason = policy.sar_required(_sc(0.55, ["customer"], [], denial=True, exposure=1000.0))
    assert not file_ and reason.startswith("R2 / 3a not met")
    assert policy.sar_required(_sc(0.55, ["memory"], [], exposure=5000.0))[0] is False           # no denial: uncertain never files


def test_r7_is_the_only_exception():
    sc = _sc(0.2, ["customer"], ["history"], denial=True, recurring_match=True, channel="in_person")
    assert not policy.r2_denial(sc) and not policy.settled_pre(sc)
    adm = policy.admissible(DISP, sc, "initial")
    assert "BLOCK_CARD" in adm["forbidden"] and "BLOCK_CARD" not in adm["required"]
    assert {"CREATE_CASE", "VERIFY_WITH_CUSTOMER", "WARN_CUSTOMER"} <= set(adm["required"])
    assert voi.should_ask(DISP, sc, "customer_validation")[0]                                  # R7 re-asks about the recurrence
    withdrawn = _sc(0.1, ["customer"], ["history"], verdict="legitimate", denial=True, outcome="confirm", asked=True)
    assert not policy.r2_denial(withdrawn)                                                      # a withdrawn dispute is not a denial


def test_no_denial_keeps_r1():
    adm = policy.admissible(CTX, _sc(0.55, ["memory"], [], channel="in_person"), "initial")
    assert "R1" in adm["fired"] and "BLOCK_CARD" in adm["forbidden"] and "BLOCK_CARD" not in adm["required"]


def test_uncertain_block_invariant_allows_only_an_r2_block():
    r2 = [{"action": "CREATE_CASE", "reason": "3a"}, {"action": "BLOCK_CARD", "reason": "R2: customer denied the transaction"}]
    assert not validator.uncertain_block_violation(r2)
    assert validator.uncertain_block_violation([{"action": "BLOCK_CARD", "reason": "§3b: fraud verdict"}])
    assert validator.uncertain_block_violation([{"action": "BLOCK_ALL_CARDS", "reason": "R2"}])
    ans = {"case": {"verdict": "uncertain", "affected_txn_ids": ["1"], "exposure_usd": 10.0, "pattern": "out_of_region_use", "pattern_description": "",
                    "status": "open"},
           "next_best_actions": {"initial": r2, "final": r2, "what_changed": "nothing"},
           "sar": {"file": False, "reason": "R2 / 3a not met", "narrative": "", "subjects": [], "total_amount_usd": 0, "activity_dates": []},
           "evidence_requests": []}
    answer_writer.assert_invariants(ans)
    ans["next_best_actions"]["initial"] = ans["next_best_actions"]["final"] = [{"action": "BLOCK_CARD", "reason": "§3b"}]
    with pytest.raises(AssertionError):
        answer_writer.assert_invariants(ans)


# ----------------------------------------------------------------------------- D-REASON: citations are README rule ids
def test_citations_and_fired_name_readme_rules_only():
    scs = [(CTX, _sc(0.45, ["memory"])), (CTX, _sc(0.8, ["memory"], verdict="fraud")), (CTX, _sc(0.9, ["memory", "history"], verdict="fraud")),
           (CTX, _sc(0.05, [], ["history"], verdict="legitimate")), (CTX, _sc(0.05, [], ["history", "memory"], verdict="legitimate", outcome="confirm", asked=True)),
           (CTX, _sc(0.55, ["memory"], ["history"], outcome="no_reply", asked=True, exposure=900.0)), (CTX, _sc(0.2, [])),
           (DISP, _sc(0.55, ["customer"], [], denial=True)), (DISP, _sc(0.9, ["customer", "memory"], verdict="fraud", denial=True, exposure=1500.0)),
           (DISP, _sc(0.2, ["customer"], ["history"], denial=True, recurring_match=True))]
    for ctx, sc in scs:
        for stage in ("initial", "final"):
            adm = policy.admissible(ctx, sc, stage)
            labels = set(adm["fired"]) | {x for k, v in adm["citations"].items() if not k.startswith("_") for x in v}
            labels |= {x for v in adm["citations"]["_forbidden"].values() for x in v}
            assert labels and all(RULE_ID.match(x) for x in labels), (stage, sorted(labels))
            for a in answer_writer.reasons_for(adm["required"], adm, ctx, sc, stage):
                assert re.search(r"\bR([1-9]|10)\b|§\d|\b3a\b", a["reason"]) and not INTERNAL.search(a["reason"]), a
                assert reason_problems([a]) == [], a                                 # the agent's reason validator agrees


def test_engine_cite_labels_pass_the_agent_citation_path_unchanged():
    """Every `cite` label the policy YAML can emit (rule or add_if branch) passes the agent's policy_cite through unchanged
    and satisfies agent.validator.reason_problems, so the engine's rule ids and the agent's reason check agree."""
    from types import SimpleNamespace

    from agent.phase_machine import policy_cite
    rules = policy.params()["rules"]
    labels = {policy.cite(n) for n in rules} | {policy.cite(n, b) for n, r in rules.items() for b in r.get("add_if", [])}
    assert {"3a", "§3b", "§6", "R2", "R4", "R8"} <= labels
    sc = SimpleNamespace(p_engine=0.55, families_fraud=["customer"], flags={"denial": True})
    for label in sorted(labels):
        assert RULE_ID.match(label), label
        assert policy_cite("MONITOR_CARD", [label], sc) == label, label
        assert reason_problems([{"action": "MONITOR_CARD", "reason": f"{label}: reason text"}]) == [], label


# ----------------------------------------------------------------------------- the exam pack
@pytest.fixture(scope="module")
def runs():
    from engine.facts_duckdb import DuckFacts
    from engine.run_cases import contexts, run_one
    fx = DuckFacts()
    return {ctx.case_id: (ctx, run_one(ctx, fx)) for ctx in contexts(fx)}


@needs_db
def test_every_exam_denial_blocks_from_intake(runs):
    for cid, (ctx, r) in runs.items():
        if ctx.trigger_type != "customer_report":
            continue
        a = r["answer"]
        nba = a["next_best_actions"]
        for stage in ("initial", "final"):
            blk = [x for x in nba[stage] if x["action"] == "BLOCK_CARD"]
            assert blk and blk[0]["route"] == "L1" and blk[0]["reason"].startswith("R2"), (cid, stage)
            assert "CREATE_CASE" in [x["action"] for x in nba[stage]], (cid, stage)
        assert not any(q["type"] == "customer_validation" for q in a["evidence_requests"]), cid
        assert r["errors"] == [] and r["violations"] == [], (cid, r["errors"], r["violations"])
    for cid in ("HHG-003", "HHG-018"):                                                          # in person: settled by the denial
        a = runs[cid][1]["answer"]
        assert a["evidence_requests"] == [] and a["next_best_actions"]["what_changed"] == "nothing" and a["stop_reason"].startswith("Policy §6 test 2")
        assert a["case"]["verdict"] == "uncertain" and a["case"]["fraud_probability"] == 0.55                     # R2 moves actions, not p
    assert runs["HHG-003"][1]["answer"]["case"]["status"] == "open" and runs["HHG-018"][1]["answer"]["case"]["status"] == "escalated"
    for cid in ("HHG-004", "HHG-011"):                                                          # online: step-up, block awaits L1
        a = runs[cid][1]["answer"]
        assert [q["type"] for q in a["evidence_requests"]] == ["step_up_auth"] and a["case"]["verdict"] == "uncertain", cid


@needs_db
def test_exam_reasons_cite_readme_rules(runs):
    for cid, (_ctx, r) in runs.items():
        for stage in ("initial", "final"):
            for a in r["answer"]["next_best_actions"][stage]:
                assert re.search(r"\bR([1-9]|10)\b|§\d|\b3a\b", a["reason"]) and not INTERNAL.search(a["reason"]), (cid, a)
            assert reason_problems(r["answer"]["next_best_actions"][stage]) == [], (cid, stage)


@needs_db
def test_out_of_region_label_follows_the_banks_convention():
    """D-LABEL (no rule change): the bank's closed cases label card-present fraud away from the card's home region
    out_of_region_use even when the region is well known to the card — 581 of 955 confirmed out_of_region_use cases
    had every transaction in a region the card had used at least 5 times before; only 173 touched a never-used region."""
    from engine.facts_duckdb import DuckFacts
    d = DuckFacts().q("""SELECT count(*) n, sum(CASE WHEN k THEN 1 ELSE 0 END) known, sum(CASE WHEN z THEN 1 ELSE 0 END) new_region FROM (
                           SELECT c.case_id, bool_and(t.prior_in_region >= 5) k, bool_or(t.prior_in_region = 0) z
                           FROM cc c JOIN cc_txn x USING(case_id) JOIN txc t ON t.TransactionID = x.TransactionID
                           WHERE c.outcome = 'confirmed_fraud' AND c.pattern = 'out_of_region_use' GROUP BY 1)""").iloc[0]
    assert (int(d.n), int(d.known), int(d.new_region)) == (955, 581, 173)
