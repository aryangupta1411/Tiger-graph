"""etl/export_graph_csvs.py — DuckDB derived tables -> headerless, TAB-separated, unquoted chunk files
in data/out/, in EXACTLY the column order of contracts/csv_columns.yaml (asserted at run time).

    python -m etl.export_graph_csvs data/hhgoa.duckdb --out data/out --yaml contracts/csv_columns.yaml
    python -m etl.export_graph_csvs data/hhgoa.duckdb --out data/out --sample 1000   # first 1,000 rows per file
    python -m etl.export_graph_csvs data/hhgoa.duckdb --out data/out --only fraud_pattern closed_case   # re-export some files

This module is the ONLY writer of data/out/fraud_pattern.csv (FRAUD_PATTERNS below; rag/chunk.py stopped writing
it). rule_refs is a comma-separated list of policy rule ids.

Why tab + no quoting: the TigerGraph loader has no escape character, JSON attributes contain `"`,
one device id contains `'`, notes contain `,`.  No field in this dataset contains a tab or a newline —
every writer asserts that and fails loudly otherwise.  Transaction files are split at 45 MB.
Requires: txc, txn_feat (with cms_p filled by etl/cms_train.py), device_profile, card_feat,
closed_case_parsed, closed_case_txn, closed_case_conn, cardmap.
"""
from __future__ import annotations

import argparse
import json
import os
import time

import duckdb
import yaml

from ops.console import Col, Table, fail, header, ok, progress, step, summary

SEP = "\t"
CHUNK_BYTES = 45 * 1024 * 1024
FLOAT2 = "printf('%.2f', {c})"
DT = "strftime({c}, '%Y-%m-%d %H:%M:%S')"
BOOL = "CASE WHEN {c} THEN 'true' ELSE 'false' END"
INT = "CAST({c} AS VARCHAR)"
STR = "coalesce({c}, '')"

FRAUD_PATTERNS = [
    ("card_testing", "A stolen card number is checked before use: three or more tiny online authorizations, often under $5, then a larger purchase. Confirmed by the sequence itself.", "R5"),
    ("card_not_present_fraud", "The number is used online without the card. Amounts and products that do not fit the cardholder's history, often in a burst of two to four within 48 hours. One unusual online purchase alone is ambiguous: verify.", "R1,R2,R3,R4"),
    ("card_not_present_new_device", "Card-not-present fraud where the identity record marks the device as New for this account, sometimes behind a proxy. Stronger than plain card-not-present fraud, still not proof: people buy new phones.", "R1,R2,R3,R4"),
    ("out_of_region_use", "Card-present purchases in a billing region the cardholder has no history in, while their normal activity continues at home. Several days of purchases in one new region is a trip, not a clone.", "R2,R3"),
    ("account_takeover", "Mixed-channel activity inconsistent with the cardholder, often with device and match-flag anomalies, pointing to stolen credentials rather than a stolen number.", "R1,R2,R10"),
    ("undocumented", "Activity that fits none of the documented patterns but shows coordinated or repeated abuse across customers; described in the agent's own words.", "R9,3a"),
    ("none", "No fraud pattern: the alert was cleared as legitimate.", "R3"),
]

