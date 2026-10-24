"""M3B-02: full regime/flag strategy matrix, WAIT coverage and the joint
necessary-condition thresholds (04-M3B-策略库.md §5, §10-1/2/8)."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ak_trading.v2 import clock as m1_clock
from ak_trading.v2 import policy as m1_policy
from ak_trading.v2.contracts import (
    AccountSnapshot,
    CalibrationEvidence,
    DataQuality,
    FactorValue,
    FeatureSnapshot,
    FeeLeg,
    Fill,
    FrozenRange,
    Horizon,
    MacroContext,
    MarketBar,
    MarketForecast,
    PositionLot,
    PositionSnapshot,
    RegimeAssessment,
    RunPolicy,
    SessionWindow,
    StrategyContext,
    StrategyProposal,
    StrategySpec,
    StrategyState,
    TimeProbability,
    TradingRule,
)
from ak_trading.v2.schemas import calibration_geometry_key, decode_record_file
from ak_trading.v2.strategies.registry import (
    build_strategy_spec, select_exit_binding, setup_expiry_nano,
    strategy_specs_v2)
from ak_trading.v2.strategies.state import commit_observation

NANO = 1_000_000_000
MINUTE = 60 * NANO
BASE = int(datetime(2026, 1, 12, 1, 0, tzinfo=timezone.utc).timestamp()) * NANO
HASH0 = "0" * 64
PRODUCT = "SHFE.RB"
CONTRACT = "SHFE.RB2605"
FAMILIES = ("TREND", "BREAKOUT", "PULLBACK", "REVERSION", "GRID", "MARTINGALE")


def quality() -> DataQuality:
    return DataQuality("READY", (), "observed", HASH0)


def weekdays(count: int) -> list[date]:
    days: list[date] = []
    day = date(2026, 1, 12)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def sessions(days: int = 3):
    windows = []
    for index, day in enumerate(weekdays(days)):
        day_base = BASE + index * 86400 * NANO
        windows.append(SessionWindow(f"day{index}-am", PRODUCT, day, day_base,
                                     day_base + 150 * MINUTE, "DAY", "cal-1", "SCENARIO"))
        windows.append(SessionWindow(f"day{index}-pm", PRODUCT, day,
                                     day_base + 16200 * NANO,
                                     day_base + 16200 * NANO + 90 * MINUTE, "DAY",
                                     "cal-1", "SCENARIO"))
    return tuple(windows)


def account(equity: Decimal = Decimal("1000000"), lots: tuple = ()) -> AccountSnapshot:
    return AccountSnapshot("acct-1", BASE, date(2026, 1, 12), equity, equity,
                           Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"),
                           equity, Decimal("0"), Decimal("0"), equity,
                           Decimal("0"), equity, lots, False, quality())


def rule(per_lot: Decimal = Decimal("1")) -> TradingRule:
    return TradingRule("rule-1", CONTRACT, BASE - 86400 * NANO, BASE + 365 * 86400 * NANO,
                       BASE - 86400 * NANO, Decimal("10"), Decimal("0.1"), 1, 1,
                       FeeLeg(per_lot, Decimal("0"), False),
                       FeeLeg(per_lot, Decimal("0"), False),
                       FeeLeg(per_lot, Decimal("0"), False),
                       Decimal("0.1"), Decimal("0.1"), None, None,
                       Decimal("0"), Decimal("0"), Decimal("0.1"), "SCENARIO", HASH0)


def frozen_range() -> FrozenRange:
    return FrozenRange("range-1", BASE, Decimal("90"), Decimal("100"), Decimal("110"),
                       Decimal("85"), Decimal("115"), "snap-1")


def minute_bars(closes, highs=None, lows=None, reference_slot=0):
    bars = []
    for index, close in enumerate(closes):
        close = Decimal(str(close))
        high = Decimal(str(highs[index])) if highs else close + Decimal("0.2")
        low = Decimal(str(lows[index])) if lows else close - Decimal("0.2")
        start = BASE + (reference_slot + index) * MINUTE
        bars.append(MarketBar(
            bar_id=f"b{reference_slot + index}", contract_id=CONTRACT, raw_symbol="rb2605",
            product=PRODUCT, duration_seconds=60, datetime_nano=start, start_nano=start,
            end_nano=start + MINUTE, trading_day=date(2026, 1, 12), session_id="day0-am",
            available_at_nano=start + MINUTE, imported_at_nano=None,
            open=close, high=high, low=low, close=close, volume=Decimal("1"),
            open_oi=Decimal("10"), close_oi=Decimal("10"), complete=True,
            expected_minutes=1, actual_minutes=1, source="test", quality=quality()))
    return tuple(bars)


def features(reference: Decimal, atr: Decimal, factors=None, decision: int = 0) -> FeatureSnapshot:
    decision = decision or BASE + 24 * MINUTE
    values = [FactorValue(f"BASE:{name}:v2.1.0:d60", value, decision - 1, quality())
              for name, value in (factors or {}).items()]
    values.append(FactorValue("META:price_tick:v2.1.0", Decimal("0.1"), decision - 1, quality()))
    return FeatureSnapshot("snap-1", CONTRACT, "rb2605", PRODUCT, date(2026, 1, 12),
                           "day0-am", "DAY", decision, decision - 1, 60, ("b0",),
                           reference, atr, tuple(values), None, quality())


def regime(base, flags=(), frozen=None, cross="ALIGNED") -> RegimeAssessment:
    return RegimeAssessment("assess-1", "snap-1", "ref-1", BASE + 24 * MINUTE, base,
                            tuple(flags), frozen, cross, 3, (), quality())


def evidence(event: str, bucket: str, policy_id: str, base_regime: str, horizon,
             features_snap: FeatureSnapshot, frozen: FrozenRange | None, *,
             samples=600, blocks=80, calib=0.01, status="VALID", duration=60,
             session_kind="DAY") -> CalibrationEvidence:
    if event.startswith("PERSIST:"):
        base = event[len("PERSIST:"):]
    elif event.startswith("STOP_TOUCH:"):
        base = "STOP_TOUCH"
    else:
        base = event
    range_free = base in ("UP_TREND", "DOWN_TREND", "COMPRESSION", "DIRECTION",
                          "PATH", "UP_TOUCH", "DOWN_TOUCH")
    geometry = calibration_geometry_key(features_snap, None if range_free else frozen, base)
    barrier = "ATR_1" if base == "PATH" else "COMMON"
    return CalibrationEvidence(
        evidence_id=f"ev-{event}", model_id="model-1", policy_id=policy_id, product=PRODUCT,
        session_kind=session_kind, duration_seconds=duration, base_regime=base_regime,
        barrier_key=barrier, geometry_key=geometry or "geometry-missing",
        event=event, horizon=horizon, label_version="label-v2.1.0", bucket=bucket,
        sample_count=samples, effective_blocks=blocks, calibration_error=calib, brier=0.01,
        training_end_nano=BASE - 100 * 86400 * NANO,
        calibration_end_nano=BASE - 50 * 86400 * NANO,
        valid_until_nano=BASE + 10 * 86400 * NANO, status=status)


def forecast(horizon: Horizon, *, base_regime: str, persistence: dict,
             frozen: FrozenRange | None = None, stop_touch: dict | None = None,
             rbb: float = 0.7, ambiguous: float = 0.01, terminal_unknown: float = 0.01,
             duration: int = 60, published: int = 0, expires: int | None = None,
             ood: bool = False, evidence_items=(), barrier: str = "ATR_1") -> MarketForecast:
    published = published or BASE + 19 * MINUTE
    if stop_touch is None:
        # M1 rev9: a forecast with a frozen range must carry BOTH stop-touch
        # probabilities (empty dicts only for range-free requests).
        stop_touch = {"LONG": 0.05, "SHORT": 0.05} if frozen is not None else {}
    window_start, window_end = m1_clock.horizon_window(
        published, PRODUCT, horizon, sessions())
    return MarketForecast(
        forecast_id=f"fc-{horizon.unit}{horizon.value}", request_id="req-1",
        snapshot_id="snap-1", contract_id=CONTRACT, product=PRODUCT,
        trading_day=date(2026, 1, 12), session_id="day0-am",
        session_kind="DAY" if duration == 60 else "DAILY", duration_seconds=duration,
        assessment_id="assess-1", base_regime=base_regime, regime_reference_id="ref-1",
        decision_nano=published - 1, published_at_nano=published,
        expires_at_nano=expires if expires is not None else published + 900 * NANO,
        horizon=horizon, window_start_nano=window_start, window_end_nano=window_end,
        barrier_key=barrier, reference_price=Decimal("100"), atr_points=Decimal("1"),
        upper_distance=Decimal("1"), lower_distance=Decimal("1"), frozen_range=frozen,
        label_version="label-v2.1.0", model_id="model-1",
        direction_probabilities={"UP": 0.4, "DOWN": 0.4, "NEUTRAL": 0.2},
        # F16 unit consistency under C=100 / ATR=1: return[i]=point[i]/C,
        # atr[i]=point[i]/ATR (Decimal fields stay strings; floats convert
        # via Decimal(str(value)) as in the shared numeric policy).
        return_quantiles={"Q05": 0.98, "Q25": 0.99, "Q50": 1.0, "Q75": 1.01, "Q95": 1.02},
        point_quantiles={"Q05": Decimal("98"), "Q25": Decimal("99"), "Q50": Decimal("100"),
                         "Q75": Decimal("101"), "Q95": Decimal("102")},
        atr_quantiles={"Q05": 98.0, "Q25": 99.0, "Q50": 100.0, "Q75": 101.0, "Q95": 102.0},
        path_probabilities={"UP_FIRST": 0.45, "DOWN_FIRST": 0.44,
                            "NEITHER": 0.11 - ambiguous, "AMBIGUOUS": ambiguous},
        time_cdf=(TimeProbability("RETURN_FIRST", horizon.value, 0.6, terminal_unknown),
                  TimeProbability("BREAK_FIRST", horizon.value, 0.3, terminal_unknown)),
        persistence_probabilities=dict(persistence),
        stop_touch_probabilities=dict(stop_touch or {}),
        return_before_break_probability=rbb,
        evidence=tuple(evidence_items), out_of_distribution=ood, quality=quality())


def default_exit_binding(strategy_id):
    """P0 placeholder retained only for direct StrategyContext constructions;
    make_context/minimal_context inject the real pre-OPEN selector."""
    from ak_trading.v2.strategies.registry import build_strategy_spec, select_exit_binding
    mode = "MINUTE" if strategy_id.endswith("_m1_v2") else "DAILY"
    family = strategy_id.rsplit("_", 2)[0].upper()
    return select_exit_binding(build_strategy_spec(family, mode), "legacy")


NO_RULE = object()  # sentinel: pass an explicitly absent TradingRule


def make_context(spec: StrategySpec, *, bars=(), forecasts=(), regime_obj=None,
                 features_snap=None, account_snap=None, position=None,
                 macro_mode="PRICE_ONLY", now=None, rule_obj=None,
                 sessions_override=None, now_override=None) -> StrategyContext:
    last_end = bars[-1].end_nano if bars else BASE + 24 * MINUTE
    now = now_override or now or last_end
    calendar = sessions_override if sessions_override is not None else sessions()
    if features_snap is None:
        features_snap = features(Decimal("100"), Decimal("1"), decision=now)
    if regime_obj is None:
        regime_obj = regime("UNKNOWN")
    return StrategyContext(
        now_nano=now, spec=spec, exit_binding=select_exit_binding(spec, "legacy"),
        features=features_snap, forecasts=tuple(forecasts),
        regime=regime_obj,
        macro=MacroContext("macro-1", now, (), "macro-risk-v2.1.0", (), macro_mode, quality()),
        bars=tuple(bars), sessions=calendar, account=account_snap or account(),
        position=position,
        rule=None if rule_obj is NO_RULE else (rule_obj or rule()),
        confidence_policy=m1_policy.confidence_policy(spec.family),
        stress_policy=m1_policy.stress_policy(),
        run_policy=RunPolicy("RESEARCH_EVALUATION", "BEST_EV", 7, None, None,
                             "research-policy-v2.1.0", "macro-risk-v2.1.0",
                             macro_mode if macro_mode != "BLOCKED" else "PRICE_ONLY", "BASE"),
        qualification=None,
        model_hashes={"forecast": HASH0, "payoff": HASH0, "regime_reference": HASH0},
        holding_deadline_nano=m1_clock.holding_deadline(
            now, features_snap.product, spec.holding_policy, calendar))


def ordinary_evidence(features_snap, frozen, base_regime, horizon):
    policy = m1_policy.confidence_policy("REVERSION").policy_id
    return (
        evidence("PERSIST:ORDERED_RANGE", "[0.90,0.95)", policy, base_regime, horizon,
                 features_snap, frozen),
        evidence("PATH", "ALL", policy, base_regime, horizon, features_snap, frozen),
        evidence("RETURN_BEFORE_BREAK", "[0.70,0.75)", policy, base_regime, horizon,
                 features_snap, frozen),
    )


def martingale_evidence(features_snap, frozen, horizon):
    policy = m1_policy.confidence_policy("MARTINGALE").policy_id
    return (
        evidence("PERSIST:ORDERED_RANGE", "[0.90,0.95)", policy, "ORDERED_RANGE", horizon,
                 features_snap, frozen, samples=600, blocks=80, calib=0.01),
        evidence("PATH", "ALL", policy, "ORDERED_RANGE", horizon, features_snap, frozen),
        evidence("RETURN_BEFORE_BREAK", "[0.70,0.75)", policy, "ORDERED_RANGE", horizon,
                 features_snap, frozen),
        evidence("STOP_TOUCH:LONG", "[0.05,0.10]", policy, "ORDERED_RANGE", horizon,
                 features_snap, frozen),
        evidence("STOP_TOUCH:SHORT", "[0.05,0.10]", policy, "ORDERED_RANGE", horizon,
                 features_snap, frozen),
    )


def range_forecast_builder(persistence=0.9, stop_touch=0.05, rbb=0.7,
                           terminal_unknown=0.01, ambiguous=0.01, martingale=False):
    """Per-decision forecasts: geometry evidence always matches the
    evaluation snapshot's own C bucket (the evidence identity checks run
    against each decision's features)."""

    def build(features_snap):
        return range_forecasts(features_snap, persistence=persistence,
                               stop_touch=stop_touch, rbb=rbb,
                               terminal_unknown=terminal_unknown,
                               ambiguous=ambiguous, martingale=martingale)

    return build


