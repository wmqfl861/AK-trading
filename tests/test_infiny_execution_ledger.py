"""E11-E19/E27/E28 offline counterexamples with real SQLite connections."""
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from ak_trading.infiny_execution import contracts as c
from ak_trading.infiny_execution.ledger import ContextReceipt, Ledger, LedgerError
from ak_trading.infiny_execution.reconcile import CallbackContext, Reconciler, normalize_event

NOW = datetime(2026, 9, 14, 1, 30, tzinfo=timezone.utc)
D = Decimal


class FakeClock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def fixture(*, name="one", instrument="rb2610", account="SIM_A", strategy="SIM_S", epoch="SIM_E",
            action=c.Action.OPEN_LONG, volume=2, position=0, yd=0, frozen=0, yd_frozen=0,
            available="100000", max_cash="100000", max_gross=100, max_inflight=1, now=NOW):
    b = c.Binding(account_key=account, strategy_key=strategy, strategy_id=23, instance_id="SIM_I", session_epoch=epoch,
                  trading_day=now.date(), exchange="SHFE", instrument_id=instrument, hedge_flag="1")
    sid = "snapshot-" + name
    wm = c.SourceWatermark(source=c.Source.SIMULATION, stream_id="SIM_STREAM", value=name, as_of=now)
    cov = c.Coverage(level=c.CoverageLevel.FULL_ACCOUNT, account_key=account, strategy_key=None,
                     session_epoch=epoch, trading_day=now.date(), source=c.Source.SIMULATION,
                     as_of=now, complete_through=wm, completion_evidence_id="barrier-" + name,
                     own_active_count=0, account_active_count=0)
    timing = c.ComponentTiming(sampled_at=now, source_at=None, received_at=now, snapshot_id=sid,
                               source=c.Source.SIMULATION, completeness=c.Completeness.COMPLETE)
    p = c.RiskPolicy(policy_id="policy-" + name, valid_until=now + timedelta(days=1), max_leg_qty=5,
                      max_gross_position=max_gross, max_inflight_legs=max_inflight, max_cash_reserve=D(max_cash),
                      cash_per_open_lot=D(1000), fee_per_close_lot=D(10),
                      cash_bound_source=c.CashBoundSource(source=c.Source.SIMULATION, evidence_id="cash-bound", verified=True))
    positions = tuple(c.PositionBucket(binding=b, side=side, timing=timing, Position=position if side is c.Side.LONG else 0,
                                      PositionClose=position if side is c.Side.LONG else 0,
                                      YdPositionClose=yd if side is c.Side.LONG else 0,
                                      FrozenClosing=frozen if side is c.Side.LONG else 0,
                                      YdFrozenClosing=yd_frozen if side is c.Side.LONG else 0) for side in c.Side)
    s = c.ExecutionSnapshot(snapshot_id=sid, binding=b, source=c.Source.SIMULATION,
                            times=c.SnapshotTimes(sampled_at=now, sent_at=now, received_at=now, quote=replace(timing, source_at=now),
                                                  contract=timing, account=timing, positions=timing),
                            quote=c.Quote(binding=b, time=now, last=D(3500), bid1=D(3500), ask1=D(3501), bid_volume1=10,
                                          ask_volume1=10, upper_limit=D(4000), lower_limit=D(3000), market_status=c.MarketStatus.CONTINUOUS),
                            contract=c.Contract(exchange=b.exchange, instrument_id=instrument, asset_class=c.AssetClass.FUTURE,
                                                expiry=date(2026, 10, 15), price_tick=D(1), multiplier=D(10), min_limit_order_volume=1,
                                                max_limit_order_volume=5), account=c.Account(binding=b, available=D(available), currency="CNY"),
                            positions=positions, coverage=cov, proof_id="proof-" + name, ledger_revision=0)
    intent = c.OrderIntent(intent_id="intent-" + name, source_key="decision-" + name, binding=b, action=action, volume=volume,
                           prices=c.Prices(open=D(3500), close=D(3500)), close_policy=c.ClosePolicy.AUTO_YD_FIRST,
                           order_type=c.OrderType.GFD, created_at=now, expires_at=now + timedelta(days=1))
    proof = c.ReconciliationProof(proof_id=s.proof_id, source=c.Source.SIMULATION, account_key=account, trading_day=b.trading_day,
                                  session_epoch=epoch, as_of=now, order_inventory=(), trade_inventory=(), position_snapshot_id=sid,
                                  complete_through=wm, exclusive_writer_verified=True, verified=True)
    auth = c.Authorization(authorization_id="auth-" + name, mode=c.ExecutionMode.SIMULATION, account_key=account,
                            strategy_key=strategy, session_epoch=epoch, symbols=(c.Symbol(exchange=b.exchange, instrument_id=instrument),),
                            actions=tuple(c.AuthorizationAction), max_volume=100,
                            price_bounds=(c.PriceBound(symbol=c.Symbol(exchange=b.exchange, instrument_id=instrument), lower=D(3000), upper=D(4000)),),
                            expires_at=now + timedelta(days=1))
    ready = c.Readiness(connection=c.ConnectionState.ROUNDTRIP, state=c.ReadinessState.EXECUTABLE, can_plan=True, can_submit=True,
                        can_cancel_owned=True, blockers=(), mode=c.ExecutionMode.SIMULATION, coverage=cov, evidence_id="ready")
    return c.ExecutionContext(mode=c.ExecutionMode.SIMULATION, binding=b, snapshot=s, proof=proof, policy=p,
                               authorization=auth, reservation_receipt=None, owner_thread_id=17, current_thread_id=17,
                               clock=c.ClockReading(now=now, monotonic_ns=100, domain_id="clock"), ledger_revision=0,
                               route_ownership=c.RouteOwnership(route="cancel", evidence_id="simulation-route", verified=True),
                               readiness=ready, reconcile=c.ReconcileState(), intent=intent)


def hand_plan(ctx, *, quantities=None):
    intent = ctx.intent
    reverse = intent.action in (c.Action.REVERSE_TO_LONG, c.Action.REVERSE_TO_SHORT)
    opening = intent.action in (c.Action.OPEN_LONG, c.Action.OPEN_SHORT)
    legs = []
    if not opening:
        total = ctx.snapshot.positions[0].Position if reverse else intent.volume
        for qty in quantities or (total,):
            bucket = c.Bucket.YD if ctx.snapshot.positions[0].YdPositionClose else c.Bucket.TD
            index = sum(x.position_bucket is bucket for x in legs)
            legs.append(c.Leg(leg_id=f"{intent.intent_id}:CLOSE:{bucket.value}:{index}", index=len(legs), phase=c.Phase.CLOSE,
                               depends_on=(), direction=c.Direction.SELL, offset=c.Offset.CLOSE_YESTERDAY if bucket is c.Bucket.YD else c.Offset.CLOSE_TODAY,
                               volume=qty, price=D(3500), position_bucket=bucket, cash_reserve=qty * (ctx.policy.fee_per_close_lot or D(0))))
    if opening or reverse:
        for index, qty in enumerate(quantities or (intent.volume,)):
            legs.append(c.Leg(leg_id=f"{intent.intent_id}:OPEN:-:{index}", index=len(legs), phase=c.Phase.OPEN,
                               depends_on=tuple(x.leg_id for x in legs if x.phase is c.Phase.CLOSE),
                               direction=c.Direction.BUY if intent.action in (c.Action.OPEN_LONG, c.Action.REVERSE_TO_LONG) else c.Direction.SELL,
                               offset=c.Offset.OPEN, volume=qty, price=D(3500), position_bucket=None,
                               cash_reserve=qty * (ctx.policy.cash_per_open_lot or D(0))))
    digest = hashlib.sha256((c.canonical_hash(intent) + ctx.snapshot.snapshot_id + str(ctx.ledger_revision)).encode()).hexdigest()
    return c.Plan(plan_id=digest, intent_hash=c.canonical_hash(intent), snapshot_id=ctx.snapshot.snapshot_id,
                   ledger_revision=ctx.ledger_revision, policy_id=ctx.policy.policy_id, expires_at=ctx.clock.now + timedelta(seconds=3),
                   legs=tuple(legs), blockers=())


def sync(ledger, ctx, receipt=None):
    revision = ledger.check().revision
    ctx = replace(ctx, snapshot=replace(ctx.snapshot, ledger_revision=revision), ledger_revision=revision, reservation_view=None,
                  reservation_receipt=receipt or ctx.reservation_receipt)
    ledger.register_context(ctx)
    ctx = replace(ctx, reservation_view=ledger.view(ctx.binding.account_key, snapshot_id=ctx.snapshot.snapshot_id))
    ledger.register_context(ctx)
    return ctx


def reserve(ledger, ctx, *, quantities=None):
    ledger.register(ctx.intent)
    ctx = sync(ledger, ctx)
    plan = hand_plan(ctx, quantities=quantities)
    receipt = ledger.reserve(plan, ctx.ledger_revision)
    ctx = sync(ledger, ctx, receipt)
    return ctx, plan


def start(ledger, ctx, *, result=7):
    ctx, plan = reserve(ledger, ctx)
    attempt = ledger.begin_attempt(plan.legs[0].leg_id, ctx)
    assert attempt.send_permitted
    if result != "unrecorded":
        ledger.record_submit_result(plan.legs[0].leg_id, result)
    return ctx, plan


def event(ctx, leg, *, event_id="event-1", kind=c.EventKind.ORDER, order_id=7, status=c.OrderState.WORKING,
          traded=0, cancelled=0, trade_id=None, volume=None, day="default", epoch=None, account=None, instrument=None,
          memo="default", price="3500", now=NOW, sys_id="0007"):
    raw = {"Exchange": ctx.binding.exchange, "InstrumentID": instrument or ctx.binding.instrument_id,
           "Hedgeflag": ctx.binding.hedge_flag, "OrderID": order_id, "OrderSysID": sys_id,
           "Memo": leg.leg_id if memo == "default" else memo, "TradingDay": ctx.binding.trading_day if day == "default" else day,
           "Direction": leg.direction, "Offset": leg.offset, "Status": status, "Price": price,
           "TotalVolume": leg.volume if kind is c.EventKind.ORDER else None,
           "TradedVolume": traded if kind is c.EventKind.ORDER else None, "CancelVolume": cancelled if kind is c.EventKind.ORDER else None,
           "TradeID": trade_id, "Volume": volume}
    callback = CallbackContext(event_id=event_id, kind=kind, received_at=now, session_epoch=epoch or ctx.binding.session_epoch,
                                source=c.Source.SIMULATION, account_key=account or ctx.binding.account_key,
                                strategy_key=ctx.binding.strategy_key, simulation_scope=True)
    return normalize_event(raw, callback)


def absorb(ledger, ctx, leg, clock, *, position=0, frozen=0, terminal=True, available="100000"):
    clock.advance(0.1)
    record = ledger.get_leg(leg.leg_id)
    name = "absorb-" + str(ledger.check().revision)
    fresh = fixture(name=name, action=ctx.intent.action, volume=ctx.intent.volume, position=position, frozen=frozen,
                    available=available, now=clock.now)
    fresh = replace(fresh, intent=ctx.intent, policy=ctx.policy, authorization=ctx.authorization)
    b = ctx.binding
    order = c.OrderInventoryItem(account_key=b.account_key, trading_day=b.trading_day, exchange=b.exchange,
                                 instrument_id=b.instrument_id, hedge_flag=b.hedge_flag, direction=leg.direction, offset=leg.offset,
                                 order_id=record.order_id, order_sys_id=record.order_sys_id, total_volume=leg.volume,
                                 traded_volume=record.risk_filled, cancel_volume=record.cancel_volume or 0, active=not terminal)
    with sqlite3.connect(ledger.path) as db:
        rows = db.execute("SELECT trade_id,volume,price FROM trades WHERE leg_id=?", (leg.leg_id,)).fetchall()
    trades = tuple(c.TradeInventoryItem(account_key=b.account_key, trading_day=b.trading_day, exchange=b.exchange,
                                        instrument_id=b.instrument_id, hedge_flag=b.hedge_flag, direction=leg.direction, offset=leg.offset,
                                        order_id=record.order_id, order_sys_id=record.order_sys_id, trade_id=x[0], volume=x[1], price=D(x[2])) for x in rows)
    proof = replace(fresh.proof, order_inventory=(order,), trade_inventory=trades, covered_leg_ids=(leg.leg_id,))
    fresh = replace(fresh, proof=proof)
    fresh = sync(ledger, fresh, ctx.reservation_receipt)
    ledger.recover(proof)
    return sync(ledger, fresh, ctx.reservation_receipt)


def counts(path):
    with sqlite3.connect(path) as db:
        return {name: db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0] for name in ("intents", "legs", "events", "trades", "reservations")}


@pytest.mark.parametrize("cash_case", [False, True], ids=["same-close-bucket", "cross-contract-cash"])
def test_E11_two_independent_connections_real_concurrent_atomic_reserve(tmp_path, cash_case):
    path = tmp_path / "ledger.db"
    clock = FakeClock()
    first = Ledger(path, clock=clock)
    second = Ledger(path, clock=clock)
    args = dict(action=c.Action.OPEN_LONG, available="3000", max_cash="3000") if cash_case else dict(action=c.Action.CLOSE_LONG, position=3)
    a = fixture(name="a", **args)
    b = fixture(name="b", instrument="cu2610" if cash_case else "rb2610", **args)
    if not cash_case:
        b = replace(b, snapshot=a.snapshot, proof=a.proof, policy=a.policy)
    try:
        for db, ctx in ((first, a), (second, b)):
            db.register(ctx.intent)
            db.register_context(ctx)
        plans = (hand_plan(a), hand_plan(b))
        gate = threading.Barrier(2)
        def run(db, plan):
            gate.wait(timeout=5)
            try:
                return db.reserve(plan, 0)
            except LedgerError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda pair: run(*pair), ((first, plans[0]), (second, plans[1]))))
        assert sum(type(x) is c.ReservationReceipt for x in results) == 1
        assert counts(path)["legs"] == counts(path)["reservations"] == 1
        assert first.view("SIM_A").cash_reserved <= D(3000)
        loser = 1 if type(results[0]) is c.ReservationReceipt else 0
        ctx = (a, b)[loser]
        # Retry at the new revision proves resource exhaustion, not just optimistic locking.
        ctx = sync(second, ctx)
        with pytest.raises(LedgerError) as caught:
            second.reserve(hand_plan(ctx), ctx.ledger_revision)
        assert ("INSUFFICIENT_CASH" if cash_case else "INSUFFICIENT_POSITION") in caught.value.blockers
        assert counts(path)["legs"] == 1
    finally:
        first.close()
        second.close()


def test_E12_held_freeze_is_conservative_until_exact_full_barrier(tmp_path):
    clock = FakeClock()
    with Ledger(tmp_path / "ledger.db", clock=clock) as db:
        ctx, plan = start(db, fixture(action=c.Action.CLOSE_LONG, position=5))
        leg = plan.legs[0]
        db.ingest_event(event(ctx, leg))
        fresh = fixture(name="freeze", action=c.Action.CLOSE_LONG, position=5, frozen=2)
        db.register_context(fresh)
        held = db.view("SIM_A").reservations[0]
        assert held.state is c.ReservationState.HELD and held.not_proven_reflected == 2
        other = replace(fresh, intent=replace(fresh.intent, volume=2))
        db.register(other.intent)
        other = sync(db, other)
        with pytest.raises(LedgerError, match="INSUFFICIENT_POSITION"):
            # sdk_available=3 minus local2 -> only1.
            db.reserve(hand_plan(other), other.ledger_revision)
        ctx = absorb(db, ctx, leg, clock, position=5, frozen=2, terminal=False)
        reflected = db.view("SIM_A").reservations[0]
        assert reflected.state is c.ReservationState.REFLECTED and reflected.not_proven_reflected == 0
        db.ingest_event(event(ctx, leg, event_id="partial", status=c.OrderState.PARTIAL, traded=1, now=clock.now))
        assert db.get_leg(leg.leg_id).trade_gap == 1
        assert db.view("SIM_A").reservations[0].state is not c.ReservationState.RELEASED


