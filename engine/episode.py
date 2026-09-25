"""Episode membership (`affected_txn_ids`) — PLAN §4.4.

members(chain, flagged_id, verdict, flags) -> ordered list of TransactionIDs (strings)

  chain   : episode_candidates.chain rows (ts <= opened_at, the <= 48 h-gap chain around the flagged txn)
  verdict : fraud | uncertain | legitimate
  flags   : ring_txn_ids, burst_txn_ids, card_testing_run_ids, conflict_chain_ids (pattern membership: always in),
            strong_profile_id (signature match by device), min_cms / uncertain_cms thresholds (optional)

Rules
  * always includes the flagged transaction
  * fraud     : chain members with cms_p >= 0.30 OR sig_match OR pattern membership
                (all ring-profile txns on the card, all burst members, the card-testing run)
  * uncertain : flagged + chain members with cms_p >= 0.50 (+ pattern members) — the rest of the
                candidate chain is named in evidence, not in affected_txn_ids
  * legitimate: []
  * home-region members are never filtered out (they occur inside out-of-region episodes)
  * ring-profile transactions outside the chain (e.g. one week earlier) are still members: the
    profile is the episode's signature
"""
from __future__ import annotations

from datetime import datetime, timedelta

from engine import config

MIN_CMS = 0.30
UNCERTAIN_CMS = 0.50


def members(chain: list[dict], flagged_id: str, verdict: str, flags: dict) -> list[str]:
    if verdict == "legitimate":
        return []
    min_cms = float(flags.get("min_cms", MIN_CMS))
    unc_cms = float(flags.get("uncertain_cms", UNCERTAIN_CMS))
    pattern_ids: set[str] = set()
    for key in ("ring_txn_ids", "burst_txn_ids", "card_testing_run_ids", "conflict_chain_ids"):
        pattern_ids |= {str(x) for x in flags.get(key, []) or []}
    chosen: dict[str, str] = {}   # id -> ts for ordering
    for m in chain:
        mid = str(m["id"])
        cms = float(m.get("cms_p", -1) or -1)
        if mid == str(flagged_id) or mid in pattern_ids:
            chosen[mid] = m["ts"]
            continue
        if verdict == "uncertain":
            if cms >= unc_cms:
                chosen[mid] = m["ts"]
        else:
            if cms >= min_cms or bool(m.get("sig_match")):
                chosen[mid] = m["ts"]
    # pattern members that are not in the (capped) chain: ring txns on the card, burst members
    extra = flags.get("pattern_member_rows", []) or []          # [{id, ts}]
    for r in extra:
        if str(r["id"]) in pattern_ids and str(r["id"]) not in chosen:
            chosen[str(r["id"])] = r["ts"]
    if str(flagged_id) not in chosen:
        chosen[str(flagged_id)] = flags.get("flagged_ts", "")
    ordered = [i for i, _ in sorted(chosen.items(), key=lambda kv: (kv[1], int(kv[0]) if kv[0].isdigit() else kv[0]))]
    # A fraud episode is a chain of SUSPICIOUS transactions with <= 48 h gaps (closed cases: intra-episode gap
    # p99 = 46 h), so keep only the members connected to the flagged transaction through <= gap_h gaps among
    # the members themselves — a suspicious-looking purchase five days earlier on an active card is not part
    # of this episode unless the chain of members reaches it. Pattern members (ring / burst / R5 run) are exempt.
    gap = timedelta(hours=float(flags.get("gap_h", config.EPISODE_GAP_H)))
    ts = {i: _parse(chosen[i]) for i in ordered}
    keep = {str(flagged_id)} | (pattern_ids & set(ordered))
    idx = ordered.index(str(flagged_id)) if str(flagged_id) in ordered else 0
    for j in range(idx - 1, -1, -1):
        if ts[ordered[j]] is not None and ts[ordered[j + 1]] is not None and (ts[ordered[j + 1]] - ts[ordered[j]]) <= gap:
            keep.add(ordered[j])
        elif ordered[j] not in pattern_ids:
            break
    for j in range(idx + 1, len(ordered)):
        if ts[ordered[j]] is not None and ts[ordered[j - 1]] is not None and (ts[ordered[j]] - ts[ordered[j - 1]]) <= gap:
            keep.add(ordered[j])
        elif ordered[j] not in pattern_ids:
            break
    return [i for i in ordered if i in keep]


def _parse(ts: str):
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def exposure(chain: list[dict], ids: list[str], extra_rows: list[dict] | None = None) -> float:
    amt = {str(m["id"]): abs(float(m["amt"])) for m in chain}
    for r in extra_rows or []:
        amt.setdefault(str(r["id"]), abs(float(r["amt"])))
    return round(sum(amt.get(i, 0.0) for i in ids), 2)
