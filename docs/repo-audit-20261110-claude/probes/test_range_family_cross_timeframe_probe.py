"""Round-4 external probe (advisory; not a product test, not a hard review).

Question: can REVERSION / GRID / MARTINGALE ever emit OPEN in the production
chain?

Two product facts are exercised, with product code imported read-only:

1. ``probability.regimes.assess_regime`` emits ``cross_timeframe`` ALIGNED or
   COUNTERTREND only when ``base`` is UP_TREND / DOWN_TREND. Every other base
   (ORDERED_RANGE included) gets "UNKNOWN", whatever the daily background says.
2. ``strategies.registry.admission_decision`` rejects ``cross_timeframe``
   "UNKNOWN" for every family (reason CROSS_TIMEFRAME_NOT_VERIFIED).

The three range families only allow base ORDERED_RANGE, so (1) + (2) means
they cannot open through ``decision.pipeline.build_strategy_context``. The
existing M3B tests still see OPEN because their shared fixture builds the
RegimeAssessment by hand with ``cross_timeframe="ALIGNED"``.

Each test asserts the behaviour a tradable range family needs. At the audited
revision (main b75d220) the result is 7 failed / 4 passed: the four
``test_production_regime_for_a_range_is_admissible`` cases and the three
``test_range_family_opens_with_the_value_production_emits`` cases fail; the
allowed-regime check and the three ALIGNED control cases pass. That outcome is
the finding, not a broken probe. Whether the fix belongs in the regime
producer, the admission gate or the plan text is the plan author's decision.

Run (repository root):
    python scripts/test_offline.py -q \
        docs/repo-audit-20261110-claude/probes/test_range_family_cross_timeframe_probe.py
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parents[3] / "tests"
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from ak_trading.v2.probability.regimes import freeze_range  # noqa: E402
from ak_trading.v2.strategies.registry import FAMILY_ALLOWED_REGIMES  # noqa: E402
from ak_trading.v2.strategies.state import commit_observation  # noqa: E402

import test_v2_m3a_regimes as regimes_fixture  # noqa: E402
import test_v2_m3b_matrix as matrix  # noqa: E402

RANGE_FAMILIES = ("REVERSION", "GRID", "MARTINGALE")


def _production_range_assessment(background):
    """ORDERED_RANGE exactly as tests/test_v2_m3a_regimes.py builds it."""
    reference = regimes_fixture.reference_with((0.01, 0.02, 0.03, 0.04, 0.05))
    frozen = freeze_range(regimes_fixture.snapshot(
        regimes_fixture.BASE, prior_low=96.0, prior_high=104.0, atr=2.0))
    features = regimes_fixture.snapshot(
        regimes_fixture.BASE, er=0.2, reference=100.0, vol001=0.03)
    return regimes_fixture.assess(features, reference, frozen=frozen, background=background)


def _drive(family: str, cross: str):
    """tests/test_v2_m3b_matrix.drive_open with one change: the regime's
    cross_timeframe is passed explicitly instead of the fixture default."""
    strategy = matrix.registry()[f"{family.lower()}_m1_v2"]
    bars_sequence, features_sequence = matrix.range_bars_sequence()
    state = None
    proposal = None
    for bars, features_snap in zip(bars_sequence, features_sequence):
        forecasts = matrix.range_forecasts(features_snap, martingale=family == "MARTINGALE")
        context = matrix.make_context(
            strategy.spec, bars=bars, forecasts=forecasts,
            regime_obj=matrix.regime("ORDERED_RANGE", (), matrix.frozen_range(), cross=cross),
            features_snap=features_snap)
        proposal = strategy.evaluate(context, state)
        state = commit_observation(state, proposal)
        if proposal.action == "OPEN":
            break
    return proposal


def test_range_families_only_allow_ordered_range():
    for family in RANGE_FAMILIES:
        assert FAMILY_ALLOWED_REGIMES[family] == ("ORDERED_RANGE",)


@pytest.mark.parametrize("label,background_args", [
    ("clear UP daily background", (0.6, 0.2)),
    ("clear DOWN daily background", (0.2, 0.6)),
    ("neutral daily background", (0.5, 0.5)),
    ("no daily background", None),
])
def test_production_regime_for_a_range_is_admissible(label, background_args):
    """A tradable range needs a cross_timeframe value the admission gate accepts."""
    background = None if background_args is None else regimes_fixture._background(*background_args)
    assessment = _production_range_assessment(background)
    assert assessment.base == "ORDERED_RANGE"
    assert assessment.cross_timeframe not in ("UNKNOWN", "CONFLICT"), (
        f"{label}: assess_regime gave cross_timeframe={assessment.cross_timeframe!r} "
        "for ORDERED_RANGE; admission_decision rejects it for every family")


@pytest.mark.parametrize("family", RANGE_FAMILIES)
def test_range_family_opens_with_the_value_production_emits(family):
    """Same bars, forecasts and evidence as the passing M3B fixture, but with
    the only cross_timeframe assess_regime can emit for ORDERED_RANGE."""
    proposal = _drive(family, "UNKNOWN")
    assert proposal.action == "OPEN", (
        f"{family}: action={proposal.action}, reasons={proposal.reasons}")


@pytest.mark.parametrize("family", RANGE_FAMILIES)
def test_fixture_default_aligned_is_what_makes_m3b_tests_open(family):
    """Control: the identical drive opens once the fixture's hand-built
    ALIGNED is used, so the difference above is cross_timeframe alone."""
    proposal = _drive(family, "ALIGNED")
    assert proposal.action == "OPEN"
