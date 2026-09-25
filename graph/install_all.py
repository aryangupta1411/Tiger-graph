"""graph/install_all.py — (re)creates and installs the whole GSQL query library in ONE compile.

    uv run python graph/install_all.py                # all files in graph/queries/*.gsql
    uv run python graph/install_all.py card_window ring_profile   # only these (still one INSTALL QUERY ALL)

Each .gsql file is a self-contained `USE GRAPH FraudGraph / CREATE OR REPLACE QUERY ...` batch; they are sent
one by one through pyTigerGraph conn.gsql() (GSQL server, not RESTPP: the 16 s timer does not apply, but the
launcher-style customizeHeader timeout does bound the HTTP call, so we set 600 s here), then
`INSTALL QUERY ALL` compiles everything that is created-but-not-installed (1–3 min on TG-00).
Exit code 1 if any CREATE reports an error, so the day-3/day-4 Makefile targets fail loudly.

Savanna auto-resume: getToken and every conn.gsql() go through graph.tg.ensure_awake(), so the first request
after an auto-suspend (502 / connection error / HTML start page) is retried for up to TG_RESUME_MAX_WAIT_S
instead of dying with a traceback. CREATE OR REPLACE and INSTALL QUERY ALL are safe to re-send.
"""

from __future__ import annotations

import glob
import os
import re
import sys
import time

from dotenv import load_dotenv
from pyTigerGraph import TigerGraphConnection

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)                       # repo root, for ops.console
QDIR = os.path.join(HERE, "queries")
SKIP = {"install_all.gsql"}

from graph.tg import ensure_awake, html_guard, require_secret  # noqa: E402
from ops.console import Col, Table, detail, fail, header, ok, progress, step, summary, warn  # noqa: E402


def connect() -> TigerGraphConnection:
    load_dotenv()
    secret = require_secret(os.environ.get("TG_SECRET", ""))   # empty secret: fail before waking the workspace
    conn = TigerGraphConnection(
        host=os.environ["TG_HOST"],
        graphname=os.environ.get("TG_GRAPHNAME", "FraudGraph"),
        gsqlSecret=os.environ.get("TG_SECRET", ""),
        username=os.environ.get("TG_USERNAME", ""),
        password=os.environ.get("TG_PASSWORD", ""),
        restppPort=os.environ.get("TG_RESTPP_PORT", "14240"),
        gsPort=os.environ.get("TG_GS_PORT", "14240"),
        tgCloud=os.environ.get("TG_TGCLOUD", "true").lower() == "true",
    )
    ensure_awake()(conn.getToken)(secret)
    conn.customizeHeader(timeout=600_000, responseSize=64_000_000)
    return conn


@ensure_awake()
def _gsql(conn: TigerGraphConnection, text: str) -> str:
    """conn.gsql(text) with resume-retry; an HTML start page in place of the reply is retried, not "success"."""
    return html_guard(conn.gsql(text))


def gsql_ok(text: str) -> bool:
    """False on a compile/install error. A handful of query files (vectorSearch queries; see the `INSTALL
    QUERY <name>` lines in their .gsql) INSTALL themselves inline, so a passing reply reads "...succeeded:
    N, skipped: 0, failed: 0." -- that trailing "failed: 0" would otherwise false-positive the substring
    check below, so it is scrubbed out first; "failed: 1" or higher still trips it."""
    bad = ("Syntax error", "Semantic Check Fails", "Encountered", "failed", "Error:", "error:")
    scrubbed = re.sub(r"failed:\s*0\b", "", text)
    return not any(b in scrubbed for b in bad)


def _tail(text: str, n: int) -> list[str]:
    """The last `n` non-empty lines of a GSQL reply, for `detail()` under a status line."""
    return [ln.strip() for ln in str(text).splitlines() if ln.strip()][-n:]


def main() -> int:
    names = [a for a in sys.argv[1:] if a != "--only"]  # `--only a b` (module F's runbook) == `a b`
    files = sorted(glob.glob(os.path.join(QDIR, "*.gsql")))
    files = [f for f in files if os.path.basename(f) not in SKIP and (not names or os.path.basename(f)[:-5] in names)]
    load_dotenv()   # so the banner shows the real host/graph before connect() blocks on a resuming workspace
    header(
        "graph.install_all",
        "CREATE OR REPLACE every query file, then one INSTALL QUERY ALL (1-3 min on TG-00)",
        {"host": os.environ.get("TG_HOST", "?"), "graph": os.environ.get("TG_GRAPHNAME", "FraudGraph"),
         "query dir": os.path.relpath(QDIR, REPO), "queries": len(files),
         "selection": " ".join(names) if names else "every *.gsql"},
    )
    conn = connect()

    created = Table(
        Col("#", align="right", width=3),
        Col("query", max_width=34),
        Col("create", width=6, align="center"),
        Col("sec", align="right", width=6),
        Col("note", max_width=60),
        title=f"{len(files)} query files sent to the GSQL server",
    )
    failed = []
    replies: dict[str, str] = {}
    # A live count while the GSQL server chews through the files; the per-query table follows.
    with progress("creating queries", total=len(files)) as p:
        for i, f in enumerate(files, 1):
            name = os.path.basename(f)
            t = time.time()
            res = _gsql(conn, open(f).read())
            good = gsql_ok(res)
            if not good:
                failed.append(name)
                replies[name] = res
            created.add_row(i, name, "OK" if good else "ERR", f"{time.time() - t:.1f}",
                            "" if good else (_tail(res, 1) or [""])[0], style=None if good else "red")
            p.advance()
    created.print()
    if failed:
        for name in failed:
            fail(f"{name} did not compile")
            for line in _tail(replies[name], 12):
                detail(line)
    else:
        ok(f"{len(files)} queries created")

    step("INSTALL QUERY ALL - compiles every created-but-not-installed query")
    t = time.time()
    res = _gsql(conn, "USE GRAPH FraudGraph\nINSTALL QUERY ALL")
    dt = time.time() - t
    if gsql_ok(res):
        ok(f"INSTALL QUERY ALL finished in {dt:.0f}s")
        for line in _tail(res, 3):
            detail(line)
    else:
        warn(f"INSTALL QUERY ALL returned errors after {dt:.0f}s")
        for line in _tail(res, 20):
            detail(line)

    summary(
        "query library installed" if not failed else "query library incomplete",
        {"query files": len(files), "created": len(files) - len(failed), "failed": len(failed),
         "install seconds": f"{dt:.0f}", "failures": ", ".join(failed) or "-"},
        status="ok" if not failed else "fail",
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
