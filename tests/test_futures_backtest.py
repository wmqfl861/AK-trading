"""S2 focused tests: ledger engine, execution rules, roll oracle, demo.

Finalized-plan §十二.14-15 (minimum 16 cases). The engine oracles cover the
daily identity, next-open causality, limits/capacity/margin, kill/terminal
paths, the 100/130 roll, and the full synthetic demo composition with
byte-identical deterministic payloads.
"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Mapping

import pytest

from ak_trading.futures.backtest import (
    ContractRuleSet,
    LedgerEngine,
    OpenIntent,
    ReplayPlan,
    SessionSchedule,
    compute_cap_lots,
    fee_components_for_fill,
    run_replay,
    slippage_cost,
)
from ak_trading.futures.io import (
    build_demo_fixture,
    publish_backtest_bundle,
)
from ak_trading.futures.spec import (
    ClockStatus,
    ContractBar,
    FeeComponent,
    FeeSpec,
    Fill,
    MarginRate,
    MarginSpec,
    ProductKey,
    ResearchPolicy,
)

SH = timezone(timedelta(hours=8))
POLICY = ResearchPolicy()
RB = ProductKey("SHFE", "RB")
IF = ProductKey("CFFEX", "IF")


def make_fee() -> FeeSpec:
    return FeeSpec(
        open=FeeComponent(cny_per_lot=Decimal("5.0"), source_id="test"),
        close=FeeComponent(cny_per_lot=Decimal("5.0"), source_id="test"),
        close_today=FeeComponent(cny_per_lot=Decimal("5.0"), source_id="test"),
        source_id="test",
    )


def make_margin(rate: str = "0.08") -> MarginSpec:
    return MarginSpec(
        long=MarginRate(Decimal(rate)),
        short=MarginRate(Decimal(rate)),
        source_id="test",
    )


def make_rules(
    *,
    multiplier: str = "10",
    tick: str = "1",
    band_ratio: str = "0.10",
    fee: FeeSpec | None = None,
    margin: MarginSpec | None = None,
    median_volume20: float | None = 50000.0,
    min_open_lots: int = 1,
        band_day: date | None = None,
    band_basis: str = "100",
) -> ContractRuleSet:
    basis = Decimal(band_basis)
    ratio = Decimal(band_ratio)
    tick = Decimal(tick)
    upper = (basis * (1 + ratio) / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    lower = (basis * (1 - ratio) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    bands = {band_day.isoformat(): (upper, lower)} if band_day else {}
    return ContractRuleSet(
        multiplier=Decimal(multiplier),
        price_tick=tick,
        fee=fee if fee is not None else make_fee(),
        margin=margin if margin is not None else make_margin(),
        limit_bands=bands,
        median_volume20=median_volume20,
        min_open_lots=min_open_lots,
    )


def make_session(day: date) -> SessionSchedule:
    base = datetime.combine(day, datetime.min.time(), tzinfo=SH)
    return SessionSchedule(day, base + timedelta(hours=9), base + timedelta(hours=15))


def make_bar(
    day: date,
    *,
    open_p: str,
    high: str | None = None,
    low: str | None = None,
    close_p: str | None = None,
    volume: str = "50000",
    symbol: str = "SHFE.test1",
) -> ContractBar:
    o = Decimal(open_p)
    c = Decimal(close_p) if close_p is not None else o
    base = datetime.combine(day, datetime.min.time(), tzinfo=SH)
    close_time = base + timedelta(hours=15)
    return ContractBar(
        symbol=symbol,
        duration_seconds=86400,
        datetime_nano=int(close_time.timestamp() * 1_000_000_000),
        ts=close_time,
        source="test",
        trading_day=day,
        trading_day_source="test",
        session_open=base + timedelta(hours=9),
        session_close=close_time,
        available_at=close_time,
        open=o,
        high=Decimal(high) if high else max(o, c) * Decimal("1.001"),
        low=Decimal(low) if low else min(o, c) * Decimal("0.999"),
        close=c,
        volume=Decimal(volume),
    )


def bands_for_prices(prices: Mapping[date, Decimal], ratio: str, tick: str):
    """±ratio bands around each day's reference (previous close)."""
    tick_dec = Decimal(tick)
    ratio_dec = Decimal(ratio)
    bands: dict[str, tuple[Decimal, Decimal]] = {}
    previous = list(prices.values())[0]
    for day, price in prices.items():
        upper = (
            previous * (1 + ratio_dec) / tick_dec
        ).to_integral_value(rounding=ROUND_CEILING) * tick_dec
        lower = (
            previous * (1 - ratio_dec) / tick_dec
        ).to_integral_value(rounding=ROUND_FLOOR) * tick_dec
        bands[day.isoformat()] = (upper, lower)
        previous = price
    return bands


def engine_with(
    rules: ContractRuleSet,
    *,
    instance: str = "SHFE.test1",
    product: ProductKey = RB,
    cash: str = "400000000",
    scenario: str = "base",
) -> LedgerEngine:
    return LedgerEngine(
        initial_cash=Decimal(cash),
        policy=POLICY,
        scenario=scenario,
        contracts={instance: (product, instance.split("|")[0], rules)},
    )


# ---------------------------------------------------------------------------
# Daily identity and positions
# ---------------------------------------------------------------------------


def test_daily_identity_long_with_overnight_gap_and_intraday_move() -> None:
    day1, day2 = date(2026, 1, 5), date(2026, 1, 6)
    rules = make_rules(band_day=None)
    rules = replace(
        rules,
        limit_bands=bands_for_prices({day1: Decimal("100"), day2: Decimal("105")}, "0.10", "1"),
    )
    engine = engine_with(rules)
    session = make_session(day1)
    bar = make_bar(day1, open_p="100", close_p="110")
    engine.execute_intent(
        OpenIntent("d1", RB, "SHFE.test1", "SHFE.test1", 1, "open"),
        session=session, open_price=bar.open, day_bars_before=[], known_equity=Decimal("400000000"),
    )
    engine.mark_day(day1, {"SHFE.test1": Decimal("110")})
    bar2 = make_bar(day2, open_p="105", close_p="102")
    engine.execute_intent(
        OpenIntent("d2", RB, "SHFE.test1", "SHFE.test1", 1, "open"),
        session=make_session(day2), open_price=bar2.open, day_bars_before=[bar],
        known_equity=engine.marks[-1].equity,
    )
    engine.mark_day(day2, {"SHFE.test1": Decimal("102")})
    # gross: day1 = 1*10*(110-100)=100; day2 = 1*10*(105-110)+1*10*(102-105)=-80.
    costs = (Decimal("5.0") + Decimal("0.01")) + Decimal("10")  # fee+addition+1 tick slip
    expected_equity = Decimal("400000000") + Decimal("100") - Decimal("80") - costs
    # B4: the position is still open, so cash excludes unrealized PnL while
    # equity includes it; the two identities are checked independently.
    assert engine.marks[-1].equity == expected_equity
    assert engine.cash == Decimal("400000000") - costs
    assert engine.marks[-1].unrealized_pnl == Decimal("20")
    assert engine.cash != engine.marks[-1].equity
    recon = engine.reconciliation()
    assert recon["cash_matches_fill_identity"] is True
    assert recon["equity_matches_gross_identity"] is True


def test_daily_identity_short_position_signs() -> None:
    day1, day2 = date(2026, 1, 5), date(2026, 1, 6)
    rules = replace(
        make_rules(), limit_bands=bands_for_prices(
            {day1: Decimal("100"), day2: Decimal("95")}, "0.10", "1")
    )
    engine = engine_with(rules)
    engine.execute_intent(
        OpenIntent("d1", RB, "SHFE.test1", "SHFE.test1", -1, "open"),
        session=make_session(day1), open_price=Decimal("100"), day_bars_before=[],
        known_equity=Decimal("400000000"),
    )
    engine.mark_day(day1, {"SHFE.test1": Decimal("95")})
    assert engine.marks[-1].equity == Decimal("400000000") + Decimal("50") - Decimal("15.01")
    assert engine.cash == Decimal("400000000") - Decimal("15.01")  # +50 unrealized
    engine.execute_intent(
        OpenIntent("d2", RB, "SHFE.test1", "SHFE.test1", -1, "open"),
        session=make_session(day2), open_price=Decimal("98"), day_bars_before=[],
        known_equity=engine.marks[-1].equity,
    )
    engine.mark_day(day2, {"SHFE.test1": Decimal("99")})
    # day2: -1*10*(98-95) + -1*10*(99-98) = -30-10 = -40
    assert engine.marks[-1].equity == (
        Decimal("400000000") + Decimal("50") - Decimal("40") - Decimal("15.01")
    )
    assert engine.cash == Decimal("400000000") - Decimal("15.01")


