"""Loopback-only observer service with a hold-only client contract."""

from __future__ import annotations

from copy import deepcopy
import json
import logging
import socket
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Mapping

from .connection import TimingPolicy, clock_sample, time_freshness
from .raw_sink import RawBatchSink, SinkDescriptor
from .protocol import (
    MAX_BODY_BYTES,
    MAX_REASON_LENGTH,
    DecisionEvaluator,
    NormalizedRequest,
    ProtocolViolation,
    hold_response,
    normalize_payload,
    parse_json_body,
    payload_hash,
    safe_reason,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17777
MAX_WORKERS = 32
DEFAULT_EVALUATOR_WORKERS = 8
DEFAULT_BODY_TIMEOUT_SECONDS = 3.0
DEFAULT_EVALUATION_BUDGET_SECONDS = 0.25
DEFAULT_REPLAY_TTL_SECONDS = 300.0
DEFAULT_REPLAY_CAPACITY = 2048
MAX_AUDIT_TICK_FIELDS = 5
MAX_AUDIT_FIELD_LENGTH = 120

_LOGGER = logging.getLogger("ak_trading.infiny_bridge")


class _BoundedThreadingMixIn:
    """Bound accepted request handlers while retaining ThreadingMixIn semantics."""

    daemon_threads = True
    block_on_close = False

    def __init__(self, *args: Any, max_workers: int = MAX_WORKERS, **kwargs: Any) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int):
            raise ValueError("max_workers must be an integer")
        if not 1 <= max_workers <= MAX_WORKERS:
            raise ValueError("max_workers must be between 1 and 32")
        self._worker_slots = threading.BoundedSemaphore(max_workers)
        self.max_workers = max_workers
        super().__init__(*args, **kwargs)

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._worker_slots.acquire(blocking=False):
            body = json.dumps(hold_response("busy"), separators=(",", ":")).encode("ascii")
            try:
                request.settimeout(0.2)
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\n".encode("ascii")
                    + b"Connection: close\r\nCache-Control: no-store\r\n\r\n"
                    + body
                )
            except OSError:
                pass
            finally:
                request.close()
            return
        try:
            # ThreadingMixIn.process_request creates the actual daemon worker.
            super().process_request(request, client_address)
        except BaseException:
            self._worker_slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()


class BridgeHTTPServer(_BoundedThreadingMixIn, ThreadingHTTPServer):
    """Threaded HTTP server that can bind to loopback only."""

    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        **kwargs: Any,
    ) -> None:
        host, port = server_address
        if host != DEFAULT_HOST:
            raise ValueError("host must be 127.0.0.1")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        super().__init__(server_address, handler_class, **kwargs)


class _Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


