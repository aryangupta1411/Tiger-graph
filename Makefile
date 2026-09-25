# HHGOA fraud agent — build pipeline (PLAN §3.2, §6; commands corrected per impl/00-integration.md §5 and
# decisions.md). Every target is idempotent and prints what it did. `make help` lists them grouped by stage and
# `make status` shows what is already built. Module entry points are variables so a renamed script is a one-line
# change. Everything runs as `python -m <module>` from the repo root except the graph/ scripts, which are plain
# files. Presentation (help / status / unknown-target help) is pure shell: no python start-up cost, no colour
# unless stdout is a real terminal.

SHELL := /bin/bash
.DEFAULT_GOAL := help
PY := uv run python
# RUN_ID_GIVEN is non-empty only when RUN_ID came from the command line or the environment (rerun-live requires it)
RUN_ID_GIVEN := $(filter-out undefined default,$(origin RUN_ID))
RUN_ID ?= $(shell date +%Y%m%d-%H%M%S)
# CASES: "all" or a comma list: HHG-014,HHG-006
CASES ?= all
# MODEL: empty = agent.bench reads MODEL from .env; override: make bench MODEL=claude-opus-5
MODEL ?=
MODEL_FLAG := $(if $(strip $(MODEL)),--model $(strip $(MODEL)),)
HHGOA_DB ?= data/hhgoa.duckdb
VALIDATOR_DB ?= data/ids.duckdb
# engine facts DB built from the ETL tables (engine/facts_from_etl.py; .env ENGINE_FACTS_DB)
ENGINE_FACTS_DB ?= data/hhgoa_engine.duckdb
# dataset README (fraud policy + patterns) that rag.chunk reads; copy it to data/raw/ on day 1 (.env HHGOA_README)
HHGOA_README ?= data/raw/README.md

# ---- module entry points (etl/ graph/ rag/ engine/ agent/ qa/ ui/ ops/) -------------
# etl/ (module A, impl/10-etl-scorer.md): python -m etl.<name> <db> [flags]
# ETL_BUILD_DB: raw CSVs -> data/hhgoa.duckdb (tx, tx_raw, idn, cc, cp, pairs, cardmap; asserts 14,975/14,975)
ETL_BUILD_DB := etl.build_db
# ETL_FEATURES: txc, txn_feat, burst_member, device_profile, card_feat
ETL_FEATURES := etl.features
# ETL_CLOSED: closed_case_parsed (templates + masked embed_text), closed_case_txn, closed_case_conn
ETL_CLOSED := etl.parse_closed_cases
# ETL_CMS: LightGBM scorer -> cms_p into txn_feat; data/models/{lgb_fraud.txt,isotonic.json,cms_metrics.json,cms_features.json}
ETL_CMS := etl.cms_train
# ETL_EXPORT: data/out/*.csv TSV chunks (headerless, per contracts/csv_columns.yaml) + manifest.json; owns fraud_pattern.csv
ETL_EXPORT := etl.export_graph_csvs
# engine/ (module D): facts DB from the ETL tables, deterministic drafts + expectation sheet, 310-case replay
# ENGINE_FACTS: data/hhgoa.duckdb -> $(ENGINE_FACTS_DB) (the only facts-DB builder, decisions D7)
ENGINE_FACTS := engine.facts_from_etl
# ENGINE_RUN: 20 engine-drafted answer files (no LLM) + qa/engine_expectation_sheet.md
ENGINE_RUN := engine.run_cases
# ENGINE_REPLAY: 310 closed-case replay -> engine/out/replay_metrics.json
ENGINE_REPLAY := engine.replay
# graph/ (modules B/C): plain scripts
# GRAPH_LOAD: DDL + loading jobs + every chunk (timeout=600000, count assertions); --schema-only / --jobs-only / --only <stems>
GRAPH_LOAD := graph/load_all.py
# GRAPH_ALGOS: project_shares_device -> tg_wcc -> wcc_summary -> degree
GRAPH_ALGOS := graph/run_algos.py
# GRAPH_INSTALL: CREATE/INSTALL all queries in graph/queries/*.gsql (one batch)
GRAPH_INSTALL := graph/install_all.py
# GRAPH_DESCRIBE: updateQueryDescription from mcp/query_descriptions.yaml
GRAPH_DESCRIBE := graph/describe_queries.py
# GRAPH_REBUILD: --from <stage>: schema|load|algos|vectors|install
GRAPH_REBUILD := graph/rebuild.py
# rag/ (module F): corpus -> chunks -> embeddings (local sentence-transformers by default) -> TigerVector
# RAG_FETCH: --verify: sha256 + text extraction of the downloaded PDFs; --ofac: sdn/alt csv
RAG_FETCH := rag.fetch_corpus
# RAG_CHUNK: README policy/pattern chunks + regulatory chunks -> data/out/policy_chunk*.{csv,jsonl}, document/chunk_of/about
RAG_CHUNK := rag.chunk
# RAG_CLOSED: closed_case_parsed.embed_text (ETL) -> data/out/closed_case_embed.jsonl
RAG_CLOSED := rag.closedcase_embed_text
# RAG_EMBED: --input <jsonl> --out <psv>: $EMBED_BACKEND (local bge-large-en-v1.5 1024-d by default; no key)
RAG_EMBED := rag.embed
# RAG_VECTORS: --ensure-schema (graph/schema_change_vectors.gsql) / --all / --status
RAG_VECTORS := rag.load_vectors
# agent/ (module E)
# AGENT_BENCH: --cases ... --run-id ... [--model ...] [--mock] [--llm-backend cli|api|mock]
AGENT_BENCH := agent.bench
# AGENT_PROMOTE: --run-id ... -> cases/
AGENT_PROMOTE := agent.promote
# AGENT_VALIDATE: --db $(VALIDATOR_DB) <files> (engine.validator over data/ids.duckdb)
AGENT_VALIDATE := agent.validator
# qa/ (module H): offline lints, no network
QA_LINT_GSQL := qa.lint_gsql
QA_INTEGRATION := qa.check_integration

