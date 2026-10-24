"""Bounded, read-only TqSdk feed. Importing this module never imports TqSdk.

Only ``open_readonly_api`` may obtain the two existing credential environment
values. Its production factory is the project's exact-local-TqSim boundary.
The collector has no account, order, cancel, task, database or service surface.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import time
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from ak_trading.v2.artifacts import canonical_json_bytes, sha256_hex, stable_id
from ak_trading.v2.schemas import parse_json_bytes
from ak_trading.v2.validation.forward_paper import (
    SCHEMA, NS, MINUTE, OUTPUT_LIMIT, ClockSample, ForwardPaperError, ForwardPaperHost,
    SourceEvent, content, preflight, received_batch, validate_runtime_inputs, verified_file,
)

QUOTE_FIELDS = ("datetime", "last_price", "ask_price1", "bid_price1", "volume", "open_interest",
                "price_tick", "volume_multiple", "ins_class", "expired", "underlying_symbol", "expire_datetime")
KLINE_FIELDS = ("id", "datetime", "open", "high", "low", "close", "volume", "open_oi", "close_oi")
MEMORY_LIMIT = 2 * 1024 ** 3


def _get(entity, name, default=None):
    return entity.get(name, default) if isinstance(entity, Mapping) else getattr(entity, name, default)


def _scalar(value):
    """Normalize an allowed scalar without copying an Entity's private members."""
    if value is None or isinstance(value, (str, bool)):
        return value
    # numpy scalar values have .item(); conversion is independent of the SDK.
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        if not math.isfinite(value):
            return None
        return str(value)
    raise ForwardPaperError("SDK_SCALAR_TYPE_INVALID")


def _serial_integer(value):
    """Keep numeric integer values without rounding or a float-to-text detour."""
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, bool):
        raise ValueError("boolean serial identity")
    result = int(value)
    if isinstance(value, (float, Decimal)) and value != result:
        raise ValueError("non-integral serial identity")
    return result


def quote_snapshot(quote) -> dict:
    return {name: _scalar(_get(quote, name)) for name in QUOTE_FIELDS}


def serial_snapshot(frame) -> tuple[dict, ...]:
    """Copy columns individually: iterrows can round int64 nanoseconds to float."""
    result = []
    if isinstance(frame, (tuple, list)):
        source = ({name: row.get(name) for name in KLINE_FIELDS} for row in frame)
    else:
        if not all(name in frame.columns for name in KLINE_FIELDS):
            raise ForwardPaperError("SDK_SERIAL_COLUMNS_MISSING")
        source = ({name: frame[name].iloc[i] for name in KLINE_FIELDS} for i in range(len(frame)))
    for values in source:
        row = {name: _scalar(value) for name, value in values.items()}
        if row["datetime"] is None or row["id"] is None:
            continue
        try:
            start, identity = _serial_integer(values["datetime"]), _serial_integer(values["id"])
        except (ValueError, TypeError):
            raise ForwardPaperError("SDK_SERIAL_TIME_INVALID") from None
        if start <= 0 or identity < 0:  # SDK's empty rows are not source bars.
            continue
        row["datetime"], row["id"] = start, identity
        result.append(row)
    if len({r["datetime"] for r in result}) != len(result) or len({r["id"] for r in result}) != len(result):
        raise ForwardPaperError("SDK_SERIAL_DUPLICATE_IDENTITY")
    if any(a["datetime"] >= b["datetime"] or a["id"] >= b["id"] for a, b in zip(result, result[1:])):
        raise ForwardPaperError("SDK_SERIAL_ORDER_INVALID")
    return tuple(result)


def clock_from_binding(binding: Mapping, *, utc=time.time_ns, monotonic=time.monotonic_ns):
    """Use pinned independent UTC evidence; unknown evidence stays unknown.

    A clock file is refreshed only by making a new run binding. Reading the
    local clock never manufactures an offset or renews an old sample's age.
    """
    ref = binding.get("clock_evidence")
    evidence = None
    digest = content({"clock": "UNVERIFIED", "run_id": binding.get("run_id")})
    if ref is not None:
        evidence = parse_json_bytes(verified_file(ref).read_bytes())
        digest = ref["sha256"]
        if (evidence.get("schema_version") != SCHEMA or evidence.get("kind") != "INDEPENDENT_UTC_EVIDENCE"
                or evidence.get("independent") is not True or evidence.get("source") not in ("NTP", "PTP", "ACCEPTED_CLOCK_SERVICE")
                or evidence.get("run_id") != binding.get("run_id")
                or type(evidence.get("offset_nano")) is not int or type(evidence.get("uncertainty_ns")) is not int
                or type(evidence.get("sampled_utc_nano")) is not int or not evidence.get("original_source_sha256")):
            raise ForwardPaperError("INDEPENDENT_CLOCK_EVIDENCE_INVALID")
        from ak_trading.v2.validation.forward_paper import _hash
        _hash(evidence["original_source_sha256"])
    def sample():
        return ClockSample(utc(), monotonic(), digest,
            None if evidence is None else evidence["offset_nano"],
            None if evidence is None else evidence["uncertainty_ns"],
            None if evidence is None else evidence["sampled_utc_nano"],
            "UNKNOWN" if evidence is None else evidence["source"],
            60 * NS if evidence is None else evidence.get("max_age_ns", 60 * NS))
    return sample


