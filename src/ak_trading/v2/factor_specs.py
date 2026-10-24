"""M2 factor registry: the ten v2 calculators and the v1-window baselines.

This module freezes the parameter registration and formula metadata of
devplan-v2.1.0 M2 (02-M2-因子计算器.md §4.1/§4.3, 变更申请-005 §4).  The
ten calculator ids, their verbatim 23-field source objects and the
definition binding hashes were read once from the read-only factor catalog
(``china_futures_v1_20260913``) and are frozen here as literals; the
catalog is never queried at runtime and formula strings are never
executed.  ``source_sha256`` is exactly
``SHA256(canonical_json_bytes(source_factor_object))`` — the full original
record with M1 canonical encoding (sorted keys, compact, one trailing LF)
— shared by all six frequencies of one source factor.

``factor_key`` format is exactly
``<source_factor_id>:v2.1.0:d<duration_seconds>:<parameter_version>``.
Minute frequencies use the ``m1/m5/m15/m30/m60`` prefixes with ``n=60``
complete bars, the daily version uses ``d1-n20``; ``VOI009`` has no lookback
window and registers the ``*-instant`` parameter versions with
``lookback=1``.  ``REV001`` keeps the source formula letter ``k`` and
registers the explicit ``k_equals_n`` alias instead of silently rewriting
the definition.  Every spec carries the eight mandatory parameter keys
(``return_kind/std_ddof/include_current/window_unit/annualization/
annualization_factor/session_boundary/zero_policy``); values that do not
apply use the literal string ``not_applicable``.

Minute main versions annualize with ``PER_BAR`` (A=1); the daily main
version uses ``NOMINAL_252`` (A=252) as a nominal scale.  Returns are
``r_t = ln(C_t/C_{t-1})`` and standard deviations use the sample estimator
(ddof=1).
"""
from __future__ import annotations

import hashlib
from typing import Mapping

from ak_trading.v2.contracts import FactorSpec, JsonValue
from ak_trading.v2.schemas import canonical_json_bytes

CONTRACT_SCHEME_VERSION = "v2.1.0"
SOURCE_CATALOG_ID = "china_futures_v1_20260913"
SOURCE_FACTOR_VERSION = "1.0"
OPERATOR_VERSION = "m2-v2.1.0"
BASELINE_OPERATOR_VERSION = "baseline-v2.1.0"
BASELINE_PARAMETER_VERSION = "legacy-window-v1"

SOURCE_FACTOR_IDS: tuple[str, ...] = (
    "MOM001", "MOM003", "MOM010", "REV001",
    "VOL001", "VOL004", "VOL005",
    "VOI001", "VOI007", "VOI009",
)

#: Verbatim catalog formulas; preserved, never executed (M2 §2, M2-H04).
SOURCE_FORMULAS: Mapping[str, str] = {
    "MOM001": "sum(r,n)",
    "MOM003": "sum(r,n)/(std(r,n)*sqrt(n))",
    "MOM010": "(P-lag(P,n))/sum(abs(diff(P)),n)",
    "REV001": "-sum(r,k)",
    "VOL001": "std(r,n)*sqrt(A)",
    "VOL004": "ATR(n)/abs(P)",
    "VOL005": "mean(min(r,0)^2,n)*A",
    "VOI001": "V/lag(mean(V,n),1)-1",
    "VOI007": "OI/lag(OI,n)-1",
    "VOI009": "V/OI",
}

