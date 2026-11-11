"""Short K10-core clock benchmark on SYNTHETIC calendars.

Measures ``ak_trading.v2.clock`` horizon/holding/can-open call cost for
fixed synthetic calendars of 5/20/60/250 trading days (8 predetermined
queries, 3 rounds each) in four modes:

* ``legacy`` — the frozen pre-K10 implementation (tests/v2_clock_legacy_oracle.py);
* ``cold_first_build`` — calendar index cleared before every single call;
* ``warm_reuse`` — the same tuple reused after one untimed priming pass;
* ``fresh_tuple_miss`` — an equal-value freshly built tuple per call, the
  documented identity-cache boundary.

Timer honesty on this host: single ``perf_counter_ns`` deltas around
microsecond-scale calls occasionally read 0 (observed both directly and,
much more often, inside job-object supervision). Warm and fresh modes
therefore record REPS=5 raw nanosecond repetitions per round and use the
median as the round sample; every raw repetition, the count of zero/negative
deltas and a back-to-back timer-floor measurement are all written to the
JSON. No samples are dropped and no thresholds are applied.

Timing (perf_counter) and memory (tracemalloc) are collected in separate
passes; the memory trace for one tier is continuous across the cold build
and the cache clear so both the retained bytes and the release are visible.
There are no research conclusions here; the calendars are synthetic shapes,
not historical data.

Usage (repository root):
    python -B -X utf8 scripts/bench_clock_core.py --output <out-of-repo JSON>
"""
from __future__ import annotations

import argparse
import gc
import importlib.util
import json
import os
import platform
import statistics
import sys
import time
import tracemalloc
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from ak_trading.v2 import clock  # noqa: E402
from ak_trading.v2.contracts import Horizon, HoldingPolicy, SessionWindow  # noqa: E402

_ORACLE_PATH = ROOT / "tests" / "v2_clock_legacy_oracle.py"
_ORACLE_SPEC = importlib.util.spec_from_file_location("v2_clock_legacy_oracle", _ORACLE_PATH)
oracle = importlib.util.module_from_spec(_ORACLE_SPEC)
_ORACLE_SPEC.loader.exec_module(oracle)

NS = 1_000_000_000
MINUTE = 60 * NS
PRODUCT = "SYN.rb"
CALENDAR = "synthetic-calendar-v2.1.0"
FRAGMENTS = ((9, 0, 10, 15), (10, 30, 11, 30), (13, 30, 15, 0))
TIERS = (5, 20, 60, 250)
ROUNDS = 3
REPS = 5


def _nano(day_date: date, hour: int, minute: int = 0) -> int:
    moment = datetime(day_date.year, day_date.month, day_date.day, hour, minute,
                      tzinfo=timezone.utc) - timedelta(hours=8)
    delta = moment.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * NS


def build_calendar(days: int):
    """SYNTHETIC RB-like calendar: 345 planned minutes per trading day."""
    trading_days: list[date] = []
    cursor = date(2026, 1, 5)
    while len(trading_days) < days:
        if cursor.weekday() < 5:
            trading_days.append(cursor)
        cursor += timedelta(days=1)
    sessions: list[SessionWindow] = []
    for index, day_date in enumerate(trading_days):
        if index:
            evening = trading_days[index - 1]
            sessions.append(SessionWindow(
                session_id=f"N{day_date.isoformat()}", product=PRODUCT,
                trading_day=day_date, start_nano=_nano(evening, 21), end_nano=_nano(evening, 23),
                kind="NIGHT", calendar_version=CALENDAR, evidence_status="SCENARIO"))
        for fragment_index, (a, b, x, y) in enumerate(FRAGMENTS):
            sessions.append(SessionWindow(
                session_id=f"D{day_date.isoformat()}-{fragment_index}", product=PRODUCT,
                trading_day=day_date, start_nano=_nano(day_date, a, b),
                end_nano=_nano(day_date, x, y), kind="DAY",
                calendar_version=CALENDAR, evidence_status="SCENARIO"))
    return tuple(sessions), trading_days


def build_queries(trading_days: list[date]):
    first = trading_days[0]
    return (
        ("min5_open", "horizon", _nano(first, 9), Horizon("TRADING_MINUTE", 5)),
        ("min60_break", "horizon", _nano(first, 10), Horizon("TRADING_MINUTE", 60)),
        ("min5_lunch", "horizon", _nano(first, 11, 30), Horizon("TRADING_MINUTE", 5)),
        ("day1", "horizon", _nano(first, 10), Horizon("TRADING_DAY", 1)),
        ("day5", "horizon", _nano(first, 10), Horizon("TRADING_DAY", 5)),
        ("hold_intraday", "holding", _nano(first, 10), HoldingPolicy(True, True, False, 30, 5)),
        ("hold_night_bridge", "holding", _nano(first, 21, 30), HoldingPolicy(True, True, True, 30, 5)),
        ("can_open_edge", "can_open", _nano(first, 14, 20), HoldingPolicy(True, True, False, 30, 5)),
    )


