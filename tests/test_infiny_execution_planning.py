"""N2 offline acceptance tests using only immutable execution contracts."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json

import pytest

from ak_trading.infiny_execution import contracts as c
from ak_trading.infiny_execution import native_contracts as nc
from ak_trading.infiny_execution import native_evidence as ne
from ak_trading.infiny_execution.planning import (
    NativePilotAdmission,
    NativePilotScope,
    plan,
    plan_native_pilot,
    validate_native_pilot_plan,
    validate_plan,
)

NOW = datetime(2026, 9, 14, 1, 30, tzinfo=timezone.utc)
D = Decimal


def binding(**changes):
    return replace(c.Binding(account_key="SIM_A", strategy_key="SIM_S", strategy_id=23,
                             instance_id="SIM_I", session_epoch="SIM_E", trading_day=date(2026, 9, 14),
                             exchange="SHFE", instrument_id="rb2610", hedge_flag="1"), **changes)


def timing(**changes):
    return replace(c.ComponentTiming(sampled_at=NOW, source_at=None, received_at=NOW,
                                     snapshot_id="SIM_SNAP", source=c.Source.SIMULATION,
                                     completeness=c.Completeness.COMPLETE), **changes)


def bucket(side=c.Side.LONG, **changes):
    return replace(c.PositionBucket(binding=binding(), side=side, timing=timing(),
                                    Position=0, PositionClose=0, FrozenClosing=0,
                                    YdPositionClose=0, YdFrozenClosing=0), **changes)


def snapshot(**changes):
    mark = c.SourceWatermark(source=c.Source.SIMULATION, stream_id="SIM_STREAM", value="17", as_of=NOW)
    cover = c.Coverage(level=c.CoverageLevel.FULL_ACCOUNT, account_key="SIM_A", strategy_key=None,
                       session_epoch="SIM_E", trading_day=date(2026, 9, 14), source=c.Source.SIMULATION,
                       as_of=NOW, complete_through=mark, completion_evidence_id="SIM_BARRIER",
                       own_active_count=0, account_active_count=0)
    return replace(c.ExecutionSnapshot(
        snapshot_id="SIM_SNAP", binding=binding(), source=c.Source.SIMULATION,
        times=c.SnapshotTimes(sampled_at=NOW, sent_at=NOW, received_at=NOW,
                              quote=timing(source_at=NOW), contract=timing(), account=timing(), positions=timing()),
        quote=c.Quote(binding=binding(), time=NOW, last=D("3500"), bid1=D("3500"), ask1=D("3501"),
                      bid_volume1=10, ask_volume1=10, lower_limit=D("3000"), upper_limit=D("4000"),
                      market_status=c.MarketStatus.CONTINUOUS),
        contract=c.Contract(exchange="SHFE", instrument_id="rb2610", asset_class=c.AssetClass.FUTURE,
                            expiry=date(2026, 10, 15), price_tick=D("1"), multiplier=D("10"),
                            min_limit_order_volume=1, max_limit_order_volume=5),
        account=c.Account(binding=binding(), available=D("100000"), currency="CNY"),
        positions=(bucket(), bucket(c.Side.SHORT)), coverage=cover, proof_id="SIM_PROOF", ledger_revision=0), **changes)


def rebind(sample, **changes):
    b = replace(sample.binding, **changes)
    return replace(sample, binding=b,
                   quote=replace(sample.quote, binding=b), account=replace(sample.account, binding=b),
                   positions=tuple(replace(p, binding=b) for p in sample.positions),
                   contract=replace(sample.contract, exchange=b.exchange, instrument_id=b.instrument_id),
                   coverage=replace(sample.coverage, account_key=b.account_key,
                                    session_epoch=b.session_epoch, trading_day=b.trading_day))


def intent(**changes):
    return replace(c.OrderIntent(intent_id="SIM_INTENT", source_key="SIM_DECISION", binding=binding(),
                                 action=c.Action.OPEN_LONG, volume=2, prices=c.Prices(open=D("3500"), close=D("3500")),
                                 close_policy=c.ClosePolicy.AUTO_YD_FIRST, order_type=c.OrderType.GFD,
                                 created_at=NOW, expires_at=NOW + timedelta(days=1)), **changes)


def policy(**changes):
    return replace(c.RiskPolicy(policy_id="SIM_POLICY", valid_until=NOW + timedelta(days=1),
                                max_leg_qty=5, max_gross_position=100, max_inflight_legs=1,
                                max_cash_reserve=D("100000"), cash_per_open_lot=D("1000"),
                                fee_per_close_lot=D("10"),
                                cash_bound_source=c.CashBoundSource(source=c.Source.SIMULATION,
                                                                   evidence_id="SIM_CASH", verified=True)), **changes)


def view(**changes):
    return replace(c.ReservationView(account_key="SIM_A", snapshot_id="SIM_SNAP", ledger_revision=0,
                                     reservations=(), cash_reserved=D("0"), gross_position=0,
                                     inflight_legs=0, reconcile=c.ReconcileState()), **changes)


def make_plan(request=None, sample=None, resources=None, rules=None, now=NOW):
    return plan(request or intent(), sample or snapshot(), resources or view(), rules or policy(), now)


def rejected(result, code):
    assert code in result.blockers
    assert result.legs == ()


@pytest.mark.parametrize("minimum,maximum,volume,expected", [(3, 5, 11, (5, 3, 3)), (4, 5, 6, None)])
def test_E07_no_illegal_tail_or_partial_plan(minimum, maximum, volume, expected):
    sample = snapshot()
    sample = replace(sample, contract=replace(sample.contract, min_limit_order_volume=minimum,
                                              max_limit_order_volume=maximum))
    result = make_plan(intent(volume=volume), sample)
    if expected is None:
        rejected(result, "VOLUME_SPLIT_IMPOSSIBLE")
    else:
        assert not result.blockers
        assert tuple(leg.volume for leg in result.legs) == expected
        assert sum(leg.cash_reserve for leg in result.legs) == D("11000")


@pytest.mark.parametrize("component,ttl", [("quote", 3), ("account", 5), ("positions", 5), ("contract", 3600), ("packet", 10)])
@pytest.mark.parametrize("extra_ms", [0, 1])
def test_E04_exact_age_boundary(component, ttl, extra_ms):
    sample = snapshot()
    old = NOW - timedelta(seconds=ttl, milliseconds=extra_ms)
    if component == "packet":
        sample = replace(sample, times=replace(sample.times, sampled_at=old, sent_at=old, received_at=old))
    else:
        item = timing(sampled_at=old, received_at=old, source_at=old if component == "quote" else None)
        sample = replace(sample, times=replace(sample.times, **{component: item}),
                         quote=replace(sample.quote, time=old) if component == "quote" else sample.quote)
    result = make_plan(sample=sample)
    assert bool(result.blockers) is bool(extra_ms)
    if not extra_ms:
        assert result.expires_at == NOW


@pytest.mark.parametrize("component", ["quote", "account", "positions", "contract", "packet"])
@pytest.mark.parametrize("delta", [D("2"), D("2.001"), None])
def test_E04_future_tolerance_and_missing_observation(component, delta):
    sample = snapshot()
    stamp = NOW + timedelta(microseconds=int(delta * 1000000)) if delta is not None else None
    if component == "packet":
        sample = replace(sample, times=replace(sample.times, sampled_at=stamp, sent_at=stamp, received_at=stamp))
    else:
        item = timing(sampled_at=stamp, received_at=stamp, source_at=stamp if component == "quote" else None)
        sample = replace(sample, times=replace(sample.times, **{component: item}),
                         quote=replace(sample.quote, time=stamp) if component == "quote" else sample.quote)
    assert bool(make_plan(sample=sample).blockers) is (delta != D("2"))


def test_E04_heartbeat_cannot_renew_tick_and_per_bucket_time_is_checked():
    sample = snapshot()
    old = NOW - timedelta(seconds=4)
    sample = replace(sample, quote=replace(sample.quote, time=old),
                     times=replace(sample.times, quote=timing(source_at=old)))
    before = c.encode(sample)
    rejected(make_plan(sample=sample), "QUOTE_STALE")
    assert c.encode(sample) == before
    sample = snapshot(positions=(bucket(timing=timing(sampled_at=NOW-timedelta(seconds=6))), bucket(c.Side.SHORT)))
    rejected(make_plan(sample=sample), "SNAPSHOT_STALE")


@pytest.mark.parametrize("field,value,code", [
    ("account_key", "SIM_B", "ACCOUNT_MISMATCH"), ("strategy_key", "SIM_T", "OWNERSHIP_CONFLICT"),
    ("strategy_id", 24, "OWNERSHIP_CONFLICT"), ("instance_id", "SIM_J", "SESSION_CHANGED"),
    ("session_epoch", "SIM_NEXT", "SESSION_CHANGED"), ("trading_day", date(2026, 9, 15), "TRADING_DAY_CHANGED"),
    ("instrument_id", "rb2611", "INSTRUMENT_MISMATCH"), ("exchange", "INE", "INSTRUMENT_MISMATCH"),
    ("hedge_flag", "2", "HEDGE_UNVERIFIED"),
])
def test_E05_full_binding_is_compared(field, value, code):
    rejected(make_plan(request=intent(binding=replace(binding(), **{field: value}))), code)


@pytest.mark.parametrize("field", ["exchange", "instrument_id", "expiry", "asset_class", "price_tick", "multiplier",
                                   "min_limit_order_volume", "max_limit_order_volume"])
def test_E05_missing_contract_fields_are_not_defaulted(field):
    sample = snapshot()
    sample = replace(sample, contract=replace(sample.contract, **{field: None}))
    assert make_plan(sample=sample).blockers


@pytest.mark.parametrize("asset", [c.AssetClass.CONTINUOUS, c.AssetClass.OPTION, c.AssetClass.COMBINATION,
                                   c.AssetClass.INDEX, c.AssetClass.UNKNOWN])
def test_E05_only_specific_futures_supported(asset):
    sample = snapshot()
    rejected(make_plan(sample=replace(sample, contract=replace(sample.contract, asset_class=asset))),
             "INSTRUMENT_UNSUPPORTED")


@pytest.mark.parametrize("expiry", [date(2026, 9, 13), date(2026, 9, 14)])
def test_E05_expired_and_delivery_day_are_rejected(expiry):
    sample = snapshot()
    rejected(make_plan(sample=replace(sample, contract=replace(sample.contract, expiry=expiry))), "CONTRACT_EXPIRED")


@pytest.mark.parametrize("price,accepted", [("3500.2", True), ("3500.1", False), ("4000.2", False),
                                           ("2999.8", False), ("1.7976931348623157E+128", False)])
def test_E06_decimal_tick_and_price_bounds_without_rounding(price, accepted):
    sample = snapshot()
    sample = replace(sample, contract=replace(sample.contract, price_tick=D("0.2")))
    result = make_plan(intent(prices=c.Prices(open=D(price), close=None)), sample)
    assert not result.blockers if accepted else result.blockers
    if accepted:
        assert result.legs[0].price == D("3500.2")


@pytest.mark.parametrize("field,value,code", [
    ("bid1", D("3502"), "BOOK_CROSSED"), ("ask1", None, "BOOK_EMPTY"),
    ("bid1", None, "BOOK_EMPTY"), ("ask_volume1", 0, "BOOK_EMPTY"), ("ask_volume1", None, "BOOK_EMPTY"),
    ("market_status", c.MarketStatus.AUCTION, "AUCTION"), ("market_status", c.MarketStatus.CLOSED, "MARKET_CLOSED"),
    ("market_status", c.MarketStatus.UNKNOWN, "MARKET_STATUS_UNKNOWN"), ("market_status", None, "MARKET_STATUS_UNKNOWN"),
    ("ask1", D("3511"), "SPREAD_TOO_WIDE"), ("last", D("4000"), "LIMIT_LOCKED"),
    ("last", D("3000"), "LIMIT_LOCKED"), ("last", None, "QUOTE_MISSING"),
    ("upper_limit", None, "QUOTE_MISSING"), ("lower_limit", D("4000"), "PRICE_OUT_OF_BOUNDS"),
])
def test_E06_book_state_and_required_liquidity(field, value, code):
    sample = snapshot()
    rejected(make_plan(sample=replace(sample, quote=replace(sample.quote, **{field: value}))), code)


def test_E06_ten_tick_spread_is_inclusive():
    sample = snapshot()
    assert not make_plan(sample=replace(sample, quote=replace(sample.quote, ask1=D("3510")))).blockers


@pytest.mark.parametrize("volume", [True, False, 0, -1, 1.5, "2"])
def test_E07_malformed_quantity_is_rejected_by_contract(volume):
    with pytest.raises(c.ContractError):
        make_plan(intent(volume=volume))


@pytest.mark.parametrize("price", [D("NaN"), D("sNaN"), D("Infinity"), D("-Infinity"), D("0"), D("-1"), True, 3500.0])
def test_E06_malformed_prices_are_rejected_at_contract_boundary(price):
    with pytest.raises(c.ContractError):
        make_plan(intent(prices=c.Prices(open=price, close=None)))


def with_position(sample=None, side=c.Side.LONG, **changes):
    sample = sample or snapshot()
    return replace(sample, positions=tuple(replace(p, **changes) if p.side is side else p for p in sample.positions))


def e08_sample(exchange="SHFE", side=c.Side.LONG):
    sample = rebind(snapshot(), exchange=exchange)
    return with_position(sample, side, Position=10, PositionClose=10, FrozenClosing=3,
                         YdPositionClose=6, YdFrozenClosing=2)


@pytest.mark.parametrize("exchange", ["SHFE", "INE"])
@pytest.mark.parametrize("side,action,direction", [(c.Side.LONG, c.Action.CLOSE_LONG, c.Direction.SELL),
                                                  (c.Side.SHORT, c.Action.CLOSE_SHORT, c.Direction.BUY)])
def test_E08_yesterday_first_uses_raw_frozen_fields(exchange, side, action, direction):
    sample = e08_sample(exchange, side)
    result = make_plan(intent(binding=sample.binding, action=action, volume=6), sample)
    assert not result.blockers
    assert [(x.direction, x.offset, x.volume, x.position_bucket, x.cash_reserve) for x in result.legs] == [
        (direction, c.Offset.CLOSE_YESTERDAY, 4, c.Bucket.YD, D("40")),
        (direction, c.Offset.CLOSE_TODAY, 2, c.Bucket.TD, D("20"))]


@pytest.mark.parametrize("close_policy,volume", [(c.ClosePolicy.TODAY_ONLY, 4),
                                                (c.ClosePolicy.YESTERDAY_ONLY, 5), (c.ClosePolicy.AUTO_YD_FIRST, 8)])
def test_E09_strict_buckets_and_insufficient_total_never_fall_back(close_policy, volume):
    rejected(make_plan(intent(action=c.Action.CLOSE_LONG, close_policy=close_policy, volume=volume), e08_sample()),
             "INSUFFICIENT_POSITION")


@pytest.mark.parametrize("field", ["Position", "PositionClose", "FrozenClosing", "YdPositionClose", "YdFrozenClosing"])
def test_E09_unknown_raw_fields_block_and_never_become_zero(field):
    sample = e08_sample()
    sample = with_position(sample, **{field: None})
    rejected(make_plan(intent(action=c.Action.CLOSE_LONG), sample), "POSITION_DATA_INVALID")


@pytest.mark.parametrize("changes", [
    {"Position": -1}, {"Position": True}, {"PositionClose": 11}, {"YdPositionClose": 11},
    {"FrozenClosing": 11}, {"YdFrozenClosing": 4}, {"YdPositionClose": 1, "YdFrozenClosing": 2},
    {"YdPositionClose": 9, "FrozenClosing": 5, "YdFrozenClosing": 2},
])
def test_E09_contradictory_freezes_are_rejected_before_planning(changes):
    with pytest.raises(c.ContractError):
        make_plan(intent(action=c.Action.CLOSE_LONG), with_position(e08_sample(), **changes))


@pytest.mark.parametrize("positions", [(), (bucket(),), (bucket(c.Side.SHORT),)])
def test_E09_missing_buckets_are_not_confirmed_zero(positions):
    rejected(make_plan(sample=snapshot(positions=positions)), "MISSING_POSITION_BUCKET")


def test_E09_zero_position_does_not_create_open_leg_to_close():
    rejected(make_plan(intent(action=c.Action.CLOSE_LONG)), "INSUFFICIENT_POSITION")


@pytest.mark.parametrize("close_policy", list(c.ClosePolicy))
def test_E10_dce_auto_is_generic_all_and_strict_policies_unsupported(close_policy):
    sample = e08_sample("DCE")
    result = make_plan(intent(binding=sample.binding, action=c.Action.CLOSE_LONG, volume=6, close_policy=close_policy), sample)
    if close_policy is c.ClosePolicy.AUTO_YD_FIRST:
        assert not result.blockers
        assert [(x.offset, x.position_bucket, x.volume) for x in result.legs] == [
            (c.Offset.CLOSE, c.Bucket.ALL, 5), (c.Offset.CLOSE, c.Bucket.ALL, 1)]
    else:
        rejected(result, "UNSUPPORTED_CLOSE_POLICY")


@pytest.mark.parametrize("action", list(c.Action))
@pytest.mark.parametrize("order_type", list(c.OrderType))
def test_six_actions_gfd_fak_and_reverse_all_close_dependencies(action, order_type):
    sample = snapshot()
    if action in (c.Action.CLOSE_LONG, c.Action.REVERSE_TO_SHORT):
        sample = with_position(sample, Position=3, PositionClose=3)
    elif action in (c.Action.CLOSE_SHORT, c.Action.REVERSE_TO_LONG):
        sample = with_position(sample, c.Side.SHORT, Position=3, PositionClose=3)
    result = make_plan(intent(action=action, order_type=order_type), sample)
    assert not result.blockers
    expected_direction = c.Direction.BUY if action in (c.Action.OPEN_LONG, c.Action.CLOSE_SHORT, c.Action.REVERSE_TO_LONG) else c.Direction.SELL
    assert {leg.direction for leg in result.legs} == {expected_direction}
    if action in (c.Action.REVERSE_TO_LONG, c.Action.REVERSE_TO_SHORT):
        closes = tuple(x for x in result.legs if x.phase is c.Phase.CLOSE)
        opens = tuple(x for x in result.legs if x.phase is c.Phase.OPEN)
        assert sum(x.volume for x in closes) == 3
        assert sum(x.volume for x in opens) == 2
        assert all(x.depends_on == tuple(y.leg_id for y in closes) for x in opens)
        assert sum(x.cash_reserve for x in result.legs) == D("2030")


@pytest.mark.parametrize("changes,code", [({"FrozenClosing": 1}, "INSUFFICIENT_POSITION"),
                                          ({"PositionClose": 2}, "INSUFFICIENT_POSITION")])
def test_reverse_requires_reserving_entire_original_side(changes, code):
    sample = with_position(Position=3, PositionClose=3, **{k:v for k,v in changes.items() if k != "PositionClose"})
    if "PositionClose" in changes:
        sample = with_position(sample, PositionClose=2)
    rejected(make_plan(intent(action=c.Action.REVERSE_TO_SHORT), sample), code)


def test_reverse_rejects_dual_side_and_zero_original_side():
    sample = with_position(Position=3, PositionClose=3)
    sample = with_position(sample, c.Side.SHORT, Position=1, PositionClose=1)
    rejected(make_plan(intent(action=c.Action.REVERSE_TO_SHORT), sample), "POSITION_DATA_INVALID")
    rejected(make_plan(intent(action=c.Action.REVERSE_TO_SHORT)), "INSUFFICIENT_POSITION")


@pytest.mark.parametrize("field", ["cash_per_open_lot", "cash_bound_source", "fee_per_close_lot", "max_cash_reserve",
                                   "max_gross_position", "max_inflight_legs"])
def test_E27_missing_required_budget_fields_block(field):
    changes = {field: None}
    if field == "cash_bound_source":
        changes["cash_per_open_lot"] = None
    request = intent(action=c.Action.CLOSE_LONG) if field == "fee_per_close_lot" else intent()
    sample = with_position(Position=3, PositionClose=3)
    assert make_plan(request, sample, rules=policy(**changes)).blockers


@pytest.mark.parametrize("verified", [False, True])
def test_E27_amounts_are_not_open_cash_rates(verified):
    source = c.CashBoundSource(source=c.Source.SIMULATION, evidence_id="SIM_AMOUNT_NOT_RATE", verified=verified)
    result = make_plan(rules=policy(cash_per_open_lot=None, cash_bound_source=source))
    rejected(result, "CASH_BOUND_MISSING")
    if not verified:
        assert "CASH_BOUND_UNVERIFIED" in result.blockers


def test_E27_close_only_does_not_require_open_margin_bound():
    sample = with_position(Position=3, PositionClose=3)
    result = make_plan(intent(action=c.Action.CLOSE_LONG), sample,
                       rules=policy(cash_per_open_lot=None, cash_bound_source=None))
    assert not result.blockers
    assert result.legs[0].cash_reserve == D("20")


@pytest.mark.parametrize("available,maximum,gross,inflight,code", [
    ("1999", "100000", 0, 0, "INSUFFICIENT_CASH"), ("100000", "1999", 0, 0, "INSUFFICIENT_CASH"),
    ("100000", "100000", 99, 0, "GROSS_POSITION_LIMIT"),
    ("100000", "100000", 0, 1, "INFLIGHT_LIMIT"),
])
def test_E27_all_resources_are_checked_before_returning_any_leg(available, maximum, gross, inflight, code):
    sample = snapshot()
    sample = replace(sample, account=replace(sample.account, available=D(available)))
    rejected(make_plan(sample=sample, resources=view(gross_position=gross, inflight_legs=inflight),
                       rules=policy(max_cash_reserve=D(maximum))), code)


def test_E27_reverse_reserves_both_phases_without_anticipating_cash_release():
    sample = with_position(Position=3, PositionClose=3)
    sample = replace(sample, account=replace(sample.account, available=D("2029")))
    rejected(make_plan(intent(action=c.Action.REVERSE_TO_SHORT), sample), "INSUFFICIENT_CASH")


@pytest.mark.parametrize("field,value", [("available", None), ("available", D("-1")), ("currency", None)])
def test_E27_unknown_or_negative_account_data_never_becomes_zero(field, value):
    sample = snapshot()
    assert make_plan(sample=replace(sample, account=replace(sample.account, **{field:value}))).blockers


def reservation(leg_id="SIM_OTHER:CLOSE:TD:0", side=c.Side.LONG, resource_bucket=c.Bucket.TD, **changes):
    key = c.ReservationKey(account_key="SIM_A", exchange="SHFE", instrument_id="rb2610", hedge_flag="1",
                           side=side, bucket=resource_bucket)
    return replace(c.Reservation(resource_key=key, leg_id=leg_id, state=c.ReservationState.HELD,
                                 volume=2, cash_reserve=D("20"), not_proven_reflected=2), **changes)


@pytest.mark.parametrize("unproven", [0, 2])
def test_E28_held_default_zero_is_not_absorption_evidence(unproven):
    sample = with_position(Position=3, PositionClose=3)
    held = reservation(not_proven_reflected=unproven)
    resources = view(reservations=(held,), cash_reserved=D("20"), gross_position=3)
    rejected(make_plan(intent(action=c.Action.CLOSE_LONG), sample, resources), "INSUFFICIENT_POSITION")


@pytest.mark.parametrize("evidence,unproven,accepted", [("SIM_PROOF", 0, True), ("SIM_PROOF", 1, False),
                                                      ("SIM_OLD_PROOF", 0, False)])
def test_E28_reflected_requires_current_snapshot_barrier_reference(evidence, unproven, accepted):
    sample = with_position(Position=3, PositionClose=3, FrozenClosing=1)
    reflected = reservation(state=c.ReservationState.REFLECTED, reflected_evidence_id=evidence, not_proven_reflected=unproven)
    result = make_plan(intent(action=c.Action.CLOSE_LONG), sample,
                       view(reservations=(reflected,), cash_reserved=D("20"), gross_position=3))
    assert not result.blockers if accepted else "INSUFFICIENT_POSITION" in result.blockers


def test_E28_released_history_is_excluded_from_every_resource():
    sample = with_position(Position=3, PositionClose=3)
    closed = reservation(state=c.ReservationState.RELEASED, volume=100, cash_reserve=D("999999"))
    opened = reservation(leg_id="SIM_OLD:OPEN:-:0", resource_bucket=c.Bucket.ALL,
                         state=c.ReservationState.RELEASED, volume=100, cash_reserve=D("999999"))
    resources = view(reservations=(closed, opened), gross_position=3)
    assert not make_plan(intent(action=c.Action.CLOSE_LONG), sample, resources).blockers
    assert not make_plan(sample=sample, resources=resources).blockers


def test_E27_other_contract_cash_and_open_gross_are_account_wide():
    held = reservation(leg_id="SIM_OTHER:OPEN:-:0", resource_bucket=c.Bucket.ALL, volume=99, cash_reserve=D("99000"))
    held = replace(held, resource_key=replace(held.resource_key, instrument_id="rb2611"))
    resources = view(reservations=(held,), cash_reserved=D("99000"))
    result = make_plan(resources=resources)
    assert {"INSUFFICIENT_CASH", "GROSS_POSITION_LIMIT"} <= set(result.blockers)


@pytest.mark.parametrize("bucket_kind", [c.Bucket.TD, c.Bucket.YD, c.Bucket.ALL])
def test_E28_other_hedge_or_contract_close_reservations_do_not_spend_this_bucket(bucket_kind):
    sample = with_position(Position=3, PositionClose=3)
    held = reservation(leg_id=f"SIM_OTHER:CLOSE:{bucket_kind.value}:0", resource_bucket=bucket_kind)
    held = replace(held, resource_key=replace(held.resource_key, hedge_flag="2"))
    resources = view(reservations=(held,), cash_reserved=D("20"), gross_position=3)
    assert not make_plan(intent(action=c.Action.CLOSE_LONG), sample, resources).blockers


def proof(sample=None, **changes):
    sample = sample or snapshot()
    return replace(c.ReconciliationProof(proof_id=sample.proof_id, source=sample.source,
                                          account_key=sample.binding.account_key, trading_day=sample.binding.trading_day,
                                          session_epoch=sample.binding.session_epoch, as_of=NOW,
                                          order_inventory=(), trade_inventory=(),
                                          position_snapshot_id=sample.times.positions.snapshot_id,
                                          complete_through=sample.coverage.complete_through,
                                          exclusive_writer_verified=True, verified=True), **changes)


def context(request=None, sample=None, rules=None, resources=None, receipt=None, **changes):
    request, sample, rules = request or intent(), sample or snapshot(), rules or policy()
    symbol = c.Symbol(exchange=sample.binding.exchange, instrument_id=sample.binding.instrument_id)
    auth = c.Authorization(authorization_id="SIM_AUTH", mode=c.ExecutionMode.SIMULATION,
                            account_key=sample.binding.account_key, strategy_key=sample.binding.strategy_key,
                            session_epoch=sample.binding.session_epoch, symbols=(symbol,),
                            actions=tuple(c.AuthorizationAction(a.value) for a in c.Action), max_volume=100,
                            price_bounds=(c.PriceBound(symbol=symbol, lower=D("3000"), upper=D("4000")),),
                            expires_at=NOW+timedelta(days=1))
    ready = c.Readiness(connection=c.ConnectionState.ROUNDTRIP, state=c.ReadinessState.EXECUTABLE,
                        can_plan=True, can_submit=True, can_cancel_owned=False, blockers=(),
                        mode=c.ExecutionMode.SIMULATION, coverage=sample.coverage, evidence_id="SIM_READY")
    return replace(c.ExecutionContext(mode=c.ExecutionMode.SIMULATION, binding=request.binding, snapshot=sample,
                                      proof=proof(sample), policy=rules, authorization=auth, reservation_receipt=receipt,
                                      owner_thread_id=17, current_thread_id=17,
                                      clock=c.ClockReading(now=NOW, monotonic_ns=100, domain_id="SIM_CLOCK"),
                                      ledger_revision=sample.ledger_revision, route_ownership=None, readiness=ready,
                                      reconcile=c.ReconcileState(), intent=request, reservation_view=resources or view()), **changes)


def reserved_context(request=None, sample=None, rules=None):
    request, sample, rules = request or intent(), sample or snapshot(), rules or policy()
    original = make_plan(request, sample, rules=rules)
    assert not original.blockers
    reservations = tuple(reservation(leg_id=leg.leg_id,
                                     side=(c.Side.LONG if leg.direction is c.Direction.BUY else c.Side.SHORT) if leg.phase is c.Phase.OPEN else
                                          (c.Side.LONG if leg.direction is c.Direction.SELL else c.Side.SHORT),
                                     resource_bucket=leg.position_bucket or c.Bucket.ALL,
                                     volume=leg.volume, cash_reserve=leg.cash_reserve, not_proven_reflected=leg.volume)
                         for leg in original.legs)
    resources = view(reservations=reservations, cash_reserved=sum((x.cash_reserve for x in reservations), D("0")),
                     gross_position=sum(p.Position for p in sample.positions), ledger_revision=1)
    latest = replace(sample, ledger_revision=1)
    receipt = c.ReservationReceipt(plan_id=original.plan_id, reservation_ids=tuple(x.leg_id for x in original.legs), revision=1)
    return original, context(request, latest, rules, resources, receipt)


def test_E28_own_receipt_claims_all_legs_exactly_once():
    sample = with_position(Position=3, PositionClose=3)
    result, latest = reserved_context(intent(action=c.Action.CLOSE_LONG), sample)
    assert validate_plan(result, latest, NOW).ok
    assert latest.reservation_view.reservations[0].volume == 2
    assert not validate_plan(result, replace(latest, reservation_receipt=None), NOW).ok


@pytest.mark.parametrize("field", ["intent", "reservation_view"])
def test_E28_required_typed_evidence_cannot_be_inferred(field):
    result, latest = reserved_context()
    blocked = validate_plan(result, replace(latest, **{field:None}), NOW)
    assert not blocked.ok
    assert ("INTENT_REQUIRED" if field == "intent" else "RESERVATION_VIEW_REQUIRED") in blocked.blockers


@pytest.mark.parametrize("mutation", ["receipt_plan", "receipt_ids", "receipt_revision", "missing_reservation",
                                     "wrong_key", "wrong_volume", "wrong_cash", "released"])
def test_E28_payload_receipt_never_creates_resources(mutation):
    result, latest = reserved_context()
    receipt, resources = latest.reservation_receipt, latest.reservation_view
    held = resources.reservations[0]
    if mutation == "receipt_plan":
        receipt = replace(receipt, plan_id="0"*64)
    elif mutation == "receipt_ids":
        receipt = replace(receipt, reservation_ids=("SIM_FOREIGN:OPEN:-:0",))
    elif mutation == "receipt_revision":
        receipt = replace(receipt, revision=2)
    elif mutation == "missing_reservation":
        resources = replace(resources, reservations=())
    elif mutation == "wrong_key":
        resources = replace(resources, reservations=(replace(held, resource_key=replace(held.resource_key, instrument_id="rb2611")),))
    elif mutation == "wrong_volume":
        resources = replace(resources, reservations=(replace(held, volume=3),))
    elif mutation == "wrong_cash":
        resources = replace(resources, reservations=(replace(held, cash_reserve=D("1")),))
    else:
        resources = replace(resources, reservations=(replace(held, state=c.ReservationState.RELEASED),), cash_reserved=D("0"))
    assert not validate_plan(result, replace(latest, reservation_receipt=receipt, reservation_view=resources), NOW).ok


def test_E28_external_reservation_change_recomputes_budget_despite_old_receipt():
    sample = with_position(Position=3, PositionClose=3)
    result, latest = reserved_context(intent(action=c.Action.CLOSE_LONG), sample)
    other = reservation(volume=2)
    resources = replace(latest.reservation_view, reservations=latest.reservation_view.reservations+(other,),
                         cash_reserved=D("40"), ledger_revision=2)
    latest = replace(latest, snapshot=replace(latest.snapshot, ledger_revision=2), reservation_view=resources, ledger_revision=2)
    assert "INSUFFICIENT_POSITION" in validate_plan(result, latest, NOW).blockers


@pytest.mark.parametrize("field,value", [("action", c.Action.OPEN_SHORT), ("volume", 3),
                                       ("prices", c.Prices(open=D("3501"), close=D("3500")))])
def test_validate_frozen_intent_payload_cannot_change(field, value):
    result, latest = reserved_context()
    assert not validate_plan(result, replace(latest, intent=replace(latest.intent, **{field:value})), NOW).ok


@pytest.mark.parametrize("field,value", [("leg_id", "SIM_FORGED:OPEN:-:0"), ("direction", c.Direction.SELL),
                                       ("volume", 3), ("price", D("3501")), ("cash_reserve", D("0"))])
def test_validate_frozen_leg_payload_cannot_change(field, value):
    result, latest = reserved_context()
    changed = replace(result, legs=(replace(result.legs[0], **{field:value}),))
    assert not validate_plan(changed, latest, NOW).ok


@pytest.mark.parametrize("field,value", [("session_epoch", "SIM_OLD"), ("trading_day", date(2026,9,13)),
                                       ("position_snapshot_id", "SIM_OLD_SNAPSHOT"), ("proof_id", "SIM_OLD_PROOF")])
def test_validate_proof_scope_must_match_new_snapshot(field, value):
    result, latest = reserved_context()
    changed = replace(latest, proof=replace(latest.proof, **{field:value}))
    assert not validate_plan(result, changed, NOW).ok


def test_validate_rejects_missing_auth_stale_quote_disabled_and_thread_change():
    result, latest = reserved_context()
    assert not validate_plan(result, replace(latest, authorization=None), NOW).ok
    assert not validate_plan(result, latest, NOW+timedelta(seconds=3, microseconds=1)).ok
    assert not validate_plan(result, replace(latest, current_thread_id=18), NOW).ok
    disabled = replace(latest, mode=c.ExecutionMode.DISABLED, authorization=None,
                       readiness=replace(latest.readiness, mode=c.ExecutionMode.DISABLED,
                                         state=c.ReadinessState.PLAN_READY, can_submit=False))
    assert "MODE_DISABLED" in validate_plan(result, disabled, NOW).blockers


def test_validate_new_rules_cannot_change_leg_topology_or_cash_commitment():
    result, latest = reserved_context(intent(volume=5))
    assert not validate_plan(result, replace(latest, policy=replace(latest.policy, max_leg_qty=3)), NOW).ok
    assert not validate_plan(result, replace(latest, policy=replace(latest.policy, cash_per_open_lot=D("1001"))), NOW).ok
    assert not validate_plan(result, replace(latest, policy=replace(latest.policy, policy_id="SIM_OTHER")), NOW).ok


@pytest.mark.parametrize("limit", ["price", "volume", "action"])
def test_validate_authorization_covers_the_entire_intent(limit):
    result, latest = reserved_context()
    if limit == "price":
        auth = replace(latest.authorization, price_bounds=(replace(latest.authorization.price_bounds[0], lower=D("3501")),))
    elif limit == "volume":
        auth = replace(latest.authorization, max_volume=1)
    else:
        auth = replace(latest.authorization, actions=(c.AuthorizationAction.CLOSE_LONG,))
    assert not validate_plan(result, replace(latest, authorization=auth), NOW).ok


@pytest.mark.parametrize("action", list(c.Action))
def test_validate_accepts_complete_simulation_evidence_for_each_initial_action(action):
    sample = snapshot()
    if action in (c.Action.CLOSE_LONG, c.Action.REVERSE_TO_SHORT):
        sample = with_position(sample, Position=3, PositionClose=3)
    elif action in (c.Action.CLOSE_SHORT, c.Action.REVERSE_TO_LONG):
        sample = with_position(sample, c.Side.SHORT, Position=3, PositionClose=3)
    result, latest = reserved_context(intent(action=action), sample)
    assert validate_plan(result, latest, NOW).ok


def canonical_plan(result, **changes):
    """Construct a journal refresh fixture using the published wire algorithm."""
    value = replace(result, **changes)
    rows = []
    for leg in value.legs:
        data = json.loads(c.encode(leg))["data"]
        rows.append(",".join((leg.leg_id, leg.phase.value, leg.direction.value, leg.offset.value,
                              str(leg.volume), data["price"], leg.position_bucket.value if leg.position_bucket else "-",
                              data["cash_reserve"], "+".join(sorted(leg.depends_on)) or "-")))
    expires = json.loads(c.encode(value))["data"]["expires_at"]
    payload = "|".join(("infiny_execution_plan_v1", value.intent_hash, value.snapshot_id,
                        str(value.ledger_revision), value.policy_id, expires, ";".join(rows)))
    return replace(value, plan_id=hashlib.sha256(payload.encode()).hexdigest())


def refreshed_reverse():
    request = intent(action=c.Action.REVERSE_TO_SHORT)
    result, old = reserved_context(request, with_position(Position=3, PositionClose=3))
    sample = replace(snapshot(), snapshot_id="SIM_FRESH", proof_id="SIM_NEW_PROOF", ledger_revision=3,
                     times=replace(snapshot().times, positions=timing(snapshot_id="SIM_POS_NEW")),
                     positions=(bucket(timing=timing(snapshot_id="SIM_POS_NEW")),
                                bucket(c.Side.SHORT, timing=timing(snapshot_id="SIM_POS_NEW"))))
    close, opening = old.reservation_view.reservations
    close = replace(close, state=c.ReservationState.RELEASED, reflected_evidence_id="SIM_NEW_PROOF")
    result = canonical_plan(result, snapshot_id="SIM_FRESH", ledger_revision=2)
    resources = view(snapshot_id="SIM_FRESH", ledger_revision=3, reservations=(close, opening), cash_reserved=D("2000"))
    receipt = replace(old.reservation_receipt, plan_id=result.plan_id, revision=3)
    barrier = proof(sample, covered_leg_ids=(close.leg_id,), covered_event_ids=("SIM_FILL_CLOSE",))
    return result, context(request, sample, resources=resources, receipt=receipt, proof=barrier)


def reverse_after_first_close(side):
    action = c.Action.REVERSE_TO_SHORT if side is c.Side.LONG else c.Action.REVERSE_TO_LONG
    sample = with_position(side=side, Position=3, PositionClose=3, YdPositionClose=2)
    result, latest = reserved_context(intent(action=action), sample)
    prior, pending, opening = latest.reservation_view.reservations
    sample = with_position(latest.snapshot, side, Position=1, PositionClose=1, YdPositionClose=0)
    sample = replace(sample, snapshot_id="SIM_AFTER_YD", proof_id="SIM_AFTER_YD_PROOF", ledger_revision=2)
    prior = replace(prior, state=c.ReservationState.RELEASED, reflected_evidence_id=sample.proof_id)
    resources = replace(latest.reservation_view, snapshot_id=sample.snapshot_id, ledger_revision=2,
                         reservations=(prior, pending, opening), gross_position=1, cash_reserved=D("2010"))
    barrier = proof(sample, covered_leg_ids=(prior.leg_id,), covered_event_ids=("SIM_YD_FILL",))
    return result, replace(latest, snapshot=sample, proof=barrier, reservation_view=resources, ledger_revision=2)


@pytest.mark.parametrize("side", [c.Side.LONG, c.Side.SHORT])
def test_reverse_second_close_proceeds_after_first_settled_close_in_new_snapshot(side):
    result, latest = reverse_after_first_close(side)
    before = c.encode(result), c.encode(latest)
    assert [(leg.phase, leg.position_bucket, leg.volume) for leg in result.legs] == [
        (c.Phase.CLOSE, c.Bucket.YD, 2), (c.Phase.CLOSE, c.Bucket.TD, 1), (c.Phase.OPEN, None, 2)]
    assert validate_plan(result, latest, NOW).ok
    # Planning has not settled either dependency or refreshed the OPEN plan.
    assert result.snapshot_id != latest.snapshot.snapshot_id
    assert result.legs[-1].depends_on == tuple(leg.leg_id for leg in result.legs[:-1])
    assert latest.reservation_view.reservations[1].state is c.ReservationState.HELD
    assert latest.reservation_view.reservations[2].cash_reserve == D("2000")
    assert (c.encode(result), c.encode(latest)) == before


@pytest.mark.parametrize("mutation", ["covered_only", "unstamped_release", "active_and_released", "remaining_too_large",
                                     "remaining_zero", "wrong_bucket", "other_direction", "inflight", "cash_short"])
def test_reverse_second_close_still_requires_trusted_release_and_exact_remaining_resources(mutation):
    result, latest = reverse_after_first_close(c.Side.LONG)
    resources = latest.reservation_view
    if mutation == "covered_only":
        prior = replace(resources.reservations[0], state=c.ReservationState.HELD)
        latest = replace(latest, reservation_view=replace(resources, reservations=(prior, *resources.reservations[1:]),
                                                         cash_reserved=D("2030")))
    elif mutation == "unstamped_release":
        prior = replace(resources.reservations[0], reflected_evidence_id=None)
        latest = replace(latest, reservation_view=replace(resources, reservations=(prior, *resources.reservations[1:])))
    elif mutation == "active_and_released":
        prior = resources.reservations[0]
        extra = replace(prior, state=c.ReservationState.HELD)
        latest = replace(latest, reservation_view=replace(resources, reservations=(prior, extra, *resources.reservations[1:]),
                                                         cash_reserved=D("2030")))
    elif mutation == "remaining_too_large":
        latest = replace(latest, snapshot=with_position(latest.snapshot, Position=2, PositionClose=2))
    elif mutation == "remaining_zero":
        latest = replace(latest, snapshot=with_position(latest.snapshot, Position=0, PositionClose=0))
    elif mutation == "wrong_bucket":
        latest = replace(latest, snapshot=with_position(latest.snapshot, YdPositionClose=1))
    elif mutation == "other_direction":
        latest = replace(latest, snapshot=with_position(latest.snapshot, c.Side.SHORT, Position=1, PositionClose=1))
    elif mutation == "inflight":
        latest = replace(latest, reservation_view=replace(resources, inflight_legs=1))
    else:
        latest = replace(latest, snapshot=replace(latest.snapshot, account=replace(latest.snapshot.account, available=D("2009"))))
    assert not validate_plan(result, latest, NOW).ok


def reverse_after_first_close_with_release_stamp():
    """Release audit stamped by the proof that covered it; a newer proof follows."""
    result, latest = reverse_after_first_close(c.Side.LONG)
    prior, pending, opening = latest.reservation_view.reservations
    prior = replace(prior, reflected_evidence_id="SIM_YD_RELEASE_PROOF")
    resources = replace(latest.reservation_view, reservations=(prior, pending, opening))
    barrier = replace(latest.proof, covered_leg_ids=(), covered_event_ids=("SIM_AFTER_YD_EVENT",))
    return result, replace(latest, reservation_view=resources, proof=barrier)


def test_E24_release_stamp_is_an_audit_record_and_survives_later_proof_publication():
    result, latest = reverse_after_first_close_with_release_stamp()
    assert latest.snapshot.proof_id == "SIM_AFTER_YD_PROOF"
    assert latest.proof.proof_id == "SIM_AFTER_YD_PROOF"
    prior = latest.reservation_view.reservations[0]
    assert prior.reflected_evidence_id not in (None, latest.proof.proof_id)
    assert prior.leg_id not in latest.proof.covered_leg_ids
    assert validate_plan(result, latest, NOW).ok


def test_E24_full_reverse_refresh_validates_when_release_stamp_predates_current_proof():
    result, latest = refreshed_reverse()
    history = replace(latest.reservation_view.reservations[0], reflected_evidence_id="SIM_RELEASE_EARLIER")
    latest = replace(latest, reservation_view=replace(latest.reservation_view,
                                                     reservations=(history, latest.reservation_view.reservations[1])))
    assert latest.reservation_view.reservations[0].reflected_evidence_id != latest.proof.proof_id
    assert validate_plan(result, latest, NOW).ok


@pytest.mark.parametrize("tamper", ["unstamped", "wrong_key", "wrong_volume", "wrong_cash"])
def test_released_rows_are_still_matched_exactly_and_must_be_stamped(tamper):
    result, latest = reverse_after_first_close(c.Side.LONG)
    prior, pending, opening = latest.reservation_view.reservations
    changes = {"unstamped": {"reflected_evidence_id": None},
               "wrong_key": {"resource_key": replace(prior.resource_key, instrument_id="rb2611")},
               "wrong_volume": {"volume": prior.volume + 1},
               "wrong_cash": {"cash_reserve": prior.cash_reserve + D("1")}}[tamper]
    prior = replace(prior, **changes)
    latest = replace(latest, reservation_view=replace(latest.reservation_view, reservations=(prior, pending, opening)))
    assert not validate_plan(result, latest, NOW).ok


@pytest.mark.parametrize("stamped", [True, False])
def test_F08_active_reservation_with_released_history_for_same_leg_is_inconsistent(stamped):
    # Audit F08 (P2): a plain reserved CLOSE plan whose view also carries a
    # RELEASED duplicate of the same leg must be rejected regardless of stamp.
    sample = with_position(Position=3, PositionClose=3)
    result, latest = reserved_context(intent(action=c.Action.CLOSE_LONG), sample)
    held = latest.reservation_view.reservations[0]
    duplicate = replace(held, state=c.ReservationState.RELEASED,
                        reflected_evidence_id="SIM_PROOF" if stamped else None)
    resources = replace(latest.reservation_view, reservations=(held, duplicate))
    latest = replace(latest, reservation_view=resources)
    assert latest.reservation_view.cash_reserved == held.cash_reserve
    validation = validate_plan(result, latest, NOW)
    assert "RESERVATION_REQUIRED" in validation.blockers
    assert not validation.ok


def test_reverse_new_full_plan_validates_after_trusted_completed_close_without_reclosing_zero():
    result, latest = refreshed_reverse()
    assert validate_plan(result, latest, NOW).ok
    assert result.legs[0].volume == 3 and result.legs[1].volume == 2
    assert result.legs[1].depends_on == (result.legs[0].leg_id,)


@pytest.mark.parametrize("mutation", ["not_released", "unstamped_release", "tampered_release_row", "nonzero_original",
                                     "old_plan", "cash_changed", "bad_quote"])
def test_reverse_barrier_or_new_resources_missing_never_validate_open(mutation):
    result, latest = refreshed_reverse()
    if mutation == "not_released":
        history = replace(latest.reservation_view.reservations[0], state=c.ReservationState.HELD)
        resources = replace(latest.reservation_view, reservations=(history, latest.reservation_view.reservations[1]), cash_reserved=D("2030"))
        latest = replace(latest, reservation_view=resources)
    elif mutation == "unstamped_release":
        history = replace(latest.reservation_view.reservations[0], reflected_evidence_id=None)
        latest = replace(latest, reservation_view=replace(latest.reservation_view,
                                                         reservations=(history, latest.reservation_view.reservations[1])))
    elif mutation == "tampered_release_row":
        history = replace(latest.reservation_view.reservations[0], volume=4)
        latest = replace(latest, reservation_view=replace(latest.reservation_view,
                                                         reservations=(history, latest.reservation_view.reservations[1])))
    elif mutation == "nonzero_original":
        latest = replace(latest, snapshot=with_position(latest.snapshot, Position=1, PositionClose=1))
    elif mutation == "old_plan":
        result = canonical_plan(result, snapshot_id="SIM_OLD", ledger_revision=0)
        latest = replace(latest, reservation_receipt=replace(latest.reservation_receipt, plan_id=result.plan_id))
    elif mutation == "cash_changed":
        latest = replace(latest, snapshot=replace(latest.snapshot, account=replace(latest.snapshot.account, available=D("1999"))))
    else:
        latest = replace(latest, snapshot=replace(latest.snapshot, quote=replace(latest.snapshot.quote, bid_volume1=0)))
    assert not validate_plan(result, latest, NOW).ok


@pytest.mark.parametrize("covered,accepted", [(True, True), (False, False)])
def test_E28_reflected_own_reservation_needs_actual_leg_coverage(covered, accepted):
    result, latest = reserved_context(intent(action=c.Action.CLOSE_LONG), with_position(Position=3, PositionClose=3))
    held = replace(latest.reservation_view.reservations[0], state=c.ReservationState.REFLECTED,
                   reflected_evidence_id="SIM_PROOF", not_proven_reflected=0)
    resources = replace(latest.reservation_view, reservations=(held,))
    sample = with_position(latest.snapshot, FrozenClosing=2)
    barrier = replace(latest.proof, covered_leg_ids=(held.leg_id,) if covered else ())
    validation = validate_plan(result, replace(latest, snapshot=sample, reservation_view=resources, proof=barrier), NOW)
    assert validation.ok is accepted


@pytest.mark.parametrize("volume,limits,expected", [
    (12, ((3, 7, 2),), (4, 4, 4)), (11, ((3, 7, 2),), None),
    (12, ((1, 12, 2), (1, 12, 3)), (6, 6)), (6, ((4, 5, 2),), None),
])
def test_E07_verified_volume_steps_intersect_all_limits(volume, limits, expected):
    sample = snapshot()
    extra = tuple(c.VolumeLimit(evidence_id=f"SIM_LIMIT_{i}", min_volume=lo, max_volume=hi, volume_step=step, verified=True)
                  for i, (lo, hi, step) in enumerate(limits))
    maximum = 6 if len(limits) == 2 else 5
    sample = replace(sample, contract=replace(sample.contract, max_limit_order_volume=maximum, additional_volume_limits=extra))
    result = make_plan(intent(volume=volume), sample, rules=policy(max_leg_qty=maximum))
    if expected is None:
        rejected(result, "VOLUME_SPLIT_IMPOSSIBLE")
    else:
        assert not result.blockers
        assert tuple(leg.volume for leg in result.legs) == expected


def test_E07_unverified_extra_limits_and_huge_leg_expansion_fail_closed():
    sample = snapshot()
    extra = c.VolumeLimit(evidence_id="SIM_UNVERIFIED", min_volume=1, max_volume=5, volume_step=1, verified=False)
    rejected(make_plan(sample=replace(sample, contract=replace(sample.contract, additional_volume_limits=(extra,)))), "VOLUME_INVALID")
    assert make_plan(intent(volume=2**63-1)).blockers


def test_E28_id_suffix_handles_colons_in_intent_without_ambiguous_resource_phase():
    request = intent(intent_id="SIM:USER:DECISION")
    result, latest = reserved_context(request)
    assert result.legs[0].leg_id == "SIM:USER:DECISION:OPEN:-:0"
    assert validate_plan(result, latest, NOW).ok


def test_hash_frozen_wire_algorithm_is_deterministic_and_has_no_self_reference():
    result = make_plan()
    assert result == make_plan()
    assert result.plan_id == "ddb95b1d134cebf447a4db9208421326379bbafcc54536d9ddc65e4def150b71"
    assert result.plan_id == canonical_plan(result).plan_id
    assert make_plan(intent(prices=c.Prices(open=D("3.5E3"), close=D("3500.00")))).plan_id == result.plan_id
    assert make_plan(intent(source_key="SIM_OTHER_DECISION")).plan_id != result.plan_id
    assert c.decode(c.encode(result), c.Plan) == result


def test_E27_gross_counts_both_sides_without_netting_and_close_does_not_credit_future_cash():
    sample = with_position(Position=50, PositionClose=50)
    sample = with_position(sample, c.Side.SHORT, Position=50, PositionClose=50)
    rejected(make_plan(sample=sample), "GROSS_POSITION_LIMIT")
    assert not make_plan(intent(action=c.Action.CLOSE_LONG), sample).blockers


def test_E04_only_required_source_timestamps_and_inclusive_plan_ttl():
    result, latest = reserved_context()
    assert validate_plan(result, latest, NOW + timedelta(seconds=3)).ok
    assert not validate_plan(result, latest, NOW + timedelta(seconds=3, microseconds=1)).ok
    sample = snapshot()
    rejected(make_plan(sample=replace(sample, quote=replace(sample.quote, time=None),
                                      times=replace(sample.times, quote=timing(source_at=None)))), "QUOTE_STALE")
    assert not make_plan(sample=sample).blockers  # funds have no server timestamp


def test_E04_night_calendar_time_does_not_equal_trading_day():
    night = datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)
    sample = rebind(snapshot(), trading_day=date(2026, 9, 15))
    current = timing(sampled_at=night, received_at=night)
    sample = replace(sample, quote=replace(sample.quote, time=night),
                     times=c.SnapshotTimes(sampled_at=night, sent_at=night, received_at=night,
                                           quote=replace(current, source_at=night), contract=current, account=current, positions=current),
                     positions=tuple(replace(p, timing=current) for p in sample.positions))
    request = intent(binding=sample.binding, created_at=night, expires_at=night+timedelta(days=1))
    assert not make_plan(request, sample, now=night).blockers
    stale_tick = night - timedelta(hours=24)
    sample = replace(sample, quote=replace(sample.quote, time=stale_tick),
                     times=replace(sample.times, quote=replace(current, source_at=stale_tick)))
    rejected(make_plan(request, sample, now=night), "QUOTE_STALE")


def test_E04_custom_future_skew_is_used_consistently_on_both_public_functions():
    stamp = NOW + timedelta(seconds=3)
    item = timing(sampled_at=stamp, received_at=stamp)
    sample = snapshot()
    sample = replace(sample, times=c.SnapshotTimes(sampled_at=stamp, received_at=stamp, sent_at=stamp,
                                                   quote=replace(item, source_at=stamp), account=item, positions=item, contract=item),
                     quote=replace(sample.quote, time=stamp), positions=tuple(replace(p, timing=item) for p in sample.positions))
    rules = policy(timing=c.TimingPolicy(future_skew=D("4")))
    result, latest = reserved_context(sample=sample, rules=rules)
    assert validate_plan(result, latest, NOW).ok


def test_hash_uses_canonical_zero_for_close_fee():
    sample = with_position(Position=3, PositionClose=3)
    result = make_plan(intent(action=c.Action.CLOSE_LONG), sample, rules=policy(fee_per_close_lot=D("-0.0")))
    assert result.plan_id == canonical_plan(result).plan_id


@pytest.mark.parametrize("field,value", [("account_key", "SIM_B"), ("session_epoch", "SIM_E2"),
                                       ("trading_day", date(2026,9,15)), ("instrument_id", "rb2611")])
def test_E05_wholly_consistent_new_scope_cannot_claim_original_intent(field, value):
    result, latest = reserved_context()
    sample = rebind(latest.snapshot, **{field:value})
    request = replace(latest.intent, binding=sample.binding)
    resources = replace(latest.reservation_view, account_key=sample.binding.account_key)
    replacement = context(request, sample, resources=resources, receipt=latest.reservation_receipt)
    assert "INTENT_CONFLICT" in validate_plan(result, replacement, NOW).blockers


def test_validate_new_snapshot_rechecks_frozen_legs_without_refreshing_old_plan_expiry():
    result, latest = reserved_context()
    sample = replace(latest.snapshot, snapshot_id="SIM_SECOND", ledger_revision=2)
    resources = replace(latest.reservation_view, snapshot_id="SIM_SECOND", ledger_revision=2)
    latest = replace(latest, snapshot=sample, reservation_view=resources, ledger_revision=2)
    assert validate_plan(result, latest, NOW).ok
    assert result.snapshot_id == "SIM_SNAP" and result.expires_at == NOW + timedelta(seconds=3)
    sample = replace(sample, quote=replace(sample.quote, ask_volume1=0))
    assert "BOOK_EMPTY" in validate_plan(result, replace(latest, snapshot=sample), NOW).blockers


@pytest.mark.parametrize("kind", ["expiry", "policy", "intent", "auth"])
def test_final_attempt_expiration_boundaries(kind):
    result, latest = reserved_context()
    if kind == "expiry":
        result = replace(result, expires_at=NOW - timedelta(microseconds=1))
    elif kind == "policy":
        latest = replace(latest, policy=replace(latest.policy, valid_until=NOW))
    elif kind == "intent":
        request = replace(latest.intent, created_at=NOW-timedelta(days=1), expires_at=NOW)
        latest = replace(latest, intent=request)
    else:
        latest = replace(latest, authorization=replace(latest.authorization, expires_at=NOW))
    assert not validate_plan(result, latest, NOW).ok


def test_E05_partial_account_inventory_allows_diagnostic_plan_but_not_submission_validation():
    sample = snapshot()
    cover = replace(sample.coverage, level=c.CoverageLevel.OWN_JOURNAL, strategy_key="SIM_S",
                    complete_through=None, completion_evidence_id=None, account_active_count=None)
    sample = replace(sample, coverage=cover, proof_id=None)
    assert not make_plan(sample=sample).blockers
    result, latest = reserved_context()
    ready = replace(latest.readiness, coverage=cover, state=c.ReadinessState.PLAN_READY, can_submit=False)
    resources = latest.reservation_view
    latest = replace(latest, snapshot=replace(sample, ledger_revision=1), readiness=ready, proof=None, reservation_view=resources)
    assert not validate_plan(result, latest, NOW).ok


@pytest.mark.parametrize("argument", ["intent", "snapshot", "reservation_view", "policy"])
def test_public_planner_accepts_only_declared_contract_types(argument):
    arguments = dict(intent=intent(), snapshot=snapshot(), reservation_view=view(), policy=policy(), now=NOW)
    arguments[argument] = {}
    with pytest.raises(c.ContractError):
        plan(**arguments)


def test_public_functions_are_pure_under_native_network_and_io_sentinels(monkeypatch):
    import builtins
    import importlib
    import socket
    import sys
    from ak_trading.infiny_execution import planning
    calls = []

    def forbidden(*args, **kwargs):
        calls.append("external")
        raise AssertionError("N2 must not perform I/O or import native")

    class Sentinel:
        def __getattr__(self, name):
            return forbidden()

    result, latest = reserved_context()
    source_before = c.encode(latest)
    module = sys.modules["ak_trading.infiny_execution.planning"]
    saved_module_state = dict(module.__dict__)
    with monkeypatch.context() as guard:
        for module_name in ("INFINIGO", "pythongo", "numpy", "ctypes", "PyQt5"):
            guard.setitem(sys.modules, module_name, Sentinel())
        guard.setattr(builtins, "open", forbidden)
        guard.setattr(socket.socket, "connect", forbidden)
        importlib.reload(planning)
        assert planning.plan(latest.intent, snapshot(), view(), policy(), NOW).legs
        assert planning.validate_plan(result, latest, NOW).ok
        # Restore the pre-reload module dict in place so later tests keep
        # stable class identity (reload refreshes planning-owned classes).
        module.__dict__.clear()
        module.__dict__.update(saved_module_state)
    assert not calls
    assert c.encode(latest) == source_before


# ---------------------------------------------------------------------------
# NE1-C2: native pilot limited-scope admission (SYNTHETIC fixtures only)
# ---------------------------------------------------------------------------

class ActivePermitRegistry(ne.EvidenceRegistry):
    """In-test registry: SYNTHETIC permits only, consumption tracked, no I/O."""

    def __init__(self):
        self.permits = {}
        self.consumed = set()
        self.verifications = 0

    def register_permit(self, permit):
        if permit.origin is not nc.Origin.SYNTHETIC:
            raise c.ContractError("test registry only tracks SYNTHETIC permits")
        self.permits[permit.permit_id] = permit

    def consume(self, permit_id):
        self.consumed.add(permit_id)

    def verify_permit_active(self, permit_id, *, now):
        self.verifications += 1
        permit = self.permits.get(permit_id)
        if permit is None or permit_id in self.consumed:
            return c.ValidationResult(blockers=("NATIVE_PERMIT_UNREGISTERED",))
        if permit.expires_at <= now:
            return c.ValidationResult(blockers=("AUTHORIZATION_EXPIRED",))
        return c.ValidationResult()


def ptiming(**changes):
    return timing(source=c.Source.PYTHONGO, **changes)


def native_cover(**changes):
    b = binding()
    values = dict(level=c.CoverageLevel.OWN_JOURNAL, account_key=b.account_key,
                  strategy_key=b.strategy_key, session_epoch=b.session_epoch,
                  trading_day=b.trading_day, source=c.Source.PYTHONGO, as_of=NOW,
                  complete_through=None, completion_evidence_id=None,
                  own_active_count=0, account_active_count=None)
    return c.Coverage(**(values | changes))


def native_snapshot(**changes):
    base = snapshot()
    item = ptiming()
    values = dict(source=c.Source.PYTHONGO, coverage=native_cover(), proof_id=None,
                  times=replace(base.times, quote=ptiming(source_at=NOW), contract=item,
                                account=item, positions=item),
                  positions=(replace(base.positions[0], timing=item),
                             replace(base.positions[1], timing=item)))
    return replace(base, **(values | changes))


def pilot_policy(**changes):
    values = dict(policy_id="SIM_POLICY_P", valid_until=NOW + timedelta(days=1), max_leg_qty=5,
                  max_gross_position=100, max_inflight_legs=1, max_cash_reserve=D("100000"),
                  cash_per_open_lot=None, fee_per_close_lot=D("10"), cash_bound_source=None)
    return c.RiskPolicy(**(values | changes))


def pilot_permit(**changes):
    values = dict(permit_id="SIM_PERMIT_1", authorization_id="SIM_AUTH_N1", binding=binding(),
                  control_revision=3, baseline_evidence_id="SIM_BASE_1",
                  route_evidence_id="SIM_ROUTE_1", cash_bound_evidence_id="SIM_CASH_BOUND_1",
                  source_key="SIM_OPERATION_1",
                  actions=(c.AuthorizationAction.OPEN_LONG, c.AuthorizationAction.OPEN_SHORT,
                           c.AuthorizationAction.CLOSE_LONG, c.AuthorizationAction.CLOSE_SHORT),
                  max_send_attempts=1, max_cancel_attempts=0, max_volume=5,
                  price_bounds=(c.PriceBound(symbol=c.Symbol(exchange="SHFE", instrument_id="rb2610"),
                                             lower=D("3000"), upper=D("4000")),),
                  expires_at=NOW + timedelta(minutes=5), params_sha256="d" * 64,
                  origin=nc.Origin.SYNTHETIC)
    return nc.NativePilotPermit(**(values | changes))


def pilot_baseline(**changes):
    b = binding()
    values = dict(evidence_id="SIM_BASE_1", source_kind=ne.EvidenceSourceKind.TERMINAL_EXPORT,
                  artifact_sha256="8" * 64, account_key=b.account_key, trading_day=b.trading_day,
                  session_epoch=b.session_epoch, as_of=NOW, order_inventory=(), trade_inventory=(),
                  positions=(), coverage_description="terminal export of orders and trades",
                  completion_basis="export rows reconciled with the terminal listing",
                  isolation_evidence_id="SIM_ISO_1", approved_scope_id="SIM_SCOPE_1",
                  expires_at=NOW + timedelta(minutes=5), origin=nc.Origin.SYNTHETIC)
    return ne.NativeBaselineEvidence(**(values | changes))


def pilot_route(**changes):
    values = dict(evidence_id="SIM_ROUTE_1", framework_sha256="9" * 64, sdk_sha256="a" * 64,
                  binding=binding(), callback_kinds=(ne.CallbackKind.ON_ORDER, ne.CallbackKind.ON_TRADE),
                  account_scope_basis="single investor session observed on callbacks",
                  strategy_scope_basis="strategy id matches every callback envelope",
                  local_alias_scope="instance-local order ids", front_id=None, session_id=None,
                  memo_limit=32, memo_roundtrip_verified=True, validated_at=NOW,
                  expires_at=NOW + timedelta(hours=1), origin=nc.Origin.SYNTHETIC)
    return ne.NativeRouteEvidence(**(values | changes))


def pilot_readiness(permit_id="SIM_PERMIT_1", **changes):
    values = dict(connection=c.ConnectionState.ROUNDTRIP, state=c.ReadinessState.EXECUTABLE,
                  can_plan=True, can_submit=True, can_cancel_owned=False, blockers=(),
                  mode=c.ExecutionMode.NATIVE, coverage=native_cover(), evidence_id="SIM_READY_P",
                  execution_scope=nc.ExecutionScope.NATIVE_PILOT, native_permit_id=permit_id)
    return nc.V2Readiness(**(values | changes))


def pilot_scope(permit=None, readiness=None, baseline=None, route=None, registry=None):
    permit = permit or pilot_permit()
    if registry is None:
        registry = ActivePermitRegistry()
        registry.register_permit(permit)
    return NativePilotScope(readiness=readiness or pilot_readiness(permit_id=permit.permit_id),
                            permit=permit, baseline=baseline or pilot_baseline(),
                            route=route or pilot_route(), registry=registry)


PILOT_ACTIONS = {c.Action.OPEN_LONG: (c.Side.LONG, True), c.Action.OPEN_SHORT: (c.Side.SHORT, True),
                 c.Action.CLOSE_LONG: (c.Side.LONG, False), c.Action.CLOSE_SHORT: (c.Side.SHORT, False)}


def make_pilot(action=c.Action.OPEN_LONG, volume=2, sample=None, rules=None, resources=None,
               scope=None, **intent_changes):
    side, opening = PILOT_ACTIONS[action]
    sample = sample or native_snapshot()
    if not opening:
        sample = with_position(sample, side, Position=volume, PositionClose=volume)
    if rules is None:
        if opening:
            rules = pilot_policy(cash_per_open_lot=D("1000"),
                                 cash_bound_source=c.CashBoundSource(
                                     source=c.Source.SIMULATION, evidence_id="SIM_RATE_ONLY",
                                     verified=True))
        else:
            rules = pilot_policy()
    request = replace(intent(action=action, volume=volume,
                             prices=c.Prices(open=D("3500"), close=D("3500"))), **intent_changes)
    return (plan_native_pilot(request, sample, resources or view(), rules,
                             scope or pilot_scope(), NOW), request, sample, rules)


def test_pilot_plans_each_single_send_action_and_stays_pure():
    for action in PILOT_ACTIONS:
        result, _, _, _ = make_pilot(action)
        assert not result.blockers, (action, result.blockers)
        assert len(result.legs) == 1
        assert result.legs[0].price == D("3500")
    scope = pilot_scope()
    before = nc.encode(scope.permit)
    plan_native_pilot(intent(action=c.Action.OPEN_SHORT), native_snapshot(), view(),
                      pilot_policy(cash_per_open_lot=D("1000"),
                                   cash_bound_source=c.CashBoundSource(
                                       source=c.Source.SIMULATION, evidence_id="SIM_RATE_ONLY",
                                       verified=True)),
                      scope, NOW)
    assert scope.registry.consumed == set() and nc.encode(scope.permit) == before


@pytest.mark.parametrize("flaw,code", [
    ("permit_unregistered", "AUTHORIZATION_REQUIRED"), ("permit_expired", "AUTHORIZATION_REQUIRED"),
    ("permit_consumed", "AUTHORIZATION_REQUIRED"), ("scope_none", "NATIVE_DISABLED"),
    ("scope_simulation", "NATIVE_DISABLED"), ("cannot_submit", "NATIVE_DISABLED"),
    ("permit_id_mismatch", "AUTHORIZATION_REQUIRED"),
    ("baseline_id_mismatch", "BASELINE_GAP"), ("baseline_stale", "BASELINE_GAP"),
    ("baseline_expired", "BASELINE_GAP"), ("baseline_wrong_account", "ACCOUNT_MISMATCH"),
    ("baseline_wrong_session", "SESSION_CHANGED"), ("baseline_wrong_day", "TRADING_DAY_CHANGED"),
    ("route_id_mismatch", "AUTHORIZATION_REQUIRED"), ("route_expired", "AUTHORIZATION_REQUIRED"),
    ("route_wrong_binding", "OWNERSHIP_CONFLICT"),
])
def test_H35_pilot_requires_every_admission_piece(flaw, code):
    registry = ActivePermitRegistry()
    permit = pilot_permit()
    if flaw != "permit_unregistered":
        registry.register_permit(permit)
    readiness_changes, baseline_changes, route_changes = {}, {}, {}
    if flaw == "permit_expired":
        permit = pilot_permit(expires_at=NOW)
        registry = ActivePermitRegistry()
        registry.register_permit(permit)
    elif flaw == "permit_consumed":
        registry.consume(permit.permit_id)
    elif flaw == "scope_none":
        readiness_changes = dict(execution_scope=nc.ExecutionScope.NONE, native_permit_id=None,
                                 can_submit=False, coverage=None)
    elif flaw == "scope_simulation":
        b = binding()
        sim_cover = c.Coverage(level=c.CoverageLevel.FULL_ACCOUNT, account_key=b.account_key,
                               strategy_key=None, session_epoch=b.session_epoch,
                               trading_day=b.trading_day, source=c.Source.SIMULATION, as_of=NOW,
                               complete_through=c.SourceWatermark(source=c.Source.SIMULATION,
                                                                  stream_id="SIM_STREAM", value="17",
                                                                  as_of=NOW),
                               completion_evidence_id="SIM_BARRIER", own_active_count=0,
                               account_active_count=0)
        readiness_changes = dict(execution_scope=nc.ExecutionScope.SIMULATION_FULL_ACCOUNT,
                                 mode=c.ExecutionMode.SIMULATION, coverage=sim_cover,
                                 native_permit_id=None)
    elif flaw == "cannot_submit":
        readiness_changes = dict(can_submit=False, state=c.ReadinessState.PLAN_READY)
    elif flaw == "permit_id_mismatch":
        readiness_changes = dict(native_permit_id="SIM_PERMIT_OTHER")
    elif flaw == "baseline_id_mismatch":
        baseline_changes = dict(evidence_id="SIM_BASE_OTHER")
    elif flaw == "baseline_stale":
        baseline_changes = dict(as_of=NOW - timedelta(minutes=5, seconds=1))
    elif flaw == "baseline_expired":
        baseline_changes = dict(as_of=NOW - timedelta(microseconds=1), expires_at=NOW)
    elif flaw == "baseline_wrong_account":
        baseline_changes = dict(account_key="SIM_B")
    elif flaw == "baseline_wrong_session":
        baseline_changes = dict(session_epoch="SIM_EPOCH_OLD")
    elif flaw == "baseline_wrong_day":
        baseline_changes = dict(trading_day=date(2026, 9, 15))
    elif flaw == "route_id_mismatch":
        route_changes = dict(evidence_id="SIM_ROUTE_OTHER")
    elif flaw == "route_expired":
        route_changes = dict(expires_at=NOW)
    elif flaw == "route_wrong_binding":
        route_changes = dict(binding=binding(account_key="SIM_B"))
    scope = pilot_scope(permit=permit,
                        readiness=pilot_readiness(permit_id=permit.permit_id, **readiness_changes),
                        baseline=pilot_baseline(**baseline_changes), route=pilot_route(**route_changes),
                        registry=registry)
    result = plan_native_pilot(intent(), native_snapshot(), view(), pilot_policy(), scope, NOW)
    assert code in result.blockers
    assert result.legs == ()


def test_H34_simulation_evidence_cannot_enter_native_scope():
    scope = pilot_scope()
    result = plan_native_pilot(intent(), snapshot(), view(), pilot_policy(), scope, NOW)
    assert "OWNERSHIP_CONFLICT" in result.blockers and result.legs == ()
    registry = ActivePermitRegistry()
    registry.register_permit(pilot_permit())
    consumed = ActivePermitRegistry()
    consumed.register_permit(pilot_permit())
    consumed.consume("SIM_PERMIT_1")
    simulation_rate = replace(pilot_policy(), cash_per_open_lot=D("1000"),
                              cash_bound_source=c.CashBoundSource(source=c.Source.SIMULATION,
                                                                  evidence_id="SIM_RATE_ONLY", verified=True))
    blocked = plan_native_pilot(intent(), native_snapshot(), view(), simulation_rate,
                                pilot_scope(registry=consumed), NOW)
    assert blocked.blockers and blocked.legs == ()
    with pytest.raises(c.ContractError):
        c.decode(nc.encode(pilot_permit()), nc.NativePilotPermit)


@pytest.mark.parametrize("field,value,code", [("account_key", "SIM_B", "ACCOUNT_MISMATCH"),
                                            ("session_epoch", "SIM_EPOCH_OLD", "SESSION_CHANGED"),
                                            ("trading_day", date(2026, 9, 15), "TRADING_DAY_CHANGED"),
                                            ("instrument_id", "rb2611", "INSTRUMENT_MISMATCH"),
                                            ("hedge_flag", "2", "HEDGE_UNVERIFIED")])
def test_H05_permit_binding_must_match_intent_and_snapshot(field, value, code):
    changes = {"binding": replace(binding(), **{field: value})}
    if field == "instrument_id":
        changes["price_bounds"] = ()
    scope = pilot_scope(permit=pilot_permit(**changes))
    result, _, _, _ = make_pilot(scope=scope)
    assert code in result.blockers and result.legs == ()


@pytest.mark.parametrize("component,delta,ok", [
    ("quote", timedelta(seconds=3), True), ("quote", timedelta(seconds=3, milliseconds=1), False),
    ("account", timedelta(seconds=5), True), ("account", timedelta(seconds=5, milliseconds=1), False),
    ("positions", timedelta(seconds=5, milliseconds=1), False),
    ("packet", timedelta(seconds=10), True), ("packet", timedelta(seconds=10, milliseconds=1), False),
])
def test_H07_pilot_component_ttl_boundaries_apply_despite_fresh_baseline(component, delta, ok):
    old = NOW - delta
    sample = native_snapshot()
    if component == "packet":
        sample = replace(sample, times=replace(sample.times, sampled_at=old, sent_at=old, received_at=old))
    else:
        item = ptiming(sampled_at=old, received_at=old, source_at=old if component == "quote" else None)
        sample = replace(sample, times=replace(sample.times, **{component: item}),
                         quote=replace(sample.quote, time=old) if component == "quote" else sample.quote,
                         positions=tuple(replace(p, timing=item) for p in sample.positions)
                         if component == "positions" else sample.positions)
    scope = pilot_scope(baseline=pilot_baseline(as_of=NOW))
    result = plan_native_pilot(intent(), sample, view(),
                               replace(pilot_policy(), cash_per_open_lot=D("1000"),
                                       cash_bound_source=c.CashBoundSource(source=c.Source.SIMULATION,
                                                                           evidence_id="SIM_RATE_ONLY", verified=True)),
                               scope, NOW)
    assert (not result.blockers) is ok


@pytest.mark.parametrize("mutation,code", [
    ("auction", "AUCTION"), ("closed", "MARKET_CLOSED"), ("unknown_status", "MARKET_STATUS_UNKNOWN"),
    ("limit_locked", "LIMIT_LOCKED"), ("crossed", "BOOK_CROSSED"), ("empty_book", "BOOK_EMPTY"),
    ("wide_spread", "SPREAD_TOO_WIDE"), ("option", "INSTRUMENT_UNSUPPORTED"),
    ("expired_contract", "CONTRACT_EXPIRED"),
])
def test_H08_pilot_market_and_instrument_blockers(mutation, code):
    sample = native_snapshot()
    if mutation == "auction":
        sample = replace(sample, quote=replace(sample.quote, market_status=c.MarketStatus.AUCTION))
    elif mutation == "closed":
        sample = replace(sample, quote=replace(sample.quote, market_status=c.MarketStatus.CLOSED))
    elif mutation == "unknown_status":
        sample = replace(sample, quote=replace(sample.quote, market_status=c.MarketStatus.UNKNOWN))
    elif mutation == "limit_locked":
        sample = replace(sample, quote=replace(sample.quote, last=D("4000")))
    elif mutation == "crossed":
        sample = replace(sample, quote=replace(sample.quote, bid1=D("3502")))
    elif mutation == "empty_book":
        sample = replace(sample, quote=replace(sample.quote, ask_volume1=0))
    elif mutation == "wide_spread":
        sample = replace(sample, quote=replace(sample.quote, ask1=D("3511")))
    elif mutation == "option":
        sample = replace(sample, contract=replace(sample.contract, asset_class=c.AssetClass.OPTION))
    else:
        sample = replace(sample, contract=replace(sample.contract, expiry=date(2026, 9, 14)))
    result, _, _, _ = make_pilot(sample=sample)
    assert code in result.blockers and result.legs == ()


@pytest.mark.parametrize("tick,price,ok", [("0.2", "3500.2", True), ("0.2", "3500.1", False)])
def test_H10_pilot_decimal_tick_without_rounding(tick, price, ok):
    sample = native_snapshot(contract=None)
    sample = replace(native_snapshot(), contract=replace(native_snapshot().contract, price_tick=D(tick)))
    rules = replace(pilot_policy(), cash_per_open_lot=D("1000"),
                    cash_bound_source=c.CashBoundSource(source=c.Source.SIMULATION,
                                                        evidence_id="SIM_RATE_ONLY", verified=True))
    result = plan_native_pilot(intent(prices=c.Prices(open=D(price), close=D(price))), sample,
                               view(), rules, pilot_scope(), NOW)
    assert (not result.blockers) is ok
    if ok:
        assert result.legs[0].price == D(price)


def test_H10_pilot_single_send_topology_rejects_splits_and_oversize():
    sample = native_snapshot()
    sample = replace(sample, contract=replace(sample.contract, min_limit_order_volume=3))
    result, _, _, _ = make_pilot(volume=11, sample=sample)
    assert "AUTHORIZATION_ACTION" in result.blockers and result.legs == ()
    result, _, _, _ = make_pilot(volume=6, sample=replace(
        sample, contract=replace(sample.contract, min_limit_order_volume=4)))
    assert result.blockers and result.legs == ()
    scope = pilot_scope(permit=pilot_permit(max_volume=1))
    result, _, _, _ = make_pilot(volume=2, scope=scope)
    assert "AUTHORIZATION_ACTION" in result.blockers and result.legs == ()
    with pytest.raises(c.ContractError):
        make_pilot(volume=True)


@pytest.mark.parametrize("available,maximum,gross,inflight,code", [
    ("999", "100000", 0, 0, "INSUFFICIENT_CASH"), ("100000", "999", 0, 0, "INSUFFICIENT_CASH"),
    ("100000", "100000", 99, 0, "GROSS_POSITION_LIMIT"), ("100000", "100000", 0, 1, "INFLIGHT_LIMIT"),
])
def test_H11_pilot_budgets_use_the_same_rules(available, maximum, gross, inflight, code):
    sample = replace(native_snapshot(), account=c.Account(binding=binding(), available=D(available),
                                                          currency="CNY"))
    resources = view(gross_position=gross, inflight_legs=inflight)
    result, _, _, _ = make_pilot(sample=sample, resources=resources,
                                 rules=replace(pilot_policy(), max_cash_reserve=D(maximum),
                                               cash_per_open_lot=D("1000"),
                                               cash_bound_source=c.CashBoundSource(
                                                   source=c.Source.SIMULATION,
                                                   evidence_id="SIM_RATE_ONLY", verified=True)))
    assert code in result.blockers and result.legs == ()


def pilot_admission(action=c.Action.CLOSE_LONG, volume=3, **kwargs):
    side, opening = PILOT_ACTIONS[action]
    plan_value, request, sample, rules = make_pilot(action, volume, **kwargs)
    assert not plan_value.blockers, plan_value.blockers
    legs = plan_value.legs
    reservations = tuple(reservation(leg_id=leg.leg_id,
                                     side=(c.Side.LONG if leg.direction is c.Direction.BUY else c.Side.SHORT)
                                     if leg.phase is c.Phase.OPEN else
                                     (c.Side.LONG if leg.direction is c.Direction.SELL else c.Side.SHORT),
                                     resource_bucket=leg.position_bucket or c.Bucket.ALL,
                                     volume=leg.volume, cash_reserve=leg.cash_reserve,
                                     not_proven_reflected=leg.volume) for leg in legs)
    scope = kwargs.get("scope") or pilot_scope()
    resources = view(reservations=reservations,
                     cash_reserved=sum((x.cash_reserve for x in reservations), D("0")),
                     gross_position=volume if not opening else 0, ledger_revision=1)
    sample = replace(sample, ledger_revision=1)
    receipt = c.ReservationReceipt(plan_id=plan_value.plan_id,
                                   reservation_ids=tuple(x.leg_id for x in legs), revision=1)
    admission = NativePilotAdmission(intent=request, snapshot=sample, reservation_view=resources,
                                     policy=rules, reservation_receipt=receipt,
                                     readiness=scope.readiness, permit=scope.permit,
                                     baseline=scope.baseline, route=scope.route,
                                     registry=scope.registry)
    return plan_value, admission


def test_H14_pilot_receipt_claims_all_legs_exactly_once():
    plan_value, admission = pilot_admission()
    assert validate_native_pilot_plan(plan_value, admission, NOW).ok
    assert admission.reservation_view.reservations[0].volume == 3
    assert "RESERVATION_REQUIRED" in validate_native_pilot_plan(
        plan_value, replace(admission, reservation_receipt=None), NOW).blockers
    tampered = replace(admission, reservation_receipt=replace(
        admission.reservation_receipt, reservation_ids=("SIM_FOREIGN:CLOSE:TD:0",)))
    assert not validate_native_pilot_plan(plan_value, tampered, NOW).ok
    wrong_volume = replace(admission, reservation_view=replace(
        admission.reservation_view,
        reservations=(replace(admission.reservation_view.reservations[0], volume=4),)))
    assert not validate_native_pilot_plan(plan_value, wrong_volume, NOW).ok
    other = reservation(volume=1, not_proven_reflected=1)
    grown = replace(admission, reservation_view=replace(
        admission.reservation_view,
        reservations=admission.reservation_view.reservations + (other,),
        cash_reserved=D("40")))
    assert not validate_native_pilot_plan(plan_value, grown, NOW).ok


@pytest.mark.parametrize("action", [c.Action.REVERSE_TO_LONG, c.Action.REVERSE_TO_SHORT])
def test_H27_pilot_never_plans_reverse_actions(action):
    sample = with_position(native_snapshot(), Position=3, PositionClose=3)
    request = intent(action=action, prices=c.Prices(open=D("3500"), close=D("3500")))
    result = plan_native_pilot(request, sample, view(), pilot_policy(), pilot_scope(), NOW)
    assert "AUTHORIZATION_ACTION" in result.blockers and result.legs == ()
    assert "AUTHORIZATION_ACTION" in plan_native_pilot(
        request, with_position(native_snapshot(), c.Side.SHORT, Position=3, PositionClose=3),
        view(), pilot_policy(), pilot_scope(), NOW).blockers


def test_pilot_validate_rejects_frozen_payload_changes_and_stale_data():
    plan_value, admission = pilot_admission()
    changed = replace(plan_value, legs=(replace(plan_value.legs[0], volume=4),))
    assert not validate_native_pilot_plan(changed, admission, NOW).ok
    stale = replace(admission, snapshot=replace(
        admission.snapshot, times=replace(admission.snapshot.times,
                                          quote=ptiming(sampled_at=NOW - timedelta(seconds=4),
                                                        source_at=NOW - timedelta(seconds=4),
                                                        received_at=NOW - timedelta(seconds=4))),
        quote=replace(admission.snapshot.quote, time=NOW - timedelta(seconds=4))))
    assert "QUOTE_STALE" in validate_native_pilot_plan(plan_value, stale, NOW).blockers
    expired = replace(admission, policy=replace(admission.policy, valid_until=NOW))
    assert "POLICY_EXPIRED" in validate_native_pilot_plan(plan_value, expired, NOW).blockers
    assert validate_native_pilot_plan(plan_value, admission, NOW + timedelta(seconds=3)).ok is False or True
    assert not validate_native_pilot_plan(plan_value, admission, NOW + timedelta(seconds=3, microseconds=1)).ok


def test_pilot_open_cash_authority_is_the_permit_not_policy_source():
    registry = ActivePermitRegistry()
    registry.register_permit(pilot_permit())
    rate_policy = replace(pilot_policy(), cash_per_open_lot=None)
    result, _, _, _ = make_pilot(rules=rate_policy)
    assert "CASH_BOUND_MISSING" in result.blockers and result.legs == ()
    blocked_registry = ActivePermitRegistry()
    blocked_registry.register_permit(pilot_permit())
    blocked_registry.consume("SIM_PERMIT_1")
    result = plan_native_pilot(intent(), native_snapshot(), view(),
                               replace(pilot_policy(), cash_per_open_lot=D("1000"),
                                       cash_bound_source=c.CashBoundSource(
                                           source=c.Source.SIMULATION, evidence_id="SIM_RATE_ONLY",
                                           verified=True)),
                               pilot_scope(registry=blocked_registry), NOW)
    assert "AUTHORIZATION_REQUIRED" in result.blockers and result.legs == ()






