# REVIEW — t7-wiring-hard-review-006, author007 repair re-review (2026-10-27)

**Independent hard re-review.** Same independent ZCode `GLM-5.3`/`max`
reviewer session as reviews 001–005; no part in the development or repair.
Unit: owner-004's frozen repair of the wiring two-file unit in
`C:\AKWorktrees\aktr-goal-t7-wiring-20261027-r2`, reviewed against
`astra-t7-wiring-scope-007/AUTHOR-DISPOSITION.md` (read in full) on the
retained MEMORY003/PERFORMANCE002/EXECUTION001 basis. Review005's PASS was
not accepted for three gaps — guarded-launcher execution, W1 exact dict
semantics, real inner-consumer lifecycle — all three of which this review
closes with independent evidence.

## Verdict

**PASS — unconditional**, scoped to the wiring two-file unit at
`experiments.py e5c4f315…` / `test_v2_m5_experiments.py ff770777…`. This
verdict does not credit natural OPEN/Fill/maturity evidence, the 121-node T7
population, target locks, any cold chain, or the MINUTE capacity decision —
all remain downstream obligations.

## Integrity (verified, not taken from the delivery)

Both owned files match owner-004's FREEZE; the 602-pin staging sweep shows
exactly those two files changed; dependencies byte-identical to their
accepted revisions (CPU review-004, memory review-004); restored local inputs
and Root's P02 contract byte-verified. The whitespace-normalized functional
diff against the review005-accepted `experiments.py` is confined to the new
`_partition_feature_view`, the `fs()` call-site, the
`_fit_training_payoffs` fold-inside-guard and comment changes — the artifact
streaming region is untouched.

## §1 Guarded launcher (author007's execution contract)

Independently executed exactly as prescribed, exact original file sets from
the sealed argv, fresh short basetemps, candidate cwd:

| Guarded suite | Selected / ran / passed | Exit |
| --- | --- | --- |
| M2 six-file | 96 / 96 / **96 passed** | 0 |
| M3A six-file | 183 / 183 / **183 passed** | 0 |
| `tests/test_v2_m5_experiments.py` | 37 / 37 / **37 passed** | 0 |

No `-k`, filters or skips; empty stderr; no collection errors. My first
attempt exited 4 (`--basetemp must not already exist` — I had pre-created the
directory); preserved in `work/suite-logs/` and corrected. (The owner's
authoritative raw walls/peaks — M2 46.67 s, M3A 570.90 s, M5 1075.71 s, all
raw 0 — are bound as record corrections in FINDINGS R1; the DELIVERY table's
figures are inaccurate.)

## §2 W1 — exact dict semantics (the rejected 005 disposition, now fixed)

`fs(name)` is now `_partition_feature_view`: per-request `features_ref`
resolution, **first key position, LAST referencing request's representation**,
chosen by exact canonical-byte match among the store's slots for that id.

- **Real partitions**: on a real `_fit_fold` (14d/30s world), TRAIN (708
  requests / 118 unique ids) and CALIBRATION (168/28) views match the ORIGINAL
  dict comprehension `{r.features.snapshot_id: r.features for r in requests}`
  in key order and member values, by canonical bytes and dataclass equality.
- **Crafted A₁,B,A₂**: keys `[A, B]`; A's member is A₂'s exact bytes — never
  A₁'s and never an outside-partition record's; same-id **different-value**
  last-wins; both error paths raise their precise reasons
  (`request_features_snapshot_missing_from_store`,
  `request_features_representation_missing_from_store`); store populations
  unchanged. No full snapshot dict, no materialized source tuples.
- The owner's regression
  `test_t7w_partition_feature_view_first_key_last_real_representation` ran
  green inside the guarded M5 suite. (One of my own probe assertions was
  initially mis-designed — the synthetic pair also differs in `source_bar_ids`
  — recorded in FINDINGS; the byte-level checks carry the case.)

## §3 Real inner-consumer lifecycle (the 005 surrogate gap, now closed)

Independent probe with **record-only observers** (the success path never
replaces a consumer; no empty early-return surrogate — the actual inner split
exists and models are real):

- **Success** (169 s): `_fit_training_payoffs` derived its actual inner split
  (41 train days), ran the REAL `_fit_fold` → real models with **1269
  snapshots / 17664 requests**, then THREE real payoff windows consumed via
  the real `_training_opportunity` with the **store alive at every consumer
  entry**; per-window plan/path/opportunity/manifest payloads produced;
  honest `NO_TRIGGERED_BUDGETED_PATH` exclusions kept as-is (no natural OPEN
  fabricated). After return: store sealed+closed, closed views keep the real
  population, `StoreClosedError` on materialization, extracted
  models/reference still valid.
- **Consumer-error exit**: a consumer-position raise propagates; the delivered
  store is released.
- **In-fold-error exit**: an exception inside the real `_fit_fold` after store
  creation is released by the fold's own guard (store captured via a
  record-only `_new_record_store` wrapper: 1 created, closed).

## Equivalence (author007's reuse boundary honored)

The repaired `_fit_fold` was compared against the **sealed review005-accepted
bytes** (`b7ed1e4e`, loaded read-only) on the identical world: 434 snapshots /
2,136 requests equal in records and canonical file bytes; labels, assignments,
exclusions, reference, seasonal, forecasts equal; models equal in
`model_set_id`, `content_hash`, evidence, models and source hashes. W5
(non-empty 42-file streaming) and W6 (store encoding semantics) are reused
after confirming the streaming region and `_fold_records.py` unchanged; both
also ran green inside the guarded M5 suite.

## Not resolved (NOT_RUN; not waived)

No natural OPEN observed; positive OPEN/Fill/maturity, the full 121-node T7
population, target locks and the F→R0→M1 L0→R1→L1→R2 chain remain downstream
T7 obligations. No full DAILY/MINUTE cold chain, no M5 formal run, no M4 work
(separate owner, accepted elsewhere). `enable_trading=False`, observer `hold`,
`broker=NOT_RUN`, real send/cancel `0/0`; no credentials/env/network/DB/SDK/
UI/Git; zero product edits. Evidence: `work/suite-logs/` (raw guarded
streams/exits incl. my preserved exit-4 attempt), `work/probes/rv6-*` and
`work/pins-before-publish.txt`.
