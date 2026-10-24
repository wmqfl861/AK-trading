"""Strategy protocol, the fourteen frozen v2.1 strategy specs and the shared
evaluation gates used by every family module (04-M3B-策略库.md §3–§5).

Exact public surface required by the node document:

    Strategy                    (protocol: ``spec`` property + ``evaluate``)
    strategy_registry_v2(mode)  (seven instances per mode, keyed by strategy_id)
    evaluate_registry_v2(...)   (one proposal per registered strategy)

Family modules import the shared gate/geometry/proposal helpers from this
module; the family classes are imported lazily inside ``strategy_registry_v2``
so the module-level import graph stays acyclic. This module never imports M4,
never reads environment variables and performs no business I/O; the only file
access is reading this subpackage's own source bytes to derive the frozen
``code_sha256`` identity (strategy module + shared registry + shared state).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Literal, Mapping, Protocol

from ak_trading.v2 import clock as m1_clock
from ak_trading.v2 import policy as m1_policy
from ak_trading.v2.artifacts import stable_id
from ak_trading.v2.contracts import (
    FrozenRange,
    Horizon,
    JsonValue,
    MarketBar,
    MarketForecast,
    PositionSnapshot,
    RegimeAssessment,
    SessionWindow,
    StrategyContext,
    StrategyFamily,
    StrategySetup,
    StrategySpec,
    StrategyState,
    StrategyProposal,
    RoundBudget,
)
from ak_trading.v2.schemas import SchemaError, calibration_geometry_key, probability_in_bucket

STRATEGY_LIBRARY_VERSION = "2.1.0"
PARAMETER_VERSION_SUFFIX = ":params-1"
OBSERVATION_DURATIONS: Mapping[str, int] = MappingProxyType({"MINUTE": 60, "DAILY": 86400})
MODE_SUFFIX: Mapping[str, str] = MappingProxyType({"MINUTE": "m1", "DAILY": "d1"})
MINUTE_MAX_HOLDING_VALUE = 30
DAILY_MAX_HOLDING_VALUE = 5
ADD_HORIZON_VALUES: Mapping[str, tuple[int, ...]] = MappingProxyType(
    {"MINUTE": (60, 30, 15, 5, 1), "DAILY": (20, 5, 1)})
RESEARCH_MAXIMUM_ROUND_LOTS = 7
STRESS_POLICY_ID = "stress-v2.1.0"
MINUTE_NANO = 60_000_000_000
DAY_NANO = 86_400_000_000_000

STRATEGY_SPECS_SCHEMA_VERSION = "ak_probability_strategy_specs_v2.1.0"
PROPOSALS_SCHEMA_VERSION = "ak_probability_proposals_v2.1.0"
STRATEGY_STATES_SCHEMA_VERSION = "ak_probability_strategy_states_v2.1.0"
ROUND_BUDGETS_SCHEMA_VERSION = "ak_probability_round_budgets_v2.1.0"
LOGICAL_REPLAY_SCHEMA_VERSION = "ak_probability_logical_replay_v2.1.0"
STRATEGY_RUN_SCHEMA_VERSION = "ak_probability_strategy_run_v2.1.0"

PROPOSALS_CSV_COLUMNS: tuple[str, ...] = (
    "proposal_id", "strategy_id", "episode_id", "contract_id", "decision_nano", "action",
    "target_lots", "layer_index", "trigger_price", "target_price", "stop_price",
    "earliest_execution_nano", "valid_until_nano", "forecast_id", "assessment_id",
    "budget_id", "reasons",
)

# --- frozen first-version parameter whitelists (04-M3B §4.1) -----------------

COMMON_PARAMETER_DEFAULTS: Mapping[str, JsonValue] = MappingProxyType({
    "entry_confirmation_bars": 3,
    "exit_confirmation_bars": 1,
    "max_entry_gap_atr": "0.10",
    "round_risk_ratio": "0.005",
    "stop_loss_multiple": "1",
    "take_profit_multiple": "2",
    "requires_macro": False,
    "allow_countertrend": False,
    "allow_event_breakout": False,
    "allow_wide_range": False,
})

FAMILY_PARAMETER_DEFAULTS: Mapping[str, Mapping[str, JsonValue]] = MappingProxyType({
    "TREND": MappingProxyType({"entry_spread_atr": "0.25"}),
    "BREAKOUT": MappingProxyType({"breakout_buffer_ticks": 1, "confirmation_bars": 3}),
    "PULLBACK": MappingProxyType({"pullback_distance_atr": "0.5", "setup_expiry_bars": 5}),
    "REVERSION": MappingProxyType({"entry_fraction": "0.20", "minimum_round_trip_cost_ratio": "2"}),
    "GRID": MappingProxyType({
        "layer_fractions": ("0.25", "0.15", "0.05"),
        "layer_multipliers": (1, 1, 1),
        "maximum_layers": 3,
    }),
    "MARTINGALE": MappingProxyType({
        "layer_fractions": ("0.25", "0.15", "0.05"),
        "layer_multipliers": (1, 2, 4),
        "maximum_layers": 3,
    }),
    "WAIT": MappingProxyType({"cancel_new_risk": True}),
})

FAMILY_ALLOWED_REGIMES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "TREND": ("UP_TREND", "DOWN_TREND"),
    "BREAKOUT": ("UP_TREND", "DOWN_TREND", "COMPRESSION"),
    "PULLBACK": ("UP_TREND", "DOWN_TREND"),
    "REVERSION": ("ORDERED_RANGE",),
    "GRID": ("ORDERED_RANGE",),
    "MARTINGALE": ("ORDERED_RANGE",),
    "WAIT": ("UP_TREND", "DOWN_TREND", "ORDERED_RANGE", "WIDE_RANGE", "COMPRESSION", "UNKNOWN"),
})

FAMILY_ALLOWED_FLAGS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "TREND": ("PULLBACK", "BREAKOUT"),
    "BREAKOUT": ("BREAKOUT",),
    "PULLBACK": ("PULLBACK",),
    "REVERSION": (),
    "GRID": (),
    "MARTINGALE": (),
    "WAIT": ("PULLBACK", "BREAKOUT", "EVENT_SHOCK", "ILLIQUID"),
})

# Documented values (04-M3B §4.1); M1 enforces WAIT as exactly 0/0 and every
# trading family as >= 1.
FAMILY_MAX_LAYERS: Mapping[str, int] = MappingProxyType({
    "TREND": 1, "BREAKOUT": 1, "PULLBACK": 1, "REVERSION": 1,
    "GRID": 3, "MARTINGALE": 3, "WAIT": 0,
})
FAMILY_MAX_TOTAL_LOTS: Mapping[str, int] = MappingProxyType({
    "TREND": 7, "BREAKOUT": 7, "PULLBACK": 7, "REVERSION": 7,
    "GRID": 6, "MARTINGALE": 7, "WAIT": 0,
})

FAMILY_FACTOR_NAMES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "TREND": ("close", "ma20", "ma60", "atr14", "er20"),
    "BREAKOUT": ("close", "prior_high20", "prior_low20", "atr14"),
    "PULLBACK": ("close", "ma20", "ma60", "atr14"),
    "REVERSION": ("close", "atr14"),
    "GRID": ("close", "atr14"),
    "MARTINGALE": ("close", "atr14"),
    "WAIT": (),
})

FAMILY_MODULES: Mapping[str, str] = MappingProxyType({
    "TREND": "trend.py",
    "BREAKOUT": "breakout.py",
    "PULLBACK": "pullback.py",
    "REVERSION": "reversion.py",
    "GRID": "grid.py",
    "MARTINGALE": "martingale.py",
    "WAIT": "wait.py",
})

RANGE_FAMILIES: frozenset[str] = frozenset({"REVERSION", "GRID", "MARTINGALE"})
DIRECTIONAL_FAMILIES: frozenset[str] = frozenset({"TREND", "BREAKOUT", "PULLBACK"})
HARD_RISK_FLAGS: frozenset[str] = frozenset({"EVENT_SHOCK", "ILLIQUID"})

_CODE_SHA_CACHE: dict[str, str] = {}


class Strategy(Protocol):
    """Pure strategy protocol; candidate proposals only, never orders."""

    @property
    def spec(self) -> StrategySpec: ...

    def evaluate(self, context: StrategyContext,
                 previous: StrategyState | None) -> StrategyProposal: ...


# --- spec construction -------------------------------------------------------


def strategy_id_for(family: StrategyFamily, mode: Literal["MINUTE", "DAILY"]) -> str:
    if mode not in MODE_SUFFIX:
        raise ValueError("mode_must_be_MINUTE_or_DAILY")
    return f"{family.lower()}_{MODE_SUFFIX[mode]}_v2"


def code_sha256_for(family: StrategyFamily) -> str:
    """SHA-256 over this family module plus the shared registry/state bytes."""
    cached = _CODE_SHA_CACHE.get(family)
    if cached is not None:
        return cached
    root = Path(__file__).resolve().parent
    digest = sha256()
    for name in (FAMILY_MODULES[family], "registry.py", "state.py"):
        digest.update((root / name).read_bytes())
        digest.update(b"\n")
    value = digest.hexdigest()
    _CODE_SHA_CACHE[family] = value
    return value


def factor_key_for(name: str, mode: Literal["MINUTE", "DAILY"]) -> str:
    return f"BASE:{name}:v2.1.0:d{OBSERVATION_DURATIONS[mode]}"


def required_factor_keys(family: StrategyFamily,
                         mode: Literal["MINUTE", "DAILY"]) -> tuple[str, ...]:
    return tuple(factor_key_for(name, mode) for name in FAMILY_FACTOR_NAMES[family])


def default_parameters(family: StrategyFamily) -> dict[str, JsonValue]:
    merged: dict[str, JsonValue] = dict(COMMON_PARAMETER_DEFAULTS)
    for key, value in FAMILY_PARAMETER_DEFAULTS[family].items():
        merged[key] = list(value) if isinstance(value, tuple) else value
    return merged


def validate_strategy_parameters(family: StrategyFamily,
                                 parameters: Mapping[str, JsonValue]) -> None:
    """Exact whitelist: every common key, every family key, nothing else."""
    expected = set(COMMON_PARAMETER_DEFAULTS) | set(FAMILY_PARAMETER_DEFAULTS[family])
    given = set(parameters)
    if given != expected:
        missing = sorted(expected - given)
        unknown = sorted(given - expected)
        raise ValueError(
            f"parameter_keys_must_match_family_whitelist:"
            f"family={family},missing={missing},unknown={unknown}")


def build_strategy_spec(family: StrategyFamily,
                        mode: Literal["MINUTE", "DAILY"]) -> StrategySpec:
    if mode not in MODE_SUFFIX:
        raise ValueError("mode_must_be_MINUTE_or_DAILY")
    parameters = default_parameters(family)
    validate_strategy_parameters(family, parameters)
    if mode == "MINUTE":
        max_holding = Horizon("TRADING_MINUTE", MINUTE_MAX_HOLDING_VALUE)
    else:
        max_holding = Horizon("TRADING_DAY", DAILY_MAX_HOLDING_VALUE)
    return StrategySpec(
        strategy_id=strategy_id_for(family, mode),
        family=family,
        version=STRATEGY_LIBRARY_VERSION,
        parameter_version=f"{strategy_id_for(family, mode)}{PARAMETER_VERSION_SUFFIX}",
        code_sha256=code_sha256_for(family),
        mode=mode,
        parameters=parameters,
        required_factor_keys=required_factor_keys(family, mode),
        allowed_regimes=FAMILY_ALLOWED_REGIMES[family],  # type: ignore[arg-type]
        allowed_flags=FAMILY_ALLOWED_FLAGS[family],  # type: ignore[arg-type]
        confidence_policy_id=m1_policy.confidence_policy(family).policy_id,
        path_barrier_key="ATR_1",
        max_holding=max_holding,
        holding_policy=m1_policy.holding_policy(mode),
        max_layers=FAMILY_MAX_LAYERS[family],
        max_total_lots=FAMILY_MAX_TOTAL_LOTS[family],
        validation_stage="RESEARCH",
    )


def strategy_specs_v2(mode: Literal["MINUTE", "DAILY"]) -> tuple[StrategySpec, ...]:
    families: tuple[StrategyFamily, ...] = (
        "TREND", "BREAKOUT", "PULLBACK", "REVERSION", "GRID", "MARTINGALE", "WAIT")
    return tuple(sorted((build_strategy_spec(family, mode) for family in families),
                        key=lambda spec: spec.strategy_id))


def strategy_registry_v2(mode: Literal["MINUTE", "DAILY"]) -> Mapping[str, Strategy]:
    """Seven family instances keyed by strategy_id (lazy import avoids cycles)."""
    from ak_trading.v2.strategies.breakout import ConfirmedBreakout
    from ak_trading.v2.strategies.grid import FixedInventoryGrid
    from ak_trading.v2.strategies.martingale import LimitedMartingale
    from ak_trading.v2.strategies.pullback import TrendPullback
    from ak_trading.v2.strategies.reversion import RangeReversion
    from ak_trading.v2.strategies.trend import TrendFollowing
    from ak_trading.v2.strategies.wait import WaitStrategy

    classes = {
        "TREND": TrendFollowing,
        "BREAKOUT": ConfirmedBreakout,
        "PULLBACK": TrendPullback,
        "REVERSION": RangeReversion,
        "GRID": FixedInventoryGrid,
        "MARTINGALE": LimitedMartingale,
        "WAIT": WaitStrategy,
    }
    registry: dict[str, Strategy] = {}
    for spec in strategy_specs_v2(mode):
        registry[spec.strategy_id] = classes[spec.family](spec)
    return MappingProxyType(registry)


def evaluate_registry_v2(registry: Mapping[str, Strategy],
                         context_provider: Callable[[Strategy], StrategyContext],
                         previous_states: Mapping[str, StrategyState],
                         ) -> tuple[StrategyProposal, ...]:
    """Evaluate every registered strategy; all outputs are candidates only."""
    proposals: list[StrategyProposal] = []
    for strategy_id in sorted(registry):
        strategy = registry[strategy_id]
        context = context_provider(strategy)
        previous = previous_states.get(strategy_id)
        proposals.append(strategy.evaluate(context, previous))
    return tuple(proposals)


def proposals_csv_row(proposal: StrategyProposal) -> tuple[str, ...]:
    """§8 ``proposals.csv`` projection; Decimal as string, None as empty cell."""
    def dec(value: Decimal | None) -> str:
        return "" if value is None else str(value)

    def num(value: int | None) -> str:
        return "" if value is None else str(value)

    def ident(value: str | None) -> str:
        return "" if value is None else value

    budget_id = "" if proposal.budget is None else proposal.budget.budget_id
    reasons = json.dumps(list(proposal.reasons), ensure_ascii=False,
                         separators=(",", ":"))
    return (
        proposal.proposal_id, proposal.strategy_id, ident(proposal.episode_id),
        proposal.contract_id, str(proposal.decision_nano), proposal.action,
        str(proposal.target_lots), num(proposal.layer_index),
        dec(proposal.trigger_price), dec(proposal.target_price), dec(proposal.stop_price),
        str(proposal.earliest_execution_nano), str(proposal.valid_until_nano),
        ident(proposal.forecast_id), proposal.assessment_id, budget_id, reasons,
    )


# --- shared evaluation helpers -----------------------------------------------


@dataclass(frozen=True)
class GateOutcome:
    ok: bool
    reasons: tuple[str, ...]
    forecast: MarketForecast | None
    window_end_nano: int | None


def fresh_state(strategy_id: str, contract_id: str) -> StrategyState:
    return StrategyState(
        strategy_id=strategy_id, episode_id=None, contract_id=contract_id,
        phase="OBSERVE", setup=None, last_regime=None, direction=0,
        filled_layers=(), layer_filled_lots=(), pending_layer_index=None,
        active_exit_nano=None, pending_exit_nano=None, pending_intent_ids=(),
        budget=None, confirmation_count=0, cooldown_until_nano=None, last_event_id=None,
    )


def evaluation_nano(context: StrategyContext, checked_at_nano: int | None = None) -> int:
    """Resolve this call's evaluation clock without changing decision identity.

    The optional live clock is supported only by the three MINUTE entrypoints.
    Legacy calls keep the decision clock, including DAILY observation semantics.
    """
    if checked_at_nano is None:
        return context.now_nano
    if isinstance(checked_at_nano, bool) or not isinstance(checked_at_nano, int):
        raise TypeError("checked_at_nano_must_be_int")
    if checked_at_nano < context.now_nano:
        raise ValueError("checked_at_nano_before_decision")
    if context.spec.mode != "MINUTE":
        raise ValueError("checked_at_nano_only_supported_for_MINUTE")
    return checked_at_nano


def observation_bars(context: StrategyContext) -> tuple[MarketBar, ...]:
    """Completed, visible, close-bearing bars of this spec's frequency only."""
    duration = OBSERVATION_DURATIONS[context.spec.mode]
    seen: dict[str, MarketBar] = {}
    for bar in context.bars:
        if bar.duration_seconds != duration or not bar.complete:
            continue
        if bar.end_nano > context.now_nano:
            continue
        if bar.close is None or bar.open is None or bar.high is None or bar.low is None:
            continue
        seen.setdefault(bar.bar_id, bar)
    return tuple(sorted(seen.values(), key=lambda bar: (bar.end_nano, bar.bar_id)))