def range_forecasts(features_snap, persistence=0.9, stop_touch=0.05, rbb=0.7,
                    terminal_unknown=0.01, ambiguous=0.01, martingale=False):
    horizon = Horizon("TRADING_MINUTE", 30)
    frozen = frozen_range()
    if martingale:
        items = martingale_evidence(features_snap, frozen, horizon)
        return (forecast(horizon, base_regime="ORDERED_RANGE",
                         persistence={"ORDERED_RANGE": persistence},
                         frozen=frozen, stop_touch={"LONG": stop_touch, "SHORT": stop_touch},
                         rbb=rbb, terminal_unknown=terminal_unknown, ambiguous=ambiguous,
                         evidence_items=items),)
    items = ordinary_evidence(features_snap, frozen, "ORDERED_RANGE", horizon)
    return (forecast(horizon, base_regime="ORDERED_RANGE",
                     persistence={"ORDERED_RANGE": persistence}, frozen=frozen,
                     rbb=rbb, terminal_unknown=terminal_unknown, ambiguous=ambiguous,
                     evidence_items=items),)


def registry(mode="MINUTE"):
    from ak_trading.v2.strategies.registry import strategy_registry_v2
    return strategy_registry_v2(mode)


def drive_open(family: str, *, regime_base, flags=(), frozen=None, bars_sequence=(),
               features_sequence=(), forecasts=(), forecast_builder=None,
               persistence=0.9, stop_touch=0.05,
               martingale=False, macro_mode="PRICE_ONLY", rule_override=None):
    """Evaluate the family over successive bar prefixes until OPEN or exhausted."""
    strategy = registry()[f"{family.lower()}_m1_v2"]
    state = None
    proposal = None
    for bars, features_snap in zip(bars_sequence, features_sequence):
        if forecast_builder is not None:
            per_eval_forecasts = forecast_builder(features_snap)
        else:
            per_eval_forecasts = forecasts or range_forecasts(
                features_snap, persistence=persistence, stop_touch=stop_touch,
                martingale=martingale)
        context = make_context(
            strategy.spec, bars=bars, forecasts=per_eval_forecasts,
            regime_obj=regime(regime_base, flags, frozen),
            features_snap=features_snap, macro_mode=macro_mode,
            rule_obj=rule_override() if callable(rule_override) else rule_override)
        proposal = strategy.evaluate(context, state)
        state = commit_observation(state, proposal)
        if proposal.action == "OPEN":
            return proposal, state
    return proposal, state


