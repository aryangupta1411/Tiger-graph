"""Engine dataclasses — exactly contracts/interfaces.md §B."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class CaseContext:
    case_id: str
    trigger_type: str            # risk_score | customer_report | analyst_request
    trigger_text: str
    flagged_txn_id: str
    card_id: str
    customer_id: str
    opened_at: str               # 'YYYY-MM-DD HH:MM:SS' (= as_of for every query)
    risk_score: float | None


@dataclass
class Evidence:
    """One row of the evidence ledger; also the answer file's evidence item."""
    claim: str
    source: str                  # graph | document | customer | external
    ref: str                     # "query:<name>(<params>)" | "evidence_request:<n>" | "doc:<id>#<section>" | "trigger"
    entity_ids: list[str]
    family: str                  # history | device | memory | customer | document
    direction: str               # fraud | legit | neutral
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
    flags: dict                  # ring_hit, burst_hit, card_testing_chain, recurring_match, denial, conflict,
                                 # scorer_unreliable, mixed_channel, any_new_member, all_in_modal_region, ...
    chain: list[dict]            # episode_candidates.chain rows
    episode_ids: list[str]
    first_suspicious_txn_id: str
    exposure_usd: float
    connected_card_ids: list[str]
    connected_device_profiles: list[str]
    pattern: str
    pattern_description: str
    verdict: str                 # fraud | legitimate | uncertain
    similar_prior_cases: list[str]
    evidence: list[Evidence] = field(default_factory=list)   # the ledger the engine built (engine-only extra)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["families_fraud"] = sorted(self.families_fraud)
        d["families_legit"] = sorted(self.families_legit)
        d["evidence"] = [e.as_item() | {"family": e.family, "direction": e.direction} for e in self.evidence]
        return d


Facts = dict[str, dict]          # query name -> printed JSON (exactly the contracts in §A)

ACTIONS = ["ALLOW_TRANSACTION", "DECLINE_TRANSACTION", "MONITOR_CARD", "MONITOR_CONNECTED_CARDS", "WARN_CUSTOMER",
           "VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH", "BLOCK_CARD", "BLOCK_ALL_CARDS", "GENERATE_REPORT", "CREATE_CASE",
           "FILE_REPORT", "ESCALATE_TO_ANALYST", "CLOSE_NO_FRAUD"]
PATTERNS = ["card_testing", "card_not_present_fraud", "card_not_present_new_device", "out_of_region_use",
            "account_takeover", "undocumented", "none"]
VERDICTS = ["fraud", "legitimate", "uncertain"]
STATUSES = ["open", "closed_fraud", "closed_legitimate", "escalated"]
REQUEST_TYPES = ["customer_validation", "step_up_auth", "analyst_info"]
