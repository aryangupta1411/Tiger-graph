"""etl/features.py — every Card / Transaction / DeviceProfile / Customer-side attribute in
contracts/schema.md, computed in DuckDB with ts-ordered, leakage-free prior_* aggregates.

Produces (in the DuckDB given on the command line, tables per contracts §D):
    txc            one row per transaction: slim Transaction attributes + card_id + JSON blobs
    txn_feat       card_seq, prior_*, gap_seconds/prev_txn_id (NEXT edge), ring_hit, burst_id, cms_p (NULL until cms_train)
    burst_member   the ≥4 × $450–499.99-in-40-min runs (all months)
    device_profile DeviceProfile attributes incl. n_cards_30d and is_strong
    card_feat      every Card attribute (JSON columns as text)
Run:
    python -m etl.features data/hhgoa.duckdb            # builds all tables, prints the report
    python -m etl.features data/hhgoa.duckdb --report   # report only
Requires tables tx, idn, cc, cp, pairs (etl/build_db.py) and cardmap (etl/card_rule.py).
"""
from __future__ import annotations

import json
import sys
import time

import duckdb

from etl.card_rule import build_cardmap
from ops.console import Col, Table, header, money, ok, rule, step, summary, warn

RING_PROFILE = "SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080"

# Row counts the contract fixes (contracts/schema.md, Makefile `features`); shown as expected-vs-actual.
EXPECTED_ROWS = {"txc": 590_742, "txn_feat": 590_742, "device_profile": 9_705, "card_feat": 14_317}

# ----------------------------------------------------------------------------------------
# 1. txc — slim transaction rows (schema.md Transaction attributes that come straight from the files)
# ----------------------------------------------------------------------------------------
C_COLS = [f"C{i}" for i in range(1, 15)]
D_COLS = [f"D{i}" for i in range(1, 16)]
M_COLS = [f"M{i}" for i in range(1, 10)]


def _json_struct(cols: list[str], cast: str | None) -> str:
    inner = ", ".join(
        f"{c} := {'TRY_CAST(t.' + c + ' AS DOUBLE)' if cast else 't.' + c}" for c in cols
    )
    return f"to_json(struct_pack({inner}))::VARCHAR"


TXC_SQL = f"""
CREATE OR REPLACE TABLE txc AS
SELECT t.TransactionID,
       m.card_id,
       t.customer_id,
       t.ts,
       t.ts_str,
       t.TransactionAmt                                   AS amt,
       t.ProductCD                                        AS product_cd,
       t.channel,
       coalesce(t.addr1, '')                              AS addr1,
       coalesce(t.addr2, '')                              AS addr2,
       coalesce(TRY_CAST(t.dist1 AS DOUBLE), -1)          AS dist1,
       coalesce(TRY_CAST(t.dist2 AS DOUBLE), -1)          AS dist2,
       coalesce(t.P_emaildomain, '')                      AS p_email,
       coalesce(t.R_emaildomain, '')                      AS r_email,
       t.risk_score,
       (i.TransactionID IS NOT NULL)                      AS has_identity,
       coalesce(i.id_15, '')                              AS device_new,
       coalesce(i.id_23, '')                              AS proxy,
       coalesce(i.DeviceType, '')                         AS device_type,
       CASE WHEN i.TransactionID IS NULL
              OR (i.DeviceInfo IS NULL AND i.id_30 IS NULL AND i.id_31 IS NULL AND i.id_33 IS NULL)
            THEN ''
            ELSE coalesce(i.DeviceInfo, 'NULL') || ' | ' || coalesce(i.id_30, 'NULL') || ' | '
                 || coalesce(i.id_31, 'NULL') || ' | ' || coalesce(i.id_33, 'NULL')
       END                                                AS device_id,
       {_json_struct(C_COLS, 'double')}                   AS c_counts,
       {_json_struct(D_COLS, 'double')}                   AS d_deltas,
       {_json_struct(M_COLS, None)}                       AS m_flags,
       t.card4, t.card6
FROM tx t
JOIN cardmap m ON m.customer_id = t.customer_id AND m.k6 = coalesce(t.card6, '')
LEFT JOIN idn i USING (TransactionID)
"""

