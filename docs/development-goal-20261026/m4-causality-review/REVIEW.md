# REVIEW — m4-causality-hard-review-003 (author007 §4, one-file causality test repair)

**Verdict: PASS — unconditional** (`VERDICT.json`; `conditional: false`). Scope:
the author007 §4 adaptation implemented by m4-causality-owner-004 — exactly two
warmth-existence assertions in
`C:\AKWorktrees\aktr-goal-m4-causality-20261027\tests\test_v2_m4_causality.py`
changed from the removed tuple form `state[0]` to the accepted `_PrefixState`
representation `state.prefix` (the `>16` condition and `any(...)` shape kept) —
at frozen bytes sha256 `b9b43fa235b0b6ec84812e49af349288ffb3438fccefa7fd081e7267547f5576`
(40,949 bytes), together with my guarded reruns: focused four preflight nodes
4/4 and the complete original M4 nine-file population **259/259**, both native
exit 0. Nothing else is accepted (no whole-T7/locking/cold-chain/wiring
acceptance; no production-code acceptance).

Reviewer: independent ZCode GLM-5.3/max, session `sess_6b517153…` — the same
reviewer across the M4 lineage; no part in development. All writes confined to
`…/m4-causality-hard-review-003/work`; `OWNER-REPORT.md` (root-watchdog owned)
untouched.

## 1. Write-set verification

- **623/623 REQUEST pins MATCH before the runs and again after** — the full
  602-file candidate population plus every authority/evidence input
  (`work/pin-and-delta.json`, `work/post-test-seal.json`).
- **Binary two-line delta reproduced independently** against the preserved
  preimage `5ea805c1…` (40,941 B — the causality file of the goal-m4-final
  lineage my prior review verified): exactly lines **598** and **615** differ,
  both `len(state[0])>16` → `len(state.prefix)>16` inside
  `factors._PREFIX_CACHE[("rb",86400)]` assertions; all 673 lines CRLF before
  and after; every other line byte-identical. The three-boundary control test
  is byte-identical to the preimage; `test_v2_m4_payoffs.py`, `factors.py` and
  all production files untouched (post-test pins prove).

## 2. Genuine counterexamples (`work/reviewer-probe.json`, 16/16, 328.41 s)

- **P1 — cold conflict ordering**: a duplicate conflict injected on the FIRST
  bar (not the tail) rejects `conflicting_duplicate_bar` with **zero provider
  calls** — position-independent preflight.
- **P2 — warm conflict ordering + integrity**: after a successful warm build
  (4 publications, READY), a conflict on a different bar rejects with zero NEW
  provider calls and the warm publication tuple preserved; a clean rebuild
  afterwards reproduces the baseline canonical bytes (failed attempt does not
  poison the prefix cache). The cached object **is** the slotted
  `_PrefixState`; `state[0]` raises TypeError (the pre-repair failures were
  exactly the removed tuple dependency), and `len(state.prefix) > 16` holds.
- **P3 — equal anchor consumed once, equality-keyed**: an identical duplicated
  anchor is consumed once (592 visible bars, count 1) with canonical context
  and request bytes equal to baseline; a **mutated** duplicate of the same
  anchor is rejected — dedup is keyed on equality, not last-wins.
- **P4 — three-boundary control re-derived**: three `_features` calls at their
  own decision boundaries (bars[591]/bars[590]/bars[591] availability), all
  `canonical_daily=True`, candidates == `visible_bars(at, False)`, `bars IS
  canonical`, `end_nano/available_at_nano <= at`, `context.bars IS` the last
  canonical set — and the two distinct boundaries carry genuinely different
  candidate sets (591 vs 592 bars).

## 3. Guarded reruns (exact work-order commands; candidate cwd; launcher
entry only; `PYTHONDONTWRITEBYTECODE=1`; modules from candidate `src`;
new basetemp per run; streams sealed after termination)

| Command | Budget | Native exit | Result |
|---|---|---|---|
| `python -B -X utf8 scripts/test_offline.py -q tests/test_v2_m4_causality.py -k test_daily_source_preflight --basetemp work/temp/post-focused4` | 3600 s / 2 GiB | **0** | selected 4 / **passed 4** / 0 failed / 39 deselected / 0 skipped-errors; the four nodes are exactly the author-corrected set (`…rejects_first_conflict…[True]`, `[False]`, `…consumes_equal_anchor_once…`, `…covers_three_snapshots…`); stderr 0 B; tree peak 390.5 MiB; completed 2026-10-27T05:21:51Z (28 poller samples) |
| `python -B -X utf8 scripts/test_offline.py -q` + exact nine-file list in work-order order (causality, execution, gates, ledger, payoff_paths, payoffs, replay, risk, routing) `--basetemp work/temp/post-m4-ninefile` | 3600 s / 2 GiB | **0** | selected 259 / **passed 259** / 0 failed-skipped-errors / 0 collection errors; all nine files collected; **950.42 s**; stderr 0 B; tree peak 572.3 MiB; background lifecycle, no redundant separate full-causality run |

No population change: same 259 nodes, no chronology change, no historical
binding-hash refresh, no skips, no relaxed thresholds, no old-tuple
compatibility branch; warmth assertions kept. The owner's pre-repair exit-1
record (exactly the two author-corrected nodes failing with
`TypeError: '_PrefixState' object is not subscriptable` at 598/615) was
cross-checked read-only and is consistent with my probe's proof of
non-subscriptability.

## 4. Metadata disposition (ROOT-RECEPTION-001 items)

- **O1**: DELIVERY's "41,045 bytes" is a transcription error (actual
  40,949; FREEZE/FIX-PROOFS/REQUEST/ROOT-RECEPTION all correct). Originals
  preserved; the actual pin was used and verified pre- and post-test; no
  unrelated suites rerun for a transcription error.
- **O2**: `frozen_at "2026-10-27T00:00+08:00"` (= 2026-10-26T16:00Z) is
  inconsistent with the actual chronology (final file 04:29:35Z → owner
  nine-file stdout 04:50:17Z → FREEZE 04:50:44Z → DELIVERY 04:51:03Z →
  TERMINAL 04:51:36.444423Z, native 0, inputs unchanged; local offset UTC-7).
  Treated as an owner-authored unverified string; actual terminal chronology
  used; no rewrite/backdate.

## 5. Boundaries

`enable_trading=false`, observer `hold`, broker `NOT_RUN`, send/cancel 0/0;
no `.env`/credential/config/rules/model reads or writes; no
git/SDK/network/broker/service/UI writes; no nested agents; candidate
read-only; no direct pytest substitution; raw native exits preserved;
generated outputs created only after absence checks; JSON validated before
exclusive publish; AGENTS §7/§8 and the referenced incidents applied; no
optional rg condition used. Review stops after this verdict.
