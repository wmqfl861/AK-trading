"""Offline contract tests: fictional identities, no native/client configuration."""
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from ak_trading.infiny_execution import contracts as c


NOW = datetime(2026, 9, 14, 1, 30, tzinfo=timezone.utc)


def binding(**changes):
    values = dict(account_key="SIM_ACCOUNT_A", strategy_key="SIM_STRATEGY_S",
                  strategy_id=23, instance_id="SIM_INSTANCE_I", session_epoch="SIM_EPOCH_E",
                  trading_day=date(2026, 9, 14), exchange="SHFE",
                  instrument_id="SIM_RB2610", hedge_flag="1")
    return c.Binding(**(values | changes))


def intent(**changes):
    values = dict(intent_id="SIM_INTENT_1", source_key="SIM_DECISION_1", binding=binding(),
                  action=c.Action.OPEN_LONG, volume=2,
                  prices=c.Prices(open=Decimal("3500.00"), close=None),
                  close_policy=c.ClosePolicy.AUTO_YD_FIRST, order_type=c.OrderType.GFD,
                  created_at=NOW, expires_at=NOW + timedelta(days=1))
    return c.OrderIntent(**(values | changes))


def test_intent_roundtrip_is_immutable_versioned_and_decimal_canonical():
    original = intent()
    wire = c.encode(original)
    assert c.decode(wire, c.OrderIntent) == original
    packet = json.loads(wire)
    assert packet["schema_version"] == "infiny_execution_v1"
    assert packet["type"] == "OrderIntent"
    assert packet["data"]["prices"] == {"open": "3500", "close": None}
    assert packet["data"]["created_at"] == "2026-09-14T01:30:00.000000Z"
    assert c.canonical_hash(original) == c.canonical_hash(intent(prices=c.Prices(open=Decimal("3.5E3"), close=None)))
    assert c.canonical_hash(original) != c.canonical_hash(intent(volume=3))
    with pytest.raises(FrozenInstanceError):
        original.volume = 3
    with pytest.raises(FrozenInstanceError):
        original.binding.account_key = "SIM_B"


@pytest.mark.parametrize("volume", [True, False, 0, -1, 1.5, "2"])
def test_intent_rejects_invalid_quantity_without_coercion(volume):
    with pytest.raises(c.ContractError):
        intent(volume=volume)


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity"), Decimal("0"), Decimal("-1"), 3500.0, True])
def test_prices_reject_nonfinite_nonpositive_and_non_decimal_values(value):
    with pytest.raises(c.ContractError):
        c.Prices(open=value, close=None)


def test_datetime_normalization_and_expiry_validation():
    china = timezone(timedelta(hours=8))
    assert intent(created_at=NOW.astimezone(china)).created_at == NOW
    with pytest.raises(c.ContractError):
        intent(created_at=NOW.replace(tzinfo=None))
    with pytest.raises(c.ContractError):
        intent(expires_at=NOW)
    with pytest.raises(c.ContractError):
        binding(strategy_id=True)
    with pytest.raises(c.ContractError):
        binding(account_key=" ")


@pytest.mark.parametrize("action,prices", [
    ("OPEN_LONG", ("3500", None)), ("OPEN_SHORT", ("3500", None)),
    ("CLOSE_LONG", (None, "3500")), ("CLOSE_SHORT", (None, "3500")),
    ("REVERSE_TO_LONG", ("3500", "3501")), ("REVERSE_TO_SHORT", ("3500", "3501")),
])
def test_six_actions_require_their_own_price_fields(action, prices):
    prices = c.Prices(open=Decimal(prices[0]) if prices[0] else None,
                      close=Decimal(prices[1]) if prices[1] else None)
    assert intent(action=c.Action(action), prices=prices).action.value == action
    with pytest.raises(c.ContractError):
        intent(action=c.Action(action), prices=c.Prices(open=None, close=None))