def range_bars_sequence():
    closes_arming = [100 - index * 0.35 for index in range(20)]  # drift down to 93.25
    bars1 = minute_bars(closes_arming)
    bars2 = minute_bars(closes_arming + [96])
    bars3 = minute_bars(closes_arming + [96, 96.2])
    snap1 = features(Decimal("93.25"), Decimal("1"))
    snap2 = features(Decimal("96"), Decimal("1"))
    snap3 = features(Decimal("96.2"), Decimal("1"))
    return (bars1, bars2, bars3), (snap1, snap2, snap3)


def trend_bars_sequence(rising=True):
    closes = [100 + index for index in range(24)] if rising else [121 - index for index in range(24)]
    bars1, bars2, bars3 = minute_bars(closes[:22]), minute_bars(closes[:23]), minute_bars(closes[:24])
    if rising:
        snaps = (features(Decimal(str(closes[21])), Decimal("4"),
                          {"close": Decimal("121"), "ma20": Decimal("110"), "ma60": Decimal("100"), "atr14": Decimal("4"), "er20": Decimal("0.5")}),
                 features(Decimal(str(closes[22])), Decimal("4"),
                          {"close": Decimal("122"), "ma20": Decimal("110"), "ma60": Decimal("100"), "atr14": Decimal("4"), "er20": Decimal("0.5")}),
                 features(Decimal(str(closes[23])), Decimal("4"),
                          {"close": Decimal("123"), "ma20": Decimal("110"), "ma60": Decimal("100"), "atr14": Decimal("4"), "er20": Decimal("0.5")}))
    else:
        snaps = (features(Decimal(str(closes[21])), Decimal("4"),
                          {"close": Decimal("100"), "ma20": Decimal("100"), "ma60": Decimal("110"), "atr14": Decimal("4"), "er20": Decimal("0.5")}),
                 features(Decimal(str(closes[22])), Decimal("4"),
                          {"close": Decimal("99"), "ma20": Decimal("100"), "ma60": Decimal("110"), "atr14": Decimal("4"), "er20": Decimal("0.5")}),
                 features(Decimal(str(closes[23])), Decimal("4"),
                          {"close": Decimal("98"), "ma20": Decimal("100"), "ma60": Decimal("110"), "atr14": Decimal("4"), "er20": Decimal("0.5")}))
    return (bars1, bars2, bars3), snaps


def breakout_bars_sequence(up=True):
    flat_high = [100] * 20
    flat_low = [90] * 20
    if up:
        closes = [95] * 20 + [100.3, 100.4, 100.5]
    else:
        closes = [95] * 20 + [89.7, 89.6, 89.5]
    highs = flat_high + ([100.4, 100.5, 100.6] if up else [95] * 3)
    lows = flat_low + ([95] * 3 if up else [89.6, 89.5, 89.4])
    return (minute_bars(closes[:21], highs[:21], lows[:21]),
            minute_bars(closes[:22], highs[:22], lows[:22]),
            minute_bars(closes[:23], highs[:23], lows[:23]))


def pullback_bars_sequence(up=True):
    if up:
        closes = [105] * 10 + [105, 105.5, 105.6]
    else:
        closes = [95] * 10 + [95, 94.5, 94.4]
    if up:
        snaps = (features(Decimal("105"), Decimal("2"), {"close": Decimal("105"), "ma20": Decimal("105"),
                                                          "ma60": Decimal("100"), "atr14": Decimal("2")}),
                 features(Decimal("105.5"), Decimal("2"), {"close": Decimal("105.5"), "ma20": Decimal("105"),
                                                           "ma60": Decimal("100"), "atr14": Decimal("2")}),
                 features(Decimal("105.6"), Decimal("2"), {"close": Decimal("105.6"), "ma20": Decimal("105"),
                                                           "ma60": Decimal("100"), "atr14": Decimal("2")}))
    else:
        snaps = (features(Decimal("95"), Decimal("2"), {"close": Decimal("95"), "ma20": Decimal("95"),
                                                        "ma60": Decimal("100"), "atr14": Decimal("2")}),
                 features(Decimal("94.5"), Decimal("2"), {"close": Decimal("94.5"), "ma20": Decimal("95"),
                                                          "ma60": Decimal("100"), "atr14": Decimal("2")}),
                 features(Decimal("94.4"), Decimal("2"), {"close": Decimal("94.4"), "ma20": Decimal("95"),
                                                          "ma60": Decimal("100"), "atr14": Decimal("2")}))
    return (minute_bars(closes[:11]), minute_bars(closes[:12]), minute_bars(closes[:13])), snaps


# --- row 1/2: trends ----------------------------------------------------------


def test_uptrend_row_candidates_and_mirrors():
    bars, snaps = trend_bars_sequence(True)
    horizon = Horizon("TRADING_MINUTE", 30)
    trend_forecasts = (forecast(
        horizon, base_regime="UP_TREND", persistence={"UP_TREND": 0.75},
        evidence_items=(
            evidence("PERSIST:UP_TREND", "[0.75,0.80)",
                     m1_policy.confidence_policy("TREND").policy_id, "UP_TREND", horizon,
                     snaps[-1], None),
            evidence("PATH", "ALL", m1_policy.confidence_policy("TREND").policy_id,
                     "UP_TREND", horizon, snaps[-1], None))),)
    proposal, _ = drive_open("TREND", regime_base="UP_TREND", bars_sequence=bars,
                             features_sequence=snaps, forecasts=trend_forecasts)
    assert proposal.action == "OPEN" and proposal.next_state.direction == 1

    from ak_trading.v2.probability.regimes import freeze_range
    breakout_bars = breakout_bars_sequence(True)
    breakout_snaps = tuple(
        features(visible[-1].close, Decimal("1"),
                 {"prior_low20": Decimal("90"), "prior_high20": Decimal("100")},
                 decision=visible[-1].end_nano)
        for visible in breakout_bars)
    # SYNTHETIC V03 seam: the real M3A producer freezes the first completed
    # prefix. Every later context/forecast carries this exact original.
    setup_range = freeze_range(breakout_snaps[0])
    assert setup_range is not None
    assert setup_range.frozen_at_nano == breakout_bars[0][-1].end_nano

    def current_breakout_forecasts(snap):
        return (forecast(
            horizon, base_regime="COMPRESSION", persistence={"UP_BREAKOUT": 0.8},
            frozen=setup_range, published=snap.decision_nano,
            evidence_items=(
                evidence("PERSIST:UP_BREAKOUT", "[0.80,0.85)",
                         m1_policy.confidence_policy("BREAKOUT").policy_id,
                         "COMPRESSION", horizon, snap, setup_range),
                evidence("PATH", "ALL", m1_policy.confidence_policy("BREAKOUT").policy_id,
                         "COMPRESSION", horizon, snap, setup_range))),)

    strategy = registry()["breakout_m1_v2"]
    state = None
    confirmations = []
    for visible, snap in zip(breakout_bars, breakout_snaps):
        assessment = replace(regime("COMPRESSION", ("BREAKOUT",), setup_range),
                             decision_nano=snap.decision_nano)
        context = make_context(
            strategy.spec, bars=visible, features_snap=snap, regime_obj=assessment,
            forecasts=current_breakout_forecasts(snap), now=snap.decision_nano)
        proposal = strategy.evaluate(context, state)
        state = commit_observation(state, proposal)
        confirmations.append(proposal.next_state.confirmation_count)
        assert proposal.next_state.setup.frozen_range == setup_range
    assert confirmations == [1, 2, 3]
    assert proposal.action == "OPEN"

    pullback_forecasts = (forecast(
        horizon, base_regime="UP_TREND", persistence={"UP_TREND": 0.75},
        published=BASE + 9 * MINUTE,
        evidence_items=(
            evidence("PERSIST:UP_TREND", "[0.75,0.80)",
                     m1_policy.confidence_policy("PULLBACK").policy_id, "UP_TREND",
                     horizon, snaps[-1], None),
            evidence("PATH", "ALL", m1_policy.confidence_policy("PULLBACK").policy_id,
                     "UP_TREND", horizon, snaps[-1], None))),)
    pb_bars, pb_snaps = pullback_bars_sequence(True)
    proposal, _ = drive_open("PULLBACK", regime_base="UP_TREND", flags=("PULLBACK",),
                             bars_sequence=pb_bars, features_sequence=pb_snaps,
                             forecasts=pullback_forecasts)
    assert proposal.action == "OPEN" and proposal.next_state.direction == 1


