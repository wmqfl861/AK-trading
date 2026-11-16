"""SYNTHETIC only: K47 numerical contracts, data gates and atomic publication."""
from __future__ import annotations

from collections import Counter
import copy
import csv
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
import gzip
import importlib.util
import io
from itertools import islice
import json
import math
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("k47_under_test", ROOT / "tools/experiments/k47_r001_decomposition.py")
k = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = k
SPEC.loader.exec_module(k)


def row(day="2024-06-04", target="UP", model=(.6, .2, .2), raw=(.3, .3, .4),
        calibrated=(.35, .35, .3), terminal="2"):
    return k.Row(day, target, model, raw, calibrated, terminal)


def synthetic_population(rows):
    return {"input_rows": len(rows), "common_rows": len(rows), "common_days": len({r.day for r in rows}),
            "independent_rows": dict.fromkeys(k.MODELS, len(rows)), "excluded_rows": 0,
            "exclusion_reasons_nonexclusive": {}}


def small_analysis(rows, calendar=None, draws=None):
    calendar = calendar or tuple(sorted({r.day for r in rows}))
    bootstrap = k.make_bootstrap(calendar, draws or [Counter(range(len(calendar)))])
    population = {1: synthetic_population(rows)}
    old = k.reproduce({1: rows}, population, bootstrap)
    return k.decompose({1: rows}, bootstrap, old)[1], bootstrap


def metric(ci=None, family=None):
    return {"ci95": ci, "ci_family": family}


def test_hand_losses_day_equal_and_neutral_direction_brier():
    rows = [row(target="UP"), row(target="NEUTRAL"), row("2024-06-05", model=(.2, .6, .2), target="DOWN")]
    cell, _ = small_analysis(rows)
    expected_l = ((-math.log(.6) - math.log(.2)) / 2 - math.log(.6)) / 2
    expected_c = ((math.log(.8) - math.log(.6)) / 2 + math.log(.8) - math.log(.6)) / 2
    assert cell["losses"]["model", "logloss"].value == pytest.approx(expected_l)
    assert cell["losses"]["model", "direction"].value == pytest.approx(expected_c)
    # The neutral zero remains in the first day's denominator of two.
    assert cell["losses"]["model", "direction_brier"].value == pytest.approx(((.75 - 1) ** 2 / 2 + .25 ** 2) / 2)
    assert expected_l != pytest.approx((-math.log(.6) * 2 - math.log(.2)) / 3)
    for identity in cell["identity"].values():
        assert identity["finite_checks"] == 3
        assert identity["max_finite_abs_diff"] <= 1e-12
    for control in ("raw", "calibrated"):
        points = [cell["metrics"][f"gain_{control}_{name}"]["point"] for name in ("total", "move", "direction")]
        assert points[0] == pytest.approx(points[1] + points[2], abs=1e-12)


def test_group_means_use_original_day_denominators():
    rows = [row(terminal="4"), row(target="DOWN", model=(.2, .6, .2), terminal="-2"),
            row("2024-06-05", target="DOWN", terminal="-2")]
    vectors, description, _ = k.group_vectors(rows, ("2024-06-04", "2024-06-05"), Decimal("2"))
    point = ((0, 1), (1, 1))
    assert description["bull"]["weight"] == 1.5
    assert vectors["bull", "balance"].mean(point).value == pytest.approx(-1 / 3)
    assert vectors["bull", "ticks"].mean(point).value == 0
    # Equal weighting of the two bull-day means would give 0 and .5 instead.
    assert vectors["bear", "ticks"].mean(point).value == -1
    assert sum(description[n]["rows"] for n in ("bull", "bear", "zero")) == 3


def test_activity_only_and_directional_worlds():
    calendar = ("2024-06-04", "2024-06-05", "2024-06-06")
    activity, directional = [], []
    for day in calendar:
        for target in k.CLASSES:
            moving = target != "NEUTRAL"
            activity.append(row(day, target, (.4, .4, .2) if moving else (.05, .05, .9)))
            signal = (.7, .1, .2) if target == "UP" else (.1, .7, .2) if target == "DOWN" else (.05, .05, .9)
            directional.append(row(day, target, signal))
    draws = [Counter({0: 3}), Counter({1: 3}), Counter({2: 3}), Counter({0: 1, 1: 1, 2: 1})]
    a, _ = small_analysis(activity, calendar, draws)
    d, _ = small_analysis(directional, calendar, draws)
    assert a["direction_reading"] != "显示出方向信息"
    assert a["metrics"]["gain_raw_move"]["point"] > 0
    assert d["direction_reading"] == "显示出方向信息"
    assert d["metrics"]["gain_raw_direction"]["ci_family"][0] > 0
    assert d["metrics"]["delta_balance"]["ci95"][0] > 0


@pytest.mark.parametrize("g,delta,expected", [
    (metric((.1, .2), (.01, .3)), metric((.1, .3)), "显示出方向信息"),
    (metric((.1, .2), (.01, .3)), metric((0., .3)), "提示性"),
    (metric((0., .2), (0., .3)), metric((0., .3)), "没有显示方向信息"),
    (metric((-.3, -.1)), metric((.1, .2)), "提示性"),
    (metric((-.3, -.1)), metric((-.1, .2)), "方向部分为负"),
    (metric(None), metric((.1, .2)), "提示性"),
    (metric((.1, .2)), metric(None), "提示性"),
    (metric(None), metric(None), "没有显示方向信息"),
    (metric((-.3, 0.)), metric(None), "没有显示方向信息"),
    (metric((math.nan, math.inf)), metric(None), "没有显示方向信息"),
])
def test_direction_priority_strict_boundaries_and_na(g, delta, expected):
    assert k.classify_direction(g, delta) == expected


