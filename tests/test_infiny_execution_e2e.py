"""E20--E28 offline integration, real typed N2/N3 and a fictitious SDK port."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import builtins
import importlib.util
from pathlib import Path
import sqlite3
import sys
from threading import Thread

import pytest

# Guard the very first execution-module imports, before any SDK can be loaded.
_import_before_execution = builtins.__import__

def _execution_import_guard(name, *args, **kwargs):
    if name.split(".")[0] in {"INFINIGO", "numpy", "ctypes", "pythongo", "PyQt5", "PyQt6", "dotenv"}:
        raise AssertionError("execution module attempted a native/GUI/config import")
    return _import_before_execution(name, *args, **kwargs)

builtins.__import__ = _execution_import_guard
try:
    from ak_trading.infiny_execution import contracts as c
    from ak_trading.infiny_execution.ledger import LedgerError
    from ak_trading.infiny_execution.native import NativeAdapter, NativeExecutionError, build_native_request
    from ak_trading.infiny_execution.simulation import FakeClock, FakeNative, Simulation, simulation_context
finally:
    builtins.__import__ = _import_before_execution

D = Decimal
ROOT = Path(__file__).resolve().parents[1]


class AttributeBomb:
    def __getattribute__(self, name):
        raise AssertionError("native attribute was inspected")


@pytest.fixture
def sim(tmp_path):
    item = Simulation(tmp_path / "journal.sqlite")
    try:
        yield item
    finally:
        item.close()


def map_leg(*, offset=c.Offset.OPEN, direction=c.Direction.BUY, price=D("3500")):
    close = offset is not c.Offset.OPEN
    bucket = {c.Offset.CLOSE: c.Bucket.ALL, c.Offset.CLOSE_TODAY: c.Bucket.TD,
              c.Offset.CLOSE_YESTERDAY: c.Bucket.YD}.get(offset)
    return c.Leg(leg_id="SIM_LEG:CLOSE:TD:0" if close else "SIM_LEG:OPEN:-:0", index=0,
                 phase=c.Phase.CLOSE if close else c.Phase.OPEN, depends_on=(), direction=direction,
                 offset=offset, volume=2, price=price, position_bucket=bucket, cash_reserve=D("2000"))


@pytest.mark.parametrize("direction,expected_direction", [(c.Direction.BUY, "0"), (c.Direction.SELL, "1")])
@pytest.mark.parametrize("order_type,expected_type", [(c.OrderType.GFD, "0"), (c.OrderType.FAK, "1")])
@pytest.mark.parametrize("exchange,offset,expected_offset", [
    ("SHFE", c.Offset.OPEN, "0"), ("SHFE", c.Offset.CLOSE_YESTERDAY, "1"),
    ("SHFE", c.Offset.CLOSE_TODAY, "3"), ("INE", c.Offset.CLOSE_YESTERDAY, "1"),
    ("INE", c.Offset.CLOSE_TODAY, "3"), ("DCE", c.Offset.CLOSE, "1")])
def test_E20_pure_exact_sdk_mapping(direction, expected_direction, order_type, expected_type,
                                  exchange, offset, expected_offset):
    context = simulation_context(exchange=exchange)
    native = FakeNative(context.binding)
    leg = map_leg(offset=offset, direction=direction)
    request = build_native_request(leg, context.binding, native.investor_handle, order_type=order_type,
                                  price_tick=D("1"), verified_hedge_flags=("1",))
    assert request == dict(StrategyID=23, Exchange=exchange, InstrumentID="rb2610", Volume=2, Price=3500.0,
                           Direction=expected_direction, OrderType=expected_type, Investor=native.investor_handle,
                           HedgeFlag="1", Offset=expected_offset, Market=False, Memo=leg.leg_id)
    native.send_order(request)
    assert len(native.sent_requests) == 1


@pytest.mark.parametrize("kwargs", [dict(order_type="FOK"), dict(order_type="3"), dict(order_type="GFD"),
                                     dict(verified_hedge_flags=()), dict(price_tick=None)])
def test_E20_invalid_evidence_never_reaches_port(kwargs):
    ctx = simulation_context()
    native = FakeNative(ctx.binding)
    values = dict(order_type=c.OrderType.GFD, price_tick=D("1"), verified_hedge_flags=("1",))
    values.update(kwargs)
    with pytest.raises(NativeExecutionError):
        native.send_order(build_native_request(map_leg(), ctx.binding, native.investor_handle, **values))
    assert native.sent_requests == []


@pytest.mark.parametrize("price,tick,accepted", [("3500.2", ".2", True), ("3500.1", ".2", False),
                                                ("9007199254740993", "1", False),
                                                ("3500.0000000000001", ".0000000000001", False)])
def test_E20_float_roundtrip_must_preserve_exact_tick(price, tick, accepted):
    ctx = simulation_context()
    native = FakeNative(ctx.binding)
    call = lambda: build_native_request(map_leg(price=D(price)), ctx.binding, native.investor_handle,
                         order_type=c.OrderType.GFD, price_tick=D(tick), verified_hedge_flags=("1",))
    if accepted:
        assert Decimal(str(call()["Price"])) == D(price)
    else:
        with pytest.raises(NativeExecutionError, match="TICK_MALIGNED"):
            call()
    assert native.sent_requests == []


def test_E20_market_and_unsupported_close_mapping_are_rejected():
    ctx = simulation_context(exchange="DCE")
    with pytest.raises(c.ContractError):
        replace(ctx.intent, market=True)
    with pytest.raises(NativeExecutionError):
        build_native_request(map_leg(offset=c.Offset.CLOSE_YESTERDAY), ctx.binding, object(),
                             order_type=c.OrderType.GFD, price_tick=D("1"), verified_hedge_flags=("1",))


@pytest.mark.parametrize("mode", [c.ExecutionMode.DISABLED, c.ExecutionMode.SIMULATION, c.ExecutionMode.NATIVE,
                                  "native", "simulation"])
def test_E21_pure_disabled_adapter_never_resolves_native(mode):
    adapter = NativeAdapter(AttributeBomb(), mode=mode)
    with pytest.raises(NativeExecutionError):
        adapter.send_order(AttributeBomb())
    with pytest.raises(NativeExecutionError):
        adapter.cancel_order(7)
    assert adapter.full_account_reconciliation == "unsupported"
    assert adapter.submit_enabled is adapter.cancel_owned_enabled is False


def test_E21_pure_bridge_has_no_observer_inheritance_or_live_switch(monkeypatch):
    """AKExecutionBridge.py ownership moved to the NE1 client owner (plan §4).

    The safety counterexample itself is unchanged: loading the entry resolves
    no native attribute, inherits nothing from the observer strategies, keeps
    no live switch, and every bypass entry rejects with MODE_DISABLED. The
    exception type is asserted semantically (ValueError + MODE_DISABLED) so it
    holds both with repository types (NativeExecutionError) and in the
    self-contained client (its own ContractError) per H37.
    """
    import types
    native = types.ModuleType("INFINIGO")
    monkeypatch.setitem(sys.modules, "INFINIGO", native)
    path = ROOT / "integrations/infiny/AKExecutionBridge.py"
    spec = importlib.util.spec_from_file_location("_n4_client_boundary", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    assert "AIBridge" not in vars(module) and "AKObserverBridge" not in vars(module)
    assert len(module.AKExecutionBridge.__bases__) == 1
    bridge = module.AKExecutionBridge(native=AttributeBomb())
    assert bridge.mode in (c.ExecutionMode.DISABLED, "disabled")
    assert bridge.submit_enabled is False and bridge.cancel_owned_enabled is False
    for method, args in ((bridge.send_order, (None,)), (bridge.cancel_order, (7,)),
                         (bridge.submit_once, ("i", "l", "p", None)),
                         (bridge.request_cancel, ("c", "i", "l", None))):
        with pytest.raises(ValueError, match="MODE_DISABLED"):
            method(*args)
    with pytest.raises(TypeError):
        module.AKExecutionBridge(enable=True)



def test_E21_pure_native_context_and_forged_proof_cannot_unlock():
    ctx = simulation_context()
    with pytest.raises(c.ContractError):
        replace(ctx, mode=c.ExecutionMode.NATIVE)
    with pytest.raises(c.ContractError):
        replace(ctx.authorization, mode=c.ExecutionMode.NATIVE)
    with pytest.raises(c.ContractError):
        replace(ctx.proof, source=c.Source.PYTHONGO)


def test_E26_pure_imports_do_not_load_sdk_gui_or_environment(monkeypatch):
    original = builtins.__import__
    seen = []
    denied = {"INFINIGO", "numpy", "ctypes", "pythongo", "PyQt5", "PyQt6", "dotenv"}
    def guarded(name, *args, **kwargs):
        seen.append(name)
        if name.split(".")[0] in denied:
            raise AssertionError("forbidden import: " + name)
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    for name in ("native", "executor", "simulation"):
        path = ROOT / "src/ak_trading/infiny_execution" / (name + ".py")
        spec = importlib.util.spec_from_file_location("ak_trading.infiny_execution._probe_" + name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
    assert not any(name.split(".")[0] in denied for name in seen)


@pytest.mark.parametrize("action", list(c.Action))
@pytest.mark.parametrize("order_type", list(c.OrderType))
@pytest.mark.parametrize("exchange", ["SHFE", "INE"])
def test_E20_six_actions_real_planner_journal_and_settled_fake_loop(tmp_path, action, order_type, exchange):
    context = simulation_context(action=action, order_type=order_type, exchange=exchange, yesterday=2,
                                 long=3 if action in (c.Action.CLOSE_LONG, c.Action.REVERSE_TO_SHORT) else 0,
                                 short=3 if action in (c.Action.CLOSE_SHORT, c.Action.REVERSE_TO_LONG) else 0)
    item = Simulation(tmp_path / "six.sqlite", context=context)
    try:
        proposed = item.prepare()
        assert item.context.reservation_receipt.reservation_ids == tuple(l.leg_id for l in proposed.legs)
        for leg in proposed.legs:
            if leg.depends_on:
                item.refresh_open(leg.leg_id)
            item.submit(leg.leg_id)
            item.fill(leg.leg_id)
        assert len(item.native.sent_requests) == len(proposed.legs)
        assert all(item.ledger.get_leg(l.leg_id).settled for l in proposed.legs)
        expected_direction = "0" if action in (c.Action.OPEN_LONG, c.Action.CLOSE_SHORT, c.Action.REVERSE_TO_LONG) else "1"
        assert all(x["Direction"] == expected_direction and x["OrderType"] == ("0" if order_type is c.OrderType.GFD else "1")
                   for x in item.native.sent_requests)
        if action in (c.Action.REVERSE_TO_LONG, c.Action.REVERSE_TO_SHORT):
            assert [x["Offset"] for x in item.native.sent_requests] == ["1", "3", "0"]
            assert [x["Volume"] for x in item.native.sent_requests] == [2, 1, 2]
        assert all(e.ownership_evidence.evidence_id.startswith("simulation:") for e in item.events)
    finally:
        item.close()


@pytest.mark.parametrize("result,state", [(7, c.OrderState.SUBMITTED_LOCAL), (-1, c.OrderState.LOCAL_REJECTED),
                                           (None, c.OrderState.SUBMIT_UNKNOWN), (True, c.OrderState.SUBMIT_UNKNOWN),
                                           (TimeoutError(), c.OrderState.SUBMIT_UNKNOWN)])
def test_E21_E26_no_retry_on_local_or_unknown_result(sim, result, state):
    proposed = sim.prepare()
    leg = proposed.legs[0]
    sim.native.send_result = result
    returned = sim.submit(leg.leg_id)
    assert returned.leg.state is state
    for _ in range(5):
        assert sim.submit(leg.leg_id).code is c.ResultCode.IDEMPOTENT
    assert len(sim.native.sent_requests) == 1


def test_E21_executor_disabled_before_port_inspection(sim):
    from ak_trading.infiny_execution.executor import Executor
    for mode in (c.ExecutionMode.DISABLED, c.ExecutionMode.NATIVE, "simulation"):
        executor = Executor(sim.ledger, port=AttributeBomb(), mode=mode, clock=sim.clock)
        with pytest.raises(NativeExecutionError):
            executor.prepare(sim.context.intent, sim.context)
        with pytest.raises(NativeExecutionError):
            executor.request_cancel("SIM_CANCEL", "i", "l", sim.context)
    assert sim.ledger.view(sim.context.binding.account_key).reservations == ()


def working(sim):
    proposed = sim.prepare()
    leg = proposed.legs[0]
    sim.submit(leg.leg_id)
    sim.event(leg.leg_id)
    assert sim.ledger.get_leg(leg.leg_id).ownership_verified
    return leg


def cancel(sim, leg, cancel_id="SIM_CANCEL", context=None):
    return sim.executor.request_cancel(cancel_id, sim.context.intent.intent_id, leg.leg_id, context or sim.context)


def test_E22_cancel_stale_quote_without_submit_or_full_proof(sim):
    leg = working(sim)
    sim.clock.advance(D("4"))
    ctx = replace(sim.context, proof=None, readiness=replace(sim.context.readiness, can_submit=False,
                   state=c.ReadinessState.PLAN_READY, blockers=("QUOTE_STALE",)))
    result = cancel(sim, leg, context=ctx)
    assert result.leg.cancel_state is c.CancelState.REQUESTED
    assert not result.leg.settled
    assert cancel(sim, leg, context=ctx).code is c.ResultCode.IDEMPOTENT
    with pytest.raises(LedgerError, match="CANCEL_UNKNOWN"):
        cancel(sim, leg, "SIM_OTHER_CANCEL", ctx)
    assert sim.native.cancelled_ids == [7]


@pytest.mark.parametrize("case", ["unowned", "route", "filled", "authorization", "unknown_alias"])
def test_E22_cancel_rejects_unowned_unroutable_or_finished(sim, case):
    proposed = sim.prepare()
    leg = proposed.legs[0]
    sim.submit(leg.leg_id)
    if case != "unowned":
        sim.event(leg.leg_id)
    ctx = sim.context
    if case == "route":
        ctx = replace(ctx, route_ownership=None)
    elif case == "filled":
        sim.fill(leg.leg_id)
        ctx = sim.context
    elif case == "authorization":
        ctx = replace(ctx, authorization=None)
    elif case == "unknown_alias":
        leg = replace(leg, leg_id="SIM_UNKNOWN")
    with pytest.raises((NativeExecutionError, LedgerError)):
        cancel(sim, leg, context=ctx)
    assert sim.native.cancelled_ids == []


def test_E23_trade_during_cancel_missing_notification_is_not_terminal(sim):
    leg = working(sim)
    cancel(sim, leg)
    trade = sim.event(leg.leg_id, kind=c.EventKind.TRADE, trade_id="SIM_T1", volume=1)
    sim.ledger.ingest_event(trade)
    sim.event(leg.leg_id, kind=c.EventKind.CANCEL, complete=False)
    current = sim.ledger.get_leg(leg.leg_id)
    assert current.trade_filled == 1
    assert current.cancel_state is c.CancelState.REQUESTED and not current.settled
    assert sim.ledger.view(sim.context.binding.account_key).cash_reserved == D("2000")
    sim.event(leg.leg_id, status="部成部撤", traded=1, cancelled=1)
    assert not sim.ledger.get_leg(leg.leg_id).settled
    sim.barrier()
    current = sim.ledger.get_leg(leg.leg_id)
    assert current.state is c.OrderState.PARTIAL_CANCELLED
    assert current.settled and current.cancel_state is c.CancelState.CONFIRMED
    assert current.trade_filled == current.risk_filled == 1
    assert sim.ledger.view(sim.context.binding.account_key).cash_reserved == 0


@pytest.mark.parametrize("fault", ["exception", "crash"])
def test_E22_cancel_unknown_or_restart_cannot_send_again(tmp_path, fault):
    from ak_trading.infiny_execution.executor import Executor
    from ak_trading.infiny_execution.ledger import Ledger
    item = Simulation(tmp_path / "cancel.sqlite")
    try:
        leg = working(item)
        if fault == "exception":
            item.native.cancel_result = TimeoutError()
            assert cancel(item, leg).leg.cancel_state is c.CancelState.UNKNOWN
        else:
            item.native.cancel_result = SystemExit("simulated loss")
            with pytest.raises(SystemExit):
                cancel(item, leg)
        item.ledger.close()
        item.ledger = Ledger(tmp_path / "cancel.sqlite", clock=item.clock)
        item.executor = Executor(item.ledger, port=item.native, mode=c.ExecutionMode.SIMULATION,
                                 binding=item.context.binding, investor_handle=item.native.investor_handle, clock=item.clock)
        assert cancel(item, leg).code is c.ResultCode.IDEMPOTENT
        with pytest.raises(RuntimeError):
            cancel(item, leg, "SIM_RETRY_CANCEL")
        assert len(item.native.cancelled_ids) == 1
        assert item.ledger.check().reconcile.required
    finally:
        item.close()


@pytest.mark.parametrize("point", ["after_latch", "after_accept", "after_return"])
def test_E26_crash_latched_attempt_never_resends(tmp_path, monkeypatch, point):
    from ak_trading.infiny_execution.executor import Executor
    from ak_trading.infiny_execution.ledger import Ledger
    item = Simulation(tmp_path / "crash.sqlite")
    try:
        proposed = item.prepare()
        leg = proposed.legs[0]
        if point == "after_latch":
            original = item.ledger.begin_attempt
            def crash(*args, **kwargs):
                original(*args, **kwargs)
                raise SystemExit("simulated loss after commit")
            monkeypatch.setattr(item.ledger, "begin_attempt", crash)
        elif point == "after_accept":
            item.native.send_result = SystemExit("simulated loss after fake acceptance")
        else:
            def crash(*args, **kwargs):
                raise SystemExit("simulated loss before result persistence")
            monkeypatch.setattr(item.ledger, "record_submit_result", crash)
        with pytest.raises(SystemExit):
            item.submit(leg.leg_id)
        count = len(item.native.sent_requests)
        assert count == (0 if point == "after_latch" else 1)
        item.ledger.close()
        item.ledger = Ledger(tmp_path / "crash.sqlite", clock=item.clock)
        item.executor = Executor(item.ledger, port=item.native, mode=c.ExecutionMode.SIMULATION,
                                 binding=item.context.binding, investor_handle=item.native.investor_handle, clock=item.clock)
        assert item.submit(leg.leg_id).code is c.ResultCode.IDEMPOTENT
        assert len(item.native.sent_requests) == count
        assert item.ledger.check().reconcile.required
        assert item.ledger.view(item.context.binding.account_key).cash_reserved == D("2000")
    finally:
        item.close()


def test_E26_synchronous_callback_and_reentry_no_sql_transaction_across_send(sim):
    proposed = sim.prepare()
    leg = proposed.legs[0]
    def callback(request, local_id):
        # A second SQLite connection can acquire its writer lock during send.
        with sqlite3.connect(str(sim.ledger.path), timeout=0) as probe:
            probe.execute("BEGIN IMMEDIATE")
            probe.rollback()
        assert sim.submit(leg.leg_id).code is c.ResultCode.IDEMPOTENT
        sim.event(leg.leg_id, kind=c.EventKind.TRADE, trade_id="SIM_SYNC_T", volume=2, order_id=local_id)
        sim.event(leg.leg_id, status="全部成交", traded=2, order_id=local_id)
    sim.native.on_send = callback
    result = sim.submit(leg.leg_id)
    assert result.leg.state is c.OrderState.FILLED
    assert result.leg.trade_filled == 2 and result.leg.trade_gap == 0
    assert not result.leg.settled
    assert len(sim.native.sent_requests) == 1
    sim.barrier()
    assert sim.ledger.get_leg(leg.leg_id).settled


@pytest.mark.parametrize("change", ["quote", "authorization", "disabled", "policy", "budget", "thread", "intent"])
def test_E26_last_moment_revalidation_blocks_without_send(sim, change):
    proposed = sim.prepare()
    leg = proposed.legs[0]
    context = sim.context
    if change == "quote":
        sim.clock.advance(D("3.001"))
    elif change == "authorization":
        context = replace(context, authorization=replace(context.authorization, expires_at=sim.clock()))
    elif change == "disabled":
        sim.executor.disable()
    elif change == "policy":
        context = replace(context, policy=replace(context.policy, valid_until=sim.clock()))
    elif change == "budget":
        context = replace(context, policy=replace(context.policy, max_cash_reserve=D("1")))
    elif change == "intent":
        context = replace(context, intent=replace(context.intent, source_key="SIM_FORGED_SOURCE"))
    if change == "thread":
        errors = []
        def attempt():
            try:
                sim.submit(leg.leg_id)
            except NativeExecutionError as exc:
                errors.extend(exc.blockers)
        worker = Thread(target=attempt)
        worker.start()
        worker.join()
        assert errors == ["THREAD_INVALID"]
    else:
        with pytest.raises((NativeExecutionError, LedgerError)):
            sim.executor.submit_once(context.intent.intent_id, leg.leg_id, proposed.plan_id, context)
    assert sim.native.sent_requests == []


def test_E28_receipt_claims_own_reservation_once_and_rechecks_other_budget(sim):
    proposed = sim.prepare()
    assert sim.context.reservation_view.cash_reserved == D("2000")
    assert sim.context.reservation_view.gross_position == 0
    assert sim.submit(proposed.legs[0].leg_id).leg.state is c.OrderState.SUBMITTED_LOCAL
    assert len(sim.native.sent_requests) == 1


@pytest.mark.parametrize("end", ["cancel", "reject", "partial", "expired", "missing_details", "missing_snapshot"])
def test_E24_reverse_never_opens_on_incomplete_close(tmp_path, end):
    context = simulation_context(action=c.Action.REVERSE_TO_SHORT, long=3)
    item = Simulation(tmp_path / "reverse.sqlite", context=context)
    try:
        proposed = item.prepare()
        close, open_leg = proposed.legs
        item.submit(close.leg_id)
        item.event(close.leg_id)
        if end == "expired":
            item.clock.advance(D("4"))
        elif end in ("cancel", "partial"):
            item.event(close.leg_id, kind=c.EventKind.TRADE, trade_id="SIM_PART", volume=1)
            item.event(close.leg_id, status="部成部撤" if end == "cancel" else "部分成交", traded=1,
                       cancelled=2 if end == "cancel" else 0)
        elif end == "reject":
            item.event(close.leg_id, status="投资经理驳回", traded=0, cancelled=3)
        else:
            item.event(close.leg_id, status="全部成交", traded=3)
            if end == "missing_snapshot":
                item.event(close.leg_id, kind=c.EventKind.TRADE, trade_id="SIM_ALL", volume=3)
        with pytest.raises((NativeExecutionError, LedgerError)):
            item.refresh_open(open_leg.leg_id)
        with pytest.raises((NativeExecutionError, LedgerError)):
            item.submit(open_leg.leg_id)
        assert all(request["Offset"] != "0" for request in item.native.sent_requests)
    finally:
        item.close()


def test_E25_empty_own_journal_cannot_override_foreign_account_order(sim):
    b = sim.context.binding
    foreign = c.OrderInventoryItem(account_key=b.account_key, trading_day=b.trading_day, exchange=b.exchange,
                   instrument_id=b.instrument_id, hedge_flag=b.hedge_flag, direction=c.Direction.BUY,
                   offset=c.Offset.OPEN, order_id=999, order_sys_id="SIM_FOREIGN", total_volume=1,
                   traded_volume=0, cancel_volume=0, active=True)
    coverage = replace(sim.context.snapshot.coverage, account_active_count=1)
    proof = replace(sim.context.proof, order_inventory=(foreign,), position_snapshot_id="SIM_FOREIGN_SNAPSHOT")
    sim.context = replace(sim.context, proof=proof,
                          snapshot=replace(sim.context.snapshot, coverage=coverage,
                                           snapshot_id="SIM_FOREIGN_SNAPSHOT", proof_id=proof.proof_id),
                          readiness=replace(sim.context.readiness, coverage=coverage))
    assert sim.ledger.view(b.account_key).reservations == ()
    try:
        proposed = sim.prepare()
        with pytest.raises((NativeExecutionError, LedgerError)):
            sim.submit(proposed.legs[0].leg_id)
    except LedgerError as exc:
        assert "BASELINE_GAP" in exc.blockers or "PROOF_REQUIRED" in exc.blockers
    ready = sim.executor.readiness(sim.context)
    assert not ready.can_submit and ready.state is c.ReadinessState.RECONCILE_REQUIRED
    assert "BASELINE_GAP" in ready.blockers
    assert ready.coverage.account_active_count == 1 and ready.coverage.own_active_count == 0
    assert sim.native.sent_requests == []


@pytest.mark.parametrize("action", [c.Action.REVERSE_TO_LONG, c.Action.REVERSE_TO_SHORT])
@pytest.mark.parametrize("change", ["quote", "cash", "new_rate", "topology"])
def test_E24_reverse_refresh_checks_new_market_cash_and_preserves_order_terms(tmp_path, action, change):
    item = Simulation(tmp_path / "refresh.sqlite", context=simulation_context(
        action=action, long=3 if action is c.Action.REVERSE_TO_SHORT else 0,
        short=3 if action is c.Action.REVERSE_TO_LONG else 0))
    try:
        proposed = item.prepare()
        close, pending = proposed.legs
        item.submit(close.leg_id)
        item.fill(close.leg_id)
        assert item.ledger.get_leg(close.leg_id).settled
        assert len(item.native.sent_requests) == 1
        if change == "quote":
            item.clock.advance(D("3.001"))
        elif change == "cash":
            item.update_controls(replace(item.context, policy=replace(item.context.policy, max_cash_reserve=D("1999"))))
        elif change == "new_rate":
            item.update_controls(replace(item.context, policy=replace(item.context.policy, policy_id="SIM_POLICY_NEW",
                                                                      cash_per_open_lot=D("1500"))))
        else:
            item.update_controls(replace(item.context, policy=replace(item.context.policy, max_leg_qty=1)))
        if change != "new_rate":
            with pytest.raises((NativeExecutionError, LedgerError)):
                item.refresh_open(pending.leg_id)
            assert len(item.native.sent_requests) == 1
        else:
            fresh = item.refresh_open(pending.leg_id)
            assert fresh.plan_id != proposed.plan_id
            assert fresh.legs[-1].cash_reserve == D("3000")
            assert replace(fresh.legs[-1], cash_reserve=pending.cash_reserve) == pending
            assert fresh.legs[0] == close
            assert item.context.reservation_receipt.plan_id == fresh.plan_id
            item.submit(pending.leg_id)
            assert item.native.sent_requests[-1]["Volume"] == 2
            assert item.native.sent_requests[-1]["Offset"] == "0"
    finally:
        item.close()


@pytest.mark.parametrize("scope", ["account", "epoch", "day", "foreign"])
def test_E22_event_scope_cannot_grant_cancel_route(sim, scope):
    leg = working(sim)
    event = sim.events[0]
    if scope == "foreign":
        event = replace(event, ownership=c.Ownership.FOREIGN)
    else:
        field, value = {"account": ("account_key", "SIM_OTHER_ACCOUNT"),
                        "epoch": ("callback_session_epoch", "SIM_OLD_EPOCH"),
                        "day": ("trading_day", event.trading_day.replace(day=13))}[scope]
        event = replace(event, **{field: value})
    event = replace(event, event_id="SIM_UNRELATED_EVENT", order_id=999, order_sys_id="SIM_UNKNOWN_SYS",
                    memo="SIM_UNOWNED:OPEN:-:0")
    result = sim.ledger.ingest_event(event)
    assert result.code is c.ResultCode.QUARANTINED
    assert sim.native.cancelled_ids == []
    assert sim.ledger.get_leg(leg.leg_id).trade_filled == 0


@pytest.mark.parametrize("mutation", ["epoch", "day", "proof", "forged_receipt", "view"])
def test_E26_changed_epoch_day_or_forged_resources_cannot_send(sim, mutation):
    proposed = sim.prepare()
    ctx = sim.context
    if mutation in ("epoch", "day"):
        field, value = ("session_epoch", "SIM_RECONNECTED") if mutation == "epoch" else (
            "trading_day", ctx.binding.trading_day.replace(day=15))
        b = replace(ctx.binding, **{field: value})
        coverage = replace(ctx.snapshot.coverage, **{field: value})
        sample = replace(ctx.snapshot, binding=b, coverage=coverage,
                         quote=replace(ctx.snapshot.quote, binding=b), account=replace(ctx.snapshot.account, binding=b),
                         positions=tuple(replace(p, binding=b) for p in ctx.snapshot.positions))
        ctx = replace(ctx, binding=b, snapshot=sample, intent=replace(ctx.intent, binding=b),
                      readiness=replace(ctx.readiness, coverage=coverage))
    elif mutation == "proof":
        ctx = replace(ctx, proof=replace(ctx.proof, session_epoch="SIM_OLD"))
    elif mutation == "forged_receipt":
        ctx = replace(ctx, reservation_receipt=replace(ctx.reservation_receipt,
                                                      revision=ctx.reservation_receipt.revision+1))
    else:
        ctx = replace(ctx, reservation_view=replace(ctx.reservation_view, reservations=(), cash_reserved=D("0")),
                      policy=replace(ctx.policy, max_cash_reserve=D("1")))
    with pytest.raises((NativeExecutionError, LedgerError)):
        sim.executor.submit_once(ctx.intent.intent_id, proposed.legs[0].leg_id, proposed.plan_id, ctx)
    assert sim.native.sent_requests == []


def test_E28_foreign_reservation_transaction_recomputes_shared_cash(sim):
    from ak_trading.infiny_execution.planning import plan
    proposed = sim.prepare()
    first_context = sim.context
    other = replace(first_context.intent, intent_id="SIM_SECOND", source_key="SIM_SOURCE_SECOND")
    sim.ledger.register(other)
    context = replace(first_context, intent=other, reservation_receipt=None)
    sim.ledger.register_context(context)
    view = sim.ledger.view(context.binding.account_key, snapshot_id=context.snapshot.snapshot_id)
    second_plan = plan(other, context.snapshot, view, context.policy, sim.clock())
    sim.ledger.reserve(second_plan, view.ledger_revision)
    context = replace(first_context, policy=replace(first_context.policy, max_cash_reserve=D("3000")))
    sim.update_controls(context)
    context = sim.context
    with pytest.raises((NativeExecutionError, LedgerError), match="INSUFFICIENT_CASH"):
        sim.executor.submit_once(first_context.intent.intent_id, proposed.legs[0].leg_id, proposed.plan_id, context)
    assert sim.native.sent_requests == []


def test_E25_partial_account_coverage_and_heartbeat_cannot_clear_gap(sim):
    cov = replace(sim.context.snapshot.coverage, level=c.CoverageLevel.OWN_JOURNAL, strategy_key=sim.context.binding.strategy_key,
                  complete_through=None, completion_evidence_id=None, account_active_count=None)
    sim.context = replace(sim.context, snapshot=replace(sim.context.snapshot, coverage=cov,
                          snapshot_id="SIM_OWN_JOURNAL_SNAPSHOT"), proof=None,
                          readiness=replace(sim.context.readiness, coverage=cov, can_submit=False,
                                            state=c.ReadinessState.PLAN_READY, blockers=("PROOF_REQUIRED",)))
    sim.context = sim.executor.refresh(sim.context)
    ready = sim.executor.readiness(sim.context)
    assert not ready.can_submit and ready.coverage.account_active_count is None
    sim.ledger.note_event_gap("SIM_DISCONNECTED_EVENT_LOSS")
    ctx = sim.executor.refresh(sim.context)
    assert ctx.readiness.state is c.ReadinessState.RECONCILE_REQUIRED
    assert c.ReconcileReason.EVENT_GAP in ctx.reconcile.reasons
    sim.ledger.recover()
    assert sim.ledger.check().reconcile.required
    assert sim.native.sent_requests == []


def test_E21_observer_buy_and_enable_are_still_blocked(monkeypatch):
    import types
    native = types.ModuleType("INFINIGO")
    def forbidden(*args, **kwargs):
        raise AssertionError("observer reached native order method")
    native.sendOrder = forbidden
    native.cancelOrder = forbidden
    native.updateParam = lambda **kwargs: None
    native.updateState = lambda *args: None
    native.writeLog = lambda *args: None
    monkeypatch.setitem(sys.modules, "INFINIGO", native)
    spec = importlib.util.spec_from_file_location("_n4_observer_boundary", ROOT / "integrations/infiny/AIBridge.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    observer = module.AKObserverBridge(transport=lambda *args: (200, b'{"signal":"buy","reason":"test"}'))
    observer.set_params({"enable_trading": True})
    assert observer.params_map.enable_trading is False
    assert observer.send_order(exchange="SHFE", instrument_id="rb2610", volume=1, price=3500,
                               order_direction="buy", order_type="FAK", offset="OPEN") is None
    assert observer.cancel_order(7) is None
    observer._handle_response(module._WorkerResponse("analyze", 200, b'{"signal":"buy","reason":"test"}'))
    assert observer.state_map.last_signal == "hold"
    assert observer.state_map.order_submissions == 0


def test_E20_trade_full_then_order_full_then_late_pending_keeps_settled(sim):
    proposed = sim.prepare()
    leg = proposed.legs[0]
    sim.submit(leg.leg_id)
    sim.fill(leg.leg_id)
    record = sim.ledger.get_leg(leg.leg_id)
    assert record.state is c.OrderState.FILLED and record.settled
    late = sim.event(leg.leg_id, status="已报入未应答", traded=0)
    sim.ledger.ingest_event(late)
    record = sim.ledger.get_leg(leg.leg_id)
    assert record.state is c.OrderState.FILLED
    assert record.trade_filled == record.reported_filled == 2
    assert not sim.ledger.check().reconcile.required
    assert len(sim.native.sent_requests) == 1


@pytest.mark.parametrize("action,allowed", [(c.Action.OPEN_LONG, False), (c.Action.CLOSE_LONG, True)])
def test_E27_cash_rate_is_required_for_open_only(tmp_path, action, allowed):
    context = simulation_context(action=action, long=2 if allowed else 0)
    context = replace(context, policy=replace(context.policy, cash_per_open_lot=None, cash_bound_source=None))
    item = Simulation(tmp_path / "cash-bound.sqlite", context=context)
    try:
        if allowed:
            proposed = item.prepare()
            item.submit(proposed.legs[0].leg_id)
            assert len(item.native.sent_requests) == 1
        else:
            with pytest.raises(LedgerError, match="CASH_BOUND"):
                item.prepare()
            assert item.native.sent_requests == []
    finally:
        item.close()


def test_E20_public_six_action_demo_is_reproducible_and_explicitly_simulation(tmp_path):
    import json
    from ak_trading.infiny_execution.simulation import run_demo
    results = run_demo(tmp_path / "six-action-demo")
    report = [dict(item) for item in results]
    (tmp_path / "demo-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    assert {item["action"] for item in results} == {a.value for a in c.Action}
    assert sum(item["send_count"] for item in results) == 10
    assert sum(item["cancel_count"] for item in results) == 0
    assert all(item["source"] == "SIMULATION" and item["settled"] for item in results)
    assert all(item["readiness"]["native_submit_enabled"] is False and
               item["readiness"]["broker"] == "NOT_RUN" for item in results)


def test_E28_repeated_prepare_reuses_durable_plan_and_attempt(sim):
    first = sim.prepare()
    again = sim.executor.prepare(sim.context.intent, sim.context)
    assert again.plan == first
    assert again.context.reservation_receipt == sim.context.reservation_receipt
    sim.submit(first.legs[0].leg_id)
    after_send = sim.executor.prepare(sim.context.intent, sim.context)
    assert after_send.plan == first
    assert sim.submit(first.legs[0].leg_id).code is c.ResultCode.IDEMPOTENT
    assert len(sim.native.sent_requests) == 1


def test_E26_changed_intent_or_source_reuse_conflicts_durably(sim):
    sim.prepare()
    for changed in (replace(sim.context.intent, volume=3),
                    replace(sim.context.intent, intent_id="SIM_NEW_UUID")):
        with pytest.raises(LedgerError) as caught:
            sim.ledger.register(changed)
        assert caught.value.code is c.ResultCode.INTENT_CONFLICT
    assert sim.native.sent_requests == []


@pytest.mark.parametrize("control", ["authorization", "policy", "mode", "route"])
def test_E26_late_context_cannot_restore_revoked_control(sim, control):
    proposed = sim.prepare()
    old = sim.context
    if control == "authorization":
        new = replace(old, authorization=None)
    elif control == "policy":
        new = replace(old, policy=replace(old.policy, max_cash_reserve=D("1")))
    elif control == "route":
        new = replace(old, route_ownership=None)
    else:
        new = replace(old, mode=c.ExecutionMode.DISABLED, authorization=None,
                      readiness=replace(old.readiness, mode=c.ExecutionMode.DISABLED, can_submit=False,
                                        can_cancel_owned=False, state=c.ReadinessState.PLAN_READY))
    sim.ledger.register_context(new)
    with pytest.raises(NativeExecutionError, match="AUTHORIZATION_REQUIRED"):
        sim.submit(proposed.legs[0].leg_id)
    assert sim.ledger.context(old.binding) == new
    assert sim.native.sent_requests == []


def test_E22_cancel_cannot_restore_revoked_authorization(sim):
    leg = working(sim)
    sim.update_controls(replace(sim.context, authorization=None))
    with pytest.raises((NativeExecutionError, LedgerError), match="AUTHORIZATION_REQUIRED"):
        cancel(sim, leg)
    assert sim.ledger.context(sim.context.binding).authorization is None
    assert sim.native.cancelled_ids == []


def test_E26_disable_between_validation_and_attempt_prevents_send(sim, monkeypatch):
    from ak_trading.infiny_execution import executor as module
    proposed = sim.prepare()
    original = module.validate_plan
    def validate(*args):
        result = original(*args)
        sim.executor.disable()
        return result
    monkeypatch.setattr(module, "validate_plan", validate)
    with pytest.raises(NativeExecutionError, match="MODE_DISABLED"):
        sim.submit(proposed.legs[0].leg_id)
    assert sim.ledger.get_leg(proposed.legs[0].leg_id).attempt_no == 0
    assert sim.native.sent_requests == []


def test_E26_new_snapshot_with_same_control_can_submit_frozen_leg(sim):
    proposed = sim.prepare()
    sim.barrier()
    assert sim.context.snapshot.snapshot_id != proposed.snapshot_id
    result = sim.submit(proposed.legs[0].leg_id)
    assert result.leg.leg == proposed.legs[0]
    assert len(sim.native.sent_requests) == 1


@pytest.mark.parametrize("control", ["authorization", "policy", "epoch"])
@pytest.mark.parametrize("operation", ["submit", "cancel"])
def test_E26_two_connections_cas_rejects_control_change_without_overwriting(tmp_path, monkeypatch, control, operation):
    from ak_trading.infiny_execution.ledger import Ledger
    item = Simulation(tmp_path / "race.sqlite")
    other = Ledger(tmp_path / "race.sqlite", clock=item.clock)
    try:
        if operation == "cancel":
            leg = working(item)
        else:
            leg = item.prepare().legs[0]
        original = item.ledger.compare_and_register_context
        observed = []
        def race(context, expected_context, **kwargs):
            new = expected_context
            if control == "authorization":
                new = replace(new, authorization=None)
            elif control == "policy":
                new = replace(new, policy=replace(new.policy, max_cash_reserve=D("1")))
            else:
                binding = replace(new.binding, session_epoch="SIM_EPOCH_NEXT")
                coverage = replace(new.snapshot.coverage, session_epoch=binding.session_epoch)
                snapshot = replace(new.snapshot, snapshot_id="SIM_EPOCH_SNAPSHOT", binding=binding, coverage=coverage,
                                   quote=replace(new.snapshot.quote, binding=binding),
                                   account=replace(new.snapshot.account, binding=binding),
                                   positions=tuple(replace(p, binding=binding) for p in new.snapshot.positions))
                new = replace(new, binding=binding, snapshot=snapshot, proof=None, authorization=None,
                              intent=replace(new.intent, binding=binding), reservation_view=None,
                              readiness=replace(new.readiness, coverage=coverage, can_submit=False,
                                                state=c.ReadinessState.PLAN_READY))
            other.register_context(new)
            observed.append(new)
            return original(context, expected_context, **kwargs)
        monkeypatch.setattr(item.ledger, "compare_and_register_context", race)
        with pytest.raises(LedgerError):
            item.submit(leg.leg_id) if operation == "submit" else cancel(item, leg)
        assert len(observed) == 1
        assert other.context(item.context.binding) == observed[0]
        assert item.native.cancelled_ids == []
        assert len(item.native.sent_requests) == (1 if operation == "cancel" else 0)
    finally:
        other.close()
        item.close()


def revoked_control(base, control):
    """A control change another connection legitimately commits first."""
    if control == "authorization":
        return replace(base, authorization=None)
    if control == "policy":
        return replace(base, policy=replace(base.policy, max_cash_reserve=D("1")))
    if control == "disabled":
        return replace(base, mode=c.ExecutionMode.DISABLED, authorization=None,
                       readiness=replace(base.readiness, mode=c.ExecutionMode.DISABLED, can_submit=False,
                                         can_cancel_owned=False, state=c.ReadinessState.PLAN_READY))
    binding = replace(base.binding, session_epoch="SIM_EPOCH_NEXT")
    coverage = replace(base.snapshot.coverage, session_epoch=binding.session_epoch)
    snapshot = replace(base.snapshot, snapshot_id="SIM_EPOCH_SNAPSHOT", binding=binding, coverage=coverage,
                       quote=replace(base.snapshot.quote, binding=binding),
                       account=replace(base.snapshot.account, binding=binding),
                       positions=tuple(replace(p, binding=binding) for p in base.snapshot.positions))
    return replace(base, binding=binding, snapshot=snapshot, proof=None, authorization=None,
                   intent=replace(base.intent, binding=binding), reservation_view=None,
                   readiness=replace(base.readiness, coverage=coverage, can_submit=False,
                                     state=c.ReadinessState.PLAN_READY))


@pytest.mark.parametrize("control", ["authorization", "policy", "disabled", "epoch"])
def test_F01_refresh_entry_cannot_roll_back_concurrent_control(tmp_path, monkeypatch, control):
    """Hard-review F01: reverse refresh must CAS, never unconditionally register.

    Connection B commits a control change after A read the old context and
    before A uses it. A must reject without writes, attempts or sends, and B's
    control stays authoritative in the shared journal.
    """
    from ak_trading.infiny_execution.ledger import Ledger
    item = Simulation(tmp_path / "refresh-race.sqlite", context=simulation_context(
        action=c.Action.REVERSE_TO_SHORT, long=3))
    other = Ledger(tmp_path / "refresh-race.sqlite", clock=item.clock)
    try:
        proposed = item.prepare()
        closes = tuple(leg for leg in proposed.legs if leg.phase is c.Phase.CLOSE)
        pending = tuple(leg for leg in proposed.legs if leg.phase is c.Phase.OPEN)[0]
        for leg in closes:
            item.submit(leg.leg_id)
            item.fill(leg.leg_id)
        sends = len(item.native.sent_requests)
        assert sends == len(closes)
        state = {}
        original = item.ledger.context

        def racing_read(binding):
            persisted = original(binding)
            if persisted is not None and "stale" not in state:
                state["stale"] = persisted
                state["b"] = revoked_control(persisted, control)
                other.register_context(state["b"])
            return state.get("stale", persisted)

        monkeypatch.setattr(item.ledger, "context", racing_read)
        with pytest.raises((NativeExecutionError, LedgerError)):
            item.refresh_open(pending.leg_id)
        assert c.canonical_hash(other.context(item.context.binding)) == c.canonical_hash(state["b"])
        record = item.ledger.get_leg(pending.leg_id)
        assert record.attempt_no == 0 and record.state is c.OrderState.RESERVED
        assert len(item.native.sent_requests) == sends
        assert item.native.cancelled_ids == []
    finally:
        other.close()
        item.close()


@pytest.mark.parametrize("control", ["authorization", "policy", "disabled", "epoch"])
def test_F01_initial_prepare_never_clobbers_a_concurrent_registration(tmp_path, monkeypatch, control):
    """Hard-review F01: an ordinary first prepare may not self-initialize.

    An empty scope read is not initialization permission; a registration that
    another connection commits in between must survive untouched.
    """
    from ak_trading.infiny_execution.executor import Executor
    from ak_trading.infiny_execution.ledger import Ledger
    clock = FakeClock()
    base = simulation_context(clock=clock)
    native = FakeNative(base.binding)
    ledger = Ledger(tmp_path / "init-race.sqlite", clock=clock)
    other = Ledger(tmp_path / "init-race.sqlite", clock=clock)
    try:
        executor = Executor(ledger, port=native, mode=c.ExecutionMode.SIMULATION,
                            binding=base.binding, investor_handle=native.investor_handle, clock=clock)
        state = {}
        original = ledger.context

        def racing_read(binding):
            if "b" not in state:
                state["b"] = revoked_control(base, control)
                other.register_context(state["b"])
            return None

        monkeypatch.setattr(ledger, "context", racing_read)
        with pytest.raises(NativeExecutionError, match="CONTEXT_REQUIRED"):
            executor.prepare(base.intent, base)
        assert c.canonical_hash(other.context(base.binding)) == c.canonical_hash(state["b"])
        assert ledger.view(base.binding.account_key).reservations == ()
        assert native.sent_requests == [] and native.cancelled_ids == []
    finally:
        ledger.close()
        other.close()


@pytest.mark.parametrize("control", ["authorization", "policy", "disabled", "epoch"])
def test_F01_reopen_never_reinitializes_existing_control_scope(tmp_path, control):
    """Hard-review r2 F01: a reopened assembler must not reset a durable scope.

    B commits a control change on the shared journal; constructing Simulation
    with stale A must reject without writes, keep B authoritative, and permit
    no attempt or fake send.
    """
    from ak_trading.infiny_execution.ledger import Ledger
    path = tmp_path / "reopen-existing.sqlite"
    first = Simulation(path)
    other = Ledger(path, clock=first.clock)
    reopened = None
    try:
        stale = first.context
        changed = revoked_control(stale, control)
        other.register_context(changed)
        sends = attempt = 0
        with pytest.raises((NativeExecutionError, LedgerError)):
            reopened = Simulation(path, context=stale, clock=first.clock)
            proposed = reopened.prepare()
            attempt = reopened.submit(proposed.legs[0].leg_id).leg.attempt_no
            sends = len(reopened.native.sent_requests)
        assert other.context(stale.binding) == changed
        assert sends == attempt == 0
        fresh = Ledger(path, clock=first.clock)
        try:
            assert fresh.context(stale.binding) == changed
            assert fresh.view(stale.binding.account_key).reservations == ()
        finally:
            fresh.close()
    finally:
        if reopened is not None:
            reopened.close()
        other.close()
        first.close()


def test_F01_reopen_with_same_controls_adopts_durable_scope(tmp_path):
    """Legitimate reopen adopts the durable scope and continues idempotently."""
    path = tmp_path / "reopen-adopt.sqlite"
    first = Simulation(path)
    proposed = first.prepare()
    carried = first.context
    first.close()
    reopened = Simulation(path, context=replace(carried, reservation_view=None), clock=first.clock)
    try:
        again = reopened.prepare()
        assert again == proposed
        result = reopened.submit(proposed.legs[0].leg_id)
        assert result.leg.state is c.OrderState.SUBMITTED_LOCAL
        assert len(reopened.native.sent_requests) == 1
    finally:
        reopened.close()


@pytest.mark.parametrize("control", ["authorization", "policy", "disabled", "epoch"])
def test_F01_virgin_scope_initialization_is_atomic_absent_only(tmp_path, monkeypatch, control):
    """A registration committed after the absent read survives initialization.

    The read that sees no scope and the initializing write are separate
    operations; a control change another connection commits in between must
    win atomically, with the late initializer rejecting on zero writes.
    """
    from ak_trading.infiny_execution import ledger as ledger_module
    from ak_trading.infiny_execution.ledger import Ledger
    path = tmp_path / "init-atomic.sqlite"
    clock = FakeClock()
    base = simulation_context(clock=clock)
    other = Ledger(path, clock=clock)
    item = None
    try:
        injected = []
        original = ledger_module.Ledger.context

        def racing_read(self, binding):
            value = original(self, binding)
            if value is None and not injected:
                injected.append(revoked_control(base, control))
                other.register_context(injected[0])
            return value

        monkeypatch.setattr(ledger_module.Ledger, "context", racing_read)
        with pytest.raises((NativeExecutionError, LedgerError)):
            item = Simulation(path, context=base, clock=clock)
        assert other.context(base.binding) == injected[0]
        fresh = Ledger(path, clock=clock)
        try:
            assert fresh.context(base.binding) == injected[0]
            assert fresh.view(base.binding.account_key).reservations == ()
        finally:
            fresh.close()
    finally:
        if item is not None:
            item.close()
        other.close()


def test_F07_refresh_expiry_takes_earliest_component_deadline(tmp_path):
    """Hard-review F07 counterexample: contract age 3599.5s of TTL 3600s.

    The refreshed plan must expire at now+0.5s (earliest component deadline)
    and remain submittable inside that window; a now+3s plan is immediately
    EXPIRED and blocks a legitimate reverse OPEN. Stays red until the ledger
    fix lands, then serves as its regression.
    """
    from ak_trading.infiny_execution.planning import validate_plan
    item = Simulation(tmp_path / "near-expiry.sqlite", context=simulation_context(
        action=c.Action.REVERSE_TO_SHORT, long=3))
    try:
        proposed = item.prepare()
        closes = tuple(leg for leg in proposed.legs if leg.phase is c.Phase.CLOSE)
        pending = tuple(leg for leg in proposed.legs if leg.phase is c.Phase.OPEN)[0]
        for leg in closes:
            item.submit(leg.leg_id)
            item.fill(leg.leg_id)
        fresh = item.barrier()
        ident = "SIM_NEAR_CONTRACT_SNAPSHOT"
        now = item.clock()
        revision = item.ledger.check().revision
        timing = c.ComponentTiming(sampled_at=now, source_at=None, received_at=now, snapshot_id=ident,
                                   source=c.Source.SIMULATION, completeness=c.Completeness.COMPLETE)
        near = replace(fresh.snapshot, snapshot_id=ident, ledger_revision=revision, proof_id="SIM_PROOF_" + ident,
                       times=replace(fresh.snapshot.times,
                                     contract=replace(timing, sampled_at=now - timedelta(seconds=3599.5)),
                                     positions=timing),
                       positions=tuple(replace(p, timing=timing) for p in fresh.snapshot.positions))
        near_context = replace(fresh, snapshot=near, proof=replace(fresh.proof, proof_id=near.proof_id,
                                                                   position_snapshot_id=ident),
                               ledger_revision=revision, reservation_view=None)
        expected = item.executor.persisted_context(near_context)
        item.context = item.executor.latest_context(near_context, expected_context=expected)
        refreshed = item.refresh_open(pending.leg_id)
        assert refreshed.expires_at == now + timedelta(milliseconds=500)
        assert validate_plan(refreshed, item.context, item.clock()).ok
        result = item.submit(pending.leg_id)
        assert result.leg.state is c.OrderState.SUBMITTED_LOCAL
    finally:
        item.close()

