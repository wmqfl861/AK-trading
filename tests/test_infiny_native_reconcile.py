"""NE1 TR: native raw-callback attribution, dedup, ordering, cancel and settlement.

All identities are SYNTHETIC offline fixtures; no native SDK, network or real
order is ever loaded. Native capability claims stay refused by production
registries; these tests inject an accepting SYNTHETIC registry explicitly.
"""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import sqlite3

import pytest

from ak_trading.infiny_execution import contracts as c
from ak_trading.infiny_execution import native_contracts as nc
from ak_trading.infiny_execution import native_evidence as ne
from ak_trading.infiny_execution.dispatch_journal import DispatchJournal
from ak_trading.infiny_execution.ledger import Ledger, LedgerError
from ak_trading.infiny_execution.reconcile import CallbackContext, normalize_event

NOW = datetime(2026, 9, 14, 1, 30, tzinfo=timezone.utc)
D = Decimal


class FakeClock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class AcceptingRegistry(ne.EvidenceRegistry):
    def applicable_deadline(self, permit_id, *, action, now):
        del permit_id, action
        return ne.EvidenceDeadlineResult(validation=c.ValidationResult(),
                                         deadline=now + timedelta(days=1))

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


def nfixture(*, name="n1", volume=2, position=0, available="100000", now=NOW,
             action=c.Action.OPEN_LONG, account="SIM_A", epoch="SIM_E", instrument="rb2610"):
    b = c.Binding(account_key=account, strategy_key="SIM_S", strategy_id=23, instance_id="SIM_I",
                  session_epoch=epoch, trading_day=now.date(), exchange="SHFE", instrument_id=instrument,
                  hedge_flag="1")
    timing = c.ComponentTiming(sampled_at=now, source_at=None, received_at=now, snapshot_id="snap-" + name,
                               source=c.Source.PYTHONGO, completeness=c.Completeness.COMPLETE)
    cov = c.Coverage(level=c.CoverageLevel.UNKNOWN, account_key=account, strategy_key="SIM_S",
                     session_epoch=epoch, trading_day=now.date(), source=c.Source.PYTHONGO, as_of=now,
                     complete_through=None, completion_evidence_id=None, own_active_count=None,
                     account_active_count=None)
    policy = c.RiskPolicy(policy_id="pol-" + name, valid_until=now + timedelta(days=1), max_leg_qty=5,
                          max_gross_position=100, max_inflight_legs=1, max_cash_reserve=D(available),
                          cash_per_open_lot=D(1000), fee_per_close_lot=D(10),
                          cash_bound_source=c.CashBoundSource(source=c.Source.SIMULATION,
                                                              evidence_id="cash-bound", verified=True))
    positions = tuple(c.PositionBucket(binding=b, side=side, timing=timing,
                                       Position=position if side is c.Side.LONG else 0,
                                       PositionClose=position if side is c.Side.LONG else 0,
                                       YdPositionClose=0, FrozenClosing=0, YdFrozenClosing=0) for side in c.Side)
    snapshot = c.ExecutionSnapshot(snapshot_id="snap-" + name, binding=b, source=c.Source.PYTHONGO,
                                   times=c.SnapshotTimes(sampled_at=now, sent_at=now, received_at=now,
                                                         quote=replace(timing, source_at=now), contract=timing,
                                                         account=timing, positions=timing),
                                   quote=c.Quote(binding=b, time=now, last=D(3500), bid1=D(3500), ask1=D(3501),
                                                 bid_volume1=10, ask_volume1=10, upper_limit=D(4000),
                                                 lower_limit=D(3000), market_status=c.MarketStatus.CONTINUOUS),
                                   contract=c.Contract(exchange="SHFE", instrument_id=instrument,
                                                       asset_class=c.AssetClass.FUTURE, expiry=date(2026, 10, 15),
                                                       price_tick=D(1), multiplier=D(10), min_limit_order_volume=1,
                                                       max_limit_order_volume=5),
                                   account=c.Account(binding=b, available=D(available), currency="CNY"),
                                   positions=positions, coverage=cov, proof_id=None, ledger_revision=0)
    intent = c.OrderIntent(intent_id="intent-" + name, source_key="decision-" + name, binding=b, action=action,
                           volume=volume, prices=c.Prices(open=D(3500), close=D(3500)),
                           close_policy=c.ClosePolicy.AUTO_YD_FIRST, order_type=c.OrderType.GFD,
                           created_at=now, expires_at=now + timedelta(days=1))
    auth = c.Authorization(authorization_id="auth-" + name, mode=c.ExecutionMode.SIMULATION, account_key=account,
                           strategy_key="SIM_S", session_epoch=epoch,
                           symbols=(c.Symbol(exchange="SHFE", instrument_id=instrument),),
                           actions=tuple(c.AuthorizationAction), max_volume=100,
                           price_bounds=(c.PriceBound(symbol=c.Symbol(exchange="SHFE", instrument_id=instrument),
                                                      lower=D(3000), upper=D(4000)),),
                           expires_at=now + timedelta(days=1))
    ready = c.Readiness(connection=c.ConnectionState.ROUNDTRIP, state=c.ReadinessState.PLAN_READY,
                        can_plan=True, can_submit=False, can_cancel_owned=True, blockers=(),
                        mode=c.ExecutionMode.SIMULATION, coverage=cov, evidence_id="ready-" + name)
    return c.ExecutionContext(mode=c.ExecutionMode.SIMULATION, binding=b, snapshot=snapshot, proof=None,
                              policy=policy, authorization=auth, reservation_receipt=None,
                              owner_thread_id=17, current_thread_id=17,
                              clock=c.ClockReading(now=now, monotonic_ns=1, domain_id="clock"),
                              ledger_revision=0,
                              route_ownership=c.RouteOwnership(route="cancel", evidence_id="sim-route", verified=True),
                              readiness=ready, reconcile=c.ReconcileState(), intent=intent)