@pytest.mark.parametrize("family", ["REVERSION", "GRID", "MARTINGALE"])
def test_uptrend_row_blocks_countertrend_range_families(family):
    bars, snaps = range_bars_sequence()
    proposal, _ = drive_open(family, regime_base="UP_TREND", bars_sequence=bars,
                             features_sequence=snaps,
                             frozen=frozen_range(), martingale=(family == "MARTINGALE"))
    assert proposal.action == "WAIT"
    assert "REGIME_NOT_ALLOWED" in proposal.reasons


def test_downtrend_row_mirror():
    bars, snaps = trend_bars_sequence(False)
    horizon = Horizon("TRADING_MINUTE", 30)
    down_forecasts = (forecast(
        horizon, base_regime="DOWN_TREND", persistence={"DOWN_TREND": 0.75},
        evidence_items=(
            evidence("PERSIST:DOWN_TREND", "[0.75,0.80)",
                     m1_policy.confidence_policy("TREND").policy_id, "DOWN_TREND",
                     horizon, snaps[-1], None),
            evidence("PATH", "ALL", m1_policy.confidence_policy("TREND").policy_id,
                     "DOWN_TREND", horizon, snaps[-1], None))),)
    proposal, _ = drive_open("TREND", regime_base="DOWN_TREND", bars_sequence=bars,
                             features_sequence=snaps, forecasts=down_forecasts)
    assert proposal.action == "OPEN" and proposal.next_state.direction == -1


# --- row 3: ordered range -------------------------------------------------------


@pytest.mark.parametrize("family", ["REVERSION", "GRID", "MARTINGALE"])
def test_ordered_range_row_candidates(family):
    bars, snaps = range_bars_sequence()
    proposal, _ = drive_open(family, regime_base="ORDERED_RANGE", bars_sequence=bars,
                             features_sequence=snaps, frozen=frozen_range(),
                             martingale=(family == "MARTINGALE"))
    assert proposal.action == "OPEN"
    assert proposal.stop_price == Decimal("85")
    assert proposal.target_price == Decimal("100")


def test_ordered_range_middle_never_fills():
    strategy = registry()["reversion_m1_v2"]
    closes = [100] * 12
    snap = features(Decimal("100"), Decimal("1"))
    proposal = strategy.evaluate(
        make_context(strategy.spec, bars=minute_bars(closes),
                     forecasts=range_forecasts(snap),
                     regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
                     features_snap=snap), None)
    assert proposal.action == "WAIT"
    assert "RANGE_MIDDLE_WAIT" in proposal.reasons


def test_ordered_range_cost_gate_blocks():
    bars, snaps = range_bars_sequence()
    strategy = registry()["reversion_m1_v2"]
    expensive = rule(per_lot=Decimal("15"))
    # Drive to the confirm bar, then swap in expensive round-trip costs.
    state = None
    for bars_i, snap_i in zip(bars[:2], snaps[:2]):
        proposal = strategy.evaluate(
            make_context(strategy.spec, bars=bars_i, forecasts=range_forecasts(snap_i),
                         regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
                         features_snap=snap_i), state)
        state = commit_observation(state, proposal)
    proposal = strategy.evaluate(
        make_context(strategy.spec, bars=bars[-1], forecasts=range_forecasts(snaps[-1]),
                     regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
                     features_snap=snaps[-1], rule_obj=expensive), state)
    assert proposal.action == "WAIT"
    assert "COST_COVERAGE_INSUFFICIENT" in proposal.reasons


# --- rows 4/5: wide range and compression ----------------------------------------


@pytest.mark.parametrize("family", ["TREND", "BREAKOUT", "PULLBACK", "REVERSION", "GRID",
                                    "MARTINGALE"])
def test_wide_range_row_default_blocks_every_family(family):
    bars, snaps = range_bars_sequence()
    proposal, _ = drive_open(family, regime_base="WIDE_RANGE", bars_sequence=bars,
                             features_sequence=snaps, frozen=frozen_range(),
                             martingale=(family == "MARTINGALE"))
    assert proposal.action == "WAIT"
    assert "REGIME_NOT_ALLOWED" in proposal.reasons


def test_fifty_fifty_direction_does_not_make_ordered_range():
    strategy = registry()["reversion_m1_v2"]
    snap = features(Decimal("93"), Decimal("1"))
    horizon = Horizon("TRADING_MINUTE", 30)
    forecasts = (forecast(horizon, base_regime="WIDE_RANGE",
                          persistence={"ORDERED_RANGE": 0.9}, frozen=frozen_range(),
                          evidence_items=ordinary_evidence(snap, frozen_range(),
                                                           "WIDE_RANGE", horizon)),)
    proposal = strategy.evaluate(
        make_context(strategy.spec, bars=minute_bars([100 - 7 + i * 0.5 for i in range(20)]),
                     forecasts=forecasts, regime_obj=regime("WIDE_RANGE", (), frozen_range()),
                     features_snap=snap), None)
    assert proposal.action == "WAIT"
    assert "REGIME_NOT_ALLOWED" in proposal.reasons
    assert forecasts[0].direction_probabilities["UP"] == forecasts[0].direction_probabilities["DOWN"]


@pytest.mark.parametrize("family", ["TREND", "PULLBACK", "REVERSION", "GRID", "MARTINGALE"])
def test_compression_row_waits(family):
    bars, snaps = range_bars_sequence()
    proposal, _ = drive_open(family, regime_base="COMPRESSION", bars_sequence=bars,
                             features_sequence=snaps, frozen=frozen_range(),
                             martingale=(family == "MARTINGALE"))
    assert proposal.action == "WAIT"
    assert "REGIME_NOT_ALLOWED" in proposal.reasons


# --- row 8/9: hard risk, unknown -------------------------------------------------


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("flag", ["EVENT_SHOCK", "ILLIQUID"])
def test_event_rows_block_all_new_risk(family, flag):
    bars, snaps = range_bars_sequence() if family in ("REVERSION", "GRID", "MARTINGALE") \
        else trend_bars_sequence(True)
    proposal, _ = drive_open(family, regime_base="UP_TREND", flags=(flag,),
                             bars_sequence=bars, features_sequence=snaps,
                             frozen=frozen_range(), martingale=(family == "MARTINGALE"))
    assert proposal.action == "WAIT"
    assert "EVENT_LIQUIDITY_RISK" in proposal.reasons \
        or "FLAG_NOT_ALLOWED" in proposal.reasons


@pytest.mark.parametrize("family", FAMILIES)
def test_unknown_row_never_opens(family):
    bars, snaps = range_bars_sequence() if family in ("REVERSION", "GRID", "MARTINGALE") \
        else trend_bars_sequence(True)
    proposal, _ = drive_open(family, regime_base="UNKNOWN", bars_sequence=bars,
                             features_sequence=snaps, frozen=frozen_range(),
                             martingale=(family == "MARTINGALE"))
    assert proposal.action == "WAIT"
    assert "REGIME_NOT_ALLOWED" in proposal.reasons