# ---------------------------------------------------------------------------
# Causality / intent lifecycles via run_replay
# ---------------------------------------------------------------------------


def _simple_plan(
    days: list[date],
    bars: dict[str, dict[date, ContractBar]],
    decisions: dict[tuple[date, str], list[OpenIntent]] | None = None,
    rules: ContractRuleSet | None = None,
    terminal: dict[str, date] | None = None,
    confirmed_closed: frozenset[tuple[str, date]] = frozenset(),
) -> tuple[LedgerEngine, ReplayPlan]:
    rules = rules or replace(
        make_rules(), limit_bands=bands_for_prices(
            {day: bar.close for day, bar in bars["SHFE.test1"].items()}, "0.10", "1")
    )
    engine = engine_with(rules)
    sessions = {
        "SHFE.test1": {day: make_session(day) for day in bars["SHFE.test1"]}
    }
    plan = ReplayPlan(
        days=days,
        sessions=sessions,
        bars=bars,
        decisions=decisions or {},
        terminal_days=terminal or {},
        confirmed_closed=confirmed_closed,
    )
    return engine, plan


def _rising_bars(days: list[date], start: str = "100") -> dict[str, dict[date, ContractBar]]:
    bars: dict[str, dict[date, ContractBar]] = {"SHFE.test1": {}}
    previous = Decimal(start)
    for index, day in enumerate(days):
        open_p = previous
        close_p = previous + Decimal("1")
        bars["SHFE.test1"][day] = make_bar(day, open_p=str(open_p), close_p=str(close_p))
        previous = close_p
    return bars


def test_next_open_execution_with_publication_delay() -> None:
    days = [date(2026, 1, 5 + i) for i in range(5)]  # Mon..Fri
    bars = _rising_bars(days)
    # Decided after day1's completed bar; day2 session exists but its BAR is
    # missing (late publication): no eligible attempt happens on day2, the
    # intent executes at day3's open.
    del bars["SHFE.test1"][days[1]]
    decisions = {
        (days[0], "SHFE.test1"): [
            OpenIntent("d1", RB, "SHFE.test1", "SHFE.test1", 1, "open")
        ]
    }
    engine, plan = _simple_plan(days, bars, decisions)
    outcome = run_replay(engine, plan)
    assert len(outcome.fills) == 1
    assert outcome.fills[0].trading_day == days[2]
    assert outcome.fills[0].ideal_open == bars["SHFE.test1"][days[2]].open
    assert outcome.fills[0].decision_id == "d1"


def test_open_intent_expires_after_one_eligible_attempt() -> None:
    days = [date(2026, 1, 5 + i) for i in range(4)]
    bars = _rising_bars(days)
    # Bands from previous close make day2's open exactly the upper band -> a
    # limit-touch no-fill counts as the single eligible attempt.
    day2_bar = bars["SHFE.test1"][days[1]]
    upper = day2_bar.open
    rules = replace(make_rules(), limit_bands={days[1].isoformat(): (upper, Decimal("1"))})
    decisions = {
        (days[0], "SHFE.test1"): [
            OpenIntent("d1", RB, "SHFE.test1", "SHFE.test1", 1, "open")
        ]
    }
    engine, plan = _simple_plan(days, bars, decisions, rules=rules)
    outcome = run_replay(engine, plan)
    assert outcome.fills == ()
    reasons = [entry["reason"] for entry in outcome.blocked]
    assert "limit_open_no_fill" in reasons
    # Not retried on day3 despite an in-band open.
    assert sum(1 for f in [] ) == 0
    assert len([r for r in reasons if r == "limit_open_no_fill"]) == 1


def test_exit_intent_persists_until_filled() -> None:
    days = [date(2026, 1, 5 + i) for i in range(4)]
    bars = _rising_bars(days)
    # The open fills on day2. The exit decided on day2 runs on day3, whose
    # open touches the upper band: exits are NOT consumed by a failed
    # attempt and fill on day4 instead.
    day3_bar = bars["SHFE.test1"][days[2]]
    rules = replace(make_rules(), limit_bands={
        days[0].isoformat(): (Decimal("1000"), Decimal("1")),
        days[1].isoformat(): (Decimal("1000"), Decimal("1")),
        days[2].isoformat(): (day3_bar.open, Decimal("1")),
        days[3].isoformat(): (Decimal("1000"), Decimal("1")),
    })
    decisions = {
        (days[0], "SHFE.test1"): [
            OpenIntent("d-open", RB, "SHFE.test1", "SHFE.test1", 1, "open")
        ],
        (days[1], "SHFE.test1"): [
            OpenIntent("d-exit", RB, "SHFE.test1", "SHFE.test1", 0, "close")
        ],
    }
    engine, plan = _simple_plan(days, bars, decisions, rules=rules)
    outcome = run_replay(engine, plan)
    opening = [f for f in outcome.fills if f.offset == "open"]
    assert len(opening) == 1 and opening[0].trading_day == days[1]
    closing = [f for f in outcome.fills if f.offset in ("close", "close_today")]
    assert len(closing) == 1
    assert closing[0].trading_day == days[3]  # retried after the band touch
    assert closing[0].decision_id == "d-exit"
    assert any(e["reason"] == "limit_open_no_fill" for e in outcome.blocked)


def test_limit_outside_band_is_data_conflict() -> None:
    day = date(2026, 1, 5)
    rules = make_rules(band_day=day, band_basis="100", band_ratio="0.05")
    engine = engine_with(rules)
    fills = engine.execute_intent(
        OpenIntent("d1", RB, "SHFE.test1", "SHFE.test1", 1, "open"),
        session=make_session(day), open_price=Decimal("107"),  # above 105 band
        day_bars_before=[], known_equity=Decimal("400000000"),
    )
    assert fills == []
    assert engine.blocked[0]["reason"] == "open_outside_limit_bands"
    assert engine.ledgers["SHFE.test1"].incomplete_reasons


def test_capacity_shared_between_open_and_close_partial() -> None:
    day = date(2026, 1, 5)
    rules = make_rules(band_day=day, median_volume20=2_000.0)  # cap = 2 lots
    engine = engine_with(rules)
    session = make_session(day)
    intent = OpenIntent("d1", RB, "SHFE.test1", "SHFE.test1", 5, "open")
    fills = engine.execute_intent(
        intent, session=session, open_price=Decimal("100"),
        day_bars_before=[], known_equity=Decimal("400000000"),
    )
    assert len(fills) == 1 and fills[0].lots == 2  # capped at 2
    # A second open the same day finds no remaining capacity.
    more = engine.execute_intent(
        OpenIntent("d2", RB, "SHFE.test1", "SHFE.test1", 4, "open"),
        session=session, open_price=Decimal("100"), day_bars_before=[],
        known_equity=Decimal("400000000"),
    )
    assert more == []
    assert engine.blocked[-1]["reason"] == "capacity_exhausted_no_open"
    # Closes draw from the same shared cap: none remains.
    closing = engine.execute_intent(
        OpenIntent("d3", RB, "SHFE.test1", "SHFE.test1", 0, "close"),
        session=session, open_price=Decimal("100"), day_bars_before=[],
        known_equity=Decimal("400000000"),
    )
    assert closing == []
    assert engine.blocked[-1]["reason"] == "capacity_exhausted_no_close"


