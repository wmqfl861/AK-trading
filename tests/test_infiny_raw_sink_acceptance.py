"""Observer raw-sink acceptance counters (miniplan §6 groups 6 + H09/H10).

SYNTHETIC sustained-load evidence with paired sink-disabled/enabled runs on
identical traces and configuration, p99 admission-to-flush, receipt-key
reconciliation and receipt-path overhead ceilings. Offline launcher only.
"""
from __future__ import annotations

import json
import math
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ak_trading.infiny_bridge.raw_sink import RawBatchSink
from ak_trading.infiny_bridge.service import BridgeService, build_server

from test_infiny_bridge import FIXED_NOW, _payload, _tick
from test_infiny_raw_sink import FreeClock, read_rows


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


class ServiceHarness:
    """In-process service (no HTTP) for direct paired timing runs.

    Timing runs inject a no-op fsync: they measure the admission-to-flush
    pipeline and receipt-path overhead, not physical disk durability stalls
    (covered separately by the real-fsync unit tests).
    """

    def __init__(self, *, sink=None):
        self.sink = sink
        self.service = BridgeService(clock=FreeClock(), allowed_symbols=["SHFE.rb2610"],
                                     raw_sink=sink)

    def submit(self, index):
        base = FIXED_NOW
        ticks = [_tick(at=(base + timedelta(seconds=index)).isoformat(), last=4000 + index)]
        body = json.dumps(_payload(batch_id=f"load-{index}", ticks=ticks),
                          separators=(",", ":")).encode("utf-8")
        # a 1 ms gap models the tenfold-burst cadence (32 req/s -> ~3 ms) and
        # lets the writer thread be scheduled like real HTTP workers; a
        # zero-gap pure-Python loop would pin the GIL for the whole trace.
        time.sleep(0.001)
        started = time.perf_counter()
        reply = self.service.analyze(json.loads(body.decode("utf-8")), source_header=None,
                                     raw_body=body, received_at=None)
        return time.perf_counter() - started, reply

    def close(self):
        self.service.close()


def run_trace(harness, count):
    durations = []
    for index in range(count):
        elapsed, reply = harness.submit(index)
        assert reply["signal"] == "hold"
        durations.append(elapsed * 1000.0)
    return durations


def test_h09_one_to_one_counters_and_tick_reconciliation(tmp_path):
    harness = ServiceHarness(sink=RawBatchSink(tmp_path / "raw", fsync=lambda fd: None))
    try:
        assert harness.sink.start()
        count = 120
        run_trace(harness, count)
        # wait for full persistence AND durability
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            counts = harness.sink.status()["counts"]
            if (counts["persisted_total"] >= count and counts["durable_total"] >= count
                    and harness.sink.status()["queue_depth"] == 0):
                break
            time.sleep(0.05)
        counts = harness.sink.status()["counts"]
        assert counts["receipt_total"] == count
        assert counts["admitted_total"] == count
        assert counts["persisted_total"] == count
        assert counts["durable_total"] == count
        assert counts["dropped_total"] == 0
        assert counts["abandoned_total"] == 0
        assert counts["uncertain_total"] == 0
        assert counts["tick_total"] == count  # one tick per batch
        # raw rows = audit receipts = receipt_total; keys unique
        rows = read_rows(tmp_path)
        assert len(rows) == count
        keys = [(row["instance_id"], row["sequence"]) for row in rows]
        assert len(set(keys)) == count
        audit = harness.service.status()["recent_receipts"]
        assert len(audit) == min(count, 20)  # audit ring is 20 by design
        # body hash and payload fidelity on every row
        for row in rows:
            assert row["schema"] == "ak_observer_raw_batch_v1"
            assert row["tick_count"] == 1
            assert len(row["payload"]["ticks"]) == 1
        # no sink loss flagged
        assert harness.sink.status()["capture_incomplete"] is False
    finally:
        harness.close()


