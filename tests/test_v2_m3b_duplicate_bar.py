"""M3B permanent regression (phase0 A2, 2026-10-26): duplicate same-bar
delivery, admission-gate reset and expired-observation-period credit over
the shared entry-confirmation counter (04-M3B-策略库.md §6/§7/§10-11).

Every population below is SYNTHETIC and per-family legal at its own
frequency; it is engineering input, never historical PIT data. The tests
drive the real registry strategies through ``evaluate`` and persist
observation state with ``commit_observation``. No counter is mocked, no
threshold is altered and no OPEN is fabricated: an OPEN assertion can only
pass when the family's own structure, admission, cost and budget gates
accept the third distinct qualifying bar. A WAIT-family control proves the
same delivered trajectories never count, so an all-WAIT run can never
satisfy an entry assertion.

Covered populations (04-M3B §10-11 plus the phase0 F1 ruling):

* clean ``0,1,2`` — counts ``1,2,3`` and OPEN only on the third distinct
  qualifying bar (for BREAKOUT the freshly created setup starts at 1);
* ``0,1,1,2`` — recross/confirmation-bar re-delivery holds ``1,2,2,3``;
* ``0,0,0,1,2`` — arming/first-observation re-delivery holds ``1,1,1,2,3``;
* saved observation state + a rebuilt strategy object re-delivering the
  same decision matches the uninterrupted run (restart equivalence);
* admission-gate failure resets to 0 first, the same bar recovering does
  not re-count, only a later distinct qualifying bar advances again;
* ``now == expires_at_nano`` is not premature expiry (counter holds on the
  same bar, the next distinct bar still advances);
* a canceled (expired) observation period never lends credit: a rebuilt
  setup counts its first qualifying observation as 1 (never old + 1), and
  when the family cannot rebuild a structure the branch waits at 0 until a
  legal arming appears.

Shared synthetic builders come from this node's ``test_v2_m3b_matrix``
module (``tests/conftest.py`` must not be edited).
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from ak_trading.v2 import clock as m1_clock
from ak_trading.v2 import policy as m1_policy
from ak_trading.v2.contracts import (
    CalibrationEvidence,
    FactorValue,
    FeatureSnapshot,
    Horizon,
    MarketBar,
    MarketForecast,
    SessionWindow,
    StrategyContext,
    StrategySpec,
    TimeProbability,
)
from ak_trading.v2.probability.regimes import freeze_range
from ak_trading.v2.schemas import calibration_geometry_key
from ak_trading.v2.strategies.registry import strategy_registry_v2
from ak_trading.v2.strategies.state import commit_observation

from test_v2_m3b_matrix import (
    BASE,
    CONTRACT,
    MINUTE,
    NANO,
    PRODUCT,
    frozen_range,
    make_context,
    quality,
    regime,
    sessions,
)

DAY = 86400 * NANO
ENTRY_FAMILIES = ("TREND", "BREAKOUT", "PULLBACK", "REVERSION", "GRID", "MARTINGALE")
RANGE_FAMILIES = ("REVERSION", "GRID", "MARTINGALE")
FAMILIES = ENTRY_FAMILIES + ("WAIT",)
MODES = ("MINUTE", "DAILY")
# Families whose own structure rules can leave no valid setup after expiry
# (pullback always constructs a fresh, possibly unarmed setup object).
NO_STRUCTURE_FAMILIES = ("TREND", "BREAKOUT") + RANGE_FAMILIES

GOOD_PERSISTENCE = {"TREND": 0.75, "PULLBACK": 0.75, "BREAKOUT": 0.8,
                    "REVERSION": 0.9, "GRID": 0.9, "MARTINGALE": 0.9}
BAD_PERSISTENCE = 0.5


def mode_id(mode: str) -> str:
    return "m1" if mode == "MINUTE" else "d1"


def mode_step(mode: str) -> int:
    return MINUTE if mode == "MINUTE" else DAY


def mode_duration(mode: str) -> int:
    return 60 if mode == "MINUTE" else 86400


def mode_calendar(mode: str):
    if mode == "MINUTE":
        return sessions()
    return tuple(
        SessionWindow(f"day{index}", PRODUCT, date(2026, 1, 12) + timedelta(days=index),
                      BASE + index * DAY, BASE + (index + 1) * DAY, "DAY", "cal-1",
                      "SCENARIO")
        for index in range(60))


def synthetic_bar(mode: str, slot: int, close, high=None, low=None) -> MarketBar:
    """One completed SYNTHETIC bar of the mode's own frequency at ``slot``."""
    step = mode_step(mode)
    close = Decimal(str(close))
    high = Decimal(str(high)) if high is not None else close + Decimal("0.2")
    low = Decimal(str(low)) if low is not None else close - Decimal("0.2")
    start = BASE + slot * step
    trading_day = date(2026, 1, 12) + (timedelta(days=slot) if mode == "DAILY"
                                       else timedelta(0))
    return MarketBar(
        bar_id=f"{'b' if mode == 'MINUTE' else 'd'}{slot}", contract_id=CONTRACT,
        raw_symbol="rb2605", product=PRODUCT, duration_seconds=mode_duration(mode),
        datetime_nano=start, start_nano=start, end_nano=start + step,
        trading_day=trading_day,
        session_id="day0-am" if mode == "MINUTE" else f"day{slot}",
        available_at_nano=start + step, imported_at_nano=None,
        open=close, high=high, low=low, close=close, volume=Decimal("1"),
        open_oi=Decimal("10"), close_oi=Decimal("10"), complete=True,
        expected_minutes=1 if mode == "MINUTE" else 1440,
        actual_minutes=1 if mode == "MINUTE" else 1440,
        source="test", quality=quality())


