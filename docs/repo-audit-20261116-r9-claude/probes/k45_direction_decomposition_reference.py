"""第九轮外部审核探针：三分类的“继续研究条件”分不清“会不会动”和“往哪边动”（只读）。

K45 的方向预测有三类：涨、跌、中性。R001 的判定只看三类合在一起的分数。
这个探针做三件事，全部用合成数据：

一、用独立写的代码重算 ``tools/experiments/k45_forecast_statistics.score_mode`` 的主表
    （损失、技能、95% 区间、家族区间、半宽、结论），逐格比较。
二、造一个“只知道价格会不会动、完全不知道往哪边动”的预测者，交给 ``score_mode``，
    看它能不能达到“继续研究条件”。
三、给出分解的参考实现：把对数损失精确拆成“会不会动”和“往哪边动”两部分；
    再按“模型偏多 / 偏空”分组，比较两组后来的涨跌。在第二步的预测者上，方向部分
    应当分不出正负；在一个真有方向信息的预测者上，方向部分应当为正。

可选的 ``--frozen-dir``：指向 R001 的封存目录（里面有 MINUTE-probabilities.json.gz 和
MINUTE-labels.json.gz）。探针先核对两份文件的哈希，再用独立实现复现已入库的主表，
逐格相同之后才输出分解。外部审核方没有这两份文件；这个模式是给项目内的独立审核者
做第二实现复算用的，只在第六批 K47 授权的范围内运行。

用法（在仓库根目录）：
    python -B -X utf8 docs/repo-audit-20261116-r9-claude/probes/k45_direction_decomposition_reference.py
    python -B -X utf8 docs/repo-audit-20261116-r9-claude/probes/k45_direction_decomposition_reference.py --quick
    python -B -X utf8 <同上> --frozen-dir <封存目录> [--json-out <仓库外的输出文件>] [--calendar-json <文件>]

退出码（合成模式）：0 第一步逐格相同，且第二步的现象没有出现；
                    1 第一步逐格相同，且现象存在（只知道会不会动也能达到继续条件）；
                    2 第一步有不一致，或内部恒等式不成立。
退出码（--frozen-dir）：0 完成；3 哈希不符、文件缺失，或复现不了已入库的主表（不输出分解）。
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import random
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from operator import mul
from pathlib import Path

CLASSES = ("UP", "DOWN", "NEUTRAL")
REPEATS = 20_000
SEED = 20_261_113
FAMILY = (0.003125, 0.996875)
MINUTE_HORIZONS = (1, 5, 15, 30, 60)
DAILY_HORIZONS = (1, 5, 20)
TOLERANCE = 1e-9


def repository_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "src" / "ak_trading").is_dir():
            return parent
    raise SystemExit(2)


ROOT = repository_root()


@dataclass(frozen=True)
class Row:
    request_id: str
    day: str
    horizon: int
    target: str
    model: tuple[float, float, float]
    raw: tuple[float, float, float]
    calibrated: tuple[float, float, float]
    ticks: float | None = None


# ------------------------------------------------------------------ 基本量
def brier(p, target) -> float:
    index = CLASSES.index(target)
    return math.fsum((value - (i == index)) ** 2 for i, value in enumerate(p))


def _negative_log(value: float) -> float:
    """概率为 0 时记无穷大，不加小数截断（与 R001 的评分口径相同）。"""
    return -math.log(value) if value > 0 else math.inf


def logloss(p, target) -> float:
    return _negative_log(p[CLASSES.index(target)])


def move_loss(p, target) -> float:
    """对数损失里“会不会动”的一半：把涨和跌并成“动”。"""
    return _negative_log(p[2]) if target == "NEUTRAL" else _negative_log(p[0] + p[1])


def direction_loss(p, target) -> float:
    """对数损失里“往哪边动”的一半：只在真的动了的行上计，按涨跌之间的相对概率。"""
    if target == "NEUTRAL":
        return 0.0
    moving = p[0] + p[1]
    return _negative_log(p[CLASSES.index(target)] / moving) if moving > 0 else math.inf


def move_brier(p, target) -> float:
    return (p[0] + p[1] - (target != "NEUTRAL")) ** 2


def direction_brier(p, target) -> float:
    if target == "NEUTRAL":
        return 0.0
    moving = p[0] + p[1]
    return (p[0] / moving - (target == "UP")) ** 2 if moving > 0 else math.inf


def percentile(values, q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    if lower == len(ordered) - 1:
        return ordered[lower]
    return ordered[lower] + (position - lower) * (ordered[lower + 1] - ordered[lower])


def weighted_quantile(pairs, q: float) -> float:
    """加权经验分布的广义逆：最小的 x，使累计权重 ≥ q × 总权重。"""
    merged = defaultdict(float)
    for value, weight in pairs:
        merged[value] += weight
    ordered = sorted(merged.items())
    target = q * math.fsum(weight for _, weight in ordered)
    running = 0.0
    for value, weight in ordered:
        running += weight
        if running >= target:
            return value
    return ordered[-1][0]


def block_counts(days: int, block: int, repeats: int = REPEATS):
    """设计第 5 节的循环移动块：同一个种子，按日历长度抽满后截断。"""
    rng = random.Random(SEED)
    for _ in range(repeats):
        sample: list[int] = []
        while len(sample) < days:
            start = rng.randrange(days)
            sample.extend((start + i) % days for i in range(block))
        counts = [0] * days
        for index in sample[:days]:
            counts[index] += 1
        yield counts


# ------------------------------------------------------------------ 统计引擎
class Engine:
    """每个统计量都是“若干个（按日加权的）比值”的函数；一次抽样循环全部算完。"""

    def __init__(self, calendar, block: int, repeats: int = REPEATS):
        self.calendar, self.block, self.repeats = list(calendar), block, repeats
        self.ratios: dict[str, tuple[list[float], list[float]]] = {}
        self.derived: dict[str, object] = {}

    def add_mean(self, name: str, per_day) -> None:
        self.ratios[name] = ([0.0 if v is None else v for v in per_day], [0.0 if v is None else 1.0 for v in per_day])

    def add_group(self, name: str, numerator, denominator) -> None:
        self.ratios[name] = (list(numerator), list(denominator))

    def add(self, name: str, function) -> None:
        self.derived[name] = function

    def evaluate(self, counts):
        values = {}
        for name, (numerator, denominator) in self.ratios.items():
            weight = sum(map(mul, counts, denominator))
            values[name] = sum(map(mul, counts, numerator)) / weight if weight else None
        return {name: function(values) for name, function in self.derived.items()}

    def run(self) -> dict[str, dict]:
        point = self.evaluate([1] * len(self.calendar))
        draws = {name: [] for name in self.derived}
        invalid: Counter = Counter()
        for counts in block_counts(len(self.calendar), self.block, self.repeats):
            for name, value in self.evaluate(counts).items():
                if value is None or not math.isfinite(value):
                    invalid[name] += 1
                else:
                    draws[name].append(value)
        result = {}
        for name in self.derived:
            valid = (point[name] is not None and math.isfinite(point[name]) and not invalid[name]
                     and len(draws[name]) == self.repeats)
            result[name] = {
                "point": point[name], "invalid": invalid[name],
                "ci95": (percentile(draws[name], .025), percentile(draws[name], .975)) if valid else None,
                "family": (percentile(draws[name], FAMILY[0]), percentile(draws[name], FAMILY[1])) if valid else None}
        return result


def _skill(model_key: str, baseline_key: str):
    def function(values):
        model, baseline = values[model_key], values[baseline_key]
        if model is None or baseline is None or baseline <= 0:
            return None
        return 1 - model / baseline
    return function


def _gain(baseline_key: str, model_key: str):
    def function(values):
        model, baseline = values[model_key], values[baseline_key]
        return None if model is None or baseline is None else baseline - model
    return function


def _difference(first: str, second: str, scale: float = 1.0):
    def function(values):
        a, b = values[first], values[second]
        return None if a is None or b is None else scale * (a - b)
    return function


def common_rows(rows):
    """与 paired_rows 相同的口径：标签是三类之一，三套概率都合法。"""
    def valid(p):
        return (p is not None and len(p) == 3 and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(v) and 0 <= v <= 1 for v in p) and abs(math.fsum(p) - 1) <= 1e-9)
    return [row for row in rows if row.target in CLASSES and valid(row.model) and valid(row.raw) and valid(row.calibrated)]


def register(engine: Engine, horizon: int, rows, *, decomposition: bool) -> dict:
    """登记一个期限的全部统计量；返回这个期限的描述信息（行数、日数、分组人数）。"""
    index = {day: i for i, day in enumerate(engine.calendar)}
    by_day: list[list[Row]] = [[] for _ in engine.calendar]
    for row in rows:
        by_day[index[row.day]].append(row)
    key = f"{horizon}:"

    def per_day(function, name):
        return [math.fsum(function(getattr(row, name), row.target) for row in group) / len(group) if group else None
                for group in by_day]

    parts = {"brier": brier, "logloss": logloss}
    if decomposition:
        parts.update({"move": move_loss, "dir": direction_loss, "bmove": move_brier, "bdir": direction_brier})
    for part, function in parts.items():
        for name in ("model", "raw", "calibrated"):
            engine.add_mean(f"{key}{part}:{name}", per_day(function, name))
    for baseline in ("raw", "calibrated"):
        for metric in ("brier", "logloss"):
            engine.add(f"{key}skill:{baseline}_{metric}", _skill(f"{key}{metric}:model", f"{key}{metric}:{baseline}"))
    info = {"rows": len(rows), "days": sum(1 for group in by_day if group)}
    if not decomposition:
        return info

    for baseline in ("raw", "calibrated"):
        for part in ("move", "dir", "logloss"):
            engine.add(f"{key}gain:{baseline}:{part}", _gain(f"{key}{part}:{baseline}", f"{key}{part}:model"))
    engine.add(f"{key}skill:move_brier", _skill(f"{key}bmove:model", f"{key}bmove:raw"))
    engine.add(f"{key}skill:direction_brier", _skill(f"{key}bdir:model", f"{key}bdir:raw"))

    # 偏多 / 偏空分组。权重是每行 1 / 当日行数，与主表的“按日等权”一致。
    def spread(row):
        return row.model[0] - row.model[1]

    def balance(row):
        return (row.target == "UP") - (row.target == "DOWN")

    weights = [(spread(row), 1 / len(group)) for group in by_day for row in group]
    low, high = (weighted_quantile(weights, 0.10), weighted_quantile(weights, 0.90)) if weights else (0.0, 0.0)
    groups = {"bull": lambda row: spread(row) > 0, "bear": lambda row: spread(row) < 0,
              "strong_bull": lambda row: spread(row) > 0 and spread(row) >= high,
              "strong_bear": lambda row: spread(row) < 0 and spread(row) <= low}
    has_ticks = all(row.ticks is not None for row in rows)
    for name, member in groups.items():
        chosen = [[row for row in group if member(row)] for group in by_day]
        denominator = [len(c) / len(group) if group else 0.0 for c, group in zip(chosen, by_day)]
        engine.add_group(f"{key}{name}:balance",
                         [sum(balance(row) for row in c) / len(group) if group else 0.0 for c, group in zip(chosen, by_day)],
                         denominator)
        if has_ticks:
            engine.add_group(f"{key}{name}:ticks",
                             [math.fsum(row.ticks for row in c) / len(group) if group else 0.0 for c, group in zip(chosen, by_day)],
                             denominator)
        info[name + "_rows"] = sum(len(c) for c in chosen)
    for prefix in ("", "strong_"):
        engine.add(f"{key}{prefix}delta:balance", _difference(f"{key}{prefix}bull:balance", f"{key}{prefix}bear:balance"))
        if has_ticks:
            engine.add(f"{key}{prefix}delta:ticks", _difference(f"{key}{prefix}bull:ticks", f"{key}{prefix}bear:ticks"))
    if has_ticks:
        # 顺着最强一成信号做（强偏多时买、强偏空时卖，拿到期限结束），每次平均多拿的跳动点。
        engine.add(f"{key}strong_edge:ticks", _difference(f"{key}strong_bull:ticks", f"{key}strong_bear:ticks", 0.5))
    info.update({"zero_spread_rows": sum(1 for row in rows if spread(row) == 0), "spread_q10": low, "spread_q90": high,
                 "has_ticks": has_ticks})

    # 参照数（描述）：用这一段自己的三类占比（按日等权）作常数预测，损失是多少。
    days = [group for group in by_day if group]
    base = tuple(math.fsum(sum(row.target == label for row in group) / len(group) for group in days) / len(days)
                 for label in CLASSES)
    if all(value > 0 for value in base):
        info["own_frequency"] = base
        info["own_frequency_brier"] = math.fsum(math.fsum(brier(base, row.target) for row in group) / len(group)
                                                for group in days) / len(days)
        info["own_frequency_logloss"] = math.fsum(math.fsum(logloss(base, row.target) for row in group) / len(group)
                                                  for group in days) / len(days)

    # 恒等式：每一行上，“会不会动”+“往哪边动”= 原来的对数损失。
    worst = max((abs(move_loss(getattr(row, name), row.target) + direction_loss(getattr(row, name), row.target)
                     - logloss(getattr(row, name), row.target)) for row in rows for name in ("model", "raw", "calibrated")
                 if math.isfinite(logloss(getattr(row, name), row.target))), default=0.0)
    info["chain_identity_error"] = worst
    return info


def monthly(rows) -> list[dict]:
    """按月份的点估计（不带区间）：月内仍按日等权。"""
    months = defaultdict(lambda: defaultdict(list))
    for row in rows:
        months[row.day[:7]][row.day].append(row)
    result = []
    for month, days in sorted(months.items()):
        def mean(function, name):
            return math.fsum(math.fsum(function(getattr(row, name), row.target) for row in group) / len(group)
                             for group in days.values()) / len(days)
        result.append({"month": month, "days": len(days), "rows": sum(len(group) for group in days.values()),
                       "brier_skill": 1 - mean(brier, "model") / mean(brier, "raw"),
                       "gain_move": mean(move_loss, "raw") - mean(move_loss, "model"),
                       "gain_direction": mean(direction_loss, "raw") - mean(direction_loss, "model")})
    return result


def binned_neutral_share(rows) -> float:
    """与已入库的可靠性表同样的分箱粗算：三类净改进里“中性”一类占多少。

    每类按模型给的概率分成 10 个等宽箱；箱内的预测均值和实际频率按日等权，
    跨箱按行数加权（与 k45_r001_published_tables_recheck.py 的描述性输出相同）。
    """
    counts = Counter(row.day for row in rows)
    weight = {day: 1 / (len(counts) * count) for day, count in counts.items()}
    net = {}
    for index, label in enumerate(CLASSES):
        bins = defaultdict(list)
        for row in rows:
            bins[min(9, int(row.model[index] * 10))].append(row)
        cells = []
        for group in bins.values():
            total = math.fsum(weight[row.day] for row in group)
            cells.append((len(group), math.fsum(weight[row.day] * row.model[index] for row in group) / total,
                          math.fsum(weight[row.day] * (row.target == label) for row in group) / total))
        base = sum(n * actual for n, _, actual in cells) / len(rows)
        net[label] = sum(n * ((actual - base) ** 2 - (predicted - actual) ** 2) for n, predicted, actual in cells) / len(rows)
    return net["NEUTRAL"] / sum(net.values())


def classify_main(stats: dict, horizon: int, days: int, algorithm: str | None, inner_sufficient: bool) -> tuple[str, float | None]:
    """设计第 5 节和 10.7 的固定顺序，独立重写。"""
    key = f"{horizon}:skill:"
    metrics = [stats[key + name] for name in ("raw_brier", "raw_logloss", "calibrated_brier", "calibrated_logloss")]
    raw = stats[key + "raw_brier"]
    half = (raw["ci95"][1] - raw["ci95"][0]) / 2 if raw["ci95"] else None
    if days < 60 or not inner_sufficient or algorithm is None or any(m["ci95"] is None for m in metrics):
        return "INSUFFICIENT", half
    if (raw["family"][0] > 0 and stats[key + "raw_logloss"]["ci95"][0] > 0
            and stats[key + "calibrated_brier"]["ci95"][0] > 0 and algorithm != "UNCONDITIONAL_FREQUENCY"):
        return "CONTINUE", half
    if raw["ci95"][0] > 0 and raw["family"][0] <= 0:
        return "SUGGESTIVE", half
    if algorithm == "UNCONDITIONAL_FREQUENCY":
        return "BASE_RATE", half
    if raw["family"][0] > 0:
        return "MIXED", half
    return ("NO_INFORMATION" if half is not None and half <= 0.02 else "UNRESOLVED"), half


def classify_direction(stats: dict, horizon: int) -> str:
    """有没有方向信息（J53 的第一个问题），K47 预先写定的读法。"""
    gain = stats[f"{horizon}:gain:raw:dir"]
    delta = stats[f"{horizon}:delta:balance"]
    if gain["ci95"] is None or delta["ci95"] is None:
        return "NOT_ASSESSED"
    if gain["family"][0] > 0 and delta["ci95"][0] > 0:
        return "DIRECTION_SHOWN"
    if gain["ci95"][0] > 0 or delta["ci95"][0] > 0:
        return "DIRECTION_SUGGESTIVE"
    if gain["ci95"][1] < 0:
        return "DIRECTION_NEGATIVE"
    return "DIRECTION_NOT_SHOWN"


def classify_cost(stats: dict, horizon: int) -> str:
    """够不够付价差（J53 的第二个问题）：按市价一买一卖至少付出 1 个跳动点。"""
    edge = stats.get(f"{horizon}:strong_edge:ticks")
    if edge is None or edge["ci95"] is None:
        return "NOT_ASSESSED"
    if edge["ci95"][0] > 1:
        return "PAYS_SPREAD"
    if edge["ci95"][1] < 1:
        return "CANNOT_PAY_SPREAD"
    return "UNRESOLVED"


COST_TEXT = {"PAYS_SPREAD": "付得起价差", "CANNOT_PAY_SPREAD": "付不起价差", "UNRESOLVED": "分辨不了",
             "NOT_ASSESSED": "无法评估"}
DIRECTION_TEXT = {"DIRECTION_SHOWN": "显示出方向信息", "DIRECTION_SUGGESTIVE": "提示性，未达到预定条件",
                  "DIRECTION_NEGATIVE": "方向部分为负", "DIRECTION_NOT_SHOWN": "没有显示方向信息",
                  "NOT_ASSESSED": "无法评估"}


# ------------------------------------------------------------------ 合成数据
def synthetic_calendar(days: int) -> list[str]:
    result, current = [], date(2024, 6, 3)
    while len(result) < days:
        if current.weekday() < 5:
            result.append(current.isoformat())
        current += timedelta(days=1)
    return result


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def tempered(p, temperature: float):
    powered = [value ** (1 / temperature) for value in p]
    total = math.fsum(powered)
    return tuple(value / total for value in powered)


def world(calendar, horizon: int, *, directional: bool, seed: int, per_day: int = 64):
    """一个合成市场和一个预测者。

    市场：每个时刻有一个“活跃度”，活跃时价格更可能动；动了以后涨还是跌，
    ``directional=False`` 时是一枚与任何已知信息无关的硬币，``True`` 时与一个信号有关。
    预测者：知道活跃度（所以会预测“会不会动”）。``directional=False`` 时它对涨跌的
    倾向是纯噪声；``True`` 时它知道那个信号。
    """
    rng = random.Random(seed)

    def draw(activity: float, signal: float):
        neutral = sigmoid(0.15 - 0.75 * activity)
        up_given_move = sigmoid(math.log(0.49 / 0.51) + (0.35 * signal if directional else 0.0))
        if rng.random() < neutral:
            return "NEUTRAL", float(rng.randint(-2, 2))
        size = 3 + min(int(rng.expovariate(0.5)), 12)
        return ("UP", float(size)) if rng.random() < up_given_move else ("DOWN", float(-size))

    training = Counter()
    for _ in range(17_909):
        training[draw(rng.gauss(0, 0.45) + rng.gauss(0, 0.6), rng.gauss(0, 1))[0]] += 1
    total = sum(training.values()) + 3
    raw = tuple((training[label] + 1) / total for label in CLASSES)
    calibrated = tempered(raw, 1.5)
    rows = []
    for day in calendar[1:]:  # 第一天整日没有样本，模仿检验段首日被隔离
        day_effect = rng.gauss(0, 0.45)
        for slot in range(per_day):
            pattern = 0.9 * math.exp(-slot / 6) + 0.5 * math.exp(-(per_day - 1 - slot) / 8) - 0.35
            activity = day_effect + pattern + rng.gauss(0, 0.5)
            signal = rng.gauss(0, 1)
            target, ticks = draw(activity, signal)
            neutral = sigmoid(0.15 - 0.6 * activity)
            if directional:
                share = sigmoid(math.log(0.49 / 0.51) + 0.35 * signal)
            else:
                share = min(0.95, max(0.05, 0.49 + rng.gauss(0, 0.055)))
            model = ((1 - neutral) * share, (1 - neutral) * (1 - share), neutral)
            rows.append(Row(f"h{horizon}-{day}-{slot}", day, horizon, target, model, raw, calibrated, ticks))
    return rows


# ------------------------------------------------------------------ 第一步
def compare_with_repository(repeats: int) -> tuple[int, list[str]]:
    sys.path.insert(0, str(ROOT / "tools" / "experiments"))
    import k45_forecast_statistics as repository  # noqa: E402  仓库里的被核对对象

    problems: list[str] = []
    checks = 0
    if repeats != repository.BOOTSTRAP_REPEATS:
        repository.BOOTSTRAP_REPEATS = repeats  # 仅 --quick：两边用同样少的抽样次数
    for mode, horizons, block, days in (("MINUTE", MINUTE_HORIZONS, 2, 92), ("DAILY", DAILY_HORIZONS, 20, 92)):
        calendar = synthetic_calendar(days)
        rows = []
        for i, horizon in enumerate(horizons):
            rows.extend(world(calendar, horizon, directional=(i % 2 == 1), seed=100 + i, per_day=12 if mode == "MINUTE" else 1))
        algorithms = {h: ("UNCONDITIONAL_FREQUENCY" if i == 0 else "L2_SOFTMAX") for i, h in enumerate(horizons)}
        observations = [repository.Observation(r.request_id, r.day, mode, r.horizon, r.target, r.model, r.raw, r.calibrated)
                        for r in rows]
        theirs = repository.score_mode(observations, calendar, mode, algorithms, True)
        engine = Engine(calendar, block, repeats)
        info = {h: register(engine, h, common_rows([r for r in rows if r.horizon == h]), decomposition=False) for h in horizons}
        mine = engine.run()
        for cell in theirs["cells"]:
            h = cell["horizon"]
            for name in ("model", "raw", "calibrated"):
                for metric in ("brier", "logloss"):
                    numerator, denominator = engine.ratios[f"{h}:{metric}:{name}"]
                    mean = sum(numerator) / sum(denominator)  # 按日等权的损失
                    checks += 1
                    if abs(mean - cell["losses"][name][metric]) > TOLERANCE:
                        problems.append(f"{mode}/{h} {name} {metric} 损失不同")
            for key in ("raw_brier", "raw_logloss", "calibrated_brier", "calibrated_logloss"):
                a, b = mine[f"{h}:skill:{key}"], cell["metrics"][key]
                checks += 2
                if abs(a["point"] - b["skill"]) > TOLERANCE:
                    problems.append(f"{mode}/{h} {key} 技能不同")
                if b["ci95"] is None or a["ci95"] is None or max(abs(x - y) for x, y in zip(a["ci95"], b["ci95"])) > TOLERANCE:
                    problems.append(f"{mode}/{h} {key} 的 95% 区间不同")
            a, b = mine[f"{h}:skill:raw_brier"]["family"], cell["metrics"]["raw_brier"]["ci_family"]
            checks += 1
            if a is None or b is None or max(abs(x - y) for x, y in zip(a, b)) > TOLERANCE:
                problems.append(f"{mode}/{h} 家族区间不同")
            status, half = classify_main(mine, h, info[h]["days"], algorithms[h], True)
            checks += 2
            if status != cell["status"]:
                problems.append(f"{mode}/{h} 结论不同：{status} 对 {cell['status']}")
            if half is None or cell["half_width95"] is None or abs(half - cell["half_width95"]) > TOLERANCE:
                problems.append(f"{mode}/{h} 半宽不同")
    return checks, problems


# ------------------------------------------------------------------ 输出
def interval(item, digits: int = 4) -> str:
    if item["ci95"] is None:
        return "不可用"
    return f"{item['point']:+.{digits}f} [{item['ci95'][0]:+.{digits}f}, {item['ci95'][1]:+.{digits}f}]"


def report_decomposition(stats: dict, info: dict, horizons, title: str) -> dict[int, str]:
    print(f"\n{title}")
    print("  对数损失的改进（原始对照 − 模型，越大越好）拆成两部分，括号里是 95% 区间：")
    print(f"  {'期限':>4} {'合计':>30} {'会不会动':>30} {'往哪边动':>30} {'往哪边动的家族区间下界':>12}  读法")
    verdicts = {}
    for h in horizons:
        total, move, direction = (stats[f"{h}:gain:raw:{part}"] for part in ("logloss", "move", "dir"))
        verdicts[h] = classify_direction(stats, h)
        family = "不可用" if direction["family"] is None else f"{direction['family'][0]:+.4f}"
        print(f"  {h:>4} {interval(total):>30} {interval(move):>30} {interval(direction):>30} {family:>12}  {DIRECTION_TEXT[verdicts[h]]}")
    print("  偏多组减偏空组（偏多 = 模型给的涨的概率大于跌的概率）：")
    print(f"  {'期限':>4} {'偏多/偏空行数':>14} {'涨跌占比之差':>30} {'后来的涨跌，跳动点':>30} {'最强一成：涨跌占比之差':>30} {'最强一成：跳动点':>30}")
    for h in horizons:
        ticks = interval(stats[f"{h}:delta:ticks"], 3) if info[h]["has_ticks"] else "没有价格"
        strong_ticks = interval(stats[f"{h}:strong_delta:ticks"], 3) if info[h]["has_ticks"] else "没有价格"
        print(f"  {h:>4} {info[h]['bull_rows']:>7}/{info[h]['bear_rows']:<6} {interval(stats[f'{h}:delta:balance']):>30} "
              f"{ticks:>30} {interval(stats[f'{h}:strong_delta:balance']):>30} {strong_ticks:>30}")
    print("  够不够付价差：顺着最强一成信号做，每次平均多拿的跳动点（= 上表“最强一成：跳动点”的一半）；按市价一买一卖至少付出 1 个：")
    for h in horizons:
        if info[h]["has_ticks"]:
            print(f"  {h:>4} {interval(stats[f'{h}:strong_edge:ticks'], 3)}  {COST_TEXT[classify_cost(stats, h)]}")
    print("  二分类 Brier 技能（同方向核对）：")
    for h in horizons:
        print(f"  {h:>4} 会不会动 {interval(stats[f'{h}:skill:move_brier'])}；往哪边动 {interval(stats[f'{h}:skill:direction_brier'])}")
    print("  参照数（描述）：用这一段自己的三类占比作常数预测的三类 Brier / 对数损失：")
    for h in horizons:
        if "own_frequency" in info[h]:
            up, down, neutral = info[h]["own_frequency"]
            print(f"  {h:>4} 占比 涨 {up:.1%} / 跌 {down:.1%} / 持平 {neutral:.1%}；Brier {info[h]['own_frequency_brier']:.4f}；"
                  f"对数损失 {info[h]['own_frequency_logloss']:.4f}")
    return verdicts


def synthetic_mode(repeats: int) -> int:
    started = time.perf_counter()
    checks, problems = compare_with_repository(repeats)
    print("一、独立重算仓库里的统计函数（合成数据，分钟和日线两种抽样块）")
    print(f"  {checks} 项比较，不同 {len(problems)} 项（容差 {TOLERANCE:g}）")
    for problem in problems[:20]:
        print("  不同：", problem)

    sys.path.insert(0, str(ROOT / "tools" / "experiments"))
    import k45_forecast_statistics as repository  # noqa: E402

    calendar = synthetic_calendar(92)
    outcome = {}
    identity = 0.0
    for name, directional in (("只知道会不会动", False), ("另外还知道往哪边动", True)):
        rows = world(calendar, 5, directional=directional, seed=20261116)
        observations = [repository.Observation(r.request_id, r.day, "MINUTE", r.horizon, r.target, r.model, r.raw, r.calibrated)
                        for r in rows]
        theirs = repository.score_mode(observations, calendar, "MINUTE", {5: "L2_SOFTMAX"}, True)
        cell = next(c for c in theirs["cells"] if c["horizon"] == 5)
        engine = Engine(calendar, 2, repeats)
        info = {5: register(engine, 5, common_rows(rows), decomposition=True)}
        stats = engine.run()
        identity = max(identity, info[5]["chain_identity_error"])
        total = stats["5:gain:raw:logloss"]["point"]
        parts = stats["5:gain:raw:move"]["point"] + stats["5:gain:raw:dir"]["point"]
        identity = max(identity, abs(total - parts))
        raw = cell["metrics"]["raw_brier"]
        print(f"\n二/三、合成预测者：{name}（91 个有样本的交易日，{info[5]['rows']} 行）")
        print(f"  仓库的 score_mode：三分类 Brier 技能 {raw['skill']:+.4f}，95% 区间 [{raw['ci95'][0]:+.4f}, {raw['ci95'][1]:+.4f}]，"
              f"家族区间下界 {raw['ci_family'][0]:+.4f}")
        print(f"  仓库给的结论：{cell['status']}（{cell['conclusion']}）")
        verdict = report_decomposition(stats, info, (5,), "  分解的参考实现：")[5]
        outcome[name] = (cell["status"], verdict)
    print("\n四、只看可靠性表能看出多少（粗算，四个随机种子）")
    for name, directional in (("只知道会不会动", False), ("另外还知道往哪边动", True)):
        shares = [binned_neutral_share(common_rows(world(calendar, 5, directional=directional, seed=seed)))
                  for seed in (20261116, 1, 2, 3)]
        print(f"  {name}：三类净改进里“中性”一类占 {min(shares):.0%}–{max(shares):.0%}")
    print("  对照：R001 已入库的可靠性表按同样的办法粗算，5 分钟是 80%（见 k45_r001_published_tables_recheck.py 的输出）。")
    print(f"\n恒等式“会不会动 + 往哪边动 = 原对数损失”的最大偏差：{identity:.2e}")
    print(f"用时 {time.perf_counter() - started:.1f} 秒")

    blind, informed = outcome["只知道会不会动"], outcome["另外还知道往哪边动"]
    if problems or identity > 1e-9:
        print("\n结果：第一步有不一致，或恒等式不成立。")
        return 2
    present = blind[0] == "CONTINUE" and blind[1] in ("DIRECTION_NOT_SHOWN", "DIRECTION_NEGATIVE")
    separated = informed[1] == "DIRECTION_SHOWN"
    if present:
        print("\n结果：现象存在。一个完全不知道涨跌方向的预测者，只靠“会不会动”就达到了“继续研究条件”。"
              f"\n      分解能把两种预测者分开：{'是' if separated else '否'}。")
        return 1
    print("\n结果：现象没有出现。")
    return 0


# ------------------------------------------------------------------ 封存文件模式
def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def frozen_mode(directory: Path, json_out: Path | None, calendar_json: Path | None = None) -> int:
    if json_out is not None and ROOT in json_out.resolve().parents:
        print("输出文件必须在仓库之外。")
        return 3
    tables = ROOT / "docs" / "research" / "k45-forecast-skill-r001"
    with (tables / "main.csv").open(encoding="utf-8", newline="") as stream:
        published = {int(row["horizon"]): row for row in csv.DictReader(stream) if row["mode"] == "MINUTE"}
    files = {"probabilities": directory / "MINUTE-probabilities.json.gz", "labels": directory / "MINUTE-labels.json.gz"}
    for name, path in files.items():
        if not path.is_file():
            print(f"找不到封存文件：{path.name}")
            return 3
        expected = {row[f"{name}_sha256"] for row in published.values()}
        if expected != {sha256(path)}:
            print(f"封存文件 {path.name} 的哈希与已入库的 main.csv 不同，停止。")
            return 3
    predictions = json.loads(gzip.decompress(files["probabilities"].read_bytes()))
    labels = {item["request_id"]: item for item in json.loads(gzip.decompress(files["labels"].read_bytes()))}
    rows = []
    for item in predictions:
        label = labels.get(item["request_id"])
        mature = label is not None and label["status"] == "MATURE"
        rows.append(Row(item["request_id"], item["day"], int(item["horizon"]),
                        label["direction"] if mature else None,
                        tuple(item["model"]) if item["model"] else None, tuple(item["raw"]) if item["raw"] else None,
                        tuple(item["calibrated"]) if item["calibrated"] else None,
                        float(label["terminal_points"]) if mature and label["terminal_points"] is not None else None))
    # 检验段日历：有样本的交易日，加上被整日隔离的首日 2024-06-03（设计第 2 节、漏斗表）。
    # 抽样按日历位置成块，所以没有样本的交易日也要占位；也可以用 --calendar-json 直接给出 92 个交易日。
    if calendar_json is not None:
        calendar = sorted(json.loads(calendar_json.read_text(encoding="utf-8")))
    else:
        calendar = sorted({row.day for row in rows} | {"2024-06-03"})
    if len(calendar) != 92 or len(set(calendar)) != 92 or not {row.day for row in rows} <= set(calendar):
        print(f"检验段日历不是 92 个互不相同、覆盖全部样本的交易日（{len(calendar)}），停止。")
        return 3
    engine = Engine(calendar, 2)
    info = {h: register(engine, h, common_rows([r for r in rows if r.horizon == h]), decomposition=True)
            for h in MINUTE_HORIZONS}
    stats = engine.run()
    mismatches = []
    for h, row in published.items():
        if info[h]["rows"] != int(row["common_rows"]) or info[h]["days"] != int(row["common_days"]):
            mismatches.append(f"{h} 分钟的行数或日数")
        for key in ("raw_brier", "raw_logloss", "calibrated_brier", "calibrated_logloss"):
            item = stats[f"{h}:skill:{key}"]
            pairs = [(item["point"], float(row[f"{key}_skill"])), (item["ci95"][0], float(row[f"{key}_ci95_lower"])),
                     (item["ci95"][1], float(row[f"{key}_ci95_upper"]))]
            if key == "raw_brier":
                pairs += [(item["family"][0], float(row["raw_brier_ci_family_lower"])),
                          (item["family"][1], float(row["raw_brier_ci_family_upper"]))]
            if any(abs(a - b) > TOLERANCE for a, b in pairs):
                mismatches.append(f"{h} 分钟的 {key}")
    if mismatches:
        print("独立实现复现不了已入库的主表，停止，不输出分解：", "、".join(mismatches))
        return 3
    print("封存文件哈希相符；独立实现复现了已入库主表的全部技能和区间（容差 1e-9）。")
    print("以下是对同一批已评分的检验段做的事后分解，不是新的盲检，不改变 R001 的结论。")
    print("跳动点按标签里的价格点数计；螺纹钢的最小变动价位是 1 元/吨，点数就是跳动点数。")
    verdicts = report_decomposition(stats, info, MINUTE_HORIZONS, "R001 分钟模式的分解（第二实现）")
    months = {h: monthly(common_rows([r for r in rows if r.horizon == h])) for h in MINUTE_HORIZONS}
    print("\n按月份（点估计，不带区间）：三分类 Brier 技能 / 会不会动的改进 / 往哪边动的改进")
    for h in MINUTE_HORIZONS:
        print(f"  {h:>3} 分钟：" + "；".join(f"{m['month']} {m['brier_skill']:+.4f} / {m['gain_move']:+.4f} / {m['gain_direction']:+.4f}"
                                           for m in months[h]))
    if json_out is not None:
        payload = {"schema": "r9-external-reference-decomposition-v1", "repeats": REPEATS, "seed": SEED,
                   "info": {str(h): info[h] for h in MINUTE_HORIZONS}, "statistics": stats,
                   "verdicts": {str(h): verdicts[h] for h in MINUTE_HORIZONS},
                   "cost_verdicts": {str(h): classify_cost(stats, h) for h in MINUTE_HORIZONS},
                   "monthly": {str(h): months[h] for h in MINUTE_HORIZONS}}
        json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        print(f"\n全部数值已写入 {json_out}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quick", action="store_true", help="合成模式只抽 2,000 次（约十分之一的时间）；结论仍以完整运行为准")
    parser.add_argument("--frozen-dir", type=Path, default=None)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--calendar-json", type=Path, default=None,
                        help="可选：检验段 92 个交易日的 JSON 数组；不给时用“有样本的日子 + 2024-06-03”")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.frozen_dir is not None:
        return frozen_mode(args.frozen_dir, args.json_out, args.calendar_json)
    return synthetic_mode(2_000 if args.quick else REPEATS)


if __name__ == "__main__":
    sys.exit(main())
