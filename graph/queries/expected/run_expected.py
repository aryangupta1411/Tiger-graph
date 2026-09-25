"""run_expected.py — computes the expected output of every read query on named exam cases with DuckDB.

Usage (from the repo root, or anywhere):
    python graph/queries/expected/run_expected.py [--rebuild]
Builds expected/_model.duckdb from the ETL DuckDB (data/hhgoa.duckdb — txc, txn_feat, card_feat, device_profile,
closed_case_parsed, closed_case_txn, cp; override with HHGOA_DB) on first run, then executes expected/<query>.sql with the parameters of the SPECS table below and writes
expected/<query>__<case>.json. tests/live/test_query_contracts.py compares these files with the installed
queries' output (after mcp/normalize.py) field by field.
"""

import datetime
import decimal
import json
import os
import re
import sys
import time

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, REPO)                       # repo root: this runs as a plain file, not as a package

from ops.console import Col, Table, detail, fail, header, ok, step, summary  # noqa: E402

DB = os.environ.get("HHGOA_DB") or os.environ.get("HHGOA_DUCKDB") or os.path.join(REPO, "data", "hhgoa.duckdb")
MODEL = os.path.join(HERE, "_model.duckdb")
RING = "SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080"

# (query, case label, params) — params are exactly what the MCP call sends, minus the {"id": ...} wrapper.
SPECS = [
    ("case_context", "HHG-001", {"t": "3514030", "as_of": "2016-12-05 01:55:28"}),
    ("case_context", "HHG-014", {"t": "3478561", "as_of": "2016-11-22 20:11:00"}),
    ("card_profile", "HHG-001", {"c": "C12382-K1", "as_of": "2016-12-05 01:55:28"}),
    ("card_profile", "HHG-011", {"c": "C11923-K2", "as_of": "2016-12-29 06:27:44"}),
    ("card_window", "HHG-002", {"c": "C11891-K1", "from_ts": "2016-11-19 23:27:07", "to_ts": "2016-11-22 23:27:07", "max_rows": 60}),
    ("card_window", "HHG-006", {"c": "C07297-K1", "from_ts": "2016-11-20 02:30:00", "to_ts": "2016-11-22 02:30:00", "max_rows": 60}),
    ("region_history", "HHG-007", {"c": "C09933-K2", "addr1": "264.0", "as_of": "2016-12-05 03:46:14"}),
    ("region_history", "HHG-001", {"c": "C12382-K1", "addr1": "444.0", "as_of": "2016-12-05 01:55:28"}),
    ("device_history", "HHG-014", {"c": "C13487-K1", "d": RING, "as_of": "2016-11-22 20:11:00"}),
    ("device_neighbors", "HHG-014-preopen", {"d": RING, "from_ts": "2016-11-01 00:00:00", "to_ts": "2016-11-22 20:11:00", "max_cards": 60}),
    ("device_neighbors", "HHG-014-wave", {"d": RING, "from_ts": "2016-11-01 00:00:00", "to_ts": "2016-12-31 23:59:59", "max_cards": 60}),
    ("device_neighbors", "generic-weak", {"d": "NULL | NULL | chrome 66.0 | NULL", "from_ts": "2016-11-01 00:00:00", "to_ts": "2016-12-31 23:59:59", "max_cards": 60}),
    ("email_neighbors", "HHG-003", {"e": "me.com", "c": "C08623-K2", "from_ts": "2016-11-10 15:01:21", "to_ts": "2016-12-10 15:01:21"}),
    ("shared_origin_scan", "HHG-014", {"c": "C13487-K1", "as_of": "2016-11-22 20:11:00", "days": 30}),
    ("card_testing_check", "HHG-011", {"c": "C11923-K2", "as_of": "2016-12-29 06:27:44", "small": 5.0, "window_min": 60, "min_n": 3, "big": 100.0, "lookahead_h": 48}),
    ("under_threshold_burst", "HHG-006", {"c": "C07297-K1", "as_of": "2016-11-22 02:30:00"}),
    ("recurring_charge_check", "HHG-018", {"t": "3491361", "tol": 0.01, "as_of": "2016-11-27 14:41:26"}),
    ("recurring_charge_check", "HHG-003", {"t": "3530164", "tol": 0.01, "as_of": "2016-12-10 15:01:21"}),
    ("episode_candidates", "HHG-007", {"t": "3514948", "as_of": "2016-12-05 03:46:14", "gap_h": 48}),
    ("episode_candidates", "HHG-008", {"t": "3558054", "as_of": "2016-12-20 03:08:56", "gap_h": 48}),
    ("prior_cases_for_customer", "HHG-018", {"cu": "C02354", "as_of": "2016-11-27 14:41:26"}),
    ("ring_profile", "HHG-014", {"c": "C13487-K1", "as_of": "2016-11-22 20:11:00"}),
    ("case_subgraph", "HHG-014", {"c": "C13487-K1", "as_of": "2016-11-22 20:11:00", "hours": 72}),
    ("post_open_activity", "HHG-009", {"c": "C08299-K1", "opened_at": "2016-12-28 17:10:53", "days": 7}),
    ("post_open_activity", "HHG-014", {"c": "C13487-K1", "opened_at": "2016-11-22 20:11:00", "days": 7}),
]


