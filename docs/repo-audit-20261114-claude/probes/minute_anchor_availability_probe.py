#!/usr/bin/env python3
"""分钟线“收盘后多少秒可用”，决定交易节内能不能做出一个可用的特征快照。

生产规则（``factors.py`` 文件头，变更申请-005 §3）：计算锚点是“完成时刻已经过了
决策时刻的最后一个计划内完整分钟格”；这一格的 K 线在决策时刻还不可见，
就是 ``MISSING / expected_slot_missing``，不退回更早的一根。

r2 的分钟线可用时刻一律是“收盘后 60 秒”（K42b：1,783,377 根 READY 行全部如此）。
连续交易时，一根 K 线变得可用的那一刻，下一格正好走完而它的 K 线还要再等 60 秒，
所以交易节内的任何时刻，锚点那一格都不可见。只有一节的最后一根之后（休市里）例外。

这个探针在合成输入上（交易节形状与螺纹钢相同：夜盘 120 分钟，日盘 75、60、90 分钟）
对几种可用延迟各量一次，调用的是生产函数 ``decision/pipeline._features``：

  甲  K45 设计 §3 的取样：每节第 5、10、15……根，决策时刻 = max(收盘, 可用)，
      只保留不晚于本节结束的
  乙  每节最后一根变得可用的时刻（落在休市里）
  丙  日线模式：当天日线变得可用的时刻
  丁  只对延迟 60 秒：在一节中间连续 10 分钟里，每隔 1 秒取一个决策时刻（共 600 个），
      看有没有任何一个时刻可用
  戊  甲组里可用的快照，各生成一个 5 分钟期限的预测请求并打标签：
      请求窗口的起点在收盘后第几秒；有多少个得到了方向标签

只读、合成数据，不修改任何产品文件。在仓库根目录运行：
    python docs/repo-audit-20261114-claude/probes/minute_anchor_availability_probe.py

退出码 1 = 延迟 60 秒时，甲组没有一个快照可用（问题存在）；
退出码 0 = 延迟 60 秒时甲组有可用的快照。
"""
from __future__ import annotations

import random
import sys
from collections import Counter
from pathlib import Path

for parent in (*Path(__file__).resolve().parents, Path.cwd()):
    if (parent / "src" / "ak_trading").is_dir():
        for extra in (parent / "src", parent / "tests"):
            if str(extra) not in sys.path:
                sys.path.insert(0, str(extra))
        break

from ak_trading.v2.contracts import FrozenInput, Horizon  # noqa: E402
from ak_trading.v2.decision import pipeline  # noqa: E402
from ak_trading.v2.probability.forecast import build_forecast_requests  # noqa: E402
from ak_trading.v2.probability.labels import label_forecast  # noqa: E402
from ak_trading.v2.probability.regimes import freeze_range  # noqa: E402
import v2_realistic_fixtures as fx  # noqa: E402

DAYS = 12
TEST_DAY = 10
DAILY_DAYS = 70                # 日线基线里最长的窗口是 60 根
DAILY_TEST_DAY = 65
DELAYS = (0, 1, 30, 59, 60, 61)


def build(delay_seconds: int):
    delay = delay_seconds * fx.NANO
    calendar = fx.exchange_calendar(DAYS)
    trading_days = fx.calendar_trading_days(calendar)
    rng = random.Random(20261114)
    price = 3500.0
    minute, daily, by_session = [], [], {}
    previous_close = None
    for index, day in enumerate(trading_days):
        sessions = sorted((s for s in calendar if s.trading_day == day), key=lambda s: s.start_nano)
        for number, session in enumerate(sessions):
            slots = (session.end_nano - session.start_nano) // fx.MINUTE
            for slot in range(slots):
                price = max(100.0, price + rng.gauss(0.0, 2.0))
                start = session.start_nano + slot * fx.MINUTE
                bar = fx._bar(f"m{index}-{number}-{slot}", start, start + fx.MINUTE, day,
                              round(price, 1), delay, session_id=session.session_id, pad=1.0)
                minute.append(bar)
                by_session.setdefault((index, number), (session, []))[1].append(bar)
        daily.append(fx.daily_bar(index, round(price, 1), calendar, delay,
                                  open_price=previous_close, pad=30.0))
        previous_close = round(price, 1)
    bars = tuple(sorted(daily + minute, key=lambda b: (b.end_nano, b.bar_id)))
    bundle = FrozenInput(Path("SYNTHETIC_ANCHOR_AVAILABILITY"), fx.SYNTHETIC_HASH,
                         (fx.instrument(),), calendar, bars, (), (fx.trading_rule(),))
    return bundle, by_session


def build_daily(delay_seconds: int):
    delay = delay_seconds * fx.NANO
    calendar = fx.exchange_calendar(DAILY_DAYS)
    rng = random.Random(20261114)
    price, previous_close, daily = 3500.0, None, []
    for index in range(DAILY_DAYS):
        price = max(100.0, price + rng.gauss(0.0, 30.0))
        daily.append(fx.daily_bar(index, round(price, 1), calendar, delay,
                                  open_price=previous_close, pad=30.0))
        previous_close = round(price, 1)
    bundle = FrozenInput(Path("SYNTHETIC_ANCHOR_AVAILABILITY_DAILY"), fx.SYNTHETIC_HASH,
                         (fx.instrument(),), calendar, tuple(daily), (), (fx.trading_rule(),))
    return bundle, daily[DAILY_TEST_DAY]


