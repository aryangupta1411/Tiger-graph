"""310-case deterministic replay of labelled closed cases — PLAN §4.10 item 2.

Samples (seeded, reproducible):
  100 cleared cases replayed as risk_score triggers          (flagged = the alert transaction)
  150 confirmed_fraud cases replayed as customer_report      (flagged = first_fraud_txn_id)
   30 confirmed_fraud cases replayed as risk_score triggers where the first fraud txn had risk >= 0.5
   30 cleared cases replayed as SYNTHETIC customer disputes  (a denial takes R2's BLOCK_CARD at any verdict — README R2 and
                                                             the §3b example — so these are blocked unless R7 matches; the
                                                             guardrail is that an R7-matched dispute is never blocked)

Scorer probabilities for Jul-Oct rows are the ETL's month-wise out-of-fold cms_p (etl/cms_train.py step 3,
`txn_feat.cms_fold = oof-2016-MM`), so the flagged transaction's own label never leaks into its score; the
calibrator is data/models/isotonic.json (D7). Case-level memory (prior closed cases) is time-boxed
to opened_at by the facts layer and the replayed case itself is stripped from every case list
(strip_self: with opened_at == as_of it would otherwise be its own "prior" case); DeviceProfile.n_fraud_cases
and Card.ring_id are all-time attributes (as in the graph) — the one known optimism of this replay.

Metrics: pattern accuracy, SAR decision accuracy, episode Jaccard, verdict accuracy per trigger,
Brier / ECE of the final probability, action-set F1 vs actions_taken per trigger, R7-matched disputes
blocked in initial (target 0; synthetic disputes blocked is a diagnostic). Writes out/replay_metrics.json and out/replay_rows.csv.

Run:  python -m engine.replay           (~2 min)
"""
from __future__ import annotations

import json
import time
from collections import Counter

import numpy as np
import pandas as pd

from engine import config
from engine.facts_duckdb import DuckFacts
from engine.run_cases import run_one
from engine.types import CaseContext
from ops.console import Col, Table, detail, fail, header, ok, prob, progress, rule, step, summary, warn

SEED = 7

# PLAN §4.10: the three numbers the replay exists to report, and the guardrail counts that go with
# them. (label, metric key over all confirmed-fraud rows, key over verdict==fraud rows, target, "min"|"max")
TARGETS: list[tuple[str, str, str | None, float, str]] = [
    ("pattern accuracy", "pattern_accuracy_fraud_all", "pattern_accuracy_fraud_verdict_fraud", 0.95, "min"),
    ("SAR decision accuracy", "sar_accuracy_fraud_all", "sar_accuracy_fraud_verdict_fraud", 1.00, "min"),
    ("episode Jaccard", "episode_jaccard_fraud_all", "episode_jaccard_fraud_verdict_fraud", 0.85, "min"),
]
# The only count with a target in the module docstring: R7 ("disputed but legitimate: do not block") must hold on
# every dispute whose charge matches the customer's own recurring pattern. The former guardrail "no synthetic dispute
# blocked in initial" contradicted README R2 (a customer denial is answered with BLOCK_CARD at any verdict; R7 is the
# only exception), so it is reported as a diagnostic. Everything merely diagnostic goes in the diagnostics table, with
# no pass/fail mark, so "targets met" stays an honest count of the stated targets.
GUARDRAILS: list[tuple[str, str, float, str]] = [
    ("R7-matched disputes blocked in initial", "r7_disputes_blocked_initial", 0, "max"),
]
DIAGNOSTICS: list[tuple[str, str]] = [
    ("synthetic disputes blocked in initial (R2)", "synthetic_disputes_blocked_initial"),
    ("synthetic disputes blocked in final (R2)", "synthetic_disputes_blocked_final"),
    ("cleared cases given a false episode", "cleared_false_episodes"),
    ("cases that raised and were skipped", "n_failed"),
]


