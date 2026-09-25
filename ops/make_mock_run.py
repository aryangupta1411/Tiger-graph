"""Build a ``runs/<run_id>/`` tree from existing answer files so the Streamlit UI,
the approvals inbox and the replay-from-log demo fallback work with no
TigerGraph, no API keys and no agent run (RUN_MODE=mock, PLAN §5 demo fallback).

For every ``<case_id>.json`` in ``--answers`` (default ``cases/``; falls back to
``tests/fixtures/answers/*.sample.json``) it writes:

  runs/<run_id>/<case_id>/answer.json     the answer file, verbatim
  runs/<run_id>/<case_id>/phases.jsonl    P0..P10 rows synthesised from the answer (contract 2D shape)
  runs/<run_id>/<case_id>/calls.jsonl     the mandatory query calls (contract 2A names) + one LLM row per LLM phase
  runs/<run_id>/<case_id>/sar.md          the SAR narrative as markdown ("" when sar.file is false)
  runs/<run_id>/run_manifest.json         {run_id, mode: "mock", model, created_at, cases[], prompt_sha256: {}}

A real run written by agent/runlog.py has the same file names and the same
row keys, so the UI cannot tell them apart except for ``run_manifest.mode``.

Usage: uv run python -m ops.make_mock_run [--answers cases] [--run-id mock] [--runs runs]
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import sys

from ops.console import Col, Table, fail, header, joinlist, money, ok, prob, step, summary, warn

MANDATORY_QUERIES = [
    "case_context",
    "card_profile",
    "card_window",
    "prior_cases_for_customer",
    "similar_prior_cases",
    "grounding_chunks",
]
PLAYBOOK_BY_TRIGGER = {
    "risk_score": ["region_history", "device_history", "episode_candidates", "recurring_charge_check"],
    "customer_report": ["recurring_charge_check", "episode_candidates", "shared_origin_scan", "device_history"],
    "analyst_request": ["device_neighbors", "ring_profile", "shared_origin_scan"],
}
LLM_PHASES = {"P2": "investigate", "P4": "assess", "P5": "decide_initial", "P7": "decide_final", "P8": "sar", "P9": "explain"}


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def _family(ev: dict) -> str:
    ref = (ev.get("ref") or "").lower()
    src = ev.get("source")
    if src == "customer":
        return "customer"
    if src == "document":
        return "document"
    if any(k in ref for k in ("prior_cases", "similar", "closed")):
        return "memory"
    if any(k in ref for k in ("device", "ring", "shared_origin", "email_neighbors", "burst")):
        return "device"
    return "history"


def synth_phases(ans: dict, trigger_type: str, opened_at: str) -> list[dict]:
    case = ans["case"]
    nba = ans["next_best_actions"]
    reqs = ans.get("evidence_requests", [])
    p_post = float(case["fraud_probability"])
    # pre-evidence probability: read from what_changed when it names two numbers, else same as post
    p_pre = p_post
    ev = case.get("evidence", [])
    fam_f = sorted({_family(e) for e in ev if e.get("source") != "document"})
    t = dt.datetime.strptime(opened_at, "%Y-%m-%d %H:%M:%S")

    def at(s: int) -> str:
        return (t + dt.timedelta(seconds=s)).strftime("%Y-%m-%d %H:%M:%S")

    rows = [
        {"phase": "P0", "name": "intake", "at": at(0), "payload": {"case_id": ans["case_id"], "trigger_type": trigger_type, "opened_at": opened_at, "graph_case_id": case.get("graph_case_id", "")}},
        {"phase": "P1", "name": "memory", "at": at(3), "payload": {"similar_prior_cases": case.get("similar_prior_cases", [])}},
        {"phase": "P2", "name": "investigate", "at": at(12), "payload": {"n_evidence": len(ev), "families": fam_f}},
        {"phase": "P3", "name": "scorecard", "at": at(13), "payload": {"cms_p": None, "cal_p": None, "adjustments": [], "p_engine": p_pre, "families_fraud": fam_f, "families_legit": [], "flags": {}, "pattern": case["pattern"], "episode_ids": case["affected_txn_ids"], "exposure_usd": case["exposure_usd"], "verdict": case["verdict"]}},
        {"phase": "P4", "name": "assess", "at": at(20), "payload": {"verdict": case["verdict"], "fraud_probability": p_pre, "pattern": case["pattern"], "calibration_basis": "mock replay of the answer file"}},
        {"phase": "P5", "name": "nba_initial", "at": at(24), "payload": {"actions": nba["initial"]}},
    ]
    if reqs:
        rows.append({"phase": "P6", "name": "evidence", "at": at(26), "payload": {"requests": reqs, "voi": {"asked": True, "branches": {}}, "counterfactual": nba.get("what_changed", "")}})
    else:
        rows.append({"phase": "P6", "name": "evidence", "at": at(26), "payload": {"requests": [], "voi": {"asked": False, "reason": "no admissible request can change the action set"}, "counterfactual": ""}})
    rows += [
        {"phase": "P7", "name": "nba_final", "at": at(30), "payload": {"fraud_probability": p_post, "verdict": case["verdict"], "actions": nba["final"], "what_changed": nba.get("what_changed", "nothing"), "stop_reason": ans.get("stop_reason", "")}},
        {"phase": "P8", "name": "sar", "at": at(36), "payload": {"file": ans["sar"]["file"], "validator": {"ok": True, "checks": []}}},
        {"phase": "P9", "name": "explain", "at": at(40), "payload": {"summary": case.get("summary", "")}},
        {"phase": "P10", "name": "persist", "at": at(41), "payload": {"graph_case_id": case.get("graph_case_id", ""), "written_to_graph": case.get("written_to_graph", False)}},
    ]
    return rows


def synth_calls(ans: dict, trigger_type: str, opened_at: str, model: str) -> list[dict]:
    case = ans["case"]
    ctx = {"txn": ans.get("_flagged_txn_id") or (case["affected_txn_ids"][0] if case["affected_txn_ids"] else ""), "card": ans.get("_card_id", ""), "customer": ans.get("_customer_id", "")}
    names = MANDATORY_QUERIES + PLAYBOOK_BY_TRIGGER.get(trigger_type, [])
    names = names[: max(int(ans.get("tool_calls", 6)), len(MANDATORY_QUERIES))]
    rows = []
    seq = 0
    t = dt.datetime.strptime(opened_at, "%Y-%m-%d %H:%M:%S")
    for i, name in enumerate(names):
        seq += 1
        params = {"as_of": opened_at}
        if name in ("case_context", "episode_candidates", "recurring_charge_check"):
            params["t"] = ctx["txn"]
        elif name == "prior_cases_for_customer":
            params["cu"] = ctx["customer"]
        elif name in ("similar_prior_cases", "grounding_chunks"):
            params = {"k": 8 if name == "similar_prior_cases" else 5}
        else:
            params["c"] = ctx["card"]
        rows.append({"seq": seq, "ts": (t + dt.timedelta(seconds=2 * i)).strftime("%Y-%m-%d %H:%M:%S"), "kind": "tool", "phase": "P0" if i < 4 else ("P1" if i < 6 else "P2"), "name": name, "params": params, "latency_s": round(0.4 + 0.1 * i, 2), "result_bytes": 1800 + 400 * i, "ok": True})
    tokens_total = int(ans.get("tokens", 0))
    per = max(tokens_total // max(len(LLM_PHASES), 1), 1)
    for j, (phase, pname) in enumerate(LLM_PHASES.items()):
        seq += 1
        rows.append({"seq": seq, "ts": (t + dt.timedelta(seconds=30 + 3 * j)).strftime("%Y-%m-%d %H:%M:%S"), "kind": "llm", "phase": phase, "name": pname, "model": model, "input_tokens": int(per * 0.8), "output_tokens": int(per * 0.2), "cache_read_tokens": 0, "latency_s": round(2.5 + 0.5 * j, 2), "ok": True})
    return rows


def load_case_pack(path: str) -> dict[str, dict]:
    import csv

    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {r["case_id"]: r for r in csv.DictReader(f)}


def build(answers_dir: str, run_id: str, runs_dir: str, case_pack_csv: str, model: str = "mock") -> list[str]:
    files = sorted(glob.glob(os.path.join(answers_dir, "HHG-*.json")))
    if not files:
        files = sorted(glob.glob(os.path.join("tests", "fixtures", "answers", "*.sample.json")))
    if not files:
        raise SystemExit(f"no answer files under {answers_dir} or tests/fixtures/answers")
    pack = load_case_pack(case_pack_csv)
    out_root = os.path.join(runs_dir, run_id)
    os.makedirs(out_root, exist_ok=True)
    written = []
    for fp in files:
        with open(fp, encoding="utf-8") as f:
            ans = json.load(f)
        cid = ans["case_id"]
        meta = pack.get(cid, {})
        trigger = meta.get("trigger_type", "risk_score")
        opened = meta.get("opened_at", "2016-12-01 00:00:00")
        ans_ctx = dict(ans, _flagged_txn_id=meta.get("flagged_txn_id", ""), _card_id=meta.get("card_id", ""), _customer_id=meta.get("customer_id", ""))
        d = os.path.join(out_root, cid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "answer.json"), "w", encoding="utf-8") as f:
            json.dump(ans, f, indent=2, ensure_ascii=False)
        with open(os.path.join(d, "phases.jsonl"), "w", encoding="utf-8") as f:
            for row in synth_phases(ans, trigger, opened):
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        with open(os.path.join(d, "calls.jsonl"), "w", encoding="utf-8") as f:
            for row in synth_calls(ans_ctx, trigger, opened, model):
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        sar = ans.get("sar", {})
        with open(os.path.join(d, "sar.md"), "w", encoding="utf-8") as f:
            if sar.get("file"):
                f.write(f"# Suspicious Activity Report — {cid}\n\n{sar.get('narrative','')}\n\n**Subjects:** {', '.join(sar.get('subjects', []))}  \n**Total:** ${sar.get('total_amount_usd', 0):,.2f}  \n**Activity dates:** {' to '.join(sar.get('activity_dates', []))}\n")
            else:
                f.write("")
        written.append(cid)
    manifest = {
        "run_id": run_id,
        "mode": "mock",
        "model": model,
        "created_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S"),
        "cases": written,
        "prompt_sha256": {},
        "source_answers": os.path.abspath(answers_dir),
        "fingerprint": _sha(json.dumps(written)),
    }
    with open(os.path.join(out_root, "run_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return written


def _lines(path: str) -> int:
    with open(path, encoding="utf-8") as f:
        return sum(1 for _ in f)


def _action_names(actions: list | None) -> list[str]:
    """``next_best_actions.final`` holds action objects; show the verbs, not the dict repr."""
    return [a.get("action", "?") if isinstance(a, dict) else str(a) for a in (actions or [])]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--answers", default="cases")
    ap.add_argument("--run-id", default="mock")
    ap.add_argument("--runs", default=os.getenv("RUNS_DIR", "runs"))
    ap.add_argument("--case-pack", default="data/raw/case_pack.csv")
    ap.add_argument("--model", default="mock")
    a = ap.parse_args(argv)
    out_root = os.path.join(a.runs, a.run_id)
    have = len(glob.glob(os.path.join(a.answers, "HHG-*.json")))
    header(
        "ops.make_mock_run",
        "replay a runs/ tree from finished answer files so the UI works with no graph and no keys",
        {
            "answers": a.answers if have else "tests/fixtures/answers (no cases/HHG-*.json yet)",
            "case pack": a.case_pack if os.path.exists(a.case_pack) else f"{a.case_pack} (missing: trigger/opened_at defaulted)",
            "run id": a.run_id,
            "output": out_root,
            "model": a.model,
        },
    )
    if not have:
        warn(f"no answer files in {a.answers}/ - falling back to the checked-in sample answers")
    step(f"synthesising phases.jsonl, calls.jsonl and sar.md under {out_root}")
    try:
        written = build(a.answers, a.run_id, a.runs, a.case_pack, a.model)
    except SystemExit as exc:  # build() raises when there is nothing at all to replay
        fail(str(exc))
        summary("mock run not written", {"answers": a.answers, "run id": a.run_id}, status="fail")
        return 1

    t = Table(
        Col("case", width=7),
        Col("verdict", max_width=10),
        Col("p", align="right", width=4),
        Col("pattern", max_width=22),
        Col("exposure", align="right", width=11),
        Col("final actions", max_width=30),
        Col("phases", align="right", width=6),
        Col("calls", align="right", width=5),
        Col("sar", width=3, align="center"),
        title=f"{len(written)} case(s) replayed into {out_root}",
        caption="phases = P0..P10 rows, calls = GSQL tool rows + one LLM row per LLM phase",
    )
    sars = 0
    exposure = 0.0
    for cid in written:
        d = os.path.join(out_root, cid)
        with open(os.path.join(d, "answer.json"), encoding="utf-8") as f:
            ans = json.load(f)
        case = ans["case"]
        filed = bool(ans.get("sar", {}).get("file"))
        sars += filed
        exposure += float(case.get("exposure_usd") or 0)
        t.add_row(
            cid,
            case.get("verdict", "-"),
            prob(case.get("fraud_probability")),
            case.get("pattern") or "-",
            money(case.get("exposure_usd")),
            joinlist(_action_names(ans.get("next_best_actions", {}).get("final")), max_items=3),
            _lines(os.path.join(d, "phases.jsonl")),
            _lines(os.path.join(d, "calls.jsonl")),
            "Y" if filed else "-",
            style="yellow" if case.get("verdict") == "fraud" else None,
        )
    t.print()
    ok(f"run manifest -> {os.path.join(out_root, 'run_manifest.json')} (mode=mock, model={a.model})")
    summary(
        "mock run ready",
        {
            "cases": len(written),
            "SARs filed": sars,
            "total exposure": money(exposure),
            "run id": a.run_id,
            "next": f"RUN_MODE=mock make ui   (pick run {a.run_id})",
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