def test_unknown_with_position_cancels_adds_and_exits():
    from ak_trading.v2.strategies.martingale import LimitedMartingale
    spec = build_strategy_spec("MARTINGALE", "MINUTE")
    snap = features(Decimal("92"), Decimal("1"))
    horizon = Horizon("TRADING_MINUTE", 30)
    forecasts = range_forecasts(snap, martingale=True)
    window_end = forecasts[0].window_end_nano
    strategy = LimitedMartingale(spec)
    holding = holding_state(spec, window_end)
    context = make_context(
        spec, bars=minute_bars([100 - i * 0.4 for i in range(24)]), forecasts=forecasts,
        regime_obj=regime("UNKNOWN", (), frozen_range()), features_snap=snap,
        account_snap=holding_account(spec, holding),
        position=PositionSnapshot(CONTRACT, spec.strategy_id, holding.episode_id or "", 1,
                                  Decimal("95"), False))
    proposal = strategy.evaluate(context, holding)
    assert proposal.action == "EXIT"
    assert "REGIME_UNKNOWN" in proposal.reasons
    assert proposal.next_state.pending_layer_index is None


def holding_state(spec: StrategySpec, window_end: int) -> StrategyState:
    from ak_trading.v2.strategies.state import freeze_round_budget
    snap = features(Decimal("96"), Decimal("1"))
    budget = freeze_round_budget(spec, make_context(
        spec, bars=minute_bars([95]), forecasts=range_forecasts(snap, martingale=True),
        regime_obj=regime("ORDERED_RANGE", (), frozen_range()), features_snap=snap),
        1, "episode-x")
    assert budget is not None
    return StrategyState(
        strategy_id=spec.strategy_id, episode_id="episode-x", contract_id=CONTRACT,
        phase="HOLDING", setup=None, last_regime=None, direction=1,
        filled_layers=(0,) if len(budget.layers) > 1 else (0,),
        layer_filled_lots=tuple(
            budget.layers[index].lots if index == 0 else 0
            for index in range(len(budget.layers))),
        pending_layer_index=None, active_exit_nano=window_end, pending_exit_nano=None,
        pending_intent_ids=(), budget=budget, confirmation_count=3,
        cooldown_until_nano=None, last_event_id=None)


def holding_account(spec: StrategySpec, state: StrategyState) -> AccountSnapshot:
    lots = (PositionLot("lot-1", CONTRACT, spec.strategy_id, state.episode_id or "",
                        1, 1, BASE + 10 * MINUTE, date(2026, 1, 12), Decimal("95"),
                        Decimal("1")),)
    return account(Decimal("1000000"), lots)


# --- thresholds (§10-2) -----------------------------------------------------------


@pytest.mark.parametrize("persistence,opens", [(0.699999, False), (0.70, True)])
def test_ordinary_persistence_boundary(persistence, opens):
    bars, snaps = range_bars_sequence()
    horizon = Horizon("TRADING_MINUTE", 30)
    snap = snaps[-1]
    frozen = frozen_range()
    items = (
        evidence("PERSIST:ORDERED_RANGE",
                 "[0.70,0.75)" if persistence < 0.75 else "[0.75,0.80)",
                 m1_policy.confidence_policy("REVERSION").policy_id, "ORDERED_RANGE",
                 horizon, snap, frozen),
        evidence("PATH", "ALL", m1_policy.confidence_policy("REVERSION").policy_id,
                 "ORDERED_RANGE", horizon, snap, frozen),
        evidence("RETURN_BEFORE_BREAK", "[0.70,0.75)",
                 m1_policy.confidence_policy("REVERSION").policy_id, "ORDERED_RANGE",
                 horizon, snap, frozen))
    proposal, _ = drive_open("REVERSION", regime_base="ORDERED_RANGE", bars_sequence=bars,
                             features_sequence=snaps, frozen=frozen,
                             forecast_builder=_ordinary_builder(persistence, opens))
    assert (proposal.action == "OPEN") is opens
    if not opens:
        assert "PERSISTENCE_BELOW_MINIMUM" in proposal.reasons


def _ordinary_builder(persistence, opens):
    policy = m1_policy.confidence_policy("REVERSION").policy_id
    bucket = "[0.70,0.75)" if persistence >= 0.70 else "[0.65,0.70)"

    def build(features_snap):
        horizon = Horizon("TRADING_MINUTE", 30)
        frozen = frozen_range()
        items = (
            evidence("PERSIST:ORDERED_RANGE", bucket, policy, "ORDERED_RANGE",
                     horizon, features_snap, frozen),
            evidence("PATH", "ALL", policy, "ORDERED_RANGE", horizon, features_snap,
                     frozen),
            evidence("RETURN_BEFORE_BREAK", "[0.70,0.75)", policy, "ORDERED_RANGE",
                     horizon, features_snap, frozen))
        return (forecast(horizon, base_regime="ORDERED_RANGE",
                         persistence={"ORDERED_RANGE": persistence}, frozen=frozen,
                         evidence_items=items),)

    return build


@pytest.mark.parametrize("persistence,opens", [(0.849999, False), (0.85, True)])
def test_martingale_persistence_boundary(persistence, opens):
    bars, snaps = range_bars_sequence()
    proposal, _ = drive_open("MARTINGALE", regime_base="ORDERED_RANGE", bars_sequence=bars,
                             features_sequence=snaps, frozen=frozen_range(),
                             forecast_builder=_martingale_builder(persistence, 0.05, opens))
    assert (proposal.action == "OPEN") is opens
    if not opens:
        assert "PERSISTENCE_BELOW_MINIMUM" in proposal.reasons


def _martingale_builder(persistence, stop_touch, opens):
    policy = m1_policy.confidence_policy("MARTINGALE").policy_id
    if persistence >= 0.90:
        persist_bucket = "[0.90,0.95)"
    elif persistence >= 0.85:
        persist_bucket = "[0.85,0.90)"
    else:
        persist_bucket = "[0.80,0.85)"
    stop_bucket = "[0.05,0.10]" if stop_touch <= 0.10 else "(0.10,0.20]"

    def build(features_snap):
        horizon = Horizon("TRADING_MINUTE", 30)
        frozen = frozen_range()
        items = (
            evidence("PERSIST:ORDERED_RANGE", persist_bucket, policy, "ORDERED_RANGE",
                     horizon, features_snap, frozen),
            evidence("PATH", "ALL", policy, "ORDERED_RANGE", horizon, features_snap,
                     frozen),
            evidence("RETURN_BEFORE_BREAK", "[0.70,0.75)", policy, "ORDERED_RANGE",
                     horizon, features_snap, frozen),
            evidence("STOP_TOUCH:LONG", stop_bucket, policy, "ORDERED_RANGE", horizon,
                     features_snap, frozen),
            evidence("STOP_TOUCH:SHORT", stop_bucket, policy, "ORDERED_RANGE", horizon,
                     features_snap, frozen))
        return (forecast(horizon, base_regime="ORDERED_RANGE",
                         persistence={"ORDERED_RANGE": persistence}, frozen=frozen,
                         stop_touch={"LONG": stop_touch, "SHORT": stop_touch},
                         evidence_items=items),)

    return build


