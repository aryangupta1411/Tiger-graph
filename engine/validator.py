"""Answer-file validator — PLAN §4.7 invariants (+ decision D10) + §4.9 (schema, id existence via DuckDB, time-box,
exposure recomputation, seed-tell lint, AC- ids hard failure, R1 lint).

validate(answer, db, meta=None) -> [] when valid, else a list of error strings
  db   : an engine.facts_duckdb.DuckFacts (or anything with .q(sql, *params) -> DataFrame over the facts tables)
  meta : optional {"p_pre": float, "families_fraud_pre": int, "opened_at": str, "card_id": str, "customer_id": str}
         used for the R1 lint (needs the pre-evidence probability), the time-box check and the SAR subject check

SAR validation is delegated to `rag.validate_sar.validate_sar` (the single SAR validator, D10) through
`FactsResolver`, an id resolver over the facts DB; when the rag package is not importable the local
equivalent `_validate_sar_local` applies the same rules with the relaxed dates check (both activity_dates
must appear in the narrative) and the relaxed pipe rule (only leading-pipe table rows and tabs are rejected).
"""

from __future__ import annotations

import re

from engine.types import ACTIONS, PATTERNS, REQUEST_TYPES, STATUSES, VERDICTS

SEED_TELL = re.compile(r"(:00\s*seconds|seconds\s*==?\s*:?00|seeded|seed(ing)?\s+tell|synthetic rows?|QA aid|second\(ts\)\s*=\s*0)", re.I)
KAGGLE = re.compile(r"(kaggle|isFraud|is_fraud)", re.I)
TABLE_ROW = re.compile(r"(^|\n)\s*\|")  # a line starting with a pipe is a markdown table row; '|' inside device profiles is fine
FRAUD_CONTAINMENT = {"BLOCK_CARD", "DECLINE_TRANSACTION", "ESCALATE_TO_ANALYST", "BLOCK_ALL_CARDS"}
_CARD = re.compile(r"C\d{5}-K\d")
_CUST = re.compile(r"C\d{5}")
_CC = re.compile(r"CC-\d{4}")
_R2_CITE = re.compile(r"\bR2\b")


def uncertain_block_violation(initial: list[dict]) -> bool:
    """`uncertain ⇒ no block in initial` (PLAN §4.7), with the README R2 exception: a customer denial takes BLOCK_CARD from
    intake at any verdict (README §3b example), so an initial BLOCK_CARD whose reason cites R2 is allowed. BLOCK_ALL_CARDS
    never is (R10)."""
    for a in initial:
        if a["action"] == "BLOCK_ALL_CARDS":
            return True
        if a["action"] == "BLOCK_CARD" and not _R2_CITE.search(a.get("reason") or ""):
            return True
    return False


class FactsResolver:
    """`rag.validate_sar.Resolver` over the facts DB (tables txc / card_feat / customer_feat / cc / device_profile)."""

    def __init__(self, db):
        self.db = db
        self._cache: dict[tuple[str, str], bool] = {}

    def exists(self, kind: str, ident: str) -> bool:
        key = (kind, ident)
        if key not in self._cache:
            q = {
                "customer": ("SELECT 1 FROM customer_feat WHERE id = ? LIMIT 1", ident),
                "card": ("SELECT 1 FROM card_feat WHERE id = ? LIMIT 1", ident),
                "closed_case": ("SELECT 1 FROM cc WHERE case_id = ? LIMIT 1", ident),
                "txn": ("SELECT 1 FROM txc WHERE TransactionID = ? LIMIT 1", int(ident) if str(ident).isdigit() else -1),
                "device": ("SELECT 1 FROM device_profile WHERE id = ? LIMIT 1", ident),
            }.get(kind)
            self._cache[key] = bool(q) and len(self.db.q(q[0], q[1])) > 0
        return self._cache[key]


