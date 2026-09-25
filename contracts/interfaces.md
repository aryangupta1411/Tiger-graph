# Contract — query outputs and interfaces

*Authoritative contract. A module may not rename anything here without updating this file.*

### A. Installed query contracts (GSQL SYNTAX v3, installed; called via `tigergraph__run_installed_query`)

Parameter encoding over the MCP: `VERTEX<Card> c` → `{"c": {"id": "C12382-K1"}}`; `DATETIME as_of` → `"2016-12-05 01:55:28"`; `INT/FLOAT/STRING/BOOL` → JSON scalars; `LIST<FLOAT>` → JSON array. Every read query takes `as_of` and applies `ts <= as_of`. Each query PRINTs exactly the keys below (top-level JSON objects in `results`). Amounts are FLOAT, ids STRING, timestamps `YYYY-MM-DD HH:MM:SS`.

| Query | Params | Printed keys |
|---|---|---|
| `case_context` | `t VERTEX<Transaction>, as_of DATETIME` | `txn{id, ts, amt, product_cd, channel, addr1, addr2, p_email, r_email, risk_score, has_identity, device_new, proxy, device_type, device_id, cms_p, card_seq, prior_in_region, prior_on_dev, prior_pem, prior_pcd, prior_med_amt, prior_max_amt, ring_hit, burst_id}`, `card{id, card_type, card_network, modal_region, n_txns, ring_id}` (`n_txns` counted over ts ≤ as_of), `customer{id, n_cards}`, `device{id, is_strong, n_cards_alltime, n_cards_30d, n_proxy, n_fraud_cases}` — a MaxAccum<TUPLE>, so an absent device prints as `id: ""` with zero counts rather than an empty object (the same holds for `ring_profile.device` and `card_testing_check.larger_purchase`) |
| `card_profile` | `c VERTEX<Card>, as_of DATETIME` | `card{all Card attributes}` — the history fields `n_txns, first_ts, last_ts, median_amt, p90_amt, max_amt, max_in_person_amt` (-1 when none), `n_online, n_in_person, n_regions, n_devices_seen` are computed over ts ≤ as_of; the profile JSON attributes stay whole-history, `last30{n, sum_amt, n_online, n_regions, n_devices, n_products}`, `regions[{addr1, n, days, first_ts, last_ts}]` (top 5 by n, ts ≤ as_of), `devices[{device_id, n, first_ts, is_strong}]` (top 5), `emails[{domain, n}]`, `other_cards[{id, card_type, n_txns}]`, `prior_closed_cases[{id, opened_at, outcome, pattern, exposure_usd, report_filed, template_id}]` (`template_id` ∈ `fraud_reported` / `cleared_travel` / `cleared_new_phone` / `cleared_amount` / `undoc_ring` / `undoc_burst` — the cleared-precedent adjustment of D9 keys on it), `prior_agent_cases[{id, opened_at, verdict, pattern, exposure_usd}]` |
| `card_window` | `c VERTEX<Card>, from_ts DATETIME, to_ts DATETIME, max_rows INT` | `txns[{id, ts, amt, product_cd, channel, addr1, p_email, r_email, risk_score, cms_p, device_id, device_new, proxy, gap_seconds}]` ordered by ts asc (last `max_rows`), `summary{n, sum_amt, n_online, n_in_person}` |
| `region_history` | `c VERTEX<Card>, addr1 STRING, as_of DATETIME` | `region{addr1, prior_n, prior_days, first_ts, last_ts, share}`, `modal_region` (modal **in-person** region), `n_regions`, `home_activity_48h{n_home, n_other}` (**any channel**), `hint` ∈ home/known/rare/new. Rule of record (D8, the GSQL is authoritative): `home` = addr1 equals `modal_region`; `known` = ≥ 3 distinct days **or** ≥ 5 transactions; `rare` = 1–4 transactions; `new` = 0. `prior_n` counts **all channels**. Called with `as_of = flagged_ts − 1 s` by both the engine and the agent (every other read uses `as_of = opened_at`). |
| `device_history` | `c VERTEX<Card>, d VERTEX<DeviceProfile>, as_of DATETIME` | `prior_n, first_ts, device_new_values[]`. `prior_n` **includes the flagged transaction** when it sits on `d` (the engine subtracts one); called with `as_of = flagged_ts − 1 s` by both the engine and the agent (D8). |
| `device_neighbors` | `d VERTEX<DeviceProfile>, from_ts DATETIME, to_ts DATETIME, max_cards INT` | `device{id, is_strong, n_cards_alltime, n_cards_30d, n_proxy, n_fraud_cases}`, `cards[{card_id, customer_id, n_txns, sum_amt, n_proxy, n_new, first_ts, last_ts}]`, `closed_cases[{id, card_id, pattern, outcome, report_filed}]`, `agent_cases[{id, card_id, verdict, pattern}]` (agent cases opened strictly before `to_ts`) (if `is_strong` is false only `device` and counts are returned) |
| `email_neighbors` | `e VERTEX<EmailDomain>, c VERTEX<Card>, from_ts DATETIME, to_ts DATETIME` | `cards[{card_id, n_txns}]`, `closed_cases[{id, card_id, pattern}]` |
| `shared_origin_scan` | `c VERTEX<Card>, as_of DATETIME, days INT` | `devices[{device_id, is_strong, n_cards_30d, n_fraud_cases, n_txns_on_card}]`, `recipient_emails[{domain, n_cards_30d, n_fraud_cases}]`, `region_cluster_30d` (JSON string from Card) |
| `card_testing_check` | `c VERTEX<Card>, as_of DATETIME, small FLOAT, window_min INT, min_n INT, big FLOAT, lookahead_h INT` | `run{start_id, ids[], amts[], start_ts, end_ts}` — a GroupByAccum, so RESTPP prints it as a **one-element list**; `mcp/normalize.unwrap_singleton` (harness) and `engine.scorecard._r5` (defensively) unwrap it to a dict (best run all-time, or empty), `larger_purchase{id, amt, ts}` or empty, `cleared_over_big BOOL`, `chain{n_members, n_small}` |
| `under_threshold_burst` | `c VERTEX<Card>, as_of DATETIME` | `bursts[{burst_id, ids[], amts[], start_ts, end_ts, device_ids[], emails[], regions[]}]`, `lookalike_cards[]` |
| `recurring_charge_check` | `t VERTEX<Transaction>, tol FLOAT, as_of DATETIME` | `groups[{place, n, median_gap_days, gap_cv, first_ts, last_ts}]` (place = addr1 for in-person, p_email for online), `total_n` |
| `episode_candidates` | `t VERTEX<Transaction>, as_of DATETIME, gap_h INT` | `chain[{id, ts, amt, product_cd, channel, addr1, p_email, device_id, device_new, cms_p, sig_match BOOL}]` (the ≤ gap_h-gap chain containing t, ts ≤ as_of, within ±7 days of t; a chain of more than 200 members prints the 100 rows before t and up to 100 after) |
| `prior_cases_for_customer` | `cu VERTEX<Customer>, as_of DATETIME` | `closed_cases[{id, card_id, opened_at, outcome, pattern, exposure_usd, report_filed, n_txns, template_id}]`, `agent_cases[{id, card_id, opened_at, verdict, pattern, exposure_usd}]` |
| `ring_profile` | `c VERTEX<Card>, as_of DATETIME` | `ring_id`, `device{…}` (as in device_neighbors), `wave_cards[]` — decision **D6**: every *other* card on the profile with at least one transaction inside ±30 days of this card's own ring transactions (27 for HHG-014); `pre_open_cards[]` = wave cards with activity ≤ `as_of` (19 for HHG-014); `n_cards_alltime` printed separately (52 for HHG-014) so the all-time fan-out stays visible; `card_txns_on_ring[{id, ts, amt}]` (ts ≤ as_of); `closed_cases[{id, card_id, pattern, outcome, report_filed}]` — **rows, not ids** (M6). `engine.scorecard._ring` takes `connected_card_ids` from `wave_cards`. |
| `case_subgraph` | `c VERTEX<Card>, as_of DATETIME, hours INT` | `nodes[{id, vtype, label}]`, `edges[{src, dst, etype}]` (≤ 300) — `type` is a GSQL reserved word, so the tuple fields are `vtype` / `etype`; `ui/common.subgraph_from_answer` and the graph page read `vtype`/`etype` and fall back to `type`. |
| `post_open_activity` | `c VERTEX<Card>, opened_at DATETIME, days INT` | `txns[{id, ts, amt, channel, addr1, device_new, cms_p}]`, `summary{n, sum_amt}` |
| `similar_prior_cases` | `q LIST<FLOAT>, k INT, card_id STRING, customer_id STRING, device_id STRING, addr1 STRING, pattern_sig STRING, as_of DATETIME` | `cases[{id, kind ∈ closed/agent, outcome_or_verdict, pattern, exposure_usd, opened_at, distance, overlap_reasons[]}]` (diversified: ≥ 1 cleared when available; AgentCase rows opened strictly before `as_of`, so a case never retrieves its own AC- id) |
| `grounding_chunks` | `q LIST<FLOAT>, k INT, doc_filter STRING, kind_filter STRING` | `chunks[{id, doc_id, section, page, kind, text, distance}]` |
| Writers (harness-only): `open_case(case_id, source_case_id, customer_id, card_id, trigger_type, opened_at DATETIME, run_id, flagged_txn_id)` (no `status` parameter — the query writes `"open"`), `append_case_event(case_id, seq INT, kind, event_at DATETIME, payload)` (`event_at` fills `CaseEvent.at`), `close_case(...)`, `record_approval(case_id, action, route, status, decided_by, decided_at DATETIME, reason)` (the id `"AP-" + case_id + "-" + action` is derived inside the query; there is no `approval_id` parameter) | attribute lists mirror the vertices | `ok BOOL, id` |

