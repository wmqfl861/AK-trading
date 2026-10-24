"""Explicitly fictitious, offline execution fixtures and reproducible demos.

No platform imports, native objects, environment flags, credentials or network
operations exist in this module. All account/evidence labels are SIMULATION.
"""
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import get_ident
from types import MappingProxyType

from . import contracts as c
from .native import NativeExecutionError

D = Decimal
SIMULATION_NOW = datetime(2026, 9, 14, 1, 30, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, now: datetime = SIMULATION_NOW):
        self.now = now
        self.monotonic_ns = 0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: Decimal) -> None:
        microseconds = int(seconds * D("1000000"))
        self.now += timedelta(microseconds=microseconds)
        self.monotonic_ns += microseconds * 1000


@dataclass(frozen=True, slots=True, kw_only=True)
class SimulatedInvestor:
    account_key: str
    source: c.Source = c.Source.SIMULATION


class FakeNative:
    """In-memory fake accepting only the exact SDK method/keyword vocabulary."""
    source = c.Source.SIMULATION
    verified_hedge_flags = ("1",)  # Fictitious fixture, not native hedge verification.

    def __init__(self, binding: c.Binding):
        if not binding.account_key.startswith("SIM_"):
            raise ValueError("FakeNative requires a fictitious SIM_ account")
        self.binding = binding
        self.investor_handle = SimulatedInvestor(account_key=binding.account_key)
        self.sent_requests: list[Mapping[str, object]] = []
        self.cancelled_ids: list[int] = []
        self.send_result: object = 7
        self.cancel_result: object = 1
        self.on_send: Callable[[Mapping[str, object], object], None] | None = None
        self.on_cancel: Callable[[int], None] | None = None

    def send_order(self, request: Mapping[str, object]) -> object:
        return self.sendOrder(**request)

    def cancel_order(self, order_id: int) -> object:
        return self.cancelOrder(OrderID=order_id)

    def sendOrder(self, *, StrategyID, Exchange, InstrumentID, Volume, Price,
                  Direction, OrderType, Investor, HedgeFlag, Offset, Market, Memo):
        b = self.binding
        if ((StrategyID, Exchange, InstrumentID) != (b.strategy_id, b.exchange, b.instrument_id) or
                Investor is not self.investor_handle or HedgeFlag not in self.verified_hedge_flags or
                type(Volume) is not int or Volume < 1 or type(Price) is not float or
                Direction not in ("0", "1") or OrderType not in ("0", "1") or
                Offset not in ("0", "1", "3") or Market is not False or type(Memo) is not str):
            raise ValueError("invalid simulated SDK request")
        request = MappingProxyType(dict(StrategyID=StrategyID, Exchange=Exchange, InstrumentID=InstrumentID,
                                        Volume=Volume, Price=Price, Direction=Direction, OrderType=OrderType,
                                        Investor=Investor, HedgeFlag=HedgeFlag, Offset=Offset, Market=Market, Memo=Memo))
        self.sent_requests.append(request)
        result = self.send_result
        if self.on_send is not None:
            self.on_send(request, result)
        if isinstance(result, BaseException):
            raise result
        if type(result) is int and result >= 0:
            self.send_result = result + 1
        return result

    def cancelOrder(self, *, OrderID):
        if type(OrderID) is not int or OrderID < 0:
            raise ValueError("invalid simulated SDK cancel route")
        self.cancelled_ids.append(OrderID)
        if self.on_cancel is not None:
            self.on_cancel(OrderID)
        if isinstance(self.cancel_result, BaseException):
            raise self.cancel_result
        return self.cancel_result


