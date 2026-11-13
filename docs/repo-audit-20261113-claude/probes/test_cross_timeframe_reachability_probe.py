"""Round-5 external probe (advisory; not a product test, not a hard review).

Question
    After K02 (option A, main d614360) a structural range base is ALIGNED only
    when the published daily background has no argmax direction, i.e. when
    ``probability.regimes._background_trend`` returns None. That needs
    P(NEUTRAL) >= max(P(UP), P(DOWN)) or an exact UP/DOWN tie.

    The K05 natural-chain smoke test reaches OPEN for the range families with a
    hand-set DIRECTION population (540 NEUTRAL / 230 UP / 230 DOWN, see
    ``tests/v2_realistic_fixtures.synthetic_models``). What happens when the
    population follows the project's own direction-label rule instead?

Parts (product code is imported read-only; nothing is patched in the product
tree, the fixture module is reused as-is):

1.  Direction-label base rates through production code
    (``decision.pipeline._features`` -> ``forecast.build_forecast_requests`` ->
    ``labels.label_forecast``) on SYNTHETIC random-walk daily bars built in the
    export shape, for the two background horizons the pipeline uses:
    TRADING_DAY 1 (MINUTE mode) and TRADING_DAY 20 (DAILY mode).
2.  ``regimes._background_trend`` on the Laplace-smoothed class frequencies of
    those labels (``models._laplace``, what the unconditional family publishes).
3.  The K05 natural chain with ONE change: the DIRECTION population handed to
    ``models._fit_unconditional`` is the label mix measured in part 1.

Each test asserts the behaviour a tradable strategy needs, so a failure is the
finding. "Tradable" means: the strategy proposes OPEN, the real M4 gate accepts
it and the router selects it. Result at main 90cb0e9: 16 failed / 14 passed
    - test_neutral_label_share_can_win_the_argmax             2 failed
    - test_label_rule_background_is_non_directional           2 failed
    - test_non_trend_family_opens_with_label_rule_population  8 failed
    - test_trend_family_admission_survives_a_one_point_shift  4 failed
    - test_export_shaped_daily_bars_are_label_aligned         1 passed
    - test_fixture_population_control                         8 passed
    - test_trend_family_opens_when_argmax_matches             4 passed
    - test_k05_fixture_daily_bar_is_not_label_aligned         1 passed

The BREAKOUT cases break UPWARD out of a compression while the label-rule
background also points UP; option A still classifies that as CONFLICT.

Everything here is SYNTHETIC. The base rates say nothing about real RB data;
they show that the K05 result depends on a population the fixture sets by hand.
The same measurement on the real frozen input is task K02b in NEXT-BATCH.md.

Run (repository root):
    python scripts/test_offline.py -q -s \
        docs/repo-audit-20261113-claude/probes/test_cross_timeframe_reachability_probe.py
"""
from __future__ import annotations

import random
import sys
from collections import Counter
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[3] / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import v2_realistic_fixtures as rf  # noqa: E402
import test_v2_m3a_regimes as regimes_fixture  # noqa: E402
from test_v2_m3b_matrix import quality  # noqa: E402

from ak_trading.v2.contracts import FrozenInput, Horizon, MarketBar  # noqa: E402
from ak_trading.v2.decision import pipeline  # noqa: E402
from ak_trading.v2.probability import models as model_module  # noqa: E402
from ak_trading.v2.probability import regimes  # noqa: E402
from ak_trading.v2.probability.forecast import build_forecast_requests  # noqa: E402
from ak_trading.v2.probability.labels import label_forecast  # noqa: E402

RANGE_FAMILIES = ("REVERSION", "GRID", "MARTINGALE")
NON_TREND_FAMILIES = RANGE_FAMILIES + ("BREAKOUT",)   # BREAKOUT here starts from COMPRESSION
TREND_FAMILIES = ("TREND", "PULLBACK")
BACKGROUND_HORIZONS = {"DAY1 (MINUTE mode background)": 1,
                       "DAY20 (DAILY mode background)": 20}
