from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal as D

import pytest

from ak_trading.v2.contracts import (
    AccountSnapshot, DataQuality, FeeLeg, Fill, Instrument, LayerSpec, OrderIntent,
    MarketBar, RoundBudget, RunPolicy, SessionWindow, StrategyState, TradingRule,
)
from ak_trading.v2.policy import stress_policy
from ak_trading.v2.decision.ledger import ContinuousLedger, publish_checkpoint, read_checkpoint

DAY = date(2026, 1, 12)
NS = 60_000_000_000
HASH = "a" * 64
MODELS = {"forecast": HASH, "payoff": HASH, "regime_reference": HASH}


def quality(status="READY"):
    return DataQuality(status, (), "reconstructed", HASH)


def instrument(contract="rb"):
    return Instrument(contract, "SHFE.rb2605", "SHFE.RB", "METAL", ("STEEL",),
                      0, 10000 * NS, 10000 * NS, 9000 * NS, quality())


def account(cash="1000"):
    c = D(cash)
    return AccountSnapshot("synthetic", 0, DAY, c, c, D(0), D(0), D(0), D(0), c,
                           D(0), D(0), c, D(0), c, (), False, quality())


FIXED_EXIT_BINDING = "c" * 64  # tests fixture binding; real runs bind actual specs


def run_policy(mode="BEST_EV"):
    return RunPolicy("RESEARCH_EVALUATION", mode, 7,
                     "trend_m1_v2" if mode == "FIXED" else None,
                     FIXED_EXIT_BINDING if mode == "FIXED" else None,
                     "research-policy-v2.1.0", "macro-risk-v2.1.0", "PRICE_ONLY", "BASE")


def rule(contract="rb", **changes):
    leg = FeeLeg(D(1), D(0), False)
    base = TradingRule("rule-" + contract, contract, 0, 10000 * NS, 0, D(10), D("0.1"),
                       1, 1, leg, leg, leg, D("0.1"), D("0.1"), D(200), D(1),
                       D(0), D(0), D("0.1"), "SCENARIO", HASH)
    return replace(base, **changes)


def bar(t=NS, price="100", contract="rb", **changes):
    p = D(price)
    return replace(MarketBar(f"bar-{contract}-{t}", contract, "SHFE.rb2605", "SHFE.RB", 60,
        t, t, t + NS, DAY, "session", t + NS, None, p, p, p, p, D(30), D(100),
        D(100), True, 1, 1, "SYNTHETIC", quality()), **changes)


def fill(name, t, price, side=1, offset="OPEN", lots=1, reference=None, day=DAY,
         strategy="trend_m1_v2", episode="episode", contract="rb", fee=None):
    r = rule(contract)
    ref = D(reference or price)
    return Fill(name, "intent-" + name, contract, strategy, episode, t, day, side, lots,
                offset, ref, D(price), D(fee) if fee is not None else D(lots),
                abs(D(price)-ref)*lots*r.multiplier, r.rule_id, "SCENARIO")


def budget(contract="rb", episode="episode", reserve="300", equity="100000", side=1):
    layers = tuple(LayerSpec(i, D(p), D(p), q) for i, (p, q) in enumerate((("100",1),("98",2),("96",4))))
    return RoundBudget("budget-"+episode, episode, contract, 0, None, None, stress_policy(),
        D(1), side, layers, D(94) if side == 1 else D(106), D(104) if side == 1 else D(90),
        100 * NS, 7, D(241), D(535), D(reserve), D(equity)*D(".02"), D(equity))


def state(b=None, filled=(), phase="OBSERVE", pending=()):
    return StrategyState("trend_m1_v2", None if b is None else b.episode_id, "rb", phase,
        None, None, 0 if b is None else b.side,
        () if b is None else tuple(i for i, (n,l) in enumerate(zip(filled,b.layers)) if n==l.lots),
        filled, None, 90*NS if b is not None and any(filled) else None,
        90*NS if b is not None and not any(filled) else None, pending, b, 3, None, None)


EXIT_REGISTRY = "d" * 64  # tests fixture exit registry hash


