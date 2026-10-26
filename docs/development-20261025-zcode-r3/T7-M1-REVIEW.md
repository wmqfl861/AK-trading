# T7-M1 version hard review — r1 (independent), 2026-10-25

**Verdict: PASS (unconditional).** Scope: the author003 five-file write-set frozen in
`t7-m1-version-owner-002/work/SOURCE-FREEZE.json`, accepted against
`T7-EXISTING-INTEGRATION-DISPOSITION.md` §2 (EFFECTIVE), the T7 plan/disposition, and the
original acceptance lineage (E-P1 RULING 6 command list, EP1-M1 implementation §1.2–§1.3).

Reviewer: independent ZCode GLM-5.3/max session; no part in the plan disposition, the
author003 implementation, or the owner-002 delivery. Candidate
`C:/AKWorktrees/aktr-r3-t7-m1-version-20261025` at baseline `908604e`, product source
untouched by this review (post-review drift check: 0 failures; git status still shows
exactly the five write-set files).

## What was independently executed

| Check | Outcome | Evidence (under `C:/AKWorktrees/aktr-r3-t7-m1-version-review-001-evidence-20261025`) |
| --- | --- | --- |
| Input pin verification (44 pins: 5 frozen files, 5 preimages, 34 REQUEST inputs) | 0 failures, exit 0 | `hash-pins-001/RESULT.json` |
| Change-scope audit vs preimages | numstat `2/2, 1/1, +25/-1, 2/2, +6/-0` matches freeze; normalized diffs contain only claimed changes | `change-scope-001/*.diff` |
| Identity pre-check (process-only PYTHONPATH, 4 modules under exact candidate) | exit 0; v2.1.5 / M1-v2.1.5 / devplan-v2.1.7 printed | `identity-precheck-001/` |
| **Rerun owner command 1** — RULING 6 original M1 five-file argv | **exit 0; selected 39, ran 39, passed 39, failed 0, skipped 0, errors 0** (owner claim reproduced; includes `test_promoted_contract_version_bindings`) | `run-m1-five-001/` |
| **Rerun owner command 2** — remaining M1 group (clock/data/macro/policy) | **exit 0; selected 18, ran 18, passed 18, failed 0, skipped 0, errors 0** | `run-m1-remaining-002/` |
| Full-population coverage | `PUBLIC_TEST_FILES` == exactly the 9 files across both commands (57 tests) — the node's full lock-required population | verdict JSON |
| Independent counterexamples (4 risk-matched groups, 39 probes) | 39/39 expected outcomes, exit 0 | `counterexamples-001/RESULT.txt`, `counterexamples-001-stdout.txt` |

## Counterexample groups (reviewer-authored, real behavior)

- **A — Legacy contract rejection.** The real strict decoder rejects the frozen fixture
  with root `contract_version` reset to legacy `v2.1.3` and to unknown `v2.1.9`
  (`contract_version_mismatch`); the record-file envelope codec rejects a legacy
  `contract_version` the same way; the real `build_contract_lock` emits
  `ak_probability_strategy_contract_v2.1.5` with `devplan-v2.1.7` and lock schema
  `v2.1.0`, the real `verify_contract_lock` accepts the freshly generated SYNTHETIC lock,
  and rejects it after tampering `contract_version` back to v2.1.3
  (`lock_contract_version_mismatch`).
- **B — Unrelated schema preservation.** All thirteen other schema IDs are unchanged
  (checkpoints v2.1.3, metrics v2.1.4, exit states/bindings v1.0.0, contract lock v2.1.0,
  input v2.1.0, shared fixture v2.1.0, seasonality v2.1.1, factor registry v2.1.1, input
  collection v2.1.0, payoff training plan / payoff path v1). The EP1 allocation still
  targets experiment/research_config v2.1.3 while its `contract_version_target` is now
  realized by the exported constant. The real generated lock's schema table equals the
  live `_schema_hash_map()`. `artifacts.py` still consumes the qualification experiment
  v2.1.1 protocol and keeps the `payoff_training_plan_sha256` registration declarations.
  The `ExitPolicyBinding` codec roundtrip is unchanged.
- **C — Fixture-only root version drift.** Predecessor and candidate `shared.json` are both
  23304 bytes; the parsed structures differ in exactly one key (`contract_version`); and
  the candidate bytes are byte-identical to the predecessor with only the version string
  swapped. The legacy literal occurs exactly once in the predecessor and never in the
  candidate; the promoted literal exactly once in the candidate. Economic data untouched.
- **D — Lock tamper detection.** The real verifier rejects a tampered `fixture_files`
  hash, a tampered `required_tests` entry, and a tampered `document_version`
  (distinct `ArtifactsError` labels each).

## Acceptance mapping (disposition §2 → evidence)

Every row of the §2 decision table was verified: both identity constants promoted as
specified; `DOCUMENT_VERSION` kept at `devplan-v2.1.7`; experiment/research_config kept at
v2.1.3; every other schema version kept; the shared fixture's root `contract_version`
synced because the real decoder strictly rejects mismatches (demonstrated, not assumed);
exit-contract assertions synced with all structure assertions kept; the artifacts and
payoff-path test changes limited to version/allocation/generation-verification
regressions; the original RULING 6 five-file command and the remaining M1 required tests
rerun independently with native exit 0 and zero skips. No L0/L1 was published; the only
locks exercised anywhere in this review are temporary SYNTHETIC locks under pytest temp
trees and this reviewer's evidence root.

## Findings

No blocking findings. Two non-blocking observations and two preserved reviewer-probe
failures (both reviewer script bugs, fixed and rerun) are recorded in `FINDINGS.json`:
preimage line-ending formatting makes raw diffs whole-file (content equivalence proven
after CR-normalization), and the SOURCE-FREEZE `predecessor_preimages.root` field embeds
a parenthetical description that programmatic consumers must strip.

## Boundaries

This PASS covers only the frozen five-file author003 write-set against the cited
acceptance. It grants no credit for formal L0/L1 publication (next stage, gated on B's
final F freeze and a real independent R0), no M4/M5 acceptance, no fit/research run, no
deployment, and nothing beyond the existing trading boundary
(`enable_trading=False`, observer `hold`, broker `NOT_RUN`, real send/cancel `0`).