TRADING_DAYS = 230          # stays inside the fixture instrument's listing window
WARMUP_DAYS = 65            # ma60 / er20 / atr need history
SEEDS = (11, 23, 37)
START_PRICE = 100.0
DAILY_SIGMA = 1.2           # close-to-close points; about 1.2% of price
INTRADAY_PAD = 0.5          # high/low extend this far beyond open/close
SESSION_MINUTES = 345       # 120 night + 75 + 60 + 90


def _export_shaped_daily(day_index: int, close: float, open_price: float, calendar) -> MarketBar:
    """Daily bar in the shape ``v2/export.py`` writes: it starts at the trading
    day's first session start and ends at its last session end."""
    start, end = rf.trading_day_bounds(calendar)[day_index]
    trading_day = rf.calendar_trading_days(calendar)[day_index]
    closing = rf._containing_session(calendar, end - 1)
    close_d, open_d = Decimal(str(round(close, 1))), Decimal(str(round(open_price, 1)))
    pad = Decimal(str(INTRADAY_PAD))
    return MarketBar(
        bar_id=f"rw{day_index}", contract_id=rf.CONTRACT, raw_symbol="rb2605",
        product=rf.PRODUCT, duration_seconds=86400, datetime_nano=start,
        start_nano=start, end_nano=end, trading_day=trading_day,
        session_id=closing.session_id if closing else "",
        available_at_nano=end, imported_at_nano=None,
        open=open_d, high=max(close_d, open_d) + pad, low=min(close_d, open_d) - pad,
        close=close_d, volume=Decimal("1000"), open_oi=Decimal("10"),
        close_oi=Decimal("10"), complete=True, expected_minutes=SESSION_MINUTES,
        actual_minutes=SESSION_MINUTES, source="test", quality=quality())


def _random_walk_bundle(seed: int):
    rng = random.Random(seed)
    calendar = rf.exchange_calendar(TRADING_DAYS)
    bars, previous = [], START_PRICE
    for index in range(TRADING_DAYS):
        close = max(20.0, previous + rng.gauss(0.0, DAILY_SIGMA))
        bars.append(_export_shaped_daily(index, close, previous, calendar))
        previous = close
    bundle = FrozenInput(Path("SYNTHETIC_R5_CROSS_TIMEFRAME_PROBE"), rf.SYNTHETIC_HASH,
                         (rf.instrument(),), calendar, tuple(bars), (),
                         (rf.trading_rule(),))
    return bundle, tuple(bars)


def _label_counts(horizon_days: int) -> Counter:
    """Direction labels produced by production code for one horizon."""
    counts: Counter = Counter()
    for seed in SEEDS:
        bundle, bars = _random_walk_bundle(seed)
        sessions = bundle.sessions
        as_of = bars[-1].end_nano + 1
        for index in range(WARMUP_DAYS, TRADING_DAYS - horizon_days):
            decision = bars[index].end_nano
            features, _, _ = pipeline._features(
                bundle, rf.CONTRACT, decision, 86400, None, None, sessions,
                canonical_daily=True)
            if features.reference_price is None or features.atr_points is None:
                counts["NO_ANCHOR"] += 1
                continue
            requests = build_forecast_requests(
                features, (Horizon("TRADING_DAY", horizon_days),), None, sessions)
            request = next(r for r in requests if r.barrier_key == "ATR_1")
            future = tuple(b for b in bars
                           if request.window_start_nano <= b.start_nano < request.window_end_nano)
            label = label_forecast(request, future, sessions, (), as_of)
            counts[label.direction if label.status == "MATURE" else label.status] += 1
    return counts


_COUNTS_CACHE: dict[int, Counter] = {}


