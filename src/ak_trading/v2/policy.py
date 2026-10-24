"""Frozen research policies and the M1 pure stress-loss money formula.

All values here are the initial research specification of devplan-v2.1.0
(01-M1 §6.2), not verified optima and not a live-trading authorization.
Amounts and ratios use ``Decimal`` and are serialized as strings in the
research policy mapping.
"""
from __future__ import annotations

from decimal import Decimal
from types import MappingProxyType
from typing import Literal, Mapping

from ak_trading.v2.contracts import (
    ConfidencePolicy,
    HoldingPolicy,
    LayerSpec,
    StrategyFamily,
    StressPolicy,
    TradingRule,
    JsonValue,
    STRATEGY_FAMILIES,
)

RESEARCH_POLICY_VERSION = "research-policy-v2.1.0"
MACRO_POLICY_VERSION = "macro-risk-v2.1.0"
STRESS_POLICY_ID = "stress-v2.1.0"


def confidence_policy(family: StrategyFamily) -> ConfidencePolicy:
    """Frozen admission thresholds; ordinary vs martingale policies are isolated."""
    if family not in STRATEGY_FAMILIES:
        raise ValueError("family_not_in_allowed_enum")
    if family == "MARTINGALE":
        return ConfidencePolicy(
            policy_id="confidence-martingale-v2.1.0",
            minimum_persistence=0.85,
            maximum_stop_touch=0.10,
            minimum_samples=500,
            minimum_blocks=60,
            maximum_calibration_error=0.03,
            maximum_ambiguity=0.02,
            confirmation_observations=3,
            exit_persistence=0.80,
            cooldown_minutes=30,
            minimum_conservative_ev_cny=Decimal("0"),
        )
    return ConfidencePolicy(
        policy_id="confidence-ordinary-v2.1.0",
        minimum_persistence=0.70,
        maximum_stop_touch=None,
        minimum_samples=200,
        minimum_blocks=30,
        maximum_calibration_error=0.05,
        maximum_ambiguity=0.05,
        confirmation_observations=3,
        exit_persistence=0.65,
        cooldown_minutes=30,
        minimum_conservative_ev_cny=Decimal("0"),
    )


def holding_policy(mode: Literal["MINUTE", "DAILY"]) -> HoldingPolicy:
    """Default holding windows: minute strategies stay inside one trading day
    (lunch allowed, night-to-day forbidden); daily strategies may span days
    and use 0/0 only to disable intraday cutoff parameters (max_holding and
    lifecycle exits still apply)."""
    if mode not in ("MINUTE", "DAILY"):
        raise ValueError("mode_must_be_MINUTE_or_DAILY")
    if mode == "MINUTE":
        return HoldingPolicy(
            intraday=True,
            allow_lunch=True,
            allow_night_to_day=False,
            stop_open_before_minutes=30,
            flatten_before_minutes=5,
        )
    return HoldingPolicy(
        intraday=False,
        allow_lunch=True,
        allow_night_to_day=True,
        stop_open_before_minutes=0,
        flatten_before_minutes=0,
    )


def stress_policy() -> StressPolicy:
    return StressPolicy(
        policy_id=STRESS_POLICY_ID,
        gap_atr_multiple=Decimal("3"),
        locked_trading_days=3,
        adverse_atr_per_day=Decimal("1"),
        release_extra_atr=Decimal("1"),
        fee_multiplier=Decimal("2"),
        slippage_multiplier=Decimal("2"),
        margin_multiplier=Decimal("1.5"),
    )


