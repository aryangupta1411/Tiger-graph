"""Benchmark runner: `uv run python -m agent.bench --run-id r1 --cases all --model claude-sonnet-5`

- Cases run in `opened_at` order (HHG-017 first … HHG-004 last) so later cases can
  retrieve earlier agent-written ones (PLAN §4.2).
- One MCP session (harness, read + write tools) for the whole run; the LLM only ever
  sees typed read-query wrappers.
- `ensure_awake()` wakes the workspace once before the run (`ops.ensure_awake.warm_up`: only
  transient errors are retried, within TG_RESUME_MAX_WAIT_S; an auth error or an empty TG_SECRET
  fails at once). The per-query resume retry lives in `agent.mcp_client.run_query`; a keep-alive
  task runs a trivial MCP call every KEEP_ALIVE_S seconds so the workspace does not auto-stop
  mid-benchmark.
- `--resume` skips cases whose runs/<run_id>/<case>/answer.json already exists.
- Writes runs/<run_id>/run_manifest.json (model, effort, prompt hashes, versions, git sha); a `--resume`
  invocation extends it: every invocation is kept, and `case_runs` records the git sha each case ran on.

Terminal output goes through `ops.console`: a run header, then one block per case (the case and its
trigger, the P0-P10 phases as the phase machine writes them, the tools it called, the result), then a
board of every case and the run totals. Retries and case failures stay on stderr. Piped or in CI the
same text comes out as plain ASCII with no escape codes; nothing here changes what is written to disk.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

from agent.config import SETTINGS, Settings
from agent.engine_api import ENGINE, CaseContext
from agent.mcp_client import GraphUnavailable, open_session
from agent.phase_machine import PhaseMachine
from agent.runlog import PHASE_NAMES, RunLog
from ops.console import Col, Table, detail, fail, header, joinlist, ok, prob, rule, step, summary, truncate, warn


@contextlib.contextmanager
def _on_stderr() -> Iterator[None]:
    """Route console output to stderr for the duration of the block.

    Retries, keep-alive hiccups and case failures stay on stderr (where they have always
    been, so anything redirecting stdout keeps working) but are still formatted by
    `ops.console` — the module writes to whatever `sys.stdout` is bound to at write time.
    """
    sys.stdout.flush()                      # keep the two streams in order when both are piped
    with contextlib.redirect_stdout(sys.stderr):
        yield
        sys.stderr.flush()


def load_case_pack(path: Path) -> list[CaseContext]:
    rows = []
    with Path(path).open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            rs = r.get("risk_score", "")
            rows.append(CaseContext(case_id=r["case_id"], trigger_type=r["trigger_type"], trigger_text=r["trigger_text"],
                                    flagged_txn_id=str(r["flagged_txn_id"]), card_id=r["card_id"], customer_id=r["customer_id"],
                                    opened_at=r["opened_at"], risk_score=float(rs) if rs not in ("", None) else None))
    rows.sort(key=lambda c: (c.opened_at, c.case_id))
    return rows


def select_cases(cases: list[CaseContext], spec: str) -> list[CaseContext]:
    if spec in ("all", "*", ""):
        return cases
    wanted = {s.strip() for s in spec.split(",") if s.strip()}
    return [c for c in cases if c.case_id in wanted]


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        return ""


def _versions() -> dict:
    import importlib.metadata as m
    out = {}
    for pkg in ("anthropic", "claude-agent-sdk", "mcp", "tigergraph-mcp", "pyTigerGraph", "sentence-transformers", "torch", "voyageai", "pydantic"):
        try:
            out[pkg] = m.version(pkg)
        except Exception:
            out[pkg] = ""
    return out


def _resolve_backend(settings: Settings) -> str:
    from agent import llm
    return llm.resolve_backend(settings)


def _git_dirty() -> bool | None:
    """True when tracked files differ from HEAD (the cases then did not run on exactly `git_sha`)."""
    try:
        out = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], stderr=subprocess.DEVNULL, text=True)
        return bool(out.strip())
    except Exception:
        return None


def _read_manifest(run_dir: Path) -> dict:
    try:
        return json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_manifest_file(run_dir: Path, manifest: dict) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def write_manifest(run_dir: Path, settings: Settings, model: str, case_ids: list[str], resume: bool = False) -> dict:
    """Start (or, on `--resume` / a re-invocation of the same run id, extend) runs/<run_id>/run_manifest.json.

    The top-level fields describe the latest invocation (what `agent.promote` reads); `invocations` keeps every
    invocation of the run id, `cases` is the union of the cases any invocation selected, and `case_runs`
    (filled by `record_case_run`) records, per completed case, the git sha / prompt hashes / model it ran on.
    Returns the invocation record."""
    prompts = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(settings.prompts_dir.glob("*.md"))}
    prev = _read_manifest(run_dir)
    invocation = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "git_sha": _git_sha(), "git_dirty": _git_dirty(),
                  "model": model, "sar_model": settings.sar_model, "effort": settings.effort, "run_mode": settings.run_mode,
                  "llm_backend": _resolve_backend(settings), "engine": ENGINE.source, "resume": bool(resume),
                  "cases_selected": list(case_ids), "prompt_sha256": prompts}
    invocations = list(prev.get("invocations") or [])
    if prev and not invocations:           # a manifest written before invocations were recorded
        invocations.append({k: prev.get(k) for k in ("started", "git_sha", "model", "sar_model", "effort", "run_mode",
                                                      "llm_backend", "engine", "prompt_sha256")} | {"cases_selected": prev.get("cases", [])})
    invocations.append(invocation)
    cases = list(dict.fromkeys([*(prev.get("cases") or []), *case_ids]))
    manifest = {"run_id": run_dir.name, "started": invocation["started"], "first_started": prev.get("first_started") or prev.get("started") or invocation["started"],
                "model": model, "sar_model": settings.sar_model,
                "effort": settings.effort, "run_mode": settings.run_mode, "llm_backend": invocation["llm_backend"], "engine": ENGINE.source, "max_tool_calls": settings.max_tool_calls,
                "prompt_sha256": prompts, "versions": _versions(), "git_sha": invocation["git_sha"], "git_dirty": invocation["git_dirty"],
                "cases": cases, "invocations": invocations, "case_runs": dict(prev.get("case_runs") or {}),
                "embed_model": settings.embed_model, "embed_dim": settings.embed_dim}
    _write_manifest_file(run_dir, manifest)
    return invocation


def record_case_run(run_dir: Path, case_id: str, invocation: dict, answer: dict | None = None) -> None:
    """Record in run_manifest.json that `case_id` completed under `invocation` (git sha, prompts, model)."""
    manifest = _read_manifest(run_dir)
    runs = dict(manifest.get("case_runs") or {})
    runs[case_id] = {"finished": time.strftime("%Y-%m-%dT%H:%M:%S"), "invocation_started": invocation.get("started"),
                     "git_sha": invocation.get("git_sha"), "git_dirty": invocation.get("git_dirty"), "model": invocation.get("model"),
                     "sar_model": invocation.get("sar_model"), "llm_backend": invocation.get("llm_backend"),
                     "prompt_sha256": {k: str(v)[:12] for k, v in (invocation.get("prompt_sha256") or {}).items()},
                     "tool_calls": (answer or {}).get("tool_calls"), "tokens": (answer or {}).get("tokens")}
    manifest["case_runs"] = dict(sorted(runs.items()))
    manifest["cases"] = list(dict.fromkeys([*(manifest.get("cases") or []), case_id]))
    manifest["git_shas"] = sorted({str(r.get("git_sha") or "") for r in runs.values()} - {""})
    _write_manifest_file(run_dir, manifest)


def ensure_awake(settings: Settings = SETTINGS, max_wait_s: int = 240) -> None:
    """Wake the Savanna workspace before the run (auto-resume drops the first request).

    `ops.ensure_awake.warm_up(tg_connection())`: retries only transient errors (502/503/504, connection
    errors, an HTML start page) within TG_RESUME_MAX_WAIT_S (default 240 s); `max_wait_s` is kept for the
    call-site signature. An auth error, a wrong host/graph or an empty TG_SECRET raises at once instead of
    spinning for four minutes and blaming the workspace. The connection carries the GSQL-TIMEOUT header.
    """
    if settings.mock:
        return
    from ops.ensure_awake import tg_connection, warm_up

    warm_up(tg_connection())


async def keep_alive(session, every_s: int, stop: asyncio.Event) -> None:
    """M25: the benchmark's keep-alive is `tigergraph__get_vertex_count`, not an installed query, exactly
    because the MCP parameter encoding differs from pyTigerGraph's. `ops/keep_alive.py` pings through
    pyTigerGraph and may pass `KEEP_ALIVE_PARAMS={"cu": "C13487", ...}` with the VERTEX parameter as a bare
    id; over MCP the same parameter has to be written `{"id": "C13487"}` (contracts §A). Do not copy the
    `.env` value into an MCP call."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=every_s)
            break
        except TimeoutError:
            pass
        try:
            await session.call_tool("tigergraph__get_vertex_count", {"vertex_type": "FraudPattern"})
        except Exception as e:
            with _on_stderr():
                warn(f"keep_alive: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- live case view

class CaseTail:
    """Reads the run log the phase machine is writing and reports it as it happens.

    `RunLog` appends every phase to `phases.jsonl` and every graph/LLM call to `calls.jsonl`
    (both under `runs/<run_id>/<case_id>/`). Tailing those files gives a live P0-P10 view with
    no hook into the phase machine, and nothing here changes a single byte of either file.
    """

    def __init__(self, case_dir: Path) -> None:
        self.phases_path = case_dir / "phases.jsonl"
        self.calls_path = case_dir / "calls.jsonl"
        self._phases_seen = 0
        self._calls_seen = 0
        self._pending: list[dict] = []      # call rows not yet counted into a printed phase line
        self.tool_rows: list[dict] = []
        self.tools = 0
        self.tokens = 0
        self.llm_calls = 0
        self.errors = 0

    @staticmethod
    def _new_rows(path: Path, seen: int) -> list[dict]:
        """Complete JSONL rows after the first `seen` of them (a half-written tail line is left)."""
        if not path.exists():
            return []
        rows: list[dict] = []
        try:
            with path.open(encoding="utf-8") as fh:
                for i, line in enumerate(fh):
                    if i < seen:
                        continue
                    line = line.strip()
                    if not line:
                        break
                    try:
                        rows.append(json.loads(line))
                    except ValueError:      # last line still being written; pick it up next poll
                        break
        except OSError:
            return rows
        return rows

    def _count(self, upto: float | None) -> None:
        """Fold buffered call rows older than `upto` (all of them when None) into the counters.

        Both logs carry `t`, seconds since the case started, so a phase line can report the tool
        and token counts as they stood WHEN THAT PHASE FINISHED - not the end-of-case totals,
        which is what a fast (mock) run would otherwise show on every line.
        """
        keep: list[dict] = []
        for r in self._pending:
            if upto is not None and float(r.get("t", 0) or 0) > upto:
                keep.append(r)
                continue
            if r.get("kind") == "llm":
                self.llm_calls += 1
                self.tokens += sum(int(r.get(k, 0) or 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
            else:
                self.tool_rows.append(r)
                if r.get("caller") != "persist":
                    self.tools += 1
                if not r.get("ok", True):
                    self.errors += 1
        self._pending = keep

    def drain(self, quiet: bool = False) -> None:
        """Consume everything written since the last call, printing one line per phase.

        `quiet=True` counts without printing (used once before a case starts, so a `--resume`
        rerun does not replay the previous attempt's phases).
        """
        calls = self._new_rows(self.calls_path, self._calls_seen)
        self._calls_seen += len(calls)
        self._pending += calls
        phases = self._new_rows(self.phases_path, self._phases_seen)
        self._phases_seen += len(phases)
        if quiet:
            self._count(None)
            return
        for r in phases:
            kind = r.get("kind")
            if kind == "phase":
                t = float(r.get("t", 0) or 0)
                self._count(t)
                p = str(r.get("phase", ""))
                name = str(r.get("name") or PHASE_NAMES.get(p.split("-")[0], ""))
                step(f"{p:<7}{name:<12}{t:>7.1f}s  tools {self.tools:>3}  llm {self.llm_calls:>2}  tokens {self.tokens:>9,}")
            elif kind == "note":
                text = str(r.get("text", "")).strip()
                extra = {k: v for k, v in r.items() if k not in ("kind", "text", "t")}
                detail(f"note: {text}" + (f" ({joinlist([f'{k}={truncate(v, 110)}' for k, v in extra.items()], sep=', ')})" if extra else ""))

    def flush(self) -> None:
        """Count every remaining call row — call once the case is over, before the tools table."""
        self._count(None)

    def tools_table(self, case_id: str) -> Table:
        """One row per distinct tool the case called, in first-call order.

        `caller` is who asked: `llm` (a typed tool the model chose), `harness` (a mandatory
        query the phase machine runs itself) or `persist` (a P10 write-back, counted in
        `writes`, not in `tool_calls`).
        """
        t = Table(
            Col("tool", max_width=36),
            Col("callers", max_width=16),
            Col("phases", max_width=14),
            Col("calls", align="right", width=5),
            Col("ms", align="right", width=8),
            Col("bytes", align="right", width=9),
            Col("errors", align="right", width=6),
            title=f"{case_id} tool calls",
        )
        agg: dict[str, dict] = {}
        writes = 0
        for r in self.tool_rows:
            a = agg.setdefault(str(r.get("name", "")), {"callers": [], "phases": [], "n": 0, "ms": 0.0, "bytes": 0, "err": 0})
            for key, val in (("phases", str(r.get("phase", ""))), ("callers", str(r.get("caller", "")))):
                if val and val not in a[key]:
                    a[key].append(val)
            a["n"] += 1
            a["ms"] += float(r.get("latency_s", 0) or 0) * 1000
            a["bytes"] += int(r.get("result_bytes", 0) or 0)
            a["err"] += 0 if r.get("ok", True) else 1
            writes += int(r.get("caller") == "persist")
        for name, a in agg.items():
            t.add_row(name, joinlist(a["callers"]), joinlist(a["phases"], max_items=3), a["n"], f"{a['ms']:,.0f}",
                      f"{a['bytes']:,}", a["err"] or "-", style="red" if a["err"] else None)
        t.caption = (f"{len(agg)} distinct tool(s): {self.tools} read call(s) counted in tool_calls, "
                     f"{writes} P10 graph write(s), {self.errors} error(s)")
        return t


async def watch_case(tail: CaseTail, stop: asyncio.Event, every: float = 0.4) -> None:
    """Poll the case's run log while `pm.run_case` is awaiting, so phases appear as they finish."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
            break
        except TimeoutError:
            pass
        finally:
            tail.drain()


def make_client(settings: Settings):
    """The LLM backend for this run (`agent/llm.py`): cli | api | mock, from LLM_BACKEND.

    `cli` (the default) needs no API key — claude-agent-sdk drives the bundled Claude Code CLI on
    the user's subscription. `--mock` / RUN_MODE=mock still selects FakeAnthropic unless
    LLM_BACKEND says otherwise, which is what lets a real model run against DuckDB-backed facts
    (`LLM_BACKEND=cli RUN_MODE=mock`, i.e. mock *graph*, live *model*).
    """
    from agent import llm
    return llm.make_backend(settings)


async def run(args: argparse.Namespace) -> int:
    settings = SETTINGS
    if args.model:
        settings = Settings(**{**settings.__dict__, "model": args.model, "sar_model": args.sar_model or args.model})
    if args.mock:
        os.environ["RUN_MODE"] = "mock"
        settings = Settings(**{**settings.__dict__, "run_mode": "mock"})
    if args.llm_backend:
        os.environ["LLM_BACKEND"] = args.llm_backend
        settings = Settings(**{**settings.__dict__, "llm_backend": args.llm_backend})
    cases = select_cases(load_case_pack(Path(args.case_pack) if args.case_pack else settings.case_pack_csv), args.cases)
    if not cases:
        with _on_stderr():
            fail(f"no cases selected (--cases {args.cases!r} matched nothing in the case pack)")
        return 2
    run_dir = settings.runs_dir / args.run_id
    header(
        "agent.bench",
        f"agent run over {len(cases)} case(s) in opened_at order -> {run_dir}",
        {
            "run id": args.run_id,
            "cases": f"{len(cases)}: {joinlist([c.case_id for c in cases], max_items=6)}",
            "model": settings.model,
            "sar model": settings.sar_model,
            "llm backend": _resolve_backend(settings),
            "run mode": settings.run_mode,
            "engine": ENGINE.source,
            "max tool calls": settings.max_tool_calls,
            "out": run_dir,
        },
    )
    invocation = write_manifest(run_dir, settings, settings.model, [c.case_id for c in cases], resume=bool(args.resume))
    ok(f"manifest -> {run_dir / 'run_manifest.json'}")
    ensure_awake(settings)
    client = make_client(settings)
    failures = 0
    t_run = time.time()
    board = Table(
        Col("case", width=7),
        Col("trigger", max_width=15),
        Col("verdict", max_width=10),
        Col("p", align="right", width=5),
        Col("pattern", max_width=20),
        Col("final actions", max_width=38),
        Col("sar", width=3, align="center"),
        Col("tools", align="right", width=5),
        Col("llm", align="right", width=4),
        Col("tokens", align="right", width=9),
        Col("secs", align="right", width=6),
        Col("problems", align="right", width=8),
        title=f"run {args.run_id}: {len(cases)} case(s), opened_at order",
    )
    totals = {"sar": 0, "tools": 0, "tokens": 0, "problems": 0, "skipped": 0, "not_run": 0}
    async with open_session(read_only=False, settings=settings) as (session, _tools):
        stop = asyncio.Event()
        ka = asyncio.create_task(keep_alive(session, settings.keep_alive_s, stop)) if not settings.mock else None
        try:
            pm = PhaseMachine(client, session, args.run_id, settings)
            for i, ctx in enumerate(cases, 1):
                out = run_dir / ctx.case_id / "answer.json"
                rule(f"[{i}/{len(cases)}] {ctx.case_id} {ctx.trigger_type}")
                if args.resume and out.exists():
                    totals["skipped"] += 1
                    warn(f"{ctx.case_id} resume: {out} exists, skipping")
                    board.add_row(ctx.case_id, ctx.trigger_type, "(resumed)", "-", "-", "-", "-", "-", "-", "-", "-", "-", style="dim")
                    if ctx.case_id not in (_read_manifest(run_dir).get("case_runs") or {}):
                        # an answer from before case_runs existed: provenance unknown, say so rather than guess
                        record_case_run(run_dir, ctx.case_id, {"started": None, "git_sha": "unknown (resumed; predates case_runs)"})
                    continue
                if settings.mock and hasattr(session, "use_case"):
                    session.use_case(ctx.case_id)
                step(f"{ctx.case_id} {ctx.trigger_type}: {truncate(ctx.trigger_text, 130)}")
                detail(f"txn {ctx.flagged_txn_id}  card {ctx.card_id}  customer {ctx.customer_id}  opened {ctx.opened_at}"
                       + (f"  risk_score {prob(ctx.risk_score, digits=3)}" if ctx.risk_score is not None else ""))
                log = RunLog(args.run_id, ctx.case_id, run_dir / ctx.case_id)
                tail = CaseTail(run_dir / ctx.case_id)
                tail.drain(quiet=True)          # a --resume rerun starts from the existing log
                tail_stop = asyncio.Event()
                tailer = asyncio.create_task(watch_case(tail, tail_stop))
                t0 = time.time()
                answer, err = None, None
                try:
                    answer = await pm.run_case(ctx, log)
                except Exception as e:                 # one bad case must not end the run (unless --fail-fast)
                    err = e
                finally:
                    tail_stop.set()
                    await tailer
                    tail.drain()
                    tail.flush()
                    if tail.tool_rows:
                        tail.tools_table(ctx.case_id).print()
                if err is not None:
                    failures += 1
                    log.note("case failed", error=repr(err)[:800])
                    with _on_stderr():
                        fail(f"{ctx.case_id} FAILED: {err!r}")
                        detail(f"after {time.time() - t0:.1f}s, {tail.tools} tool call(s), {tail.llm_calls} llm call(s)")
                    board.add_row(ctx.case_id, ctx.trigger_type, "FAILED", "-", "-", f"{type(err).__name__}: {err}", "-",
                                  tail.tools, tail.llm_calls, f"{tail.tokens:,}", f"{time.time() - t0:.1f}", "-", style="red")
                    if args.fail_fast:
                        raise err
                    if isinstance(err, GraphUnavailable):
                        # the workspace stayed down for the whole resume budget: every later case would spin the
                        # same TG_RESUME_MAX_WAIT_S on its first read and fail too. Stop; --resume picks up here.
                        with _on_stderr():
                            fail(f"workspace unreachable for TG_RESUME_MAX_WAIT_S; stopping after {ctx.case_id}. "
                                 f"Rerun with --run-id {args.run_id} --resume once `make awake` answers.")
                        totals["not_run"] = len(cases) - i
                        break
                    continue
                try:
                    record_case_run(run_dir, ctx.case_id, invocation, answer)
                    c = answer["case"]
                    actions = [a["action"] for a in answer["next_best_actions"]["final"]]
                    problems = answer.get("_validation", []) or []
                    sar = bool(answer["sar"]["file"])
                    totals["sar"] += int(sar)
                    totals["tools"] += int(answer["tool_calls"])
                    totals["tokens"] += int(answer["tokens"])
                    totals["problems"] += len(problems)
                    board.add_row(
                        ctx.case_id, ctx.trigger_type, c["verdict"], prob(c["fraud_probability"]), c["pattern"] or "-",
                        joinlist(actions), "Y" if sar else "-", answer["tool_calls"], tail.llm_calls, f"{int(answer['tokens']):,}",
                        f"{time.time() - t0:.1f}", len(problems) or "-",
                        style=("red" if problems else ("yellow" if c["verdict"] == "uncertain" else None)),
                    )
                    (warn if problems else ok)(
                        f"{ctx.case_id} {c['verdict']} p={prob(c['fraud_probability'])} pattern={c['pattern'] or 'none'} "
                        f"sar={'yes' if sar else 'no'} tools={answer['tool_calls']} tokens={int(answer['tokens']):,} "
                        f"{time.time() - t0:.1f}s problems={len(problems)}"
                    )
                    detail(f"final actions: {joinlist(actions)}")
                    detail(f"answer -> {out}")
                    for p in problems:
                        detail(f"validator: {p}")
                except Exception as e:
                    failures += 1
                    with _on_stderr():
                        fail(f"{ctx.case_id} answer unreadable: {type(e).__name__}: {e}")
                    board.add_row(ctx.case_id, ctx.trigger_type, "ERROR", "-", "-", str(e)[:200], "-", tail.tools, tail.llm_calls,
                                  f"{tail.tokens:,}", f"{time.time() - t0:.1f}", "-", style="red")
        finally:
            stop.set()
            if ka:
                await ka
    rule("run complete")
    board.print()
    summary(
        f"agent.bench {args.run_id}",
        {
            "cases": (f"{len(cases)} selected, {len(cases) - failures - totals['skipped'] - totals['not_run']} completed, "
                      f"{totals['skipped']} skipped, {failures} failed"
                      + (f", {totals['not_run']} not run (workspace unreachable)" if totals["not_run"] else "")),
            "SARs filed": totals["sar"],
            "tool calls": f"{totals['tools']:,}",
            "tokens": f"{totals['tokens']:,}",
            "validator problems": totals["problems"],
            "wall clock": f"{time.time() - t_run:.1f}s",
            "run dir": run_dir,
        },
        status="fail" if failures else ("warn" if totals["problems"] else "ok"),
    )
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="HHGOA benchmark runner")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--cases", default="all", help="all | HHG-014 | HHG-014,HHG-006")
    ap.add_argument("--model", default=None)
    ap.add_argument("--sar-model", default=None)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--mock", action="store_true", help="RUN_MODE=mock: FakeSession (DuckDB facts) + FakeAnthropic unless --llm-backend says otherwise")
    ap.add_argument("--llm-backend", default=None, choices=["cli", "api", "mock"],
                    help="cli (claude-agent-sdk, no API key, default) | api (ANTHROPIC_API_KEY) | mock (FakeAnthropic)")
    ap.add_argument("--case-pack", default=None)
    ap.add_argument("--fail-fast", action="store_true")
    args = ap.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