def _counts(horizon_days: int) -> Counter:
    if horizon_days not in _COUNTS_CACHE:
        _COUNTS_CACHE[horizon_days] = _label_counts(horizon_days)
    return _COUNTS_CACHE[horizon_days]


def _shares(counts: Counter) -> dict[str, float]:
    total = sum(counts[name] for name in ("UP", "DOWN", "NEUTRAL"))
    return {name: counts[name] / total for name in ("UP", "DOWN", "NEUTRAL")}


def _population(counts: Counter, size: int = 1000) -> list[str]:
    """A DIRECTION population of ``size`` labels in the measured proportions."""
    shares = _shares(counts)
    up = round(shares["UP"] * size)
    down = round(shares["DOWN"] * size)
    return ["UP"] * up + ["DOWN"] * down + ["NEUTRAL"] * (size - up - down)


@contextmanager
def _direction_population(labels: list[str]):
    """Hand ``labels`` to the fixture's DIRECTION fit; every other fit is
    untouched. The product function is wrapped for the duration of the block
    and restored afterwards."""
    original = model_module._fit_unconditional

    def patched(samples_y, classes, *args, **kwargs):
        if tuple(classes) == ("UP", "DOWN", "NEUTRAL"):
            samples_y = list(labels)
        return original(samples_y, classes, *args, **kwargs)

    model_module._fit_unconditional = patched
    try:
        yield
    finally:
        model_module._fit_unconditional = original


def _natural(family: str, mode: str, labels: list[str] | None):
    if labels is None:
        return rf.natural_open(family, mode)[1]
    with _direction_population(labels):
        return rf.natural_open(family, mode)[1]


def _reasons(result) -> set[str]:
    return {reason for record in result["trace"] for reason in record["reasons"]}


def _tradable(result) -> bool:
    """An OPEN proposal that also passes the real M4 gate and is selected."""
    record = result["open"]
    return (record is not None and record["gate"].eligible
            and record["routing"].selected_proposal_id is not None)


def _why(result) -> str:
    record = result["open"]
    if record is None:
        return f"no OPEN proposal; M3B reasons={sorted(_reasons(result))}"
    return f"OPEN proposed, M4 gate rejected it: {sorted(record['gate'].reasons)}"


# --- part 1: label base rates through production code -----------------------------


def test_export_shaped_daily_bars_are_label_aligned():
    """Control: the probe's daily bars mature under the production label code."""
    counts = _counts(1)
    assert counts["UP"] + counts["DOWN"] + counts["NEUTRAL"] > 300, counts
    assert not {"INVALID", "MISSING", "UNMATURED"} & set(counts), counts


@pytest.mark.parametrize("label,horizon_days", list(BACKGROUND_HORIZONS.items()))
def test_neutral_label_share_can_win_the_argmax(label, horizon_days):
    """A non-directional background needs NEUTRAL to be at least as likely as
    the more likely direction."""
    counts = _counts(horizon_days)
    shares = _shares(counts)
    print(f"\n{label}: n={sum(counts.values())} "
          f"UP={shares['UP']:.3f} DOWN={shares['DOWN']:.3f} NEUTRAL={shares['NEUTRAL']:.3f}")
    assert shares["NEUTRAL"] >= max(shares["UP"], shares["DOWN"]), (
        f"{label}: NEUTRAL share {shares['NEUTRAL']:.3f} is below the larger "
        f"direction share {max(shares['UP'], shares['DOWN']):.3f}")


# --- part 2: what the regime producer makes of such a background ------------------


@pytest.mark.parametrize("label,horizon_days", list(BACKGROUND_HORIZONS.items()))
def test_label_rule_background_is_non_directional(label, horizon_days):
    counts = _counts(horizon_days)
    up, down, _neutral = model_module._laplace(
        [counts["UP"], counts["DOWN"], counts["NEUTRAL"]])
    background = regimes_fixture._background(up, down)
    trend = regimes._background_trend(background)
    assert trend is None, (
        f"{label}: _background_trend={trend!r} for P(UP)={up:.3f} P(DOWN)={down:.3f}; "
        "a range base then gets CONFLICT and admission rejects it")


