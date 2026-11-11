"""K05 shared SYNTHETIC realistic-shape fixtures (r4-plan01 §5, 2026-11-10).

Everything here is SYNTHETIC engineering data with declared provenance: an
exchange-shaped calendar (prior-evening night session plus three daytime
fragments sharing one trading day, weekends excluded), parametrized bar
availability delay, deterministic per-family bar scenarios, and reusable
model/payoff builders for the natural chain. It is never historical PIT
data.

The legacy matrix ``regime()`` ``cross="ALIGNED"`` default remains a unit
seam for the immutable phase0 probe; nothing here uses it as natural-chain
evidence — regimes and forecasts come from the real producers.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from ak_trading.v2 import policy as m1_policy
from ak_trading.v2.contracts import (
    DataQuality,
    FrozenRange,
    Horizon,
    Instrument,
    MarketBar,
    SessionWindow,
    TradingRule,
)
from ak_trading.v2.probability.regimes import freeze_range
from ak_trading.v2.schemas import calibration_geometry_key
from ak_trading.v2.strategies.registry import strategy_registry_v2

from test_v2_m3b_matrix import BASE, MINUTE, NANO, quality

DAY = 86400 * NANO

SYNTHETIC_PROVENANCE = "SYNTHETIC-K05-REALISTIC-SHAPE"
SYNTHETIC_HASH = "0" * 64
PRODUCT = "SYN.rb"
CONTRACT = "SYN.rb2605"
BASE_DATE = date(2026, 1, 12)  # matrix BASE anchor: Monday 09:00 CST
HOUR = 3600 * NANO
EQUITY = Decimal("1000000")  # the existing declared synthetic equity scale


def _nano(day: date, hour: int, minute: int = 0) -> int:
    """UTC nanos of a CST wall-clock time (CST = UTC+8; BASE is 09:00 CST)."""
    midnight_utc = int(BASE // DAY) * DAY - 9 * HOUR  # 00:00 CST of BASE day
    offset = (day - BASE_DATE).days * DAY
    return midnight_utc + offset + hour * HOUR + minute * MINUTE


def exchange_calendar(days: int = 40):
    """Prior-evening night session plus three daytime fragments per trading
    day (21:00-23:00 previous evening, 09:00-10:15, 10:30-11:30, 13:30-15:00
    CST), weekends excluded; night sessions carry the next trading day."""
    fragments = ((21, 0, 23, 0, True), (9, 0, 10, 15, False),
                 (10, 30, 11, 30, False), (13, 30, 15, 0, False))
    sessions = []
    current, made = BASE_DATE, 0
    while made < days:
        if current.weekday() < 5:
            for index, (sh, sm, eh, em, evening) in enumerate(fragments):
                anchor = current - timedelta(days=1) if evening else current
                sessions.append(SessionWindow(
                    f"{current.isoformat()}-{index}", PRODUCT, current,
                    _nano(anchor, sh, sm), _nano(anchor, eh, em),
                    "NIGHT" if evening else "DAY", SYNTHETIC_PROVENANCE,
                    "SCENARIO"))
            made += 1
        current += timedelta(days=1)
    return tuple(sessions)


def single_session_calendar(days: int = 40):
    sessions = []
    current, made = BASE_DATE, 0
    while made < days:
        if current.weekday() < 5:
            sessions.append(SessionWindow(
                f"{current.isoformat()}-0", PRODUCT, current,
                _nano(current, 9, 0), _nano(current, 15, 0),
                "DAY", SYNTHETIC_PROVENANCE, "SCENARIO"))
            made += 1
        current += timedelta(days=1)
    return tuple(sessions)


def trading_day_bounds(calendar):
    """[(first_start, last_end)] per trading day, ordered."""
    days: dict[date, tuple[int, int]] = {}
    for session in calendar:
        current = days.get(session.trading_day)
        days[session.trading_day] = (
            session.start_nano if current is None else min(current[0], session.start_nano),
            session.end_nano if current is None else max(current[1], session.end_nano))
    return [days[day] for day in sorted(days)]


def _bar(bar_id: str, start: int, end: int, trading_day: date, close,
         delay_ns: int, high=None, low=None, session_id: str = "",
         open_price=None, pad: float = 0.2) -> MarketBar:
    close = Decimal(str(close))
    open_price = Decimal(str(open_price)) if open_price is not None else close
    high = Decimal(str(high)) if high is not None else (
        max(close, open_price) + Decimal(str(pad)))
    low = Decimal(str(low)) if low is not None else (
        min(close, open_price) - Decimal(str(pad)))
    span = end - start
    return MarketBar(
        bar_id=bar_id, contract_id=CONTRACT, raw_symbol="rb2605", product=PRODUCT,
        duration_seconds=span // NANO, datetime_nano=start, start_nano=start,
        end_nano=end,
        trading_day=trading_day, session_id=session_id,
        available_at_nano=end + delay_ns, imported_at_nano=None,
        open=open_price, high=high, low=low, close=close, volume=Decimal("1000"),
        open_oi=Decimal("10"), close_oi=Decimal("10"), complete=True,
        expected_minutes=span // MINUTE, actual_minutes=span // MINUTE,
        source="test", quality=quality())


def _containing_session(calendar, moment: int):
    for session in calendar:
        if session.start_nano <= moment < session.end_nano:
            return session
    return None


def calendar_trading_days(calendar):
    return sorted({s.trading_day for s in calendar})


def daily_bar(day_index: int, close, calendar, delay_ns: int = 0,
              open_price=None, pad: float = 0.2) -> MarketBar:
    """One DAILY bar (duration 86400) ending exactly at the trading day's
    last session close; ``open_price`` defaults to the prior close so the
    real opening-gap factor sees a continuous session open."""
    _, end = trading_day_bounds(calendar)[day_index]
    start = end - DAY
    closing = _containing_session(calendar, end - 1)
    return _bar(f"d{day_index}", start, end, calendar_trading_days(calendar)[day_index],
                close, delay_ns, session_id=closing.session_id if closing else "",
                open_price=open_price, pad=pad)


def day_open(calendar, day_index: int) -> int:
    """Start of the trading day's first daytime fragment (post-midnight)."""
    trading_day = trading_day_bounds(calendar)  # ordered by trading_day date
    day = sorted({s.trading_day for s in calendar})[day_index]
    midnight = _nano(day, 0, 0)
    return min(s.start_nano for s in calendar
               if s.trading_day == day and s.start_nano >= midnight)