def _validate_sar_local(sar: dict, c: dict, fin: list[str], meta: dict | None) -> list[str]:
    """Fallback when rag.validate_sar cannot be imported: the same rules, relaxed dates and pipes (D10)."""
    e: list[str] = []
    if sar["file"]:
        nar = sar["narrative"]
        if sar["total_amount_usd"] != c["exposure_usd"]:
            e.append("sar.total_amount_usd != exposure_usd")
        if len(sar["activity_dates"]) != 2 or not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) for d in sar["activity_dates"]) or sar["activity_dates"][0] > sar["activity_dates"][1]:
            e.append("sar.activity_dates must be [first, last] YYYY-MM-DD")
        elif not all(d in nar for d in sar["activity_dates"]):
            e.append("sar.narrative must name both activity_dates")
        n_sent = len(re.findall(r"[.!?](\s|$)", nar))
        if not 6 <= n_sent <= 12:
            e.append(f"sar.narrative must be 6-12 sentences (got {n_sent})")
        if not re.search(r"\d{4}-\d{2}-\d{2}", nar) or not re.search(r"\$\d", nar):
            e.append("sar.narrative needs an ISO date and an amount")
        if re.search(r"see attached|see attachment", nar, re.I) or "\t" in nar or TABLE_ROW.search(nar):
            e.append("sar.narrative must stand alone (no attachments / tables / tabs)")
        if f"${float(sar['total_amount_usd']):,.2f}" not in nar:
            e.append("sar.narrative must write the total amount")
        if not sar["subjects"]:
            e.append("sar.subjects empty")
        for must in ((meta or {}).get("customer_id"), (meta or {}).get("card_id")):
            if must and must not in sar["subjects"]:
                e.append(f"sar.subjects must include {must}")
        if not all(s in nar for s in sar["subjects"]):
            e.append("sar.subjects must each be named in the narrative")
        if "ofac" not in nar.lower():
            e.append("sar.narrative must state the OFAC screening result")
        if not re.search(r"prior (suspicious activity )?report|no prior|previously (filed|reported)", nar.lower()):
            e.append("sar.narrative must state the prior-report status")
        if re.search(r"simulated|assumed \(|assumed:", nar, re.I):
            e.append("sar.narrative must not mention simulated / assumed replies")
    else:
        if sar["narrative"] or sar["subjects"] or sar["total_amount_usd"] or sar["activity_dates"]:
            e.append("sar not filed ⇒ empty narrative/subjects/amount/dates")
    return e


def _validate_sar(answer: dict, db, meta: dict | None, affected_ts: list[str]) -> list[str]:
    c, sar, fin = answer["case"], answer["sar"], [a["action"] for a in answer["next_best_actions"]["final"]]
    try:
        from rag.validate_sar import validate_sar as _rag_validate
    except Exception:
        return _validate_sar_local(sar, c, fin, meta)
    ids_from_subjects = [s for s in sar.get("subjects", []) if _CARD.fullmatch(str(s))]
    card_id = (meta or {}).get("card_id") or (ids_from_subjects[0] if ids_from_subjects else "")
    return [
        f"sar: {x}"
        for x in _rag_validate(
            sar,
            exposure_usd=float(c["exposure_usd"]),
            affected_ts=affected_ts or None,
            case_ids=(answer.get("case_id", ""), c.get("graph_case_id", "")),
            customer_id=(meta or {}).get("customer_id", ""),
            card_id=card_id,
            resolver=FactsResolver(db),
            file_report_in_final=("FILE_REPORT" in fin),
        )
    ]


TOP_KEYS = {"case_id", "case", "evidence_requests", "next_best_actions", "sar", "stop_reason", "tool_calls", "tokens", "latency_s"}
CASE_KEYS = {
    "status",
    "verdict",
    "fraud_probability",
    "pattern",
    "pattern_description",
    "affected_txn_ids",
    "first_suspicious_txn_id",
    "connected_card_ids",
    "connected_device_profiles",
    "exposure_usd",
    "evidence",
    "similar_prior_cases",
    "summary",
    "written_to_graph",
    "graph_case_id",
}
SAR_KEYS = {"file", "reason", "narrative", "subjects", "total_amount_usd", "activity_dates"}


