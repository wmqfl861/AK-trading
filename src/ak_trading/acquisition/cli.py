"""C4 CLI: plan / verify-package / run / resume (H01, H15 gate ordering).

Default mode is offline: ``plan`` and ``verify-package`` create no SDK object
and no database engine. ``run``/``resume`` validate, in order and fail-closed
BEFORE constructing any adapter or engine:

1. authorization structure (approver must be HUMAN_USER; the program never
   self-signs), expiry, and session window;
2. hash binding: policy / review seal / plan / code manifest all match the
   authorization scope, and the review seal verdict is PASS covering this
   code manifest;
3. authorization budget caps within policy;
4. H12 isolation: protection list, output-root resolution, junction refusal,
   disk floor, RSS cap, below-normal priority, health signals;
5. session validity (including the frozen rule that Saturday execution is
   not authorized in this policy revision);
6. credential *presence* by variable name only (values never read or logged).

Only after every gate passes are the real adapter factory and the project
engine factory reachable. As of this delivery no review seal exists, so
``run`` always refuses - by design (H15: development then independent review
then user confirmation).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from . import artifacts as artifact_mod
from . import storage as storage_mod
from . import tq_adapter as tq
from .calendar import CalendarBook
from .capture import CaptureEngine, CaptureOutcome, CaptureRequest, classify_frame
from .contracts import (
    ContractError,
    CoverageResult,
    CoverageStatus,
    NANOSECOND,
    Policy,
    RunAuthorization,
    SHANGHAI_TZ,
    TaskState,
    TargetKind,
    canonical_sha256,
    nano_to_iso,
    sha256_bytes,
    sha256_file,
)
from .identity import (
    Universe,
    expand_denominator,
)
from .scheduler import (
    BACKOFF_LADDER_SECONDS,
    BudgetExhausted,
    BudgetLedger,
    HealthCheckDescriptor,
    IsolationGuard,
    IsolationRefused,
    ProtectionList,
    RateLimitCoordinator,
    RetryGate,
    RunStateStore,
    TaskLease,
    build_execution_queue,
    canonical_task_key,
    classify_exception,
)

EXIT_OK = 0
EXIT_GATE_REFUSED = 2
EXIT_BLOCKED = 3
EXIT_FAILURE = 4
EXIT_DEFERRED_BUDGET = 5

_CREDENTIAL_SCAN_PATTERNS = (
    "TQ_PASS=",
    "TQ_USER=",
    "AK_DATABASE_URL=",
    "postgresql://",
    "postgres://",
)


# ---------------------------------------------------------------------------
# Code manifest (self-hash binding for run authorization)
# ---------------------------------------------------------------------------

def compute_code_manifest(package_root: Path) -> dict[str, Any]:
    root = Path(package_root).resolve()
    files = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        files.append({"path": rel, "sha256": sha256_file(path)})
    payload = {"files": files, "kind": "ak_acquisition_w2_code_manifest_v1"}
    payload["manifest_sha256"] = canonical_sha256(files)
    return payload


def _package_root() -> Path:
    return Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

def cmd_plan(args: argparse.Namespace) -> int:
    policy = Policy.from_file(Path(args.policy)).validate()
    inventory_payload = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    universe = Universe.from_mapping(
        inventory_payload,
        provenance=str(Path(args.inventory).resolve()),
    )
    universe.check_baseline_conservation()
    expansion = expand_denominator(universe)

    calendar_book = CalendarBook([])  # evidence optional at plan time
    calendar_sha = calendar_book.evidence_hash()
    if getattr(args, "calendar_evidence", None):
        evidence_payload = json.loads(
            Path(args.calendar_evidence).read_text(encoding="utf-8")
        )
        calendar_book = CalendarBook.from_mapping(evidence_payload)
        calendar_sha = calendar_book.evidence_hash()

    inventory_sha = sha256_file(Path(args.inventory))
    plan = {
        "plan_id": f"acq-w2-plan-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}",
        "created_at_nano": str(int(time.time() * NANOSECOND)),
        "policy_path": str(Path(args.policy).resolve()),
        "policy_sha256": policy.sha256,
        "inventory_sha256": inventory_sha,
        "universe_version": expansion.universe_version,
        "calendar_evidence_sha256": calendar_sha,
        "code_manifest_sha256": compute_code_manifest(_package_root())["manifest_sha256"],
        "baseline_products": expansion.product_count_baseline,
        "denominator": expansion.canonical(),
        "synthetic": bool(inventory_payload.get("synthetic", False)),
        "tasks": [],
        "rollup": {
            "product_duration_cells_baseline": expansion.product_count_baseline * 2,
            "cells": {},
        },
    }

    statuses: dict[str, int] = {status.value: 0 for status in CoverageStatus}
    product_priorities = {
        p.product_key: p.priority for p in universe.products
    }
    product_codes = {
        p.product_key: p.source_product_code.upper() for p in universe.products
    }
    for product in sorted({p.product_key for p in universe.products}):
        code = product_codes.get(product, product.rsplit(".", 1)[-1].upper())
        for duration in policy.durations:
            key = f"{product}|{duration}"
            symbols = list(expansion.concrete_by_product.get(product, ()))
            references = [
                s
                for s in expansion.reference_only
                if s.upper().rsplit(".", 1)[-1] == code
            ]
            plan["tasks"].append(
                {
                    "logical_task_key": key,
                    "product_key": product,
                    "priority": product_priorities.get(product, "P5"),
                    "duration_seconds": duration,
                    "concrete_symbols": symbols,
                    "reference_only_symbols": references,
                    "target_kind": [
                        TargetKind.A.value,
                        TargetKind.B.value,
                    ],
                    "state": TaskState.PLANNED.value,
                    "coverage_status": CoverageStatus.NOT_RUN.value,
                }
            )
            statuses["NOT_RUN"] += 1
    plan["rollup"]["cells"] = statuses

    output_dir = Path(args.output)
    if output_dir.exists() and any(output_dir.iterdir()):
        print(f"plan output directory not empty: {output_dir}", file=sys.stderr)
        return EXIT_FAILURE
    output_dir.mkdir(parents=True, exist_ok=True)
    plan_body = json.dumps(plan, sort_keys=True, indent=2, ensure_ascii=False).encode(
        "utf-8"
    )
    path, digest = artifact_mod.atomic_publish_bytes(output_dir, "plan.json", plan_body)
    manifest = {
        "files": {"plan.json": digest},
        "plan_sha256": digest,
    }
    artifact_mod.atomic_publish_bytes(
        output_dir,
        "plan-manifest.json",
        json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8"),
    )
    print(f"plan written: {path} (sha256 {digest})")
    return EXIT_OK


# ---------------------------------------------------------------------------
# verify-package
# ---------------------------------------------------------------------------

def cmd_verify_package(args: argparse.Namespace) -> int:
    package_dir = Path(args.package)
    if not package_dir.is_dir():
        print(f"package directory missing: {package_dir}", file=sys.stderr)
        return EXIT_FAILURE
    problems: list[str] = []
    products_root = package_dir / "products"
    product_dirs = sorted(p for p in products_root.iterdir()) if products_root.is_dir() else []

    coverage_counts: dict[str, int] = {s.value: 0 for s in CoverageStatus}
    baseline_products = 0

    for product_dir in product_dirs:
        name = product_dir.name
        for required in (
            "identity.json",
            "raw-manifest.json",
            "rows.jsonl",
            "reconciliation.json",
            "coverage.json",
            "gaps.json",
            "acceptance.json",
        ):
            if not (product_dir / required).exists() and not list(
                product_dir.glob(f"*{required}*")
            ):
                problems.append(f"{name}: missing {required}")
        raw_manifest_path = product_dir / "raw-manifest.json"
        if raw_manifest_path.exists():
            try:
                manifest = json.loads(raw_manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                problems.append(f"{name}: raw-manifest.json is not valid JSON")
                manifest = None
            if manifest is not None:
                for entry in manifest.get("entries", []):
                    raw_path = Path(entry["raw_path"])
                    if not raw_path.is_file():
                        problems.append(f"{name}: raw file missing {raw_path}")
                        continue
                    digest = sha256_file(raw_path)
                    if digest != entry["raw_sha256"]:
                        problems.append(f"{name}: raw hash mismatch {raw_path}")
        coverage_path = product_dir / "coverage.json"
        if coverage_path.exists():
            try:
                coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
                for cell in coverage.get("cells", []):
                    status = cell.get("coverage_status")
                    if status not in coverage_counts:
                        problems.append(f"{name}: invalid coverage status {status}")
                    else:
                        coverage_counts[status] += 1
                if coverage.get("is_baseline"):
                    baseline_products += 1
            except json.JSONDecodeError:
                problems.append(f"{name}: coverage.json is not valid JSON")

    run_summary_path = package_dir / "run-summary.json"
    if run_summary_path.exists():
        summary = json.loads(run_summary_path.read_text(encoding="utf-8"))
        if summary.get("baseline_product_total") == 81:
            if baseline_products != 0 and baseline_products != 81:
                problems.append(
                    f"baseline conservation broken: {baseline_products} != 81"
                )

    # Credential material scan: artifacts must never contain secrets.
    for path in package_dir.rglob("*.json"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in _CREDENTIAL_SCAN_PATTERNS:
            if pattern in text:
                problems.append(f"credential-like content in {path.name}: {pattern}")

    if problems:
        for problem in problems:
            print(f"PROBLEM: {problem}", file=sys.stderr)
        return EXIT_FAILURE
    print(
        "package verified: "
        + ", ".join(f"{k}={v}" for k, v in sorted(coverage_counts.items()))
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# run / resume gates
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _now_nano() -> int:
    return int(time.time() * NANOSECOND)


def run_gates(
    *,
    plan_path: Path,
    authorization_path: Path,
    review_seal_path: Path,
    now_nano: int,
) -> tuple[Optional[RunAuthorization], list[str]]:
    """All fail-closed checks that must pass BEFORE any adapter/engine."""
    problems: list[str] = []
    try:
        plan = _load_json(plan_path)
        auth_payload = _load_json(authorization_path)
        seal = _load_json(review_seal_path)
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"input files unreadable: {type(exc).__name__}"]

    seal_digest = sha256_file(review_seal_path)
    plan_digest = sha256_file(plan_path)

    if seal.get("verdict") != "PASS":
        problems.append("review seal is not PASS; run refused (H15)")
    if seal.get("scope", {}).get("covers_package") != "ak_acquisition_w2":
        problems.append("review seal scope does not cover this package")

    policy_path = plan.get("policy_path")
    if not policy_path or not Path(policy_path).exists():
        return None, problems + ["plan does not resolve its policy file"]
    try:
        policy = Policy.from_file(Path(policy_path)).validate()
    except ContractError as exc:
        return None, problems + [f"policy validation failed: {exc}"]
    if policy.sha256 != plan.get("policy_sha256"):
        problems.append("plan policy hash mismatch")

    try:
        auth = _authorization_from_payload(auth_payload)
    except (ContractError, KeyError, ValueError) as exc:
        return None, problems + [f"authorization invalid: {exc}"]

    if auth.scope_policy_sha256 != policy.sha256:
        problems.append("authorization policy hash mismatch")
    if auth.scope_review_seal_sha256 != seal_digest:
        problems.append("authorization review-seal hash mismatch")
    if auth.scope_plan_sha256 != plan_digest:
        problems.append("authorization plan hash mismatch")
    manifest = compute_code_manifest(_package_root())
    if auth.scope_code_manifest_sha256 != manifest["manifest_sha256"]:
        problems.append("authorization code-manifest hash mismatch")
    seal_manifest = seal.get("scope", {}).get("code_manifest_sha256")
    if seal_manifest is not None and seal_manifest != manifest["manifest_sha256"]:
        problems.append("review seal code-manifest hash mismatch")
    problems.extend(auth.within_budget(policy))
    if auth.expired(now_nano):
        problems.append("authorization expired")

    # Session window + frozen Saturday rule (H04: saturday_execution_authorized_now=false).
    if not (
        auth.session_window_start_nano
        <= now_nano
        < auth.session_window_end_nano
    ):
        problems.append("current instant outside authorized session window")
    from datetime import datetime

    moment = datetime.fromtimestamp(now_nano // NANOSECOND, tz=SHANGHAI_TZ)
    if moment.weekday() == 5:  # Saturday
        problems.append(
            "policy forbids Saturday execution in this revision (H04)"
        )

    # Credential presence by NAME only (H01: values never read here).
    missing = [
        name
        for name in (tq.TQ_USER_ENV, tq.TQ_PASS_ENV)
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        problems.append(f"credential env(s) not set: {', '.join(missing)}")
    if not os.environ.get("AK_DATABASE_URL", "").strip():
        problems.append("credential env not set: AK_DATABASE_URL")

    if problems:
        return None, problems
    return auth, []


def _authorization_from_payload(payload: Mapping[str, Any]) -> RunAuthorization:
    from .contracts import iso_instant_to_nano

    return RunAuthorization(
        confirmation_ref=str(payload["confirmation_ref"]),
        confirmed_at_nano=int(payload["confirmed_at_nano"]),
        approver=str(payload["approver"]),
        scope_policy_sha256=str(payload["scope_policy_sha256"]),
        scope_review_seal_sha256=str(payload["scope_review_seal_sha256"]),
        scope_code_manifest_sha256=str(payload["scope_code_manifest_sha256"]),
        scope_plan_sha256=str(payload["scope_plan_sha256"]),
        output_root=str(payload["output_root"]),
        db_target_non_sensitive=str(payload["db_target_non_sensitive"]),
        session_window_start_nano=iso_instant_to_nano(
            payload["session_window_start"]
        ),
        session_window_end_nano=iso_instant_to_nano(payload["session_window_end"]),
        max_serial_attempts=int(payload["max_serial_attempts"]),
        max_wall_seconds=int(payload["max_wall_seconds"]),
        allow_subsequent_batches=bool(payload["allow_subsequent_batches"]),
        protected_paths=tuple(str(p) for p in payload["protected_paths"]),
        protected_db_objects=tuple(str(o) for o in payload["protected_db_objects"]),
        expires_at_nano=(
            iso_instant_to_nano(payload["expires_at"])
            if payload.get("expires_at")
            else None
        ),
    )


def cmd_run(args: argparse.Namespace, *, resume: bool = False) -> int:
    plan_path = Path(args.plan)
    now = _now_nano()
    auth, problems = run_gates(
        plan_path=plan_path,
        authorization_path=Path(args.authorization),
        review_seal_path=Path(args.review_seal),
        now_nano=now,
    )
    if auth is None:
        for problem in problems:
            print(f"GATE REFUSED: {problem}", file=sys.stderr)
        return EXIT_GATE_REFUSED

    plan = _load_json(plan_path)
    policy = Policy.from_file(Path(plan["policy_path"])).validate()

    # H12 isolation checks before anything is constructed.
    protection = ProtectionList.from_authorization(auth)
    guard = IsolationGuard(
        protection,
        minimum_free_bytes=policy.budget["free_space_floor_bytes_after_reservations"],
        batch_peak_bytes=policy.budget[
            "minimum_batch_peak_reservation_bytes_including_database_wal"
        ],
    )
    try:
        output_root = guard.validate_output_root(Path(auth.output_root))
        guard.check_disk(output_root)
        guard.check_rss_bytes(policy.budget["process_rss_bytes_max"])
    except IsolationRefused as exc:
        print(f"GATE REFUSED: {exc}", file=sys.stderr)
        return EXIT_GATE_REFUSED
    health = guard.check_health_signals(
        HealthCheckDescriptor(**item)
        for item in _load_json(Path(args.authorization)).get("health_checks", [])
    )
    if health:
        for item in health:
            print(f"GATE REFUSED: {item}", file=sys.stderr)
        return EXIT_GATE_REFUSED
    priority_status = guard.set_below_normal_priority()

    def adapter_factory():
        return tq.RealTqApiSession()

    def engine_factory():
        url = os.environ["AK_DATABASE_URL"]
        if storage_mod.safe_database_location(url) != auth.db_target_non_sensitive:
            raise ContractError(
                "AK_DATABASE_URL target does not match the authorized "
                "non-sensitive DB identity; refusing (H01)"
            )
        return storage_mod.build_project_engine(url)

    return execute_run(
        plan=plan,
        policy=policy,
        auth=auth,
        output_root=output_root,
        adapter_factory=adapter_factory,
        engine_factory=engine_factory,
        clock=lambda: _now_nano(),
        resume=resume,
        priority_status=priority_status,
    )


# ---------------------------------------------------------------------------
# Execution pipeline (offline-injectable for SYNTHETIC end-to-end tests)
# ---------------------------------------------------------------------------

def execute_run(
    *,
    plan: Mapping[str, Any],
    policy: Policy,
    auth: RunAuthorization,
    output_root: Path,
    adapter_factory: Callable[[], Any],
    engine_factory: Callable[[], Any],
    clock: Callable[[], int],
    resume: bool = False,
    priority_status: str = "NOT_SET_OFFLINE",
) -> int:
    run_dir = Path(output_root)
    run_dir.mkdir(parents=True, exist_ok=True)
    lease = TaskLease(run_dir, holder_name="acq_w2_collector")
    if not lease.acquire(os.getpid(), clock()):
        print("GATE REFUSED: lease unavailable or holder not confirmed dead", file=sys.stderr)
        return EXIT_GATE_REFUSED
    ledger = BudgetLedger(policy)
    engine = None
    adapter = None
    task_results: list[dict[str, Any]] = []
    product_state: dict[str, dict[str, Any]] = {}
    exit_code = EXIT_OK
    try:
        try:
            # Always load under the lease. `run` and `resume` have the same
            # immutable plan scope; neither may reset an existing directory.
            store = RunStateStore(run_dir)
            store.bind_plan(plan)
            ledger.check_rss_bytes()
        except (ContractError, IsolationRefused, OSError, ValueError, TypeError) as exc:
            print(f"GATE REFUSED: {exc}", file=sys.stderr)
            return EXIT_GATE_REFUSED
        retry = RetryGate(store, clock)
        rate = RateLimitCoordinator()
        queue = _plan_queue(plan)
        calendar_book = CalendarBook([])
        evidence_path = plan.get("calendar_evidence_path")
        if evidence_path and Path(evidence_path).exists():
            calendar_book = CalendarBook.from_mapping(_load_json(Path(evidence_path)))

        def before_serial_attempt(request: CaptureRequest) -> None:
            lease.heartbeat(os.getpid(), clock())
            ledger.check_rss_bytes()
            ledger.charge_serial_attempt()
            retry.start_attempt(canonical_task_key(request.source_symbol, request.duration_seconds))

        lease.heartbeat(os.getpid(), clock())
        ledger.charge_connection_start()
        adapter = adapter_factory()
        lease.heartbeat(os.getpid(), clock())
        engine = engine_factory()
        storage_mod.ensure_schema_and_tables(engine)
        storage_mod.install_immutability_guard(engine)

        capture = CaptureEngine(adapter, policy, calendar_book, clock=clock)
        groups = _group_queue(queue, policy)

        for group in groups:
            # H11: rate limit pauses the worker; two consecutive account rate
            # limits stop new requests for this run order. Remaining tasks are
            # deferred (resumable), never dropped.
            if rate.stop_new_requests or rate.paused(clock()):
                _mark_remaining(queue, task_results, TaskState.DEFERRED_BUDGET)
                exit_code = EXIT_DEFERRED_BUDGET
                break
            requests = []
            for item in group:
                key = canonical_task_key(item.source_symbol, item.duration_seconds)
                if retry.can_attempt_now(key):
                    requests.append(CaptureRequest(
                        source_symbol=item.source_symbol,
                        duration_seconds=item.duration_seconds,
                        task_id=f"task-{key}",
                        attempt_id=f"attempt-{key}-{store.task_state(key).attempts + 1}",
                        product_key=item.product_key,
                    ))
            if not requests:
                continue
            try:
                outcomes = capture.capture_group(
                    requests,
                    deadline_monotonic=time.monotonic() + max(ledger.wall_remaining(), 1.0),
                    on_serial_attempt=before_serial_attempt,
                )
            except BudgetExhausted:
                _mark_remaining(queue, task_results, TaskState.DEFERRED_BUDGET)
                exit_code = EXIT_DEFERRED_BUDGET
                break
            for outcome in outcomes:
                lease.heartbeat(os.getpid(), clock())
                _handle_outcome(
                    outcome,
                    engine=engine,
                    policy=policy,
                    plan=plan,
                    auth=auth,
                    output_root=run_dir,
                    store=store,
                    retry=retry,
                    rate=rate,
                    ledger=ledger,
                    clock=clock,
                    calendar_book=calendar_book,
                    task_results=task_results,
                    product_state=product_state,
                )
            lease.heartbeat(os.getpid(), clock())
    except tq.AccountDeniedError:
        _mark_remaining(queue, task_results, TaskState.PERMISSION_DENIED)
        exit_code = EXIT_BLOCKED
    except (tq.TqAdapterError, ContractError, storage_mod.StorageError, IsolationRefused, OSError) as exc:
        print(f"execution failure: {getattr(exc, 'safe_message', str(exc))}", file=sys.stderr)
        exit_code = EXIT_FAILURE
    finally:
        try:
            if adapter is not None:
                try:
                    adapter.close()
                except Exception:
                    pass
            if engine is not None:
                engine.dispose()
        finally:
            lease.release(os.getpid())

    _finalize_products(run_dir, plan, product_state)
    _write_run_summary(run_dir, plan, policy, task_results, priority_status, exit_code)
    return exit_code


def _plan_queue(plan: Mapping[str, Any]):
    from .scheduler import QueueItem

    concrete: dict[str, list[str]] = {}
    priorities: dict[str, str] = {}
    for task in plan.get("tasks", []):
        product = task["product_key"]
        concrete.setdefault(product, []).extend(task.get("concrete_symbols", []))
    for task in plan.get("tasks", []):
        priorities.setdefault(task["product_key"], task.get("priority", "P5"))
    full = build_execution_queue(concrete, priorities=priorities)
    # Phases order priority; one run captures each logical key (symbol x
    # duration) at most once - re-capturing would waste the serial budget.
    seen: set[str] = set()
    queue: list[QueueItem] = []
    for item in full:
        key = canonical_task_key(item.source_symbol, item.duration_seconds)
        if key in seen:
            continue
        seen.add(key)
        queue.append(item)
    return queue


def _group_queue(queue: Sequence[Any], policy: Policy) -> list[list[Any]]:
    budget = policy.budget
    max_symbols = budget["active_symbols_max"]
    max_serials = budget["active_serials_max"]
    groups: list[list[Any]] = []
    current: list[Any] = []
    symbols: set[str] = set()
    for item in queue:
        if (
            len(current) >= max_serials
            or len(symbols | {item.source_symbol}) > max_symbols
        ):
            groups.append(current)
            current = []
            symbols = set()
        current.append(item)
        symbols.add(item.source_symbol)
    if current:
        groups.append(current)
    return groups


def _handle_outcome(
    outcome: CaptureOutcome,
    *,
    engine,
    policy: Policy,
    plan: Mapping[str, Any],
    auth: RunAuthorization,
    output_root: Path,
    store: RunStateStore,
    retry: RetryGate,
    rate: RateLimitCoordinator,
    ledger: BudgetLedger,
    clock: Callable[[], int],
    calendar_book: CalendarBook,
    task_results: list[dict[str, Any]],
    product_state: dict[str, dict[str, Any]],
) -> None:
    request = outcome.request
    logical_key = canonical_task_key(request.source_symbol, request.duration_seconds)
    if outcome.error is not None:
        failure_class, state = classify_exception(outcome.error)
        if isinstance(outcome.error, tq.RateLimitError):
            rate.observe(outcome.error, clock())
        else:
            rate.observe_non_rate_limit()
        retry.record_failure(logical_key, failure_class, attempt_started=True)
        if isinstance(outcome.error, tq.AccountDeniedError):
            raise outcome.error
        task_results.append(
            {
                "logical_key": logical_key,
                "state": state.value,
                "failure_class": failure_class.value,
            }
        )
        _record_failed_attempt_evidence(output_root, request, outcome)
        return

    rate.observe_non_rate_limit()
    ledger.charge_uncommitted_snapshot()
    try:
        synthetic = plan.get("synthetic", False)
        counts = {
            "closed": sum(
                1 for r in outcome.rows if r.valid_status is not None and r.valid_status.value == "CLOSED"
            ),
            "duplicate": outcome.duplicate_keys,
            "invalid": outcome.meta.invalid_rows,
            "padding": outcome.meta.padding_rows,
            "provisional": sum(
                1 for r in outcome.rows if r.valid_status is not None and r.valid_status.value == "PROVISIONAL"
            ),
            "unknown": sum(
                1 for r in outcome.rows if r.valid_status is not None and r.valid_status.value == "UNKNOWN"
            ),
            "valid": outcome.meta.valid_rows,
        }
        meta, status = artifact_mod.publish_raw_snapshot(
            output_root,
            request.product_key,
            outcome.meta,
            outcome.frame,
            synthetic=synthetic,
            counts=counts,
        )
        if status != "RAW_VERIFIED":
            raise ContractError("raw snapshot not verified after publish")
        retry.record_success(logical_key, TaskState.RAW_VERIFIED)

        batch_plan = storage_mod.build_batch_plan(
            task_logical_key=logical_key,
            task_id=request.task_id,
            attempt_id=request.attempt_id,
            source_id=f"tq_free_serial|{plan.get('policy_sha256', '')[:12]}",
            meta=meta,
            rows=outcome.rows,
            policy_sha256=policy.sha256,
            universe_sha256=str(plan.get("universe_version", "")),
            source_sha256=str(plan.get("inventory_sha256", "")),
            calendar_sha256=str(plan.get("calendar_evidence_sha256", "NONE_UNKNOWN")),
            created_at_nano=clock(),
        )
        ingest = storage_mod.ingest_batch(engine, batch_plan)
        reconciliation = storage_mod.reconcile_batch(engine, batch_plan)
        if not reconciliation["verified"]:
            retry.record_success(logical_key, TaskState.RUNNING)
            task_results.append(
                {
                    "logical_key": logical_key,
                    "state": TaskState.RETRYABLE_FAILURE.value,
                    "failure_class": "RECONCILIATION_MISMATCH",
                }
            )
            return
        state = ingest["state"]
        retry.record_success(logical_key, state)
        store.checkpoint(
            batch_plan.batch_identity_sha256,
            {"status": state.value, "counts": ingest["counts"]},
        )
        task_results.append(
            {
                "logical_key": logical_key,
                "state": state.value,
                "counts": ingest["counts"],
                "raw_sha256": meta.raw_sha256,
            }
        )
        # Immutable per-row records flow out immediately; summary-level
        # product files are finalized once at run end (publish-once files).
        artifact_mod.append_jsonl(
            output_root,
            f"products/{request.product_key.replace('.', '_')}/rows.jsonl",
            [
                {
                    "content_sha256": row.content_sha256,
                    "datetime_nano": (
                        str(row.datetime_nano) if row.datetime_nano is not None else None
                    ),
                    "frame_partition": row.frame_partition.value,
                    "raw_row_ordinal": row.raw_row_ordinal,
                    "reason": row.reason,
                    "source_symbol": row.source_symbol,
                    "trading_day": row.trading_day,
                    "valid_status": (
                        row.valid_status.value if row.valid_status else None
                    ),
                }
                for row in outcome.rows
            ],
        )
        entry = artifact_mod.raw_manifest_entry(meta, "RAW_VERIFIED")
        bucket = product_state.setdefault(
            request.product_key,
            {"manifest": [], "reconciliations": [], "cells": [], "gaps": [],
             "symbols": set(), "counts": {}},
        )
        bucket["manifest"].append(entry)
        bucket["symbols"].add(request.source_symbol)
        for key, value in ingest["counts"].items():
            bucket["counts"][key] = bucket["counts"].get(key, 0) + value
        bucket["reconciliations"].append(
            {"batch_id": batch_plan.batch_id, "verified": reconciliation["verified"]}
        )
        coverage = CoverageResult(
            target_kind=TargetKind.B,
            product_key=request.product_key,
            duration_seconds=request.duration_seconds,
            expected_slots=None,  # frozen-target expected slots stay UNKNOWN
            actual_slots=counts["closed"],
            missing_slots=None,
            unknown_slot_count=int(counts["unknown"]),
            conflicts=ingest["counts"].get("conflicts", 0),
            raw_watermark="RAW_VERIFIED",
            db_watermark=(
                "INGEST_VERIFIED" if state is TaskState.INGEST_VERIFIED else "NONE"
            ),
            source_availability="ATTEMPTED",
        )
        bucket["cells"].append(
            {
                "coverage_status": coverage.coverage_status().value,
                "duration_seconds": request.duration_seconds,
                "expected_slots": None,
                "source_symbol": request.source_symbol,
                "unknown_expected_slots": True,
            }
        )
        if counts["provisional"]:
            bucket["gaps"].append(
                {"reason": "PROVISIONAL", "source_symbol": request.source_symbol}
            )
        if counts["unknown"]:
            bucket["gaps"].append(
                {"reason": "CALENDAR_UNKNOWN", "source_symbol": request.source_symbol}
            )
    finally:
        ledger.release_uncommitted_snapshot()


def _record_failed_attempt_evidence(
    output_root: Path, request: CaptureRequest, outcome: CaptureOutcome
) -> None:
    """Zero-row and failed attempts keep structured evidence (H06/H11)."""
    import re

    def _safe(component: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]", "_", component)

    product = _safe(request.product_key.replace(".", "_"))
    directory = output_root / "products" / product
    directory.mkdir(parents=True, exist_ok=True)
    entry = {
        "attempt_id": request.attempt_id,
        "duration_seconds": request.duration_seconds,
        "error_class": outcome.error.failure_class.value if outcome.error else None,
        "rows": 0,
        "source_symbol": request.source_symbol,
        "task_id": request.task_id,
    }
    part = (
        f".part-{time.strftime('%H%M%S')}-{_safe(request.attempt_id)}"
        "-requests.jsonl"
    )
    payload = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode("utf-8")
    try:
        artifact_mod.atomic_publish_bytes(directory, part, payload + b"\n")
    except artifact_mod.ArtifactError:
        pass


# H13 one-time per-product summary files. ``_finalize_products`` publishes
# this set exactly once per product directory; a later run of the same
# immutable plan (recovery of remaining cycles) must never re-publish them.
_ONE_TIME_PRODUCT_FILES = (
    "identity.json",
    "raw-manifest.json",
    "reconciliation.json",
    "coverage.json",
    "gaps.json",
    "acceptance.json",
)


def _finalize_products(
    run_dir: Path,
    plan: Mapping[str, Any],
    product_state: Mapping[str, Mapping[str, Any]],
) -> None:
    """Write H13 per-product summary files exactly once at run end.

    Raw snapshots and rows.jsonl parts are already published immutably during
    the run; these summary files aggregate them and are never overwritten
    (a resumed run keeps its prior evidence and writes nothing new here).

    A product whose one-time files were already published by a prior run of
    the same immutable plan is skipped whole: recovery finalization neither
    re-publishes nor overwrites existing evidence and the OPT-05 no-clobber
    contract stays intact. A product directory holding only part of the set
    is an inconsistent evidence state (possible tamper): it is refused loudly
    instead of being silently skipped or completed.
    """
    synthetic = bool(plan.get("synthetic", False))
    for product, bucket in sorted(product_state.items()):
        product_dir = artifact_mod.resolve_within_root(
            run_dir, f"products/{product.replace('.', '_')}"
        )
        existing = {
            name for name in _ONE_TIME_PRODUCT_FILES if (product_dir / name).exists()
        }
        if existing:
            missing = set(_ONE_TIME_PRODUCT_FILES) - existing
            if missing:
                raise artifact_mod.ArtifactError(
                    "product one-time evidence set is incomplete for "
                    f"{product}: present={sorted(existing)} "
                    f"missing={sorted(missing)}; refusing to finalize "
                    "(inconsistent evidence state / possible tamper)"
                )
            # Already finalized by a prior run of this same immutable plan;
            # the resume keeps prior evidence and re-publishes nothing.
            continue
        payload_identity = {
            "instrument_kinds": "derived at runtime from source metadata; "
            "see denominator expansion in the plan",
            "lifecycle": "see plan denominator and raw attempts",
            "product_key": product,
            "source_symbols": sorted(bucket["symbols"]),
            "synthetic": synthetic,
            "universe_version": plan.get("universe_version"),
        }
        artifact_mod.write_product_json(
            run_dir, product, "identity.json", payload_identity
        )
        artifact_mod.write_product_json(
            run_dir,
            product,
            "raw-manifest.json",
            {"entries": bucket["manifest"], "synthetic": synthetic},
        )
        artifact_mod.write_product_json(
            run_dir,
            product,
            "reconciliation.json",
            {
                "batch_counts": bucket["counts"],
                "batches": bucket["reconciliations"],
                "product_key": product,
                "synthetic": synthetic,
            },
        )
        artifact_mod.write_product_json(
            run_dir,
            product,
            "coverage.json",
            {
                "cells": bucket["cells"],
                "is_baseline": True,
                "product_key": product,
                "synthetic": synthetic,
            },
        )
        artifact_mod.write_product_json(
            run_dir,
            product,
            "gaps.json",
            {
                "gaps": bucket["gaps"],
                "notes": [
                    "expected slot denominators remain UNKNOWN until frozen "
                    "targets with calendar evidence (H13); null is never 0"
                ],
                "product_key": product,
            },
        )
        statuses = {cell["coverage_status"] for cell in bucket["cells"]}
        from .contracts import AcceptanceRecord, CoverageStatus

        coverage_status = (
            CoverageStatus.PARTIAL
            if len(statuses) != 1
            else CoverageStatus(next(iter(statuses)))
        )
        record = AcceptanceRecord(
            author="COLLECTOR_PRE_ACCEPTANCE",
            independence_declaration=(
                "automated pre-acceptance only; independent acceptance by a "
                "reviewer who took no part in implementation is required "
                "(H15); this record claims no part of PASS-level evidence"
            ),
            policy_sha256="0" * 64,  # placeholder: pre-acceptance binds no PASS scope
            code_manifest_sha256="0" * 64,
            input_hashes=(),
            db_verification_snapshot_sha256="0" * 64,
            evidence_integrity_verdict="NOT_RUN",
            coverage_status=coverage_status,
            scope_statement="SYNTHETIC pre-acceptance; not a data acceptance verdict",
            gaps=("independent review NOT_RUN",),
            next_steps=("dispatch independent acceptance reviewer",),
        )
        artifact_mod.write_product_json(
            run_dir,
            product,
            "acceptance.json",
            {
                "author": record.author,
                "coverage_status": record.coverage_status.value,
                "evidence_integrity_verdict": record.evidence_integrity_verdict,
                "gaps": list(record.gaps),
                "independence_declaration": record.independence_declaration,
                "synthetic": synthetic,
            },
        )


def _mark_remaining(queue: Sequence[Any], results: list[dict[str, Any]], state: TaskState) -> None:
    done = {item["logical_key"] for item in results}
    for item in queue:
        key = canonical_task_key(item.source_symbol, item.duration_seconds)
        if key not in done:
            results.append({"logical_key": key, "state": state.value})


def _write_run_summary(
    run_dir: Path,
    plan: Mapping[str, Any],
    policy: Policy,
    task_results: Sequence[Mapping[str, Any]],
    priority_status: str,
    exit_code: int,
) -> None:
    states: dict[str, int] = {}
    for item in task_results:
        states[item["state"]] = states.get(item["state"], 0) + 1
    summary = {
        "baseline_product_total": plan.get("baseline_products"),
        "collector_run": "SYNTHETIC_OFFLINE" if plan.get("synthetic") else "REAL",
        "coverage_status_counts": states,
        "exit_code": exit_code,
        "policy_sha256": policy.sha256,
        "priority_status": priority_status,
        "task_count": len(task_results),
    }
    payload = json.dumps(summary, sort_keys=True, indent=2, ensure_ascii=False).encode(
        "utf-8"
    )
    try:
        artifact_mod.atomic_publish_bytes(run_dir, "run-summary.json", payload)
    except artifact_mod.ArtifactError:
        # Resume runs already carry a summary; keep prior evidence intact.
        pass


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acquire_futures_free.py",
        description=(
            "W2 free-channel futures acquisition collector (offline default; "
            "run/resume require authorization + review seal)"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="build a dated task plan (offline)")
    plan_parser.add_argument("--policy", required=True)
    plan_parser.add_argument("--inventory", required=True)
    plan_parser.add_argument("--calendar-evidence", default=None)
    plan_parser.add_argument("--output", required=True)

    verify_parser = subparsers.add_parser(
        "verify-package", help="verify an artifact package offline"
    )
    verify_parser.add_argument("--package", required=True)
    verify_parser.add_argument("--offline", action="store_true", default=True)

    run_parser = subparsers.add_parser("run", help="execute an authorized run")
    run_parser.add_argument("--plan", required=True)
    run_parser.add_argument("--authorization", required=True)
    run_parser.add_argument("--review-seal", required=True)

    resume_parser = subparsers.add_parser("resume", help="resume an authorized run")
    resume_parser.add_argument("--plan", required=True)
    resume_parser.add_argument("--authorization", required=True)
    resume_parser.add_argument("--review-seal", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "plan":
        return cmd_plan(args)
    if args.command == "verify-package":
        return cmd_verify_package(args)
    if args.command == "run":
        return cmd_run(args, resume=False)
    if args.command == "resume":
        return cmd_run(args, resume=True)
    parser.error(f"unknown command {args.command}")
    return EXIT_FAILURE


__all__ = [
    "EXIT_BLOCKED",
    "EXIT_DEFERRED_BUDGET",
    "EXIT_FAILURE",
    "EXIT_GATE_REFUSED",
    "EXIT_OK",
    "cmd_plan",
    "cmd_run",
    "cmd_verify_package",
    "compute_code_manifest",
    "execute_run",
    "main",
    "run_gates",
]
