# REVIEW — t7-cpu-hard-review-004, F01 repair re-review (2026-10-26)

**Independent re-review after FAIL.** Same independent ZCode `GLM-5.3`/`max`
reviewer session as 001–003 (`sess_7229454f-…`); no part in the development or
repair. 003 found one blocking defect (F01); the original ZCode owner (owner-002,
same developer session as owner-001) repaired it and froze a new revision.
Owner-002's CLI later ended raw1/HTTP 502/empty stdout — a transport fact
recorded separately (R01); its real deliverables (FREEZE, DELIVERY, supervised
suites 96/165/33 raw 0) were complete and Root-verified before this review.

## Verdict

**PASS — unconditional**, scoped to the T7 CPU four-file write-set at the new
frozen hashes. F01 is repaired and verified byte-identical to baseline on its
entire input class. PASS does **not** resolve the MINUTE memory/disk bound, the
§4 memory increment/wiring, the remaining M1/M4/M5-full/T7 populations, or
anything broker-related (`broker=NOT_RUN`).

## What changed (verified against bytes, not the delivery's claims)

| File | New SHA-256 | Role |
| --- | --- | --- |
| `src/ak_trading/v2/factors.py` | `c1e435c1…f1046db7b` | repaired (F01) |
| `src/ak_trading/v2/validation/experiments.py` | `2aecad49…cfa8829a` | byte-identical to FAIL revision |
| `tests/test_v2_m2_causality.py` | `52f54ad4…582e0277e` | +1 F01 regression (append-only) |
| `tests/test_v2_m5_experiments.py` | `147d2efe…850d93f65` | byte-identical to FAIL revision |

Candidate bytes match the freeze exactly; the 600-file pin sweep shows zero
non-owned drift; owner-002's `pre-repair/` copies hash-match the 003 FAIL
revision. The factors.py diff is 13 lines in `_visible`'s extension path: when
the merge satisfies exactly `_merge_prefixes`' own overlap condition, the new
state's index-bound `covers` map starts empty (next `_prepare` runs the original
full scan); pure appends keep inheritance; order-independent aggregates and the
chain are untouched.

## F01 resolution — independent evidence

Probe recheck (`work/probes/rv4_covers_recheck.py`), the reviewer's original
three cases plus one new case, run against three trees (repaired candidate,
pre-repair shadow, original pinned baseline):

| Case | repaired | prerepair (FAIL rev) | baseline |
| --- | --- | --- | --- |
| A uncovered mid-insert (F01 input) | warm==cold MISSING/2, full rescan (verified=2) | **warm READY/12 ≠ cold MISSING/2** (F01 reproduced; stale verified=121) | warm==cold MISSING/2 |
| B covered mid-insert (control) | READY/12 == cold | READY/12 == cold | READY/12 == cold |
| C uncovered tail append (control) | INVALID == cold | INVALID == cold | INVALID == cold |
| D multi-bar mixed overlap tail (new) | INVALID == cold, verified=2 | INVALID == cold, stale verified=122 | INVALID == cold |

Decisive detail: on every case, the repaired tree's **warm and cold record
hashes are byte-identical to the pinned baseline** — the only warm-vs-baseline
divergence ever observed (prerepair case A) is gone. (My first case-D
construction raised `conflicting_duplicate_bar` because two tail bars shared a
dedup-key datetime — my probe bug, fixed with distinct datetimes; the initial
failed attempt is preserved in the overwritten stream files of this run set
only, noted here for honesty.)

The new regression test
`test_t7cpu_overlap_merge_resets_index_bound_covers_verification` is
discriminating, not decorative: it **fails** on the pre-repair shadow tree
(`PYTHONPATH`-bound, origin asserted; raw exit 1, `READY != MISSING`) and
**passes** on the repaired candidate (exit 0). It is an append-only test
change; no existing test was modified.

## Required suites (independent rerun on the repaired bytes, exact owner argv)

| Suite | Result | Wall / RSS |
| --- | --- | --- |
| M2 six-file | **96 passed**, exit 0 | 45.1 s / 59 MiB |
| M3A six-file | **165 passed**, exit 0 | 653.4 s / 60 MiB |
| `test_v2_m5_experiments.py` | **33 passed**, exit 0 | 122.6 s / 128 MiB |

Expected current populations (96 = 95 + 1 F01 regression), no skips, no
removals, all within 3600 s / 2 GiB.

## Performance (no unresolved regression; 003 measurements reused)

The repair touches only the overlap branch, which frozen inputs never take
(`datetime == start` on all 25,980 fixture bars). Recheck on the repaired tree:
512-prefix no-fit stream chain **byte-identical** to the 003 measurement
(`69fb1bb1…`), wall 13.79 s vs 12.83 s (+0.96 s, run-to-run variance). 003's
independent 2.30×/2.35× measurements and its full counterexample battery
(D2 revision, duplicate tails, daily background, six durations, Wilder,
255/256/257, grid binding, calendar/spec identity, eviction) remain valid for
the byte-identical files and untouched paths; no broad re-benchmark was needed.

## Preserved limitations (bound separately from the verdict)

- **F02**: the pre-existing overlap-branch warm/cold `input_hash` (chain-order)
  asymmetry remains, byte-identically to the baseline, per the 003 scoping (a
  baseline limitation, not new acceptance).
- **R02**: owner-001's historical evidence limitations (869.30 s cProfile
  overrun stopped by Root, exit 15, omitted from its DELIVERY; overwritten first
  M5 failure logs) and reviewer-001's early probe corrections remain recorded;
  owner-002's delivery adds no new such events.
- **R03 / NOT_RUN**: full MINUTE cold chain and its ≈2.491 GiB static lower
  bound, §4 memory increment and wiring, M1/M4/M5-full/T7/goal-lock
  populations, downstream stage profiling, real broker.

## Disposition

The T7 CPU four-file unit is **accepted** at the frozen hashes above
(`c1e435c1…` / `2aecad49…` / `52f54ad4…` / `147d2efe…`). The write-freeze
ends. This PASS accepts the CPU unit only; the separately demonstrated MINUTE
memory block and the remaining integration obligations are unaffected. Safety
throughout: `enable_trading=False`, observer `hold`, `broker=NOT_RUN`, real
send/cancel `0/0`; no credentials/env/network/DB/SDK/UI/Git/nested agents; no
model or configuration changes; 0 product edits by this review.
