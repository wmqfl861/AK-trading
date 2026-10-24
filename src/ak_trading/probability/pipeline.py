"""Configuration and orchestration for frozen probability-strategy experiments."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re
from typing import Mapping

from ak_trading.futures.scenario_backtest import ScenarioConfig
from ak_trading.probability.contracts import PathScenario


@dataclass(frozen=True)
class ResearchConfig:
    symbols: tuple[str, ...]
    economics: ScenarioConfig
    horizons: tuple[int, ...]
    barriers_atr: tuple[float, ...]
    action_horizon: int
    routing_barrier_atr: float
    min_samples: int
    min_win_probability: float
    min_ev: float
    max_ambiguous_probability: float
    seed: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ResearchConfig:
        required = set(cls.__dataclass_fields__)
        if not isinstance(value, Mapping) or set(value) != required:
            raise ValueError("research config keys must match the documented schema")
        symbols = value["symbols"]
        if not isinstance(symbols, list) or not 1 <= len(symbols) <= 12 or len(set(symbols)) != len(symbols):
            raise ValueError("select 1..12 distinct concrete futures symbols")
        if any(not isinstance(s, str) or not re.fullmatch(r"(?:SHFE|INE|DCE|CZCE|CFFEX|GFEX)\.[A-Za-z]+[0-9]{3,4}", s) for s in symbols):
            raise ValueError("symbols must be explicit concrete futures contracts")
        for symbol in symbols:
            exchange, code = symbol.split(".", 1)
            delivery = re.search(r"[0-9]+$", code).group()
            if len(delivery) != (3 if exchange == "CZCE" else 4) or not 1 <= int(delivery[-2:]) <= 12:
                raise ValueError("contract delivery code is invalid")
        products = {re.sub(r"[0-9]+$", "", s).upper() for s in symbols}
        if len(products) != 1:
            raise ValueError("one economic configuration requires contracts of the same product")
        horizons = value["horizons"]
        barriers = value["barriers_atr"]
        if not isinstance(horizons, list) or not 1 <= len(horizons) <= 5 or len(set(horizons)) != len(horizons):
            raise ValueError("horizons must be a distinct bounded list")
        if not isinstance(barriers, list) or not 1 <= len(barriers) <= 5 or len(set(barriers)) != len(barriers):
            raise ValueError("barriers_atr must be a distinct bounded list")
        for h in horizons:
            for b in barriers:
                PathScenario(h, b, b)
        for name, low, high in (("min_win_probability", 0, 1), ("max_ambiguous_probability", 0, 1), ("min_ev", 0, 1e12)):
            x = value[name]
            if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or not low <= x <= high:
                raise ValueError(f"invalid {name}")
        n, seed = value["min_samples"], value["seed"]
        if isinstance(n, bool) or not isinstance(n, int) or not 2 <= n <= 10000:
            raise ValueError("min_samples must be in 2..10000")
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**63:
            raise ValueError("seed must be a nonnegative 63-bit integer")
        route = PathScenario(value["action_horizon"], value["routing_barrier_atr"], value["routing_barrier_atr"])
        if route.horizon not in horizons or route.upper_atr not in barriers:
            raise ValueError("routing scenario must be included in path scenarios")
        return cls(
            tuple(symbols), ScenarioConfig.from_mapping(value["economics"]), tuple(sorted(horizons)),
            tuple(float(b) for b in sorted(barriers)), route.horizon, float(route.upper_atr), n,
            float(value["min_win_probability"]), float(value["min_ev"]), float(value["max_ambiguous_probability"]), seed,
        )

    @property
    def path_scenarios(self) -> tuple[PathScenario, ...]:
        return tuple(PathScenario(h, b, b) for h in self.horizons for b in self.barriers_atr)

    @property
    def route_scenario(self) -> PathScenario:
        return PathScenario(self.action_horizon, self.routing_barrier_atr, self.routing_barrier_atr)

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["symbols"] = list(self.symbols)
        value["economics"] = self.economics.to_dict()
        value["horizons"] = list(self.horizons)
        value["barriers_atr"] = list(self.barriers_atr)
        return value


def run_probability_research(input_bundle, output, config_path) -> dict[str, object]:
    from collections import Counter
    from pathlib import Path

    from ak_trading.probability.artifacts import (
        ROOT, digest, json_bytes, jsonable, load_frozen_input, publish_result,
        rows_csv, safe_path, strict_json, validate_output,
    )
    from ak_trading.probability.episodes import action_specs, simulate_action
    from ak_trading.probability.factors import build_factor_rows, factor_registry
    from ak_trading.probability.labels import label_paths
    from ak_trading.probability.models import (
        estimate_payoff, evaluate_predictions, fit_payoff_model, fit_probability_model,
        payoff_model_dict, predict_probability, predict_unconditional_probability, probability_model_dict,
    )
    from ak_trading.probability.partition import build_partition_plan, partition_event
    from ak_trading.probability.routing import aggregate_selected, choose_strategy, select_nonoverlapping

    source, config_file = safe_path(Path(input_bundle)), safe_path(Path(config_path))
    destination = validate_output(Path(output), source, config_file)
    if config_file.stat().st_size > 32768:
        raise ValueError("research config exceeds 32 KiB")
    config_raw = config_file.read_bytes()
    config_value = strict_json(config_raw, decimal_floats=True)
    if not isinstance(config_value, dict):
        raise ValueError("research configuration must be an object")
    from decimal import Decimal
    for name in ("routing_barrier_atr", "min_win_probability", "min_ev", "max_ambiguous_probability"):
        if isinstance(config_value.get(name), Decimal):
            config_value[name] = float(config_value[name])
    if isinstance(config_value.get("barriers_atr"), list):
        config_value["barriers_atr"] = [float(x) if isinstance(x, Decimal) else x for x in config_value["barriers_atr"]]
    config = ResearchConfig.from_mapping(config_value)
    bundle, manifest_hash = load_frozen_input(source)
    declarations = {}
    seen_ids = set()
    for row in bundle.contracts:
        symbol, identity = row.get("raw_symbol"), row.get("instance_id")
        if not isinstance(symbol, str) or not isinstance(identity, str) or not identity or identity in seen_ids:
            raise ValueError("invalid or duplicate contract identity")
        seen_ids.add(identity)
        declarations.setdefault(symbol, []).append(row)
    selected = {}
    inventory = []
    for symbol in config.symbols:
        found = declarations.get(symbol, [])
        if len(found) != 1:
            raise ValueError("selected raw symbol must resolve to exactly one contract instance")
        identity = found[0]
        from datetime import datetime
        expected_product = re.sub(r"[0-9]+$", "", symbol).upper()
        if identity.get("product") != expected_product:
            raise ValueError("contract product does not match symbol")
        if any(identity.get(key, "FUTURE") != "FUTURE" for key in ("asset_class", "instrument_class", "ins_class")):
            raise ValueError("contract must be a FUTURE")
        try:
            expiry = datetime.fromisoformat(identity["expiry_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("contract requires an explicit expiry") from exc
        if expiry.utcoffset() is None or identity["instance_id"] != f"{symbol}|{expiry.isoformat()}":
            raise ValueError("contract instance must match symbol and aware expiry")
        bars = tuple(sorted((bar for bar in bundle.bars if bar.symbol == symbol), key=lambda bar: bar.datetime_nano))
        if len(bars) < 120:
            raise ValueError("selected contract requires at least 120 stored observations")
        if len(bars) > 10000:
            raise ValueError("selected contract exceeds 10000 observations")
        selected[symbol] = (identity, bars)
        inventory.append({"symbol": symbol, "contract_id": identity["instance_id"], "product": identity.get("product"),
                          "rows": len(bars), "first_nano": bars[0].datetime_nano, "last_nano": bars[-1].datetime_nano,
                          "identity_status": identity.get("identity_status", "unknown")})
    products = {str(identity.get("product")) for identity, _ in selected.values()}
    if len(products) != 1:
        raise ValueError("input identities disagree with same-product cost configuration")
    if sum(len(bars) for _, bars in selected.values()) > 30000:
        raise ValueError("selected study exceeds 30000 observations")
    files = sorted((ROOT / "src/ak_trading/probability").glob("*.py")) + [
        ROOT / "src/ak_trading/futures/spec.py", ROOT / "src/ak_trading/futures/structures.py",
        ROOT / "src/ak_trading/futures/scenario_backtest.py", ROOT / "src/ak_trading/futures/io.py",
        ROOT / "scripts/research_probability_strategies.py",
    ]
    hashes = {str(p.relative_to(ROOT)).replace("\\", "/"): digest(p.read_bytes()) for p in files}
    events = []
    bars_by_contract = {}
    for symbol, (identity, bars) in selected.items():
        contract_id = identity["instance_id"]
        bars_by_contract[contract_id] = bars
        events.extend(build_factor_rows(contract_id, symbol, str(identity["product"]), bars))
    events.sort(key=lambda e: (e.timestamp_nano, e.contract_id, e.index))
    split = build_partition_plan(e.timestamp_nano for e in events)
    maximum_horizon = max(*config.horizons, config.action_horizon)
    partitions = {}
    labels = []
    route_labels = {}
    for event in events:
        bars = bars_by_contract[event.contract_id]
        terminal = bars[event.index + maximum_horizon].datetime_nano if event.index + maximum_horizon < len(bars) else None
        partitions[event.event_id] = partition_event(event, terminal, split)
        for scenario in config.path_scenarios:
            label = label_paths(event, bars, scenario)
            labels.append(label)
            if scenario.scenario_id == config.route_scenario.scenario_id:
                route_labels[event.event_id] = label
    by_id = {event.event_id: event for event in events}
    pairs = [(by_id[label.event_id], label) for label in labels]
    train = [(event, label) for event, label in pairs if partitions[event.event_id] == "train"]
    calibration = [(event, label) for event, label in pairs if partitions[event.event_id] == "calibration"]
    model = fit_probability_model(train, calibration, min_samples=config.min_samples)
    specs = action_specs(config.action_horizon)
    active_specs = [spec for spec in specs if spec.name != "cash"]
    outcomes = []
    for event in events:
        if event.status != "ready" or partitions[event.event_id] not in {"train", "calibration", "test"}:
            continue
        for spec in active_specs:
            outcomes.append(simulate_action(event, bars_by_contract[event.contract_id], spec, config.economics))
    payoff_rows = [(by_id[row.event_id], route_labels[row.event_id], row) for row in outcomes if partitions[row.event_id] == "train"]
    payoff_model = fit_payoff_model(payoff_rows, min_samples=config.min_samples)
    test_events = [event for event in events if partitions[event.event_id] == "test" and event.status == "ready"]
    if not test_events:
        raise ValueError("no ready final-test observations after chronological purging")
    predictions = [predict_probability(model, event, scenario.scenario_id) for event in test_events for scenario in config.path_scenarios]
    test_labels = [label for label in labels if partitions[label.event_id] == "test" and by_id[label.event_id].status == "ready"]
    metrics = evaluate_predictions(predictions, test_labels)
    baseline_predictions = [predict_unconditional_probability(model, event, scenario.scenario_id) for event in test_events for scenario in config.path_scenarios]
    baseline_metrics = evaluate_predictions(baseline_predictions, test_labels)
    prediction_lookup = {(row.event_id, row.scenario_id): row for row in predictions}
    estimates = []
    estimates_by_event = {}
    for event in test_events:
        prediction = prediction_lookup[event.event_id, config.route_scenario.scenario_id]
        current = [estimate_payoff(payoff_model, event, spec.action_id, prediction) for spec in active_specs]
        estimates.extend(current)
        estimates_by_event[event.event_id] = current
    scheduled_ids = set(select_nonoverlapping(test_events, config.action_horizon))
    # The schedule is fixed by event identity and horizon before any outcome is selected.
    selections = []
    for event in test_events:
        if event.event_id not in scheduled_ids:
            continue
        prediction = prediction_lookup[event.event_id, config.route_scenario.scenario_id]
        for mode in ("best_ev", "probability_sample", "cash"):
            selections.append(choose_strategy(
                event.event_id, mode, estimates_by_event[event.event_id], prediction,
                min_win_probability=config.min_win_probability, min_ev=config.min_ev,
                min_train_samples=config.min_samples, max_ambiguous_probability=config.max_ambiguous_probability,
                seed=config.seed,
            ))
        for spec in active_specs:
            choice = choose_strategy(event.event_id, "fixed", (), prediction, fixed_action_id=spec.action_id)
            from dataclasses import replace
            selections.append(replace(choice, mode=f"fixed:{spec.name}"))
    comparison = aggregate_selected(selections, outcomes, test_events, horizon=config.action_horizon)
    report = {
        "schema_version": "probability_strategy_research_v1", "status": comparison["status"],
        "scope": "same_product_contract_episodes_not_live_portfolio",
        "clock_status": "raw_daily_observation_only", "historical_strict_execution": False,
        "final_holdout_consumed": True, "economic_assumptions": "explicit_not_historical_verified",
        "counts": {"contracts": len(selected), "factor_rows": len(events), "labels": len(labels),
                   "action_outcomes": len(outcomes), "predictions": len(predictions),
                   "scheduled_test_opportunities": len(scheduled_ids), "selections": len(selections)},
        "partition_counts": dict(Counter(partitions.values())), "factor_status_counts": dict(Counter(e.status for e in events)),
        "label_status_counts": dict(Counter(label.status for label in labels)),
        "probability_model_status_counts": dict(Counter(p.model_status for p in predictions)),
        "payoff_status_counts": dict(Counter(p.status for p in estimates)),
        "comparison": comparison, "calibration_metrics": metrics,
        "unconditional_probability_baseline": baseline_metrics,
        "limitations": [
            "This retrospective baseline has not demonstrated production calibration or profitability.",
            "Current ER/MA regime is heuristic; predicted regime is a future ATR-displacement label.",
            "Within-partition training/calibration labels overlap and are dependent; standard-error penalties are heuristic.",
            "The same historical input may have been examined in prior research; this is not a pristine investment holdout.",
            "Barrier ambiguity is modeled as a separate outcome; missing conditional payoff support leads to abstention.",
            "Routing weights are declared decision-policy weights, not calibrated probabilities that a strategy is best.",
            "Grid uses completed-close confirmation and next-open execution, not intrabar resting limit orders.",
            "Opportunity outcomes start from independent fixed cash; per-contract cumulative episode PnL is not a reinvested portfolio NAV.",
            "No audited historical sessions, tick-grid execution, liquidity fills or contract rolls are inferred.",
        ],
    }
    event_rows = [dict(jsonable(event), partition=partitions[event.event_id]) for event in events]
    label_rows = []
    for label in labels:
        row = jsonable(label)
        atr = by_id[label.event_id].values.get("atr14")
        for field_name, target_name in (("terminal_points", "terminal_atr"), ("mfe_points", "mfe_atr"), ("mae_points", "mae_atr")):
            value = getattr(label, field_name)
            scaled = value / atr if value is not None and atr is not None and atr > 0 else None
            row[target_name] = scaled if scaled is not None and math.isfinite(scaled) else None
        label_rows.append(row)
    payloads = {
        "config.json": json_bytes(config.to_dict()), "factor_registry.json": json_bytes(factor_registry()),
        "sample.json": json_bytes({"contracts": inventory, "selection": "explicit_config_symbols_no_performance_selection"}),
        "splits.json": json_bytes(dict(jsonable(split), maximum_label_horizon=maximum_horizon, partition_counts=report["partition_counts"])),
        "factors.csv": rows_csv(event_rows), "labels.csv": rows_csv(label_rows),
        "strategy_specs.json": json_bytes(specs), "action_outcomes.csv": rows_csv([jsonable(x) for x in outcomes]),
        "models.json": json_bytes({"probability": probability_model_dict(model), "payoff": payoff_model_dict(payoff_model)}),
        "predictions.csv": rows_csv([jsonable(x) for x in predictions]),
        "payoff_estimates.csv": rows_csv([jsonable(x) for x in estimates]),
        "selections.csv": rows_csv([jsonable(x) for x in selections]),
        "calibration.json": json_bytes({"conditional": metrics, "unconditional_baseline": baseline_metrics}), "report.json": json_bytes(report),
    }
    if hashes != {str(p.relative_to(ROOT)).replace("\\", "/"): digest(p.read_bytes()) for p in files}:
        raise ValueError("source code changed during research")
    validate_output(destination, source, config_file)
    publish_result(destination, payloads, {
        "schema_version": "probability_strategy_research_v1", "input_manifest_sha256": manifest_hash,
        "config_sha256": digest(config_raw), "source_code_hashes": hashes,
        "historical_strict_execution": False, "final_holdout_consumed": True,
    })
    return {"status": report["status"], "output": str(destination), "counts": report["counts"],
            "payoff_status_counts": report["payoff_status_counts"], "historical_strict_execution": False}
