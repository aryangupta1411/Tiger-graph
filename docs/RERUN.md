# Live re-run: one clean pass over the 20 exam cases

This is the ordered command list for the final live pass after the R2 / SAR / query fixes. It re-installs the
fixed GSQL queries, clears the agent's write-backs from superseded runs, runs the 20 cases one at a time in
`opened_at` order with the live graph and the free model backend (`LLM_BACKEND=cli`), then promotes the answers.
Run it from the repo root. Everything before step 3 is offline.

Budget: about 1-3 min to install the queries, then about 2-3 min per case (roughly one hour for all 20). Keep
the machine awake for the whole run.

## 0. Offline pre-flight

The working tree must be committed, so each case in the run manifest records a clean git sha (`git_dirty: false`).

```bash
git status --short
```

Expect no output. If anything is listed, commit it first.

```bash
make test lint
```

Expect the unit tests and `All checks passed!` from ruff.

Do not run `make sheet` here: it rewrites the `latency_s` field in `engine/out/*.json` and leaves the tree dirty.
The committed sheet and drafts are the offline prediction this run is compared with in step 4.

## 1. Wake the workspace

```bash
make awake
```

Expect the TigerGraph version to print. A suspended Savanna workspace can take up to 4 minutes to answer.

## 2. Clear the agent's own write-backs from earlier runs

Superseded runs left rows in the graph that a re-run's upserts do not remove. Known examples are a stale pending
`AP-AC-HHG-011-BLOCK_CARD` approval and `CASE_CONNECTED_TO` edges on `AC-HHG-019`. The tool only touches
`AgentCase AC-HHG-0NN`, `CaseEvent AC-HHG-0NN-###` and `Approval AP-AC-HHG-0NN-*`, and only for HHG-001 to HHG-020.
Deleting an AgentCase vertex also drops its edges.

Dry run first. This lists the rows and writes the list to `runs/live-final-2/stale_writebacks.json`:

```bash
make clear-writebacks RUN_ID=live-final-2
```

Expect a table of AgentCase, CaseEvent and Approval rows for the exam cases, and no row for any other id. Read it
before you delete anything.

```bash
make clear-writebacks APPLY=1 RUN_ID=live-final-2
```

Expect `no agent write-backs left for these cases`, with `remaining : 0`. The command exits 1 if any listed
vertex is still there.

The Streamlit approvals queue (`ui/approvals.sqlite`) is a local file, not the graph, and this step leaves it alone. If
it still shows decisions from earlier runs, move it aside before the demo (`mv ui/approvals.sqlite ui/approvals.old.sqlite`).

## 3. Run the 20 cases

`rerun-live` wakes the workspace, then runs `make install-queries` (every `graph/queries/*.gsql`, including the
changed `card_profile`, `case_context`, `episode_candidates`, `device_neighbors`, `similar_prior_cases`,
`similar_prior_cases_agent` and `similar_cases_structural`, plus the earlier `shared_origin_scan` fix) and
`make describe`. It then runs each case with `--resume`, sets `LLM_BACKEND=cli RUN_MODE=live` in the environment
(`.env` is not edited), and validates each answer as soon as it is written. It stops at the first failure.

```bash
make rerun-live RUN_ID=live-final-2
```

Expect 20 `case n/20` banners, each followed by an `OK HHG-0NN ...` line and a clean validator line, and then
`20/20 answers in runs/live-final-2`. If a case fails, the target stops and names it. For an agent error, fix the
cause and run the same command again, and `--resume` skips the finished cases. For a validation failure, inspect
`runs/live-final-2/HHG-0NN/`, delete that one case directory, and run the command again.

If `install-queries` fails to compile `card_profile` or `case_context` (they have new as-of code paths), the
previously installed version stays live and would leak whole-history card statistics. Stop, fix the query, and
start step 3 again.

## 4. Compare with the engine's offline sheet

The engine drafts in `engine/out/` are the offline prediction for every case. This check prints each case where the
live answer differs on the compared fields:

```bash
uv run python -c "import json,glob,sys; R=sys.argv[1]; L=lambda p:json.load(open(p)); S=lambda xs:sorted((x['action'],x['route']) for x in xs); F=lambda a:(a['case']['verdict'],a['case']['pattern'],sorted(a['case']['affected_txn_ids']),a['case']['exposure_usd'],a['sar']['file'],a['case']['status'],S(a['next_best_actions']['initial']),S(a['next_best_actions']['final'])); [print(c,'live',F(a),'p',a['case']['fraud_probability'],'| sheet',F(e),'p',e['case']['fraud_probability']) for c,a,e in ((p.split('/')[-2],L(p),L('engine/out/'+p.split('/')[-2]+'.draft.json')) for p in sorted(glob.glob('runs/'+R+'/HHG-*/answer.json'))) if F(a)!=F(e) or abs(a['case']['fraud_probability']-e['case']['fraud_probability'])>0.10]" live-final-2
```

Expect no output: verdict, pattern, episode, exposure, SAR decision, status and both action sets (with routes, in any
order) equal the sheet, and the probability is within 0.10 of it. Check any case it prints by hand.

Things to check by eye in `runs/live-final-2/`:

- HHG-003 and HHG-018: `CREATE_CASE` plus `BLOCK_CARD` (L1, reason cites R2) in both the initial and final sets, no
  evidence request, and the customer is never asked to verify. HHG-018 also has `ESCALATE_TO_ANALYST`.
- HHG-004 and HHG-011: one `step_up_auth` request, `BLOCK_CARD` (L1, R2) in both sets, and `MONITOR_CARD` plus
  `DECLINE_TRANSACTION` added in final.
- HHG-018: `affected_txn_ids` is `[3485990, 3490180, 3491361]`, exposure is `$197.11`, and the first suspicious
  transaction is `3485990`.
- HHG-014: the `device_neighbors` evidence cites 4 closed cases (CC-2649, CC-2971, CC-2985, CC-3035) and never "44".
  The SAR says the block awaits team-lead approval, quotes the card's as-of history (71 prior transactions or 73 at
  opening, maximum $225.94), and does not deny prior reports on the device profile.
- HHG-015: no evidence line calls 3462385, 3462387 or 3462404 "normal continued use".
- Every summary is 2-6 sentences and at most 700 characters. No `what_changed` says "from X to X". Every action
  reason starts with R1-R10, 3a or a § section.
- No case lists its own `AC-HHG-0NN` in its memory pack (`runs/live-final-2/HHG-0NN/phases.jsonl`, phase P1).

## 5. Promote and validate

```bash
make promote RUN_ID=live-final-2
```

Expect the validator over `runs/live-final-2/*/answer.json` to report 20 clean, and then `cases/MANIFEST.md` to be
rewritten with a run_id and git sha for all 20 cases.

```bash
make validate
```

Expect `clean : 20` and `problems : 0`. CI runs this same check on `cases/HHG-*.json`, so do not push until it passes.

## 6. Refresh the numbers in the docs

The run numbers and some replay figures in the docs predate this pass:

- `docs/blog.md`: the run table (completion, agreement, latency, tokens), the run-provenance paragraph (all 20 now
  come from one clean pass), the HHG-018 sentence (the episode now has three transactions), and the replay table
  (from `engine/out/replay_metrics.json`: pattern 0.878, SAR decision 0.939, episode Jaccard 0.792).
- `README.md`: fill in the `## Results` section from `cases/MANIFEST.md`.

## 7. Commit, then push with the owner's go-ahead

`runs/*` is gitignored except the one committed run (`runs/live-final-2/`, the source of `cases/*.json`); a fresh
rerun should replace it the same way.

```bash
git add cases docs README.md runs/live-final-2
```

```bash
git commit -m "Promote the clean live re-run of all 20 cases"
```

```bash
git push origin main
```

After the push, GitHub Actions runs ruff, the unit tests, the validator on `cases/`, the mock end-to-end case
(which uses `tests/fixtures/case_pack_HHG-014.csv` because `data/raw/` is not in the repo) and the UI smoke test.
All of them should pass.