def mode_features(mode: str, reference: Decimal, atr: Decimal, factors=None,
                  decision: int = 0) -> FeatureSnapshot:
    decision = decision or (BASE + 24 * MINUTE if mode == "MINUTE" else BASE + 24 * DAY)
    suffix = "d60" if mode == "MINUTE" else "d86400"
    values = [FactorValue(f"BASE:{name}:v2.1.0:{suffix}", value, decision - 1, quality())
              for name, value in (factors or {}).items()]
    values.append(FactorValue("META:price_tick:v2.1.0", Decimal("0.1"),
                              decision - 1, quality()))
    return FeatureSnapshot(
        "snap-1", CONTRACT, "rb2605", PRODUCT, date(2026, 1, 12), "day0-am",
        "DAY" if mode == "MINUTE" else "DAILY", decision, decision - 1,
        mode_duration(mode), ("b0",), reference, atr, tuple(values), None, quality())


def world_evidence(mode: str, event: str, bucket: str, policy_id: str,
                   base_regime: str, horizon: Horizon, snap: FeatureSnapshot,
                   frozen) -> CalibrationEvidence:
    if event.startswith("PERSIST:"):
        base = event[len("PERSIST:"):]
    elif event.startswith("STOP_TOUCH:"):
        base = "STOP_TOUCH"
    else:
        base = event
    range_free = base in ("UP_TREND", "DOWN_TREND", "COMPRESSION", "DIRECTION",
                          "PATH", "UP_TOUCH", "DOWN_TOUCH")
    geometry = calibration_geometry_key(snap, None if range_free else frozen, base)
    barrier = "ATR_1" if base == "PATH" else "COMMON"
    return CalibrationEvidence(
        evidence_id=f"ev-{event}", model_id="model-1", policy_id=policy_id,
        product=PRODUCT, session_kind="DAY" if mode == "MINUTE" else "DAILY",
        duration_seconds=mode_duration(mode), base_regime=base_regime,
        barrier_key=barrier, geometry_key=geometry or "geometry-missing",
        event=event, horizon=horizon, label_version="label-v2.1.0", bucket=bucket,
        sample_count=600, effective_blocks=80, calibration_error=0.01, brier=0.01,
        training_end_nano=BASE - 100 * DAY, calibration_end_nano=BASE - 50 * DAY,
        valid_until_nano=BASE + 400 * DAY, status="VALID")


