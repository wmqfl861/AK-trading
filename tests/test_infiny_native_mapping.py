"""NE1-H09/H10: exact framework mapping, thin-mirror differential, tick/volume vectors.

All fixtures are SYNTHETIC; no native SDK import, network or journal.
"""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import pytest

import builtins

_import_before = builtins.__import__


def _guard(name, *args, **kwargs):
    if name.split(".")[0] in {"INFINIGO", "numpy", "ctypes", "pythongo", "PyQt5", "PyQt6", "dotenv"}:
        raise AssertionError("mapping module attempted a native/GUI import")
    return _import_before(name, *args, **kwargs)


builtins.__import__ = _guard
try:
    from ak_trading.infiny_execution import contracts as c
    from ak_trading.infiny_execution.native import NativeExecutionError, build_native_request
    from ak_trading.infiny_execution.simulation import FakeNative, simulation_context
finally:
    builtins.__import__ = _import_before

D = Decimal
NOW = datetime(2026, 9, 14, 1, 30, tzinfo=timezone.utc)


def leg(**changes):
    offset = changes.get("offset", c.Offset.OPEN)
    close = offset is not c.Offset.OPEN
    bucket = {c.Offset.CLOSE: c.Bucket.ALL, c.Offset.CLOSE_TODAY: c.Bucket.TD,
              c.Offset.CLOSE_YESTERDAY: c.Bucket.YD}.get(offset)
    values = dict(leg_id="SIM_INTENT_1:CLOSE:TD:0" if close else "SIM_INTENT_1:OPEN:-:0",
                  index=0, phase=c.Phase.CLOSE if close else c.Phase.OPEN, depends_on=(),
                  direction=c.Direction.BUY, offset=c.Offset.OPEN, volume=2, price=D("3500"),
                  position_bucket=bucket, cash_reserve=D("2000"))
    values.update(changes)
    return c.Leg(**values)


# --- NE1-H09: the eight legal direction/offset combinations with exact keys ---

H09_MATRIX = [
    # direction, offset, exchange, Direction, Offset
    (c.Direction.BUY, c.Offset.OPEN, "SHFE", "0", "0"),
    (c.Direction.SELL, c.Offset.OPEN, "SHFE", "1", "0"),
    (c.Direction.BUY, c.Offset.CLOSE, "DCE", "0", "1"),
    (c.Direction.SELL, c.Offset.CLOSE, "DCE", "1", "1"),
    (c.Direction.BUY, c.Offset.CLOSE_TODAY, "SHFE", "0", "3"),
    (c.Direction.SELL, c.Offset.CLOSE_TODAY, "INE", "1", "3"),
    (c.Direction.BUY, c.Offset.CLOSE_YESTERDAY, "SHFE", "0", "1"),
    (c.Direction.SELL, c.Offset.CLOSE_YESTERDAY, "INE", "1", "1"),
]


@pytest.mark.parametrize("direction,offset,exchange,want_direction,want_offset", H09_MATRIX)
@pytest.mark.parametrize("order_type,want_type", [(c.OrderType.GFD, "0"), (c.OrderType.FAK, "1")])
def test_H09_golden_direction_offset_matrix(direction, offset, exchange, want_direction,
                                             want_offset, order_type, want_type):
    context = simulation_context(exchange=exchange)
    native = FakeNative(context.binding)
    item = leg(direction=direction, offset=offset)
    request = build_native_request(item, context.binding, native.investor_handle,
                                   order_type=order_type, price_tick=D("1"),
                                   verified_hedge_flags=("1",))
    assert request == dict(
        StrategyID=23, Exchange=exchange, InstrumentID="rb2610", Volume=2, Price=3500.0,
        Direction=want_direction, OrderType=want_type, Investor=native.investor_handle,
        HedgeFlag="1", Offset=want_offset, Market=False, Memo=item.leg_id)
    native.send_order(request)
    assert len(native.sent_requests) == 1


# --- NE1-H09: the reviewed thin mirror the client must byte-match ---

def mirror_map(leg_value, binding, investor_handle, *, order_type, price_tick, verified_hedge_flags):
    """Reviewed standalone mirror of build_native_request (no repository imports).

    The deployed client file must reproduce exactly this behavior; the frozen
    golden vectors below pin both ends to identical output.
    """
    if leg_value.offset in (c.Offset.CLOSE_TODAY, c.Offset.CLOSE_YESTERDAY) and binding.exchange not in ("SHFE", "INE"):
        raise ValueError("unsupported close offset")
    if leg_value.offset is c.Offset.CLOSE and binding.exchange in ("SHFE", "INE"):
        raise ValueError("generic close unsupported on split exchanges")
    if order_type not in (c.OrderType.GFD, c.OrderType.FAK):
        raise ValueError("unsupported order type")
    if binding.hedge_flag not in verified_hedge_flags:
        raise ValueError("unverified hedge")
    converted = float(leg_value.price)
    from decimal import Decimal as _D
    if _D(str(converted)) != leg_value.price or leg_value.price % price_tick != 0:
        raise ValueError("tick lost in float conversion")
    return {
        "StrategyID": binding.strategy_id, "Exchange": binding.exchange,
        "InstrumentID": binding.instrument_id, "Volume": leg_value.volume, "Price": converted,
        "Direction": {"BUY": "0", "SELL": "1"}[leg_value.direction.value],
        "OrderType": {c.OrderType.GFD: "0", c.OrderType.FAK: "1"}[order_type],
        "Investor": investor_handle, "HedgeFlag": binding.hedge_flag,
        "Offset": {c.Offset.OPEN: "0", c.Offset.CLOSE: "1",
                   c.Offset.CLOSE_YESTERDAY: "1", c.Offset.CLOSE_TODAY: "3"}[leg_value.offset],
        "Market": False, "Memo": leg_value.leg_id,
    }