# ---- terminal presentation ------------------------------------------------------------
# make strips `#` and everything after it from a *variable definition* (recipe lines keep it),
# so a literal hash the shell needs - ${#name} - has to come through this variable.
HASH := \#

# $(SAY) is a shell prelude that every human-facing target starts with. It picks colour or
# plain exactly the way ops/console.py does, computes the banner width W, and defines the
# printers used below. Pure shell on purpose: `make help` must not pay a python start-up.
#   plain  when HHG_PLAIN is set to anything but 0, NO_COLOR is set, or TERM is unset/dumb
#   colour when FORCE_COLOR is set to anything but 0, or stdout is a tty
# So `make help | cat`, `make status > file` and CI logs contain zero escape bytes, and the
# status tags line up with the python CLIs ("OK  ", "WARN", "FAIL" + one space).
SAY = C=; B=; D=; G=; Y=; RD=; R=; \
	if [ -n "$${HHG_PLAIN:-}" ] && [ "$${HHG_PLAIN:-0}" != 0 ]; then :; \
	elif [ -n "$${NO_COLOR:-}" ] || [ -z "$${TERM:-}" ] || [ "$${TERM:-dumb}" = dumb ]; then :; \
	elif { [ -n "$${FORCE_COLOR:-}" ] && [ "$${FORCE_COLOR:-0}" != 0 ]; } || [ -t 1 ]; then \
	  C=$$'\033[36m'; B=$$'\033[1m'; D=$$'\033[2m'; G=$$'\033[32m'; Y=$$'\033[33m'; RD=$$'\033[1;31m'; R=$$'\033[0m'; \
	fi; \
	W=$$(tput cols 2>/dev/null || echo 100); case "$$W" in ''|*[!0-9]*) W=100;; esac; \
	if [ "$$W" -gt 100 ] || [ "$$W" -lt 40 ]; then W=100; fi; \
	_bar() { printf '%*s' "$$1" '' | tr ' ' "$$2"; }; \
	banner() { printf '\n%s%s== %s %s%s\n' "$$B" "$$C" "$$1" "$$(_bar $$((W-4-$${$(HASH)1})) =)" "$$R"; \
	           if [ -n "$$2" ]; then printf '     %s%s%s\n' "$$D" "$$2" "$$R"; fi; }; \
	kv() { printf '     %-17s : %s\n' "$$1" "$$2"; }; \
	okl() { printf '%sOK  %s %s\n' "$$G" "$$R" "$$1"; }; \
	warnl() { printf '%sWARN%s %s\n' "$$Y" "$$R" "$$1"; }; \
	faill() { printf '%sFAIL%s %s\n' "$$RD" "$$R" "$$1"; }; \
	dim() { printf '     %s%s%s\n' "$$D" "$$1" "$$R"; };