#: Frozen original 23-field catalog records (change request 005 §4).  The
#: extraction evidence — original ZIP/CSV-checksums/SQLite file hashes and
#: the read-only query trail — ships with the M2 run as
#: ``source-extraction.json``; the runtime never queries the catalog.
SOURCE_FACTOR_OBJECTS: dict[str, dict[str, object]] = {
    "MOM001": {
        "available_at_rule": "max(各输入的available_at)+计算延迟；下一可成交时点，日终数据禁止当日收盘回填",
        "backtest": "未回测；收益/胜率/IC未知",
        "caveats": "r为同一合约单日收益，换月日不能跨合约相除",
        "data_requirement": "L0 日行情/合约",
        "direction_hypothesis": "正：延续假说",
        "evidence": "研究定义；不是已验证Alpha",
        "factor_id": "MOM001",
        "family": "趋势与动量",
        "formula": "sum(r,n)",
        "frequency": "日",
        "implementation": "公式规范；未接数据/未实现全部计算器",
        "inputs": "r",
        "layer": "通用",
        "minimum_history": "不短于最大回看窗；月度季节项优先≥3年，短样本保留缺失",
        "name": "累计合约收益动量",
        "parameters": "n=20/60/120；仅训练窗选择",
        "priority": "P0",
        "role": "收益预测候选",
        "scope": "ALL",
        "source_ids": "TSMOM,SHFE,DCE,CZCE",
        "source_urls": "https://www.aqr.com/Insights/Datasets/Time-Series-Momentum-Factors-Monthly ; https://www.shfe.com.cn/ ; https://www.dce.com.cn/ ; https://www.czce.com.cn/",
        "transform": "滚动去极值/时序z分数；横截面按板块和波动中性化",
        "version": "1.0"
    },
    "MOM003": {
        "available_at_rule": "max(各输入的available_at)+计算延迟；下一可成交时点，日终数据禁止当日收盘回填",
        "backtest": "未回测；收益/胜率/IC未知",
        "caveats": "零波动返回缺失",
        "data_requirement": "L0 日行情/合约",
        "direction_hypothesis": "正：延续假说",
        "evidence": "研究定义；不是已验证Alpha",
        "factor_id": "MOM003",
        "family": "趋势与动量",
        "formula": "sum(r,n)/(std(r,n)*sqrt(n))",
        "frequency": "日",
        "implementation": "公式规范；未接数据/未实现全部计算器",
        "inputs": "r",
        "layer": "通用",
        "minimum_history": "不短于最大回看窗；月度季节项优先≥3年，短样本保留缺失",
        "name": "波动调整动量",
        "parameters": "n=20/60/120；仅训练窗选择",
        "priority": "P0",
        "role": "收益预测候选",
        "scope": "ALL",
        "source_ids": "TSMOM,SHFE,DCE,CZCE",
        "source_urls": "https://www.aqr.com/Insights/Datasets/Time-Series-Momentum-Factors-Monthly ; https://www.shfe.com.cn/ ; https://www.dce.com.cn/ ; https://www.czce.com.cn/",
        "transform": "滚动去极值/时序z分数；横截面按板块和波动中性化",
        "version": "1.0"
    },
    "MOM010": {
        "available_at_rule": "max(各输入的available_at)+计算延迟；下一可成交时点，日终数据禁止当日收盘回填",
        "backtest": "未回测；收益/胜率/IC未知",
        "caveats": "使用一致尺度的连续研究路径",
        "data_requirement": "L0 日行情/合约",
        "direction_hypothesis": "正：方向性趋势",
        "evidence": "研究定义；不是已验证Alpha",
        "factor_id": "MOM010",
        "family": "趋势与动量",
        "formula": "(P-lag(P,n))/sum(abs(diff(P)),n)",
        "frequency": "日",
        "implementation": "公式规范；未接数据/未实现全部计算器",
        "inputs": "P",
        "layer": "通用",
        "minimum_history": "不短于最大回看窗；月度季节项优先≥3年，短样本保留缺失",
        "name": "路径效率比",
        "parameters": "n=20/60/120；仅训练窗选择",
        "priority": "P0",
        "role": "收益预测候选",
        "scope": "ALL",
        "source_ids": "TSMOM,SHFE,DCE,CZCE",
        "source_urls": "https://www.aqr.com/Insights/Datasets/Time-Series-Momentum-Factors-Monthly ; https://www.shfe.com.cn/ ; https://www.dce.com.cn/ ; https://www.czce.com.cn/",
        "transform": "滚动去极值/时序z分数；横截面按板块和波动中性化",
        "version": "1.0"
    },
    "REV001": {
        "available_at_rule": "max(各输入的available_at)+计算延迟；下一可成交时点，日终数据禁止当日收盘回填",
        "backtest": "未回测；收益/胜率/IC未知",
        "caveats": "必须扣高换手成本",
        "data_requirement": "L0 日行情/合约",
        "direction_hypothesis": "正：买弱卖强假说",
        "evidence": "研究定义；不是已验证Alpha",
        "factor_id": "REV001",
        "family": "反转与偏离",
        "formula": "-sum(r,k)",
        "frequency": "日",
        "implementation": "公式规范；未接数据/未实现全部计算器",
        "inputs": "r",
        "layer": "通用",
        "minimum_history": "不短于最大回看窗；月度季节项优先≥3年，短样本保留缺失",
        "name": "短期收益反转",
        "parameters": "n=20/60/120；仅训练窗选择",
        "priority": "P1",
        "role": "收益预测候选",
        "scope": "ALL",
        "source_ids": "SHFE,DCE,CZCE,GFEX,CFFEX",
        "source_urls": "https://www.shfe.com.cn/ ; https://www.dce.com.cn/ ; https://www.czce.com.cn/ ; https://www.gfex.com.cn/ ; https://www.cffex.com.cn/",
        "transform": "滚动去极值/时序z分数；横截面按板块和波动中性化",
        "version": "1.0"
    },
    "VOI001": {
        "available_at_rule": "max(各输入的available_at)+计算延迟；下一可成交时点，日终数据禁止当日收盘回填",
        "backtest": "未回测；收益/胜率/IC未知",
        "caveats": "成交量、持仓量历史单双边统计变化需统一；换月不能跨合约错误计算",
        "data_requirement": "L0 日行情/合约",
        "direction_hypothesis": "状态：活跃度变化",
        "evidence": "研究定义；不是已验证Alpha",
        "factor_id": "VOI001",
        "family": "成交量与持仓",
        "formula": "V/lag(mean(V,n),1)-1",
        "frequency": "日",
        "implementation": "公式规范；未接数据/未实现全部计算器",
        "inputs": "V",
        "layer": "通用",
        "minimum_history": "不短于最大回看窗；月度季节项优先≥3年，短样本保留缺失",
        "name": "相对成交量",
        "parameters": "n=20/60/120；仅训练窗选择",
        "priority": "P0",
        "role": "收益预测候选",
        "scope": "ALL",
        "source_ids": "SHFE,DCE,CZCE,GFEX,CFFEX",
        "source_urls": "https://www.shfe.com.cn/ ; https://www.dce.com.cn/ ; https://www.czce.com.cn/ ; https://www.gfex.com.cn/ ; https://www.cffex.com.cn/",
        "transform": "滚动去极值/时序z分数；横截面按板块和波动中性化",
        "version": "1.0"
    },
    "VOI007": {
        "available_at_rule": "max(各输入的available_at)+计算延迟；下一可成交时点，日终数据禁止当日收盘回填",
        "backtest": "未回测；收益/胜率/IC未知",
        "caveats": "OI>0；不能解释成净资金流入",
        "data_requirement": "L0 日行情/合约",
        "direction_hypothesis": "状态：风险承担变化",
        "evidence": "研究定义；不是已验证Alpha",
        "factor_id": "VOI007",
        "family": "成交量与持仓",
        "formula": "OI/lag(OI,n)-1",
        "frequency": "日",
        "implementation": "公式规范；未接数据/未实现全部计算器",
        "inputs": "OI",
        "layer": "通用",
        "minimum_history": "不短于最大回看窗；月度季节项优先≥3年，短样本保留缺失",
        "name": "持仓变化率",
        "parameters": "n=20/60/120；仅训练窗选择",
        "priority": "P0",
        "role": "收益预测候选",
        "scope": "ALL",
        "source_ids": "SHFE,DCE,CZCE,GFEX,CFFEX",
        "source_urls": "https://www.shfe.com.cn/ ; https://www.dce.com.cn/ ; https://www.czce.com.cn/ ; https://www.gfex.com.cn/ ; https://www.cffex.com.cn/",
        "transform": "滚动去极值/时序z分数；横截面按板块和波动中性化",
        "version": "1.0"
    },
    "VOI009": {
        "available_at_rule": "max(各输入的available_at)+计算延迟；下一可成交时点，日终数据禁止当日收盘回填",
        "backtest": "未回测；收益/胜率/IC未知",
        "caveats": "成交量、持仓量历史单双边统计变化需统一；换月不能跨合约错误计算",
        "data_requirement": "L0 日行情/合约",
        "direction_hypothesis": "状态：投机活跃度代理",
        "evidence": "研究定义；不是已验证Alpha",
        "factor_id": "VOI009",
        "family": "成交量与持仓",
        "formula": "V/OI",
        "frequency": "日",
        "implementation": "公式规范；未接数据/未实现全部计算器",
        "inputs": "V,OI",
        "layer": "通用",
        "minimum_history": "不短于最大回看窗；月度季节项优先≥3年，短样本保留缺失",
        "name": "成交持仓比",
        "parameters": "n=20/60/120；仅训练窗选择",
        "priority": "P0",
        "role": "收益预测候选",
        "scope": "ALL",
        "source_ids": "SHFE,DCE,CZCE,GFEX,CFFEX",
        "source_urls": "https://www.shfe.com.cn/ ; https://www.dce.com.cn/ ; https://www.czce.com.cn/ ; https://www.gfex.com.cn/ ; https://www.cffex.com.cn/",
        "transform": "滚动去极值/时序z分数；横截面按板块和波动中性化",
        "version": "1.0"
    },
    "VOL001": {
        "available_at_rule": "max(各输入的available_at)+计算延迟；下一可成交时点，日终数据禁止当日收盘回填",
        "backtest": "未回测；收益/胜率/IC未知",
        "caveats": "",
        "data_requirement": "L0 日行情/合约",
        "direction_hypothesis": "风险：高值波动更大",
        "evidence": "研究定义；不是已验证Alpha",
        "factor_id": "VOL001",
        "family": "波动与尾部风险",
        "formula": "std(r,n)*sqrt(A)",
        "frequency": "日",
        "implementation": "公式规范；未接数据/未实现全部计算器",
        "inputs": "r",
        "layer": "通用",
        "minimum_history": "不短于最大回看窗；月度季节项优先≥3年，短样本保留缺失",
        "name": "历史实现波动",
        "parameters": "n=20/60/120；A按bar频率定义；alpha=0.95/0.99",
        "priority": "P0",
        "role": "风险/状态",
        "scope": "ALL",
        "source_ids": "SHFE,DCE,CZCE,GFEX,CFFEX",
        "source_urls": "https://www.shfe.com.cn/ ; https://www.dce.com.cn/ ; https://www.czce.com.cn/ ; https://www.gfex.com.cn/ ; https://www.cffex.com.cn/",
        "transform": "滚动去极值/时序z分数；横截面按板块和波动中性化",
        "version": "1.0"
    },
    "VOL004": {
        "available_at_rule": "max(各输入的available_at)+计算延迟；下一可成交时点，日终数据禁止当日收盘回填",
        "backtest": "未回测；收益/胜率/IC未知",
        "caveats": "P接近零禁用",
        "data_requirement": "L0 日行情/合约",
        "direction_hypothesis": "风险：波幅尺度",
        "evidence": "研究定义；不是已验证Alpha",
        "factor_id": "VOL004",
        "family": "波动与尾部风险",
        "formula": "ATR(n)/abs(P)",
        "frequency": "日",
        "implementation": "公式规范；未接数据/未实现全部计算器",
        "inputs": "O,H,L,C",
        "layer": "通用",
        "minimum_history": "不短于最大回看窗；月度季节项优先≥3年，短样本保留缺失",
        "name": "ATR相对价格",
        "parameters": "n=20/60/120；A按bar频率定义；alpha=0.95/0.99",
        "priority": "P0",
        "role": "风险/状态",
        "scope": "ALL",
        "source_ids": "SHFE,DCE,CZCE,GFEX,CFFEX",
        "source_urls": "https://www.shfe.com.cn/ ; https://www.dce.com.cn/ ; https://www.czce.com.cn/ ; https://www.gfex.com.cn/ ; https://www.cffex.com.cn/",
        "transform": "滚动去极值/时序z分数；横截面按板块和波动中性化",
        "version": "1.0"
    },
    "VOL005": {
        "available_at_rule": "max(各输入的available_at)+计算延迟；下一可成交时点，日终数据禁止当日收盘回填",
        "backtest": "未回测；收益/胜率/IC未知",
        "caveats": "",
        "data_requirement": "L0 日行情/合约",
        "direction_hypothesis": "风险：下跌尾部",
        "evidence": "研究定义；不是已验证Alpha",
        "factor_id": "VOL005",
        "family": "波动与尾部风险",
        "formula": "mean(min(r,0)^2,n)*A",
        "frequency": "日",
        "implementation": "公式规范；未接数据/未实现全部计算器",
        "inputs": "r",
        "layer": "通用",
        "minimum_history": "不短于最大回看窗；月度季节项优先≥3年，短样本保留缺失",
        "name": "下行半方差",
        "parameters": "n=20/60/120；A按bar频率定义；alpha=0.95/0.99",
        "priority": "P0",
        "role": "风险/状态",
        "scope": "ALL",
        "source_ids": "SHFE,DCE,CZCE,GFEX,CFFEX",
        "source_urls": "https://www.shfe.com.cn/ ; https://www.dce.com.cn/ ; https://www.czce.com.cn/ ; https://www.gfex.com.cn/ ; https://www.cffex.com.cn/",
        "transform": "滚动去极值/时序z分数；横截面按板块和波动中性化",
        "version": "1.0"
    }
}

