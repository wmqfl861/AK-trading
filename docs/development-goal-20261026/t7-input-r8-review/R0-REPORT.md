# T7 R0 Frozen-Input / Scope Check Report — v2-ep1-t7-review-r1 (goal-t7b-20261027-r7, F r8)

* **Task:** `aktr-zcode-goal-t7-R0-review-20261027-006` (LAUNCH-WORKORDER-002).
  **This is an input R0 only — not a plan review, not the behavioral R1, and
  not a full-T7 PASS.** No fit, no cold chain, no lock authoring.
* **Reviewer:** the original independent T7 reviewer session
  (`sess_6c24d61a-5ae5-40d3-938b-d722f8cfa468`, GLM-5.3/max), which took no
  part in B022. The CLI cwd is NOT the candidate and was never imported as
  product; all product imports used process-only
  `PYTHONPATH=C:/AKWorktrees/aktr-goal-t7b-20261027-r7/src` with asserted
  module origin (no MAIN editable install, no old-lock borrowing).
* **Candidate:** `C:\AKWorktrees\aktr-goal-t7b-20261027-r7` — frozen F(r8)
  `tests/fixtures/v2/ep1_t7/acceptance-input-manifest.json` (21 778 B,
  **H_F `df7bc0ee6598707625ca3ca794e9d8bcf1a70a70bfc06296573fbc194a6f1065`**,
  LF-normalized), DAILY manifest H `04ac309e…38c61`, per
  `t7-b-successor-owner-022/work/F.json`. Exactly three members changed vs
  the r6b freeze (support module with §5-allowed monotonic phase-timing
  marks + both manifests); the **12 data/recipe/partition inputs are
  byte-identical to r6b** (re-verified directly: e.g. minute bars
  `79d4b68b…`, DAILY sessions `9916e759…`; prior R0 evidence for those
  unchanged inputs reused after byte match — no redundant large
  regeneration). All 151 REQUEST pins verified before AND after the review;
  L0/L1 paths correctly absent; no new bytecode; trading boundary
  `false/hold/NOT_RUN/0/0` throughout.
* **Dependencies under review:** the Root-staged accepted r7 set — 10-path
  union (CPU/memory/wiring on `39296d9`, incl. the new
  `src/ak_trading/v2/_fold_records.py`) plus the separately accepted M4 —
  governed by `ROOT-STAGING-ACCEPTED-002.json`; the historical M4/six-input
  handoffs are superseded for current bytes where the staging changed them
  (`tests/test_v2_m4_causality.py` recorded SUPERSEDED_BY_STAGING with
  tree==F-pin==staging verified; old records preserved read-only).

## Checks (commands, raw exits — logs under `logs/`)

| # | Check | Exit | Outcome |
|---|---|---|---|
| C1 | REQUEST pins (×2, pre/post) | 0 | **151/151** exact; interpreter pinned. |
| C2 | fixture audit | 0 | F **129 attachments** (floor **104**, incl. `_fold_records.py`), DAILY manifest **111**; sorted/unique/safe-paths/no-circular-keys; per-file bytes+SHA exact; exact membership; F pins DAILY manifest bytes; H_F/H_daily recompute = frozen values; accepted records 32 files → 28 MATCH + 4 SUPERSEDED_BY_A2 (staging files all MATCH, m4_causality superseded-by-staging); M4 expiry-fixture markers and canonical-reconciliation markers intact; support carries phase-timing marks writing only the non-canonical `runroot/phase-timing.jsonl` side log, no canonical-record writes. (Two earlier script-side attempts preserved: handoff-shape and supersession-classification bugs of MY audit script — product unrelated.) |
| C3 | regeneration identity | 0 | frozen verifiers `[]` both trees; **14/14 files byte-identical** rebuilt from the r7 module constants (support sha `62a0977c…2bbf6` asserted before import); world/calendar/bundle identities unchanged; **both plan-set hashes equal r6b's** (`d6a64545…`/`2b2f49f6…`) — the dependency merge + timing marks altered no planning semantics and no canonical records. |
| C4 | floors/chronology + r7 contracts | 0 | policy 200/30 unchanged; outer arithmetic + nano equality both profiles; minute B/C/O=366/52/8, W=7; DAILY inner 3150/2898/226/26; warmup day 20; dev 231≥225 (margin 6); D1/D5/D20=225/221/206≥200 with the DAY20 lag exactly 20; 226 blocks≥30; 19 windows + one honest `NO_LEGAL_WARMUP` skip; **six durations restored and accepted by real `parse_config`**; the `[86400]`-only copy rejected with exactly `frozen_config_values_changed:feature_durations`; **`parse_config` strictly precedes `outer_stand_in_daily`** (ordering unchanged by the timing marks). |
| C5a | official M1 verifier | 0 | returns the pinned H_F `df7bc0ee…f1065` (both entry points). |
| C5b | producer bindings | 0 | **14/14** real `pipeline.legacy_exit_binding == select_exit_binding(legacy)`, entry == current `code_sha256`, rows equal the frozen record (registry bytes unchanged by the merge); manifest bindings match (`0118e630…`, `279c497d…`). |
| C5c | manifest nodes (offline runner, fresh basetemp) | 0 | `test_t7e1_acceptance_input_manifest_matches_tree` + `test_t7e2_daily_manifest_matches_tree`: **2/2 passed, 0 failed/skipped**. |
| C5d | config fast-fail regressions | 0 | `test_t7e2_daily_config_preflight` + `test_t7e2_daily_reduced_durations_rejected_before_setup`: **2/2 passed** — the r6 repair contracts hold on the merged source. |
| C6 | negative probes | 0 | **13/13** exact rejections (sandbox tamper/extra/unsorted/DAILY-tree-extra; adversarial path/circular; pristine/restored acceptance at the new H_F). |
| C7 | closing immutability | 0 | 151/151 pins again; L0/L1 ABSENT; no new `.pyc`; plain worktree (pin equality is the immutability basis). |

**Budget note (preserved as required):** the owner's extended count-check
took 195 s against a 60 s guidance — that overrun stands recorded and is NOT
called budget-compliant; my independent world/config checks ran within the
≤900 s/profile R0 allowance with real outer execution limits.

## Findings

**No blocking findings.** Observations: OBS-1 (carried) — `recipe.json`
raw newlines inside `shape` (writer comma-replace; hash-bound, never
strict-parsed). OBS-2 — the phase-timing marks are diagnostics-only by
construction (side log; verified no canonical-record writes and unchanged
regeneration), exactly the author006 §5 allowance.

## Verdict

**R0_FROZEN_INPUT_SCOPE_CHECK_OK (F r8, H_F `df7bc0ee…f1065`).** Every
§5.4(2) frozen-input/scope item succeeded with raw exit 0 against the r7
candidate's actual bytes — schema/membership/floor(104, incl.
`_fold_records.py`)/accepted-staging bytes/producer bindings/regeneration
identity (equal plan-set hashes prove semantic neutrality of the merge and
timing marks)/DAILY chronology/200-30 floors/six-duration and fast-fail
config contracts/official verifier/manifest nodes/negative probes. The
candidate stayed byte-identical throughout. This verdict states the
frozen-input/scope check only: **no fit, no cold chain, no lock authored,
no R1/full-T7 PASS.** The original M1 owner may bind this report's actual
bytes below as the new L0's `reviewer_report_sha256` input immediately;
the full 121-node/M1/M4/M5 consumer obligations remain for R1.

*Machine records in `R0-RESULT.json`; report bytes sealed in the dispatch
`R0-HANDOFF.json` (closed producing processes only; the seal writer's own
streams excluded for Root's post-termination closure).*
