"""SYNTHETIC disabled PythonGO snapshot capture through the actual client lane.

The client is copied alone into a temporary directory. All native data comes
from this fake; the HTTP service and Ledger are real, isolated temporary state.
AK_INFINY_SNAPSHOT_CLIENT selects the preserved baseline or legacy candidate.
"""
from __future__ import annotations

import builtins
import copy
import hashlib
import json
import os
import socket
import sqlite3
import sys
import threading
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from ak_trading.infiny_execution import contracts as c
from ak_trading.infiny_execution import native_contracts as nc
from ak_trading.infiny_execution.client_service import (
    ExecutionService,
    build_execution_server,
)
from ak_trading.infiny_execution.ledger import Ledger
import infiny_native_fakes as fakes


TOKEN = "ab" * 32  # SYNTHETIC fixture token, never a production credential.
DAY = date(2026, 9, 14)
INVESTOR = "SYNTHETIC-ACCOUNT-A"
CHINA = timezone(timedelta(hours=8))
QUERY_SURFACES = frozenset({
    "getInvestorList", "getInvestorAccount", "getInvestorPosition",
    "getInstrument", "subMarketData", "unsubMarketData",
})
POSITION_QUANTITIES = (
    "Position", "PositionClose", "FrozenClosing", "YdPositionClose", "YdFrozenClosing",
)


class SnapshotNative(fakes.FakeNativeModule):
    """Verified native signatures with mutable SYNTHETIC raw SDK return values."""

    def __init__(self):
        super().__init__()
        self.native_lookups = []
        self.failures = set()
        self.read_durations = {}
        self.read_clock = None
        self.query_completed = {}
        self.account = {"InvestorID": INVESTOR, "Available": 125000.25,
                        "CurrencyID": "CNY"}
        self.positions = [{
            "InvestorID": INVESTOR, "Exchange": "SHFE",
            "InstrumentID": "rb2610", "HedgeFlag": "1",
            "Direction": "\u591a", "Position": 5, "PositionClose": 4,
            "FrozenClosing": 1, "YdPositionClose": 3,
            "YdFrozenClosing": 1,
        }]
        self.contract = {
            "Exchange": "SHFE", "InstrumentID": "rb2610",
            "PriceTick": 1, "VolumeMultiple": 10,
            "MinLimitOrderVolume": 1, "MaxLimitOrderVolume": 500,
        }

        def record(name, result):
            def call(*args, **kwargs):
                with self.lock:
                    self.calls.append((name, {"args": args, "kwargs": kwargs},
                                       threading.current_thread()))
                if name in self.failures:
                    raise RuntimeError("SYNTHETIC native query failure")
                if self.read_clock is not None:
                    self.read_clock.advance(self.read_durations.get(name, 0))
                value = result()
                if self.read_clock is not None:
                    self.query_completed[name] = self.read_clock.now()
                return value
            return call

        self.getInvestorAccount = record("getInvestorAccount", lambda: self.account)
        self.getInvestorPosition = record("getInvestorPosition", lambda: self.positions)
        self.getInstrument = record("getInstrument", lambda: self.contract)
        self.subMarketData = record("subMarketData", lambda: None)
        self.unsubMarketData = record("unsubMarketData", lambda: None)

    def __getattribute__(self, name):
        if name in QUERY_SURFACES:
            state = super().__getattribute__("__dict__")
            if "native_lookups" in state:
                state["native_lookups"].append((name, threading.current_thread()))
        return super().__getattribute__(name)


def raw_tick(**changes):
    result = {
        "Exchange": "SHFE", "InstrumentID": "rb2610",
        "TradingDay": DAY.strftime("%Y%m%d"),
        "Datetime": datetime(2026, 9, 14, 9, 30, 1),
        "LastPrice": 3510, "BidPrice1": 3509, "AskPrice1": 3511,
        "BidVolume1": 6, "AskVolume1": 8,
        "UpperLimitPrice": 3800, "LowerLimitPrice": 3200,
    }
    result.update(changes)
    return result