def minute_bars(day_index: int, closes, calendar, delay_ns: int = 0,
                first_slot: int = 0, pad: float = 0.2):
    """Minute bars inside the day's first daytime fragment (09:00-10:15)."""
    opening = _containing_session(calendar, day_open(calendar, day_index))
    trading_day = calendar_trading_days(calendar)[day_index]
    bars = []
    for offset, close in enumerate(closes):
        start = day_open(calendar, day_index) + (first_slot + offset) * MINUTE
        bars.append(_bar(f"m{day_index}-{first_slot + offset}", start, start + MINUTE,
                         trading_day, close, delay_ns,
                         session_id=opening.session_id if opening else "", pad=pad))
    return tuple(bars)


def instrument() -> Instrument:
    return Instrument(CONTRACT, "SYN.rb2605", PRODUCT, "METAL", ("STEEL",),
                      BASE - 300 * DAY, BASE + 400 * DAY, BASE + 380 * DAY,
                      BASE + 350 * DAY, quality())


def trading_rule() -> TradingRule:
    return TradingRule("rule-k05", CONTRACT, BASE - 300 * DAY, BASE + 400 * DAY,
                       BASE - 300 * DAY, Decimal("10"), Decimal("0.1"), 1, 1,
                       _fee(Decimal("0.5")), _fee(Decimal("0.5")), _fee(Decimal("0.5")),
                       Decimal("0.1"), Decimal("0.1"), None, None,
                       Decimal("0.1"), Decimal("0.1"), Decimal("0"), "SCENARIO",
                       SYNTHETIC_HASH)


