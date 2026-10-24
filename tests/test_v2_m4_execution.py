from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from ak_trading.v2.contracts import FeeLeg, OrderIntent, PositionSnapshot, SessionWindow
from ak_trading.v2.policy import calculate_stress_loss
from ak_trading.v2.decision.execution import capacity_before, simulate_execution, simulate_stress_paths
from test_v2_m4_ledger import DAY, NS, bar, budget, rule


def intent(delta=1, offset="OPEN", **changes):
    return replace(OrderIntent("intent", "decision", "trend_m1_v2", "episode", "rb",0,NS,
        10*NS,delta,offset,"ENTRY" if offset=="OPEN" else "STOP",0 if offset=="OPEN" else None,None),**changes)


def test_execution_uses_only_open_and_frozen_capacity():
    r=rule(slippage_ticks=D(1),impact_ticks=D(1))
    a=simulate_execution(intent(4),bar(),r,3,None)
    b=simulate_execution(intent(4),bar(high=D(199),low=D(2),close=D(5),volume=D(9999999)),r,3,None)
    assert a==b and a.status=="PARTIAL" and a.remaining_lots==1
    assert a.fills[0].execution_price==D("100.2")
    assert a.fills[0].slippage_cny==D(6)


def test_capacity_twenty_complete_same_frequency_excludes_future_volume():
    bars=tuple(bar(t=i*NS,volume=D(i)) for i in range(1,21))
    assert capacity_before(bars,"rb",60,21*NS,rule())==1
    assert capacity_before(bars[:-1],"rb",60,21*NS,rule()) is None
    assert capacity_before(bars,"rb",86400,21*NS,rule()) is None
    assert capacity_before(bars+(bar(t=21*NS,volume=D(999999)),),"rb",60,21*NS,rule())==1


@pytest.mark.parametrize("side,price",[(1,"200"),(-1,"1")])
def test_locked_board_exit_preserves_signed_remaining(side,price):
    p=PositionSnapshot("rb","trend_m1_v2","episode",-side*3,D(100),True)
    out=simulate_execution(intent(side*3,"CLOSE_TODAY"),bar(price=price),rule(),3,p)
    assert out.fills==() and out.remaining_lots==side*3 and "EXIT_PENDING" in out.reasons


def test_short_partial_result_and_shared_capacity():
    a=simulate_execution(intent(-2),bar(),rule(),3,None)
    b=simulate_execution(replace(intent(-2),intent_id="second"),bar(),rule(),3-sum(f.lots for f in a.fills),None)
    assert sum(f.lots for x in (a,b) for f in x.fills)==3
    assert b.remaining_lots==-1


def test_gap_stop_uses_next_actual_open():
    p=PositionSnapshot("rb","trend_m1_v2","episode",1,D(100),True)
    out=simulate_execution(intent(-1,"CLOSE_TODAY"),bar(price="90"),rule(),1,p)
    assert out.fills[0].execution_price==D(90)


def test_open_gap_limit_never_clamps_and_unknown_fee_does_not_close():
    out=simulate_execution(intent(limit_price=D("100.1")),bar(price="101"),rule(),1,None)
    assert not out.fills and "ENTRY_GAP_EXCEEDED" in out.reasons
    p=PositionSnapshot("rb","trend_m1_v2","episode",1,D(100),True)
    out=simulate_execution(intent(-1,"CLOSE_TODAY"),bar(),rule(close_today_fee=None),1,p)
    assert not out.fills and "COST_UNKNOWN" in out.reasons and out.remaining_lots==-1


def test_fees_add_per_lot_and_notional_rate_at_execution_price():
    r=rule(open_fee=FeeLeg(D(2),D(".001"),False),slippage_ticks=D(10))
    f=simulate_execution(intent(2),bar(),r,2,None).fills[0]
    assert f.execution_price==D(101) and f.fee_cny==D("6.02")


