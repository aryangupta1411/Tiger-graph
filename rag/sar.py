"""SAR narrative generator (PLAN section 4.8).

Skeleton S1-S8 (FinCEN 2003 narrative guidance: who / what / when / where / why / how, introduction-body-
conclusion, individual dates and amounts, no tables, no 'see attached'):
  S1 typology + internal case id      S2 who (customer, card, device profile, connected cards)
  S3-S5 what/when/where/how, chronological, with dates, amounts, channel, region, device
  S6 why unusual vs the cardholder's baseline
  S7 prior SAR on the card + OFAC screening result
  S8 actions taken / requested, connected cards monitored, total amount

Two producers, one contract:
  * build_prompt(facts, chunks)  -> (system, user) for the LLM (agent/sar.py, messages.parse(SarDraft));
  * fallback_narrative(facts)    -> deterministic prose from the same facts, used by the day-6 engine-only
                                    fallback and as the retry-of-last-resort when the LLM draft fails
                                    validate_sar twice.
Both go through assemble(facts, narrative) -> the README `sar` object, and every narrative is checked by
rag.validate_sar before it is written into the answer file.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

TYPOLOGY = {
    "card_testing": "card testing (small online authorizations used to test a stolen card number before a larger purchase)",
    "card_not_present_fraud": "card-not-present fraud (online use of the card number without the card)",
    "card_not_present_new_device": "card-not-present fraud from a device new to the account",
    "out_of_region_use": "card-present use in a billing region where the cardholder has no history",
    "account_takeover": "account takeover (mixed-channel activity inconsistent with the cardholder, pointing to stolen credentials)",
    "undocumented": "an undocumented coordinated fraud pattern",
    "none": "unauthorized card activity",
}


@dataclass
class SarFacts:
    case_id: str
    graph_case_id: str
    customer_id: str
    card_id: str
    opened_at: str                                   # 'YYYY-MM-DD HH:MM:SS'
    verdict: str
    fraud_probability: float
    pattern: str
    pattern_description: str
    affected: list[dict]                             # [{id, ts, amt, channel, product_cd, addr1, device_id, device_new, proxy}]
    exposure_usd: float
    connected_card_ids: list[str] = field(default_factory=list)
    connected_device_profiles: list[str] = field(default_factory=list)
    card_type: str = ""
    card_network: str = ""
    baseline: dict = field(default_factory=dict)     # card_profile.card subset: n_txns, median_amt, max_amt, n_online, n_in_person, modal_region, first_ts, n_devices_seen
    prior_cases: list[dict] = field(default_factory=list)   # [{id, pattern, outcome, report_filed, opened_at}]
    customer_reply: str = ""                          # assumed_response text, "" when none
    ofac: dict | None = None                          # rag.ofac.ofac_screen result
    final_actions: list[dict] = field(default_factory=list)
    sar_reason: str = ""                              # engine/policy.sar_required reason (cites 3a / R2 / R6 / R9)
    shared_element: str = ""                          # e.g. "device profile X used by 27 other cards in November 2016"
    institution: str = "the issuing bank"
    trigger_type: str = "risk_score"


# ----------------------------------------------------------------------------- helpers

def usd(x: float) -> str:
    return f"${x:,.2f}"


def _date(ts: str) -> str:
    return (ts or "")[:10]


def _time(ts: str) -> str:
    return (ts or "")[11:16]


def activity_dates(affected: list[dict]) -> list[str]:
    ds = sorted(_date(t.get("ts", "")) for t in affected if t.get("ts"))
    return [ds[0], ds[-1]] if ds else []


def subjects_from(facts: SarFacts, narrative: str) -> list[str]:
    """Customer, card, connected cards and device profiles that are literally named in the narrative."""
    subs: list[str] = []
    for s in [facts.customer_id, facts.card_id, *facts.connected_card_ids, *facts.connected_device_profiles]:
        if s and s in narrative and s not in subs:
            subs.append(s)
    return subs


def _device_phrase(t: dict) -> str:
    dev = t.get("device_id") or ""
    if not dev:
        return ""
    bits = [f"from device profile {dev}"]
    if t.get("device_new") == "New":
        bits.append("marked New for this account")
    if (t.get("proxy") or "").upper().startswith("IP_PROXY:ANONYMOUS"):
        bits.append("behind an anonymous proxy")
    elif (t.get("proxy") or "").upper().startswith("IP_PROXY:HIDDEN"):
        bits.append("behind a hidden proxy")
    return ", ".join(bits)


def skeleton(facts: SarFacts) -> list[dict]:
    """The S1-S8 slots with the facts each must carry. Given to the LLM verbatim; also drives the fallback."""
    prior_reported = [c["id"] for c in facts.prior_cases if str(c.get("report_filed", "")).lower() in ("true", "yes", "1")]
    ofac_line = ("no OFAC/SDN match for any subject" if not facts.ofac or not facts.ofac.get("matches")
                 else f"possible OFAC/SDN match: {facts.ofac['matches'][0]['name']} (score {facts.ofac['best_score']})")
    return [
        {"slot": "S1", "must": "typology and internal case id",
         "facts": {"typology": TYPOLOGY.get(facts.pattern, TYPOLOGY["none"]), "graph_case_id": facts.graph_case_id,
                   "alert_id": facts.case_id, "opened_at": _date(facts.opened_at), "institution": facts.institution}},
        {"slot": "S2", "must": "who: customer, card (type/network), device profile(s), connected cards",
         "facts": {"customer_id": facts.customer_id, "card_id": facts.card_id, "card_type": facts.card_type,
                   "card_network": facts.card_network, "devices": facts.connected_device_profiles,
                   "connected_card_ids": facts.connected_card_ids, "shared_element": facts.shared_element}},
        {"slot": "S3-S5", "must": "what/when/where/how in chronological order: every transaction with date, time, amount, channel, product code, region, device",
         "facts": {"transactions": sorted(facts.affected, key=lambda t: (t.get("ts", ""), str(t.get("id", ""))))}},
        {"slot": "S6", "must": "why unusual versus the cardholder's own history",
         "facts": {"baseline": facts.baseline, "pattern_description": facts.pattern_description,
                   "customer_reply": facts.customer_reply}},
        {"slot": "S7", "must": "prior SAR status on this card and OFAC screening result",
         "facts": {"prior_cases": facts.prior_cases, "prior_reports": prior_reported, "ofac": ofac_line}},
        {"slot": "S8", "must": "actions taken or requested (with approval route), connected cards monitored, total amount",
         "facts": {"final_actions": facts.final_actions, "exposure_usd": facts.exposure_usd,
                   "sar_reason": facts.sar_reason}},
    ]


# ----------------------------------------------------------------------------- LLM prompt

SYSTEM = """You draft the narrative section of a Suspicious Activity Report for a card issuer. Write one paragraph of
six to twelve sentences, plain prose, chronological, that a regulator can read without any attachment.
Follow FinCEN's narrative guidance: cover who, what, when, where, why and how; write every transaction as
'On YYYY-MM-DD at HH:MM ...' (the data carries no time zone; never name one) with its amount in USD with cents, in chronological order; name the instrument (card id), the channel (online /
in person), the billing region code and the device profile string exactly as given; say why the activity is
unusual for this cardholder; state whether a prior report exists and the OFAC screening result (reserve the phrase 'On <date>' for
transactions; write the opening date as 'opened YYYY-MM-DD'); end with the
actions taken or requested and the total amount. Use only the facts provided - never invent merchants,
names, addresses or ids. No tables, no lists, no headings, no line breaks, no 'see attached'. Do not mention
timestamp seconds, model internals or that a reply was simulated; describe the customer's reply as a statement.
Never state a time zone or the cardholder's gender."""


