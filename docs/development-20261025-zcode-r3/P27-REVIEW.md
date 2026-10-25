# P2-7 Independent Product Hard Review (p27-hard-review-001)

- Date: 2026-10-25. Reviewer: independent ZCode `GLM-5.3` / `max`, fresh session and store; took no part in P2-7 development or repair.
- Candidate: `D:/AKWorktrees/aktr-r3-p27-20261025`, branch `work/zcode-r3-p27-20261025`, baseline HEAD `0cc305454d06643e93b14e426d04a6a755094d84` (verified at start and finish).
- Reviewed write-set (only): `src/ak_trading/futures/data.py`, `tests/test_futures_data.py`.
- Basis: WORKORDER `p27-hard-review-001`, owner `FREEZE.json`/`DELIVERY.md` (p27-owner-001), Astra r3 IMPLEMENTATION-PLAN §1/§5, REPAIR-ORDER P2-7 (r3-001). No new acceptance criteria invented; no plan review repeated.

## VERDICT: PASS (unconditional)

## 1. Freeze integrity (start and finish)

Verified by `reviewer_verify_bytes.py` (evidence: `evidence/bytes-verify-start.json`, `evidence/bytes-verify-finish.json`, both `overall_ok=true`, exit 0):

| File | Working sha256 (start = finish) | FREEZE frozen | HEAD blob sha256 | FREEZE prior_head |
| --- | --- | --- | --- | --- |
| `src/ak_trading/futures/data.py` | `c08e56484a3345eb66a0d6ca1c7dd87c36ee3a8a655c590d03a53d8ffe6e88b1` | match | `50db8742fea1aefb0b5b66a29688fdc40a1004de17bc120135fa0223994ce332` | match |
| `tests/test_futures_data.py` | `78eb9ff44d299d20c8bcb2eeeecfe6e7961945fc9cda899edb11e6eb0dd4c7a1` | match | `3375fe80403fd26bab4781cb0d4d0e60b37cff753eaaa412dcc725726ed79028` | match |

Because `core.autocrlf=true` can mask byte differences in a normalized git diff, an independent byte-level reconstruct check was added beyond git diff: for each file, reversing the single diff hunk on the working bytes reproduces the HEAD blob **byte-for-byte** (`reverse_reconstruct_equals_head_blob=true` both files, start and finish).

- `data.py`: exactly one delete hunk — `    "instance_observation_start",\n` (former line 49).
- `test_futures_data.py`: exactly one insert hunk — 16 lines (CRLF, matching surrounding file style), the new `test_star_import_binds_exactly_all_and_every_export_resolves`.

Git scope at start and finish: `M src/ak_trading/futures/data.py`, `M tests/test_futures_data.py`, diff stat `1 -` / `16 +`, nothing else modified, no new untracked artifacts in candidate. Full patch: `evidence/git-diff-write-set.patch`. Conclusion: the write-set is exactly the frozen two-file change; no hidden or unrelated changes.

## 2. Required independent reruns (raw argv, exit, streams preserved)

Interpreter `D:/Programs/Python/Python312/python.exe`, cwd = candidate root (recorded in `evidence/*.cwd`).

1. `scripts/test_offline.py -q tests/test_futures_data.py` — native exit **0**; stdout: `30 passed in 0.68s` (`Offline selection: 30 selected, 0 deselected`); stderr empty. Evidence: `evidence/rerun1-focus.{stdout,stderr,exit}`. Owner claim "30 passed" reproduced exactly.
2. `scripts/test_offline.py -q -rs tests/test_futures_backtest.py tests/test_futures_export.py tests/test_futures_export_connections.py tests/test_futures_readiness.py tests/test_futures_strategies.py tests/test_futures_study_pair_confirmation.py tests/test_futures_study_sample.py tests/test_futures_study_signals.py tests/test_futures_validation.py` — native exit **0**; stdout: `267 passed, 1 skipped in 109.41s`; stderr empty. Evidence: `evidence/rerun2-consumers.{stdout,stderr,exit}`. Owner claim "267 passed / 1 skip" reproduced exactly.

Scope note: this is a bounded rerun of the specified suites against the narrow P2-7 import-fix acceptance, **not** a full-suite PASS claim.

### Skip assessment

The single skip is `SKIPPED [1] tests\test_futures_validation.py:232: frozen real-input-02 bundle not present`. Source inspected (`tests/test_futures_validation.py:229-232`): `test_s1_frozen_real_input_bundle_still_readable` guards on `Path("data/strategy_artifacts/futures-lib-20260907-codex-01/real-input-02").exists()` and skips defensively ("never fabricate a read") when the frozen bundle directory is absent. The directory is confirmed absent on this machine (`test -d` exit 1, `evidence/bundle-dir-presence.exit`). The file is not in the write-set and the condition is unrelated to `__all__` or imports. Assessment: pre-existing environmental conditional skip, unrelated to this change, and not required by the narrow P2-7 acceptance. Reported as-is; not counted as a failure and not a reason to widen scope.

## 3. Independent fresh-process star import and export checks

