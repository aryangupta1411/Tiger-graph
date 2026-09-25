"""Per-case run log (contracts §C `agent/runlog.py`, §D `runs/<run_id>/<case_id>/`).

Every graph / retrieval call goes to `calls.jsonl`; every LLM call goes to BOTH files and every
phase output goes to `phases.jsonl`. `totals()` feeds `tool_calls`, `tokens` and `latency_s` in the
answer file. The log is append-only and safe to tail from the UI.

Row shapes are the ones `ui/common.py` and `ops/make_mock_run.py` read:

  calls.jsonl  {"seq", "ts", "kind": "tool", "phase", "name", "params", "latency_s", "result_bytes", "ok", "error", "caller"}
               {"seq", "ts", "kind": "llm",  "phase", "name", "model", "input_tokens", "output_tokens",
                "cache_read_tokens", "latency_s", "ok"}
  phases.jsonl {"kind": "phase", "phase", "payload"} · {"kind": "llm", ...} · {"kind": "note", ...}
"""
from __future__ import annotations

import contextvars
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _json_default(o: Any):
    if hasattr(o, "model_dump"):
        return o.model_dump()
    if hasattr(o, "__dict__"):
        return o.__dict__
    return str(o)


@dataclass
class RunLog:
    run_id: str
    case_id: str
    root: Path                      # runs/<run_id>/<case_id>
    started: float = field(default_factory=time.time)
    tool_calls: int = 0
    tokens: int = 0
    llm_calls: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.calls_path = self.root / "calls.jsonl"
        self.phases_path = self.root / "phases.jsonl"

    # ---- writers ---------------------------------------------------------
    def _append(self, path: Path, row: dict) -> None:
        row = {"t": round(time.time() - self.started, 3), **row}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True, default=_json_default) + "\n")

    writes: int = 0

    def record_tool(self, name: str, params: dict, latency_s: float, result_bytes: int,
                    ok: bool = True, error: str = "", caller: str = "llm", phase: str = "") -> None:
        """One graph or retrieval call (MCP query, local retriever, OFAC lookup).

        `tool_calls` (the answer-file field) counts every READ made for the case —
        the model's typed-tool calls and the harness's mandatory / engine re-run
        queries and retrievers. Persist-phase writes (caller="persist") are logged
        but counted separately in `writes`.
        """
        if caller == "persist":
            self.writes += 1
            seq = -self.writes
        else:
            self.tool_calls += 1
            seq = self.tool_calls
        self._append(self.calls_path, {
            "seq": seq, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "tool", "phase": phase or current_phase.get(),
            "name": name, "params": params, "latency_s": round(latency_s, 3),
            "result_bytes": result_bytes, "ok": ok, "error": error[:500], "caller": caller,
        })

    def record_llm(self, phase: str, response: Any, latency_s: float = 0.0) -> None:
        """One Messages API response (BetaMessage / ParsedBetaMessage / fake).

        Written to `phases.jsonl` (full usage) and, in the UI's row shape, to `calls.jsonl` so the
        run page can show tools and model calls in one table (M22).
        """
        usage = getattr(response, "usage", None)
        inp = int(getattr(usage, "input_tokens", 0) or 0)
        out = int(getattr(usage, "output_tokens", 0) or 0)
        cr = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cw = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        self.tokens += inp + out + cr + cw
        self.cache_read_tokens += cr
        self.cache_write_tokens += cw
        self.llm_calls += 1
        self._append(self.phases_path, {
            "kind": "llm", "phase": phase, "model": getattr(response, "model", ""),
            "id": getattr(response, "id", ""), "stop_reason": getattr(response, "stop_reason", ""),
            "usage": {"input": inp, "output": out, "cache_read": cr, "cache_write": cw},
            "input_tokens": inp, "output_tokens": out, "cache_read_tokens": cr, "latency_s": round(latency_s, 3),
        })
        self._append(self.calls_path, {
            "seq": self.llm_calls, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "llm", "phase": phase,
            "name": phase, "model": getattr(response, "model", ""), "input_tokens": inp, "output_tokens": out,
            "cache_read_tokens": cr, "cache_write_tokens": cw, "latency_s": round(latency_s, 3), "ok": True,
        })

    def set_phase(self, phase: str) -> None:
        """Stamp the following `calls.jsonl` rows with this phase (M22)."""
        current_phase.set(phase)

    def record_phase(self, phase: str, payload: Any) -> None:
        current_phase.set(phase.split("-")[0])          # "P6-voi" logs its calls under "P6" (M22)
        self._append(self.phases_path, {"kind": "phase", "phase": phase, "name": PHASE_NAMES.get(phase.split("-")[0], phase),
                                        "at": time.strftime("%Y-%m-%d %H:%M:%S"), "payload": payload})

    def note(self, text: str, **extra: Any) -> None:
        self._append(self.phases_path, {"kind": "note", "text": text, **extra})

    # ---- totals ----------------------------------------------------------
    def totals(self) -> dict:
        return {
            "tool_calls": self.tool_calls,
            "tokens": self.tokens,
            "latency_s": round(time.time() - self.started, 1),
        }


# The active run log for helpers that have no runlog parameter in their contract
# signature (mcp_client.run_query, tools_local.*). Set by the phase machine.
current_runlog: contextvars.ContextVar[RunLog | None] = contextvars.ContextVar("hhgoa_runlog", default=None)

# The phase every `calls.jsonl` row is stamped with; `record_phase` advances it (M22).
current_phase: contextvars.ContextVar[str] = contextvars.ContextVar("hhgoa_phase", default="P0")

PHASE_NAMES = {"P0": "intake", "P1": "memory", "P2": "investigate", "P3": "scorecard", "P4": "assess",
               "P5": "nba_initial", "P6": "evidence", "P7": "nba_final", "P8": "sar", "P9": "explain", "P10": "persist"}
