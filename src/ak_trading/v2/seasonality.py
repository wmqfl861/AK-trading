"""Intraday seasonality: training-window slot statistics only (M2 §4.3).

Revision r3 implements 变更申请-005 §1/§2/§5 on top of the precheck
repairs:

- **Cumulative slot indices** (Q01): for one product, trading day and
  session kind, the declared contiguous ``SessionWindow`` fragments
  ``W0..Wk`` (sorted by start) contribute their *planned complete* slot
  counts ``N_j = floor(span_j / duration)``; a full bar in ``W_j`` at
  offset ``i`` receives the unique ``slot_index = sum(N_h, h<j) + i``.
  DAY and NIGHT accumulate separately and restart each trading day;
  missing observations never shift later slot numbers, and a fragment
  tail shorter than the requested duration never enters a bucket.  The
  bucket key keeps its seven fields — no segment field exists.
- **Close-to-close absolute returns** (Q02): the return column is exactly
  ``abs(ln(bar.close / previous_bar.close))``.  ``seasonal_values`` takes
  a *required* ``previous_bar`` keyword: the calendar-planned previous
  complete slot of the same contract, visible at the decision; the
  segment-opening overnight/intraday gap enters through that return.  A
  missing/invisible previous slot degrades only the return column
  (``MISSING/previous_bar_missing``); a wrong-identity previous bar is
  ``INVALID``; the volume column stays independent.  ``|ln(C/O)|`` is no
  longer used anywhere.
- **Training gates** (E01/E06 + 005): a model whose training ends after
  the decision makes both columns ``INVALID/model_fit_after_decision``;
  missing training-calendar evidence is ``MISSING/calendar_evidence_missing``;
  a different ``calendar_version`` is ``INVALID/calendar_version_mismatch``;
  the same version with a different same-kind day template is
  ``INVALID/calendar_template_mismatch``.  Availability times never hide
  the model boundary: the volume row is available at
  ``max(bar.available, model.fit_end)`` and the return row additionally
  includes ``previous_bar.available``; unknown availability stays
  unknown.
- **PIT before conflict** (E03): fit filters candidates into the training
  window (data time and availability) before duplicate detection;
  content-equal rows deduplicate idempotently, simultaneously visible
  conflicts and multi-contract contention on one product day slot are
  rejected — no first-writer-wins.
- **Public content hash** (004 §3): ``SeasonalityModel.content_hash``
  hashes the canonical projection of the final six fields minus its own
  ``model_id``; ``model_id = "seasonality:" + content_hash()``.
- **Versions** (005 §5): the operator is ``seasonality-v2.1.1``, the two
  columns are ``SEASONAL:volume_relative:v2.1.1:d<duration>`` and
  ``SEASONAL:abs_return_relative:v2.1.1:d<duration>``, and the model file
  schema is ``ak_probability_seasonality_v2.1.1``.  ``input_sha256``
  binds the operator version, fit window, candidate/previous-slot
  content, training calendar templates and the per-day-slot selected
  contract ids.

Buckets still need at least 20 contributing days per column before that
column's median exists; zero medians are ``INVALID`` (zero denominator),
never a substituted 1.
"""
from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Mapping

from ak_trading.v2.contracts import DataQuality, FactorValue, MarketBar, SessionWindow
from ak_trading.v2.schemas import canonical_json_bytes

SCHEME_VERSION = "v2.1.1"
SEASONALITY_SCHEMA_VERSION = "ak_probability_seasonality_v2.1.1"
OPERATOR_VERSION = "seasonality-v2.1.1"
MINIMUM_SAMPLE_DAYS = 20
DAY_NANO = 86_400 * 1_000_000_000
SEASONAL_VOLUME_KEY = "SEASONAL:volume_relative:" + SCHEME_VERSION + ":d{duration_seconds}"
SEASONAL_ABS_RETURN_KEY = "SEASONAL:abs_return_relative:" + SCHEME_VERSION + ":d{duration_seconds}"

