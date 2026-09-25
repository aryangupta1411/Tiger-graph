"""Answer-file validator (contracts §C `agent/validator.py`).

A thin wrapper over `engine.validator.validate(answer, db, meta)` — the single set of PLAN §4.7 / §4.9
invariants (decision D10) — that

  * builds `meta` from the case pack (`p_pre`, `families_fraud_pre`, `opened_at`, `card_id`,
    `customer_id`) so the R1 lint, the time-box check and the SAR subject check can run, and
  * wraps a plain DuckDB connection in the `.q(sql, *params) -> DataFrame` object the engine validator
    expects, so the CLI works against the small `data/ids.duckdb` (`ops/export_id_tables.py`) instead of
    the 590 k-row facts DB. That file carries the views the validator queries:
    `txc(TransactionID, id, card_id, customer_id, ts, amt, p_email, r_email, addr1)`, `card_feat(id)`,
    `customer_feat(id)`, `cc(case_id, outcome, pattern)` and the table `device_profile(id)`.

Library use (the phase machine resolves this module before `engine.validator`):

    from agent.validator import validate
    problems = validate(answer, db)            # db = DuckFacts or any .q(sql, *params)

CLI (Makefile `validate`, CI "Validate answer files"):

    uv run python -m agent.validator --db data/ids.duckdb cases/*.json
    uv run python -m agent.validator --db data/ids.duckdb runs/r1/*/answer.json

prints a file -> status table (case id, verdict, probability, SAR, problem count), the problems of each
failing file underneath it, and a pass/fail summary; exit status 1 when any file has a problem
(0 otherwise), so it can gate a build. Output goes through `ops.console`, so a pipe or CI gets plain
ASCII with no escape codes.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
from pathlib import Path
from typing import Any

from engine import validator as engine_validator
from ops.console import Col, Table, detail, fail, header, ok, prob, summary

REPO_ROOT = Path(os.environ.get("HHGOA_REPO_ROOT", Path(__file__).resolve().parents[1]))


class _Q:
    """`.q(sql, *params)` over a DuckDB connection — the shape `engine.validator.validate` needs."""

    def __init__(self, con: Any) -> None:
        self.con = con

    def q(self, sql: str, *params: Any):
        return self.con.execute(sql, list(params)).fetchdf()


def open_db(path: str | Path) -> _Q:
    """Read-only DuckDB at `path` wrapped for the engine validator."""
    import duckdb

    return _Q(duckdb.connect(str(path), read_only=True))


# ----------------------------------------------------------------------------- case-pack meta

def _case_pack_rows(path: str | Path | None = None) -> dict[str, dict]:
    p = Path(path or os.environ.get("CASE_PACK_CSV", REPO_ROOT / "data" / "raw" / "case_pack.csv"))
    if not p.exists():
        return {}
    with p.open(encoding="utf-8", newline="") as fh:
        return {r["case_id"]: r for r in csv.DictReader(fh)}


def meta_for(answer: dict, case_pack: dict[str, dict] | None = None) -> dict:
    """`meta` for `engine.validator.validate` from the case pack plus the answer's own numbers.

    `p_pre` / `families_fraud_pre` are the pre-evidence values the R1 lint needs: when the case made no
    evidence request the answer's own probability and families are the pre-evidence ones; when it did,
    `engine.run_cases` writes `out/<case>.engine.json` next to the draft — the CLI does not read it, so
    the R1 lint is only applied when the answer carries no request (a lint, never a false positive).
    """
    case = answer.get("case", {}) or {}
    rows = case_pack if case_pack is not None else _case_pack_rows()
    cp = rows.get(str(answer.get("case_id", "")), {})
    meta = {"opened_at": cp.get("opened_at", ""), "card_id": cp.get("card_id", ""), "customer_id": cp.get("customer_id", "")}
    if not answer.get("evidence_requests"):
        meta["p_pre"] = float(case.get("fraud_probability", 0) or 0)
        ev = case.get("evidence", []) or []
        meta["families_fraud_pre"] = len({e.get("source") for e in ev if e.get("source") in ("graph", "document", "external")})
    return {k: v for k, v in meta.items() if v not in ("", None)}


# ----------------------------------------------------------------------------- API

def validate(answer: dict, db: Any, meta: dict | None = None) -> list[str]:
    """`engine.validator.validate` with `meta` filled in from the case pack when the caller has none, plus the
    agent-side checks the engine validator does not make:

      * the SAR fact checks V14-V19 (`rag.validate_sar.check_answer_sar_facts`: a pending block written as done,
        gendered pronouns, time-zone claims, named ids missing from subjects, a "no prior report" claim the
        evidence contradicts);
      * `answer_text_problems`: every action reason cites a Fraud Policy rule, the summary is short, and
        `what_changed` never reports a move "from X to X".
    """
    meta = meta if meta is not None else meta_for(answer)
    problems = list(engine_validator.validate(answer, db, meta))
    have = set(problems)
    from rag.validate_sar import check_answer_sar_facts

    problems += [f"sar: {p}" for p in check_answer_sar_facts(answer, str(meta.get("card_id", "") or "")) if f"sar: {p}" not in have]
    problems += answer_text_problems(answer)
    return problems


# ----------------------------------------------------------------------------- agent-side text checks

RULE_REF = re.compile(r"\b(?:R(?:[1-9]|10)|3a|3b)\b|§\s?[1-7]\b")
HOUSE_RULE_NAMES = ("fraud_band", "uncertain_initial", "uncertain_final", "legit_band", "legit_leaning_initial",
                    "3a_case", "3a_report_only")
SUMMARY_MAX_CHARS = 700
SUMMARY_SENTENCES = (2, 6)
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'$])")
_SAME_MOVE = re.compile(r"\bfrom (\d+(?:\.\d+)?) to (\d+(?:\.\d+)?)\b")


def text_sentences(text: str) -> list[str]:
    return [s for s in _SENT.split(" ".join(str(text).split())) if s.strip()]


def reason_problems(actions: list[dict], where: str = "") -> list[str]:
    """Every action reason names a Fraud Policy rule (R1-R10, 3a, 3b, §1-§7) and none cites an internal
    policy-gate label (`fraud_band`, `uncertain_initial`, ...) in its place."""
    out = []
    for a in actions or []:
        r = str(a.get("reason", "") or "")
        house = [h for h in HOUSE_RULE_NAMES if re.search(rf"(?<![\w-]){re.escape(h)}(?![\w-])", r)]
        if house:
            out.append(f"{where}{a.get('action')} reason cites the internal label {house[0]!r}; cite the Fraud Policy rule instead")
        elif not RULE_REF.search(r):
            out.append(f"{where}{a.get('action')} reason names no policy rule (R1-R10, 3a, 3b, §5, §6)")
    return out


def summary_problems(summary: str) -> list[str]:
    """README: 'Two to six sentences an analyst could read' and 'Keep summary short'."""
    s = " ".join(str(summary or "").split())
    n = len(text_sentences(s))
    out = []
    if not SUMMARY_SENTENCES[0] <= n <= SUMMARY_SENTENCES[1]:
        out.append(f"summary has {n} sentences; must be {SUMMARY_SENTENCES[0]}-{SUMMARY_SENTENCES[1]}")
    if len(s) > SUMMARY_MAX_CHARS:
        out.append(f"summary is {len(s)} characters; keep it under {SUMMARY_MAX_CHARS} (the evidence list carries the detail)")
    return out


def what_changed_problems(text: str) -> list[str]:
    for m in _SAME_MOVE.finditer(str(text or "")):
        if float(m.group(1)) == float(m.group(2)):
            return [f"what_changed reports a move {m.group(0)!r} although nothing moved"]
    return []


def answer_text_problems(answer: dict) -> list[str]:
    nba = answer.get("next_best_actions", {}) or {}
    case = answer.get("case", {}) or {}
    return (reason_problems(nba.get("initial", []), "initial: ") + reason_problems(nba.get("final", []), "final: ")
            + summary_problems(case.get("summary", "")) + what_changed_problems(nba.get("what_changed", "")))


# ----------------------------------------------------------------------------- CLI

def _expand(files: list[str]) -> list[Path]:
    out: list[Path] = []
    for f in files:
        hits = [Path(h) for h in sorted(glob.glob(f))] or [Path(f)]
        out += hits
    return out


def _display_path(p: Path, width: int = 56) -> str:
    """The shortest readable form of `p` for the table: relative when it can be, tail-truncated
    when it cannot (a path is identified by its end, so the head is what gets dropped)."""
    s = str(p)
    for base in (Path.cwd(), REPO_ROOT):
        try:
            s = min(s, str(p.relative_to(base)), key=len)
        except ValueError:
            pass
    return s if len(s) <= width else "..." + s[-(width - 3):]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate HHGOA answer files against the PLAN §4.7 / §4.9 invariants.")
    ap.add_argument("--db", default=os.environ.get("VALIDATOR_DB", str(REPO_ROOT / "data" / "ids.duckdb")),
                    help="DuckDB with the id tables/views (data/ids.duckdb from ops.export_id_tables)")
    ap.add_argument("--case-pack", default=None, help="case_pack.csv for the meta (default data/raw/case_pack.csv)")
    ap.add_argument("files", nargs="+", help="answer files (cases/*.json, runs/<run>/*/answer.json)")
    args = ap.parse_args(argv)

    files = _expand(args.files)
    db_path = Path(args.db)
    header(
        "agent.validator",
        "answer files vs the PLAN §4.7 / §4.9 invariants (engine.validator)",
        {"db": db_path, "case pack": args.case_pack or os.environ.get("CASE_PACK_CSV", REPO_ROOT / "data" / "raw" / "case_pack.csv"),
         "files": len(files)},
    )
    if not db_path.exists():
        fail(f"validator db not found: {db_path}")
        detail("run `python -m ops.export_id_tables` (or `make ids`) to build it")
        summary("validation aborted", {"db": db_path, "files": len(files)}, status="fail")
        return 1
    db = open_db(db_path)
    pack = _case_pack_rows(args.case_pack)
    bad = 0
    failed: list[tuple[Path, list[str]]] = []
    table = Table(
        Col("file", max_width=56),
        Col("case", width=8),
        Col("verdict", max_width=10),
        Col("p", align="right", width=5),
        Col("sar", width=3, align="center"),
        Col("problems", align="right", width=8),
        Col("status", width=6),
        title=f"{len(files)} answer file(s)",
    )
    for f in files:
        try:
            answer = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            bad += 1
            failed.append((f, [f"unreadable: {e}"]))
            table.add_row(_display_path(f), "-", "-", "-", "-", 1, "FAIL", style="red")
            continue
        answer.pop("_validation", None)
        try:
            problems = validate(answer, db, meta_for(answer, pack))
        except Exception as e:                       # a malformed file must fail, never crash the build
            problems = [f"validator raised: {type(e).__name__}: {e}"]
        case = answer.get("case", {}) or {}
        row = [_display_path(f), str(answer.get("case_id", "") or "-"), str(case.get("verdict", "") or "-"),
               prob(case.get("fraud_probability")), "Y" if (answer.get("sar", {}) or {}).get("file") else "-"]
        if problems:
            bad += 1
            failed.append((f, problems))
            table.add_row(*row, len(problems), "FAIL", style="red")
        else:
            table.add_row(*row, "-", "OK")
    table.print()
    for f, problems in failed:
        fail(f"{f}: {len(problems)} problem(s)")
        for p in problems:
            detail(p)
    clean = len(files) - bad
    if not bad:
        ok(f"all {len(files)} file(s) satisfy the §4.7 / §4.9 invariants")
    summary(
        "validation failed" if bad else "validation passed",
        {"files": len(files), "clean": clean, "with problems": bad,
         "problems": sum(len(p) for _, p in failed), "db": db_path},
        status="fail" if bad else "ok",
    )
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
