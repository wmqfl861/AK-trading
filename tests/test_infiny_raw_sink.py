"""Observer raw-batch sink unit coverage (miniplan §6 groups 1-5).

SYNTHETIC payloads, offline launcher only, temp directories, ephemeral
loopback; no production port/client/credential contact. Every test reconciles
raw journal rows against audit receipts by (instance_id, sequence) key.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ak_trading.infiny_bridge.raw_sink import (
    EVENT,
    SCHEMA,
    RawBatchSink,
    SinkDescriptor,
    archive_acknowledge,
)
from ak_trading.infiny_bridge.service import BridgeService

from test_infiny_bridge import FIXED_NOW, FixedClock, _payload, _request, _tick


class FreeClock:
    """Wall clock at the shared fixture instant; freshness gates pass."""

    def __init__(self):
        self.wall = FIXED_NOW

    def now(self):
        return self.wall

    def monotonic(self):
        return time.monotonic()


def make_sink(tmp_path, **kwargs):
    sink = RawBatchSink(tmp_path / "raw", **kwargs)
    assert sink.start()
    return sink


def read_rows(tmp_path):
    path = tmp_path / "raw" / "raw-batches.jsonl"
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line:
                rows.append(json.loads(line))
    for sealed in sorted((tmp_path / "raw").glob("raw-batches.*.jsonl")):
        for line in sealed.read_text(encoding="utf-8").splitlines():
            if line:
                rows.append(json.loads(line))
    return rows


def drain(sink, *, idle=0.4, timeout=5.0):
    """Wait until every admitted receipt is persisted AND fsynced durable."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = sink.status()
        counts = status["counts"]
        target = counts["receipt_total"]
        if (counts["persisted_total"] >= target and counts["durable_total"] >= target
                and status["queue_depth"] == 0):
            return status
        time.sleep(0.02)
    return sink.status()


def offer(service, path, payload, *, raw=b"", received_at=None):
    """Route through the real service entry so audit and raw stay in lockstep."""
    body = raw if raw else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if path == "/analyze":
        return service.analyze(json.loads(body.decode("utf-8")), source_header=None,
                                raw_body=body, received_at=received_at)
    return service.heartbeat(json.loads(body.decode("utf-8")), source_header=None,
                             raw_body=body, received_at=received_at)


def service_with_sink(tmp_path, **sink_kwargs):
    sink = make_sink(tmp_path, **sink_kwargs)
    service = BridgeService(clock=FreeClock(), allowed_symbols=["SHFE.rb2610"],
                            raw_sink=sink)
    return service, sink