_BAR_CONTENT_FIELDS = (
    "bar_id", "contract_id", "raw_symbol", "product", "duration_seconds",
    "datetime_nano", "start_nano", "end_nano", "trading_day", "session_id",
    "available_at_nano", "imported_at_nano", "open", "high", "low", "close",
    "volume", "open_oi", "close_oi", "complete", "expected_minutes",
    "actual_minutes", "source",
)


class SeasonalityError(ValueError):
    """Fixed-reason seasonality violation."""


def seasonal_factor_keys(duration_seconds: int) -> tuple[str, str]:
    """The two seasonal column ids for one duration, sorted by key."""
    return (
        SEASONAL_ABS_RETURN_KEY.format(duration_seconds=duration_seconds),
        SEASONAL_VOLUME_KEY.format(duration_seconds=duration_seconds),
    )


def _bar_content(bar: MarketBar) -> str:
    parts = []
    for name in _BAR_CONTENT_FIELDS:
        value = getattr(bar, name)
        if value is None:
            parts.append("\\0")
        elif isinstance(value, Decimal):
            parts.append(str(value))
        elif isinstance(value, date):
            parts.append(value.isoformat())
        elif isinstance(value, bool):
            parts.append("true" if value else "false")
        else:
            parts.append(str(value))
    return "|".join(parts)


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Planned-slot calendar (Q01): cumulative slot ordinals over the declared
# fragments of one product / trading day / kind.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _SlotAddress:
    session_id: str
    kind: str
    trading_day: date
    slot_index: int
    start_nano: int
    end_nano: int


class _ProductCalendar:
    """Planned slot grid of one product built from frozen SessionWindows."""

    def __init__(self, sessions: tuple[SessionWindow, ...], product: str,
                 duration_seconds: int) -> None:
        self.product = product
        self.duration_seconds = duration_seconds
        self.slot_nano = duration_seconds * 1_000_000_000
        self._sessions = sorted(
            (s for s in sessions if s.product == product), key=lambda s: s.start_nano)
        self._by_id = {s.session_id: s for s in self._sessions}
        # fragments grouped by (kind, trading_day), sorted by start
        self._fragments: dict[tuple[str, date], list[SessionWindow]] = {}
        for session in self._sessions:
            self._fragments.setdefault((session.kind, session.trading_day), []).append(session)
        for key in self._fragments:
            self._fragments[key].sort(key=lambda s: s.start_nano)
        # full planned slot sequence across all kinds, chronological
        self._slots: list[_SlotAddress] = []
        for (kind, trading_day), fragments in sorted(
                self._fragments.items(), key=lambda item: min(f.start_nano for f in item[1])):
            offset = 0
            for fragment in fragments:
                count = (fragment.end_nano - fragment.start_nano) // self.slot_nano
                for i in range(count):
                    self._slots.append(_SlotAddress(
                        session_id=fragment.session_id, kind=kind,
                        trading_day=trading_day, slot_index=offset + i,
                        start_nano=fragment.start_nano + i * self.slot_nano,
                        end_nano=fragment.start_nano + (i + 1) * self.slot_nano))
                offset += count
        self._ordered_slots = sorted(self._slots, key=lambda slot: slot.start_nano)
        self._slot_position = {slot.start_nano: index
                               for index, slot in enumerate(self._ordered_slots)}

    @property
    def sessions(self) -> tuple[SessionWindow, ...]:
        return tuple(self._sessions)

    def session(self, session_id: str | None) -> SessionWindow | None:
        if session_id is None:
            return None
        return self._by_id.get(session_id)

    def address(self, bar: MarketBar) -> _SlotAddress | None:
        """The unique planned slot a full bar occupies, else None."""
        session = self._by_id.get(bar.session_id) if bar.session_id is not None else None
        if session is None or session.product != bar.product:
            return None
        if bar.duration_seconds != self.duration_seconds:
            return None
        if bar.start_nano < session.start_nano or bar.end_nano > session.end_nano:
            return None
        if (bar.start_nano - session.start_nano) % self.slot_nano != 0:
            return None
        fragments = self._fragments.get((session.kind, session.trading_day), [])
        offset = 0
        for fragment in fragments:
            count = (fragment.end_nano - fragment.start_nano) // self.slot_nano
            if fragment.session_id == session.session_id:
                index = (bar.start_nano - fragment.start_nano) // self.slot_nano
                if index >= count:
                    return None
                return _SlotAddress(
                    session_id=session.session_id, kind=session.kind,
                    trading_day=session.trading_day, slot_index=offset + index,
                    start_nano=bar.start_nano, end_nano=bar.start_nano + self.slot_nano)
            offset += count
        return None

    def previous_slot(self, slot: _SlotAddress) -> _SlotAddress | None:
        """The chronologically previous planned slot across all kinds."""
        position = self._slot_position.get(slot.start_nano)
        if position is None:
            return None
        return self._ordered_slots[position - 1] if position > 0 else None

    def template(self, kind: str) -> list[tuple[int, int]]:
        """Normalized day template of one kind: [(UTC intraday start, span)].

        All declared days of this kind must share one template; different
        templates under one calendar version are a mismatch (005 §1).
        """
        seen: list[tuple[tuple[int, int], ...]] = []
        for (fragment_kind, _day), fragments in self._fragments.items():
            if fragment_kind != kind:
                continue
            template = tuple(
                (fragment.start_nano % DAY_NANO, fragment.end_nano - fragment.start_nano)
                for fragment in fragments)
            if template not in seen:
                seen.append(template)
        if not seen:
            return []
        if len(seen) > 1:
            raise SeasonalityError("calendar_template_mismatch")
        return sorted(seen[0])

    def day_template(self, kind: str, trading_day: date) -> list[tuple[int, int]]:
        """The current trading day's normalized fragment template of a kind."""
        fragments = self._fragments.get((kind, trading_day), ())
        return sorted(
            (fragment.start_nano % DAY_NANO, fragment.end_nano - fragment.start_nano)
            for fragment in fragments)

    def calendars_between(self, start_nano: int, end_nano: int) -> tuple[SessionWindow, ...]:
        return tuple(s for s in self._sessions
                     if s.start_nano < end_nano and s.end_nano > start_nano)