class RealSnapshotHarness:
    """Public lifecycle/callbacks, real authenticated HTTP and temporary journal."""

    def __init__(self, monkeypatch, tmp_path, *, http_timeout=3.0):
        selected = Path(os.environ.get("AK_INFINY_SNAPSHOT_CLIENT", fakes.CLIENT_SOURCE))
        monkeypatch.setattr(fakes, "CLIENT_SOURCE", selected)
        self.native = SnapshotNative()
        monkeypatch.setitem(sys.modules, "INFINIGO", self.native)
        normal_import = builtins.__import__

        def no_repository_import(name, *args, **kwargs):
            assert not name.startswith("ak_trading"), "client imported repository code"
            return normal_import(name, *args, **kwargs)

        with monkeypatch.context() as imports:
            imports.setattr(builtins, "__import__", no_repository_import)
            self.module, _ = fakes.load_client_from_temp(
                monkeypatch, tmp_path, fake=self.native)

        def no_client_repository_import(name, globals=None, locals=None, fromlist=(), level=0):
            if globals is not None and globals.get("__name__") == self.module.__name__:
                assert not name.startswith("ak_trading"), "client imported repository code at runtime"
            return normal_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", no_client_repository_import)
        self.client_clock = fakes.StepClock()
        self.client_clock._now = datetime(2026, 9, 14, 1, 30, tzinfo=timezone.utc)
        self.native.read_clock = self.client_clock
        monkeypatch.setattr(self.module, "_now_utc", self.client_clock.now)
        client_time = SimpleNamespace(**vars(self.module.time))
        client_time.monotonic = self.client_clock.monotonic
        monkeypatch.setattr(self.module, "time", client_time)
        self.bridge = self.module.AKExecutionBridge(
            native=self.native, store_directory=tmp_path / "client-store",
            pairing_token=TOKEN, http_timeout=http_timeout)
        self.constructor_calls = list(self.native.calls)
        self.constructor_lookups = list(self.native.native_lookups)
        self.bridge.strategy_id = 23
        self.bridge.strategy_name = "SYNTHETIC"
        # Drive the actual lanes explicitly to keep this test deterministic.
        monkeypatch.setattr(self.bridge, "_spawn_lanes", lambda: None)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        self.bridge.set_params({
            "exchange": "SHFE", "instrument_id": "rb2610",
            "hedge_flag": "1", "investor_id": INVESTOR,
            "execution_api_url": f"http://127.0.0.1:{port}/execution/v1/poll",
        })
        self.bridge.on_init()
        self.bridge.on_start()
        self.binding = c.Binding(
            account_key="SIM_ACCOUNT_A", strategy_key="SIM_STRATEGY_S",
            strategy_id=23, instance_id=self.bridge._instance_id,
            session_epoch=self.bridge._session_epoch, trading_day=DAY,
            exchange="SHFE", instrument_id="rb2610", hedge_flag="1")
        self.clock = fakes.StepClock()
        self.journal_path = tmp_path / "snapshot.sqlite"
        self.journal = Ledger(self.journal_path, clock=self.clock.now, schema="v2")
        with self.journal.maintenance():
            self.registration_revision = self.journal.register_actor(
                "SIM_ACTOR_SNAPSHOT", self.binding.account_key, (self.binding,),
                operator_action_id="SIM_OP_SNAPSHOT", manifest_sha256="ab" * 32,
                contract_lock_sha256="cd" * 32)
        self.service = ExecutionService(
            self.journal, tokens={self.binding.instance_id: TOKEN}, clock=self.clock,
            audit_log=tmp_path / "snapshot-audit.jsonl")
        self.server = build_execution_server(service=self.service, port=port)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.polls = []
        self.responses = []
        self.posts = []
        self.response_transform = None
        self.before_response = None
        self.after_response_packet = None
        original = self.bridge._post

        def observed_post(path, packet, **kwargs):
            self.posts.append((path, copy.deepcopy(packet)))
            if path == "/execution/v1/poll":
                self.polls.append(json.loads(json.dumps(packet)))
            response = original(path, packet, **kwargs)
            if path == "/execution/v1/poll":
                self.responses.append(json.loads(json.dumps(response)))
                if self.before_response is not None:
                    self.before_response()
                if self.response_transform is not None:
                    response = self.response_transform(copy.deepcopy(response))
                if self.after_response_packet is not None:
                    self.after_response_packet(packet)
            return response

        monkeypatch.setattr(self.bridge, "_post", observed_post)

    def poll(self):
        self.clock.advance(5.1)
        before = len(self.polls)
        self.bridge._control_cycle()
        return self.polls[-1] if len(self.polls) > before else None

    def handshake(self):
        packet = self.poll()
        data = packet["data"]
        assert data["service_epoch"] is None
        assert data["snapshot"] is None
        assert data["snapshot_id"] is None
        assert data["ack_watermark"] is None
        assert self.bridge._service_epoch == self.service.service_epoch
        assert self.responses[-1]["data"]["binding"] == json.loads(c.encode(self.binding))["data"]
        return packet

    def snapshot(self, tick=None):
        self.bridge.on_tick(raw_tick() if tick is None else tick)
        packet = self.poll()
        request = nc.decode(json.dumps(packet), nc.PollRequest)
        assert request.snapshot is not None, "callback produced no typed snapshot"
        assert self.journal.capture_registered(request.snapshot.snapshot_id)
        return request.snapshot

    def capture_row(self, snapshot_id):
        with sqlite3.connect(self.journal_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT raw_hash,trusted_received_at FROM snapshot_captures WHERE snapshot_id=?",
                (snapshot_id,)).fetchone()
        assert row is not None
        return dict(row)

    def replace_service_binding(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.service.close()
        self.binding = replace(self.binding, instance_id=self.bridge._instance_id,
                               session_epoch=self.bridge._session_epoch)
        with self.journal.maintenance():
            self.registration_revision = self.journal.register_actor(
                "SIM_ACTOR_SNAPSHOT", self.binding.account_key, (self.binding,),
                operator_action_id="SIM_OP_SNAPSHOT_NEW", manifest_sha256="ac" * 32,
                contract_lock_sha256="cd" * 32,
                expected_registration_revision=self.registration_revision)
        port = self.server.server_address[1]
        self.service = ExecutionService(
            self.journal, tokens={self.binding.instance_id: TOKEN}, clock=self.clock)
        self.server = build_execution_server(service=self.service, port=port)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def close(self):
        try:
            fakes.stop_started_bridge(self.bridge)
        finally:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
            self.service.close()
            self.journal.close()


@pytest.fixture
def harness(monkeypatch, tmp_path, request):
    value = RealSnapshotHarness(monkeypatch, tmp_path,
                                http_timeout=getattr(request, "param", 3.0))
    try:
        yield value
    finally:
        try:
            assert value.native.send_calls() == []
            assert value.native.cancel_calls() == []
            assert value.bridge.counters["native_send_attempts"] == 0
            assert value.bridge.counters["native_cancel_attempts"] == 0
        finally:
            value.close()


def test_h05_h06_real_callback_control_cycle_registers_snapshot(harness):
    harness.handshake()
    harness.bridge.on_tick(raw_tick())
    packet = harness.poll()
    snapshot_body = packet["data"]["snapshot"]
    assert snapshot_body is not None, (
        "real on_tick -> _control_cycle omitted the by-value ExecutionSnapshot")
    request = nc.decode(json.dumps(packet), nc.PollRequest)
    assert request.snapshot is not None
    assert request.snapshot.binding == harness.binding
    assert harness.journal.capture_registered(request.snapshot.snapshot_id)
    assert harness.native.send_calls() == []
    assert harness.native.cancel_calls() == []


def assert_no_snapshot_poll(harness):
    packet = harness.poll()
    if packet is not None:
        assert packet["data"]["snapshot"] is None
        assert packet["data"]["snapshot_id"] is None


def query_counts(native):
    return {name: native.call_counts().get(name, 0) for name in (
        "getInvestorAccount", "getInvestorPosition", "getInstrument")}


def observed_times(timing):
    return timing.sampled_at, timing.source_at, timing.received_at


def test_h01_constructor_and_network_lanes_never_resolve_native(harness):
    assert harness.constructor_calls == []
    assert harness.constructor_lookups == []
    before = list(harness.native.native_lookups)
    errors = []

    def network_lane():
        try:
            harness.handshake()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=network_lane, name="SYNTHETIC-control")
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert errors == []
    assert harness.native.native_lookups == before
    harness.snapshot()
    assert all(thread.ident == threading.get_ident()
               for _, thread in harness.native.native_lookups)
    names = {name for name, _, _ in harness.native.calls}
    assert {"getInvestorList", "getInvestorAccount", "getInvestorPosition",
            "getInstrument"} <= names


