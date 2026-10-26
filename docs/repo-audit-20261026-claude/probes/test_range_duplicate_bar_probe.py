"""N1 probe: duplicate bar delivery must not advance range-family entry confirmation.

External review round 2 (2026-10-26), see ../REVIEW.md section N1.

REVERSION, GRID and MARTINGALE compute
``confirmation = state.confirmation_count + 1 if gate.ok else 0`` without the
same-bar guard that d46274c added to TREND and PULLBACK. When the same bar is
delivered again (replay timer events, restart/resume), the counter still
advances, so the strategy can OPEN before the third distinct bar required by
``entry_confirmation_bars=3``.

The probe only uses the SYNTHETIC builders already in
tests/test_v2_m3b_matrix.py: no network, no data, no credentials.

Run from the repository root (Git Bash):

    python scripts/test_offline.py -q docs/repo-audit-20261026-claude/probes/test_range_duplicate_bar_probe.py

Expected before the fix: 3 passed, 6 failed (the clean control passes; both
redelivery cases fail for all three families). After the fix: 9 passed.
Once fixed, fold these cases into the permanent M3B suite, parametrised over
all seven entry families, and retire this probe.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parents[3] / "tests"
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from ak_trading.v2.strategies.state import commit_observation  # noqa: E402
from test_v2_m3b_matrix import (  # noqa: E402
    frozen_range,
    make_context,
    range_bars_sequence,
    range_forecasts,
    regime,
    registry,
)

FAMILIES = (("REVERSION", False), ("GRID", False), ("MARTINGALE", True))


def run(family, martingale, order):
    """Deliver bars in ``order`` (indices into the synthetic sequence).

    Returns (bar index, action, confirmation_count) per step, stopping at OPEN.
    """
    strategy = registry()[f"{family.lower()}_m1_v2"]
    bars, snaps = range_bars_sequence()
    state, steps = None, []
    for index in order:
        context = make_context(
            strategy.spec,
            bars=bars[index],
            forecasts=range_forecasts(snaps[index], martingale=martingale),
            regime_obj=regime("ORDERED_RANGE", (), frozen_range()),
            features_snap=snaps[index],
        )
        proposal = strategy.evaluate(context, state)
        state = commit_observation(state, proposal)
        steps.append((index, proposal.action, state.confirmation_count))
        if proposal.action == "OPEN":
            break
    return steps


@pytest.mark.parametrize("family,martingale", FAMILIES)
def test_clean_three_distinct_bars(family, martingale):
    """Control: bars 0, 1, 2 delivered once each -> OPEN on bar 2."""
    steps = run(family, martingale, (0, 1, 2))
    assert steps[-1][1] == "OPEN" and steps[-1][0] == 2, steps


@pytest.mark.parametrize("family,martingale", FAMILIES)
def test_recross_bar_redelivered_must_not_open(family, martingale):
    """Bars 0, 1, 1: only two distinct bars, so no OPEN is allowed."""
    steps = run(family, martingale, (0, 1, 1))
    assert all(action != "OPEN" for _, action, _ in steps), steps


@pytest.mark.parametrize("family,martingale", FAMILIES)
def test_arming_bar_redelivered_must_not_open_on_second_distinct_bar(family, martingale):
    """Bars 0, 0, 0, 1: still only two distinct bars, so no OPEN is allowed."""
    steps = run(family, martingale, (0, 0, 0, 1))
    assert all(action != "OPEN" for _, action, _ in steps), steps
