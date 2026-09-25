"""graph/run_algos.py — the once-only graph-algorithm pass (PLAN §3.5 "Graph algorithms", day 4).

    uv run python graph/run_algos.py                        # install the six algo queries, project, wcc, degree, summary
    uv run python graph/run_algos.py --no-install           # queries already installed (re-run after a data reload)
    uv run python graph/run_algos.py --no-project           # keep the existing SHARES_DEVICE edges
    uv run python graph/run_algos.py --report runs/algos.json

Every call is graph/tg.run_query -> runInstalledQuery(name, params, timeout=600000, usePost=True), never the MCP.
  1. clear_shares_device()                                     drops every SHARES_DEVICE edge (idempotent re-run)
  2. project_shares_device(60, true)                           SHARES_DEVICE over strong profiles (n_shared, weight, via)
  3. tg_wcc(["Card"], ["SHARES_DEVICE"], 0, false, "wcc_id")   Card.wcc_id (library query, SYNTAX V1, file verbatim)
  4. tg_degree_cent(["Card"], ["SHARES_DEVICE"], ["SHARES_DEVICE"], true, false, 20, true, "device_degree", "", false)
                                                               Card.device_degree + top-20 hubs; the library stores a
                                                               DOUBLE via setAttr - if the server rejects DOUBLE->INT the
                                                               step is reported and step 5 is the result
  5. set_device_degree(true)                                   exact INT degree; wcc_id = -1 for isolated cards
  6. wcc_summary()                                             component sizes + the ring cards' component (blog numbers)
The library files are sent with `CREATE QUERY` rewritten to `CREATE OR REPLACE QUERY` at send time so the checked-in
text stays verbatim (algos/README.md) and re-runs need no DROP QUERY. Card.fraud_ppr stays 0 (tg_pagerank_pers is a
PLAN stretch item and is not fetched).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # repo root, for ops.console
from tg import GRAPH_DIR, backoff_delays, connect, ensure_awake, gsql, merged, run_query  # noqa: E402

from ops.console import Col, Table, detail, fail, header, joinlist, ok, step, summary, warn  # noqa: E402

GRAPH = "FraudGraph"
ALGOS = GRAPH_DIR / "algos"
OURS = ["clear_shares_device", "project_shares_device", "set_device_degree", "wcc_summary"]   # USE GRAPH + CREATE OR REPLACE
LIBRARY = ["tg_wcc", "tg_degree_cent"]                                                        # verbatim, no USE GRAPH
RING_PROFILE = "SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080"
RING_CARD = "C13487-K1"                                                                       # HHG-014


def _n(value) -> str:
    """A count as a right-alignable, thousands-separated cell; non-numbers pass through."""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def install(conn) -> None:
    for i, name in enumerate(OURS + LIBRARY, 1):
        step(f"[{i}/{len(OURS + LIBRARY)}] create or replace {name}")
        if name in OURS:
            gsql(conn, (ALGOS / f"{name}.gsql").read_text(), ok_if=("successfully created",))
        else:
            text = (ALGOS / f"{name}.gsql").read_text().replace("CREATE QUERY", "CREATE OR REPLACE QUERY", 1)
            gsql(conn, f"USE GRAPH {GRAPH}\n{text}", ok_if=("successfully created",))
    step(f"INSTALL QUERY {len(OURS + LIBRARY)} algorithm queries")
    t0 = time.time()
    gsql(conn, f"USE GRAPH {GRAPH}\nINSTALL QUERY {', '.join(OURS + LIBRARY)}", ok_if=("query installation finished",))
    ok(f"installed {len(OURS + LIBRARY)} algorithm queries in {time.time() - t0:.0f}s")


# What each algorithm writes back to the graph, so the run says so instead of leaving it to the reader.
WRITES_BACK = {
    "clear_shares_device": "deletes every SHARES_DEVICE edge",
    "project_shares_device": "SHARES_DEVICE edges (n_shared, weight, via)",
    "tg_wcc": "Card.wcc_id",
    "tg_degree_cent": "Card.device_degree (DOUBLE; may be rejected)",
    "set_device_degree": "Card.device_degree (INT), wcc_id = -1 for isolated cards",
    "wcc_summary": "nothing (reads component sizes)",
}
# The result keys worth showing per algorithm; everything else stays in --report.
SHOW = {
    "clear_shares_device": ["n_deleted"],
    "project_shares_device": ["n_inserts", "n_profiles", "n_pairs"],
    "tg_wcc": ["@@comp_sizes.size()", "n_components"],
    "tg_degree_cent": ["top_scores"],
    "set_device_degree": ["n_cards", "n_isolated"],
    "wcc_summary": ["n_components", "n_components_ge2", "n_isolated_cards"],
}


def result_cells(name: str, res: dict) -> list[str]:
    """`key=value` strings worth showing for one algorithm - never a raw list/dict repr."""
    if name == "tg_degree_cent":
        top = [r for r in (res.get("top_scores") or []) if isinstance(r, dict)]
        return [f"top_k={len(top)}"] + [f"{r.get('Vertex_ID', r.get('v_id', '?'))}={r.get('score')}" for r in top[:2]]
    cells = [f"{k}={res[k]}" for k in SHOW.get(name, []) if k in res]
    return cells or [f"{k}={v}" for k, v in list(res.items())[:4] if not isinstance(v, (list, dict))]


def settle_edge_count(conn, edge_type: str, max_wait_s: float = 90) -> int:
    """Wait for RESTPP's edge count to stop moving before an algorithm reads the edge set.

    getEdgeCount() lags a bulk insert by tens of seconds (same trap as graph/load_all.py verify_counts
    and rag/load_vectors.py stats parsing): a query that reads SHARES_DEVICE right after
    project_shares_device can see a small fraction of the real edges and silently produce a wrong
    answer instead of an error -- tg_wcc reported every Card isolated once, reading the graph seconds
    after a 109,507-edge insert. Polls until two consecutive reads agree, not a hard target: the
    insert's own n_inserts double-counts an undirected edge from both endpoints, so the converged count
    is not a number this script can predict in advance.
    """
    last = -1
    for delay in backoff_delays(max_wait_s, first=3.0, cap=15.0):
        n = conn.getEdgeCount(edge_type)
        if n == last:
            return n
        detail(f"{edge_type} edge count still settling: {n:,} (was {last:,})" if last >= 0 else f"{edge_type} edge count: {n:,}")
        last = n
        time.sleep(delay)
    return last


def run_step(conn, report: dict, name: str, params: dict) -> dict:
    """Run one installed algorithm query, timed. Results go in the table after the pass, not here."""
    step(f"{name} - {WRITES_BACK.get(name, 'runs')}")
    t0 = time.time()
    res = merged(run_query(conn, name, params, timeout_ms=600_000))
    dt = round(time.time() - t0, 1)
    report[name] = {"seconds": dt, "params": params, "result": res}
    ok(f"{name} in {dt:.1f}s")
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-install", action="store_true")
    ap.add_argument("--no-project", action="store_true")
    ap.add_argument("--max-cards-per-profile", type=int, default=60)
    ap.add_argument("--report", type=Path, default=None)
    args = ap.parse_args(argv)
    # graph/tg.py echoes every GSQL reply at INFO; WARNING keeps the resume retries and hard errors on stderr.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    stages = (["install"] if not args.no_install else []) + (["project"] if not args.no_project else []) + ["wcc", "degree", "summary"]
    header(
        "graph.run_algos",
        "the once-only graph-algorithm pass: SHARES_DEVICE projection, WCC, degree",
        {"graph": GRAPH, "stages": " -> ".join(stages), "timeout": "600s per query",
         "max cards/profile": args.max_cards_per_profile, "ring card": RING_CARD, "report": args.report or "-"},
    )

    conn = connect(timeout_ms=600_000)
    report: dict = {"started": time.strftime("%Y-%m-%d %H:%M:%S")}
    if not args.no_install:
        install(conn)

    if not args.no_project:
        run_step(conn, report, "clear_shares_device", {})
        proj = run_step(conn, report, "project_shares_device", {"max_cards_per_profile": args.max_cards_per_profile, "do_insert": True})
        if not proj.get("n_inserts"):
            fail("project_shares_device inserted nothing - is DeviceProfile.is_strong loaded?")
            detail(joinlist([f"{k}={v}" for k, v in proj.items()], sep="  ", empty="empty result"))
            summary("algorithm pass aborted", {"stage": "project_shares_device", "SHARES_DEVICE edges": 0}, status="fail")
            return 1
        step("settling SHARES_DEVICE edge count before tg_wcc reads it")
        settled = settle_edge_count(conn, "SHARES_DEVICE")
        ok(f"SHARES_DEVICE settled at {settled:,} edges")

    run_step(conn, report, "tg_wcc", {"v_type_set": ["Card"], "e_type_set": ["SHARES_DEVICE"], "print_limit": 0,
                                      "print_results": False, "result_attribute": "wcc_id", "file_path": ""})

    try:
        run_step(conn, report, "tg_degree_cent", {"v_type_set": ["Card"], "e_type_set": ["SHARES_DEVICE"], "reverse_e_type_set": ["SHARES_DEVICE"],
                                                  "in_degree": True, "out_degree": False, "top_k": 20, "print_results": True,
                                                  "result_attribute": "device_degree", "file_path": "", "normalize": False})
    except Exception as exc:  # noqa: BLE001 - DOUBLE -> INT setAttr rejected: set_device_degree below is the result
        report["tg_degree_cent"] = {"error": str(exc)[:500]}
        warn("tg_degree_cent failed; set_device_degree provides Card.device_degree")
        detail(str(exc)[:160])

    run_step(conn, report, "set_device_degree", {"reset_isolated_wcc": True})
    wcc = run_step(conn, report, "wcc_summary", {"ring_profile_id": RING_PROFILE, "top_n": 10})

    @ensure_awake()
    def _facts():
        return {"SHARES_DEVICE": conn.getEdgeCount("SHARES_DEVICE"),
                "ring_card": conn.getVerticesById("Card", RING_CARD, select="wcc_id,device_degree,ring_id")}

    facts = _facts()
    report["facts"] = facts

    timings = Table(
        Col("algorithm", max_width=24),
        Col("sec", align="right", width=7),
        Col("writes back to the graph", max_width=56),
        Col("result", max_width=58),
        title="algorithm pass, in order",
    )
    for name in ["clear_shares_device", "project_shares_device", "tg_wcc", "tg_degree_cent", "set_device_degree", "wcc_summary"]:
        r = report.get(name)
        if r is None:
            timings.add_row(name, "-", WRITES_BACK.get(name, ""), "skipped", style="yellow")
            continue
        if "error" in r:
            timings.add_row(name, "-", WRITES_BACK.get(name, ""), str(r["error"])[:120], style="red")
            continue
        timings.add_row(name, f"{r['seconds']:.1f}", WRITES_BACK.get(name, ""),
                        joinlist(result_cells(name, r["result"]), sep="  ", empty="-"))
    timings.print()

    top = wcc.get("top_components", []) or []
    comps = Table(
        Col("rank", align="right", width=4),
        Col("component", max_width=26),
        Col("cards", align="right", width=9),
        title="largest weakly-connected components (SHARES_DEVICE over strong device profiles)",
    )
    for i, c in enumerate(top[:3], 1):
        if isinstance(c, dict):
            comps.add_row(i, c.get("wcc_id", c.get("id", "?")), _n(c.get("n_cards", c.get("size", c.get("n", "?")))))
        else:
            comps.add_row(i, str(c), "")
    comps.print()

    ring = facts["ring_card"]
    ring_attrs = ring[0].get("attributes", {}) if isinstance(ring, list) and ring else ring
    summary(
        "algorithm pass complete",
        {"SHARES_DEVICE edges": _n(facts["SHARES_DEVICE"]), "components": _n(wcc.get("n_components", "?")),
         "components >= 2 cards": _n(wcc.get("n_components_ge2", "?")), "isolated cards": _n(wcc.get("n_isolated_cards", "?")),
         "ring card": f"{RING_CARD} {joinlist([f'{k}={v}' for k, v in (ring_attrs or {}).items()], sep='  ', empty='not found')}",
         "ring card components": joinlist(wcc.get("ring_card_components") or [], empty="-"),
         "report": args.report or "-"},
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
