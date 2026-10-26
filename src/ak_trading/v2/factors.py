"""Causal factor operators and feature-snapshot construction (M2 §4.2).

Revision r3 implements 变更申请-005 §3/§4 on top of the precheck repairs:

- **Required calendar** (E02/005 §3): ``compute_factor`` takes a required
  ``sessions`` keyword — the complete frozen plan covering the needed
  history window, previous slots and the current trading day.  The
  computation anchor is the last *planned complete slot* whose completion
  time has passed the decision; a missing anchor observation is
  ``MISSING/expected_slot_missing`` and never falls back to an earlier
  bar.  Empty calendars, uncovered dates, ``UNKNOWN`` evidence or bars
  outside the declared plan yield ``MISSING/calendar_unknown``.  Missing
  scheduled slots (session head, session tail or a whole declared trading
  day) interrupt windows and reset the Wilder recursion; explicit
  closures have no slots and stay legal ``INCLUDE_GAP`` boundaries.
- **PIT before conflict** (E03): bars are point-in-time filtered first;
  duplicate conflict detection runs only among simultaneously visible
  rows, so a future revision can neither alter nor crash a historical
  recomputation.
- **Session interval identity** (E05): the snapshot anchor bar must lie
  inside (and slot-aligned with) its declared session.
- **Content-bound identities** (E07/005 §4): ``input_hash`` binds the
  canonical *content* of the actually consumed bars, the full spec
  binding, the calendar fragment covering the window and — for the
  seasonal columns — the previous-slot fact and the model content hash.
  The Wilder hash covers every recursion input since the last reset.
- **Registered baselines** (E08): baseline specs dispatch to the v2
  baseline calculators, so every registered same-duration spec keeps a
  row inside snapshots.
- **Seasonal columns** (005 §2): ``build_feature_snapshot`` selects and
  validates the calendar-planned ``previous_bar`` for the last bar and
  passes it to ``seasonal_values``.

Numeric discipline (总纲 §4): price, volume/OI and path arithmetic use
``Decimal``; log returns, standard deviations and other statistics use
finite ``float``.  Null values always carry a distinct reason — missing
source value (MISSING), insufficient history (WARMUP), zero variance or a
zero denominator (INVALID); VOL001's explicit zero-risk observation on a
zero-variance window is reported DEGRADED so it can never certify a
risk-free qualification.
"""
from __future__ import annotations

import hashlib
import math
import statistics
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ak_trading.v2.artifacts import stable_id
from ak_trading.v2.contracts import (
    BAR_DURATIONS,
    DataQuality,
    FactorSpec,
    FactorValue,
    FeatureSnapshot,
    MarketBar,
    SessionWindow,
    TradingRule,
)
from ak_trading.v2.factor_specs import BASELINE_NAMES, DAILY_DURATION
from ak_trading.v2.schemas import META_TICK_FACTOR_KEY, canonical_json_bytes, encode_record
from ak_trading.v2.seasonality import (
    MINIMUM_SAMPLE_DAYS,
    SeasonalityModel,
    _ProductCalendar,
    seasonal_values,
)

META_MULTIPLIER_FACTOR_KEY = "META:multiplier:v2.1.0"
COST_UNKNOWN_REASON = "COST_UNKNOWN"
NANO_PER_SECOND = 1_000_000_000


class FactorsError(ValueError):
    """Fixed-reason factor/snapshot violation."""


# ---------------------------------------------------------------------------
# Canonical content hashing (E07): bar ids are slot identities, not content
# identities — every input hash binds the canonical field content of the
# consumed bars plus the spec and calendar bindings.
# ---------------------------------------------------------------------------

_BAR_CONTENT_FIELDS = (
    "bar_id", "contract_id", "raw_symbol", "product", "duration_seconds",
    "datetime_nano", "start_nano", "end_nano", "trading_day", "session_id",
    "available_at_nano", "imported_at_nano", "open", "high", "low", "close",
    "volume", "open_oi", "close_oi", "complete", "expected_minutes",
    "actual_minutes", "source",
)


# ---------------------------------------------------------------------------
# Bounded content-bound value memos (T7 CPU fix, byte-identical outputs).
#
# MarketBar/SessionWindow/FactorSpec are frozen dataclasses, so a held
# reference's identity implies immutable content: each memo below keys on
# ``id(record)`` while storing the record itself in the value, so an address
# can never be reused by a different object while the entry lives.  Every
# memo is bounded (clear-on-full, like the pre-existing prefix cache) and
# pure — a miss recomputes exactly what a fresh call would produce.
# ---------------------------------------------------------------------------

_BAR_CONTENT_CACHE: dict[int, tuple[MarketBar, str]] = {}
_BAR_CONTENT_CACHE_LIMIT = 16384


def _bar_content(bar: MarketBar) -> str:
    cached = _BAR_CONTENT_CACHE.get(id(bar))
    if cached is not None and cached[0] is bar:
        return cached[1]
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
    text = "|".join(parts)
    if len(_BAR_CONTENT_CACHE) >= _BAR_CONTENT_CACHE_LIMIT:
        _BAR_CONTENT_CACHE.clear()
    _BAR_CONTENT_CACHE[id(bar)] = (bar, text)
    return text


_SESSION_BINDING_CACHE: dict[int, tuple[SessionWindow, str]] = {}
_SESSION_BINDING_CACHE_LIMIT = 2048


def _session_binding(session: SessionWindow) -> str:
    cached = _SESSION_BINDING_CACHE.get(id(session))
    if cached is not None and cached[0] is session:
        return cached[1]
    text = "|".join((
        "session", session.session_id, session.product,
        session.trading_day.isoformat(), str(session.start_nano),
        str(session.end_nano), session.kind, session.calendar_version,
        session.evidence_status))
    if len(_SESSION_BINDING_CACHE) >= _SESSION_BINDING_CACHE_LIMIT:
        _SESSION_BINDING_CACHE.clear()
    _SESSION_BINDING_CACHE[id(session)] = (session, text)
    return text


_SPEC_BINDING_CACHE: dict[int, tuple[FactorSpec, str]] = {}
_SPEC_BINDING_CACHE_LIMIT = 512


def _spec_binding(spec: FactorSpec) -> str:
    cached = _SPEC_BINDING_CACHE.get(id(spec))
    if cached is not None and cached[0] is spec:
        return cached[1]
    text = "|".join((
        "spec", spec.factor_key, spec.operator_version, spec.parameter_version,
        spec.source_sha256, str(spec.duration_seconds), str(spec.lookback),
        canonical_json_bytes(dict(spec.parameters)).decode("utf-8").rstrip("\n"),
        ",".join(spec.required_fields),
    ))
    if len(_SPEC_BINDING_CACHE) >= _SPEC_BINDING_CACHE_LIMIT:
        _SPEC_BINDING_CACHE.clear()
    _SPEC_BINDING_CACHE[id(spec)] = (spec, text)
    return text


# Whole-spec-set binding (T7 CPU fix): the frozen spec tuple object is held
# by reference, so its identity implies immutable content; a fresh tuple of
# equal specs re-derives the identical binding on a miss.
_SPECS_TUPLE_BINDING_CACHE: dict[int, tuple[tuple, str]] = {}
_SPECS_TUPLE_BINDING_LIMIT = 64


def _specs_tuple_binding(specs: tuple[FactorSpec, ...]) -> str:
    cached = _SPECS_TUPLE_BINDING_CACHE.get(id(specs))
    if cached is not None and cached[0] is specs:
        return cached[1]
    binding = _text_hash("|".join(sorted(_spec_binding(spec) for spec in specs))
                         or "no-specs")
    if len(_SPECS_TUPLE_BINDING_CACHE) >= _SPECS_TUPLE_BINDING_LIMIT:
        _SPECS_TUPLE_BINDING_CACHE.clear()
    _SPECS_TUPLE_BINDING_CACHE[id(specs)] = (specs, binding)
    return binding


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _content_hash(bars: tuple[MarketBar, ...]) -> str:
    if not bars:
        return _text_hash("no-inputs")
    rolling = _text_hash(_bar_content(bars[0]))
    for bar in bars[1:]:
        rolling = _text_hash(rolling + "|" + _bar_content(bar))
    return rolling


def _spec_binding(spec: FactorSpec) -> str:
    return "|".join((
        "spec", spec.factor_key, spec.operator_version, spec.parameter_version,
        spec.source_sha256, str(spec.duration_seconds), str(spec.lookback),
        canonical_json_bytes(dict(spec.parameters)).decode("utf-8").rstrip("\n"),
        ",".join(spec.required_fields),
    ))


def _session_binding(session: SessionWindow) -> str:
    return "|".join((
        "session", session.session_id, session.product,
        session.trading_day.isoformat(), str(session.start_nano),
        str(session.end_nano), session.kind, session.calendar_version,
        session.evidence_status))


def _input_hash(content: str, spec: FactorSpec, calendar: str = "") -> str:
    return _text_hash(f"inputs|{content}|{_spec_binding(spec)}|{calendar}")


# ---------------------------------------------------------------------------
# Point-in-time filtering before duplicate conflict detection (E03)
# ---------------------------------------------------------------------------