def invoke(module, spec, sessions):
    _, kind, decision, payload = spec
    if kind == "horizon":
        return module.horizon_window(decision, PRODUCT, payload, sessions)
    if kind == "holding":
        return module.holding_deadline(decision, PRODUCT, payload, sessions)
    return module.can_open_before_close(decision, PRODUCT, payload, sessions)


def safe_invoke(module, spec, sessions):
    """Run one query, returning the raised exception as the outcome instead of aborting."""
    try:
        return invoke(module, spec, sessions)
    except BaseException as error:  # noqa: BLE001 - benchmark outcome capture
        return error


def describe(result) -> str:
    if isinstance(result, BaseException):
        return f"error:{type(result).__name__}:{result.args[0] if result.args else ''}"
    return f"value:{result}"


def timed_call_ns(spec, sessions) -> tuple[int, object]:
    begin = time.perf_counter_ns()
    result = safe_invoke(clock, spec, sessions)
    return time.perf_counter_ns() - begin, result


def timed_median_ns(spec, sessions_factory, repetitions: int):
    """Median raw-ns sample over ``repetitions`` recorded repetitions."""
    raws: list[int] = []
    result = None
    for _ in range(repetitions):
        raw, result = timed_call_ns(spec, sessions_factory())
        raws.append(raw)
    return statistics.median(raws), raws, result


def zero_deltas(raws: list[int]) -> int:
    return sum(1 for value in raws if value <= 0)


def timer_floor_ns(count: int = 200) -> int:
    reads = [time.perf_counter_ns() for _ in range(count)]
    positive = [b - a for a, b in zip(reads, reads[1:]) if b > a]
    return min(positive) if positive else 0


def summarize(samples_ns: dict[str, list[float]]) -> dict:
    summary = {}
    for name, values in samples_ns.items():
        seconds = [value / 1e9 for value in values]
        summary[name] = {
            "min": min(seconds),
            "median": statistics.median(seconds),
            "mean": statistics.fmean(seconds),
            "max": max(seconds),
        }
    return summary