@pytest.mark.parametrize("change", [
    lambda p: p.update(schema_version="infiny_execution_v2"),
    lambda p: p.update(type="Binding"),
    lambda p: p["data"].update(schema_version="infiny_execution_v2"),
    lambda p: p["data"].update(enable=True),
    lambda p: p["data"].pop("source_key"),
    lambda p: p["data"].update(market=True),
    lambda p: p["data"].update(volume=True),
    lambda p: p["data"].update(order_type="FOK"),
    lambda p: p["data"]["prices"].update(open=3500.0),
    lambda p: p["data"]["prices"].update(open="NaN"),
    lambda p: p["data"].update(created_at="2026-09-14T01:30:00"),
    lambda p: p["data"]["binding"].update(trading_day="20260914"),
])
def test_wire_validation_rejects_version_shape_and_type_confusion(change):
    packet = json.loads(c.encode(intent()))
    change(packet)
    with pytest.raises(c.ContractError):
        c.decode(json.dumps(packet), c.OrderIntent)


def test_decoder_rejects_duplicate_keys_invalid_json_and_wrong_root():
    for wire in ['{"schema_version": "a", "schema_version": "b"}', '[1]', 'NaN', '{', b'\xff']:
        with pytest.raises(c.ContractError):
            c.decode(wire, c.OrderIntent)


def timing(**changes):
    values = dict(sampled_at=NOW, source_at=None, received_at=NOW,
                  snapshot_id="SIM_SNAPSHOT_1", source=c.Source.SIMULATION,
                  completeness=c.Completeness.COMPLETE)
    return c.ComponentTiming(**(values | changes))


def watermark(**changes):
    return c.SourceWatermark(**(dict(source=c.Source.SIMULATION, stream_id="SIM_STREAM",
                                    value="17", as_of=NOW) | changes))


def coverage(**changes):
    values = dict(level=c.CoverageLevel.FULL_ACCOUNT, account_key="SIM_ACCOUNT_A",
                  strategy_key=None, session_epoch="SIM_EPOCH_E", trading_day=NOW.date(),
                  source=c.Source.SIMULATION, as_of=NOW, complete_through=watermark(),
                  completion_evidence_id="SIM_BARRIER_1", own_active_count=0,
                  account_active_count=0)
    return c.Coverage(**(values | changes))


def proof(**changes):
    values = dict(proof_id="SIM_PROOF_1", source=c.Source.SIMULATION,
                  account_key="SIM_ACCOUNT_A", trading_day=NOW.date(), session_epoch="SIM_EPOCH_E",
                  as_of=NOW, order_inventory=(), trade_inventory=(),
                  position_snapshot_id="SIM_SNAPSHOT_1", complete_through=watermark(),
                  exclusive_writer_verified=True, verified=True)
    return c.ReconciliationProof(**(values | changes))


def bucket(side=None, **changes):
    values = dict(binding=binding(), side=side or c.Side.LONG, timing=timing(),
                  Position=0, PositionClose=0, FrozenClosing=0,
                  YdPositionClose=0, YdFrozenClosing=0)
    return c.PositionBucket(**(values | changes))


def snapshot(**changes):
    values = dict(snapshot_id="SIM_SNAPSHOT_1", binding=binding(), source=c.Source.SIMULATION,
                  times=c.SnapshotTimes(sampled_at=NOW, sent_at=NOW, received_at=NOW,
                                        quote=timing(source_at=NOW), contract=timing(),
                                        account=timing(), positions=timing()),
                  quote=c.Quote(binding=binding(), time=NOW, last=Decimal("3500"),
                                bid1=Decimal("3500"), ask1=Decimal("3501"),
                                bid_volume1=10, ask_volume1=10, upper_limit=Decimal("4000"),
                                lower_limit=Decimal("3000"), market_status=c.MarketStatus.CONTINUOUS),
                  contract=c.Contract(exchange="SHFE", instrument_id="SIM_RB2610",
                                      asset_class=c.AssetClass.FUTURE, expiry=date(2026, 10, 15),
                                      price_tick=Decimal("1"), multiplier=Decimal("10"),
                                      min_limit_order_volume=1, max_limit_order_volume=5,
                                      additional_volume_limits=()),
                  account=c.Account(binding=binding(), available=Decimal("100000"), currency="CNY"),
                  positions=(bucket(), bucket(c.Side.SHORT)), coverage=coverage(),
                  proof_id="SIM_PROOF_1", ledger_revision=0)
    return c.ExecutionSnapshot(**(values | changes))


