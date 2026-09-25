"""Unit tests for engine/ (PLAN §4.10 item 1). Run: pytest tests/unit/test_engine.py -q

Needs the facts DB (`python -m engine.facts_from_etl --etl data/hhgoa.duckdb --out data/hhgoa_engine.duckdb`, or
ENGINE_FACTS_DB) and the ETL calibrator data/models/isotonic.json (D7). Skipped when the facts DB is absent (CI, M10).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from engine import answer_writer, config, episode, pattern_rule, policy, scorecard, simulator, status, validator
from engine.facts_duckdb import DuckFacts
from engine.run_cases import contexts, run_one
from engine.types import CaseContext, Scorecard

pytestmark = pytest.mark.skipif(not Path(config.FACTS_DB).exists(), reason=f"needs the facts DB ({config.FACTS_DB}); run engine.facts_from_etl")


@pytest.fixture(scope="module")
def fx():
    return DuckFacts()


@pytest.fixture(scope="module")
def runs(fx):
    return {ctx.case_id: (ctx, run_one(ctx, fx)) for ctx in contexts(fx)}


# ----------------------------------------------------------------------------- ring / burst facts
def test_ring_profile_is_strong_and_wave_counts(fx):
    d = fx.device_row(config.RING_PROFILE)
    assert d["is_strong"] and d["n_cards_alltime"] == 52 and d["n_fraud_cases"] == 4
    rp = fx.ring_profile({"id": "C13487-K1"}, "2016-11-22 20:11:00")
    assert rp["ring_id"] == config.RING_PROFILE
    assert len(rp["wave_cards"]) == 27 and len(rp["pre_open_cards"]) == 19 and rp["n_cards_alltime"] == 52   # D6
    assert [t["id"] for t in rp["card_txns_on_ring"]] == ["3460634", "3478561"]
    assert [c["id"] for c in rp["closed_cases"]] == ["CC-2649", "CC-2971", "CC-2985", "CC-3035"]
    dn = fx.device_neighbors({"id": config.RING_PROFILE}, "2016-07-02 00:00:00", "2016-12-31 23:59:59", 60)
    assert len(dn["cards"]) == 52


def test_burst_fires_on_the_twelve_novdec_cards(fx):
    cards = fx.q("SELECT DISTINCT card_id FROM txc WHERE burst_id <> '' AND ts >= '2016-11-01'").card_id.tolist()
    assert len(cards) == 12 and "C07297-K1" in cards
    ub = fx.under_threshold_burst({"id": "C07297-K1"}, "2016-11-22 02:30:00")
    assert ub["bursts"][0]["ids"] == ["3476602", "3476633", "3476665", "3476682"]
    assert round(sum(ub["bursts"][0]["amts"]), 2) == 1906.07 and len(ub["lookalike_cards"]) >= 11


def test_generic_profiles_are_not_strong(fx):
    for dev in ("NULL | NULL | chrome 66.0 | NULL", "Windows | Windows 10 | chrome 63.0 | 1920x1080", "iOS Device | iOS 9.3.5 | mobile safari 9.0 | 1024x768"):
        assert not fx.device_row(dev)["is_strong"]


def test_region_history_follows_the_gsql_rule(fx):
    """D8: hint home = modal region; known = >= 3 distinct days or >= 5 txns; rare = 1..4; new = 0; any channel counts."""
    rh = fx.region_history({"id": "C13487-K1"}, "272.0", "2016-11-22 20:10:59")
    assert rh["modal_region"] == "272.0" and rh["hint"] == "home"
    assert set(rh["home_activity_48h"]) == {"n_home", "n_other"}
    rows = fx.q("""SELECT addr1, count(*) n, count(DISTINCT ts::DATE) d FROM txc WHERE card_id='C13487-K1' AND addr1<>'' AND ts <= '2016-11-22 20:10:59'
                   GROUP BY 1 ORDER BY n""")
    for r in rows.itertuples():
        want = "home" if r.addr1 == rh["modal_region"] else "known" if (r.d >= 3 or r.n >= 5) else "rare"
        assert fx.region_history({"id": "C13487-K1"}, r.addr1, "2016-11-22 20:10:59")["hint"] == want, r
    assert fx.region_history({"id": "C13487-K1"}, "999.5", "2016-11-22 20:10:59")["hint"] == "new"


def test_calibrator_is_the_etl_isotonic(fx):
    assert config.CALIBRATION_JSON.name == "isotonic.json" and config.CALIBRATION_JSON.exists()
    ys = [scorecard.calibrate(x) for x in (0.0, 0.05, 0.3, 0.7, 0.99)]
    assert ys == sorted(ys) and 0 <= ys[0] and ys[-1] <= 1 and scorecard.calibrate(-1) == 0.05


def test_r5_is_defensive_to_the_live_list_shape():
    """M7: card_testing_check.run may arrive as a one-element list (GroupByAccum) — never a TypeError."""
    txn = {"id": "1", "ts": "2016-11-01 00:00:00"}
    run = {"ids": ["7", "8", "9"], "amts": [1.0, 2.0, 3.0], "start_ts": "2016-10-31 23:00:00", "end_ts": "2016-10-31 23:30:00"}
    assert scorecard._r5({"card_testing_check": {"run": [run], "cleared_over_big": True}}, txn)["r5_run"] is True
    assert scorecard._r5({"card_testing_check": {"run": []}}, txn)["r5_run"] is False
    assert scorecard._r5({"card_testing_check": {"run": {}}}, txn)["r5_run"] is False


# ----------------------------------------------------------------------------- pattern rule
def test_pattern_rule_replays_the_closed_case_convention(fx):
    """Replay the 4,665 confirmed-fraud cases through pattern_rule.label on their TRUE members (target >= 95 %)."""
    df = fx.q("""SELECT c.case_id, c.pattern, c.card_id, c.opened_at, t.id, t.channel, t.addr1, t.amt, t.device_new, t.device_id, t.burst_id
                 FROM cc c JOIN cc_txn x USING(case_id) JOIN txc t USING(TransactionID) WHERE c.outcome='confirmed_fraud' ORDER BY c.case_id, t.ts""")
    modal = fx.q("""SELECT c.case_id, arg_max(t.addr1, n) modal FROM cc c JOIN (
                      SELECT card_id, addr1, ts, count(*) OVER (PARTITION BY card_id, addr1 ORDER BY ts) n FROM txc WHERE channel='in_person' AND addr1<>'') t
                      ON t.card_id=c.card_id AND t.ts <= c.opened_at GROUP BY 1""")
    modal = dict(zip(modal.case_id, modal.modal))
    ok = tot = 0
    conf: dict = {}
    for cid, g in df.groupby("case_id", sort=False):
        rows = g.to_dict("records")
        flags = {"ring_hit": any(r["device_id"] == config.RING_PROFILE for r in rows), "burst_hit": any(r["burst_id"] for r in rows)}
        lab = pattern_rule.label(rows, modal.get(cid, ""), flags)
        truth = g.pattern.iloc[0]
        tot += 1
        ok += lab == truth
        conf[(truth, lab)] = conf.get((truth, lab), 0) + 1
    acc = ok / tot
    print(f"pattern rule on true members: {ok}/{tot} = {acc:.4f}", {k: v for k, v in conf.items() if k[0] != k[1]})
    assert acc >= 0.95


def test_pattern_rule_basics():
    assert pattern_rule.label([], "1.0", {}) == "none"
    assert pattern_rule.label([{"channel": "online", "amt": 10, "device_new": "New", "addr1": ""}], "1.0", {"ring_hit": True}) == "undocumented"
    assert pattern_rule.label([{"channel": "online", "amt": 10, "device_new": "", "addr1": ""}, {"channel": "in_person", "amt": 10, "device_new": "", "addr1": "1.0"}], "1.0", {}) == "account_takeover"
    assert pattern_rule.label([{"channel": "in_person", "amt": 10, "device_new": "", "addr1": "1.0"}], "1.0", {}) == "account_takeover"
    assert pattern_rule.label([{"channel": "in_person", "amt": 10, "device_new": "", "addr1": "2.0"}], "1.0", {}) == "out_of_region_use"
    assert pattern_rule.label([{"channel": "online", "amt": 10, "device_new": "New", "addr1": ""}], "1.0", {}) == "card_not_present_new_device"
    assert pattern_rule.label([{"channel": "online", "amt": 10, "device_new": "Found", "addr1": ""}], "1.0", {}) == "card_not_present_fraud"
    small = [{"id": str(i), "ts": f"2016-11-01 00:0{i}:00", "channel": "online", "amt": 2.0, "device_new": "", "addr1": ""} for i in range(5)]
    assert pattern_rule.label(small, "1.0", {}) == "card_testing"


# ----------------------------------------------------------------------------- episode rule
def test_episode_rule_chain_through_members():
    chain = [{"id": "1", "ts": "2016-11-01 00:00:00", "amt": 10, "cms_p": 0.9, "sig_match": False},
             {"id": "2", "ts": "2016-11-04 00:00:00", "amt": 10, "cms_p": 0.1, "sig_match": False},
             {"id": "3", "ts": "2016-11-05 00:00:00", "amt": 10, "cms_p": 0.6, "sig_match": False},
             {"id": "4", "ts": "2016-11-05 01:00:00", "amt": 10, "cms_p": 0.0, "sig_match": True},
             {"id": "5", "ts": "2016-11-05 02:00:00", "amt": 10, "cms_p": 0.2, "sig_match": False}]
    assert episode.members(chain, "5", "fraud", {}) == ["3", "4", "5"]          # "1" is > 48 h from the next member
    assert episode.members(chain, "5", "uncertain", {}) == ["3", "5"]           # uncertain keeps cms >= 0.5 only
    assert episode.members(chain, "5", "legitimate", {}) == []
    assert episode.members(chain, "5", "fraud", {"ring_txn_ids": ["1"]}) == ["1", "3", "4", "5"]   # pattern members are exempt


# ----------------------------------------------------------------------------- policy table
def _sc(p, fams_f, fams_l=(), verdict="uncertain", exposure=100.0, **flags) -> Scorecard:
    base = {"denial": False, "outcome": "", "asked": False, "channel": "online", "ring_hit": False, "burst_hit": False,
            "shared_device_fraud": False, "shared_recipient_email": [], "r5_run": False, "recurring_match": False, "conflict": False}
    base.update(flags)
    return Scorecard(cms_p=p, cal_p=p, adjustments=[], p_engine=p, families_fraud=set(fams_f), families_legit=set(fams_l), flags=base, chain=[],
                     episode_ids=["1"], first_suspicious_txn_id="1", exposure_usd=exposure, connected_card_ids=[], connected_device_profiles=[],
                     pattern="card_not_present_fraud", pattern_description="", verdict=verdict, similar_prior_cases=[])


CTX = CaseContext("T-1", "risk_score", "", "1", "C00001-K1", "C00001", "2016-12-01 00:00:00", 0.8)
DISP = CaseContext("T-2", "customer_report", "", "1", "C00001-K1", "C00001", "2016-12-01 00:00:00", None)


def test_routes_and_ordering():
    assert policy.route("BLOCK_CARD", 2500.0) == "L1" and policy.route("BLOCK_CARD", 2500.01) == "L2"
    assert policy.route("FILE_REPORT", 1) == "L2" and policy.route("DECLINE_TRANSACTION", 1) == "L1" and policy.route("CREATE_CASE", 1) == "auto"
    acts = [{"action": "CLOSE_NO_FRAUD"}, {"action": "VERIFY_WITH_CUSTOMER"}, {"action": "CREATE_CASE"}]
    assert [a["action"] for a in policy.order(acts)] == ["CREATE_CASE", "VERIFY_WITH_CUSTOMER", "CLOSE_NO_FRAUD"]


def test_r1_single_signal_forbids_block():
    adm = policy.admissible(CTX, _sc(0.45, ["memory"]), "initial")
    assert "BLOCK_CARD" in adm["forbidden"] and "CREATE_CASE" in adm["required"]
    assert any(a in adm["required"] for a in ("VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH"))
    assert "R1" in adm["fired"]


def test_uncertain_initial_never_blocks_and_r8_is_gated():
    """D3: R8 only when uncertain AND (exposure > $500 OR conflict); conflict excludes the customer's own statement.
    Without a customer denial the uncertain band never blocks before evidence (R1 / §3b); a denial takes R2's block from
    intake instead (tests/unit/test_engine_r2.py), and R8 is gated the same way there."""
    adm = policy.admissible(CTX, _sc(0.55, ["memory"], ["history"], channel="in_person"), "initial")
    assert "BLOCK_CARD" in adm["forbidden"] and "MONITOR_CARD" in adm["required"] and "VERIFY_WITH_CUSTOMER" in adm["required"]
    adm = policy.admissible(DISP, _sc(0.55, ["customer"], ["history"], denial=True, channel="in_person"), "initial")
    assert "ESCALATE_TO_ANALYST" not in adm["required"] and "R8" not in adm["fired"]          # F' = {}, exposure 100: no R8
    adm = policy.admissible(DISP, _sc(0.55, ["customer"], ["history"], denial=True, channel="in_person", exposure=600.0), "initial")
    assert "ESCALATE_TO_ANALYST" in adm["required"] and "R8" in adm["fired"]                    # exposure > 500
    adm = policy.admissible(DISP, _sc(0.55, ["customer", "memory"], ["history"], denial=True, channel="in_person"), "initial")
    assert "ESCALATE_TO_ANALYST" in adm["required"] and adm["conditions"]["conflict"]           # memory vs history = conflict
    adm = policy.admissible(CTX, _sc(0.30, ["history"], ["memory"], conflict=True), "initial")
    assert "ESCALATE_TO_ANALYST" in adm["required"]                                             # rulebook-chain override
    sc = _sc(0.55, ["customer"], [], denial=True, outcome="inconclusive", asked=True)
    adm = policy.admissible(DISP, sc, "final")
    assert {"MONITOR_CARD", "DECLINE_TRANSACTION"} <= set(adm["required"]) and "ESCALATE_TO_ANALYST" not in adm["required"]
    assert status.derive("uncertain", [{"action": a} for a in adm["required"]], True) == "open"


def test_fraud_band_is_probability_alone_and_still_verifies():
    """D1: fraud at p >= 0.70 with one family; §3b verification in the initial set while §6 test 1 does not hold."""
    sc = _sc(0.74, ["memory"], ["history"], verdict="fraud", flagged_device_new="New")
    assert scorecard._verdict(0.74, {"memory"}, sc.flags, policy.params()["engine"]) == "fraud"
    assert scorecard._verdict(0.69, {"memory", "history"}, sc.flags, policy.params()["engine"]) == "uncertain"
    assert not policy.settled_pre(sc)
    adm = policy.admissible(CTX, sc, "initial")
    assert {"CREATE_CASE", "BLOCK_CARD"} <= set(adm["required"]) and "R1" not in adm["fired"]
    assert any(a in adm["required"] for a in ("VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH"))
    settled = _sc(0.90, ["memory", "history"], verdict="fraud")
    assert policy.settled_pre(settled)
    adm = policy.admissible(CTX, settled, "initial")
    assert not any(a in adm["required"] for a in ("VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH"))


def test_confirmation_never_clears_ring_or_burst():
    """D10: R3 does not fire on a ring / burst hit; the floor is re-applied and the verdict stays fraud."""
    sc = _sc(0.90, ["device", "memory"], [], verdict="fraud", ring_hit=True, ring_id=config.RING_PROFILE, wave_cards=[], pre_open_cards=[],
             ring_cases=[], ring_rows=[], ring_txn_ids=[], floor=0.90, flagged_txn_id="1", flagged_ts="2016-11-22 00:00:00")
    post = scorecard.post_evidence(sc, "confirm")
    assert post.verdict == "fraud" and post.p_engine >= 0.90
    adm = policy.admissible(CTX, post, "final")
    assert "CLOSE_NO_FRAUD" not in adm["required"] and "BLOCK_CARD" in adm["required"] and "R3" not in adm["fired"]
    plain = scorecard.post_evidence(_sc(0.55, ["memory"], ["history"], flagged_txn_id="1", flagged_ts="2016-11-22 00:00:00"), "pass")
    assert plain.verdict == "legitimate" and plain.p_engine <= 0.15


def test_r2_after_denial_blocks_and_r7_never_blocks():
    adm = policy.admissible(DISP, _sc(0.9, ["customer", "memory"], verdict="fraud", denial=True), "initial")
    assert {"CREATE_CASE", "BLOCK_CARD"} <= set(adm["required"]) and "FILE_REPORT" not in adm["required"]
    adm = policy.admissible(DISP, _sc(0.9, ["customer", "memory"], verdict="fraud", denial=True, exposure=1500.0), "initial")
    assert "FILE_REPORT" in adm["required"] and adm["routes"]["FILE_REPORT"] == "L2"
    adm = policy.admissible(DISP, _sc(0.2, ["customer"], ["history"], denial=True, recurring_match=True), "initial")
    assert "BLOCK_CARD" in adm["forbidden"] and {"VERIFY_WITH_CUSTOMER", "WARN_CUSTOMER", "CREATE_CASE"} <= set(adm["required"])


def test_r3_r4_finals():
    sc = _sc(0.05, [], ["history", "memory"], verdict="legitimate", outcome="confirm", asked=True)
    adm = policy.admissible(CTX, sc, "final")
    assert {"CLOSE_NO_FRAUD", "ALLOW_TRANSACTION"} <= set(adm["required"]) and "BLOCK_CARD" in adm["forbidden"]
    sc = _sc(0.55, ["customer"], ["history"], outcome="no_reply", asked=True, denial=True, exposure=600.0)
    adm = policy.admissible(DISP, sc, "final")
    assert {"MONITOR_CARD", "DECLINE_TRANSACTION", "ESCALATE_TO_ANALYST"} <= set(adm["required"])


def test_r10_forbids_block_all_cards():
    v = policy.check([{"action": "BLOCK_ALL_CARDS", "route": "L2", "reason": "x"}], DISP, _sc(0.95, ["customer", "memory"], verdict="fraud", denial=True), "final")
    assert any("R10" in x for x in v)


def test_sar_rule_ring_and_exposure():
    assert policy.sar_required(_sc(0.5, ["customer"], exposure=5000.0))[0] is False           # not fraud
    assert policy.sar_required(_sc(0.9, ["customer", "memory"], verdict="fraud", exposure=1000.0))[0] is False
    assert policy.sar_required(_sc(0.9, ["customer", "memory"], verdict="fraud", exposure=1000.01))[0] is True
    sc = _sc(0.9, ["device", "memory"], verdict="fraud", exposure=187.33, ring_hit=True, ring_id=config.RING_PROFILE, wave_cards=list(range(27)))
    assert policy.sar_required(sc)[0] is True
    sc = _sc(0.9, ["customer", "memory"], verdict="fraud", exposure=100.0, shared_device_fraud=True, shared_fraud_cards=["C1-K1", "C2-K1"])
    assert policy.sar_required(sc)[0] is False                                                # replay: 0/9 such cases were filed


# ----------------------------------------------------------------------------- simulator / status
def test_simulator_is_deterministic_and_evidence_driven():
    """D5 (sim_hybrid): F' / L' exclude the customer's own statement; p_pre < 0.5 gates a confirmation."""
    sc = _sc(0.3, [], ["history", "memory"], channel="in_person", flagged_addr1="444.0", flagged_amt=77.07)
    a, b = simulator.reply("customer_validation", CTX, sc), simulator.reply("customer_validation", CTX, sc)
    assert a == b and a["outcome"] == "confirm" and "444.0" in a["assumed_response"]
    assert simulator.reply("customer_validation", CTX, _sc(0.3, [], ["history"]))["outcome"] == "confirm"              # L' >= 1, F' 0, p < 0.5
    assert simulator.reply("customer_validation", CTX, _sc(0.6, [], ["history"]))["outcome"] == "no_reply"             # p_pre >= 0.5: no confirm
    assert simulator.reply("customer_validation", CTX, _sc(0.8, ["history", "memory"]))["outcome"] == "deny"           # F' >= 2
    assert simulator.reply("customer_validation", CTX, _sc(0.5, ["memory"], ["history"]))["outcome"] == "no_reply"
    assert simulator.reply("customer_validation", CTX, _sc(0.3, [], []))["outcome"] == "no_reply"                      # L' 0
    assert simulator.reply("step_up_auth", CTX, _sc(0.6, ["memory"], [], device_known=True))["outcome"] == "pass"     # known device, F' <= 1
    assert simulator.reply("step_up_auth", CTX, _sc(0.6, ["memory"], [], device_known=False))["outcome"] == "inconclusive"
    assert simulator.reply("step_up_auth", CTX, _sc(0.8, ["memory", "history"], [], device_known=True))["outcome"] == "fail"
    # disputes never pass / confirm without R7
    assert simulator.reply("step_up_auth", DISP, _sc(0.55, ["customer"], [], denial=True, device_known=True))["outcome"] == "inconclusive"
    assert simulator.reply("step_up_auth", DISP, _sc(0.7, ["customer", "memory"], [], denial=True))["outcome"] == "inconclusive"   # F' = 1
    assert simulator.reply("step_up_auth", DISP, _sc(0.8, ["customer", "memory", "device"], [], denial=True))["outcome"] == "fail"  # F' = 2
    assert simulator.reply("customer_validation", DISP, _sc(0.55, ["customer"], ["history"], denial=True, channel="in_person"))["outcome"] == "no_reply"
    assert simulator.reply("customer_validation", DISP, _sc(0.2, ["customer"], ["history"], denial=True, recurring_match=True, flagged_amt=9.99))["outcome"] == "confirm"


