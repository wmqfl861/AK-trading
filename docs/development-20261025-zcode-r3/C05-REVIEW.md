# C05 Final Independent Product Hard Review (c05-product-hard-review-005, closed by -006 continuation)

Date: 2026-10-25. Same independent C05 reviewer (original SID/store/cwd, GLM-5.3/max, unchanged configuration). Scope: unconditional bounded hard review of the frozen two-file C05 write-set (`SOURCE-FREEZE006`) under the effective C05 plan (SHA-256 `73b8e13e…`, disposition `3c5310a6…`), using the owner's closed 007 results plus my own single independent 11-step batch, which completed naturally at 2026-10-25T22:33:16Z.

## VERDICT: PASS — bounded to the two frozen write-set files

`scripts/test_offline.py` (`5d9f3b05…`, 28967 bytes) and `tests/test_offline_launcher.py` (`300ef264…`, 61083 bytes) implement the effective plan's import-domain isolation contracts, and every required original validation command was independently executed by this reviewer with captured native exits on byte-exact frozen inputs. This PASS is unconditional for the bounded write-set. It is NOT a claim that the full C05 baseline is green, that the remaining business/pin failures are fixed, or that any research/run qualification is granted. Historical pin mismatches and business failures are preserved and honestly classified below; they do not become "conditional PASS" caveats (plan §5 explicitly forbids that reading).

## Verified source pins (before AND after the batch, identical)

- `scripts/test_offline.py` `5d9f3b0590095d70ad200cccf12165ebd8dcda83bda5cc9863c4378405e75eb4` (28967)
- `tests/test_offline_launcher.py` `300ef264e90fd24448f65942bfbee57f4d06c50ea07ec2869dbdd1e554e0860a` (61083)
- read-only input `src/ak_trading/testing/offline_guard.py` `e0a667d5…` (15397; outside write-set, unchanged)
- effective plan `73b8e13e…` / disposition `3c5310a6…`; baseline `0cc30545…`; freeze lineage `4509681f…/cd12cb37…` → `f77297d2…/55d7f915…` → `70f2f939…/1c633488…` → frozen `5d9f3b05…/300ef264…` all preserved.

## My independent batch (single instance, OS-detached supervisor PID 139988, manifest/PID published before waiting, exclusive lock, no-overlap verified by process scan before launch; ran 20:15:36Z→22:33:16Z; never restarted)

| Step | Native exit | Key result |
| --- | --- | --- |
| focused (`-q tests/test_offline_launcher.py`) | 0 | 45 selected, 45 ran, 45 passed, 0 failed, unresolved 0 |
| collect-full (`--collect-only -q`) | 2 | selected 6015 (+9 deselected), 3 collection errors (`test_boundaries.py`, `test_end_to_end.py`, `test_release.py`), unresolved 0 |
| full (`-q`) | 2 | selected 6015 (+9), ran 6015, passed 5754, failed 171, skipped 30, errors 60, 3 collection errors, unresolved 0 (normal domain 5829 selected: 5584/155/30/60) |
| perm-fwd-co / perm-rev-co (5 files) | 2 / 2 | 189 selected each, identical per-order collection; `test_end_to_end.py` collection error both |
| perm-fwd-run / perm-rev-run (5 files) | 2 / 2 | 189 ran each, 179 passed / 10 failed each, **identical failed node sets in both orders**, normal domain 45/45 both |
| adj-collector-alone | 1 | restart test PASSED; only `test_cli_synthetic_run_writes_summary` failed |
| adj-calendar-then-collector | 1 | calendar 9 failed; collector: only CLI failed; restart PASSED |
| adj-scheduler-then-collector | 1 | restart PASSED; only CLI failed |
| adj-collector-then-calendar | 1 | restart PASSED; calendar 9; only CLI failed in collector |

Owner's closed 007 results match on every deterministic quantity: focused 45/0; collect-full 6015+9 with the same 3 collection-error files; full totals identical (6015 ran, 5754/171/30/60, same 3 collection errors); perms 189 selected with the same calendar-9 + e2e error in both orders. The only inter-run membership differences are the load-sensitive nodes described in findings HR2/HR4 below.

## Findings (all non-blocking for this write-set; evidence in `batch1/` and referenced owner evidence)

