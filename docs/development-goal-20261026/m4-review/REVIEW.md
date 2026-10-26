# REVIEW — m4-hard-review-002 (independent hard review, final M4 one-file repair)

**Verdict: PASS — unconditional** (`VERDICT.json`; `conditional: false`). Scope:
the final M4 one-file repair in candidate `C:\AKWorktrees\aktr-goal-m4-final-20261026`
— owner003's EOL publication-scope repair over owner002's logical merge of the
T7 author003 §1 two-layer historical-binding repair with main's accepted A2
natural-expiry fixture — at frozen bytes
`tests/test_v2_m4_payoffs.py` sha256 `2dbd9d62103148e4cc3d0da00fe95484ea7d584887c560c2d3390e3a15885677`
(92,591 bytes), together with the independently rerun full M4 nine-file result
(259/259, native exit 0) on this exact candidate. No acceptance beyond this
file on this candidate; no blanket T7/cold-chain/L0/L1/MINUTE-capacity
acceptance.

Reviewer: independent ZCode GLM-5.3/max (same session as the prior M4 baseline
hard review, resumed per REQUEST); took no part in any M4 development.

## 1. Authority and lineage

EXECUTION-CLARIFICATION §1/§4/§5 (EFFECTIVE 2026-10-26), author003, T7
plan/disposition — all REQUEST-pinned. Lineage verified at byte level:

| Stage | sha256 | bytes | Evidence state |
|---|---|---|---|
| main baseline (A2-accepted) | `d62959fb…` | 77,874 | read-only oracle |
| predecessor (m4-owner-002 merge) | `6912dff1…` | 92,262 | ran pre 14F/exit1; post 14P/80P/259P all raw0 (raw streams cross-checked, stderr empty) |
| **final (m4-owner-003 EOL repair)** | `2dbd9d62…` | 92,591 | frozen; this review's target |

## 2. EOL restoration — reproduced independently (14/14 checks)

My own binary alignment (`work/eol-ast-verification.json`), not the owner's
script: final↔predecessor **AST identical** and **LF-normalized text
identical**; exactly **329 lines** differ in bytes vs predecessor, every one
CRLF-in-final where the predecessor had normalized to LF; **every logically
equal line is byte-identical to main**; all 329 CRLF lines lie in equal
segments (restored originals, not new content); the **A2 fixture region
(final lines 1215–1292) is byte-identical to main** — the natural-expiry
positive (third decision exactly on the saved expiry, `<= expiry` legal,
counts 1,2,3 → OPEN) and the **expiry+1ns negative control** (restart at 1,
WAIT, never OPEN) are preserved intact.

## 3. Contract compliance of the merged test

- **Logical delta vs main confined exactly to the §1 repair**: import
  extension, the two frozen provenance tables, and the rewritten fourteen-node
  test. The parametrize decorator, `_L2_BASELINE_BINDING_SHA256`, L-A/L-C,
  the registry-hash isolation test and the qualification-scope negative all
  sit in byte-identical segments. No skips, no node changes, no hash refresh.
- **Biconditional, not unconditional inequality** (test lines 387–388):
  `(forwarded.binding == historical binding) iff (current six-field
  projection == historical projection)` — exactly the clarified contract.
  The unconditional `!= entry` / `!= list(exits)` tails are gone.
- All layer-1 checks retained: registration↔witness field agreement,
  historical shapes, M1 recompute to the frozen baseline, six single-field
  tampers. Layer-2 checks retained: real-producer equality, entry ==
  `spec.code_sha256`, exit == real M3C identity, changed-entry counterexample.

## 4. Independent verification and counterexamples (`work/reviewer-probe.json`, 17/17)

- All 46 REQUEST pins MATCH; module identity resolved under the exact
  candidate `src` (launcher pins child `PYTHONPATH`; probe asserted resolved
  paths).
- Tables re-extracted by me from both repository records (registration: 14
  complete six-field records; divergence witness: 14 combos,
  `registry_check` m3b_matches_registered 14/14) equal the AST-extracted
  frozen tables; baseline table == registration binding hashes; real M1
  formula over my extraction reproduces every recorded binding.
- **84 reviewer-authored tampers** (6 fields × 14, distinct mutations) all
  break the layer-1 hash.
