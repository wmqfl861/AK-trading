#!/usr/bin/env python3
"""K45 的拟合要多久：按设计里的样本量，测生产拟合函数的耗时。

只读、只用标准库、合成数据。直接调用 ``ak_trading.v2.probability.models`` 里的
生产函数，参数取 ``default_fit_config`` 的默认值（500 次迭代、50 棵树）。
为了让探针几分钟内跑完，迭代次数和树的棵数按比例缩小后再线性换算回默认值：
这两个循环每一轮的工作量相同，探针用两个规模各测一次来核对这一点。

样本量取 K45 设计 §8 的上界：分钟模式训练段 17,909 个决策时刻，
内层拟合用其中 70% 的交易日，约 12,536 个。

输出三张表：
1. 单个候选算法在两个样本量上的耗时（换算到默认配置）；
2. 一个期限里各类任务的耗时；
3. 分钟模式五个期限合计：全部任务、去掉时间类任务、只拟合方向任务，各要多久。

在仓库根目录运行：
    python docs/repo-audit-20261114-claude/probes/k45_fit_cost_probe.py
    python docs/repo-audit-20261114-claude/probes/k45_fit_cost_probe.py --quick   # 小样本自检

只是测量，退出码恒为 0。耗时与机器有关，看的是两种做法的倍数。
"""
from __future__ import annotations

import argparse
import math
import random
import sys
import time
from pathlib import Path

for parent in (*Path(__file__).resolve().parents, Path.cwd()):
    if (parent / "src" / "ak_trading").is_dir():
        sys.path.insert(0, str(parent / "src"))
        break

from ak_trading.v2.probability import models as m  # noqa: E402

FEATURES = 10                     # 设计 §3：十个已注册因子
TRAIN_ANCHORS = 17_909            # 设计 §8：分钟模式训练段的决策时刻上界
INNER_FRACTION = 0.70             # default_fit_config: inner_split_fraction
MINUTE_HORIZONS = (1, 5, 15, 30, 60)
PERSIST_EVENTS = 7                # contracts.PERSISTENCE_EVENTS
SOFTMAX_PROBE_ITERATIONS = 20
GBM_PROBE_TREES = 2
QUANTILE_PROBE_ITERATIONS = 20


def synth(n: int, classes: int, seed: int):
    rng = random.Random(seed)
    X = [tuple(rng.gauss(0.0, 1.0) for _ in range(FEATURES)) for _ in range(n)]
    weights = [0.42, 0.42, 0.16, 0.10][:classes]
    y = rng.choices(range(classes), weights=weights, k=n)
    return X, y


def clock(function, *args):
    start = time.perf_counter()
    result = function(*args)
    return time.perf_counter() - start, result


def measure_softmax(n: int, classes: int, iterations: int) -> float:
    X, y = synth(n, classes, 11)
    seconds, fitted = clock(m._softmax_gd, X, y, classes, 1.0, 0.01, iterations)
    assert fitted is not None
    return seconds


def measure_gbm(n: int, classes: int, trees: int) -> float:
    X, y = synth(n, classes, 13)
    keys = [f"f{i}" for i in range(FEATURES)]
    config = m.default_fit_config(tuple(keys), 1, 2, 3, tree_count=trees)
    names = ("UP", "DOWN", "NEUTRAL", "AMBIGUOUS")[:classes]
    seconds, model = clock(m._fit_gbm, X, y, names, config, [0.0] * FEATURES,
                           [1.0] * FEATURES, keys)
    assert model is not None and len(model["trees"]) == trees
    return seconds


def measure_linear_quantile(n: int, iterations: int) -> float:
    X, _ = synth(n, 2, 17)
    rng = random.Random(19)
    values = [rng.gauss(0.0, 1.0) for _ in range(n)]
    keys = [f"f{i}" for i in range(FEATURES)]
    config = m.default_fit_config(tuple(keys), 1, 2, 3, max_iterations=iterations)
    seconds, _ = clock(m._fit_linear_quantile, X, values, [0.5], config, keys,
                       [0.0] * FEATURES, [1.0] * FEATURES)
    return seconds        # 一个分位数；默认配置有五个


