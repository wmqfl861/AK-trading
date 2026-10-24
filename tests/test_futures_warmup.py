"""Offline replay warmup oracles; no CLI, database or settings imports."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest

from ak_trading.futures.backtest import (
    ContractRuleSet,
    EquityMark,
    LedgerEngine,
    Lot,
    OpenIntent,
    ReplayPlan,
    SessionSchedule,
    run_replay,
)
from ak_trading.futures.spec import (
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
PRODUCT = ProductKey("SHFE", "RB")
INSTANCE = "SHFE.warmup1"
START = date(2026, 1, 1)
CASH = Decimal("1000000")


def session(day: date) -> SessionSchedule:
    return SessionSchedule(
        day,
        datetime.combine(day, time(9), SH),
        datetime.combine(day, time(15), SH),
    )


def bar(day: date, *, volume: str = "5000", price: str = "100") -> ContractBar:
    clock = session(day)
    return ContractBar(
        symbol=INSTANCE,
        duration_seconds=86400,
        datetime_nano=int(clock.session_close.timestamp()) * 1_000_000_000,
        ts=clock.session_close,
        source="warmup_unit_fixture",
        trading_day=day,
        trading_day_source="warmup_unit_fixture",
        session_open=clock.session_open,
        session_close=clock.session_close,
        available_at=clock.session_close,
        open=Decimal(price),
        high=Decimal(price) + 1,
        low=Decimal(price) - 1,
        close=Decimal(price),
        volume=Decimal(volume),
    )


def intent(day: date, *, lots: int = 1, kind: str = "open", label: str = "signal") -> OpenIntent:
    return OpenIntent(
        decision_id=f"{day}|{label}",
        product=PRODUCT,
        contract_instance_id=INSTANCE,
        raw_symbol=INSTANCE,
        target_lots=lots,
        kind=kind,
        used_bar_id=f"{INSTANCE}:{bar(day).datetime_nano}",
    )


def fixture(*, warm_count: int = 20, score_count: int = 3, capacity: float | None = None):
    warm = tuple(START + timedelta(days=i) for i in range(warm_count))
    scored = tuple(START + timedelta(days=warm_count + i) for i in range(score_count))
    series = {day: bar(day) for day in warm + scored}
    fee = FeeComponent(cny_per_lot=Decimal("5"), source_id="warmup_unit_fixture")
    rules = ContractRuleSet(
        multiplier=Decimal("10"),
        price_tick=Decimal("1"),
        fee=FeeSpec(open=fee, close=fee, close_today=fee, source_id="warmup_unit_fixture"),
        margin=MarginSpec(long=MarginRate(Decimal("0.08")), short=MarginRate(Decimal("0.08"))),
        limit_bands={day.isoformat(): (Decimal("1000"), Decimal("1")) for day in scored},
        median_volume20=capacity,
    )
    engine = LedgerEngine(
        initial_cash=CASH,
        policy=ResearchPolicy(),
        contracts={INSTANCE: (PRODUCT, INSTANCE, rules)},
    )
    plan = ReplayPlan(
        days=scored,
        sessions={INSTANCE: {day: session(day) for day in warm + scored}},
        bars={INSTANCE: series},
        decisions={},
        terminal_days={},
    )
    return engine, plan, warm


def final_decision(plan: ReplayPlan, warm: tuple[date, ...], **kwargs) -> ReplayPlan:
    return replace(plan, decisions={(warm[-1], INSTANCE): [intent(warm[-1], **kwargs)]})


def test_provider_prewarms_then_first_scored_open_trades_without_training_account():
    engine, plan, warm = fixture(score_count=2)
    before = deepcopy(vars(engine))
    seen = []
    history = []

    def provider(state, day):
        seen.append(day)
        history.append(plan.bars[INSTANCE][day])
        if day in warm:
            assert vars(state) == before
            return [intent(day)]
        assert len(state.marks) == plan.days.index(day) + 1
        return []

    plan = replace(plan, terminal_days={INSTANCE: plan.days[-1]})
    outcome = run_replay(engine, plan, decision_provider=provider, warmup_days=warm)

    assert seen == list(warm + tuple(plan.days))
    assert [b.trading_day for b in history] == seen
    assert [fill.trading_day for fill in outcome.fills] == list(plan.days)
    opening, closing = outcome.fills
    assert opening.decision_id == intent(warm[-1]).decision_id
    assert opening.event_time == session(plan.days[0]).session_open
    assert opening.event_time > history[len(warm) - 1].available_at
    assert opening.offset == "open" and closing.offset == "close"
    assert [mark.trading_day for mark in outcome.marks] == list(plan.days)
    assert outcome.reconciliation["fees"] == Decimal("10.02")
    assert outcome.reconciliation["slippage"] == Decimal("20")
    assert outcome.reconciliation["cumulative_gross"] == 0
    assert outcome.marks[-1].cash == CASH - Decimal("30.02")
    assert outcome.marks[-1].equity == outcome.marks[-1].cash
    assert outcome.status == "complete"
    assert outcome.blocked == ()


@pytest.mark.parametrize("direction", [1, -1])
def test_first_scored_entry_has_no_training_pnl_in_the_cash_or_equity_identity(direction):
    engine, plan, warm = fixture(score_count=2)
    first, second = plan.days
    plan.bars[INSTANCE][warm[-1]] = bar(warm[-1], price="800")
    plan.bars[INSTANCE][first] = replace(bar(first, price="100"), close=Decimal("110"))
    plan.bars[INSTANCE][second] = bar(second, price="120")
    plan = replace(final_decision(plan, warm, lots=direction), terminal_days={INSTANCE: second})
    outcome = run_replay(engine, plan, warmup_days=warm)
    assert outcome.marks[0].cash == CASH - Decimal("15.01")
    assert outcome.marks[0].equity == CASH + direction * Decimal("100") - Decimal("15.01")
    assert outcome.trades[0].gross_pnl == direction * Decimal("200")
    assert outcome.reconciliation["cumulative_gross"] == direction * Decimal("200")
    assert outcome.reconciliation["cash_matches_fill_identity"] is True
    assert outcome.reconciliation["equity_matches_gross_identity"] is True
    assert outcome.status == "complete"


def test_earlier_warmup_intents_are_discarded_even_if_final_day_returns_none():
    engine, plan, warm = fixture()
    plan = replace(plan, decisions={(warm[0], INSTANCE): [intent(warm[0], label="frozen")]})

    def provider(state, day):
        return [intent(day)] if day in warm[:-1] else []

    outcome = run_replay(engine, plan, decision_provider=provider, warmup_days=warm)
    assert outcome.fills == ()
    assert outcome.blocked == ()
    assert all(mark.cash == mark.equity == CASH for mark in outcome.marks)
    assert len(outcome.marks) == len(plan.days)
    assert outcome.reconciliation["fees"] == outcome.reconciliation["slippage"] == 0


@pytest.mark.parametrize("warm_count, expected", [(19, 0), (20, 5), (21, 5)])
def test_initial_capacity_uses_twenty_prior_completed_volumes(warm_count, expected):
    engine, plan, warm = fixture(warm_count=warm_count, score_count=1)
    if warm_count == 21:
        plan.bars[INSTANCE][warm[0]] = bar(warm[0], volume="1000000")
    plan.bars[INSTANCE][plan.days[0]] = bar(plan.days[0], volume="9000000")
    outcome = run_replay(engine, final_decision(plan, warm, lots=50), warmup_days=warm)
    assert sum(fill.lots for fill in outcome.fills) == expected
    if not expected:
        assert [row["reason"] for row in outcome.blocked] == ["capacity_unknown"]
    else:
        assert outcome.fills[0].trading_day == plan.days[0]
        assert engine.capacity_used == {(INSTANCE, plan.days[0]): expected}


@pytest.mark.parametrize("availability", ["early", "equal", "late", "unknown", "naive", "pre_close"])
def test_extra_prehistory_capacity_requires_publication_before_first_scored_open(availability):
    engine, plan, warm = fixture(warm_count=19, score_count=1)
    old_day = warm[0] - timedelta(days=1)
    old_bar = bar(old_day)
    first_open = session(plan.days[0]).session_open
    publication = {
        "early": first_open - timedelta(microseconds=1),
        "equal": first_open,
        "late": first_open + timedelta(microseconds=1),
        "unknown": None,
        "naive": first_open.replace(tzinfo=None),
        "pre_close": old_bar.session_close - timedelta(seconds=1),
    }[availability]
    plan.bars[INSTANCE][old_day] = replace(old_bar, available_at=publication)
    outcome = run_replay(engine, final_decision(plan, warm), warmup_days=warm)
    assert len(outcome.fills) == (1 if availability == "early" else 0)
    if availability != "early":
        assert [row["reason"] for row in outcome.blocked] == ["capacity_unknown"]


@pytest.mark.parametrize("dates", ["equal", "future", "duplicate", "reverse", "overlap"])
def test_invalid_warmup_days_fail_before_callbacks_or_account_changes(dates):
    engine, plan, warm = fixture(warm_count=2)
    invalid = {
        "equal": (plan.days[0],),
        "future": (plan.days[-1] + timedelta(days=1),),
        "duplicate": (warm[0], warm[0]),
        "reverse": tuple(reversed(warm)),
        "overlap": (warm[0], plan.days[0]),
    }[dates]
    before = deepcopy(vars(engine))
    calls = []
    with pytest.raises(ValueError, match="warmup_days"):
        run_replay(engine, plan, warmup_days=invalid, decision_provider=lambda state, day: calls.append(day) or [])
    assert calls == []
    assert vars(engine) == before


@pytest.mark.parametrize("scored", ["duplicate", "reverse"])
@pytest.mark.parametrize("with_warmup", [False, True])
def test_scored_days_must_always_be_ordered_and_unique(scored, with_warmup):
    engine, plan, warm = fixture(warm_count=1)
    days = (plan.days[0], plan.days[0]) if scored == "duplicate" else tuple(reversed(plan.days))
    with pytest.raises(ValueError, match=r"scored plan.days"):
        run_replay(engine, replace(plan, days=days), warmup_days=warm if with_warmup else ())
    assert engine.marks == []


def test_empty_scored_smoke_remains_valid_without_warmup():
    engine, plan, warm = fixture(warm_count=1)
    plan = replace(plan, days=(), bars={}, sessions={})
    outcome = run_replay(engine, plan)
    assert outcome.status == "complete"
    assert outcome.fills == outcome.marks == ()
    with pytest.raises(ValueError, match="nonempty scored"):
        run_replay(engine, plan, warmup_days=warm)


def test_default_does_not_seed_prehistory_or_pre_scored_decisions():
    engine, plan, warm = fixture(score_count=2)
    decisions = {
        (warm[-1], INSTANCE): [intent(warm[-1])],
        (plan.days[0], INSTANCE): [intent(plan.days[0])],
    }
    outcome = run_replay(engine, replace(plan, decisions=decisions))
    assert outcome.fills == ()
    assert [row["reason"] for row in outcome.blocked] == ["capacity_unknown"]


@pytest.mark.parametrize("residual", [
    "cash", "peak_equity", "fills", "marks", "killed", "insolvent", "blocked",
    "capacity_used", "fill_seq", "roll_fill_ids", "lots", "entry_lots",
    "cumulative_gross", "realized_gross", "fees_paid", "slip_paid", "last_mark_close",
    "last_mark_day", "today_open", "lots_before_open", "incomplete_reasons",
])
def test_warmup_requires_a_fresh_engine_even_when_flat(residual):
    engine, plan, warm = fixture(warm_count=1)
    ledger = engine.ledgers[INSTANCE]
    mutations = {
        "cash": (engine, "cash", CASH + 1),
        "peak_equity": (engine, "peak_equity", CASH + 1),
        "fills": (engine, "fills", [Fill(
            "prior", "prior", PRODUCT, INSTANCE, INSTANCE, session(warm[0]).session_open,
            warm[0], 1, "open", 1, Decimal("100"), Decimal("101"), Decimal("10"),
        )]),
        "marks": (engine, "marks", [EquityMark(warm[0], CASH, CASH, Decimal(0), "active")]),
        "killed": (engine, "killed", True),
        "insolvent": (engine, "insolvent", True),
        "blocked": (engine, "blocked", [{"reason": "prior_attempt"}]),
        "capacity_used": (engine, "capacity_used", {(INSTANCE, warm[0]): 1}),
        "fill_seq": (engine, "_fill_seq", 1),
        "roll_fill_ids": (engine, "roll_fill_ids", {"prior"}),
        "lots": (ledger, "lots", 1),
        "entry_lots": (ledger, "entry_lots", [Lot(1, warm[0], Decimal("100"), Decimal(0), Decimal(0))]),
        "cumulative_gross": (ledger, "cumulative_gross", Decimal(1)),
        "realized_gross": (ledger, "realized_gross", Decimal(1)),
        "fees_paid": (ledger, "fees_paid", Decimal(1)),
        "slip_paid": (ledger, "slip_paid", Decimal(1)),
        "last_mark_close": (ledger, "last_mark_close", Decimal("100")),
        "last_mark_day": (ledger, "last_mark_day", warm[0]),
        "today_open": (ledger, "today_open", Decimal("100")),
        "lots_before_open": (ledger, "lots_before_open", 0),
        "incomplete_reasons": (ledger, "incomplete_reasons", ["prior_failure"]),
    }
    obj, field, value = mutations[residual]
    setattr(obj, field, value)
    before = deepcopy(vars(engine))
    calls = []
    with pytest.raises(ValueError, match="fresh engine"):
        run_replay(engine, plan, warmup_days=warm, decision_provider=lambda state, day: calls.append(day) or [])
    assert calls == []
    assert vars(engine) == before


@pytest.mark.parametrize("mutation", ["mark", "fill", "cash", "capital", "position"])
def test_provider_may_update_closure_but_must_not_mutate_the_account(mutation):
    engine, plan, warm = fixture(warm_count=2, capacity=50000)
    engine.rules[INSTANCE] = replace(engine.rules[INSTANCE], limit_bands={
        **engine.rules[INSTANCE].limit_bands,
        warm[0].isoformat(): (Decimal("1000"), Decimal("1")),
    })
    calls = []

    def provider(state, day):
        calls.append(day)
        if mutation == "mark":
            state.mark_day(day, {INSTANCE: Decimal("100")})
        elif mutation == "fill":
            state.execute_intent(intent(day), session=session(day), open_price=Decimal("100"),
                                 day_bars_before=[], known_equity=CASH)
        elif mutation == "cash":
            state.cash += 1
        elif mutation == "capital":
            state.initial_cash += 1
            state.cash += 1
            state.peak_equity += 1
        else:
            state.ledgers[INSTANCE].lots = 1
        return []

    with pytest.raises(ValueError, match="warmup"):
        run_replay(engine, plan, warmup_days=warm, decision_provider=provider)
    assert calls == [warm[0]]


@pytest.mark.parametrize("clock", ["missing", "naive", "pre_close", "equal", "late", "missing_close", "wrong_day"])
def test_invalid_warmup_bar_clocks_raise_before_any_callback(clock):
    engine, plan, warm = fixture(warm_count=2)
    old = plan.bars[INSTANCE][warm[-1]]
    first_open = session(plan.days[0]).session_open
    changes = {
        "missing": {"available_at": None},
        "naive": {"available_at": old.available_at.replace(tzinfo=None)},
        "pre_close": {"available_at": old.session_close - timedelta(seconds=1)},
        "equal": {"available_at": first_open},
        "late": {"available_at": first_open + timedelta(seconds=1)},
        "missing_close": {"session_close": None},
        "wrong_day": {"trading_day": plan.days[0]},
    }[clock]
    plan.bars[INSTANCE][warm[-1]] = replace(old, **changes)
    calls = []
    with pytest.raises(ValueError, match="warmup bar"):
        run_replay(engine, plan, warmup_days=warm, decision_provider=lambda state, day: calls.append(day) or [])
    assert calls == []
    assert engine.marks == engine.fills == []
    assert engine.cash == CASH


def test_delayed_earlier_warmup_bar_cannot_reorder_callback_clock():
    engine, plan, warm = fixture(warm_count=2)
    plan.bars[INSTANCE][warm[0]] = replace(
        plan.bars[INSTANCE][warm[0]],
        available_at=plan.bars[INSTANCE][warm[1]].available_at + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="callback day order"):
        run_replay(engine, plan, warmup_days=warm)
    assert engine.marks == engine.fills == []


@pytest.mark.parametrize("delay", [timedelta(), timedelta(seconds=1)])
@pytest.mark.parametrize("source", ["frozen", "provider"])
def test_scored_decision_waits_until_strictly_after_publication(delay, source):
    engine, plan, warm = fixture(warm_count=1, capacity=50000, score_count=3)
    first, second, third = plan.days
    publication = session(second).session_open + delay
    plan.bars[INSTANCE][first] = replace(plan.bars[INSTANCE][first], available_at=publication)
    signal = intent(first)
    provider = None
    if source == "frozen":
        plan = replace(plan, decisions={(first, INSTANCE): [signal]})
    else:
        provider = lambda state, day: [signal] if day == first else []
    outcome = run_replay(engine, plan, warmup_days=warm, decision_provider=provider)
    assert len(outcome.fills) == 1
    assert outcome.fills[0].trading_day == third
    assert outcome.fills[0].event_time > publication
    assert [row["reason"] for row in outcome.blocked] == ["decision_not_available_before_open"]


def test_later_signal_preserves_unpublished_history_dependency():
    engine, plan, warm = fixture(warm_count=1, score_count=4, capacity=50000)
    first, second, third, fourth = plan.days
    delayed = session(third).session_open + timedelta(seconds=1)
    plan.bars[INSTANCE][first] = replace(plan.bars[INSTANCE][first], available_at=delayed)
    seen = []

    def provider(state, day):
        seen.append(plan.bars[INSTANCE][day].close)
        return [intent(day, lots=1 if sum(seen) > 0 else -1)] if day == second else []

    result = run_replay(engine, plan, warmup_days=warm, decision_provider=provider)
    assert len(result.fills) == 1
    assert result.fills[0].trading_day == fourth
    assert result.fills[0].event_time > delayed


def test_final_warmup_decision_citing_future_bar_cannot_execute_at_first_open():
    engine, plan, warm = fixture(warm_count=1, capacity=50000)
    signal = replace(intent(warm[-1]), used_bar_id=intent(plan.days[0]).used_bar_id)
    plan = replace(plan, decisions={(warm[-1], INSTANCE): [signal]})
    outcome = run_replay(engine, plan, warmup_days=warm)
    assert len(outcome.fills) == 1
    assert outcome.fills[0].trading_day == plan.days[1]
    assert outcome.fills[0].event_time > plan.bars[INSTANCE][plan.days[0]].available_at


def test_unresolvable_cited_bar_never_uses_a_guessed_decision_clock():
    engine, plan, warm = fixture(warm_count=1, capacity=50000)
    signal = replace(intent(warm[-1]), used_bar_id="unknown_bar")
    plan = replace(plan, decisions={(warm[-1], INSTANCE): [signal]})
    outcome = run_replay(engine, plan, warmup_days=warm)
    assert outcome.fills == ()
    assert [row["reason"] for row in outcome.blocked] == ["decision_clock_unknown"]


def test_warmup_publication_must_precede_the_actual_night_session_open():
    engine, plan, warm = fixture(warm_count=2)
    first_day = plan.days[0]
    night_open = datetime.combine(warm[-1], time(21), SH)
    plan.sessions[INSTANCE][first_day] = replace(session(first_day), session_open=night_open)
    plan.bars[INSTANCE][warm[-1]] = replace(plan.bars[INSTANCE][warm[-1]], available_at=night_open)
    with pytest.raises(ValueError, match="before first scored open"):
        run_replay(engine, plan, warmup_days=warm)
    assert engine.marks == engine.fills == []


def test_capacity_at_later_scored_open_excludes_unpublished_scored_volume():
    engine, plan, warm = fixture(score_count=2)
    for index, day in enumerate(warm):
        plan.bars[INSTANCE][day] = bar(day, volume="5000" if index < 10 else "1000")
    first, second = plan.days
    plan.bars[INSTANCE][first] = replace(
        bar(first, volume="0"),
        available_at=session(second).session_open + timedelta(seconds=1),
    )
    plan = replace(final_decision(plan, warm, lots=3), terminal_days={INSTANCE: second})
    outcome = run_replay(engine, plan, warmup_days=warm)
    # The 20 available prior rows have median 3000. Using the unpublished
    # zero-volume row would reduce capacity to one and strand two lots.
    assert [fill.lots for fill in outcome.fills] == [3, 3]
    assert [fill.trading_day for fill in outcome.fills] == [first, second]
    assert outcome.open_positions == {}
    assert outcome.status == "complete"


def test_missing_first_scored_session_keeps_the_final_warmup_intent():
    engine, plan, warm = fixture()
    del plan.sessions[INSTANCE][plan.days[0]]
    outcome = run_replay(engine, final_decision(plan, warm), warmup_days=warm)
    assert len(outcome.fills) == 1
    assert outcome.fills[0].trading_day == plan.days[1]


def test_separate_runs_share_inputs_but_never_account_or_provider_state():
    first, plan, warm = fixture(score_count=2)
    plan = replace(plan, terminal_days={INSTANCE: plan.days[-1]})
    frozen_before = deepcopy(plan)
    histories = []

    def make_provider():
        history = []
        histories.append(history)

        def provider(state, day):
            history.append(plan.bars[INSTANCE][day])
            return [intent(day)] if day == warm[-1] else []

        return provider

    first_outcome = run_replay(first, plan, warmup_days=warm, decision_provider=make_provider())
    second = LedgerEngine(
        initial_cash=CASH, policy=first.policy,
        contracts={INSTANCE: (PRODUCT, INSTANCE, first.rules[INSTANCE])},
    )
    second_outcome = run_replay(second, plan, warmup_days=warm, decision_provider=make_provider())
    assert first_outcome == second_outcome
    assert histories[0] == histories[1]
    assert histories[0] is not histories[1]
    assert first.ledgers[INSTANCE] is not second.ledgers[INSTANCE]
    assert plan == frozen_before
    with pytest.raises(ValueError, match="fresh engine"):
        run_replay(first, plan, warmup_days=warm)


@pytest.mark.parametrize("direction", [1, -1])
def test_warmup_preserves_rejected_close_cash_and_fifo_accounting(direction):
    engine, plan, warm = fixture(score_count=3)
    first, second, third = plan.days
    plan.bars[INSTANCE][second] = bar(second, price="110")
    plan.bars[INSTANCE][third] = bar(third, price="110")
    engine.rules[INSTANCE] = replace(engine.rules[INSTANCE], limit_bands={
        **engine.rules[INSTANCE].limit_bands,
        second.isoformat(): (Decimal("110.5"), Decimal("109.5")),
    })
    plan = replace(plan, decisions={
        (warm[-1], INSTANCE): [intent(warm[-1], lots=direction)],
        (first, INSTANCE): [intent(first, lots=0, kind="close")],
    })
    outcome = run_replay(engine, plan, warmup_days=warm)
    assert [fill.trading_day for fill in outcome.fills] == [first, third]
    assert [row["reason"] for row in outcome.blocked] == ["modeled_slippage_outside_band"]
    assert outcome.marks[1].cash == outcome.marks[0].cash == CASH - Decimal("15.01")
    assert outcome.trades[0].gross_pnl == direction * Decimal("100")
    assert outcome.marks[-1].cash == CASH + direction * Decimal("100") - Decimal("30.02")
    assert outcome.reconciliation["cash_matches_fill_identity"] is True
    assert outcome.reconciliation["equity_matches_gross_identity"] is True
    assert outcome.status == "complete"
