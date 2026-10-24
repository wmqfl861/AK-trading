"""Causal, source-bound forward paper consumer. No broker or SDK order surface.

The local envelope deliberately is not an M1 schema. Financial records retain
their original codecs and the ContinuousLedger remains the only financial book.
An intent is executable only after its immutable S segment and subsequent C
receipt have both been durably published. Capture is useful without activation.
"""
from __future__ import annotations

import ctypes
import inspect
import json
import math
import os
import sys
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

from ak_trading.v2.artifacts import canonical_json_bytes, sha256_hex, stable_id
from ak_trading.v2.contracts import (
    AccountSnapshot, CalibrationEvidence, DataQuality, DecisionRecord,
    ExecutionResult, ExitPolicyBinding, ExitSpec, Fill, FrozenInput,
    GateDecision, Horizon, LedgerCheckpoint, MarketBar, OrderIntent, RunPolicy,
    StrategySpec, StrategyState,
)
from ak_trading.v2.data import read_frozen_input, visible_rule
from ak_trading.v2.decision.execution import (
    capacity_before, position_for, rule_at, simulate_execution, simulate_protective_exit,
)
from ak_trading.v2.decision.gates import evaluate_gate
from ak_trading.v2.decision.ledger import ContinuousLedger, record_hash
from ak_trading.v2.decision.payoffs import (
    PathPayoffModel, PayoffObservation, estimate_proposal_payoff,
)
from ak_trading.v2.decision.pipeline import (
    build_strategy_context, exit_registry_hash_for, legacy_exit_binding,
    model_hashes_for, registry_hash_for,
)
from ak_trading.v2.decision.replay import (
    _hard_exit, apply_receipt, intents_for_proposal,
    verify_committed_prefix, verify_restore_sources,
)
from ak_trading.v2.decision.risk import enforce_account_risk
from ak_trading.v2.decision.routing import choose_proposal, opportunity_id
from ak_trading.v2.policy import research_policy
from ak_trading.v2.probability.forecast import build_forecast_requests, predict_market, publish_market_forecast
from ak_trading.v2.probability.models import ForecastModelSet
from ak_trading.v2.probability.regimes import RegimeReference
from ak_trading.v2.schemas import decode_record, encode_record, parse_json_bytes
from ak_trading.v2.strategies.registry import build_strategy_spec, strategy_registry_v2
from ak_trading.v2.strategies.state import commit_observation, commit_selected_proposal

SCHEMA = "ak_forward_paper_host_v1"
NS = 1_000_000_000
MINUTE = 60 * NS
MAX_OPEN_AGE = 5 * NS
MAX_MARKET_AGE = 120 * NS
MAX_CLOCK_ERROR = 100_000_000
OUTPUT_LIMIT = 1024 ** 3
TERMINAL_OUTPUT_RESERVE = 64 * 1024
POLICIES = ("BEST_EV", "PROBABILITY_SAMPLE", "FIXED", "CASH")
STRATEGIES = ("trend_m1_v2", "breakout_m1_v2", "pullback_m1_v2")
ZERO = Decimal(0)


class ForwardPaperError(ValueError):
    """Fixed public error codes; never include SDK exceptions or credentials."""


def content(value) -> str:
    return sha256_hex(canonical_json_bytes(value))


