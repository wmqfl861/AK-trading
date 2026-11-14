"""SYNTHETIC K45 statistics, hand calculations and fixed decision boundaries."""
from __future__ import annotations

from collections import Counter
import copy
from datetime import date, timedelta
import importlib.util
import json
import math
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("k45_statistics_test", ROOT / "tools/experiments/k45_forecast_statistics.py")
stats = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = stats
SPEC.loader.exec_module(stats)


def observation(identity, day, target="UP", model=(.6, .2, .2), raw=(.4, .3, .3), calibrated=(.5, .25, .25), mode="MINUTE", horizon=1):
    return stats.Observation(identity, day, mode, horizon, target, model, raw, calibrated)


def cell(interval=(-.02, .02), family=(-.03, .03), *, days=92, algorithm="SOFTMAX"):
    metrics = {key: {"skill": 0.0, "ci95": list(interval)} for key in
               ("raw_brier", "raw_logloss", "calibrated_brier", "calibrated_logloss")}
    metrics["raw_brier"]["ci_family"] = list(family)
    return {"metrics": metrics, "days": days, "algorithm": algorithm, "inner_sufficient": True}


def test_hand_losses_pairing_and_day_equal_weights():
    rows = [observation("a", "d1"), observation("b", "d1", target="DOWN"),
            observation("c", "d2", model=(1., 0., 0.)), observation("bad", "d2", calibrated=None)]
    common, independent, exclusions = stats.paired_rows(rows)
    assert independent == {"model": 4, "raw": 4, "calibrated": 3}
    assert len(common) == 3 and exclusions == {"calibrated:DIRECTION_MISSING": 1}
    days = stats.daily_losses(common)
    assert days["d1"]["model"][0] == pytest.approx((.24 + 1.04) / 2)
    assert stats._weighted_day_mean([days["d1"]["model"][0], days["d2"]["model"][0]], Counter({0: 1, 1: 1})) == pytest.approx(.32)
    assert math.isinf(stats.losses((0., .5, .5), "UP")[1])
    assert stats._skill(0.1, 0) == (None, "NO_DENOMINATOR")
    assert stats._skill(math.inf, .2) == (None, "NONFINITE")
    with pytest.raises(ValueError, match="DUPLICATE_SCORING_REQUEST"):
        stats.paired_rows([rows[0], rows[0]])


def test_percentiles_ecdf_ties_and_reliability_original_day_weights():
    assert stats.percentile([0., 10.], .25) == 2.5
    assert stats.weighted_quantile([(1., .25), (1., .25), (2., .5)], .5) == 1.
    assert stats.weighted_quantile([(1., .25), (1., .25), (2., .5)], .5001) == 2.
    rows = [observation("a", "d1", model=(.4, .2, .4)),
            observation("b", "d1", model=(.4, .2, .4)),
            observation("c", "d2", model=(.1, .1, .8), target="DOWN")]
    result = stats.diagnostics(rows)
    assert result["neutral"] == {"strict_maximum": .5, "tied_maximum": .5, "strict_or_tied": 1.}
    assert result["spread_quantiles"]["0.5"] == 0.
    assert result["row_equal_quantiles"]["0.5"] == .2
    assert len(result["reliability"]) == 30
    empty = next(row for row in result["reliability"] if row["class"] == "UP" and row["bin"] == 9)
    assert empty["rows"] == 0 and empty["predicted"] is None


@pytest.mark.parametrize("width,status", [(.02, "NO_INFORMATION"), (.020000000000000004, "UNRESOLVED")])
def test_unrounded_halfwidth_boundary(width, status):
    result = stats.classify_cell(cell((-width, width)))
    assert result["status"] == status
    assert result["half_width95"] == width