def build_prompt(facts: SarFacts, chunks: list[dict]) -> tuple[str, str]:
    """(system, user). `chunks` = grounding_chunks rows (doc_id, section, page, text) - 3-5 FinCEN excerpts."""
    excerpts = "\n\n".join(f"[{c['doc_id']}#{c['section']} p.{c.get('page', 0)}]\n{c['text']}" for c in chunks[:5])
    user = ("FACTS (JSON, authoritative):\n" + json.dumps(skeleton(facts), indent=1, default=str) +
            "\n\nREGULATORY EXCERPTS (for style and required elements only):\n" + excerpts +
            "\n\nWrite the narrative now. Return JSON {\"narrative\": str, \"subjects\": [ids named]}.")
    return SYSTEM, user


# ----------------------------------------------------------------------------- deterministic fallback

def fallback_narrative(facts: SarFacts) -> str:
    """Rule-templated narrative that satisfies rag.validate_sar by construction (6-12 sentences)."""
    txns = sorted(facts.affected, key=lambda t: (t.get("ts", ""), str(t.get("id", ""))))
    dates = activity_dates(txns)
    s: list[str] = []
    typ = TYPOLOGY.get(facts.pattern, TYPOLOGY["none"])
    # S1
    # NB: "opened {date}" not "opened on {date}": the validator reads "On <date>" as a transaction sentence.
    s.append(f"This report by {facts.institution} concerns {typ} suspected on card {facts.card_id} held by customer "
             f"{facts.customer_id}; internal case {facts.graph_case_id} (alert {facts.case_id}) was opened "
             f"{_date(facts.opened_at)}.")
    # S2
    card_desc = " ".join(x for x in [facts.card_type, facts.card_network] if x and x != "unknown")
    who = f"The subjects are customer {facts.customer_id} and card {facts.card_id}" + (f", a {card_desc} card" if card_desc else "")
    if facts.connected_device_profiles:
        who += f"; the activity came from device profile {facts.connected_device_profiles[0]}"
        if facts.shared_element:
            who += f", {facts.shared_element}"
    elif facts.shared_element:
        who += f"; {facts.shared_element}"
    s.append(who + ".")
    if facts.connected_card_ids:
        shown = ", ".join(facts.connected_card_ids[:6])
        more = f" and {len(facts.connected_card_ids) - 6} further cards listed in the case record" if len(facts.connected_card_ids) > 6 else ""
        s.append(f"The same device profile was used on {len(facts.connected_card_ids)} other cards ({shown}{more}), which are treated as connected.")
    # S3-S5 chronology, at most ~3 transactions per sentence
    n = len(txns)
    if n == 0:
        s.append(f"No individual transactions were identified; the suspicious activity is described in the case record {facts.graph_case_id}.")
    else:
        lead = (f"The activity consists of {n} transaction{'s' if n != 1 else ''} between {dates[0]} and {dates[-1]} "
                f"totaling {usd(facts.exposure_usd)}.") if n > 1 else "The activity consists of one transaction."
        s.append(lead)
        for i in range(0, n, 3):
            group = txns[i:i + 3]
            parts = []
            for t in group:
                ch = "online" if t.get("channel") == "online" else "in-person"
                p = f"on {_date(t.get('ts'))} at {_time(t.get('ts'))} a {usd(float(t.get('amt', 0)))} {ch} purchase"
                if t.get("product_cd"):
                    p += f" (product code {t['product_cd']})"
                if t.get("addr1"):
                    p += f" billed in region {t['addr1']}"
                dp = _device_phrase(t)
                if dp:
                    p += f" {dp}"
                parts.append(p)
            sent = "; ".join(parts)
            s.append(sent[0].upper() + sent[1:] + ".")
    # S6 why unusual
    b = facts.baseline or {}
    why = []
    if b.get("n_txns"):
        why.append(f"the card's {int(b['n_txns'])} prior transactions had a median amount of {usd(float(b.get('median_amt', 0)))}"
                   + (f" and a maximum of {usd(float(b['max_amt']))}" if b.get("max_amt") else ""))
    if b.get("modal_region") and facts.pattern == "out_of_region_use":
        why.append(f"the cardholder's normal in-person activity is in billing region {b['modal_region']}")
    if facts.pattern == "card_not_present_new_device" or any(t.get("device_new") == "New" for t in txns):
        # the identity record's own flag (id_15), not a claim about the device's history: a profile marked New can still
        # have earlier uses on the card (HHG-006: Trident prior_n=3)
        why.append("the identity record marked the device as New for this account")
    if facts.pattern_description:
        pd = facts.pattern_description.rstrip(".").replace("\n", " ")
        why.append(pd[0].lower() + pd[1:])
    if facts.customer_reply:
        why.append(f"the cardholder stated: {facts.customer_reply.replace('ASSUMED (simulated): ', '').rstrip('.')}")
    if not why:
        why.append("the amounts, timing and channel do not fit the cardholder's established pattern of use")
    s.append("The activity is inconsistent with the cardholder's history: " + "; ".join(why) + ".")
    # S7 prior SAR + OFAC
    reported = [c["id"] for c in facts.prior_cases if str(c.get("report_filed", "")).lower() in ("true", "yes", "1")]
    if reported:
        prior = f"A prior suspicious activity report was filed on this card under closed case {reported[0]}"
    elif facts.prior_cases:
        prior = f"No prior suspicious activity report has been filed on this card ({len(facts.prior_cases)} earlier closed cases, none reported)"
    else:
        prior = "No prior suspicious activity report has been filed on this card"
    if facts.ofac and facts.ofac.get("matches"):
        m = facts.ofac["matches"][0]
        ofac = f"OFAC screening returned a possible SDN match ({m['name']}, score {facts.ofac['best_score']}) that is under review"
    else:
        ofac = "OFAC screening of the customer and card identifiers against the SDN list returned no match"
    s.append(f"{prior}; {ofac}.")
    # S8 actions
    acts = []
    for a in facts.final_actions:
        name, route = a.get("action", ""), a.get("route", "auto")
        text = {
            "BLOCK_CARD": "block and reissue of the card",
            "BLOCK_ALL_CARDS": "block of all the customer's cards",
            "DECLINE_TRANSACTION": "decline of pending authorizations",
            "MONITOR_CARD": "enhanced monitoring of the card for 72 hours",
            "MONITOR_CONNECTED_CARDS": "monitoring of the connected cards",
            "ESCALATE_TO_ANALYST": "escalation to a fraud analyst",
            "FILE_REPORT": "this filing",
            "CREATE_CASE": "an internal fraud case",
            "STEP_UP_AUTH": "step-up authentication on further activity",
            "VERIFY_WITH_CUSTOMER": "verification with the cardholder",
            "WARN_CUSTOMER": "a warning to the cardholder",
            "CLOSE_NO_FRAUD": "closure as no fraud",
            "ALLOW_TRANSACTION": "release of the transaction",
            "GENERATE_REPORT": "an internal report",
        }.get(name, name.lower().replace("_", " "))
        if route == "L1":
            text += " (team-lead approval requested)"
        elif route == "L2":
            text += " (fraud-manager approval requested)"
        acts.append(text)
    s.append(("Actions: " + ", ".join(acts) if acts else "Actions are recorded in the case file") +
             f"; total suspicious amount {usd(facts.exposure_usd)}.")
    s.append("Supporting documentation, including the transaction records and device evidence, is retained on file for five years and is available to law enforcement on request.")
    return " ".join(s)


# ----------------------------------------------------------------------------- assembly

def assemble(facts: SarFacts, narrative: str, reason: str | None = None) -> dict:
    """README `sar` object for file=true."""
    narrative = " ".join(narrative.split())
    return {
        "file": True,
        "reason": reason or facts.sar_reason or "3a: fraud strongly suspected and a filing condition holds",
        "narrative": narrative,
        "subjects": subjects_from(facts, narrative),
        "total_amount_usd": round(float(facts.exposure_usd), 2),
        "activity_dates": activity_dates(facts.affected),
    }


def negative_sar(reason: str) -> dict:
    """README `sar` object for file=false (reason must still cite the rule, e.g. '3a not met: ...')."""
    return {"file": False, "reason": reason, "narrative": "", "subjects": [], "total_amount_usd": 0, "activity_dates": []}


def facts_to_dict(facts: SarFacts) -> dict:
    return asdict(facts)