Probe `reviewer_probe_star_import.py` (evidence `probe-star-import.json`, exit 0, stderr empty): candidate `src` pinned at `sys.path[0]`; both `ak_trading.__file__` and `ak_trading.futures.data.__file__` proven to resolve from the **candidate tree**, not the canonical editable install.

- `__all__` = 31 entries, all strings, all unique.
- Real star import (exec into a fresh namespace): bound set equals `__all__` exactly (`extra=[]`, `missing=[]`).
- Every declared export resolves on the module: `unresolved_exports=[]`.
- Star-bound objects are identity-equal (`is`) to the module attributes for all 31 names: `identity_mismatches=[]`.
- Normal named import of all 31 exports in one statement: succeeds.

## 4. Counterexample (one, proportionate; isolated process; in-memory only)

Isolated child process (`--counterexample`, frozen source never written): confirmed `instance_observation_start` is absent from both the module and `__all__`; appended it to `__all__` **in memory only**; the real star import then raised

```
AttributeError: module 'ak_trading.futures.data' has no attribute 'instance_observation_start'
```

— the exact C10 defect mode. Child asserted its precise expected outcome and exited 0 (`precise_expected_outcome=true`). Minimal reproduction is the child's mechanism itself (see `reviewer_probe_star_import.py::run_counterexample`). This demonstrates both the defect mechanism and that the new regression test (`exec` star import + binding-set + resolution asserts at `tests/test_futures_data.py:88-99`) would catch reintroduction. The owner's pre-change once-only action was not rerun or regenerated.

## 5. Contract conformance (REPAIR-ORDER P2-7, r3 plan §1/§5)

Effective REPAIR-ORDER entry (`docs/strategy-first-blueprint-20261015-r1/revisions/r3-001/REPAIR-ORDER.md:41`, evidence `evidence/rg-repair-order-p27-r2.stdout`): defect 成立; disposition "删过期导出或补真实所需实现，**不为填名字加空函数**"; acceptance "公开导入及实际消费接口符合原合同，必要行为验证与独立产品硬审完成；不连带重写 v1/v2 或更改策略语义".

- Delete-not-stub: owner deleted the stale export; independently supported — observation-start semantics live in `ContractInstance.research_window_start` (`src/ak_trading/futures/data.py:119`), exercised by existing tests (`tests/test_futures_data.py:193-196`) that passed in rerun 1. The stale name is now absent from all of candidate src+tests (`rg` exit 1, empty streams → NO_MATCH, `evidence/rg-stale-name.*`).
- Public imports conform: §3 above (star import exact, all exports resolve, identities hold).
- Actually-consumed interfaces conform: all 9 consumer test files rerun green (267 passed); no star-import consumer exists anywhere in src/tests/scripts — the only occurrence of the pattern is the new regression test itself (`evidence/rg-star-import-consumers.stdout`, single match `tests/test_futures_data.py:95`), so no existing consumer could break.
- No v1/v2 rewrite, no strategy-semantics change: byte-reconstruct proves the only source change is the one `__all__` line deletion.
- No missing original contract blocked this review: no blocking contract reference was identified (the original finalized plan is LOCAL_ONLY historical material; nothing in the effective REPAIR-ORDER or r3 plan requires the stale name).

## 6. Findings

Blocking findings: **none**.

- O1 (non-blocking, recorded as-is): pre-existing environmental skip in `tests/test_futures_validation.py:232` (frozen real-input-02 bundle absent), unrelated to this change (§2).
- O2 (non-blocking, §7/§8 discipline): first REPAIR-ORDER lookup at `docs/repo-audit-20261025-claude/REPAIR-ORDER.md` failed with rg native exit 2 (file does not exist; that directory contains only `REPORT.md` and `VERIFICATION.md`). Preserved in `evidence/rg-repair-order-p27.*`; resolved by locating the effective REPAIR-ORDER in the r3-001 revisions directory (exit 0). No other failed tools; no blanket ignoring; all native exits recorded per their own semantics (rg exit 1 + empty streams only where noted as NO_MATCH).

## 7. Boundaries

Reviewed code unchanged (hashes identical start→finish); no commits/pushes; no nested agents; no credentials/.env/environment dumps; no database/server/network/broker use; `enable_trading=False`, observer=hold, `broker=NOT_RUN`, real send/cancel = 0/0. All reviewer outputs confined to `D:/Maintenance/aktr-development-20261025/p27-hard-review-001/work/`; no writes into the candidate tree (offline runner used temporary basetemp, no cache artifacts left).

## 8. Evidence index

`work/`: `reviewer_verify_bytes.py`, `reviewer_probe_star_import.py`, `probe-star-import.json`, this `REVIEW.md`, `VERDICT.json`.
`work/evidence/`: `bytes-verify-start.*`, `bytes-verify-finish.*`, `git-diff-write-set.*`, `rerun1-focus.*`, `rerun2-consumers.*`, `bundle-dir-presence.exit`, `probe-star-import.*`, `rg-stale-name.*`, `rg-star-import-consumers.*`, `rg-research-window-start.*`, `rg-repair-order-p27.*` (exit 2, preserved), `rg-repair-order-p27-r2.*` (exit 0).