@pytest.mark.parametrize("callback", ["on_tick", "on_contract_status", "on_start", "on_stop"])
def test_h01_wrong_callback_thread_fails_before_native_lookup(harness, callback):
    harness.handshake()
    before = list(harness.native.native_lookups)
    errors = []

    def foreign_callback():
        try:
            method = getattr(harness.bridge, callback)
            method(raw_tick()) if callback in {"on_tick", "on_contract_status"} else method()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=foreign_callback, name="SYNTHETIC-foreign-callback")
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert str(errors[0]) == "legal_framework_callback_thread_required"
    assert harness.native.native_lookups == before


def test_h01_subscription_start_stop_is_explicit_and_idempotent(harness):
    bridge = harness.bridge
    calls = [payload for name, payload, _ in harness.native.calls if name == "subMarketData"]
    assert len(calls) == 1
    assert calls[0] == {"args": (), "kwargs": {
        "StrategyObj": bridge, "ExchangeID": "SHFE", "InstrumentID": "rb2610"}}
    bridge.on_start()
    assert harness.native.call_counts()["subMarketData"] == 1
    bridge.on_stop()
    unsubs = [payload for name, payload, _ in harness.native.calls if name == "unsubMarketData"]
    assert unsubs == calls
    assert bridge.params_map.enable_trading is False
    assert getattr(bridge, "trading", False) is False


def test_h01_subscription_failure_is_contained_and_diagnosable(harness):
    harness.bridge.on_stop()
    harness.native.failures.add("subMarketData")
    before = len(harness.native.calls)
    harness.bridge.on_start()
    assert harness.bridge.params_map.enable_trading is False
    diagnostics = [str(payload) for name, payload, _ in harness.native.calls[before:]
                   if name == "writeLog"]
    assert any("sub" in item.lower() for item in diagnostics)
    assert all(TOKEN not in item for item in diagnostics)


@pytest.mark.parametrize("path,bad", [
    (("instance_id",), "SIM_OTHER_INSTANCE"),
    (("session_epoch",), "SIM_OTHER_SESSION"),
    (("generation",), "SIM_OTHER_GENERATION"),
    (("request_id",), "SIM_OTHER_REQUEST"),
    (("sequence",), 999),
    (("params_sha256",), "ef" * 32),
    (("ledger_revision",), -1),
    (("ledger_revision",), True),
    (("ledger_revision",), "1"),
    (("service_epoch",), 0),
    (("service_epoch",), True),
    (("service_epoch",), None),
    (("binding",), None),
    (("binding", "instance_id"), "SIM_OTHER_INSTANCE"),
    (("binding", "session_epoch"), "SIM_OTHER_SESSION"),
    (("binding", "strategy_id"), 24),
    (("binding", "exchange"), "DCE"),
    (("binding", "instrument_id"), "rb2609"),
    (("binding", "hedge_flag"), "2"),
    (("binding", "trading_day"), "2026-02-30"),
])
def test_h02_invalid_authenticated_context_prevents_capture(harness, path, bad):
    def changed(response):
        target = response["data"]
        for field in path[:-1]:
            target = target[field]
        target[path[-1]] = bad
        return response

    harness.response_transform = changed
    try:
        harness.poll()
    except harness.module.ContractError:
        pass
    harness.bridge.on_tick(raw_tick())
    try:
        assert_no_snapshot_poll(harness)
    except harness.module.ContractError:
        assert harness.polls[-1]["data"]["snapshot"] is None


@pytest.mark.parametrize("field,value", [
    ("account_key", "SIM_OTHER_ACCOUNT"),
    ("strategy_key", "SIM_OTHER_STRATEGY"),
    ("trading_day", "2026-09-15"),
])
def test_h02_validated_lifecycle_cannot_silently_retarget_binding(harness, field, value):
    harness.handshake()
    first = harness.snapshot()

    def changed(response):
        response["data"]["binding"][field] = value
        return response

    harness.response_transform = changed
    try:
        harness.poll()
    except harness.module.ContractError:
        pass
    try:
        assert_no_snapshot_poll(harness)
    except harness.module.ContractError:
        assert harness.polls[-1]["data"]["snapshot"] is None
    assert harness.journal.capture_registered(first.snapshot_id)


