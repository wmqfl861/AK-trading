"""C1 calendar: targets A/B, session evidence consumption, closure rules (H04/H07).

Design constraints from decision.md H04:

- Nothing is generated from "workday + fixed night session" heuristics. A
  trading day's sessions exist only when explicit :class:`SessionEvidence`
  (with provenance hash) covers them; otherwise the answer is *unknown*
  (``trading_day=None`` + reason), never a guess and never a zero.
- Target A = trading day 2026-09-18 full applicable sessions (may include the
  prior civil date's night session). Target B = the civil interval
  [2026-09-18T00:00, 2026-09-20T00:00) in Shanghai time. The same authority
  row is stored once and referenced by both targets (overlap counting).
- A Saturday civil date never implies a trading day, and Friday night
  sessions crossing midnight into Saturday are legal sessions belonging to the
  *next* trading day when evidence says so.
- Daily bars close only after the **last proven session end** of their trading
  day plus the 60 s conservative buffer. ``start + 86400 s`` is never used
  (there is deliberately no code path computing it).
- No SCENARIO calendar is ever produced: evidence gaps stay gaps.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping, Optional, Sequence

from .contracts import (
    CLOSE_BUFFER_SECONDS,
    ContractError,
    NANOSECOND,
    TargetKind,
    ValidRowStatus,
    civil_date_to_nano,
    iso_instant_to_nano,
    validate_nano_int,
)

CALENDAR_EVIDENCE_SCHEMA = "acq_w2_calendar_evidence_v1"


@dataclass(frozen=True)
class SessionEvidence:
    """One trading day's proven session segments for a product."""

    product_key: str
    trading_day: str  # YYYY-MM-DD
    segments: tuple[tuple[int, int], ...]  # (start_nano, end_nano) inclusive bounds
    provenance: str  # e.g. venue announcement file sha256 + observed_at
    observed_at_nano: int

    def __post_init__(self) -> None:
        if not self.product_key or not self.trading_day:
            raise ContractError("session evidence requires product and trading day")
        try:
            date.fromisoformat(self.trading_day)
        except ValueError as exc:
            raise ContractError(f"invalid trading day {self.trading_day}") from exc
        if not self.segments:
            raise ContractError("session evidence carries at least one segment")
        previous_end = -1
        for start_nano, end_nano in self.segments:
            validate_nano_int(start_nano)
            validate_nano_int(end_nano)
            if end_nano <= start_nano:
                raise ContractError("session segment end must exceed start")
            if start_nano <= previous_end:
                raise ContractError("session segments must be ordered, non-overlapping")
            previous_end = end_nano
        validate_nano_int(self.observed_at_nano)

    def contains(self, instant_nano: int) -> bool:
        return any(start <= instant_nano < end for start, end in self.segments)

    def last_end_nano(self) -> int:
        return self.segments[-1][1]

    def first_start_nano(self) -> int:
        return self.segments[0][0]