def _legacy_bindings():
    from ak_trading.v2.strategies.registry import build_strategy_spec
    from ak_trading.v2.decision.pipeline import legacy_exit_binding
    return (legacy_exit_binding(build_strategy_spec("TREND","MINUTE")),)

LEGACY_BINDINGS = _legacy_bindings()


def legacy_exit_fixtures(spec):
    """LEGACY exit spec + binding fixtures for tests (explicit, never default)."""
    from ak_trading.v2.decision.pipeline import legacy_exit_binding, legacy_exit_spec
    return legacy_exit_spec(spec.mode), legacy_exit_binding(spec)


def exit_fixtures_for(specs):
    from ak_trading.v2.decision.pipeline import exit_registry_hash_for
    specs_tuple = tuple(specs)
    exit_specs = tuple(legacy_exit_fixtures(s)[0] for s in specs_tuple)
    bindings = tuple(legacy_exit_fixtures(s)[1] for s in specs_tuple)
    return exit_specs, bindings, exit_registry_hash_for(exit_specs)


def ledger(cash="1000"):
    return ContinuousLedger(account(cash), (instrument(),), run_policy(), HASH, HASH,
                            exit_registry_sha256=EXIT_REGISTRY)


def test_cash_slippage_is_memo_not_second_cash_deduction():
    book = ledger()
    book.apply_fill(fill("buy", NS, "101", reference="100"), rule())
    book.mark({"rb":bar(t=0,price="101")},NS,DAY,(rule(),))
    a = book.snapshot(NS)
    assert (a.cash_cny, a.equity_cny, a.margin_cny) == (D(999),D(999),D(101))
    book.apply_fill(fill("sell", 2*NS, "102", -1, "CLOSE_TODAY", reference="103"), rule())
    a = book.snapshot(2*NS)
    assert (a.realized_gross_cny,a.fees_cny,a.slippage_cny,a.cash_cny)==(D(10),D(2),D(20),D(1008))
    assert book.reconcile() == {"cash_difference_cny":D(0),"equity_difference_cny":D(0),
                              "position_match":True,"fees_match":True,"status":"PASS"}


def test_two_episodes_share_one_initial_cash():
    book = ledger()
    for f in (fill("a",NS,"100"), fill("b",2*NS,"101",-1,"CLOSE_TODAY"),
              fill("c",3*NS,"100",episode="two"),
              fill("d",4*NS,"98",-1,"CLOSE_TODAY",episode="two")):
        book.apply_fill(f,rule())
    assert book.snapshot(4*NS).cash_cny == D(986)


def test_short_round_and_signed_position():
    from ak_trading.v2.decision.execution import position_for
    book = ledger()
    book.apply_fill(fill("short",NS,"100",-1),rule())
    assert position_for(book.snapshot(NS),"rb").lots == -1
    book.apply_fill(fill("cover",2*NS,"98",1,"CLOSE_TODAY"),rule())
    assert book.snapshot(2*NS).cash_cny == D(1018)


def test_trading_day_fee_and_fifo_batches():
    book = ledger("10000")
    r = rule(close_today_fee=FeeLeg(D(3),D(0),False))
    book.apply_fill(fill("old",NS,"100",lots=2),r)
    book.apply_fill(fill("today",2*NS,"102",day=date(2026,1,13)),r)
    book.apply_fill(fill("old-close",3*NS,"103",-1,"CLOSE",lots=1,day=date(2026,1,13)),r)
    book.apply_fill(fill("today-close",4*NS,"103",-1,"CLOSE_TODAY",day=date(2026,1,13),fee="3"),r)
    a = book.snapshot(4*NS)
    assert a.realized_gross_cny == D(40) and a.fees_cny == D(7)
    assert len(a.lots)==1 and a.lots[0].entry_price==D(100) and a.lots[0].opening_fee_cny==D(1)
    with pytest.raises(ValueError, match="owner_offset"):
        book.apply_fill(fill("wrong-day",5*NS,"103",-1,"CLOSE_TODAY",day=date(2026,1,13),fee="3"),r)


