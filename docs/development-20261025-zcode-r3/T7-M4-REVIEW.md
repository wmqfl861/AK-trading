# REVIEW — t7-m4-baseline-hard-review-001 (independent product hard review)

**Verdict: PASS — unconditional.** Scope of acceptance: the **test repair** of
`tests/test_v2_m4_payoffs.py` (T7 author003 §1 two-layer historical-binding
check; frozen revision `651933f48e4beb0d6afa1d50a4e3d857a9ad5f7dce102012a6ed489744c22b53`,
88,129 bytes, git blob `bbbac3a5…`, base commit `908604e`) together with the
**real full M4 nine-file result (259/259)** on candidate
`C:\AKWorktrees\aktr-r3-t7-m4-baseline-20261025`. This is **not** a blanket
strategy-profitability, T7 cold-chain, or L0/L1 acceptance, and it accepts
nothing outside the single frozen test file.

Reviewer: fresh independent ZCode GLM-5.3/max session; took no part in the
development or repair; owner session `sess_c7ffc367…` and the Astra author were
not consulted beyond their frozen written artifacts.

## 1. What was reviewed and how

Authority: T7-EXISTING-INTEGRATION-DISPOSITION (author003, EFFECTIVE
2026-10-25) §1, T7-PLAN/T7-PLAN-DISPOSITION, AGENTS §1–§10 (current main
copies, hash-verified). The repair contract: keep the fourteen parametrized
nodes, `_L2_BASELINE_BINDING_SHA256` and every other assertion; replace the
identity-version-mixed comparison with (1) a historical fixed-value check over
the fourteen complete six-field originals from the two repository records and
(2) a current forwarding check against the real producers — no expected-hash
refresh, no monkeypatch, no deleted historical assertions.

All review actions were performed by me in new processes with sealed streams:
REQUEST input pin verification, AST-level diff analysis vs HEAD, independent
re-extraction from both pinned repository records, an independent counterexample
probe against the real candidate producers, and fresh reruns of the three
post-repair commands. No optional-discovery (rg) condition was used anywhere.

## 2. Write-set and immutability

- `git status` in the candidate shows exactly one modification,
  `tests/test_v2_m4_payoffs.py`; HEAD is `908604e` (base commit per freeze).
- Working-tree hash/bytes match the frozen pin exactly; git blob matches
  `bbbac3a5…`; HEAD preimage hash matches `453614df…` (LF-normalized).
- Logical diff vs HEAD: **143 insertions / 8 deletions in three hunks only** —
  the `exit_binding_content_hash` import, two module-level frozen tables with
  provenance comments (`_L2_HISTORICAL_BINDING_ORIGINALS`,
  `_L2_HISTORICAL_DIVERGENCE_WITNESS`), and the rewritten body of
  `test_forwarded_binding_keeps_frozen_baseline_value`. The parametrize
  decorator, `_L2_BASELINE_BINDING_SHA256`, L-A/L-C, the registry-hash
  isolation test and the qualification-scope negative are untouched (AST-equal
  to HEAD). `src/` is untouched; no git writes were performed.
- Post-test re-read (after all reruns): the frozen file and all eight sibling
  M4 test files still match their REQUEST pins; both data records unchanged;
  git state unchanged (`work/HASHES.json`, 0 pin failures).

## 3. Provenance — re-extracted myself, repository records as oracle

All 29 REQUEST inputs re-hashed **MATCH** (`work/pin-check.json`). From the
pinned main-repo originals (the candidate worktree carries no `data/` copies —
see FINDINGS O3):

- `m5-r3-smoke-chunk01.json` → `spec.exit_bindings` is a **list of exactly 14
  complete records**, each with all six hash fields plus `strategy_id` and the
  recorded `binding_sha256`.
- `l2-baseline-divergence.json` → 14 `combos` with `spec_code_sha256`,
  `m3c_array_spec_sha256` and the m3b side of `binding_diffs`; its
  `registry_check` has `registered_count=14` and
  `m3b_matches_registered=true` with `m3b_binding_sha256 ==
  registered_binding_sha256` for all fourteen.

My extraction equals the inline frozen tables field-for-field; the baseline
table equals the registration `binding_sha256` values; the real M1 formula
(`contracts.exit_binding_content_hash`) over **my** extracted six fields
reproduces the recorded binding for all 14. The inline fixture is therefore not
the sole oracle.

## 4. Counterexamples (reviewer-authored, against the real candidate producers)

Module identity first: the probe resolved `ak_trading` / `ak_trading.v2` under
`…\aktr-r3-t7-m4-baseline-20261025\src`; the offline launcher additionally
strips inherited `PYTHONPATH` and pins each child to
`<bootstrap>;<candidate>\src` (author003 §4 satisfied).

- **Historical tamper, 6 fields × 14 nodes = 84 checks** with a mutation set
  distinct from the test's own (strategy_id suffix, first-char entry flip, exit
  replaced, sizing made non-null, policy swapped, rules bumped): every single
  mutated field breaks the layer-1 hash match.