- Current layer on all 14 nodes: forward == producer (full record), entry ==
  current code digest, exit == real M3C hash, binding == M1 formula over the
  current projection.
- **Biconditional verified as a fact per node.** Measured reality on this
  candidate: **exit identity equals historical 14/14** (the convergence that
  broke the old unconditional assertion), **entry differs 14/14**, hence
  projection and binding differ 14/14 — derived, not hardcoded.
- **current==historical projection case through the REAL producer**: on all
  14 nodes, `select_exit_binding(replace(spec, code_sha256=historical_entry))`
  reproduces the *complete* historical six-field projection and the
  historical baseline binding — equal projection ⇒ equal binding proven on
  real code.
- Changed-entry `"0"*64` regenerates identically through both entries and
  differs from the original; version/parameter_version/parameters-only
  changes keep the binding equal while `pipeline.registry_hash_for` moves;
  the qualification-scope negative passed in the full-80 rerun.

## 5. Required reruns on the exact final bytes (fresh processes, sealed streams)

| Command (owner's order; cwd = candidate; candidate's offline launcher) | Budget | Native exit | Result |
|---|---|---|---|
| focused14 (`-k test_forwarded_binding_keeps_frozen_baseline_value`, basetemp `work/temp/post-focused14`) | 300 s | **0** | 14 selected / **14 passed** / 66 deselected / 0 failed-skipped-errors; stderr 0 B |
| full payoffs (`tests/test_v2_m4_payoffs.py`, basetemp `work/temp/post-fullpayoffs`) | 600 s | **0** | 80 selected / **80 passed** / 0 failed-skipped-errors; both isolation tests in population; stderr 0 B |
| full M4 nine-file (payoff_paths, payoffs, replay, ledger, gates, routing, risk, execution, causality; basetemp `work/temp/post-m4-ninefile`) | 2400 s | **0** | 259 selected / **259 passed** / 0 failed-skipped-errors, collection_errors 0, all nine files; **1108.70 s**; stderr 0 B; background lifecycle, sealed after termination |

**Process-tree peak** (methodology: recursive child-tree `WorkingSetSize` sum
anchored to the run-unique `post-m4-ninefile` command-line marker, poller and
its bash ancestors excluded; 82 samples @10 s): **539.2 MiB** — within the
2 GiB exclusive lane. Poller validated by a marker smoke test first; two
reviewer-side poller defects (PID translation, self-match deadlock on a smoke
task that was stopped) and one read-only diagnostic error flood are disclosed
in `FINDINGS.json` harness notes; none touched any product process or frozen
input.

## 6. Post-test immutability

Full-tree re-verification after all runs: **584/584 non-write-set files
byte-identical** to owner003's `ROOT-CANDIDATE.json` staging manifest; the
write-set file matches its frozen REQUEST pin (`2dbd9d62…`, mtime
13:50:02Z, unchanged since before the owner's freeze); main/predecessor
lineage files and both data records unchanged (`work/HASHES.json`).

## 7. Non-blocking observations

- **O1 (metadata)**: owner `frozen_at` "2026-10-26T07:05+08:00" is
  inconsistent with the actual chronology (final file 13:50:02Z → owner
  focused14 13:51:03Z → freeze file 13:51:33Z → terminal 13:52:59.683608Z;
  local offset UTC-7). Per the work order, actual terminal chronology used;
  string treated as owner-authored unverified metadata; not rewritten.
- **O2 (informational)**: `ROOT-CANDIDATE.json` is the *staging preimage*
  manifest (its write-set entry still shows predecessor bytes by design);
  final pins live in SOURCE-FREEZE/REQUEST.
- **O3 (factual context)**: the exit-identity convergence (14/14 equal
  historical; entry still differs) is why the old unconditional assertion
  failed 14 nodes; the biconditional handles both directions lawfully.

## 8. Boundaries

`enable_trading=false`, observer `hold`, broker `NOT_RUN`, real send/cancel
0/0. No `.env`/credential/config/model reads or writes; no
network/SDK/broker/DB/service/UI/git writes; candidate tree read-only; all
reviewer writes confined to `…/zcode-dispatch/m4-hard-review-002`; no open
parent streams included in any seal; no optional rg condition used; native
exits recorded for every command. Stop after verdict.