def nplan(ctx):
    intent = ctx.intent
    opening = intent.action in (c.Action.OPEN_LONG, c.Action.OPEN_SHORT)
    legs = []
    if not opening:
        legs.append(c.Leg(leg_id=f"{intent.intent_id}:CLOSE:{'YD' if ctx.snapshot.positions[0].YdPositionClose else 'TD'}:0",
                          index=0, phase=c.Phase.CLOSE, depends_on=(),
                          direction=c.Direction.SELL, offset=c.Offset.CLOSE_TODAY, volume=intent.volume,
                          price=D(3500), position_bucket=c.Bucket.TD, cash_reserve=D(10) * intent.volume))
    if opening or intent.action in (c.Action.REVERSE_TO_LONG, c.Action.REVERSE_TO_SHORT):
        legs.append(c.Leg(leg_id=f"{intent.intent_id}:OPEN:-:0", index=len(legs), phase=c.Phase.OPEN,
                          depends_on=tuple(x.leg_id for x in legs), direction=c.Direction.BUY, offset=c.Offset.OPEN,
                          volume=intent.volume, price=D(3500), position_bucket=None,
                          cash_reserve=D(1000) * intent.volume))
    import hashlib
    plan_id = hashlib.sha256((c.canonical_hash(intent) + ctx.snapshot.snapshot_id + "0").encode()).hexdigest()
    return c.Plan(plan_id=plan_id, intent_hash=c.canonical_hash(intent), snapshot_id=ctx.snapshot.snapshot_id,
                  ledger_revision=ctx.ledger_revision, policy_id=ctx.policy.policy_id,
                  expires_at=ctx.clock.now + timedelta(seconds=3), legs=tuple(legs), blockers=())


