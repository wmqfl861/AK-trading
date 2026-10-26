# T7 Six-File Memory Unit — F01 Repair Re-Review (PASS)

**Review:** `t7-memory-hard-review-004`, same independent reviewer session as
001–003 (`sess_a365bf07-cae0-4d44-9933-bbb1daa4a354`; no part in the plan,
development, or repair). **Verdict: PASS — unconditional, scoped to the
six-file unit** at the hashes pinned in VERDICT.json (calibration.py
`962eb84c…`, test_v2_m3a_calibration.py `41a11ec4…`, the other four files
byte-identical to the review-003 frozen revision).

## F01 resolution

The review-003 FAIL driver is repaired exactly as required: the duplicate-feature
check moved out of `_TupleFeatureAccess.__init__` into `scalars_by_id()`,
raising `duplicate_feature_snapshot_id` at the original shared-core position
inside `_validate_connections_core` — after the forecast/label/block-id checks
— where `_ViewFeatureAccess` already performed it. The repair-only diff
(against the preserved pre-repair root, hash-matched to my 003 pin) is exactly
one hunk; the public `_validate_connections` wrapper still returns the snapshot
map built at the original position; nothing else in the file changed.

Independently verified three ways:

1. **Required suite rerun on the final bytes** (`work/m3a-rerun-004`,
   `scripts/test_offline.py`, all six M3A files, real 3600 s timeout):
   **183 selected / 183 ran / 183 passed / 0 failed / 0 skipped / 0 errors**,
   offline overall exit 0, stderr empty, 522.61 s. Population 182 → 183 with
   the new regression and no lost nodes.
2. **Reviewer combined-defect battery** (`work/probe-runs-004`, candidate and
   genuine-baseline modes with in-band module-origin proof): Section A —
   **8/8 exact parity with the staged baseline**, including all five F01 cases
   now raising the original errors (`features_required_whenever_predictions_exist`,
   `duplicate_forecast_id`, `duplicate_label_request_id`,
   `block_id_key_set_must_equal_prediction_request_ids`,
   `mixed_model_id_in_calibration_inputs`); single-defect and empty cases
   unchanged. Section B — tuple/view parity restored (no divergence between
   the candidate's own entries).
3. **Meaningful owner regression**: `test_combined_defect_priority_matches_
   baseline_and_view_parity` pins all five cases on both entries plus
   single-defect controls and the all-empty view case — it ran and passed in
   the independent rerun.

## Everything else re-verified or validly reused

- **Freeze/write-set:** all six files match owner-004 FREEZE.json before and
  after this review (hashes derived from real bytes by this reviewer); exactly
  calibration.py and its test changed vs the 003 revision; staged footprint
  595 unchanged / 0 missing / `_fold_records.py` still absent (FUTURE_OUTPUT).
- **Test integrity:** cumulative diff vs baseline `9d875a4` is a single
  end-of-file hunk, **+206/−0** (the 001 additions intact plus the new
  regression); 27 original + 6 + 1 = 34 test functions; zero deletions; no
  skip/xfail markers.
- **Battery sections re-run on the repaired source:** store counterexamples
  35/35 true; encoding parity all-true (Decimal value/encoding separation,
  tricky IDs, 15-nesting, generator inputs, kind-rejection message parity; NaN
  unconstructible via constructors); fit/reference/evidence equivalences all
  true with identical tuple/view errors — the changed calibration.py was
  fully re-exercised through public, view and fit-evidence paths.
- **Reused unchanged evidence:** 001–003 store/encoding/capacity conclusions
  remain valid for the four byte-identical files; the capacity-first §5
  conclusion stands on reservation facts validated in 003 (no capacity rerun
  needed or performed).
- **Append-only corrections:** CORRECTION-F02-F03.md verified accurate against
  my independently recomputed hashes; original records untouched.

## Observations (non-blocking)

- **O04:** owner-004 DELIVERY.md's prose table carries a corrupted
  regimes.py hash (65 chars, diverges from FREEZE.json at index 26). FREEZE.json
  equals the real bytes, and this review accepts only byte-derived pins, so no
  accepted pin is affected — prose-only cosmetic defect.
- **O05:** my own 003 narrative said "36/36" store checks; the battery has 35
  checks, all true — corrected here; conclusion unaffected.

## Exclusions (unchanged)

Full MINUTE output/cold chain (capacity-blocked per §5), `experiments.py`
wiring (CPU owner's unit, may now integrate the exact interface bytes per
§6), whole-population 2 GiB in-process verification (NOT_RUN, needs that
wiring), DAILY/aggregate/label sizes (UNKNOWN, not zeroed). This PASS accepts
none of those.

Safety: `enable_trading=False`, observer=`hold`, broker=`NOT_RUN`, real
send/cancel `0/0`; no credentials/.env/network/DB/SDK/services/UI/broker/Git
access; no configuration changes; no product edits by the reviewer; failed
harness attempts from 002 preserved immutably; this review's streams were
sealed only after their producers terminated. Writing stops with this verdict.