def world_forecast(mode: str, *, base_regime: str, persistence_key: str,
                   persistence: float, frozen, published: int,
                   evidence_items) -> MarketForecast:
    horizon = Horizon("TRADING_MINUTE", 30) if mode == "MINUTE" else Horizon("TRADING_DAY", 5)
    window_start, window_end = m1_clock.horizon_window(
        published, PRODUCT, horizon, mode_calendar(mode))
    return MarketForecast(
        forecast_id=f"fc-{horizon.unit}{horizon.value}", request_id="req-1",
        snapshot_id="snap-1", contract_id=CONTRACT, product=PRODUCT,
        trading_day=date(2026, 1, 12), session_id="day0-am",
        session_kind="DAY" if mode == "MINUTE" else "DAILY",
        duration_seconds=mode_duration(mode), assessment_id="assess-1",
        base_regime=base_regime, regime_reference_id="ref-1",
        decision_nano=published - 1, published_at_nano=published,
        expires_at_nano=window_end, horizon=horizon,
        window_start_nano=window_start, window_end_nano=window_end,
        barrier_key="ATR_1", reference_price=Decimal("100"), atr_points=Decimal("1"),
        upper_distance=Decimal("1"), lower_distance=Decimal("1"), frozen_range=frozen,
        label_version="label-v2.1.0", model_id="model-1",
        direction_probabilities={"UP": 0.4, "DOWN": 0.4, "NEUTRAL": 0.2},
        return_quantiles={"Q05": 0.98, "Q25": 0.99, "Q50": 1.0, "Q75": 1.01, "Q95": 1.02},
        point_quantiles={"Q05": Decimal("98"), "Q25": Decimal("99"), "Q50": Decimal("100"),
                         "Q75": Decimal("101"), "Q95": Decimal("102")},
        atr_quantiles={"Q05": 98.0, "Q25": 99.0, "Q50": 100.0, "Q75": 101.0, "Q95": 102.0},
        path_probabilities={"UP_FIRST": 0.45, "DOWN_FIRST": 0.44,
                            "NEITHER": 0.10, "AMBIGUOUS": 0.01},
        time_cdf=(TimeProbability("RETURN_FIRST", horizon.value, 0.6, 0.01),
                  TimeProbability("BREAK_FIRST", horizon.value, 0.3, 0.01)),
        persistence_probabilities={persistence_key: persistence},
        stop_touch_probabilities={} if frozen is None else {"LONG": 0.05, "SHORT": 0.05},
        return_before_break_probability=0.7, evidence=tuple(evidence_items),
        out_of_distribution=False, quality=quality())


def bucket_for(value: float) -> str:
    if value >= 0.90:
        return "[0.90,0.95)"
    if value >= 0.80:
        return "[0.80,0.85)"
    if value >= 0.75:
        return "[0.75,0.80)"
    if value >= 0.70:
        return "[0.70,0.75)"
    return "[0.45,0.55)"


def family_forecasts(family: str, mode: str, snap: FeatureSnapshot, *, gate_ok: bool,
                     frozen, published: int):
    if family == "WAIT":
        return ()
    policy_id = m1_policy.confidence_policy(family).policy_id
    horizon = (Horizon("TRADING_MINUTE", 30) if mode == "MINUTE"
               else Horizon("TRADING_DAY", 5))
    persistence = GOOD_PERSISTENCE[family] if gate_ok else BAD_PERSISTENCE
    base_regime, event = {
        "TREND": ("UP_TREND", "PERSIST:UP_TREND"),
        "PULLBACK": ("UP_TREND", "PERSIST:UP_TREND"),
        "BREAKOUT": ("COMPRESSION", "PERSIST:UP_BREAKOUT"),
    }.get(family, ("ORDERED_RANGE", "PERSIST:ORDERED_RANGE"))
    items = [
        world_evidence(mode, event, bucket_for(persistence), policy_id, base_regime,
                       horizon, snap, frozen),
        world_evidence(mode, "PATH", "ALL", policy_id, base_regime, horizon, snap, frozen),
    ]
    if family in RANGE_FAMILIES:
        items.append(world_evidence(mode, "RETURN_BEFORE_BREAK", "[0.70,0.75)", policy_id,
                                    base_regime, horizon, snap, frozen))
    if family == "MARTINGALE":
        items.append(world_evidence(mode, "STOP_TOUCH:LONG", "[0.05,0.10]", policy_id,
                                    base_regime, horizon, snap, frozen))
        items.append(world_evidence(mode, "STOP_TOUCH:SHORT", "[0.05,0.10]", policy_id,
                                    base_regime, horizon, snap, frozen))
    return (world_forecast(mode, base_regime=base_regime,
                           persistence_key=event[len("PERSIST:"):],
                           persistence=persistence, frozen=frozen, published=published,
                           evidence_items=items),)


# --- per-family SYNTHETIC bar plans ---------------------------------------------
#
# A plan maps a step name to the delivered bar list; each entry is
# (slot, close) or (slot, close, high, low). Late steps sit well past every
# family's setup expiry so the old observation period is genuinely canceled.