def _fee(per_lot: Decimal):
    from ak_trading.v2.contracts import FeeLeg
    return FeeLeg(per_lot, Decimal("0"), False)


# --- deterministic per-family bar scenarios -------------------------------------
#
# `daily_closes` are the per-trading-day closes; `minute_closes` live inside
# one day's first fragment. Shapes are chosen so the real structural regime
# producer classifies the intended base (trend up / compression breakout /
# ordered range); the daily background direction comes from the synthetic
# model populations (flavour below), never from hand-set assessments.

def _triangle_wave(bars: int, low: float = 99.0, step: float = 0.2,
                   peak: int = 10) -> list:
    """Smooth low-er oscillation: closes drift up ``step`` for ``peak`` bars
    and back down, so the range width stays far above 2*ATR."""
    return [round(low + step * min(k, 2 * peak - k), 4)
            for k in (i % (2 * peak) for i in range(bars))]


_RANGE_OSCILLATION = _triangle_wave(60)
_RANGE_ENTRY = [99.0, 99.45, 99.5]
_TREND_DAILY = [100 + 0.2 * i for i in range(66)]
_TREND_MINUTE = [100 + 0.2 * i for i in range(66)]
_BREAKOUT_BASE = [95.0 if i % 2 == 0 else 95.15 for i in range(60)]                  + [95.5, 95.6, 95.7]
_PULLBACK_DAILY = [100 + i for i in range(60)] + [154.0, 155.0, 155.5]
_PULLBACK_MINUTE = [100 + 0.2 * i for i in range(60)] + [110.8, 111.0, 111.2]

_MINUTE_DAILY_WARMUP = [100 + 0.1 * i for i in range(25)]
MINUTE_DAY = 25  # minute scenarios trade after the daily warmup days

SCENARIOS = {
    ("TREND", "MINUTE"): {"minute": _TREND_MINUTE, "daily": _MINUTE_DAILY_WARMUP,
                          "flavor": "UP"},
    ("TREND", "DAILY"): {"daily": _TREND_DAILY, "flavor": "UP"},
    ("BREAKOUT", "MINUTE"): {"minute": _BREAKOUT_BASE, "daily": _MINUTE_DAILY_WARMUP,
                             "flavor": "NEUTRAL"},
    ("BREAKOUT", "DAILY"): {"daily": _BREAKOUT_BASE, "flavor": "NEUTRAL"},
    ("PULLBACK", "MINUTE"): {"minute": _PULLBACK_MINUTE,
                             "daily": _MINUTE_DAILY_WARMUP, "flavor": "UP",
                             "pad": 1.5},
    ("PULLBACK", "DAILY"): {"daily": _PULLBACK_DAILY, "flavor": "UP", "pad": 10.0},
    ("REVERSION", "MINUTE"): {"minute": _RANGE_OSCILLATION + _RANGE_ENTRY,
                              "daily": _MINUTE_DAILY_WARMUP, "flavor": "NEUTRAL"},
    ("REVERSION", "DAILY"): {"daily": _RANGE_OSCILLATION + _RANGE_ENTRY,
                             "flavor": "NEUTRAL"},
}
for _family in ("GRID", "MARTINGALE", "WAIT"):
    SCENARIOS[(_family, "MINUTE")] = dict(SCENARIOS[("REVERSION", "MINUTE")])
    SCENARIOS[(_family, "DAILY")] = dict(SCENARIOS[("REVERSION", "DAILY")])

