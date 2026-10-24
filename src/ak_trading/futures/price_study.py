"""Offline retrospective price associations, not a trading backtest."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Mapping

from ak_trading.futures.io import (
    assert_output_directory_is_new,
    canonical_json_bytes,
    read_input_bundle,
    sha256_file,
    write_canonical_csv,
)

SCHEMA_VERSION = "futures_price_study_v1"
HORIZONS = (1, 5, 20)
SIGNAL_COLUMNS = (
    "contract_instance_id", "raw_symbol", "product", "strategy_id", "index",
    "datetime_nano", "candidate_direction", "feature_status", "reason",
    "target_position", "non_actionable", "diagnostics",
)
OUTCOME_COLUMNS = (
    "contract_instance_id", "raw_symbol", "product", "strategy_id", "index",
    "horizon", "partition", "partition_start", "partition_end", "entry_index",
    "exit_index", "decision_datetime_nano", "entry_datetime_nano", "exit_datetime_nano",
    "candidate_direction", "feature_status", "label_status", "reason",
    "entry_open", "exit_close", "raw_change", "directional_change",
    "eligible_directional", "elapsed_calendar_days", "gap_status",
)
LIMITATIONS = (
    "Retrospective reconstructed-history price associations; not point-in-time executable signals.",
    "No fees, slippage, margin, position sizing, fills or net trading PnL are modeled.",
    "Contracts are a deterministic coverage-convenience sample, not a survivorship-free universe.",
    "Horizons count stored observations, not verified trading days; unobserved missing sessions cannot be inferred.",
    "Overlapping horizons and events across contracts are dependent; no independent-sample significance is claimed.",
    "Train/development/holdout are per-contract observation partitions, not market-wide chronological folds.",
    "All parameters are fixed; held-out descriptive outcomes are now consumed and cannot be called unseen again.",
    "The first three strategies describe independent candidate events, not their full holding/exit behavior.",
    "Expiry and import timestamps remain reference metadata, not verified historical publication evidence.",
)


def _signal_row(signal) -> dict[str, object]:
    return {
        "contract_instance_id": signal.contract_instance_id,
        "raw_symbol": signal.raw_symbol,
        "product": signal.product,
        "strategy_id": signal.strategy_id,
        "index": signal.index,
        "datetime_nano": signal.datetime_nano,
        "candidate_direction": signal.candidate_direction,
        "feature_status": signal.feature_status,
        "reason": signal.reason,
        "target_position": None,
        "non_actionable": "true",
        "diagnostics": json.dumps(dict(signal.diagnostics), sort_keys=True,
                                  ensure_ascii=False, allow_nan=False, separators=(",", ":")),
    }


def _outcome_row(row: Mapping[str, object]) -> dict[str, object]:
    converted = {key: row.get(key) for key in OUTCOME_COLUMNS}
    converted["eligible_directional"] = "true" if row.get("eligible_directional") is True else "false"
    return converted


def run_price_study(
    bundle_path: Path,
    output: Path,
    *,
    max_contracts_per_product: int = 3,
    min_rows: int = 120,
) -> dict[str, object]:
    from ak_trading.futures.study_sample import select_study_sample
    from ak_trading.futures.study_signals import generate_study_signals
    from ak_trading.futures.study_labels import label_study_outcomes, summarize_study_outcomes

    bundle_path, output = Path(bundle_path).resolve(), Path(output).resolve()
    assert_output_directory_is_new(output, (bundle_path,))
    source_manifest_sha = sha256_file(bundle_path / "manifest.json")
    bundle = read_input_bundle(bundle_path)
    if sha256_file(bundle_path / "manifest.json") != source_manifest_sha:
        raise ValueError("input_manifest_changed_during_read")
    sample = select_study_sample(
        bundle, max_contracts_per_product=max_contracts_per_product, min_rows=min_rows
    )
    if not sample.selected:
        return {"status": "blocked", "reason": "no_selected_contracts",
                "inventory": list(sample.inventory)}

    signals = generate_study_signals(sample.selected, sample.universe)
    outcomes = label_study_outcomes(sample.selected, signals, horizons=HORIZONS)
    summary = summarize_study_outcomes(outcomes)
    selected_rows = sum(len(contract.bars) for contract in sample.selected)
    if len(signals) != selected_rows * 4 or len(outcomes) != len(signals) * len(HORIZONS):
        raise ValueError("study_output_count_mismatch")
    if any(signal.candidate_direction not in (-1, 0, 1, None)
           or isinstance(signal.candidate_direction, bool) for signal in signals):
        raise ValueError("invalid_candidate_direction")

    counts = {
        "selected_contracts": len(sample.selected),
        "selected_bar_rows": selected_rows,
        "signals": len(signals),
        "outcomes": len(outcomes),
        "eligible_directional_outcomes": sum(row.get("eligible_directional") is True for row in outcomes),
    }
    sample_payload = {
        "config": dict(sample.config),
        "inventory": list(sample.inventory),
        "selected": [{
            "contract_instance_id": contract.contract_instance_id,
            "raw_symbol": contract.raw_symbol,
            "product": contract.product,
            "expiry_at": contract.expiry_at.isoformat(),
            "bar_rows": len(contract.bars),
            "split_indices": [0, 3 * len(contract.bars) // 5, 4 * len(contract.bars) // 5, len(contract.bars)],
        } for contract in sample.selected],
    }
    from dataclasses import asdict
    from ak_trading.futures.strategies import default_registry
    registry = default_registry()
    fixed_parameters = {key: asdict(strategy) for key, strategy in sorted(registry.items())}
    # JSON string encoding of Decimal defaults is explicit and stable.
    fixed_parameters = json.loads(json.dumps(fixed_parameters, default=str, sort_keys=True, allow_nan=False))
    config = {
        "schema_version": SCHEMA_VERSION,
        "max_contracts_per_product": max_contracts_per_product,
        "min_rows": min_rows,
        "horizons": list(HORIZONS),
        "partition_rule": "per_contract_observation_60_20_20_v1",
        "parameter_selection": "fixed_no_optimization",
        "strategy_parameters": fixed_parameters,
        "price_change_formula": "close[i+h] / open[i+1] - 1",
    }
    source_names = (
        "price_study.py", "study_spec.py", "study_sample.py", "study_signals.py", "study_labels.py",
        "strategies.py", "structures.py", "data.py", "spec.py", "io.py",
    )
    code_hashes = {name: sha256_file(Path(__file__).parent / name) for name in source_names}
    report = {
        "schema_version": SCHEMA_VERSION,
        "dataset_kind": "real_retrospective_price_study",
        "status": "published",
        "non_actionable": True,
        "strict_execution_enabled": False,
        "account_artifacts": "not_requested",
        "point_in_time_execution": "unavailable_unverified_historical_clocks",
        "source_manifest_sha256": source_manifest_sha,
        "counts": counts,
        "products": sorted({contract.product for contract in sample.selected}),
        "label_status_counts": dict(sorted(Counter(str(row.get("label_status")) for row in outcomes).items())),
        "feature_status_counts": dict(sorted(Counter(signal.feature_status for signal in signals).items())),
        "holdout_consumed": True,
        "parameter_selection": "fixed_no_optimization",
        "limitations": list(LIMITATIONS),
        "summary_file": "summary.json",
    }

    # No destination is created until input, calculations and finite JSON validate.
    payloads = {"sample.json": canonical_json_bytes(sample_payload),
                "config.json": canonical_json_bytes(config),
                "summary.json": canonical_json_bytes(summary),
                "report.json": canonical_json_bytes(report)}
    output.mkdir(parents=True, exist_ok=False)
    try:
        for name, content in payloads.items():
            (output / name).write_bytes(content)
        write_canonical_csv(output / "signals.csv", SIGNAL_COLUMNS, [_signal_row(signal) for signal in signals])
        write_canonical_csv(output / "outcomes.csv", OUTCOME_COLUMNS, [_outcome_row(row) for row in outcomes])
        names = sorted((*payloads, "signals.csv", "outcomes.csv"))
        hashes = {name: sha256_file(output / name) for name in names}
        (output / "manifest.json").write_bytes(canonical_json_bytes({
            "schema_version": SCHEMA_VERSION,
            "status": "complete",
            "completion_basis": "retrospective_study_publication_not_trading_validation",
            "source_manifest_sha256": source_manifest_sha,
            "source_payload_hashes": dict(bundle.manifest["content_hashes"]),
            "code_hashes": code_hashes,
            "content_hashes": hashes,
            "strict_execution_enabled": False,
        }))
    except Exception:
        # Partial outputs remain inspectable, but no success marker survives.
        (output / "manifest.json").unlink(missing_ok=True)
        raise
    return {"status": "published", "output": str(output), "counts": counts}