### B. Engine interfaces (Python, `engine/`)

```python
# engine/types.py
@dataclass
class CaseContext:
    case_id: str; trigger_type: str; trigger_text: str; flagged_txn_id: str
    card_id: str; customer_id: str; opened_at: str  # 'YYYY-MM-DD HH:MM:SS'
    risk_score: float | None

@dataclass
class Evidence:                     # one row of the ledger; also the answer file's evidence item
    claim: str; source: str         # graph | document | customer | external
    ref: str; entity_ids: list[str]
    family: str                     # history | device | memory | customer | document
    direction: str                  # fraud | legit | neutral
    weight: float = 1.0

@dataclass
class Scorecard:
    cms_p: float; cal_p: float; adjustments: list[tuple[str, float]]; p_engine: float
    families_fraud: set[str]; families_legit: set[str]
    flags: dict            # ring_hit, burst_hit, card_testing_chain, recurring_match, denial, conflict, scorer_unreliable, mixed_channel, any_new_member, all_in_modal_region
    chain: list[dict]      # episode_candidates.chain rows
    episode_ids: list[str]; first_suspicious_txn_id: str; exposure_usd: float
    connected_card_ids: list[str]; connected_device_profiles: list[str]
    pattern: str; pattern_description: str
    verdict: str           # fraud | legitimate | uncertain (from bands)
    similar_prior_cases: list[str]

Facts = dict[str, dict]   # query name -> printed JSON (exactly the contracts in section A)

# engine/scorecard.py
def compute(ctx: CaseContext, facts: Facts, evidence: list[Evidence]) -> Scorecard
def post_evidence(sc: Scorecard, outcome: str) -> Scorecard   # outcome ∈ confirm | deny | no_reply | pass | fail | inconclusive | info
# engine/pattern_rule.py
def label(chain_members: list[dict], card_modal_region: str, flags: dict) -> str
# engine/episode.py
def members(chain: list[dict], flagged_id: str, verdict: str, flags: dict) -> list[str]
# engine/policy.py
def admissible(ctx, sc: Scorecard, stage: str) -> dict   # {"required": [...], "allowed": [...], "forbidden": [...], "routes": {a: route}, "citations": {a: [rule,...]}}; stage ∈ initial | final
def check(actions: list[dict], ctx, sc: Scorecard, stage: str) -> list[str]   # violations
def order(actions: list[dict]) -> list[dict]
def route(action: str, exposure: float) -> str
def sar_required(sc: Scorecard) -> tuple[bool, str]        # (file, reason citing 3a)
# engine/voi.py
def should_ask(ctx, sc: Scorecard, request_type: str) -> tuple[bool, dict]   # (ask, branches{confirm: actions, deny: actions, no_reply: actions})
# engine/simulator.py
def reply(request_type: str, ctx, sc: Scorecard) -> dict   # {"assumed_response": str, "outcome": str, "counterfactual": str}
# engine/status.py
def derive(verdict: str, final_actions: list[dict], pending: bool) -> str
# engine/stop.py
def stop_reason(sc_pre: Scorecard, sc_post: Scorecard | None, asked: bool, voi_zero: bool) -> str
```