# --- part 3: the K05 natural chain with the measured population -------------------


@pytest.mark.parametrize("mode", ["MINUTE", "DAILY"])
@pytest.mark.parametrize("family", NON_TREND_FAMILIES)
def test_fixture_population_control(family, mode):
    """Control: with the fixture's hand-set 54% NEUTRAL population the chain
    proposes OPEN, passes the M4 gate and is selected."""
    result = _natural(family, mode, None)
    assert _tradable(result), f"{family} {mode}: {_why(result)}"


@pytest.mark.parametrize("mode", ["MINUTE", "DAILY"])
@pytest.mark.parametrize("family", NON_TREND_FAMILIES)
def test_non_trend_family_opens_with_label_rule_population(family, mode):
    """Identical chain, bars, evidence binding and payoff model; only the
    DIRECTION population follows the label rule."""
    horizon_days = 1 if mode == "MINUTE" else 20
    result = _natural(family, mode, _population(_counts(horizon_days)))
    assert _tradable(result), f"{family} {mode}: {_why(result)}"


@pytest.mark.parametrize("mode", ["MINUTE", "DAILY"])
@pytest.mark.parametrize("family", TREND_FAMILIES)
def test_trend_family_opens_when_argmax_matches(family, mode):
    """Control: UP 44% / DOWN 43% / NEUTRAL 13% is ALIGNED for an up-trend."""
    labels = ["UP"] * 440 + ["DOWN"] * 430 + ["NEUTRAL"] * 130
    result = _natural(family, mode, labels)
    assert _tradable(result), f"{family} {mode}: {_why(result)}"


@pytest.mark.parametrize("mode", ["MINUTE", "DAILY"])
@pytest.mark.parametrize("family", TREND_FAMILIES)
def test_trend_family_admission_survives_a_one_point_shift(family, mode):
    """Swap one percentage point between UP and DOWN: nothing about the bars
    or the trend structure changes."""
    labels = ["UP"] * 430 + ["DOWN"] * 440 + ["NEUTRAL"] * 130
    result = _natural(family, mode, labels)
    assert _tradable(result), f"{family} {mode}: {_why(result)}"


# --- a note for H6-prime: the K05 daily bar shape differs from the export ----------


def test_k05_fixture_daily_bar_is_not_label_aligned():
    """``rf.daily_bar`` starts 24h before the close; the export starts a daily
    bar at the trading day's first session start. The label code only accepts
    the export shape, so labels built on the K05 daily bars are INVALID. The
    smoke test never builds labels, which is why this does not show up there."""
    calendar = rf.exchange_calendar(TRADING_DAYS)
    closes = [START_PRICE + 0.1 * i for i in range(90)]
    previous = None
    bars = []
    for index, close in enumerate(closes):
        bars.append(rf.daily_bar(index, close, calendar, open_price=previous))
        previous = close
    bundle = FrozenInput(Path("SYNTHETIC_R5_K05_SHAPE"), rf.SYNTHETIC_HASH,
                         (rf.instrument(),), calendar, tuple(bars), (),
                         (rf.trading_rule(),))
    decision = bars[70].end_nano
    features, _, _ = pipeline._features(bundle, rf.CONTRACT, decision, 86400, None,
                                        None, bundle.sessions, canonical_daily=True)
    request = next(r for r in build_forecast_requests(
        features, (Horizon("TRADING_DAY", 1),), None, bundle.sessions)
        if r.barrier_key == "ATR_1")
    future = tuple(b for b in bars if b.end_nano > decision)[:1]
    label = label_forecast(request, future, bundle.sessions, (), bars[-1].end_nano + 1)
    assert label.status == "INVALID" and "bar_not_slot_aligned" in label.reasons, (
        label.status, label.reasons)