def test_E13_canonical_idempotency_100_times_and_changed_payload_source_uuid_conflicts(tmp_path):
    clock = FakeClock()
    ctx = fixture()
    with Ledger(tmp_path / "ledger.db", clock=clock) as db:
        for index in range(100):
            result = db.register(ctx.intent)
            assert result.code is (c.ResultCode.OK if index == 0 else c.ResultCode.IDEMPOTENT)
        assert counts(db.path)["intents"] == 1
        for altered in (replace(ctx.intent, volume=3), replace(ctx.intent, prices=c.Prices(open=D(3501), close=D(3500))),
                        replace(ctx.intent, intent_id="new-uuid"), replace(ctx.intent, source_key="new-source", volume=4)):
            with pytest.raises(LedgerError) as caught:
                db.register(altered)
            assert caught.value.code is c.ResultCode.INTENT_CONFLICT
        ctx, plan = reserve(db, ctx)
        assert db.reserve(plan, 0) == ctx.reservation_receipt
        sends = sum(db.begin_attempt(plan.legs[0].leg_id, ctx).send_permitted for _ in range(100))
        assert sends == 1
        with pytest.raises(FrozenInstanceError):
            result.code = c.ResultCode.OK


@pytest.mark.parametrize("window", ["reserved", "latched", "accepted", "returned"], ids=["after-reserve", "before-send", "before-return", "before-result-commit"])
def test_E14_crash_windows_never_resend_latched_attempt(tmp_path, window):
    clock = FakeClock()
    path = tmp_path / "ledger.db"
    db = Ledger(path, clock=clock)
    ctx, plan = reserve(db, fixture())
    leg = plan.legs[0]
    sends = 0
    if window != "reserved":
        assert db.begin_attempt(leg.leg_id, ctx).send_permitted
        sends = int(window in ("accepted", "returned"))
    db.close()
    with Ledger(path, clock=clock) as reopened:
        ctx = sync(reopened, ctx)
        if window == "reserved":
            assert reopened.begin_attempt(leg.leg_id, ctx).send_permitted
        else:
            before = sends
            outcome = reopened.begin_attempt(leg.leg_id, ctx)
            sends += outcome.send_permitted
            assert sends == before and outcome.leg.state is c.OrderState.SUBMIT_UNKNOWN
            assert reopened.recover().reconcile.required
        assert reopened.view("SIM_A").cash_reserved == D(2000)


@pytest.mark.parametrize("value,expected,cash", [(7, c.OrderState.SUBMITTED_LOCAL, 2000), (-1, c.OrderState.LOCAL_REJECTED, 0),
                                                (None, c.OrderState.SUBMIT_UNKNOWN, 2000), (True, c.OrderState.SUBMIT_UNKNOWN, 2000),
                                                (False, c.OrderState.SUBMIT_UNKNOWN, 2000), (7.0, c.OrderState.SUBMIT_UNKNOWN, 2000),
                                                (-2, c.OrderState.SUBMIT_UNKNOWN, 2000)])
def test_E15_submit_result_classes_and_ten_second_confirmation_timeout(tmp_path, value, expected, cash):
    clock = FakeClock()
    with Ledger(tmp_path / "ledger.db", clock=clock) as db:
        ctx, plan = start(db, fixture(), result=value)
        assert db.get_leg(plan.legs[0].leg_id).state is expected
        assert db.view("SIM_A").cash_reserved == D(cash)
        if type(value) is int and value == 7:
            clock.advance(10)
            assert c.ReconcileReason.SUBMIT_UNKNOWN in db.check().reconcile.reasons


def test_E15_exception_and_contradictory_minus_one_keep_resources(tmp_path):
    clock = FakeClock()
    with Ledger(tmp_path / "error.db", clock=clock) as db:
        ctx, plan = start(db, fixture(), result="unrecorded")
        result = db.record_submit_result(plan.legs[0].leg_id, error_code="TIMEOUT")
        assert result.leg.state is c.OrderState.SUBMIT_UNKNOWN
        assert db.view("SIM_A").cash_reserved == D(2000)
    with Ledger(tmp_path / "conflict.db", clock=clock) as db:
        ctx, plan = start(db, fixture(), result="unrecorded")
        db.ingest_event(event(ctx, plan.legs[0]))
        db.record_submit_result(plan.legs[0].leg_id, -1)
        assert not db.get_leg(plan.legs[0].leg_id).settled
        assert db.view("SIM_A").cash_reserved == D(2000)


def test_E16_order_max_trade_unique_terminal_monotonic_and_barrier_settlement(tmp_path):
    clock = FakeClock()
    with Ledger(tmp_path / "ledger.db", clock=clock) as db:
        ctx, plan = start(db, fixture(volume=4))
        leg = plan.legs[0]
        partial = event(ctx, leg, traded=1, status=c.OrderState.PARTIAL)
        filled = event(ctx, leg, event_id="filled", traded=4, status=c.OrderState.FILLED)
        pending = event(ctx, leg, event_id="late", traded=0, status=c.OrderState.SUBMITTED_LOCAL)
        for item in (partial, filled, pending, partial, filled, pending):
            db.ingest_event(item)
        record = db.get_leg(leg.leg_id)
        assert record.state is c.OrderState.FILLED and record.reported_filled == 4 and record.trade_gap == 4
        for trade_id, qty in (("T1", 1), ("T2", 3)):
            item = event(ctx, leg, event_id=trade_id, kind=c.EventKind.TRADE, trade_id=trade_id, volume=qty)
            db.ingest_event(item)
            db.ingest_event(item)
            db.ingest_event(replace(item, event_id=trade_id + "-repeat"))
        record = db.get_leg(leg.leg_id)
        assert (record.reported_filled, record.trade_filled, record.risk_filled, record.trade_gap) == (4, 4, 4, 0)
        assert not record.settled and db.view("SIM_A").cash_reserved == D(4000)
        absorb(db, ctx, leg, clock, position=4)
        assert db.get_leg(leg.leg_id).settled
        assert db.view("SIM_A").cash_reserved == 0
        released = db.view("SIM_A").reservations[0]
        assert released.state is c.ReservationState.RELEASED and released.reflected_evidence_id


@pytest.mark.parametrize("conflict", ["quantity", "price", "missing-trade", "missing-day", "overfill", "event-payload" ])
def test_E17_trade_conflict_missing_keys_overfill_and_restart_replay(tmp_path, conflict):
    clock = FakeClock()
    path = tmp_path / "ledger.db"
    with Ledger(path, clock=clock) as db:
        ctx, plan = start(db, fixture(volume=4))
        leg = plan.legs[0]
        db.ingest_event(event(ctx, leg))
        trade = event(ctx, leg, event_id="trade", kind=c.EventKind.TRADE, trade_id="000T1", volume=1)
        db.ingest_event(trade)
        altered = replace(trade, event_id="bad")
        if conflict == "quantity":
            altered = replace(altered, volume=2)
        elif conflict == "price":
            altered = replace(altered, price=D(3501))
        elif conflict == "missing-trade":
            altered = replace(altered, trade_id=None)
        elif conflict == "missing-day":
            altered = replace(altered, trading_day=None)
        elif conflict == "overfill":
            altered = event(ctx, leg, event_id="overfill", traded=5)
        else:
            altered = replace(trade, volume=2)
        assert db.ingest_event(altered).code is c.ResultCode.QUARANTINED
        before = db.get_leg(leg.leg_id)
        assert before.trade_filled == 1 and db.check().reconcile.required
    with Ledger(path, clock=clock) as db:
        db.ingest_event(trade)
        db.ingest_event(altered)
        assert db.get_leg(leg.leg_id).trade_filled == before.trade_filled
        assert db.check().reconcile.required


def test_E18_synchronous_trade_before_order_and_local_result_unique_attribution(tmp_path):
    clock = FakeClock()
    with Ledger(tmp_path / "ledger.db", clock=clock) as db:
        ctx, plan = start(db, fixture(volume=4), result="unrecorded")
        leg = plan.legs[0]
        trade = event(ctx, leg, event_id="first-trade", kind=c.EventKind.TRADE, trade_id="0001", volume=4)
        order = event(ctx, leg, event_id="then-order", status=c.OrderState.FILLED, traded=4)
        assert db.ingest_event(trade).code is c.ResultCode.QUARANTINED
        assert db.ingest_event(order).code is c.ResultCode.QUARANTINED
        assert db.get_leg(leg.leg_id).risk_filled == 0
        db.record_submit_result(leg.leg_id, 7)
        record = db.get_leg(leg.leg_id)
        assert record.state is c.OrderState.FILLED and record.risk_filled == 4 and record.trade_filled == 4
        db.record_submit_result(leg.leg_id, 7)
        assert counts(db.path)["trades"] == 1


@pytest.mark.parametrize("change", ["account", "instrument", "epoch", "day", "memo", "native-owned"])
def test_E18_local_id_reuse_and_forged_scope_never_pollute(tmp_path, change):
    clock = FakeClock()
    with Ledger(tmp_path / "ledger.db", clock=clock) as db:
        ctx, plan = start(db, fixture())
        leg = plan.legs[0]
        kwargs = {"account": "SIM_B"} if change == "account" else {"instrument": "cu2610"} if change == "instrument" else \
                 {"epoch": "OLD"} if change == "epoch" else {"day": date(2026, 9, 15)} if change == "day" else \
                 {"memo": "other:OPEN:-:0"} if change == "memo" else {}
        item = event(ctx, leg, **kwargs)
        if change == "native-owned":
            item = replace(item, ownership=c.Ownership.OWNED, ownership_evidence=replace(item.ownership_evidence, evidence_id="native-claimed"))
        assert db.ingest_event(item).code is c.ResultCode.QUARANTINED
        record = db.get_leg(leg.leg_id)
        assert record.risk_filled == 0 and not record.ownership_verified
        current = sync(db, ctx)
        with pytest.raises(LedgerError):
            db.request_cancel("cancel", ctx.intent.intent_id, leg.leg_id, current)


def test_E19_write_failure_lock_timeout_and_gap_latch_no_memory_success(tmp_path):
    clock = FakeClock()
    path = tmp_path / "ledger.db"
    with Ledger(path, clock=clock, busy_timeout_ms=10) as db:
        ctx, plan = reserve(db, fixture())
        with sqlite3.connect(path) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            with pytest.raises(LedgerError) as caught:
                db.begin_attempt(plan.legs[0].leg_id, ctx)
            assert caught.value.code is c.ResultCode.PERSISTENCE_ERROR
            blocker.rollback()
        assert db.check().reconcile.required
        assert db.get_leg(plan.legs[0].leg_id).attempt_no == 0
        db.register_context(ctx)
        assert db.check().reconcile.required
        assert db.view("SIM_A").cash_reserved == D(2000)
    with Ledger(path, clock=clock) as db:
        assert c.ReconcileReason.LEDGER_FAILURE in db.check().reconcile.reasons
    with Ledger(tmp_path / "gap.db", clock=clock) as db:
        db.note_event_gap("queue-overflow")
        db.register_context(fixture())
        assert c.ReconcileReason.EVENT_GAP in db.recover(fixture().proof).reconcile.reasons
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a SQLite journal")
    with pytest.raises(LedgerError) as caught:
        Ledger(bad, clock=clock)
    assert "LEDGER_CORRUPT" in caught.value.blockers


def test_E19_sql_trigger_failure_rolls_back_all_legs_and_full_synchronous_close(tmp_path):
    clock = FakeClock()
    path = tmp_path / "ledger.db"
    db = Ledger(path, clock=clock)
    ctx = sync(db, fixture(volume=4))
    db.register(ctx.intent)
    # SQLite trigger is a real write failure inside the transaction, after the first leg.
    with sqlite3.connect(path) as fault:
        fault.execute("CREATE TRIGGER fail_second BEFORE INSERT ON reservations WHEN (SELECT COUNT(*) FROM reservations)>0 BEGIN SELECT RAISE(ABORT,'test-write-failure'); END")
    with pytest.raises(LedgerError) as caught:
        db.reserve(hand_plan(ctx, quantities=(2, 2)), ctx.ledger_revision)
    assert caught.value.code is c.ResultCode.PERSISTENCE_ERROR
    assert counts(path)["legs"] == counts(path)["reservations"] == 0
    assert db.check().reconcile.required
    assert db._conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    db.close()
    db.close()
    with pytest.raises(LedgerError):
        db.view("SIM_A")


@pytest.mark.parametrize("case", ["missing-bound", "cash", "gross", "close-no-margin"])
def test_E27_account_budget_and_close_does_not_require_open_margin_rate(tmp_path, case):
    clock = FakeClock()
    ctx = fixture(position=10, action=c.Action.CLOSE_LONG) if case == "close-no-margin" else fixture()
    if case in ("missing-bound", "close-no-margin"):
        ctx = replace(ctx, policy=replace(ctx.policy, cash_per_open_lot=None, cash_bound_source=None))
    elif case == "cash":
        ctx = replace(ctx, policy=replace(ctx.policy, max_cash_reserve=D(1000)))
    else:
        ctx = replace(ctx, policy=replace(ctx.policy, max_gross_position=1))
    with Ledger(tmp_path / "ledger.db", clock=clock) as db:
        if case == "close-no-margin":
            _, plan = reserve(db, ctx)
            assert db.view("SIM_A").cash_reserved == D(20)
            assert plan.legs[0].phase is c.Phase.CLOSE
        else:
            with pytest.raises(LedgerError):
                reserve(db, ctx)
            assert counts(db.path)["legs"] == 0


def test_E28_receipt_owns_exact_reservations_and_other_budget_changes_recheck(tmp_path):
    clock = FakeClock()
    with Ledger(tmp_path / "ledger.db", clock=clock) as db:
        ctx, plan = reserve(db, fixture(action=c.Action.CLOSE_LONG, position=3))
        assert ctx.reservation_receipt.reservation_ids == (plan.legs[0].leg_id,)
        assert db.begin_attempt(plan.legs[0].leg_id, ctx).send_permitted
    with Ledger(tmp_path / "forged.db", clock=clock) as db:
        ctx, plan = reserve(db, fixture(action=c.Action.CLOSE_LONG, position=3))
        forged = replace(ctx, reservation_receipt=replace(ctx.reservation_receipt, reservation_ids=("forged",)))
        db.register_context(forged)
        with pytest.raises(LedgerError, match="RESERVATION_REQUIRED"):
            db.begin_attempt(plan.legs[0].leg_id, forged)
        ctx = sync(db, ctx)
        other = fixture(name="other", instrument="cu2610", available="100000")
        reserve(db, other)
        with pytest.raises(LedgerError):
            db.begin_attempt(plan.legs[0].leg_id, ctx)
        ctx = sync(db, ctx)
        assert db.begin_attempt(plan.legs[0].leg_id, ctx).send_permitted


def test_E23_cancel_is_persistent_independent_and_trades_continue_until_barrier(tmp_path):
    clock = FakeClock()
    with Ledger(tmp_path / "ledger.db", clock=clock) as db:
        ctx, plan = start(db, fixture(volume=4))
        leg = plan.legs[0]
        db.ingest_event(event(ctx, leg))
        clock.advance(4)  # quote expired, cancel still allowed on its independent route.
        ctx = sync(db, ctx)
        attempt = db.request_cancel("cancel-1", ctx.intent.intent_id, leg.leg_id, ctx)
        assert attempt.cancel_permitted
        assert not db.request_cancel("cancel-1", ctx.intent.intent_id, leg.leg_id, ctx).cancel_permitted
        with pytest.raises(LedgerError):
            db.request_cancel("different", ctx.intent.intent_id, leg.leg_id, ctx)
        db.record_cancel_result("cancel-1", 0)
        assert db.get_leg(leg.leg_id).cancel_state is c.CancelState.REQUESTED
        db.ingest_event(event(ctx, leg, event_id="trade-during-cancel", kind=c.EventKind.TRADE, trade_id="T1", volume=1, now=clock.now))
        db.ingest_event(event(ctx, leg, event_id="cancel-note", kind=c.EventKind.CANCEL, now=clock.now))
        assert db.get_leg(leg.leg_id).trade_filled == 1
        assert db.get_leg(leg.leg_id).state is c.OrderState.PARTIAL
        db.ingest_event(event(ctx, leg, event_id="cancel-final", status=c.OrderState.PARTIAL_CANCELLED, traded=1, cancelled=3, now=clock.now))
        assert db.view("SIM_A").cash_reserved == D(4000)
        absorb(db, ctx, leg, clock, position=1)
        record = db.get_leg(leg.leg_id)
        assert record.cancel_state is c.CancelState.CONFIRMED and record.settled
        assert record.state is c.OrderState.PARTIAL_CANCELLED


