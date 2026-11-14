#!/usr/bin/env python3
"""六品种报告第二版（K46）：已入库的表格之间、表格与报告正文之间是否对得上。

仓库里没有逐合约的逐日账本，这里核对不了“每个品种每天的折算值算得对不对”
（那一步由项目内的独立审核者用仓库外的输入逐格复算）。这个探针只用已入库的文件，
核对它们互相是否一致：

  甲  CHECK.json 登记的工具、配置、测试和 9 个发布文件的大小、SHA-256 等于仓库里的文件；
      登记的各表行数和列名等于实际的 CSV
  乙  第二版 summary.csv 的净额、正年数、最差年、最大回撤（元）、主力日数
      等于第一版各品种 dominant-summary.csv（100 万元、BASE）
  丙  第二版 annual.csv 的逐年净额和天数等于第一版 dominant-year.csv；
      逐年相加等于全期（净额、折算单位）；正年数和最差年按逐年数重数一遍
  丁  readiness.csv：各类天数相加等于主力日数；占比、是否计入按 1/2 门槛重算；
      全部账户的开仓次数等于第一版 sensitivity-summary.csv
  戊  capital-events.csv 四档资金的尝试/成交/拦下次数等于第一版 sensitivity-summary.csv
  己  combined-daily.csv 逐行重算：每天等权（半偶舍入）、累计、回撤、回撤天数
  庚  由 combined-daily.csv 重算 combined-summary.csv 和 combined-annual.csv
  辛  交叉核对：某个做法某一年里，各品种的年度折算单位相加，等于合并表里
      “当天各品种之和”逐日相加（整数，逐位相等）；各品种参与的天数等于它的主力日数
  壬  报告正文的五张表和第一屏的数字，等于对应 CSV 四舍五入后的值；facts.csv 重数一遍

最后另算一张表（不是核对，是我自己的描述性计算，报告里没有）：合并口径的逐日序列
波动有多大、全期合计的 95% 区间有多宽（20 个交易日一块的循环块自助抽样，20,000 次，
种子 20261114）。它回答的是“+21% 这样的数和运气分不分得开”。

只读，只用标准库。在仓库根目录运行（约 5 秒）：
    python docs/repo-audit-20261114-r8-claude/probes/k46_published_tables_recheck.py

退出码 0 = 全部一致；退出码 1 = 有不一致（逐条列出，最多 40 条）。
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import re
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

ROOT = next(parent for parent in (*Path(__file__).resolve().parents, Path.cwd())
            if (parent / "src" / "ak_trading").is_dir())
BASE = ROOT / "docs" / "research" / "v1-daily-baseline-universe"
V2 = BASE / "v2"
REPORT = ROOT / "docs" / "research" / "v1-daily-baseline-universe.md"
PRODUCTS = {"SHFE.RB": "螺纹钢", "INE.SC": "原油", "DCE.M": "豆粕", "CZCE.TA": "PTA",
            "CFFEX.IF": "沪深300股指", "GFEX.SI": "工业硅"}
METHODS = {"donchian_trend": "突破", "ma_trend": "均线", "zscore_reversion": "回归",
           "flat_control": "始终空仓", "buy_hold_control": "一直持有"}
RULES = ("donchian_trend", "ma_trend", "zscore_reversion")
YEAR_STATUS = {"PARTIAL_YEAR": "部分年度", "FULL_OBSERVED_YEAR": "已有全年观察"}
SCALE = Decimal(10) ** 22                # 折算单位 ÷ 10^22 = 百分数

checks: dict[str, int] = defaultdict(int)
problems: list[str] = []


def check(group: str, condition: bool, message: str) -> None:
    checks[group] += 1
    if not condition:
        problems.append(f"[{group}] {message}")


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def dec(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def half_even(numerator: int, denominator: int) -> int:
    quotient, remainder = divmod(numerator, denominator)
    if 2 * remainder > denominator or (2 * remainder == denominator and quotient % 2):
        quotient += 1
    return quotient


def near(shown: str, exact: Decimal, places: int) -> bool:
    return abs(dec(shown) - exact) <= Decimal(5) / Decimal(10) ** (places + 1)


def group_a() -> None:
    manifest = json.loads((V2 / "CHECK.json").read_text(encoding="utf-8"))
    for section in ("build_files", "published_files"):
        for name, entry in manifest[section].items():
            path = ROOT / name
            data = path.read_bytes() if path.is_file() else b""
            check("甲", path.is_file() and len(data) == entry["bytes"]
                  and hashlib.sha256(data).hexdigest() == entry["sha256"],
                  f"{section} {name}：大小或 SHA-256 与 CHECK.json 不同")
    for name, entry in manifest["tables"].items():
        with (V2 / name).open(encoding="utf-8", newline="") as handle:
            table = list(csv.reader(handle))
        check("甲", table[0] == entry["columns"] and len(table) - 1 == entry["rows"],
              f"{name}：列名或行数与 CHECK.json 不同")


def first_edition(name: str) -> dict[tuple, dict[str, str]]:
    table = {}
    for product in PRODUCTS:
        for row in rows(BASE / product / name):
            # 螺纹钢目录是 K40 报告的原样副本：主力表没有资金和成本两列（就是 100 万元、BASE）
            key = [row.get("product", product), row.get("capital", "1000000"),
                   row.get("scenario", "BASE"), row["method"]]
            if "view" in row:
                key.append(row["view"])
            if "year" in row:
                key.append(row["year"])
            table[tuple(key)] = row
    return table


def main() -> int:
    group_a()
    summary = {(row["product"], row["method"]): row for row in rows(V2 / "summary.csv")}
    annual = defaultdict(dict)
    for row in rows(V2 / "annual.csv"):
        annual[(row["product"], row["method"])][row["year"]] = row
    readiness = {(row["product"], row["method"]): row for row in rows(V2 / "readiness.csv")}
    old_summary = first_edition("dominant-summary.csv")
    old_year = first_edition("dominant-year.csv")
    old_events = first_edition("sensitivity-summary.csv")

    # 乙、丙
    for (product, method), row in summary.items():
        old = old_summary[(product, "1000000", "BASE", method)]
        label = f"{product} {method}"
        check("乙", dec(row["net_yuan"]) == dec(old["net"]), f"{label} 净额 {row['net_yuan']} ≠ 第一版 {old['net']}")
        check("乙", row["positive_years"] == old["positive_years"] and row["covered_years"] == old["covered_years"],
              f"{label} 正年数/覆盖年数与第一版不同")
        check("乙", row["worst_year"] == old["worst_year"]
              and dec(row["worst_year_net_yuan"]) == dec(old["worst_year_net"]), f"{label} 最差年与第一版不同")
        check("乙", dec(row["max_drawdown_yuan"]) == dec(old["max_drawdown"]), f"{label} 最大回撤（元）与第一版不同")
        check("乙", row["selected_days"] == old["covered_observations"], f"{label} 主力日数与第一版不同")
        check("乙", dec(row["normalized_percent"]) * SCALE == dec(row["normalized_units"]),
              f"{label} 折算百分数与折算单位不对应")
        years = annual[(product, method)]
        nets = {year: dec(item["net_yuan"]) for year, item in years.items() if item["net_yuan"]}
        for year, item in years.items():
            old_row = old_year[(product, "1000000", "BASE", method, year)]
            if year not in nets:                      # 上市之前的年份：两版都留空
                check("丙", item["year_status"] == "NO_COVERAGE" and item["covered_days"] == "0"
                      and old_row["net"] == "" and item["normalized_units"] == "",
                      f"{label} {year} 年没有净额，但不是“无覆盖”")
                continue
            check("丙", nets[year] == dec(old_row["net"]) and item["covered_days"] == old_row["covered_observations"],
                  f"{label} {year} 年净额或天数与第一版不同")
            check("丙", dec(item["normalized_percent"]) * SCALE == dec(item["normalized_units"]),
                  f"{label} {year} 年折算百分数与折算单位不对应")
        check("丙", sum(nets.values()) == dec(row["net_yuan"]), f"{label} 逐年净额相加 ≠ 全期")
        check("丙", sum(int(item["normalized_units"] or 0) for item in years.values()) == int(row["normalized_units"]),
              f"{label} 逐年折算单位相加 ≠ 全期")
        check("丙", sum(value > 0 for value in nets.values()) == int(row["positive_years"]), f"{label} 正年数重数不同")
        worst = min(sorted(nets), key=lambda year: nets[year])
        check("丙", worst == row["worst_year"] and nets[worst] == dec(row["worst_year_net_yuan"]),
              f"{label} 最差年重数为 {worst}")
        check("丙", sum(int(item["covered_days"]) for item in years.values()) == int(row["selected_days"]),
              f"{label} 逐年天数相加 ≠ 主力日数")

    # 丁
    for (product, method), row in readiness.items():
        label = f"{product} {method}"
        events = old_events[(product, "1000000", "BASE", method, "ALL")]
        for mine, theirs in (("all_contracts", "n"), ("all_contracts_never_opened", "never_opened_accounts"),
                             ("all_opening_attempts", "opening_attempts"), ("all_opening_fills", "opening_fills"),
                             ("all_opening_blocks", "opening_blocks"),
                             ("all_opening_margin_blocked", "opening_margin_blocked"),
                             ("all_opening_costs_exhaust_equity", "opening_costs_exhaust_equity")):
            check("丁", row[mine] == events[theirs], f"{label} {mine}={row[mine]} ≠ 第一版 {theirs}={events[theirs]}")
        total = summary[(product, method)]
        check("丁", row["selected_opening_fills"] == total["selected_opening_fills"]
              and row["eligibility"] == total["eligibility"], f"{label} readiness 与 summary 的开仓数或资格不同")
        if method not in RULES:
            check("丁", row["readiness_status"] == "NOT_APPLICABLE" and row["eligibility"] == "INCLUDED",
                  f"{label} 对照不该有可计算日门槛")
            continue
        parts = [int(row[name]) for name in ("evaluator_ready_days", "warmup_days", "zero_variance_days",
                                             "other_unavailable_days")]
        denominator = int(row["readiness_denominator"])
        ready = int(row["ready_days"])
        check("丁", sum(parts) == denominator == int(total["selected_days"]), f"{label} 各类天数相加 ≠ 主力日数")
        check("丁", ready == parts[0] == int(total["ready_days"]), f"{label} 可计算日数不一致")
        reasons = json.loads(row["reason_counts_json"])
        check("丁", sum(sum(bucket.values()) for bucket in reasons.values()) == denominator
              and sum(reasons.get("EVALUATOR_READY", {}).values()) == ready, f"{label} 原因计数相加不对")
        percent = Decimal(half_even(ready * 100 * 10 ** 24, denominator)) / Decimal(10) ** 24
        check("丁", dec(row["readiness_percent"]) == percent == dec(total["readiness_percent"]),
              f"{label} 可计算日占比重算为 {percent}")
        check("丁", (row["eligibility"] == "INCLUDED") == (2 * ready >= denominator), f"{label} 是否计入与 1/2 门槛不符")

    # 戊
    for row in rows(V2 / "capital-events.csv"):
        events = old_events[(row["product"], row["capital"], row["scenario"], row["method"], row["view"])]
        for mine, theirs in (("accounts", "n"), ("opening_attempts", "opening_attempts"),
                             ("opening_fills", "opening_fills"), ("opening_blocks", "opening_blocks"),
                             ("opening_margin_blocked", "opening_margin_blocked"),
                             ("opening_costs_exhaust_equity", "opening_costs_exhaust_equity")):
            check("戊", row[mine] == events[theirs],
                  f"{row['product']} {row['capital']} {row['method']} {mine}={row[mine]} ≠ 第一版 {events[theirs]}")

    # 己、庚、辛
    daily = defaultdict(list)
    for row in rows(V2 / "combined-daily.csv"):
        daily[row["method"]].append(row)
    combined = {row["method"]: row for row in rows(V2 / "combined-summary.csv")}
    combined_year = {(row["method"], row["year"]): row for row in rows(V2 / "combined-annual.csv")}
    for method, series in daily.items():
        cumulative = peak = streak = 0
        longest = 0
        deepest = Decimal(0)
        year_state: dict[str, dict] = {}
        participation: dict[tuple[str, str], int] = defaultdict(int)
        product_sum: dict[str, int] = defaultdict(int)
        seen: set[str] = set()
        for row in series:
            year = row["raw_date"][:4]
            state = year_state.setdefault(year, {"units": 0, "days": 0, "cum": 0, "peak": 0, "streak": 0,
                                                 "longest": 0, "deepest": 0, "low": 99, "high": 0})
            count = int(row["participant_count"])
            names = [name for name in row["participants"].split("|") if name]
            label = f"{method} {row['raw_date']}"
            check("己", len(names) == count and (row["status"] == "COVERED") == (count > 0), f"{label} 参与品种数与名单不符")
            units = 0
            if count:
                units = half_even(int(row["sum_product_units"]), count)
                check("己", units == int(row["normalized_units"]), f"{label} 等权值重算为 {units}")
                check("己", dec(row["normalized_percent"]) * SCALE == units, f"{label} 百分数与单位不对应")
                state["days"] += 1
                product_sum[year] += int(row["sum_product_units"])
                for name in names:
                    participation[(name, year)] += 1
                seen.update(names)
            cumulative += units
            peak = max(peak, cumulative)
            streak = streak + 1 if peak > cumulative else 0
            longest = max(longest, streak)
            deepest = max(deepest, Decimal(peak - cumulative))
            check("己", int(row["cumulative_units"]) == cumulative and dec(row["cumulative_percent"]) * SCALE == cumulative,
                  f"{label} 累计重算为 {cumulative}")
            check("己", dec(row["drawdown_pp"]) * SCALE == peak - cumulative and int(row["drawdown_observations"]) == streak,
                  f"{label} 回撤或回撤天数重算不同")
            state["units"] += units
            state["cum"] += units
            state["peak"] = max(state["peak"], state["cum"])
            state["streak"] = state["streak"] + 1 if state["peak"] > state["cum"] else 0
            state["longest"] = max(state["longest"], state["streak"])
            state["deepest"] = max(state["deepest"], state["peak"] - state["cum"])
            state["low"], state["high"] = min(state["low"], count), max(state["high"], count)
        total = combined[method]
        check("庚", int(total["contributing_days"]) == sum(state["days"] for state in year_state.values())
              and int(total["total_observation_days"]) == len(series), f"{method} 合并天数重数不同")
        check("庚", int(total["normalized_units"]) == cumulative and dec(total["normalized_percent"]) * SCALE == cumulative,
              f"{method} 合并全期重算为 {cumulative}")
        check("庚", dec(total["max_drawdown_pp"]) * SCALE == deepest and int(total["longest_drawdown_observations"]) == longest,
              f"{method} 合并最大回撤或最长回撤天数重算不同")
        check("庚", (total["open_drawdown_at_end"] == "true") == (peak > cumulative), f"{method} 末尾是否未收复不同")
        check("庚", int(total["min_participants"]) == min(state["low"] for state in year_state.values())
              and int(total["max_participants"]) == max(state["high"] for state in year_state.values()),
              f"{method} 参与品种数范围不同")
        check("庚", set(total["eligible_products"].split("|")) == seen, f"{method} 参与过的品种与登记的不同")
        for year, state in year_state.items():
            line = combined_year[(method, year)]
            label = f"{method} {year}"
            check("庚", int(line["normalized_units"]) == state["units"] and int(line["contributing_days"]) == state["days"],
                  f"{label} 合并年度值重算为 {state['units']}")
            check("庚", dec(line["max_drawdown_pp"]) * SCALE == state["deepest"]
                  and int(line["longest_drawdown_observations"]) == state["longest"], f"{label} 年内回撤重算不同")
            check("庚", int(line["min_participants"]) == state["low"] and int(line["max_participants"]) == state["high"],
                  f"{label} 年内参与品种数范围不同")
            by_product = sum(int(annual[(product, method)][year]["normalized_units"] or 0)
                             for product in seen if year in annual[(product, method)])
            check("辛", by_product == product_sum[year],
                  f"{label} 各品种年度折算单位之和 {by_product} ≠ 合并表逐日之和 {product_sum[year]}")
            for product in seen:
                expected = int(annual[(product, method)].get(year, {"covered_days": 0})["covered_days"])
                check("辛", participation[(product, year)] == expected,
                      f"{label} {product} 参与 {participation[(product, year)]} 天 ≠ 主力日 {expected} 天")

    # 壬：报告正文
    text = REPORT.read_text(encoding="utf-8")
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for raw in text.splitlines():
        if raw.startswith("|"):
            current.append([cell.strip() for cell in raw.strip().strip("|").split("|")])
        elif current:
            tables.append(current)
            current = []
    if current:
        tables.append(current)
    body = [table[2:] for table in tables]
    check("壬", [len(table) for table in body] == [30, 5, 35, 18, 24], f"报告里五张表的行数是 {[len(t) for t in body]}")
    product_of = {name: code for code, name in PRODUCTS.items()}
    method_of = {name: code for code, name in METHODS.items()}
    for cells in body[0]:
        row = summary[(product_of[cells[0]], method_of[cells[1]])]
        label = f"报告总表 {cells[0]} {cells[1]}"
        rule = method_of[cells[1]] in RULES
        check("壬", near(cells[2], dec(row["net_yuan"]), 0) and near(cells[3], dec(row["normalized_percent"]), 2)
              and near(cells[6], dec(row["worst_year_net_yuan"]), 0) and near(cells[7], dec(row["max_drawdown_pp"]), 2),
              f"{label} 金额或百分数与 summary.csv 不同")
        check("壬", cells[4] == row["positive_years"] and cells[5] == row["worst_year"]
              and cells[9] == row["selected_opening_fills"]
              and (cells[10] == "计入") == (row["eligibility"] == "INCLUDED"), f"{label} 计数列与 summary.csv 不同")
        check("壬", near(cells[8], dec(row["readiness_percent"]), 2) if rule else cells[8] == "—", f"{label} 可计算日占比不同")
    for cells in body[1]:
        row = combined[method_of[cells[0]]]
        check("壬", near(cells[1], dec(row["normalized_percent"]), 2) and near(cells[2], dec(row["max_drawdown_pp"]), 2)
              and cells[3] == row["longest_drawdown_observations"]
              and (cells[4] == "是") == (row["open_drawdown_at_end"] == "true")
              and cells[5] == row["min_participants"] and cells[6] == row["max_participants"],
              f"报告合并表 {cells[0]} 与 combined-summary.csv 不同")
    for cells in body[2]:
        row = combined_year[(method_of[cells[0]], cells[1])]
        check("壬", cells[2] == YEAR_STATUS[row["year_status"]] and near(cells[3], dec(row["normalized_percent"]), 2)
              and near(cells[4], dec(row["max_drawdown_pp"]), 2) and cells[5] == row["longest_drawdown_observations"],
              f"报告合并年度表 {cells[0]} {cells[1]} 与 combined-annual.csv 不同")
    for cells in body[3]:
        row = readiness[(product_of[cells[0]], method_of[cells[1]])]
        check("壬", cells[2] == row["ready_days"] and cells[3] == row["readiness_denominator"]
              and near(cells[4], dec(row["readiness_percent"]), 2) and cells[5] == row["selected_opening_fills"]
              and cells[6] == row["all_contracts"] and cells[7] == row["all_contracts_never_opened"],
              f"报告可计算日表 {cells[0]} {cells[1]} 与 readiness.csv 不同")
    for cells in body[4]:
        product, capital = product_of[cells[0]], cells[1].replace(",", "")
        for shown, method in zip(cells[2:], RULES):
            events = old_events[(product, capital, "BASE", method, "ALL")]
            expected = "/".join(events[name] for name in ("opening_attempts", "opening_fills", "opening_margin_blocked"))
            check("壬", shown == expected, f"报告资金表 {cells[0]} {capital} {method}：{shown} ≠ {expected}")

    cells18 = [summary[(product, method)] for product in PRODUCTS for method in RULES
               if summary[(product, method)]["eligibility"] == "INCLUDED"]
    wins = sum(dec(row["net_yuan"]) > 0 for row in cells18)
    losses = sum(dec(row["net_yuan"]) < 0 for row in cells18)
    beats = sum(dec(row["net_yuan"]) > dec(summary[(row["product"], "buy_hold_control")]["net_yuan"]) for row in cells18)
    facts = {row["fact_id"]: row["value"] for row in rows(V2 / "facts.csv")}
    check("壬", [facts["included_rule_cells"], facts["positive_rule_cells"], facts["negative_rule_cells"],
                 facts["zero_rule_cells"], facts["better_than_hold_cells"]]
          == [str(len(cells18)), str(wins), str(losses), str(len(cells18) - wins - losses), str(beats)],
          f"facts.csv 的赚/亏/好过持有个数重数为 {wins}/{losses}/{beats}")
    screen = text.split("\n|", 1)[0]
    found = re.search(r"(\d+) 个“品种×规则”中 (\d+) 个赚、(\d+) 个亏、(\d+) 个持平，好过一直持有同品种主力合约的有 (\d+) 个", screen)
    check("壬", found is not None and [int(value) for value in found.groups()]
          == [len(cells18), wins, losses, len(cells18) - wins - losses, beats], "第一屏的赚/亏/好过持有个数与表不符")
    for method, name in METHODS.items():
        if method == "flat_control":
            continue
        shown = re.search(rf"{name} (-?[\d.]+)%", screen)
        check("壬", shown is not None and near(shown.group(1), dec(combined[method]["normalized_percent"]), 2)
              and dec(facts[f"combined_percent/{method}"]) == dec(combined[method]["normalized_percent"]),
              f"第一屏或 facts.csv 的 {name} 合并百分数与 combined-summary.csv 不符")

    print("六品种报告第二版：已入库表格的一致性核对")
    for group in "甲乙丙丁戊己庚辛壬":
        bad = sum(1 for problem in problems if problem.startswith(f"[{group}]"))
        print(f"  {group}  {checks[group]:>7,} 项，不一致 {bad} 项")
    print(f"合计 {sum(checks.values()):,} 项，不一致 {len(problems)} 项")
    for problem in problems[:40]:
        print("  ✗", problem)
    print(f"\n按主力合约：{len(cells18)} 个“品种×规则”里 {wins} 个赚、{losses} 个亏，好过一直持有的 {beats} 个")
    print("每个品种三个规则的选中日开仓次数（6 年合计）和可计算日占比：")
    for product, name in PRODUCTS.items():
        parts = [f"{METHODS[method]} {readiness[(product, method)]['selected_opening_fills']} 次/"
                 f"{dec(readiness[(product, method)]['readiness_percent']):.1f}%" for method in RULES]
        print(f"  {name:<8} " + "，".join(parts))

    print("\n合并口径的逐日序列（我自己的描述性计算；百分数是名义金额的百分之几，不复利）：")
    print(f"{'做法':<8} {'全期合计':>9} {'日均':>9} {'日波动':>8} {'年化的均值/波动':>14} "
          f"{'全期合计的 95% 区间':>22} {'合计不大于 0 的比例':>18}")
    generator = random.Random(20261114)
    block, repeats = 20, 20_000
    for method, name in METHODS.items():
        values = [float(row["normalized_percent"]) for row in daily[method] if row["status"] == "COVERED"]
        count = len(values)
        mean = sum(values) / count
        spread = math.sqrt(sum((value - mean) ** 2 for value in values) / (count - 1))
        if spread == 0:
            print(f"{name:<8} {sum(values):>12.2f}% {mean:>10.4f}% {spread:>9.3f}% {'—':>16}")
            continue
        prefix = [0.0]
        for value in values + values[:block]:
            prefix.append(prefix[-1] + value)
        blocks = math.ceil(count / block)
        totals = []
        for _ in range(repeats):
            total = 0.0
            for _ in range(blocks):
                start = generator.randrange(count)
                total += prefix[start + block] - prefix[start]
            totals.append(total * count / (blocks * block))
        totals.sort()
        low, high = totals[int(0.025 * repeats)], totals[int(0.975 * repeats) - 1]
        below = sum(1 for total in totals if total <= 0) / repeats
        print(f"{name:<8} {sum(values):>12.2f}% {mean:>10.4f}% {spread:>9.3f}% {mean / spread * math.sqrt(242):>16.2f} "
              f"{low:>14.1f}% 到 {high:>6.1f}% {below:>18.1%}")
    left = {row["raw_date"]: float(row["normalized_percent"]) for row in daily["donchian_trend"] if row["status"] == "COVERED"}
    right = {row["raw_date"]: float(row["normalized_percent"]) for row in daily["buy_hold_control"] if row["status"] == "COVERED"}
    dates = sorted(set(left) & set(right))
    mean_left = sum(left[day] for day in dates) / len(dates)
    mean_right = sum(right[day] for day in dates) / len(dates)
    covariance = sum((left[day] - mean_left) * (right[day] - mean_right) for day in dates)
    scale = math.sqrt(sum((left[day] - mean_left) ** 2 for day in dates) * sum((right[day] - mean_right) ** 2 for day in dates))
    print(f"突破与一直持有的逐日相关系数：{covariance / scale:.2f}（{len(dates):,} 个共同交易日）")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