.PHONY: help status install env db features cms facts chunks ids load algos install-queries describe \
        corpus embed vectors replay sheet engine-drafts bench bench-one bench-free promote validate ui mock-run \
        test test-data test-live lint lint-gsql lint-contracts fmt keepalive awake rebuild demo-warm clean \
        clear-writebacks rerun-live

##@ Start here
help: ## this list, grouped by build stage (the default target)
	@$(SAY) \
	 n=$$(grep -hE '^[a-zA-Z_][a-zA-Z0-9_-]*:.*## ' $(MAKEFILE_LIST) | wc -l | tr -d ' '); \
	 banner "make" "agentic fraud investigation on TigerGraph - $$n targets, in build order"; \
	 awk -v c="$$C" -v b="$$B" -v r="$$R" -v w=$$((W-22)) ' \
	   function emit(name, desc,   nn,i,line,lead,wd) { \
	     nn=split(desc, wd, " "); line=""; lead=sprintf("  %s%-17s%s ", c, name, r); \
	     for (i=1; i<=nn; i++) { \
	       if (line != "" && length(line)+1+length(wd[i]) > w) { print lead line; lead=sprintf("  %-17s ", ""); line=wd[i] } \
	       else { line = (line=="" ? wd[i] : line " " wd[i]) } } \
	     if (line != "") print lead line } \
	   BEGIN { FS=":.*?## " } \
	   /^##@ / { printf "\n%s%s%s\n", b, substr($$0,5), r; next } \
	   /^[a-zA-Z_][a-zA-Z0-9_-]*:.*## / { emit($$1, $$2) } \
	 ' $(MAKEFILE_LIST); \
	 printf '\n'; \
	 dim "make status            what is already built, and what the next command is"; \
	 dim "make install db features cms facts     the offline pipeline, in order"; \
	 dim "HHG_PLAIN=1 make ...   force plain ASCII output (demo capture / CI)"; \
	 printf '\n'