def test_E22_cancel_crash_latch_no_retry_with_same_or_new_id(tmp_path):
    clock = FakeClock()
    path = tmp_path / "ledger.db"
    with Ledger(path, clock=clock) as db:
        ctx, plan = start(db, fixture())
        leg = plan.legs[0]
        db.ingest_event(event(ctx, leg))
        ctx = sync(db, ctx)
        assert db.request_cancel("cancel-1", ctx.intent.intent_id, leg.leg_id, ctx).cancel_permitted
    with Ledger(path, clock=clock) as db:
        assert db.get_leg(leg.leg_id).cancel_state is c.CancelState.UNKNOWN
        assert not db.request_cancel("cancel-1", ctx.intent.intent_id, leg.leg_id, ctx).cancel_permitted
        with pytest.raises(LedgerError):
            db.request_cancel("cancel-2", ctx.intent.intent_id, leg.leg_id, ctx)


def test_normalize_whitelist_missing_identity_trade_keys_no_day_guess_or_native_scope(tmp_path):
    callback = CallbackContext(event_id="raw", kind=c.EventKind.TRADE, received_at=NOW, session_epoch="SIM_E",
                                source=c.Source.PYTHONGO, account_key="SIM_A")
    item = normalize_event({"TradeTime": "21:30:00", "InvestorID": "secret", "Password": "secret", "Memo": "arbitrary secret",
                            "errMsg": "private text", "ownership": "owned", "verified": True}, callback)
    assert item.trade_id is None and item.trading_day is None and item.trade_time is None
    assert item.memo is None and item.exchange is None and item.ownership is c.Ownership.UNKNOWN
    with pytest.raises(ValueError):
        replace(callback, simulation_scope=True, strategy_key="SIM_S")
    with Ledger(tmp_path / "ledger.db", clock=FakeClock()) as db:
        assert Reconciler(db).ingest(item).code is c.ResultCode.QUARANTINED
        with sqlite3.connect(db.path) as connection:
            text = connection.execute("SELECT payload FROM events WHERE kind='CALLBACK'").fetchone()[0]
        assert "secret" not in text and "private text" not in text and "InvestorID" not in text


def test_trade_before_filled_order_preserves_final_projection_and_duplicates(tmp_path):
    clock = FakeClock()
    with Ledger(tmp_path / "trade-first.db", clock=clock) as db:
        ctx, plan = start(db, fixture(volume=2))
        leg = plan.legs[0]
        db.ingest_event(event(ctx, leg))
        trade = event(ctx, leg, event_id="trade-full", kind=c.EventKind.TRADE, trade_id="000T", volume=2)
        filled = event(ctx, leg, event_id="order-full", traded=2, status=c.OrderState.FILLED)
        for item in (trade, filled, trade, filled):
            assert db.ingest_event(item).code in (c.ResultCode.OK, c.ResultCode.IDEMPOTENT)
        record = db.get_leg(leg.leg_id)
        assert record.state is c.OrderState.FILLED and record.reported_filled == record.trade_filled == 2
        absorb(db, ctx, leg, clock, position=2)
        assert db.get_leg(leg.leg_id).settled


def test_partial_cancel_late_unique_trade_fills_details_but_excess_is_quarantined(tmp_path):
    clock = FakeClock()
    with Ledger(tmp_path / "partial.db", clock=clock) as db:
        ctx, plan = start(db, fixture(volume=4))
        leg = plan.legs[0]
        final = event(ctx, leg, event_id="cancelled", traded=1, cancelled=3, status=c.OrderState.PARTIAL_CANCELLED)
        assert db.ingest_event(final).code is c.ResultCode.OK
        late = event(ctx, leg, event_id="late-trade", kind=c.EventKind.TRADE, trade_id="T1", volume=1)
        assert db.ingest_event(late).code is c.ResultCode.OK
        assert db.get_leg(leg.leg_id).state is c.OrderState.PARTIAL_CANCELLED
        assert db.get_leg(leg.leg_id).trade_gap == 0
        extra = replace(late, event_id="extra", trade_id="T2")
        assert db.ingest_event(extra).code is c.ResultCode.QUARANTINED
        assert db.get_leg(leg.leg_id).trade_filled == 1


def test_UNKNOWN_raw_quantity_conflict_and_missing_error_code_are_persisted(tmp_path):
    callback = CallbackContext(event_id="quantity", kind=c.EventKind.ORDER, received_at=NOW, session_epoch="SIM_E", source=c.Source.PYTHONGO)
    item = normalize_event({"TotalVolume": 4, "TradedVolume": 3, "CancelVolume": 2}, callback)
    error = normalize_event({}, replace(callback, event_id="error", kind=c.EventKind.ERROR))
    assert (item.total_volume, item.traded_volume, item.cancel_volume) == (4, 3, 2)
    assert error.error_code is None
    with Ledger(tmp_path / "raw.db", clock=FakeClock()) as db:
        assert db.ingest_event(item).reason == "QUANTITY_CONFLICT"
        assert db.ingest_event(error).reason == "MISSING_ERROR_CODE"
        assert c.ReconcileReason.QUANTITY_CONFLICT in db.check().reconcile.reasons
        with sqlite3.connect(db.path) as conn:
            stored = c.decode(conn.execute("SELECT payload FROM events WHERE event_id='quantity'").fetchone()[0], c.ExecutionEvent)
        assert stored == item


def test_F02_open_cash_reflection_requires_available_absorption_evidence(tmp_path):
    clock = FakeClock()
    with Ledger(tmp_path / "cash.db", clock=clock) as db:
        ctx, plan = start(db, fixture())
        leg = plan.legs[0]
        db.ingest_event(event(ctx, leg))
        # An unchanged available proves nothing: the reservation stays HELD and the
        # policy commitment still rejects a second equal promise.
        ctx = absorb(db, ctx, leg, clock, terminal=False)
        assert db.view("SIM_A").reservations[0].state is c.ReservationState.HELD
        second = replace(ctx.intent, intent_id="intent-second", source_key="second")
        db.register(second)
        tight = replace(ctx, intent=second, reservation_receipt=None,
                        policy=replace(ctx.policy, policy_id="tight", max_cash_reserve=D(3000)))
        tight = sync(db, tight)
        with pytest.raises(LedgerError, match="INSUFFICIENT_CASH"):
            db.reserve(hand_plan(tight), tight.ledger_revision)
        # available reduced by at least the reserved bound is real absorption; the
        # SDK remainder then covers exactly one further equal commitment.
        ctx = absorb(db, ctx, leg, clock, terminal=False, available="2000")
        assert db.view("SIM_A").reservations[0].state is c.ReservationState.REFLECTED
        third = replace(ctx.intent, intent_id="intent-third", source_key="third")
        db.register(third)
        another = sync(db, replace(ctx, intent=third, reservation_receipt=None))
        db.reserve(hand_plan(another), another.ledger_revision)
        assert db.view("SIM_A").cash_reserved == D(4000)


@pytest.mark.parametrize("change", ["front", "session", "epoch", "account"])
def test_cancel_route_rechecks_persistent_alias_and_rejects_conflicting_callback(tmp_path, change):
    clock = FakeClock()
    with Ledger(tmp_path / "route.db", clock=clock) as db:
        ctx, plan = start(db, fixture())
        leg = plan.legs[0]
        original = replace(event(ctx, leg), front_id=11, session_id=22)
        assert db.ingest_event(original).code is c.ResultCode.OK
        conflict = replace(original, event_id="conflicting-route")
        if change == "front":
            conflict = replace(conflict, front_id=12)
        elif change == "session":
            conflict = replace(conflict, session_id=23)
        elif change == "epoch":
            conflict = replace(conflict, callback_session_epoch="OLD_EPOCH")
        else:
            conflict = replace(conflict, account_key="OTHER_ACCOUNT")
        assert db.ingest_event(conflict).code is c.ResultCode.QUARANTINED
        ctx = sync(db, ctx)
        with pytest.raises(LedgerError, match="CANCEL_ROUTE_UNVERIFIED"):
            db.request_cancel("cancel-route", ctx.intent.intent_id, leg.leg_id, ctx)
        assert db.get_leg(leg.leg_id).cancel_state is c.CancelState.NONE


@pytest.mark.parametrize("change", ["budget", "authorization", "disabled"])
def test_control_change_between_prepare_and_attempt_is_checked_atomically(tmp_path, change):
    clock = FakeClock()
    with Ledger(tmp_path / "control.db", clock=clock) as db:
        ctx, plan = reserve(db, fixture())
        updated = replace(ctx, policy=replace(ctx.policy, policy_id="lower", max_cash_reserve=D(10))) if change == "budget" else \
                  replace(ctx, authorization=None) if change == "authorization" else \
                  replace(ctx, mode=c.ExecutionMode.DISABLED, authorization=None,
                          readiness=replace(ctx.readiness, mode=c.ExecutionMode.DISABLED, state=c.ReadinessState.PLAN_READY,
                                            can_submit=False, can_cancel_owned=False))
        db.register_context(updated)
        with pytest.raises(LedgerError, match="CONTEXT_REQUIRED"):
            db.begin_attempt(plan.legs[0].leg_id, ctx)
        assert db.get_leg(plan.legs[0].leg_id).attempt_no == 0
        assert db.context(ctx.binding) == updated


def test_cancel_checks_registered_authorization_revocation_inside_transaction(tmp_path):
    clock = FakeClock()
    with Ledger(tmp_path / "cancel-auth.db", clock=clock) as db:
        ctx, plan = start(db, fixture())
        leg = plan.legs[0]
        db.ingest_event(event(ctx, leg))
        ctx = sync(db, ctx)
        db.register_context(replace(ctx, authorization=None))
        with pytest.raises(LedgerError, match="CONTEXT_REQUIRED"):
            db.request_cancel("cancel-after-revoke", ctx.intent.intent_id, leg.leg_id, ctx)
        assert db.get_leg(leg.leg_id).cancel_state is c.CancelState.NONE


def test_reconnect_current_scope_blocks_prior_epoch_submit_and_cancel(tmp_path):
    clock = FakeClock()
    with Ledger(tmp_path / "reconnect.db", clock=clock) as db:
        ctx, plan = reserve(db, fixture())
        replacement = fixture(name="reconnected", epoch="NEW_EPOCH")
        db.register_context(replacement)
        assert db.context(ctx.binding).binding.session_epoch == "NEW_EPOCH"
        with pytest.raises(LedgerError, match="CONTEXT_REQUIRED"):
            db.begin_attempt(plan.legs[0].leg_id, ctx)
    with Ledger(tmp_path / "cancel-reconnect.db", clock=clock) as db:
        ctx, plan = start(db, fixture())
        leg = plan.legs[0]
        db.ingest_event(event(ctx, leg))
        ctx = sync(db, ctx)
        db.register_context(replacement)
        with pytest.raises(LedgerError, match="CONTEXT_REQUIRED"):
            db.request_cancel("old-epoch", ctx.intent.intent_id, leg.leg_id, ctx)


@pytest.mark.parametrize("change", ["revoke", "budget", "epoch", "disable"])
def test_two_connections_context_CAS_rejects_control_update_race_without_writes(tmp_path, change):
    clock = FakeClock()
    path = tmp_path / "cas.db"
    with Ledger(path, clock=clock) as first, Ledger(path, clock=clock) as second:
        original = fixture()
        first.register_context(original)
        expected = first.context(original.binding)
        new_quote = fixture(name="new-quote", now=NOW + timedelta(seconds=1))
        proposed = replace(expected, snapshot=new_quote.snapshot, proof=new_quote.proof, clock=new_quote.clock)
        replacement = replace(expected, authorization=None) if change == "revoke" else \
                      replace(expected, policy=replace(expected.policy, policy_id="lower", max_cash_reserve=D(100))) if change == "budget" else \
                      fixture(name="new-session", epoch="NEW_EPOCH") if change == "epoch" else \
                      replace(expected, mode=c.ExecutionMode.DISABLED, authorization=None,
                              readiness=replace(expected.readiness, mode=c.ExecutionMode.DISABLED,
                                                state=c.ReadinessState.PLAN_READY, can_submit=False, can_cancel_owned=False))
        second.register_context(replacement)
        before = counts(path)
        with pytest.raises(LedgerError, match="CONTEXT_CONFLICT"):
            first.compare_and_register_context(proposed, expected)
        assert counts(path) == before
        assert first.context(original.binding) == replacement
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT COUNT(*) FROM events WHERE event_id=?", ("snapshot:" + new_quote.snapshot.snapshot_id,)).fetchone()[0] == 0


def test_context_CAS_accepts_same_controls_new_quote_and_blocks_stale_replay(tmp_path):
    clock = FakeClock()
    path = tmp_path / "cas-success.db"
    with Ledger(path, clock=clock) as first, Ledger(path, clock=clock) as second:
        original = fixture()
        first.register_context(original)
        expected = second.context(original.binding)
        fresh = fixture(name="fresh", now=NOW + timedelta(seconds=1))
        proposed = replace(expected, snapshot=fresh.snapshot, proof=fresh.proof, clock=fresh.clock)
        receipt = first.compare_and_register_context(proposed, expected)
        assert receipt.snapshot_id == fresh.snapshot.snapshot_id
        assert second.context(original.binding) == proposed
        with pytest.raises(LedgerError, match="CONTEXT_CONFLICT"):
            second.compare_and_register_context(original, expected)
        with pytest.raises(LedgerError, match="CONTEXT_CONFLICT"):
            first.compare_and_register_context(replace(proposed, authorization=None), proposed)


def test_F02_unabsorbed_close_snapshot_never_reflects_and_blocks_double_commitment(tmp_path):
    clock = FakeClock()
    with Ledger(tmp_path / "f02-active.db", clock=clock) as db:
        ctx, plan = start(db, fixture(action=c.Action.CLOSE_LONG, position=3))
        leg = plan.legs[0]
        db.ingest_event(event(ctx, leg))
        # Complete proof, covered leg, matching snapshot id, but Position=3/Frozen=0:
        # the SDK truth still shows all three lots closeable without our freeze.
        ctx = absorb(db, ctx, leg, clock, position=3, frozen=0, terminal=False)
        reservation = db.view("SIM_A").reservations[0]
        assert reservation.state is c.ReservationState.HELD and reservation.not_proven_reflected == 2
        second = replace(ctx.intent, intent_id="intent-second", source_key="second")
        db.register(second)
        other = sync(db, replace(ctx, intent=second, reservation_receipt=None))
        with pytest.raises(LedgerError, match="INSUFFICIENT_POSITION"):
            db.reserve(hand_plan(other), other.ledger_revision)
        assert counts(db.path)["reservations"] == 1
        # A snapshot that actually freezes this order's lots is reflected, and the
        # shrunken SDK availability still caps any further close at one lot.
        ctx = absorb(db, ctx, leg, clock, position=3, frozen=2, terminal=False)
        assert db.view("SIM_A").reservations[0].state is c.ReservationState.REFLECTED
        third = replace(ctx.intent, intent_id="intent-third", source_key="third")
        db.register(third)
        another = sync(db, replace(ctx, intent=third, reservation_receipt=None))
        with pytest.raises(LedgerError, match="INSUFFICIENT_POSITION"):
            db.reserve(hand_plan(another), another.ledger_revision)
        assert counts(db.path)["reservations"] == 1