def validate_runtime_binding(config: Mapping, binding: Mapping, *, activate: bool):
    status = preflight(config, binding, activate=activate)
    if not status["requested_mode_ready"]:
        raise ForwardPaperError("PREFLIGHT_BLOCKED")
    validate_runtime_inputs(config, binding)


def open_readonly_api(config: Mapping, binding: Mapping, *, activate: bool = False):
    validate_runtime_binding(config, binding, activate=activate)
    # The only credential reads in this consumer. No dotenv/env enumeration,
    # credential file, secret argv, Settings repr or exception text is emitted.
    tq_user_value = os.environ.get("TQ_USER", "").strip()
    tq_pass_value = os.environ.get("TQ_PASS", "").strip()
    if not tq_user_value or not tq_pass_value:
        raise ForwardPaperError("WAITING_AUTH")
    try:
        from ak_trading.config import Settings
        from ak_trading.session import create_api
        settings = Settings(tq_user=tq_user_value, tq_pass=tq_pass_value, default_symbol="KQ.m@SHFE.rb")
        return create_api(settings, sim_balance=1_000_000.0)
    except Exception:
        raise ForwardPaperError("SDK_START_FAILED") from None


def committed_memory_bytes() -> int:
    if os.name == "nt":
        class Counters(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
                (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage",
                    "PeakPagefileUsage", "PrivateUsage")]
        kernel, psapi = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong)
        data = Counters()
        data.cb = ctypes.sizeof(data)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(data), data.cb):
            raise ForwardPaperError("MEMORY_MEASUREMENT_FAILED")
        return data.PrivateUsage
    # Linux VmData+VmStk measures committed process data; no network/imports.
    status = Path("/proc/self/status")
    if not status.is_file():
        raise ForwardPaperError("MEMORY_MEASUREMENT_UNAVAILABLE")
    fields = {line.split(":", 1)[0]: line.split(":", 1)[1].strip() for line in status.read_text().splitlines() if ":" in line}
    return sum(int(fields[k].split()[0]) * 1024 for k in ("VmData", "VmStk"))


class ResourceGuard:
    def __init__(self, output: Path, wall_seconds: int, *, first: bool = True,
                 monotonic=time.monotonic_ns, memory=committed_memory_bytes, size=None):
        if type(wall_seconds) is not int or not 0 < wall_seconds <= (600 if first else 3600):
            raise ForwardPaperError("WALL_BUDGET_INVALID")
        self.output, self.monotonic, self.memory = Path(output), monotonic, memory
        self.started = monotonic()
        self.deadline = self.started + wall_seconds * NS
        self.size = size or (lambda: sum(p.stat().st_size for p in self.output.rglob("*") if p.is_file()))
        self.peak_memory, self.peak_output = 0, 0

    def check(self):
        used_memory, used_output = self.memory(), self.size()
        self.peak_memory, self.peak_output = max(self.peak_memory, used_memory), max(self.peak_output, used_output)
        if used_memory > MEMORY_LIMIT:
            return "MEMORY_BUDGET"
        if used_output > OUTPUT_LIMIT:
            return "OUTPUT_BUDGET"
        return "WALL_BUDGET" if self.monotonic() >= self.deadline else None