# ----------------------------------------------------------------------------------------
# 2. txn_base — ts-ordered prior_* aggregates (strictly earlier rows on the same card: no leakage)
# ----------------------------------------------------------------------------------------
TXN_BASE_SQL = """
CREATE OR REPLACE TABLE txn_base AS
SELECT TransactionID, card_id, ts, channel, device_id, product_cd,
       row_number() OVER w                                                   AS card_seq,
       CASE WHEN addr1 = '' THEN 0 ELSE
         count(*) OVER (PARTITION BY card_id, addr1 ORDER BY ts, TransactionID
                        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) END AS prior_in_region,
       CASE WHEN device_id = '' THEN 0 ELSE
         count(*) OVER (PARTITION BY card_id, device_id ORDER BY ts, TransactionID
                        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) END AS prior_on_dev,
       CASE WHEN p_email = '' THEN 0 ELSE
         count(*) OVER (PARTITION BY card_id, p_email ORDER BY ts, TransactionID
                        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) END AS prior_pem,
       count(*) OVER (PARTITION BY card_id, product_cd ORDER BY ts, TransactionID
                      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)       AS prior_pcd,
       count(*) OVER (PARTITION BY card_id, channel ORDER BY ts, TransactionID
                      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)       AS prior_chan,
       coalesce(quantile_cont(amt, 0.5) OVER (PARTITION BY card_id ORDER BY ts, TransactionID
                      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), -1)  AS prior_med_amt,
       coalesce(max(amt) OVER (PARTITION BY card_id ORDER BY ts, TransactionID
                      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), -1)  AS prior_max_amt,
       lag(TransactionID) OVER w                                              AS prev_txn_id,
       coalesce(date_diff('second', lag(ts) OVER w, ts), -1)                  AS gap_seconds
FROM txc
WINDOW w AS (PARTITION BY card_id ORDER BY ts, TransactionID)
"""

# ----------------------------------------------------------------------------------------
# 3. burst_member — runs of ≥ 4 online txns with 450 ≤ amt ≤ 499.99 inside a 40-minute window
#    burst_id = card_id || '#' || first TransactionID of the run (schema.md)
# ----------------------------------------------------------------------------------------
BURST_SQL = """
CREATE OR REPLACE TABLE burst_member AS
WITH o AS (
  SELECT card_id, TransactionID, ts, amt FROM txc
  WHERE channel = 'online' AND amt BETWEEN 450 AND 499.99
), anchors AS (                       -- windows [a.ts, a.ts + 40 min] holding ≥ 4 qualifying txns
  SELECT a.card_id, a.TransactionID, a.ts
  FROM o a JOIN o b ON b.card_id = a.card_id AND b.ts BETWEEN a.ts AND a.ts + INTERVAL 40 MINUTE
  GROUP BY 1, 2, 3 HAVING count(*) >= 4
), members AS (                        -- every qualifying txn covered by some anchor window
  SELECT DISTINCT b.card_id, b.TransactionID, b.ts, b.amt
  FROM anchors a JOIN o b ON b.card_id = a.card_id AND b.ts BETWEEN a.ts AND a.ts + INTERVAL 40 MINUTE
), grouped AS (                        -- chain members ≤ 40 min apart into one burst
  SELECT *, sum(new_run) OVER (PARTITION BY card_id ORDER BY ts, TransactionID) AS run_no
  FROM (SELECT *, CASE WHEN lag(ts) OVER (PARTITION BY card_id ORDER BY ts, TransactionID) IS NULL
                         OR date_diff('minute', lag(ts) OVER (PARTITION BY card_id ORDER BY ts, TransactionID), ts) > 40
                       THEN 1 ELSE 0 END AS new_run FROM members)
)
SELECT card_id, TransactionID, ts, amt,
       card_id || '#' || min(TransactionID) OVER (PARTITION BY card_id, run_no)::VARCHAR AS burst_id,
       min(ts) OVER (PARTITION BY card_id, run_no) AS burst_start,
       max(ts) OVER (PARTITION BY card_id, run_no) AS burst_end
FROM grouped
"""

