"""C2 capture: bounded symbol groups, deep-copy snapshots, row classification.

H06/H07 essentials implemented here:

- One connection serves bounded groups of at most 4 symbols x 2 durations =
  8 serials; single-symbol string requests; ``data_length`` fixed at 8192 for
  v1. The actual returned/padding/valid/closed/provisional counts are
  recorded; 8192 is never assumed to be the returned amount.
- Readiness is driven by a bounded loop (per ``wait_update`` deadline <= 5 s,
  cumulative ready wait <= 90 s per task). On ready, the frame is deep-copied
  into an independent snapshot; ``wait_update`` alone is not treated as
  readiness proof.
- Row classification is a mutually exclusive PADDING / INVALID / VALID
  partition; VALID splits CLOSED / PROVISIONAL / UNKNOWN using the calendar
  book and the capture watermark (observed_at - 60 s). Duplicate keys are
  counted per valid partition without breaking frame conservation. Zero-row
  and all-padding responses still yield a snapshot with empty-response
  evidence.
- The SDK float ``datetime`` column is converted with a single ``int(value)``
  step after verifying the float is integral (no float round-trips).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from . import tq_adapter as tq
from .calendar import (
    CalendarBook,
    daily_row_status,
    minute_row_status,
)
from .contracts import (
    BAR_VALUE_FIELDS,
    FramePartition,
    NANOSECOND,
    Policy,
    RawSnapshotMeta,
    ValidRowStatus,
    decimal_string,
)
from .contracts import ClassifiedRow

CLASSIFICATION_RULES_VERSION = "acq-w2-row-classification-v1"


@dataclass(frozen=True)
class CaptureRequest:
    source_symbol: str
    duration_seconds: int
    task_id: str
    attempt_id: str
    product_key: str


@dataclass
class CaptureOutcome:
    request: CaptureRequest
    frame: Any  # deep-copied snapshot (pandas DataFrame or equivalent)
    meta: RawSnapshotMeta
    rows: list[ClassifiedRow]
    duplicate_keys: int
    unique_valid_keys: int
    error: Optional[tq.TqAdapterError] = None


@dataclass
class GroupStats:
    requested: int = 0
    frame: int = 0
    padding: int = 0
    invalid: int = 0
    valid: int = 0
    closed: int = 0
    provisional: int = 0
    unknown: int = 0
    unique: int = 0
    duplicate: int = 0
    conflict: int = 0


class CaptureEngine:
    """Bounded capture over an injectable adapter (fake offline / real gated)."""

    def __init__(
        self,
        adapter: Any,
        policy: Policy,
        calendar_book: CalendarBook,
        *,
        clock: Optional[Any] = None,
        evidence_version: str = CLASSIFICATION_RULES_VERSION,
    ) -> None:
        budget = policy.budget
        self._adapter = adapter
        self._policy = policy
        self._book = calendar_book
        self._evidence_version = evidence_version
        self._clock = clock or (lambda: int(time.time() * NANOSECOND))
        self._max_symbols = budget["active_symbols_max"]
        self._max_serials = budget["active_serials_max"]
        self._ready_budget = budget["serial_ready_timeout_seconds"]
        self._wait_deadline = budget["wait_update_deadline_seconds_max"]
        self._sdk_version = _adapter_sdk_version(adapter)

    # -- group capture -------------------------------------------------------
    def check_group(self, requests: Sequence[CaptureRequest]) -> None:
        symbols = {r.source_symbol for r in requests}
        if len(symbols) > self._max_symbols:
            raise tq.TqAdapterError(
                tq.FailureClass.LOCAL_ERROR,
                f"group exceeds active symbol cap ({len(symbols)} > {self._max_symbols})",
            )
        if len(requests) > self._max_serials:
            raise tq.TqAdapterError(
                tq.FailureClass.LOCAL_ERROR,
                f"group exceeds active serial cap ({len(requests)} > {self._max_serials})",
            )

    def capture_group(
        self,
        requests: Sequence[CaptureRequest],
        *,
        deadline_monotonic: Optional[float] = None,
        on_serial_attempt: Optional[Any] = None,
    ) -> list[CaptureOutcome]:
        """Capture a bounded group; returns outcomes including failures.

        Failures are returned as outcomes with ``error`` set (and an empty
        snapshot meta only when a frame was never obtained), so that zero-row
        and failed attempts are still fully evidenced.
        """
        self.check_group(requests)
        deadline = deadline_monotonic if deadline_monotonic is not None else math.inf
        serials: dict[int, Any] = {}
        observed: dict[int, int] = {}
        outcomes: dict[int, CaptureOutcome] = {}

        # Subscribe all serials first (bounded group), tracking failures.
        for index, request in enumerate(requests):
            if on_serial_attempt is not None:
                on_serial_attempt(request)
            try:
                serials[index] = self._adapter.get_kline_serial(
                    request.source_symbol,
                    request.duration_seconds,
                    data_length=self._policy.request_data_length,
                )
            except tq.TqAdapterError as exc:
                outcomes[index] = CaptureOutcome(
                    request=request,
                    frame=None,
                    meta=_empty_failure_meta(request, self._policy),
                    rows=[],
                    duplicate_keys=0,
                    unique_valid_keys=0,
                    error=exc,
                )

        # Drive readiness with bounded waits; each request has a cumulative
        # 90 s ready budget. wait_update alone never proves readiness.
        ready_spent: dict[int, float] = {index: 0.0 for index in serials}
        pending = set(serials) - set(outcomes)
        while pending:
            now = time.monotonic()
            if now >= deadline:
                for index in pending:
                    outcomes[index] = CaptureOutcome(
                        request=requests[index],
                        frame=None,
                        meta=_empty_failure_meta(requests[index], self._policy),
                        rows=[],
                        duplicate_keys=0,
                        unique_valid_keys=0,
                        error=tq.NotReadyTimeoutError(),
                    )
                break
            progressed = False
            for index in sorted(pending):
                request = requests[index]
                remaining_budget = self._ready_budget - ready_spent[index]
                if remaining_budget <= 0:
                    outcomes[index] = CaptureOutcome(
                        request=request,
                        frame=serials[index],
                        meta=_empty_failure_meta(request, self._policy),
                        rows=[],
                        duplicate_keys=0,
                        unique_valid_keys=0,
                        error=tq.NotReadyTimeoutError(),
                    )
                    continue
                wait_for = min(self._wait_deadline, remaining_budget, deadline - now)
                started = time.monotonic()
                self._adapter.wait_update(deadline=time.time() + max(wait_for, 0.001))
                ready_spent[index] += time.monotonic() - started
                if self._adapter.is_serial_ready(serials[index]):
                    # Ready: deep-copy an independent snapshot immediately.
                    observed_at = int(self._clock())
                    snapshot = _deep_copy_frame(serials[index])
                    rows, duplicates, uniques = classify_frame(
                        snapshot,
                        source_symbol=request.source_symbol,
                        duration_seconds=request.duration_seconds,
                        product_key=request.product_key,
                        calendar_book=self._book,
                        watermark_nano=_watermark(observed_at),
                        evidence_version=self._evidence_version,
                    )
                    meta = _build_meta(
                        request=request,
                        policy=self._policy,
                        snapshot=snapshot,
                        rows=rows,
                        observed_at_nano=observed_at,
                        sdk_version=self._sdk_version,
                    )
                    outcomes[index] = CaptureOutcome(
                        request=request,
                        frame=snapshot,
                        meta=meta,
                        rows=rows,
                        duplicate_keys=duplicates,
                        unique_valid_keys=uniques,
                    )
                else:
                    progressed = True
            pending = set(serials) - set(outcomes)
            if not progressed and pending:
                # No serial progressed within this sweep; loop guard keeps
                # the cumulative budget the only termination condition.
                continue

        return [outcomes[index] for index in range(len(requests))]


# ---------------------------------------------------------------------------
# Frame helpers
# ---------------------------------------------------------------------------

def _deep_copy_frame(frame: Any) -> Any:
    return frame.copy(deep=True)


def _adapter_sdk_version(adapter: Any) -> str:
    label = getattr(adapter, "sdk_version", None)
    if isinstance(label, str) and label:
        return label
    if getattr(adapter, "is_synthetic", False):
        return "SYNTHETIC-FAKE"
    return "UNKNOWN_NOT_CONNECTED"


def _watermark(observed_at_nano: int) -> int:
    return observed_at_nano - 60 * NANOSECOND


def _build_meta(
    *,
    request: CaptureRequest,
    policy: Policy,
    snapshot: Any,
    rows: list[ClassifiedRow],
    observed_at_nano: int,
    sdk_version: str,
) -> RawSnapshotMeta:
    """Exact post-classification counts; conservation is enforced by the record."""
    from .contracts import CANONICAL_SERIALIZER_VERSION

    padding = sum(1 for r in rows if r.frame_partition is FramePartition.PADDING)
    invalid = sum(1 for r in rows if r.frame_partition is FramePartition.INVALID)
    valid = sum(1 for r in rows if r.frame_partition is FramePartition.VALID)
    valid_nanos = [r.datetime_nano for r in rows if r.datetime_nano is not None]
    return RawSnapshotMeta(
        task_id=request.task_id,
        attempt_id=request.attempt_id,
        source_symbol=request.source_symbol,
        duration_seconds=request.duration_seconds,
        requested_length=policy.request_data_length,
        returned_length=len(rows),
        frame_rows=len(rows),
        padding_rows=padding,
        invalid_rows=invalid,
        valid_rows=valid,
        observed_at_nano=observed_at_nano,
        sdk_version=sdk_version,
        serializer_version=CANONICAL_SERIALIZER_VERSION,
        raw_path="",
        raw_sha256="",
        raw_bytes=0,
        first_valid_nano=min(valid_nanos) if valid_nanos else None,
        last_valid_nano=max(valid_nanos) if valid_nanos else None,
    )


def _empty_failure_meta(request: CaptureRequest, policy: Policy) -> RawSnapshotMeta:
    return RawSnapshotMeta(
        task_id=request.task_id,
        attempt_id=request.attempt_id,
        source_symbol=request.source_symbol,
        duration_seconds=request.duration_seconds,
        requested_length=policy.request_data_length,
        returned_length=0,
        frame_rows=0,
        padding_rows=0,
        invalid_rows=0,
        valid_rows=0,
        observed_at_nano=0,
        sdk_version="",
        serializer_version="",
        raw_path="",
        raw_sha256="",
        raw_bytes=0,
        first_valid_nano=None,
        last_valid_nano=None,
    )


def _is_nan(value: Any) -> bool:
    return isinstance(value, float) and value != value


def classify_frame(
    frame: Any,
    *,
    source_symbol: str,
    duration_seconds: int,
    product_key: str,
    calendar_book: CalendarBook,
    watermark_nano: int,
    evidence_version: str,
) -> tuple[list[ClassifiedRow], int, int]:
    """Partition a snapshot frame into classified rows (H06/H07).

    Returns (rows, duplicate_key_count, unique_valid_key_count). Duplicate
    counting never reduces the frame conservation totals: every row keeps its
    own PADDING/INVALID/VALID partition.
    """
    if duration_seconds not in (60, 86400):
        raise ValueError("unsupported duration")
    datetimes = list(frame["datetime"])
    rows: list[ClassifiedRow] = []
    seen_keys: dict[int, int] = {}
    duplicates = 0

    for ordinal, raw_dt in enumerate(datetimes):
        if _is_nan(raw_dt):
            rows.append(
                ClassifiedRow(
                    raw_row_ordinal=ordinal,
                    datetime_nano=None,
                    source_symbol=source_symbol,
                    duration_seconds=duration_seconds,
                    bar_start_nano=None,
                    bar_end_nano=None,
                    trading_day=None,
                    trading_day_source="none",
                    frame_partition=FramePartition.PADDING,
                    valid_status=None,
                    values={},
                    content_sha256=None,
                    evidence_version=evidence_version,
                    reason="sdk_padding_row",
                )
            )
            continue

        invalid_reason = _validate_datetime(raw_dt)
        if invalid_reason:
            rows.append(
                ClassifiedRow(
                    raw_row_ordinal=ordinal,
                    datetime_nano=None,
                    source_symbol=source_symbol,
                    duration_seconds=duration_seconds,
                    bar_start_nano=None,
                    bar_end_nano=None,
                    trading_day=None,
                    trading_day_source="none",
                    frame_partition=FramePartition.INVALID,
                    valid_status=None,
                    values={},
                    content_sha256=None,
                    evidence_version=evidence_version,
                    reason=invalid_reason,
                )
            )
            continue
        datetime_nano = int(raw_dt)

        raw_values = {name: frame[name][ordinal] for name in BAR_VALUE_FIELDS}
        value_error = _validate_values(raw_values)
        if value_error:
            rows.append(
                ClassifiedRow(
                    raw_row_ordinal=ordinal,
                    datetime_nano=datetime_nano,
                    source_symbol=source_symbol,
                    duration_seconds=duration_seconds,
                    bar_start_nano=None,
                    bar_end_nano=None,
                    trading_day=None,
                    trading_day_source="none",
                    frame_partition=FramePartition.INVALID,
                    valid_status=None,
                    values={},
                    content_sha256=None,
                    evidence_version=evidence_version,
                    reason=value_error,
                )
            )
            continue

        bar_start_nano = datetime_nano
        bar_end_nano = datetime_nano + duration_seconds * NANOSECOND
        if duration_seconds == 60:
            status, trading_day, reason = minute_row_status(
                book=calendar_book,
                product_key=product_key,
                duration_seconds=60,
                bar_start_nano=bar_start_nano,
                bar_end_nano=bar_end_nano,
                watermark_nano=watermark_nano,
            )
        else:
            provisional_day = calendar_book.trading_day_for_daily_bar(
                product_key, bar_start_nano
            )
            status, trading_day = daily_row_status(
                book=calendar_book,
                product_key=product_key,
                trading_day=provisional_day,
                watermark_nano=watermark_nano,
            )
            reason = None if status != ValidRowStatus.UNKNOWN else "CALENDAR_UNKNOWN"

        if datetime_nano in seen_keys:
            duplicates += 1
        else:
            seen_keys[datetime_nano] = ordinal

        exact = {name: decimal_string(raw_values[name]) for name in BAR_VALUE_FIELDS}
        content = _content_hash(
            duration_seconds=duration_seconds,
            datetime_nano=datetime_nano,
            bar_start_nano=bar_start_nano,
            bar_end_nano=bar_end_nano,
            trading_day=trading_day,
            exact=exact,
        )
        rows.append(
            ClassifiedRow(
                raw_row_ordinal=ordinal,
                datetime_nano=datetime_nano,
                source_symbol=source_symbol,
                duration_seconds=duration_seconds,
                bar_start_nano=bar_start_nano,
                bar_end_nano=bar_end_nano,
                trading_day=trading_day,
                trading_day_source=(
                    "session_evidence" if trading_day is not None else "none"
                ),
                frame_partition=FramePartition.VALID,
                valid_status=status,
                values=exact,
                content_sha256=content,
                evidence_version=evidence_version,
                reason=reason,
            )
        )

    return rows, duplicates, len(seen_keys)


def _validate_datetime(raw: Any) -> Optional[str]:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return "invalid_datetime_type"
    if isinstance(raw, float):
        if raw != raw or math.isinf(raw):
            return "invalid_datetime_value"
        if raw != int(raw):
            return "datetime_not_integral_nano"
        if not 0 <= raw <= 9_223_372_036_854_775_807:
            return "datetime_out_of_range"
    return None


def _validate_values(values: dict[str, Any]) -> Optional[str]:
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"invalid_value_type:{name}"
        if isinstance(value, float) and (value != value or math.isinf(value)):
            return f"invalid_value:{name}"
        if value < 0:
            return f"negative_value:{name}"
    open_, high, low, close = (
        float(values[name]) for name in ("open", "high", "low", "close")
    )
    if low > high:
        return "invalid_ohlc_low_above_high"
    if not (low <= open_ <= high and low <= close <= high):
        return "invalid_ohlc_out_of_range"
    if min(open_, close) <= 0:
        return "invalid_ohlc_nonpositive_price"
    return None


def _content_hash(
    *,
    duration_seconds: int,
    datetime_nano: int,
    bar_start_nano: int,
    bar_end_nano: int,
    trading_day: Optional[str],
    exact: dict[str, str],
) -> str:
    from .contracts import canonical_bar_content

    return canonical_bar_content(
        duration_seconds=duration_seconds,
        datetime_nano=datetime_nano,
        bar_start_nano=bar_start_nano,
        bar_end_nano=bar_end_nano,
        trading_day=trading_day,
        values=exact,
    )


__all__ = [
    "CLASSIFICATION_RULES_VERSION",
    "CaptureEngine",
    "CaptureOutcome",
    "CaptureRequest",
    "GroupStats",
    "classify_frame",
]