def test_snapshot_preserves_complete_nesting_and_explicit_unknowns():
    sample = snapshot()
    assert c.decode(c.encode(sample), c.ExecutionSnapshot) == sample
    assert sample.account.available == Decimal("100000")
    assert sample.positions[0].FrozenClosing == 0
    unknown = replace(sample, account=replace(sample.account, available=None), positions=())
    assert c.decode(c.encode(unknown), c.ExecutionSnapshot).account.available is None
    assert unknown.positions == ()
    with pytest.raises(c.ContractError):
        replace(sample, positions=[bucket()])
    with pytest.raises(c.ContractError):
        replace(sample, positions=(bucket(), bucket()))


@pytest.mark.parametrize("changes", [
    {"Position": -1}, {"Position": True},
    {"Position": 2, "PositionClose": 3},
    {"Position": 10, "PositionClose": 10, "YdPositionClose": 2, "YdFrozenClosing": 3},
    {"Position": 10, "PositionClose": 10, "FrozenClosing": 1, "YdFrozenClosing": 2},
    {"Position": 10, "PositionClose": 10, "FrozenClosing": 6, "YdPositionClose": 8},
])
def test_position_hierarchy_is_not_clamped(changes):
    with pytest.raises(c.ContractError):
        bucket(**changes)


def test_unknown_position_fields_are_not_invented():
    item = bucket(Position=None, PositionClose=None, FrozenClosing=None,
                  YdPositionClose=None, YdFrozenClosing=None)
    assert c.decode(c.encode(item), c.PositionBucket) == item
    assert item.Position is None


@pytest.mark.parametrize("mutation", [
    lambda s: replace(s, source=c.Source.PYTHONGO),
    lambda s: replace(s, quote=replace(s.quote, binding=binding(account_key="SIM_OTHER"))),
    lambda s: replace(s, account=replace(s.account, binding=binding(session_epoch="SIM_OLD"))),
    lambda s: replace(s, contract=replace(s.contract, instrument_id="SIM_OTHER")),
    lambda s: replace(s, positions=(bucket(binding=binding(hedge_flag="2")),)),
    lambda s: replace(s, coverage=coverage(trading_day=date(2026, 9, 15))),
    lambda s: replace(s, times=replace(s.times, quote=timing(source_at=NOW + timedelta(seconds=1)))),
])
def test_snapshot_rejects_cross_identity_source_and_quote_time_conflicts(mutation):
    with pytest.raises(c.ContractError):
        mutation(snapshot())


def test_native_proof_cannot_claim_complete_barrier_or_verified():
    assert c.decode(c.encode(proof()), c.ReconciliationProof) == proof()
    for changes in [dict(source=c.Source.PYTHONGO), dict(complete_through=None),
                    dict(order_inventory=None), dict(position_snapshot_id=None),
                    dict(complete_through=watermark(source=c.Source.PYTHONGO))]:
        with pytest.raises(c.ContractError):
            proof(**changes)
    native = proof(source=c.Source.PYTHONGO, complete_through=None, verified=False,
                   exclusive_writer_verified=False, order_inventory=None, trade_inventory=None,
                   position_snapshot_id=None)
    assert native.verified is False and native.complete_through is None
    packet = json.loads(c.encode(native))
    packet["data"]["verified"] = True
    with pytest.raises(c.ContractError):
        c.decode(json.dumps(packet), c.ReconciliationProof)


def test_own_zero_does_not_invent_full_account_coverage():
    own = coverage(level=c.CoverageLevel.OWN_JOURNAL, strategy_key="SIM_STRATEGY_S",
                   complete_through=None, completion_evidence_id=None, account_active_count=None)
    assert own.own_active_count == 0 and own.account_active_count is None
    with pytest.raises(c.ContractError):
        replace(own, account_active_count=0)
    with pytest.raises(c.ContractError):
        coverage(source=c.Source.PYTHONGO)


@pytest.mark.parametrize("component,ttl", [("quote", 3), ("account", 5), ("positions", 5), ("contract", 3600)])
def test_component_time_boundaries_missing_source_and_future_skew(component, ttl):
    old = NOW - timedelta(seconds=ttl)
    item = timing(sampled_at=old, source_at=old if component == "quote" else None, received_at=old)
    assert c.validate_timing(item, NOW, Decimal(ttl), require_source=component == "quote").ok
    assert not c.validate_timing(item, NOW + timedelta(milliseconds=1), Decimal(ttl), require_source=component == "quote").ok
    assert c.validate_timing(timing(sampled_at=NOW + timedelta(seconds=2)), NOW, Decimal(ttl)).ok
    assert not c.validate_timing(timing(sampled_at=NOW + timedelta(seconds=2, milliseconds=1)), NOW, Decimal(ttl)).ok
    assert not c.validate_timing(timing(sampled_at=None), NOW, Decimal(ttl)).ok
    assert not c.validate_timing(timing(), NOW, Decimal(ttl), require_source=True).ok