def last_close_of(bars: tuple[MarketBar, ...]) -> Decimal | None:
    return bars[-1].close if bars else None


def factor_decimal(context: StrategyContext, name: str) -> Decimal | None:
    key = factor_key_for(name, context.spec.mode)
    for factor in context.features.values:
        if factor.factor_key == key and isinstance(factor.value, Decimal):
            return factor.value
    return None


def atr_of(context: StrategyContext) -> Decimal | None:
    atr = context.features.atr_points
    if atr is not None and atr > 0:
        return atr
    return factor_decimal(context, "atr14")


def ours_position(context: StrategyContext, spec: StrategySpec) -> PositionSnapshot | None:
    position = context.position
    if position is None or position.lots == 0:
        return None
    if position.strategy_id != spec.strategy_id:
        return None
    return position


def next_open_nano(context: StrategyContext) -> int:
    unit: Literal["TRADING_MINUTE", "TRADING_DAY"] = (
        "TRADING_MINUTE" if context.spec.mode == "MINUTE" else "TRADING_DAY")
    try:
        window_start, _ = m1_clock.horizon_window(
            context.now_nano, context.features.product, Horizon(unit, 1), context.sessions)
        return window_start
    except m1_clock.ClockError:
        step = MINUTE_NANO if context.spec.mode == "MINUTE" else DAY_NANO
        return context.now_nano + step


