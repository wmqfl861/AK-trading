"""Focused loopback tests for the observation bridge contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

import pytest

from ak_trading.infiny_bridge.protocol import MAX_BODY_BYTES, normalize_payload
from ak_trading.infiny_bridge.service import (
    DEFAULT_PORT,
    BridgeService,
    build_server,
)


ROOT = Path(__file__).resolve().parents[1]
FIXED_NOW = datetime(2026, 9, 9, 1, 0, tzinfo=timezone.utc)


class FixedClock:
    def now(self) -> datetime:
        return FIXED_NOW

    def monotonic(self) -> float:
        return time.monotonic()


def _tick(at: str = "2026-09-09T09:00:00+08:00", **extra: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "time": at,
        "last": 4000,
        "bid1": 3999,
        "ask1": 4001,
        "volume": 1,
    }
    value.update(extra)
    return value


def _payload(**updates: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": 2,
        "batch_id": "batch-1",
        "exchange": "SHFE",
        "instrument_id": "rb2610",
        "ticks": [_tick()],
        "trading_day": "20260909",
        "market_status": "continuous",
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
            "risk": 0.1,
            "account_id": "must-not-cross-boundary",
        },
        "contract": {
            "status": "ready",
            "price_tick": 1,
            "upper_limit": 4500,
            "lower_limit": 3500,
        },
        "active_orders": {"status": "ready", "count": 0},
        "runtime": {
            "status": "available",
            "observer_only": True,
            "enable_trading": False,
            "framework_trading": False,
            "order_submission_count": 3,
        },
    }
    value.update(updates)
    return value


def _request(
    address: tuple[str, int],
    method: str,
    path: str,
    payload: Any = None,
    *,
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
) -> tuple[int, dict[str, Any], str]:
    body = raw
    request_headers = {"Connection": "close"}
    if payload is not None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    connection = HTTPConnection(address[0], address[1], timeout=3)
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        response_body = response.read().decode("utf-8")
        decoded = json.loads(response_body) if response_body else {}
        return response.status, decoded, response_body
    finally:
        connection.close()


@pytest.fixture
def running_bridge(tmp_path: Path):
    calls: list[dict[str, Any]] = []

    def evaluator(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return {"candidate_signal": "buy", "reason": "internal candidate"}

    audit = tmp_path / "audit.jsonl"
    service = BridgeService(
        evaluator,
        allowed_symbols=["SHFE.rb2610"],
        audit_log=audit,
        clock=FixedClock(),
        evaluation_budget_seconds=0.2,
        evaluator_workers=2,
        replay_capacity=8,
    )
    server = build_server(port=0, service=service, max_workers=2)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = ("127.0.0.1", server.server_address[1])
    try:
        yield address, service, calls, audit
    finally:
        server.shutdown()
        server.server_close()
        service.close()
        thread.join(timeout=2)


@pytest.mark.parametrize("flag", ["auction", "limit_up", "limit_down"])
def test_market_flags_survive_the_actual_normalization_boundary(flag):
    request = _payload(**{flag: True})
    normalized = normalize_payload(request, allowed_symbols=["SHFE.rb2610"], now=FIXED_NOW)
    assert normalized.blocked_reason in {"auction_state", "limit_state"}
    request = _payload(ticks=[_tick(**{flag: True})])
    normalized = normalize_payload(request, allowed_symbols=["SHFE.rb2610"], now=FIXED_NOW)
    assert normalized.blocked_reason in {"auction_state", "limit_state"}


def test_float_active_order_count_does_not_become_an_integer_zero():
    normalized = normalize_payload(_payload(active_orders={"status": "ready", "count": 0.0}),
                                   allowed_symbols=["SHFE.rb2610"], now=FIXED_NOW)
    assert normalized.payload["active_orders"]["count"] is None


def test_default_service_port_is_distinct_from_isolated_cli_port():
    assert DEFAULT_PORT == 17777
    script = (ROOT / "scripts" / "run_infiny_bridge.py").read_text(encoding="utf-8")
    assert "DEFAULT_ISOLATED_PORT = 17778" in script


def test_v2_canonical_symbol_and_safe_account_boundary():
    normalized = normalize_payload(
        _payload(),
        allowed_symbols=["SHFE.rb2610"],
        now=FIXED_NOW,
    )
    assert normalized.exchange == "SHFE"
    assert normalized.instrument == "rb2610"
    assert normalized.blocked_reason is None
    assert normalized.payload["schema_version"] == 2
    assert normalized.payload["account"] == {
        "status": "ready", "available": 100000, "balance": 120000, "risk": 0.1
    }
    serialized = json.dumps(normalized.payload)
    assert "account_id" not in serialized
    assert "must-not-cross-boundary" not in serialized


def test_naive_sdk_time_is_china_time_and_chronology_is_checked():
    normalized = normalize_payload(
        _payload(ticks=[_tick("2026-09-09 09:00:00")]),
        allowed_symbols=["SHFE.rb2610"],
        now=FIXED_NOW,
    )
    assert normalized.blocked_reason is None
    reversed_ticks = [
        _tick("2026-09-09T09:01:00+08:00"),
        _tick("2026-09-09T09:00:00+08:00"),
    ]
    normalized = normalize_payload(
        _payload(ticks=reversed_ticks),
        allowed_symbols=["SHFE.rb2610"],
        now=FIXED_NOW,
    )
    assert normalized.blocked_reason == "tick_chronology"


def test_real_http_analyze_always_holds_and_blocks_candidate(running_bridge):
    address, service, calls, _audit = running_bridge
    status, body, raw = _request(
        address, "POST", "/analyze", _payload(),
        headers={"X-Infiny-Source": "claimed-client"},
    )
    assert status == 200
    assert set(body) == {"signal", "reason"}
    assert body["signal"] == "hold"
    assert len(body["reason"]) <= 20
    assert body["reason"] == "candidate_blocked"
    assert len(calls) == 1
    assert "buy" not in raw
    assert service.status()["counters"]["broker_requests"] == 1


def test_heartbeat_returns_same_hold_shape_and_reports_nested_telemetry(running_bridge):
    address, service, calls, _audit = running_bridge
    heartbeat = {
        "schema_version": 2,
        "heartbeat": True,
        "exchange": "SHFE",
        "instrument_id": "rb2610",
        "runtime": {"observer_only": True, "enable_trading": False, "order_submission_count": 7},
    }
    status, body, _raw = _request(address, "POST", "/heartbeat", heartbeat)
    assert status == 200
    assert body["signal"] == "hold"
    assert set(body) == {"signal", "reason"}
    assert calls == []
    snapshot = service.status()
    assert snapshot["telemetry"]["order_submissions"] == 7
    assert snapshot["telemetry"]["order_submissions_verified"] is False
    assert snapshot["telemetry"]["native_order_submission_count"] is None


def test_unknown_symbol_test_marker_and_status_truthfulness(running_bridge):
    address, service, calls, _audit = running_bridge
    unknown = _payload(instrument_id="cu2610", batch_id="unknown")
    status, body, _raw = _request(address, "POST", "/analyze", unknown)
    assert status == 200
    assert body == {"signal": "hold", "reason": "symbol_unknown"}
    assert calls == []

    synthetic = _payload(exchange="TEST", batch_id="synthetic")
    status, body, _raw = _request(address, "POST", "/analyze", synthetic)
    assert status == 200
    assert body["signal"] == "hold"
    snapshot = service.status()
    assert snapshot["counters"]["synthetic_requests"] == 1
    assert snapshot["counters"]["broker_requests"] == 1
    assert snapshot["client"]["source_header_trust"] == "claimed_unverified"


def test_stale_limit_and_bad_quote_are_holds_without_evaluator(running_bridge):
    address, _service, calls, _audit = running_bridge
    stale = _payload(
        batch_id="stale",
        ticks=[_tick("2026-09-09T08:00:00+08:00")],
    )
    assert _request(address, "POST", "/analyze", stale)[1] == {
        "signal": "hold", "reason": "stale_tick"
    }
    limited = _payload(batch_id="limit", market_status="limit_up")
    assert _request(address, "POST", "/analyze", limited)[1] == {
        "signal": "hold", "reason": "limit_state"
    }
    bad_quote = _payload(batch_id="bad-quote", ticks=[_tick(bid1=4100, ask1=4000)])
    status, body, raw = _request(address, "POST", "/analyze", bad_quote)
    assert status == 400
    assert body["signal"] == "hold"
    assert "<html" not in raw.lower()
    assert calls == []


def test_duplicate_json_nan_and_unknown_paths_never_return_html(running_bridge):
    address, _service, _calls, _audit = running_bridge
    for raw in (
        b'{"schema_version":2,"schema_version":2}',
        b'{"schema_version":2,"exchange":"SHFE","instrument_id":"rb2610","ticks":[{"last":NaN}]}',
    ):
        status, body, response_text = _request(address, "POST", "/analyze", raw=raw)
        assert status == 400
        assert body["signal"] == "hold"
        assert "<html" not in response_text.lower()
    status, body, response_text = _request(address, "POST", "/not-an-endpoint", _payload())
    assert status == 404
    assert body["signal"] == "hold"
    assert "<html" not in response_text.lower()


def test_status_and_health_have_observer_flags_and_recent_receipt(running_bridge):
    address, _service, _calls, _audit = running_bridge
    status, health, _raw = _request(address, "GET", "/health")
    assert status == 200
    assert health["observer_only"] is True
    assert health["execution_enabled"] is False
    assert health["client_status"] == "awaiting_client"
    _request(address, "POST", "/heartbeat", {"schema_version": 2})
    status, snapshot, _raw = _request(address, "GET", "/status")
    assert status == 200
    assert snapshot["observer_only"] is True
    assert snapshot["execution_enabled"] is False
    assert snapshot["client"]["status"] == "client_seen"
    assert snapshot["recent_receipt"]["decision"] == "hold"
    assert "account_id" not in json.dumps(snapshot)


def test_optional_local_token_is_enforced_without_echoing_it(tmp_path: Path):
    service = BridgeService(local_token="local-secret", clock=FixedClock())
    server = build_server(port=0, service=service, max_workers=1)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = ("127.0.0.1", server.server_address[1])
    try:
        status, body, raw = _request(address, "GET", "/health")
        assert status == 401
        assert body == {"signal": "hold", "reason": "unauthorized"}
        assert "local-secret" not in raw
        status, body, _raw = _request(
            address, "GET", "/health", headers={"X-Bridge-Token": "local-secret"}
        )
        assert status == 200
        assert body["observer_only"] is True
    finally:
        server.shutdown()
        server.server_close()
        service.close()
        thread.join(timeout=2)


def test_replay_is_bounded_and_sequence_remains_monotonic(running_bridge):
    address, service, calls, _audit = running_bridge
    first = _request(address, "POST", "/analyze", _payload())[1]
    second = _request(address, "POST", "/analyze", _payload())[1]
    assert first == second == {"signal": "hold", "reason": "candidate_blocked"}
    assert len(calls) == 1
    snapshot = service.status()
    assert snapshot["counters"]["replays"] == 1
    assert snapshot["sequence"] == 2
    assert snapshot["recent_receipts"][-1]["blocked_reason"] == "replay"


def test_evaluator_timeout_does_not_create_unbounded_workers():
    started = threading.Event()

    def slow(_payload: dict[str, Any]) -> dict[str, Any]:
        started.set()
        time.sleep(0.15)
        return {"candidate_signal": "sell", "reason": "late"}

    service = BridgeService(
        slow,
        allowed_symbols=["SHFE.rb2610"],
        clock=FixedClock(),
        evaluation_budget_seconds=0.02,
        evaluator_workers=1,
    )
    try:
        first = service.analyze(_payload(batch_id="timeout-1"))
        assert started.is_set()
        assert first == {"signal": "hold", "reason": "evaluator_timeout"}
        second = service.analyze(_payload(batch_id="timeout-2"))
        assert second == {"signal": "hold", "reason": "evaluator_busy"}
        assert service.status()["counters"]["evaluator_timeouts"] == 1
        assert service.status()["counters"]["evaluator_busy"] == 1
    finally:
        service.close()
        time.sleep(0.16)


def test_audit_is_jsonl_scrubbed_and_only_records_hold(running_bridge):
    address, _service, _calls, audit = running_bridge
    _request(
        address, "POST", "/analyze", _payload(),
        headers={"X-Infiny-Source": "adapter"},
    )
    record = json.loads(audit.read_text(encoding="utf-8").splitlines()[-1])
    assert record["decision"] == "hold"
    assert record["instrument"] == "rb2610"
    assert record["source_header"] == "adapter"
    encoded = json.dumps(record)
    assert "account_id" not in encoded
    assert "must-not-cross-boundary" not in encoded
    assert "enable_trading" not in encoded


def test_payload_limit_is_json_hold_error_without_html(running_bridge):
    address, _service, _calls, _audit = running_bridge
    raw = b"{" + b"a" * MAX_BODY_BYTES + b"}"
    status, body, response_text = _request(address, "POST", "/analyze", raw=raw)
    assert status == 400
    assert body == {"signal": "hold", "reason": "payload_too_large"}
    assert "<html" not in response_text.lower()


def test_cli_dry_run_uses_isolated_port_and_no_credentials(tmp_path: Path):
    audit = tmp_path / "cli.jsonl"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_infiny_bridge.py"),
            "--dry-run",
            "--symbol",
            "SHFE.rb2610",
            "--audit-log",
            str(audit),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )
    assert result.returncode == 0
    startup = json.loads(result.stdout.strip())
    assert startup["port"] == 17778
    assert startup["observer_only"] is True
    assert startup["execution_enabled"] is False
    assert "TQ_USER" not in result.stdout
    assert "tqsdk" not in result.stdout.lower()