def test_signal_open_expired_and_illegal_tick_reject():
    assert not simulate_execution(intent(),bar(t=0),rule(),2,None).fills
    assert simulate_execution(intent(),bar(t=10*NS),rule(),2,None).status=="EXPIRED"
    assert "PRICE_TICK_INVALID" in simulate_execution(intent(),bar(price="100.01"),rule(),2,None).reasons


def test_stress_martingale_124_hand_money_and_calendar():
    b=budget()
    sessions=tuple(SessionWindow(f"day-{i}","SHFE.RB",DAY+timedelta(days=i),
        (i*200+1)*NS,(i*200+100)*NS,"DAY","synthetic","SCENARIO") for i in range(4))
    paths=simulate_stress_paths(b,rule(),sessions)
    gap=next(p for p in paths if p.scenario=="GAP")
    locked=next(p for p in paths if p.scenario=="NO_STOP")
    # 1*6.1*10 + 2*4.1*10 + 4*2.1*10 = 227; normal fees 14 =>241.
    assert sum(l.lots*(p-D(94))*D(10) for l,p in zip(b.layers,gap.entry_prices))==D(227)
    # gap 91: 91+142+204=437 gross plus 28 stressed fees.
    assert (gap.gross_loss_cny,gap.fees_cny,gap.loss_cny)==(D(437),D(28),D(465))
    assert (locked.gross_loss_cny,locked.fees_cny,locked.loss_cny)==(D(507),D(28),D(535))
    assert locked.reference_prices==(D(93),D(92),D(91),D(90)) and len(locked.trading_days)==4
    assert max(p.loss_cny for p in paths)==calculate_stress_loss(b.layers,1,D(94),D(1),rule(),b.stress_policy)
    # (100.1+2*98.1+4*96.1)*10*.15 = 1021.05, no netting or first-layer-only margin.
    assert locked.peak_margin_cny==D("1021.05")
    assert locked.margin_shortfall_cny==0


def test_stress_calendar_failure_and_low_equity_shortfall():
    paths=simulate_stress_paths(replace(budget(),equity_at_start_cny=D(600)),rule(),())
    assert all(p.evidence=="SCENARIO" and not p.trading_days for p in paths)
    assert max(p.margin_shortfall_cny for p in paths)>0
    with pytest.raises(ValueError,match="COST_UNKNOWN"):
        simulate_stress_paths(budget(),rule(close_fee=None),())


def test_daily_open_execution_keeps_trading_day_offset_and_capacity():
    i=intent(-2)
    b=bar(duration_seconds=86400,end_nano=6*NS,available_at_nano=6*NS)
    out=simulate_execution(i,b,rule(),1,None)
    assert out.status=="PARTIAL" and out.remaining_lots==-1 and out.fills[0].trading_day==DAY


def test_registered_stop_first_ambiguity_does_not_rewrite_label_or_bar():
    from ak_trading.v2.decision.execution import simulate_protective_exit
    p=PositionSnapshot("rb","trend_m1_v2","episode",1,D(100),True)
    b=bar(price="100",high=D(106),low=D(93),close=D(100))
    out=simulate_protective_exit(intent(-1,"CLOSE_TODAY"),b,rule(),1,p,budget())
    assert out.fills[0].execution_price==D(94) and "STOP_FIRST_AMBIGUITY" in out.reasons
    assert b.open==D(100) and b.low==D(93)


def test_host_cost_scenario_rule_is_consumed_once_and_stress_uses_base():
    # RunPolicy never multiplies costs inside execution; the host freezes this rule once.
    doubled=rule(open_fee=FeeLeg(D(2),D(0),False),slippage_ticks=D(2))
    f=simulate_execution(intent(),bar(),doubled,1,None).fills[0]
    assert f.fee_cny==D(2) and f.execution_price==D("100.2") and f.slippage_cny==D(2)
    base=rule(slippage_ticks=D(1))
    paths=simulate_stress_paths(budget(),base,())
    gap=next(p for p in paths if p.scenario=="GAP")
    assert gap.exit_price==D("90.8") and gap.fees_cny==D(28)
    assert gap.entry_prices==(D("100.2"),D("98.2"),D("96.2"))