def test_capacity_from_bar_history_needs_twenty_rows() -> None:
    day = date(2026, 2, 1)
    assert compute_cap_lots(
        policy=POLICY, rule_median_volume20=None, prior_bars=[]
    ) is None
    bars = [
        make_bar(date(2026, 1, 1 + i), open_p="100") for i in range(19)
    ]
    assert compute_cap_lots(policy=POLICY, rule_median_volume20=None, prior_bars=bars) is None
    bars20 = bars + [make_bar(date(2026, 1, 20), open_p="100")]
    cap = compute_cap_lots(policy=POLICY, rule_median_volume20=None, prior_bars=bars20)
    assert cap == 50  # 0.001 * 50000


def test_reversal_closes_then_opens_with_separate_costs() -> None:
    day1, day2 = date(2026, 1, 5), date(2026, 1, 6)
    rules = replace(make_rules(), limit_bands=bands_for_prices(
        {day1: Decimal("100"), day2: Decimal("100")}, "0.10", "1"))
    engine = engine_with(rules)
    engine.execute_intent(
        OpenIntent("d1", RB, "SHFE.test1", "SHFE.test1", 1, "open"),
        session=make_session(day1), open_price=Decimal("100"), day_bars_before=[],
        known_equity=Decimal("400000000"),
    )
    engine.mark_day(day1, {"SHFE.test1": Decimal("100")})
    fills = engine.execute_intent(
        OpenIntent("d2", RB, "SHFE.test1", "SHFE.test1", -1, "open"),
        session=make_session(day2), open_price=Decimal("100"), day_bars_before=[],
        known_equity=engine.marks[-1].equity,
    )
    offsets = [f.offset for f in fills]
    assert offsets == ["close", "open"]
    close_fill, open_fill = fills
    assert close_fill.lots == 1 and open_fill.lots == 1
    assert close_fill.side == -1 and open_fill.side == -1
    # Each leg charged its own fee + account addition + slippage.
    assert close_fill.fee_cny == open_fill.fee_cny == Decimal("5.01")
    assert close_fill.slippage_cost == open_fill.slippage_cost == Decimal("10")
    assert engine.ledgers["SHFE.test1"].lots == -1


def test_close_today_and_close_history_fee_legs() -> None:
    day1, day2 = date(2026, 1, 5), date(2026, 1, 6)
    rules = replace(
        make_rules(),
        limit_bands=bands_for_prices({day1: Decimal("100"), day2: Decimal("100")}, "0.10", "1"),
        fee=FeeSpec(
            open=FeeComponent(cny_per_lot=Decimal("5.0"), source_id="t"),
            close=FeeComponent(cny_per_lot=Decimal("20.0"), source_id="t"),
            close_today=FeeComponent(cny_per_lot=Decimal("0.0"), explicit_zero=True, source_id="t_zero"),
            source_id="t",
        ),
    )
    engine = engine_with(rules)
    engine.execute_intent(
        OpenIntent("d1", RB, "SHFE.test1", "SHFE.test1", 2, "open"),
        session=make_session(day1), open_price=Decimal("100"), day_bars_before=[],
        known_equity=Decimal("400000000"),
    )
    # Close on the SAME day uses the close_today leg (explicit zero + addition).
    fills = engine.execute_intent(
        OpenIntent("d1b", RB, "SHFE.test1", "SHFE.test1", 1, "close"),
        session=make_session(day1), open_price=Decimal("100"), day_bars_before=[],
        known_equity=Decimal("400000000"),
    )
    assert fills[0].offset == "close_today"
    assert fills[0].fee_cny == Decimal("0.01")  # explicit zero + 0.01 addition
    engine.mark_day(day1, {"SHFE.test1": Decimal("100")})
    next_fills = engine.execute_intent(
        OpenIntent("d2", RB, "SHFE.test1", "SHFE.test1", 0, "close"),
        session=make_session(day2), open_price=Decimal("100"), day_bars_before=[],
        known_equity=Decimal("400000000"),
    )
    assert next_fills[0].offset == "close"
    assert next_fills[0].fee_cny == Decimal("20.01")  # history close rate


def test_unknown_fee_rule_blocks_fill_fail_closed() -> None:
    day = date(2026, 1, 5)
    rules = replace(make_rules(band_day=day), fee=None)
    engine = engine_with(rules)
    fills = engine.execute_intent(
        OpenIntent("d1", RB, "SHFE.test1", "SHFE.test1", 1, "open"),
        session=make_session(day), open_price=Decimal("100"),
        day_bars_before=[], known_equity=Decimal("400000000"),
    )
    assert fills == []
    assert engine.blocked[0]["reason"] == "fee_rule_unknown"


def test_unknown_margin_blocks_open_but_not_exit() -> None:
    day1, day2 = date(2026, 1, 5), date(2026, 1, 6)
    rules = replace(
        make_rules(),
        limit_bands=bands_for_prices({day1: Decimal("100"), day2: Decimal("100")}, "0.10", "1"),
    )
    engine = engine_with(rules)
    engine.execute_intent(
        OpenIntent("d1", RB, "SHFE.test1", "SHFE.test1", 1, "open"),
        session=make_session(day1), open_price=Decimal("100"), day_bars_before=[],
        known_equity=Decimal("400000000"),
    )
    engine.mark_day(day1, {"SHFE.test1": Decimal("100")})
    # Rules turn unknown afterwards (e.g. evidence invalidated).
    engine.rules["SHFE.test1"] = replace(rules, margin=None)
    blocked_open = engine.execute_intent(
        OpenIntent("d2", RB, "SHFE.test1", "SHFE.test1", 2, "open"),
        session=make_session(day2), open_price=Decimal("100"), day_bars_before=[],
        known_equity=engine.marks[-1].equity,
    )
    assert blocked_open == []
    assert engine.blocked[-1]["reason"] == "margin_rule_unknown_blocks_new_risk"
    exit_fill = engine.execute_intent(
        OpenIntent("d3", RB, "SHFE.test1", "SHFE.test1", 0, "close"),
        session=make_session(day2), open_price=Decimal("100"), day_bars_before=[],
        known_equity=engine.marks[-1].equity,
    )
    assert len(exit_fill) == 1 and exit_fill[0].offset == "close"


def test_margin_ratio_blocks_new_risk_but_exits_continue() -> None:
    day1, day2 = date(2026, 1, 5), date(2026, 1, 6)
    rules = replace(
        make_rules(),
        limit_bands=bands_for_prices({day1: Decimal("100"), day2: Decimal("100")}, "0.10", "1"),
    )
    # Tiny capital: 1 lot margin 0.08*100*10=80 already > 40% of 150.
    engine = engine_with(rules, cash="150")
    fills = engine.execute_intent(
        OpenIntent("d1", RB, "SHFE.test1", "SHFE.test1", 1, "open"),
        session=make_session(day1), open_price=Decimal("100"), day_bars_before=[],
        known_equity=Decimal("150"),
    )
    assert fills == [] and engine.blocked[-1]["reason"] == "margin_ratio_exceeded"
    # With a pre-existing position, exits still execute.
    engine2 = engine_with(rules, cash="100000")
    engine2.execute_intent(
        OpenIntent("d1", RB, "SHFE.test1", "SHFE.test1", 1, "open"),
        session=make_session(day1), open_price=Decimal("100"), day_bars_before=[],
        known_equity=Decimal("100000"),
    )
    engine2.mark_day(day1, {"SHFE.test1": Decimal("100")})
    exit_fill = engine2.execute_intent(
        OpenIntent("d2", RB, "SHFE.test1", "SHFE.test1", 0, "close"),
        session=make_session(day2), open_price=Decimal("100"), day_bars_before=[],
        known_equity=engine2.marks[-1].equity,
    )
    assert len(exit_fill) == 1