def observation_span_nano(mode: Literal["MINUTE", "DAILY"]) -> int:
    return MINUTE_NANO if mode == "MINUTE" else DAY_NANO


def setup_expiry_nano(context: StrategyContext, start_nano: int, bars: int) -> int:
    """End of the ``bars``-th planned bar of this spec's frequency after start.

    Uses the frozen product calendar directly (setup windows are 3/5 bars,
    which are not registered ``Horizon`` values); a missing calendar falls
    back to the plain span, never to a natural-date guess.
    """
    product = context.features.product
    ordered = sorted((s for s in context.sessions if s.product == product),
                     key=lambda s: s.start_nano)
    if context.spec.mode == "MINUTE":
        # The creating bar is planned bar #1, so the setup spans ``bars - 1``
        # further planned bars; expiry is the end of the ``bars``-th one.
        remaining = bars - 1
        for session in ordered:
            span = session.end_nano - session.start_nano
            if span % MINUTE_NANO:
                continue
            slots = span // MINUTE_NANO
            slot_index = 0
            cursor = session.start_nano
            while cursor < start_nano and slot_index < slots:
                cursor += MINUTE_NANO
                slot_index += 1
            if cursor < start_nano:
                continue
            usable = slots - slot_index
            if remaining <= usable:
                return session.start_nano + (slot_index + remaining) * MINUTE_NANO
            remaining -= usable
        return start_nano + remaining * MINUTE_NANO
    day_ends: dict[int, int] = {}
    for session in ordered:
        if session.start_nano > start_nano:
            day_ends[session.start_nano] = max(
                day_ends.get(session.start_nano, session.end_nano), session.end_nano)
    starts = sorted(day_ends)
    if 1 <= bars <= len(starts):
        return starts[bars - 1]
    return start_nano + bars * DAY_NANO


def cooldown_end_nano(now_nano: int, product: str, mode: Literal["MINUTE", "DAILY"],
                      sessions: tuple,) -> int | None:
    """Minute strategies cool 30 trading minutes; daily at least one full
    trading day (the whole next trading day must elapse)."""
    try:
        if mode == "MINUTE":
            _, window_end = m1_clock.horizon_window(
                now_nano, product, Horizon("TRADING_MINUTE", 30), sessions)
        else:
            _, window_end = m1_clock.horizon_window(
                now_nano, product, Horizon("TRADING_DAY", 1), sessions)
        return window_end
    except m1_clock.ClockError:
        return None


# --- exit-policy binding selection (M3C P1 skeleton, 08-M3C §5.1–§5.4) --------
#
# Interface contract with the exits package (parallel development):
#   * the binding is SELECTED AND FROZEN BEFORE the first OPEN: the entry
#     StrategySpec x ExitSpec combination is bound pre-OPEN and the episode
#     keeps this identity for its whole life (RunPolicy.fixed_exit_binding_
#     sha256 carries it in FIXED mode);
#   * the exit_spec_sha256s below are the canonical identity projection
#     sha256("<exit_policy_id>|<version>|<parameter_version>") of the
#     registered P1 catalog; when the exits package publishes its registry,
#     M3B consumes its exported ExitSpec.source_sha256 objects directly and
#     any byte divergence is an integration failure, never silently merged;
#   * P1 registered set per 08 §5.4: legacy, trailing, full-flat time decay;
#     the two new families apply only to single-layer TREND/BREAKOUT/
#     PULLBACK; WAIT/REVERSION/GRID/MARTINGALE keep the legacy binding and
#     trailing stays closed for GRID/MARTINGALE.

EXIT_POLICY_VERSION = "1.0.0"
EXIT_LEGACY_RULES_VERSION = "legacy-rules-v2.1.0"
EXIT_PROFILE_IDS: Mapping[str, Mapping[str, str]] = MappingProxyType({
    "MINUTE": MappingProxyType({
        "legacy": "exit_legacy_m1_v1",
        "trailing": "exit_trailing_atr_m1_v1",
        "time_decay": "exit_time_decay_m1_v1",
    }),
    "DAILY": MappingProxyType({
        "legacy": "exit_legacy_d1_v1",
        "trailing": "exit_trailing_atr_d1_v1",
        "time_decay": "exit_time_decay_d1_v1",
    }),
})
P1_EXIT_ELIGIBLE_FAMILIES = frozenset({"TREND", "BREAKOUT", "PULLBACK"})
def _exit_profile_priority(mode: str, profile: str) -> tuple[str, ...]:
    """Arbitration priority lists EXIT POLICY IDS (exits/arbitration.py D3:
    every triggerable ``triggering_policy_id`` must be present; rule tokens
    are rejected with ``arbitration_priority_missing_policy`` and there is no
    runtime fallback). Protective policies arbitrate before the legacy
    baseline; hard-risk exits carry no policy id and always win outright."""
    ids = EXIT_PROFILE_IDS[mode]
    if profile == "legacy":
        return (ids["legacy"],)
    return (ids[profile], ids["legacy"])


