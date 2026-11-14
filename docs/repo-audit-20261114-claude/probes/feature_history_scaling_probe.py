#!/usr/bin/env python3
"""一个特征快照的耗时，随“这个合约已经有多少根可见的分钟线”怎样增长。

K14 之后，一个决策上下文里最贵的一步是特征快照（上一轮的
context_cost_profile.py：分钟模式约 64%，日线模式约 84%）。那个探针用的合成输入
只有 92 根分钟线。真实的 r2 里，一个合约最多有 8.2 万根可用的分钟线（K42b）。

这个探针把合成输入的历史逐步加长，按时间顺序每隔 5 根取一个决策时刻（与 K45 设计
的取样相同），量生产函数 ``decision/pipeline._features`` 一次调用的耗时。
分钟线的可用时刻取“收盘后 5 秒”：取 60 秒的话，交易节内的快照全部是 MISSING
（见 minute_anchor_availability_probe.py），量到的不是正常路径。

两种调用方式分开量：
  顺序  决策时刻逐个向后推进（回放、K45 都是这样）
  重复  同一个决策时刻再调用一次（同一时刻给多个策略各建一次上下文时是这样）
最后一列是 Wilder 递推这一次重新计算了多少根 K 线的真实波幅（给 ``factors._true_range``
套了一层只计数的包装，量完即还原）。计时受这层包装的影响很小，两种方式受的影响相同。

只读、合成数据。借用 tests/ 下的夹具构造 K 线，不修改任何产品文件。
在仓库根目录运行（我这边约 15 秒，慢的机器上可能要几分钟）：
    python docs/repo-audit-20261114-claude/probes/feature_history_scaling_probe.py
    python docs/repo-audit-20261114-claude/probes/feature_history_scaling_probe.py --days 2 10 40   # 快速

退出码 1 = 顺序调用时，历史长 10 倍以上而一个快照慢 3 倍以上（耗时随历史长度增长）；
退出码 0 = 不随历史长度增长。
耗时与机器有关，看的是倍数。
"""
from __future__ import annotations

import argparse
import random
import statistics
import sys
import time
from pathlib import Path

for parent in (*Path(__file__).resolve().parents, Path.cwd()):
    if (parent / "src" / "ak_trading").is_dir():
        for extra in (parent / "src", parent / "tests"):
            if str(extra) not in sys.path:
                sys.path.insert(0, str(extra))
        break

from ak_trading.v2 import factors  # noqa: E402
from ak_trading.v2.contracts import FrozenInput  # noqa: E402
from ak_trading.v2.decision import pipeline  # noqa: E402
import v2_realistic_fixtures as fx  # noqa: E402

DELAY = 5 * fx.NANO
ANCHORS = 24
K45_MINUTE_ANCHORS = 27_739          # K45 设计 §8：三段合计的分钟决策时刻上界


def build(days: int):
    calendar = fx.exchange_calendar(days)
    trading_days = fx.calendar_trading_days(calendar)
    rng = random.Random(20261114)
    price = 3500.0
    minute, daily, by_day = [], [], {}
    previous_close = None
    for index, day in enumerate(trading_days):
        sessions = sorted((s for s in calendar if s.trading_day == day), key=lambda s: s.start_nano)
        for number, session in enumerate(sessions):
            slots = (session.end_nano - session.start_nano) // fx.MINUTE
            for slot in range(slots):
                price = max(100.0, price + rng.gauss(0.0, 2.0))
                start = session.start_nano + slot * fx.MINUTE
                bar = fx._bar(f"m{index}-{number}-{slot}", start, start + fx.MINUTE, day,
                              round(price, 1), DELAY, session_id=session.session_id, pad=1.0)
                minute.append(bar)
                if number == 0:                       # 夜盘：120 根，够放 24 个连续的取样点
                    by_day.setdefault(index, []).append(bar)
        daily.append(fx.daily_bar(index, round(price, 1), calendar, DELAY,
                                  open_price=previous_close, pad=30.0))
        previous_close = round(price, 1)
    bars = tuple(sorted(daily + minute, key=lambda b: (b.end_nano, b.bar_id)))
    bundle = FrozenInput(Path("SYNTHETIC_HISTORY_SCALING"), fx.SYNTHETIC_HASH,
                         (fx.instrument(),), calendar, bars, (), (fx.trading_rule(),))
    return bundle, by_day, len(minute)