def test_snapshot_timing_uses_the_callers_policy_not_the_default():
    future = NOW + timedelta(seconds=3)
    stamp = timing(sampled_at=future, source_at=future, received_at=future)
    base = snapshot()
    skewed = replace(base, times=replace(base.times, quote=stamp, sampled_at=future, received_at=future),
                     quote=replace(base.quote, time=future))
    assert c.validate_snapshot_timing(skewed, NOW, c.TimingPolicy(future_skew=Decimal("4"))).ok
    default_result = c.validate_snapshot_timing(skewed, NOW)
    assert not default_result.ok
    assert "QUOTE_FUTURE_SOURCE_AT" in default_result.blockers


def risk_policy(**changes):
    values = dict(policy_id="SIM_POLICY_1", valid_until=NOW + timedelta(days=1),
                  max_leg_qty=5, max_gross_position=100, max_inflight_legs=1,
                  max_cash_reserve=Decimal("100000"), cash_per_open_lot=Decimal("1000"),
                  fee_per_close_lot=Decimal("10"),
                  cash_bound_source=c.CashBoundSource(source=c.Source.SIMULATION,
                                                     evidence_id="SIM_CASH_BOUND_1", verified=True),
                  timing= c.TimingPolicy())
    return c.RiskPolicy(**(values | changes))


def plan_value(**changes):
    leg = c.Leg(leg_id="SIM_LEG_1", index=0, phase=c.Phase.OPEN, depends_on=(),
                direction=c.Direction.BUY, offset=c.Offset.OPEN, volume=2,
                price=Decimal("3500"), position_bucket=None, cash_reserve=Decimal("2000"))
    values = dict(plan_id="0" * 64, intent_hash=c.canonical_hash(intent()),
                  snapshot_id="SIM_SNAPSHOT_1", ledger_revision=0, policy_id="SIM_POLICY_1",
                  expires_at=NOW + timedelta(seconds=3), legs=(leg,), blockers=())
    return c.Plan(**(values | changes))


def reservation_view(**changes):
    values = dict(account_key="SIM_ACCOUNT_A", snapshot_id="SIM_SNAPSHOT_1", ledger_revision=0,
                  reservations=(), cash_reserved=Decimal("0"), gross_position=0,
                  inflight_legs=0, reconcile=c.ReconcileState())
    return c.ReservationView(**(values | changes))


def readiness(**changes):
    values = dict(connection=c.ConnectionState.ROUNDTRIP, state=c.ReadinessState.EXECUTABLE,
                  can_plan=True, can_submit=True, can_cancel_owned=False, blockers=(),
                  mode=c.ExecutionMode.SIMULATION, coverage=coverage(), evidence_id="SIM_READY_1")
    return c.Readiness(**(values | changes))


def authorization(**changes):
    values = dict(authorization_id="SIM_AUTH_1", mode=c.ExecutionMode.SIMULATION,
                  account_key="SIM_ACCOUNT_A", strategy_key="SIM_STRATEGY_S", session_epoch="SIM_EPOCH_E",
                  symbols=(c.Symbol(exchange="SHFE", instrument_id="SIM_RB2610"),),
                  actions=(c.AuthorizationAction.OPEN_LONG,), max_volume=5,
                  price_bounds=(c.PriceBound(symbol=c.Symbol(exchange="SHFE", instrument_id="SIM_RB2610"),
                                             lower=Decimal("3000"), upper=Decimal("4000")),),
                  expires_at=NOW + timedelta(days=1))
    return c.Authorization(**(values | changes))