def _exists(db, table: str, col: str, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    if table == "txc":
        vals = [int(i) for i in ids if str(i).isdigit()]
        if not vals:
            return set()
        df = db.q(f"SELECT TransactionID::VARCHAR id FROM txc WHERE TransactionID IN ({','.join('?' * len(vals))})", *vals)
    else:
        df = db.q(f"SELECT {col} AS id FROM {table} WHERE {col} IN ({','.join('?' * len(ids))})", *ids)
    return set(df.id.astype(str))


def _resolve(db, ids: list[str]) -> list[str]:
    """Every id must exist in the dataset: TransactionID, card id, customer id, CC- id, device profile string, email domain, region."""
    bad = []
    txn = [i for i in ids if str(i).isdigit()]
    cards = [i for i in ids if re.fullmatch(r"C\d{5}-K\d", str(i))]
    custs = [i for i in ids if re.fullmatch(r"C\d{5}", str(i))]
    ccs = [i for i in ids if str(i).startswith("CC-")]
    rest = [i for i in ids if i not in txn + cards + custs + ccs]
    bad += [i for i in txn if i not in _exists(db, "txc", "id", txn)]
    bad += [i for i in cards if i not in _exists(db, "card_feat", "id", cards)]
    bad += [i for i in custs if i not in _exists(db, "customer_feat", "id", custs)]
    bad += [i for i in ccs if i not in _exists(db, "cc", "case_id", ccs)]
    for i in rest:
        s = str(i)
        if s.startswith("AC-"):
            bad.append(s + " (agent-case id: never in entity_ids)")
        elif " | " in s:
            if not _exists(db, "device_profile", "id", [s]):
                bad.append(s)
        elif re.fullmatch(r"[\w.-]+\.\w+", s):
            if not len(db.q("SELECT 1 FROM txc WHERE p_email = ? OR r_email = ? LIMIT 1", s, s)):
                bad.append(s)
        elif re.fullmatch(r"\d+\.\d", s):
            if not len(db.q("SELECT 1 FROM txc WHERE addr1 = ? LIMIT 1", s)):
                bad.append(s)
        else:
            bad.append(s)
    return bad


def validate(answer: dict, db, meta: dict | None = None) -> list[str]:
    e: list[str] = []
    if set(answer) != TOP_KEYS:
        e.append(f"top-level keys mismatch: missing {TOP_KEYS - set(answer)}, extra {set(answer) - TOP_KEYS}")
        return e
    c, sar, nba = answer["case"], answer["sar"], answer["next_best_actions"]
    if set(c) != CASE_KEYS:
        e.append(f"case keys mismatch: missing {CASE_KEYS - set(c)}, extra {set(c) - CASE_KEYS}")
    if set(sar) != SAR_KEYS:
        e.append(f"sar keys mismatch: {set(sar) ^ SAR_KEYS}")
    if set(nba) != {"initial", "final", "what_changed"}:
        e.append("next_best_actions keys mismatch")
    if e:
        return e
    # enums and types
    if c["status"] not in STATUSES:
        e.append(f"bad status {c['status']}")
    if c["verdict"] not in VERDICTS:
        e.append(f"bad verdict {c['verdict']}")
    if c["pattern"] not in PATTERNS:
        e.append(f"bad pattern {c['pattern']}")
    if not isinstance(c["fraud_probability"], (int, float)) or not 0 <= c["fraud_probability"] <= 1:
        e.append("fraud_probability out of [0,1]")
    if not isinstance(c["written_to_graph"], bool):
        e.append("written_to_graph must be bool")
    for k in ("affected_txn_ids", "connected_card_ids", "connected_device_profiles", "similar_prior_cases"):
        if not isinstance(c[k], list) or not all(isinstance(x, str) for x in c[k]):
            e.append(f"{k} must be a list of strings")
    if not isinstance(c["first_suspicious_txn_id"], str):
        e.append("first_suspicious_txn_id must be a string")
    for i, ev in enumerate(c["evidence"]):
        if set(ev) != {"claim", "source", "ref", "entity_ids"}:
            e.append(f"evidence[{i}] keys")
        elif ev["source"] not in ("graph", "document", "customer", "external"):
            e.append(f"evidence[{i}] bad source {ev['source']}")
    for i, r in enumerate(answer["evidence_requests"]):
        if set(r) != {"type", "asked_after_step", "assumed_response"} or r["type"] not in REQUEST_TYPES or not isinstance(r["asked_after_step"], int):
            e.append(f"evidence_requests[{i}] malformed")
    for stage in ("initial", "final"):
        for i, a in enumerate(nba[stage]):
            if set(a) != {"action", "route", "reason"}:
                e.append(f"{stage}[{i}] keys")
            elif a["action"] not in ACTIONS or a["route"] not in ("auto", "L1", "L2") or not a["reason"]:
                e.append(f"{stage}[{i}] bad action/route/reason")
    for k in ("tool_calls", "tokens"):
        if not isinstance(answer[k], int):
            e.append(f"{k} must be int")
    if not isinstance(answer["latency_s"], (int, float)):
        e.append("latency_s must be a number")
    if not (2 <= len(re.findall(r"[.!?](\s|$)", c["summary"])) <= 12):
        e.append("summary should be two to six sentences")
    # ids exist in the dataset
    ids = list(c["affected_txn_ids"]) + list(c["connected_card_ids"]) + list(c["connected_device_profiles"]) + list(c["similar_prior_cases"]) + list(sar["subjects"])
    if c["first_suspicious_txn_id"]:
        ids.append(c["first_suspicious_txn_id"])
    for ev in c["evidence"]:
        ids += list(ev.get("entity_ids", []))
    bad = _resolve(db, sorted(set(ids)))
    if bad:
        e.append(f"ids not in dataset: {bad[:10]}")
    if any(not i.startswith("CC-") for i in c["similar_prior_cases"]):
        e.append("similar_prior_cases must be CC- ids")
    # episode checks
    affected_ts: list[str] = []
    if c["affected_txn_ids"]:
        vals = [int(i) for i in c["affected_txn_ids"] if i.isdigit()]
        rows = db.q(f"SELECT TransactionID::VARCHAR id, card_id, ts, abs(amt) amt FROM txc WHERE TransactionID IN ({','.join('?' * len(vals))})", *vals)
        affected_ts = [str(r.ts) for r in rows.itertuples()]
        cards_ok = set([meta.get("card_id")] if meta and meta.get("card_id") else []) | set(c["connected_card_ids"])
        if meta and meta.get("card_id") and not all(r.card_id in cards_ok for r in rows.itertuples()):
            e.append("affected transaction not on the case card or a connected card")
        if meta and meta.get("opened_at") and any(str(r.ts) > meta["opened_at"] for r in rows.itertuples()):
            e.append("affected transaction after opened_at (time-box)")
        if abs(round(float(rows.amt.sum()), 2) - float(c["exposure_usd"])) > 0.011:
            e.append(f"exposure_usd {c['exposure_usd']} != recomputed {round(float(rows.amt.sum()), 2)}")
        if c["first_suspicious_txn_id"] != min(c["affected_txn_ids"], key=int):
            e.append("first_suspicious_txn_id is not the earliest affected transaction")
    # invariants (PLAN §4.7)
    fin = [a["action"] for a in nba["final"]]
    ini = [a["action"] for a in nba["initial"]]
    if sar["file"] != ("FILE_REPORT" in fin):
        e.append("sar.file must agree with FILE_REPORT in final")
    if "FILE_REPORT" in fin and "CREATE_CASE" not in fin:
        e.append("FILE_REPORT without CREATE_CASE")
    if c["verdict"] == "legitimate":
        if c["affected_txn_ids"] or c["exposure_usd"] != 0 or sar["file"] or c["first_suspicious_txn_id"]:
            e.append("legitimate ⇒ empty episode, exposure 0, no SAR")
        if "CLOSE_NO_FRAUD" not in fin:
            e.append("legitimate ⇒ CLOSE_NO_FRAUD in final")
        if any(a in ("BLOCK_CARD", "BLOCK_ALL_CARDS") for a in fin):
            e.append("legitimate ⇒ no block in final")
        if any(a in ("BLOCK_CARD", "BLOCK_ALL_CARDS") for a in ini) and not (answer["evidence_requests"] and ini != fin):
            e.append("legitimate with a block in initial ⇒ an evidence request must have withdrawn it")  # D1 + D5 (passed step-up)
        if c["pattern"] != "none":
            e.append("legitimate ⇒ pattern none")
    if "BLOCK_CARD" in fin and c["verdict"] == "legitimate":
        e.append("BLOCK_CARD with legitimate verdict")
    if c["verdict"] == "fraud":  # D10
        if {"CLOSE_NO_FRAUD", "ALLOW_TRANSACTION"} & set(fin):
            e.append("fraud ⇒ no CLOSE_NO_FRAUD / ALLOW_TRANSACTION in final")
        if not (FRAUD_CONTAINMENT & set(fin)):
            e.append("fraud ⇒ at least one of BLOCK_CARD / DECLINE_TRANSACTION / ESCALATE_TO_ANALYST / BLOCK_ALL_CARDS in final")
    if "CLOSE_NO_FRAUD" in fin and c["verdict"] != "legitimate":
        e.append("CLOSE_NO_FRAUD in final ⇒ verdict legitimate")
    if c["verdict"] == "uncertain":
        if uncertain_block_violation(nba["initial"]):
            e.append("uncertain ⇒ no block in initial (except an R2 BLOCK_CARD on a customer denial)")
        if c["status"] not in ("escalated", "open"):
            e.append("uncertain ⇒ status escalated or open")
    if c["verdict"] != "legitimate" and not c["affected_txn_ids"]:
        e.append("non-legitimate verdict needs affected_txn_ids (at least the flagged)")
    if (nba["what_changed"] == "nothing") != (ini == fin):
        e.append("what_changed == 'nothing' ⇔ initial == final")
    if ini != fin and not answer["evidence_requests"]:
        e.append("initial ≠ final ⇒ evidence_requests non-empty")
    if (c["pattern_description"] != "") != (c["pattern"] == "undocumented"):
        e.append("pattern_description ⇔ undocumented")
    if not sar["reason"]:
        e.append("sar.reason empty")
    e += _validate_sar(answer, db, meta, affected_ts)
    if "ESCALATE_TO_ANALYST" in fin and c["status"] != "escalated":
        e.append("ESCALATE_TO_ANALYST in final ⇒ status escalated")
    # route table
    for stage in ("initial", "final"):
        for a in nba[stage]:
            exp = (
                "L2"
                if a["action"] == "BLOCK_CARD" and c["exposure_usd"] > 2500
                else "L1"
                if a["action"] in ("DECLINE_TRANSACTION", "BLOCK_CARD")
                else "L2"
                if a["action"] in ("BLOCK_ALL_CARDS", "FILE_REPORT")
                else "auto"
            )
            if a["route"] != exp:
                e.append(f"{stage} {a['action']} route {a['route']} != {exp}")
    # R1 lint (needs the pre-evidence probability)
    if meta and "p_pre" in meta:
        for a in nba["initial"]:
            if re.search(r"\bR1\b(?!\s*(not|does not|is not|n/a))", a["reason"]) and not (meta["p_pre"] < 0.70 and meta.get("families_fraud_pre", 0) < 2):
                e.append(f"initial {a['action']} cites R1 at p_pre={meta['p_pre']} families={meta.get('families_fraud_pre')}")
    # seed-tell and Kaggle lints over every free-text field
    texts = (
        [c["summary"], c["pattern_description"], sar["narrative"], sar["reason"], answer["stop_reason"], nba["what_changed"]]
        + [ev["claim"] for ev in c["evidence"]]
        + [a["reason"] for a in nba["initial"] + nba["final"]]
        + [r["assumed_response"] for r in answer["evidence_requests"]]
    )
    for t in texts:
        if SEED_TELL.search(t):
            e.append(f"seed tell in text: {t[:80]!r}")
        if KAGGLE.search(t):
            e.append(f"Kaggle/outcome reference in text: {t[:80]!r}")
    return e