# ----------------------------------------------------------------------------------------
# 4. device_profile — n_cards_30d is the max over 30-day windows (day granularity) of distinct cards
# ----------------------------------------------------------------------------------------
DEVICE_SQL = """
CREATE OR REPLACE TABLE device_profile AS
WITH d AS (
  SELECT device_id, card_id, TransactionID, ts, ts::DATE AS d_day, proxy FROM txc WHERE device_id <> ''
), base AS (
  SELECT device_id, count(*) n_txns, count(DISTINCT card_id) n_cards_alltime,
         count(*) FILTER (WHERE proxy <> '') n_proxy, min(ts) first_seen, max(ts) last_seen
  FROM d GROUP BY 1
), dd AS (SELECT DISTINCT device_id, card_id, d_day FROM d),
w30 AS (
  SELECT a.device_id, a.d_day, count(DISTINCT b.card_id) n
  FROM (SELECT DISTINCT device_id, d_day FROM dd) a
  JOIN dd b ON b.device_id = a.device_id AND b.d_day BETWEEN a.d_day AND a.d_day + INTERVAL 29 DAY
  GROUP BY 1, 2
), m30 AS (SELECT device_id, max(n) n_cards_30d FROM w30 GROUP BY 1),
fr AS (
  SELECT d.device_id, count(DISTINCT p.case_id) n_fraud_cases
  FROM d JOIN pairs p USING (TransactionID) JOIN cc ON cc.case_id = p.case_id
  WHERE p.src = 'closed' AND cc.outcome = 'confirmed_fraud' GROUP BY 1
), parts AS (
  SELECT device_id, string_split(device_id, ' | ') AS p FROM base
)
SELECT b.device_id AS id,
       p[1] AS device_info, p[2] AS os, p[3] AS browser, p[4] AS screen,
       b.n_txns, b.n_cards_alltime, m.n_cards_30d, b.n_proxy, b.first_seen, b.last_seen,
       coalesce(f.n_fraud_cases, 0) AS n_fraud_cases,
       (p[1] <> 'NULL'
        AND ((p[1] <> 'NULL')::INT + (p[2] <> 'NULL')::INT + (p[3] <> 'NULL')::INT + (p[4] <> 'NULL')::INT) >= 3
        AND b.n_cards_alltime <= 60
        AND m.n_cards_30d BETWEEN 2 AND 40) AS is_strong
FROM base b JOIN m30 m USING (device_id) JOIN parts USING (device_id) LEFT JOIN fr f USING (device_id)
"""

