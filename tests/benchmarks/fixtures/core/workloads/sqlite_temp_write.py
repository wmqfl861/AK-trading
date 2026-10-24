"""SYNTHETIC core workload: declared temp-SQLite row writes.

NF20 harness-validation fixture (case ``core/sqlite_temp_write``).  Exercises
the C20 declared-resource path: the case manifest declares
``resources.temp_sqlite = true`` and the harness provisions a database file
inside the run-scoped temporary directory (created and removed by the
harness, H20.2 cleanup scope).  The workload performs real local I/O through
the standard-library sqlite3 driver - no network, no production database.

Determinism: payloads derive from the row counter and the frozen seed.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any, Dict


class SqliteTempWriteWorkload:
    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.conn = None
        self.inserted = 0

    def setup(self, ctx: Any) -> None:
        path = ctx.resources.get("temp_sqlite_path")
        if path is None:
            raise RuntimeError("temp_sqlite resource not provisioned by the harness")
        self.conn = sqlite3.connect(str(path))
        self.conn.execute("CREATE TABLE rows (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT)")
        self.conn.commit()
        self.inserted = 0

    def operation(self, ctx: Any) -> Dict[str, Any]:
        self.inserted += 1
        payload = "row-%d-%d" % (self.inserted, self.ctx.seed)
        self.conn.execute("INSERT INTO rows (payload) VALUES (?)", (payload,))
        return {"ok": True, "units": 1}

    def teardown(self, ctx: Any) -> None:
        if self.conn is not None:
            self.conn.commit()
            self.conn.close()
            self.conn = None

    def verify(self, ctx: Any) -> Dict[str, Any]:
        cur = self.conn.execute("SELECT COUNT(*), COALESCE(SUM(id), 0) FROM rows")
        count, id_sum = cur.fetchone()
        digest = hashlib.sha256(
            ("count=%d|id_sum=%d|seed=%d" % (count, id_sum, self.ctx.seed)).encode("utf-8")
        ).hexdigest()
        ok = count == self.inserted
        return {
            "status": "PASS" if ok else "FAIL",
            "digest": digest,
            "summary": {
                "rows": count,
                "id_sum": id_sum,
                "inserted_calls": self.inserted,
                "seed": self.ctx.seed,
            },
            "fail_reasons": [] if ok else ["row count %d != executed inserts %d" % (count, self.inserted)],
        }


def build(ctx: Any) -> SqliteTempWriteWorkload:
    return SqliteTempWriteWorkload(ctx)