def calculate_stress_loss(layers: tuple[LayerSpec, ...], side: Literal[-1, 1],
                          stop_price: Decimal, initial_atr_points: Decimal,
                          rule: TradingRule, policy: StressPolicy) -> Decimal:
    """Worst net loss of the finite stress scenarios, as a non-negative amount.

    Scenarios compared (M1 §6.2): plain stop, GAP (next legal slot jumps to
    ``stop - side*3*initial_ATR``) and NO_STOP (3 locked trading days, then
    release at ``stop - side*4*initial_ATR``). Entries use the worst allowed
    fill per layer relative to ``LayerSpec.reference_entry_price`` (the known
    budget fill reference — the price trigger line is never treated as a
    known execution reference): reference plus 0.1 initial ATR adverse plus
    the established slippage/impact; stress executions add 2x slippage/
    impact adverse and fees are 2x the higher applicable frozen fee leg.
    The result is the M1 pure money formula; M4 re-derives path execution
    against the same policy and must match. A non-positive execution price
    is INVALID, never truncated.
    """
    if not isinstance(layers, tuple) or not layers or any(not isinstance(l, LayerSpec) for l in layers):
        raise TypeError("layers_must_be_non_empty_tuple_of_LayerSpec")
    if side not in (-1, 1) or isinstance(side, bool):
        raise ValueError("side_must_be_minus1_or_1")
    if not isinstance(stop_price, Decimal) or not stop_price.is_finite() or stop_price <= 0:
        raise ValueError("stop_price_must_be_positive")
    if not isinstance(initial_atr_points, Decimal) or not initial_atr_points.is_finite() or initial_atr_points <= 0:
        raise ValueError("initial_atr_points_must_be_positive")
    if not isinstance(rule, TradingRule):
        raise TypeError("rule_must_be_TradingRule")
    if not isinstance(policy, StressPolicy):
        raise TypeError("policy_must_be_StressPolicy")

    # F14: every applicable fee leg must be known — an unknown leg can never
    # be priced by substituting another known leg, and zero legs are unknown.
    legs = [rule.open_fee, rule.close_fee, rule.close_today_fee]
    if any(leg is None for leg in legs):
        raise ValueError("fee_leg_required_unknown_fee_must_not_be_zero")

    slippage_ticks = rule.slippage_ticks + rule.impact_ticks
    slip_cost = slippage_ticks * rule.price_tick
    entry_adverse = Decimal("0.1") * initial_atr_points + slip_cost
    worst_entries = tuple(layer.reference_entry_price + side * entry_adverse for layer in layers)
    total_lots = sum(layer.lots for layer in layers)
    stress_slip = policy.slippage_multiplier * slip_cost

    def higher_leg_fee(price: Decimal, lots: int) -> Decimal:
        return max(
            leg.per_lot_cny * lots + leg.notional_rate * price * rule.multiplier * lots
            for leg in legs
        )

    def scenario_loss(exit_base: Decimal) -> Decimal:
        execution = exit_base - side * stress_slip
        if execution <= 0:
            raise ValueError("stress_execution_price_non_positive_INVALID")
        if any(entry <= 0 for entry in worst_entries):
            raise ValueError("worst_allowed_entry_price_non_positive_INVALID")
        gross = sum(
            layer.lots * side * (entry - execution) * rule.multiplier
            for layer, entry in zip(layers, worst_entries)
        )
        fees = policy.fee_multiplier * (
            sum(higher_leg_fee(entry, layer.lots) for layer, entry in zip(layers, worst_entries))
            + higher_leg_fee(execution, total_lots)
        )
        return gross + fees

    losses = (
        scenario_loss(stop_price),
        scenario_loss(stop_price - side * policy.gap_atr_multiple * initial_atr_points),
        scenario_loss(
            stop_price
            - side * (policy.gap_atr_multiple + policy.release_extra_atr) * initial_atr_points
        ),
    )
    worst = max(losses)
    return worst if worst > 0 else Decimal("0")


def research_policy() -> Mapping[str, JsonValue]:
    """The frozen initial research policy table (M1 §6.2); Decimals as strings.

    Keys with ordinary/martingale dual values are nested
    ``{"ordinary": v, "martingale": v}`` objects; slash-paired rows split
    into independent keys of the same names.
    """
    policy: dict[str, JsonValue] = {
        "policy_version": RESEARCH_POLICY_VERSION,
        "ordinary_persistence": 0.70,
        "martingale_persistence": 0.85,
        "martingale_stop_touch": 0.10,
        "minimum_samples": {"ordinary": 200, "martingale": 500},
        "minimum_blocks": {"ordinary": 30, "martingale": 60},
        "maximum_calibration_error": {"ordinary": 0.05, "martingale": 0.03},
        "maximum_ambiguity": {"ordinary": 0.05, "martingale": 0.02},
        "confirmation_observations": 3,
        "ordinary_exit_persistence": 0.65,
        "martingale_exit_persistence": 0.80,
        "cooldown_minutes": 30,
        "daily_cooldown_trading_days": 1,
        "maximum_round_loss_ratio": "0.005",
        "maximum_day_loss_ratio": "0.02",
        "maximum_drawdown_ratio": "0.10",
        "maximum_margin_ratio": "0.40",
        "maximum_strategy_margin_ratio": "0.10",
        "maximum_product_margin_ratio": "0.15",
        "maximum_group_margin_ratio": "0.25",
        "maximum_round_lots": 7,
        "martingale_layer_multipliers": [1, 2, 4],
        "minimum_conservative_ev_cny": "0",
        "maximum_cvar95_to_budget": "1",
        "maximum_stress_loss_to_equity": "0.02",
        "minute_freshness_seconds": 120,
        "daily_background_max_trading_days": 1,
        "allow_wide_range": False,
        "allow_event_breakout": False,
        "allow_countertrend": False,
        "minute_embargo": 60,
        "daily_embargo": 20,
        "final_holdout_policy": "ONCE",
    }
    return MappingProxyType(policy)
