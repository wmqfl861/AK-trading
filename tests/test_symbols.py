from types import SimpleNamespace

import pytest

from ak_trading.symbols import require_concrete_symbol, resolve_trade_symbol


def test_main_contract_uses_underlying():
    quote = SimpleNamespace(
        ins_class="CONT",
        expired=False,
        underlying_symbol="SHFE.rb2610",
        instrument_id="KQ.m@SHFE.rb",
    )
    assert resolve_trade_symbol(quote, "KQ.m@SHFE.rb") == "SHFE.rb2610"


def test_future_uses_self():
    quote = SimpleNamespace(
        ins_class="FUTURE",
        expired=False,
        underlying_symbol="",
        instrument_id="SHFE.rb2610",
    )
    assert resolve_trade_symbol(quote, "SHFE.rb2610") == "SHFE.rb2610"


def test_future_does_not_follow_underlying():
    quote = SimpleNamespace(
        ins_class="FUTURE",
        expired=False,
        underlying_symbol="SHOULD.NOT.USE",
        instrument_id="SHFE.rb2610",
    )
    assert resolve_trade_symbol(quote, "SHFE.rb2610") == "SHFE.rb2610"


def test_option_rejected_even_with_underlying():
    quote = SimpleNamespace(
        ins_class="OPTION",
        expired=False,
        underlying_symbol="SHFE.rb2610",
        instrument_id="SHFE.rb2610C3500",
    )
    with pytest.raises(ValueError, match="options"):
        resolve_trade_symbol(quote, "SHFE.rb2610C3500")


def test_expired_rejected():
    quote = SimpleNamespace(
        ins_class="FUTURE",
        expired=True,
        underlying_symbol="",
        instrument_id="SHFE.rb2401",
    )
    with pytest.raises(ValueError, match="expired"):
        resolve_trade_symbol(quote, "SHFE.rb2401")


def test_main_without_underlying_rejected():
    quote = SimpleNamespace(
        ins_class="CONT",
        expired=False,
        underlying_symbol="",
        instrument_id="KQ.m@SHFE.rb",
    )
    with pytest.raises(ValueError, match="underlying_symbol"):
        resolve_trade_symbol(quote, "KQ.m@SHFE.rb")


def test_require_concrete_symbol():
    assert require_concrete_symbol(" SHFE.rb2610 ") == "SHFE.rb2610"
    with pytest.raises(ValueError, match="concrete"):
        require_concrete_symbol("KQ.m@SHFE.rb")
    with pytest.raises(ValueError, match="EXCHANGE"):
        require_concrete_symbol("rb2610")