@pytest.mark.parametrize("stop_touch,opens", [(0.10, True), (0.100001, False)])
def test_martingale_stop_touch_boundary(stop_touch, opens):
    bars, snaps = range_bars_sequence()
    horizon = Horizon("TRADING_MINUTE", 30)
    snap = snaps[-1]
    frozen = frozen_range()
    policy = m1_policy.confidence_policy("MARTINGALE").policy_id
    stop_bucket = "[0.05,0.10]" if stop_touch <= 0.10 else "(0.10,0.20]"
    items = (
        evidence("PERSIST:ORDERED_RANGE", "[0.90,0.95)", policy, "ORDERED_RANGE", horizon,
                 snap, frozen),
        evidence("PATH", "ALL", policy, "ORDERED_RANGE", horizon, snap, frozen),
        evidence("RETURN_BEFORE_BREAK", "[0.70,0.75)", policy, "ORDERED_RANGE", horizon,
                 snap, frozen),
        evidence("STOP_TOUCH:LONG", stop_bucket, policy, "ORDERED_RANGE", horizon, snap, frozen),
        evidence("STOP_TOUCH:SHORT", stop_bucket, policy, "ORDERED_RANGE", horizon, snap, frozen))
    proposal, _ = drive_open("MARTINGALE", regime_base="ORDERED_RANGE", bars_sequence=bars,
                             features_sequence=snaps, frozen=frozen,
                             forecast_builder=_martingale_builder(0.9, stop_touch, opens))
    assert (proposal.action == "OPEN") is opens
    if not opens:
        assert "STOP_TOUCH_EXCEEDED" in proposal.reasons


def test_calibration_failed_blocks():
    bars, snaps = range_bars_sequence()
    horizon = Horizon("TRADING_MINUTE", 30)
    snap = snaps[-1]
    frozen = frozen_range()
    policy = m1_policy.confidence_policy("REVERSION").policy_id
    items = (
        evidence("PERSIST:ORDERED_RANGE", "[0.90,0.95)", policy, "ORDERED_RANGE", horizon,
                 snap, frozen, status="FAILED"),
        evidence("PATH", "ALL", policy, "ORDERED_RANGE", horizon, snap, frozen),
        evidence("RETURN_BEFORE_BREAK", "[0.70,0.75)", policy, "ORDERED_RANGE", horizon,
                 snap, frozen))
    forecasts = (forecast(horizon, base_regime="ORDERED_RANGE",
                          persistence={"ORDERED_RANGE": 0.9}, frozen=frozen,
                          evidence_items=items),)
    proposal, _ = drive_open("REVERSION", regime_base="ORDERED_RANGE", bars_sequence=bars,
                             features_sequence=snaps, frozen=frozen, forecasts=forecasts)
    assert proposal.action == "WAIT"
    assert "EVIDENCE_FAILED" in proposal.reasons


def test_enough_rows_but_not_enough_date_blocks_blocks():
    bars, snaps = range_bars_sequence()
    horizon = Horizon("TRADING_MINUTE", 30)
    snap = snaps[-1]
    frozen = frozen_range()
    policy = m1_policy.confidence_policy("REVERSION").policy_id
    items = list(ordinary_evidence(snap, frozen, "ORDERED_RANGE", horizon))
    items[0] = evidence("PERSIST:ORDERED_RANGE", "[0.90,0.95)", policy, "ORDERED_RANGE",
                        horizon, snap, frozen, samples=500, blocks=10)
    forecasts = (forecast(horizon, base_regime="ORDERED_RANGE",
                          persistence={"ORDERED_RANGE": 0.9}, frozen=frozen,
                          evidence_items=items),)
    proposal, _ = drive_open("REVERSION", regime_base="ORDERED_RANGE", bars_sequence=bars,
                             features_sequence=snaps, frozen=frozen, forecasts=forecasts)
    assert proposal.action == "WAIT"
    assert "EVIDENCE_INSUFFICIENT_BLOCKS" in proposal.reasons


def test_ood_and_expired_and_macro_blocked_each_block():
    bars, snaps = range_bars_sequence()
    strategy = registry()["reversion_m1_v2"]
    horizon = Horizon("TRADING_MINUTE", 30)

    def evaluate_with(**overrides):
        snap = snaps[-1]
        frozen = frozen_range()
        forecasts = overrides.get("forecasts") or range_forecasts(snap)
        if overrides.get("ood"):
            new = list(forecasts[0].__dict__.items())
            payload = dict(new)
            payload["out_of_distribution"] = True
            forecasts = (MarketForecast(**payload),)
        if overrides.get("expired"):
            payload = dict(forecasts[0].__dict__)
            payload["expires_at_nano"] = BASE + 19 * MINUTE + 10 * NANO
            forecasts = (MarketForecast(**payload),)
        state = None
        for bars_i, snap_i in zip(bars, snaps):
            proposal = strategy.evaluate(
                make_context(strategy.spec, bars=bars_i, forecasts=forecasts,
                             regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
                             features_snap=snap_i,
                             macro_mode=overrides.get("macro", "PRICE_ONLY")), state)
            state = commit_observation(state, proposal)
        return proposal

    proposal = evaluate_with(ood=True)
    assert proposal.action == "WAIT" and "OUT_OF_DISTRIBUTION" in proposal.reasons
    proposal = evaluate_with(expired=True)
    assert proposal.action == "WAIT" and "FORECAST_EXPIRED" in proposal.reasons
    proposal = evaluate_with(macro="BLOCKED")
    assert proposal.action == "WAIT" and "MACRO_BLOCKED" in proposal.reasons


# --- hysteresis band / exits while holding (§4.2) ---------------------------------


def test_martingale_083_no_add_but_hold_and_079_exit():
    from ak_trading.v2.strategies.martingale import LimitedMartingale
    spec = build_strategy_spec("MARTINGALE", "MINUTE")
    for persistence, expected in ((0.83, "HOLD"), (0.79, "EXIT")):
        snap = features(Decimal("92.5"), Decimal("1"))
        horizon = Horizon("TRADING_MINUTE", 30)
        policy = m1_policy.confidence_policy("MARTINGALE").policy_id
        frozen = frozen_range()
        bucket = "[0.80,0.85)"
        items = (
            evidence("PERSIST:ORDERED_RANGE", bucket, policy, "ORDERED_RANGE", horizon,
                     snap, frozen),
            evidence("PATH", "ALL", policy, "ORDERED_RANGE", horizon, snap, frozen),
            evidence("RETURN_BEFORE_BREAK", "[0.70,0.75)", policy, "ORDERED_RANGE",
                     horizon, snap, frozen),
            evidence("STOP_TOUCH:LONG", "[0.05,0.10]", policy, "ORDERED_RANGE", horizon,
                     snap, frozen),
            evidence("STOP_TOUCH:SHORT", "[0.05,0.10]", policy, "ORDERED_RANGE", horizon,
                     snap, frozen))
        forecasts = (forecast(horizon, base_regime="ORDERED_RANGE",
                              persistence={"ORDERED_RANGE": persistence}, frozen=frozen,
                              stop_touch={"LONG": 0.05, "SHORT": 0.05},
                              evidence_items=items),)
        holding = holding_state(spec, forecasts[0].window_end_nano)
        context = make_context(
            spec, bars=minute_bars([100 - i * 0.35 for i in range(24)]),
            forecasts=forecasts, regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
            features_snap=snap, account_snap=holding_account(spec, holding),
            position=PositionSnapshot(CONTRACT, spec.strategy_id, holding.episode_id or "",
                                      1, Decimal("95"), False))
        # SYNTHETIC V24 management publication: at minute 24 only a fresh
        # 15-minute window fits the original minute-50 exit. Keep the
        # original budget/exit and publish that registered shorter label.
        selected = Horizon("TRADING_MINUTE", 15)
        current_forecast = forecast(
            selected, base_regime="ORDERED_RANGE",
            persistence={"ORDERED_RANGE": persistence}, frozen=frozen,
            published=context.now_nano,
            evidence_items=tuple(replace(row, horizon=selected) for row in items))
        context = replace(context, forecasts=(current_forecast,))
        proposal = LimitedMartingale(spec).evaluate(context, holding)
        assert proposal.action == expected
        if expected == "HOLD":
            assert "MARTINGALE_ADD_SUSPENDED" in proposal.reasons
        else:
            assert "PERSISTENCE_BELOW_EXIT" in proposal.reasons