MINUTE_DURATIONS: tuple[int, ...] = (60, 300, 900, 1800, 3600)
DAILY_DURATION = 86400
DURATIONS: tuple[int, ...] = (*MINUTE_DURATIONS, DAILY_DURATION)
MINUTE_WINDOW_N = 60
DAILY_WINDOW_N = 20

MINUTE_PARAMETER_VERSIONS: Mapping[int, str] = {
    60: "m1-n60", 300: "m5-n60", 900: "m15-n60", 1800: "m30-n60", 3600: "m60-n60",
}
DAILY_PARAMETER_VERSION = "d1-n20"
MINUTE_INSTANT_VERSIONS: Mapping[int, str] = {
    60: "m1-instant", 300: "m5-instant", 900: "m15-instant",
    1800: "m30-instant", 3600: "m60-instant",
}
DAILY_INSTANT_VERSION = "d1-instant"

MANDATORY_PARAMETER_KEYS: tuple[str, ...] = (
    "return_kind", "std_ddof", "include_current", "window_unit",
    "annualization", "annualization_factor", "session_boundary", "zero_policy",
)
NOT_APPLICABLE = "not_applicable"

#: Units of the ten calculators (registry artifacts must show units, M2 §5).
_UNITS: Mapping[str, str] = {
    "MOM001": "log_return_sum",
    "MOM003": "log_return_sum_per_std",
    "MOM010": "signed_efficiency_ratio",
    "REV001": "log_return_sum",
    "VOL001": "log_return_std",
    "VOL004": "atr_per_price_ratio",
    "VOL005": "downside_semi_variance",
    "VOI001": "fractional_change",
    "VOI007": "fractional_change",
    "VOI009": "volume_to_oi_ratio",
}