def test_answer_invariants_d10():
    good = {"case": {"verdict": "fraud", "affected_txn_ids": ["1"], "exposure_usd": 10.0, "pattern": "card_not_present_fraud", "pattern_description": "", "status": "closed_fraud"},
            "next_best_actions": {"initial": [{"action": "CREATE_CASE"}, {"action": "BLOCK_CARD"}], "final": [{"action": "CREATE_CASE"}, {"action": "BLOCK_CARD"}], "what_changed": "nothing"},
            "sar": {"file": False, "reason": "3a not met", "narrative": "", "subjects": [], "total_amount_usd": 0, "activity_dates": []}, "evidence_requests": []}
    answer_writer.assert_invariants(good)
    bad = copy.deepcopy(good)
    bad["next_best_actions"]["final"] = [{"action": "CREATE_CASE"}, {"action": "CLOSE_NO_FRAUD"}]
    bad["next_best_actions"]["initial"] = bad["next_best_actions"]["final"]
    with pytest.raises(AssertionError):
        answer_writer.assert_invariants(bad)
    bad = copy.deepcopy(good)
    bad["next_best_actions"]["final"] = [{"action": "CREATE_CASE"}, {"action": "MONITOR_CARD"}]
    bad["next_best_actions"]["initial"] = bad["next_best_actions"]["final"]
    with pytest.raises(AssertionError):
        answer_writer.assert_invariants(bad)


