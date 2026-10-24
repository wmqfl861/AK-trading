"""AK trading offline benchmark harness (NF20, C20 contract).

Public surface (frozen by C20-1.0.0):

* :mod:`tools.benchmarks.contracts` - version identities, statistics
  (nearest-rank quantiles, unscaled MAD), path containment rules, manifest /
  suite / source-map / policy validators, workload protocol, environment
  capture allow-list.
* :mod:`tools.benchmarks.runner` - case execution under frozen budgets
  (explicit inputs hashed before import, harness-owned ``perf_counter_ns``
  timing, subprocess batches, correctness-first status machine, end-of-run
  drift re-hash).
* :mod:`tools.benchmarks.compare` - baseline store, matching, verdict state
  machine, manual promotion, drift reports.
* :mod:`tools.benchmarks.cli` - the repeatable entry point
  (``python -B -m tools.benchmarks.cli``).

``tools`` is an implicit namespace package by design: this project adds no
``tools/__init__.py`` so the directory stays exclusively owned by this work
order's write set.  Run the CLI from the repository root.
"""

from tools.benchmarks import contracts as _contracts

CONTRACT_VERSION = _contracts.CONTRACT_VERSION
HARNESS_VERSION = _contracts.HARNESS_VERSION

__all__ = ["CONTRACT_VERSION", "HARNESS_VERSION", "contracts"]