_REQUIRED_FIELDS: Mapping[str, tuple[str, ...]] = {
    "MOM001": ("close",), "MOM003": ("close",), "MOM010": ("close",),
    "REV001": ("close",), "VOL001": ("close",), "VOL005": ("close",),
    "VOL004": ("high", "low", "close"),
    "VOI001": ("volume",), "VOI007": ("close_oi",), "VOI009": ("volume", "close_oi"),
}

#: The eight mandatory keys plus factor-specific zero handling.  ``signed``
#: makes the MOM010/baseline-ER distinction visible in the registry.
_STATIC_PARAMETERS: Mapping[str, Mapping[str, JsonValue]] = {
    "MOM001": {"return_kind": "LOG", "std_ddof": NOT_APPLICABLE,
               "include_current": True, "zero_policy": NOT_APPLICABLE},
    "MOM003": {"return_kind": "LOG", "std_ddof": 1, "include_current": True,
               "zero_policy": "none_on_zero_std"},
    "MOM010": {"return_kind": "PRICE_DIFF", "std_ddof": NOT_APPLICABLE,
               "include_current": True, "zero_policy": "none_on_zero_total_path",
               "signed": True},
    "REV001": {"return_kind": "LOG", "std_ddof": NOT_APPLICABLE,
               "include_current": True, "zero_policy": NOT_APPLICABLE},
    "VOL001": {"return_kind": "LOG", "std_ddof": 1, "include_current": True,
               "zero_policy": "zero_output_on_zero_std"},
    "VOL004": {"return_kind": NOT_APPLICABLE, "std_ddof": NOT_APPLICABLE,
               "include_current": True, "zero_policy": "invalid_on_non_positive_close"},
    "VOL005": {"return_kind": "LOG", "std_ddof": NOT_APPLICABLE,
               "include_current": True, "zero_policy": NOT_APPLICABLE},
    "VOI001": {"return_kind": NOT_APPLICABLE, "std_ddof": NOT_APPLICABLE,
               "include_current": False,
               "zero_policy": "minus1_on_zero_current_invalid_on_zero_reference"},
    "VOI007": {"return_kind": NOT_APPLICABLE, "std_ddof": NOT_APPLICABLE,
               "include_current": True, "zero_policy": "invalid_on_zero_lag_oi"},
    "VOI009": {"return_kind": NOT_APPLICABLE, "std_ddof": NOT_APPLICABLE,
               "include_current": True, "zero_policy": "invalid_on_zero_oi"},
}