Action dict shape everywhere: `{"action": "BLOCK_CARD", "route": "L1", "reason": "R2: ..."}`.

### C. Agent interfaces (Python, `agent/`)

```python
# agent/schemas.py  — Pydantic models mirroring README exactly (no numeric bounds in schema)
class EvidenceItem(BaseModel): claim: str; source: Literal["graph","document","customer","external"]; ref: str; entity_ids: list[str]
class EvidenceRequest(BaseModel): type: Literal["customer_validation","step_up_auth","analyst_info"]; asked_after_step: int; assumed_response: str
class ActionRec(BaseModel): action: Literal[<14 actions>]; route: Literal["auto","L1","L2"]; reason: str
class NextBestActions(BaseModel): initial: list[ActionRec]; final: list[ActionRec]; what_changed: str
class CaseRecord(BaseModel): status: Literal["open","closed_fraud","closed_legitimate","escalated"]; verdict: Literal["fraud","legitimate","uncertain"]; fraud_probability: float; pattern: Literal["card_testing","card_not_present_fraud","card_not_present_new_device","out_of_region_use","account_takeover","undocumented","none"]; pattern_description: str; affected_txn_ids: list[str]; first_suspicious_txn_id: str; connected_card_ids: list[str]; connected_device_profiles: list[str]; exposure_usd: float; evidence: list[EvidenceItem]; similar_prior_cases: list[str]; summary: str; written_to_graph: bool; graph_case_id: str
class SAR(BaseModel): file: bool; reason: str; narrative: str; subjects: list[str]; total_amount_usd: float; activity_dates: list[str]
class Answer(BaseModel): case_id: str; case: CaseRecord; evidence_requests: list[EvidenceRequest]; next_best_actions: NextBestActions; sar: SAR; stop_reason: str; tool_calls: int; tokens: int; latency_s: float
# LLM-facing intermediate schemas
class Assessment(BaseModel): verdict; fraud_probability: float; calibration_basis: str; pattern; pattern_description: str; affected_txn_ids: list[str]; first_suspicious_txn_id: str; connected_card_ids: list[str]; connected_device_profiles: list[str]; evidence: list[EvidenceItem]; wanted_requests: list[Literal[...]]; sufficient: bool
class ActionChoice(BaseModel): actions: list[ActionRec]           # chosen within the admissible set
class Closing(BaseModel): summary: str; stop_reason: str; similar_prior_cases_used: list[str]
class SarDraft(BaseModel): narrative: str; subjects: list[str]

# agent/mcp_client.py
async def open_session(read_only: bool) -> tuple[ClientSession, list[Tool]]   # typed tools generated from mcp/query_descriptions.yaml
async def run_query(session, name: str, params: dict) -> dict                  # returns printed JSON merged into one dict; logs to runlog
# agent/tools_local.py
def find_similar_cases(query_text: str, ctx, k=8) -> dict      # embeds with Voyage, calls similar_prior_cases
def grounding_chunks(query_text: str, doc_filter="", kind_filter="", k=5) -> dict
def ofac_screen(name: str) -> dict
# agent/runlog.py
class RunLog: record_tool(name, params, latency_s, result_bytes, ok=True, error="", caller="llm", phase=""); record_llm(phase, response, latency_s=0.0); set_phase(phase); record_phase(phase, payload); totals() -> {"tool_calls": int, "tokens": int, "latency_s": float}
#   calls.jsonl rows: {"seq","ts","kind":"tool"|"llm","phase","name", tool: "params","latency_s","result_bytes","ok","error","caller";
#                      llm: "model","input_tokens","output_tokens","cache_read_tokens","latency_s"}
#   phases.jsonl rows: {"kind":"phase","phase","name","at","payload"} — P4 payload has a top-level fraud_probability,
#                      P6 {request, simulated, voi, counterfactual, fraud_probability}, P5/P7 {stage, admissible, actions, fraud_probability, verdict}
# agent/answer_writer.py
def build(ctx, sc_pre, sc_post, initial, final, evidence, requests, sar, closing, runlog, graph_case_id) -> dict   # README JSON
# agent/validator.py   (thin wrapper over engine.validator — the single invariant set, decision D10)
def validate(answer: dict, db, meta: dict | None = None) -> list[str]   # [] when valid; resolves ids against DuckDB
def open_db(path) -> _Q                       # duckdb read-only wrapped as .q(sql, *params) -> DataFrame
def meta_for(answer: dict, case_pack=None) -> dict      # opened_at / card_id / customer_id (+ p_pre when no request)
# CLI: python -m agent.validator --db data/ids.duckdb <files...>   # prints "<file>: []" or the problems; exit 1 on any
```

