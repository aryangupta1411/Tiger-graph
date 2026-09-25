"""The single SAR validator. Returns a list of
violations; [] == valid. engine.validator, agent (P8) and ui.common.sar_checks all delegate here - do not copy
these rules elsewhere.

    from rag.validate_sar import validate_sar, validate_answer_sar, DuckResolver, StaticResolver
    errs = validate_sar(sar, exposure_usd=187.33, affected_ts=["2016-11-15 20:30:00", ...],
                        case_ids=("HHG-014", "AC-HHG-014"), customer_id="C13487", card_id="C13487-K1",
                        resolver=DuckResolver("data/hhgoa.duckdb"))
    errs = validate_answer_sar(answer, customer_id=..., card_id=..., resolver=...)   # derives the rest from the answer
    uv run python -m rag.validate_sar answer.json [data/hhgoa.duckdb]                 # CLI over an answer file

Checks when sar.file is true:
  V01 narrative present, single paragraph (no newlines, tabs)
  V02 6 <= sentences <= 12
  V03 >= 1 ISO date (YYYY-MM-DD) and >= 1 currency amount ($n.nn)
  V04 both activity_dates appear verbatim in the narrative (D10: the former "transaction dates non-decreasing"
      rule is dropped - a narrative legitimately re-mentions dates as context, e.g. HHG-014's wave window)
  V05 activity_dates == [min, max] of the affected transactions (when affected_ts given), both ISO, first <= last
  V06 total_amount_usd == exposure_usd (+-0.005) and that amount is written in the narrative
  V07 subjects non-empty, include customer_id and card_id, each appears verbatim in the narrative, each resolves
  V08 every C-/card/CC-/transaction id in the narrative resolves against the dataset; AC- ids allowed only as the
      case's own graph_case_id
  V09 forbidden text: 'see attached', 'see attachment', markdown headings/bullets, 'simulated', 'ASSUMED'
  V10 the internal case id (graph_case_id or case_id) is named
  V11 OFAC screening result stated; prior-report status stated
  V12 seed-tell lint: no mention of ':00 seconds' / 'seconds equal to zero' / 'seeded'
  V13 no tables: a '|' is allowed inside a device-profile string ("SM-G935F ... | Android 7.0 | ..."); only a
      leading-pipe table row (`^|` / `\n|`), a `|---` rule line or a tab is rejected (D10)
When sar.file is false: narrative == "", subjects == [], total_amount_usd == 0, activity_dates == [], reason cites a rule.

Fact checks (V14-V19, `check_sar_facts`) - run when the caller passes `fact_context` (the agent's P8 re-prompt loop,
agent.validator, validate_answer_sar and so the UI); they need to know what was actually done and what was known:
  V14 a pending (L1/L2-routed) or absent block is described as done ("blocked card X", "the card has been blocked")
  V15 gendered pronouns / honorifics (the dataset has no gender)
  V16 a time-zone claim (UTC, GMT, EST, "time zone"): the dataset timestamps carry no zone
  V17 a card id, customer id or known device-profile string named in the narrative is missing from subjects
  V18 a "no prior report" claim contradicted by prior reports on the card (card scope) or on the device profile /
      connected cards (related scope)
  V19 card statistics that are not as of opened_at: a history count above the as-of count, or a baseline
      maximum / median that differs from the as-of value
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Protocol

ISO_DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
AMOUNT = re.compile(r"\$\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\$\d+(?:\.\d{2})?")
CARD_ID = re.compile(r"\bC\d{5}-K\d\b")
CUST_ID = re.compile(r"\bC\d{5}\b(?!-K)")
CC_ID = re.compile(r"\bCC-\d{4}\b")
AC_ID = re.compile(r"\bAC-[A-Z]{3}-\d{3}\b")
TXN_ID = re.compile(r"\b3[05]\d{5}\b")
FORBIDDEN = ["see attached", "see attachment", "simulated", "assumed (", "assumed:", "```"]
# table markup: a row starting with '|' (at the start or after a line break), a '|---' rule, or a tab (D10 narrative regex)
TABLE_MARKUP = re.compile(r"(?:^|\n)\s*\||\|\s*-{3,}|\t")
SEED_TELL = re.compile(r"(:00 seconds|seconds (equal to|are) zero|seeded|seed tell|synthetic row)", re.I)
RULE_CITE = re.compile(r"\b(R\d{1,2}|3a|3b)\b")
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")


class Resolver(Protocol):
    def exists(self, kind: str, ident: str) -> bool: ...   # kind in customer | card | closed_case | txn | device


class StaticResolver:
    def __init__(self, ids: dict[str, set[str]]):
        self.ids = ids

    def exists(self, kind: str, ident: str) -> bool:
        return ident in self.ids.get(kind, set())


class DuckResolver:
    """Resolves ids against data/hhgoa.duckdb (tables tx, idn, cc); card ids via the schema.md card rule."""

    def __init__(self, db_path: str | Path):
        import duckdb

        self.con = duckdb.connect(str(db_path), read_only=True)
        self.con.execute("""create temp table _cardmap as
            select customer_id || '-K' || dense_rank() over (partition by customer_id order by coalesce(card6,'')) as card_id
            from (select distinct customer_id, card6 from tx)""")
        self._cache: dict[tuple[str, str], bool] = {}

    def exists(self, kind: str, ident: str) -> bool:
        key = (kind, ident)
        if key in self._cache:
            return self._cache[key]
        q = {
            "customer": ("select 1 from tx where customer_id = ? limit 1", ident),
            "card": ("select 1 from _cardmap where card_id = ? limit 1", ident),
            "closed_case": ("select 1 from cc where case_id = ? limit 1", ident),
            "txn": ("select 1 from tx where TransactionID = ? limit 1", int(ident) if ident.isdigit() else -1),
            "device": ("""select 1 from idn where coalesce(DeviceInfo,'NULL') || ' | ' || coalesce(id_30,'NULL') || ' | ' ||
                          coalesce(id_31,'NULL') || ' | ' || coalesce(id_33,'NULL') = ? limit 1""", ident),
        }[kind]
        ok = self.con.execute(q[0], [q[1]]).fetchone() is not None
        self._cache[key] = ok
        return ok


def sentences(text: str) -> list[str]:
    return [s for s in _SENT.split(text.strip()) if s.strip()]


# ----------------------------------------------------------------------------- fact checks (V14-V19)

# V14: a block described as already done. Only past/perfect/present-state forms are claims; "be blocked",
# "to block", "block ... pending approval", "has not been blocked" are not matched.
_DONE_STATE = re.compile(
    r"\b(?:has|have|had|was|were|is|are)\s+(?:now\s+|already\s+|since\s+)?(?:been\s+)?(?:now\s+|already\s+)?"
    r"(?:blocked|frozen|suspended|deactivated|reissued|cancell?ed)\b", re.I)
_DONE_VERB_CARD = re.compile(
    r"\b(?:blocked|froze|frozen|suspended|deactivated|cancell?ed|closed|reissued)\s+(?:the\s+|all\s+|both\s+|its\s+)?"
    r"(?:customer's\s+|cardholder's\s+)?cards?\b", re.I)
_CARD_STATE = re.compile(
    r"\bcards?(?:\s+C\d{5}-K\d)?\s+(?:(?:was|were|is|are|has been|have been)\s+(?:now\s+|already\s+)?closed|blocked)\b", re.I)
_EXECUTED_BLOCK = {"BLOCK_CARD", "BLOCK_ALL_CARDS"}
_HISTORIC_CASE = re.compile(r"\b(?:closed|prior|earlier|previous) (?:fraud )?cases?\b", re.I)
GENDERED = re.compile(r"\b(?:[Ss]he|[Hh]ers?|[Hh]erself|[Hh]e|[Hh]im|[Hh]is|[Hh]imself|Mr|Mrs|Ms)\b")
TIME_ZONE = re.compile(r"\b(?:UTC|GMT|EST|EDT|CST|CDT|MST|MDT|PST|PDT|CET|CEST|BST|IST|AEST|JST)\b|\btime[ -]?zone\b|\bZulu\b")
# V18: "no prior report" style claims
_NO_PRIOR = re.compile(
    r"\bno\s+(?:prior|previous|earlier|other)\s+(?:suspicious activity reports?|SARs?|reports?|filings?)\b"
    r"|\bno\s+(?:suspicious activity reports?|SARs?|reports?)\s+(?:has|have|had|was|were)\s+(?:ever\s+|previously\s+|yet\s+)?(?:been\s+)?(?:filed|submitted|made)\b"
    r"|\b(?:has|have|had|was|were)\s+not\s+(?:previously\s+|yet\s+)?(?:been\s+)?(?:previously\s+)?(?:reported|the subject of (?:a|any) (?:prior |previous )?(?:SAR|suspicious activity report|report))\b"
    r"|\bnever\s+(?:been\s+)?(?:reported|the subject of)\b", re.I)
_CLAUSE_END = re.compile(r";|\.(?=\s|$)|,\s|\s(?:and|but|while|whereas|although|however)\s")
_SCOPE_CARD = re.compile(r"\b(?:card|account|cardholder|customer)\b|\bC\d{5}(?:-K\d)?\b", re.I)
_SCOPE_RELATED = re.compile(r"device|profile|\bring\b|connected|other cards|\bwave\b|same (?:pattern|shape)", re.I)
# V19: history statistics
_HISTORY_WORDS = re.compile(r"\b(?:history|prior|previous|baseline|account|cardholder|since|lifetime|established)\b", re.I)
_NOT_CARD_HISTORY = re.compile(r"device|profile|\bring\b|\bwave\b|other cards|closed cases|look-?alike|connected", re.I)
_TXN_COUNT = re.compile(r"\b(\d{1,3}(?:,\d{3})*|\d+)(?:-|\s+)(?:prior\s+|previous\s+|total\s+|earlier\s+|recorded\s+)?transactions?\b", re.I)
_MAX_AMT = re.compile(r"\b(?:maximum|max|largest|highest)\b[^$.;]{0,40}?\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)", re.I)
_MED_AMT = re.compile(r"\bmedian\b[^$.;]{0,40}?\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)", re.I)


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _prose_sentences(nar: str) -> list[str]:
    # split on a period followed by whitespace so '7.0' / '62.0' inside device strings do not split
    return [s for s in re.split(r"(?<=[.!?])\s+", nar) if s.strip()]


def check_sar_facts(sar: dict, *, final_actions: list[dict] | None = None, known_devices: list[str] | None = None,
                    prior_reports_card: list[str] | None = None, prior_reports_related: list[str] | None = None,
                    baseline_as_of: dict | None = None, affected_amounts: list[float] | None = None) -> list[str]:
    """V14-V19 on a filed SAR. [] when sar.file is false or every check passes.

    final_actions      the answer's next_best_actions.final ({action, route}); a block counts as done only when it
                       is routed `auto` (it never is: BLOCK_CARD is L1/L2), so any "blocked" claim is a V14.
    known_devices      device-profile strings the case knows (connected profiles, the affected transactions' devices,
                       evidence entity ids); each one named in the narrative must be a subject.
    prior_reports_*    CC- ids of prior closed cases with a report filed, on this card / on the device profile or
                       connected cards; a "no prior report" clause in that scope is a V18.
    baseline_as_of     {"n_prior_txns": int, "max_amt": float, "median_amt": float} as of the flagged transaction
                       (case_context.txn card_seq - 1 / prior_max_amt / prior_med_amt), optionally with the
                       *_at_opening values of a time-boxed card_profile; None skips V19.
    affected_amounts   episode amounts (a "maximum" equal to an episode amount is not a baseline claim).
    """
    if not sar.get("file"):
        return []
    nar = " ".join(str(sar.get("narrative", "")).split())
    if not nar:
        return []
    errs: list[str] = []
    final_actions = final_actions or []
    executed = {a.get("action") for a in final_actions if a.get("route") == "auto"}
    if not (_EXECUTED_BLOCK & executed):
        # a sentence about the bank's earlier closed cases may report what was done then; only this case's claims count
        claims = [m for s in _prose_sentences(nar) if not (CC_ID.search(s) or _HISTORIC_CASE.search(s))
                  for rx in (_DONE_STATE, _DONE_VERB_CARD, _CARD_STATE) for m in [rx.search(s)] if m]
        if claims:
            pend = [f"{a.get('action')} ({a.get('route')})" for a in final_actions if a.get("action") in _EXECUTED_BLOCK]
            errs.append(f"V14 narrative states the card was blocked/closed ({claims[0].group(0)!r}) but the block is "
                        + (f"only recommended and awaiting approval: {', '.join(pend)}" if pend else "not among the final actions")
                        + "; write 'a card block is recommended, pending team-lead approval'")
    g = GENDERED.search(nar)
    if g:
        errs.append(f"V15 gendered pronoun or honorific {g.group(0)!r}: the dataset carries no gender; write 'the cardholder' / 'the customer'")
    tz = TIME_ZONE.search(nar)
    if tz:
        errs.append(f"V16 time-zone claim {tz.group(0)!r}: the dataset timestamps carry no time zone; give the date and time only")
    subs = set(sar.get("subjects", []) or [])
    missing = [i for i in dict.fromkeys(CARD_ID.findall(nar) + CUST_ID.findall(nar)) if i not in subs]
    missing += [d for d in dict.fromkeys(known_devices or []) if d and " | " in d and d in nar and d not in subs]
    if missing:
        errs.append(f"V17 ids named in the narrative are missing from subjects: {missing}")
    card_r, rel_r = list(prior_reports_card or []), list(prior_reports_related or [])
    if card_r or rel_r:
        for m in _NO_PRIOR.finditer(nar):
            end = _CLAUSE_END.search(nar, m.end())
            clause = nar[m.start(): end.start() if end else len(nar)]
            on_card, on_rel = bool(_SCOPE_CARD.search(clause)), bool(_SCOPE_RELATED.search(clause))
            if not on_card and not on_rel:
                on_card = on_rel = True
            hit = (card_r if on_card else []) + (rel_r if on_rel else [])
            if hit:
                errs.append(f"V18 prior-report claim {clause!r} contradicts the evidence: reports were filed on "
                            f"{', '.join(sorted(set(hit)))}")
                break
    if baseline_as_of:
        errs += _baseline_errors(nar, baseline_as_of, affected_amounts)
    return errs


def _baseline_errors(nar: str, b: dict, affected_amounts: list[float] | None) -> list[str]:
    """V19. Accepted values: the counts / median / maximum before the flagged transaction (`n_prior_txns`,
    `median_amt`, `max_amt`) and, when the caller has a time-boxed card profile, the same statistics at opening
    (`n_txns_at_opening`, `median_amt_at_opening`, `max_amt_at_opening`). A maximum or median qualified by a channel,
    a region or a period ('the in-person maximum', 'monthly median') is a different statistic and is not checked."""
    def vals(*keys: str) -> list[float]:
        out = []
        for k in keys:
            try:
                if b.get(k) is not None and float(b[k]) >= 0:
                    out.append(round(float(b[k]), 2))
            except (TypeError, ValueError):
                pass
        return out

    amts = {round(float(a), 2) for a in (affected_amounts or [])}
    counts = vals("n_prior_txns", "n_txns_at_opening")
    limit = max([counts[0] + len(affected_amounts or [])] + counts[1:]) if counts else None
    maxes, meds = vals("max_amt", "max_amt_at_opening"), vals("median_amt", "median_amt_at_opening")
    qualified = re.compile(r"in[- ]person|online|card[- ]not[- ]present|region|daily|weekly|monthly|per day|30[- ]day|single", re.I)
    qualified_before = re.compile(r"(?:in[- ]person|online|card[- ]not[- ]present|regional|daily|weekly|monthly|30[- ]day|single[- ]day)\s+$", re.I)
    errs: list[str] = []
    for s in _prose_sentences(nar):
        if not _HISTORY_WORDS.search(s):
            continue

        def other_entity(m: re.Match, s: str = s) -> bool:
            # the statistic belongs to a device / ring / other cards when one is named before it or in its own clause
            # ("the ring profile appears in 114 transactions"); a device named in a LATER clause does not exempt the
            # card's own history (the live HHG-014 / HHG-006 sentences end with "... a brand-new mobile profile")
            end = re.compile(r"[,;]|\s(?:and|but|while|whereas)\s").search(s, m.end())
            return bool(_NOT_CARD_HISTORY.search(s[: end.start() if end else len(s)]))

        if limit is not None:
            for m in _TXN_COUNT.finditer(s):
                if other_entity(m):
                    continue
                if _num(m.group(1)) > limit:
                    errs.append(f"V19 card history count {m.group(0)!r} exceeds the {int(limit)} transactions on the card as of "
                                "opening (it includes activity after opened_at); quote the as-of count")
                    break
        for rx, allowed, what in ((_MAX_AMT, maxes, "maximum"), (_MED_AMT, meds, "median")):
            if not allowed or max(allowed) <= 0:
                continue
            for m in rx.finditer(s):
                # a different statistic: 'in-person maximum', 'monthly median', 'maximum online purchase of $x'
                if other_entity(m) or qualified.search(m.group(0)) or qualified_before.search(s[max(0, m.start() - 20): m.start()]):
                    continue
                x = round(_num(m.group(1)), 2)
                if x not in amts and all(abs(x - v) > 0.005 for v in allowed):
                    errs.append(f"V19 baseline {what} ${x:,.2f} is not the card's as-of {what} "
                                f"({' / '.join(f'${v:,.2f}' for v in allowed)})")
                    break
    return errs


def validate_sar(sar: dict, *, exposure_usd: float, affected_ts: list[str] | None = None,
                 case_ids: tuple[str, str] = ("", ""), customer_id: str = "", card_id: str = "",
                 resolver: Resolver | None = None, file_report_in_final: bool | None = None,
                 fact_context: dict | None = None) -> list[str]:
    """V00-V13 always; V14-V19 (`check_sar_facts(sar, **fact_context)`) when `fact_context` is given."""
    errs = _validate_sar_core(sar, exposure_usd=exposure_usd, affected_ts=affected_ts, case_ids=case_ids,
                              customer_id=customer_id, card_id=card_id, resolver=resolver,
                              file_report_in_final=file_report_in_final)
    if fact_context is not None and all(k in sar for k in ("file", "narrative", "subjects")):
        errs += check_sar_facts(sar, **fact_context)
    return errs


def _validate_sar_core(sar: dict, *, exposure_usd: float, affected_ts: list[str] | None = None,
                       case_ids: tuple[str, str] = ("", ""), customer_id: str = "", card_id: str = "",
                       resolver: Resolver | None = None, file_report_in_final: bool | None = None) -> list[str]:
    errs: list[str] = []
    for k in ("file", "reason", "narrative", "subjects", "total_amount_usd", "activity_dates"):
        if k not in sar:
            errs.append(f"V00 missing key sar.{k}")
    if errs:
        return errs
    if not str(sar["reason"]).strip():
        errs.append("V00 sar.reason must be non-empty and cite the rule")
    elif not RULE_CITE.search(str(sar["reason"])):
        errs.append("V00 sar.reason must cite a policy rule (R1-R10, 3a)")
    if file_report_in_final is not None and bool(sar["file"]) != bool(file_report_in_final):
        errs.append("V00 sar.file must agree with FILE_REPORT in next_best_actions.final")

    if not sar["file"]:
        if sar["narrative"] != "":
            errs.append("V00 file=false requires narrative == ''")
        if sar["subjects"] != []:
            errs.append("V00 file=false requires subjects == []")
        if float(sar["total_amount_usd"] or 0) != 0:
            errs.append("V00 file=false requires total_amount_usd == 0")
        if sar["activity_dates"] != []:
            errs.append("V00 file=false requires activity_dates == []")
        return errs

    nar = str(sar["narrative"])
    if not nar.strip():
        return errs + ["V01 narrative is empty"]
    if "\n" in nar or "\t" in nar:
        errs.append("V01 narrative must be one paragraph: no newlines or tabs")
    n_sent = len(sentences(nar))
    if not 6 <= n_sent <= 12:
        errs.append(f"V02 narrative has {n_sent} sentences; must be 6-12")
    dates = ISO_DATE.findall(nar)
    if not dates:
        errs.append("V03 narrative has no ISO date (YYYY-MM-DD)")
    if not AMOUNT.search(nar):
        errs.append("V03 narrative has no currency amount")

    ad = sar["activity_dates"]
    if not (isinstance(ad, list) and len(ad) == 2 and all(ISO_DATE.fullmatch(str(d)) for d in ad)):
        errs.append("V05 activity_dates must be two YYYY-MM-DD strings")
    else:
        if ad[0] > ad[1]:
            errs.append("V05 activity_dates first > last")
        if affected_ts:
            ds = sorted(t[:10] for t in affected_ts)
            if [ds[0], ds[-1]] != list(ad):
                errs.append(f"V05 activity_dates {ad} != affected min/max {[ds[0], ds[-1]]}")
        # D10: the chronological-order rule is gone; the narrative must simply name both activity dates
        if ad[0] not in nar or ad[1] not in nar:
            errs.append(f"V04 both activity dates {ad} must appear in the narrative")

    total = float(sar["total_amount_usd"])
    if abs(total - float(exposure_usd)) > 0.005:
        errs.append(f"V06 total_amount_usd {total} != exposure_usd {exposure_usd}")
    if f"${total:,.2f}" not in nar:
        errs.append(f"V06 total amount ${total:,.2f} is not written in the narrative")

    subs = list(sar["subjects"])
    if not subs:
        errs.append("V07 subjects is empty")
    for must in (customer_id, card_id):
        if must and must not in subs:
            errs.append(f"V07 subjects must include {must}")
    for s in subs:
        if s not in nar:
            errs.append(f"V07 subject {s!r} is not named in the narrative")
        if resolver is not None:
            kind = ("card" if CARD_ID.fullmatch(s) else "customer" if CUST_ID.fullmatch(s) else
                    "closed_case" if CC_ID.fullmatch(s) else "device" if " | " in s else "txn" if TXN_ID.fullmatch(s) else "")
            if not kind or not resolver.exists(kind, s):
                errs.append(f"V07 subject {s!r} does not resolve against the dataset")

    if resolver is not None:
        for rx, kind in ((CARD_ID, "card"), (CUST_ID, "customer"), (CC_ID, "closed_case"), (TXN_ID, "txn")):
            for ident in sorted(set(rx.findall(nar))):
                if not resolver.exists(kind, ident):
                    errs.append(f"V08 {kind} id {ident} in narrative does not exist in the dataset")
    for ident in sorted(set(AC_ID.findall(nar))):
        if ident != case_ids[1]:
            errs.append(f"V08 agent case id {ident} may not appear (only the case's own graph_case_id {case_ids[1]!r})")

    low = nar.lower()
    for f in FORBIDDEN:
        if f in low:
            errs.append(f"V09 forbidden text {f!r}")
    if re.search(r"(^|\s)(#{1,6}|\*|-)\s", nar):
        errs.append("V09 markdown heading/bullet characters in narrative")
    if TABLE_MARKUP.search(nar):
        # a '|' inside a device-profile string is fine; a leading-pipe row, a '|---' rule or a tab is a table
        errs.append("V13 table markup in narrative (leading '|' row, '|---' rule or tab); prose only")

    if not any(cid and cid in nar for cid in case_ids):
        errs.append("V10 narrative must name the internal case id")
    if "ofac" not in low:
        errs.append("V11 narrative must state the OFAC screening result")
    if not re.search(r"prior (suspicious activity )?report|no prior|previously (filed|reported)", low):
        errs.append("V11 narrative must state the prior-report status")
    if SEED_TELL.search(nar):
        errs.append("V12 narrative mentions the seeding tell")
    return errs


def answer_resolver(answer: dict, customer_id: str = "", card_id: str = "") -> StaticResolver:
    """A StaticResolver over the ids the answer itself names (affected transactions, connected cards, device profiles,
    similar prior cases, the case's customer/card). Enough for a UI badge or an offline check; the promote-time
    validator resolves against data/ids.duckdb (DuckResolver / agent.validator) instead."""
    case = answer.get("case", {}) or {}
    ids = {
        "txn": set(map(str, case.get("affected_txn_ids", []) or [])),
        "card": set(case.get("connected_card_ids", []) or []),
        "device": set(case.get("connected_device_profiles", []) or []),
        "closed_case": set(case.get("similar_prior_cases", []) or []),
        "customer": set(),
    }
    if case.get("first_suspicious_txn_id"):
        ids["txn"].add(str(case["first_suspicious_txn_id"]))
    if card_id:
        ids["card"].add(card_id)
    if customer_id:
        ids["customer"].add(customer_id)
    for s in (answer.get("sar", {}) or {}).get("subjects", []) or []:
        if CARD_ID.fullmatch(s):
            ids["card"].add(s)
            ids["customer"].add(s.split("-K")[0])
        elif CUST_ID.fullmatch(s):
            ids["customer"].add(s)
    for c in ids["card"]:
        ids["customer"].add(c.split("-K")[0])
    return StaticResolver(ids)


_REPORTED = re.compile(
    r"\breports?\s+(?:were\s+|was\s+|had\s+been\s+|have\s+been\s+)?filed\b|\breport_filed\s*[=:]\s*(?:true|yes|1)\b"
    r"|\b(?:SAR|suspicious activity report)s?\s+(?:was|were|had been|have been)\s+filed\b", re.I)
_NOT_REPORTED = re.compile(
    r"\bno\s+(?:prior\s+)?(?:SAR|report)s?\b|\bnone\s+(?:were\s+)?reported\b|report_filed\s*[=:]\s*(?:false|no|0)\b"
    r"|\bwithout\s+(?:a\s+)?(?:SAR|report)|\bnot\s+(?:been\s+)?(?:reported|filed)\b", re.I)


def reported_cases_from_evidence(evidence: list[dict], card_id: str = "") -> tuple[list[str], list[str]]:
    """(card-scope, related-scope) CC- ids that evidence claims say had a report filed. A claim about this card
    ('on this card' / the card id without a device or profile word) is card scope; every other claim is related."""
    card, rel = [], []
    for e in evidence or []:
        claim = str(e.get("claim", ""))
        if not _REPORTED.search(claim) or _NOT_REPORTED.search(claim):
            continue
        ids = [i for i in dict.fromkeys(CC_ID.findall(claim) + [x for x in e.get("entity_ids", []) or [] if CC_ID.fullmatch(str(x))])]
        on_card = (" this card" in claim.lower() or (card_id and card_id in claim)) and not _SCOPE_RELATED.search(claim)
        (card if on_card else rel).extend(i for i in ids if i not in card and i not in rel)
    return card, rel


def answer_fact_context(answer: dict, card_id: str = "") -> dict:
    """`fact_context` for check_sar_facts derived from a README-format answer alone (no as-of baseline: V19 is only
    applied in the agent's P8 loop, which has case_context's as-of statistics)."""
    case = answer.get("case", {}) or {}
    ev = case.get("evidence", []) or []
    devices = list(case.get("connected_device_profiles", []) or [])
    devices += [str(i) for e in ev for i in (e.get("entity_ids", []) or []) if " | " in str(i)]
    card_r, rel_r = reported_cases_from_evidence(ev, card_id)
    return {"final_actions": list((answer.get("next_best_actions", {}) or {}).get("final", []) or []),
            "known_devices": list(dict.fromkeys(devices)), "prior_reports_card": card_r, "prior_reports_related": rel_r}


def check_answer_sar_facts(answer: dict, card_id: str = "") -> list[str]:
    """V14-V19 only, over an answer file (agent.validator adds these to the engine validator's problems)."""
    return check_sar_facts(answer.get("sar", {}) or {}, **answer_fact_context(answer, card_id))


def validate_answer_sar(answer: dict, *, customer_id: str = "", card_id: str = "", affected_ts: list[str] | None = None,
                        resolver: Resolver | None = None) -> list[str]:
    """validate_sar over a README-format answer file: exposure, case ids and FILE_REPORT come from the answer.
    `customer_id` / `card_id` come from the case pack (the answer does not carry them); `affected_ts` from the facts
    when the caller has them. Without a resolver the ids are checked against the answer's own lists (answer_resolver).
    The V14-V19 fact checks run on the context the answer itself carries (answer_fact_context)."""
    case = answer.get("case", {}) or {}
    finals = [a.get("action") for a in (answer.get("next_best_actions", {}) or {}).get("final", []) or []]
    return validate_sar(answer.get("sar", {}) or {}, exposure_usd=float(case.get("exposure_usd", 0) or 0),
                        affected_ts=affected_ts, case_ids=(str(answer.get("case_id", "")), str(case.get("graph_case_id", ""))),
                        customer_id=customer_id, card_id=card_id,
                        resolver=resolver if resolver is not None else answer_resolver(answer, customer_id, card_id),
                        file_report_in_final=("FILE_REPORT" in finals),
                        fact_context=answer_fact_context(answer, card_id))


CHECK_NAMES = {
    "V00": "shape / reason cites a rule", "V01": "one paragraph, no newlines or tabs",
    "V02": "6-12 sentences", "V03": "an ISO date and a currency amount",
    "V04": "both activity dates named in the narrative", "V05": "activity_dates == affected min/max",
    "V06": "total_amount_usd == exposure and written out", "V07": "subjects named and resolvable",
    "V08": "every id in the narrative exists", "V09": "no forbidden text or markdown",
    "V10": "the internal case id is named", "V11": "OFAC result and prior-report status stated",
    "V12": "no seed tell", "V13": "prose only, no table markup",
    "V14": "a pending block is not described as done", "V15": "no gendered pronouns",
    "V16": "no time-zone claim", "V17": "every named id / device is a subject",
    "V18": "prior-report claim agrees with the evidence", "V19": "card statistics as of opened_at (P8 loop only)",
}


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 2
    from ops.console import Col, Table, fail, header, ok, summary

    path = Path(argv[0])
    ans = json.loads(path.read_text())
    db = argv[1] if len(argv) > 1 else None
    sar = ans.get("sar", {}) or {}
    case = ans.get("case", {}) or {}
    header("rag.validate_sar",
           "the single SAR validator (PLAN 4.8 / decisions D10) over one README-format answer",
           {"answer": str(path), "case_id": ans.get("case_id", "-"),
            "resolver": f"DuckResolver({db})" if db else "the answer's own ids (StaticResolver)",
            "sar.file": str(bool(sar.get("file"))), "checks": len(CHECK_NAMES),
            "exposure_usd": case.get("exposure_usd", "-")})
    errs = validate_answer_sar(ans, resolver=DuckResolver(db) if db else None)
    failed = {e.split(" ", 1)[0] for e in errs}
    filing = bool(sar.get("file"))
    t = Table(Col("check", width=5), Col("what it asserts", max_width=46),
              Col("result", width=6, align="center"), title="SAR checks",
              caption="" if filing else "sar.file is false: only the V00 shape rules apply "
                                        "(narrative '', subjects [], amount 0, dates [])")
    for code in sorted(CHECK_NAMES):
        if not filing and code != "V00":
            t.add_row(code, CHECK_NAMES[code], "n/a", style="dim")
            continue
        bad = code in failed
        t.add_row(code, CHECK_NAMES[code], "FAIL" if bad else "PASS", style="red" if bad else None)
    t.print()
    for e in errs:
        fail(e)
    if not errs:
        ok("sar: OK - every check passed")
    summary("rag.validate_sar complete",
            {"answer": str(path), "violations": len(errs),
             "checks failed": ", ".join(sorted(failed)) or "none"},
            status="fail" if errs else "ok")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