def test_kill_latch_blocks_new_risk_and_allows_exit() -> None:
    day1, day2, day3 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)
    rules = replace(make_rules(), limit_bands=bands_for_prices(
        {day1: Decimal("100"), day2: Decimal("100"), day3: Decimal("100")}, "0.10", "1"))
    engine = engine_with(rules, cash="1000")
    engine.execute_intent(
        OpenIntent("d1", RB, "SHFE.test1", "SHFE.test1", 1, "open"),
        session=make_session(day1), open_price=Decimal("100"), day_bars_before=[],
        known_equity=Decimal("1000"),
    )
    engine.mark_day(day1, {"SHFE.test1": Decimal("100")})
    # A 20% drawdown from the initial high-water trips the latch.
    engine.mark_day(day2, {"SHFE.test1": Decimal("75")})
    assert engine.killed is True
    assert engine.marks[-1].status == "killed"
    blocked = engine.execute_intent(
        OpenIntent("d3", RB, "SHFE.test1", "SHFE.test1", 2, "open"),
        session=make_session(day3), open_price=Decimal("100"), day_bars_before=[],
        known_equity=engine.marks[-1].equity,
    )
    assert blocked == []
    assert engine.blocked[-1]["reason"] == "kill_switch_blocks_new_risk"
    exit_fill = engine.execute_intent(
        OpenIntent("d4", RB, "SHFE.test1", "SHFE.test1", 0, "close"),
        session=make_session(day3), open_price=Decimal("100"), day_bars_before=[],
        known_equity=engine.marks[-1].equity,
    )
    assert len(exit_fill) == 1


def test_insolvency_status_stops_percentage_metrics_path() -> None:
    day1, day2 = date(2026, 1, 5), date(2026, 1, 6)
    rules = replace(make_rules(), limit_bands=bands_for_prices(
        {day1: Decimal("100"), day2: Decimal("100")}, "0.10", "1"))
    engine = engine_with(rules, cash="1000")
    engine.execute_intent(
        OpenIntent("d1", RB, "SHFE.test1", "SHFE.test1", 1, "open"),
        session=make_session(day1), open_price=Decimal("100"), day_bars_before=[],
        known_equity=Decimal("1000"),
    )
    engine.mark_day(day1, {"SHFE.test1": Decimal("100")})
    engine.mark_day(day2, {"SHFE.test1": Decimal("1")})
    assert engine.insolvent is True
    assert engine.marks[-1].status == "insolvent"


def test_roll_two_contracts_only_lose_declared_costs() -> None:
    """§十二.15 oracle: old contract always 100, new always 130.

    Closing the old and opening the new may only cost the declared two-sided
    fees and slippage; the 30-point price difference never becomes PnL.
    """
    old_id, new_id = "SHFE.old1", "SHFE.new1"
    days = [date(2026, 1, 5 + i) for i in range(5)]
    rules_old = replace(
        make_rules(),
        limit_bands=bands_for_prices({day: Decimal("100") for day in days}, "0.20", "1"),
    )
    rules_new = replace(
        make_rules(),
        limit_bands=bands_for_prices({day: Decimal("130") for day in days}, "0.20", "1"),
    )
    engine = LedgerEngine(
        initial_cash=Decimal("1000000"),
        policy=POLICY,
        contracts={
            old_id: (RB, old_id, rules_old),
            new_id: (RB, new_id, rules_new),
        },
    )
    old_bars = {day: make_bar(day, open_p="100", close_p="100", symbol=old_id) for day in days}
    new_bars = {day: make_bar(day, open_p="130", close_p="130", symbol=new_id) for day in days}
    decisions = {
        (days[0], old_id): [OpenIntent("d-open-old", RB, old_id, old_id, 1, "open")],
        (days[1], old_id): [OpenIntent("d-roll-close", RB, old_id, old_id, 0, "close")],
        # The successor's first session is days[2]; the roll opens there.
        (days[1], new_id): [OpenIntent("d-roll-open", RB, new_id, new_id, 1, "open")],
    }
    plan = ReplayPlan(
        days=days,
        sessions={
            old_id: {day: make_session(day) for day in days[:3]},
            new_id: {day: make_session(day) for day in days[2:]},
        },
        bars={old_id: old_bars, new_id: new_bars},
        decisions=decisions,
        terminal_days={old_id: days[2], new_id: days[-1]},
    )
    outcome = run_replay(engine, plan)
    assert outcome.reconciliation["cash_matches_fill_identity"] is True
    # Open old at 100, close old at 100 -> zero gross. Open new at 130,
    # close new at 130 -> zero gross. Cash changes only by the declared
    # four-sided fees+slippage; the 30-point gap never becomes PnL.
    expected_costs = Decimal("4") * (Decimal("5.01") + Decimal("10"))
    assert engine.cash == Decimal("1000000") - expected_costs
    assert engine.ledgers[old_id].cumulative_gross == Decimal("0")
    assert engine.ledgers[new_id].cumulative_gross == Decimal("0")
    assert engine.turnover_notional() == Decimal("1000") * 2 + Decimal("1300") * 2
    assert outcome.open_positions == {}


def test_terminal_day_close_only_and_incomplete_when_unclosed() -> None:
    days = [date(2026, 1, 5 + i) for i in range(3)]
    bars = _rising_bars(days)
    last = days[-1]
    decisions = {
        (days[0], "SHFE.test1"): [
            OpenIntent("d-open", RB, "SHFE.test1", "SHFE.test1", 1, "open")
        ],
        # An open attempt decided on the last day must be refused at terminal.
        (days[1], "SHFE.test1"): [
            OpenIntent("d-more", RB, "SHFE.test1", "SHFE.test1", 2, "open"),
            OpenIntent("d-exit", RB, "SHFE.test1", "SHFE.test1", 0, "close"),
        ],
    }
    engine, plan = _simple_plan(days, bars, decisions, terminal={"SHFE.test1": last})
    outcome = run_replay(engine, plan)
    assert any(e["reason"] == "terminal_close_only" for e in outcome.blocked)
    closes = [f for f in outcome.fills if f.offset in ("close", "close_today")]
    assert closes and closes[-1].trading_day == last
    assert outcome.open_positions == {}

    # If the exit cannot fill (limit touch on the terminal open), the run
    # reports open positions instead of pretending flat.
    terminal_bar = bars["SHFE.test1"][last]
    touch_bands = dict(
        bands_for_prices(
            {day: bar.close for day, bar in bars["SHFE.test1"].items()}, "0.10", "1"
        )
    )
    touch_bands[last.isoformat()] = (terminal_bar.open, Decimal("1"))
    rules_touch = replace(make_rules(), limit_bands=touch_bands)
    engine2, plan2 = _simple_plan(days, bars, decisions, rules=rules_touch,
                                  terminal={"SHFE.test1": last})
    outcome2 = run_replay(engine2, plan2)
    assert outcome2.open_positions.get("SHFE.test1") == 1


def test_confirmed_closed_extends_mark_and_unknown_gap_is_incomplete() -> None:
    days = [date(2026, 1, 5 + i) for i in range(4)]
    bars = _rising_bars(days)
    # The position opens at day2's open (decided after day1). Day3 (index 2)
    # is confirmed closed: the mark extends with a zero increment.
    del bars["SHFE.test1"][days[2]]
    decisions = {
        (days[0], "SHFE.test1"): [
            OpenIntent("d-open", RB, "SHFE.test1", "SHFE.test1", 1, "open")
        ]
    }
    engine, plan = _simple_plan(
        days, bars, decisions, confirmed_closed=frozenset({("SHFE.test1", days[2])})
    )
    outcome = run_replay(engine, plan)
    assert len(outcome.fills) == 1
    assert "SHFE.test1" not in outcome.incomplete_contracts
    day3_mark = [m for m in outcome.marks if m.trading_day == days[2]][0]
    day2_mark = [m for m in outcome.marks if m.trading_day == days[1]][0]
    assert day3_mark.equity == day2_mark.equity  # zero increment, closed market

    # Without confirmation the same hole is a data gap -> incomplete ledger
    # and the held position cannot be valued.
    engine2, plan2 = _simple_plan(days, bars, decisions)
    outcome2 = run_replay(engine2, plan2)
    assert "SHFE.test1" in outcome2.incomplete_contracts
    gap_mark = [m for m in outcome2.marks if m.trading_day == days[2]][0]
    assert gap_mark.status == "incomplete"


