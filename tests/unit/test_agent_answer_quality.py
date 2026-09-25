"""Agent-side answer-quality fixes (audit D-SAR, D-REASON, D-MISC, D-HHG015 wording): SAR fact checks V14-V19,
rule citations, summary length, what_changed wording, analyst source label, memory time-box, run manifests.

Every fixture is recorded text (the defective narratives of the first live run are quoted verbatim where they
show a defect); nothing here touches the TigerGraph workspace or an LLM.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import mcp_client
from agent.phase_machine import PhaseMachine, policy_cite
from agent.validator import answer_text_problems, reason_problems, summary_problems, what_changed_problems
from rag.retrieval import similar_prior_cases, timebox_cases
from rag.validate_sar import (
    answer_fact_context,
    check_answer_sar_facts,
    check_sar_facts,
    reported_cases_from_evidence,
    validate_sar,
)

RING = "SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080"
TRIDENT = "Trident/7.0 | Windows 7 | ie 11.0 for desktop | 1920x1080"
IOS = "iOS Device | iOS 11.2.1 | mobile safari 11.0 | 2208x1242"
FINAL_014 = [{"action": "CREATE_CASE", "route": "auto"}, {"action": "MONITOR_CONNECTED_CARDS", "route": "auto"},
             {"action": "BLOCK_CARD", "route": "L1"}, {"action": "FILE_REPORT", "route": "L2"},
             {"action": "ESCALATE_TO_ANALYST", "route": "auto"}]
RELATED_014 = ["CC-2649", "CC-2971", "CC-2985", "CC-3035"]
BASE_014 = {"n_prior_txns": 71, "max_amt": 225.94, "median_amt": 55.92}


def sar_of(narrative: str, subjects: list[str]) -> dict:
    return {"file": True, "reason": "3a / R6 / R9", "narrative": narrative, "subjects": subjects,
            "total_amount_usd": 187.33, "activity_dates": ["2016-11-15", "2016-11-22"]}


def codes(errs: list[str]) -> set[str]:
    return {e.split(" ", 1)[0] for e in errs}


# ------------------------------------------------------------------------------------------------ V14 pending block

# first live run, HHG-014 / HHG-006 (verbatim): the block is only an L1 recommendation
LIVE_014_ACTIONS = ("Based on this evidence the bank opened case AC-HHG-014, blocked card C13487-K1, placed all 27 connected "
                    "cards under monitoring for further activity on the shared device profile, and escalated the matter to an analyst.")
LIVE_006_ACTIONS = "The bank opened case AC-HHG-006, blocked card C07297-K1, and is escalating the broader under-threshold burst pattern."


@pytest.mark.parametrize("text", [
    LIVE_014_ACTIONS,
    LIVE_006_ACTIONS,
    "The card C13487-K1 has been blocked and the connected cards are monitored.",
    "Card C13487-K1 blocked and scheduled for reissue.",
    "The card was closed on 2016-11-22.",
])
def test_v14_block_written_as_done_is_caught(text):
    errs = check_sar_facts(sar_of(text, ["C13487", "C13487-K1"]), final_actions=FINAL_014)
    assert "V14" in codes(errs), errs
    assert "BLOCK_CARD (L1)" in next(e for e in errs if e.startswith("V14"))


@pytest.mark.parametrize("text", [
    "A block of card C13487-K1 is recommended and awaits team-lead approval; the connected cards have been placed under monitoring.",
    "Actions: an internal fraud case, block and reissue of the card (team-lead approval requested), this filing.",
    "The bank recommends that card C13487-K1 be blocked; the card has not been blocked yet.",
    "Four closed cases on the profile (CC-2649, CC-2971, CC-2985, CC-3035), in which each card was blocked, were confirmed fraud.",
    "The case was closed as confirmed fraud in closed case CC-2649.",
])
def test_v14_recommended_block_and_history_are_not_flagged(text):
    assert "V14" not in codes(check_sar_facts(sar_of(text, ["C13487", "C13487-K1"]), final_actions=FINAL_014))


def test_v14_block_absent_from_final_is_also_caught():
    errs = check_sar_facts(sar_of("The bank blocked the card.", ["C13487"]), final_actions=[{"action": "CREATE_CASE", "route": "auto"}])
    assert any(e.startswith("V14") and "not among the final actions" in e for e in errs)


# ------------------------------------------------------------------------------------------------ V15 / V16

def test_v15_gendered_pronoun():
    live = "The customer disputed this transaction, stating she never made it."      # HHG-006 live narrative
    assert "V15" in codes(check_sar_facts(sar_of(live, ["C07297"])))
    for ok in ("The customer disputed this transaction, stating they never made it.", "The shell of the scheme is the device.",
               "The cardholder's history shows the three purchases."):
        assert "V15" not in codes(check_sar_facts(sar_of(ok, ["C07297"])))


def test_v16_time_zone():
    live = "On 2016-11-15 at 20:30 UTC, card C13487-K1 (transaction 3460634) was used online for $112.37."   # HHG-014 live
    assert "V16" in codes(check_sar_facts(sar_of(live, ["C13487-K1"])))
    ok = "On 2016-11-15 at 20:30, card C13487-K1 (transaction 3460634) was used online for $112.37 per the SDN LIST screening."
    assert "V16" not in codes(check_sar_facts(sar_of(ok, ["C13487-K1"])))


# ------------------------------------------------------------------------------------------------ V17 subjects

def test_v17_named_devices_and_cards_must_be_subjects():
    nar = (f"On 2016-11-21 at 20:00 a $478.95 purchase came from device profile {TRIDENT} and at 20:10 a $456.96 purchase "
           f"from device profile {IOS} on card C07297-K1 of customer C07297.")
    errs = check_sar_facts(sar_of(nar, ["C07297", "C07297-K1"]), known_devices=[TRIDENT, IOS])   # HHG-006 live subjects
    assert "V17" in codes(errs) and TRIDENT in errs[0] and IOS in errs[0]
    assert "V17" not in codes(check_sar_facts(sar_of(nar, ["C07297", "C07297-K1", TRIDENT, IOS]), known_devices=[TRIDENT, IOS]))
    # a card id in the narrative is a subject too
    assert "V17" in codes(check_sar_facts(sar_of(nar + " Card C06650-K2 shares it.", ["C07297", "C07297-K1", TRIDENT, IOS])))


# ------------------------------------------------------------------------------------------------ V18 prior reports

LIVE_014_PRIOR = ("No prior Suspicious Activity Report has been filed on this card or in connection with this device profile, "
                  "and OFAC screening returned no OFAC/SDN match for any subject.")


def test_v18_no_prior_report_claim_contradicting_the_evidence():
    errs = check_sar_facts(sar_of(LIVE_014_PRIOR, ["C13487"]), prior_reports_related=RELATED_014)
    assert "V18" in codes(errs) and "CC-2649" in next(e for e in errs if e.startswith("V18"))
    # a bare claim covers every scope
    assert "V18" in codes(check_sar_facts(sar_of("No prior report exists.", ["C13487"]), prior_reports_related=RELATED_014))
    # card reports contradict a card-scoped claim
    assert "V18" in codes(check_sar_facts(sar_of("No prior SAR has been filed on this card.", ["C13487"]), prior_reports_card=["CC-0141"]))


@pytest.mark.parametrize("text", [
    "No prior suspicious activity report has been filed on this card; closed cases CC-2649 and CC-2971 on the same device profile were reported.",
    "No prior suspicious activity report has been filed on this card, and OFAC screening returned no match.",
    "Reports were filed on the four closed cases on this device profile; none on this card before.",
])
def test_v18_scoped_or_true_claims_pass(text):
    assert "V18" not in codes(check_sar_facts(sar_of(text, ["C13487"]), prior_reports_related=RELATED_014))


def test_v18_without_reports_any_claim_passes():
    assert check_sar_facts(sar_of(LIVE_014_PRIOR, ["C13487"])) == []


def test_reported_cases_from_evidence_reads_the_hhg014_claim():
    ev = [{"claim": "ring_profile lists four closed cases on this exact profile - CC-2649, CC-2971, CC-2985, CC-3035 - all "
                    "confirmed_fraud with pattern undocumented and reports filed, on cards C03528-K1, C09998-K1.",
           "source": "graph", "ref": "query:ring_profile", "entity_ids": ["CC-2649"]},
          {"claim": "Card C13487-K1 has 2 earlier closed cases, none reported.", "source": "graph", "ref": "q", "entity_ids": ["CC-0001"]}]
    card, rel = reported_cases_from_evidence(ev, "C13487-K1")
    assert card == [] and rel == RELATED_014


# ------------------------------------------------------------------------------------------------ V19 as-of statistics

LIVE_014_BASELINE = ("This activity is unusual for the cardholder because the account's 85-transaction history dating to 2016-07-04 "
                     "is overwhelmingly in-person (81 of 85 transactions) in home region 272.0, carries a median amount of $48.03 "
                     "and a maximum of $252.28, and had used only two devices in total before these online purchases from a brand-new "
                     "mobile profile appeared.")          # the full live sentence: a device named in a later clause
LIVE_006_BASELINE = ("This activity is unusual for the cardholder, whose 261-transaction history since 2016-07-05 is overwhelmingly "
                     "conducted in person (255 of 261 transactions) in billing region 264.0, with a median transaction amount of $58.98 "
                     "and only six prior online transactions on two previously known devices; a same-day cluster of four online "
                     "purchases near the $500 mark on newly flagged device profiles has no precedent on this account.")


def test_v19_whole_history_statistics_are_caught():
    errs = check_sar_facts(sar_of(LIVE_014_BASELINE, ["C13487"]), baseline_as_of=BASE_014, affected_amounts=[112.37, 74.96])
    msgs = [e for e in errs if e.startswith("V19")]
    assert any("85" in m for m in msgs) and any("$252.28" in m for m in msgs) and any("$48.03" in m for m in msgs), msgs
    base_006 = {"n_prior_txns": 199, "max_amt": 1104.0, "median_amt": 62.93, "n_txns_at_opening": 201, "median_amt_at_opening": 63.04}
    msgs = [e for e in check_sar_facts(sar_of(LIVE_006_BASELINE, ["C07297"]), baseline_as_of=base_006) if e.startswith("V19")]
    assert any("261" in m for m in msgs) and any("$58.98" in m for m in msgs), msgs


@pytest.mark.parametrize("text", [
    "The card's 71 prior transactions had a median amount of $55.92 and a maximum of $225.94.",
    "At opening the card had 73 transactions in its history, with a maximum of $225.94.",
    "The ring profile appears in 114 transactions across 52 cards, far beyond this card's history.",
    "The card's 71 prior transactions had a median of $55.92, while the device profile carries 114 transactions and a maximum of $900.00.",
    "The $112.37 purchase is the largest in the episode and above the prior median of $55.92.",
    "The card's prior in-person maximum of $180.00 was never exceeded online.",
])
def test_v19_as_of_statistics_pass(text):
    errs = check_sar_facts(sar_of(text, ["C13487"]), baseline_as_of=dict(BASE_014, n_txns_at_opening=73),
                           affected_amounts=[112.37, 74.96])
    assert "V19" not in codes(errs), errs


def test_fact_checks_run_inside_validate_sar_only_when_asked():
    sar = sar_of(LIVE_014_ACTIONS + " " + LIVE_014_PRIOR, ["C13487", "C13487-K1"])
    kw = dict(exposure_usd=187.33, case_ids=("HHG-014", "AC-HHG-014"))
    assert not codes(validate_sar(sar, **kw)) & {"V14", "V18"}
    assert {"V14", "V18"} <= codes(validate_sar(sar, **kw, fact_context={"final_actions": FINAL_014, "prior_reports_related": RELATED_014}))


def test_answer_level_fact_checks_catch_the_live_hhg014_defects():
    answer = {"case_id": "HHG-014", "case": {"connected_device_profiles": [RING], "evidence": [
        {"claim": "Four closed cases on this exact profile - CC-2649, CC-2971, CC-2985, CC-3035 - confirmed fraud with reports filed.",
         "source": "graph", "ref": "query:ring_profile", "entity_ids": ["CC-2649", RING]}]},
        "next_best_actions": {"final": FINAL_014},
        "sar": sar_of(LIVE_014_ACTIONS + " On 2016-11-15 at 20:30 UTC a purchase. " + LIVE_014_PRIOR, ["C13487", "C13487-K1", RING])}
    assert {"V14", "V16", "V18"} <= codes(check_answer_sar_facts(answer, "C13487-K1"))
    assert answer_fact_context(answer)["known_devices"] == [RING]
    assert check_answer_sar_facts(dict(answer, sar={"file": False, "narrative": "", "subjects": []})) == []


# ------------------------------------------------------------------------------------------------ reasons / summary / what_changed

def test_reason_citations():
    live = [{"action": "BLOCK_CARD", "reason": "fraud_band: p=0.90 on this shared anonymous-proxy device profile"},          # HHG-014 live
            {"action": "MONITOR_CARD", "reason": "uncertain_initial: verdict is uncertain and no reply has been received yet"},  # HHG-004
            {"action": "MONITOR_CARD", "reason": "Uncertain band, step-up reply pending: monitor card C11923-K2"},               # HHG-011
            {"action": "CREATE_CASE", "reason": "3a_case: required by policy"}]
    assert len(reason_problems(live)) == 4
    good = [{"action": "BLOCK_CARD", "reason": "R2: customer denied; exposure $166.97 <= $2,500"},
            {"action": "STEP_UP_AUTH", "reason": "§3b/§5: possession check while the block awaits approval"},
            {"action": "BLOCK_CARD", "reason": "R1 not applicable (p >= 0.70): two families"},
            {"action": "ESCALATE_TO_ANALYST", "reason": "R10 does not apply; R8: uncertain and exposure > $500"}]
    assert reason_problems(good) == []


def test_policy_cite_translates_gate_labels():
    sc = SimpleNamespace(p_engine=0.90, families_fraud=["device", "memory"], flags={})
    assert policy_cite("BLOCK_CARD", ["fraud_band"], sc) == "R1 not applicable (p >= 0.70)"
    assert policy_cite("BLOCK_CARD", ["fraud_band", "R2"], SimpleNamespace(p_engine=0.9, families_fraud=[], flags={"denial": True})) == "R2"
    assert policy_cite("CREATE_CASE", ["3a_case", "R6", "R9"], sc) == "3a/R6/R9"
    assert policy_cite("MONITOR_CARD", ["uncertain_final"], sc) == "R4"
    assert policy_cite("MONITOR_CARD", ["§3b"], sc) == "§3b"             # the policy YAML's own `cite`
    low = SimpleNamespace(p_engine=0.45, families_fraud=["history"], flags={})
    assert policy_cite("VERIFY_WITH_CUSTOMER", ["uncertain_initial"], low) == "R1"
    assert policy_cite("ESCALATE_TO_ANALYST", [], sc) == "R8"
    for a, g in (("BLOCK_CARD", ["fraud_band"]), ("MONITOR_CARD", ["uncertain_initial"]), ("CLOSE_NO_FRAUD", ["legit_band"])):
        assert reason_problems([{"action": a, "reason": policy_cite(a, g, sc) + ": x"}]) == []


def test_summary_length():
    long_live = ("Verdict: fraud at probability 0.90. " * 3) + "x" * 1500          # the live summaries ran 1,400-1,950 chars
    assert summary_problems(long_live)
    assert summary_problems("One sentence only.")
    readme = ("Textbook card testing: three sub-$3 online authorizations in 40 minutes, then a $259 purchase in a category the "
              "cardholder has never used. All four share a device profile marked New for this account, which appears on a closed "
              "case from August and on another card this month. Customer denied the activity. Card compromised; a second card is "
              "likely compromised through the same device.")                 # the README's own example
    assert summary_problems(readme) == []


def test_what_changed_same_value():
    assert what_changed_problems("The assumed customer validation reply (no_reply) moved the probability from 0.62 to 0.62 and the verdict to uncertain.")
    assert what_changed_problems("Customer denial raised probability from 0.72 to 0.86 and confirmed the block.") == []
    assert what_changed_problems("nothing") == []


def _pm_stub() -> PhaseMachine:
    pm = PhaseMachine.__new__(PhaseMachine)
    return pm


@pytest.mark.parametrize("p0,p1,v0,v1,expect,forbid", [
    (0.62, 0.62, "uncertain", "uncertain", "left the probability at 0.62 and kept the verdict uncertain", "0.62 to 0.62"),
    (0.04, 0.04, "legitimate", "legitimate", "left the probability at 0.04", "0.04 to 0.04"),
    (0.30, 0.10, "uncertain", "legitimate", "moved the probability from 0.30 to 0.10 and changed the verdict from uncertain to legitimate", "kept"),
])
def test_what_changed_wording(p0, p1, v0, v1, expect, forbid):
    pm = _pm_stub()
    sc = SimpleNamespace(p_engine=p1, verdict=v1, flags={})     # post_evidence may update sc in place: p_pre is passed
    post = SimpleNamespace(p_engine=p1, verdict=v1, flags={"post_outcome": "no_reply", "counterfactual": "a confirmation would have closed the alert (R3); a denial would add BLOCK_CARD (R2)"})
    ini = [{"action": "CREATE_CASE", "route": "auto"}, {"action": "VERIFY_WITH_CUSTOMER", "route": "auto"}]
    fin = [{"action": "CREATE_CASE", "route": "auto"}, {"action": "DECLINE_TRANSACTION", "route": "L1"}]
    out = pm._what_changed({"type": "customer_validation"}, sc, post, ini, fin, p0, v0)
    assert expect in out and forbid not in out and what_changed_problems(out) == []
    assert out.count(". ") <= 2      # README: one or two sentences


def test_answer_text_problems_on_a_live_style_answer():
    answer = {"case": {"summary": "x " * 900},
              "next_best_actions": {"initial": [{"action": "MONITOR_CARD", "reason": "uncertain_initial: pending"}], "final": [],
                                    "what_changed": "moved the probability from 0.58 to 0.58"}}
    probs = answer_text_problems(answer)
    assert any("internal label" in p for p in probs) and any("summary" in p for p in probs) and any("0.58 to 0.58" in p for p in probs)


# ------------------------------------------------------------------------------------------------ memory time-box

OPENED_014 = "2016-11-22 20:11:00"
MEMORY_ROWS = {"cases": [
    {"id": "CC-2649", "kind": "closed", "outcome_or_verdict": "confirmed_fraud", "opened_at": "2016-08-19 10:00:00", "distance": 0.1},
    {"id": "AC-HHG-014", "kind": "agent", "outcome_or_verdict": "fraud", "opened_at": OPENED_014, "distance": 0.0},        # itself, earlier run
    {"id": "AC-HHG-004", "kind": "agent", "outcome_or_verdict": "uncertain", "opened_at": "2016-12-30 01:00:00", "distance": 0.2},  # future
    {"id": "AC-HHG-006", "kind": "agent", "outcome_or_verdict": "fraud", "opened_at": "2016-11-22 02:30:00", "distance": 0.3},  # earlier: kept
]}


def test_memory_timebox_drops_self_and_future_agent_cases():
    out, dropped = mcp_client.memory_timebox(MEMORY_ROWS, OPENED_014, frozenset({"AC-HHG-014"}))
    assert [r["id"] for r in out["cases"]] == ["CC-2649", "AC-HHG-006"]
    assert sorted(dropped) == ["AC-HHG-004", "AC-HHG-014"]
    raw = {"agent_cases": [{"v_id": "AC-HHG-018", "v_type": "AgentCase", "attributes": {"@opened_at": "2016-12-01 00:00:00"}}],
           "cards": ["C01289-K1"]}
    out, _ = mcp_client.memory_timebox(raw, OPENED_014)
    assert out == {"agent_cases": [], "cards": ["C01289-K1"]}
    # rows without opened_at (device_neighbors.agent_cases) are dropped by id
    out, _ = mcp_client.memory_timebox({"agent_cases": [{"id": "AC-HHG-004", "card_id": "C1"}]}, OPENED_014, frozenset({"AC-HHG-004"}))
    assert out["agent_cases"] == []


def test_harness_query_applies_the_timebox(monkeypatch):
    async def fake_run(session, name, params):
        return json.loads(json.dumps(MEMORY_ROWS))

    monkeypatch.setattr(mcp_client, "run_query", fake_run)
    tctx = mcp_client.ToolContext(opened_at=OPENED_014, exclude_case_ids=frozenset({"AC-HHG-014"}))
    res = asyncio.run(mcp_client.harness_query(None, tctx, "similar_prior_cases", {"as_of": OPENED_014}))
    assert "AC-HHG-014" not in json.dumps(res) and "AC-HHG-004" not in json.dumps(res)
    assert "AC-HHG-014" not in json.dumps(tctx.facts["similar_prior_cases"])
    out = asyncio.run(mcp_client.call_query_tool(None, tctx, "similar_prior_cases", {"as_of": OPENED_014}))
    assert "AC-HHG-014" not in out and "CC-2649" in out


def test_rag_retrieval_timebox():
    rows = [dict(r, overlap_reasons=[]) for r in MEMORY_ROWS["cases"]]
    assert [r["id"] for r in timebox_cases(rows, OPENED_014, ["AC-HHG-014"])] == ["CC-2649", "AC-HHG-006"]

    class Emb:
        def embed_query(self, _t):
            return [0.0] * 4

    got = similar_prior_cases(lambda n, p: {"cases": rows}, Emb(), "q", as_of=OPENED_014, k=8, exclude_ids=["AC-HHG-014"])
    assert {r["id"] for r in got["cases"]} == {"CC-2649", "AC-HHG-006"}


# ------------------------------------------------------------------------------------------------ as-of card statistics

def test_llm_view_withholds_stale_whole_history_card_statistics():
    stale = {"card": {"id": "C13487-K1", "n_txns": 85, "max_amt": 252.28, "median_amt": 48.03, "last_ts": "2016-12-29 10:00:00",
                      "known_device_ids": "a,b", "modal_region": "272.0"}, "regions": []}
    v = mcp_client.llm_view("card_profile", stale, OPENED_014)
    assert "n_txns" not in v["card"] and "max_amt" not in v["card"] and "known_device_ids" not in v["card"]
    assert v["card"]["modal_region"] == "272.0" and "_note" in v["card"] and stale["card"]["n_txns"] == 85   # facts untouched
    fresh = {"card": {"id": "C13487-K1", "n_txns": 73, "max_amt": 225.94, "last_ts": "2016-11-22 16:11:00", "known_device_ids": "a"}}
    v = mcp_client.llm_view("card_profile", fresh, OPENED_014)
    assert v["card"]["n_txns"] == 73 and "known_device_ids" not in v["card"]
    cc = {"txn": {"card_seq": 72}, "card": {"id": "C13487-K1", "n_txns": 85}}
    assert "n_txns" not in mcp_client.llm_view("case_context", cc, OPENED_014, {"card_profile": stale})["card"]
    cc_ok = {"txn": {"card_seq": 72}, "card": {"id": "C13487-K1", "n_txns": 73}}
    assert mcp_client.llm_view("case_context", cc_ok, OPENED_014, {"card_profile": fresh})["card"]["n_txns"] == 73


# ------------------------------------------------------------------------------------------------ manifests

def test_run_manifest_accumulates_across_resume(tmp_path, monkeypatch):
    from agent import bench

    monkeypatch.setattr(bench, "_git_sha", lambda: "sha-one")
    monkeypatch.setattr(bench, "_git_dirty", lambda: False)
    settings = bench.SETTINGS
    inv1 = bench.write_manifest(tmp_path / "r", settings, "claude-sonnet-5", ["HHG-017", "HHG-015"])
    bench.record_case_run(tmp_path / "r", "HHG-017", inv1, {"tool_calls": 12, "tokens": 5})
    monkeypatch.setattr(bench, "_git_sha", lambda: "sha-two")
    inv2 = bench.write_manifest(tmp_path / "r", settings, "claude-sonnet-5", ["HHG-015", "HHG-004"], resume=True)
    bench.record_case_run(tmp_path / "r", "HHG-015", inv2)
    m = json.loads((tmp_path / "r" / "run_manifest.json").read_text())
    assert m["cases"] == ["HHG-017", "HHG-015", "HHG-004"]
    assert len(m["invocations"]) == 2 and m["invocations"][1]["resume"] is True
    assert m["case_runs"]["HHG-017"]["git_sha"] == "sha-one" and m["case_runs"]["HHG-015"]["git_sha"] == "sha-two"
    assert m["git_shas"] == ["sha-one", "sha-two"] and m["git_sha"] == "sha-two"


def test_cases_manifest_keeps_every_case(tmp_path, monkeypatch):
    from agent import promote

    monkeypatch.setattr(promote, "SETTINGS", replace(promote.SETTINGS, cases_dir=tmp_path))
    for c in ("HHG-001", "HHG-002", "HHG-014"):
        (tmp_path / f"{c}.json").write_text("{}")
    (tmp_path / "MANIFEST.md").write_text(
        "# cases/ manifest\n\n| case | verdict | p | pattern | sar | tool_calls | tokens | latency_s | notes |\n|---|---|---|---|---|---|---|---|---|\n"
        "| HHG-001 | legitimate | 0.1 | none | False | 15 | 1 | 1.0 |  |\n| HHG-002 | fraud | 0.92 | cnp | False | 17 | 1 | 1.0 |  |\n")
    rows = [("HHG-014", "fraud", 0.9, "undocumented", True, 22, 1, 1.0, "", "final", "abc123")]
    md = promote.manifest_markdown(rows, "final", {"model": "m", "prompt_sha256": {}})
    body = [ln for ln in md.splitlines() if ln.startswith("| HHG-")]
    assert [ln.split("|")[1].strip() for ln in body] == ["HHG-001", "HHG-002", "HHG-014"]
    assert "abc123" in body[2] and "- cases: 3" in md


# ------------------------------------------------------------------------------------------------ mock phase-machine run

@pytest.fixture
def hhg014_run(monkeypatch, tmp_path):
    """HHG-014 through the whole phase machine in RUN_MODE=mock; the memory query also returns the case's own
    AC-HHG-014 (an earlier run's write-back) and a later case's AC-HHG-004."""
    monkeypatch.setenv("RUN_MODE", "mock")
    monkeypatch.setenv("LLM_BACKEND", "mock")
    from agent import tools_local
    from agent.bench import load_case_pack, make_client
    from agent.config import SETTINGS
    from agent.mock_fixtures import FakeSession, format_like_mcp
    from agent.runlog import RunLog

    settings = replace(SETTINGS, run_mode="mock", llm_backend="mock")
    monkeypatch.setattr(tools_local, "SETTINGS", settings)
    if not settings.case_pack_csv.exists():
        pytest.skip("case pack not present")
    ctx = {c.case_id: c for c in load_case_pack(settings.case_pack_csv)}["HHG-014"]

    class Session(FakeSession):
        async def call_tool(self, name, arguments=None, **kw):
            res = await super().call_tool(name, arguments, **kw)
            if name == mcp_client.RUN_INSTALLED and (arguments or {}).get("query_name") == "similar_prior_cases":
                payload = mcp_client.parse_tool_text(res.content[0].text)
                merged = mcp_client.merge_printed((payload.get("data") or {}).get("result"), "similar_prior_cases")
                cases = list(merged.get("cases", [])) + [dict(r) for r in MEMORY_ROWS["cases"] if r["id"].startswith("AC-")]
                text = format_like_mcp("similar_prior_cases", arguments.get("params", {}), {"cases": cases})
                return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], is_error=False, structured_content=None)
            return res

    pm = PhaseMachine(make_client(settings), Session("HHG-014"), "t", settings)
    log = RunLog("t", ctx.case_id, tmp_path / ctx.case_id)
    answer = asyncio.run(pm.run_case(ctx, log))
    return answer, log, ctx


def test_mock_hhg014_answer_passes_the_agent_checks(hhg014_run):
    answer, log, ctx = hhg014_run
    assert answer_text_problems(answer) == [], answer_text_problems(answer)
    if answer["sar"]["file"]:
        assert check_answer_sar_facts(answer, ctx.card_id) == []
        assert " UTC" not in answer["sar"]["narrative"]
    phases = [json.loads(ln) for ln in (log.root / "phases.jsonl").read_text().splitlines() if ln.strip()]
    memory = next(p for p in phases if p.get("name") == "memory" or p.get("phase") == "P1")
    ids = {c.get("id") for c in memory["payload"]["similar_prior_cases"]}
    assert "AC-HHG-014" not in ids and "AC-HHG-004" not in ids and "AC-HHG-006" in ids
    for e in answer["case"]["evidence"]:
        if e["ref"] == "trigger":
            assert e["source"] != "customer"          # an analyst request is not the customer's statement
    assert Path(log.root / "answer.json").exists()
