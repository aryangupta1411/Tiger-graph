"""Offline integration lint (Module H): do the query contracts agree across the modules?

For every graph/queries/*.gsql: parameter names (in order) must equal
  * mcp/query_descriptions.yaml `parameters` keys and `example` keys      (typed tools + descriptions)
  * engine.facts_duckdb.DuckFacts.<query> signature                        (mock backend / oracle)
  * agent.mcp_client.BUILTIN_QUERIES[query]["parameters"] when present     (fallback tool table)
and the PRINT ... AS keys must equal the yaml `prints` list.
Also checks: every schema.md vertex attribute list == graph/schema.gsql (the only allowed rename is
proxy -> proxy_type), and every `alias.proxy` in the queries is spelled `proxy_type`.

    uv run python -m qa.check_integration          # exit 1 on any mismatch
"""
from __future__ import annotations

import glob
import inspect
import os
import re
import sys

import yaml

# the key renames mcp/normalize.py applies to raw query output before any reader sees it
PRINT_RENAMES = {"best_run": "run"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("RUN_MODE", "mock")
RENAMES = {"proxy": "proxy_type"}

from ops.console import Col, Table, fail, header, ok, rule, summary  # noqa: E402


def gsql_sig(path: str) -> tuple[str, list[str], list[str]]:
    txt = open(path).read()
    m = re.search(r"CREATE OR REPLACE QUERY (\w+)\s*\((.*?)\)\s*FOR GRAPH", txt, re.S)
    params = [p.strip().split()[-1] for p in re.split(r",(?![^<]*>)", m.group(2)) if p.strip()]
    prints: list[str] = []
    body = txt.split("{", 1)[1]
    for pm in re.finditer(r"^\s*PRINT\s+(.*?);", body, re.S | re.M):
        for part in re.split(r",(?![^\[]*\])", pm.group(1)):
            k = re.search(r"AS\s+(\w+)\s*$", part.strip())
            prints.append(k.group(1) if k else part.strip().split("[")[0].replace("@@", "").strip())
    return m.group(1), params, prints


def mark(present: bool, broken: bool) -> str:
    """Coverage cell: ``X`` a mismatch, ``ok`` agrees, ``-`` that module does not define it."""
    return "X" if broken else ("ok" if present else "-")


def main() -> int:
    errors: list[tuple[str, str, str]] = []   # (check, subject, detail)
    from agent.mcp_client import BUILTIN_QUERIES
    from engine.facts_duckdb import DuckFacts
    yaml_path = os.path.join(ROOT, "mcp", "query_descriptions.yaml")
    md_path = next((p for p in (os.path.join(ROOT, "contracts", "schema.md"), os.path.join(os.path.dirname(ROOT), "contracts", "schema.md")) if os.path.exists(p)), None)
    header(
        "qa.check_integration",
        "do the GSQL signatures, the MCP descriptions, the DuckDB oracle and the agent tool table agree?",
        {
            "queries": "graph/queries/*.gsql",
            "descriptions": os.path.relpath(yaml_path, ROOT),
            "oracle": "engine.facts_duckdb.DuckFacts",
            "tool table": "agent.mcp_client.BUILTIN_QUERIES",
            "schema.md": os.path.relpath(md_path, ROOT) if md_path else "NOT FOUND",
        },
    )
    y = yaml.safe_load(open(yaml_path))["queries"]
    t = Table(
        Col("query", max_width=30),
        Col("params", max_width=34),
        Col("prints", align="right", width=6),
        Col("yaml", width=6, align="center"),
        Col("oracle", width=6, align="center"),
        Col("agent", width=6, align="center"),
        title="query contract coverage (ok = agrees, X = mismatch, - = not defined in that module)",
    )
    for f in sorted(glob.glob(os.path.join(ROOT, "graph", "queries", "*.gsql"))):
        if f.endswith("install_all.gsql"):
            continue
        name, params, prints = gsql_sig(f)
        bad = {"yaml": False, "oracle": False, "agent": False}
        if name in y:
            yp, ye, yk = list(y[name].get("parameters", {})), list(y[name].get("example", {})), y[name].get("prints")
            if yp != params:
                errors.append(("yaml parameters", name, f"yaml {yp} != gsql {params}"))
                bad["yaml"] = True
            if ye != params:
                errors.append(("yaml example", name, f"yaml example keys {ye} != gsql {params}"))
                bad["yaml"] = True
            # mcp/normalize renames GSQL print keys that cannot carry the contract name (card_testing_check: best_run -> run)
            prints = [PRINT_RENAMES.get(k, k) for k in prints]
            if yk is not None and set(yk) != set(prints):
                errors.append(("yaml prints", name, f"yaml {yk} != gsql {sorted(set(prints))}"))
                bad["yaml"] = True
        if hasattr(DuckFacts, name):
            dp = [p for p in inspect.signature(getattr(DuckFacts, name)).parameters if p != "self"]
            if dp != params:
                errors.append(("DuckFacts", name, f"signature {dp} != gsql {params}"))
                bad["oracle"] = True
        bp = BUILTIN_QUERIES.get(name, {}).get("parameters")
        if bp and list(bp) != params:
            errors.append(("BUILTIN_QUERIES", name, f"parameters {list(bp)} != gsql {params}"))
            bad["agent"] = True
        proxy = False
        for ln, line in enumerate(open(f), 1):
            if re.search(r"\b[a-z]\.proxy\b", line):
                errors.append((".proxy", f"{os.path.basename(f)}:{ln}", "schema.gsql stores the attribute as proxy_type (decision M1)"))
                proxy = True

        t.add_row(
            name,
            ",".join(params) or "-",
            len(prints),
            mark(name in y, bad["yaml"]),
            mark(hasattr(DuckFacts, name), bad["oracle"]),
            mark(bool(bp), bad["agent"]),
            style="red" if any(bad.values()) or proxy else None,
        )
    t.print()

    # schema.md vs schema.gsql
    sys.path.insert(0, os.path.join(ROOT, "graph"))
    from check_contracts import parse_schema  # type: ignore
    vertices, _ = parse_schema(open(os.path.join(ROOT, "graph", "schema.gsql")).read())
    if md_path is None:
        # Not a warning: the vertex-attribute cross-check is one of this lint's two jobs, so a
        # missing contracts/schema.md is a failure - but a readable one, not a StopIteration.
        errors.append(("schema.md", "contracts/schema.md", "file not found - the vertex-attribute cross-check cannot run"))
    else:
        checked = 0
        for line in open(md_path).read().splitlines():
            m = re.match(r"\| `(\w+)` \| `\w+`.*?\| `(.*?)`", line)
            if m and " → " not in line:
                checked += 1
                want = ["id"] + [RENAMES.get(a, a) for a in re.findall(r"(\w+) (?:INT|STRING|FLOAT|DATETIME|BOOL)\b", m.group(2))]
                if vertices.get(m.group(1)) != want:
                    errors.append(("schema.md", m.group(1), f"attrs {vertices.get(m.group(1))} != schema.md {want}"))
        ok(f"schema.md: {checked} vertex rows compared against graph/schema.gsql ({len(vertices)} vertex types)")

    if errors:
        rule("mismatches")
        e = Table(Col("check", max_width=16), Col("subject", max_width=26), Col("detail", max_width=110), title=f"{len(errors)} mismatch(es)")
        for check, subject, det in errors:
            e.add_row(check, subject, det, style="red")
        e.print()
        fail(f"{len(errors)} mismatch(es) - the modules disagree about the query contracts")
    else:
        ok(f"{len(t)} queries: gsql, yaml, DuckFacts and BUILTIN_QUERIES all agree")
    summary(
        "integration check",
        {"queries": len(t), "yaml entries": len(y), "vertex types": len(vertices), "mismatches": len(errors)},
        status="fail" if errors else "ok",
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
