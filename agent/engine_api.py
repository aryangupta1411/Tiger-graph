"""Resolver for the engine interfaces (contracts §B).

Prefers the real `engine/` package; falls back to `agent.engine_stub` when it is
not importable (RUN_MODE=mock, CI without the engine, or the engine module not yet
written). Every consumer in `agent/` imports the names from here, never from
`engine` directly, so the fallback is one line to remove.
"""
from __future__ import annotations

import importlib
import os
from types import SimpleNamespace


def _load_real():
    types_ = importlib.import_module("engine.types")
    return SimpleNamespace(
        source="engine",
        CaseContext=types_.CaseContext, Evidence=types_.Evidence, Scorecard=types_.Scorecard,
        scorecard=importlib.import_module("engine.scorecard"),
        pattern_rule=importlib.import_module("engine.pattern_rule"),
        episode=importlib.import_module("engine.episode"),
        policy=importlib.import_module("engine.policy"),
        voi=importlib.import_module("engine.voi"),
        simulator=importlib.import_module("engine.simulator"),
        status=importlib.import_module("engine.status"),
        stop=importlib.import_module("engine.stop"),
    )


def _load_stub():
    from agent import engine_stub as s
    ns = SimpleNamespace(source="agent.engine_stub", CaseContext=s.CaseContext, Evidence=s.Evidence, Scorecard=s.Scorecard)
    ns.scorecard = SimpleNamespace(compute=s.compute, post_evidence=s.post_evidence)
    ns.pattern_rule = SimpleNamespace(label=s.label)
    ns.episode = SimpleNamespace(members=s.members)
    ns.policy = SimpleNamespace(admissible=s.admissible, check=s.check, order=s.order, route=s.route, sar_required=s.sar_required)
    ns.voi = SimpleNamespace(should_ask=s.should_ask)
    ns.simulator = SimpleNamespace(reply=s.reply)
    ns.status = SimpleNamespace(derive=s.derive)
    ns.stop = SimpleNamespace(stop_reason=s.stop_reason)
    return ns


def load(force_stub: bool | None = None):
    if force_stub is None:
        force_stub = os.environ.get("ENGINE_IMPL", "").lower() == "stub"
    if not force_stub:
        try:
            return _load_real()
        except Exception:  # ImportError or a half-written engine: fall back
            pass
    return _load_stub()


ENGINE = load()
CaseContext = ENGINE.CaseContext
Evidence = ENGINE.Evidence
Scorecard = ENGINE.Scorecard