def _integer(value, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ForwardPaperError(name + "_INVALID")
    return value


def _hash(value, name: str = "HASH") -> str:
    if not isinstance(value, str) or len(value) != 64 or set(value) - set("0123456789abcdef"):
        raise ForwardPaperError(name + "_INVALID")
    return value


def verified_file(ref: Mapping) -> Path:
    if not isinstance(ref, Mapping) or set(ref) != {"path", "bytes", "sha256"}:
        raise ForwardPaperError("FILE_REF_INVALID")
    path = Path(ref["path"])
    # Bindings are untrusted inputs. Reject secret surfaces before any read.
    if path.name.lower().startswith(".env") or path.suffix.lower() in (".pem", ".key") or any(
            part.lower() in ("credentials", ".ssh", ".aws") for part in path.parts):
        raise ForwardPaperError("CREDENTIAL_PATH_FORBIDDEN")
    if path.is_symlink() or not path.is_file():
        raise ForwardPaperError("REQUIRED_INPUT_MISSING")
    raw = path.read_bytes()
    if len(raw) != ref["bytes"] or sha256_hex(raw) != _hash(ref["sha256"]):
        raise ForwardPaperError("INPUT_PIN_MISMATCH")
    return path


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _plain(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    return value


@dataclass(frozen=True)
class ClockSample:
    raw_utc_nano: int
    monotonic_ns: int
    evidence_sha256: str
    offset_nano: int | None = None  # local UTC minus independent UTC
    uncertainty_ns: int | None = None
    sampled_utc_nano: int | None = None
    source: str = "UNKNOWN"
    max_age_ns: int = 60 * NS

    def __post_init__(self):
        _integer(self.raw_utc_nano, "UTC")
        _integer(self.monotonic_ns, "MONOTONIC")
        _hash(self.evidence_sha256, "CLOCK_EVIDENCE")
        _integer(self.max_age_ns, "CLOCK_MAX_AGE", 1)
        if self.offset_nano is not None and type(self.offset_nano) is not int:
            raise ForwardPaperError("CLOCK_OFFSET_INVALID")
        for name in ("uncertainty_ns", "sampled_utc_nano"):
            if getattr(self, name) is not None:
                _integer(getattr(self, name), name)

    @property
    def utc_nano(self) -> int:
        return self.raw_utc_nano - (self.offset_nano or 0)

    def reasons(self, previous: ClockSample | None = None) -> tuple[str, ...]:
        reasons = []
        if self.offset_nano is None or self.uncertainty_ns is None or self.sampled_utc_nano is None or self.source == "UNKNOWN":
            reasons.append("CLOCK_UNVERIFIED")
        else:
            if abs(self.offset_nano) + self.uncertainty_ns > MAX_CLOCK_ERROR:
                reasons.append("CLOCK_ERROR_EXCEEDED")
            if not 0 <= self.raw_utc_nano - self.sampled_utc_nano <= self.max_age_ns:
                reasons.append("CLOCK_EVIDENCE_EXPIRED")
        if self.utc_nano < 0:
            reasons.append("CLOCK_UNVERIFIED")
        if previous is not None:
            elapsed = self.monotonic_ns - previous.monotonic_ns
            if elapsed < 0 or self.utc_nano < previous.utc_nano:
                reasons.append("CLOCK_REVERSED")
            if abs((self.raw_utc_nano - previous.raw_utc_nano) - elapsed) > MAX_CLOCK_ERROR:
                reasons.append("CLOCK_JUMP")
        return tuple(sorted(set(reasons)))

    def payload(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class SourceEvent:
    event_id: str
    kind: str
    source_nano: int
    data: Mapping

    def __post_init__(self):
        if not isinstance(self.event_id, str) or not self.event_id:
            raise ForwardPaperError("EVENT_ID_INVALID")
        if self.kind not in ("BOOTSTRAP", "OPEN", "CLOSED", "CLOCK", "HEALTH", "CORRECTION"):
            raise ForwardPaperError("EVENT_KIND_INVALID")
        _integer(self.source_nano, "SOURCE_TIME")
        # Copy through canonical JSON: no live SDK entities or mutable nested aliases.
        object.__setattr__(self, "data", _freeze(parse_json_bytes(canonical_json_bytes(_plain(self.data)))))

    def payload(self) -> dict:
        return {"event_id": self.event_id, "kind": self.kind,
                "source_nano": self.source_nano, "data": _plain(self.data)}


@dataclass(frozen=True)
class ReceivedBatch:
    run_id: str
    source_sequence: int
    events: tuple[SourceEvent, ...]
    clock: ClockSample
    payload_sha256: str

    def __post_init__(self):
        _integer(self.source_sequence, "SOURCE_SEQUENCE", 1)
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ForwardPaperError("RUN_ID_INVALID")
        if not isinstance(self.events, tuple) or any(not isinstance(e, SourceEvent) for e in self.events):
            raise ForwardPaperError("EVENTS_INVALID")
        if self.payload_sha256 != content([e.payload() for e in self.events]):
            raise ForwardPaperError("BATCH_PAYLOAD_HASH_MISMATCH")

    def payload(self) -> dict:
        return {"run_id": self.run_id, "source_sequence": self.source_sequence,
                "payload_sha256": self.payload_sha256, "clock": self.clock.payload(),
                "events": [e.payload() for e in self.events]}


def received_batch(run_id: str, sequence: int, events, clock: ClockSample) -> ReceivedBatch:
    rows = tuple(events)
    return ReceivedBatch(run_id, sequence, rows, clock, content([e.payload() for e in rows]))


def _write_file(path: Path, raw: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def _publish(source: Path, target: Path, *, replace_existing: bool = False) -> None:
    """Same-volume publication: files fsync; Windows write-through rename.

    POSIX additionally fsyncs the parent directory. Windows uses MoveFileExW
    with WRITE_THROUGH and checks its native result; no ordinary close is called
    durable. Failure leaves the old committed chain or an auditable orphan.
    """
    if target.exists() and not replace_existing:
        raise FileExistsError(target)
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        move = kernel.MoveFileExW
        move.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
        move.restype = ctypes.c_int
        if not move(str(source), str(target), 8 | (1 if replace_existing else 0)):
            raise OSError(ctypes.get_last_error(), "DURABLE_PUBLICATION_FAILED")
    else:
        os.replace(source, target)
        descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class Journal:
    """Append-only financial/control segments; manifest commits, HEAD is a hint."""

    def __init__(self, root: Path, identity: Mapping, *, create: bool = True,
                 fault: Callable[[str], None] | None = None, output_limit_bytes: int = OUTPUT_LIMIT):
        self.root = Path(root)
        self.output_limit_bytes = _integer(output_limit_bytes, "OUTPUT_LIMIT", 1)
        self.fault = fault or (lambda _point: None)
        self.identity = dict(identity)
        self.sequence = 0
        self.parent = content(self.identity)
        self.segments: list[tuple[dict, dict]] = []
        self.orphans: list[str] = []
        if create:
            self.root.mkdir(parents=True, exist_ok=False)
            (self.root / "segments").mkdir()
            (self.root / "reports").mkdir()
            self._write(self.root / "IDENTITY.json", canonical_json_bytes(self.identity))
        else:
            if self.root.is_symlink() or not self.root.is_dir():
                raise ForwardPaperError("RUN_DIRECTORY_MISSING")
            if parse_json_bytes((self.root / "IDENTITY.json").read_bytes()) != self.identity:
                raise ForwardPaperError("RUN_IDENTITY_MISMATCH")
            self._read_chain()

    def _write(self, path: Path, raw: bytes):
        # The receive/checkpoint/report writer is the single output owner. Check
        # before each exclusive write, including temporary files and orphan tails,
        # so one large seal cannot overshoot the process's output budget.
        current = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        # Keep a small, fixed part of the original cap for the external
        # supervisor's detached terminal receipt if the worker cannot seal.
        reserve = min(TERMINAL_OUTPUT_RESERVE, self.output_limit_bytes // 16)
        if current + len(raw) > self.output_limit_bytes - reserve:
            raise ForwardPaperError("OUTPUT_BUDGET")
        _write_file(path, raw)

    def _read_chain(self):
        for path in sorted((self.root / "segments").iterdir()):
            if path.name.startswith(".pending-"):
                self.orphans.append(path.name)
                continue
            if path.is_symlink() or not path.is_dir() or not path.name.isdigit():
                raise ForwardPaperError("UNDECLARED_SEGMENT_PATH")
            manifest_path = path / "manifest.json"
            if not manifest_path.is_file() or manifest_path.is_symlink():
                raise ForwardPaperError("COMMITTED_MANIFEST_MISSING")
            raw = manifest_path.read_bytes()
            manifest = parse_json_bytes(raw)
            if (manifest.get("schema_version") != SCHEMA or manifest.get("sequence") != self.sequence + 1
                    or int(path.name) != manifest["sequence"] or manifest.get("parent_sha256") != self.parent
                    or manifest.get("identity_sha256") != content(self.identity)):
                raise ForwardPaperError("SEGMENT_CHAIN_MISMATCH")
            files = manifest.get("files")
            if not isinstance(files, dict) or set(files) != {"EVENT.json", "HOST.json", "RECORDS.json", "CHECKPOINTS.json"}:
                raise ForwardPaperError("SEGMENT_FILE_SET_MISMATCH")
            if {p.name for p in path.iterdir()} != {*files, "manifest.json"}:
                raise ForwardPaperError("UNDECLARED_SEGMENT_FILE")
            decoded = {}
            for name, pin in files.items():
                member = path / name
                if member.is_symlink() or not member.is_file():
                    raise ForwardPaperError("SEGMENT_MEMBER_MISSING")
                payload = member.read_bytes()
                if len(payload) != pin["bytes"] or sha256_hex(payload) != pin["sha256"]:
                    raise ForwardPaperError("SEGMENT_MEMBER_HASH_MISMATCH")
                decoded[name] = parse_json_bytes(payload)
            self.sequence += 1
            self.parent = sha256_hex(raw)
            self.segments.append(({**manifest, "manifest_sha256": self.parent}, decoded))

    def seal(self, kind: str, event: Mapping, host: Mapping, records: Mapping,
             checkpoints: Mapping) -> dict:
        sequence = self.sequence + 1
        temporary = self.root / "segments" / (".pending-" + uuid.uuid4().hex)
        temporary.mkdir(exist_ok=False)
        payloads = parse_json_bytes(canonical_json_bytes({"EVENT.json": dict(event), "HOST.json": dict(host),
                    "RECORDS.json": dict(records), "CHECKPOINTS.json": dict(checkpoints)}))
        pins = {}
        for name, value in payloads.items():
            raw = canonical_json_bytes(value)
            self._write(temporary / name, raw)
            pins[name] = {"bytes": len(raw), "sha256": sha256_hex(raw)}
        self.fault("payloads_fsynced")
        manifest = {"schema_version": SCHEMA, "sequence": sequence, "kind": kind,
                    "parent_sha256": self.parent, "identity_sha256": content(self.identity),
                    "files": pins, "storage": "os.fsync+MoveFileExW_WRITE_THROUGH" if os.name == "nt"
                    else "os.fsync+same_volume_replace+parent_fsync"}
        raw_manifest = canonical_json_bytes(manifest)
        self._write(temporary / "manifest.json", raw_manifest)
        self.fault("manifest_fsynced")
        _publish(temporary, self.root / "segments" / f"{sequence:08d}")
        self.sequence, self.parent = sequence, sha256_hex(raw_manifest)
        result = {**manifest, "manifest_sha256": self.parent}
        self.segments.append((result, payloads))
        self.fault("segment_published")
        pointer = self.root / (".HEAD-" + uuid.uuid4().hex)
        self._write(pointer, canonical_json_bytes({"sequence": sequence, "manifest_sha256": self.parent}))
        self.fault("before_head")
        _publish(pointer, self.root / "HEAD.json", replace_existing=True)
        self.fault("head_published")
        return result

    def report(self, report: Mapping) -> Path:
        report = _plain(report)
        existing = list((self.root / "reports").glob("r*"))
        revision = len(existing) + 1
        temporary = self.root / "reports" / (".pending-" + uuid.uuid4().hex)
        temporary.mkdir(exist_ok=False)
        destination = temporary / "REPORT.json"
        self._write(destination, canonical_json_bytes(dict(report)))
        markdown = ("# Forward paper research\n\n"
                    f"Run: {report['run_id']}\n\n"
                    f"Capture: {report['capture_status']}; strategy: {report['strategy_evaluation']}; "
                    f"paper account: {report['paper_account']}.\n\n"
                    f"Forward closed bars: {report['forward_bar_count']}.\n\n"
                    "```json\n" + json.dumps(dict(report), ensure_ascii=False, indent=2) + "\n```\n")
        self._write(temporary / "REPORT.md", markdown.encode("utf-8"))
        self._write(temporary / "manifest.json", canonical_json_bytes({"schema_version": SCHEMA,
            "sealed_sequence": self.sequence, "sealed_manifest_sha256": self.parent,
            "files": {name: {"bytes": (temporary / name).stat().st_size,
                              "sha256": sha256_hex((temporary / name).read_bytes())}
                      for name in ("REPORT.json", "REPORT.md")}}))
        final = self.root / "reports" / f"r{revision:06d}"
        _publish(temporary, final)
        return final / "REPORT.json"


def report_from_prefix(journal: Journal, *, host=None, records=None, checkpoints=None) -> dict:
    """Report the sealed prefix without importing an SDK or restoring finances.

    The running host supplies its exact current view; the offline command uses
    only sealed members. Both paths share the same axes and null semantics.
    Financial peaks are measured from the original committed checkpoints.
    """
    if not journal.segments:
        raise ForwardPaperError("EMPTY_COMMITTED_PREFIX")
    latest = journal.segments[-1][1]
    host = latest["HOST.json"] if host is None else host
    checkpoints = latest["CHECKPOINTS.json"] if checkpoints is None else checkpoints
    if records is None:
        records = {mode: {name: [] for name in rows} for mode, rows in latest["RECORDS.json"].items()}
        for _, segment in journal.segments:
            for mode, rows in segment["RECORDS.json"].items():
                for name, values in rows.items():
                    records[mode][name].extend(values)
    def metric(values):
        values = sorted(values)
        return {"n": len(values), "p99_nano": None if not values else values[math.ceil(.99 * len(values)) - 1],
                "status": "INSUFFICIENT" if not values else "DEFINED"}
    accounts, remaining = {}, {}
    for mode, cp in checkpoints.items():
        a, rows, control = cp["account"], records[mode], host["accounts"][mode]
        samples = [segment["CHECKPOINTS.json"][mode]["account"] for _, segment in journal.segments
                   if mode in segment["CHECKPOINTS.json"]] + [a]
        funnel, families = {}, {name: {"evaluations": 0, "actions": {}, "gate_reasons": {}} for name in STRATEGIES}
        for decision in rows["decisions"]:
            gates = {g["proposal_id"]: g for g in decision["gates"]}
            for proposal in decision["proposals"]:
                family = families[proposal["strategy_id"]]
                family["evaluations"] += 1
                action = proposal["action"]
                family["actions"][action] = family["actions"].get(action, 0) + 1
                for reason in gates[proposal["proposal_id"]]["reasons"]:
                    family["gate_reasons"][reason] = family["gate_reasons"].get(reason, 0) + 1
                    funnel[reason] = funnel.get(reason, 0) + 1
        admissions = control["admission"].values()
        remaining[mode] = {"lots": sum(l["lots"] for l in a["lots"]),
            "pending": sorted(i["intent_id"] for i in cp["pending_intents"]),
            "exit_debt": control["debt"], "protective_barrier": control["barrier"] is not None}
        accounts[mode] = {"account": a, "policy": next(p for p in journal.identity["activation"]["policies"]
                                                        if p["routing_mode"] == mode),
            "decisions": len(rows["decisions"]), "sealed_intents": len(rows["intents"]),
            "confirmed": sum(v["status"] == "CONFIRMED_ELIGIBLE" for v in admissions),
            "unconfirmed_cancelled": sum(v["status"] == "UNCONFIRMED_AFTER_CRASH" for v in admissions),
            "missed_commit_window": sum(v["status"] == "MISSED_COMMIT_WINDOW" for v in admissions),
            "virtual_fills": len(rows["fills"]), "executions": len(rows["executions"]),
            "pending_unfilled": len(cp["pending_intents"]), "family_funnel": families,
            "gate_rejection_funnel": funnel, "reconciliation": control["reconciliation"],
            "financial_summary": {"net_equity_cny": a["equity_cny"], "fees_cny": a["fees_cny"],
                "slippage_cny": a["slippage_cny"], "day_net_cny": a["day_net_cny"],
                "inventory_peak_lots": max(sum(l["lots"] for l in x["lots"]) for x in samples),
                "used_margin_peak_cny": str(max(Decimal(x["margin_cny"]) for x in samples)),
                "reserved_margin_peak_cny": str(max(Decimal(x["reserved_margin_cny"]) for x in samples)),
                "total_margin_peak_cny": str(max(Decimal(x["margin_cny"]) + Decimal(x["reserved_margin_cny"]) for x in samples)),
                "maximum_day_loss_cny": str(max(ZERO, *(max(ZERO, -Decimal(x["day_net_cny"])) for x in samples))),
                "maximum_drawdown_cny": str(max(Decimal(x["peak_equity_cny"]) - Decimal(x["equity_cny"]) for x in samples)),
                "maximum_drawdown_ratio": str(max((Decimal(x["peak_equity_cny"]) - Decimal(x["equity_cny"]))
                    / Decimal(x["peak_equity_cny"]) for x in samples)), "peak_source": "COMMITTED_LEDGER_CHECKPOINTS"},
            "status": "INCOMPLETE_EXIT/RECOVERY_REQUIRED" if control["barrier"] is not None else
                      "INCOMPLETE" if a["lots"] or cp["pending_intents"] else "COMPLETE"}
    identity, facts = journal.identity, host["facts"]
    live = [f for f in facts if f.get("role") == "FOREGROUND_LIVE" and f["kind"] == "PUBLICATION"]
    forward = [f for f in facts if f["kind"] == "FORWARD_CLOSED"]
    observed = [f["received"]["raw_utc_nano"] for f in forward]
    clock = None if host["last_clock"] is None else ClockSample(**host["last_clock"])
    return {"schema_version": SCHEMA, "run_id": identity["run_id"], "purpose": identity["purpose"],
        "experiment_type": identity["experiment_type"], "final_holdout": False, "synthetic": identity["synthetic"],
        "engineering_hard_review": "NOT_RUN", "milestones": {"IMPLEMENTED_HARD_PASS": "NOT_RUN",
            "REAL_FEED_CAPTURE_STARTED": not identity["synthetic"] and host["forward_bar_count"] > 0,
            "REAL_PAPER_EVALUATED": not identity["synthetic"] and host["strategy_batches"] > 0,
            "REAL_PAPER_VIRTUAL_FILL_OBSERVED": not identity["synthetic"] and any(a["virtual_fills"] for a in accounts.values())},
        "capture_status": "CAPTURED_FORWARD" if host["forward_bar_count"] else "NO_FORWARD_BAR",
        "run_status": host["terminal_reason"] or "RUNNING", "terminal_reason": host["terminal_reason"],
        "clock_status": "UNKNOWN" if clock is None else "UNVERIFIED"
            if clock.reasons() or "CLOCK_UNVERIFIED" in host["pauses"] else "VERIFIED",
        "warmup_status": "NOT_BOUND" if identity["activation"] is None else "BOUND_ORIGINAL_SOURCE",
        "input_status": "WAITING_INPUT/NO_TRADE" if identity["activation"] is None else "BOUND",
        "strategy_evaluation": "NOT_RUN" if not host["strategy_batches"] else "EVALUATED",
        "paper_account": "NOT_ACTIVATED" if not checkpoints else "ACTIVATED", "activation": identity["activation"],
        "forward_bar_count": host["forward_bar_count"], "strategy_batches": host["strategy_batches"],
        "forward_source_interval": None if not forward else
            [min(f["source_start_nano"] for f in forward), max(f["source_end_nano"] for f in forward)],
        "observed_interval": None if not observed else [min(observed), max(observed)],
        "source_watermark": host["source_watermark"], "source_chain": host["source_chain"],
        "coverage": {name: sum(f["kind"] == name for f in facts) for name in
            ("SOURCE_GAP", "DUPLICATE_BATCH", "DUPLICATE_EVENT", "SOURCE_EVENT_CONFLICT", "SOURCE_SEQUENCE_CONFLICT",
             "SOURCE_CORRECTION", "RECONNECTED", "CLOSED_REJECTED", "CLOSED_OUT_OF_ORDER")},
        "accounts": accounts, "remaining_risk": remaining, "pauses": host["pauses"], "last_clock": host["last_clock"],
        "publication_latency": metric([f["published_at_nano"] - f["decision_nano"] for f in live]),
        "publication_timeliness": {"n": len(live), "rate": None if not live else
            sum(f["published_at_nano"] < f["window_start_nano"] for f in live) / len(live),
            "status": "INSUFFICIENT" if not live else "DEFINED"},
        "publication_not_published": [f for f in facts if f["kind"] == "PUBLICATION_NOT_PUBLISHED"],
        "commit_completion": [s["EVENT.json"] for m, s in journal.segments if m["kind"] == "COMMIT_C"],
        "execution_times": [f for f in facts if f["kind"] in ("EXECUTION_RECEIPT", "PROTECTIVE_RECEIPT")],
        "background_publications": [f for f in facts if f.get("role") == "BACKGROUND_RECONSTRUCTED"],
        "runtime_results": [f for f in facts if f["kind"] == "RUNTIME_RESULT"],
        "sealed_sequence": journal.sequence, "sealed_manifest_sha256": journal.parent,
        "not_run": {key: "NOT_RUN" for key in ("other_five_cost_scenarios", "DAILY", "eight_horizons", "all_products",
            "full_AB", "ordinary_SH", "SHADOW_ACCEPTED", "PIT_OOS_qualification", "twenty_trading_days")},
        "real_send_count": 0, "real_cancel_count": 0, "sdk_order_calls": 0,
        "broker": "NOT_RUN", "observer": "hold", "enable_trading": False}


@dataclass(frozen=True)
class Activation:
    bundle: FrozenInput
    models: ForecastModelSet
    payoff: PathPayoffModel
    reference: RegimeReference
    specs: tuple[StrategySpec, ...]
    exit_specs: tuple[ExitSpec, ...]
    bindings: tuple[ExitPolicyBinding, ...]
    run_policies: tuple[RunPolicy, ...]
    contract_id: str
    activation_nano: int
    contract_lock_sha256: str
    source_pins: Mapping[str, str]
    synthetic: bool
    initial_equity_cny: Decimal = Decimal("1000000")
    seasonality: object | None = None

    def __post_init__(self):
        _integer(self.activation_nano, "ACTIVATION")
        _hash(self.contract_lock_sha256, "CONTRACT_LOCK")
        if type(self.synthetic) is not bool or not isinstance(self.initial_equity_cny, Decimal) or not self.initial_equity_cny.is_finite() or self.initial_equity_cny <= 0:
            raise ForwardPaperError("ACTIVATION_INVALID")
        if {s.strategy_id for s in self.specs} != set(STRATEGIES) or len(self.specs) != 3:
            raise ForwardPaperError("EXACT_THREE_STRATEGIES_REQUIRED")
        for spec in self.specs:
            if spec != build_strategy_spec(spec.family, "MINUTE") or spec.max_holding != Horizon("TRADING_MINUTE", 30):
                raise ForwardPaperError("STRATEGY_SPEC_IDENTITY_MISMATCH")
        expected = tuple(sorted((legacy_exit_binding(s) for s in self.specs), key=lambda b: b.binding_sha256))
        if tuple(sorted(self.bindings, key=lambda b: b.binding_sha256)) != expected:
            raise ForwardPaperError("EXIT_BINDINGS_MUST_MATCH_EXACTLY")
        if len({s.exit_policy_id for s in self.exit_specs}) != len(self.exit_specs):
            raise ForwardPaperError("EXIT_REGISTRY_DUPLICATE")
        from ak_trading.v2.exits.registry import exit_registry_specs
        full = {s.exit_policy_id: s for mode in ("MINUTE", "DAILY") for s in exit_registry_specs(mode)}
        if {s.exit_policy_id: s for s in self.exit_specs} != full:
            raise ForwardPaperError("COMPLETE_EXIT_REGISTRY_REQUIRED")
        if len(self.run_policies) != 4 or {p.routing_mode for p in self.run_policies} != set(POLICIES):
            raise ForwardPaperError("EXACT_FOUR_POLICIES_REQUIRED")
        seeds = {p.seed for p in self.run_policies}
        if len(seeds) != 1:
            raise ForwardPaperError("COMMON_PREDECLARED_SEED_REQUIRED")
        trend = next(b for b in self.bindings if b.strategy_id == "trend_m1_v2")
        for policy in self.run_policies:
            if policy.purpose != "RESEARCH_EVALUATION" or policy.cost_scenario != "BASE" or policy.research_policy_version != research_policy()["policy_version"]:
                raise ForwardPaperError("RUN_POLICY_MISMATCH")
            if policy.routing_mode == "FIXED" and (policy.fixed_strategy_id != "trend_m1_v2" or policy.fixed_exit_binding_sha256 != trend.binding_sha256):
                raise ForwardPaperError("FIXED_BINDING_MISMATCH")
        if set(self.run_policies) != set(default_policies(self.specs, next(iter(seeds)))):
            raise ForwardPaperError("ORIGINAL_RUN_POLICIES_REQUIRED")
        instruments = [i for i in self.bundle.instruments if i.contract_id == self.contract_id]
        if len(instruments) != 1 or len(self.bundle.instruments) != 1 or instruments[0].product != "SHFE.RB" or not instruments[0].raw_symbol.startswith("SHFE.rb"):
            raise ForwardPaperError("SINGLE_CONCRETE_RB_REQUIRED")
        if "@" in instruments[0].raw_symbol or instruments[0].quality.status != "READY":
            raise ForwardPaperError("INSTRUMENT_NOT_READY")
        for cutoff in (instruments[0].expiry_nano, instruments[0].last_trade_nano, instruments[0].delivery_cutoff_nano):
            if cutoff is None or cutoff <= self.activation_nano:
                raise ForwardPaperError("INSTRUMENT_LIFECYCLE_UNKNOWN")
        if not self.bundle.sessions or any(s.evidence_status == "UNKNOWN" for s in self.bundle.sessions):
            raise ForwardPaperError("CALENDAR_REQUIRED")
        activation_rules = [r for r in self.bundle.rules if r.contract_id == self.contract_id
                            and r.available_at_nano <= self.activation_nano
                            and r.effective_from_nano <= self.activation_nano < r.effective_until_nano]
        if len(activation_rules) != 1:
            raise ForwardPaperError("CURRENT_RULE_REQUIRED")
        r = activation_rules[0]
        if r.evidence == "UNKNOWN" or any(v is None for v in (r.open_fee, r.close_fee, r.close_today_fee,
                r.long_margin_rate, r.short_margin_rate, r.limit_up, r.limit_down)):
            raise ForwardPaperError("COMPLETE_CURRENT_RULE_REQUIRED")
        from ak_trading.v2.clock import horizon_window
        try:
            _, holding_end = horizon_window(self.activation_nano, "SHFE.RB", Horizon("TRADING_MINUTE", 30), self.bundle.sessions)
        except ValueError:
            raise ForwardPaperError("HOLDING_CALENDAR_REQUIRED") from None
        for window in self.bundle.sessions:
            start, end = max(self.activation_nano, window.start_nano), min(holding_end, window.end_nano)
            if start >= end:
                continue
            for when in (start, end - 1):
                visible = rule_at(self.bundle.rules, self.contract_id, when)
                if visible is None or visible.evidence == "UNKNOWN" or any(v is None for v in (
                        visible.open_fee, visible.close_fee, visible.close_today_fee, visible.long_margin_rate,
                        visible.short_margin_rate, visible.limit_up, visible.limit_down)):
                    raise ForwardPaperError("HOLDING_WINDOW_RULE_REQUIRED")
        if not self.synthetic and (any(s.evidence_status != "VERIFIED" for s in self.bundle.sessions) or any(r.evidence != "VERIFIED" for r in self.bundle.rules)):
            raise ForwardPaperError("REAL_SOURCE_VERIFICATION_REQUIRED")
        if not self.models.models or not self.models.evidence or not self.payoff.observations or not self.reference.volatility_samples:
            raise ForwardPaperError("MODEL_PACK_INCOMPLETE")
        if self.models.model_set_id != "forecast_model_set:" + self.models.content_hash() or self.reference.reference_id != "regime_reference:" + self.reference.content_hash():
            raise ForwardPaperError("MODEL_CONTENT_IDENTITY_MISMATCH")
        if not (self.models.training_end_nano < self.models.calibration_end_nano < self.activation_nano < self.models.valid_until_nano
                and self.payoff.training_end_nano < self.activation_nano and self.reference.available_at_nano < self.activation_nano):
            raise ForwardPaperError("MODEL_NOT_AVAILABLE_AT_ACTIVATION")
        model_hashes_for(self.models, self.payoff, self.reference, self.seasonality)
        for digest in self.source_pins.values():
            if _hash(digest) == "0" * 64:
                raise ForwardPaperError("ZERO_SOURCE_HASH_FORBIDDEN")

    @property
    def identity(self) -> dict:
        return {"contract_id": self.contract_id, "activation_nano": self.activation_nano,
                "lock": self.contract_lock_sha256, "registry": registry_hash_for(self.specs),
                "R": exit_registry_hash_for(self.exit_specs),
                "B": [encode_record(b) for b in sorted(self.bindings, key=lambda b: b.binding_sha256)],
                "models": dict(model_hashes_for(self.models, self.payoff, self.reference, self.seasonality)),
                "source_pins": dict(self.source_pins), "bundle_manifest": self.bundle.manifest_sha256,
                "instrument": [encode_record(i) for i in self.bundle.instruments],
                "calendar": [record_hash(s) for s in self.bundle.sessions],
                "rules": [record_hash(r) for r in self.bundle.rules],
                "policies": [encode_record(p) for p in sorted(self.run_policies, key=lambda p: p.routing_mode)],
                "initial_equity_cny": str(self.initial_equity_cny), "synthetic": self.synthetic}


def default_policies(specs: tuple[StrategySpec, ...], seed: int = 7) -> tuple[RunPolicy, ...]:
    trend = legacy_exit_binding(next(s for s in specs if s.strategy_id == "trend_m1_v2"))
    return tuple(RunPolicy("RESEARCH_EVALUATION", mode, seed,
                           "trend_m1_v2" if mode == "FIXED" else None,
                           trend.binding_sha256 if mode == "FIXED" else None,
                           research_policy()["policy_version"], "macro-risk-v2.1.0", "PRICE_ONLY", "BASE")
                 for mode in POLICIES)


def live_clock_available() -> bool:
    registry = strategy_registry_v2("MINUTE")
    return all("checked_at_nano" in inspect.signature(registry[name].evaluate).parameters for name in STRATEGIES)


def load_activation(path: Path, *, allow_synthetic: bool = False) -> Activation:
    """Decode pinned original producer artifacts, never substitute a fixture.

    The binding is a prospective Root run input. Acceptance records are pinned
    separately from the artifact bytes. Synthetic bindings are only usable by
    offline controls, never by the SDK runner.
    """
    raw = parse_json_bytes(Path(path).read_bytes())
    if raw.get("schema_version") != SCHEMA or raw.get("kind") != "ACTIVATION":
        raise ForwardPaperError("ACTIVATION_SCHEMA_MISMATCH")
    synthetic = raw.get("input_kind") == "SYNTHETIC_EXPLICIT"
    if raw.get("input_kind") not in ("REAL", "SYNTHETIC_EXPLICIT") or (synthetic and not allow_synthetic) or raw.get("final_holdout") is not False:
        raise ForwardPaperError("ACTIVATION_INPUT_KIND_FORBIDDEN")
    sources = raw.get("sources", {})
    required = {"warmup", "forecast", "regime", "payoff", "contract_lock", "code_closure"}
    if set(sources) not in (required, required | {"seasonality"}):
        raise ForwardPaperError("ACTIVATION_SOURCE_SET_MISMATCH")
    paths = {name: verified_file(ref) for name, ref in sources.items()}
    acceptances = raw.get("acceptances", {})
    if set(acceptances) != set(sources):
        raise ForwardPaperError("ACCEPTANCE_SOURCE_SET_MISMATCH")
    for name, ref in acceptances.items():
        accepted = parse_json_bytes(verified_file(ref).read_bytes())
        if (accepted.get("verdict") != "PASS" or accepted.get("artifact_sha256") != sources[name]["sha256"]
                or accepted.get("input_kind") != raw["input_kind"] or accepted.get("final_holdout") is not False):
            raise ForwardPaperError("SOURCE_NOT_ACCEPTED")
    # Code closure enumerates the actual single import tree, not a donor overlay.
    closure = parse_json_bytes(paths["code_closure"].read_bytes())
    closure_root = Path(closure["root"]).resolve()
    own_root = Path(__file__).resolve().parents[4]
    if closure_root != own_root or not closure.get("files"):
        raise ForwardPaperError("RUNTIME_CLOSURE_ROOT_MISMATCH")
    for name, pin in closure["files"].items():
        member = (closure_root / name).resolve()
        if not member.is_relative_to(closure_root):
            raise ForwardPaperError("RUNTIME_CLOSURE_PATH_ESCAPE")
        verified_file({"path": str(member), **pin})
    bundle = read_frozen_input(paths["warmup"].parent)
    if bundle.manifest_sha256 != sources["warmup"]["sha256"]:
        raise ForwardPaperError("WARMUP_MANIFEST_MISMATCH")
    forecast = parse_json_bytes(paths["forecast"].read_bytes())
    regime = parse_json_bytes(paths["regime"].read_bytes())
    payoff = parse_json_bytes(paths["payoff"].read_bytes())
    if (forecast.get("schema_version") != "ak_probability_models_v2.1.0"
            or regime.get("schema_version") != "ak_probability_regime_reference_v2.1.0"
            or payoff.get("schema_version") != "ak_probability_payoff_model_v2.1.0"):
        raise ForwardPaperError("MODEL_SCHEMA_MISMATCH")
    model = forecast["model_set"]
    models = ForecastModelSet(**{**model, "feature_keys": tuple(model["feature_keys"]),
                                  "evidence": tuple(decode_record(CalibrationEvidence, row) for row in model["evidence"])})
    ref = regime["reference"]
    reference = RegimeReference(**{**ref, "volatility_samples": {k: tuple(v) for k, v in ref["volatility_samples"].items()}})
    pm = payoff["model"]
    observations = []
    for original in pm["observations"]:
        row = dict(original)
        row["horizon"] = decode_record(Horizon, row["horizon"])
        for key in ("net_pnl_cny", "peak_margin_cny", "maximum_loss_cny", "budget_cny"):
            if not isinstance(row[key], str):
                raise ForwardPaperError("PAYOFF_DECIMAL_STRING_REQUIRED")
            row[key] = Decimal(row[key])
        observations.append(PayoffObservation(**row))
    payoff_model = PathPayoffModel(**{**pm, "observations": tuple(observations)})
    seasonal = None
    if "seasonality" in paths:
        from ak_trading.v2.seasonality import SeasonalBucket, SeasonalityModel
        original = parse_json_bytes(paths["seasonality"].read_bytes())
        if original.get("schema_version") != "ak_probability_seasonality_v2.1.1":
            raise ForwardPaperError("SEASONALITY_SCHEMA_MISMATCH")
        sm = original["model"]
        buckets = tuple(SeasonalBucket(**{**row, "median_volume": None if row["median_volume"] is None else Decimal(row["median_volume"])}) for row in sm["buckets"])
        seasonal = SeasonalityModel(**{**sm, "buckets": buckets})
    activation = Activation(bundle, models, payoff_model, reference,
        tuple(decode_record(StrategySpec, row) for row in raw["specs"]),
        tuple(decode_record(ExitSpec, row) for row in raw["exit_specs"]),
        tuple(decode_record(ExitPolicyBinding, row) for row in raw["bindings"]),
        tuple(decode_record(RunPolicy, row) for row in raw["run_policies"]),
        raw["contract_id"], raw["activation_nano"], sources["contract_lock"]["sha256"],
        {**{name: ref["sha256"] for name, ref in sources.items()},
         **{"acceptance:" + name: ref["sha256"] for name, ref in acceptances.items()}},
        synthetic, Decimal(raw["initial_equity_cny"]), seasonal)
    if not live_clock_available():
        raise ForwardPaperError("WAITING_LIVE_CLOCK_PREREQUISITE")
    return activation


def validate_runtime_inputs(config: Mapping, binding: Mapping):
    """Offline validation of Root's exact authorization and one source closure."""
    for key, code in (("runtime_authorization", "SDK_RUNTIME_NOT_AUTHORIZED"),
                      ("runtime_closure", "RUNTIME_CLOSURE_NOT_BOUND")):
        if binding.get(key) is None:
            raise ForwardPaperError(code)
    auth = parse_json_bytes(verified_file(binding["runtime_authorization"]).read_bytes())
    closure_ref = binding["runtime_closure"]
    if (not isinstance(auth, Mapping) or auth.get("schema_version") != SCHEMA or auth.get("kind") != "ROOT_RUNTIME_AUTHORIZATION"
            or auth.get("issuer") != "/root" or auth.get("authorized") is not True
            or auth.get("run_id") != binding["run_id"] or auth.get("action") != "READ_ONLY_TQSDK"
            or auth.get("runtime_closure_sha256") != closure_ref["sha256"]
            or auth.get("config_sha256") != content(config)
            or auth.get("enable_trading") is not False or auth.get("observer") != "hold"
            or auth.get("broker") != "NOT_RUN"):
        raise ForwardPaperError("SDK_RUNTIME_AUTHORIZATION_MISMATCH")
    closure = parse_json_bytes(verified_file(closure_ref).read_bytes())
    root = Path(__file__).resolve().parents[4]
    if (not isinstance(closure, Mapping) or not isinstance(closure.get("root"), str)
            or Path(closure["root"]).resolve() != root or not isinstance(closure.get("files"), Mapping)
            or not closure["files"]):
        raise ForwardPaperError("RUNTIME_CLOSURE_ROOT_MISMATCH")
    required = {"src/ak_trading/v2/validation/forward_paper.py",
                "src/ak_trading/v2/validation/forward_paper_feed.py", "scripts/run_forward_paper.py",
                "src/ak_trading/session.py", "src/ak_trading/config.py"}
    for name, module in tuple(sys.modules.items()):
        if not name.startswith("ak_trading") or getattr(module, "__file__", None) is None:
            continue
        path = Path(module.__file__).resolve()
        if not path.is_relative_to(root):
            raise ForwardPaperError("RUNTIME_CLOSURE_MIXED_IMPORT_ROOT")
        if path.suffix == ".py":
            required.add(path.relative_to(root).as_posix())
    if not required <= set(closure["files"]):
        raise ForwardPaperError("RUNTIME_CLOSURE_INCOMPLETE")
    for name, pin in closure["files"].items():
        if not isinstance(name, str):
            raise ForwardPaperError("RUNTIME_CLOSURE_PATH_INVALID")
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ForwardPaperError("RUNTIME_CLOSURE_PATH_ESCAPE")
        try:
            verified_file({"path": str(path), **pin})
        except (OSError, ValueError, TypeError, KeyError):
            raise ForwardPaperError("RUNTIME_CLOSURE_MEMBER_MISMATCH") from None


def preflight(config: Mapping, binding: Mapping, *, activate: bool = False) -> dict:
    """Offline only: this function never imports session, SDK, dotenv or DB."""
    if not isinstance(config, Mapping) or not isinstance(binding, Mapping):
        raise ForwardPaperError("JSON_OBJECT_REQUIRED")
    blockers = []
    if (config.get("schema_version") != SCHEMA or config.get("purpose") != "RESEARCH_EVALUATION"
            or config.get("experiment_type") != "FORWARD_PAPER_RESEARCH" or config.get("final_holdout") is not False
            or tuple(config.get("strategy_ids", ())) != STRATEGIES or tuple(config.get("routing_modes", ())) != POLICIES
            or config.get("enable_trading") is not False or config.get("observer") != "hold"
            or config.get("broker") != "NOT_RUN" or config.get("cost_scenario") != "BASE"):
        blockers.append("CONFIG_CONTRACT_MISMATCH")
    if (binding.get("schema_version") != SCHEMA or not isinstance(binding.get("run_id"), str)
            or not binding.get("run_id") or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in binding["run_id"])):
        blockers.append("RUN_BINDING_REQUIRED")
    try:
        initial = Decimal(config["initial_equity_cny"]) if isinstance(config.get("initial_equity_cny"), str) else None
        if initial is None or not initial.is_finite() or initial <= 0 or type(config.get("seed")) is not int:
            blockers.append("CONFIG_CONTRACT_MISMATCH")
    except (InvalidOperation, ValueError):
        blockers.append("CONFIG_CONTRACT_MISMATCH")
    activation = None
    if binding.get("activation") is None:
        blockers.extend(("FORECAST_PAYOFF_REGIME_PACK_NOT_ACCEPTED", "CURRENT_INSTRUMENT_CALENDAR_RULES_NOT_BOUND", "WARMUP_NOT_BOUND"))
    else:
        try:
            activation = load_activation(verified_file(binding["activation"]))
        except (ValueError, TypeError, KeyError, OSError) as exc:
            blockers.append(str(exc) if isinstance(exc, ForwardPaperError) else "ACTIVATION_DECODING_FAILED")
    budgets = config.get("budgets", {})
    if (budgets.get("first_wall_seconds") != 600 or budgets.get("later_wall_seconds_max") != 3600
            or budgets.get("memory_limit_bytes") != 2 * 1024 ** 3 or budgets.get("output_limit_bytes") != 1024 ** 3
            or budgets.get("tqapi_connections_max") != 1 or budgets.get("subscriptions_max") != 4
            or budgets.get("sdk_order_calls_max") != 0):
        blockers.append("BUDGET_CONTRACT_MISMATCH")
    for key, code in (("runtime_authorization", "SDK_RUNTIME_NOT_AUTHORIZED"),
                      ("runtime_closure", "RUNTIME_CLOSURE_NOT_BOUND")):
        if binding.get(key) is None:
            blockers.append(code)
        else:
            try:
                verified_file(binding[key])
            except (ValueError, TypeError, KeyError, OSError):
                blockers.append(code)
    if binding.get("runtime_authorization") is not None and binding.get("runtime_closure") is not None:
        try:
            validate_runtime_inputs(config, binding)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            blockers.append(str(exc) if isinstance(exc, ForwardPaperError) else "RUNTIME_CLOSURE_VALIDATION_FAILED")
    hard = [b for b in blockers if b in ("CONFIG_CONTRACT_MISMATCH", "RUN_BINDING_REQUIRED", "BUDGET_CONTRACT_MISMATCH")
            or b.startswith(("SDK_RUNTIME_", "RUNTIME_CLOSURE_"))]
    return {"schema_version": SCHEMA, "status": "WAITING_INPUT" if activation is None else "INPUT_READY",
            "blockers": sorted(set(blockers)), "offline": True, "sdk_constructed": 0,
            "strategy_evaluation": "NOT_RUN", "paper_account": "NOT_ACTIVATED",
            "capture_preflight_ok": not hard,
            "activation_preflight_ok": activation is not None and not hard,
            "requested_mode_ready": not hard and (activation is not None if activate else True),
            "enable_trading": False, "observer": "hold", "broker": "NOT_RUN"}


@dataclass
class PaperAccount:
    policy: RunPolicy
    ledger: ContinuousLedger
    states: dict[str, StrategyState] = field(default_factory=dict)
    pending: dict[str, OrderIntent] = field(default_factory=dict)
    remaining: dict[str, int] = field(default_factory=dict)
    admission: dict[str, dict] = field(default_factory=dict)
    barrier: dict | None = None
    financial_cursor: int = 0
    debt: set[str] = field(default_factory=set)


class ForwardPaperHost:
    """One concrete instrument, four independent books, one ordered receive stream."""

    def __init__(self, root: Path, run_id: str, *, clock: Callable[[], ClockSample],
                 activation: Activation | None = None, synthetic: bool = False,
                 binding_identity: Mapping | None = None, resume: bool = False,
                 fault: Callable[[str], None] | None = None):
        if not isinstance(run_id, str) or not run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in run_id):
            raise ForwardPaperError("RUN_ID_INVALID")
        self.run_id, self.clock, self.activation = run_id, clock, activation
        self.synthetic, self.fault = synthetic, fault or (lambda _point: None)
        if activation is not None and activation.synthetic != synthetic:
            raise ForwardPaperError("INPUT_KIND_MISMATCH")
        self.accounts: dict[str, PaperAccount] = {}
        self.records = {mode: {name: [] for name in ("decisions", "intents", "fills", "executions", "features", "forecasts", "regimes")} for mode in POLICIES}
        self.record_offsets = {mode: {name: 0 for name in self.records[mode]} for mode in POLICIES}
        self.bars: list[MarketBar] = []
        self.raw_closed: dict[str, dict] = {}
        self.event_hashes: dict[str, str] = {}
        self.batch_hashes: dict[str, str] = {}
        self.source_watermark = 0
        self.source_chain = content([run_id, "RECEIVE_START"])
        self.raw_queue: list[dict] = []
        self._request_identities: dict = {}
        self.open_sources: dict[str, dict] = {}
        self.pauses: set[str] = set()
        self.poisoned = False
        self.last_clock: ClockSample | None = None
        self.facts: list[dict] = []
        self.forward_bar_count = 0
        self.strategy_batches = 0
        self.terminal_reason = None
        self.connected = True
        self.conflicted = False
        self.recovering = resume
        identity = {"schema_version": SCHEMA, "run_id": run_id, "synthetic": synthetic,
                    "purpose": "RESEARCH_EVALUATION", "experiment_type": "FORWARD_PAPER_RESEARCH",
                    "final_holdout": False, "binding": dict(binding_identity or {}),
                    "activation": None if activation is None else activation.identity}
        self.journal = Journal(root, identity, create=not resume, fault=self.fault)
        if activation is not None:
            day = self._day(activation.activation_nano)
            for policy in activation.run_policies:
                initial = activation.initial_equity_cny
                account = AccountSnapshot(run_id + ":" + policy.routing_mode, activation.activation_nano, day,
                    initial, initial, ZERO, ZERO, ZERO, ZERO, initial, ZERO, ZERO,
                    initial, ZERO, initial, (), False,
                    DataQuality("READY", ("SCENARIO",), "observed" if not synthetic else "reconstructed", content(identity)))
                ledger = ContinuousLedger(account, activation.bundle.instruments, policy,
                    activation.contract_lock_sha256, registry_hash_for(activation.specs),
                    exit_registry_sha256=exit_registry_hash_for(activation.exit_specs))
                self.accounts[policy.routing_mode] = PaperAccount(policy, ledger, financial_cursor=activation.activation_nano)
        if resume:
            self._restore()
        else:
            self._seal("START", {"activation": activation is not None})

    def _bundle(self) -> FrozenInput:
        if self.activation is None:
            raise ForwardPaperError("WAITING_INPUT")
        return replace(self.activation.bundle, bars=self.activation.bundle.bars + tuple(self.bars))

    def _day(self, when: int) -> date:
        rows = [s for s in self.activation.bundle.sessions if s.start_nano <= when < s.end_nano]
        if len(rows) != 1:
            raise ForwardPaperError("CALENDAR_SESSION_UNKNOWN")
        return rows[0].trading_day

    def _envelope(self) -> dict:
        return {"source_watermark": self.source_watermark, "source_chain": self.source_chain,
                "raw_queue": self.raw_queue, "open_sources": self.open_sources, "pauses": sorted(self.pauses),
                "event_hashes": self.event_hashes, "batch_hashes": self.batch_hashes,
                "last_clock": None if self.last_clock is None else self.last_clock.payload(),
                "bars": [encode_record(b) for b in self.bars], "raw_closed": self.raw_closed,
                "facts": self.facts, "forward_bar_count": self.forward_bar_count,
                "strategy_batches": self.strategy_batches, "terminal_reason": self.terminal_reason,
                "connected": self.connected, "conflicted": self.conflicted,
                "accounts": {mode: {"admission": book.admission, "barrier": book.barrier,
                                     "financial_cursor": book.financial_cursor, "debt": sorted(book.debt),
                                     "reconciliation": _plain(book.ledger.reconcile()),
                                     "account_id": book.ledger._account.account_id,
                                     "policy_sha256": record_hash(book.policy)} for mode, book in self.accounts.items()}}

    def _checkpoints(self) -> dict:
        if not self.accounts:
            return {}
        activation = self.activation
        hashes = model_hashes_for(activation.models, activation.payoff, activation.reference, activation.seasonality)
        return {mode: encode_record(book.ledger.checkpoint(tuple(book.states.values()), tuple(book.pending.values()),
            hashes, {}, pending_remaining_lots=book.remaining,
            exit_registry_sha256=exit_registry_hash_for(activation.exit_specs),
            exit_bindings=activation.bindings, exit_states=(), exit_execution_links=()))
                for mode, book in self.accounts.items()}

    def _seal(self, kind: str, event: Mapping) -> dict:
        self._usable()
        delta = {mode: {name: [encode_record(row) for row in rows[self.record_offsets[mode][name]:]]
                         for name, rows in records.items()} for mode, records in self.records.items()}
        try:
            sealed = self.journal.seal(kind, event, self._envelope(), delta, self._checkpoints())
        except BaseException:
            self.poisoned = True
            raise

        for mode, records in self.records.items():
            for name, rows in records.items():
                self.record_offsets[mode][name] = len(rows)
        return sealed

    def process_batch(self, batch: ReceivedBatch) -> None:
        """Receive first, then consume OPEN before CLOSED from the same update.

        The persisted receive queue is distinct from the financial processed
        ids. A crash between the two leaves original raw evidence to reconcile.
        """
        self._usable()
        if batch.run_id != self.run_id:
            raise ForwardPaperError("RECEIVE_RUN_MISMATCH")
        key = str(batch.source_sequence)
        prior = self.batch_hashes.get(key)
        if prior is not None:
            if prior != batch.payload_sha256:
                self._conflict("SOURCE_SEQUENCE_CONFLICT", key)
            else:
                self.facts.append({"kind": "DUPLICATE_BATCH", "sequence": batch.source_sequence})
                self._seal("DUPLICATE", self.facts[-1])
            return
        if batch.source_sequence <= self.source_watermark:
            self._conflict("SOURCE_SEQUENCE_REVERSED", key)
            return
        if batch.source_sequence != self.source_watermark + 1:
            self.pauses.add("SOURCE_GAP")
            self.facts.append({"kind": "SOURCE_GAP", "expected": self.source_watermark + 1,
                               "actual": batch.source_sequence})
        self.source_watermark = batch.source_sequence
        self.batch_hashes[key] = batch.payload_sha256
        self.source_chain = content([self.source_chain, batch.payload()])
        order = {"OPEN": 0, "CLOSED": 1, "BOOTSTRAP": 2, "CORRECTION": 3, "HEALTH": 4, "CLOCK": 5}
        # Sorting is only inside a single SDK update. Updates retain receipt order.
        ordered = sorted(batch.events, key=lambda e: (order[e.kind], e.source_nano, e.event_id))
        self.raw_queue.extend({"event": e.payload(), "clock": batch.clock.payload(),
                               "source_sequence": batch.source_sequence} for e in ordered)
        self._seal("RECEIVE", batch.payload())
        self._drain()

    def _drain(self, *, recovery: bool = False):
        while self.raw_queue:
            item = self.raw_queue.pop(0)
            event = SourceEvent(**item["event"])
            sample = ClockSample(**item["clock"])
            digest = content(event.payload())
            previous = self.event_hashes.get(event.event_id)
            if previous is not None:
                if previous != digest:
                    self._conflict("SOURCE_EVENT_CONFLICT", event.event_id)
                else:
                    self.facts.append({"kind": "DUPLICATE_EVENT", "event_id": event.event_id})
                    self._seal("DUPLICATE", self.facts[-1])
                continue
            self.event_hashes[event.event_id] = digest
            reasons = sample.reasons(self.last_clock)
            if reasons:
                self.pauses.add("CLOCK_UNVERIFIED")
                self.facts.append({"kind": "CLOCK_REJECTED", "event_id": event.event_id, "reasons": list(reasons)})
            else:
                self.pauses.discard("CLOCK_UNVERIFIED")
            self.last_clock = sample
            try:
                if event.kind == "OPEN":
                    self._open(event, sample, historical=recovery)
                elif event.kind == "CLOSED":
                    self._closed(event, sample, historical=recovery)
                elif event.kind == "BOOTSTRAP":
                    self.facts.append({"kind": "BOOTSTRAP", "source_sha256": digest,
                                       "received": sample.payload(), "forward_credit": False})
                elif event.kind == "CORRECTION":
                    self._conflict("SOURCE_CORRECTION", event.event_id, seal=False)
                elif event.kind == "HEALTH":
                    connected = event.data.get("connected")
                    if type(connected) is not bool:
                        raise ForwardPaperError("HEALTH_INVALID")
                    self.connected = connected
                    if connected and event.data.get("recovered") is True:
                        self.pauses.discard("SOURCE_GAP")
                        self.facts.append({"kind": "RECONNECTED", "historical_open_eligible": False})
                    self._health(sample)
                else:
                    self._health(sample)
                self._seal("EVENT", {"event_id": event.event_id, "kind": event.kind,
                                     "source_sha256": digest, "received": sample.payload(),
                                     "recovery": recovery})
                if self.forward_bar_count:
                    self.write_report()
            except BaseException:
                self.poisoned = True
                raise

    def _conflict(self, reason: str, identity: str, *, seal=True):
        self.conflicted = True
        self.pauses.add(reason)
        self.facts.append({"kind": reason, "identity": identity})
        self._cancel_entries(reason)
        for book in self.accounts.values():
            if book.barrier is not None:
                book.barrier["incomplete_reasons"] = sorted(set(book.barrier["incomplete_reasons"]) | {reason})
        if seal:
            self._seal("CONFLICT", self.facts[-1])

    def _cancel_entries(self, reason: str):
        for book in self.accounts.values():
            if book.barrier is not None:
                continue
            for intent in tuple(book.pending.values()):
                if intent.offset == "OPEN":
                    self._cancel(book, intent.intent_id, reason)

    def _sample(self, previous: ClockSample | None = None) -> tuple[ClockSample, tuple[str, ...]]:
        sample = self.clock()
        if not isinstance(sample, ClockSample):
            raise ForwardPaperError("CLOCK_SAMPLE_REQUIRED")
        return sample, sample.reasons(previous or self.last_clock)

    def _session(self, start: int, end: int):
        matches = [s for s in self.activation.bundle.sessions
                   if s.product == "SHFE.RB" and s.start_nano <= start < end <= s.end_nano]
        if len(matches) != 1 or matches[0].evidence_status == "UNKNOWN":
            raise ForwardPaperError("CALENDAR_SESSION_UNKNOWN")
        return matches[0]

    def _market_bar(self, event: SourceEvent, sample: ClockSample, *, closed: bool) -> MarketBar:
        row = event.data
        inst = self.activation.bundle.instruments[0]
        start = _integer(row.get("start_nano"), "BAR_START", 1)
        if (event.source_nano != start or row.get("contract_id") != inst.contract_id
                or row.get("raw_symbol") != inst.raw_symbol or row.get("duration_seconds") != 60
                or row.get("observed_progression") is not True or start > sample.utc_nano):
            raise ForwardPaperError("SOURCE_BAR_IDENTITY_INVALID")
        end = start + MINUTE
        session = self._session(start, end)
        if (start - session.start_nano) % MINUTE:
            raise ForwardPaperError("SOURCE_BAR_SLOT_INVALID")
        if closed and (row.get("progressed_to_nano", 0) < end or sample.utc_nano < end):
            raise ForwardPaperError("CLOSED_WITHOUT_REAL_PROGRESSION")
        def number(name, required=False):
            val = row.get(name)
            if val is None and not required:
                return None
            if not isinstance(val, str):
                raise ForwardPaperError("SOURCE_DECIMAL_STRING_REQUIRED")
            value = Decimal(val)
            if not value.is_finite() or value < 0 or (required and value <= 0):
                raise ForwardPaperError("SOURCE_PRICE_INVALID")
            return value
        opening = number("open", True)
        high, low, close = (number(n, True) for n in ("high", "low", "close")) if closed else (None, None, None)
        if closed and not low <= min(opening, close) <= max(opening, close) <= high:
            raise ForwardPaperError("SOURCE_OHLC_INVALID")
        return MarketBar(stable_id("forward_bar", canonical_json_bytes([inst.contract_id, start])),
            inst.contract_id, inst.raw_symbol, inst.product, 60, start, start, end,
            session.trading_day, session.session_id, max(end, sample.utc_nano) if closed else None,
            sample.raw_utc_nano, opening, high, low, close, number("volume") if closed else None,
            number("open_oi") if closed else None, number("close_oi") if closed else None,
            closed, 1, 1 if closed else 0, "TQSDK_FORWARD" if not self.synthetic else "SYNTHETIC_FORWARD",
            DataQuality("READY", () if not self.synthetic else ("SCENARIO",), "observed",
                        content(event.payload())))

    def _open(self, event: SourceEvent, received: ClockSample, *, historical=False):
        # Capture-only still preserves original OPEN, but owns no financial account.
        self.open_sources[event.event_id] = {"event": event.payload(), "received": received.payload()}
        if not self.accounts:
            return
        try:
            bar = self._market_bar(event, received, closed=False)
        except (ValueError, TypeError, InvalidOperation) as exc:
            self.facts.append({"kind": "OPEN_REJECTED", "event_id": event.event_id,
                               "reason": str(exc) if isinstance(exc, ForwardPaperError) else "OPEN_INVALID"})
            self._cancel_entries("OPEN_INVALID")
            return
        processing, errors = self._sample(received)
        ages = (received.utc_nano - bar.start_nano, processing.utc_nano - bar.start_nano)
        historical = historical or event.data.get("historical") is True or self.recovering
        for mode, book in self.accounts.items():
            if book.barrier is not None:
                self.facts.append({"kind": "OPEN_SKIPPED_BARRIER", "mode": mode, "event_id": event.event_id})
                continue
            matching = sorted((i for i in book.pending.values() if i.not_before_nano <= bar.start_nano),
                              key=lambda i: (i.offset == "OPEN", i.created_at_nano, i.intent_id))
            rejected = []
            for intent in matching:
                admission = book.admission[intent.intent_id]
                reason = None
                if admission.get("status") != "CONFIRMED_ELIGIBLE":
                    reason = "INTENT_UNCONFIRMED"
                elif historical:
                    reason = "HISTORICAL_OPEN"
                elif errors or received.reasons() or self.pauses or not self.connected or self.conflicted:
                    reason = "OPEN_CLOCK_OR_SOURCE_UNVERIFIED"
                elif min(ages) < 0 or max(ages) > MAX_OPEN_AGE:
                    reason = "MISSED_OPEN_AGE"
                elif bar.start_nano < max(book.financial_cursor, book.ledger._account.as_of_nano):
                    reason = "OPEN_BEFORE_FINANCIAL_CURSOR"
                elif bar.start_nano >= intent.expires_at_nano:
                    reason = "INTENT_EXPIRED"
                elif intent.offset == "OPEN" and processing.utc_nano >= self.activation.models.valid_until_nano:
                    reason = "MODEL_EXPIRED"
                elif intent.offset == "OPEN" and book.ledger._account.killed:
                    reason = "ACCOUNT_KILLED"
                elif intent.offset == "CLOSE_TODAY" and book.ledger._account.trading_day != bar.trading_day:
                    reason = "CLOSE_OFFSET_STALE"
                if reason:
                    rejected.append((intent.intent_id, reason))
            for intent_id, reason in rejected:
                self._cancel(book, intent_id, reason)
                self.facts.append({"kind": reason, "mode": mode, "intent_id": intent_id,
                                   "open_event_id": event.event_id, "observed": received.payload(),
                                   "processing": processing.payload()})
            matching = [i for i in matching if i.intent_id in book.pending]
            if not matching:
                continue
            rule = rule_at(self.activation.bundle.rules, bar.contract_id, bar.start_nano)
            # This prefix intentionally excludes CLOSED received in this same batch.
            prefix = tuple(b for b in self._bundle().bars if b.end_nano <= bar.start_nano
                           and b.available_at_nano is not None and b.available_at_nano <= bar.start_nano)
            capacity = capacity_before(prefix, bar.contract_id, 60, bar.start_nano, rule)
            cap_key = f"{bar.contract_id}|{bar.start_nano}|BASE"
            remaining = max(0, (capacity or 0) - book.ledger._capacity.get(cap_key, 0))
            protective = [i for i in matching if i.purpose in ("STOP", "TARGET") and i.offset != "OPEN"]
            if protective and rule is not None:
                # Freeze the original economic prefix. No mark/decision may advance
                # beyond a prospective Fill whose economic time is this OPEN.
                book.barrier = {"status": "WAITING_PROTECTIVE_RECEIPT", "open_event_id": event.event_id,
                    "open": encode_record(bar), "open_source": event.payload(), "received": received.payload(),
                    "processing": processing.payload(), "rule": encode_record(rule), "rule_sha256": record_hash(rule),
                    "capacity_remaining": remaining, "capacity_key": cap_key,
                    "prefix_sha256": content([encode_record(b) for b in prefix]),
                    "account_sha256": record_hash(book.ledger._account),
                    "financial_cursor": book.financial_cursor,
                    "intent_ids": [i.intent_id for i in protective], "incomplete_reasons": []}
                continue
            used = 0
            for intent in matching:
                position = position_for(book.ledger.snapshot(bar.start_nano), bar.contract_id)
                result = (ExecutionResult(intent.intent_id, (), book.remaining[intent.intent_id], "BLOCKED", ("COST_UNKNOWN",))
                          if rule is None else simulate_execution(replace(intent, delta_lots=book.remaining[intent.intent_id]),
                                                                 bar, rule, remaining, position))
                if result.status == "BLOCKED" and not result.fills and intent.offset == "OPEN":
                    # Production replay contract (003 §4): a transiently blocked
                    # entry keeps its intent and signed remainder; record once,
                    # retry at a later real OPEN. Only expiry or an independent
                    # cancel terminates the unfilled part. Applying the receipt
                    # here would settle the state while the intent stays pending
                    # and break the checkpoint's pending/state invariant.
                    self.records[book.policy.routing_mode]["executions"].append(result)
                else:
                    self._receipt(book, intent, result, bar.start_nano)
                count = sum(f.lots for f in result.fills)
                used += count
                remaining -= count
                self.facts.append({"kind": "EXECUTION_RECEIPT", "mode": mode, "intent_id": intent.intent_id,
                                   "open_event_id": event.event_id, "economic_execution_nano": bar.start_nano,
                                   "observed": received.payload(), "processing": processing.payload(),
                                   "receipt_observed": self.clock().payload(), "status": result.status})
            book.ledger.record_event(event.event_id, {cap_key: used})
            book.ledger.sync_reservations(tuple(book.states.values()), self.activation.bundle.rules, bar.start_nano)
            book.financial_cursor = bar.start_nano

    def _verify_barrier(self, book: PaperAccount):
        barrier = book.barrier
        opening = decode_record(MarketBar, barrier["open"])
        from ak_trading.v2.contracts import TradingRule
        rule = decode_record(TradingRule, barrier["rule"])
        actual_rule = rule_at(self.activation.bundle.rules, opening.contract_id, opening.start_nano)
        prefix = tuple(b for b in self._bundle().bars if b.end_nano <= opening.start_nano
                       and b.available_at_nano is not None and b.available_at_nano <= opening.start_nano)
        capacity = capacity_before(prefix, opening.contract_id, 60, opening.start_nano, actual_rule)
        remaining = max(0, (capacity or 0) - book.ledger._capacity.get(barrier["capacity_key"], 0))
        if (barrier["status"] != "WAITING_PROTECTIVE_RECEIPT" or opening.complete
                or any(getattr(opening, n) is not None for n in ("high", "low", "close", "volume"))
                or rule != actual_rule or record_hash(rule) != barrier["rule_sha256"]
                or record_hash(book.ledger._account) != barrier["account_sha256"]
                or book.financial_cursor != barrier["financial_cursor"]
                or barrier["prefix_sha256"] != content([encode_record(b) for b in prefix])
                or barrier["capacity_remaining"] != remaining
                or not barrier["intent_ids"]
                or opening.start_nano < book.financial_cursor
                or any(i not in book.pending or book.pending[i].purpose not in ("STOP", "TARGET") for i in barrier["intent_ids"])):
            raise ForwardPaperError("PROTECTIVE_BARRIER_SOURCE_MISMATCH")
        original = self.open_sources.get(barrier["open_event_id"])
        if original is None or original["event"] != barrier["open_source"] or original["received"] != barrier["received"]:
            raise ForwardPaperError("PROTECTIVE_OPEN_ORIGINAL_MISSING")

    def _protective_receipt(self, book: PaperAccount, bar: MarketBar, received: ClockSample,
                            *, historical=False, source_event_id=None):
        self._verify_barrier(book)
        barrier = book.barrier
        opening = decode_record(MarketBar, barrier["open"])
        if bar.start_nano != opening.start_nano:
            return False
        if bar.open != opening.open or not bar.complete:
            raise ForwardPaperError("PROTECTIVE_CLOSED_OPEN_MISMATCH")
        from ak_trading.v2.contracts import TradingRule
        rule = decode_record(TradingRule, barrier["rule"])
        remaining = barrier["capacity_remaining"]
        used = 0
        recovered = (bool(barrier["incomplete_reasons"]) or self.recovering or historical or bool(received.reasons())
                     or received.utc_nano - bar.end_nano > MAX_MARKET_AGE)
        for intent_id in barrier["intent_ids"]:
            intent = book.pending[intent_id]
            state = book.states[intent.strategy_id]
            position = position_for(book.ledger.snapshot(opening.start_nano), bar.contract_id)
            if position is None or state.budget is None:
                raise ForwardPaperError("PROTECTIVE_POSITION_MISSING")
            result = simulate_protective_exit(replace(intent, delta_lots=book.remaining[intent_id]), bar,
                                              rule, remaining, position, state.budget)
            # The actual receipt is observed now. Economic state is transitioned
            # at original OPEN, before the deferred close can mark the account.
            self._receipt(book, intent, result, opening.start_nano)
            count = sum(f.lots for f in result.fills)
            remaining -= count
            used += count
            self.facts.append({"kind": "PROTECTIVE_RECEIPT", "mode": book.policy.routing_mode,
                               "intent_id": intent_id, "economic_execution_nano": opening.start_nano,
                               "open_event_id": barrier["open_event_id"], "closed_event_id": source_event_id,
                               "fill_ids": [f.fill_id for f in result.fills],
                               "received": received.payload(), "status": result.status,
                               "recovered_receipt": recovered, "timely_forward_credit": not recovered})
        book.ledger.record_event(barrier["open_event_id"], {barrier["capacity_key"]: used})
        book.barrier = None
        book.ledger.sync_reservations(tuple(book.states.values()), self.activation.bundle.rules, opening.start_nano)
        book.financial_cursor = opening.start_nano
        return True

    def _closed(self, event: SourceEvent, received: ClockSample, *, historical=False):
        self.raw_closed[event.event_id] = {"event": event.payload(), "received": received.payload()}
        if self.activation is None:
            # Validate capture without inventing an accepted calendar/Instrument.
            row = event.data
            valid_numbers = {}
            for name in ("open", "high", "low", "close", "volume", "open_oi", "close_oi"):
                value = row.get(name)
                try:
                    number = Decimal(value) if isinstance(value, str) else None
                except (ValueError, InvalidOperation):
                    number = None
                valid_numbers[name] = number is not None and number.is_finite() and (
                    number > 0 if name in ("open", "high", "low", "close") else number >= 0)
            if (row.get("observed_progression") is not True or type(row.get("start_nano")) is not int
                    or event.source_nano != row["start_nano"] or row.get("progressed_to_nano", 0) < row["start_nano"] + MINUTE
                    or received.utc_nano < row["start_nano"] + MINUTE or row.get("duration_seconds") != 60
                    or not isinstance(row.get("raw_symbol"), str) or not row["raw_symbol"].startswith("SHFE.rb")
                    or not isinstance(row.get("contract_id"), str) or row["contract_id"].upper() != row["raw_symbol"].upper()
                    or not all(valid_numbers.values())
                    or not Decimal(row["low"]) <= min(Decimal(row["open"]), Decimal(row["close"]))
                        <= max(Decimal(row["open"]), Decimal(row["close"])) <= Decimal(row["high"])):
                self.facts.append({"kind": "CLOSED_REJECTED", "event_id": event.event_id})
                return
            if not historical and event.data.get("historical") is not True:
                self.forward_bar_count += 1
                self.facts.append({"kind": "FORWARD_CLOSED", "event_id": event.event_id,
                    "source_start_nano": row["start_nano"], "source_end_nano": row["start_nano"] + MINUTE,
                    "received": received.payload()})
            return
        try:
            bar = self._market_bar(event, received, closed=True)
        except (ValueError, TypeError, InvalidOperation) as exc:
            self.facts.append({"kind": "CLOSED_REJECTED", "event_id": event.event_id,
                               "reason": str(exc) if isinstance(exc, ForwardPaperError) else "CLOSED_INVALID"})
            self._cancel_entries("CLOSED_INVALID")
            return
        existing = next((b for b in self._bundle().bars if b.contract_id == bar.contract_id
                         and b.duration_seconds == 60 and b.start_nano == bar.start_nano), None)
        if existing is not None:
            if (existing.open, existing.high, existing.low, existing.close, existing.volume) != (
                    bar.open, bar.high, bar.low, bar.close, bar.volume):
                self._conflict("CLOSED_SOURCE_CONFLICT", event.event_id, seal=False)
            return
        matches_barrier = any(book.barrier is not None
            and decode_record(MarketBar, book.barrier["open"]).start_nano == bar.start_nano
            for book in self.accounts.values())
        out_of_order = bool(self.bars and bar.start_nano <= self.bars[-1].start_nano)
        if out_of_order and not matches_barrier:
            self.pauses.add("SOURCE_OUT_OF_ORDER")
            self.facts.append({"kind": "CLOSED_OUT_OF_ORDER", "event_id": event.event_id})
            self._cancel_entries("SOURCE_OUT_OF_ORDER")
            return
        self.bars.append(bar)
        self.bars.sort(key=lambda b: b.start_nano)
        if not historical and not out_of_order and event.data.get("historical") is not True:
            self.forward_bar_count += 1
            self.facts.append({"kind": "FORWARD_CLOSED", "event_id": event.event_id,
                "source_start_nano": bar.start_nano, "source_end_nano": bar.end_nano,
                "trading_day": bar.trading_day.isoformat(), "session_id": bar.session_id,
                "received": received.payload()})
        for book in self.accounts.values():
            if book.barrier is not None:
                self._protective_receipt(book, bar, received,
                    historical=historical or out_of_order or event.data.get("historical") is True,
                    source_event_id=event.event_id)
        decision_clock, reasons = self._sample(received)
        if reasons:
            self.pauses.add("CLOCK_UNVERIFIED")
        now = decision_clock.utc_nano
        if reasons or received.reasons() or "CLOCK_UNVERIFIED" in self.pauses:
            self.facts.append({"kind": "EVALUATION_NOT_RUN", "reason": "CLOCK_UNVERIFIED", "event_id": event.event_id})
            self._health(decision_clock)
            return
        for book in self.accounts.values():
            if book.barrier is not None:
                continue
            if now < max(book.financial_cursor, book.ledger._account.as_of_nano):
                self.pauses.add("FINANCIAL_TIME_REVERSED")
                continue
            latest = max((b for b in self.bars if b.available_at_nano is not None
                          and b.available_at_nano <= now), key=lambda b: b.end_nano)
            book.ledger.mark({latest.contract_id: latest}, now, self._day(now), self.activation.bundle.rules)
            book.financial_cursor = now
        if historical or out_of_order or event.data.get("historical") is True:
            self.facts.append({"kind": "RECOVERY_CLOSED", "event_id": event.event_id, "decision_credit": False})
            self._health(decision_clock)
            return
        self._evaluate(now, decision_clock)

    def _provider(self, decision: int):
        bundle = self._bundle()
        from ak_trading.v2.data import visible_bars
        daily = visible_bars(bundle, self.activation.contract_id, 86400, decision, True)
        last_daily = daily[-1] if daily else None
        def provider(models, request, regime):
            f = request.features
            if models is not self.activation.models or f.contract_id != self.activation.contract_id:
                raise ForwardPaperError("PROVIDER_MODEL_OR_CONTRACT_MISMATCH")
            # The pure request builder is deterministic in its inputs; one
            # identity rebuild per distinct (features, horizon, range) keeps
            # the per-publication originality check without re-deriving the
            # whole request set for every single publication.
            key = (f, request.horizon, regime.frozen_range)
            identities = self._request_identities.get(key)
            if identities is None:
                identities = build_forecast_requests(f, (request.horizon,), regime.frozen_range, bundle.sessions)
                self._request_identities[key] = identities
            if request not in identities:
                raise ForwardPaperError("PROVIDER_REQUEST_IDENTITY_MISMATCH")
            if f.duration_seconds == 60 and f.decision_nano == decision:
                samples = []
                def publication_clock():
                    sample, errors = self._sample()
                    samples.append((sample, errors))
                    if errors or sample.utc_nano < decision:
                        self.facts.append({"kind": "PUBLICATION_NOT_PUBLISHED", "role": "FOREGROUND_LIVE",
                            "request_id": request.request_id, "clock": sample.payload(), "reasons": list(errors) or ["CLOCK_REVERSED"]})
                        from ak_trading.v2.probability.forecast import UnpublishableForecastError
                        raise UnpublishableForecastError(request.request_id, ("PUBLICATION_CLOCK_UNVERIFIED",))
                    return sample.utc_nano
                forecast = publish_market_forecast(models, request, regime, clock_nano=publication_clock)
                sample, _ = samples[0]
                self.facts.append({"kind": "PUBLICATION", "role": "FOREGROUND_LIVE",
                    "request_id": request.request_id, "forecast_id": forecast.forecast_id,
                    "decision_nano": decision, "published_at_nano": forecast.published_at_nano,
                    "window_start_nano": forecast.window_start_nano, "clock": sample.payload(),
                    "quality": forecast.quality.status})
                return forecast
            at = None if last_daily is None else max(last_daily.end_nano, last_daily.available_at_nano)
            if (f.duration_seconds == 86400 and last_daily is not None and f.decision_nano == at < decision
                    and f.trading_day == last_daily.trading_day and request.horizon == Horizon("TRADING_DAY", 1)
                    and request.barrier_key == "ATR_1" and last_daily.bar_id in f.source_bar_ids
                    and all(any(b.bar_id == source and b.complete and b.end_nano <= at
                                and b.available_at_nano is not None and b.available_at_nano <= at for b in daily)
                            for source in f.source_bar_ids)):
                forecast = predict_market(models, request, regime, published_at_nano=at)
                self.facts.append({"kind": "PUBLICATION", "role": "BACKGROUND_RECONSTRUCTED",
                    "publication_clock": "RESEARCH_RECONSTRUCTED_NOT_OBSERVED", "decision_nano": at,
                    "request_id": request.request_id, "forecast_id": forecast.forecast_id,
                    "reconstruction_clock": self.clock().payload(),
                    "source_hashes": {b.bar_id: record_hash(b) for b in daily if b.bar_id in f.source_bar_ids}})
                return forecast
            raise ForwardPaperError("UNDECLARED_FORECAST_REQUEST_ROLE")
        return provider

    def _new_intents(self, book, proposal, decision, now):
        if (decision not in self.records[book.policy.routing_mode]["decisions"]
                or decision.selected_proposal_id != proposal.proposal_id
                or proposal not in decision.proposals or decision.decision_nano != now
                or decision.run_policy_sha256 != record_hash(book.policy)):
            raise ForwardPaperError("SELECTED_DECISION_ORIGINAL_REQUIRED")
        if proposal.action in ("EXIT", "REDUCE"):
            book.debt.add(proposal.strategy_id)
            for old in tuple(book.pending.values()):
                if old.episode_id == proposal.episode_id and old.offset == "OPEN":
                    self._cancel(book, old.intent_id, "RISK_CANCEL")
        if any(i.episode_id == proposal.episode_id for i in book.pending.values()):
            return ()
        rule = visible_rule(self._bundle(), self.activation.contract_id, now)
        intents = intents_for_proposal(proposal, decision.decision_id, book.ledger.snapshot(now), rule)
        book.states[proposal.strategy_id] = commit_selected_proposal(book.states[proposal.strategy_id], proposal, intents)
        book.pending.update({i.intent_id: i for i in intents})
        book.remaining.update({i.intent_id: i.delta_lots for i in intents})
        self.records[book.policy.routing_mode]["intents"].extend(intents)
        source = next((f for f in reversed(self.facts) if f["kind"] == "DECISION"
                       and f["mode"] == book.policy.routing_mode and f["decision_id"] == decision.decision_id), None)
        if source is None or source["intent_ids"]:
            raise ForwardPaperError("DECISION_SOURCE_ORIGINAL_REQUIRED")
        source["intent_ids"] = [i.intent_id for i in intents]
        return intents

    def _record_decision(self, book, decision: DecisionRecord, account: AccountSnapshot,
                         *, risk_state: StrategyState | None = None, clocks: Mapping | None = None):
        """Retain original routing inputs as evidence, never as another ledger."""
        self.records[book.policy.routing_mode]["decisions"].append(decision)
        prefix = [encode_record(b) for b in self._bundle().bars
                  if b.end_nano <= decision.decision_nano and b.available_at_nano is not None
                  and b.available_at_nano <= decision.decision_nano]
        self.facts.append({"kind": "DECISION", "mode": book.policy.routing_mode,
            "origin": "STRATEGY" if risk_state is None else "RISK_TIMER",
            "decision_id": decision.decision_id, "input_prefix_sha256": content(prefix),
            "account_before_sha256": record_hash(account), "account_before": encode_record(account),
            "risk_state": None if risk_state is None else encode_record(risk_state),
            "evaluation_gate_clocks": dict(clocks or {}),
            "intent_ids": [],
            "source_sequence": self.source_watermark, "source_chain": self.source_chain,
            "policy_sha256": record_hash(book.policy),
            "candidates": [{"strategy_id": p.strategy_id, "proposal_id": p.proposal_id,
                            "action": p.action, "reasons": list(p.reasons)} for p in decision.proposals]})

    def _evaluate(self, decision: int, initial_clock: ClockSample):
        if not live_clock_available():
            raise ForwardPaperError("WAITING_LIVE_CLOCK_PREREQUISITE")
        bundle = self._bundle()
        policy = research_policy()
        strategies = strategy_registry_v2("MINUTE")
        bindings = {b.strategy_id: b for b in self.activation.bindings}
        additions = {mode: () for mode in self.accounts}
        evaluated = False
        for mode, book in self.accounts.items():
            if book.barrier is not None or book.financial_cursor > decision:
                continue
            contexts, batch = {}, []
            pending_owner = bool(book.pending)
            for spec in sorted(self.activation.specs, key=lambda s: s.strategy_id):
                context = build_strategy_context(bundle, self.activation.contract_id, decision,
                    book.ledger.snapshot(decision), book.states.get(spec.strategy_id), self.activation.models,
                    self.activation.payoff, spec, self.activation.seasonality, self.activation.reference,
                    book.policy, (), exit_binding=bindings[spec.strategy_id], forecast_provider=self._provider(decision))
                checked, clock_errors = self._sample(initial_clock)
                if checked.utc_nano < decision:
                    self.facts.append({"kind": "EVALUATION_NOT_RUN", "mode": mode, "reason": "STRATEGY_CLOCK_REVERSED"})
                    contexts, batch = {}, []
                    break
                proposal = strategies[spec.strategy_id].evaluate(context, book.states.get(spec.strategy_id),
                                                                 checked_at_nano=checked.utc_nano)
                contexts[spec.strategy_id] = (context, checked, clock_errors)
                batch.append(proposal)
                records = self.records[mode]
                records["features"].append(context.features)
                records["forecasts"].extend(context.forecasts)
                records["regimes"].append(context.regime)
                if context.rule is not None:
                    book.ledger._consume_rule(context.rule)
            for proposal in batch:
                book.states[proposal.strategy_id] = commit_observation(book.states.get(proposal.strategy_id), proposal)
            groups = {}
            for p in batch:
                context = contexts[p.strategy_id][0]
                f = next((f for f in context.forecasts if f.forecast_id == p.forecast_id), None)
                horizon = f.horizon if f is not None else context.spec.max_holding
                groups.setdefault((horizon.unit, horizon.value), []).append((p, horizon))
            for _, members in sorted(groups.items()):
                before = book.ledger.snapshot(decision)
                opp = opportunity_id(self.activation.contract_id, decision, members[0][1], before)
                attached, gates, evaluation_gate_clocks = [], [], {}
                for p, _ in members:
                    context, checked, e_errors = contexts[p.strategy_id]
                    context = replace(context, account=before)
                    if p.action in ("OPEN", "ADD"):
                        p = replace(p, payoff=estimate_proposal_payoff(self.activation.payoff, p, context, opp))
                    gate_clock, g_errors = self._sample(checked)
                    if gate_clock.utc_nano < checked.utc_nano:
                        self.facts.append({"kind": "EVALUATION_NOT_RUN", "mode": mode, "reason": "GATE_CLOCK_REVERSED"})
                        attached, gates = [], []
                        break
                    gate = evaluate_gate(p, context, policy, checked_at_nano=gate_clock.utc_nano)
                    extra = set(self.pauses)
                    if pending_owner:
                        extra.add("EXIT_PENDING")
                    if not self.connected:
                        extra.add("SOURCE_DISCONNECTED")
                    if self.conflicted:
                        extra.add("SOURCE_CONFLICT")
                    if e_errors or g_errors:
                        extra.add("CLOCK_UNVERIFIED")
                    if not self.bars or gate_clock.utc_nano - self.bars[-1].end_nano > MAX_MARKET_AGE:
                        extra.add("MARKET_STALE")
                    if p.action in ("OPEN", "ADD") and extra:
                        gate = replace(gate, eligible=False, reasons=tuple(sorted(set(gate.reasons) | extra)))
                    attached.append(p)
                    gates.append(gate)
                    evaluation_gate_clocks[p.proposal_id] = {"evaluation": checked.payload(), "gate": gate_clock.payload()}
                if not attached:
                    continue
                rule = visible_rule(bundle, self.activation.contract_id, decision)
                cap = capacity_before(contexts[attached[0].strategy_id][0].bars, self.activation.contract_id, 60, decision, rule)
                snapshot = book.ledger._fork().checkpoint(tuple(book.states.values()), tuple(book.pending.values()),
                    model_hashes_for(self.activation.models, self.activation.payoff, self.activation.reference, self.activation.seasonality),
                    {}, pending_remaining_lots=book.remaining, exit_registry_sha256=exit_registry_hash_for(self.activation.exit_specs),
                    exit_bindings=self.activation.bindings, exit_states=(), exit_execution_links=())
                risked = enforce_account_risk(tuple(attached), tuple(gates), snapshot.account, bundle.instruments,
                    snapshot.states, snapshot.pending_intents, bundle.rules,
                    {} if cap is None else {self.activation.contract_id: cap}, policy, valuations=snapshot.valuation_marks)
                selected = choose_proposal(tuple(attached), risked, before, opp, decision, book.policy, ())
                self._record_decision(book, selected, before, clocks=evaluation_gate_clocks)
                if selected.selected_proposal_id is not None:
                    p = next(p for p in attached if p.proposal_id == selected.selected_proposal_id)
                    additions[mode] += self._new_intents(book, p, selected, decision)
                book.ledger.sync_reservations(tuple(book.states.values()), bundle.rules, decision)
                book.financial_cursor = decision
            evaluated = evaluated or bool(batch)
        if evaluated:
            self.strategy_batches += 1
        self._commit_intents(additions)
        self._risk_timer(decision)

    def _health(self, sample: ClockSample):
        now = sample.utc_nano
        stale = not self.bars or now - self.bars[-1].end_nano > MAX_MARKET_AGE
        invalid = stale or sample.reasons() or self.pauses or not self.connected or self.conflicted
        if invalid:
            self._cancel_entries("MARKET_STALE" if stale else "CLOCK_OR_SOURCE_UNVERIFIED")
        for book in self.accounts.values():
            if book.barrier is not None:
                opening = decode_record(MarketBar, book.barrier["open"])
                reasons = set(book.barrier["incomplete_reasons"])
                if now - opening.start_nano > MAX_MARKET_AGE:
                    reasons.add("PROTECTIVE_RECEIPT_TIMEOUT")
                if not self.connected:
                    reasons.add("SOURCE_DISCONNECTED")
                if "SOURCE_GAP" in self.pauses:
                    reasons.add("SOURCE_GAP")
                if sample.reasons() or "CLOCK_UNVERIFIED" in self.pauses:
                    reasons.add("CLOCK_UNVERIFIED")
                book.barrier["incomplete_reasons"] = sorted(reasons)
        if sample.reasons() or "CLOCK_UNVERIFIED" in self.pauses:
            for book in self.accounts.values():
                book.debt.update(l.strategy_id for l in book.ledger._account.lots)
            return
        self._risk_timer(now)

    def _risk_timer(self, now: int):
        if not self.accounts:
            return
        bundle = self._bundle()
        inst = self.activation.bundle.instruments[0]
        additions = {mode: () for mode in self.accounts}
        for mode, book in self.accounts.items():
            if book.barrier is not None or now < max(book.financial_cursor, book.ledger._account.as_of_nano):
                continue
            for intent in tuple(book.pending.values()):
                if now >= intent.expires_at_nano or (intent.offset == "OPEN" and now >= intent.not_before_nano + MAX_OPEN_AGE):
                    self._cancel(book, intent.intent_id, "INTENT_EXPIRED" if now >= intent.expires_at_nano else "UNOBSERVED_OPEN")
                elif intent.offset == "OPEN" and now >= self.activation.models.valid_until_nano:
                    self._cancel(book, intent.intent_id, "MODEL_EXPIRED")
            for strategy_id, state in sorted(tuple(book.states.items())):
                account = book.ledger.snapshot(now)
                position = position_for(account, state.contract_id)
                if position is None or state.budget is None:
                    book.debt.discard(strategy_id)
                    continue
                deadlines = [v for v in (state.active_exit_nano, inst.delivery_cutoff_nano,
                                         inst.last_trade_nano, inst.expiry_nano) if v is not None]
                mark = book.ledger._marks.get(inst.contract_id)
                stale = mark is None or mark.status != "CURRENT" or now - mark.as_of_nano > MAX_MARKET_AGE
                if not (account.killed or stale or state.phase == "EXITING" or strategy_id in book.debt
                        or (deadlines and now >= min(deadlines))):
                    continue
                book.debt.add(strategy_id)
                for old in tuple(book.pending.values()):
                    if old.episode_id == state.episode_id and old.offset == "OPEN":
                        self._cancel(book, old.intent_id, "RISK_CANCEL")
                if any(i.episode_id == state.episode_id and i.offset != "OPEN" for i in book.pending.values()):
                    continue
                starts = [t for s in bundle.sessions if s.product == inst.product and s.evidence_status != "UNKNOWN"
                          for t in range(s.start_nano, s.end_nano, MINUTE) if t > now]
                if not starts:
                    book.states[strategy_id] = replace(book.states[strategy_id], phase="EXITING")
                    continue
                reason = "ROLL_EXIT" if inst.delivery_cutoff_nano is not None and now >= inst.delivery_cutoff_nano else "TIME_EXIT"
                proposal = _hard_exit(book.states[strategy_id], account, now, min(starts), reason)
                timer = self._timer_decision(book, book.states[strategy_id], account, proposal)
                self._record_decision(book, timer, account, risk_state=book.states[strategy_id])
                additions[mode] += self._new_intents(book, proposal, timer, now)
                book.ledger.sync_reservations(tuple(book.states.values()), bundle.rules, now)
                book.financial_cursor = now
        self._commit_intents(additions)

    def _timer_decision(self, book: PaperAccount, state: StrategyState, account: AccountSnapshot, proposal):
        now = proposal.decision_nano
        spec = next(s for s in self.activation.specs if s.strategy_id == state.strategy_id)
        return DecisionRecord(stable_id("risk_timer_decision", canonical_json_bytes(
            [state.episode_id, now, proposal.proposal_id, record_hash(account), record_hash(book.policy)])),
            opportunity_id(state.contract_id, now, spec.max_holding, account), now,
            book.policy.research_policy_version, record_hash(book.policy), book.policy.purpose, (),
            book.policy.routing_mode, (proposal,), (GateDecision(proposal.proposal_id, True, (), (), now),),
            {proposal.proposal_id: 1.0, "CASH": 0.0}, book.policy.seed, None,
            proposal.proposal_id, "MANAGE_POSITION", ("RISK_TIMER_EXIT",), record_hash(account))

    def finish(self, reason: str):
        self._usable()
        self.terminal_reason = reason
        self._cancel_entries("RUN_TERMINATED")
        for book in self.accounts.values():
            if book.barrier is not None:
                book.barrier["incomplete_reasons"] = sorted(set(book.barrier["incomplete_reasons"]) | {"RUN_TERMINATED"})
        self._seal("TERMINAL", {"reason": reason, "remaining_risk": self._remaining_risk()})
        return self.write_report()

    def _remaining_risk(self):
        return {mode: {"lots": sum(l.lots for l in b.ledger._account.lots), "pending": sorted(b.pending),
                       "exit_debt": sorted(b.debt), "protective_barrier": b.barrier is not None}
                for mode, b in self.accounts.items()}

    def report(self):
        records = {mode: {name: [encode_record(row) for row in rows]
                          for name, rows in values.items()} for mode, values in self.records.items()}
        return report_from_prefix(self.journal, host=self._envelope(), records=records,
                                  checkpoints=self._checkpoints())

    def write_report(self):
        self._usable()
        return self.journal.report(self.report())

    def _usable(self):
        if self.poisoned:
            raise ForwardPaperError("RECOVERY_REQUIRED")

    def _restore(self):
        types = {"decisions": DecisionRecord, "intents": OrderIntent, "fills": Fill,
                 "executions": ExecutionResult}
        from ak_trading.v2.contracts import FeatureSnapshot, MarketForecast, RegimeAssessment
        types.update(features=FeatureSnapshot, forecasts=MarketForecast, regimes=RegimeAssessment)
        if not self.journal.segments:
            raise ForwardPaperError("EMPTY_COMMITTED_PREFIX")
        seen_s = {}
        confirmations, sealed_intents, used_s = {}, {}, set()
        for manifest, segment in self.journal.segments:
            for mode, records in segment["RECORDS.json"].items():
                if mode not in self.records:
                    raise ForwardPaperError("RECOVERY_POLICY_MISMATCH")
                for name, rows in records.items():
                    self.records[mode][name].extend(decode_record(types[name], row) for row in rows)
            if manifest["kind"] == "INTENT_S":
                seen_s[manifest["manifest_sha256"]] = (manifest, segment)
                for mode, ids in segment["EVENT.json"]["intent_ids"].items():
                    for identity in ids:
                        if (mode, identity) in sealed_intents:
                            raise ForwardPaperError("DUPLICATE_INTENT_SEAL")
                        sealed_intents[mode, identity] = manifest["manifest_sha256"]
            if manifest["kind"] == "COMMIT_C":
                self._verify_confirmation(segment["EVENT.json"], seen_s)
                confirmation = segment["EVENT.json"]
                s_hash = confirmation["s_manifest_sha256"]
                if s_hash in used_s or manifest["parent_sha256"] != s_hash:
                    raise ForwardPaperError("CONFIRMATION_SEQUENCE_MISMATCH")
                used_s.add(s_hash)
                for mode, ids in confirmation["intent_ids"].items():
                    for identity in ids:
                        confirmations[mode, identity] = {"status": confirmation["qualifications"][identity],
                            "s_manifest_sha256": s_hash, "s_completed": confirmation["s_completed"]}
        _, last = self.journal.segments[-1]
        host = last["HOST.json"]
        for name in ("source_watermark", "source_chain", "event_hashes", "batch_hashes", "raw_closed", "facts", "forward_bar_count", "strategy_batches", "terminal_reason", "connected", "conflicted", "raw_queue", "open_sources"):
            setattr(self, name, host[name])
        self.pauses = set(host["pauses"])
        self.last_clock = None if host["last_clock"] is None else ClockSample(**host["last_clock"])
        self.bars = [decode_record(MarketBar, row) for row in host["bars"]]
        self._verify_host_sources()
        if set(last["CHECKPOINTS.json"]) != set(self.accounts) or set(host["accounts"]) != set(self.accounts):
            raise ForwardPaperError("RECOVERY_ACCOUNT_SET_MISMATCH")
        for mode, book in self.accounts.items():
            cp = decode_record(LedgerCheckpoint, last["CHECKPOINTS.json"][mode])
            expected_bindings = tuple(sorted(self.activation.bindings, key=lambda b: b.binding_sha256))
            if cp.exit_bindings != expected_bindings:
                raise ForwardPaperError("RECOVERY_COMPLETE_BINDING_MISMATCH")
            state = host["accounts"][mode]
            if state["account_id"] != book.ledger._account.account_id or state["policy_sha256"] != record_hash(book.policy):
                raise ForwardPaperError("RECOVERY_POLICY_ACCOUNT_MISMATCH")
            records = self.records[mode]
            self._verify_decision_sources(book, records)
            verify_committed_prefix(cp, self._bundle().bars, tuple(records["fills"]), tuple(records["intents"]))
            verify_restore_sources(cp, self._bundle())
            expected_capacity = {}
            for fill in records["fills"]:
                key = f"{fill.contract_id}|{fill.execution_nano}|BASE"
                expected_capacity[key] = expected_capacity.get(key, 0) + fill.lots
                if not any(original["event"]["source_nano"] == fill.execution_nano
                           and original["event"]["data"].get("contract_id") == fill.contract_id
                           for original in self.open_sources.values()):
                    raise ForwardPaperError("RECOVERY_FILL_OPEN_ORIGINAL_MISSING")
            if (any(v != expected_capacity.get(k, 0) for k, v in cp.capacity_used.items())
                    or set(expected_capacity) - set(cp.capacity_used)
                    or set(cp.processed_event_ids) - set(self.open_sources)):
                raise ForwardPaperError("RECOVERY_CAPACITY_EVENT_MISMATCH")
            book.ledger = ContinuousLedger.restore(cp, self.activation.bundle.instruments, self.activation.bundle.rules,
                book.policy, self.activation.contract_lock_sha256, registry_hash_for(self.activation.specs),
                model_hashes_for(self.activation.models, self.activation.payoff, self.activation.reference, self.activation.seasonality),
                {}, exit_registry_sha256=exit_registry_hash_for(self.activation.exit_specs))
            book.states = {s.strategy_id: s for s in cp.states}
            book.pending = {i.intent_id: i for i in cp.pending_intents}
            book.remaining = dict(cp.pending_remaining_lots)
            book.admission, book.barrier = state["admission"], state["barrier"]
            book.financial_cursor, book.debt = state["financial_cursor"], set(state["debt"])
            if book.financial_cursor < cp.account.as_of_nano:
                raise ForwardPaperError("RECOVERY_FINANCIAL_CURSOR_MISMATCH")
            for intent_id in book.pending:
                admission = book.admission.get(intent_id)
                if admission is None or admission.get("status") not in ("SEALED_UNCONFIRMED", "CONFIRMED_ELIGIBLE"):
                    raise ForwardPaperError("RECOVERY_PENDING_ADMISSION_MISMATCH")
                expected = confirmations.get((mode, intent_id))
                if admission["status"] == "CONFIRMED_ELIGIBLE":
                    if expected is None or any(admission.get(k) != v for k, v in expected.items()):
                        raise ForwardPaperError("RECOVERY_CONFIRMATION_PROOF_MISSING")
                elif (mode, intent_id) not in sealed_intents or expected is not None:
                    raise ForwardPaperError("RECOVERY_UNCONFIRMED_PROOF_MISMATCH")
            for fill in records["fills"]:
                expected = confirmations.get((mode, fill.intent_id))
                if expected is None or expected["status"] != "CONFIRMED_ELIGIBLE":
                    raise ForwardPaperError("RECOVERY_FILL_WITHOUT_CONFIRMED_INTENT")
            if book.barrier is not None:
                self._verify_barrier(book)
        for mode, records in self.records.items():
            self.record_offsets[mode] = {name: len(rows) for name, rows in records.items()}
        unconfirmed = [(mode, intent_id) for mode, book in self.accounts.items()
                       for intent_id in book.pending if book.admission[intent_id]["status"] == "SEALED_UNCONFIRMED"]
        if unconfirmed:
            for mode, intent_id in unconfirmed:
                self._cancel(self.accounts[mode], intent_id, "UNCONFIRMED_AFTER_CRASH")
            self._seal("UNCONFIRMED_RECONCILE", {"reason": "UNCONFIRMED_AFTER_CRASH", "intents": unconfirmed})
        if self.journal.orphans:
            self.facts.append({"kind": "ORPHAN_DISPOSITION", "paths": self.journal.orphans, "action": "PRESERVED_NOT_COMMITTED"})
            self._seal("ORPHAN_DISPOSITION", self.facts[-1])
        # An OPEN persisted before a crash is history on restart. A queued
        # CLOSED still supplies data, but never earns a retrospective decision.
        self._drain(recovery=True)
        self.recovering = False

    def _verify_decision_sources(self, book: PaperAccount, records: Mapping):
        mode = book.policy.routing_mode
        decisions = {d.decision_id: d for d in records["decisions"]}
        if len(decisions) != len(records["decisions"]):
            raise ForwardPaperError("RECOVERY_DUPLICATE_DECISION")
        sources = [f for f in self.facts if f["kind"] == "DECISION" and f.get("mode") == mode]
        if len(sources) != len(decisions) or {f["decision_id"] for f in sources} != set(decisions):
            raise ForwardPaperError("RECOVERY_DECISION_SOURCE_MISSING")
        source_chains = {0: content([self.run_id, "RECEIVE_START"])}
        chain = source_chains[0]
        for manifest, segment in self.journal.segments:
            if manifest["kind"] == "RECEIVE":
                original = segment["EVENT.json"]
                chain = content([chain, original])
                source_chains[original["source_sequence"]] = chain
        originals = {}
        for source in sources:
            decision = decisions[source["decision_id"]]
            before = decode_record(AccountSnapshot, source["account_before"])
            prefix = [encode_record(b) for b in self._bundle().bars
                      if b.end_nano <= decision.decision_nano and b.available_at_nano is not None
                      and b.available_at_nano <= decision.decision_nano]
            if (before.account_id != book.ledger._account.account_id
                    or before.as_of_nano != decision.decision_nano
                    or decision.account_before_hash != record_hash(before)
                    or source["account_before_sha256"] != record_hash(before)
                    or source["policy_sha256"] != record_hash(book.policy)
                    or source["input_prefix_sha256"] != content(prefix)
                    or source_chains.get(source["source_sequence"]) != source["source_chain"]
                    or source["candidates"] != [{"strategy_id": p.strategy_id, "proposal_id": p.proposal_id,
                        "action": p.action, "reasons": list(p.reasons)} for p in decision.proposals]):
                raise ForwardPaperError("RECOVERY_DECISION_SOURCE_MISMATCH")
            if source["origin"] == "STRATEGY":
                clock_evidence = source["evaluation_gate_clocks"]
                if set(clock_evidence) != {p.proposal_id for p in decision.proposals}:
                    raise ForwardPaperError("RECOVERY_DECISION_CLOCK_ORIGINAL_MISSING")
                gates = {g.proposal_id: g for g in decision.gates}
                for proposal in decision.proposals:
                    pair = clock_evidence[proposal.proposal_id]
                    e, g = ClockSample(**pair["evaluation"]), ClockSample(**pair["gate"])
                    if (not decision.decision_nano <= e.utc_nano <= g.utc_nano
                            or gates[proposal.proposal_id].checked_at_nano != g.utc_nano):
                        raise ForwardPaperError("RECOVERY_DECISION_CLOCK_MISMATCH")
                expected = choose_proposal(decision.proposals, decision.gates, before,
                    decision.opportunity_set_id, decision.decision_nano, book.policy, ())
                if decision != expected:
                    raise ForwardPaperError("RECOVERY_ROUTING_DECISION_MISMATCH")
            elif source["origin"] == "RISK_TIMER":
                if len(decision.proposals) != 1:
                    raise ForwardPaperError("RECOVERY_RISK_TIMER_MISMATCH")
                proposal = decision.proposals[0]
                state = decode_record(StrategyState, source["risk_state"])
                if (len(proposal.reasons) != 1 or proposal.reasons[0] not in ("TIME_EXIT", "ROLL_EXIT")
                        or proposal != _hard_exit(state, before, decision.decision_nano,
                                                 proposal.earliest_execution_nano, proposal.reasons[0])
                        or decision != self._timer_decision(book, state, before, proposal)):
                    raise ForwardPaperError("RECOVERY_RISK_TIMER_MISMATCH")
            else:
                raise ForwardPaperError("RECOVERY_DECISION_ORIGIN_INVALID")
            if decision.selected_proposal_id is not None:
                selected = next((p for p in decision.proposals if p.proposal_id == decision.selected_proposal_id), None)
                if selected is None:
                    raise ForwardPaperError("RECOVERY_SELECTED_PROPOSAL_MISSING")
                rule = visible_rule(self._bundle(), self.activation.contract_id, decision.decision_nano)
                native = {i.intent_id: i for i in intents_for_proposal(selected, decision.decision_id, before, rule)}
                if (len(set(source["intent_ids"])) != len(source["intent_ids"])
                        or set(source["intent_ids"]) - set(native)):
                    raise ForwardPaperError("RECOVERY_INTENT_SELECTED_DECISION_MISMATCH")
                for intent_id in source["intent_ids"]:
                    intent = native[intent_id]
                    if intent.intent_id in originals:
                        raise ForwardPaperError("RECOVERY_DUPLICATE_INTENT")
                    originals[intent.intent_id] = intent
            elif source["intent_ids"]:
                raise ForwardPaperError("RECOVERY_INTENT_SELECTED_DECISION_MISMATCH")
        actual = {i.intent_id: i for i in records["intents"]}
        if len(actual) != len(records["intents"]) or actual != originals:
            raise ForwardPaperError("RECOVERY_INTENT_SELECTED_DECISION_MISMATCH")

    def _verify_host_sources(self):
        originals, batches, received_batches = {}, {}, {}
        chain = content([self.run_id, "RECEIVE_START"])
        watermark = 0
        for manifest, segment in self.journal.segments:
            if manifest["kind"] != "RECEIVE":
                continue
            batch = segment["EVENT.json"]
            sequence = batch["source_sequence"]
            if (batch["run_id"] != self.run_id or type(sequence) is not int or sequence <= watermark
                    or batch["payload_sha256"] != content(batch["events"])):
                raise ForwardPaperError("RECOVERY_RECEIVE_ORIGINAL_MISMATCH")
            watermark = sequence
            batches[str(sequence)] = batch["payload_sha256"]
            received_batches[sequence] = batch
            chain = content([chain, batch])
            for event in batch["events"]:
                # The first received identity is its immutable original. Later
                # conflicting receives remain audit evidence, never replacements.
                originals.setdefault(event["event_id"], {"event": event, "clock": batch["clock"],
                                                       "source_sequence": sequence})
        if (self.source_chain != chain or self.source_watermark != watermark or self.batch_hashes != batches):
            raise ForwardPaperError("RECOVERY_RECEIVE_CHAIN_MISMATCH")
        for event_id, digest in self.event_hashes.items():
            original = originals.get(event_id)
            if original is None or content(original["event"]) != digest:
                raise ForwardPaperError("RECOVERY_PROCESSED_SOURCE_MISMATCH")
        for item in self.raw_queue:
            batch = received_batches.get(item["source_sequence"])
            if batch is None or item["clock"] != batch["clock"] or item["event"] not in batch["events"]:
                raise ForwardPaperError("RECOVERY_QUEUE_SOURCE_MISMATCH")
        for collection, kind in ((self.open_sources, "OPEN"), (self.raw_closed, "CLOSED")):
            for event_id, item in collection.items():
                original = originals.get(event_id)
                if (original is None or original["event"]["kind"] != kind
                        or item != {"event": original["event"], "received": original["clock"]}
                        or event_id not in self.event_hashes):
                    raise ForwardPaperError("RECOVERY_MARKET_ORIGINAL_MISMATCH")
        for bar in self.bars:
            candidates = (item for item in self.raw_closed.values()
                          if item["event"]["source_nano"] == bar.start_nano)
            if not any(self._market_bar(SourceEvent(**item["event"]), ClockSample(**item["received"]), closed=True) == bar
                       for item in candidates):
                raise ForwardPaperError("RECOVERY_CLOSED_BAR_ORIGINAL_MISMATCH")

    def _verify_confirmation(self, confirmation: Mapping, seen_s: Mapping):
        if set(confirmation) != {"s_manifest_sha256", "s_sequence", "intent_ids", "s_completed",
                                 "qualifications", "original_financial_parent"}:
            raise ForwardPaperError("CONFIRMATION_FIELD_SET_MISMATCH")
        pair = seen_s.get(confirmation.get("s_manifest_sha256"))
        if pair is None:
            raise ForwardPaperError("CONFIRMATION_S_REFERENCE_MISMATCH")
        manifest, original = pair
        expected = original["EVENT.json"].get("intent_ids")
        if (not isinstance(expected, dict) or not expected or set(expected) - set(self.accounts)
                or any(not isinstance(ids, list) or not ids or len(set(ids)) != len(ids) for ids in expected.values())
                or confirmation.get("intent_ids") != expected or confirmation.get("s_sequence") != manifest["sequence"]):
            raise ForwardPaperError("CONFIRMATION_INTENT_SET_MISMATCH")
        if (set(confirmation.get("qualifications", {})) != {i for ids in expected.values() for i in ids}
                or confirmation.get("original_financial_parent") != {
                    mode: original["HOST.json"]["accounts"][mode]["financial_cursor"] for mode in expected}):
            raise ForwardPaperError("CONFIRMATION_FINANCIAL_PARENT_MISMATCH")
        sample = ClockSample(**confirmation["s_completed"])
        previous = original["HOST.json"]["last_clock"]
        reasons = sample.reasons(None if previous is None else ClockSample(**previous))
        for mode, ids in expected.items():
            intents = {row["intent_id"]: decode_record(OrderIntent, row)
                       for row in original["RECORDS.json"][mode]["intents"]}
            for intent_id in ids:
                intent = intents.get(intent_id)
                if intent is None:
                    raise ForwardPaperError("CONFIRMATION_ORIGINAL_INTENT_MISSING")
                timely = not reasons and sample.utc_nano + sample.uncertainty_ns < intent.not_before_nano
                status = "COMMIT_CLOCK_UNVERIFIED" if reasons else "CONFIRMED_ELIGIBLE" if timely else "MISSED_COMMIT_WINDOW"
                if confirmation["qualifications"].get(intent_id) != status:
                    raise ForwardPaperError("CONFIRMATION_CLOCK_QUALIFICATION_MISMATCH")

    def _receipt(self, book: PaperAccount, intent: OrderIntent, result: ExecutionResult, when: int):
        state, new_fills = apply_receipt(book.ledger, book.states[intent.strategy_id], intent,
            result, book.remaining[intent.intent_id], self.activation.bundle.rules, when)
        book.states[intent.strategy_id] = state
        records = self.records[book.policy.routing_mode]
        records["fills"].extend(new_fills)
        records["executions"].append(result)
        if result.status in ("FILLED", "CANCELLED", "EXPIRED"):
            book.pending.pop(intent.intent_id)
            book.remaining.pop(intent.intent_id)
            book.admission[intent.intent_id] = {**book.admission.get(intent.intent_id, {}), "terminal_status": result.status}
        else:
            book.remaining[intent.intent_id] = result.remaining_lots
        book.financial_cursor = max(book.financial_cursor, when)

    def _cancel(self, book: PaperAccount, intent_id: str, reason: str):
        intent = book.pending[intent_id]
        if intent.offset != "OPEN":
            book.debt.add(intent.strategy_id)
        self._receipt(book, intent, ExecutionResult(intent_id, (), book.remaining[intent_id], "CANCELLED", (reason,)), book.financial_cursor)
        book.admission[intent_id]["status"] = reason
        book.ledger.sync_reservations(tuple(book.states.values()), self.activation.bundle.rules, book.financial_cursor)

    def _commit_intents(self, additions: Mapping[str, tuple[OrderIntent, ...]]) -> None:
        intent_ids = {mode: [intent.intent_id for intent in rows] for mode, rows in additions.items() if rows}
        if not intent_ids:
            return
        for mode, rows in additions.items():
            book = self.accounts[mode]
            for intent in rows:
                if not intent.created_at_nano < intent.not_before_nano < intent.expires_at_nano:
                    raise ForwardPaperError("ORIGINAL_INTENT_WINDOW_INVALID")
                book.admission[intent.intent_id] = {"status": "SEALED_UNCONFIRMED"}
        s = self._seal("INTENT_S", {"intent_ids": intent_ids})
        try:
            self.fault("s_returned")
            completed = self.clock()
            if not isinstance(completed, ClockSample):
                raise ForwardPaperError("CLOCK_SAMPLE_REQUIRED")
            self.fault("s_completion_sampled")
        except BaseException:
            self.poisoned = True
            raise
        reasons = completed.reasons(self.last_clock)
        qualifications = {}
        for mode, ids in intent_ids.items():
            book = self.accounts[mode]
            for intent_id in ids:
                intent = book.pending[intent_id]
                status = ("COMMIT_CLOCK_UNVERIFIED" if reasons else "CONFIRMED_ELIGIBLE"
                          if completed.utc_nano + completed.uncertainty_ns < intent.not_before_nano else "MISSED_COMMIT_WINDOW")
                qualifications[intent_id] = status
                book.admission[intent_id] = {"status": status, "s_manifest_sha256": s["manifest_sha256"],
                                            "s_completed": completed.payload()}
                if status != "CONFIRMED_ELIGIBLE":
                    self._cancel(book, intent_id, status)
        confirmation = {"s_manifest_sha256": s["manifest_sha256"], "s_sequence": s["sequence"],
                        "intent_ids": intent_ids, "s_completed": completed.payload(),
                        "qualifications": qualifications,
                        "original_financial_parent": {mode: self.accounts[mode].financial_cursor for mode in intent_ids}}
        try:
            self.fault("before_c_seal")
            self._seal("COMMIT_C", confirmation)
            self.fault("c_returned")
        except BaseException:
            self.poisoned = True
            raise