def test_h02_parameters_retire_a_frozen_capture(harness):
    harness.handshake()
    harness.snapshot()
    harness.bridge.set_params({"investor_id": "SYNTHETIC-OTHER"})
    assert_no_snapshot_poll(harness)


def test_h02_old_inflight_response_cannot_populate_restarted_lifecycle(harness):
    old_session = harness.bridge._session_epoch

    def restarted():
        harness.before_response = None
        harness.bridge.on_stop()
        harness.bridge.on_start()

    harness.before_response = restarted
    harness.poll()
    assert harness.bridge._session_epoch != old_session
    assert harness.bridge._service_epoch is None
    harness.replace_service_binding()
    harness.handshake()
    assert_no_snapshot_poll(harness)
    snapshot = harness.snapshot()
    assert snapshot.binding.session_epoch == harness.bridge._session_epoch


def test_h02_stop_start_and_new_service_epoch_retire_old_facts(harness):
    harness.handshake()
    first = harness.snapshot()
    harness.bridge.on_stop()
    harness.bridge.on_start()
    harness.replace_service_binding()
    harness.handshake()
    assert_no_snapshot_poll(harness)
    second = harness.snapshot()
    assert second.snapshot_id != first.snapshot_id
    assert second.binding.session_epoch != first.binding.session_epoch


def test_h03_exact_configured_investor_is_selected_among_multiple(harness):
    harness.native.investors = [{"InvestorID": "SYNTHETIC-OTHER"}, {"InvestorID": INVESTOR}]
    harness.handshake()
    snapshot = harness.snapshot()
    assert snapshot.account.available == Decimal("125000.25")
    assert len(snapshot.positions) == 1
    account_call = next(payload for name, payload, _ in harness.native.calls
                        if name == "getInvestorAccount")
    position_call = next(payload for name, payload, _ in harness.native.calls
                         if name == "getInvestorPosition")
    assert account_call == {"args": (INVESTOR,), "kwargs": {}}
    assert position_call == {"args": (INVESTOR,), "kwargs": {"Simple": True}}


def test_h03_unique_verified_investor_may_supply_missing_selection(harness):
    harness.bridge.set_params({"investor_id": ""})
    harness.handshake()
    snapshot = harness.snapshot()
    assert snapshot.account.available == Decimal("125000.25")
    assert snapshot.binding.account_key == "SIM_ACCOUNT_A"


@pytest.mark.parametrize("configured,investors", [
    (INVESTOR, [{"InvestorID": "SYNTHETIC-OTHER"}]),
    ("", [{"InvestorID": INVESTOR}, {"InvestorID": "SYNTHETIC-OTHER"}]),
    (INVESTOR, [{"InvestorID": INVESTOR}, {"InvestorID": INVESTOR}]),
    ("", [{}]),
])
def test_h03_ambiguous_or_mismatched_investor_is_unavailable(harness, configured, investors):
    harness.bridge.set_params({"investor_id": configured})
    harness.native.investors = investors
    harness.handshake()
    snapshot = harness.snapshot()
    assert snapshot.account is None or snapshot.account.available is None
    assert snapshot.positions == ()
    assert harness.native.call_counts().get("getInvestorAccount", 0) == 0
    assert harness.native.call_counts().get("getInvestorPosition", 0) == 0


@pytest.mark.parametrize("component", ["account", "positions", "contract"])
def test_h03_present_contradictory_identity_clears_component(harness, component):
    harness.handshake()
    if component == "account":
        harness.native.account["InvestorID"] = "SYNTHETIC-OTHER"
    elif component == "positions":
        harness.native.positions[0]["InvestorID"] = "SYNTHETIC-OTHER"
    else:
        harness.native.contract["InstrumentID"] = "rb2609"
    snapshot = harness.snapshot()
    if component == "account":
        assert snapshot.account is None or snapshot.account.available is None
    elif component == "positions":
        assert snapshot.positions == ()
    else:
        assert snapshot.contract is None or snapshot.contract.price_tick is None


def test_h03_position_filter_and_both_native_sides_preserve_five_quantities(harness):
    row = copy.deepcopy(harness.native.positions[0])
    short = {**row, "Direction": "\u7a7a", "Position": 7, "PositionClose": 6,
             "FrozenClosing": 2, "YdPositionClose": 4, "YdFrozenClosing": 1}
    harness.native.positions += [short, {**row, "Exchange": "DCE"},
                                {**row, "InstrumentID": "rb2609"},
                                {**row, "HedgeFlag": "2"}]
    harness.handshake()
    snapshot = harness.snapshot()
    buckets = {bucket.side: bucket for bucket in snapshot.positions}
    assert set(buckets) == {c.Side.LONG, c.Side.SHORT}
    for side, expected in ((c.Side.LONG, row), (c.Side.SHORT, short)):
        assert [getattr(buckets[side], field) for field in POSITION_QUANTITIES] == [
            expected[field] for field in POSITION_QUANTITIES]


@pytest.mark.parametrize("defect", ["duplicate", "hierarchy", "unknown_side", "missing_scope"])
def test_h03_unproven_position_bucket_is_never_summed_or_invented(harness, defect):
    row = harness.native.positions[0]
    if defect == "duplicate":
        harness.native.positions.append(copy.deepcopy(row))
    elif defect == "hierarchy":
        row["PositionClose"] = row["Position"] + 1
    elif defect == "unknown_side":
        row["Direction"] = "SYNTHETIC_UNKNOWN"
    else:
        row.pop("HedgeFlag")
    harness.handshake()
    snapshot = harness.snapshot()
    assert snapshot.positions == ()