@pytest.mark.parametrize("interval,expected", [
    ((1.00001, 2.), "付得起价差"), ((0., .99999), "付不起价差"),
    ((1., 2.), "分辨不了"), ((0., 1.), "分辨不了"), (None, "分辨不了"), ((-.1, 2.), "分辨不了")])
def test_cost_strict_boundaries(interval, expected):
    assert k.classify_cost(metric(interval)) == expected


@pytest.mark.parametrize("costs,branch,optional", [
    (("付得起价差", "分辨不了"), "甲", False), (("付不起价差", "分辨不了"), "乙", False),
    (("付不起价差", "付不起价差"), "丙", True)])
def test_j52(costs, branch, optional):
    cells = {h: {"cost_reading": cost, "direction_reading": "显示出方向信息"} for h, cost in zip((5, 15), costs)}
    result = k.j52(cells)
    assert result["branch"] == branch and result["optional_k47d"] is optional
    assert result["engineering_resumed"] is False


def test_quantile_ties_boundary_fixed_groups_and_zero(monkeypatch):
    rows = [row(model=(.1, .7, .2), target="DOWN") for _ in range(2)]
    rows += [row(model=(.3, .3, .4)) for _ in range(6)]
    rows += [row(model=(.7, .1, .2)) for _ in range(2)]
    real_quantile = k.weighted_quantile
    calls = []
    def counted(values, q):
        calls.append(q)
        return real_quantile(values, q)
    monkeypatch.setattr(k, "weighted_quantile", counted)
    cell, _ = small_analysis(rows, draws=[Counter({0: 1})] * 3)
    assert calls == [.1, .9]
    assert cell["quantiles"]["q10"] == -.6 and cell["quantiles"]["q90"] == .6
    assert cell["group_counts"]["strong_bull"]["rows"] == 2
    assert cell["group_counts"]["strong_bear"]["rows"] == 2
    assert cell["group_counts"]["zero"]["rows"] == 6
    assert real_quantile([(1., .25), (1., .25), (2., .5)], .5) == 1.
    assert real_quantile([(1., .25), (1., .25), (2., .5)], .5001) == 2.


@pytest.mark.parametrize("model", [(.3, .3, .4), (.6, .2, .2)])
def test_zero_and_single_sided_signals_retain_na_groups(model):
    cell, _ = small_analysis([row(model=model)])
    assert cell["metrics"]["delta_balance"]["point"] is None
    assert cell["metrics"]["strong_edge_ticks"]["point_reason"] == "NO_DENOMINATOR"
    assert cell["cost_reading"] == "分辨不了"


def test_one_invalid_bootstrap_draw_invalidates_complete_interval_only():
    rows = [row(), row("2024-06-05", target="DOWN", model=(.2, .6, .2))]
    cell, _ = small_analysis(rows, draws=[Counter({0: 1, 1: 1}), Counter({0: 2})])
    item = cell["metrics"]["delta_balance"]
    assert item["point"] == 2
    assert item["valid_repeats"] == 1 and item["invalid_repeats"] == {"NO_DENOMINATOR": 1}
    assert item["ci95"] is None and item["ci_family"] is None
    assert cell["metrics"]["gain_raw_direction"]["valid_repeats"] == 2


def test_complete_original_bootstrap_sequence_and_dense_hash():
    calendar = tuple((date(2024, 6, 3) + timedelta(days=i)).isoformat() for i in range(92))
    actual = k.make_bootstrap(calendar)
    expected = list(k.bootstrap_indices(calendar, "MINUTE"))
    assert len(actual.draws) == len(expected) == 20000
    assert all(dict(draw) == counts and sum(counts.values()) == 92 for draw, counts in zip(actual.draws, expected))
    assert any(0 not in counts for counts in expected) and any(0 in counts for counts in expected)
    dense = [[counts.get(i, 0) for i in range(92)] for counts in expected]
    assert actual.sha256 == k.digest(json.dumps(dense, separators=(",", ":")).encode())


def test_zero_tiny_probabilities_and_extended_identities():
    identity = k._identity_counts()
    result = k.split_losses((1e-320, .5, .5), "UP", identity)
    assert math.isfinite(result["direction"])
    assert result["direction"] == pytest.approx(math.log(.5) - math.log(1e-320))
    for probabilities, target in (((0., 1., 0.), "UP"), ((0., 0., 1.), "UP"), ((1., 0., 0.), "NEUTRAL")):
        loss = k.split_losses(probabilities, target, identity)
        assert loss["logloss"] == math.inf and loss["move"] + loss["direction"] == math.inf
    assert identity["finite_checks"] == 1 and identity["extended_positive_infinity_matches"] == 3
    assert identity["zero_move_denominator_rows"] == 1
    neutral = k.split_losses((0., 0., 1.), "NEUTRAL", identity)
    assert neutral["direction"] == neutral["direction_brier"] == 0


