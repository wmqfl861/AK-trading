"""Version-1 daily baselines, read-only adapted, plus v2 minute rewrites.

Revision r3 implements 变更申请-005 §3 on top of the precheck repairs:

- **Required calendar** (E02): ``baseline_values_v2`` takes a required
  ``sessions`` keyword and resolves the same planned-slot anchor as the
  ten calculators (missing anchor observation -> ``MISSING``; empty,
  uncovered or ``UNKNOWN`` calendars -> ``MISSING/calendar_unknown``).
- **Per-anchor PIT legacy adapter** (E04): ``legacy_daily_baseline`` takes
  required ``decision_nano``/``sessions`` keywords and issues one snapshot
  per daily anchor whose own visibility ``d = max(end, available)`` does
  not exceed the query cutoff; each snapshot consumes only inputs visible
  at *its* ``d`` (PIT filter first, then duplicate detection at that
  time), sliced to the contiguous declared-trading-day prefix.  Anchors
  with unknown availability are never fabricated into a known issue time
  and produce no snapshot.
- **PIT before conflict** (E03), **overflow-safe statistics** (E09),
  **ATR dependency truth** (E10) and **content-bound hashes** (E07) from
  the earlier repairs carry over unchanged.

``legacy_daily_baseline`` stays a read-only adapter over the existing v1
calculator; completed daily bars become v2 ``FeatureSnapshot`` rows with
the exact ``BASE:<name>:v2.1.0:d86400`` keys and the ``legacy-v1``
comparison label.
"""
from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from ak_trading.probability.factors import build_factor_rows
from ak_trading.futures.spec import ContractBar
from ak_trading.v2.artifacts import stable_id
from ak_trading.v2.contracts import (
    DataQuality,
    FactorSpec,
    FactorValue,
    FeatureSnapshot,
    MarketBar,
    SessionWindow,
)
from ak_trading.v2.factor_specs import BASELINE_NAMES, LEGACY_COMPARISON_NAMES, baseline_factor_key
from ak_trading.v2.factors import (
    DAILY_DURATION,
    FactorsError,
    _FactorGrid,
    _content_hash,
    _grid_hole,
    _input_hash,
    _single_contract,
    _temporal_basis,
    _visible,
)

LEGACY_LABEL = "legacy-v1"
LEGACY_SOURCE = "ak_trading.probability.factors.build_factor_rows"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_V1_WINDOW = 60  # v1 builds each row from the trailing 60-bar window


