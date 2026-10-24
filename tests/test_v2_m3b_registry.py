"""M3B-01: seven implementation classes, fourteen specs, parameter
whitelists and the pure interface (04-M3B-策略库.md §9)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ak_trading.v2 import policy as m1_policy
from ak_trading.v2.contracts import (
    AccountSnapshot,
    DataQuality,
    MacroContext,
    RunPolicy,
    StrategyContext,
)
from ak_trading.v2.strategies.registry import (
    COMMON_PARAMETER_DEFAULTS,
    FAMILY_ALLOWED_FLAGS,
    FAMILY_ALLOWED_REGIMES,
    FAMILY_PARAMETER_DEFAULTS,
    PROPOSALS_CSV_COLUMNS,
    Strategy,
    build_strategy_spec,
    code_sha256_for,
    select_exit_binding,
    default_parameters,
    evaluate_registry_v2,
    factor_key_for,
    strategy_id_for,
    strategy_registry_v2,
    strategy_specs_v2,
    validate_strategy_parameters,
    proposals_csv_row,
)
from ak_trading.v2.strategies.breakout import ConfirmedBreakout
from ak_trading.v2.strategies.grid import FixedInventoryGrid
from ak_trading.v2.strategies.martingale import LimitedMartingale
from ak_trading.v2.strategies.pullback import TrendPullback
from ak_trading.v2.strategies.reversion import RangeReversion
from ak_trading.v2.strategies.trend import TrendFollowing
from ak_trading.v2.strategies.wait import WaitStrategy

NANO = 1_000_000_000
MINUTE = 60 * NANO
BASE = int(datetime(2026, 1, 12, 1, 0, tzinfo=timezone.utc).timestamp()) * NANO
NOW = BASE + 30 * MINUTE
HASH0 = "0" * 64
PRODUCT = "SHFE.RB"
CONTRACT = "SHFE.RB2605"
FAMILIES = ("TREND", "BREAKOUT", "PULLBACK", "REVERSION", "GRID", "MARTINGALE", "WAIT")
MINUTE_IDS = ("breakout_m1_v2", "grid_m1_v2", "martingale_m1_v2", "pullback_m1_v2",
              "reversion_m1_v2", "trend_m1_v2", "wait_m1_v2")
DAILY_IDS = ("breakout_d1_v2", "grid_d1_v2", "martingale_d1_v2", "pullback_d1_v2",
             "reversion_d1_v2", "trend_d1_v2", "wait_d1_v2")


def quality() -> DataQuality:
    return DataQuality("READY", (), "observed", HASH0)


def _weekdays(count: int) -> list[date]:
    days: list[date] = []
    day = date(2026, 1, 12)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def sessions(days: int = 3):
    from ak_trading.v2.contracts import SessionWindow
    windows = []
    for index, day in enumerate(_weekdays(days)):
        day_base = BASE + index * 86400 * NANO
        windows.append(SessionWindow(f"day{index}-am", PRODUCT, day, day_base,
                                     day_base + 150 * MINUTE, "DAY", "cal-1", "SCENARIO"))
        windows.append(SessionWindow(f"day{index}-pm", PRODUCT, day,
                                     day_base + 16200 * NANO,
                                     day_base + 16200 * NANO + 90 * MINUTE, "DAY",
                                     "cal-1", "SCENARIO"))
    return tuple(windows)


def account(equity: Decimal = Decimal("1000000")) -> AccountSnapshot:
    return AccountSnapshot("acct-1", NOW, date(2026, 1, 12), equity, equity,
                           Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"),
                           equity, Decimal("0"), Decimal("0"), equity,
                           Decimal("0"), equity, (), False, quality())


def default_exit_binding(strategy_id):
    """P0 placeholder retained only for direct StrategyContext constructions;
    make_context/minimal_context inject the real pre-OPEN selector."""
    from ak_trading.v2.strategies.registry import build_strategy_spec, select_exit_binding
    mode = "MINUTE" if strategy_id.endswith("_m1_v2") else "DAILY"
    family = strategy_id.rsplit("_", 2)[0].upper()
    return select_exit_binding(build_strategy_spec(family, mode), "legacy")


def minimal_context(spec) -> StrategyContext:
    from ak_trading.v2.contracts import FactorValue, FeatureSnapshot, RegimeAssessment
    features = FeatureSnapshot(
        "snap-1", CONTRACT, "rb2605", PRODUCT, date(2026, 1, 12), "day0-am", "DAY",
        NOW, NOW - 1, 60 if spec.mode == "MINUTE" else 86400, ("b0",),
        Decimal("100"), Decimal("1"),
        (FactorValue("META:price_tick:v2.1.0", Decimal("0.1"), NOW - 1, quality()),),
        None, quality())
    regime = RegimeAssessment("assess-1", "snap-1", "ref-1", NOW, "UNKNOWN", (), None,
                              "ALIGNED", 0, (), quality())
    return StrategyContext(
        now_nano=NOW, spec=spec, exit_binding=select_exit_binding(spec, "legacy"),
        features=features, forecasts=(), regime=regime,
        macro=MacroContext("macro-1", NOW, (), "macro-risk-v2.1.0", (), "PRICE_ONLY",
                           quality()),
        bars=(), sessions=sessions(), account=account(), position=None, rule=None,
        confidence_policy=m1_policy.confidence_policy(spec.family),
        stress_policy=m1_policy.stress_policy(),
        run_policy=RunPolicy("RESEARCH_EVALUATION", "BEST_EV", 7, None, None,
                             "research-policy-v2.1.0", "macro-risk-v2.1.0",
                             "PRICE_ONLY", "BASE"),
        qualification=None,
        model_hashes={"forecast": HASH0, "payoff": HASH0, "regime_reference": HASH0},
        holding_deadline_nano=m1_clock_deadline(spec))


def m1_clock_deadline(spec) -> int:
    from ak_trading.v2 import clock as m1_clock
    return m1_clock.holding_deadline(NOW, PRODUCT, spec.holding_policy, sessions())


# --- fourteen specs -----------------------------------------------------------


@pytest.mark.parametrize("mode,expected_ids", [("MINUTE", MINUTE_IDS), ("DAILY", DAILY_IDS)])
def test_strategy_ids_exact(mode, expected_ids):
    specs = strategy_specs_v2(mode)
    assert tuple(spec.strategy_id for spec in specs) == expected_ids
    assert list(specs) == sorted(specs, key=lambda spec: spec.strategy_id)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("mode", ["MINUTE", "DAILY"])
def test_spec_fixed_fields(family, mode):
    spec = build_strategy_spec(family, mode)
    assert spec.strategy_id == strategy_id_for(family, mode)
    assert spec.version == "2.1.0"
    assert spec.parameter_version == f"{spec.strategy_id}:params-1"
    assert spec.validation_stage == "RESEARCH"
    assert spec.path_barrier_key == "ATR_1"
    assert spec.confidence_policy_id == m1_policy.confidence_policy(family).policy_id
    assert spec.holding_policy == m1_policy.holding_policy(mode)
    if mode == "MINUTE":
        assert (spec.max_holding.unit, spec.max_holding.value) == ("TRADING_MINUTE", 30)
    else:
        assert (spec.max_holding.unit, spec.max_holding.value) == ("TRADING_DAY", 5)
    assert spec.allowed_regimes == FAMILY_ALLOWED_REGIMES[family]
    assert spec.allowed_flags == FAMILY_ALLOWED_FLAGS[family]


def test_wait_registers_zero_layers_and_lots():
    spec = build_strategy_spec("WAIT", "MINUTE")
    assert (spec.max_layers, spec.max_total_lots) == (0, 0)
    assert build_strategy_spec("WAIT", "DAILY").max_layers == 0


@pytest.mark.parametrize("family,expected", [
    ("TREND", (1, 7)), ("BREAKOUT", (1, 7)), ("PULLBACK", (1, 7)), ("REVERSION", (1, 7)),
    ("GRID", (3, 6)), ("MARTINGALE", (3, 7)), ("WAIT", (0, 0))])
def test_layer_and_lot_caps(family, expected):
    spec = build_strategy_spec(family, "MINUTE")
    assert (spec.max_layers, spec.max_total_lots) == expected


def test_allowed_regimes_and_flags_tables():
    assert FAMILY_ALLOWED_REGIMES["TREND"] == ("UP_TREND", "DOWN_TREND")
    assert FAMILY_ALLOWED_REGIMES["BREAKOUT"] == ("UP_TREND", "DOWN_TREND", "COMPRESSION")
    assert FAMILY_ALLOWED_REGIMES["REVERSION"] == ("ORDERED_RANGE",)
    assert FAMILY_ALLOWED_REGIMES["WAIT"] == (
        "UP_TREND", "DOWN_TREND", "ORDERED_RANGE", "WIDE_RANGE", "COMPRESSION", "UNKNOWN")
    assert FAMILY_ALLOWED_FLAGS["TREND"] == ("PULLBACK", "BREAKOUT")
    assert FAMILY_ALLOWED_FLAGS["BREAKOUT"] == ("BREAKOUT",)
    assert FAMILY_ALLOWED_FLAGS["PULLBACK"] == ("PULLBACK",)
    assert FAMILY_ALLOWED_FLAGS["REVERSION"] == ()
    assert FAMILY_ALLOWED_FLAGS["WAIT"] == ("PULLBACK", "BREAKOUT", "EVENT_SHOCK", "ILLIQUID")


# --- parameter whitelists -------------------------------------------------------


@pytest.mark.parametrize("family", FAMILIES)
def test_parameters_carry_common_defaults(family):
    parameters = build_strategy_spec(family, "MINUTE").parameters
    for key, value in COMMON_PARAMETER_DEFAULTS.items():
        assert key in parameters
        expected = list(value) if isinstance(value, tuple) else value
        assert parameters[key] == expected


def test_family_specific_parameters_exact():
    minute = {family: build_strategy_spec(family, "MINUTE").parameters for family in FAMILIES}
    assert minute["TREND"]["entry_spread_atr"] == "0.25"
    assert minute["BREAKOUT"]["breakout_buffer_ticks"] == 1
    assert minute["BREAKOUT"]["confirmation_bars"] == 3
    assert minute["PULLBACK"]["pullback_distance_atr"] == "0.5"
    assert minute["PULLBACK"]["setup_expiry_bars"] == 5
    assert minute["REVERSION"]["entry_fraction"] == "0.20"
    assert minute["REVERSION"]["minimum_round_trip_cost_ratio"] == "2"
    assert list(minute["GRID"]["layer_fractions"]) == ["0.25", "0.15", "0.05"]
    assert list(minute["GRID"]["layer_multipliers"]) == [1, 1, 1]
    assert minute["GRID"]["maximum_layers"] == 3
    assert list(minute["MARTINGALE"]["layer_fractions"]) == ["0.25", "0.15", "0.05"]
    assert list(minute["MARTINGALE"]["layer_multipliers"]) == [1, 2, 4]
    assert minute["MARTINGALE"]["maximum_layers"] == 3
    assert minute["WAIT"]["cancel_new_risk"] is True


FOREIGN_KEYS = {
    "TREND": "entry_fraction", "BREAKOUT": "entry_spread_atr", "PULLBACK": "layer_fractions",
    "REVERSION": "entry_spread_atr", "GRID": "entry_fraction", "MARTINGALE": "entry_fraction",
    "WAIT": "entry_spread_atr",
}


@pytest.mark.parametrize("family", FAMILIES)
def test_foreign_parameter_keys_rejected(family):
    parameters = default_parameters(family)
    polluted = dict(parameters)
    polluted[FOREIGN_KEYS[family]] = "0.20"
    with pytest.raises(ValueError):
        validate_strategy_parameters(family, polluted)
    missing = dict(parameters)
    removed = next(iter(FAMILY_PARAMETER_DEFAULTS[family])) \
        if FAMILY_PARAMETER_DEFAULTS[family] else "entry_confirmation_bars"
    del missing[removed]
    with pytest.raises(ValueError):
        validate_strategy_parameters(family, missing)


@pytest.mark.parametrize("family", FAMILIES)
def test_minute_and_daily_ids_differ(family):
    assert strategy_id_for(family, "MINUTE") != strategy_id_for(family, "DAILY")
    assert strategy_id_for(family, "MINUTE").endswith("_m1_v2")
    assert strategy_id_for(family, "DAILY").endswith("_d1_v2")


# --- required factor keys --------------------------------------------------------


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("mode,duration", [("MINUTE", 60), ("DAILY", 86400)])
def test_factor_keys_use_full_m2_ids(family, mode, duration):
    spec = build_strategy_spec(family, mode)
    for key in spec.required_factor_keys:
        assert key.startswith("BASE:") and key.endswith(f":v2.1.0:d{duration}")
    assert factor_key_for("ma20", mode) == f"BASE:ma20:v2.1.0:d{duration}"
    assert build_strategy_spec("TREND", "MINUTE").required_factor_keys == (
        "BASE:close:v2.1.0:d60", "BASE:ma20:v2.1.0:d60", "BASE:ma60:v2.1.0:d60",
        "BASE:atr14:v2.1.0:d60", "BASE:er20:v2.1.0:d60")
    assert build_strategy_spec("WAIT", "MINUTE").required_factor_keys == ()


# --- classes and registry --------------------------------------------------------


@pytest.mark.parametrize("family,cls", [
    ("TREND", TrendFollowing), ("BREAKOUT", ConfirmedBreakout), ("PULLBACK", TrendPullback),
    ("REVERSION", RangeReversion), ("GRID", FixedInventoryGrid),
    ("MARTINGALE", LimitedMartingale), ("WAIT", WaitStrategy)])
def test_class_init_takes_only_spec(family, cls):
    spec = build_strategy_spec(family, "MINUTE")
    strategy = cls(spec)
    assert strategy.spec is spec
    # Protocol conformance by structure: a ``spec`` property plus evaluate.
    assert isinstance(getattr(type(strategy), "spec", None), property)
    assert callable(strategy.evaluate)


@pytest.mark.parametrize("mode", ["MINUTE", "DAILY"])
def test_registry_seven_instances_per_mode(mode):
    registry = strategy_registry_v2(mode)
    assert len(registry) == 7
    assert set(registry) == set(MINUTE_IDS if mode == "MINUTE" else DAILY_IDS)
    for strategy_id, strategy in registry.items():
        assert strategy.spec.strategy_id == strategy_id


def test_code_sha256_covers_shared_bytes_and_is_stable():
    first = {family: code_sha256_for(family) for family in FAMILIES}
    second = {family: code_sha256_for(family) for family in FAMILIES}
    assert first == second
    for family, digest in first.items():
        assert len(digest) == 64 and digest == digest.lower()
    assert len(set(first.values())) == len(FAMILIES)


@pytest.mark.parametrize("mode", ["MINUTE", "DAILY"])
def test_evaluate_registry_orders_candidates_only(mode):
    registry = strategy_registry_v2(mode)
    proposals = evaluate_registry_v2(
        registry, lambda strategy: minimal_context(strategy.spec), {})
    assert tuple(p.strategy_id for p in proposals) == tuple(sorted(registry))
    for proposal in proposals:
        assert proposal.action == "WAIT"
        assert proposal.payoff is None
        assert proposal.budget is None
        assert proposal.target_lots == 0


# --- P1 exit-policy binding selection (08-M3C §5.1–§5.4 skeleton) ---------------


def test_exit_binding_profiles_follow_p1_family_eligibility():
    trend = build_strategy_spec("TREND", "MINUTE")
    bindings = {profile: select_exit_binding(trend, profile)
                for profile in ("legacy", "trailing", "time_decay")}
    assert len({b.binding_sha256 for b in bindings.values()}) == 3
    for binding in bindings.values():
        assert binding.strategy_id == trend.strategy_id
        assert binding.entry_spec_sha256 == trend.code_sha256
    assert bindings["legacy"].priority != bindings["trailing"].priority
    for family in ("WAIT", "REVERSION", "GRID", "MARTINGALE"):
        spec = build_strategy_spec(family, "DAILY")
        assert select_exit_binding(spec, "legacy").strategy_id == spec.strategy_id
        for profile in ("trailing", "time_decay"):
            with pytest.raises(ValueError):
                select_exit_binding(spec, profile)
    with pytest.raises(ValueError):
        select_exit_binding(trend, "unknown_profile")


def test_exit_binding_frozen_pre_open_is_deterministic_and_version_sensitive():
    trend = build_strategy_spec("TREND", "MINUTE")
    first = select_exit_binding(trend, "trailing")
    second = select_exit_binding(trend, "trailing")
    assert first == second  # bound before OPEN; byte-stable across selections
    other = select_exit_binding(trend, "time_decay")
    assert other.binding_sha256 != first.binding_sha256
    # Same entry strategy + different exit version => different contexts.
    legacy_context = minimal_context_with(select_exit_binding(trend, "legacy"), trend)
    trailing_context = minimal_context_with(first, trend)
    assert legacy_context.exit_binding.binding_sha256         != trailing_context.exit_binding.binding_sha256
    assert legacy_context.spec is trailing_context.spec


def minimal_context_with(binding, spec):
    from ak_trading.v2.contracts import FactorValue, FeatureSnapshot, RegimeAssessment
    duration = 60 if spec.mode == "MINUTE" else 86400
    features = FeatureSnapshot(
        "snap-1", CONTRACT, "rb2605", PRODUCT, date(2026, 1, 12), "day0-am",
        "DAY" if duration == 60 else "DAILY", NOW, NOW - 1, duration, ("b0",),
        Decimal("100"), Decimal("1"),
        (FactorValue("META:price_tick:v2.1.0", Decimal("0.1"), NOW - 1, quality()),),
        None, quality())
    regime = RegimeAssessment("assess-1", "snap-1", "ref-1", NOW, "UNKNOWN", (), None,
                              "ALIGNED", 0, (), quality())
    return StrategyContext(
        now_nano=NOW, spec=spec, exit_binding=binding, features=features,
        forecasts=(), regime=regime,
        macro=MacroContext("macro-1", NOW, (), "macro-risk-v2.1.0", (), "PRICE_ONLY",
                           quality()),
        bars=(), sessions=sessions(), account=account(), position=None, rule=None,
        confidence_policy=m1_policy.confidence_policy(spec.family),
        stress_policy=m1_policy.stress_policy(),
        run_policy=RunPolicy("RESEARCH_EVALUATION", "BEST_EV", 7, None, None,
                             "research-policy-v2.1.0", "macro-risk-v2.1.0",
                             "PRICE_ONLY", "BASE"),
        qualification=None,
        model_hashes={"forecast": HASH0, "payoff": HASH0, "regime_reference": HASH0},
        holding_deadline_nano=m1_clock_deadline(spec))


def test_exit_binding_spec_mismatch_is_rejected():
    trend_binding = select_exit_binding(build_strategy_spec("TREND", "MINUTE"), "legacy")
    wait_spec = build_strategy_spec("WAIT", "MINUTE")
    with pytest.raises(ValueError):
        minimal_context_with(trend_binding, wait_spec)


def test_wait_strategy_reasons_and_csv_projection():
    strategy = strategy_registry_v2("MINUTE")["wait_m1_v2"]
    proposal = strategy.evaluate(minimal_context(strategy.spec), None)
    assert proposal.action == "WAIT"
    assert proposal.reasons
    row = proposals_csv_row(proposal)
    assert len(row) == len(PROPOSALS_CSV_COLUMNS)
    mapping = dict(zip(PROPOSALS_CSV_COLUMNS, row))
    assert mapping["action"] == "WAIT"
    assert mapping["episode_id"] == ""
    assert mapping["layer_index"] == ""
    assert mapping["trigger_price"] == ""
    assert mapping["budget_id"] == ""
    assert mapping["reasons"].startswith("[")
