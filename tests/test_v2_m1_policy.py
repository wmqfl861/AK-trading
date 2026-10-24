"""M1-07 policy: frozen thresholds, budgets, cost scenarios, stress hand-cases."""
from __future__ import annotations

from decimal import Decimal

import pytest

from ak_trading.v2 import contracts as c
from ak_trading.v2 import policy as p
from ak_trading.v2 import schemas as s

ZERO = "0" * 64


def trading_rule(*, notional: str = "0.0001", slippage: str = "1",
                 with_fees: bool = True) -> c.TradingRule:
    leg = c.FeeLeg(per_lot_cny=Decimal("0"), notional_rate=Decimal(notional),
                   explicit_zero=False) if with_fees else None
    return c.TradingRule(
        rule_id="RL", contract_id="SYN.rb2609", effective_from_nano=0,
        effective_until_nano=10**19, available_at_nano=0, multiplier=Decimal("10"),
        price_tick=Decimal("1"), lot_step=1, min_open_lots=1, open_fee=leg,
        close_fee=leg, close_today_fee=leg, long_margin_rate=Decimal("0.09"),
        short_margin_rate=Decimal("0.09"), limit_up=None, limit_down=None,
        slippage_ticks=Decimal(slippage), impact_ticks=Decimal("0"),
        participation_rate=Decimal("0.1"), evidence="SCENARIO", source_sha256=ZERO)


def layer(index: int, trigger: str, reference: str, lots: int) -> c.LayerSpec:
    return c.LayerSpec(index=index, trigger_price=Decimal(trigger),
                       reference_entry_price=Decimal(reference), lots=lots)