# name -> SQL expression (VARCHAR) per file.  Column ORDER is taken from the YAML; this dict must cover every name.
EXPR = {
    "customer": {
        "id": STR.format(c="c.customer_id"), "n_cards": INT.format(c="c.n_cards"), "n_txns": INT.format(c="c.n_txns"),
        "n_closed_cases": INT.format(c="c.n_closed_cases"),
    },
    "card": {
        "id": STR.format(c="c.id"), "customer_id": STR.format(c="c.customer_id"), "card_type": STR.format(c="c.card_type"),
        "card_network": STR.format(c="c.card_network"), "n_txns": INT.format(c="c.n_txns"),
        "first_ts": DT.format(c="c.first_ts"), "last_ts": DT.format(c="c.last_ts"),
        "median_amt": FLOAT2.format(c="c.median_amt"), "p90_amt": FLOAT2.format(c="c.p90_amt"), "max_amt": FLOAT2.format(c="c.max_amt"),
        "max_in_person_amt": FLOAT2.format(c="c.max_in_person_amt"), "n_online": INT.format(c="c.n_online"),
        "n_in_person": INT.format(c="c.n_in_person"), "modal_region": STR.format(c="c.modal_region"), "n_regions": INT.format(c="c.n_regions"),
        "home_regions": STR.format(c="c.home_regions"), "usual_products": STR.format(c="c.usual_products"),
        "usual_p_email": STR.format(c="c.usual_p_email"), "n_devices_seen": INT.format(c="c.n_devices_seen"),
        "known_device_ids": STR.format(c="c.known_device_ids"), "recurring_amounts": STR.format(c="c.recurring_amounts"),
        "n_prior_fraud_cases": INT.format(c="c.n_prior_fraud_cases"), "n_prior_cleared_cases": INT.format(c="c.n_prior_cleared_cases"),
        "ring_id": STR.format(c="c.ring_id"), "burst_lookalike_ids": STR.format(c="c.burst_lookalike_ids"),
        "region_cluster_30d": STR.format(c="c.region_cluster_30d"), "wcc_id": INT.format(c="c.wcc_id"),
        "device_degree": INT.format(c="c.device_degree"), "fraud_ppr": "printf('%.6f', c.fraud_ppr)",
    },
    "device_profile": {
        "id": STR.format(c="d.id"), "device_info": STR.format(c="d.device_info"), "os": STR.format(c="d.os"),
        "browser": STR.format(c="d.browser"), "screen": STR.format(c="d.screen"), "n_txns": INT.format(c="d.n_txns"),
        "n_cards_alltime": INT.format(c="d.n_cards_alltime"), "n_cards_30d": INT.format(c="d.n_cards_30d"),
        "n_proxy": INT.format(c="d.n_proxy"), "first_seen": DT.format(c="d.first_seen"), "last_seen": DT.format(c="d.last_seen"),
        "n_fraud_cases": INT.format(c="d.n_fraud_cases"), "is_strong": BOOL.format(c="d.is_strong"),
    },
    "email_domain": {"id": STR.format(c="e.id"), "n_txns": INT.format(c="e.n_txns"), "n_cards": INT.format(c="e.n_cards")},
    "billing_region": {"id": STR.format(c="r.id"), "country": STR.format(c="r.country"), "n_txns": INT.format(c="r.n_txns"), "n_cards": INT.format(c="r.n_cards")},
    "fraud_pattern": {"id": STR.format(c="f.id"), "description": STR.format(c="f.description"), "rule_refs": STR.format(c="f.rule_refs")},
    "closed_case": {
        "id": STR.format(c="k.id"), "customer_id": STR.format(c="k.customer_id"), "card_id": STR.format(c="k.card_id"),
        "opened_at": DT.format(c="k.opened_at"), "closed_at": DT.format(c="k.closed_at"), "outcome": STR.format(c="k.outcome"),
        "pattern": STR.format(c="k.pattern"), "first_fraud_txn_id": STR.format(c="k.first_fraud_txn_id"), "n_txns": INT.format(c="k.n_txns"),
        "exposure_usd": FLOAT2.format(c="k.exposure_usd"), "actions_taken": STR.format(c="k.actions_taken"),
        "report_filed": BOOL.format(c="k.report_filed"), "analyst_notes": STR.format(c="k.analyst_notes"),
        "template_id": STR.format(c="k.template_id"), "note_region": STR.format(c="k.note_region"), "note_device": STR.format(c="k.note_device"),
        "note_evidence_type": STR.format(c="k.note_evidence_type"), "embed_text": STR.format(c="k.embed_text"),
    },
    "txn": {
        "id": INT.format(c="t.TransactionID"), "card_id": STR.format(c="t.card_id"), "customer_id": STR.format(c="t.customer_id"),
        "ts": DT.format(c="t.ts"), "ts_str": STR.format(c="t.ts_str"), "amt": FLOAT2.format(c="t.amt"), "product_cd": STR.format(c="t.product_cd"),
        "channel": STR.format(c="t.channel"), "addr1": STR.format(c="t.addr1"), "addr2": STR.format(c="t.addr2"),
        "dist1": FLOAT2.format(c="t.dist1"), "dist2": FLOAT2.format(c="t.dist2"), "p_email": STR.format(c="t.p_email"),
        "r_email": STR.format(c="t.r_email"), "risk_score": "printf('%.4f', t.risk_score)", "has_identity": BOOL.format(c="t.has_identity"),
        "device_new": STR.format(c="t.device_new"), "proxy": STR.format(c="t.proxy"), "device_type": STR.format(c="t.device_type"),
        "device_id": STR.format(c="t.device_id"), "c_counts": STR.format(c="t.c_counts"), "d_deltas": STR.format(c="t.d_deltas"),
        "m_flags": STR.format(c="t.m_flags"), "cms_p": "printf('%.6f', f.cms_p)", "card_seq": INT.format(c="f.card_seq"),
        "prior_in_region": INT.format(c="f.prior_in_region"), "prior_on_dev": INT.format(c="f.prior_on_dev"),
        "prior_pem": INT.format(c="f.prior_pem"), "prior_pcd": INT.format(c="f.prior_pcd"),
        "prior_med_amt": FLOAT2.format(c="f.prior_med_amt"), "prior_max_amt": FLOAT2.format(c="f.prior_max_amt"),
        "ring_hit": BOOL.format(c="f.ring_hit"), "burst_id": STR.format(c="f.burst_id"),
    },
    "owns": {"customer_id": STR.format(c="m.customer_id"), "card_id": STR.format(c="m.card_id")},
    "next": {"from_id": INT.format(c="f.prev_txn_id"), "to_id": INT.format(c="f.TransactionID"), "gap_seconds": INT.format(c="f.gap_seconds")},
    "involves": {"case_id": STR.format(c="x.case_id"), "txn_id": INT.format(c="x.TransactionID")},
    "on_card": {"case_id": STR.format(c="k.id"), "card_id": STR.format(c="k.card_id")},
    "connected_to": {"case_id": STR.format(c="x.case_id"), "card_id": STR.format(c="x.card_id")},
    "matches": {"case_id": STR.format(c="k.id"), "pattern_id": "CASE WHEN k.outcome = 'cleared' THEN 'none' ELSE k.pattern END"},
}