status: ## what exists and what does not: data, models, chunks, vectors, cases, backends. Offline; never prints a secret
	@$(SAY) \
	 cfg() { v="$${!1-}"; \
	         if [ -z "$$v" ] && [ -f .env ]; then \
	           v=$$(sed -n "s/^$$1=//p" .env | tail -1 | sed -e 's/[[:space:]][[:space:]]*\#.*$$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$$//' -e 's/^"//' -e 's/"$$//'); \
	         fi; printf '%s' "$${v:-(unset)}"; }; \
	 held() { if [ "$$(cfg "$$1")" != "(unset)" ]; then printf 'set (value never printed)'; else printf 'not set'; fi; }; \
	 MISSING=0; NEXT=""; \
	 chk() { n=0; \
	   for f in $$2; do if [ -e "$$f" ]; then n=$$((n+1)); fi; done; \
	   if [ "$$n" -gt 0 ]; then \
	     sz=$$(du -ch $$2 2>/dev/null | tail -1 | cut -f1 | tr -d ' '); \
	     if [ "$$n" -gt 1 ]; then info="$$n files, $$sz"; else info="$$sz"; fi; \
	     st=$$(printf '%-7s' ready); st="$$G$$st$$R"; \
	   else \
	     MISSING=$$((MISSING+1)); NEXT="$${NEXT:-$$3}"; info="-> $$3"; \
	     st=$$(printf '%-7s' missing); st="$$Y$$st$$R"; \
	   fi; \
	   printf '%-26s  %s  %-34s  %s\n' "$$1" "$$st" "$$2" "$$info"; }; \
	 banner "make status" "what this checkout already has, checked on disk only - no network, no secrets"; \
	 kv "repo"        "$$PWD"; \
	 kv "RUN_MODE"    "$$(cfg RUN_MODE)"; \
	 kv "LLM_BACKEND" "$$(cfg LLM_BACKEND)   (cli = claude-agent-sdk, no API key)"; \
	 kv "MODEL"       "$$(cfg MODEL)"; \
	 kv "EMBED_BACKEND" "$$(cfg EMBED_BACKEND)   model $$(cfg EMBED_MODEL) dim $$(cfg EMBED_DIM)"; \
	 kv "TG_HOST"     "$$(cfg TG_HOST)"; \
	 kv "TG_GRAPHNAME" "$$(cfg TG_GRAPHNAME)"; \
	 kv "TG_SECRET"   "$$(held TG_SECRET)"; \
	 kv "ANTHROPIC_API_KEY" "$$(held ANTHROPIC_API_KEY)"; \
	 kv "VOYAGE_API_KEY" "$$(held VOYAGE_API_KEY)"; \
	 printf '\n'; \
	 printf '%-26s  %-7s  %-34s  %s\n' artefact state path detail; \
	 printf '%-26s  %-7s  %-34s  %s\n' "$$(_bar 26 -)" "$$(_bar 7 -)" "$$(_bar 34 -)" "$$(_bar 6 -)"; \
	 chk "env file"            ".env"                        "make install"; \
	 chk "raw dataset"         "data/raw/*.csv"              "copy the 4 HHGOA CSVs to data/raw"; \
	 chk "dataset README"      "$(HHGOA_README)"             "copy the dataset README to data/raw"; \
	 chk "etl duckdb"          "$(HHGOA_DB)"                 "make db"; \
	 chk "cms scorer"          "data/models/lgb_fraud.txt"   "make cms"; \
	 chk "cms calibration"     "data/models/isotonic.json"   "make cms"; \
	 chk "engine facts db"     "$(ENGINE_FACTS_DB)"          "make facts"; \
	 chk "validator ids db"    "$(VALIDATOR_DB)"             "make ids"; \
	 chk "graph chunks"        "data/out/txn_*.csv"          "make chunks"; \
	 chk "chunk manifest"      "data/out/manifest.json"      "make chunks"; \
	 chk "policy chunks"       "data/out/policy_chunk*.jsonl" "make corpus"; \
	 chk "closed-case text"    "data/out/closed_case_embed.jsonl" "make corpus"; \
	 chk "embeddings"          "data/out/vec_*.psv"          "make embed"; \
	 chk "expectation sheet"   "qa/engine_expectation_sheet.md" "make sheet"; \
	 chk "engine drafts"       "engine/out/*.draft.json"     "make sheet"; \
	 chk "replay metrics"      "engine/out/replay_metrics.json" "make replay"; \
	 chk "answer files"        "cases/HHG-*.json"            "make bench promote RUN_ID=v1"; \
	 chk "agent runs"          "runs/*/run_manifest.json"    "make bench RUN_ID=v1 (or make mock-run)"; \
	 chk "schema contract"     "contracts/schema.md"         "needed by make lint-contracts"; \
	 printf '\n'; \
	 if [ "$$MISSING" -eq 0 ]; then okl "every offline artefact is present"; \
	 else warnl "$$MISSING artefact(s) missing"; dim "next: $$NEXT"; fi; \
	 dim "graph load state is a server fact and cannot be read offline: run 'make awake' then '$(PY) $(GRAPH_LOAD) --dry-run'"; \
	 printf '\n'

