"""The phase machine P0–P10 (PLAN §4.2), over the LLM backend seam `agent/llm.py`.

Decision authority (PLAN §3.1): the deterministic engine decides — scorecard,
pattern, episode, SAR rule, admissible actions, routes, stop rule; the LLM
investigates through typed MCP tools, writes evidence claims, may move the
probability by at most ±0.10 with a cited reason, chooses within the admissible
set, and writes the summary and the SAR narrative.

The machine asks the backend for exactly two things and does not care which runs
(`LLM_BACKEND` = cli | api | mock, default cli — see `agent/llm.py`):

  llm.parse(phase, schema, user_text, history, system, tool_defs, model, log) -> pydantic object
  llm.investigate(user_text, history, system, tools, model, log, max_iterations) -> None

  cli   claude-agent-sdk on the bundled Claude Code CLI (subscription auth, no ANTHROPIC_API_KEY):
        JSON-schema structured output for the parse phases, an in-process SDK MCP server carrying
        the same typed query tools for P2.
  api   anthropic 1.7.0: client.beta.messages.parse(output_format=Model) and
        client.beta.messages.tool_runner(...) + await runner.generate_tool_call_response();
        cache_control {"type": "ephemeral", "ttl": "1h"} on the system block; thinking adaptive;
        tool_choice {"type": "none"} on parse phases.
  mock  the api code driven by agent/fake_anthropic.FakeAnthropic (RUN_MODE=mock).

History is append-only: every user prompt, every assistant message (including
thinking blocks) and every tool_result is appended and never edited.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from typing import Any

from agent import tools_local
from agent.answer_fallback import build as build_answer_fallback
from agent.answer_fallback import check_invariants
from agent.config import SETTINGS, Settings
from agent.engine_api import ENGINE, CaseContext, Evidence
from agent.llm import wrap_client
from agent.mcp_client import (
    GraphUnavailable,
    ToolContext,
    current_caller,
    extract_ids,
    harness_query,
    llm_view,
    load_query_descriptions,
    make_query_tools,
    run_query,
)
from agent.runlog import RunLog, current_runlog
from agent.schemas import ActionChoice, Assessment, Closing, SarDraft
from agent.validator import HOUSE_RULE_NAMES, SUMMARY_MAX_CHARS, reason_problems, summary_problems, text_sentences


def _resolve_builder():
    """agent.answer_writer.build (contracts §C) > engine.answer_writer.build > agent.answer_fallback.build."""
    for modname in ("agent.answer_writer", "engine.answer_writer"):
        try:
            mod = __import__(modname, fromlist=["build"])
            return mod.build, modname
        except Exception:
            continue
    return build_answer_fallback, "agent.answer_fallback"


def _resolve_validator():
    """agent.validator.validate(answer, db, meta) > engine.validator.validate(answer, db, meta); db = engine DuckFacts."""
    for modname in ("agent.validator", "engine.validator"):
        try:
            mod = __import__(modname, fromlist=["validate"])
            return mod.validate, modname
        except Exception:
            continue
    return None, ""


def _facts_db():
    try:
        from engine import config as ecfg
        from engine.facts_duckdb import DuckFacts

        return DuckFacts() if ecfg.FACTS_DB.exists() else None
    except Exception:
        return None


build_answer, BUILDER_SOURCE = _resolve_builder()
validate_answer, VALIDATOR_SOURCE = _resolve_validator()

TS = "%Y-%m-%d %H:%M:%S"


def _ts_add(ts: str, **delta: float) -> str:
    return (datetime.strptime(ts, TS) + timedelta(**delta)).strftime(TS)


def _to_jsonable(o: Any) -> Any:
    if is_dataclass(o):
        return {k: _to_jsonable(v) for k, v in asdict(o).items()}
    if isinstance(o, set):
        return sorted(o)
    if isinstance(o, dict):
        return {k: _to_jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_to_jsonable(v) for v in o]
    return o


def _fill(template: str, **kw: Any) -> str:
    out = template
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _norm_ids(xs: Any) -> list[str]:
    """Defensive: wave/connected lists may arrive as strings or {"card_id"|"id": ...} rows."""
    out: list[str] = []
    for x in xs or []:
        v = x.get("card_id") or x.get("id") if isinstance(x, dict) else x
        if v and str(v) not in out:
            out.append(str(v))
    return out


# ----------------------------------------------------------------------------- rule citations (§7)

_BLOCKS = ("BLOCK_CARD", "BLOCK_ALL_CARDS")
_R1_CLAIM = re.compile(r"\bR1\b(?!\s*(?:not|does not|is not|n/a))")  # same reading as engine.policy.check
_VERIFY = ("VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH")
_ACTION_DEFAULT_CITE = {
    "CREATE_CASE": "3a", "FILE_REPORT": "3a", "GENERATE_REPORT": "3a", "MONITOR_CONNECTED_CARDS": "R6",
    "ESCALATE_TO_ANALYST": "R8", "DECLINE_TRANSACTION": "R4", "MONITOR_CARD": "R4", "VERIFY_WITH_CUSTOMER": "§5",
    "STEP_UP_AUTH": "§5", "CLOSE_NO_FRAUD": "R3", "ALLOW_TRANSACTION": "R3", "WARN_CUSTOMER": "R7",
    "BLOCK_CARD": "§3b", "BLOCK_ALL_CARDS": "R10",
}


def policy_cite(action: str, gate_rules: list[str], sc: Any) -> str:
    """The Fraud Policy rule an action's reason should cite, translated from the policy gate's rule names
    (engine policy YAML). README rules (R1-R10, 3a, 3b) pass through; the gate's own bands (`fraud_band`,
    `uncertain_initial`, `uncertain_final`, `legit_band`, `legit_leaning_initial`, `3a_case`) are mapped to the
    README rule they implement, so no answer file cites an internal label."""
    p = float(getattr(sc, "p_engine", 0.0) or 0.0)
    fams = len(getattr(sc, "families_fraud", []) or [])
    flags = getattr(sc, "flags", {}) or {}
    denial = bool(flags.get("denial")) or flags.get("post_outcome") in ("deny", "fail")
    out: list[str] = []
    for g in gate_rules or []:
        if re.fullmatch(r"R(?:[1-9]|10)|3a|3b|§\s?[1-7][ab]?", g):     # already a README rule / section (policy YAML `cite`)
            c = g
        elif g in ("3a_case", "3a_report_only"):
            c = "3a"
        elif g == "uncertain_initial":
            c = ("R1" if p < 0.70 and fams < 2 else "§3b/§5") if action in _VERIFY else "3a" if action == "CREATE_CASE" else "§3b"
        elif g == "uncertain_final":
            c = {"ESCALATE_TO_ANALYST": "R8", "CREATE_CASE": "3a"}.get(action, "R4")
        elif g == "fraud_band":
            c = {"CREATE_CASE": "3a", "FILE_REPORT": "3a", "MONITOR_CONNECTED_CARDS": "R6"}.get(action) or (
                ("R2" if denial else "R1 not applicable (p >= 0.70)") if action in _BLOCKS else "§3b/§5" if action in _VERIFY else "§3b")
        elif g in ("legit_band", "legit_leaning_initial"):
            c = "3a" if action == "CREATE_CASE" else ("R3" if flags.get("post_outcome") in ("confirm", "pass") else "§6") if g == "legit_band" else "§6/§3b"
        else:
            continue
        if c not in out:
            out.append(c)
    return "/".join(out) or _ACTION_DEFAULT_CITE.get(action, "§7")


def _family(e: dict) -> str:
    """Evidence family 4 is 'customer or analyst reply': the analyst's trigger (source external, ref trigger) and an
    analyst_info reply belong to it just as a customer statement does."""
    if e.get("source") == "customer" or (e.get("source") == "external" and str(e.get("ref", "")).startswith(("trigger", "evidence_request"))):
        return "customer"
    return "history"


def _norm_scorecard(sc: Any) -> Any:
    sc.connected_card_ids = _norm_ids(sc.connected_card_ids)
    sc.connected_device_profiles = _norm_ids(sc.connected_device_profiles)
    sc.episode_ids = [str(i) for i in sc.episode_ids]
    sc.similar_prior_cases = _norm_ids(sc.similar_prior_cases)
    return sc


class PhaseMachine:
    def __init__(self, client: Any, session: Any, run_id: str, settings: Settings = SETTINGS, engine: Any = ENGINE, tg_conn_factory: Any = None):
        self.llm = wrap_client(client, settings)
        self.client = getattr(self.llm, "client", client)   # the raw SDK client, when there is one
        self.session = session
        self.run_id = run_id
        self.s = settings
        self.E = engine
        self.tg_conn_factory = tg_conn_factory
        self.prompts = {p.stem: p.read_text(encoding="utf-8") for p in settings.prompts_dir.glob("*.md")}
        # Frozen system prefix: one block, cached for the run (>= 512 tokens on Opus 5 / 1024 on Sonnet 5).
        self.system = [{"type": "text", "text": self.prompts["system"], "cache_control": {"type": "ephemeral", "ttl": settings.cache_ttl}}]
        self.descriptions = load_query_descriptions(settings.query_descriptions_yaml)

    # ------------------------------------------------------------------ helpers
    async def _parse(self, phase: str, schema: type, user_text: str, model: str | None = None):
        """One structured phase, on whichever backend is configured (agent/llm.py)."""
        return await self.llm.parse(
            phase=phase, schema=schema, user_text=user_text, history=self.history,
            system=self.system, tool_defs=self.tool_defs, model=model or self.s.model, log=self.log,
        )

    def _known_ids(self) -> set[str]:
        ids: set[str] = set()
        for rows in self.tctx.facts_all.values():
            for r in rows:
                ids.update(extract_ids(r))
        ids.add(self.ctx.flagged_txn_id)
        ids.add(self.ctx.card_id)
        ids.add(self.ctx.customer_id)
        return ids

    def _clean_evidence(self, items: list[Any], known: set[str]) -> list[dict]:
        out = []
        for e in items:
            d = e.model_dump() if hasattr(e, "model_dump") else dict(e)
            kept = [i for i in d.get("entity_ids", []) if i in known and not str(i).startswith("AC-")]
            dropped = [i for i in d.get("entity_ids", []) if i not in kept]
            if dropped:
                self.log.note("dropped unknown entity_ids from a claim", dropped=dropped, ref=d.get("ref"))
            d["entity_ids"] = kept
            if d.get("source") not in ("graph", "document", "customer", "external"):
                d["source"] = "graph"
            out.append(d)
        return out

    def _sc_json(self, sc: Any) -> dict:
        d = _to_jsonable(sc)
        d["ledger"] = [_to_jsonable(e) for e in self.tctx.ledger[-14:]]
        d["trigger_type"] = self.ctx.trigger_type
        d["flagged_txn_id"] = self.ctx.flagged_txn_id
        d["online"] = (self.tctx.facts.get("case_context", {}).get("txn", {}) or {}).get("channel") == "online"
        return d

    def _actions_dicts(self, choice: ActionChoice) -> list[dict]:
        return [a.model_dump() for a in choice.actions]

    # ------------------------------------------------------------------ P0
    async def p0_intake(self, ctx: CaseContext) -> str:
        self.graph_case_id = f"AC-{ctx.case_id}"
        self.log.record_phase("P0", {"ctx": _to_jsonable(ctx), "graph_case_id": self.graph_case_id, "engine": self.E.source})
        tok = current_caller.set("persist")
        try:
            # M8: the installed GSQL signature is
            # open_case(case_id, source_case_id, customer_id, card_id, trigger_type, opened_at, run_id, flagged_txn_id)
            # — `status` is not a parameter (the query writes "open" itself) and flagged_txn_id is required.
            await run_query(
                self.session,
                "open_case",
                {
                    "case_id": self.graph_case_id,
                    "source_case_id": ctx.case_id,
                    "customer_id": ctx.customer_id,
                    "card_id": ctx.card_id,
                    "trigger_type": ctx.trigger_type,
                    "opened_at": ctx.opened_at,
                    "run_id": self.run_id,
                    "flagged_txn_id": ctx.flagged_txn_id,
                },
            )
            self.opened_in_graph = True
        except GraphUnavailable:
            raise           # workspace down: the mandatory reads below would spin the same budget again, then fail
        except Exception as e:
            self.opened_in_graph = False
            self.log.note("open_case writer failed; will persist at P10", error=str(e)[:300])
        finally:
            current_caller.reset(tok)
        # mandatory queries (PLAN §3.5)
        F: dict[str, dict] = {}
        F["case_context"] = await harness_query(self.session, self.tctx, "case_context", {"t": {"id": ctx.flagged_txn_id}, "as_of": ctx.opened_at})
        txn = F["case_context"].get("txn", {}) or {}
        flagged_ts = str(txn.get("ts") or ctx.opened_at)
        F["card_profile"] = await harness_query(self.session, self.tctx, "card_profile", {"c": {"id": ctx.card_id}, "as_of": ctx.opened_at})
        # M5: the same window DuckFacts.collect uses (flagged_ts − 72 h → opened_at), not opened_at − 72 h
        F["card_window"] = await harness_query(self.session, self.tctx, "card_window", {"c": {"id": ctx.card_id}, "from_ts": _ts_add(flagged_ts, hours=-72), "to_ts": ctx.opened_at, "max_rows": 60})
        F["prior_cases_for_customer"] = await harness_query(self.session, self.tctx, "prior_cases_for_customer", {"cu": {"id": ctx.customer_id}, "as_of": ctx.opened_at})
        head = {
            "case_id": ctx.case_id,
            "trigger_type": ctx.trigger_type,
            "trigger_text": ctx.trigger_text,
            "flagged_txn_id": ctx.flagged_txn_id,
            "card_id": ctx.card_id,
            "customer_id": ctx.customer_id,
            "opened_at": ctx.opened_at,
            "risk_score": ctx.risk_score,
            "graph_case_id": self.graph_case_id,
        }
        note = ""
        if ctx.trigger_type == "customer_report":
            note = (
                f"The customer has already denied transaction {ctx.flagged_txn_id}: the denial is evidence (source customer, ref trigger) "
                "and it is the trigger, so R2 applies from intake (BLOCK_CARD + CREATE_CASE, FILE_REPORT when exposure > $1,000 or a "
                "shared device profile / another card's fraud connects); R1 governs only before a denial (README 3b). The one exception "
                "is R7: a charge that matches the customer's own recurring pattern is not blocked. Follow Playbook C."
            )
        elif ctx.trigger_type == "analyst_request":
            note = ("An analyst asked for related activity on a shared element: follow Playbook D. The analyst's request is not a "
                    "customer statement: cite it as source external, ref trigger.")
        else:
            note = f"Risk-score alert ({'in-person' if txn.get('channel') == 'in_person' else 'online'}): follow Playbook {'A' if txn.get('channel') == 'in_person' else 'B'}."
        brief = ["CASE:", json.dumps(head, sort_keys=True), note, "", "MANDATORY QUERY RESULTS (ts <= opened_at):"]
        for name in ("case_context", "card_profile", "card_window", "prior_cases_for_customer"):
            view = llm_view(name, F[name], ctx.opened_at, F)   # card statistics that include post-opening activity withheld
            brief.append(f"## {name}\n" + json.dumps(view, separators=(",", ":"), sort_keys=True, default=str)[: self.s.tool_result_max_chars // 2])
        return "\n".join(brief)

    # ------------------------------------------------------------------ P1
    async def p1_memory(self) -> str:
        cc = self.tctx.facts.get("case_context", {})
        txn, dev = cc.get("txn", {}) or {}, cc.get("device", {}) or {}
        q = (
            f"{self.ctx.trigger_type} alert; {txn.get('channel', '')} purchase; product {txn.get('product_cd', '')}; device_new {txn.get('device_new', '')}; "
            f"proxy {txn.get('proxy', '')}; region {txn.get('addr1', '')}; ring_hit {txn.get('ring_hit', False)}; burst {bool(txn.get('burst_id'))}"
        )
        # M5: same arguments as DuckFacts.collect's similar_prior_cases (device id of the flagged txn, not only strong profiles)
        sims = await tools_local.find_similar_cases(q, self.ctx, k=8, device_id=str(txn.get("device_id") or dev.get("id", "")), addr1=str(txn.get("addr1", "")), pattern_sig="")
        gq = {
            "risk_score": "verify before block on a weak signal; travel versus clone; new phone; R1 R3 R4 stopping rule",
            "customer_report": "customer denies a charge; recurring charge dispute; block and case; shared device report threshold R2 R7 R8 3a",
            "analyst_request": "shared device profile across several cards; undocumented coordinated pattern; report and monitor connected cards R6 R9 3a",
        }
        chunks = await tools_local.grounding_chunks(gq.get(self.ctx.trigger_type, "fraud policy rules"), doc_filter="", kind_filter="", k=5)
        pack = {
            "similar_prior_cases": sims.get("cases", [])[:8],
            "grounding": [{"id": c.get("id"), "section": c.get("section"), "text": (c.get("text") or "")[:500]} for c in chunks.get("chunks", [])[:5]],
        }
        self.log.record_phase("P1", pack)
        return "MEMORY PACK:\n" + json.dumps(pack, sort_keys=True, default=str)

    # ------------------------------------------------------------------ P2
    async def p2_investigate(self, brief: str, memory: str) -> None:
        instructions = _fill(self.prompts["investigate"], max_tool_calls=self.s.max_tool_calls)
        user = f"{instructions}\n\n{brief}\n\n{memory}"
        await self.llm.investigate(
            phase="P2", user_text=user, history=self.history, system=self.system, tools=self.tools,
            model=self.s.model, log=self.log, max_iterations=self.s.max_tool_calls + 2,
        )
        self.log.record_phase("P2", {"llm_tool_calls": self.tctx.budget.used, "facts": sorted(self.tctx.facts)})

    async def _ensure_facts(self) -> None:
        """Re-run the playbook queries the engine needs if the model skipped them.

        M5: this is the query list and the parameters of `engine.facts_duckdb.DuckFacts.collect`
        verbatim — the same set for **every** case, whatever the channel or trigger. `region_history`
        and `device_history` are called with `as_of = flagged_ts − 1 s` (D8: they exclude the flagged
        transaction); every other read uses `as_of = opened_at`.
        """
        ctx, F = self.ctx, self.tctx.facts
        txn = (F.get("case_context", {}) or {}).get("txn", {}) or {}
        as_of = ctx.opened_at
        flagged_ts = str(txn.get("ts") or as_of)
        before = _ts_add(flagged_ts, seconds=-1)  # D8: strictly prior to the flagged transaction
        c, t = {"id": ctx.card_id}, {"id": ctx.flagged_txn_id}
        need: list[tuple[str, dict]] = []
        if str(txn.get("addr1", "")):
            need.append(("region_history", {"c": c, "addr1": str(txn["addr1"]), "as_of": before}))
        if txn.get("device_id"):
            need += [
                ("device_history", {"c": c, "d": {"id": txn["device_id"]}, "as_of": before}),
                ("device_neighbors", {"d": {"id": txn["device_id"]}, "from_ts": _ts_add(flagged_ts, days=-30), "to_ts": as_of, "max_cards": 60}),
            ]
        need += [
            ("shared_origin_scan", {"c": c, "as_of": as_of, "days": 30}),
            ("card_testing_check", {"c": c, "as_of": as_of, "small": 5.0, "window_min": 60, "min_n": 3, "big": 100.0, "lookahead_h": 48}),
            ("under_threshold_burst", {"c": c, "as_of": as_of}),
            ("recurring_charge_check", {"t": t, "tol": 0.01, "as_of": as_of}),
            ("episode_candidates", {"t": t, "as_of": as_of, "gap_h": 48}),
            ("ring_profile", {"c": c, "as_of": as_of}),
        ]
        for name, params in need:
            try:
                # the harness's own call is authoritative even when the model already ran the query with
                # different parameters ("first call wins" in record_result would otherwise leak the model's)
                F[name] = await harness_query(self.session, self.tctx, name, params)
            except GraphUnavailable:
                raise           # the workspace is down: fail the case (bench --resume reruns it), never score missing facts
            except Exception as e:
                self.log.note(f"engine re-run of {name} failed", error=str(e)[:300])
        try:
            self.post_open = await run_query(self.session, "post_open_activity", {"c": {"id": ctx.card_id}, "opened_at": ctx.opened_at, "days": 7})
        except GraphUnavailable:
            raise
        except Exception as e:
            self.post_open = {}
            self.log.note("post_open_activity failed", error=str(e)[:300])

    # ------------------------------------------------------------------ P3
    async def p3_scorecard(self):
        await self._ensure_facts()
        sc = _norm_scorecard(self.E.scorecard.compute(self.ctx, self.tctx.facts, self.tctx.ledger))
        self.log.record_phase("P3", _to_jsonable(sc))
        return sc

    # ------------------------------------------------------------------ P4
    async def p4_assess(self, sc: Any) -> tuple[Any, list[dict], list[str]]:
        text = _fill(self.prompts["assess"], scorecard_json=json.dumps(self._sc_json(sc), sort_keys=True, default=str))
        a: Assessment = await self._parse("P4", Assessment, text)
        lo, hi = max(0.0, sc.p_engine - 0.10), min(1.0, sc.p_engine + 0.10)
        p = min(max(float(a.fraud_probability), lo), hi)
        if p != a.fraud_probability:
            self.log.note("fraud_probability clamped to p_engine ± 0.10", llm=a.fraud_probability, clamped=p)
        if p != sc.p_engine:
            sc.adjustments.append((f"LLM adjustment ({a.calibration_basis[:120]})", round(p - sc.p_engine, 3)))
            sc.p_engine = round(p, 3)
            new_v = "fraud" if p >= 0.70 else "legitimate" if p <= 0.15 else "uncertain"
            if sc.flags.get("denial") and new_v == "legitimate":
                new_v = "uncertain"
            if new_v != sc.verdict:
                self.log.note("verdict band changed by LLM adjustment", old=sc.verdict, new=new_v)
                sc.verdict = new_v
                if new_v == "legitimate":
                    sc.pattern, sc.pattern_description, sc.episode_ids, sc.first_suspicious_txn_id, sc.exposure_usd = "none", "", [], "", 0.0
                    sc.connected_card_ids, sc.connected_device_profiles = [], []
        known = self._known_ids()
        evidence = self._clean_evidence(a.evidence, known)
        if self.ctx.trigger_type == "analyst_request":
            # an analyst's request is not a customer statement (README sources: graph | document | customer | external)
            for e in evidence:
                if e["source"] == "customer":
                    e["source"] = "external"
                    if not str(e.get("ref", "")).startswith(("trigger", "evidence_request")):
                        e["ref"] = "trigger"
                    self.log.note("analyst-request evidence relabelled source customer -> external", ref=e["ref"])
        if self.ctx.trigger_type == "customer_report" and not any(e["source"] == "customer" for e in evidence):
            evidence.insert(
                0,
                {
                    "claim": f"Customer denied transaction {self.ctx.flagged_txn_id} in the alert message: '{self.ctx.trigger_text[:120]}'.",
                    "source": "customer",
                    "ref": "trigger",
                    "entity_ids": [self.ctx.flagged_txn_id],
                },
            )
        for e in evidence:  # every claim also lands in the engine ledger (direction from the verdict context is neutral)
            self.tctx.ledger.append(
                Evidence(claim=e["claim"], source=e["source"], ref=e["ref"], entity_ids=e["entity_ids"], family=_family(e), direction="neutral")
            )
        # M22: `fraud_probability` at the top level of the payload (ui/common.probability_pre_post)
        self.log.record_phase(
            "P4",
            {
                "assessment": a.model_dump(),
                "fraud_probability": sc.p_engine,
                "p_engine_after": sc.p_engine,
                "verdict": sc.verdict,
                "families_fraud": sorted(sc.families_fraud),
                "families_legit": sorted(sc.families_legit),
            },
        )
        return sc, evidence, list(a.wanted_requests)

    # ------------------------------------------------------------------ P5 / P7
    def _rule_hints(self, adm: dict, sc: Any) -> dict[str, str]:
        """{action: the Fraud Policy rule its reason must cite} for every action the gate allows."""
        cites = adm.get("citations", {}) or {}
        return {a: policy_cite(a, list(cites.get(a, []) or []), sc) for a in adm.get("allowed", []) or []}

    def _fix_reasons(self, actions: list[dict], adm: dict, sc: Any) -> list[dict]:
        """Deterministic last step (§7): a reason that cites an internal gate label gets the README rule in its
        place; a reason that names no rule is prefixed with the rule the gate applied."""
        hints = self._rule_hints(adm, sc)
        for a in actions:
            if not reason_problems([a]):
                continue
            cite = hints.get(a["action"]) or policy_cite(a["action"], [], sc)
            r = str(a.get("reason", "") or "")
            for h in HOUSE_RULE_NAMES:
                r = re.sub(rf"(?<![\w-]){re.escape(h)}(?![\w-])", cite, r)
            if reason_problems([dict(a, reason=r)]):
                r = f"{cite}: {r}" if r else f"{cite}: required by policy"
            self.log.note("action reason given its policy citation", action=a["action"], before=a.get("reason", "")[:160], after=r[:160])
            a["reason"] = r
        return actions

    async def _decide(self, phase: str, stage: str, sc: Any, post_note: str = "") -> list[dict]:
        adm = self.E.policy.admissible(self.ctx, sc, stage)
        text = _fill(
            self.prompts["nba"],
            phase=phase,
            stage=stage,
            p=sc.p_engine,
            verdict=sc.verdict,
            families_fraud=sorted(sc.families_fraud),
            families_legit=sorted(sc.families_legit),
            exposure=f"{sc.exposure_usd:,.2f}",
            post_evidence_note=post_note,
            admissible_json=json.dumps(adm, sort_keys=True),
            rule_hints=json.dumps(self._rule_hints(adm, sc), sort_keys=True),
        )
        choice: ActionChoice = await self._parse(phase, ActionChoice, text)
        actions = self._actions_dicts(choice)
        violations = self.E.policy.check(actions, self.ctx, sc, stage)
        cite_problems = reason_problems(actions)
        if violations or cite_problems:
            self.log.note(f"{phase}: policy violations, re-prompting", violations=violations + cite_problems)
            choice = await self._parse(
                phase + "-retry", ActionChoice,
                "Your action list violates the policy gate:\n- " + "\n- ".join(violations + cite_problems)
                + "\nReply with a corrected ActionChoice inside the admissible set; every reason cites the Fraud Policy rule "
                  "(R1-R10, 3a, 3b, §5, §6) given for that action in RULE TO CITE, never an internal label such as fraud_band.",
            )
            actions = self._actions_dicts(choice)
            violations = self.E.policy.check(actions, self.ctx, sc, stage)
        if violations:
            actions = self._correct(actions, adm, sc)
            self.events.append({"kind": "policy_override", "payload": {"stage": stage, "violations": violations, "corrected": actions}})
            self.log.note(f"{phase}: deterministic policy correction applied", violations=violations)
        actions = self._fix_reasons(actions, adm, sc)
        actions = self.E.policy.order(actions)
        for a in actions:
            a["route"] = self.E.policy.route(a["action"], sc.exposure_usd)
        # M22: P7 carries the post-evidence probability at the top level (ui/common.probability_pre_post reads it)
        self.log.record_phase(phase, {"stage": stage, "admissible": adm, "actions": actions, "fraud_probability": sc.p_engine, "verdict": sc.verdict})
        return actions

    def _request_type_for(self, sc: Any) -> str:
        """`engine.voi.request_type_for`; the stub engine has no such function, so fall back to the
        action the policy would put in the initial set."""
        fn = getattr(self.E.voi, "request_type_for", None)
        if callable(fn):
            try:
                return fn(self.ctx, sc)
            except Exception as e:
                self.log.note("voi.request_type_for raised", error=str(e)[:200])
        if self.ctx.trigger_type == "analyst_request":
            return "analyst_info"
        online = (self.tctx.facts.get("case_context", {}).get("txn", {}) or {}).get("channel") == "online"
        return "step_up_auth" if online and sc.flags.get("denial") else "customer_validation"

    def _correct(self, actions: list[dict], adm: dict, sc: Any) -> list[dict]:
        by_name = {a["action"]: a for a in actions}
        out: list[dict] = []
        for r in adm["required"]:
            a = by_name.get(r) or {"action": r, "route": adm["routes"].get(r, "auto"),
                                   "reason": policy_cite(r, list(adm["citations"].get(r, []) or []), sc) + ": required by policy (deterministic correction)"}
            out.append(a)
        for a in actions:
            if a["action"] in adm["allowed"] and a["action"] not in adm["required"]:
                out.append(a)
        for a in out:
            a["route"] = adm["routes"].get(a["action"], a.get("route", "auto"))
            if _R1_CLAIM.search(a.get("reason", "")) and not (sc.p_engine < 0.70 and len(sc.families_fraud) < 2):
                a["reason"] = _R1_CLAIM.sub("§3b/§5", a["reason"])
        return out

    # ------------------------------------------------------------------ P6
    async def p6_evidence(self, sc: Any, rt: str, branches: dict) -> tuple[list[dict], Any]:
        """Simulate exactly the one request type the engine chose (`engine.voi.request_type_for`).

        M5: the decision to ask and the request type are taken in `_run` before P5 so the initial
        recommendation already names the matching verification action; P6 only plays the reply out.
        """
        rep = self.E.simulator.reply(rt, self.ctx, sc)
        request = {"type": rt, "asked_after_step": self.log.tool_calls, "assumed_response": rep["assumed_response"]}
        requests = [request]
        self.evidence.append(
            {
                "claim": rep["assumed_response"].replace("ASSUMED (simulated): ", "Assumed reply: "),
                "source": "customer" if rt == "customer_validation" else "external",
                "ref": "evidence_request:1",
                "entity_ids": [self.ctx.flagged_txn_id] if rt != "analyst_info" else [],
            }
        )
        sc_post = _norm_scorecard(self.E.scorecard.post_evidence(sc, rep["outcome"]))
        sc_post.flags["asked"] = True
        sc_post.flags["request_seq"] = 1
        sc_post.flags["post_outcome"] = rep["outcome"]
        sc_post.flags["counterfactual"] = rep["counterfactual"]
        self.events += [{"kind": "evidence_request", "payload": request}, {"kind": "assumed_response", "payload": rep}]
        # M22: the shape ui/common.py reads (counterfactual / voi branches / post-evidence probability)
        self.log.record_phase(
            "P6", {"request": request, "simulated": rep, "voi": {"request_type": rt, "branches": branches}, "counterfactual": rep["counterfactual"], "fraud_probability": sc_post.p_engine}
        )
        self.history.append(
            {
                "role": "user",
                "content": (
                    f"EVIDENCE REQUEST 1 ({rt}) was made after step {request['asked_after_step']}. Assumed reply (simulated): "
                    f"{rep['assumed_response']}\nOutcome class: {rep['outcome']}. Probability moved {sc.p_engine} -> {sc_post.p_engine}; "
                    f"verdict now {sc_post.verdict}. Counterfactual: {rep['counterfactual']}."
                ),
            }
        )
        return requests, sc_post

    # ------------------------------------------------------------------ P8
    def _sar_resolver(self):
        """Id resolver for `rag.validate_sar` (D10): the dataset DuckDB when it is there, else the engine
        facts DB, else the answer's own ids (`rag.validate_sar.answer_resolver` is applied by the caller)."""
        try:
            from rag.validate_sar import DuckResolver

            if self.s.duckdb_path.exists():
                return DuckResolver(self.s.duckdb_path)
        except Exception as e:
            self.log.note("SAR DuckResolver unavailable", error=str(e)[:200])
        try:
            from engine.validator import FactsResolver

            db = _facts_db()
            if db is not None:
                return FactsResolver(db)
        except Exception as e:
            self.log.note("SAR FactsResolver unavailable", error=str(e)[:200])
        return None

    def _sar_facts(self, sc: Any, final: list[dict], reason: str, ofac: dict):
        """`rag.sar.SarFacts` for this case, built from the fact pack the harness collected."""
        from rag.sar import SarFacts

        f = sc.flags
        ts_of = self._ts_lookup()
        chain = {str(m.get("id")): dict(m) for m in sc.chain}
        for r in f.get("ring_rows", []) or []:
            chain.setdefault(
                str(r["id"]), {"id": r["id"], "ts": r.get("ts", ""), "amt": r.get("amt", 0.0), "channel": "online", "addr1": "", "device_new": "New", "device_id": f.get("ring_id", ""), "proxy": ""}
            )
        affected = []
        for i in sc.episode_ids:
            row = chain.get(str(i)) or {"id": str(i), "ts": ts_of.get(str(i), ""), "amt": 0.0}
            row.setdefault("ts", ts_of.get(str(i), ""))
            affected.append(row)
        affected.sort(key=lambda r: (str(r.get("ts", "")), str(r.get("id", ""))))
        card = (self.tctx.facts.get("card_profile", {}) or {}).get("card", {}) or {}
        prior = (self.tctx.facts.get("card_profile", {}) or {}).get("prior_closed_cases", []) or []
        reply = ""
        for e in self.evidence:
            if str(e.get("ref", "")).startswith("evidence_request"):
                reply = e["claim"]
        shared = ""
        if f.get("ring_hit"):
            shared = f"a profile that appears on {len(f.get('wave_cards', []) or [])} other cards in the same wave"
        elif sc.connected_card_ids:
            shared = f"a profile shared with {len(sc.connected_card_ids)} other cards carrying confirmed fraud within 30 days"
        return SarFacts(
            case_id=self.ctx.case_id,
            graph_case_id=self.graph_case_id,
            customer_id=self.ctx.customer_id,
            card_id=self.ctx.card_id,
            opened_at=self.ctx.opened_at,
            verdict=sc.verdict,
            fraud_probability=float(sc.p_engine),
            pattern=sc.pattern,
            pattern_description=sc.pattern_description,
            affected=affected,
            exposure_usd=float(sc.exposure_usd),
            connected_card_ids=list(sc.connected_card_ids),
            connected_device_profiles=list(sc.connected_device_profiles),
            card_type=str(card.get("card_type", "")),
            card_network=str(card.get("card_network", "")),
            # as of the flagged transaction, never the Card vertex's whole-history n_txns / max_amt / n_online ...
            baseline={k: v for k, v in self._baseline_as_of().items() if k in ("n_txns", "median_amt", "max_amt", "modal_region", "first_ts")},
            prior_cases=[{"id": c.get("id"), "pattern": c.get("pattern"), "outcome": c.get("outcome"), "report_filed": c.get("report_filed"), "opened_at": c.get("opened_at")} for c in prior],
            customer_reply=reply,
            ofac=ofac,
            final_actions=[dict(a) for a in final],
            sar_reason=reason,
            shared_element=shared,
            trigger_type=self.ctx.trigger_type,
        )

    def _baseline_as_of(self) -> dict:
        """Card statistics as of the flagged transaction (strictly before it), from case_context.txn: card_seq is the
        transaction's 1-based position on the card, prior_med_amt / prior_max_amt are over the earlier ones
        (-1 when there are none). The Card vertex's n_txns / median_amt / max_amt / n_online / n_in_person are
        whole-history (they include activity after opened_at) and are never used in a report."""
        cc = self.tctx.facts.get("case_context", {}) or {}
        txn = cc.get("txn", {}) or {}
        prof = self.tctx.facts.get("card_profile", {}) or {}
        card = prof.get("card", {}) or {}
        out: dict[str, Any] = {}
        try:
            seq = int(txn.get("card_seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        if seq > 0:
            out["n_txns"] = seq - 1
            out["as_of"] = f"before the flagged transaction {self.ctx.flagged_txn_id} ({txn.get('ts', '')})"
            for key, src in (("median_amt", "prior_med_amt"), ("max_amt", "prior_max_amt")):
                try:
                    v = float(txn.get(src, -1))
                except (TypeError, ValueError):
                    v = -1.0
                if v >= 0 and seq > 1:
                    out[key] = round(v, 2)
        regions = prof.get("regions", []) or []            # as-of heap (ts <= opened_at)
        if regions and isinstance(regions[0], dict) and regions[0].get("addr1"):
            out["modal_region"] = str(regions[0]["addr1"])
            out["top_regions_as_of_opening"] = [{"addr1": r.get("addr1"), "n": r.get("n")} for r in regions[:3] if isinstance(r, dict)]
        devices = prof.get("devices", []) or []
        if devices:
            out["top_devices_as_of_opening"] = [{"device_id": d.get("device_id"), "n": d.get("n"), "first_ts": d.get("first_ts")}
                                                for d in devices[:3] if isinstance(d, dict)]
        if prof.get("last30"):
            out["last_30_days_as_of_opening"] = prof["last30"]
        if card.get("first_ts"):
            out["first_ts"] = card["first_ts"]
        # a time-boxed card_profile (the as-of query) may also be quoted: its count / median / maximum are as of opening
        from agent.mcp_client import card_profile_stale

        if card and not card_profile_stale(prof, self.ctx.opened_at):
            for key, src in (("n_txns_at_opening", "n_txns"), ("median_amt_at_opening", "median_amt"), ("max_amt_at_opening", "max_amt")):
                if card.get(src) not in (None, ""):
                    out[key] = card[src]
        return out

    def _prior_reports(self, facts: Any) -> tuple[list[str], list[str]]:
        """(this card, related) CC- ids with a report filed: the card's own closed cases; the closed cases on the
        ring / device profile (ring_profile) plus any the evidence claims were reported."""
        from rag.validate_sar import reported_cases_from_evidence

        def filed(v: Any) -> bool:
            return str(v).lower() in ("true", "yes", "1")

        card = [str(c.get("id")) for c in facts.prior_cases if filed(c.get("report_filed")) and c.get("id")]
        rel = [str(c.get("id")) for c in ((self.tctx.facts.get("ring_profile", {}) or {}).get("closed_cases", []) or [])
               if isinstance(c, dict) and filed(c.get("report_filed")) and c.get("id") and c.get("card_id") != self.ctx.card_id]
        ev_card, ev_rel = reported_cases_from_evidence(self.evidence, self.ctx.card_id)
        card += [i for i in ev_card if i not in card]
        rel += [i for i in ev_rel if i not in rel and i not in card]
        return card, rel

    def _sar_known_devices(self, facts: Any) -> list[str]:
        devs = list(facts.connected_device_profiles) + [str(t.get("device_id")) for t in facts.affected if t.get("device_id")]
        devs += [str(i) for e in self.evidence for i in (e.get("entity_ids", []) or []) if " | " in str(i)]
        return [d for d in dict.fromkeys(devs) if " | " in d]

    def _sar_case_json(self, facts: Any, reports: tuple[list[str], list[str]]) -> dict:
        """CASE DATA for the P8 prompt: what happened, what is only recommended, and statistics as of opening."""
        from rag import sar as rag_sar

        def status(a: dict) -> str:
            if a.get("action") == "FILE_REPORT":
                return "this report: recommended, awaiting fraud-manager (L2) approval"
            if a.get("route") == "auto":
                return "done by the agent (auto route)"
            who = "team-lead (L1)" if a.get("route") == "L1" else "fraud-manager (L2)"
            return f"recommended, NOT yet done: awaiting {who} approval"

        card_r, rel_r = reports
        if card_r:
            card_note = f"a prior suspicious activity report was filed on this card under closed case {', '.join(card_r)}"
        elif facts.prior_cases:
            card_note = f"no prior suspicious activity report has been filed on this card ({len(facts.prior_cases)} earlier closed case(s), none reported)"
        else:
            card_note = "no prior suspicious activity report has been filed on this card"
        rel_note = (f"closed cases {', '.join(rel_r)} on the same device profile or connected cards were confirmed fraud with reports filed"
                    if rel_r else "")
        ofac = facts.ofac or {}
        ofac_note = ("OFAC screening of the customer and card identifiers against the SDN list returned no match" if not ofac.get("matches")
                     else f"OFAC screening returned a possible SDN match ({ofac['matches'][0]['name']}, score {ofac.get('best_score')}) under review")
        txns = [{k: t.get(k) for k in ("id", "ts", "amt", "channel", "product_cd", "addr1", "device_id", "device_new", "proxy") if t.get(k) not in (None, "")}
                for t in facts.affected]
        base = self._baseline_as_of()
        return {
            "graph_case_id": facts.graph_case_id, "alert_id": facts.case_id, "opened": facts.opened_at[:10],
            "customer_id": facts.customer_id, "card_id": facts.card_id, "card_type": facts.card_type, "card_network": facts.card_network,
            "typology": rag_sar.TYPOLOGY.get(facts.pattern, rag_sar.TYPOLOGY["none"]), "pattern": facts.pattern,
            "pattern_description": facts.pattern_description, "trigger_type": facts.trigger_type,
            "transactions": txns, "affected_txn_ids": [str(t.get("id")) for t in facts.affected],
            "activity_dates": rag_sar.activity_dates(facts.affected), "exposure_usd": round(facts.exposure_usd, 2),
            "connected_card_ids": list(facts.connected_card_ids), "connected_device_profiles": list(facts.connected_device_profiles),
            "shared_element": facts.shared_element,
            "baseline_as_of_opening": base, "median_amt": base.get("median_amt", 0),
            "prior_reports": {"this_card": card_r, "device_profile_or_connected_cards": rel_r},
            "prior_sar_note": card_note[0].upper() + card_note[1:] + (f"; {rel_note}" if rel_note else ""),
            "ofac_note": ofac_note, "customer_reply": facts.customer_reply,
            "similar_prior_cases": [c for c in (getattr(self, "_sar_similar", []) or []) if str(c).startswith("CC-")],
            "actions": [{"action": a.get("action"), "route": a.get("route"), "status": status(a)} for a in facts.final_actions],
            "sar_reason": facts.sar_reason,
        }

    @staticmethod
    def _complete_subjects(sar: dict, drafted: list[str], known_devices: list[str], lead: tuple[str, str]) -> dict:
        """subjects = every customer id, card id and known device-profile string the narrative names (README),
        customer and card first. The model's own list is kept only where the narrative names the id verbatim."""
        from rag.validate_sar import CARD_ID, CUST_ID

        nar = sar.get("narrative", "")
        subs = [s for s in lead if s and s in nar]
        cands = list(sar.get("subjects", []) or []) + list(drafted or []) + CUST_ID.findall(nar) + CARD_ID.findall(nar)
        cands += [d for d in known_devices if d in nar]
        for s in cands:
            s = str(s)
            if s and s in nar and s not in subs and (CARD_ID.fullmatch(s) or CUST_ID.fullmatch(s) or " | " in s):
                subs.append(s)
        sar["subjects"] = subs
        return sar

    async def p8_sar(self, sc: Any, final: list[dict]) -> dict:
        """M15 / M16 / D10: grounded on the two FinCEN documents by id, drafted from agent/prompts/sar.md (actions with
        their approval status, card statistics as of opening, prior reports per scope), assembled by
        `rag.sar.assemble`, subjects completed from the narrative, and checked by `rag.validate_sar.validate_sar` with
        the fact checks V14-V19 — a failed check re-prompts; after three failed drafts the deterministic
        `rag.sar.fallback_narrative` is used."""
        from rag import sar as rag_sar
        from rag.validate_sar import validate_sar

        file_, reason = self.E.policy.sar_required(sc)
        file_ = file_ and "FILE_REPORT" in {a["action"] for a in final}
        if not file_:
            return rag_sar.negative_sar(reason)
        ofac = tools_local.ofac_screen(self.ctx.customer_id)
        ofac = dict(ofac, best_score=max([m["score"] for m in ofac.get("matches", [])] or [0.0]))
        facts = self._sar_facts(sc, final, reason, ofac)
        self._sar_similar = list(sc.similar_prior_cases or [])
        # M15: the Document ids are `sar_guidance_narrative` (the narrative guidance) and `sar_tti_19`
        # (the effective / less-effective narrative examples); an unknown filter searches everything.
        chunks: list[dict] = []
        for doc, q in (
            ("sar_guidance_narrative", "SAR narrative: who what when where how why; supporting documentation"),
            ("sar_tti_19", "example of an effective and a less effective SAR narrative"),
        ):
            try:
                got = await tools_local.grounding_chunks(q, doc_filter=doc, kind_filter="", k=3)
                chunks += list(got.get("chunks", []) or [])
            except Exception as e:
                self.log.note(f"grounding_chunks({doc}) failed", error=str(e)[:200])
        for c in chunks:
            c.setdefault("doc_id", str(c.get("id", "")).split("#")[0])
            c.setdefault("section", "")
            c.setdefault("page", 0)
        resolver = self._sar_resolver()
        affected_ts = [str(t.get("ts", "")) for t in facts.affected if t.get("ts")]
        reports = self._prior_reports(facts)
        known_devices = self._sar_known_devices(facts)
        base = self._baseline_as_of()
        fact_context = {
            "final_actions": [dict(a) for a in final], "known_devices": known_devices,
            "prior_reports_card": reports[0], "prior_reports_related": reports[1],
            "baseline_as_of": ({"n_prior_txns": base["n_txns"], "max_amt": base.get("max_amt"), "median_amt": base.get("median_amt"),
                                "n_txns_at_opening": base.get("n_txns_at_opening"), "max_amt_at_opening": base.get("max_amt_at_opening"),
                                "median_amt_at_opening": base.get("median_amt_at_opening")}
                               if "n_txns" in base else None),
            "affected_amounts": [float(t.get("amt", 0) or 0) for t in facts.affected],
        }
        case_json = self._sar_case_json(facts, reports)
        excerpts = "\n\n".join(f"[{c['doc_id']}#{c['section']} p.{c.get('page', 0)}]\n{c.get('text', '')}" for c in chunks[:5]) or "(none retrieved)"
        text = _fill(self.prompts["sar"], sar_reason=reason, graph_case_id=facts.graph_case_id, customer_id=facts.customer_id,
                     card_id=facts.card_id, prior_sar_note=case_json["prior_sar_note"], ofac_note=case_json["ofac_note"],
                     exposure=f"{facts.exposure_usd:,.2f}", chunks=excerpts, case_json=json.dumps(case_json, indent=1, default=str))

        def check(cand: dict) -> list[str]:
            return validate_sar(cand, exposure_usd=facts.exposure_usd, affected_ts=affected_ts,
                                case_ids=(self.ctx.case_id, self.graph_case_id), customer_id=self.ctx.customer_id,
                                card_id=self.ctx.card_id, resolver=resolver, file_report_in_final=True, fact_context=fact_context)

        lead = (self.ctx.customer_id, self.ctx.card_id)
        sar: dict | None = None
        problems: list[str] = []
        for attempt in range(3):
            d: SarDraft = await self._parse(
                "P8" if attempt == 0 else f"P8-retry{attempt}",
                SarDraft,
                text if attempt == 0 else ("The narrative failed validation: " + "; ".join(problems)
                                           + ". Rewrite the SarDraft from the CASE DATA, fixing every problem listed."),
                model=self.s.sar_model,
            )
            cand = self._complete_subjects(rag_sar.assemble(facts, d.narrative, reason), list(d.subjects), known_devices, lead)
            problems = check(cand)
            if not problems:
                sar = cand
                break
            self.log.note("SAR validation failed", attempt=attempt + 1, problems=problems)
        if sar is None:
            nar = rag_sar.fallback_narrative(facts)
            sar = self._complete_subjects(rag_sar.assemble(facts, nar, reason), [], known_devices, lead)
            self.log.note("SAR: rag.sar.fallback_narrative used after three failed drafts", residual=check(sar))
        self.events.append({"kind": "sar", "payload": {"reason": reason, "ofac": ofac, "chunks": [c.get("id") for c in chunks],
                                                       "prior_reports": {"card": reports[0], "related": reports[1]}}})
        return sar

    def _ts_lookup(self) -> dict[str, str]:
        """TransactionID -> ts from every fact row seen (chain, card_window, ring_profile, bursts...)."""
        out: dict[str, str] = {}

        def walk(o: Any):
            if isinstance(o, dict):
                if "id" in o and "ts" in o and isinstance(o.get("ts"), str) and str(o["id"]).isdigit():
                    out.setdefault(str(o["id"]), o["ts"])
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        for rows in self.tctx.facts_all.values():
            walk(rows)
        return out

    def _memory_exclusions(self, ctx: CaseContext) -> set[str]:
        """AgentCase ids case memory must never return for this case: its own AC-<case_id> (written by an earlier
        run) and the agent case of every exam case opened at or after this one (some query rows, e.g.
        device_neighbors.agent_cases, carry no opened_at to filter on)."""
        out = {f"AC-{ctx.case_id}"}
        try:
            with self.s.case_pack_csv.open(encoding="utf-8", newline="") as fh:
                for r in csv.DictReader(fh):
                    if r.get("case_id") and str(r.get("opened_at", "")) >= ctx.opened_at:
                        out.add(f"AC-{r['case_id']}")
        except OSError:
            pass
        return out

    # ------------------------------------------------------------------ run
    async def run_case(self, ctx: CaseContext, log: RunLog) -> dict:
        self.ctx, self.log = ctx, log
        token = current_runlog.set(log)
        try:
            return await self._run(ctx)
        finally:
            current_runlog.reset(token)

    async def _run(self, ctx: CaseContext) -> dict:
        self.llm.new_case()          # cli backend: one Claude CLI session per case
        self.history: list[dict] = []
        self.events: list[dict] = []
        self.evidence: list[dict] = []
        self.post_open: dict = {}
        excluded = self._memory_exclusions(ctx)
        self.tctx = ToolContext(opened_at=ctx.opened_at, exclude_case_ids=frozenset(excluded))
        self.tctx.budget.limit = self.s.max_tool_calls
        tools_local.bind(self.session, self.tctx)
        self.tools = make_query_tools(self.session, self.tctx, self.descriptions) + tools_local.make_local_tools(ctx, self.tctx)
        self.tools.sort(key=lambda t: t.name)  # deterministic tool order (cache prefix)
        self.tool_defs = [t.to_dict() for t in self.tools]

        self.log.set_phase("P0")
        brief = await self.p0_intake(ctx)  # P0
        self.log.set_phase("P1")
        memory = await self.p1_memory()  # P1
        self.log.set_phase("P2")
        await self.p2_investigate(brief, memory)  # P2
        if self.tctx.graph_unavailable:  # a P2 tool call hit an unreachable workspace: its absence is not evidence
            raise GraphUnavailable(f"graph unavailable during P2 ({self.tctx.graph_unavailable})")
        self.events.append({"kind": "evidence", "payload": [_to_jsonable(e) for e in self.tctx.ledger]})
        self.log.set_phase("P3")
        sc = await self.p3_scorecard()  # P3
        self.log.set_phase("P4")
        sc, self.evidence, wanted = await self.p4_assess(sc)  # P4
        self.events.append({"kind": "assessment", "payload": _to_jsonable(sc)})
        # M5: the §6 test-1 gate and the value-of-information test run BEFORE P5, exactly as engine.run_cases.run_one
        # does, so the initial recommendation already knows whether a verification request is part of it and which one.
        settled = bool(getattr(self.E.stop, "settled_pre", lambda _s: False)(sc))
        rt, ask, branches, voi_zero = "", False, {}, False
        if not settled:
            rt = self._request_type_for(sc)
            ask, branches = self.E.voi.should_ask(self.ctx, sc, rt)
            # D1: a fraud-band case that §6 test 1 has not settled still makes its one §3b verification request
            if not ask and sc.verdict == "fraud" and ctx.trigger_type != "analyst_request":
                ask, voi_zero = True, True
            if ask:
                sc.flags["asked"] = True
                sc.flags["request_type"] = rt
            else:
                voi_zero = True
        self.log.record_phase("P5-voi", {"settled_pre": settled, "request_type": rt, "ask": ask, "voi_zero": voi_zero, "branches": branches, "llm_wanted": list(wanted)})
        initial = await self._decide("P5", "initial", sc)  # P5
        self.events.append({"kind": "nba_initial", "payload": initial})
        p_pre, verdict_pre = float(sc.p_engine), sc.verdict   # before P6 (post_evidence may update sc in place)
        if ask:  # P6 — only the request type the engine chose
            requests, sc_post = await self.p6_evidence(sc, rt, branches)
        else:
            requests, sc_post = [], None
        if requests:  # P7
            sc_final = sc_post
            final = await self._decide("P7", "final", sc_final, post_note=f"Post-evidence: assumed {requests[0]['type']} outcome '{sc_post.flags.get('post_outcome')}'.")
            what_changed = self._what_changed(requests[0], sc, sc_post, initial, final, p_pre, verdict_pre)
            if what_changed == "nothing":  # same actions and routes: keep initial verbatim so final == initial exactly
                final = [dict(a) for a in initial]
        else:
            sc_final, final, what_changed = sc, [dict(a) for a in initial], "nothing"
        self.events.append({"kind": "nba_final", "payload": final})
        stop = self.E.stop.stop_reason(sc, sc_post, bool(requests), voi_zero)
        self.log.set_phase("P8")
        sar = await self.p8_sar(sc_final, final)  # P8
        final_json = {
            "verdict": sc_final.verdict,
            "fraud_probability": sc_final.p_engine,
            "pattern": sc_final.pattern,
            "affected_txn_ids": sc_final.episode_ids,
            "exposure_usd": sc_final.exposure_usd,
            "families_fraud": sorted(sc_final.families_fraud),
            "families_legit": sorted(sc_final.families_legit),
            "evidence_requests": requests,
            "initial": initial,
            "final": final,
            "what_changed": what_changed,
            "similar_prior_cases": sc_final.similar_prior_cases,
            "sar_file": sar["file"],
            "post_open_activity": self.post_open.get("summary", {}),
            "counterfactual": (sc_post.flags.get("counterfactual") if sc_post else ""),
        }
        closing: Closing = await self._parse("P9", Closing, _fill(self.prompts["explain"], engine_stop_reason=stop, final_json=json.dumps(final_json, sort_keys=True, default=str)))
        closing = await self._short_summary(closing)
        closing_d = closing.model_dump()
        closing_d["stop_reason"] = closing.stop_reason if "§6" in closing.stop_reason else f"{stop} {closing.stop_reason}".strip()
        closing_d["what_changed"] = what_changed
        # P10 persist before the file is finalised so written_to_graph is truthful
        self.log.set_phase("P10")
        from agent import persist

        written = await persist.persist_case(self, ctx, sc_final, initial, final, requests, sar, closing_d, self.evidence)
        closing_d["written_to_graph"] = written
        ev_objs = [
            Evidence(claim=e["claim"], source=e["source"], ref=e["ref"], entity_ids=list(e["entity_ids"]), family=_family(e), direction="neutral")
            for e in self.evidence
        ]
        if requests and sc_post is not None:
            sc_post.flags["pending"] = sc_post.flags.get("post_outcome") in ("no_reply", "inconclusive")
        try:
            answer = build_answer(ctx, sc, sc_post or sc, initial, final, ev_objs, requests, sar, closing_d, self.log.totals(), self.graph_case_id)
            builder = BUILDER_SOURCE
        except Exception as e:  # the engine builder asserts invariants; keep the file, log the reason
            self.log.note(f"{BUILDER_SOURCE}.build failed; using agent.answer_fallback", error=str(e)[:400])
            answer = build_answer_fallback(ctx, sc, sc_post, initial, final, ev_objs, requests, sar, closing_d, self.log.totals(), self.graph_case_id)
            builder = "agent.answer_fallback"
        answer["case"]["written_to_graph"] = written
        answer["case"]["graph_case_id"] = self.graph_case_id if written else ""
        answer["next_best_actions"]["what_changed"] = what_changed
        problems = check_invariants(answer)
        if validate_answer is not None:
            try:
                db = _facts_db()
                if db is not None:
                    meta = {"p_pre": sc.p_engine, "families_fraud_pre": len(sc.families_fraud), "opened_at": ctx.opened_at, "card_id": ctx.card_id, "customer_id": ctx.customer_id}
                    problems += validate_answer(answer, db, meta)  # agent.validator and engine.validator share the signature
                else:
                    self.log.note("validator skipped: engine facts DuckDB not found")
            except Exception as e:
                self.log.note("validator raised", error=str(e)[:300])
        self.log.note("answer assembled", builder=builder, validator=VALIDATOR_SOURCE)
        if problems:
            self.log.note("answer validation problems", problems=problems)
        answer["_validation"] = problems
        (self.log.root / "answer.json").write_text(json.dumps({k: v for k, v in answer.items() if k != "_validation"}, indent=2, ensure_ascii=False), encoding="utf-8")
        (self.log.root / "sar.md").write_text((sar["narrative"] or f"No SAR. {sar['reason']}") + "\n", encoding="utf-8")
        self.log.record_phase("P9", {"validation": problems, "totals": self.log.totals()})
        return answer

    async def _short_summary(self, closing: Closing) -> Closing:
        """README: the summary is 'two to six sentences' and 'short' (the evidence list carries the detail). One
        re-prompt on a violation, then a deterministic trim to the leading sentences that fit."""
        problems = summary_problems(closing.summary)
        if not problems:
            return closing
        self.log.note("P9: summary too long, re-prompting", problems=problems)
        retry: Closing = await self._parse(
            "P9-retry", Closing,
            "The summary fails the README format: " + "; ".join(problems) + f". Rewrite the Closing with a summary of two to four "
            f"sentences and under {SUMMARY_MAX_CHARS - 150} characters: verdict and probability, pattern, episode and exposure, the "
            "decisive evidence, and the recommended action with its route. Keep stop_reason and similar_prior_cases_used.",
        )
        if not summary_problems(retry.summary):
            return retry
        sents = text_sentences(retry.summary if len(retry.summary) < len(closing.summary) else closing.summary)
        keep: list[str] = []
        for x in sents:
            if len(keep) >= 6 or (len(keep) >= 2 and len(" ".join(keep + [x])) > SUMMARY_MAX_CHARS):
                break
            keep.append(x)
        trimmed = " ".join(keep)
        if len(trimmed) > SUMMARY_MAX_CHARS:            # two very long sentences: cut at the last word boundary
            trimmed = trimmed[: SUMMARY_MAX_CHARS - 1].rsplit(" ", 1)[0].rstrip(",;:") + "."
        self.log.note("P9: summary trimmed deterministically", before=len(closing.summary), after=len(trimmed))
        return closing.model_copy(update={"summary": trimmed})

    def _what_changed(self, req: dict, sc: Any, sc_post: Any, initial: list[dict], final: list[dict],
                      p_pre: float | None = None, verdict_pre: str | None = None) -> str:
        """README: one or two sentences on why `final` differs from `initial`, or "nothing". Never "from X to X":
        an unchanged probability / verdict is said to have stayed."""
        ini, fin = [(a["action"], a["route"]) for a in initial], [(a["action"], a["route"]) for a in final]
        if ini == fin:
            return "nothing"
        p0 = float(sc.p_engine if p_pre is None else p_pre)
        v0 = sc.verdict if verdict_pre is None else verdict_pre
        p1, v1 = float(sc_post.p_engine), sc_post.verdict
        added = [a for a, _ in fin if a not in {x for x, _ in ini}]
        removed = [a for a, _ in ini if a not in {x for x, _ in fin}]
        rerouted = [a for a, r in fin if (a, r) not in ini and a in {x for x, _ in ini}]
        reply = f"The assumed {req['type'].replace('_', ' ')} reply ({sc_post.flags.get('post_outcome')})"
        if f"{p0:.2f}" != f"{p1:.2f}":
            move = f"moved the probability from {p0:.2f} to {p1:.2f}"
        else:
            move = f"left the probability at {p1:.2f}"
        move += f" and kept the verdict {v1}" if v1 == v0 else f" and changed the verdict from {v0} to {v1}"
        parts = [f"{reply} {move}"]
        if added:
            parts.append(f"adding {', '.join(added)}")
        if removed:
            parts.append(f"removing {', '.join(removed)}")
        if rerouted:
            parts.append(f"re-routing {', '.join(rerouted)}")
        out = ", ".join(parts) + "."
        cf = " ".join(str(sc_post.flags.get("counterfactual") or "").split()).rstrip(".")
        if cf:
            first = text_sentences(cf + ".")[0].rstrip(".")      # one sentence: the README allows two in all
            out += f" Counterfactual: {first}."
        return out
