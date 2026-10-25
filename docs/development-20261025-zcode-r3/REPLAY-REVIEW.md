# P0-2 M4 replay qualification-evidence wiring — independent ZCode product hard review

Reviewer: independent ZCode GLM-5.3/max session `sess_5cf8971a-54d0-4748-ad5a-f68c8067904c`
(replay-hard-review-001 → bounded recovery replay-hard-review-002; prior CLI attempt ended
native 1 after provider HTTP502 — preserved, never counted). Reviewer took no part in the
development. Effective plan: `M4-REPLAY-PLAN.md` (2026-10-25 author-disposition EFFECTIVE,
10643 bytes, SHA-256 `346ca2596fe85110b4a258793e0e7a2684a0bc76c612d10033714baed969c018`,
re-verified at review start) plus author disposition. No source modifications, no
commits/pushes, no nested agents, no network/DB/SDK/broker/credentials.

## 1. Reviewed scope and integrity chain

Candidate `D:\AKWorktrees\aktr-r3-replay-20261025`, branch `work/zcode-r3-replay-20261025`,
HEAD = baseline `0cc305454d06643e93b14e426d04a6a755094d84`.

| File | Bytes | SHA-256 (start) | SHA-256 (end) | FREEZE match |
| --- | --- | --- | --- | --- |
| `src/ak_trading/v2/decision/replay.py` | 40650 | `0226b0f2ff5524f81f8327611260d6c7b4e87d46b593b4a514ea772b6735af64` | identical | yes |
| `tests/test_v2_m4_replay.py` | 58147 | `afe72aceae18723fc8418e2bb99a84d947db72627c25c6bf275e1035e6ede182` | identical | yes |

Whole-tree diff vs baseline: only these two files (`replay.py` +15/−3 across 18 lines;
test file +391/−0). Producing paths of the open baseline blocker are byte-identical to
baseline (`tests/test_v2_m4_payoffs.py`, `pipeline.py`, `artifacts.py`, `gates.py`,
`routing.py`, `ledger.py` — empty `git diff 0cc3054 -- …`; HEAD is the baseline commit and
`git status --porcelain` shows only the two write-set entries). `rg legacy_exit_binding
src/ak_trading/v2/decision/replay.py` → exit 1, empty output, recorded NO_MATCH
(optional discovery; the removed import has no dangling reference).

## 2. Code review findings (write set vs plan §3)

1. **Same-run dependencies (§3.1) — satisfied.** The qualification loop calls the real
   `verify_qualification_evidence(q, matching[0], qualification_evidence,
   contract_lock_sha256, registry, exit_registry_sha256=exit_registry,
   exit_binding=bindings_by_strategy[q.strategy_id])`, where `exit_registry` is this run's
   `exit_registry_hash_for(exit_specs)` (the same value given to ledger construction,
   restore, checkpoints and `build_strategy_context`'s `exit_binding=`). Nothing is read
   back out of the qualification as a trusted dependency; no files/globals read; no legacy
   binding generated. Functionally proven by probe P2a/P2b below (a chain bound to another
   real registry or a substituted run binding is rejected).
2. **Binding population boundary (§3.2) — satisfied.** Duplicate `strategy_id` check
   (`duplicate_exit_bindings`) precedes the dict build; the exact-spec-set check
   (`exit_bindings_must_match_specs_exactly`) still rejects missing/extra. `matching!=1`
   raises before indexing `bindings_by_strategy`, so no KeyError path. Entry/exit/scope
   attribution remains the verifier's job (unchanged `artifacts.py`).
3. **Fail-closed semantics (§3.3) — satisfied.** `except (ValueError, TypeError)` →
   `invalid_q` → `QUALIFICATION_EVIDENCE_MISSING/INVALID` unchanged; `ArtifactsError`
   inherits `ValueError`; SHADOW OPEN/ADD intents of invalid qualifications are cancelled
   (`MODEL_EXPIRED`) and gates marked `QUALIFICATION_MISMATCH`+token (replay.py:401-412,
   516-519).
4. **No relaxation (§3.4) — satisfied.** `artifacts.py` (verifier, six scenarios, review
   independence, pre-registered groups, thresholds), `gates.py`, `routing.py`, `ledger.py`,
   `pipeline.py` are byte-identical to baseline. Public API unchanged: `exit_specs`/
   `exit_bindings` and verifier `exit_registry_sha256`/`exit_binding` remain keyword-only
   required parameters without defaults (probe API checks).
5. **Tests (§2/§4) — real, not fake success.** The 5 new tests (+391 lines, pure
   additions) use the real verifier, real gates/risk/routing and the real
   `run_decision_replay`; only the scheduler context construction is a deterministic
   fixture (explicitly allowed), with the qualification attached the way the real
   `build_strategy_context` selects it and the run's own `exit_binding` passed through.
   Fixtures are in-memory, labeled SYNTHETIC/SCENARIO, no threshold/constant edits.

## 3. Independent reruns (exact argv, cwd = candidate root, Python
`D:/Programs/Python/Python312/python.exe`; raw streams in this directory)

