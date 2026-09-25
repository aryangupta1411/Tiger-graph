"""Schemas: README shape, and NO numeric bounds in any structured-output schema."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

# M10: skip the module (never error at collection) when pydantic, the structured-output layer, is absent.
pytest.importorskip("pydantic", reason="needs pydantic")

from agent import schemas as S  # noqa: E402


@pytest.mark.parametrize("model", S.ALL_SCHEMAS, ids=lambda m: m.__name__)
def test_no_numeric_bounds(model):
    js = model.model_json_schema()
    assert S.find_numeric_bounds(js) == [], f"{model.__name__}: {S.find_numeric_bounds(js)}"
    S.assert_no_numeric_bounds(model)


def test_bounds_detector_catches_a_bad_schema():
    from pydantic import BaseModel, Field

    class Bad(BaseModel):
        p: float = Field(ge=0.0, le=1.0)
        s: str = Field(min_length=1)

    hits = S.find_numeric_bounds(Bad.model_json_schema())
    assert any(h.endswith(".minimum") for h in hits) and any(h.endswith(".maximum") for h in hits)
    assert any(h.endswith(".minLength") for h in hits)
    with pytest.raises(AssertionError):
        S.assert_no_numeric_bounds(Bad)


def test_property_named_pattern_is_not_flagged():
    js = S.CaseRecord.model_json_schema()
    assert "pattern" in js["properties"]
    assert S.find_numeric_bounds(js) == []


def test_additional_properties_false_everywhere():
    for m in S.ALL_SCHEMAS:
        js = m.model_json_schema()
        assert js.get("additionalProperties") is False
        for d in (js.get("$defs") or {}).values():
            assert d.get("additionalProperties") is False


def test_answer_matches_readme_example_shape():
    ex = {
        "case_id": "HHG-017",
        "case": {"status": "closed_fraud", "verdict": "fraud", "fraud_probability": 0.86, "pattern": "card_testing", "pattern_description": "",
                 "affected_txn_ids": ["3412877"], "first_suspicious_txn_id": "3412877", "connected_card_ids": ["C00877-K1"],
                 "connected_device_profiles": ["SAMSUNG SM-G892A Build/NRD90M | Android 7.0 | samsung browser 6.2 | 2220x1080"],
                 "exposure_usd": 268.43,
                 "evidence": [{"claim": "x", "source": "graph", "ref": "query:card_window(card_id=C00377-K1, hours=2)", "entity_ids": ["3412877"]}],
                 "similar_prior_cases": ["CC-0141"], "summary": "s", "written_to_graph": True, "graph_case_id": "AC-HHG-017"},
        "evidence_requests": [{"type": "customer_validation", "asked_after_step": 4, "assumed_response": "ASSUMED (simulated): denied"}],
        "next_best_actions": {"initial": [{"action": "DECLINE_TRANSACTION", "route": "L1", "reason": "R5"}],
                              "final": [{"action": "BLOCK_CARD", "route": "L1", "reason": "R2"}], "what_changed": "denial"},
        "sar": {"file": True, "reason": "R2", "narrative": "n", "subjects": ["C00377"], "total_amount_usd": 268.43, "activity_dates": ["2016-11-14", "2016-11-14"]},
        "stop_reason": "settled", "tool_calls": 9, "tokens": 12480, "latency_s": 18.7,
    }
    a = S.Answer.model_validate(ex)
    assert json.loads(a.model_dump_json())["case"]["pattern"] == "card_testing"


def test_enums_are_strict():
    with pytest.raises(ValidationError):
        S.ActionRec(action="FREEZE", route="auto", reason="x")
    with pytest.raises(ValidationError):
        S.ActionRec(action="BLOCK_CARD", route="L3", reason="x")
    with pytest.raises(ValidationError):
        S.CaseRecord.model_validate({"status": "closed", "verdict": "fraud"})


def test_llm_schemas_dump():
    d = S.dump_schemas()
    assert set(d) == {m.__name__ for m in S.ALL_SCHEMAS}
    assert "wanted_requests" in d["Assessment"]["properties"]