def test_F02_release_requires_post_trade_position_not_just_inventory_proof(tmp_path):
    clock = FakeClock()
    with Ledger(tmp_path / "f02-terminal.db", clock=clock) as db:
        ctx, plan = start(db, fixture(action=c.Action.CLOSE_LONG, position=3))
        leg = plan.legs[0]
        db.ingest_event(event(ctx, leg, event_id="filled", status=c.OrderState.FILLED, traded=2))
        db.ingest_event(event(ctx, leg, event_id="trade", kind=c.EventKind.TRADE, trade_id="TF", volume=2))
        record = db.get_leg(leg.leg_id)
        assert record.risk_filled == 2 and record.trade_gap == 0
        # Barrier inventory matches exactly, but the position still shows the
        # pre-trade 3 lots: nothing was actually absorbed.
        ctx = absorb(db, ctx, leg, clock, position=3)
        assert not db.get_leg(leg.leg_id).settled
        assert db.view("SIM_A").reservations[0].state is c.ReservationState.HELD
        second = replace(ctx.intent, intent_id="intent-second", source_key="second")
        db.register(second)
        other = sync(db, replace(ctx, intent=second, reservation_receipt=None))
        with pytest.raises(LedgerError, match="INSUFFICIENT_POSITION"):
            db.reserve(hand_plan(other), other.ledger_revision)
        # The real post-trade position releases and settles; the remaining SDK
        # closeable is 1, so a second CLOSE2 is still impossible (2+2 > 3).
        ctx = absorb(db, ctx, leg, clock, position=1)
        assert db.get_leg(leg.leg_id).settled
        assert db.view("SIM_A").reservations[0].state is c.ReservationState.RELEASED
        third = replace(ctx.intent, intent_id="intent-third", source_key="third")
        db.register(third)
        another = sync(db, replace(ctx, intent=third, reservation_receipt=None))
        with pytest.raises(LedgerError, match="INSUFFICIENT_POSITION"):
            db.reserve(hand_plan(another), another.ledger_revision)
        assert counts(db.path)["reservations"] == 1


def test_F03_working_after_terminal_partial_cancel_is_quarantined(tmp_path):
    clock = FakeClock()
    with Ledger(tmp_path / "f03.db", clock=clock) as db:
        ctx, plan = start(db, fixture(volume=4))
        leg = plan.legs[0]
        final = event(ctx, leg, event_id="pc", status=c.OrderState.PARTIAL_CANCELLED, traded=1, cancelled=3)
        assert db.ingest_event(final).code is c.ResultCode.OK
        late = event(ctx, leg, event_id="late-working", status=c.OrderState.WORKING, traded=2, cancelled=0)
        result = db.ingest_event(late)
        assert result.code is c.ResultCode.QUARANTINED and result.reason == "QUANTITY_CONFLICT"
        record = db.get_leg(leg.leg_id)
        assert (record.state, record.reported_filled, record.risk_filled, record.cancel_volume) == \
               (c.OrderState.PARTIAL_CANCELLED, 1, 1, 3)
        assert c.ReconcileReason.QUANTITY_CONFLICT in db.check().reconcile.reasons
        # A stale cumulative snapshot at or below the frozen final is harmless.
        stale = event(ctx, leg, event_id="stale-working", status=c.OrderState.WORKING, traded=1, cancelled=0)
        assert db.ingest_event(stale).code is c.ResultCode.OK
        assert db.get_leg(leg.leg_id).reported_filled == 1


def test_F04_begin_attempt_rejects_proof_watermark_mismatch(tmp_path):
    clock = FakeClock()
    with Ledger(tmp_path / "f04.db", clock=clock) as db:
        ctx, plan = reserve(db, fixture())
        mismatch = replace(ctx, proof=replace(ctx.proof,
                                             complete_through=replace(ctx.proof.complete_through, value="different-barrier")))
        db.register_context(mismatch)
        with pytest.raises(LedgerError) as caught:
            db.begin_attempt(plan.legs[0].leg_id, mismatch)
        assert "PROOF_REQUIRED" in caught.value.blockers
        assert db.get_leg(plan.legs[0].leg_id).attempt_no == 0


def test_F05_alias_fallthrough_after_broken_preferred_attribute(tmp_path):
    class Hostile:
        @property
        def Exchange(self):
            raise ValueError("broken sdk attribute")

        @property
        def exchange(self):
            return "SHFE"

        @property
        def total_volume(self):
            return 4

    callback = CallbackContext(event_id="alias", kind=c.EventKind.ORDER, received_at=NOW, session_epoch="SIM_E",
                               source=c.Source.SIMULATION, account_key="SIM_A", strategy_key="SIM_S", simulation_scope=True)
    item = normalize_event(Hostile(), callback)
    assert item.exchange == "SHFE" and item.total_volume == 4


def test_F05_unknown_zero_quantities_preserved_and_isolated_at_ledger_gate(tmp_path):
    callback = CallbackContext(event_id="zero", kind=c.EventKind.TRADE, received_at=NOW, session_epoch="SIM_E",
                               source=c.Source.SIMULATION, account_key="SIM_A", strategy_key="SIM_S", simulation_scope=True)
    raw = {"Exchange": "SHFE", "InstrumentID": "rb2610", "Hedgeflag": "1", "TradeID": " 000Z ", "Price": "3500",
           "Direction": "0", "Offset": "0", "TradingDay": "2026-09-14",
           "TotalVolume": 0, "Volume": 0, "TradedVolume": 0, "CancelVolume": 0}
    item = normalize_event(raw, callback)
    assert (item.total_volume, item.volume, item.traded_volume, item.cancel_volume) == (0, 0, 0, 0)
    with Ledger(tmp_path / "zero.db", clock=FakeClock()) as db:
        assert db.ingest_event(item).reason == "MISSING_TRADE_KEY"


def test_F07_refresh_expiry_takes_earliest_component_ttl(tmp_path):
    clock = FakeClock()
    with Ledger(tmp_path / "f07.db", clock=clock) as db:
        ctx, plan = start(db, fixture(action=c.Action.REVERSE_TO_SHORT, position=3, volume=2))
        close, opening = plan.legs
        db.ingest_event(event(ctx, close, event_id="cf", status=c.OrderState.FILLED, traded=3))
        db.ingest_event(event(ctx, close, event_id="ct", kind=c.EventKind.TRADE, trade_id="TC", volume=3))
        fresh = absorb(db, ctx, close, clock, position=0)
        aged = replace(fresh, reservation_view=None,
                       snapshot=replace(fresh.snapshot, snapshot_id="snapshot-aged-contract",
                                        times=replace(fresh.snapshot.times,
                                                      contract=replace(fresh.snapshot.times.contract,
                                                                       sampled_at=clock.now - timedelta(seconds=3599.5)))),
                       proof=replace(fresh.proof, position_snapshot_id="snapshot-aged-contract"))
        aged = sync(db, aged, fresh.reservation_receipt)
        receipt = db.refresh_pending_open(opening.leg_id, aged)
        remaining = (db.get_leg(opening.leg_id).plan.expires_at - clock.now).total_seconds()
        assert 0.4 < remaining <= 0.6
        attempt = sync(db, aged, receipt)
        assert db.begin_attempt(opening.leg_id, attempt).send_permitted


def test_F02r2_partial_fill_without_freeze_keeps_unfilled_remainder_occupied(tmp_path):
    clock = FakeClock()
    with Ledger(tmp_path / "f02r2.db", clock=clock) as db:
        ctx, plan = start(db, fixture(action=c.Action.CLOSE_LONG, position=3, max_inflight=2))
        leg = plan.legs[0]
        db.ingest_event(event(ctx, leg, event_id="partial", status=c.OrderState.PARTIAL, traded=1))
        db.ingest_event(event(ctx, leg, event_id="trade", kind=c.EventKind.TRADE, trade_id="TP", volume=1))
        record = db.get_leg(leg.leg_id)
        assert record.risk_filled == 1 and record.trade_gap == 0
        # Complete barrier, inventory active 1 == our remaining 1, position shows
        # the fill, but FrozenClosing=0: the working lot has no freeze evidence.
        ctx = absorb(db, ctx, leg, clock, position=2, frozen=0, terminal=False)
        reservation = db.view("SIM_A").reservations[0]
        assert reservation.state is c.ReservationState.HELD and reservation.not_proven_reflected == 2
        second = replace(ctx.intent, intent_id="intent-second", source_key="second")
        db.register(second)
        other = sync(db, replace(ctx, intent=second, reservation_receipt=None))
        with pytest.raises(LedgerError, match="INSUFFICIENT_POSITION"):
            db.reserve(hand_plan(other), other.ledger_revision)
        assert counts(db.path)["reservations"] == 1
        # Real freeze evidence for the remaining lot hands the claim to the SDK;
        # only the true remaining capacity (one lot) is bookable again.
        ctx = absorb(db, ctx, leg, clock, position=2, frozen=1, terminal=False)
        assert db.view("SIM_A").reservations[0].state is c.ReservationState.REFLECTED
        third = replace(ctx.intent, intent_id="intent-third", source_key="third", volume=1)
        blocked_close2 = replace(ctx.intent, intent_id="intent-blocked", source_key="blocked")
        db.register(blocked_close2)
        blocked_ctx = sync(db, replace(ctx, intent=blocked_close2, reservation_receipt=None))
        with pytest.raises(LedgerError, match="INSUFFICIENT_POSITION"):
            db.reserve(hand_plan(blocked_ctx), blocked_ctx.ledger_revision)
        fourth_ctx, fourth_plan = reserve(db, replace(ctx, intent=third, reservation_receipt=None))
        assert db.begin_attempt(fourth_plan.legs[0].leg_id, fourth_ctx).send_permitted


def test_two_connections_initialize_context_race_only_one_wins(tmp_path):
    clock = FakeClock()
    path = tmp_path / "init.db"
    with Ledger(path, clock=clock) as first, Ledger(path, clock=clock) as second:
        winner = fixture(name="winner")
        loser = fixture(name="loser", available="90000")
        gate = threading.Barrier(2)

        def run(db, ctx):
            gate.wait(timeout=5)
            try:
                return db.initialize_context(ctx)
            except LedgerError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda pair: run(*pair), ((first, winner), (second, loser))))
        codes = [type(x) for x in results]
        assert codes.count(ContextReceipt) == 1
        assert codes.count(c.ResultCode) == 1
        stored = first.context(winner.binding)
        assert stored == (winner if type(results[0]) is ContextReceipt else loser)
        # The loser cannot retry its stale initialization over the winner's controls.
        with pytest.raises(LedgerError, match="CONTEXT_CONFLICT"):
            second.initialize_context(loser)
        assert first.context(winner.binding) == stored
        # Regular CAS keeps working on the initialized scope.
        fresh = fixture(name="after-init", now=NOW + timedelta(seconds=1))
        updated = replace(stored, snapshot=fresh.snapshot, proof=fresh.proof, clock=fresh.clock)
        first.compare_and_register_context(updated, stored)
        assert second.context(winner.binding) == updated


def test_E24_reverse_refresh_stable_open_and_retains_original_close_plan(tmp_path):
    clock = FakeClock()
    with Ledger(tmp_path / "ledger.db", clock=clock) as db:
        ctx, plan = start(db, fixture(action=c.Action.REVERSE_TO_SHORT, position=3, volume=2))
        close, opening = plan.legs
        assert db.view("SIM_A").cash_reserved == D(2030)
        blocked = sync(db, ctx)
        with pytest.raises(LedgerError):
            db.begin_attempt(opening.leg_id, blocked)
        db.ingest_event(event(ctx, close, event_id="close-filled", status=c.OrderState.FILLED, traded=3))
        db.ingest_event(event(ctx, close, event_id="close-trade", kind=c.EventKind.TRADE, trade_id="TC", volume=3))
        fresh = absorb(db, ctx, close, clock, position=0)
        fresh = replace(fresh, policy=replace(fresh.policy, policy_id="new-policy", cash_per_open_lot=D(1500)))
        fresh = sync(db, fresh)
        # Insufficient replacement budget is atomic, preserving the old Plan and cash.
        insufficient = replace(fresh, policy=replace(fresh.policy, policy_id="too-small", max_cash_reserve=D(2999)))
        db.register_context(insufficient)
        with pytest.raises(LedgerError, match="INSUFFICIENT_CASH"):
            db.refresh_pending_open(opening.leg_id, insufficient)
        assert db.get_leg(opening.leg_id).plan == plan
        assert db.view("SIM_A").cash_reserved == D(2000)
        db.register_context(fresh)
        receipt = db.refresh_pending_open(opening.leg_id, fresh)
        assert receipt.plan_id != plan.plan_id
        assert db.get_leg(close.leg_id).plan == plan
        new = db.get_leg(opening.leg_id)
        assert new.leg.leg_id == opening.leg_id and new.leg.volume == 2 and new.leg.cash_reserve == D(3000)
        assert db.refresh_pending_open(opening.leg_id, fresh) == receipt
        stale_receipt = sync(db, fresh, ctx.reservation_receipt)
        with pytest.raises(LedgerError, match="RESERVATION_REQUIRED"):
            db.begin_attempt(opening.leg_id, stale_receipt)
        fresh = sync(db, fresh, receipt)
        assert db.begin_attempt(opening.leg_id, fresh).send_permitted
        with pytest.raises(LedgerError, match="ATTEMPT_EXISTS"):
            db.refresh_pending_open(opening.leg_id, fresh)
        with sqlite3.connect(db.path) as con:
            assert con.execute("SELECT COUNT(*) FROM events WHERE kind='PLAN'").fetchone()[0] == 2


# ---- NE1 C2: dispatch tickets, control CAS, permit budgets, migration ----

from ak_trading.infiny_execution import native_contracts as nc
from ak_trading.infiny_execution import native_evidence as ne
from ak_trading.infiny_execution.dispatch_journal import DispatchJournal
import subprocess
import sys


class AcceptingRegistry(ne.EvidenceRegistry):
    """SYNTHETIC-only test registry: accepts structurally valid evidence."""

    def __init__(self, *, permit_deadline=None, deadline_ok=True):
        self.permit_deadline = permit_deadline
        self.deadline_ok = deadline_ok

    def applicable_deadline(self, permit_id, *, action, now):
        del permit_id, action
        if not self.deadline_ok:
            return ne.EvidenceDeadlineResult(
                validation=c.ValidationResult(blockers=("NATIVE_DEADLINE_UNAVAILABLE",)), deadline=None)
        deadline = self.permit_deadline if self.permit_deadline is not None else now + timedelta(days=1)
        return ne.EvidenceDeadlineResult(validation=c.ValidationResult(), deadline=deadline)

    def register_baseline(self, evidence):
        pass

    def register_route(self, evidence):
        pass

    def register_thread(self, evidence):
        pass

    def register_settlement(self, evidence):
        pass

    def register_permit(self, permit):
        pass

    def verify_permit_active(self, permit_id, *, now):
        del permit_id, now
        return c.ValidationResult()


def control_state(ctx, revision, *, permit_id=None, mode=None, disabled=None):
    return nc.ControlState(control_revision=revision, binding=ctx.binding, mode=mode or ctx.mode,
                           policy_id=ctx.policy.policy_id,
                           authorization_id=ctx.authorization.authorization_id if ctx.authorization else None,
                           native_permit_id=permit_id, params_sha256="0" * 64, generation="gen-1",
                           updated_at=ctx.clock.now, disabled_reason=disabled)


def offer_for(ctx, plan, leg, revision, *, offer_id="offer-1"):
    return nc.ExecutionOffer(offer_id=offer_id, intent_id=ctx.intent.intent_id, leg_id=leg.leg_id,
                             plan_id=plan.plan_id, intent_hash=c.canonical_hash(ctx.intent),
                             leg_hash=c.canonical_hash(leg), binding=ctx.binding,
                             snapshot_id=ctx.snapshot.snapshot_id, policy_id=ctx.policy.policy_id,
                             params_sha256="0" * 64, control_revision=revision,
                             expires_at=ctx.clock.now + timedelta(milliseconds=1400))