def sample_cases(fx: DuckFacts) -> list[dict]:
    cc = fx.q("""SELECT c.case_id, c.customer_id, c.card_id, c.opened_at, c.outcome, c.pattern, c.first_fraud_txn_id, c.txn_ids,
                        c.exposure_usd, c.report_filed, c.actions_taken, t.risk_score first_risk
                 FROM cc c JOIN txc t ON t.TransactionID = CASE WHEN coalesce(c.first_fraud_txn_id, '') <> '' THEN c.first_fraud_txn_id::BIGINT
                                                                ELSE string_split(c.txn_ids, '|')[1]::BIGINT END
                 ORDER BY c.case_id""")          # ORDER BY: DuckDB returns rows in a non-deterministic order, which made the seeded sample drift between runs
    rng = np.random.default_rng(SEED)
    cleared = cc[cc.outcome == "cleared"].reset_index(drop=True)
    fraud = cc[cc.outcome == "confirmed_fraud"].reset_index(drop=True)
    fraud_hi = fraud[fraud.first_risk >= 0.5].reset_index(drop=True)
    def pick(df, n):
        return df.iloc[rng.choice(len(df), size=min(n, len(df)), replace=False)]

    groups = [("cleared_as_risk_score", pick(cleared, 130).iloc[:100], "risk_score"),
              ("fraud_as_dispute", pick(fraud, 150), "customer_report"),
              ("fraud_as_risk_score", pick(fraud_hi, 30), "risk_score"),
              ("cleared_as_synthetic_dispute", pick(cleared, 130).iloc[100:130], "customer_report")]
    out = []
    for name, df, trig in groups:
        for r in df.itertuples():
            ff = r.first_fraud_txn_id if isinstance(r.first_fraud_txn_id, str) else ""
            flagged = ff if ff and ff != "nan" else r.txn_ids.split("|")[0]
            out.append({"group": name, "trigger": trig, "case_id": r.case_id, "customer_id": r.customer_id, "card_id": r.card_id,
                        "opened_at": r.opened_at.strftime("%Y-%m-%d %H:%M:%S"), "flagged": str(flagged), "risk": float(r.first_risk),
                        "outcome": r.outcome, "pattern": r.pattern, "txn_ids": [str(x) for x in r.txn_ids.split("|")],
                        "report_filed": bool(r.report_filed), "actions_taken": r.actions_taken.split("|"), "exposure": float(r.exposure_usd)})
    return out


def strip_self(facts: dict, case_id: str) -> dict:
    """A replayed closed case has opened_at == as_of, so the time-boxed memory queries (opened_at <= as_of) return the
    case itself as a "prior" case — its own label would leak into the recent-case bump, the cleared precedent and
    similar_prior_cases. Remove it from every case list before scoring."""
    for key, sub in (("prior_cases_for_customer", "closed_cases"), ("card_profile", "prior_closed_cases"), ("similar_prior_cases", "cases"),
                     ("device_neighbors", "closed_cases"), ("ring_profile", "closed_cases")):
        d = facts.get(key) or {}
        if isinstance(d.get(sub), list):
            d[sub] = [c for c in d[sub] if c.get("id") != case_id]
    return facts


def f1(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    tp = len(a & b)
    return 0.0 if tp == 0 else 2 * tp / (len(a) + len(b))


def ece(p: np.ndarray, y: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0, 1, bins + 1)
    tot = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p > lo) & (p <= hi) if lo > 0 else (p >= lo) & (p <= hi)
        if m.any():
            tot += m.mean() * abs(p[m].mean() - y[m].mean())
    return float(tot)


# --------------------------------------------------------------------------- terminal report
# Display only. `m` is written to engine/out/replay_metrics.json exactly as it was built; nothing
# below touches it, and no formatter here is ever used for a file.


def _hit(value: float | None, target: float, sense: str) -> bool:
    if value is None:
        return False
    return value >= target if sense == "min" else value <= target


def _gap(value: float | None, target: float, sense: str, digits: int) -> str:
    """How far short the metric falls, signed, so a miss cannot be read as a pass."""
    if value is None:
        return "-"
    d = (value - target) if sense == "min" else (target - value)
    return "on target" if d >= 0 else f"{d:+.{digits}f}"


def print_targets(m: dict) -> list[str]:
    """The three PLAN §4.10 targets and the guardrail counts, each against its target. Returns the misses."""
    t = Table(Col("metric", max_width=38), Col("all fraud", align="right", max_width=10),
              Col("verdict=fraud", align="right", max_width=13), Col("target", align="right", max_width=8),
              Col("vs target", align="right", max_width=10), Col("result", max_width=6),
              title="targets (PLAN §4.10)",
              caption="'all fraud' is every confirmed-fraud case replayed; 'verdict=fraud' only those the engine also called fraud. The target is judged on 'all fraud'.")
    missed = []
    for label, key_all, key_vf, target, sense in TARGETS:
        v = m.get(key_all)
        good = _hit(v, target, sense)
        missed += [] if good else [label]
        t.add_row(label, prob(v, digits=4), prob(m.get(key_vf), digits=4), f"{'>=' if sense == 'min' else '<='} {target:.2f}",
                  _gap(v, target, sense, 4), "PASS" if good else "MISS", style="green" if good else "bold red")
    for label, key, target, sense in GUARDRAILS:
        v = m.get(key)
        good = _hit(None if v is None else float(v), target, sense)
        missed += [] if good else [label]
        t.add_row(label, "-" if v is None else str(v), "-", f"{'>=' if sense == 'min' else '<='} {target:.0f}",
                  _gap(None if v is None else float(v), target, sense, 0), "PASS" if good else "MISS",
                  style="green" if good else "bold red")
    t.print()
    return missed