FAMILY_EVENTS = {
    "TREND": ("PERSIST:UP_TREND", "PATH", "DIRECTION"),
    "PULLBACK": ("PERSIST:UP_TREND", "PATH", "DIRECTION"),
    "BREAKOUT": ("PERSIST:UP_BREAKOUT", "PATH", "DIRECTION"),
    "REVERSION": ("PERSIST:ORDERED_RANGE", "PATH", "RETURN_BEFORE_BREAK", "DIRECTION"),
    "GRID": ("PERSIST:ORDERED_RANGE", "PATH", "RETURN_BEFORE_BREAK", "DIRECTION"),
    "MARTINGALE": ("PERSIST:ORDERED_RANGE", "PATH", "RETURN_BEFORE_BREAK",
                   "STOP_TOUCH:LONG", "STOP_TOUCH:SHORT", "DIRECTION"),
    "WAIT": (),
}


def synthetic_models(base: int, flavor: str = "NEUTRAL"):
    """Prebuilt SYNTHETIC model populations (no bundle fitting).

    Covers DIRECTION/PATH/QUANTILE plus the persistence, stop-touch and
    return-before-break tasks for the MINUTE TRADING_MINUTE-30 and DAILY
    TRADING_DAY-5 strategy horizons and the DAILY TRADING_DAY-1 background
    horizon. The daily DIRECTION flavour controls the published background
    direction: ``UP`` gives a clear UP background, ``NEUTRAL`` a published
    non-directional one (K02's aligned case for structural ranges).
    Evidence rows are bound separately per scenario via
    :func:`bind_evidence` because their geometry buckets depend on the
    actual decision snapshots.
    """
    from dataclasses import replace as _replace
    from ak_trading.v2.probability.models import (
        ForecastModelSet, _fit_conditional, _fit_empirical_cdf,
        _fit_empirical_quantile, _fit_unconditional, default_fit_config,
        model_key,
    )
    from ak_trading.v2.probability.regimes import RegimeReference

    train = base - 200 * DAY
    calibration = base - 100 * DAY
    expiry = base + 400 * DAY
    reference = RegimeReference("pending", train, train,
                                {f"{PRODUCT}|DAY|60": (.001, .1, 1.),
                                 f"{PRODUCT}|DAILY|86400": (.001, .1, 1.)},
                                {SYNTHETIC_PROVENANCE: SYNTHETIC_HASH})
    reference = _replace(reference,
                         reference_id="regime_reference:" + reference.content_hash())

    feature_keys = ("META:price_tick:v2.1.0",)
    direction_labels = (["UP"] * 540 + ["DOWN"] * 30 + ["NEUTRAL"] * 30 if flavor == "UP"
                        else ["NEUTRAL"] * 540 + ["UP"] * 230 + ["DOWN"] * 230)
    combos = (("MINUTE", Horizon("TRADING_MINUTE", 30)),
              ("DAILY", Horizon("TRADING_DAY", 5)),
              ("DAILY", Horizon("TRADING_DAY", 20)),
              ("DAILY", Horizon("TRADING_DAY", 1)))
    models: dict[str, dict] = {}

    def put(mode: str, horizon: Horizon, task: str, barrier: str, model: dict) -> None:
        model = {**model, "fit_status": "SELECTED:SYNTHETIC_PREDECLARED_FAMILY"}
        models[model_key(PRODUCT, mode, horizon, barrier, task, model["algorithm"])] = model

    levels = [(f"{PRODUCT}|DAY|UP_TREND", f"{PRODUCT}|DAY", PRODUCT)] * 600
    for mode, horizon in combos:
        put(mode, horizon, "DIRECTION", "COMMON", _fit_unconditional(
            direction_labels, ("UP", "DOWN", "NEUTRAL"), list(feature_keys), [.1], [1.]))
        for barrier in ("ATR_0.5", "ATR_1", "ATR_2"):
            put(mode, horizon, "PATH", barrier, _fit_unconditional(
                ["UP_FIRST"] * 540 + ["DOWN_FIRST"] * 60,
                ("UP_FIRST", "DOWN_FIRST", "NEITHER", "AMBIGUOUS"), list(feature_keys),
                [.1], [1.]))
        put(mode, horizon, "QUANTILE", "COMMON", _fit_empirical_quantile(
            [-2.0 + 0.04 * i for i in range(100)] * 6, levels,
            [.05, .25, .5, .75, .95], list(feature_keys), [.1], [1.], 10))
        for event in ("UP_TREND", "DOWN_TREND", "UP_BREAKOUT", "DOWN_BREAKOUT",
                      "ORDERED_RANGE", "COMPRESSION"):
            put(mode, horizon, f"PERSIST:{event}", "COMMON", _fit_conditional(
                [(.1,)] * 600, ["POS"] * 540 + ["NEG"] * 60, levels,
                ("NEG", "POS"), list(feature_keys), [.1], [1.]))
        for side in ("LONG", "SHORT"):
            put(mode, horizon, f"STOP_TOUCH:{side}", "COMMON", _fit_conditional(
                [(.1,)] * 600, ["NEG"] * 550 + ["POS"] * 50, levels,
                ("NEG", "POS"), list(feature_keys), [.1], [1.]))
        put(mode, horizon, "RETURN_BEFORE_BREAK", "COMMON", _fit_conditional(
            [(.1,)] * 600, ["POS"] * 540 + ["NEG"] * 60, levels,
            ("NEG", "POS"), list(feature_keys), [.1], [1.]))
        # Time-to-event CDF tasks: every synthetic observation resolves at
        # the first step, so the published competing CDF carries
        # RETURN_FIRST/BREAK_FIRST with zero unknown mass.
        time_levels = [tuple(f"{PRODUCT}|DAY|UP_TREND".split("|")) + (PRODUCT,)] * 600
        time_levels = [(f"{PRODUCT}|DAY|UP_TREND", f"{PRODUCT}|DAY", PRODUCT)] * 600
        for task, barrier in (("TIME:UP_TOUCH", "ATR_0.5"), ("TIME:UP_TOUCH", "ATR_1"),
                              ("TIME:UP_TOUCH", "ATR_2"), ("TIME:DOWN_TOUCH", "ATR_0.5"),
                              ("TIME:DOWN_TOUCH", "ATR_1"), ("TIME:DOWN_TOUCH", "ATR_2"),
                              ("TIME:RETURN_FIRST", "COMMON"), ("TIME:BREAK_FIRST", "COMMON")):
            put(mode, horizon, task, barrier, _fit_empirical_cdf(
                [1] * 600, time_levels, list(feature_keys), [.1], [1.]))

    provisional = ForecastModelSet("pending", reference.reference_id, feature_keys,
                                   train, calibration, expiry,
                                   default_fit_config(feature_keys, train, calibration, expiry),
                                   models, (), {SYNTHETIC_PROVENANCE: SYNTHETIC_HASH})
    return provisional, reference


