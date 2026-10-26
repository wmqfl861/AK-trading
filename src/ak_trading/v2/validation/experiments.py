"""Pre-registration, durable once-only holdouts, and offline experiment orchestration."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from bisect import bisect_left
from dataclasses import dataclass, fields, is_dataclass
from datetime import date
from decimal import Decimal
import hashlib
import os
from pathlib import Path
import secrets
import time
from typing import Literal, Mapping, TYPE_CHECKING

from ak_trading.v2.contracts import (
    PAYOFF_PATH_ACCOUNT_ID_PREFIX, PAYOFF_TRAINING_PLAN_PROTOCOL_VERSION,
    PAYOFF_TRAINING_PLAN_SELECTION_RULE, FrozenInput, JsonValue,
    PayoffTrainingPlan, RunPolicy, SessionWindow, payoff_training_plan_sha256,
)
from ak_trading.v2.schemas import (
    PAYOFF_PATH_SCHEMA_ID, PAYOFF_TRAINING_PLAN_SCHEMA_ID,
    canonical_json_bytes, encode_record, encode_record_file, parse_json_bytes,
)
from .splits import SampleInterval, SplitSpec, assign_splits, calendar_days

if TYPE_CHECKING:
    from ak_trading.v2.contracts import LedgerCheckpoint

COST_SCENARIOS = ("BASE", "FEE_X2", "SLIPPAGE_X2", "MARGIN_X1_5", "GAP", "NO_STOP")
PROJECT_ROOT = Path(__file__).resolve().parents[4]
RUN_ROOT = PROJECT_ROOT / "data/research_runs/v2"
ZERO_HASH = "0" * 64

# Frozen sample-unavailability reason prefix (emergency arbitration
# 2026-09-17 §3.4). Only a ValueError from the payoff producer whose message
# carries exactly the producer-frozen ``PAYOFF_SAMPLE_UNAVAILABLE:`` prefix
# may become a per-window training exclusion (original scope retained, N=0 /
# INSUFFICIENT); the producer's ``PAYOFF_INTEGRITY:`` and
# ``PAYOFF_INTEGRITY_PROGRAM:`` namespaces — identity tampering, ledger
# non-conservation, broken decision/intent/receipt/fill chains, program
# interface misuse — plus every other error, including TypeError/ImportError,
# keep propagating and block the run. The prefix constant is imported from
# the producer module so M5 can never drift from the M4 freeze (E1); the
# frozen suffix lists are registered in
# data/maintenance_artifacts/v2-emergency-e1-m4-20260917/implementation.md
# and re-listed in v2-emergency-e2-m5-20260917/implementation.md.
from ak_trading.v2.decision.payoffs import PAYOFF_SAMPLE_UNAVAILABLE_PREFIX
PAYOFF_EXCLUSION_REASON_PREFIXES = (PAYOFF_SAMPLE_UNAVAILABLE_PREFIX,)


def payoff_producer_excludable_codes() -> frozenset[str]:
    """Complete full-code exclusion set imported from the producer module.

    E-P1 RULING 3.4/4.1: M5 matches producer refusals by the COMPLETE frozen
    code, never by prefix. An unknown suffix that merely shares the
    ``PAYOFF_SAMPLE_UNAVAILABLE:`` prefix does NOT become a legal exclusion
    and keeps blocking the run. The frozen suffix registry lives in the
    producer module (E1 freeze; EP1-M4 extends it for the measurement path);
    the public names are preferred and the E1 private registry is read
    directly so the two can never drift apart on this tree.
    """
    from ak_trading.v2.decision import payoffs
    for public in ("PAYOFF_SAMPLE_UNAVAILABLE_REASONS", "PAYOFF_EXCLUDABLE_REASONS"):
        reasons = getattr(payoffs, public, None)
        if reasons is not None:
            return frozenset(PAYOFF_SAMPLE_UNAVAILABLE_PREFIX + code for code in reasons)
    e1_registry = getattr(payoffs, "_REASONS_UNAVAILABLE", ())
    return frozenset(PAYOFF_SAMPLE_UNAVAILABLE_PREFIX + code for code in e1_registry)


def _payoff_excludable(error: ValueError) -> bool:
    """True only when the producer refusal carries a complete frozen code."""
    return str(error) in payoff_producer_excludable_codes()


# Producer symbols probed before any expensive fitting. A missing public
# producer must fail the run as an engineering error ahead of ``_fit_fold``
# on both the smoke and shadow-replay entries (arbitration §3.1); merely
# moving imports earlier does not satisfy the check. EP1-M5 extends the
# probes to the E-P1 §2.1 measurement interface and the §2.4 source-bytes
# producer so an unmerged EP1-M4 is reported as an engineering dependency
# failure before any costly work or registration.
PAYOFF_TRAINING_DEPENDENCY_PROBES = (
    ("ak_trading.v2.decision.payoffs", "observation_from_replay"),
    ("ak_trading.v2.decision.payoffs", "payoff_observation_source"),
    ("ak_trading.v2.decision.payoffs", "fit_path_payoff_model"),
    ("ak_trading.v2.decision.payoff_paths", "replay_payoff_training_window"),
    ("ak_trading.v2.decision.replay", "run_decision_replay"),
    ("ak_trading.v2.decision.replay", "replay_payloads"),
    ("ak_trading.v2.decision.pipeline", "build_strategy_context"),
    ("ak_trading.v2.strategies.registry", "select_exit_binding"),
    ("ak_trading.v2.strategies.registry", "strategy_specs_v2"),
)


class ExperimentError(ValueError):
    pass


class HoldoutConsumedError(ExperimentError):
    pass


class StageIncompleteError(RuntimeError):
    pass


def content(value: object) -> JsonValue:
    """Encode declared local records without mutating/augmenting upstream records."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: content(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {k: content(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [content(v) for v in value]
    if isinstance(value, (Decimal, date)):
        return str(value)
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise TypeError(f"unsupported_content_type:{type(value).__name__}")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(content(value))).hexdigest()


def require_hash(value: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ExperimentError("invalid_sha256")


def require_id(value: str) -> None:
    if not isinstance(value, str) or not value or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in value):
        raise ExperimentError("unsafe_identifier")


@dataclass(frozen=True)
class HoldoutScope:
    market_family: Literal["CN_FUTURES_V2"]
    products: tuple[str, ...]
    contract_ids: tuple[str, ...]
    decision_start_nano: int
    decision_end_nano: int
    maturity_end_nano: int
    calendar_sha256: str

    def __post_init__(self) -> None:
        if self.market_family != "CN_FUTURES_V2":
            raise ExperimentError("invalid_market_family")
        for v in (self.products, self.contract_ids):
            if not isinstance(v, tuple) or not v or len(set(v)) != len(v) or any(not isinstance(x, str) or not x for x in v):
                raise ExperimentError("invalid_scope_members")
        if any(type(v) is not int for v in (self.decision_start_nano, self.decision_end_nano, self.maturity_end_nano)):
            raise ExperimentError("scope_times_must_be_int")
        if not self.decision_start_nano < self.decision_end_nano < self.maturity_end_nano:
            raise ExperimentError("invalid_scope_bounds")
        require_hash(self.calendar_sha256)


@dataclass(frozen=True)
class ExperimentSpec:
    experiment_id: str
    run_id: str
    input_manifest_hashes: tuple[str, ...]
    contract_lock_sha256: str
    exit_registry_sha256: str
    exit_bindings: tuple
    source_sha256: str
    parameter_sha256: str
    split_specs: tuple[SplitSpec, ...]
    holdout_id: str
    holdout_scope: HoldoutScope
    run_policies: tuple[RunPolicy, ...]
    required_group_keys: tuple[str, ...]
    seed: int
    research_policy_version: str
    macro_policy_version: str
    macro_mode: Literal["PRICE_ONLY", "HISTORICAL_PIT", "FORWARD_ONLY"]
    evaluation_as_of_nano: int
    payoff_training_plan_sha256: str

    def __post_init__(self) -> None:
        require_id(self.experiment_id)
        require_id(self.run_id)
        for h in (*self.input_manifest_hashes, self.contract_lock_sha256, self.exit_registry_sha256, self.source_sha256, self.parameter_sha256, self.payoff_training_plan_sha256):
            require_hash(h)
        from ak_trading.v2.contracts import ExitPolicyBinding
        if not isinstance(self.exit_bindings, tuple) or any(not isinstance(b, ExitPolicyBinding) for b in self.exit_bindings):
            raise ExperimentError("exit_bindings_must_be_tuple_of_ExitPolicyBinding")
        hashes=[b.binding_sha256 for b in self.exit_bindings]
        if hashes != sorted(set(hashes)):
            raise ExperimentError("exit_bindings_must_be_sorted_unique")
        strategies={s_.strategy_id for s_ in self.exit_bindings}
        if not strategies or strategies != set(self.run_policies and () or strategies):
            pass  # strategy coverage validated against config.strategy_ids at parse time
        if self.holdout_id != digest(self.holdout_scope):
            raise ExperimentError("holdout_id_must_hash_scope")
        if type(self.seed) is not int or type(self.evaluation_as_of_nano) is not int:
            raise ExperimentError("experiment_times_seed_must_be_int")
        if self.macro_mode not in ("PRICE_ONLY", "HISTORICAL_PIT", "FORWARD_ONLY"):
            raise ExperimentError("invalid_macro_mode")
        if not self.split_specs or len({s.split_id for s in self.split_specs}) != len(self.split_specs):
            raise ExperimentError("split_ids_must_be_unique_nonempty")
        if not self.run_policies or len({digest(p) for p in self.run_policies}) != len(self.run_policies):
            raise ExperimentError("run_policies_must_be_unique_nonempty")
        for p in self.run_policies:
            if (p.seed, p.research_policy_version, p.macro_policy_version) != (self.seed, self.research_policy_version, self.macro_policy_version):
                raise ExperimentError("run_policy_identity_mismatch")
        if len(set(self.required_group_keys)) != len(self.required_group_keys):
            raise ExperimentError("duplicate_required_group")
        for key in self.required_group_keys:
            parsed = parse_json_bytes(key.encode())
            if (not isinstance(parsed, list) or not parsed or parsed[0] not in ("FORECAST", "STRATEGY")
                    or len(parsed) not in ((15,) if parsed[0] == "FORECAST" else (11, 12))
                    or canonical_json_bytes(parsed).decode().strip() != key):
                raise ExperimentError("invalid_required_group_key")


def experiment_bytes(spec: ExperimentSpec) -> bytes:
    # E-P1 RULING 2.2 (M1 allocation): the structurally changed ExperimentSpec
    # (strict payoff_training_plan_sha256 registration field) is serialized
    # under the next real unoccupied lineage version v2.1.3.
    return canonical_json_bytes({"schema_version": "ak_probability_experiment_v2.1.3", "spec": content(spec)})


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.{secrets.token_hex(8)}.tmp")
    with temporary.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _try_lock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def _open_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ExperimentError("symlink_lock_forbidden")
    handle = path.open("a+b")
    if path.stat().st_size == 0:
        handle.write(b" ")
        handle.flush()
    return handle


@contextmanager
def exclusive_lock(path: Path, run_id: str):
    handle = _open_lock(path)
    deadline = time.monotonic() + 5
    while True:
        try:
            _try_lock(handle)
            break
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                raise ExperimentError("registry_busy") from None
            time.sleep(0.01)
    try:
        # The OS lock, not the file's existence or the PID text, proves exclusivity.
        handle.seek(0)
        handle.truncate()
        handle.write(canonical_json_bytes({"run_id": run_id, "pid": os.getpid()}))
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        _unlock(handle)


def read_chain(path: Path) -> list[dict]:
    if not path.exists():
        return []
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise ExperimentError("uncommitted_registry_tail")
    rows, previous = [], ZERO_HASH
    for line in raw.splitlines():
        row = parse_json_bytes(line)
        if not isinstance(row, dict) or row.get("sequence") != len(rows) + 1 or row.get("previous_hash") != previous:
            raise ExperimentError("invalid_registry_chain")
        check = {k: v for k, v in row.items() if k != "row_hash"}
        if row.get("row_hash") != digest(check):
            raise ExperimentError("registry_hash_mismatch")
        rows.append(row)
        previous = row["row_hash"]
    return rows


