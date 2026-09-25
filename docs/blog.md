# A 0.05 risk score on a device shared by 52 cards: building a graph-first fraud agent on TigerGraph

*How I used GSQL, TigerVector and the TigerGraph MCP server to take a card-fraud alert from an uncertain signal to a defensible, policy-routed action, and what broke on the way to a real live run.*

---

## The problem: fraud alerts are relationship questions

Transaction 3478561 is a $74.96 online purchase. The bank's risk model scores it 0.05, and my own case-memory scorer says 0.07. A model that only scored transactions would let it through. But the device it came from had been used by 52 cards, 114 of those uses went through an anonymous proxy, and four earlier cases on that exact device were confirmed fraud. You only see that by following edges.

That is the argument of this post. Card-fraud investigation is mostly relationship questions, which belong in a graph, plus a few judgment calls, which belong to an LLM: weighing evidence, deciding whether to ask the customer, and writing the explanation and the suspicious activity report (SAR). For the TigerGraph x Hacker House Goa 2026 "Agentic Fraud Investigation" challenge I built an agent that splits the work along that line.

The hackathon dataset, HHGOA_IEEE, is built on the IEEE-CIS Fraud Detection data from Vesta Corporation: about 590,000 card transactions over six months from about 13,500 customers. There is no fraud label. Every transaction carries the bank's risk score, and the only ground truth is 5,565 closed investigations from the first four months, alongside a written fraud policy, five documented patterns and regulatory references. The benchmark is 20 alerts from the last two months, and not every fraud pattern in the data is documented.

## What I built, shown on one case (HHG-014)

In short: an agent that takes an alert (a risk score, a customer report or an analyst request), investigates it through my own GSQL queries exposed by the official TigerGraph MCP (Model Context Protocol) server, retrieves precedents and policy text with vector search, decides what to do under a policy file, and writes the whole case back into the graph so later cases can use it. HHG-014 is the case that shows the idea best.

**The trigger.** It is an analyst request, opened 2016-11-22 20:11: several cards this month show purchases from the same unusual device profile. The flagged transaction is 3478561, the $74.96 purchase above, on card C13487-K1.

**The graph.** The agent follows `FROM_DEVICE` from the flagged transaction to its device profile, `SM-G935F Build/NRD90M | Android 7.0 | chrome 62.0 for android | 1920x1080`, and back out to every card that used it. The `ring_profile` query reports:

- the profile is "strong" (specific enough to count as shared-device evidence) and has been used by 52 cards all-time (28 in its busiest 30-day window);
- 27 other cards share it in the current wave, 19 of which were already active on it before this case opened;
- 114 uses went through an anonymous proxy (`IP_PROXY:ANONYMOUS`);
- four closed cases on this exact profile (CC-2649, CC-2971, CC-2985, CC-3035) were confirmed fraud, all with the pattern `undocumented`, and all had reports filed.

The card's billing region is one it has used 36 times before, so geography says nothing. The signal lives entirely in the shared device.

**Memory.** Hybrid vector search over past case notes returns those same four ring cases as the nearest closed-case precedents (cosine distance 0.30, overlap reason `same_device`). The case's own `AgentCase` can't come back as a precedent, because agent cases must have opened strictly before the case under investigation (an earlier run ranked that self-match first). The ring cases rank ahead of a same-region account-takeover case, two cleared cases and a same-region out-of-region-use case. The search keeps cleared cases on purpose so the model sees both sides.

**Decision.** The deterministic engine applies its anonymous-proxy ring rule and floors the probability at 0.90. The LLM reviews the evidence and leaves it there. A value-of-information check finds that no further evidence request could change the action set, so the agent does not ask. The final actions, each routed by policy:

| Action | Route | Why |
|---|---|---|
| CREATE_CASE | auto | case opened |
| MONITOR_CONNECTED_CARDS | auto | R6, shared origin across 27 other cards |
| BLOCK_CARD | L1 | exposure $187.33, below the $2,500 L2 threshold |
| FILE_REPORT | L2 | policy 3a, ring membership |
| ESCALATE_TO_ANALYST | auto | R9, undocumented coordinated pattern |

