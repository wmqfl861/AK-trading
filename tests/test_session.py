"""Account-boundary tests for ak_trading.session.create_api (P03).

The boundary under test: only an exact local tqsdk ``TqSim`` instance may
reach ``TqApi`` through ``sim_account``; anything else is rejected before
``TqAuth``/``TqApi`` are constructed.

Type-faithful stub semantics (no real accounts, no network):
- Positive ``sim_account`` values are genuine ``TqSim`` instances. The tqsdk
  ``TqSim`` constructor only assigns local attributes, so it is offline-safe.
- ``TqKq`` (Quick) and ``TqAccount`` (real) are never instantiated here. Their
  rejection branch is represented by stand-ins that subclass the very same
  tqsdk base class (``BaseOtg``) while bypassing every real constructor, so
  the type-level relationship "remote OTG account, not a TqSim" is exercised
  without credentials, connection state, or sockets.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from tqsdk import TqSim
from tqsdk.tradeable.otg.base_otg import BaseOtg

from ak_trading.session import create_api


class _QuickLikeAccount(BaseOtg):
    """Stands for TqKq (Quick): same tqsdk base class, no real constructor."""

    def __init__(self):
        pass


class _RealLikeAccount(BaseOtg):
    """Stands for TqAccount (real): same tqsdk base class, no real constructor."""

    def __init__(self):
        pass


class _TqSimSubclass(TqSim):
    """A TqSim subclass; must be rejected by the exact-type boundary."""


def _settings():
    return SimpleNamespace(tq_user="unit-user", tq_pass="unit-secret")


def test_create_api_rejects_bad_balance():
    settings = _settings()
    with pytest.raises(ValueError):
        create_api(settings, sim_balance=0)
    with pytest.raises(ValueError):
        create_api(settings, sim_balance=-1.0)
    with pytest.raises(ValueError):
        create_api(settings, sim_balance=float("nan"))
    with pytest.raises(ValueError):
        create_api(settings, sim_balance=float("inf"))


def test_create_api_bad_balance_takes_precedence_over_valid_sim():
    settings = _settings()
    sim = TqSim(init_balance=10.0)
    with (
        patch("ak_trading.session.TqAuth") as auth_cls,
        patch("ak_trading.session.TqApi") as api_cls,
    ):
        with pytest.raises(ValueError, match="sim_balance"):
            create_api(settings, sim_balance=float("nan"), sim_account=sim)
        auth_cls.assert_not_called()
        api_cls.assert_not_called()


def test_create_api_uses_tqsim_and_auth():
    settings = _settings()
    fake_api = object()
    with (
        patch("ak_trading.session.TqSim") as sim_cls,
        patch("ak_trading.session.TqAuth") as auth_cls,
        patch("ak_trading.session.TqApi", return_value=fake_api) as api_cls,
    ):
        sim_cls.return_value = MagicMock(name="sim")
        auth_cls.return_value = MagicMock(name="auth")
        api = create_api(settings, sim_balance=12345.0)
        assert api is fake_api
        sim_cls.assert_called_once_with(init_balance=12345.0)
        auth_cls.assert_called_once_with("unit-user", "unit-secret")
        api_cls.assert_called_once_with(account=sim_cls.return_value, auth=auth_cls.return_value)


def test_create_api_accepts_explicit_local_tqsim_with_backtest():
    settings = _settings()
    fake_api = object()
    sim = TqSim(init_balance=777.0)
    backtest = MagicMock(name="backtest")
    with (
        patch("ak_trading.session.TqAuth") as auth_cls,
        patch("ak_trading.session.TqApi", return_value=fake_api) as api_cls,
    ):
        auth_cls.return_value = MagicMock(name="auth")
        api = create_api(settings, sim_account=sim, backtest=backtest)
        assert api is fake_api
        auth_cls.assert_called_once_with("unit-user", "unit-secret")
        api_cls.assert_called_once_with(account=sim, auth=auth_cls.return_value, backtest=backtest)


def _rejection_cases():
    """(case id, offending sim_account value) for every rejected surface."""
    return [
        ("arbitrary-object", object()),
        ("duck-object", SimpleNamespace(account="SHFET-7777", password="hunter2", init_balance=1.0)),
        ("account-id-string", "TQSIM"),
        ("quick-account", _QuickLikeAccount()),
        ("real-account", _RealLikeAccount()),
        ("multi-account-sim-list", [TqSim(init_balance=1.0), TqSim(init_balance=2.0)]),
        ("multi-account-mixed-list", [TqSim(init_balance=1.0), _QuickLikeAccount()]),
        ("multi-account-tuple", (TqSim(init_balance=1.0),)),
        ("tqsim-subclass", _TqSimSubclass(init_balance=1.0)),
    ]


@pytest.mark.parametrize(
    "bad_sim_account",
    [pytest.param(value, id=case_id) for case_id, value in _rejection_cases()],
)
def test_create_api_rejects_non_local_tqsim_before_tqauth_and_tqapi(bad_sim_account):
    settings = _settings()
    with (
        patch("ak_trading.session.TqAuth") as auth_cls,
        patch("ak_trading.session.TqApi") as api_cls,
    ):
        with pytest.raises(ValueError, match="sim_account") as excinfo:
            create_api(settings, sim_account=bad_sim_account)
        auth_cls.assert_not_called()
        api_cls.assert_not_called()
        message = str(excinfo.value)
        assert "unit-secret" not in message
        assert "hunter2" not in message
        assert "SHFET-7777" not in message
        assert "TqSim" in message
        assert not any(
            token in message
            for token in ("MagicMock", "object at 0x", "<", "[", "(", repr(bad_sim_account))
        )


def test_create_api_rejection_message_is_static_and_secret_free():
    settings = _settings()
    secrets = ("unit-user", "unit-secret", "hunter2", "SHFET-7777")
    for _case_id, bad_sim_account in _rejection_cases():
        with pytest.raises(ValueError) as excinfo:
            create_api(settings, sim_account=bad_sim_account)
        message = str(excinfo.value)
        assert message == (
            "sim_account must be a local TqSim instance; "
            "real, remote, Quick and multi-account objects are not supported"
        )
        assert not any(secret in message for secret in secrets)
