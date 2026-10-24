"""Compare every bar-bearing RB contract under fixed offline daily scenarios.

The existing single-contract engine owns signals, fills and accounting. This
entry only binds its inputs, publishes the complete fixed matrix, and describes
contracts with at least 60 stored observations. That coverage gate does not
change any strategy's natural warmup or its full observation trajectory.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
import io
import json
import math
from pathlib import Path
import re
import stat
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / "src"))

from ak_trading.futures.io import (  # noqa: E402
    OutputDirectoryError, assert_output_directory_is_new, canonical_json_bytes,
    read_input_bundle, sha256_bytes, sha256_file,
)
from ak_trading.futures.scenario_backtest import (  # noqa: E402
    ScenarioConfig, run_history_scenario, run_scenario_backtest,
)

ARITHMETIC = Context(prec=128, rounding=ROUND_HALF_EVEN)
STRATEGIES = (
    ("donchian_trend", 41, {"entry_window": 40, "exit_window": 20}),
    ("ma_trend", 60, {"fast_window": 20, "slow_window": 60}),
    ("zscore_reversion", 21, {"lookback": 20, "entry_z": 2.0, "exit_z": 0.5,
                              "stop_z": 4.0, "max_signal_age": 20}),
)
SCENARIOS = ("BASE", "FEE_X2", "SLIPPAGE_X2", "MARGIN_X1_5")
ENGINE_CODE_PATHS = (
    "src/ak_trading/__init__.py", "src/ak_trading/futures/__init__.py",
    "src/ak_trading/futures/scenario_backtest.py", "src/ak_trading/futures/spec.py",
    "src/ak_trading/futures/strategies.py", "src/ak_trading/futures/structures.py",
    "src/ak_trading/futures/io.py", "src/ak_trading/futures/data.py",
    "src/ak_trading/strategy.py", "scripts/backtest_history.py",
)
CODE_PATHS = (*ENGINE_CODE_PATHS, "tools/experiments/historical_daily_scenario_compare.py")
CORE_INPUTS = ("bars.csv", "contracts.json", "metadata.json", "availability.json")
CHILD_PAYLOADS = ("config.json", "fills.csv", "equity.csv", "decisions.csv", "report.json")
COMMON = "MEETS_60_OBSERVATIONS"
SHORT = "INSUFFICIENT_COMMON_COVERAGE"
SAFETY = {
    "evidence_level": "SCENARIO", "historical_strict_execution": False,
    "assumptions_verification": "not_historical_verified", "broker": "NOT_RUN",
    "enable_trading": False, "real_order_send_calls": 0, "real_order_cancel_calls": 0,
}
MONEY_FIELDS = (
    "turnover_notional", "initial_cash", "ending_cash", "ending_equity",
    "gross_pnl", "fees", "slippage", "net_pnl", "total_return",
    "max_drawdown", "max_drawdown_ratio",
)
COUNT_FIELDS = (
    "fills", "closed_trades", "turnover_lots", "opening_blocks",
    "margin_breach_observations", "nonpositive_equity_observations", "ending_position_lots",
)
DIFFERENCE_FIELDS = ("net_pnl", "fees", "slippage", "max_drawdown", "fills", "opening_blocks")
COLUMNS = (
    "symbol", "contract_instance_id", "strategy", "scenario", "evidence_level",
    "coverage_status", "observations", "first_raw_ts", "last_raw_ts",
    "required_bars", "potential_next_open_slots", *COUNT_FIELDS, *MONEY_FIELDS,
    "peak_required_margin", "minimum_available_equity", "risk_observation_basis",
    *(f"delta_{name}_vs_BASE" for name in DIFFERENCE_FIELDS), "child_directory",
)


def _text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("nonfinite comparison number")
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _decimal(value: object) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("ledger/report amount must be a Decimal string")
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError("nonfinite ledger/report amount")
    return number


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError(f"nonfinite JSON number: {value}")


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        _nonfinite(value)
    return result


def _json(payload: bytes, *, decimal_floats: bool = False):
    return json.loads(payload.decode("utf-8"), object_pairs_hook=_unique,
                      parse_float=Decimal if decimal_floats else _finite_float,
                      parse_constant=_nonfinite)


def _object(payload: bytes, *, decimal_floats: bool = False) -> dict:
    result = _json(payload, decimal_floats=decimal_floats)
    if not isinstance(result, dict):
        raise ValueError("JSON document must be an object")
    return result


def _safe_path(path: Path, *, output: bool = False) -> Path:
    path = Path(path)
    if str(path).startswith(("\\\\?\\", "\\\\.\\")):
        raise ValueError("device namespaces are unsupported")
    for part in path.parts:
        stem = part.rstrip(" .").split(".", 1)[0].upper()
        if (stem in {"NUL", "CON", "PRN", "AUX", "CONIN$", "CONOUT$"}
                or re.fullmatch(r"(?:COM|LPT)[1-9]", stem)):
            raise ValueError("reserved device paths are unsupported")
        if part != path.anchor and ":" in part:
            raise ValueError("alternate stream paths are unsupported")
        if part.lower() == ".env" or part.lower().startswith(".env."):
            raise ValueError("credential files are outside the offline input contract")
    if output:
        absolute = path.absolute()
        for component in (absolute, *absolute.parents):
            try:
                info = component.lstat()
            except FileNotFoundError:
                continue
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise OutputDirectoryError("output path components may not be symlinks or junctions")
    return path.resolve()


def _code_hashes() -> dict[str, str]:
    return {name: sha256_file(ROOT / name) for name in CODE_PATHS}


def _unchanged(source: Path, snapshot: dict[str, str], base_file: Path,
               base_hash: str, code_hashes: dict[str, str]) -> None:
    if {name: sha256_file(source / name) for name in snapshot} != snapshot:
        raise ValueError("input bytes changed during comparison")
    if sha256_file(base_file) != base_hash:
        raise ValueError("BASE input bytes changed during comparison")
    if _code_hashes() != code_hashes:
        raise ValueError("comparison source code changed during comparison")


def _read_bundle(source: Path):
    if not source.is_dir():
        raise ValueError("required input bundle directory does not exist")
    for name in ("manifest.json", *CORE_INPUTS):
        if not (source / name).is_file():
            raise ValueError(f"required input file does not exist: {name}")
    manifest_bytes = (source / "manifest.json").read_bytes()
    manifest = _object(manifest_bytes)
    hashes = manifest.get("content_hashes")
    if not isinstance(hashes, dict) or not set(CORE_INPUTS) <= set(hashes):
        raise ValueError("input content_hashes must declare all four core payloads")
    snapshot = {"manifest.json": sha256_bytes(manifest_bytes)}
    for name, expected in hashes.items():
        if (not isinstance(name, str) or name in {"", ".", "..", "manifest.json"}
                or any(character in name for character in "/\\:")
                or not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None):
            raise ValueError("invalid input payload hash declaration")
        path = _safe_path(source / name)
        if path.parent != source or not path.is_file():
            raise ValueError("required input payload is absent or resolves outside bundle")
        payload = path.read_bytes()
        if sha256_bytes(payload) != expected:
            raise ValueError(f"input content hash mismatch: {name}")
        snapshot[name] = expected
        if name == "contracts.json":
            declarations = _json(payload)
            if not isinstance(declarations, list) or any(not isinstance(row, dict) for row in declarations):
                raise ValueError("contracts.json must contain only contract objects")
        elif name in {"metadata.json", "availability.json"}:
            _object(payload)
    bundle = read_input_bundle(source)
    if bundle.manifest != manifest or {name: sha256_file(source / name) for name in snapshot} != snapshot:
        raise ValueError("input changed while reading")
    return bundle, snapshot


def _configs(base_file: Path) -> tuple[bytes, dict[str, ScenarioConfig]]:
    if not base_file.is_file():
        raise ValueError("required BASE config file does not exist")
    if base_file.stat().st_size > 16_384:
        raise ValueError("BASE config exceeds 16 KiB")
    payload = base_file.read_bytes()
    base = ScenarioConfig.from_mapping(_object(payload, decimal_floats=True))
    result = {"BASE": base}
    with localcontext(ARITHMETIC):
        for name in SCENARIOS[1:]:
            values = base.to_dict()
            if name == "FEE_X2":
                values.update(fee_per_lot=_text(base.fee_per_lot * 2), fee_rate=_text(base.fee_rate * 2))
            elif name == "SLIPPAGE_X2":
                values["slippage_ticks"] = _text(base.slippage_ticks * 2)
            else:
                values["margin_rate"] = _text(base.margin_rate * Decimal("1.5"))
            result[name] = ScenarioConfig.from_mapping(values)
    return payload, result


def _coverage(bundle, configs: dict[str, ScenarioConfig]) -> dict:
    declarations = {}
    instances = set()
    for declaration in bundle.contracts:
        symbol = declaration.get("raw_symbol")
        if (not isinstance(symbol, str)
                or re.fullmatch(r"SHFE\.(?i:rb)[0-9]{2}(?:0[1-9]|1[0-2])", symbol) is None
                or declaration.get("product") != "SHFE.RB"):
            raise ValueError("population requires only concrete SHFE.RB future declarations")
        if symbol in declarations:
            raise ValueError("ambiguous raw contract symbol declaration")
        try:
            expiry = datetime.fromisoformat(declaration["expiry_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("contract requires timezone-aware expiry_at") from exc
        instance = declaration.get("instance_id")
        if expiry.utcoffset() is None or instance != f"{symbol}|{expiry.isoformat()}":
            raise ValueError("contract full instance identity mismatch")
        if instance in instances:
            raise ValueError("ambiguous full contract instance declaration")
        if any(key in declaration and declaration[key] != "FUTURE"
               for key in ("asset_class", "instrument_class", "ins_class")):
            raise ValueError("non-future contract declaration")
        declarations[symbol] = declaration
        instances.add(instance)
    bars_by_symbol = {symbol: [] for symbol in declarations}
    for bar in bundle.bars:
        if bar.symbol not in bars_by_symbol:
            raise ValueError("bar has no unique declared RB full instance")
        bars_by_symbol[bar.symbol].append(bar)
    if not bundle.bars:
        raise ValueError("comparison needs at least one stored daily observation")
    records = []
    for symbol in sorted(declarations):
        bars = sorted(bars_by_symbol[symbol], key=lambda bar: bar.datetime_nano)
        count = len(bars)
        # The public pure engine performs its unchanged full-instance, daily and
        # price validation before the output root exists. The largest adverse
        # move covers all four derived configs. No preflight trajectory is reused.
        if bars:
            run_scenario_backtest(bundle, symbol=symbol, strategy="donchian_trend",
                                  config=configs["SLIPPAGE_X2"])
        records.append({
            "symbol": symbol, "contract_instance_id": declarations[symbol]["instance_id"],
            "observations": count,
            "interval_status": "HAS_INTERVAL_OBSERVATIONS" if count else "NO_INTERVAL_OBSERVATIONS",
            "common_coverage_status": COMMON if count >= 60 else SHORT if count else "NO_INTERVAL_OBSERVATIONS",
            "first_raw_ts": bars[0].ts.isoformat() if bars else None,
            "last_raw_ts": bars[-1].ts.isoformat() if bars else None,
            "first_datetime_nano": bars[0].datetime_nano if bars else None,
            "last_datetime_nano": bars[-1].datetime_nano if bars else None,
            "strategy_coverage": {strategy: {
                "required_bars": required, "potential_next_open_slots": max(0, count - required),
            } for strategy, required, _ in STRATEGIES},
        })
    with_bars = sum(record["observations"] > 0 for record in records)
    common = sum(record["common_coverage_status"] == COMMON for record in records)
    return {
        **SAFETY, "schema_version": "rb_daily_scenario_compare_coverage_v1",
        "bar_count": len(bundle.bars), "declared_contracts": len(records),
        "contracts_with_bars": with_bars, "declared_without_interval_bars": len(records) - with_bars,
        "common_coverage_contracts": common, "insufficient_common_coverage_contracts": with_bars - common,
        "matrix_cells": with_bars * 12, "common_matrix_cells": common * 12,
        "insufficient_matrix_cells": (with_bars - common) * 12, "contracts": records,
        "coverage_gate": "at_least_60_stored_observations_for_descriptive_denominators_only",
        "raw_dates_verification": "stored_observation_identity_not_verified_trading_days",
        "warmup": "existing_strategy_natural_warmup_full_trajectory_no_unified_60_bar_start",
    }


def _csv_rows(payload: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(payload.decode("utf-8"), newline="")))


def _cell(child: Path, relative: str, record: dict, strategy: str, required: int,
          parameters: dict, scenario: str, config: ScenarioConfig, config_hash: str,
          input_snapshot: dict, input_manifest: dict, code_hashes: dict) -> tuple[dict, dict, dict]:
    expected_names = {*CHILD_PAYLOADS, "manifest.json"}
    if {path.name for path in child.iterdir()} != expected_names:
        raise ValueError("child output does not contain exactly the six engine files")
    payloads = {}
    for name in sorted(expected_names):
        path = child / name
        if _safe_path(path, output=True) != path or not path.is_file():
            raise ValueError("child contains unsafe or non-file output")
        payloads[name] = path.read_bytes()
    manifest = _object(payloads["manifest.json"])
    report = _object(payloads["report.json"])
    published_config = _object(payloads["config.json"])
    payload_hashes = {name: sha256_bytes(payloads[name]) for name in CHILD_PAYLOADS}
    if manifest.get("status") != "complete" or manifest.get("content_hashes") != payload_hashes:
        raise ValueError("child manifest is incomplete or has invalid content hashes")
    if (manifest.get("input_manifest_sha256") != input_snapshot["manifest.json"]
            or manifest.get("input_content_hashes") != input_manifest["content_hashes"]
            or manifest.get("input_config_sha256") != config_hash
            or manifest.get("code_hashes") != {name: code_hashes[name] for name in ENGINE_CODE_PATHS}
            or manifest.get("source_hashes") != input_manifest.get("source_hashes", {})):
        raise ValueError("child consumed different input, config or source versions")
    for document in (manifest, report):
        if (document.get("historical_strict_execution") is not False
                or document.get("assumptions_verification") != "not_historical_verified"
                or document.get("dataset_kind") != "historical_single_contract_daily_scenario"):
            raise ValueError("child evidence is outside the SCENARIO boundary")
    for document in (published_config, report):
        if (document.get("symbol") != record["symbol"]
                or document.get("contract_instance_id") != record["contract_instance_id"]
                or document.get("strategy") != strategy):
            raise ValueError("child identity mismatch")
    if (published_config.get("assumptions") != config.to_dict()
            or published_config.get("strategy_parameters") != parameters
            or report.get("status") != "complete"
            or report.get("observations") != record["observations"]
            or report.get("first_datetime_nano") != record["first_datetime_nano"]
            or report.get("last_datetime_nano") != record["last_datetime_nano"]):
        raise ValueError("child report/config does not match the fixed matrix")
    reconciliation = report.get("reconciliation")
    if not isinstance(reconciliation, dict) or not reconciliation or any(value is not True for value in reconciliation.values()):
        raise ValueError("child accounting reconciliation failed")
    fills = _csv_rows(payloads["fills.csv"])
    equity = _csv_rows(payloads["equity.csv"])
    decisions = _csv_rows(payloads["decisions.csv"])
    if len(fills) != report["fills"] or len(equity) != record["observations"] or len(decisions) != record["observations"]:
        raise ValueError("child ledger observation/fill counts do not match report")
    for field in MONEY_FIELDS:
        _decimal(report[field])
    if any(isinstance(report[field], bool) or not isinstance(report[field], int) for field in COUNT_FIELDS):
        raise ValueError("child count must be an integer")
    candidates = dict(sorted(Counter(row["candidate_direction"] or "null" for row in decisions).items()))
    if candidates != report["candidate_counts"]:
        raise ValueError("child candidate counts do not match decisions")
    row = {
        "symbol": record["symbol"], "contract_instance_id": record["contract_instance_id"],
        "strategy": strategy, "scenario": scenario, "evidence_level": "SCENARIO",
        "coverage_status": record["common_coverage_status"], "observations": record["observations"],
        "first_raw_ts": record["first_raw_ts"], "last_raw_ts": record["last_raw_ts"],
        "required_bars": required, "potential_next_open_slots": max(0, record["observations"] - required),
        **{field: report[field] for field in (*COUNT_FIELDS, *MONEY_FIELDS)},
        "peak_required_margin": _text(max(_decimal(mark["required_margin"]) for mark in equity)),
        "minimum_available_equity": _text(min(_decimal(mark["available_equity"]) for mark in equity)),
        "risk_observation_basis": "stored_observation_close_only_not_intrabar_worst_risk",
        "child_directory": relative,
    }
    details = {
        **row, "child_report": report,
        "decision_strategy_status_counts": dict(sorted(Counter(d["strategy_status"] for d in decisions).items())),
        "decision_reason_counts": dict(sorted(Counter(d["reason"] for d in decisions).items())),
        "decision_execution_status_counts": dict(sorted(Counter(d["execution_status"] for d in decisions).items())),
        "first_nonnull_candidate_index": next((int(d["observation_index"]) for d in decisions if d["candidate_direction"] != ""), None),
    }
    all_hashes = {f"{relative}/{name}": sha256_bytes(payload) for name, payload in payloads.items()}
    return row, details, all_hashes


def _distribution(values: list[Decimal]) -> dict:
    if not values:
        return {"min": None, "median": None, "max": None}
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    return {"min": _text(ordered[0]), "median": _text(median), "max": _text(ordered[-1])}


def _summary(rows: list[dict], details: list[dict], coverage: dict) -> dict:
    groups = []
    for strategy, _, _ in STRATEGIES:
        for scenario in SCENARIOS:
            population = [r for r in rows if r["strategy"] == strategy and r["scenario"] == scenario and r["coverage_status"] == COMMON]
            nets = [_decimal(r["net_pnl"]) for r in population]
            groups.append({
                "strategy": strategy, "scenario": scenario, "n": len(population),
                "status": "DESCRIPTIVE_ONLY" if population else "INSUFFICIENT",
                "population_unit": "overlapping_contract_coverage_not_independent_episodes",
                "positive_net_pnl_contracts": sum(value > 0 for value in nets),
                "zero_net_pnl_contracts": sum(value == 0 for value in nets),
                "negative_net_pnl_contracts": sum(value < 0 for value in nets),
                "zero_fill_contracts": sum(r["fills"] == 0 for r in population),
                "net_pnl": _distribution(nets),
                "total_return": _distribution([_decimal(r["total_return"]) for r in population]),
                "worst_single_contract_max_drawdown": _text(max(_decimal(r["max_drawdown"]) for r in population)) if population else None,
                "worst_single_contract_max_drawdown_ratio": _text(max(_decimal(r["max_drawdown_ratio"]) for r in population)) if population else None,
                "opening_block_contracts": sum(r["opening_blocks"] > 0 for r in population),
                "opening_blocks": sum(r["opening_blocks"] for r in population),
                "margin_breach_contracts": sum(r["margin_breach_observations"] > 0 for r in population),
                "margin_breach_observations": sum(r["margin_breach_observations"] for r in population),
            })
    return {
        **SAFETY, "schema_version": "rb_daily_scenario_compare_summary_v1",
        "matrix_cells": len(rows), "common_matrix_cells": coverage["common_matrix_cells"],
        "insufficient_matrix_cells": coverage["insufficient_matrix_cells"],
        "strategy_order": [s for s, _, _ in STRATEGIES], "scenario_order": list(SCENARIOS),
        "common_coverage_gate": 60, "cells": details, "groups": groups,
        "insufficient_common_coverage_contracts": [r for r in coverage["contracts"] if r["common_coverage_status"] == SHORT],
        "insufficient_common_coverage_cells": [r["child_directory"] for r in rows if r["coverage_status"] == SHORT],
        "decimal_precision": 128, "ratio_rounding": "ROUND_HALF_EVEN",
        "undefined_distribution_reason": "no_contract_meets_60_stored_observations" if not coverage["common_coverage_contracts"] else None,
    }


def _report(rows: list[dict], summary: dict, coverage: dict, *, synthetic: bool) -> bytes:
    population = "明确标记 SYNTHETIC 的离线测试输入" if synthetic else "冻结导出的真实行情观测"
    sample_note = ("这些价格是 SYNTHETIC，仅用于入口契约验证，不是历史收益或统计证据。" if synthetic
                   else "冻结 RB 输入区间已看过；旧演示只运行 SHFE.rb2101 / ma_trend / BASE 的 243 根观测。本次不能作为未知样本、样本外或最终留出证据。")
    lines = [
        f"本次使用{population}完成 {len(rows)} 个 SCENARIO 单元：{coverage['contracts_with_bars']} 个有行情的具体 RB 合约 × 三策略 × 四情景。",
        f"共同描述人口为 {coverage['common_coverage_contracts']} 个至少 60 根存储观测的合约；{coverage['insufficient_common_coverage_contracts']} 个短合约的全部结果单列。{coverage['declared_without_interval_bars']} 个声明合约在这个导出区间没有行情，不能据此断言历史缺失。",
        "",
        sample_note + "全部经济参数均为未核验情景假设，historical_strict_execution=false，assumptions_verification=not_historical_verified；broker=NOT_RUN，enable_trading=false，真实下单及撤单调用数均为 0。",
        "",
        "三策略保留各自自然暖机（Donchian 41、MA 60、z-score 21 根），完整轨迹并非统一等待 60 根后从空仓起跑。各合约实际首尾原始日期不同，原始时间戳不构成已核验交易日日历；详见 [coverage.json](coverage.json)。每单元使用相同初始资金与固定手数，未建立统一风险预算。",
        "",
        "BASE 净损益对照（按合约固定升序，金额为各自账户的情景净损益）：",
        "",
        "| 合约 | 存储观测 | Donchian | MA | z-score |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    base = {(r["symbol"], r["strategy"]): r for r in rows if r["scenario"] == "BASE"}
    for record in coverage["contracts"]:
        if record["common_coverage_status"] == COMMON:
            nets = [base[(record["symbol"], s)]["net_pnl"] for s, _, _ in STRATEGIES]
            lines.append(f"| {record['symbol']} | {record['observations']} | {' | '.join(nets)} |")
    if not coverage["common_coverage_contracts"]:
        lines += ["", "共同人口为空：分布指标为 null，状态为 INSUFFICIENT。"]
    lines += ["", "策略 × 情景描述（n 是相关且区间重叠的合约覆盖数；不表示独立机会）：", "",
              "| 策略 | 情景 | n | 净损益正/零/负 | 零成交 | 净损益 min / median / max | 总回报 min / median / max | 最严重单合约回撤 | 阻开合约/次数 | 保证金超限合约/观测 |",
              "| --- | --- | ---: | --- | ---: | --- | --- | ---: | --- | --- |"]
    for group in summary["groups"]:
        def display(field):
            return " / ".join("null" if group[field][key] is None else group[field][key] for key in ("min", "median", "max"))
        lines.append(f"| {group['strategy']} | {group['scenario']} | {group['n']} | {group['positive_net_pnl_contracts']}/{group['zero_net_pnl_contracts']}/{group['negative_net_pnl_contracts']} | {group['zero_fill_contracts']} | {display('net_pnl')} | {display('total_return')} | {group['worst_single_contract_max_drawdown'] or 'null'} | {group['opening_block_contracts']}/{group['opening_blocks']} | {group['margin_breach_contracts']}/{group['margin_breach_observations']} |")
    lines += ["", "共同覆盖不足的合约（全部 12 单元仍保留，不进入上述分母）：", "",
              "| 合约 | 观测 | Donchian/MA/z-score 潜在下一开盘槽 | BASE 净损益 Donchian/MA/z-score |",
              "| --- | ---: | --- | --- |"]
    for record in summary["insufficient_common_coverage_contracts"]:
        slots = "/".join(str(record["strategy_coverage"][s]["potential_next_open_slots"]) for s, _, _ in STRATEGIES)
        nets = "/".join(base[(record["symbol"], s)]["net_pnl"] for s, _, _ in STRATEGIES)
        lines.append(f"| {record['symbol']} | {record['observations']} | {slots} | {nets} |")
    lines += ["", "FEE_X2 同时翻倍每手与比例费用；SLIPPAGE_X2 只翻倍滑点 tick；MARGIN_X1_5 只把保证金率乘 1.5。四情景均完整重放。成本可能改变阻开及后继成交轨迹，压力净收益不保证单调下降。",
              "", "保证金峰值和最低可用权益只来自存储观测收盘标记，不是盘中最坏风险。n=60 只满足覆盖门，MA 最后观测产生的候选可能没有下一开盘执行槽；零候选和零成交均保留为完整结果。",
              "", "合约价格相关且区间重叠；不计算显著性、年化、Sharpe、胜率资格或组合收益，也不把各自初始现金账户相加/拼接为组合 NAV。不按结果改参数、删合约或授予策略盈利/资格 PASS。没有换月连续账户、历史 PIT、上市历史、历史 available_at、费用/保证金核验或细粒度可成交/涨跌停/容量证明。此描述支线没有 GAP/NO_STOP、概率校准、模型训练或正式 v2 资格验收。",
              "", "[comparison.csv](comparison.csv) 包含每单元金额、计数与相对 BASE 差值；[summary.json](summary.json) 保留决策状态/原因计数、首个非空候选索引、完整子报告会计核对与限制；[manifest.json](manifest.json) 绑定全部输出、输入和源码哈希。原始成交、权益与决策账本：", ""]
    for row in rows:
        directory = row["child_directory"]
        lines.append(f"- {row['symbol']} / {row['strategy']} / {row['scenario']}: [report]({directory}/report.json), [fills]({directory}/fills.csv), [equity]({directory}/equity.csv), [decisions]({directory}/decisions.csv)")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _comparison_csv(rows: list[dict]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _write_new(root: Path, name: str, payload: bytes, hashes: dict) -> None:
    path = root / name
    _safe_path(path, output=True)
    with path.open("xb") as handle:
        handle.write(payload)
    expected = sha256_bytes(payload)
    if sha256_file(path) != expected:
        raise OSError(f"output hash verification failed: {name}")
    hashes[name] = expected


def _verify_tree(root: Path, hashes: dict, *, marker: bool = False) -> None:
    actual = set()
    for path in root.rglob("*"):
        _safe_path(path, output=True)
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
        elif not path.is_dir():
            raise ValueError("non-file artifact in output tree")
    expected = set(hashes) | ({"manifest.json"} if marker else set())
    if actual != expected:
        raise ValueError("output tree does not match exact published hash closure")
    for name, expected_hash in hashes.items():
        if sha256_file(root / name) != expected_hash:
            raise ValueError(f"published output drift: {name}")


class _Incomplete(RuntimeError):
    def __init__(self, cause: BaseException):
        super().__init__(str(cause))
        self.cause = cause


def run_comparison(input_bundle: Path, base_config: Path, output: Path) -> dict:
    """Validate first, then exclusively publish one deterministic fixed matrix."""
    source = _safe_path(input_bundle)
    base_file = _safe_path(base_config)
    destination = _safe_path(output, output=True)
    forbidden = (source, base_file, *(ROOT / name for name in ("src", "scripts", "tests", "config", "tools", "docs")))
    assert_output_directory_is_new(destination, forbidden)
    code_hashes = _code_hashes()
    base_bytes, configs = _configs(base_file)
    bundle, snapshot = _read_bundle(source)
    coverage = _coverage(bundle, configs)
    base_hash = sha256_bytes(base_bytes)
    _unchanged(source, snapshot, base_file, base_hash, code_hashes)
    config_payloads = {name: canonical_json_bytes(config.to_dict()) for name, config in configs.items()}
    provenance = {
        **SAFETY, "schema_version": "rb_daily_scenario_compare_v1",
        "dataset_kind": "rb_fixed_matrix_daily_scenario_comparison",
        "input_manifest_sha256": snapshot["manifest.json"],
        "input_content_hashes": dict(bundle.manifest["content_hashes"]),
        "input_base_config_sha256": base_hash, "input_file_hashes": snapshot,
        "code_hashes": code_hashes,
        "configs": {name: config.to_dict() for name, config in configs.items()},
        "derived_config_hashes": {name: sha256_bytes(payload) for name, payload in config_payloads.items()},
        "matrix_cells": coverage["matrix_cells"], "common_matrix_cells": coverage["common_matrix_cells"],
        "insufficient_matrix_cells": coverage["insufficient_matrix_cells"],
    }
    _safe_path(destination, output=True)
    assert_output_directory_is_new(destination, forbidden)
    destination.mkdir(parents=True, exist_ok=False)
    hashes = {}

    def incomplete(exc):
        return canonical_json_bytes({**provenance, "status": "incomplete", "content_hashes": dict(hashes),
                                     "error_type": type(exc).__name__, "reason": "comparison_publication_failed"})

    try:
        (destination / "configs").mkdir(exist_ok=False)
        for name, payload in config_payloads.items():
            _write_new(destination, f"configs/{name}.json", payload, hashes)
        rows, details = [], []
        with localcontext(ARITHMETIC):
            for record in coverage["contracts"]:
                if not record["observations"]:
                    continue
                for strategy, required, parameters in STRATEGIES:
                    base_row = None
                    for scenario in SCENARIOS:
                        _unchanged(source, snapshot, base_file, base_hash, code_hashes)
                        relative = f"cells/{record['symbol']}/{strategy}/{scenario}"
                        child = _safe_path(destination / relative, output=True)
                        run_history_scenario(source, child, symbol=record["symbol"], strategy=strategy,
                                             config_path=destination / "configs" / f"{scenario}.json")
                        row, detail, child_hashes = _cell(
                            child, relative, record, strategy, required, parameters, scenario,
                            configs[scenario], sha256_bytes(config_payloads[scenario]), snapshot,
                            bundle.manifest, code_hashes,
                        )
                        _unchanged(source, snapshot, base_file, base_hash, code_hashes)
                        if scenario == "BASE":
                            base_row = row
                        for field in DIFFERENCE_FIELDS:
                            row[f"delta_{field}_vs_BASE"] = (row[field] - base_row[field] if field in COUNT_FIELDS
                                                           else _text(_decimal(row[field]) - _decimal(base_row[field])))
                        detail.update({field: row[field] for field in row if field.startswith("delta_")})
                        rows.append(row)
                        details.append(detail)
                        hashes.update(child_hashes)
            keys = {(r["symbol"], r["contract_instance_id"], r["strategy"], r["scenario"]) for r in rows}
            if len(keys) != len(rows) or len(rows) != coverage["matrix_cells"]:
                raise ValueError("fixed comparison matrix has missing or duplicate cells")
            summary = _summary(rows, details, coverage)
        synthetic = bundle.metadata.get("evidence_level") == "SYNTHETIC"
        payloads = {
            "coverage.json": canonical_json_bytes(coverage), "comparison.csv": _comparison_csv(rows),
            "summary.json": canonical_json_bytes(summary), "report.md": _report(rows, summary, coverage, synthetic=synthetic),
        }
        for name, payload in payloads.items():
            _write_new(destination, name, payload, hashes)
        _verify_tree(destination, hashes)
        _unchanged(source, snapshot, base_file, base_hash, code_hashes)
        for name, expected in provenance["derived_config_hashes"].items():
            if sha256_file(destination / "configs" / f"{name}.json") != expected:
                raise ValueError("derived config changed during comparison")
        marker = destination / "manifest.json"
        complete = canonical_json_bytes({**provenance, "status": "complete", "content_hashes": dict(sorted(hashes.items()))})
        with marker.open("xb") as handle:
            try:
                handle.write(complete)
                handle.flush()
                if marker.read_bytes() != complete:
                    raise OSError("complete manifest read-back failed")
                _verify_tree(destination, hashes, marker=True)
                _unchanged(source, snapshot, base_file, base_hash, code_hashes)
            except BaseException as exc:
                # Repair only the descriptor created by this writer. Never
                # reopen/overwrite another writer's collision file.
                try:
                    handle.seek(0)
                    handle.truncate()
                    handle.write(incomplete(exc))
                    handle.flush()
                except OSError:
                    pass
                raise
    except BaseException as exc:
        try:
            with (destination / "manifest.json").open("xb") as handle:
                handle.write(incomplete(exc))
        except OSError:
            pass
        if isinstance(exc, Exception):
            raise _Incomplete(exc) from exc
        raise
    return {**SAFETY, "status": "complete", "matrix_cells": len(rows),
            "common_matrix_cells": coverage["common_matrix_cells"],
            "insufficient_matrix_cells": coverage["insufficient_matrix_cells"],
            "output": str(destination)}


class _ArgumentError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _ArgumentError(message)


class _Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            parser.error(f"{option_string} must be provided exactly once")
        setattr(namespace, self.dest, values)


def main(argv=None) -> int:
    parser = _Parser(description=__doc__, allow_abbrev=False)
    for option in ("--input-bundle", "--base-config", "--output"):
        parser.add_argument(option, type=Path, required=True, action=_Once)
    try:
        args = parser.parse_args(argv)
        result = run_comparison(args.input_bundle, args.base_config, args.output)
    except Exception as exc:
        cause = exc.cause if isinstance(exc, _Incomplete) else exc
        print(json.dumps({**SAFETY, "status": "incomplete" if isinstance(exc, _Incomplete) else "rejected",
                          "error_type": type(cause).__name__, "reason": str(cause)[:500]},
                         ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