# ----------------------------------------------------------------------------------------
# 5. card_feat — every Card attribute
# ----------------------------------------------------------------------------------------
CARD_SQL = """
CREATE OR REPLACE TABLE card_feat AS
WITH base AS (
  SELECT card_id, customer_id, count(*) n_txns, min(ts) first_ts, max(ts) last_ts,
         quantile_cont(amt, 0.5) median_amt, quantile_cont(amt, 0.9) p90_amt, max(amt) max_amt,
         coalesce(max(amt) FILTER (WHERE channel = 'in_person'), -1) max_in_person_amt,
         count(*) FILTER (WHERE channel = 'online') n_online,
         count(*) FILTER (WHERE channel = 'in_person') n_in_person,
         count(DISTINCT addr1) FILTER (WHERE addr1 <> '') n_regions,
         count(DISTINCT device_id) FILTER (WHERE device_id <> '') n_devices_seen
  FROM txc GROUP BY 1, 2
), net AS (                                   -- most frequent card4 on the card
  SELECT card_id, coalesce(card4, 'unknown') card_network FROM (
    SELECT card_id, card4, row_number() OVER (PARTITION BY card_id ORDER BY count(*) DESC, coalesce(card4,'')) rk
    FROM txc GROUP BY 1, 2) WHERE rk = 1
), modal AS (                                 -- modal in-person region, ties -> earliest seen
  SELECT card_id, addr1 modal_region FROM (
    SELECT card_id, addr1, row_number() OVER (PARTITION BY card_id ORDER BY count(*) DESC, min(ts)) rk
    FROM txc WHERE channel = 'in_person' AND addr1 <> '' GROUP BY 1, 2) WHERE rk = 1
), reg AS (
  SELECT card_id, addr1, count(*) n, count(DISTINCT ts::DATE) n_days FROM txc WHERE addr1 <> '' GROUP BY 1, 2
), home AS (
  SELECT card_id, to_json(list_slice(list(struct_pack(addr1 := addr1, n := n, days := n_days) ORDER BY n DESC, addr1), 1, 5))::VARCHAR home_regions
  FROM reg GROUP BY 1
), prod AS (
  SELECT card_id, to_json(list(struct_pack(product_cd := product_cd, n := n) ORDER BY n DESC, product_cd))::VARCHAR usual_products
  FROM (SELECT card_id, product_cd, count(*) n FROM txc GROUP BY 1, 2) GROUP BY 1
), pem AS (
  SELECT card_id, p_email usual_p_email FROM (
    SELECT card_id, p_email, row_number() OVER (PARTITION BY card_id ORDER BY count(*) DESC, p_email) rk
    FROM txc WHERE p_email <> '' GROUP BY 1, 2) WHERE rk = 1
), dev AS (                                   -- known devices: most used first, capped at 200 entries
  SELECT card_id, to_json(list_slice(list(struct_pack(device_id := device_id, first_seen := strftime(first_seen, '%Y-%m-%d %H:%M:%S'), n := n)
                                          ORDER BY n DESC, first_seen, device_id), 1, 200))::VARCHAR known_device_ids
  FROM (SELECT card_id, device_id, min(ts) first_seen, count(*) n FROM txc WHERE device_id <> '' GROUP BY 1, 2) GROUP BY 1
), cases AS (
  SELECT card_id, count(*) FILTER (WHERE outcome = 'confirmed_fraud') n_prior_fraud_cases,
                  count(*) FILTER (WHERE outcome = 'cleared') n_prior_cleared_cases
  FROM cc GROUP BY 1
), ring AS (                                  -- profile-centric ring id (schema.md); anonymous-proxy profiles win ties
  SELECT card_id, device_id ring_id FROM (
    SELECT cd.card_id, cd.device_id,
           row_number() OVER (PARTITION BY cd.card_id ORDER BY (dp.n_proxy * 1.0 / dp.n_txns >= 0.9) DESC, dp.n_fraud_cases DESC, cd.n DESC, cd.device_id) rk
    FROM (SELECT card_id, device_id, count(*) n FROM txc WHERE device_id <> '' AND channel = 'online' GROUP BY 1, 2) cd
    JOIN device_profile dp ON dp.id = cd.device_id
    WHERE dp.is_strong AND (dp.n_fraud_cases >= 2 OR dp.n_proxy * 1.0 / dp.n_txns >= 0.9)) WHERE rk = 1
), cb AS (                                    -- burst look-alikes: other cards with a burst within ±30 d
  SELECT DISTINCT card_id, burst_id, burst_start FROM burst_member
), look AS (
  SELECT a.card_id, to_json(list(DISTINCT b.card_id ORDER BY b.card_id))::VARCHAR burst_lookalike_ids
  FROM cb a JOIN cb b ON b.card_id <> a.card_id AND b.burst_start BETWEEN a.burst_start - INTERVAL 30 DAY AND a.burst_start + INTERVAL 30 DAY
  GROUP BY 1
)
SELECT b.card_id AS id, b.customer_id, m.card_type, n.card_network,
       b.n_txns, b.first_ts, b.last_ts, b.median_amt, b.p90_amt, b.max_amt, b.max_in_person_amt,
       b.n_online, b.n_in_person,
       coalesce(md.modal_region, '') modal_region, b.n_regions,
       coalesce(h.home_regions, '[]') home_regions, coalesce(pr.usual_products, '[]') usual_products,
       coalesce(pe.usual_p_email, '') usual_p_email, b.n_devices_seen, coalesce(d.known_device_ids, '[]') known_device_ids,
       '[]' AS recurring_amounts,              -- filled by recurring_amounts()
       coalesce(c.n_prior_fraud_cases, 0) n_prior_fraud_cases, coalesce(c.n_prior_cleared_cases, 0) n_prior_cleared_cases,
       coalesce(r.ring_id, '') ring_id, coalesce(l.burst_lookalike_ids, '[]') burst_lookalike_ids,
       '[]' AS region_cluster_30d,             -- filled by region_cluster_30d()
       -1 AS wcc_id, 0 AS device_degree, 0.0 AS fraud_ppr   -- written later by graph/run_algos.py
FROM base b
JOIN cardmap m ON m.card_id = b.card_id
JOIN net n ON n.card_id = b.card_id
LEFT JOIN modal md ON md.card_id = b.card_id
LEFT JOIN home h ON h.card_id = b.card_id
LEFT JOIN prod pr ON pr.card_id = b.card_id
LEFT JOIN pem pe ON pe.card_id = b.card_id
LEFT JOIN dev d ON d.card_id = b.card_id
LEFT JOIN cases c ON c.card_id = b.card_id
LEFT JOIN ring r ON r.card_id = b.card_id
LEFT JOIN look l ON l.card_id = b.card_id
"""

