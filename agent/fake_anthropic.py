"""FakeAnthropic — an offline stand-in for `anthropic.AsyncAnthropic` used in
RUN_MODE=mock and CI. It mimics exactly the two SDK surfaces the phase machine uses:

  client.beta.messages.tool_runner(...)  -> async iterator of messages + generate_tool_call_response()
  client.beta.messages.parse(...)        -> ParsedBetaMessage-like object with .parsed_output

The canned structured outputs are derived from the JSON the harness embeds in the
prompt (SCORECARD / ADMISSIBLE SET / Final case state / Case data), so the dry run is
coherent with the engine and exercises every code path (clamping, policy check,
SAR validation, answer assembly) without an API key.
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel

_MARKERS = ("SCORECARD:", "ADMISSIBLE SET:", "Final case state:", "Case data:")


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
            return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _json_after(text: str, marker: str) -> dict:
    i = text.rfind(marker)
    if i < 0:
        return {}
    tail = text[i + len(marker):].strip()
    try:
        return json.loads(tail)
    except Exception:
        dec = json.JSONDecoder()
        try:
            obj, _ = dec.raw_decode(tail)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}


class _Usage(SimpleNamespace):
    pass


def _usage(inp: int = 1200, out: int = 300) -> _Usage:
    return _Usage(input_tokens=inp, output_tokens=out, cache_read_input_tokens=0, cache_creation_input_tokens=0)


class FakeBlock(SimpleNamespace):
    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        return d


class FakeMessage:
    _n = 0

    def __init__(self, content: list[FakeBlock], stop_reason: str, model: str, parsed_output: Any = None):
        FakeMessage._n += 1
        self.id = f"msg_fake_{FakeMessage._n:04d}"
        self.model = model
        self.role = "assistant"
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _usage()
        self.parsed_output = parsed_output

    def to_param(self) -> dict:
        return {"role": "assistant", "content": [b.to_dict() for b in self.content]}


class FakeRunner:
    """One tool-call turn (playbook-dependent) then a final text turn."""

    def __init__(self, client: FakeAnthropic, params: dict):
        self.client = client
        self.params = params
        self.tools = {t.name: t for t in params.get("tools", [])}
        self.messages = list(params.get("messages", []))
        self._last: FakeMessage | None = None
        self._cached: dict | None = None
        self._turn = 0

    def _plan_calls(self) -> list[tuple[str, dict]]:
        brief = _last_user_text(self.messages)
        opened = re.search(r"opened_at[\"']?\s*[:=]\s*[\"']?(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", brief)
        opened_at = opened.group(1) if opened else "2016-11-22 20:11:00"
        card = re.search(r"\b(C\d{5}-K\d)\b", brief)
        txn = re.search(r"flagged_txn_id[\"']?\s*[:=]\s*[\"']?(3\d{6})", brief)
        card_id = card.group(1) if card else "C13487-K1"
        txn_id = txn.group(1) if txn else "3478561"
        calls: list[tuple[str, dict]] = []
        ring = re.search(r'"ring_id":\s*"([^"]+ \| [^"]+)"', brief)
        if ring and "ring_profile" in self.tools:
            calls.append(("device_neighbors", {"d": {"id": ring.group(1)}, "from_ts": "2016-11-01 00:00:00", "to_ts": opened_at, "max_cards": 60}))
            calls.append(("ring_profile", {"c": {"id": card_id}, "as_of": opened_at}))
        if "episode_candidates" in self.tools:
            calls.append(("episode_candidates", {"t": {"id": txn_id}, "as_of": opened_at, "gap_h": 48}))
        if "find_similar_cases" in self.tools:
            calls.append(("find_similar_cases", {"query_text": "anonymous proxy device ring online product C new device", "device_id": ring.group(1) if ring else "", "addr1": "", "pattern_sig": "undocumented"}))
        return calls

    async def __aiter__(self):
        # turn 1: tool calls
        calls = self._plan_calls()
        blocks = [FakeBlock(type="text", text="Investigating per the playbook.")]
        for i, (name, inp) in enumerate(calls):
            blocks.append(FakeBlock(type="tool_use", id=f"toolu_fake_{self._turn}_{i}", name=name, input=inp))
        self._last = FakeMessage(blocks, "tool_use" if calls else "end_turn", self.params.get("model", "fake"))
        self.client.calls.append(("tool_runner", self._last))
        yield self._last
        if calls:
            resp = await self.generate_tool_call_response()
            self.messages += [self._last.to_param(), resp]
            self._turn += 1
            # turn 2: final note
            self._cached = None
            self._last = FakeMessage([FakeBlock(type="text", text=(
                "Evidence families for fraud: device (strong profile shared across many cards behind an anonymous proxy), "
                "memory (four prior undocumented closed cases on the same profile). No legitimacy family. Candidate pattern: "
                "undocumented (anonymous-proxy device ring). Candidate episode: the card's transactions on the ring profile before "
                "opening. No evidence request could change the action set; §6 test 1 is met."))], "end_turn", self.params.get("model", "fake"))
            self.client.calls.append(("tool_runner", self._last))
            yield self._last

    async def generate_tool_call_response(self) -> dict | None:
        if self._cached is not None:
            return self._cached
        if self._last is None:
            return None
        results = []
        for b in self._last.content:
            if getattr(b, "type", "") != "tool_use":
                continue
            tool = self.tools.get(b.name)
            if tool is None:
                text, err = f"unknown tool {b.name}", True
            else:
                try:
                    text, err = await tool.call(b.input), False
                except Exception as e:  # mirrors the SDK: errors become is_error results
                    text, err = repr(e), True
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": text if isinstance(text, str) else json.dumps(text), "is_error": err})
        if not results:
            return None
        self._cached = {"role": "user", "content": results}
        return self._cached

    async def until_done(self) -> FakeMessage:
        async for _ in self:
            pass
        return self._last


class FakeAnthropic:
    """Duck-typed AsyncAnthropic for RUN_MODE=mock."""

    def __init__(self, canned: dict[str, Any] | None = None):
        self.calls: list[tuple[str, Any]] = []
        self.canned = canned or {}
        self.beta = SimpleNamespace(messages=SimpleNamespace(tool_runner=self._tool_runner, parse=self._parse))

    def _tool_runner(self, **params: Any) -> FakeRunner:
        return FakeRunner(self, params)

    async def _parse(self, **params: Any) -> FakeMessage:
        schema: type[BaseModel] = params["output_format"]
        text = _last_user_text(params.get("messages", []))
        name = schema.__name__
        if name in self.canned:
            obj = self.canned[name](text) if callable(self.canned[name]) else self.canned[name]
        else:
            obj = self._default(name, text, schema)
        msg = FakeMessage([FakeBlock(type="text", text=obj.model_dump_json())], "end_turn", params.get("model", "fake"), parsed_output=obj)
        self.calls.append(("parse", msg))
        return msg

    # ---- canned generators -------------------------------------------------
    def _default(self, name: str, text: str, schema: type[BaseModel]) -> BaseModel:
        if name == "Assessment":
            sc = _json_after(text, "SCORECARD:")
            ev = []
            for row in (sc.get("ledger") or [])[:8]:
                ev.append({"claim": row.get("claim", "")[:300], "source": row.get("source", "graph"), "ref": row.get("ref", ""), "entity_ids": list(row.get("entity_ids", []))[:10]})
            if sc.get("trigger_type") == "customer_report":
                ev.insert(0, {"claim": "Customer denied the flagged transaction in the alert message.", "source": "customer", "ref": "trigger", "entity_ids": [sc.get("flagged_txn_id", "")]})
            wanted = [] if sc.get("p_engine", 0.5) >= 0.85 or sc.get("flags", {}).get("ring_hit") else (["step_up_auth"] if sc.get("online") else ["customer_validation"])
            return schema(
                verdict=sc.get("verdict", "uncertain"), fraud_probability=float(sc.get("p_engine", 0.5)),
                calibration_basis=f"cms_p {sc.get('cms_p')} calibrated to {sc.get('cal_p')}, adjustments {sc.get('adjustments')}; kept p_engine.",
                pattern=sc.get("pattern", "none"), pattern_description=sc.get("pattern_description", ""),
                affected_txn_ids=list(sc.get("episode_ids", [])), first_suspicious_txn_id=sc.get("first_suspicious_txn_id", ""),
                connected_card_ids=list(sc.get("connected_card_ids", [])), connected_device_profiles=list(sc.get("connected_device_profiles", [])),
                evidence=ev, wanted_requests=wanted, sufficient=bool(sc.get("p_engine", 0.5) >= 0.85 and len(sc.get("families_fraud", [])) >= 2),
            )
        if name == "ActionChoice":
            adm = _json_after(text, "ADMISSIBLE SET:")
            acts = []
            for a in adm.get("required", []):
                acts.append({"action": a, "route": adm.get("routes", {}).get(a, "auto"), "reason": "/".join(adm.get("citations", {}).get(a, ["policy"])) + ": required by the policy gate for this stage"})
            return schema(actions=acts)
        if name == "Closing":
            fin = _json_after(text, "Final case state:")
            m = re.search(r'harness\'s own determination is: "(.*?)"', text, re.S)
            return schema(
                summary=(f"Verdict {fin.get('verdict')} at probability {fin.get('fraud_probability')}: pattern {fin.get('pattern')}; "
                         f"episode {fin.get('affected_txn_ids')} with exposure ${fin.get('exposure_usd')}. Decisive evidence came from the "
                         f"{', '.join(fin.get('families_fraud', []) or ['history'])} family. "
                         f"{'An evidence request was simulated. ' if fin.get('evidence_requests') else 'No evidence request was needed. '}"
                         f"Recommended: {', '.join(a['action'] + ' (' + a['route'] + ')' for a in fin.get('final', []))}."),
                stop_reason=m.group(1) if m else "Policy §6 test 1.",
                similar_prior_cases_used=list(fin.get("similar_prior_cases", []))[:6],
            )
        if name == "SarDraft":
            cd = _json_after(text, "Case data:")
            ids = cd.get("affected_txn_ids", [])
            dates = cd.get("activity_dates", ["", ""])
            narrative = (
                f"This report concerns suspected coordinated card fraud recorded under internal case {cd.get('graph_case_id')}. "
                f"The subject is customer {cd.get('customer_id')} holding card {cd.get('card_id')}, whose card was used from the device profile "
                f"{(cd.get('connected_device_profiles') or ['unknown'])[0]} behind an anonymous proxy. "
                f"Between {dates[0]} and {dates[-1]} the card recorded {len(ids)} online purchases totalling ${cd.get('exposure_usd')} on that profile (transactions {', '.join(ids)}). "
                f"The same profile was used by {len(cd.get('connected_card_ids', []))} other cards in the same weeks, all online, all marked as new devices. "
                f"Four earlier investigations on the same profile ({', '.join(cd.get('similar_prior_cases', [])[:4])}) were closed as confirmed fraud. "
                f"The activity is inconsistent with the cardholder's history of in-person purchases in the home billing region at a median of ${cd.get('median_amt', 0)}. "
                f"{cd.get('prior_sar_note', 'No prior report exists on this card')}; {cd.get('ofac_note', 'OFAC screening returned no match')}. "
                f"A case has been opened, a card block is pending team-lead approval and the connected cards have been placed under monitoring; the total unauthorised amount is ${cd.get('exposure_usd')}."
            )
            subjects = [cd.get("customer_id", ""), cd.get("card_id", "")] + list(cd.get("connected_device_profiles", []))
            return schema(narrative=narrative, subjects=[s for s in subjects if s])
        raise ValueError(f"no canned output for {name}")
