"""NF23 observer-domain SYNTHETIC benchmark workloads (C20 case interface).

Work order: ``docs/devplan-next-frontier-20260924-r1/work-orders/
NF23-observer-benchmark.md`` (H23.1-H23.6).  Every case built here measures
**in-process** observer components under the C20 harness
(``tools/benchmarks/contracts.py``):

* ``ak_trading.infiny_bridge.protocol.parse_json_body`` (H23.1 PARSE),
* ``ak_trading.infiny_bridge.protocol.normalize_payload`` (H23.1 NORMALIZE),
* ``ak_trading.infiny_bridge.service.BridgeService.analyze / heartbeat /
  status`` (H23.2 SERVICE_IN_PROCESS) with an isolated service instance.

Honest measurement scope (frozen per case in the manifests):

* No HTTP, no sockets, no ports (17778/17779 are never touched), no INFINIGO
  client, no trading objects, no deployed service (H23.6).
* The only clock is the harness ``perf_counter_ns`` around each operation;
  business time comes from :class:`SyntheticBusinessClock` advanced once per
  operation (H23.3: the business clock advances per protocol, never
  regresses, and never enters a digest).
* Service cases run a single evaluator worker with the raw sink **disabled**
  (H23.4 first-version default); the temp audit JSONL write+flush inside the
  run temporary directory IS inside the timed operation and is declared via
  ``measurement.includes_io=true``.
* Normal accepts, format rejections and the boundary maximum legal input are
  separate cases; rejection workloads report ``ok`` for *correctly produced*
  rejections and account accepted/rejected counts separately so rejection
  samples can never mix into the accept path (H23 counterexample class 7).
* The duplicate-batch replay (cache hit) case is measured and reported
  separately from the business-processing case (H23.3).

Everything is deterministic: payload bytes, business clock readings, receipt
and audit content depend only on the frozen spec fixture
``tools/benchmarks/fixtures/observer/payload-spec.json`` and the manifest
seed - so smoke digests and per-batch formal digests are reproducible.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ak_trading.infiny_bridge.protocol import (
    MAX_BODY_BYTES,
    MAX_TICKS,
    ProtocolViolation,
    normalize_payload,
    parse_json_body,
)
from ak_trading.infiny_bridge.service import BridgeService

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SPEC_PATH = "tools/benchmarks/fixtures/observer/payload-spec.json"

EVALUATOR_NAME = "deterministic_hold_double"
ACCEPT_RESPONSE_REASON = "observing"


# ---------------------------------------------------------------------------
# deterministic business clock (H23.3) - advances only on explicit advance()
# ---------------------------------------------------------------------------


class SyntheticBusinessClock:
    """Business clock: fixed base, integer microsecond steps, no regression.

    ``now()``/``monotonic()`` are pure functions of the internal step counter,
    so every receipt/audit timestamp is reproducible.  The harness performance
    clock is never taken from here.
    """

    def __init__(self, base_wall: datetime, base_mono_s: float, step_us: int) -> None:
        self._base_wall = base_wall
        self._base_mono = float(base_mono_s)
        self._step_us = int(step_us)
        if self._step_us <= 0:
            raise ValueError("step_us must be positive")
        self._n = 0

    def advance(self) -> None:
        self._n += 1

    def rewind(self, steps: int = 1) -> None:
        # Test/probe helper simulating a wrong-epoch clock fault: receipts
        # sampled after a rewind are flagged _clock_regressed by the service.
        self._n -= int(steps)

    @property
    def steps(self) -> int:
        return self._n

    def now(self) -> datetime:
        return self._base_wall + timedelta(microseconds=self._step_us * self._n)

    def monotonic(self) -> float:
        return self._base_mono + (self._step_us * self._n) / 1_000_000.0


def _clock_from_spec(spec: Dict[str, Any]) -> SyntheticBusinessClock:
    clock = spec["clock"]
    return SyntheticBusinessClock(
        base_wall=datetime.fromisoformat(clock["base_wall"]),
        base_mono_s=float(clock["base_mono_s"]),
        step_us=int(clock["step_us_per_operation"]),
    )


# ---------------------------------------------------------------------------
# deterministic evaluator double (H23.2: explicitly named, deterministic)
# ---------------------------------------------------------------------------


class DeterministicHoldEvaluatorDouble:
    """Named deterministic evaluator double: always answers ``hold``.

    Bound into BridgeService as ``double.evaluate`` so the service ``status()``
    exposes the evaluator identity (``status()["observer"]``), binding the
    evaluator type into the measured facts.
    """

    name = EVALUATOR_NAME

    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, payload: Any) -> Dict[str, Any]:
        self.calls += 1
        return {"candidate_signal": "hold", "reason": "observing"}

    def status(self) -> Dict[str, Any]:
        return {"name": self.name, "signal": "hold", "calls": self.calls}

    __call__ = evaluate


# ---------------------------------------------------------------------------
# canonical JSON / digest helpers (same canonical form as C20 contracts)
# ---------------------------------------------------------------------------


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest_of(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(_canon(payload).encode("utf-8")).hexdigest()


def _count_audit_lines(path: Path) -> Tuple[int, Optional[str]]:
    try:
        data = path.read_bytes()
    except OSError:
        return -1, None
    if not data:
        return 0, hashlib.sha256(b"").hexdigest()
    return (
        data.count(b"\n"),
        hashlib.sha256(data).hexdigest(),
    )


# ---------------------------------------------------------------------------
# SYNTHETIC payload construction (driven by the frozen spec fixture)
# ---------------------------------------------------------------------------


def _tick(base: datetime, index: int, field_set: str, stride_us: int) -> Dict[str, Any]:
    when = (base + timedelta(microseconds=stride_us * (index + 1))).isoformat()
    last = 4000 + index
    tick: Dict[str, Any] = {"time": when, "last": last, "volume": 10 + index}
    if field_set == "minimal":
        pass  # minimum legal tick: one valid price plus time
    elif field_set == "standard":
        tick.update(
            {
                "bid1": last - 1,
                "ask1": last + 1,
                "open": 3995,
                "high": 4012,
                "low": 3988,
                "close": 4000,
                "open_interest": 310000 + index,
                "turnover": 1000000 + index * 10,
                "upper_limit": 4500,
                "lower_limit": 3500,
            }
        )
    elif field_set == "wide":
        for level in range(1, 6):
            tick["bid%d" % level] = last - level
            tick["ask%d" % level] = last + level
        for level in range(1, 6):
            tick["bid_volume%d" % level] = 5 + level
            tick["ask_volume%d" % level] = 6 + level
        tick.update(
            {
                "open": 3995,
                "high": 4012,
                "low": 3988,
                "close": 4000,
                "open_interest": 310000 + index,
                "turnover": 1000000 + index * 10,
                "upper_limit": 4500,
                "lower_limit": 3500,
                "pre_close": 3998,
                "pre_settlement": 3997,
            }
        )
    else:
        raise ValueError("unknown tick field set: %r" % (field_set,))
    return tick


def _quote(base: datetime, offset_s: float) -> Dict[str, Any]:
    return {
        "time": (base + timedelta(seconds=offset_s)).isoformat(),
        "status": "available",
        "last": 4000,
        "bid1": 3999,
        "ask1": 4001,
        "volume": 123,
        "open_interest": 310000,
    }


def _envelope(
    spec: Dict[str, Any],
    seed: int,
    batch_id: str,
    ticks: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    shared = spec["shared"]
    base = datetime.fromisoformat(spec["clock"]["base_wall"])
    # quote timestamp is placed in the past (base - 0.25 s) so the synthetic
    # freshness window can never classify it stale or future at any op index
    return {
        "schema_version": 2,
        "batch_id": batch_id,
        "exchange": shared["exchange"],
        "instrument_id": shared["instrument_id"],
        "ticks": list(ticks),
        "trading_day": shared["trading_day"],
        "market_status": shared["market_status"],
        "quote": _quote(base, -0.25),
        "position": {
            "status": "ready",
            "long": {"total": 0, "today": 0, "yesterday": 0},
            "short": {"total": 0, "today": 0, "yesterday": 0},
            "net": 0,
        },
        "account": {
            "status": "ready",
            "available": 100000,
            "balance": 120000,
            "risk": 0,
        },
        "contract": {
            "status": "ready",
            "price_tick": shared["contract_price_tick"],
            "upper_limit": shared["contract_upper_limit"],
            "lower_limit": shared["contract_lower_limit"],
        },
        "active_orders": {"status": "ready", "count": 0},
        "runtime": {
            "status": "available",
            "observer_only": True,
            "enable_trading": False,
            "framework_trading": False,
            "order_submission_count": 0,
        },
        "connection": {
            "instance_id": "nf23-bench-instance",
            "session_epoch": "nf23-epoch-1",
            "request_id": "nf23-req-0001",
            "sequence": 1,
            "sent_at": (base - timedelta(seconds=1)).isoformat(),
            "snapshots": {
                "quote": _snapshot(base, -0.5, "snap-quote"),
                "account": _snapshot(base, -0.5, "snap-account"),
            },
        },
        "orders": [],
        "trades": [],
        "errors": [],
        "observed_orders": {
            "status": "open",
            "coverage": "callbacks_since_start",
            "window_started_at": (base - timedelta(seconds=5)).isoformat(),
            "truncated": False,
            "count": 0,
            "pending_count": 0,
            "unknown_status_count": 0,
            "capacity": 64,
        },
        "observed_trades": {
            "status": "open",
            "coverage": "callbacks_since_start",
            "window_started_at": (base - timedelta(seconds=5)).isoformat(),
            "truncated": False,
            "count": 0,
            "pending_count": 0,
            "unknown_status_count": 0,
            "capacity": 64,
        },
        "capabilities": {
            "native_candidate_names": ["QueryMarginRate"],
            "available_values": {"OpenRatioByMoney": 0.05},
        },
        "metadata": {"source": "nf23-synthetic-benchmark", "seed": int(seed)},
    }


def _snapshot(base: datetime, offset_s: float, snapshot_id: str) -> Dict[str, Any]:
    stamp = (base - timedelta(seconds=offset_s)).isoformat()
    return {
        "snapshot_id": snapshot_id,
        "source": "simulation",
        "sampled_at": stamp,
        "source_at": stamp,
        "received_at": stamp,
    }


def _ticks_from_spec(spec: Dict[str, Any], case_spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    base = datetime.fromisoformat(spec["clock"]["base_wall"])
    stride = int(spec["clock"]["tick_stride_us"])
    count = int(case_spec["ticks"])
    if not 1 <= count <= MAX_TICKS:
        raise ValueError("spec tick count out of protocol bounds: %d" % count)
    return [
        _tick(base + timedelta(seconds=float(spec["clock"]["tick_offset_s"]) - 1.0), i, case_spec["tick_field_set"], stride)
        for i in range(count)
    ]


def build_typical_payload(spec: Dict[str, Any], seed: int, case_key: str) -> Dict[str, Any]:
    case_spec = spec["cases"][case_key]
    return _envelope(spec, seed, case_spec["fixed_batch_id"], _ticks_from_spec(spec, case_spec))


def build_max_legal_dict(spec: Dict[str, Any], seed: int, case_key: str) -> Dict[str, Any]:
    """Boundary maximum legal *normalize* input: MAX_TICKS wide ticks."""
    case_spec = spec["cases"][case_key]
    if int(case_spec["ticks"]) != MAX_TICKS:
        raise ValueError("normalize_max_legal spec must use exactly MAX_TICKS ticks")
    return _envelope(spec, seed, case_spec["fixed_batch_id"], _ticks_from_spec(spec, case_spec))


def build_padded_max_body(spec: Dict[str, Any], seed: int, case_key: str) -> bytes:
    """Boundary maximum legal *parse* input: body of exactly MAX_BODY_BYTES.

    A single legal top-level padding string grows the canonical envelope to
    the exact protocol byte ceiling (parse_json_body rejects one byte more).
    """
    case_spec = spec["cases"][case_key]
    envelope = _envelope(spec, seed, case_spec["fixed_batch_id"], _ticks_from_spec(spec, case_spec))
    base_body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    # serialized form adds ,"nf23_padding":"<value>"  ->  19 bytes of syntax
    room = MAX_BODY_BYTES - len(base_body) - len(',"nf23_padding":""')
    if room < 0:
        raise ValueError("envelope already exceeds MAX_BODY_BYTES")
    envelope["nf23_padding"] = "p" * room
    body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    if len(body) != MAX_BODY_BYTES:
        raise ValueError("padded body is %d bytes, expected %d" % (len(body), MAX_BODY_BYTES))
    return body


PARSE_REJECT_BODIES: Dict[str, bytes] = {
    "invalid_json": b'{"schema_version":2,"ticks":[{"time":"2026-09-24T0',
    "duplicate_key": b'{"schema_version":2,"schema_version":3}',
    "non_finite_number": b'{"schema_version":2,"last":NaN}',
}


def build_normalize_reject_payloads(
    spec: Dict[str, Any], seed: int, case_key: str
) -> List[Tuple[str, Dict[str, Any]]]:
    """Deterministic envelopes that normalize_payload must reject per flavor."""
    case_spec = spec["cases"][case_key]
    flavors = list(case_spec["flavors"])
    base = datetime.fromisoformat(spec["clock"]["base_wall"])
    good_ticks = _ticks_from_spec(spec, spec["cases"]["normalize_accept_typical_inprocess"])
    out: List[Tuple[str, Dict[str, Any]]] = []
    for flavor in flavors:
        if flavor == "invalid_tick":
            envelope = _envelope(spec, seed, "nf23-rej-invalid-tick", good_ticks)
            envelope["ticks"] = [
                {"time": good_ticks[0]["time"], "volume": 5}  # no last/bid1/ask1
            ]
        elif flavor == "ticks_limit":
            envelope = _envelope(spec, seed, "nf23-rej-ticks-limit", good_ticks)
            tiny = {"time": (base + timedelta(seconds=0.5)).isoformat(), "last": 4000}
            envelope["ticks"] = [dict(tiny)] * (MAX_TICKS + 1)
        elif flavor == "invalid_quote":
            envelope = _envelope(spec, seed, "nf23-rej-invalid-quote", good_ticks)
            envelope["quote"] = dict(envelope["quote"], bid1=4002, ask1=4001)
        else:
            raise ValueError("unknown normalize reject flavor: %r" % (flavor,))
        out.append((flavor, envelope))
    return out


def build_heartbeat_payload() -> Dict[str, Any]:
    return {"schema_version": 2, "heartbeat": True}


# ---------------------------------------------------------------------------
# workload base
# ---------------------------------------------------------------------------


class _ObserverWorkloadBase:
    """Shared machinery: spec loading, fact accounting, frozen verify rules."""

    case_key: str = ""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        self.seed = int(ctx.seed)
        self.spec_path = REPO_ROOT / str(ctx.params.get("payload_spec", DEFAULT_SPEC_PATH))
        declared = str(ctx.params.get("case", ""))
        if declared and declared.rsplit("/", 1)[-1] != self.case_key.rsplit("/", 1)[-1]:
            raise ValueError(
                "manifest case %r does not match workload case %r" % (declared, self.case_key)
            )
        self.spec: Dict[str, Any] = {}
        self.case_spec: Dict[str, Any] = {}
        self.clock: Optional[SyntheticBusinessClock] = None
        self._facts: List[List[Any]] = []
        self._ops = 0
        self._accepted = 0
        self._rejected = 0
        self._expected_rejections = 0

    # -- lifecycle ---------------------------------------------------------

    def setup(self, ctx: Any) -> None:
        with open(self.spec_path, "r", encoding="utf-8") as handle:
            self.spec = json.load(handle)
        if self.spec.get("schema") != "ak-nf23-observer-payload-spec/1.0.0":
            raise ValueError("unsupported payload spec schema")
        self.case_spec = self.spec["cases"][self.case_key]
        self.clock = _clock_from_spec(self.spec)
        self._facts = []
        self._ops = 0
        self._accepted = 0
        self._rejected = 0

    def teardown(self, ctx: Any) -> None:
        pass  # subclasses close service resources

    # -- accounting (probe surface for the seven H23 counterexamples) ------

    def _record(self, index: int, accepted: bool, *, response: Optional[Dict[str, Any]] = None,
                reason: Optional[str] = None) -> None:
        """Classify one completed operation; verify re-checks the split.

        A rejection recorded with ``accepted=True`` (or vice versa) is caught
        by verify as REJECTION_MIXED (H23 counterexample class 7).
        """
        if accepted:
            effective = response["reason"] if response is not None else reason
            if effective is not None and effective not in (ACCEPT_RESPONSE_REASON, "heartbeat"):
                # a non-clean reason recorded as accepted = mixing fault
                self._rejected += 1
                self._facts.append([index, "MIXED", effective])
                return
            self._accepted += 1
            self._facts.append([index, "accept", response["signal"] if response else None,
                                response["reason"] if response else reason])
        else:
            self._rejected += 1
            self._facts.append([index, "reject", reason])

    def _digest(self, final: Dict[str, Any]) -> str:
        return _digest_of({"facts": self._facts, "final": final})


# ---------------------------------------------------------------------------
# H23.1 PARSE workloads
# ---------------------------------------------------------------------------


class ParseAcceptWorkload(_ObserverWorkloadBase):
    case_key = "parse_accept_typical_inprocess"

    def setup(self, ctx: Any) -> None:
        super().setup(ctx)
        self.body = json.dumps(
            build_typical_payload(self.spec, self.seed, self.case_key), separators=(",", ":")
        ).encode("utf-8")
        self.key_count = len(json.loads(self.body.decode("utf-8")))
        self.ticks = int(self.case_spec["ticks"])

    def operation(self, ctx: Any) -> Dict[str, Any]:
        assert self.clock is not None
        self.clock.advance()
        parsed = parse_json_body(self.body)
        self._record(self._ops, True, reason=ACCEPT_RESPONSE_REASON)
        self._ops += 1
        self._facts[-1].extend([len(self.body), len(parsed), len(parsed.get("ticks", ()))])
        return {"ok": True, "units": 1}

    def verify(self, ctx: Any) -> Dict[str, Any]:
        reasons: List[str] = []
        if self._ops <= 0:
            reasons.append("no operations executed")
        if self._rejected:
            reasons.append("REJECTION_MIXED: %d rejections in accept-only case" % self._rejected)
        if self._accepted != self._ops:
            reasons.append("accept accounting %d != ops %d" % (self._accepted, self._ops))
        for fact in self._facts:
            # fact layout: [index, "accept", None, reason, body_bytes, keys, ticks]
            if len(fact) < 7 or fact[6] != self.ticks:
                reasons.append("unexpected parsed tick count in fact %r" % (fact[:3],))
                break
            if fact[4] != len(self.body):
                reasons.append("body byte count changed: %r" % (fact[4],))
                break
        final = {
            "body_bytes": len(self.body),
            "top_level_keys": self.key_count,
            "ticks": self.ticks,
            "ops": self._ops,
            "accepted": self._accepted,
            "rejected": self._rejected,
        }
        return {
            "status": "PASS" if not reasons else "FAIL",
            "digest": self._digest(final),
            "summary": final,
            "fail_reasons": reasons,
        }


class ParseRejectMalformedWorkload(_ObserverWorkloadBase):
    """Format-rejection case: every op must raise ProtocolViolation.

    A rejection correctly produced is the successful work unit; accepted/rejected
    accounting stays split so these samples never enter the accept case.
    """

    case_key = "parse_reject_malformed_inprocess"

    def setup(self, ctx: Any) -> None:
        super().setup(ctx)
        flavors = list(self.case_spec["flavors"])
        self.bodies = [(f, PARSE_REJECT_BODIES[f]) for f in flavors]

    def operation(self, ctx: Any) -> Dict[str, Any]:
        assert self.clock is not None
        self.clock.advance()
        flavor, body = self.bodies[self._ops % len(self.bodies)]
        try:
            parse_json_body(body)
        except ProtocolViolation as exc:
            self._record(self._ops, False, reason=exc.reason)
            self._ops += 1
            self._facts[-1].append(flavor)
            return {"ok": True, "units": 1}
        self._record(self._ops, False, reason="NOT_REJECTED")
        self._ops += 1
        return {"ok": False, "units": 0, "reason": "malformed body was accepted"}

    def verify(self, ctx: Any) -> Dict[str, Any]:
        reasons: List[str] = []
        if self._ops <= 0:
            reasons.append("no operations executed")
        if self._accepted:
            reasons.append("rejection case recorded %d accepts" % self._accepted)
        if self._rejected != self._ops:
            reasons.append("rejection accounting %d != ops %d" % (self._rejected, self._ops))
        flavors = {f for f, _ in self.bodies}
        for fact in self._facts:
            if (
                len(fact) < 4
                or fact[3] not in flavors
                or fact[2] == "NOT_REJECTED"
                or fact[2] not in flavors
            ):
                reasons.append("body not rejected as expected: %r" % (fact,))
                break
        final = {
            "ops": self._ops,
            "accepted": self._accepted,
            "rejected": self._rejected,
            "flavors": sorted(flavors),
        }
        return {
            "status": "PASS" if not reasons else "FAIL",
            "digest": self._digest(final),
            "summary": final,
            "fail_reasons": reasons,
        }


class ParseMaxLegalWorkload(_ObserverWorkloadBase):
    case_key = "parse_max_legal_inprocess"

    def setup(self, ctx: Any) -> None:
        super().setup(ctx)
        self.body = build_padded_max_body(self.spec, self.seed, self.case_key)
        self.ticks = int(self.case_spec["ticks"])

    def operation(self, ctx: Any) -> Dict[str, Any]:
        assert self.clock is not None
        self.clock.advance()
        parsed = parse_json_body(self.body)
        self._record(self._ops, True, reason=ACCEPT_RESPONSE_REASON)
        self._ops += 1
        self._facts[-1].extend([len(self.body), len(parsed.get("ticks", ()))])
        return {"ok": True, "units": 1}

    def verify(self, ctx: Any) -> Dict[str, Any]:
        reasons: List[str] = []
        if self._ops <= 0:
            reasons.append("no operations executed")
        if self._rejected:
            reasons.append("REJECTION_MIXED: %d rejections in accept-only case" % self._rejected)
        if len(self.body) != MAX_BODY_BYTES:
            reasons.append("body not at byte boundary: %d" % len(self.body))
        for fact in self._facts:
            if len(fact) < 6 or fact[4] != MAX_BODY_BYTES or fact[5] != self.ticks:
                reasons.append("unexpected parsed boundary fact")
                break
        final = {
            "body_bytes": len(self.body),
            "max_body_bytes": MAX_BODY_BYTES,
            "ticks": self.ticks,
            "ops": self._ops,
            "accepted": self._accepted,
            "rejected": self._rejected,
        }
        return {
            "status": "PASS" if not reasons else "FAIL",
            "digest": self._digest(final),
            "summary": final,
            "fail_reasons": reasons,
        }


# ---------------------------------------------------------------------------
# H23.1 NORMALIZE workloads
# ---------------------------------------------------------------------------


class NormalizeAcceptWorkload(_ObserverWorkloadBase):
    case_key = "normalize_accept_typical_inprocess"

    def setup(self, ctx: Any) -> None:
        super().setup(ctx)
        self.payload = build_typical_payload(self.spec, self.seed, self.case_key)
        self.ticks = int(self.case_spec["ticks"])

    def operation(self, ctx: Any) -> Dict[str, Any]:
        assert self.clock is not None
        self.clock.advance()
        normalized = normalize_payload(dict(self.payload))
        blocked = normalized.blocked_reason
        accepted = blocked is None
        self._record(
            self._ops,
            accepted,
            reason=None if accepted else blocked,
        )
        if accepted:
            self._facts[-1].extend(
                [normalized.payload_hash, normalized.tick_count, normalized.exchange,
                 normalized.instrument, len(normalized.payload)]
            )
        self._ops += 1
        return {"ok": True, "units": 1}

    def verify(self, ctx: Any) -> Dict[str, Any]:
        reasons: List[str] = []
        if self._ops <= 0:
            reasons.append("no operations executed")
        if self._rejected:
            reasons.append("REJECTION_MIXED: %d rejections in accept-only case" % self._rejected)
        if self._accepted != self._ops:
            reasons.append("accept accounting %d != ops %d" % (self._accepted, self._ops))
        hashes = {f[4] for f in self._facts if len(f) > 4}
        if len(hashes) != 1:
            reasons.append("normalized payload hash not constant: %d distinct" % len(hashes))
        final = {
            "ops": self._ops,
            "accepted": self._accepted,
            "rejected": self._rejected,
            "ticks": self.ticks,
            "payload_hash": sorted(hashes)[0] if len(hashes) == 1 else None,
        }
        return {
            "status": "PASS" if not reasons else "FAIL",
            "digest": self._digest(final),
            "summary": final,
            "fail_reasons": reasons,
        }


class NormalizeRejectWorkload(_ObserverWorkloadBase):
    case_key = "normalize_reject_violations_inprocess"

    def setup(self, ctx: Any) -> None:
        super().setup(ctx)
        self.payloads = build_normalize_reject_payloads(self.spec, self.seed, self.case_key)

    def operation(self, ctx: Any) -> Dict[str, Any]:
        assert self.clock is not None
        self.clock.advance()
        flavor, payload = self.payloads[self._ops % len(self.payloads)]
        try:
            normalize_payload(dict(payload))
        except ProtocolViolation as exc:
            self._record(self._ops, False, reason=exc.reason)
            self._ops += 1
            self._facts[-1].append(flavor)
            return {"ok": True, "units": 1}
        self._record(self._ops, False, reason="NOT_REJECTED")
        self._ops += 1
        return {"ok": False, "units": 0, "reason": "violating payload was normalized"}

    def verify(self, ctx: Any) -> Dict[str, Any]:
        reasons: List[str] = []
        if self._ops <= 0:
            reasons.append("no operations executed")
        if self._accepted:
            reasons.append("rejection case recorded %d accepts" % self._accepted)
        if self._rejected != self._ops:
            reasons.append("rejection accounting %d != ops %d" % (self._rejected, self._ops))
        expected_flavors = {f for f, _ in self.payloads}
        for fact in self._facts:
            if (
                len(fact) < 4
                or fact[3] not in expected_flavors
                or fact[2] == "NOT_REJECTED"
                or fact[2] not in expected_flavors
            ):
                reasons.append("payload not rejected as expected: %r" % (fact,))
                break
        final = {
            "ops": self._ops,
            "accepted": self._accepted,
            "rejected": self._rejected,
            "flavors": sorted(expected_flavors),
        }
        return {
            "status": "PASS" if not reasons else "FAIL",
            "digest": self._digest(final),
            "summary": final,
            "fail_reasons": reasons,
        }


class NormalizeMaxLegalWorkload(_ObserverWorkloadBase):
    case_key = "normalize_max_legal_inprocess"

    def setup(self, ctx: Any) -> None:
        super().setup(ctx)
        self.payload = build_max_legal_dict(self.spec, self.seed, self.case_key)
        self.ticks = int(self.case_spec["ticks"])

    def operation(self, ctx: Any) -> Dict[str, Any]:
        assert self.clock is not None
        self.clock.advance()
        normalized = normalize_payload(dict(self.payload))
        blocked = normalized.blocked_reason
        accepted = blocked is None
        self._record(self._ops, accepted, reason=None if accepted else blocked)
        if accepted:
            self._facts[-1].extend([normalized.payload_hash, normalized.tick_count])
        self._ops += 1
        return {"ok": True, "units": 1}

    def verify(self, ctx: Any) -> Dict[str, Any]:
        reasons: List[str] = []
        if self._ops <= 0:
            reasons.append("no operations executed")
        if self._rejected:
            reasons.append("REJECTION_MIXED: %d rejections in accept-only case" % self._rejected)
        if self.ticks != MAX_TICKS:
            reasons.append("not at tick boundary: %d" % self.ticks)
        for fact in self._facts:
            if len(fact) < 6 or fact[5] != MAX_TICKS:
                reasons.append("unexpected normalized tick count")
                break
        final = {
            "ops": self._ops,
            "accepted": self._accepted,
            "rejected": self._rejected,
            "ticks": self.ticks,
            "max_ticks": MAX_TICKS,
        }
        return {
            "status": "PASS" if not reasons else "FAIL",
            "digest": self._digest(final),
            "summary": final,
            "fail_reasons": reasons,
        }


# ---------------------------------------------------------------------------
# H23.2 SERVICE_IN_PROCESS workloads (isolated BridgeService, no sockets)
# ---------------------------------------------------------------------------


class _ServiceWorkloadBase(_ObserverWorkloadBase):
    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.evaluator = DeterministicHoldEvaluatorDouble()
        self.service: Optional[BridgeService] = None
        self.audit_path: Optional[Path] = None

    def _make_service(self, ctx: Any) -> BridgeService:
        assert self.clock is not None
        self.audit_path = Path(ctx.tmp_dir) / "nf23-audit.jsonl"
        return BridgeService(
            evaluator=self.evaluator.evaluate,
            allowed_symbols=["SHFE.rb2610"],
            allow_test=False,
            audit_log=self.audit_path,
            instance_id="nf23-bench-bridge",
            clock=self.clock,
            stale_after_seconds=30.0,
            evaluator_workers=1,
            raw_sink=None,
        )

    def setup(self, ctx: Any) -> None:
        super().setup(ctx)
        self.service = self._make_service(ctx)

    def teardown(self, ctx: Any) -> None:
        if self.service is not None:
            self.service.close()
            self.service = None

    def _service_counters(self) -> Dict[str, int]:
        assert self.service is not None
        return dict(self.service.status()["counters"])

    def _audit_state(self) -> Tuple[int, Optional[str]]:
        if self.audit_path is None:
            return (-1, None)
        return _count_audit_lines(self.audit_path)

    def _trading_safety(self) -> Dict[str, Any]:
        assert self.service is not None
        status = self.service.status()
        return {
            "observer_only": status["observer_only"],
            "execution_enabled": status["execution_enabled"],
            "telemetry_order_submissions": status["telemetry"]["order_submissions"],
        }


class AnalyzeAcceptWorkload(_ServiceWorkloadBase):
    """Standard request path: unique batch/sequence per op, all responses hold.

    Timed region = the full ``analyze`` call (normalize + hold-double dispatch
    through a single evaluator worker + receipt ring update + one temp-audit
    JSONL write&flush).  No HTTP, no sockets, raw sink disabled.
    """

    case_key = "analyze_accept_typical_inprocess_hold_double"

    def setup(self, ctx: Any) -> None:
        super().setup(ctx)
        self.template = build_typical_payload(
            self.spec, self.seed, "normalize_accept_typical_inprocess"
        )
        self.prefix = str(self.case_spec["batch_prefix"])

    def operation(self, ctx: Any) -> Dict[str, Any]:
        assert self.service is not None and self.clock is not None
        self.clock.advance()
        payload = dict(self.template)
        payload["batch_id"] = "%s-%06d" % (self.prefix, self._ops)
        response = self.service.analyze(payload, source_header="nf23-benchmark")
        accepted = response == {"signal": "hold", "reason": ACCEPT_RESPONSE_REASON}
        self._record(self._ops, accepted, response=response)
        self._ops += 1
        return {"ok": True, "units": 1}

    def verify(self, ctx: Any) -> Dict[str, Any]:
        reasons: List[str] = []
        counters = self._service_counters()
        status = self.service.status()
        audit_lines, audit_sha = self._audit_state()
        if self._ops <= 0:
            reasons.append("no operations executed")
        if self._rejected:
            reasons.append(
                "REJECTION_MIXED_OR_BLOCKED: %d ops not cleanly accepted" % self._rejected
            )
        if counters.get("requests") != self._ops or counters.get("responses") != self._ops:
            reasons.append(
                "MISSING_REQUEST: counters requests/responses %s/%s != ops %d"
                % (counters.get("requests"), counters.get("responses"), self._ops)
            )
        if counters.get("analyze_requests") != self._ops:
            reasons.append("analyze counter %s != ops %d" % (counters.get("analyze_requests"), self._ops))
        if counters.get("blocked_requests") or counters.get("protocol_errors"):
            reasons.append("blocked/protocol errors leaked into accept case")
        if counters.get("replays"):
            reasons.append("replay hits leaked into accept case: %s" % counters.get("replays"))
        if status["sequence"] != self._ops:
            reasons.append("MISSING_REQUEST: sequence %s != ops %d" % (status["sequence"], self._ops))
        if audit_lines != self._ops:
            reasons.append(
                "AUDIT_LINES_MISMATCH: %d audit lines != %d ops" % (audit_lines, self._ops)
            )
        if self.evaluator.calls != self._ops:
            reasons.append(
                "evaluator calls %d != ops %d" % (self.evaluator.calls, self._ops)
            )
        safety = self._trading_safety()
        if safety["telemetry_order_submissions"] not in (0, None):
            reasons.append("trading safety violated: %r" % (safety,))
        final = {
            "ops": self._ops,
            "accepted": self._accepted,
            "rejected": self._rejected,
            "counters": counters,
            "sequence": status["sequence"],
            "audit_lines": audit_lines,
            "audit_sha256": audit_sha,
            "evaluator": EVALUATOR_NAME,
            "evaluator_calls": self.evaluator.calls,
            "trading_safety": safety,
        }
        return {
            "status": "PASS" if not reasons else "FAIL",
            "digest": self._digest(final),
            "summary": final,
            "fail_reasons": reasons,
        }


class AnalyzeReplayDuplicateWorkload(_ServiceWorkloadBase):
    """Duplicate-batch cache-hit (replay) path, reported separately (H23.3).

    Setup performs ONE business analyze that seeds the replay cache; every
    measured (and warmup) operation replays the SAME batch id and must hit
    the cached response WITHOUT calling the evaluator again.
    """

    case_key = "analyze_replay_duplicate_inprocess_hold_double"

    def setup(self, ctx: Any) -> None:
        super().setup(ctx)
        self.template = build_typical_payload(
            self.spec, self.seed, "normalize_accept_typical_inprocess"
        )
        self.fixed_batch = str(self.case_spec["fixed_batch_id"])
        seed_payload = dict(self.template)
        seed_payload["batch_id"] = self.fixed_batch
        self.seed_response = self.service.analyze(seed_payload, source_header="nf23-benchmark")
        if self.seed_response != {"signal": "hold", "reason": ACCEPT_RESPONSE_REASON}:
            raise RuntimeError("replay seed did not produce a clean hold: %r" % (self.seed_response,))

    def operation(self, ctx: Any) -> Dict[str, Any]:
        assert self.service is not None and self.clock is not None
        self.clock.advance()
        payload = dict(self.template)
        payload["batch_id"] = self.fixed_batch
        response = self.service.analyze(payload, source_header="nf23-benchmark")
        hit = response == self.seed_response
        self._record(self._ops, hit, response=response if hit else None,
                     reason=None if hit else "REPLAY_MISS")
        self._ops += 1
        return {"ok": True, "units": 1}

    def verify(self, ctx: Any) -> Dict[str, Any]:
        reasons: List[str] = []
        counters = self._service_counters()
        status = self.service.status()
        audit_lines, audit_sha = self._audit_state()
        if self._ops <= 0:
            reasons.append("no operations executed")
        if counters.get("replays") != self._ops:
            reasons.append(
                "replay accounting %s != ops %d (cache hits must equal ops)"
                % (counters.get("replays"), self._ops)
            )
        if self.evaluator.calls != 1:
            reasons.append(
                "DUPLICATE_BATCH: evaluator called %d times; replay path must not "
                "re-evaluate (1 seed call expected)" % self.evaluator.calls
            )
        if audit_lines != self._ops + 1:
            reasons.append(
                "AUDIT_LINES_MISMATCH: %d audit lines != %d ops + 1 seed"
                % (audit_lines, self._ops)
            )
        if counters.get("requests") != self._ops + 1:
            reasons.append("MISSING_REQUEST: requests %s != ops+1" % counters.get("requests"))
        last = status["recent_receipt"] or {}
        if last.get("blocked_reason") != "replay":
            reasons.append("last receipt not marked replay: %r" % (last.get("blocked_reason"),))
        final = {
            "ops": self._ops,
            "accepted": self._accepted,
            "rejected": self._rejected,
            "counters": counters,
            "sequence": status["sequence"],
            "audit_lines": audit_lines,
            "audit_sha256": audit_sha,
            "evaluator": EVALUATOR_NAME,
            "evaluator_calls": self.evaluator.calls,
            "trading_safety": self._trading_safety(),
        }
        return {
            "status": "PASS" if not reasons else "FAIL",
            "digest": self._digest(final),
            "summary": final,
            "fail_reasons": reasons,
        }


class HeartbeatWorkload(_ServiceWorkloadBase):
    case_key = "heartbeat_typical_inprocess_hold_double"

    def setup(self, ctx: Any) -> None:
        super().setup(ctx)
        self.payload = build_heartbeat_payload()

    def operation(self, ctx: Any) -> Dict[str, Any]:
        assert self.service is not None and self.clock is not None
        self.clock.advance()
        response = self.service.heartbeat(self.payload, source_header="nf23-benchmark")
        accepted = response == {"signal": "hold", "reason": "heartbeat"}
        self._record(self._ops, accepted, response=response)
        self._ops += 1
        return {"ok": True, "units": 1}

    def verify(self, ctx: Any) -> Dict[str, Any]:
        reasons: List[str] = []
        counters = self._service_counters()
        status = self.service.status()
        audit_lines, audit_sha = self._audit_state()
        if self._ops <= 0:
            reasons.append("no operations executed")
        if self._rejected:
            reasons.append("REJECTION_MIXED_OR_BLOCKED: %d ops not cleanly accepted" % self._rejected)
        if counters.get("heartbeat_requests") != self._ops:
            reasons.append("heartbeat counter %s != ops %d" % (counters.get("heartbeat_requests"), self._ops))
        if counters.get("replays") or counters.get("protocol_errors"):
            reasons.append("unexpected replay/protocol counters in heartbeat case")
        if self.evaluator.calls != 0:
            reasons.append("heartbeat must never evaluate: %d calls" % self.evaluator.calls)
        if audit_lines != self._ops:
            reasons.append("AUDIT_LINES_MISMATCH: %d != %d ops" % (audit_lines, self._ops))
        if status["sequence"] != self._ops:
            reasons.append("MISSING_REQUEST: sequence %s != ops %d" % (status["sequence"], self._ops))
        final = {
            "ops": self._ops,
            "accepted": self._accepted,
            "rejected": self._rejected,
            "counters": counters,
            "sequence": status["sequence"],
            "audit_lines": audit_lines,
            "audit_sha256": audit_sha,
            "evaluator": EVALUATOR_NAME,
            "evaluator_calls": self.evaluator.calls,
            "trading_safety": self._trading_safety(),
        }
        return {
            "status": "PASS" if not reasons else "FAIL",
            "digest": self._digest(final),
            "summary": final,
            "fail_reasons": reasons,
        }


class StatusSnapshotWorkload(_ServiceWorkloadBase):
    """/status snapshot cost over a full (20-deep) receipt ring (H23.2).

    Setup seeds the receipt ring with a deterministic number of analyze
    receipts; each measured operation renders one full ``status()`` snapshot
    (client status, freshness views, counters, evaluator status).  No audit
    writes happen inside the timed op (includes_io=false).
    """

    case_key = "status_snapshot_inprocess_hold_double"

    def setup(self, ctx: Any) -> None:
        super().setup(ctx)
        self.template = build_typical_payload(
            self.spec, self.seed, "normalize_accept_typical_inprocess"
        )
        self.prefix = str(self.case_spec["batch_prefix"])
        self.seed_receipts = int(self.case_spec["seed_receipts"])
        for index in range(self.seed_receipts):
            self.clock.advance()
            payload = dict(self.template)
            payload["batch_id"] = "%s-%06d" % (self.prefix, index)
            response = self.service.analyze(payload, source_header="nf23-benchmark")
            if response != {"signal": "hold", "reason": ACCEPT_RESPONSE_REASON}:
                raise RuntimeError("status seed analyze failed: %r" % (response,))

    def operation(self, ctx: Any) -> Dict[str, Any]:
        assert self.service is not None and self.clock is not None
        self.clock.advance()
        status = self.service.status()
        client_status = status["client"]["status"]
        observer = status["observer"]
        fact = [self._ops, status["sequence"], status["counters"]["responses"],
                client_status, observer.get("name"), observer.get("calls")]
        self._facts.append(fact)
        self._ops += 1
        self._accepted += 1
        return {"ok": True, "units": 1}

    def verify(self, ctx: Any) -> Dict[str, Any]:
        reasons: List[str] = []
        counters = self._service_counters()
        audit_lines, audit_sha = self._audit_state()
        if self._ops <= 0:
            reasons.append("no operations executed")
        if counters.get("analyze_requests") != self.seed_receipts:
            reasons.append(
                "seed receipts %s != %d" % (counters.get("analyze_requests"), self.seed_receipts)
            )
        if audit_lines != self.seed_receipts:
            reasons.append(
                "AUDIT_LINES_MISMATCH: %d != %d seeded" % (audit_lines, self.seed_receipts)
            )
        for fact in self._facts:
            if fact[3] != "client_seen":
                reasons.append("client status changed during status ops: %r" % (fact,))
                break
            if fact[4] != EVALUATOR_NAME:
                reasons.append("evaluator identity missing from status: %r" % (fact,))
                break
            if fact[5] != self.seed_receipts:
                reasons.append("evaluator call count moved during status ops: %r" % (fact,))
                break
        final = {
            "ops": self._ops,
            "seed_receipts": self.seed_receipts,
            "counters": counters,
            "audit_lines": audit_lines,
            "audit_sha256": audit_sha,
            "evaluator": EVALUATOR_NAME,
            "evaluator_calls": self.evaluator.calls,
            "trading_safety": self._trading_safety(),
        }
        return {
            "status": "PASS" if not reasons else "FAIL",
            "digest": self._digest(final),
            "summary": final,
            "fail_reasons": reasons,
        }


# ---------------------------------------------------------------------------
# C20 entrypoints (workload.entrypoint in each case manifest)
# ---------------------------------------------------------------------------


def build_parse_accept(ctx: Any) -> ParseAcceptWorkload:
    return ParseAcceptWorkload(ctx)


def build_parse_reject(ctx: Any) -> ParseRejectMalformedWorkload:
    return ParseRejectMalformedWorkload(ctx)


def build_parse_max_legal(ctx: Any) -> ParseMaxLegalWorkload:
    return ParseMaxLegalWorkload(ctx)


def build_normalize_accept(ctx: Any) -> NormalizeAcceptWorkload:
    return NormalizeAcceptWorkload(ctx)


def build_normalize_reject(ctx: Any) -> NormalizeRejectWorkload:
    return NormalizeRejectWorkload(ctx)


def build_normalize_max_legal(ctx: Any) -> NormalizeMaxLegalWorkload:
    return NormalizeMaxLegalWorkload(ctx)


def build_analyze_accept(ctx: Any) -> AnalyzeAcceptWorkload:
    return AnalyzeAcceptWorkload(ctx)


def build_analyze_replay(ctx: Any) -> AnalyzeReplayDuplicateWorkload:
    return AnalyzeReplayDuplicateWorkload(ctx)


def build_heartbeat(ctx: Any) -> HeartbeatWorkload:
    return HeartbeatWorkload(ctx)


def build_status_snapshot(ctx: Any) -> StatusSnapshotWorkload:
    return StatusSnapshotWorkload(ctx)


__all__ = [
    "AnalyzeAcceptWorkload",
    "AnalyzeReplayDuplicateWorkload",
    "DeterministicHoldEvaluatorDouble",
    "HeartbeatWorkload",
    "NormalizeAcceptWorkload",
    "NormalizeMaxLegalWorkload",
    "NormalizeRejectWorkload",
    "ParseAcceptWorkload",
    "ParseMaxLegalWorkload",
    "ParseRejectMalformedWorkload",
    "StatusSnapshotWorkload",
    "SyntheticBusinessClock",
    "build_analyze_accept",
    "build_analyze_replay",
    "build_heartbeat",
    "build_normalize_accept",
    "build_normalize_max_legal",
    "build_normalize_reject",
    "build_parse_accept",
    "build_parse_max_legal",
    "build_parse_reject",
    "build_status_snapshot",
]