# ---------------------------------------------------------------------------
# Model types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SeasonalBucket:
    product: str
    session_kind: str
    slot_index: int
    duration_seconds: int
    median_volume: Decimal | None
    median_abs_return: float | None
    sample_days: int

    def __post_init__(self) -> None:
        if not isinstance(self.product, str) or not self.product:
            raise ValueError("product_must_be_non_empty_str")
        if self.session_kind not in ("DAY", "NIGHT"):
            raise ValueError("session_kind_must_be_DAY_or_NIGHT")
        if isinstance(self.slot_index, bool) or not isinstance(self.slot_index, int):
            raise TypeError("slot_index_must_be_int")
        if self.slot_index < 0:
            raise ValueError("slot_index_must_be_non_negative")
        if isinstance(self.duration_seconds, bool) or not isinstance(self.duration_seconds, int):
            raise TypeError("duration_seconds_must_be_int")
        if isinstance(self.sample_days, bool) or not isinstance(self.sample_days, int):
            raise TypeError("sample_days_must_be_int")
        if self.sample_days < 0:
            raise ValueError("sample_days_must_be_non_negative")
        for name, value in (("median_volume", self.median_volume),
                            ("median_abs_return", self.median_abs_return)):
            if value is None:
                continue
            if isinstance(value, Decimal):
                if not value.is_finite():
                    raise ValueError(f"{name}_must_be_finite")
            elif isinstance(value, bool) or not isinstance(value, float) or not math.isfinite(value):
                raise ValueError(f"{name}_must_be_finite_float")