def record_first_offer(target, ctx, plan, leg, revision, expected, *, registry=None, kind=nc.CommandKind.SUBMIT,
                       cancel_id=None, offer=None, first_issued_at=None):
    """C1.4 mandatory first-record step before any begin_dispatch."""
    journal = target if isinstance(target, DispatchJournal) else DispatchJournal(target, evidence_registry=registry)
    return journal.record_offer(offer or offer_for(ctx, plan, leg, revision), ctx,
                                first_issued_at=first_issued_at or ctx.clock.now, kind=kind,
                                expected_control_revision=revision, expected_context=expected,
                                cancel_id=cancel_id)


def receipt_for(command, ctx, *, stage, result_kind=None, result_value=None, error_code=None,
                receipt_id="receipt-1", previous=None):
    return nc.DispatchReceipt(receipt_id=receipt_id, command_id=command.command_id, attempt_id=command.attempt_id,
                              binding=command.binding, generation="gen-1", stage=stage, local_sequence=1,
                              recorded_at=ctx.clock.now, result_kind=result_kind, result_value=result_value,
                              error_code=error_code, request_sha256="1" * 64, previous_receipt_sha256=previous)


def pilot_permit(ctx, revision, *, permit_id="permit-1"):
    return ne.TestEvidenceFactory(purpose="SYNTHETIC_TEST_ONLY").issue_permit(
        permit_id=permit_id, authorization_id="auth-pilot", binding=ctx.binding, control_revision=revision,
        baseline_evidence_id="baseline-1", route_evidence_id="route-1", cash_bound_evidence_id="cash-1",
        source_key="pilot-1", actions=(c.AuthorizationAction.OPEN_LONG,), max_volume=10,
        expires_at=ctx.clock.now + timedelta(hours=1), params_sha256="0" * 64)


def dispatch_setup(tmp_path, name="dispatch", *, permit=None, registry=None, revision=1, **fx):
    clock = FakeClock()
    db = Ledger(tmp_path / (name + ".db"), clock=clock)
    ctx, plan = reserve(db, fixture(**fx))
    db.register_control_update(control_state(ctx, revision, permit_id=permit.permit_id if permit else None,
                                             mode=c.ExecutionMode.NATIVE if permit else None),
                               permit=permit, evidence_registry=registry if permit else None)
    ctx = sync(db, ctx, ctx.reservation_receipt)
    return db, clock, ctx, plan


def test_scope_key_is_deterministic_across_control_roundtrips(tmp_path):
    db, clock, ctx, plan = dispatch_setup(tmp_path, "scope-key")
    assert db.control_revision(ctx.binding) == 1
    with sqlite3.connect(db.path) as con:
        stored = con.execute("SELECT scope FROM control_state").fetchone()[0]
    assert stored == "SIM_A|SIM_S|SHFE|rb2610|1"
    other = replace(ctx.binding, session_epoch="OTHER")
    assert db.control_revision(other) == 1  # scope ignores session epoch by design
    third = replace(ctx.binding, instrument_id="cu2610")
    assert db.control_revision(third) is None


def test_v1_journal_refuses_dispatch_apis(tmp_path):
    with Ledger(tmp_path / "v1.db", clock=FakeClock(), schema="v1") as db:
        with pytest.raises(LedgerError, match="SCHEMA_UNSUPPORTED"):
            DispatchJournal(db).pending_commands(fixture().binding)
        with pytest.raises(LedgerError, match="CONTROL_SCHEMA_UNSUPPORTED"):
            db.register_control_update(control_state(fixture(), 1))


def test_H12_H13_control_cas_rejects_stale_dispatch_and_keeps_monotonic_revision(tmp_path):
    db, clock, ctx, plan = dispatch_setup(tmp_path, "h13")
    journal = DispatchJournal(db)
    persisted = db.context(ctx.binding)
    record_first_offer(db, ctx, plan, plan.legs[0], 1, persisted)
    offer = offer_for(ctx, plan, plan.legs[0], 1)
    # Same offer id with changed bytes is a durable-record conflict (C1.4).
    with pytest.raises(LedgerError, match="OFFER_DEADLINE_CONFLICT"):
        journal.begin_dispatch(offer_for(ctx, plan, plan.legs[0], 0), ctx, expected_control_revision=1)
    with pytest.raises(LedgerError, match="CONTEXT_CONFLICT"):
        journal.begin_dispatch(offer, ctx, expected_control_revision=0)
    assert db.get_leg(plan.legs[0].leg_id).attempt_no == 0
    with Ledger(db.path, clock=clock) as other:
        other.register_control_update(control_state(ctx, 2, disabled="gate-closed"))
    with pytest.raises(LedgerError, match="CONTEXT_CONFLICT"):
        journal.begin_dispatch(offer, ctx, expected_control_revision=1)
    assert db.get_leg(plan.legs[0].leg_id).attempt_no == 0
    with pytest.raises(LedgerError, match="CONTEXT_CONFLICT"):
        db.register_control_update(control_state(ctx, 1))


def test_H14_control_revision_is_independent_of_ledger_revision(tmp_path):
    db, clock, ctx, plan = dispatch_setup(tmp_path, "h14")
    reserve(db, fixture(name="other-leg", instrument="cu2610"))
    ctx = sync(db, ctx, ctx.reservation_receipt)
    persisted = db.context(ctx.binding)
    record_first_offer(db, ctx, plan, plan.legs[0], 1, persisted)
    command = DispatchJournal(db).begin_dispatch(offer_for(ctx, plan, plan.legs[0], 1), ctx, expected_control_revision=1)
    assert command.control_revision == 1 and command.permit_id == "sim-scope"


def test_H15_poll_and_offer_replay_create_exactly_one_ticket(tmp_path):
    db, clock, ctx, plan = dispatch_setup(tmp_path, "h15")
    journal = DispatchJournal(db)
    persisted = db.context(ctx.binding)
    offer = offer_for(ctx, plan, plan.legs[0], 1)
    record_first_offer(db, ctx, plan, plan.legs[0], 1, persisted)
    command = journal.begin_dispatch(offer, ctx, expected_control_revision=1)
    for _ in range(5):
        assert journal.begin_dispatch(offer, ctx, expected_control_revision=1) == command
    for _ in range(99):
        db.register(ctx.intent)
    with sqlite3.connect(db.path) as con:
        assert con.execute("SELECT COUNT(*) FROM dispatch_commands").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 1


def test_H16_dispatch_commit_crash_window_keeps_single_ticket(tmp_path):
    db, clock, ctx, plan = dispatch_setup(tmp_path, "h16")
    offer = offer_for(ctx, plan, plan.legs[0], 1)
    record_first_offer(db, ctx, plan, plan.legs[0], 1, db.context(ctx.binding))
    command = DispatchJournal(db).begin_dispatch(offer, ctx, expected_control_revision=1)
    db.close()
    with Ledger(db.path, clock=clock) as reopened:
        assert reopened.get_leg(plan.legs[0].leg_id).state is c.OrderState.SUBMIT_UNKNOWN
        assert reopened.pending_dispatch_commands(ctx.binding) == (command,)
        ctx = sync(reopened, ctx, ctx.reservation_receipt)
        assert DispatchJournal(reopened).begin_dispatch(offer, ctx, expected_control_revision=1) == command
        with sqlite3.connect(reopened.path) as con:
            assert con.execute("SELECT COUNT(*) FROM dispatch_commands").fetchone()[0] == 1


def test_H17_receipt_stages_are_idempotent_and_conflicts_latch(tmp_path):
    db, clock, ctx, plan = dispatch_setup(tmp_path, "h17")
    journal = DispatchJournal(db)
    record_first_offer(db, ctx, plan, plan.legs[0], 1, db.context(ctx.binding))
    command = journal.begin_dispatch(offer_for(ctx, plan, plan.legs[0], 1), ctx, expected_control_revision=1)
    consumed = receipt_for(command, ctx, stage=nc.ReceiptStage.CONSUMED,
                           result_kind=nc.ResultKind.EXCEPTION, error_code="OUTCOME_PENDING")
    assert journal.record_receipt(consumed).code is c.ResultCode.OK
    assert db.get_leg(plan.legs[0].leg_id).state is c.OrderState.SUBMITTING
    assert journal.record_receipt(consumed).code is c.ResultCode.IDEMPOTENT
    returned = receipt_for(command, ctx, stage=nc.ReceiptStage.RETURNED,
                           result_kind=nc.ResultKind.LOCAL_ACK, result_value=7, receipt_id="receipt-2")
    journal.record_receipt(returned)
    record = db.get_leg(plan.legs[0].leg_id)
    assert record.state is c.OrderState.SUBMITTED_LOCAL and record.order_id == 7
    assert journal.record_receipt(returned).code is c.ResultCode.IDEMPOTENT
    with pytest.raises(LedgerError, match="OWNERSHIP_CONFLICT"):
        journal.record_receipt(replace(returned, result_value=8))
    assert c.ReconcileReason.OWNERSHIP_CONFLICT in db.check().reconcile.reasons


@pytest.mark.parametrize("stage,kind,value,error,expected,cash", [
    (nc.ReceiptStage.RETURNED, nc.ResultKind.LOCAL_ACK, 7, None, c.OrderState.SUBMITTED_LOCAL, 2000),
    (nc.ReceiptStage.RETURNED, nc.ResultKind.LOCAL_REJECTED, None, None, c.OrderState.LOCAL_REJECTED, 0),
    (nc.ReceiptStage.RETURNED, nc.ResultKind.EXCEPTION, None, "E_CALL", c.OrderState.SUBMIT_UNKNOWN, 2000),
    (nc.ReceiptStage.RETURNED, nc.ResultKind.TIMEOUT, None, "E_TIMEOUT", c.OrderState.SUBMIT_UNKNOWN, 2000),
    (nc.ReceiptStage.RETURNED, nc.ResultKind.INVALID_RETURN, None, "E_BAD", c.OrderState.SUBMIT_UNKNOWN, 2000),
    (nc.ReceiptStage.NO_CALL, None, None, None, c.OrderState.SUBMIT_UNKNOWN, 2000),
])
def test_H18_receipt_result_kinds_project_like_native_returns(tmp_path, stage, kind, value, error, expected, cash):
    db, clock, ctx, plan = dispatch_setup(tmp_path, "h18")
    journal = DispatchJournal(db)
    record_first_offer(db, ctx, plan, plan.legs[0], 1, db.context(ctx.binding))
    command = journal.begin_dispatch(offer_for(ctx, plan, plan.legs[0], 1), ctx, expected_control_revision=1)
    journal.record_receipt(receipt_for(command, ctx, stage=stage, result_kind=kind, result_value=value, error_code=error))
    assert db.get_leg(plan.legs[0].leg_id).state is expected
    assert db.view("SIM_A").cash_reserved == D(cash)


def test_H23_ack_watermark_monotonic_and_gap_blocks_dispatch(tmp_path):
    db, clock, ctx, plan = dispatch_setup(tmp_path, "h23")
    db.ingest_event(event(ctx, plan.legs[0]))
    assert db.ack_outbox(ctx.binding, "event-1") is True
    assert db.ack_outbox(ctx.binding, "event-1") is False
    with pytest.raises(LedgerError, match="EVENT_NOT_FOUND"):
        db.ack_outbox(ctx.binding, "missing")
    db.note_event_gap("queue-overflow")
    record_first_offer(db, ctx, plan, plan.legs[0], 1, db.context(ctx.binding))
    with pytest.raises(LedgerError, match="RECONCILE_REQUIRED"):
        DispatchJournal(db).begin_dispatch(offer_for(ctx, plan, plan.legs[0], 1), ctx, expected_control_revision=1)


def test_H24_permit_budget_consumed_atomically_and_cancel_budget_separate(tmp_path):
    registry = AcceptingRegistry()
    clock = FakeClock()
    db = Ledger(tmp_path / "h24.db", clock=clock)
    ctx, plan = reserve(db, fixture())
    permit = pilot_permit(ctx, 1)
    db.register_control_update(control_state(ctx, 1, permit_id=permit.permit_id, mode=c.ExecutionMode.NATIVE),
                               permit=permit, evidence_registry=registry)
    ctx = sync(db, ctx, ctx.reservation_receipt)
    journal = DispatchJournal(db, evidence_registry=registry)
    record_first_offer(db, ctx, plan, plan.legs[0], 1, db.context(ctx.binding), registry=registry)
    command = journal.begin_dispatch(offer_for(ctx, plan, plan.legs[0], 1), ctx, expected_control_revision=1)
    assert command.permit_id == "permit-1"
    # A second intent on the same pilot scope: control moves to revision 2 with the
    # SAME permit, whose consumed send budget must not re-arm.
    other, other_plan = reserve(db, fixture(name="second-pilot"))
    db.register_control_update(control_state(other, 2, permit_id=permit.permit_id, mode=c.ExecutionMode.NATIVE),
                               permit=replace(permit, control_revision=2), evidence_registry=registry)
    other = sync(db, other, other.reservation_receipt)
    record_first_offer(db, other, other_plan, other_plan.legs[0], 2, db.context(other.binding), registry=registry,
                       offer=offer_for(other, other_plan, other_plan.legs[0], 2, offer_id="offer-2"))
    with pytest.raises(LedgerError, match="PERMIT_EXHAUSTED"):
        journal.begin_dispatch(offer_for(other, other_plan, other_plan.legs[0], 2, offer_id="offer-2"),
                               other, expected_control_revision=2)
    assert db.get_leg(other_plan.legs[0].leg_id).attempt_no == 0
    journal.record_receipt(receipt_for(command, ctx, stage=nc.ReceiptStage.RETURNED,
                                       result_kind=nc.ResultKind.LOCAL_ACK, result_value=7, receipt_id="r2"))
    db.ingest_event(event(ctx, plan.legs[0]))
    ctx = sync(db, ctx, ctx.reservation_receipt)
    record_first_offer(db, ctx, plan, plan.legs[0], 2, db.context(ctx.binding), registry=registry,
                       kind=nc.CommandKind.CANCEL, cancel_id="dc-" + plan.legs[0].leg_id,
                       offer=offer_for(ctx, plan, plan.legs[0], 2, offer_id="offer-cancel"))
    with pytest.raises(LedgerError, match="PERMIT_EXHAUSTED"):
        journal.begin_dispatch(offer_for(ctx, plan, plan.legs[0], 2, offer_id="offer-cancel"),
                               ctx, expected_control_revision=2)


def test_H26_replaceable_requires_terminal_settled_gap_free_leg(tmp_path):
    clock = FakeClock()
    with Ledger(tmp_path / "h26.db", clock=clock) as db:
        ctx, plan = start(db, fixture(volume=4))
        leg = plan.legs[0]
        db.ingest_event(event(ctx, leg, event_id="f", status=c.OrderState.FILLED, traded=4))
        db.ingest_event(event(ctx, leg, event_id="t", kind=c.EventKind.TRADE, trade_id="T1", volume=4))
        with pytest.raises(LedgerError, match="RECONCILE_REQUIRED"):
            db.assert_replaceable(leg.leg_id)
        absorb(db, ctx, leg, clock, position=4)
        assert db.assert_replaceable(leg.leg_id).state is c.OrderState.FILLED


def test_H31_committed_ticket_survives_revocation_as_single_potential_call(tmp_path):
    db, clock, ctx, plan = dispatch_setup(tmp_path, "h31")
    record_first_offer(db, ctx, plan, plan.legs[0], 1, db.context(ctx.binding))
    command = DispatchJournal(db).begin_dispatch(offer_for(ctx, plan, plan.legs[0], 1), ctx, expected_control_revision=1)
    # A second intent on the same scope records its offer before the control
    # actor closes the gate; the closed gate must still block its dispatch.
    second, second_plan = reserve(db, fixture(name="h31-second"))
    second = sync(db, second, second.reservation_receipt)
    record_first_offer(db, second, second_plan, second_plan.legs[0], 1, db.context(second.binding),
                       offer=offer_for(second, second_plan, second_plan.legs[0], 1, offer_id="offer-2"))
    db.register_control_update(control_state(ctx, 2, disabled="gate-closed"))
    with pytest.raises(LedgerError, match="CONTEXT_CONFLICT"):
        DispatchJournal(db).begin_dispatch(offer_for(second, second_plan, second_plan.legs[0], 1, offer_id="offer-2"),
                                           second, expected_control_revision=1)
    assert db.pending_dispatch_commands(ctx.binding) == (command,)
    assert db.recover().unresolved_command_ids == (command.command_id,)