def nsync(db, ctx, receipt=None):
    revision = db.check().revision
    ctx = replace(ctx, snapshot=replace(ctx.snapshot, ledger_revision=revision), ledger_revision=revision,
                  reservation_view=None, reservation_receipt=receipt or ctx.reservation_receipt)
    db.register_context(ctx)
    ctx = replace(ctx, reservation_view=db.view(ctx.binding.account_key, snapshot_id=ctx.snapshot.snapshot_id))
    db.register_context(ctx)
    return ctx


def nreserve(db, ctx):
    db.register(ctx.intent)
    ctx = nsync(db, ctx)
    plan = nplan(ctx)
    receipt = db.reserve(plan, ctx.ledger_revision)
    return nsync(db, ctx, receipt), plan


def route_evidence(ctx, *, route_id="route-1", front=11, session=22):
    return ne.NativeRouteEvidence(evidence_id=route_id, framework_sha256="f" * 64, sdk_sha256="5" * 64,
                                  binding=ctx.binding,
                                  callback_kinds=(ne.CallbackKind.ON_ORDER, ne.CallbackKind.ON_TRADE,
                                                  ne.CallbackKind.ON_CANCEL),
                                  account_scope_basis="terminal export account column verified",
                                  strategy_scope_basis="strategy routing verified",
                                  local_alias_scope="per-session local ids", front_id=front, session_id=session,
                                  memo_limit=32, memo_roundtrip_verified=True, validated_at=ctx.clock.now,
                                  expires_at=ctx.clock.now + timedelta(days=1), origin=ne.Origin.SYNTHETIC)


def pilot_permit(ctx, revision):
    return ne.TestEvidenceFactory(purpose="SYNTHETIC_TEST_ONLY").issue_permit(
        permit_id="permit-1", authorization_id="auth-pilot", binding=ctx.binding, control_revision=revision,
        baseline_evidence_id="baseline-1", route_evidence_id="route-1", cash_bound_evidence_id="cash-1",
        source_key="pilot-1", actions=(c.AuthorizationAction.OPEN_LONG, c.AuthorizationAction.CLOSE_LONG,
                                       c.AuthorizationAction.CANCEL_OWNED), max_volume=10,
        expires_at=ctx.clock.now + timedelta(hours=1), params_sha256="0" * 64)


def ncontrol(ctx, revision, *, permit_id=None):
    return nc.ControlState(control_revision=revision, binding=ctx.binding,
                           mode=c.ExecutionMode.NATIVE if permit_id else c.ExecutionMode.SIMULATION,
                           policy_id=ctx.policy.policy_id,
                           authorization_id=ctx.authorization.authorization_id, native_permit_id=permit_id,
                           params_sha256="0" * 64, generation="gen-1", updated_at=ctx.clock.now, disabled_reason=None)


def noffer(ctx, plan, leg, revision, *, offer_id="offer-1"):
    return nc.ExecutionOffer(offer_id=offer_id, intent_id=ctx.intent.intent_id, leg_id=leg.leg_id,
                             plan_id=plan.plan_id, intent_hash=c.canonical_hash(ctx.intent),
                             leg_hash=c.canonical_hash(leg), binding=ctx.binding,
                             snapshot_id=ctx.snapshot.snapshot_id, policy_id=ctx.policy.policy_id,
                             params_sha256="0" * 64, control_revision=revision,
                             expires_at=ctx.clock.now + timedelta(milliseconds=1400))


def nreceipt(command, ctx, *, stage, result_kind=None, result_value=None, error_code=None, receipt_id="r-1"):
    return nc.DispatchReceipt(receipt_id=receipt_id, command_id=command.command_id, attempt_id=command.attempt_id,
                              binding=command.binding, generation="gen-1", stage=stage, local_sequence=1,
                              recorded_at=ctx.clock.now, result_kind=result_kind, result_value=result_value,
                              error_code=error_code, request_sha256="1" * 64, previous_receipt_sha256=None)