def context(**changes):
    values = dict(mode=c.ExecutionMode.SIMULATION, binding=binding(), snapshot=snapshot(), proof=proof(),
                  policy=risk_policy(), authorization=authorization(), reservation_receipt=None,
                  owner_thread_id=17, current_thread_id=17,
                  clock=c.ClockReading(now=NOW, monotonic_ns=100, domain_id="SIM_CLOCK"),
                  ledger_revision=0, route_ownership=None, readiness=readiness(),
                  reconcile=c.ReconcileState(), intent=intent(),
                  reservation_view=reservation_view())
    return c.ExecutionContext(**(values | changes))


def test_plan_reservation_policy_and_context_are_typed_roundtrip_contracts():
    for value in (risk_policy(), plan_value(), reservation_view(), context()):
        assert c.decode(c.encode(value), type(value)) == value
    assert c.validate_context(context(), NOW).ok
    with pytest.raises(c.ContractError):
        risk_policy(max_inflight_legs=True)
    with pytest.raises(c.ContractError):
        plan_value(legs=(replace(plan_value().legs[0], depends_on=("SIM_LEG_1",)),))
    with pytest.raises(c.ContractError):
        plan_value(blockers=("INVALID_PRICE",))


def test_rejected_plan_may_carry_no_legs_and_released_reservations_keep_history():
    rejected = plan_value(legs=(), blockers=("QUOTE_STALE",))
    assert rejected.legs == () and rejected.blockers
    assert c.decode(c.encode(rejected), c.Plan) == rejected
    with pytest.raises(c.ContractError):
        plan_value(legs=(), blockers=())
    key = c.ReservationKey(account_key="SIM_ACCOUNT_A", exchange="SHFE",
                           instrument_id="SIM_RB2610", hedge_flag="1",
                           side=c.Side.LONG, bucket=c.Bucket.TD)
    held = c.Reservation(resource_key=key, leg_id="SIM_LEG_1", state=c.ReservationState.HELD,
                         volume=2, cash_reserve=Decimal("2000"), not_proven_reflected=2)
    released = c.Reservation(resource_key=key, leg_id="SIM_LEG_2", state=c.ReservationState.RELEASED,
                             volume=2, cash_reserve=Decimal("2000"))
    view = reservation_view(reservations=(held, released), cash_reserved=Decimal("2000"))
    assert c.decode(c.encode(view), c.ReservationView) == view
    with pytest.raises(c.ContractError):
        c.Reservation(resource_key=key, leg_id="SIM_LEG_3", state=c.ReservationState.HELD,
                      volume=2, cash_reserve=Decimal("2000"), not_proven_reflected=3)
    with pytest.raises(c.ContractError):
        reservation_view(reservations=(held, replace(held, cash_reserve=Decimal("1"))))


def test_execution_context_default_disabled_and_native_is_always_blocked():
    assert c.ExecutionMode.DISABLED.value == "disabled"
    disabled = context(mode=c.ExecutionMode.DISABLED, authorization=None,
                       readiness=readiness(mode=c.ExecutionMode.DISABLED, can_submit=False,
                                           state=c.ReadinessState.PLAN_READY))
    assert "MODE_DISABLED" in c.validate_context(disabled, NOW).blockers
    with pytest.raises(c.ContractError):
        context(mode=c.ExecutionMode.NATIVE)
    with pytest.raises(c.ContractError):
        readiness(mode=c.ExecutionMode.NATIVE)
    assert c.NATIVE_FULL_ACCOUNT_RECONCILIATION == "unsupported"
    assert c.NATIVE_SUBMIT_ENABLED is False
    assert c.NATIVE_CANCEL_OWNED_ENABLED is False


def test_reconcile_latch_survives_new_clock_or_connection_and_blocks_submits():
    latch = c.ReconcileState(required=True, reasons=(c.ReconcileReason.EVENT_GAP,),
                              since=NOW, evidence_id="SIM_GAP_1")
    ready = readiness(state=c.ReadinessState.RECONCILE_REQUIRED, can_submit=False,
                      blockers=("EVENT_GAP",))
    current = context(reconcile=latch, readiness=ready)
    assert "RECONCILE_REQUIRED" in c.validate_context(current, NOW).blockers
    advanced = replace(current, clock=replace(current.clock, now=NOW + timedelta(seconds=1)),
                       readiness=replace(ready, connection=c.ConnectionState.STALE))
    assert advanced.reconcile == latch
    with pytest.raises(c.ContractError):
        context(reconcile=latch)
    with pytest.raises(c.ContractError):
        c.ReconcileState(required=False, reasons=(c.ReconcileReason.EVENT_GAP,))