_EXIT_SPEC_EXPORTS: Mapping[str, str] = MappingProxyType({
    "MINUTE": MappingProxyType({
        "exit_legacy_m1_v1": "legacy_exit_spec",
        "exit_trailing_atr_m1_v1": "trailing_exit_spec",
        "exit_time_decay_m1_v1": "time_decay_exit_spec",
    }),
    "DAILY": MappingProxyType({
        "exit_legacy_d1_v1": "legacy_exit_spec",
        "exit_trailing_atr_d1_v1": "trailing_exit_spec",
        "exit_time_decay_d1_v1": "time_decay_exit_spec",
    }),
})


def exit_spec_identity(mode: str, exit_policy_id: str) -> str:
    """Identity of the exits package's exported spec (byte-exact): never a
    locally re-derived projection -- exits/evaluate_exit rejects divergent
    hashes (P1 audit finding)."""
    from ak_trading.v2.exits import registry as exits_registry
    export_name = _EXIT_SPEC_EXPORTS[mode][exit_policy_id]
    spec = getattr(exits_registry, export_name)(mode)
    return exits_registry.exit_spec_sha256(spec)


def exit_profiles_for(spec: StrategySpec) -> tuple[str, ...]:
    """Profiles a family may bind in P1 (legacy always; new families only
    for single-layer TREND/BREAKOUT/PULLBACK)."""
    profiles = ["legacy"]
    if spec.family in P1_EXIT_ELIGIBLE_FAMILIES:
        profiles.extend(["trailing", "time_decay"])
    return tuple(profiles)


def select_exit_binding(spec: StrategySpec, profile: str = "legacy") -> "ExitPolicyBinding":
    """Select the pre-OPEN entry x exit binding (08-M3C §5.1: bound before
    the first OPEN; the episode keeps this identity)."""
    from ak_trading.v2.contracts import ExitPolicyBinding, exit_binding_content_hash
    if profile not in EXIT_PROFILE_IDS[spec.mode]:
        raise ValueError("exit_profile_unknown_for_mode")
    if profile not in exit_profiles_for(spec):
        raise ValueError("exit_profile_not_eligible_for_family_p1")
    exit_policy_id = EXIT_PROFILE_IDS[spec.mode][profile]
    exit_hash = exit_spec_identity(spec.mode, exit_policy_id)
    priority = _exit_profile_priority(spec.mode, profile)
    binding_hash = exit_binding_content_hash(
        spec.strategy_id, spec.code_sha256, (exit_hash,), None,
        priority, EXIT_LEGACY_RULES_VERSION)
    return ExitPolicyBinding(
        binding_sha256=binding_hash, strategy_id=spec.strategy_id,
        entry_spec_sha256=spec.code_sha256, exit_spec_sha256s=(exit_hash,),
        sizing_spec_sha256=None, priority=priority,
        legacy_rules_version=EXIT_LEGACY_RULES_VERSION)


def episode_id_for(spec: StrategySpec, contract_id: str, decision_nano: int) -> str:
    return stable_id("episode", f"{spec.strategy_id}|{contract_id}|{decision_nano}")


def ensure_cooldown(context: StrategyContext, state: StrategyState) -> StrategyState:
    """Anchor the trading-minute cooldown of a freshly PAUSED state.

    ``apply_execution`` performs the PAUSED transition without the session
    calendar; the first evaluation after the flat receipt anchors the
    cooldown end here. Anchoring at the first post-flat decision time is
    strictly conservative: it can only delay the unlock, never pull it
    across a lunch/overnight closure.
    """
    if state.phase != "PAUSED" or state.cooldown_until_nano is not None:
        return state
    cooldown = cooldown_end_nano(
        context.now_nano, context.features.product, context.spec.mode, context.sessions)
    if cooldown is None:
        return state
    from ak_trading.v2.contracts import StrategyState as _StrategyState
    return _StrategyState(
        strategy_id=state.strategy_id, episode_id=None,
        contract_id=state.contract_id, phase="PAUSED", setup=None,
        last_regime=context.regime, direction=0, filled_layers=(),
        layer_filled_lots=(), pending_layer_index=None, active_exit_nano=None,
        pending_exit_nano=None, pending_intent_ids=(), budget=None,
        confirmation_count=state.confirmation_count, cooldown_until_nano=cooldown,
        last_event_id=state.last_event_id,
    )


def _bucket_contains(bucket: str, value: float) -> bool | None:
    """Shared M1 binary-bucket membership (single frozen rule with M3A)."""
    try:
        return probability_in_bucket(value, bucket)
    except SchemaError:
        return None


def base_event_of(event: str) -> str:
    if event.startswith("PERSIST:"):
        return event[len("PERSIST:"):]
    if event.startswith("STOP_TOUCH:"):
        return "STOP_TOUCH"
    return event


def _check_evidence(forecast: MarketForecast, policy: ConfidencePolicy,
                    context: StrategyContext, event: str, probability: float,
                    reasons: list[str], *,
                    checked_at_nano: int | None = None) -> None:
    """Select unique applicable evidence before applying its quality gates.

    Other geometry, probability buckets and time intervals cannot mask the
    matching original. Identical repetitions are idempotent; distinct
    applicable records are ambiguous regardless of status or sample quality.
    """
    checked = evaluation_nano(context, checked_at_nano)
    base_event = base_event_of(event)
    range_free = base_event in ("UP_TREND", "DOWN_TREND", "COMPRESSION",
                                "DIRECTION", "PATH", "UP_TOUCH", "DOWN_TOUCH")
    geometry = calibration_geometry_key(
        context.features, None if range_free else forecast.frozen_range, base_event)
    if geometry is None:
        reasons.append("EVIDENCE_GEOMETRY_MISMATCH")
        return
    barrier = forecast.barrier_key if base_event == "PATH" else "COMMON"
    identity_matches = [evidence for evidence in forecast.evidence
                        if evidence.event == event
                        and evidence.model_id == forecast.model_id
                        and evidence.policy_id == policy.policy_id
                        and evidence.product == forecast.product
                        and evidence.session_kind == forecast.session_kind
                        and evidence.duration_seconds == forecast.duration_seconds
                        and evidence.base_regime == forecast.base_regime
                        and evidence.horizon == forecast.horizon
                        and evidence.label_version == forecast.label_version
                        and evidence.barrier_key == barrier]
    if not identity_matches:
        reasons.append("EVIDENCE_IDENTITY_MISMATCH")
        return
    geometry_matches = [evidence for evidence in identity_matches
                        if evidence.geometry_key == geometry]
    if not geometry_matches:
        reasons.append("EVIDENCE_GEOMETRY_MISMATCH")
        return
    bucket_matches: list[CalibrationEvidence] = []
    invalid_bucket = False
    for evidence in geometry_matches:
        if base_event == "PATH":
            contains = evidence.bucket == "ALL"
        else:
            contains = evidence.bucket != "ALL" and _bucket_contains(evidence.bucket, probability)
        if contains is True:
            bucket_matches.append(evidence)
        elif contains is None:
            invalid_bucket = True
    if not bucket_matches:
        reasons.append("EVIDENCE_BUCKET_INVALID" if invalid_bucket else "EVIDENCE_BUCKET_MISMATCH")
        return
    matches = [evidence for evidence in bucket_matches
               if evidence.training_end_nano < evidence.calibration_end_nano < context.now_nano
               and context.now_nano < evidence.valid_until_nano]
    if not matches:
        if any(e.training_end_nano >= e.calibration_end_nano for e in bucket_matches):
            reasons.append("EVIDENCE_IDENTITY_MISMATCH")
        if any(e.calibration_end_nano >= context.now_nano for e in bucket_matches):
            reasons.append("EVIDENCE_NOT_YET_AVAILABLE")
        if any(e.valid_until_nano <= context.now_nano for e in bucket_matches):
            reasons.append("EVIDENCE_EXPIRED")
        return
    matched = matches[0]
    if any(evidence != matched for evidence in matches[1:]):
        reasons.append("EVIDENCE_AMBIGUOUS")
        return
    # Preserve the complete d-applicable set and its ambiguity before expiry at e.
    if matched.valid_until_nano <= checked:
        reasons.append("EVIDENCE_EXPIRED")
    if matched.status != "VALID":
        reasons.append(f"EVIDENCE_{matched.status}")
    if matched.sample_count < policy.minimum_samples:
        reasons.append("EVIDENCE_INSUFFICIENT_SAMPLES")
    if matched.effective_blocks < policy.minimum_blocks:
        reasons.append("EVIDENCE_INSUFFICIENT_BLOCKS")
    if (matched.calibration_error is None
            or matched.calibration_error > policy.maximum_calibration_error):
        reasons.append("EVIDENCE_CALIBRATION_ERROR")