def _event_probability(forecast, event: str):
    if event.startswith("PERSIST:"):
        return forecast.persistence_probabilities.get(event.split(":", 1)[1])
    if event.startswith("STOP_TOUCH:"):
        return forecast.stop_touch_probabilities.get(event.split(":", 1)[1])
    if event == "RETURN_BEFORE_BREAK":
        return forecast.return_before_break_probability
    return None


def _bucket_for_probability(value):
    """Left-closed 0.05-wide interval containing ``value`` (frozen bucket
    grammar); exact multiples keep the M1 left-closed rule."""
    import math
    if value is None:
        return "[0.00,0.05)"
    low = math.floor(round(value, 10) / 0.05) * 0.05
    if abs(low - round(low)) > 1e9:  # never: guard float noise
        low = round(low)
    return f"[{low:.2f},{low + 0.05:.2f})"


def bind_evidence(models, probes, family="TREND"):
    """Bind per-decision evidence rows harvested from a real drive.

    ``probes`` maps ``(event, base_regime)`` to the harvested
    ``(features, frozen_range, horizon)`` snapshots; each distinct
    (event, base_regime, horizon, geometry) combination becomes exactly one
    evidence row whose geometry bucket matches that decision shape.
    """
    from dataclasses import replace as _replace
    from ak_trading.v2.contracts import CalibrationEvidence
    from ak_trading.v2.probability.calibration import canonical_evidence_id

    rows = []
    seen: set[tuple] = set()
    policy_id = m1_policy.confidence_policy(family).policy_id
    for (event, base_regime), snapshots in probes.items():
        base = event.split(":", 1)[1] if event.startswith("PERSIST:") else (
            "STOP_TOUCH" if event.startswith("STOP_TOUCH:") else event)
        range_free = base in ("UP_TREND", "DOWN_TREND", "COMPRESSION",
                              "DIRECTION", "PATH", "UP_TOUCH", "DOWN_TOUCH")
        for features, frozen, horizon, value in snapshots:
            geometry = calibration_geometry_key(features, frozen, base)
            if geometry is None:
                continue
            # PATH and DIRECTION evidence use the multi-class ALL bucket;
            # every other event (range-free geometry included) carries the
            # probability bucket containing the published value.
            bucket = ("ALL" if event in ("PATH", "DIRECTION")
                      else _bucket_for_probability(value))
            identity = (event, base_regime, horizon.unit, horizon.value,
                        features.duration_seconds, geometry, bucket)
            if identity in seen:
                continue
            seen.add(identity)
            rows.append(CalibrationEvidence(
                evidence_id="pending", model_id="pending",
                policy_id=policy_id, product=PRODUCT,
                session_kind="DAILY" if features.duration_seconds == 86400 else "DAY",
                duration_seconds=features.duration_seconds, base_regime=base_regime,
                barrier_key="ATR_1" if base == "PATH" else "COMMON",
                geometry_key=geometry, event=event, horizon=horizon,
                label_version="path-label-v2.1.0", bucket=bucket, sample_count=600,
                effective_blocks=80, calibration_error=0.01, brier=0.01,
                training_end_nano=models.training_end_nano,
                calibration_end_nano=models.calibration_end_nano,
                valid_until_nano=models.valid_until_nano, status="VALID"))
    provisional = _replace(models, evidence=tuple(rows))
    identity = "forecast_model_set:" + provisional.content_hash()
    bound = tuple(_replace(_replace(row, model_id=identity),
                           evidence_id=canonical_evidence_id(row))
                  for row in rows)
    return _replace(provisional, model_set_id=identity, evidence=bound)


