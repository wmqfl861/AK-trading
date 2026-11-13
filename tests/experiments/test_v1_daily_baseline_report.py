"""SYNTHETIC accounting paths only; no real input, engine, API or credentials."""

import csv
import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, Inexact, localcontext
from fractions import Fraction
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/experiments/v1_daily_baseline_report.py"
SPEC = importlib.util.spec_from_file_location("v1_daily_baseline_report", TOOL)
report = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report)
S = ("donchian_trend", "ma_trend", "zscore_reversion")
Y = range(2020, 2027)
V = ("ALL", "PRIMARY")
R = ("UNKNOWN", "FLAT", "TREND_UP", "TREND_DOWN", "RANGE", "MIXED")
PAIRS = ((S[0], S[1]), (S[0], S[2]), (S[1], S[2]))
COUNTS = (60, 61, 62, 63)
INITIAL = 1000000


def ftext(value):
    if value is None:
        return ""
    if isinstance(value, Fraction):
        assert value.denominator in (1, 2)
        return str(value.numerator) if value.denominator == 1 else str(Decimal(value.numerator) / 2)
    return str(value)


def summary(values):
    ordered = sorted(values)
    n = len(values)
    return (sum(values, Fraction()) if n else None,
            (ordered[(n - 1) // 2] + ordered[n // 2]) / 2 if n else None,
            min(values) if n else None, max(values) if n else None,
            sum(x > 0 for x in values), sum(x == 0 for x in values), sum(x < 0 for x in values))


def put_json(path, data):
    raw = (json.dumps(data, sort_keys=True, indent=2) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def put_csv(path, rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=rows[0].keys(), lineterminator="\n")
    writer.writeheader()
    writer.writerows({k: ftext(v) for k, v in row.items()} for row in rows)
    raw = stream.getvalue().encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def synthetic_daily():
    """Known increments straddle j=59/60 and New Year; terminal cost is one."""
    rows = []
    gains = ((0, 100, 101, 102), (0, 1, 2, 100), (-2, -4, 0, 5))
    for contract, n in enumerate(COUNTS):
        symbol = f"SYNTHETIC.rb{contract}"
        for st, strategy in enumerate(S):
            gross = fees = 0
            equity = INITIAL
            for j in range(n):
                ts = datetime(2020, 11, 2, tzinfo=timezone(timedelta(hours=8))) + timedelta(days=j)
                g = gains[st][contract] if j == 58 else 10 if j == 59 else 2 if j == 60 else 0
                fee = int(j == n - 1)
                previous = equity
                gross += g
                fees += fee
                equity += g - fee
                rows.append({"symbol": symbol, "contract_instance_id": symbol + "|SYNTHETIC",
                             "strategy": strategy, "j": str(j), "datetime_nano": str(int(ts.timestamp()) * 10**9),
                             "raw_ts": ts.isoformat(), "raw_date": ts.date().isoformat(), "raw_year": str(ts.year),
                             "primary_support": str(j >= 60).lower(), "terminal": str(j == n - 1).lower(),
                             "equity": str(equity), "previous_equity": str(previous), "equity_delta": str(g - fee),
                             "gross_pnl": str(gross), "gross_delta": str(g), "fees": str(fees), "fee_delta": str(fee),
                             "slippage": "0", "slippage_delta": "0", "net_delta": str(g - fee),
                             "net_pnl": str(equity - INITIAL), "cash": str(equity), "realized_pnl": str(equity - INITIAL),
                             "unrealized_pnl": "0", "fills_at_j": str(fee), "terminal_close_fills": str(fee)})
    return rows


def synthetic_chronological_tables(daily):
    """Fixture reference tables use exact Fraction sums; expectations below are hand checked."""
    cells = []
    symbols = sorted({r["symbol"] for r in daily})
    for symbol in symbols:
        for st in S:
            for year in Y:
                for view in V:
                    rows = [r for r in daily if r["symbol"] == symbol and r["strategy"] == st and
                            int(r["raw_year"]) == year and (view == "ALL" or int(r["j"]) >= 60)]
                    cell = {"symbol": symbol, "contract_instance_id": symbol + "|SYNTHETIC", "strategy": st,
                            "raw_year": str(year), "view": view, "observations": len(rows),
                            "raw_dates": len(rows), "fills": sum(int(r["fills_at_j"]) for r in rows),
                            "terminal_close_fills": sum(int(r["terminal_close_fills"]) for r in rows),
                            "first_j": rows[0]["j"] if rows else "", "last_j": rows[-1]["j"] if rows else "",
                            "first_raw_ts": rows[0]["raw_ts"] if rows else "", "last_raw_ts": rows[-1]["raw_ts"] if rows else "",
                            "previous_equity": rows[0]["previous_equity"] if rows else "",
                            "end_equity": rows[-1]["equity"] if rows else ""}
                    for field in ("net", "gross", "fee", "slippage"):
                        cell["year_" + field + "_delta"] = sum((Fraction(r[field + "_delta"]) for r in rows), Fraction()) if rows else None
                    cells.append(cell)
    years = []
    for year in Y:
        for st in S:
            for view in V:
                cs = [c for c in cells if (c["raw_year"], c["strategy"], c["view"]) == (str(year), st, view) and c["observations"]]
                rows = [r for r in daily if r["strategy"] == st and int(r["raw_year"]) == year and
                        (view == "ALL" or int(r["j"]) >= 60)]
                total, median, low, high, pos, zero, neg = summary([c["year_net_delta"] for c in cs])
                record = {"raw_year": str(year), "strategy": st, "view": view,
                          "status": ("PARTIAL_INPUT_WINDOW" if year in (2020, 2026) else "OBSERVED") if rows else
                          "PRIMARY_EMPTY" if view == "PRIMARY" else "NO_ROWS",
                          "year_status": "PARTIAL_INPUT_WINDOW" if year in (2020, 2026) else "OBSERVED_RAW_YEAR",
                          "contract_year_cells": len(cs), "distinct_contracts": len(cs), "observations": len(rows),
                          "raw_dates": len({r["raw_date"] for r in rows}), "fills": sum(int(r["fills_at_j"]) for r in rows),
                          "terminal_close_fills": sum(int(r["terminal_close_fills"]) for r in rows),
                          "contract_year_net_min": low, "contract_year_net_median": median, "contract_year_net_max": high,
                          "positive_contract_years": pos, "zero_contract_years": zero, "negative_contract_years": neg}
                for field in ("net", "gross", "fee", "slippage"):
                    record["independent_account_descriptive_" + field + "_sum"] = sum((c["year_" + field + "_delta"] for c in cs), Fraction()) if cs else None
                years.append(record)
    pairs = []
    for year in Y:
        for view in V:
            for left, right in PAIRS:
                a = {c["symbol"]: c for c in cells if (c["raw_year"], c["strategy"], c["view"]) == (str(year), left, view) and c["observations"]}
                b = {c["symbol"]: c for c in cells if (c["raw_year"], c["strategy"], c["view"]) == (str(year), right, view) and c["observations"]}
                differences = [a[k]["year_net_delta"] - b[k]["year_net_delta"] for k in a]
                total, median, low, high, pos, zero, neg = summary(differences)
                rows = [r for r in daily if r["strategy"] == left and int(r["raw_year"]) == year and
                        (view == "ALL" or int(r["j"]) >= 60)]
                record = {"raw_year": str(year), "view": view, "strategy_a": left, "strategy_b": right,
                          "status": "MATCHED" if rows else "PRIMARY_EMPTY" if view == "PRIMARY" else "NO_ROWS",
                          "matched_contract_years": len(a), "distinct_contracts": len(a),
                          "matched_observations": len(rows), "unmatched_observations": 0,
                          "raw_dates": len({r["raw_date"] for r in rows}), "contract_year_difference_min": low,
                          "contract_year_difference_median": median, "contract_year_difference_max": high,
                          "positive_contract_years": pos, "zero_contract_years": zero, "negative_contract_years": neg}
                for field in ("net", "gross", "fee", "slippage"):
                    record["independent_account_descriptive_" + field + "_difference_sum"] = sum((a[k]["year_" + field + "_delta"] - b[k]["year_" + field + "_delta"] for k in a), Fraction()) if a else None
                pairs.append(record)
    return cells, years, pairs


@pytest.fixture
def source_repo(tmp_path):
    repo = tmp_path / "SYNTHETIC-source"
    # The production authority's relative directory is deliberately long. Use
    # Windows extended paths for the temporary fixture, without changing OS policy.
    if sys.platform == "win32":
        repo = Path("\\\\?\\" + str(repo.resolve()))
    goal = repo / report.GOAL
    daily = synthetic_daily()
    cy, ys, ps = synthetic_chronological_tables(daily)
    universe = [{"symbol": f"SYNTHETIC.rb{i}", "contract_instance_id": f"SYNTHETIC.rb{i}|SYNTHETIC"} for i in range(4)]
    primary_count = sum(int(r["j"]) >= 60 for r in daily)
    paired_count = sum(r["matched_observations"] for r in ps)
    counts = {"rb_declarations": 4, "rb_bars": sum(COUNTS), "rb_distinct_raw_dates": max(COUNTS), "child_calls": 12,
              "daily_rows_ALL": len(daily), "primary_rows_j_ge_60": primary_count, "contract_year_cells": len(cy),
              "year_summary_rows": len(ys), "paired_summary_rows": len(ps), "paired_observation_rows": paired_count,
              "short_contracts": {}, "pair_classifications": {"MATCHED": paired_count, "MISSING_A": 0, "MISSING_B": 0,
               "IDENTITY_MISMATCH": 0, "NONFINITE_OR_INVALID_AMOUNT": 0, "SUPPORT_MISMATCH": 0}}
    output_counts = {k: counts[k] for k in ("child_calls", "contract_year_cells", "year_summary_rows", "paired_summary_rows", "paired_observation_rows")}
    output_counts.update(daily_rows=len(daily), primary_rows=primary_count)
    safety = {"evidence_level": "SCENARIO", "enable_trading": False, "broker": "NOT_RUN", "historical_strict_execution": False,
              "real_order_send_calls": 0, "real_order_cancel_calls": 0, "data_kind": "SYNTHETIC"}
    config = {"initial_cash": "1000000", "lots": 1, "max_margin_ratio": "0.4", "margin_rate": "0.12",
              "fee_per_lot": "0.01", "fee_rate": "0.0001", "multiplier": "10", "price_tick": "1", "slippage_ticks": "1"}
    precommit = {**safety, "universe": universe, "expected_counts": output_counts,
                 "chronology": {"raw_years": list(Y), "primary_mask": "zero_based_observation_index >= 60",
                                "year_status": {str(y): "PARTIAL_INPUT_WINDOW" if y in (2020, 2026) else "OBSERVED_RAW_YEAR" for y in Y}}}
    coverage = {**safety, "expected_counts": output_counts, "rb_declarations": 4, "rb_bars": sum(COUNTS),
                "rb_distinct_raw_dates": max(COUNTS), "rb_raw_ts_min": min(r["raw_ts"] for r in daily),
                "rb_raw_ts_max": max(r["raw_ts"] for r in daily)}
    review = repo / "accepted-SYNTHETIC-review"
    chronological = review / "work/replay-r3"
    pins = {}
    for name, rows in (("daily_attribution.csv", daily), ("contract_year.csv", cy), ("year_summary.csv", ys), ("paired_summary.csv", ps)):
        pins[name] = put_csv(chronological / name, rows)["sha256"]
    for name, value in (("base_config.json", config), ("chronology-precommit.json", precommit), ("coverage.json", coverage)):
        pins[name] = put_json(chronological / name, value)["sha256"]
    manifest_pin = put_json(chronological / "manifest.json", {**safety, "content_hashes": pins, "actual_counts": output_counts, "expected_counts": output_counts})
    reproduction = "SYNTHETIC manifest " + manifest_pin["sha256"]
    verdict = {"verdict": "PASS", "conditional": False, "basis": {"independent_fresh_replay": {"output_root": "work/replay-r3"}, "reproduction": reproduction}}
    verdict_pin = put_json(review / "VERDICT.json", verdict)
    put_json(goal / "CHRONOLOGICAL-ACCEPTANCE.json", {"independent_verdict": "PASS", "conditional": False,
         "published_evidence": {"VERDICT.json": {"source": {"path": str(review / "VERDICT.json"), **verdict_pin}}},
         "validation": {"independent_fresh_replay": {"output_root": "work/replay-r3"}, "reproduction": reproduction, "counts": counts}})

    comparison, capital = [], []
    for identity in universe:
        for st in S:
            rows = [r for r in daily if r["symbol"] == identity["symbol"] and r["strategy"] == st]
            net = int(rows[-1]["net_pnl"])
            common = {**identity, "strategy": st, "scenario": "BASE", "evidence_level": "SCENARIO",
                      "coverage_status": "MEETS_60_OBSERVATIONS", "observations": len(rows),
                      "first_raw_ts": rows[0]["raw_ts"], "last_raw_ts": rows[-1]["raw_ts"], "initial_cash": "1000000",
                      "ending_equity": str(INITIAL + net), "gross_pnl": str(net + 1), "fees": "1", "slippage": "0",
                      "net_pnl": str(net), "fills": 1, "closed_trades": 1, "opening_blocks": 0}
            comparison.append(common)
            for cash in (5000, 15000, 1000000):
                for margin, rate in (("BASE", "0.12"), ("MARGIN_X1_5", "0.18")):
                    n = 0 if cash == 5000 else -3 if cash == 15000 else net
                    blocks, fills = (2, 0) if cash == 5000 else (3, 1) if cash == 15000 else (0, 1)
                    fee = int(fills > 0)
                    capital.append({**common, "capital_id": "C" + str(cash), "scenario": margin, "initial_cash": str(cash),
                                    "margin_rate": rate, "max_margin_ratio": "0.4", "ending_equity": str(cash + n),
                                    "gross_pnl": str(n + fee), "fees": str(fee), "net_pnl": str(n), "opening_blocks": blocks,
                                    "opening_fills": fills, "opening_attempts": blocks + fills, "opening_margin_blocked": blocks,
                                    "opening_costs_exhaust_equity": 0, "closed_trades": fills})
    scenario_rel = Path("data/research_runs/rb-daily-scenario-compare-SYNTHETIC/comparison.csv")
    capital_rel = Path("data/research_runs/rb-daily-capital-stress-SYNTHETIC/comparison.csv")
    source_pins = [{"path": p.as_posix(), **put_csv(repo / p, rows)} for p, rows in ((scenario_rel, comparison), (capital_rel, capital))]
    groups = []
    for st in S:
        total, median, low, high, positive, zero, negative = summary([Fraction(r["net_pnl"]) for r in comparison if r["strategy"] == st])
        groups.append({"strategy": st, "cells": 4, "net_sum": ftext(total), "net_median": ftext(median),
                       "positive": positive, "zero": zero, "negative": negative, "closed_trades": 4})
    put_json(goal / "STRATEGY-RESULTS-CHECK.json", {"source_pins": source_pins, "groups": groups,
             "filter": {"capital_stress": {"strategy": S[0]}}, "capital_stress": {"cells": 4, "initial_cash": "15000",
             "margin_rate": "0.18", "net_sum": "-12", "opening_blocks": 12}, "C5000_opening_fills": 0})
    causal_root = repo / "data/research_runs/rb-daily-causal-regime-SYNTHETIC"
    (goal / "STRATEGY-RESULTS.md").write_text("SYNTHETIC\n[因果行情归因](../../data/research_runs/rb-daily-causal-regime-SYNTHETIC/report.md)\n", encoding="utf-8")
    causal_daily = []
    for row in daily:
        j = int(row["j"])
        causal_daily.append({**row, "observation_index": row["j"], "primary": row["primary_support"],
                             "regime": "UNKNOWN" if j < 21 else "RANGE" if j < 59 else "TREND_UP" if j == 59 else "MIXED",
                             "active_observation": "true", "fill_legs": row["fills_at_j"],
                             "terminal_close_legs": row["terminal_close_fills"], "terminal_close_fees": row["fee_delta"],
                             "terminal_close_slippage": "0"})
    regime_summary = []
    for identity in universe:
        for st in S:
            for view in V:
                for regime in R:
                    rows = [r for r in causal_daily if r["symbol"] == identity["symbol"] and r["strategy"] == st and r["regime"] == regime and (view == "ALL" or int(r["j"]) >= 60)]
                    record = {**identity, "strategy": st, "window": view, "regime": regime,
                              "statistical_qualification": "NOT_ASSESSED", "observation_count": len(rows), "raw_date_count": len(rows),
                              "active_observations": len(rows), "fill_legs": sum(int(r["fill_legs"]) for r in rows),
                              "terminal_observations": sum(r["terminal"] == "true" for r in rows),
                              "terminal_close_legs": sum(int(r["terminal_close_legs"]) for r in rows)}
                    for source, field in (("net_delta", "net_pnl"), ("gross_delta", "gross_pnl"), ("fee_delta", "fees"),
                                          ("slippage_delta", "slippage"), ("terminal_close_fees", "terminal_close_fees"),
                                          ("terminal_close_slippage", "terminal_close_slippage")):
                        record[field] = sum(Fraction(r[source]) for r in rows)
                    regime_summary.append(record)
    causal_pins = {n: put_csv(causal_root / n, rows)["sha256"] for n, rows in (("daily_attribution.csv", causal_daily), ("regime_summary.csv", regime_summary))}
    old_schema_safety = {k: v for k, v in safety.items() if k != "data_kind"}
    manifest_pin = put_json(causal_root / "manifest.json", {**old_schema_safety, "content_hashes": causal_pins})
    put_json(repo / report.CAUSAL_REVIEW / "PIN-VERIFICATION.json", {"checks": {"owner_result_manifest/owner": {"ok": True, "expected": manifest_pin, "actual": manifest_pin}}})
    put_json(repo / report.CAUSAL_REVIEW / "VERDICT.json", {"verdict": "PASS", "unconditional": True,
             "required_command_results": {"real_51_account_review_run": {"output_root": causal_root.relative_to(repo).as_posix(),
              "compact_return": {"accounts": 12, "attribution_rows": len(causal_daily), "primary_attribution_rows": primary_count,
                                 "regime_summary_rows": len(regime_summary)}}}})
    yield repo
    # Clean our own explicitly bounded SYNTHETIC fixture with the same extended
    # path that created it, before the outer offline runner cleans its temp root.
    expected_parent = Path("\\\\?\\" + str(tmp_path.resolve())) if sys.platform == "win32" else tmp_path.resolve()
    assert repo.resolve().parent == expected_parent
    shutil.rmtree(repo)


def run_cli(repo, output):
    return subprocess.run([sys.executable, str(TOOL), "--source-repo", str(repo), "--output-dir", str(output)],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60)


def csv_rows(output, name):
    return list(csv.DictReader((output / name).open(encoding="utf-8", newline="")))


def test_odd_even_zero_negative_and_empty_medians():
    assert report.distribution([Decimal(-5), Decimal(0), Decimal(3)])["median"] == 0
    assert report.distribution([Decimal(-5), Decimal(0)])["median"] == Decimal("-2.5")
    assert report.distribution([])["net_sum"] is None
    assert report.distribution([], empty_sum=Decimal(0))["net_sum"] == 0
    assert report.distribution([Decimal(0)]) == {"n": 1, "net_sum": Decimal(0), "median": Decimal(0),
                                                "min": Decimal(0), "max": Decimal(0), "profit": 0, "zero": 1, "loss": 0}
    assert report.text(Decimal("-0.000")) == "0"
    assert report.text(Decimal("100.1000")) == "100.1"


def test_primary_60_61_cross_year_and_terminal_original_year():
    accounts = report.accounts_from_daily(synthetic_daily(), Decimal(INITIAL))
    short = accounts[("SYNTHETIC.rb0", "SYNTHETIC.rb0|SYNTHETIC", S[0])]
    long = accounts[("SYNTHETIC.rb1", "SYNTHETIC.rb1|SYNTHETIC", S[0])]
    assert not any(r["primary"] for r in short)
    assert sum(r["primary"] for r in long) == 1
    assert (long[59]["raw_year"], long[60]["raw_year"]) == (2020, 2021)
    assert long[60]["previous_equity"] == Decimal("1000110")
    assert long[60]["net_delta"] == 1
    assert short[59]["raw_year"] == 2020 and short[59]["fee_delta"] == 1


@pytest.mark.parametrize("defect", ["missing", "duplicate", "identity", "nonfinite", "support", "year", "reset"])
def test_invalid_daily_is_rejected(defect):
    rows = synthetic_daily()
    if defect == "missing":
        rows.pop(60)
    elif defect == "duplicate":
        rows.insert(1, dict(rows[0]))
    elif defect == "identity":
        rows[0]["contract_instance_id"] += "-changed"
    elif defect == "nonfinite":
        rows[0]["net_delta"] = "NaN"
    elif defect == "support":
        rows[0]["primary_support"] = "true"
    elif defect == "year":
        rows[0]["raw_year"] = "2021"
    else:
        target = next(r for r in rows if r["symbol"] == "SYNTHETIC.rb1" and r["strategy"] == S[0] and r["j"] == "60")
        target["previous_equity"] = "1000000"
    with pytest.raises(report.ValidationError):
        report.accounts_from_daily(rows, Decimal(INITIAL))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "", " 0", 1.5])
def test_nonfinite_empty_and_float_amounts_rejected(value):
    with pytest.raises(report.ValidationError):
        report.amount(value)


def test_synthetic_cli_reconciles_every_family_and_pair_counterexample(source_repo, tmp_path):
    output = tmp_path / "complete"
    result = run_cli(source_repo, output)
    assert result.returncode == 0, result.stderr
    all_years = csv_rows(output, "year-all.csv")
    pairs = csv_rows(output, "paired-years.csv")
    assert len(all_years) == 21 and len(pairs) == 42
    pair = next(r for r in pairs if (r["raw_year"], r["view"], r["strategy_a"], r["strategy_b"]) == ("2020", "ALL", S[0], S[1]))
    # Differences are 0,99,99,2; their median is 50.5, not 110.5-11.5=99.
    assert (pair["net_sum"], pair["median"], pair["n"]) == ("200", "50.5", "4")
    a = next(r for r in all_years if r["raw_year"] == "2020" and r["strategy"] == S[0])
    b = next(r for r in all_years if r["raw_year"] == "2020" and r["strategy"] == S[1])
    assert Decimal(a["median"]) - Decimal(b["median"]) == 99
    empty = next(r for r in all_years if r["raw_year"] == "2022" and r["strategy"] == S[0])
    assert empty["n"] == "0" and empty["net_sum"] == "" and empty["zero"] == "0"
    regimes = csv_rows(output, "causal-regime.csv")
    assert len(regimes) == 36
    flat = next(r for r in regimes if r["regime"] == "FLAT")
    assert (flat["net_sum"], flat["n"], flat["median"], flat["status"]) == ("0", "0", "", "INSUFFICIENT")
    assert sum(Decimal(r["net_sum"]) for r in regimes if r["window"] == "PRIMARY" and r["strategy"] == S[0]) == 3
    capital = csv_rows(output, "capital-stress.csv")
    assert len(capital) == 18
    assert all(r["opening_fills"] == "0" and r["net_sum"] == "0" and r["n"] == "4" for r in capital if r["capital_id"] == "C5000")
    check = json.loads((output / "CHECK.json").read_text(encoding="utf-8"))
    assert check["status"] == "PENDING_TERMINAL_SEAL" and check["command"] is None
    assert "SYNTHETIC" in (output / report.REPORT).read_text(encoding="utf-8")
    for ref in check["report_value_refs"]:
        assert csv_rows(output, ref["csv"])[ref["row"] - 2][ref["column"]] == ref["value"]


def test_different_output_directories_are_deterministic_and_success_can_seal(source_repo, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    results = [run_cli(source_repo, out) for out in (first, second)]
    assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
    for filename in (*report.CSV_NAMES, report.REPORT):
        assert (first / filename).read_bytes() == (second / filename).read_bytes()
        assert b"\r" not in (first / filename).read_bytes()
    command = {"argv": results[0].args, "cwd": str(ROOT), "raw_exit": results[0].returncode,
               "source_code_commit": "0" * 40}
    sealed = report.seal_check(first, command)
    assert sealed["command"]["raw_exit"] == 0 and sealed["status"] == "SEALED_CHILD_EXIT_0"
    with pytest.raises(report.ValidationError, match="awaiting seal"):
        report.seal_check(first, command)


@pytest.mark.parametrize("defect", ["hash", "path", "duplicate_json"])
def test_source_integrity_failure_exits_two_without_report(source_repo, tmp_path, defect):
    if defect == "hash":
        file = source_repo / "accepted-SYNTHETIC-review/work/replay-r3/daily_attribution.csv"
        file.write_bytes(file.read_bytes() + b"\n")
    elif defect == "path":
        file = source_repo / report.GOAL / "STRATEGY-RESULTS-CHECK.json"
        value = json.loads(file.read_text())
        value["source_pins"][0]["path"] = "../data/research_runs/rb-daily-scenario-compare-SYNTHETIC/comparison.csv"
        put_json(file, value)
    else:
        file = source_repo / report.GOAL / "CHRONOLOGICAL-ACCEPTANCE.json"
        file.write_bytes(b'{"independent_verdict":"PASS","independent_verdict":"FAIL"}')
    output = tmp_path / "must-not-publish"
    result = run_cli(source_repo, output)
    assert result.returncode == 2 and not output.exists()
    assert json.loads(result.stderr)["status"] == "VALIDATION_FAILED"


def test_nonzero_child_cannot_seal(source_repo, tmp_path):
    output = tmp_path / "nonzero"
    result = run_cli(source_repo, output)
    assert result.returncode == 0, result.stderr
    before = (output / "CHECK.json").read_bytes()
    with pytest.raises(report.ValidationError, match="nonzero"):
        report.seal_check(output, {"argv": result.args, "cwd": str(ROOT), "raw_exit": 2})
    assert (output / "CHECK.json").read_bytes() == before


def test_artifact_tamper_cannot_seal(source_repo, tmp_path):
    output = tmp_path / "tampered-output"
    result = run_cli(source_repo, output)
    assert result.returncode == 0, result.stderr
    with (output / "year-all.csv").open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(report.ValidationError, match="mismatch"):
        report.seal_check(output, {"argv": result.args, "cwd": str(ROOT), "raw_exit": 0, "source_code_commit": "0" * 40})


def test_existing_output_never_overwritten(source_repo, tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    (output / "keep").write_text("SYNTHETIC")
    result = run_cli(source_repo, output)
    assert result.returncode == 2 and (output / "keep").read_text() == "SYNTHETIC"


def test_precision_loss_is_not_tolerated():
    with localcontext() as ctx:
        ctx.prec = 100
        ctx.traps[Inexact] = True
        with pytest.raises(Inexact):
            report.distribution([Decimal("1e100"), Decimal(1)])
