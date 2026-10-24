import math

import pytest

from ak_trading.strategy import DualMAStrategy


def test_dual_ma_long_and_flat_signals():
    strategy = DualMAStrategy(fast_window=2, slow_window=3)

    long_signal = strategy.evaluate([1, 2, 3])
    assert long_signal.target_position == 1
    assert long_signal.reason == "fast_above_slow"
    assert long_signal.fast_ma == pytest.approx(2.5)
    assert long_signal.slow_ma == pytest.approx(2.0)

    flat_signal = strategy.evaluate([3, 2, 1])
    assert flat_signal.target_position == 0
    assert flat_signal.reason == "fast_not_above_slow"


def test_dual_ma_holds_on_missing_or_invalid_completed_data():
    strategy = DualMAStrategy(fast_window=2, slow_window=3)
    assert strategy.evaluate([1, 2]).target_position is None
    invalid = strategy.evaluate([1, math.nan, 3])
    assert invalid.target_position is None
    assert invalid.reason == "invalid_close"


@pytest.mark.parametrize(
    ("fast", "slow"),
    [(0, 3), (3, 3), (4, 3), (True, 3), (2, 2.5)],
)
def test_dual_ma_rejects_bad_windows(fast, slow):
    with pytest.raises(ValueError):
        DualMAStrategy(fast_window=fast, slow_window=slow)