### D. File and folder contracts

- Raw data: `data/raw/{transactions,identity,closed_cases_history,case_pack}.csv`; DuckDB `data/hhgoa.duckdb` with tables `tx` (typed), `tx_raw`, `idn`, `cc`, `cp`, and derived `cardmap`, `txc`, `device_profile`, `card_feat`, `txn_feat`, `closed_case_parsed`.
- Chunks: `data/out/<vertex_or_edge>.csv` headerless, **TAB-separated and unquoted** (no `"` quoting anywhere; the loader passes `sep="\t"`); transactions split at 45 MiB into `data/out/txn_000..007.csv`; column order and counts are fixed by `contracts/csv_columns.yaml`; vectors as `data/out/vec_<vertex>.psv` (`id|f1,f2,...`, split into <= 40 MB parts).
- Runs: `runs/<run_id>/<case_id>/{calls.jsonl, phases.jsonl, answer.json, sar.md}` and `runs/<run_id>/run_manifest.json`.
- Answers: `cases/<case_id>.json` (README format), `cases/MANIFEST.md`.
- Config: `.env` keys `TG_HOST, TG_GRAPHNAME=FraudGraph, TG_SECRET, TG_QUERY_TIMEOUT_MS=120000, TG_LOAD_TIMEOUT_MS=600000, LLM_BACKEND=cli|api|mock (empty = cli; mock under RUN_MODE=mock), ANTHROPIC_API_KEY (only for LLM_BACKEND=api), VOYAGE_API_KEY, MODEL=claude-sonnet-5, SAR_MODEL, EMBED_MODEL=voyage-4-lite, EMBED_DIM=1024, RUN_MODE=live|mock, HHGOA_DB=data/hhgoa.duckdb, ENGINE_DATA_DIR=data, ENGINE_FACTS_DB=data/hhgoa_engine.duckdb, HHGOA_README=data/raw/README.md` (M24).
- Engine facts: `engine/facts_from_etl.py` is the **only** builder of `ENGINE_FACTS_DB` (decision D7); the engine, the replay and `agent.validator` read it, never `f.parquet` / `novdec_scores.parquet` (retired to `impl/attic/`). The calibrator of record is `data/models/isotonic.json`, written by `etl/cms_train.py`.


---