1. **Focused replay file** (replay-hard-review-001, reused after this recovery verified
   the two file hashes unchanged — not repeated for bookkeeping):
   `["D:/Programs/Python/Python312/python.exe","scripts/test_offline.py","-q",
   "tests/test_v2_m4_replay.py"]` → native exit **0**; 20 selected / 0 deselected;
   **20 passed, 0 failed, 0 skipped** in 23.00s; stdout 297 B
   (`rerun1-replay-file.stdout.txt` preserved in `../replay-hard-review-001/work`),
   stderr 0 B.
2. **Mandatory 8-file full population** (this recovery; prior attempt interrupted by the
   CLI HTTP502 after 148 B of progress output — preserved as
   `interrupted-attempt1-*`, never counted): argv
   `["D:/Programs/Python/Python312/python.exe","scripts/test_offline.py","-q",
   "tests/test_v2_m4_payoffs.py","tests/test_v2_m4_gates.py","tests/test_v2_m4_routing.py",
   "tests/test_v2_m4_risk.py","tests/test_v2_m4_execution.py","tests/test_v2_m4_ledger.py",
   "tests/test_v2_m4_replay.py","tests/test_v2_m4_causality.py"]` → native exit **1**
   (real test failure exit, captured to `rerun2-eight-file.exit.txt` after process
   termination; wrapper-shell exit 0 is the capture harness only and is not the test
   verdict). 205 selected / 0 deselected, **191 passed, 14 failed, 0 skipped**,
   570.98s pytest (577s wall). stdout 18687 B SHA-256
   `3a9f66b0d46c331a2c44326ccaecc361d6ba3d5cc51e950370c3a41e7e25d445`, stderr 0 B. No
   `-k` truncation; the full population completed under dedicated supervision of its own
   handle (blocking await, no unrelated calls interleaved).

## 4. B01 baseline failure — independently assessed, preserved unresolved

All 14 failures are `tests/test_v2_m4_payoffs.py::test_forwarded_binding_keeps_frozen_baseline_value[*]`
(7 strategy families × m1/d1): frozen `_L2_BASELINE_BINDING_SHA256` constants do not match
the current `pipeline.legacy_exit_binding(spec)` producer (assertion detail in
`rerun2-eight-file.stdout.txt`). Assessment against raw evidence, not the owner's waiver:

- My 14 failing IDs == owner run2's 14 IDs (sorted byte-diff, `my-run2-failed-ids.txt` vs
  `owner-run2-failed-ids.txt`).
- My 14 failing IDs == owner's pristine-baseline run's 14 IDs at detached worktree
  `0cc3054…` (`baseline-payoffs.stdout.txt`: 80 selected, 66 passed / 14 failed,
  exit 1) — the same 14 fail with the write set entirely absent.
- The producing path (`test_v2_m4_payoffs.py`, `pipeline.py`) is byte-identical to the
  baseline commit in this candidate.