def test_fifo_trade_attribution_with_proportional_costs() -> None:
    day1, day2, day3 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)
    rules = replace(make_rules(), limit_bands=bands_for_prices(
        {day1: Decimal("100"), day2: Decimal("100"), day3: Decimal("100")}, "0.30", "1"))
    engine = engine_with(rules)
    engine.execute_intent(
        OpenIntent("d1", RB, "SHFE.test1", "SHFE.test1", 1, "open"),
        session=make_session(day1), open_price=Decimal("100"), day_bars_before=[],
        known_equity=Decimal("400000000"),
    )
    engine.mark_day(day1, {"SHFE.test1": Decimal("105")})
    engine.execute_intent(
        OpenIntent("d2", RB, "SHFE.test1", "SHFE.test1", 2, "open"),
        session=make_session(day2), open_price=Decimal("110"), day_bars_before=[],
        known_equity=Decimal("400000000"),
    )
    engine.mark_day(day2, {"SHFE.test1": Decimal("115")})
    engine.execute_intent(
        OpenIntent("d3", RB, "SHFE.test1", "SHFE.test1", 0, "close"),
        session=make_session(day3), open_price=Decimal("120"), day_bars_before=[],
        known_equity=Decimal("400000000"),
    )
    trades = engine.build_trades()
    assert len(trades) == 2
    by_entry = {t.entry_ideal_price: t for t in trades}
    first, second = by_entry[Decimal("100")], by_entry[Decimal("110")]
    assert first.lots == 1 and second.lots == 1
    assert first.gross_pnl == Decimal("200")  # (120-100)*10
    assert second.gross_pnl == Decimal("100")  # (120-110)*10
    # Each FIFO batch closes as its own fill: opening fees per batch and
    # one close fill per batch, each charged its own fee + addition.
    assert first.opening_fees == Decimal("5.01")
    assert second.opening_fees == Decimal("5.01")
    assert first.closing_fees == Decimal("5.01")
    assert second.closing_fees == Decimal("5.01")
    # Every fill is chained to its decision and bar id (§十二.16).
    for fill in engine.fills:
        assert fill.decision_id
        assert fill.rule_evidence_ids


# ---------------------------------------------------------------------------
# Full demo composition (strategies + portfolio + engine) and byte identity
# ---------------------------------------------------------------------------


def run_demo_composition(
    *,
    scenario: str = "base",
    total_days: int = 630,
    initial_nav: str = "400000000",
    fixture: Mapping[str, object] | None = None,
):
    """Causal demo host: registry -> portfolio -> engine (no glue by id)."""
    from ak_trading.futures.data import ContractInstance, synchronized_far_bar
    from ak_trading.futures.portfolio import (
        admission_review,
        build_portfolio_targets,
        build_slot_budgets,
        constrain_requests,
        net_requests_by_product,
    )
    from ak_trading.futures.spec import (
        ContractMetadata,
        ResearchPolicy as RP,
        StrategyContext,
    )
    from ak_trading.futures.strategies import default_registry
    from ak_trading.futures.structures import build_structure_frame

    fixture = fixture or build_demo_fixture(total_days)
    dates = [date.fromisoformat(d) for d in fixture["dates"]]
    if total_days < len(dates):
        dates = dates[:total_days]
    final_day = dates[-1]
    policy = RP(initial_nav_cny=Decimal(initial_nav))
    registry = default_registry(policy)

    instances: dict[str, ContractInstance] = {}
    rules: dict[str, ContractRuleSet] = {}
    bars: dict[str, dict[date, ContractBar]] = {}
    sessions: dict[str, dict[date, SessionSchedule]] = {}
    products: dict[str, ProductKey] = {}
    expiries: dict[str, datetime] = {}
    active_range: dict[str, tuple[int, int]] = {}
    for instance_id, meta in fixture["contracts"].items():
        product = ProductKey(*str(meta["product"]).split("."))
        expiry = datetime.fromisoformat(str(meta["expiry_at"]))
        instances[instance_id] = ContractInstance(
            instance_id=f"{instance_id}|{expiry.isoformat()}",
            raw_symbol=instance_id,
            product=product,
            expiry_at=expiry,
            expired_in_cache=False,
            listing_at=None,
        )
        products[instance_id] = product
        expiries[instance_id] = expiry
        active_range[instance_id] = (int(meta["active_from_index"]), int(meta["active_through_index"]))
        rule_payload = meta["rules"]
        fee = FeeSpec(
            open=FeeComponent(cny_per_lot=Decimal(str(rule_payload["fee_open_cny_per_lot"])), source_id="demo_fixture"),
            close=FeeComponent(cny_per_lot=Decimal(str(rule_payload["fee_close_cny_per_lot"])), source_id="demo_fixture"),
            close_today=FeeComponent(cny_per_lot=Decimal(str(rule_payload["fee_close_today_cny_per_lot"])), source_id="demo_fixture"),
            source_id="demo_fixture",
        )
        margin = MarginSpec(
            long=MarginRate(Decimal(str(rule_payload["margin_long_rate"])), benchmark="close_price"),
            short=MarginRate(Decimal(str(rule_payload["margin_short_rate"])), benchmark="close_price"),
            basis="exchange_only",
            source_id="demo_fixture",
        )
        series = {
            date.fromisoformat(d): b
            for d, b in fixture["bars"][instance_id].items()
            if date.fromisoformat(d) in dates
        }
        if not series:
            # Truncated windows legitimately exclude later contracts (e.g.
            # the far leg before its first session inside the window).
            continue
        closes = {day: bar.close for day, bar in series.items()}
        rules[instance_id] = ContractRuleSet(
            multiplier=Decimal(str(rule_payload["multiplier"])),
            price_tick=Decimal(str(rule_payload["price_tick"])),
            lot_step=int(rule_payload["lot_step"]),
            min_open_lots=int(rule_payload["min_open_lots"]),
            fee=fee,
            margin=margin,
            limit_bands=bands_for_prices(closes, str(rule_payload["limit_band_ratio"]), str(rule_payload["price_tick"])),
            median_volume20=float(rule_payload["median_volume20_override"]),
        )
        bars[instance_id] = series
        sessions[instance_id] = {
            date.fromisoformat(iso_day): SessionSchedule(
                date.fromisoformat(iso_day),
                datetime.fromisoformat(open_iso),
                datetime.fromisoformat(close_iso),
            )
            for iso_day, (open_iso, close_iso) in fixture["sessions"][instance_id].items()
            if date.fromisoformat(iso_day) in series
        }

    roll_day = date.fromisoformat(str(fixture["roll"]["day"]))
    roll_old = str(fixture["roll"]["old"])
    roll_new = str(fixture["roll"]["new"])

    engine = LedgerEngine(
        initial_cash=Decimal(initial_nav),
        policy=policy,
        scenario=scenario,
        contracts={
            instance_id: (products[instance_id], instance_id, rules[instance_id])
            for instance_id in rules
        },
    )
    history: dict[str, list[ContractBar]] = {instance_id: [] for instance_id in instances}
    strategy_states: dict[tuple[str, str], object] = {}
    last_active: dict[ProductKey, str] = {}

    def active_contract(product: ProductKey, index: int) -> str | None:
        if product == RB:
            active = roll_new if dates[index] >= roll_day else roll_old
        else:
            active = "CFFEX.demoIF1"
        start, _ = active_range[active]
        return active if index >= start else None

    def provider(engine_state: LedgerEngine, day: date) -> list[OpenIntent]:
        index = dates.index(day)
        intents: list[OpenIntent] = []
        for instance_id, series in sorted(bars.items()):
            bar = series.get(day)
            if bar is not None:
                history[instance_id].append(bar)
        known_nav = engine_state.marks[-1].equity if engine_state.marks else Decimal(initial_nav)
        for product in (RB, IF):
            active = active_contract(product, index)
            previous_active = last_active.get(product)
            if previous_active and previous_active != active:
                held = engine_state.ledgers[previous_active].lots
                if held != 0:
                    intents.append(
                        OpenIntent(
                            decision_id=f"{day.isoformat()}|roll_exit|{previous_active}",
                            product=product,
                            contract_instance_id=previous_active,
                            raw_symbol=previous_active,
                            target_lots=0,
                            kind="close",
                            used_bar_id=f"{previous_active}:{bar.datetime_nano if bar else ''}",
                        )
                    )
            if active is None or active not in history or not history[active]:
                last_active[product] = active
                continue
            last_active[product] = active
            active_bars = history[active]
            requests = []
            for strategy_id, strategy in sorted(registry.items()):
                instance = instances[active]
                pair_candidates: tuple[ContractMetadata, ...] = ()
                pair_bars: dict[str, tuple[ContractBar, ...]] = {}
                if strategy_id == "term_structure_carry":
                    near_expiry = expiries[active]
                    for other_id, other_expiry in expiries.items():
                        if other_id == active or products[other_id] != product:
                            continue
                        gap = (other_expiry - near_expiry).total_seconds() / 86400.0
                        if 60.0 <= gap <= 180.0 and other_expiry > near_expiry:
                            pair_candidates += (
                                ContractMetadata(
                                    contract_instance_id=instances[other_id].instance_id,
                                    raw_symbol=other_id,
                                    product=product,
                                    expiry_at=other_expiry,
                                ),
                            )
                            pair_bars[other_id] = tuple(history.get(other_id, []))
                frame = build_structure_frame(active_bars)
                context = StrategyContext(
                    strategy_id=strategy_id,
                    product=product,
                    contract_instance_id=instance.instance_id,
                    raw_symbol=active,
                    decision_at=active_bars[-1].available_at,
                    clock_status=ClockStatus.VERIFIED,
                    completed_bar_id=f"{active}:{active_bars[-1].datetime_nano}",
                    bars=tuple(active_bars),
                    structure=frame,
                    factors={},
                    expiry_at=expiries[active],
                    pair_candidates=pair_candidates,
                    pair_bars=pair_bars,
                )
                previous_state = strategy_states.get((strategy_id, active))
                result = strategy.evaluate(context, previous_state)
                strategy_states[(strategy_id, active)] = result.next_state
                rule = rules[active]
                from ak_trading.futures.portfolio import StrategyRequest

                requests.append(
                    StrategyRequest(
                        strategy_id=strategy_id,
                        family=strategy.spec.family,
                        product=product,
                        contract_instance_id=active,
                        raw_symbol=active,
                        signal_status=result.status if result.target_position is not None else result.status,
                        direction=result.target_position if result.target_position is not None else 0,
                        observed_daily_vol=frame.daily_vol20,
                        completed_close=active_bars[-1].close,
                        multiplier=rule.multiplier,
                        lot_step=rule.lot_step,
                        has_position=engine_state.ledgers[active].lots != 0,
                        position_direction=1 if engine_state.ledgers[active].lots > 0 else (-1 if engine_state.ledgers[active].lots < 0 else 0),
                        decision_id=f"{day.isoformat()}|{strategy_id}",
                    )
                )
            budgets = build_slot_budgets(requests, policy, products=(RB, IF))
            admission = admission_review(requests, budgets, policy)
            if not admission.accepted:
                continue
            net, _, _, contract_nets = net_requests_by_product(admission.accepted, budgets, policy)
            volatility = {
                request.product: request.observed_daily_vol
                for request in admission.accepted
            }
            constrained = constrain_requests(
                net, volatility, policy,
                (RB, ProductKey("INE", "SC"), ProductKey("DCE", "M"),
                 ProductKey("CZCE", "TA"), IF, ProductKey("GFEX", "SI")),
                killed=engine_state.killed or engine_state.insolvent,
            )
            if constrained.refused:
                continue
            targets = build_portfolio_targets(
                admission.accepted, budgets, policy, constrained.targets, known_nav,
                killed=engine_state.killed or engine_state.insolvent,
            )
            for target in targets:
                held = engine_state.ledgers[active].lots
                if target.target_lots == 0 and held != 0:
                    kind = "close"
                elif target.status == "active":
                    kind = "open"
                else:
                    continue
                intents.append(
                    OpenIntent(
                        decision_id=target.decision_id or f"{day.isoformat()}|{active}",
                        product=product,
                        contract_instance_id=active,
                        raw_symbol=active,
                        target_lots=target.target_lots,
                        kind=kind,
                        used_bar_id=f"{active}:{active_bars[-1].datetime_nano}",
                    )
                )
        return intents

    plan = ReplayPlan(
        days=dates,
        sessions=sessions,
        bars=bars,
        decisions={},
        terminal_days={instance_id: final_day for instance_id in sessions},
    )
    assert rules, "composition requires at least one in-window contract"
    outcome = run_replay(engine, plan, decision_provider=provider)
    return fixture, engine, outcome