def test_unselected_nonfinite_day_and_inf_minus_inf():
    vector = k.Vector((1., 0.), (1., 1.), (None, "NONFINITE"), "NO_OBSERVATIONS")
    assert vector.mean(((0, 2), (1, 0))) == k.Number(1.)
    assert vector.mean(((1, 2),)).reason == "NONFINITE"
    rows = [row(model=(0., 0., 1.), raw=(0., 0., 1.)), row("2024-06-05")]
    cell, _ = small_analysis(rows, draws=[Counter({0: 2}), Counter({1: 2})])
    metric = cell["metrics"]["gain_raw_direction"]
    assert metric["point_reason"] == "NONFINITE" and metric["invalid_repeats"] == {"NONFINITE": 1}
    assert metric["valid_repeats"] == 1
    assert cell["losses"]["model", "brier"].reason is None


def test_invalid_terminal_dependency_and_unselected_day():
    first = "2024-06-04"
    second = "2024-06-05"
    rows = [row(first, model=(.51, .49, 0.), terminal=None)]
    rows += [row(first, model=(.9, .05, .05)), row(first, model=(.05, .9, .05), terminal="-2")]
    rows += [row(second, model=(.9, .05, .05)), row(second, model=(.05, .9, .05), terminal="-2")]
    panels, counts, quantiles = k.group_vectors(rows, (first, second), Decimal("1"))
    assert counts["bull"]["weight"] == pytest.approx(2 / 3 + 1 / 2)
    assert quantiles["invalid_terminal_points_rows"] == 1
    assert panels["bull", "ticks"].mean(((0, 1), (1, 1))).reason == "TERMINAL_POINTS_INVALID"
    assert panels["strong_bull", "ticks"].mean(((0, 1), (1, 1))).value == 2
    assert panels["bull", "ticks"].mean(((0, 0), (1, 2))).value == 2
    cell, _ = small_analysis(rows, draws=[Counter({0: 2}), Counter({1: 2})])
    assert cell["metrics"]["delta_ticks"]["invalid_repeats"] == {"TERMINAL_POINTS_INVALID": 1}
    assert cell["metrics"]["strong_delta_ticks"]["valid_repeats"] == 2
    assert cell["metrics"]["delta_balance"]["valid_repeats"] == 2
    zero = rows + [row(second, model=(.3, .3, .4), terminal="NaN")]
    zero_panels, _, info = k.group_vectors(zero, (first, second), Decimal("1"))
    assert info["invalid_terminal_points_rows"] == 2
    assert zero_panels["strong_bull", "ticks"].mean(((0, 1), (1, 1))).value == 2


@pytest.mark.parametrize("terminal", [None, True, {}, "broken", "NaN", "Infinity", "1e9999"])
def test_terminal_invalid_does_not_filter_population(terminal):
    rows = [row(terminal=terminal), row(model=(.2, .6, .2), target="DOWN")]
    cell, _ = small_analysis(rows)
    assert cell["group_counts"]["bull"]["rows"] == 1
    assert cell["metrics"]["delta_ticks"]["point_reason"] == "TERMINAL_POINTS_INVALID"
    assert cell["metrics"]["delta_balance"]["point"] == 2


def test_own_frequency_zero_class_and_empty_months():
    rows = [row(), row(target="DOWN", model=(.2, .6, .2))]
    cell, _ = small_analysis(rows)
    own = cell["own_frequency"]
    assert own["frequency"]["NEUTRAL"].value == 0
    assert own["brier"].value == .5
    assert own["logloss"].value == pytest.approx(math.log(2))
    assert len(cell["monthly"]) == 5
    assert all(item["raw_brier_skill"].reason == "NO_OBSERVATIONS" for item in cell["monthly"][1:])