def npilot_start(tmp_path, name, *, registry, clock, **fx):
    db = Ledger(tmp_path / (name + ".db"), clock=clock)
    ctx, plan = nreserve(db, nfixture(now=clock.now, **fx))
    db.register_native_route(route_evidence(ctx), evidence_registry=registry)
    permit = pilot_permit(ctx, 1)
    db.register_control_update(ncontrol(ctx, 1, permit_id=permit.permit_id), permit=permit, evidence_registry=registry)
    ctx = nsync(db, ctx, ctx.reservation_receipt)
    journal = DispatchJournal(db, evidence_registry=registry)
    offer = noffer(ctx, plan, plan.legs[0], 1)
    journal.record_offer(offer, ctx, first_issued_at=clock.now, kind=nc.CommandKind.SUBMIT,
                         expected_control_revision=1, expected_context=db.context(ctx.binding))
    command = journal.begin_dispatch(offer, ctx, expected_control_revision=1)
    journal.record_receipt(nreceipt(command, ctx, stage=nc.ReceiptStage.RETURNED,
                                    result_kind=nc.ResultKind.LOCAL_ACK, result_value=7, receipt_id="r-ack"))
    return db, clock, ctx, plan, journal


def nevent(ctx, leg, *, event_id="e-1", kind=c.EventKind.ORDER, status=c.OrderState.WORKING, traded=0,
           cancelled=0, trade_id=None, volume=None, route_id="route-1", memo="default", account=None,
           epoch=None, day=None, instrument=None, front=None, session=None, sys_id="0007", now=None):
    raw = {"Exchange": "SHFE", "InstrumentID": instrument or ctx.binding.instrument_id, "Hedgeflag": "1",
           "OrderID": 7, "OrderSysID": sys_id, "Memo": leg.leg_id if memo == "default" else memo,
           "TradingDay": (day or ctx.binding.trading_day).isoformat(), "Direction": leg.direction, "Offset": leg.offset,
           "Status": status, "Price": "3500", "TotalVolume": leg.volume if kind is c.EventKind.ORDER else None,
           "TradedVolume": traded if kind is c.EventKind.ORDER else None,
           "CancelVolume": cancelled if kind is c.EventKind.ORDER else None, "TradeID": trade_id, "Volume": volume,
           "FrontID": front, "SessionID": session}
    callback = CallbackContext(event_id=event_id, kind=kind, received_at=now or ctx.clock.now,
                               session_epoch=epoch or ctx.binding.session_epoch, source=c.Source.PYTHONGO,
                               account_key=account or ctx.binding.account_key,
                               native_route_evidence_id=route_id)
    return normalize_event(raw, callback)


def test_H19_native_route_attribution_accepts_only_registered_matching_routes(tmp_path):
    registry = AcceptingRegistry()
    clock = FakeClock()
    db, clock, ctx, plan, journal = npilot_start(tmp_path, "h19", registry=registry, clock=clock)
    leg = plan.legs[0]
    assert db.ingest_event(nevent(ctx, leg)).code is c.ResultCode.OK
    record = db.get_leg(leg.leg_id)
    assert record.ownership_verified and record.state is c.OrderState.WORKING
    forged = nevent(ctx, leg, event_id="forged", route_id="route-unknown")
    assert db.ingest_event(forged).reason == "OWNERSHIP_UNVERIFIED"
    foreign = nevent(ctx, leg, event_id="foreign", account="SIM_B")
    assert db.ingest_event(foreign).code is c.ResultCode.QUARANTINED
    memo_lie = nevent(ctx, leg, event_id="memo-lie", memo="other:OPEN:-:0")
    assert db.ingest_event(memo_lie).reason == "MEMO_CONFLICT"
    front_lie = nevent(ctx, leg, event_id="front-lie", front=99, session=22)
    assert db.ingest_event(front_lie).reason == "ALIAS_CONFLICT"
    assert db.get_leg(leg.leg_id).risk_filled == 0


