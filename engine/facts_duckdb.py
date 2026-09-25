"""DuckDB implementation of the installed-query contracts (contracts/interfaces.md §A).

Every public method has the same name, parameters and printed keys as the GSQL query it
mirrors, so this module is (1) the RUN_MODE=mock backend for the agent, (2) the expected-output
oracle for tests/live/test_query_contracts.py, and (3) what engine/run_cases.py uses to build
the Facts dict when no TigerGraph is reachable.

Conventions (identical to the GSQL side — decision D8: the GSQL is the rule):
  * ids are strings, amounts floats, timestamps 'YYYY-MM-DD HH:MM:SS'
  * every read query applies ts <= as_of; the engine (and the agent) call region_history and device_history
    with as_of = flagged_ts - 1 s so "prior" excludes the flagged transaction, every other read uses opened_at
  * region_history: hint home = modal in-person region, known = >= 3 distinct days or >= 5 transactions,
    rare = 1-4, new = 0; prior_n and home_activity_48h count every channel
  * device_history.prior_n counts every transaction on the profile with ts <= as_of (the flagged one included
    when as_of >= its ts)
  * episode_candidates.sig_match = same strong device profile, or same purchaser email + product with the amount
    within 5 % inside 2 h (no same-region clause)
  * ring_profile.wave_cards = the other cards on the profile with a transaction within +-30 days of the case
    card's own ring transactions (the wave cluster), pre_open_cards = those with such a transaction <= as_of,
    n_cards_alltime = every card ever on the profile (D6)
  * card_testing_check.run is a dict (the live GroupByAccum list is unwrapped by mcp/normalize.unwrap_singleton)
  * empty results are [] / {} — never None

Usage:
    fx = DuckFacts()                       # opens ENGINE_FACTS_DB read-only
    facts = fx.collect(ctx)                # the mandatory Facts dict for one CaseContext
    fx.card_window({"id": "C12382-K1"}, "2016-12-02 01:55:28", "2016-12-05 01:55:28", 60)
"""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta

import duckdb
import pandas as pd

from engine import config
from engine.types import CaseContext, Facts

TS_FMT = "%Y-%m-%d %H:%M:%S"
CHAIN_SPAN_DAYS = 7      # chain walk cut-off around the anchor transaction
CHAIN_MAX_ROWS = 200     # rows returned by episode_candidates / considered by card_testing_check


def _ts(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NaT:
        return ""
    if isinstance(v, str):
        return v
    return pd.Timestamp(v).strftime(TS_FMT)


def _py(v):
    """numpy/pandas scalars → JSON-native python."""
    if v is None:
        return None
    if isinstance(v, (pd.Timestamp, datetime)):
        return _ts(v)
    if isinstance(v, (list, tuple)) or (hasattr(v, "tolist") and getattr(v, "ndim", 0) >= 1):
        return [_py(x) for x in (v.tolist() if hasattr(v, "tolist") else v)]
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float) and pd.isna(v):
        return None
    return v


def _rows(df: pd.DataFrame) -> list[dict]:
    out = []
    for rec in df.to_dict("records"):
        out.append({k: _py(v) for k, v in rec.items()})
    return out


def _vid(v) -> str:
    """Accept {"id": ...} (MCP encoding) or a bare id."""
    return v["id"] if isinstance(v, dict) else str(v)


def _dt(s: str) -> datetime:
    return datetime.strptime(s, TS_FMT)