def classify(bundle, sessions, decisions, duration=60, canonical_daily=False):
    ready, reasons = 0, Counter()
    for decision in decisions:
        snapshot, _, _ = pipeline._features(bundle, fx.CONTRACT, decision, duration, None, None,
                                            sessions, canonical_daily=canonical_daily)
        if (snapshot.quality.status == "READY" and snapshot.reference_price is not None
                and snapshot.atr_points is not None):
            ready += 1
        else:
            reasons[snapshot.quality.reasons[0] if snapshot.quality.reasons else snapshot.quality.status] += 1
    return ready, reasons


def request_and_label(bundle, sessions, anchors):
    """戊：(窗口起点相对收盘的秒数集合, 得到方向标签的个数, 请求个数)。"""
    minute = sorted((b for b in bundle.bars if b.duration_seconds == 60), key=lambda b: b.end_nano)
    as_of = max(b.available_at_nano for b in bundle.bars) + 1
    offsets, labelled, requested = set(), 0, 0
    for bar_end, decision in anchors:
        snapshot, _, _ = pipeline._features(bundle, fx.CONTRACT, decision, 60, None, None, sessions)
        if snapshot.quality.status != "READY" or snapshot.atr_points is None:
            continue
        requests = build_forecast_requests(snapshot, (Horizon("TRADING_MINUTE", 5),),
                                           freeze_range(snapshot), sessions)
        request = next(r for r in requests if r.barrier_key == "ATR_1")
        requested += 1
        offsets.add((request.window_start_nano - bar_end) // fx.NANO)
        window = tuple(b for b in minute if b.start_nano >= request.window_start_nano
                       and b.end_nano <= request.window_end_nano)
        labelled += label_forecast(request, window, sessions, (), as_of).direction is not None
    return offsets, labelled, requested


def main() -> int:
    print(f"合成输入：{DAYS} 个交易日，每天 4 节共 345 根分钟线；在第 {TEST_DAY + 1} 个交易日上取决策时刻。"
          f"日线另用 {DAILY_DAYS} 个交易日，取第 {DAILY_TEST_DAY + 1} 天。")
    print(f"{'可用延迟':>8} | {'甲：节内每第 5 根':^34} | {'乙：每节最后一根之后':^22} | {'丙：日线':^10} | "
          f"{'戊：5 分钟请求与标签':^26}")
    print(f"{'（秒）':>8} | {'保留':>6} {'可用':>6}  {'不可用的原因':<16} | {'时刻':>6} {'可用':>6}{'':10} | {'可用':>8} | "
          f"{'窗口起点':>10} {'有标签':>10}")
    blocked = False
    for delay_seconds in DELAYS:
        bundle, by_session = build(delay_seconds)
        sessions = pipeline._cached_product_sessions(bundle.sessions, fx.PRODUCT)
        inside, after, anchors = [], [], []
        for (index, _), (session, bars) in sorted(by_session.items()):
            if index != TEST_DAY:
                continue
            for position in range(4, len(bars), 5):                 # 第 5、10、15……根
                decision = max(bars[position].end_nano, bars[position].available_at_nano)
                if decision <= session.end_nano:
                    inside.append(decision)
                    anchors.append((bars[position].end_nano, decision))
            last = bars[-1]
            after.append(max(last.end_nano, last.available_at_nano))
        ready_inside, reasons = classify(bundle, sessions, inside)
        ready_after, _ = classify(bundle, sessions, after)
        daily_bundle, day_bar = build_daily(delay_seconds)
        daily_sessions = pipeline._cached_product_sessions(daily_bundle.sessions, fx.PRODUCT)
        ready_daily, _ = classify(daily_bundle, daily_sessions,
                                  [max(day_bar.end_nano, day_bar.available_at_nano)],
                                  duration=86400, canonical_daily=True)
        reason = "、".join(f"{name}×{count}" for name, count in reasons.most_common(2)) or "—"
        offsets, labelled, requested = request_and_label(bundle, sessions, anchors)
        start = "、".join(f"+{value} 秒" for value in sorted(offsets)) or "—"
        print(f"{delay_seconds:>8} | {len(inside):>6} {ready_inside:>6}  {reason:<22} | "
              f"{len(after):>6} {ready_after:>6}{'':10} | {ready_daily:>6}/1 | "
              f"{start:>10} {labelled:>6}/{requested}")
        if delay_seconds == 60:
            session, bars = by_session[(TEST_DAY, 0)]          # 夜盘，取中间一段
            start = bars[40].end_nano
            sweep = [start + second * fx.NANO for second in range(600)]
            ready_sweep, sweep_reasons = classify(bundle, sessions, sweep)
            sweep_line = (f"丁  延迟 60 秒，一节中间连续 600 个逐秒时刻：可用 {ready_sweep} 个；"
                          + "、".join(f"{name}×{count}" for name, count in sweep_reasons.most_common(2)))
            if ready_inside == 0 and ready_sweep == 0:
                blocked = True
    print()
    print(sweep_line)
    if blocked:
        print("延迟 60 秒（r2 的做法）时，交易节内的决策时刻没有一个能得到可用的特征快照；")
        print("只有每节最后一根之后的那一刻可用，一天 4 个。日线模式不受影响。")
        return 1
    print("延迟 60 秒时，交易节内有可用的特征快照。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