def print_diagnostics(m: dict) -> None:
    """Counts worth watching that PLAN §4.10 sets no target for — reported, not graded."""
    t = Table(Col("diagnostic", max_width=36), Col("count", align="right", max_width=7), title="diagnostics (no target set)")
    for label, key in DIAGNOSTICS:
        v = m.get(key)
        t.add_row(label, "-" if v is None else str(v))
    t.print()


def print_calibration(m: dict) -> None:
    t = Table(Col("probability", max_width=22), Col("Brier", align="right", max_width=8), Col("ECE", align="right", max_width=8),
              title="calibration of the engine probability (lower is better)")
    t.add_row("p_pre (before request)", prob(m.get("brier_pre"), digits=4), prob(m.get("ece_pre"), digits=4))
    t.add_row("p_post (final answer)", prob(m.get("brier_final"), digits=4), prob(m.get("ece_final"), digits=4))
    t.print()


def print_groups(m: dict) -> None:
    t = Table(Col("sample group", max_width=28), Col("n", align="right", max_width=4),
              Col("v.acc", align="right", max_width=6), Col("unc", align="right", max_width=6),
              Col("mean p", align="right", max_width=6), Col("f/u/l", align="right", max_width=12),
              Col("F1 fin", align="right", max_width=6), Col("F1 i|f", align="right", max_width=6),
              Col("blk i", align="right", max_width=5), Col("blk f", align="right", max_width=5),
              Col("R7", align="right", max_width=4), Col("ring", align="right", max_width=4), Col("burst", align="right", max_width=5),
              Col("verr", align="right", max_width=4), Col("viol", align="right", max_width=4),
              title="per sample group",
              caption="v.acc = verdict accuracy vs the case label; unc = share called uncertain; f/u/l = fraud/uncertain/legitimate verdict counts;\n"
                      "F1 fin / F1 i|f = action-set F1 (final, and initial union final) vs actions_taken; blk i / blk f = cases recommending BLOCK_CARD;\n"
                      "R7 / ring / burst = rule hits; verr / viol = validator errors and policy violations")
    for g, d in sorted(m.get("per_group", {}).items()):
        vc = d.get("verdict_counts", {})
        t.add_row(g, d.get("n"), prob(d.get("verdict_accuracy"), digits=4), prob(d.get("uncertain_share"), digits=4),
                  prob(d.get("mean_p"), digits=4), "/".join(str(vc.get(k, 0)) for k in ("fraud", "uncertain", "legitimate")),
                  prob(d.get("action_f1_final_mean"), digits=4), prob(d.get("action_f1_initial_union_final_mean"), digits=4),
                  d.get("blocked_initial"), d.get("blocked_final"), d.get("r7_fires"), d.get("ring_hits"), d.get("burst_hits"),
                  d.get("validator_errors"), d.get("policy_violations"),
                  style="red" if (d.get("validator_errors") or d.get("policy_violations")) else None)
    t.print()


def print_requests(m: dict) -> None:
    t = Table(Col("sample group", max_width=28), Col("request / outcome", max_width=30), Col("n", align="right", max_width=4),
              title="evidence requests made (§3b, one per case)")
    for g, d in sorted(m.get("per_group", {}).items()):
        for req, n in sorted(d.get("requests", {}).items(), key=lambda kv: (-kv[1], kv[0])):
            t.add_row(g, req or "(none)", n)
    t.print()


def print_confusion(title: str, crosstab: dict, row_label: str, caption: str) -> None:
    """A pandas crosstab().to_dict() is {column: {row: count}}; keys may be numpy bools, so never stringify them
    before the lookup — only on the way out."""
    cols = sorted(crosstab, key=str)
    idx: list = []
    for col in cols:
        for r in crosstab[col]:
            if r not in idx:
                idx.append(r)
    idx.sort(key=str)
    t = Table(Col(row_label, max_width=30), *[Col(str(c), align="right") for c in cols], title=title, caption=caption)
    for r in idx:
        t.add_row(str(r), *[str(crosstab[c].get(r, 0)) for c in cols])
    t.print()