def _persistence_policy(event: str) -> str:
    family = "MARTINGALE" if event.startswith("STOP_TOUCH") else (
        "REVERSION" if event.startswith("PERSIST:ORDERED_RANGE")
        or event == "RETURN_BEFORE_BREAK" else "TREND")
    return m1_policy.confidence_policy(family).policy_id


def registry(mode: str):
    return strategy_registry_v2(mode)


# --- natural-chain driver --------------------------------------------------------
#
# Mirrors the production replay shape (decision/pipeline.build_strategy_context
# -> strategy.evaluate -> real payoff attachment -> gates.evaluate_gate ->
# routing.choose_proposal) on deterministic SYNTHETIC bundles. Regimes and
# forecasts come from the real assess/predict/publish producers; the provider
# records every publication and (in the harvest pass) the exact
# (features, frozen range, horizon, regime) tuple each publication saw so the
# evidence rows can be bound to the real geometry buckets.


def scenario(family: str, mode: str, delay_ns: int = 0, calendar=None,
             flavor: str | None = None, *, daily_days: int | None = None):
    """Assemble the SYNTHETIC bundle, decision list and base models."""
    from ak_trading.v2.contracts import FrozenInput

    plan = SCENARIOS[(family, mode)]
    calendar = calendar if calendar is not None else exchange_calendar(100)
    flavor = flavor if flavor is not None else plan["flavor"]
    models, reference = synthetic_models(BASE, flavor)

    pad = plan.get("pad", 0.2)
    if mode == "MINUTE":
        minute = minute_bars(MINUTE_DAY, plan["minute"], calendar, delay_ns, pad=pad)
        # The prior-evening night session's closing minute bar: without it
        # the real opening-gap factor stays MISSING and forces the regime
        # UNKNOWN (006 §2 — true session open vs previous session close).
        day = calendar_trading_days(calendar)[MINUTE_DAY]
        night = min((s for s in calendar if s.trading_day == day),
                    key=lambda s: s.start_nano)
        night_close = _bar(f"m{MINUTE_DAY}-night-close", night.end_nano - MINUTE,
                           night.end_nano, day,
                           Decimal(str(plan["minute"][0])) - Decimal("0.1"),
                           delay_ns, session_id=night.session_id, pad=pad)
        daily = []
        previous = None
        for i, close in enumerate(plan["daily"]):
            daily.append(daily_bar(i, close, calendar, delay_ns,
                                   open_price=previous, pad=pad))
            previous = close
        daily = tuple(daily)
        observations = minute
        minute = (night_close,) + minute
    else:
        closes = plan["daily"]
        if daily_days is not None:
            closes = closes[:daily_days]
        daily = []
        previous = None
        for i, close in enumerate(closes):
            daily.append(daily_bar(i, close, calendar, delay_ns,
                                   open_price=previous, pad=pad))
            previous = close
        daily = tuple(daily)
        minute = ()
        observations = daily

    decisions = [observations[index].available_at_nano
                 for index in (len(observations) - 3, len(observations) - 2,
                               len(observations) - 1)]
    bars = tuple(sorted(daily + minute, key=lambda b: (b.end_nano, b.bar_id)))
    bundle = FrozenInput(Path("SYNTHETIC_K05_NATURAL_CHAIN"), SYNTHETIC_HASH,
                         (instrument(),), calendar, bars, (), (trading_rule(),))
    return {"family": family, "mode": mode, "delay_ns": delay_ns,
            "bundle": bundle, "decisions": decisions,
            "base_models": models, "reference": reference,
            "observations": observations}