def locate_forecast(forecasts: tuple[MarketForecast, ...], *, barrier_key: str,
                    horizon: Horizon, duration_seconds: int) -> MarketForecast | None:
    for forecast in forecasts:
        if (forecast.horizon == horizon and forecast.barrier_key == barrier_key
                and forecast.duration_seconds == duration_seconds):
            return forecast
    return None


def persistence_key(event: str) -> str:
    """M1 persistence map keys are the bare regime/breakout events."""
    return event[len("PERSIST:"):] if event.startswith("PERSIST:") else event


def _select_and_validate_forecast(context: StrategyContext, spec: StrategySpec,
                                  horizon: Horizon, event: str,
                                  reasons: list[str], *,
                                  checked_at_nano: int | None = None) -> MarketForecast | None:
    checked = evaluation_nano(context, checked_at_nano)
    duration = OBSERVATION_DURATIONS[spec.mode]
    event_key = persistence_key(event)
    matches = [forecast for forecast in context.forecasts
               if forecast.horizon == horizon
               and forecast.barrier_key == spec.path_barrier_key
               and forecast.duration_seconds == duration]
    if not matches:
        reasons.append("FORECAST_MISSING")
        return None
    if len(matches) > 1:
        reasons.append("FORECAST_DUPLICATED")
        return None
    chosen = matches[0]
    chosen_persistence = chosen.persistence_probabilities.get(event_key)
    for other in context.forecasts:
        if (other.horizon == horizon and other.barrier_key != spec.path_barrier_key
                and other.duration_seconds == duration):
            other_persistence = other.persistence_probabilities.get(event_key)
            if (chosen_persistence is not None and other_persistence is not None
                    and other_persistence != chosen_persistence):
                reasons.append("BARRIER_CONFLICT")
            if (chosen.return_before_break_probability is not None
                    and other.return_before_break_probability is not None
                    and other.return_before_break_probability
                    != chosen.return_before_break_probability):
                reasons.append("BARRIER_CONFLICT")
    if chosen.quality.status != "READY":
        reasons.append(f"FORECAST_QUALITY_{chosen.quality.status}")
    if chosen.published_at_nano > checked:
        reasons.append("FORECAST_NOT_YET_PUBLISHED")
    if chosen.expires_at_nano <= checked:
        reasons.append("FORECAST_EXPIRED")
    if chosen.out_of_distribution:
        reasons.append("OUT_OF_DISTRIBUTION")
    return chosen


def admission_decision(context: StrategyContext, *,
                       event: str,
                       side: Literal[-1, 1] | None,
                       horizon: Horizon,
                       required_range: FrozenRange | None = None,
                       require_stop_touch: bool = False,
                       require_return_before_break: bool = False,
                       require_competing_unknown: bool = False,
                       checked_at_nano: int | None = None,
                       ) -> GateOutcome:
    """Necessary joint conditions for any OPEN/ADD risk candidate.

    Ordinary 0.70 / martingale 0.85 persistence and (martingale) stop-touch
    <= 0.10 are necessary, never sufficient; M4 re-checks everything and adds
    net EV, tail loss, capacity and account budget gates.
    """
    checked = evaluation_nano(context, checked_at_nano)
    spec = context.spec
    policy = context.confidence_policy
    reasons: list[str] = []
    if checked_at_nano is not None:
        if checked >= next_open_nano(context):
            reasons.append("EVALUATION_WINDOW_ELAPSED")
        if checked >= context.holding_deadline_nano:
            reasons.append("HOLDING_DEADLINE_REACHED")

    if context.features.quality.status != "READY":
        reasons.append(f"FEATURES_{context.features.quality.status}")
    if context.regime.quality.status != "READY":
        reasons.append(f"REGIME_{context.regime.quality.status}")
    if context.regime.base not in spec.allowed_regimes:
        reasons.append("REGIME_NOT_ALLOWED")
    unknown_flags = [flag for flag in context.regime.flags if flag not in spec.allowed_flags]
    if unknown_flags:
        reasons.append("FLAG_NOT_ALLOWED")
    if context.regime.cross_timeframe in ("CONFLICT", "UNKNOWN"):
        reasons.append("CROSS_TIMEFRAME_NOT_VERIFIED")
    if context.macro.mode == "BLOCKED":
        reasons.append("MACRO_BLOCKED")
    if context.macro.quality.status == "INVALID":
        reasons.append("MACRO_INVALID")

    forecast = _select_and_validate_forecast(
        context, spec, horizon, event, reasons, checked_at_nano=checked_at_nano)
    if forecast is None:
        return GateOutcome(False, tuple(dict.fromkeys(reasons)), None, None)

    if required_range is not None and forecast.frozen_range != required_range:
        reasons.append("RANGE_MISMATCH")

    persistence = forecast.persistence_probabilities.get(persistence_key(event))
    if persistence is None:
        reasons.append("PERSISTENCE_EVENT_MISSING")
    elif persistence < policy.minimum_persistence:
        reasons.append("PERSISTENCE_BELOW_MINIMUM")

    if persistence is not None:
        _check_evidence(forecast, policy, context, event, persistence, reasons,
                        checked_at_nano=checked_at_nano)

    ambiguous = forecast.path_probabilities.get("AMBIGUOUS")
    if ambiguous is not None and ambiguous > policy.maximum_ambiguity:
        reasons.append("AMBIGUITY_EXCEEDED")
    _check_evidence(forecast, policy, context, "PATH",
                    float(sum(forecast.path_probabilities.values())), reasons,
                    checked_at_nano=checked_at_nano)

    if require_stop_touch and side is not None:
        key = "LONG" if side == 1 else "SHORT"
        stop_touch = forecast.stop_touch_probabilities.get(key)
        if stop_touch is None:
            reasons.append("STOP_TOUCH_MISSING")
        else:
            maximum = policy.maximum_stop_touch
            if maximum is not None and stop_touch > maximum:
                reasons.append("STOP_TOUCH_EXCEEDED")
            _check_evidence(forecast, policy, context, f"STOP_TOUCH:{key}",
                            stop_touch, reasons, checked_at_nano=checked_at_nano)

    if require_return_before_break:
        value = forecast.return_before_break_probability
        if value is None:
            reasons.append("RETURN_BEFORE_BREAK_MISSING")
        else:
            _check_evidence(forecast, policy, context, "RETURN_BEFORE_BREAK",
                            value, reasons, checked_at_nano=checked_at_nano)
        if require_competing_unknown:
            terminal_unknown: dict[str, float] = {}
            for entry in forecast.time_cdf:
                if entry.event in ("RETURN_FIRST", "BREAK_FIRST"):
                    current = terminal_unknown.get(entry.event)
                    if current is None or entry.step > current[0]:
                        terminal_unknown[entry.event] = (entry.step, entry.unknown_probability)
            for name in ("RETURN_FIRST", "BREAK_FIRST"):
                entry = terminal_unknown.get(name)
                if entry is None:
                    reasons.append(f"COMPETING_CDF_MISSING_{name}")
                elif entry[1] > policy.maximum_ambiguity:
                    reasons.append("COMPETING_UNKNOWN_EXCEEDED")

    if context.run_policy.purpose == "SHADOW_OPERATIONAL":
        qualification = context.qualification
        if qualification is None or qualification.statistics != "PASS":
            reasons.append("QUALIFICATION_NOT_PASS")
        elif qualification.valid_until_nano <= checked:
            reasons.append("QUALIFICATION_EXPIRED")

    return GateOutcome(not reasons, tuple(dict.fromkeys(reasons)), forecast,
                       forecast.window_end_nano)