def family_plan(family: str):
    if family in RANGE_FAMILIES:
        drift = [(index, 100 - 0.35 * index) for index in range(20)]  # arms long
        return {
            "s0": drift,
            "s1": drift + [(20, 96)],
            "s2": drift + [(20, 96), (21, "96.2")],
            "s3": drift + [(20, 96), (21, "96.2"), (22, "96.4")],
            "mid": drift + [(30, 97)],  # post-expiry middle close: no structure
            "arm": drift + [(31, "93.4")],
            "ra": drift + [(31, "93.4"), (32, 96)],
            "rb": drift + [(31, "93.4"), (32, 96), (33, "96.2")],
        }
    if family == "TREND":
        rising = [(index, 100 + index) for index in range(25)]
        return {
            "s0": rising[:22], "s1": rising[:23], "s2": rising[:24], "s3": rising[:25],
            # Post-expiry structure break (close far below its own MA20 window).
            "mid": rising[:23] + [(30, 100)],
            "arm": rising[:25] + [(31, 124)],
            "ra": rising[:25] + [(31, 124), (32, 125)],
            "rb": rising[:25] + [(31, 124), (32, 125), (33, 126)],
        }
    if family == "PULLBACK":
        seq = [(index, 105) for index in range(10)] + [
            (10, 105), (11, "105.5"), (12, "105.6"), (13, "105.7")]
        return {
            "s0": seq[:11], "s1": seq[:12], "s2": seq[:13], "s3": seq[:14],
            "mid": None,
            "arm": seq[:14] + [(20, 105)],
            "ra": seq[:14] + [(20, 105), (21, "105.5")],
            "rb": seq[:14] + [(20, 105), (21, "105.5"), (22, "105.6")],
        }
    # BREAKOUT: flat 90/100 prior boundary, three closes beyond the buffer,
    # then a post-expiry inside close and a fresh breakout beyond the live
    # re-derived boundary.
    flat = [(index, 95, 100, 90) for index in range(20)]
    breakout = [(20, "100.3", "100.4", 95), (21, "100.4", "100.5", 95),
                (22, "100.5", "100.6", 95), (23, "100.6", "100.7", 95)]
    late = [(30, 99, "99.2", "98.8"), (31, "101.0", "101.2", "100.8"),
            (32, "101.1", "101.3", "101.0"), (33, "101.2", "101.4", "101.1")]
    return {
        "s0": flat + breakout[:1], "s1": flat + breakout[:2],
        "s2": flat + breakout[:3], "s3": flat + breakout[:4],
        "mid": flat + breakout[:3] + late[:1],
        "arm": flat + breakout[:3] + late[:2],
        "ra": flat + breakout[:3] + late[:3],
        "rb": flat + breakout[:3] + late[:4],
    }


def _boundary_factors(bars) -> dict[str, Decimal]:
    window = bars[-(20 + 1):-1]
    lows = [bar.low for bar in window if bar.low is not None]
    highs = [bar.high for bar in window if bar.high is not None]
    assert len(lows) >= 20 and len(highs) >= 20
    return {"prior_low20": min(lows), "prior_high20": max(highs)}


def build_world(family: str, mode: str):
    """Real-registry strategy plus per-step context factories.

    Each factory accepts ``gate_ok`` (admission-quality forecasts vs a
    below-minimum one) and ``now_override`` and builds a fully legal
    SYNTHETIC decision context of the mode's own frequency. WAIT reuses the
    range-family world; it never reads any of the entry machinery.
    """
    strategy = strategy_registry_v2(mode)[f"{family.lower()}_{mode_id(mode)}_v2"]
    spec = strategy.spec
    calendar = mode_calendar(mode)
    plan = family_plan("REVERSION" if family == "WAIT" else family)
    frozen_shared = None if family in ("TREND", "PULLBACK", "WAIT") else frozen_range()
    breakout_creation = {}  # step name -> per-decision frozen range for creations

    def step(key):
        entries = plan[key]
        bars = tuple(synthetic_bar(mode, *entry) for entry in entries)
        last = Decimal(str(entries[-1][1]))
        decision = bars[-1].end_nano

        def make(gate_ok: bool = True, now_override=None) -> StrategyContext:
            if family == "BREAKOUT":
                return breakout_step(spec, mode, bars, key, gate_ok, now_override,
                                     breakout_creation, calendar)
            factors = family_factors(family, bars, last)
            snap = mode_features(mode, last, family_atr(family), factors,
                                 decision=decision)
            forecasts = family_forecasts(family, mode, snap, gate_ok=gate_ok,
                                         frozen=frozen_shared, published=decision)
            base, flags = family_regime(family)
            return make_context(
                spec, bars=bars, forecasts=forecasts,
                regime_obj=regime(base, flags, frozen_shared), features_snap=snap,
                sessions_override=calendar, now_override=now_override)

        return make

    steps = {key: step(key) for key, entries in plan.items() if entries is not None}
    return strategy, steps