@pytest.mark.parametrize("bad", [None, True, False, float("nan"), float("inf"), "invalid"])
def test_h04_bad_native_numbers_are_unknown(harness, bad):
    harness.native.account["Available"] = bad
    harness.native.positions[0]["Position"] = bad
    harness.native.contract["PriceTick"] = bad
    harness.handshake()
    snapshot = harness.snapshot(raw_tick(LastPrice=bad, BidVolume1=bad))
    assert snapshot.quote.last is None
    assert snapshot.quote.bid_volume1 is None
    assert snapshot.account is None or snapshot.account.available is None
    assert snapshot.contract is None or snapshot.contract.price_tick is None
    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].Position is None


def test_h04_missing_is_unknown_and_raw_zero_signed_funds_are_retained(harness):
    harness.native.account["Available"] = -17.5
    for field in POSITION_QUANTITIES:
        harness.native.positions[0][field] = 0
    harness.handshake()
    tick = raw_tick(LastPrice=0, BidVolume1=0, AskVolume1=0)
    tick.pop("AskPrice1")
    snapshot = harness.snapshot(tick)
    assert snapshot.account.available == Decimal("-17.5")
    assert snapshot.quote.last is None
    assert snapshot.quote.ask1 is None
    assert snapshot.quote.bid_volume1 == 0
    assert snapshot.quote.ask_volume1 == 0
    assert [getattr(snapshot.positions[0], field) for field in POSITION_QUANTITIES] == [0] * 5


def test_h04_zero_filled_tick_wrapper_numbers_remain_unknown(harness):
    harness.handshake()
    # Exact installed TickData property names; absent native numbers were
    # irreversibly zero-filled before the callback received this object.
    tick = SimpleNamespace(
        exchange="SHFE", instrument_id="rb2610", trading_day="20260914",
        update_time="09:30:01", datetime=datetime(2026, 9, 14, 9, 30, 1),
        last_price=3510, bid_price1=0, ask_price1=3511,
        bid_volume1=0, ask_volume1=8, upper_limit_price=3800, lower_limit_price=0)
    snapshot = harness.snapshot(tick)
    assert snapshot.quote.last == Decimal("3510")
    assert snapshot.quote.bid1 is None
    assert snapshot.quote.bid_volume1 is None
    assert snapshot.quote.lower_limit is None
    assert snapshot.quote.ask_volume1 == 8
    assert snapshot.quote.time == datetime(2026, 9, 14, 1, 30, 1, tzinfo=timezone.utc)
    assert snapshot.times.quote.source_at == snapshot.quote.time


@pytest.mark.parametrize("source", [
    datetime(2026, 9, 14, 9, 30, 1),
    datetime(2026, 9, 14, 9, 30, 1, tzinfo=CHINA),
    datetime(2026, 9, 11, 21, 15, 2),
])
def test_h04_raw_quote_source_time_including_night_session(harness, source):
    harness.handshake()
    snapshot = harness.snapshot(raw_tick(Datetime=source, UpdateTime="23:59:59"))
    expected = (source.replace(tzinfo=CHINA) if source.tzinfo is None else source).astimezone(timezone.utc)
    assert snapshot.quote.time == expected
    assert snapshot.times.quote.source_at == expected
    assert snapshot.times.quote.sampled_at == harness.client_clock.now()


def test_h04_missing_quote_datetime_never_combines_trading_day_and_update_time(harness):
    harness.handshake()
    tick = raw_tick(UpdateTime="21:15:02")
    tick.pop("Datetime")
    snapshot = harness.snapshot(tick)
    assert snapshot.quote.time is None
    assert snapshot.times.quote.source_at is None
    assert snapshot.times.quote.sampled_at is not None


@pytest.mark.parametrize("bad_time", [None, True, "SYNTHETIC_INVALID_DATETIME"])
def test_h04_invalid_quote_datetime_never_becomes_arrival_time(harness, bad_time):
    harness.handshake()
    snapshot = harness.snapshot(raw_tick(Datetime=bad_time, UpdateTime="09:30:01"))
    assert snapshot.quote.time is None
    assert snapshot.times.quote.source_at is None
    assert snapshot.times.quote.sampled_at is not None


def test_h04_present_mismatched_trading_day_retires_quote(harness):
    harness.handshake()
    harness.snapshot()
    harness.client_clock.advance(1)
    snapshot = harness.snapshot(raw_tick(TradingDay="20260915"))
    assert snapshot.quote is None or all(
        value is None for value in (snapshot.quote.last, snapshot.quote.bid1, snapshot.quote.ask1))


def test_h04_independent_component_read_completion_times_and_cadences(harness):
    harness.native.read_durations = {
        "getInvestorAccount": 0.02, "getInvestorPosition": 0.03, "getInstrument": 0.04}
    harness.handshake()
    first = harness.snapshot()
    completed = dict(harness.native.query_completed)
    assert first.times.account.sampled_at == completed["getInvestorAccount"]
    assert first.times.positions.sampled_at == completed["getInvestorPosition"]
    assert first.times.contract.sampled_at == completed["getInstrument"]
    assert all(bucket.timing.sampled_at == completed["getInvestorPosition"]
               for bucket in first.positions)
    assert len({first.times.account.sampled_at, first.times.positions.sampled_at,
                first.times.contract.sampled_at}) == 3
    counts = query_counts(harness.native)
    harness.client_clock.advance(1)
    second = harness.snapshot(raw_tick(LastPrice=3512))
    assert query_counts(harness.native) == counts
    assert observed_times(second.times.account) == observed_times(first.times.account)
    assert observed_times(second.times.positions) == observed_times(first.times.positions)
    assert observed_times(second.times.contract) == observed_times(first.times.contract)
    harness.client_clock.advance(5)
    third = harness.snapshot(raw_tick(LastPrice=3513))
    third_counts = query_counts(harness.native)
    assert third_counts["getInvestorAccount"] == counts["getInvestorAccount"] + 1
    assert third_counts["getInvestorPosition"] == counts["getInvestorPosition"] + 1
    assert third_counts["getInstrument"] == counts["getInstrument"]
    assert third.times.account.sampled_at > second.times.account.sampled_at
    assert third.times.positions.sampled_at > second.times.positions.sampled_at
    assert observed_times(third.times.contract) == observed_times(second.times.contract)
    harness.client_clock.advance(60)
    fourth = harness.snapshot(raw_tick(LastPrice=3514))
    assert query_counts(harness.native)["getInstrument"] == counts["getInstrument"] + 1
    assert fourth.times.contract.sampled_at > third.times.contract.sampled_at