- **Current equality, all 14 nodes**: `pipeline.legacy_exit_binding(spec) ==
  select_exit_binding(spec, profile="legacy")` (full record); entry ==
  `spec.code_sha256`; exit == real M3C
  `exits_registry.exit_spec_sha256(exits_registry.legacy_exit_spec(mode))`
  (byte-exact via `registry.exit_spec_identity`); priority and rules version
  continuous with the historical constants; binding == the M1 canonical formula
  over the current six fields.
- **Changed entry**: `replace(spec, code_sha256="0"*64)` regenerates the
  binding identically through both entries (and equals the formula over the
  synthetic digest) while differing from the original.
- **No fabricated historical spec**: attaching the *historical* entry digest to
  a current spec reproduces **neither** the historical binding nor the current
  one — the six historical fields can only be consumed as raw hash material,
  exactly as the repaired test does.
- **strategy_id** (in the projection) moves the binding; **version /
  parameter_version / parameters-only** changes keep the binding equal while
  `pipeline.registry_hash_for` moves; the untouched qualification-scope
  negative rejects the mix (ran and passed in the full-80 rerun).
- **Identity divergence is real and per-node**: current entry, exit and binding
  differ from the historical originals on every node (registration-event
  consequence, arbitration RULING 1.1/1.2). The old mixed comparison provably
  failed all 14 (owner pre-repair records cross-checked: 0/14 focused, 66/80
  full file — consistent with my independent finding).

## 5. Required reruns (fresh, my own processes, sealed streams)

| # | Command (order per owner DELIVERY) | Native exit | Result |
|---|-----------------------------------|-------------|--------|
| 1 | `python -B -X utf8 scripts/test_offline.py -q tests/test_v2_m4_payoffs.py -k test_forwarded_binding_keeps_frozen_baseline_value --basetemp <evidence>/temp/post-focused14` | **0** | selected 14, ran 14, **passed 14**, failed 0, deselected 66, skipped 0, errors 0; stderr 0 bytes |
| 2 | `python -B -X utf8 scripts/test_offline.py -q tests/test_v2_m4_payoffs.py --basetemp <evidence>/temp/post-fullpayoffs` | **0** | selected 80, ran 80, **passed 80**, failed 0, skipped 0, errors 0; both isolation tests in population; stderr 0 bytes |
| 3 | `python -B -X utf8 scripts/test_offline.py -q tests/test_v2_m4_payoff_paths.py tests/test_v2_m4_payoffs.py tests/test_v2_m4_replay.py tests/test_v2_m4_ledger.py tests/test_v2_m4_gates.py tests/test_v2_m4_routing.py tests/test_v2_m4_risk.py tests/test_v2_m4_execution.py tests/test_v2_m4_causality.py --basetemp <evidence>/temp/post-m4-ninefile` | **0** | selected 259, ran 259, **passed 259**, failed 0, skipped 0, errors 0, collection_errors 0, all nine files collected, 959.80s (owner: 970.98s) within the heavy-lane budget; run in background lifecycle, streams sealed after termination; stderr 0 bytes |

`<evidence>` = `C:\AKWorktrees\aktr-r3-t7-m4-baseline-review-001-evidence-20261025`
(temps/probes per this review's work order; the only redirection from the
owner's literal paths). Original populations, `-k` expression, argument order,
cwd, launcher and budgets preserved; no skips, subsets or threshold changes.
Counts equal the owner's DELIVERY on all three commands.

## 6. Non-blocking observation (O1) — owner freeze timestamp label

`work/SOURCE-FREEZE.json` `frozen_at: 2026-10-25T17:30+08:00` (= 09:30Z) is
inconsistent with the actual write facts: the freeze file's mtime is
2026-10-26T00:27:39Z, the owner TERMINAL ended 00:29:14Z, and the machine's
local offset is UTC-7 — the label looks like local 17:30 rendered with a wrong
`+08:00`. Recorded as unverified metadata; not rewritten, not backdated; the
exact pins and independent results are decisive and all verified. Details in
`FINDINGS.json`.

## 7. Product-contract statement

No product contract failure was encountered: for every current legal spec the
M4 forward and the M3B producer agree exactly (fields, canonical encoding,
hash), so no production repair was needed — matching the owner's statement.
The fourteen pre-repair failures were the test's identity-version mixing,
exactly as diagnosed by author003 §1.

## 8. Boundaries honored

`enable_trading=false`, observer `hold`, broker `NOT_RUN`, real send=0,
cancel=0. No credentials/`.env`/agent-config/model/provider reads or writes; no
network/SDK/broker/DB/service/client-UI/git writes; candidate worktree strictly
read-only for the reviewer; evidence only under the assigned `C:` evidence root
and this `work/` directory. Reviewer-side probe iteration disclosed in
`FINDINGS.json` (harness fix only; no frozen input touched).

## 9. Evidence index

`VERDICT.json`, `FINDINGS.json`, `HASHES.json`, `pin-check.json`,
`reviewer-probe.json`, `diff-vs-head-…patch`, `probe-reviewer-*.txt`,
`post-repair-focused14-*.txt`, `post-repair-full-payoffs-*.txt`,
`post-repair-m4-ninefile-*.txt` (all in this `work/` directory); probe scripts
under `<evidence>/probes/`.
