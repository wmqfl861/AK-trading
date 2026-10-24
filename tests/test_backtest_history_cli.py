"""Scenario CLI contracts: offline inputs, deterministic artifacts, atomic status."""
from __future__ import annotations

import csv
import importlib.util
import inspect
import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from ak_trading.futures.io import bars_rows_from_bars, publish_input_bundle
from test_futures_scenario_backtest import SYMBOL, assumptions, bars_from_prices, bundle_for

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backtest_history.py"
OUTPUTS = {"config.json", "fills.csv", "equity.csv", "decisions.csv", "report.json", "manifest.json"}


@pytest.fixture
def cli():
    spec = importlib.util.spec_from_file_location("history_scenario_test_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def inputs(tmp_path):
    source = tmp_path / "input"
    bundle = bundle_for(bars_from_prices(list(range(100, 190))))
    publish_input_bundle(
        source, dataset_kind="real_export", created_at="2024-06-01T00:00:00+00:00",
        bars_rows=bars_rows_from_bars(bundle.bars), contracts=bundle.contracts,
        metadata={}, availability={}, request_info={}, source_hashes={"test_source": "a" * 64},
        read_only_transaction={"executed": False}, clock_policy={"d_status": "unavailable"},
    )
    config = tmp_path / "assumptions.json"
    config.write_text(json.dumps(assumptions()), encoding="utf-8")
    return source, config


def args(source, config, output, strategy="donchian_trend", symbol=SYMBOL):
    return ["--input-bundle", str(source), "--config", str(config), "--output", str(output),
            "--strategy", strategy, "--symbol", symbol]


def tree(path):
    return {file.relative_to(path).as_posix(): file.read_bytes() for file in path.rglob("*") if file.is_file()}


def read_csv(path):
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_cli_replays_every_artifact_deterministically_with_provenance(cli, inputs, tmp_path, capsys):
    source, config = inputs
    before = tree(source)
    first, second = tmp_path / "one", tmp_path / "nested" / "two"
    assert cli.main(args(source, config, first)) == 0
    assert cli.main(args(source, config, second)) == 0
    payloads = tree(first)
    assert set(payloads) == OUTPUTS
    assert payloads == tree(second)
    assert tree(source) == before
    manifest = json.loads(payloads["manifest.json"])
    report = json.loads(payloads["report.json"])
    published_config = json.loads(payloads["config.json"])
    assert manifest["status"] == report["status"] == "complete"
    assert manifest["historical_strict_execution"] is False
    assert manifest["input_manifest_sha256"] == sha256(before["manifest.json"]).hexdigest()
    assert manifest["input_config_sha256"] == sha256(config.read_bytes()).hexdigest()
    assert manifest["input_content_hashes"] == json.loads(before["manifest.json"])["content_hashes"]
    assert manifest["source_hashes"] == {"test_source": "a" * 64}
    assert manifest["content_hashes"] == {name: sha256(payloads[name]).hexdigest() for name in OUTPUTS - {"manifest.json"}}
    for name in ("src/ak_trading/futures/scenario_backtest.py", "src/ak_trading/futures/strategies.py",
                 "src/ak_trading/futures/structures.py", "src/ak_trading/futures/spec.py", "src/ak_trading/futures/io.py",
                 "scripts/backtest_history.py"):
        assert manifest["code_hashes"][name] == sha256((ROOT / name).read_bytes()).hexdigest()
    assert published_config["assumptions_verification"] == "not_historical_verified"
    assert published_config["terminal_policy"] == "precommitted_final_observation_close"
    assert published_config["strategy_parameters"] == {"entry_window": 40, "exit_window": 20}
    assert len(read_csv(first / "equity.csv")) == 90
    assert len(read_csv(first / "decisions.csv")) == 90
    assert len(read_csv(first / "fills.csv")) == 2
    assert report["event_order"] and report["limitations"]
    assert "sharpe" not in report and "annualized_sharpe" not in report
    assert all(row["decision_at"] == "" for row in read_csv(first / "decisions.csv"))
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "complete"


@pytest.mark.parametrize("missing", ["--input-bundle", "--symbol", "--strategy", "--config", "--output"])
def test_each_cli_argument_is_required(cli, inputs, tmp_path, missing):
    source, config = inputs
    output = tmp_path / "out"
    arguments = args(source, config, output)
    index = arguments.index(missing)
    del arguments[index:index + 2]
    with pytest.raises(SystemExit) as error:
        cli.main(arguments)
    assert error.value.code == 2
    assert not output.exists()


@pytest.mark.parametrize("extra", [["--symbol", SYMBOL], ["--config", "other.json"], ["--fee", "5"], ["--str", "ma_trend"]])
def test_cli_rejects_repeated_or_unknown_arguments(cli, inputs, tmp_path, extra):
    source, config = inputs
    output = tmp_path / "out"
    with pytest.raises(SystemExit) as error:
        cli.main(args(source, config, output) + extra)
    assert error.value.code == 2
    assert not output.exists()


@pytest.mark.parametrize("problem", ["missing", "unknown", "missing_key", "duplicate_key", "nonfinite", "not_object", "fractional_lots"])
def test_bad_config_creates_no_output(cli, inputs, tmp_path, problem):
    source, config = inputs
    if problem == "missing":
        config = tmp_path / "missing-config.json"
    elif problem == "unknown":
        config.write_text(json.dumps(assumptions(fee="1")))
    elif problem == "missing_key":
        values = assumptions()
        del values["multiplier"]
        config.write_text(json.dumps(values))
    elif problem == "duplicate_key":
        config.write_text(config.read_text().replace('"lots": 2', '"lots": 2, "lots": 1'))
    elif problem == "nonfinite":
        config.write_text(config.read_text().replace('"100000"', 'NaN'))
    elif problem == "fractional_lots":
        config.write_text(json.dumps(assumptions(lots=1.5)))
    else:
        config.write_text("[]")
    output = tmp_path / "out"
    assert cli.main(args(source, config, output)) == 2
    assert not output.exists()


@pytest.mark.parametrize("kind", ["existing_empty", "existing_file", "existing_content", "input", "child", "alias_child", "parent", "config_directory"])
def test_outputs_are_new_and_cannot_overlap_input_or_sources(cli, inputs, tmp_path, kind):
    source, config = inputs
    output = tmp_path / "out"
    if kind == "existing_empty":
        output.mkdir()
    elif kind == "existing_file":
        output.write_bytes(b"keep")
    elif kind == "existing_content":
        output.mkdir()
        (output / "manifest.json").write_bytes(b"keep")
    elif kind == "input":
        output = source
    elif kind == "child":
        output = source / "new"
    elif kind == "alias_child":
        output = source / ".." / source.name / "new"
    elif kind == "parent":
        output = source.parent
    else:
        output = ROOT / "config" / "forbidden-scenario-output"
    before = tree(tmp_path)
    assert cli.main(args(source, config, output)) == 2
    assert tree(tmp_path) == before
    if kind == "config_directory":
        assert not output.exists()


@pytest.mark.parametrize("name,value", [("open", "0"), ("close", "NaN"), ("high", "3"),
                                          ("datetime_nano", "bad"), ("duration_seconds", "60"), ("close", "")])
def test_invalid_interior_bundle_row_retained_as_rejection_without_output(cli, inputs, tmp_path, name, value):
    from ak_trading.futures.io import BARS_CSV_COLUMNS, write_canonical_csv
    source, config = inputs
    rows = read_csv(source / "bars.csv")
    rows[50][name] = value
    digest = write_canonical_csv(source / "bars.csv", BARS_CSV_COLUMNS, rows)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["content_hashes"]["bars.csv"] = digest
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = tree(source)
    output = tmp_path / "out"
    assert cli.main(args(source, config, output)) == 2
    assert not output.exists() and tree(source) == before


@pytest.mark.parametrize("problem", ["bad_hash", "incomplete", "manifest_list", "missing_payload", "not_bundle", "bad_optional_hash"])
def test_unverified_inputs_reject_before_publication(cli, inputs, tmp_path, problem):
    source, config = inputs
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    if problem == "bad_hash":
        (source / "bars.csv").write_bytes(b"tampered")
    elif problem == "incomplete":
        manifest["status"] = "incomplete"
        manifest_path.write_text(json.dumps(manifest))
    elif problem == "manifest_list":
        manifest_path.write_text("[]")
    elif problem == "missing_payload":
        (source / "contracts.json").unlink()
    elif problem == "bad_optional_hash":
        manifest["content_hashes"]["session_windows.csv"] = "b" * 64
        (source / "session_windows.csv").write_bytes(b"unknown sessions")
        manifest_path.write_text(json.dumps(manifest))
    else:
        source = tmp_path / "missing-input"
    output = tmp_path / "out"
    assert cli.main(args(source, config, output)) == 2
    assert not output.exists()


@pytest.mark.parametrize("name", ["contracts.json", "metadata.json", "availability.json"])
def test_hash_valid_duplicate_json_keys_in_source_are_rejected(cli, inputs, tmp_path, name):
    source, config = inputs
    path = source / name
    if name == "contracts.json":
        payload = path.read_bytes().replace(b'"raw_symbol":"SHFE.rb2405"',
                                          b'"raw_symbol":"SHFE.rb2410","raw_symbol":"SHFE.rb2405"')
    else:
        payload = b'{"identity":"other","identity":"last"}\n'
    path.write_bytes(payload)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["content_hashes"][name] = sha256(payload).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "out"
    assert cli.main(args(source, config, output)) == 2
    assert not output.exists()


@pytest.mark.parametrize("problem", ["two_instances", "duplicate_instance", "non_object"])
def test_ambiguous_or_malformed_contract_declarations_reject(cli, inputs, tmp_path, problem):
    source, config = inputs
    path = source / "contracts.json"
    declarations = json.loads(path.read_bytes())
    if problem == "two_instances":
        row = dict(declarations[0], expiry_at="2034-05-15T15:00:00+08:00",
                   instance_id=f"{SYMBOL}|2034-05-15T15:00:00+08:00")
    elif problem == "duplicate_instance":
        row = dict(declarations[0], raw_symbol="SHFE.rb2410")
    else:
        row = "not-an-object"
    payload = json.dumps(declarations + [row]).encode("utf-8")
    path.write_bytes(payload)
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["content_hashes"]["contracts.json"] = sha256(payload).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    output = tmp_path / "out"
    assert cli.main(args(source, config, output)) == 2
    assert not output.exists()


def test_failure_writes_incomplete_manifest_and_success_marker_is_last(cli, inputs, tmp_path, monkeypatch):
    source, config = inputs
    output = tmp_path / "failed"
    original = Path.open
    writes = []

    def fail_equity(path, mode="r", *a, **kw):
        if output in path.parents and any(character in mode for character in "wax"):
            writes.append(path.name)
            if path.name == "equity.csv":
                raise OSError("injected disk failure")
        return original(path, mode, *a, **kw)

    monkeypatch.setattr(Path, "open", fail_equity)
    assert cli.main(args(source, config, output)) == 2
    manifest = json.loads((output / "manifest.json").read_bytes())
    assert manifest["status"] == "incomplete"
    assert manifest["reason"] == "artifact_publication_failed"
    assert writes[-1] == "manifest.json"
    assert set(manifest["content_hashes"]) == {"config.json", "fills.csv"}
    # Verify order on success too, independently of failure behavior.
    output = tmp_path / "success"
    writes.clear()

    def record_open(path, mode="r", *a, **kw):
        if output in path.parents and any(character in mode for character in "wax"):
            writes.append(path.name)
        return original(path, mode, *a, **kw)

    monkeypatch.setattr(Path, "open", record_open)
    assert cli.main(args(source, config, output)) == 0
    assert writes[-1] == "manifest.json"
    assert len(writes) == 6


def test_competing_manifest_is_preserved_when_complete_marker_creation_collides(cli, inputs, tmp_path, monkeypatch):
    source, config = inputs
    output = tmp_path / "collision"
    marker = output / "manifest.json"
    original = Path.open
    injected = False

    def collide(path, mode="r", *a, **kw):
        nonlocal injected
        if path == marker and mode == "xb" and not injected:
            injected = True
            with original(marker, "xb") as handle:
                handle.write(b"sentinel")
        return original(path, mode, *a, **kw)

    monkeypatch.setattr(Path, "open", collide)
    assert cli.main(args(source, config, output)) == 2
    assert marker.read_bytes() == b"sentinel"


def test_partial_complete_marker_is_repaired_on_its_original_handle(cli, inputs, tmp_path, monkeypatch):
    source, config = inputs
    output = tmp_path / "partial-marker"
    marker = output / "manifest.json"
    original = Path.open
    wrapped = False

    class FailOnce:
        def __init__(self, handle):
            self.handle = handle
            self.failed = False

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return self.handle.__exit__(*a)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def write(self, payload):
            if not self.failed:
                self.failed = True
                self.handle.write(payload[:50])
                raise OSError("injected partial marker write")
            return self.handle.write(payload)

    def partial_marker(path, mode="r", *a, **kw):
        nonlocal wrapped
        handle = original(path, mode, *a, **kw)
        if path == marker and mode == "xb" and not wrapped:
            wrapped = True
            return FailOnce(handle)
        return handle

    monkeypatch.setattr(Path, "open", partial_marker)
    assert cli.main(args(source, config, output)) == 2
    document = json.loads(marker.read_bytes())
    assert document["status"] == "incomplete"
    assert set(document["content_hashes"]) == OUTPUTS - {"manifest.json"}


def test_no_signal_cli_writes_empty_fills_with_column_header(cli, inputs, tmp_path):
    source, config = inputs
    output = tmp_path / "no-signals"
    # Monotonic prices put z-score outside the range-regime entry gate.
    assert cli.main(args(source, config, output, strategy="zscore_reversion")) == 0
    assert read_csv(output / "fills.csv") == []
    assert (output / "fills.csv").read_text().startswith("fill_id,")
    report = json.loads((output / "report.json").read_bytes())
    assert report["net_pnl"] == "0" and report["fills"] == 0


def test_numeric_decimal_literals_preserve_exact_config_values(cli, inputs, tmp_path):
    source, config = inputs
    config.write_text(config.read_text().replace('"100000"', "100000.000000000001"))
    output = tmp_path / "exact-decimal"
    assert cli.main(args(source, config, output)) == 0
    parsed = json.loads((output / "config.json").read_bytes())
    assert parsed["assumptions"]["initial_cash"] == "100000.000000000001"


def test_existing_dangling_output_symlink_is_rejected(cli, inputs, tmp_path):
    source, config = inputs
    output = tmp_path / "existing-link"
    target = tmp_path / "does-not-exist"
    try:
        output.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    assert cli.main(args(source, config, output)) == 2
    assert output.is_symlink() and not target.exists()


def test_alias_into_input_is_rejected_without_writing(cli, inputs, tmp_path):
    source, config = inputs
    alias = tmp_path / "input-alias"
    try:
        alias.symlink_to(source, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    before = tree(source)
    assert cli.main(args(source, config, alias / "child")) == 2
    assert tree(source) == before


def test_manifest_change_during_reader_is_rejected(cli, inputs, tmp_path, monkeypatch):
    source, config = inputs
    manifest_path = source / "manifest.json"
    original = Path.read_bytes
    calls = 0

    def changing_read(path):
        nonlocal calls
        payload = original(path)
        if path == manifest_path:
            calls += 1
            if calls == 2:
                document = json.loads(payload)
                document["created_at"] = "changed-after-reader-captured-bytes"
                manifest_path.write_text(json.dumps(document), encoding="utf-8")
        return payload

    monkeypatch.setattr(Path, "read_bytes", changing_read)
    output = tmp_path / "out"
    assert cli.main(args(source, config, output)) == 2
    assert not output.exists()


def _install_offline_guards():
    import builtins
    import importlib
    import io
    import os
    import socket

    forbidden = ("dotenv", "tqsdk", "sqlalchemy", "psycopg", "psycopg2", "requests", "httpx",
                 "ak_trading.persistence", "ak_trading.config", "ak_trading.runtime")
    original_import = builtins.__import__
    original_module = importlib.import_module

    def check(name):
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden):
            raise AssertionError(f"offline boundary import attempted: {name}")

    def import_guard(name, *a, **kw):
        check(name)
        return original_import(name, *a, **kw)

    def module_guard(name, package=None):
        check(importlib.util.resolve_name(name, package) if name.startswith(".") else name)
        return original_module(name, package)

    def guard_open(original):
        def guarded(file, *a, **kw):
            if isinstance(file, (str, bytes, os.PathLike)):
                name = os.fsdecode(file).replace("\\", "/").rsplit("/", 1)[-1].lower()
                if name == "nul" or name == ".env" or name.startswith(".env."):
                    raise AssertionError(f"offline boundary file attempted: {name}")
            return original(file, *a, **kw)
        return guarded

    def denied(*a, **kw):
        raise AssertionError("network attempted")

    builtins.__import__ = import_guard
    importlib.import_module = module_guard
    builtins.open = guard_open(builtins.open)
    io.open = guard_open(io.open)
    os.open = guard_open(os.open)
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.create_connection = denied
    socket.getaddrinfo = denied


def test_fresh_process_cli_is_offline_and_byte_identical(cli, inputs, tmp_path):
    source, config = inputs
    first, second = tmp_path / "first", tmp_path / "second"
    assert cli.main(args(source, config, first)) == 0
    bootstrap = inspect.getsource(_install_offline_guards) + """
import runpy
import sys
_install_offline_guards()
script = sys.argv.pop(1)
runpy.run_path(script, run_name="__main__")
"""
    environment = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR") if name in os.environ}
    environment.update(PYTHONHASHSEED="728", PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1",
                       AK_DATABASE_URL="", TEMP=str(tmp_path), TMP=str(tmp_path))
    process = subprocess.run([sys.executable, "-B", "-c", bootstrap, str(SCRIPT), *args(source, config, second)],
                             cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=60)
    assert process.returncode == 0, process.stdout + process.stderr
    assert tree(first) == tree(second)


def test_repository_frozen_real_bundle_works_with_all_three_fixed_evaluators():
    from ak_trading.futures.io import read_input_bundle
    from ak_trading.futures.scenario_backtest import ScenarioConfig, run_scenario_backtest
    source = ROOT / "data/strategy_artifacts/futures-lib-20260907-codex-01/real-input-02"
    if not source.is_dir():
        pytest.skip("reviewed local frozen export is not installed")
    assert sha256((source / "manifest.json").read_bytes()).hexdigest() == "99366eb8310222957d2dc896df1e6781982b9995a91c352f106bfbbfd77493a3"
    bundle = read_input_bundle(source)
    assert len(bundle.bars) == 92461 and len(bundle.contracts) == 439
    config = ScenarioConfig.from_mapping(assumptions(initial_cash="1000000", lots=1, price_tick="1"))
    for strategy in ("donchian_trend", "ma_trend", "zscore_reversion"):
        result = run_scenario_backtest(bundle, symbol="SHFE.rb2101", strategy=strategy, config=config)
        assert result.report["status"] == "complete"
        assert result.report["historical_strict_execution"] is False
        assert len(result.decisions) == sum(bar.symbol == "SHFE.rb2101" for bar in bundle.bars)
        assert all(result.report["reconciliation"].values())
        if strategy == "ma_trend":
            assert result.fills