def test_context_validation_checks_freshness_thread_revision_and_authorization():
    assert "THREAD_INVALID" in c.validate_context(context(current_thread_id=18), NOW).blockers
    assert "AUTHORIZATION_EXPIRED" in c.validate_context(
        context(authorization=authorization(expires_at=NOW)), NOW).blockers
    assert "AUTHORIZATION_REQUIRED" in c.validate_context(context(authorization=None), NOW).blockers
    with pytest.raises(c.ContractError):
        context(ledger_revision=1)
    assert "LEDGER_REVISION_MISMATCH" in c.validate_context(
        context(ledger_revision=1, reservation_view=reservation_view(ledger_revision=1)), NOW).blockers
    assert not c.validate_context(context(), NOW + timedelta(seconds=4)).ok
    assert "PROOF_REQUIRED" in c.validate_context(context(proof=None), NOW).blockers
    assert not c.validate_context(context(), NOW, action=c.AuthorizationAction.OPEN_SHORT).ok


def event(**changes):
    values = dict(event_id="SIM_EVENT_1", kind=c.EventKind.ORDER, received_at=NOW,
                  callback_session_epoch="SIM_EPOCH_E", account_key=None,
                  ownership=c.Ownership.UNKNOWN, exchange="SHFE", instrument_id="SIM_RB2610",
                  hedge_flag="1", order_id=7, order_sys_id=None, front_id=None, session_id=None,
                  memo=None, trade_id=None, order_time=None, trade_time=None, trading_day=NOW.date(),
                  status=c.OrderState.WORKING, total_volume=4, traded_volume=1, cancel_volume=0,
                  price=Decimal("3500"), volume=None, error_code=None,
                  direction=c.Direction.BUY, offset=c.Offset.OPEN, ownership_evidence=None)
    return c.ExecutionEvent(**(values | changes))


def test_events_preserve_missing_routing_and_require_real_ownership_evidence():
    item = event(order_sys_id=" 00 007 ", trade_id=" 000T1 ")
    assert item.order_sys_id == "00007" and item.trade_id == "000T1"
    assert item.account_key is None and item.front_id is None
    assert c.decode(c.encode(item), c.ExecutionEvent) == item
    with pytest.raises(c.ContractError):
        event(ownership=c.Ownership.OWNED, account_key="SIM_ACCOUNT_A", memo="SIM_LEG_1")
    with pytest.raises(c.ContractError):
        event(total_volume=4, traded_volume=3, cancel_volume=2, ownership=c.Ownership.OWNED,
              account_key="SIM_ACCOUNT_A",
              ownership_evidence=c.OwnershipEvidence(journal_leg_id="SIM_LEG_1", matched_alias="local",
                                                     callback_account_verified=True,
                                                     strategy_scope_verified=True,
                                                     evidence_id="SIM_OWN_1"))
    with pytest.raises(c.ContractError):
        event(order_id=True)
    assert event(total_volume=None, traded_volume=None, cancel_volume=None).total_volume is None


def owned_evidence():
    return c.OwnershipEvidence(journal_leg_id="SIM_LEG_1", matched_alias="local",
                               callback_account_verified=True, strategy_scope_verified=True,
                               evidence_id="SIM_OWN_1")


def test_contradictory_unknown_events_roundtrip_for_quarantine():
    contradictory = event(kind=c.EventKind.TRADE, trade_id=None, volume=None, price=None,
                          direction=None, offset=None, exchange=None, instrument_id=None,
                          hedge_flag=None, order_id=None, order_sys_id=None,
                          ownership=c.Ownership.UNKNOWN,
                          total_volume=4, traded_volume=3, cancel_volume=2)
    assert contradictory.traded_volume + contradictory.cancel_volume > contradictory.total_volume
    assert c.decode(c.encode(contradictory), c.ExecutionEvent) == contradictory
    with pytest.raises(c.ContractError):
        event(traded_volume=True, ownership=c.Ownership.UNKNOWN)
    with pytest.raises(c.ContractError):
        event(price=Decimal("NaN"), ownership=c.Ownership.UNKNOWN)
    codeless = event(kind=c.EventKind.ERROR, error_code=None, status=None, price=None,
                     total_volume=None, traded_volume=None, cancel_volume=None,
                     order_id=None, order_sys_id=None, direction=None, offset=None,
                     ownership=c.Ownership.UNKNOWN)
    assert codeless.error_code is None
    assert c.decode(c.encode(codeless), c.ExecutionEvent) == codeless


