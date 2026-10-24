from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ak_trading.futures.data import update_pair_selection
from ak_trading.futures.spec import PairSelectionState, ResearchPolicy


def candidate(symbol):
    near = datetime(2026, 5, 1, tzinfo=timezone.utc)
    return SimpleNamespace(near_symbol="N", far_symbol=symbol, near_expiry_at=near,
                           far_expiry_at=near + timedelta(days=90), gap_days=90,
                           far_median_volume20=100)


@pytest.mark.parametrize("interruption", ["current_wins", "no_candidates"])
def test_switch_confirmation_requires_consecutive_observations(interruption):
    a, b = candidate("A"), candidate("B")
    state = PairSelectionState(near_symbol="N", far_symbol="A", far_expiry_at=a.far_expiry_at)
    pending, _, _ = update_pair_selection(state, [b], ResearchPolicy())
    assert pending.pending_far_symbol == "B"
    reset, _, _ = update_pair_selection(pending, [a] if interruption == "current_wins" else [], ResearchPolicy())
    assert reset.far_symbol == "A"
    assert reset.pending_far_symbol is None
    assert reset.pending_far_expiry_at is None
    assert reset.confirmation_count == 0
    again, _, _ = update_pair_selection(reset, [b], ResearchPolicy())
    assert again.far_symbol == "A"
    assert again.pending_far_symbol == "B" and again.confirmation_count == 1
    confirmed, _, _ = update_pair_selection(again, [b], ResearchPolicy())
    assert confirmed.far_symbol == "B"