def append_chain(path: Path, record: dict) -> dict:
    rows = read_chain(path)
    row = {"sequence": len(rows) + 1, **record,
           "previous_hash": rows[-1]["row_hash"] if rows else ZERO_HASH}
    row["row_hash"] = digest(row)
    with path.open("ab") as handle:
        handle.write(canonical_json_bytes(row))
        handle.flush()
        os.fsync(handle.fileno())
    return row


def _check_registration_available(spec: ExperimentSpec, registry_path: Path) -> None:
    """Reject occupied identities without creating a registry or lock file."""
    rows = read_chain(registry_path)
    if any(r["run_id"] == spec.run_id or r["experiment_id"] == spec.experiment_id for r in rows):
        raise ExperimentError("experiment_or_run_already_registered")
    frozen = registry_path.parent / "registered-experiments" / (spec.experiment_id + ".json")
    if frozen.exists():
        raise ExperimentError("registration_incomplete_requires_review")


def register_experiment(spec: ExperimentSpec, registry_path: Path) -> None:
    with exclusive_lock(registry_path.with_name("experiment-registry.lock"), spec.run_id):
        # Repeat the read-only preflight while holding the existing writer lock.
        _check_registration_available(spec, registry_path)
        frozen = registry_path.parent / "registered-experiments" / (spec.experiment_id + ".json")
        atomic_write(frozen, experiment_bytes(spec))
        append_chain(registry_path, {
            "experiment_id": spec.experiment_id, "run_id": spec.run_id, "holdout_id": spec.holdout_id,
            "action": "REGISTER", "as_of_nano": spec.evaluation_as_of_nano,
            "source_sha256": spec.source_sha256, "parameter_sha256": spec.parameter_sha256,
        })


# A lease is held through scoring. It is released by the OS on process death;
# recovery never guesses whether a still-running owner has read labels.
_LEASES: dict[tuple[str, str], object] = {}


def _lease_key(directory: Path, holdout_id: str) -> tuple[str, str]:
    return str(directory.resolve()), holdout_id


def _lease(directory: Path, holdout_id: str) -> None:
    key = _lease_key(directory, holdout_id)
    if key in _LEASES:
        return
    handle = _open_lock(directory / (holdout_id + ".active.lock"))
    try:
        _try_lock(handle)
    except OSError:
        handle.close()
        raise ExperimentError("active_holdout_runner") from None
    _LEASES[key] = handle


def _release(directory: Path, holdout_id: str) -> None:
    handle = _LEASES.pop(_lease_key(directory, holdout_id), None)
    if handle is not None:
        _unlock(handle)


def _scope_overlaps(a: Mapping, b: Mapping) -> bool:
    return a["market_family"] == b["market_family"] and max(a["decision_start_nano"], b["decision_start_nano"]) <= min(a["maturity_end_nano"], b["maturity_end_nano"])


def _holdout_path(directory: Path, spec: ExperimentSpec) -> Path:
    require_hash(spec.holdout_id)
    return directory / (spec.holdout_id + ".json")


def reserve_final_holdout(spec: ExperimentSpec, holdout_directory: Path) -> str:
    with exclusive_lock(holdout_directory / "registry.lock", spec.run_id):
        scope = content(spec.holdout_scope)
        rows = read_chain(holdout_directory / "consumed-intervals.jsonl")
        for row in rows:
            if _scope_overlaps(scope, row["scope"]):
                raise HoldoutConsumedError("global_market_interval_already_observed")
        for path in sorted(holdout_directory.glob("*.json")):
            record = parse_json_bytes(path.read_bytes())
            if _scope_overlaps(scope, record["scope"]):
                raise HoldoutConsumedError("overlapping_holdout_reservation")
        frozen=holdout_directory/"registrations"/(spec.experiment_id+".json")
        if frozen.exists() and frozen.read_bytes()!=experiment_bytes(spec):
            raise ExperimentError("reserved_experiment_changed")
        if not frozen.exists():atomic_write(frozen,experiment_bytes(spec))
        token = secrets.token_urlsafe(32)
        _lease(holdout_directory, spec.holdout_id)
        try:
            atomic_write(_holdout_path(holdout_directory, spec), canonical_json_bytes({
                "holdout_id": spec.holdout_id, "status": "RESERVED", "experiment_id": spec.experiment_id,
                "reservation_token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "started_at_nano": spec.evaluation_as_of_nano, "report_sha256": None,
                "scope": scope, "failure_reason": None,
            }))
        except BaseException:
            _release(holdout_directory, spec.holdout_id)
            raise
        return token


def _record(spec: ExperimentSpec, token: str, directory: Path) -> dict:
    frozen=directory/"registrations"/(spec.experiment_id+".json")
    if not frozen.is_file() or frozen.read_bytes()!=experiment_bytes(spec):
        raise ExperimentError("reserved_experiment_changed")
    record = parse_json_bytes(_holdout_path(directory, spec).read_bytes())
    if (record["experiment_id"] != spec.experiment_id or record["scope"] != content(spec.holdout_scope)
            or record["reservation_token_hash"] != hashlib.sha256(token.encode()).hexdigest()):
        raise ExperimentError("holdout_token_or_identity_mismatch")
    return record


def _consume_interval(directory: Path, record: dict, action: str, as_of: int) -> None:
    path = directory / "consumed-intervals.jsonl"
    rows = read_chain(path)
    if not any(r["holdout_id"] == record["holdout_id"] and r["action"] == action for r in rows):
        append_chain(path, {"market_family": "CN_FUTURES_V2", "scope": record["scope"],
                            "experiment_id": record["experiment_id"], "holdout_id": record["holdout_id"],
                            "action": action, "as_of_nano": as_of})


def begin_final_holdout(spec: ExperimentSpec, reservation_token: str,
                        holdout_directory: Path) -> None:
    with exclusive_lock(holdout_directory / "registry.lock", spec.run_id):
        record = _record(spec, reservation_token, holdout_directory)
        if record["status"] not in ("RESERVED", "CONSUMED_IN_PROGRESS"):
            raise HoldoutConsumedError("holdout_not_available_to_begin")
        _lease(holdout_directory, spec.holdout_id)
        record["status"] = "CONSUMED_IN_PROGRESS"
        atomic_write(_holdout_path(holdout_directory, spec), canonical_json_bytes(record))
        _consume_interval(holdout_directory, record, "BEGIN", spec.evaluation_as_of_nano)
        # No caller can access final labels until BOTH durable writes have returned.


def consume_final_holdout(spec: ExperimentSpec, reservation_token: str,
                          holdout_directory: Path, report_sha256: str) -> None:
    require_hash(report_sha256)
    with exclusive_lock(holdout_directory / "registry.lock", spec.run_id):
        record = _record(spec, reservation_token, holdout_directory)
        if record["status"] == "CONSUMED_SUCCESS" and record["report_sha256"] == report_sha256:
            return
        if record["status"] != "CONSUMED_IN_PROGRESS":
            raise HoldoutConsumedError("holdout_invalid_success_transition")
        _consume_interval(holdout_directory, record, "BEGIN", spec.evaluation_as_of_nano)
        record.update(status="CONSUMED_SUCCESS", report_sha256=report_sha256)
        atomic_write(_holdout_path(holdout_directory, spec), canonical_json_bytes(record))
        _release(holdout_directory, spec.holdout_id)


def fail_final_holdout(spec: ExperimentSpec, reservation_token: str,
                       holdout_directory: Path, reason: str) -> None:
    if not isinstance(reason, str) or not reason:
        raise ExperimentError("failure_reason_required")
    with exclusive_lock(holdout_directory / "registry.lock", spec.run_id):
        record = _record(spec, reservation_token, holdout_directory)
        if record["status"] == "FAILED_CONSUMED" and record["failure_reason"] == reason:
            return
        if record["status"] not in ("RESERVED", "CONSUMED_IN_PROGRESS"):
            raise HoldoutConsumedError("holdout_invalid_failure_transition")
        _consume_interval(holdout_directory, record, "FAIL", spec.evaluation_as_of_nano)
        record.update(status="FAILED_CONSUMED", failure_reason=reason)
        atomic_write(_holdout_path(holdout_directory, spec), canonical_json_bytes(record))
        _release(holdout_directory, spec.holdout_id)


def recover_final_holdouts(holdout_directory: Path, as_of_nano: int) -> None:
    if type(as_of_nano) is not int:
        raise ExperimentError("as_of_must_be_int")
    with exclusive_lock(holdout_directory / "registry.lock", "recovery"):
        records = [(p, parse_json_bytes(p.read_bytes())) for p in sorted(holdout_directory.glob("*.json"))]
        acquired = []
        try:
            # Check every active lease before changing any state.
            for _, record in records:
                if record["status"] in ("RESERVED", "CONSUMED_IN_PROGRESS"):
                    if _lease_key(holdout_directory, record["holdout_id"]) in _LEASES:
                        raise ExperimentError("active_holdout_runner")
                    _lease(holdout_directory, record["holdout_id"])
                    acquired.append(record["holdout_id"])
            for path, record in records:
                if record["status"] not in ("RESERVED", "CONSUMED_IN_PROGRESS"):
                    continue
                if as_of_nano < record["started_at_nano"]:
                    raise ExperimentError("recovery_clock_precedes_reservation")
                _consume_interval(holdout_directory, record, "FAIL", as_of_nano)
                record.update(status="FAILED_CONSUMED", failure_reason="ORPHAN_ACCESS_CANNOT_BE_DISPROVED")
                atomic_write(path, canonical_json_bytes(record))
        finally:
            for holdout_id in acquired:
                _release(holdout_directory, holdout_id)


def import_observed_scope(scope: HoldoutScope, holdout_directory: Path, evidence_id: str,
                           as_of_nano: int) -> None:
    """Import v1/development observations conservatively, independent of data hashes."""
    require_id(evidence_id)
    with exclusive_lock(holdout_directory / "registry.lock", evidence_id):
        _consume_interval(holdout_directory, {"scope": content(scope), "experiment_id": evidence_id,
                                             "holdout_id": digest(scope)}, "IMPORT_PREVIOUSLY_SEEN", as_of_nano)


def validate_holdout_calendar(scope: HoldoutScope, sessions: tuple[SessionWindow, ...],
                              as_of_nano: int) -> None:
    if scope.calendar_sha256 != digest(sessions):
        raise ExperimentError("holdout_calendar_hash_mismatch")
    days = calendar_days(sessions)
    decisions = [(d, lo, hi) for d, lo, hi in days if scope.decision_start_nano <= lo < scope.decision_end_nano]
    tail = [(d, lo, hi) for d, lo, hi in days if scope.decision_end_nano <= lo and hi <= scope.maturity_end_nano]
    if len(decisions) != 60 or len(tail) < 20 or scope.maturity_end_nano > as_of_nano:
        raise StageIncompleteError("HOLDOUT_REQUIRES_60_DECISION_DAYS_AND_20_MATURE_TAIL_DAYS")
    if any(hi > scope.decision_end_nano for _, _, hi in decisions):
        raise ExperimentError("holdout_splits_trading_day")
    observed = [(d, lo, hi) for d, lo, hi in days if hi <= as_of_nano]
    if not observed or tail[-1][0] != observed[-1][0] or decisions[-1][0] != observed[-len(tail)-1][0]:
        raise ExperimentError("holdout_not_global_last_mature_cohort")


def verify_reproduction(reference: Path, config_hash: str, input_hashes: tuple[str, ...],
                        source_hash: str) -> dict:
    manifest = parse_json_bytes((reference / "manifest.json").read_bytes())
    for name, expected in manifest["files"].items():
        path = (reference / name).resolve()
        if not path.is_relative_to(reference.resolve()) or path.is_symlink():
            raise ExperimentError("unsafe_reference_path")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ExperimentError("reference_artifact_hash_mismatch")
    spec = parse_json_bytes((reference / "experiment.json").read_bytes())["spec"]
    if (spec["parameter_sha256"] != config_hash or spec["input_manifest_hashes"] != list(input_hashes)
            or spec["source_sha256"] != source_hash):
        raise ExperimentError("reproduce_identity_mismatch")
    return {"mode": "REPRODUCE", "new_statistical_evidence": False, "reference_manifest_sha256": hashlib.sha256((reference / "manifest.json").read_bytes()).hexdigest()}


