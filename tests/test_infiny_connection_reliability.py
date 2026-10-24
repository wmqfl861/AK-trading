from datetime import timedelta

import pytest

from ak_trading.infiny_bridge.connection import ConnectionMonitor, TimingPolicy, time_freshness
from test_infiny_connection_liveness import MutableClock


HOLD = {"signal": "hold", "reason": "heartbeat"}


def meta(clock, *, epoch="epoch-1", sequence=1, **changes):
    return {"instance_id": "instance-1", "session_epoch": epoch, "sequence": sequence,
            "request_id": f"request-{sequence}", "sent_at": clock.now().isoformat(), **changes}


def test_old_epoch_request_cannot_roll_monitor_back():
    clock = MutableClock()
    monitor = ConnectionMonitor(clock=clock)
    monitor.start("instance-1", "epoch-1")
    first = meta(clock)
    assert monitor.note_request(first)
    assert monitor.note_response(first, HOLD)
    monitor.start("instance-1", "epoch-2")
    current = meta(clock, epoch="epoch-2")
    assert monitor.note_request(current)
    assert monitor.note_response(current, HOLD)
    # A late request from the superseded epoch must not restart that epoch.
    assert not monitor.note_request(first)
    assert not monitor.note_response(first, HOLD)
    status = monitor.status()
    assert status["session_epoch"] == "epoch-2" and status["connection"] == "roundtrip"
    assert status["client_verified"] is False


def test_link_expires_after_last_matching_reply():
    clock = MutableClock()
    monitor = ConnectionMonitor(clock=clock)
    monitor.start("instance-1", "epoch-1")
    request = meta(clock)
    monitor.note_request(request)
    assert monitor.note_response(request, HOLD)
    clock.advance(15)
    assert monitor.status()["connection"] == "roundtrip"
    clock.advance(0.001)
    assert monitor.status()["connection"] == "stale"


@pytest.mark.parametrize("component,ttl", [("packet", 10), ("quote", 3), ("account", 5), ("position", 5), ("contract", 3600)])
def test_timing_policy_inclusive_boundary_and_missing_timestamp(component, ttl):
    clock = MutableClock()
    assert getattr(TimingPolicy(), component + "_ttl") == ttl
    at = clock.now().isoformat()
    clock.advance(ttl)
    assert time_freshness(at, clock.now(), ttl)["status"] == "fresh"
    clock.advance(0.001)
    assert time_freshness(at, clock.now(), ttl)["status"] == "stale"
    assert time_freshness(None, clock.now(), ttl)["status"] == "unknown"


def test_future_skew_tolerance_does_not_fabricate_calendar_time():
    clock = MutableClock()
    assert TimingPolicy().future_skew == 2
    assert time_freshness((clock.now() + timedelta(seconds=2)).isoformat(), clock.now(), 10)["status"] == "fresh"
    assert time_freshness((clock.now() + timedelta(seconds=2.001)).isoformat(), clock.now(), 10)["status"] == "future"
    assert time_freshness("21:00:00", clock.now(), 10)["status"] == "unknown"


@pytest.mark.parametrize("bad_result", [{"signal": "buy", "reason": "x"}, {"signal": "hold", "reason": "x", "other": 1}, {}, {"signal": "hold", "reason": ""}])
def test_monitor_requires_exact_hold_reply_and_correlated_request(bad_result):
    clock = MutableClock()
    monitor = ConnectionMonitor(clock=clock)
    monitor.start("instance-1", "epoch-1")
    request = meta(clock)
    monitor.note_request(request)
    assert not monitor.note_response(request, bad_result)
    assert not monitor.note_response({**request, "request_id": "foreign"}, HOLD)
    assert monitor.status()["connection"] != "roundtrip"


def test_monitor_stop_and_clock_faults_cannot_be_refreshed_by_old_reply():
    clock = MutableClock()
    monitor = ConnectionMonitor(clock=clock)
    monitor.start("instance-1", "epoch-1")
    request = meta(clock)
    monitor.note_request(request)
    assert monitor.note_response(request, HOLD)
    clock.elapsed -= 1
    assert monitor.status()["connection"] == "stale"
    monitor.stop()
    assert not monitor.note_response(request, HOLD)
    assert monitor.status()["connection"] == "offline"