- **HR1 — owner full attempt-1 supervision failure (preserved, not credited).** Owner007's first full run's wrapper died while the launcher survived; two full launchers overlapped ~10 minutes (both in their normal domain only; no staging race), the duplicate was deliberately killed, and the later `exit 1` was handwritten, not native. That attempt stays INCOMPLETE and uncredited. My batch provides the fresh no-overlap verification the incident required (single supervisor, process-scan-verified clean start, natural completion, pins unchanged before/after).
- **HR2 — permutation failure-set difference adjudicated: load-sensitive historical test, not an isolation defect.** Owner's forward (10 failed) vs reverse (11 failed) differed by exactly `tests/datapipeline/test_collector.py::test_restart_resumes_pointer_and_keeps_gaps_scheduled`. Both owner failures (full-attempt2 and reverse) carry the identical signature `assert 5 == 0` with `pending_uncommitted=1` — the test's own 20-second wall-clock `run_deadline_seconds` was exhausted. In my independent evidence the test PASSED in all six executions, including both permutation orders and the exact calendar-immediately-before-collector adjacency the coupling hypothesis predicted; my forward and reverse failure sets are identical (10 each). DP03 and DP04 bootstraps build disjoint staging trees (`.tmp/datapipeline/DP03/staging` vs `DP04/staging`), so no cross-domain state path exists. Classification: order-VARYING failure caused by machine-load timing on a historical test-internal deadline (the host carried documented heavy foreign-project load during the owner's windows). Owner-closure item for test robustness; outside the write-set; not masked as noise and not a launcher defect.
- **HR3 — collector CLI test fails deterministically in all orders (owner closure item).** `test_cli_synthetic_run_writes_summary` failed in all 10 executions (owner 3, mine 7, including collector-alone) with `IndexError` on parsing the CLI subprocess's empty stdout. Same class in every order; never masked; faithfully reported per-domain. Root cause not established by this review (the guard's PYTHONPATH startup-chain overwrite for test-spawned python children predates C05 and lives in the unchanged `offline_guard.py`); it is a full-baseline-layer owner-closure item, not an order or isolation artifact.
- **HR4 — normal-domain failures are the pre-existing baseline, with documented timing-sensitive membership.** 155 failed + 60 errors in my normal domain (owner: 154 + 60) across benchmarks/docs_reference/experiments/risk_semantics/mcp_auth/postgres-setup/factor_library/m5/t7 files. The old entry could not execute these at all (174 collection errors, 0 executed), so C05 made them runnable and reported; the exact failing membership inside timing-heavy files (e.g. `test_harness`, `test_static_reference`) varies by ±1 between owner's and my full runs — itself evidence of load sensitivity, not of launcher-induced breakage. No failure class implicates routing, guard, exclusions, basetemp or exit-code behavior; deselected is exactly the 9 mandatory nodes in every run; `unresolved_files 0` everywhere.
- **HR5 — historical DP failures preserved exactly.** DP08 old-consumption pin mismatch visible as the `test_end_to_end.py` collection error (`collector.py` manifest `f5cf3d0e…` vs tracked `64387dda…`), plus `test_boundaries.py`/`test_release.py` collection errors; calendar's 9 DP04 pin/anchor failures identical in every run and order; task_installation 6 identical in both fulls. No pin refreshed, nothing masked.
- **HR6 — isolation conformance confirmed on frozen bytes.** Focused 45/45 includes every synthetic contamination/order/basetemp/empty/priority/interrupt regression with unchanged strength (shared-process counterexample still required to fail); RF1's prefix-free encoder verified injective by my independent 37,448-string uniqueness/roundtrip universe (`c05-reviewer-rf1-recheck-004`); permutations order-invariant with the normal domain unpolluted (45/45 both directions); per-domain native exits and streams fully visible; overall exits 2/2/2 (full/perms) and 1 (adjacency) correctly propagate raw priorities.

## Reused prior reviewer results (precise applicability)

- probes-003 B–F (original-F1 pair, F2 planning-failure, F3 not-dispatched, exit priority/accounting, basetemp fresh/reuse): applicable — the FREEZE005→006 launcher diff is confined to `_datapipeline_domain_name` (verified by full-file comparison), and every one of these behaviors is additionally covered by the focused 45 that both owner and I ran green on the frozen bytes.
- probes-003 static/A (FREEZE005 FAIL): historical, superseded by the repair and by 004.
- 004 RF1 recheck: directly binding on these frozen bytes (same hashes).
- Owner's validation004 stop and run-full attempt-1: preserved INCOMPLETE/uncredited; never rerun or resumed by anyone.

## Evidence index

My batch (all with `COMMAND.json`, child pid, `stdout.txt`, `stderr.txt`, `exit.txt`, `end.txt`, sealed after child termination, plus `.tmp/datapipeline` snapshots per step): `work/batch1/{focused,collect-full,full,perm-fwd-co,perm-fwd-run,perm-rev-co,perm-rev-run,adj-*}`; supervisor `work/supervisor.py`, `work/batch1/{manifest.json,state.json,BATCH.lock}`; Root's preserved copy pins `state.json` sha256 `8afeb525…` and `manifest.json` sha256 `7b1f0263…`. Owner evidence: `c05-owner-complete-validation-007/work/evidence/*` and `ROOT-EXISTING-RESULTS-HANDOFF.json`. Prior reviewer phases: `c05-reviewer-probes-003/work`, `c05-reviewer-rf1-recheck-004/work`.

## Constraints honored

No candidate writes (verified hash-identical before/after); no rerun of any consumed one-use command (validation004, owner's closed runs, my completed batch); no git/services/network/DB/SDK/broker/UI; no `.env`/credentials; no nested agents; every process executing test code ran under the byte-identical offline guard via the candidate launcher; streams sealed only after producer termination; `enable_trading=False`, observer `hold`, broker `NOT_RUN`, real send/cancel `0/0`.