FROM = {
    "customer": """FROM (SELECT customer_id, count(DISTINCT card_id) n_cards, count(*) n_txns,
                          (SELECT count(*) FROM cc WHERE cc.customer_id = txc.customer_id) n_closed_cases
                   FROM txc GROUP BY 1) c ORDER BY c.customer_id""",
    "card": "FROM card_feat c ORDER BY c.id",
    "device_profile": "FROM device_profile d ORDER BY d.id",
    "email_domain": """FROM (SELECT id, count(*) n_txns, count(DISTINCT card_id) n_cards FROM (
                          SELECT DISTINCT id, TransactionID, card_id FROM (
                            SELECT p_email id, TransactionID, card_id FROM txc WHERE p_email <> ''
                            UNION ALL SELECT r_email, TransactionID, card_id FROM txc WHERE r_email <> '')) GROUP BY 1) e ORDER BY e.id""",
    "billing_region": """FROM (SELECT addr1 id, count(*) n_txns, count(DISTINCT card_id) n_cards,
                            (SELECT addr2 FROM txc t2 WHERE t2.addr1 = txc.addr1 GROUP BY 1 ORDER BY count(*) DESC, addr2 LIMIT 1) country
                         FROM txc WHERE addr1 <> '' GROUP BY 1) r ORDER BY r.id""",
    "fraud_pattern": "FROM _fraud_pattern f ORDER BY f.ord",
    "closed_case": "FROM closed_case_parsed k ORDER BY k.id",
    "txn": "FROM txc t JOIN txn_feat f USING (TransactionID) ORDER BY t.TransactionID",
    "owns": "FROM cardmap m ORDER BY m.customer_id, m.card_id",
    "next": "FROM txn_feat f WHERE f.prev_txn_id IS NOT NULL ORDER BY f.card_id, f.ts, f.TransactionID",
    "involves": "FROM closed_case_txn x ORDER BY x.case_id, x.TransactionID",
    "on_card": "FROM closed_case_parsed k ORDER BY k.id",
    "connected_to": "FROM closed_case_conn x ORDER BY x.case_id, x.card_id",
    "matches": "FROM closed_case_parsed k ORDER BY k.id",
}