def test_frozen_policy_values() -> None:
    # Exact frozen research policy table; Decimals serialized as strings and
    # ordinary/martingale dual values nested.
    assert dict(p.research_policy()) == {
        "policy_version": "research-policy-v2.1.0",
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
    # Holding policies: MINUTE keeps 30/5 intraday cutoffs; DAILY uses 0/0 only
    # to disable those intraday parameters.
    assert p.holding_policy("MINUTE") == c.HoldingPolicy(True, True, False, 30, 5)
    assert p.holding_policy("DAILY") == c.HoldingPolicy(False, True, True, 0, 0)
    with pytest.raises(ValueError):
        p.holding_policy("HOUR")
    # Stress policy fixed constants.
    assert p.stress_policy() == c.StressPolicy(
        policy_id="stress-v2.1.0", gap_atr_multiple=Decimal("3"), locked_trading_days=3,
        adverse_atr_per_day=Decimal("1"), release_extra_atr=Decimal("1"),
        fee_multiplier=Decimal("2"), slippage_multiplier=Decimal("2"),
        margin_multiplier=Decimal("1.5"))
    # Confidence policies: ordinary vs martingale fully isolated, ordinary stop None.
    ordinary = p.confidence_policy("TREND")
    for family in ("TREND", "BREAKOUT", "PULLBACK", "REVERSION", "GRID", "WAIT"):
        assert p.confidence_policy(family).policy_id == "confidence-ordinary-v2.1.0"
    martingale = p.confidence_policy("MARTINGALE")
    assert martingale.policy_id == "confidence-martingale-v2.1.0"
    assert ordinary.policy_id != martingale.policy_id
    assert (ordinary.minimum_persistence, ordinary.maximum_stop_touch,
            ordinary.minimum_samples, ordinary.minimum_blocks,
            ordinary.maximum_calibration_error, ordinary.maximum_ambiguity,
            ordinary.exit_persistence) == (0.70, None, 200, 30, 0.05, 0.05, 0.65)
    assert (martingale.minimum_persistence, martingale.maximum_stop_touch,
            martingale.minimum_samples, martingale.minimum_blocks,
            martingale.maximum_calibration_error, martingale.maximum_ambiguity,
            martingale.exit_persistence) == (0.85, 0.10, 500, 60, 0.03, 0.02, 0.80)
    assert ordinary.minimum_conservative_ev_cny == Decimal("0")
    assert martingale.confirmation_observations == 3
    with pytest.raises(ValueError):
        p.confidence_policy("NOT_A_FAMILY")
    # Barrier keys: COMMON evidence never mixes with an ATR_1 path barrier.
    assert p.research_policy()["policy_version"] == "research-policy-v2.1.0"
    assert s.probability_in_bucket(0.85, "[0.85,0.90)") and \
        not s.probability_in_bucket(0.85, "[0.80,0.85)")


def test_calculate_stress_loss_hand_cases() -> None:
    rule = trading_rule()
    stress = p.stress_policy()
    # Hand case A: single long layer, reference 100, ATR 2, stop 96.
    # entry = 100 + 0.1*ATR(0.2) + 1 tick slip = 101.2
    # STOP exit = 96 - 2*1 = 94        gross = 1*(101.2-94)*10 = 72
    # GAP  exit = 96 - 3*2 - 2 = 88    gross = 132
    # NOST exit = 96 - 4*2 - 2 = 86    gross = 152  <- worst
    # fees = 2 * (0.0001*10*(101.2*1) + 0.0001*10*(86*1)) = 2*(0.1012+0.086) = 0.3744
    loss = p.calculate_stress_loss(
        (layer(0, "99.8", "100", 1),), 1, Decimal("96"), Decimal("2"), rule, stress)
    assert loss == Decimal("152.3744")
    # Short side: reference 100, stop 104, ATR 2. entry = 98.8; NO_STOP exit =
    # 104 + 8 + 2 = 114; gross = 152; fees = 2*(0.0001*10*98.8 + 0.0001*10*114)
    # = 2*(0.0988+0.114) = 0.4256 (notional fees are not mirror-symmetric).
    short = p.calculate_stress_loss(
        (layer(0, "100.2", "100", 1),), -1, Decimal("104"), Decimal("2"), rule, stress)
    assert short == Decimal("152.4256")
    # Hand case B: martingale 1/2/4 layers at references 100/98/96, stop 95, ATR 2.
    # entries = 101.2 / 99.2 / 97.2; NO_STOP exit = 95 - 8 - 2 = 85
    # gross = (1*16.2 + 2*14.2 + 4*12.2)*10 = 934
    # entry fees = 0.001*(101.2 + 2*99.2 + 4*97.2) = 0.6884
    # exit fee  = 0.001*85*7 = 0.595 ; fees = 2*(0.6884+0.595) = 2.5668
    martingale = p.calculate_stress_loss(
        (layer(0, "99.8", "100", 1), layer(1, "97.8", "98", 2), layer(2, "95.8", "96", 4)),
        1, Decimal("95"), Decimal("2"), rule, stress)
    assert martingale == Decimal("936.5668")
    # The trigger line is NOT the execution reference: moving only trigger_price
    # (reference unchanged) cannot change the frozen budget.
    moved_trigger = p.calculate_stress_loss(
        (layer(0, "90.8", "100", 1), layer(1, "89.8", "98", 2), layer(2, "88.8", "96", 4)),
        1, Decimal("95"), Decimal("2"), rule, stress)
    assert moved_trigger == martingale
    # Non-positive stress execution prices are INVALID, never truncated.
    with pytest.raises(ValueError):
        p.calculate_stress_loss((layer(0, "50", "50", 1),), 1, Decimal("2"),
                                Decimal("2"), rule, stress)
    # Unknown fees must not silently become zero cost.
    with pytest.raises(ValueError):
        p.calculate_stress_loss((layer(0, "99.8", "100", 1),), 1, Decimal("96"),
                                Decimal("2"), trading_rule(with_fees=False), stress)
    # A profitable worst case is floored at a non-negative loss.
    tiny = p.calculate_stress_loss(
        (layer(0, "200", "1", 1),), 1, Decimal("199"), Decimal("2"), rule, stress)
    assert tiny >= Decimal("0")
    with pytest.raises(TypeError):
        p.calculate_stress_loss((), 1, Decimal("96"), Decimal("2"), rule, stress)
    with pytest.raises(TypeError):
        p.calculate_stress_loss([layer(0, "99.8", "100", 1)], 1, Decimal("96"),
                                Decimal("2"), rule, stress)
    with pytest.raises(TypeError):
        p.calculate_stress_loss((layer(0, "99.8", "100", 1),), 1, Decimal("96"),
                                Decimal("2"), rule, None)