def main() -> None:
    t0 = time.time()
    header("engine.replay", "310-case deterministic replay of labelled closed cases (PLAN §4.10)",
           {"facts db": config.FACTS_DB, "calibrator": config.CALIBRATION_JSON, "seed": SEED,
            "sample": "100 cleared/risk_score + 150 fraud/dispute + 30 fraud/risk_score + 30 synthetic disputes",
            "out": config.OUT_DIR})
    step("sampling labelled closed cases")
    fx = DuckFacts()
    cases = sample_cases(fx)
    st = Table(Col("sample group", max_width=28), Col("replayed as", max_width=16), Col("truth", max_width=16), Col("n", align="right", max_width=4),
               title=f"{len(cases)} cases sampled (seed {SEED}, reproducible)")
    seen = Counter((c["group"], c["trigger"], c["outcome"]) for c in cases)
    for (g, trig, outcome), n in sorted(seen.items()):
        st.add_row(g, trig, outcome, n)
    st.print()

    rows = []
    failures: list[tuple[str, str, str]] = []
    with progress("replaying cases", total=len(cases)) as tick:
        for c in cases:
            txt = (f"Real-time model scored transaction {c['flagged']} at {c['risk']:.2f}. Review and decide." if c["trigger"] == "risk_score"
                   else f"Customer {c['customer_id']} message: 'I never made this purchase. Please check my card.' Refers to {c['flagged']}.")
            ctx = CaseContext(c["case_id"], c["trigger"], txt, c["flagged"], c["card_id"], c["customer_id"], c["opened_at"],
                              c["risk"] if c["trigger"] == "risk_score" else None)
            tick.advance()
            try:
                r = run_one(ctx, fx, strip_self(fx.collect(ctx), c["case_id"]))
            except Exception as ex:   # keep going; report failures
                rows.append({**c, "error": repr(ex)[:200]})
                failures.append((c["case_id"], c["group"], repr(ex)[:160]))
                continue
            a = r["answer"]
            ini = {x["action"] for x in a["next_best_actions"]["initial"]}
            fin = {x["action"] for x in a["next_best_actions"]["final"]}
            aff = set(a["case"]["affected_txn_ids"])
            truth = set(c["txn_ids"]) if c["outcome"] == "confirmed_fraud" else set()
            rows.append({**c, "error": "", "verdict": a["case"]["verdict"], "p": a["case"]["fraud_probability"], "p_pre": r["sc_pre"].p_engine,
                         "cms": r["sc_pre"].cms_p, "engine_pattern": a["case"]["pattern"], "sar": a["sar"]["file"],
                         "jaccard": (len(aff & truth) / len(aff | truth)) if (aff | truth) else 1.0,
                         "initial": sorted(ini), "final": sorted(fin), "f1_final": f1(fin, set(c["actions_taken"])),
                         "blocked_initial": "BLOCK_CARD" in ini, "blocked_final": "BLOCK_CARD" in fin,
                         "request": r["request"]["type"] + "/" + r["request"]["outcome"] if r["request"] else "",
                         "f1_union": f1(ini | fin, set(c["actions_taken"])), "n_shared_fraud_cards": len(r["sc_pre"].flags.get("shared_fraud_cards", [])),
                         "sar_reason": a["sar"]["reason"][:60],
                         "r7": bool(r["sc_pre"].flags.get("recurring_match")), "ring": bool(r["sc_pre"].flags.get("ring_hit")),
                         "burst": bool(r["sc_pre"].flags.get("burst_hit")), "errors": "; ".join(r["errors"]), "violations": "; ".join(r["violations"])})
    for case_id, group, ex in failures:
        fail(f"{case_id} ({group}) raised during replay")
        detail(ex)
    df = pd.DataFrame(rows)
    df.to_csv(config.OUT_DIR / "replay_rows.csv", index=False)
    good = df[df.error == ""]          # `good`, not `ok`: ops.console.ok is the status-line helper
    m: dict = {"n": int(len(df)), "n_failed": int((df.error != "").sum()), "seconds": round(time.time() - t0, 1)}
    fr = good[good.outcome == "confirmed_fraud"]
    cl = good[good.outcome == "cleared"]
    m["pattern_accuracy_fraud_all"] = round(float((fr.engine_pattern == fr.pattern).mean()), 4)
    frf = fr[fr.verdict == "fraud"]
    m["pattern_accuracy_fraud_verdict_fraud"] = round(float((frf.engine_pattern == frf.pattern).mean()), 4) if len(frf) else None
    m["pattern_confusion"] = pd.crosstab(fr.pattern, fr.engine_pattern).to_dict()
    m["sar_accuracy_fraud_all"] = round(float((fr.sar == fr.report_filed).mean()), 4)
    m["sar_accuracy_fraud_verdict_fraud"] = round(float((frf.sar == frf.report_filed).mean()), 4) if len(frf) else None
    m["sar_confusion"] = pd.crosstab(fr.report_filed, fr.sar).to_dict()
    m["episode_jaccard_fraud_all"] = round(float(fr.jaccard.mean()), 4)
    m["episode_jaccard_fraud_verdict_fraud"] = round(float(frf.jaccard.mean()), 4) if len(frf) else None
    m["cleared_false_episodes"] = int((cl.jaccard < 1).sum())
    y = (good.outcome == "confirmed_fraud").astype(int).values
    p = good.p.values.astype(float)
    m["brier_final"] = round(float(np.mean((p - y) ** 2)), 4)
    m["ece_final"] = round(ece(p, y), 4)
    m["brier_pre"] = round(float(np.mean((good.p_pre.values.astype(float) - y) ** 2)), 4)
    m["ece_pre"] = round(ece(good.p_pre.values.astype(float), y), 4)
    per = {}
    for g, d in good.groupby("group"):
        want = "fraud" if d.outcome.iloc[0] == "confirmed_fraud" else "legitimate"
        per[g] = {"n": int(len(d)), "verdict_counts": d.verdict.value_counts().to_dict(), "verdict_accuracy": round(float((d.verdict == want).mean()), 4),
                  "uncertain_share": round(float((d.verdict == "uncertain").mean()), 4), "action_f1_final_mean": round(float(d.f1_final.mean()), 4),
                  "action_f1_initial_union_final_mean": round(float(d.f1_union.mean()), 4),
                  "blocked_initial": int(d.blocked_initial.sum()), "blocked_final": int(d.blocked_final.sum()),
                  "requests": d.request.value_counts().to_dict(), "r7_fires": int(d.r7.sum()), "ring_hits": int(d.ring.sum()), "burst_hits": int(d.burst.sum()),
                  "mean_p": round(float(d.p.mean()), 4), "validator_errors": int((d.errors != "").sum()), "policy_violations": int((d.violations != "").sum())}
    m["per_group"] = per
    m["synthetic_disputes_blocked_initial"] = per.get("cleared_as_synthetic_dispute", {}).get("blocked_initial")
    m["synthetic_disputes_blocked_final"] = per.get("cleared_as_synthetic_dispute", {}).get("blocked_final")
    disputes = good[good.trigger == "customer_report"]
    m["r7_disputes_blocked_initial"] = int((disputes.r7.astype(bool) & disputes.blocked_initial.astype(bool)).sum())
    json.dump(m, open(config.OUT_DIR / "replay_metrics.json", "w"), indent=1, default=str)

    # ---- report (display only; `m` above is already on disk, untouched by anything below) ----
    rule("results")
    missed = print_targets(m)
    print_diagnostics(m)
    print_calibration(m)
    print_groups(m)
    print_requests(m)
    print_confusion("pattern confusion (confirmed-fraud cases)", m["pattern_confusion"], "true pattern \\ engine pattern",
                    "rows = the closed case's labelled pattern, columns = the pattern the engine named; the diagonal is correct")
    print_confusion("SAR confusion (confirmed-fraud cases)", m["sar_confusion"], "report_filed \\ engine sar",
                    "rows = whether a SAR was actually filed, columns = whether the engine filed one")
    if m["n_failed"]:
        fail(f"{m['n_failed']} of {m['n']} cases raised and were skipped")
    for label in missed:
        warn(f"below target: {label}")
    ok(f"rows  -> {config.OUT_DIR / 'replay_rows.csv'} ({m['n']} rows)")
    ok(f"metrics -> {config.OUT_DIR / 'replay_metrics.json'}")
    summary("replay complete",
            {"cases replayed": m["n"],
             "failed": m["n_failed"],
             "targets met": f"{len(TARGETS) + len(GUARDRAILS) - len(missed)}/{len(TARGETS) + len(GUARDRAILS)}",
             "below target": ", ".join(missed) or "none",
             "wall time": f"{m['seconds']}s",
             "metrics": config.OUT_DIR / "replay_metrics.json"},
            status="fail" if m["n_failed"] else ("warn" if missed else "ok"))


if __name__ == "__main__":
    main()