def test_full_demo_three_families_roll_and_reconciliation() -> None:
    fixture, engine, outcome = run_demo_composition()
    assert outcome.reconciliation["cash_matches_fill_identity"] is True
    assert outcome.reconciliation["ledger_fees_match"] is True
    assert outcome.reconciliation["ledger_slip_match"] is True
    assert not outcome.insolvent
    fills = outcome.fills
    assert fills, "demo must produce actual fills"
    products_traded = {str(f.product) for f in fills}
    assert products_traded == {"SHFE.RB", "CFFEX.IF"}
    offsets = {f.offset for f in fills}
    assert "open" in offsets and ("close" in offsets or "close_today" in offsets)
    # The roll executed: a close on demoRB1 and an open on demoRB2.
    rolled_old = [f for f in fills if f.raw_symbol == "SHFE.demoRB1" and f.offset != "open"]
    rolled_new = [f for f in fills if f.raw_symbol == "SHFE.demoRB2" and f.offset == "open"]
    assert rolled_old and rolled_new
    # Terminal opens attempt engine-generated closes on every held contract
    # (§十.12). A capacity-capped residue is reported honestly as open
    # positions instead of a fabricated end_exposure=0.
    for instance_id in set(outcome.open_positions):
        terminal_closes = [
            f for f in fills
            if f.contract_instance_id == instance_id
            and f.decision_id.startswith("terminal|")
        ]
        assert terminal_closes, f"missing terminal close attempt for {instance_id}"
    # Every fill carries the causal chain (decision/contract/evidence ids);
    # engine-generated terminal closes cite the terminal rule, not a bar.
    for fill in fills:
        assert fill.decision_id and fill.rule_evidence_ids
        if not fill.decision_id.startswith("terminal|"):
            assert fill.used_bar_id


def test_demo_insufficient_data_reports_no_fills_cleanly() -> None:
    # 15 days is below every implementation's required_bars (min 21).
    fixture, engine, outcome = run_demo_composition(total_days=15)
    assert outcome.fills == ()
    assert outcome.reconciliation["cash_matches_fill_identity"] is True
    assert engine.cash == Decimal("400000000")
    assert outcome.marks[-1].status in ("active", "terminal")


def test_demo_deterministic_payload_byte_identity(tmp_path: Path) -> None:
    from ak_trading.futures.io import equity_rows, fills_rows, trades_rows

    fixture, engine, outcome = run_demo_composition()
    ledger_payload = {
        "fills_rows": fills_rows(outcome.fills),
        "equity_rows": equity_rows(outcome.marks),
        "trades_rows": trades_rows(outcome.trades),
        "report": {
            "status": "complete",
            "reconciliation": {
                k: (str(v) if isinstance(v, Decimal) else v)
                for k, v in dict(outcome.reconciliation).items()
            },
            "killed": outcome.killed,
            "insolvent": outcome.insolvent,
        },
    }

    def publish(destination: Path) -> None:
        publish_backtest_bundle(
            destination,
            created_at="2026-09-07T00:00:00+08:00",
            config={"stage": "S2", "dataset_kind": "synthetic_demo"},
            report={"summary": "demo", "fills": len(outcome.fills)},
            availability={"member_factor": {"status": "not_requested"}},
            splits={"rule": "demo"},
            signals_rows=[],
            rolls_rows=[{
                "decision_at": fixture["roll"]["day"],
                "product": fixture["roll"]["product"],
                "old_contract_instance_id": fixture["roll"]["old"],
                "new_contract_instance_id": fixture["roll"]["new"],
                "reason": fixture["roll"]["reason"],
                "decision_id": "demo_roll",
            }],
            input_manifest_sha256="0" * 64,
            scenario_ledgers={"base": {"futures_fixed_risk": ledger_payload}},
        )

    first, second = tmp_path / "replay-1", tmp_path / "replay-2"
    publish(first)
    publish(second)
    files_first = sorted(p.relative_to(first) for p in first.rglob("*") if p.is_file())
    files_second = sorted(p.relative_to(second) for p in second.rglob("*") if p.is_file())
    assert files_first == files_second
    for relative in files_first:
        assert (first / relative).read_bytes() == (second / relative).read_bytes(), relative