def test_reservation_replaces_actual_margin_and_releases_cancelled_layers():
    book = ledger("100000")
    b = budget()
    book.sync_reservations((state(b,(0,0,0),"WAIT_TRIGGER",("entry",)),),(rule(),),0)
    assert book.snapshot(0).reserved_margin_cny == D(300)
    book.apply_fill(fill("entry",NS,"100"),rule())
    book.sync_reservations((state(b,(1,0,0),"HOLDING"),),(rule(),),NS)
    a=book.snapshot(NS)
    assert a.margin_cny + a.reserved_margin_cny == D(300)
    book.sync_reservations((state(b,(1,0,0),"EXITING"),),(rule(),),NS)
    assert book.snapshot(NS).reserved_margin_cny == D(0)
    assert len(book.snapshot(NS).lots)==1


def test_restore_fill_idempotency_and_dependency_hashes(tmp_path):
    book=ledger()
    f=fill("a",NS,"100")
    book.apply_fill(f,rule())
    s=state(budget(),(1,0,0),"HOLDING")
    cp=book.checkpoint((s,),(),MODELS,{},pending_remaining_lots={},exit_registry_sha256=EXIT_REGISTRY,
                             exit_bindings=LEGACY_BINDINGS,exit_states=(),exit_execution_links=())
    path=tmp_path/'checkpoint.json'
    publish_checkpoint(path,cp)
    restored=ContinuousLedger.restore(read_checkpoint(path),(instrument(),),(rule(),),run_policy(),HASH,HASH,MODELS,{},exit_registry_sha256=EXIT_REGISTRY)
    restored.apply_fill(f,rule())
    assert restored.snapshot(NS)==book.snapshot(NS)
    with pytest.raises(ValueError, match="conflicting_duplicate"):
        restored.apply_fill(replace(f,execution_price=D(101)),rule())
    with pytest.raises(ValueError, match="dependency"):
        ContinuousLedger.restore(cp,(instrument(),),(rule(),),run_policy(),"b"*64,HASH,MODELS,{},exit_registry_sha256=EXIT_REGISTRY)
    with pytest.raises(ValueError, match="rule_hash"):
        ContinuousLedger.restore(cp,(instrument(),),(rule(price_tick=D(1)),),run_policy(),HASH,HASH,MODELS,{},exit_registry_sha256=EXIT_REGISTRY)
    with pytest.raises(FileExistsError):
        publish_checkpoint(path,cp)
    path.write_bytes(path.read_bytes()[:-8])
    with pytest.raises(ValueError):
        read_checkpoint(path)


def test_fee_mismatch_and_out_of_order_are_atomic():
    book=ledger()
    before=book.snapshot(0)
    with pytest.raises(ValueError,match="fee_mismatch"):
        book.apply_fill(fill("bad",NS,"100",fee="0"),rule())
    assert book.snapshot(0)==before
    book.apply_fill(fill("ok",NS,"100"),rule())
    with pytest.raises(ValueError,match="out_of_order"):
        book.apply_fill(fill("older",0,"101"),rule())


def test_missing_quote_keeps_position_and_unrealized_and_risk_lock():
    book=ledger()
    book.apply_fill(fill("a",NS,"100"),rule())
    a=book.mark({"rb":bar(t=NS,price="98")},2*NS,DAY,(rule(),))
    assert a.killed and a.unrealized_cny==D(-20)
    stale=book.mark({"rb":None},3*NS,DAY,(rule(),))
    assert stale.unrealized_cny==D(-20) and stale.lots and stale.quality.status=="STALE"
    next_day=book.mark({"rb":bar(t=3*NS,price="100",trading_day=date(2026,1,13))},4*NS,date(2026,1,13),(rule(),))
    assert next_day.killed and next_day.day_start_equity_cny==D(1000)
    assert book.reconcile()["status"]=="INCOMPLETE"


