"""S3 CLI tests: argument contract, output isolation, demo replay byte
identity, frozen real-input signals semantics, offline guarantees.

Finalized-plan §十二.18 (minimum 4 cases).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "research_futures.py"
import os

# A10: the frozen real bundle path is parameterized so artifact moves
# fail loudly in CI instead of silently reducing coverage.
REAL_BUNDLE = Path(os.environ.get(
    "FUTURES_REAL_INPUT_BUNDLE",
    "data/strategy_artifacts/futures-lib-20260907-codex-01/real-input-02",
))


def load_cli():
    spec = importlib.util.spec_from_file_location("research_futures_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_argument_contract_exit_codes(tmp_path: Path) -> None:
    cli = load_cli()
    # Exactly one of --demo / --input-bundle is required.
    assert cli.main(["--mode", "backtest", "--output", str(tmp_path / "a")]) == 2
    assert cli.main(["--demo", "--input-bundle", "x",
                     "--mode", "backtest", "--output", str(tmp_path / "b")]) == 2
    # --validation only applies to backtest.
    assert cli.main(["--input-bundle", "x", "--mode", "signals",
                     "--validation", "smoke", "--output", str(tmp_path / "c")]) == 2
    # Output directory must not exist.
    existing = tmp_path / "exists"
    existing.mkdir()
    assert cli.main(["--demo", "--mode", "backtest", "--output", str(existing)]) == 2
    # Demo only supports backtest; signals require a real_export bundle.
    assert cli.main(["--demo", "--mode", "signals", "--output", str(tmp_path / "d")]) == 2
    assert cli.main(["--input-bundle", "x", "--mode", "backtest",
                     "--output", str(tmp_path / "e")]) == 3  # bundle missing
    # Unsupported probing options are rejected, never silently ignored.
    assert cli.main(["--demo", "--mode", "backtest", "--include-member-ranks",
                     "--output", str(tmp_path / "f")]) == 2
    unsupported = tmp_path / "not-implemented"
    with pytest.raises(SystemExit) as rejected:
        cli.main(["--demo", "--mode", "backtest", "--validation", "unknown-mode",
                  "--output", str(unsupported)])
    assert rejected.value.code == 2
    assert not unsupported.exists()


def test_demo_backtest_publishes_full_bundle_and_replay_is_byte_identical(
    tmp_path: Path,
) -> None:
    cli = load_cli()
    first = tmp_path / "demo-01"
    assert cli.main(["--demo", "--mode", "backtest", "--validation", "smoke",
                     "--output", str(first)]) == 0
    for name in (
        "config.json", "manifest.json", "availability.json", "splits.json",
        "report.json", "signals.csv", "rolls.csv",
        "scenarios/base/futures_fixed_risk/fills.csv",
        "scenarios/base/futures_fixed_risk/equity.csv",
        "scenarios/base/futures_fixed_risk/trades.csv",
        "scenarios/stress/futures_fixed_risk/fills.csv",
        "scenarios/base/cash_baseline/equity.csv",
        "scenarios/base/family_standalone_trend/fills.csv",
        "legacy_reference/report.json", "input/manifest.json", "input/bars.csv",
    ):
        assert (first / name).exists(), name
    manifest = json.loads((first / "manifest.json").read_bytes().decode("utf-8"))
    assert manifest["dataset_kind"] == "backtest" and manifest["status"] == "complete"
    assert manifest["accounting"] == "ideal_price_cny_ledger"
    legacy = json.loads((first / "legacy_reference/report.json").read_bytes().decode("utf-8"))
    assert legacy["comparable_to_futures"] is False

    # Replay from the frozen input + config must be byte-identical (§十三.4).
    second = tmp_path / "demo-replay-01"
    assert cli.main([
        "--input-bundle", str(first / "input"),
        "--config", str(first / "config.json"),
        "--mode", "backtest", "--validation", "smoke",
        "--output", str(second),
    ]) == 0
    files_first = sorted(p.relative_to(first) for p in first.rglob("*") if p.is_file())
    files_second = sorted(p.relative_to(second) for p in second.rglob("*") if p.is_file())
    # The replay reproduces every payload of the source run except the
    # nested input/ directory it consumed (which is identical by source).
    differing = [
        rel for rel in files_first
        if rel in files_second
        and (first / rel).read_bytes() != (second / rel).read_bytes()
    ]
    assert differing == [], f"non-identical payloads: {differing[:5]}"
    for rel in files_second:
        if str(rel).startswith("input" + "/"):
            assert (first / rel).read_bytes() == (second / rel).read_bytes()


def test_real_bundle_signals_observation_only_with_null_targets(tmp_path: Path) -> None:
    if not REAL_BUNDLE.exists():
        pytest.skip("frozen real-input-02 bundle not present")
    cli = load_cli()
    out = tmp_path / "real-signals"
    assert cli.main(["--input-bundle", str(REAL_BUNDLE), "--mode", "signals",
                     "--output", str(out)]) == 0
    report = json.loads((out / "report.json").read_bytes().decode("utf-8"))
    assert report["D_status"] == "unavailable"
    assert report["performance_status"] == "unavailable_no_real_strict_backtest"
    assert report["net_pnl"] is None and report["sharpe"] is None
    manifest = json.loads((out / "manifest.json").read_bytes().decode("utf-8"))
    assert manifest["account_artifacts"] == "not_requested"
    import csv

    with (out / "signals.csv").open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows, "six slots across four strategies must produce diagnostic rows"
    assert all(row["target_position"] == "" for row in rows)  # null, never zero
    assert all("non_actionable" in row["diagnostics"] for row in rows)
    assert {row["product"] for row in rows} >= {"SHFE.RB", "CFFEX.IF"}
    # B5: ALL FOUR implementations appear, including term_structure_carry.
    strategy_ids = {row["strategy_id"] for row in rows}
    assert strategy_ids == {
        "donchian_trend", "ma_trend", "zscore_reversion", "term_structure_carry",
    }
    # B5: the §十三.2 per-strategy/per-field availability matrix with carry
    # pairing diagnostics is published in report and availability payloads.
    matrix = report["availability_matrix"]
    assert set(matrix) == {"per_strategy", "per_field", "carry_pairing"}
    for product in matrix["per_strategy"]:
        assert set(matrix["per_strategy"][product]) == strategy_ids
    for product, pairing in matrix["carry_pairing"].items():
        assert "pairs_with_sync_in_gap_range" in pairing
    assert report["strategies_evaluated"] == sorted(strategy_ids)


def test_empty_real_input_does_not_fake_pass(tmp_path: Path) -> None:
    from ak_trading.futures.io import publish_input_bundle

    empty = tmp_path / "empty-real"
    publish_input_bundle(
        empty,
        dataset_kind="real_export",
        created_at="2026-09-07T00:00:00+08:00",
        bars_rows=[],
        contracts=[],
        metadata={},
        availability={},
        request_info={"products": []},
        source_hashes={},
        read_only_transaction={"executed": True, "verified": True},
        clock_policy={},
    )
    cli = load_cli()
    out = tmp_path / "signals-out"
    code = cli.main(["--input-bundle", str(empty), "--mode", "signals",
                     "--output", str(out)])
    assert code == 3  # blocked: no readable real bars, never a fake PASS
    assert not out.exists()


def test_cli_module_has_no_database_network_or_env_imports() -> None:
    source = SCRIPT.read_bytes().decode("utf-8")
    import re

    import_lines = re.findall(r"^(?:from|import)\s+.*$", source, re.M)
    joined = "\n".join(import_lines)
    for forbidden in (
        "tqsdk", "psycopg", "sqlalchemy", "dotenv", "requests", "urllib",
        "ak_trading.persistence",
    ):
        assert forbidden not in joined, forbidden
    import subprocess

    code = (
        "import importlib.util, json, sys;"
        "spec = importlib.util.spec_from_file_location('rf', r'%s');"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
        "banned = [name for name in ('tqsdk', 'psycopg', 'dotenv', 'requests')"
        " if any(mod == name or mod.startswith(name + '.') for mod in sys.modules)];"
        "print(json.dumps(banned))" % str(SCRIPT)
    )
    result = subprocess.run([sys.executable, "-B", "-c", code],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == []


# ---------------------------------------------------------------------------
# Fix-round additions: B1/B2/B3/B6/B7 assertions, A9 subprocess determinism
# ---------------------------------------------------------------------------


def test_demo_bundle_b1_b2_b3_b6_content(tmp_path: Path) -> None:
    cli = load_cli()
    out = tmp_path / "demo-fix"
    assert cli.main(["--demo", "--mode", "backtest", "--validation", "smoke",
                     "--output", str(out)]) == 0
    report = json.loads((out / "report.json").read_bytes().decode("utf-8"))
    # B1: the mandatory long_only_rolled_contract baseline is published.
    assert "long_only_rolled_contract" in report["portfolios"]
    assert (out / "scenarios/base/long_only_rolled_contract/fills.csv").exists()
    assert (out / "scenarios/stress/long_only_rolled_contract/fills.csv").exists()
    long_report = json.loads(
        (out / "scenarios/base/long_only_rolled_contract/report.json").read_bytes().decode("utf-8")
    )
    assert long_report["invested_days"] > 0
    # B2: ALL THREE families transact — per-family fill counts are real.
    carry_fills = (out / "scenarios/base/family_standalone_carry/fills.csv").read_text(
        encoding="utf-8"
    ).strip().splitlines()
    assert len(carry_fills) > 1, "carry family must produce non-zero fills"
    mr_fills = (out / "scenarios/base/family_standalone_mean_reversion/fills.csv").read_text(
        encoding="utf-8"
    ).strip().splitlines()
    trend_fills = (out / "scenarios/base/family_standalone_trend/fills.csv").read_text(
        encoding="utf-8"
    ).strip().splitlines()
    assert len(mr_fills) > 1 and len(trend_fills) > 1
    # B3: the fixed-risk run with terminal residue is incomplete with null
    # annualized turnover and reported unrealized PnL.
    base = report["futures_fixed_risk_base"]
    assert base["open_positions"], "fixture must exercise terminal residue"
    assert base["status"] == "incomplete"
    assert base["annualized_turnover"] is None
    assert base["final_unrealized_pnl"] not in (None, "0")
    assert base["total_gross_pnl"] is not None
    assert base["roll_costs_cny"] is not None
    assert base["trades"]["open_trade_count"] > 0
    assert base["drawdown_duration_d_days"] is not None
    cash = json.loads((out / "scenarios/base/cash_baseline/report.json").read_bytes())
    assert set(cash) >= set(base) - {"reconciliation", "open_positions", "incomplete_reasons"}
    assert cash["trades"]["open_trade_count"] == 0
    assert cash["sharpe"] is None and cash["sharpe_reason"] == "zero_variance"
    # B4: cash and equity are separately accumulated (published columns).
    import csv as _csv

    with (out / "scenarios/base/futures_fixed_risk/equity.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        equity_rows = list(_csv.DictReader(handle))
    separated = [r for r in equity_rows if r["cash"] != r["equity"]]
    assert separated, "cash must differ from equity while positions are open"
    # B6: the top-level signals.csv is the causal request trace and links to
    # fills through shared decision ids.
    with (out / "signals.csv").open(newline="", encoding="utf-8") as handle:
        signal_rows = list(_csv.DictReader(handle))
    assert signal_rows, "top-level signals.csv must not be empty (B6)"
    assert {r["scenario"] for r in signal_rows} == {"base", "stress"}
    assert all(r["clock_status"] != "stress" for r in signal_rows)
    statuses = {r["status"] for r in signal_rows}
    assert statuses & {"admitted", "active", "netted_flat", "below_minimum_lot",
                       "slot_unallocated", "risk_refusal", "warmup_before_evaluate"}
    with (out / "scenarios/base/futures_fixed_risk/fills.csv").open(
        newline="", encoding="utf-8"
    ) as handle:
        fill_rows = list(_csv.DictReader(handle))
    fill_decisions = {r["decision_id"] for r in fill_rows}
    diagnostics_decisions = set()
    for row in signal_rows:
        try:
            payload = json.loads(row["diagnostics"])
            if payload.get("decision_id"):
                diagnostics_decisions.add(payload["decision_id"])
        except json.JSONDecodeError:
            continue
    assert fill_decisions & diagnostics_decisions, (
        "fills must be traceable to signal-trace decision ids (B6)"
    )


def test_frozen_bundle_replay_is_input_driven_b7(tmp_path: Path) -> None:
    """The replay reads the bundle's bars: removing a contract breaks it."""
    cli = load_cli()
    demo = tmp_path / "demo-src"
    assert cli.main(["--demo", "--mode", "backtest", "--validation", "smoke",
                     "--output", str(demo)]) == 0
    replay = tmp_path / "demo-replay"
    assert cli.main([
        "--input-bundle", str(demo / "input"),
        "--config", str(demo / "config.json"),
        "--mode", "backtest", "--validation", "smoke",
        "--output", str(replay),
    ]) == 0
    files_a = sorted(p.relative_to(demo) for p in demo.rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(replay) for p in replay.rglob("*") if p.is_file())
    differing = [
        rel for rel in files_a
        if rel in files_b and (demo / rel).read_bytes() != (replay / rel).read_bytes()
    ]
    assert differing == [], f"input-driven replay not byte-identical: {differing[:5]}"
    # Input-driven proof: strip a contract's bars from the bundle copy; the
    # replay must fail loudly on the missing input rather than regenerate
    # from the fixture.
    tampered = tmp_path / "tampered-input"
    tampered.mkdir()
    for item in (demo / "input").iterdir():
        if item.name == "bars.csv":
            lines = item.read_text(encoding="utf-8").splitlines(keepends=True)
            kept = [lines[0]] + [
                line for line in lines[1:] if not line.startswith("SHFE.demoRB3")
            ]
            (tampered / "bars.csv").write_text("".join(kept), encoding="utf-8")
        else:
            (tampered / item.name).write_bytes(item.read_bytes())
    out_tampered = tmp_path / "replay-tampered"
    code = cli.main([
        "--input-bundle", str(tampered),
        "--config", str(demo / "config.json"),
        "--mode", "backtest", "--validation", "smoke",
        "--output", str(out_tampered),
    ])
    assert code == 2  # config declares demoRB3 rules; bundle lost its bars
    # Unsupported config keys are rejected instead of ignored.
    bad_config = tmp_path / "bad-config.json"
    bad_config.write_text(json.dumps({"surprise_key": 1}), encoding="utf-8")
    out_bad = tmp_path / "replay-bad-config"
    assert cli.main([
        "--input-bundle", str(demo / "input"),
        "--config", str(bad_config),
        "--mode", "backtest", "--validation", "smoke",
        "--output", str(out_bad),
    ]) == 2
    # A frozen-bundle replay without a config is rejected (rules needed).
    out_noconf = tmp_path / "replay-noconf"
    assert cli.main([
        "--input-bundle", str(demo / "input"),
        "--mode", "backtest", "--output", str(out_noconf),
    ]) == 2


def test_subprocess_replay_determinism_a9(tmp_path: Path) -> None:
    """A9: regenerate the bundle from persisted inputs in a FRESH process."""
    import subprocess
    import sys as _sys

    cli = load_cli()
    demo = tmp_path / "demo-proc"
    assert cli.main(["--demo", "--mode", "backtest", "--validation", "smoke",
                     "--output", str(demo)]) == 0
    fresh = tmp_path / "demo-proc-replay"
    result = subprocess.run(
        [_sys.executable, "-B", str(SCRIPT),
         "--input-bundle", str(demo / "input"),
         "--config", str(demo / "config.json"),
         "--mode", "backtest", "--validation", "smoke",
         "--output", str(fresh)],
        capture_output=True, text=True, timeout=900,
    )
    assert result.returncode == 0, result.stdout[-400:] + result.stderr[-400:]
    files_a = sorted(p.relative_to(demo) for p in demo.rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(fresh) for p in fresh.rglob("*") if p.is_file())
    differing = [
        rel for rel in files_a
        if rel in files_b and (demo / rel).read_bytes() != (fresh / rel).read_bytes()
    ]
    assert differing == []