##@ Setup (day 0)
install: ## uv sync (+dev extras); copies .env.example -> .env if missing
	uv sync --extra dev
	@test -f .env || (cp .env.example .env && echo "created .env — fill TG_HOST / TG_SECRET / API keys")

env: ## print the effective config as KEY=value (secrets masked); `make status` is the readable version
	@$(PY) -c "import os; from dotenv import load_dotenv; load_dotenv(); \
	  [print(f'{k}={(v[:6]+\"…\") if k.endswith((\"KEY\",\"SECRET\")) and v else v}') for k,v in sorted(os.environ.items()) if k.startswith(('TG_','RUN_MODE','MODEL','SAR_MODEL','LLM_BACKEND','EMBED','VOYAGE','ANTHROPIC','ENGINE_','HHGOA_'))]"

awake: ## wake the Savanna workspace (retries 502/503 up to 4 min) and print the version
	$(PY) -m ops.keep_alive --once

keepalive: ## ping an installed query every 3 min (benchmark / demo recording); Ctrl-C to stop
	$(PY) -m ops.keep_alive

##@ ETL, scorer, facts (day 1)
db: ## data/raw/*.csv -> data/hhgoa.duckdb (tx 590,742 / idn 144,432 / cc 5,565 / cp 20; card rule 14,975/14,975)
	$(PY) -m $(ETL_BUILD_DB) --raw data/raw --db $(HHGOA_DB)

features: ## txn_feat, device_profile (9,705), card_feat (14,317), burst_member; then the closed-case parser (embed_text)
	$(PY) -m $(ETL_FEATURES) $(HHGOA_DB)
	$(PY) -m $(ETL_CLOSED) $(HHGOA_DB)

cms: ## train the case-memory scorer (Oct holdout AUC >= 0.95) -> txn_feat.cms_p + data/models/ (isotonic.json = the calibration of record)
	$(PY) -m $(ETL_CMS) $(HHGOA_DB) --out data/models

facts: ## engine facts DuckDB from the ETL tables -> data/hhgoa_engine.duckdb, ENGINE_FACTS_DB (590,742 / 14,317 / 9,705 / 13,553 / 5,565 / 14,955 / 20)
	$(PY) -m $(ENGINE_FACTS) --etl $(HHGOA_DB) --out $(ENGINE_FACTS_DB)

chunks: ## headerless TSV chunks in data/out/ (txn_000..007 <= 45 MB each), verified against contracts/csv_columns.yaml
	$(PY) -m $(ETL_EXPORT) $(HHGOA_DB) --out data/out --yaml contracts/csv_columns.yaml --verify

ids: ## id-only DuckDB for the validator in CI (data/ids.duckdb, ~20 MB, checked in) + views txc/card_feat/customer_feat/cc
	$(PY) -m ops.export_id_tables --src $(HHGOA_DB) --out $(VALIDATOR_DB)

##@ Graph: schema, load, algorithms, queries (day 2-4)
load: ## schema + loading jobs + all chunks in one run (counts asserted: 590,742 / 576,425 / 140,784 / 14,955); `graph/load_all.py --schema-only` for the DDL alone
	$(PY) $(GRAPH_LOAD)

algos: ## project_shares_device -> tg_wcc -> wcc_summary -> degree (timeout 600 s each), results into Card / DeviceProfile attrs
	$(PY) $(GRAPH_ALGOS)

install-queries: ## CREATE + INSTALL every graph/queries/*.gsql in one batch (16 reads + 4 writers + 4 RAG = 24; 1-3 min compile on TG-00)
	$(PY) $(GRAPH_INSTALL)

describe: ## register query descriptions + JSON parameter examples (pyTigerGraph updateQueryDescription)
	$(PY) $(GRAPH_DESCRIBE)

