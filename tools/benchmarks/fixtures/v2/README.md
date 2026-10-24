# NF21 v2 benchmark fixtures (all inputs SYNTHETIC)

Owner: NF21 (v2 benchmark seat), work order
`docs/devplan-next-frontier-20260924-r1/work-orders/NF21-v2-benchmark.md`,
handed over under C20 (`docs/benchmarks/README.md` §12).  Nothing in this
directory is historical PIT data, production data or a real model.

## Contents

| File | Role |
| --- | --- |
| `feature_prefix.json` | frozen inputs for `v2/feature_prefix_cold` / `v2/feature_prefix_hot` |
| `decision_replay.json` | frozen inputs + pre-registered counts for `v2/decision_replay_wait_only` / `v2/decision_replay_filled` |
| `validation.json` | frozen inputs for `v2/validation_forecasts` / `v2/validation_bootstrap` / `v2/validation_replay_metrics` |
| `source-map.json` | NF21 source map binding the measured `ak_trading.v2.*` modules (WORKTREE_SNAPSHOT provenance, per-file sha256/bytes) |

## Generation rules (frozen)

* Every numeric series is an integer-index formula listed in the JSON
  (`bar_formula`, `label_formula`, `value_formula`); there is no randomness
  except the seed-driven production bootstrap RNG
  (`validation.metrics.block_bootstrap`, seed `20260924`), no clock reads
  inside workloads, and no I/O.
* Input shapes mirror frozen v2 test fixtures so legal, non-degenerate
  inputs are exercised (feature snapshots produce READY factor rows; the
  filled replay produces candidate selection, risk filtering, next-window
  fills, fees and reconciled equity): the M2 causality session/bar
  constructors and the M4 ledger/replay fixtures.  Test modules are never
  imported by workloads - the constructors are replicated in
  `tools/benchmarks/workloads/v2.py` and this file pins their parameters.
* The three pre-registered feature prefix tiers are 90 / 150 / 300 minute
  bars of one 400-slot synthetic DAY session; every evaluation cuts off at
  the completion time of the prefix's last planned slot.
* Pre-registered replay counts (`expected` blocks) are enforced by each
  workload's `verify()`; the frozen smoke `expected_digest` in the case
  manifests binds the full business output.
* The 1000-replicate trading-day block bootstrap of
  `src/ak_trading/v2/validation/metrics.py` is preserved untouched;
  workloads refuse to run if that semantic shrinks.

## Hash discipline

`source-map.json`, the case manifests under `tools/benchmarks/cases/v2/`
and every `expected_digest` are produced programmatically (scripted
sha256 over the canonical C20 JSON encoding); no hash is hand-copied.
The generation/verification commands are recorded in
`docs/next-frontier-evidence-20260924-r1/NF21/r1/`.
