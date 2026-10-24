import pytest

from ak_trading.runtime import is_valid_price, wait_until


def test_is_valid_price():
    assert is_valid_price(1.2)
    assert not is_valid_price(float("nan"))
    assert not is_valid_price(float("inf"))
    assert not is_valid_price(None)
    assert not is_valid_price("x")


def test_wait_until_true_and_timeout():
    state = {"n": 0}

    class FakeApi:
        def wait_update(self, deadline=None):
            state["n"] += 1
            return False

    api = FakeApi()
    assert wait_until(api, lambda: True, timeout=1.0) is True
    assert wait_until(api, lambda: False, timeout=0.3, poll=0.05) is False
    assert state["n"] >= 1


def test_wait_until_rejects_bad_timeout():
    class FakeApi:
        def wait_update(self, deadline=None):
            return False

    api = FakeApi()
    with pytest.raises(ValueError, match="timeout"):
        wait_until(api, lambda: False, timeout=float("nan"))
    with pytest.raises(ValueError, match="timeout"):
        wait_until(api, lambda: False, timeout=0)
    with pytest.raises(ValueError, match="poll"):
        wait_until(api, lambda: False, timeout=1, poll=-1)
