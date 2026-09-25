"""Promote a run's answer files into cases/ (contracts §D): `uv run python -m agent.promote --run-id r1`

Only files that pass the invariants (and the DuckDB validator when available) are
copied; cases/MANIFEST.md records run id, model, prompt hashes and per-case totals.

The terminal gets a table of what was copied and what was refused (with each refusal's problems on
stderr, where they have always gone) and a pass/fail summary, through `ops.console`; the MANIFEST.md
bytes and the copied files are untouched by any of that.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import sys
from collections.abc import Iterator

from agent.answer_fallback import check_invariants
from agent.config import SETTINGS
from agent.phase_machine import VALIDATOR_SOURCE, _facts_db, validate_answer
from agent.schemas import Answer
from ops.console import Col, Table, detail, fail, header, joinlist, ok, prob, summary, warn


@contextlib.contextmanager
def _on_stderr() -> Iterator[None]:
    """Console output to stderr — refusals have always gone there and still do, now formatted."""
    sys.stdout.flush()                      # keep the two streams in order when both are piped
    with contextlib.redirect_stdout(sys.stderr):
        yield
        sys.stderr.flush()


MANIFEST_COLS = ["case", "verdict", "p", "pattern", "sar", "tool_calls", "tokens", "latency_s", "notes", "run_id", "git"]


def _existing_rows(path) -> dict[str, tuple]:
    """Rows of an existing cases/MANIFEST.md table keyed by case id (older 9-column tables get run/git '')."""
    out: dict[str, tuple] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    header_cols: list[str] = []
    for ln in lines:
        if not ln.startswith("|") or ln.startswith("|---"):
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if cells and cells[0] == "case":
            header_cols = cells
            continue
        if header_cols and cells and cells[0].startswith("HHG-"):
            row = dict(zip(header_cols, cells))
            out[cells[0]] = tuple(row.get(c, "") for c in MANIFEST_COLS)
    return out


def manifest_markdown(rows: list[tuple], run_id: str, manifest: dict, existing_path=None) -> str:
    """cases/MANIFEST.md: every case file in cases/, one row each with the run and git sha it came from. Rows for
    cases not promoted by this invocation are carried over from the previous MANIFEST.md, so a partial promotion
    (`--cases HHG-014`) never drops the other nineteen."""
    existing = _existing_rows(existing_path or (SETTINGS.cases_dir / "MANIFEST.md"))
    merged = dict(existing)
    for r in rows:
        merged[r[0]] = tuple(r)
    present = {p.stem for p in SETTINGS.cases_dir.glob("HHG-*.json")}
    table = [merged[k] for k in sorted(merged) if k in present or k in {r[0] for r in rows}]
    runs = sorted({str(r[9]) for r in table if len(r) > 9 and r[9]})
    shas = sorted({str(r[10]) for r in table if len(r) > 10 and r[10]})
    lines = ["# cases/ manifest", "",
             f"- cases: {len(table)} ({', '.join(r[0] for r in table)})",
             f"- runs: {', '.join(f'`{x}`' for x in runs) or '-'}; last promoted: `{run_id}`",
             f"- model: `{manifest.get('model', '')}` (effort {manifest.get('effort', '')}), llm backend `{manifest.get('llm_backend', '')}`",
             f"- engine: `{manifest.get('engine', '')}`",
             f"- git (per case below): {', '.join(f'`{x}`' for x in shas) or '-'}",
             "- prompt sha256: " + ", ".join(f"`{k}`={v[:12]}" for k, v in (manifest.get("prompt_sha256") or {}).items()), "",
             "| " + " | ".join(MANIFEST_COLS) + " |", "|" + "---|" * len(MANIFEST_COLS)]
    lines += ["| " + " | ".join(str(x) for x in (list(r) + [""] * len(MANIFEST_COLS))[: len(MANIFEST_COLS)]) + " |" for r in table]
    return "\n".join(lines) + "\n"


def promote(run_id: str, cases: str = "all", force: bool = False) -> int:
    run_dir = SETTINGS.runs_dir / run_id
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8")) if (run_dir / "run_manifest.json").exists() else {}
    wanted = None if cases in ("all", "") else {c.strip() for c in cases.split(",")}
    header(
        "agent.promote",
        f"copy validated answer files from {run_dir} to {SETTINGS.cases_dir}",
        {"run id": run_id, "cases": cases, "model": manifest.get("model", ""), "effort": manifest.get("effort", ""),
         "engine": manifest.get("engine", ""), "git": (manifest.get("git_sha", "") or "")[:12],
         "validator": VALIDATOR_SOURCE or "none", "force": force, "out": SETTINGS.cases_dir},
    )
    if not run_dir.exists():
        with _on_stderr():
            fail(f"run not found: {run_dir}")
        summary("promotion aborted", {"run id": run_id, "run dir": run_dir}, status="fail")
        return 1
    SETTINGS.cases_dir.mkdir(parents=True, exist_ok=True)
    table = Table(
        Col("case", width=8),
        Col("result", width=12),
        Col("verdict", max_width=10),
        Col("p", align="right", width=5),
        Col("pattern", max_width=20),
        Col("sar", width=3, align="center"),
        Col("tools", align="right", width=5),
        Col("tokens", align="right", width=9),
        Col("secs", align="right", width=6),
        Col("problems", max_width=44),
        title=f"run {run_id} -> {SETTINGS.cases_dir}",
    )
    refused: list[tuple[str, list[str]]] = []
    missing: list[str] = []
    rows, bad = [], 0
    for case_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        if wanted and case_dir.name not in wanted:
            continue
        src = case_dir / "answer.json"
        if not src.exists():
            missing.append(case_dir.name)
            table.add_row(case_dir.name, "no answer", "-", "-", "-", "-", "-", "-", "-", f"no answer.json in {case_dir}", style="dim")
            continue
        answer = json.loads(src.read_text(encoding="utf-8"))
        problems = []
        try:
            Answer.model_validate(answer)
        except Exception as e:
            problems.append(f"schema: {e}")
        problems += check_invariants(answer)
        if validate_answer is not None:
            try:
                db = _facts_db()
                if db is not None:
                    problems += validate_answer(answer, db, None) if VALIDATOR_SOURCE == "engine.validator" else validate_answer(answer, db)
            except Exception as e:
                problems.append(f"validator raised: {e}")
        c = answer.get("case", {}) or {}
        cells = [c.get("verdict", "-"), prob(c.get("fraud_probability")), c.get("pattern") or "-",
                 "Y" if (answer.get("sar", {}) or {}).get("file") else "-", answer.get("tool_calls", "-"),
                 f"{int(answer.get('tokens', 0) or 0):,}", answer.get("latency_s", "-")]
        if problems and not force:
            bad += 1
            refused.append((case_dir.name, problems))
            table.add_row(case_dir.name, "REFUSED", *cells, joinlist(problems, sep="; "), style="red")
            with _on_stderr():
                fail(f"{case_dir.name} NOT promoted: {len(problems)} problem(s)")
                for p in problems:
                    detail(p)
            continue
        dst = SETTINGS.cases_dir / f"{case_dir.name}.json"
        shutil.copyfile(src, dst)
        c = answer["case"]
        prov = (manifest.get("case_runs") or {}).get(case_dir.name) or {}
        sha = str(prov.get("git_sha") or (manifest.get("git_sha", "") + " (run start)" if manifest.get("git_sha") else ""))
        if prov.get("git_dirty"):
            sha += " +uncommitted changes"
        rows.append((case_dir.name, c["verdict"], c["fraud_probability"], c["pattern"], answer["sar"]["file"],
                     answer["tool_calls"], answer["tokens"], answer["latency_s"], "; ".join(problems) if problems else "",
                     run_id, sha[:40] if " " not in sha else sha))
        table.add_row(case_dir.name, "forced" if problems else "promoted", *cells,
                      joinlist(problems, sep="; ") if problems else "-", style="yellow" if problems else None)
        if problems:
            with _on_stderr():
                warn(f"{case_dir.name} promoted with --force despite {len(problems)} problem(s)")
                for p in problems:
                    detail(p)
    (SETTINGS.cases_dir / "MANIFEST.md").write_text(manifest_markdown(rows, run_id, manifest), encoding="utf-8")
    table.print()
    if rows:
        ok(f"{len(rows)} case(s) copied to {SETTINGS.cases_dir}: {joinlist([r[0] for r in rows], max_items=10)}")
    if refused:
        fail(f"{len(refused)} case(s) refused (not copied): {joinlist([r[0] for r in refused], max_items=10)}")
        detail("the problems of each refused case are on stderr; re-run with --force to copy them anyway")
    if missing:
        warn(f"{len(missing)} case dir(s) had no answer.json: {joinlist(missing, max_items=10)}")
    ok(f"manifest -> {SETTINGS.cases_dir / 'MANIFEST.md'}")
    summary(
        "promotion failed" if bad else "promotion complete",
        {"run id": run_id, "promoted": len(rows), "refused": len(refused), "no answer.json": len(missing),
         "SARs filed": sum(1 for r in rows if r[4]), "cases dir": SETTINGS.cases_dir,
         "manifest": SETTINGS.cases_dir / "MANIFEST.md"},
        status="fail" if bad else "ok",
    )
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--cases", default="all")
    ap.add_argument("--force", action="store_true", help="promote even with validation problems (never for the frozen run)")
    a = ap.parse_args(argv)
    return promote(a.run_id, a.cases, a.force)


if __name__ == "__main__":
    raise SystemExit(main())