_SOURCE_RECORD_FIELDS: tuple[str, ...] = (
    "factor_id", "name", "layer", "family", "role", "scope", "formula", "inputs",
    "frequency", "parameters", "direction_hypothesis", "transform",
    "data_requirement", "available_at_rule", "minimum_history", "source_ids",
    "source_urls", "evidence", "caveats", "priority", "implementation",
    "backtest", "version",
)


def source_factor_object(factor_id: str) -> dict[str, object]:
    """The frozen original 23-field catalog record of one source factor."""
    if factor_id not in SOURCE_FACTOR_OBJECTS:
        raise ValueError(f"unknown_source_factor_id:{factor_id}")
    record = SOURCE_FACTOR_OBJECTS[factor_id]
    if tuple(sorted(record)) != tuple(sorted(_SOURCE_RECORD_FIELDS)):
        raise ValueError(f"source_record_fields_mismatch:{factor_id}")
    return record


def source_definition_sha256(factor_id: str) -> str:
    """``SHA256(canonical_json_bytes(source_factor_object))`` (005 §4).

    The preimage is the complete original record — every field verbatim,
    M1 canonical encoding with exactly one trailing LF; not a transcribed
    subset and not a file hash of the catalog archive.
    """
    return hashlib.sha256(canonical_json_bytes(source_factor_object(factor_id))).hexdigest()


def window_n(duration_seconds: int) -> int:
    """Complete-bar window of the main version: 60 minute bars / 20 days."""
    if duration_seconds in MINUTE_PARAMETER_VERSIONS:
        return MINUTE_WINDOW_N
    if duration_seconds == DAILY_DURATION:
        return DAILY_WINDOW_N
    raise ValueError(f"unsupported_duration_seconds:{duration_seconds}")


def parameter_version(duration_seconds: int) -> str:
    if duration_seconds in MINUTE_PARAMETER_VERSIONS:
        return MINUTE_PARAMETER_VERSIONS[duration_seconds]
    if duration_seconds == DAILY_DURATION:
        return DAILY_PARAMETER_VERSION
    raise ValueError(f"unsupported_duration_seconds:{duration_seconds}")