Conclusion: **B01 is a genuine pre-existing baseline problem** (frozen-constant drift in
the exit-binding registration owner's domain), outside the two-file write set, not a
regression of this repair. It is preserved as an unresolved blocker, not fixed, not
suppressed, and no constants were refreshed. Full M4 is **not** green. Per plan §5 this is
itemized and routed to the owning node; it does not gate this bounded product.

## 5. Independent counterexample probes (this directory; candidate untouched)

`probe_counterexamples_v2.py` → native exit **0**, 24/24 checks PASS
(`probe-v2.stdout.txt`, 1371 B; stderr 0 B). Reuses the owner file's private fixture
helpers (inspected line-by-line) with reviewer-chosen parameters; real production
verifier/gates/routing throughout:

- **API**: `exit_specs`/`exit_bindings` and verifier `exit_registry_sha256`/`exit_binding`
  keyword-only, required, no defaults.
- **P1 positive** (entry 105·NS, issued 90·NS, valid-until 300·NS — independent of the
  owner's numbers): real verifier accepts the chain; real SHADOW replay selects the OPEN,
  weight 1.0 / CASH 0.0, `qualification_ids` recorded, fill at 106·NS, no rejection tokens.
- **P2a cross-registry confusion**: chain bound to a genuinely distinct real registry
  (one exit spec's free-form `version` altered, registry recomputed — a real "another exit
  version registry") → verifier `qualification_exit_registry_mismatch`; replay gate
  `QUALIFICATION_MISMATCH`+`QUALIFICATION_EVIDENCE_INVALID`, ineligible, CASH 1.0, zero
  intents/fills. Proves the run's own registry is the verified dependency.
- **P2b run-binding substitution**: structurally valid forged binding (recomputed content
  hash) fed to the run against a chain bound to the true binding → verifier
  `qualification_exit_binding_mismatch`; gate mismatch, zero intents/fills.
- **P3 not-yet-effective** (issued 120·NS > decision 105·NS): chain stays verifier-valid;
  gate blocks `QUALIFICATION_EXPIRED`, nothing selected, CASH 1.0, zero fills.
- **P4 duplicate different bindings** for one strategy at the replay boundary →
  `duplicate_exit_bindings` (no dict folding).

Probe attempt 1 is preserved (`probe-attempt1.*`, exit 1): its P2a premise was wrong —
`legacy_exit_spec()` depends only on mode, so TREND-MINUTE and WAIT-MINUTE legitimately
share one registry and the product correctly admitted that genuinely valid chain. The
failure was the probe's premise, not the product; v2 derives a genuinely distinct
registry. No product behavior difference was observed between attempt and v2 other than
the corrected premise.

## 6. Plan §4 acceptance matrix

| Row | Evidence |
| --- | --- |
| Nonempty valid qualification → real verifier accept → SHADOW OPEN/ADD admitted | owner test + my P1 + focused 20/20 |
| Missing file / tampered bytes / rehashed narrowed scope → visible rejection, zero added risk | owner 7-case negative test (passed in my rerun1) + my P2a/P2b independent variants |
| Wrong binding / registry / spec → rejected, no substitution | owner cases + my P2a (real distinct registry), P2b (real forged binding) |
| Binding missing / extra / duplicate incl. dict-folding counterexample | owner boundary test + my P4 |
| Validity window `issued_at ≤ decision < valid_until`, REDUCE/EXIT safe channel | owner window-boundary + timer-exit tests + my P3 |
| Original semantics regression / no schema, validator or threshold change | diff scope (2 files only), unchanged producing modules, focused file's original 15 tests + 8-file population green outside B01 |

## 7. Verdict

**PASS — bounded, unconditional within scope.** The two-file write set implements the
effective plan §3 exactly, every in-scope requirement of §4 is independently evidenced,
and no counterexample against the bounded product was found. Bounds of this verdict, per
plan §5 and the review work order: B01 remains an open pre-existing baseline blocker
(preserved, owning node's to fix); full M4 acceptance is **not** granted (the 8-file
population is native-exit-1 with the 14 baseline failures); shadow/SHCP7/M5 acceptance is
not granted; research/simulation status unchanged; T7 pin coordination is a Root/P1-3
queue action upon accepted integration. No unmet in-scope requirement exists.

Safety: offline SYNTHETIC/SCENARIO validation only; no network/API/DB/services/
credentials/.env; TqSdk untouched; `enable_trading=False`, observer=`hold`,
broker=`NOT_RUN`, real send/cancel `0/0`.