class BridgeService:
    """Validate telemetry, invoke a bounded evaluator, and return only hold."""

    def __init__(
        self,
        evaluator: DecisionEvaluator | None = None,
        *,
        allowed_symbols: set[str] | frozenset[str] | list[str] | tuple[str, ...] | None = None,
        allow_test: bool = False,
        audit_log: str | Path | None = None,
        instance_id: str | None = None,
        local_token: str | None = None,
        client_token: str | None = None,
        clock: Any | None = None,
        stale_after_seconds: float = 30.0,
        evaluation_budget_seconds: float = DEFAULT_EVALUATION_BUDGET_SECONDS,
        evaluator_workers: int = DEFAULT_EVALUATOR_WORKERS,
        replay_ttl_seconds: float = DEFAULT_REPLAY_TTL_SECONDS,
        replay_capacity: int = DEFAULT_REPLAY_CAPACITY,
        body_timeout_seconds: float = DEFAULT_BODY_TIMEOUT_SECONDS,
        raw_sink: RawBatchSink | None = None,
    ) -> None:
        stale_after_seconds = float(stale_after_seconds)
        evaluation_budget_seconds = float(evaluation_budget_seconds)
        replay_ttl_seconds = float(replay_ttl_seconds)
        body_timeout_seconds = float(body_timeout_seconds)
        if not 0 < stale_after_seconds <= 86_400:
            raise ValueError("stale_after_seconds must be positive")
        if not 0 < evaluation_budget_seconds < 15:
            raise ValueError("evaluation_budget_seconds must be less than 15")
        if not 0 < replay_ttl_seconds <= 86_400:
            raise ValueError("replay_ttl_seconds must be positive")
        if isinstance(evaluator_workers, bool) or not isinstance(evaluator_workers, int):
            raise ValueError("evaluator_workers must be an integer")
        if not 1 <= evaluator_workers <= MAX_WORKERS:
            raise ValueError("evaluator_workers must be between 1 and 32")
        if not 0 < body_timeout_seconds <= 3:
            raise ValueError("body_timeout_seconds must be between 0 and 3")
        if not isinstance(replay_capacity, int) or isinstance(replay_capacity, bool) or not 1 <= replay_capacity <= 100_000:
            raise ValueError("replay_capacity must be between 1 and 100000")
        if local_token is not None and client_token is not None and local_token != client_token:
            raise ValueError("local_token and client_token differ")
        configured_token = local_token if local_token is not None else client_token
        if configured_token is not None and (not isinstance(configured_token, str) or not configured_token or len(configured_token) > 256):
            raise ValueError("local_token must be a non-empty short string")

        self.evaluator = evaluator or (lambda _payload: {"candidate_signal": "hold", "reason": "observing"})
        if not callable(self.evaluator):
            raise TypeError("evaluator must be callable")
        self.allowed_symbols = frozenset(
            str(symbol).strip() for symbol in (allowed_symbols or ()) if str(symbol).strip()
        )
        self.allow_test = bool(allow_test)
        self.audit_log = Path(audit_log) if audit_log is not None else None
        self.reference_root: Path | None = None
        self.instance_id = (instance_id or f"bridge-{uuid.uuid4().hex[:16]}")[:64]
        self.local_token = configured_token
        self.clock = clock or _Clock()
        self.timing = TimingPolicy()
        self.stale_after_seconds = stale_after_seconds
        self.evaluation_budget_seconds = evaluation_budget_seconds
        self.evaluator_workers = evaluator_workers
        self.replay_ttl_seconds = replay_ttl_seconds
        self.replay_capacity = replay_capacity
        self.body_timeout_seconds = body_timeout_seconds
        self._lock = threading.RLock()
        self._request_context = threading.local()
        self._sequence = 0
        self._replays: OrderedDict[str, tuple[float, dict[str, str]]] = OrderedDict()
        self._recent_receipts: list[dict[str, Any]] = []
        self._recent_rejections: list[dict[str, Any]] = []
        self._contract_receipts: dict[str, dict[str, Any]] = {}
        self._adapter_order_submissions: int | None = None
        self._last_source_header: str | None = None
        self._evaluator_slots = threading.BoundedSemaphore(evaluator_workers)
        self._evaluator_pool = ThreadPoolExecutor(
            max_workers=evaluator_workers,
            thread_name_prefix="infiny-evaluator",
        )
        self._closed = False
        self._audit_handle: Any | None = None
        self._started_at = self._now()
        self._sample_high_water = {"mono": None, "wall": None}
        self._counters: dict[str, int] = {
            "requests": 0,
            "analyze_requests": 0,
            "heartbeat_requests": 0,
            "responses": 0,
            "protocol_errors": 0,
            "blocked_requests": 0,
            "unknown_symbols": 0,
            "stale_requests": 0,
            "limit_or_auction_requests": 0,
            "tick_chronology_errors": 0,
            "evaluator_errors": 0,
            "evaluator_timeouts": 0,
            "evaluator_busy": 0,
            "replays": 0,
            "synthetic_requests": 0,
            "broker_requests": 0,
        }
        if self.audit_log is not None:
            self._open_audit_log()
        # Optional receipt-boundary raw sink: disabled by default; failures
        # here never alter the receipt/evaluator paths (observer-sink H03).
        self.raw_sink = raw_sink

    def _now(self) -> datetime:
        # Invalid injected clock readings remain invalid for liveness. This
        # fallback is only for legacy protocol/audit formatting, not freshness.
        value, _ = clock_sample(self.clock)
        return value or datetime.now(timezone.utc)

    def _mono(self) -> float:
        value = self.clock.monotonic() if hasattr(self.clock, "monotonic") else time.monotonic()
        return float(value)

    def _open_audit_log(self) -> None:
        assert self.audit_log is not None
        self.audit_log.parent.mkdir(parents=True, exist_ok=True)
        self._audit_handle = self.audit_log.open("a", encoding="utf-8")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            handle, self._audit_handle = self._audit_handle, None
        self._evaluator_pool.shutdown(wait=False, cancel_futures=True)
        sink = getattr(self, "raw_sink", None)
        if sink is not None:
            try:
                sink.close()
            except Exception:
                pass
        if handle is not None:
            try:
                handle.close()
            except OSError:
                _LOGGER.warning("audit_close_failed")

    def _inc(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + amount

    def note_protocol_error(self, *, endpoint: str = "unknown", payload: Mapping[str, Any] | None = None, reason: str = "protocol_error") -> None:
        self._inc("protocol_errors")
        entry = {
            "at": self._now().isoformat().replace("+00:00", "Z"),
            "endpoint": endpoint,
            "exchange": payload.get("exchange") if isinstance(payload, Mapping) else None,
            "instrument": payload.get("instrument_id") if isinstance(payload, Mapping) else None,
            "payload_hash": payload_hash(payload),
            "reason": reason,
        }
        with self._lock:
            self._recent_rejections.append(entry)
            del self._recent_rejections[:-20]

    def consume_http_status(self) -> int:
        status = getattr(self._request_context, "status", HTTPStatus.OK)
        self._request_context.status = HTTPStatus.OK
        return int(status)

    def _set_http_status(self, status: int) -> None:
        self._request_context.status = int(status)

    def _next_sequence(self) -> int:
        with self._lock:
            self._sequence += 1
            return self._sequence

    def _purge_replays(self, now_mono: float) -> None:
        cutoff = now_mono - self.replay_ttl_seconds
        while self._replays:
            key, (created, _response) = next(iter(self._replays.items()))
            if created >= cutoff and len(self._replays) <= self.replay_capacity:
                break
            self._replays.pop(key, None)

    def _get_replay(self, batch_id: str | None, now_mono: float) -> dict[str, str] | None:
        if not batch_id:
            return None
        with self._lock:
            self._purge_replays(now_mono)
            record = self._replays.get(batch_id)
            if record is None:
                return None
            created, response = record
            if created < now_mono - self.replay_ttl_seconds:
                self._replays.pop(batch_id, None)
                return None
            self._replays.move_to_end(batch_id)
            self._counters["replays"] += 1
            return dict(response)

    def _remember_replay(self, batch_id: str | None, response: dict[str, str], now_mono: float) -> None:
        if not batch_id:
            return
        with self._lock:
            self._purge_replays(now_mono)
            self._replays[batch_id] = (now_mono, dict(response))
            self._replays.move_to_end(batch_id)
            while len(self._replays) > self.replay_capacity:
                self._replays.popitem(last=False)

    @staticmethod
    def _audit_value(value: Any, maximum: int = MAX_AUDIT_FIELD_LENGTH) -> Any:
        if isinstance(value, str):
            return " ".join(value.split())[:maximum]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return None

    def _audit(
        self,
        request: NormalizedRequest,
        *,
        decision: str,
        blocked_reason: str | None,
        sequence: int,
        source_header: str | None,
        ticks: list[dict[str, Any]] | None = None,
    ) -> None:
        handle = self._audit_handle
        if handle is None:
            return
        quotes: list[dict[str, Any]] = []
        for tick in (ticks or [])[:MAX_AUDIT_TICK_FIELDS]:
            item: dict[str, Any] = {}
            for key in ("time", "last", "bid1", "ask1", "volume"):
                if key in tick:
                    item[key] = self._audit_value(tick[key])
            if item:
                quotes.append(item)
        record = {
            "event": "receipt",
            "at": self._now().isoformat().replace("+00:00", "Z"),
            "instance_id": self.instance_id,
            "sequence": sequence,
            "batch_id": request.batch_id,
            "payload_hash": request.payload_hash,
            "exchange": request.exchange,
            "instrument": request.instrument,
            "synthetic": request.synthetic,
            "source_header": self._audit_value(source_header, 64),
            "tick_count": request.tick_count,
            "quotes": quotes,
            "decision": decision,
            "blocked_reason": blocked_reason,
        }
        try:
            handle.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n")
            handle.flush()
        except OSError:
            _LOGGER.warning("audit_write_failed")

    def _receipt(
        self,
        request: NormalizedRequest,
        *,
        decision: str,
        blocked_reason: str | None,
        sequence: int,
        source_header: str | None,
        frozen: Mapping[str, Any] | None = None,
        raw_body: bytes | bytearray | None = None,
        received_at: str | None = None,
        endpoint: str = "unknown",
    ) -> None:
        received_wall, received_mono = clock_sample(self.clock)
        # A receipt written while the monotonic or wall sample regressed below
        # a previously observed reading cannot certify fresh liveness. Each
        # axis advances its own high-water independently of the other's fault.
        regressed = False
        with self._lock:
            mono_high = self._sample_high_water["mono"]
            wall_high = self._sample_high_water["wall"]
            if received_mono is not None and mono_high is not None and received_mono < mono_high:
                regressed = True
            if received_wall is not None and wall_high is not None and received_wall < wall_high:
                regressed = True
            if received_mono is not None and (mono_high is None or received_mono > mono_high):
                self._sample_high_water["mono"] = received_mono
            if received_wall is not None and (wall_high is None or received_wall > wall_high):
                self._sample_high_water["wall"] = received_wall
        facts = frozen if frozen is not None else request.payload
        receipt = {
            "at": received_wall.isoformat().replace("+00:00", "Z") if received_wall else None,
            "_received_mono": received_mono,
            "_clock_regressed": regressed,
            "sequence": sequence,
            "batch_id": request.batch_id,
            "exchange": request.exchange,
            "instrument": request.instrument,
            "decision": decision,
            "blocked_reason": blocked_reason,
            "source_header": self._audit_value(source_header, 64),
            "synthetic": request.synthetic,
            "tick_count": request.tick_count,
            "source_verified": False,
            "connection": deepcopy(facts.get("connection", {})),
            "snapshot": deepcopy({key: facts.get(key) for key in ("account", "position", "contract", "quote", "active_orders", "runtime", "trading_day", "market_status", "orders", "trades", "errors", "observed_orders", "observed_trades", "capabilities", "metadata")}),
        }
        with self._lock:
            self._recent_receipts.append(receipt)
            del self._recent_receipts[:-20]
            key = f"{request.exchange}.{request.instrument}"
            if request.exchange and request.instrument and key in self.allowed_symbols:
                self._contract_receipts[key] = receipt
            if source_header:
                self._last_source_header = self._audit_value(source_header, 64)
        self._offer_raw_sink(
            request, facts=facts, decision=decision, blocked_reason=blocked_reason,
            sequence=sequence, source_header=receipt["source_header"],
            receipt_at=receipt["at"], clock_valid=(received_wall is not None and not regressed),
            raw_body=raw_body, received_at=received_at, endpoint=endpoint)
        self._audit(
            request,
            decision=decision,
            blocked_reason=blocked_reason,
            sequence=sequence,
            source_header=source_header,
            ticks=deepcopy(facts.get("ticks", [])),
        )

    def _raw_sink_status(self) -> dict[str, Any]:
        """Read-only in-memory sink snapshot; additive, never raises."""
        sink = getattr(self, "raw_sink", None)
        if sink is None:
            return {"enabled": False, "state": "disabled"}
        try:
            return sink.status()
        except Exception:
            return {"enabled": True, "state": "unknown"}

    def _offer_raw_sink(
        self,
        request: NormalizedRequest,
        *,
        facts: Mapping[str, Any],
        decision: str,
        blocked_reason: str | None,
        sequence: int,
        source_header: str | None,
        receipt_at: str | None,
        clock_valid: bool,
        raw_body: bytes | bytearray | None = None,
        received_at: str | None = None,
        endpoint: str = "unknown",
    ) -> None:
        """Offer one immutable descriptor at the receipt boundary (H02/H03).

        Bounded in-memory admission only: no file I/O, encoding, capacity wait
        or acknowledgement wait. Any sink exception is swallowed so the legacy
        receipt/audit path is untouched.
        """
        sink = self.raw_sink
        if sink is None:
            return
        try:
            body = bytes(raw_body) if raw_body is not None else None
            descriptor = SinkDescriptor(
                endpoint=endpoint, batch_id=request.batch_id,
                exchange=request.exchange, instrument=request.instrument,
                synthetic=request.synthetic, source_header=source_header,
                received_at=received_at, receipt_at=receipt_at,
                clock_valid=clock_valid, decision=decision,
                blocked_reason=blocked_reason, tick_count=request.tick_count,
                payload_hash=request.payload_hash, instance_id=self.instance_id,
                sequence=sequence, payload=facts, body_bytes=body)
            sink.offer(descriptor)
        except Exception:
            return

    def _observe_order_telemetry(self, payload: Mapping[str, Any]) -> None:
        runtime = payload.get("runtime")
        value: Any = runtime.get("order_submission_count") if isinstance(runtime, Mapping) else None
        if value is None:
            value = payload.get("order_submission_count")
        if value is None:
            nested = payload.get("order_submissions")
            value = nested.get("count") if isinstance(nested, Mapping) else nested
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2_147_483_647:
            with self._lock:
                self._adapter_order_submissions = value

    def _evaluate(self, evaluator_input: Mapping[str, Any]) -> tuple[Mapping[str, Any] | None, str | None]:
        if not self._evaluator_slots.acquire(blocking=False):
            self._inc("evaluator_busy")
            return None, "evaluator_busy"

        def invoke() -> Mapping[str, Any]:
            try:
                value = self.evaluator(evaluator_input)
                if not isinstance(value, Mapping):
                    raise TypeError("evaluator_result")
                return dict(value)
            finally:
                self._evaluator_slots.release()

        try:
            future: Future[Mapping[str, Any]] = self._evaluator_pool.submit(invoke)
        except Exception:
            self._evaluator_slots.release()
            self._inc("evaluator_errors")
            return None, "evaluator_error"
        try:
            return future.result(timeout=self.evaluation_budget_seconds), None
        except TimeoutError:
            self._inc("evaluator_timeouts")
            return None, "evaluator_timeout"
        except Exception:
            self._inc("evaluator_errors")
            return None, "evaluator_error"

    def _response_from_candidate(
        self,
        candidate: Mapping[str, Any] | None,
        error: str | None,
    ) -> tuple[dict[str, str], str | None]:
        if error is not None:
            return hold_response(error), error
        if candidate is None:
            return hold_response("evaluator_error"), "evaluator_error"
        signal = candidate.get("candidate_signal", "hold")
        if not isinstance(signal, str):
            return hold_response("evaluator_invalid"), "evaluator_invalid"
        signal = signal.casefold()
        if signal in {"buy", "sell"}:
            # Candidate direction is an internal diagnostic only.
            return hold_response("candidate_blocked"), "candidate_blocked"
        if signal != "hold":
            return hold_response("evaluator_invalid"), "evaluator_invalid"
        reason = safe_reason(candidate.get("reason"), "observing")
        return hold_response(reason), None

    def analyze(self, payload: Mapping[str, Any], *, source_header: str | None = None,
                raw_body: bytes | bytearray | None = None,
                received_at: str | None = None) -> dict[str, str]:
        self._set_http_status(HTTPStatus.OK)
        self._inc("requests")
        self._inc("analyze_requests")
        self._observe_order_telemetry(payload)
        sequence = self._next_sequence()
        now_mono = self._mono()
        try:
            request = normalize_payload(
                payload,
                allowed_symbols=self.allowed_symbols,
                allow_test=self.allow_test,
                now=self._now(),
                stale_after_seconds=self.stale_after_seconds,
            )
        except ProtocolViolation as exc:
            self.note_protocol_error(endpoint="/analyze", payload=payload, reason=exc.reason)
            self._set_http_status(HTTPStatus.BAD_REQUEST)
            self._inc("responses")
            return hold_response(exc.reason)

        # Freeze received facts before any evaluator or timeout path can run;
        # receipts and audit are built only from this immutable copy.
        frozen = deepcopy(request.payload)
        with self._lock:
            self._counters["synthetic_requests" if request.synthetic else "broker_requests"] += 1
        replay = self._get_replay(request.batch_id, now_mono)
        if replay is not None:
            self._receipt(
                request, frozen=frozen, decision="hold", blocked_reason="replay",
                sequence=sequence, source_header=source_header,
                raw_body=raw_body, received_at=received_at, endpoint="/analyze",
            )
            self._inc("responses")
            return replay

        if request.blocked_reason:
            blocked = request.blocked_reason
            self._inc("blocked_requests")
            if blocked == "symbol_unknown":
                self._inc("unknown_symbols")
            if blocked in {"stale_tick", "future_tick", "missing_full_context"}:
                self._inc("stale_requests")
            if blocked == "tick_chronology":
                self._inc("tick_chronology_errors")
            if blocked in {"limit_state", "auction_state"}:
                self._inc("limit_or_auction_requests")
            response = hold_response(blocked)
            decision_error = blocked
        else:
            candidate, evaluator_error = self._evaluate(deepcopy(frozen))
            response, decision_error = self._response_from_candidate(candidate, evaluator_error)
            if candidate is not None:
                with self._lock:
                    self._last_candidate = {key: self._audit_value(candidate.get(key)) for key in (
                        "candidate_signal", "reason", "strategy", "signal_key", "history_manifest_sha256",
                        "history_through", "candidate_as_of", "execution_authorized", "observation_only",
                    )}
        self._remember_replay(request.batch_id, response, now_mono)
        self._receipt(
            request,
            frozen=frozen,
            decision="hold",
            blocked_reason=request.blocked_reason or decision_error,
            sequence=sequence,
            source_header=source_header,
            raw_body=raw_body,
            received_at=received_at,
            endpoint="/analyze",
        )
        self._inc("responses")
        return response

    def heartbeat(self, payload: Mapping[str, Any], *, source_header: str | None = None,
                   raw_body: bytes | bytearray | None = None,
                   received_at: str | None = None) -> dict[str, str]:
        self._set_http_status(HTTPStatus.OK)
        self._inc("requests")
        self._inc("heartbeat_requests")
        self._observe_order_telemetry(payload)
        sequence = self._next_sequence()
        try:
            request = normalize_payload(
                payload,
                allowed_symbols=self.allowed_symbols,
                allow_test=self.allow_test,
                now=self._now(),
                stale_after_seconds=self.stale_after_seconds,
                require_ticks=False,
                require_identity=False,
            )
        except ProtocolViolation as exc:
            self.note_protocol_error(endpoint="/heartbeat", payload=payload, reason=exc.reason)
            self._set_http_status(HTTPStatus.BAD_REQUEST)
            self._inc("responses")
            return hold_response(exc.reason)
        blocked = request.blocked_reason
        response = hold_response(blocked or "heartbeat")
        self._receipt(
            request, frozen=deepcopy(request.payload), decision="hold", blocked_reason=blocked,
            sequence=sequence, source_header=source_header,
            raw_body=raw_body, received_at=received_at,
        )
        self._inc("responses")
        return response

    def reference(self, symbol: str, day: str, trade_price: str | None = None, settlement_price: str | None = None) -> dict:
        from datetime import date
        from decimal import Decimal
        from ak_trading.settlement_reference import load_settlement_reference
        if symbol not in self.allowed_symbols:
            raise ValueError("symbol is not allowed")
        if self.reference_root is None:
            return {"status": "unavailable", "reason": "reference_root_not_configured"}
        return load_settlement_reference(self.reference_root, day=date.fromisoformat(day), symbol=symbol,
                                         trade_price=Decimal(trade_price) if trade_price is not None else None,
                                         settlement_price=Decimal(settlement_price) if settlement_price is not None else None)

    def _client_status(self, receipt, sample) -> dict[str, Any]:
        wall, mono = sample
        received = receipt.get("at") if receipt else None
        freshness = time_freshness(received, wall, self.timing.link_ttl, future_skew=0)
        age = freshness["age_seconds"]
        then = receipt.get("_received_mono") if receipt else None
        elapsed = mono - then if mono is not None and then is not None else None
        regressed = bool(receipt.get("_clock_regressed")) if receipt else False
        valid = (not regressed and age is not None and elapsed is not None
                 and min(age, elapsed) >= 0)
        age = max(age, elapsed) if valid else None
        return {"status": "awaiting_client" if receipt is None else "client_seen" if age is not None and age <= self.timing.link_ttl else "client_stale",
                "last_received_at": received, "age_seconds": age,
                "stale_after_seconds": self.timing.link_ttl, "clock_valid": valid,
                "source_header": receipt.get("source_header") if receipt else None,
                "source_header_trust": "claimed_unverified", "client_verified": False,
                "synthetic": receipt.get("synthetic") if receipt else None,
                "ui_idle_refresh": "unsupported"}

    def _receipt_view(self, receipt, sample):
        if receipt is None:
            return None
        result = deepcopy({key: value for key, value in receipt.items() if not key.startswith("_")})
        transport = self._client_status(receipt, sample)
        now = sample[0] if transport["clock_valid"] else None
        snapshot = receipt["snapshot"]
        connection = receipt.get("connection") or {}
        times = connection.get("snapshots", {})
        result["transport"] = transport
        result["freshness"] = {
            "snapshot": time_freshness((snapshot.get("metadata") or {}).get("snapshot_observed_at"), now, self.stale_after_seconds, self.timing.future_skew),
            "quote": time_freshness((snapshot.get("quote") or {}).get("time"), now, self.stale_after_seconds, self.timing.future_skew),
            "packet": time_freshness(connection.get("sent_at"), now, self.timing.packet_ttl, self.timing.future_skew),
            "components": {},
        }
        for name in ("quote", "account", "position", "contract"):
            record = times.get(name, {})
            result["freshness"]["components"][name] = {
                "sampling": time_freshness(record.get("sampled_at"), now, getattr(self.timing, name + "_ttl"), self.timing.future_skew),
                "source": time_freshness(record.get("source_at"), now, getattr(self.timing, name + "_ttl"), self.timing.future_skew),
                "received": time_freshness(record.get("received_at"), now, getattr(self.timing, name + "_ttl"), self.timing.future_skew),
                "completeness": record.get("completeness", "unknown"),
            }
        return result

    def status(self) -> dict[str, Any]:
        sample = clock_sample(self.clock)
        with self._lock:
            counters = dict(self._counters)
            raw_recent = list(self._recent_receipts)
            recent = [self._receipt_view(item, sample) for item in raw_recent]
            client = self._client_status(raw_recent[-1] if raw_recent else None, sample)
            contracts = {key: self._receipt_view(value, sample) for key, value in self._contract_receipts.items()}
            rejections = [dict(item) for item in self._recent_rejections]
            sequence = self._sequence
            reported_orders = self._adapter_order_submissions
        return {
            "observer_only": True,
            "execution_enabled": False,
            "instance_id": self.instance_id,
            "sequence": sequence,
            "started_at": self._started_at.isoformat().replace("+00:00", "Z"),
            "allowed_symbols": sorted(self.allowed_symbols),
            "client": client,
            "telemetry": {
                "order_submissions": reported_orders,
                "order_submissions_source": "adapter_reported",
                "order_submissions_verified": False,
                "native_order_submission_count": None,
            },
            "counters": counters,
            "recent_receipt": recent[-1] if recent else None,
            "recent_receipts": recent,
            "contracts": contracts,
            "recent_rejections": rejections,
            "latest_candidate": dict(getattr(self, "_last_candidate", {})),
            "observer": self.evaluator.__self__.status() if hasattr(getattr(self.evaluator, "__self__", None), "status") else {},
            "live_client_verified": False,
            "raw_sink": self._raw_sink_status(),
        }

    def health(self) -> dict[str, Any]:
        sample = clock_sample(self.clock)
        with self._lock:
            receipt = self._recent_receipts[-1] if self._recent_receipts else None
            recent = self._receipt_view(receipt, sample)
            client = self._client_status(receipt, sample)
            sequence = self._sequence
            reported_orders = self._adapter_order_submissions
        return {
            "status": "ok",
            "observer_only": True,
            "execution_enabled": False,
            "client_status": client["status"],
            "client": client,
            "instance_id": self.instance_id,
            "sequence": sequence,
            "telemetry": {
                "order_submissions": reported_orders,
                "source": "adapter_reported",
                "verified": False,
                "native_order_submission_count": None,
            },
            "recent_receipt": recent,
            "raw_sink": self._raw_sink_status(),
        }


class _BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: BridgeHTTPServer

    def _service(self) -> BridgeService:
        return self.server.bridge_service  # type: ignore[attr-defined]

    def _respond(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Replace BaseHTTPRequestHandler's HTML errors with JSON."""

        reason = "bad_request" if code == HTTPStatus.BAD_REQUEST else "method_not_allowed"
        if code == HTTPStatus.REQUEST_TIMEOUT:
            reason = "request_timeout"
        elif code == HTTPStatus.NOT_FOUND:
            reason = "not_found"
        elif code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE:
            reason = "payload_too_large"
        elif code == HTTPStatus.SERVICE_UNAVAILABLE:
            reason = "busy"
        self._respond(code, hold_response(reason))

    def _authorized(self) -> bool:
        expected = self._service().local_token
        if expected is None:
            return True
        supplied = self.headers.get("X-Bridge-Token") or self.headers.get("X-Infiny-Token")
        return isinstance(supplied, str) and len(supplied) <= 256 and supplied == expected

    def _source_header(self) -> str | None:
        value = self.headers.get("X-Infiny-Source") or self.headers.get("X-Bridge-Source")
        if not isinstance(value, str):
            return None
        value = " ".join(value.split())
        if not value or len(value) > 64 or any(ord(char) < 32 for char in value):
            return None
        return value

    def _read_body(self) -> bytes:
        header = self.headers.get("Content-Length")
        if header is None:
            raise ProtocolViolation("content_length_required")
        try:
            length = int(header)
        except (TypeError, ValueError):
            raise ProtocolViolation("invalid_content_length")
        if length < 0:
            raise ProtocolViolation("invalid_content_length")
        self.connection.settimeout(self._service().body_timeout_seconds)
        if length > MAX_BODY_BYTES:
            # Consume only a bounded prefix so the client receives a JSON
            # response instead of an early socket reset, then close the socket.
            self.rfile.read(MAX_BODY_BYTES + 1)
            raise ProtocolViolation("payload_too_large")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ProtocolViolation("body_read_timeout")
        return body

    def do_POST(self) -> None:
        if self.path not in {"/analyze", "/heartbeat"}:
            self._respond(HTTPStatus.NOT_FOUND, hold_response("not_found"))
            return
        if not self._authorized():
            self._respond(HTTPStatus.UNAUTHORIZED, hold_response("unauthorized"))
            return
        try:
            body_bytes = self._read_body()
            received_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            payload = parse_json_body(body_bytes)
            source = self._source_header()
            if self.path == "/analyze":
                response = self._service().analyze(payload, source_header=source,
                                                    raw_body=body_bytes, received_at=received_at)
            else:
                response = self._service().heartbeat(payload, source_header=source,
                                                     raw_body=body_bytes, received_at=received_at)
            self._respond(self._service().consume_http_status(), response)
        except ProtocolViolation as exc:
            self._service().note_protocol_error(endpoint=self.path, payload=locals().get("payload"), reason=exc.reason)
            self._respond(HTTPStatus.BAD_REQUEST, hold_response(exc.reason))
        except (ConnectionError, TimeoutError, socket.timeout):
            self._respond(HTTPStatus.REQUEST_TIMEOUT, hold_response("request_timeout"))
        except Exception:
            _LOGGER.warning("request_failed")
            self._respond(HTTPStatus.INTERNAL_SERVER_ERROR, hold_response("internal_error"))

    def do_GET(self) -> None:
        if not self._authorized():
            self._respond(HTTPStatus.UNAUTHORIZED, hold_response("unauthorized"))
            return
        if self.path.startswith("/reference?"):
            from urllib.parse import parse_qs, urlsplit
            try:
                query = parse_qs(urlsplit(self.path).query, strict_parsing=True, max_num_fields=4)
                if not {"symbol", "day"} <= set(query) or set(query) - {"symbol", "day", "price", "settlement"} or any(len(v) != 1 for v in query.values()):
                    raise ValueError("invalid reference query")
                result = self._service().reference(query["symbol"][0], query["day"][0],
                                                   query.get("price", [None])[0], query.get("settlement", [None])[0])
                self._respond(HTTPStatus.OK, result)
            except (ValueError, OSError, ArithmeticError):
                self._respond(HTTPStatus.BAD_REQUEST, {"status": "invalid_reference_request"})
        elif self.path == "/health":
            self._respond(HTTPStatus.OK, self._service().health())
        elif self.path == "/status":
            self._respond(HTTPStatus.OK, self._service().status())
        else:
            self._respond(HTTPStatus.NOT_FOUND, hold_response("not_found"))

    def do_PUT(self) -> None:
        self._respond(HTTPStatus.METHOD_NOT_ALLOWED, hold_response("method_not_allowed"))

    do_PATCH = do_PUT
    do_DELETE = do_PUT
    do_OPTIONS = do_PUT
    do_CONNECT = do_PUT
    do_TRACE = do_PUT

    def do_HEAD(self) -> None:
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def handle_one_request(self) -> None:
        try:
            self.connection.settimeout(self._service().body_timeout_seconds)
            super().handle_one_request()
        except (socket.timeout, TimeoutError):
            try:
                self.send_error(HTTPStatus.REQUEST_TIMEOUT)
            except Exception:
                pass


def build_server(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    service: BridgeService | None = None,
    evaluator: DecisionEvaluator | None = None,
    allowed_symbols: set[str] | frozenset[str] | list[str] | tuple[str, ...] | None = None,
    allow_test: bool = False,
    audit_log: str | Path | None = None,
    max_workers: int = MAX_WORKERS,
    **service_options: Any,
) -> BridgeHTTPServer:
    """Build an in-process loopback server for the CLI and tests."""

    if host != DEFAULT_HOST:
        raise ValueError("host must be 127.0.0.1")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    if service is None:
        service = BridgeService(
            evaluator,
            allowed_symbols=allowed_symbols,
            allow_test=allow_test,
            audit_log=audit_log,
            **service_options,
        )
    server = BridgeHTTPServer((DEFAULT_HOST, port), _BridgeHandler, max_workers=max_workers)
    server.bridge_service = service  # type: ignore[attr-defined]
    return server


def serve_forever(server: BridgeHTTPServer) -> None:
    """Serve until interrupted, then close sockets and service resources."""

    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        service = getattr(server, "bridge_service", None)
        if service is not None:
            service.close()


ObservationBridge = BridgeService

__all__ = [
    "BridgeHTTPServer",
    "BridgeService",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "ObservationBridge",
    "build_server",
    "serve_forever",
]
