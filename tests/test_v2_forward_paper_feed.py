"""SYNTHETIC SDK boundary, CLI and resource controls; no real SDK/network."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ak_trading.v2.schemas import parse_json_bytes
from ak_trading.v2.artifacts import canonical_json_bytes, sha256_hex
from ak_trading.v2.validation.forward_paper import SCHEMA, NS, MINUTE, ForwardPaperError, ForwardPaperHost, content, preflight
from ak_trading.v2.validation.forward_paper_feed import (
    KLINE_FIELDS, MEMORY_LIMIT, OUTPUT_LIMIT, ResourceGuard, TqReadOnlyFeed, clock_from_binding,
    quote_snapshot, serial_snapshot, validate_runtime_binding,
)
from test_v2_forward_paper import BASE, FakeClock, HASH

CONFIG = Path("config/research-v2-forward-paper-rb-minute.json")


def row(identity=100, **changes):
    return {"id": identity, "datetime": BASE + identity * MINUTE, "open": 110., "high": 111.,
            "low": 109., "close": 110., "volume": 1000, "open_oi": 100, "close_oi": 100, **changes}


def quote(**changes):
    return {"underlying_symbol": "SHFE.rb2605", "ins_class": "FUTURE", "expired": False,
        "price_tick": .1, "volume_multiple": 10, "expire_datetime": 1780000000,
        "datetime": "2026-01-12 09:00:00", "last_price": 110., "ask_price1": 110.1,
        "bid_price1": 109.9, "volume": 100, "open_interest": 100, **changes}


class FakeSDK:
    """Any unlisted SDK/DB/order/account/service access is an immediate trap."""
    def __init__(self, steps=(), *, close_fail=False):
        self.main, self.concrete = quote(), quote()
        self.minute, self.daily = [row(99), row()], [row(1)]
        self.steps, self.calls = list(steps), []
        self.closed, self.close_fail = False, close_fail

    def __getattr__(self, name):
        raise AssertionError("FORBIDDEN_SURFACE:" + name)

    def get_quote(self, symbol):
        self.calls.append(("quote", symbol))
        return self.main if symbol == "KQ.m@SHFE.rb" else self.concrete

    def get_kline_serial(self, symbol, duration_seconds, *, data_length):
        self.calls.append(("serial", symbol, duration_seconds, data_length))
        return self.minute if duration_seconds == 60 else self.daily

    def wait_update(self, *, deadline):
        self.calls.append(("wait", deadline))
        if self.steps:
            action = self.steps.pop(0)
            if isinstance(action, BaseException):
                raise action
            if callable(action):
                action(self)
            return action is not False
        return False

    def close(self):
        self.closed = True
        if self.close_fail:
            raise RuntimeError("SYNTHETIC_SECRET_CANARY_DO_NOT_LEAK")


def setup(tmp_path, *, seconds=10, steps=(), close_fail=False):
    clock = FakeClock()
    host = ForwardPaperHost(tmp_path / "run", "feed", clock=clock, synthetic=True)
    ticks = [0]
    def monotonic():
        ticks[0] += NS
        return ticks[0]
    guard = ResourceGuard(host.journal.root, seconds, monotonic=monotonic, memory=lambda: 100)
    api = FakeSDK(steps, close_fail=close_fail)
    return host, clock, api, TqReadOnlyFeed(api, host, guard, wall_clock=lambda: 1000.)


def test_whitelist_copy_and_integer_nanoseconds_have_no_secret_aliases():
    source = quote(secret="SYNTHETIC_SECRET_CANARY_DO_NOT_LEAK")
    frozen = quote_snapshot(source)
    source["price_tick"] = 100
    assert frozen["price_tick"] == "0.1" and "secret" not in frozen
    import pandas as pd
    frame = pd.DataFrame([row()])
    copied = serial_snapshot(frame)
    frame.loc[0, "high"] = 999999
    assert copied[0]["datetime"] == BASE + 100 * MINUTE
    assert copied[0]["high"] == "111.0"


def sdk_float_frame(rows):
    # SYNTHETIC: match TqSdk3.10.2 _init_serial's single FloatBlock columns.
    import numpy as np
    import pandas as pd
    return pd.DataFrame(np.array([[r[name] for name in KLINE_FIELDS] for r in rows],
                                 dtype=np.float64), columns=KLINE_FIELDS)


def test_sdk_float_serial_identity_preserves_value_and_skips_empty_rows():
    exact_nano = BASE + 100 * MINUTE + 256
    frame = sdk_float_frame([row(-1, datetime=0), row(100, datetime=exact_nano)])
    assert all(dtype.kind == "f" for dtype in frame.dtypes)
    copied = serial_snapshot(frame)
    assert len(copied) == 1
    assert copied[0]["datetime"] == exact_nano and copied[0]["id"] == 100
    assert type(copied[0]["datetime"]) is int and type(copied[0]["id"]) is int
    frame.loc[1, "datetime"] = 0
    assert copied[0]["datetime"] == exact_nano


def test_sdk_serial_int64_identity_preserves_low_bits():
    import numpy as np
    import pandas as pd
    exact_nano, exact_id = BASE + 100 * MINUTE + 1, 2 ** 53 + 1
    frame = pd.DataFrame([row(exact_id, datetime=exact_nano)])
    frame["datetime"], frame["id"] = (frame[name].astype(np.int64) for name in ("datetime", "id"))
    copied = serial_snapshot(frame)
    assert copied[0]["datetime"] == exact_nano and copied[0]["id"] == exact_id


@pytest.mark.parametrize("field", ["datetime", "id"])
@pytest.mark.parametrize("value", [1.5, True, "1.5", "NaN", "Infinity"])
def test_sdk_serial_identity_rejects_non_integral_or_invalid_values(field, value):
    with pytest.raises(ForwardPaperError, match="SDK_SERIAL_TIME_INVALID"):
        serial_snapshot([row(**{field: value})])


@pytest.mark.parametrize("field", ["datetime", "id"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_sdk_non_finite_serial_rows_never_become_source_bars(field, value):
    assert serial_snapshot([row(**{field: value})]) == ()


def test_single_connection_four_subscriptions_bootstrap_then_real_progression(tmp_path):
    host, clock, api, feed = setup(tmp_path)
    feed.main = api.get_quote("KQ.m@SHFE.rb")
    feed.subscriptions = 1
    feed.snapshot()  # Concrete discovery/subscriptions.
    feed.snapshot()  # Initial history is bootstrap, never OPEN/CLOSED.
    assert host.forward_bar_count == 0 and not host.open_sources
    api.minute[-1].update(high=999999., low=.1, close=1234., volume=999999999)
    feed.snapshot()
    assert not host.open_sources and host.forward_bar_count == 0
    # The same update supplies completed old row and genuinely new OPEN.
    api.minute[-1].update(high=111., low=109., close=110., volume=1000)
    api.minute.append(row(101))
    clock.at(BASE + 101 * MINUTE + NS)
    feed.snapshot()
    assert host.forward_bar_count == 1 and feed.subscriptions == 4
    assert api.calls[:4] == [("quote", "KQ.m@SHFE.rb"), ("quote", "SHFE.rb2605"),
        ("serial", "SHFE.rb2605", 60, 8192), ("serial", "SHFE.rb2605", 86400, 512)]
    opening = next(iter(host.open_sources.values()))["event"]["data"]
    assert not {"high", "low", "close", "volume"} & set(opening)
    prior_sequence = host.source_watermark
    feed.snapshot()
    assert host.source_watermark == prior_sequence


def test_gap_rows_are_history_and_mapping_change_stops_without_roll(tmp_path):
    host, clock, api, feed = setup(tmp_path)
    feed.main = api.main
    feed.snapshot()
    feed.snapshot()
    api.minute.extend([row(101), row(102)])
    clock.at(BASE + 102 * MINUTE + NS)
    feed.snapshot()
    assert host.forward_bar_count == 0
    assert all(v["event"]["data"]["historical"] for v in host.open_sources.values())
    assert not host.connected
    api.main["underlying_symbol"] = "SHFE.rb2610"
    with pytest.raises(ForwardPaperError, match="MAIN_MAPPING_CHANGED"):
        feed.snapshot()
    assert feed.symbol == "SHFE.rb2605" and host.conflicted
    assert len([c for c in api.calls if c[0] in ("quote", "serial")]) == 3


def test_closed_correction_and_expired_contract_are_rejected(tmp_path):
    host, _, api, feed = setup(tmp_path)
    feed.main = api.main
    feed.snapshot()
    feed.snapshot()
    api.minute[0]["close"] = 109
    with pytest.raises(ForwardPaperError, match="CLOSED_CORRECTION"):
        feed.snapshot()
    assert host.conflicted
    api.minute[0]["close"] = 110.
    api.concrete["expired"] = True
    with pytest.raises(ForwardPaperError, match="CONCRETE_CONTRACT"):
        feed.snapshot()


@pytest.mark.parametrize("memory,output,elapsed,reason", [
    (MEMORY_LIMIT + 1, 0, 0, "MEMORY_BUDGET"), (100, OUTPUT_LIMIT + 1, 0, "OUTPUT_BUDGET"),
    (MEMORY_LIMIT, OUTPUT_LIMIT, 600 * NS, "WALL_BUDGET"), (MEMORY_LIMIT, OUTPUT_LIMIT, 1, None)])
def test_resource_budget_boundaries(tmp_path, memory, output, elapsed, reason):
    current = [0]
    guard = ResourceGuard(tmp_path, 600, monotonic=lambda: current[0], memory=lambda: memory, size=lambda: output)
    current[0] = elapsed
    assert guard.check() == reason
    with pytest.raises(ForwardPaperError, match="WALL_BUDGET"):
        ResourceGuard(tmp_path, 601)
    with pytest.raises(ForwardPaperError, match="WALL_BUDGET"):
        ResourceGuard(tmp_path, 3601, first=False)


def test_wait_timeout_terminates_bounded_no_fake_open_and_closes_api(tmp_path):
    host, _, api, feed = setup(tmp_path, seconds=8, steps=[True, True, False, False])
    native, reason = feed.run()
    assert native == 0 and reason == "WALL_BUDGET" and api.closed
    assert all(call[1] == 1001. for call in api.calls if call[0] == "wait")
    assert host.forward_bar_count == 0 and not host.open_sources
    assert host.report()["capture_status"] == "NO_FORWARD_BAR"
    assert host.report()["strategy_evaluation"] == "NOT_RUN"


@pytest.mark.parametrize("steps", [(), (True,)], ids=["no-update", "discovery-only-update"])
@pytest.mark.parametrize("sdk_serial_format", ["integer_columns", "sdk_float_block"])
def test_ready_initial_data_bootstraps_without_further_update(tmp_path, steps, sdk_serial_format):
    # SYNTHETIC getters already hold valid data; subsequent pumps all time out.
    host, _, api, feed = setup(tmp_path, seconds=8, steps=steps)
    if sdk_serial_format == "sdk_float_block":
        api.minute, api.daily = sdk_float_frame(api.minute), sdk_float_frame(api.daily)
    native, reason = feed.run()
    assert native == 0 and reason == "WALL_BUDGET" and api.closed
    assert feed.runtime_result["terminal_persistence"] == "SEALED"
    assert [call for call in api.calls if call[0] in ("quote", "serial")] == [
        ("quote", "KQ.m@SHFE.rb"), ("quote", "SHFE.rb2605"),
        ("serial", "SHFE.rb2605", 60, 8192), ("serial", "SHFE.rb2605", 86400, 512)]
    bootstraps = [fact for fact in host.facts if fact["kind"] == "BOOTSTRAP"]
    assert len(bootstraps) == 1 and bootstraps[0]["forward_credit"] is False
    assert host.forward_bar_count == 0 and not host.open_sources
    assert host.report()["capture_status"] == "NO_FORWARD_BAR"
    assert host.report()["strategy_evaluation"] == "NOT_RUN"


@pytest.mark.parametrize("failure,close_failure,native", [(RuntimeError("SYNTHETIC_SECRET_CANARY_DO_NOT_LEAK"), False, 2),
    (KeyboardInterrupt(), False, 130), (RuntimeError("SYNTHETIC_SECRET_CANARY_DO_NOT_LEAK"), True, 3)])
def test_interrupt_pump_exception_close_failure_separate_native_facts_no_secret(tmp_path, failure, close_failure, native):
    host, _, api, feed = setup(tmp_path, steps=[failure], close_fail=close_failure)
    result, _ = feed.run()
    assert result == native and api.closed
    facts = [f for f in host.facts if f["kind"] == "RUNTIME_RESULT"]
    assert facts[-1]["native_result"] == native
    assert facts[-1]["close_status"] == ("SDK_CLOSE_FAILED" if close_failure else "CLOSED")
    for path in host.journal.root.rglob("*"):
        if path.is_file():
            assert b"SYNTHETIC_SECRET_CANARY_DO_NOT_LEAK" not in path.read_bytes()


@pytest.mark.parametrize("failure,expected", [(None, 4), (KeyboardInterrupt(), 130),
                                              (RuntimeError("SYNTHETIC_SECRET_CANARY_DO_NOT_LEAK"), 2)])
def test_terminal_budget_failure_preserves_pump_and_close_outcomes(tmp_path, failure, expected):
    host, _, api, feed = setup(tmp_path, seconds=1 if failure is None else 10,
                               steps=() if failure is None else [failure])
    # Only clocks/SDK are controlled; a real journal write fails its hard bound.
    used = sum(p.stat().st_size for p in host.journal.root.rglob("*") if p.is_file())
    host.journal.output_limit_bytes = used
    native, _ = feed.run()
    assert native == expected and api.closed
    assert feed.runtime_result["pump_native_result"] == (0 if failure is None else expected)
    assert feed.runtime_result["close_status"] == "CLOSED"
    assert feed.runtime_result["terminal_persistence"] == "FAILED"
    assert feed.runtime_result["terminal_persistence_reason"] == "OUTPUT_BUDGET"
    assert host.journal.sequence == 1


def test_preflight_missing_pack_does_not_claim_strategy_and_offline_cli_never_sdk(tmp_path):
    config = parse_json_bytes(CONFIG.read_bytes())
    binding = {"schema_version": SCHEMA, "run_id": "offline"}
    result = preflight(config, binding)
    assert result["status"] == "WAITING_INPUT" and result["sdk_constructed"] == 0
    assert result["strategy_evaluation"] == "NOT_RUN" and result["paper_account"] == "NOT_ACTIVATED"
    assert "SDK_RUNTIME_NOT_AUTHORIZED" in result["blockers"]
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(binding), encoding="utf-8")
    command = [sys.executable, "scripts/run_forward_paper.py", "preflight", "--binding", str(path)]
    run = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert run.returncode == 2, run.stderr
    assert json.loads(run.stdout)["sdk_constructed"] == 0
    blocked = subprocess.run([sys.executable, "scripts/run_forward_paper.py", "run", "--binding", str(path)],
                             capture_output=True, text=True, timeout=30)
    assert blocked.returncode == 2 and json.loads(blocked.stdout)["status"] == "PREFLIGHT_BLOCKED"
    with pytest.raises(ForwardPaperError, match="PREFLIGHT_BLOCKED"):
        validate_runtime_binding(config, binding, activate=True)


def test_no_independent_clock_is_unknown_not_local_utc_self_certification():
    clock = clock_from_binding({"run_id": "u"}, utc=lambda: BASE, monotonic=lambda: BASE)
    assert "CLOCK_UNVERIFIED" in clock().reasons()


def test_bad_cli_arguments_and_missing_required_input_are_native_failures():
    for args in (["wrong-mode"], ["preflight", "--binding", "required-absent-binding.json"]):
        run = subprocess.run([sys.executable, "scripts/run_forward_paper.py", *args], capture_output=True,
                             text=True, timeout=30)
        assert run.returncode == 2


def synthetic_runtime_binding(tmp_path, config):
    """SYNTHETIC authorization documents exercise only offline preconditions."""
    root = Path.cwd().resolve()
    names = {"src/ak_trading/v2/validation/forward_paper.py",
             "src/ak_trading/v2/validation/forward_paper_feed.py", "scripts/run_forward_paper.py",
             "src/ak_trading/session.py", "src/ak_trading/config.py"}
    for name, module in tuple(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if name.startswith("ak_trading") and path is not None:
            path = Path(path).resolve()
            if path.suffix == ".py" and path.is_relative_to(root):
                names.add(path.relative_to(root).as_posix())
    files = {name: {"bytes": len(raw), "sha256": sha256_hex(raw)}
             for name in names for raw in [(root / name).read_bytes()]}
    closure = {"root": str(root), "files": files, "input_kind": "SYNTHETIC_PRECONDITION_CONTROL"}
    def put(name, value):
        path = tmp_path / name
        raw = canonical_json_bytes(value)
        path.write_bytes(raw)
        return {"path": str(path), "bytes": len(raw), "sha256": sha256_hex(raw)}
    closure_ref = put("closure.json", closure)
    auth = {"schema_version": SCHEMA, "kind": "ROOT_RUNTIME_AUTHORIZATION", "issuer": "/root",
            "authorized": True, "run_id": "synthetic-precondition", "action": "READ_ONLY_TQSDK",
            "runtime_closure_sha256": closure_ref["sha256"], "config_sha256": content(config),
            "enable_trading": False, "observer": "hold", "broker": "NOT_RUN",
            "input_kind": "SYNTHETIC_PRECONDITION_CONTROL"}
    return {"schema_version": SCHEMA, "run_id": auth["run_id"], "runtime_closure": closure_ref,
            "runtime_authorization": put("authorization.json", auth)}, put


@pytest.mark.parametrize("change", ["issuer", "action", "run_id", "closure_hash", "config_hash", "trading"])
def test_hash_valid_wrong_runtime_authority_never_makes_preflight_ready(tmp_path, change):
    config = parse_json_bytes(CONFIG.read_bytes())
    binding, put = synthetic_runtime_binding(tmp_path, config)
    good = preflight(config, binding)
    assert good["capture_preflight_ok"] and not good["activation_preflight_ok"]
    auth = parse_json_bytes(Path(binding["runtime_authorization"]["path"]).read_bytes())
    key, value = {"issuer": ("issuer", "SYNTHETIC_OTHER_OWNER"), "action": ("action", "ORDERS"),
                  "run_id": ("run_id", "OTHER_RUN"), "closure_hash": ("runtime_closure_sha256", "1" * 64),
                  "config_hash": ("config_sha256", "2" * 64), "trading": ("enable_trading", True)}[change]
    auth[key] = value
    binding["runtime_authorization"] = put("bad-authorization.json", auth)
    result = preflight(config, binding)
    assert not result["requested_mode_ready"] and not result["capture_preflight_ok"]
    assert "SDK_RUNTIME_AUTHORIZATION_MISMATCH" in result["blockers"]


@pytest.mark.parametrize("change", ["root", "missing_import", "member_hash", "escape"])
def test_actual_runtime_closure_is_complete_and_hash_checked_before_sdk(tmp_path, change):
    config = parse_json_bytes(CONFIG.read_bytes())
    binding, put = synthetic_runtime_binding(tmp_path, config)
    closure = parse_json_bytes(Path(binding["runtime_closure"]["path"]).read_bytes())
    if change == "root":
        closure["root"] = str(tmp_path)
    elif change == "missing_import":
        del closure["files"]["src/ak_trading/v2/validation/forward_paper.py"]
    elif change == "member_hash":
        closure["files"]["src/ak_trading/v2/validation/forward_paper.py"]["sha256"] = "1" * 64
    else:
        closure["files"]["../SYNTHETIC-ESCAPE.py"] = {"bytes": 1, "sha256": "1" * 64}
    binding["runtime_closure"] = put("bad-closure.json", closure)
    auth = parse_json_bytes(Path(binding["runtime_authorization"]["path"]).read_bytes())
    auth["runtime_closure_sha256"] = binding["runtime_closure"]["sha256"]
    binding["runtime_authorization"] = put("bad-closure-authorization.json", auth)
    result = preflight(config, binding)
    assert not result["requested_mode_ready"] and result["sdk_constructed"] == 0


@pytest.mark.parametrize("value", ["NaN", "-1", 1000000, True])
def test_invalid_initial_equity_is_an_offline_blocker(value):
    config = parse_json_bytes(CONFIG.read_bytes())
    config["initial_equity_cny"] = value
    result = preflight(config, {"schema_version": SCHEMA, "run_id": "synthetic"})
    assert "CONFIG_CONTRACT_MISMATCH" in result["blockers"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job supervision is the selected platform")
@pytest.mark.parametrize("phase", ["constructor", "subscription", "close", "memory"])
def test_real_job_supervisor_stops_blocked_fake_sdk_and_preserves_last_seal(tmp_path, phase):
    """SYNTHETIC child; preserve raw outcomes and the original outer budget.

    The memory mechanism control changes only a smaller inner Job cap, with
    identical bounded allocations. It is not a new 2GiB boundary measurement.
    The entire existing pytest population retains the exact outer 2GiB limit.
    """
    import ctypes
    from ctypes import wintypes as w
    import runpy
    import time
    import psutil
    root = Path(__file__).resolve().parents[1]
    supervisor = root / "docs/strategy-first-blueprint-20261015-r1/implementation-clarifications/forward-paper-start-20261015-r1/owner-evidence/r1-001/verify_job_004.py"
    assert supervisor.is_file()
    namespace = runpy.run_path(str(supervisor))
    ExtendedLimits = namespace["ExtendedLimits"]
    worker = r'''
import ctypes,json,sys,threading,time
from datetime import datetime,timezone
from pathlib import Path
from ak_trading.v2.validation.forward_paper import ClockSample,ForwardPaperHost,NS
root,phase=Path(sys.argv[1]),sys.argv[2]
marker=root/'phase.json'
# Exact inline replicas of the test module's synthetic constants and clock.
# Importing the whole test module here recompiled its full dependency chain
# inside the supervised child and repeatedly consumed the pinned cutoffs.
BASE=int(datetime(2026,1,12,tzinfo=timezone.utc).timestamp())*NS
HASH=__import__('hashlib').sha256(b'SYNTHETIC_FORWARD_PAPER_CONTROL').hexdigest()
_when=BASE+100*60*NS+NS
def FakeClock():
    return ClockSample(_when,_when,HASH,0,0,_when,'SYNTHETIC_INDEPENDENT_CLOCK')
def publish(value):
    raw=json.dumps(value).encode('utf-8')
    temporary=marker.with_suffix('.pending')
    temporary.write_bytes(raw)
    temporary.replace(marker)

def wait(name):
    while not (root/name).is_file(): time.sleep(.01)

def process_commit():
    import psutil
    info=psutil.Process().memory_info()
    return {'private_bytes':info.private,'pagefile_bytes':info.pagefile,
            'source':'psutil Windows memory_info; PrivateUsage and PagefileUsage, not aggregate Job commit'}

def block(stage):
    publish({'phase':stage,'sdk_order_accesses':0,'close_status':'INCOMPLETE' if stage=='close' else 'NOT_RUN'})
    threading.Event().wait()
host=ForwardPaperHost(root/'run','supervised',clock=FakeClock,synthetic=True)
if phase=='memory':
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.VirtualAlloc.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_ulong,ctypes.c_ulong]
    kernel.VirtualAlloc.restype=ctypes.c_void_p
    kernel.VirtualFree.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_ulong]
    kernel.VirtualFree.restype=ctypes.c_int
    facts={'phase':'BEFORE_ALLOCATION','sdk_order_accesses':0,'close_status':'NOT_RUN',
           'requested_bytes':192*1024*1024,'before_process_commit':process_commit(),
           'current_aggregate_job_commit_bytes':None,'current_aggregate_status':'UNKNOWN',
           'system_commit_headroom_bytes':None,'system_commit_headroom_status':'NOT_OBSERVED'}
    publish(facts)
    wait('allocate.signal')
    ctypes.set_last_error(0)
    allocation=kernel.VirtualAlloc(None,facts['requested_bytes'],0x3000,0x04)
    native_error=ctypes.get_last_error()
    facts.update(phase='ALLOCATION_COMPLETE',allocation='ACCEPTED' if allocation else 'KERNEL_REJECTED',
                 native_allocation_error=native_error,after_process_commit=process_commit())
    publish(facts)
    wait('release.signal')
    if allocation:
        ctypes.set_last_error(0)
        freed=bool(kernel.VirtualFree(allocation,0,0x8000))
        free_error=ctypes.get_last_error()
    else:
        freed,free_error=None,None
    facts.update(phase='RELEASED',allocation_freed=freed,native_free_error=free_error,
                 released_process_commit=process_commit())
    publish(facts)
    wait('exit.signal')
    print(json.dumps(facts),flush=True)
    raise SystemExit(0 if allocation and freed else 77 if not allocation else 78)
from ak_trading.v2.validation.forward_paper_feed import ResourceGuard,TqReadOnlyFeed
class Fake:
    def __init__(self):
        if phase=='constructor': block('constructor')
    def get_quote(self,symbol):
        if phase=='subscription': block('subscription')
        return {}
    def close(self):
        if phase=='close': block('close')
    def __getattr__(self,name):
        raise AssertionError('SYNTHETIC_FORBIDDEN_SDK_ACCESS')
ticks=[0]
def monotonic():
    ticks[0]+=NS
    return ticks[0]
feed=TqReadOnlyFeed(Fake(),host,ResourceGuard(root/'run',1,monotonic=monotonic,memory=lambda:1))
feed.run()
raise SystemExit(79)
'''
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.IsProcessInJob.argtypes = [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)]
    kernel.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p]
    kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    controls = (("reject", 128 * 1024 * 1024), ("accept_release", 512 * 1024 * 1024)) if phase == "memory" else ((phase, MEMORY_LIMIT),)
    for label, inner_cap in controls:
        directory = tmp_path / label
        directory.mkdir()
        job = kernel.CreateJobObjectW(None, None)
        assert job, ctypes.get_last_error()
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000 | 0x00000200
        limits.JobMemoryLimit = inner_cap
        assert kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
        child, saved = None, None
        started = time.monotonic()
        forced = False
        samples = {}
        try:
            with (directory / "child.stdout").open("xb") as stdout, (directory / "child.stderr").open("xb") as stderr:
                child = subprocess.Popen([sys.executable, "-c", worker, str(directory), phase], cwd=root,
                    stdout=stdout, stderr=stderr, creationflags=0x00000004)
                created = psutil.Process(child.pid).create_time()
                assert kernel.AssignProcessToJobObject(job, w.HANDLE(int(child._handle)))
                membership = w.BOOL()
                assert kernel.IsProcessInJob(w.HANDLE(int(child._handle)), job, ctypes.byref(membership)) and membership.value
                actual = ExtendedLimits()
                assert kernel.QueryInformationJobObject(job, 9, ctypes.byref(actual), ctypes.sizeof(actual), None)
                assert actual.JobMemoryLimit == inner_cap and actual.BasicLimitInformation.LimitFlags == limits.BasicLimitInformation.LimitFlags
                resume = ctypes.WinDLL("ntdll").NtResumeProcess
                resume.argtypes, resume.restype = [w.HANDLE], ctypes.c_long
                assert resume(w.HANDLE(int(child._handle))) == 0
                while child.poll() is None:
                    marker_path = directory / "phase.json"
                    if marker_path.is_file():
                        marker = json.loads(marker_path.read_bytes())
                        if saved is None:
                            saved = {str(p.relative_to(directory / "run")): sha256_hex(p.read_bytes())
                                     for p in (directory / "run").rglob("*") if p.is_file()}
                        stage = marker["phase"]
                        if phase == "memory" and stage not in samples:
                            observed = ExtendedLimits()
                            assert kernel.QueryInformationJobObject(job, 9, ctypes.byref(observed), ctypes.sizeof(observed), None)
                            process = psutil.Process(child.pid)
                            assert process.create_time() == created and child.poll() is None
                            samples[stage] = {"worker_alive": True, "raw_job_peak_bytes": observed.PeakJobMemoryUsed,
                                "private_process_commit_bytes": process.memory_info().private,
                                "current_aggregate_job_commit_bytes": None, "current_aggregate_status": "UNKNOWN"}
                            signal = {"BEFORE_ALLOCATION": "allocate.signal", "ALLOCATION_COMPLETE": "release.signal", "RELEASED": "exit.signal"}[stage]
                            (directory / signal).write_bytes(b"CONTINUE\n")
                    if time.monotonic() - started >= 10:
                        assert kernel.TerminateJobObject(job, 124)
                        forced = True
                        break
                    time.sleep(.05)
                native = child.wait(timeout=5)
            terminal = ExtendedLimits()
            assert kernel.QueryInformationJobObject(job, 9, ctypes.byref(terminal), ctypes.sizeof(terminal), None)
            elapsed = time.monotonic() - started
        finally:
            if child is not None and child.poll() is None:
                kernel.TerminateJobObject(job, 125)
                child.wait(timeout=5)
            kernel.CloseHandle(job)
        assert child is not None and child.poll() is not None
        if psutil.pid_exists(child.pid):
            assert psutil.Process(child.pid).create_time() != created
        if phase == "memory":
            assert native == (77 if label == "reject" else 0) and not forced
            assert set(samples) == {"BEFORE_ALLOCATION", "ALLOCATION_COMPLETE", "RELEASED"}
            allocation = json.loads((directory / "child.stdout").read_text())
            assert allocation["allocation"] == ("KERNEL_REJECTED" if label == "reject" else "ACCEPTED")
            assert allocation["requested_bytes"] == 192 * 1024 * 1024
            if label == "reject":
                assert allocation["native_allocation_error"] == 1455 and allocation["allocation_freed"] is None
            else:
                assert allocation["native_allocation_error"] == 0 and allocation["allocation_freed"] is True
                assert allocation["native_free_error"] == 0
        else:
            assert native == 124 and forced and saved is not None
        marker = json.loads((directory / "phase.json").read_bytes())
        assert marker["sdk_order_accesses"] == 0
        assert marker["close_status"] == ("INCOMPLETE" if phase == "close" else "NOT_RUN")
        after = {str(p.relative_to(directory / "run")): sha256_hex(p.read_bytes())
                 for p in (directory / "run").rglob("*") if p.is_file()}
        assert saved is not None and after == saved
        assert (directory / "run/segments/00000001/manifest.json").is_file()
        assert not list((directory / "run/reports").glob("r*"))
        # The inner rejected-commit peak is recorded honestly. Its smaller cap
        # is a mechanism control, not the original 2GiB outer runtime budget.
        assert terminal.PeakJobMemoryUsed <= MEMORY_LIMIT
        evidence = {"phase": phase, "control": label, "raw_native_exit": native, "forced": forced,
            "membership_proved_before_resume": bool(membership.value), "job_limit_bytes": actual.JobMemoryLimit,
            "job_limit_role": "SYNTHETIC_INNER_MECHANISM_CONTROL" if phase == "memory" else "ORIGINAL_RUNTIME_LIMIT",
            "original_outer_memory_limit_bytes": MEMORY_LIMIT, "aggregate_peak_bytes": terminal.PeakJobMemoryUsed,
            "configured_cutoff_seconds": 10, "observed_elapsed_seconds": elapsed, "pid": child.pid,
            "creation_time": created, "owned_process_terminated": True, "close_status": marker["close_status"],
            "checkpoint": "START_ONLY_BYTE_IDENTICAL", "terminal_report": "NOT_GENERATED",
            "live_process_samples": samples, "allocation": marker if phase == "memory" else None,
            "supervisor_sha256": sha256_hex(supervisor.read_bytes()), "pointer_bytes": ctypes.sizeof(ctypes.c_size_t),
            "extended_limits_bytes": ctypes.sizeof(ExtendedLimits), "abi_source": "pinned existing management run_bounded.py",
            "real_send_count": 0, "real_cancel_count": 0}
        (directory / "SUPERVISION.json").write_bytes(canonical_json_bytes(evidence))
        print(json.dumps(evidence, sort_keys=True))