def file_key(path: str) -> str:
    base = os.path.basename(path).replace(".csv", "")
    return "txn" if base.startswith("txn_") else base


def build_select(key: str, cols: list[str]) -> str:
    exprs = EXPR[key]
    missing = [c for c in cols if c not in exprs]
    assert not missing, f"{key}: no expression for {missing}"
    return "SELECT " + ", ".join(f"{exprs[c]} AS {c}" for c in cols) + " " + FROM[key]


def write_file(con, key, cols, out_path, sample=None, chunk=False):
    """Write rows tab-separated; returns list of (path, rows, bytes).  Asserts no tab/newline in any field."""
    sql = build_select(key, cols)
    if sample:
        sql += f" LIMIT {sample}"
    cur = con.execute(sql)
    written = []
    idx, rows, size = 0, 0, 0
    path = out_path if not chunk else out_path.replace("NNN", f"{idx:03d}")
    fh = open(path, "w", encoding="utf-8", newline="\n")
    while True:
        batch = cur.fetchmany(50_000)
        if not batch:
            break
        for r in batch:
            for v in r:
                if v is None or "\t" in v or "\n" in v or "\r" in v:
                    raise ValueError(f"{key}: bad field {v!r} in row {r[:3]}")
            line = SEP.join(r) + "\n"
            fh.write(line)
            rows += 1
            size += len(line.encode("utf-8"))
            if chunk and size >= CHUNK_BYTES:
                fh.close()
                written.append((path, rows, size))
                idx += 1
                rows = size = 0
                path = out_path.replace("NNN", f"{idx:03d}")
                fh = open(path, "w", encoding="utf-8", newline="\n")
    fh.close()
    if rows or not written:
        written.append((path, rows, size))
    return written


def export(db: str, out: str, yaml_path: str, sample: int | None = None, only: list[str] | None = None) -> dict:
    """Write every file of the YAML (or just the `only` keys, e.g. ["fraud_pattern"]); manifest.json is rewritten in
    full, or merged into the existing one when `only` is given."""
    t0 = time.time()
    os.makedirs(out, exist_ok=True)
    spec = yaml.safe_load(open(yaml_path))
    assert spec["format"]["separator"] == SEP and spec["format"]["quote"] == "none"
    con = duckdb.connect(db, read_only=True)
    con.execute("PRAGMA threads=8")
    keys = {file_key(path) for path in spec["files"]}
    if only:
        unknown = set(only) - keys
        assert not unknown, f"--only: unknown file keys {sorted(unknown)}; known: {sorted(keys)}"
    if not only or "txn" in only:
        assert con.execute("SELECT count(*) FROM txn_feat WHERE cms_p IS NULL").fetchone()[0] == 0, "run etl/cms_train.py first"
    con.execute("CREATE TEMP TABLE _fraud_pattern(ord INT, id VARCHAR, description VARCHAR, rule_refs VARCHAR)")
    con.executemany("INSERT INTO _fraud_pattern VALUES (?, ?, ?, ?)", [(i, *p) for i, p in enumerate(FRAUD_PATTERNS)])
    manifest_path = os.path.join(out, "manifest.json")
    report = {}
    if only and os.path.exists(manifest_path):
        report = json.load(open(manifest_path))
    todo = [(p, f) for p, f in spec["files"].items() if not only or file_key(p) in only]
    t = Table(
        Col("file", max_width=22),
        Col("rows", align="right", width=10),
        Col("bytes", align="right", width=13),
        Col("cols", align="right", width=5),
        title=f"data/out chunks (TAB-separated, headerless, unquoted){' - SAMPLE' if sample else ''}",
    )
    total_rows = total_bytes = 0
    with progress("exporting files", total=len(todo)) as p:
        for path, fspec in todo:
            key = file_key(path)
            cols = [c["name"] for c in fspec["columns"]]
            out_path = os.path.join(out, os.path.basename(path))
            parts = write_file(con, key, cols, out_path, sample=sample, chunk=(key == "txn"))
            for part_path, n, b in parts:
                report[os.path.basename(part_path)] = {"rows": n, "bytes": b, "columns": len(cols)}
                t.add_row(os.path.basename(part_path), f"{n:,}", f"{b:,}", len(cols))
                total_rows += n
                total_bytes += b
            p.advance()
    t.caption = f"{len(t)} file(s), {total_rows:,} rows, {total_bytes / 1e6:,.1f} MB"
    t.print()
    report["_elapsed_s"] = round(time.time() - t0, 1)
    json.dump(report, open(manifest_path, "w"), indent=1)
    ok(f"manifest -> {manifest_path} ({time.time() - t0:.1f}s)")
    return report