TRUE_RANGE_CALLS = [0]
_original_true_range = factors._true_range


def _counted_true_range(previous, current):
    """只计数，原样转交：用来看 Wilder 递推这一次重算了多少根。"""
    TRUE_RANGE_CALLS[0] += 1
    return _original_true_range(previous, current)


def timed(function):
    started = time.perf_counter()
    result = function()
    return (time.perf_counter() - started) * 1000.0, result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, nargs="+", default=[3, 20, 60, 120, 240])
    args = parser.parse_args()
    total_days = max(args.days)
    bundle, by_day, minute_count = build(total_days)
    sessions = pipeline._cached_product_sessions(bundle.sessions, fx.PRODUCT)
    print(f"合成输入：{total_days} 个交易日，{minute_count:,} 根分钟线，{len(bundle.sessions):,} 个交易节；"
          f"每个历史长度取 {ANCHORS} 个连续的决策时刻")
    print(f"{'可见分钟线':>10} {'顺序 ms（中位数）':>18} {'重复 ms':>10} {'快照可用':>8} "
          f"{'递推重算的根数：顺序 / 重复':>30}")
    factors._true_range = _counted_true_range
    rows = []
    for days in sorted(args.days):
        session_bars = by_day[days - 1]              # 该日夜盘里的分钟线
        samples, ready, visible_count = [], 0, 0
        decision = None
        walked_chronological = 0
        for position in range(4, 4 + 5 * ANCHORS, 5):
            bar = session_bars[position]
            decision = max(bar.end_nano, bar.available_at_nano)
            TRUE_RANGE_CALLS[0] = 0
            elapsed, (snapshot, visible, _) = timed(
                lambda: pipeline._features(bundle, fx.CONTRACT, decision, 60, None, None, sessions))
            samples.append(elapsed)
            walked_chronological = TRUE_RANGE_CALLS[0]       # 留下最后一个顺序时刻的计数
            visible_count = len(visible)
            ready += snapshot.quality.status == "READY" and snapshot.atr_points is not None
        TRUE_RANGE_CALLS[0] = 0
        pipeline._features(bundle, fx.CONTRACT, decision + 0, 60, None, None, sessions)
        walked_repeat_first = TRUE_RANGE_CALLS[0]
        TRUE_RANGE_CALLS[0] = 0
        repeat, _ = timed(
            lambda: pipeline._features(bundle, fx.CONTRACT, decision, 60, None, None, sessions))
        walked_repeat = max(walked_repeat_first, TRUE_RANGE_CALLS[0])
        chronological = statistics.median(samples[1:])   # 第一次要补走这段历史，不计
        rows.append((visible_count, chronological, repeat))
        print(f"{visible_count:>10,} {chronological:>18.2f} {repeat:>10.2f} {ready:>5}/{ANCHORS} "
              f"{walked_chronological:>18,} / {walked_repeat:,}")

    factors._true_range = _original_true_range
    first, last = rows[0], rows[-1]
    length = last[0] / first[0]
    growth = last[1] / first[1]
    print(f"\n顺序调用：可见历史长 {length:.0f} 倍，一个快照慢 {growth:.1f} 倍；"
          f"同一时刻重复调用是顺序调用的 {last[2] / last[1]:.1f} 倍")
    per_bar = (last[1] - first[1]) / (last[0] - first[0])
    base = first[1] - per_bar * first[0]
    print(f"按直线外推：每个快照约 {base:.1f} ms + 每 1 万根可见历史 {per_bar * 10_000:.1f} ms")
    print(f"K45 的分钟特征阶段（{K45_MINUTE_ANCHORS:,} 个决策时刻，本机，单进程）：")
    for history in (20_000, 40_000, 60_000):
        each = base + per_bar * history
        minutes = each * K45_MINUTE_ANCHORS / 1000.0 / 60.0
        print(f"  平均可见 {history:>6,} 根：每个快照 {each:6.1f} ms，合计约 {minutes:5.0f} 分钟")
    print("说明：只是分钟快照这一步；日线快照、行情状态、请求、标签另计。")
    return 1 if length >= 10 and growth > 3 else 0


if __name__ == "__main__":
    raise SystemExit(main())