def seed_payoff_model():
    """A valid seed PathPayoffModel for context construction (reused immutable
    M4 fixture preparation; no refit per test)."""
    from test_v2_m4_payoffs import example, observations
    from ak_trading.v2.decision.payoffs import fit_path_payoff_model

    seed_proposal, seed_context = example()
    return fit_path_payoff_model(
        observations(seed_proposal, seed_context, blocks=1, per_block=4),
        BASE - 50 * DAY, seed_context.run_policy.research_policy_version)


_SEED_PAYOFF = None


def shared_seed_payoff():
    global _SEED_PAYOFF
    if _SEED_PAYOFF is None:
        _SEED_PAYOFF = seed_payoff_model()
    return _SEED_PAYOFF


def run_chain(scn, models, *, collect_events=()):
    """Drive the real chain over the scenario's decisions.

    Returns a record with the per-deision trace, the first OPEN decision
    (proposal/gate/routing) and, when ``collect_events`` is given, the
    harvested probes for evidence binding.
    """
    from dataclasses import replace as _replace
    from ak_trading.v2.decision.gates import evaluate_gate
    from ak_trading.v2.decision.payoffs import estimate_proposal_payoff, fit_path_payoff_model
    from ak_trading.v2.decision.pipeline import (
        build_strategy_context, legacy_exit_binding, model_hashes_for)
    from ak_trading.v2.decision.routing import choose_proposal, opportunity_id
    from ak_trading.v2.probability.forecast import predict_market, publish_market_forecast
    from ak_trading.v2.strategies.state import commit_observation
    from test_v2_m4_ledger import account, run_policy
    from test_v2_m4_payoffs import _range_synthetic_payoff_model

    family, mode = scn["family"], scn["mode"]
    suffix = "m1" if mode == "MINUTE" else "d1"
    strategy = strategy_registry_v2(mode)[f"{family.lower()}_{suffix}_v2"]
    spec = strategy.spec
    events = collect_events or ()
    probes: dict[tuple, list] = {}
    seed_payoff = shared_seed_payoff()
    policy = m1_policy.research_policy()
    state = None
    trace = []
    open_record = None
    for now in scn["decisions"]:
        publications = []

        def provider(model_set, request, regime):
            daily = request.features.duration_seconds == 86400
            day1 = (daily and request.horizon.unit == "TRADING_DAY"
                    and request.horizon.value == 1)
            day20 = (daily and request.horizon.unit == "TRADING_DAY"
                     and request.horizon.value == 20)
            if day1:
                forecast = predict_market(model_set, request, regime,
                                          request.features.decision_nano)
                publications.append(("predict_market", request.request_id,
                                     forecast.forecast_id))
            elif day20:
                # The DAILY background publishes at its own prior-day
                # snapshot boundary, never at the current decision.
                forecast = publish_market_forecast(
                    model_set, request, regime,
                    clock_nano=lambda at=request.features.decision_nano: at)
                publications.append(("publish_market_forecast", request.request_id,
                                     forecast.forecast_id))
            else:
                forecast = publish_market_forecast(model_set, request, regime,
                                                   clock_nano=lambda: now)
                publications.append(("publish_market_forecast", request.request_id,
                                     forecast.forecast_id))
            for event in events:
                probes.setdefault((event, regime.base), []).append(
                    (request.features, regime.frozen_range, request.horizon,
                     _event_probability(forecast, event)))
            return forecast

        book = account(str(EQUITY))
        context = build_strategy_context(
            scn["bundle"], CONTRACT, now, book, state, models, seed_payoff,
            spec, None, scn["reference"], run_policy(), (),
            exit_binding=legacy_exit_binding(spec), forecast_provider=provider)
        proposal = strategy.evaluate(context, state)
        state = commit_observation(state, proposal)
        record = {"now": now, "action": proposal.action,
                  "count": state.confirmation_count,
                  "reasons": proposal.reasons, "publications": tuple(publications)}
        if proposal.action == "OPEN" and open_record is None:
            horizon = next((f.horizon for f in context.forecasts
                            if f.forecast_id == proposal.forecast_id), spec.max_holding)
            payoff_model = _range_synthetic_payoff_model(
                proposal, context, BASE - 50 * DAY)
            bound_context = _replace(
                context, model_hashes=model_hashes_for(models, payoff_model,
                                                       scn["reference"], None))
            opp = opportunity_id(CONTRACT, now, horizon, book)
            attached = _replace(proposal, payoff=estimate_proposal_payoff(
                payoff_model, proposal, bound_context, opp))
            gate = evaluate_gate(attached, bound_context, policy, checked_at_nano=now)
            decision = choose_proposal((attached,), (gate,), book, opp, now,
                                       run_policy(), ())
            record["gate"] = gate
            record["routing"] = decision
            open_record = record
        trace.append(record)
    return {"trace": trace, "open": open_record, "probes": probes,
            "state": state}


def natural_open(family: str, mode: str, delay_ns: int = 0, calendar=None,
                 flavor: str | None = None, models=None):
    """Two-pass natural drive: harvest the real publication geometries, bind
    evidence to them, then run the same real chain with genuinely eligible
    publications. Nothing is hand-set to ALIGNED and no forecast is forced."""
    scn = scenario(family, mode, delay_ns=delay_ns, calendar=calendar,
                   flavor=flavor)
    harvest = run_chain(scn, scn["base_models"],
                        collect_events=FAMILY_EVENTS[scn["family"]])
    bound = models if models is not None else bind_evidence(
        scn["base_models"], harvest["probes"], scn["family"])
    return scn, run_chain(scn, bound)