def test_H32_reconnect_recovers_ticket_and_late_returned_still_projects(tmp_path):
    db, clock, ctx, plan = dispatch_setup(tmp_path, "h32")
    record_first_offer(db, ctx, plan, plan.legs[0], 1, db.context(ctx.binding))
    command = DispatchJournal(db).begin_dispatch(offer_for(ctx, plan, plan.legs[0], 1), ctx, expected_control_revision=1)
    db.close()
    with Ledger(db.path, clock=clock) as reopened:
        assert reopened.recover().unresolved_command_ids == (command.command_id,)
        ctx = sync(reopened, ctx, ctx.reservation_receipt)
        result = DispatchJournal(reopened).record_receipt(
            receipt_for(command, ctx, stage=nc.ReceiptStage.RETURNED,
                        result_kind=nc.ResultKind.LOCAL_ACK, result_value=7, receipt_id="r-late"))
        assert result.leg.state is c.OrderState.SUBMITTED_LOCAL
        assert reopened.pending_dispatch_commands(ctx.binding) == ()


def test_H36_migration_conserves_counts_and_enables_dispatch(tmp_path):
    clock = FakeClock()
    source = tmp_path / "v1.db"
    with Ledger(source, clock=clock, schema="v1") as db:
        ctx, plan = start(db, fixture(volume=4))
        db.ingest_event(event(ctx, plan.legs[0]))
        before = counts(source)
    output = tmp_path / "v2-migrated.db"
    proc = subprocess.run([sys.executable, "-B", "scripts/migrate_infiny_execution.py",
                           "--source", str(source), "--output", str(output)],
                          cwd="D:/Projects/AK-trading", capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["conserved"] is True and report["counts_after"] == report["counts_before"] == before
    assert output.exists() and source.exists()
    with Ledger(output, clock=clock) as migrated:
        migrated_state = migrated.get_leg(plan.legs[0].leg_id)
        assert migrated_state.state is c.OrderState.WORKING and migrated_state.ownership_verified
        migrated.register_control_update(control_state(ctx, 1))
        assert migrated.control_revision(ctx.binding) == 1


# ---- NE1 C1.1 RULING 04/05/06/07/09: registrations, epochs, replay, cursors ----


def operator_manifest(*, registration_id="man-1", record_path="D:/reg/actor.json", record_sha256=None):
    return ne.OperatorRegistrationManifest(
        registration_id=registration_id, record_type=ne.OperatorRecordType.ACTOR_REGISTRATION,
        record_path=record_path, record_sha256=record_sha256 or "b" * 64, contract_lock_sha256="c" * 64,
        operator_action_id="op-1", authorization_artifact_path="D:/reg/authz.json",
        authorization_artifact_sha256="d" * 64,
        artifacts=(ne.OperatorArtifact(path="D:/reg/actor.json", sha256="b" * 64),))


def envelopes(*sequences, stream="stream-1", epoch="SIM_E", generation="gen-1"):
    return tuple(nc.RawCallbackEnvelope(
        event_id=f"ev-{sequence}", kind=nc.EventKind.ORDER, event_stream_id=stream, event_sequence=sequence,
        captured_at=NOW, callback_generation=generation, callback_session_epoch=epoch,
        route_evidence_id=None, raw_fields=(nc.RawCallbackField(key="OrderID", value="7"),))
        for sequence in sequences)


def test_actor_registration_requires_maintenance_and_is_idempotent_then_versioned(tmp_path):
    with Ledger(tmp_path / "actor.db", clock=FakeClock()) as db:
        with pytest.raises(LedgerError, match="MAINTENANCE_REQUIRED"):
            db.register_actor("actor-1", "SIM_A", (fixture().binding,), operator_action_id="op-1",
                              manifest_sha256="a" * 64, contract_lock_sha256="b" * 64)
        with db.maintenance():
            revision = db.register_actor("actor-1", "SIM_A", (fixture().binding,), operator_action_id="op-1",
                                         manifest_sha256="a" * 64, contract_lock_sha256="b" * 64)
            assert revision == 1
            assert db.register_actor("actor-1", "SIM_A", (fixture().binding,), operator_action_id="op-1",
                                     manifest_sha256="a" * 64, contract_lock_sha256="b" * 64) == 1
            with pytest.raises(LedgerError, match="ACTOR_CONFLICT"):
                db.register_actor("actor-1", "SIM_A", (fixture().binding,), operator_action_id="op-2",
                                  manifest_sha256="a" * 64, contract_lock_sha256="b" * 64)
            assert db.register_actor("actor-1", "SIM_A", (fixture().binding,), operator_action_id="op-2",
                                     manifest_sha256="a" * 64, contract_lock_sha256="b" * 64,
                                     expected_registration_revision=1) == 2
        assert db.actors()[0].registration_revision == 2
        assert db.actors()[0].bindings == (fixture().binding,)


def test_actor_capacity_and_duplicate_bindings_rejected(tmp_path):
    with Ledger(tmp_path / "capacity.db", clock=FakeClock()) as db:
        with db.maintenance():
            for index in range(8):
                assert db.register_actor(f"actor-{index}", "SIM_A", (fixture().binding,), operator_action_id="op",
                                         manifest_sha256="a" * 64, contract_lock_sha256="b" * 64) == 1
            with pytest.raises(LedgerError, match="ACTOR_CAPACITY"):
                db.register_actor("actor-9", "SIM_A", (fixture().binding,), operator_action_id="op",
                                  manifest_sha256="a" * 64, contract_lock_sha256="b" * 64)
        with pytest.raises(ValueError):
            db.register_actor("bad", "SIM_A", (fixture().binding, fixture().binding), operator_action_id="op",
                              manifest_sha256="a" * 64, contract_lock_sha256="b" * 64)


def test_operator_manifest_persists_idempotently_and_rejects_bare_dotted_paths(tmp_path):
    with Ledger(tmp_path / "manifest.db", clock=FakeClock()) as db:
        with db.maintenance():
            db.register_operator_manifest(operator_manifest())
            db.register_operator_manifest(operator_manifest())
            with pytest.raises(LedgerError, match="MANIFEST_CONFLICT"):
                db.register_operator_manifest(operator_manifest(record_sha256="e" * 64))
        with pytest.raises(LedgerError, match="MAINTENANCE_REQUIRED"):
            db.register_operator_manifest(operator_manifest(registration_id="man-2"))
        with db.maintenance():
            with pytest.raises(LedgerError, match="MANIFEST_PATH_INVALID"):
                db.register_operator_manifest(operator_manifest(registration_id="man-3", record_path="modules.actor"))


def test_service_epoch_strictly_increasing_across_restarts(tmp_path):
    path = tmp_path / "epoch.db"
    with Ledger(path, clock=FakeClock()) as db:
        assert db.service_epoch() is None
        assert db.allocate_service_epoch() == nc.INITIAL_SERVICE_EPOCH == 1
        assert db.allocate_service_epoch() == 2
    with Ledger(path, clock=FakeClock()) as reopened:
        assert reopened.service_epoch() == 2
        assert reopened.allocate_service_epoch() == 3


def test_http_request_replay_same_fingerprint_idempotent_changed_conflicts(tmp_path):
    with Ledger(tmp_path / "replay.db", clock=FakeClock()) as db:
        code, cached = db.record_http_request(instance_id="I", session_epoch="E", generation="G",
                                              service_epoch=1, request_id="r-1", fingerprint="f" * 64, response="resp")
        assert code is c.ResultCode.OK and cached is None
        db.store_http_response(instance_id="I", session_epoch="E", generation="G", service_epoch=1,
                               request_id="r-1", fingerprint="f" * 64, response="resp")
        code, cached = db.record_http_request(instance_id="I", session_epoch="E", generation="G",
                                              service_epoch=1, request_id="r-1", fingerprint="f" * 64)
        assert code is c.ResultCode.IDEMPOTENT and cached == "resp"
        with pytest.raises(LedgerError, match="REQUEST_ID_CONFLICT"):
            db.record_http_request(instance_id="I", session_epoch="E", generation="G",
                                   service_epoch=1, request_id="r-1", fingerprint="0" * 64)
        assert db.record_http_request(instance_id="I", session_epoch="E", generation="G",
                                      service_epoch=2, request_id="r-1", fingerprint="0" * 64)[0] is c.ResultCode.OK


def client_captured(name="cap"):
    ctx = fixture(name=name)
    snap = ctx.snapshot
    blank = lambda comp: replace(comp, received_at=None)
    times = replace(snap.times, received_at=None, sent_at=None, quote=blank(snap.times.quote),
                    contract=blank(snap.times.contract), account=blank(snap.times.account),
                    positions=blank(snap.times.positions))
    positions = tuple(replace(item, timing=blank(item.timing)) for item in snap.positions)
    return replace(snap, times=times, positions=positions)


def test_capture_identity_binding_idempotent_and_scope_conflict(tmp_path):
    with Ledger(tmp_path / "capture.db", clock=FakeClock()) as db:
        snap = client_captured("cap")
        received = db.register_capture(snap, instance_id="INST-1", session_epoch="E", generation="G1")
        assert db.register_capture(snap, instance_id="INST-1", session_epoch="E", generation="G1") == received
        with pytest.raises(LedgerError, match="SNAPSHOT_CONFLICT"):
            db.register_capture(snap, instance_id="INST-2", session_epoch="E", generation="G1")
        with pytest.raises(LedgerError, match="SNAPSHOT_CONFLICT"):
            db.register_capture(client_captured("cap"), instance_id="INST-1", session_epoch="E", generation="G2")
        assert db.capture_registered("snapshot-cap") is True
        stamped = replace(snap.times, received_at=NOW)
        with pytest.raises(LedgerError, match="SNAPSHOT_INVALID"):
            db.register_capture(replace(snap, times=stamped), instance_id="INST-1", session_epoch="E", generation="G1")


def test_event_stream_contiguous_checkpoint_with_gap_fill_and_conflicts(tmp_path):
    with Ledger(tmp_path / "stream.db", clock=FakeClock()) as db:
        mark = db.record_event_envelopes(envelopes(1, 3), instance_id="INST-1")
        assert mark.sequence == 1  # saving 3 while 2 is missing cannot advance past 1
        assert db.record_event_envelopes(envelopes(2), instance_id="INST-1").sequence == 3
        assert db.record_event_envelopes(envelopes(1, 3), instance_id="INST-1").sequence == 3
        forged = envelopes(3)
        with pytest.raises(LedgerError, match="EVENT_CONFLICT"):
            db.record_event_envelopes((replace(forged[0], event_id="ev-x"),), instance_id="INST-1")
        assert c.ReconcileReason.EVENT_GAP in db.check().reconcile.reasons
        assert db.ack_watermark(event_stream_id="stream-1").sequence == 3
        with pytest.raises(LedgerError, match="EVENT_STREAM_MIXED"):
            db.record_event_envelopes(envelopes(4, stream="stream-2") + envelopes(4), instance_id="INST-1")


def test_client_ack_validation_above_or_inconsistent_rejected_lower_stale_ok(tmp_path):
    with Ledger(tmp_path / "ack.db", clock=FakeClock()) as db:
        durable = db.record_event_envelopes(envelopes(1, 2), instance_id="INST-1")
        db.validate_client_ack(replace(durable, sequence=1, event_id="ev-1", event_sha256=durable.event_sha256))
        with pytest.raises(LedgerError, match="ACK_WATERMARK_INVALID"):
            db.validate_client_ack(replace(durable, sequence=3, event_id="ev-3", event_sha256="0" * 64))
        with pytest.raises(LedgerError, match="ACK_WATERMARK_INVALID"):
            db.validate_client_ack(replace(durable, event_id="ev-9"))
        assert db.ack_watermark(event_stream_id="stream-x") is None
        with pytest.raises(LedgerError, match="ACK_WATERMARK_INVALID"):
            db.validate_client_ack(nc.AckWatermark(event_stream_id="stream-x", instance_id="INST-1",
                                                   session_epoch="SIM_E", generation="gen-1", sequence=1,
                                                   event_id="ev-1", event_sha256="0" * 64))


def test_execution_counters_report_journal_facts_as_interval_not_invented(tmp_path):
    registry = AcceptingRegistry()
    clock = FakeClock()
    with Ledger(tmp_path / "counters.db", clock=clock) as db:
        ctx, plan = reserve(db, fixture())
        permit = pilot_permit(ctx, 1)
        db.register_control_update(control_state(ctx, 1, permit_id=permit.permit_id, mode=c.ExecutionMode.NATIVE),
                                   permit=permit, evidence_registry=registry)
        ctx = sync(db, ctx, ctx.reservation_receipt)
        journal = DispatchJournal(db, evidence_registry=registry)
        record_first_offer(db, ctx, plan, plan.legs[0], 1, db.context(ctx.binding), registry=registry)
        command = journal.begin_dispatch(offer_for(ctx, plan, plan.legs[0], 1), ctx, expected_control_revision=1)
        journal.record_receipt(receipt_for(command, ctx, stage=nc.ReceiptStage.CONSUMED,
                                           result_kind=nc.ResultKind.EXCEPTION, error_code="OUTCOME_PENDING"))
        mid = db.execution_counters(ctx.binding)
        assert (mid.committed_dispatch_count, mid.consumed_receipt_count, mid.unresolved_dispatch_count) == (1, 1, 1)
        assert mid.count_complete is False and mid.native_send_attempts.value is None
        assert mid.native_send_attempts.lower_bound == 1
        journal.record_receipt(receipt_for(command, ctx, stage=nc.ReceiptStage.RETURNED,
                                           result_kind=nc.ResultKind.LOCAL_ACK, result_value=7, receipt_id="r2"))
        db.ingest_event(event(ctx, plan.legs[0]))
        facts = db.execution_counters(ctx.binding)
        assert (facts.committed_dispatch_count, facts.returned_receipt_count, facts.unresolved_dispatch_count) == (1, 1, 0)
        assert facts.local_order_ids == (7,) and facts.unique_trade_count == 0
        assert facts.native_cancel_attempts.lower_bound == 0 and facts.count_complete is False


# ---- NE1 C1.2 dispatch-window seam (DTTL-03/04/05) ----


def seam_setup(tmp_path, name, *, registry=None):
    clock = FakeClock()
    db = Ledger(tmp_path / (name + ".db"), clock=clock)
    ctx, plan = reserve(db, fixture())
    permit = pilot_permit(ctx, 1) if registry is not None else None
    db.register_control_update(control_state(ctx, 1, permit_id=permit.permit_id if permit else None,
                                             mode=c.ExecutionMode.NATIVE if permit else None),
                               permit=permit, evidence_registry=registry)
    ctx = sync(db, ctx, ctx.reservation_receipt)
    persisted = db.context(ctx.binding)
    journal = DispatchJournal(db, evidence_registry=registry)
    return db, clock, ctx, plan, persisted, journal


def test_seam_stale_expected_context_conflicts_with_zero_write(tmp_path):
    db, clock, ctx, plan, persisted, journal = seam_setup(tmp_path, "seam-stale")
    record_first_offer(db, ctx, plan, plan.legs[0], 1, persisted)
    stale = replace(persisted, clock=replace(persisted.clock, monotonic_ns=999))
    revision_before = db.check().revision
    with pytest.raises(LedgerError, match="CONTEXT_CONFLICT"):
        journal.begin_dispatch(offer_for(ctx, plan, plan.legs[0], 1), ctx,
                               expected_control_revision=1, expected_context=stale)
    assert db.check().revision == revision_before
    assert db.get_leg(plan.legs[0].leg_id).attempt_no == 0
    assert db.context(ctx.binding) == persisted


def test_seam_control_fields_are_not_refreshable_by_observation(tmp_path):
    db, clock, ctx, plan, persisted, journal = seam_setup(tmp_path, "seam-control")
    record_first_offer(db, ctx, plan, plan.legs[0], 1, persisted)
    for mutation in (
        lambda candidate: replace(candidate, policy=replace(candidate.policy, policy_id="smuggled")),
        lambda candidate: replace(candidate, authorization=None),
        lambda candidate: replace(candidate, route_ownership=None),
    ):
        candidate = mutation(persisted)
        with pytest.raises(LedgerError, match="CONTEXT_CONFLICT"):
            journal.begin_dispatch(offer_for(ctx, plan, plan.legs[0], 1), candidate,
                                   expected_control_revision=1, expected_context=persisted)
    assert db.get_leg(plan.legs[0].leg_id).attempt_no == 0
    assert db.context(ctx.binding) == persisted


def test_seam_single_commit_refreshes_context_latches_attempt_and_debits(tmp_path):
    db, clock, ctx, plan, persisted, journal = seam_setup(tmp_path, "seam-single")
    record_first_offer(db, ctx, plan, plan.legs[0], 1, persisted)
    fresh = fixture(name="seam-obs", now=clock.now)
    candidate = replace(persisted, snapshot=fresh.snapshot, proof=fresh.proof, clock=fresh.clock,
                        reservation_view=None)
    revision_before = db.check().revision
    command = journal.begin_dispatch(offer_for(ctx, plan, plan.legs[0], 1), candidate,
                                     expected_control_revision=1, expected_context=persisted)
    assert db.check().revision == revision_before + 1  # exactly one durable write transaction
    stored = db.context(ctx.binding)
    assert stored.snapshot.snapshot_id == fresh.snapshot.snapshot_id and stored.policy == persisted.policy
    record = db.get_leg(plan.legs[0].leg_id)
    assert record.attempt_no == 1 and record.state is c.OrderState.SUBMITTING
    with sqlite3.connect(db.path) as con:
        assert con.execute("SELECT COUNT(*) FROM dispatch_commands").fetchone()[0] == 1
    # Replay of the same offer is idempotent and never renews the window.
    replay = journal.begin_dispatch(offer_for(ctx, plan, plan.legs[0], 1), candidate,
                                    expected_control_revision=1, expected_context=stored)
    assert replay == command and replay.expires_at == command.expires_at


def test_seam_competing_revoke_between_read_and_dispatch_conflicts(tmp_path):
    db, clock, ctx, plan, persisted, journal = seam_setup(tmp_path, "seam-revoke")
    record_first_offer(db, ctx, plan, plan.legs[0], 1, persisted)
    other = Ledger(db.path, clock=clock)
    other.register_control_update(control_state(ctx, 2, disabled="gate-closed"))
    other.close()
    candidate = replace(persisted, reservation_view=None)
    with pytest.raises(LedgerError, match="CONTEXT_CONFLICT"):
        journal.begin_dispatch(offer_for(ctx, plan, plan.legs[0], 1), candidate,
                               expected_control_revision=1, expected_context=persisted)
    assert db.get_leg(plan.legs[0].leg_id).attempt_no == 0
    assert db.context(ctx.binding) == persisted


def test_seam_deadline_inheritance_never_renewed(tmp_path):
    db, clock, ctx, plan, persisted, journal = seam_setup(tmp_path, "seam-deadline")
    # An offer already at expiry cannot even be constructed/recorded (C1.4 DTO
    # and record gate both refuse a nonpositive window).
    with pytest.raises((c.ContractError, LedgerError)):
        journal.record_offer(replace(offer_for(ctx, plan, plan.legs[0], 1), expires_at=clock.now), ctx,
                             first_issued_at=clock.now, kind=nc.CommandKind.SUBMIT,
                             expected_control_revision=1, expected_context=persisted)
    assert db.get_leg(plan.legs[0].leg_id).attempt_no == 0
    early = replace(offer_for(ctx, plan, plan.legs[0], 1), expires_at=clock.now + timedelta(milliseconds=600))
    record_first_offer(db, ctx, plan, plan.legs[0], 1, persisted, offer=early)
    command = journal.begin_dispatch(early, ctx, expected_control_revision=1, expected_context=persisted)
    # The command inherits the durable record's absolute deadline, never a fresh 1500 ms.
    assert command.expires_at == clock.now + timedelta(milliseconds=600)
    clock.advance(0.1)
    replay = journal.begin_dispatch(early, ctx, expected_control_revision=1, expected_context=persisted)
    assert replay.expires_at == command.expires_at and replay.issued_at == command.issued_at
    assert db.get_leg(plan.legs[0].leg_id).attempt_no == 1  # only the first dispatch latched
    clock.advance(0.6)
    with pytest.raises(LedgerError, match="DISPATCH_EXPIRED"):
        journal.begin_dispatch(early, ctx, expected_control_revision=1, expected_context=persisted)
    assert journal.get_offer("offer-1").offer.expires_at == command.expires_at


def test_seam_post_commit_expiry_suppression_keeps_facts(tmp_path):
    db, clock, ctx, plan, persisted, journal = seam_setup(tmp_path, "seam-suppress")
    real_bump = db._bump

    def bump_then_expire():
        value = real_bump()
        clock.advance(2.0)  # the deadline expires after commit, before publication
        return value

    early = replace(offer_for(ctx, plan, plan.legs[0], 1), expires_at=clock.now + timedelta(milliseconds=600))
    record_first_offer(db, ctx, plan, plan.legs[0], 1, persisted, offer=early)
    db._bump = bump_then_expire
    journal2 = DispatchJournal(db)
    with pytest.raises(LedgerError, match="DISPATCH_EXPIRED"):
        journal2.begin_dispatch(early, ctx, expected_control_revision=1, expected_context=persisted)
    # Committed facts survive: the attempt and ticket are durable, send stays zero.
    record = db.get_leg(plan.legs[0].leg_id)
    assert record.attempt_no == 1 and record.state is c.OrderState.SUBMITTING
    assert db.pending_dispatch_commands(ctx.binding)
    with pytest.raises(LedgerError, match="DISPATCH_EXPIRED"):
        journal2.begin_dispatch(early, ctx, expected_control_revision=1, expected_context=persisted)


def test_seam_fault_injection_rolls_back_the_complete_transaction(tmp_path):
    db, clock, ctx, plan, persisted, journal = seam_setup(tmp_path, "seam-fault")
    record_first_offer(db, ctx, plan, plan.legs[0], 1, persisted)
    revision_before = db.check().revision
    with sqlite3.connect(db.path) as fault:
        # Fail at the last write: after refresh, attempt latch and permit/state work.
        fault.execute("CREATE TRIGGER fail_dispatch_insert BEFORE INSERT ON dispatch_commands "
                      "BEGIN SELECT RAISE(ABORT,'fault-before-dispatch'); END")
    fresh = fixture(name="seam-fault-obs", now=clock.now)
    candidate = replace(persisted, snapshot=fresh.snapshot, proof=fresh.proof, clock=fresh.clock,
                        reservation_view=None)
    with pytest.raises(LedgerError) as caught:
        journal.begin_dispatch(offer_for(ctx, plan, plan.legs[0], 1), candidate,
                               expected_control_revision=1, expected_context=persisted)
    assert caught.value.code is c.ResultCode.PERSISTENCE_ERROR
    # All-or-nothing: no observation, no attempt, no revision drift.
    assert db.check().revision == revision_before
    assert db.get_leg(plan.legs[0].leg_id).attempt_no == 0
    assert db.context(ctx.binding) == persisted
    with sqlite3.connect(db.path) as con:
        assert con.execute("SELECT COUNT(*) FROM dispatch_commands").fetchone()[0] == 0


def test_P17_cancel_recordable_after_plan_expiry_when_all_cancel_bounds_valid(tmp_path):
    """Review n3-r4 B1-r4 replica: CANCEL-D-01 excludes the original SUBMIT plan
    deadline; an owned cancel stays recordable and issuable after plan expiry."""
    registry = AcceptingRegistry()
    clock = FakeClock()
    with Ledger(tmp_path / "p17.db", clock=clock) as db:
        ctx, plan = reserve(db, fixture())
        db.register_native_route(route_evidence_for(ctx), evidence_registry=registry)
        permit = replace(pilot_permit(ctx, 1), max_cancel_attempts=1)
        db.register_control_update(control_state(ctx, 1, permit_id=permit.permit_id, mode=c.ExecutionMode.NATIVE),
                                   permit=permit, evidence_registry=registry)
        ctx = sync(db, ctx, ctx.reservation_receipt)
        persisted = db.context(ctx.binding)
        journal = DispatchJournal(db, evidence_registry=registry)
        submit = offer_for(ctx, plan, plan.legs[0], 1)
        journal.record_offer(submit, ctx, first_issued_at=clock.now, kind=nc.CommandKind.SUBMIT,
                             expected_control_revision=1, expected_context=persisted)
        command = journal.begin_dispatch(submit, ctx, expected_control_revision=1)
        journal.record_receipt(receipt_for(command, ctx, stage=nc.ReceiptStage.RETURNED,
                                           result_kind=nc.ResultKind.LOCAL_ACK, result_value=7, receipt_id="r1"))
        db.ingest_event(event(ctx, plan.legs[0]))
        # The original SUBMIT plan expires; every CANCEL-D-01 bound stays valid.
        clock.advance(4.0)
        assert clock.now > plan.expires_at
        ctx = sync(db, ctx, ctx.reservation_receipt)
        persisted = db.context(ctx.binding)
        # N4 builds the cancel offer from the CURRENT trusted clock (the frozen
        # context clock is only the last observation hint).
        cancel = replace(offer_for(ctx, plan, plan.legs[0], 1, offer_id="offer-cancel"),
                         expires_at=clock.now + timedelta(milliseconds=1400))
        record_first_offer(db, ctx, plan, plan.legs[0], 1, persisted, registry=registry,
                           kind=nc.CommandKind.CANCEL, cancel_id="dc-" + plan.legs[0].leg_id,
                           offer=cancel, first_issued_at=clock.now)
        cancel_command = journal.begin_dispatch(cancel, ctx, expected_control_revision=1)
        assert cancel_command.kind is nc.CommandKind.CANCEL
        assert db.get_leg(plan.legs[0].leg_id).cancel_state is c.CancelState.REQUESTED


def route_evidence_for(ctx):
    return ne.NativeRouteEvidence(
        evidence_id="route-p17", framework_sha256="f" * 64, sdk_sha256="5" * 64, binding=ctx.binding,
        callback_kinds=(ne.CallbackKind.ON_ORDER, ne.CallbackKind.ON_TRADE, ne.CallbackKind.ON_CANCEL),
        account_scope_basis="terminal export account column verified",
        strategy_scope_basis="strategy routing verified",
        local_alias_scope="per-session local ids", front_id=11, session_id=22,
        memo_limit=32, memo_roundtrip_verified=True, validated_at=ctx.clock.now,
        expires_at=ctx.clock.now + timedelta(days=1), origin=ne.Origin.SYNTHETIC)


def test_seam_p9_authorization_deadline_wins_over_full_ticket_budget(tmp_path):
    """Review n3-r1 B1/DTTL-03 replica: an authorization expiring at t+800ms
    must clamp the ticket even when the offer carries a longer deadline."""
    clock = FakeClock()
    db = Ledger(tmp_path / "p9.db", clock=clock)
    base = fixture(name="p9")
    short_auth = replace(base.authorization, expires_at=clock.now + timedelta(milliseconds=800))
    ctx, plan = reserve(db, replace(base, authorization=short_auth))
    db.register_control_update(control_state(ctx, 1))
    ctx = sync(db, ctx, ctx.reservation_receipt)
    persisted = db.context(ctx.binding)
    journal = DispatchJournal(db)
    # The N4-shaped long offer outlives the t+800ms authorization and is refused
    # by the first-record transaction itself (C1.4 independent original bounds).
    with pytest.raises(LedgerError, match="EXPIRED"):
        journal.record_offer(offer_for(ctx, plan, plan.legs[0], 1), ctx, first_issued_at=clock.now,
                             kind=nc.CommandKind.SUBMIT, expected_control_revision=1,
                             expected_context=persisted)
    assert db.get_leg(plan.legs[0].leg_id).attempt_no == 0
    compliant = replace(offer_for(ctx, plan, plan.legs[0], 1), expires_at=clock.now + timedelta(milliseconds=800))
    record_first_offer(db, ctx, plan, plan.legs[0], 1, persisted, offer=compliant)
    command = journal.begin_dispatch(compliant, ctx, expected_control_revision=1, expected_context=persisted)
    assert command.expires_at == clock.now + timedelta(milliseconds=800)
    assert command.expires_at < clock.now + timedelta(milliseconds=1500)
    record = db.get_leg(plan.legs[0].leg_id)
    assert record.attempt_no == 1 and record.state is c.OrderState.SUBMITTING


def test_seam_permit_deadline_wins_independently_and_fail_closed(tmp_path):
    """C1.3 RD-04: the registry deadline participates in D_current inside the
    transaction; an unavailable answer commits nothing."""
    registry = AcceptingRegistry(permit_deadline=NOW + timedelta(milliseconds=400))
    clock = FakeClock()
    db = Ledger(tmp_path / "permit-dl.db", clock=clock)
    ctx, plan = reserve(db, fixture())
    permit = pilot_permit(ctx, 1)
    db.register_control_update(control_state(ctx, 1, permit_id=permit.permit_id, mode=c.ExecutionMode.NATIVE),
                               permit=permit, evidence_registry=registry)
    ctx = sync(db, ctx, ctx.reservation_receipt)
    journal = DispatchJournal(db, evidence_registry=registry)
    tight = replace(offer_for(ctx, plan, plan.legs[0], 1),
                    expires_at=clock.now + timedelta(milliseconds=400))
    record_first_offer(db, ctx, plan, plan.legs[0], 1, db.context(ctx.binding), registry=registry, offer=tight)
    command = journal.begin_dispatch(tight, ctx, expected_control_revision=1)
    assert command.expires_at == clock.now + timedelta(milliseconds=400)
    assert command.expires_at < clock.now + timedelta(milliseconds=800)  # earlier than every other deadline
    # Fail-closed: a registry that cannot answer commits nothing at all.
    clock2 = FakeClock()
    db2 = Ledger(tmp_path / "permit-fc.db", clock=clock2)
    ctx2, plan2 = reserve(db2, fixture())
    permit2 = pilot_permit(ctx2, 1)
    working = AcceptingRegistry()
    db2.register_control_update(control_state(ctx2, 1, permit_id=permit2.permit_id, mode=c.ExecutionMode.NATIVE),
                                permit=permit2, evidence_registry=working)
    ctx2 = sync(db2, ctx2, ctx2.reservation_receipt)
    record_first_offer(db2, ctx2, plan2, plan2.legs[0], 1, db2.context(ctx2.binding), registry=working)
    revision_before = db2.check().revision
    with pytest.raises(LedgerError, match="NATIVE_DEADLINE_UNAVAILABLE"):
        DispatchJournal(db2, evidence_registry=AcceptingRegistry(deadline_ok=False)).begin_dispatch(
            offer_for(ctx2, plan2, plan2.legs[0], 1), ctx2, expected_control_revision=1)
    assert db2.get_leg(plan2.legs[0].leg_id).attempt_no == 0
    assert db2.check().revision == revision_before


def test_seam_default_path_unchanged_without_expected_context(tmp_path):
    db, clock, ctx, plan, persisted, journal = seam_setup(tmp_path, "seam-default")
    unregistered = replace(ctx, snapshot=fixture(name="never-registered", now=clock.now).snapshot,
                           reservation_view=None)
    record_first_offer(db, ctx, plan, plan.legs[0], 1, persisted)
    # Without expected_context the strict path rejects an unregistered candidate.
    with pytest.raises(LedgerError, match="CONTEXT_REQUIRED"):
        journal.begin_dispatch(offer_for(ctx, plan, plan.legs[0], 1), unregistered,
                               expected_control_revision=1)
    # No durable first record means no dispatch; ready cannot lazily create it.
    db2, clock2, ctx2, plan2, persisted2, journal2 = seam_setup(tmp_path, "seam-default-2")
    with pytest.raises(LedgerError, match="OFFER_DEADLINE_REQUIRED"):
        journal2.begin_dispatch(offer_for(ctx2, plan2, plan2.legs[0], 1), ctx2,
                                expected_control_revision=1)
    command = journal.begin_dispatch(offer_for(ctx, plan, plan.legs[0], 1), ctx,
                                     expected_control_revision=1)
    assert command.expires_at <= clock.now + timedelta(milliseconds=nc.DISPATCH_TTL_MS)


def test_F2_R3_P14_recovery_rebuild_has_zero_deadline_drift(tmp_path):
    """C1.4 OFD-02/03: a +3 s recovery rebuild cannot move the first deadline."""
    db, clock, ctx, plan, persisted, journal = seam_setup(tmp_path, "f2-p14")
    record_first_offer(db, ctx, plan, plan.legs[0], 1, persisted,
                       offer=replace(offer_for(ctx, plan, plan.legs[0], 1),
                                     expires_at=clock.now + timedelta(milliseconds=500)))
    original = journal.get_offer("offer-1")
    assert original.first_issued_at == clock.now and original.offer.expires_at == clock.now + timedelta(milliseconds=500)
    db.close()
    clock.advance(3.0)  # N4 process restart with empty caches at +3 s
    with Ledger(db.path, clock=clock) as reopened:
        journal_after = DispatchJournal(reopened)
        # A rebuilt same-ID candidate with ANY later deadline is a conflict; the
        # durable record keeps the original bytes untouched.
        with pytest.raises(LedgerError, match="OFFER_DEADLINE_CONFLICT"):
            journal_after.begin_dispatch(
                replace(offer_for(ctx, plan, plan.legs[0], 1), expires_at=clock.now + timedelta(milliseconds=1400)),
                ctx, expected_control_revision=1)
        stored = journal_after.get_offer("offer-1")
        assert stored == original and stored.offer.expires_at == original.offer.expires_at
        assert reopened.get_leg(plan.legs[0].leg_id).attempt_no == 0
        # Recovery with the ORIGINAL stored bytes fails only on the honest expiry gate.
        with pytest.raises(LedgerError, match="EXPIRED"):
            journal_after.begin_dispatch(original.offer, ctx, expected_control_revision=1)
        assert reopened.get_leg(plan.legs[0].leg_id).attempt_no == 0
        assert journal_after.get_offer("offer-1") == original


def test_F2_T05_concurrent_first_recording_exactly_one_winner(tmp_path):
    path = tmp_path / "race.db"
    clock = FakeClock()
    first = Ledger(path, clock=clock)
    ctx, plan = reserve(first, fixture())
    first.register_control_update(control_state(ctx, 1))
    ctx = sync(first, ctx, ctx.reservation_receipt)
    first.close()
    gate = threading.Barrier(2)

    def run(candidate_expiry):
        with Ledger(path, clock=clock) as db:
            journal = DispatchJournal(db)
            candidate = replace(offer_for(ctx, plan, plan.legs[0], 1), expires_at=candidate_expiry)
            gate.wait(timeout=5)
            try:
                return ("recorded", journal.record_offer(
                    candidate, ctx, first_issued_at=clock.now, kind=nc.CommandKind.SUBMIT,
                    expected_control_revision=1, expected_context=db.context(ctx.binding)))
            except LedgerError as exc:
                return ("rejected", exc.blockers)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (clock.now + timedelta(milliseconds=1400),
                                      clock.now + timedelta(milliseconds=900))))
    winners = [item for item in results if item[0] == "recorded"]
    assert len(winners) == 1  # exactly one first-record transaction commits
    with Ledger(path, clock=clock) as db:
        journal = DispatchJournal(db)
        stored = journal.get_offer("offer-1")
        assert stored.offer.expires_at == winners[0][1].offer.expires_at
        # The loser can only read the winner's original bytes; no duplicate action.
        with sqlite3.connect(path) as con:
            assert con.execute("SELECT COUNT(*) FROM offer_deadlines").fetchone()[0] == 1