# recurring_amounts: (amount ±1 %, product, place) groups with n ≥ 5.  Amount buckets are 2 %-wide
# log bins (round(ln(amt)/ln(1.02))), i.e. ±1 % around the bin centre; place = addr1 in person, p_email online.
RECURRING_SQL = """
CREATE OR REPLACE TEMP TABLE recurring AS
WITH g AS (
  SELECT card_id, product_cd, CASE WHEN channel = 'in_person' THEN addr1 ELSE p_email END place,
         amt, ts, round(ln(amt) / ln(1.02))::INT bucket
  FROM txc
), gaps AS (
  SELECT *, date_diff('second', lag(ts) OVER (PARTITION BY card_id, product_cd, place, bucket ORDER BY ts), ts) / 86400.0 gap_days
  FROM g
), grp AS (
  SELECT card_id, product_cd, place, bucket, count(*) n, round(quantile_cont(amt, 0.5), 2) amt,
         round(quantile_cont(gap_days, 0.5), 3) median_gap_days,
         round(coalesce(stddev_samp(gap_days) / nullif(avg(gap_days), 0), 0), 3) gap_cv
  FROM gaps GROUP BY 1, 2, 3, 4 HAVING count(*) >= 5
)
SELECT card_id, to_json(list_slice(list(struct_pack(amt := amt, product_cd := product_cd, place := place, n := n,
                                                    median_gap_days := median_gap_days, gap_cv := gap_cv)
                                        ORDER BY n DESC, amt), 1, 50))::VARCHAR recurring_amounts
FROM grp GROUP BY 1
"""