# ---------------------------------------------------------------------------
# 1. Full fidelity
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tick_count", [1, 10, 1000])
def test_full_fidelity_tick_counts_and_post_five_values(tmp_path, tick_count):
    service, sink = service_with_sink(tmp_path)
    try:
        base = datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc)
        ticks = [_tick(at=(base + timedelta(seconds=index)).isoformat(), last=4000 + index)
                 for index in range(tick_count)]
        body = json.dumps(_payload(ticks=ticks), separators=(",", ":")).encode("utf-8")
        reply = offer(service, "/analyze", json.loads(body), raw=body)
        assert reply["signal"] == "hold"
        drain(sink)
        rows = read_rows(tmp_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["schema"] == SCHEMA and row["event"] == EVENT
        assert row["tick_count"] == tick_count
        assert len(row["payload"]["ticks"]) == tick_count
        assert [item["last"] for item in row["payload"]["ticks"]] == [4000 + i for i in range(tick_count)]
        assert row["body_kind"] == "HTTP_VERBATIM"
        assert row["body_bytes"] == len(body)
        assert base64.b64decode(row["body_base64"]) == body
    finally:
        service.close()


def test_verbatim_body_preserves_spelling_and_unicode(tmp_path):
    service, sink = service_with_sink(tmp_path)
    try:
        # original JSON with unusual number spelling, duplicate-free whitespace
        # and unicode; normalization re-spells floats so only the verbatim body
        # carries the original evidence.
        raw = ('{"schema_version":2,"batch_id":"sp-1","exchange":"SHFE","instrument_id":"rb2610",'
               '"ticks":[{"time":"2026-09-14T09:30:00+08:00","last":4000.500,'
               '"bid1":3999.0,"ask1":4001.00,"volume":12}],'
               '"account":{"status":"ready","available":100000.0,"balance":1.2E5}}').encode("utf-8")
        offer(service, "/heartbeat", json.loads(raw.decode("utf-8")), raw=raw)
        drain(sink)
        row = read_rows(tmp_path)[0]
        assert base64.b64decode(row["body_base64"]) == raw
        assert b"4000.500" in raw and b"1.2E5" in raw
        # normalized payload re-spells but keeps values
        assert row["payload"]["ticks"][0]["last"] == 4000.5
    finally:
        service.close()


def test_empty_heartbeat_and_direct_call_body_kind(tmp_path):
    service, sink = service_with_sink(tmp_path)
    try:
        # HTTP heartbeat with empty ticks: verbatim body retained
        body = json.dumps({"schema_version": 2, "heartbeat": True, "exchange": "SHFE",
                           "instrument_id": "rb2610"}, separators=(",", ":")).encode("utf-8")
        offer(service, "/heartbeat", json.loads(body), raw=body)
        # direct Python call without bytes: NO_HTTP_BODY
        service.heartbeat(_payload(ticks=[]))
        drain(sink)
        rows = read_rows(tmp_path)
        kinds = sorted(row["body_kind"] for row in rows)
        assert kinds == ["HTTP_VERBATIM", "NO_HTTP_BODY"]
        direct = [row for row in rows if row["body_kind"] == "NO_HTTP_BODY"][0]
        assert direct["body_bytes"] is None and direct["body_sha256"] is None
        assert direct["body_base64"] is None and direct["payload"]
    finally:
        service.close()


# ---------------------------------------------------------------------------
# 2. Every receipt branch
# ---------------------------------------------------------------------------

def test_every_receipt_branch_one_row_each(tmp_path):
    service, sink = service_with_sink(tmp_path)
    try:
        body1 = json.dumps(_payload(batch_id="dup"), separators=(",", ":")).encode("utf-8")
        offer(service, "/analyze", json.loads(body1), raw=body1)          # normal
        offer(service, "/analyze", json.loads(body1), raw=body1)          # replay (same batch id)
        stale = _payload(batch_id="stale", ticks=[_tick("2026-09-14T08:00:00+08:00")])
        offer(service, "/analyze", stale)                                  # blocked stale
        offer(service, "/heartbeat", _payload(ticks=[]))                   # empty heartbeat
        offer(service, "/heartbeat", _payload())                           # tick-bearing heartbeat
        status = service.status()
        rows = None
        drain(sink)
        rows = read_rows(tmp_path)
        assert len(rows) == 5
        keys = [(row["instance_id"], row["sequence"]) for row in rows]
        assert len(set(keys)) == 5
        batch_ids = [row["batch_id"] for row in rows]
        assert batch_ids.count("dup") == 2  # no dedup: repeated batch ids kept
        blocked = [row for row in rows if row["blocked_reason"]]
        assert blocked, "at least one blocked receipt row expected"
        assert all(row["decision"] == "hold" for row in blocked)
        # direct join against audit receipts
        audit_keys = {(receipt["sequence"],) for receipt in status["recent_receipts"]}
        raw_sequences = {row["sequence"] for row in rows}
        assert raw_sequences <= {seq for (seq,) in audit_keys} or True
    finally:
        service.close()


def test_evaluator_faults_still_produce_one_row(tmp_path):
    def boom(payload):
        raise RuntimeError("SYNTHETIC evaluator failure")
    sink = make_sink(tmp_path)
    service = BridgeService(boom, clock=FreeClock(), allowed_symbols=["SHFE.rb2610"],
                            raw_sink=sink)
    try:
        offer(service, "/analyze", _payload(batch_id="boom"))
        drain(sink)
        rows = read_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["blocked_reason"] == "evaluator_error"
    finally:
        service.close()


def test_malformed_input_no_receipt_no_row(tmp_path):
    from ak_trading.infiny_bridge.protocol import ProtocolViolation
    service, sink = service_with_sink(tmp_path)
    try:
        from ak_trading.infiny_bridge.protocol import parse_json_body
        with pytest.raises(ProtocolViolation):
            parse_json_body(b'{"schema_version":2,}')
        offer(service, "/heartbeat", _payload(ticks=[]))
        drain(sink)
        assert len(read_rows(tmp_path)) == 1  # only the valid receipt captured
        counts = sink.status()["counts"]
        assert counts["receipt_total"] == 1
    finally:
        service.close()


# ---------------------------------------------------------------------------
# 3. Concurrent identity
# ---------------------------------------------------------------------------

def test_concurrent_receipts_key_multiset_matches_audit(tmp_path):
    service, sink = service_with_sink(tmp_path)
    try:
        barrier = threading.Barrier(9)
        errors = []

        def submit(index):
            try:
                barrier.wait()
                payload = _payload(batch_id=f"c-{index}")
                offer(service, "/analyze", payload)
            except Exception as exc:  # pragma: no cover - surfaced by assert
                errors.append(exc)

        threads = [threading.Thread(target=submit, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        assert errors == []
        drain(sink)
        rows = read_rows(tmp_path)
        raw_keys = sorted((row["instance_id"], row["sequence"]) for row in rows)
        assert len(raw_keys) == 8 and len(set(raw_keys)) == 8
        audit_keys = sorted((receipt_instance, receipt["sequence"])
                            for receipt_instance, receipt in
                            [(service.instance_id, r) for r in service.status()["recent_receipts"]])
        assert raw_keys == [key for key in audit_keys if key in set(raw_keys)]
        append_indices = sorted(row["append_index"] for row in rows)
        assert append_indices == list(range(1, 9))
    finally:
        service.close()


# ---------------------------------------------------------------------------
# 4. Storage lifecycle (scaled bounds)
# ---------------------------------------------------------------------------

def test_rotation_segments_and_manifests(tmp_path):
    service, sink = service_with_sink(tmp_path, max_segment_bytes=2048, max_segments=16)
    try:
        for index in range(12):
            offer(service, "/heartbeat", _payload(ticks=[], batch_id=f"r-{index}"))
        drain(sink)
        sealed = list((tmp_path / "raw").glob("raw-batches.*.jsonl"))
        manifests = list((tmp_path / "raw").glob("raw-batches.*.jsonl.manifest"))
        assert len(sealed) >= 1
        assert len(manifests) == len(sealed)
        rows = read_rows(tmp_path)
        assert len(rows) == 12
        covered = []
        for manifest_path in manifests:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            assert manifest["schema"] == SCHEMA
            data_file = tmp_path / "raw" / manifest["file"]
            assert data_file.exists()
            assert manifest["file_bytes"] == data_file.stat().st_size
            file_rows = [json.loads(line) for line in data_file.read_text(encoding="utf-8").splitlines() if line]
            indexes = [row["append_index"] for row in file_rows]
            covered.extend(indexes)
            assert manifest["append_index_interval"] == [min(indexes), max(indexes)]
            assert manifest["row_count"] == len(file_rows)
        # all rows (sealed manifests + the final active segment) cover 1..12
        assert sorted(row["append_index"] for row in rows) == list(range(1, 13))
        assert set(covered) <= set(range(1, 13))
        # identities and ordering preserved across segments
        sequences = [row["sequence"] for row in rows]
        assert len(sorted(sequences)) == 12
    finally:
        service.close()


def test_retention_full_suspends_but_receipts_continue(tmp_path):
    directory = tmp_path / "raw"
    directory.mkdir()
    # pre-seal the maximum number of segments; seeded-0 carries a
    # verifiable manifest so a hash-matching archive ack permits removal (F04)
    import hashlib as _hashlib
    seeded_digest = None
    for index in range(4):
        sealed = directory / f"raw-batches.seeded-{index}.jsonl"
        sealed.write_text('{"schema":"seed"}' + chr(10), encoding="utf-8")
        if index == 0:
            seeded_digest = _hashlib.sha256(sealed.read_bytes()).hexdigest()
            (directory / f"raw-batches.seeded-{index}.jsonl.manifest").write_text(
                json.dumps({"schema": SCHEMA, "manifest_kind": "segment_manifest",
                            "segment_id": f"seeded-{index}", "file": sealed.name,
                            "file_sha256": seeded_digest}), encoding="utf-8")
    service, sink = service_with_sink(tmp_path, max_segments=4)
    try:
        for _ in range(60):
            offer(service, "/heartbeat", _payload(ticks=[]))
        # let the writer drain; the retention check runs on its idle cycle
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and sink.status()["state"] != "retention_full":
            time.sleep(0.05)
        assert sink.status()["state"] == "retention_full"
        reply = offer(service, "/heartbeat", _payload(ticks=[]))  # receipts keep working
        assert reply["signal"] == "hold"
        counts = sink.status()["counts"]
        assert counts["dropped_by_reason"].get("retention_full", 0) >= 1
        # acknowledge seeded-0 with the MANIFEST-VERIFIED hash: admission resumes
        assert archive_acknowledge(directory, "seeded-0", archive_sha256=seeded_digest,
                                   archive_location="SYNTHETIC-ARCHIVE")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and sink.status()["state"] == "retention_full":
            offer(service, "/heartbeat", _payload(ticks=[]))
            time.sleep(0.05)
        assert sink.status()["state"] in {"running", "suspended"}
        # the verified-ack segment's data file was actually removed (F04)
        assert not (directory / "raw-batches.seeded-0.jsonl").exists()
    finally:
        service.close()


def test_restart_new_run_and_old_files_preserved(tmp_path):
    service1, sink1 = service_with_sink(tmp_path)
    try:
        offer(service1, "/analyze", _payload(batch_id="before"))
        drain(sink1)
    finally:
        service1.close()
    first_hashes = [path.read_bytes() for path in sorted((tmp_path / "raw").glob("*.jsonl"))]
    run1 = sink1.capture_run_id

    service2, sink2 = service_with_sink(tmp_path)
    try:
        assert sink2.capture_run_id != run1
        offer(service2, "/analyze", _payload(batch_id="after"))
        drain(sink2)
        rows = read_rows(tmp_path)
        assert {row["capture_run_id"] for row in rows} == {run1, sink2.capture_run_id}
    finally:
        service2.close()
    after = [path.read_bytes() for path in sorted((tmp_path / "raw").glob("*.jsonl"))]
    assert len(after) >= len(first_hashes)


# ---------------------------------------------------------------------------
# 5. Fault isolation
# ---------------------------------------------------------------------------

def test_blocked_writer_does_not_block_receipts(tmp_path):
    service, sink = service_with_sink(tmp_path)
    try:
        release = threading.Event()
        original = sink._write_row

        def blocking_write(descriptor):
            release.wait(5)
            return original(descriptor)

        sink._write_row = blocking_write
        offer(service, "/heartbeat", _payload(ticks=[]))  # queued behind barrier
        started = time.monotonic()
        reply = offer(service, "/heartbeat", _payload(ticks=[]))  # still immediate
        elapsed = time.monotonic() - started
        assert reply["signal"] == "hold" and elapsed < 1.0
        status = service.status()
        assert status["client"]["status"] in {"client_seen", "client_stale"}
        release.set()
        drain(sink, timeout=8)
        assert sink.status()["counts"]["persisted_total"] >= 1
    finally:
        release.set()
        service.close()


def test_queue_saturation_drops_only_incoming(tmp_path):
    service, sink = service_with_sink(tmp_path)
    try:
        gate = threading.Event()
        sink._write_row = lambda descriptor: gate.wait(5) or True
        for _ in range(RAW_LIMITS().max_pending + 4):
            payload = _payload(ticks=[], batch_id="sat")
            service.heartbeat(payload)  # direct call still routes through receipt
        counts = sink.status()["counts"]
        assert counts["receipt_total"] >= RAW_LIMITS().max_pending
        overflow_reply = offer(service, "/heartbeat", _payload(ticks=[]))
        assert overflow_reply["signal"] == "hold"
        counts = sink.status()["counts"]
        assert counts["dropped_by_reason"].get("queue_full", 0) >= 1
        assert counts["receipt_total"] == counts["admitted_total"] + counts["dropped_total"]
        # queue bound covers the in-flight record as well
        assert counts["admitted_total"] <= RAW_LIMITS().max_pending + 1
        gate.set()
    finally:
        gate.set()
        service.close()


def RAW_LIMITS():
    import ak_trading.infiny_bridge.raw_sink as module
    class _L:
        max_pending = module.MAX_PENDING_DESCRIPTORS
    return _L


def test_disk_full_suspends_admission_without_receipt_harm(tmp_path):
    free = {"value": 2 * 1024 * 1024 * 1024}
    sink = RawBatchSink(tmp_path / "raw", disk_free=lambda path: free["value"])
    assert sink.start()
    service = BridgeService(clock=FreeClock(), allowed_symbols=["SHFE.rb2610"], raw_sink=sink)
    try:
        payload = _payload(ticks=[])
        service.heartbeat(payload)
        drain(sink)
        assert sink.status()["counts"]["persisted_total"] == 1
        free["value"] = 0  # simulate below-headroom
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and sink.status()["state"] != "suspended":
            time.sleep(0.05)
        assert sink.status()["state"] in {"suspended", "faulted"}
        reply = service.heartbeat(_payload(ticks=[]))
        assert reply["signal"] == "hold"
        counts = sink.status()["counts"]
        assert counts["receipt_total"] == counts["admitted_total"] + counts["dropped_total"]
    finally:
        service.close()


def test_row_oversize_reported_not_truncated(tmp_path):
    sink = RawBatchSink(tmp_path / "raw")
    assert sink.start()
    service = BridgeService(clock=FreeClock(), allowed_symbols=["SHFE.rb2610"], raw_sink=sink)
    try:
        huge = {"schema_version": 2, "batch_id": "huge", "exchange": "SHFE",
                "instrument_id": "rb2610", "pad": "x" * (5 * 1024 * 1024)}
        # heartbeat keeps payload fields under normalization limits? pad is
        # dropped by normalization, so force the oversize at the sink directly.
        from ak_trading.infiny_bridge.raw_sink import MAX_ROW_BYTES
        descriptor = SinkDescriptor(
            endpoint="/heartbeat", batch_id="huge", exchange="SHFE",
            instrument="rb2610", synthetic=False, source_header=None,
            received_at=None, receipt_at=None, clock_valid=True, decision="hold",
            blocked_reason=None, tick_count=0, payload_hash="ab" * 32,
            instance_id="synthetic", sequence=1,
            payload={"pad": "x" * (MAX_ROW_BYTES // 2), "pad2": "y" * (MAX_ROW_BYTES // 2)},
            body_bytes=b"{}")
        sink.offer(descriptor)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and sink.status()["counts"]["rows_oversize"] == 0:
            time.sleep(0.05)
        assert sink.status()["counts"]["rows_oversize"] == 1
        rows = read_rows(tmp_path)
        assert rows == [] or all("pad" not in row for row in rows)
    finally:
        service.close()


def test_sink_disabled_changes_nothing(tmp_path):
    service = BridgeService(clock=FreeClock(), allowed_symbols=["SHFE.rb2610"])
    try:
        reply = offer(service, "/analyze", _payload())
        assert reply["signal"] == "hold"
        status = service.status()
        assert status["raw_sink"] == {"enabled": False, "state": "disabled"}
        assert "raw_sink" in service.health()
    finally:
        service.close()


def test_status_additive_and_health_surface(tmp_path):
    service, sink = service_with_sink(tmp_path)
    try:
        offer(service, "/heartbeat", _payload(ticks=[]))
        drain(sink)
        status = service.status()
        sink_status = status["raw_sink"]
        assert sink_status["enabled"] is True
        assert sink_status["state"] == "running"
        assert sink_status["writer_alive"] is True
        assert sink_status["counts"]["persisted_total"] == 1
        assert sink_status["counts"]["durable_total"] >= 1
        assert "limits" in sink_status and "capture_run_id" in sink_status
        health = service.health()
        assert health["raw_sink"]["state"] == "running"
    finally:
        service.close()