def test_owned_events_still_require_conservation_and_complete_keys():
    evidence = owned_evidence()
    for changes in (
        dict(total_volume=4, traded_volume=5),
        dict(total_volume=4, traded_volume=3, cancel_volume=2),
        dict(kind=c.EventKind.TRADE, trade_id=None, volume=None, price=None,
             direction=None, offset=None),
        dict(kind=c.EventKind.ORDER, order_id=None, order_sys_id=None),
        dict(kind=c.EventKind.ERROR, error_code=None),
    ):
        with pytest.raises(c.ContractError):
            event(ownership=c.Ownership.OWNED, account_key="SIM_ACCOUNT_A",
                  ownership_evidence=evidence, **changes)


def test_owned_trade_requires_full_identity_keys_with_identifier_format():
    evidence = owned_evidence()
    base = dict(kind=c.EventKind.TRADE, ownership=c.Ownership.OWNED,
                account_key="SIM_ACCOUNT_A", ownership_evidence=evidence,
                trade_id="000T1", volume=1, price=Decimal("3500"),
                direction=c.Direction.BUY, offset=c.Offset.OPEN, order_id=7, status=None)
    complete = event(**base)
    assert complete.trading_day is not None and complete.exchange == "SHFE"
    assert c.decode(c.encode(complete), c.ExecutionEvent) == complete
    for changes in (
        dict(trading_day=None),
        dict(exchange=None),
        dict(instrument_id=None),
        dict(account_key="unsafe account label"),
        dict(account_key=None),
        dict(volume=0),
    ):
        with pytest.raises(c.ContractError):
            event(**(base | changes))


def test_unknown_zero_quantities_are_preserved_verbatim_for_quarantine():
    zeroed = event(kind=c.EventKind.TRADE, trade_id=None, volume=0, price=None,
                   direction=None, offset=None, exchange=None, instrument_id=None,
                   hedge_flag=None, order_id=None, order_sys_id=None, status=None,
                   ownership=c.Ownership.UNKNOWN,
                   total_volume=0, traded_volume=0, cancel_volume=0)
    assert (zeroed.total_volume, zeroed.traded_volume, zeroed.cancel_volume, zeroed.volume) == (0, 0, 0, 0)
    assert c.decode(c.encode(zeroed), c.ExecutionEvent) == zeroed
    with pytest.raises(c.ContractError):
        event(traded_volume=-1, ownership=c.Ownership.UNKNOWN)


def test_attempt_gate_requires_typed_intent_and_reservation_view():
    assert c.validate_context(context(), NOW, require_attempt=True).ok
    missing_intent = c.validate_context(context(intent=None), NOW, require_attempt=True)
    assert "INTENT_REQUIRED" in missing_intent.blockers
    missing_view = c.validate_context(context(reservation_view=None), NOW, require_attempt=True)
    assert "RESERVATION_VIEW_REQUIRED" in missing_view.blockers
    expired = c.validate_context(
        context(intent=intent(expires_at=NOW + timedelta(seconds=1))), NOW + timedelta(seconds=2),
        require_attempt=True)
    assert "EXPIRED" in expired.blockers


def test_foreign_binding_or_intent_cannot_ride_a_context_plan():
    plan = plan_value()
    assert plan.intent_hash == c.canonical_hash(context().intent)
    assert c.canonical_hash(intent(intent_id="SIM_INTENT_2")) != plan.intent_hash
    with pytest.raises(c.ContractError):
        context(binding=binding(account_key="SIM_ACCOUNT_B"))
    with pytest.raises(c.ContractError):
        context(reservation_view=reservation_view(account_key="SIM_ACCOUNT_B"))
    with pytest.raises(c.ContractError):
        context(reservation_view=reservation_view(ledger_revision=1))
    with pytest.raises(c.ContractError):
        context(reservation_view=reservation_view(snapshot_id="SIM_SNAPSHOT_9"))