def instant_parameter_version(duration_seconds: int) -> str:
    if duration_seconds in MINUTE_INSTANT_VERSIONS:
        return MINUTE_INSTANT_VERSIONS[duration_seconds]
    if duration_seconds == DAILY_DURATION:
        return DAILY_INSTANT_VERSION
    raise ValueError(f"unsupported_duration_seconds:{duration_seconds}")


def _annualization(factor_id: str, duration_seconds: int) -> tuple[str, JsonValue]:
    if factor_id not in ("VOL001", "VOL005"):
        return NOT_APPLICABLE, NOT_APPLICABLE
    if duration_seconds == DAILY_DURATION:
        return "NOMINAL_252", 252
    return "PER_BAR", 1


def _ten_spec(factor_id: str, duration_seconds: int) -> FactorSpec:
    n = window_n(duration_seconds)
    annualization, annualization_factor = _annualization(factor_id, duration_seconds)
    instant = factor_id == "VOI009"
    parameters: dict[str, JsonValue] = {
        **_STATIC_PARAMETERS[factor_id],
        "window_unit": NOT_APPLICABLE if instant else "BARS",
        "annualization": annualization,
        "annualization_factor": annualization_factor,
        "session_boundary": NOT_APPLICABLE if instant else "INCLUDE_GAP",
    }
    lookback = 1
    if not instant:
        parameters["n"] = n
        lookback = n + 1
        if factor_id == "REV001":
            # Source formula letter k := n, registered as an explicit alias
            # (M2 §4.1); the original definition is not rewritten.
            parameters["k"] = n
            parameters["alias"] = "k_equals_n"
    parameter_version_value = (
        instant_parameter_version(duration_seconds) if instant
        else parameter_version(duration_seconds))
    return FactorSpec(
        factor_key=f"{factor_id}:{CONTRACT_SCHEME_VERSION}:d{duration_seconds}:{parameter_version_value}",
        source_factor_id=factor_id,
        operator_version=OPERATOR_VERSION,
        parameter_version=parameter_version_value,
        duration_seconds=duration_seconds,
        lookback=lookback,
        parameters=parameters,
        unit=_UNITS[factor_id],
        required_fields=_REQUIRED_FIELDS[factor_id],
        formula=SOURCE_FORMULAS[factor_id],
        source_sha256=source_definition_sha256(factor_id),
    )


def factor_registry_v2(duration_seconds: int) -> tuple[FactorSpec, ...]:
    """The ten v2 calculators registered for one duration, sorted by key."""
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int):
        raise TypeError("duration_seconds_must_be_int")
    if duration_seconds not in DURATIONS:
        raise ValueError(f"unsupported_duration_seconds:{duration_seconds}")
    return tuple(
        sorted((_ten_spec(factor_id, duration_seconds) for factor_id in SOURCE_FACTOR_IDS),
               key=lambda spec: spec.factor_key))


# ---------------------------------------------------------------------------
# v1-window baselines (M2 §4.3): old window semantics, new duration-aware keys.
# The fourteen names, lookbacks, units and definitions mirror the version-1
# registry in ``ak_trading.probability.factors`` verbatim; only the keys and
# the parameter metadata are new.  The adapted daily output additionally
# carries the ``legacy-v1`` comparison label (see ``baseline.py``) and is
# never re-labelled as a new alpha.
# ---------------------------------------------------------------------------

#: The fifteen v2-registered baselines (006 §2 adds opening_gap_points).
BASELINE_NAMES: tuple[str, ...] = (
    "close", "prior_high20", "prior_low20", "atr14", "er20", "vol20", "ma20",
    "ma60", "ma_spread_atr", "zscore20", "distance_high_atr", "distance_low_atr",
    "volume_ratio20", "oi_change5", "opening_gap_points",
)

#: The original fourteen items keep the v1 read-only comparison scope; the
#: new opening-gap baseline has no v1 counterpart and never gets one (006).
LEGACY_COMPARISON_NAMES: tuple[str, ...] = BASELINE_NAMES[:-1]

