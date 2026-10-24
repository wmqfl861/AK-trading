"""Chronological attribution contracts; every generated price is SYNTHETIC."""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
import hashlib
import importlib.util
import io
import csv
import json
from pathlib import Path

import pytest

from ak_trading.futures.io import (BARS_CSV_COLUMNS, bars_rows_from_bars, canonical_json_bytes,
                                   publish_input_bundle, write_canonical_csv)
from ak_trading.futures.spec import ContractBar

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/experiments/historical_daily_chronological_transfer.py"
FIXTURE = ROOT / "tests/fixtures/experiments/rb_daily_chronological_transfer/base.json"
ARITHMETIC = Context(prec=128, rounding=ROUND_HALF_EVEN)
STRATEGIES = ("donchian_trend", "ma_trend", "zscore_reversion")
BASE = {"initial_cash": "1000000", "lots": 1, "multiplier": "10", "price_tick": "1",
        "fee_per_lot": "0.01", "fee_rate": "0.0001", "slippage_ticks": "1",
        "margin_rate": "0.12", "max_margin_ratio": "0.4"}
SHANGHAI = timezone(timedelta(hours=8))


@pytest.fixture
def cli():
    spec = importlib.util.spec_from_file_location("rb_chronology_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def document(path):
    return json.loads(path.read_bytes())


def ledger(path):
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"), newline="")))


def tree(path):
    return {p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob("*") if p.is_file()}


def bundle(tmp_path, histories=None, *, first=None, reverse=False):
    # The engine consumes real_export schema; the metadata, source and notes
    # explicitly identify all generated prices as SYNTHETIC, never history.
    histories = histories if histories is not None else {
        "SHFE.rb2203": list(range(100, 194)) + [10, 6],
        "SHFE.rb2204": [100] * 60,
        "SHFE.rb2205": [100] * 14,
    }
    first = first or datetime(2021, 11, 1, tzinfo=SHANGHAI)
    declarations, bars = [], []
    for symbol, closes in histories.items():
        expiry = f"2022-{symbol[-2:]}-15T15:00:00+08:00"
        declarations.append({"raw_symbol": symbol, "product": "SHFE.RB", "expiry_at": expiry,
                             "instance_id": f"{symbol}|{expiry}", "identity_status": "SYNTHETIC"})
        for j, raw in enumerate(closes):
            opening, closing = raw if isinstance(raw, tuple) else (raw, raw)
            opening, closing = Decimal(str(opening)), Decimal(str(closing))
            ts = first + timedelta(days=j)
            bars.append(ContractBar(symbol=symbol, duration_seconds=86400,
                                    datetime_nano=int(ts.timestamp()) * 1_000_000_000,
                                    ts=ts, source="SYNTHETIC_chronology_prices",
                                    open=opening, close=closing,
                                    high=max(opening, closing) + Decimal("0.1"),
                                    low=min(opening, closing) - Decimal("0.1"), volume=Decimal("1000")))
    other_products = (("CFFEX.IF2401", "CFFEX.IF"), ("CZCE.TA401", "CZCE.TA"),
                      ("DCE.m2401", "DCE.M"), ("GFEX.si2401", "GFEX.SI"), ("INE.sc2401", "INE.SC"))
    for index, (symbol, product) in enumerate(other_products):
        expiry = "2024-01-15T15:00:00+08:00"
        declarations.append({"raw_symbol": symbol, "product": product, "expiry_at": expiry,
                             "instance_id": f"{symbol}|{expiry}", "identity_status": "SYNTHETIC"})
        # Four have bars; one non-RB declaration deliberately has no bars.
        if index < 4:
            bars.append(ContractBar(symbol=symbol, duration_seconds=86400,
                                    datetime_nano=int(first.timestamp()) * 1_000_000_000,
                                    ts=first, source="SYNTHETIC_excluded_prices", open=Decimal(100),
                                    high=Decimal(101), low=Decimal(99), close=Decimal(100)))
    if reverse:
        declarations.reverse()
        bars.reverse()
    source = tmp_path / "SYNTHETIC-input"
    publish_input_bundle(source, dataset_kind="real_export", created_at="2024-01-01T00:00:00+08:00",
                         bars_rows=bars_rows_from_bars(bars), contracts=declarations,
                         metadata={"evidence_level": "SYNTHETIC", "role": "offline_schema_fixture"},
                         availability={}, request_info={"products": [p for _, p in other_products] + ["SHFE.RB"]},
                         source_hashes={"SYNTHETIC": "a" * 64}, read_only_transaction={"executed": False},
                         clock_policy={"calendar": "SYNTHETIC_raw_dates_only"}, notes={"prices": "SYNTHETIC"})
    config = tmp_path / "SYNTHETIC-base.json"
    config.write_bytes(FIXTURE.read_bytes())
    return source, config


def repin(source, name, payload):
    (source / name).write_bytes(payload)
    manifest = document(source / "manifest.json")
    manifest["content_hashes"][name] = hashlib.sha256(payload).hexdigest()
    (source / "manifest.json").write_bytes(canonical_json_bytes(manifest))


def repin_child(child, name, payload):
    (child / name).write_bytes(payload)
    manifest = document(child / "manifest.json")
    manifest["content_hashes"][name] = hashlib.sha256(payload).hexdigest()
    (child / "manifest.json").write_bytes(canonical_json_bytes(manifest))


def csv_bytes(columns, rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def prepare(cli, source, config):
    source, config = source.resolve(), config.resolve()
    inventory, legacy, snapshot = cli._source_pins()
    with localcontext(ARITHMETIC):
        captured, assumptions, raw_config, input_snapshot = cli._capture(source, config)
        coverage, bars = cli._audit_population(captured, assumptions)
    snapshot.update(input_snapshot)
    return source, config, captured, assumptions, raw_config, coverage, bars, legacy, snapshot


def publish_fixture(cli, source, config, output):
    # This internal publisher seam accepts already captured SYNTHETIC cases.
    # The production run_transfer/CLI always requires the pinned real bundle.
    source, config, captured, assumptions, raw_config, coverage, bars, legacy, snapshot = prepare(cli, source, config)
    return cli._publish(source, config, output, captured, assumptions, raw_config,
                        coverage, bars, legacy, snapshot)


@pytest.fixture
def published(cli, tmp_path):
    source, config = bundle(tmp_path)
    output = tmp_path / "result"
    publish_fixture(cli, source, config, output)
    return source, config, output


def child_path(output, symbol="SHFE.rb2203", strategy="donchian_trend"):
    return output / "children" / symbol / strategy


def test_registered_parameter_fixture_is_canonical_nine_key_synthetic_contract():
    assert document(FIXTURE) == BASE
    assert FIXTURE.read_bytes() == canonical_json_bytes(BASE)
    assert len(document(FIXTURE)) == 9


@pytest.mark.parametrize("label", [True, False])
def test_production_cli_cannot_relax_pinned_population_by_claiming_synthetic(cli, tmp_path, capsys, monkeypatch, label):
    source, config = bundle(tmp_path)
    if not label:
        repin(source, "metadata.json", canonical_json_bytes({"role": "offline_schema_fixture"}))
    calls = []
    monkeypatch.setattr(cli, "run_history_scenario", lambda *a, **kw: calls.append((a, kw)))
    output = tmp_path / "forbidden-production-fixture"
    args = ["--input-bundle", str(source), "--base-config", str(config), "--output", str(output)]
    assert cli.main(args) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "rejected"
    assert ("pinned real-input-02" if label else "original mixed input directory") in result["reason"]
    assert not calls and not output.exists()
    with pytest.raises(ValueError):
        cli.run_transfer(source, config, output)
    assert not calls and not output.exists()
    assert cli.main(args + ["--allow-synthetic"]) == 2
    assert not calls and not output.exists()


@pytest.mark.parametrize("forgery", ["counts", "production_label", "real_manifest"])
def test_labels_counts_and_copied_real_manifest_cannot_qualify_a_private_synthetic_case(cli, tmp_path, monkeypatch, forgery):
    source, config = bundle(tmp_path)
    output, calls = tmp_path / "forged", []
    monkeypatch.setattr(cli, "run_history_scenario", lambda *a, **kw: calls.append((a, kw)))
    if forgery == "real_manifest":
        inventory, _, _ = cli._source_pins()
        real_manifest = Path(inventory["bundle"]["path"]) / "manifest.json"
        (source / "manifest.json").write_bytes(real_manifest.read_bytes())
        with pytest.raises(ValueError, match="input hash"):
            publish_fixture(cli, source, config, output)
    else:
        source, config, captured, assumptions, raw_config, coverage, bars, legacy, snapshot = prepare(cli, source, config)
        if forgery == "counts":
            coverage["expected_counts"] = dict(coverage["expected_counts"], child_calls=252)
            coverage["rb_declarations"] = 84
        else:
            coverage["data_kind"] = "FROZEN_REAL_EXPORT"
            coverage["evaluation_scope"] = "FROZEN_REAL_INPUT_SCENARIO"
            coverage["production_acceptance"] = True
        with pytest.raises(ValueError, match="recomputed publication coverage"):
            cli._publish(source, config, output, captured, assumptions, raw_config, coverage, bars, legacy, snapshot)
    assert calls == [] and not output.exists()


def test_mixed_six_product_bundle_precommit_and_exactly_one_original_engine_call_per_account(cli, tmp_path, monkeypatch):
    source, config = bundle(tmp_path)
    before = tree(source)
    output = tmp_path / "result"
    original, calls, engine_bytes = cli.run_history_scenario, [], {}
    precommit_bytes = None
    def record(actual_source, directory, **kw):
        nonlocal precommit_bytes
        assert actual_source == source.resolve()
        assert kw["config_path"] == config.resolve()
        precommit = output / "chronology-precommit.json"
        assert precommit.is_file()
        if not calls:
            assert set(tree(output)) == {"chronology-precommit.json", "base_config.json"}
            precommit_bytes = precommit.read_bytes()
        assert precommit.read_bytes() == precommit_bytes
        assert document(precommit)["pnl_inspection_started"] is False
        calls.append((kw["symbol"], kw["strategy"]))
        result = original(actual_source, directory, **kw)
        engine_bytes[directory] = {p.name: p.read_bytes() for p in directory.iterdir()}
        return result
    monkeypatch.setattr(cli, "run_history_scenario", record)
    publish_fixture(cli, source, config, output)
    assert calls == [(s, strategy) for s in ("SHFE.rb2203", "SHFE.rb2204", "SHFE.rb2205") for strategy in STRATEGIES]
    coverage = document(output / "coverage.json")
    assert coverage["rb_declarations"] == 3 and coverage["rb_bars"] == 170
    assert coverage["non_rb_declarations_excluded"] == 5 and coverage["non_rb_bars_excluded"] == 4
    assert len(coverage["declaration_classifications"]) == 8
    assert sum(r["classification"] == "EXCLUDED_NON_RB" for r in coverage["declaration_classifications"]) == 5
    assert tree(source) == before
    for directory, payloads in engine_bytes.items():
        assert set(payloads) == {"config.json", "fills.csv", "equity.csv", "decisions.csv", "report.json", "manifest.json"}
        assert payloads == {p.name: p.read_bytes() for p in directory.iterdir()}
        for name in ("config.json", "report.json", "manifest.json"):
            native = json.loads(payloads[name])
            assert {"evidence_level", "broker", "enable_trading", "real_send_count", "real_cancel_count"}.isdisjoint(native)


def test_cross_year_account_carry_and_years_conserve_full_trajectory_without_boundary_close(published):
    _, _, output = published
    daily = [r for r in ledger(output / "daily_attribution.csv") if r["symbol"] == "SHFE.rb2203" and r["strategy"] == "donchian_trend"]
    first_2022 = next(i for i, r in enumerate(daily) if r["raw_year"] == "2022")
    prior, current = daily[first_2022 - 1], daily[first_2022]
    assert prior["position_lots"] == current["position_lots"] == "1"
    assert current["previous_equity"] == prior["equity"]
    assert current["previous_equity"] != BASE["initial_cash"]
    assert prior["terminal"] == "false" and current["terminal_close_fills"] == "0"
    years = [r for r in ledger(output / "contract_year.csv") if r["symbol"] == "SHFE.rb2203" and r["strategy"] == "donchian_trend" and r["view"] == "ALL"]
    y2021, y2022 = (next(r for r in years if r["raw_year"] == y) for y in ("2021", "2022"))
    assert y2022["carry_status"] == "CARRIED_FROM_PREVIOUS_RAW_YEAR"
    assert y2022["position_before_first_row"] == "1"
    assert y2022["previous_equity"] == y2021["end_equity"]
    assert y2021["terminal_close_fills"] == "0" and y2022["terminal_close_fills"] == "1"
    report = document(child_path(output) / "report.json")
    for year_field, report_field in (("year_net_delta", "net_pnl"), ("year_gross_delta", "gross_pnl"),
                                      ("year_fee_delta", "fees"), ("year_slippage_delta", "slippage")):
        assert sum(Fraction(r[year_field] or "0") for r in years) == Fraction(report[report_field])
    # Copying the future full-account terminal result into the earlier block
    # is a false attribution, exposed by this deliberately reversing path.
    assert Fraction(y2021["year_net_delta"]) != Fraction(report["net_pnl"])


def test_fraction_oracle_all_daily_costs_reversal_terminal_and_conservation(published):
    _, _, output = published
    for strategy in STRATEGIES:
        child = child_path(output, strategy=strategy)
        report, equity, fills = document(child / "report.json"), ledger(child / "equity.csv"), ledger(child / "fills.csv")
        daily = [r for r in ledger(output / "daily_attribution.csv") if r["symbol"] == "SHFE.rb2203" and r["strategy"] == strategy]
        previous_equity, previous_gross, previous_fee, previous_slip = Fraction(BASE["initial_cash"]), Fraction(0), Fraction(0), Fraction(0)
        grouped = defaultdict(list)
        for f in fills:
            grouped[int(f["observation_index"])].append(f)
            price, lots = Fraction(f["ideal_price"]), int(f["lots"])
            flat = Fraction(BASE["fee_per_lot"]) * lots
            rate = Fraction(BASE["fee_rate"]) * price * Fraction(BASE["multiplier"]) * lots
            slip = Fraction(BASE["price_tick"]) * Fraction(BASE["slippage_ticks"]) * Fraction(BASE["multiplier"]) * lots
            assert Fraction(f["fee_per_lot_component"]) == flat
            assert Fraction(f["fee_rate_component"]) == rate
            assert Fraction(f["fee"]) == flat + rate
            assert Fraction(f["slippage"]) == slip
        for j, (mark, row) in enumerate(zip(equity, daily)):
            value, gross, fee, slip = (Fraction(mark[k]) for k in ("equity", "gross_pnl", "fees", "slippage"))
            assert Fraction(row["previous_equity"]) == previous_equity
            assert Fraction(row["equity_delta"]) == value - previous_equity
            assert Fraction(row["gross_delta"]) == gross - previous_gross
            assert Fraction(row["fee_delta"]) == fee - previous_fee == sum(Fraction(f["fee"]) for f in grouped[j])
            assert Fraction(row["slippage_delta"]) == slip - previous_slip == sum(Fraction(f["slippage"]) for f in grouped[j])
            assert Fraction(row["net_delta"]) == value - previous_equity == gross - previous_gross - (fee - previous_fee) - (slip - previous_slip)
            previous_equity, previous_gross, previous_fee, previous_slip = value, gross, fee, slip
        assert previous_equity - Fraction(BASE["initial_cash"]) == Fraction(report["net_pnl"])
    fills = ledger(child_path(output) / "fills.csv")
    assert [r["offset"] for r in fills] == ["open", "close", "open", "close"]
    assert [r["side"] for r in fills] == ["1", "-1", "-1", "1"]
    assert fills[-1]["phase"] == "terminal_close" and fills[-1]["signal_index"] == ""
    assert Fraction(fills[-1]["fee"]) > 0 and Fraction(fills[-1]["slippage"]) > 0


def test_primary_mask_n14_n60_n96_and_empty_future_only_denominators(published):
    _, _, output = published
    daily, years = ledger(output / "daily_attribution.csv"), ledger(output / "contract_year.csv")
    assert len(daily) == (14 + 60 + 96) * 3
    assert sum(r["primary_support"] == "true" for r in daily) == 36 * 3
    assert all((r["primary_support"] == "true") == (int(r["j"]) >= 60) for r in daily)
    assert len(years) == 3 * 3 * 7 * 2
    empty = [r for r in years if r["symbol"] in ("SHFE.rb2204", "SHFE.rb2205") and r["view"] == "PRIMARY"]
    assert all(r["status"] == "PRIMARY_EMPTY" and r["year_net_delta"] == "" and r["undefined_reason"] for r in empty)
    future = [r for r in years if r["raw_year"] == "2020"]
    assert all(r["temporal_class"] == "FUTURE_ONLY" and r["observations"] == "0" for r in future)
    assert all(r["year_status"] == "PARTIAL_INPUT_WINDOW" for r in years if r["raw_year"] in ("2020", "2026"))
    for symbol in ("SHFE.rb2204", "SHFE.rb2205"):
        for strategy in STRATEGIES:
            child = child_path(output, symbol=symbol, strategy=strategy)
            report = document(child / "report.json")
            assert report["fills"] == 0 and report["net_pnl"] == "0"
            assert all(Fraction(r["equity"]) == Fraction(BASE["initial_cash"]) for r in ledger(child / "equity.csv"))
    assert all(document(child_path(output, "SHFE.rb2205", s) / "report.json")["candidate_counts"] == {"null": 14} for s in STRATEGIES)


def load_daily(cli, output):
    rows = ledger(output / "daily_attribution.csv")
    for row in rows:
        for field in ("j", "datetime_nano", "raw_year"):
            row[field] = int(row[field])
        row["primary_support"] = row["primary_support"] == "true"
        for field in ("net_delta", "gross_delta", "fee_delta", "slippage_delta", "equity"):
            row[field] = Decimal(row[field])
    return rows


@pytest.mark.parametrize("problem", ["missing", "timestamp", "nonfinite"])
def test_pairing_classifies_only_actual_missing_mismatch_or_nonfinite_rows(cli, published, problem):
    _, _, output = published
    daily = load_daily(cli, output)
    target = next(r for r in daily if r["symbol"] == "SHFE.rb2203" and r["strategy"] == "ma_trend" and r["j"] == 65)
    if problem == "missing":
        daily.remove(target)
    elif problem == "timestamp":
        target["datetime_nano"] += 1
    else:
        target["net_delta"] = Decimal("NaN")
    with localcontext(ARITHMETIC):
        pairs, excluded = cli.pair_observations(daily)
    assert len(excluded) == 4  # two affected pairs, both ALL and PRIMARY
    assert {r["view"] for r in excluded} == {"ALL", "PRIMARY"}
    assert all(r["symbol"] == "SHFE.rb2203" and r["j"] == 65 for r in excluded)
    expected = {"MISSING_A", "MISSING_B"} if problem == "missing" else {"IDENTITY_MISMATCH"} if problem == "timestamp" else {"NONFINITE_OR_INVALID_AMOUNT"}
    assert {r["status"] for r in excluded} <= expected
    unaffected = [r for r in pairs if r["symbol"] == "SHFE.rb2203" and r["j"] == 65]
    assert len(unaffected) == 2 and all((r["strategy_a"], r["strategy_b"]) == ("donchian_trend", "zscore_reversion") for r in unaffected)


def test_pair_and_year_summaries_have_exact_ledger_derived_denominators_and_even_medians(published):
    _, _, output = published
    daily, cy = ledger(output / "daily_attribution.csv"), ledger(output / "contract_year.csv")
    pairs, ps, ys = (ledger(output / n) for n in ("paired_observations.csv", "paired_summary.csv", "year_summary.csv"))
    assert len(pairs) == (170 + 36) * 3 and len(ps) == len(ys) == 42
    for row in ys:
        cells = [r for r in cy if (r["raw_year"], r["strategy"], r["view"]) == (row["raw_year"], row["strategy"], row["view"]) and int(r["observations"])]
        values = sorted(Fraction(r["year_net_delta"]) for r in cells)
        assert int(row["contract_year_cells"]) == len(cells)
        assert int(row["observations"]) == sum(int(r["observations"]) for r in cells)
        if values:
            assert Fraction(row["independent_account_descriptive_net_sum"]) == sum(values)
            n = len(values)
            median = values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2
            assert Fraction(row["contract_year_net_median"]) == median
            assert [int(row[k]) for k in ("positive_contract_years", "zero_contract_years", "negative_contract_years")] == [sum(v > 0 for v in values), sum(v == 0 for v in values), sum(v < 0 for v in values)]
        else:
            assert row["independent_account_descriptive_net_sum"] == row["contract_year_net_median"] == ""
            assert row["undefined_reason"]
    for row in ps:
        matched = [r for r in pairs if (r["raw_year"], r["view"], r["strategy_a"], r["strategy_b"]) == (row["raw_year"], row["view"], row["strategy_a"], row["strategy_b"])]
        assert int(row["matched_observations"]) == len(matched)
        assert int(row["matched_contract_years"]) == len({r["contract_instance_id"] for r in matched})
        assert int(row["raw_dates"]) == len({r["raw_date"] for r in matched})
        for name in ("net", "gross", "fee", "slippage"):
            for p in matched:
                assert Fraction(p[f"{name}_delta_a_minus_b"]) == Fraction(p[f"{name}_delta_a"]) - Fraction(p[f"{name}_delta_b"])
            actual = row[f"independent_account_descriptive_{name}_difference_sum"]
            assert (Fraction(actual) == sum(Fraction(p[f"{name}_delta_a_minus_b"]) for p in matched)) if matched else actual == ""
    assert sum(int(r["observations"]) for r in ys if r["view"] == "ALL") == len(daily)


def test_two_fresh_directories_are_identical_and_hash_closure_includes_every_child(cli, tmp_path):
    source, config = bundle(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    publish_fixture(cli, source, config, first)
    with localcontext() as low_precision:
        low_precision.prec = 4
        publish_fixture(cli, source, config, second)
    assert tree(first) == tree(second)
    manifest, summary = document(first / "manifest.json"), document(first / "summary.json")
    assert manifest["status"] == "complete" and len(manifest["legacy_code_hashes"]) == 10
    assert manifest["content_hashes"] == {n: h for n, h in tree(first).items() if n != "manifest.json"}
    assert manifest["actual_counts"] == {"child_calls": 9, "daily_rows": 510, "primary_rows": 108,
                                         "contract_year_cells": 126, "year_summary_rows": 42,
                                         "paired_summary_rows": 42, "paired_observation_rows": 618}
    assert summary["product_hard_review"] == "NOT_RUN"
    for name in ("coverage.json", "summary.json", "manifest.json", "chronology-precommit.json"):
        scoped = document(first / name)
        assert scoped["evaluation_scope"] == "SYNTHETIC_TEST_ONLY"
        assert scoped["production_acceptance"] is False
    coverage = document(first / "coverage.json")
    entries = coverage["child_provenance"]
    reference = {"path": "coverage.json", "json_pointer": "/child_provenance", "entry_count": 9,
                 "sha256": hashlib.sha256((first / "coverage.json").read_bytes()).hexdigest()}
    assert summary["child_provenance_ref"] == manifest["child_provenance_ref"] == reference
    assert "child_provenance" not in summary and len(entries) == 9
    assert [(r["raw_symbol"], r["strategy"]) for r in entries] == [(s, strategy) for s in
        ("SHFE.rb2203", "SHFE.rb2204", "SHFE.rb2205") for strategy in STRATEGIES]
    fields = {"raw_symbol", "contract_instance_id", "strategy", "child_directory", "child_artifact_sha256",
              "input_manifest_sha256", "input_config_sha256", "input_content_hashes", "code_hashes", "wrapper_source_sha256",
              "assumptions_verification", "historical_strict_execution", "evidence_level", "broker", "enable_trading",
              "real_send_count", "real_cancel_count", "safety_basis", "external_broker_verification", "real_order_count_scope"}
    for row in entries:
        assert set(row) == fields
        assert row["historical_strict_execution"] is row["enable_trading"] is False
        assert row["assumptions_verification"] == "not_historical_verified" and row["evidence_level"] == "SCENARIO"
        assert row["broker"] == row["external_broker_verification"] == "NOT_RUN"
        assert type(row["real_send_count"]) is type(row["real_cancel_count"]) is int
        assert row["real_send_count"] == row["real_cancel_count"] == 0
        assert row["safety_basis"] == "ENGINE_BYTES_AND_WRAPPER_OFFLINE_EXECUTION_SCOPE"
        assert row["real_order_count_scope"] == "THIS_WRAPPER_INVOCATION_AND_CHILD"
        child = first / row["child_directory"]
        native = document(child / "manifest.json")
        assert row["child_directory"] == f"children/{row['raw_symbol']}/{row['strategy']}/"
        assert row["contract_instance_id"] == document(child / "config.json")["contract_instance_id"]
        assert row["child_artifact_sha256"] == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in child.iterdir()}
        assert {n: h for n, h in row["child_artifact_sha256"].items() if n != "manifest.json"} == native["content_hashes"]
        assert row["wrapper_source_sha256"] == manifest["wrapper_sha256"]
        assert row["code_hashes"] == manifest["legacy_code_hashes"] == native["code_hashes"]
        assert row["input_manifest_sha256"] == manifest["input_manifest_sha256"] == native["input_manifest_sha256"]
        assert row["input_config_sha256"] == manifest["input_base_config_sha256"] == native["input_config_sha256"]
        assert row["input_content_hashes"] == manifest["input_content_hashes"] == native["input_content_hashes"]
    assert len(ledger(child_path(first) / "fills.csv")) > 0  # simulated fills do not change scoped real counts
    precommit = document(first / "chronology-precommit.json")
    assert precommit["pnl_inspection_started"] is False
    assert precommit["chronology"]["raw_years"] == list(range(2020, 2027))
    report = (first / "report.md").read_text(encoding="utf-8")
    assert report.startswith("SCENARIO only") and "SYNTHETIC" in report and "confounded" in report
    assert f"path=`coverage.json`; json_pointer=`/child_provenance`; entry_count=9; sha256=`{reference['sha256']}`" in report
    assert "ENGINE_BYTES_AND_WRAPPER_OFFLINE_EXECUTION_SCOPE" in report and "External broker verification remains NOT_RUN" in report
    for p in first.rglob("*"):
        if p.is_file():
            assert b"\r" not in p.read_bytes()


@pytest.mark.parametrize("problem", ["missing_rb_bars", "undeclared_rb", "duplicate_symbol", "duplicate_instance", "wrong_instance",
                                     "nonfuture", "wrong_product", "duration", "duplicate_time", "invalid_ohlc", "nonfinite", "raw_year", "raw_offset"])
def test_population_identity_and_price_failures_are_preflight_without_replay(cli, tmp_path, monkeypatch, problem):
    source, config = bundle(tmp_path)
    declarations = document(source / "contracts.json")
    rows = ledger(source / "bars.csv")
    if problem in ("duplicate_symbol", "duplicate_instance", "wrong_instance", "nonfuture", "wrong_product"):
        if problem == "duplicate_symbol":
            declarations.append(dict(declarations[0]))
        elif problem == "duplicate_instance":
            declarations[1]["instance_id"] = declarations[0]["instance_id"]
        elif problem == "wrong_instance":
            declarations[0]["instance_id"] = "SHFE.rb2203|2030-03-15T15:00:00+08:00"
        elif problem == "nonfuture":
            declarations[0]["asset_class"] = "OPTION"
        else:
            declarations[0]["product"] = "DCE.M"
        repin(source, "contracts.json", canonical_json_bytes(declarations))
    else:
        if problem == "missing_rb_bars":
            rows = [r for r in rows if r["symbol"] != "SHFE.rb2205"]
        elif problem == "undeclared_rb":
            rows[0]["symbol"] = "SHFE.rb2299"
        elif problem == "duration":
            rows[0]["duration_seconds"] = "60"
        elif problem == "duplicate_time":
            rows[1] = dict(rows[0])
        elif problem == "invalid_ohlc":
            rows[0]["high"] = "1"
        elif problem == "nonfinite":
            rows[0]["close"] = "NaN"
        elif problem == "raw_year":
            rows[0]["ts"] = "2027-01-01T00:00:00+08:00"
        else:
            rows[0]["ts"] = "2021-11-01T00:00:00+00:00"
        repin(source, "bars.csv", csv_bytes(BARS_CSV_COLUMNS, rows))
    before, output, calls = tree(source), tmp_path / "out", []
    monkeypatch.setattr(cli, "run_history_scenario", lambda *a, **kw: calls.append((a, kw)))
    with pytest.raises((ValueError, ArithmeticError)):
        publish_fixture(cli, source, config, output)
    assert not calls and not output.exists() and tree(source) == before


@pytest.mark.parametrize("problem", ["hash", "missing", "duplicate_json", "nonfinite_json", "base_variant", "base_missing"])
def test_strict_capture_rejects_corruption_before_any_output(cli, tmp_path, problem):
    source, config = bundle(tmp_path)
    if problem == "hash":
        (source / "bars.csv").write_bytes(b"SYNTHETIC corruption")
    elif problem == "missing":
        (source / "availability.json").unlink()
    elif problem == "duplicate_json":
        repin(source, "metadata.json", b'{"evidence_level":"SYNTHETIC","evidence_level":"REAL"}\n')
    elif problem == "nonfinite_json":
        repin(source, "metadata.json", b'{"evidence_level":"SYNTHETIC","bad":1e999}\n')
    elif problem == "base_variant":
        config.write_bytes(canonical_json_bytes(dict(BASE, lots=2)))
    else:
        config = tmp_path / "not-created-required-base.json"
    output = tmp_path / "out"
    with pytest.raises((ValueError, OSError)):
        publish_fixture(cli, source, config, output)
    assert not output.exists()


@pytest.mark.parametrize("arguments", [[], ["--input-bundle", "a"], ["--input-bundle", "a", "--input-bundle", "b"],
                                      ["--out", "a"], ["--strategy", "ma_trend"], ["--allow-synthetic"]])
def test_cli_grammar_rejects_unknown_repeated_missing_and_abbreviated_options(cli, arguments, capsys):
    assert cli.main(arguments) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "rejected" and result["error_type"] == "_ArgumentError"


@pytest.mark.parametrize("kind", ["existing", "input_child", "input_parent", "src", "scripts", "tests", "config", "tools", "docs", "NUL"])
def test_outputs_are_create_new_and_cannot_overlap_inputs_or_source(cli, tmp_path, kind):
    source, config = bundle(tmp_path)
    output = tmp_path / "out"
    if kind == "existing":
        output.mkdir()
        (output / "sentinel").write_bytes(b"preserve")
    elif kind == "input_child":
        output = source / "out"
    elif kind == "input_parent":
        output = source.parent
    elif kind == "NUL":
        output = tmp_path / "NUL"
    else:
        output = ROOT / kind / "rb-chronology-forbidden-output"
    before = tree(tmp_path)
    with pytest.raises((ValueError, cli.OutputDirectoryError)):
        publish_fixture(cli, source, config, output)
    assert tree(tmp_path) == before


def test_output_symlink_ancestor_rejected(cli, tmp_path):
    source, config = bundle(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(cli.OutputDirectoryError):
        publish_fixture(cli, source, config, alias / "out")
    assert not (target / "out").exists()


@pytest.mark.parametrize("corruption", ["incomplete", "timestamp", "missing_row", "nonfinite", "double_slippage", "future_pnl_earlier", "terminal_boundary", "report"])
def test_bad_child_even_when_rehashed_cannot_receive_complete_publication(cli, tmp_path, monkeypatch, corruption):
    source, config = bundle(tmp_path)
    output, original, calls = tmp_path / "bad", cli.run_history_scenario, 0
    def corrupt(*a, **kw):
        nonlocal calls
        result = original(*a, **kw)
        calls += 1
        if calls != 1:
            return result
        child = a[1]
        if corruption == "incomplete":
            manifest = document(child / "manifest.json")
            manifest["status"] = "incomplete"
            (child / "manifest.json").write_bytes(canonical_json_bytes(manifest))
        elif corruption == "report":
            report = document(child / "report.json")
            report["net_pnl"] = "999999"
            repin_child(child, "report.json", canonical_json_bytes(report))
        elif corruption == "double_slippage":
            fills = ledger(child / "fills.csv")
            fills[0]["slippage"] = str(Decimal(fills[0]["slippage"]) * 2)
            repin_child(child, "fills.csv", csv_bytes(cli.FILL_COLUMNS, fills))
        else:
            rows = ledger(child / "equity.csv")
            if corruption == "timestamp":
                rows[20]["datetime_nano"] = str(int(rows[20]["datetime_nano"]) + 1)
            elif corruption == "missing_row":
                rows.pop(20)
            elif corruption == "nonfinite":
                rows[20]["equity"] = "NaN"
            elif corruption == "future_pnl_earlier":
                rows[60]["equity"] = rows[-1]["equity"]
            else:
                rows[60]["terminal"] = "true"
            repin_child(child, "equity.csv", csv_bytes(cli.EQUITY_COLUMNS, rows))
        return result
    monkeypatch.setattr(cli, "run_history_scenario", corrupt)
    with pytest.raises(cli._Incomplete) as raised:
        publish_fixture(cli, source, config, output)
    assert calls == 1
    assert isinstance(raised.value.cause, (ValueError, ArithmeticError))
    assert document(output / "manifest.json")["status"] == "incomplete"


@pytest.mark.parametrize("name,field,replacement", [
    ("config.json", "assumptions_verification", None),
    ("report.json", "assumptions_verification", None),
    ("manifest.json", "assumptions_verification", None),
    ("config.json", "historical_strict_execution", None),
    ("report.json", "historical_strict_execution", 0),
    ("manifest.json", "historical_strict_execution", "false"),
    ("manifest.json", "broker", "RUN"),
    ("report.json", "enable_trading", 0),
    ("config.json", "evidence_level", "QUALIFIED"),
    ("manifest.json", "real_send_count", True),
    ("report.json", "real_send_count", 0.0),
    ("config.json", "real_cancel_count", "0"),
    ("manifest.json", "real_cancel_count", 1),
    ("config.json", "symbol", "SHFE.rb9999"),
    ("report.json", "contract_instance_id", "SYNTHETIC_wrong_instance"),
    ("manifest.json", "strategy", "ma_trend"),
    ("config.json", "strategy_parameters", {"entry_window": "40", "exit_window": 20}),
    ("manifest.json", "code_hashes", {"SYNTHETIC_wrong_source": "0" * 64}),
    ("manifest.json", "input_config_sha256", "0" * 64),
    ("manifest.json", "source_hashes_status", "externally_verified"),
    ("config.json", "dataset_kind", "historical_qualified"),
    ("config.json", "raw_symbol", "SHFE.rb9999"),
])
def test_native_child_required_facts_and_optional_assertions_are_typed_and_fail_closed(cli, tmp_path, monkeypatch,
                                                                                     name, field, replacement):
    source, config = bundle(tmp_path)
    output, calls, original = tmp_path / "native-contradiction", 0, cli.run_history_scenario
    def replay(*a, **kw):
        nonlocal calls
        calls += 1
        result = original(*a, **kw)
        child = a[1]
        value = document(child / name)
        if replacement is None:
            value.pop(field)
        else:
            value[field] = replacement
        if name == "manifest.json":
            (child / name).write_bytes(canonical_json_bytes(value))
        else:
            repin_child(child, name, canonical_json_bytes(value))
        return result
    monkeypatch.setattr(cli, "run_history_scenario", replay)
    with pytest.raises(cli._Incomplete) as raised:
        publish_fixture(cli, source, config, output)
    assert calls == 1 and isinstance(raised.value.cause, ValueError)
    assert "child" in str(raised.value.cause)
    marker = document(output / "manifest.json")
    assert marker["status"] == "incomplete" and marker["production_acceptance"] is False


@pytest.mark.parametrize("problem", ["missing_record", "duplicate_record", "missing_field", "send_bool", "cancel_string",
                                     "strict_int", "broker", "child_hash", "input_pin", "wrapper_pin", "source_map", "identity"])
def test_contradictory_or_incomplete_wrapper_child_records_prevent_complete_publication(cli, tmp_path, monkeypatch, problem):
    source, config = bundle(tmp_path)
    output = tmp_path / "bad-wrapper-record"
    original_json, original_engine = cli.canonical_json_bytes, cli.run_history_scenario
    calls, injected = 0, False
    def replay(*a, **kw):
        nonlocal calls
        calls += 1
        return original_engine(*a, **kw)
    def serialized(obj):
        nonlocal injected
        if isinstance(obj, dict) and obj.get("schema_version") == "rb_chronological_coverage_v1" and "child_provenance" in obj and not injected:
            injected = True
            records = obj["child_provenance"]
            if problem == "missing_record":
                records.pop()
            elif problem == "duplicate_record":
                records[-1] = deepcopy(records[0])
            elif problem == "missing_field":
                records[0].pop("safety_basis")
            else:
                key, value = {
                    "send_bool": ("real_send_count", False), "cancel_string": ("real_cancel_count", "0"),
                    "strict_int": ("historical_strict_execution", 0), "broker": ("broker", "UNKNOWN"),
                    "child_hash": ("child_artifact_sha256", dict(records[0]["child_artifact_sha256"], **{"manifest.json": "0" * 64})),
                    "input_pin": ("input_manifest_sha256", "0" * 64), "wrapper_pin": ("wrapper_source_sha256", "0" * 64),
                    "source_map": ("code_hashes", {"SYNTHETIC_wrong_source": "0" * 64}),
                    "identity": ("raw_symbol", "SHFE.rb9999"),
                }[problem]
                records[0][key] = value
        return original_json(obj)
    monkeypatch.setattr(cli, "run_history_scenario", replay)
    monkeypatch.setattr(cli, "canonical_json_bytes", serialized)
    with pytest.raises(cli._Incomplete) as raised:
        publish_fixture(cli, source, config, output)
    assert injected and calls == 9
    assert isinstance(raised.value.cause, ValueError) and "child provenance" in str(raised.value.cause)
    assert document(output / "manifest.json")["status"] == "incomplete"


@pytest.mark.parametrize("problem", ["summary_stale", "parent_stale", "report_stale", "summary_scope", "parent_scope"])
def test_stale_coverage_references_or_forged_synthetic_acceptance_prevent_complete_publication(cli, tmp_path, monkeypatch, problem):
    source, config = bundle(tmp_path)
    output = tmp_path / "stale-reference"
    original_json, original_write, original_engine = cli.canonical_json_bytes, cli._write_new, cli.run_history_scenario
    calls, injected = 0, False
    def replay(*a, **kw):
        nonlocal calls
        calls += 1
        return original_engine(*a, **kw)
    def serialized(obj):
        nonlocal injected
        if isinstance(obj, dict) and obj.get("schema_version") == "rb_daily_chronological_transfer_v1" and obj.get("status") == "complete":
            target = "summary" if "year_summary" in obj else "parent" if "content_hashes" in obj else ""
            if problem.startswith(target + "_") and target and not injected:
                injected = True
                if problem.endswith("stale"):
                    obj["child_provenance_ref"] = dict(obj["child_provenance_ref"], sha256="0" * 64)
                else:
                    obj["production_acceptance"] = True
        return original_json(obj)
    def written(root, name, payload, hashes):
        nonlocal injected
        if problem == "report_stale" and name == "report.md":
            injected = True
            payload = payload.replace(b"json_pointer=`/child_provenance`", b"json_pointer=`/SYNTHETIC_wrong_pointer`")
        return original_write(root, name, payload, hashes)
    monkeypatch.setattr(cli, "run_history_scenario", replay)
    monkeypatch.setattr(cli, "canonical_json_bytes", serialized)
    monkeypatch.setattr(cli, "_write_new", written)
    with pytest.raises(cli._Incomplete) as raised:
        publish_fixture(cli, source, config, output)
    assert injected and calls == 9
    assert isinstance(raised.value.cause, ValueError) and "child provenance" in str(raised.value.cause)
    assert document(output / "manifest.json")["status"] == "incomplete"


@pytest.mark.parametrize("failure", ["engine", "input_drift", "config_drift", "source_drift", "late_child_drift", "payload", "marker_collision", "partial_marker"])
def test_publication_failures_preserve_actual_failure_and_never_complete(cli, tmp_path, monkeypatch, failure):
    source, config = bundle(tmp_path)
    output = tmp_path / "failed"
    original_engine, original_hash, original_open = cli.run_history_scenario, cli.sha256_file, Path.open
    calls, drift, injected = 0, False, False
    first_child = None
    def replay(*a, **kw):
        nonlocal calls, drift, first_child
        calls += 1
        if failure == "engine" and calls == 2:
            raise RuntimeError("SYNTHETIC injected engine exception")
        result = original_engine(*a, **kw)
        if calls == 1:
            first_child = a[1]
            if failure == "input_drift":
                repin(source, "metadata.json", canonical_json_bytes({"evidence_level": "SYNTHETIC", "revision": 2}))
            elif failure == "config_drift":
                config.write_bytes(canonical_json_bytes(dict(BASE, lots=2)))
            elif failure == "source_drift":
                drift = True  # hash-reader injection; actual repository bytes remain read-only
        if calls == 2 and failure == "late_child_drift":
            (first_child / "decisions.csv").write_bytes(b"SYNTHETIC late mutation")
        return result
    def hashed(path):
        if drift and path == ROOT / cli.SELF:
            return "f" * 64
        return original_hash(path)
    class Interrupted:
        def __init__(self, handle):
            self.handle, self.failed = handle, False
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return self.handle.__exit__(*a)
        def __getattr__(self, name):
            return getattr(self.handle, name)
        def write(self, payload):
            if not self.failed:
                self.failed = True
                self.handle.write(payload[:30])
                raise OSError("SYNTHETIC partial marker failure")
            return self.handle.write(payload)
    def opened(path, mode="r", *a, **kw):
        nonlocal injected
        if failure == "payload" and path == output / "daily_attribution.csv" and "x" in mode:
            raise OSError("SYNTHETIC payload disk failure")
        if path == output / "manifest.json" and "x" in mode and not injected:
            injected = True
            if failure == "marker_collision":
                with original_open(path, "xb") as handle:
                    handle.write(b"other-writer-sentinel")
            elif failure == "partial_marker":
                return Interrupted(original_open(path, mode, *a, **kw))
        return original_open(path, mode, *a, **kw)
    monkeypatch.setattr(cli, "run_history_scenario", replay)
    monkeypatch.setattr(cli, "sha256_file", hashed)
    monkeypatch.setattr(Path, "open", opened)
    with pytest.raises(cli._Incomplete) as raised:
        publish_fixture(cli, source, config, output)
    expected_type = RuntimeError if failure == "engine" else OSError if failure in ("payload", "marker_collision", "partial_marker") else ValueError
    assert isinstance(raised.value.cause, expected_type)
    if failure == "engine":
        assert calls == 2 and "injected engine exception" in str(raised.value.cause)
    elif failure in ("late_child_drift", "payload", "marker_collision", "partial_marker"):
        assert calls == 9
    marker = output / "manifest.json"
    if failure == "marker_collision":
        assert marker.read_bytes() == b"other-writer-sentinel"
    else:
        assert document(marker)["status"] == "incomplete"
