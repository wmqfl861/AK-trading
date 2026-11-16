"""K47: fixed, posthoc decomposition of the already scored K45-R001 inputs.

Only the four pinned input byte strings are research data.  Pure functions also
accept in-memory SYNTHETIC records; the CLI has no alternate analysis profile.
No fitting, prediction, label generation, trading, services or environment reads.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, DecimalException, localcontext
import csv
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import zlib

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from k45_forecast_statistics import (  # pure, frozen helpers only
    BOOTSTRAP_REPEATS, BOOTSTRAP_SEED, CLASSES, bootstrap_indices,
    percentile, probability_error, weighted_quantile,
)

ANALYSIS_ID = "K47-R001-DECOMPOSITION"
HORIZONS = (1, 5, 15, 30, 60)
MODELS = ("model", "raw", "calibrated")
LOSS_NAMES = ("logloss", "move", "direction", "brier", "move_brier", "direction_brier")
GROUPS = ("bull", "bear", "strong_bull", "strong_bear")
MONTHS = tuple(f"2024-{month:02d}" for month in range(6, 11))
METRICS = tuple(f"gain_{control}_{part}" for control in ("raw", "calibrated")
                for part in ("move", "direction", "total")) + (
    "skill_move_brier", "skill_direction_brier", "delta_balance", "delta_ticks",
    "strong_delta_balance", "strong_delta_ticks", "strong_edge_ticks")
REASONS = ("NO_DENOMINATOR", "NO_OBSERVATIONS", "NONFINITE", "TERMINAL_POINTS_INVALID")
INPUT_HASHES = {
    "probabilities": "805a738d81c7470cc85064e96658089c8a2d7b8909f952479a2ca51bed24716c",
    "labels": "a0b82777d84a4661948b18ec7caefb97ab7ffe4d5f7836ee6aade53ce051ee66",
    "calendar": "8450e193eea2a57a6c39362b110bfee4761a53e320663aabb089740034067e4d",
    "published": "c8920cc295fdc9efb53e3d58b08e25768bdf7723e6e2abb46af023bdc5fe7c5c",
}
CALENDAR_HASH = "1fdd43d9ba44adacc916b81f139bd29247b19e018dc5fe0aeaf149ea02e3b872"
DESIGN_HASH = "0dee657a3206864eb7416349a8a400c44e3074aab8771db6be2dc0f784aab7f5"
STATS_HASH = "321ced8d8f22b4d27f8b8e209209a3fe63b4461f42dd0095ddc434a524cbc4b0"
TICK_SOURCE_HASH = "c0768b445925be5f79a36d4520f71bb9944c66d5cbc400ca4ed62662a1b05d04"
LEDGER_PATH = Path("C:/AKR91116/k47/analysis-ledger.json")
REPOSITORY = Path(__file__).resolve().parents[2]
CODE_PATHS = {
    "tool": "tools/experiments/k47_r001_decomposition.py",
    "test": "tests/experiments/test_k47_r001_decomposition.py",
    "design": "docs/research/k47-r001-decomposition-design.md",
    "statistics": "tools/experiments/k45_forecast_statistics.py",
}
OUTPUT_NAMES = ("main.csv", "statistics.csv", "losses.csv", "groups.csv", "monthly.csv",
                "own-frequency.csv", "reproduction.csv", "CHECK.json")
SAFETY = {"enable_trading": False, "observer": "hold", "broker": "NOT_RUN",
          "real_send_count": 0, "real_cancel_count": 0, "holdout_read": False}


class BoundaryError(RuntimeError):
    """Public errors contain a fixed code, never a research row or exception repr."""

    def __init__(self, code, phase="VALIDATION", detail=None):
        super().__init__(code)
        self.code, self.phase, self.detail = code, phase, detail


def require(condition, code, phase="VALIDATION"):
    if not condition:
        raise BoundaryError(code, phase)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def strict_json(raw):
    def invalid_constant(_):
        raise ValueError("NONSTANDARD_JSON")

    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("DUPLICATE_JSON_KEY")
            obj[key] = value
        return obj

    try:
        return json.loads(raw, parse_constant=invalid_constant, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, TypeError):
        raise BoundaryError("JSON_INVALID") from None


@dataclass(frozen=True)
class Number:
    value: float | None
    reason: str | None = None

    def __post_init__(self):
        if self.value is None:
            require(self.reason in REASONS, "MISSING_REASON")
        else:
            require(self.reason is None and math.isfinite(self.value), "NUMBER_NOT_FINITE")


def number(value, missing="NONFINITE"):
    return Number(float(value)) if value is not None and math.isfinite(value) else Number(None, missing)


def difference(left, right):
    if left.reason or right.reason:
        return Number(None, left.reason or right.reason)
    return number(left.value - right.value)


def skill(model, control):
    if model.reason or control.reason:
        return Number(None, model.reason or control.reason)
    if control.value <= 0:
        return Number(None, "NO_DENOMINATOR")
    return number(1 - model.value / control.value)


@dataclass(frozen=True)
class Vector:
    """One numerator/denominator per calendar day; member weights never change."""

    numerator: tuple[float, ...]
    denominator: tuple[float, ...]
    errors: tuple[str | None, ...]
    empty_reason: str = "NO_DENOMINATOR"

    def mean(self, counts):
        active = [(i, count) for i, count in counts if count > 0 and self.denominator[i] > 0]
        if not active:
            return Number(None, self.empty_reason)
        for i, _ in active:
            if self.errors[i]:
                return Number(None, self.errors[i])
        try:
            denominator = math.fsum(count * self.denominator[i] for i, count in active)
            numerator = math.fsum(count * self.numerator[i] for i, count in active)
            return number(numerator / denominator)
        except (ValueError, OverflowError):
            return Number(None, "NONFINITE")


@dataclass(frozen=True)
class Bootstrap:
    calendar: tuple[str, ...]
    draws: tuple[tuple[tuple[int, int], ...], ...]
    sha256: str

    @property
    def point(self):
        return tuple((i, 1) for i in range(len(self.calendar)))


def make_bootstrap(calendar, counts=None):
    """Generate the original sequence once. Injected counts are memory-only tests."""
    calendar = tuple(calendar)
    require(bool(calendar) and list(calendar) == sorted(set(calendar)), "CALENDAR_ORDER")
    source = bootstrap_indices(calendar, "MINUTE") if counts is None else counts
    draws, dense = [], []
    for count in source:
        require(all(type(i) is int and 0 <= i < len(calendar) and type(v) is int and v >= 0
                    for i, v in count.items()) and sum(count.values()) == len(calendar), "BOOTSTRAP_COUNTS")
        draws.append(tuple(sorted((i, v) for i, v in count.items() if v > 0)))
        dense.append([count.get(i, 0) for i in range(len(calendar))])
    require(bool(draws), "BOOTSTRAP_EMPTY")
    # Canonical compact JSON, no newline: exactly the 20000 x 92 integer matrix.
    pin = digest(json.dumps(dense, separators=(",", ":")).encode("utf-8"))
    return Bootstrap(calendar, tuple(draws), pin)


@dataclass
class Distribution:
    point: Number
    samples: list = field(default_factory=list)
    invalid: Counter = field(default_factory=Counter)

    def add(self, value):
        if value.reason:
            self.invalid[value.reason] += 1
        else:
            self.samples.append(value.value)

    def finish(self, repeats, family=True):
        require(len(self.samples) + sum(self.invalid.values()) == repeats, "BOOTSTRAP_ACCOUNTING")
        valid = self.point.reason is None and not self.invalid
        return {"point": self.point.value, "point_reason": self.point.reason,
                "ci95": [percentile(self.samples, p) for p in (.025, .975)] if valid else None,
                "ci_family": [percentile(self.samples, p) for p in (.003125, .996875)] if valid and family else None,
                "ci_reason": None if valid else "BOOTSTRAP_INVALID",
                "valid_repeats": len(self.samples), "invalid_repeats": dict(sorted(self.invalid.items())),
                "estimability": "ESTIMABLE" if valid else "NOT_ESTIMABLE"}


@dataclass(frozen=True)
class Row:
    day: str
    target: str
    model: tuple
    raw: tuple
    calibrated: tuple
    # Opaque until the old-table gate has passed. No identifier is needed by math.
    terminal_points: object


def _probability_problem(value):
    if value is not None and not isinstance(value, (list, tuple)):
        return "PROBABILITY_TYPE"
    try:
        return probability_error(value)
    except (ValueError, OverflowError, TypeError):
        return "PROBABILITY_NONFINITE"


def pair_records(predictions, labels, calendar, horizons=HORIZONS):
    require(isinstance(predictions, list) and isinstance(labels, list), "RECORD_ARRAY_REQUIRED")
    required_p = {"request_id", "day", "mode", "horizon", "model", "raw", "calibrated"}
    required_l = {"request_id", "status", "direction", "horizon", "contract_id", "terminal_points"}
    by_label, seen = {}, set()
    for label in labels:
        require(isinstance(label, dict) and required_l <= label.keys(), "LABEL_STRUCTURE")
        identity = label["request_id"]
        require(isinstance(identity, str) and bool(identity), "REQUEST_ID_TYPE")
        require(identity not in by_label, "DUPLICATE_LABEL")
        by_label[identity] = label
    rows = {h: [] for h in horizons}
    population = {h: {"input_rows": 0, "independent_rows": dict.fromkeys(MODELS, 0),
                      "exclusion_reasons_nonexclusive": Counter()} for h in horizons}
    for prediction in predictions:
        require(isinstance(prediction, dict) and required_p <= prediction.keys(), "PREDICTION_STRUCTURE")
        identity, horizon, day = prediction["request_id"], prediction["horizon"], prediction["day"]
        require(isinstance(identity, str) and bool(identity), "REQUEST_ID_TYPE")
        require(identity not in seen, "DUPLICATE_PREDICTION")
        seen.add(identity)
        require(prediction["mode"] == "MINUTE" and type(horizon) is int and horizon in horizons
                and isinstance(day, str) and day in calendar, "PREDICTION_SCOPE")
        pop = population[horizon]
        pop["input_rows"] += 1
        label = by_label.get(identity)
        if label is not None:
            label_h = label["horizon"]
            require(isinstance(label_h, dict) and label_h.get("unit") == "TRADING_MINUTE"
                    and type(label_h.get("value")) is int and label_h["value"] == horizon, "LABEL_HORIZON_CONFLICT")
            require(isinstance(label["contract_id"], str) and
                    re.fullmatch(r"SHFE\.rb[0-9]{4}", label["contract_id"], re.IGNORECASE) is not None,
                    "LABEL_CONTRACT_CONFLICT")
            for key in ("contract_id", "day", "mode"):
                if key in prediction and key in label:
                    require(prediction[key] == label[key], "PAIR_IDENTITY_CONFLICT")
        if label is None or label["status"] != "MATURE" or label["direction"] not in CLASSES:
            pop["exclusion_reasons_nonexclusive"]["DIRECTION_LABEL_MISSING"] += 1
            continue
        problems = {}
        for name in MODELS:
            problem = _probability_problem(prediction[name])
            if problem:
                problems[name] = problem
                pop["exclusion_reasons_nonexclusive"][name + ":" + problem] += 1
            else:
                pop["independent_rows"][name] += 1
        if not problems:
            rows[horizon].append(Row(day, label["direction"], *(tuple(prediction[n]) for n in MODELS),
                                     label["terminal_points"]))
    require(set(by_label) <= seen, "UNEXPLAINED_EXTRA_LABEL")
    for horizon, group in rows.items():
        population[horizon].update(common_rows=len(group), common_days=len({row.day for row in group}),
                                   excluded_rows=population[horizon]["input_rows"] - len(group))
        population[horizon]["exclusion_reasons_nonexclusive"] = dict(sorted(
            population[horizon]["exclusion_reasons_nonexclusive"].items()))
    return rows, population


def basic_losses(probabilities, target):
    index = CLASSES.index(target)
    probability = probabilities[index]
    return {"brier": math.fsum((p - int(i == index)) ** 2 for i, p in enumerate(probabilities)),
            "logloss": -math.log(probability) if probability > 0 else math.inf}


def day_vectors(rows, calendar, evaluator, names):
    by_day = defaultdict(list)
    for row in rows:
        by_day[row.day].append(evaluator(row))
    result = {}
    for name in names:
        numerators, denominators, errors = [], [], []
        for day in calendar:
            values = [entry[name] for entry in by_day[day]]
            denominator = int(bool(values))
            invalid = bool(values) and any(not math.isfinite(value) for value in values)
            numerators.append(0.0 if not values or invalid else math.fsum(values) / len(values))
            denominators.append(denominator)
            errors.append("NONFINITE" if invalid else None)
        result[name] = Vector(tuple(numerators), tuple(denominators), tuple(errors), "NO_OBSERVATIONS")
    return result


def reproduce(rows_by_horizon, populations, bootstrap):
    """Only the original three-class losses, populations and four skills."""
    result = {}
    keys = tuple((model, loss) for model in MODELS for loss in ("brier", "logloss"))
    for horizon, rows in rows_by_horizon.items():
        def evaluate(row):
            return {(model, loss): value for model in MODELS
                    for loss, value in basic_losses(getattr(row, model), row.target).items()}
        vectors = day_vectors(rows, bootstrap.calendar, evaluate, keys)
        losses = {key: vector.mean(bootstrap.point) for key, vector in vectors.items()}
        distributions = {f"{control}_{loss}": Distribution(skill(losses["model", loss], losses[control, loss]))
                         for control in ("raw", "calibrated") for loss in ("brier", "logloss")}
        for counts in bootstrap.draws:
            averages = {key: vector.mean(counts) for key, vector in vectors.items()}
            for control in ("raw", "calibrated"):
                for loss in ("brier", "logloss"):
                    distributions[f"{control}_{loss}"].add(skill(averages["model", loss], averages[control, loss]))
        result[horizon] = {"population": populations[horizon], "losses": losses,
                           "metrics": {key: distribution.finish(len(bootstrap.draws), key == "raw_brier")
                                       for key, distribution in distributions.items()}}
    return result


def reproduction_fields(cell):
    pop = cell["population"]
    fields = {name: pop[name] for name in ("common_rows", "common_days", "excluded_rows")}
    fields["exclusion_reasons_nonexclusive"] = pop["exclusion_reasons_nonexclusive"]
    for model in MODELS:
        fields[model + "_independent_rows"] = pop["independent_rows"][model]
        for loss in ("brier", "logloss"):
            item = cell["losses"][model, loss]
            fields[model + "_" + loss] = item.value
            fields[model + "_" + loss + "_reason"] = item.reason
    for key, metric in cell["metrics"].items():
        for source, suffix in (("point", "skill"), ("point_reason", "point_reason"),
                               ("valid_repeats", "valid_repeats"), ("invalid_repeats", "invalid_repeats"),
                               ("ci_reason", "ci_reason")):
            fields[key + "_" + suffix] = metric[source]
        for interval in ("ci95", "ci_family"):
            for i, end in enumerate(("lower", "upper")):
                fields[key + "_" + interval + "_" + end] = metric[interval][i] if metric[interval] else None
    return fields


def compare_published(reproduction, published):
    comparisons = []
    for horizon, cell in reproduction.items():
        require(horizon in published, "PUBLISHED_HORIZON_MISSING", "REPRODUCTION")
        for key, actual in reproduction_fields(cell).items():
            require(key in published[horizon], "PUBLISHED_FIELD_MISSING", "REPRODUCTION")
            encoded = published[horizon][key]
            expected, delta, matches = None, None, False
            try:
                if encoded == "NA":
                    matches = actual is None
                elif actual is None:
                    expected = encoded
                elif isinstance(actual, dict):
                    expected = strict_json(encoded)
                    matches = expected == actual
                elif type(actual) is int:
                    expected = int(encoded)
                    matches = str(expected) == encoded and expected == actual
                elif isinstance(actual, float):
                    expected = float(encoded)
                    if math.isfinite(expected) and math.isfinite(actual):
                        delta = abs(expected - actual)
                        matches = delta <= 1e-9
                    else:
                        expected = "NONFINITE"
                else:
                    expected = encoded
                    matches = expected == actual
            except (ValueError, TypeError, BoundaryError):
                expected = "INVALID_ENCODING"
            comparisons.append({"horizon": horizon, "field": key, "expected": expected, "actual": actual,
                                "abs_diff": delta, "status": "MATCH" if matches else "DIFFERENCE"})
    report = {"comparisons": len(comparisons), "differences": sum(row["status"] != "MATCH" for row in comparisons),
              "max_abs_diff": max((row["abs_diff"] or 0 for row in comparisons), default=0)}
    if report["differences"]:
        raise BoundaryError("PUBLISHED_REPRODUCTION_MISMATCH", "REPRODUCTION", report)
    return comparisons, report


def split_losses(probabilities, target, identity):
    losses = basic_losses(probabilities, target)
    u, v, z = probabilities
    q = u + v
    moving = target != "NEUTRAL"
    py = probabilities[CLASSES.index(target)]
    move = (-math.log(q) if q > 0 else math.inf) if moving else losses["logloss"]
    direction = (math.log(q) - math.log(py) if q > 0 and py > 0 else math.inf) if moving else 0.0
    direction_brier = ((u / q - int(target == "UP")) ** 2 if q > 0 else math.inf) if moving else 0.0
    if moving and q == 0:
        identity["zero_move_denominator_rows"] += 1
    if all(math.isfinite(value) for value in (losses["logloss"], move, direction)):
        delta = abs(move + direction - losses["logloss"])
        require(delta <= 1e-12, "ROW_LOGLOSS_IDENTITY", "DECOMPOSITION")
        identity["finite_checks"] += 1
        identity["max_finite_abs_diff"] = max(identity["max_finite_abs_diff"], delta)
    else:
        require(losses["logloss"] == math.inf and move + direction == math.inf
                and move != -math.inf and direction != -math.inf,
                "EXTENDED_LOGLOSS_IDENTITY", "DECOMPOSITION")
        identity["extended_positive_infinity_matches"] += 1
    return dict(losses, move=move, direction=direction, move_brier=(q - int(moving)) ** 2,
                direction_brier=direction_brier)


def terminal_ticks(value, tick):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return Number(None, "TERMINAL_POINTS_INVALID")
    try:
        with localcontext() as context:
            context.prec = 50
            points = Decimal(str(value))
            if not points.is_finite():
                return Number(None, "TERMINAL_POINTS_INVALID")
            converted = float(points / tick)
        return number(converted, "TERMINAL_POINTS_INVALID")
    except (DecimalException, ValueError, OverflowError):
        return Number(None, "TERMINAL_POINTS_INVALID")


def group_vectors(rows, calendar, tick):
    sizes = Counter(row.day for row in rows)
    signals = [row.model[0] - row.model[1] for row in rows]
    weighted = [(signal, 1 / sizes[row.day]) for row, signal in zip(rows, signals)]
    q10, q90 = weighted_quantile(weighted, .1), weighted_quantile(weighted, .9)
    ticks = [terminal_ticks(row.terminal_points, tick) for row in rows]
    members = {name: [] for name in (*GROUPS, "zero")}
    for row, signal, tick_value in zip(rows, signals, ticks):
        flags = {"bull": signal > 0, "bear": signal < 0, "zero": signal == 0,
                 "strong_bull": signal > 0 and signal >= q90,
                 "strong_bear": signal < 0 and signal <= q10}
        for name, included in flags.items():
            if included:
                members[name].append((row, tick_value))
    require(sum(len(members[n]) for n in ("bull", "bear", "zero")) == len(rows), "GROUP_CONSERVATION")
    vectors, descriptions = {}, {}
    for name, group in members.items():
        by_day = defaultdict(list)
        for row, tick_value in group:
            by_day[row.day].append((row, tick_value))
        denominators, balances, tick_sums, tick_errors = [], [], [], []
        for day in calendar:
            entries = by_day[day]
            scale = 1 / sizes[day] if sizes[day] else 0
            denominators.append(len(entries) * scale)
            balances.append(math.fsum((1 if row.target == "UP" else -1 if row.target == "DOWN" else 0)
                                      for row, _ in entries) * scale)
            bad = any(value.reason for _, value in entries)
            tick_error = "TERMINAL_POINTS_INVALID" if bad else None
            try:
                tick_sum = 0.0 if bad else math.fsum(value.value for _, value in entries) * scale
            except (OverflowError, ValueError):
                tick_sum, tick_error = 0.0, "NONFINITE"
            tick_sums.append(tick_sum)
            tick_errors.append(tick_error)
        for metric, numerator, errors in (("balance", balances, [None] * len(calendar)),
                                           ("ticks", tick_sums, tick_errors)):
            vectors[name, metric] = Vector(tuple(numerator), tuple(denominators), tuple(errors))
        descriptions[name] = {"rows": len(group), "days": len(by_day) - sum(not v for v in by_day.values()),
                              "weight": math.fsum(denominators),
                              "invalid_terminal_points_rows": sum(bool(value.reason) for _, value in group)}
    return vectors, descriptions, {"q10": q10, "q90": q90,
                                   "invalid_terminal_points_rows": sum(bool(value.reason) for value in ticks)}


def _identity_counts():
    return {"finite_checks": 0, "max_finite_abs_diff": 0.0,
            "extended_positive_infinity_matches": 0, "zero_move_denominator_rows": 0}


def registered_values(losses, groups, identity=None):
    values = {}
    for control in ("raw", "calibrated"):
        for part, loss in (("move", "move"), ("direction", "direction"), ("total", "logloss")):
            values[f"gain_{control}_{part}"] = difference(losses[control, loss], losses["model", loss])
        total, move, direction = (values[f"gain_{control}_{part}"] for part in ("total", "move", "direction"))
        if all(item.reason is None for item in (total, move, direction)):
            delta = abs(total.value - (move.value + direction.value))
            require(delta <= 1e-12, "AGGREGATE_GAIN_IDENTITY", "DECOMPOSITION")
            if identity is not None:
                identity["finite_checks"] += 1
                identity["max_finite_abs_diff"] = max(identity["max_finite_abs_diff"], delta)
        elif identity is not None:
            identity["nonfinite_checks"] += 1
    for part in ("move", "direction"):
        values[f"skill_{part}_brier"] = skill(losses["model", part + "_brier"], losses["raw", part + "_brier"])
    for prefix, bull, bear in (("", "bull", "bear"), ("strong_", "strong_bull", "strong_bear")):
        for metric in ("balance", "ticks"):
            values[prefix + "delta_" + metric] = difference(groups[bull, metric], groups[bear, metric])
    edge = values["strong_delta_ticks"]
    values["strong_edge_ticks"] = number(edge.value / 2) if edge.reason is None else edge
    require(tuple(values) == METRICS, "METRIC_REGISTRY")
    return values


def classify_direction(gain, delta):
    def endpoint(metric, interval, index):
        pair = metric.get(interval)
        value = pair[index] if isinstance(pair, (list, tuple)) and len(pair) == 2 else None
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None

    family, g_low, g_high, d_low = (endpoint(gain, "ci_family", 0), endpoint(gain, "ci95", 0),
                                  endpoint(gain, "ci95", 1), endpoint(delta, "ci95", 0))
    if family is not None and family > 0 and d_low is not None and d_low > 0:
        return "显示出方向信息"
    if (g_low is not None and g_low > 0) or (d_low is not None and d_low > 0):
        return "提示性"
    if g_high is not None and g_high < 0:
        return "方向部分为负"
    return "没有显示方向信息"


def classify_cost(edge):
    pair = edge.get("ci95")
    if pair and len(pair) == 2:
        if pair[0] is not None and math.isfinite(pair[0]) and pair[0] > 1:
            return "付得起价差"
        if pair[1] is not None and math.isfinite(pair[1]) and pair[1] < 1:
            return "付不起价差"
    return "分辨不了"


def j52(cells):
    require(5 in cells and 15 in cells, "J52_REQUIRED_HORIZONS")
    costs = [cells[h]["cost_reading"] for h in (5, 15)]
    branch = "甲" if "付得起价差" in costs else "乙" if "分辨不了" in costs else "丙"
    return {"branch": branch, "optional_k47d": branch == "丙" and any(
        cells[h]["direction_reading"] == "显示出方向信息" for h in (5, 15)),
        "followup": "K48_K49" if branch in ("甲", "乙") else "ROOT_DECIDES_K47d_OR_END",
        "engineering_resumed": False}


def own_frequency(rows, calendar):
    counts = Counter(row.day for row in rows)
    if not rows:
        return {"frequency": {label: Number(None, "NO_OBSERVATIONS") for label in CLASSES},
                "brier": Number(None, "NO_OBSERVATIONS"), "logloss": Number(None, "NO_OBSERVATIONS")}
    frequency = tuple(math.fsum(sum(row.target == label and row.day == day for row in rows) / counts[day]
                               for day in calendar if counts[day]) / len(counts) for label in CLASSES)
    require(abs(math.fsum(frequency) - 1) <= 1e-12, "OWN_FREQUENCY_SUM")
    vectors = day_vectors(rows, calendar, lambda row: basic_losses(frequency, row.target), ("brier", "logloss"))
    point = tuple((i, 1) for i in range(len(calendar)))
    return {"frequency": {label: number(value) for label, value in zip(CLASSES, frequency)},
            **{key: vector.mean(point) for key, vector in vectors.items()}}


def decompose(rows_by_horizon, bootstrap, reproduction, tick=Decimal("1")):
    require(isinstance(tick, Decimal) and tick.is_finite() and tick > 0, "TICK_INVALID")
    cells = {}
    for horizon, rows in rows_by_horizon.items():
        identities = {model: _identity_counts() for model in MODELS}
        def evaluate(row):
            return {(model, loss): value for model in MODELS
                    for loss, value in split_losses(getattr(row, model), row.target, identities[model]).items()}
        loss_vectors = day_vectors(rows, bootstrap.calendar, evaluate,
                                   tuple((model, loss) for model in MODELS for loss in LOSS_NAMES))
        group_panels, descriptions, quantiles = group_vectors(rows, bootstrap.calendar, tick)
        losses = {key: vector.mean(bootstrap.point) for key, vector in loss_vectors.items()}
        groups = {key: vector.mean(bootstrap.point) for key, vector in group_panels.items()}
        aggregate = {"finite_checks": 0, "nonfinite_checks": 0, "max_finite_abs_diff": 0.0,
                     "published_total_checks": 0, "published_total_max_abs_diff": 0.0}
        points = registered_values(losses, groups, aggregate)
        for control in ("raw", "calibrated"):
            old = difference(reproduction[horizon]["losses"][control, "logloss"],
                             reproduction[horizon]["losses"]["model", "logloss"])
            new = points[f"gain_{control}_total"]
            require(old.reason == new.reason, "TOTAL_REPRODUCTION_STATE", "DECOMPOSITION")
            if old.reason is None:
                delta = abs(old.value - new.value)
                require(delta <= 1e-9, "TOTAL_REPRODUCTION_IDENTITY", "DECOMPOSITION")
                aggregate["published_total_checks"] += 1
                aggregate["published_total_max_abs_diff"] = max(aggregate["published_total_max_abs_diff"], delta)
        distributions = {key: Distribution(value) for key, value in points.items()}
        for counts in bootstrap.draws:
            sampled = registered_values({key: vector.mean(counts) for key, vector in loss_vectors.items()},
                                        {key: vector.mean(counts) for key, vector in group_panels.items()}, aggregate)
            for key, value in sampled.items():
                distributions[key].add(value)
        metrics = {key: distribution.finish(len(bootstrap.draws)) for key, distribution in distributions.items()}
        monthly = []
        for month in MONTHS:
            counts = tuple((i, 1) for i, day in enumerate(bootstrap.calendar) if day.startswith(month))
            monthly_losses = {key: vector.mean(counts) for key, vector in loss_vectors.items()}
            month_rows = [row for row in rows if row.day.startswith(month)]
            monthly.append({"month": month, "rows": len(month_rows), "days": len({row.day for row in month_rows}),
                            "raw_brier_skill": skill(monthly_losses["model", "brier"], monthly_losses["raw", "brier"]),
                            "gain_raw_move": difference(monthly_losses["raw", "move"], monthly_losses["model", "move"]),
                            "gain_raw_direction": difference(monthly_losses["raw", "direction"], monthly_losses["model", "direction"])})
        cells[horizon] = {"metrics": metrics, "losses": losses, "groups": groups, "group_counts": descriptions,
                          "quantiles": quantiles, "identity": identities, "aggregate_identity": aggregate,
                          "monthly": monthly, "own_frequency": own_frequency(rows, bootstrap.calendar),
                          "direction_reading": classify_direction(metrics["gain_raw_direction"], metrics["delta_balance"]),
                          "cost_reading": classify_cost(metrics["strong_edge_ticks"])}
    return cells


@dataclass(frozen=True)
class InputPolicy:
    """Internal byte-loader contract. The production CLI always uses FORMAL."""

    hashes: dict
    calendar_hash: str
    start: str
    end: str
    calendar_days: int
    session_records: int
    common_rows: int
    common_days: int
    horizons: tuple = HORIZONS


FORMAL = InputPolicy(INPUT_HASHES, CALENDAR_HASH, "2024-06-03", "2024-10-18", 92, 1795, 5838, 91)


def fixed_config():
    return {"mode": "MINUTE", "horizons": list(HORIZONS), "start": FORMAL.start, "end": FORMAL.end,
            "calendar_days": 92, "calendar_sha256": CALENDAR_HASH, "common_rows_per_horizon": 5838,
            "common_days_per_horizon": 91, "seed": 20261113, "repeats": 20000, "block_days": 2,
            "ci95": [.025, .975], "ci_family": [.003125, .996875], "family_horizons": 8,
            "metrics": list(METRICS), "tick": "1", "tick_source": "R9_NEXT_BATCH_3_4_DECLARED",
            "tick_rules_revalidated": False, "tick_source_sha256": TICK_SOURCE_HASH}


def decode_inputs(payloads, policy):
    """Hash all four already-read byte strings, then decode those same strings."""
    require(set(payloads) == set(policy.hashes) == set(INPUT_HASHES), "INPUT_WHITELIST")
    for name, raw in payloads.items():
        require(type(raw) is bytes and digest(raw) == policy.hashes[name], "INPUT_HASH_MISMATCH")
    sessions = strict_json(payloads["calendar"])
    require(isinstance(sessions, dict) and isinstance(sessions.get("records"), list)
            and len(sessions["records"]) == policy.session_records, "CALENDAR_ENVELOPE")
    dates = set()
    for session in sessions["records"]:
        require(isinstance(session, dict) and isinstance(session.get("trading_day"), str), "CALENDAR_STRUCTURE")
        day = session["trading_day"]
        try:
            require(date.fromisoformat(day).isoformat() == day, "CALENDAR_DATE")
        except ValueError:
            raise BoundaryError("CALENDAR_DATE") from None
        if policy.start <= day <= policy.end:
            dates.add(day)
    calendar = tuple(sorted(dates))
    require(len(calendar) == policy.calendar_days and calendar[0] == policy.start and calendar[-1] == policy.end,
            "CALENDAR_POPULATION")
    # The author fixed this specific date-array encoding, including its newline.
    dates_hash = digest((json.dumps(calendar, separators=(",", ":")) + "\n").encode("utf-8"))
    require(dates_hash == policy.calendar_hash, "CALENDAR_HASH_MISMATCH")
    try:
        predictions = strict_json(gzip.decompress(payloads["probabilities"]))
        labels = strict_json(gzip.decompress(payloads["labels"]))
        reader = csv.DictReader(io.StringIO(payloads["published"].decode("utf-8"), newline=""))
        require(reader.fieldnames is not None and len(reader.fieldnames) == len(set(reader.fieldnames)), "PUBLISHED_COLUMNS")
        published_rows = list(reader)
    except (OSError, EOFError, UnicodeError, csv.Error, zlib.error):
        raise BoundaryError("INPUT_ENCODING") from None
    published = {}
    seen = set()
    for row in published_rows:
        require(None not in row and all(v is not None for v in row.values()), "PUBLISHED_STRUCTURE")
        require(row.get("mode") in ("MINUTE", "DAILY"), "PUBLISHED_MODE")
        try:
            horizon = int(row["horizon"])
        except (ValueError, KeyError):
            raise BoundaryError("PUBLISHED_HORIZON") from None
        identity = row["mode"], horizon
        require(identity not in seen, "PUBLISHED_DUPLICATE")
        seen.add(identity)
        if row["mode"] == "MINUTE":
            require(horizon in policy.horizons, "PUBLISHED_HORIZON")
            require(row.get("probabilities_sha256") == policy.hashes["probabilities"]
                    and row.get("labels_sha256") == policy.hashes["labels"], "PUBLISHED_GZIP_IDENTITY")
            published[horizon] = row
        else:
            require(horizon in (1, 5, 20), "PUBLISHED_HORIZON")
    require(set(published) == set(policy.horizons), "PUBLISHED_HORIZON_FAMILY")
    rows, population = pair_records(predictions, labels, calendar, policy.horizons)
    for pop in population.values():
        require(pop["common_rows"] == policy.common_rows and pop["common_days"] == policy.common_days
                and pop["excluded_rows"] == 0 and all(value == policy.common_rows for value in pop["independent_rows"].values()),
                "COMMON_POPULATION_MISMATCH")
    return calendar, rows, population, published


def analyze_bytes(payloads, policy, *, bootstrap=None):
    """Complete memory-only chain for formal bytes or explicitly SYNTHETIC tests."""
    calendar, rows, population, published = decode_inputs(payloads, policy)
    if bootstrap is None:
        bootstrap = make_bootstrap(calendar)
    require(bootstrap.calendar == calendar, "BOOTSTRAP_CALENDAR_MISMATCH")
    if policy is FORMAL:
        require(BOOTSTRAP_REPEATS == 20000 and BOOTSTRAP_SEED == 20261113
                and len(bootstrap.draws) == 20000, "FIXED_BOOTSTRAP_CONFIG")
    reproduction = reproduce(rows, population, bootstrap)
    comparisons, comparison_report = compare_published(reproduction, published)
    # No terminal numeric conversion or decomposition occurs above this gate.
    cells = decompose(rows, bootstrap, reproduction)
    return {"cells": cells, "population": population, "published": published,
            "comparisons": comparisons, "reproduction_report": comparison_report,
            "bootstrap": {"sha256": bootstrap.sha256, "repeats": len(bootstrap.draws),
                          "calendar_days": len(calendar), "encoding": "compact-json-dense-counts-no-newline"},
            "empty_dates": {str(h): [day for day in calendar if day not in {row.day for row in group}]
                            for h, group in rows.items()}, "j52": j52(cells)}


def number_fields(name, item):
    return {name: item.value, name + "_reason": item.reason}


def metric_fields(item):
    result = {name: item[name] for name in ("point", "point_reason", "ci_reason", "valid_repeats", "estimability")}
    result["invalid_repeats"] = item["invalid_repeats"]
    result["invalid_repeats_total"] = sum(item["invalid_repeats"].values())
    for reason in REASONS:
        result["invalid_" + reason] = item["invalid_repeats"].get(reason, 0)
    for interval in ("ci95", "ci_family"):
        for index, end in enumerate(("lower", "upper")):
            result[interval + "_" + end] = item[interval][index] if item[interval] else None
    return result


def csv_bytes(rows):
    require(bool(rows), "EMPTY_OUTPUT_TABLE", "EXPORT")
    fields = list(rows[0])
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fields, lineterminator="\n", extrasaction="raise")
    writer.writeheader()
    for row in rows:
        require(list(row) == fields, "OUTPUT_COLUMNS", "EXPORT")
        encoded = {}
        for key, value in row.items():
            require(not isinstance(value, float) or math.isfinite(value), "OUTPUT_NONFINITE", "EXPORT")
            encoded[key] = ("NA" if value is None else json.dumps(value, ensure_ascii=False, allow_nan=False,
                            sort_keys=True, separators=(",", ":")) if isinstance(value, (dict, list)) else value)
        writer.writerow(encoded)
    return stream.getvalue().encode("utf-8")


def aggregate_outputs(result, runtime_identity):
    main, statistics, losses, groups, monthly, frequency = [], [], [], [], [], []
    for horizon in HORIZONS:
        cell = result["cells"][horizon]
        main_row = {"horizon": horizon, "algorithm": result["published"][horizon].get("algorithm"),
                    "common_rows": result["population"][horizon]["common_rows"],
                    "common_days": result["population"][horizon]["common_days"]}
        for key in ("gain_raw_total", "gain_raw_move", "gain_raw_direction", "delta_balance", "strong_edge_ticks"):
            for name, value in metric_fields(cell["metrics"][key]).items():
                main_row[key + "_" + name] = value
        main_row.update(direction_reading=cell["direction_reading"], cost_reading=cell["cost_reading"],
                        j52_branch=result["j52"]["branch"], j52_input=horizon in (5, 15),
                        tick="1", tick_source="R9_NEXT_BATCH_3_4_DECLARED", tick_rules_revalidated=False)
        main.append(main_row)
        for key in METRICS:
            statistics.append({"horizon": horizon, "metric": key, **metric_fields(cell["metrics"][key])})
        for model in MODELS:
            for loss in LOSS_NAMES:
                losses.append({"horizon": horizon, "model": model, "loss": loss,
                               **number_fields("mean", cell["losses"][model, loss])})
        for group in (*GROUPS, "zero"):
            groups.append({"horizon": horizon, "group": group, "q10": cell["quantiles"]["q10"],
                           "q90": cell["quantiles"]["q90"], **cell["group_counts"][group],
                           **number_fields("balance", cell["groups"][group, "balance"]),
                           **number_fields("ticks", cell["groups"][group, "ticks"])})
        for item in cell["monthly"]:
            monthly_row = {"horizon": horizon, "month": item["month"], "rows": item["rows"], "days": item["days"]}
            for key in ("raw_brier_skill", "gain_raw_move", "gain_raw_direction"):
                monthly_row.update(number_fields(key, item[key]))
            monthly.append(monthly_row)
        own = cell["own_frequency"]
        frequency_row = {"horizon": horizon, "population": "ALREADY_SCORED_TEST_DESCRIPTIVE_ONLY"}
        for label, value in own["frequency"].items():
            frequency_row.update(number_fields(label, value))
        for loss in ("brier", "logloss"):
            frequency_row.update(number_fields(loss, own[loss]))
        frequency.append(frequency_row)
    require(len(main) == 5 and len(statistics) == 65 and len(monthly) == 25 and len(frequency) == 5,
            "OUTPUT_ROW_COUNTS", "EXPORT")
    payloads = {name: csv_bytes(table) for name, table in zip(OUTPUT_NAMES[:-1],
                (main, statistics, losses, groups, monthly, frequency, result["comparisons"]))}
    check = {"schema": "k47-runtime-check-v1", "analysis_id": ANALYSIS_ID,
             "analysis_kind": "POSTHOC_OF_ALREADY_SCORED_R001", "scenario": "SCENARIO",
             "runtime": runtime_identity, "publication": {"status": "AWAITING_ROOT_TERMINAL_RECEIPT_AND_INDEPENDENT_RECOMPUTATION"},
             "config": fixed_config(), "reproduction": result["reproduction_report"],
             "population": {str(key): value for key, value in result["population"].items()},
             "calendar_sha256": CALENDAR_HASH, "empty_dates": result["empty_dates"], "bootstrap": result["bootstrap"],
             "identity": {str(h): {"rows": cell["identity"], "aggregate": cell["aggregate_identity"]}
                          for h, cell in result["cells"].items()},
             "invalid_terminal_points_rows": {str(h): cell["quantiles"]["invalid_terminal_points_rows"]
                                              for h, cell in result["cells"].items()},
             "metrics": {str(h): cell["metrics"] for h, cell in result["cells"].items()},
             "j52": result["j52"], "posthoc_analysis_count": 1, "research_run_count": 1,
             "fit_calls": 0, "prediction_calls": 0, "label_calls": 0, "research_scoring_calls": 0,
             "safety": SAFETY, "artifacts": {name: {"bytes": len(raw), "sha256": digest(raw)}
                                             for name, raw in payloads.items()}}
    payloads["CHECK.json"] = json_bytes(check)
    return payloads


def _read_bytes(path):
    try:
        return Path(path).read_bytes()
    except OSError:
        raise BoundaryError("REQUIRED_FILE_UNREADABLE") from None


def _pin(path, raw):
    return {"path": str(Path(path).resolve()), "bytes": len(raw), "sha256": digest(raw)}


def _read_metadata_pin(pin):
    require(isinstance(pin, dict) and set(pin) == {"path", "bytes", "sha256"}, "METADATA_PIN")
    require(isinstance(pin["path"], str), "METADATA_PATH")
    path = Path(pin["path"]).resolve()
    require(path.suffix.lower() == ".json" and not path.name.lower().startswith(".env"), "METADATA_PATH")
    raw = _read_bytes(path)
    require(len(raw) == pin["bytes"] and digest(raw) == pin["sha256"], "METADATA_HASH_MISMATCH")
    return strict_json(raw)


def _git_bytes(arguments):
    try:
        process = subprocess.run(["git", *arguments], cwd=REPOSITORY, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise BoundaryError("CODE_COMMIT_UNVERIFIED", "RELEASE") from None
    require(process.returncode == 0 and not process.stderr, "CODE_COMMIT_UNVERIFIED", "RELEASE")
    return process.stdout


def verify_release(release_path, paths, output):
    """Process metadata is validated before any of the four research files open."""
    release_path = Path(release_path).resolve()
    require(release_path.suffix.lower() == ".json" and not release_path.name.lower().startswith(".env"), "RELEASE_PATH")
    raw = _read_bytes(release_path)
    release = strict_json(raw)
    require(isinstance(release, dict) and release.get("schema") == "k47-release-v1"
            and release.get("analysis_id") == ANALYSIS_ID, "RELEASE_SCHEMA", "RELEASE")
    require(release.get("config") == fixed_config() and release.get("input_hashes") == INPUT_HASHES,
            "RELEASE_FIXED_CONFIG", "RELEASE")
    require(isinstance(release.get("attempt_id"), str) and
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", release["attempt_id"]) is not None,
            "RELEASE_ATTEMPT", "RELEASE")
    require(isinstance(release.get("ledger_path"), str) and Path(release["ledger_path"]).resolve() == LEDGER_PATH.resolve(),
            "RELEASE_LEDGER", "RELEASE")
    require(isinstance(release.get("commit"), str) and re.fullmatch(r"[0-9a-f]{40}", release["commit"]) is not None,
            "RELEASE_COMMIT", "RELEASE")
    require(release.get("integrated_main") is True and release.get("code_hashes", {}).get("design") == DESIGN_HASH
            and release.get("code_hashes", {}).get("statistics") == STATS_HASH, "RELEASE_CODE_IDENTITY", "RELEASE")
    require(_git_bytes(["rev-parse", "HEAD"]).decode("ascii").strip() == release["commit"], "CHECKOUT_COMMIT", "RELEASE")
    require(set(release.get("code_hashes", {})) == set(CODE_PATHS), "RELEASE_CODE_SET", "RELEASE")
    code = {}
    for name, relative in CODE_PATHS.items():
        source = _read_bytes(REPOSITORY / relative)
        require(digest(source) == release["code_hashes"][name] == digest(_git_bytes(["show", release["commit"] + ":" + relative])),
                "SOURCE_HASH_MISMATCH", "RELEASE")
        code[name] = {"path": relative, "sha256": digest(source), "bytes": len(source), "commit": release["commit"]}
    require(set(release.get("reviews", {})) == {"boundary", "numerical"}, "RELEASE_REVIEWS", "RELEASE")
    for kind in ("boundary", "numerical"):
        review = _read_metadata_pin(release["reviews"][kind])
        require(isinstance(review, dict) and review.get("verdict") == "PASS" and review.get("conditional") is False
                and review.get("independent") is True
                and review.get("review_scope") == f"K47_{kind.upper()}_HARD_REVIEW"
                and review.get("code_hashes") == release["code_hashes"],
                "RELEASE_HARD_REVIEW", "RELEASE")
    ci = _read_metadata_pin(release.get("ci"))
    require(isinstance(ci, dict) and ci.get("conclusion") == "success" and ci.get("commit") == release["commit"],
            "RELEASE_CI", "RELEASE")
    require(not output.exists() and not output.is_relative_to(REPOSITORY), "OUTPUT_NOT_FRESH_EXTERNAL", "RELEASE")
    require(set(paths) == set(INPUT_HASHES), "INPUT_WHITELIST", "RELEASE")
    expected_names = {"probabilities": "MINUTE-probabilities.json.gz", "labels": "MINUTE-labels.json.gz",
                      "calendar": "sessions.json", "published": "main.csv"}
    require(set(release.get("inputs", {})) == set(paths), "RELEASE_INPUT_SET", "RELEASE")
    for name, path in paths.items():
        pin = release["inputs"][name]
        require(path.name == expected_names[name] and isinstance(pin, dict)
                and set(pin) == {"path", "bytes", "sha256"} and isinstance(pin["path"], str)
                and Path(pin["path"]).resolve() == path and pin["sha256"] == INPUT_HASHES[name]
                and type(pin["bytes"]) is int and pin["bytes"] > 0, "RELEASE_INPUT_IDENTITY", "RELEASE")
    return release, _pin(release_path, raw), code


def atomic_json(path, value, *, replace=False):
    """Serialize first; a sibling temp plus rename publishes a complete JSON file."""
    path = Path(path)
    raw = json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".writing")
    require(not temporary.exists() and (replace or not path.exists()), "METADATA_ALREADY_EXISTS", "LEDGER")
    with temporary.open("xb") as stream:
        stream.write(raw)
    if replace:
        temporary.replace(path)
    else:
        temporary.rename(path)
    return _pin(path, raw)


class Ledger:
    """One fixed Root-owned ledger; only short atomic transitions, no supervisor.

    Root appends ``termination`` to an attempt after the child has exited. It is
    a pin of a JSON receipt containing the exact request binding plus terminated
    true and the raw integer native_exit. An unconfirmed exit is always active.
    """

    def __init__(self, path, identity):
        self.path = Path(path)
        self.identity = identity
        self.lock = self.path.with_name(self.path.name + ".lock")

    def _read(self):
        if not self.path.exists():
            return {"schema": "k47-ledger-v1", "identity": self.identity, "attempts": []}
        value = strict_json(_read_bytes(self.path))
        require(isinstance(value, dict) and value.get("schema") == "k47-ledger-v1"
                and value.get("identity") == self.identity and isinstance(value.get("attempts"), list),
                "LEDGER_IDENTITY", "LEDGER")
        return value

    def _acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.lock.mkdir()
        except FileExistsError:
            raise BoundaryError("LEDGER_BUSY", "LEDGER") from None

    def start(self, request):
        request_raw = json_bytes(request)  # complete serialization before started
        self._acquire()
        try:
            ledger = self._read()
            for attempt in ledger["attempts"]:
                require(digest(json_bytes(attempt["request"])) == attempt["request_sha256"],
                        "LEDGER_REQUEST_HASH", "LEDGER")
                require(attempt["request"]["attempt_id"] != request["attempt_id"], "ATTEMPT_ALREADY_EXISTS", "LEDGER")
                require(attempt.get("runtime_complete") is None, "ANALYSIS_ALREADY_COMPLETED", "LEDGER")
                require(attempt.get("termination") is not None, "PRIOR_ATTEMPT_UNTERMINATED", "LEDGER")
                terminal = _read_metadata_pin(attempt["termination"])
                binding = {"analysis_id": ANALYSIS_ID, "attempt_id": attempt["request"]["attempt_id"],
                           "input_hashes": self.identity["input_hashes"], "request_sha256": attempt["request_sha256"],
                           "release_sha256": attempt["request"]["release"]["sha256"],
                           "output": attempt["request"]["output"]}
                require(isinstance(terminal, dict) and all(terminal.get(key) == value for key, value in binding.items())
                        and terminal.get("terminated") is True and type(terminal.get("native_exit")) is int,
                        "TERMINATION_BINDING", "LEDGER")
                require(terminal["native_exit"] != 0, "ANALYSIS_ALREADY_SUCCEEDED", "LEDGER")
            ledger["attempts"].append({"request": request, "request_sha256": digest(request_raw),
                                       "runtime_complete": None, "runtime_failure": None, "termination": None})
            atomic_json(self.path, ledger, replace=self.path.exists())
        finally:
            self.lock.rmdir()

    def runtime_result(self, attempt_id, *, complete=None, failure=None):
        self._acquire()
        try:
            ledger = self._read()
            require(bool(ledger["attempts"]) and ledger["attempts"][-1]["request"]["attempt_id"] == attempt_id,
                    "LEDGER_ATTEMPT_IDENTITY", "LEDGER")
            attempt = ledger["attempts"][-1]
            require(attempt["runtime_complete"] is None and attempt["runtime_failure"] is None
                    and attempt["termination"] is None, "LEDGER_RESULT_ALREADY_EXISTS", "LEDGER")
            require((complete is None) != (failure is None), "LEDGER_RESULT_KIND", "LEDGER")
            attempt["runtime_complete"], attempt["runtime_failure"] = complete, failure
            atomic_json(self.path, ledger, replace=True)
        finally:
            self.lock.rmdir()


def publish_aggregates(output, payloads, attempt_id):
    output = Path(output)
    require(set(payloads) == set(OUTPUT_NAMES) and all(type(raw) is bytes for raw in payloads.values()),
            "OUTPUT_FILE_SET", "EXPORT")
    require(not output.exists(), "OUTPUT_ALREADY_EXISTS", "EXPORT")
    stage = output.with_name(output.name + ".writing-" + attempt_id)
    require(not stage.exists(), "OUTPUT_STAGE_ALREADY_EXISTS", "EXPORT")
    stage.mkdir(parents=True)
    for name in OUTPUT_NAMES:
        with (stage / name).open("xb") as stream:
            stream.write(payloads[name])
    require(not output.exists(), "OUTPUT_ALREADY_EXISTS", "EXPORT")
    stage.rename(output)
    return _pin(output / "CHECK.json", payloads["CHECK.json"])


def run_formal(frozen_dir, calendar_sessions, published_main, release_path, output):
    frozen_dir, output = Path(frozen_dir).resolve(), Path(output).resolve()
    paths = {"probabilities": frozen_dir / "MINUTE-probabilities.json.gz",
             "labels": frozen_dir / "MINUTE-labels.json.gz", "calendar": Path(calendar_sessions).resolve(),
             "published": Path(published_main).resolve()}
    # Resolve individual filenames as well: a link to an excluded path is not a data whitelist entry.
    paths = {key: path.resolve() for key, path in paths.items()}
    release, release_pin, code = verify_release(release_path, paths, output)
    ledger = Ledger(LEDGER_PATH, {"analysis_id": ANALYSIS_ID, "input_hashes": INPUT_HASHES})
    request = {"analysis_id": ANALYSIS_ID, "attempt_id": release["attempt_id"], "release": release_pin,
               "input_hashes": INPUT_HASHES, "inputs": release["inputs"], "output": str(output),
               "code": code, "commit": release["commit"], "config": fixed_config()}
    ledger.start(request)
    try:
        payloads = {name: _read_bytes(path) for name, path in paths.items()}
        require(all(len(payloads[name]) == release["inputs"][name]["bytes"] for name in paths), "INPUT_SIZE_MISMATCH")
        result = analyze_bytes(payloads, FORMAL)
        runtime_identity = {"attempt_id": release["attempt_id"], "release": release_pin, "code": code,
                            "inputs": {name: _pin(paths[name], raw) for name, raw in payloads.items()},
                            "commit": release["commit"], "ledger_path": str(LEDGER_PATH),
                            "process_terminal_receipt": None, "native_exit": None,
                            "independent_recomputation": None}
        output_payloads = aggregate_outputs(result, runtime_identity)
        check_pin = publish_aggregates(output, output_payloads, release["attempt_id"])
        try:
            ledger.runtime_result(release["attempt_id"], complete={"status": "RUNTIME_COMPLETE", "check": check_pin})
        except BaseException:
            # Preserve all generated evidence, but withdraw this uncommitted
            # directory from the requested success location if custody fails.
            output.rename(output.with_name(output.name + ".uncommitted-" + release["attempt_id"]))
            raise
        return {"status": "RUNTIME_COMPLETE", "analysis_id": ANALYSIS_ID, "attempt_id": release["attempt_id"],
                "reproduction": result["reproduction_report"], "j52": result["j52"]}
    except BaseException as error:
        failure = {"code": error.code if isinstance(error, BoundaryError) else "UNEXPECTED_EXCEPTION",
                   "phase": error.phase if isinstance(error, BoundaryError) else "RUNTIME",
                   "native_exit": None, "terminal_receipt": None}
        # Runtime evidence is not a terminal receipt and never permits a retry.
        ledger.runtime_result(release["attempt_id"], failure=failure)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    for option in ("frozen-dir", "calendar-sessions", "published-main", "release", "output"):
        parser.add_argument("--" + option, required=True)
    args = parser.parse_args(argv)
    try:
        result = run_formal(args.frozen_dir, args.calendar_sessions, args.published_main, args.release, args.output)
    except BoundaryError as error:
        print(json.dumps({"phase": error.phase, "code": error.code, "aggregate": error.detail},
                         ensure_ascii=False, allow_nan=False), file=sys.stderr, flush=True)
        return 3
    except Exception:
        print('{"phase":"RUNTIME","code":"UNEXPECTED_EXCEPTION"}', file=sys.stderr, flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
