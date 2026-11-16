"""第九轮外部审核探针：K45-R001 已入库文件之间的一致性（只读）。

回答两个问题：

1. R001 报告的正文、九份 CSV 和 CHECK.json 相互对不对得上；登记的哈希是否等于
   仓库里的实际文件；每个期限的结论按设计第 5 节的固定顺序重算后是否相同。
2. 已公布的可靠性表里，改进落在“涨、跌、中性”三类中的哪一类（描述，不计入核对）。

只读仓库里已入库的文件，只用标准库。逐行的概率和标签保存在仓库外，这里核对
不了，由项目内的独立审核者从封存文件复算。

用法（在仓库根目录）：
    python -B -X utf8 docs/repo-audit-20261116-r9-claude/probes/k45_r001_published_tables_recheck.py

退出码：0 没有不一致；1 有不一致；2 找不到需要的文件。
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import sys
from pathlib import Path


def repository_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "src" / "ak_trading").is_dir():
            return parent
    raise SystemExit(2)


ROOT = repository_root()
REPORT = ROOT / "docs" / "research" / "k45-forecast-skill-r001.md"
TABLES = ROOT / "docs" / "research" / "k45-forecast-skill-r001"
TOOL = ROOT / "tools" / "experiments" / "k45_forecast_skill.py"
MINUTE_HORIZONS = ("1", "5", "15", "30", "60")
DAILY_HORIZONS = ("1", "5", "20")
CLASSES = ("UP", "DOWN", "NEUTRAL")
CHECKS = {"total": 0}
PROBLEMS: list[str] = []


def check(group: str, condition: bool, message: str) -> None:
    CHECKS["total"] += 1
    CHECKS[group] = CHECKS.get(group, 0) + 1
    if not condition:
        PROBLEMS.append(f"[{group}] {message}")


def close(a: float, b: float, tolerance: float = 1e-12) -> bool:
    return abs(a - b) <= tolerance * max(1.0, abs(a), abs(b))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_csv(name: str) -> list[dict[str, str]]:
    with (TABLES / name).open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def number(text: str) -> float:
    return float(text.replace(",", ""))


# ---------------------------------------------------------------- 甲：CHECK.json
def group_check_json(check_json: dict, main: list[dict[str, str]]) -> None:
    for member in check_json["code_pins"]:
        path = ROOT / member["path"]
        ok = path.is_file()
        check("甲", ok, f"登记的代码文件不存在：{member['path']}")
        if ok:
            check("甲", path.stat().st_size == member["bytes"], f"字节数不同：{member['path']}")
            check("甲", sha256(path) == member["sha256"], f"哈希不同：{member['path']}")
    for member in check_json["summary_files"]:
        path = ROOT / member["path"]
        ok = path.is_file()
        check("甲", ok, f"登记的汇总文件不存在：{member['path']}")
        if ok:
            check("甲", path.stat().st_size == member["bytes"], f"字节数不同：{member['path']}")
            check("甲", sha256(path) == member["sha256"], f"哈希不同：{member['path']}")
    listed = {Path(member["path"]).name for member in check_json["summary_files"]}
    present = {p.name for p in TABLES.iterdir() if p.suffix == ".csv"} | {REPORT.name}
    check("甲", listed == present, f"汇总文件清单与目录不一致：{sorted(listed ^ present)}")

    tool_text = TOOL.read_text(encoding="utf-8")
    plan = re.search(r'PLAN_SHA = "([0-9a-f]{64})"', tool_text)
    models = re.search(r'PRODUCTION_MODELS_SHA = "([0-9a-f]{64})"', tool_text)
    pins = {member["path"]: member["sha256"] for member in check_json["code_pins"]}
    check("甲", bool(plan) and plan.group(1) == pins.get("docs/research/k45-forecast-skill-design.md"),
          "工具里写死的设计文档哈希与 CHECK 不同")
    check("甲", bool(models) and models.group(1) == pins.get("src/ak_trading/v2/probability/models.py"),
          "工具里写死的 models.py 哈希与 CHECK 不同")

    calls = check_json["actual_test_calls"]
    minute = [row for row in main if row["mode"] == "MINUTE"]
    rows = sum(int(row["common_rows"]) for row in minute)
    check("甲", calls["test_label_calls"] == rows, "检验段标签调用次数不等于五个期限的共同行数之和")
    check("甲", calls["test_probability_calls"] == 3 * rows, "检验段概率调用次数不等于 3 × 共同行数之和")
    check("甲", calls["test_scoring_run_count"] == 1 and check_json["research_run_count"] == 1, "评分次数不是 1")
    check("甲", check_json["mode_calls"]["MINUTE"] == {"label_calls": rows, "probability_calls": 3 * rows,
                                                      "scoring_runs": 1}, "分钟模式调用计数不一致")
    check("甲", check_json["mode_calls"]["DAILY"] == {"label_calls": 0, "probability_calls": 0, "scoring_runs": 0},
          "日线模式调用计数不是 0")
    check("甲", check_json["training_calls"] == {"DAILY": {"formal": 0, "rehearsal": 0},
                                                "MINUTE": {"formal": 1, "rehearsal": 1}}, "拟合次数不一致")
    safety = check_json["safety"]
    check("甲", safety == {"M5_entry_used": False, "broker": "NOT_RUN", "enable_trading": False,
                          "holdout_read": False, "observer": "hold", "real_cancel": 0, "real_send": 0},
          "安全边界字段不是预期值")
    check("甲", check_json["fixed_sample_dates"] == {"TRAIN": ["2023-01-03", "2024-02-29"],
                                                    "CALIBRATION": ["2024-03-01", "2024-05-31"],
                                                    "TEST": ["2024-06-03", "2024-10-18"]}, "固定日期不一致")
    check("甲", check_json["scenario_seconds"] == 5 and check_json["step"] == 5
          and check_json["source_input_revision"] == "r1", "情景秒数、取样步长或输入版本不一致")
    check("甲", all(row["scores_sha256"] == check_json["score_sha256"] for row in main),
          "主表各行的评分文件哈希与 CHECK 不同")
    review = check_json["product_hard_review"]
    check("甲", review["verdict"] == "PASS" and review["conditional"] is False and review["independent"] is True,
          "硬审结论不是独立、无条件 PASS")
    check("甲", check_json["logical_elapsed_through_r001_seconds"] < check_json["wall_limit_seconds"],
          "逻辑耗时超过 72 小时上限")
    memory = check_json["memory"]
    check("甲", memory["observed_003_peak_private_bytes"] < memory["fixed_process_tree_cap_bytes"]
          and memory["observed_r001_peak_private_bytes"] < memory["fixed_process_tree_cap_bytes"],
          "实测内存峰值超过固定上限")


# ---------------------------------------------------------------- 乙：主表内部
def classify(row: dict[str, str]) -> tuple[str, str]:
    """设计第 5 节和 10.7 的固定顺序，独立重写。"""
    if row["common_days"] == "NA":
        return "INSUFFICIENT", "本固定设计没有可评估结果"
    lower95, upper95 = float(row["raw_brier_ci95_lower"]), float(row["raw_brier_ci95_upper"])
    family_lower = float(row["raw_brier_ci_family_lower"])
    logloss_lower = float(row["raw_logloss_ci95_lower"])
    calibrated_lower = float(row["calibrated_brier_ci95_lower"])
    if int(row["common_days"]) < 60 or row["inner_sufficient"] != "True" or row["algorithm"] == "NA":
        return "INSUFFICIENT", "本固定设计没有可评估结果"
    if family_lower > 0 and logloss_lower > 0 and calibrated_lower > 0 and row["algorithm"] != "UNCONDITIONAL_FREQUENCY":
        return "CONTINUE", "在本固定输入与这组特征/模型上出现值得继续检验的信息量"
    if lower95 > 0 and family_lower <= 0:
        return "SUGGESTIVE", "提示性改善，未达到预设继续条件"
    if row["algorithm"] == "UNCONDITIONAL_FREQUENCY":
        return "BASE_RATE", "基率/温度校准结果，未显示新增特征信息"
    if family_lower > 0:
        return "MIXED", "指标证据不一致，或优势未能排除基率校准解释，未达到预设继续条件"
    if (upper95 - lower95) / 2 <= 0.02:
        return "NO_INFORMATION", "未显示信息量"
    return "UNRESOLVED", "这次检验分辨不了"


def group_main(main: list[dict[str, str]]) -> None:
    check("乙", [(row["mode"], row["horizon"]) for row in main]
          == [("DAILY", h) for h in DAILY_HORIZONS] + [("MINUTE", h) for h in MINUTE_HORIZONS],
          "主表不是固定的 8 行或顺序不同")
    for row in main:
        name = f"{row['mode']}/{row['horizon']}"
        status, text = classify(row)
        check("乙", row["status"] == status and row["conclusion"] == text, f"{name} 的结论重算后不同：{status}")
        if row["mode"] == "DAILY":
            numeric = [key for key, value in row.items()
                       if key.endswith(("_brier", "_logloss", "_skill", "_lower", "_upper")) and value != "NA"]
            check("乙", not numeric, f"{name} 应全部为 NA，却有数值列：{numeric[:3]}")
            check("乙", row["execution"] == "NOT_RUN" and row["reasons"] == "daily_start_not_production_slot",
                  f"{name} 的未运行原因不一致")
            continue
        check("乙", row["execution"] == "SCORED_ONCE" and row["assessment"] == "SCENARIO / NOT_ASSESSED",
              f"{name} 的执行或评估标记不一致")
        check("乙", row["excluded_rows"] == "0" and row["exclusion_reasons_nonexclusive"] == "{}",
              f"{name} 有被排除的行")
        for population in ("model", "raw", "calibrated"):
            check("乙", row[f"{population}_independent_rows"] == row["common_rows"],
                  f"{name} 的 {population} 独立可评分行数不等于共同行数")
        for baseline in ("raw", "calibrated"):
            for metric in ("brier", "logloss"):
                skill = 1 - float(row[f"model_{metric}"]) / float(row[f"{baseline}_{metric}"])
                key = f"{baseline}_{metric}"
                check("乙", close(skill, float(row[f"{key}_skill"])), f"{name} 的 {key} 技能重算后不同")
                lower, upper = float(row[f"{key}_ci95_lower"]), float(row[f"{key}_ci95_upper"])
                check("乙", lower <= upper, f"{name} 的 {key} 区间上下界颠倒")
                check("乙", lower <= float(row[f"{key}_skill"]) <= upper, f"{name} 的 {key} 点估计不在 95% 区间内")
                check("乙", row[f"{key}_valid_repeats"] == "20000" and row[f"{key}_invalid_repeats"] == "{}",
                      f"{name} 的 {key} 有效重复数不是 20000")
        lower, upper = float(row["raw_brier_ci95_lower"]), float(row["raw_brier_ci95_upper"])
        check("乙", close((upper - lower) / 2, float(row["half_width95"])), f"{name} 的半宽重算后不同")
        check("乙", (float(row["half_width95"]) <= 0.02) == (row["resolved_at_002"] == "True"),
              f"{name} 的“分辨得了”标记与半宽不符")
        check("乙", float(row["raw_brier_ci_family_lower"]) <= lower and upper <= float(row["raw_brier_ci_family_upper"]),
              f"{name} 的家族区间没有包住普通区间")
    minute = [row for row in main if row["mode"] == "MINUTE"]
    for column in ("common_rows", "common_days", "probabilities_sha256", "labels_sha256"):
        check("乙", len({row[column] for row in minute}) == 1, f"分钟各期限的 {column} 不相同")


# ---------------------------------------------------------------- 丙：报告正文
def significant(value: str) -> str:
    return format(float(value), ".8g")


def group_report(main: list[dict[str, str]], check_json: dict, costs: list[dict[str, str]]) -> None:
    text = REPORT.read_text(encoding="utf-8")
    lines = text.splitlines()
    first_table = next((index for index, line in enumerate(lines) if line.startswith("|")), len(lines))
    first_screen = [line for line in lines[:first_table] if line and not line.startswith("#")]
    check("丙", len(first_screen) == 5, f"主表之前不是五句：{len(first_screen)}")
    check("丙", "假设在收盘后 5 秒可用" in text, "第一屏没有写明 5 秒可用的假设")
    table = [line for line in lines if line.startswith(("| DAILY/", "| MINUTE/"))]
    check("丙", len(table) == 8, f"正文主表不是 8 行：{len(table)}")
    longest = max((len([l for l in block.splitlines() if l.startswith("|")]) for block in text.split("\n\n")), default=0)
    check("丙", longest <= 50, f"正文里有超过 50 行的表：{longest}")
    by_key = {f"{row['mode']}/{row['horizon']}": row for row in main}
    for line in table:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        row = by_key.get(cells[0])
        check("丙", row is not None, f"正文主表的行名不在 CSV 里：{cells[0]}")
        if row is None:
            continue
        if row["mode"] == "DAILY":
            check("丙", cells[1] == "NA/NA" and cells[4] == "NA" and row["reasons"] in cells[10]
                  and row["conclusion"] in cells[10], f"{cells[0]} 的正文与 CSV 不一致")
            continue
        check("丙", cells[1] == f"{row['common_days']}/{int(row['common_rows']):,}", f"{cells[0]} 的共同日/行不一致")
        check("丙", cells[2] == " / ".join(significant(row[f"{p}_brier"]) for p in ("model", "raw", "calibrated")),
              f"{cells[0]} 的 Brier 三个数不一致")
        check("丙", cells[3] == " / ".join(significant(row[f"{p}_logloss"]) for p in ("model", "raw", "calibrated")),
              f"{cells[0]} 的 Logloss 三个数不一致")
        check("丙", cells[4] == significant(row["raw_brier_skill"]), f"{cells[0]} 的 Brier 技能不一致")
        check("丙", cells[5] == f"[{significant(row['raw_brier_ci95_lower'])}, {significant(row['raw_brier_ci95_upper'])}]",
              f"{cells[0]} 的普通 95% 区间不一致")
        check("丙", cells[6] == significant(row["half_width95"]), f"{cells[0]} 的半宽不一致")
        check("丙", cells[7] == f"[{significant(row['raw_brier_ci_family_lower'])}, "
              f"{significant(row['raw_brier_ci_family_upper'])}]", f"{cells[0]} 的家族区间不一致")
        check("丙", cells[8] == f"{significant(row['raw_logloss_skill'])} [{significant(row['raw_logloss_ci95_lower'])}, "
              f"{significant(row['raw_logloss_ci95_upper'])}]", f"{cells[0]} 的 Logloss 技能及区间不一致")
        check("丙", cells[9] == f"{significant(row['calibrated_brier_skill'])} "
              f"[{significant(row['calibrated_brier_ci95_lower'])}, {significant(row['calibrated_brier_ci95_upper'])}]",
              f"{cells[0]} 的调温对照技能及区间不一致")
        check("丙", cells[10] == row["conclusion"], f"{cells[0]} 的结论文字不一致")

    def cost_row(label: str) -> str | None:
        match = re.search(r"\| " + re.escape(label) + r" \| ([0-9.,]+) (秒|字节) \|", text)
        return match.group(1) if match else None

    walls = {row["stage"]: row for row in costs if row["component"] == "whole_process_tree_wall"}
    fits = {row["dimensions"]: row for row in costs
            if row["stage"] == "FORMAL_AND_REHEARSAL_003" and row["component"] == "resident_fit"}
    expected = {
        "003 正式与预演进程树墙钟": float(walls["FORMAL_AND_REHEARSAL_003"]["total_seconds"]),
        "正式 T/C 拟合": float(fits['["MINUTE","R001"]']["total_seconds"]),
        "预演拟合": float(fits['["MINUTE","DRY_RUN"]']["total_seconds"]),
        "R001 进程树墙钟": float(walls["R001_ONCE"]["total_seconds"]),
        "原逻辑作业起点至 R001 结束": check_json["logical_elapsed_through_r001_seconds"],
        "原预算预计峰值": check_json["memory"]["estimated_peak_bytes"],
        "003 实测峰值": check_json["memory"]["observed_003_peak_private_bytes"],
        "R001 实测峰值": check_json["memory"]["observed_r001_peak_private_bytes"],
        "固定内存上限": check_json["memory"]["fixed_process_tree_cap_bytes"],
    }
    for label, value in expected.items():
        shown = cost_row(label)
        check("丙", shown is not None and abs(number(shown) - value) <= 5e-7 * max(1.0, abs(value)),
              f"正文计量表“{label}”与 CSV/CHECK 不一致：{shown} 对 {value}")
    check("丙", int(walls["FORMAL_AND_REHEARSAL_003"]["peak_tree_private_bytes"])
          == check_json["memory"]["observed_003_peak_private_bytes"]
          and int(walls["R001_ONCE"]["peak_tree_private_bytes"]) == check_json["memory"]["observed_r001_peak_private_bytes"],
          "实际耗时表里的峰值与 CHECK 不同")


# ---------------------------------------------------------------- 丁、戊：算法表
def group_algorithms(main, algorithms, shares, funnel) -> None:
    by_cell: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in algorithms:
        by_cell.setdefault((row["mode"], row["horizon"]), []).append(row)
    check("丁", len(algorithms) == 32 and all(len(group) == 4 for group in by_cell.values()),
          "算法表不是 8 个期限 × 4 个候选")
    samples = {(row["partition"], row["horizon"]): row for row in funnel
               if row["phase"] == "PRETEST_STAGE1" and row["mode"] == "MINUTE" and row["layer"] == "direction_samples"}
    for row in main:
        group = by_cell[(row["mode"], row["horizon"])]
        name = f"{row['mode']}/{row['horizon']}"
        chosen = [item for item in group if item["selected"] == "True"]
        if row["mode"] == "DAILY":
            check("丁", not chosen and all(item["fit_status"] == "NOT_RUN" for item in group), f"{name} 不应有拟合记录")
            continue
        check("丁", len(chosen) == 1 and chosen[0]["algorithm"] == row["algorithm"], f"{name} 选中的算法与主表不同")
        losses = {item["algorithm"]: float(item["development_loss"]) for item in group}
        check("丁", len(chosen) == 1 and losses[chosen[0]["algorithm"]] == min(losses.values()),
              f"{name} 选中的不是内层验证损失最小的候选")
        check("丁", len(chosen) == 1 and chosen[0]["fit_status"] == f"SELECTED:{chosen[0]['algorithm']}",
              f"{name} 选中候选的状态字段不一致")
        for item in group:
            check("丁", item["train_days"] == "278" and item["calibration_days"] == "60", f"{name} 的训练/校准日数不一致")
            check("丁", item["train_direction_samples"] == samples[("TRAIN", row["horizon"])]["count"]
                  and item["calibration_direction_samples"] == samples[("CALIBRATION", row["horizon"])]["count"],
                  f"{name} 的方向样本数与漏斗表不同")
            if item["selected"] != "True":
                check("丁", item["temperature"] == "1.0", f"{name} 落选候选的温度不是 1")
    minute = [row for row in main if row["mode"] == "MINUTE"]
    total = sum(int(row["common_rows"]) for row in minute)
    for row in shares:
        if row["mode"] == "DAILY":
            check("戊", row["selected_cells"] == "0" and row["prediction_total"] == "0" and row["cell_share"] == "NA",
                  "日线的算法占比不是空")
            continue
        cells = [item for item in minute if item["algorithm"] == row["algorithm"]]
        predictions = sum(int(item["common_rows"]) for item in cells)
        check("戊", int(row["selected_cells"]) == len(cells) and int(row["valid_model_cells"]) == len(minute),
              f"{row['algorithm']} 的选中期限数不一致")
        check("戊", int(row["predictions_with_algorithm"]) == predictions and int(row["prediction_total"]) == total,
              f"{row['algorithm']} 的预测数不一致")
        check("戊", close(float(row["cell_share"]), len(cells) / len(minute))
              and close(float(row["prediction_share"]), predictions / total), f"{row['algorithm']} 的占比不一致")


# ---------------------------------------------------------------- 己、庚、辛：附表
def group_diagnostics(main, reliability, neutral, spread) -> dict[str, dict[str, list[dict[str, float]]]]:
    rows_by_horizon = {row["horizon"]: int(row["common_rows"]) for row in main if row["mode"] == "MINUTE"}
    days_by_horizon = {row["horizon"]: int(row["common_days"]) for row in main if row["mode"] == "MINUTE"}
    check("己", len(reliability) == 8 * 3 * 10, "可靠性表不是 8 个期限 × 3 类 × 10 箱")
    bins: dict[str, dict[str, list[dict[str, float]]]] = {}
    for row in reliability:
        if row["mode"] == "DAILY":
            check("己", all(row[key] == "NA" for key in ("rows", "days", "predicted", "actual", "gap")), "日线可靠性表应为 NA")
            continue
        index = int(row["bin"])
        count = int(row["rows"])
        if count == 0:
            check("己", row["predicted"] == "NA" and row["actual"] == "NA", "空箱应为 NA")
            continue
        predicted, actual = float(row["predicted"]), float(row["actual"])
        check("己", index / 10 <= predicted <= (index + 1) / 10 + 1e-12, f"{row['horizon']} {row['class']} 第 {index} 箱的预测均值越界")
        check("己", 0 <= actual <= 1 and close(predicted - actual, float(row["gap"])), f"{row['horizon']} {row['class']} 第 {index} 箱的差值不一致")
        check("己", int(row["days"]) <= days_by_horizon[row["horizon"]], f"{row['horizon']} {row['class']} 第 {index} 箱的日数超过共同日数")
        bins.setdefault(row["horizon"], {}).setdefault(row["class"], []).append(
            {"rows": count, "predicted": predicted, "actual": actual})
    for horizon, by_class in bins.items():
        for label in CLASSES:
            check("己", sum(item["rows"] for item in by_class[label]) == rows_by_horizon[horizon],
                  f"{horizon} 分钟 {label} 各箱行数之和不等于共同行数")
        # 行数加权只是近似（表里的均值是按日等权算的），所以只要求三类之和接近 1。
        for field in ("predicted", "actual"):
            total = sum(item["rows"] * item[field] for label in CLASSES for item in by_class[label]) / rows_by_horizon[horizon]
            check("己", abs(total - 1) <= 0.01, f"{horizon} 分钟三类 {field} 的行数加权和偏离 1：{total:.4f}")

    for row in neutral:
        if row["mode"] == "DAILY":
            check("庚", row["strict_maximum"] == "NA", "日线中性占比应为 NA")
            continue
        strict, tied, either = (float(row[key]) for key in ("strict_maximum", "tied_maximum", "strict_or_tied"))
        check("庚", 0 <= strict <= 1 and 0 <= tied <= 1 and close(strict + tied, either), f"{row['horizon']} 分钟中性占比不自洽")
        by_class = bins[row["horizon"]]
        neutral_low = min(item["predicted"] for item in by_class["NEUTRAL"])
        other_high = max(item["predicted"] for label in ("UP", "DOWN") for item in by_class[label])
        neutral_high = max(item["predicted"] for item in by_class["NEUTRAL"])
        other_low = min(item["predicted"] for label in ("UP", "DOWN") for item in by_class[label])
        if strict == 1.0:
            check("庚", neutral_high > other_low, f"{row['horizon']} 分钟：中性恒为最大，与可靠性表矛盾")
        if either == 0.0:
            check("庚", neutral_low < other_high, f"{row['horizon']} 分钟：中性从不最大，与可靠性表矛盾")

    by_horizon: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in spread:
        by_horizon.setdefault((row["mode"], row["horizon"]), []).append(row)
    check("辛", len(spread) == 8 * 9 and all(len(group) == 9 for group in by_horizon.values()), "概率差分位数表不是 8 × 9 行")
    for (mode, horizon), group in by_horizon.items():
        if mode == "DAILY":
            check("辛", all(row["day_equal"] == "NA" for row in group), "日线概率差应为 NA")
            continue
        for column in ("day_equal", "row_equal"):
            values = [float(row[column]) for row in sorted(group, key=lambda r: float(r["quantile"]))]
            check("辛", values == sorted(values) and -1 <= values[0] and values[-1] <= 1,
                  f"{horizon} 分钟概率差的 {column} 分位数不单调或越界")
        ends = [row for row in group if row["quantile"] in ("0.0", "1.0")]
        check("辛", all(row["day_equal"] == row["row_equal"] for row in ends), f"{horizon} 分钟概率差的最小/最大值两种口径不同")
    return bins


# ---------------------------------------------------------------- 壬：漏斗表
def group_funnel(main, funnel) -> None:
    text = REPORT.read_text(encoding="utf-8")
    stage = {(row["partition"], row["horizon"], row["layer"]): row for row in funnel
             if row["phase"] == "PRETEST_STAGE1" and row["mode"] == "MINUTE"}
    layers = ("candidate_anchors", "usable_features", "generated_requests", "legal_requests", "label_mature", "direction_samples")

    def count(partition, horizon, layer):
        value = stage[(partition, horizon, layer)]["count"]
        return None if value == "NA" else int(value)

    for partition in ("TRAIN", "CALIBRATION", "TEST"):
        for horizon in ("ALL", *MINUTE_HORIZONS):
            check("壬", all((partition, horizon, layer) in stage for layer in layers), f"漏斗缺行：{partition} {horizon}")
            factor = 15 if horizon == "ALL" else 3
            candidates, usable = count(partition, horizon, "candidate_anchors"), count(partition, horizon, "usable_features")
            generated, legal = count(partition, horizon, "generated_requests"), count(partition, horizon, "legal_requests")
            check("壬", usable == candidates - int(stage[(partition, horizon, "usable_features")]["loss_from_previous"]),
                  f"{partition} {horizon}：可用快照 ≠ 候选 − 损失")
            check("壬", generated == usable * factor, f"{partition} {horizon}：生成请求 ≠ 可用快照 × {factor}")
            check("壬", legal == generated - int(stage[(partition, horizon, "legal_requests")]["loss_from_previous"]),
                  f"{partition} {horizon}：合法请求 ≠ 生成 − 损失")
            mature, units = count(partition, horizon, "label_mature"), count(partition, horizon, "direction_samples")
            if partition == "TEST":
                check("壬", mature is None and units is None
                      and stage[(partition, horizon, "label_mature")]["execution"] == "NOT_RUN:TEST_SEALED",
                      f"TEST {horizon}：评分前的标签层应为未运行")
            else:
                check("壬", mature == legal - int(stage[(partition, horizon, "label_mature")]["loss_from_previous"])
                      and mature % 3 == 0 and units == mature // 3, f"{partition} {horizon}：成熟标签或方向样本数不自洽")
        for layer in ("generated_requests", "legal_requests") + (() if partition == "TEST" else ("label_mature", "direction_samples")):
            check("壬", count(partition, "ALL", layer) == sum(count(partition, h, layer) for h in MINUTE_HORIZONS),
                  f"{partition} 的 {layer}：合计 ≠ 各期限之和")
    final = {row["horizon"]: row for row in funnel if row["phase"] == "R001_FINAL" and row["mode"] == "MINUTE"}
    for row in main:
        if row["mode"] != "MINUTE":
            continue
        check("壬", final[row["horizon"]]["count"] == row["common_rows"] and final[row["horizon"]]["days"] == row["common_days"],
              f"{row['horizon']} 分钟的最终评分人口与主表不同")
        check("壬", count("TEST", row["horizon"], "legal_requests") == 3 * int(row["common_rows"]),
              f"{row['horizon']} 分钟：检验段合法请求 ≠ 3 × 共同行数")
    quoted = {"TRAIN 的候选/可用快照为 19,014/17,909": (count("TRAIN", "ALL", "candidate_anchors"), count("TRAIN", "ALL", "usable_features")),
              "合法成熟请求 268,569": (count("TRAIN", "ALL", "label_mature"),),
              "CAL 为 4,161/3,919": (count("CALIBRATION", "ALL", "candidate_anchors"), count("CALIBRATION", "ALL", "usable_features")),
              "合法成熟请求 57,744、60 天": (count("CALIBRATION", "ALL", "label_mature"), int(stage[("CALIBRATION", "ALL", "label_mature")]["days"])),
              "TEST 候选 6,276、可用快照 5,903、合法请求 87,570、91 天": (
                  count("TEST", "ALL", "candidate_anchors"), count("TEST", "ALL", "usable_features"),
                  count("TEST", "ALL", "legal_requests"), int(stage[("TEST", "ALL", "legal_requests")]["days"]))}
    for phrase, values in quoted.items():
        check("壬", phrase in text and [int(v.replace(",", "")) for v in re.findall(r"\d[\d,]*", phrase)] == list(values),
              f"正文“{phrase}”与漏斗表不一致")
    daily = [row for row in funnel if row["mode"] == "DAILY"]
    check("壬", daily and all(row["execution"].startswith("NOT_RUN") for row in daily), "日线漏斗不应有实际运行的行")


# ---------------------------------------------------------------- 癸：预算与实际耗时
def group_costs(budget, costs, check_json) -> None:
    minute = [row for row in budget if row["mode"] == "MINUTE"]
    for row in minute:
        check("癸", close(float(row["quantity"]) * float(row["unit_seconds"]), float(row["estimated_seconds"]), 1e-9),
              f"预算行 {row['component']}：数量 × 单位耗时 ≠ 小计")
    total = math.fsum(float(row["estimated_seconds"]) for row in minute)
    elapsed, remaining, planning = (float(minute[0][key]) for key in
                                    ("elapsed_at_budget_seconds", "remaining_at_budget_seconds", "planning_total_seconds"))
    check("癸", close(total, remaining, 1e-9), f"预算各行小计之和 {total:.3f} ≠ 剩余时间 {remaining:.3f}")
    check("癸", close(elapsed + remaining, planning, 1e-12), "已发生 + 剩余 ≠ 规划总时间")
    check("癸", planning < check_json["wall_limit_seconds"], "规划总时间超过 72 小时")
    check("癸", int(minute[0]["estimated_peak_bytes"]) == check_json["memory"]["estimated_peak_bytes"]
          and int(minute[0]["fixed_process_tree_cap_bytes"]) == check_json["memory"]["fixed_process_tree_cap_bytes"],
          "预算表里的内存估计或上限与 CHECK 不同")
    for row in costs:
        if row["calls"] in ("", "NA") or row["total_seconds"] in ("", "NA") or row["unit_seconds"] in ("", "NA"):
            continue
        calls = float(row["calls"])
        if calls > 0:
            check("癸", close(float(row["total_seconds"]) / calls, float(row["unit_seconds"]), 1e-9),
                  f"实际耗时行 {row['stage']} {row['component']} {row['dimensions']}：总耗时 ÷ 次数 ≠ 单位耗时")
    exits = {row["native_exit_code"] for row in costs if row["component"] == "whole_process_tree_wall"}
    check("癸", exits <= {"0"}, f"正式运行的原始退出码不是 0：{exits}")


# ---------------------------------------------------------------- 描述：改进落在哪一类
def describe_classes(bins, main) -> None:
    print("\n改进落在哪一类（描述，不计入核对）")
    print("  按可靠性表的分箱粗算；表里各箱的均值是按日等权算的，这里用行数加权，所以是近似。")
    print("  净改进 = 分箱后的分辨度 − 校准误差；三类相加，对应“模型比检验段自身占比好多少”。")
    print("  中性占比超过 100% 表示涨、跌两类的净改进为负。只会预测“动不动”的预测者，这个占比在合成数据上是 73%–75%；")
    print("  另外还知道方向的是 58%–60%（见 k45_direction_decomposition_reference.py 的第四部分）。")
    print(f"  {'期限':>4} {'检验段占比 涨/跌/中性':>24} {'净改进 涨':>10} {'跌':>9} {'中性':>9} {'中性占比':>8} {'概率最高两箱 预测→实际（涨；跌）'}")
    brier = {row["horizon"]: row for row in main if row["mode"] == "MINUTE"}
    for horizon in MINUTE_HORIZONS:
        by_class = bins[horizon]
        total = sum(item["rows"] for item in by_class["UP"])
        net, base = {}, {}
        for label in CLASSES:
            rate = sum(item["rows"] * item["actual"] for item in by_class[label]) / total
            resolution = sum(item["rows"] * (item["actual"] - rate) ** 2 for item in by_class[label]) / total
            reliability = sum(item["rows"] * (item["predicted"] - item["actual"]) ** 2 for item in by_class[label]) / total
            net[label], base[label] = resolution - reliability, rate
        everything = sum(net.values())
        share = f"{net['NEUTRAL'] / everything:8.0%}" if everything > 0 else "  合计为负"
        tops = []
        for label in ("UP", "DOWN"):
            top = sorted(by_class[label], key=lambda item: item["predicted"])[-2:]
            tops.append(" ".join(f"{item['predicted']:.2f}→{item['actual']:.2f}({item['rows']}行)" for item in top))
        print(f"  {horizon:>4} {base['UP']:>10.1%}/{base['DOWN']:.1%}/{base['NEUTRAL']:.1%} "
              f"{net['UP']:>+10.4f} {net['DOWN']:>+9.4f} {net['NEUTRAL']:>+9.4f} {share} {'；'.join(tops)}")
        climatology = 1 - sum(rate ** 2 for rate in base.values())
        print(f"       三类 Brier：检验段自身占比 {climatology:.4f}，原始对照 {float(brier[horizon]['raw_brier']):.4f}，"
              f"模型 {float(brier[horizon]['model_brier']):.4f}；分箱净改进合计 {everything:+.4f}")


def main() -> int:
    needed = [REPORT, TOOL] + [TABLES / name for name in (
        "CHECK.json", "main.csv", "algorithms.csv", "algorithm-shares.csv", "reliability.csv", "neutral.csv",
        "spread-quantiles.csv", "funnel.csv", "budget.csv", "actual-costs.csv")]
    missing = [str(path.relative_to(ROOT)) for path in needed if not path.is_file()]
    if missing:
        print("找不到文件：", ", ".join(missing))
        return 2
    check_json = json.loads((TABLES / "CHECK.json").read_text(encoding="utf-8"))
    main_rows, algorithms, shares = read_csv("main.csv"), read_csv("algorithms.csv"), read_csv("algorithm-shares.csv")
    reliability, neutral, spread = read_csv("reliability.csv"), read_csv("neutral.csv"), read_csv("spread-quantiles.csv")
    funnel, budget, costs = read_csv("funnel.csv"), read_csv("budget.csv"), read_csv("actual-costs.csv")

    group_check_json(check_json, main_rows)
    group_main(main_rows)
    group_report(main_rows, check_json, costs)
    group_algorithms(main_rows, algorithms, shares, funnel)
    bins = group_diagnostics(main_rows, reliability, neutral, spread)
    group_funnel(main_rows, funnel)
    group_costs(budget, costs, check_json)

    names = {"甲": "CHECK.json 的哈希、计数和边界", "乙": "主表内部（技能、区间、半宽、结论重算）",
             "丙": "报告正文对 CSV 和 CHECK", "丁": "算法表", "戊": "算法占比", "己": "可靠性表",
             "庚": "中性概率最大占比", "辛": "概率差分位数", "壬": "漏斗表", "癸": "预算表和实际耗时表"}
    print("K45-R001 已入库文件的一致性核对")
    for key, title in names.items():
        print(f"  {key} {title}：{CHECKS.get(key, 0)} 项")
    print(f"  合计 {CHECKS['total']} 项，不一致 {len(PROBLEMS)} 项")
    for problem in PROBLEMS[:40]:
        print("  不一致：", problem)
    describe_classes(bins, main_rows)
    return 1 if PROBLEMS else 0


if __name__ == "__main__":
    sys.exit(main())