def run_validation_experiment(bundle: FrozenInput, config: Mapping[str, JsonValue],
                               output: Path, mode: str) -> Mapping[str, JsonValue]:
    # The body is connected only to verified public producers below. A missing
    # upstream lock is a stage error, never a locally fabricated contract PASS.
    return _execute_validation(bundle, config, output, mode)


def run_shadow_replay_experiment(bundle: FrozenInput, config: Mapping[str, JsonValue],
                                 output: Path, feed) -> Mapping[str, JsonValue]:
    from ak_trading.v2.artifacts import verify_contract_lock
    from ak_trading.v2.contracts import AccountSnapshot,DataQuality
    from ak_trading.v2.decision.ledger import ContinuousLedger
    from ak_trading.v2.decision.payoffs import fit_path_payoff_model
    from ak_trading.v2.decision.pipeline import model_hashes_for,registry_hash_for
    from ak_trading.v2.policy import research_policy
    from ak_trading.v2.strategies.registry import strategy_specs_v2
    from .shadow import run_shadow
    splits,policies,scope=parse_config(config)
    lock=Path(config["contract_lock_path"])
    if not lock.is_absolute():lock=PROJECT_ROOT/lock
    if not lock.is_file():raise StageIncompleteError("M1_CONTRACT_LOCK_NOT_PUBLISHED")
    # Dependency gate before lock-content verification and before the
    # expensive fold fit (arbitration §3.1): a missing payoff producer is an
    # immediate engineering failure on this entry too.
    verify_payoff_training_dependencies()
    verify_contract_lock(lock,PROJECT_ROOT)
    specs=tuple(s for mode in ("MINUTE","DAILY") for s in strategy_specs_v2(mode) if s.strategy_id in config["strategy_ids"])
    if len(splits)!=1:raise ExperimentError("shadow_replay_requires_one_frozen_model_cohort")
    # E-P1 RULING 2.2: the plan set is frozen and (for schema v2.1.3)
    # verified against the declared hash before any expensive fitting here
    # too — never only after the models exist.
    plan_sets,_actual,_declared=_frozen_payoff_training_plans(bundle,config,splits,specs)
    fold=_fit_fold(bundle,config,splits[0],specs)
    if fold["models"] is None:raise StageIncompleteError("SHADOW_MODEL_TRAINING_INSUFFICIENT")
    payoff,training_payloads,training_exclusions=_fit_training_payoffs(bundle,config,splits[0],specs,hashlib.sha256(lock.read_bytes()).hexdigest(),plans=plan_sets.get(splits[0].split_id))
    # One policy is explicit in each account; additional branches never share cash.
    selected=tuple(p for p in policies if p.cost_scenario=="BASE" and p.routing_mode=="BEST_EV")
    if len(selected)!=1:raise ExperimentError("shadow_replay_base_policy_ambiguous")
    run_policy=selected[0]
    initial_cash=Decimal(config["initial_cash_cny"])
    start=splits[0].test_start_nano
    day=next((s.trading_day for s in bundle.sessions if s.start_nano<=start<s.end_nano),None)
    if day is None:raise ExperimentError("shadow_start_calendar_missing")
    zero=Decimal(0)
    account=AccountSnapshot("account:"+digest(run_policy),start,day,initial_cash,initial_cash,zero,zero,zero,zero,initial_cash,
                            zero,zero,initial_cash,zero,initial_cash,(),False,
                            DataQuality("READY",("SYNTHETIC",),"reconstructed",digest([content(run_policy),start])))
    lock_hash=hashlib.sha256(lock.read_bytes()).hexdigest()
    dependencies=model_hashes_for(fold["models"],payoff,fold["reference"],_seasonal_handle(fold))
    ledger=ContinuousLedger(account,bundle.instruments,run_policy,lock_hash,registry_hash_for(specs),exit_registry_sha256=_reg)
    checkpoint=ledger.checkpoint((),(),dependencies,{},pending_remaining_lots={})
    coroutine=run_shadow(feed,bundle,fold["models"],payoff,_seasonal_handle(fold),fold["reference"],specs,checkpoint,run_policy,
                          research_policy(),(),{},feed.clock,lock,None,None,output,config["evaluation_as_of_nano"],activation_request=None)
    # Frozen replay has no I/O waits; drive cooperative yields without opening
    # Windows asyncio's socketpair, which the offline guard intentionally denies.
    try:
        while True:
            yielded=coroutine.send(None)
            if yielded is not None:raise ExperimentError("offline_replay_unexpected_external_await")
    except StopIteration as done:
        acceptance=done.value
    finally:
        coroutine.close()
    return {"engineering":"PASS","statistics":"INSUFFICIENT","runtime":acceptance["runtime"],
            "m5_complete":False,"blockers":acceptance["reasons"]}


CONFIG_FIELDS = frozenset("schema_version run_id contract_ids minute_horizons daily_horizons feature_durations strategy_ids run_policies required_group_keys seed initial_cash_cny research_policy_version macro_policy_version contract_lock_path split_specs holdout_id holdout_scope macro_mode evaluation_as_of_nano model_fit_configs cost_scenarios synthetic".split())
# E-P1 RULING 2.2 (M1 allocation ak_probability_research_config_v2.1.3): the
# strict plan-set registration field. Legacy v2.1.0 configs stay acceptable
# unchanged; v2.1.3 configs must carry exactly this one additional field and
# its value must be a real 64-hex SHA-256 of the canonical frozen plan set.
PAYOFF_TRAINING_PLAN_FIELD = "payoff_training_plan_sha256"
RESEARCH_CONFIG_SCHEMA_LEGACY = "ak_probability_research_config_v2.1.0"
RESEARCH_CONFIG_SCHEMA_WITH_PLANS = "ak_probability_research_config_v2.1.3"


def parse_config(config: Mapping[str, JsonValue]) -> tuple[tuple[SplitSpec,...],tuple[RunPolicy,...],HoldoutScope]:
    from ak_trading.v2.schemas import decode_record
    version=config["schema_version"] if isinstance(config,Mapping) and "schema_version" in config else None
    if version==RESEARCH_CONFIG_SCHEMA_WITH_PLANS:
        if set(config)!=CONFIG_FIELDS|{PAYOFF_TRAINING_PLAN_FIELD}:
            raise ExperimentError("research_config_schema_mismatch")
        require_hash(config[PAYOFF_TRAINING_PLAN_FIELD])
    elif version==RESEARCH_CONFIG_SCHEMA_LEGACY:
        if set(config)!=CONFIG_FIELDS:
            raise ExperimentError("research_config_schema_mismatch")
    else:
        raise ExperimentError("research_config_schema_mismatch")
    require_id(config["run_id"])
    for key,expected in (("minute_horizons",[1,5,15,30,60]),("daily_horizons",[1,5,20]),
                         ("feature_durations",[60,300,900,1800,3600,86400]),("cost_scenarios",list(COST_SCENARIOS))):
        if config[key] != expected:
            raise ExperimentError(f"frozen_config_values_changed:{key}")
    if type(config["synthetic"]) is not bool or type(config["seed"]) is not int or type(config["evaluation_as_of_nano"]) is not int:
        raise ExperimentError("invalid_config_scalar_types")
    if not isinstance(config["initial_cash_cny"],str):
        raise ExperimentError("initial_cash_must_be_decimal_string")
    cash=Decimal(config["initial_cash_cny"])
    if not cash.is_finite() or cash<=0:
        raise ExperimentError("initial_cash_must_be_positive_finite")
    splits = tuple(SplitSpec(**s) for s in config["split_specs"])
    policies = tuple(decode_record(RunPolicy,p) for p in config["run_policies"])
    raw = config["holdout_scope"]
    scope = HoldoutScope(**{**raw,"products":tuple(raw["products"]),"contract_ids":tuple(raw["contract_ids"])})
    if config["holdout_id"] != digest(scope):
        raise ExperimentError("holdout_scope_identity_mismatch")
    if tuple(config["contract_ids"]) != scope.contract_ids:
        raise ExperimentError("contract_whitelist_mismatch")
    if len(set(config["strategy_ids"])) != len(config["strategy_ids"]):
        raise ExperimentError("duplicate_strategy_id")
    from ak_trading.v2.probability.models import default_fit_config
    if set(config["model_fit_configs"]) != {s.split_id for s in splits}:
        raise ExperimentError("fit_config_split_keys_mismatch")
    for s in splits:
        fit = config["model_fit_configs"][s.split_id]
        if any(k.startswith("SEASONAL:") and ":v2.1.1:" not in k for k in fit["feature_keys"]):
            raise ExperimentError("obsolete_seasonal_feature_key")
        if set(fit) != set(default_fit_config((),0,1,2)):
            raise ExperimentError("fit_config_fields_mismatch")
        if (fit["training_end_nano"],fit["calibration_end_nano"],fit["seed"]) != (s.train_end_nano,s.calibration_end_nano,config["seed"]):
            raise ExperimentError("fit_config_time_or_seed_mismatch")
        if fit["model_valid_until_nano"] <= s.test_end_nano:
            raise ExperimentError("model_expires_before_test_end")
    canonical_json_bytes(content(config))
    return splits,policies,scope


