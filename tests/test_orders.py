from types import SimpleNamespace

from ak_trading.orders import dump_orders_for_symbol, iter_orders_for_symbol, order_full_symbol


def test_order_full_symbol():
    order = SimpleNamespace(exchange_id="SHFE", instrument_id="rb2610")
    assert order_full_symbol(order) == "SHFE.rb2610"


def test_iter_orders_matches_full_symbol_not_local_id():
    orders = {
        "o1": SimpleNamespace(
            exchange_id="SHFE",
            instrument_id="rb2610",
            status="FINISHED",
            direction="BUY",
            offset="OPEN",
            volume_left=0,
            last_msg="ok",
        ),
        "o2": SimpleNamespace(
            exchange_id="DCE",
            instrument_id="i2609",
            status="ALIVE",
            direction="SELL",
            offset="OPEN",
            volume_left=1,
            last_msg="",
        ),
        "_x": SimpleNamespace(exchange_id="SHFE", instrument_id="rb2610"),
    }
    matched = list(iter_orders_for_symbol(orders, "SHFE.rb2610"))
    assert [oid for oid, _ in matched] == ["o1"]
    # Old buggy compare against full symbol would match nothing:
    assert not any(o.instrument_id == "SHFE.rb2610" for _, o in matched)


def test_dump_orders_for_symbol_prints_match_and_none():
    lines: list[tuple] = []

    class Api:
        def get_order(self):
            return {
                "o1": SimpleNamespace(
                    exchange_id="SHFE",
                    instrument_id="rb2610",
                    status="FINISHED",
                    direction="BUY",
                    offset="OPEN",
                    volume_left=0,
                    last_msg="filled",
                )
            }

    n = dump_orders_for_symbol(Api(), "SHFE.rb2610", printer=lambda *a: lines.append(a))
    assert n == 1
    assert lines[0][0] == "order"
    assert "SHFE.rb2610" in lines[0]

    lines.clear()
    n2 = dump_orders_for_symbol(Api(), "DCE.i2609", printer=lambda *a: lines.append(a))
    assert n2 == 0
    assert lines[0][:3] == ("order", "none_for_symbol", "DCE.i2609")
