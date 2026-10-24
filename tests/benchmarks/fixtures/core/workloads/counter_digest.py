"""SYNTHETIC core workload: per-operation digest accumulator.

NF20 harness-validation fixture (case ``core/counter_digest``).  It is NOT a
performance claim of any kind: its only job is to exercise the C20
per-operation measurement path - one harness-timed ``operation()`` call per
sample, a deterministic business digest, and a frozen verification rule.

Determinism: every value derives from the operation counter and the frozen
workload seed; no clocks, no randomness, no I/O.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict

_PRIME = (1 << 61) - 1


class CounterDigestWorkload:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.count = 0
        self.value_total = 0
        self._acc = hashlib.sha256()

    def setup(self, ctx: Any) -> None:
        self.count = 0
        self.value_total = 0
        self._acc = hashlib.sha256()

    def operation(self, ctx: Any) -> Dict[str, Any]:
        self.count += 1
        value = (self.count * 2654435761 + self.ctx.seed) % _PRIME
        self.value_total += value
        self._acc.update(b"%d:%d;" % (self.count, value))
        return {"ok": True, "units": 1}

    def teardown(self, ctx: Any) -> None:
        pass  # nothing to release; the harness owns tmp cleanup

    def verify(self, ctx: Any) -> Dict[str, Any]:
        digest = self._acc.hexdigest()
        return {
            "status": "PASS" if self.count > 0 else "FAIL",
            "digest": digest,
            "summary": {
                "operations": self.count,
                "units": self.count,
                "value_total": self.value_total,
                "seed": self.ctx.seed,
            },
            "fail_reasons": [] if self.count > 0 else ["no operations executed"],
        }


def build(ctx: Any) -> CounterDigestWorkload:
    return CounterDigestWorkload(ctx)
