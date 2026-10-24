"""Offline integration contracts for the retrospective price-study API and CLI.

Prices below are generated test data in a real_export envelope, never evidence
about a market or a database export. Only published files and public entry
points are tested; the sampling, strategies and labeler are not mocked.
"""
from __future__ import annotations

import csv
import importlib.util
import inspect
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "study_futures_signals.py"
SH = timezone(timedelta(hours=8))
CREATED_AT = "2024-05-01T16:00:00+08:00"
ROWS = 125
CONTRACTS = (
    ("SHFE.rb2405", "2024-05-15T15:00:00+08:00"),
    ("SHFE.rb2410", "2024-10-15T15:00:00+08:00"),
)
STRATEGIES = {"donchian_trend", "ma_trend", "zscore_reversion", "term_structure_carry"}
OUTPUT_FILES = {
    "sample.json", "signals.csv", "outcomes.csv", "summary.json",
    "report.json", "config.json", "manifest.json",
}


def _install_offline_guards(install, attempts):
    """Also runnable in a fresh interpreter, before importing application code."""
    import builtins
    import importlib
    import io
    import os
    import socket

    forbidden = (
        "dotenv", "tqsdk", "sqlalchemy", "psycopg", "psycopg2",
        "ak_trading.persistence", "requests", "httpx",
    )
    original_import = builtins.__import__
    original_import_module = importlib.import_module

    def check_import(name):
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden):
            attempts.append(name)
            raise AssertionError(f"offline study imported {name}")

    def guarded_import(name, *args, **kwargs):
        check_import(name)
        return original_import(name, *args, **kwargs)

    def guarded_import_module(name, package=None):
        absolute = importlib.util.resolve_name(name, package) if name.startswith(".") else name
        check_import(absolute)
        return original_import_module(name, package)

    def guard_open(original):
        def guarded(file, *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)):
                basename = os.fsdecode(file).replace("\\", "/").rsplit("/", 1)[-1].lower()
                if basename == ".env" or basename.startswith(".env.") or basename == "nul":
                    attempts.append(basename)
                    raise AssertionError(f"offline study opened {basename}")
            return original(file, *args, **kwargs)
        return guarded

    def no_network(*args, **kwargs):
        attempts.append("network")
        raise AssertionError("offline study attempted network access")

    install(builtins, "__import__", guarded_import)
    install(importlib, "import_module", guarded_import_module)
    install(builtins, "open", guard_open(builtins.open))
    install(io, "open", guard_open(io.open))
    install(os, "open", guard_open(os.open))
    install(socket.socket, "connect", no_network)
    install(socket.socket, "connect_ex", no_network)
    install(socket, "create_connection", no_network)
    install(socket, "getaddrinfo", no_network)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    attempts = []
    _install_offline_guards(monkeypatch.setattr, attempts)
    yield
    assert attempts == [], f"offline boundary attempts: {attempts}"