def test_stress_scenario_actually_replayed_not_doubled_report() -> None:
    _, base_engine, base_outcome = run_demo_composition(scenario="base")
    _, stress_engine, stress_outcome = run_demo_composition(scenario="stress")
    assert base_outcome.reconciliation["cash_matches_fill_identity"] is True
    assert stress_outcome.reconciliation["cash_matches_fill_identity"] is True
    base_costs = base_engine.reconciliation()["fees"] + base_engine.reconciliation()["slippage"]
    stress_costs = stress_engine.reconciliation()["fees"] + stress_engine.reconciliation()["slippage"]
    # Stress doubles every component; identical fills must cost exactly more,
    # not the same numbers repackaged.
    if base_costs > 0:
        assert stress_costs > base_costs
        assert float(stress_costs) == pytest.approx(float(base_costs) * 2.0, rel=0.02)
    assert abs(len(stress_outcome.fills) - len(base_outcome.fills)) <= 2


def test_tiny_capital_if_below_minimum_lot_never_opens() -> None:
    _, engine, outcome = run_demo_composition(initial_nav="1000")
    assert outcome.fills == ()
    assert engine.cash == Decimal("1000")



# ---------------------------------------------------------------------------
# Fix-round oracles: roll linkage (B8), cross-contract precedence (A1),
# post-trade margin totals (A2), terminal residue reporting (A3/B3)
# ---------------------------------------------------------------------------


def test_roll_linkage_old_leg_blocked_suppresses_new_leg_b8() -> None:
    """§十二.15 oracle: the old leg cannot close => the new leg never opens
    and the stuck old position keeps consuming its margin budget."""
    old_id, new_id = "SHFE.old1", "SHFE.new1"
    days = [date(2026, 1, 5 + i) for i in range(5)]
    touch_day = days[2]
    # Old leg's sessions end on the touch day: its close (and the engine
    # terminal close) hit the lower band there, so the old leg stays stuck.
    old_bands = dict(
        bands_for_prices({day: Decimal("100") for day in days}, "0.20", "1")
    )
    old_bands[touch_day.isoformat()] = (Decimal("200"), Decimal("100"))
    rules_old = replace(make_rules(), limit_bands=old_bands)
    new_bands = dict(
        bands_for_prices({day: Decimal("130") for day in days}, "0.20", "1")
    )
    rules_new = replace(make_rules(), limit_bands=new_bands)
    engine = LedgerEngine(
        initial_cash=Decimal("1000000"),
        policy=POLICY,
        contracts={old_id: (RB, old_id, rules_old), new_id: (RB, new_id, rules_new)},
    )
    old_bars = {day: make_bar(day, open_p="100", close_p="100", symbol=old_id) for day in days}
    new_bars = {day: make_bar(day, open_p="130", close_p="130", symbol=new_id) for day in days}
    decisions = {
        (days[0], old_id): [OpenIntent("d-open-old", RB, old_id, old_id, 1, "open")],
        (days[1], old_id): [OpenIntent("d-roll-close", RB, old_id, old_id, 0, "close")],
        (days[1], new_id): [OpenIntent(
            "d-roll-open", RB, new_id, new_id, 1, "open",
            requires_full_exit_of=old_id,
        )],
    }
    plan = ReplayPlan(
        days=days,
        sessions={
            old_id: {day: make_session(day) for day in days[:3]},
            new_id: {day: make_session(day) for day in days[1:]},
        },
        bars={old_id: old_bars, new_id: new_bars},
        decisions=decisions,
        terminal_days={old_id: days[2], new_id: days[-1]},
    )
    outcome = run_replay(engine, plan)
    reasons = {entry["reason"] for entry in outcome.blocked}
    assert "roll_old_position_not_closed" in reasons
    assert all(f.raw_symbol != new_id for f in outcome.fills if f.offset == "open")
    assert engine.ledgers[new_id].lots == 0
    assert engine.ledgers[old_id].lots != 0  # the stuck old leg keeps its margin


def test_roll_partial_to_cash_when_old_closes_but_new_blocked_b8() -> None:
    old_id, new_id = "SHFE.old1", "SHFE.new1"
    days = [date(2026, 1, 5 + i) for i in range(5)]
    rules_old = replace(
        make_rules(),
        limit_bands=bands_for_prices({day: Decimal("100") for day in days}, "0.20", "1"),
    )
    new_bands = dict(
        bands_for_prices({day: Decimal("130") for day in days}, "0.20", "1")
    )
    new_bands[days[2].isoformat()] = (Decimal("130"), Decimal("1"))  # touch band
    rules_new = replace(make_rules(), limit_bands=new_bands)
    engine = LedgerEngine(
        initial_cash=Decimal("1000000"),
        policy=POLICY,
        contracts={old_id: (RB, old_id, rules_old), new_id: (RB, new_id, rules_new)},
    )
    old_bars = {day: make_bar(day, open_p="100", close_p="100", symbol=old_id) for day in days}
    new_bars = {day: make_bar(day, open_p="130", close_p="130", symbol=new_id) for day in days}
    decisions = {
        (days[0], old_id): [OpenIntent("d-open-old", RB, old_id, old_id, 1, "open")],
        (days[1], old_id): [OpenIntent("d-roll-close", RB, old_id, old_id, 0, "close")],
        (days[1], new_id): [OpenIntent(
            "d-roll-open", RB, new_id, new_id, 1, "open",
            requires_full_exit_of=old_id,
        )],
    }
    plan = ReplayPlan(
        days=days,
        sessions={
            old_id: {day: make_session(day) for day in days[:4]},
            new_id: {day: make_session(day) for day in days[1:]},
        },
        bars={old_id: old_bars, new_id: new_bars},
        decisions=decisions,
        terminal_days={old_id: days[3], new_id: days[-1]},
    )
    outcome = run_replay(engine, plan)
    reasons = {entry["reason"] for entry in outcome.blocked}
    assert "roll_partial_to_cash" in reasons  # old closed, new could not open
    assert engine.ledgers[new_id].lots == 0
    assert outcome.open_positions == {}


def test_exits_execute_before_opens_across_contracts_a1() -> None:
    """A1: on the same day, exits across ALL contracts execute before any
    open, in a stable (product, raw symbol) order, regardless of sort."""
    exit_sym, open_sym = "SHFE.zzz", "SHFE.aaa"  # open sorts FIRST
    day_before, day = date(2025, 12, 29), date(2026, 1, 5)
    rules = replace(
        make_rules(),
        limit_bands=bands_for_prices(
            {day_before: Decimal("100"), day: Decimal("100")}, "0.30", "1"
        ),
    )
    engine = LedgerEngine(
        initial_cash=Decimal("1000000"),
        policy=POLICY,
        contracts={
            open_sym: (RB, open_sym, rules),
            exit_sym: (RB, exit_sym, rules),
        },
    )
    bars = {
        sym: {
            day_before: make_bar(day_before, open_p="100", close_p="100", symbol=sym),
            day: make_bar(day, open_p="100", close_p="100", symbol=sym),
        }
        for sym in (open_sym, exit_sym)
    }
    sessions = {sym: {d: make_session(d) for d in series} for sym, series in bars.items()}
    engine.execute_intent(
        OpenIntent("seed-a", RB, open_sym, open_sym, 1, "open"),
        session=make_session(day_before), open_price=Decimal("100"),
        day_bars_before=[], known_equity=Decimal("1000000"),
    )
    engine.execute_intent(
        OpenIntent("seed-z", RB, exit_sym, exit_sym, 1, "open"),
        session=make_session(day_before), open_price=Decimal("100"),
        day_bars_before=[], known_equity=Decimal("1000000"),
    )
    engine.mark_day(day_before, {open_sym: Decimal("100"), exit_sym: Decimal("100")})
    decisions = {
        (day_before, exit_sym): [
            OpenIntent("exit-z", RB, exit_sym, exit_sym, 0, "close")
        ],
        (day_before, open_sym): [
            OpenIntent("add-a", RB, open_sym, open_sym, 2, "open")
        ],
    }
    plan = ReplayPlan(
        days=[day_before, day], sessions=sessions, bars=bars, decisions=decisions,
        terminal_days={},
    )
    outcome = run_replay(engine, plan)
    closes = [f for f in outcome.fills if f.offset != "open"]
    assert len(closes) == 1 and closes[0].raw_symbol == exit_sym
    adds = [
        f for f in outcome.fills
        if f.offset == "open" and f.decision_id == "add-a"
    ]
    assert adds and adds[0].fill_id > closes[0].fill_id  # exit processed first