def test_H20_native_dedup_max_and_terminal_monotonic(tmp_path):
    registry = AcceptingRegistry()
    clock = FakeClock()
    db, clock, ctx, plan, journal = npilot_start(tmp_path, "h20", registry=registry, clock=clock, volume=4)
    leg = plan.legs[0]
    for item in ("p1", "f4", "late0", "p1b", "f4b"):
        pass
    db.ingest_event(nevent(ctx, leg, event_id="p1", traded=1, status=c.OrderState.PARTIAL))
    db.ingest_event(nevent(ctx, leg, event_id="f4", traded=4, status=c.OrderState.FILLED))
    db.ingest_event(nevent(ctx, leg, event_id="late0", traded=0, status=c.OrderState.SUBMITTED_LOCAL))
    db.ingest_event(nevent(ctx, leg, event_id="p1-dup", traded=1, status=c.OrderState.PARTIAL))
    for trade_id, qty, eid in (("T1", 1, "t1"), ("T2", 3, "t2"), ("T1", 1, "t1-dup"), ("T2", 3, "t2-dup")):
        item = nevent(ctx, leg, event_id=eid, kind=c.EventKind.TRADE, trade_id=trade_id, volume=qty)
        db.ingest_event(item)
        db.ingest_event(item)
    record = db.get_leg(leg.leg_id)
    assert (record.reported_filled, record.trade_filled, record.risk_filled) == (4, 4, 4)
    assert record.state is c.OrderState.FILLED and record.trade_gap == 0
    conflicting = nevent(ctx, leg, event_id="t2-alt", kind=c.EventKind.TRADE, trade_id="T2", volume=2)
    assert db.ingest_event(conflicting).reason == "TRADE_CONFLICT"


def test_H21_native_trade_before_returned_is_isolated_then_associated_once(tmp_path):
    registry = AcceptingRegistry()
    clock = FakeClock()
    db = Ledger(tmp_path / "h21.db", clock=clock)
    ctx, plan = nreserve(db, nfixture(now=clock.now, volume=2))
    db.register_native_route(route_evidence(ctx), evidence_registry=registry)
    permit = pilot_permit(ctx, 1)
    db.register_control_update(ncontrol(ctx, 1, permit_id=permit.permit_id), permit=permit, evidence_registry=registry)
    ctx = nsync(db, ctx, ctx.reservation_receipt)
    journal = DispatchJournal(db, evidence_registry=registry)
    offer = noffer(ctx, plan, plan.legs[0], 1)
    journal.record_offer(offer, ctx, first_issued_at=clock.now, kind=nc.CommandKind.SUBMIT,
                         expected_control_revision=1, expected_context=db.context(ctx.binding))
    command = journal.begin_dispatch(offer, ctx, expected_control_revision=1)
    journal.record_receipt(nreceipt(command, ctx, stage=nc.ReceiptStage.CONSUMED,
                                    result_kind=nc.ResultKind.EXCEPTION, error_code="OUTCOME_PENDING"))
    leg = plan.legs[0]
    trade = nevent(ctx, leg, event_id="t-early", kind=c.EventKind.TRADE, trade_id="NT1", volume=2)
    assert db.ingest_event(trade).reason == "ALIAS_PENDING"
    assert db.get_leg(leg.leg_id).risk_filled == 0
    journal.record_receipt(nreceipt(command, ctx, stage=nc.ReceiptStage.RETURNED,
                                    result_kind=nc.ResultKind.LOCAL_ACK, result_value=7, receipt_id="r-ret"))
    record = db.get_leg(leg.leg_id)
    assert record.state is c.OrderState.FILLED and record.trade_filled == 2 and record.risk_filled == 2
    with sqlite3.connect(db.path) as con:
        assert con.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["account", "epoch", "day", "instrument"])