# region_cluster_30d: regions the card used (in person) in its last 30 days, with
#   n_txns_card, n_other_cards_30d (distinct other cards in the region in the window),
#   n_cards_with_fraud_case_30d (distinct other cards with a confirmed-fraud closed-case txn there in the window).
REGION_CLUSTER_SQL = """
CREATE OR REPLACE TEMP TABLE region_cluster AS
WITH win AS (SELECT id card_id, last_ts - INTERVAL 30 DAY w0, last_ts w1 FROM card_feat),
 mine AS (
  SELECT t.card_id, t.addr1, count(*) n_txns_card, w.w0, w.w1
  FROM txc t JOIN win w USING (card_id)
  WHERE t.channel = 'in_person' AND t.addr1 <> '' AND t.ts BETWEEN w.w0 AND w.w1 GROUP BY 1, 2, 4, 5
), rd AS (SELECT DISTINCT addr1, card_id, ts::DATE d_day FROM txc WHERE channel = 'in_person' AND addr1 <> ''),
 rdf AS (SELECT DISTINCT t.addr1, t.card_id, t.ts::DATE d_day FROM txc t JOIN pairs p USING (TransactionID) JOIN cc USING (case_id)
         WHERE p.src = 'closed' AND cc.outcome = 'confirmed_fraud' AND t.addr1 <> ''),
 oth AS (
  SELECT m.card_id, m.addr1, count(DISTINCT r.card_id) n_other_cards_30d
  FROM mine m JOIN rd r ON r.addr1 = m.addr1 AND r.card_id <> m.card_id AND r.d_day BETWEEN m.w0::DATE AND m.w1::DATE GROUP BY 1, 2
), frd AS (
  SELECT m.card_id, m.addr1, count(DISTINCT r.card_id) n_cards_with_fraud_case_30d
  FROM mine m JOIN rdf r ON r.addr1 = m.addr1 AND r.card_id <> m.card_id AND r.d_day BETWEEN m.w0::DATE AND m.w1::DATE GROUP BY 1, 2
)
SELECT m.card_id, to_json(list(struct_pack(addr1 := m.addr1, n_txns_card := m.n_txns_card,
                                           n_other_cards_30d := coalesce(o.n_other_cards_30d, 0),
                                           n_cards_with_fraud_case_30d := coalesce(f.n_cards_with_fraud_case_30d, 0))
                               ORDER BY m.n_txns_card DESC, m.addr1))::VARCHAR region_cluster_30d
FROM mine m LEFT JOIN oth o USING (card_id, addr1) LEFT JOIN frd f USING (card_id, addr1) GROUP BY 1
"""

# ----------------------------------------------------------------------------------------
# 6. txn_feat — txn_base + ring_hit + burst_id (+ cms_p placeholder)
# ----------------------------------------------------------------------------------------
TXN_FEAT_SQL = """
CREATE OR REPLACE TABLE txn_feat AS
SELECT b.TransactionID, b.card_id, b.ts, b.card_seq, b.prior_in_region, b.prior_on_dev, b.prior_pem, b.prior_pcd, b.prior_chan,
       b.prior_med_amt, b.prior_max_amt, b.prev_txn_id, b.gap_seconds,
       (b.device_id <> '' AND b.device_id = c.ring_id) AS ring_hit,
       coalesce(bm.burst_id, '') AS burst_id,
       CAST(NULL AS DOUBLE) AS cms_p,
       CAST(NULL AS VARCHAR) AS cms_fold
FROM txn_base b
JOIN card_feat c ON c.id = b.card_id
LEFT JOIN burst_member bm USING (TransactionID)
"""


_STAGES: list[tuple[str, float]] = []


def _run(con, label, sql):
    step(f"building {label}")
    t0 = time.time()
    con.execute(sql)
    dt = time.time() - t0
    _STAGES.append((label, dt))
    ok(f"{label} in {dt:.1f}s")