def test_ordinary_hysteresis_band_holds_with_frozen_stop_and_target():
    from ak_trading.v2.strategies.reversion import RangeReversion
    spec = build_strategy_spec("REVERSION", "MINUTE")
    snap = features(Decimal("96"), Decimal("1"))
    horizon = Horizon("TRADING_MINUTE", 30)
    policy = m1_policy.confidence_policy("REVERSION").policy_id
    frozen = frozen_range()
    items = (
        evidence("PERSIST:ORDERED_RANGE", "[0.65,0.70)", policy, "ORDERED_RANGE", horizon,
                 snap, frozen),
        evidence("PATH", "ALL", policy, "ORDERED_RANGE", horizon, snap, frozen),
        evidence("RETURN_BEFORE_BREAK", "[0.70,0.75)", policy, "ORDERED_RANGE", horizon,
                 snap, frozen))
    forecasts = (forecast(horizon, base_regime="ORDERED_RANGE",
                          persistence={"ORDERED_RANGE": 0.66}, frozen=frozen,
                          evidence_items=items),)
    holding = holding_state(spec, forecasts[0].window_end_nano)
    context = make_context(
        spec, bars=minute_bars([96] * 5, reference_slot=19), forecasts=forecasts,
        regime_obj=regime("ORDERED_RANGE", (), frozen_range()), features_snap=snap,
        account_snap=holding_account(spec, holding),
        position=PositionSnapshot(CONTRACT, spec.strategy_id, holding.episode_id or "",
                                  holding.budget.layers[0].lots if holding.budget else 1,
                                  Decimal("95"), False))
    # SYNTHETIC current publication for the clock-selected remaining label.
    selected = Horizon("TRADING_MINUTE", 15)
    current_forecast = forecast(
        selected, base_regime="ORDERED_RANGE", persistence={"ORDERED_RANGE": 0.66},
        frozen=frozen, published=context.now_nano,
        evidence_items=tuple(replace(row, horizon=selected) for row in items))
    context = replace(context, forecasts=(current_forecast,))
    proposal = RangeReversion(spec).evaluate(context, holding)
    assert proposal.action == "HOLD"
    assert "HYSTERESIS_BAND_MANAGED" in proposal.reasons
    assert proposal.stop_price == holding.budget.stop_price
    assert proposal.target_price == holding.budget.target_price


# --- 006 EVENT_SHOCK integration with the frozen M3A rev3 regime engine -------

from ak_trading.v2.probability.regimes import (  # noqa: E402
    GAP_ATR_MULTIPLE, RegimeReference, assess_regime, baseline_factor_key)


def make_macro() -> MacroContext:
    return MacroContext("macro-gap", BASE + 24 * MINUTE, (), "macro-risk-v2.1.0",
                        (), "PRICE_ONLY", quality())


def gap_features(atr: Decimal, gap: Decimal | None, *, include_gap: bool = True,
                 duplicate_gap: bool = False, include_volume: bool = True,
                 decision: int | None = None) -> FeatureSnapshot:
    """Snapshot carrying real M2 BASE rows for the M3A regime engine."""
    decision = decision or BASE + 24 * MINUTE
    values = [
        FactorValue(baseline_factor_key("er20", 60), Decimal("0.6"), decision - 1, quality()),
        FactorValue(baseline_factor_key("ma20", 60), Decimal("110"), decision - 1, quality()),
        FactorValue(baseline_factor_key("ma60", 60), Decimal("100"), decision - 1, quality()),
        FactorValue("META:price_tick:v2.1.0", Decimal("0.1"), decision - 1, quality()),
    ]
    if include_volume:
        values.append(FactorValue(baseline_factor_key("volume_ratio20", 60),
                                  Decimal("1.5"), decision - 1, quality()))
    if include_gap:
        row = FactorValue(baseline_factor_key("opening_gap_points", 60), gap,
                          decision - 1, quality())
        values.append(row)
        if duplicate_gap:
            values.append(FactorValue(baseline_factor_key("opening_gap_points", 60),
                                      gap + Decimal("1"), decision - 1, quality()))
    return FeatureSnapshot(
        "snap-gap", CONTRACT, "rb2605", PRODUCT, date(2026, 1, 12), "day0-am", "DAY",
        decision, decision - 1, 60, ("b0",), Decimal("112"), atr, tuple(values),
        None, quality())


def gap_reference() -> RegimeReference:
    return RegimeReference("regime-ref-gap", BASE, BASE - 86400 * NANO, {}, {})


def test_m3a_opening_gap_equality_hits_event_shock_flag():
    assessment = assess_regime(
        gap_features(Decimal("2"), Decimal("4")), make_macro(), None, None,
        Decimal("1"), gap_reference(), None)
    assert assessment.base == "UP_TREND"
    assert "EVENT_SHOCK" in assessment.flags
    assert "opening_gap_at_least_2_atr" in assessment.reasons
    assert GAP_ATR_MULTIPLE == Decimal("2")


def test_m3a_missing_or_conflicting_gap_forces_unknown():
    missing = assess_regime(
        gap_features(Decimal("2"), None, include_gap=False), make_macro(), None, None,
        Decimal("1"), gap_reference(), None)
    assert missing.base == "UNKNOWN"
    assert "opening_gap_points_missing" in missing.reasons
    conflicting = assess_regime(
        gap_features(Decimal("2"), Decimal("4"), duplicate_gap=True), make_macro(),
        None, None, Decimal("1"), gap_reference(), None)
    assert conflicting.base == "UNKNOWN"
    assert "opening_gap_points_conflicting_sources" in conflicting.reasons


def test_m3a_macro_risk_tag_keeps_event_shock_flag():
    macro = MacroContext("macro-gap", BASE + 24 * MINUTE, (), "macro-risk-v2.1.0",
                         ("GLOBAL_EVENT",), "WITH_MACRO", quality())
    assessment = assess_regime(
        gap_features(Decimal("2"), Decimal("0.5")), macro, None, None,
        Decimal("1"), gap_reference(), None)
    assert "EVENT_SHOCK" in assessment.flags
    assert "macro_risk_tag_active" in assessment.reasons


@pytest.mark.parametrize("family", ["TREND", "BREAKOUT", "PULLBACK", "REVERSION",
                                    "GRID", "MARTINGALE"])
def test_event_shock_flag_from_m3a_blocks_every_trading_family(family):
    assessment = assess_regime(
        gap_features(Decimal("2"), Decimal("4")), make_macro(), None, None,
        Decimal("1"), gap_reference(), None)
    assert "EVENT_SHOCK" in assessment.flags
    strategy = registry()[f"{family.lower()}_m1_v2"]
    snap = gap_features(Decimal("2"), Decimal("4"))
    proposal = strategy.evaluate(
        make_context(strategy.spec, bars=minute_bars([112] * 6),
                     forecasts=range_forecasts(features(Decimal("112"), Decimal("2"))),
                     regime_obj=assessment, features_snap=snap), None)
    assert proposal.action == "WAIT"
    assert "EVENT_LIQUIDITY_RISK" in proposal.reasons


def test_m3b_does_not_fabricate_meta_or_base_rows():
    import pathlib
    package = pathlib.Path("src/ak_trading/v2/strategies")
    for module in package.glob("*.py"):
        text = module.read_text(encoding="utf-8")
        assert "opening_gap_points" not in text, module.name
        assert 'FactorValue("META:' not in text and "FactorValue('META':" not in text, module.name
        assert 'FactorValue("BASE:' not in text and "FactorValue('BASE':" not in text, module.name


def test_allow_event_breakout_stays_false_across_all_specs():
    for spec in strategy_specs_v2("MINUTE") + strategy_specs_v2("DAILY"):
        assert spec.parameters["allow_event_breakout"] is False
        assert spec.parameters["allow_wide_range"] is False


# --- real-track adaptation (real-input-rb-r2 collection; skip if absent) -------

from pathlib import Path as _Path  # noqa: E402
import json as _json  # noqa: E402

REAL_COLLECTION = _Path("data/research_runs/v2/real-input-rb-r2/collection")
REAL_SHARD = REAL_COLLECTION / "shards" / (
    "0711d08f92a71425c91a145be16cdc3aea7dc06d465ae0352228846ededc372d")


def _decode_records(raw, record_type):
    # Record-level strict decode: the r2 shards were published under an
    # earlier contract_version; the file-level gate is the input node's to
    # re-stamp on republication, individual records stay strictly validated.
    from ak_trading.v2.schemas import decode_record
    root = _json.loads(raw)
    return tuple(decode_record(record_type, record) for record in root["records"])


