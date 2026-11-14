"""Fixed K45 research statistics; no fitting, data loading or trading entry point.

Inputs are already paired frozen direction predictions and labels. Real TEST
inputs are released only by the separately reviewed stage-three orchestrator.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import math
import random
from typing import Mapping, Sequence

CLASSES = ("UP", "DOWN", "NEUTRAL")
BOOTSTRAP_REPEATS = 20_000
BOOTSTRAP_SEED = 20_261_113
RESOLUTION_HALF_WIDTH = 0.02
QUANTILES = (0.0, .01, .05, .25, .50, .75, .95, .99, 1.0)


@dataclass(frozen=True)
class Observation:
    request_id: str
    day: str
    mode: str
    horizon: int
    target: str
    model: tuple[float, float, float] | None
    raw: tuple[float, float, float] | None
    calibrated: tuple[float, float, float] | None


def probability_error(values) -> str | None:
    if values is None:
        return "DIRECTION_MISSING"
    if len(values) != 3 or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in values):
        return "PROBABILITY_TYPE"
    if any(not math.isfinite(v) for v in values):
        return "PROBABILITY_NONFINITE"
    if any(v < 0 or v > 1 for v in values) or abs(math.fsum(values) - 1) > 1e-9:
        return "PROBABILITY_INVALID"
    return None


def losses(values, target: str) -> tuple[float, float]:
    error = probability_error(values)
    if error or target not in CLASSES:
        raise ValueError(error or "DIRECTION_LABEL_INVALID")
    index = CLASSES.index(target)
    return (math.fsum((value - int(i == index)) ** 2 for i, value in enumerate(values)),
            -math.log(values[index]) if values[index] > 0 else math.inf)


def percentile(values: Sequence[float], q: float) -> float:
    """Fixed type-7 percentile; invalid input is never silently discarded."""
    if not values or not 0 <= q <= 1 or any(not math.isfinite(v) for v in values):
        raise ValueError("PERCENTILE_INVALID")
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    if lower == len(ordered) - 1:
        return ordered[lower]
    return ordered[lower] + (position - lower) * (ordered[lower + 1] - ordered[lower])


def weighted_quantile(values: Sequence[tuple[float, float]], q: float) -> float | None:
    if not 0 <= q <= 1:
        raise ValueError("QUANTILE_OUTSIDE_UNIT_INTERVAL")
    if not values:
        return None
    merged = defaultdict(list)
    for value, weight in values:
        if not math.isfinite(value) or not math.isfinite(weight) or weight <= 0:
            raise ValueError("WEIGHTED_ECDF_INVALID")
        merged[value].append(weight)
    ordered = [(value, math.fsum(weights)) for value, weights in sorted(merged.items())]
    if q == 0:
        return ordered[0][0]
    if q == 1:
        return ordered[-1][0]
    target = q * math.fsum(weight for _, weight in ordered)
    cumulative = []
    for value, weight in ordered:
        cumulative.append(weight)
        if math.fsum(cumulative) >= target:
            return value
    return ordered[-1][0]


def paired_rows(rows: Sequence[Observation]):
    """A three-way common population, with each independent population disclosed."""
    seen = set()
    independent = Counter()
    exclusions = Counter()
    common = []
    for row in rows:
        if row.request_id in seen:
            raise ValueError("DUPLICATE_SCORING_REQUEST")
        seen.add(row.request_id)
        if row.target not in CLASSES:
            exclusions["DIRECTION_LABEL_MISSING"] += 1
            continue
        errors = {}
        for name in ("model", "raw", "calibrated"):
            error = probability_error(getattr(row, name))
            if error is None:
                independent[name] += 1
            else:
                errors[name] = error
                exclusions[f"{name}:{error}"] += 1
        if not errors:
            common.append(row)
    return common, {name: independent[name] for name in ("model", "raw", "calibrated")}, dict(exclusions)


def daily_losses(rows: Sequence[Observation]) -> dict[str, dict[str, tuple[float, float]]]:
    days = defaultdict(list)
    for row in rows:
        days[row.day].append(row)
    return {day: {name: tuple(math.fsum(losses(getattr(row, name), row.target)[metric]
                                      for row in group) / len(group) for metric in (0, 1))
                  for name in ("model", "raw", "calibrated")}
            for day, group in sorted(days.items())}


def _skill(model: float | None, baseline: float | None):
    if model is None or baseline is None:
        return None, "NO_DENOMINATOR"
    if not math.isfinite(model) or not math.isfinite(baseline):
        return None, "NONFINITE"
    if baseline <= 0:
        return None, "NO_DENOMINATOR"
    value = 1 - model / baseline
    return (value, None) if math.isfinite(value) else (None, "NONFINITE")


def bootstrap_indices(calendar: Sequence[str], mode: str):
    """Same fixed date indices for all horizons and both controls of one mode."""
    if not calendar or len(set(calendar)) != len(calendar) or list(calendar) != sorted(calendar):
        raise ValueError("EXPLICIT_CALENDAR_REQUIRED")
    if mode not in ("MINUTE", "DAILY"):
        raise ValueError("MODE_INVALID")
    block = 2 if mode == "MINUTE" else 20
    rng = random.Random(BOOTSTRAP_SEED)
    for _ in range(BOOTSTRAP_REPEATS):
        sample = []
        while len(sample) < len(calendar):
            start = rng.randrange(len(calendar))
            sample.extend((start + i) % len(calendar) for i in range(block))
        yield Counter(sample[:len(calendar)])


def _weighted_day_mean(values, counts):
    denominator = sum(count for index, count in counts.items() if values[index] is not None)
    if denominator == 0:
        return None
    return math.fsum(values[index] * count for index, count in counts.items()
                     if values[index] is not None) / denominator


def score_mode(rows: Sequence[Observation], calendar: Sequence[str], mode: str,
               algorithms: Mapping[int, str | None], inner_sufficient: bool) -> dict:
    """Score every fixed horizon, retaining missing dates and invalid bootstrap draws."""
    horizons = (1, 5, 15, 30, 60) if mode == "MINUTE" else (1, 5, 20)
    if mode not in ("MINUTE", "DAILY"):
        raise ValueError("MODE_INVALID")
    if any(row.mode != mode or row.horizon not in horizons or row.day not in calendar for row in rows):
        raise ValueError("SCORING_SCOPE_MISMATCH")
    cells = {}
    vectors = {}
    draws = {}
    invalid = {}
    for horizon in horizons:
        group = [row for row in rows if row.horizon == horizon]
        common, independent, exclusions = paired_rows(group)
        days = daily_losses(common)
        values = {(name, metric): [days[day][name][metric] if day in days else None for day in calendar]
                  for name in ("model", "raw", "calibrated") for metric in (0, 1)}
        vectors[horizon] = values
        losses_by_model = {name: {metric_name: _weighted_day_mean(values[name, metric], Counter(range(len(calendar))))
                                 for metric, metric_name in enumerate(("brier", "logloss"))}
                           for name in ("model", "raw", "calibrated")}
        metrics = {}
        for baseline in ("raw", "calibrated"):
            for metric, metric_name in enumerate(("brier", "logloss")):
                key = baseline + "_" + metric_name
                skill, reason = _skill(losses_by_model["model"][metric_name], losses_by_model[baseline][metric_name])
                metrics[key] = {"skill": skill, "point_reason": reason}
                draws[horizon, key] = []
                invalid[horizon, key] = Counter()
        loss_reasons = {name: {metric: "NONFINITE" if value is not None and not math.isfinite(value) else
                              "NO_OBSERVATIONS" if value is None else None
                              for metric, value in items.items()} for name, items in losses_by_model.items()}
        reported_losses = {name: {metric: value if value is not None and math.isfinite(value) else None
                                 for metric, value in items.items()} for name, items in losses_by_model.items()}
        cells[horizon] = {"mode": mode, "horizon": horizon, "rows": len(common), "days": len(days),
                          "independent_rows": independent, "excluded_rows": len(group) - len(common),
                          "exclusion_reasons_nonexclusive": exclusions, "losses": reported_losses,
                          "loss_reasons": loss_reasons,
                          "metrics": metrics, "algorithm": algorithms.get(horizon),
                          "inner_sufficient": inner_sufficient, "diagnostics": diagnostics(common)}
    for counts in bootstrap_indices(calendar, mode):
        for horizon in horizons:
            values = vectors[horizon]
            averages = {(name, metric): _weighted_day_mean(values[name, metric], counts)
                        for name in ("model", "raw", "calibrated") for metric in (0, 1)}
            for baseline in ("raw", "calibrated"):
                for metric, metric_name in enumerate(("brier", "logloss")):
                    key = baseline + "_" + metric_name
                    value, reason = _skill(averages["model", metric], averages[baseline, metric])
                    if reason:
                        invalid[horizon, key][reason] += 1
                    else:
                        draws[horizon, key].append(value)
    for horizon, cell in cells.items():
        for key, metric in cell["metrics"].items():
            samples = draws[horizon, key]
            errors = invalid[horizon, key]
            valid = metric["point_reason"] is None and not errors and len(samples) == BOOTSTRAP_REPEATS
            metric.update({"valid_repeats": len(samples), "invalid_repeats": dict(errors),
                           "ci95": [percentile(samples, .025), percentile(samples, .975)] if valid else None,
                           "ci_family": [percentile(samples, .003125), percentile(samples, .996875)] if valid and key == "raw_brier" else None,
                           "ci_reason": None if valid else "BOOTSTRAP_INVALID"})
        cell.update(classify_cell(cell))
    return {"schema": "k45-direction-statistics-v2", "mode": mode, "seed": BOOTSTRAP_SEED,
            "repeats": BOOTSTRAP_REPEATS, "calendar_days": len(calendar), "block_days": 2 if mode == "MINUTE" else 20,
            "cells": list(cells.values()), "mode_conclusion": classify_mode(list(cells.values()))}


def classify_cell(cell: Mapping) -> dict:
    metrics = cell["metrics"]
    raw = metrics["raw_brier"]
    interval = raw.get("ci95")
    half_width = (interval[1] - interval[0]) / 2 if interval and all(math.isfinite(x) for x in interval) else None
    reasons = []
    if cell["days"] < 60:
        reasons.append("COMMON_DAYS_BELOW_60")
    if not cell["inner_sufficient"]:
        reasons.append("INNER_INSUFFICIENT")
    if cell.get("algorithm") is None:
        reasons.append("DIRECTION_MODEL_MISSING")
    def valid_interval(value):
        return isinstance(value, (list, tuple)) and len(value) == 2 and all(
            isinstance(x, (int, float)) and math.isfinite(x) for x in value) and value[0] <= value[1]
    if any(m.get("skill") is None or not math.isfinite(m["skill"]) or not valid_interval(m.get("ci95"))
           for m in metrics.values()) or not valid_interval(raw.get("ci_family")):
        reasons.append("LOSS_SKILL_OR_CI_INVALID")
    resolved = not reasons and half_width is not None and math.isfinite(half_width) and half_width <= RESOLUTION_HALF_WIDTH
    if reasons:
        status, text = "INSUFFICIENT", "本固定设计没有可评估结果"
    elif raw["ci_family"][0] > 0 and metrics["raw_logloss"]["ci95"][0] > 0 and metrics["calibrated_brier"]["ci95"][0] > 0 and cell["algorithm"] != "UNCONDITIONAL_FREQUENCY":
        status, text = "CONTINUE", "在本固定输入与这组特征/模型上出现值得继续检验的信息量"
    elif raw["ci95"][0] > 0 and raw["ci_family"][0] <= 0:
        status, text = "SUGGESTIVE", "提示性改善，未达到预设继续条件"
    elif cell["algorithm"] == "UNCONDITIONAL_FREQUENCY":
        status, text = "BASE_RATE", "基率/温度校准结果，未显示新增特征信息"
    elif raw["ci_family"][0] > 0:
        status, text = "MIXED", "指标证据不一致，或优势未能排除基率校准解释，未达到预设继续条件"
    elif resolved:
        status, text = "NO_INFORMATION", "未显示信息量"
    else:
        status, text = "UNRESOLVED", "这次检验分辨不了"
    return {"status": status, "conclusion": text, "reasons": reasons,
            "half_width95": half_width, "resolved_at_002": resolved}


def classify_mode(cells: Sequence[Mapping]) -> str:
    statuses = {cell["status"] for cell in cells}
    if "CONTINUE" in statuses:
        return "达到继续研究条件；全部期限仍逐项列示"
    if statuses & {"SUGGESTIVE", "BASE_RATE", "MIXED"}:
        return "提示性改善、基率校准或指标证据不一致；未达到预设继续条件"
    if statuses == {"INSUFFICIENT"}:
        return "本固定设计没有可评估结果"
    result = ("现有特征和模型在这份数据上没有显示出信息量" if any(cell["resolved_at_002"] for cell in cells)
              else "这次检验分辨不了")
    if "UNRESOLVED" in statuses:
        result += "；部分期限分辨不了"
    if "INSUFFICIENT" in statuses:
        result += "；部分期限无法评估"
    return result


def diagnostics(rows: Sequence[Observation]) -> dict:
    counts = Counter(row.day for row in rows)
    if not rows:
        return {"reliability": [], "spread_quantiles": {str(q): None for q in QUANTILES},
                "row_equal_quantiles": {str(q): None for q in QUANTILES}, "neutral": None}
    weights = {day: 1 / (len(counts) * count) for day, count in counts.items()}
    reliability = []
    for index, label in enumerate(CLASSES):
        for bin_index in range(10):
            group = [row for row in rows if min(9, int(row.model[index] * 10)) == bin_index]
            weight = math.fsum(weights[row.day] for row in group)
            predicted = math.fsum(weights[row.day] * row.model[index] for row in group) / weight if weight else None
            actual = math.fsum(weights[row.day] * int(row.target == label) for row in group) / weight if weight else None
            reliability.append({"class": label, "bin": bin_index, "rows": len(group),
                                "days": len({row.day for row in group}), "predicted": predicted,
                                "actual": actual, "gap": predicted - actual if weight else None})
    spread = [(row.model[0] - row.model[1], weights[row.day]) for row in rows]
    row_equal = [(value, 1.0) for value, _ in spread]
    strict = math.fsum(weights[row.day] for row in rows if row.model[2] > max(row.model[:2]))
    tied = math.fsum(weights[row.day] for row in rows if row.model[2] == max(row.model) and row.model[2] in row.model[:2])
    return {"reliability": reliability, "spread_quantiles": {str(q): weighted_quantile(spread, q) for q in QUANTILES},
            "row_equal_quantiles": {str(q): weighted_quantile(row_equal, q) for q in QUANTILES},
            "neutral": {"strict_maximum": strict, "tied_maximum": tied, "strict_or_tied": strict + tied}}