def fmt(seconds: float) -> str:
    if seconds < 120:
        return f"{seconds:7.1f} 秒"
    if seconds < 7200:
        return f"{seconds / 60:7.1f} 分"
    return f"{seconds / 3600:7.1f} 时"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="小样本自检，不用于报告")
    args = parser.parse_args()
    n_all = 1_200 if args.quick else TRAIN_ANCHORS
    n_fit = int(n_all * INNER_FRACTION)
    default_iterations = int(m.default_fit_config((), 1, 2, 3)["max_iterations"])
    default_trees = int(m.default_fit_config((), 1, 2, 3)["tree_count"])

    print(f"样本量：内层拟合 {n_fit:,}，全训练段重拟合 {n_all:,}；特征 {FEATURES} 个")
    print(f"默认配置：softmax {default_iterations} 次迭代，提升树 {default_trees} 棵，"
          f"每个特征最多 {m.TREE_CANDIDATE_LIMIT} 个切分点")

    # 线性核对：迭代次数翻倍、棵数翻倍，耗时应接近翻倍
    small = max(400, n_fit // 8)
    a = measure_softmax(small, 3, SOFTMAX_PROBE_ITERATIONS)
    b = measure_softmax(small, 3, SOFTMAX_PROBE_ITERATIONS * 2)
    c = measure_gbm(small, 3, GBM_PROBE_TREES)
    d = measure_gbm(small, 3, GBM_PROBE_TREES * 2)
    print(f"线性核对（n={small:,}）：softmax 迭代 ×2 → 耗时 ×{b / a:.2f}；"
          f"提升树棵数 ×2 → 耗时 ×{d / c:.2f}")

    softmax, gbm = {}, {}
    for n in (n_fit, n_all):
        for classes in (2, 3, 4):
            softmax[n, classes] = (measure_softmax(n, classes, SOFTMAX_PROBE_ITERATIONS)
                                   * default_iterations / SOFTMAX_PROBE_ITERATIONS)
            gbm[n, classes] = (measure_gbm(n, classes, GBM_PROBE_TREES)
                               * default_trees / GBM_PROBE_TREES)
    quantile = {n: measure_linear_quantile(n, QUANTILE_PROBE_ITERATIONS)
                * default_iterations / QUANTILE_PROBE_ITERATIONS * 5 for n in (n_fit, n_all)}

    print()
    print("表 1  单个候选算法一次拟合（换算到默认配置）")
    print(f"{'算法':<26}{'内层 n=' + format(n_fit, ','):>18}{'重拟合 n=' + format(n_all, ','):>20}")
    rows = [("softmax，2 类（二分类、触碰风险）", softmax, 2),
            ("softmax，3 类（方向、竞争风险）", softmax, 3),
            ("softmax，4 类（路径）", softmax, 4),
            ("提升树，2 类", gbm, 2), ("提升树，3 类（方向）", gbm, 3),
            ("提升树，4 类（路径）", gbm, 4)]
    for name, table, classes in rows:
        print(f"{name:<26}{fmt(table[n_fit, classes]):>18}{fmt(table[n_all, classes]):>20}")
    print(f"{'线性分位数，5 个分位数':<26}{fmt(quantile[n_fit]):>18}{fmt(quantile[n_all]):>20}")

    # 一个期限里的任务（_fit_forecast_models_core 的 task_specs）
    # 内层：每个候选各拟合一次；之后胜出的那个在全训练段重拟合一次。
    # 重拟合按“胜者是两个里较贵的那个”计，是上界；频率类候选的耗时可忽略。
    def pair(classes):
        inner = softmax[n_fit, classes] + gbm[n_fit, classes]
        refit = max(softmax[n_all, classes], gbm[n_all, classes])
        return inner, refit

    direction = pair(3)
    path = tuple(3 * x for x in pair(4))
    binary_tasks = PERSIST_EVENTS + 2 + 1          # PERSIST×7、STOP_TOUCH×2、RETURN_BEFORE_BREAK
    binary = tuple(binary_tasks * x for x in pair(2))
    quant = (quantile[n_fit], quantile[n_all])

    def time_tasks(horizon):
        # TIME_UP×3 屏障、TIME_DOWN×3 屏障：每一步一次 2 类 softmax
        # TIME_RETURN、TIME_BREAK：每一步一次 3 类 softmax
        # 风险集随步数缩小，这里按不缩小计，是上界
        inner = horizon * (6 * softmax[n_fit, 2] + 2 * softmax[n_fit, 3])
        refit = horizon * (6 * softmax[n_all, 2] + 2 * softmax[n_all, 3])
        return inner, refit

    print()
    print("表 2  一个期限里各类任务的耗时（内层选族 + 胜者重拟合的上界）")
    print(f"{'任务':<34}{'内层':>12}{'重拟合上界':>14}")
    for name, (inner, refit) in (("方向（1 个任务）", direction), ("路径（3 个屏障）", path),
                                 (f"二分类（{binary_tasks} 个任务）", binary),
                                 ("分位数（1 个任务）", quant)):
        print(f"{name:<34}{fmt(inner):>12}{fmt(refit):>14}")
    for horizon in MINUTE_HORIZONS:
        inner, refit = time_tasks(horizon)
        print(f"{'时间（8 个任务），期限 ' + str(horizon) + ' 分钟':<34}{fmt(inner):>12}{fmt(refit):>14}")

    cells = len(MINUTE_HORIZONS)
    static_inner = cells * (direction[0] + path[0] + binary[0] + quant[0])
    static_refit = cells * (direction[1] + path[1] + binary[1] + quant[1])
    time_inner = sum(time_tasks(h)[0] for h in MINUTE_HORIZONS)
    time_refit = sum(time_tasks(h)[1] for h in MINUTE_HORIZONS)
    dir_low = cells * direction[0]
    dir_high = dir_low + cells * direction[1]
    rows = (("全部任务（设计现在的写法）", static_inner + time_inner,
             static_inner + static_refit + time_inner + time_refit),
            ("全部任务，但时间类任务一点不算", static_inner, static_inner + static_refit),
            ("只拟合方向任务", dir_low, dir_high))

    print()
    print("表 3  分钟模式五个期限合计（本机，单进程）")
    print(f"{'做法':<30}{'低估':>12}{'高估':>12}{'是只拟合方向的':>16}")
    for name, low, high in rows:
        a, b = sorted((round(low / dir_low), round(high / dir_high)))
        ratio = f"{a} 倍" if a == b else f"{a}–{b} 倍"
        print(f"{name:<30}{fmt(low):>12}{fmt(high):>12}{ratio:>14}")
    print()
    print("低估 = 每个候选在内层各拟合一次，胜者都是不需要迭代的频率类候选；")
    print("高估 = 再加胜者在全训练段重拟合一次。两者的时间类任务都按风险集不随步数缩小计，")
    print("所以第一行偏高；第二行把时间类任务整个去掉，是全部任务耗时的下限。")
    print("样本量取的是设计 §8 的上界，实际样本更少，三行会同比例变小，倍数不变。")
    print("方向任务的模型与其他任务互不依赖（各自选族、各自定温度），"
          "只拟合方向任务得到的方向模型与全部拟合时相同。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