rebuild: ## disaster recovery: rebuild the workspace from checked-in chunks: make rebuild FROM=schema|load|algos|vectors|install
	$(PY) $(GRAPH_REBUILD) --from $(FROM)

##@ GraphRAG: corpus, embeddings, TigerVector (day 7)
corpus: ## verify the downloaded corpus, chunk README + regulations, emit the ClosedCase embed text (ETL embed_text)
	$(PY) -m $(RAG_FETCH) --verify
	HHGOA_README=$(HHGOA_README) $(PY) -m $(RAG_CHUNK)
	$(PY) -m $(RAG_CLOSED) --db $(HHGOA_DB) --out data/out

embed: ## embeddings ($EMBED_BACKEND, default local/free) -> data/out/vec_PolicyChunk.psv and vec_ClosedCase.psv (id|f1,f2,...)
	$(PY) -m $(RAG_EMBED) --input data/out/policy_chunk_embed.jsonl --out data/out/vec_PolicyChunk.psv
	$(PY) -m $(RAG_EMBED) --input data/out/closed_case_embed.jsonl --out data/out/vec_ClosedCase.psv

vectors: ## add the VECTOR attributes (graph/schema_change_vectors.gsql, GLOBAL job) if missing, load vec_*.psv, poll /vector/status
	$(PY) -m $(RAG_VECTORS) --ensure-schema
	$(PY) -m $(RAG_VECTORS) --all

##@ Engine QA: the deterministic decision engine (day 5-6)
sheet: ## regenerate qa/engine_expectation_sheet.md (+ engine/out/*.draft.json) from the deterministic engine
	ENGINE_FACTS_DB=$(ENGINE_FACTS_DB) $(PY) -m $(ENGINE_RUN)

engine-drafts: ## 20 engine-drafted answer files with no LLM (day-6 fallback) -> runs/engine-drafts/<case>.draft.json
	ENGINE_FACTS_DB=$(ENGINE_FACTS_DB) $(PY) -m $(ENGINE_RUN) --out runs/engine-drafts

replay: ## 310-case deterministic replay -> engine/out/replay_metrics.json (targets: pattern >= 95 %, SAR 100 %, Jaccard >= 0.85)
	ENGINE_FACTS_DB=$(ENGINE_FACTS_DB) $(PY) -m $(ENGINE_REPLAY)

##@ Agent: run, validate, promote the 20 cases (day 8-12)
bench: ## run the agent on CASES (default all 20, opened_at order) -> runs/<RUN_ID>/
	$(PY) -m $(AGENT_BENCH) --cases $(strip $(CASES)) --run-id $(RUN_ID) $(MODEL_FLAG)

bench-one: ## one case: make bench-one CASE=HHG-014
	$(PY) -m $(AGENT_BENCH) --cases $(CASE) --run-id $(RUN_ID) $(MODEL_FLAG)