@dataclass(frozen=True)
class SeasonalityModel:
    model_id: str
    fit_start_nano: int
    fit_end_nano: int
    calendar_version: str
    buckets: tuple[SeasonalBucket, ...]
    input_sha256: str

    def __post_init__(self) -> None:
        for name in ("model_id", "calendar_version"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name}_must_be_non_empty_str")
        for name in ("fit_start_nano", "fit_end_nano"):
            if isinstance(getattr(self, name), bool) or not isinstance(getattr(self, name), int):
                raise TypeError(f"{name}_must_be_int")
        if self.fit_start_nano >= self.fit_end_nano:
            raise ValueError("fit_start_must_precede_fit_end")
        if not isinstance(self.buckets, tuple) or any(
                not isinstance(bucket, SeasonalBucket) for bucket in self.buckets):
            raise TypeError("buckets_must_be_tuple_of_SeasonalBucket")
        if len({(b.product, b.session_kind, b.slot_index, b.duration_seconds)
                for b in self.buckets}) != len(self.buckets):
            raise ValueError("bucket_keys_must_be_unique")
        if not isinstance(self.input_sha256, str) or len(self.input_sha256) != 64 \
                or any(char not in "0123456789abcdef" for char in self.input_sha256):
            raise ValueError("input_sha256_must_be_64_lowercase_hex")

    def content_projection(self) -> dict[str, object]:
        """Canonical public projection: every field except its own model_id."""
        return {
            "fit_start_nano": self.fit_start_nano,
            "fit_end_nano": self.fit_end_nano,
            "calendar_version": self.calendar_version,
            "buckets": [
                {
                    "product": bucket.product,
                    "session_kind": bucket.session_kind,
                    "slot_index": bucket.slot_index,
                    "duration_seconds": bucket.duration_seconds,
                    "median_volume": (None if bucket.median_volume is None
                                      else str(bucket.median_volume)),
                    "median_abs_return": bucket.median_abs_return,
                    "sample_days": bucket.sample_days,
                }
                for bucket in sorted(
                    self.buckets,
                    key=lambda b: (b.product, b.session_kind, b.slot_index,
                                   b.duration_seconds))
            ],
            "input_sha256": self.input_sha256,
        }

    def content_hash(self) -> str:
        """Bare 64-hex SHA-256 of the canonical final-content projection."""
        return hashlib.sha256(canonical_json_bytes(self.content_projection())).hexdigest()


_CALENDAR_CACHE: dict[tuple[str, str, int], "_ProductCalendar"] = {}


def _calendar_fingerprint(sessions: tuple[SessionWindow, ...]) -> str:
    parts = []
    for session in sessions:
        parts.append("|".join((
            session.session_id, session.product, session.trading_day.isoformat(),
            str(session.start_nano), str(session.end_nano), session.kind,
            session.calendar_version, session.evidence_status)))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def cached_product_calendar(sessions: tuple[SessionWindow, ...], product: str,
                             duration_seconds: int) -> "_ProductCalendar":
    """Content-keyed immutable reuse of one product/duration calendar.

    The calendar is a pure function of the frozen sessions; the cache key
    binds every identity field, so consumers always see exactly what a
    fresh construction would produce.
    """
    key = (_calendar_fingerprint(sessions), product, duration_seconds)
    calendar = _CALENDAR_CACHE.get(key)
    if calendar is None:
        calendar = _ProductCalendar(sessions, product, duration_seconds)
        _CALENDAR_CACHE[key] = calendar
    return calendar


def _check_bars(bars: tuple[MarketBar, ...]) -> None:
    if not isinstance(bars, tuple) or any(not isinstance(bar, MarketBar) for bar in bars):
        raise TypeError("bars_must_be_tuple_of_MarketBar")


def _check_sessions(sessions: tuple[SessionWindow, ...]) -> None:
    if not isinstance(sessions, tuple) or any(
            not isinstance(session, SessionWindow) for session in sessions):
        raise TypeError("sessions_must_be_tuple_of_SessionWindow")


def _abs_log_ratio(current: Decimal, previous: Decimal) -> float | None:
    """abs(ln(current/previous)) in float; None when not representable."""
    try:
        ratio = float(current / previous)
    except (OverflowError, ValueError, ZeroDivisionError):
        return None
    if not math.isfinite(ratio) or ratio <= 0:
        return None
    value = abs(math.log(ratio))
    return value if math.isfinite(value) else None


