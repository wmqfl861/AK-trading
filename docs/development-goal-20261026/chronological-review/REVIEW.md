# REVIEW — chronological-full-review-007: independent product hard review of the frozen RB chronological-transfer candidate

Independent reviewer session `sess_03bbb184-c36a-4c58-8644-97b79104b79e` (same SID/store/cwd
and original GLM-5.3/max configuration since dispatch 001; no role in the product's planning,
development or repairs). Standard: **PLAN-F01-20261015-r2 H01–H11** + **C01/C02** (r3
clarification/disposition), as amended by namespace-001 **N01–N05**, runtime-002 **R01–R06**
and concurrency-004 scheduling amendment. Reviewed write-set (frozen):

| File | SHA-256 |
| --- | --- |
| `tools/experiments/historical_daily_chronological_transfer.py` (76,366 B) | `f1c0a416383fa07313d03d01abd6a608c6d52fadd3e289700265ed7cc8d2e950` |
| `tests/experiments/test_historical_daily_chronological_transfer.py` (45,265 B) | `8270cd94809b04556380c249def68a5499eb8a519dba3723f51de3b7eb1608b9` |
| `tests/fixtures/experiments/rb_daily_chronological_transfer/base.json` (179 B) | `3a958393bebd7da253c13d32ea996653a213405536e20e278083398e30a9b996` |

## 1. Independent evidence chain (all raw logs preserved in dispatch work trees)

1. **001 — staging & existing outputs.** All 213 staged candidate files byte-exact vs
   `STAGING.json` (`8be18885…`, 55,979 B); ten legacy engine pins, four standard docs, legacy
   manifest and sibling reference match the unchanged `INPUTS.json` pins; staged real-input-02
   payloads match the original absolute D: bundle pins. The relocated-namespace gate was
   reproduced read-only: unadapted `_source_pins()` raises `StopIteration`; the production CLI
   exits native 2 `status=rejected` and creates no output root. Both closed owner runs
   (`…-owner-r1`, `…-owner-replay-r1`) verified: 1,523 files / 60,682,453 B each, identical
   manifests `a1879111dd2d15560983fee16eead53947b521c136d61f4ee0cd7c8184431c3b`, full
   content-hash closure, all expected counts and C01 typed provenance (252 × 20 exact fields).