def _synthetic_bundle(repeats):
    calendar = ("2024-06-03", "2024-06-04", "2024-06-05")
    predictions, labels = [], []
    probabilities = ((.6, .2, .2), (.2, .6, .2), (.2, .2, .6))
    for horizon in k.HORIZONS:
        for day in calendar[1:]:
            for target, model, terminal in zip(k.CLASSES, probabilities, ("2", "-2", "0")):
                identity = f"SYNTHETIC-{horizon}-{day}-{target}"
                predictions.append({"request_id": identity, "day": day, "mode": "MINUTE", "horizon": horizon,
                                    "model": model, "raw": (.3, .3, .4), "calibrated": (.35, .35, .3)})
                labels.append({"request_id": identity, "status": "MATURE", "direction": target,
                               "horizon": {"unit": "TRADING_MINUTE", "value": horizon},
                               "contract_id": "SHFE.rb2409", "terminal_points": terminal})
    payloads = {"probabilities": gzip.compress(k.json_bytes(predictions), mtime=0),
                "labels": gzip.compress(k.json_bytes(labels), mtime=0),
                "calendar": k.json_bytes({"records": [{"trading_day": day} for day in calendar]})}
    # Independent constant-day analytic reference. Every resampled nonempty day
    # has the same loss vector, so all percentile endpoints equal the skill.
    means = {}
    for name in k.MODELS:
        vectors = probabilities if name == "model" else [(.3, .3, .4) if name == "raw" else (.35, .35, .3)] * 3
        means[name, "brier"] = math.fsum(math.fsum((p - int(i == target)) ** 2 for i, p in enumerate(vector))
                                         for target, vector in enumerate(vectors)) / 3
        means[name, "logloss"] = math.fsum(-math.log(vector[target]) for target, vector in enumerate(vectors)) / 3
    main = []
    for horizon in k.HORIZONS:
        record = {"mode": "MINUTE", "horizon": horizon, "common_rows": 6, "common_days": 2, "excluded_rows": 0,
                  "algorithm": "SYNTHETIC_FIXED", "probabilities_sha256": k.digest(payloads["probabilities"]),
                  "labels_sha256": k.digest(payloads["labels"]), "exclusion_reasons_nonexclusive": "{}"}
        for name in k.MODELS:
            record[name + "_independent_rows"] = 6
            for loss in ("brier", "logloss"):
                record[name + "_" + loss] = means[name, loss]
                record[name + "_" + loss + "_reason"] = "NA"
        for control in ("raw", "calibrated"):
            for loss in ("brier", "logloss"):
                key = control + "_" + loss
                point = 1 - means["model", loss] / means[control, loss]
                record.update({key + "_skill": point, key + "_point_reason": "NA", key + "_valid_repeats": repeats,
                               key + "_invalid_repeats": "{}", key + "_ci_reason": "NA"})
                for interval in ("ci95", "ci_family"):
                    for end in ("lower", "upper"):
                        record[key + "_" + interval + "_" + end] = point if interval == "ci95" or key == "raw_brier" else "NA"
        main.append(record)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(main[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(main)
    payloads["published"] = stream.getvalue().encode()
    policy = k.InputPolicy({name: k.digest(raw) for name, raw in payloads.items()},
                           k.digest((json.dumps(calendar, separators=(",", ":")) + "\n").encode()),
                           calendar[0], calendar[-1], 3, 3, 6, 2)
    bootstrap = k.make_bootstrap(calendar, islice(k.bootstrap_indices(calendar, "MINUTE"), repeats))
    return payloads, policy, bootstrap


@pytest.fixture(scope="module")
def bundle():
    return _synthetic_bundle(7)


@pytest.fixture(scope="module")
def full_result():
    payloads, policy, bootstrap = _synthetic_bundle(20000)
    return k.analyze_bytes(payloads, policy, bootstrap=bootstrap), payloads, policy, bootstrap


def test_full_synthetic_chain_20000_shared_samples_and_publication(full_result, tmp_path):
    result, payloads, _, bootstrap = full_result
    assert result["reproduction_report"]["differences"] == 0
    assert result["reproduction_report"]["max_abs_diff"] <= 1e-9
    assert result["bootstrap"]["sha256"] == bootstrap.sha256
    assert all(value == ["2024-06-03"] for value in result["empty_dates"].values())
    for cell in result["cells"].values():
        assert tuple(cell["metrics"]) == k.METRICS
        assert all(m["valid_repeats"] + sum(m["invalid_repeats"].values()) == 20000 for m in cell["metrics"].values())
    outputs = k.aggregate_outputs(result, {"kind": "SYNTHETIC", "inputs": {name: k.digest(raw) for name, raw in payloads.items()}})
    target = tmp_path / "complete"
    k.publish_aggregates(target, outputs, "synthetic-1")
    assert set(p.name for p in target.iterdir()) == set(k.OUTPUT_NAMES)
    check = json.loads((target / "CHECK.json").read_bytes(), parse_constant=lambda value: pytest.fail(value))
    assert check["fit_calls"] == check["prediction_calls"] == check["label_calls"] == check["research_scoring_calls"] == 0
    assert "AWAITING" in check["publication"]["status"]
    assert check["safety"] == k.SAFETY
    lengths = {}
    for name in k.OUTPUT_NAMES[:-1]:
        table = list(csv.DictReader(io.StringIO(outputs[name].decode())))
        lengths[name] = len(table)
        assert all("request_id" not in key and "members" not in key for key in table[0])
        assert all(value != "" for item in table for value in item.values())
    assert lengths["statistics.csv"] == 65 and lengths["monthly.csv"] == 25 and lengths["own-frequency.csv"] == 5
    stats = list(csv.DictReader(io.StringIO(outputs["statistics.csv"].decode())))
    for item in stats:
        actual = result["cells"][int(item["horizon"])]["metrics"][item["metric"]]
        assert (item["point"] == "NA" and actual["point"] is None) or float(item["point"]) == actual["point"]
    assert b"SYNTHETIC-1-2024" not in b"".join(outputs.values())


def test_same_bootstrap_object_used_in_both_stages(bundle, monkeypatch):
    payloads, policy, _ = bundle
    seen = []
    original_reproduce, original_decompose = k.reproduce, k.decompose
    bootstrap = bundle[2]
    def reproduce(rows, population, plan):
        seen.append(plan)
        return original_reproduce(rows, population, plan)
    def decompose(rows, plan, old):
        seen.append(plan)
        return original_decompose(rows, plan, old)
    monkeypatch.setattr(k, "reproduce", reproduce)
    monkeypatch.setattr(k, "decompose", decompose)
    k.analyze_bytes(payloads, policy, bootstrap=bootstrap)
    assert len(seen) == 2 and seen[0] is seen[1] is bootstrap


def _change_json(payloads, name, change):
    compressed = name in ("labels", "probabilities")
    content = json.loads(gzip.decompress(payloads[name]) if compressed else payloads[name])
    change(content)
    payloads[name] = gzip.compress(k.json_bytes(content), mtime=0) if compressed else k.json_bytes(content)


@pytest.mark.parametrize("kind,code", [("hash", "INPUT_HASH_MISMATCH"), ("calendar_count", "CALENDAR_POPULATION"),
    ("calendar_hash", "CALENDAR_HASH_MISMATCH"), ("population", "COMMON_POPULATION_MISMATCH"),
    ("skill", "PUBLISHED_REPRODUCTION_MISMATCH"), ("interval", "PUBLISHED_REPRODUCTION_MISMATCH"),
    ("na_family", "PUBLISHED_REPRODUCTION_MISMATCH"), ("duplicate", "DUPLICATE_PREDICTION")])
def test_predecomposition_gates_are_closed(bundle, monkeypatch, kind, code):
    payloads, policy, bootstrap = copy.deepcopy(bundle)
    calls = []
    monkeypatch.setattr(k, "decompose", lambda *a, **kw: calls.append("UNAUTHORIZED"))
    if kind == "hash":
        payloads["labels"] += b"tampered"
    elif kind == "calendar_count":
        _change_json(payloads, "calendar", lambda content: content["records"][1].update(trading_day="2024-06-03"))
    elif kind == "calendar_hash":
        policy = replace(policy, calendar_hash="0" * 64)
    elif kind == "population":
        policy = replace(policy, common_rows=7)
    elif kind == "duplicate":
        _change_json(payloads, "probabilities", lambda content: content.append(content[0]))
    else:
        table = list(csv.DictReader(io.StringIO(payloads["published"].decode())))
        key = {"skill": "raw_brier_skill", "interval": "calibrated_logloss_ci95_upper",
               "na_family": "raw_logloss_ci_family_lower"}[kind]
        table[0][key] = "0.123456789"
        payloads["published"] = k.csv_bytes(table)
    if kind != "hash":
        policy = replace(policy, hashes={name: k.digest(raw) for name, raw in payloads.items()})
        if kind == "duplicate":
            table = list(csv.DictReader(io.StringIO(payloads["published"].decode())))
            for item in table:
                item["probabilities_sha256"] = policy.hashes["probabilities"]
            payloads["published"] = k.csv_bytes(table)
            policy = replace(policy, hashes={name: k.digest(raw) for name, raw in payloads.items()})
    with pytest.raises(k.BoundaryError) as error:
        k.analyze_bytes(payloads, policy, bootstrap=bootstrap)
    assert error.value.code == code and calls == []


def test_terminal_conversion_never_precedes_reproduction(bundle, monkeypatch):
    payloads, policy, bootstrap = copy.deepcopy(bundle)
    table = list(csv.DictReader(io.StringIO(payloads["published"].decode())))
    table[0]["model_brier"] = "999"
    payloads["published"] = k.csv_bytes(table)
    policy = replace(policy, hashes={name: k.digest(raw) for name, raw in payloads.items()})
    monkeypatch.setattr(k, "terminal_ticks", lambda *args: pytest.fail("terminal conversion before gate"))
    with pytest.raises(k.BoundaryError, match="PUBLISHED_REPRODUCTION_MISMATCH"):
        k.analyze_bytes(payloads, policy, bootstrap=bootstrap)


@pytest.mark.parametrize("kind,code", [("label_duplicate", "DUPLICATE_LABEL"), ("extra", "UNEXPLAINED_EXTRA_LABEL"),
    ("conflict", "LABEL_HORIZON_CONFLICT"), ("contract", "LABEL_CONTRACT_CONFLICT"), ("day", "PREDICTION_SCOPE")])
def test_identity_pairing_refuses_conflicts(bundle, kind, code):
    predictions = json.loads(gzip.decompress(bundle[0]["probabilities"]))
    labels = json.loads(gzip.decompress(bundle[0]["labels"]))
    if kind == "label_duplicate":
        labels.append(labels[0])
    elif kind == "extra":
        labels.append(dict(labels[0], request_id="SYNTHETIC-EXTRA"))
    elif kind == "conflict":
        labels[0]["horizon"]["value"] = 60
    elif kind == "contract":
        labels[0]["contract_id"] = "DCE.i2409"
    else:
        predictions[0]["day"] = "2025-01-01"
    with pytest.raises(k.BoundaryError, match=code):
        k.pair_records(predictions, labels, bundle[2].calendar)


def test_illegal_probability_and_missing_label_are_exclusions(bundle):
    predictions = json.loads(gzip.decompress(bundle[0]["probabilities"]))
    labels = json.loads(gzip.decompress(bundle[0]["labels"]))
    predictions[0]["model"] = [True, 0, 0]
    predictions[1]["raw"] = [math.inf, 0, 0]
    labels.pop(2)
    rows, populations = k.pair_records(predictions, labels, bundle[2].calendar)
    pop = populations[1]
    assert len(rows[1]) == 3 and pop["excluded_rows"] == 3
    assert pop["independent_rows"] == {"model": 4, "raw": 4, "calibrated": 5}
    assert pop["exclusion_reasons_nonexclusive"] == {"model:PROBABILITY_TYPE": 1,
                                                    "raw:PROBABILITY_NONFINITE": 1, "DIRECTION_LABEL_MISSING": 1}


def request(attempt="a1", output="SYNTHETIC-output", release_hash="a" * 64):
    return {"analysis_id": k.ANALYSIS_ID, "attempt_id": attempt,
            "release": {"sha256": release_hash}, "output": output}


def test_fixed_ledger_blocks_active_success_and_changed_output(tmp_path):
    identity = {"analysis_id": k.ANALYSIS_ID, "input_hashes": {"SYNTHETIC": "same"}}
    ledger = k.Ledger(tmp_path / "analysis-ledger.json", identity)
    ledger.start(request())
    with pytest.raises(k.BoundaryError, match="PRIOR_ATTEMPT_UNTERMINATED"):
        ledger.start(request("a2", "SYNTHETIC-different-output"))
    ledger.runtime_result("a1", complete={"status": "RUNTIME_COMPLETE"})
    with pytest.raises(k.BoundaryError, match="ANALYSIS_ALREADY_COMPLETED"):
        ledger.start(request("a3", "SYNTHETIC-another-output", "b" * 64))
    saved = json.loads(ledger.path.read_bytes())
    assert len(saved["attempts"]) == 1
    assert saved["attempts"][0]["termination"] is None


def test_g2_retry_requires_exact_root_terminal_failure(tmp_path):
    identity = {"analysis_id": k.ANALYSIS_ID, "input_hashes": {"SYNTHETIC": "same"}}
    ledger = k.Ledger(tmp_path / "analysis-ledger.json", identity)
    ledger.start(request())
    ledger.runtime_result("a1", failure={"code": "SYNTHETIC_FAILURE"})
    with pytest.raises(k.BoundaryError, match="PRIOR_ATTEMPT_UNTERMINATED"):
        ledger.start(request("a2"))
    saved = json.loads(ledger.path.read_bytes())
    first = copy.deepcopy(saved["attempts"][0])
    terminal = {"analysis_id": k.ANALYSIS_ID, "attempt_id": "a1", "input_hashes": identity["input_hashes"],
                "request_sha256": first["request_sha256"], "release_sha256": first["request"]["release"]["sha256"],
                "output": first["request"]["output"], "terminated": True, "native_exit": 3}
    pin = k.atomic_json(tmp_path / "terminal.json", terminal)
    saved["attempts"][0]["termination"] = pin
    k.atomic_json(ledger.path, saved, replace=True)  # emulate Root after real process exit
    ledger.start(request("a2", "SYNTHETIC-new-attempt"))
    after = json.loads(ledger.path.read_bytes())
    assert len(after["attempts"]) == 2
    assert after["attempts"][0] == dict(first, termination=pin)
    assert after["attempts"][1]["request"]["attempt_id"] == "a2"


def test_terminal_raw_zero_cannot_be_relabelled_failure(tmp_path):
    identity = {"analysis_id": k.ANALYSIS_ID, "input_hashes": {"SYNTHETIC": "same"}}
    ledger = k.Ledger(tmp_path / "ledger.json", identity)
    ledger.start(request())
    data = json.loads(ledger.path.read_bytes())
    terminal = {"analysis_id": k.ANALYSIS_ID, "attempt_id": "a1", "input_hashes": identity["input_hashes"],
                "request_sha256": data["attempts"][0]["request_sha256"], "release_sha256": "a" * 64,
                "output": "SYNTHETIC-output", "terminated": True, "native_exit": 0}
    data["attempts"][0]["termination"] = k.atomic_json(tmp_path / "terminal.json", terminal)
    k.atomic_json(ledger.path, data, replace=True)
    with pytest.raises(k.BoundaryError, match="ANALYSIS_ALREADY_SUCCEEDED"):
        ledger.start(request("a2"))


def test_atomic_export_and_failed_stage_never_publish_check(bundle, tmp_path, monkeypatch):
    result = k.analyze_bytes(bundle[0], bundle[1], bootstrap=bundle[2])
    payloads = k.aggregate_outputs(result, {"kind": "SYNTHETIC"})
    output = tmp_path / "failed-output"
    real_open = Path.open
    def fail_partway(path, *args, **kwargs):
        if path.name == "monthly.csv":
            raise OSError("SYNTHETIC write failure")
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", fail_partway)
    with pytest.raises(OSError):
        k.publish_aggregates(output, payloads, "attempt1")
    assert not output.exists()
    stage = tmp_path / "failed-output.writing-attempt1"
    assert stage.exists() and not (stage / "CHECK.json").exists()
    with pytest.raises(k.BoundaryError, match="OUTPUT_STAGE_ALREADY_EXISTS"):
        k.publish_aggregates(output, payloads, "attempt1")


def test_atomic_metadata_serializes_before_creating_final(tmp_path):
    path = tmp_path / "bad.json"
    with pytest.raises(ValueError):
        k.atomic_json(path, {"value": math.inf})
    assert not path.exists() and not path.with_name("bad.json.writing").exists()


def test_bad_release_opens_no_research_data_and_sanitizes_cli(tmp_path, monkeypatch, capsys):
    release = tmp_path / "release.json"
    release.write_text('{"schema":"SYNTHETIC_INVALID","secret":"do-not-echo"}', encoding="utf-8")
    seen = []
    real_read = Path.read_bytes
    def observed(path):
        seen.append(path.resolve())
        assert path.resolve() == release.resolve()
        return real_read(path)
    monkeypatch.setattr(Path, "read_bytes", observed)
    code = k.main(["--frozen-dir", str(tmp_path), "--calendar-sessions", str(tmp_path / "sessions.json"),
                   "--published-main", str(tmp_path / "main.csv"), "--release", str(release),
                   "--output", str(tmp_path / "output")])
    assert code == 3 and seen == [release.resolve()]
    text = capsys.readouterr().err
    assert "RELEASE_SCHEMA" in text and "secret" not in text and "do-not-echo" not in text
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("option", ["--repeats", "--tick", "--skip-hash", "--test-profile", "--ledger"])
def test_cli_has_no_relaxed_profile(option):
    with pytest.raises(SystemExit) as error:
        k.main(["--frozen-dir", "x", "--calendar-sessions", "x", "--published-main", "x",
                "--release", "x", "--output", "x", option, "1"])
    assert error.value.code == 2


def test_module_import_has_no_product_calls_or_environment_access(tmp_path):
    # A fresh process makes import side effects visible independently of pytest.
    script = """
import importlib.util, pathlib, sys
source = pathlib.Path(sys.argv[1])
before = set(sys.modules)
def audit(event, args):
    if event.startswith('socket.'):
        raise AssertionError('network forbidden')
sys.addaudithook(audit)
spec = importlib.util.spec_from_file_location('k47_isolated', source)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
added = set(sys.modules) - before
assert not any(name.startswith('ak_trading') for name in added)
assert 'k45_forecast_skill' not in added
assert 'numpy' not in added
print('SYNTHETIC_IMPORT_BOUNDARY_OK')
"""
    process = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", script,
                              str(ROOT / "tools/experiments/k47_r001_decomposition.py")],
                             capture_output=True, text=True, cwd=tmp_path, timeout=30)
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == "SYNTHETIC_IMPORT_BOUNDARY_OK"