class TqReadOnlyFeed:
    """One connection, exactly four subscription calls, no account operations."""
    def __init__(self, api, host: ForwardPaperHost, guard: ResourceGuard, *, expected_symbol: str | None = None,
                 wall_clock=time.time):
        self.api, self.host, self.guard, self.wall_clock = api, host, guard, wall_clock
        self.expected_symbol, self.symbol = expected_symbol, None
        self.sequence = host.source_watermark
        self.main = self.quote = self.minute = self.daily = None
        self.prior = None
        self.closed_hashes = {}
        self.daily_hash = None
        self.initialized = False
        self.subscriptions = 0

    def _emit(self, events):
        if events:
            self.sequence += 1
            self.host.process_batch(received_batch(self.host.run_id, self.sequence, tuple(events), self.host.clock()))

    def _quote_ok(self, quote):
        if quote["ins_class"] != "FUTURE" or quote["expired"] is not False:
            raise ForwardPaperError("CONCRETE_CONTRACT_NOT_FUTURE_OR_EXPIRED")
        if self.expected_symbol is not None and self.symbol != self.expected_symbol:
            raise ForwardPaperError("CONCRETE_CONTRACT_BINDING_MISMATCH")
        for name in ("price_tick", "volume_multiple", "expire_datetime"):
            if quote[name] is None or Decimal(quote[name]) <= 0:
                raise ForwardPaperError("CONCRETE_CONTRACT_METADATA_MISSING")
        if self.host.activation is not None:
            inst = self.host.activation.bundle.instruments[0]
            if inst.raw_symbol != self.symbol:
                raise ForwardPaperError("CONCRETE_CONTRACT_BINDING_MISMATCH")
            sample = self.host.clock()
            rules = [r for r in self.host.activation.bundle.rules if r.contract_id == inst.contract_id
                     and r.available_at_nano <= sample.utc_nano and r.effective_from_nano <= sample.utc_nano < r.effective_until_nano]
            if len(rules) != 1 or (Decimal(quote["price_tick"]), Decimal(quote["volume_multiple"])) != (
                    rules[0].price_tick, rules[0].multiplier):
                raise ForwardPaperError("CONCRETE_CONTRACT_METADATA_MISMATCH")

    def _event_row(self, row, *, closed, progressed_to=0, historical=False, quote=None):
        contract = self.host.activation.contract_id if self.host.activation else self.symbol
        data = {"contract_id": contract, "raw_symbol": self.symbol, "duration_seconds": 60,
                "start_nano": row["datetime"], "sdk_id": row["id"], "open": row["open"],
                "observed_progression": True, "historical": historical}
        if closed:
            data.update({name: row[name] for name in ("high", "low", "close", "volume", "open_oi", "close_oi")})
            data["progressed_to_nano"] = progressed_to
        else:
            data["quote"] = quote
        kind = "CLOSED" if closed else "OPEN"
        identity = stable_id("tq_forward_" + kind.lower(), canonical_json_bytes([contract, row["datetime"], row["id"]]))
        return SourceEvent(identity, kind, row["datetime"], data)

    def snapshot(self):
        main = quote_snapshot(self.main)
        mapped = main["underlying_symbol"]
        if self.symbol is None:
            if not isinstance(mapped, str) or not mapped.startswith("SHFE.rb") or "@" in mapped or not mapped[7:].isdigit():
                return
            if self.expected_symbol is not None and mapped != self.expected_symbol:
                raise ForwardPaperError("CONCRETE_CONTRACT_BINDING_MISMATCH")
            self.symbol = mapped
            self.quote = self.api.get_quote(mapped)
            self.minute = self.api.get_kline_serial(mapped, 60, data_length=8192)
            self.daily = self.api.get_kline_serial(mapped, 86400, data_length=512)
            self.subscriptions += 3
            return
        if mapped != self.symbol:
            self._emit([SourceEvent(stable_id("main_roll", str(self.sequence)), "CORRECTION", 0,
                                    {"reason": "MAIN_MAPPING_CHANGED", "frozen_symbol": self.symbol, "mapped": mapped})])
            raise ForwardPaperError("MAIN_MAPPING_CHANGED")
        quote = quote_snapshot(self.quote)
        self._quote_ok(quote)
        minute, daily = serial_snapshot(self.minute), serial_snapshot(self.daily)
        if not minute:
            return
        current = minute[-1]
        if not self.initialized:
            self.initialized, self.prior = True, current
            self.closed_hashes = {str(r["datetime"]): content(r) for r in minute[:-1]}
            self.daily_hash = content(daily)
            self._emit([SourceEvent(stable_id("tq_bootstrap", self.host.run_id), "BOOTSTRAP", current["datetime"],
                {"frozen_symbol": self.symbol, "minute": minute, "daily": daily, "quote": quote,
                 "purpose": "BOOTSTRAP_ONLY", "not_forward": True})])
            return
        for row in minute[:-1]:
            old = self.closed_hashes.get(str(row["datetime"]))
            if old is not None and old != content(row):
                self._emit([SourceEvent(stable_id("tq_correction", canonical_json_bytes(row)), "CORRECTION", row["datetime"],
                                        {"original_sha256": old, "corrected": row})])
                raise ForwardPaperError("SDK_CLOSED_CORRECTION")
        if current["datetime"] < self.prior["datetime"] or current["id"] < self.prior["id"]:
            raise ForwardPaperError("SDK_SERIAL_REVERSED")
        events = []
        if current["datetime"] == self.prior["datetime"]:
            if current["id"] != self.prior["id"] or current["open"] != self.prior["open"]:
                raise ForwardPaperError("SDK_OPEN_CORRECTION")
        else:
            gap = current["id"] != self.prior["id"] + 1
            completed = [r for r in minute[:-1] if self.prior["datetime"] <= r["datetime"] < current["datetime"]]
            if not completed or completed[0]["datetime"] != self.prior["datetime"]:
                gap = True
            events.append(self._event_row(current, closed=False, historical=gap, quote=quote))
            events.extend(self._event_row(r, closed=True, progressed_to=current["datetime"], historical=gap) for r in completed)
            if gap:
                events.append(SourceEvent(stable_id("tq_gap", str(self.sequence)), "HEALTH", current["datetime"],
                    {"connected": False, "reason": "SDK_SERIAL_GAP", "previous_id": self.prior["id"], "current_id": current["id"]}))
            self.closed_hashes.update({str(r["datetime"]): content(r) for r in completed})
            self.prior = current
        if content(daily) != self.daily_hash:
            # Current daily remains explicitly unconsumed research raw data.
            events.append(SourceEvent(stable_id("tq_daily_raw", content(daily)), "BOOTSTRAP", current["datetime"],
                {"daily": daily, "purpose": "DAILY_RAW_NOT_ACTIVATED", "not_forward": True}))
            self.daily_hash = content(daily)
        self._emit(events)

    def run(self) -> tuple[int, str]:
        reason, native = "WALL_BUDGET", 0
        try:
            self.main = self.api.get_quote("KQ.m@SHFE.rb")
            self.subscriptions = 1
            while True:
                exhausted = self.guard.check()
                if exhausted:
                    reason = exhausted
                    break
                # The real SDK's pump is bounded independently of quote ticks.
                changed = self.api.wait_update(deadline=self.wall_clock() + 1.0)
                # Getters can populate initial references without another update.
                if changed or not self.initialized:
                    self.snapshot()
                if not changed:
                    self._emit([SourceEvent(stable_id("tq_clock", str(self.sequence)), "CLOCK", 0,
                                            {"wait_update_timeout": True})])
                exhausted = self.guard.check()
                if exhausted:
                    reason = exhausted
                    break
        except KeyboardInterrupt:
            reason, native = "INTERRUPTED", 130
        except Exception as exc:
            reason = str(exc) if isinstance(exc, ForwardPaperError) else "SDK_PUMP_FAILED"
            native = 2
        finally:
            pump_native = native
            close_status = "CLOSED"
            try:
                self.api.close()
            except Exception:
                close_status, native = "SDK_CLOSE_FAILED", 3
            self.runtime_result = {"kind": "RUNTIME_RESULT", "native_result": native,
                "pump_native_result": pump_native, "reason": reason, "close_status": close_status,
                "subscriptions": self.subscriptions, "tqapi_connections": 1,
                "peak_committed_memory_bytes": self.guard.peak_memory,
                "output_bytes_before_terminal": self.guard.peak_output,
                "memory_limit_bytes": MEMORY_LIMIT, "output_limit_bytes": OUTPUT_LIMIT,
                "terminal_persistence": "NOT_ATTEMPTED_RECOVERY_REQUIRED" if self.host.poisoned else "ATTEMPTED"}
            if self.host.poisoned and native == 0:
                native = 4
                self.runtime_result["native_result"] = native
            if not self.host.poisoned:
                self.host.facts.append(dict(self.runtime_result))
                try:
                    report = self.host.finish(reason if close_status == "CLOSED" else reason + "/SDK_CLOSE_FAILED")
                except Exception as exc:
                    self.runtime_result["terminal_persistence"] = "FAILED"
                    self.runtime_result["terminal_persistence_reason"] = (
                        str(exc) if isinstance(exc, ForwardPaperError) else "TERMINAL_PERSISTENCE_FAILED")
                    if native == 0:
                        native = 4
                    self.runtime_result["native_result"] = native
                else:
                    self.runtime_result["terminal_persistence"] = "SEALED"
                    self.runtime_result["report"] = str(report)
        return native, reason
