"""LLM-free engine run over a case pack — the day-6 fallback and the expectation-sheet generator.

For every case (in opened_at order):
  facts     = DuckFacts.collect(ctx)                       (mock backend; live: the same query names over MCP)
  sc_pre    = scorecard.compute(ctx, facts, [])
  initial   = policy.admissible(initial) → reasons
  request   = none if §6 test 1 already holds, else voi.should_ask → simulator.reply → scorecard.post_evidence
  final     = policy.admissible(final) → reasons
  status / stop_reason / sar / summary → answer_writer.build → validator.validate → out/<case_id>.draft.json

Run:  python -m engine.run_cases                 # 20 exam cases → engine/out/*.draft.json + qa/engine_expectation_sheet.md
      python -m engine.run_cases --cases HHG-014 HHG-006
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

from engine import answer_writer, config, policy, scorecard, simulator, stop, validator, voi
from engine import status as status_rule
from engine.facts_duckdb import DuckFacts
from engine.types import CaseContext
from ops.console import Col, Table, detail, fail, header, is_plain, joinlist, money, ok, prob, progress, rule, step, summary


def contexts(fx: DuckFacts, only: list[str] | None = None) -> list[CaseContext]:
    cp = fx.q("SELECT * FROM cp ORDER BY opened_at, case_id")
    out = []
    for r in cp.itertuples():
        if only and r.case_id not in only:
            continue
        rs = None if r.risk_score != r.risk_score else float(r.risk_score)
        out.append(CaseContext(r.case_id, r.trigger_type, r.trigger_text, str(r.flagged_txn_id), r.card_id, r.customer_id,
                               r.opened_at.strftime("%Y-%m-%d %H:%M:%S"), rs))
    return out


def run_one(ctx: CaseContext, fx: DuckFacts, facts: dict | None = None) -> dict:
    """Deterministic decision for one case. Returns the answer dict plus engine internals for the sheet."""
    t0 = time.time()
    facts = facts or fx.collect(ctx)
    n_calls = len(facts)
    sc_pre = scorecard.compute(ctx, facts, [])
    settled = stop.settled_pre(sc_pre)
    request = None
    sc_post = sc_pre
    branches: dict = {}
    asked = False
    voi_zero = False
    if not settled:
        rt = voi.request_type_for(ctx, sc_pre)
        ask, branches = voi.should_ask(ctx, sc_pre, rt)
        # D1: a fraud-band case that is not settled (§6 test 1) still makes its one §3b verification request even when the
        # value-of-information test finds no branch that changes the action set; a settled case never asks (handled above).
        if not ask and sc_pre.verdict == "fraud" and ctx.trigger_type != "analyst_request":
            ask, voi_zero = True, True
        if ask:
            # the request is part of the initial recommendation: 3a opens the case, the verification action matches the request type
            sc_pre.flags["asked"] = True
            sc_pre.flags["request_type"] = rt
            rep = simulator.reply(rt, ctx, sc_pre)
            sc_post = scorecard.post_evidence(sc_pre, rep["outcome"])
            sc_post.flags["asked"] = True
            sc_post.flags["request_seq"] = 1
            request = {"type": rt, "asked_after_step": n_calls, "assumed_response": rep["assumed_response"],
                       "outcome": rep["outcome"], "counterfactual": rep["counterfactual"]}
            asked = True
        else:
            voi_zero = True
    adm_i = policy.admissible(ctx, sc_pre, "initial")
    initial = answer_writer.reasons_for(adm_i["required"], adm_i, ctx, sc_pre, "initial", request["type"] if request else "")
    if asked:
        adm_f = policy.admissible(ctx, sc_post, "final")
        final = answer_writer.reasons_for(adm_f["required"], adm_f, ctx, sc_post, "final", request["type"])
    else:
        adm_f, final = adm_i, list(initial)
    viol_i = policy.check(initial, ctx, sc_pre, "initial")
    viol_f = policy.check(final, ctx, sc_post, "final")
    pending = bool(sc_post.flags.get("pending"))
    st = status_rule.derive(sc_post.verdict, final, pending)
    sc_post.flags["escalated"] = st == "escalated"
    reason = stop.stop_reason(sc_pre, sc_post if asked else None, asked, voi_zero)
    graph_case_id = f"AC-{ctx.case_id}"
    sar = answer_writer.draft_sar(ctx, sc_post, facts, graph_case_id)
    requests = [request] if request else []
    summary = answer_writer.draft_summary(ctx, sc_pre, sc_post, initial, final, request, st)
    # evidence for the file: the engine ledger minus neutral notes beyond the first few, plus the assumed reply
    evid = [e for e in sc_post.evidence if e.direction != "neutral"] + [e for e in sc_post.evidence if e.direction == "neutral"][:3]
    closing = {"summary": summary, "stop_reason": reason, "status": st, "similar_prior_cases_used": sc_post.similar_prior_cases,
               "written_to_graph": False,
               "what_changed": answer_writer.what_changed_text(initial, final, request, sc_pre, sc_post)}
    runlog = {"tool_calls": n_calls, "tokens": 0, "latency_s": time.time() - t0}
    ans = answer_writer.build(ctx, sc_pre, sc_post, initial, final, evid, requests, sar, closing, runlog, graph_case_id)
    meta = {"p_pre": sc_pre.p_engine, "families_fraud_pre": len(sc_pre.families_fraud), "opened_at": ctx.opened_at, "card_id": ctx.card_id,
            "customer_id": ctx.customer_id}
    errors = validator.validate(ans, fx, meta)
    return {"answer": ans, "sc_pre": sc_pre, "sc_post": sc_post, "request": request, "branches": branches, "initial": initial, "final": final,
            "violations": viol_i + viol_f, "errors": errors, "settled": settled, "voi_zero": voi_zero, "adm_initial": adm_i, "adm_final": adm_f}


def sheet_row(ctx: CaseContext, r: dict) -> dict:
    a, b = r["sc_pre"], r["sc_post"]
    adj = "; ".join(f"{k} {v:+.2f}" if v else k for k, v in a.adjustments)
    return {"case": ctx.case_id, "trigger": ctx.trigger_type, "cms": a.cms_p, "cal_p": a.cal_p, "adjustments": adj, "p_pre": a.p_engine,
            "F_pre": "/".join(sorted(a.families_fraud)) or "-", "L_pre": "/".join(sorted(a.families_legit)) or "-", "verdict_pre": a.verdict,
            "pattern": b.pattern, "episode": ",".join(b.episode_ids), "exposure": b.exposure_usd, "connected": len(b.connected_card_ids),
            "initial": ", ".join(f"{x['action']}({x['route']})" for x in r["initial"]),
            "request": f"{r['request']['type']} → {r['request']['outcome']}" if r["request"] else ("none (settled)" if r["settled"] else "none (VOI 0)"),
            "p_post": b.p_engine, "verdict_post": b.verdict, "final": ", ".join(f"{x['action']}({x['route']})" for x in r["final"]),
            "status": r["answer"]["case"]["status"], "sar": r["answer"]["sar"]["file"]}


def write_sheet(rows: list[dict], path: Path) -> None:
    cols = ["case", "trigger", "cms", "cal_p", "adjustments", "p_pre", "F_pre", "L_pre", "verdict_pre", "pattern", "episode", "exposure", "connected",
            "initial", "request", "p_post", "verdict_post", "final", "status", "sar"]
    lines = ["# Engine expectation sheet (regenerated from the deterministic engine, `python -m engine.run_cases`, on the ETL facts DB "
             "`engine.facts_from_etl` — the sheet of record, decision D7)", "",
             "Cases in `opened_at` order. `F_pre` / `L_pre` = evidence families for fraud / legitimacy before any request; "
             "`p_pre` = calibrated engine probability after adjustments and floors; `request` = the simulated evidence request and its assumed outcome.", "",
             "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        lines.append("| " + " | ".join(str(r[c]).replace("|", "\\|") for c in cols) + " |")
    path.write_text("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- terminal table
# The sheet above is the artefact; everything below is display only. Nothing here may reach a
# file: `write_sheet` keeps its own plain str()/f-string formatting so the sheet stays byte-identical.

# One entry per column: key into the cell dict, its Col, and a DROP RANK. The table is laid out at
# full width and, while it is wider than the terminal, the highest-ranked column still present is
# dropped (ties broken by width). Rank 0 = never dropped — case id, the final probability/verdict,
# the pattern, the status and the SAR flag are the answer and always survive.
_PLAN: list[tuple[str, Col, int]] = [
    ("flag", Col("ok", width=3, align="center"), 0),
    ("case", Col("case", width=7), 0),
    ("trigger", Col("trigger", max_width=15), 42),
    ("cms", Col("cms", align="right", max_width=5), 50),
    ("p_pre", Col("p_pre", align="right", max_width=5), 40),
    ("pre", Col("pre", max_width=10), 45),
    ("fraud evid.", Col("fraud evid.", max_width=16), 20),
    ("legit evid.", Col("legit evid.", max_width=17), 21),
    ("request/outcome", Col("request/outcome", max_width=28), 10),
    ("p_post", Col("p_post", align="right", max_width=6), 0),
    ("post", Col("post", max_width=10), 0),
    ("pattern", Col("pattern", max_width=27), 0),
    ("exposure", Col("exposure", align="right", max_width=12), 5),
    ("status", Col("status", max_width=17), 0),
    ("sar", Col("sar", width=3, align="center"), 0),
]
# Whole-row tint by the post-request verdict (TTY only; ignored when piped).
_ROW_STYLE = {"fraud": "red", "uncertain": "yellow", "legitimate": "green"}


def term_budget() -> int:
    """Columns available for the table. A pipe/file/CI capture is NEVER narrowed (10_000), so a
    redirected run keeps every column; only a real terminal makes us drop any."""
    env = os.environ.get("COLUMNS", "")
    if env.isdigit() and int(env) > 0:
        return int(env)
    try:
        if sys.stdout.isatty():
            return shutil.get_terminal_size(fallback=(200, 24)).columns
    except Exception:
        pass
    return 10_000


def _col_width(c: Col, cells: list[str]) -> int:
    """The width ops.console.Table will give this column — mirrors Table._layout exactly."""
    if c.width is not None:
        return max(c.width, 1)
    widest = max([0, *(len(x) for x in cells)])
    if c.max_width is not None:
        widest = min(widest, c.max_width)
    return max(len(c.header), widest, 1)


def plan_columns(cells: list[dict[str, str]], budget: int) -> tuple[list[Col], list[str], list[str]]:
    """Choose the widest column set that fits `budget`. Returns (columns, kept keys, dropped keys)."""
    cols = {k: c for k, c, _ in _PLAN}
    rank = {k: r for k, _, r in _PLAN}
    width = {k: _col_width(c, [r[k] for r in cells]) for k, c, _ in _PLAN}
    # Inter-column cost: 2 spaces in plain mode; rich's box adds one separator cell per gap on top
    # of the same 2-space padding, so a rich table is (ncols - 1) wider than the plain one.
    gap = 2 if is_plain() else 3
    kept = [k for k, _, _ in _PLAN]
    dropped: list[str] = []
    while sum(width[k] for k in kept) + gap * max(len(kept) - 1, 0) > budget:
        droppable = [k for k in kept if rank[k]]
        if not droppable:
            break
        worst = max(droppable, key=lambda k: (rank[k], width[k]))
        kept.remove(worst)
        dropped.append(worst)
    # Add-back pass: dropping by rank alone overshoots (a 28-wide column sacrificed to save 3),
    # so re-admit anything that still fits, most-wanted first. Without this a 120-column terminal
    # loses the whole evidence block to buy headroom it never uses.
    order = {k: i for i, (k, _, _) in enumerate(_PLAN)}
    for k in sorted(dropped, key=lambda k: rank[k]):
        if sum(width[j] for j in kept) + width[k] + gap * len(kept) <= budget:
            kept.append(k)
            dropped.remove(k)
    kept.sort(key=order.__getitem__)
    return [cols[k] for k in kept], kept, dropped


def table_cells(ctx: CaseContext, r: dict) -> dict[str, str]:
    """One case as display strings, keyed by column. Display only — never written to a file."""
    a, b = r["sc_pre"], r["sc_post"]
    return {"flag": "ERR" if (r["errors"] or r["violations"]) else "OK",
            "case": ctx.case_id,
            "trigger": ctx.trigger_type,
            "cms": prob(a.cms_p, digits=3),
            "p_pre": prob(a.p_engine),
            "pre": a.verdict,
            "fraud evid.": joinlist(a.families_fraud, sort=True, max_items=2),
            "legit evid.": joinlist(a.families_legit, sort=True, max_items=2),
            "request/outcome": f"{r['request']['type']}/{r['request']['outcome']}" if r["request"] else "-",
            "p_post": prob(b.p_engine),
            "post": b.verdict,
            "pattern": b.pattern or "-",
            "exposure": money(b.exposure_usd),
            "status": r["answer"]["case"]["status"],
            "sar": "Y" if r["answer"]["sar"]["file"] else "-"}


def print_table(cells: list[dict[str, str]], verdicts: list[str], bad: list[bool], title: str) -> None:
    columns, kept, dropped = plan_columns(cells, term_budget())
    caption = "row tint on a TTY: red = fraud, yellow = uncertain, green = legitimate (post-request verdict); ok=ERR marks a validator or policy finding, detailed under the table"
    if dropped:
        caption += f"\nnarrowed to fit this terminal — hidden: {joinlist(dropped, sep=', ')} (pipe the command or widen the window for the full table)"
    t = Table(*columns, title=title, caption=caption)
    for row, verdict, is_bad in zip(cells, verdicts, bad):
        t.add_row(*[row[k] for k in kept], style="bold red" if is_bad else _ROW_STYLE.get(verdict))
    t.print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", nargs="*")
    ap.add_argument("--out", default=str(config.OUT_DIR))
    ap.add_argument("--sheet", default=str(Path(config.HERE).parent / "qa" / "engine_expectation_sheet.md"),
                    help="where the expectation sheet is written (default: qa/engine_expectation_sheet.md)")
    args = ap.parse_args()
    config.ensure_dirs()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    header("engine.run_cases", "deterministic drafts + expectation sheet (no LLM)",
           {"facts db": config.FACTS_DB, "backend": "duckdb (mock facts layer)", "policy": config.POLICY_YAML,
            "cases": joinlist(args.cases, empty="all (opened_at order)", sep=","), "drafts": out, "sheet": args.sheet})
    step(f"loading the facts DB: {config.FACTS_DB}")
    fx = DuckFacts()
    ctxs = contexts(fx, args.cases)
    ok(f"{len(ctxs)} cases to run")

    rows, cells, verdicts, bad, problems = [], [], [], [], []
    n_err = n_viol = n_sar = 0
    exposure = 0.0
    with progress("scoring cases", total=len(ctxs)) as p:
        for ctx in ctxs:
            r = run_one(ctx, fx)
            (out / f"{ctx.case_id}.draft.json").write_text(json.dumps(r["answer"], indent=2))
            (out / f"{ctx.case_id}.engine.json").write_text(json.dumps({"scorecard_pre": r["sc_pre"].to_dict(), "scorecard_post": r["sc_post"].to_dict(),
                                                                        "branches": r["branches"], "adm_initial": {k: v for k, v in r["adm_initial"].items() if k != "conditions"},
                                                                        "violations": r["violations"], "errors": r["errors"]}, indent=1, default=str))
            rows.append(sheet_row(ctx, r))
            cells.append(table_cells(ctx, r))
            verdicts.append(r["sc_post"].verdict)
            is_bad = bool(r["errors"] or r["violations"])
            bad.append(is_bad)
            n_err += bool(r["errors"])
            n_viol += len(r["violations"])
            n_sar += bool(r["answer"]["sar"]["file"])
            exposure += float(r["sc_post"].exposure_usd or 0.0)
            if is_bad:
                problems.append((ctx.case_id, r))
            p.advance()

    print_table(cells, verdicts, bad, f"{len(rows)} cases, opened_at order")

    if problems:
        rule("validator / policy findings")
        for case_id, r in problems:
            fail(f"{case_id}: {len(r['errors'])} validator error(s), {len(r['violations'])} policy violation(s)")
            for e in r["errors"]:
                detail(f"validator: {e}")
            for v in r["violations"]:
                detail(f"policy: {v}")
    else:
        ok("every case passed the validator and the policy check")

    write_sheet(rows, Path(args.sheet))
    ok(f"drafts -> {out} ({len(rows)} .draft.json + {len(rows)} .engine.json)")
    ok(f"sheet -> {args.sheet} ({len(rows)} rows)")
    mix = Counter(verdicts)
    status_mix = Counter(c["status"] for c in cells)
    summary("engine run complete",
            {"cases": len(rows),
             "validator errors": n_err,
             "policy violations": n_viol,
             "verdict mix": joinlist([f"{k} {mix[k]}" for k in ("fraud", "uncertain", "legitimate") if mix[k]], sep=" / ", empty="-"),
             "status mix": joinlist([f"{k} {v}" for k, v in sorted(status_mix.items())], sep=" / ", empty="-"),
             "SARs filed": n_sar,
             "total exposure": money(exposure),
             "sheet": args.sheet},
            status="fail" if n_err else ("warn" if n_viol else "ok"))


if __name__ == "__main__":
    main()
