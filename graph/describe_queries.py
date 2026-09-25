"""graph/describe_queries.py — registers the query and parameter descriptions from mcp/query_descriptions.yaml
through pyTigerGraph updateQueryDescription (PUT /gsql/v1/description?graph=FraudGraph), offline, so the
MCP's get_query_description / get_query_metadata show the LLM the exact JSON parameter examples.
Run after every install_all.py (CREATE OR REPLACE keeps descriptions only when the query text is unchanged).

    uv run python graph/describe_queries.py
"""

from __future__ import annotations

import json
import os
import sys

import yaml
from install_all import connect

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)                       # repo root, for ops.console
YAML = os.path.join(REPO, "mcp", "query_descriptions.yaml")

from graph.tg import ensure_awake  # noqa: E402
from ops.console import Col, Table, detail, fail, header, ok, summary  # noqa: E402


def _reply(res) -> str:
    """The server's answer as one readable line - never a raw dict repr in a table cell."""
    if isinstance(res, dict):
        msg = str(res.get("message") or res.get("results") or "").strip()
        return (f"error: {msg}" if res.get("error") else msg) or ("error" if res.get("error") else "ok")
    return str(res).strip()


def main() -> int:
    spec = yaml.safe_load(open(YAML))
    queries = spec["queries"]
    header(
        "graph.describe_queries",
        "registers query + parameter descriptions so the MCP shows the LLM real JSON examples",
        {"registry": os.path.relpath(YAML, REPO), "queries": len(queries),
         "endpoint": "PUT /gsql/v1/description (updateQueryDescription)"},
    )
    conn = connect()

    t = Table(
        Col("#", align="right", width=3),
        Col("query", max_width=28),
        Col("result", width=6, align="center"),
        Col("params", align="right", width=6),
        Col("harness", width=7, align="center"),
        Col("server reply / error", max_width=62),
        title=f"{len(queries)} query descriptions registered",
    )
    failures = []
    for i, (name, q) in enumerate(queries.items(), 1):
        desc = q["description"].strip() + "\nExample params: " + json.dumps(q["example"], separators=(",", ":"))
        params = {p: str(d).strip() for p, d in q.get("parameters", {}).items()}
        kind = "yes" if q.get("harness_only") else ("writer" if q.get("writer") else "-")
        try:
            res = ensure_awake()(conn.updateQueryDescription)(name, desc, params)   # resume-retry, then the ERR row
            t.add_row(i, name, "OK", len(params), kind, _reply(res))
        except Exception as e:  # noqa: BLE001
            failures.append((name, str(e)))
            t.add_row(i, name, "ERR", len(params), kind, str(e)[:120], style="red")
    t.print()
    for name, err in failures:
        fail(f"{name}: description not registered")
        detail(err[:300])
    if not failures:
        ok(f"all {len(queries)} descriptions registered")
    summary(
        "query descriptions registered" if not failures else "some descriptions failed",
        {"queries": len(queries), "registered": len(queries) - len(failures), "failed": len(failures),
         "registry": os.path.relpath(YAML, REPO)},
        status="ok" if not failures else "fail",
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