bench-free: ## real model (claude-agent-sdk, no API key) over DuckDB facts: make bench-free CASES=HHG-014,HHG-010
	$(PY) -m $(AGENT_BENCH) --cases $(strip $(CASES)) --run-id $(RUN_ID) $(MODEL_FLAG) --mock --llm-backend cli
	$(PY) -m $(AGENT_VALIDATE) --db $(VALIDATOR_DB) runs/$(RUN_ID)/*/answer.json

promote: ## copy runs/<RUN_ID>/*/answer.json -> cases/ after validation, write cases/MANIFEST.md
	$(PY) -m $(AGENT_VALIDATE) --db $(VALIDATOR_DB) runs/$(RUN_ID)/*/answer.json
	$(PY) -m $(AGENT_PROMOTE) --run-id $(RUN_ID)

validate: ## validate cases/*.json against the README schema + id existence (VALIDATOR_DB)
	$(PY) -m $(AGENT_VALIDATE) --db $(VALIDATOR_DB) cases/*.json

##@ Live re-run (docs/RERUN.md)
clear-writebacks: ## list the agent's own AgentCase / CaseEvent / Approval write-backs for HHG-001..020 (dry run); APPLY=1 deletes exactly those
	$(PY) -m ops.clear_case_writebacks $(if $(filter 1 yes true,$(APPLY)),--apply,) $(if $(strip $(RUN_ID_GIVEN)),--run-id $(RUN_ID),)

rerun-live: ## RUN_ID=<id> required: wake, install-queries, describe, then the 20 cases one at a time (opened_at order, --resume), validating each
	@$(SAY) \
	 if [ -z "$(RUN_ID_GIVEN)" ]; then faill "RUN_ID is required: make rerun-live RUN_ID=live-final"; exit 2; fi; \
	 banner "rerun-live $(RUN_ID)" "live graph + claude-agent-sdk (LLM_BACKEND=cli, RUN_MODE=live from the environment; .env is not edited)"; \
	 if [ -n "$$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; then \
	   warnl "the working tree has uncommitted changes: the manifest will record git_dirty=true (commit first for a clean sha)"; fi
	$(MAKE) --no-print-directory awake
	$(MAKE) --no-print-directory install-queries
	$(MAKE) --no-print-directory describe
	@$(SAY) \
	 CASES_ORDERED=$$($(PY) -c "from agent.bench import load_case_pack; from agent.config import SETTINGS; print(' '.join(c.case_id for c in load_case_pack(SETTINGS.case_pack_csv)))"); \
	 n=0; for c in $$CASES_ORDERED; do \
	   n=$$((n+1)); banner "case $$n/20: $$c" "runs/$(RUN_ID)/$$c/answer.json"; \
	   LLM_BACKEND=cli RUN_MODE=live $(PY) -m $(AGENT_BENCH) --cases $$c --run-id $(RUN_ID) --resume $(MODEL_FLAG) \
	     || { faill "$$c: the agent run failed; fix the cause, then re-run 'make rerun-live RUN_ID=$(RUN_ID)' (--resume skips finished cases)"; exit 1; }; \
	   $(PY) -m $(AGENT_VALIDATE) --db $(VALIDATOR_DB) runs/$(RUN_ID)/$$c/answer.json \
	     || { faill "$$c: answer failed validation; inspect runs/$(RUN_ID)/$$c/, delete that case directory, then re-run the target"; exit 1; }; \
	 done; \
	 banner "rerun-live $(RUN_ID): summary" "every answer re-validated together"; \
	 $(PY) -m $(AGENT_VALIDATE) --db $(VALIDATOR_DB) runs/$(RUN_ID)/HHG-*/answer.json || exit 1; \
	 k=$$(ls runs/$(RUN_ID)/HHG-*/answer.json 2>/dev/null | wc -l | tr -d ' '); \
	 if [ "$$k" = 20 ]; then okl "20/20 answers in runs/$(RUN_ID); next: make promote RUN_ID=$(RUN_ID)"; \
	 else faill "$$k/20 answers in runs/$(RUN_ID)"; exit 1; fi

##@ UI and demo
ui: ## Streamlit analyst dashboard (queue / case / approvals / graph / runs)
	uv run streamlit run ui/app.py --server.headless true --server.port 8501

mock-run: ## runs/mock/ from cases/*.json (or the sample HHG-014) so the UI works without a graph or keys
	$(PY) -m ops.make_mock_run --answers cases --run-id mock

demo-warm: awake keepalive ## wake the workspace and keep it awake for the recording

##@ Quality
test: ## unit tests (no network; data-backed tests skip when data/hhgoa.duckdb / the facts DB are absent)
	uv run pytest tests/unit

test-data: ## the data-backed unit tests, mandatory locally after `make db features cms facts` (fails if the DBs are missing)
	@test -f $(HHGOA_DB) || (echo "missing $(HHGOA_DB): run make db features cms" && exit 1)
	@test -f $(ENGINE_FACTS_DB) || (echo "missing $(ENGINE_FACTS_DB): run make facts" && exit 1)
	HHGOA_DB=$(HHGOA_DB) ENGINE_FACTS_DB=$(ENGINE_FACTS_DB) uv run pytest tests/unit/test_card_rule.py tests/unit/test_features.py tests/unit/test_engine.py -rs