def test_h04_failed_queries_do_not_renew_old_funds_positions_or_contract(harness):
    harness.handshake()
    first = harness.snapshot()
    harness.native.failures.update({"getInvestorAccount", "getInvestorPosition", "getInstrument"})
    harness.client_clock.advance(61)
    second = harness.snapshot(raw_tick(LastPrice=3512))
    for name in ("account", "positions", "contract"):
        old = getattr(first.times, name).sampled_at
        assert getattr(second.times, name).sampled_at in (None, old)
    assert all(bucket.timing.sampled_at == first.times.positions.sampled_at
               for bucket in second.positions)
    failed_counts = query_counts(harness.native)
    harness.client_clock.advance(1)
    harness.snapshot(raw_tick(LastPrice=3513))
    assert query_counts(harness.native) == failed_counts


def test_h05_complete_typed_wire_has_no_manufactured_receipt_or_coverage(harness):
    harness.handshake()
    snapshot = harness.snapshot()
    assert snapshot.source is c.Source.PYTHONGO
    assert snapshot.times.sent_at is None
    for timing in (snapshot.times, snapshot.times.quote, snapshot.times.account,
                   snapshot.times.positions, snapshot.times.contract,
                   *(bucket.timing for bucket in snapshot.positions)):
        assert timing.received_at is None
    assert snapshot.coverage.level is c.CoverageLevel.UNKNOWN
    assert snapshot.coverage.complete_through is None
    assert snapshot.coverage.completion_evidence_id is None
    assert snapshot.coverage.own_active_count is None
    assert snapshot.coverage.account_active_count is None
    assert snapshot.proof_id is None
    assert snapshot.contract.additional_volume_limits == ()
    assert snapshot.times.account.source_at is None
    assert snapshot.times.positions.source_at is None
    assert snapshot.times.contract.source_at is None
    assert all(getattr(snapshot.times, name).completeness is not c.Completeness.COMPLETE
               for name in ("quote", "account", "positions", "contract"))


def test_h06_native_mutations_cannot_change_frozen_capture(harness):
    harness.handshake()
    tick = raw_tick()
    harness.bridge.on_tick(tick)
    tick["LastPrice"] = 9999
    harness.native.account["Available"] = 999999
    harness.native.positions[0]["Position"] = 999
    harness.native.contract["PriceTick"] = 9
    packet = harness.poll()
    snapshot = nc.decode(json.dumps(packet), nc.PollRequest).snapshot
    assert snapshot.quote.last == Decimal("3510")
    assert snapshot.account.available == Decimal("125000.25")
    assert snapshot.positions[0].Position == 5
    assert snapshot.contract.price_tick == Decimal("1")
    assert harness.journal.capture_registered(snapshot.snapshot_id)


def test_h06_retry_preserves_id_raw_hash_first_trusted_receipt_and_revision(harness):
    harness.handshake()
    first_revision = harness.responses[-1]["data"]["ledger_revision"]
    first = harness.snapshot()
    first_body = copy.deepcopy(harness.polls[-1]["data"]["snapshot"])
    first_row = harness.capture_row(first.snapshot_id)
    first_sent = harness.polls[-1]["data"]["sent_at"]
    assert first.ledger_revision == first_revision
    assert first_row["raw_hash"] == hashlib.sha256(c.encode(first).encode("utf-8")).hexdigest()
    native_counts = query_counts(harness.native)
    harness.client_clock.advance(8)
    second_packet = harness.poll()
    second = nc.decode(json.dumps(second_packet), nc.PollRequest).snapshot
    assert second == first
    assert second_packet["data"]["snapshot"] == first_body
    assert second_packet["data"]["sent_at"] != first_sent
    assert harness.capture_row(first.snapshot_id) == first_row
    assert query_counts(harness.native) == native_counts
    harness.bridge.on_tick(raw_tick(LastPrice=3517))
    third_packet = harness.poll()
    third = nc.decode(json.dumps(third_packet), nc.PollRequest).snapshot
    assert third.snapshot_id != first.snapshot_id
    assert third.quote.last == Decimal("3517")


def test_h06_each_send_gets_a_copy_of_frozen_bytes(harness):
    harness.handshake()

    def mutate_outgoing_after_service_received(packet):
        body = packet["data"]["snapshot"]
        if body is not None:
            body["quote"]["last"] = "9999"
            body["ledger_revision"] = 99999

    harness.after_response_packet = mutate_outgoing_after_service_received
    first = harness.snapshot()
    second = nc.decode(json.dumps(harness.poll()), nc.PollRequest).snapshot
    assert second == first
    assert first.quote.last == Decimal("3510")


def test_h06_callback_before_context_and_poll_without_callback_do_not_fabricate(harness):
    harness.bridge.on_tick(raw_tick())
    harness.handshake()
    assert_no_snapshot_poll(harness)
    assert_no_snapshot_poll(harness)
    harness.snapshot()


def test_h06_no_valid_native_facts_or_quote_produces_no_snapshot(harness):
    harness.native.account = None
    harness.native.positions = None
    harness.native.contract = None
    harness.handshake()
    harness.bridge.on_tick({})
    assert_no_snapshot_poll(harness)