def _median_decimal(values: list[Decimal]) -> Decimal | None:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def _median_float(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _fit_input_binding(operator: str, fit_start: int, fit_end: int,
                       calendar_version: str, templates: dict[str, list[tuple[int, int]]],
                       candidates: list[MarketBar], selected: dict[str, str]) -> str:
    """Canonical preimage of the model's input_sha256 (005 §5)."""
    return canonical_json_bytes({
        "operator_version": operator,
        "fit_start_nano": fit_start,
        "fit_end_nano": fit_end,
        "calendar_version": calendar_version,
        "templates": {kind: [list(pair) for pair in template]
                      for kind, template in sorted(templates.items())},
        "candidates": [_bar_content(bar) for bar in
                       sorted(candidates, key=lambda b: (b.start_nano, b.bar_id))],
        "selected_contracts": {key: selected[key] for key in sorted(selected)},
    }).decode("utf-8")


def fit_seasonality(bars: tuple[MarketBar, ...], sessions: tuple[SessionWindow, ...],
                    fit_start_nano: int, fit_end_nano: int) -> SeasonalityModel:
    """Fit slot medians from the training window only.

    Candidates are filtered into the training window (start/end *and*
    availability) before duplicate detection; content-equal rows are
    idempotent, simultaneously visible conflicts and multi-contract
    contention on one product day slot are rejected.  ``previous_bar``
    context may come from bars that precede ``fit_start_nano`` — they
    never count as training-day samples.  Each bucket keeps at most one
    observation per trading day; a column's median exists only with at
    least 20 valid days for that column.
    """
    _check_bars(bars)
    _check_sessions(sessions)
    if isinstance(fit_start_nano, bool) or not isinstance(fit_start_nano, int):
        raise TypeError("fit_start_nano_must_be_int")
    if isinstance(fit_end_nano, bool) or not isinstance(fit_end_nano, int):
        raise TypeError("fit_end_nano_must_be_int")
    if fit_start_nano >= fit_end_nano:
        raise SeasonalityError("fit_start_must_precede_fit_end")
    if not sessions:
        raise SeasonalityError("calendar_unknown")

    products = {bar.product for bar in bars} or {
        session.product for session in sessions}
    if len(products) > 1:
        raise SeasonalityError("mixed_product_bars")
    product = next(iter(products))
    durations = {bar.duration_seconds for bar in bars if
                 fit_start_nano <= bar.start_nano and bar.end_nano <= fit_end_nano}
    if not durations:
        durations = {bar.duration_seconds for bar in bars} or {60}
    if len(durations) > 1:
        raise SeasonalityError("mixed_duration_bars")
    duration_seconds = next(iter(durations))
    calendar = cached_product_calendar(sessions, product, duration_seconds)
    if not calendar.sessions:
        raise SeasonalityError("calendar_unknown")
    if any(session.evidence_status == "UNKNOWN" for session in calendar.sessions):
        raise SeasonalityError("calendar_unknown")

    # PIT cutoff first (E03): a row joins only what was visible inside the
    # training window; pre-window rows remain eligible as return context.
    eligible = [
        bar for bar in bars
        if bar.complete
        and bar.end_nano <= fit_end_nano
        and bar.available_at_nano is not None
        and bar.available_at_nano <= fit_end_nano
    ]
    seen: dict[tuple[str, int, int], MarketBar] = {}
    for bar in sorted(eligible, key=lambda b: (b.start_nano, b.datetime_nano, b.bar_id)):
        key = (bar.contract_id, bar.duration_seconds, bar.datetime_nano)
        existing = seen.get(key)
        if existing is not None:
            if existing != bar:
                raise SeasonalityError("conflicting_duplicate_bar")
            continue
        seen[key] = bar
    candidates = tuple(seen[key] for key in sorted(seen, key=lambda k: (seen[k].start_nano, k)))

    calendar_versions = {session.calendar_version for session in calendar.sessions}
    if len(calendar_versions) > 1:
        raise SeasonalityError("mixed_calendar_versions")
    calendar_version = next(iter(calendar_versions))
    templates: dict[str, list[tuple[int, int]]] = {}
    for kind in ("DAY", "NIGHT"):
        template = calendar.template(kind)  # raises calendar_template_mismatch
        if template:
            templates[kind] = template

    by_slot_start: dict[int, MarketBar] = {}
    for bar in candidates:
        if bar.product != product or bar.duration_seconds != duration_seconds:
            continue
        address = calendar.address(bar)
        if address is None:
            continue
        existing = by_slot_start.get(address.start_nano)
        if existing is not None and existing != bar:
            raise SeasonalityError("conflicting_slot_observation")
        by_slot_start[address.start_nano] = bar

    samples: dict[tuple[str, str, int], dict] = {}
    selected_contracts: dict[str, str] = {}
    consumed_candidates: list[MarketBar] = []
    for bar in candidates:
        if bar.product != product or bar.duration_seconds != duration_seconds:
            continue
        if bar.start_nano < fit_start_nano:
            continue  # pre-window context only, never a training sample
        address = calendar.address(bar)
        if address is None:
            continue
        key = (bar.product, address.kind, address.slot_index)
        bucket = samples.setdefault(key, {"days": {}})
        trading_day = bar.trading_day if bar.trading_day is not None else address.trading_day
        previous_slot = calendar.previous_slot(address)
        previous: MarketBar | None = None
        if previous_slot is not None:
            previous = by_slot_start.get(previous_slot.start_nano)
            if previous is not None and previous.contract_id != bar.contract_id:
                previous = None  # returns never cross contracts
        entry = {
            "volume": bar.volume if bar.volume is not None else None,
            "abs_return": _pair_abs_return(bar, previous),
        }
        existing_day = bucket["days"].get(trading_day)
        if existing_day is not None:
            raise SeasonalityError("conflicting_slot_observation")
        bucket["days"][trading_day] = entry
        selected_contracts[f"{trading_day.isoformat()}|{address.kind}|{address.slot_index}"] = \
            bar.contract_id
        consumed_candidates.append(bar)

    buckets: list[SeasonalBucket] = []
    for key in sorted(samples):
        bucket_product, kind, slot_index = key
        days = samples[key]["days"]
        volumes = [sample["volume"] for sample in days.values()
                   if sample["volume"] is not None]
        abs_returns = [sample["abs_return"] for sample in days.values()
                       if sample["abs_return"] is not None]
        buckets.append(SeasonalBucket(
            product=bucket_product,
            session_kind=kind,
            slot_index=slot_index,
            duration_seconds=duration_seconds,
            median_volume=(_median_decimal(volumes)
                           if len(volumes) >= MINIMUM_SAMPLE_DAYS else None),
            median_abs_return=(_median_float(abs_returns)
                               if len(abs_returns) >= MINIMUM_SAMPLE_DAYS else None),
            sample_days=len(days),
        ))
    binding = _fit_input_binding(
        OPERATOR_VERSION, fit_start_nano, fit_end_nano, calendar_version,
        templates, list(candidates), selected_contracts)
    model = SeasonalityModel(
        model_id="pending",
        fit_start_nano=fit_start_nano,
        fit_end_nano=fit_end_nano,
        calendar_version=calendar_version,
        buckets=tuple(buckets),
        input_sha256=_text_hash(binding),
    )
    object.__setattr__(model, "model_id", f"seasonality:{model.content_hash()}")
    return model


def _pair_abs_return(bar: MarketBar, previous: MarketBar | None) -> float | None:
    """abs(ln(bar.close / previous.close)); None when not computable."""
    if previous is None:
        return None
    if bar.close is None or previous.close is None:
        return None
    if bar.close <= 0 or previous.close <= 0:
        return None
    return _abs_log_ratio(bar.close, previous.close)


def seasonal_values(model: SeasonalityModel, bar: MarketBar,
                    sessions: tuple[SessionWindow, ...],
                    decision_nano: int, *,
                    previous_bar: MarketBar | None) -> tuple[FactorValue, ...]:
    """The two standardized seasonal columns for one visible bar (Q02).

    The return column is ``abs(ln(bar.close/previous_bar.close))`` — the
    segment-opening gap enters through the explicit planned previous slot.
    A missing or not-yet-visible previous slot degrades only the return
    column; a wrong-identity previous bar is INVALID for that column.  A
    model whose training ends after the decision invalidates both
    columns; calendar evidence/version/template problems are shared.
    """
    if not isinstance(model, SeasonalityModel):
        raise TypeError("model_must_be_SeasonalityModel")
    if not isinstance(bar, MarketBar):
        raise TypeError("bar_must_be_MarketBar")
    _check_sessions(sessions)
    if isinstance(decision_nano, bool) or not isinstance(decision_nano, int):
        raise TypeError("decision_nano_must_be_int")
    if not bar.complete or bar.end_nano > decision_nano \
            or bar.available_at_nano is None or bar.available_at_nano > decision_nano:
        raise SeasonalityError("bar_not_visible_at_decision_nano")

    durations = {bucket.duration_seconds for bucket in model.buckets}
    if durations and bar.duration_seconds not in durations:
        raise SeasonalityError("duration_mismatch_between_model_and_bar")

    abs_return_key, volume_key = seasonal_factor_keys(bar.duration_seconds)
    model_binding = "|".join((
        model.model_id, model.content_hash(), model.input_sha256,
        model.calendar_version, str(model.fit_start_nano), str(model.fit_end_nano)))

    def row(factor_key: str, value: Decimal | float | None, status: str,
            reason: str, available_at: int | None,
            extra_binding: str = "") -> FactorValue:
        input_hash = _text_hash(f"{model_binding}|{_bar_content(bar)}"
                                f"|{extra_binding or 'no-previous'}")
        reasons = () if status == "READY" else (reason,)
        return FactorValue(
            factor_key=factor_key, value=value, available_at_nano=available_at,
            quality=DataQuality(status=status, reasons=reasons,
                                temporal_basis=bar.quality.temporal_basis,
                                input_hash=input_hash))

    # Shared gates: both columns unavailable.
    if model.fit_end_nano > decision_nano:
        return (row(volume_key, None, "INVALID", "model_fit_after_decision",
                    None),
                row(abs_return_key, None, "INVALID", "model_fit_after_decision",
                    None))
    training_sessions = cached_product_calendar(
        sessions, bar.product, bar.duration_seconds).calendars_between(
            model.fit_start_nano, model.fit_end_nano)
    if not training_sessions:
        return (row(volume_key, None, "MISSING", "calendar_evidence_missing", None),
                row(abs_return_key, None, "MISSING", "calendar_evidence_missing", None))
    calendar = cached_product_calendar(sessions, bar.product, bar.duration_seconds)
    session = calendar.session(bar.session_id)
    if session is None:
        return (row(volume_key, None, "MISSING", "slot_mapping_unavailable", None),
                row(abs_return_key, None, "MISSING", "slot_mapping_unavailable", None))
    if session.calendar_version != model.calendar_version:
        return (row(volume_key, None, "INVALID", "calendar_version_mismatch", None),
                row(abs_return_key, None, "INVALID", "calendar_version_mismatch", None))
    address = calendar.address(bar)
    if address is None:
        return (row(volume_key, None, "MISSING", "slot_mapping_unavailable", None),
                row(abs_return_key, None, "MISSING", "slot_mapping_unavailable", None))
    try:
        current_template = calendar.day_template(address.kind, address.trading_day)
        training_calendar = cached_product_calendar(training_sessions, bar.product,
                                                   bar.duration_seconds)
        training_template = training_calendar.template(address.kind)
    except SeasonalityError:
        return (row(volume_key, None, "INVALID", "calendar_template_mismatch", None),
                row(abs_return_key, None, "INVALID", "calendar_template_mismatch", None))
    if current_template and training_template and current_template != training_template:
        return (row(volume_key, None, "INVALID", "calendar_template_mismatch", None),
                row(abs_return_key, None, "INVALID", "calendar_template_mismatch", None))

    bucket = next((b for b in model.buckets
                   if b.product == bar.product and b.session_kind == address.kind
                   and b.slot_index == address.slot_index
                   and b.duration_seconds == bar.duration_seconds), None)

    # Volume column: independent of any previous-bar evidence.
    volume_available = max(bar.available_at_nano, model.fit_end_nano)
    if bucket is None:
        volume_value = row(volume_key, None, "MISSING", "bucket_missing",
                           volume_available)
    elif bucket.median_volume is None:
        volume_value = row(volume_key, None, "MISSING", "insufficient_seasonal_samples",
                           volume_available)
    elif bucket.median_volume == 0:
        volume_value = row(volume_key, None, "INVALID", "zero_denominator",
                           volume_available)
    elif bar.volume is None:
        volume_value = row(volume_key, None, "MISSING", "missing_source_value",
                           volume_available)
    else:
        volume_value = row(
            volume_key, bar.volume / bucket.median_volume - Decimal(1),
            "READY", "", volume_available)
        volume_value = _strip_ready_reason(volume_value)

    # Return column: explicit planned previous slot (Q02).
    planned_previous = calendar.previous_slot(address)
    if previous_bar is None:
        return_value = row(abs_return_key, None, "MISSING", "previous_bar_missing",
                           volume_available)
    else:
        previous_address = calendar.address(previous_bar)
        valid_identity = (
            previous_bar.contract_id == bar.contract_id
            and previous_bar.product == bar.product
            and previous_bar.duration_seconds == bar.duration_seconds
            and previous_bar.complete
            and previous_address is not None
            and planned_previous is not None
            and previous_address.start_nano == planned_previous.start_nano)
        if not valid_identity:
            return_value = row(abs_return_key, None, "INVALID",
                               "previous_bar_invalid", volume_available,
                               extra_binding=_bar_content(previous_bar))
        elif (previous_bar.available_at_nano is None
              or previous_bar.available_at_nano > decision_nano):
            return_value = row(abs_return_key, None, "MISSING",
                               "previous_bar_missing", volume_available,
                               extra_binding=_bar_content(previous_bar))
        elif bar.close is None or previous_bar.close is None:
            return_value = row(abs_return_key, None, "MISSING", "missing_source_value",
                               volume_available, extra_binding=_bar_content(previous_bar))
        elif bar.close <= 0 or previous_bar.close <= 0:
            return_value = row(abs_return_key, None, "INVALID", "non_positive_price",
                               volume_available, extra_binding=_bar_content(previous_bar))
        else:
            abs_return = _abs_log_ratio(bar.close, previous_bar.close)
            available = max(volume_available,
                            previous_bar.available_at_nano
                            if previous_bar.available_at_nano is not None else 0)
            if abs_return is None:
                return_value = row(abs_return_key, None, "INVALID", "non_finite_value",
                                   available, extra_binding=_bar_content(previous_bar))
            elif bucket is None or bucket.median_abs_return is None:
                return_value = row(abs_return_key, None, "MISSING",
                                   "insufficient_seasonal_samples", available,
                                   extra_binding=_bar_content(previous_bar))
            elif bucket.median_abs_return == 0.0:
                return_value = row(abs_return_key, None, "INVALID", "zero_denominator",
                                   available, extra_binding=_bar_content(previous_bar))
            else:
                return_value = row(
                    abs_return_key, abs_return / bucket.median_abs_return,
                    "READY", "", available, extra_binding=_bar_content(previous_bar))
                return_value = _strip_ready_reason(return_value)
    return tuple(sorted((volume_value, return_value), key=lambda value: value.factor_key))


def _strip_ready_reason(value: FactorValue) -> FactorValue:
    if not value.quality.reasons:
        return value
    quality = DataQuality(status=value.quality.status, reasons=(),
                          temporal_basis=value.quality.temporal_basis,
                          input_hash=value.quality.input_hash)
    return FactorValue(factor_key=value.factor_key, value=value.value,
                       available_at_nano=value.available_at_nano, quality=quality)