def test_consuming_future_rules_does_not_change_checkpoint():
    book=ledger()
    book.mark({},NS,DAY,(rule(),))
    cp=book.checkpoint((),(),MODELS,{},pending_remaining_lots={},exit_registry_sha256=EXIT_REGISTRY,
                             exit_bindings=LEGACY_BINDINGS,exit_states=(),exit_execution_links=())
    future=replace(rule(),rule_id="future",effective_from_nano=20000*NS,effective_until_nano=30000*NS,available_at_nano=20000*NS)
    restored=ContinuousLedger.restore(cp,(instrument(),),(rule(),future),run_policy(),HASH,HASH,MODELS,{},exit_registry_sha256=EXIT_REGISTRY)
    assert restored.snapshot(NS)==cp.account and cp.rule_hashes=={}


def test_two_contract_source_marks_restore_and_one_missing_quote_hand_calc():
    from ak_trading.v2.decision.ledger import record_hash
    inst=(instrument("a"),instrument("b"))
    rules=(rule("a"),rule("b",multiplier=D(5)))
    book=ContinuousLedger(account("10000"),inst,run_policy(),HASH,HASH,exit_registry_sha256=EXIT_REGISTRY)
    book.apply_fill(fill("a-open",0,"100",contract="a",episode="a"),rules[0])
    book.apply_fill(fill("b-open",0,"200",lots=2,contract="b",episode="b"),rules[1])
    sa=replace(state(budget("a","a"),(1,0,0),"HOLDING"),contract_id="a")
    sb=replace(state(budget("b","b"),(1,1,0),"HOLDING"),contract_id="b")
    a_bar=bar(t=0,price="102",contract="a")
    b_bar=bar(t=NS,price="197",contract="b")
    book.mark({"a":a_bar},NS,DAY,rules)
    before=book.mark({"b":b_bar},2*NS,DAY,rules)
    assert (before.cash_cny,before.unrealized_cny,before.equity_cny,before.margin_cny)==(D(9997),D(-10),D(9987),D(299))
    cp=book.checkpoint((sa,sb),(),MODELS,{},pending_remaining_lots={},exit_registry_sha256=EXIT_REGISTRY,
                             exit_bindings=LEGACY_BINDINGS,exit_states=(),exit_execution_links=())
    assert [m.as_of_nano for m in cp.valuation_marks]==[NS,2*NS]
    assert cp.valuation_marks[0].source_bar_sha256==record_hash(a_bar)
    resumed=ContinuousLedger.restore(cp,inst,rules,run_policy(),HASH,HASH,MODELS,{},exit_registry_sha256=EXIT_REGISTRY)
    assert resumed.snapshot(2*NS)==before and resumed.reconcile()==book.reconcile()
    next_b=bar(t=2*NS,price="198",contract="b")
    x=book.mark({"b":next_b},3*NS,DAY,rules)
    y=resumed.mark({"b":next_b},3*NS,DAY,rules)
    assert x==y
    assert (x.unrealized_cny,x.equity_cny,x.margin_cny,x.fees_cny)==(D(0),D(9997),D(300),D(3))
    cp2=resumed.checkpoint((sa,sb),(),MODELS,{},pending_remaining_lots={},exit_registry_sha256=EXIT_REGISTRY,
                             exit_bindings=LEGACY_BINDINGS,exit_states=(),exit_execution_links=())
    assert [x.as_of_nano-m.as_of_nano for m in cp2.valuation_marks]==[2*NS,0]
    assert x.quality.status=="STALE" and resumed.reconcile()["status"]=="INCOMPLETE"


def test_unvalued_first_fill_never_becomes_entry_mark_and_duplicate_is_noop():
    book=ledger()
    f=fill("first",NS,"100")
    assert book.apply_fill(f,rule()) is True
    s=state(budget(),(1,0,0),"HOLDING")
    cp=book.checkpoint((s,),(),MODELS,{},pending_remaining_lots={},exit_registry_sha256=EXIT_REGISTRY,
                             exit_bindings=LEGACY_BINDINGS,exit_states=(),exit_execution_links=())
    assert cp.valuation_marks[0].status=="UNVALUED" and cp.valuation_marks[0].price is None
    assert cp.account.margin_cny==D(0) and cp.account.equity_cny==D(999) and cp.account.quality.status=="MISSING"
    restored=ContinuousLedger.restore(cp,(instrument(),),(rule(),),run_policy(),HASH,HASH,MODELS,{},exit_registry_sha256=EXIT_REGISTRY)
    before=restored.snapshot(NS)
    assert restored.apply_fill(f,rule()) is False and restored.snapshot(NS)==before
    assert restored.reconcile()["status"]=="INCOMPLETE"
    with pytest.raises(ValueError,match="existing_account"):
        ContinuousLedger(before,(instrument(),),run_policy(),HASH,HASH,exit_registry_sha256=EXIT_REGISTRY)