def _publish_fixture(path, *, row_count=ROWS, dataset_kind="real_export"):
    from ak_trading.futures.io import bars_rows_from_bars, publish_input_bundle
    from ak_trading.futures.spec import ContractBar

    observations = []
    cursor = datetime(2023, 11, 1, 15, tzinfo=SH)
    while len(observations) < row_count:
        if cursor.weekday() < 5:
            observations.append(cursor)
        cursor += timedelta(days=1)
    bars = []
    contracts = []
    for contract_number, (symbol, expiry) in enumerate(CONTRACTS):
        contracts.append({
            "instance_id": f"{symbol}|{expiry}", "raw_symbol": symbol,
            "product": "SHFE.RB", "expiry_at": expiry,
            "identity_status": "reconstructed_static", "listing_at": None,
            "identity_issues": ["listing_at_missing"],
        })
        # Opposite trends and distinct price levels expose accidental labels
        # from another contract while retaining nonzero candidate directions.
        direction = 1 if contract_number == 0 else -1
        base = 3500 if contract_number == 0 else 5000
        for index, ts in enumerate(observations):
            open_price = Decimal(base + direction * 2 * index)
            close_price = open_price + direction
            bars.append(ContractBar(
                symbol=symbol, duration_seconds=86400,
                datetime_nano=int(ts.timestamp()) * 1_000_000_000,
                ts=ts, trading_day=ts.date(),
                source="offline_generated_test_fixture",
                trading_day_source="unverified_weekday_fixture",
                available_at=datetime.fromisoformat(CREATED_AT),
                open=open_price, close=close_price,
                high=max(open_price, close_price) + 3,
                low=min(open_price, close_price) - 3,
                volume=Decimal("1000"), open_oi=None, close_oi=None,
            ))
    publish_input_bundle(
        path, dataset_kind=dataset_kind, created_at=CREATED_AT,
        bars_rows=bars_rows_from_bars(bars), contracts=contracts,
        metadata={"fields": {}, "revision_history_unavailable": True},
        availability={}, request_info={"products": ["SHFE.RB"], "duration_seconds": 86400},
        source_hashes={}, clock_policy={"d_status": "unavailable"},
        read_only_transaction={"executed": False, "reason": "generated_fixture_no_database"},
        notes={"fixture": "Generated prices for tests only; not real-market evidence."},
    )
    return path


@pytest.fixture
def input_bundle(tmp_path):
    return _publish_fixture(tmp_path / "input")


def test_verified_contract_bytes_are_not_reopened(input_bundle, monkeypatch):
    from ak_trading.futures.io import read_input_bundle
    target = input_bundle / "contracts.json"
    original = Path.read_bytes
    calls = []

    def change_after_read(path):
        payload = original(path)
        if path == target:
            calls.append(path)
            target.write_text("[]", encoding="utf-8")
        return payload

    monkeypatch.setattr(Path, "read_bytes", change_after_read)
    bundle = read_input_bundle(input_bundle)
    assert len(bundle.contracts) == 2
    assert len(calls) == 1


@pytest.mark.parametrize("field,value", [("open", "not-a-number"), ("datetime_nano", "")])
def test_malformed_numeric_csv_has_structured_cli_rejection(input_bundle, cli, tmp_path, capsys, field, value):
    from ak_trading.futures.io import BARS_CSV_COLUMNS, write_canonical_csv
    target = input_bundle / "bars.csv"
    with target.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows[0][field] = value
    digest = write_canonical_csv(target, BARS_CSV_COLUMNS, rows)
    manifest_path = input_bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["content_hashes"]["bars.csv"] = digest
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    destination = tmp_path / "invalid-output"
    assert cli.main(["--input-bundle", str(input_bundle), "--output", str(destination)]) == 2
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["status"] == "rejected"
    assert not destination.exists()