def test_status_rule():
    assert status.derive("uncertain", [{"action": "ESCALATE_TO_ANALYST"}], False) == "escalated"
    assert status.derive("uncertain", [{"action": "MONITOR_CARD"}], True) == "open"
    assert status.derive("fraud", [{"action": "BLOCK_CARD"}], False) == "closed_fraud"
    assert status.derive("legitimate", [{"action": "CLOSE_NO_FRAUD"}], False) == "closed_legitimate"


# ----------------------------------------------------------------------------- end to end on the pack
def test_twenty_drafts_validate(runs):
    for cid, (_ctx, r) in runs.items():
        assert r["errors"] == [], (cid, r["errors"])
        assert r["violations"] == [], (cid, r["violations"])


def test_seeded_cases(runs):
    a = runs["HHG-014"][1]["answer"]
    assert a["case"]["pattern"] == "undocumented" and a["sar"]["file"] and a["case"]["exposure_usd"] == 187.33
    assert a["case"]["affected_txn_ids"] == ["3460634", "3478561"] and len(a["case"]["connected_card_ids"]) == 27
    assert a["case"]["connected_device_profiles"] == [config.RING_PROFILE] and a["evidence_requests"] == []
    assert a["next_best_actions"]["what_changed"] == "nothing" and a["case"]["status"] == "escalated"
    b = runs["HHG-006"][1]["answer"]
    assert b["case"]["pattern"] == "undocumented" and b["sar"]["file"] and b["case"]["exposure_usd"] == 1906.07 and b["case"]["connected_card_ids"] == []