def _check_bars_container(bars: object) -> None:
    if not isinstance(bars, tuple) or any(not isinstance(bar, MarketBar) for bar in bars):
        raise TypeError("bars_must_be_tuple_of_MarketBar")


def _visible_candidates(bars: tuple[MarketBar, ...], decision_nano: int) -> list[MarketBar]:
    return [
        bar for bar in bars
        if bar.complete
        and bar.end_nano <= decision_nano
        and bar.available_at_nano is not None
        and bar.available_at_nano <= decision_nano
    ]


def _deduplicate_visible(candidates: list[MarketBar]) -> tuple[MarketBar, ...]:
    seen: dict[tuple[str, int, int], MarketBar] = {}
    for bar in sorted(candidates, key=lambda b: (b.start_nano, b.datetime_nano, b.bar_id)):
        key = (bar.contract_id, bar.duration_seconds, bar.datetime_nano)
        existing = seen.get(key)
        if existing is not None:
            if existing != bar:
                raise FactorsError("conflicting_duplicate_bar")
            continue
        seen.setdefault(key, bar)
    return tuple(seen[key] for key in sorted(seen, key=lambda k: (seen[k].start_nano, k)))


class _PrefixState:
    """One cached PIT-deduplicated prefix (cross-call, content-validated).

    Reuse is only ever allowed after the *whole* cached head validates
    against the current candidates by tuple equality — frozen bars make
    element equality imply identical content — never from endpoint, length
    or id alone (D2).  ``max_datetime`` bounds the dedup keys consumed so
    far: a tail carrying an earlier-or-equal ``datetime_nano`` could collide
    with a head key, so it forces a full recompute.  ``covers`` (per grid
    object), ``basis`` (temporal-basis counts) and ``max_available``
    accumulate prefix-level scans for consumers that receive exactly
    ``prefix``, so those scans shrink to the freshly appended tail.
    """

    __slots__ = ("prefix", "decision_nano", "chain", "max_datetime",
                 "covers", "basis", "max_available")

    def __init__(self, prefix, decision_nano, chain, max_datetime,
                 covers=None, basis=(0, 0, 0), max_available=None) -> None:
        self.prefix = prefix
        self.decision_nano = decision_nano
        self.chain = chain
        self.max_datetime = max_datetime
        self.covers = covers if covers is not None else {}
        self.basis = basis
        self.max_available = max_available


_PREFIX_CACHE: dict[tuple[str, int], dict[str, "_PrefixState"]] = {}
_PREFIX_CACHE_LIMIT = 8
_LAST_PREFIX_STATE: "_PrefixState | None" = None


def _prefix_state_for(visible: tuple[MarketBar, ...]) -> "_PrefixState | None":
    """The state producing exactly this visible tuple, or None."""
    state = _LAST_PREFIX_STATE
    if state is not None and state.prefix is visible:
        return state
    return None


def _basis_counts(bars, counts=(0, 0, 0)) -> tuple[int, int, int]:
    total, observed, reconstructed = counts
    for bar in bars:
        basis = bar.quality.temporal_basis
        if basis == "observed":
            observed += 1
        elif basis == "reconstructed":
            reconstructed += 1
        total += 1
    return total, observed, reconstructed


def _basis_status(counts: tuple[int, int, int]) -> str:
    total, observed, reconstructed = counts
    if observed == total:  # mirrors all(): an empty set counts as observed
        return "observed"
    if reconstructed:
        return "reconstructed"
    return "unknown"


def _max_available(bars, current=None):
    for bar in bars:
        value = bar.available_at_nano
        if value is not None and (current is None or value > current):
            current = value
    return current


def _rolling_chain(previous_hash: str | None, bar: MarketBar) -> str:
    content = _bar_content(bar)
    if previous_hash is None:
        return _text_hash(content)
    return _text_hash(previous_hash + "|" + content)


def _full_chain(bars: tuple[MarketBar, ...]) -> str:
    if not bars:
        return _text_hash("no-inputs")
    chain = None
    for bar in bars:
        chain = _rolling_chain(chain, bar)
    return chain


def _visible(bars: tuple[MarketBar, ...], decision_nano: int) -> tuple[MarketBar, ...]:
    """PIT-deduplicated visible prefix with an append-extension fast path.

    A cached state may only extend the current call when its complete
    deduplicated head equals the leading candidates (tuple equality over
    frozen bars) and every appended tail bar carries a strictly later
    ``datetime_nano`` than any key consumed by the head, so neither revised
    history nor cross-boundary duplicate keys can ride a stale cache; any
    mismatch falls back to the full computation.  Outputs are byte-identical
    by construction (same dedup algorithm over the same candidate set).
    """
    global _LAST_PREFIX_STATE
    candidates = _visible_candidates(list(bars), decision_nano)
    if not candidates:
        _LAST_PREFIX_STATE = None
        return ()
    key = (candidates[0].contract_id, candidates[0].duration_seconds)
    bucket = _PREFIX_CACHE.setdefault(key, {})
    best: _PrefixState | None = None
    for state in reversed(bucket.values()):
        prefix = state.prefix
        length = len(prefix)
        if length <= 16 or length > len(candidates):
            continue
        if tuple(candidates[:length]) == prefix:
            best = state
            break
    tail: list[MarketBar] = []
    if best is not None:
        tail = candidates[len(best.prefix):]
        if tail:
            head_max = best.max_datetime
            if head_max is None:
                best = None
            else:
                for bar in tail:
                    datetime_nano = bar.datetime_nano
                    if datetime_nano is None or datetime_nano <= head_max:
                        best = None
                        break
    if best is not None:
        cached_prefix = best.prefix
        deduped_tail = _deduplicate_visible(list(tail)) if tail else ()
        merged = _merge_prefixes(cached_prefix, deduped_tail)
        # F01 repair (t7-cpu-owner-002): the covers verification is
        # INDEX-BOUND over the head's tuple layout.  When the merge takes
        # its overlap-safety branch the merged tuple is re-sorted, so a
        # mid-inserted bar can land below the inherited verified index and
        # would never be coverage-checked.  Any overlap merge therefore
        # discards the inherited per-grid verified counts (the next
        # ``_prepare`` runs the original full scan); the order-independent
        # aggregates (basis counts, availability max, max datetime, chain)
        # stay inherited.  A pure append keeps head indices and may inherit.
        overlap_reordered = bool(deduped_tail) and bool(cached_prefix) and \
            deduped_tail[0].start_nano < cached_prefix[-1].start_nano
        chain = best.chain
        for bar in deduped_tail:
            chain = _rolling_chain(chain, bar)
        state = _PrefixState(
            merged, decision_nano, chain,
            _max_datetime(deduped_tail, best.max_datetime),
            {} if overlap_reordered else dict(best.covers),
            _basis_counts(deduped_tail, best.basis),
            _max_available(deduped_tail, best.max_available))
        if len(bucket) >= _PREFIX_CACHE_LIMIT:
            bucket.clear()
        bucket[str(decision_nano)] = state
        _LAST_PREFIX_STATE = state
        return merged
    result = _deduplicate_visible(candidates)
    state = _PrefixState(
        result, decision_nano, _full_chain(result),
        _max_datetime(result), {}, _basis_counts(result), _max_available(result))
    if len(bucket) >= _PREFIX_CACHE_LIMIT:
        bucket.clear()
    bucket[str(decision_nano)] = state
    _LAST_PREFIX_STATE = state
    return result


def _max_datetime(bars, current=None):
    for bar in bars:
        value = bar.datetime_nano
        if value is not None and (current is None or value > current):
            current = value
    return current


def _prefix_chain_hash(bars: tuple[MarketBar, ...]) -> str:
    """Full-prefix rolling content hash, reusing validated cache state.

    A state's chain is returned only when its whole prefix equals ``bars``
    (frozen-bar tuple equality); otherwise the direct chain runs — which is
    byte-identical by construction.
    """
    if not bars:
        return _text_hash("no-inputs")
    key = (bars[0].contract_id, bars[0].duration_seconds)
    for state in reversed(_PREFIX_CACHE.get(key, {}).values()):
        if state.prefix == bars:
            return state.chain
    return _full_chain(bars)


def _merge_prefixes(head: tuple[MarketBar, ...],
                    tail: tuple[MarketBar, ...]) -> tuple[MarketBar, ...]:
    """Merge a deduplicated head with a deduplicated strictly-later tail.

    The tail's keys cannot collide with the head's (they carry strictly
    later start times); conflicts within the tail were already rejected
    by its own dedup pass.
    """
    if not tail:
        return head
    if head and tail[0].start_nano < head[-1].start_nano:
        # Overlap safety: fall back to a full merge.
        combined: dict[tuple[str, int, int], MarketBar] = {}
        for bar in head + tail:
            key = (bar.contract_id, bar.duration_seconds, bar.datetime_nano)
            existing = combined.get(key)
            if existing is not None and existing != bar:
                raise FactorsError("conflicting_duplicate_bar")
            combined.setdefault(key, bar)
        return tuple(combined[key] for key in
                     sorted(combined, key=lambda k: (combined[k].start_nano, k)))
    return head + tail


