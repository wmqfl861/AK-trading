import pytest

from ak_trading.risk import limit_target_position, require_positive_position_limit


def test_position_limit_is_long_only_by_default():
    assert limit_target_position(None).target_position is None
    assert limit_target_position(1).target_position == 1
    short = limit_target_position(-1)
    assert short.target_position == 0
    assert short.reason == "short_blocked_to_flat"
    assert limit_target_position(5).target_position == 1


def test_position_limit_can_allow_bounded_short():
    decision = limit_target_position(-5, max_abs_position=2, allow_short=True)
    assert decision.target_position == -2
    assert decision.reason == "clamped_min"


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_position_limit_rejects_bad_limit(value):
    with pytest.raises(ValueError):
        limit_target_position(0, max_abs_position=value)


def test_require_positive_position_limit_returns_canonical_integer():
    assert require_positive_position_limit(3) == 3
    with pytest.raises(ValueError, match="lot_cap"):
        require_positive_position_limit(0, name="lot_cap")
