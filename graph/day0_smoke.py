"""graph/day0_smoke.py — runs graph/day0_smoke.gsql block by block on the fresh workspace and fills in the day-0 records.

    uv run python graph/day0_smoke.py                       # every block + toy vectors + two-type search + 3-row tab load
    uv run python graph/day0_smoke.py --post-test-mb 150    # also POST a 150 MB CSV (Nginx body cap; seconds per 150 MB)
    uv run python graph/day0_smoke.py --drop-all            # ... and USE GLOBAL DROP ALL at the end
    uv run python graph/day0_smoke.py --only ddl_all_names two_type_vector_query
    uv run python graph/day0_smoke.py --report docs/day0_records.json

Prints one line per record of the day-0 workspace checks (Savanna sign-up, Database Secret, smoke query):
  ddl_all_names      every CREATE VERTEX / EDGE of schema.gsql accepted (reserved-word check)
  proxy_reserved     whether `proxy` as an attribute name is rejected (schema.gsql uses proxy_type)
  vectors            GLOBAL schema-change job adding VECTOR attributes accepted
  two_type_search    vectorSearch over {ClosedCase.note_emb, AgentCase.note_emb} with a mixed candidate_set returns
                     one vertex of EACH type (else: similar_prior_cases becomes two queries, PLAN §3.7)
  tab_load           sep="\\t" through POST /ddl and `"` `,` `'` `|#|` inside a field round-trip (module A open issue 1)
  gdbms_algo         IMPORT PACKAGE GDBMS_ALGO / CALL works on the Free tier (blog only)
  post_150mb         accepted size and seconds (chunk-size decision; the ETL default is 45 MiB)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # repo root, for ops.console
from tg import GRAPH_DIR, connect, ensure_awake, env, gsql, merged, run_query  # noqa: E402

from ops.console import Col, Table, detail, fail, header, joinlist, ok, step, summary, warn  # noqa: E402

SMOKE_GSQL = GRAPH_DIR / "day0_smoke.gsql"
GRAPH = "SmokeGraph"
INFORMATIONAL = {"probe_reserved_proxy", "gdbms_algo_import", "gdbms_algo_call"}   # failure is a record, not an error
TOY_VECTORS = [("ClosedCase", "CC-0001", {"outcome": "confirmed_fraud", "note_emb": [0.1, 0.2, 0.3, 0.4]}),
               ("ClosedCase", "CC-0002", {"outcome": "cleared", "note_emb": [0.9, 0.1, 0.1, 0.1]}),
               ("AgentCase", "AC-HHG-014", {"verdict": "fraud", "note_emb": [0.1, 0.2, 0.3, 0.5]})]
# What each day-0 record decides, so the terminal says it instead of leaving it to a separate checklist.
RECORD_MEANS = {
    "ddl_all_names": "every CREATE VERTEX / EDGE of schema.gsql accepted (no reserved word)",
    "proxy_reserved": "`proxy` rejected as an attribute name -> schema.gsql uses proxy_type",
    "vectors": "GLOBAL schema-change job adding VECTOR attributes accepted",
    "two_type_search": "one vectorSearch over two vertex types returns both -> similar_prior_cases stays one query",
    "one_type_search": "single-type vectorSearch works",
    "tab_load": 'sep="\\t" through POST /ddl; " , \' |#| round-trip inside a field',
    "gdbms_algo": "IMPORT PACKAGE GDBMS_ALGO / CALL works on the Free tier (blog only)",
    "post_150mb": "largest accepted POST body (the ETL chunks at 45 MiB are well under it)",
}
TAB_ROWS = [["SMOKE-1", "AC-SMOKE", "1", "2016-11-22 20:11:00", "evidence", '{"a":"b,c","d":"it\'s","e":"x|#|y","f":"F80\'S+ | NULL"}'],
            ["SMOKE-2", "AC-SMOKE", "2", "2016-11-22 20:12:00", "assessment", ""],
            ["SMOKE-3", "AC-SMOKE", "3", "2016-11-22 20:13:00", "sar", "plain, text; with 'quotes' and \"double\""]]


def blocks(text: str) -> list[tuple[str, str]]:
    """[(step name, gsql text)] from the `// ==== STEP <name>` markers."""
    out, name, buf = [], None, []
    for line in text.splitlines():
        m = re.match(r"// ==== STEP (\w+)", line)
        if m:
            if name:
                out.append((name, "\n".join(buf)))
            name, buf = m.group(1), []
        elif name and not line.strip().startswith("//"):
            buf.append(line)
    if name:
        out.append((name, "\n".join(buf)))
    return out


def send(conn, name: str, text: str, records: dict) -> bool:
    step(f"{name}")
    t0 = time.time()
    try:
        reply = gsql(conn, text, quiet=True)
        good = True
    except Exception as exc:  # noqa: BLE001
        reply, good = str(exc), False
    dt = time.time() - t0
    records[name] = {"ok": good, "seconds": round(dt, 1), "reply_tail": reply[-600:]}
    if good:
        ok(f"{name} in {dt:.1f}s")
    elif name in INFORMATIONAL:
        warn(f"{name} rejected in {dt:.1f}s - that is itself the record")
    else:
        fail(f"{name} failed in {dt:.1f}s")
    if not good:
        for line in _tail(reply, 6):
            detail(line)
    return good


def _tail(text: str, n: int) -> list[str]:
    """The last `n` non-empty lines of a reply, for `detail()` under a status line."""
    return [ln.strip() for ln in str(text).splitlines() if ln.strip()][-n:]


def wait_index(conn, timeout_s: int = 180) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        st = conn.getVectorIndexStatus(graphName=GRAPH)
        need = st.get("NeedRebuildServers", []) if isinstance(st, dict) else []
        if not need:
            return True
        time.sleep(3)
    return False


def vector_steps(conn, records: dict) -> None:
    step(f"upserting {len(TOY_VECTORS)} toy vectors")
    for vt, vid, attrs in TOY_VECTORS:
        conn.upsertVertex(vt, vid, attrs)
    records["vector_upsert"] = {"ok": True, "n": len(TOY_VECTORS)}
    step("waiting for the vector index to rebuild (up to 180s)")
    t0 = time.time()
    ready = wait_index(conn)
    records["index_ready"] = {"ok": ready, "seconds": round(time.time() - t0, 1)}
    (ok if ready else warn)(f"vector index {'ready' if ready else 'still rebuilding'} after {time.time() - t0:.0f}s")
    for q in ("smoke_two_type", "smoke_one_type"):
        step(f"{q} vectorSearch")
        try:
            res = run_query(conn, q, {"q": [0.1, 0.2, 0.3, 0.45], "k": 2}, timeout_ms=30_000)
            vs = merged(res).get("v", [])
            types = sorted({v.get("v_type") for v in vs})
            ids = sorted(v.get("v_id") for v in vs)
            records[q] = {"ok": True, "types": types, "ids": ids, "raw": json.dumps(res)[:800]}
            ok(f"{q} returned {len(vs)} vertices of type(s) {joinlist(types)}")
        except Exception as exc:  # noqa: BLE001
            records[q] = {"ok": False, "error": str(exc)[:600]}
            fail(f"{q}: {str(exc)[:160]}")
    two = records["smoke_two_type"]
    both = bool(two.get("ok")) and len(two.get("types", [])) == 2
    records["two_type_search"] = {"ok": both, "detail": two}
    (ok if both else warn)(f"two-type vectorSearch {'returns both types' if both else 'does NOT return both types - similar_prior_cases becomes two queries'}")


def tab_load_step(conn, records: dict) -> None:
    step("tab-separated POST /ddl load with quotes, commas and |#| inside a field")
    with tempfile.NamedTemporaryFile("w", suffix=".tsv", delete=False, encoding="utf-8", newline="") as fh:
        for r in TAB_ROWS:
            fh.write("\t".join(r) + "\n")
        path = fh.name
    try:
        res = conn.runLoadingJobWithFile(path, "f", "smoke_load_tab", sep="\t", eol="\n", timeout=60_000, sizeLimit=128_000_000)
        got = conn.getVerticesById("CaseEvent", [r[0] for r in TAB_ROWS], select="payload,kind,seq")
        by_id = {v["v_id"]: v["attributes"] for v in got}
        good = all(by_id.get(r[0], {}).get("payload") == r[5] and by_id.get(r[0], {}).get("kind") == r[4] for r in TAB_ROWS)
        records["tab_load"] = {"ok": good, "stats": json.dumps(res)[:600], "payload_roundtrip": {k: v.get("payload") for k, v in by_id.items()}}
        (ok if good else fail)(f"tab_load: {len(TAB_ROWS)} rows, payload round-trip {'exact' if good else 'CORRUPTED'}")
    except Exception as exc:  # noqa: BLE001
        records["tab_load"] = {"ok": False, "error": str(exc)[:600]}
        fail(f"tab_load: {str(exc)[:160]}")
    finally:
        os.unlink(path)


def post_test(conn, mb: int, records: dict) -> None:
    step(f"building and POSTing a {mb} MB CSV (Nginx body cap probe)")
    path = Path(tempfile.gettempdir()) / f"smoke_{mb}mb.csv"
    with path.open("w") as fh:
        i = 0
        while path.stat().st_size < mb * 1_000_000 if i % 100_000 == 0 else True:
            fh.write(f"X{i:09d},{i % 7},{i % 11},0\n")
            i += 1
            if i % 100_000 == 0 and fh.flush() is None and path.stat().st_size >= mb * 1_000_000:
                break
    size = path.stat().st_size
    t0 = time.time()
    try:
        res = conn.runLoadingJobWithFile(str(path), "f", "smoke_load_csv", sep=",", eol="\n", timeout=600_000, sizeLimit=200_000_000)
        records["post_150mb"] = {"ok": True, "bytes": size, "seconds": round(time.time() - t0, 1),
                                 "customer_count": conn.getVertexCount("Customer"), "stats": json.dumps(res)[:400]}
        ok(f"POST accepted {size / 1e6:,.0f} MB in {time.time() - t0:.0f}s")
    except Exception as exc:  # noqa: BLE001
        records["post_150mb"] = {"ok": False, "bytes": size, "seconds": round(time.time() - t0, 1), "error": str(exc)[:600]}
        fail(f"POST of {size / 1e6:,.0f} MB rejected: {str(exc)[:160]}")
    finally:
        path.unlink(missing_ok=True)


def _note(name: str, rec) -> str:
    """One short, human line per record for the checklist table."""
    if not isinstance(rec, dict):
        return "" if rec is None else str(rec)
    if rec.get("error"):
        return str(rec["error"]).replace("\n", " | ")
    if name in ("smoke_two_type", "smoke_one_type"):
        return f"types {joinlist(rec.get('types'))} ids {joinlist(rec.get('ids'))}"
    if name == "two_type_search":
        return f"{len((rec.get('detail') or {}).get('types', []))} vertex type(s) in one vectorSearch"
    if name == "vector_upsert":
        return f"{rec.get('n')} toy vectors upserted"
    if name == "tab_load":
        return joinlist([f"{k}={v!r}" for k, v in (rec.get("payload_roundtrip") or {}).items()], sep="  ", max_items=1, empty="-")
    if name == "post_150mb":
        return f"{rec.get('bytes', 0) / 1e6:,.0f} MB accepted in {rec.get('seconds')}s"
    if not rec.get("ok"):
        return (_tail(rec.get("reply_tail", ""), 1) or [""])[0]
    return ""


def checklist(records: dict) -> tuple[Table, int, int]:
    """The smoke run as a checklist: one row per check, PASS / FAIL / INFO, seconds, note."""
    t = Table(
        Col("#", align="right", width=3),
        Col("check", max_width=24),
        Col("result", width=6, align="center"),
        Col("sec", align="right", width=6),
        Col("note", max_width=76),
        title=f"day-0 smoke checklist ({len(records)} checks)",
    )
    passed = 0
    for i, (name, rec) in enumerate(records.items(), 1):
        good = bool(rec.get("ok")) if isinstance(rec, dict) else bool(rec)
        info = name in INFORMATIONAL
        passed += int(good)
        t.add_row(i, name, "PASS" if good else ("INFO" if info else "FAIL"),
                  f"{rec['seconds']:.1f}" if isinstance(rec, dict) and rec.get("seconds") is not None else "-",
                  _note(name, rec),
                  style=None if good else ("yellow" if info else "red"))
    return t, passed, len(records)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--drop-all", action="store_true")
    ap.add_argument("--post-test-mb", type=int, default=0)
    ap.add_argument("--report", type=Path, default=None)
    args = ap.parse_args(argv)
    # graph/tg.py echoes GSQL replies at INFO; WARNING keeps the resume retries and hard errors on stderr.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    header(
        "graph.day0_smoke",
        "runs graph/day0_smoke.gsql block by block on a fresh workspace and fills in the day-0 records",
        {"graph": GRAPH, "gsql": SMOKE_GSQL.name, "only": joinlist(args.only, empty="every block"),
         "post test": f"{args.post_test_mb} MB" if args.post_test_mb else "off",
         "drop all at end": "yes" if args.drop_all else "no", "report": args.report or "-"},
    )
    records: dict = {}
    conn = connect(graphname=GRAPH, timeout_ms=600_000, token=False)   # DDL first: Basic secret auth, no graph yet
    steps = blocks(SMOKE_GSQL.read_text())
    for name, text in steps:
        if name == "drop_all" or (args.only and name not in args.only):
            continue
        send(conn, name, text, records)
        if name == "ddl_all_names" and records[name]["ok"]:
            ensure_awake()(conn.getToken)(env("TG_SECRET"))            # REST++ (upserts, loads, counts) needs a token
        if name == "two_type_vector_query" and records[name]["ok"]:
            vector_steps(conn, records)
        if name == "loading_jobs" and records[name]["ok"]:
            tab_load_step(conn, records)
            if args.post_test_mb:
                post_test(conn, args.post_test_mb, records)
    if args.drop_all:
        send(conn, "drop_all", dict(steps)["drop_all"], records)

    checks, passed, total = checklist(records)
    checks.print()

    day0 = {
        "ddl_all_names": records.get("ddl_all_names", {}).get("ok"),
        "proxy_reserved": (not records["probe_reserved_proxy"]["ok"]) if "probe_reserved_proxy" in records else None,
        "vectors": records.get("vectors_global_job", {}).get("ok"),
        "two_type_search": records.get("two_type_search", {}).get("ok"),
        "one_type_search": records.get("smoke_one_type", {}).get("ok"),
        "tab_load": records.get("tab_load", {}).get("ok"),
        "gdbms_algo": records.get("gdbms_algo_import", {}).get("ok") and records.get("gdbms_algo_call", {}).get("ok"),
        "post_150mb": records.get("post_150mb"),
    }
    recorded = Table(
        Col("day-0 record", max_width=18),
        Col("value", width=9, align="center"),
        Col("means", max_width=74),
        title="day-0 workspace checks",
    )
    for k, v in day0.items():
        if isinstance(v, dict):
            shown, style = f"{v.get('bytes', 0) / 1e6:,.0f} MB", None if v.get("ok") else "red"
        elif v is None:
            shown, style = "not run", "yellow"
        else:
            shown, style = ("yes" if v else "no"), (None if v else "red")
        recorded.add_row(k, shown, RECORD_MEANS.get(k, ""), style=style)
    recorded.print()

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({"summary": day0, "records": records}, indent=1, default=str))
    hard = ["ddl_all_names", "vectors", "tab_load"]
    rc = 0 if all(day0.get(k) for k in hard if k in records or k == "tab_load") else 1
    if rc:
        fail(f"hard checks failed: {joinlist([k for k in hard if not day0.get(k)])}")
    else:
        ok(f"hard checks passed: {joinlist(hard)}")
    summary(
        "day-0 smoke complete" if rc == 0 else "day-0 smoke failed",
        {"checks": f"{passed}/{total} passed", "hard checks": joinlist(hard),
         "informational": joinlist(sorted(INFORMATIONAL & set(records)), empty="-"),
         "report": args.report or "-"},
        status="ok" if rc == 0 else "fail",
    )
    return rc


if __name__ == "__main__":
    sys.exit(main())