def _real_sessions():
    from ak_trading.v2.contracts import SessionWindow
    raw = (REAL_SHARD / "sessions.json").read_bytes()
    return _decode_records(raw, SessionWindow)


def _real_rules():
    from ak_trading.v2.contracts import TradingRule
    rules = []
    for shard in sorted((REAL_COLLECTION / "shards").iterdir()):
        rules.extend(_decode_records((shard / "rules.json").read_bytes(), TradingRule))
    return tuple(rules)


def _real_product_features(product, decision, sessions):
    last_end = max(s.end_nano for s in sessions)
    decision = min(decision, last_end - 1)
    values = [FactorValue("META:price_tick:v2.1.0", Decimal("1"), decision - 1, quality())]
    return features(Decimal("3600"), Decimal("20"), decision=decision), decision


@pytest.mark.skipif(not REAL_SHARD.exists(), reason="real rb collection not present")
def test_real_calendar_night_to_day_holding_deadline_cuts_at_night_end():
    sessions = _real_sessions()
    night = next(s for s in sessions if s.kind == "NIGHT" and s.evidence_status == "VERIFIED")
    spec = build_strategy_spec("REVERSION", "MINUTE")
    decision = night.start_nano + 10 * MINUTE
    deadline = m1_clock.holding_deadline(decision, night.product, spec.holding_policy,
                                         sessions)
    assert deadline == night.end_nano  # night-to-day continuation is forbidden


@pytest.mark.skipif(not REAL_SHARD.exists(), reason="real rb collection not present")
def test_real_calendar_setup_expiry_counts_planned_bars_not_natural_minutes():
    sessions = _real_sessions()
    spec = build_strategy_spec("REVERSION", "MINUTE")
    # A real trading day with two DAY fragments (am/lunch/pm): a setup
    # created on the last planned am bar expires four planned bars later,
    # which are the first pm bars — natural minutes would land inside lunch.
    by_day: dict = {}
    for session in sessions:
        by_day.setdefault(session.trading_day, []).append(session)
    target = None
    for day, fragments in sorted(by_day.items()):
        fragments = sorted(fragments, key=lambda s: s.start_nano)
        for first, second in zip(fragments, fragments[1:]):
            if second.start_nano - first.end_nano > MINUTE:
                target = (first, second)
                break
        if target:
            break
    assert target is not None
    am, pm = target
    decision = am.end_nano  # end of the last am bar
    snap = _swap_product(features(Decimal("3600"), Decimal("20"), decision=decision - 1),
                         am.product)
    context = make_context(spec, bars=(), forecasts=(), regime_obj=regime("ORDERED_RANGE"),
                           features_snap=snap, sessions_override=sessions,
                           now_override=decision)
    expiry = setup_expiry_nano(context, decision, 5)
    assert expiry == pm.start_nano + 4 * MINUTE  # 4 further planned pm bars
    assert expiry - decision != 4 * MINUTE  # natural minutes would stay in lunch
    # Within one fragment the walk is slot-exact.
    mid = am.start_nano + 10 * MINUTE
    assert setup_expiry_nano(context, mid, 5) == mid + 4 * MINUTE


def _swap_product(snapshot, product):
    from ak_trading.v2.contracts import FeatureSnapshot
    return FeatureSnapshot(
        snapshot.snapshot_id, snapshot.contract_id, snapshot.raw_symbol, product,
        snapshot.trading_day, snapshot.session_id, snapshot.session_kind,
        snapshot.decision_nano, snapshot.available_at_nano, snapshot.duration_seconds,
        snapshot.source_bar_ids, snapshot.reference_price, snapshot.atr_points,
        snapshot.values, snapshot.daily_background_id, snapshot.quality)


@pytest.mark.skipif(not REAL_SHARD.exists(), reason="real rb collection not present")
def test_real_fee_timeline_rules_invisible_at_historical_decisions():
    from ak_trading.v2.data import visible_rule
    from ak_trading.v2.contracts import FrozenInput
    rules = _real_rules()
    fee_rules = [r for r in rules if r.close_today_fee is not None]
    assert len(fee_rules) == 9  # FEE-C1-01 verified close-today timeline
    sessions = _real_sessions()
    last_decision = max(s.end_nano for s in sessions)
    for rule in fee_rules:
        assert rule.available_at_nano > last_decision, rule.rule_id
        assert rule.available_at_nano > rule.effective_until_nano, rule.rule_id
    bundle = FrozenInput(REAL_COLLECTION, "0" * 64, (), sessions, (), (), rules)
    for rule in fee_rules:
        mid = (rule.effective_from_nano + rule.effective_until_nano) // 2
        assert visible_rule(bundle, rule.contract_id, mid) is None
    # Downstream effect: no visible rule -> cost families wait, never zero fees.
    spec = build_strategy_spec("REVERSION", "MINUTE")
    snap = features(Decimal("3600"), Decimal("20"))
    proposal = registry()["reversion_m1_v2"].evaluate(
        make_context(spec, bars=minute_bars([3600] * 3), forecasts=range_forecasts(snap),
                     regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
                     features_snap=snap, rule_obj=NO_RULE), None)
    assert proposal.action == "WAIT"
    assert "RULE_MISSING" in proposal.reasons


def test_unknown_evidence_rule_blocks_cost_families_and_budgets():
    def unknown_rule():
        base = rule()
        from ak_trading.v2.contracts import TradingRule
        return TradingRule(base.rule_id + "-u", base.contract_id,
                           base.effective_from_nano, base.effective_until_nano,
                           base.available_at_nano, base.multiplier, base.price_tick,
                           base.lot_step, base.min_open_lots, base.open_fee,
                           base.close_fee, base.close_today_fee, base.long_margin_rate,
                           base.short_margin_rate, base.limit_up, base.limit_down,
                           base.slippage_ticks, base.impact_ticks,
                           base.participation_rate, "UNKNOWN", base.source_sha256)

    bars, snaps = range_bars_sequence()
    proposal, _ = drive_open("REVERSION", regime_base="ORDERED_RANGE",
                             bars_sequence=bars, features_sequence=snaps,
                             frozen=frozen_range(),
                             forecast_builder=range_forecast_builder(),
                             rule_override=unknown_rule())
    assert proposal.action == "WAIT"
    assert "RULE_EVIDENCE_UNKNOWN" in proposal.reasons

    spec = build_strategy_spec("TREND", "MINUTE")
    snap = features(Decimal("123"), Decimal("4"))
    proposal, _ = drive_open("TREND", regime_base="UP_TREND",
                             bars_sequence=trend_bars_sequence(True)[0],
                             features_sequence=trend_bars_sequence(True)[1],
                             forecasts=trend_forecasts_local(snap),
                             rule_override=unknown_rule())
    assert proposal.action == "WAIT"
    assert "BUDGET_INFEASIBLE" in proposal.reasons


def trend_forecasts_local(snap):
    horizon = Horizon("TRADING_MINUTE", 30)
    policy = m1_policy.confidence_policy("TREND").policy_id
    return (forecast(
        horizon, base_regime="UP_TREND", persistence={"UP_TREND": 0.75},
        evidence_items=(
            evidence("PERSIST:UP_TREND", "[0.75,0.80)", policy, "UP_TREND", horizon,
                     snap, None),
            evidence("PATH", "ALL", policy, "UP_TREND", horizon, snap, None))),)


def test_wait_reports_foreign_position_and_account_state():
    from ak_trading.v2.strategies.wait import WaitStrategy
    spec = build_strategy_spec("WAIT", "MINUTE")
    snap = features(Decimal("100"), Decimal("1"))
    proposal = WaitStrategy(spec).evaluate(
        make_context(spec, bars=minute_bars([100]), forecasts=(), features_snap=snap,
                     regime_obj=regime("ORDERED_RANGE"),
                     position=PositionSnapshot(CONTRACT, "trend_m1_v2", "episode-y", 2,
                                               Decimal("100"), False)), None)
    assert proposal.action == "WAIT" and proposal.target_lots == 0
    assert "POSITION_MANAGED_BY_OWNER_STRATEGY" in proposal.reasons