class CalendarBook:
    """Consumes hashed session evidence; invents nothing."""

    def __init__(self, evidences: Iterable[SessionEvidence]) -> None:
        self._by_product_day: dict[tuple[str, str], SessionEvidence] = {}
        self._segments_index: list[tuple[str, str, int, int]] = []
        for evidence in evidences:
            key = (evidence.product_key, evidence.trading_day)
            if key in self._by_product_day:
                raise ContractError(f"duplicate session evidence for {key}")
            self._by_product_day[key] = evidence
            for start_nano, end_nano in evidence.segments:
                self._segments_index.append(
                    (evidence.product_key, evidence.trading_day, start_nano, end_nano)
                )
        self._segments_index.sort(key=lambda item: (item[0], item[2], item[3]))

    # -- construction -------------------------------------------------------
    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "CalendarBook":
        if payload.get("schema_version") != CALENDAR_EVIDENCE_SCHEMA:
            raise ContractError(
                f"calendar evidence schema must be {CALENDAR_EVIDENCE_SCHEMA}"
            )
        evidences = [
            SessionEvidence(
                product_key=str(item["product_key"]),
                trading_day=str(item["trading_day"]),
                segments=tuple(
                    (int(start), int(end)) for start, end in item["segments"]
                ),
                provenance=str(item["provenance"]),
                observed_at_nano=int(item["observed_at_nano"]),
            )
            for item in payload.get("sessions", [])
        ]
        return cls(evidences)

    # -- queries ------------------------------------------------------------
    def evidence_hash(self) -> str:
        from .contracts import canonical_sha256

        payload = sorted(
            (
                {
                    "observed_at_nano": str(e.observed_at_nano),
                    "product_key": e.product_key,
                    "provenance": e.provenance,
                    "segments": [
                        [str(start), str(end)] for start, end in e.segments
                    ],
                    "trading_day": e.trading_day,
                }
                for e in self._by_product_day.values()
            ),
            key=lambda item: (item["product_key"], item["trading_day"]),
        )
        return canonical_sha256(payload) if payload else "NONE_UNKNOWN"

    def trading_day_for_instant(
        self, product_key: str, instant_nano: int
    ) -> Optional[str]:
        """Trading day owning an instant, per evidence only."""
        for prod, day, start, end in self._segments_index:
            if prod == product_key and start <= instant_nano < end:
                return day
        return None

    def evidence_for_day(self, product_key: str, trading_day: str) -> Optional[SessionEvidence]:
        return self._by_product_day.get((product_key, trading_day))

    def trading_day_for_daily_bar(
        self, product_key: str, bar_start_nano: int
    ) -> Optional[str]:
        """Trading day owning a daily bar (H04).

        A daily bar stamped inside [first session start, last session end) of
        exactly one evidenced trading day belongs to that day. Night-session
        products have daily bars stamped after midnight while their trading
        day began the prior evening. Ambiguous or unmatched stamps return
        None (unknown), never a guess.
        """
        matches = [
            day
            for product, day in self._by_product_day
            if product == product_key
            and self._by_product_day[(product, day)].first_start_nano()
            <= bar_start_nano
            < self._by_product_day[(product, day)].last_end_nano()
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def day_close_nano(self, product_key: str, trading_day: str) -> Optional[int]:
        """Last proven session end + conservative 60 s buffer (H07).

        Deliberately NOT ``day_start + 86400``: the close of a trading day is
        its final legal session's end, which for night-session products may
        sit on the following civil date.
        """
        evidence = self._by_product_day.get((product_key, trading_day))
        if evidence is None:
            return None
        return evidence.last_end_nano() + CLOSE_BUFFER_SECONDS * NANOSECOND


@dataclass(frozen=True)
class TargetSpec:
    """Frozen target definitions from the policy (H04)."""

    kind: TargetKind
    trading_day: Optional[str]  # target A
    civil_start_nano: Optional[int]  # target B bounds
    civil_end_nano: Optional[int]

    @classmethod
    def target_a(cls, trading_day: str) -> "TargetSpec":
        return cls(
            kind=TargetKind.A, trading_day=trading_day, civil_start_nano=None, civil_end_nano=None
        )

    @classmethod
    def target_b(cls, start_nano: int, end_nano: int) -> "TargetSpec":
        validate_nano_int(start_nano)
        validate_nano_int(end_nano)
        if end_nano <= start_nano:
            raise ContractError("target B civil interval is empty")
        return cls(
            kind=TargetKind.B, trading_day=None, civil_start_nano=start_nano, civil_end_nano=end_nano
        )

    def references_bar(
        self,
        *,
        product_key: str,
        bar_start_nano: int,
        trading_day: Optional[str],
        book: CalendarBook,
    ) -> bool:
        """Does this bar belong to the target? Bars are counted once per key."""
        if self.kind is TargetKind.A:
            return trading_day is not None and trading_day == self.trading_day
        assert self.civil_start_nano is not None and self.civil_end_nano is not None
        return self.civil_start_nano <= bar_start_nano < self.civil_end_nano


def minute_row_status(
    *,
    book: CalendarBook,
    product_key: str,
    duration_seconds: int,
    bar_start_nano: int,
    bar_end_nano: int,
    watermark_nano: int,
) -> tuple[ValidRowStatus, Optional[str], Optional[str]]:
    """Closure classification for a minute bar (H07).

    CLOSED requires: proven session grid covering the bar, and
    ``bar_end <= watermark`` (watermark = capture time - 60 s buffer).
    PROVISIONAL: grid proven, bar end beyond the watermark.
    UNKNOWN: no session evidence covering the bar (CALENDAR_UNKNOWN).
    """
    if duration_seconds != 60:
        raise ContractError("minute_row_status only classifies 60 s bars")
    trading_day = book.trading_day_for_instant(product_key, bar_start_nano)
    if trading_day is None:
        return ValidRowStatus.UNKNOWN, None, "CALENDAR_UNKNOWN"
    day_close = book.day_close_nano(product_key, trading_day)
    if day_close is None:  # defensive; trading_day found implies evidence
        return ValidRowStatus.UNKNOWN, None, "CALENDAR_UNKNOWN"
    if bar_end_nano <= watermark_nano and bar_end_nano <= day_close:
        return ValidRowStatus.CLOSED, trading_day, None
    if bar_end_nano > watermark_nano:
        return ValidRowStatus.PROVISIONAL, trading_day, "beyond_capture_watermark"
    return ValidRowStatus.UNKNOWN, trading_day, "bar_end_after_session_close"


def daily_row_status(
    *,
    book: CalendarBook,
    product_key: str,
    trading_day: Optional[str],
    watermark_nano: int,
) -> tuple[ValidRowStatus, Optional[str]]:
    """Closure classification for a daily bar (H04/H07).

    CLOSED only when the trading day's last proven session end + buffer is at
    or before the watermark. Unknown calendar -> UNKNOWN, and the row keeps
    ``trading_day=None`` with its reason; it is never zero-filled.
    """
    if trading_day is None:
        return ValidRowStatus.UNKNOWN, None
    day_close = book.day_close_nano(product_key, trading_day)
    if day_close is None:
        return ValidRowStatus.UNKNOWN, trading_day
    if day_close <= watermark_nano:
        return ValidRowStatus.CLOSED, trading_day
    return ValidRowStatus.PROVISIONAL, trading_day


def compute_watermark_nano(observed_at_nano: int) -> int:
    """Capture watermark = actual capture time - 60 s conservative buffer."""
    validate_nano_int(observed_at_nano)
    return observed_at_nano - CLOSE_BUFFER_SECONDS * NANOSECOND


def target_b_window_from_policy(
    start_iso: str, end_iso: str
) -> tuple[int, int]:
    return iso_instant_to_nano(start_iso), iso_instant_to_nano(end_iso)


def build_targets(policy: Any, book: CalendarBook) -> tuple[TargetSpec, TargetSpec]:
    """Frozen A/B targets from the validated policy object."""
    target_a = TargetSpec.target_a(policy.target_a_trading_day)
    target_b = TargetSpec.target_b(
        policy.target_b_start_nano, policy.target_b_end_nano
    )
    return target_a, target_b


__all__ = [
    "CALENDAR_EVIDENCE_SCHEMA",
    "CalendarBook",
    "SessionEvidence",
    "TargetSpec",
    "build_targets",
    "compute_watermark_nano",
    "daily_row_status",
    "minute_row_status",
    "target_b_window_from_policy",
]