class DuckFacts:
    def __init__(self, db_path: str | None = None):
        self.con = duckdb.connect(str(db_path or config.FACTS_DB), read_only=True)
        self.con.execute("PRAGMA threads=4")

    def q(self, sql: str, *params) -> pd.DataFrame:
        return self.con.execute(sql, list(params)).fetchdf()

    # ------------------------------------------------------------------ helpers
    # ClosedCase.template_id (schema.md; etl/parse_closed_cases.py) derived from the note text so the mock backend
    # prints the same value the graph holds. Additive key on prior_closed_cases / closed_cases (contract deviation, see spec).
    TEMPLATE_SQL = """CASE WHEN outcome = 'cleared' AND analyst_notes LIKE '%confirmed travel to the billing region%' THEN 'cleared_travel'
                           WHEN outcome = 'cleared' AND analyst_notes LIKE '%from a new phone%' THEN 'cleared_new_phone'
                           WHEN outcome = 'cleared' AND analyst_notes LIKE '%Amount unusual%' THEN 'cleared_amount'
                           WHEN analyst_notes LIKE '%behind an anonymous proxy%' THEN 'undoc_ring'
                           WHEN analyst_notes LIKE '%four online purchases within forty minutes%' THEN 'undoc_burst'
                           ELSE 'fraud_reported' END"""

    def txn_row(self, txn_id: str) -> dict:
        df = self.q("SELECT * FROM txc WHERE TransactionID = ?", int(txn_id))
        if df.empty:
            raise KeyError(f"unknown TransactionID {txn_id}")
        r = _rows(df)[0]
        r["ts"] = _ts(r["ts"])
        return r

    def device_row(self, device_id: str) -> dict:
        if not device_id:
            return {}
        df = self.q("SELECT id, is_strong, n_cards_alltime, n_cards_30d, n_proxy, n_fraud_cases, n_txns FROM device_profile WHERE id = ?", device_id)
        if df.empty:
            return {}
        r = _rows(df)[0]
        return {k: r[k] for k in ("id", "is_strong", "n_cards_alltime", "n_cards_30d", "n_proxy", "n_fraud_cases")}

    def _chain(self, card_id: str, anchor_ts: str, as_of: str, gap_h: int) -> pd.DataFrame:
        """The ≤ gap_h-gap chain on the card containing the transaction at anchor_ts (ts <= as_of)."""
        df = self.q("""SELECT TransactionID, id, ts, amt, product_cd, channel, addr1, p_email, r_email, risk_score, device_id,
                              device_new, proxy, cms_p, burst_id, ring_hit
                       FROM txc WHERE card_id = ? AND ts <= ?::TIMESTAMP ORDER BY ts, TransactionID""", card_id, as_of)
        if df.empty:
            return df
        gap = timedelta(hours=gap_h)
        ts = list(df.ts)
        anchor = pd.Timestamp(anchor_ts)
        # index of the anchor (first row with ts == anchor)
        idx = next((i for i, t in enumerate(ts) if t == anchor), None)
        if idx is None:
            idx = max(i for i, t in enumerate(ts) if t <= anchor)
        # Bound the walk: on a card that transacts daily the ≤48 h chain is its whole history, so the
        # chain is cut at ±CHAIN_SPAN_DAYS around the anchor and at CHAIN_MAX_ROWS rows nearest to it.
        # (The GSQL query applies the same two caps; episode membership is decided by engine/episode.py.)
        span = timedelta(days=CHAIN_SPAN_DAYS)
        lo = idx
        while lo > 0 and (ts[lo] - ts[lo - 1]) <= gap and (anchor - ts[lo - 1]) <= span:
            lo -= 1
        hi = idx
        while hi + 1 < len(ts) and (ts[hi + 1] - ts[hi]) <= gap and (ts[hi + 1] - anchor) <= span:
            hi += 1
        if hi - lo + 1 > CHAIN_MAX_ROWS:
            half = CHAIN_MAX_ROWS // 2
            lo, hi = max(lo, idx - half), min(hi, idx + half)
        return df.iloc[lo:hi + 1].reset_index(drop=True)

    # ------------------------------------------------------------------ §A queries
    def case_context(self, t, as_of: str) -> dict:
        txn = self.txn_row(_vid(t))
        keys = ["id", "ts", "amt", "product_cd", "channel", "addr1", "addr2", "p_email", "r_email", "risk_score",
                "has_identity", "device_new", "proxy", "device_type", "device_id", "cms_p", "card_seq", "prior_in_region",
                "prior_on_dev", "prior_pem", "prior_pcd", "prior_med_amt", "prior_max_amt", "ring_hit", "burst_id"]
        card = _rows(self.q("SELECT id, card_type, card_network, modal_region, n_txns, ring_id, customer_id FROM card_feat WHERE id = ?", txn["card_id"]))[0]
        # n_txns time-boxed to ts <= as_of, as case_context.gsql counts it (card_feat.n_txns is whole-history)
        card["n_txns"] = int(self.q("SELECT count(*) n FROM txc WHERE card_id=? AND ts <= ?::TIMESTAMP", txn["card_id"], as_of).n[0])
        cust = _rows(self.q("SELECT id, n_cards FROM customer_feat WHERE id = ?", card["customer_id"]))[0]
        return {"txn": {k: txn[k] for k in keys},
                "card": {k: card[k] for k in ("id", "card_type", "card_network", "modal_region", "n_txns", "ring_id")},
                "customer": cust, "device": self.device_row(txn["device_id"])}

    def card_profile(self, c, as_of: str) -> dict:
        cid = _vid(c)
        card = _rows(self.q("SELECT * FROM card_feat WHERE id = ?", cid))[0]
        # the history fields are recomputed over ts <= as_of, as card_profile.gsql does (card_feat holds whole-history values)
        hist = _rows(self.q("""SELECT count(*) n_txns, min(ts) first_ts, max(ts) last_ts, coalesce(quantile_cont(amt, 0.5), 0) median_amt,
                                 coalesce(quantile_cont(amt, 0.9), 0) p90_amt, coalesce(max(amt), 0) max_amt,
                                 coalesce(max(amt) FILTER (WHERE channel='in_person'), -1) max_in_person_amt,
                                 count(*) FILTER (WHERE channel='online') n_online, count(*) FILTER (WHERE channel='in_person') n_in_person,
                                 count(DISTINCT addr1) FILTER (WHERE addr1<>'') n_regions,
                                 count(DISTINCT device_id) FILTER (WHERE device_id<>'') n_devices_seen
                                 FROM txc WHERE card_id=? AND ts <= ?::TIMESTAMP""", cid, as_of))[0]
        card.update(hist)
        card["first_ts"], card["last_ts"] = _ts(card["first_ts"]), _ts(card["last_ts"])
        last30 = _rows(self.q("""SELECT count(*) n, round(coalesce(sum(amt),0),2) sum_amt, count(*) FILTER (WHERE channel='online') n_online,
                                  count(DISTINCT addr1) FILTER (WHERE addr1<>'') n_regions,
                                  count(DISTINCT device_id) FILTER (WHERE device_id<>'') n_devices, count(DISTINCT product_cd) n_products
                                  FROM txc WHERE card_id=? AND ts > ?::TIMESTAMP - INTERVAL 30 DAY AND ts <= ?::TIMESTAMP""", cid, as_of, as_of))[0]
        regions = _rows(self.q("""SELECT addr1, count(*) n, count(DISTINCT ts::DATE) AS "days", min(ts) first_ts, max(ts) last_ts
                                  FROM txc WHERE card_id=? AND ts <= ?::TIMESTAMP AND addr1<>'' GROUP BY 1 ORDER BY n DESC, first_ts LIMIT 5""", cid, as_of))
        devices = _rows(self.q("""SELECT t.device_id, count(*) n, min(t.ts) first_ts, coalesce(d.is_strong, false) is_strong
                                  FROM txc t LEFT JOIN device_profile d ON d.id=t.device_id
                                  WHERE t.card_id=? AND t.ts <= ?::TIMESTAMP AND t.device_id<>'' GROUP BY 1, 4 ORDER BY n DESC, first_ts LIMIT 5""", cid, as_of))
        emails = _rows(self.q("""SELECT p_email AS "domain", count(*) n FROM txc WHERE card_id=? AND ts <= ?::TIMESTAMP AND p_email<>''
                                 GROUP BY 1 ORDER BY n DESC LIMIT 5""", cid, as_of))
        other = _rows(self.q("SELECT id, card_type, n_txns FROM card_feat WHERE customer_id=? AND id<>? ORDER BY id", card["customer_id"], cid))
        prior = _rows(self.q(f"""SELECT case_id id, opened_at, outcome, pattern, exposure_usd, report_filed, {self.TEMPLATE_SQL} template_id FROM cc
                                WHERE card_id=? AND opened_at <= ?::TIMESTAMP ORDER BY opened_at""", cid, as_of))
        return {"card": card, "last30": last30, "regions": regions, "devices": devices, "emails": emails,
                "other_cards": other, "prior_closed_cases": prior, "prior_agent_cases": []}

    def card_window(self, c, from_ts: str, to_ts: str, max_rows: int = 60) -> dict:
        cid = _vid(c)
        df = self.q("""SELECT * FROM (
                         SELECT id, ts, amt, product_cd, channel, addr1, p_email, r_email, risk_score, cms_p, device_id, device_new, proxy,
                                coalesce(date_diff('second', lag(ts) OVER (PARTITION BY card_id ORDER BY ts, TransactionID), ts), -1) gap_seconds
                         FROM txc WHERE card_id=?) w
                       WHERE ts >= ?::TIMESTAMP AND ts <= ?::TIMESTAMP ORDER BY ts DESC, id DESC LIMIT ?""", cid, from_ts, to_ts, int(max_rows))
        df = df.iloc[::-1].reset_index(drop=True)
        summ = _rows(self.q("""SELECT count(*) n, round(coalesce(sum(amt),0),2) sum_amt, count(*) FILTER (WHERE channel='online') n_online,
                               count(*) FILTER (WHERE channel='in_person') n_in_person FROM txc
                               WHERE card_id=? AND ts >= ?::TIMESTAMP AND ts <= ?::TIMESTAMP""", cid, from_ts, to_ts))[0]
        return {"txns": _rows(df), "summary": summ}

    def region_history(self, c, addr1: str, as_of: str) -> dict:
        cid = _vid(c)
        tot = self.q("SELECT count(*) n FROM txc WHERE card_id=? AND ts <= ?::TIMESTAMP AND addr1<>''", cid, as_of).n[0]
        reg = _rows(self.q("""SELECT count(*) prior_n, count(DISTINCT ts::DATE) prior_days, min(ts) first_ts, max(ts) last_ts
                              FROM txc WHERE card_id=? AND addr1=? AND ts <= ?::TIMESTAMP""", cid, addr1, as_of))[0]
        reg = {"addr1": addr1, "prior_n": int(reg["prior_n"]), "prior_days": int(reg["prior_days"]), "first_ts": _ts(reg["first_ts"]),
               "last_ts": _ts(reg["last_ts"]), "share": round(float(reg["prior_n"]) / float(tot), 4) if tot else 0.0}
        modal = self.q("""SELECT addr1 FROM (SELECT addr1, count(*) n, min(ts) f FROM txc WHERE card_id=? AND channel='in_person' AND addr1<>''
                          AND ts <= ?::TIMESTAMP GROUP BY 1) ORDER BY n DESC, f LIMIT 1""", cid, as_of)
        modal_region = str(modal.addr1[0]) if len(modal) else ""
        n_regions = int(self.q("SELECT count(DISTINCT addr1) n FROM txc WHERE card_id=? AND ts <= ?::TIMESTAMP AND addr1<>''", cid, as_of).n[0])
        # GSQL rule (D8): the last 48 h before as_of, ANY channel with a billing region: n_home in the modal region, n_other elsewhere
        home = _rows(self.q("""SELECT count(*) FILTER (WHERE addr1 = ?) n_home, count(*) FILTER (WHERE addr1 <> ?) n_other
                               FROM txc WHERE card_id=? AND addr1<>''
                               AND ts BETWEEN ?::TIMESTAMP - INTERVAL 48 HOUR AND ?::TIMESTAMP""", modal_region, modal_region, cid, as_of, as_of))[0]
        # GSQL rule (D8): home = modal region; known = >= 3 distinct days OR >= 5 transactions; rare = 1..4; new = never seen
        if addr1 == modal_region and modal_region:
            hint = "home"
        elif reg["prior_days"] >= 3 or reg["prior_n"] >= 5:
            hint = "known"
        elif reg["prior_n"] >= 1:
            hint = "rare"
        else:
            hint = "new"
        return {"region": reg, "modal_region": modal_region, "n_regions": n_regions, "home_activity_48h": home, "hint": hint}

    def device_history(self, c, d, as_of: str) -> dict:
        cid, did = _vid(c), _vid(d)
        r = _rows(self.q("""SELECT count(*) prior_n, min(ts) first_ts, list(DISTINCT device_new) vals FROM txc
                            WHERE card_id=? AND device_id=? AND ts <= ?::TIMESTAMP""", cid, did, as_of))[0]
        return {"prior_n": r["prior_n"], "first_ts": _ts(r["first_ts"]), "device_new_values": sorted(v for v in (r["vals"] or []) if v is not None)}   # sorted: list(DISTINCT) order is not stable

    def device_neighbors(self, d, from_ts: str, to_ts: str, max_cards: int = 60) -> dict:
        did = _vid(d)
        dev = self.device_row(did)
        if not dev:
            return {"device": {}, "cards": [], "closed_cases": [], "agent_cases": []}
        if not dev["is_strong"]:
            return {"device": dev, "cards": [], "closed_cases": [], "agent_cases": []}
        cards = _rows(self.q(f"""SELECT card_id, any_value(customer_id) customer_id, count(*) n_txns, round(sum(amt),2) sum_amt,
                                 count(*) FILTER (WHERE proxy = '{config.ANON_PROXY}') n_proxy, count(*) FILTER (WHERE device_new='New') n_new,
                                 min(ts) first_ts, max(ts) last_ts
                                 FROM txc WHERE device_id=? AND ts >= ?::TIMESTAMP AND ts <= ?::TIMESTAMP
                                 GROUP BY 1 ORDER BY first_ts LIMIT ?""", did, from_ts, to_ts, int(max_cards)))
        closed = _rows(self.q("""SELECT DISTINCT c.case_id id, c.card_id, c.pattern, c.outcome, c.report_filed
                                 FROM txc t JOIN cc_txn x USING(TransactionID) JOIN cc c USING(case_id)
                                 WHERE t.device_id=? ORDER BY c.case_id""", did))
        return {"device": dev, "cards": cards, "closed_cases": closed, "agent_cases": []}

    def email_neighbors(self, e, c, from_ts: str, to_ts: str) -> dict:
        dom, cid = _vid(e), _vid(c)
        cards = _rows(self.q("""SELECT card_id, count(*) n_txns FROM txc WHERE r_email=? AND card_id<>? AND ts >= ?::TIMESTAMP AND ts <= ?::TIMESTAMP
                                GROUP BY 1 ORDER BY n_txns DESC LIMIT 60""", dom, cid, from_ts, to_ts))
        closed = _rows(self.q("""SELECT DISTINCT c.case_id id, c.card_id, c.pattern FROM txc t JOIN cc_txn x USING(TransactionID) JOIN cc c USING(case_id)
                                 WHERE t.r_email=? AND t.card_id<>? AND t.ts >= ?::TIMESTAMP AND t.ts <= ?::TIMESTAMP ORDER BY 1""", dom, cid, from_ts, to_ts))
        return {"cards": cards, "closed_cases": closed}

    def shared_origin_scan(self, c, as_of: str, days: int = 30) -> dict:
        cid = _vid(c)
        devices = _rows(self.q("""SELECT t.device_id, coalesce(d.is_strong,false) is_strong, coalesce(d.n_cards_30d,0) n_cards_30d,
                                  coalesce(d.n_fraud_cases,0) n_fraud_cases, count(*) n_txns_on_card
                                  FROM txc t LEFT JOIN device_profile d ON d.id=t.device_id
                                  WHERE t.card_id=? AND t.channel='online' AND t.device_id<>'' AND t.ts > ?::TIMESTAMP - INTERVAL (?) DAY AND t.ts <= ?::TIMESTAMP
                                  GROUP BY 1,2,3,4 ORDER BY n_txns_on_card DESC""", cid, as_of, int(days), as_of))
        emails = _rows(self.q("""WITH mine AS (SELECT DISTINCT r_email FROM txc WHERE card_id=? AND r_email<>'' AND ts > ?::TIMESTAMP - INTERVAL (?) DAY AND ts <= ?::TIMESTAMP)
                                 SELECT m.r_email AS "domain",
                                   (SELECT count(DISTINCT o.card_id) FROM txc o WHERE o.r_email=m.r_email AND o.card_id<>? AND o.ts > ?::TIMESTAMP - INTERVAL 30 DAY AND o.ts <= ?::TIMESTAMP) n_cards_30d,
                                   (SELECT count(DISTINCT c.case_id) FROM txc o JOIN cc_txn x USING(TransactionID) JOIN cc c USING(case_id)
                                     WHERE o.r_email=m.r_email AND o.card_id<>? AND c.outcome='confirmed_fraud' AND o.ts > ?::TIMESTAMP - INTERVAL 30 DAY AND o.ts <= ?::TIMESTAMP) n_fraud_cases
                                 FROM mine m""", cid, as_of, int(days), as_of, cid, as_of, as_of, cid, as_of, as_of))
        rc = self.q("SELECT region_cluster_30d FROM card_feat WHERE id=?", cid).region_cluster_30d[0]
        return {"devices": devices, "recipient_emails": emails, "region_cluster_30d": rc}

    def card_testing_check(self, c, as_of: str, small: float = config.CARD_TESTING_SMALL, window_min: int = config.CARD_TESTING_WINDOW_MIN,
                           min_n: int = config.CARD_TESTING_MIN_N, big: float = config.CARD_TESTING_BIG,
                           lookahead_h: int = config.CARD_TESTING_LOOKAHEAD_H) -> dict:
        cid = _vid(c)
        s = self.q("""SELECT id, ts, amt FROM txc WHERE card_id=? AND channel='online' AND amt < ? AND ts <= ?::TIMESTAMP ORDER BY ts, TransactionID""", cid, float(small), as_of)
        best: list[int] = []
        ts = list(s.ts)
        for i in range(len(ts)):
            j = i
            while j + 1 < len(ts) and (ts[j + 1] - ts[i]) <= timedelta(minutes=window_min):
                j += 1
            if j - i + 1 >= min_n and j - i + 1 > len(best):
                best = list(range(i, j + 1))
        run: dict = {}
        larger: dict = {}
        cleared = False
        if best:
            run = {"ids": [str(s.id[i]) for i in best], "amts": [float(s.amt[i]) for i in best],
                   "start_ts": _ts(ts[best[0]]), "end_ts": _ts(ts[best[-1]])}
            after = self.q("""SELECT id, amt, ts FROM txc WHERE card_id=? AND channel='online' AND amt >= ? AND ts > ?::TIMESTAMP
                              AND ts <= least(?::TIMESTAMP + INTERVAL (?) HOUR, ?::TIMESTAMP) ORDER BY ts, TransactionID""",
                           cid, float(small), run["end_ts"], run["end_ts"], int(lookahead_h), as_of)
            if len(after):
                larger = {"id": str(after.id[0]), "amt": float(after.amt[0]), "ts": _ts(after.ts[0])}
                cleared = bool((after.amt > big).any())
        # the ≤48 h chain containing the latest transaction ≤ as_of (== the flagged transaction's chain)
        last = self.q("SELECT ts FROM txc WHERE card_id=? AND ts <= ?::TIMESTAMP ORDER BY ts DESC LIMIT 1", cid, as_of)
        chain = self._chain(cid, _ts(last.ts[0]), as_of, config.EPISODE_GAP_H) if len(last) else pd.DataFrame()
        chain_info = {"n_members": int(len(chain)), "n_small": int(((chain.channel == "online") & (chain.amt < small)).sum()) if len(chain) else 0}
        return {"run": run, "larger_purchase": larger, "cleared_over_big": cleared, "chain": chain_info}

    def under_threshold_burst(self, c, as_of: str) -> dict:
        cid = _vid(c)
        df = self.q("""SELECT burst_id, id, amt, ts, device_id, p_email, addr1 FROM txc WHERE card_id=? AND burst_id<>'' AND ts <= ?::TIMESTAMP ORDER BY ts""", cid, as_of)
        bursts = []
        for bid, g in df.groupby("burst_id", sort=False):
            bursts.append({"burst_id": bid, "ids": [str(x) for x in g.id], "amts": [float(x) for x in g.amt],
                           "start_ts": _ts(g.ts.min()), "end_ts": _ts(g.ts.max()),
                           "device_ids": sorted({x for x in g.device_id if x}), "emails": sorted({x for x in g.p_email if x}),
                           "regions": sorted({x for x in g.addr1 if x})})
        la = self.q("SELECT burst_lookalike_ids FROM card_feat WHERE id=?", cid).burst_lookalike_ids[0]
        return {"bursts": bursts, "lookalike_cards": json.loads(la) if la else []}

    def recurring_charge_check(self, t, tol: float = 0.01, as_of: str = "") -> dict:
        txn = self.txn_row(_vid(t))
        place_col = "addr1" if txn["channel"] == "in_person" else "p_email"
        df = self.q(f"""SELECT {place_col} place, ts FROM txc WHERE card_id=? AND product_cd=? AND abs(amt - ?) <= ? * ? AND ts < ?::TIMESTAMP
                        AND ts <= ?::TIMESTAMP AND TransactionID <> ? ORDER BY ts""",
                    txn["card_id"], txn["product_cd"], float(txn["amt"]), float(tol), float(txn["amt"]), txn["ts"], as_of or txn["ts"], int(txn["id"]))
        groups = []
        for place, g in df.groupby("place", sort=False):
            gaps = [(b - a).total_seconds() / 86400.0 for a, b in zip(g.ts[:-1], g.ts[1:])]
            med = statistics.median(gaps) if gaps else -1.0
            cv = (statistics.pstdev(gaps) / statistics.mean(gaps)) if len(gaps) >= 2 and statistics.mean(gaps) > 0 else -1.0
            groups.append({"place": place, "n": int(len(g)), "median_gap_days": round(med, 3), "gap_cv": round(cv, 3),
                           "first_ts": _ts(g.ts.min()), "last_ts": _ts(g.ts.max())})
        groups.sort(key=lambda x: -x["n"])
        return {"groups": groups, "total_n": int(len(df))}

    def episode_candidates(self, t, as_of: str, gap_h: int = config.EPISODE_GAP_H) -> dict:
        txn = self.txn_row(_vid(t))
        chain = self._chain(txn["card_id"], txn["ts"], as_of, gap_h)
        dev = self.device_row(txn["device_id"])
        strong = bool(dev.get("is_strong")) if dev else False
        t_ts = pd.Timestamp(txn["ts"])
        rows = []
        for r in chain.itertuples(index=False):
            # signature match (= episode_candidates.gsql, D8): same strong device profile, or same purchaser email + product
            # with the amount within 5 % inside 2 h. No same-region clause.
            same_dev = strong and bool(r.device_id) and r.device_id == txn["device_id"]
            sig = same_dev or (
                r.p_email and r.p_email == txn["p_email"] and r.product_cd == txn["product_cd"]
                and abs(float(r.amt) - float(txn["amt"])) <= 0.05 * float(txn["amt"])
                and abs((pd.Timestamp(r.ts) - t_ts).total_seconds()) <= 2 * 3600)
            rows.append({"id": str(r.id), "ts": _ts(r.ts), "amt": float(r.amt), "product_cd": r.product_cd, "channel": r.channel,
                         "addr1": r.addr1, "p_email": r.p_email, "device_id": r.device_id, "device_new": r.device_new,
                         "cms_p": float(r.cms_p), "sig_match": bool(sig)})
        return {"chain": rows}

    def prior_cases_for_customer(self, cu, as_of: str) -> dict:
        cid = _vid(cu)
        closed = _rows(self.q(f"""SELECT case_id id, card_id, opened_at, outcome, pattern, exposure_usd, report_filed, n_txns, {self.TEMPLATE_SQL} template_id FROM cc
                                 WHERE customer_id=? AND opened_at <= ?::TIMESTAMP ORDER BY opened_at""", cid, as_of))
        return {"closed_cases": closed, "agent_cases": []}

    def ring_profile(self, c, as_of: str) -> dict:
        cid = _vid(c)
        ring_id = self.q("SELECT ring_id FROM card_feat WHERE id=?", cid).ring_id[0] or ""
        if not ring_id:
            return {"ring_id": "", "device": {}, "wave_cards": [], "pre_open_cards": [], "n_cards_alltime": 0, "card_txns_on_ring": [], "closed_cases": []}
        dev = self.device_row(ring_id)
        mine = self.q("SELECT id, ts, amt FROM txc WHERE card_id=? AND device_id=? AND ts <= ?::TIMESTAMP ORDER BY ts", cid, ring_id, as_of)
        # D6: wave_cards = the OTHER cards on the profile with any transaction within +-30 days of this card's own ring
        # transactions (the wave cluster: 27 for HHG-014); pre_open_cards = those whose such transaction is <= as_of (19);
        # n_cards_alltime = every card ever on the profile (52). The GSQL prints the same three.
        acts = self.q("SELECT card_id, ts FROM txc WHERE device_id=? AND card_id<>? ORDER BY ts", ring_id, cid)
        n_alltime = int(self.q("SELECT count(DISTINCT card_id) n FROM txc WHERE device_id=?", ring_id).n[0])
        wave_cards: set[str] = set()
        pre_open: set[str] = set()
        anchors = mine if len(mine) else self.q("SELECT id, ts, amt FROM txc WHERE card_id=? AND device_id=? ORDER BY ts", cid, ring_id)   # contract: all ring txns when none precede as_of
        if len(anchors) and len(acts):
            near = pd.Series(False, index=acts.index)
            for m in anchors.ts:
                near |= (acts.ts - pd.Timestamp(m)).abs() <= timedelta(days=30)
            in_wave = acts[near]
            wave_cards = set(in_wave.card_id)
            pre_open = set(in_wave[in_wave.ts <= pd.Timestamp(as_of)].card_id)
        closed = _rows(self.q("""SELECT DISTINCT c.case_id id, c.card_id, c.pattern, c.outcome, c.report_filed
                                 FROM txc t JOIN cc_txn x USING(TransactionID) JOIN cc c USING(case_id) WHERE t.device_id=? ORDER BY 1""", ring_id))
        return {"ring_id": ring_id, "device": dev, "wave_cards": sorted(wave_cards), "pre_open_cards": sorted(pre_open), "n_cards_alltime": n_alltime,
                "card_txns_on_ring": [{"id": str(r.id), "ts": _ts(r.ts), "amt": float(r.amt)} for r in mine.itertuples()],
                "closed_cases": closed}

    def case_subgraph(self, c, as_of: str, hours: int = 72) -> dict:
        cid = _vid(c)
        tx = self.q("""SELECT id, device_id, addr1, p_email FROM txc WHERE card_id=? AND ts > ?::TIMESTAMP - INTERVAL (?) HOUR AND ts <= ?::TIMESTAMP
                       ORDER BY ts DESC LIMIT 100""", cid, as_of, int(hours), as_of)
        nodes = [{"id": cid, "type": "Card", "label": cid}]
        edges = []
        seen = {cid}
        for r in tx.itertuples():
            nodes.append({"id": r.id, "type": "Transaction", "label": r.id})
            edges.append({"src": cid, "dst": r.id, "type": "MADE"})
            for val, typ, et in ((r.device_id, "DeviceProfile", "FROM_DEVICE"), (r.addr1, "BillingRegion", "BILLED_IN"), (r.p_email, "EmailDomain", "PURCHASER_EMAIL")):
                if val:
                    if val not in seen:
                        nodes.append({"id": val, "type": typ, "label": val})
                        seen.add(val)
                    edges.append({"src": r.id, "dst": val, "type": et})
            if len(nodes) + len(edges) > 300:
                break
        return {"nodes": nodes[:300], "edges": edges[:300]}

    def post_open_activity(self, c, opened_at: str, days: int = 7) -> dict:
        cid = _vid(c)
        txns = _rows(self.q("""SELECT id, ts, amt, channel, addr1, device_new, cms_p FROM txc WHERE card_id=? AND ts > ?::TIMESTAMP
                               AND ts <= ?::TIMESTAMP + INTERVAL (?) DAY ORDER BY ts""", cid, opened_at, opened_at, int(days)))
        return {"txns": txns, "summary": {"n": len(txns), "sum_amt": round(sum(t["amt"] for t in txns), 2)}}

    def similar_prior_cases(self, q: list[float] | None, k: int, card_id: str, customer_id: str, device_id: str, addr1: str,
                            pattern_sig: str, as_of: str) -> dict:
        """Structural half of the retriever (the vector rerank needs TigerVector; in mock the structural
        score orders the candidates: same card > same customer > same strong device > same region+pattern)."""
        df = self.q("""
            WITH cand AS (
              SELECT c.case_id, c.outcome, c.pattern, c.exposure_usd, c.opened_at,
                     (c.card_id = ?) same_card, (c.customer_id = ?) same_customer,
                     EXISTS (SELECT 1 FROM cc_txn x JOIN txc t USING(TransactionID) JOIN device_profile d ON d.id = t.device_id
                             WHERE x.case_id=c.case_id AND t.device_id = ? AND ? <> '' AND d.n_cards_alltime <= 150) same_device,
                     EXISTS (SELECT 1 FROM cc_txn x JOIN txc t USING(TransactionID) WHERE x.case_id=c.case_id AND t.addr1 = ? AND ? <> '') same_region,
                     (c.pattern = ?) same_pattern
              FROM cc c WHERE c.opened_at <= ?::TIMESTAMP)
            SELECT *, (same_card::INT*8 + same_customer::INT*4 + same_device::INT*4 + same_region::INT*1 + same_pattern::INT*2) score
            FROM cand WHERE same_card OR same_customer OR same_device OR (same_region AND same_pattern)
            ORDER BY score DESC, opened_at DESC LIMIT ?""", card_id, customer_id, device_id, device_id, addr1, addr1, pattern_sig, as_of, int(k) * 3)
        rows = []
        for r in df.itertuples():
            reasons = [n for n, v in (("same_card", r.same_card), ("same_customer", r.same_customer), ("same_device", r.same_device),
                                      ("same_region", r.same_region), ("same_pattern", r.same_pattern)) if v]
            rows.append({"id": r.case_id, "kind": "closed", "outcome_or_verdict": r.outcome, "pattern": r.pattern,
                         "exposure_usd": float(r.exposure_usd), "opened_at": _ts(r.opened_at),
                         "distance": round(1.0 - float(r.score) / 19.0, 4), "overlap_reasons": reasons})
        # diversify: keep at least one cleared case when available
        top = rows[:k]
        if top and not any(r["outcome_or_verdict"] == "cleared" for r in top):
            cl = next((r for r in rows[k:] if r["outcome_or_verdict"] == "cleared"), None)
            if cl:
                top = top[:-1] + [cl]
        return {"cases": top}

    def grounding_chunks(self, q, k: int = 5, doc_filter: str = "", kind_filter: str = "") -> dict:
        return {"chunks": []}   # PolicyChunk retrieval needs TigerVector; the engine never consumes it

    # ------------------------------------------------------------------ the mandatory set
    def collect(self, ctx: CaseContext) -> Facts:
        """Run the mandatory query set for one case and return the Facts dict keyed by query name."""
        t = {"id": ctx.flagged_txn_id}
        c = {"id": ctx.card_id}
        cu = {"id": ctx.customer_id}
        as_of = ctx.opened_at
        cc_ = self.case_context(t, as_of)
        txn = cc_["txn"]
        flagged_ts = _dt(txn["ts"])
        before = (flagged_ts - timedelta(seconds=1)).strftime(TS_FMT)      # D8: region_history / device_history exclude the flagged txn
        facts: Facts = {"case_context": cc_}
        facts["card_profile"] = self.card_profile(c, as_of)
        facts["card_window"] = self.card_window(c, (flagged_ts - timedelta(hours=72)).strftime(TS_FMT), as_of, 60)
        facts["region_history"] = self.region_history(c, txn["addr1"], before) if txn["addr1"] else \
            {"region": {"addr1": "", "prior_n": 0, "prior_days": 0, "first_ts": "", "last_ts": "", "share": 0.0},
             "modal_region": self.region_history(c, "", before)["modal_region"], "n_regions": 0,
             "home_activity_48h": {"n_home": 0, "n_other": 0}, "hint": "new"}
        facts["device_history"] = self.device_history(c, {"id": txn["device_id"]}, before) if txn["device_id"] else \
            {"prior_n": 0, "first_ts": "", "device_new_values": []}
        facts["device_neighbors"] = self.device_neighbors({"id": txn["device_id"]}, (flagged_ts - timedelta(days=30)).strftime(TS_FMT), as_of, 60) \
            if txn["device_id"] else {"device": {}, "cards": [], "closed_cases": [], "agent_cases": []}
        facts["shared_origin_scan"] = self.shared_origin_scan(c, as_of, 30)
        facts["card_testing_check"] = self.card_testing_check(c, as_of)
        facts["under_threshold_burst"] = self.under_threshold_burst(c, as_of)
        facts["recurring_charge_check"] = self.recurring_charge_check(t, 0.01, as_of)
        facts["episode_candidates"] = self.episode_candidates(t, as_of, config.EPISODE_GAP_H)
        facts["prior_cases_for_customer"] = self.prior_cases_for_customer(cu, as_of)
        facts["ring_profile"] = self.ring_profile(c, as_of)
        facts["post_open_activity"] = self.post_open_activity(c, as_of, 7)
        facts["similar_prior_cases"] = self.similar_prior_cases(None, 8, ctx.card_id, ctx.customer_id, txn["device_id"], txn["addr1"], "", as_of)
        return facts