def test_F2_T09_upgrade_tombstones_preexisting_legs_but_new_actions_work(tmp_path):
    path = tmp_path / "legacy.db"
    clock = FakeClock()
    with Ledger(path, clock=clock) as db:
        ctx, plan = reserve(db, fixture(name="legacy"))
        db.close()
    # Simulate a pre-C1.4 journal: the feature marker did not exist yet.
    with sqlite3.connect(path) as con:
        con.execute("DELETE FROM ledger_meta WHERE key='ofd_feature'")
        con.execute("DROP TABLE IF EXISTS offer_deadlines")
        con.commit()
    with Ledger(path, clock=clock) as db:
        db.register_control_update(control_state(ctx, 1))
        ctx = sync(db, ctx, ctx.reservation_receipt)
        with pytest.raises(LedgerError, match="OFFER_DEADLINE_UNPROVEN"):
            DispatchJournal(db).record_offer(
                offer_for(ctx, plan, plan.legs[0], 1), ctx, first_issued_at=clock.now,
                kind=nc.CommandKind.SUBMIT, expected_control_revision=1,
                expected_context=db.context(ctx.binding))
        with pytest.raises(LedgerError, match="OFFER_DEADLINE_REQUIRED"):
            DispatchJournal(db).begin_dispatch(offer_for(ctx, plan, plan.legs[0], 1), ctx,
                                               expected_control_revision=1)
        # A demonstrably never-issued C1.4 action on the same journal still works.
        fresh_ctx, fresh_plan = reserve(db, fixture(name="post-upgrade"))
        db.register_control_update(control_state(fresh_ctx, 2))
        fresh_ctx = sync(db, fresh_ctx, fresh_ctx.reservation_receipt)
        record_first_offer(db, fresh_ctx, fresh_plan, fresh_plan.legs[0], 2, db.context(fresh_ctx.binding),
                           offer=offer_for(fresh_ctx, fresh_plan, fresh_plan.legs[0], 2, offer_id="offer-fresh"))
        command = DispatchJournal(db).begin_dispatch(
            offer_for(fresh_ctx, fresh_plan, fresh_plan.legs[0], 2, offer_id="offer-fresh"),
            fresh_ctx, expected_control_revision=2)
        assert command.issued_at == clock.now