def build_model():
    if os.path.exists(MODEL):
        os.remove(MODEL)
    con = duckdb.connect(MODEL)
    con.execute("PRAGMA threads=8")
    con.execute(f"ATTACH '{DB}' AS hh (READ_ONLY)")
    con.execute(open(os.path.join(HERE, "_model.sql")).read())
    con.close()


def _json(o):
    if isinstance(o, decimal.Decimal):
        return float(o)
    if isinstance(o, (datetime.datetime, datetime.date)):
        return o.strftime("%Y-%m-%d %H:%M:%S")
    raise TypeError(str(type(o)))


def main():
    rebuild = "--rebuild" in sys.argv or not os.path.exists(MODEL)
    header(
        "graph.queries.expected.run_expected",
        "DuckDB ground truth for every read query on the named exam cases (offline, no workspace)",
        {"source db": DB, "model": os.path.relpath(MODEL, REPO), "model build": "rebuild" if rebuild else "reuse",
         "specs": len(SPECS), "out": os.path.relpath(HERE, REPO) + "/<query>__<case>.json"},
    )
    if rebuild:
        step("building the model DuckDB from the ETL tables")
        t = time.time()
        build_model()
        ok(f"model built in {time.time() - t:.1f}s")
    con = duckdb.connect(MODEL, read_only=True)

    t_all = time.time()
    results = Table(
        Col("#", align="right", width=3),
        Col("query", max_width=24),
        Col("case", max_width=17),
        Col("ms", align="right", width=7),
        Col("fields", align="right", width=6),
        Col("expectation file", max_width=46),
        title=f"{len(SPECS)} expectation files",
    )
    errors = []
    for i, (q, case, params) in enumerate(SPECS, 1):
        sql = open(os.path.join(HERE, f"{q}.sql")).read()
        t = time.time()
        code = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
        used = {k: v for k, v in params.items() if re.search(rf"\${k}\b", code)}  # DuckDB rejects unused named params
        try:
            cur = con.execute(sql, used)
        except Exception as e:  # keep going so one bad SQL does not hide the others
            errors.append((q, case, str(e)))
            results.add_row(i, q, case, "-", "-", f"ERROR {str(e)[:60]}", style="red")
            continue
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
        out = dict(zip(cols, row))
        ms = (time.time() - t) * 1000
        path = os.path.join(HERE, f"{q}__{case}.json")
        # File content is a checked-in artefact: plain json.dump, never a display formatter.
        with open(path, "w") as fh:
            json.dump({"query": q, "case": case, "params": params, "expected": out}, fh, indent=1, default=_json)
        results.add_row(i, q, case, f"{ms:.0f}", len(cols), os.path.basename(path))
    results.print()

    for q, case, err in errors:
        fail(f"{q} / {case} did not run")
        detail(err[:300])
    if not errors:
        ok(f"{len(SPECS)} expectation files written")
    summary(
        "expectations rebuilt" if not errors else "expectations incomplete",
        {"specs": len(SPECS), "written": len(SPECS) - len(errors), "errors": len(errors),
         "seconds": f"{time.time() - t_all:.1f}", "out": os.path.relpath(HERE, REPO)},
        status="ok" if not errors else "fail",
    )


if __name__ == "__main__":
    main()
