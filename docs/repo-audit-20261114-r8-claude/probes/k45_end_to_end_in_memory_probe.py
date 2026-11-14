#!/usr/bin/env python3
"""把 K45 的整条链在内存里从头到尾跑一遍（合成数据），看各步要多久、占多少内存。

第四批的 K45 没有跑到任何一步真实计算就停了；各步的耗时一直是分开估的。
这个探针用合成数据、调用生产函数，把整条链接起来真的跑一遍：

  输入 → 每个决策时刻的特征 → 行情参照（每个训练日用此前全部快照重算一次，元组入口）
       → 行情状态 → 请求（5 个分钟期限 × 3 个距离）→ 标签
       → 只拟合方向任务（生产函数 fit_forecast_models，元组入口）
       → 对“检验段”的请求逐个预测 → 与只用训练段各类占比的对照比较 Brier 分数

所有对象直接留在内存里，不放进紧凑库，不写盘。合成价格是随机游走，所以模型不该比
对照好——这里看的不是预测效果，是链路通不通、每步多久、内存多大。

与 K45 的差别（都是为了能在一台小机器上几分钟到十几分钟内跑完）：
  - 只有一个合成合约，历史最长约 9 万根分钟线（真实的 r1 里一个合约最多 8.3 万根）；
  - 默认每节每第 15 根取一个决策时刻（K45 是每第 5 根），可用 --step 改；
  - 分段只按交易日切开，训练段和校准段里窗口跨过段末的请求按“标签未成熟”剔除；
    没有调用生产的 assign_splits（清除带和隔离带由 K45 的工具按设计去做）；
  - “只拟合方向”沿用上一轮探针的办法（给 _run_task_selection 套一层包装，量完即还原），
    这只是测量手段，不是 K45 该用的实现方式（设计 §10.3 已禁止在工具里这样做）。

只读、合成数据，不修改任何产品文件。需要同目录下的 k45_inmemory_stage_cost_probe.py。
在仓库根目录运行（我这边约 8 分钟、峰值内存约 2.8 GB；慢的机器上可能要一个多小时）：
    python docs/repo-audit-20261114-r8-claude/probes/k45_end_to_end_in_memory_probe.py
    python docs/repo-audit-20261114-r8-claude/probes/k45_end_to_end_in_memory_probe.py --days 236 --step 60   # 较快：我这边约 70 秒

退出码 0 = 整条链跑通；退出码 1 = 某一步没有跑通（输出里写明是哪一步）。
耗时与机器有关。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
for parent in (*HERE.parents, Path.cwd()):
    if (parent / "src" / "ak_trading").is_dir():
        for extra in (parent / "src", parent / "tests", HERE):
            if str(extra) not in sys.path:
                sys.path.insert(0, str(extra))
        break

import k45_inmemory_stage_cost_probe as stage  # noqa: E402
from ak_trading.v2.clock import ClockError  # noqa: E402
from ak_trading.v2.decision import pipeline  # noqa: E402
from ak_trading.v2.factor_specs import factor_registry_v2  # noqa: E402
from ak_trading.v2.macro import MacroCache  # noqa: E402
from ak_trading.v2.probability import models  # noqa: E402
from ak_trading.v2.probability.forecast import build_forecast_requests  # noqa: E402
from ak_trading.v2.probability.labels import label_forecast  # noqa: E402
from ak_trading.v2.probability.regimes import fit_regime_reference  # noqa: E402
from ak_trading.v2.validation.experiments import _WindowIndex  # noqa: E402
import v2_realistic_fixtures as fx  # noqa: E402

GAP = 61 * fx.NANO                      # 设计 §2：段末取最后一节收盘后 61 秒


class Phases:
    def __init__(self) -> None:
        self.seconds: dict[str, float] = {}
        self.mark = time.perf_counter()

    def add(self, name: str) -> None:
        now = time.perf_counter()
        self.seconds[name] = self.seconds.get(name, 0.0) + now - self.mark
        self.mark = now


def brier(probabilities: dict[str, float], outcome: str) -> float:
    return sum((value - (1.0 if key == outcome else 0.0)) ** 2 for key, value in probabilities.items())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=260, help="合成交易日总数")
    parser.add_argument("--step", type=int, default=15, help="每节每第几根取一个决策时刻")
    parser.add_argument("--train-days", type=int, default=200)
    parser.add_argument("--cal-days", type=int, default=30)
    args = parser.parse_args()
    if args.days < args.train_days + args.cal_days + 5:
        parser.error("交易日总数要比训练段加校准段至少多 5 天")

    phases = Phases()
    bundle, _, minute_count = stage.build(args.days)
    sessions = pipeline._cached_product_sessions(bundle.sessions, fx.PRODUCT)
    window_index = _WindowIndex(bundle)
    macro_cache = MacroCache(())
    trading_days = fx.calendar_trading_days(bundle.sessions)
    day_index = {day: index for index, day in enumerate(trading_days)}
    last_end = {}
    for session in bundle.sessions:
        last_end[session.trading_day] = max(last_end.get(session.trading_day, 0), session.end_nano)
    train_end = last_end[trading_days[args.train_days - 1]] + GAP
    cal_end = last_end[trading_days[args.train_days + args.cal_days - 1]] + GAP
    test_last = len(trading_days) - 2                    # 最后一天只给标签留出未来窗口
    as_of = last_end[trading_days[test_last]] + GAP
    session_end = {session.session_id: session.end_nano for session in bundle.sessions}
    by_session: dict[str, list] = {}
    for bar in bundle.bars:
        if bar.duration_seconds == 60:
            by_session.setdefault(bar.session_id, []).append(bar)
    anchors = []
    for bars in by_session.values():
        bars.sort(key=lambda item: item.start_nano)
        for position in range(args.step - 1, len(bars), args.step):
            bar = bars[position]
            decision = max(bar.end_nano, bar.available_at_nano)
            if decision <= session_end[bar.session_id] and day_index[bar.trading_day] <= test_last:
                anchors.append((decision, bar.trading_day))
    anchors.sort()
    feature_keys = tuple(spec.factor_key for spec in factor_registry_v2(60))
    phases.add("输入和日历")

    segments = {name: {"features": [], "regimes": [], "requests": [], "labels": [], "days": set()}
                for name in ("TRAIN", "CAL", "TEST")}
    test_regime = {}
    earlier: list = []                # 此前各日的训练段快照
    today: list = []
    current_day = None
    reference = formal = None
    previous = None
    resident = 0
    counts = {"决策时刻": 0, "没有参考价或 ATR": 0, "请求": 0, "标签成熟": 0, "标签未成熟或缺失": 0}
    for decision, day in anchors:
        segment = "TRAIN" if decision < train_end else "CAL" if decision < cal_end else "TEST"
        if day != current_day:
            earlier.extend(today)
            today, current_day = [], day
            if segment == "TRAIN":
                cutoff = max((item.decision_nano for item in earlier), default=decision - 1)
                reference = fit_regime_reference(tuple(earlier), cutoff, cutoff)
            else:
                if formal is None:
                    formal = fit_regime_reference(tuple(earlier), train_end, train_end)
                reference = formal
            phases.add("行情参照（每日一次）")
        counts["决策时刻"] += 1
        snapshot, _, _ = pipeline._features(bundle, fx.CONTRACT, decision, 60, None, None, sessions)
        resident += sys.getsizeof(snapshot.source_bar_ids)
        phases.add("特征")
        if snapshot.reference_price is None or snapshot.atr_points is None or snapshot.atr_points <= 0:
            counts["没有参考价或 ATR"] += 1
            continue
        if segment == "TRAIN":
            today.append(snapshot)
        regime = stage.regime_for(bundle, snapshot, macro_cache, reference, previous)
        previous = regime
        requests = []
        for horizon in stage.HORIZONS:
            try:
                requests.extend(build_forecast_requests(
                    snapshot, (horizon,), regime.frozen_range, bundle.sessions))
            except ClockError:
                pass
        phases.add("行情状态和请求")
        cutoff = {"TRAIN": train_end, "CAL": cal_end, "TEST": as_of}[segment]
        bucket = segments[segment]
        kept = 0
        for request in requests:
            counts["请求"] += 1
            label = label_forecast(request, window_index.window(request), bundle.sessions, (), cutoff)
            if label.status == "MATURE":
                counts["标签成熟"] += 1
                bucket["requests"].append(request)
                bucket["labels"].append(label)
                kept += 1
            else:
                counts["标签未成熟或缺失"] += 1
        if kept:
            bucket["features"].append(snapshot)
            bucket["regimes"].append(regime)
            bucket["days"].add(day)
            if segment == "TEST":
                test_regime[snapshot.snapshot_id] = regime
        phases.add("标签")
    if formal is None:
        print("没有走到校准段：交易日太少")
        return 1

    print(f"合成输入：{args.days} 个交易日，{minute_count:,} 根分钟线；每节每第 {args.step} 根取一个决策时刻；"
          f"分钟线收盘后 5 秒可用")
    print(f"决策时刻 {counts['决策时刻']:,} 个（没有参考价或 ATR 的 {counts['没有参考价或 ATR']:,} 个）；"
          f"请求 {counts['请求']:,} 个，标签成熟 {counts['标签成熟']:,} 个、"
          f"未成熟或缺失 {counts['标签未成熟或缺失']:,} 个")
    for name, bucket in segments.items():
        print(f"  {name:<5} {len(bucket['days']):>4} 个交易日，{len(bucket['features']):>6,} 个快照，"
              f"{len(bucket['requests']):>7,} 个带成熟标签的请求")

    train, cal, test = segments["TRAIN"], segments["CAL"], segments["TEST"]
    config = models.default_fit_config(feature_keys, train_end, cal_end, as_of + 1)
    original = models._run_task_selection
    originals = (models._calibration_predictions, models.build_calibration_evidence)

    def only_direction(task, *arguments, **keywords):
        if task != models.TASK_DIRECTION:
            return None
        return original(task, *arguments, **keywords)

    models._run_task_selection = only_direction
    models._calibration_predictions = lambda *arguments, **keywords: []
    models.build_calibration_evidence = lambda *arguments, **keywords: []
    try:
        model_set = models.fit_forecast_models(
            tuple(train["features"]), tuple(train["regimes"]), tuple(train["requests"]),
            tuple(train["labels"]), tuple(cal["features"]), tuple(cal["regimes"]),
            tuple(cal["requests"]), tuple(cal["labels"]), config, formal, seasonality=None)
    except Exception as error:                      # noqa: BLE001 记录是哪一步、什么原因
        print(f"拟合没有跑通：{type(error).__name__}: {error}")
        return 1
    finally:
        models._run_task_selection = original
        models._calibration_predictions, models.build_calibration_evidence = originals
    phases.add("拟合（只方向，5 个期限）")
    selected = sorted(key for key, value in model_set.models.items()
                      if str(value.get("fit_status", "")).startswith("SELECTED"))
    print(f"\n拟合得到 {len(model_set.models)} 个方向模型，其中选中 {len(selected)} 个：")
    for key in selected:
        print(f"  {key}，温度 {model_set.models[key]['temperature']}")

    label_of = {label.request_id: label for label in test["labels"]}
    train_direction: dict[int, dict[str, int]] = {}
    for request, label in zip(train["requests"], train["labels"]):
        if request.barrier_key == "ATR_1" and label.direction is not None:
            tally = train_direction.setdefault(request.horizon.value, {})
            tally[label.direction] = tally.get(label.direction, 0) + 1
    scores: dict[int, list[float]] = {}
    missing = 0
    for request in test["requests"]:
        if request.barrier_key != "ATR_1":
            continue
        label = label_of[request.request_id]
        regime = test_regime[request.features.snapshot_id]
        output = models.apply_model_set(model_set.models, feature_keys,
                                        int(config["minimum_leaf_blocks"]), request, regime.base)
        direction = output["direction"]
        tally = train_direction.get(request.horizon.value)
        if direction is None or label.direction is None or not tally:
            missing += 1
            continue
        total = sum(tally.values())
        baseline = {key: tally.get(key, 0) / total for key in direction}
        row = scores.setdefault(request.horizon.value, [0.0, 0.0, 0])
        row[0] += brier(direction, label.direction)
        row[1] += brier(baseline, label.direction)
        row[2] += 1
    phases.add("预测和评分")
    print(f"\n检验段方向预测（合成的随机游走，不该有信息量；这里只看链路）：没有方向输出的 {missing:,} 个")
    print(f"{'期限（分钟）':>10} {'请求数':>8} {'模型 Brier':>11} {'对照 Brier':>11} {'技能':>8}")
    for horizon in sorted(scores):
        model_sum, base_sum, count = scores[horizon]
        skill = 1.0 - model_sum / base_sum if base_sum else float("nan")
        print(f"{horizon:>12} {count:>10,} {model_sum / count:>12.4f} {base_sum / count:>12.4f} {skill:>+9.4f}")

    total = sum(phases.seconds.values())
    print("\n各步耗时（本机）：")
    for name, seconds in phases.seconds.items():
        print(f"  {name:<16} {seconds:>8.1f} 秒  {seconds / total:>6.1%}")
    print(f"  {'合计':<16} {total:>8.1f} 秒")
    print(f"\n进程峰值内存{stage.memory_text(1024.0, 'GB')}；其中各快照的来源清单合计 {resident / 1e9:.2f} GB"
          f"（每个快照一份“到当时为止全部可见分钟线”的清单）")
    if not scores:
        print("检验段没有得到任何方向预测")
        return 1
    print("整条链在内存里跑通：特征、行情参照、行情状态、请求、标签、方向拟合、预测、评分")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