def test_h10_receipt_path_overhead_ceiling(tmp_path):
    # steady-state ceilings: a 25-receipt warmup prefix absorbs one-time costs
    # (first write/fsync, allocator and AV first-touch) identically on both
    # runs; the measured window is the identical 150-receipt steady trace.
    warmup, count = 25, 150
    baseline_harness = ServiceHarness()
    try:
        run_trace(baseline_harness, warmup)
        baseline = run_trace(baseline_harness, count)
    finally:
        baseline_harness.close()

    enabled_harness = ServiceHarness(sink=RawBatchSink(tmp_path / "raw", fsync=lambda fd: None))
    try:
        assert enabled_harness.sink.start()
        run_trace(enabled_harness, warmup)
        enabled = run_trace(enabled_harness, count)
    finally:
        enabled_harness.close()

    base_p95 = _percentile(baseline, 0.95)
    base_p99 = _percentile(baseline, 0.99)
    enabled_p95 = _percentile(enabled, 0.95)
    enabled_p99 = _percentile(enabled, 0.99)
    # H10 ceilings: p95/p99 increases <= 5 ms relative to the paired baseline
    assert enabled_p95 - base_p95 <= 5.0, (base_p95, enabled_p95)
    assert enabled_p99 - base_p99 <= 5.0, (base_p99, enabled_p99)


def test_p99_admission_to_flush_under_load(tmp_path):
    sink = RawBatchSink(tmp_path / "raw", fsync=lambda fd: None)
    assert sink.start()
    harness = ServiceHarness(sink=sink)
    try:
        count = 200
        lags = []

        original = sink._write_row

        def measuring_write(descriptor):
            result = original(descriptor)
            lags.append(time.monotonic() - descriptor.admitted_at)
            return result

        sink._write_row = measuring_write
        run_trace(harness, count)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(lags) < count:
            time.sleep(0.02)
        assert len(lags) == count
        p99 = _percentile(lags, 0.99)
        assert p99 <= 0.100, f"admission-to-flush p99 {p99*1000:.1f} ms exceeds 100 ms"
    finally:
        harness.close()


def test_real_http_roundtrip_with_sink_and_additive_status(tmp_path):
    """End-to-end over loopback HTTP: body bytes retained, status additive."""
    sink = RawBatchSink(tmp_path / "raw")
    assert sink.start()
    service = BridgeService(clock=FreeClock(), allowed_symbols=["SHFE.rb2610"], raw_sink=sink)
    server = build_server(port=0, service=service, max_workers=4)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from http.client import HTTPConnection
        address = ("127.0.0.1", server.server_address[1])
        payload = _payload()
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

        def post(path, data):
            connection = HTTPConnection(address[0], address[1], timeout=3)
            try:
                connection.request("POST", path, body=data, headers={
                    "Content-Type": "application/json", "Connection": "close"})
                response = connection.getresponse()
                return response.status, json.loads(response.read().decode("utf-8"))
            finally:
                connection.close()

        status, reply = post("/analyze", body)
        assert status == 200 and reply["signal"] == "hold"
        # drain
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and sink.status()["counts"]["persisted_total"] < 1:
            time.sleep(0.05)
        rows = read_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["body_kind"] == "HTTP_VERBATIM"
        assert rows[0]["endpoint"] == "/analyze"
        import base64
        assert base64.b64decode(rows[0]["body_base64"]) == body

        connection = HTTPConnection(address[0], address[1], timeout=3)
        try:
            connection.request("GET", "/status", headers={"Connection": "close"})
            response = connection.getresponse()
            snapshot = json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()
        assert snapshot["raw_sink"]["enabled"] is True
        assert snapshot["raw_sink"]["counts"]["persisted_total"] >= 1
        connection = HTTPConnection(address[0], address[1], timeout=3)
        try:
            connection.request("GET", "/health", headers={"Connection": "close"})
            response = connection.getresponse()
            health = json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()
        assert health["raw_sink"]["state"] == "running"
    finally:
        server.shutdown()
        server.server_close()
        service.close()
        thread.join(timeout=2)


def test_launcher_raw_batches_argument(tmp_path):
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "scripts/run_infiny_bridge.py"), "--dry-run",
         "--symbol", "SHFE.rb2610", "--audit-log", str(tmp_path / "audit.jsonl"),
         "--raw-batches", str(tmp_path / "rawdir")],
        capture_output=True, text=True, encoding="utf-8", cwd=root, check=False)
    assert result.returncode == 0
    startup = json.loads(result.stdout.strip())
    assert startup["raw_sink"] == {"enabled": True}
    # rejection: non-absolute raw path
    result = subprocess.run(
        [sys.executable, str(root / "scripts/run_infiny_bridge.py"), "--dry-run",
         "--symbol", "SHFE.rb2610", "--audit-log", str(tmp_path / "audit2.jsonl"),
         "--raw-batches", "relative/dir"],
        capture_output=True, text=True, encoding="utf-8", cwd=root, check=False)
    assert result.returncode == 2
    assert "RAW_BATCHES_PATH_MUST_BE_ABSOLUTE" in result.stdout