def test_h07_quote_and_status_are_facts_without_order_outbox_entries(harness):
    harness.handshake()
    harness.snapshot()
    harness.bridge.on_contract_status({
        "Exchange": "SHFE", "InstrumentID": "rb2610", "Status": "2"})
    harness.bridge._events_cycle()
    assert not any(path == "/execution/v1/events" for path, _ in harness.posts)


def test_h07_status_only_does_not_refresh_contract_spec_time(harness):
    harness.handshake()
    first = harness.snapshot()
    counts = query_counts(harness.native)
    harness.client_clock.advance(1)
    harness.bridge.on_contract_status({
        "Exchange": "SHFE", "InstrumentID": "rb2610", "Status": "2"})
    packet = harness.poll()
    second = nc.decode(json.dumps(packet), nc.PollRequest).snapshot
    assert observed_times(second.times.contract) == observed_times(first.times.contract)
    assert query_counts(harness.native)["getInstrument"] == counts["getInstrument"]


def test_h07_genuine_order_trade_cancel_error_callbacks_keep_outbox_semantics(harness):
    harness.handshake()
    order = {"Exchange": "SHFE", "InstrumentID": "rb2610", "OrderID": 42,
             "Price": 3510, "Volume": 1}
    harness.bridge.on_order(order)
    harness.bridge.on_trade({**order, "TradeID": "SYNTHETIC-TRADE-1"})
    harness.bridge.on_cancel(order)
    harness.bridge.on_error({"errCode": 7, "Memo": "SYNTHETIC-error"})
    harness.bridge._events_cycle()
    events = [event for path, packet in harness.posts
              if path == "/execution/v1/events" for event in packet["data"]["events"]]
    assert [event["kind"] for event in events] == ["order", "trade", "cancel", "error"]
    assert {field["key"]: field["value"] for field in events[0]["raw_fields"]}["OrderID"] == "42"
    for path, packet in harness.posts:
        if path == "/execution/v1/events":
            assert isinstance(nc.decode(json.dumps(packet), nc.EventBatch), nc.EventBatch)


def test_h08_repo_stripped_single_file_load_uses_only_synthetic_sdk(harness):
    loaded = Path(harness.module.__file__)
    assert loaded.parent.name == "client_only"
    assert list(loaded.parent.iterdir()) == [loaded]
    assert sys.modules["INFINIGO"] is harness.native
    assert "snapshot" in harness.module.ADAPTER_VERSION.lower()
    harness.handshake()
    harness.snapshot()


def registered_control_cycle(harness):
    """One real control cycle registered in the original lifecycle lane table."""
    lane = harness.module._Lane("control")
    errors = []

    def cycle():
        lane.busy = True
        try:
            harness.poll()
        except BaseException as exc:
            errors.append(exc)
        finally:
            lane.busy = False

    lane.thread = threading.Thread(target=cycle, name="SYNTHETIC-registered-control", daemon=True)
    with harness.bridge._state_lock:
        assert "control" not in harness.bridge._lanes
        harness.bridge._lanes["control"] = lane
    lane.thread.start()
    return lane, errors


def snapshot_transport_recorder(harness, monkeypatch):
    records = []
    original = harness.bridge._transport

    def transport(url, packet, timeout, headers=None):
        body = packet["data"].get("snapshot")
        if body is not None:
            records.append({
                "snapshot_id": body["snapshot_id"],
                "authenticated": bool(headers and headers.get("X-Execution-Token") == TOKEN),
                "running": harness.bridge._running,
                "params_match": packet["data"]["params_sha256"] == harness.bridge._params_sha256,
            })
        return original(url, packet, timeout, headers)

    monkeypatch.setattr(harness.bridge, "_transport", transport)
    return records


def test_r2_params_preparation_retirement_rejects_stale_transport(harness, monkeypatch):
    harness.handshake()
    first = harness.snapshot()
    records = snapshot_transport_recorder(harness, monkeypatch)
    prepared = threading.Event()
    release = threading.Event()
    original_endpoint = harness.module._endpoint

    def paused_endpoint(base, path):
        result = original_endpoint(base, path)
        if path == "/execution/v1/poll":
            prepared.set()
            assert release.wait(5), "SYNTHETIC endpoint preparation release timed out"
        return result

    monkeypatch.setattr(harness.module, "_endpoint", paused_endpoint)
    lane, errors = registered_control_cycle(harness)
    try:
        assert prepared.wait(5), "registered control lane did not reach endpoint preparation"
        assert harness.bridge._lanes["control"] is lane
        previous_params = harness.bridge._params_sha256
        harness.bridge.set_params({"order_volume": 2})
        assert harness.bridge._params_sha256 != previous_params
    finally:
        release.set()
        lane.thread.join(timeout=5)
    assert not lane.thread.is_alive()
    assert errors == []
    assert records == [], (
        "retired snapshot entered authenticated transport after legal parameter callback",
        records)
    assert harness.journal.capture_registered(first.snapshot_id)