def verify(out: str, yaml_path: str) -> None:
    """Parse every written file back with DuckDB (tab, no quote) and check the column count."""
    spec = yaml.safe_load(open(yaml_path))
    con = duckdb.connect()
    step("re-reading every written file with DuckDB (delim=TAB, quote='')")
    t = Table(
        Col("file", max_width=22),
        Col("rows", align="right", width=10),
        Col("cols expected", align="right", width=13),
        Col("cols read", align="right", width=9),
        Col("result", width=6, align="center"),
        title="round-trip verification",
    )
    bad = []
    for path, fspec in spec["files"].items():
        n = len(fspec["columns"])
        base = os.path.basename(path)
        files = sorted(f for f in os.listdir(out) if (f.startswith("txn_") if base == "txn_NNN.csv" else f == base))
        for f in files:
            cols = con.execute(f"SELECT count(*) FROM (DESCRIBE SELECT * FROM read_csv('{os.path.join(out, f)}', delim='\t', quote='', escape='', header=false, all_varchar=true, columns={{{', '.join(f"'c{i}': 'VARCHAR'" for i in range(n))}}}))").fetchone()[0]
            rows = con.execute(f"SELECT count(*) FROM read_csv('{os.path.join(out, f)}', delim='\t', quote='', escape='', header=false, all_varchar=true, columns={{{', '.join(f"'c{i}': 'VARCHAR'" for i in range(n))}}})").fetchone()[0]
            good = cols == n
            if not good:
                bad.append((f, cols, n))
            t.add_row(f, f"{rows:,}", n, cols, "PASS" if good else "FAIL", style=None if good else "red")
    t.print()
    for f, cols, n in bad:
        fail(f"{f}: read {cols} columns, contract says {n}")
    assert not bad, bad
    ok(f"{len(t)} file(s) parse back at the contracted column count")


if __name__ == "__main__":  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("db", nargs="?", default="data/hhgoa.duckdb")
    ap.add_argument("--out", default="data/out")
    ap.add_argument("--yaml", default="contracts/csv_columns.yaml")
    ap.add_argument("--sample", type=int, default=None)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--only", nargs="+", default=None, help="file keys to (re)write, e.g. fraud_pattern closed_case txn")
    a = ap.parse_args()
    header("etl.export_graph_csvs",
           "DuckDB derived tables -> headerless TAB-separated chunks for the TigerGraph loader",
           {"db": a.db, "out": a.out, "contract": a.yaml,
            "only": " ".join(a.only) if a.only else "every file",
            "sample": a.sample if a.sample else "all rows", "verify": "yes" if a.verify else "no"})
    rep = export(a.db, a.out, a.yaml, a.sample, a.only)
    if a.verify:
        verify(a.out, a.yaml)
    written = [k for k in rep if not k.startswith("_")]
    summary("etl.export_graph_csvs complete",
            {"files": len(written), "rows": f"{sum(rep[k]['rows'] for k in written):,}",
             "bytes": f"{sum(rep[k]['bytes'] for k in written):,}",
             "out": a.out, "elapsed": f"{rep['_elapsed_s']}s"})
