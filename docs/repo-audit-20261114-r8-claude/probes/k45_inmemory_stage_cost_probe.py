#!/usr/bin/env python3
"""K45 的每个决策时刻：各步在内存里做要多久；把同样的记录展开写出再读回，又要多久。

第四批的 K45 停在“规划超过 72 小时”。超出的不是特征、标签或拟合，而是阶段之间
“把每条记录完整写出、再完整读回核对”这一步（设计 §10.11 第 3 项、§10.12）。
原因在生产代码里：每个特征快照的 ``source_bar_ids`` 是这个合约到决策时刻为止
全部可见的分钟线（VOL004 的 Wilder 递推要用整段历史），每个决策时刻有 1 个快照和
15 个请求，每个请求里又嵌着一份快照。于是每个决策时刻要展开 16 份“全部历史的清单”。

这个探针用合成数据、按真实的规模（一个合约最多约 8.2 万根分钟线，线的编号与生产
同为 ``bar:`` 加 64 位十六进制）把两种做法各量一遍，都调用生产函数：

  内存里做（M5 的 ``_fit_fold`` 就是这个顺序）
    特征      decision/pipeline._features
    行情状态  probability/regimes.assess_regime
    请求      probability/forecast.build_forecast_requests（5 个分钟期限 × 3 个距离）
    入库      _fold_records.FoldRecords.append_snapshot / append_request（内存，来源链共享）
    时间区间  validation/experiments._WindowIndex.interval
    标签      probability/labels.label_forecast

  展开往返（第四批规划里的那一步）
    写出      _fold_records._record_line_bytes（页文件用的规范编码）+ SHA-256
    读回      schemas.parse_json_bytes + decode_record，再放进另一个内存库

另外核对一件事：同样的输入在另一个进程里（换一个 PYTHONHASHSEED）重算，快照和请求的
编号是否逐个相同。编号是内容的 SHA-256（来源清单在内），所以“编号序列相同”就等于
“内容相同”——阶段之间可以只封存编号序列，后一阶段重算再核对。

只读、合成数据。借用 tests/ 下的夹具构造 K 线，不修改任何产品文件。
在仓库根目录运行（我这边约 2 分钟，慢的机器上可能要二十分钟）：
    python docs/repo-audit-20261114-r8-claude/probes/k45_inmemory_stage_cost_probe.py
    python docs/repo-audit-20261114-r8-claude/probes/k45_inmemory_stage_cost_probe.py --days 3 20 60   # 快速

退出码 1 = 在最长的历史上，展开往返比内存里全部步骤加起来贵 3 倍以上（问题存在）；
退出码 0 = 没有贵到 3 倍；退出码 2 = 另一个进程重算出的编号不同（不能用“封存编号”的办法）。
耗时与机器有关，看的是倍数和各步的比例。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import random
import statistics
import subprocess
import sys
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

for parent in (*Path(__file__).resolve().parents, Path.cwd()):
    if (parent / "src" / "ak_trading").is_dir():
        for extra in (parent / "src", parent / "tests"):
            if str(extra) not in sys.path:
                sys.path.insert(0, str(extra))
        break

from ak_trading.v2._fold_records import FoldRecords, RecordView, _record_line_bytes  # noqa: E402
from ak_trading.v2.artifacts import stable_id  # noqa: E402
from ak_trading.v2.clock import ClockError  # noqa: E402
from ak_trading.v2.contracts import FeatureSnapshot, ForecastRequest, FrozenInput, Horizon  # noqa: E402
from ak_trading.v2.data import visible_rule  # noqa: E402
from ak_trading.v2.decision import pipeline  # noqa: E402
from ak_trading.v2.decision.execution import fee_amount  # noqa: E402
from ak_trading.v2.macro import MacroCache  # noqa: E402
from ak_trading.v2.probability.forecast import build_forecast_requests  # noqa: E402
from ak_trading.v2.probability.labels import label_forecast  # noqa: E402
from ak_trading.v2.probability.regimes import (  # noqa: E402
    _fit_regime_reference_from_view, assess_regime, fit_regime_reference, freeze_range)
from ak_trading.v2.schemas import decode_record, parse_json_bytes  # noqa: E402
from ak_trading.v2.validation.experiments import _WindowIndex  # noqa: E402
import v2_realistic_fixtures as fx  # noqa: E402

DELAY = 5 * fx.NANO                      # J33 / 设计 §10.2：收盘后 5 秒可用
ANCHORS = 24                             # 每个历史长度取 24 个连续的决策时刻
ROUNDTRIP_ANCHORS = 4                    # 其中前 4 个另做一次展开往返
HORIZONS = tuple(Horizon("TRADING_MINUTE", value) for value in (1, 5, 15, 30, 60))
STEPS = ("特征", "行情状态", "请求", "入库", "时间区间", "标签")

# 第四批状态页和设计 §10.12 公布的数字
K45_ANCHORS = {"TRAIN": 17_909, "CAL": 3_919, "TEST": 5_911}
K45_FORMAL_LOGICAL_BYTES = 1_497_853_164_240
K45_REHEARSAL_LOGICAL_BYTES = 841_867_930_800
K45_TRAIN_DAYS = 278


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
            for slot in range((session.end_nano - session.start_nano) // fx.MINUTE):
                price = max(100.0, price + rng.gauss(0.0, 2.0))
                start = session.start_nano + slot * fx.MINUTE
                bar = fx._bar(stable_id("bar", f"{fx.CONTRACT}|60|{start}"), start, start + fx.MINUTE,
                              day, round(price, 1), DELAY, session_id=session.session_id, pad=1.0)
                minute.append(bar)
                if number == 0:                       # 夜盘：120 根，够放 24 个连续的取样点
                    by_day.setdefault(index, []).append(bar)
        daily.append(fx.daily_bar(index, round(price, 1), calendar, DELAY,
                                  open_price=previous_close, pad=30.0))
        previous_close = round(price, 1)
    bars = tuple(sorted(daily + minute, key=lambda b: (b.end_nano, b.bar_id)))
    bundle = FrozenInput(Path("SYNTHETIC_K45_STAGE_COST"), fx.SYNTHETIC_HASH,
                         (fx.instrument(),), calendar, bars, (), (fx.trading_rule(),))
    return bundle, by_day, len(minute)


def regime_for(bundle, snapshot, macro_cache, reference, previous):
    """与 validation/experiments._fit_fold 相同的调用方式。"""
    macro = macro_cache.snapshot(snapshot.decision_nano, snapshot.product, False, True)
    rule = visible_rule(bundle, snapshot.contract_id, snapshot.decision_nano)
    known = rule is not None and all(
        leg is not None for leg in (rule.open_fee, rule.close_fee, rule.close_today_fee))
    if not known:
        missing = replace(snapshot, quality=replace(
            snapshot.quality, status="MISSING",
            reasons=tuple(sorted({*snapshot.quality.reasons, "COST_UNKNOWN"}))))
        return assess_regime(missing, macro, None, freeze_range(snapshot), Decimal(0),
                             reference, previous)
    cost = (fee_amount(rule.open_fee, snapshot.reference_price, 1, rule.multiplier)
            + max(fee_amount(leg, snapshot.reference_price, 1, rule.multiplier)
                  for leg in (rule.close_fee, rule.close_today_fee))) / rule.multiplier
    cost += 2 * rule.price_tick * (rule.slippage_ticks + rule.impact_ticks)
    return assess_regime(snapshot, macro, None, freeze_range(snapshot), cost, reference, previous)


def peak_memory_mb() -> float | None:
    """进程到目前为止的内存峰值（MB）。Linux/macOS 用 resource，Windows 用 psapi；量不到返回 None。"""
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / 1048576.0 if sys.platform == "darwin" else peak / 1024.0
    except ImportError:
        pass
    try:
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                    "PagefileUsage", "PeakPagefileUsage")]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return counters.PeakWorkingSetSize / 1048576.0
    except Exception:                               # noqa: BLE001 量不到就不报这个数
        pass
    return None


def memory_text(scale: float = 1.0, unit: str = "MB") -> str:
    peak = peak_memory_mb()
    return "没有量到" if peak is None else f"约 {peak / scale:,.1f} {unit}"


def new_store() -> FoldRecords:
    return FoldRecords(cache_max_records=64, cache_max_bytes=64 * 1024 * 1024)


class Clock:
    def __init__(self) -> None:
        self.mark = time.perf_counter()

    def lap(self) -> float:
        now = time.perf_counter()
        elapsed, self.mark = (now - self.mark) * 1000.0, now
        return elapsed


def run(days_list, with_roundtrip: bool, total_days: int):
    bundle, by_day, minute_count = build(total_days)
    sessions = pipeline._cached_product_sessions(bundle.sessions, fx.PRODUCT)
    window_index = _WindowIndex(bundle)
    macro_cache = MacroCache(())
    as_of = max(bar.available_at_nano for bar in bundle.bars) + 1
    store, mirror = new_store(), new_store()
    uncached = FoldRecords(cache_max_records=1, cache_max_bytes=1)   # 人数远多于缓存时的情形
    rows, ids_by_group = [], {}
    previous = None
    mature = total_labels = 0
    for days in sorted(days_list):
        session_bars = by_day[days - 1]
        first = session_bars[4]
        cutoff = max(first.end_nano, first.available_at_nano) - 1
        reference = _fit_regime_reference_from_view(
            RecordView(store, "FeatureSnapshot", tuple(range(store.logical_snapshot_count))),
            cutoff, cutoff)
        samples = {step: [] for step in STEPS}
        expanded_bytes, write_ms, read_ms = [], [], []
        visible_count = 0
        group_ids: list[str] = []
        group_snapshots = []
        for number, position in enumerate(range(4, 4 + 5 * ANCHORS, 5)):
            bar = session_bars[position]
            decision = max(bar.end_nano, bar.available_at_nano)
            clock = Clock()
            snapshot, visible, _ = pipeline._features(
                bundle, fx.CONTRACT, decision, 60, None, None, sessions)
            samples["特征"].append(clock.lap())
            regime = regime_for(bundle, snapshot, macro_cache, reference, previous)
            previous = regime
            samples["行情状态"].append(clock.lap())
            requests = []
            for horizon in HORIZONS:
                try:
                    requests.extend(build_forecast_requests(
                        snapshot, (horizon,), regime.frozen_range, bundle.sessions))
                except ClockError:
                    pass
            samples["请求"].append(clock.lap())
            store.append_snapshot(snapshot)
            for request in requests:
                store.append_request(request)
            samples["入库"].append(clock.lap())
            uncached.append_snapshot(snapshot)
            group_snapshots.append(snapshot)
            clock.lap()
            for request in requests:
                window_index.interval(request, request.window_end_nano)
            samples["时间区间"].append(clock.lap())
            for request in requests:
                label = label_forecast(request, window_index.window(request), bundle.sessions, (), as_of)
                total_labels += 1
                mature += label.status == "MATURE"
            samples["标签"].append(clock.lap())
            visible_count = len(visible)
            group_ids.append(snapshot.snapshot_id)
            group_ids.extend(request.request_id for request in requests)
            if with_roundtrip and number < ROUNDTRIP_ANCHORS:
                clock = Clock()
                digest = hashlib.sha256()
                lines = []
                for record in (snapshot, *requests):
                    line = _record_line_bytes(record)
                    digest.update(line)
                    lines.append(line)
                write_ms.append(clock.lap())
                expanded_bytes.append(sum(len(line) for line in lines))
                restored = decode_record(FeatureSnapshot, parse_json_bytes(lines[0]))
                mirror.append_snapshot(restored)
                for line in lines[1:]:
                    mirror.append_request(decode_record(ForecastRequest, parse_json_bytes(line)))
                read_ms.append(clock.lap())
                if restored != snapshot:
                    raise SystemExit("读回的快照与原快照不相等：探针自身有误")
                del lines
        ids_by_group[days] = group_ids
        medians = {step: statistics.median(values[1:]) for step, values in samples.items()}
        # 行情参照：M5 的做法是每个训练日用此前全部快照重算一次。两个入口各量一次。
        last = group_snapshots[-1].decision_nano
        slots = tuple(range(uncached.logical_snapshot_count - len(group_snapshots),
                            uncached.logical_snapshot_count))
        clock = Clock()
        by_view = _fit_regime_reference_from_view(
            RecordView(uncached, "FeatureSnapshot", slots), last, last)
        view_ms = clock.lap() / len(group_snapshots)
        by_tuple = fit_regime_reference(tuple(group_snapshots), last, last)
        tuple_ms = clock.lap() / len(group_snapshots)
        if by_view != by_tuple:
            raise SystemExit("两个入口得到的行情参照不相等：探针自身有误")
        resident = statistics.median(sys.getsizeof(item.source_bar_ids) for item in group_snapshots)
        rows.append({
            "view_ms": view_ms, "tuple_ms": tuple_ms, "resident": resident,
            "visible": visible_count, "steps": medians, "requests": len(requests),
            "bytes": statistics.median(expanded_bytes) if expanded_bytes else 0.0,
            "write": statistics.median(write_ms) if write_ms else 0.0,
            "read": statistics.median(read_ms) if read_ms else 0.0,
        })
    return {"rows": rows, "ids": ids_by_group, "minute_count": minute_count,
            "sessions": len(bundle.sessions), "mature": mature, "labels": total_labels,
            "store": store.stats()}


def id_digest(ids_by_group, groups) -> str:
    digest = hashlib.sha256()
    for days in groups:
        for identifier in ids_by_group[days]:
            digest.update(identifier.encode("utf-8") + b"\n")
    return digest.hexdigest()


def line(points, key):
    """过最短和最长两个历史长度的直线：返回 (截距, 每根可见历史的斜率)。"""
    (x0, y0), (x1, y1) = (points[0]["visible"], key(points[0])), (points[-1]["visible"], key(points[-1]))
    slope = (y1 - y0) / (x1 - x0) if x1 != x0 else 0.0
    return y0 - slope * x0, slope


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, nargs="+", default=[3, 20, 60, 120, 240])
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--total", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    days_list = sorted(args.days)
    check_groups = days_list[:2]

    if args.child:                      # 另一个进程：同一份输入，只重算编号
        result = run(check_groups, with_roundtrip=False, total_days=args.total)
        print(id_digest(result["ids"], check_groups))
        return 0

    started = time.perf_counter()
    result = run(days_list, with_roundtrip=True, total_days=max(days_list) + 1)
    rows = result["rows"]
    print(f"合成输入：{max(days_list) + 1} 个交易日，{result['minute_count']:,} 根分钟线，"
          f"{result['sessions']:,} 个交易节；分钟线收盘后 5 秒可用；线的编号为 bar: 加 64 位十六进制")
    print(f"每个历史长度取 {ANCHORS} 个连续的决策时刻（中位数，不计第一个）；"
          f"每个决策时刻 1 个快照 + {rows[-1]['requests']} 个请求；"
          f"标签 {result['mature']:,}/{result['labels']:,} 个成熟")
    print("\n一、内存里做：每个决策时刻各步的耗时（毫秒）。“合计”不含入库：对象直接留在内存里时没有这一步")
    print(f"{'可见分钟线':>10} " + " ".join(f"{step:>8}" for step in STEPS) + f" {'合计':>9}")
    for row in rows:
        row["memory"] = sum(value for step, value in row["steps"].items() if step != "入库")
        print(f"{row['visible']:>10,} " + " ".join(f"{row['steps'][step]:>10.2f}" for step in STEPS)
              + f" {row['memory']:>10.2f}")
    print("\n   行情参照（每个训练日用此前全部快照重算一次）：每个快照每次的耗时，和把快照对象留在内存里的代价")
    print(f"{'可见分钟线':>10} {'紧凑库视图入口 ms':>18} {'元组入口 ms':>12} {'视图是元组的':>12} {'每个快照的来源清单':>18}")
    for row in rows:
        print(f"{row['visible']:>10,} {row['view_ms']:>20.3f} {row['tuple_ms']:>14.4f} "
              f"{row['view_ms'] / row['tuple_ms']:>10.0f} 倍 {row['resident'] / 1024:>15,.0f} KB")
    print("\n二、展开往返：同一个决策时刻的 16 条记录完整写出再读回")
    print(f"{'可见分钟线':>10} {'展开字节':>14} {'写出+哈希 ms':>13} {'读回+入库 ms':>13} {'往返 ms':>10} "
          f"{'MB/秒':>7} {'是内存合计的':>12}")
    for row in rows:
        row["roundtrip"] = row["write"] + row["read"]
        speed = row["bytes"] / 1e6 / (row["roundtrip"] / 1000.0) if row["roundtrip"] else 0.0
        print(f"{row['visible']:>10,} {row['bytes']:>16,.0f} {row['write']:>14.1f} {row['read']:>14.1f} "
              f"{row['roundtrip']:>11.1f} {speed:>8.1f} {row['roundtrip'] / row['memory']:>10.1f} 倍")

    bytes_base, bytes_slope = line(rows, lambda row: row["bytes"])
    memory_base, memory_slope = line(rows, lambda row: row["memory"])
    trip_base, trip_slope = line(rows, lambda row: row["roundtrip"])
    print(f"\n按直线外推（每个决策时刻）：展开字节 ≈ {bytes_base:,.0f} + {bytes_slope:,.0f} × 可见根数；"
          f"内存合计 ≈ {memory_base:.1f} ms + 每 1 万根 {memory_slope * 1e4:.1f} ms；"
          f"展开往返 ≈ {trip_base:.1f} ms + 每 1 万根 {trip_slope * 1e4:.1f} ms")

    formal = sum(K45_ANCHORS.values())
    average = (K45_FORMAL_LOGICAL_BYTES / formal - bytes_base) / bytes_slope
    print(f"\n三、套到 K45 的规模上（{formal:,} 个分钟决策时刻）")
    print(f"第四批公布的正式展开量是 {K45_FORMAL_LOGICAL_BYTES:,} 字节，平均每个决策时刻 "
          f"{K45_FORMAL_LOGICAL_BYTES / formal / 1e6:.1f} MB；按上面的直线折算，平均可见历史约 {average:,.0f} 根")
    per_memory = memory_base + memory_slope * average
    per_trip = trip_base + trip_slope * average
    store_base, store_slope = line(rows, lambda row: row["steps"]["入库"])
    view_base, view_slope = line(rows, lambda row: row["view_ms"])
    tuple_base, tuple_slope = line(rows, lambda row: row["tuple_ms"])
    resident_base, resident_slope = line(rows, lambda row: row["resident"])
    visits = K45_TRAIN_DAYS * K45_ANCHORS["TRAIN"] / 2
    view_hours = visits * (view_base + view_slope * average) / 3_600_000.0
    tuple_minutes = visits * (tuple_base + tuple_slope * average) / 60000.0
    store_minutes = formal * (store_base + store_slope * average) / 60000.0
    resident_gb = (K45_ANCHORS["TRAIN"] + K45_ANCHORS["CAL"]) * (resident_base + resident_slope * average) / 1e9
    once = per_memory * formal / 60000.0
    again = per_memory * K45_ANCHORS["TEST"] / 60000.0
    total_bytes = K45_FORMAL_LOGICAL_BYTES + K45_REHEARSAL_LOGICAL_BYTES
    trip_hours = per_trip * (total_bytes / (K45_FORMAL_LOGICAL_BYTES / formal)) / 3_600_000.0
    print(f"  内存里做一遍（特征到标签）：每个决策时刻 {per_memory:.0f} ms，合计约 {once:.0f} 分钟")
    print(f"  行情参照（{K45_TRAIN_DAYS} 个训练日，共约 {visits / 1e6:.2f} 百万次“快照×日”）：")
    print(f"    快照对象留在内存里、走元组入口：约 {tuple_minutes:.1f} 分钟；"
          f"训练段和校准段的来源清单常驻约 {resident_gb:.1f} GB")
    print(f"    放进紧凑库、走视图入口：约 {view_hours:.1f} 小时（每次都要把快照连同整份来源清单重建出来）；"
          f"另加入库约 {store_minutes:.0f} 分钟")
    print(f"  阶段三把检验段重算一遍核对编号：再加约 {again:.0f} 分钟")
    print(f"  展开往返（正式 + 预演，共 {total_bytes / 1e12:.2f} 万亿字节）：每个决策时刻 {per_trip:.0f} ms，"
          f"合计约 {trip_hours:.1f} 小时（本机；不含压缩和磁盘）")
    identifiers = [identifier for group in result["ids"].values() for identifier in group]
    width = sum(len(identifier) + 1 for identifier in identifiers) / len(identifiers)
    print(f"  同一批记录的全部编号：{formal * 16:,} 个，约 {formal * 16 * width / 1e6:.0f} MB")

    stats = result["store"]
    print(f"\n四、内存：进程峰值{memory_text()}（含 {result['minute_count']:,} 根分钟线的输入和读回用的第二个库）；"
          f"内存库里 {stats['logical_snapshots']:,} 个快照、{stats['logical_requests']:,} 个请求，"
          f"来源链 {stats['spine_nodes']:,} 个节点、{stats['interned_ids']:,} 个不重复的编号")

    environment = dict(os.environ, PYTHONHASHSEED="20261114")
    child = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", str(Path(__file__).resolve()), "--child",
         "--total", str(max(days_list) + 1), "--days", *map(str, check_groups)],
        capture_output=True, encoding="utf-8", errors="replace", env=environment, cwd=os.getcwd())
    mine = id_digest(result["ids"], check_groups)
    theirs = child.stdout.strip().splitlines()[-1] if child.stdout.strip() else f"子进程失败：{child.stderr[-300:]}"
    count = sum(len(result["ids"][days]) for days in check_groups)
    same = child.returncode == 0 and mine == theirs
    print(f"\n五、另一个进程重算（PYTHONHASHSEED 不同）：{count:,} 个快照和请求的编号序列 SHA-256 "
          f"{'相同' if same else '不同'}（{mine[:16]} / {theirs[:16]}）")
    print(f"\n探针总耗时 {time.perf_counter() - started:.0f} 秒")
    if not same:
        print("另一个进程重算出的编号不同：不能用“封存编号、后一阶段重算核对”的办法")
        return 2
    ratio = rows[-1]["roundtrip"] / rows[-1]["memory"]
    print(f"最长历史上，展开往返是内存里全部步骤的 {ratio:.1f} 倍")
    return 1 if ratio > 3 else 0


if __name__ == "__main__":
    raise SystemExit(main())
