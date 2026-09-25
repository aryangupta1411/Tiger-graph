"""Keep a Savanna workspace awake during a benchmark run or a demo recording.

Savanna's auto-suspend counts *running queries* as activity (release note
2024-08-27), so a trivial installed query every 3 minutes prevents the
auto-stop that would otherwise cost a 502 + resume in the middle of a case.
Controller-API calls do not count, so we run a real query.

Usage::

    uv run python -m ops.keep_alive                     # every 180 s until Ctrl-C
    uv run python -m ops.keep_alive --interval 120 --for 5400   # 90 minutes then exit
    make keepalive

The query defaults to ``prior_cases_for_customer`` (contract 2A, always
installed) on the HHG-014 customer; override with KEEP_ALIVE_QUERY and
KEEP_ALIVE_PARAMS (JSON) if module D ships a dedicated ``ping`` query.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time

from ops.console import detail, fail, header, ok, step, summary, warn
from ops.ensure_awake import run_query, tg_connection, warm_up

DEFAULT_QUERY = "prior_cases_for_customer"
DEFAULT_PARAMS = {"cu": "C13487", "as_of": "2016-12-31 23:59:59"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=float, default=float(os.getenv("KEEP_ALIVE_INTERVAL_S", "180")))
    ap.add_argument("--for", dest="duration", type=float, default=0.0, help="seconds to run (0 = until Ctrl-C)")
    ap.add_argument("--query", default=os.getenv("KEEP_ALIVE_QUERY", DEFAULT_QUERY))
    ap.add_argument("--params", default=os.getenv("KEEP_ALIVE_PARAMS", json.dumps(DEFAULT_PARAMS)))
    ap.add_argument("--once", action="store_true", help="single ping (used by `make awake`)")
    args = ap.parse_args(argv)
    # ops.ensure_awake still logs its auto-resume retries; show them at the console indent
    # so a 502/503 retry lines up under the ping it belongs to. INFO chatter stays off.
    logging.basicConfig(level=logging.WARNING, format="     %(name)s: %(message)s")

    params = json.loads(args.params)
    header(
        "ops.keep_alive",
        "one real query keeps the Savanna workspace from auto-suspending mid-benchmark"
        if not args.once
        else "wake the Savanna workspace (the first request after an auto-stop is dropped)",
        {
            "host": os.getenv("TG_HOST", "(TG_HOST unset)"),
            "graph": os.getenv("TG_GRAPHNAME", "FraudGraph"),
            "query": args.query,
            "params": json.dumps(params, sort_keys=True),
            "mode": "single ping (--once)" if args.once else f"every {args.interval:.0f}s",
            "runs for": "until Ctrl-C" if not args.duration else f"{args.duration:.0f}s",
        },
    )

    step("connecting and waiting for the workspace to answer getVer()")
    try:
        conn = tg_connection()
        ver = warm_up(conn)
    except Exception as exc:  # noqa: BLE001 - a bad .env must read as one line, not a traceback
        fail(f"cannot reach the workspace: {exc}")
        detail("check TG_HOST / TG_GRAPHNAME / TG_SECRET in .env (`make status`)")
        summary("keep-alive stopped", {"pings": 0, "reason": "connection failed"}, status="fail")
        return 1
    ok(f"workspace awake, TigerGraph {ver}")

    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    started = time.time()
    n = 0
    failures = 0
    last_error = ""
    while not stop["flag"]:
        t0 = time.time()
        clock = time.strftime("%H:%M:%S")
        try:
            res = run_query(conn, args.query, params, timeout_ms=30_000)
            n += 1
            ok(f"{clock} ping {n} {args.query} in {time.time() - t0:.2f}s ({len(res)} result objects)")
        except Exception as exc:  # noqa: BLE001 - keep the loop alive, report loudly
            failures += 1
            last_error = str(exc)
            warn(f"{clock} ping failed after {time.time() - t0:.2f}s")
            detail(last_error)
        if args.once or (args.duration and time.time() - started >= args.duration):
            break
        # sleep in 1 s slices so Ctrl-C is prompt
        for _ in range(int(args.interval)):
            if stop["flag"]:
                break
            time.sleep(1)
    summary(
        "keep-alive stopped",
        {
            "pings ok": n,
            "pings failed": failures,
            "last error": last_error or "-",
            "kept awake for": f"{time.time() - started:.0f}s",
            "query": args.query,
        },
        status="warn" if failures else "ok",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