# ---- NE1 C1.5 one-transaction preparation seam (AP-01..04) ----


def prep_setup(tmp_path, name, *, registry=None):
    clock = FakeClock()
    db = Ledger(tmp_path / (name + ".db"), clock=clock)
    base = fixture()
    db.register_context(base)
    permit = pilot_permit(base, 1) if registry is not None else None
    db.register_control_update(control_state(base, 1, permit_id=permit.permit_id if permit else None,
                                             mode=c.ExecutionMode.NATIVE if permit else None),
                               permit=permit, evidence_registry=registry)
    persisted = db.context(base.binding)
    return db, clock, base, persisted, permit


def test_AP01_one_commit_preparation_with_receipt_and_view(tmp_path):
    registry = AcceptingRegistry()
    db, clock, base, persisted, permit = prep_setup(tmp_path, "ap01", registry=registry)
    journal = DispatchJournal(db, evidence_registry=registry)
    revision_before = db.check().revision
    plan, final = journal.prepare_native_submission(base.intent, base, expected_control_revision=1,
                                                    expected_context=persisted)
    assert db.check().revision == revision_before + 1  # exactly one mutating commit
    assert plan.legs and plan.intent_hash == c.canonical_hash(base.intent)
    assert final.reservation_receipt.plan_id == plan.plan_id
    assert final.reservation_view is not None and final.reservation_view.reconcile.required is False
    assert final.reservation_receipt.reservation_ids == tuple(leg.leg_id for leg in plan.legs)
    stored = db.context(base.binding)
    assert stored == final and db.get_leg(plan.legs[0].leg_id).state is c.OrderState.RESERVED
    # AP-03: the offer record stays a separate transaction on top (OFD-02 keeps
    # it free of resource-revision side effects, so the revision stays at +1).
    offer = offer_for(base, plan, plan.legs[0], 1)
    journal.record_offer(offer, final, first_issued_at=clock.now, kind=nc.CommandKind.SUBMIT,
                         expected_control_revision=1, expected_context=stored)
    assert db.check().revision == revision_before + 1
    with sqlite3.connect(db.path) as con:
        assert con.execute("SELECT COUNT(*) FROM offer_deadlines").fetchone()[0] == 1


def test_AP02_whole_unit_rolls_back_on_reservation_failure(tmp_path):
    db, clock, base, persisted, _ = prep_setup(tmp_path, "ap02")
    journal = DispatchJournal(db)
    revision_before = db.check().revision
    with sqlite3.connect(db.path) as fault:
        fault.execute("CREATE TRIGGER fail_reserve BEFORE INSERT ON reservations "
                      "BEGIN SELECT RAISE(ABORT,'fault-before-reserve'); END")
    with pytest.raises(LedgerError) as caught:
        journal.prepare_native_submission(base.intent, base, expected_control_revision=1,
                                          expected_context=persisted)
    assert caught.value.code is c.ResultCode.PERSISTENCE_ERROR
    assert db.check().revision == revision_before
    with sqlite3.connect(db.path) as con:
        assert con.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM legs").fetchone()[0] == 0
    assert db.context(base.binding) == persisted


def test_AP02_business_failure_rolls_back_intent_registration(tmp_path):
    db, clock, base, persisted, _ = prep_setup(tmp_path, "ap02b")
    tight = replace(base, policy=replace(base.policy, policy_id="tight", max_cash_reserve=D(10)))
    db.register_context(tight)
    persisted_tight = db.context(base.binding)
    journal = DispatchJournal(db)
    revision_before = db.check().revision
    with pytest.raises(LedgerError, match="INSUFFICIENT_CASH"):
        journal.prepare_native_submission(base.intent, tight, expected_control_revision=1,
                                          expected_context=persisted_tight)
    assert db.check().revision == revision_before
    with sqlite3.connect(db.path) as con:
        assert con.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0


def test_AP02_uncertain_commit_resolved_from_durable_facts(tmp_path):
    db, clock, base, persisted, _ = prep_setup(tmp_path, "ap02c")
    journal = DispatchJournal(db)
    plan, final = journal.prepare_native_submission(base.intent, base, expected_control_revision=1,
                                                    expected_context=persisted)
    db.close()
    with Ledger(db.path, clock=clock) as reopened:
        reopened_journal = DispatchJournal(reopened)
        stored_context = reopened.context(base.binding)
        revision_before = reopened.check().revision
        # Idempotent re-preparation returns the committed plan with zero writes.
        again_plan, again_ctx = reopened_journal.prepare_native_submission(
            base.intent, stored_context, expected_control_revision=1, expected_context=stored_context)
        assert again_plan == plan and reopened.check().revision == revision_before
        with sqlite3.connect(reopened.path) as con:
            assert con.execute("SELECT COUNT(*) FROM legs").fetchone()[0] == len(plan.legs)


def test_AP04_idempotent_reprepare_zero_writes_and_cancel_excluded(tmp_path):
    db, clock, base, persisted, _ = prep_setup(tmp_path, "ap04")
    journal = DispatchJournal(db)
    plan, final = journal.prepare_native_submission(base.intent, base, expected_control_revision=1,
                                                    expected_context=persisted)
    stored = db.context(base.binding)
    revision_before = db.check().revision
    same_plan, _ = journal.prepare_native_submission(base.intent, stored, expected_control_revision=1,
                                                     expected_context=stored)
    assert same_plan == plan and db.check().revision == revision_before  # no re-reserve, no new ID
    close = replace(base.intent, intent_id="intent-close", source_key="close",
                    action=c.Action.CLOSE_LONG)
    with pytest.raises(LedgerError, match="UNSUPPORTED_CLOSE_POLICY"):
        journal.prepare_native_submission(close, stored, expected_control_revision=1, expected_context=stored)


def test_AP04_competing_control_update_blocks_stale_preparation(tmp_path):
    db, clock, base, persisted, _ = prep_setup(tmp_path, "ap04b")
    journal = DispatchJournal(db)
    db.register_control_update(control_state(base, 2, disabled="gate-closed"))
    revision_before = db.check().revision
    with pytest.raises(LedgerError, match="CONTEXT_CONFLICT"):
        journal.prepare_native_submission(base.intent, base, expected_control_revision=1,
                                          expected_context=persisted)
    assert db.check().revision == revision_before
    with sqlite3.connect(db.path) as con:
        assert con.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0


def test_AP7_new_observation_adopted_in_the_same_transaction(tmp_path):
    """Review n3-r6 B1-r6 replica: a candidate carrying a NEW snapshot id (same
    business bytes) and a genuinely newer quote observation are both adopted
    inside the single preparation transaction — no separate pre-registration."""
    # (a) same business content under a brand-new snapshot id.
    db, clock, base, persisted, _ = prep_setup(tmp_path, "ap7a")
    journal = DispatchJournal(db)
    revision_before = db.check().revision
    renamed = replace(base, snapshot=replace(base.snapshot, snapshot_id="snapshot-ap7-alias"),
                      reservation_view=None)
    plan_a, final_a = journal.prepare_native_submission(base.intent, renamed,
                                                        expected_control_revision=1,
                                                        expected_context=persisted)
    assert final_a.snapshot.snapshot_id == "snapshot-ap7-alias"
    assert db.check().revision == revision_before + 1  # still exactly one commit
    assert db.get_leg(plan_a.legs[0].leg_id).state is c.OrderState.RESERVED

    # (b) a genuinely newer quote observation (new id, newer sampled/source times).
    db2, clock2, base2, persisted2, _ = prep_setup(tmp_path, "ap7b")
    clock2.advance(0.5)
    fresh = fixture(name="ap7-new", now=clock2.now)
    candidate2 = replace(fresh, intent=base2.intent, policy=base2.policy,
                         authorization=base2.authorization, reservation_view=None)
    journal2 = DispatchJournal(db2)
    revision_before2 = db2.check().revision
    plan_b, final_b = journal2.prepare_native_submission(base2.intent, candidate2,
                                                          expected_control_revision=1,
                                                          expected_context=persisted2)
    assert final_b.snapshot.snapshot_id == "snapshot-ap7-new"
    assert db2.check().revision == revision_before2 + 1
    assert db2.get_leg(plan_b.legs[0].leg_id).state is c.OrderState.RESERVED
    stored2 = db2.context(base2.binding)
    assert stored2 == final_b and stored2.reservation_receipt.plan_id == plan_b.plan_id


def test_variant_B_interleaved_write_between_prepare_and_ready(tmp_path):
    """Joint-faction replica: a legal interleaved journal write (any bump) must
    not make begin_dispatch's internal context rebuild raise ContractError."""
    registry = AcceptingRegistry()
    db, clock, base, persisted, permit = prep_setup(tmp_path, "vb", registry=registry)
    journal = DispatchJournal(db, evidence_registry=registry)
    plan, final = journal.prepare_native_submission(base.intent, base,
                                                    expected_control_revision=1,
                                                    expected_context=persisted)
    stored = db.context(base.binding)
    # A legal concurrent write bumps the journal revision without touching the
    # persisted context (HTTP replay bookkeeping, events, results, control...).
    db.record_http_request(instance_id="I", session_epoch="E", generation="G",
                           service_epoch=1, request_id="r-1", fingerprint="f" * 64)
    assert db.check().revision > final.ledger_revision
    offer = offer_for(base, plan, plan.legs[0], 1)
    journal.record_offer(offer, stored, first_issued_at=clock.now, kind=nc.CommandKind.SUBMIT,
                         expected_control_revision=1, expected_context=stored)
    command = journal.begin_dispatch(offer, final, expected_control_revision=1,
                                     expected_context=stored)
    assert command.kind is nc.CommandKind.SUBMIT
    assert db.get_leg(plan.legs[0].leg_id).state is c.OrderState.SUBMITTING