test-live: ## live contract tests against the workspace (needs .env)
	uv run pytest tests/live -v

lint: ## ruff check
	uv run ruff check .

lint-gsql: ## offline GSQL lint: every alias.attribute resolves against graph/schema.gsql; yaml matches the query files
	$(PY) -m $(QA_LINT_GSQL)

lint-contracts: ## offline contract lint: gsql params vs mcp/query_descriptions.yaml vs DuckFacts vs the agent tool table
	$(PY) -m $(QA_INTEGRATION)

fmt: ## ruff format + fix
	uv run ruff format . && uv run ruff check --fix .

clean: ## remove caches (never data/ or runs/)
	rm -rf .pytest_cache .ruff_cache; find . -name __pycache__ -type d -prune -exec rm -rf {} +

# ---- unknown targets --------------------------------------------------------------------
# Paste a command block with a trailing `# comment` and make reads every word after the `#`
# as another goal, then dies with "make: *** No rule to make target '#'. Stop." — three
# times in a row, with no hint about what went wrong. `.DEFAULT` catches every unknown goal
# instead: a goal at or after a `#` is reported once as a pasted comment and ignored (so the
# pasted block still does what it looks like it does), anything else prints the closest real
# target names and fails. `Makefile: ;` keeps .DEFAULT away from remaking the makefile.
Makefile: ;

.DEFAULT:
	@$(SAY) \
	 MG=" $(MAKECMDGOALS) "; BEFORE=" $${MG%%\#*}"; AFTER=1; \
	 case "$$BEFORE" in *" $@ "*) AFTER=0;; esac; \
	 case "$@" in \#*) AFTER=2;; esac; \
	 if [ "$$AFTER" = 2 ]; then \
	   warnl "'$@' is not a target: that looks like a comment pasted after a make command"; \
	   dim "make splits the command line on spaces, so 'make db  # build the DB' asks for the targets '#', 'build', 'the', 'DB'"; \
	   dim "put the comment on its own line, or drop it. The real targets on that line already ran."; \
	   exit 0; \
	 fi; \
	 if [ "$$AFTER" = 1 ]; then dim "ignoring '$@' (part of the pasted comment above)"; exit 0; fi; \
	 faill "no rule to make target '$@'"; \
	 near=$$(grep -hoE '^[a-zA-Z_][a-zA-Z0-9_-]*:.*## ' $(MAKEFILE_LIST) | cut -d: -f1 | sort -u | \
	   awk -v g='$@' ' \
	     function lev(a,b,   i,j,m,n,cst,x,y,z,d) { \
	       m=length(a); n=length(b); \
	       for (i=0;i<=m;i++) d[i,0]=i; for (j=0;j<=n;j++) d[0,j]=j; \
	       for (i=1;i<=m;i++) for (j=1;j<=n;j++) { \
	         cst = (substr(a,i,1)==substr(b,j,1)) ? 0 : 1; \
	         x=d[i-1,j]+1; y=d[i,j-1]+1; z=d[i-1,j-1]+cst; \
	         d[i,j] = (x<y?x:y); if (z<d[i,j]) d[i,j]=z } \
	       return d[m,n] } \
	     { s=lev(tolower(g), $$0); if (index($$0,tolower(g)) || index(tolower(g),$$0)) s=s-4; printf "%d %s\n", s, $$0 } \
	   ' | sort -n | awk -v g='$@' 'BEGIN{lim=(length(g)>6?4:3)} $$1<=lim{print $$2}' | head -3 | paste -sd" " -); \
	 if [ -n "$$near" ]; then dim "did you mean: $$near"; fi; \
	 dim "'make help' lists every target grouped by stage; 'make status' shows what is already built"; \
	 exit 1