@pytest.mark.parametrize("direction,offset,exchange,want_direction,want_offset", H09_MATRIX)
def test_H09_thin_mirror_matches_baseline(direction, offset, exchange, want_direction, want_offset):
    context = simulation_context(exchange=exchange)
    native = FakeNative(context.binding)
    item = leg(direction=direction, offset=offset)
    baseline = build_native_request(item, context.binding, native.investor_handle,
                                    order_type=c.OrderType.GFD, price_tick=D("1"),
                                    verified_hedge_flags=("1",))
    mirrored = mirror_map(item, context.binding, native.investor_handle,
                          order_type=c.OrderType.GFD, price_tick=D("1"), verified_hedge_flags=("1",))
    assert dict(mirrored) == dict(baseline)


@pytest.mark.parametrize("kwargs", [
    dict(order_type="FOK"), dict(order_type="3"), dict(order_type="2"),
    dict(verified_hedge_flags=()), dict(price_tick=None), dict(price_tick=D("0")),
])
def test_H09_invalid_order_type_hedge_and_tick_evidence_rejected(kwargs):
    context = simulation_context()
    native = FakeNative(context.binding)
    values = dict(order_type=c.OrderType.GFD, price_tick=D("1"), verified_hedge_flags=("1",))
    values.update(kwargs)
    with pytest.raises((NativeExecutionError, c.ContractError)):
        build_native_request(leg(), context.binding, native.investor_handle, **values)
    assert native.sent_requests == []


def test_H09_split_exchange_policies():
    context = simulation_context(exchange="DCE")
    with pytest.raises(NativeExecutionError):
        build_native_request(leg(offset=c.Offset.CLOSE_YESTERDAY), context.binding, object(),
                             order_type=c.OrderType.GFD, price_tick=D("1"), verified_hedge_flags=("1",))
    context = simulation_context(exchange="SHFE")
    with pytest.raises(NativeExecutionError):
        build_native_request(leg(offset=c.Offset.CLOSE), context.binding, object(),
                             order_type=c.OrderType.GFD, price_tick=D("1"), verified_hedge_flags=("1",))


# --- NE1-H10: price tick, float round-trip and deterministic volume vectors ---

@pytest.mark.parametrize("price,tick,accepted", [
    ("3500.2", "0.2", True), ("3500.1", "0.2", False), ("3500", "1", True),
    ("3500.5", "1", False), ("9007199254740993", "1", False),
    ("3500.0000000000001", "0.0000000000001", False),
])
def test_H10_tick_alignment_and_float_roundtrip(price, tick, accepted):
    context = simulation_context()
    native = FakeNative(context.binding)
    call = lambda: build_native_request(leg(price=D(price)), context.binding, native.investor_handle,
                                        order_type=c.OrderType.GFD, price_tick=D(tick),
                                        verified_hedge_flags=("1",))
    if accepted:
        assert D(str(call()["Price"])) == D(price)
    else:
        with pytest.raises((NativeExecutionError, c.ContractError)):
            call()
    assert native.sent_requests == []


@pytest.mark.parametrize("price", [D("NaN"), D("Infinity"), D("-1"), D("0"), True, 3500.0])
def test_H10_malformed_prices_rejected_by_contract(price):
    with pytest.raises(c.ContractError):
        leg(price=price)


def test_H10_volume_must_be_positive_integer():
    for volume in (True, 0, -1, 1.5, "2"):
        with pytest.raises(c.ContractError):
            leg(volume=volume)


def test_H10_hand_computed_split_vectors(tmp_path):
    """11 with min3/max5 splits 5+3+3; 6 with min4/max5 has no legal split."""
    from ak_trading.infiny_execution.ledger import Ledger
    from ak_trading.infiny_execution.planning import plan as pure_plan
    from ak_trading.infiny_execution.simulation import FakeClock

    def split_of(minimum, maximum, volume, name):
        clock = FakeClock()
        context = simulation_context(clock=clock)
        context = replace(context, intent=replace(context.intent, volume=volume),
                          policy=replace(context.policy, max_leg_qty=maximum),
                          snapshot=replace(
                              context.snapshot,
                              contract=replace(context.snapshot.contract,
                                               min_limit_order_volume=minimum,
                                               max_limit_order_volume=maximum)))
        ledger = Ledger(tmp_path / (name + ".sqlite"), clock=clock)
        try:
            ledger.register(context.intent)
            ledger.register_context(context)
            view = ledger.view(context.binding.account_key,
                               snapshot_id=context.snapshot.snapshot_id)
            return pure_plan(context.intent, context.snapshot, view, context.policy, clock())
        finally:
            ledger.close()

    loose = split_of(3, 5, 11, "loose")
    assert not loose.blockers
    assert tuple(item.volume for item in loose.legs) == (5, 3, 3)
    strict = split_of(4, 5, 6, "strict")
    assert "VOLUME_SPLIT_IMPOSSIBLE" in strict.blockers and strict.legs == ()