def _to_contract_bar(bar: MarketBar) -> ContractBar:
    return ContractBar(
        symbol=bar.raw_symbol,
        duration_seconds=DAILY_DURATION,
        datetime_nano=bar.datetime_nano,
        ts=_EPOCH + timedelta(microseconds=bar.datetime_nano // 1000),
        source=f"v2-adapter:{bar.source}",
        trading_day=bar.trading_day,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        open_oi=bar.open_oi,
        close_oi=bar.close_oi,
    )


_LEGACY_SPEC_CACHE: dict[str, FactorSpec] = {}


def _legacy_spec(name: str) -> FactorSpec:
    """A canonical baseline spec template for hash binding of a name."""
    if name not in _LEGACY_SPEC_CACHE:
        from ak_trading.v2.factor_specs import baseline_registry_v2
        _LEGACY_SPEC_CACHE[name] = {
            spec.source_factor_id: spec for spec in baseline_registry_v2(DAILY_DURATION)
        }[name]
    return _LEGACY_SPEC_CACHE[name]


def legacy_daily_baseline(contract_id: str, bars: tuple[MarketBar, ...], *,
                          decision_nano: int,
                          sessions: tuple[SessionWindow, ...]) -> tuple[FeatureSnapshot, ...]:
    """Per-anchor PIT daily comparison snapshots (005 §3, E04).

    ``decision_nano`` is this query's cutoff.  A daily anchor ``b`` yields
    a snapshot only when it is complete, its first-seen time is known and
    ``d = max(b.end, b.available) <= decision_nano``; the snapshot is
    issued at ``d`` and consumes exactly the prefix visible at ``d`` (PIT
    filter first, duplicate detection at that time, then the contiguous
    declared-trading-day slice).  No visible anchor returns an empty tuple.
    """
    if not isinstance(contract_id, str) or not contract_id:
        raise TypeError("contract_id_must_be_non_empty_str")
    if not isinstance(bars, tuple) or any(not isinstance(bar, MarketBar) for bar in bars):
        raise TypeError("bars_must_be_tuple_of_MarketBar")
    if isinstance(decision_nano, bool) or not isinstance(decision_nano, int):
        raise TypeError("decision_nano_must_be_int")
    if not isinstance(sessions, tuple) or any(
            not isinstance(session, SessionWindow) for session in sessions):
        raise TypeError("sessions_must_be_tuple_of_SessionWindow")
    if any(bar.duration_seconds != DAILY_DURATION for bar in bars):
        raise FactorsError("legacy_baseline_requires_daily_bars")
    _single_contract(bars)
    if bars and bars[0].contract_id != contract_id:
        raise FactorsError("bars_contract_mismatch")
    for bar in bars:
        if bar.trading_day is None:
            raise FactorsError("trading_day_missing_for_daily_snapshot")

    completed = sorted((bar for bar in bars if bar.complete),
                       key=lambda bar: (bar.start_nano, bar.datetime_nano, bar.bar_id))
    if not completed:
        return ()
    product = completed[0].product
    raw_symbol = completed[0].raw_symbol
    grid = _FactorGrid(sessions, product, DAILY_DURATION)

    snapshots: list[FeatureSnapshot] = []
    for index, bar in enumerate(completed):
        if bar.available_at_nano is None:
            continue  # unknown first-seen: never fabricated into a known issue time
        decision = max(bar.end_nano, bar.available_at_nano)
        if decision > decision_nano:
            continue  # not yet issuable within this query cutoff
        # PIT at this anchor's issue time first, then duplicate detection.
        visible_at_d = [
            earlier for earlier in completed[:index + 1]
            if earlier.available_at_nano is not None
            and earlier.end_nano <= bar.end_nano
            and earlier.available_at_nano <= decision
        ]
        seen: dict[tuple[str, int, int], MarketBar] = {}
        for candidate in sorted(visible_at_d,
                                key=lambda b: (b.start_nano, b.datetime_nano, b.bar_id)):
            key = (candidate.contract_id, candidate.duration_seconds, candidate.datetime_nano)
            existing = seen.get(key)
            if existing is not None and existing != candidate:
                raise FactorsError("conflicting_duplicate_bar")
            seen.setdefault(key, candidate)
        prefix = tuple(seen[key] for key in sorted(seen, key=lambda k: (seen[k].start_nano, k)))
        if not prefix or prefix[-1] is not bar:
            continue  # the anchor itself lost a same-time conflict; skip
        # Contiguous declared-trading-day slice ending at this anchor.
        window_bars = prefix
        if grid.slots:
            anchor_position = None
            for position, slot in enumerate(grid.slots):
                if slot.trading_day == bar.trading_day:
                    anchor_position = position
                    break
            if anchor_position is None:
                continue  # anchor day outside the declared calendar
            by_day = {earlier.trading_day for earlier in prefix}
            first = anchor_position
            while first > 0 and grid.slots[first - 1].trading_day in by_day:
                first -= 1
            slice_days = {grid.slots[position].trading_day
                          for position in range(first, anchor_position + 1)}
            window_bars = tuple(earlier for earlier in prefix
                                if earlier.trading_day in slice_days)
        window = window_bars[max(0, len(window_bars) - _V1_WINDOW):]
        rows = build_factor_rows(
            contract_id, raw_symbol, product,
            [_to_contract_bar(earlier) for earlier in window],
        )
        row = rows[-1] if rows else None
        if row is None:
            raise FactorsError("legacy_row_unavailable")
        values: list[FactorValue] = []
        for name in LEGACY_COMPARISON_NAMES:  # v1 comparison stays at 14 (006)
            value = row.values.get(name)
            if value is None:
                status = {"ready": "MISSING", "warmup": "WARMUP"}.get(row.status, "INVALID")
                reasons: tuple[str, ...] = (LEGACY_LABEL, row.reason)
            else:
                if not math.isfinite(value):
                    raise FactorsError(f"legacy_value_not_finite:{name}")
                status = "READY"
                reasons = (LEGACY_LABEL,)
            values.append(FactorValue(
                factor_key=baseline_factor_key(name, DAILY_DURATION),
                value=value,
                available_at_nano=decision,
                quality=DataQuality(
                    status=status,
                    reasons=reasons,
                    temporal_basis=_temporal_basis(window),
                    input_hash=_input_hash(_content_hash(window), _legacy_spec(name)),
                ),
            ))
        values.sort(key=lambda value: value.factor_key)
        reference_price = bar.close if bar.close is not None and bar.close > 0 else None
        atr14 = row.values.get("atr14")
        atr_points = Decimal(str(atr14)) if atr14 is not None and atr14 > 0 else None
        status = "READY" if row.status == "ready" else (
            "WARMUP" if row.status == "warmup" else "INVALID")
        quality = DataQuality(
            status=status,
            reasons=(LEGACY_LABEL, row.reason),
            temporal_basis=_temporal_basis(window),
            input_hash=_input_hash(_content_hash(window), _legacy_spec("close")),
        )
        trading_day: date = bar.trading_day
        session_id = f"DAILY:{product}:{trading_day.isoformat()}"
        snapshot_id = stable_id("snapshot", "|".join((
            contract_id, raw_symbol, product, trading_day.isoformat(), session_id,
            str(decision), str(DAILY_DURATION),
            *[earlier.bar_id for earlier in window],
            _content_hash(window),
        )))
        snapshots.append(FeatureSnapshot(
            snapshot_id=snapshot_id,
            contract_id=contract_id,
            raw_symbol=raw_symbol,
            product=product,
            trading_day=trading_day,
            session_id=session_id,
            session_kind="DAILY",
            decision_nano=decision,
            available_at_nano=decision,
            duration_seconds=DAILY_DURATION,
            source_bar_ids=tuple(earlier.bar_id for earlier in window),
            reference_price=reference_price,
            atr_points=atr_points,
            values=tuple(values),
            daily_background_id=None,
            quality=quality,
        ))
    return tuple(snapshots)


def opening_gap_value(spec, context, calendar_binding: str):
    """BASE:opening_gap_points (006 §2): true session open minus the
    immediately previous completed session's close.

    Both endpoints are located on the full declared calendar, never from
    the last stored rows: ``b0`` is the first planned complete bar of the
    anchor's session (minute: its continuous SessionWindow; daily: the
    complete trading day), ``c`` is the same-contract close bar ending
    exactly at the immediately previous declared open fragment.  Every bar
    of the session repeats the same session gap while READY.
    """
    from ak_trading.v2.factors import _factor_value, _grid_hole

    def row(value, status, reason):
        return _factor_value(spec, value, status, (reason,) if reason else (),
                             context.bars, calendar_binding, context=context)

    grid = context.grid
    if grid is None or context.status == "calendar_unknown":
        return row(None, "MISSING", "calendar_unknown")
    if context.status == "warmup":
        return row(None, "WARMUP", "insufficient_history")
    anchor = context.anchor
    bars = context.bars
    duration = grid.duration_seconds
    session_index = None
    if duration == 86400:
        # anchor identifies the trading day; its "session" is the whole day
        for index, slot in enumerate(grid.slots):
            if slot.trading_day == anchor.trading_day:
                session_index = index
                break
    else:
        for index, session in enumerate(grid.sessions):
            if session.start_nano <= anchor.start_nano < session.end_nano:
                session_index = ("s", index)
                break
    if duration == 86400:
        if session_index is None or session_index == 0:
            return row(None, "MISSING", "prior_session_close_missing")
        prior_slot = grid.slots[session_index - 1]
        b0 = grid.anchor_bar(anchor, bars)
        if b0 is None:
            return row(None, "MISSING", "expected_slot_missing")
        c = next((bar for bar in bars
                  if bar.trading_day == prior_slot.trading_day
                  and bar.contract_id == b0.contract_id), None)
        prior_end = prior_slot.end_nano
        # 006 §2 daily: the completion anchor is the prior day's last
        # planned session end; a stored 86400 row is identified by its
        # trading_day and contract, never by a natural 24h stride.  The
        # PIT-visible prefix already guarantees completion/visibility.
    else:
        _kind, index = session_index
        if index == 0:
            return row(None, "MISSING", "prior_session_close_missing")
        session = grid.sessions[index]
        prior_session = grid.sessions[index - 1]
        first_start = session.start_nano
        b0 = next((bar for bar in bars
                   if bar.start_nano == first_start
                   and bar.end_nano <= session.end_nano
                   and bar.duration_seconds == duration), None)
        if b0 is None:
            return row(None, "MISSING", "session_open_missing")
        c = next((bar for bar in bars
                  if bar.end_nano == prior_session.end_nano
                  and bar.start_nano == prior_session.end_nano - duration * 1_000_000_000
                  and bar.contract_id == b0.contract_id
                  and bar.duration_seconds == duration), None)
        prior_end = prior_session.end_nano
        if prior_end > session.start_nano:
            return row(None, "INVALID", "opening_gap_identity_mismatch")
    if b0 is None:
        return row(None, "MISSING", "session_open_missing")
    if c is None:
        # declared previous fragment without a qualifying close bar
        return row(None, "MISSING", "prior_session_close_missing")
    if b0.open is None or c.close is None:
        return row(None, "MISSING",
                   "session_open_missing" if b0.open is None
                   else "prior_session_close_missing")
    if b0.open <= 0 or c.close <= 0:
        return row(None, "INVALID", "opening_gap_price_invalid")
    return _factor_value(spec, b0.open - c.close, "READY", (), context.bars,
                         calendar_binding, context=context)


# ---------------------------------------------------------------------------
# v2 baseline re-implementation (minute and daily, same v1 windows)
# ---------------------------------------------------------------------------

def _window(bars: tuple[MarketBar, ...], count: int, hole) \
        -> tuple[tuple[MarketBar, ...], bool]:
    window = bars[-count:] if count >= 1 else ()
    holed = any(hole(a, b) for a, b in zip(window, window[1:]))
    return window, holed


def _ready(key: str, value: Decimal | float, window: tuple[MarketBar, ...],
           spec: FactorSpec, calendar: str) -> FactorValue:
    return FactorValue(
        factor_key=key, value=value,
        available_at_nano=max((bar.available_at_nano for bar in window), default=None),
        quality=DataQuality(status="READY", reasons=(),
                            temporal_basis=_temporal_basis(window),
                            input_hash=_input_hash(_content_hash(window), spec, calendar)),
    )


def _unavailable(key: str, status: str, reason: str, window: tuple[MarketBar, ...],
                 spec: FactorSpec, calendar: str) -> FactorValue:
    return FactorValue(
        factor_key=key, value=None,
        available_at_nano=max((bar.available_at_nano for bar in window), default=None),
        quality=DataQuality(status=status, reasons=(reason,),
                            temporal_basis=_temporal_basis(window),
                            input_hash=_input_hash(_content_hash(window), spec, calendar)),
    )


def _closes(window: tuple[MarketBar, ...]) -> tuple[list[Decimal] | None, str]:
    for bar in window:
        if bar.close is None:
            return None, "missing_source_value"
    for bar in window:
        if bar.close <= 0:
            return None, "non_positive_price"
    return [bar.close for bar in window], ""


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


def _prior_extreme(window: tuple[MarketBar, ...], field: str, count: int,
                   reducer) -> Decimal | None:
    prior = window[:-1]
    if len(prior) < count:
        return None
    values = [getattr(bar, field) for bar in prior[-count:]]
    if any(value is None for value in values):
        return None
    return reducer(value for value in values if value is not None)


def _atr14(window: tuple[MarketBar, ...]) -> tuple[Decimal | None, str]:
    """Arithmetic mean of the last 14 true ranges (reasons per E10)."""
    if len(window) < 15:
        return None, "insufficient_history"
    ranges: list[Decimal] = []
    for previous, current in zip(window[-15:-1], window[-14:]):
        values = (previous.close, current.high, current.low, current.close)
        if any(value is None for value in values):
            return None, "missing_source_value"
        if any(value <= 0 for value in values):
            return None, "non_positive_price"
        ranges.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
    return sum(ranges, Decimal(0)) / Decimal(14), ""


def _spec_for_name(specs: dict, name: str) -> object:
    """Resolve a BASELINE_NAMES entry onto its registry spec.

    The fourteen rolling baselines register ``source_factor_id == name``;
    the 006 opening-gap baseline registers ``"BASE:" + name``.  Both
    warmup and calendar-unknown row sets must resolve every registered
    name (precheck F-R2).
    """
    spec = specs.get(name)
    if spec is None:
        spec = specs.get(f"BASE:{name}")
    if spec is None:
        raise FactorsError(f"unknown_baseline_name:{name}")
    return spec


def _baseline_values(bars: tuple[MarketBar, ...], decision_nano: int, hole,
                     calendar_binding: str, grid: _FactorGrid | None,
                     duration: int, pre_deduped: bool = False) -> dict[str, tuple[FactorValue, tuple[MarketBar, ...]]]:
    """Internal computation returning (value, consumed window) per name.

    ``pre_deduped=True`` marks that ``bars`` already IS the PIT-deduplicated
    visible prefix (the prepared-context path), so no second full
    deduplication pass runs.
    """
    visible = bars if pre_deduped else _visible(bars, decision_nano)
    specs = {spec.source_factor_id: spec for spec in _registry(duration)}
    if not visible:
        return {name: (_unavailable(baseline_factor_key(name, duration), "WARMUP",
                                    "insufficient_history", (),
                                    _spec_for_name(specs, name),
                                    calendar_binding), ())
                for name in BASELINE_NAMES}

    results: dict[str, tuple[FactorValue, tuple[MarketBar, ...]]] = {}

    def evaluate(name: str, count: int, compute) -> None:
        window, holed = _window(visible, count, hole)
        spec = specs[name]
        key = baseline_factor_key(name, duration)
        if holed:
            results[name] = (_unavailable(key, "MISSING", "missing_slot_in_window",
                                          window, spec, calendar_binding), window)
            return
        if len(window) < count:
            results[name] = (_unavailable(key, "WARMUP", "insufficient_history",
                                           visible, spec, calendar_binding), visible)
            return
        outcome = compute(window)
        if isinstance(outcome, tuple) and len(outcome) == 2 and isinstance(outcome[0], str):
            status, reason = outcome
            results[name] = (_unavailable(key, status, reason, window, spec,
                                          calendar_binding), window)
        else:
            results[name] = (outcome, window)

    def missing_or_invalid(window: tuple[MarketBar, ...],
                           fields: tuple[str, ...]) -> tuple[str, str] | None:
        for field in fields:
            if any(getattr(bar, field) is None for bar in window):
                return "MISSING", "missing_source_value"
        for field in fields:
            if any(getattr(bar, field) <= 0 for bar in window
                   if getattr(bar, field) is not None):
                return "INVALID", "non_positive_price"
        return None

    def compute_close(window: tuple[MarketBar, ...]):
        problem = missing_or_invalid(window, ("close",))
        if problem:
            return problem
        return _ready(baseline_factor_key("close", duration), window[-1].close,
                      window, specs["close"], calendar_binding)

    def compute_prior_high(window: tuple[MarketBar, ...]):
        problem = missing_or_invalid(window[:-1], ("high",))
        if problem:
            return problem
        value = _prior_extreme(window, "high", 20, max)
        if value is None:
            return "MISSING", "missing_source_value"
        return _ready(baseline_factor_key("prior_high20", duration), value,
                      window, specs["prior_high20"], calendar_binding)

    def compute_prior_low(window: tuple[MarketBar, ...]):
        problem = missing_or_invalid(window[:-1], ("low",))
        if problem:
            return problem
        value = _prior_extreme(window, "low", 20, min)
        if value is None:
            return "MISSING", "missing_source_value"
        return _ready(baseline_factor_key("prior_low20", duration), value,
                      window, specs["prior_low20"], calendar_binding)

    def compute_atr14(window: tuple[MarketBar, ...]):
        value, reason = _atr14(window)
        if value is None:
            return ("MISSING" if reason == "missing_source_value" else "INVALID"), reason
        if value == 0:
            return FactorValue(
                factor_key=baseline_factor_key("atr14", duration), value=Decimal(0),
                available_at_nano=max((bar.available_at_nano for bar in window), default=None),
                quality=DataQuality(status="DEGRADED", reasons=("zero_true_range",),
                                    temporal_basis=_temporal_basis(window),
                                    input_hash=_input_hash(_content_hash(window),
                                                           specs["atr14"], calendar_binding)),
            )
        return _ready(baseline_factor_key("atr14", duration), value,
                      window, specs["atr14"], calendar_binding)

    def compute_er20(window: tuple[MarketBar, ...]):
        closes, reason = _closes(window)
        if closes is None:
            status = "MISSING" if reason == "missing_source_value" else "INVALID"
            return status, reason
        displacement = abs(closes[-1] - closes[0])
        total_path = sum((abs(b - a) for a, b in zip(closes, closes[1:])), Decimal(0))
        if total_path == 0:
            return "INVALID", "zero_total_path"
        return _ready(baseline_factor_key("er20", duration), displacement / total_path,
                      window, specs["er20"], calendar_binding)

    def compute_vol20(window: tuple[MarketBar, ...]):
        closes, reason = _closes(window)
        if closes is None:
            status = "MISSING" if reason == "missing_source_value" else "INVALID"
            return status, reason
        returns = [float(b / a) - 1.0 for a, b in zip(closes, closes[1:])]
        if any(not math.isfinite(value) for value in returns):
            return "INVALID", "non_finite_value"
        std = statistics.stdev(returns)
        if std == 0.0:
            return "INVALID", "zero_variance"
        return _ready(baseline_factor_key("vol20", duration), std,
                      window, specs["vol20"], calendar_binding)

    def compute_ma(name: str, window_size: int):
        def compute(window: tuple[MarketBar, ...]):
            closes, reason = _closes(window)
            if closes is None:
                status = "MISSING" if reason == "missing_source_value" else "INVALID"
                return status, reason
            return _ready(baseline_factor_key(name, duration),
                          _mean(closes[-window_size:]), window, specs[name],
                          calendar_binding)
        return compute

    def compute_zscore20(window: tuple[MarketBar, ...]):
        closes, reason = _closes(window)
        if closes is None:
            status = "MISSING" if reason == "missing_source_value" else "INVALID"
            return status, reason
        reference = closes[:-1][-20:]
        mean_decimal = _mean(reference)
        deviations = [float(value - mean_decimal) for value in reference]
        if any(not math.isfinite(value) for value in deviations):
            return "INVALID", "non_finite_value"
        std = statistics.stdev(deviations)
        if std == 0.0:
            return "INVALID", "zero_variance"
        value = float(closes[-1] - mean_decimal) / std
        if not math.isfinite(value):
            return "INVALID", "non_finite_value"
        return _ready(baseline_factor_key("zscore20", duration), value,
                      window, specs["zscore20"], calendar_binding)

    def compute_spread(window: tuple[MarketBar, ...]):
        closes, reason = _closes(window)
        if closes is None:
            status = "MISSING" if reason == "missing_source_value" else "INVALID"
            return status, reason
        atr, atr_reason = _atr14(window)
        if atr is None:
            status = "MISSING" if atr_reason == "missing_source_value" else "INVALID"
            return status, atr_reason
        if atr <= 0:
            return "INVALID", "zero_denominator"
        spread = _mean(closes[-20:]) - _mean(closes[-60:])
        return _ready(baseline_factor_key("ma_spread_atr", duration), spread / atr,
                      window, specs["ma_spread_atr"], calendar_binding)

    def compute_distance(name: str, window: tuple[MarketBar, ...]):
        closes, reason = _closes(window)
        if closes is None:
            status = "MISSING" if reason == "missing_source_value" else "INVALID"
            return status, reason
        field = "high" if name == "distance_high_atr" else "low"
        problem = missing_or_invalid(window[:-1], (field,))
        if problem:
            return problem
        extreme = _prior_extreme(window, field, 20, max if field == "high" else min)
        if extreme is None:
            return "MISSING", "missing_source_value"
        atr, atr_reason = _atr14(window)
        if atr is None:
            status = "MISSING" if atr_reason == "missing_source_value" else "INVALID"
            return status, atr_reason
        if atr <= 0:
            return "INVALID", "zero_denominator"
        if name == "distance_high_atr":
            value = (extreme - closes[-1]) / atr
        else:
            value = (closes[-1] - extreme) / atr
        return _ready(baseline_factor_key(name, duration), value,
                      window, specs[name], calendar_binding)

    def compute_volume_ratio(window: tuple[MarketBar, ...]):
        for bar in window:
            if bar.volume is None:
                return "MISSING", "missing_source_value"
        average = _mean([bar.volume for bar in window[:-1]])
        if average == 0:
            return "INVALID", "zero_denominator"
        return _ready(baseline_factor_key("volume_ratio20", duration),
                      window[-1].volume / average, window,
                      specs["volume_ratio20"], calendar_binding)

    def compute_oi_change(window: tuple[MarketBar, ...]):
        for bar in window:
            if bar.close_oi is None:
                return "MISSING", "missing_source_value"
        lagged = window[0].close_oi
        if lagged == 0:
            return "INVALID", "zero_denominator"
        return _ready(baseline_factor_key("oi_change5", duration),
                      window[-1].close_oi / lagged - Decimal(1), window,
                      specs["oi_change5"], calendar_binding)

    evaluate("close", 1, compute_close)
    evaluate("prior_high20", 21, compute_prior_high)
    evaluate("prior_low20", 21, compute_prior_low)
    evaluate("atr14", 15, compute_atr14)
    evaluate("er20", 21, compute_er20)
    evaluate("vol20", 21, compute_vol20)
    evaluate("ma20", 20, compute_ma("ma20", 20))
    evaluate("ma60", 60, compute_ma("ma60", 60))
    evaluate("ma_spread_atr", 60, compute_spread)
    evaluate("zscore20", 21, compute_zscore20)
    evaluate("distance_high_atr", 21, lambda w: compute_distance("distance_high_atr", w))
    evaluate("distance_low_atr", 21, lambda w: compute_distance("distance_low_atr", w))
    evaluate("volume_ratio20", 21, compute_volume_ratio)
    evaluate("oi_change5", 6, compute_oi_change)
    return results


_REGISTRY_CACHE: dict[int, dict[str, FactorSpec]] = {}


def _registry(duration_seconds: int) -> tuple[FactorSpec, ...]:
    from ak_trading.v2.factor_specs import baseline_registry_v2
    if duration_seconds not in _REGISTRY_CACHE:
        _REGISTRY_CACHE[duration_seconds] = {
            spec.source_factor_id: spec for spec in baseline_registry_v2(duration_seconds)
        }
    return tuple(_REGISTRY_CACHE[duration_seconds].values())


def _anchor_or_unknown(bars: tuple[MarketBar, ...], decision_nano: int,
                       sessions: tuple[SessionWindow, ...], duration: int, lookback: int):
    """Resolve the shared planned anchor or a fourteen-row unavailable set."""
    from ak_trading.v2.factors import _prepare
    context = _prepare(bars, decision_nano, sessions, duration)
    if context.status == "ok":
        calendar_binding = context.grid.binding(
            context.grid.window_start(context.anchor, lookback),
            context.anchor.end_nano)
        return context, calendar_binding
    calendar_binding = "calendar-unknown"
    return context, calendar_binding


def _uniform_rows(duration: int, status: str, reason: str,
                  consumed: tuple[MarketBar, ...], calendar: str) -> dict:
    specs = {spec.source_factor_id: spec for spec in _registry(duration)}
    return {
        name: (_unavailable(baseline_factor_key(name, duration), status, reason,
                            consumed, _spec_for_name(specs, name), calendar), consumed)
        for name in BASELINE_NAMES
    }


def baseline_value_for_spec(spec: FactorSpec, bars: tuple[MarketBar, ...],
                            decision_nano: int, hole, calendar_binding: str,
                            grid: _FactorGrid | None) -> FactorValue:
    """Compute one registered baseline spec from a pre-anchored context."""
    computed = _baseline_values(bars, decision_nano, hole, calendar_binding, grid,
                                spec.duration_seconds)
    entry = computed.get(spec.source_factor_id)
    if entry is None:
        raise FactorsError(f"unknown_baseline_name:{spec.source_factor_id}")
    value, window = entry
    return FactorValue(
        factor_key=spec.factor_key,
        value=value.value,
        available_at_nano=value.available_at_nano,
        quality=DataQuality(
            status=value.quality.status,
            reasons=value.quality.reasons,
            temporal_basis=value.quality.temporal_basis,
            input_hash=_input_hash(_content_hash(window), spec, calendar_binding),
        ),
    )


def baseline_rows_for_context(context, hole) -> dict:
    """All fourteen baseline rows computed once per prepared context.

    The rows are cached on the call-scoped context, so a 24-spec snapshot
    evaluates the shared window statistics a single time; per-spec rebinding
    below still applies each spec's own factor key and hash binding.
    """
    if context._baseline_rows is None:
        duration = context.grid.duration_seconds if context.grid is not None else 60
        context._baseline_rows = _baseline_values(
            context.bars, context.decision_nano, hole, "rows", context.grid,
            duration, pre_deduped=True)
    return context._baseline_rows


def baseline_rebind_for_spec(spec: FactorSpec, rows: dict, context,
                             calendar_binding: str) -> FactorValue:
    """Rebind one cached row onto a spec's key and hash binding."""
    entry = rows.get(spec.source_factor_id)
    if entry is None:
        raise FactorsError(f"unknown_baseline_name:{spec.source_factor_id}")
    value, window = entry
    return FactorValue(
        factor_key=spec.factor_key,
        value=value.value,
        available_at_nano=value.available_at_nano,
        quality=DataQuality(
            status=value.quality.status,
            reasons=value.quality.reasons,
            temporal_basis=value.quality.temporal_basis,
            input_hash=_input_hash(context.content_hash(window), spec,
                                   calendar_binding),
        ),
    )


def baseline_values_v2(bars: tuple[MarketBar, ...],
                       decision_nano: int, *,
                       sessions: tuple[SessionWindow, ...]) -> tuple[FactorValue, ...]:
    """The fourteen v1-window baselines, recomputed for any one duration.

    ``sessions`` is the required complete frozen plan; the planned-slot
    anchor semantics match ``compute_factor`` (005 §3).
    """
    if not isinstance(bars, tuple) or any(not isinstance(bar, MarketBar) for bar in bars):
        raise TypeError("bars_must_be_tuple_of_MarketBar")
    if isinstance(decision_nano, bool) or not isinstance(decision_nano, int):
        raise TypeError("decision_nano_must_be_int")
    if not isinstance(sessions, tuple) or any(
            not isinstance(session, SessionWindow) for session in sessions):
        raise TypeError("sessions_must_be_tuple_of_SessionWindow")
    visible = _visible(bars, decision_nano)
    durations = {bar.duration_seconds for bar in visible} or \
        {bar.duration_seconds for bar in bars}
    if len(durations) > 1:
        raise FactorsError("mixed_duration_bars")
    if not durations:
        raise FactorsError("duration_undetermined")
    duration = next(iter(durations))
    context, calendar_binding = _anchor_or_unknown(bars, decision_nano, sessions,
                                                   duration, 21)
    if context.status == "calendar_unknown":
        rows = _uniform_rows(duration, "MISSING", "calendar_unknown",
                             context.bars, "calendar-unknown")
    elif context.status == "warmup":
        rows = _uniform_rows(duration, "WARMUP", "insufficient_history",
                             context.bars, "calendar-unknown")
    elif context.status == "expected_slot_missing":
        rows = _uniform_rows(duration, "MISSING", "expected_slot_missing",
                             context.bars, calendar_binding)
    else:
        rows = _baseline_values(bars, decision_nano, _grid_hole(context.grid),
                                calendar_binding, context.grid, duration)
    values = [value for value, _window_bars in (rows[name] for name in sorted(rows))]
    # The fifteenth registered baseline (006 §2) keeps its row here too.
    # Uniform row sets already carry it via BASELINE_NAMES (F-R2); only the
    # ok path — whose rows cover the fourteen rolling names — appends it.
    if context.status == "ok":
        gap_spec = next(spec for spec in _registry(duration)
                        if spec.source_factor_id == "BASE:opening_gap_points")
        values.append(opening_gap_value(gap_spec, context,
                                        context.grid.binding(
                                            context.grid.window_start(context.anchor, 2),
                                            context.anchor.end_nano)))
    values.sort(key=lambda value: value.factor_key)
    return tuple(values)
