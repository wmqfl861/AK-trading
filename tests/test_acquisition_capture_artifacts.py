"""C2 tests: capture engine, classification, raw artifacts (H06/H07/H12/H13).

All frames are SYNTHETIC (FakeTqApi); no real SDK object is ever created.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ak_trading.acquisition import artifacts as art
from ak_trading.acquisition import contracts as c
from ak_trading.acquisition import tq_adapter as tq
from ak_trading.acquisition.calendar import CalendarBook
from ak_trading.acquisition.capture import CaptureEngine, CaptureRequest, classify_frame


def _nano(iso: str) -> int:
    return c.iso_instant_to_nano(iso)


@pytest.fixture()
def policy(frozen_policy_path):
    return c.Policy.from_file(frozen_policy_path).validate()


@pytest.fixture()
def book(synthetic_calendar_payload):
    return CalendarBook.from_mapping(synthetic_calendar_payload)


class TestCaptureEngine:
    def test_capture_counts_and_provisional_tail(self, policy, book):
        # 6 closed bars 14:54-14:59 + 1 live bar 15:00-15:01 beyond watermark.
        observed = _nano("2026-09-18T15:00:30+08:00")  # watermark = 14:59:30
        frame = tq.make_fake_frame(
            symbol="SHFE.rb2601", duration_seconds=60,
            start_nano=_nano("2026-09-18T14:54:00+08:00"),
            rows=7, closed_rows=6, padding_rows=2,
        )
        fake = tq.FakeTqApi(
            {("SHFE.rb2601", 60): frame},
            clock=lambda: observed,
            updates_until_ready=2,
        )
        engine = CaptureEngine(fake, policy, book, clock=lambda: observed)
        request = CaptureRequest(
            source_symbol="SHFE.rb2601", duration_seconds=60,
            task_id="t1", attempt_id="a1", product_key="SHFE.rb",
        )
        outcomes = engine.capture_group([request])
        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert outcome.error is None
        assert outcome.meta.requested_length == 8192
        assert outcome.meta.returned_length == 9  # 2 padding + 7 bars: NOT 8192
        assert outcome.meta.padding_rows == 2
        assert outcome.duplicate_keys == 0
        statuses = [r.valid_status for r in outcome.rows if r.frame_partition is c.FramePartition.VALID]
        # Bars ending at 14:55..14:59 are <= watermark 14:59:30 -> CLOSED (5);
        # the bar ending 15:00 is beyond the watermark -> PROVISIONAL; the bar
        # starting exactly at session end 15:00 has no evidenced grid ->
        # UNKNOWN (never closed, never fabricated).
        assert statuses.count(c.ValidRowStatus.CLOSED) == 5
        assert statuses.count(c.ValidRowStatus.PROVISIONAL) == 1
        assert statuses.count(c.ValidRowStatus.UNKNOWN) == 1
        assert outcome.meta.valid_rows == 7
        assert fake.request_log[0]["data_length"] == 8192
        assert fake.request_log[0]["symbol"] == "SHFE.rb2601"

    def test_snapshot_does_not_drift_with_live_frame(self, policy, book):
        observed = _nano("2026-09-18T15:30:00+08:00")
        frame = tq.make_fake_frame(
            symbol="SHFE.rb2601", duration_seconds=60,
            start_nano=_nano("2026-09-18T14:00:00+08:00"),
            rows=5, closed_rows=5,
        )
        fake = tq.FakeTqApi(
            {("SHFE.rb2601", 60): frame}, clock=lambda: observed
        )
        engine = CaptureEngine(fake, policy, book, clock=lambda: observed)
        request = CaptureRequest(
            source_symbol="SHFE.rb2601", duration_seconds=60,
            task_id="t", attempt_id="a", product_key="SHFE.rb",
        )
        outcome = engine.capture_group([request])[0]
        before = outcome.frame["close"].tolist()
        # The SDK keeps updating the live object after our deep copy (H06).
        frame.loc[frame.index[-1], "close"] = 999999.0
        assert outcome.frame["close"].tolist() == before

    def test_zero_row_response_still_evidenced(self, policy, book):
        import pandas as pd

        observed = _nano("2026-09-18T15:30:00+08:00")
        empty = pd.DataFrame(
            {
                name: [float("nan")] * 3
                for name in ("datetime", *c.BAR_VALUE_FIELDS)
            }
        )
        fake = tq.FakeTqApi({("SHFE.rb2601", 60): empty}, clock=lambda: observed)
        engine = CaptureEngine(fake, policy, book, clock=lambda: observed)
        request = CaptureRequest(
            source_symbol="SHFE.rb2601", duration_seconds=60,
            task_id="t", attempt_id="a", product_key="SHFE.rb",
        )
        outcome = engine.capture_group([request])[0]
        assert outcome.error is None
        assert outcome.meta.returned_length == 3
        assert outcome.meta.padding_rows == 3
        assert outcome.meta.valid_rows == 0
        assert outcome.meta.first_valid_nano is None

    def test_symbol_failure_isolated_from_group(self, policy, book):
        observed = _nano("2026-09-18T15:30:00+08:00")
        good = tq.make_fake_frame(
            symbol="SHFE.rb2601", duration_seconds=60,
            start_nano=_nano("2026-09-18T14:00:00+08:00"), rows=3, closed_rows=3,
        )
        fake = tq.FakeTqApi(
            {("SHFE.rb2601", 60): good},
            clock=lambda: observed,
            failures={("SHFE.rb9999", 60): tq.SymbolUnsupportedError("SHFE.rb9999")},
        )
        engine = CaptureEngine(fake, policy, book, clock=lambda: observed)
        requests = [
            CaptureRequest("SHFE.rb2601", 60, "t1", "a1", "SHFE.rb"),
            CaptureRequest("SHFE.rb9999", 60, "t2", "a2", "SHFE.rb"),
        ]
        outcomes = engine.capture_group(requests)
        assert outcomes[0].error is None
        assert isinstance(outcomes[1].error, tq.SymbolUnsupportedError)

    def test_group_caps_enforced(self, policy, book):
        engine = CaptureEngine(
            tq.FakeTqApi({}, clock=lambda: 1), policy, book, clock=lambda: 1
        )
        five_symbols = [
            CaptureRequest(f"S{i}", 60, f"t{i}", f"a{i}", "P") for i in range(5)
        ]
        with pytest.raises(tq.TqAdapterError, match="symbol cap"):
            engine.capture_group(five_symbols)
        nine_serials = [
            CaptureRequest("S", 60, f"t{i}", f"a{i}", "P") for i in range(9)
        ]
        with pytest.raises(tq.TqAdapterError, match="serial cap"):
            engine.capture_group(nine_serials)


class TestClassifyFrame:
    def test_partition_conservation_duplicates_invalids(self, book):
        import pandas as pd

        start = _nano("2026-09-18T14:00:00+08:00")
        frame = tq.make_fake_frame(
            symbol="SHFE.rb2601", duration_seconds=60, start_nano=start,
            rows=6, closed_rows=6, padding_rows=2, invalid_rows=1,
        )
        # Add a duplicate key: same datetime as the second closed bar (valid).
        frame.loc[len(frame)] = {
            "datetime": float(start + 60 * 10**9),
            "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
            "volume": 10.0, "open_oi": 1.0, "close_oi": 1.0,
        }
        rows, duplicates, unique = classify_frame(
            frame, source_symbol="SHFE.rb2601", duration_seconds=60,
            product_key="SHFE.rb",
            calendar_book=book,
            watermark_nano=_nano("2026-09-18T15:30:00+08:00"),
            evidence_version="v1",
        )
        partitions = [r.frame_partition for r in rows]
        assert partitions.count(c.FramePartition.PADDING) == 2
        assert partitions.count(c.FramePartition.INVALID) == 1
        assert partitions.count(c.FramePartition.VALID) == 6
        assert len(rows) == 9  # frame conservation: duplicates never dropped
        assert duplicates == 1
        assert unique == 5

    def test_non_integral_float_nano_is_invalid(self, book):
        import pandas as pd

        # Sub-nanosecond floats only exist at small magnitudes (float64 ulp
        # at 2026 epochs is 256 ns, so current-epoch datetimes are always
        # integral floats); probe the guard at a small value where 0.5 ns is
        # representable.
        small = 1_000_000_000
        frame = tq.make_fake_frame(
            symbol="SHFE.rb2601", duration_seconds=60, start_nano=small,
            rows=2, closed_rows=2,
        )
        frame.loc[0, "datetime"] = small + 0.5  # non-integral nanosecond float
        rows, _, _ = classify_frame(
            frame, source_symbol="SHFE.rb2601", duration_seconds=60,
            product_key="SHFE.rb", calendar_book=book,
            watermark_nano=_nano("2026-09-18T15:30:00+08:00"),
            evidence_version="v1",
        )
        assert rows[0].frame_partition is c.FramePartition.INVALID
        assert rows[0].reason == "datetime_not_integral_nano"
        # The valid row stays integer nanoseconds.
        valid = [r for r in rows if r.frame_partition is c.FramePartition.VALID]
        assert all(isinstance(r.datetime_nano, int) for r in valid)

    def test_unknown_calendar_rows_keep_null_trading_day(self, book):
        import pandas as pd

        start = _nano("2026-09-18T10:00:00+08:00")
        frame = tq.make_fake_frame(
            symbol="CZCE.SF601", duration_seconds=60, start_nano=start,
            rows=2, closed_rows=2,
        )
        rows, _, _ = classify_frame(
            frame, source_symbol="CZCE.SF601", duration_seconds=60,
            product_key="CZCE.SF", calendar_book=book,
            watermark_nano=_nano("2026-09-18T15:30:00+08:00"),
            evidence_version="v1",
        )
        assert all(r.trading_day is None for r in rows)
        assert all(r.valid_status is c.ValidRowStatus.UNKNOWN for r in rows)
        assert all(r.reason == "CALENDAR_UNKNOWN" for r in rows)

    def test_daily_bars_use_last_session_close(self, book):
        frame = tq.make_fake_frame(
            symbol="SHFE.rb2601", duration_seconds=86400,
            start_nano=_nano("2026-09-18T00:00:00+08:00"),
            rows=1, closed_rows=1,
        )
        rows, _, _ = classify_frame(
            frame, source_symbol="SHFE.rb2601", duration_seconds=86400,
            product_key="SHFE.rb", calendar_book=book,
            watermark_nano=_nano("2026-09-18T15:01:00+08:00"),
            evidence_version="v1",
        )
        assert rows[0].valid_status is c.ValidRowStatus.CLOSED
        # Before final session close: PROVISIONAL regardless of watermark.
        rows2, _, _ = classify_frame(
            frame, source_symbol="SHFE.rb2601", duration_seconds=86400,
            product_key="SHFE.rb", calendar_book=book,
            watermark_nano=_nano("2026-09-18T14:00:00+08:00"),
            evidence_version="v1",
        )
        assert rows2[0].valid_status is c.ValidRowStatus.PROVISIONAL


class TestArtifacts:
    def test_atomic_publish_refuses_overwrite(self, tmp_path):
        payload = b'{"a":1}'
        path, digest = art.atomic_publish_bytes(tmp_path, "x.json", payload)
        assert path.exists()
        assert digest == c.sha256_bytes(payload)
        with pytest.raises(art.ArtifactError, match="refusing to overwrite"):
            art.atomic_publish_bytes(tmp_path, "x.json", b"{}")
        # No temp litter left behind.
        assert not list(tmp_path.glob(".tmp-*"))

    def test_verify_published_file_detects_tamper(self, tmp_path):
        payload = b"data"
        path, digest = art.atomic_publish_bytes(tmp_path, "y.bin", payload)
        assert art.verify_published_file(path, digest) == 4
        path.write_bytes(b"tampered")
        with pytest.raises(art.ArtifactError, match="mismatch"):
            art.verify_published_file(path, digest)

    def test_path_traversal_and_absolute_rejected(self, tmp_path):
        with pytest.raises(art.ArtifactError, match="absolute"):
            art.resolve_within_root(tmp_path, str(tmp_path / "elsewhere"))
        with pytest.raises(art.ArtifactError, match="traversal"):
            art.resolve_within_root(tmp_path, "../outside")

    def test_junction_escape_rejected(self, tmp_path, monkeypatch):
        import os as os_mod

        target = tmp_path / "outside"
        target.mkdir()
        monkeypatch.setattr(
            os_mod.path, "isjunction", lambda p: str(p).endswith("link"), raising=False
        )
        with pytest.raises(art.ArtifactError, match="junction"):
            art.resolve_within_root(tmp_path, "link/inner")

    def test_snapshot_serialization_preserves_nan_and_counts(self, tmp_path, book):
        observed = _nano("2026-09-18T15:00:30+08:00")
        frame = tq.make_fake_frame(
            symbol="SHFE.rb2601", duration_seconds=60,
            start_nano=_nano("2026-09-18T14:54:00+08:00"),
            rows=3, closed_rows=3, padding_rows=2,
        )
        rows, _, _ = classify_frame(
            frame, source_symbol="SHFE.rb2601", duration_seconds=60,
            product_key="SHFE.rb", calendar_book=book,
            watermark_nano=observed - 60 * 10**9, evidence_version="v1",
        )
        from ak_trading.acquisition.capture import _build_meta

        meta = _build_meta(
            request=CaptureRequest("SHFE.rb2601", 60, "t", "a", "SHFE.rb"),
            policy=c.Policy.from_file(
                Path(__file__).resolve().parents[1]
                / "config/execution-policy.frozen.json"
            ).validate(),
            snapshot=frame, rows=rows, observed_at_nano=observed,
            sdk_version="SYNTHETIC-FAKE",
        )
        payload = art.serialize_snapshot(
            meta, frame, synthetic=True, counts={"closed": 3, "valid": 3}
        )
        record = json.loads(payload.decode("utf-8"))
        assert record["meta"]["raw_kind"] == "RAW_SDK_SNAPSHOT"
        assert record["meta"]["raw_kind_is_wire_capture"] is False
        assert record["meta"]["synthetic"] is True
        assert record["meta"]["requested_length"] == 8192
        assert record["meta"]["returned_length"] == 5
        assert record["columns"]["datetime"][:2] == [None, None]
        assert record["meta"]["nan_value_columns"]["datetime"] == 2

    def test_publish_raw_snapshot_verifies_hash(self, tmp_path, book):
        observed = _nano("2026-09-18T15:00:30+08:00")
        frame = tq.make_fake_frame(
            symbol="SHFE.rb2601", duration_seconds=60,
            start_nano=_nano("2026-09-18T14:54:00+08:00"),
            rows=2, closed_rows=2,
        )
        rows, _, _ = classify_frame(
            frame, source_symbol="SHFE.rb2601", duration_seconds=60,
            product_key="SHFE.rb", calendar_book=book,
            watermark_nano=observed - 60 * 10**9, evidence_version="v1",
        )
        from ak_trading.acquisition.capture import _build_meta

        meta = _build_meta(
            request=CaptureRequest("SHFE.rb2601", 60, "t", "a", "SHFE.rb"),
            policy=c.Policy.from_file(
                Path(__file__).resolve().parents[1]
                / "config/execution-policy.frozen.json"
            ).validate(),
            snapshot=frame, rows=rows, observed_at_nano=observed,
            sdk_version="SYNTHETIC-FAKE",
        )
        updated, status = art.publish_raw_snapshot(
            tmp_path, "SHFE.rb", meta, frame, synthetic=True,
            counts={"valid": 2},
        )
        assert status == "RAW_VERIFIED"
        assert updated.raw_bytes > 0
        assert updated.raw_sha256 == c.sha256_bytes(Path(updated.raw_path).read_bytes())
        entry = art.raw_manifest_entry(updated, status)
        assert entry["rows"] == 2 and entry["status"] == "RAW_VERIFIED"
