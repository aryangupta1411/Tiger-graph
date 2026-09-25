"""Unit tests for Module F (rag/). Run: uv run pytest tests/unit/test_rag.py -q
Set HHGOA_DUCKDB to run the DuckDB-backed id resolution; otherwise a static resolver is used."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from rag import sar as sar_mod
from rag.chunk import chunk_readme, n_tokens, window_chunks
from rag.closedcase_embed_text import agent_case_embed_text, embed_text, mask, parse_note
from rag.embed import mock_vector, read_psv, write_psv
from rag.retrieval import case_query_text, grounding_chunks, similar_prior_cases
from rag.validate_sar import DuckResolver, StaticResolver, sentences, validate_sar

RING = "SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080"
WAVE = ['C01289-K1', 'C01996-K2', 'C02910-K1', 'C02975-K1', 'C03676-K2', 'C04108-K2', 'C04274-K1', 'C05448-K2',
        'C06031-K2', 'C06650-K2', 'C06710-K1', 'C07472-K1', 'C08168-K1', 'C08972-K2', 'C09049-K1', 'C09195-K1',
        'C09906-K1', 'C09975-K1', 'C10020-K1', 'C10193-K2', 'C10326-K1', 'C11082-K1', 'C11670-K2', 'C11702-K1',
        'C12645-K1', 'C12796-K1', 'C13291-K1']
PRIOR = ["CC-2649", "CC-2971", "CC-2985", "CC-3035"]


def resolver():
    db = os.environ.get("HHGOA_DUCKDB")
    if db and Path(db).exists():
        return DuckResolver(db)
    return StaticResolver({"customer": {"C13487"}, "card": {"C13487-K1", *WAVE}, "closed_case": set(PRIOR),
                           "txn": {"3460634", "3478561"}, "device": {RING}})


def hhg014_facts() -> sar_mod.SarFacts:
    """Verified facts (DuckDB, 2026-09-19): two ring transactions on C13487-K1 before opened_at 2016-11-22 20:11:00."""
    return sar_mod.SarFacts(
        case_id="HHG-014", graph_case_id="AC-HHG-014", customer_id="C13487", card_id="C13487-K1",
        opened_at="2016-11-22 20:11:00", verdict="fraud", fraud_probability=0.90, pattern="undocumented",
        pattern_description=("Purchases from one unusual Samsung/Chrome-for-Android device profile behind an anonymous proxy "
                             "spread across 28 cards of different customers in November 2016, all online product code C, "
                             "all marked New for each account; four closed cases in August-September 2016 confirmed the "
                             "same profile as fraud"),
        affected=[
            {"id": "3460634", "ts": "2016-11-15 20:30:00", "amt": 112.37, "channel": "online", "product_cd": "C",
             "addr1": "191.0", "device_id": RING, "device_new": "New", "proxy": "IP_PROXY:ANONYMOUS"},
            {"id": "3478561", "ts": "2016-11-22 16:11:00", "amt": 74.96, "channel": "online", "product_cd": "C",
             "addr1": "191.0", "device_id": RING, "device_new": "New", "proxy": "IP_PROXY:ANONYMOUS"},
        ],
        exposure_usd=187.33, connected_card_ids=WAVE, connected_device_profiles=[RING],
        card_type="debit", card_network="visa",
        baseline={"n_txns": 14, "median_amt": 57.95, "max_amt": 200.0, "n_online": 14, "n_in_person": 0},
        prior_cases=[],   # C13487-K1 has no closed case of its own; the four ring cases are on other cards (shared_element)
        customer_reply="",
        ofac={"query": "C13487", "exact": False, "best_score": 0, "matches": [], "ref": "ofac_screen(sdn.csv 2026-09-19)"},
        final_actions=[{"action": "CREATE_CASE", "route": "auto", "reason": "3a"},
                       {"action": "BLOCK_CARD", "route": "L1", "reason": "R6"},
                       {"action": "MONITOR_CONNECTED_CARDS", "route": "auto", "reason": "R6"},
                       {"action": "FILE_REPORT", "route": "L2", "reason": "R6/R9/3a"},
                       {"action": "ESCALATE_TO_ANALYST", "route": "auto", "reason": "R9"}],
        sar_reason="R6 and R9 with 3a: shared device profile across 27 other cards and an undocumented coordinated pattern",
        shared_element="a profile used by 27 other cards in November 2016 and named in four closed fraud cases (CC-2649, CC-2971, CC-2985, CC-3035)",
        trigger_type="analyst_request",
    )


HAND_WRITTEN_HHG014 = (
    "This report concerns suspected coordinated card-not-present fraud through a shared anonymous-proxy device profile, "
    "recorded as internal case AC-HHG-014 (alert HHG-014, opened 2016-11-22 on an analyst's request). "
    "The subjects are customer C13487 and debit card C13487-K1, together with the device profile "
    "SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080, which was used behind an anonymous proxy on 27 other "
    "cards in November 2016, including C01289-K1, C04108-K2 and C13291-K1. "
    "On 2016-11-15 at 20:30 UTC an online purchase of $112.37 under product code C, billed in region 191.0, was made on the card from that device profile, "
    "which was marked New for this account. "
    "On 2016-11-22 at 16:11 UTC a second online purchase of $74.96 under product code C from the same device profile and proxy followed. "
    "The card had 14 prior transactions, all online, with a median amount of $57.95, and the device profile had never been seen on this account "
    "before 2016-11-15. "
    "The same profile was confirmed as fraud in four closed cases in August and September 2016 (CC-2649, CC-2971, CC-2985 and CC-3035), "
    "each of which listed the other cards of that wave as connected, so the activity is treated as an undocumented coordinated pattern rather than isolated misuse. "
    "No prior suspicious activity report has been filed on this card, and OFAC screening of the customer and card identifiers against the SDN list returned no match. "
    "The bank has recommended blocking and reissuing the card (team-lead approval requested), has placed the 27 connected cards under monitoring, "
    "has escalated the case to a fraud analyst, and is filing this report with fraud-manager approval; the total suspicious amount is $187.33."
)


def test_hand_written_hhg014_narrative_validates():
    f = hhg014_facts()
    sar = sar_mod.assemble(f, HAND_WRITTEN_HHG014)
    errs = validate_sar(sar, exposure_usd=187.33, affected_ts=[t["ts"] for t in f.affected],
                        case_ids=("HHG-014", "AC-HHG-014"), customer_id="C13487", card_id="C13487-K1",
                        resolver=resolver(), file_report_in_final=True)
    assert errs == [], errs
    assert sar["activity_dates"] == ["2016-11-15", "2016-11-22"]
    assert "C13487-K1" in sar["subjects"] and RING in sar["subjects"]
    assert 6 <= len(sentences(sar["narrative"])) <= 12


def test_fallback_narrative_hhg014_validates():
    f = hhg014_facts()
    nar = sar_mod.fallback_narrative(f)
    sar = sar_mod.assemble(f, nar)
    errs = validate_sar(sar, exposure_usd=187.33, affected_ts=[t["ts"] for t in f.affected],
                        case_ids=("HHG-014", "AC-HHG-014"), customer_id="C13487", card_id="C13487-K1",
                        resolver=resolver(), file_report_in_final=True)
    assert errs == [], (errs, nar)
    assert sar["total_amount_usd"] == 187.33


def test_fallback_narrative_hhg006_shape_validates():
    """Burst case: 4 affected transactions, no device subjects, no connected cards, customer denial."""
    f = sar_mod.SarFacts(
        case_id="HHG-006", graph_case_id="AC-HHG-006", customer_id="C07297", card_id="C07297-K1",
        opened_at="2016-11-22 02:30:00", verdict="fraud", fraud_probability=0.9, pattern="undocumented",
        pattern_description="Four online purchases of $450-$500 within thirty minutes on a card with two prior online purchases in five months, a shape matching five closed cases and eleven other cards in November-December",
        affected=[{"id": "3476601", "ts": "2016-11-21 22:00:00", "amt": 478.95, "channel": "online", "product_cd": "R", "addr1": ""},
                  {"id": "3476633", "ts": "2016-11-21 22:12:00", "amt": 456.96, "channel": "online", "product_cd": "R", "addr1": ""},
                  {"id": "3476660", "ts": "2016-11-21 22:21:00", "amt": 488.04, "channel": "online", "product_cd": "R", "addr1": ""},
                  {"id": "3476682", "ts": "2016-11-21 22:30:00", "amt": 482.12, "channel": "online", "product_cd": "R", "addr1": ""}],
        exposure_usd=1906.07, baseline={"n_txns": 2, "median_amt": 61.0, "max_amt": 90.0},
        customer_reply="Customer states they did not make these purchases and still has the card",
        final_actions=[{"action": "CREATE_CASE", "route": "auto"}, {"action": "BLOCK_CARD", "route": "L1"},
                       {"action": "FILE_REPORT", "route": "L2"}, {"action": "ESCALATE_TO_ANALYST", "route": "auto"}],
        sar_reason="R2 and 3a: customer denied and exposure $1,906.07 exceeds $1,000; undocumented pattern (R9)",
        trigger_type="customer_report")
    sar = sar_mod.assemble(f, sar_mod.fallback_narrative(f))
    res = StaticResolver({"customer": {"C07297"}, "card": {"C07297-K1"}, "txn": {"3476601", "3476633", "3476660", "3476682"}})
    errs = validate_sar(sar, exposure_usd=1906.07, affected_ts=[t["ts"] for t in f.affected],
                        case_ids=("HHG-006", "AC-HHG-006"), customer_id="C07297", card_id="C07297-K1", resolver=res)
    assert errs == [], (errs, sar["narrative"])


def test_validator_catches_bad_narratives():
    f = hhg014_facts()
    good = sar_mod.assemble(f, HAND_WRITTEN_HHG014)
    kw = dict(exposure_usd=187.33, affected_ts=[t["ts"] for t in f.affected], case_ids=("HHG-014", "AC-HHG-014"),
              customer_id="C13487", card_id="C13487-K1", resolver=resolver())
    bad = dict(good, narrative=good["narrative"] + " Details are in the table below, see attached.")
    assert any(e.startswith("V09") for e in validate_sar(bad, **kw))
    bad = dict(good, total_amount_usd=187.0)
    assert any(e.startswith("V06") for e in validate_sar(bad, **kw))
    bad = dict(good, narrative="Short. Two. Three. Four. Five.")
    assert any(e.startswith("V02") for e in validate_sar(bad, **kw))
    bad = dict(good, narrative=good["narrative"].replace("C13487-K1", "C99999-K9"), subjects=["C13487", "C99999-K9", RING])
    assert any(e.startswith("V07") or e.startswith("V08") for e in validate_sar(bad, **kw))
    # D10: re-ordered dates are fine (context re-mentions are legitimate) ...
    swapped = good["narrative"].replace("On 2016-11-15 at 20:30", "On 2016-11-XX at 20:30").replace("On 2016-11-22 at 16:11", "On 2016-11-15 at 16:11").replace("On 2016-11-XX at 20:30", "On 2016-11-22 at 20:30")
    assert not any(e.startswith("V04") for e in validate_sar(dict(good, narrative=swapped), **kw))
    # ... but both activity dates must be named
    bad = dict(good, narrative=good["narrative"].replace("2016-11-22", "22 November"))
    assert any(e.startswith("V04") for e in validate_sar(bad, **kw))
    # D10: '|' inside a device-profile string is fine (the good narrative has one); table rows / rules / tabs are not
    bad = dict(good, narrative=good["narrative"] + " | card | amount |")
    assert not any(e.startswith("V13") for e in validate_sar(bad, **kw))          # a stray pipe mid-prose is tolerated
    bad = dict(good, narrative="| card | amount |\n|---|---| " + good["narrative"])
    assert any(e.startswith("V13") for e in validate_sar(bad, **kw))
    bad = dict(good, narrative=good["narrative"].replace(" was used", "\twas used"))
    assert any(e.startswith("V13") or e.startswith("V01") for e in validate_sar(bad, **kw))
    neg = sar_mod.negative_sar("3a not met: exposure $59.67 <= $1,000; no shared strong device; no other customer's fraud")
    assert validate_sar(neg, exposure_usd=59.67) == []
    assert validate_sar(dict(neg, narrative="x"), exposure_usd=59.67)


def test_mask_and_parse_note_are_the_etl_functions():
    """D11: rag re-exports etl.parse_closed_cases.mask / embed_text / parse_note - one masking implementation."""
    from etl import parse_closed_cases as etl_pcc

    assert mask is etl_pcc.mask and embed_text is etl_pcc.embed_text and parse_note is etl_pcc.parse_note
    note = ("Case CC-2649: cardholder C03528 reported 3 online purchase(s) they did not make. The purchases came from a "
            "Samsung SM-G935F on Chrome for Android behind an anonymous proxy, a device never seen on this account. "
            "Two other cardholders reported the same device profile this month. Pattern not matched to a documented "
            "typology. Card blocked and reissued.")
    p = parse_note(note)
    assert p["template_id"] == "undoc_ring"
    assert p["note_device"] == "Samsung SM-G935F on Chrome for Android behind an anonymous proxy"
    m = mask("Case CC-0003: model scored a $442.92 transaction at 0.91 on 2016-07-02 on card C05876-K2 for C05876; 1 transaction(s)")
    assert "<AMT>" in m and "<SCORE>" in m and "<DATE>" in m and "<CASE>" in m and "<N> transaction(s)" in m
    assert "C05876" not in m and "<ID>" in m
    t = embed_text("undocumented", "confirmed_fraud", p["note_device"], "", note)
    assert t.startswith("pattern: undocumented | outcome: confirmed_fraud | device: Samsung") and " | region: none | " in t
    assert "\n" not in t
    a = agent_case_embed_text({"case": {"verdict": "fraud", "pattern": "undocumented", "summary": "Ring on card C13487-K1 for $187.33."},
                               "next_best_actions": {"final": [{"action": "BLOCK_CARD"}]}}, RING, "191.0")
    assert a.startswith("pattern: undocumented | outcome: confirmed_fraud | device: SM-G935F") and "<AMT>" in a and "C13487" not in a
    assert " | region: 191.0 | " in a and "Actions: BLOCK_CARD" in a
    q = case_query_text("undocumented", "analyst_request", RING, "191.0", "Two purchases of $112.37 on C13487-K1 from the ring device.")
    assert q.startswith("pattern: undocumented | outcome: unknown | device: SM-G935F") and "<AMT>" in q and "\n" not in q


def test_readme_chunks(tmp_path):
    readme = Path(os.environ.get("HHGOA_README", "")) if os.environ.get("HHGOA_README") else None
    if not readme or not readme.exists():
        pytest.skip("HHGOA_README not set")
    chunks, desc = chunk_readme(readme.read_text())
    ids = {c.id for c in chunks}
    for r in [f"policy#R{i}" for i in range(1, 11)] + ["policy#3a", "policy#3b", "policy#4", "policy#5", "policy#6", "policy#7"]:
        assert r in ids, r
    for p in ["card_testing", "card_not_present_fraud", "card_not_present_new_device", "out_of_region_use", "account_takeover", "undocumented"]:
        assert f"pattern#{p}" in ids
    r5 = next(c for c in chunks if c.id == "policy#R5")
    assert "DECLINE_TRANSACTION" in r5.text and r5.about == ["card_testing"]
    assert desc["card_testing"].startswith("A stolen card number")


def test_window_chunks_overlap_and_bounds():
    para = " ".join(f"Sentence number {i} says something about suspicious activity reporting." for i in range(400))
    pieces = window_chunks([para], 500, 800)
    assert len(pieces) >= 2
    assert all(n_tokens(p) <= 800 * 1.05 for p in pieces)
    # overlap: the first sentence of chunk 2 appears at the end of chunk 1
    first = pieces[1].split(".")[0]
    assert first in pieces[0]


def test_embed_mock_and_psv(tmp_path):
    v = mock_vector("pattern: undocumented | device: SM-G935F", 1024)
    assert len(v) == 1024 and abs(sum(x * x for x in v) - 1.0) < 1e-6
    assert v == mock_vector("pattern: undocumented | device: SM-G935F", 1024)
    p = tmp_path / "v.psv"
    write_psv([("CC-0001", v)], p)
    line = p.read_text().splitlines()[0]
    assert line.startswith("CC-0001|") and "[" not in line and line.count(",") == 1023
    assert list(read_psv(p))[0][0] == "CC-0001"


def test_ofac_screen():
    from rag import config
    if not (config.OFAC_DIR / "sdn.csv").exists():
        pytest.skip("sdn.csv not fetched")
    from rag.ofac import ofac_screen
    r = ofac_screen("AEROCARIBBEAN AIRLINES")
    assert r["exact"] and r["best_score"] == 100 and r["matches"][0]["program"] == "CUBA"
    r = ofac_screen("Aero-Caribbean")  # alias in alt.csv
    assert r["matches"] and r["matches"][0]["ent_num"] == 36
    r = ofac_screen("C13487")
    assert r["matches"] == [] and not r["exact"]
    r = ofac_screen("John Q Cardholder Of Nowhere")
    assert r["best_score"] < 90 or r["matches"] == [] or all(m["score"] >= 90 for m in r["matches"])


class FakeEmbedder:
    def embed_query(self, text):
        return mock_vector(text, 8)


def _printed(rows):
    return [{"v_id": r["id"], "v_type": "ClosedCase", "attributes": {"@kind": "closed", "@outcome_or_verdict": r["o"],
             "@pattern": "undocumented", "@exposure_usd": 100.0, "@opened_at": "2016-08-30 10:00:00",
             "@distance": r["d"], "@overlap_reasons": ["same_device"]}} for r in rows]


def test_retrieval_flatten_diversify_and_fallbacks():
    rows = _printed([{"id": "CC-2649", "o": "confirmed_fraud", "d": 0.10}, {"id": "CC-2971", "o": "confirmed_fraud", "d": 0.12},
                     {"id": "CC-2985", "o": "confirmed_fraud", "d": 0.13}, {"id": "CC-0873", "o": "cleared", "d": 0.40}])
    calls = []

    def run_ok(name, params):
        calls.append(name)
        assert len(params["q"]) == 8 and params["as_of"] == "2016-11-22 20:11:00"
        return {"cases": rows}

    out = similar_prior_cases(run_ok, FakeEmbedder(), "q", device_id=RING, as_of="2016-11-22 20:11:00", k=3)
    ids = [c["id"] for c in out["cases"]]
    assert ids == ["CC-2649", "CC-2971", "CC-0873"] and out["meta"]["source"] == "similar_prior_cases"
    assert out["cases"][0]["overlap_reasons"] == ["same_device"] and out["cases"][0]["kind"] == "closed"

    def run_two(name, params):
        if name == "similar_prior_cases":
            raise RuntimeError("vectorSearch with candidate_set of multiple vertex types is not supported")
        if name == "similar_prior_cases_closed":
            return [{"cases": rows[:2]}]
        if name == "similar_prior_cases_agent":
            return {"cases": []}
        raise AssertionError(name)

    out = similar_prior_cases(run_two, FakeEmbedder(), "q", as_of="2016-11-22 20:11:00", k=8)
    assert out["meta"]["source"] == "two_query" and [c["id"] for c in out["cases"]] == ["CC-2649", "CC-2971"]

    def run_struct(name, params):
        assert name == "similar_cases_structural" and "q" not in params
        return {"cases": rows[:1]}

    out = similar_prior_cases(run_struct, FakeEmbedder(), "q", as_of="2016-11-22 20:11:00", k=8, index_ready=lambda: False)
    assert out["meta"]["source"] == "similar_cases_structural"

    def run_chunks(name, params):
        assert name == "grounding_chunks" and params["doc_filter"] == "sar_guidance_narrative"
        return {"chunks": [{"v_id": "sar_guidance_narrative#p06-when", "v_type": "PolicyChunk",
                            "attributes": {"doc_id": "sar_guidance_narrative", "section": "When", "page": 6, "kind": "regulation",
                                           "text": "individual dates and amounts", "@distance": 0.2}}]}

    g = grounding_chunks(run_chunks, FakeEmbedder(), "when did it happen", doc_filter="sar_guidance_narrative")
    assert g["chunks"][0]["id"] == "sar_guidance_narrative#p06-when" and g["chunks"][0]["distance"] == 0.2
    q = case_query_text("undocumented", "analyst_request", "Samsung SM-G935F on Chrome for Android behind an anonymous proxy", "191.0",
                        "27 cards on the profile; $112.37 and $74.96 on 2016-11-15 and 2016-11-22")
    assert q.startswith("pattern: undocumented | outcome: unknown | device: Samsung") and "<AMT>" in q and "<DATE>" in q