#: Verbatim version-1 registry definitions (read-only transcription).
BASELINE_DEFINITIONS: Mapping[str, str] = {
    "close": "Current completed close.",
    "prior_high20": "Maximum high of the prior 20 bars, excluding current.",
    "prior_low20": "Minimum low of the prior 20 bars, excluding current.",
    "atr14": ("Arithmetic mean of 14 true ranges max(high-low, "
              "abs(high-prior_close), abs(low-prior_close)); needs 15 bars."),
    "er20": ("Absolute 20-bar close displacement / sum of 20 absolute close moves; "
             "zero movement unavailable. Current regime heuristic: ER>=0.5 uses "
             "MA20-MA60 sign; ER<=0.3 range; otherwise transition. Not a prediction."),
    "vol20": "Sample standard deviation (ddof=1) of 20 simple close returns; zero variance unavailable.",
    "ma20": "Arithmetic mean of the last 20 closes including current.",
    "ma60": "Arithmetic mean of the last 60 closes including current.",
    "ma_spread_atr": "(MA20-MA60) / positive ATR14.",
    "zscore20": ("(Current close - mean of prior 20 closes) / prior 20 close sample "
                 "std (ddof=1); reference excludes current; zero variance unavailable."),
    "distance_high_atr": ("(Prior high20 - current close) / positive ATR14. Near high "
                          "when absolute distance <=0.5; nearest qualifying boundary "
                          "wins, high on a tie."),
    "distance_low_atr": ("(Current close - prior low20) / positive ATR14. Near low when "
                         "absolute distance <=0.5; nearest qualifying boundary wins, "
                         "high on a tie; otherwise middle."),
    "volume_ratio20": ("Current volume / mean of prior 20 volumes excluding current; "
                       "all 21 volumes finite nonnegative, denominator positive."),
    "oi_change5": ("Current close OI / close OI five bars earlier - 1; all six OI "
                   "values finite nonnegative, lagged OI positive."),
}

_BASELINE_LOOKBACKS: Mapping[str, int] = {
    "opening_gap_points": 2,
    "close": 1, "prior_high20": 21, "prior_low20": 21, "atr14": 15, "er20": 21,
    "vol20": 21, "ma20": 20, "ma60": 60, "ma_spread_atr": 60, "zscore20": 21,
    "distance_high_atr": 21, "distance_low_atr": 21, "volume_ratio20": 21,
    "oi_change5": 6,
}

_BASELINE_UNITS: Mapping[str, str] = {
    "opening_gap_points": "price_points",
    "close": "price_points", "prior_high20": "price_points",
    "prior_low20": "price_points", "atr14": "price_points", "er20": "ratio",
    "vol20": "return_std", "ma20": "price_points", "ma60": "price_points",
    "ma_spread_atr": "atr", "zscore20": "standard_deviations",
    "distance_high_atr": "atr", "distance_low_atr": "atr",
    "volume_ratio20": "ratio", "oi_change5": "fractional_change",
}

_BASELINE_PARAMETERS: Mapping[str, Mapping[str, JsonValue]] = {
    "opening_gap_points": {
        "return_kind": NOT_APPLICABLE, "std_ddof": NOT_APPLICABLE,
        "include_current": True, "window_unit": "SESSION_PAIR",
        "annualization": NOT_APPLICABLE, "annualization_factor": NOT_APPLICABLE,
        "session_boundary": "IMMEDIATE_PREVIOUS_SESSION",
        "zero_policy": "KEEP_VALID_ZERO",
        "value_scope": "CURRENT_SESSION", "prior_close_anchor": "EXACT_SESSION_END",
    },
    "close": {"include_current": True, "zero_policy": NOT_APPLICABLE, "window": 1},
    "prior_high20": {"include_current": False, "zero_policy": NOT_APPLICABLE,
                     "prior_window": 20},
    "prior_low20": {"include_current": False, "zero_policy": NOT_APPLICABLE,
                    "prior_window": 20},
    "atr14": {"include_current": True, "zero_policy": "degraded_zero_on_zero_true_range",
              "tr_window": 14},
    "er20": {"include_current": True, "zero_policy": "none_on_zero_total_path",
             "window": 20, "signed": False},
    "vol20": {"return_kind": "SIMPLE", "std_ddof": 1, "include_current": True,
              "annualization": "NOT_ANNUALIZED", "zero_policy": "none_on_zero_std",
              "window": 20},
    "ma20": {"include_current": True, "zero_policy": NOT_APPLICABLE, "window": 20},
    "ma60": {"include_current": True, "zero_policy": NOT_APPLICABLE, "window": 60},
    "ma_spread_atr": {"include_current": True, "zero_policy": "none_on_zero_atr"},
    "zscore20": {"std_ddof": 1, "include_current": False,
                 "zero_policy": "none_on_zero_std", "reference_window": 20},
    "distance_high_atr": {"include_current": False, "zero_policy": "none_on_zero_atr"},
    "distance_low_atr": {"include_current": False, "zero_policy": "none_on_zero_atr"},
    "volume_ratio20": {"include_current": False, "zero_policy": "invalid_on_zero_reference",
                       "reference_window": 20, "minus_one": False},
    "oi_change5": {"include_current": True, "zero_policy": "invalid_on_zero_lag_oi",
                   "lag": 5, "minus_one": True},
}

#: 006 §2 local definition object D of the opening-gap baseline; the
#: source hash is SHA256(M1 canonical JSON of D), shared by all durations.
OPENING_GAP_DEFINITION: Mapping[str, str] = {
    "definition_id": "BASE:opening_gap_points:v2.1.0",
    "operator_version": "opening-gap-v2.1.0",
    "formula": "session_open - prior_session_close",
    "unit": "price_points",
    "minute_session": "continuous_SessionWindow",
    "daily_session": "complete_trading_day",
    "current_open": "first_planned_complete_bar_open",
    "prior_close": "same_duration_bar_ending_at_immediate_prior_session_end",
    "later_bars": "same_session_gap",
    "availability_policy": "complete_and_PIT_visible",
    "missing_policy": "MISSING_with_reason_no_fallback",
    "calendar_policy": "M1_and_change_005",
}