def _single_contract(bars: tuple[MarketBar, ...]) -> None:
    if len({bar.contract_id for bar in bars}) > 1:
        raise FactorsError("mixed_contract_bars")


def _single_duration(bars: tuple[MarketBar, ...]) -> int | None:
    durations = {bar.duration_seconds for bar in bars}
    if len(durations) > 1:
        raise FactorsError("mixed_duration_bars")
    return next(iter(durations)) if durations else None


# ---------------------------------------------------------------------------
# Planned-slot factor grid (005 §3): minute slots per session fragment and
# one slot per declared trading day for daily bars.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _PlannedSlot:
    start_nano: int
    end_nano: int
    trading_day: date


class _FactorGrid:
    """Planned grid of one product/duration from the frozen calendar.

    Immutable after construction and safely shareable across calls: the
    module-level content-keyed cache below reuses one grid per distinct
    calendar/product/duration so a 24-spec snapshot no longer rebuilds the
    full planned-slot table per spec.  Grid outputs depend only on the
    frozen calendar content, never on bars or the decision.
    """

    def __init__(self, sessions: tuple[SessionWindow, ...], product: str,
                 duration_seconds: int) -> None:
        self.product = product
        self.duration_seconds = duration_seconds
        self.sessions = tuple(sorted(
            (s for s in sessions if s.product == product), key=lambda s: s.start_nano))
        self.calendar_unknown = (not self.sessions) or any(
            session.evidence_status == "UNKNOWN" for session in self.sessions)
        if duration_seconds == DAILY_DURATION:
            days: dict[date, tuple[int, int]] = {}
            for session in self.sessions:
                first, last = days.get(session.trading_day, (session.start_nano,
                                                             session.end_nano))
                days[session.trading_day] = (min(first, session.start_nano),
                                             max(last, session.end_nano))
            self.slots = tuple(
                _PlannedSlot(start_nano=days[day][0], end_nano=days[day][1],
                             trading_day=day)
                for day in sorted(days, key=lambda d: days[d][0]))
            self._slot_by_day = {slot.trading_day: slot for slot in self.slots}
            self._slot_by_start = {slot.trading_day: slot for slot in self.slots}
        else:
            slot_nano = duration_seconds * NANO_PER_SECOND
            slots: list[_PlannedSlot] = []
            for session in self.sessions:
                count = (session.end_nano - session.start_nano) // slot_nano
                for index in range(count):
                    slots.append(_PlannedSlot(
                        start_nano=session.start_nano + index * slot_nano,
                        end_nano=session.start_nano + (index + 1) * slot_nano,
                        trading_day=session.trading_day))
            slots.sort(key=lambda slot: slot.start_nano)
            self.slots = tuple(slots)
            self._slot_by_start = {slot.start_nano: slot for slot in self.slots}
            self._slot_by_day = {}
        # Ascending completion times make the anchor a bisect, and the
        # start->position map makes planned-window lookup O(1).
        self._slot_ends = [slot.end_nano for slot in self.slots]
        self._slot_starts = [slot.start_nano for slot in self.slots]
        self._position_by_start = {start: index
                                   for index, start in enumerate(self._slot_starts)}
        self._session_starts = [session.start_nano for session in self.sessions]
        self._max_session_span = max(
            (session.end_nano - session.start_nano for session in self.sessions),
            default=0)

    def covers(self, bar: MarketBar) -> bool:
        """A visible bar belongs to the plan (its slot must exist)."""
        if self.duration_seconds == DAILY_DURATION:
            return bar.trading_day is not None and bar.trading_day in self._slot_by_day
        return bar.start_nano in self._slot_by_start

    def anchor(self, decision_nano: int) -> _PlannedSlot | None:
        """The last planned complete slot finishing at or before decision."""
        index = bisect_right(self._slot_ends, decision_nano)
        return self.slots[index - 1] if index > 0 else None

    def anchor_bar(self, slot: _PlannedSlot,
                   bars: tuple[MarketBar, ...]) -> MarketBar | None:
        if self.duration_seconds == DAILY_DURATION:
            return next((bar for bar in reversed(bars)
                         if bar.trading_day == slot.trading_day), None)
        return next((bar for bar in reversed(bars)
                     if bar.start_nano == slot.start_nano), None)

    def window_start(self, slot: _PlannedSlot, lookback: int) -> int | None:
        """Start of the planned window of ``lookback`` slots ending at slot."""
        position = self._position_by_start.get(slot.start_nano)
        if position is None:
            return None
        first = max(0, position - max(0, lookback - 1))
        return self.slots[first].start_nano

    def missing_slot_between(self, t1: int, t2: int) -> bool | None:
        """True/False when a declared session overlaps the gap, else None."""
        if t2 <= t1:
            return False
        overlapping = [s for s in self.sessions if s.start_nano < t2 and s.end_nano > t1]
        if not overlapping:
            return None
        slot_nano = self.duration_seconds * NANO_PER_SECOND
        for session in overlapping:
            offset = max(0, -((session.start_nano - t1) // slot_nano))
            candidate = session.start_nano + offset * slot_nano
            if t1 <= candidate < t2 and candidate < session.end_nano:
                return True
        return False

    def declared_day_between(self, first: date, second: date) -> bool | None:
        if first not in self._slot_by_day or second not in self._slot_by_day:
            return None
        days = [slot.trading_day for slot in self.slots]
        low, high = sorted((days.index(first), days.index(second)))
        return any(low < index < high for index in range(len(days)))

    def binding(self, window_start: int | None, anchor_end: int) -> str:
        """Calendar fragment covering the consumed window (E07)."""
        if self.calendar_unknown or window_start is None:
            relevant: list[SessionWindow] = []
        else:
            # Sessions are sorted by start and each ends at most
            # ``_max_session_span`` after its start, so a session starting
            # before ``window_start - span`` cannot reach past
            # ``window_start``: the bisected slice covers exactly the
            # original full filter, in the same order.
            lo = bisect_left(self._session_starts,
                             window_start - self._max_session_span)
            hi = bisect_right(self._session_starts, anchor_end - 1)
            relevant = [session for session in self.sessions[lo:hi]
                        if session.end_nano > window_start]
        return _text_hash("|".join(_session_binding(session)
                                   for session in relevant) or "calendar-unknown")


def _hole_between(previous: MarketBar, current: MarketBar,
                  grid: _FactorGrid) -> bool:
    if current.start_nano < previous.end_nano:
        return True  # overlapping spans can never join a window
    if current.start_nano == previous.end_nano:
        return False
    if grid.duration_seconds == DAILY_DURATION:
        if previous.trading_day is None or current.trading_day is None:
            return True
        verdict = grid.declared_day_between(previous.trading_day, current.trading_day)
        if verdict is not None:
            return verdict
        return True
    verdict = grid.missing_slot_between(previous.end_nano, current.start_nano)
    if verdict is not None:
        return verdict
    return False


def _grid_hole(grid: _FactorGrid):
    def hole(previous: MarketBar, current: MarketBar) -> bool:
        return _hole_between(previous, current, grid)
    return hole


def _no_hole(previous: MarketBar, current: MarketBar) -> bool:
    return False


@dataclass(frozen=True)
class _AnchorContext:
    status: str  # ok | calendar_unknown | warmup | expected_slot_missing
    reason: str
    grid: _FactorGrid | None
    anchor: _PlannedSlot | None
    bars: tuple[MarketBar, ...]


# ---------------------------------------------------------------------------
# Content-keyed immutable reuse (performance fix, byte-identical outputs).
# Calendars are few and frozen; a 24-spec snapshot used to rebuild the full
# planned-slot table (150k+ slots on a year of minute sessions) once per
# spec and re-run PIT deduplication per spec.  The caches below key purely
# on calendar *content* — the grids themselves are never mutated — so every
# consumer sees exactly the values a fresh construction would produce.
# ---------------------------------------------------------------------------

_GRID_CACHE: dict[tuple[str, str, int], _FactorGrid] = {}
_GRID_CACHE_LIMIT = 64

# One bundle per frozen sessions tuple (held by reference): the calendar
# fingerprint (identity order), the full sorted calendar binding and the
# session-id map, so per-snapshot binding work stops re-joining hundreds
# of session strings.  Bounded; a miss rebuilds byte-identical values.
_SESSIONS_BUNDLE_CACHE: dict[int, tuple] = {}
_SESSIONS_BUNDLE_LIMIT = 16


def _sessions_bundle(sessions: tuple[SessionWindow, ...]) -> tuple:
    cached = _SESSIONS_BUNDLE_CACHE.get(id(sessions))
    if cached is not None and cached[0] is sessions:
        return cached[1], cached[2], cached[3]
    fingerprint = _text_hash("|".join(_session_binding(session)
                                      for session in sessions) or "no-sessions")
    calendar_binding = _text_hash("|".join(
        sorted(_session_binding(session) for session in sessions)) or "no-sessions")
    by_id = {session.session_id: session for session in sessions}
    if len(_SESSIONS_BUNDLE_CACHE) >= _SESSIONS_BUNDLE_LIMIT:
        _SESSIONS_BUNDLE_CACHE.clear()
    _SESSIONS_BUNDLE_CACHE[id(sessions)] = (sessions, fingerprint,
                                            calendar_binding, by_id)
    return fingerprint, calendar_binding, by_id


def _sessions_fingerprint(sessions: tuple[SessionWindow, ...]) -> str:
    return _sessions_bundle(sessions)[0]


def _cached_grid(sessions: tuple[SessionWindow, ...], product: str,
                 duration_seconds: int) -> _FactorGrid:
    key = (_sessions_fingerprint(sessions), product, duration_seconds)
    grid = _GRID_CACHE.get(key)
    if grid is None:
        grid = _FactorGrid(sessions, product, duration_seconds)
        if len(_GRID_CACHE) >= _GRID_CACHE_LIMIT:
            _GRID_CACHE.clear()
        _GRID_CACHE[key] = grid
    return grid


# id-memoised wrapper around the seasonality content-keyed calendar cache:
# the underlying object is exactly what ``cached_product_calendar`` returns;
# this only avoids re-hashing every session on every call.
_PRODUCT_CALENDAR_CACHE: dict[int, tuple] = {}
_PRODUCT_CALENDAR_LIMIT = 32


def _cached_product_calendar(sessions: tuple[SessionWindow, ...], product: str,
                             duration_seconds: int):
    cached = _PRODUCT_CALENDAR_CACHE.get(id(sessions))
    if cached is None or cached[0] is not sessions:
        cached = (sessions, {})
        if len(_PRODUCT_CALENDAR_CACHE) >= _PRODUCT_CALENDAR_LIMIT:
            _PRODUCT_CALENDAR_CACHE.clear()
        _PRODUCT_CALENDAR_CACHE[id(sessions)] = cached
    per_duration = cached[1]
    calendar = per_duration.get((product, duration_seconds))
    if calendar is None:
        from ak_trading.v2.seasonality import cached_product_calendar
        calendar = cached_product_calendar(sessions, product, duration_seconds)
        per_duration[(product, duration_seconds)] = calendar
    return calendar


@dataclass
class _PreparedContext:
    """One PIT-deduplicated bar prefix plus its planned anchor and per-call
    memoisation (bar content strings, baseline rows, Wilder walks).  Scoped
    to a single public call; the id()-keyed memos are safe because the bar
    objects stay alive inside ``bars`` for the whole call.  Bar content
    strings themselves live in the bounded global frozen-record memo."""

    bars: tuple[MarketBar, ...]
    status: str
    reason: str
    grid: _FactorGrid | None
    anchor: _PlannedSlot | None
    decision_nano: int
    sessions: tuple[SessionWindow, ...]
    _contents: dict[int, str] | None = None
    _baseline_rows: dict | None = None
    _wilder: dict[int, tuple] | None = None

    def content(self, bar: MarketBar) -> str:
        return _bar_content(bar)

    def _aggregate(self, key: str, bars: tuple[MarketBar, ...], compute):
        cache = getattr(self, "_aggregates", None)
        if cache is None:
            cache = {}
            object.__setattr__(self, "_aggregates", cache)
        sig = (key, len(bars), id(bars[-1]) if bars else 0)
        value = cache.get(sig)
        if value is None:
            value = compute(bars)
            cache[sig] = value
        return value

    def temporal_basis_for(self, bars: tuple[MarketBar, ...]) -> str:
        return self._aggregate("basis", bars, _temporal_basis)

    def max_available_for(self, bars: tuple[MarketBar, ...]) -> int | None:
        return self._aggregate("maxavail", bars,
                               lambda b: max((x.available_at_nano for x in b),
                                             default=None))

    def content_hash(self, bars: tuple[MarketBar, ...]) -> str:
        if not bars:
            return _text_hash("no-inputs")
        if len(bars) > 256:
            # Large sets are almost always the full consumed prefix; the
            # cross-call chain cache answers after full validation and the
            # direct chain fallback is byte-identical.
            return _prefix_chain_hash(bars)
        def _rolling(bars: tuple[MarketBar, ...]) -> str:
            rolling = _text_hash(self.content(bars[0]))
            for bar in bars[1:]:
                rolling = _text_hash(rolling + "|" + self.content(bar))
            return rolling
        return self._aggregate("content", bars, _rolling)

    def wilder(self, n: int, hole) -> tuple:
        cache = self._wilder
        if cache is None:
            cache = self._wilder = {}
        result = cache.get(n)
        if result is None:
            if self.bars:
                key = (self.bars[0].contract_id, self.bars[0].duration_seconds,
                       n, _sessions_fingerprint(self.sessions))
                result = _wilder_walk_incremental(self.bars, n, hole, key)
            else:
                result = _wilder_walk(self.bars, n, hole)
            cache[n] = result
        return result


def _prepare(bars: tuple[MarketBar, ...], decision_nano: int,
             sessions: tuple[SessionWindow, ...], duration_seconds: int,
             visible: tuple[MarketBar, ...] | None = None) -> _PreparedContext:
    """Shared planned-slot anchor resolution (005 §3), computed once.

    ``visible`` lets a caller that already ran the PIT deduplication (the
    snapshot entry) hand the prefix straight through, so ``_visible`` runs
    exactly once per public call and the incremental fast path is always
    taken on the growing per-minute pattern.  The planned-coverage scan
    reuses the prefix state: the head's coverage is remembered per grid,
    so only the freshly appended tail is re-checked; a state mismatch
    (different tuple) falls back to the original full scan.
    """
    if visible is None:
        visible = _visible(bars, decision_nano)
    _single_contract(visible)
    product = visible[0].product if visible else None
    if product is None:
        products = {session.product for session in sessions}
        product = next(iter(products)) if len(products) == 1 else None
    grid = _cached_grid(sessions, product, duration_seconds) if product else None
    if grid is None or grid.calendar_unknown:
        return _PreparedContext(visible, "calendar_unknown", "calendar_unknown",
                                grid, None, decision_nano, sessions)
    state = _prefix_state_for(visible)
    if state is not None:
        verified = state.covers.get(grid, 0)
        uncovered_at = None
        for index in range(verified, len(visible)):
            if not grid.covers(visible[index]):
                uncovered_at = index
                break
        state.covers[grid] = len(visible) if uncovered_at is None else uncovered_at
        covered = uncovered_at is None
    else:
        covered = all(grid.covers(bar) for bar in visible)
    if not covered:
        return _PreparedContext(visible, "calendar_unknown", "calendar_unknown",
                                grid, None, decision_nano, sessions)
    anchor = grid.anchor(decision_nano)
    if anchor is None:
        return _PreparedContext(visible, "warmup", "insufficient_history",
                                grid, None, decision_nano, sessions)
    if grid.anchor_bar(anchor, visible) is None:
        return _PreparedContext(visible, "expected_slot_missing", "expected_slot_missing",
                                grid, anchor, decision_nano, sessions)
    return _PreparedContext(visible, "ok", "", grid, anchor, decision_nano, sessions)


def _anchor_context(bars: tuple[MarketBar, ...], decision_nano: int,
                    sessions: tuple[SessionWindow, ...], duration_seconds: int,
                    lookback: int) -> _AnchorContext:
    """Shared planned-slot anchor resolution (005 §3)."""
    visible = _visible(bars, decision_nano)
    _single_contract(visible)
    product = visible[0].product if visible else None
    if product is None:
        # Derive the product from the declared calendar when no bar is visible.
        products = {session.product for session in sessions}
        product = next(iter(products)) if len(products) == 1 else None
    grid = _cached_grid(sessions, product, duration_seconds) if product else None
    if grid is None or grid.calendar_unknown:
        return _AnchorContext("calendar_unknown", "calendar_unknown", grid, None, visible)
    if any(not grid.covers(bar) for bar in visible):
        return _AnchorContext("calendar_unknown", "calendar_unknown", grid, None, visible)
    anchor = grid.anchor(decision_nano)
    if anchor is None:
        return _AnchorContext("warmup", "insufficient_history", grid, None, visible)
    if grid.anchor_bar(anchor, visible) is None:
        return _AnchorContext("expected_slot_missing", "expected_slot_missing",
                              grid, anchor, visible)
    return _AnchorContext("ok", "", grid, anchor, visible)


def _temporal_basis(bars: tuple[MarketBar, ...]) -> str:
    if all(bar.quality.temporal_basis == "observed" for bar in bars):
        return "observed"
    if any(bar.quality.temporal_basis == "reconstructed" for bar in bars):
        return "reconstructed"
    return "unknown"


def _tail_window(bars: tuple[MarketBar, ...], count: int,
                 hole) -> tuple[tuple[MarketBar, ...], bool]:
    window = bars[-count:] if count >= 1 else ()
    holed = any(hole(a, b) for a, b in zip(window, window[1:]))
    return window, holed


def _positive(field: Decimal | None) -> bool:
    return field is not None and field > 0


def _log_return(current: Decimal, previous: Decimal) -> float | None:
    try:
        ratio = float(current / previous)
    except (OverflowError, ValueError, ZeroDivisionError):
        return None
    if not math.isfinite(ratio) or ratio <= 0:
        return None
    value = math.log(ratio)
    return value if math.isfinite(value) else None


def _closes_returns(window: tuple[MarketBar, ...], n: int) -> tuple[list[float] | None, str]:
    for bar in window:
        if bar.close is None:
            return None, "missing_source_value"
    for bar in window:
        if bar.close <= 0:
            return None, "non_positive_price"
    returns: list[float] = []
    for previous, current in zip(window, window[1:]):
        value = _log_return(current.close, previous.close)
        if value is None:
            return None, "non_finite_value"
        returns.append(value)
    if len(returns) != n:
        return None, "insufficient_history"
    return returns, ""


# ---------------------------------------------------------------------------
# The ten calculators (M2 §4.2).
# ---------------------------------------------------------------------------

def _factor_value(spec: FactorSpec, value: Decimal | float | None, status: str,
                  reasons: tuple[str, ...], consumed: tuple[MarketBar, ...],
                  calendar: str = "", content: str | None = None,
                  context: "_PreparedContext | None" = None) -> FactorValue:
    if content is None:
        content = context.content_hash(consumed) if context is not None \
            else _content_hash(consumed)
    return FactorValue(
        factor_key=spec.factor_key,
        value=value,
        available_at_nano=(context.max_available_for(consumed)
                           if context is not None else
                           max((bar.available_at_nano for bar in consumed),
                               default=None)),
        quality=DataQuality(
            status=status,
            reasons=reasons,
            temporal_basis=(context.temporal_basis_for(consumed)
                            if context is not None and consumed
                            else (_temporal_basis(consumed) if consumed else "unknown")),
            input_hash=_input_hash(content, spec, calendar),
        ),
    )


def _warmup(spec: FactorSpec, consumed: tuple[MarketBar, ...],
            calendar: str = "", reason: str = "insufficient_history",
            context: "_PreparedContext | None" = None) -> FactorValue:
    return _factor_value(spec, None, "WARMUP", (reason,), consumed, calendar,
                         context=context)


def _missing_window(spec: FactorSpec, consumed: tuple[MarketBar, ...],
                    reason: str, calendar: str = "",
                    context: "_PreparedContext | None" = None) -> FactorValue:
    return _factor_value(spec, None, "MISSING", (reason,), consumed, calendar,
                         context=context)


def _invalid(spec: FactorSpec, consumed: tuple[MarketBar, ...], reason: str,
             calendar: str = "",
             context: "_PreparedContext | None" = None) -> FactorValue:
    return _factor_value(spec, None, "INVALID", (reason,), consumed, calendar,
                         context=context)


def _param_n(spec: FactorSpec, key: str = "n") -> int:
    value = spec.parameters.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FactorsError(f"invalid_window_parameter:{key}")
    return value


def _annualization_factor(spec: FactorSpec) -> int:
    value = spec.parameters.get("annualization_factor")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise FactorsError("invalid_annualization_factor")
    return value


def _mom001(spec: FactorSpec, bars: tuple[MarketBar, ...], hole, calendar: str,
                    context: "_PreparedContext | None" = None) -> FactorValue:
    n = _param_n(spec)
    window, holed = _tail_window(bars, n + 1, hole)
    if holed:
        return _missing_window(spec, window, "missing_slot_in_window", calendar,
                             context=context)
    if len(window) < n + 1:
        return _warmup(spec, bars, calendar, context=context)
    returns, reason = _closes_returns(window, n)
    if returns is None:
        status = "MISSING" if reason == "missing_source_value" else "INVALID"
        return _factor_value(spec, None, status, (reason,), window, calendar,
                             context=context)
    return _factor_value(spec, math.fsum(returns), "READY", (), window, calendar,
                             context=context)


def _mom003(spec: FactorSpec, bars: tuple[MarketBar, ...], hole, calendar: str,
                    context: "_PreparedContext | None" = None) -> FactorValue:
    n = _param_n(spec)
    window, holed = _tail_window(bars, n + 1, hole)
    if holed:
        return _missing_window(spec, window, "missing_slot_in_window", calendar,
                             context=context)
    if len(window) < n + 1:
        return _warmup(spec, bars, calendar, context=context)
    returns, reason = _closes_returns(window, n)
    if returns is None:
        status = "MISSING" if reason == "missing_source_value" else "INVALID"
        return _factor_value(spec, None, status, (reason,), window, calendar,
                             context=context)
    if n < 2:
        return _invalid(spec, window, "std_requires_n_ge_2", calendar)
    std = statistics.stdev(returns)
    if std == 0.0:
        return _invalid(spec, window, "zero_variance", calendar)
    value = math.fsum(returns) / (std * math.sqrt(n))
    return _factor_value(spec, value, "READY", (), window, calendar,
                         context=context)


def _mom010(spec: FactorSpec, bars: tuple[MarketBar, ...], hole, calendar: str,
                    context: "_PreparedContext | None" = None) -> FactorValue:
    n = _param_n(spec)
    window, holed = _tail_window(bars, n + 1, hole)
    if holed:
        return _missing_window(spec, window, "missing_slot_in_window", calendar,
                             context=context)
    if len(window) < n + 1:
        return _warmup(spec, bars, calendar, context=context)
    for bar in window:
        if bar.close is None:
            return _missing_window(spec, window, "missing_source_value", calendar)
    for bar in window:
        if bar.close <= 0:
            return _invalid(spec, window, "non_positive_price", calendar)
    displacement = window[-1].close - window[0].close
    total_path = sum((abs(current.close - previous.close)
                      for previous, current in zip(window, window[1:])), Decimal(0))
    if total_path == 0:
        return _invalid(spec, window, "zero_total_path", calendar)
    return _factor_value(spec, displacement / total_path, "READY", (), window, calendar,
                             context=context)


def _rev001(spec: FactorSpec, bars: tuple[MarketBar, ...], hole, calendar: str,
                    context: "_PreparedContext | None" = None) -> FactorValue:
    n = _param_n(spec, "k") if "k" in spec.parameters else _param_n(spec)
    window, holed = _tail_window(bars, n + 1, hole)
    if holed:
        return _missing_window(spec, window, "missing_slot_in_window", calendar,
                             context=context)
    if len(window) < n + 1:
        return _warmup(spec, bars, calendar, context=context)
    returns, reason = _closes_returns(window, n)
    if returns is None:
        status = "MISSING" if reason == "missing_source_value" else "INVALID"
        return _factor_value(spec, None, status, (reason,), window, calendar,
                             context=context)
    return _factor_value(spec, -math.fsum(returns), "READY", (), window, calendar,
                             context=context)


def _vol001(spec: FactorSpec, bars: tuple[MarketBar, ...], hole, calendar: str,
                    context: "_PreparedContext | None" = None) -> FactorValue:
    n = _param_n(spec)
    window, holed = _tail_window(bars, n + 1, hole)
    if holed:
        return _missing_window(spec, window, "missing_slot_in_window", calendar,
                             context=context)
    if len(window) < n + 1:
        return _warmup(spec, bars, calendar, context=context)
    returns, reason = _closes_returns(window, n)
    if returns is None:
        status = "MISSING" if reason == "missing_source_value" else "INVALID"
        return _factor_value(spec, None, status, (reason,), window, calendar,
                             context=context)
    if n < 2:
        return _invalid(spec, window, "std_requires_n_ge_2", calendar)
    annualization = _annualization_factor(spec)
    std = statistics.stdev(returns)
    if std == 0.0:
        return _factor_value(spec, 0.0, "DEGRADED", ("zero_variance",), window, calendar,
                                context=context)
    return _factor_value(spec, std * math.sqrt(annualization), "READY", (), window, calendar,
                                context=context)


def _true_range(previous: MarketBar, current: MarketBar) -> Decimal | None:
    values = (previous.close, current.high, current.low, current.close)
    if any(value is None for value in values):
        return None
    if any(value <= 0 for value in values):
        return None
    return max(
        current.high - current.low,
        abs(current.high - previous.close),
        abs(current.low - previous.close),
    )


def _wilder_walk(bars: tuple[MarketBar, ...], n: int, hole):
    """Wilder ATR at the last bar plus the post-reset content hash (E07)."""
    seeded: Decimal | None = None
    pending: list[Decimal] = []
    run_start = 0
    for index, (previous, current) in enumerate(zip(bars, bars[1:])):
        if hole(previous, current):
            seeded = None
            pending = []
            run_start = index + 1
            continue
        tr = _true_range(previous, current)
        if tr is None:
            seeded = None
            pending = []
            run_start = index + 1
            continue
        if seeded is None:
            pending.append(tr)
            if len(pending) == n:
                seeded = sum(pending, Decimal(0)) / Decimal(n)
                pending = []
        else:
            seeded = (seeded * Decimal(n - 1) + tr) / Decimal(n)
    prefix_hash = _content_hash(bars[run_start:])
    if not bars:
        return None, "insufficient_history", prefix_hash
    last = bars[-1]
    if len(bars) < n + 1:
        return None, "insufficient_history", prefix_hash
    if any(field is None for field in (last.high, last.low, last.close)):
        return None, "missing_source_value", prefix_hash
    if any(field <= 0 for field in (last.high, last.low, last.close)):
        return None, "non_positive_price", prefix_hash
    if seeded is None:
        return None, "wilder_reseeding", prefix_hash
    return seeded, "", prefix_hash


# ---------------------------------------------------------------------------
# Incremental Wilder state cache (DESIGN-NOTES §1.6).  The recursion is a
# deterministic left fold over the PIT prefix; a cross-call cache keyed by
# (contract, duration, n, sessions fingerprint) stores the fold state
# (consumed count, the full consumed head tuple, seeded value, pending TRs,
# post-reset run start and the rolling content-hash chain).  A resume is
# only accepted when the *entire* consumed head validates against the
# current bars by tuple equality (frozen bars: element equality implies
# identical content) — never from the last bar's identity alone — so
# revised history forces a full replay.  Byte-identity: resumed folds
# replay exactly the original operation order.
# ---------------------------------------------------------------------------

_WILDER_STATE_CACHE: dict[tuple, dict[int, tuple]] = {}
_WILDER_STATE_LIMIT = 2


def _wilder_walk_incremental(bars: tuple[MarketBar, ...], n: int, hole,
                             cache_key: tuple) -> tuple:
    bucket = _WILDER_STATE_CACHE.setdefault(cache_key, {})
    state = bucket.get(n)
    consumed = 1
    seeded: Decimal | None = None
    pending: list[Decimal] = []
    run_start = 0
    chain = None
    if bars:
        chain = _text_hash(_bar_content(bars[0]))
    if state is not None:
        state_consumed, state_head, state_seeded, state_pending,             state_run_start, state_chain = state
        if (0 < state_consumed < len(bars)
                and len(state_head) == state_consumed
                and bars[:state_consumed] == state_head):
            consumed = state_consumed
            seeded = state_seeded
            pending = list(state_pending)
            run_start = state_run_start
            chain = state_chain
        else:
            state = None
    for index in range(consumed - 1, len(bars) - 1):
        previous = bars[index]
        current = bars[index + 1]
        tr = None
        if not hole(previous, current):
            tr = _true_range(previous, current)
        if tr is None:
            seeded = None
            pending = []
            run_start = index + 1
            chain = _text_hash(_bar_content(current)) if index + 1 < len(bars)                 else _text_hash("no-inputs")
        else:
            if seeded is None:
                pending.append(tr)
                if len(pending) == n:
                    seeded = sum(pending, Decimal(0)) / Decimal(n)
                    pending = []
            else:
                seeded = (seeded * Decimal(n - 1) + tr) / Decimal(n)
            chain = _text_hash(chain + "|" + _bar_content(current))
        consumed = index + 2
    if len(bucket) >= _WILDER_STATE_LIMIT:
        bucket.clear()
    if bars:
        bucket[n] = (len(bars), bars, seeded, tuple(pending),
                     run_start, chain)
    prefix_hash = chain if chain is not None else _text_hash("no-inputs")
    if not bars:
        return None, "insufficient_history", prefix_hash
    last = bars[-1]
    if len(bars) < n + 1:
        return None, "insufficient_history", prefix_hash
    if any(field is None for field in (last.high, last.low, last.close)):
        return None, "missing_source_value", prefix_hash
    if any(field <= 0 for field in (last.high, last.low, last.close)):
        return None, "non_positive_price", prefix_hash
    if seeded is None:
        return None, "wilder_reseeding", prefix_hash
    return seeded, "", prefix_hash


def wilder_atr(bars: tuple[MarketBar, ...], n: int,
               sessions: tuple[SessionWindow, ...] = ()) -> tuple[Decimal | None, str]:
    """Public Wilder helper over the planned grid of the first bar's product."""
    if bars and sessions:
        grid = _cached_grid(sessions, bars[0].product, bars[0].duration_seconds)
        return _wilder_walk(bars, n, _grid_hole(grid))[:2]
    return _wilder_walk(bars, n, _no_hole)[:2]


def _vol004(spec: FactorSpec, bars: tuple[MarketBar, ...], hole, calendar: str,
            context: "_PreparedContext | None" = None) -> FactorValue:
    n = _param_n(spec)
    if not bars:
        return _warmup(spec, bars, calendar, context=context)
    if context is not None:
        atr, reason, prefix_hash = context.wilder(n, hole)
    else:
        atr, reason, prefix_hash = _wilder_walk(bars, n, hole)
    value_hash = _input_hash(prefix_hash, spec, calendar)
    if atr is None:
        if reason == "insufficient_history":
            return _factor_value(spec, None, "WARMUP", ("insufficient_history",),
                                 bars, calendar, content=prefix_hash)
        if reason == "missing_source_value":
            return _factor_value(spec, None, "MISSING", (reason,), bars, calendar,
                                 content=prefix_hash)
        if reason == "wilder_reseeding":
            return _factor_value(spec, None, "WARMUP", ("wilder_reseeding",),
                                 bars, calendar, content=prefix_hash)
        return _factor_value(spec, None, "INVALID", (reason,), bars, calendar,
                             content=prefix_hash)
    close = bars[-1].close
    if close is None:
        return _factor_value(spec, None, "MISSING", ("missing_source_value",), bars,
                             calendar, content=prefix_hash)
    if close <= 0:
        return _factor_value(spec, None, "INVALID", ("non_positive_price",), bars,
                             calendar, content=prefix_hash)
    return _factor_value(spec, atr / abs(close), "READY", (), bars, calendar,
                         content=prefix_hash)


def _negative_squared_sum(returns: list[float], annualization: int) -> float:
    try:
        total = math.fsum(min(value, 0.0) ** 2 for value in returns)
        value = annualization * total / len(returns)
    except OverflowError as exc:
        raise FactorsError("non_finite_value") from exc
    if not math.isfinite(value):
        raise FactorsError("non_finite_value")
    return value


def _vol005(spec: FactorSpec, bars: tuple[MarketBar, ...], hole, calendar: str,
                    context: "_PreparedContext | None" = None) -> FactorValue:
    n = _param_n(spec)
    window, holed = _tail_window(bars, n + 1, hole)
    if holed:
        return _missing_window(spec, window, "missing_slot_in_window", calendar,
                             context=context)
    if len(window) < n + 1:
        return _warmup(spec, bars, calendar, context=context)
    returns, reason = _closes_returns(window, n)
    if returns is None:
        status = "MISSING" if reason == "missing_source_value" else "INVALID"
        return _factor_value(spec, None, status, (reason,), window, calendar,
                             context=context)
    annualization = _annualization_factor(spec)
    return _factor_value(
        spec, _negative_squared_sum(returns, annualization), "READY", (), window,
        calendar, context=context)


def _voi001(spec: FactorSpec, bars: tuple[MarketBar, ...], hole, calendar: str,
                    context: "_PreparedContext | None" = None) -> FactorValue:
    n = _param_n(spec)
    window, holed = _tail_window(bars, n + 1, hole)
    if holed:
        return _missing_window(spec, window, "missing_slot_in_window", calendar,
                             context=context)
    if len(window) < n + 1:
        return _warmup(spec, bars, calendar, context=context)
    for bar in window:
        if bar.volume is None:
            return _missing_window(spec, window, "missing_source_value", calendar)
    current_volume = window[-1].volume
    reference = sum((bar.volume for bar in window[:-1]), Decimal(0)) / Decimal(n)
    if reference == 0:
        return _invalid(spec, window, "zero_denominator", calendar)
    return _factor_value(spec, current_volume / reference - Decimal(1), "READY", (),
                         window, calendar, context=context)


def _voi007(spec: FactorSpec, bars: tuple[MarketBar, ...], hole, calendar: str,
                    context: "_PreparedContext | None" = None) -> FactorValue:
    n = _param_n(spec)
    window, holed = _tail_window(bars, n + 1, hole)
    if holed:
        return _missing_window(spec, window, "missing_slot_in_window", calendar,
                             context=context)
    if len(window) < n + 1:
        return _warmup(spec, bars, calendar, context=context)
    for bar in window:
        if bar.close_oi is None:
            return _missing_window(spec, window, "missing_source_value", calendar)
    lagged = window[0].close_oi
    if lagged == 0:
        return _invalid(spec, window, "zero_denominator", calendar)
    return _factor_value(spec, window[-1].close_oi / lagged - Decimal(1), "READY", (),
                         window, calendar, context=context)


def _voi009(spec: FactorSpec, bars: tuple[MarketBar, ...], hole, calendar: str,
                    context: "_PreparedContext | None" = None) -> FactorValue:
    window, _holed = _tail_window(bars, 1, hole)
    if not window:
        return _warmup(spec, bars, calendar, context=context)
    bar = window[-1]
    if bar.volume is None or bar.close_oi is None:
        return _missing_window(spec, window, "missing_source_value", calendar)
    if bar.close_oi == 0:
        return _invalid(spec, window, "zero_denominator", calendar)
    return _factor_value(spec, bar.volume / bar.close_oi, "READY", (), window, calendar,
                            context=context)


_CALCULATORS = {
    "MOM001": _mom001, "MOM003": _mom003, "MOM010": _mom010, "REV001": _rev001,
    "VOL001": _vol001, "VOL004": _vol004, "VOL005": _vol005,
    "VOI001": _voi001, "VOI007": _voi007, "VOI009": _voi009,
}


def compute_factor(spec: FactorSpec, bars: tuple[MarketBar, ...],
                   decision_nano: int, *,
                   sessions: tuple[SessionWindow, ...]) -> FactorValue:
    """Compute one registered factor causally at ``decision_nano``.

    ``sessions`` is the required complete frozen plan.  Bars are
    point-in-time filtered before duplicate conflict detection; the
    computation anchor is the last planned complete slot finishing at or
    before the decision — a missing anchor observation never falls back
    to an earlier bar.  Empty/uncovered/``UNKNOWN`` calendars yield
    ``MISSING/calendar_unknown``.
    """
    if not isinstance(spec, FactorSpec):
        raise TypeError("spec_must_be_FactorSpec")
    _check_bars_container(bars)
    if isinstance(decision_nano, bool) or not isinstance(decision_nano, int):
        raise TypeError("decision_nano_must_be_int")
    if not isinstance(sessions, tuple) or any(
            not isinstance(session, SessionWindow) for session in sessions):
        raise TypeError("sessions_must_be_tuple_of_SessionWindow")

    context = _prepare(bars, decision_nano, sessions, spec.duration_seconds)
    return _compute_with_context(spec, context)


def _compute_with_context(spec: FactorSpec,
                          context: "_PreparedContext") -> FactorValue:
    """Compute one spec from an already prepared PIT prefix (005 §3)."""
    if context.status == "calendar_unknown":
        return _factor_value(spec, None, "MISSING", ("calendar_unknown",),
                             context.bars, "calendar-unknown", context=context)
    if context.status == "warmup":
        return _warmup(spec, context.bars, context=context)
    calendar_binding = context.grid.binding(
        context.grid.window_start(context.anchor, spec.lookback),
        context.anchor.end_nano)
    if context.status == "expected_slot_missing":
        return _factor_value(spec, None, "MISSING", ("expected_slot_missing",),
                             context.bars, calendar_binding, context=context)
    duration = _single_duration(context.bars)
    if duration is not None and duration != spec.duration_seconds:
        raise FactorsError("duration_mismatch_between_spec_and_bars")
    hole = _grid_hole(context.grid)
    calculator = _CALCULATORS.get(spec.source_factor_id)
    if calculator is not None:
        return calculator(spec, context.bars, hole, calendar_binding, context)
    if spec.source_factor_id == "BASE:opening_gap_points":
        from ak_trading.v2.baseline import opening_gap_value
        return opening_gap_value(spec, context, calendar_binding)
    if spec.source_factor_id in BASELINE_NAMES:
        from ak_trading.v2.baseline import baseline_rows_for_context, \
            baseline_rebind_for_spec
        rows = baseline_rows_for_context(context, hole)
        return baseline_rebind_for_spec(spec, rows, context, calendar_binding)
    raise FactorsError(f"unknown_source_factor_id:{spec.source_factor_id}")


# ---------------------------------------------------------------------------
# Feature snapshot construction (M2 §4)
# ---------------------------------------------------------------------------

_STATUS_SEVERITY = {"READY": 0, "WARMUP": 1, "STALE": 2, "DEGRADED": 3, "MISSING": 4, "INVALID": 5}


def _worst_status(statuses: list[str]) -> str:
    return max(statuses, key=lambda status: _STATUS_SEVERITY.get(status, 0))


def _merge_reasons(values: tuple[FactorValue, ...], extra: tuple[str, ...]) -> tuple[str, ...]:
    reasons: list[str] = []
    for value in values:
        for reason in value.quality.reasons:
            if reason not in reasons:
                reasons.append(reason)
    for reason in extra:
        if reason not in reasons:
            reasons.append(reason)
    return tuple(reasons)


def _check_rule(rule: TradingRule, contract_id: str, decision_nano: int) -> None:
    if rule.contract_id != contract_id:
        raise FactorsError("rule_contract_mismatch")
    if not (rule.effective_from_nano <= decision_nano < rule.effective_until_nano):
        raise FactorsError("rule_not_effective_at_decision_nano")
    if rule.available_at_nano > decision_nano:
        raise FactorsError("rule_not_visible_at_decision_nano")


def _meta_values(rule: TradingRule | None, contract_id: str,
                 decision_nano: int) -> tuple[FactorValue, ...]:
    def meta(factor_key: str, value: Decimal | None) -> FactorValue:
        status = "READY" if value is not None else "MISSING"
        reasons: tuple[str, ...] = () if value is not None else (COST_UNKNOWN_REASON,)
        input_hash = _text_hash(
            f"meta-rule|{rule.rule_id}|{rule.source_sha256}" if rule is not None
            else "meta-rule|none")
        return FactorValue(
            factor_key=factor_key, value=value,
            available_at_nano=(rule.available_at_nano if rule is not None else None),
            quality=DataQuality(
                status=status, reasons=reasons, temporal_basis="observed",
                input_hash=input_hash),
        )

    if rule is not None:
        _check_rule(rule, contract_id, decision_nano)
        return (meta(META_TICK_FACTOR_KEY, rule.price_tick),
                meta(META_MULTIPLIER_FACTOR_KEY, rule.multiplier))
    return (meta(META_TICK_FACTOR_KEY, None), meta(META_MULTIPLIER_FACTOR_KEY, None))


def _seasonal_binding(model: SeasonalityModel) -> str:
    return "|".join((
        "seasonal", model.model_id, model.content_hash(), model.input_sha256,
        model.calendar_version, str(model.fit_start_nano), str(model.fit_end_nano)))


def _planned_previous_bar(last_bar: MarketBar, visible: tuple[MarketBar, ...],
                          sessions: tuple[SessionWindow, ...]) -> MarketBar | None:
    """Select the calendar-planned previous slot's bar (005 §2)."""
    calendar = _cached_product_calendar(sessions, last_bar.product,
                                        last_bar.duration_seconds)
    address = calendar.address(last_bar)
    if address is None:
        return None
    previous_slot = calendar.previous_slot(address)
    if previous_slot is None:
        return None
    target = previous_slot.start_nano
    # ``visible`` is sorted by (start_nano, dedup key), so bars sharing the
    # target start form one contiguous run; walking back from the end and
    # keeping the earliest match reproduces the original forward first-hit
    # without scanning the whole prefix.
    found: MarketBar | None = None
    for bar in reversed(visible):
        start = bar.start_nano
        if start > target:
            continue
        if start == target:
            if bar.contract_id == last_bar.contract_id:
                found = bar
            continue
        break
    return found


def build_feature_snapshot(contract_id: str, bars: tuple[MarketBar, ...],
                           decision_nano: int, specs: tuple[FactorSpec, ...],
                           daily_background: FeatureSnapshot | None,
                           seasonal: SeasonalityModel | None,
                           rule: TradingRule | None,
                           sessions: tuple[SessionWindow, ...]) -> FeatureSnapshot:
    """Assemble one contract/duration feature snapshot at ``decision_nano``.

    All specs share the bar duration; every registered factor — the ten
    calculators *and* registered baselines — keeps a row even at
    missing/warmup points.  The anchor is the last planned complete slot
    of the frozen calendar; session-tail/head/trading-day holes interrupt
    windows, legal boundaries stay ``INCLUDE_GAP``.  ``available_at_nano``
    is the latest visibility time of every actually consumed input (bars,
    rule, daily background and, when legitimately usable, the seasonal fit
    boundary) and never exceeds ``decision_nano``.
    """
    if not isinstance(contract_id, str) or not contract_id:
        raise TypeError("contract_id_must_be_non_empty_str")
    _check_bars_container(bars)
    if isinstance(decision_nano, bool) or not isinstance(decision_nano, int):
        raise TypeError("decision_nano_must_be_int")
    if not isinstance(specs, tuple) or any(not isinstance(spec, FactorSpec) for spec in specs):
        raise TypeError("specs_must_be_tuple_of_FactorSpec")
    if daily_background is not None and not isinstance(daily_background, FeatureSnapshot):
        raise TypeError("daily_background_must_be_FeatureSnapshot_or_None")
    if seasonal is not None and not isinstance(seasonal, SeasonalityModel):
        raise TypeError("seasonal_must_be_SeasonalityModel_or_None")
    if rule is not None and not isinstance(rule, TradingRule):
        raise TypeError("rule_must_be_TradingRule_or_None")
    if not isinstance(sessions, tuple) or any(
            not isinstance(session, SessionWindow) for session in sessions):
        raise TypeError("sessions_must_be_tuple_of_SessionWindow")

    visible = _visible(bars, decision_nano)
    _single_contract(visible)
    if visible and visible[0].contract_id != contract_id:
        raise FactorsError("bars_contract_mismatch")
    duration = _single_duration(visible)
    spec_durations = {spec.duration_seconds for spec in specs}
    if len(spec_durations) > 1:
        raise FactorsError("mixed_spec_durations")
    if spec_durations:
        spec_duration = next(iter(spec_durations))
        if duration is not None and duration != spec_duration:
            raise FactorsError("duration_mismatch_between_specs_and_bars")
        duration = spec_duration
    if duration is None:
        raise FactorsError("duration_undetermined")
    if duration not in BAR_DURATIONS:
        raise FactorsError(f"unsupported_duration_seconds:{duration}")
    if not visible:
        raise FactorsError("no_visible_bars_for_snapshot")
    last_bar = visible[-1]
    product = last_bar.product
    raw_symbol = last_bar.raw_symbol

    grid = _cached_grid(sessions, product, duration)

    session_by_id = _sessions_bundle(sessions)[2]
    identity_reasons: tuple[str, ...] = ()
    if duration == DAILY_DURATION:
        if last_bar.trading_day is None:
            raise FactorsError("trading_day_missing_for_daily_snapshot")
        trading_day: date = last_bar.trading_day
        session_id = f"DAILY:{product}:{trading_day.isoformat()}"
        session_kind = "DAILY"
    else:
        session = session_by_id.get(last_bar.session_id) \
            if last_bar.session_id is not None else None
        if session is None:
            raise FactorsError("session_identity_missing_for_minute_snapshot")
        if session.product != product:
            raise FactorsError("session_product_mismatch")
        if last_bar.trading_day is not None and last_bar.trading_day != session.trading_day:
            raise FactorsError("session_trading_day_mismatch")
        # E05: the anchor bar must actually belong to the declared session.
        slot_nano = duration * NANO_PER_SECOND
        inside = (session.start_nano <= last_bar.start_nano
                  and last_bar.end_nano <= session.end_nano
                  and (last_bar.start_nano - session.start_nano) % slot_nano == 0)
        if not inside:
            identity_reasons = ("bar_outside_declared_session",)
        trading_day = session.trading_day
        session_id = session.session_id
        session_kind = session.kind

    prepared = _prepare(bars, decision_nano, sessions, duration, visible=visible)
    spec_values = [_compute_with_context(spec, prepared) for spec in specs]

    has_wilder = any(spec.source_factor_id == "VOL004" for spec in specs)
    if has_wilder:
        # VOL004's recursion consumes the whole visible prefix (plus the
        # anchor, which is already inside it); the union equals ``visible``
        # and the set/filter passes are unnecessary (incremental cursor).
        consumed_bars = visible
    else:
        consumed_ids: set[str] = set()
        for spec in specs:
            consumed_ids.update(bar.bar_id for bar in visible[-spec.lookback:])
        consumed_ids.add(last_bar.bar_id)
        consumed_bars = tuple(
            bar for bar in visible if bar.bar_id in consumed_ids
        )
    source_bar_ids = tuple(bar.bar_id for bar in consumed_bars)

    factor_values: list[FactorValue] = list(spec_values)
    seasonal_used = False
    if seasonal is not None:
        previous_bar = _planned_previous_bar(last_bar, visible, sessions)
        seasonal_columns = seasonal_values(
            seasonal, last_bar, sessions, decision_nano,
            previous_bar=previous_bar)
        factor_values.extend(seasonal_columns)
        seasonal_used = _model_legitimately_usable(seasonal, sessions, last_bar,
                                                   decision_nano)
    meta = _meta_values(rule, contract_id, decision_nano)
    factor_values.extend(meta)
    values = tuple(sorted(factor_values, key=lambda value: value.factor_key))

    reference_price = last_bar.close if _positive(last_bar.close) else None
    reference_reasons: tuple[str, ...] = () if reference_price is not None \
        else ("reference_price_unavailable",)

    atr_points: Decimal | None = None
    atr_reasons: tuple[str, ...] = ()
    vol004_specs = [spec for spec in specs if spec.source_factor_id == "VOL004"]
    if vol004_specs:
        vol004_value = spec_values[[spec.source_factor_id for spec in specs].index("VOL004")]
        if vol004_value.quality.status == "READY" and reference_price is not None:
            atr, _reason, _hash = prepared.wilder(_param_n(vol004_specs[0]),
                                                _grid_hole(grid))
            if atr is not None and atr > 0:
                atr_points = atr
        if atr_points is None:
            atr_reasons = ("atr_points_unavailable",)

    if daily_background is not None:
        if daily_background.duration_seconds != DAILY_DURATION:
            raise FactorsError("daily_background_must_be_daily_duration")
        if daily_background.session_kind != "DAILY":
            raise FactorsError("daily_background_must_be_daily_kind")
        if daily_background.contract_id != contract_id:
            raise FactorsError("daily_background_contract_mismatch")
        if daily_background.product != product:
            raise FactorsError("daily_background_product_mismatch")
        if daily_background.trading_day >= trading_day:
            raise FactorsError("daily_background_must_precede_current_trading_day")

    status = _worst_status([value.quality.status for value in values]
                           + (["MISSING"] if reference_reasons else [])
                           + (["MISSING"] if atr_reasons else [])
                           + (["INVALID"] if identity_reasons else []))
    reasons = _merge_reasons(values, (*reference_reasons, *atr_reasons, *identity_reasons))

    availability_base: int | None
    consumed_state = _prefix_state_for(visible) if consumed_bars is visible else None
    if consumed_state is not None:
        availability_base = consumed_state.max_available
        temporal_basis = _basis_status(consumed_state.basis)
    else:
        availability_base = max((bar.available_at_nano for bar in consumed_bars
                                 if bar.available_at_nano is not None), default=None)
        temporal_basis = _temporal_basis(consumed_bars)
    availability = [] if availability_base is None else [availability_base]
    if rule is not None:
        availability.append(rule.available_at_nano)
    if daily_background is not None:
        availability.append(daily_background.available_at_nano)
    if seasonal_used and seasonal.fit_end_nano <= decision_nano:
        availability.append(seasonal.fit_end_nano)
    available_at = max(availability) if availability else decision_nano
    if available_at > decision_nano:
        raise FactorsError("available_at_exceeds_decision_nano")

    consumed_content = (_prefix_chain_hash(consumed_bars)
                        if len(consumed_bars) > 256
                        else _content_hash(consumed_bars))
    specs_binding = _specs_tuple_binding(specs)
    calendar_binding = _sessions_bundle(sessions)[1]
    quality = DataQuality(
        status=status,
        reasons=reasons,
        temporal_basis=temporal_basis,
        input_hash=_text_hash("|".join((
            "snapshot-inputs", consumed_content, specs_binding, calendar_binding,
            f"rule|{rule.rule_id}|{rule.source_sha256}" if rule is not None else "rule|none",
            f"background|{daily_background.snapshot_id}"
            if daily_background is not None else "background|none",
            _seasonal_binding(seasonal) if seasonal is not None else "seasonal|none",
        ))),
    )
    snapshot_id = stable_id("snapshot", canonical_json_bytes({
        "contract_id": contract_id,
        "raw_symbol": raw_symbol,
        "product": product,
        "trading_day": trading_day.isoformat(),
        "session_id": session_id,
        "session_kind": session_kind,
        "decision_nano": decision_nano,
        "available_at_nano": available_at,
        "duration_seconds": duration,
        "source_bar_ids": list(source_bar_ids),
        "reference_price": None if reference_price is None else str(reference_price),
        "atr_points": None if atr_points is None else str(atr_points),
        "values": [encode_record(value) for value in values],
        "daily_background_id": (daily_background.snapshot_id
                                if daily_background is not None else None),
        "specs_binding": specs_binding,
        "calendar_binding": calendar_binding,
        "seasonal_binding": _seasonal_binding(seasonal) if seasonal is not None else None,
        "rule_binding": (f"{rule.rule_id}|{rule.source_sha256}"
                         if rule is not None else None),
        "consumed_content": consumed_content,
        "quality": encode_record(quality),
    }))
    return FeatureSnapshot(
        snapshot_id=snapshot_id,
        contract_id=contract_id,
        raw_symbol=raw_symbol,
        product=product,
        trading_day=trading_day,
        session_id=session_id,
        session_kind=session_kind,
        decision_nano=decision_nano,
        available_at_nano=available_at,
        duration_seconds=duration,
        source_bar_ids=source_bar_ids,
        reference_price=reference_price,
        atr_points=atr_points,
        values=values,
        daily_background_id=(daily_background.snapshot_id
                             if daily_background is not None else None),
        quality=quality,
    )


def _model_legitimately_usable(model: SeasonalityModel, sessions: tuple[SessionWindow, ...],
                               bar: MarketBar, decision_nano: int) -> bool:
    """The model actually contributed evidence at this decision time."""
    if model.fit_end_nano > decision_nano:
        return False
    calendar = _cached_product_calendar(sessions, bar.product, bar.duration_seconds)
    session = calendar.session(bar.session_id)
    if session is None or session.calendar_version != model.calendar_version:
        return False
    return bool(calendar.calendars_between(model.fit_start_nano, model.fit_end_nano))