def test_stale_rule_preserves_old_price_rule_combination_and_remaining_lots():
    book=ledger("10000")
    book.apply_fill(fill("open",NS,"100",lots=2),rule())
    book.mark({"rb":bar(t=NS,price="102")},2*NS,DAY,(rule(),))
    unknown=replace(rule(),rule_id="unknown",long_margin_rate=None)
    a=book.mark({"rb":bar(t=2*NS,price="150")},3*NS,DAY,(unknown,))
    assert a.unrealized_cny==D(40) and a.margin_cny==D(204) and a.quality.status=="STALE"
    book.apply_fill(fill("close-one",4*NS,"103",-1,"CLOSE_TODAY"),rule())
    a=book.snapshot(4*NS)
    assert a.unrealized_cny==D(20) and a.margin_cny==D(102) and len(a.lots)==1
    s=state(budget(),(1,1,0),"HOLDING")
    cp=book.checkpoint((s,),(),MODELS,{},pending_remaining_lots={},exit_registry_sha256=EXIT_REGISTRY,
                             exit_bindings=LEGACY_BINDINGS,exit_states=(),exit_execution_links=())
    assert cp.valuation_marks[0].price==D(102) and cp.valuation_marks[0].rule_id==rule().rule_id
    assert "unknown" in cp.rule_hashes
    with pytest.raises(ValueError,match="rule_hash"):
        ContinuousLedger.restore(cp,(instrument(),),(unknown,),run_policy(),HASH,HASH,MODELS,{},exit_registry_sha256=EXIT_REGISTRY)


def test_repeat_old_bar_cannot_clear_missing_status_and_future_bar_rejects_valuation():
    book=ledger()
    book.apply_fill(fill("open",NS,"100"),rule())
    old=bar(t=NS,price="101")
    book.mark({"rb":old},2*NS,DAY,(rule(),))
    book.mark({},3*NS,DAY,(rule(),))
    repeated=book.mark({"rb":old},4*NS,DAY,(rule(),))
    assert repeated.quality.status=="STALE"
    future=book.mark({"rb":bar(t=10*NS,price="199")},5*NS,DAY,(rule(),))
    assert future.unrealized_cny==D(10) and future.quality.status=="STALE"


def test_model_change_requires_explicit_flat_activation_and_preserves_kill():
    book=ledger()
    cp=book.checkpoint((),(),MODELS,{},pending_remaining_lots={},exit_registry_sha256=EXIT_REGISTRY,
                             exit_bindings=LEGACY_BINDINGS,exit_states=(),exit_execution_links=())
    changed={**MODELS,"payoff":"b"*64}
    with pytest.raises(ValueError,match="flat_activation"):
        book.checkpoint((),(),changed,{},pending_remaining_lots={},exit_registry_sha256=EXIT_REGISTRY,
                             exit_bindings=LEGACY_BINDINGS,exit_states=(),exit_execution_links=())
    before=book.snapshot(0)
    book.record_event("MODEL_ACTIVATION:"+"c"*64,{})
    next_cp=book.checkpoint((),(),changed,{},pending_remaining_lots={},exit_registry_sha256=EXIT_REGISTRY,
                             exit_bindings=LEGACY_BINDINGS,exit_states=(),exit_execution_links=())
    assert next_cp.account==before and next_cp.processed_fill_hashes==cp.processed_fill_hashes
    with pytest.raises(ValueError,match="dependency"):
        ContinuousLedger.restore(cp,(instrument(),),(rule(),),run_policy(),HASH,HASH,changed,{},exit_registry_sha256=EXIT_REGISTRY)