Routes: **auto** runs without sign-off; **L1** and **L2** are the policy's two human-approval tiers (L2 covers SAR filing, blocking all of a customer's cards, and card blocks above $2,500). R6, R9 and 3a are clause numbers in the dataset's written fraud policy, which `policy/fraud_policy.yaml` encodes.

**Output.** The agent drafts a SAR covering both of the card's ring transactions (3460634 and 3478561, $187.33 in total) and runs an OFAC sanctions screen on the customer. It then writes the case back to the graph as `AgentCase` AC-HHG-014 with 6 `CaseEvent`s, 2 pending `Approval`s (the card block and the SAR filing) and edges to the 27 connected cards, the 4 precedents and the device. The whole case took 22 tool calls (21 graph reads and an OFAC screen), 5 LLM calls and 149 seconds. The SAR narrative passed the fact checks from lesson 8 on its first draft. The case summary and the card-block reason still share one slip, which I cover there too.

## Architecture: the engine decides, the LLM investigates

```text
 alert: risk score | customer report | analyst request
                          |
                          v
 +--------------------------------------------------------+
 | Phase machine (Python), P0 intake ... P10 persist      |
 +---------------------------+----------------------------+
 | LLM (claude-agent-sdk,    | Deterministic engine       |
 | Claude subscription)      |                            |
 | - picks graph queries     | - scorecard, pattern,      |
 | - moves p by <= 0.10      |   episode, SAR rule        |
 | - picks from admissible   | - policy gate              |
 |   actions                 | - value-of-info check      |
 | - writes summary and SAR  | - evidence simulator       |
 |                           | - LightGBM scorer          |
 +---------------------------+----------------------------+
                          |
     16 typed read tools (LLM) + query playbook (harness)
                          |
                          v
       tigergraph-mcp 1.0.3 over stdio (allowlisted)
                          |
                          v
 +--------------------------------------------------------+
 | TigerGraph Savanna, graph FraudGraph                   |
 | 25 installed GSQL queries; tg_wcc, tg_degree_cent      |
 | TigerVector: ClosedCase / AgentCase note_emb,          |
 |              PolicyChunk.emb (1024-d, cosine)          |
 +--------------------------------------------------------+
                          |
                          v
  Streamlit UI: queue | case | approvals | graph | runs
```

The core design decision is that **the engine decides and the LLM investigates**. The deterministic engine owns the scorecard, the pattern label, the fraud episode (the set of transactions that make up the fraud, which sets the exposure and the SAR scope), the SAR rule, the admissible actions, the approval routes and the stop rule. The LLM chooses which graph queries to run and weighs what they return. It may move the engine's probability by at most ±0.10, with a cited reason, then picks actions from the admissible set and writes the explanation and SAR narrative. That split matches the brief's role for the LLM ("reasoning, tool selection, evidence synthesis, and generating explanations rather than replacing graph analysis") and keeps every decision reproducible.

## How TigerGraph is used

### The graph model

`FraudGraph` has 13 vertex types and 24 edge types. The data layer is `Customer -OWNS-> Card -MADE-> Transaction`. Each card's transactions are chained in time order by `NEXT` edges carrying `gap_seconds`. Transactions link to `DeviceProfile`, `EmailDomain` (purchaser and recipient) and `BillingRegion`. The history layer is `ClosedCase -INVOLVES-> Transaction`, `-ON_CARD-> Card`, `-CONNECTED_TO-> Card` and `-MATCHES-> FraudPattern`. The agent's own memory is `AgentCase` (named that way because `Case` is a GSQL keyword), with `CaseEvent` and `Approval` vertices. Documents come in as `PolicyChunk -CHUNK_OF-> Document`.

The loaded graph has 590,742 transactions, 14,317 cards, 13,553 customers, 9,705 device profiles and 5,565 closed cases, connected by about 2.5 million base edges (576,425 of them `NEXT`).