def simulation_context(*, clock: FakeClock | None = None, action: c.Action = c.Action.OPEN_LONG,
                       volume: int = 2, long: int = 0, short: int = 0, yesterday: int = 0,
                       exchange: str = "SHFE", order_type: c.OrderType = c.OrderType.GFD,
                       intent_id: str = "SIM_INTENT", revision: int = 0,
                       snapshot_id: str = "SIM_SNAPSHOT") -> c.ExecutionContext:
    """Construct fixture F; every source/evidence explicitly belongs to simulation."""
    clock = clock or FakeClock()
    now = clock()
    binding = c.Binding(account_key="SIM_ACCOUNT_A", strategy_key="SIM_STRATEGY_S", strategy_id=23,
                        instance_id="SIM_INSTANCE_I", session_epoch="SIM_EPOCH_E", trading_day=date(2026, 9, 14),
                        exchange=exchange, instrument_id="rb2610", hedge_flag="1")
    timing = c.ComponentTiming(sampled_at=now, source_at=None, received_at=now, snapshot_id=snapshot_id,
                               source=c.Source.SIMULATION, completeness=c.Completeness.COMPLETE)
    mark = c.SourceWatermark(source=c.Source.SIMULATION, stream_id="SIMULATION_STREAM", value=snapshot_id, as_of=now)
    coverage = c.Coverage(level=c.CoverageLevel.FULL_ACCOUNT, account_key=binding.account_key,
                          strategy_key=None, session_epoch=binding.session_epoch, trading_day=binding.trading_day,
                          source=c.Source.SIMULATION, as_of=now, complete_through=mark,
                          completion_evidence_id="SIM_BARRIER_" + snapshot_id, own_active_count=0, account_active_count=0)
    positions = tuple(c.PositionBucket(binding=binding, side=side, timing=timing, Position=qty, PositionClose=qty,
                                       FrozenClosing=0, YdPositionClose=min(yesterday, qty), YdFrozenClosing=0)
                      for side, qty in ((c.Side.LONG, long), (c.Side.SHORT, short)))
    sample = c.ExecutionSnapshot(
        snapshot_id=snapshot_id, binding=binding, source=c.Source.SIMULATION,
        times=c.SnapshotTimes(sampled_at=now, sent_at=now, received_at=now, quote=replace(timing, source_at=now),
                              contract=timing, account=timing, positions=timing),
        quote=c.Quote(binding=binding, time=now, last=D("3500"), bid1=D("3500"), ask1=D("3501"),
                      bid_volume1=10, ask_volume1=10, upper_limit=D("4000"), lower_limit=D("3000"),
                      market_status=c.MarketStatus.CONTINUOUS),
        contract=c.Contract(exchange=exchange, instrument_id=binding.instrument_id, asset_class=c.AssetClass.FUTURE,
                            expiry=date(2026, 10, 15), price_tick=D("1"), multiplier=D("10"),
                            min_limit_order_volume=1, max_limit_order_volume=5),
        account=c.Account(binding=binding, available=D("100000"), currency="CNY"), positions=positions,
        coverage=coverage, proof_id="SIM_PROOF_" + snapshot_id, ledger_revision=revision)
    proof = c.ReconciliationProof(proof_id=sample.proof_id, source=c.Source.SIMULATION,
                                  account_key=binding.account_key, trading_day=binding.trading_day,
                                  session_epoch=binding.session_epoch, as_of=now, order_inventory=(), trade_inventory=(),
                                  position_snapshot_id=snapshot_id, complete_through=mark,
                                  exclusive_writer_verified=True, verified=True)
    rules = c.RiskPolicy(policy_id="SIM_POLICY", valid_until=now + timedelta(days=1), max_leg_qty=5,
                         max_gross_position=100, max_inflight_legs=1, max_cash_reserve=D("100000"),
                         cash_per_open_lot=D("1000"), fee_per_close_lot=D("10"),
                         cash_bound_source=c.CashBoundSource(source=c.Source.SIMULATION,
                                                            evidence_id="SIM_CASH_BOUND", verified=True))
    intent = c.OrderIntent(intent_id=intent_id, source_key="SIM_SOURCE_" + intent_id, binding=binding, action=action,
                           volume=volume, prices=c.Prices(open=D("3500"), close=D("3500")),
                           close_policy=c.ClosePolicy.AUTO_YD_FIRST, order_type=order_type,
                           created_at=now, expires_at=now + timedelta(days=1))
    symbol = c.Symbol(exchange=exchange, instrument_id=binding.instrument_id)
    auth = c.Authorization(authorization_id="SIM_AUTHORIZATION", mode=c.ExecutionMode.SIMULATION,
                            account_key=binding.account_key, strategy_key=binding.strategy_key,
                            session_epoch=binding.session_epoch, symbols=(symbol,),
                            actions=tuple(c.AuthorizationAction), max_volume=100,
                            price_bounds=(c.PriceBound(symbol=symbol, lower=D("3000"), upper=D("4000")),),
                            expires_at=now + timedelta(days=1))
    readiness = c.Readiness(connection=c.ConnectionState.ROUNDTRIP, state=c.ReadinessState.EXECUTABLE,
                            can_plan=True, can_submit=True, can_cancel_owned=True, blockers=(),
                            mode=c.ExecutionMode.SIMULATION, coverage=coverage, evidence_id=proof.proof_id)
    return c.ExecutionContext(mode=c.ExecutionMode.SIMULATION, binding=binding, snapshot=sample, proof=proof,
                              policy=rules, authorization=auth, reservation_receipt=None,
                              owner_thread_id=get_ident(), current_thread_id=get_ident(),
                              clock=c.ClockReading(now=now, monotonic_ns=clock.monotonic_ns, domain_id="SIM_CLOCK"),
                              ledger_revision=revision, route_ownership=c.RouteOwnership(
                                  route="cancel", evidence_id="SIM_CURRENT_ROUTE", verified=True),
                              readiness=readiness, reconcile=c.ReconcileState(), intent=intent,
                              reservation_view=None)