def test_H22_local_id_reuse_never_merges_scopes(tmp_path, change):
    registry = AcceptingRegistry()
    clock = FakeClock()
    db, clock, ctx, plan, journal = npilot_start(tmp_path, "h22-" + change, registry=registry, clock=clock)
    leg = plan.legs[0]
    kwargs = {"account": "SIM_B"} if change == "account" else {"epoch": "OLD_EPOCH"} if change == "epoch" else \
             {"day": date(2026, 9, 15)} if change == "day" else {"instrument": "cu2610"}
    assert db.ingest_event(nevent(ctx, leg, event_id="reuse", **kwargs)).code is c.ResultCode.QUARANTINED
    record = db.get_leg(leg.leg_id)
    assert record.risk_filled == 0 and not record.ownership_verified
    with pytest.raises(LedgerError):
        fresh = nsync(db, ctx, ctx.reservation_receipt)
        db.request_cancel("cx", ctx.intent.intent_id, leg.leg_id, fresh)


def test_H25_native_settlement_releases_only_proven_scope(tmp_path):
    registry = AcceptingRegistry()
    clock = FakeClock()
    db, clock, ctx, plan, journal = npilot_start(tmp_path, "h25", registry=registry, clock=clock,
                                                 volume=4, position=4, action=c.Action.CLOSE_LONG)
    leg = plan.legs[0]
    db.ingest_event(nevent(ctx, leg, event_id="p1", traded=1, status=c.OrderState.PARTIAL))
    db.ingest_event(nevent(ctx, leg, event_id="t1", kind=c.EventKind.TRADE, trade_id="NT1", volume=1))
    ctx = nsync(db, ctx, ctx.reservation_receipt)
    db.request_cancel("cancel-h25", ctx.intent.intent_id, leg.leg_id, ctx)
    db.ingest_event(nevent(ctx, leg, event_id="final", traded=1, cancelled=3,
                           status=c.OrderState.PARTIAL_CANCELLED))
    assert not db.get_leg(leg.leg_id).settled
    record = db.get_leg(leg.leg_id)
    post = nfixture(name="post", now=clock.now, volume=4, position=3, action=c.Action.CLOSE_LONG)
    post = replace(post, intent=ctx.intent, policy=ctx.policy, authorization=ctx.authorization)
    post = nsync(db, post, ctx.reservation_receipt)
    order = c.OrderInventoryItem(account_key="SIM_A", trading_day=ctx.binding.trading_day, exchange="SHFE",
                                 instrument_id="rb2610", hedge_flag="1", direction=leg.direction,
                                 offset=leg.offset, order_id=7, order_sys_id="0007", total_volume=4,
                                 traded_volume=1, cancel_volume=3, active=False)
    trade = c.TradeInventoryItem(account_key="SIM_A", trading_day=ctx.binding.trading_day, exchange="SHFE",
                                 instrument_id="rb2610", hedge_flag="1", trade_id="NT1", order_id=7,
                                 order_sys_id="0007", direction=leg.direction, offset=leg.offset,
                                 price=D(3500), volume=1)
    evidence = ne.NativeSettlementEvidence(
        evidence_id="settle-1", baseline_evidence_id="baseline-1", binding=ctx.binding, as_of=clock.now,
        artifact_sha256="a" * 64, covered_leg_ids=(leg.leg_id,), covered_event_ids=("p1", "t1", "final"),
        order_inventory=(order,), trade_inventory=(trade,), position_snapshot_id=post.snapshot.snapshot_id,
        completion_basis="terminal export for this order scope", isolation_evidence_id="iso-1",
        origin=ne.Origin.SYNTHETIC)
    result = db.absorb_settlement(evidence, evidence_registry=registry)
    settled = db.get_leg(leg.leg_id)
    assert settled.settled and settled.cancel_state is c.CancelState.CONFIRMED
    assert db.view("SIM_A").reservations[0].state is c.ReservationState.RELEASED
    assert result.unresolved_leg_ids == ()
    # An unchanged position snapshot proves nothing: a second leg stays unsettled.
    stale = replace(evidence, evidence_id="settle-2", covered_leg_ids=(leg.leg_id,), order_inventory=(replace(order, active=True),))
    db.absorb_settlement(stale, evidence_registry=registry)
    assert db.get_leg(leg.leg_id).settled  # already settled stays; scope did not widen
