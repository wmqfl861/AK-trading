"""SYNTHETIC core workload: amortized batch row transforms.

NF20 harness-validation fixture (case ``core/amortized_rows``).  Exercises
the C20 amortized measurement path: the harness times whole ``batch(ctx, n)``
calls (one sample per call, never per-row latency), and the verification
digest covers the accumulated business outputs.

Determinism: rows derive from the batch index, the row index and the frozen
workload seed; no clocks, no randomness, no I/O.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict

_PRIME = (1 << 61) - 1


class AmortizedRowsWorkload:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.batches = 0
        self.rows = 0
        self.value_total = 0
        self._acc = hashlib.sha256()

    def setup(self, ctx: Any) -> None:
        self.batches = 0
        self.rows = 0
        self.value_total = 0
        self._acc = hashlib.sha256()

    def batch(self, ctx: Any, n: int) -> Dict[str, Any]:
        done = 0
        for i in range(int(n)):
            value = ((self.batches * 1_000_003 + i) * 2654435761 + self.ctx.seed) % _PRIME
            self.value_total += value
            self._acc.update(b"%d:%d:%d;" % (self.batches, i, value))
            done += 1
        self.batches += 1
        self.rows += done
        return {"ok": True, "units": done}

    def teardown(self, ctx: Any) -> None:
        pass

    def verify(self, ctx: Any) -> Dict[str, Any]:
        ok = self.rows > 0
        return {
            "status": "PASS" if ok else "FAIL",
            "digest": self._acc.hexdigest(),
            "summary": {
                "batches": self.batches,
                "rows": self.rows,
                "value_total": self.value_total,
                "seed": self.ctx.seed,
            },
            "fail_reasons": [] if ok else ["no rows transformed"],
        }


def build(ctx: Any) -> AmortizedRowsWorkload:
    return AmortizedRowsWorkload(ctx)