2. **002 — plan-conformance and behavior.** Session precheck OK. N01/N02 adapter
   self-verification OK: exactly two logical lookup aliases; physical reads/hashes preserved
   (18 behavior checks); unique exact inventory rows; corrupted bytes behind a correct logical
   name still fail in the product's own `_source_pins()` (`legacy source manifest hash
   mismatch`) — the adapter supplies names, never validation. **Namespace-adapted H10
   execution (disclosed as such, launcher controls preserved)**: all four pinned test files —
   **272 passed / 0 failed / 0 deselected** in 368.98 s, engine module origins bound to the
   frozen candidate; separate smoke run 106 passed. The `production_acceptance=true` labels in
   run outputs are the wrapper's structural self-description; the independent product verdict
   is only this review's.
3. **004 — accounting oracle validated on the closed owner tree.** Exact-Fraction oracle over
   all 53,541 daily rows, 252 children, 3,528 contract-year cells, 92,235 paired rows and both
   42-row summaries: **OK, 0 problems** (daily conservation identities, fill-derived
   fee/slippage deltas, report totals, terminal-flat, cross-year carry without reset,
   seven-year conservation, j≥60 mask, pair A−B arithmetic and view membership, denominators
   and exact medians). Three oracle-script bugs found during owner-run validation were fixed
   and disclosed (see FINDINGS).
4. **005 — external supervision (R03/R04).** Supervisor (suspended spawn → Job Object with
   aggregate 1 GiB `JOB_MEMORY` + `KILL_ON_JOB_CLOSE`; startup record with PID/creation
   identity persisted before release; external monotonic deadline; byte accounting while
   writing; terminated-stream sealing; post-run pin reverification) validated by seven bounded
   negative/positive cases — **7/7 match** with raw negative exits preserved (attempt-1 harness
   bugs retained). Prepared freeze `62f4e877…`.
5. **007 — the single authorized fresh full replay (this dispatch).** After joint admission
   (`HEAVY-ADMISSION.json`, concurrency-004 authority) and a **before-phase verification of
   all 3,303 transitive frozen inputs (0 absent / 0 mismatch)**, the exact `ROOT-COMMAND.json`
   argv (pinned `01105d77…`) launched the unchanged wrapper `main(argv)` through the
   supervision chain with the N01–N05 adapter. Outcome, separated per R04:
   - worker: **exit 0, `main` returned 0**, final stdout JSON `status:"complete"`;
   - supervisor: no stop reason, elapsed **3,622.8 s** (deadline 5,400), peak job memory
     **888,279,040 B ≈ 847 MiB** (cap 1 GiB; 1,608 samples), output written **60,692,844 B**
     (quota 196,112,437 B), **0 descendants alive** after termination, all post-pins OK,
     evidence sealed; launcher native exit 0;
   - custody: worker pre-main record persisted before `main` (R04 correction in production),
     post record with `main_return=0`, streams closed and hashed after termination only;
   - overlap window (concurrency-004): DAILY016 PIDs 135780/138276/158012 alive at both
     replay start and end — real overlap 17:29:29–18:36:50 UTC-adjacent local, ≈60.6 min;
     timings carry **no performance-qualification claim** and no limit was waived;
   - **after-phase inventory: all 3,303 transitive inputs unchanged**;
   - **check8 (007 corrected copy `ec51af29…`): OK — 1,523 files / 60,682,453 B, full
     content-hash closure, manifest `complete`, and *every file byte-identical to the closed
     owner-r1 tree* (also matching owner-replay-r1's identical manifest `a1879111…`)**;
   - **check9 (004 oracle, unmodified bytes): OK — 0 accounting problems on the fresh tree.**

## 2. Acceptance facts (original H01–H11/C01C02 counts, unchanged and reproduced)

84 RB contracts (79 ≥60 + 5 short: rb2009 42, rb2010 58, rb2706 58, rb2707 37, rb2708 14),
17,847 RB bars, 1,489 distinct raw dates, parent 439 declarations / 92,461 bars / 355 / 74,614
exclusions; **252** child calls; **53,541** ALL rows; **38,694** PRIMARY rows (j≥60 mask);
**3,528** contract-year cells; **42** year-summary rows; **42** paired-summary rows;
**92,235** paired-observation rows (all MATCHED; 0 missing/mismatch/non-finite/support).
C01: `coverage.json/child_provenance` = 252 entries, exact 20-field typed closure, hashes
re-verified against actual child files, wrapper pin `f1c0a416…`, real send/cancel integer 0/0.
C02: production CLI has only the three options; the private `_publish` seam cannot produce a
production-accepted tree (forgery tests). Safety fields on every level:
`SCENARIO / not_historical_verified / historical_strict_execution=false / broker=NOT_RUN /
enable_trading=false / real send-cancel 0/0 / calendar UNAVAILABLE_VERIFIED_TRADING_CALENDAR`.

## 3. Scope of this verdict

PASS is scoped to the **frozen three-file historical candidate** at the pins above, verified
under the disclosed N01–N05 namespace mapping (two logical lookup aliases; all reads/hashes
physical) and R01–R06/concurrency-004 supervision. It is **not** a claim about MAIN's accepted
P2-7 successor, not general arbitrary-location CLI compatibility, not historical/PIT/OOS
qualification, not an M5 A/B gate, and not paper acceptance. `new_statistical_evidence=false`;
overlap timings are not performance evidence. Statistical qualification remains
`NOT_ASSESSED`; research limits remain `SCENARIO / CALENDAR_UNVERIFIED`.