The data has no card id, so I had to derive one. `customer_id + "-K" + dense_rank(card6)` reproduces all 14,975 of the labelled transaction-to-card pairs in the closed cases. The obvious alternatives match only 34-38%, and they would have silently broken every card-level answer.

### The GSQL query library

I wrote 25 installed queries. They fall into five groups: intake (4), baseline history (4), pattern detectors (3), shared-origin and ring queries (4) and retrieval (5), plus one UI query and four writers. The full list is in `graph/queries/`.

Time is the main trap. Every evidence query takes an `as_of` (or `to_ts`) bound, and the tool layer clamps those parameters to the case's `opened_at` whatever the model asks for, so the episode and the exposure never include anything from after the case opened. There is plenty to leak: HHG-018's card has 289 transactions in the seven days after its case opened. Two queries look past the open time on purpose. `post_open_activity` is harness-only (run by the phase machine, never offered to the model) and feeds a monitoring note that never counts toward exposure. `ring_profile` lists the whole ring wave (30 days either side of the card's own ring use), so some connected cards appear only after opening: 8 of HHG-014's 27. A few device attributes, such as `DeviceProfile.n_fraud_cases`, are all-time aggregates too, which matters for the replay numbers below.

Here is the heart of `ring_profile` (GSQL, highlighted as SQL). It goes from the ring's device profile back out to every other card in the wave, and to the closed cases on the same profile.

```sql
/* D = the ring's DeviceProfile, reached from card c via MADE -> FROM_DEVICE; @@cid = card c;
   wave_lo..wave_hi = 30 days either side of c's own ring transactions */
W = SELECT k FROM (d:D)-[:REV_FROM_DEVICE]->(u:Transaction)-[:REV_MADE]->(k:Card)
    WHERE k.id != @@cid AND datetime_to_epoch(u.ts) >= wave_lo AND datetime_to_epoch(u.ts) <= wave_hi
    ACCUM @@wave_cards += k.id,
          IF u.ts <= as_of THEN @@pre_open_cards += k.id END;
P = SELECT p FROM (d:D)-[:REV_FROM_DEVICE]->(u:Transaction)-[:REV_INVOLVES]->(p:ClosedCase)
    ACCUM @@closed_cases += ClosedRow(p.id, p.card_id, p.pattern, p.outcome, p.report_filed);
```

### Graph algorithms: why WCC is not a ring detector here

`graph/run_algos.py` runs once, directly through pyTigerGraph rather than MCP. It builds a `SHARES_DEVICE` Card-Card projection over strong device profiles only: 109,507 edges over 3,703 cards. It then runs `tg_wcc` and `tg_degree_cent`, taken unmodified from the official `gsql-graph-algorithms` repo. The results are written back as `Card.wcc_id` and `Card.device_degree`.

The negative result: connected components are not a ring detector on this data. Even after strength filtering, hub profiles (strong profiles shared by up to 60 cards each) bridge everything. My check of the projection puts 3,687 of its 3,703 cards in one component. (That figure comes from the offline copy of the same projection; I didn't save the live `wcc_summary` report.) So I made ring membership profile-centric: a card belongs to a ring when one specific strong profile on it carries confirmed-fraud cards or anonymous-proxy traffic. That is what finds HHG-014's ring. WCC and degree reach the model only as context attributes printed by `card_profile`. No decision code reads them.

### TigerVector and GraphRAG

TigerVector is TigerGraph's native `VECTOR` attribute type plus `vectorSearch`. Three `VECTOR` attributes (1024-d, cosine) hold the memory and the documents. `ClosedCase.note_emb` and `AgentCase.note_emb` share one vector space, so a case the agent wrote is retrievable exactly like a closed case the bank wrote. `PolicyChunk.emb` holds 219 chunks: 192 from 10 FinCEN, FATF and FFIEC documents, 21 from the dataset's fraud policy plus an internal OFAC screening note, and 6 from the five documented pattern descriptions.

Pure vector search fails on templated analyst notes. They all read almost alike, so nearest neighbours are close to random. `similar_prior_cases` therefore builds a *structural* candidate set first (same card, customer, billing region, pattern, or device profile reached by graph traversal, within 120 days), then ranks inside that set by vector similarity:

```sql
cc_dev = SELECT c FROM (d:DeviceProfile WHERE d.id == device_id)<-[:FROM_DEVICE]-(t:Transaction)<-[:INVOLVES]-(c:ClosedCase)
         WHERE c.opened_at <= as_of AND datetime_diff(as_of, c.opened_at) <= window_s
         ACCUM c.@overlap_reasons += "same_device", ...
/* ... more structural candidates ... */
v = vectorSearch({ClosedCase.note_emb, AgentCase.note_emb}, q, kk, {candidate_set: cand, distance_map: @@dist});
```

The query also guarantees at least one cleared case in the top-k when one exists. The model gets these summarised rows and the relevant policy chunks, not raw tables.

### The TigerGraph MCP server

Every graph read the agent makes goes through the official `tigergraph-mcp` server (1.0.3) over stdio. The allowlist permits 13 read tools and 4 write tools (`add_node(s)`, `add_edge(s)`) and blocks 19, including `gsql`, `run_query`, `install_query`, every `drop_*` and `delete_*`, and `update_schema`.

The model never sees raw MCP tools. It gets 16 typed tools: 14 wrappers, one per model-facing read query, each with a schema-checked parameter set, plus `find_similar_cases` and `grounding_chunks` for retrieval. Time parameters are clamped to `opened_at`, results are truncated at 24,000 characters, and each case has a budget of 12 LLM-chosen calls. The writer queries are harness-only (the harness is the Python phase machine, which calls queries itself, outside the model's control). With the subscription backend, the tools reach claude-agent-sdk through an in-process MCP server with every built-in tool turned off, so the only things the model can call are my queries.

## Agentic capabilities: an eleven-phase loop

Each case runs through eleven logged phases:

- **P0-P1 intake and memory:** open the `AgentCase` in the graph, run the four intake queries, and retrieve up to 8 precedents (1 to 8 in the final run) and 5 policy chunks.
- **P2 investigate:** the LLM picks its own queries (74 calls across the 20 cases, most often `episode_candidates`, `card_testing_check`, `shared_origin_scan` and `device_history`).
- **P3-P4 scorecard and assess:** the harness re-runs the engine's full query playbook, so the scorecard never depends on which queries the model happened to call. The LLM may then adjust the probability within ±0.10, citing a reason.
- **P5-P7 actions, evidence, actions again:** a value-of-information check decides whether asking could change the action set. The LLM picks initial actions from the admissible set, a deterministic simulator answers the one engine-chosen request (labelled `ASSUMED (simulated)`, with the counterfactual branch recorded), and actions are chosen again with a computed `what_changed`.
- **P8-P10 SAR, explain, persist:** a SAR is drafted only when policy requires it, grounded on FinCEN narrative guidance, with an OFAC screen and a validator. The LLM then writes the summary, and the case goes back into the graph.

**Uncertain to resolved.** HHG-001 shows the loop working:

| HHG-001 | Before evidence | After evidence |
|---|---|---|
| Probability | 0.30 from the engine, 0.22 after the LLM (no fraud-side evidence, every detector negative) | 0.10 |
| Verdict | uncertain (the card has four earlier confirmed-fraud episodes) | legitimate |
| Evidence | customer validation requested | simulated reply: customer confirmed |
| Actions | CREATE_CASE, VERIFY_WITH_CUSTOMER, MONITOR_CARD | CREATE_CASE, ALLOW_TRANSACTION, CLOSE_NO_FRAUD |
| Counterfactual | | a denial would have triggered BLOCK_CARD (L1) |

HHG-002 went the other way: a denial took it from 0.80 to 0.92, and the block stood.

**Policy gate.** Routes come from `policy/fraud_policy.yaml`, not from the model. Low-risk actions run automatically. DECLINE_TRANSACTION and BLOCK_CARD need L1 approval. FILE_REPORT and BLOCK_ALL_CARDS need L2, and BLOCK_CARD also moves to L2 above $2,500 of exposure. Action order is fixed by the policy file. If the LLM proposes an inadmissible action or cites a rule whose conditions do not hold, it gets one re-prompt and then a deterministic correction. In an earlier run the gate rejected three action lists (HHG-007, 011 and 014), all for citing rule R1 without meeting it, and each re-prompt fixed the list. In the final run every action list passed on the first try. Every L1/L2 action lands as a pending `Approval` vertex, and a team lead can approve or reject it from the Streamlit approvals page.

**Case memory.** Write-back is split. The `AgentCase` vertex and its 1024-d embedding go in as one pyTigerGraph REST upsert. Events, approvals and edges go through MCP. The agent then waits until the vector index reports ready, so the very next case can retrieve this one.

## Running it for $0

- **LLM:** `LLM_BACKEND=cli` drives claude-agent-sdk on a Claude subscription, with no API key. The final run used claude-sonnet-5 at high effort for every phase. The CLI session is resumed across phases so the prompt cache carries the case.
- **Embeddings:** `EMBED_BACKEND=local` runs BAAI/bge-large-en-v1.5 (MIT, 1024-d) on my machine, with no key and no network at run time.
- **Graph:** TigerGraph Savanna's free tier. Auto-resume can drop the first request, so an `ensure_awake()` step retries on 502/503 before a run.

## Results: what was measured, and what wasn't

**Read this first.** Two different 20/20s appear below. All 20 cases *completed* live through MCP. All 20 also *agree* with my deterministic engine's own offline prediction sheet. That agreement is a self-consistency check, not accuracy, because the organisers hold the answer key. Accuracy against real outcomes is the replay further down, and it misses all three of my targets.

**What the agent decided**

| Measure | Value |
|---|---|
| Verdicts | 8 fraud, 7 legitimate, 5 uncertain |
| SAR required | 2 (HHG-006 under-$500 burst, HHG-014 device ring; both `undocumented`), each drafted and awaiting L2 approval to file |
| Evidence requested | 14 of 20 cases (9 customer validation, 5 step-up) |
| Final actions by route | 46 auto, 18 L1, 2 L2 |
| Agreement with my engine's offline sheet | 20/20 on verdict, probability (±0.10), pattern, episode transactions, exposure, SAR, initial and final actions with routes, status |
| Answer validator | 20/20 clean |
| Written to graph | 20 AgentCase, 130 CaseEvent, 20 pending Approval |

**What it cost**

| Measure | Value |
|---|---|
| Completion | 20/20, 0 failed tool calls |
| Graph reads per case | median 19 (304 harness, 74 LLM-chosen in total, plus 2 OFAC screens) |
| Graph read latency | median 0.24 s per query |
| Time per case | median 151 s (85% of it in LLM calls) |
| Tokens per case | median 286k; ~95% prompt-cache writes and reads |

The agreement shows that the LLM agent, running on live graph data through MCP, reproduces the engine's reasoning. It does **not** show the verdicts are correct. It does now include the episode: HHG-018's live episode has all three transactions (3485990, 3490180, 3491361) and $197.11 of exposure, as offline. In an earlier run a row cap in `episode_candidates`, tighter than the engine's, cut 3485990 off.

**Run provenance.** The 20 answers come from one clean live pass: each case ran exactly once, all from the same committed code with no local changes, the same model and the same prompts. Before the pass I deleted the 173 rows my earlier runs had written back to the graph (20 `AgentCase`, 134 `CaseEvent`, 19 `Approval`) and reinstalled the fixed GSQL library (all 25 queries). So no answer mixes pre-fix and post-fix code, and no case can retrieve a stale copy of itself.

**Accuracy against real outcomes.** I replayed 310 labelled closed cases (180 confirmed fraud, 130 cleared) through the engine. The three metrics below are scored on the 180 confirmed-fraud cases. Episode Jaccard is the overlap between the transactions the engine put in the fraud episode and the ones the analyst did (1.0 means identical).

| Metric (180 confirmed-fraud cases) | Result | My target |
|---|---|---|
| Pattern accuracy | 0.878 | 0.95 |
| SAR decision accuracy | 0.939 | 1.00 |
| Episode Jaccard | 0.792 | 0.85 |

All three miss their targets. The SAR number flatters the engine: most of it comes from correctly *not* filing. The bank filed on 12 of those cases, and the engine agreed on only 5, while filing on 4 the bank did not. The engine is also conservative. It called 49% of replay cases uncertain, and of the verdicts it did commit to, 93.7% were right. The replay is also somewhat optimistic: `DeviceProfile.n_fraud_cases` and `Card.ring_id` are all-time attributes, so a replayed case can see fraud on its device that was confirmed after it opened.

The case-memory scorer (LightGBM with isotonic calibration, trained on the bank's own history) reaches an October-holdout AUC of 0.956, against 0.866 for the bank's risk score alone. October was also the early-stopping and calibration set, so treat that number as slightly optimistic. The out-of-fold monthly AUCs, 0.945 to 0.965, tell the same story (October's is the same holdout figure again). The scorer does not recognise either undocumented pattern (HHG-014 at 0.07, HHG-006 at 0.01), which is exactly why the graph detectors exist.

## What I learned: eight things that broke on the way to a live run

These are the most useful parts of the project to share, because none of them showed up offline.

**1. Response keys that silently emptied the memory.** TigerGraph's JSON API v2 prints vertex-set attributes with the alias prefix and accumulator marker: `res.text`, `res.@distance`. My offline fixtures used plain names. The first live run crashed in the SAR step. The worse effect was silent: every grounding chunk came back with empty text, and every precedent with distance 1.0 and no outcome or pattern, because the readers used `.get()` defaults. Stripping the prefix in `mcp/normalize.py` fixed both. Lesson: readers and contract tests should fail loudly on a missing key, not fall back to a default.

**2. A live query that disagreed with the engine of record.** Every graph-analysis query has a DuckDB mirror that acts as the test oracle. Live, `shared_origin_scan` counted recipient-email fraud with no 30-day window and without excluding the card's own history. Its top-10-by-fan-out heap also dropped exactly the rare domains the shared-origin rule looks for. Before the fix it pushed HHG-011 to fraud at 0.71; with the fix it is uncertain at 0.60, and live output equals offline. Lesson: diff live output against the second implementation case by case, and never put a top-k heap inside a detector for rare rows.

**3. A hub card posing as a fraud device.** `device_neighbors` counted every closed case on any card that had touched the device. A hub card's unrelated fraud history made a 5-card profile look like it carried 40 closed cases (HHG-019). It now counts only cases whose `INVOLVES` transactions actually used the device. Lesson: to count fraud on a device, traverse through the transactions that used it, not the cards that touched it.

**4. GSQL details worth knowing:**

- `run` and `type` are reserved: `run` can't be a PRINT alias (hence `best_run`) and `type` can't be a tuple field (hence `vtype` and `etype`).
- `PROXY` is a DDL reserved word, so the attribute is stored as `proxy_type`.
- A `POST-ACCUM` did not honour a `WHERE` comparing two pattern aliases (`o.id != s.id`), while `ACCUM` did.
- In a statement block (not inside `ACCUM`), a one-line `IF ... THEN x += 1; END;` needs that `;` before `END`.

**5. Algorithms on a half-settled graph.** Straight after inserting 109,507 `SHARES_DEVICE` edges, `tg_wcc` reported every card isolated, because the edge count lags a bulk insert by tens of seconds. `run_algos.py` now polls until two consecutive counts agree. Lesson: after a bulk insert, wait for the edge count to stop moving before running an algorithm on it.

**6. A successful load that said "0 loaded".** TigerGraph 4.2.5 nests loading statistics under `parsingStatistics.objectLevel` (and vector loads under `.embedding`). My loaders expected a flat layout and reported zero. Both now accept either layout. Lesson: parse the stats layout your server version actually returns; a loader that reports 0 on success trains you to ignore it.

**7. The 16-second ceiling.** The MCP server's `run_installed_query` sends no timeout header, so the server default of 16 s applies. My launcher pre-populates the MCP connection pool with a connection whose headers carry a 120 s `GSQL-TIMEOUT`. Lesson: check which headers an MCP server actually sends; its default timeout can be far shorter than your slowest query.

**8. Explanations can drift from the facts.** In an earlier run HHG-014's SAR said no prior SAR had been filed in connection with the device profile, although all four ring cases on it were filed. Both SARs said the bank had "blocked" the card, when the block was still a pending L1 approval. HHG-014's also quoted the card's all-time history (85 transactions, maximum $252.28) rather than what was known when the case opened (73 transactions, maximum $225.94; the corrected SAR quotes 71, the count before the flagged purchase), because `card_profile` read all-time card attributes. The decisions were right; those sentences were not, and the validator, which then checked only structure, ids, time-boxing and policy rules, passed them. `card_profile` now recomputes the card history up to `as_of`, and `rag/validate_sar.py` fact-checks the narrative: V14 rejects a pending or absent block described as done, V18 a "no prior report" claim contradicted by filed cases on the card, device or connected cards, and V19 card statistics that aren't as of the opening. A failure re-prompts the SAR step, and `make validate` re-runs V14-V18 on every answer file. Both final SARs passed on the first draft. Only the SAR is checked, though: HHG-014's summary says "two independent families" settle the verdict, and its card-block reason says "two evidence families" (a reason that went into the graph's events and the L1 approval), where the engine's scorecard counted one (device). Lesson: check factual sentences against engine fields, or better, assemble them from those fields.

### What the data taught me

- A "New" device is *not* a fraud signal here: 4.5% fraud for new online devices against 6.8% for online overall.
- No customer denial in the history was ever cleared, which is why a denial only ever pushes the probability up (HHG-002 went from 0.80 to 0.92).
- 7 of the 20 benchmark alerts resolved as legitimate in my run (the dataset README says to expect about half), so an agent that blocks on one signal will over-block.

## What I'd improve with more time

- **Make memory compound, visibly.** Write-back works, but in the final run no benchmark case retrieved another benchmark case's `AgentCase`. Every precedent came from the 5,565 closed cases, because the structural filter rarely links two exam cases. The live path also never writes `CASE_CITES` or `CASE_SIMILAR_TO_AGENT`.
- **Check the rest of the text.** The SAR is now fact-checked, but the summary and the action reasons are not. Their factual sentences should be built from engine fields.
- **Better grounding queries.** P1 uses one fixed query per trigger type. The top five often included off-target chunks: an FFIEC paragraph about currency straps in 8 cases, and a FATF report-scope paragraph in all 20. Case-specific queries or a reranker would fix it.
- **Recall on SARs and patterns.** Close the replay gaps above, starting with SAR under-filing.
- **Personalised PageRank from fraud cards,** so each new case moves a graph score. I never ran it, so `Card.fraud_ppr` is still zero.
- **Real channels** for customer and analyst evidence instead of the simulator, and a monitoring mode over the whole two months rather than 20 alerts.

## The pattern I'd reuse

Let the graph and a deterministic engine own the decisions, let the LLM own investigation and explanation, and put a policy file between the agent and anything irreversible. On HHG-014, that split turned a $74.96 purchase with a 0.05 risk score into a device ring shared by 52 cards, a drafted SAR and two actions waiting for a human. The decision is reproducible because the engine made it, not the model.

TigerGraph made that practical: traversal, vector search and case write-back live in one database, and every graph read the agent makes goes through one allowlisted MCP server. If you build something similar, budget for the lessons section: none of those eight problems showed up offline.

- Demo: <DEMO_URL>
- Code: <REPO_URL>