@pytest.fixture
def released_files(tmp_path, monkeypatch, bundle):
    """Root metadata is SYNTHETIC; no real release or ledger is used or created."""
    payloads = bundle[0]
    names = {"probabilities": "MINUTE-probabilities.json.gz", "labels": "MINUTE-labels.json.gz",
             "calendar": "sessions.json", "published": "main.csv"}
    paths = {name: tmp_path / filename for name, filename in names.items()}
    for name, path in paths.items():
        path.write_bytes(payloads[name])
    code = {name: k.digest((ROOT / relative).read_bytes()) for name, relative in k.CODE_PATHS.items()}
    commit = "a" * 40
    def synthetic_git(arguments):
        if arguments == ["rev-parse", "HEAD"]:
            return (commit + "\n").encode()
        assert arguments[0] == "show" and arguments[1].startswith(commit + ":")
        relative = arguments[1].split(":", 1)[1]
        assert relative in k.CODE_PATHS.values()
        return (ROOT / relative).read_bytes()
    monkeypatch.setattr(k, "_git_bytes", synthetic_git)
    ledger = tmp_path / "fixed-ledger.json"
    monkeypatch.setattr(k, "LEDGER_PATH", ledger)
    reviews = {}
    for kind in ("boundary", "numerical"):
        reviews[kind] = k.atomic_json(tmp_path / (kind + "-review.json"), {
            "verdict": "PASS", "conditional": False, "independent": True,
            "review_scope": "K47_" + kind.upper() + "_HARD_REVIEW", "code_hashes": code,
            "reviewer": "SYNTHETIC_FIXTURE_NOT_AN_ACTUAL_REVIEW"})
    ci = k.atomic_json(tmp_path / "ci.json", {"conclusion": "success", "commit": commit, "kind": "SYNTHETIC"})
    release = {"schema": "k47-release-v1", "analysis_id": k.ANALYSIS_ID, "attempt_id": "SYNTHETIC-1",
               "config": k.fixed_config(), "input_hashes": k.INPUT_HASHES,
               "ledger_path": str(ledger), "commit": commit, "integrated_main": True,
               "code_hashes": code, "reviews": reviews, "ci": ci,
               "inputs": {name: {"path": str(path), "bytes": len(payloads[name]), "sha256": k.INPUT_HASHES[name]}
                          for name, path in paths.items()}}
    release_path = tmp_path / "release.json"
    k.atomic_json(release_path, release)
    return paths, release, release_path, tmp_path / "output", ledger


