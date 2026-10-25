# T7 Unit A F01 Repair — Independent Re-Review Report (t7-strategy-f01-hard-review-004/005)

**Verdict: PASS (unconditional, scope-bounded)** — see `VERDICT.json`. Finding F01
(003 FAIL) is verified **resolved** on the repaired frozen bytes by independent code
review, a full independent rerun of the §10.2 population, and 11 repaired-behavior
counterexamples. This PASS closes F01 and unlocks the accepted-byte handoff of these
exact bytes to unit B; it grants nothing beyond the original unit-A scope.

## 1. Identity and inputs

- Reviewer: the same independent ZCode reviewer (session `sess_9fd6415c…`) who
  issued the 003 FAIL/F01, in a new immutable root
  `C:\AKWorktrees\aktr-r3-t7-A-review-f01-20261025`; the 003 package is untouched.
  The 004 CLI ended native 1 / HTTP 502 **after** all nine acceptance runs had
  closed raw 0; this 005 continuation published the verdict from those verified
  results without rerunning them (all nine RESULT.json envelopes hash-matched
  against `ROOT-HANDOFF-005` first). One interrupted TodoWrite was a progress-list
  tool event, not a test (O-4).
- Repaired revision (`t7-strategy-owner-f01-repair-004` FREEZE, verified by
  independent recomputation): exactly three files changed — `trend.py`
  (`c1879eb7…`), `pullback.py` (`093d8405…`), `tests/test_v2_m3b_directional.py`
  (`a88d8d44…`); the other five lease files byte-identical to the prior frozen r1;
  32 real-RB inputs equal. Baseline `cf647ea…` lineage preserved.

## 2. Repair assessment (code + probes)

Both `_observe` paths now derive `same_bar` from the committed setup identity
(`state.setup.last_bar_id == bars[-1].bar_id` and setup not expired), hold
`confirmation_count` on a re-delivered decision context, reset to 0 on gate failure
(unchanged), and increment only on a distinct completed bar — the same hold
semantics as breakout.py's confirmation branch. The expiry-transition clause is
sound: a re-delivery landing after setup expiry re-founds a new setup whose first
observation legitimately counts, and further duplicates of that bar then hold.
The diff is minimal and confined to the two defect sites plus three genuine
regressions (they fail on the pre-repair bytes).

## 3. Required population — independent rerun on the repaired bytes

Reviewer's own bounded harness (Job 2 GiB / 600 s / 1 GiB, race-tolerant walk,
suspended child joined before execution, native exit after closure, 40 pins per
run — zero mismatches anywhere):

| Run | Raw exit | Nodes |
| --- | --- | --- |
| f01rv-r1-c1_registry | 0 | 77 passed, 0 deselected |
| f01rv-r1-c2_matrix | 0 | 68 passed, 0 deselected, 0 skipped (real-RB nodes executed) |
| f01rv-r1-c3_directional | 0 | 19 passed = 16 + 3 new regressions |
| f01rv-r1-c4_reversion_grid | 0 | 12 passed |
| f01rv-r1-c5_martingale | 0 | 14 passed |
| f01rv-r1-c6_state | 0 | 17 passed |
| f01rv-r1-c7_causality | 0 | 5 passed |
| f01rv-r1-c8_full | 0 | **212 passed** = 77+68+19+12+14+17+5, 0 deselected |

The historical 209 on faulty bytes and the owner's own 212 were not reused as
acceptance evidence.

## 4. Counterexamples on the repaired bytes (11/11, raw 0)

F01 reproductions flipped positive: TREND duplicate bar ×4 → `[(WAIT,1,b21)]×4`,
no OPEN (pre-repair this exact world OPENed on the third duplicate); PULLBACK
long-mirror duplicate reclaim holds 2 and OPENs only on the third distinct bar.
Guard-scope proofs: distinct bars still advance 1→2→3 and OPEN; restart/redelivery
equivalence `[1,1,2,2,OPEN@3rd-distinct]` equals the clean run's third-bar
confirmation (§10-11); gate-fail resets to 0 and a same-bar re-delivery holds the
reset. Unchanged-clause guards all green: breakout idempotence, evidence
ambiguity/expiry/availability rejection, clock-only horizon first-fit, full-window
OPEN rejection, `code_sha256` binding the repaired bytes, signed short hysteresis
HOLD −2, below-exit EXIT, EVENT_SHOCK hard-risk EXIT.

## 5. Helper delta and honest history (non-blocking O-1/O-2/O-3)

The owner's `run_bounded.py` was modified **in place** (9833→10128 B,
`250356…`→`f54d9e97…`) after its first c3 supervisor hit the same
pytest-temporary-cleanup race as reviewer incident R-1 — a read-only-reuse
discipline deviation, preserved honestly: both hashes pinned, the failed first c3
remains UNKNOWN_NOT_ACCEPTED (no RESULT.json, no fabricated exit), and the owner
FREEZE omitted the helper hash (covered by ROOT-HANDOFF, verified equal to the
actual bytes). The delta itself is confined to the race-tolerant walk — no limit
weakening, no change to child execution or native-exit capture — and this verdict
draws **zero** credit from any owner run: all nine acceptance runs are the
reviewer's own under the reviewer's hash-pinned harness.

## 6. Safety and limitations

`enable_trading=False`, observer `hold`, broker `NOT_RUN`, real send/cancel 0; no
credentials/.env, network, SDK, DB, broker, UI, git, or delegation; candidate
bytes unmodified by the review (post-run invariance verified, helper still
`f54d9e97…`). Out of scope: remaining M3B files, `experiments.py`, M5/T7
consumption, historical backtests, profitability, any node PASS; the range
families' counters were not probed for the F01 pattern (O-5).

**Deliverables:** `VERDICT.json`, `FINDINGS.json`, `HASHES.json`, this `REVIEW.md`,
plus `evidence/runs/` (nine closed envelopes) and `evidence/probes/probe_f01_repair.py`.
Package immutable post-verdict.