def family_atr(family: str) -> Decimal:
    return Decimal("4") if family == "TREND" else (Decimal("2") if family == "PULLBACK"
                                                   else Decimal("1"))


def family_regime(family: str):
    if family == "TREND":
        return "UP_TREND", ()
    if family == "PULLBACK":
        return "UP_TREND", ("PULLBACK",)
    return "ORDERED_RANGE", ()


def family_factors(family: str, bars, last: Decimal):
    if family in RANGE_FAMILIES or family == "WAIT":
        return {"close": last, "atr14": Decimal("1")}
    if family == "TREND":
        return {"close": last, "ma20": Decimal("110"), "ma60": Decimal("100"),
                "atr14": Decimal("4"), "er20": Decimal("0.5")}
    if family == "PULLBACK":
        return {"close": last, "ma20": Decimal("105"), "ma60": Decimal("100"),
                "atr14": Decimal("2")}
    return _boundary_factors(bars)


def breakout_step(spec: StrategySpec, mode: str, bars, key: str, gate_ok: bool,
                  now_override, creation_cache: dict, calendar):
    """BREAKOUT step: the SYNTHETIC V03 seam keeps one frozen original.

    Continuation steps carry the creating decision's frozen range (``s1``–
    ``s3`` continue ``s0``; ``ra``/``rb`` continue ``arm``); creation steps
    (``s0``, ``mid``, ``arm``) freeze this decision's own live prior-20
    boundary, exactly as the M3A producer would.
    """
    decision = bars[-1].end_nano
    last = bars[-1].close
    source_of = {"s1": "s0", "s2": "s0", "s3": "s0", "ra": "arm", "rb": "arm"}
    if key in source_of:
        source = creation_cache[source_of[key]]
        factors = {"prior_low20": source.lower, "prior_high20": source.upper}
    else:
        factors = _boundary_factors(bars)
        source = creation_cache.get(key)
        if source is None:
            source = freeze_range(mode_features(mode, last, Decimal("1"), factors,
                                                decision=decision))
            assert source is not None and source.frozen_at_nano == decision
            creation_cache[key] = source
    snap = mode_features(mode, last, Decimal("1"), factors, decision=decision)
    forecasts = family_forecasts("BREAKOUT", mode, snap, gate_ok=gate_ok,
                                 frozen=source, published=decision)
    return make_context(
        spec, bars=bars, forecasts=forecasts,
        regime_obj=regime("COMPRESSION", ("BREAKOUT",), source), features_snap=snap,
        sessions_override=calendar, now_override=now_override)


def run(world, order, *, rebuild_at=(), per_step=None):
    """Deliver ``order`` (step names); returns [(action, confirmation_count)].

    ``rebuild_at`` reconstructs the strategy object from its spec before the
    given positions (saved state + rebuilt strategy re-delivery); never an
    OPEN early-exit so WAIT-family trajectories see every delivery.
    """
    strategy, steps = world
    state = None
    trace = []
    for position, key in enumerate(order):
        if position in rebuild_at:
            strategy = type(strategy)(strategy.spec)
        options = (per_step or {}).get(position, {})
        proposal = strategy.evaluate(steps[key](**options), state)
        state = commit_observation(state, proposal)
        trace.append((proposal.action, state.confirmation_count))
    return trace


CLEAN = ("WAIT", 1), ("WAIT", 2), ("OPEN", 3)
RECROSS_DUP = ("WAIT", 1), ("WAIT", 2), ("WAIT", 2), ("OPEN", 3)
ARMING_DUP = ("WAIT", 1), ("WAIT", 1), ("WAIT", 1), ("WAIT", 2), ("OPEN", 3)
RESTART_REDELIVERY = ("WAIT", 1), ("WAIT", 1), ("WAIT", 2), ("WAIT", 2), ("OPEN", 3)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("family", ENTRY_FAMILIES)
def test_clean_three_distinct_qualifying_bars_open_only_on_third(family, mode):
    world = build_world(family, mode)
    assert run(world, ("s0", "s1", "s2")) == list(CLEAN)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("family", ENTRY_FAMILIES)
def test_recross_confirmation_bar_redelivered_holds_counter(family, mode):
    world = build_world(family, mode)
    assert run(world, ("s0", "s1", "s1", "s2")) == list(RECROSS_DUP)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("family", ENTRY_FAMILIES)
