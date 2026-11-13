"""Fixed, offline K40-B research matrix over explicitly accepted frozen bytes.

The CLI performs standard-library preflight before importing the frozen engine.
It never uses the current checkout's trading package. A separate ``seal_check``
call consumes the terminated matrix receipt; it does not run the matrix again.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import io
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from dataclasses import asdict, fields
from datetime import datetime, timedelta
from decimal import Decimal, Inexact, localcontext
from pathlib import Path
from types import MappingProxyType, SimpleNamespace


STRATEGIES = ("donchian_trend", "ma_trend", "zscore_reversion")
CONTROLS = ("flat_control", "buy_hold_control")
METHODS = STRATEGIES + CONTROLS
CAPITALS = ("1000000", "300000", "100000", "50000")
SCENARIOS = ("BASE", "FEE_X2", "SLIPPAGE_X2", "MARGIN_X1_5")
VIEWS = ("ALL", "PRIMARY")
YEARS = tuple(range(2020, 2027))
BASE = {"initial_cash": "1000000", "lots": 1, "multiplier": "10", "price_tick": "1",
        "fee_per_lot": "0.01", "fee_rate": "0.0001", "slippage_ticks": "1",
        "margin_rate": "0.12", "max_margin_ratio": "0.4"}
PARAMETERS = ({"entry_window": 40, "exit_window": 20}, {"fast_window": 20, "slow_window": 60},
              {"lookback": 20, "entry_z": 2.0, "exit_z": 0.5, "stop_z": 4.0, "max_signal_age": 20})
AUTHORITY_PINS = {'PLAN-F01-20261015-r2.md': 'a1c51e2bf8b94b6a5e4859c9e74e101ac1aa2669e2f1d207b23a1c7cd6b042e7', 'INPUTS.json': '9d331675c899e7b04e915e6731ebef694741491d2c0460b3dbfee180ecc16a70', 'CLARIFICATION-IMPLEMENTATION-20261015-r3.md': '0ec3ddb50edd712f0629ff472062e4a49c3340b3cac60a9aa192bb0b3098ec8c', 'DISPOSITION-IMPLEMENTATION-20261015-r3.md': '7421a9b15d7bfbdf2a9cb468635e6025595a2d07dbe3d37892a71621bc014507'}
PRIOR_VERDICT_PIN = "28faa33c1259ac5304834a76e393c9cd8606c611db58e37fb42f5f25fb05ade2"
PRIOR_VERDICT_PATH = "C:/AKH71113/k40-a/review/hard-review-r1/VERDICT.json"
OLD_WRAPPER_PIN = "f1c0a416383fa07313d03d01abd6a608c6d52fadd3e289700265ed7cc8d2e950"
GOAL = Path("docs/development-goal-20261026")
REPORT = "v1-daily-baseline-rb.md"
PUBLISHED = ("sensitivity-summary.csv", "sensitivity-year.csv", "control-comparison.csv",
             "b-conclusions.csv", "b-scope.csv")
ZERO = Decimal(0)
EVENTS = ("opening_attempts", "opening_fills", "opening_blocks", "opening_margin_blocked",
          "opening_costs_exhaust_equity", "margin_breach_observations", "nonpositive_equity_observations",
          "terminal_close_fills", "fills")
ACCOUNT_COUNTS = ("opening_accounts_in_view", "no_new_open_accounts_in_view", "blocked_accounts_in_view",
                  "never_opened_accounts", "held_at_view_start_accounts")
MONEY = ("gross", "fee", "slippage", "net", "terminal_close_fee", "terminal_close_slippage")
SAFETY = {"enable_trading": False, "observer": "hold", "broker": "NOT_RUN",
          "real_order_send_calls": 0, "real_order_cancel_calls": 0}


class ValidationError(ValueError):
    pass


def need(ok, reason):
    if not ok:
        raise ValidationError(reason)


def eq(actual, expected, name):
    need(actual == expected, f"{name}: expected={expected!s}; actual={actual!s}")


def path(value):
    need(isinstance(value, (str, Path)), "path must be string or Path")
    p = Path(value)
    need(not any(x.lower().startswith(".env") or "holdout" in x.lower() for x in p.parts), "forbidden path")
    need(".." not in p.parts, "path traversal rejected")
    return p.resolve()


def child(root, name):
    p = Path(name)
    need(not p.is_absolute() and not p.drive and ".." not in p.parts, "unsafe relative source")
    result = path(root / p)
    need(result.is_relative_to(root), "source escaped its root")
    return result


def pin(raw):
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def unique(items):
    result = {}
    for k, v in items:
        need(k not in result, "duplicate JSON key: " + k)
        result[k] = v
    return result


def loads(raw):
    def reject(value):
        raise ValidationError("nonfinite JSON: " + value)
    return json.loads(raw, object_pairs_hook=unique, parse_constant=reject)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")


def d(value):
    need(isinstance(value, (str, int, Decimal)) and not isinstance(value, bool), "amount is not exact decimal input")
    need(str(value) and str(value).strip() == str(value), "empty/whitespace amount")
    result = Decimal(value)
    need(result.is_finite(), "nonfinite amount")
    return result


def integer(value):
    need(not isinstance(value, bool) and re.fullmatch(r"-?(?:0|[1-9][0-9]*)", str(value)) is not None, "invalid integer")
    return int(value)


def cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        if not value:
            return "0"
        s = format(value, "f")
        return s.rstrip("0").rstrip(".") if "." in s else s
    return str(value)


def csv_bytes(rows, columns=None):
    columns = columns or list(rows[0])
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows([cell(row[name]) for name in columns] for row in rows)
    return out.getvalue().encode("utf-8")


def csv_load(raw):
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""))
    need(reader.fieldnames and len(set(reader.fieldnames)) == len(reader.fieldnames), "duplicate/missing CSV header")
    rows = list(reader)
    need(all(None not in r and None not in r.values() for r in rows), "ragged CSV")
    return rows


def distribution(values):
    values = sorted(values)
    n = len(values)
    return {"n": n, "net_sum": sum(values, ZERO) if n else None,
            "net_median": (values[(n - 1) // 2] + values[n // 2]) / 2 if n else None,
            "net_min": values[0] if n else None, "net_max": values[-1] if n else None,
            "profit": sum(x > 0 for x in values), "zero": sum(x == 0 for x in values), "loss": sum(x < 0 for x in values)}


def write_new(file, raw):
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("xb") as stream:
        stream.write(raw)
    eq(pin(file.read_bytes()), pin(raw), "written payload " + file.name)


def atomic_new(file, payload):
    raw = json_bytes(payload)
    need(not file.exists(), "refusing to replace " + str(file))
    fd, temporary = tempfile.mkstemp(prefix="." + file.name + "-", dir=file.parent)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
    Path(temporary).rename(file)


def fixed_config():
    return {"schema": "aktr-rb-baseline-sensitivity-config/v1", "capital_order": list(CAPITALS),
            "scenario_order": list(SCENARIOS), "method_order": list(METHODS), "base_assumptions": BASE,
            "strategy_parameters": dict(zip(STRATEGIES, PARAMETERS)), "raw_years": list(YEARS),
            "views": list(VIEWS), "primary_j_min": 60, "product": "SHFE.RB", "symbol_regex": r"^SHFE\.rb[0-9]{4}$",
            "original_authority_pins": AUTHORITY_PINS, "prior_report_commit": "a80db5c",
            "prior_verdict": {"path": PRIOR_VERDICT_PATH, "sha256": PRIOR_VERDICT_PIN},
            "scope": "same accepted universe; fixed single-factor stresses; no optimization", "safety": SAFETY}


def matrix_configs(config):
    eq(config, fixed_config(), "fixed B research configuration")
    for capital in CAPITALS:
        for scenario in SCENARIOS:
            values = dict(BASE, initial_cash=capital)
            if scenario == "FEE_X2":
                values.update(fee_per_lot="0.02", fee_rate="0.0002")
            elif scenario == "SLIPPAGE_X2":
                values["slippage_ticks"] = "2"
            elif scenario == "MARGIN_X1_5":
                values["margin_rate"] = "0.18"
            yield capital, scenario, values


class Pins:
    def __init__(self):
        self.records, self.locations = {}, {}

    def read(self, logical, physical, expected=None):
        p = path(physical)
        need(p.is_file(), "missing required source: " + str(p))
        raw = p.read_bytes()
        actual = pin(raw)
        if expected is not None:
            expected = {"sha256": expected} if isinstance(expected, str) else expected
            for key in ("sha256", "bytes"):
                if key in expected:
                    eq(actual[key], expected[key], logical + "." + key)
        need(logical not in self.records, "duplicate logical pin")
        self.records[logical] = {**actual, "verification": "accepted_pin" if expected else "authority_record_current_bytes"}
        self.locations[logical] = str(p)
        return raw

    def verify(self):
        for logical, record in self.records.items():
            eq(pin(Path(self.locations[logical]).read_bytes()), {k: record[k] for k in ("bytes", "sha256")},
               "source changed: " + logical)


def accepted_manifest_pin(text):
    hashes = re.findall(r"\bmanifest ([0-9a-f]{64})(?![0-9a-f])", text)
    need(len(hashes) == 1, "one accepted manifest pin required")
    return hashes[0]


def preflight(source_repo, authority_dir, engine_root, config_file, output_dir, *, sealing=False):
    repo, authority, engine, config_path, output = map(path, (source_repo, authority_dir, engine_root, config_file, output_dir))
    need(sealing or not output.exists(), "output directory already exists")
    pins = Pins()
    config = loads(pins.read("B.config", config_path))
    list(matrix_configs(config))
    pins.read("B.driver", Path(__file__))
    acceptance = loads(pins.read("chronological.acceptance", repo / GOAL / "CHRONOLOGICAL-ACCEPTANCE.json"))
    need(acceptance["independent_verdict"] == "PASS" and acceptance["conditional"] is False, "chronological acceptance not PASS")
    parsed = {}
    for name in ("VERDICT.json", "ROOT-COMMAND.json"):
        source = acceptance["published_evidence"][name]["source"]
        parsed[name] = loads(pins.read("chronological." + name, source["path"], source))
    verdict, command = parsed["VERDICT.json"], parsed["ROOT-COMMAND.json"]
    need(verdict["verdict"] == "PASS" and verdict["conditional"] is False, "chronological verdict not PASS")
    argv = command["argv"]
    bindings = [argv[i + 1].rsplit("=", 1) for i, x in enumerate(argv[:-1]) if x == "--bind-file"]
    staging = [(p, h) for p, h in bindings if Path(p).name == "STAGING.json"]
    need(len(staging) == 1, "one accepted STAGING binding required")
    staging_doc = loads(pins.read("chronological.STAGING", *staging[0]))
    eq(path(command["cwd"]), path(staging_doc["candidate"]), "accepted engine identity")
    # Explicit relocation is allowed only when every original engine pin matches.
    for filename, expected in AUTHORITY_PINS.items():
        raw = pins.read("authority." + filename, authority / filename, expected)
        if filename == "INPUTS.json":
            inputs = loads(raw)
    eq(inputs["schema"], "rb-daily-chronological-transfer-inputs/v1", "INPUTS schema")
    engine_hashes = inputs["engine_interface"]["legacy_code_hashes"]
    eq(len(engine_hashes), 10, "original engine inventory count")
    for relative, expected in engine_hashes.items():
        pins.read("engine." + relative, child(engine, relative), expected)
    wrapper = [(p, h) for p, h in bindings if Path(p).name == "historical_daily_chronological_transfer.py"]
    need(len(wrapper) == 1, "accepted wrapper reference required")
    eq(wrapper[0][1], OLD_WRAPPER_PIN, "original wrapper pin")
    pins.read("chronological.wrapper_reference", wrapper[0][0], wrapper[0][1])
    fixture = [(p, h) for p, h in bindings if Path(p).name == "base.json"]
    need(len(fixture) == 1, "one accepted BASE fixture required")
    eq(loads(pins.read("chronological.BASE", *fixture[0])), BASE, "accepted BASE assumptions")
    output_ref = verdict["basis"]["independent_fresh_replay"]["output_root"]
    eq(output_ref, acceptance["validation"]["independent_fresh_replay"]["output_root"], "accepted output root")
    original = child(path(acceptance["published_evidence"]["VERDICT.json"]["source"]["path"]).parent, output_ref)
    manifest_hash = accepted_manifest_pin(acceptance["validation"]["reproduction"])
    eq(manifest_hash, accepted_manifest_pin(verdict["basis"]["reproduction"]), "accepted manifest references")
    manifest = loads(pins.read("chronological.manifest", original / "manifest.json", manifest_hash))
    coverage = loads(pins.read("chronological.coverage", original / "coverage.json", manifest["content_hashes"]["coverage.json"]))
    bundle_root = path(inputs["bundle"]["path"])
    input_manifest = loads(pins.read("input.manifest", bundle_root / "manifest.json", inputs["bundle"]["manifest_sha256"]))
    eq(inputs["bundle"]["manifest_sha256"], manifest["input_manifest_sha256"], "accepted input manifest")
    eq(input_manifest["content_hashes"], inputs["bundle"]["content_hashes"], "input content hashes")
    eq(input_manifest["content_hashes"], manifest["input_content_hashes"], "accepted input payloads")
    eq(set(input_manifest["content_hashes"]), {"bars.csv", "contracts.json", "metadata.json", "availability.json"}, "four input payloads")
    for name, expected in input_manifest["content_hashes"].items():
        pins.read("input." + name, bundle_root / name, expected)
    prior_verdict = loads(pins.read("A.verdict", PRIOR_VERDICT_PATH, PRIOR_VERDICT_PIN))
    need(prior_verdict["verdict"] == "PASS" and prior_verdict["unconditional"] is True and prior_verdict["conditional"] is False,
         "A independent verdict not unconditional PASS")
    freeze_ref = prior_verdict["frozen_owner_manifest"]
    freeze = loads(pins.read("A.freeze", freeze_ref["path"], freeze_ref))
    prior = {}
    for relative, expected in prior_verdict["reviewed_files"].items():
        eq(freeze["files"][relative], expected, "A reviewed freeze member")
        raw = pins.read("A." + relative, child(path(freeze_ref["path"]).parent / "files", relative), expected)
        if relative.startswith("docs/research/"):
            prior[Path(relative).name] = raw
    for forbidden in (repo, authority, engine, bundle_root, original, Path(__file__).resolve().parents[2],
                      path(freeze_ref["path"]).parent, path(PRIOR_VERDICT_PATH).parent):
        need(not output.is_relative_to(forbidden) and not forbidden.is_relative_to(output), "output overlaps protected source/code/evidence")
    pins.verify()
    return SimpleNamespace(pins=pins, config=config, engine_root=engine, engine_hashes=engine_hashes,
                           bundle_root=bundle_root, input_manifest=input_manifest, inputs=inputs,
                           manifest=manifest, coverage=coverage, prior=prior, output=output,
                           accepted_counts=acceptance["validation"]["counts"])


def load_engine(engine_root, engine_hashes, cache_root):
    need(not any(n == "ak_trading" or n.startswith("ak_trading.") for n in sys.modules), "trading package was preloaded before verified engine import")
    engine_root = path(engine_root)
    for relative, expected in engine_hashes.items():
        eq(pin(child(engine_root, relative).read_bytes())["sha256"], expected, "engine before import " + relative)
    sys.dont_write_bytecode = True
    sys.pycache_prefix = str(path(cache_root) / "never-written-pycache")
    sys.path.insert(0, str(engine_root / "src"))
    engine = importlib.import_module("ak_trading.futures.scenario_backtest")
    engine_io = importlib.import_module("ak_trading.futures.io")
    strategies = importlib.import_module("ak_trading.futures.strategies")
    imported = {}
    for name, module in sorted(sys.modules.items()):
        if name == "ak_trading" or name.startswith("ak_trading."):
            actual = path(module.__file__)
            need(actual.is_relative_to(engine_root), "module escaped accepted engine: " + name)
            relative = actual.relative_to(engine_root).as_posix()
            need(relative in engine_hashes, "unbound local engine dependency: " + relative)
            eq(pin(actual.read_bytes())["sha256"], engine_hashes[relative], "actual imported source " + name)
            imported[name] = {"source": relative, "sha256": engine_hashes[relative]}
    for cls, expected in zip((strategies.DonchianTrend, strategies.MATrend, strategies.ZScoreReversion), PARAMETERS):
        eq(asdict(cls()), expected, "original fixed strategy defaults")
    return engine, engine_io, imported


def frozen(value):
    if isinstance(value, dict):
        return MappingProxyType({k: frozen(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(frozen(v) for v in value)
    return value


def contract_views(bundle, engine_io, coverage, accepted_counts):
    declarations = [r for r in bundle.contracts if r.get("product") == "SHFE.RB"]
    need(all(re.fullmatch(r"SHFE\.rb[0-9]{4}", r["raw_symbol"]) for r in declarations), "unexpected RB declaration")
    by_symbol = {}
    for declaration in declarations:
        symbol = declaration["raw_symbol"]
        need(symbol not in by_symbol, "duplicate RB symbol/instance")
        by_symbol[symbol] = declaration
    eq(len({r["instance_id"] for r in declarations}), len(declarations), "unique RB instance identities")
    expected = {(r["symbol"], r["contract_instance_id"]): r for r in coverage["contracts"]}
    eq({(r["raw_symbol"], r["instance_id"]) for r in declarations}, set(expected), "full accepted universe")
    bars_by_symbol = defaultdict(list)
    for bar in bundle.bars:
        if re.fullmatch(r"SHFE\.rb[0-9]{4}", bar.symbol):
            need(bar.symbol in by_symbol, "undeclared RB bar")
            bars_by_symbol[bar.symbol].append(bar)
    views, raw_dates = {}, set()
    for symbol, declaration in sorted(by_symbol.items()):
        bars = tuple(sorted(bars_by_symbol[symbol], key=lambda b: b.datetime_nano))
        need(bars and len({b.datetime_nano for b in bars}) == len(bars), "missing/duplicate RB observations")
        record = expected[(symbol, declaration["instance_id"])]
        for b in bars:
            need(b.ts.utcoffset() == timedelta(hours=8) and b.ts.year in YEARS, "raw timestamp outside fixed window")
            raw_dates.add(b.ts.date())
        eq(len(bars), record["observations"], "contract observations")
        eq(max(0, len(bars) - 60), record["primary_observations"], "contract PRIMARY observations")
        eq(bars[0].ts.isoformat(), record["first_raw_ts"], "contract first timestamp")
        eq(bars[-1].ts.isoformat(), record["last_raw_ts"], "contract last timestamp")
        eq({str(y): sum(b.ts.year == y for b in bars) for y in YEARS}, record["year_observations"], "contract raw years")
        views[symbol] = engine_io.InputBundle(bundle.root, frozen(dict(bundle.manifest)), bars,
                                             (frozen(dict(declaration)),), frozen(dict(bundle.metadata)), frozen(dict(bundle.availability)))
    eq(len(declarations), accepted_counts["rb_declarations"], "accepted contract count")
    eq(sum(len(v.bars) for v in views.values()), accepted_counts["rb_bars"], "accepted RB bar count")
    eq(len(raw_dates), accepted_counts["rb_distinct_raw_dates"], "accepted date count")
    return views


def native_result(result):
    return {"config": dict(result.config), "report": dict(result.report),
            "fills": [asdict(r) for r in result.fills], "equity": [asdict(r) for r in result.equity],
            "decisions": [asdict(r) for r in result.decisions]}


def native_payloads(result, engine):
    return {"config.json": json_bytes(result["config"]), "report.json": json_bytes(result["report"]),
            **{name + ".csv": csv_bytes(result[name], [f.name for f in fields(cls)]) for name, cls in
               (("fills", engine.ScenarioFill), ("equity", engine.ScenarioEquity), ("decisions", engine.ScenarioDecision))}}


def costs(price, config, lots=1):
    return (d(config["fee_per_lot"]) * lots, d(config["fee_rate"]) * price * d(config["multiplier"]) * lots,
            d(config["slippage_ticks"]) * d(config["price_tick"]) * d(config["multiplier"]) * lots)


def control_account(bars, declaration, method, config):
    """Precommitted first-open-only buy/hold, or an always-flat exact account."""
    need(method in CONTROLS and bars, "invalid control account")
    initial, multiplier = d(config["initial_cash"]), d(config["multiplier"])
    cash, fees, slip, realized, position, entry = initial, ZERO, ZERO, ZERO, 0, ZERO
    fills, equity, decisions = [], [], []
    previous_gross = ZERO
    for j, bar in enumerate(bars):
        status, margin, after = "target_unchanged", None, None
        if j == 0 and method == "buy_hold_control":
            flat, rate, charge = costs(bar.open, config)
            after = cash - flat - rate - charge
            margin = bar.open * multiplier * d(config["margin_rate"])
            status = "opening_costs_exhaust_equity" if after <= 0 else "opening_margin_blocked" \
                if margin > after * d(config["max_margin_ratio"]) else "opened"
            if status == "opened":
                cash, fees, slip, position, entry = after, flat + rate, charge, 1, bar.open
                fills.append({"fill_id": f"fill-{len(fills):06d}", "observation_index": j, "signal_index": None,
                              "datetime_nano": bar.datetime_nano, "raw_ts": bar.ts.isoformat(), "phase": "first_open",
                              "offset": "open", "side": 1, "lots": 1, "ideal_price": bar.open,
                              "effective_price": bar.open + d(config["slippage_ticks"]) * d(config["price_tick"]),
                              "fee_per_lot_component": flat, "fee_rate_component": rate, "fee": flat + rate,
                              "slippage": charge, "realized_pnl": ZERO, "cash_after": cash, "position_after": position})
        terminal = j == len(bars) - 1
        if terminal and position:
            flat, rate, charge = costs(bar.close, config)
            realized = (bar.close - entry) * multiplier
            fees += flat + rate
            slip += charge
            cash += realized - flat - rate - charge
            position = 0
            fills.append({"fill_id": f"fill-{len(fills):06d}", "observation_index": j, "signal_index": None,
                          "datetime_nano": bar.datetime_nano, "raw_ts": bar.ts.isoformat(), "phase": "terminal_close",
                          "offset": "close", "side": -1, "lots": 1, "ideal_price": bar.close,
                          "effective_price": bar.close - d(config["slippage_ticks"]) * d(config["price_tick"]),
                          "fee_per_lot_component": flat, "fee_rate_component": rate, "fee": flat + rate,
                          "slippage": charge, "realized_pnl": realized, "cash_after": cash, "position_after": 0})
        unrealized = position * multiplier * (bar.close - entry)
        gross, mark = realized + unrealized, cash + unrealized
        required = position * bar.close * multiplier * d(config["margin_rate"])
        equity.append({"observation_index": j, "datetime_nano": bar.datetime_nano, "raw_ts": bar.ts.isoformat(),
                       "close": bar.close, "position_lots": position, "cash": cash, "realized_pnl": realized,
                       "unrealized_pnl": unrealized, "equity": mark, "daily_gross_pnl": gross - previous_gross,
                       "gross_pnl": gross, "fees": fees, "slippage": slip, "net_pnl": mark - initial,
                       "required_margin": required, "available_equity": mark - required,
                       "margin_breached": bool(position and (mark <= 0 or required > mark * d(config["max_margin_ratio"]))),
                       "terminal": terminal})
        previous_gross = gross
        decisions.append({"observation_index": j, "datetime_nano": bar.datetime_nano, "raw_ts": bar.ts.isoformat(),
                          "execution_status": status, "opening_required_margin": margin, "opening_equity_after_costs": after,
                          "signal_index": None, "decision_rule": "precommitted", "candidate_direction": None,
                          "logical_direction": 1 if method == "buy_hold_control" else 0})
    return {"config": {"method": method, "assumptions": config, "rule": "precommitted_first_open_once_final_close",
                       "symbol": declaration["raw_symbol"], "contract_instance_id": declaration["instance_id"]},
            "fills": fills, "equity": equity, "decisions": decisions,
            "report": {"status": "complete", "evidence_level": "SCENARIO", "observations": len(bars),
                       "gross_pnl": cell(previous_gross), "fees": cell(fees), "slippage": cell(slip),
                       "net_pnl": cell(equity[-1]["equity"] - initial), "no_entry": not any(f["offset"] == "open" for f in fills)}}


def verify_account(bars, declaration, method, capital, scenario, config, result):
    """Reconstruct fills and marks independently of the engine/control producer."""
    eq(len(result["equity"]), len(bars), "equity support")
    eq(len(result["decisions"]), len(bars), "decision support")
    initial, multiplier = d(config["initial_cash"]), d(config["multiplier"])
    position, entry = 0, ZERO
    cash, realized, fees, slip = initial, ZERO, ZERO, ZERO
    prior_equity, prior_gross, prior_fees, prior_slip = initial, ZERO, ZERO, ZERO
    grouped, seen = defaultdict(list), set()
    for f in result["fills"]:
        need(f["fill_id"] not in seen, "duplicate fill ID")
        seen.add(f["fill_id"])
        j = integer(f["observation_index"])
        need(0 <= j < len(bars), "fill outside observed support")
        grouped[j].append(f)
    ledger = []
    allowed = {"opened", "reversed", "opening_margin_blocked", "opening_costs_exhaust_equity",
               "no_preceding_target", "target_unchanged", "closed", "flat"}
    for j, (bar, mark, decision) in enumerate(zip(bars, result["equity"], result["decisions"])):
        need(bar.ts.utcoffset() == timedelta(hours=8), "raw timestamp must be +08:00")
        for price in (bar.open, bar.high, bar.low, bar.close):
            need(d(price) > 0, "nonpositive OHLC")
        need(bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high, "invalid OHLC")
        for record in (mark, decision):
            eq(integer(record["observation_index"]), j, "unique ordered observation index")
            eq(integer(record["datetime_nano"]), bar.datetime_nano, "raw nano identity")
            eq(record["raw_ts"], bar.ts.isoformat(), "raw ts identity")
        previous_position = position
        status = decision["execution_status"]
        need(status in allowed, "unknown execution status: " + status)
        attempt = status in {"opened", "reversed", "opening_margin_blocked", "opening_costs_exhaust_equity"}
        blocked_margin, blocked_costs = int(status == "opening_margin_blocked"), int(status == "opening_costs_exhaust_equity")
        row_fills = grouped[j]
        open_fills = sum(f["offset"] == "open" for f in row_fills)
        eq(int(attempt), open_fills + blocked_margin + blocked_costs, "actual opening attempt identity")
        if method == "flat_control":
            need(not row_fills and not attempt, "flat control attempted trading")
        if method == "buy_hold_control":
            eq(int(attempt), int(j == 0), "buy/hold attempts only once at first open")
        at_attempt = None
        terminal_fees = terminal_slip = ZERO
        for f in row_fills:
            eq(integer(f["datetime_nano"]), bar.datetime_nano, "fill timestamp")
            eq(f["raw_ts"], bar.ts.isoformat(), "fill raw timestamp")
            phase, offset, side, lots = f["phase"], f["offset"], integer(f["side"]), integer(f["lots"])
            need(side in (-1, 1) and lots == 1, "fixed one-lot fill")
            if phase == "terminal_close":
                need(j == len(bars) - 1 and offset == "close" and f["signal_index"] is None, "terminal fill identity")
                ideal = bar.close
            else:
                need((method in STRATEGIES and phase == "next_open" and j > 0 and f["signal_index"] == j - 1) or
                     (method == "buy_hold_control" and phase == "first_open" and j == 0 and f["signal_index"] is None),
                     "fill did not follow its precommitted event rule")
                ideal = bar.open
            eq(d(f["ideal_price"]), ideal, "ideal fill price")
            eq(d(f["effective_price"]), ideal + side * d(config["slippage_ticks"]) * d(config["price_tick"]), "informational effective price")
            flat, rate, charge = costs(ideal, config, lots)
            for name, value in (("fee_per_lot_component", flat), ("fee_rate_component", rate), ("fee", flat + rate), ("slippage", charge)):
                eq(d(f[name]), value, "fill " + name)
            if offset == "open":
                need(position == 0, "opening into an existing position")
                at_attempt = cash
                pnl = ZERO
                position, entry = side * lots, ideal
            else:
                need(offset == "close" and position != 0 and side == -position and lots == abs(position), "invalid closing position")
                pnl = (ideal - entry) * position * multiplier
                position, entry = 0, ZERO
            realized += pnl
            fees += flat + rate
            slip += charge
            cash += pnl - flat - rate - charge
            eq(d(f["realized_pnl"]), pnl, "fill realized PnL")
            eq(d(f["cash_after"]), cash, "fill cash")
            eq(integer(f["position_after"]), position, "fill position")
            if phase == "terminal_close":
                terminal_fees += flat + rate
                terminal_slip += charge
        if attempt:
            opening_cash = at_attempt if at_attempt is not None else cash
            flat, rate, charge = costs(bar.open, config)
            after = opening_cash - flat - rate - charge
            margin = bar.open * multiplier * d(config["margin_rate"])
            eq(d(decision["opening_equity_after_costs"]), after, "attempt after-cost equity")
            eq(d(decision["opening_required_margin"]), margin, "attempt margin")
            expected_block = "opening_costs_exhaust_equity" if after <= 0 else "opening_margin_blocked" \
                if margin > after * d(config["max_margin_ratio"]) else None
            eq(status if blocked_margin or blocked_costs else None, expected_block, "admission result")
        unrealized = position * multiplier * (bar.close - entry)
        gross, equity = realized + unrealized, cash + unrealized
        required = abs(position) * bar.close * multiplier * d(config["margin_rate"])
        breached = bool(position and (equity <= 0 or required > equity * d(config["max_margin_ratio"])))
        for field, value in (("cash", cash), ("realized_pnl", realized), ("unrealized_pnl", unrealized),
                              ("equity", equity), ("gross_pnl", gross), ("fees", fees), ("slippage", slip),
                              ("net_pnl", equity - initial), ("daily_gross_pnl", gross - prior_gross),
                              ("required_margin", required), ("available_equity", equity - required)):
            eq(d(mark[field]), value, "mark " + field)
        eq(integer(mark["position_lots"]), position, "marked position")
        eq(mark["margin_breached"], breached, "close margin monitor")
        eq(mark["terminal"], j == len(bars) - 1, "terminal mark")
        eq(cash, initial + realized - fees - slip, "cash conservation")
        eq(equity - prior_equity, gross - prior_gross - (fees - prior_fees) - (slip - prior_slip), "daily equity conservation")
        ledger.append({"capital": capital, "scenario": scenario, "symbol": declaration["raw_symbol"],
                       "contract_instance_id": declaration["instance_id"], "method": method, "j": j,
                       "datetime_nano": bar.datetime_nano, "raw_ts": bar.ts.isoformat(), "raw_date": bar.ts.date().isoformat(),
                       "raw_year": bar.ts.year, "primary": j >= 60, "terminal": mark["terminal"],
                       "previous_position": previous_position, "position": position, "cash": cash,
                       "realized_pnl": realized, "unrealized_pnl": unrealized, "previous_equity": prior_equity,
                       "equity": equity, "gross": gross, "fee": fees, "slippage": slip, "net": equity - initial,
                       "gross_delta": gross - prior_gross, "fee_delta": fees - prior_fees,
                       "slippage_delta": slip - prior_slip, "net_delta": equity - prior_equity,
                       "execution_status": status, "opening_attempts": int(attempt), "opening_fills": open_fills,
                       "opening_blocks": blocked_margin + blocked_costs, "opening_margin_blocked": blocked_margin,
                       "opening_costs_exhaust_equity": blocked_costs, "margin_breach_observations": int(breached),
                       "nonpositive_equity_observations": int(equity <= 0), "terminal_close_fills": sum(f["phase"] == "terminal_close" for f in row_fills),
                       "fills": len(row_fills), "terminal_close_fee_delta": terminal_fees,
                       "terminal_close_slippage_delta": terminal_slip})
        prior_equity, prior_gross, prior_fees, prior_slip = equity, gross, fees, slip
    eq(position, 0, "terminal flat")
    for field, value in (("gross_pnl", prior_gross), ("fees", fees), ("slippage", slip), ("net_pnl", prior_equity - initial)):
        eq(d(result["report"][field]), value, "report " + field)
    return ledger


def window_cell(ledger, view, scope):
    rows = [r for r in ledger if (view == "ALL" or r["primary"]) and (scope == "ALL_YEARS" or r["raw_year"] == scope)]
    first = ledger[0]
    result = {k: first[k] for k in ("capital", "scenario", "symbol", "contract_instance_id", "method")}
    result.update(view=view, scope=str(scope), observations=len(rows), first_j=rows[0]["j"] if rows else None,
                  last_j=rows[-1]["j"] if rows else None)
    for field in MONEY:
        result[field + "_sum"] = sum((r[field + "_delta"] for r in rows), ZERO) if rows else None
    for field in EVENTS:
        result[field] = sum(r[field] for r in rows)
    result.update(opening_accounts_in_view=int(any(r["opening_fills"] for r in rows)),
                  no_new_open_accounts_in_view=int(bool(rows) and not any(r["opening_fills"] for r in rows)),
                  blocked_accounts_in_view=int(any(r["opening_blocks"] for r in rows)),
                  never_opened_accounts=int(bool(rows) and not any(r["opening_fills"] for r in ledger)),
                  held_at_view_start_accounts=int(bool(rows) and rows[0]["previous_position"] != 0),
                  support_sha256=pin(json_bytes([[r[k] for k in ("j", "datetime_nano", "raw_ts", "raw_date")] for r in rows]))["sha256"],
                  _dates=tuple(sorted({r["raw_date"] for r in rows})))
    return result


def aggregate(cells):
    cells = [c for c in cells if c["observations"]]
    result = distribution([c["net_sum"] for c in cells])
    result["observations"] = sum(c["observations"] for c in cells)
    result["raw_dates"] = len({date for c in cells for date in c["_dates"]})
    for field in MONEY:
        if field != "net":
            result[field + "_sum"] = sum((c[field + "_sum"] for c in cells), ZERO) if cells else None
    for field in (*EVENTS, *ACCOUNT_COUNTS):
        result[field] = sum(c[field] for c in cells)
    eq(result["opening_attempts"], result["opening_fills"] + result["opening_blocks"], "aggregate attempts")
    eq(result["n"], result["opening_accounts_in_view"] + result["no_new_open_accounts_in_view"], "view account partition")
    result["status"] = "OBSERVED_DESCRIPTIVE_ONLY" if cells else "INSUFFICIENT"
    return result


def paired(left, right):
    a = {c["contract_instance_id"]: c for c in left if c["observations"]}
    b = {c["contract_instance_id"]: c for c in right if c["observations"]}
    eq(len(a), sum(bool(c["observations"]) for c in left), "unique left cells")
    eq(len(b), sum(bool(c["observations"]) for c in right), "unique right cells")
    eq(set(a), set(b), "exact paired population")
    differences = []
    for key in sorted(a):
        eq(a[key]["support_sha256"], b[key]["support_sha256"], "exact paired observations")
        differences.append(a[key]["net_sum"] - b[key]["net_sum"])
    result = distribution(differences)
    result["matched_observations"] = sum(c["observations"] for c in a.values())
    for side, records in (("left", a), ("right", b)):
        for field in ("opening_fills", "opening_attempts", "opening_blocks", *ACCOUNT_COUNTS):
            result[side + "_" + field] = sum(c[field] for c in records.values())
    return result


def group_key(capital, scenario, method, view, scope="ALL_YEARS"):
    return capital, scenario, method, view, str(scope)


def check_A_anchor(groups, prior):
    for filename, view in (("year-all.csv", "ALL"), ("year-primary.csv", "PRIMARY")):
        for row in csv_load(prior[filename]):
            computed = aggregate(groups[group_key("1000000", "BASE", row["strategy"], view, row["raw_year"])])
            for name in ("n", "observations", "raw_dates", "fills", "terminal_close_fills", "profit", "zero", "loss"):
                eq(computed[name], integer(row[name]), "A annual " + name)
            for name, original in (("net_sum", "net_sum"), ("gross_sum", "gross_sum"), ("fee_sum", "fee_sum"),
                                    ("slippage_sum", "slippage_sum"), ("net_median", "median"), ("net_min", "min"), ("net_max", "max")):
                eq(computed[name], None if row[original] == "" else d(row[original]), "A annual " + original)
    for row in csv_load(prior["paired-years.csv"]):
        a = groups[group_key("1000000", "BASE", row["strategy_a"], row["view"], row["raw_year"])]
        b = groups[group_key("1000000", "BASE", row["strategy_b"], row["view"], row["raw_year"])]
        computed = paired(a, b)
        for name in ("n", "matched_observations", "profit", "zero", "loss"):
            eq(computed[name], integer(row[name]), "A paired " + name)
        for name, original in (("net_sum", "net_sum"), ("net_median", "median"), ("net_min", "min"), ("net_max", "max")):
            eq(computed[name], None if row[original] == "" else d(row[original]), "A paired " + original)


def aggregate_matrix(groups):
    summary, years, controls = [], [], []
    for capital in CAPITALS:
        for scenario in SCENARIOS:
            for method in METHODS:
                for view in VIEWS:
                    current = groups[group_key(capital, scenario, method, view)]
                    record = {"capital": capital, "scenario": scenario, "method": method, "view": view, **aggregate(current)}
                    for label, reference in (("vs_same_capital_BASE", groups[group_key(capital, "BASE", method, view)]),
                                              ("vs_same_scenario_C1000000", groups[group_key("1000000", scenario, method, view)])):
                        diff = paired(current, reference)
                        record[label + "_net_sum"] = diff["net_sum"]
                        record[label + "_net_median"] = diff["net_median"]
                    summary.append(record)
                    for year in YEARS:
                        years.append({"capital": capital, "scenario": scenario, "method": method, "view": view,
                                      "raw_year": year, "year_status": "PARTIAL_INPUT_WINDOW" if year in (2020, 2026) else "OBSERVED_RAW_YEAR",
                                      **aggregate(groups[group_key(capital, scenario, method, view, year)])})
            for strategy in STRATEGIES:
                for control in CONTROLS:
                    for view in VIEWS:
                        for scope in ("ALL_YEARS", *YEARS):
                            controls.append({"capital": capital, "scenario": scenario, "strategy": strategy,
                                             "control": control, "view": view, "scope": str(scope),
                                             **paired(groups[group_key(capital, scenario, strategy, view, scope)],
                                                      groups[group_key(capital, scenario, control, view, scope)])})
    eq((len(summary), len(years), len(controls)), (160, 1120, 1536), "full aggregate matrix")
    return summary, years, controls


def conclusions_and_scope(summary, controls, context, counts):
    old = {(r["strategy"], r["view"]): r for r in csv_load(context.prior["conclusions.csv"])}
    conclusions = []
    for method in STRATEGIES:
        selected = [r for r in summary if r["method"] == method and r["view"] == "ALL"]
        low, high = min(r["net_sum"] for r in selected), max(r["net_sum"] for r in selected)
        difference = next(r for r in controls if (r["capital"], r["scenario"], r["strategy"], r["control"], r["view"], r["scope"]) ==
                          ("1000000", "BASE", method, "buy_hold_control", "ALL", "ALL_YEARS"))
        conclusions.append({"method": method, "base_capital": "1000000", "base_scenario": "BASE",
                            "annual_denominator": integer(old[(method, "ALL")]["calendar_year_slots"]),
                            "ALL_positive_years": integer(old[(method, "ALL")]["positive_years"]),
                            "PRIMARY_positive_years": integer(old[(method, "PRIMARY")]["positive_years"]),
                            "fixed_cell_count": len(selected), "ALL_min_net": low, "ALL_max_net": high,
                            "ALL_max_opening_blocks": max(r["opening_blocks"] for r in selected),
                            "min_cells": ";".join(r["capital"] + "/" + r["scenario"] for r in selected if r["net_sum"] == low),
                            "max_cells": ";".join(r["capital"] + "/" + r["scenario"] for r in selected if r["net_sum"] == high),
                            "BASE_ALL_strategy_minus_buy_hold": difference["net_sum"],
                            "selection_rule": "all prespecified correlated capital/scenario cells; no selection by result"})
    scope = []
    def put(source, metric, value, unit=""):
        scope.append({"source": source, "metric": metric, "value": value, "unit": unit})
    for k, v in counts.items():
        put("matrix", k, v)
    for k, v in (("cash_count", len(CAPITALS)), ("scenario_count", len(SCENARIOS)), ("method_count", len(METHODS)),
                  ("cell_count_per_method", len(CAPITALS) * len(SCENARIOS)), ("primary_j_min", 60), ("lots", 1)):
        put("fixed", k, v)
    put("input", "first_date", context.coverage["rb_raw_ts_min"][:10])
    put("input", "last_date", context.coverage["rb_raw_ts_max"][:10])
    put("input", "contracts", context.accepted_counts["rb_declarations"])
    for capital, scenario, values in matrix_configs(context.config):
        for k, v in values.items():
            put(capital + "/" + scenario, k, v)
    for name, h in AUTHORITY_PINS.items():
        put("original_authority", name, h)
    for k, v in (("evidence_level", "SCENARIO"), ("qualification", "NOT_ASSESSED"),
                  ("calendar", "CALENDAR_UNVERIFIED"), ("prior_report_commit", "a80db5c"),
                  ("buy_hold", "first stored open; one admission attempt; final stored close; no forced liquidation"),
                  ("PRIMARY", "j>=60 retains previous actual state; report mask only")):
        put("limits", k, v)
    return conclusions, scope


def render_v2(tables, prior):
    names = dict(zip(METHODS, ("唐奇安", "均线", "Z 回归", "始终空仓", "买入持有")))
    refs = list(loads(prior["CHECK.json"])["report_value_refs"])
    def value(filename, selector, field):
        matches = [(i, r) for i, r in enumerate(tables[filename]) if all(r[k] == v for k, v in selector.items())]
        need(len(matches) == 1, "ambiguous report value")
        index, record = matches[0]
        text = cell(record[field])
        refs.append({"csv": filename, "row": index + 2, "column": field, "value": text})
        return text or "空"
    def con(method, field):
        return value("b-conclusions.csv", {"method": method}, field)
    def summary(method, field, capital="1000000", scenario="BASE"):
        return value("sensitivity-summary.csv", {"method": method, "capital": capital, "scenario": scenario, "view": "ALL"}, field)
    first = "C" + con(STRATEGIES[0], "base_capital") + "/BASE 的正净额年数：ALL 为" + "、".join(
        f"{names[m]} {con(m, 'ALL_positive_years')}/{con(m, 'annual_denominator')}" for m in STRATEGIES) + "；PRIMARY 为" + "、".join(
        f"{names[m]} {con(m, 'PRIMARY_positive_years')}/{con(m, 'annual_denominator')}" for m in STRATEGIES) + "。"
    second = "预定 " + con(STRATEGIES[0], "fixed_cell_count") + " 个相关资金/成本单元的 ALL 净额范围与最大开仓阻断次数分别为" + "；".join(
        f"{names[m]} {con(m, 'ALL_min_net')} 至 {con(m, 'ALL_max_net')} 元、{con(m, 'ALL_max_opening_blocks')} 次" for m in STRATEGIES) + "，这些相关单元不构成独立试验。"
    third = "C" + con(STRATEGIES[0], "base_capital") + "/BASE/ALL 中，三策略减买持的净额差为" + "、".join(
        f"{names[m]} {con(m, 'BASE_ALL_strategy_minus_buy_hold')} 元" for m in STRATEGIES) + "；仍为 SCENARIO，非样本外、历史实盘或组合收益。"
    previous_lines = prior[REPORT].decode("utf-8").splitlines()
    need(previous_lines[0] == "# RB 日线既有结果", "unrecognized accepted A report")
    preserved = "\n".join(previous_lines[5:]).replace("未运行新模拟或训练", "原 A 部分未运行新模拟或训练；本次 B 的新模拟另列")
    lines = ["# RB 日线既有结果", "", first, second, third, preserved, "", "**资金与成本敏感性：同一合约集合的新 SCENARIO 矩阵**", "",
             "下表固定展示 ALL；两口径及全部资金/成本组合的全期与分年结果见新 CSV。资金表为 BASE，每格依次为净额；中位数；开仓阻断/尝试。", "",
             "| 初始资金（元） | 唐奇安 | 均线 | Z 回归 |", "| --- | --- | --- | --- |"]
    for capital in CAPITALS:
        label = value("sensitivity-summary.csv", {"capital": capital, "scenario": "BASE", "method": STRATEGIES[0], "view": "ALL"}, "capital")
        values = [summary(m, "net_sum", capital) + "；" + summary(m, "net_median", capital) + "；" +
                  summary(m, "opening_blocks", capital) + "/" + summary(m, "opening_attempts", capital) for m in STRATEGIES]
        lines.append("| " + " | ".join([label, *values]) + " |")
    lines += ["", "成本表固定为 C" + con(STRATEGIES[0], "base_capital") + "；每格为 ALL 净额；相对同资金 BASE 的逐合约配对净额合计差。", "",
              "| 单项成本情景 | 唐奇安 | 均线 | Z 回归 |", "| --- | --- | --- | --- |"]
    for scenario in SCENARIOS:
        values = [summary(m, "net_sum", scenario=scenario) + "；" + summary(m, "vs_same_capital_BASE_net_sum", scenario=scenario) for m in STRATEGIES]
        lines.append("| " + " | ".join([scenario, *values]) + " |")
    lines += ["", "**对照：C" + con(STRATEGIES[0], "base_capital") + "/BASE/ALL**", "",
              "买持只在首根 open 按既定准入尝试一次，拒绝后不再试，成功入场则持至末根 close 并收取实际终点成本。始终空仓没有利息或成交。", "",
              "| 方法 | 净额合计 | 中位数 | 有开仓账户 | open 成交次数 | 阻断次数 | 策略减买持净额 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for method in METHODS:
        values = [summary(method, k) for k in ("net_sum", "net_median", "opening_accounts_in_view", "opening_fills", "opening_blocks")]
        difference = con(method, "BASE_ALL_strategy_minus_buy_hold") if method in STRATEGIES else "—"
        lines.append("| " + " | ".join([names[method], *values, difference]) + " |")
    n = value("b-scope.csv", {"source": "input", "metric": "contracts"}, "value")
    lots = value("b-scope.csv", {"source": "fixed", "metric": "lots"}, "value")
    lines += ["", f"本次仅对同一已观察的 {n} 个合约、固定 {lots} 手逐账户研究；初始资金独立，合约窗口重叠，合计不是组合净值。"
              "经济参数是假设，交易日历未核实，不换月；资金与单项成本加压不覆盖联合极端压力。持仓后的保证金违约和非正权益只记录，"
              "没有强平、补款或利息，结果不证明真实可执行性。PRIMARY 继承先前状态，不重新开仓；窗口内没有新增 open 不表示完整账户从未入场。"
              "A 中较早的补充资金表使用较早共同样本，与本次全合约四资金档分开。", "",
              "本次 new_simulation=true、new_training=false、holdout_read=false；所有金额仍为 SCENARIO / NOT_ASSESSED。"
              "[全期敏感性](v1-daily-baseline-rb/sensitivity-summary.csv)、[年度敏感性](v1-daily-baseline-rb/sensitivity-year.csv)、"
              "[对照配对](v1-daily-baseline-rb/control-comparison.csv)、[新结论](v1-daily-baseline-rb/b-conclusions.csv)、"
              "[矩阵范围](v1-daily-baseline-rb/b-scope.csv) 与 [CHECK](v1-daily-baseline-rb/CHECK.json) 提供完整聚合与数值映射。", ""]
    return "\n".join(lines).encode("utf-8"), refs


def write_account(output, capital, scenario, symbol, method, result, ledger, engine):
    directory = output / "accounts" / ("C" + capital) / scenario / symbol / method
    if method in STRATEGIES:
        payloads = native_payloads(result, engine)
    else:
        payloads = {"config.json": json_bytes(result["config"]), "report.json": json_bytes(result["report"]),
                    "fills.csv": csv_bytes(result["fills"], [f.name for f in fields(engine.ScenarioFill)]),
                    "equity.csv": csv_bytes(result["equity"]), "decisions.csv": csv_bytes(result["decisions"])}
    payloads["ledger.csv"] = csv_bytes(ledger)
    for name, raw in payloads.items():
        write_new(directory / name, raw)
    return {name: pin(raw)["sha256"] for name, raw in payloads.items()}


def run_matrix(context):
    output = context.output
    engine, engine_io, imported = load_engine(context.engine_root, context.engine_hashes, output)
    bundle = engine_io.read_input_bundle(context.bundle_root)
    eq(dict(bundle.manifest), context.input_manifest, "parsed accepted input manifest")
    views = contract_views(bundle, engine_io, context.coverage, context.accepted_counts)
    del bundle
    context.pins.verify()
    output.mkdir(parents=True, exist_ok=False)
    write_new(output / "runtime/source-locations.json", json_bytes(context.pins.locations))
    write_new(output / "config.json", json_bytes(context.config))
    groups, signal_pins = defaultdict(list), {}
    counts = {"accounts": 0, "strategy_accounts": 0, "control_accounts": 0,
              "ALL_observations": 0, "PRIMARY_observations": 0, "contract_year_cells": 0,
              "A_native_payloads_equal": 0}
    anchor_complete = False
    matrix_stream = (output / "contract-year.csv").open("x", encoding="utf-8", newline="")
    matrix_writer = None

    def account(capital, scenario, values, symbol, method):
        nonlocal matrix_writer
        view = views[symbol]
        declaration = dict(view.contracts[0])
        if method in STRATEGIES:
            result = native_result(engine.run_scenario_backtest(view, symbol=symbol, strategy=method,
                                                                config=engine.ScenarioConfig.from_mapping(values)))
            signature = pin(json_bytes([{k: r[k] for k in ("candidate_direction", "logical_direction", "signal_age", "strategy_status", "reason")}
                                        for r in result["decisions"]]))["sha256"]
            key = symbol, method
            if key in signal_pins:
                eq(signature, signal_pins[key], "funding/cost invariant logical signals")
            else:
                signal_pins[key] = signature
        else:
            need(anchor_complete, "controls cannot precede the completed A anchor")
            result = control_account(view.bars, declaration, method, values)
        ledger = verify_account(view.bars, declaration, method, capital, scenario, values, result)
        hashes = write_account(output, capital, scenario, symbol, method, result, ledger, engine)
        if (capital, scenario) == ("1000000", "BASE") and method in STRATEGIES:
            for filename in ("config.json", "fills.csv", "equity.csv", "decisions.csv", "report.json"):
                relative = f"children/{symbol}/{method}/{filename}"
                eq(hashes[filename], context.manifest["content_hashes"][relative], "A native payload " + relative)
                counts["A_native_payloads_equal"] += 1
        for view_name in VIEWS:
            for scope in ("ALL_YEARS", *YEARS):
                result_cell = window_cell(ledger, view_name, scope)
                groups[group_key(capital, scenario, method, view_name, scope)].append(result_cell)
                if scope != "ALL_YEARS":
                    public = {k: cell(v) for k, v in result_cell.items() if not k.startswith("_")}
                    if matrix_writer is None:
                        matrix_writer = csv.DictWriter(matrix_stream, fieldnames=list(public), lineterminator="\n")
                        matrix_writer.writeheader()
                    matrix_writer.writerow(public)
                    counts["contract_year_cells"] += 1
        counts["accounts"] += 1
        counts["strategy_accounts" if method in STRATEGIES else "control_accounts"] += 1
        counts["ALL_observations"] += len(ledger)
        counts["PRIMARY_observations"] += sum(r["primary"] for r in ledger)
        if counts["accounts"] % 84 == 0:
            print(json.dumps({"completed_accounts": counts["accounts"], "capital": capital, "scenario": scenario}), flush=True)

    try:
        with localcontext() as ctx:
            ctx.prec = 128
            ctx.traps[Inexact] = True
            for symbol in views:
                for method in STRATEGIES:
                    account("1000000", "BASE", dict(BASE), symbol, method)
            check_A_anchor(groups, context.prior)
            eq(counts["A_native_payloads_equal"], len(views) * len(STRATEGIES) * 5, "complete A native anchor")
            anchor_complete = True
            for capital, scenario, values in matrix_configs(context.config):
                methods = CONTROLS if (capital, scenario) == ("1000000", "BASE") else METHODS
                for symbol in views:
                    for method in methods:
                        account(capital, scenario, values, symbol, method)
            n = context.accepted_counts["rb_declarations"]
            combinations = len(CAPITALS) * len(SCENARIOS)
            expected = {"accounts": n * len(METHODS) * combinations,
                        "strategy_accounts": n * len(STRATEGIES) * combinations,
                        "control_accounts": n * len(CONTROLS) * combinations,
                        "ALL_observations": context.accepted_counts["rb_bars"] * len(METHODS) * combinations,
                        "PRIMARY_observations": sum(max(0, len(v.bars) - 60) for v in views.values()) * len(METHODS) * combinations,
                        "contract_year_cells": n * len(METHODS) * combinations * len(YEARS) * len(VIEWS),
                        "A_native_payloads_equal": n * len(STRATEGIES) * 5}
            eq(counts, expected, "complete fixed matrix counts")
            summary, years, controls = aggregate_matrix(groups)
            conclusions, scope = conclusions_and_scope(summary, controls, context, counts)
            tables = dict(zip(PUBLISHED, (summary, years, controls, conclusions, scope)))
            report_raw, refs = render_v2(tables, context.prior)
            for filename, rows in tables.items():
                write_new(output / "publish" / filename, csv_bytes(rows))
            write_new(output / "publish" / REPORT, report_raw)
    finally:
        matrix_stream.close()
    context.pins.verify()
    draft = {"schema": "aktr-v1-daily-baseline-report/v2", "new_simulation": True,
             "new_training": False, "holdout_read": False, "evidence_level": "SCENARIO",
             "statistical_qualification": "NOT_ASSESSED", "prior_report_commit": "a80db5c",
             "prior_report_sha256": pin(context.prior[REPORT])["sha256"],
             "prior_CHECK_sha256": pin(context.prior["CHECK.json"])["sha256"],
             "prior_verdict_sha256": PRIOR_VERDICT_PIN, "sources": context.pins.records,
             "imported_engine_modules": imported, "original_authority_pins": AUTHORITY_PINS,
             "matrix_counts": counts, "A_anchor": {"native_payloads": counts["A_native_payloads_equal"],
                "annual_rows": 42, "paired_rows": 42, "all_exact": True},
             "reconciliation": {"all_account_fills_and_marks_exact": True, "logical_signals_equal_across_cells": True,
                "summary_rows": len(summary), "year_rows": len(years), "control_comparison_rows": len(controls)},
             "report_value_refs": refs, "safety": SAFETY,
             "argv_template": ["<PYTHON>=3.13", "-B", "-X", "utf8", "<B_DRIVER>", "--source-repo", "<ACCEPTED_RECORD_REPO>",
                "--authority-dir", "<ORIGINAL_AUTHORITY>", "--engine-root", "<FROZEN_ENGINE>", "--config", "<FIXED_B_CONFIG>", "--output-dir", "<RUN_OUTPUT>"],
             "argv_template_is_actual_command": False,
             "receipt_reference": "external matrix-terminal receipt; actual argv/cwd/PID/physical paths are not mathematical payload"}
    write_new(output / "check-draft.json", json_bytes(draft))
    payloads = {p.relative_to(output).as_posix(): pin(p.read_bytes()) for p in sorted(output.rglob("*"))
                if p.is_file() and not p.relative_to(output).as_posix().startswith("runtime/")}
    atomic_new(output / "manifest.json", {"schema": "aktr-rb-sensitivity-matrix/v1",
               "status": "matrix_complete_pending_seal", "counts": counts, "payloads": payloads})
    return counts


def seal_check(output_dir, matrix_receipt):
    """Separate seal child: validate a terminated matrix and write final CHECK."""
    output, receipt_path = path(output_dir), path(matrix_receipt)
    receipt = loads(receipt_path.read_bytes())
    need(receipt["stage"] == "matrix_child" and receipt["terminated"] is True and
         type(receipt["raw_exit"]) is int and receipt["raw_exit"] == 0, "matrix child did not terminate successfully")
    need(integer(receipt["pid"]) > 0 and receipt["task_id"] and receipt["started_utc"] and receipt["ended_utc"], "incomplete matrix process receipt")
    argv, cwd = receipt["argv"], path(receipt["cwd"])
    need(len(argv) == 15 and argv[1:4] == ["-B", "-X", "utf8"], "matrix argv differs from fixed template")
    eq(path(cwd / argv[4]), path(__file__), "actual matrix driver")
    args = parse_args(argv[5:])
    eq(path(args.output_dir), output, "actual matrix output")
    for name, record in receipt["closed_streams"].items():
        eq(pin(path(record["path"]).read_bytes()), {k: record[k] for k in ("bytes", "sha256")}, "terminated stream " + name)
    eq(set(receipt["closed_streams"]), {"stdout", "stderr"}, "both terminal streams")
    context = preflight(args.source_repo, args.authority_dir, args.engine_root, cwd / args.config, output, sealing=True)
    eq(loads((output / "runtime/source-locations.json").read_bytes()), context.pins.locations,
       "actual matrix physical source locations")
    draft = loads((output / "check-draft.json").read_bytes())
    eq(context.pins.records, draft["sources"], "source pins at seal")
    manifest_raw = (output / "manifest.json").read_bytes()
    manifest = loads(manifest_raw)
    eq(manifest["status"], "matrix_complete_pending_seal", "completed matrix marker")
    eq(manifest["counts"], draft["matrix_counts"], "matrix count receipt")
    actual_names = {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()
                    and not p.relative_to(output).as_posix().startswith("runtime/") and p.name != "manifest.json"}
    eq(actual_names, set(manifest["payloads"]), "complete deterministic payload set")
    for relative, expected in manifest["payloads"].items():
        eq(pin(child(output, relative).read_bytes()), expected, "sealed payload " + relative)
    draft.update(status="SEALED_MATRIX_EXIT_0", raw_exit=receipt["raw_exit"], raw_exit_subject="matrix_child",
                 matrix_manifest=pin(manifest_raw), output_hashes={k: v for k, v in manifest["payloads"].items() if k.startswith("publish/")})
    atomic_new(output / "publish/CHECK.json", draft)
    return {"status": draft["status"], "CHECK": pin((output / "publish/CHECK.json").read_bytes())}


class Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            parser.error("repeated option: " + option_string)
        setattr(namespace, self.dest, values)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    for name in ("source-repo", "authority-dir", "engine-root", "config", "output-dir"):
        parser.add_argument("--" + name, required=True, action=Once)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        context = preflight(args.source_repo, args.authority_dir, args.engine_root, args.config, args.output_dir)
        counts = run_matrix(context)
    except (ValidationError, ValueError, KeyError, ArithmeticError) as exc:
        print(json.dumps({"status": "VALIDATION_FAILED", "type": type(exc).__name__, "reason": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"status": "UNEXPECTED_FAILURE", "type": type(exc).__name__, "reason": str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 1
    print(json.dumps({"status": "MATRIX_COMPLETE_PENDING_SEAL", "counts": counts, "new_simulation": True}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
