"""H4 visibility benchmark: build cost, retained memory, raw full and
recent-20 queries in both intraday modes, per-query intraday day changes and
adversarial availability ordering — all measured on the actual product
implementation (``ak_trading.v2.data.visible_bars`` /
``visible_bars_recent`` with the real bounded index cache).

Reference equality is sampled against an independent scan implementation of
the pre-H4 per-query semantics (both intraday modes, recent window and
capacity arithmetic). Root's old-call baseline (C:/AKH1107/bench0) is cited
as a labeled constant; the external probe itself is never read or modified
by this benchmark. All inputs are SYNTHETIC and clearly labeled as such.

Usage: python scripts/bench_visible_bars.py [--output PATH]
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import random
import sys
import time
import tracemalloc
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from ak_trading.v2 import data as v2_data
from ak_trading.v2.contracts import (
    DataQuality, FeeLeg, FrozenInput, Instrument, MarketBar, SessionWindow,
    TradingRule,
)
from ak_trading.v2.decision.execution import capacity_before
from ak_trading.v2.schemas import canonical_json_bytes

NS = 1_000_000_000
M = 60 * NS
BASE = 1_760_000_000 * NS
EPOCH_DAY = date(2026, 6, 1)
BARS_PER_DAY = 240
SIZES = (1200, 2400, 4800, 9600)
ROOT_OLD_BASELINE_MS_PER_CALL = {
    "1200": 1.22, "2400": 5.64, "4800": 20.32, "9600": 60.72,
    "note": ("Root bench0 old-call baseline on this machine; external probe "
             "not read or modified by this benchmark"),
}
EQUALITY_SAMPLES = 120
TIMING_ITERATIONS = 200
RECENT_ITERATIONS = 2000
CONTRACT = "rb"
PRODUCT = "RB"


def _quality() -> DataQuality:
    return DataQuality(status="READY", reasons=(), temporal_basis="reconstructed",
                       input_hash=hashlib.sha256(b"bench-synthetic").hexdigest())


def _instrument() -> Instrument:
    return Instrument(contract_id=CONTRACT, raw_symbol=CONTRACT, product=PRODUCT,
                      sector="synthetic", risk_groups=("SYN",), listing_nano=0,
                      expiry_nano=None, last_trade_nano=None, delivery_cutoff_nano=None,
                      quality=_quality())


def _rule() -> TradingRule:
    leg = FeeLeg(per_lot_cny=Decimal("0"), notional_rate=Decimal("0.0001"),
                 explicit_zero=False)
    return TradingRule(
        rule_id="bench-rule", contract_id=CONTRACT, effective_from_nano=0,
        effective_until_nano=BASE + 400 * 86_400 * NS, available_at_nano=0,
        multiplier=Decimal("10"), price_tick=Decimal("1"), lot_step=1, min_open_lots=1,
        open_fee=leg, close_fee=leg, close_today_fee=leg,
        long_margin_rate=Decimal("0.09"), short_margin_rate=Decimal("0.09"),
        limit_up=None, limit_down=None, slippage_ticks=Decimal("1"),
        impact_ticks=Decimal("0"), participation_rate=Decimal("0.1"),
        evidence="SCENARIO", source_sha256="a" * 64)


def _session(day_index: int) -> SessionWindow:
    day_start = BASE + day_index * 86_400 * NS
    return SessionWindow(f"bench-d{day_index}", PRODUCT, EPOCH_DAY + timedelta(days=day_index),
                         day_start + 9 * 3600 * NS, day_start + 13 * 3600 * NS,
                         "DAY", "bench-calendar", "SCENARIO")


def _minute_bars(days: int) -> list[MarketBar]:
    bars = []
    for day_index in range(days):
        day_start = BASE + day_index * 86_400 * NS
        session_start = day_start + 9 * 3600 * NS
        for minute in range(BARS_PER_DAY):
            start = session_start + minute * M
            bars.append(MarketBar(
                bar_id=f"m-{day_index}-{minute}", contract_id=CONTRACT,
                raw_symbol=CONTRACT, product=PRODUCT, duration_seconds=60,
                datetime_nano=start, start_nano=start, end_nano=start + M,
                trading_day=EPOCH_DAY + timedelta(days=day_index), session_id=f"bench-d{day_index}",
                available_at_nano=start + M + NS, imported_at_nano=start + M,
                open=Decimal("3900"), high=Decimal("3910"), low=Decimal("3890"),
                close=Decimal("3905"), volume=Decimal(1000 + minute),
                open_oi=Decimal("0"), close_oi=Decimal("0"), complete=True,
                expected_minutes=1, actual_minutes=1, source="bench-synthetic",
                quality=_quality()))
    return bars


def _daily_bars(days: int) -> list[MarketBar]:
    bars = []
    for day_index in range(days):
        day_start = BASE + day_index * 86_400 * NS
        end = day_start + 13 * 3600 * NS
        bars.append(MarketBar(
            bar_id=f"d-{day_index}", contract_id=CONTRACT, raw_symbol=CONTRACT,
            product=PRODUCT, duration_seconds=86400,
            datetime_nano=day_start + 9 * 3600 * NS, start_nano=day_start + 9 * 3600 * NS,
            end_nano=end, trading_day=EPOCH_DAY + timedelta(days=day_index),
            session_id=f"bench-d{day_index}", available_at_nano=end + NS,
            imported_at_nano=end, open=Decimal("3900"), high=Decimal("3910"),
            low=Decimal("3890"), close=Decimal("3905"), volume=Decimal("240000"),
            open_oi=Decimal("0"), close_oi=Decimal("0"), complete=True,
            expected_minutes=240, actual_minutes=240, source="bench-synthetic",
            quality=_quality()))
    return bars


def _bundle(bars: list[MarketBar], sessions: list[SessionWindow]) -> FrozenInput:
    return FrozenInput(Path("SYNTHETIC-BENCH"), "0" * 64, (_instrument(),),
                       tuple(sessions), tuple(bars), (), (_rule(),))


# ── independent pre-H4 reference scan (differential standard) ──────────────

def _reference_current_day(bundle: FrozenInput, decision: int):
    products = {i.product for i in bundle.instruments if i.contract_id == CONTRACT}
    products.update(b.product for b in bundle.bars if b.contract_id == CONTRACT)
    scoped = [s for s in bundle.sessions if s.product in products]
    for session in sorted(scoped, key=lambda s: s.start_nano):
        if session.start_nano <= decision < session.end_nano:
            return session.trading_day
    preceding = [s for s in scoped if s.end_nano <= decision]
    if preceding:
        return max(preceding, key=lambda s: s.end_nano).trading_day
    return None


def _reference_visible_bars(bundle: FrozenInput, duration: int, decision: int,
                            intraday: bool):
    current_day = (_reference_current_day(bundle, decision)
                   if intraday and duration == 86400 else None)
    products = {b.product for b in bundle.bars if b.contract_id == CONTRACT}
    sessions = [s for s in bundle.sessions if s.product in products]

    def on_minute_grid(bar: MarketBar) -> bool:
        if not isinstance(bar, MarketBar) or bar.duration_seconds != 60:
            return True
        for session in sessions:
            if session.start_nano <= bar.start_nano < session.end_nano:
                return (bar.start_nano - session.start_nano) % M == 0
        return True

    visible = [
        bar for bar in bundle.bars
        if bar.contract_id == CONTRACT
        and bar.duration_seconds == duration
        and bar.complete
        and bar.end_nano <= decision
        and bar.available_at_nano is not None
        and bar.available_at_nano <= decision
        and bar.quality.status == "READY"
        and bar.quality.temporal_basis != "unknown"
        and on_minute_grid(bar)
        and not (current_day is not None and bar.trading_day == current_day)
    ]
    return tuple(sorted(visible, key=lambda b: (b.start_nano, b.datetime_nano)))


# ── measurement helpers ─────────────────────────────────────────────────────

def _ms_per_call(operation: Callable[[], object], iterations: int) -> float:
    gc.collect()
    start = time.perf_counter()
    for _ in range(iterations):
        operation()
    return (time.perf_counter() - start) * 1000.0 / iterations


def _cycler(values: list[int]) -> Callable[[], int]:
    state = {"index": 0}

    def next_value() -> int:
        value = values[state["index"] % len(values)]
        state["index"] += 1
        return value

    return next_value


def _decision_grid(bundle: FrozenInput, count: int) -> list[int]:
    starts = [b.start_nano for b in bundle.bars if b.duration_seconds == 60]
    first, last = min(starts), max(starts) + M
    step = max(1, (last - first) // count)
    return [first + (index * step) % (last - first) for index in range(count)]


def _equality_sample(bundle: FrozenInput, decisions: list[int], rule: TradingRule) -> dict:
    full_ok = recent_ok = capacity_ok = 0
    for decision in decisions:
        for intraday in (False, True):
            produced = v2_data.visible_bars(bundle, CONTRACT, 60, decision, intraday)
            expected = _reference_visible_bars(bundle, 60, decision, intraday)
            if produced == expected:
                full_ok += 1
            recent = v2_data.visible_bars_recent(bundle, CONTRACT, 60, decision,
                                                 intraday, 20)
            window = tuple(sorted(expected, key=lambda b: (b.end_nano, b.bar_id))[-20:])
            if recent == window:
                recent_ok += 1
            if capacity_before(recent, CONTRACT, 60, decision, rule) == \
                    capacity_before(expected, CONTRACT, 60, decision, rule):
                capacity_ok += 1
    return {"queries": len(decisions), "modes": ["intraday=False", "intraday=True"],
            "checks": len(decisions) * 2,
            "full_equal": full_ok, "recent_equal": recent_ok,
            "capacity_equal": capacity_ok}


def _measure_size(minute_bars: int, rule: TradingRule) -> dict:
    days = minute_bars // BARS_PER_DAY
    sessions = [_session(day_index) for day_index in range(days)]
    bundle = _bundle(_minute_bars(days) + _daily_bars(days), sessions)
    assert len([b for b in bundle.bars if b.duration_seconds == 60]) == minute_bars

    # Cold build: first query on an empty cache, three fresh repetitions.
    # Time is measured untraced; the traced repetition only reports the
    # allocation peak (tracing itself would inflate the wall time).
    probe = BASE + (days // 2) * 86_400 * NS + 10 * 3600 * NS
    cold_ms = []
    for _ in range(3):
        v2_data.clear_visible_index_cache()
        gc.collect()
        started = time.perf_counter()
        v2_data.visible_bars(bundle, CONTRACT, 60, probe, False)
        cold_ms.append((time.perf_counter() - started) * 1000.0)
    v2_data.clear_visible_index_cache()
    tracemalloc.start()
    v2_data.visible_bars(bundle, CONTRACT, 60, probe, False)
    cold_peak_bytes = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    cold_build_ms = median(cold_ms)

    # The late decision exposes the whole minute history at once — the same
    # worst-case full-population shape Root's old-call baseline measured.
    late = max(b.end_nano for b in bundle.bars if b.duration_seconds == 60) + 2 * M
    decisions = _decision_grid(bundle, TIMING_ITERATIONS)
    next_decision = _cycler(decisions)
    scan_ms = _ms_per_call(
        lambda: _reference_visible_bars(bundle, 60, late, False), 40)
    full_false_ms = _ms_per_call(
        lambda: v2_data.visible_bars(bundle, CONTRACT, 60, late, False),
        TIMING_ITERATIONS)
    full_true_ms = _ms_per_call(
        lambda: v2_data.visible_bars(bundle, CONTRACT, 60, late, True),
        TIMING_ITERATIONS)
    full_cycled_ms = _ms_per_call(
        lambda: v2_data.visible_bars(bundle, CONTRACT, 60, next_decision(), False),
        TIMING_ITERATIONS)
    recent_false_ms = _ms_per_call(
        lambda: v2_data.visible_bars_recent(bundle, CONTRACT, 60, late, False, 20),
        RECENT_ITERATIONS)
    recent_true_ms = _ms_per_call(
        lambda: v2_data.visible_bars_recent(bundle, CONTRACT, 60, late, True, 20),
        RECENT_ITERATIONS)
    # Per-query intraday day changes: every query lands inside a different
    # day's session, so the current-trading-day exclusion is recomputed.
    day_decisions = [BASE + day_index * 86_400 * NS + 10 * 3600 * NS
                     for day_index in range(days)] * max(1, TIMING_ITERATIONS // days)
    next_day_decision = _cycler(day_decisions)
    day_change_ms = _ms_per_call(
        lambda: v2_data.visible_bars(bundle, CONTRACT, 86400, next_day_decision(), True),
        TIMING_ITERATIONS)

    equality = _equality_sample(
        bundle,
        [day_decisions[index] for index in range(
            0, len(day_decisions), max(1, len(day_decisions) // EQUALITY_SAMPLES))][:EQUALITY_SAMPLES]
        + decisions[:EQUALITY_SAMPLES] + [late], rule)
    stats = v2_data.visible_index_cache_stats()
    return {
        "bars": minute_bars, "days": days,
        "late_decision_nano": late,
        "cold_build_ms_median": round(cold_build_ms, 4),
        "cold_build_ms_reps": [round(value, 4) for value in cold_ms],
        "cold_build_peak_traced_bytes": cold_peak_bytes,
        "scan_full_ms_per_call": round(scan_ms, 4),
        "full_intraday_false_ms_per_call": round(full_false_ms, 5),
        "full_intraday_true_ms_per_call": round(full_true_ms, 5),
        "full_cycled_avg_ms_per_call": round(full_cycled_ms, 5),
        "recent20_intraday_false_ms_per_call": round(recent_false_ms, 5),
        "recent20_intraday_true_ms_per_call": round(recent_true_ms, 5),
        "intraday_day_change_ms_per_call": round(day_change_ms, 5),
        "intraday_day_change_queries": len(set(day_decisions)),
        "retained_bytes_after_warm": stats["retained_bytes"],
        "cache_entries": stats["entries"],
        "equality": equality,
    }


def _measure_adversarial(rule: TradingRule) -> dict:
    # 9600 minute bars in SHUFFLED bundle order with adversarial
    # availability: every third bar becomes visible only three days after it
    # closes, so recent-20 backward scans must skip invisible trailing bars.
    days = 9600 // BARS_PER_DAY
    sessions = [_session(day_index) for day_index in range(days)]
    bars = _minute_bars(days)
    for index, bar in enumerate(bars):
        if index % 3 == 0:
            bars[index] = MarketBar(
                **{**bar.__dict__,
                   "available_at_nano": bar.end_nano + 3 * 86_400 * NS,
                   "imported_at_nano": bar.end_nano + 3 * 86_400 * NS})
    rng = random.Random(20261107)
    rng.shuffle(bars)
    bundle = _bundle(bars + _daily_bars(days), sessions)
    late = max(b.available_at_nano for b in bars)
    last_end = max(b.end_nano for b in bars if b.duration_seconds == 60)
    decisions = _decision_grid(bundle, TIMING_ITERATIONS) + [late, late + M]
    next_decision = _cycler(decisions)
    full_ms = _ms_per_call(
        lambda: v2_data.visible_bars(bundle, CONTRACT, 60, next_decision(), False),
        TIMING_ITERATIONS)
    # Steady-state adversarial recent: two minutes after the last close the
    # one-minute-availability bars are visible but every third bar (3-day
    # availability) is still invisible, so the backward window scan must
    # skip runs of invisible trailing bars on every query.
    skip_decision = last_end + 2 * M
    recent_ms = _ms_per_call(
        lambda: v2_data.visible_bars_recent(bundle, CONTRACT, 60, skip_decision,
                                            False, 20),
        RECENT_ITERATIONS)
    equality = _equality_sample(bundle, decisions[:EQUALITY_SAMPLES] + [skip_decision],
                                rule)
    return {
        "bars": 9600, "ordering": "shuffled-bundle-order+every-third-bar-late-availability",
        "full_intraday_false_ms_per_call": round(full_ms, 5),
        "recent20_intraday_false_ms_per_call": round(recent_ms, 5),
        "equality": equality,
    }



def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=None,
                        help="write the full measurement JSON to this path")
    arguments = parser.parse_args()
    rule = _rule()
    report = {
        "benchmark": "bench_visible_bars",
        "input_kind": "SYNTHETIC",
        "implementation": ("ak_trading.v2.data visible_bars/visible_bars_recent "
                           "with the H4 bounded index cache"),
        "root_old_baseline_ms_per_call": ROOT_OLD_BASELINE_MS_PER_CALL,
        "sizes": [],
        "adversarial": None,
        "cache": {},
    }
    for size in SIZES:
        v2_data.clear_visible_index_cache()
        row = _measure_size(size, rule)
        report["sizes"].append(row)
        report["cache"] = dict(v2_data.visible_index_cache_stats())
    v2_data.clear_visible_index_cache()
    report["adversarial"] = _measure_adversarial(rule)
    report["cache_after_adversarial"] = dict(v2_data.visible_index_cache_stats())

    header = (f"{'bars':>6} {'days':>5} {'cold ms':>9} {'scan ms':>9} "
              f"{'full F/T ms':>14} {'full avg':>9} {'recent F/T ms':>15} {'daychg ms':>9} "
              f"{'retained B':>11} {'equal':>13}")
    print(header)
    for row in report["sizes"]:
        print(f"{row['bars']:>6} {row['days']:>5} "
              f"{row['cold_build_ms_median']:>9.3f} "
              f"{row['scan_full_ms_per_call']:>9.3f} "
              f"{row['full_intraday_false_ms_per_call']:>6.3f}/"
              f"{row['full_intraday_true_ms_per_call']:<7.3f} "
              f"{row['full_cycled_avg_ms_per_call']:>9.4f} "
              f"{row['recent20_intraday_false_ms_per_call']:>7.4f}/"
              f"{row['recent20_intraday_true_ms_per_call']:<7.4f} "
              f"{row['intraday_day_change_ms_per_call']:>9.4f} "
              f"{row['retained_bytes_after_warm']:>11} "
              f"{row['equality']['full_equal']}/{row['equality']['checks']} "
              f"r{row['equality']['recent_equal']} c{row['equality']['capacity_equal']}")
    adversarial = report["adversarial"]
    print(f"adversarial {adversarial['bars']} bars ({adversarial['ordering']}): "
          f"full {adversarial['full_intraday_false_ms_per_call']:.4f} ms/call, "
          f"recent20 {adversarial['recent20_intraday_false_ms_per_call']:.4f} ms/call, "
          f"equal {adversarial['equality']['full_equal']}/{adversarial['equality']['checks']} "
          f"r{adversarial['equality']['recent_equal']} "
          f"c{adversarial['equality']['capacity_equal']}")
    if arguments.output is not None:
        payload = canonical_json_bytes(report)
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        with open(arguments.output, "wb") as handle:
            handle.write(payload)
        print(f"wrote {arguments.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