def test_arming_first_observation_redelivered_holds_counter(family, mode):
    world = build_world(family, mode)
    assert run(world, ("s0", "s0", "s0", "s1", "s2")) == list(ARMING_DUP)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("family", ENTRY_FAMILIES)
def test_rebuilt_strategy_redelivery_matches_continuous_run(family, mode):
    # Saved observation state + a freshly constructed strategy object must
    # reproduce the uninterrupted counter and OPEN timing exactly.
    world = build_world(family, mode)
    assert run(world, ("s0", "s0", "s1", "s1", "s2"),
               rebuild_at={1, 3}) == list(RESTART_REDELIVERY)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("family", ENTRY_FAMILIES)
def test_gate_failure_zeroes_and_same_bar_recovery_does_not_recount(family, mode):
    # Admission failure resets the counter first; the same bar later
    # qualifying again neither re-counts nor opens; only the next distinct
    # qualifying bar advances from zero. BREAKOUT's setup window spans only
    # three bars, so its advancing step is the next legal fresh creation
    # instead of a fourth continuation bar.
    world = build_world(family, mode)
    advance = "arm" if family == "BREAKOUT" else "s3"
    trace = run(world, ("s0", "s1", "s2", "s2", advance),
                per_step={2: {"gate_ok": False}})
    assert trace == [("WAIT", 1), ("WAIT", 2), ("WAIT", 0), ("WAIT", 0), ("WAIT", 1)]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("family", ENTRY_FAMILIES)
def test_expiry_boundary_is_not_premature_expiry(family, mode):
    # now == expires_at_nano: the re-delivered same bar holds the counter at
    # 2 (no OPEN from a re-delivery), and a distinct qualifying bar delivered
    # exactly at the boundary still advances to 3 and may OPEN.
    strategy, steps = build_world(family, mode)
    state = None
    for key in ("s0", "s1"):
        proposal = strategy.evaluate(steps[key](), state)
        state = commit_observation(state, proposal)
    assert state is not None and state.setup is not None
    assert state.confirmation_count == 2
    expiry = state.setup.expires_at_nano
    proposal = strategy.evaluate(steps["s1"](now_override=expiry), state)
    state = commit_observation(state, proposal)
    assert (proposal.action, state.confirmation_count) == ("WAIT", 2)
    proposal = strategy.evaluate(steps["s2"](now_override=expiry), state)
    state = commit_observation(state, proposal)
    assert (proposal.action, state.confirmation_count) == ("OPEN", 3)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("family", ENTRY_FAMILIES)
def test_expired_setup_credit_never_transfers_to_rebuilt_setup(family, mode):
    # Old count 2, the setup expires; a legal fresh arming counts its first
    # qualifying observation as 1 (never 3), re-delivery holds it, and only
    # the third distinct qualifying bar of the new period may OPEN.
    world = build_world(family, mode)
    trace = run(world, ("s0", "s1", "arm", "arm", "ra", "ra", "rb"))
    assert trace == [("WAIT", 1), ("WAIT", 2), ("WAIT", 1), ("WAIT", 1),
                     ("WAIT", 2), ("WAIT", 2), ("OPEN", 3)]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("family", NO_STRUCTURE_FAMILIES)
def test_expired_without_new_structure_waits_at_zero_then_restarts_from_one(
        family, mode):
    # After expiry with no valid new structure the observation branch waits
    # at 0; the later legal arming starts the new period from 1.
    world = build_world(family, mode)
    trace = run(world, ("s0", "s1", "mid", "arm", "ra", "rb"))
    assert trace == [("WAIT", 1), ("WAIT", 2), ("WAIT", 0), ("WAIT", 1),
                     ("WAIT", 2), ("OPEN", 3)]


@pytest.mark.parametrize("mode", MODES)
def test_wait_family_same_deliveries_never_count_or_enter(mode):
    # WAIT sees the same delivered trajectories and must always answer WAIT
    # with zero lots and a zero counter; an all-WAIT population can never
    # satisfy an entry assertion.
    world = build_world("WAIT", mode)
    strategy, steps = world
    for order in (("s0", "s1", "s2"), ("s0", "s1", "s1", "s2"),
                  ("s0", "s0", "s0", "s1", "s2")):
        state = None
        for key in order:
            proposal = strategy.evaluate(steps[key](), state)
            state = commit_observation(state, proposal)
            assert proposal.action == "WAIT"
            assert proposal.target_lots == 0
            assert state.confirmation_count == 0
            assert state.setup is None