def test_incomplete_unknown_events_are_quarantinable_not_rejected():
    raw = event(kind=c.EventKind.TRADE, trade_id=None, volume=None, price=None,
                direction=None, offset=None, exchange=None, instrument_id=None,
                hedge_flag=None, order_id=None, order_sys_id=None,
                ownership=c.Ownership.UNKNOWN)
    assert raw.trade_id is None and raw.exchange is None and raw.ownership is c.Ownership.UNKNOWN
    assert c.decode(c.encode(raw), c.ExecutionEvent) == raw
    routing_less = event(kind=c.EventKind.ORDER, order_id=None, order_sys_id=None,
                         exchange=None, instrument_id=None)
    assert c.decode(c.encode(routing_less), c.ExecutionEvent) == routing_less
    with pytest.raises(c.ContractError):
        event(ownership=c.Ownership.OWNED, account_key="SIM_ACCOUNT_A", memo="SIM_LEG_1")


def cancel_context(**changes):
    base = snapshot()
    stale_quote = timing(sampled_at=NOW - timedelta(seconds=10), source_at=NOW - timedelta(seconds=10))
    stale = replace(base, times=replace(base.times, quote=stale_quote),
                    quote=replace(base.quote, time=NOW - timedelta(seconds=10)))
    values = dict(snapshot=stale, proof=None,
                  authorization=authorization(actions=(c.AuthorizationAction.CANCEL_OWNED,)),
                  readiness=readiness(state=c.ReadinessState.PLAN_READY, can_plan=True,
                                      can_submit=False, can_cancel_owned=True, blockers=(),
                                      coverage=coverage(level=c.CoverageLevel.OWN_JOURNAL,
                                                        strategy_key="SIM_STRATEGY_S",
                                                        complete_through=None,
                                                        completion_evidence_id=None,
                                                        account_active_count=None)),
                  route_ownership=c.RouteOwnership(route="cancel", evidence_id="SIM_ROUTE_1", verified=True))
    return context(**(values | changes))


def test_cancel_owned_is_independent_of_quote_ttl_proof_and_can_submit():
    ctx = cancel_context()
    assert c.validate_context(ctx, NOW, action=c.AuthorizationAction.CANCEL_OWNED).ok
    assert not c.validate_context(ctx, NOW).ok
    bad_route = c.RouteOwnership(route="cancel", evidence_id="SIM_ROUTE_1", verified=False)
    assert "CANCEL_ROUTE_UNVERIFIED" in c.validate_context(
        replace(ctx, route_ownership=bad_route), NOW, action=c.AuthorizationAction.CANCEL_OWNED).blockers
    no_ability = replace(ctx, readiness=replace(ctx.readiness, can_cancel_owned=False))
    assert "CANCEL_NOT_AVAILABLE" in c.validate_context(
        no_ability, NOW, action=c.AuthorizationAction.CANCEL_OWNED).blockers
    no_auth = replace(ctx, authorization=authorization(actions=(c.AuthorizationAction.OPEN_LONG,)))
    assert "AUTHORIZATION_ACTION" in c.validate_context(
        no_auth, NOW, action=c.AuthorizationAction.CANCEL_OWNED).blockers
    wrong_thread = replace(ctx, current_thread_id=18)
    assert "THREAD_INVALID" in c.validate_context(
        wrong_thread, NOW, action=c.AuthorizationAction.CANCEL_OWNED).blockers
    latched = c.ReconcileState(required=True, reasons=(c.ReconcileReason.LEDGER_CORRUPT,),
                               since=NOW, evidence_id="SIM_GAP_1")
    latched_ready = replace(ctx.readiness, state=c.ReadinessState.RECONCILE_REQUIRED,
                            blockers=("LEDGER_FAILURE",))
    ledger_broken = replace(ctx, reconcile=latched, readiness=latched_ready)
    assert "LEDGER_FAILURE" in c.validate_context(
        ledger_broken, NOW, action=c.AuthorizationAction.CANCEL_OWNED).blockers
    gap_only = replace(ctx, reconcile=c.ReconcileState(
        required=True, reasons=(c.ReconcileReason.EVENT_GAP,), since=NOW, evidence_id="SIM_GAP_2"),
        readiness=replace(ctx.readiness, state=c.ReadinessState.RECONCILE_REQUIRED,
                          blockers=("EVENT_GAP",)))
    assert c.validate_context(gap_only, NOW, action=c.AuthorizationAction.CANCEL_OWNED).ok