@pytest.fixture
def cli():
    spec = importlib.util.spec_from_file_location("futures_price_study_test_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json(path):
    return json.loads(path.read_bytes())


def _csv(path):
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _tree_bytes(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


def _arguments(source, output, *extra):
    return ["--input-bundle", str(source), "--output", str(output), *extra]


def test_public_api_and_fresh_offline_cli_reproduce_all_seven_files(input_bundle, tmp_path):
    from ak_trading.futures.price_study import run_price_study

    source_before = _tree_bytes(input_bundle)
    first = tmp_path / "api-study"
    result = run_price_study(input_bundle, first)
    assert isinstance(result, dict) and result["status"] == "published"
    payloads = _tree_bytes(first)
    assert set(payloads) == OUTPUT_FILES
    assert all(isinstance(payload, bytes) for payload in payloads.values())
    manifest = _json(first / "manifest.json")
    assert manifest["schema_version"] == "futures_price_study_v1"
    assert manifest["status"] == "complete"
    assert manifest["source_manifest_sha256"] == sha256(source_before["manifest.json"]).hexdigest()
    assert manifest["content_hashes"] == {
        name: sha256(payloads[name]).hexdigest() for name in OUTPUT_FILES - {"manifest.json"}
    }

    # Different interpreter/hash seed, same frozen source and explicit CLI
    # defaults. Guards are installed before the CLI or its dependency imports.
    bootstrap = inspect.getsource(_install_offline_guards) + """
import runpy
import sys
attempts = []
_install_offline_guards(setattr, attempts)
script = sys.argv.pop(1)
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as exc:
    assert exc.code == 0, f"CLI exit code: {exc.code}"
assert attempts == [], attempts
"""
    environment = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR")
                   if name in os.environ}
    environment.update({
        "PYTHONHASHSEED": "8675309", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1", "AK_DATABASE_URL": "",
        "TEMP": str(tmp_path), "TMP": str(tmp_path),
    })
    replay = tmp_path / "cli-study"
    process = subprocess.run(
        [sys.executable, "-B", "-c", bootstrap, str(SCRIPT),
         *_arguments(input_bundle, replay, "--max-contracts-per-product", "3", "--min-rows", "120")],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=60,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    assert _tree_bytes(replay) == payloads  # Includes manifest, config and every payload byte.
    assert _tree_bytes(input_bundle) == source_before


def test_counts_fixed_parameters_and_null_targets_never_claim_trading(input_bundle, tmp_path):
    from ak_trading.futures.price_study import run_price_study

    output = tmp_path / "one-contract"
    result = run_price_study(input_bundle, output, max_contracts_per_product=1, min_rows=125)
    assert result["status"] == "published"
    assert set(_tree_bytes(output)) == OUTPUT_FILES
    report = _json(output / "report.json")
    summary = _json(output / "summary.json")
    config = _json(output / "config.json")
    sample = _json(output / "sample.json")
    signals = _csv(output / "signals.csv")
    outcomes = _csv(output / "outcomes.csv")
    assert isinstance(summary, dict) and summary
    assert report["dataset_kind"] == "real_retrospective_price_study"
    assert report["non_actionable"] is True
    assert report["strict_execution_enabled"] is False
    assert report["account_artifacts"] == "not_requested"
    assert report["holdout_consumed"] is True
    assert report["limitations"] and all(isinstance(item, str) and item for item in report["limitations"])
    for metric in ("net_pnl", "sharpe", "annualized_sharpe", "equity_curve", "p_value"):
        assert report.get(metric) is None and summary.get(metric) is None
    assert config["max_contracts_per_product"] == 1
    assert config["min_rows"] == 125
    assert config["horizons"] == [1, 5, 20]
    assert config["parameter_selection"] == "fixed_no_optimization"
    assert set(config["strategy_parameters"]) == STRATEGIES
    iid = f"{CONTRACTS[0][0]}|{CONTRACTS[0][1]}"
    assert [item["contract_instance_id"] for item in sample["selected"]] == [iid]
    assert sample["selected"][0]["split_indices"] == [0, 75, 100, 125]
    assert sample["inventory"]
    assert len(signals) == 500 and len(outcomes) == 1500
    assert Counter((row["contract_instance_id"], int(row["index"])) for row in signals) == {
        (iid, index): 4 for index in range(ROWS)
    }
    assert {row["strategy_id"] for row in signals} == STRATEGIES
    assert all(row["target_position"] == "" and row["non_actionable"] == "true" for row in signals)
    assert {row["candidate_direction"] for row in signals} <= {"", "0", "1", "-1"}
    assert any(row["candidate_direction"] == "" for row in signals)
    eligible = [row for row in outcomes if row["eligible_directional"] == "true"]
    assert 0 < len(eligible) < len(outcomes)
    assert all(row["candidate_direction"] in {"-1", "1"} and row["directional_change"] != ""
               for row in eligible)
    assert all(row["directional_change"] == "" and row["eligible_directional"] == "false"
               for row in outcomes if row["candidate_direction"] in {"", "0"})
    assert summary["counts"]["n_total"] == 1500
    assert summary["counts"]["n_signals"] == 500
    assert summary["counts"]["n_contracts"] == 1
    assert summary["counts"]["n_eligible"] == len(eligible)
    assert summary["counts"]["n_valid"] == sum(row["label_status"] == "valid" for row in outcomes)
    assert summary["counts"]["label_status_counts"] == report["label_status_counts"] == dict(
        Counter(row["label_status"] for row in outcomes)
    )
    assert report["counts"] == result["counts"] == {
        "selected_contracts": 1, "selected_bar_rows": 125, "signals": 500,
        "outcomes": 1500, "eligible_directional_outcomes": len(eligible),
    }


def test_labels_stay_within_each_contract_and_observation_partition(input_bundle, cli, tmp_path):
    from ak_trading.futures.io import read_input_bundle

    output = tmp_path / "partition-study"
    assert cli.main(_arguments(input_bundle, output)) == 0
    rows = _csv(output / "outcomes.csv")
    source = read_input_bundle(input_bundle)
    by_symbol = {symbol: sorted((bar for bar in source.bars if bar.symbol == symbol),
                                key=lambda bar: bar.datetime_nano) for symbol, _ in CONTRACTS}
    expected_ids = {f"{symbol}|{expiry}" for symbol, expiry in CONTRACTS}
    assert len(rows) == 3000
    assert {row["contract_instance_id"] for row in rows} == expected_ids
    assert Counter((row["contract_instance_id"], row["strategy_id"], int(row["index"]), int(row["horizon"]))
                   for row in rows) == {
        (iid, strategy, index, horizon): 1
        for iid in expected_ids for strategy in STRATEGIES
        for index in range(ROWS) for horizon in (1, 5, 20)
    }
    partition_names = {}
    matured = Counter()
    for row in rows:
        index, horizon = int(row["index"]), int(row["horizon"])
        start, end = (0, 75) if index < 75 else (75, 100) if index < 100 else (100, 125)
        assert (int(row["partition_start"]), int(row["partition_end"])) == (start, end)
        partition_names.setdefault((start, end), set()).add(row["partition"])
        assert row["label_status"]
        if index + horizon >= end:
            assert row["raw_change"] == row["directional_change"] == ""
            assert row["eligible_directional"] == "false" and row["reason"]
            continue
        bars = by_symbol[row["raw_symbol"]]
        assert row["contract_instance_id"].split("|", 1)[0] == row["raw_symbol"]
        assert int(row["entry_index"]) == index + 1
        assert int(row["exit_index"]) == index + horizon
        assert int(row["decision_datetime_nano"]) == bars[index].datetime_nano
        assert int(row["entry_datetime_nano"]) == bars[index + 1].datetime_nano
        assert int(row["exit_datetime_nano"]) == bars[index + horizon].datetime_nano
        assert Decimal(row["entry_open"]) == bars[index + 1].open
        assert Decimal(row["exit_close"]) == bars[index + horizon].close
        assert Decimal(row["raw_change"]).is_finite()
        matured[(row["contract_instance_id"], horizon)] += 1
        if index == 50 and horizon == 5:
            # Worked examples: rb2405 enters at 3602, ends at 3611;
            # rb2410 enters at 4898, ends at 4889. No contract splicing.
            expected = 9 / 3602 if row["raw_symbol"] == "SHFE.rb2405" else -9 / 4898
            assert float(row["raw_change"]) == pytest.approx(expected)
    assert len(partition_names) == 3
    assert all(len(names) == 1 for names in partition_names.values())
    assert len(set.union(*partition_names.values())) == 3
    assert matured == {
        (iid, horizon): count for iid in expected_ids
        for horizon, count in ((1, 488), (5, 440), (20, 260))
    }


@pytest.mark.parametrize("problem", ["bad_hash", "legacy_schema", "synthetic_demo", "wrong_kind", "bad_type"])
def test_cli_rejects_untrusted_or_wrong_input_without_output(input_bundle, cli, tmp_path, problem):
    manifest_path = input_bundle / "manifest.json"
    manifest = _json(manifest_path)
    if problem == "bad_hash":
        bars_path = input_bundle / "bars.csv"
        bars_path.write_bytes(bars_path.read_bytes().replace(
            b"offline_generated_test_fixture", b"tampered_fixture", 1,
        ))
    else:
        if problem == "legacy_schema":
            manifest["schema_version"] = "normalized_research_v1"
        else:
            manifest["dataset_kind"] = {
                "synthetic_demo": "synthetic_demo", "wrong_kind": "signals",
                "bad_type": {"invalid": "dataset kind must be a string"},
            }[problem]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = _tree_bytes(input_bundle)
    output = tmp_path / "rejected-study"
    assert cli.main(_arguments(input_bundle, output)) == 2
    assert not output.exists()
    assert _tree_bytes(input_bundle) == before


@pytest.mark.parametrize("destination", ["existing_empty", "existing_file", "existing_content", "input", "child", "alias_child", "parent"])
def test_cli_refuses_existing_or_overlapping_outputs_without_mutation(input_bundle, cli, tmp_path, destination):
    output = tmp_path / "destination"
    if destination == "existing_empty":
        output.mkdir()
    elif destination == "existing_file":
        output.write_bytes(b"preserve this file")
    elif destination == "existing_content":
        output.mkdir()
        (output / "manifest.json").write_bytes(b"preserve existing manifest")
    elif destination == "input":
        output = input_bundle
    elif destination == "child":
        output = input_bundle / "study"
    elif destination == "alias_child":
        output = input_bundle / ".." / input_bundle.name / "study"
    else:
        output = input_bundle.parent
    before = _tree_bytes(tmp_path)
    assert cli.main(_arguments(input_bundle, output)) == 2
    assert _tree_bytes(tmp_path) == before


@pytest.mark.parametrize("input_kind", ["missing", "file"])
def test_missing_input_is_blocked_with_exit_three_and_no_output(cli, tmp_path, input_kind):
    source = tmp_path / "not-a-bundle"
    if input_kind == "file":
        source.write_bytes(b"not a directory")
    output = tmp_path / "blocked"
    assert cli.main(_arguments(source, output)) == 3
    assert not output.exists()


@pytest.mark.parametrize("row_count,min_rows", [(0, 120), (119, 120), (125, 126)])
def test_no_selected_contracts_blocks_api_and_cli_without_output(cli, tmp_path, row_count, min_rows):
    from ak_trading.futures.price_study import run_price_study

    source = _publish_fixture(tmp_path / "insufficient-input", row_count=row_count)
    before = _tree_bytes(source)
    api_output = tmp_path / "blocked-api"
    result = run_price_study(source, api_output, min_rows=min_rows)
    assert result["status"] == "blocked"
    assert result["reason"] == "no_selected_contracts"
    assert not api_output.exists()
    cli_output = tmp_path / "blocked-cli"
    assert cli.main(_arguments(source, cli_output, "--min-rows", str(min_rows))) == 3
    assert not cli_output.exists()
    assert _tree_bytes(source) == before


@pytest.mark.parametrize("extra", [
    ["--max-contracts-per-product", "0"], ["--max-contracts-per-product", "4"],
    ["--max-contracts-per-product", "not-an-integer"],
    ["--min-rows", "119"], ["--min-rows", "10001"], ["--min-rows", "120.5"],
    ["--demo"],
])
def test_invalid_cli_parameters_exit_through_argparse_before_output(cli, tmp_path, extra):
    output = tmp_path / "invalid-parameters"
    with pytest.raises(SystemExit) as rejected:
        cli.main(_arguments(tmp_path / "missing-input", output, *extra))
    assert rejected.value.code == 2
    assert not output.exists()