def opening_gap_definition_sha256() -> str:
    """H(D) over the local definition object (006 §2), not a catalog record."""
    return hashlib.sha256(canonical_json_bytes(dict(OPENING_GAP_DEFINITION))).hexdigest()


_BASELINE_REQUIRED_FIELDS: Mapping[str, tuple[str, ...]] = {
    "opening_gap_points": ("open", "close"),
    "close": ("close",),
    "prior_high20": ("high",), "prior_low20": ("low",),
    "atr14": ("high", "low", "close"),
    "er20": ("close",), "vol20": ("close",),
    "ma20": ("close",), "ma60": ("close",),
    # ATR-denominated ratios consume high+low+close (the true range and
    # the closing anchor both enter the denominator, precheck E10).
    "ma_spread_atr": ("high", "low", "close"),
    "zscore20": ("close",),
    "distance_high_atr": ("high", "low", "close"),
    "distance_low_atr": ("high", "low", "close"),
    "volume_ratio20": ("volume",), "oi_change5": ("close_oi",),
}


def baseline_factor_key(name: str, duration_seconds: int) -> str:
    """Exact baseline key ``BASE:<name>:v2.1.0:d<duration_seconds>``."""
    if name not in BASELINE_NAMES:
        raise ValueError(f"unknown_baseline_name:{name}")
    if duration_seconds not in DURATIONS:
        raise ValueError(f"unsupported_duration_seconds:{duration_seconds}")
    return f"BASE:{name}:{CONTRACT_SCHEME_VERSION}:d{duration_seconds}"


def baseline_definition_sha256(name: str) -> str:
    """Binding hash of the transcribed version-1 registry definition."""
    if name not in BASELINE_DEFINITIONS:
        raise ValueError(f"unknown_baseline_name:{name}")
    payload = hashlib.sha256()
    payload.update(
        f'{{"source":"ak_trading.probability.factors.factor_registry",'
        f'"factor":"{name}","version":"1",'
        f'"definition":"{BASELINE_DEFINITIONS[name]}"}}'.encode("utf-8"))
    return payload.hexdigest()


def baseline_registry_v2(duration_seconds: int) -> tuple[FactorSpec, ...]:
    """The fifteen v2 baselines: fourteen legacy items plus opening gap."""
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int):
        raise TypeError("duration_seconds_must_be_int")
    if duration_seconds not in DURATIONS:
        raise ValueError(f"unsupported_duration_seconds:{duration_seconds}")
    specs: list[FactorSpec] = []
    for name in BASELINE_NAMES:
        if name == "opening_gap_points":
            # 006 §2: a local BASE definition with its own operator,
            # session-pair parameters and the H(D) source binding.
            specs.append(FactorSpec(
                factor_key=baseline_factor_key(name, duration_seconds),
                source_factor_id="BASE:opening_gap_points",
                operator_version="opening-gap-v2.1.0",
                parameter_version="session-pair-v1",
                duration_seconds=duration_seconds,
                lookback=2,  # two price endpoints, not the last two rows
                parameters=dict(_BASELINE_PARAMETERS[name]),
                unit="price_points",
                required_fields=("open", "close"),
                formula="session_open - prior_session_close",
                source_sha256=opening_gap_definition_sha256(),
            ))
            continue
        defaults: dict[str, JsonValue] = {
            "return_kind": NOT_APPLICABLE, "std_ddof": NOT_APPLICABLE,
        }
        parameters: dict[str, JsonValue] = {
            **defaults,
            **_BASELINE_PARAMETERS[name],
            "window_unit": "BARS",
            "annualization": _BASELINE_PARAMETERS[name].get("annualization", NOT_APPLICABLE),
            "annualization_factor": NOT_APPLICABLE,
            "session_boundary": "INCLUDE_GAP",
            "reference_label": "legacy-v1",
        }
        specs.append(FactorSpec(
            factor_key=baseline_factor_key(name, duration_seconds),
            source_factor_id=name,
            operator_version=BASELINE_OPERATOR_VERSION,
            parameter_version=BASELINE_PARAMETER_VERSION,
            duration_seconds=duration_seconds,
            lookback=_BASELINE_LOOKBACKS[name],
            parameters=parameters,
            unit=_BASELINE_UNITS[name],
            required_fields=_BASELINE_REQUIRED_FIELDS[name],
            formula=BASELINE_DEFINITIONS[name],
            source_sha256=baseline_definition_sha256(name),
        ))
    return tuple(sorted(specs, key=lambda spec: spec.factor_key))