def test_decision_effects_on_the_exam(runs):
    """D1 / D3 / D4 pins: HHG-019 fraud with a §3b step-up and an L1 block in initial; 001 / 004 initial without R8; 016 fraud pre-evidence."""
    a = runs["HHG-019"][1]["answer"]
    ini = a["next_best_actions"]["initial"]
    assert a["case"]["verdict"] == "fraud" and [x["action"] for x in ini] == ["CREATE_CASE", "STEP_UP_AUTH", "BLOCK_CARD"]
    assert next(x for x in ini if x["action"] == "STEP_UP_AUTH")["reason"].startswith("§3b") and next(x for x in ini if x["action"] == "BLOCK_CARD")["route"] == "L1"
    for cid in ("HHG-001", "HHG-004"):
        assert "ESCALATE_TO_ANALYST" not in [x["action"] for x in runs[cid][1]["answer"]["next_best_actions"]["initial"]], cid
    assert runs["HHG-004"][1]["answer"]["case"]["status"] == "open" and runs["HHG-003"][1]["answer"]["case"]["status"] == "open"
    assert runs["HHG-015"][1]["answer"]["case"]["status"] == "escalated" and runs["HHG-018"][1]["answer"]["case"]["status"] == "escalated"
    c = runs["HHG-016"][1]["answer"]
    assert c["case"]["verdict"] == "fraud" and "BLOCK_CARD" in [x["action"] for x in c["next_best_actions"]["initial"]]
    for cid, (_ctx, r) in runs.items():
        fin = [x["action"] for x in r["answer"]["next_best_actions"]["final"]]
        if r["answer"]["case"]["verdict"] == "fraud":
            assert not ({"CLOSE_NO_FRAUD", "ALLOW_TRANSACTION"} & set(fin)) and (answer_writer.FRAUD_CONTAINMENT & set(fin)), cid


def test_no_seed_tell_and_no_ac_ids(runs):
    for cid, (_ctx, r) in runs.items():
        txt = json.dumps(r["answer"])
        assert not validator.SEED_TELL.search(txt), cid
        for ev in r["answer"]["case"]["evidence"]:
            assert not any(e.startswith("AC-") for e in ev["entity_ids"]), cid


def test_validator_rejects_bad_files(fx, runs):
    good = copy.deepcopy(runs["HHG-014"][1]["answer"])
    assert validator.validate(good, fx) == []
    bad = copy.deepcopy(good)
    bad["case"]["affected_txn_ids"].append("9999999")
    assert any("ids not in dataset" in e for e in validator.validate(bad, fx))
    bad = copy.deepcopy(good)
    bad["case"]["evidence"][0]["entity_ids"].append("AC-HHG-006")
    assert any("AC-" in e for e in validator.validate(bad, fx))
    bad = copy.deepcopy(good)
    bad["sar"]["file"] = False
    assert any("sar.file" in e for e in validator.validate(bad, fx))
    bad = copy.deepcopy(good)
    bad["case"]["summary"] += " The seeded rows have :00 seconds."
    assert any("seed tell" in e for e in validator.validate(bad, fx))