def code_hashes() -> dict[str,str]:
    files=sorted((PROJECT_ROOT/"src/ak_trading/v2").rglob("*.py")) + [PROJECT_ROOT/"scripts/research_probability_v2.py"]
    return {p.relative_to(PROJECT_ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()}


def _build_snapshots(bundle, config, split):
    from dataclasses import replace
    from ak_trading.v2.clock import aggregate_bars
    from ak_trading.v2.data import visible_rule
    from ak_trading.v2.factor_specs import factor_registry_v2,baseline_registry_v2
    from ak_trading.v2.factors import build_feature_snapshot
    from ak_trading.v2.seasonality import fit_seasonality
    train_minutes=tuple(b for b in bundle.bars if b.duration_seconds==60)
    seasonal=fit_seasonality(train_minutes,bundle.sessions,split.train_start_nano,split.train_end_nano)
    requires_seasonal=any(k.startswith("SEASONAL:") for k in config["model_fit_configs"][split.split_id]["feature_keys"])
    seasonal_prefixes={}
    # Calendar and data are frozen. No database, transport or model configuration loads.
    # T7 CPU fix, byte-identical outputs: (1) the per-duration frozen spec
    # registry (a pure function of the duration) is built once instead of
    # per bar; (2) the daily background is selected from a per-contract list
    # of that contract's daily snapshots in creation order — exactly the
    # subsequence the original ``reversed(snapshots)`` scan filtered to —
    # so the last matching snapshot is the same one the full scan returned.
    durations=sorted(config["feature_durations"],key=lambda d:(d!=86400,d))
    specs_by_duration={duration:factor_registry_v2(duration)+baseline_registry_v2(duration) for duration in durations}
    snapshots=[];exclusions=[]
    daily_by_contract:dict[str,list]={}
    for contract in config["contract_ids"]:
        minutes=tuple(b for b in bundle.bars if b.contract_id==contract and b.duration_seconds==60
                      and b.end_nano<=split.test_end_nano and b.available_at_nano is not None)
        daily=tuple(b for b in bundle.bars if b.contract_id==contract and b.duration_seconds==86400
                    and b.end_nano<=split.test_end_nano and b.available_at_nano is not None)
        for duration in durations:
            bars=daily if duration==86400 else minutes if duration==60 else aggregate_bars(minutes,bundle.sessions,duration,split.test_end_nano)
            specs=specs_by_duration[duration]
            prefix=[]
            for bar in sorted(bars,key=lambda b:(b.available_at_nano or b.end_nano,b.end_nano,b.bar_id)):
                if not bar.complete or bar.available_at_nano is None:continue
                prefix.append(bar)
                decision=max(bar.end_nano,bar.available_at_nano)
                if decision>=split.test_end_nano:continue
                seasonal_for_snapshot=None
                if requires_seasonal and duration!=86400:
                    if decision>=split.train_end_nano and duration==60:
                        seasonal_for_snapshot=seasonal
                    else:
                        cache_key=(contract,duration,bar.trading_day)
                        if cache_key not in seasonal_prefixes:
                            past=tuple(b for b in bars if b.trading_day is not None and b.trading_day<bar.trading_day
                                       and b.available_at_nano is not None and b.available_at_nano<decision)
                            if past:
                                cutoff=max(b.available_at_nano for b in past)
                                seasonal_prefixes[cache_key]=fit_seasonality(past,bundle.sessions,min(b.start_nano for b in past),cutoff)
                            else:
                                seasonal_prefixes[cache_key]=None
                        seasonal_for_snapshot=seasonal_prefixes[cache_key]
                background=None
                if duration!=86400:
                    background=next((f for f in reversed(daily_by_contract.get(contract,()))
                                     if f.trading_day<bar.trading_day and f.available_at_nano<=decision),None)
                snapshot=build_feature_snapshot(contract,tuple(prefix),decision,specs,background,seasonal_for_snapshot,
                                                 visible_rule(bundle,contract,decision),bundle.sessions)
                snapshots.append(snapshot)
                if duration==86400:daily_by_contract.setdefault(contract,[]).append(snapshot)
    return tuple(sorted(snapshots,key=lambda f:(f.decision_nano,f.contract_id,f.duration_seconds))),seasonal,exclusions


class _WindowIndex:
    """Local immutable-source index; keeps full slot data without sampling."""
    def __init__(self,bundle:FrozenInput):
        grouped={}
        for bar in bundle.bars:grouped.setdefault((bar.contract_id,bar.duration_seconds),[]).append(bar)
        self.groups={key:tuple(sorted(bars,key=lambda b:(b.start_nano,b.bar_id))) for key,bars in grouped.items()}
        self.starts={key:tuple(b.start_nano for b in bars) for key,bars in self.groups.items()}

    def interval(self,request,holding_end):
        from .splits import SampleInterval
        available=max([request.window_end_nano,holding_end]+[b.available_at_nano for b in self.window(request,holding_end) if b.available_at_nano is not None])
        return SampleInterval(request.request_id,request.features.product,request.features.trading_day,request.features.decision_nano,
                              request.window_end_nano,holding_end,available,request.horizon)

    def window(self,request,holding_end=None):
        duration=86400 if request.horizon.unit=="TRADING_DAY" else 60
        key=request.features.contract_id,duration
        starts=self.starts.get(key,())
        bars=self.groups.get(key,())
        lo=bisect_left(starts,request.window_start_nano)
        hi=bisect_left(starts,max(request.window_end_nano,holding_end or request.window_end_nano))
        return bars[lo:hi]


def label_window_bars(bundle:FrozenInput,request) -> tuple:
    return _WindowIndex(bundle).window(request)


def interval_for_request(request,bundle:FrozenInput,holding_end:int):
    from .splits import SampleInterval
    relevant=tuple(b for b in bundle.bars if b.contract_id==request.features.contract_id
                   and request.window_start_nano<=b.start_nano<max(request.window_end_nano,holding_end)
                   and b.duration_seconds==(86400 if request.horizon.unit=="TRADING_DAY" else 60))
    available=max([request.window_end_nano,holding_end]+[b.available_at_nano for b in relevant if b.available_at_nano is not None])
    return SampleInterval(request.request_id,request.features.product,request.features.trading_day,request.features.decision_nano,
                          request.window_end_nano,holding_end,available,request.horizon)


def _safe_forecast(model,request,regime,published,exclusions):
    from ak_trading.v2.probability.forecast import predict_market
    try:return predict_market(model,request,regime,published)
    except (ValueError,TypeError) as error:
        exclusions.append("PUBLICATION_FAILED:"+request.request_id+":"+str(error))
        return None


def _fit_fold(bundle,config,split,specs):
    from dataclasses import replace
    from ak_trading.v2.contracts import Horizon
    from ak_trading.v2.clock import ClockError,horizon_window
    from ak_trading.v2.macro import MacroCache
    from ak_trading.v2.probability.regimes import fit_regime_reference,assess_regime,freeze_range
    from ak_trading.v2.probability.forecast import build_forecast_requests,predict_market
    from ak_trading.v2.probability.labels import label_forecast
    from ak_trading.v2.probability.models import fit_forecast_models
    from .splits import SampleInterval,assign_splits
    snapshots,seasonal,exclusions=_build_snapshots(bundle,config,split)
    features=tuple(f for f in snapshots if f.duration_seconds in (60,86400) and f.reference_price is not None and f.atr_points is not None and f.atr_points>0)
    training=tuple(f for f in features if f.decision_nano<split.train_end_nano)
    reference=fit_regime_reference(training,split.train_end_nano,split.train_end_nano)
    cache=MacroCache(bundle.macro_items if config["macro_mode"]=="HISTORICAL_PIT" else ())
    requests=[];regimes={};sample_intervals=[];previous={};prefix=[]
    reference_by_day={}
    window_index=_WindowIndex(bundle)
    for f in features:
        if f.decision_nano>=split.calibration_end_nano:continue
        # An inner reference is fitted once from the completed previous-day
        # prefix, then frozen throughout this day's original minute grid.
        if f.decision_nano<split.train_end_nano:
            if f.trading_day not in reference_by_day:
                past=tuple(x for x in prefix if x.trading_day<f.trading_day)
                cutoff=max((x.decision_nano for x in past),default=f.decision_nano-1)
                reference_by_day[f.trading_day]=fit_regime_reference(past,cutoff,cutoff)
            ref=reference_by_day[f.trading_day]
        else:
            ref=reference
        key=f.contract_id,f.duration_seconds
        with_macro=config["macro_mode"]=="HISTORICAL_PIT" and all(p["macro_mode"]=="WITH_MACRO" for p in config["run_policies"])
        macro=cache.snapshot(f.decision_nano,f.product,with_macro,not with_macro)
        from ak_trading.v2.data import visible_rule
        from ak_trading.v2.decision.execution import fee_amount
        rule=visible_rule(bundle,f.contract_id,f.decision_nano)
        costs_known=rule is not None and all(leg is not None for leg in (rule.open_fee,rule.close_fee,rule.close_today_fee))
        if costs_known:
            cost=(fee_amount(rule.open_fee,f.reference_price,1,rule.multiplier)
                  +max(fee_amount(leg,f.reference_price,1,rule.multiplier) for leg in (rule.close_fee,rule.close_today_fee)))/rule.multiplier
            cost+=2*rule.price_tick*(rule.slippage_ticks+rule.impact_ticks)
        else:
            cost=Decimal(0)  # pure classifier placeholder, invalidated immediately below
        if costs_known:
            regime=assess_regime(f,macro,None,freeze_range(f),cost,ref,previous.get(key))
        else:
            missing_feature=replace(f,quality=replace(f.quality,status="MISSING",reasons=tuple(sorted(set((*f.quality.reasons,"COST_UNKNOWN"))))))
            regime=assess_regime(missing_feature,macro,None,freeze_range(f),Decimal(0),ref,previous.get(key))
        previous[key]=regime;regimes[f.snapshot_id]=regime;prefix.append(f)
        horizons=tuple(Horizon("TRADING_DAY" if f.duration_seconds==86400 else "TRADING_MINUTE",v)
                       for v in config["daily_horizons" if f.duration_seconds==86400 else "minute_horizons"])
        for h in horizons:
            try:
                batch=build_forecast_requests(f,(h,),regime.frozen_range,bundle.sessions)
            except ClockError:
                exclusions.append(f"HORIZON_CALENDAR_INSUFFICIENT:{h.unit}:{h.value}");continue
            for request in batch:
                requests.append(request)
                holding_end=request.window_end_nano
                for spec in specs:
                    if spec.mode==("DAILY" if f.duration_seconds==86400 else "MINUTE"):
                        try:holding_end=max(holding_end,horizon_window(f.decision_nano,f.product,spec.max_holding,bundle.sessions)[1])
                        except ClockError:pass
                sample_intervals.append(window_index.interval(request,holding_end))
    assignments=assign_splits(tuple(sample_intervals),split,bundle.sessions)
    by_id={a.sample_id:a for a in assignments}
    partitions={name:tuple(r for r in requests if by_id[r.request_id].partition==name) for name in ("TRAIN","CALIBRATION","TEST")}
    labels={}
    for name,reqs in partitions.items():
        cutoff={"TRAIN":split.train_end_nano,"CALIBRATION":split.calibration_end_nano,"TEST":config["evaluation_as_of_nano"]}[name]
        all_labels=tuple(label_forecast(r,window_index.window(r),bundle.sessions,(),cutoff) for r in reqs)
        labels[name]=all_labels
        if name!="TEST":
            partitions[name]=tuple(r for r,l in zip(reqs,all_labels) if l.status=="MATURE")
            labels[name]=tuple(l for l in all_labels if l.status=="MATURE")
        for l in all_labels:
            if l.status!="MATURE":exclusions.append(name+":"+l.status)
    if not partitions["TRAIN"] or not partitions["CALIBRATION"]:
        return {"snapshots":snapshots,"seasonal":seasonal,"reference":reference,"requests":tuple(requests),
                "labels":tuple(l for ls in labels.values() for l in ls),"assignments":assignments,
                "models":None,"forecasts":(),"exclusions":[*exclusions,"TRAIN_OR_CALIBRATION_INSUFFICIENT"]}
    def fs(name):return tuple({r.features.snapshot_id:r.features for r in partitions[name]}.values())
    def rs(name):return tuple(regimes[f.snapshot_id] for f in fs(name))
    model=fit_forecast_models(fs("TRAIN"),rs("TRAIN"),partitions["TRAIN"],labels["TRAIN"],
                              fs("CALIBRATION"),rs("CALIBRATION"),partitions["CALIBRATION"],labels["CALIBRATION"],
                              config["model_fit_configs"][split.split_id],reference,
                              seasonality=seasonal if any(k.startswith("SEASONAL:") for k in config["model_fit_configs"][split.split_id]["feature_keys"]) else None)
    if any(k.startswith("SEASONAL:") for k in config["model_fit_configs"][split.split_id]["feature_keys"]):
        if model.source_hashes.get("seasonality")!=seasonal.content_hash():
            raise StageIncompleteError("FORECAST_PRODUCER_SEASONALITY_BINDING_MISSING")
    # Build test contexts only after the final model exists, so daily background
    # is a real already-published forecast rather than a synthetic trend label.
    test_forecasts=[];test_labels=[];test_requests=[];test_intervals=[]
    published_daily={}
    from ak_trading.v2.data import visible_rule
    from ak_trading.v2.decision.execution import fee_amount
    for f in features:
        if not split.test_start_nano<=f.decision_nano<split.test_end_nano:continue
        prior=previous.get((f.contract_id,f.duration_seconds))
        background=published_daily.get(f.contract_id) if f.duration_seconds!=86400 else None
        if background is not None and not background.published_at_nano<=f.decision_nano<background.expires_at_nano:background=None
        rule=visible_rule(bundle,f.contract_id,f.decision_nano)
        valid=rule is not None and all(leg is not None for leg in (rule.open_fee,rule.close_fee,rule.close_today_fee))
        if not valid:
            exclusions.append("TEST:COST_UNKNOWN")
            continue
        cost=(fee_amount(rule.open_fee,f.reference_price,1,rule.multiplier)+max(fee_amount(leg,f.reference_price,1,rule.multiplier) for leg in (rule.close_fee,rule.close_today_fee)))/rule.multiplier+2*rule.price_tick*(rule.slippage_ticks+rule.impact_ticks)
        macro=cache.snapshot(f.decision_nano,f.product,False,True)
        regime=assess_regime(f,macro,background,freeze_range(f),cost,reference,prior)
        previous[(f.contract_id,f.duration_seconds)]=regime
        horizons=tuple(Horizon("TRADING_DAY" if f.duration_seconds==86400 else "TRADING_MINUTE",v)
                       for v in config["daily_horizons" if f.duration_seconds==86400 else "minute_horizons"])
        for h in horizons:
            try:batch=build_forecast_requests(f,(h,),regime.frozen_range,bundle.sessions)
            except ClockError:
                exclusions.append(f"TEST:HORIZON_CALENDAR_INSUFFICIENT:{h.unit}:{h.value}");continue
            for request in batch:
                test_requests.append(request)
                test_intervals.append(window_index.interval(request,request.window_end_nano))
                prediction=_safe_forecast(model,request,regime,f.decision_nano,exclusions)
                if prediction is not None:test_forecasts.append(prediction)
                test_labels.append(label_forecast(request,window_index.window(request),bundle.sessions,(),config["evaluation_as_of_nano"]))
                if prediction is not None and f.duration_seconds==86400 and h.value==1 and request.barrier_key=="ATR_1" and prediction.quality.status=="READY":
                    published_daily[f.contract_id]=prediction
    all_assignments=assign_splits(tuple((*sample_intervals,*test_intervals)),split,bundle.sessions)
    accepted={a.sample_id for a in all_assignments if a.partition=="TEST"}
    forecasts=tuple(f for f in test_forecasts if f.request_id in accepted)
    return {"snapshots":snapshots,"seasonal":seasonal,"reference":reference,"requests":tuple((*requests,*test_requests)),
            "labels":tuple([l for ls in labels.values() for l in ls]+test_labels),"assignments":all_assignments,"models":model,
            "forecasts":forecasts,"exclusions":exclusions}


def _training_windows(bundle:FrozenInput,start:int,end:int,spec) -> tuple[tuple[int,int],...]:
    from ak_trading.v2.clock import horizon_window,ClockError
    days=[(d,lo,hi) for d,lo,hi in calendar_days(bundle.sessions) if start<=lo and hi<=end]
    windows=[]
    for _,lo,hi in days:
        if spec.mode=="MINUTE":
            windows.append((lo,hi+1))
        else:
            ends=[]
            for product in sorted({i.product for i in bundle.instruments}):
                try:ends.append(horizon_window(hi,product,spec.max_holding,bundle.sessions)[1])
                except ClockError:ends=[];break
            if ends and max(ends)<end:windows.append((lo,max(ends)+1))
    return tuple(windows)


def verify_payoff_training_dependencies() -> None:
    """Import-probe the payoff training producers before expensive fitting.

    Raises StageIncompleteError naming every missing module/symbol so the
    engineering failure is reported before any costly work, on both the
    validation and shadow-replay entries.
    """
    import importlib
    missing = []
    for module_name, symbol in PAYOFF_TRAINING_DEPENDENCY_PROBES:
        try:
            module = importlib.import_module(module_name)
        except ImportError as error:
            missing.append(f"{module_name}.{symbol}:{type(error).__name__}")
            continue
        if not hasattr(module, symbol):
            missing.append(f"{module_name}.{symbol}:AttributeError")
    if missing:
        raise StageIncompleteError("PAYOFF_TRAINING_DEPENDENCY_MISSING:" + ",".join(missing))


def _payoff_inner_split(bundle:FrozenInput,config,split):
    """Deterministic inner split inside the outer TRAIN interval.

    E-P1 RULING 4.1: the inner train/calibration/OOS partitions stay real and
    recomputed here (never one shared frozen fold for every period); the same
    arithmetic feeds both the pre-frozen plan set and the real inner fit so
    the two can never disagree. Returns (inner, days) or (None, None) when
    the registered inner coverage is insufficient.
    """
    days=[d for d in calendar_days(bundle.sessions) if split.train_start_nano<=d[1] and d[2]<=split.train_end_nano]
    fit_config=config["model_fit_configs"][split.split_id]
    border=int(len(days)*fit_config["inner_split_fraction"])
    minimum=fit_config["inner_minimum_validation_days"]+fit_config["inner_embargo_days"]
    if border<fit_config["inner_minimum_train_days"] or len(days)-border<minimum+2:
        return None,None
    calibration_end=border+minimum
    inner=SplitSpec(split.split_id+"-payoff-inner",days[0][1],days[border][1],days[border][1],days[calibration_end][1],
                    days[calibration_end][1],split.train_end_nano,split.embargo_minutes,split.embargo_days,False)
    return inner,days


def payoff_training_plan_set_bytes(plans) -> bytes:
    """Canonical bytes of the complete frozen plan set (M1 §2.2 registration)."""
    records=[encode_record(plan) for plan in
             sorted(plans,key=lambda p:(p.outer_split_id,p.strategy_id,p.window_start_nano,p.window_end_nano))]
    return canonical_json_bytes({"schema_version":PAYOFF_TRAINING_PLAN_SCHEMA_ID,"plans":records})


def payoff_training_plan_set_sha256(plans) -> str:
    return hashlib.sha256(payoff_training_plan_set_bytes(plans)).hexdigest()


def _payoff_training_plans(bundle:FrozenInput,config,split,specs):
    """Pre-frozen per-window sampling plans (E-P1 RULING 2.2/4.1).

    Windows are bounded by the frozen calendar, the original outer split and
    the original strategy term only — never by realized returns. Each
    window's warmup starts at the close of the previous trading day and is
    legal only when it begins strictly after the inner calibration cutoff;
    the window end is clamped to the outer training cutoff. Returns
    (planned, skipped); planned entries are (spec, window_start, window_end,
    plan) and skipped entries are (strategy_id, window_start, reason).
    """
    from ak_trading.v2.strategies.registry import select_exit_binding
    inner,days=_payoff_inner_split(bundle,config,split)
    planned=[];skipped=[]
    if inner is None or not days:return tuple(planned),tuple(skipped)
    for spec in specs:
        if spec.family=="WAIT":continue
        binding=select_exit_binding(spec)
        for window_start,window_end in _training_windows(bundle,inner.test_start_nano,inner.test_end_nano,spec):
            end=min(window_end,inner.test_end_nano)
            index=next((i for i,(_,lo,_) in enumerate(days) if lo==window_start),None)
            if index is None or index==0:
                skipped.append((spec.strategy_id,window_start,"PAYOFF_PLAN_WINDOW_UNPLANNED:WINDOW_NOT_IN_TRAIN_CALENDAR"));continue
            warmup=days[index-1][2]
            if warmup<=inner.calibration_end_nano or not window_start<end:
                skipped.append((spec.strategy_id,window_start,"PAYOFF_PLAN_WINDOW_UNPLANNED:NO_LEGAL_WARMUP"));continue
            plan=PayoffTrainingPlan(
                protocol_version=PAYOFF_TRAINING_PLAN_PROTOCOL_VERSION,
                outer_split_id=split.split_id,inner_split_id=inner.split_id,
                inner_training_end_nano=inner.train_end_nano,
                inner_calibration_end_nano=inner.calibration_end_nano,
                warmup_start_nano=warmup,window_start_nano=window_start,window_end_nano=end,
                training_end_nano=split.train_end_nano,
                contract_ids=tuple(sorted(set(config["contract_ids"]))),
                strategy_id=spec.strategy_id,exit_binding_sha256=binding.binding_sha256,
                initial_cash_cny=Decimal(config["initial_cash_cny"]),
                calendar_sha256=digest(bundle.sessions),
                selection_rule=PAYOFF_TRAINING_PLAN_SELECTION_RULE)
            planned.append((spec,window_start,end,plan))
    return tuple(planned),tuple(skipped)


def _frozen_payoff_training_plans(bundle,config,splits,specs):
    """Freeze the complete plan set before expensive fitting, before holdout
    consumption and before formal registration (E-P1 RULING 2.2); a v2.1.3
    research config must declare the identical plan-set hash."""
    plan_sets={s.split_id:_payoff_training_plans(bundle,config,s,specs) for s in splits}
    actual=payoff_training_plan_set_sha256([entry[3] for value in plan_sets.values() for entry in value[0]])
    declared=config[PAYOFF_TRAINING_PLAN_FIELD] if PAYOFF_TRAINING_PLAN_FIELD in config else None
    if declared is not None and declared!=actual:
        raise ExperimentError("payoff_training_plan_set_mismatch")
    return plan_sets,actual,declared


def _fit_training_payoffs(bundle,config,split,specs,lock_hash,plans=None):
    """Use a predeclared inner split from the outer training interval only.

    The inner train/calibration/OOS fit runs for real against the inner
    ``SplitSpec`` (E-P1 RULING 4.1: real inner split/models retained; no
    shared frozen fold); observations come only from the §2.1 measurement
    interface over the pre-frozen plan set, never from an ordinary
    ``ReplayResult`` OPEN.
    """
    from dataclasses import replace
    from ak_trading.v2.decision.payoffs import fit_path_payoff_model
    inner,_days=_payoff_inner_split(bundle,config,split)
    if inner is None:
        return fit_path_payoff_model((),split.train_end_nano,config["research_policy_version"]),{},["INNER_OOS_PAYOFF_COVERAGE_INSUFFICIENT"]
    fit_config=config["model_fit_configs"][split.split_id]
    inner_config=dict(config)
    inner_config["model_fit_configs"]={inner.split_id:{**fit_config,"training_end_nano":inner.train_end_nano,
                                                      "calibration_end_nano":inner.calibration_end_nano,
                                                      "model_valid_until_nano":split.train_end_nano+1}}
    inner_config["evaluation_as_of_nano"]=split.train_end_nano
    prefix_bundle=replace(bundle,bars=tuple(b for b in bundle.bars if b.available_at_nano is not None and b.available_at_nano<=split.train_end_nano))
    fold=_fit_fold(prefix_bundle,inner_config,inner,specs)
    if fold["models"] is None:
        return fit_path_payoff_model((),split.train_end_nano,config["research_policy_version"]),{},["INNER_OOS_MODEL_INSUFFICIENT"]
    observations=[];payloads={};exclusions=[]
    planned,skipped=plans if plans is not None else _payoff_training_plans(bundle,config,split,specs)
    for strategy_id,window_start,reason in skipped:
        exclusions.append(strategy_id+":"+str(window_start)+":"+reason)
    for spec,window_start,window_end,plan in planned:
        outcome,pieces,reason=_training_opportunity(prefix_bundle,config,split,spec,fold,lock_hash,window_start,window_end,plan)
        for name,raw in pieces.items():payloads[name]=raw
        if outcome is not None:observations.append(outcome)
        if reason:exclusions.append(spec.strategy_id+":"+str(window_start)+":"+reason)
    return fit_path_payoff_model(tuple(observations),split.train_end_nano,config["research_policy_version"]),payloads,exclusions


def _training_fixed_policy(config,spec,binding_origin):
    """Nine-field FIXED training policy from one registered binding original.

    P-1 (08-M3C §5.1, arbitration §3.1): exactly one M3B ``select_exit_binding``
    original supplies ``fixed_exit_binding_sha256``; the same original also
    feeds the replay ``exit_bindings`` and the payoff condition key's 17th
    item via the strategy context.
    """
    from ak_trading.v2.strategies.registry import select_exit_binding
    binding=select_exit_binding(spec)
    if binding.binding_sha256!=binding_origin.binding_sha256:
        raise ExperimentError("training_exit_binding_identity_mismatch")
    return RunPolicy(purpose="RESEARCH_EVALUATION",routing_mode="FIXED",seed=config["seed"],
                     fixed_strategy_id=spec.strategy_id,
                     fixed_exit_binding_sha256=binding.binding_sha256,
                     research_policy_version=config["research_policy_version"],
                     macro_policy_version=config["macro_policy_version"],
                     macro_mode="PRICE_ONLY",cost_scenario="BASE")


def _training_window_manifest(pieces,window,plan_hash,lock_hash,exit_registry_sha256,result):
    files={name[len(window):]:hashlib.sha256(raw).hexdigest() for name,raw in pieces.items() if name.startswith(window)}
    return canonical_json_bytes({"schema_version":"ak_probability_training_window_manifest_v1","files":files,
                                 "plan_sha256":plan_hash,"path_id":result.path_id,
                                 "contract_lock_sha256":lock_hash,"exit_registry_sha256":exit_registry_sha256,
                                 "input_manifest_sha256":result.input_manifest_sha256,
                                 "source_kind":result.source_kind})


def _training_opportunity(bundle,config,split,spec,fold,lock_hash,window_start,window_end,plan):
    """Measure one pre-frozen training window through the sole E-P1 §2.1
    offline measurement interface, then label and produce through the sole
    §2.4 observation producer.

    The empty-payoff ordinary replay that could never bootstrap (E-P1
    EVIDENCE: gates reject every unsupported OPEN) is removed: candidate
    generation, the seven-domain checks, isolated execution, exits and the
    ledger all run inside ``replay_payoff_training_window`` against the real
    inner-OOS models, and this function never treats an ordinary
    ``ReplayResult`` OPEN as an executed episode. The original candidate
    context/request, the complete ``PayoffPathResult`` and its originals are
    saved before labelling; the label uses the original ATR_1 window and the
    outer training cutoff; the producer is called exactly once at the end.
    Producer integrity/program errors and unknown codes block the run; only
    complete frozen refusal codes become per-window exclusions (RULING 3.4).
    """
    from ak_trading.v2.clock import ClockError,horizon_window
    from ak_trading.v2.contracts import AccountSnapshot,DataQuality
    from ak_trading.v2.decision.payoff_paths import replay_payoff_training_window
    from ak_trading.v2.decision.payoffs import observation_from_replay,payoff_observation_source
    from ak_trading.v2.probability.labels import label_forecast
    from ak_trading.v2.policy import research_policy
    from ak_trading.v2.strategies.registry import select_exit_binding
    binding=select_exit_binding(spec)
    policy=_training_fixed_policy(config,spec,binding)
    _reg,_es,_bs=_exit_pack((spec,))
    _replay_binding=next((b for b in _bs if b.strategy_id==spec.strategy_id),None)
    if _replay_binding is None or _replay_binding.binding_sha256!=binding.binding_sha256:
        raise ExperimentError("training_exit_binding_identity_mismatch")
    if policy.fixed_exit_binding_sha256!=binding.binding_sha256:
        raise ExperimentError("training_fixed_binding_identity_mismatch")
    plan_hash=payoff_training_plan_sha256(plan)
    zero=Decimal(0);cash=Decimal(config["initial_cash_cny"])
    # The warmup start is the previous trading day's close; it may equal that
    # day's hi and fall inside the inter-day gap, so the owning day resolves
    # with an inclusive upper bound.
    day=next((d for d,lo,hi in calendar_days(bundle.sessions) if lo<=plan.warmup_start_nano<=hi),None)
    if day is None:raise ExperimentError("training_plan_warmup_not_in_calendar")
    account=AccountSnapshot(PAYOFF_PATH_ACCOUNT_ID_PREFIX+plan_hash,plan.warmup_start_nano,day,cash,cash,zero,zero,zero,zero,
                            cash,zero,zero,cash,zero,cash,(),False,
                            DataQuality("READY",("SYNTHETIC" if config["synthetic"] else "RESEARCH",),"reconstructed",digest([plan_hash])))
    result=replay_payoff_training_window(bundle,fold["models"],spec,account,policy,research_policy(),_seasonal_handle(fold),
                                         fold["reference"],lock_hash,plan,exit_specs=_es,exit_binding=binding)
    window=spec.strategy_id+"/"+str(window_start)+"/"
    pieces={window+"plan.json":encode_record_file("PayoffTrainingPlan",PAYOFF_TRAINING_PLAN_SCHEMA_ID,(plan,)),
            window+"path.json":encode_record_file("PayoffPathResult",PAYOFF_PATH_SCHEMA_ID,(result,))}

    def record(status,reason,extra=None):
        payload={"window_start_nano":window_start,"window_end_nano":window_end,"scope":"TRAINING_INNER_OOS",
                 "selection":plan.selection_rule,"plan_sha256":plan_hash,"path_id":result.path_id,
                 "status":status,"status_reason":reason,"candidates_checked":len(result.candidate_checks),
                 "path_exclusions":list(result.exclusions)}
        if extra is not None:payload.update(extra)
        pieces[window+"opportunity.json"]=canonical_json_bytes(payload)
        pieces[window+"manifest.json"]=_training_window_manifest(pieces,window,plan_hash,lock_hash,_reg,result)
        return reason

    if result.status=="NO_CANDIDATE":
        # Candidate-stage fact (RULING 3.4): no producer call, no receipt.
        return None,pieces,record("NO_CANDIDATE","NO_TRIGGERED_BUDGETED_PATH")
    entry=next((d for d in result.path_decisions if d.action=="MEASURE_ENTRY"),None)
    if result.status!="COMPLETE" or entry is None:
        reason="PAYOFF_PATH_INCOMPLETE" if result.status!="COMPLETE" else "PAYOFF_PATH_COMPLETE_WITHOUT_ENTRY"
        return None,pieces,record("PATH_INCOMPLETE",reason)
    opened=entry.proposal;context=entry.context;request=entry.request
    if request is None:raise ExperimentError("training_measure_entry_request_missing")
    label=label_forecast(request,label_window_bars(bundle,request),bundle.sessions,(),split.train_end_nano)
    try:
        observation=observation_from_replay(opened,context,request,label,result,split.train_end_nano,
                                            str(context.features.trading_day),rule_originals=result.rule_originals)
        source_bytes=payoff_observation_source(opened,context,request,label,result,split.train_end_nano,
                                               str(context.features.trading_day),rule_originals=result.rule_originals)
    except ValueError as error:
        # Only complete frozen refusal codes become per-window exclusions;
        # same-prefix unknown suffixes and every other error keep blocking.
        if not _payoff_excludable(error):raise
        return None,pieces,record("PRODUCER_EXCLUDED",str(error))
    if hashlib.sha256(source_bytes).hexdigest()!=observation.source_sha256:
        raise ExperimentError("training_source_bytes_hash_mismatch")
    if observation.exit_binding_sha256!=binding.binding_sha256:
        raise ExperimentError("training_observation_binding_identity_mismatch")
    # Real M5 partition policy (RULING 4.1/4.3 item 4): closed influence
    # interval [start, max(label_end, holding_end, available)] with the
    # pre-registered longest-holding reservation, judged on the trading
    # calendar. The producer's own maturity minimums stay in the producer;
    # equality with the next partition boundary still purges.
    holding_end=request.window_end_nano
    try:
        holding_end=max(holding_end,horizon_window(context.features.decision_nano,context.features.product,
                                                   spec.max_holding,bundle.sessions)[1])
    except ClockError:pass
    available=max([request.window_end_nano,holding_end]
                  +[b.available_at_nano for b in label_window_bars(bundle,request) if b.available_at_nano is not None])
    if label.available_at_nano is not None:available=max(available,label.available_at_nano)
    interval=SampleInterval(request.request_id,context.features.product,context.features.trading_day,
                            context.features.decision_nano,request.window_end_nano,holding_end,available,request.horizon)
    assignment=assign_splits((interval,),split,bundle.sessions)[0]
    if assignment.partition!="TRAIN":
        reason="PAYOFF_PARTITION_EXCLUDED:"+assignment.partition+":"+"|".join(assignment.reasons)
        return None,pieces,record("PARTITION_EXCLUDED",reason,{"observation_id":observation.observation_id})
    pieces[window+"source.json"]=source_bytes
    pieces[window+"observation.json"]=canonical_json_bytes(content(observation))
    record("OBSERVATION",None,{"observation_id":observation.observation_id,"source_sha256":observation.source_sha256})
    return observation,pieces,None


def load_component_models(path:Path):
    """Read-only producer artifacts, used by offline component evidence tools."""
    from ak_trading.v2.probability.models import ForecastModelSet
    from ak_trading.v2.probability.regimes import RegimeReference
    from ak_trading.v2.contracts import CalibrationEvidence
    from ak_trading.v2.schemas import decode_record
    raw=parse_json_bytes((path/"models.json").read_bytes())
    reference=parse_json_bytes((path/"regime_reference.json").read_bytes())["reference"]
    if raw["schema_version"]!="ak_probability_models_v2.1.0":raise ExperimentError("component_model_schema_mismatch")
    model=raw["model_set"]
    handle=ForecastModelSet(**{**model,"feature_keys":tuple(model["feature_keys"]),
                              "evidence":tuple(decode_record(CalibrationEvidence,e) for e in model["evidence"])})
    ref=RegimeReference(**{**reference,"volatility_samples":{k:tuple(v) for k,v in reference["volatility_samples"].items()}})
    return handle,ref


def scenario_rules(base_rules:tuple,scenario:str) -> tuple:
    """One cost transformation from BASE originals; GAP/NO_STOP use stress paths."""
    from dataclasses import replace
    if scenario not in COST_SCENARIOS:raise ExperimentError("unknown_cost_scenario")
    if scenario in ("GAP","NO_STOP"):raise ExperimentError("stress_scenario_requires_path_simulation")
    if scenario=="BASE":return base_rules
    converted=[]
    for rule in base_rules:
        if rule.rule_id.startswith("scenario-rule:"):raise ExperimentError("cost_multiplier_already_applied")
        changes={}
        if scenario=="FEE_X2":
            for name in ("open_fee","close_fee","close_today_fee"):
                leg=getattr(rule,name)
                changes[name]=replace(leg,per_lot_cny=leg.per_lot_cny*2,notional_rate=leg.notional_rate*2) if leg is not None else None
        elif scenario=="SLIPPAGE_X2":changes.update(slippage_ticks=rule.slippage_ticks*2,impact_ticks=rule.impact_ticks*2)
        else:
            for name in ("long_margin_rate","short_margin_rate"):
                rate=getattr(rule,name);changes[name]=min(Decimal(1),rate*Decimal('1.5')) if rate is not None else None
        changed=replace(rule,**changes,evidence="SCENARIO")
        converted.append(replace(changed,rule_id="scenario-rule:"+digest([scenario,content(changed)])))
    return tuple(converted)


def evaluate_stress_cohort(result,bundle,scenario:str) -> tuple[dict,tuple]:
    from ak_trading.v2.decision.execution import simulate_stress_paths,rule_at
    if scenario not in ("GAP","NO_STOP"):raise ExperimentError("stress_scenario_required")
    # Completed BASE episodes form the only cohort; pressure paths never add samples.
    last=result.checkpoints[-1] if result.checkpoints else None
    open_ids={l.episode_id for l in last.account.lots} if last else set()
    if last:open_ids|={i.episode_id for i in last.pending_intents}
    mature={f.episode_id for f in result.fills if f.offset!="OPEN"}-open_ids
    budgets={p.episode_id:p.budget for p in result.proposals if p.action=="OPEN" and p.budget is not None and p.episode_id in mature}
    records=[];reasons=[]
    for episode,budget in sorted(budgets.items()):
        rule=rule_at(bundle.rules,budget.contract_id,budget.frozen_at_nano)
        if rule is None or rule.rule_id.startswith("scenario-rule:"):
            reasons.append("BASE_FROZEN_RULE_MISSING");continue
        sessions=tuple(s for s in bundle.sessions if any(i.contract_id==budget.contract_id and i.product==s.product for i in bundle.instruments))
        try:
            path=next(p for p in simulate_stress_paths(budget,rule,sessions) if p.scenario==scenario)
        except ValueError as error:
            reasons.append(str(error));continue
        if not path.trading_days:reasons.append("STRESS_CALENDAR_UNVERIFIED")
        records.append({"episode_id":episode,"budget":content(budget),"base_rule":content(rule),"path":content(path)})
    worst=max((Decimal(r["path"]["loss_cny"])/Decimal(r["budget"]["equity_at_start_cny"]) for r in records),default=Decimal(0))
    shortfall=max((Decimal(r["path"]["margin_shortfall_cny"]) for r in records),default=Decimal(0))
    peak=max((Decimal(r["path"]["peak_margin_cny"]) for r in records),default=Decimal(0))
    if not records:reasons.append("NO_MATURE_BASE_EPISODE_STRESS_PATH")
    return {"maximum_stress_loss_ratio":float(worst),"margin_shortfall_cny":str(shortfall),"peak_margin_cny":str(peak),
            "status":"FAIL" if worst>Decimal('.02') or shortfall>0 else "INSUFFICIENT",
            "reasons":sorted(set(reasons))},tuple(records)


def _seasonal_handle(fold):
    return fold["seasonal"] if fold["models"] is not None and "seasonality" in fold["models"].source_hashes else None


def _exit_pack(specs):
    from ak_trading.v2.strategies.registry import select_exit_binding
    from ak_trading.v2.exits.registry import exit_registry_specs
    from ak_trading.v2.decision.pipeline import exit_registry_hash_for
    bindings=tuple(sorted({select_exit_binding(x).binding_sha256:select_exit_binding(x) for x in specs}.values(),key=lambda b:b.binding_sha256))
    especs=tuple(sorted({x.exit_policy_id:x for x in (*exit_registry_specs("MINUTE"),*exit_registry_specs("DAILY"))}.values(),key=lambda x:x.exit_policy_id))
    return exit_registry_hash_for(especs),especs,bindings


def _replay_fold(bundle,split,fold,payoff,specs,run_policy,lock_hash,initial_equity:Decimal):
    from dataclasses import replace
    from ak_trading.v2.contracts import AccountSnapshot,DataQuality
    from ak_trading.v2.policy import research_policy
    from ak_trading.v2.decision.replay import run_decision_replay
    rules=scenario_rules(bundle.rules,run_policy.cost_scenario)
    scenario_bundle=replace(bundle,rules=rules)
    initial=initial_equity;zero=Decimal(0)
    if not isinstance(initial,Decimal) or not initial.is_finite() or initial<=0:raise ExperimentError("invalid_initial_equity")
    days=calendar_days(bundle.sessions)
    day=next((day for day,lo,hi in days if lo<=split.test_start_nano<hi),None)
    if day is None:raise ExperimentError("replay_test_start_not_in_calendar")
    account=AccountSnapshot("account:"+digest([split.split_id,content(run_policy)]),split.test_start_nano,day,initial,initial,zero,zero,zero,zero,
                            initial,zero,zero,initial,zero,initial,(),False,
                            DataQuality("READY",("SCENARIO",),"reconstructed",digest([split.split_id,content(run_policy)])))
    _reg,_es,_bs=_exit_pack(specs)
    result=run_decision_replay(scenario_bundle,fold["models"],payoff,specs,account,run_policy,(),{},research_policy(),_seasonal_handle(fold),
                               fold["reference"],lock_hash,None,split.test_start_nano,split.test_end_nano,
                               exit_specs=_es,exit_bindings=_bs)
    return result, rules


def score_strategy_group(group:dict,economic:Mapping,result,spec) -> dict:
    from .metrics import block_bootstrap
    rows=[r for r in economic["episode_records"] if r["strategy_id"]==group["strategy_id"] and r["product"]==group["product"]
          and (group["contract_id"] is None or r["contract_id"]==group["contract_id"])
          and (r["mode"],r["horizon_unit"],r["horizon_value"])==(group["mode"],group["horizon_unit"],group["horizon_value"])]
    pnls=[Decimal(r["net_pnl_cny"]) for r in rows]
    blocks={r["block_id"] for r in rows}
    lower,_=block_bootstrap([float(p) for p in pnls],[r["block_id"] for r in rows],result.decisions[0].seed if result.decisions else 7)
    losses=sorted((max(Decimal(0),-p) for p in pnls),reverse=True)
    mass=Decimal(len(losses))*Decimal('.05');remaining=mass;total=Decimal(0)
    for loss in losses:
        weight=min(Decimal(1),remaining);total+=loss*weight;remaining-=weight
        if remaining<=0:break
    cvar=total/mass if mass else None
    gates=[g for d in result.decisions for p,g in zip(d.proposals,d.gates) if p.strategy_id==group["strategy_id"]
           and (group["contract_id"] is None or p.contract_id==group["contract_id"])]
    admitted=sum(g.eligible for g in gates)
    reasons=[]
    if economic["replay_summary"]["status"]!="COMPLETE":reasons.append("REPLAY_INCOMPLETE")
    if len(rows)!=economic["episodes"] and group["contract_id"] is None and result.decisions and result.decisions[0].mode=="FIXED":
        reasons.append("EPISODE_SCOPE_OR_METADATA_MISSING")
    minimum=(200,60) if spec.family=="MARTINGALE" else (100,30)
    sufficient=len(rows)>=minimum[0] and len(blocks)>=minimum[1]
    if not sufficient:reasons.append("INSUFFICIENT_EPISODES_OR_BLOCKS")
    failure=(lower is not None and lower<=0) or (cvar is not None and rows and cvar>min(Decimal(r["budget_cny"]) for r in rows))
    stress=max((r["stress_loss_ratio"] for r in rows),default=0.0)
    if stress>.02:failure=True;reasons.append("STRESS_LOSS_EXCEEDED")
    if Decimal(economic["margin_shortfall_cny"])>0:failure=True;reasons.append("MARGIN_SHORTFALL")
    status="FAIL" if sufficient and failure else "INSUFFICIENT" if reasons or not sufficient or spec.family=="WAIT" else "PASS"
    return {**group,"episodes":len(rows),"blocks":len(blocks),"net_pnl_cny":str(sum(pnls,Decimal(0))),
            "conservative_ev_cny":str(Decimal(str(lower))) if lower is not None else None,"cvar95_cny":str(cvar) if cvar is not None else None,
            "admission_rate":admitted/len(gates) if gates else 0.0,"no_trade_rate":1-admitted/len(gates) if gates else 0.0,
            "maximum_stress_loss_ratio":stress,"margin_shortfall_cny":economic["margin_shortfall_cny"],
            "peak_margin_cny":economic["peak_margin_cny"],"max_inventory":economic["max_inventory"],
            "max_drawdown_ratio":economic["max_drawdown_ratio"],"longest_recovery_days":economic["longest_recovery_days"],
            "status":status,"reasons":sorted(set(reasons))}


def _execute_validation(bundle: FrozenInput, config: Mapping[str, JsonValue], output: Path, mode: str) -> Mapping[str, JsonValue]:
    from dataclasses import replace
    import csv,io
    from ak_trading.v2 import CONTRACT_VERSION,DOCUMENT_VERSION
    from ak_trading.v2.artifacts import verify_contract_lock
    from ak_trading.v2.schemas import encode_record_file
    from ak_trading.v2.strategies.registry import strategy_specs_v2
    from .reports import empty_registered_metrics,build_readiness,write_validation_report
    from .metrics import evaluate_forecasts,forecast_group_key
    if mode not in ("smoke","walk-forward","final-holdout","reproduce","shadow-replay"):
        raise ExperimentError("unknown_validation_mode")
    if output.exists():raise ExperimentError("output_directory_exists")
    splits,policies,scope=parse_config(config)
    contract_lock=Path(config["contract_lock_path"])
    if not contract_lock.is_absolute():contract_lock=PROJECT_ROOT/contract_lock
    if not contract_lock.is_file():raise StageIncompleteError("M1_CONTRACT_LOCK_NOT_PUBLISHED")
    # Engineering dependency gate (arbitration 2026-09-17 §3.1): a missing
    # payoff producer fails here — before lock-content verification, before
    # registration, before any final-holdout consumption and before any
    # expensive ``_fit_fold``. Ordered after the lock-publication check so an
    # unpublished lock is still reported as the M1 stage failure it is.
    verify_payoff_training_dependencies()
    verify_contract_lock(contract_lock,PROJECT_ROOT)
    if parse_json_bytes(contract_lock.read_bytes())["document_version"] != DOCUMENT_VERSION:
        raise ExperimentError("contract_document_version_mismatch")
    if not config["synthetic"] and any(s.evidence_status!="VERIFIED" for s in bundle.sessions):
        raise StageIncompleteError("HISTORICAL_CALENDAR_COVERAGE_NOT_VERIFIED")
    if scope.calendar_sha256 != digest(bundle.sessions):raise ExperimentError("calendar_hash_mismatch")
    if not set(config["contract_ids"]) <= {i.contract_id for i in bundle.instruments}:raise ExperimentError("contract_not_in_bundle")
    specs=tuple(s for mode_ in ("MINUTE","DAILY") for s in strategy_specs_v2(mode_) if s.strategy_id in config["strategy_ids"])
    if {s.strategy_id for s in specs} != set(config["strategy_ids"]):raise ExperimentError("unknown_strategy_id")
    # E-P1 RULING 2.2/4.1: the complete sampling plan set is frozen here —
    # before ``code_hashes``, before formal registration, before holdout
    # consumption and before any expensive ``_fit_fold``. A schema v2.1.3
    # config must declare the identical plan-set hash; legacy v2.1.0 configs
    # still get the real constructed set registered on the ExperimentSpec.
    plan_sets,actual_plan_hash,declared_plan_hash=_frozen_payoff_training_plans(bundle,config,splits,specs)
    hashes=code_hashes()
    from ak_trading.v2.strategies.registry import select_exit_binding
    from ak_trading.v2.decision.pipeline import exit_registry_hash_for
    from ak_trading.v2.exits.registry import exit_registry_specs
    _by_id={x.exit_policy_id:x for x in (*exit_registry_specs("MINUTE"),*exit_registry_specs("DAILY"))}
    all_exit_specs=tuple(sorted(_by_id.values(),key=lambda s:s.exit_policy_id))
    _by_hash={select_exit_binding(s).binding_sha256:select_exit_binding(s) for s in specs}
    bindings=tuple(sorted(_by_hash.values(),key=lambda b:b.binding_sha256))
    registry_hash=exit_registry_hash_for(all_exit_specs)
    experiment=ExperimentSpec(config["run_id"],config["run_id"],(bundle.manifest_sha256,),hashlib.sha256(contract_lock.read_bytes()).hexdigest(),
                              registry_hash,bindings,
                              digest(hashes),digest(config),splits,config["holdout_id"],scope,policies,tuple(config["required_group_keys"]),
                              config["seed"],config["research_policy_version"],config["macro_policy_version"],config["macro_mode"],config["evaluation_as_of_nano"],
                              declared_plan_hash if declared_plan_hash is not None else actual_plan_hash)
    if mode!="reproduce":_check_registration_available(experiment,RUN_ROOT/"experiment-registry.jsonl")
    if config["macro_mode"] == "HISTORICAL_PIT" and any(i.quality.temporal_basis!="observed" for i in bundle.macro_items):
        raise ExperimentError("historical_macro_PIT_missing")
    if not config["synthetic"]:
        for s in splits:
            days=calendar_days(bundle.sessions)
            if any(sum(lo>=start and hi<=end for _,lo,hi in days)<minimum for start,end,minimum in (
                (s.train_start_nano,s.train_end_nano,252),(s.calibration_start_nano,s.calibration_end_nano,60),(s.test_start_nano,s.test_end_nano,60))):
                raise StageIncompleteError("REGISTERED_WINDOW_COVERAGE_INSUFFICIENT")
    token=None
    if mode=="final-holdout":
        if config["synthetic"]:raise ExperimentError("synthetic_cannot_be_real_final_holdout")
        validate_holdout_calendar(scope,bundle.sessions,config["evaluation_as_of_nano"])
        if not (RUN_ROOT/"holdouts/consumed-intervals.jsonl").exists():raise StageIncompleteError("PREVIOUSLY_OBSERVED_INTERVAL_HISTORY_UNKNOWN")
        token=reserve_final_holdout(experiment,RUN_ROOT/"holdouts")
        begin_final_holdout(experiment,token,RUN_ROOT/"holdouts")
    try:
        folds=[]
        for s in splits:
            if s.final_holdout != (mode=="final-holdout") and mode!="reproduce":continue
            folds.append((s,_fit_fold(bundle,config,s,specs)))
        from ak_trading.v2.decision.payoffs import fit_path_payoff_model
        from ak_trading.v2.decision.pipeline import model_hashes_for
        fitted=[(s,f) for s,f in folds if f["models"] is not None]
        if not fitted:
            raise StageIncompleteError("TRAIN_OR_CALIBRATION_INSUFFICIENT_NO_MODEL_ARTIFACT")
        first_split,first_fold=fitted[0]
        first_payoff,first_training_payloads,first_training_exclusions=_fit_training_payoffs(bundle,config,first_split,specs,experiment.contract_lock_sha256,plans=plan_sets.get(first_split.split_id))
        payoffs_by_split={first_split.split_id:(first_payoff,first_training_payloads,first_training_exclusions)}
        dependencies=model_hashes_for(first_fold["models"],first_payoff,first_fold["reference"],_seasonal_handle(first_fold))
        metrics=empty_registered_metrics(experiment,dependencies)
        assignments=[];artifacts={};blockers=["REAL_FEED_NOT_ACCEPTED","SHADOW_COHORT_NOT_STARTED"]
        for s,fold in folds:
            assignments.extend(fold["assignments"])
            blockers.extend(fold["exclusions"])
            for reason in fold["exclusions"]:
                code="PUBLICATION_FAILED" if reason.startswith("PUBLICATION_FAILED:") else reason
                metrics["exclusions"][code]=metrics["exclusions"].get(code,0)+1
            prefix="folds/"+s.split_id+"/"
            for name,kind,schema,records in (("features","FeatureSnapshot","ak_probability_features_v2.1.0",fold["snapshots"]),
                                           ("requests","ForecastRequest","ak_probability_requests_v2.1.0",fold["requests"]),
                                           ("labels","PathLabel","ak_probability_labels_v2.1.0",fold["labels"]),
                                           ("forecasts","MarketForecast","ak_probability_forecasts_v2.1.0",fold["forecasts"])):
                artifacts[prefix+name+".json"]=encode_record_file(kind,schema,records)
            artifacts[prefix+"feature_availability.json"]=canonical_json_bytes({"scope":"REGISTERED_FULL_GRID",
                "counts":dict(Counter(f"d{f.duration_seconds}:{reason}" for f in fold["snapshots"] for value in f.values
                    if value.factor_key.startswith("BASE:opening_gap_points:") and value.quality.status!="READY" for reason in value.quality.reasons))})
            artifacts[prefix+"regime_reference.json"]=canonical_json_bytes({"schema_version":"ak_probability_regime_reference_v2.1.0","reference":content(fold["reference"])})
            artifacts[prefix+"seasonality.json"]=canonical_json_bytes({"schema_version":"ak_probability_seasonality_v2.1.1","model":content(fold["seasonal"])})
            if fold["models"] is None:continue
            artifacts[prefix+"models.json"]=canonical_json_bytes({"schema_version":"ak_probability_models_v2.1.0","model_set":content(fold["models"])})
            labels={l.request_id:l for l in fold["labels"]}
            scoring=evaluate_forecasts(fold["forecasts"],tuple(labels[f.request_id] for f in fold["forecasts"]),
                                       {f.request_id:str(f.trading_day) for f in fold["forecasts"]},experiment.seed)
            artifacts[prefix+"forecast_diagnostics.json"]=canonical_json_bytes(scoring)
            for code,count in scoring["exclusions"].items():metrics["exclusions"][code]=metrics["exclusions"].get(code,0)+count
            if s.split_id not in payoffs_by_split:
                payoffs_by_split[s.split_id]=_fit_training_payoffs(bundle,config,s,specs,experiment.contract_lock_sha256,plans=plan_sets.get(s.split_id))
            payoff,training_payloads,training_exclusions=payoffs_by_split[s.split_id]
            for name,raw in training_payloads.items():artifacts[prefix+"training_paths/"+name]=raw
            blockers.extend(training_exclusions)
            hashes_for_fold=model_hashes_for(fold["models"],payoff,fold["reference"],_seasonal_handle(fold))
            bundle_hash=digest(hashes_for_fold);metrics["model_bundles"][bundle_hash]=dict(hashes_for_fold)
            for g in metrics["forecast_groups"]:
                if g["split_id"]==s.split_id:g["model_bundle_sha256"]=bundle_hash
            for group in scoring["forecast_groups"]:
                group={**group,"split_id":s.split_id,"model_bundle_sha256":bundle_hash}
                group["group_key"]=forecast_group_key(group)
                index=next((i for i,g in enumerate(metrics["forecast_groups"]) if g["group_key"]==group["group_key"]),None)
                if index is not None:metrics["forecast_groups"][index]=group
            base_results={}
            for run_policy in sorted(policies,key=lambda p:(p.cost_scenario!="BASE",COST_SCENARIOS.index(p.cost_scenario),digest(p))):
                ph=digest(run_policy)
                if run_policy.cost_scenario in ("GAP","NO_STOP"):
                    base_policy=replace(run_policy,cost_scenario="BASE")
                    base_result=base_results.get(digest(base_policy))
                    if base_result is None:raise ExperimentError("stress_requires_registered_BASE_policy")
                    stress,paths=evaluate_stress_cohort(base_result,bundle,run_policy.cost_scenario)
                    artifacts[prefix+"stress/"+ph+".json"]=canonical_json_bytes({"scenario":run_policy.cost_scenario,"records":content(paths),"summary":stress})
                    base_groups=[g for g in metrics["strategy_groups"] if g["split_id"]==s.split_id and g["run_policy_sha256"]==digest(base_policy)]
                    for g in metrics["strategy_groups"]:
                        if g["split_id"]!=s.split_id or g["run_policy_sha256"]!=ph:continue
                        base=next((x for x in base_groups if (x["strategy_id"],x["contract_id"],x["horizon_unit"],x["horizon_value"])==(g["strategy_id"],g["contract_id"],g["horizon_unit"],g["horizon_value"])),None)
                        if base:
                            for key in ("episodes","blocks","admission_rate","no_trade_rate","max_inventory"):
                                g[key]=base[key]
                        g.update(stress,model_bundle_sha256=bundle_hash)
                        g["conservative_ev_cny"]=None;g["cvar95_cny"]=None
                        g["net_pnl_cny"]=str(-sum((Decimal(r["path"]["loss_cny"]) for r in paths),Decimal(0)))
                    continue
                result,scenario_rules_=_replay_fold(bundle,s,fold,payoff,specs,run_policy,experiment.contract_lock_sha256,Decimal(config["initial_cash_cny"]))
                if run_policy.cost_scenario=="BASE":base_results[ph]=result
                artifacts[prefix+"accounts/"+ph+"/rules.json"]=encode_record_file("TradingRule","ak_probability_rules_v2.1.0",scenario_rules_)
                from ak_trading.v2.decision.replay import replay_payloads
                from .metrics import evaluate_replay
                episode_days={fill.episode_id:str(fill.trading_day) for fill in reversed(result.fills) if fill.offset=="OPEN"}
                economic=evaluate_replay(result,Decimal(config["initial_cash_cny"]),episode_days,experiment.seed,rules=scenario_rules_)
                ph=digest(run_policy)
                for name,raw in replay_payloads(result,payoff).items():artifacts[prefix+"accounts/"+ph+"/"+name]=raw
                metrics["replay_summaries"].append({**economic["replay_summary"],"split_id":s.split_id,
                                                    "run_policy_sha256":ph,"model_bundle_sha256":bundle_hash})
                for index,g in enumerate(metrics["strategy_groups"]):
                    if g["split_id"]!=s.split_id or g["run_policy_sha256"]!=ph:continue
                    g["model_bundle_sha256"]=bundle_hash;g["account_id"]=economic["replay_summary"]["account_id"]
                    spec=next(x for x in specs if x.strategy_id==g["strategy_id"])
                    metrics["strategy_groups"][index]=score_strategy_group(g,economic,result,spec)
                if result.status!="COMPLETE":blockers.append("REPLAY_INCOMPLETE:"+ph)
        if any(not model.observations for model,_,_ in payoffs_by_split.values()):
            blockers.append("PAYOFF_TRAINING_EPISODES_NOT_AVAILABLE")
        products=[{"product":i.product,"contract_ids":[i.contract_id],"minute_history":"SYNTHETIC" if config["synthetic"] else "FROZEN",
                   "daily_history":"SYNTHETIC" if config["synthetic"] else "FROZEN","calendar":"SCENARIO" if config["synthetic"] else "FROZEN",
                   "cost_rules":"SCENARIO" if config["synthetic"] else "FROZEN","realtime":"NOT_ACCEPTED","reasons":[]}
                  for i in bundle.instruments if i.contract_id in config["contract_ids"]]
        known_stage_failures=any(reason.startswith(("REPLAY_INCOMPLETE:","TRAIN_OR_CALIBRATION_INSUFFICIENT")) for reason in blockers)
        engineering="FAIL" if known_stage_failures else "PASS"
        if "PAYOFF_TRAINING_EPISODES_NOT_AVAILABLE" in blockers:engineering="FAIL"
        if not config["synthetic"]:blockers.append("CALENDAR_OPEN_CLOSED_COVERAGE_EVIDENCE_REVIEW_REQUIRED")
        readiness=build_readiness(metrics,products,specs,engineering,"SHADOW_PENDING" if mode=="shadow-replay" else "OFFLINE_ONLY",tuple(sorted(set(blockers))))
        report_hash=write_validation_report(output,experiment,metrics,readiness)
        stream=io.StringIO(newline="");writer=csv.writer(stream,lineterminator="\n")
        writer.writerow(["sample_id","split_id","partition","reasons"])
        for a in assignments:writer.writerow([a.sample_id,a.split_id,a.partition,canonical_json_bytes(list(a.reasons)).decode().strip()])
        artifacts["splits.csv"]=stream.getvalue().encode()
        artifacts["model_activations.json"]=canonical_json_bytes({"schema_version":"ak_probability_model_activations_v2.1.3","records":[]})
        artifacts["macro_comparison.json"]=canonical_json_bytes({"schema_version":"ak_probability_macro_comparison_v2.1.0","mode":experiment.macro_mode,
            "paired_opportunities":0,"paired_blocks":0,"price_only":{},"with_macro":{},"delta":{},"unmatured":0,"reasons":["PAIRED_ECONOMIC_COHORT_NOT_AVAILABLE"]})
        for name,payload in artifacts.items():atomic_write(output/name,payload)
        files={p.relative_to(output).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in output.rglob("*") if p.is_file()}
        manifest={"schema_version":"ak_probability_validation_run_v2.1.0","contract_version":CONTRACT_VERSION,"created_at_nano":experiment.evaluation_as_of_nano,
                  "files":files,"provenance":{"node":"M5","dataset_kind":"SYNTHETIC" if config["synthetic"] else "HISTORICAL_RECONSTRUCTED",
                   "input_hashes":[bundle.manifest_sha256,experiment.contract_lock_sha256],"code_hashes":hashes,"parameter_hashes":{"config":digest(config)},
                   "model_hashes":{},"policy_versions":{"research":experiment.research_policy_version,"macro":experiment.macro_policy_version},"seed":experiment.seed,
                   "limitations":readiness["blockers"],"previous_segment_manifest_sha256":None,"recovery_source_sha256":None,"ignored_tail_sha256":None}}
        atomic_write(output/"manifest.json",canonical_json_bytes(manifest))
        if token:consume_final_holdout(experiment,token,RUN_ROOT/"holdouts",report_hash)
        # Failed fits, report writes and engineering failures are diagnostics,
        # never registered experiments. Keep the existing holdout consumption
        # boundary and commit registration only after the run artifacts seal.
        if mode!="reproduce" and engineering=="PASS":
            register_experiment(experiment,RUN_ROOT/"experiment-registry.jsonl")
        return readiness
    except BaseException as error:
        if token:fail_final_holdout(experiment,token,RUN_ROOT/"holdouts",type(error).__name__)
        raise
