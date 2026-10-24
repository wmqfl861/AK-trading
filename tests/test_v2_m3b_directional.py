"""M3B-03: trend, breakout and pullback mirror rules (04-M3B §6.1–§6.3).

Shared synthetic builders come from this node's ``test_v2_m3b_matrix``
module (the M1 shared fixture is not published yet and ``tests/conftest.py``
must not be edited).
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from ak_trading.v2 import policy as m1_policy
from ak_trading.v2.contracts import FrozenRange, Horizon, PositionSnapshot
from ak_trading.v2.probability.regimes import freeze_range
from ak_trading.v2.strategies.registry import build_strategy_spec
from ak_trading.v2.strategies.state import commit_observation

from test_v2_m3b_matrix import (
    BASE,
    MINUTE,
    account,
    drive_open,
    evidence,
    features,
    forecast,
    frozen_range,
    holding_account,
    holding_state,
    make_context,
    minute_bars,
    range_forecasts,
    regime,
    rule,
    sessions,
)

HORIZON = Horizon("TRADING_MINUTE", 30)


def trend_forecasts(snap, persistence_key="UP_TREND", value=0.75, bucket="[0.75,0.80)"):
    policy = m1_policy.confidence_policy("TREND").policy_id
    return (forecast(
        HORIZON, base_regime=persistence_key, persistence={persistence_key: value},
        evidence_items=(
            evidence(f"PERSIST:{persistence_key}", bucket, policy, persistence_key,
                     HORIZON, snap, None),
            evidence("PATH", "ALL", policy, persistence_key, HORIZON, snap, None))),)


def drive_trend(ma20, ma60, atr=Decimal("4"), closes=None, regime_base="UP_TREND",
                forecasts=None):
    if closes is None:
        step = 1 if ma20 > ma60 else -1
        start = 100 if step == 1 else 121
        closes = [start + step * index for index in range(24)]
    snaps = tuple(
        features(Decimal(str(closes[index])), Decimal(str(atr)),
                 {"close": Decimal(str(closes[index])), "ma20": Decimal(str(ma20)),
                  "ma60": Decimal(str(ma60)), "atr14": Decimal(str(atr)),
                  "er20": Decimal("0.5")})
        for index in (len(closes) - 3, len(closes) - 2, len(closes) - 1))
    if forecasts is None:
        forecasts = trend_forecasts(
            snaps[-1], "UP_TREND" if regime_base == "UP_TREND" else "DOWN_TREND")
    bars = tuple(minute_bars(closes[:index]) for index in
                 (len(closes) - 2, len(closes) - 1, len(closes)))
    return drive_open("TREND", regime_base=regime_base, bars_sequence=bars,
                      features_sequence=snaps, forecasts=forecasts)


# --- trend (§6.1) ---------------------------------------------------------------


def test_trend_long_spread_boundary_025():
    proposal, _ = drive_trend(ma20=101, ma60=100, atr=4)
    assert proposal.action == "OPEN"
    reference = Decimal("123")
    assert proposal.target_price == reference + Decimal(2) * Decimal(4)
    assert proposal.stop_price == reference - Decimal(4)
    assert proposal.next_state.direction == 1


def test_trend_spread_just_below_blocks():
    proposal, _ = drive_trend(ma20=Decimal("100.999"), ma60=100, atr=4)
    assert proposal.action == "WAIT"
    assert "TREND_SPREAD_NOT_QUALIFIED" in proposal.reasons


def test_trend_requires_all_three_closes_above_own_ma():
    closes = [100 + index for index in range(19)] + [100, 121, 122]
    proposal, _ = drive_trend(ma20=101, ma60=100, closes=closes)
    assert proposal.action == "WAIT"
    assert "WAITING_ENTRY_CONFIRMATION" in proposal.reasons


def test_trend_ma_equality_has_no_side():
    proposal, _ = drive_trend(ma20=100, ma60=100)
    assert proposal.action == "WAIT"
    assert "TREND_SPREAD_NOT_QUALIFIED" in proposal.reasons


def test_trend_short_mirror_freezes_opposite_geometry():
    proposal, _ = drive_trend(ma20=100, ma60=110, regime_base="DOWN_TREND")
    assert proposal.action == "OPEN"
    reference = Decimal("98")
    assert proposal.target_price == reference - Decimal(2) * Decimal(4)
    assert proposal.stop_price == reference + Decimal(4)
    assert proposal.next_state.direction == -1


def test_trend_holding_does_not_reopen():
    from ak_trading.v2.strategies.trend import TrendFollowing
    spec = build_strategy_spec("TREND", "MINUTE")
    snaps = features(Decimal("123"), Decimal("4"))
    forecasts = trend_forecasts(snaps)
    holding = holding_state(spec, forecasts[0].window_end_nano)
    closes = [100 + index for index in range(24)]
    context = make_context(
        spec, bars=minute_bars(closes), forecasts=forecasts,
        regime_obj=regime("UP_TREND"), features_snap=snaps,
        account_snap=holding_account(spec, holding),
        position=PositionSnapshot("SHFE.RB2605", spec.strategy_id,
                                  holding.episode_id or "", 7, Decimal("121"),
                                  False))
    proposal = TrendFollowing(spec).evaluate(context, holding)
    assert proposal.action in ("HOLD", "EXIT")
    assert proposal.action != "OPEN"


def test_trend_ma_reversal_exits_immediately():
    from ak_trading.v2.strategies.trend import TrendFollowing
    spec = build_strategy_spec("TREND", "MINUTE")
    snaps = features(Decimal("123"), Decimal("4"),
                     {"close": Decimal("123"), "ma20": Decimal("100"),
                      "ma60": Decimal("110"), "atr14": Decimal("4"),
                      "er20": Decimal("0.5")})
    forecasts = trend_forecasts(snaps)
    holding = holding_state(spec, forecasts[0].window_end_nano)
    closes = [100 + index for index in range(24)]
    context = make_context(
        spec, bars=minute_bars(closes), forecasts=forecasts,
        regime_obj=regime("UP_TREND"), features_snap=snaps,
        account_snap=holding_account(spec, holding),
        position=PositionSnapshot("SHFE.RB2605", spec.strategy_id,
                                  holding.episode_id or "", 7, Decimal("121"),
                                  False))
    proposal = TrendFollowing(spec).evaluate(context, holding)
    assert proposal.action == "EXIT"
    assert "TREND_CONDITION_REVERSED" in proposal.reasons


# --- breakout (§6.2) -------------------------------------------------------------


def breakout_snaps():
    return (features(Decimal("100.3"), Decimal("1")),
            features(Decimal("100.4"), Decimal("1")),
            features(Decimal("100.5"), Decimal("1")))


def breakout_forecasts(snap, setup_range):
    policy = m1_policy.confidence_policy("BREAKOUT").policy_id
    return (forecast(
        HORIZON, base_regime="COMPRESSION", persistence={"UP_BREAKOUT": 0.8},
        frozen=setup_range,
        evidence_items=(
            evidence("PERSIST:UP_BREAKOUT", "[0.80,0.85)", policy, "COMPRESSION",
                     HORIZON, snap, setup_range),
            evidence("PATH", "ALL", policy, "COMPRESSION", HORIZON, snap, setup_range))),)


def setup_range_up():
    from ak_trading.v2.artifacts import stable_id
    return FrozenRange(
        stable_id("range", "breakout_m1_v2|b20|90|100"), BASE + 21 * MINUTE,
        Decimal("90"), Decimal("95"), Decimal("100"), Decimal("89"), Decimal("101"),
        "snap-1")


def real_range_snaps(closes=("100.3", "100.4", "100.5")):
    # SYNTHETIC prefixes only; the M3A producer owns the range identity.
    return tuple(features(
        Decimal(str(close)), Decimal("1"),
        {"prior_low20": Decimal("90"), "prior_high20": Decimal("100")},
        decision=BASE + (21 + index) * MINUTE)
        for index, close in enumerate(closes))


def real_range_forecasts(snap, original, *, side=1, base="COMPRESSION"):
    policy = m1_policy.confidence_policy("BREAKOUT").policy_id
    event = "UP_BREAKOUT" if side == 1 else "DOWN_BREAKOUT"
    return (replace(
        forecast(
            HORIZON, base_regime=base, persistence={event: 0.8},
            frozen=original, published=snap.decision_nano,
            expires=snap.decision_nano + 15 * MINUTE,
            evidence_items=(
                evidence(f"PERSIST:{event}", "[0.80,0.85)", policy, base,
                         HORIZON, snap, original),
                evidence("PATH", "ALL", policy, base, HORIZON, snap, original))),
        snapshot_id=snap.snapshot_id),)


def real_range_context(spec, bars, snap, original, *, side=1, base="COMPRESSION"):
    # The root decision consumes one complete M3A original from the same
    # source as its regime/request/forecast; later confirmation bars carry
    # that same original instead of re-drawing the boundary.
    assessment = replace(
        regime(base, ("BREAKOUT",), original),
        snapshot_id=snap.snapshot_id, decision_nano=snap.decision_nano)
    return make_context(
        spec, bars=bars, features_snap=snap, regime_obj=assessment,
        forecasts=real_range_forecasts(snap, original, side=side, base=base),
        now=snap.decision_nano)


def test_breakout_confirmation_uses_same_frozen_boundary():
    from ak_trading.v2.strategies.breakout import ConfirmedBreakout
    snaps = real_range_snaps()
    original = freeze_range(snaps[0])
    assert original is not None
    closes = [95] * 20 + [100.3, 100.4, 100.5]
    # Later bars carry much higher highs; a redrawn 20-bar window would lift
    # the boundary past the closes and kill the confirmation. The frozen
    # line must stay at 100.1.
    highs = [100] * 20 + [100.4, 103, 103]
    lows = [90] * 20 + [95, 95, 95]
    bars = (minute_bars(closes[:21], highs[:21], lows[:21]),
            minute_bars(closes[:22], highs[:22], lows[:22]),
            minute_bars(closes[:23], highs[:23], lows[:23]))
    spec = build_strategy_spec("BREAKOUT", "MINUTE")
    state = None
    for visible, snap in zip(bars, snaps):
        proposal = ConfirmedBreakout(spec).evaluate(
            real_range_context(spec, visible, snap, original), state)
        state = commit_observation(state, proposal)
    assert proposal.action == "OPEN"
    assert proposal.trigger_price == Decimal("100.1")
    assert state is not None and state.setup is not None
    assert state.setup.trigger_level == Decimal("100.1")


def test_breakout_middle_close_back_inside_resets_setup():
    strategy = build_strategy_spec("BREAKOUT", "MINUTE")
    from ak_trading.v2.strategies.breakout import ConfirmedBreakout
    range_up = setup_range_up()
    snaps = breakout_snaps()
    closes = [95] * 20 + [100.3, 99.5]  # second bar falls back inside
    highs = [100] * 20 + [100.4, 99.7]
    lows = [90] * 20 + [95, 94.8]
    bars = (minute_bars(closes[:21], highs[:21], lows[:21]),
            minute_bars(closes[:22], highs[:22], lows[:22]))
    state = None
    proposal = None
    for bars_i, snap_i in zip(bars, snaps[:2]):
        proposal = ConfirmedBreakout(strategy).evaluate(
            make_context(strategy, bars=bars_i,
                         forecasts=breakout_forecasts(snap_i, range_up),
                         regime_obj=regime("COMPRESSION", ("BREAKOUT",)),
                         features_snap=snap_i), state)
        state = commit_observation(state, proposal)
    assert proposal.action == "WAIT"
    assert state is not None and state.setup is None


def test_breakout_short_mirror():
    from ak_trading.v2.strategies.breakout import ConfirmedBreakout
    snaps = real_range_snaps(("89.7", "89.6", "89.5"))
    original = freeze_range(snaps[0])
    assert original is not None
    closes = [95] * 20 + [89.7, 89.6, 89.5]
    highs = [100] * 20 + [95, 95, 95]
    lows = [90] * 20 + [89.6, 89.5, 89.4]
    bars = (minute_bars(closes[:21], highs[:21], lows[:21]),
            minute_bars(closes[:22], highs[:22], lows[:22]),
            minute_bars(closes[:23], highs[:23], lows[:23]))
    spec = build_strategy_spec("BREAKOUT", "MINUTE")
    state = None
    for visible, snap in zip(bars, snaps):
        proposal = ConfirmedBreakout(spec).evaluate(
            real_range_context(spec, visible, snap, original, side=-1,
                               base="DOWN_TREND"), state)
        state = commit_observation(state, proposal)
    assert proposal.action == "OPEN"
    assert proposal.trigger_price == Decimal("89.9")
    assert proposal.next_state.direction == -1


def test_breakout_false_breakout_exit():
    from ak_trading.v2.strategies.breakout import ConfirmedBreakout
    spec = build_strategy_spec("BREAKOUT", "MINUTE")
    snap = features(Decimal("100.5"), Decimal("1"))
    forecasts = breakout_forecasts(snap, setup_range_up())
    holding = holding_state(spec, forecasts[0].window_end_nano)
    closes = [95] * 20 + [100.3, 100.4, 100.5, 99.8]
    context = make_context(
        spec, bars=minute_bars(closes), forecasts=forecasts,
        regime_obj=regime("COMPRESSION", ("BREAKOUT",)), features_snap=snap,
        account_snap=holding_account(spec, holding),
        position=PositionSnapshot("SHFE.RB2605", spec.strategy_id,
                                  holding.episode_id or "", 7, Decimal("100.5"),
                                  False))
    # Supply the setup so the boundary is known while managing the position.
    holding = _with_setup(holding, Decimal("100.1"))
    proposal = ConfirmedBreakout(spec).evaluate(context, holding)
    assert proposal.action == "EXIT"
    assert "FALSE_BREAKOUT" in proposal.reasons


def _with_setup(state, trigger_level):
    from ak_trading.v2.contracts import StrategySetup
    from ak_trading.v2.strategies.registry import setup_expiry_nano

    class _FakeContext:
        pass

    setup = StrategySetup(
        setup_id="setup-brk", strategy_id=state.strategy_id, contract_id=state.contract_id,
        side=1, started_at_nano=BASE, expires_at_nano=BASE + 10 * MINUTE,
        observed_bars=4, frozen_range=None, frozen_ma=None, frozen_atr=Decimal("1"),
        trigger_level=trigger_level, armed=True, consecutive_confirmations=3,
        previous_close=Decimal("100.5"), last_bar_id="b23")
    from ak_trading.v2.contracts import StrategyState
    return StrategyState(
        strategy_id=state.strategy_id, episode_id=state.episode_id,
        contract_id=state.contract_id, phase=state.phase, setup=setup,
        last_regime=state.last_regime, direction=state.direction,
        filled_layers=state.filled_layers, layer_filled_lots=state.layer_filled_lots,
        pending_layer_index=state.pending_layer_index,
        active_exit_nano=state.active_exit_nano, pending_exit_nano=state.pending_exit_nano,
        pending_intent_ids=state.pending_intent_ids, budget=state.budget,
        confirmation_count=state.confirmation_count,
        cooldown_until_nano=state.cooldown_until_nano, last_event_id=state.last_event_id)


# --- pullback (§6.3) --------------------------------------------------------------


def pullback_forecasts(snap, key="UP_TREND"):
    policy = m1_policy.confidence_policy("PULLBACK").policy_id
    return (forecast(
        HORIZON, base_regime=key, persistence={key: 0.75}, published=BASE + 9 * MINUTE,
        evidence_items=(
            evidence(f"PERSIST:{key}", "[0.75,0.80)", policy, key, HORIZON, snap, None),
            evidence("PATH", "ALL", policy, key, HORIZON, snap, None))),)


def pullback_snaps(sequence):
    factor_sets = {
        "long": {"close": Decimal("105"), "ma20": Decimal("105"), "ma60": Decimal("100"),
                 "atr14": Decimal("2")},
    }
    snaps = []
    for reference in sequence:
        snaps.append(features(Decimal(str(reference)), Decimal("2"),
                              {"close": Decimal(str(reference)), "ma20": Decimal("105"),
                               "ma60": Decimal("100"), "atr14": Decimal("2")}))
    return tuple(snaps)


def test_pullback_zone_entry_then_reclaim_confirms():
    closes = [105] * 10 + [105, 105.5, 105.6]
    bars = (minute_bars(closes[:11]), minute_bars(closes[:12]), minute_bars(closes[:13]))
    snaps = pullback_snaps([105, 105.5, 105.6])
    proposal, _ = drive_open("PULLBACK", regime_base="UP_TREND", flags=("PULLBACK",),
                             bars_sequence=bars, features_sequence=snaps,
                             forecasts=pullback_forecasts(snaps[-1]))
    assert proposal.action == "OPEN"
    assert proposal.trigger_price == Decimal("105")
    assert proposal.next_state.direction == 1


def test_pullback_reclaim_needs_higher_close_than_previous():
    # Close above the frozen MA but not above the previous close.
    closes = [105] * 10 + [105.5, 105.3, 105.2]
    bars = (minute_bars(closes[:11]), minute_bars(closes[:12]), minute_bars(closes[:13]))
    snaps = pullback_snaps([105.5, 105.3, 105.2])
    proposal, _ = drive_open("PULLBACK", regime_base="UP_TREND", flags=("PULLBACK",),
                             bars_sequence=bars, features_sequence=snaps,
                             forecasts=pullback_forecasts(snaps[-1]))
    assert proposal.action == "WAIT"
    assert "WAITING_PULLBACK_RECLAIM" in proposal.reasons


def test_pullback_setup_expires_after_five_bars():
    strategy = build_strategy_spec("PULLBACK", "MINUTE")
    from ak_trading.v2.strategies.pullback import TrendPullback
    # Bar 0 enters the zone; bars 1..4 stay inside without reclaiming; bar 5
    # is the sixth observation -> the setup must expire and restart.
    closes = [105] + [104.9, 104.8, 104.7, 104.6, 104.5]
    snaps = pullback_snaps(closes)
    state = None
    proposal = None
    for index in range(1, 7):
        proposal = TrendPullback(strategy).evaluate(
            make_context(strategy, bars=minute_bars(closes[:index]),
                         forecasts=pullback_forecasts(snaps[index - 1]),
                         regime_obj=regime("UP_TREND", ("PULLBACK",)),
                         features_snap=snaps[index - 1]), state)
        state = commit_observation(state, proposal)
    assert proposal.action == "WAIT"
    setup = state.setup if state is not None else None
    assert setup is not None and setup.observed_bars == 1
    assert setup.consecutive_confirmations == 0


def test_pullback_short_mirror_reverse_dip():
    closes = [95] * 10 + [95, 94.5, 94.4]
    bars = (minute_bars(closes[:11]), minute_bars(closes[:12]), minute_bars(closes[:13]))
    snaps = []
    for reference in (95, 94.5, 94.4):
        snaps.append(features(Decimal(str(reference)), Decimal("2"),
                              {"close": Decimal(str(reference)), "ma20": Decimal("95"),
                               "ma60": Decimal("100"), "atr14": Decimal("2")}))
    proposal, _ = drive_open("PULLBACK", regime_base="DOWN_TREND", flags=("PULLBACK",),
                             bars_sequence=bars, features_sequence=tuple(snaps),
                             forecasts=pullback_forecasts(snaps[-1], "DOWN_TREND"))
    assert proposal.action == "OPEN"
    assert proposal.next_state.direction == -1
    assert proposal.trigger_price == Decimal("95")


def test_pullback_background_failure_exits():
    from ak_trading.v2.strategies.pullback import TrendPullback
    spec = build_strategy_spec("PULLBACK", "MINUTE")
    snap = features(Decimal("105.6"), Decimal("2"))
    forecasts = pullback_forecasts(snap)
    holding = holding_state(spec, forecasts[0].window_end_nano)
    closes = [105] * 10 + [105, 105.5, 105.6]
    context = make_context(
        spec, bars=minute_bars(closes), forecasts=forecasts,
        regime_obj=regime("DOWN_TREND", ("PULLBACK",)), features_snap=snap,
        account_snap=holding_account(spec, holding),
        position=PositionSnapshot("SHFE.RB2605", spec.strategy_id,
                                  holding.episode_id or "", 7, Decimal("105.5"),
                                  False))
    proposal = TrendPullback(spec).evaluate(context, holding)
    assert proposal.action == "EXIT"
    assert "BACKGROUND_TREND_LOST" in proposal.reasons