class Simulation:
    """Offline driver using real contracts, planning, SQLite and reconciliation."""

    def __init__(self, path: str | Path, *, clock: FakeClock | None = None,
                 context: c.ExecutionContext | None = None):
        from .executor import Executor
        from .ledger import Ledger, LedgerError
        self.clock = clock or FakeClock()
        self.context = context or simulation_context(clock=self.clock)
        if self.context.mode is not c.ExecutionMode.SIMULATION or self.context.snapshot.source is not c.Source.SIMULATION:
            raise ValueError("Simulation only accepts explicit simulation evidence")
        self.ledger = Ledger(path, clock=self.clock)
        self.native = FakeNative(self.context.binding)
        self.executor = Executor(self.ledger, port=self.native, mode=c.ExecutionMode.SIMULATION,
                                 binding=self.context.binding, investor_handle=self.native.investor_handle,
                                 clock=self.clock)
        # Trusted initialization is absent-only: a virgin scope is registered
        # once, an existing durable scope with identical control claims is
        # adopted as-is, and any other durable state rejects construction with
        # zero writes. A constructor therefore can never roll back a control
        # change another connection committed; explicit control changes go
        # through update_controls and ordinary refreshes through CAS.
        prior = self.ledger.context(self.context.binding)
        if prior is None:
            # Atomic absent-only initialization: a scope another connection
            # created after this read rejects with CONTEXT_CONFLICT and zero
            # writes instead of overwriting the winner's controls.
            try:
                self.ledger.initialize_context(self.context)
            except LedgerError:
                self.ledger.close()
                raise
        elif (prior.mode, prior.binding, prior.policy, prior.authorization, prior.route_ownership) == (
                self.context.mode, self.context.binding, self.context.policy,
                self.context.authorization, self.context.route_ownership):
            self.context = prior
        else:
            self.ledger.close()
            raise NativeExecutionError("CONTEXT_CONFLICT")
        self.events: list[c.ExecutionEvent] = []
        self._sequence = 0
        self._orders: dict[str, c.OrderInventoryItem] = {}
        self._trades: dict[str, c.TradeInventoryItem] = {}
        self._positions = self.context.snapshot.positions

    def close(self):
        self.ledger.close()

    def prepare(self):
        prepared = self.executor.prepare(self.context.intent, self.context)
        self.context = prepared.context
        return prepared.plan

    def refresh_open(self, leg_id: str):
        prepared = self.executor.refresh_pending_open(leg_id, self.context)
        self.context = prepared.context
        return prepared.plan

    def submit(self, leg_id: str):
        record = self.ledger.get_leg(leg_id)
        return self.executor.submit_once(record.intent.intent_id, leg_id, record.plan.plan_id, self.context)

    def event(self, leg_id: str, *, kind: c.EventKind = c.EventKind.ORDER,
              status: str | None = "未成交", traded: int | None = 0, cancelled: int | None = 0,
              trade_id: str | None = None, volume: int | None = None,
              order_id: int | None = None, complete: bool = True):
        from .reconcile import CallbackContext, normalize_event
        self._sequence += 1
        record = self.ledger.get_leg(leg_id)
        leg, b = record.leg, record.intent.binding
        local_id = record.order_id if order_id is None else order_id
        offset = {c.Offset.OPEN: "0", c.Offset.CLOSE: "1", c.Offset.CLOSE_YESTERDAY: "1",
                  c.Offset.CLOSE_TODAY: "3"}[leg.offset]
        raw = {"OrderID": local_id, "OrderSysID": f"SIM_SYS_{local_id}", "FrontID": 1, "SessionID": 1,
               "Exchange": b.exchange, "InstrumentID": b.instrument_id, "Hedgeflag": b.hedge_flag,
               "Direction": "0" if leg.direction is c.Direction.BUY else "1", "Offset": offset,
               "Memo": leg_id, "TradingDay": b.trading_day.strftime("%Y%m%d"), "Price": str(leg.price)}
        if kind is c.EventKind.TRADE:
            raw.update(TradeID=trade_id, Volume=volume)
            raw.pop("FrontID")
            raw.pop("SessionID")
        elif complete:
            raw.update(Status=status, TotalVolume=leg.volume, TradedVolume=traded, CancelVolume=cancelled)
        event = normalize_event(raw, CallbackContext(event_id=f"SIM_EVENT_{self._sequence}", kind=kind,
                                received_at=self.clock(), session_epoch=b.session_epoch, source=c.Source.SIMULATION,
                                account_key=b.account_key, strategy_key=b.strategy_key, simulation_scope=True))
        self.events.append(event)
        self.ledger.ingest_event(event)
        if kind is c.EventKind.TRADE and trade_id is not None and volume is not None:
            item = c.TradeInventoryItem(account_key=b.account_key, trading_day=b.trading_day, exchange=b.exchange,
                        instrument_id=b.instrument_id, hedge_flag=b.hedge_flag, trade_id=trade_id,
                        order_sys_id=f"SIM_SYS_{local_id}", order_id=local_id, direction=leg.direction,
                        offset=leg.offset, price=leg.price, volume=volume)
            prior = self._trades.get(trade_id)
            if prior is None:
                self._trades[trade_id] = item
                self._apply_fill(leg, volume)
        elif kind is c.EventKind.ORDER and complete:
            self._orders[leg_id] = c.OrderInventoryItem(
                account_key=b.account_key, trading_day=b.trading_day, exchange=b.exchange,
                instrument_id=b.instrument_id, hedge_flag=b.hedge_flag, direction=leg.direction, offset=leg.offset,
                order_id=local_id, order_sys_id=f"SIM_SYS_{local_id}", total_volume=leg.volume,
                traded_volume=traded, cancel_volume=cancelled,
                active=status not in ("全部成交", "已撤销", "部成部撤", "投资经理驳回", "交易员驳回"))
        return event

    def _apply_fill(self, leg: c.Leg, volume: int):
        side = (c.Side.LONG if (leg.direction is c.Direction.BUY) == (leg.phase is c.Phase.OPEN) else c.Side.SHORT)
        positions = []
        for p in self._positions:
            if p.side is side:
                delta = volume if leg.phase is c.Phase.OPEN else -volume
                yd = p.YdPositionClose
                if leg.offset is c.Offset.CLOSE_YESTERDAY:
                    yd -= volume
                elif leg.offset is c.Offset.CLOSE:
                    yd = max(0, yd-volume)
                p = replace(p, Position=p.Position+delta, PositionClose=p.PositionClose+delta, YdPositionClose=yd)
            positions.append(p)
        # This is only the fake exchange's sampled state; register_context is
        # delayed until a distinct completed snapshot/barrier is published.
        self._positions = tuple(positions)

    def update_controls(self, context: c.ExecutionContext):
        """Explicit trusted control change; the assembler-only entry."""
        self.ledger.register_context(context)
        self.context = self.executor.refresh(context)

    def barrier(self):
        """Publish a new simulated full-account snapshot and explicit source watermarks."""
        self._sequence += 1
        self.clock.advance(D("0.001"))
        old = self.context
        ident = f"SIM_SNAPSHOT_{self._sequence}"
        now = self.clock()
        revision = self.ledger.check().revision
        timing = c.ComponentTiming(sampled_at=now, source_at=None, received_at=now, snapshot_id=ident,
                                   source=c.Source.SIMULATION, completeness=c.Completeness.COMPLETE)
        mark = c.SourceWatermark(source=c.Source.SIMULATION, stream_id="SIMULATION_STREAM", value=str(self._sequence), as_of=now)
        count = sum(item.active for item in self._orders.values())
        coverage = replace(old.snapshot.coverage, as_of=now, complete_through=mark,
                           completion_evidence_id="SIM_BARRIER_" + ident, own_active_count=count, account_active_count=count)
        proof = replace(old.proof, proof_id="SIM_PROOF_" + ident, as_of=now,
                        order_inventory=tuple(self._orders.values()), trade_inventory=tuple(self._trades.values()),
                        position_snapshot_id=ident, complete_through=mark,
                        covered_event_ids=tuple(e.event_id for e in self.events), covered_leg_ids=tuple(self._orders))
        snapshot = replace(old.snapshot, snapshot_id=ident, ledger_revision=revision, proof_id=proof.proof_id,
                           quote=replace(old.snapshot.quote, time=now), coverage=coverage,
                           positions=tuple(replace(p, timing=timing) for p in self._positions),
                           times=c.SnapshotTimes(sampled_at=now, sent_at=now, received_at=now,
                               quote=replace(timing, source_at=now), contract=timing, account=timing, positions=timing))
        ready = replace(old.readiness, state=c.ReadinessState.EXECUTABLE, can_submit=True, blockers=(),
                        coverage=coverage, evidence_id=proof.proof_id)
        context = replace(old, snapshot=snapshot, proof=proof, ledger_revision=revision,
                          clock=replace(old.clock, now=now, monotonic_ns=self.clock.monotonic_ns),
                          readiness=ready, reconcile=c.ReconcileState(), reservation_view=None)
        expected = self.executor.persisted_context(context)
        published = self.executor.latest_context(context, expected_context=expected)
        self.ledger.recover(proof)
        self.context = self.executor.refresh(published)
        return self.context

    def fill(self, leg_id: str):
        leg = self.ledger.get_leg(leg_id).leg
        self.event(leg_id, status="未成交")
        self.event(leg_id, kind=c.EventKind.TRADE, trade_id=f"SIM_TRADE_{self._sequence}", volume=leg.volume)
        self.event(leg_id, status="全部成交", traded=leg.volume)
        return self.barrier()