def persistence_status(context: StrategyContext, event: str, *,
                       active_exit_nano: int | None,
                       checked_at_nano: int | None = None) -> tuple[str | None, float | None]:
    """Read only the d-selected horizon; check its live publication at e."""
    checked = evaluation_nano(context, checked_at_nano)
    spec = context.spec
    if active_exit_nano is None:
        return "ACTIVE_EXIT_MISSING", None
    horizon = select_management_horizon(
        context.now_nano, context.features.product, spec.mode, active_exit_nano,
        context.sessions)
    if horizon is None:
        return "MANAGEMENT_HORIZON_UNAVAILABLE", None
    matches = [forecast for forecast in context.forecasts
               if forecast.horizon == horizon
               and forecast.barrier_key == spec.path_barrier_key
               and forecast.duration_seconds == OBSERVATION_DURATIONS[spec.mode]]
    if not matches:
        return "FORECAST_UNAVAILABLE", None
    if len(matches) != 1:
        return "FORECAST_DUPLICATED", None
    forecast = matches[0]
    if forecast.quality.status == "INVALID":
        if "FORECAST_LATE" in forecast.quality.reasons:
            return "FORECAST_LATE", None
        return "FORECAST_INVALID", None
    if forecast.published_at_nano > checked:
        return "FORECAST_NOT_YET_PUBLISHED", None
    if forecast.expires_at_nano <= checked:
        return "FORECAST_EXPIRED", None
    if forecast.out_of_distribution:
        return "OUT_OF_DISTRIBUTION", None
    if forecast.quality.status != "READY":
        return f"FORECAST_QUALITY_{forecast.quality.status}", None
    if forecast.window_end_nano > active_exit_nano:
        return "FORECAST_WINDOW_EXCEEDS_ACTIVE_EXIT", None
    value = forecast.persistence_probabilities.get(persistence_key(event))
    if value is None:
        return "PERSISTENCE_EVENT_MISSING", None
    return None, value


def select_management_horizon(
    decision_nano: int, product: str, mode: Literal["MINUTE", "DAILY"],
    active_exit_nano: int, sessions: tuple[SessionWindow, ...],
) -> Horizon | None:
    """First registered horizon whose real new window fits the existing exit.

    RULING 3.3: minute 60/30/15/5/1, daily 20/5/1. Selection reads only the
    trading clock; forecasts and probabilities cannot change this result.
    """
    if isinstance(decision_nano, bool) or not isinstance(decision_nano, int):
        raise TypeError("decision_nano_must_be_int")
    if isinstance(active_exit_nano, bool) or not isinstance(active_exit_nano, int):
        raise TypeError("active_exit_nano_must_be_int")
    if not isinstance(product, str) or not product:
        raise ValueError("product_must_be_non_empty_str")
    if mode not in ADD_HORIZON_VALUES:
        raise ValueError("mode_must_be_MINUTE_or_DAILY")
    unit: Literal["TRADING_MINUTE", "TRADING_DAY"] = (
        "TRADING_MINUTE" if mode == "MINUTE" else "TRADING_DAY")
    for value in ADD_HORIZON_VALUES[mode]:
        horizon = Horizon(unit, value)
        try:
            _, window_end = m1_clock.horizon_window(
                decision_nano, product, horizon, sessions)
        except m1_clock.ClockError:
            continue
        if window_end <= active_exit_nano:
            return horizon
    return None


def select_add_horizon(context: StrategyContext,
                       active_exit_nano: int) -> Horizon | None:
    """Preserve ADD's clock rule; admission and layer gates stay with its callers."""
    return select_management_horizon(
        context.now_nano, context.features.product, context.spec.mode,
        active_exit_nano, context.sessions)


COST_GATED_FAMILIES = frozenset({"REVERSION", "GRID"})


def rule_evidence_blocked(context: StrategyContext) -> bool:
    """CONFLICT-1/FEE-C1-01: rules resolved as UNKNOWN evidence carry no
    verified fee timeline; cost-gated families must not open on them."""
    rule = context.rule
    return rule is not None and rule.evidence == "UNKNOWN"


def can_open_now(context: StrategyContext, *,
                 checked_at_nano: int | None = None) -> tuple[bool, str | None]:
    checked = evaluation_nano(context, checked_at_nano)
    spec = context.spec
    if spec.mode == "MINUTE":
        allowed = m1_clock.can_open_before_close(
            context.now_nano, context.features.product, spec.holding_policy,
            context.sessions)
        if not allowed:
            return False, "OPEN_TOO_CLOSE_TO_CLOSE"
        if checked_at_nano is not None and not m1_clock.can_open_before_close(
                checked, context.features.product, spec.holding_policy,
                context.sessions):
            return False, "OPEN_TOO_CLOSE_TO_CLOSE"
    return True, None


def open_window_fits(context: StrategyContext, window_end_nano: int) -> bool:
    return window_end_nano <= context.holding_deadline_nano


# --- fee and cost helpers -----------------------------------------------------


def fee_leg_cny(leg, price: Decimal, lots: int, rule) -> Decimal:
    return leg.per_lot_cny * lots + leg.notional_rate * price * rule.multiplier * lots


def worst_entry_price(reference: Decimal, side: Literal[-1, 1], rule,
                      initial_atr_points: Decimal) -> Decimal:
    slip = (rule.slippage_ticks + rule.impact_ticks) * rule.price_tick
    adverse = Decimal("0.1") * initial_atr_points + slip
    return reference + side * adverse


def round_trip_cost_per_lot(rule, entry_price: Decimal, exit_price: Decimal) -> Decimal | None:
    """Full round-trip cost per lot: open leg + worst close leg + two slippages."""
    if rule.open_fee is None:
        return None
    close_legs = [leg for leg in (rule.close_fee, rule.close_today_fee) if leg is not None]
    if not close_legs:
        return None
    # Slippage is expressed in price points; costs are CNY, so the points
    # leg must be converted with the contract multiplier.
    slip_cny = (rule.slippage_ticks + rule.impact_ticks) * rule.price_tick         * rule.multiplier
    exit_cost = max(fee_leg_cny(leg, exit_price, 1, rule) for leg in close_legs)
    return fee_leg_cny(rule.open_fee, entry_price, 1, rule) + exit_cost + 2 * slip_cny


def cost_coverage_ok(rule, side: Literal[-1, 1], entry_reference: Decimal,
                     target_price: Decimal, initial_atr_points: Decimal,
                     ratio: Decimal) -> tuple[bool, str | None]:
    """Expected closing spread from the worst allowed entry to the target must
    be at least ``ratio`` times the full round-trip cost."""
    worst = worst_entry_price(entry_reference, side, rule, initial_atr_points)
    cost = round_trip_cost_per_lot(rule, worst, target_price)
    if cost is None:
        return False, "COST_UNKNOWN"
    gross = side * (target_price - worst) * rule.multiplier
    if gross < ratio * cost:
        return False, "COST_COVERAGE_INSUFFICIENT"
    return True, None


# --- proposal builders --------------------------------------------------------


def _proposal_id(context: StrategyContext, action: str, target_lots: int,
                 layer_index: int | None, trigger, target, stop) -> str:
    parts = [
        context.spec.strategy_id, context.features.contract_id,
        str(context.now_nano), action, str(target_lots),
        "" if layer_index is None else str(layer_index),
        "" if trigger is None else str(trigger),
        "" if target is None else str(target),
        "" if stop is None else str(stop),
    ]
    return stable_id("proposal", "|".join(parts))


def _valid_until(context: StrategyContext, earliest: int) -> int:
    return earliest + observation_span_nano(context.spec.mode)


def make_wait_proposal(context: StrategyContext, state: StrategyState,
                       reasons: tuple[str, ...]) -> StrategyProposal:
    earliest = next_open_nano(context)
    return StrategyProposal(
        proposal_id=_proposal_id(context, "WAIT", 0, None, None, None, None),
        strategy_id=context.spec.strategy_id, episode_id=state.episode_id,
        contract_id=state.contract_id, decision_nano=context.now_nano,
        action="WAIT", target_lots=0, layer_index=None,
        trigger_price=None, target_price=None, stop_price=None,
        earliest_execution_nano=earliest,
        valid_until_nano=_valid_until(context, earliest),
        forecast_id=None, assessment_id=context.regime.assessment_id,
        budget=None, payoff=None, next_state=state, reasons=reasons,
    )


