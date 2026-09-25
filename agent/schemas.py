"""Pydantic schemas (contracts §C) — the README answer format plus the LLM-facing
intermediate schemas used with `client.beta.messages.parse(output_format=...)`.

Rule (PLAN §4.10, structured-outputs docs): NO numeric bounds in any schema —
`minimum`, `maximum`, `minLength`, `maxLength`, `multipleOf` are rejected by the
structured-outputs grammar. Ranges (0 <= fraud_probability <= 1, p_engine ± 0.10,
6–12 SAR sentences) are validated in code (`agent/phase_machine.py`).
`assert_no_numeric_bounds()` is the guard used by tests/unit/test_schemas.py.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

ActionName = Literal[
    "ALLOW_TRANSACTION", "DECLINE_TRANSACTION", "MONITOR_CARD", "MONITOR_CONNECTED_CARDS",
    "WARN_CUSTOMER", "VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH", "BLOCK_CARD", "BLOCK_ALL_CARDS",
    "GENERATE_REPORT", "CREATE_CASE", "FILE_REPORT", "ESCALATE_TO_ANALYST", "CLOSE_NO_FRAUD",
]
Route = Literal["auto", "L1", "L2"]
Verdict = Literal["fraud", "legitimate", "uncertain"]
Status = Literal["open", "closed_fraud", "closed_legitimate", "escalated"]
Pattern = Literal[
    "card_testing", "card_not_present_fraud", "card_not_present_new_device",
    "out_of_region_use", "account_takeover", "undocumented", "none",
]
EvidenceSource = Literal["graph", "document", "customer", "external"]
RequestType = Literal["customer_validation", "step_up_auth", "analyst_info"]


class _Strict(BaseModel):
    """additionalProperties: false everywhere (required by structured outputs)."""
    model_config = ConfigDict(extra="forbid")


# ---- README answer format -------------------------------------------------

class EvidenceItem(_Strict):
    claim: str
    source: EvidenceSource
    ref: str                       # query name, document section, or request id
    entity_ids: list[str]          # ids the claim rests on; must exist in the dataset


class EvidenceRequest(_Strict):
    type: RequestType
    asked_after_step: int
    assumed_response: str          # prefixed "ASSUMED (simulated): ..."


class ActionRec(_Strict):
    action: ActionName
    route: Route
    reason: str                    # cites the policy rule (R1–R10, 3a, 3b, §5, §6)


class NextBestActions(_Strict):
    initial: list[ActionRec]
    final: list[ActionRec]
    what_changed: str              # literal "nothing" when final == initial


class CaseRecord(_Strict):
    status: Status
    verdict: Verdict
    fraud_probability: float
    pattern: Pattern
    pattern_description: str       # "" unless pattern == undocumented
    affected_txn_ids: list[str]
    first_suspicious_txn_id: str   # "" when legitimate
    connected_card_ids: list[str]
    connected_device_profiles: list[str]
    exposure_usd: float
    evidence: list[EvidenceItem]
    similar_prior_cases: list[str] # CC-xxxx ids only
    summary: str
    written_to_graph: bool
    graph_case_id: str


class SAR(_Strict):
    file: bool
    reason: str
    narrative: str
    subjects: list[str]
    total_amount_usd: float
    activity_dates: list[str]      # [] or [first, last] as YYYY-MM-DD


class Answer(_Strict):
    case_id: str
    case: CaseRecord
    evidence_requests: list[EvidenceRequest]
    next_best_actions: NextBestActions
    sar: SAR
    stop_reason: str
    tool_calls: int
    tokens: int
    latency_s: float


# ---- LLM-facing intermediate schemas (parsed with output_format=...) -------

class Assessment(_Strict):
    verdict: Verdict
    fraud_probability: float       # clamped to p_engine ± 0.10 in code
    calibration_basis: str         # must cite the scorecard (cms_p, cal_p, adjustments)
    pattern: Pattern
    pattern_description: str
    affected_txn_ids: list[str]
    first_suspicious_txn_id: str
    connected_card_ids: list[str]
    connected_device_profiles: list[str]
    evidence: list[EvidenceItem]
    wanted_requests: list[RequestType]
    sufficient: bool               # LLM's view of policy §6; the engine's stop rule decides


class ActionChoice(_Strict):
    actions: list[ActionRec]       # chosen within the admissible set given in the prompt


class Closing(_Strict):
    summary: str                   # two to six sentences
    stop_reason: str               # names the §6 test that fired
    similar_prior_cases_used: list[str]


class SarDraft(_Strict):
    narrative: str                 # six to twelve sentences; who/what/when/where/how/why
    subjects: list[str]


LLM_SCHEMAS: tuple[type[BaseModel], ...] = (Assessment, ActionChoice, Closing, SarDraft)
ALL_SCHEMAS: tuple[type[BaseModel], ...] = (
    EvidenceItem, EvidenceRequest, ActionRec, NextBestActions, CaseRecord, SAR, Answer,
    Assessment, ActionChoice, Closing, SarDraft,
)

_FORBIDDEN_KEYS = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
                   "minLength", "maxLength", "maxItems"}


def find_numeric_bounds(schema: Any, path: str = "$") -> list[str]:
    """Return every JSON-schema path carrying a keyword structured outputs reject.

    Property NAMES are never keywords (`properties.pattern` is our field, not a regex),
    so the children of `properties` / `$defs` are traversed without checking their keys.
    """
    hits: list[str] = []
    if isinstance(schema, dict):
        for k, v in schema.items():
            if k in ("properties", "$defs", "definitions") and isinstance(v, dict):
                for name, sub in v.items():
                    hits.extend(find_numeric_bounds(sub, f"{path}.{k}.{name}"))
                continue
            if k in _FORBIDDEN_KEYS:
                hits.append(f"{path}.{k}")
            hits.extend(find_numeric_bounds(v, f"{path}.{k}"))
    elif isinstance(schema, list):
        for i, v in enumerate(schema):
            hits.extend(find_numeric_bounds(v, f"{path}[{i}]"))
    return hits


def assert_no_numeric_bounds(model: type[BaseModel]) -> None:
    hits = find_numeric_bounds(model.model_json_schema())
    if hits:
        raise AssertionError(f"{model.__name__} schema carries unsupported keywords: {hits}")


def dump_schemas() -> dict[str, dict]:
    """All schemas as JSON (used by the verification step and the UI 'why' panel)."""
    return {m.__name__: m.model_json_schema() for m in ALL_SCHEMAS}
