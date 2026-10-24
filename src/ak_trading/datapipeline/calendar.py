"""DP04 versioned trading-calendar evidence and time semantics (r1).

AUTHORITY
    docs/devplan-datapipeline-20260923-r1/work-orders/DP04-calendar.md
    (H04.1-H04.7), 03-data-quality-contract.md Q05/D06, and the frozen DP01
    interface package ``datapipeline_contracts_v1`` (contracts.py / ports.py
    pinned read-only by DP01 r1; this module implements the CalendarProvider
    port without redefining any frozen semantic).

DESIGN RULES (frozen for DP04 r1)

    H04.1  Evidence FIRST, calendar objects SECOND.  Every document passes
           :func:`validate_calendar_evidence_document` (envelope: schema,
           origin/synthetic isolation, provenance, validity domain, session
           ordering) before a read-only :class:`CalendarBook` can be built
           from it.  The legacy ``acquisition.calendar.CalendarBook.from_mapping``
           behavior (accepting any mapping without synthetic isolation, with
           an evidence hash that did not even cover the synthetic flag -
           finding F16) is NOT a production entry here.
    H04.2  Evidence paths resolve only under explicitly allowed roots, with
           per-component lexical checks BEFORE any filesystem access (no '..',
           no forbidden face markers, no credential-looking components).
           bytes -> sha256 -> parsed object -> plan reference must agree;
           missing path, hash mismatch and conflicting sources are explicit
           errors, a missing validity domain is rejected, and an empty book
           never masquerades as a successful binding.
    H04.3  60-second slot semantics and daily semantics are separate.  A bar
           is legal only when BOTH its start and its end fall inside one
           evidenced session segment and the start is minute-grid aligned;
           a bar whose start lies in a segment but whose tail crosses a
           session boundary (lunch break, close, night-session end) is
           CROSSES_SESSION_BOUNDARY, never CLOSED.
    H04.4  Night sessions keep the exchange trading-day LABEL from the
           evidence (Friday 21:00 -> next trading day, crossing midnight and
           the weekend; pre-holiday cancelled night sessions simply have no
           evening segment).  Nothing is derived from natural days, weekday
           arithmetic or a fixed 20:00/21:00 rule; products differ, listing
           and delisting windows are explicit evidence records.
    H04.5  Missing historical evidence answers UNKNOWN_CALENDAR with the
           unknown ranges and reasons.  Evidence added later creates a NEW
           classified chain version; queries under an older as_of keep the
           old UNKNOWN answers (H04.6 gating), and no SYNTHETIC fixture or
           current quote-session shape may ever be projected backwards.
    H04.6  ``as_of`` controls historical knowability.  A document can inform
           a query only when it was possessed by ``as_of`` (obtained_at
           evidence).  When the possession/PIT basis is itself unknown, the
           document informs nothing and the query returns UNKNOWN with the
           PIT reason.
    H04.7  Expected bar grids bind DP01 ``BarGenerationEvidence``.  With no
           evidenced FIXED_SLOT rule the expectation is EXPECTATION_UNKNOWN -
           the module never fabricates zero-volume bars or minute grids.

RUNTIME SURFACE (02 blueprint, runtime output whitelist): DP04 calendar is a
    pure evidence consumer - this module performs NO file writes, opens no
    socket, no database, no .env.  Reading evidence files happens only through
    :func:`load_calendar_evidence_document` under an injected allowed root.

ERROR CODES: DP04-local codes live in :data:`DP04_ERROR_CODES` and are raised
    as ``ContractError`` messages prefixed ``E_CALENDAR_*``; they are a DP04
    namespace ADDITION, not an edit of the frozen DP01 ERROR_CODE_REGISTRY
    (codes reused from DP01 keep their exact frozen spelling, e.g.
    E_ENVELOPE_HASH_MISMATCH).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

from .contracts import (
    NANOSECOND,
    SHANGHAI_TZ,
    BarGenerationEvidence,
    ContractError,
    DataOrigin,
    EmissionPolicy,
    GenerationEvidenceStatus,
    IntervalNano,
    canonical_sha256,
    iso_instant_to_nano,
    validate_duration_seconds,
    validate_nano_int,
    assert_daily_close_evidenced,
)
from .ports import CalendarAnswer, CalendarAnswerStatus, CalendarProvider

__all__ = [
    "CALENDAR_EVIDENCE_SCHEMA_VERSION",
    "DP04_ERROR_CODES",
    "MINUTE_SLOT_NANO",
    "EvidenceKind",
    "SessionKind",
    "CoverageDeclaration",
    "PitBasis",
    "RangeStatus",
    "BarCalendarVerdict",
    "ExpectationStatus",
    "SegmentRecord",
    "SessionDayRecord",
    "ClosedDateRecord",
    "InstrumentWindowRecord",
    "CalendarEvidenceDocument",
    "CalendarRangeVerdict",
    "CalendarQueryResult",
    "ExpectedSlotGrid",
    "CalendarBook",
    "validate_calendar_evidence_document",
    "calendar_evidence_document_from_mapping",
    "resolve_evidence_path",
    "load_calendar_evidence_document",
    "bind_calendar_evidence",
    "exchange_closed_dates_document_from_catalog",
    "parse_source_symbol_domains",
    "civil_date_bounds_nano",
    "is_minute_grid_aligned",
    "expected_minute_slots_in_segment",
    "trading_day_for_instant",
    "daily_session_span",
    "classify_bar_calendar",
    "build_expected_slot_grid",
    "build_expected_daily_bars",
]

#: Envelope schema identity (mirrored by config/datapipeline/
#: calendar-evidence.schema.json; tests cross-check the two for drift).
CALENDAR_EVIDENCE_SCHEMA_VERSION = "datapipeline-calendar-evidence-v1"

#: One 60-second slot in nanoseconds (epoch-aligned minute grid).
MINUTE_SLOT_NANO = 60 * NANOSECOND

#: DP04-local error codes (namespace addition; DP01 registry untouched).
DP04_ERROR_CODES: Tuple[str, ...] = (
    "E_CALENDAR_EVIDENCE_SCHEMA",          # H04.1 wrong/absent schema identity
    "E_CALENDAR_EVIDENCE_ORIGIN_MARKER",   # H04.1/F16 SYNTHETIC/SCENARIO isolation
    "E_CALENDAR_EVIDENCE_PROVENANCE",      # H04.1 REAL_SOURCE proof obligations
    "E_CALENDAR_EVIDENCE_VALIDITY",        # H04.2 missing/broken validity domain
    "E_CALENDAR_EVIDENCE_SESSION_ORDER",   # H04.1 unordered/overlapping sessions
    "E_CALENDAR_EVIDENCE_SESSION_DOMAIN",  # H04.1 session outside validity domain
    "E_CALENDAR_EVIDENCE_EMPTY",           # H04.2 zero-content masquerade guard
    "E_CALENDAR_EVIDENCE_APPLICABILITY",   # H04.1 empty applicability domain
    "E_CALENDAR_EVIDENCE_ENUM",            # unknown enum value in envelope
    "E_CALENDAR_EVIDENCE_PATH",            # H04.2 path outside allowed roots
    "E_CALENDAR_EVIDENCE_PATH_MISSING",    # H04.2 absent evidence file
    "E_CALENDAR_BOOK_DUPLICATE_CONFLICT",  # same id, different content
    "E_CALENDAR_BINDING_EMPTY",            # H04.2 hash ok, book empty
    "E_CALENDAR_BAR_LENGTH",               # H04.3 bar interval length mismatch
    "E_CALENDAR_GENERATION_NOT_APPLICABLE",  # H04.7 evidence does not bind task
    "E_CALENDAR_SLOT_WINDOW",              # H04.3 malformed slot/bar window
)

# DP01-frozen codes reused verbatim where the semantics are identical:
#   E_ENVELOPE_HASH_MISMATCH (bytes/document hash vs expected),
#   E_DURATION_UNSUPPORTED / E_NANO_* (via contracts validators).


def _fail_local(code: str, detail: str) -> None:
    raise ContractError(f"{code}: {detail} (DP04 calendar evidence rules)")


class EvidenceKind(str, Enum):
    """What a document can prove (H04.1/H04.3).

    EXCHANGE_CLOSED_DATES
        Exchange-level closed civil dates (the DP00 2026 anchor shape).
        Can prove NO_TRADING_EVIDENCED on listed dates and nothing else:
        it carries no session intervals, so open dates stay UNKNOWN.
    SESSION_GRID
        Per trading-day session segments with the exchange trading-day
        label retained (night sessions included).  Can prove
        VERIFIED_SESSIONS and - only under COMPLETE_IN_VALIDITY_DOMAIN
        coverage - enumerated no-trading ranges.
    """

    EXCHANGE_CLOSED_DATES = "EXCHANGE_CLOSED_DATES"
    SESSION_GRID = "SESSION_GRID"


class SessionKind(str, Enum):
    DAY = "DAY"
    NIGHT = "NIGHT"


class CoverageDeclaration(str, Enum):
    """H04.5 honesty knob for SESSION_GRID documents.

    PARTIAL_DAYS_LISTED (conservative default): only the listed trading days
    are evidenced; time not covered by a listed session is UNKNOWN, never
    "no trading".  COMPLETE_IN_VALIDITY_DOMAIN: the grid enumerates every
    session of the applicability domain inside the validity window, so
    non-session time inside the window is NO_TRADING_EVIDENCED by
    enumeration (weekends, nights before holidays, lunch gaps).
    """

    PARTIAL_DAYS_LISTED = "PARTIAL_DAYS_LISTED"
    COMPLETE_IN_VALIDITY_DOMAIN = "COMPLETE_IN_VALIDITY_DOMAIN"


class PitBasis(str, Enum):
    """H04.6: what proves the document was possessed at a given as_of.

    OBTAINED_AT - obtained_at_nano is the possession proof; the document
    informs queries with as_of >= obtained_at_nano only.  UNKNOWN - the
    possession/PIT basis is unknown (e.g. an inherited file whose retrieval
    time cannot be reconstructed); the document informs NO query and only
    contributes UNKNOWN ranges with the PIT reason.
    """

    OBTAINED_AT = "OBTAINED_AT"
    UNKNOWN = "UNKNOWN"


class RangeStatus(str, Enum):
    VERIFIED_SESSIONS = "VERIFIED_SESSIONS"
    NO_TRADING_EVIDENCED = "NO_TRADING_EVIDENCED"
    UNKNOWN = "UNKNOWN"
    CONFLICT = "CONFLICT"


class BarCalendarVerdict(str, Enum):
    """H04.3 per-bar classification (calendar leg only; row status is DP03's)."""

    IN_SESSION = "IN_SESSION"                        # legal, aligned, fully inside one segment
    CROSSES_SESSION_BOUNDARY = "CROSSES_SESSION_BOUNDARY"  # start in segment, tail crosses out (F16)
    OUTSIDE_SESSION_EVIDENCED = "OUTSIDE_SESSION_EVIDENCED"  # evidenced no-trading time
    UNALIGNED_START = "UNALIGNED_START"              # off minute grid inside a session
    CALENDAR_UNKNOWN = "CALENDAR_UNKNOWN"
    CALENDAR_CONFLICT = "CALENDAR_CONFLICT"


class ExpectationStatus(str, Enum):
    """H04.7 expected-grid outcome."""

    EXPECTED_GRID = "EXPECTED_GRID"                  # FIXED_SLOT evidenced + calendar verified
    EXPECTATION_UNKNOWN = "EXPECTATION_UNKNOWN"      # never fabricate bars


# ---------------------------------------------------------------------------
# Symbol -> domain parsing (engineering glue over the frozen universe shapes;
# derives NO calendar facts - only which evidence documents apply to a symbol)
# ---------------------------------------------------------------------------

# KQ.m@SHFE.rb / KQ.i@SHFE.rb reference symbols carry NO digits; concrete
# contracts (SHFE.rb2610, CZCE.TA601, CFFEX.IC2606) do.
_SOURCE_SYMBOL_RE = re.compile(r"^(?:(KQ\.m|KQ\.i)@)?([A-Za-z]+)\.([A-Za-z]+)([0-9]*)$")


def parse_source_symbol_domains(source_symbol: str) -> Tuple[Tuple[str, str], ...]:
    """Parse the frozen r1 symbol shapes into (exchange, product) domains.

    Recognized shapes (DP00 universe.json): ``KQ.m@SHFE.rb``, ``KQ.i@SHFE.rb``,
    ``SHFE.rb2610``, ``CZCE.TA601``, ``CFFEX.IC2606``.  Returns lower-case
    ``(exchange, product)`` tuples (``("shfe", "rb")``).  Unparseable symbols
    return an empty tuple: they match only documents that list the exact
    ``source_symbol``.  This parser NEVER decides when the market trades -
    it only routes a symbol to its applicable evidence documents.
    """
    if not isinstance(source_symbol, str) or not source_symbol:
        return ()
    match = _SOURCE_SYMBOL_RE.match(source_symbol)
    if match is None:
        return ()
    exchange = match.group(2).lower()
    product = match.group(3).lower()
    return ((exchange, product),)


def _domains_of(symbol: str) -> Tuple[Tuple[str, str], ...]:
    return parse_source_symbol_domains(symbol)


# ---------------------------------------------------------------------------
# Civil-date <-> nanosecond helpers (Asia/Shanghai; integer arithmetic only)
# ---------------------------------------------------------------------------


def civil_date_bounds_nano(day: str) -> Tuple[int, int]:
    """[00:00, 24:00) Shanghai nanosecond bounds of a civil date."""
    try:
        parsed = date.fromisoformat(day)
    except ValueError as exc:
        _fail_local("E_CALENDAR_EVIDENCE_ENUM", f"invalid civil date {day!r}: {exc}")
    start = datetime(parsed.year, parsed.month, parsed.day, tzinfo=SHANGHAI_TZ)
    day_nanos = 86400 * NANOSECOND
    start_nano = iso_instant_to_nano(start.isoformat())
    return (start_nano, start_nano + day_nanos)


def _valid_civil_date(day: str) -> str:
    try:
        date.fromisoformat(day)
    except ValueError as exc:
        _fail_local("E_CALENDAR_EVIDENCE_ENUM", f"invalid trading_day {day!r}: {exc}")
    return day


def is_minute_grid_aligned(start_nano: int) -> bool:
    validate_nano_int(start_nano)
    return start_nano % MINUTE_SLOT_NANO == 0


def expected_minute_slots_in_segment(segment: IntervalNano) -> Tuple[int, ...]:
    """Epoch-aligned 60 s slot starts fully inside one session segment.

    A slot [t, t+60s) belongs to the segment iff segment.start <= t and
    t+60s <= segment.end.  H04.3: slots crossing the segment tail (lunch
    break, close) are NOT emitted merely because their start lies inside.
    """
    step = MINUTE_SLOT_NANO
    first = ((segment.start_nano + step - 1) // step) * step
    slots: List[int] = []
    t = first
    while t + step <= segment.end_nano:
        slots.append(t)
        t += step
    return tuple(slots)


# ---------------------------------------------------------------------------
# H04.1 Evidence envelope records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SegmentRecord:
    """One session segment [start, end) tagged with the exchange label."""

    kind: SessionKind
    start_nano: int
    end_nano: int

    def __post_init__(self) -> None:
        _coerce_local(SessionKind, self.kind)
        validate_nano_int(self.start_nano)
        validate_nano_int(self.end_nano)
        if self.end_nano <= self.start_nano:
            _fail_local(
                "E_CALENDAR_EVIDENCE_SESSION_ORDER",
                f"segment end {self.end_nano} must exceed start {self.start_nano}",
            )

    def interval(self) -> IntervalNano:
        return IntervalNano(start_nano=self.start_nano, end_nano=self.end_nano)

    def canonical(self) -> dict:
        return {
            "end_nano": str(self.end_nano),
            "kind": self.kind.value,
            "start_nano": str(self.start_nano),
        }


@dataclass(frozen=True)
class SessionDayRecord:
    """All evidenced segments of ONE exchange trading day (H04.4).

    ``trading_day`` is the exchange's trading-day label retained verbatim
    from the evidence (a Friday 21:00 night session carries the NEXT trading
    day's label; the label is never recomputed from civil dates).
    """

    trading_day: str
    segments: Tuple[SegmentRecord, ...]

    def __post_init__(self) -> None:
        _valid_civil_date(self.trading_day)
        if not self.segments:
            _fail_local(
                "E_CALENDAR_EVIDENCE_SESSION_ORDER",
                f"trading day {self.trading_day} needs at least one segment",
            )
        previous_end = -1
        for segment in self.segments:
            if segment.start_nano <= previous_end:
                _fail_local(
                    "E_CALENDAR_EVIDENCE_SESSION_ORDER",
                    f"trading day {self.trading_day}: segments must be ordered and "
                    f"non-overlapping (segment start {segment.start_nano} follows end {previous_end})",
                )
            previous_end = segment.end_nano

    def first_start_nano(self) -> int:
        return self.segments[0].start_nano

    def last_end_nano(self) -> int:
        return self.segments[-1].end_nano

    def canonical(self) -> dict:
        return {
            "segments": [s.canonical() for s in self.segments],
            "trading_day": self.trading_day,
        }


@dataclass(frozen=True)
class ClosedDateRecord:
    """An exchange-level closed civil date (holiday evidence)."""

    exchange: str            # upper-case exchange code, e.g. "SHFE"
    date: str                # YYYY-MM-DD
    reason: str              # e.g. "HOLIDAY_NEW_YEAR" / free-form evidence label

    def __post_init__(self) -> None:
        if not self.exchange or not isinstance(self.exchange, str):
            _fail_local("E_CALENDAR_EVIDENCE_ENUM", "closed-date record needs an exchange")
        _valid_civil_date(self.date)
        if not self.reason or not isinstance(self.reason, str):
            _fail_local("E_CALENDAR_EVIDENCE_ENUM", "closed-date record needs a reason label")

    def canonical(self) -> dict:
        return {"date": self.date, "exchange": self.exchange, "reason": self.reason}


@dataclass(frozen=True)
class InstrumentWindowRecord:
    """Listing/delisting validity window of one exact source_symbol (H04.4)."""

    source_symbol: str
    valid_from_nano: int
    valid_through_nano: int
    event: str               # "LISTED_UNTIL_DELISTED" carries both bounds

    def __post_init__(self) -> None:
        if not self.source_symbol or not isinstance(self.source_symbol, str):
            _fail_local("E_CALENDAR_EVIDENCE_ENUM", "instrument window needs source_symbol")
        validate_nano_int(self.valid_from_nano)
        validate_nano_int(self.valid_through_nano)
        if self.valid_through_nano <= self.valid_from_nano:
            _fail_local("E_CALENDAR_EVIDENCE_VALIDITY", "instrument window is [from, through)")
        if self.event not in ("LISTED_UNTIL_DELISTED",):
            _fail_local("E_CALENDAR_EVIDENCE_ENUM", f"unknown instrument window event {self.event!r}")

    def canonical(self) -> dict:
        return {
            "event": self.event,
            "source_symbol": self.source_symbol,
            "valid_from_nano": str(self.valid_from_nano),
            "valid_through_nano": str(self.valid_through_nano),
        }


def _coerce_local(enum_cls: type, value: Any) -> Any:
    try:
        return enum_cls(value)
    except ValueError:
        _fail_local(
            "E_CALENDAR_EVIDENCE_ENUM",
            f"{value!r} is not a {enum_cls.__name__} value",
        )


@dataclass(frozen=True)
class CalendarEvidenceDocument:
    """One immutable, versioned calendar-evidence envelope (H04.1).

    ``document_sha256`` covers the CANONICAL content INCLUDING data_origin
    and synthetic_marker (finding F16: the legacy evidence hash did not even
    cover the synthetic flag).  Two documents differing only in origin or
    marker hash differently.
    """

    evidence_version: str
    dataset: str
    source_id: str
    evidence_kind: EvidenceKind
    data_origin: DataOrigin
    applicability_exchanges: Tuple[str, ...]      # upper-case codes
    applicability_products: Tuple[str, ...]       # lower-case "exchange.product"
    applicability_symbols: Tuple[str, ...]        # exact source_symbol matches
    valid_from_nano: int
    valid_through_nano: int
    pit_basis: PitBasis
    obtained_at_nano: Optional[int]               # possession proof (None iff pit_basis UNKNOWN)
    announced_at_nano: Optional[int]              # source publication time, when evidenced
    evidence_reference: Optional[str]             # where the evidence came from
    evidence_text_sha256: Optional[str]           # hash of the evidence text (REAL_SOURCE)
    review_status: str                            # e.g. "review_required" (carried, surfaced)
    synthetic_marker: Optional[str] = None        # REQUIRED for SYNTHETIC/SCENARIO
    coverage: CoverageDeclaration = CoverageDeclaration.PARTIAL_DAYS_LISTED
    sessions: Tuple[SessionDayRecord, ...] = ()   # SESSION_GRID only
    closed_dates: Tuple[ClosedDateRecord, ...] = ()  # EXCHANGE_CLOSED_DATES (or grid docs)
    instrument_windows: Tuple[InstrumentWindowRecord, ...] = ()
    notes: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (self.evidence_version, self.dataset, self.source_id):
            if not isinstance(name, str) or not name:
                _fail_local("E_CALENDAR_EVIDENCE_SCHEMA", f"envelope field {name!r} required")
        _coerce_local(EvidenceKind, self.evidence_kind)
        _coerce_local(DataOrigin, self.data_origin)
        _coerce_local(PitBasis, self.pit_basis)
        _coerce_local(CoverageDeclaration, self.coverage)
        if not (
            self.applicability_exchanges
            or self.applicability_products
            or self.applicability_symbols
        ):
            _fail_local(
                "E_CALENDAR_EVIDENCE_APPLICABILITY",
                "document must state exchange/product/symbol applicability domain",
            )
        validate_nano_int(self.valid_from_nano)
        validate_nano_int(self.valid_through_nano)
        if self.valid_through_nano <= self.valid_from_nano:
            _fail_local(
                "E_CALENDAR_EVIDENCE_VALIDITY",
                "validity domain is [valid_from, valid_through)",
            )
        # H04.1/F16: synthetic isolation at the ENVELOPE, not at consumption.
        if self.data_origin in (DataOrigin.SYNTHETIC, DataOrigin.SCENARIO):
            if not self.synthetic_marker or "SYNTHETIC" not in self.synthetic_marker.upper():
                _fail_local(
                    "E_CALENDAR_EVIDENCE_ORIGIN_MARKER",
                    f"{self.data_origin.value} calendar evidence carries a prominent "
                    "SYNTHETIC marker; unmarked synthetic evidence cannot enter a book",
                )
        else:
            # REAL_SOURCE / LEGACY_UNVERIFIED carry no synthetic marker.
            if self.synthetic_marker is not None:
                _fail_local(
                    "E_CALENDAR_EVIDENCE_ORIGIN_MARKER",
                    "synthetic_marker is reserved for SYNTHETIC/SCENARIO documents",
                )
        if self.data_origin is DataOrigin.REAL_SOURCE:
            if not self.evidence_reference or not self.evidence_text_sha256:
                _fail_local(
                    "E_CALENDAR_EVIDENCE_PROVENANCE",
                    "REAL_SOURCE calendar evidence requires the reference and the "
                    "evidence-text sha256",
                )
        if self.pit_basis is PitBasis.OBTAINED_AT:
            if self.obtained_at_nano is None:
                _fail_local(
                    "E_CALENDAR_EVIDENCE_PROVENANCE",
                    "pit_basis=OBTAINED_AT requires obtained_at_nano",
                )
            validate_nano_int(self.obtained_at_nano)
        else:
            if self.obtained_at_nano is not None:
                _fail_local(
                    "E_CALENDAR_EVIDENCE_PROVENANCE",
                    "pit_basis=UNKNOWN documents carry NO obtained_at (the possession "
                    "time is exactly what is unknown)",
                )
        if self.announced_at_nano is not None:
            validate_nano_int(self.announced_at_nano)
        # Content shape per kind.
        if self.evidence_kind is EvidenceKind.SESSION_GRID:
            if not self.sessions:
                _fail_local(
                    "E_CALENDAR_EVIDENCE_EMPTY",
                    "SESSION_GRID documents carry at least one trading day",
                )
        else:
            if self.sessions:
                _fail_local(
                    "E_CALENDAR_EVIDENCE_SCHEMA",
                    "EXCHANGE_CLOSED_DATES documents carry no session grid",
                )
            if not self.closed_dates:
                _fail_local(
                    "E_CALENDAR_EVIDENCE_EMPTY",
                    "EXCHANGE_CLOSED_DATES documents carry at least one closed date",
                )
        if not self.sessions and not self.closed_dates and not self.instrument_windows:
            _fail_local(
                "E_CALENDAR_EVIDENCE_EMPTY",
                "zero-content evidence document (the empty-book masquerade guard)",
            )
        # Sessions must lie inside the validity domain; days unique; segments
        # consistent with the declared coverage kind.
        seen_days = set()
        for day_record in self.sessions:
            if day_record.trading_day in seen_days:
                _fail_local(
                    "E_CALENDAR_EVIDENCE_SESSION_ORDER",
                    f"duplicate trading day {day_record.trading_day} in one document",
                )
            seen_days.add(day_record.trading_day)
            if day_record.first_start_nano() < self.valid_from_nano:
                _fail_local(
                    "E_CALENDAR_EVIDENCE_SESSION_DOMAIN",
                    f"trading day {day_record.trading_day} starts before valid_from",
                )
            if day_record.last_end_nano() > self.valid_through_nano:
                _fail_local(
                    "E_CALENDAR_EVIDENCE_SESSION_DOMAIN",
                    f"trading day {day_record.trading_day} ends after valid_through",
                )
        for closed in self.closed_dates:
            start, end = civil_date_bounds_nano(closed.date)
            if end <= self.valid_from_nano or start >= self.valid_through_nano:
                _fail_local(
                    "E_CALENDAR_EVIDENCE_SESSION_DOMAIN",
                    f"closed date {closed.date} lies outside the validity domain",
                )

    # -- applicability -----------------------------------------------------

    def applies_to_symbol(self, source_symbol: str) -> bool:
        if source_symbol in self.applicability_symbols:
            return True
        domains = _domains_of(source_symbol)
        if not domains:
            return False
        exchange, product = domains[0]
        if exchange.upper() in [e.upper() for e in self.applicability_exchanges]:
            return True
        key = f"{exchange}.{product}"
        return key in [p.lower() for p in self.applicability_products]

    def known_by(self, as_of_nano: int) -> bool:
        """H04.6: could this document have been possessed at as_of?"""
        validate_nano_int(as_of_nano)
        if self.pit_basis is not PitBasis.OBTAINED_AT:
            return False
        return self.obtained_at_nano <= as_of_nano

    # -- hashing -----------------------------------------------------------

    def canonical(self) -> dict:
        return {
            "announced_at_nano": (str(self.announced_at_nano) if self.announced_at_nano is not None else None),
            "applicability": {
                "exchanges": sorted(e.upper() for e in self.applicability_exchanges),
                "products": sorted(p.lower() for p in self.applicability_products),
                "source_symbols": sorted(self.applicability_symbols),
            },
            "closed_dates": [c.canonical() for c in sorted(self.closed_dates, key=lambda c: (c.exchange, c.date))],
            "coverage": self.coverage.value,
            "data_origin": self.data_origin.value,
            "dataset": self.dataset,
            "evidence_kind": self.evidence_kind.value,
            "evidence_reference": self.evidence_reference,
            "evidence_text_sha256": self.evidence_text_sha256,
            "evidence_version": self.evidence_version,
            "instrument_windows": [
                w.canonical()
                for w in sorted(self.instrument_windows, key=lambda w: (w.source_symbol, w.valid_from_nano))
            ],
            "notes": sorted(self.notes),
            "obtained_at_nano": (str(self.obtained_at_nano) if self.obtained_at_nano is not None else None),
            "pit_basis": self.pit_basis.value,
            "review_status": self.review_status,
            "schema_version": CALENDAR_EVIDENCE_SCHEMA_VERSION,
            "sessions": [
                d.canonical()
                for d in sorted(self.sessions, key=lambda d: (d.trading_day, d.first_start_nano()))
            ],
            "source_id": self.source_id,
            "synthetic_marker": self.synthetic_marker,
            "valid_from_nano": str(self.valid_from_nano),
            "valid_through_nano": str(self.valid_through_nano),
        }

    def document_sha256(self) -> str:
        """Canonical digest covering origin + synthetic marker (F16 fix)."""
        return canonical_sha256(self.canonical())

    def reference_label(self) -> str:
        return f"{self.evidence_version}#{self.document_sha256()}"


def _parse_nano_field(payload: Mapping[str, Any], name: str, *, required: bool = True) -> Optional[int]:
    value = payload.get(name)
    if value is None:
        if required:
            _fail_local("E_CALENDAR_EVIDENCE_VALIDITY", f"field {name!r} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        _fail_local("E_CALENDAR_EVIDENCE_ENUM", f"field {name!r} must be an integer nano or its string form")
    try:
        return validate_nano_int(int(value))
    except (TypeError, ValueError):
        _fail_local("E_CALENDAR_EVIDENCE_ENUM", f"field {name!r} is not an integer: {value!r}")


def calendar_evidence_document_from_mapping(payload: Mapping[str, Any]) -> CalendarEvidenceDocument:
    """H04.1 envelope validation FIRST - the ONLY production constructor."""
    if not isinstance(payload, Mapping):
        _fail_local("E_CALENDAR_EVIDENCE_SCHEMA", "evidence payload must be a JSON object")
    schema = payload.get("schema_version")
    if schema != CALENDAR_EVIDENCE_SCHEMA_VERSION:
        _fail_local(
            "E_CALENDAR_EVIDENCE_SCHEMA",
            f"schema_version must be {CALENDAR_EVIDENCE_SCHEMA_VERSION!r}, got {schema!r}; "
            "new envelope semantics require a new schema version and contract revision",
        )
    applicability = payload.get("applicability") or {}
    if not isinstance(applicability, Mapping):
        _fail_local("E_CALENDAR_EVIDENCE_APPLICABILITY", "applicability must be an object")
    sessions_payload = payload.get("sessions") or []
    closed_payload = payload.get("closed_dates") or []
    windows_payload = payload.get("instrument_windows") or []
    segments_by_day: List[SessionDayRecord] = []
    for item in sessions_payload:
        day = _valid_civil_date(str(item["trading_day"]))
        segments: List[SegmentRecord] = []
        for seg in item["segments"]:
            segments.append(
                SegmentRecord(
                    kind=_coerce_local(SessionKind, seg["kind"]),
                    start_nano=_parse_nano_field(seg, "start_nano"),
                    end_nano=_parse_nano_field(seg, "end_nano"),
                )
            )
        segments_by_day.append(SessionDayRecord(trading_day=day, segments=tuple(segments)))
    closed_dates = tuple(
        ClosedDateRecord(
            exchange=str(c["exchange"]),
            date=str(c["date"]),
            reason=str(c.get("reason", "CLOSED_DATE_EVIDENCED")),
        )
        for c in closed_payload
    )
    windows = tuple(
        InstrumentWindowRecord(
            source_symbol=str(w["source_symbol"]),
            valid_from_nano=_parse_nano_field(w, "valid_from_nano"),
            valid_through_nano=_parse_nano_field(w, "valid_through_nano"),
            event=str(w.get("event", "LISTED_UNTIL_DELISTED")),
        )
        for w in windows_payload
    )
    pit_basis = _coerce_local(PitBasis, payload.get("pit_basis", PitBasis.OBTAINED_AT.value))
    return CalendarEvidenceDocument(
        evidence_version=str(payload["evidence_version"]),
        dataset=str(payload["dataset"]),
        source_id=str(payload["source_id"]),
        evidence_kind=_coerce_local(EvidenceKind, payload["evidence_kind"]),
        data_origin=_coerce_local(DataOrigin, payload["data_origin"]),
        applicability_exchanges=tuple(str(e) for e in applicability.get("exchanges") or ()),
        applicability_products=tuple(str(p) for p in applicability.get("products") or ()),
        applicability_symbols=tuple(str(s) for s in applicability.get("source_symbols") or ()),
        valid_from_nano=_parse_nano_field(payload, "valid_from_nano"),
        valid_through_nano=_parse_nano_field(payload, "valid_through_nano"),
        pit_basis=pit_basis,
        obtained_at_nano=_parse_nano_field(payload, "obtained_at_nano", required=False),
        announced_at_nano=_parse_nano_field(payload, "announced_at_nano", required=False),
        evidence_reference=(str(payload["evidence_reference"]) if payload.get("evidence_reference") else None),
        evidence_text_sha256=(str(payload["evidence_text_sha256"]) if payload.get("evidence_text_sha256") else None),
        review_status=str(payload.get("review_status", "review_required")),
        synthetic_marker=(str(payload["synthetic_marker"]) if payload.get("synthetic_marker") else None),
        coverage=_coerce_local(CoverageDeclaration, payload.get("coverage", CoverageDeclaration.PARTIAL_DAYS_LISTED.value)),
        sessions=tuple(segments_by_day),
        closed_dates=closed_dates,
        instrument_windows=windows,
        notes=tuple(str(n) for n in payload.get("notes") or ()),
    )


#: Backwards-friendly alias: validation is mandatory, there is no
#: unvalidated from_mapping path (H04.1).
validate_calendar_evidence_document = calendar_evidence_document_from_mapping


# ---------------------------------------------------------------------------
# H04.2 Path resolution under allowed roots + bytes->hash->object binding
# ---------------------------------------------------------------------------

_FORBIDDEN_MARKERS = ("maintenance_artifacts", "AKF260920")
_ENV_LIKE = (".env", ".env.local", ".env.production", "credentials", "secrets")


def _lexical_components(path_text: str) -> List[str]:
    if not isinstance(path_text, str) or not path_text.strip():
        _fail_local("E_CALENDAR_EVIDENCE_PATH", "evidence path must be a non-empty string")
    parts = [p for p in re.split(r"[\\/]+", path_text) if p != ""]
    for part in parts:
        if part in ("..", "."):
            _fail_local("E_CALENDAR_EVIDENCE_PATH", f"path component {part!r} forbidden (no traversal)")
        for marker in _FORBIDDEN_MARKERS:
            if marker.lower() in part.lower():
                _fail_local(
                    "E_CALENDAR_EVIDENCE_PATH",
                    f"path component {part!r} touches forbidden face marker {marker!r}",
                )
        if part.lower() in _ENV_LIKE:
            _fail_local("E_CALENDAR_EVIDENCE_PATH", f"path component {part!r} looks like a credential file")
    if not parts:
        _fail_local("E_CALENDAR_EVIDENCE_PATH", "evidence path has no components")
    return parts


def resolve_evidence_path(path_text: str, allowed_roots: Sequence[str]) -> str:
    """Resolve an evidence path lexically under one of the allowed roots.

    Per-component containment (DP00 path_component_rules) BEFORE any
    filesystem access: no '..' traversal, no absolute paths outside roots,
    no forbidden face markers, no credential-looking components.  Returns a
    normalized forward-slash path under the matching root.  A path that is
    already absolute is accepted only when it lies under an allowed root
    (drive letter checked per root).
    """
    if not allowed_roots:
        _fail_local("E_CALENDAR_EVIDENCE_PATH", "at least one allowed evidence root must be injected")
    normalized_roots: List[Tuple[str, List[str]]] = []
    for root in allowed_roots:
        root_parts = [p for p in re.split(r"[\\/]+", str(root)) if p != ""]
        if not root_parts:
            _fail_local("E_CALENDAR_EVIDENCE_PATH", f"allowed root {root!r} is empty")
        normalized_roots.append(("/".join(root_parts), root_parts))

    absolute = bool(re.match(r"^[A-Za-z]:", path_text) or path_text.startswith(("\\", "/")))
    if absolute:
        target_parts = _lexical_components(path_text)
        for root_norm, root_parts in normalized_roots:
            if len(target_parts) > len(root_parts) and target_parts[: len(root_parts)] == root_parts:
                return "/".join(target_parts)
        _fail_local(
            "E_CALENDAR_EVIDENCE_PATH",
            f"absolute evidence path {path_text!r} lies outside every allowed root",
        )
    # Relative path: must not escape upward and must resolve under a root.
    rel_parts = _lexical_components(path_text)
    return "/".join(normalized_roots[0][1] + rel_parts) if normalized_roots else ""  # pragma: no cover


def load_calendar_evidence_document(
    path_text: str,
    *,
    allowed_roots: Sequence[str],
    expected_sha256: Optional[str] = None,
) -> Tuple[CalendarEvidenceDocument, str]:
    """Load, hash and validate one evidence file (H04.2 binding chain).

    Chain: resolve under allowed roots -> read bytes -> sha256 -> (optional
    expected-hash check) -> parse -> ENVELOPE validation.  Returns the
    document plus the file-bytes sha256.  Missing file, hash mismatch and
    envelope violations raise explicit errors - an empty book is never
    returned in place of a failure.
    """
    from pathlib import Path  # local import: no import-time filesystem coupling

    resolved = resolve_evidence_path(path_text, allowed_roots)
    path = Path(resolved)
    if not path.is_file():
        _fail_local("E_CALENDAR_EVIDENCE_PATH_MISSING", f"evidence file not found: {resolved}")
    payload_bytes = path.read_bytes()
    digest = hashlib.sha256(payload_bytes).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise ContractError(
            "E_ENVELOPE_HASH_MISMATCH: evidence bytes hash "
            f"{digest} != expected {expected_sha256} (DP04 H04.2 binding chain)"
        )
    try:
        parsed = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail_local("E_CALENDAR_EVIDENCE_SCHEMA", f"evidence file is not UTF-8 JSON: {exc}")
    return calendar_evidence_document_from_mapping(parsed), digest


# ---------------------------------------------------------------------------
# H04.1 DP00 handoff adapter for the 2026 exchange-level closed-dates anchor
# ---------------------------------------------------------------------------


def exchange_closed_dates_document_from_catalog(
    catalog_payload: Mapping[str, Any],
    *,
    obtained_at_nano: int,
    evidence_reference: str,
    evidence_version: str = "dp00-anchor-exchange-closed-dates-2026",
) -> Tuple[CalendarEvidenceDocument, ...]:
    """Convert DP00's china_futures_catalog ``#/calendars`` anchor (read-only
    handoff input) into one EXCHANGE_CLOSED_DATES document per exchange.

    The anchor carries closed civil dates only - no session intervals, no
    night sessions, no per-product validity.  The produced documents
    therefore prove NO_TRADING_EVIDENCED on listed dates and leave every
    other range UNKNOWN (open dates: sessions unknown; pre-2026: no evidence
    at all).  ``review_required`` is carried through, never laundered.
    """
    validate_nano_int(obtained_at_nano)
    documents: List[CalendarEvidenceDocument] = []
    entries = catalog_payload.get("calendars")
    if not isinstance(entries, list) or not entries:
        _fail_local("E_CALENDAR_EVIDENCE_EMPTY", "catalog carries no calendars anchor")
    for entry in entries:
        exchange = str(entry["exchange"]).upper()
        documents.append(
            CalendarEvidenceDocument(
                evidence_version=f"{evidence_version}-{exchange}",
                dataset="futures_kline",
                source_id="dp00-handoff-china-futures-catalog",
                evidence_kind=EvidenceKind.EXCHANGE_CLOSED_DATES,
                data_origin=DataOrigin.REAL_SOURCE,
                applicability_exchanges=(exchange,),
                applicability_products=(),
                applicability_symbols=(),
                valid_from_nano=civil_date_bounds_nano(str(entry["valid_from"]))[0],
                valid_through_nano=civil_date_bounds_nano(str(entry["valid_through"]))[1],
                pit_basis=PitBasis.OBTAINED_AT,
                obtained_at_nano=obtained_at_nano,
                announced_at_nano=None,  # per-exchange announce dates unknown => PIT beyond possession not claimed
                evidence_reference=evidence_reference,
                evidence_text_sha256=canonical_sha256(
                    {"closed_dates": [str(d) for d in entry.get("closed_dates") or []], "exchange": exchange}
                ),
                review_status=str(entry.get("status", "review_required")),
                closed_dates=tuple(
                    ClosedDateRecord(exchange=exchange, date=str(d), reason="EXCHANGE_CLOSED_DATE_ANCHOR")
                    for d in entry.get("closed_dates") or []
                ),
            )
        )
    return tuple(documents)


# ---------------------------------------------------------------------------
# H04.5 Read-only CalendarBook (versioned, append-only evidence chain)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalendarRangeVerdict:
    """One atomic sub-range of a query window with its classification."""

    interval: IntervalNano
    status: RangeStatus
    reason: str
    trading_days: Tuple[str, ...] = ()
    evidence_refs: Tuple[str, ...] = ()

    def canonical(self) -> dict:
        return {
            "evidence_refs": list(self.evidence_refs),
            "end_nano": str(self.interval.end_nano),
            "reason": self.reason,
            "start_nano": str(self.interval.start_nano),
            "status": self.status.value,
            "trading_days": list(self.trading_days),
        }


@dataclass(frozen=True)
class CalendarQueryResult:
    """H04.3-H04.6 answer: the frozen DP01 CalendarAnswer plus the explicit
    unknown ranges, reasons and contributing evidence."""

    answer: CalendarAnswer
    subranges: Tuple[CalendarRangeVerdict, ...]
    data_origins: Tuple[str, ...] = ()
    unknown_reasons: Tuple[str, ...] = ()

    @property
    def status(self) -> CalendarAnswerStatus:
        return self.answer.status


def _merge_verdicts(verdicts: List[CalendarRangeVerdict]) -> Tuple[CalendarRangeVerdict, ...]:
    merged: List[CalendarRangeVerdict] = []
    for verdict in sorted(verdicts, key=lambda v: (v.interval.start_nano, v.interval.end_nano)):
        if (
            merged
            and merged[-1].status is verdict.status
            and merged[-1].reason == verdict.reason
            and merged[-1].trading_days == verdict.trading_days
            and merged[-1].interval.end_nano == verdict.interval.start_nano
        ):
            previous = merged[-1]
            merged[-1] = CalendarRangeVerdict(
                interval=IntervalNano(
                    start_nano=previous.interval.start_nano, end_nano=verdict.interval.end_nano
                ),
                status=previous.status,
                reason=previous.reason,
                trading_days=previous.trading_days,
                evidence_refs=tuple(sorted(set(previous.evidence_refs) | set(verdict.evidence_refs))),
            )
        else:
            merged.append(verdict)
    return tuple(merged)


class CalendarBook:
    """Read-only view over versioned calendar evidence (H04.1/H04.5).

    The book never mutates; new evidence constructs a NEW book whose chain
    hash differs (a new classified version).  Query-time conflicts between
    documents are surfaced as CONFLICT ranges - never silently resolved.
    """

    def __init__(self, documents: Iterable[CalendarEvidenceDocument]) -> None:
        self._documents: Tuple[CalendarEvidenceDocument, ...] = tuple(documents)
        seen: dict = {}
        for document in self._documents:
            label = f"{document.evidence_version}#{document.document_sha256()}"
            if label in seen:
                continue  # identical document bound twice: idempotent
            seen[label] = document
        # Cross-document hard conflicts are detected at construction ONLY when
        # two documents share evidence_version AND domain but differ in bytes
        # (a corrupt chain); content conflicts surface at query time.
        by_version: dict = {}
        for document in self._documents:
            key = document.evidence_version
            by_version.setdefault(key, set()).add(document.document_sha256())
        for version, hashes in by_version.items():
            if len(hashes) > 1:
                _fail_local(
                    "E_CALENDAR_BOOK_DUPLICATE_CONFLICT",
                    f"evidence_version {version!r} bound with {len(hashes)} different "
                    f"documents {sorted(hashes)}; one version is one document",
                )
        self._unique: Tuple[CalendarEvidenceDocument, ...] = tuple(seen.values())

    # -- evidence chain ------------------------------------------------------

    @property
    def documents(self) -> Tuple[CalendarEvidenceDocument, ...]:
        return self._unique

    def evidence_chain_sha256(self) -> Optional[str]:
        """The ``calendar_evidence_sha256`` for plan/commit/report binding
        (DP01 EVIDENCE_CHAIN_HASHES).  None <=> empty book <=> UNKNOWN."""
        if not self._unique:
            return None
        payload = [
            {
                "data_origin": document.data_origin.value,
                "document_sha256": document.document_sha256(),
                "evidence_kind": document.evidence_kind.value,
                "evidence_version": document.evidence_version,
                "synthetic_marker": document.synthetic_marker,
            }
            for document in sorted(
                self._unique, key=lambda d: (d.evidence_version, d.document_sha256())
            )
        ]
        return canonical_sha256(payload)

    def has_synthetic_evidence(self) -> bool:
        return any(d.data_origin in (DataOrigin.SYNTHETIC, DataOrigin.SCENARIO) for d in self._unique)

    # -- H04.3/H04.4 instant-level helpers -----------------------------------

    def trading_day_for_instant(
        self, dataset: str, source_symbol: str, instant_nano: int, as_of_nano: int
    ) -> Optional[str]:
        """Exchange trading-day label for an instant, from evidence only.

        None means UNKNOWN (no evidenced session contains the instant under
        this as_of) - the caller must not fall back to natural days or
        weekday rules (H04.4).
        """
        validate_nano_int(instant_nano)
        eligible = [
            d
            for d in self._unique
            if d.dataset == dataset and d.applies_to_symbol(source_symbol) and d.known_by(as_of_nano)
        ]
        matches = set()
        for document in eligible:
            if document.evidence_kind is not EvidenceKind.SESSION_GRID:
                continue
            for day_record in document.sessions:
                for segment in day_record.segments:
                    if segment.start_nano <= instant_nano < segment.end_nano:
                        matches.add(day_record.trading_day)
        if len(matches) == 1:
            return matches.pop()
        return None

    def daily_session_span(
        self, dataset: str, source_symbol: str, trading_day: str, as_of_nano: int
    ) -> Optional[IntervalNano]:
        """[first session start, last session end) of one trading day."""
        eligible = [
            d
            for d in self._unique
            if d.dataset == dataset and d.applies_to_symbol(source_symbol) and d.known_by(as_of_nano)
        ]
        spans = set()
        for document in eligible:
            if document.evidence_kind is not EvidenceKind.SESSION_GRID:
                continue
            for day_record in document.sessions:
                if day_record.trading_day == trading_day:
                    spans.add(
                        (day_record.first_start_nano(), day_record.last_end_nano())
                    )
        if len(spans) != 1:
            return None
        start, end = spans.pop()
        return IntervalNano(start_nano=start, end_nano=end)

    # -- H04.3-H04.6 the query ------------------------------------------------

    def query(
        self,
        *,
        dataset: str,
        source_symbol: str,
        duration_seconds: int,
        window: IntervalNano,
        as_of_nano: int,
    ) -> CalendarQueryResult:
        validate_duration_seconds(duration_seconds)
        validate_nano_int(as_of_nano)

        relevant = [
            d
            for d in self._unique
            if d.dataset == dataset and d.applies_to_symbol(source_symbol)
        ]
        eligible = [d for d in relevant if d.known_by(as_of_nano)]
        pit_blocked = [d for d in relevant if not d.known_by(as_of_nano)]

        # ---- cross-document conflicts (H04.2 来源冲突显式) ----------------
        conflict_segments: List[Tuple[int, int, Tuple[str, ...]]] = []
        grids = [d for d in eligible if d.evidence_kind is EvidenceKind.SESSION_GRID]
        for i in range(len(grids)):
            for j in range(i + 1, len(grids)):
                a, b = grids[i], grids[j]
                if not _grids_overlap_domain(a, b):
                    continue
                days_a = {r.trading_day: r for r in a.sessions}
                days_b = {r.trading_day: r for r in b.sessions}
                for day in sorted(set(days_a) & set(days_b)):
                    if _day_canonical(days_a[day]) != _day_canonical(days_b[day]):
                        conflict_segments.append(
                            (
                                days_a[day].first_start_nano(),
                                max(days_a[day].last_end_nano(), days_b[day].last_end_nano()),
                                (a.reference_label(), b.reference_label()),
                            )
                        )
        # Grid sessions landing on another eligible document's closed date.
        for grid in grids:
            for other in eligible:
                if other.evidence_kind is not EvidenceKind.EXCHANGE_CLOSED_DATES:
                    continue
                if not _doc_covers_grid_domain(grid, other):
                    continue
                for day_record in grid.sessions:
                    for closed in other.closed_dates:
                        start, end = civil_date_bounds_nano(closed.date)
                        if day_record.first_start_nano() < end and day_record.last_end_nano() > start:
                            conflict_segments.append(
                                (
                                    max(start, day_record.first_start_nano()),
                                    min(end, day_record.last_end_nano()),
                                    (grid.reference_label(), other.reference_label()),
                                )
                            )

        # ---- build the atomic range partition --------------------------------
        edges = {window.start_nano, window.end_nano}
        for document in relevant:
            if window.start_nano < document.valid_from_nano < window.end_nano:
                edges.add(document.valid_from_nano)
            if window.start_nano < document.valid_through_nano < window.end_nano:
                edges.add(document.valid_through_nano)
        session_facts: List[Tuple[str, IntervalNano, str]] = []  # (day, interval, ref)
        lifecycle_windows = [
            w for d in eligible for w in d.instrument_windows if w.source_symbol == source_symbol
        ]
        for document in eligible:
            if document.evidence_kind is not EvidenceKind.SESSION_GRID:
                continue
            for day_record in document.sessions:
                for segment in day_record.segments:
                    if segment.start_nano < window.end_nano and segment.end_nano > window.start_nano:
                        if any(
                            segment.end_nano <= w.valid_from_nano
                            or segment.start_nano >= w.valid_through_nano
                            for w in lifecycle_windows
                        ):
                            continue  # lifecycle excludes this symbol (H04.4)
                        lo = max(segment.start_nano, window.start_nano)
                        hi = min(segment.end_nano, window.end_nano)
                        session_facts.append(
                            (day_record.trading_day, IntervalNano(lo, hi), document.reference_label())
                        )
                        edges.update({lo, hi})
        closed_facts: List[Tuple[IntervalNano, str, str]] = []  # (interval, reason, ref)
        for document in eligible:
            for closed in document.closed_dates:
                start, end = civil_date_bounds_nano(closed.date)
                lo, hi = max(start, window.start_nano), min(end, window.end_nano)
                if lo < hi:
                    closed_facts.append(
                        (IntervalNano(lo, hi), f"CLOSED_DATE:{closed.reason}", document.reference_label())
                    )
                    edges.update({lo, hi})
        # Listed trading-day spans: a day's COMPLETE segment list evidences
        # the intra-span breaks (lunch gap, overnight weekend tail inside one
        # trading day) as NO_TRADING time (H04.3/H04.4).
        day_span_facts: List[Tuple[int, int, str, str]] = []  # (start, end, day, ref)
        for document in eligible:
            if document.evidence_kind is not EvidenceKind.SESSION_GRID:
                continue
            for day_record in document.sessions:
                if day_record.first_start_nano() < window.end_nano and day_record.last_end_nano() > window.start_nano:
                    day_span_facts.append(
                        (
                            day_record.first_start_nano(),
                            day_record.last_end_nano(),
                            day_record.trading_day,
                            document.reference_label(),
                        )
                    )
                    edges.update(
                        e
                        for e in (day_record.first_start_nano(), day_record.last_end_nano())
                        if window.start_nano < e < window.end_nano
                    )
        for record in [
            w for d in eligible for w in d.instrument_windows if w.source_symbol == source_symbol
        ]:
            for edge in (record.valid_from_nano, record.valid_through_nano):
                if window.start_nano < edge < window.end_nano:
                    edges.add(edge)
        for start, end, _refs in conflict_segments:
            for edge in (start, end):
                if window.start_nano < edge < window.end_nano:
                    edges.add(edge)

        ordered_edges = sorted(e for e in edges if window.start_nano <= e <= window.end_nano)
        verdicts: List[CalendarRangeVerdict] = []
        for lo, hi in zip(ordered_edges, ordered_edges[1:]):
            if hi <= lo:
                continue
            verdicts.append(
                self._classify_atomic(
                    lo=lo,
                    hi=hi,
                    source_symbol=source_symbol,
                    eligible=eligible,
                    pit_blocked=pit_blocked,
                    session_facts=session_facts,
                    closed_facts=closed_facts,
                    day_span_facts=day_span_facts,
                    conflict_segments=conflict_segments,
                )
            )
        subranges = _merge_verdicts(verdicts)

        # ---- overall status + port answer (H04.5 masquerade rules) ----------
        statuses = {v.status for v in subranges}
        contributing_refs = tuple(
            sorted({ref for v in subranges for ref in v.evidence_refs})
        )
        contributing_docs = [d for d in eligible if d.reference_label() in set(contributing_refs)]
        chain_hash = self.evidence_chain_sha256()
        versions = "+".join(sorted({d.evidence_version for d in contributing_docs})) or None
        conflict_days = set()
        for start, end, _refs in conflict_segments:
            for day, interval, _ref in session_facts:
                if interval.start_nano < end and interval.end_nano > start:
                    conflict_days.add(day)
        verified_sessions = tuple(
            sorted(
                {
                    (day, interval)
                    for day, interval, _ref in session_facts
                    if day not in conflict_days
                },
                key=lambda pair: (pair[1].start_nano, pair[1].end_nano, pair[0]),
            )
        )

        if RangeStatus.CONFLICT in statuses:
            overall = CalendarAnswerStatus.CONFLICT
        elif RangeStatus.UNKNOWN in statuses or not subranges:
            overall = CalendarAnswerStatus.UNKNOWN
        elif verified_sessions:
            overall = CalendarAnswerStatus.VERIFIED_SESSIONS
        else:
            overall = CalendarAnswerStatus.NO_TRADING_EVIDENCED

        unknown_reasons = tuple(
            sorted({v.reason for v in subranges if v.status is RangeStatus.UNKNOWN})
        )
        data_origins = tuple(sorted({d.data_origin.value for d in contributing_docs}))

        if overall is CalendarAnswerStatus.CONFLICT:
            refs = tuple(sorted({r for _s, _e, conflict_refs in conflict_segments for r in conflict_refs}))
            answer = CalendarAnswer(
                status=overall,
                dataset=dataset,
                source_symbol=source_symbol,
                duration_seconds=duration_seconds,
                window=window,
                as_of_nano=as_of_nano,
                sessions=(),
                evidence_sha256=chain_hash,
                evidence_version=versions,
                conflict_refs=refs,
            )
        elif overall is CalendarAnswerStatus.UNKNOWN:
            # UNKNOWN still lists the VERIFIED sessions it did find (empty
            # only when none were verified): the masquerade rule forbids an
            # empty list posing as "no trading", not honest partial facts.
            # No evidence hash: the window as a whole is not conclusively
            # answered (H04.5 unknown ranges live in CalendarQueryResult).
            answer = CalendarAnswer(
                status=overall,
                dataset=dataset,
                source_symbol=source_symbol,
                duration_seconds=duration_seconds,
                window=window,
                as_of_nano=as_of_nano,
                sessions=verified_sessions,
                evidence_sha256=None,
                evidence_version=None,
            )
        elif overall is CalendarAnswerStatus.VERIFIED_SESSIONS:
            answer = CalendarAnswer(
                status=overall,
                dataset=dataset,
                source_symbol=source_symbol,
                duration_seconds=duration_seconds,
                window=window,
                as_of_nano=as_of_nano,
                sessions=verified_sessions,
                evidence_sha256=chain_hash,
                evidence_version=versions,
            )
        else:  # NO_TRADING_EVIDENCED
            answer = CalendarAnswer(
                status=overall,
                dataset=dataset,
                source_symbol=source_symbol,
                duration_seconds=duration_seconds,
                window=window,
                as_of_nano=as_of_nano,
                sessions=(),
                evidence_sha256=chain_hash,
                evidence_version=versions,
            )
        return CalendarQueryResult(
            answer=answer,
            subranges=subranges,
            data_origins=data_origins,
            unknown_reasons=unknown_reasons,
        )

    # -- DP01 CalendarProvider port -------------------------------------------

    def sessions(
        self,
        *,
        dataset: str,
        source_symbol: str,
        duration_seconds: int,
        window: IntervalNano,
        as_of_nano: int,
    ) -> CalendarAnswer:
        """Frozen DP01 port: explicit status, never an empty-list masquerade."""
        return self.query(
            dataset=dataset,
            source_symbol=source_symbol,
            duration_seconds=duration_seconds,
            window=window,
            as_of_nano=as_of_nano,
        ).answer

    # -- internals --------------------------------------------------------------

    def _classify_atomic(
        self,
        *,
        lo: int,
        hi: int,
        source_symbol: str,
        eligible: Sequence[CalendarEvidenceDocument],
        pit_blocked: Sequence[CalendarEvidenceDocument],
        session_facts: Sequence[Tuple[str, IntervalNano, str]],
        closed_facts: Sequence[Tuple[IntervalNano, str, str]],
        day_span_facts: Sequence[Tuple[int, int, str, str]],
        conflict_segments: Sequence[Tuple[int, int, Tuple[str, ...]]],
    ) -> CalendarRangeVerdict:
        interval = IntervalNano(lo, hi)

        def _refs(*docs: CalendarEvidenceDocument) -> Tuple[str, ...]:
            return tuple(sorted({d.reference_label() for d in docs}))

        for start, end, conflict_refs in conflict_segments:
            if lo >= start and hi <= end:
                return CalendarRangeVerdict(
                    interval=interval,
                    status=RangeStatus.CONFLICT,
                    reason="CONFLICTING_CALENDAR_EVIDENCE",
                    evidence_refs=conflict_refs,
                )
        # Instrument lifecycle FIRST: a specific contract's listing window
        # dominates product-level sessions (a delisted contract does not
        # trade merely because the product has sessions - H04.4).
        for document in eligible:
            for record in document.instrument_windows:
                if record.source_symbol != source_symbol:
                    continue
                if hi <= record.valid_from_nano:
                    return CalendarRangeVerdict(
                        interval=interval,
                        status=RangeStatus.NO_TRADING_EVIDENCED,
                        reason="PRE_LISTING_NO_TRADING",
                        evidence_refs=(document.reference_label(),),
                    )
                if lo >= record.valid_through_nano:
                    return CalendarRangeVerdict(
                        interval=interval,
                        status=RangeStatus.NO_TRADING_EVIDENCED,
                        reason="POST_DELISTING_NO_TRADING",
                        evidence_refs=(document.reference_label(),),
                    )
        hit_sessions = [
            (day, ref) for day, seg, ref in session_facts if seg.start_nano <= lo and hi <= seg.end_nano
        ]
        if hit_sessions:
            days = tuple(sorted({d for d, _ in hit_sessions}))
            if len(days) == 1:
                return CalendarRangeVerdict(
                    interval=interval,
                    status=RangeStatus.VERIFIED_SESSIONS,
                    reason=f"SESSION:{days[0]}",
                    trading_days=days,
                    evidence_refs=tuple(sorted({r for _d, r in hit_sessions})),
                )
            return CalendarRangeVerdict(
                interval=interval,
                status=RangeStatus.CONFLICT,
                reason="CONFLICTING_CALENDAR_EVIDENCE_OVERLAPPING_SESSIONS",
                trading_days=days,
                evidence_refs=tuple(sorted({r for _d, r in hit_sessions})),
            )
        for closed_interval, reason, ref in closed_facts:
            if closed_interval.start_nano <= lo and hi <= closed_interval.end_nano:
                return CalendarRangeVerdict(
                    interval=interval,
                    status=RangeStatus.NO_TRADING_EVIDENCED,
                    reason=reason,
                    evidence_refs=(ref,),
                )
        # Intra-span break of a listed trading day: the day's complete
        # segment list evidences the gaps between its own sessions.
        span_hits = [
            (day, ref)
            for start, end, day, ref in day_span_facts
            if start <= lo and hi <= end
        ]
        span_days = {day for day, _ref in span_hits}
        if len(span_days) == 1:
            day = span_days.pop()
            return CalendarRangeVerdict(
                interval=interval,
                status=RangeStatus.NO_TRADING_EVIDENCED,
                reason=f"INTRADAY_BREAK:{day}",
                trading_days=(day,),
                evidence_refs=tuple(sorted({ref for _d, ref in span_hits})),
            )
        if len(span_days) > 1:
            return CalendarRangeVerdict(
                interval=interval,
                status=RangeStatus.UNKNOWN,
                reason="OVERLAPPING_TRADING_DAY_SPANS",
                trading_days=tuple(sorted(span_days)),
                evidence_refs=tuple(sorted({ref for _d, ref in span_hits})),
            )
        # COMPLETE grid enumeration: covered validity, no session => no trading.
        complete_docs = [
            d
            for d in eligible
            if d.evidence_kind is EvidenceKind.SESSION_GRID
            and d.coverage is CoverageDeclaration.COMPLETE_IN_VALIDITY_DOMAIN
            and d.valid_from_nano <= lo
            and hi <= d.valid_through_nano
        ]
        if complete_docs:
            return CalendarRangeVerdict(
                interval=interval,
                status=RangeStatus.NO_TRADING_EVIDENCED,
                reason="ENUMERATED_NO_SESSION_IN_COMPLETE_GRID",
                evidence_refs=_refs(*complete_docs),
            )
        # Inside some document's validity domain but sessions unknown there.
        covering = [
            d
            for d in eligible
            if d.valid_from_nano <= lo and hi <= d.valid_through_nano
        ]
        if covering:
            grid_like = [d for d in covering if d.evidence_kind is EvidenceKind.SESSION_GRID]
            if grid_like:
                reason = "PARTIAL_GRID_SESSION_NOT_EVIDENCED"
            else:
                reason = "CLOSED_DATES_ONLY_SESSIONS_UNKNOWN"
            return CalendarRangeVerdict(
                interval=interval,
                status=RangeStatus.UNKNOWN,
                reason=reason,
                evidence_refs=_refs(*covering),
            )
        blocked_covering = [
            d
            for d in pit_blocked
            if d.valid_from_nano <= lo and hi <= d.valid_through_nano
        ]
        if blocked_covering:
            return CalendarRangeVerdict(
                interval=interval,
                status=RangeStatus.UNKNOWN,
                reason="EVIDENCE_NOT_KNOWN_AT_AS_OF" if all(
                    d.pit_basis is PitBasis.OBTAINED_AT for d in blocked_covering
                ) else "EVIDENCE_PIT_BASIS_UNKNOWN",
                evidence_refs=_refs(*blocked_covering),
            )
        # Not covered by any relevant document's validity domain.
        reason = "NO_CALENDAR_EVIDENCE_IN_DOMAIN"
        all_relevant = list(eligible) + list(pit_blocked)
        earliest_from = min((d.valid_from_nano for d in all_relevant), default=None)
        latest_through = max((d.valid_through_nano for d in all_relevant), default=None)
        if earliest_from is not None and hi <= earliest_from:
            reason = "BEFORE_EVIDENCE_VALIDITY_PRE_2026_STYLE"
        elif latest_through is not None and lo >= latest_through:
            reason = "AFTER_EVIDENCE_VALIDITY"
        return CalendarRangeVerdict(
            interval=interval,
            status=RangeStatus.UNKNOWN,
            reason=reason,
        )


def _grids_overlap_domain(a: CalendarEvidenceDocument, b: CalendarEvidenceDocument) -> bool:
    """Could both documents apply to at least one common symbol?

    Structural check (no symbol enumeration): symbol lists intersect, or an
    exchange-wide side covers the other side's exchange/product domain.
    """

    def parts(doc: CalendarEvidenceDocument) -> Tuple[set, set, set]:
        symbols = set(doc.applicability_symbols)
        products = {p.lower() for p in doc.applicability_products}
        exchanges = {e.upper() for e in doc.applicability_exchanges}
        return symbols, products, exchanges

    sa, pa, ea = parts(a)
    sb, pb, eb = parts(b)
    if sa & sb:
        return True
    if pa & pb:
        return True
    for product in pa:
        exchange = _exchange_of_product(product)
        if exchange in eb:
            return True
    for product in pb:
        exchange = _exchange_of_product(product)
        if exchange in ea:
            return True
    return False


def _doc_covers_grid_domain(grid: CalendarEvidenceDocument, other: CalendarEvidenceDocument) -> bool:
    """Does the closed-dates document's exchange scope cover the grid's?"""
    grid_exchanges = {e.upper() for e in grid.applicability_exchanges} | {
        _exchange_of_product(p) for p in grid.applicability_products
    }
    other_exchanges = {e.upper() for e in other.applicability_exchanges}
    return bool(grid_exchanges & other_exchanges)


def _exchange_of_product(product_key: str) -> str:
    return product_key.split(".", 1)[0].upper() if "." in product_key else product_key.upper()


def _day_canonical(record: SessionDayRecord) -> Tuple:
    return (record.trading_day, tuple((s.kind.value, s.start_nano, s.end_nano) for s in record.segments))


def bind_calendar_evidence(
    expected_chain_sha256: Optional[str],
    documents: Sequence[CalendarEvidenceDocument],
) -> CalendarBook:
    """H04.2 binding: plan-declared chain hash must match the actual book AND
    the book must be non-empty - a matching hash over an empty book is a
    binding failure, never a success (hash对但实际book空)."""
    book = CalendarBook(documents)
    if not documents:
        _fail_local(
            "E_CALENDAR_BINDING_EMPTY",
            "binding an empty document list is not a successful calendar binding",
        )
    actual = book.evidence_chain_sha256()
    if expected_chain_sha256 is None:
        _fail_local(
            "E_CALENDAR_BINDING_EMPTY",
            "plan carried calendar_evidence_sha256=None (UNKNOWN); refusing to bind "
            "documents against an UNKNOWN expectation",
        )
    if actual != expected_chain_sha256:
        raise ContractError(
            "E_ENVELOPE_HASH_MISMATCH: evidence chain hash "
            f"{actual} != plan expectation {expected_chain_sha256} (H04.2)"
        )
    return book


# ---------------------------------------------------------------------------
# H04.3 bar classification (60-second slots vs daily semantics)
# ---------------------------------------------------------------------------


def classify_bar_calendar(
    book: CalendarBook,
    *,
    dataset: str,
    source_symbol: str,
    duration_seconds: int,
    bar_start_nano: int,
    bar_end_nano: int,
    as_of_nano: int,
) -> Tuple[BarCalendarVerdict, Optional[str], Optional[str]]:
    """Classify ONE bar interval against the evidenced calendar.

    Returns (verdict, trading_day_or_None, reason_or_None).

    60-second bars: IN_SESSION requires the WHOLE [start, end) inside ONE
    evidenced session segment (same trading day), a minute-grid-aligned
    start and an exact 60 s length.  A bar whose start lies in a segment but
    whose end crosses the segment tail (lunch break, close, night end) is
    CROSSES_SESSION_BOUNDARY - never legal merely because the start lies in
    a segment (H04.3 / finding F16).  Daily bars: the verdict refers to the
    daily session span [first start, last end); the natural-day
    start+86400 inference is structurally impossible (the span comes from
    evidence and DP01's assert_daily_close_evidenced guard applies when the
    caller builds the daily interval).
    """
    validate_duration_seconds(duration_seconds)
    validate_nano_int(bar_start_nano)
    validate_nano_int(bar_end_nano)
    if bar_end_nano <= bar_start_nano:
        raise ContractError("E_CALENDAR_SLOT_WINDOW: bar interval must be [start, end)")
    window = IntervalNano(bar_start_nano, bar_end_nano)
    if duration_seconds == 60 and bar_end_nano - bar_start_nano != MINUTE_SLOT_NANO:
        _fail_local(
            "E_CALENDAR_BAR_LENGTH",
            f"60 s bar length {bar_end_nano - bar_start_nano}ns != {MINUTE_SLOT_NANO}ns",
        )
    # duration 86400: the daily bar interval IS the trading-day session span
    # [first session start, last session end) - its physical length is NOT
    # 86400s and must never be (Q01: natural-day inference forbidden).
    result = book.query(
        dataset=dataset,
        source_symbol=source_symbol,
        duration_seconds=duration_seconds,
        window=window,
        as_of_nano=as_of_nano,
    )
    subranges = result.subranges
    if any(v.status is RangeStatus.CONFLICT for v in subranges):
        return (BarCalendarVerdict.CALENDAR_CONFLICT, None, "CONFLICTING_CALENDAR_EVIDENCE")
    hits = [v for v in subranges if v.status is RangeStatus.VERIFIED_SESSIONS]

    if duration_seconds == 60:
        if hits:
            # The bar touches an evidenced session: the specific session
            # rules decide - a bar is NOT downgraded to a generic UNKNOWN
            # merely because its tail leaves the segment (H04.3/F16).
            if not is_minute_grid_aligned(bar_start_nano):
                return (BarCalendarVerdict.UNALIGNED_START, None, "START_OFF_MINUTE_GRID")
            for verdict_range in hits:
                if (
                    verdict_range.interval.start_nano <= bar_start_nano
                    and bar_end_nano <= verdict_range.interval.end_nano
                    and len(verdict_range.trading_days) == 1
                ):
                    return (BarCalendarVerdict.IN_SESSION, verdict_range.trading_days[0], None)
            return (
                BarCalendarVerdict.CROSSES_SESSION_BOUNDARY,
                hits[0].trading_days[0] if len(hits[0].trading_days) == 1 else None,
                "BAR_TAIL_CROSSES_SESSION_BOUNDARY",
            )
        if any(v.status is RangeStatus.UNKNOWN for v in subranges):
            return (BarCalendarVerdict.CALENDAR_UNKNOWN, None, ";".join(result.unknown_reasons))
        return (BarCalendarVerdict.OUTSIDE_SESSION_EVIDENCED, None, "NO_TRADING_EVIDENCED")

    # duration 86400: unknown anywhere still blocks the daily judgement.
    if any(v.status is RangeStatus.UNKNOWN for v in subranges):
        return (BarCalendarVerdict.CALENDAR_UNKNOWN, None, ";".join(result.unknown_reasons))
    candidate_days = {
        day
        for verdict_range in subranges
        if verdict_range.status is RangeStatus.VERIFIED_SESSIONS
        for day in verdict_range.trading_days
    }
    spans = []
    for day in sorted(candidate_days):
        span = book.daily_session_span(dataset, source_symbol, day, as_of_nano)
        if span is not None:
            spans.append((day, span))
    for day, span in sorted(spans, key=lambda kv: (kv[1].start_nano, kv[0])):
        if span.start_nano == bar_start_nano and span.end_nano == bar_end_nano:
            # Bind the DP01 frozen guard: daily close must be the evidenced
            # session end, never start+86400.
            assert_daily_close_evidenced(
                duration_seconds, bar_start_nano, bar_end_nano, span.end_nano
            )
            return (BarCalendarVerdict.IN_SESSION, day, None)
    if spans:
        return (BarCalendarVerdict.CROSSES_SESSION_BOUNDARY, None, "DAILY_BAR_SPAN_MISMATCH")
    return (BarCalendarVerdict.OUTSIDE_SESSION_EVIDENCED, None, "NO_TRADING_EVIDENCED")


def trading_day_for_instant(
    book: CalendarBook,
    *,
    dataset: str,
    source_symbol: str,
    instant_nano: int,
    as_of_nano: int,
) -> Optional[str]:
    return book.trading_day_for_instant(dataset, source_symbol, instant_nano, as_of_nano)


def daily_session_span(
    book: CalendarBook,
    *,
    dataset: str,
    source_symbol: str,
    trading_day: str,
    as_of_nano: int,
) -> Optional[IntervalNano]:
    return book.daily_session_span(dataset, source_symbol, trading_day, as_of_nano)


# ---------------------------------------------------------------------------
# H04.7 expected bar grid bound to BarGenerationEvidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpectedSlotGrid:
    """Expected 60 s slots (or daily bars) with explicit unknown ranges.

    ``slots``/``daily_bars`` are non-empty only under EXPECTED_GRID.  Under
    EXPECTATION_UNKNOWN nothing is fabricated - missing bars are NOT
    zero-volume bars (H04.7)."""

    status: ExpectationStatus
    dataset: str
    source_symbol: str
    duration_seconds: int
    window: IntervalNano
    as_of_nano: int
    slots: Tuple[int, ...] = ()                     # 60 s slot starts (duration 60)
    daily_bars: Tuple[Tuple[str, IntervalNano], ...] = ()  # (trading_day, span)
    unknown_ranges: Tuple[IntervalNano, ...] = ()
    reasons: Tuple[str, ...] = ()
    generation_evidence_sha256: Optional[str] = None


def _assert_generation_evidence_applies(
    generation: BarGenerationEvidence,
    *,
    dataset: str,
    source_symbol: str,
    duration_seconds: int,
    window: IntervalNano,
) -> None:
    if generation.dataset != dataset:
        raise ContractError(
            "E_CALENDAR_GENERATION_NOT_APPLICABLE: generation evidence dataset "
            f"{generation.dataset!r} does not bind task dataset {dataset!r}"
        )
    if generation.duration_seconds != duration_seconds:
        raise ContractError(
            "E_CALENDAR_GENERATION_NOT_APPLICABLE: generation evidence duration "
            f"{generation.duration_seconds}s does not bind task duration {duration_seconds}s"
        )
    if generation.applicable_symbols:
        applies = source_symbol in generation.applicable_symbols
    else:
        # product-scoped evidence: the symbol's (exchange, product) domain
        # must be listed; an unparseable symbol never matches product scope.
        applies = any(
            product in generation.applicable_products
            for product in (f"{e}.{p}" for e, p in _domains_of(source_symbol))
        )
    if not applies:
        raise ContractError(
            "E_CALENDAR_GENERATION_NOT_APPLICABLE: generation evidence applicability "
            f"{sorted(generation.applicable_symbols) or sorted(generation.applicable_products)} "
            f"does not cover symbol {source_symbol!r}"
        )
    if (
        window.start_nano < generation.valid_from_nano
        or window.end_nano > generation.valid_through_nano
    ):
        raise ContractError(
            "E_CALENDAR_GENERATION_NOT_APPLICABLE: query window escapes the generation "
            "evidence validity domain; bind an evidence version that covers the window"
        )


def build_expected_slot_grid(
    book: CalendarBook,
    *,
    dataset: str,
    source_symbol: str,
    window: IntervalNano,
    as_of_nano: int,
    generation: Optional[BarGenerationEvidence],
) -> ExpectedSlotGrid:
    """H04.7: enumerate the expected 60 s slot grid.

    Only an evidenced FIXED_SLOT generation rule may produce a grid; UNKNOWN
    or TRADE_TRIGGERED policies return EXPECTATION_UNKNOWN (bars exist only
    on trades -> not enumerable; absent bars are then a source question for
    DP05, never fabricated here).  Calendar-unknown sub-ranges are carried
    as unknown_ranges, never silently filled.
    """
    validate_duration_seconds(60)
    base_reasons: List[str] = []
    if generation is None or generation.status() is GenerationEvidenceStatus.UNKNOWN:
        base_reasons.append("GENERATION_EVIDENCE_UNKNOWN" if generation is None else "GENERATION_EVIDENCE_UNKNOWN")
        return ExpectedSlotGrid(
            status=ExpectationStatus.EXPECTATION_UNKNOWN,
            dataset=dataset,
            source_symbol=source_symbol,
            duration_seconds=60,
            window=window,
            as_of_nano=as_of_nano,
            unknown_ranges=(window,),
            reasons=tuple(base_reasons),
            generation_evidence_sha256=(
                generation.generation_evidence_sha256() if generation is not None else None
            ),
        )
    _assert_generation_evidence_applies(
        generation,
        dataset=dataset,
        source_symbol=source_symbol,
        duration_seconds=60,
        window=window,
    )
    if generation.emission_policy is EmissionPolicy.TRADE_TRIGGERED:
        return ExpectedSlotGrid(
            status=ExpectationStatus.EXPECTATION_UNKNOWN,
            dataset=dataset,
            source_symbol=source_symbol,
            duration_seconds=60,
            window=window,
            as_of_nano=as_of_nano,
            unknown_ranges=(window,),
            reasons=("TRADE_TRIGGERED_NOT_ENUMERABLE",),
            generation_evidence_sha256=generation.generation_evidence_sha256(),
        )
    result = book.query(
        dataset=dataset,
        source_symbol=source_symbol,
        duration_seconds=60,
        window=window,
        as_of_nano=as_of_nano,
    )
    slots: List[int] = []
    unknown: List[IntervalNano] = []
    for verdict_range in result.subranges:
        if verdict_range.status is RangeStatus.VERIFIED_SESSIONS:
            for start in expected_minute_slots_in_segment(verdict_range.interval):
                slots.append(start)
        elif verdict_range.status is RangeStatus.CONFLICT:
            unknown.append(verdict_range.interval)
        else:
            # NO_TRADING ranges contribute no slots; UNKNOWN ranges stay unknown.
            if verdict_range.status is RangeStatus.UNKNOWN:
                unknown.append(verdict_range.interval)
    reasons = tuple(sorted({f"CALENDAR_{v.status.value}" for v in result.subranges if v.status is not RangeStatus.NO_TRADING_EVIDENCED}))
    status = ExpectationStatus.EXPECTATION_UNKNOWN if unknown else ExpectationStatus.EXPECTED_GRID
    return ExpectedSlotGrid(
        status=status,
        dataset=dataset,
        source_symbol=source_symbol,
        duration_seconds=60,
        window=window,
        as_of_nano=as_of_nano,
        slots=tuple(sorted(set(slots))),
        daily_bars=(),
        unknown_ranges=tuple(unknown),
        reasons=reasons if status is ExpectationStatus.EXPECTATION_UNKNOWN else (),
        generation_evidence_sha256=generation.generation_evidence_sha256(),
    )


def build_expected_daily_bars(
    book: CalendarBook,
    *,
    dataset: str,
    source_symbol: str,
    window: IntervalNano,
    as_of_nano: int,
    generation: Optional[BarGenerationEvidence],
) -> ExpectedSlotGrid:
    """H04.7 daily semantics: one expected daily bar per evidenced trading
    day, keyed [first session start, last session end) - the daily close is
    the evidenced session end, never start+86400 (DP01 Q01 guard re-bound
    per produced bar)."""
    validate_duration_seconds(86400)
    if generation is None or generation.status() is GenerationEvidenceStatus.UNKNOWN:
        return ExpectedSlotGrid(
            status=ExpectationStatus.EXPECTATION_UNKNOWN,
            dataset=dataset,
            source_symbol=source_symbol,
            duration_seconds=86400,
            window=window,
            as_of_nano=as_of_nano,
            unknown_ranges=(window,),
            reasons=("GENERATION_EVIDENCE_UNKNOWN",),
            generation_evidence_sha256=(
                generation.generation_evidence_sha256() if generation is not None else None
            ),
        )
    _assert_generation_evidence_applies(
        generation,
        dataset=dataset,
        source_symbol=source_symbol,
        duration_seconds=86400,
        window=window,
    )
    if generation.emission_policy is EmissionPolicy.TRADE_TRIGGERED:
        return ExpectedSlotGrid(
            status=ExpectationStatus.EXPECTATION_UNKNOWN,
            dataset=dataset,
            source_symbol=source_symbol,
            duration_seconds=86400,
            window=window,
            as_of_nano=as_of_nano,
            unknown_ranges=(window,),
            reasons=("TRADE_TRIGGERED_NOT_ENUMERABLE",),
            generation_evidence_sha256=generation.generation_evidence_sha256(),
        )
    result = book.query(
        dataset=dataset,
        source_symbol=source_symbol,
        duration_seconds=86400,
        window=window,
        as_of_nano=as_of_nano,
    )
    daily: List[Tuple[str, IntervalNano]] = []
    unknown: List[IntervalNano] = []
    for verdict_range in result.subranges:
        if verdict_range.status is RangeStatus.VERIFIED_SESSIONS and len(verdict_range.trading_days) == 1:
            day = verdict_range.trading_days[0]
            span = book.daily_session_span(dataset, source_symbol, day, as_of_nano)
            if span is not None and span.start_nano < window.end_nano and span.end_nano > window.start_nano:
                daily.append((day, span))
        elif verdict_range.status is RangeStatus.UNKNOWN or verdict_range.status is RangeStatus.CONFLICT:
            unknown.append(verdict_range.interval)
    # dedup by day (a day's span may appear as several merged verdicts)
    by_day = {}
    for day, span in daily:
        by_day[day] = span
    bars = tuple(sorted(by_day.items(), key=lambda kv: kv[1].start_nano))
    for _day, span in bars:
        assert_daily_close_evidenced(86400, span.start_nano, span.end_nano, span.end_nano)
    status = ExpectationStatus.EXPECTATION_UNKNOWN if unknown else ExpectationStatus.EXPECTED_GRID
    return ExpectedSlotGrid(
        status=status,
        dataset=dataset,
        source_symbol=source_symbol,
        duration_seconds=86400,
        window=window,
        as_of_nano=as_of_nano,
        slots=(),
        daily_bars=bars,
        unknown_ranges=tuple(unknown),
        reasons=() if status is ExpectationStatus.EXPECTED_GRID else ("CALENDAR_UNKNOWN_OR_CONFLICT",),
        generation_evidence_sha256=generation.generation_evidence_sha256(),
    )