def test_conclusion_precedence_and_na():
    cases = []
    sufficient = cell((.1, .2), (.05, .25))
    assert stats.classify_cell(sufficient)["status"] == "CONTINUE"
    suggestive = cell((.01, .03), (-.01, .05))
    assert stats.classify_cell(suggestive)["status"] == "SUGGESTIVE"
    base = copy.deepcopy(sufficient)
    base["algorithm"] = "UNCONDITIONAL_FREQUENCY"
    assert stats.classify_cell(base)["status"] == "BASE_RATE"
    mixed = copy.deepcopy(sufficient)
    mixed["metrics"]["raw_logloss"]["ci95"] = [-.1, .2]
    assert stats.classify_cell(mixed)["status"] == "MIXED"
    for bad in (cell(days=40), cell(algorithm=None), cell()):
        if bad["days"] == 92 and bad["algorithm"] is not None:
            bad["metrics"]["raw_brier"]["ci95"] = None
        result = stats.classify_cell(bad)
        assert result["status"] == "INSUFFICIENT" and not result["resolved_at_002"]
        cases.append(result)
    assert stats.classify_mode(cases) == "本固定设计没有可评估结果"
    assert "提示性" in stats.classify_mode([stats.classify_cell(mixed), stats.classify_cell(cell())])


@pytest.mark.parametrize("mode,block,horizons", [("MINUTE", 2, (1, 5, 15, 30, 60)), ("DAILY", 20, (1, 5, 20))])
def test_exact_fixed_bootstrap_shared_calendar_missing_dates_and_invalid_draws(mode, block, horizons):
    calendar = [(date(2024, 6, 3) + timedelta(days=i)).isoformat() for i in range(92)]
    draws = stats.bootstrap_indices(calendar, mode)
    first = next(draws)
    assert sum(first.values()) == 92
    assert first == next(stats.bootstrap_indices(calendar, mode))
    rows = [observation(f"{h}-{day}", day, mode=mode, horizon=h) for h in horizons for day in calendar if day != calendar[30]]
    result = stats.score_mode(rows, calendar, mode, {h: "SOFTMAX" for h in horizons}, True)
    assert result["calendar_days"] == 92 and result["block_days"] == block
    assert result["repeats"] == 20000 and len(result["cells"]) == len(horizons)
    metrics = result["cells"][0]["metrics"]
    assert all(c["days"] == 91 and c["metrics"] == metrics for c in result["cells"])
    assert all(m["valid_repeats"] == 20000 for m in metrics.values())
    json.dumps(result, allow_nan=False)


def test_zero_target_probability_is_infinite_loss_and_not_clipped():
    calendar = [str(i).zfill(3) for i in range(60)]
    rows = [observation(str(i), day, model=(0., .5, .5)) for i, day in enumerate(calendar)]
    # Individual infinite losses remain observable before artifact conversion.
    assert all(math.isinf(value["model"][1]) for value in stats.daily_losses(rows).values())
    assert stats._skill(math.inf, 1.) == (None, "NONFINITE")


def test_any_invalid_bootstrap_draw_invalidates_entire_ci_without_resampling():
    calendar = [str(i).zfill(3) for i in range(92)]
    row = observation("only", calendar[0], mode="DAILY", model=(0., .5, .5))
    result = stats.score_mode([row], calendar, "DAILY", {1: "SOFTMAX"}, True)
    first = result["cells"][0]
    assert first["losses"]["model"]["logloss"] is None
    assert first["loss_reasons"]["model"]["logloss"] == "NONFINITE"
    assert first["metrics"]["raw_logloss"]["ci95"] is None
    brier = first["metrics"]["raw_brier"]
    assert 0 < brier["valid_repeats"] < 20000
    assert brier["valid_repeats"] + sum(brier["invalid_repeats"].values()) == 20000
    assert brier["ci95"] is None and brier["ci_family"] is None
    assert first["status"] == "INSUFFICIENT"
    json.dumps(result, allow_nan=False)


def test_invalid_probability_and_all_missing_draw():
    assert stats.probability_error((.4, .4, .4)) == "PROBABILITY_INVALID"
    assert stats.probability_error((math.nan, 0., 1.)) == "PROBABILITY_NONFINITE"
    assert stats._weighted_day_mean([None, .3, None], Counter({0: 1, 2: 2})) is None
    assert stats._weighted_day_mean([None, .3, None], Counter({0: 1, 1: 3})) == .3