@pytest.mark.parametrize("kind,code", [("review_scope", "RELEASE_HARD_REVIEW"), ("conditional", "RELEASE_HARD_REVIEW"),
    ("independent", "RELEASE_HARD_REVIEW"), ("review_code", "RELEASE_HARD_REVIEW"),
    ("ci", "RELEASE_CI"), ("source", "SOURCE_HASH_MISMATCH"), ("config", "RELEASE_FIXED_CONFIG"),
    ("inputs", "RELEASE_FIXED_CONFIG"), ("ledger", "RELEASE_LEDGER"), ("output", "OUTPUT_NOT_FRESH_EXTERNAL"),
    ("main", "RELEASE_CODE_IDENTITY"), ("path", "RELEASE_INPUT_IDENTITY")])
def test_release_gates_before_research_opens(released_files, monkeypatch, kind, code):
    paths, release, release_path, output, _ = released_files
    if kind in ("review_scope", "conditional", "independent", "review_code"):
        pin = release["reviews"]["boundary"]
        record = json.loads(Path(pin["path"]).read_bytes())
        if kind == "review_scope":
            record["review_scope"] = "K47_PLAN_OPINION"
        elif kind == "review_code":
            record["code_hashes"]["tool"] = "0" * 64
        else:
            record[kind] = kind == "conditional"
        release["reviews"]["boundary"] = k.atomic_json(Path(pin["path"]), record, replace=True)
    elif kind == "ci":
        release["ci"] = k.atomic_json(Path(release["ci"]["path"]), {"conclusion": "success", "commit": "b" * 40}, replace=True)
    elif kind == "source":
        release["code_hashes"]["tool"] = "0" * 64
    elif kind == "config":
        release["config"]["repeats"] = 2
    elif kind == "inputs":
        release["input_hashes"] = dict(release["input_hashes"], labels="0" * 64)
    elif kind == "ledger":
        release["ledger_path"] = str(output / "alternate-ledger.json")
    elif kind == "output":
        output.mkdir()
    elif kind == "main":
        release["integrated_main"] = False
    else:
        paths["calendar"] = paths["calendar"].with_name(".env")
        release["inputs"]["calendar"]["path"] = str(paths["calendar"])
    k.atomic_json(release_path, release, replace=True)
    real_read = k._read_bytes
    opened = []
    def observed(path):
        opened.append(Path(path))
        assert Path(path) not in paths.values(), "research bytes opened before release"
        return real_read(path)
    monkeypatch.setattr(k, "_read_bytes", observed)
    with pytest.raises(k.BoundaryError) as error:
        k.verify_release(release_path, paths, output)
    assert error.value.code == code
    assert release_path in opened