def test_r2_stop_after_original_auth_preparation_rejects_stale_transport(harness, monkeypatch):
    harness.handshake()
    harness.snapshot()
    records = snapshot_transport_recorder(harness, monkeypatch)
    prepared = threading.Event()
    release = threading.Event()
    owner_thread = threading.get_ident()
    original_auth = harness.bridge._auth_headers
    original_report = harness.bridge._report_control

    def paused_original_auth():
        headers = original_auth()
        if threading.get_ident() != owner_thread:
            assert headers.get("X-Execution-Token") == TOKEN
            prepared.set()
            assert release.wait(5), "SYNTHETIC auth preparation release timed out"
        return headers

    def release_before_original_stopped_report(kind, *args, **kwargs):
        if kind == "STOPPED":
            release.set()
        return original_report(kind, *args, **kwargs)

    monkeypatch.setattr(harness.bridge, "_auth_headers", paused_original_auth)
    monkeypatch.setattr(harness.bridge, "_report_control", release_before_original_stopped_report)
    lane, errors = registered_control_cycle(harness)
    try:
        assert prepared.wait(5), "registered lane did not prepare original authentication"
        assert harness.bridge._lanes["control"] is lane
        harness.bridge.on_stop()
    finally:
        release.set()
        lane.thread.join(timeout=5)
    assert not lane.thread.is_alive()
    assert errors == []
    assert harness.bridge._stop_incomplete is False
    assert records == [], (
        "retired snapshot entered transport after original authenticated preparation and legal stop",
        records)


@pytest.mark.parametrize("stage", ["serialization", "connection", "request_resolution"])
def test_r2_builtin_preparation_retirement_rejects_connection_request(harness, monkeypatch, stage):
    harness.handshake()
    harness.snapshot()
    prepared = threading.Event()
    release = threading.Event()
    owner_thread = threading.get_ident()
    connections = []
    requests = []
    response_bytes = json.dumps(harness.responses[-1]).encode("utf-8")

    def pause_here():
        if threading.get_ident() != owner_thread:
            prepared.set()
            assert release.wait(5), "SYNTHETIC built-in preparation release timed out"

    class FakeHTTPConnection:
        def __init__(self, host, port, timeout):
            self.closed = False
            connections.append(self)
            if stage == "connection":
                pause_here()

        def __getattribute__(self, name):
            if name == "request" and stage == "request_resolution":
                pause_here()
            return object.__getattribute__(self, name)

        def request(self, method, path, body, headers):
            packet = json.loads(body)
            if packet["data"].get("snapshot") is not None:
                requests.append({
                    "snapshot_id": packet["data"]["snapshot_id"],
                    "authenticated": headers.get("X-Execution-Token") == TOKEN,
                    "params_match": packet["data"]["params_sha256"] == harness.bridge._params_sha256,
                })

        def getresponse(self):
            return SimpleNamespace(status=200, read=lambda limit: response_bytes)

        def close(self):
            self.closed = True

    monkeypatch.setattr(harness.module, "HTTPConnection", FakeHTTPConnection)
    monkeypatch.setattr(harness.module, "HTTPSConnection", FakeHTTPConnection)
    if stage == "serialization":
        original_dumps = harness.module.json.dumps
        client_json = SimpleNamespace(**vars(harness.module.json))

        def paused_serialization(value, *args, **kwargs):
            result = original_dumps(value, *args, **kwargs)
            if isinstance(value, dict) and value.get("type") == "PollRequest" \
                    and value["data"].get("snapshot") is not None:
                pause_here()
            return result

        client_json.dumps = paused_serialization
        monkeypatch.setattr(harness.module, "json", client_json)
    lane, errors = registered_control_cycle(harness)
    try:
        assert prepared.wait(5), "registered control lane did not reach built-in preparation"
        assert harness.bridge._lanes["control"] is lane
        harness.bridge.set_params({"order_volume": 2})
    finally:
        release.set()
        lane.thread.join(timeout=5)
    assert not lane.thread.is_alive()
    assert errors == []
    assert connections, "built-in transport never constructed its fake connection"
    assert all(connection.closed for connection in connections)
    assert requests == [], (
        "retired snapshot reached built-in connection.request after preparation",
        stage, requests)


def test_r2_admitted_blocked_io_keeps_stop_bounded_and_blocks_successor(harness, monkeypatch):
    harness.handshake()
    first = harness.snapshot()
    monkeypatch.setattr(harness.bridge, "_http_timeout", 0.1)
    entered = threading.Event()
    release = threading.Event()
    records = []
    original_transport = harness.bridge._transport

    def blocked_admitted_transport(url, packet, timeout, headers=None):
        if packet["data"].get("snapshot") is not None:
            assert headers.get("X-Execution-Token") == TOKEN
            records.append(copy.deepcopy(packet))
            entered.set()
            assert release.wait(5), "SYNTHETIC admitted I/O release timed out"
        return original_transport(url, packet, timeout, headers)

    monkeypatch.setattr(harness.bridge, "_transport", blocked_admitted_transport)
    lane, errors = registered_control_cycle(harness)
    emergency_release = threading.Timer(1.5, release.set)
    emergency_release.daemon = True
    try:
        assert entered.wait(5), "registered lane did not enter admitted transport"
        assert harness.bridge._lanes["control"] is lane
        emergency_release.start()
        started = time.monotonic()
        harness.bridge.on_stop()
        elapsed = time.monotonic() - started
        assert elapsed < 1.2, "stop was serialized behind blocked network I/O"
        assert lane.thread.is_alive(), "already-admitted worker unexpectedly completed before release"
        assert harness.bridge._stop_incomplete is True
        harness.bridge.on_start()
        assert harness.bridge._running is False
        assert harness.bridge._stop_incomplete is True
    finally:
        release.set()
        emergency_release.cancel()
        if emergency_release.ident is not None:
            emergency_release.join(timeout=2)
        lane.thread.join(timeout=5)
        # An incomplete stop deliberately retained the store while the old
        # registered worker survived. Only release it after that worker exits.
        if not lane.thread.is_alive():
            harness.bridge.on_stop()
    assert not lane.thread.is_alive()
    assert errors == []
    assert len(records) == 1
    assert records[0]["data"]["snapshot"]["snapshot_id"] == first.snapshot_id
    assert harness.bridge._service_epoch is None