def run_tier(days: int) -> dict:
    sessions, trading_days = build_calendar(days)
    queries = build_queries(trading_days)

    outcomes = {spec[0]: describe(safe_invoke(clock, spec, sessions)) for spec in queries}

    # Legacy frozen implementation (no index involvement); millisecond-scale
    # calls where the timer is reliable, so single shots per round.
    legacy_ns: dict[str, list[int]] = {spec[0]: [] for spec in queries}
    for _ in range(ROUNDS):
        for spec in queries:
            begin = time.perf_counter_ns()
            safe_invoke(oracle, spec, sessions)
            legacy_ns[spec[0]].append(time.perf_counter_ns() - begin)

    # First build: cleared before every call, each sample is a true cold call.
    clock._clear_calendar_index_cache()
    cold_ns: dict[str, list[int]] = {spec[0]: [] for spec in queries}
    for _ in range(ROUNDS):
        for spec in queries:
            clock._clear_calendar_index_cache()
            raw, _ = timed_call_ns(spec, sessions)
            cold_ns[spec[0]].append(raw)

    # Warm reuse of the same tuple after one untimed priming pass. Microsecond
    # calls: median of REPS recorded repetitions per round.
    clock._clear_calendar_index_cache()
    for spec in queries:
        safe_invoke(clock, spec, sessions)
    warm_ns: dict[str, list[int]] = {spec[0]: [] for spec in queries}
    warm_raw_ns: dict[str, list[int]] = {spec[0]: [] for spec in queries}
    for _ in range(ROUNDS):
        for spec in queries:
            sample, raws, _ = timed_median_ns(spec, lambda: sessions, REPS)
            warm_ns[spec[0]].append(sample)
            warm_raw_ns[spec[0]].extend(raws)
    warm_cache = clock._index_cache_state()

    # Same-value fresh tuple per repetition: a deliberate identity-cache miss.
    # ``tuple(sessions)`` and even ``sessions + ()`` return the same object on
    # this CPython, so the equal-value copy goes through a list to force the
    # miss. The copy itself is built untimed outside timed_call_ns.
    def fresh_sessions() -> tuple:
        return tuple(list(sessions))

    fresh_ns: dict[str, list[int]] = {spec[0]: [] for spec in queries}
    fresh_raw_ns: dict[str, list[int]] = {spec[0]: [] for spec in queries}
    stats_before_fresh = clock._index_stats()
    for _ in range(ROUNDS):
        for spec in queries:
            sample, raws, _ = timed_median_ns(spec, fresh_sessions, REPS)
            fresh_ns[spec[0]].append(sample)
            fresh_raw_ns[spec[0]].extend(raws)
    fresh_validations = (clock._index_stats()["session_validations"]
                         - stats_before_fresh["session_validations"])

    # Memory: one continuous trace over the cold build and the clear so the
    # retained index bytes and their release are both visible; the legacy and
    # warm passes are separate traces (warm shows near-zero new allocations).
    legacy_memory = {"baseline": 0, "current_after": 0, "peak": 0}
    gc.collect()
    tracemalloc.start()
    tracemalloc.reset_peak()
    legacy_baseline, _ = tracemalloc.get_traced_memory()
    for spec in queries:
        safe_invoke(oracle, spec, sessions)
    legacy_current, legacy_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    legacy_memory = {"baseline": legacy_baseline, "current_after": legacy_current,
                     "peak": legacy_peak}

    clock._clear_calendar_index_cache()
    gc.collect()
    tracemalloc.start()
    tracemalloc.reset_peak()
    build_baseline, _ = tracemalloc.get_traced_memory()
    for spec in queries:
        safe_invoke(clock, spec, sessions)
    build_current, build_peak = tracemalloc.get_traced_memory()
    cache_before_clear = clock._index_cache_state()
    clock._clear_calendar_index_cache()
    gc.collect()
    release_current, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    cold_memory = {"baseline": build_baseline, "current_after": build_current,
                   "peak": build_peak}
    release_memory = {"retained_before_clear": build_current,
                      "current_after_clear": release_current,
                      "cache_state_before_clear": cache_before_clear}

    for spec in queries:  # re-prime, then trace the warm pass
        safe_invoke(clock, spec, sessions)
    gc.collect()
    tracemalloc.start()
    tracemalloc.reset_peak()
    warm_baseline, _ = tracemalloc.get_traced_memory()
    for spec in queries:
        safe_invoke(clock, spec, sessions)
    warm_current, warm_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    warm_memory = {"baseline": warm_baseline, "current_after": warm_current,
                   "peak": warm_peak}

    return {
        "trading_days": days,
        "sessions": len(sessions),
        "planned_minutes_per_day": 345,
        "repetitions_per_warm_fresh_sample": REPS,
        "query_outcomes": outcomes,
        "legacy_ns": legacy_ns,
        "cold_first_build_ns": cold_ns,
        "warm_reuse_ns": warm_ns,
        "warm_reuse_raw_ns": warm_raw_ns,
        "fresh_tuple_miss_ns": fresh_ns,
        "fresh_tuple_miss_raw_ns": fresh_raw_ns,
        "fresh_tuple_miss_validations": fresh_validations,
        "zero_delta_raw_reads": {
            "warm_reuse": zero_deltas([v for values in warm_raw_ns.values() for v in values]),
            "fresh_tuple_miss": zero_deltas([v for values in fresh_raw_ns.values() for v in values]),
            "cold_first_build": zero_deltas([v for values in cold_ns.values() for v in values]),
            "legacy": zero_deltas([v for values in legacy_ns.values() for v in values]),
        },
        "summary_seconds": {
            "legacy": summarize(legacy_ns),
            "cold_first_build": summarize(cold_ns),
            "warm_reuse": summarize(warm_ns),
            "fresh_tuple_miss": summarize(fresh_ns),
        },
        "memory_bytes": {
            "legacy": legacy_memory,
            "cold_first_build": cold_memory,
            "warm_reuse": warm_memory,
            "release_after_clear": release_memory,
        },
        "cache_state_after_warm": warm_cache,
    }


def write_atomic(path: Path, payload: dict) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True)
    json.loads(text)  # full round-trip validation before touching the target
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True,
                        help="output JSON path outside the repository")
    arguments = parser.parse_args()
    output = Path(arguments.output).resolve()
    if output == ROOT or ROOT in output.parents:
        raise SystemExit("--output must live outside the repository")
    output.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    tiers = {}
    for days in TIERS:
        tiers[str(days)] = run_tier(days)
    payload = {
        "synthetic": True,
        "note": ("SYNTHETIC calendars; timing and memory are separate passes; warm and fresh "
                 "samples are medians of 5 recorded repetitions; raw samples kept; no "
                 "thresholds and no research conclusions"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "argv": sys.argv,
        "rounds_per_mode": ROUNDS,
        "timer_floor_ns": timer_floor_ns(),
        "tiers": tiers,
        "index_stats_total": clock._index_stats(),
        "wall_seconds": round(time.perf_counter() - started, 6),
    }
    write_atomic(output, payload)
    print(json.dumps({"output": str(output), "wall_seconds": payload["wall_seconds"],
                      "tiers": list(tiers)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