def make_open_proposal(context: StrategyContext, *, side: Literal[-1, 1],
                       layer_index: int, trigger_price: Decimal,
                       budget: RoundBudget, forecast: MarketForecast,
                       state: StrategyState, reasons: tuple[str, ...],
                       ) -> StrategyProposal:
    earliest = next_open_nano(context)
    valid = min(forecast.expires_at_nano, forecast.window_end_nano)
    if state.pending_exit_nano is not None:
        valid = min(valid, state.pending_exit_nano)
    signed_target = side * budget.layers[layer_index].lots
    return StrategyProposal(
        proposal_id=_proposal_id(context, "OPEN", signed_target,
                                 layer_index, trigger_price, budget.target_price,
                                 budget.stop_price),
        strategy_id=context.spec.strategy_id, episode_id=state.episode_id,
        contract_id=state.contract_id, decision_nano=context.now_nano,
        action="OPEN", target_lots=signed_target,
        layer_index=layer_index, trigger_price=trigger_price,
        target_price=budget.target_price, stop_price=budget.stop_price,
        earliest_execution_nano=earliest, valid_until_nano=valid,
        forecast_id=forecast.forecast_id, assessment_id=context.regime.assessment_id,
        budget=budget, payoff=None, next_state=state, reasons=reasons,
    )


def make_add_proposal(context: StrategyContext, *, layer_index: int,
                      current_lots: int, forecast: MarketForecast,
                      state: StrategyState, reasons: tuple[str, ...],
                      ) -> StrategyProposal:
    budget = state.budget
    if budget is None:
        raise ValueError("add_requires_budget")
    layer = budget.layers[layer_index]
    signed_target = budget.side * (current_lots + layer.lots)
    earliest = next_open_nano(context)
    valid = min(forecast.expires_at_nano, forecast.window_end_nano)
    if state.pending_exit_nano is not None:
        valid = min(valid, state.pending_exit_nano)
    return StrategyProposal(
        proposal_id=_proposal_id(context, "ADD", signed_target,
                                 layer_index, layer.trigger_price,
                                 budget.target_price, budget.stop_price),
        strategy_id=context.spec.strategy_id, episode_id=state.episode_id,
        contract_id=state.contract_id, decision_nano=context.now_nano,
        action="ADD", target_lots=signed_target,
        layer_index=layer_index, trigger_price=layer.trigger_price,
        target_price=budget.target_price, stop_price=budget.stop_price,
        earliest_execution_nano=earliest, valid_until_nano=valid,
        forecast_id=forecast.forecast_id, assessment_id=context.regime.assessment_id,
        budget=budget, payoff=None, next_state=state, reasons=reasons,
    )


def make_hold_proposal(context: StrategyContext, state: StrategyState,
                        target_lots: int, reasons: tuple[str, ...]) -> StrategyProposal:
    """Keep the family's actual signed net inventory as the HOLD target."""
    earliest = next_open_nano(context)
    valid = earliest + observation_span_nano(context.spec.mode)
    budget = state.budget
    return StrategyProposal(
        proposal_id=_proposal_id(
            context, "HOLD", target_lots, None, None,
            None if budget is None else budget.stop_price,
            None if budget is None else budget.target_price),
        strategy_id=context.spec.strategy_id, episode_id=state.episode_id,
        contract_id=state.contract_id, decision_nano=context.now_nano,
        action="HOLD", target_lots=target_lots, layer_index=None,
        trigger_price=None,
        target_price=None if budget is None else budget.target_price,
        stop_price=None if budget is None else budget.stop_price,
        earliest_execution_nano=earliest, valid_until_nano=valid,
        forecast_id=None, assessment_id=context.regime.assessment_id,
        budget=None, payoff=None, next_state=state, reasons=reasons,
    )


def exiting_state(state: StrategyState, regime: RegimeAssessment) -> StrategyState:
    """EXITING keeps episode/budget/layers/active exit and drops ADD pendings."""
    return StrategyState(
        strategy_id=state.strategy_id, episode_id=state.episode_id,
        contract_id=state.contract_id, phase="EXITING", setup=state.setup,
        last_regime=regime, direction=state.direction,
        filled_layers=state.filled_layers, layer_filled_lots=state.layer_filled_lots,
        pending_layer_index=None, active_exit_nano=state.active_exit_nano,
        pending_exit_nano=None, pending_intent_ids=(), budget=state.budget,
        confirmation_count=state.confirmation_count,
        cooldown_until_nano=state.cooldown_until_nano,
        last_event_id=state.last_event_id,
    )


def make_exit_proposal(context: StrategyContext, state: StrategyState,
                       reasons: tuple[str, ...]) -> StrategyProposal:
    earliest = next_open_nano(context)
    budget = state.budget
    exiting = exiting_state(state, context.regime)
    return StrategyProposal(
        proposal_id=_proposal_id(context, "EXIT", 0, None, None,
                                 None if budget is None else budget.target_price,
                                 None if budget is None else budget.stop_price),
        strategy_id=context.spec.strategy_id, episode_id=state.episode_id,
        contract_id=state.contract_id, decision_nano=context.now_nano,
        action="EXIT", target_lots=0, layer_index=None,
        trigger_price=None,
        target_price=None if budget is None else budget.target_price,
        stop_price=None if budget is None else budget.stop_price,
        earliest_execution_nano=earliest,
        valid_until_nano=_valid_until(context, earliest),
        forecast_id=None, assessment_id=context.regime.assessment_id,
        budget=None, payoff=None, next_state=exiting, reasons=reasons,
    )


# --- shared position management ------------------------------------------------


def evaluate_exits(context: StrategyContext, state: StrategyState,
                   family_extras: tuple[str, ...] = (), *,
                   checked_at_nano: int | None = None) -> tuple[str, ...]:
    """Priority-ordered exit reasons; hard risks first, persistence last.

    Stop, limit/liquidity risk and planned closes never wait for soft
    confirmation; the persistence exit is the single soft confirmation
    observation. Hysteresis band handling (exit_persistence..minimum) is the
    caller's hold decision, this function reports only below-exit cases.
    """
    checked = evaluation_nano(context, checked_at_nano)
    budget = state.budget
    if budget is None:
        return ("BUDGET_MISSING",)
    reasons: list[str] = []
    side = budget.side
    if context.account.killed:
        reasons.append("ACCOUNT_KILLED")
    bars = observation_bars(context)
    bar = bars[-1] if bars else None
    if bar is not None:
        if side == 1 and bar.low is not None and bar.low <= budget.stop_price:
            reasons.append("STOP_TOUCHED")
        if side == -1 and bar.high is not None and bar.high >= budget.stop_price:
            reasons.append("STOP_TOUCHED")
        if side == 1 and bar.high is not None and bar.high >= budget.target_price:
            reasons.append("TARGET_REACHED")
        if side == -1 and bar.low is not None and bar.low <= budget.target_price:
            reasons.append("TARGET_REACHED")
    if state.active_exit_nano is not None and checked >= state.active_exit_nano:
        reasons.append("HORIZON_REACHED")
    if context.regime.base == "UNKNOWN":
        reasons.append("REGIME_UNKNOWN")
    if HARD_RISK_FLAGS.intersection(context.regime.flags):
        reasons.append("EVENT_LIQUIDITY_RISK")
    frozen = budget.frozen_range
    if (context.spec.family in RANGE_FAMILIES and bar is not None
            and bar.close is not None and frozen is not None):
        if bar.close < frozen.lower or bar.close > frozen.upper:
            reasons.append("RANGE_BROKEN")
    reasons.extend(family_extras)
    if reasons:
        return tuple(dict.fromkeys(reasons))
    event = "PERSIST:UP_TREND" if side == 1 else "PERSIST:DOWN_TREND"
    if context.spec.family in RANGE_FAMILIES:
        event = "PERSIST:ORDERED_RANGE"
    elif context.spec.family == "BREAKOUT":
        event = "PERSIST:UP_BREAKOUT" if side == 1 else "PERSIST:DOWN_BREAKOUT"
    status, value = persistence_status(context, event,
                                       active_exit_nano=state.active_exit_nano,
                                       checked_at_nano=checked_at_nano)
    if status is not None:
        return (status,)
    if value is not None and value < context.confidence_policy.exit_persistence:
        return ("PERSISTENCE_BELOW_EXIT",)
    return ()


