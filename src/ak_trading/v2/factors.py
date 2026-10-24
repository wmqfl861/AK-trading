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
from bisect import bisect_right
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


_PREFIX_CACHE: dict[tuple[str, int], dict[str, tuple]] = {}
_PREFIX_CACHE_LIMIT = 8


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

    The cache stores, per (contract, duration), up to a few recent
    (decision -> prefix) states.  A new call whose input contains the
    cached prefix's bars verbatim and whose decision advanced can reuse
    the deduplicated prefix and deduplicate only the appended tail;
    conflicts among newly visible rows are still detected on the tail,
    and any mismatch falls back to the full computation.  Outputs are
    byte-identical by construction (same dedup algorithm over the same
    candidate set).
    """
    candidates = _visible_candidates(list(bars), decision_nano)
    if not candidates:
        return ()
    key = (candidates[0].contract_id, candidates[0].duration_seconds)
    bucket = _PREFIX_CACHE.setdefault(key, {})
    # Try to extend the longest cached prefix that is a subsequence-prefix
    # of the current candidate list (by object identity, which implies
    # equal content for frozen dataclasses).
    best_length = -1
    best_state = None
    for state_key, state in list(bucket.items()):
        cached_prefix = state[0]
        length = len(cached_prefix)
        if length == 0 or length > len(candidates):
            continue
        if candidates[length - 1] is not cached_prefix[length - 1]:
            continue
        if length > best_length:
            best_length = length
            best_state = (state_key, state)
    if best_state is not None and best_length > 16:
        _state_key, state = best_state
        cached_prefix, _cached_decision, cached_chain = state
        tail = candidates[best_length:]
        # Identity check at the head boundary is sufficient: frozen bars
        # from one decoded collection are unique objects per slot.
        if tail and candidates[best_length - 1] is cached_prefix[best_length - 1]:
            deduped_tail = _deduplicate_visible(list(tail))
            merged = _merge_prefixes(cached_prefix, deduped_tail)
            chain = cached_chain
            for bar in deduped_tail:
                chain = _rolling_chain(chain, bar)
            if len(bucket) >= _PREFIX_CACHE_LIMIT:
                bucket.clear()
            bucket[str(decision_nano)] = (merged, decision_nano, chain)
            return merged
    result = _deduplicate_visible(candidates)
    chain = _full_chain(result)
    if len(bucket) >= _PREFIX_CACHE_LIMIT:
        bucket.clear()
    bucket[str(decision_nano)] = (result, decision_nano, chain)
    return result


def _prefix_chain_hash(bars: tuple[MarketBar, ...]) -> str:
    """Full-prefix rolling content hash, reusing the prefix cache state.

    Falls back to the direct chain when no cached state matches (the
    fallback is byte-identical by construction).
    """
    if not bars:
        return _text_hash("no-inputs")
    key = (bars[0].contract_id, bars[0].duration_seconds)
    for state in _PREFIX_CACHE.get(key, {}).values():
        cached_prefix = state[0]
        if len(cached_prefix) == len(bars) and cached_prefix[-1] is bars[-1]:
            return state[2]
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
        relevant = [
            session for session in self.sessions
            if window_start is not None
            and session.start_nano < anchor_end and session.end_nano > window_start
        ] if not self.calendar_unknown else []
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


def _cached_product_calendar(sessions: tuple[SessionWindow, ...], product: str,
                             duration_seconds: int):
    """Content-cached seasonality calendar (immutable, shareable)."""
    from ak_trading.v2.seasonality import cached_product_calendar
    return cached_product_calendar(sessions, product, duration_seconds)


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


def _sessions_fingerprint(sessions: tuple[SessionWindow, ...]) -> str:
    return _text_hash("|".join(_session_binding(session) for session in sessions)
                      or "no-sessions")


def _cached_grid(sessions: tuple[SessionWindow, ...], product: str,
                 duration_seconds: int) -> _FactorGrid:
    key = (_sessions_fingerprint(sessions), product, duration_seconds)
    grid = _GRID_CACHE.get(key)
    if grid is None:
        grid = _FactorGrid(sessions, product, duration_seconds)
        _GRID_CACHE[key] = grid
    return grid


@dataclass
class _PreparedContext:
    """One PIT-deduplicated bar prefix plus its planned anchor and per-call
    memoisation (bar content strings, baseline rows, Wilder walks).  Scoped
    to a single public call; the id()-keyed memos are safe because the bar
    objects stay alive inside ``bars`` for the whole call."""

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
        contents = self._contents
        if contents is None:
            contents = self._contents = {}
        text = contents.get(id(bar))
        if text is None:
            text = _bar_content(bar)
            contents[id(bar)] = text
        return text

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
            # cross-call chain cache answers in O(1) and the direct chain
            # fallback is byte-identical.
            return _prefix_chain_hash(bars)
        rolling = _text_hash(self.content(bars[0]))
        for bar in bars[1:]:
            rolling = _text_hash(rolling + "|" + self.content(bar))
        return rolling

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
    taken on the growing per-minute pattern.
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
    if any(not grid.covers(bar) for bar in visible):
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
# (consumed count, seeded value, pending TRs, post-reset run start and the
# rolling content-hash chain) and resumes from the object-identity-verified
# head, so a per-minute caller pays O(new bars) instead of O(prefix).
# Byte-identity: resumed folds replay exactly the original operation order.
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
        state_consumed, state_last, state_seeded, state_pending,             state_run_start, state_chain = state
        if (0 < state_consumed < len(bars)
                and bars[state_consumed - 1] is state_last):
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
        bucket[n] = (len(bars), bars[-1], seeded, tuple(pending),
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
    return next((bar for bar in visible
                 if bar.start_nano == previous_slot.start_nano
                 and bar.contract_id == last_bar.contract_id), None)


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

    session_by_id = {session.session_id: session for session in sessions}
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

    availability = [bar.available_at_nano for bar in consumed_bars
                    if bar.available_at_nano is not None]
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
    specs_binding = _text_hash("|".join(sorted(_spec_binding(spec) for spec in specs))
                               or "no-specs")
    calendar_binding = _text_hash("|".join(
        sorted(_session_binding(session) for session in sessions)) or "no-sessions")
    quality = DataQuality(
        status=status,
        reasons=reasons,
        temporal_basis=_temporal_basis(consumed_bars),
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