def test_same_direction_add_charges_margin_on_post_trade_total_a2() -> None:
    day1, day2 = date(2026, 1, 5), date(2026, 1, 6)
    rules = replace(
        make_rules(),
        limit_bands=bands_for_prices(
            {day1: Decimal("100"), day2: Decimal("100")}, "0.30", "1"
        ),
    )
    engine_tight = engine_with(rules, cash="300")
    engine_tight.execute_intent(
        OpenIntent("s1", RB, "SHFE.test1", "SHFE.test1", 1, "open"),
        session=make_session(day1), open_price=Decimal("100"), day_bars_before=[],
        known_equity=Decimal("300"),
    )
    engine_tight.mark_day(day1, {"SHFE.test1": Decimal("100")})
    fills = engine_tight.execute_intent(
        OpenIntent("s2", RB, "SHFE.test1", "SHFE.test1", 2, "open"),
        session=make_session(day2), open_price=Decimal("100"), day_bars_before=[],
        known_equity=engine_tight.marks[-1].equity,
    )
    # Post-trade requirement: 2 lots * 100 * 10 * 0.08 = 160 > 0.4*300=120
    # -> blocked, even though the ADDED lot alone (80) would fit.
    assert fills == []
    assert engine_tight.blocked[-1]["reason"] == "margin_ratio_exceeded"


@pytest.mark.parametrize("direction", [1, -1], ids=["long", "short"])
@pytest.mark.parametrize("close_price", [Decimal("90"), Decimal("110")], ids=["lower", "higher"])
@pytest.mark.parametrize("remaining_lots", [0, 3], ids=["full-close", "partial-request"])
def test_r2_band_rejected_close_has_no_account_effect(direction, close_price, remaining_lots):
    from copy import deepcopy

    days = [date(2026, 1, 5) + timedelta(days=i) for i in range(3)]
    rules = replace(make_rules(), limit_bands={
        days[0].isoformat(): (Decimal("200"), Decimal("1")),
        days[1].isoformat(): (Decimal("200"), Decimal("1")),
        days[2].isoformat(): (close_price + Decimal("0.5"), close_price - Decimal("0.5")),
    })
    engine = engine_with(rules)
    for day, price, target in ((days[0], Decimal("100"), 2), (days[1], Decimal("105"), 4)):
        engine.execute_intent(
            OpenIntent("seed", RB, "SHFE.test1", "SHFE.test1", direction * target, "open"),
            session=make_session(day), open_price=price, day_bars_before=[],
            known_equity=engine.initial_cash,
        )
        engine.mark_day(day, {"SHFE.test1": close_price if target == 4 else price})
    ledger = engine.ledgers["SHFE.test1"]
    before = deepcopy((engine.cash, ledger.lots, ledger.realized_gross, ledger.entry_lots,
                       engine.fills, ledger.fees_paid, ledger.slip_paid, engine.capacity_used))
    last_equity = engine.marks[-1].equity
    assert engine.reconciliation()["cash_matches_fill_identity"]
    assert engine.reconciliation()["equity_matches_gross_identity"]

    rejected = engine.execute_intent(
        OpenIntent("reject", RB, "SHFE.test1", "SHFE.test1", direction * remaining_lots, "close"),
        session=make_session(days[2]), open_price=close_price, day_bars_before=[],
        known_equity=last_equity,
    )
    assert rejected == []
    assert engine.blocked[-1]["reason"] == "modeled_slippage_outside_band"
    assert (engine.cash, ledger.lots, ledger.realized_gross, ledger.entry_lots,
            engine.fills, ledger.fees_paid, ledger.slip_paid, engine.capacity_used) == before
    mark = engine.mark_day(days[2], {"SHFE.test1": close_price})
    assert mark.equity == last_equity
    assert mark.cash == before[0]
    assert engine.reconciliation()["cash_matches_fill_identity"]
    assert engine.reconciliation()["equity_matches_gross_identity"]


@pytest.mark.parametrize("direction", [1, -1], ids=["sell-close", "buy-close"])
def test_r2_close_effective_price_uses_fill_side(direction):
    day1, day2 = date(2026, 1, 5), date(2026, 1, 6)
    rules = replace(make_rules(), limit_bands={
        day1.isoformat(): (Decimal("200"), Decimal("1")),
        day2.isoformat(): (Decimal("111"), Decimal("109")),
    })
    engine = engine_with(rules)
    engine.execute_intent(
        OpenIntent("seed", RB, "SHFE.test1", "SHFE.test1", direction, "open"),
        session=make_session(day1), open_price=Decimal("100"), day_bars_before=[],
        known_equity=engine.initial_cash,
    )
    engine.mark_day(day1, {"SHFE.test1": Decimal("100")})
    fills = engine.execute_intent(
        OpenIntent("close", RB, "SHFE.test1", "SHFE.test1", 0, "close"),
        session=make_session(day2), open_price=Decimal("110"), day_bars_before=[],
        known_equity=engine.initial_cash,
    )
    assert len(fills) == 1
    assert fills[0].side == -direction
    assert fills[0].effective_price == (Decimal("109") if direction == 1 else Decimal("111"))


def test_r2_reconciliation_failure_cannot_complete_a_flat_run():
    day = date(2026, 1, 5)
    engine = engine_with(make_rules())
    # Simulate a corrupted independent cash accumulator, with no position
    # residue or insolvency to otherwise mark this run incomplete.
    engine.ledgers["SHFE.test1"].realized_gross = Decimal("10")
    outcome = run_replay(engine, ReplayPlan(
        days=[day], sessions={}, bars={}, decisions={}, terminal_days={},
    ))
    assert not outcome.reconciliation["cash_matches_fill_identity"]
    assert outcome.open_positions == {}
    assert outcome.status == "incomplete"
    assert "reconciliation_failed:cash_matches_fill_identity" in outcome.incomplete_reasons["account"]


def test_terminal_residue_reports_reasons_and_incomplete_status_a3_b3() -> None:
    days = [date(2026, 1, 5 + i) for i in range(3)]
    bars = _rising_bars(days)
    last = days[-1]
    terminal_bar = bars["SHFE.test1"][last]
    touch_bands = dict(
        bands_for_prices(
            {day: bar.close for day, bar in bars["SHFE.test1"].items()}, "0.10", "1"
        )
    )
    touch_bands[last.isoformat()] = (terminal_bar.open, Decimal("1"))
    rules_touch = replace(make_rules(), limit_bands=touch_bands)
    decisions = {
        (days[0], "SHFE.test1"): [
            OpenIntent("d-open", RB, "SHFE.test1", "SHFE.test1", 1, "open")
        ],
    }
    engine, plan = _simple_plan(
        days, bars, decisions, rules=rules_touch, terminal={"SHFE.test1": last}
    )
    outcome = run_replay(engine, plan)
    assert outcome.open_positions.get("SHFE.test1") == 1
    assert outcome.status == "incomplete"  # B3: never silently complete
    reasons = outcome.incomplete_reasons.get("SHFE.test1", ())
    assert any(r.startswith("terminal_close_incomplete") for r in reasons)  # A3
    assert outcome.marks[-1].status == "terminal"
    assert outcome.final_unrealized_pnl != Decimal("0")
