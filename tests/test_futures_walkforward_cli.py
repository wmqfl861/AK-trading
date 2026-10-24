"""CLI integration contracts for independent walk-forward windows and readiness.

The composition functions are the agreed causal seams. Only one test runs a
full validation schedule (441 dates, one development fold); the other replays
use 30 warmup observations and five scored dates.
"""
from __future__ import annotations

import builtins
import csv
import importlib.util
import inspect
import io
import json
import os
import socket
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "research_futures.py"
REAL_BUNDLE = Path(os.environ.get(
    "FUTURES_REAL_INPUT_BUNDLE",
    str(ROOT / "data/strategy_artifacts/futures-lib-20260907-codex-01/real-input-02"),
))
if not REAL_BUNDLE.is_absolute():
    REAL_BUNDLE = ROOT / REAL_BUNDLE


def load_cli():
    spec = importlib.util.spec_from_file_location("futures_walkforward_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    """Fail before opening credentials, importing DB clients, or networking."""
    attempts = []
    original_import = builtins.__import__
    forbidden = ("dotenv", "psycopg", "psycopg2", "sqlalchemy", "tqsdk",
                 "ak_trading.persistence")

    def guarded_import(name, *args, **kwargs):
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden):
            attempts.append(name)
            raise AssertionError(f"offline CLI imported {name}")
        return original_import(name, *args, **kwargs)

    def guard_open(original):
        def guarded(file, *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)):
                basename = os.fsdecode(file).replace("\\", "/").rsplit("/", 1)[-1].lower()
                if basename in {".env", "nul"}:
                    attempts.append(basename)
                    raise AssertionError(f"offline CLI attempted to open {basename}")
            return original(file, *args, **kwargs)
        return guarded

    def no_network(*args, **kwargs):
        attempts.append("network")
        raise AssertionError("offline CLI attempted network access")

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(builtins, "open", guard_open(builtins.open))
    monkeypatch.setattr(io, "open", guard_open(io.open))
    monkeypatch.setattr(os, "open", guard_open(os.open))
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    yield
    assert attempts == [], f"offline boundary attempts: {attempts}"


@pytest.fixture
def cli():
    return load_cli()


@pytest.fixture(scope="module")
def demo_source():
    # Short fixture construction used to fail on empty demoRB3 history. Build
    # once, then bound only the evaluation dates; no 630-day replay is needed.
    return load_cli()._inputs_from_fixture(630)


@pytest.fixture
def short_inputs(demo_source):
    inputs = deepcopy(demo_source[0])
    # Both RB near/far histories exist here; carry can trade within 30 days.
    inputs.dates = inputs.dates[390:425]
    return inputs


@pytest.fixture
def policy():
    from ak_trading.futures.spec import ResearchPolicy

    return ResearchPolicy(initial_nav_cny=Decimal("400000000"))


def compose(cli, kind, inputs, policy, *, scored_days, warmup_days=(), registry=None):
    if kind == "long_only":
        return cli._compose_long_only(
            inputs, policy, "base", scored_days=scored_days, warmup_days=warmup_days,
        )
    if registry is None:
        from ak_trading.futures.strategies import default_registry

        registry = default_registry(policy)
    return cli._compose_fixed_risk(
        inputs, policy, registry, "base",
        scored_days=scored_days, warmup_days=warmup_days,
    )


def read_json(path):
    return json.loads(path.read_bytes().decode("utf-8"))


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@pytest.mark.parametrize("kind", ["fixed_risk", "long_only"])
def test_warmup_decisions_fill_only_on_scored_dates(cli, short_inputs, policy, kind):
    function = cli._compose_long_only if kind == "long_only" else cli._compose_fixed_risk
    parameters = inspect.signature(function).parameters
    for name, default in (("scored_days", None), ("warmup_days", ())):
        assert name in parameters, f"forthcoming composition API missing {name}"
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters[name].default == default

    warmup = tuple(short_inputs.dates[:30])
    scored = tuple(short_inputs.dates[30:])
    engine, outcome, signals = compose(
        cli, kind, short_inputs, policy, scored_days=scored, warmup_days=warmup,
    )
    assert engine.initial_cash == policy.initial_nav_cny
    assert tuple(mark.trading_day for mark in outcome.marks) == scored
    assert outcome.fills, "the window must exercise real execution"
    assert {fill.trading_day for fill in outcome.fills} <= set(scored)
    assert outcome.reconciliation["cash_matches_fill_identity"] is True
    assert outcome.reconciliation["equity_matches_gross_identity"] is True

    first_opens = [f for f in outcome.fills if f.trading_day == scored[0] and f.offset == "open"]
    assert first_opens, "the final warmup decision must seed the first scored open"
    bars_by_id = {
        f"{iid}:{bar.datetime_nano}": bar
        for iid, series in short_inputs.bars.items() for bar in series.values()
    }
    for fill in outcome.fills:
        if fill.used_bar_id is not None:
            used = bars_by_id[fill.used_bar_id]
            assert used.available_at < fill.event_time
            assert used.trading_day < fill.trading_day
    for fill in first_opens:
        assert bars_by_id[fill.used_bar_id].trading_day == warmup[-1]
    assert any(row["decision_at"] == warmup[-1].isoformat() for row in signals)
    # Every earlier hypothetical warmup fill was discarded: IDs and cash
    # reconciliation start with this account's scored executions only.
    assert outcome.fills[0].fill_id == "F000001"
    assert Decimal(cli._summarize_outcome(outcome, policy)["initial_nav"]) == policy.initial_nav_cny


@pytest.mark.parametrize("kind", ["fixed_risk", "long_only"])
def test_unwarmed_window_starts_at_initial_capital(cli, short_inputs, policy, kind):
    scored = tuple(short_inputs.dates[30:])
    _, outcome, _ = compose(cli, kind, short_inputs, policy, scored_days=scored)
    first = outcome.marks[0]
    assert first.trading_day == scored[0]
    assert first.cash == first.equity == policy.initial_nav_cny
    assert first.unrealized_pnl == first.required_margin == 0
    assert first.invested is False
    assert all(fill.trading_day > scored[0] for fill in outcome.fills)


@pytest.mark.parametrize("kind", ["fixed_risk", "long_only"])
def test_future_price_mutation_preserves_prior_scored_fills(cli, short_inputs, policy, kind):
    warmup = tuple(short_inputs.dates[:30])
    scored = tuple(short_inputs.dates[30:])
    prefix_end = scored[1]
    _, original, original_signals = compose(
        cli, kind, short_inputs, policy, scored_days=scored, warmup_days=warmup,
    )
    changed = deepcopy(short_inputs)
    for iid, series in changed.bars.items():
        for day, bar in list(series.items()):
            if day > prefix_end:
                series[day] = replace(
                    bar, open=bar.open * Decimal("1.005"), high=bar.high * Decimal("1.005"),
                    low=bar.low * Decimal("1.005"), close=bar.close * Decimal("1.005"),
                )
    _, mutated, mutated_signals = compose(
        cli, kind, changed, policy, scored_days=scored, warmup_days=warmup,
    )
    prefix_fills = tuple(f for f in original.fills if f.trading_day <= prefix_end)
    assert prefix_fills
    assert prefix_fills == tuple(f for f in mutated.fills if f.trading_day <= prefix_end)
    assert tuple(m for m in original.marks if m.trading_day <= prefix_end) == tuple(
        m for m in mutated.marks if m.trading_day <= prefix_end
    )
    assert [r for r in original_signals if r["decision_at"] <= prefix_end.isoformat()] == [
        r for r in mutated_signals if r["decision_at"] <= prefix_end.isoformat()
    ]
    assert original.marks[-1].equity != mutated.marks[-1].equity, (
        "the changed future prices must affect this real replay after the prefix"
    )


@pytest.mark.parametrize("kind", ["fixed_risk", "long_only"])
def test_train_and_oos_replays_keep_independent_accounts(cli, short_inputs, policy, kind):
    warmup = tuple(short_inputs.dates[:30])
    scored = tuple(short_inputs.dates[30:])
    train_engine, train, _ = compose(
        cli, kind, short_inputs, policy, scored_days=warmup,
    )
    assert train.fills and train.marks[-1].equity != policy.initial_nav_cny
    train_snapshot = deepcopy((train_engine.fills, train_engine.marks, train_engine.ledgers))
    oos_engine, oos, rows = compose(
        cli, kind, short_inputs, policy, scored_days=scored, warmup_days=warmup,
    )
    fresh_engine, fresh, fresh_rows = compose(
        load_cli(), kind, deepcopy(short_inputs), policy,
        scored_days=scored, warmup_days=warmup,
    )
    assert train_engine is not oos_engine and oos_engine is not fresh_engine
    assert all(train_engine.ledgers[iid] is not oos_engine.ledgers[iid] for iid in train_engine.ledgers)
    assert oos_engine.initial_cash == fresh_engine.initial_cash == policy.initial_nav_cny
    assert oos == fresh and rows == fresh_rows
    assert train_snapshot == (train_engine.fills, train_engine.marks, train_engine.ledgers)
    assert {f.trading_day for f in train.fills}.isdisjoint(f.trading_day for f in oos.fills)
    assert oos.fills[0].fill_id == "F000001"


def test_warmup_does_not_carry_hypothetical_zscore_position(cli, short_inputs, policy):
    from ak_trading.futures.strategies import ZScoreReversion

    symbol = "CFFEX.demoIF1"
    short_inputs.universe_products = (short_inputs.products[symbol],)
    for index, day in enumerate(short_inputs.dates):
        # Alternation has zero ER; the excursion enters short at day 26.
        # 4015 remains inside the holding band but outside the entry band.
        value = "4025" if index == 26 else "4015" if index >= 27 else "4010" if index % 2 else "3990"
        price = Decimal(value)
        short_inputs.bars[symbol][day] = replace(
            short_inputs.bars[symbol][day], open=price, close=price,
            high=price + 1, low=price - 1,
        )
    warmup = tuple(short_inputs.dates[:30])
    scored = tuple(short_inputs.dates[30:])
    registry = {"zscore_reversion": ZScoreReversion()}
    _, history, history_rows = compose(
        cli, "fixed_risk", short_inputs, policy, scored_days=warmup, registry=registry,
    )
    assert any(fill.side == -1 and fill.offset == "open" for fill in history.fills)
    assert any(
        row["strategy_id"] == "zscore_reversion"
        and row["decision_at"] == warmup[-1].isoformat()
        and row["target_position"] == "-1"
        for row in history_rows
    ), "fixture must exercise a direction that survives only with prior held state"
    _, oos, rows = compose(
        cli, "fixed_risk", short_inputs, policy,
        scored_days=scored, warmup_days=warmup, registry=registry,
    )
    decisions = [row for row in rows if row["strategy_id"] == "zscore_reversion"
                 and row["family"] == "mean_reversion"]
    assert decisions and all(row["target_position"] == "0" for row in decisions)
    assert oos.fills == ()
    assert all(mark.cash == mark.equity == policy.initial_nav_cny for mark in oos.marks)


@pytest.mark.parametrize("kind", ["fixed_risk", "long_only"])
def test_empty_scoring_and_overlapping_warmup_are_rejected(cli, short_inputs, policy, kind):
    with pytest.raises(ValueError, match="scored.*empty|empty.*scored"):
        compose(cli, kind, short_inputs, policy, scored_days=())
    with pytest.raises(ValueError, match="warmup.*precede|warmup.*before|overlap"):
        compose(
            cli, kind, short_inputs, policy,
            scored_days=tuple(short_inputs.dates[30:]),
            warmup_days=tuple(short_inputs.dates[:31]),
        )


def test_cli_walk_forward_publishes_independent_windows_and_oos_unit_index(
    cli, demo_source, monkeypatch, tmp_path, capsys,
):
    from ak_trading.futures.strategies import default_registry

    inputs, config = deepcopy(demo_source)
    inputs.dates = inputs.dates[:441]
    monkeypatch.setattr(cli, "_inputs_from_fixture", lambda total_days=630: (inputs, config))
    out = tmp_path / "one-fold-walk-forward"
    code = cli.main(["--demo", "--mode", "backtest", "--validation", "walk-forward",
                     "--output", str(out)])
    assert code == 0, capsys.readouterr().out
    for name in ("report.json", "manifest.json", "splits.json", "config.json",
                 "input/manifest.json", "input/bars.csv"):
        assert (out / name).is_file(), name
    report = read_json(out / "report.json")
    splits = read_json(out / "splits.json")
    published_config = read_json(out / "config.json")
    assert report["validation"] == "walk-forward"
    assert report["parameter_selection"] == "fixed_no_optimization"
    assert report["strategy_parameters"] == {
        sid: dict(strategy.spec.parameters) for sid, strategy in default_registry().items()
    }
    assert published_config["policy"] == config["policy"]
    assert splits["n_days"] == 441 and splits["retained_k"] == [252]
    assert len(splits["folds"]) == 1
    assert len(splits["folds"][0]["train"]) == 252
    assert len(splits["folds"][0]["oos"]) == 63
    assert len(splits["final_holdout"]) == 126
    holdout = report["final_holdout"]
    assert holdout["window_id"] == "final_holdout"
    assert holdout["consumed"] is True
    assert holdout["excluded_from_development_aggregate"] is True

    expected_windows = {
        "development/fold-01/train": splits["folds"][0]["train"],
        "development/fold-01/oos": splits["folds"][0]["oos"],
        "final_holdout": splits["final_holdout"],
    }
    windows = {window["window_id"]: window for window in report["windows"]}
    assert windows.keys() == expected_windows.keys()
    required_portfolios = {
        "base": {"futures_fixed_risk", "long_only_rolled_contract", "cash_baseline",
                 "family_standalone_trend", "family_standalone_mean_reversion",
                 "family_standalone_carry"},
        "stress": {"futures_fixed_risk", "long_only_rolled_contract"},
    }
    for window_id, days in expected_windows.items():
        window = windows[window_id]
        assert window["artifact_path"] == window_id
        assert window["scored_days"] == days
        assert Decimal(window["initial_nav"]) == Decimal("400000000")
        assert all(day < days[0] for day in window["warmup_days"])
        for scenario, portfolios in required_portfolios.items():
            assert set(window["portfolios"][scenario]) >= portfolios
            for portfolio in portfolios:
                directory = out / window_id / "scenarios" / scenario / portfolio
                for name in ("fills.csv", "equity.csv", "trades.csv", "report.json"):
                    assert (directory / name).is_file(), str(directory / name)
                equity = read_csv(directory / "equity.csv")
                fills = read_csv(directory / "fills.csv")
                ledger_report = read_json(directory / "report.json")
                assert [row["trading_day"] for row in equity] == days
                assert {row["trading_day"] for row in fills} <= set(days)
                assert ledger_report["days"] == len(days)
                assert Decimal(ledger_report["initial_nav"]) == Decimal("400000000")
                for row in fills:
                    if row["used_bar_id"]:
                        nano = int(row["used_bar_id"].rsplit(":", 1)[1])
                        assert nano < int(datetime.fromisoformat(row["event_time"]).timestamp() * 1_000_000_000)

    aggregates = report["development_oos_aggregate"]
    incomplete_seen = False
    for scenario, portfolios in required_portfolios.items():
        for portfolio in portfolios:
            aggregate = aggregates[scenario][portfolio]
            ledger_dir = out / "development/fold-01/oos/scenarios" / scenario / portfolio
            oos_report = read_json(ledger_dir / "report.json")
            assert aggregate["reset_initial_capital_per_fold"] is True
            assert aggregate["non_continuous_account_path"] is True
            if oos_report["status"] == "complete":
                assert aggregate["status"] == "available"
                path = aggregate["unit_index_path"]
                assert len(path) == 64, "aggregate must contain only 63 development OOS returns"
                assert path[0] == 1.0
                equity = read_csv(ledger_dir / "equity.csv")
                expected = [1.0] + [float(Decimal(row["equity"]) / Decimal("400000000")) for row in equity]
                assert path == pytest.approx(expected)
                assert aggregate["unit_index_final"] == pytest.approx(expected[-1])
            else:
                incomplete_seen = True
                assert aggregate["status"] == "incomplete"
                assert aggregate["unit_index_path"] is None
                assert aggregate["unit_index_final"] is None
                assert aggregate["total_return"] is None
                assert aggregate["reasons"]
                assert {reason["window_id"] for reason in aggregate["reasons"]} == {
                    "development/fold-01/oos"
                }
    assert incomplete_seen, "fixture must keep incomplete ledgers visible in the aggregate"


@pytest.mark.parametrize("n_days", [0, 440])
def test_cli_walk_forward_blocks_insufficient_history(
    cli, demo_source, monkeypatch, tmp_path, capsys, n_days,
):
    inputs, config = deepcopy(demo_source)
    inputs.dates = inputs.dates[:n_days]
    monkeypatch.setattr(cli, "_inputs_from_fixture", lambda total_days=630: (inputs, config))
    out = tmp_path / f"insufficient-{n_days}"
    code = cli.main(["--demo", "--mode", "backtest", "--validation", "walk-forward",
                     "--output", str(out)])
    response = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 3
    assert response["status"] == "blocked"
    assert "insufficient" in json.dumps(response).lower() or "empty" in json.dumps(response).lower()
    assert not list(out.rglob("fills.csv"))
    assert not list(out.rglob("equity.csv"))


def test_readiness_on_frozen_real_bundle_writes_diagnostics_without_accounts(cli, tmp_path, capsys):
    if not REAL_BUNDLE.is_dir():
        pytest.skip("frozen real input bundle not present; set FUTURES_REAL_INPUT_BUNDLE")
    source_hashes = {path.name: sha256(path.read_bytes()).hexdigest()
                     for path in REAL_BUNDLE.iterdir() if path.is_file()}
    out = tmp_path / "real-readiness"
    code = cli.main(["--input-bundle", str(REAL_BUNDLE), "--mode", "readiness",
                     "--output", str(out)])
    assert code == 0, capsys.readouterr().out
    assert (out / "report.json").is_file() and (out / "manifest.json").is_file()
    report = read_json(out / "report.json")
    manifest = read_json(out / "manifest.json")
    assert report["schema_version"] == manifest["schema_version"] == "futures_readiness_v1"
    assert report["basis"] == "frozen_inventory"
    assert report["strict_execution_enabled"] is False
    assert report["execution_status"] == "unavailable"
    assert report["input_references"]["dataset_kind"] == "real_export"
    assert report["counts"]["bar_rows"] > 0
    assert report["account_artifacts"] == manifest["account_artifacts"] == "not_requested"
    assert not list(out.rglob("fills.csv"))
    assert not list(out.rglob("equity.csv"))
    assert not list(out.rglob("trades.csv"))
    assert not (out / "scenarios").exists()
    assert any(term in json.dumps(report).lower() for term in ("blocked", "unknown", "unavailable"))
    assert source_hashes == {path.name: sha256(path.read_bytes()).hexdigest()
                             for path in REAL_BUNDLE.iterdir() if path.is_file()}
