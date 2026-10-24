"""r3 repair counterexamples: N1 resume, N2 durable subset, N3 manifest truth.

SYNTHETIC, offline, temp-dir. Each test mirrors the reviewer's demonstrated
defect class and asserts the r3 semantics.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from ak_trading.infiny_bridge.raw_sink import RawBatchSink, SinkDescriptor
from ak_trading.infiny_bridge.service import BridgeService

from test_infiny_bridge import FIXED_NOW, _payload, _tick
from test_infiny_raw_sink import FreeClock, read_rows


def make_descriptor(seq, *, payload=None):
    return SinkDescriptor(
        endpoint="/analyze", batch_id=str(seq), exchange="SHFE", instrument="rb2610",
        synthetic=True, source_header=None, received_at=None, receipt_at=None,
        clock_valid=True, decision="hold", blocked_reason=None, tick_count=1,
        payload_hash="ab" * 32, instance_id="probe", sequence=seq,
        payload=payload if payload is not None else {"probe": seq}, body_bytes=b"")


def offer_analyze(service, batch_id):
    from datetime import timedelta
    payload = _payload(batch_id=batch_id,
                       ticks=[_tick(at=(FIXED_NOW + timedelta(seconds=15)).isoformat())])
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return service.analyze(json.loads(body.decode("utf-8")), source_header=None,
                           raw_body=body, received_at=None)


# N1: transient fault recovery
def test_n1_transient_fault_resumes_with_counter(tmp_path):
    sink = RawBatchSink(tmp_path / "raw")
    assert sink.start()
    service = BridgeService(clock=FreeClock(), allowed_symbols=["SHFE.rb2610"],
                            raw_sink=sink)
    try:
        offer_analyze(service, "ok-1")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and sink.status()["counts"]["persisted_total"] < 1:
            time.sleep(0.02)

        original = sink._write_row
        failed = {"once": False}

        def failing_once(descriptor):
            if not failed["once"]:
                failed["once"] = True
                raise OSError("transient synthetic writer error")
            return original(descriptor)

        sink._write_row = failing_once
        offer_analyze(service, "t-1")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and sink.status()["counts"]["write_failures"] < 1:
            time.sleep(0.02)
        sink._write_row = original  # fault cleared

        persisted_before = sink.status()["counts"]["persisted_total"]
        for index in range(4):
            time.sleep(0.4)
            offer_analyze(service, f"after-{index}")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and sink.status()["queue_depth"] > 0:
            time.sleep(0.05)
        counts = sink.status()["counts"]
        assert counts["persisted_total"] > persisted_before, (
            "N1: capture never resumed after a transient fault: %s" % sink.status())
        assert counts["resumes"] >= 1
        assert sink.status()["state"] == "running"
    finally:
        service.close()


def test_n1_disk_full_stays_suspended_honestly(tmp_path):
    free = {"value": 2 * 1024 * 1024 * 1024}
    sink = RawBatchSink(tmp_path / "raw", disk_free=lambda path: free["value"])
    assert sink.start()
    try:
        free["value"] = 0
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and sink.status()["state"] != "suspended":
            time.sleep(0.05)
        assert sink.status()["state"] == "suspended"
        assert sink.offer(make_descriptor(1)) is False
        # headroom returns -> suspended state clears via the periodic check
        free["value"] = 2 * 1024 * 1024 * 1024
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and sink.status()["state"] == "suspended":
            time.sleep(0.05)
        assert sink.status()["state"] == "running"
    finally:
        sink.close()


# N2: durable subset honesty on mixed fsync outcomes
def test_n2_mixed_fsync_keeps_durable_at_fsynced_prefix(tmp_path):
    fail_flag = {"on": True}

    def flaky_fsync(fd):
        if fail_flag["on"]:
            raise OSError(5, "synthetic fsync failure")

    sink = RawBatchSink(tmp_path / "raw", fsync=flaky_fsync, max_segment_bytes=700)
    assert sink.start()
    try:
        for seq in range(1, 7):
            sink.offer(make_descriptor(seq))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and sink.status()["rotations"] < 1:
            time.sleep(0.02)
        assert sink.status()["rotations"] >= 1
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and sink.status()["queue_depth"] > 0:
            time.sleep(0.02)
        manifests = [json.loads(m.read_text(encoding="utf-8"))
                     for m in (tmp_path / "raw").glob("*.manifest")]
        assert any(m.get("durability") == "uncertain" for m in manifests)
        assert sink.status()["counts"]["durable_total"] == 0

        fail_flag["on"] = False  # storage recovers; new file fsyncs succeed
        time.sleep(2.5)
        counts = sink.status()["counts"]
        assert counts["durable_total"] < counts["persisted_total"], (
            "N2 whitewash: durable=%d == persisted=%d after an unfsynced sealed segment"
            % (counts["durable_total"], counts["persisted_total"]))
        # the durable prefix is permanently capped by the bad segment
        assert counts["durable_total"] == 0
    finally:
        fail_flag["on"] = False
        sink.close()


def test_n2_seal_fsync_field_records_durable_through(tmp_path):
    fail_flag = {"on": True}

    def flaky_fsync(fd):
        if fail_flag["on"]:
            raise OSError(5, "synthetic fsync failure")

    sink = RawBatchSink(tmp_path / "raw", fsync=flaky_fsync, max_segment_bytes=700)
    assert sink.start()
    try:
        for seq in range(1, 4):
            sink.offer(make_descriptor(seq))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and sink.status()["queue_depth"] == 0:
            break
        time.sleep(1.5)
    finally:
        fail_flag["on"] = False
        sink.close()
    manifests = [json.loads(m.read_text(encoding="utf-8"))
                 for m in (tmp_path / "raw").glob("*.manifest")]
    assert manifests
    for manifest in manifests:
        if manifest.get("durability") == "uncertain":
            assert "durable_through_append_index" in manifest


# N3: manifest truth after rename refusal
def test_n3_rename_refusal_keeps_manifest_matching_physical_file(tmp_path):
    import ak_trading.infiny_bridge.raw_sink as raw_sink_module
    sink = RawBatchSink(tmp_path / "raw", max_segment_bytes=700, max_segments=16)
    assert sink.start()
    original_replace = raw_sink_module.os.replace
    refused = {"once": False}

    def fail_once(source, destination):
        if not refused["once"]:
            refused["once"] = True
            raise PermissionError("synthetic one-shot rename refusal")
        return original_replace(source, destination)

    raw_sink_module.os.replace = fail_once
    try:
        for seq in range(1, 10):
            sink.offer(make_descriptor(seq))
            time.sleep(0.02)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            st = sink.status()
            if st["queue_depth"] == 0 and st["counts"]["persisted_total"] >= 9:
                break
            time.sleep(0.05)
    finally:
        raw_sink_module.os.replace = original_replace
    sink.close()
    assert refused["once"]
    problems = []
    for manifest_path in sorted((tmp_path / "raw").glob("*.manifest")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        seg = tmp_path / "raw" / manifest["file"]
        if not seg.exists():
            continue
        rows = [json.loads(line) for line in seg.read_text(encoding="utf-8").splitlines() if line]
        rows = [row for row in rows if isinstance(row, dict)
                and row.get("schema") == "ak_observer_raw_batch_v1"]
        if not rows:
            continue
        actual = [row["append_index"] for row in rows]
        if manifest["row_count"] != len(rows):
            problems.append(("row_count", manifest["row_count"], len(rows)))
        if manifest["append_index_interval"] != [min(actual), max(actual)]:
            problems.append(("interval", manifest["append_index_interval"],
                             [min(actual), max(actual)]))
    assert not problems, problems


def test_n3_persistent_rename_refusal_same_segment_continues(tmp_path):
    import ak_trading.infiny_bridge.raw_sink as raw_sink_module
    sink = RawBatchSink(tmp_path / "raw", max_segment_bytes=700, max_segments=16)
    assert sink.start()
    original_replace = raw_sink_module.os.replace
    raw_sink_module.os.replace = lambda s, d: (_ for _ in ()).throw(
        PermissionError("persistent refusal"))
    try:
        for seq in range(1, 6):
            sink.offer(make_descriptor(seq))
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            st = sink.status()
            if st["queue_depth"] == 0 and st["counts"]["persisted_total"] >= 5:
                break
            time.sleep(0.05)
        assert sink.status()["counts"]["persisted_total"] >= 1  # writing continues
    finally:
        raw_sink_module.os.replace = original_replace
    sink.close()
    # after refusal clears, the eventual seal describes every physical row
    problems = []
    for manifest_path in sorted((tmp_path / "raw").glob("*.manifest")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        seg = tmp_path / "raw" / manifest["file"]
        if not seg.exists():
            continue
        rows = [json.loads(line) for line in seg.read_text(encoding="utf-8").splitlines() if line]
        rows = [row for row in rows if isinstance(row, dict)
                and row.get("schema") == "ak_observer_raw_batch_v1"]
        if not rows:
            continue
        actual = [row["append_index"] for row in rows]
        if manifest["row_count"] != len(rows):
            problems.append(("row_count", manifest["row_count"], len(rows)))
        if manifest["append_index_interval"] != [min(actual), max(actual)]:
            problems.append(("interval", manifest["append_index_interval"],
                             [min(actual), max(actual)]))
    assert not problems, problems


# m4: crash-tail manifest interval from parsed rows
def test_m4_crash_tail_manifest_interval_from_rows(tmp_path):
    directory = tmp_path / "raw"
    directory.mkdir()
    row = {"schema": "ak_observer_raw_batch_v1", "event": "receipt_batch",
           "capture_run_id": "run-prior", "append_index": 7, "tick_count": 2}
    (directory / "raw-batches.jsonl").write_text(
        json.dumps(row) + "\n" + '{"torn":', encoding="utf-8")
    sink = RawBatchSink(directory)
    assert sink.start()
    try:
        sink.offer(make_descriptor(1))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and sink.status()["counts"]["persisted_total"] < 1:
            time.sleep(0.05)
    finally:
        sink.close()
    manifests = [json.loads(m.read_text(encoding="utf-8"))
                 for m in directory.glob("*.manifest")]
    crash = [m for m in manifests if m.get("cause") == "restart_crash_tail"]
    assert crash and crash[0]["append_index_interval"] == [7, 7]
    assert crash[0]["incomplete_tail"] == "unterminated_tail"


def test_n1_rotation_window_transient_reopen_failure_recovers(tmp_path):
    """O1: the rotation-reopen window (the only mid-run _file=None moment);
    one transient open failure must not permanently disable capture."""
    import builtins
    sink = RawBatchSink(tmp_path / "raw", max_segment_bytes=700, max_segments=16)
    assert sink.start()
    try:
        sink.offer(make_descriptor(1))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and sink.status()["counts"]["persisted_total"] < 1:
            time.sleep(0.02)
        real_open = builtins.open
        armed = {"on": True}

        def flaky_open(file, mode="r", *args, **kwargs):
            if (armed["on"] and mode == "ab"
                    and str(file).endswith("raw-batches.jsonl")):
                armed["on"] = False
                raise PermissionError(5, "synthetic transient reopen refusal")
            return real_open(file, mode, *args, **kwargs)

        builtins.open = flaky_open
        try:
            for seq in range(2, 8):  # drive the rotation; its reopen fails once
                sink.offer(make_descriptor(seq))
                time.sleep(0.02)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and sink.status()["counts"]["rotation_failures"] < 1:
                time.sleep(0.02)
        finally:
            builtins.open = real_open  # the fault is gone

        persisted_before = sink.status()["counts"]["persisted_total"]
        for index in range(4):
            time.sleep(0.5)
            sink.offer(make_descriptor(100 + index))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and sink.status()["queue_depth"] > 0:
            time.sleep(0.05)
        counts = sink.status()["counts"]
        assert counts["persisted_total"] > persisted_before, (
            "O1: a single transient rotation-reopen failure permanently disabled "
            "capture: state=%s resumes=%d dropped=%s"
            % (sink.status()["state"], counts["resumes"], counts["dropped_by_reason"]))
        assert sink.status()["state"] in {"running", "closed"}
    finally:
        sink.close()