def test_hash_failure_preserves_started_attempt_without_decomposition(released_files, monkeypatch):
    paths, _, release_path, output, ledger = released_files
    seen = Counter()
    real_read = k._read_bytes
    def observed(path):
        path = Path(path)
        if path in paths.values():
            seen[path] += 1
        return real_read(path)
    monkeypatch.setattr(k, "_read_bytes", observed)
    monkeypatch.setattr(k, "decompose", lambda *a: pytest.fail("decomposition before hash gate"))
    with pytest.raises(k.BoundaryError, match="INPUT_HASH_MISMATCH"):
        k.run_formal(paths["probabilities"].parent, paths["calendar"], paths["published"], release_path, output)
    assert dict(seen) == dict.fromkeys(paths.values(), 1)
    saved = json.loads(ledger.read_bytes())
    assert saved["attempts"][0]["runtime_failure"]["code"] == "INPUT_HASH_MISMATCH"
    assert saved["attempts"][0]["termination"] is None
    assert not output.exists()


def test_full_file_flow_only_opens_declared_data_once_and_rejects_second_success(
        released_files, full_result, monkeypatch):
    paths, release, release_path, output, ledger = released_files
    _, payloads, policy, bootstrap = full_result
    # Internal test injection is explicit and entirely SYNTHETIC. There is no
    # production flag or release field that can supply an alternate policy.
    monkeypatch.setattr(k, "INPUT_HASHES", policy.hashes)
    monkeypatch.setattr(k, "FORMAL", policy)
    monkeypatch.setattr(k, "CALENDAR_HASH", policy.calendar_hash)
    monkeypatch.setattr(k, "make_bootstrap", lambda days: bootstrap)
    for name, path in paths.items():
        path.write_bytes(payloads[name])
    release.update(config=k.fixed_config(), input_hashes=policy.hashes)
    release["inputs"] = {name: k._pin(path, payloads[name]) for name, path in paths.items()}
    k.atomic_json(release_path, release, replace=True)
    code_reads = {ROOT / relative for relative in k.CODE_PATHS.values()}
    allowed = set(paths.values()) | code_reads | {release_path, ledger, Path(release["ci"]["path"])}
    allowed |= {Path(pin["path"]) for pin in release["reviews"].values()}
    seen = Counter()
    real_read = k._read_bytes
    def guarded_read(path):
        path = Path(path)
        assert path in allowed, "undeclared input open"
        seen[path] += 1
        return real_read(path)
    monkeypatch.setattr(k, "_read_bytes", guarded_read)
    completed = k.run_formal(paths["probabilities"].parent, paths["calendar"], paths["published"], release_path, output)
    assert completed["status"] == "RUNTIME_COMPLETE"
    assert all(seen[path] == 1 for path in paths.values())
    saved = json.loads(ledger.read_bytes())
    assert saved["attempts"][0]["runtime_complete"] is not None
    assert saved["attempts"][0]["termination"] is None
    check = json.loads((output / "CHECK.json").read_bytes())
    assert check["runtime"]["native_exit"] is None
    assert check["runtime"]["independent_recomputation"] is None
    assert check["runtime"]["inputs"] == release["inputs"]
    new_output = output.with_name("fresh-second-output")
    release["attempt_id"] = "SYNTHETIC-2"
    renamed_release = release_path.with_name("copied-renamed-release.json")
    k.atomic_json(renamed_release, release)
    allowed.add(renamed_release)
    monkeypatch.setattr(k, "decompose", lambda *a: pytest.fail("second successful decomposition"))
    with pytest.raises(k.BoundaryError, match="ANALYSIS_ALREADY_COMPLETED"):
        k.run_formal(paths["probabilities"].parent, paths["calendar"], paths["published"], renamed_release, new_output)
    assert not new_output.exists()
    assert all(seen[path] == 1 for path in paths.values())


def test_terminal_sum_overflow_is_na_without_population_changes():
    rows = [row(terminal="1e308"), row(terminal="1e308"), row(model=(.2, .6, .2), target="DOWN")]
    panels, counts, _ = k.group_vectors(rows, ("2024-06-04",), Decimal("1"))
    assert counts["bull"]["rows"] == 2
    assert panels["bull", "ticks"].mean(((0, 1),)).reason == "NONFINITE"
    assert panels["bull", "balance"].mean(((0, 1),)).value == 1


def test_unexpected_error_stays_raw_one_and_does_not_echo_payload(monkeypatch, capsys):
    def fail(*args):
        raise ValueError("SYNTHETIC-SENSITIVE-ROW-CONTENT")
    monkeypatch.setattr(k, "run_formal", fail)
    assert k.main(["--frozen-dir", "x", "--calendar-sessions", "x", "--published-main", "x",
                   "--release", "x", "--output", "x"]) == 1
    captured = capsys.readouterr()
    assert "UNEXPECTED_EXCEPTION" in captured.err and "SENSITIVE" not in captured.err