def build_all(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("PRAGMA threads=8")
    _STAGES.clear()
    t0 = time.time()
    if not con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='cardmap'").fetchone()[0]:
        step("cardmap absent: deriving it first (etl.card_rule)")
        build_cardmap(con)
    _run(con, "txc", TXC_SQL)
    _run(con, "txn_base", TXN_BASE_SQL)
    _run(con, "burst_member", BURST_SQL)
    _run(con, "device_profile", DEVICE_SQL)
    _run(con, "card_feat", CARD_SQL)
    _run(con, "recurring", RECURRING_SQL)
    con.execute("UPDATE card_feat SET recurring_amounts = r.recurring_amounts FROM recurring r WHERE card_feat.id = r.card_id")
    _run(con, "region_cluster", REGION_CLUSTER_SQL)
    con.execute("UPDATE card_feat SET region_cluster_30d = r.region_cluster_30d FROM region_cluster r WHERE card_feat.id = r.card_id")
    keep = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='txn_feat'").fetchone()[0]
    if keep:   # a previous cms_train.py run: carry cms_p / cms_fold over (features are deterministic)
        con.execute("CREATE OR REPLACE TEMP TABLE _cms AS SELECT TransactionID, cms_p, cms_fold FROM txn_feat WHERE cms_p IS NOT NULL")
    _run(con, "txn_feat", TXN_FEAT_SQL)
    if keep:
        con.execute("UPDATE txn_feat SET cms_p = c.cms_p, cms_fold = c.cms_fold FROM _cms c WHERE txn_feat.TransactionID = c.TransactionID")
        ok("carried cms_p / cms_fold over from the previous etl.cms_train run")
    con.execute("DROP TABLE txn_base")
    t = Table(Col("stage", max_width=18), Col("seconds", align="right", width=8),
              title="build stages")
    for label, dt in _STAGES:
        t.add_row(label, f"{dt:.1f}")
    t.add_row("TOTAL", f"{time.time() - t0:.1f}")
    t.print()


def report(con: duckdb.DuckDBPyConnection) -> dict:
    """The checks the plan asks for (also printed).  Returns a dict for tests."""
    q = lambda s, *a: con.execute(s, list(a)).fetchall()  # noqa: E731
    out = {}
    out["rows"] = {t: q(f"SELECT count(*) FROM {t}")[0][0] for t in ("txc", "txn_feat", "device_profile", "card_feat", "burst_member")}
    out["is_strong"] = q("SELECT count(*) FROM device_profile WHERE is_strong")[0][0]
    out["ring_profile"] = q("SELECT is_strong, n_cards_alltime, n_cards_30d, n_txns, n_proxy, n_fraud_cases FROM device_profile WHERE id = ?", RING_PROFILE)
    out["ring_id_cards"] = q("SELECT count(*) FROM card_feat WHERE ring_id <> ''")[0][0]
    out["ring_id_exact_profile_cards"] = q("SELECT count(*) FROM card_feat WHERE ring_id = ?", RING_PROFILE)[0][0]
    out["ring_hit_txns"] = q("SELECT count(*) FROM txn_feat WHERE ring_hit")[0][0]
    out["burst"] = {
        "bursts": q("SELECT count(DISTINCT burst_id) FROM burst_member")[0][0],
        "txns": q("SELECT count(*) FROM txn_feat WHERE burst_id <> ''")[0][0],
        "novdec_cards": q("SELECT count(DISTINCT card_id) FROM burst_member WHERE ts >= '2016-11-01'")[0][0],
        "hhg006": q("SELECT TransactionID, burst_id, amt FROM burst_member WHERE card_id = 'C07297-K1' ORDER BY ts"),
    }
    out["modal_region"] = {c: q("SELECT modal_region FROM card_feat WHERE id = ?", c)[0][0] for c in ("C09933-K2", "C08623-K2", "C02354-K2", "C12382-K1")}
    out["recurring"] = {c: q("SELECT recurring_amounts FROM card_feat WHERE id = ?", c)[0][0] for c in ("C02354-K2", "C08623-K2")}
    out["next_ties"] = q("SELECT count(*) FROM txn_feat WHERE gap_seconds = 0")[0][0]
    render_report(out)
    return out


# ----------------------------------------------------------------------------------------
# rendering — display only: `report()` returns exactly the dict it always did
# ----------------------------------------------------------------------------------------

def _recurring_table(card_id: str, blob: str) -> Table:
    """One row per recurring (amount, product, place) group — never the raw JSON blob."""
    t = Table(
        Col("amount", align="right", width=10),
        Col("product", width=7, align="center"),
        Col("place", max_width=12),
        Col("n", align="right", width=5),
        Col("median gap (d)", align="right", width=14),
        Col("gap cv", align="right", width=7),
        title=f"card_feat.recurring_amounts - {card_id}",
    )
    try:
        groups = json.loads(blob or "[]")
    except json.JSONDecodeError:
        t.add_row(str(blob))
        return t
    for g in groups:
        t.add_row(money(g.get("amt")), g.get("product_cd"), g.get("place"), f"{g.get('n', 0):,}",
                  f"{g.get('median_gap_days', 0):.3f}", f"{g.get('gap_cv', 0):.3f}")
    t.caption = f"{len(groups)} group(s) with n >= 5"
    return t


def render_report(out: dict) -> None:
    """Print the `report()` dict as aligned tables (no Python dict/list reprs, no JSON blobs)."""
    rows = out["rows"]
    t = Table(
        Col("table", max_width=16),
        Col("expected", align="right", width=10),
        Col("actual", align="right", width=10),
        Col("result", width=6, align="center"),
        title="derived tables",
    )
    mismatched = []
    for name, got in rows.items():
        want = EXPECTED_ROWS.get(name)
        good = want is None or got == want
        if not good:
            mismatched.append(f"{name}: expected {want:,}, got {got:,}")
        t.add_row(name, f"{want:,}" if want is not None else "-", f"{got:,}",
                  "PASS" if want is not None and good else "-" if want is None else "FAIL",
                  style=None if good else "red")
    t.print()
    for m in mismatched:
        warn(m)
    if not mismatched:
        ok("every contracted row count matches")

    rule("device ring")
    dp = Table(
        Col("attribute", max_width=20), Col("value", align="right", width=12),
        title="device_profile of the ring profile",
        caption=f"id = {RING_PROFILE}",
    )
    if out["ring_profile"]:
        is_strong, n_cards_all, n_cards_30d, n_txns, n_proxy, n_fraud = out["ring_profile"][0]
        for k, v in (("is_strong", "yes" if is_strong else "no"), ("n_cards_alltime", f"{n_cards_all:,}"),
                     ("n_cards_30d", f"{n_cards_30d:,}"), ("n_txns", f"{n_txns:,}"),
                     ("n_proxy", f"{n_proxy:,}"), ("n_fraud_cases", f"{n_fraud:,}")):
            dp.add_row(k, v)
    dp.print()
    g = Table(Col("measure", max_width=38), Col("value", align="right", width=12), title="ring / burst reach")
    g.add_row("strong device profiles", f"{out['is_strong']:,}")
    g.add_row("cards with a ring_id", f"{out['ring_id_cards']:,}")
    g.add_row("cards on this exact ring profile", f"{out['ring_id_exact_profile_cards']:,}")
    g.add_row("transactions with ring_hit", f"{out['ring_hit_txns']:,}")
    g.add_row("bursts (>=4 x $450-499.99 / 40 min)", f"{out['burst']['bursts']:,}")
    g.add_row("transactions in a burst", f"{out['burst']['txns']:,}")
    g.add_row("cards bursting in Nov-Dec", f"{out['burst']['novdec_cards']:,}")
    g.add_row("NEXT edges with gap_seconds = 0", f"{out['next_ties']:,}")
    g.print()

    rule("HHG-006 burst (C07297-K1)")
    b = Table(Col("txn id", align="right", width=10), Col("burst id", max_width=22),
              Col("amount", align="right", width=10), title="burst_member rows, ts order")
    for txn_id, burst_id, amt in out["burst"]["hhg006"]:
        b.add_row(txn_id, burst_id, money(amt))
    b.print()

    rule("spot checks")
    m = Table(Col("card", width=10), Col("modal_region", max_width=14), title="modal in-person region")
    for card, region in out["modal_region"].items():
        m.add_row(card, region)
    m.print()
    for card, blob in out["recurring"].items():
        _recurring_table(card, blob).print()


if __name__ == "__main__":  # pragma: no cover
    db = sys.argv[1] if len(sys.argv) > 1 else "data/hhgoa.duckdb"
    report_only = "--report" in sys.argv
    header("etl.features",
           "report only (no rebuild)" if report_only else
           "txc, txn_feat, burst_member, device_profile, card_feat in DuckDB",
           {"db": db, "threads": 8, "mode": "report" if report_only else "build + report"})
    con = duckdb.connect(db)
    if not report_only:
        build_all(con)
    rep = report(con)
    summary("etl.features complete",
            {"txc": f"{rep['rows']['txc']:,}", "txn_feat": f"{rep['rows']['txn_feat']:,}",
             "device_profile": f"{rep['rows']['device_profile']:,}", "card_feat": f"{rep['rows']['card_feat']:,}",
             "burst_member": f"{rep['rows']['burst_member']:,}", "db": db})