def readiness_report(context: c.ExecutionContext) -> Mapping[str, object]:
    """Presentation only; no inference from journal counts or simulation to native."""
    ready = context.readiness
    return MappingProxyType({
        "source": context.snapshot.source.value, "mode": ready.mode.value,
        "connection": ready.connection.value, "state": ready.state.value,
        "can_plan": ready.can_plan, "can_submit": ready.can_submit,
        "can_cancel": ready.can_cancel_owned, "coverage": ready.coverage.level.value,
        "own_active_count": ready.coverage.own_active_count,
        "account_active_count": ready.coverage.account_active_count,
        "blockers": ready.blockers, "engineering": "SIMULATION_ONLY",
        "platform": "PARTIAL_UNVERIFIED", "broker": "NOT_RUN",
        "native_full_account_reconciliation": c.NATIVE_FULL_ACCOUNT_RECONCILIATION,
        "native_submit_enabled": c.NATIVE_SUBMIT_ENABLED,
        "native_cancel_owned_enabled": c.NATIVE_CANCEL_OWNED_ENABLED,
    })


def run_demo(directory: str | Path) -> tuple[Mapping[str, object], ...]:
    """Run six fictitious actions over real SQLite journals, no live entry point."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    results = []
    for action in c.Action:
        long = 3 if action in (c.Action.CLOSE_LONG, c.Action.REVERSE_TO_SHORT) else 0
        short = 3 if action in (c.Action.CLOSE_SHORT, c.Action.REVERSE_TO_LONG) else 0
        clock = FakeClock()
        context = simulation_context(clock=clock, action=action, long=long, short=short, yesterday=2,
                                     intent_id="SIM_" + action.value)
        simulation = Simulation(directory / (action.value + ".sqlite"), clock=clock, context=context)
        try:
            proposed = simulation.prepare()
            for leg in proposed.legs:
                if leg.depends_on:
                    simulation.refresh_open(leg.leg_id)
                simulation.submit(leg.leg_id)
                simulation.fill(leg.leg_id)
            records = tuple(simulation.ledger.get_leg(leg.leg_id) for leg in proposed.legs)
            if not all(item.state is c.OrderState.FILLED and item.settled for item in records):
                raise RuntimeError("simulation did not settle every leg")
            simulation.context = replace(simulation.context, readiness=simulation.executor.readiness(simulation.context))
            requests = tuple({key: (value.account_key if isinstance(value, SimulatedInvestor) else value)
                              for key, value in request.items()} for request in simulation.native.sent_requests)
            results.append(MappingProxyType({"action": action.value, "source": "SIMULATION", "requests": requests,
                         "send_count": len(requests), "cancel_count": len(simulation.native.cancelled_ids),
                         "plan_hash": c.canonical_hash(proposed), "final_context": c.encode(simulation.context),
                         "final_positions": tuple((p.side.value, p.Position) for p in simulation.context.snapshot.positions),
                         "settled": all(item.settled for item in records),
                         "readiness": dict(readiness_report(simulation.context))}))
        finally:
            simulation.close()
    return tuple(results)
