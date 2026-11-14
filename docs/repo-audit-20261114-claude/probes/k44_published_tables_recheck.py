#!/usr/bin/env python3
"""六品种日线基准（K43、K44）已公布表格的算术核对。

只读、只用标准库。不读行情、不跑引擎，只核对仓库里已公布的 CSV、报告和 CHECK.json
相互之间是否一致。输入明细不在仓库里，所以这不是从源数据的独立复算；
独立复算由项目内的独立审核者完成（状态页记录为 253,816,634 项）。

核对内容：
  A. 螺纹钢在六品种目录里的副本，与已接受的螺纹钢目录逐字节相同
  B. 主力口径：分年净额之和 = 全期净额；为正的年数、有覆盖的年数、最差年份和金额；
     覆盖观察数之和；覆盖率的四舍六入
  C. 全部合约口径：净额 = 毛额 − 手续费 − 滑点；盈/平/亏账户数之和 = 账户数；
     分年之和 = 全期；相对基准情景、相对 100 万元档的差
  D. 对照比较：规则减“始终空仓” = 规则净额；规则减“买入持有” = 两者之差
  E. 100 万元档（没有开仓被拦）下的情景恒等式：手续费加倍 → 手续费恰为两倍；
     滑点加倍 → 滑点恰为两倍；保证金加半 → 净额不变
  F. 总表 summary.csv 与各品种明细一致
  G. 六品种报告正文的五张表与 CSV、配置逐格一致；螺纹钢报告里主力口径的三张表与 CSV 逐格一致
  H. CHECK.json 里登记的每个引用格子与 CSV 一致；登记的已发布文件哈希与仓库里的文件一致

另外列出一条“注意”（不计入不一致）：CHECK.json 里登记的工具、配置、测试文件的哈希，
与仓库里现在的文件是否相同。

核对通过后打印三张“读报告用”的派生表（不是核对项）。

在仓库根目录运行：
    python docs/repo-audit-20261114-claude/probes/k44_published_tables_recheck.py
退出码 0 = 全部一致；1 = 有不一致（逐条列出）。
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from decimal import Decimal as D, ROUND_HALF_EVEN
from pathlib import Path

for parent in (*Path(__file__).resolve().parents, Path.cwd()):
    if (parent / "docs" / "research" / "v1-daily-baseline-universe").is_dir():
        ROOT = parent
        break
else:
    sys.exit("找不到 docs/research/v1-daily-baseline-universe，请在仓库根目录运行")

U = ROOT / "docs" / "research" / "v1-daily-baseline-universe"
RB = ROOT / "docs" / "research" / "v1-daily-baseline-rb"
REPORT = ROOT / "docs" / "research" / "v1-daily-baseline-universe.md"
RB_REPORT = ROOT / "docs" / "research" / "v1-daily-baseline-rb.md"
CONFIG = ROOT / "config" / "research" / "v1_daily_baseline_universe.json"

NEW = ("INE.SC", "DCE.M", "CZCE.TA", "CFFEX.IF", "GFEX.SI")
PRODUCTS = ("SHFE.RB",) + NEW
STRATEGIES = ("donchian_trend", "ma_trend", "zscore_reversion")
METHODS = STRATEGIES + ("flat_control", "buy_hold_control")
CAPITALS = ("1000000", "300000", "100000", "50000")
SCENARIOS = ("BASE", "FEE_X2", "SLIPPAGE_X2", "MARGIN_X1_5")
YEARS = tuple(str(y) for y in range(2020, 2027))
SUM_FIELDS = ("net_sum", "gross_sum", "fee_sum", "slippage_sum", "observations",
              "opening_attempts", "opening_fills", "opening_blocks", "fills",
              "terminal_close_fills")

counts: Counter = Counter()
failures: list[str] = []


def check(group: str, condition: bool, message: str) -> None:
    counts[group] += 1
    if not condition:
        failures.append(f"[{group}] {message}")


def table(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def dec(value: str) -> D:
    return D(value) if value != "" else D(0)


def sha(path: Path) -> tuple[int, str]:
    raw = path.read_bytes()
    return len(raw), hashlib.sha256(raw).hexdigest()


def text(value: D) -> str:
    if value == 0:
        return "0"
    shown = format(value, "f")
    return shown.rstrip("0").rstrip(".") if "." in shown else shown


# ---------------------------------------------------------------- 读入
dominant_summary, dominant_year, dominant_scope = {}, {}, {}
sens_summary, sens_year, control = {}, {}, {}
for product in PRODUCTS:
    folder = U / product
    rows = table(folder / "dominant-summary.csv")
    for row in rows:
        key = (row.get("capital", "1000000"), row.get("scenario", "BASE"), row["method"])
        dominant_summary[(product, *key)] = row
    for row in table(folder / "dominant-year.csv"):
        key = (product, row.get("capital", "1000000"), row.get("scenario", "BASE"), row["method"])
        dominant_year.setdefault(key, []).append(row)
    scope_rows = table(folder / "dominant-scope.csv")
    if product == "SHFE.RB":
        dominant_scope[product] = {r["metric"]: r["value"] for r in scope_rows}
    else:
        dominant_scope[product] = scope_rows[0]
    for row in table(folder / "sensitivity-summary.csv"):
        sens_summary[(product, row["capital"], row["scenario"], row["method"], row["view"])] = row
    for row in table(folder / "sensitivity-year.csv"):
        key = (product, row["capital"], row["scenario"], row["method"], row["view"])
        sens_year.setdefault(key, []).append(row)
    for row in table(folder / "control-comparison.csv"):
        control[(product, row["capital"], row["scenario"], row["strategy"], row["control"],
                 row["view"], row["scope"])] = row

# ---------------------------------------------------------------- A
for name in ("dominant-summary.csv", "dominant-year.csv", "dominant-scope.csv",
             "sensitivity-summary.csv", "sensitivity-year.csv", "control-comparison.csv"):
    check("A 螺纹钢副本", (U / "SHFE.RB" / name).read_bytes() == (RB / name).read_bytes(),
          f"SHFE.RB/{name} 与 v1-daily-baseline-rb/{name} 不同")

# ---------------------------------------------------------------- B
for (product, capital, scenario, method), row in dominant_summary.items():
    tag = f"{product}/{capital}/{scenario}/{method}"
    years = dominant_year[(product, capital, scenario, method)]
    check("B 主力口径", len(years) == 7, f"{tag} 分年行数 {len(years)}")
    covered = [y for y in years if y["status"] == "COVERED"]
    check("B 主力口径", all(y["net"] == "" for y in years if y["status"] != "COVERED"),
          f"{tag} 无覆盖年份出现了金额")
    total = sum((dec(y["net"]) for y in covered), D(0))
    check("B 主力口径", total == dec(row["net"]), f"{tag} 分年之和 {total} ≠ 全期 {row['net']}")
    check("B 主力口径", sum(dec(y["net"]) > 0 for y in covered) == int(row["positive_years"]),
          f"{tag} 为正的年数")
    check("B 主力口径", len(covered) == int(row["covered_years"]), f"{tag} 有覆盖的年数")
    if covered:
        worst = min(covered, key=lambda y: (dec(y["net"]), int(y["year"])))
        check("B 主力口径", worst["year"] == row["worst_year"]
              and dec(worst["net"]) == dec(row["worst_year_net"]), f"{tag} 最差年份")
    check("B 主力口径", sum(int(y["covered_observations"]) for y in years)
          == int(row["covered_observations"]), f"{tag} 覆盖观察数之和")
    check("B 主力口径", sum(int(y["total_observations"]) for y in years)
          == int(row["total_observations"]), f"{tag} 全部观察数之和")
    percent = (D(row["covered_observations"]) * 100 / D(row["total_observations"])).quantize(
        D("0.000001"), rounding=ROUND_HALF_EVEN)
    check("B 主力口径", percent == D(row["coverage_percent"]), f"{tag} 覆盖率")
    check("B 主力口径", dec(row["max_drawdown"]) >= 0
          and (dec(row["max_drawdown"]) >= -dec(row["net"]) or dec(row["net"]) >= 0),
          f"{tag} 最大回撤小于期末亏损")
    all_row = sens_summary[(product, capital, scenario, method, "ALL")]
    check("B 主力口径", dec(row["all_accounts_net"]) == dec(all_row["net_sum"])
          and row["all_accounts"] == all_row["n"], f"{tag} 全部合约净额与敏感性表不一致")
    mean = (dec(row["all_accounts_net"]) / D(row["all_accounts"])).quantize(
        D("0.001"), rounding=ROUND_HALF_EVEN)
    check("B 主力口径", mean == D(row["all_accounts_mean_display"]), f"{tag} 账户均值")
check("B 主力口径", len(dominant_summary) == 5 + 5 * 80,
      f"主力口径行数 {len(dominant_summary)}，应为 405")

# ---------------------------------------------------------------- C
for key, row in sens_summary.items():
    product, capital, scenario, method, view = key
    tag = "/".join(key)
    check("C 全部合约口径", dec(row["net_sum"]) == dec(row["gross_sum"]) - dec(row["fee_sum"])
          - dec(row["slippage_sum"]), f"{tag} 净额 ≠ 毛额 − 手续费 − 滑点")
    check("C 全部合约口径", int(row["profit"]) + int(row["zero"]) + int(row["loss"]) == int(row["n"]),
          f"{tag} 盈平亏之和 ≠ 账户数")
    check("C 全部合约口径", int(row["opening_attempts"]) == int(row["opening_fills"])
          + int(row["opening_blocks"]), f"{tag} 开仓尝试 ≠ 成交 + 被拦")
    years = sens_year[key]
    for field in SUM_FIELDS:
        check("C 全部合约口径", sum((dec(y[field]) for y in years), D(0)) == dec(row[field]),
              f"{tag} {field} 分年之和 ≠ 全期")
    base = sens_summary[(product, capital, "BASE", method, view)]
    check("C 全部合约口径", dec(row["vs_same_capital_BASE_net_sum"])
          == dec(row["net_sum"]) - dec(base["net_sum"]), f"{tag} 相对基准情景的差")
    top = sens_summary[(product, "1000000", scenario, method, view)]
    check("C 全部合约口径", dec(row["vs_same_scenario_C1000000_net_sum"])
          == dec(row["net_sum"]) - dec(top["net_sum"]), f"{tag} 相对 100 万元档的差")
check("C 全部合约口径", len(sens_summary) == 6 * 160, f"敏感性汇总行数 {len(sens_summary)}，应为 960")

# ---------------------------------------------------------------- D
for key, row in control.items():
    product, capital, scenario, strategy, other, view, scope = key
    tag = "/".join(key)
    if scope == "ALL_YEARS":
        left = dec(sens_summary[(product, capital, scenario, strategy, view)]["net_sum"])
        right = dec(sens_summary[(product, capital, scenario, other, view)]["net_sum"])
    else:
        def year_net(method):
            rows = [y for y in sens_year[(product, capital, scenario, method, view)]
                    if y["raw_year"] == scope]
            return dec(rows[0]["net_sum"]) if rows else D(0)
        left, right = year_net(strategy), year_net(other)
    check("D 对照比较", dec(row["net_sum"]) == left - right, f"{tag} 差额 {row['net_sum']} ≠ {left - right}")

# ---------------------------------------------------------------- E
for product in PRODUCTS:
    for method in METHODS:
        for view in ("ALL", "PRIMARY"):
            base = sens_summary[(product, "1000000", "BASE", method, view)]
            fee = sens_summary[(product, "1000000", "FEE_X2", method, view)]
            slip = sens_summary[(product, "1000000", "SLIPPAGE_X2", method, view)]
            margin = sens_summary[(product, "1000000", "MARGIN_X1_5", method, view)]
            tag = f"{product}/{method}/{view}"
            if all(int(r["opening_blocks"]) == 0 for r in (base, fee, slip, margin)):
                check("E 情景恒等式", dec(fee["fee_sum"]) == 2 * dec(base["fee_sum"])
                      and dec(fee["gross_sum"]) == dec(base["gross_sum"])
                      and dec(fee["slippage_sum"]) == dec(base["slippage_sum"]), f"{tag} 手续费加倍")
                check("E 情景恒等式", dec(slip["slippage_sum"]) == 2 * dec(base["slippage_sum"])
                      and dec(slip["gross_sum"]) == dec(base["gross_sum"])
                      and dec(slip["fee_sum"]) == dec(base["fee_sum"]), f"{tag} 滑点加倍")
                check("E 情景恒等式", dec(margin["net_sum"]) == dec(base["net_sum"])
                      and margin["fills"] == base["fills"], f"{tag} 保证金加半")

# ---------------------------------------------------------------- F
summary = table(U / "summary.csv")
check("F 总表", len(summary) == 30, f"总表行数 {len(summary)}")
for row in summary:
    source = dominant_summary[(row["product"], row["capital"], row["scenario"], row["method"])]
    tag = f"{row['product']}/{row['method']}"
    for left, right in (("dominant_net", "net"), ("all_accounts_net", "all_accounts_net"),
                        ("dominant_positive_years", "positive_years"),
                        ("dominant_covered_years", "covered_years"),
                        ("dominant_worst_year", "worst_year"),
                        ("dominant_worst_year_net", "worst_year_net"),
                        ("dominant_max_drawdown", "max_drawdown"),
                        ("dominant_status", "status")):
        check("F 总表", row[left] == source[right], f"{tag} {left}：{row[left]} ≠ {source[right]}")

# ---------------------------------------------------------------- G
availability = {r["product"]: r for r in table(U / "availability.csv")}
config = json.loads(CONFIG.read_text(encoding="utf-8"))


def markdown_tables(path: Path) -> list[list[list[str]]]:
    found, current = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
                current.append(cells)
        elif current:
            found.append(current)
            current = []
    if current:
        found.append(current)
    return found


names = {r["method_name"]: r["method"] for r in summary}
md = markdown_tables(REPORT)
check("G 报告正文", len(md) == 5, f"报告里的表格数 {len(md)}，应为 5")
if len(md) == 5:
    first, avail_md, matrix_md, coverage_md, spec_md = md
    check("G 报告正文", len(first) - 1 == 30, f"首表行数 {len(first) - 1}")
    for cells in first[1:]:
        product, method = cells[0], names[cells[1]]
        source = dominant_summary[(product, cells[2], cells[3], method)]
        expected = [source["net"], source["all_accounts_net"], source["positive_years"],
                    source["covered_years"], source["worst_year"], source["worst_year_net"],
                    source["max_drawdown"], source["status"]]
        check("G 报告正文", cells[4:] == expected, f"首表 {product}/{method}：{cells[4:]} ≠ {expected}")
    for cells in avail_md[1:]:
        source = availability[cells[0]]
        expected = [source["declared_contracts"], source["contracts_with_bars"],
                    source["contracts_without_bars"], source["bar_rows"], source["start_raw_date"],
                    source["end_raw_date"], source["status"], source["reason_code"]]
        check("G 报告正文", cells[1:] == expected, f"可用性表 {cells[0]}")
    check("G 报告正文", len(matrix_md) - 1 == 5 * 15 * 5, f"情景表行数 {len(matrix_md) - 1}，应为 375")
    for cells in matrix_md[1:]:
        product, capital, scenario, method = cells[0], cells[1], cells[2], names[cells[3]]
        source = dominant_summary[(product, capital, scenario, method)]
        expected = [source["net"], source["all_accounts_net"], source["longest_drawdown_observations"]]
        check("G 报告正文", cells[4:] == expected,
              f"情景表 {product}/{capital}/{scenario}/{method}：{cells[4:]} ≠ {expected}")
    for cells in coverage_md[1:]:
        scope = dominant_scope[cells[0]]
        expected = [scope["covered_observations"], scope["observations"],
                    scope["coverage_percent"], scope["switches"]]
        check("G 报告正文", cells[1:] == expected, f"覆盖表 {cells[0]}：{cells[1:]} ≠ {expected}")
    for cells in spec_md[1:]:
        economics = config["economics"][cells[0]]
        check("G 报告正文", cells[2] == economics["multiplier"] and cells[3] == economics["price_tick"]
              and cells[4].strip("`") == economics["official_source_sha256"], f"规格表 {cells[0]}")

# 螺纹钢报告：主力口径的三张表（总表、主力与账户均值、分年）
RB_NAMES = {"唐奇安突破": "donchian_trend", "均线": "ma_trend", "Z 分数回归": "zscore_reversion",
            "买入持有": "buy_hold_control", "始终空仓": "flat_control"}
rb_md = markdown_tables(RB_REPORT)
check("G 报告正文", len(rb_md) >= 3, f"螺纹钢报告里的表格数 {len(rb_md)}")
if len(rb_md) >= 3:
    rb_first, rb_mean, rb_year = rb_md[0], rb_md[1], rb_md[2]
    for cells in rb_first[1:]:
        source = dominant_summary[("SHFE.RB", "1000000", "BASE", RB_NAMES[cells[0].split("·")[1].strip()])]
        expected = [source["net"], source["all_accounts_net"],
                    source["positive_years"] + "/" + source["covered_years"], source["worst_year"],
                    source["worst_year_net"], source["max_drawdown"]]
        check("G 报告正文", cells[1:] == expected, f"螺纹钢总表 {cells[0]}：{cells[1:]} ≠ {expected}")
    for cells in rb_mean[1:]:
        source = dominant_summary[("SHFE.RB", "1000000", "BASE", RB_NAMES[cells[0]])]
        expected = [source["net"], source["all_accounts_mean_display"],
                    source["longest_drawdown_observations"],
                    "是" if source["open_drawdown_at_end"] == "true" else "否"]
        check("G 报告正文", cells[1:] == expected, f"螺纹钢均值表 {cells[0]}：{cells[1:]} ≠ {expected}")
    order = ("donchian_trend", "ma_trend", "zscore_reversion", "buy_hold_control", "flat_control")
    for cells in rb_year[1:]:
        year = cells[0].rstrip("*")
        expected = []
        for method in order:
            row = [y for y in dominant_year[("SHFE.RB", "1000000", "BASE", method)] if y["year"] == year][0]
            expected.append(row["net"])
        expected.append(row["covered_observations"])
        check("G 报告正文", cells[1:] == expected, f"螺纹钢分年表 {year}：{cells[1:]} ≠ {expected}")

# ---------------------------------------------------------------- H
check_json = json.loads((U / "CHECK.json").read_text(encoding="utf-8"))
cache: dict[str, list[dict[str, str]]] = {}
for ref in check_json["report_value_refs"]:
    rows = cache.setdefault(ref["csv"], table(U / ref["csv"]))
    index = ref["row"] - 2
    ok = 0 <= index < len(rows) and rows[index].get(ref["column"]) == ref["value"]
    check("H CHECK 引用", ok, f"{ref['csv']} 第 {ref['row']} 行 {ref['column']} ≠ {ref['value']}")
for name, expected in check_json["payloads"].items():
    relative = name.removeprefix("publish/")
    target = ROOT / relative
    check("H CHECK 引用", target.is_file() and sha(target) == (expected["bytes"], expected["sha256"]),
          f"已发布文件与登记的哈希不一致：{relative}")
for key, expected in (("holdout_read", False), ("new_training", False),
                      ("evidence_level", "SCENARIO"), ("statistical_qualification", "NOT_ASSESSED")):
    check("H CHECK 引用", check_json.get(key) == expected, f"CHECK.json {key} = {check_json.get(key)!r}")
check("H CHECK 引用", check_json["safety"] == {"broker": "NOT_RUN", "enable_trading": False,
                                             "observer": "hold", "real_order_cancel_calls": 0,
                                             "real_order_send_calls": 0}, "CHECK.json 安全字段")
for product in PRODUCTS:
    end = availability[product]["end_raw_date"]
    check("H CHECK 引用", end <= "2026-09-04", f"{product} 最后一根 K 线 {end} 晚于 2026-09-04")

# ---------------------------------------------------------------- 结果
total = sum(counts.values())
print("核对项数：")
for group in sorted(counts):
    print(f"  {group:<14} {counts[group]:>7,}")
print(f"  {'合计':<14} {total:>7,}，不一致 {len(failures)}")
for line in failures[:60]:
    print("  ✗", line)
stale = [name for name, expected in check_json["build_files"].items()
         if not (ROOT / name).is_file() or sha(ROOT / name) != (expected["bytes"], expected["sha256"])]
print(f"注意：CHECK.json 登记了 {len(check_json['build_files'])} 个构建文件的哈希，"
      f"其中 {len(stale)} 个与仓库里现在的文件不同" + ("：" + "、".join(stale) if stale else ""))
if failures:
    sys.exit(1)

# ---------------------------------------------------------------- 派生表（不是核对项）
LABEL = {"donchian_trend": "突破", "ma_trend": "均线", "zscore_reversion": "回归",
         "buy_hold_control": "买入持有", "flat_control": "空仓"}
print()
print("派生表 1  100 万元、基准成本：主力口径净额，以及它相当于一手名义金额的百分之几")
print("  一手名义金额 = 全部合约口径下平均每次成交的金额，由手续费反推：")
print("  (手续费合计 − 0.01 × 成交次数) ÷ 0.0001 ÷ 成交次数")
print(f"  {'品种':<9}{'一手名义金额':>12}  " + "".join(f"{LABEL[m]:>16}" for m in STRATEGIES + ("buy_hold_control",)))
notional = {}
for product in PRODUCTS:
    rows = [sens_summary[(product, "1000000", "BASE", m, "ALL")] for m in STRATEGIES]
    fills = sum(int(r["fills"]) for r in rows)
    fees = sum((dec(r["fee_sum"]) for r in rows), D(0))
    notional[product] = (fees - D("0.01") * fills) / D("0.0001") / fills
    cells = []
    for method in STRATEGIES + ("buy_hold_control",):
        net = dec(dominant_summary[(product, "1000000", "BASE", method)]["net"])
        cells.append(f"{net:>10,.0f}{100 * net / notional[product]:>+5.0f}%")
    print(f"  {product:<9}{notional[product]:>12,.0f}  " + "".join(f"{c:>16}" for c in cells))

positive = sum(dec(dominant_summary[(p, "1000000", "BASE", m)]["net"]) > 0
               for p in PRODUCTS for m in STRATEGIES)
beat = sum(dec(dominant_summary[(p, "1000000", "BASE", m)]["net"])
           > dec(dominant_summary[(p, "1000000", "BASE", "buy_hold_control")]["net"])
           for p in PRODUCTS for m in STRATEGIES)
print(f"  18 个“品种 × 规则”里：主力口径净额为正的 {positive} 个；高于买入持有的 {beat} 个")

print()
print("派生表 2  100 万元、基准成本、全部合约口径：规则实际交易了多少")
print(f"  {'品种':<9}{'合约数':>6}{'超过 60 根的合约':>12}  "
      + "".join(f"{LABEL[m] + ' 开仓次数/从未开仓':>24}" for m in STRATEGIES))
for product in PRODUCTS:
    cells = []
    for method in STRATEGIES:
        row = sens_summary[(product, "1000000", "BASE", method, "ALL")]
        cells.append(f"{row['opening_fills']:>6} / {row['never_opened_accounts']:>2}")
    n = sens_summary[(product, "1000000", "BASE", "flat_control", "ALL")]["n"]
    n60 = sens_summary[(product, "1000000", "BASE", "flat_control", "PRIMARY")]["n"]
    print(f"  {product:<9}{n:>6}{n60:>12}  " + "".join(f"{c:>24}" for c in cells))

print()
print("派生表 3  主力口径，按“品种 × 年份”数：净额为正 / 为负 / 为零（只数有覆盖的年份）")
for method in STRATEGIES + ("buy_hold_control",):
    tally = Counter()
    for product in PRODUCTS:
        for row in dominant_year[(product, "1000000", "BASE", method)]:
            if row["status"] == "COVERED":
                value = dec(row["net"])
                tally["正" if value > 0 else "负" if value < 0 else "零"] += 1
    total_years = sum(tally.values())
    print(f"  {LABEL[method]:<6} 正 {tally['正']:>2} / 负 {tally['负']:>2} / 零 {tally['零']:>2}（共 {total_years} 个）")
sys.exit(0)