def hysteresis_hold(context: StrategyContext, state: StrategyState, *,
                    checked_at_nano: int | None = None) -> bool:
    """True while persistence sits in the registered hysteresis band."""
    evaluation_nano(context, checked_at_nano)
    budget = state.budget
    if budget is None:
        return False
    side = budget.side
    if context.spec.family in RANGE_FAMILIES:
        event = "PERSIST:ORDERED_RANGE"
    elif context.spec.family == "BREAKOUT":
        event = "PERSIST:UP_BREAKOUT" if side == 1 else "PERSIST:DOWN_BREAKOUT"
    else:
        event = "PERSIST:UP_TREND" if side == 1 else "PERSIST:DOWN_TREND"
    status, value = persistence_status(context, event,
                                       active_exit_nano=state.active_exit_nano,
                                       checked_at_nano=checked_at_nano)
    if status is not None or value is None:
        return False
    return context.confidence_policy.exit_persistence <= value \
        < context.confidence_policy.minimum_persistence


def with_regime(state: StrategyState, regime: RegimeAssessment) -> StrategyState:
    from ak_trading.v2.contracts import StrategyState as _StrategyState
    return _StrategyState(
        strategy_id=state.strategy_id, episode_id=state.episode_id,
        contract_id=state.contract_id, phase=state.phase, setup=state.setup,
        last_regime=regime, direction=state.direction,
        filled_layers=state.filled_layers, layer_filled_lots=state.layer_filled_lots,
        pending_layer_index=state.pending_layer_index,
        active_exit_nano=state.active_exit_nano, pending_exit_nano=state.pending_exit_nano,
        pending_intent_ids=state.pending_intent_ids, budget=state.budget,
        confirmation_count=state.confirmation_count,
        cooldown_until_nano=state.cooldown_until_nano,
        last_event_id=state.last_event_id,
    )


def range_entry_setup(context: StrategyContext, state: StrategyState,
                      fraction: Decimal, expiry_bars: int,
                      ) -> tuple[StrategySetup | None, int, str | None]:
    """Advance the shared auditable range entry observation.

    Long: a completed close at or below ``L + fraction*W`` arms the setup;
    a later completed close strictly re-crossing that level while still
    below M confirms. Short is the mirror at ``U - fraction*W``. First touch
    and re-cross must be different bars; middle-of-range or one-sided
    adverse crossings never confirm. Returns the new setup, the side and a
    WAIT reason when no structure exists.
    """
    frozen = context.regime.frozen_range
    if frozen is None:
        return None, 0, "FROZEN_RANGE_MISSING"
    bars = observation_bars(context)
    if not bars:
        return None, 0, "NO_COMPLETED_BARS"
    bar = bars[-1]
    close = bar.close
    width = frozen.upper - frozen.lower
    long_level = frozen.lower + fraction * width
    short_level = frozen.upper - fraction * width
    center = frozen.center

    previous = state.setup
    if previous is not None and (
            previous.frozen_range is None or previous.frozen_range.range_id != frozen.range_id
            or context.now_nano > previous.expires_at_nano):
        previous = None
    if previous is not None and previous.last_bar_id == bar.bar_id:
        return previous, previous.side, None

    if previous is None:
        side = 0
        if close <= long_level:
            side = 1
        elif close >= short_level:
            side = -1
        if side == 0:
            return None, 0, "RANGE_MIDDLE_WAIT"
        setup = StrategySetup(
            setup_id=stable_id(
                "setup", f"{context.spec.strategy_id}|{bar.bar_id}|{side}|{frozen.range_id}"),
            strategy_id=context.spec.strategy_id, contract_id=bar.contract_id,
            side=side, started_at_nano=bar.end_nano,
            expires_at_nano=setup_expiry_nano(context, bar.end_nano, expiry_bars),
            observed_bars=1, frozen_range=frozen, frozen_ma=None,
            frozen_atr=context.features.atr_points or Decimal("1"),
            trigger_level=long_level if side == 1 else short_level,
            armed=True, consecutive_confirmations=0, previous_close=close,
            last_bar_id=bar.bar_id)
        return setup, side, None

    side = previous.side
    level = previous.trigger_level
    prior_close = previous.previous_close
    confirmed = previous.consecutive_confirmations
    crossed_back = (
        (side == 1 and prior_close is not None and close > level and close < center)
        or (side == -1 and prior_close is not None and close < level and close > center))
    if crossed_back:
        confirmed += 1
    setup = StrategySetup(
        setup_id=previous.setup_id, strategy_id=previous.strategy_id,
        contract_id=previous.contract_id, side=side,
        started_at_nano=previous.started_at_nano,
        expires_at_nano=previous.expires_at_nano,
        observed_bars=previous.observed_bars + 1, frozen_range=previous.frozen_range,
        frozen_ma=previous.frozen_ma, frozen_atr=previous.frozen_atr,
        trigger_level=level, armed=previous.armed, consecutive_confirmations=confirmed,
        previous_close=close, last_bar_id=bar.bar_id)
    return setup, side, None


def observation_wait(context: StrategyContext, state: StrategyState, setup,
                     confirmation: int, reasons: tuple[str, ...],
                     phase: str = "OBSERVE") -> StrategyProposal:
    """WAIT proposal that still carries the saveable observation fields."""
    from ak_trading.v2.contracts import StrategyState as _StrategyState
    nxt = _StrategyState(
        strategy_id=context.spec.strategy_id, episode_id=None,
        contract_id=state.contract_id, phase=phase, setup=setup,
        last_regime=context.regime, direction=state.direction if setup is None else setup.side,
        filled_layers=(), layer_filled_lots=(), pending_layer_index=None,
        active_exit_nano=None, pending_exit_nano=None, pending_intent_ids=(),
        budget=None, confirmation_count=confirmation,
        cooldown_until_nano=state.cooldown_until_nano,
        last_event_id=state.last_event_id)
    return make_wait_proposal(context, nxt, reasons)


def evaluate_common(strategy: Strategy, context: StrategyContext,
                    previous: StrategyState | None, *,
                    observe: Callable[[StrategyContext, StrategyState], StrategyProposal],
                    manage: Callable[[StrategyContext, StrategyState, PositionSnapshot],
                                     StrategyProposal]) -> StrategyProposal:
    """Family-independent skeleton: cooldown, hard-risk priority, position
    management before fresh qualification, then the observation path."""
    spec = strategy.spec
    state = previous if previous is not None else fresh_state(
        spec.strategy_id, context.features.contract_id)
    state = ensure_cooldown(context, state)
    if state.cooldown_until_nano is not None and context.now_nano < state.cooldown_until_nano:
        return make_wait_proposal(context, state, ("COOLDOWN_ACTIVE",))
    position = ours_position(context, spec)
    hard_risk = bool(HARD_RISK_FLAGS.intersection(context.regime.flags))
    if state.episode_id is not None:
        if position is not None:
            return manage(context, state, position)
        if state.phase == "EXITING":
            return make_wait_proposal(
                context, with_regime(state, context.regime), ("EXIT_PENDING",))
        return make_wait_proposal(
            context, with_regime(state, context.regime), ("AWAITING_ENTRY_RECEIPT",))
    if hard_risk:
        return make_wait_proposal(
            context, with_regime(state, context.regime), ("EVENT_LIQUIDITY_RISK",))
    if rule_evidence_blocked(context) and spec.family in COST_GATED_FAMILIES:
        return make_wait_proposal(
            context, with_regime(state, context.regime), ("RULE_EVIDENCE_UNKNOWN",))
    return observe(context, state)
