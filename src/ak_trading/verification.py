"""Offline, read-only verification of backtest evidence bundles.

Validates consistency among stdout, stderr, journal JSONL, and summary report
without network access, TqSdk construction, settings/.env loading, or writes
to input evidence files.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import stat
from urllib.parse import unquote, urlsplit
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from ak_trading.artifacts import JOURNAL_SCHEMA_VERSION

VERIFIER_SCHEMA_VERSION = 1
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_STREAM_LINES = 20_000
MAX_JSONL_RECORDS = 200_000
MAX_JSON_DEPTH = 32
MAX_AST_DEPTH = 32
MAX_AST_NODES = 5_000
MAX_SAFE_INT = 10**15
MAX_SAFE_ABS_POSITION = 1_000_000
NUMERIC_REL_TOL = 1e-9
NUMERIC_ABS_TOL = 1e-9
DEFAULT_MIN_DECISIONS = 1

_KNOWN_EVENTS = frozenset(
    {"run_manifest", "run_started", "decision", "run_finished", "shutdown"}
)
_DECISION_ACTIONS = frozenset(
    {
        "blocked",
        "no_signal",
        "target_already_requested",
        "already_at_target",
        "set_target",
    }
)
_BLOCK_REASONS = frozenset(
    {
        "position_not_ready",
        "quote_not_ready",
        "target_task_finished",
    }
)
_SENSITIVE_KEY_FRAGMENTS = frozenset(
    {
        "tq_user",
        "tq_pass",
        "password",
        "passwd",
        "api_key",
        "apikey",
        "secret",
        "token",
        "authorization",
        "auth_header",
        "access_key",
        "private_key",
        "credential",
        "database_url",
        "ak_database_url",
        "dsn",
        "conninfo",
    }
)
_SNAPSHOT_FIELDS = ("balance", "available", "margin", "position")
_SUMMARY_FIELDS = ("reason", "updates", "decisions", "target_changes", "final_position")
_RUN_SUMMARY_FIELDS = frozenset(_SUMMARY_FIELDS)
_REPORT_TOP_LEVEL = (
    "schema_version",
    "journal",
    "run_id",
    "manifest",
    "identity",
    "integrity",
    "lifecycle",
    "events",
    "account_snapshots",
    "run_summary",
)
_TOKEN_ORDER = ("BACKTEST_REPORT", "BACKTEST_DONE", "BACKTEST_STAT", "JOURNAL")
_TOKEN_LINE = re.compile(
    r"^(BACKTEST_REPORT|BACKTEST_DONE|BACKTEST_STAT|JOURNAL)(?:\s+(.*))?$"
)
_NP_FLOAT_CALLS = frozenset({"np.float64", "float64"})
_NP_INT_CALLS = frozenset({"np.int64", "int64"})
_STAT_REQUIRED = frozenset(
    {
        "start_date",
        "end_date",
        "init_balance",
        "start_balance",
        "balance",
        "end_balance",
    }
)
_STAT_ALLOWED = frozenset(
    {
        "start_date",
        "end_date",
        "init_balance",
        "balance",
        "start_balance",
        "end_balance",
        "ror",
        "annual_yield",
        "trading_days",
        "cum_profit_days",
        "cum_loss_days",
        "max_drawdown",
        "commission",
        "open_times",
        "close_times",
        "daily_risk_ratio",
        "max_cont_profit_days",
        "max_cont_loss_days",
        "sharpe_ratio",
        "calmar_ratio",
        "sortino_ratio",
        "tqsdk_punchline",
        "profit_volumes",
        "loss_volumes",
        "profit_value",
        "loss_value",
        "winning_rate",
        "profit_loss_ratio",
    }
)
_STAT_STRING_KEYS = frozenset({"start_date", "end_date", "tqsdk_punchline"})
_STAT_INT_KEYS = frozenset(
    {
        "trading_days",
        "cum_profit_days",
        "cum_loss_days",
        "open_times",
        "close_times",
        "max_cont_profit_days",
        "max_cont_loss_days",
        "profit_volumes",
        "loss_volumes",
    }
)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MANIFEST_REQUIRED = frozenset(
    {
        "event",
        "schema_version",
        "run_id",
        "created_at",
        "mode",
        "symbol",
        "strategy",
        "execution",
        "backtest",
    }
)
_MANIFEST_OPTIONAL = frozenset({"market_symbol"})
_STARTED_KEYS = frozenset(
    {
        "event",
        "mode",
        "symbol",
        "duration_seconds",
        "balance",
        "available",
        "margin",
        "position",
        "run_id",
    }
)
_FINISHED_KEYS = frozenset(
    {
        "event",
        "mode",
        "symbol",
        "duration_seconds",
        "balance",
        "available",
        "margin",
        "position",
        "run_id",
        "reason",
        "updates",
        "decisions",
        "target_changes",
        "final_position",
    }
)
_DECISION_KEYS = frozenset(
    {
        "event",
        "mode",
        "symbol",
        "duration_seconds",
        "balance",
        "available",
        "margin",
        "position",
        "run_id",
        "bar_datetime",
        "close",
        "fast_ma",
        "slow_ma",
        "signal_target",
        "signal_reason",
        "risk_target",
        "risk_reason",
        "current_position",
        "requested_target",
        "last_price",
        "action",
        "block_reason",
    }
)
_REPORT_IDENTITY_KEYS = frozenset({"mode", "symbol"})
_REPORT_INTEGRITY_KEYS = frozenset(
    {
        "selected_records",
        "foreign_records",
        "malformed_lines",
        "non_object_lines",
        "blank_lines",
        "unknown_events",
        "manifest_count",
        "run_started_count",
        "run_finished_count",
        "shutdown_count",
    }
)
_REPORT_LIFECYCLE_KEYS = frozenset({"state", "run_complete", "shutdown_complete"})
_REPORT_EVENTS_KEYS = frozenset({"counts", "decision_actions", "blocked_reasons"})
_REPORT_ACCOUNT_KEYS = frozenset({"start", "last_recorded"})
_REPORT_RUN_SUMMARY_KEYS = frozenset(_SUMMARY_FIELDS)
_STRATEGY_KEYS = frozenset({"name", "fast_window", "slow_window"})
_EXECUTION_KEYS = frozenset(
    {"duration_seconds", "max_abs_position", "allow_short", "initial_balance"}
)
_BACKTEST_KEYS = frozenset({"start", "end"})


class VerificationError(Exception):
    """Unrecoverable verifier failure (exit code 2)."""

    def __init__(self, code: str, message: str, *, location: str = "verifier") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.location = location


@dataclass(frozen=True)
class CheckResult:
    code: str
    location: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "code": self.code,
            "location": self.location,
            "ok": self.ok,
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class FileIdentity:
    path: str
    size: int
    sha256: str
    st_dev: int
    st_ino: int


@dataclass
class VerificationState:
    checks: list[CheckResult] = field(default_factory=list)
    fatal: VerificationError | None = None
    run_id: str | None = None
    secret_scan: str = "skipped"
    inputs: dict[str, str] = field(default_factory=dict)

    def add(
        self,
        code: str,
        location: str,
        ok: bool,
        detail: str = "",
    ) -> None:
        self.checks.append(
            CheckResult(
                code=_safe_text(code),
                location=_safe_text(location),
                ok=ok,
                detail=_safe_text(detail) if detail else "",
            )
        )

    def fail(self, code: str, location: str, detail: str = "") -> None:
        self.add(code, location, False, detail)

    def pass_(self, code: str, location: str, detail: str = "") -> None:
        self.add(code, location, True, detail)


@dataclass(frozen=True)
class ExpectedParams:
    symbol: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    duration: int | None = None
    fast_window: int | None = None
    slow_window: int | None = None
    max_position: int | None = None
    run_id: str | None = None


def verify_backtest_artifacts(
    *,
    stdout_path: str | Path,
    stderr_path: str | Path,
    journal_path: str | Path,
    report_path: str | Path,
    exit_code: int,
    expected: ExpectedParams | None = None,
    min_decisions: int = DEFAULT_MIN_DECISIONS,
) -> dict[str, Any]:
    """Verify one offline backtest evidence bundle and return a stable report."""
    state = VerificationState()
    expected = expected or ExpectedParams()
    try:
        _validate_expected_params(expected)
        _validate_min_decisions(min_decisions)
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise VerificationError(
                "usage_error",
                "exit_code must be an int",
                location="cli",
            )
        loaded = _load_inputs(
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            journal_path=journal_path,
            report_path=report_path,
        )
        state.inputs = {
            name: meta.identity.path for name, meta in loaded.items()
        }
        start_snaps = {name: meta.identity for name, meta in loaded.items()}
        raw = {name: meta.data for name, meta in loaded.items()}
        texts = {
            "stdout": _decode_stream(raw["stdout"], location="stdout"),
            "stderr": _decode_stream(raw["stderr"], location="stderr"),
            "journal": _decode_utf8(raw["journal"], location="journal"),
            "report": _decode_utf8(raw["report"], location="report"),
        }
        stdout_lines = _split_lines(texts["stdout"], location="stdout")
        _split_lines(texts["stderr"], location="stderr")
        tokens = _extract_tokens(stdout_lines, state)
        journal_records = _parse_jsonl(texts["journal"])
        report = _parse_report_json(texts["report"], state)
        secrets = _load_env_secrets()
        state.secret_scan = _scan_secrets(
            state,
            journal_records=journal_records,
            report=report,
            raw_files=raw,
            stream_texts={"stdout": texts["stdout"], "stderr": texts["stderr"]},
            secrets=secrets,
        )
        _validate_exit_code(exit_code, state)
        _validate_tokens_against_paths(tokens, loaded, state)
        _validate_report_family_schemas(report)
        state.pass_("report_family_schema", "report", "ok")
        done, stat = _parse_and_validate_console_evidence(tokens, state)
        summary = _validate_journal_and_report(
            journal_records,
            report,
            loaded["journal"].path,
            loaded["journal"].identity.path,
            state,
            expected=expected,
            min_decisions=min_decisions,
        )
        if summary is not None:
            state.run_id = summary.get("run_id")
            if isinstance(state.run_id, str):
                state.run_id = _safe_text(state.run_id)
            _validate_done_and_stat(
                done,
                stat,
                summary,
                report,
                state,
                expected=expected,
            )
        end_snaps = _recheck_inputs(loaded)
        _compare_snapshots(start_snaps, end_snaps, state)
    except VerificationError as exc:
        state.fatal = VerificationError(
            _safe_text(exc.code),
            _safe_text(exc.message),
            location=_safe_text(exc.location),
        )
        state.fail(state.fatal.code, state.fatal.location, state.fatal.message)
    except RecursionError:
        state.fatal = VerificationError(
            "internal_error",
            "recursion limit exceeded",
            location="verifier",
        )
        state.fail(state.fatal.code, state.fatal.location, state.fatal.message)
    except (OverflowError, MemoryError, ValueError, TypeError, OSError) as exc:
        state.fatal = VerificationError(
            "internal_error",
            "internal verification failure",
            location="verifier",
        )
        state.fail(state.fatal.code, state.fatal.location, state.fatal.message)
        del exc
    except Exception:
        state.fatal = VerificationError(
            "internal_error",
            "internal verification failure",
            location="verifier",
        )
        state.fail(state.fatal.code, state.fatal.location, state.fatal.message)

    result = _build_result(state)
    secrets = _load_env_secrets()
    return _finalize_result(result, secrets=secrets, state=state)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint used by scripts/verify_backtest_artifacts.py."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Offline verifier for backtest stdout/stderr/journal/report bundles",
        exit_on_error=False,
    )
    parser.add_argument("--stdout", type=Path, required=True)
    parser.add_argument("--stderr", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--expected-symbol")
    parser.add_argument("--expected-start-date")
    parser.add_argument("--expected-end-date")
    parser.add_argument("--expected-duration", type=int)
    parser.add_argument("--expected-fast-window", type=int)
    parser.add_argument("--expected-slow-window", type=int)
    parser.add_argument("--expected-max-position", type=int)
    parser.add_argument("--expected-run-id")
    parser.add_argument(
        "--min-decisions",
        type=int,
        default=DEFAULT_MIN_DECISIONS,
    )

    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except argparse.ArgumentError:
        return _emit_usage_error()
    except SystemExit as exc:
        code = exc.code
        if code in (0, None):
            return 0
        return _emit_usage_error()

    try:
        result = verify_backtest_artifacts(
            stdout_path=args.stdout,
            stderr_path=args.stderr,
            journal_path=args.journal,
            report_path=args.report,
            exit_code=args.exit_code,
            expected=ExpectedParams(
                symbol=args.expected_symbol,
                start_date=args.expected_start_date,
                end_date=args.expected_end_date,
                duration=args.expected_duration,
                fast_window=args.expected_fast_window,
                slow_window=args.expected_slow_window,
                max_position=args.expected_max_position,
                run_id=args.expected_run_id,
            ),
            min_decisions=args.min_decisions,
        )
    except Exception:
        result = {
            "schema_version": VERIFIER_SCHEMA_VERSION,
            "verdict": "error",
            "valid": False,
            "exit_code": 2,
            "run_id": None,
            "secret_scan": "skipped",
            "inputs": {},
            "checks": [
                {
                    "code": "internal_error",
                    "location": "cli",
                    "ok": False,
                    "detail": "internal verification failure",
                }
            ],
        }
    _write_result_json(result)
    return int(result["exit_code"])


def _emit_usage_error(detail: str = "invalid or missing arguments") -> int:
    secrets = _load_env_secrets()
    result = {
        "schema_version": VERIFIER_SCHEMA_VERSION,
        "verdict": "error",
        "valid": False,
        "exit_code": 2,
        "run_id": None,
        "secret_scan": "skipped",
        "inputs": {},
        "checks": [
            {
                "code": "usage_error",
                "location": "cli",
                "ok": False,
                "detail": "invalid or missing arguments",
            }
        ],
    }
    del detail
    result = _redact_known_secrets(result, secrets=secrets)
    if not isinstance(result, dict):
        result = {
            "schema_version": VERIFIER_SCHEMA_VERSION,
            "verdict": "error",
            "valid": False,
            "exit_code": 2,
            "run_id": None,
            "secret_scan": "skipped",
            "inputs": {},
            "checks": [
                {
                    "code": "usage_error",
                    "location": "cli",
                    "ok": False,
                    "detail": "invalid or missing arguments",
                }
            ],
        }
    result = _ascii_sanitize_tree(result)
    if not isinstance(result, dict):
        result = {
            "schema_version": VERIFIER_SCHEMA_VERSION,
            "verdict": "error",
            "valid": False,
            "exit_code": 2,
            "run_id": None,
            "secret_scan": "skipped",
            "inputs": {},
            "checks": [
                {
                    "code": "usage_error",
                    "location": "cli",
                    "ok": False,
                    "detail": "invalid or missing arguments",
                }
            ],
        }
    _write_result_json(result)
    return 2


def _write_result_json(result: Mapping[str, Any]) -> None:
    secrets = _load_env_secrets()
    redacted = _redact_known_secrets(result, secrets=secrets)
    if not isinstance(redacted, Mapping):
        redacted = result
    sanitized = _ascii_sanitize_tree(redacted)
    if not isinstance(sanitized, Mapping):
        sanitized = redacted
    text = json.dumps(
        sanitized, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    sys.stdout.write(text + "\n")


def _build_result(state: VerificationState) -> dict[str, Any]:
    checks = sorted(
        (item.as_dict() for item in state.checks),
        key=lambda item: (
            item["code"],
            item["location"],
            item.get("detail", ""),
        ),
    )
    if state.fatal is not None:
        verdict = "error"
        valid = False
        exit_code = 2
    elif any(not item["ok"] for item in checks):
        verdict = "invalid"
        valid = False
        exit_code = 1
    else:
        verdict = "pass"
        valid = True
        exit_code = 0
    return {
        "schema_version": VERIFIER_SCHEMA_VERSION,
        "verdict": verdict,
        "valid": valid,
        "exit_code": exit_code,
        "run_id": state.run_id,
        "secret_scan": state.secret_scan,
        "inputs": dict(sorted(state.inputs.items())),
        "checks": checks,
    }


def _finalize_result(
    result: dict[str, Any],
    *,
    secrets: Sequence[str],
    state: VerificationState,
) -> dict[str, Any]:
    findings = _scan_value_for_known_secrets(result, secrets=secrets, location="result")
    if findings:
        for location in sorted(set(findings)):
            state.fail("secret_leak", location, "sensitive material detected")
        state.secret_scan = "failed"
        result = _build_result(state)
    redacted = _redact_known_secrets(result, secrets=secrets)
    if not isinstance(redacted, dict):
        redacted = result
    sanitized = _ascii_sanitize_tree(redacted)
    if not isinstance(sanitized, dict):
        sanitized = result
    return sanitized


@dataclass(frozen=True)
class LoadedInput:
    path: Path
    identity: FileIdentity
    data: bytes


def _load_inputs(
    *,
    stdout_path: str | Path,
    stderr_path: str | Path,
    journal_path: str | Path,
    report_path: str | Path,
) -> dict[str, LoadedInput]:
    named = {
        "stdout": Path(stdout_path),
        "stderr": Path(stderr_path),
        "journal": Path(journal_path),
        "report": Path(report_path),
    }
    loaded: dict[str, LoadedInput] = {}
    for name, path in named.items():
        identity, data = _read_file_snapshot(path, location=name)
        loaded[name] = LoadedInput(path=path if path.is_absolute() else path.resolve(strict=True), identity=identity, data=data)
        # Prefer identity.path's resolved Path for later samefile checks.
        loaded[name] = LoadedInput(
            path=Path(identity.path.replace("/", os.sep)),
            identity=identity,
            data=data,
        )

    names = list(loaded.keys())
    for index, left_name in enumerate(names):
        for right_name in names[index + 1 :]:
            left = loaded[left_name]
            right = loaded[right_name]
            if _identities_collide(left.identity, right.identity, left.path, right.path):
                raise VerificationError(
                    "input_path_collision",
                    f"{right_name} collides with {left_name}",
                    location=right_name,
                )
    return loaded


def _identities_collide(
    left: FileIdentity,
    right: FileIdentity,
    left_path: Path,
    right_path: Path,
) -> bool:
    if left.path == right.path:
        return True
    if left.st_ino != 0 and right.st_ino != 0:
        if left.st_dev == right.st_dev and left.st_ino == right.st_ino:
            return True
    try:
        if os.path.samefile(left_path, right_path):
            return True
    except OSError:
        return False
    return False


def _read_file_snapshot(path: Path, *, location: str) -> tuple[FileIdentity, bytes]:
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise VerificationError(
            "input_missing",
            f"{location} path does not exist",
            location=location,
        ) from exc
    except OSError as exc:
        raise VerificationError(
            "input_unreadable",
            f"{location} path cannot be resolved",
            location=location,
        ) from exc

    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        fd = os.open(resolved, flags)
    except OSError as exc:
        raise VerificationError(
            "input_unreadable",
            f"{location} cannot be opened",
            location=location,
        ) from exc
    try:
        try:
            st_before = os.fstat(fd)
        except OSError as exc:
            raise VerificationError(
                "input_unreadable",
                f"{location} cannot be stat'ed",
                location=location,
            ) from exc
        if not stat.S_ISREG(st_before.st_mode):
            raise VerificationError(
                "input_not_file",
                f"{location} is not a regular file",
                location=location,
            )
        if st_before.st_size > MAX_FILE_BYTES:
            raise VerificationError(
                "input_too_large",
                f"{location} exceeds {MAX_FILE_BYTES} bytes",
                location=location,
            )
        try:
            data = os.read(fd, MAX_FILE_BYTES + 1)
        except OSError as exc:
            raise VerificationError(
                "input_unreadable",
                f"{location} cannot be read",
                location=location,
            ) from exc
        if len(data) > MAX_FILE_BYTES:
            raise VerificationError(
                "input_too_large",
                f"{location} exceeds {MAX_FILE_BYTES} bytes",
                location=location,
            )
        # Drain remaining only if OS returned a short read under the size limit.
        if len(data) < st_before.st_size:
            chunks = [data]
            total = len(data)
            while total < st_before.st_size and total <= MAX_FILE_BYTES:
                try:
                    chunk = os.read(fd, min(MAX_FILE_BYTES + 1 - total, st_before.st_size - total))
                except OSError as exc:
                    raise VerificationError(
                        "input_unreadable",
                        f"{location} cannot be read",
                        location=location,
                    ) from exc
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    raise VerificationError(
                        "input_too_large",
                        f"{location} exceeds {MAX_FILE_BYTES} bytes",
                        location=location,
                    )
            data = b"".join(chunks)
        try:
            st_after = os.fstat(fd)
        except OSError as exc:
            raise VerificationError(
                "input_unreadable",
                f"{location} cannot be re-stat'ed",
                location=location,
            ) from exc
        if (
            st_before.st_dev != st_after.st_dev
            or st_before.st_ino != st_after.st_ino
            or st_before.st_size != st_after.st_size
            or st_before.st_size != len(data)
        ):
            raise VerificationError(
                "input_unstable",
                f"{location} changed during read",
                location=location,
            )
        identity = FileIdentity(
            path=_canonical_path(resolved),
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            st_dev=int(st_before.st_dev),
            st_ino=int(st_before.st_ino),
        )
        return identity, data
    finally:
        os.close(fd)


def _recheck_inputs(loaded: Mapping[str, LoadedInput]) -> dict[str, FileIdentity]:
    end: dict[str, FileIdentity] = {}
    for name, meta in loaded.items():
        try:
            identity, data = _read_file_snapshot(meta.path, location=name)
        except VerificationError:
            raise
        except OSError as exc:
            raise VerificationError(
                "input_unreadable",
                f"{name} cannot be rechecked",
                location=name,
            ) from exc
        if (
            identity.st_dev != meta.identity.st_dev
            or identity.st_ino != meta.identity.st_ino
            or identity.size != meta.identity.size
            or identity.sha256 != meta.identity.sha256
            or data != meta.data
            or identity.path != meta.identity.path
        ):
            raise VerificationError(
                "input_mutated",
                f"{name} changed during verification",
                location=name,
            )
        end[name] = identity
    return end


def _canonical_path(path: Path) -> str:
    try:
        resolved = Path(path).resolve(strict=True)
    except FileNotFoundError as exc:
        raise VerificationError(
            "input_missing",
            "path does not exist",
            location="path",
        ) from exc
    except OSError as exc:
        raise VerificationError(
            "input_unreadable",
            "path cannot be resolved",
            location="path",
        ) from exc
    normalized = os.path.normcase(str(resolved))
    return normalized.replace("\\", "/")


def _paths_bound_equal(token_path: str, expected: Path) -> bool:
    try:
        left = Path(token_path)
        if not left.is_absolute():
            left = Path.cwd() / left
        left_canonical = _canonical_path(left)
        right_canonical = _canonical_path(expected)
    except VerificationError:
        return False
    except (OSError, FileNotFoundError):
        return False
    return left_canonical == right_canonical


def _decode_utf8(data: bytes, *, location: str) -> str:
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        raise VerificationError(
            "decode_error",
            f"{location} is not UTF-8",
            location=location,
        )
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise VerificationError(
            "decode_error",
            f"{location} is not strict UTF-8",
            location=location,
        ) from exc


def _decode_stream(data: bytes, *, location: str) -> str:
    for encoding in ("utf-8", "gbk", "cp936"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise VerificationError(
        "decode_error",
        f"{location} cannot be strictly decoded as UTF-8 or GBK/CP936",
        location=location,
    )


def _split_lines(text: str, *, location: str) -> list[str]:
    if not text:
        return []
    lines = text.splitlines()
    if len(lines) > MAX_STREAM_LINES:
        raise VerificationError(
            "stream_too_many_lines",
            f"{location} exceeds {MAX_STREAM_LINES} lines",
            location=location,
        )
    return lines


def _extract_tokens(
    stdout_lines: Sequence[str], state: VerificationState
) -> dict[str, str]:
    found: dict[str, list[tuple[int, str]]] = {name: [] for name in _TOKEN_ORDER}
    for index, line in enumerate(stdout_lines, start=1):
        if line[:1].isspace():
            stripped = line.lstrip()
            for token in _TOKEN_ORDER:
                if (
                    stripped == token
                    or stripped.startswith(token + " ")
                    or stripped.startswith(token + "\t")
                ):
                    state.fail(
                        "token_leading_whitespace",
                        f"stdout:L{index}",
                        token,
                    )
                    break
                if re.search(rf"(?<![A-Za-z0-9_]){token}(?![A-Za-z0-9_])", stripped):
                    state.fail(
                        "token_embedded",
                        f"stdout:L{index}",
                        f"{token} embedded in non-token line",
                    )
                    break
            continue

        match = _TOKEN_LINE.match(line)
        if match is None:
            for token in _TOKEN_ORDER:
                if re.search(rf"(?<![A-Za-z0-9_]){token}(?![A-Za-z0-9_])", line):
                    state.fail(
                        "token_embedded",
                        f"stdout:L{index}",
                        f"{token} embedded in non-token line",
                    )
            continue
        name = match.group(1)
        payload = match.group(2) if match.group(2) is not None else ""
        found[name].append((index, payload))

    tokens: dict[str, str] = {}
    positions: list[tuple[int, str]] = []
    for name in _TOKEN_ORDER:
        occurrences = found[name]
        if not occurrences:
            state.fail("token_missing", "stdout", name)
            continue
        if len(occurrences) > 1:
            state.fail(
                "token_duplicate",
                "stdout",
                f"{name} count={len(occurrences)}",
            )
            continue
        line_no, payload = occurrences[0]
        tokens[name] = payload
        positions.append((line_no, name))
        state.pass_("token_present", f"stdout:L{line_no}", name)

    if len(positions) == len(_TOKEN_ORDER):
        ordered_names = [name for _, name in sorted(positions, key=lambda item: item[0])]
        if ordered_names != list(_TOKEN_ORDER):
            state.fail(
                "token_order",
                "stdout",
                "->".join(ordered_names),
            )
        else:
            state.pass_("token_order", "stdout", "->".join(ordered_names))
    return tokens


def _validate_exit_code(exit_code: int, state: VerificationState) -> None:
    if exit_code != 0:
        state.fail("exit_code", "cli", f"expected 0 got {exit_code}")
    else:
        state.pass_("exit_code", "cli", "0")


def _validate_tokens_against_paths(
    tokens: Mapping[str, str],
    loaded: Mapping[str, LoadedInput],
    state: VerificationState,
) -> None:
    report_token = tokens.get("BACKTEST_REPORT")
    if report_token is not None:
        payload = report_token.strip()
        if not payload or " " not in payload and "\t" not in payload:
            # Need path and status: rsplit once.
            state.fail(
                "token_report_shape",
                "stdout",
                "BACKTEST_REPORT requires path and state",
            )
        else:
            path_part, status = payload.rsplit(None, 1)
            if not path_part:
                state.fail(
                    "token_report_shape",
                    "stdout",
                    "BACKTEST_REPORT requires path and state",
                )
            else:
                if status != "complete":
                    state.fail("report_status", "stdout", status)
                else:
                    state.pass_("report_status", "stdout", "complete")
                if _paths_bound_equal(path_part, loaded["report"].path):
                    state.pass_("report_path_match", "stdout", "matched")
                else:
                    state.fail("report_path_match", "stdout", "path mismatch")

    journal_token = tokens.get("JOURNAL")
    if journal_token is not None:
        token_path = journal_token
        if not token_path or not token_path.strip():
            state.fail("token_journal_shape", "stdout", "JOURNAL requires one path")
        elif _paths_bound_equal(token_path.strip(), loaded["journal"].path):
            # strip only for empty check; path may contain internal spaces.
            # Use original payload with only surrounding strip for resolution.
            state.pass_("journal_path_match", "stdout", "matched")
        else:
            # Try without strip of internal content - strip ends only.
            if _paths_bound_equal(token_path, loaded["journal"].path):
                state.pass_("journal_path_match", "stdout", "matched")
            else:
                state.fail("journal_path_match", "stdout", "path mismatch")


def _object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _check_json_depth(value: Any, *, depth: int = 0, location: str) -> None:
    if depth > MAX_JSON_DEPTH:
        raise VerificationError(
            "json_too_deep",
            f"{location} exceeds max nesting depth {MAX_JSON_DEPTH}",
            location=location,
        )
    if isinstance(value, Mapping):
        for item in value.values():
            _check_json_depth(item, depth=depth + 1, location=location)
    elif isinstance(value, list):
        for item in value:
            _check_json_depth(item, depth=depth + 1, location=location)


def _reject_nonfinite(value: Any, *, location: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise VerificationError(
            "nonfinite_number",
            f"{location} contains non-finite number",
            location=location,
        )
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > MAX_SAFE_INT:
            raise VerificationError(
                "integer_too_large",
                f"{location} integer out of bounds",
                location=location,
            )
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_nonfinite(item, location=f"{location}")
            del key
    elif isinstance(value, list):
        for item in value:
            _reject_nonfinite(item, location=location)


def _loads_json(text: str, *, location: str) -> Any:
    try:
        value = json.loads(text, object_pairs_hook=_object_pairs_hook, parse_int=_parse_json_int)
    except RecursionError as exc:
        raise VerificationError(
            "json_too_deep",
            f"{location} exceeds max nesting depth",
            location=location,
        ) from exc
    except (ValueError, OverflowError, MemoryError) as exc:
        raise VerificationError(
            "json_parse_error",
            f"{location} JSON parse failed",
            location=location,
        ) from exc
    _check_json_depth(value, location=location)
    _reject_nonfinite(value, location=location)
    return value


def _parse_json_int(text: str) -> int:
    if len(text) > 20:
        raise ValueError("integer too large")
    value = int(text)
    if abs(value) > MAX_SAFE_INT:
        raise ValueError("integer too large")
    return value


def _parse_jsonl(
    text: str,
) -> list[tuple[int, str, Any | None, str]]:
    """Return rows of (line_no, raw, parsed_or_None, kind)."""
    rows: list[tuple[int, str, Any | None, str]] = []
    if not text:
        return rows
    lines = text.splitlines()
    if len(lines) > MAX_JSONL_RECORDS:
        raise VerificationError(
            "jsonl_too_many_records",
            f"journal exceeds {MAX_JSONL_RECORDS} records",
            location="journal",
        )
    for line_no, line in enumerate(lines, start=1):
        if not line.strip():
            rows.append((line_no, line, None, "blank"))
            continue
        try:
            parsed = _loads_json(line, location=f"journal:L{line_no}")
        except VerificationError as exc:
            if exc.code in {
                "json_too_deep",
                "nonfinite_number",
                "integer_too_large",
                "input_too_large",
            }:
                raise
            rows.append((line_no, line, None, "malformed"))
            continue
        if not isinstance(parsed, dict):
            rows.append((line_no, line, parsed, "non_object"))
            continue
        rows.append((line_no, line, parsed, "object"))
    return rows


def _parse_report_json(text: str, state: VerificationState) -> dict[str, Any]:
    value = _loads_json(text, location="report")
    if not isinstance(value, dict):
        raise VerificationError(
            "report_not_object",
            "report root must be a JSON object",
            location="report",
        )
    schema_version = value.get("schema_version")
    if type(schema_version) is not int or isinstance(schema_version, bool):
        raise VerificationError(
            "report_schema_unsupported",
            "unsupported report schema_version",
            location="report",
        )
    if schema_version != JOURNAL_SCHEMA_VERSION:
        raise VerificationError(
            "report_schema_unsupported",
            "unsupported report schema_version",
            location="report",
        )
    extra = set(value) - set(_REPORT_TOP_LEVEL)
    missing = set(_REPORT_TOP_LEVEL) - set(value)
    if extra or missing:
        raise VerificationError(
            "report_schema_unsupported",
            "report top-level schema mismatch",
            location="report",
        )
    state.pass_("report_json", "report", "parsed")
    return value


def _load_env_secrets() -> tuple[str, ...]:
    secrets: list[str] = []
    for name in ("TQ_USER", "TQ_PASS", "AK_DATABASE_URL"):
        value = os.environ.get(name)
        if isinstance(value, str) and value:
            secrets.append(value)
            if name == "AK_DATABASE_URL":
                try:
                    password = urlsplit(value).password
                except ValueError:
                    password = None
                if password:
                    secrets.append(unquote(password))
    return tuple(secrets)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _SENSITIVE_KEY_FRAGMENTS:
        return True
    return any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS)


def _sensitive_value_ok(value: Any) -> bool:
    if value is None:
        return True
    if value == "":
        return True
    if value == "[REDACTED]":
        return True
    return False


def _scan_structure_for_secrets(
    value: Any, *, location: str, findings: list[str]
) -> None:
    if isinstance(value, Mapping):
        for index, (key, item) in enumerate(value.items()):
            key_text = str(key)
            child = f"{location}/k{index}"
            if _is_sensitive_key(key_text) and not _sensitive_value_ok(item):
                findings.append(child)
            _scan_structure_for_secrets(item, location=child, findings=findings)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_structure_for_secrets(
                item, location=f"{location}[{index}]", findings=findings
            )


def _secret_byte_patterns(secret: str) -> list[bytes]:
    patterns: list[bytes] = []
    variants: list[str] = [secret]
    # JSON ensure_ascii escape form without surrounding quotes.
    try:
        escaped = json.dumps(secret, ensure_ascii=True)
        if len(escaped) >= 2 and escaped[0] == '"' and escaped[-1] == '"':
            variants.append(escaped[1:-1])
    except (TypeError, ValueError):
        pass
    for text in variants:
        for encoding in ("utf-8", "gbk", "cp936", "utf-16le", "utf-16be"):
            try:
                encoded = text.encode(encoding)
            except UnicodeEncodeError:
                continue
            if encoded and encoded not in patterns:
                patterns.append(encoded)
    return patterns


def _scan_parsed_strings_for_secrets(
    value: Any, *, location: str, secrets: Sequence[str], findings: list[str]
) -> None:
    if isinstance(value, str):
        for secret in secrets:
            if secret and secret in value:
                findings.append(location)
                return
    elif isinstance(value, Mapping):
        for index, (key, item) in enumerate(value.items()):
            child = f"{location}/k{index}"
            if isinstance(key, str):
                for secret in secrets:
                    if secret and secret in key:
                        findings.append(child)
                        break
            _scan_parsed_strings_for_secrets(
                item, location=child, secrets=secrets, findings=findings
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_parsed_strings_for_secrets(
                item,
                location=f"{location}[{index}]",
                secrets=secrets,
                findings=findings,
            )


def _scan_value_for_known_secrets(
    value: Any, *, secrets: Sequence[str], location: str
) -> list[str]:
    findings: list[str] = []
    if not secrets:
        return findings
    _scan_parsed_strings_for_secrets(
        value, location=location, secrets=secrets, findings=findings
    )
    return findings


def _redact_known_secrets(value: Any, *, secrets: Sequence[str]) -> Any:
    if not secrets:
        return value
    if isinstance(value, str):
        text = value
        for secret in secrets:
            if secret and secret in text:
                text = text.replace(secret, "[REDACTED]")
        return text
    if isinstance(value, Mapping):
        return {
            (
                _redact_known_secrets(key, secrets=secrets)
                if isinstance(key, str)
                else key
            ): _redact_known_secrets(item, secrets=secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_known_secrets(item, secrets=secrets) for item in value]
    return value


def _ascii_sanitize_tree(value: Any) -> Any:
    if isinstance(value, str):
        return "".join(ch if ord(ch) < 128 else "?" for ch in value)
    if isinstance(value, Mapping):
        return {
            _ascii_sanitize_tree(key) if isinstance(key, str) else key: _ascii_sanitize_tree(
                item
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_ascii_sanitize_tree(item) for item in value]
    return value


def _safe_text(value: str) -> str:
    return "".join(ch if ord(ch) < 128 else "?" for ch in str(value))


def _scan_secrets(
    state: VerificationState,
    *,
    journal_records: Sequence[tuple[int, str, Any | None, str]],
    report: Mapping[str, Any],
    raw_files: Mapping[str, bytes],
    stream_texts: Mapping[str, str],
    secrets: Sequence[str],
) -> str:
    findings: list[str] = []
    for line_no, _raw, parsed, kind in journal_records:
        if kind == "object" and isinstance(parsed, Mapping):
            _scan_structure_for_secrets(
                parsed, location=f"journal:L{line_no}", findings=findings
            )
            if secrets:
                _scan_parsed_strings_for_secrets(
                    parsed,
                    location=f"journal:L{line_no}",
                    secrets=secrets,
                    findings=findings,
                )
    _scan_structure_for_secrets(report, location="report", findings=findings)
    if secrets:
        _scan_parsed_strings_for_secrets(
            report, location="report", secrets=secrets, findings=findings
        )
        for name, blob in raw_files.items():
            for secret in secrets:
                for pattern in _secret_byte_patterns(secret):
                    if pattern in blob:
                        findings.append(f"{name}:bytes")
                        break
        for stream_name, text in stream_texts.items():
            for secret in secrets:
                if secret and secret in text:
                    findings.append(f"{stream_name}:text")
                    break

    if findings:
        unique_locations = sorted(set(findings))
        for location in unique_locations:
            state.fail("secret_leak", location, "sensitive material detected")
        return "failed"

    if secrets:
        state.pass_("secret_scan", "bundle", "clean")
        return "performed"
    state.pass_("secret_scan", "bundle", "structure-only")
    return "skipped"


def _validate_expected_params(expected: ExpectedParams) -> None:
    if expected.symbol is not None:
        if not isinstance(expected.symbol, str):
            raise VerificationError(
                "usage_error",
                "expected symbol invalid",
                location="expected",
            )
        if expected.symbol != expected.symbol.strip() or not expected.symbol.strip():
            raise VerificationError(
                "usage_error",
                "expected symbol invalid",
                location="expected",
            )
        try:
            _require_concrete_symbol(expected.symbol)
        except ValueError as exc:
            raise VerificationError(
                "usage_error",
                "expected symbol invalid",
                location="expected",
            ) from exc
    for label, value in (
        ("start_date", expected.start_date),
        ("end_date", expected.end_date),
    ):
        if value is not None and not _is_iso_date(value):
            raise VerificationError(
                "usage_error",
                f"expected {label} invalid",
                location="expected",
            )
    if (
        expected.start_date is not None
        and expected.end_date is not None
        and expected.start_date > expected.end_date
    ):
        raise VerificationError(
            "usage_error",
            "expected start_date must be <= end_date",
            location="expected",
        )
    for label, value in (
        ("duration", expected.duration),
        ("fast_window", expected.fast_window),
        ("slow_window", expected.slow_window),
        ("max_position", expected.max_position),
    ):
        if value is None:
            continue
        if not _is_positive_int(value):
            raise VerificationError(
                "usage_error",
                f"expected {label} invalid",
                location="expected",
            )
    if (
        expected.fast_window is not None
        and expected.slow_window is not None
        and expected.fast_window >= expected.slow_window
    ):
        raise VerificationError(
            "usage_error",
            "expected fast_window must be < slow_window",
            location="expected",
        )
    if expected.run_id is not None:
        if not isinstance(expected.run_id, str):
            raise VerificationError(
                "usage_error",
                "expected run_id invalid",
                location="expected",
            )
        if expected.run_id != expected.run_id.strip() or not expected.run_id.strip():
            raise VerificationError(
                "usage_error",
                "expected run_id invalid",
                location="expected",
            )


def _validate_min_decisions(min_decisions: int) -> None:
    if isinstance(min_decisions, bool) or not isinstance(min_decisions, int):
        raise VerificationError(
            "usage_error",
            "min_decisions invalid",
            location="expected",
        )
    if min_decisions <= 0 or min_decisions > MAX_SAFE_ABS_POSITION:
        raise VerificationError(
            "usage_error",
            "min_decisions invalid",
            location="expected",
        )


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_nonneg_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_iso_date(value: Any) -> bool:
    if not isinstance(value, str) or not _ISO_DATE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_aware_iso_datetime(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return False
    text = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _require_concrete_symbol(symbol: str) -> str:
    value = (symbol or "").strip()
    if not value:
        raise ValueError("symbol is empty")
    if value.startswith("KQ.") or "@" in value:
        raise ValueError("a concrete delivery-month contract is required")
    exchange, separator, instrument = value.partition(".")
    if not separator or not exchange or not instrument:
        raise ValueError("symbol must use EXCHANGE.instrument format")
    return value


def _exact_type(value: Any) -> type:
    return type(value)


def _type_sensitive_equal(left: Any, right: Any) -> bool:
    """Recursive equality that rejects bool/int/float cross-types."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        if set(left) != set(right):
            return False
        for key in left:
            if not _type_sensitive_equal(left[key], right[key]):
                return False
        return True
    if isinstance(left, list):
        if len(left) != len(right):
            return False
        return all(
            _type_sensitive_equal(item_left, item_right)
            for item_left, item_right in zip(left, right)
        )
    return left == right


def _schema_error(code: str, message: str, *, location: str) -> None:
    raise VerificationError(code, message, location=location)


def _require_exact_keys(
    value: Mapping[str, Any],
    required: frozenset[str],
    *,
    location: str,
    code: str,
    optional: frozenset[str] = frozenset(),
) -> None:
    keys = set(value)
    missing = required - keys
    extra = keys - required - optional
    if missing or extra:
        _schema_error(code, "schema key set mismatch", location=location)


def _require_schema_version_int(value: Any, *, location: str, code: str) -> int:
    if type(value) is not int or isinstance(value, bool):
        _schema_error(code, "schema_version must be int", location=location)
    if value != JOURNAL_SCHEMA_VERSION:
        _schema_error(code, "unsupported schema_version", location=location)
    return value


def _require_count_int(value: Any, *, location: str, code: str) -> None:
    if not _is_int(value):
        _schema_error(code, "count must be non-bool int", location=location)


def _require_str(value: Any, *, location: str, code: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        _schema_error(code, "string required", location=location)
    if not allow_empty and not value:
        _schema_error(code, "non-empty string required", location=location)
    return value


def _require_bool(value: Any, *, location: str, code: str) -> bool:
    if type(value) is not bool:
        _schema_error(code, "bool required", location=location)
    return value


def _require_nullable_finite(value: Any, *, location: str, code: str) -> None:
    if value is not None and not _is_finite_number(value):
        _schema_error(code, "finite number or null required", location=location)


def _require_finite_number(value: Any, *, location: str, code: str) -> None:
    if not _is_finite_number(value):
        _schema_error(code, "finite number required", location=location)


def _validate_manifest_schema(manifest: Mapping[str, Any], *, location: str) -> None:
    if not isinstance(manifest, Mapping):
        _schema_error("manifest_schema", "manifest must be object", location=location)
    _require_exact_keys(
        manifest,
        _MANIFEST_REQUIRED,
        location=location,
        code="manifest_schema",
        optional=_MANIFEST_OPTIONAL,
    )
    if manifest.get("event") != "run_manifest":
        _schema_error("manifest_schema", "event must be run_manifest", location=location)
    _require_schema_version_int(
        manifest.get("schema_version"),
        location=f"{location}.schema_version",
        code="manifest_schema_unsupported",
    )
    _require_str(manifest.get("run_id"), location=f"{location}.run_id", code="manifest_schema")
    created_at = manifest.get("created_at")
    if not _is_aware_iso_datetime(created_at):
        _schema_error(
            "manifest_schema",
            "created_at must be timezone-aware ISO-8601",
            location=f"{location}.created_at",
        )
    _require_str(manifest.get("mode"), location=f"{location}.mode", code="manifest_schema")
    _require_str(manifest.get("symbol"), location=f"{location}.symbol", code="manifest_schema")
    if "market_symbol" in manifest:
        _require_str(
            manifest.get("market_symbol"),
            location=f"{location}.market_symbol",
            code="manifest_schema",
        )
    strategy = manifest.get("strategy")
    if not isinstance(strategy, Mapping):
        _schema_error("manifest_schema", "strategy object required", location=location)
    _require_exact_keys(
        strategy, _STRATEGY_KEYS, location=f"{location}.strategy", code="manifest_schema"
    )
    if strategy.get("name") != "dual_ma" or not isinstance(strategy.get("name"), str):
        _schema_error(
            "manifest_schema",
            "strategy.name invalid type/value",
            location=f"{location}.strategy.name",
        )
    if not _is_positive_int(strategy.get("fast_window")):
        _schema_error(
            "manifest_schema",
            "fast_window type",
            location=f"{location}.strategy.fast_window",
        )
    if not _is_positive_int(strategy.get("slow_window")):
        _schema_error(
            "manifest_schema",
            "slow_window type",
            location=f"{location}.strategy.slow_window",
        )
    execution = manifest.get("execution")
    if not isinstance(execution, Mapping):
        _schema_error("manifest_schema", "execution object required", location=location)
    _require_exact_keys(
        execution,
        _EXECUTION_KEYS,
        location=f"{location}.execution",
        code="manifest_schema",
    )
    if not _is_positive_int(execution.get("duration_seconds")):
        _schema_error(
            "manifest_schema",
            "duration_seconds type",
            location=f"{location}.execution.duration_seconds",
        )
    if not _is_positive_int(execution.get("max_abs_position")):
        _schema_error(
            "manifest_schema",
            "max_abs_position type",
            location=f"{location}.execution.max_abs_position",
        )
    if type(execution.get("allow_short")) is not bool:
        _schema_error(
            "manifest_schema",
            "allow_short type",
            location=f"{location}.execution.allow_short",
        )
    if not _is_finite_number(execution.get("initial_balance")):
        _schema_error(
            "manifest_schema",
            "initial_balance type",
            location=f"{location}.execution.initial_balance",
        )
    backtest = manifest.get("backtest")
    if not isinstance(backtest, Mapping):
        _schema_error("manifest_schema", "backtest object required", location=location)
    _require_exact_keys(
        backtest, _BACKTEST_KEYS, location=f"{location}.backtest", code="manifest_schema"
    )
    if not isinstance(backtest.get("start"), str):
        _schema_error(
            "manifest_schema",
            "backtest.start type",
            location=f"{location}.backtest.start",
        )
    if not isinstance(backtest.get("end"), str):
        _schema_error(
            "manifest_schema",
            "backtest.end type",
            location=f"{location}.backtest.end",
        )


def _validate_started_schema(record: Mapping[str, Any], *, location: str) -> None:
    _require_exact_keys(record, _STARTED_KEYS, location=location, code="started_schema")
    if record.get("event") != "run_started":
        _schema_error("started_schema", "event", location=location)
    for field_name in ("mode", "symbol", "run_id"):
        _require_str(record.get(field_name), location=f"{location}.{field_name}", code="started_schema")
    if not _is_positive_int(record.get("duration_seconds")):
        _schema_error("started_schema", "duration_seconds", location=location)
    for field_name in ("balance", "available", "margin"):
        _require_finite_number(
            record.get(field_name), location=f"{location}.{field_name}", code="started_schema"
        )
    if not _is_int(record.get("position")):
        _schema_error("started_schema", "position", location=f"{location}.position")


def _validate_finished_schema(record: Mapping[str, Any], *, location: str) -> None:
    _require_exact_keys(record, _FINISHED_KEYS, location=location, code="finished_schema")
    if record.get("event") != "run_finished":
        _schema_error("finished_schema", "event", location=location)
    for field_name in ("mode", "symbol", "run_id", "reason"):
        _require_str(
            record.get(field_name), location=f"{location}.{field_name}", code="finished_schema"
        )
    if not _is_positive_int(record.get("duration_seconds")):
        _schema_error("finished_schema", "duration_seconds", location=location)
    for field_name in ("balance", "available", "margin"):
        _require_finite_number(
            record.get(field_name), location=f"{location}.{field_name}", code="finished_schema"
        )
    for field_name in ("position", "updates", "decisions", "target_changes", "final_position"):
        if not _is_int(record.get(field_name)):
            _schema_error(
                "finished_schema",
                field_name,
                location=f"{location}.{field_name}",
            )


def _validate_decision_schema(
    decision: Mapping[str, Any],
    *,
    location: str,
) -> None:
    _require_exact_keys(decision, _DECISION_KEYS, location=location, code="decision_schema")
    if decision.get("event") != "decision":
        _schema_error("decision_schema", "event", location=location)
    for field_name in ("mode", "symbol", "run_id", "bar_datetime", "signal_reason", "risk_reason"):
        _require_str(
            decision.get(field_name),
            location=f"{location}.{field_name}",
            code="decision_schema",
            allow_empty=field_name in {"signal_reason", "risk_reason"},
        )
    if not _is_positive_int(decision.get("duration_seconds")):
        _schema_error("decision_schema", "duration_seconds", location=location)
    for field_name in ("balance", "available", "margin"):
        _require_nullable_finite(
            decision.get(field_name),
            location=f"{location}.{field_name}",
            code="decision_schema",
        )
    for field_name in ("close", "fast_ma", "slow_ma", "last_price"):
        _require_nullable_finite(
            decision.get(field_name),
            location=f"{location}.{field_name}",
            code="decision_schema",
        )
    action = decision.get("action")
    if not isinstance(action, str) or action not in _DECISION_ACTIONS:
        _schema_error("decision_schema", "action", location=f"{location}.action")
    block_reason = decision.get("block_reason")
    if block_reason is not None and not isinstance(block_reason, str):
        _schema_error("decision_schema", "block_reason type", location=location)

    position = decision.get("position")
    current_position = decision.get("current_position")
    signal_target = decision.get("signal_target")
    risk_target = decision.get("risk_target")
    requested_target = decision.get("requested_target")

    for field_name, value in (
        ("position", position),
        ("current_position", current_position),
        ("signal_target", signal_target),
        ("risk_target", risk_target),
        ("requested_target", requested_target),
    ):
        if value is None:
            continue
        if not _is_int(value):
            _schema_error(
                "decision_schema",
                f"{field_name} type",
                location=f"{location}.{field_name}",
            )

    # Conditional schema/nullability by action.
    if action == "set_target":
        if not _is_int(risk_target) or not _is_int(requested_target):
            _schema_error("decision_schema", "set_target targets", location=location)
        if block_reason is not None:
            _schema_error("decision_schema", "set_target block_reason", location=location)
    elif action == "target_already_requested":
        if not _is_int(risk_target) or not _is_int(requested_target):
            _schema_error(
                "decision_schema",
                "target_already_requested targets",
                location=location,
            )
        if block_reason is not None:
            _schema_error(
                "decision_schema",
                "target_already_requested block_reason",
                location=location,
            )
    elif action == "already_at_target":
        if (
            not _is_int(risk_target)
            or not _is_int(requested_target)
            or not _is_int(current_position)
        ):
            _schema_error("decision_schema", "already_at_target targets", location=location)
        if block_reason is not None:
            _schema_error(
                "decision_schema",
                "already_at_target block_reason",
                location=location,
            )
    elif action == "no_signal":
        if signal_target is not None or risk_target is not None:
            # impossible relation is invalid, not schema if types ok
            pass
        if block_reason is not None:
            _schema_error("decision_schema", "no_signal block_reason", location=location)
    elif action == "blocked":
        if block_reason not in _BLOCK_REASONS:
            _schema_error("decision_schema", "blocked reason", location=location)


def _validate_report_family_schemas(report: Mapping[str, Any]) -> None:
    schema_version = report.get("schema_version")
    _require_schema_version_int(
        schema_version, location="report.schema_version", code="report_schema_unsupported"
    )
    if not isinstance(report.get("run_id"), str) or not report.get("run_id"):
        _schema_error("report_schema", "run_id", location="report.run_id")
    if not isinstance(report.get("journal"), str) or not report.get("journal"):
        _schema_error("report_schema", "journal", location="report.journal")

    identity = report.get("identity")
    if not isinstance(identity, Mapping):
        _schema_error("report_schema", "identity", location="report.identity")
    _require_exact_keys(
        identity, _REPORT_IDENTITY_KEYS, location="report.identity", code="report_schema"
    )
    for key in _REPORT_IDENTITY_KEYS:
        value = identity.get(key)
        if value is not None and not isinstance(value, str):
            _schema_error("report_schema", key, location=f"report.identity.{key}")

    integrity = report.get("integrity")
    if not isinstance(integrity, Mapping):
        _schema_error("report_schema", "integrity", location="report.integrity")
    _require_exact_keys(
        integrity, _REPORT_INTEGRITY_KEYS, location="report.integrity", code="report_schema"
    )
    for key in _REPORT_INTEGRITY_KEYS:
        _require_count_int(
            integrity.get(key), location=f"report.integrity.{key}", code="report_schema"
        )

    lifecycle = report.get("lifecycle")
    if not isinstance(lifecycle, Mapping):
        _schema_error("report_schema", "lifecycle", location="report.lifecycle")
    _require_exact_keys(
        lifecycle, _REPORT_LIFECYCLE_KEYS, location="report.lifecycle", code="report_schema"
    )
    if not isinstance(lifecycle.get("state"), str):
        _schema_error("report_schema", "state", location="report.lifecycle.state")
    if type(lifecycle.get("run_complete")) is not bool:
        _schema_error(
            "report_schema",
            "run_complete",
            location="report.lifecycle.run_complete",
        )
    shutdown_complete = lifecycle.get("shutdown_complete")
    if shutdown_complete is not None and type(shutdown_complete) is not bool:
        _schema_error(
            "report_schema",
            "shutdown_complete",
            location="report.lifecycle.shutdown_complete",
        )

    events = report.get("events")
    if not isinstance(events, Mapping):
        _schema_error("report_schema", "events", location="report.events")
    _require_exact_keys(
        events, _REPORT_EVENTS_KEYS, location="report.events", code="report_schema"
    )
    for map_name in _REPORT_EVENTS_KEYS:
        mapping = events.get(map_name)
        if not isinstance(mapping, Mapping):
            _schema_error("report_schema", map_name, location=f"report.events.{map_name}")
        for key, value in mapping.items():
            if not isinstance(key, str):
                _schema_error(
                    "report_schema",
                    "event map key",
                    location=f"report.events.{map_name}",
                )
            _require_count_int(
                value,
                location=f"report.events.{map_name}.{key}",
                code="report_schema",
            )

    accounts = report.get("account_snapshots")
    if not isinstance(accounts, Mapping):
        _schema_error(
            "report_schema",
            "account_snapshots",
            location="report.account_snapshots",
        )
    _require_exact_keys(
        accounts,
        _REPORT_ACCOUNT_KEYS,
        location="report.account_snapshots",
        code="report_schema",
    )
    for snap_name in ("start", "last_recorded"):
        snap = accounts.get(snap_name)
        if snap is None:
            continue
        if not isinstance(snap, Mapping):
            _schema_error(
                "report_schema",
                snap_name,
                location=f"report.account_snapshots.{snap_name}",
            )
        _require_exact_keys(
            snap,
            frozenset(_SNAPSHOT_FIELDS),
            location=f"report.account_snapshots.{snap_name}",
            code="report_schema",
        )
        for field_name in ("balance", "available", "margin"):
            if not _is_finite_number(snap.get(field_name)):
                _schema_error(
                    "report_schema",
                    field_name,
                    location=f"report.account_snapshots.{snap_name}.{field_name}",
                )
        if not _is_int(snap.get("position")):
            _schema_error(
                "report_schema",
                "position",
                location=f"report.account_snapshots.{snap_name}.position",
            )

    run_summary = report.get("run_summary")
    if not isinstance(run_summary, Mapping):
        _schema_error("report_schema", "run_summary", location="report.run_summary")
    _require_exact_keys(
        run_summary,
        _REPORT_RUN_SUMMARY_KEYS,
        location="report.run_summary",
        code="report_schema",
    )
    if not isinstance(run_summary.get("reason"), str):
        _schema_error("report_schema", "reason", location="report.run_summary.reason")
    for field_name in ("updates", "decisions", "target_changes", "final_position"):
        if not _is_int(run_summary.get(field_name)):
            _schema_error(
                "report_schema",
                field_name,
                location=f"report.run_summary.{field_name}",
            )

    manifest = report.get("manifest")
    if not isinstance(manifest, Mapping):
        _schema_error("report_schema", "manifest", location="report.manifest")
    _validate_manifest_schema(manifest, location="report.manifest")


def _validate_selected_event_schemas(
    selected: Sequence[tuple[int, Mapping[str, Any]]],
) -> None:
    """Validate intrinsic schemas before lifecycle relationships can short-circuit."""
    for line_no, record in selected:
        event = record.get("event")
        location = f"journal:L{line_no}"
        if event == "run_manifest":
            _validate_manifest_schema(record, location=location)
        elif event == "run_started":
            _validate_started_schema(record, location=location)
        elif event == "decision":
            _validate_decision_schema(record, location=location)
        elif event == "run_finished":
            _validate_finished_schema(record, location=location)
        elif event == "shutdown":
            # A shutdown record is a known event but invalid lifecycle evidence
            # for backtests. Its presence remains a class-1 relationship error.
            continue


def _recompute_risk_decision(
    signal_target: int | None,
    *,
    max_abs_position: int,
    allow_short: bool,
) -> tuple[int | None, str]:
    """Mirror risk.limit_target_position without trusting recorded risk fields."""
    if signal_target is None:
        return None, "no_signal"
    lower_bound = -max_abs_position if allow_short else 0
    target = min(max_abs_position, max(lower_bound, signal_target))
    if target == signal_target:
        return target, "allowed"
    if signal_target < lower_bound:
        reason = "clamped_min" if allow_short else "short_blocked_to_flat"
    else:
        reason = "clamped_max"
    return target, reason


def _validate_journal_and_report(
    rows: Sequence[tuple[int, str, Any | None, str]],
    report: Mapping[str, Any],
    journal_path: Path,
    journal_canonical: str,
    state: VerificationState,
    *,
    expected: ExpectedParams,
    min_decisions: int,
) -> dict[str, Any] | None:
    blank_lines = 0
    malformed_lines = 0
    non_object_lines = 0
    unknown_events = 0
    foreign_records = 0
    selected: list[tuple[int, dict[str, Any]]] = []
    event_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    blocked_reason_counts: Counter[str] = Counter()
    manifests: list[dict[str, Any]] = []
    starts: list[dict[str, Any]] = []
    finishes: list[dict[str, Any]] = []
    shutdowns: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    last_snapshot: dict[str, Any] | None = None
    mode_values: set[str] = set()
    symbol_values: set[str] = set()

    selected_run_id = expected.run_id
    if selected_run_id is None:
        report_run_id = report.get("run_id")
        if isinstance(report_run_id, str) and report_run_id:
            selected_run_id = report_run_id
    if selected_run_id is None:
        for _line_no, _raw, parsed, kind in rows:
            if kind != "object" or not isinstance(parsed, dict):
                continue
            if parsed.get("event") == "run_manifest":
                candidate = parsed.get("run_id")
                if isinstance(candidate, str) and candidate:
                    selected_run_id = candidate
                    break

    for line_no, _raw, parsed, kind in rows:
        if kind == "blank":
            blank_lines += 1
            state.fail("journal_blank_line", f"journal:L{line_no}")
            continue
        if kind == "malformed":
            malformed_lines += 1
            state.fail("journal_malformed", f"journal:L{line_no}")
            continue
        if kind == "non_object":
            non_object_lines += 1
            state.fail("journal_non_object", f"journal:L{line_no}")
            continue
        assert isinstance(parsed, dict)
        run_id = parsed.get("run_id")
        if selected_run_id is not None and run_id != selected_run_id:
            foreign_records += 1
            continue
        event = parsed.get("event")
        if not isinstance(event, str) or event not in _KNOWN_EVENTS:
            unknown_events += 1
            state.fail("journal_unknown_event", f"journal:L{line_no}")
            continue
        selected.append((line_no, parsed))
        event_counts[event] += 1
        _collect_identity(parsed, mode_values, symbol_values)
        if event == "run_manifest":
            manifests.append(parsed)
        elif event == "run_started":
            starts.append(parsed)
        elif event == "run_finished":
            finishes.append(parsed)
        elif event == "shutdown":
            shutdowns.append(parsed)
        elif event == "decision":
            decisions.append(parsed)
            action = parsed.get("action")
            if isinstance(action, str):
                action_counts[action] += 1
            blocked_reason = parsed.get("block_reason")
            if isinstance(blocked_reason, str):
                blocked_reason_counts[blocked_reason] += 1

    if foreign_records:
        state.pass_(
            "foreign_records_detected",
            "journal",
            f"count={foreign_records}",
        )
    else:
        state.pass_("foreign_records_detected", "journal", "count=0")

    _validate_selected_event_schemas(selected)
    state.pass_("selected_event_schema", "journal", f"count={len(selected)}")

    if blank_lines == 0:
        state.pass_("journal_blank_lines", "journal", "0")
    if malformed_lines == 0:
        state.pass_("journal_malformed_lines", "journal", "0")
    if non_object_lines == 0:
        state.pass_("journal_non_object_lines", "journal", "0")
    if unknown_events == 0:
        state.pass_("journal_unknown_events", "journal", "0")

    # Lifecycle: exactly manifest, start, 0+ decision, finish. Nothing after finish.
    # shutdown_count must be 0 for backtest evidence.
    order_ok = True
    phase = 0  # 0=manifest, 1=start, 2=decisions, 3=finished
    for line_no, record in selected:
        event = record["event"]
        if event == "run_manifest":
            if phase != 0:
                order_ok = False
                state.fail("lifecycle_order", f"journal:L{line_no}", "manifest out of order")
                break
            phase = 1
        elif event == "run_started":
            if phase != 1:
                order_ok = False
                state.fail("lifecycle_order", f"journal:L{line_no}", "start out of order")
                break
            phase = 2
        elif event == "decision":
            if phase != 2:
                order_ok = False
                state.fail("lifecycle_order", f"journal:L{line_no}", "decision out of order")
                break
        elif event == "run_finished":
            if phase != 2:
                order_ok = False
                state.fail("lifecycle_order", f"journal:L{line_no}", "finish out of order")
                break
            phase = 3
        elif event == "shutdown":
            order_ok = False
            state.fail("lifecycle_order", f"journal:L{line_no}", "shutdown not allowed")
            break
        else:
            order_ok = False
            state.fail("lifecycle_order", f"journal:L{line_no}", f"unexpected {event}")
            break
    if order_ok and phase == 3:
        state.pass_("lifecycle_order", "journal", "ok")
    elif order_ok:
        state.fail("lifecycle_order", "journal", f"incomplete phase={phase}")

    if len(manifests) != 1:
        state.fail("manifest_count", "journal", f"count={len(manifests)}")
    else:
        state.pass_("manifest_count", "journal", "1")
    if len(starts) != 1:
        state.fail("run_started_count", "journal", f"count={len(starts)}")
    else:
        state.pass_("run_started_count", "journal", "1")
    if len(finishes) != 1:
        state.fail("run_finished_count", "journal", f"count={len(finishes)}")
    else:
        state.pass_("run_finished_count", "journal", "1")
    if len(shutdowns) != 0:
        state.fail("shutdown_count", "journal", f"count={len(shutdowns)}")
    else:
        state.pass_("shutdown_count", "journal", "0")

    if not manifests or not starts or not finishes:
        return None

    manifest = manifests[0]
    start = starts[0]
    finish = finishes[0]
    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        state.fail("run_id", "journal.manifest", "missing")
        return None
    if expected.run_id is not None and run_id != expected.run_id:
        state.fail("run_id_expected", "journal.manifest", "mismatch")
    state.run_id = run_id

    state.pass_("schema_version", "journal.manifest", str(JOURNAL_SCHEMA_VERSION))
    state.pass_("manifest_schema", "journal.manifest", "ok")

    mode = manifest.get("mode")
    symbol = manifest.get("symbol")
    if mode != "backtest":
        state.fail("mode", "journal.manifest", "invalid")
    else:
        state.pass_("mode", "journal.manifest", "backtest")

    if not isinstance(symbol, str) or not symbol:
        state.fail("symbol", "journal.manifest", "missing")
        symbol = None
    else:
        try:
            symbol = _require_concrete_symbol(symbol)
            state.pass_("symbol", "journal.manifest", "ok")
        except ValueError:
            state.fail("symbol", "journal.manifest", "not concrete")
            symbol = None
        if symbol is not None and expected.symbol is not None and symbol != expected.symbol:
            state.fail("symbol_expected", "journal.manifest", "mismatch")

    strategy = manifest.get("strategy")
    execution = manifest.get("execution")
    backtest = manifest.get("backtest")
    assert isinstance(strategy, Mapping)
    assert isinstance(execution, Mapping)
    assert isinstance(backtest, Mapping)

    strategy_name = strategy.get("name")
    if strategy_name != "dual_ma":
        state.fail("strategy_name", "journal.manifest", "invalid")
    else:
        state.pass_("strategy_name", "journal.manifest", "dual_ma")

    duration = execution.get("duration_seconds")
    fast_window = strategy.get("fast_window")
    slow_window = strategy.get("slow_window")
    max_abs_position = execution.get("max_abs_position")
    allow_short = execution.get("allow_short")
    initial_balance = execution.get("initial_balance")
    start_date = backtest.get("start")
    end_date = backtest.get("end")

    if (
        _is_positive_int(fast_window)
        and _is_positive_int(slow_window)
        and not (fast_window < slow_window)
    ):
        state.fail("window_order", "journal.manifest", "fast>=slow")
    if allow_short is not False:
        state.fail("allow_short", "journal.manifest", "invalid")
    else:
        state.pass_("allow_short", "journal.manifest", "false")
    if not _is_finite_number(initial_balance) or float(initial_balance) <= 0:
        state.fail("initial_balance", "journal.manifest", "invalid")
    else:
        state.pass_("initial_balance", "journal.manifest", "ok")
    if not _is_iso_date(start_date):
        state.fail("start_date", "journal.manifest", "invalid")
    if not _is_iso_date(end_date):
        state.fail("end_date", "journal.manifest", "invalid")
    if _is_iso_date(start_date) and _is_iso_date(end_date):
        if start_date > end_date:
            state.fail("date_order", "journal.manifest", "start>end")
        else:
            state.pass_("date_order", "journal.manifest", "ok")

    if expected.duration is not None and duration != expected.duration:
        state.fail("duration_expected", "journal.manifest", "mismatch")
    if expected.fast_window is not None and fast_window != expected.fast_window:
        state.fail("fast_window_expected", "journal.manifest", "mismatch")
    if expected.slow_window is not None and slow_window != expected.slow_window:
        state.fail("slow_window_expected", "journal.manifest", "mismatch")
    if expected.max_position is not None and max_abs_position != expected.max_position:
        state.fail("max_position_expected", "journal.manifest", "mismatch")
    if expected.start_date is not None and start_date != expected.start_date:
        state.fail("start_date_expected", "journal.manifest", "mismatch")
    if expected.end_date is not None and end_date != expected.end_date:
        state.fail("end_date_expected", "journal.manifest", "mismatch")

    state.pass_("started_schema", "journal.run_started", "ok")
    state.pass_("finished_schema", "journal.run_finished", "ok")

    # Identity: every selected event must explicitly carry run_id/mode/symbol.
    # Non-manifest events must explicitly carry duration_seconds.
    identity_ok = True
    for line_no, record in selected:
        event = record.get("event")
        if record.get("run_id") != run_id or not isinstance(record.get("run_id"), str):
            identity_ok = False
            state.fail("run_id_consistency", f"journal:L{line_no}", "missing or mismatch")
        if record.get("mode") != mode or not isinstance(record.get("mode"), str):
            identity_ok = False
            state.fail("mode_consistency", f"journal:L{line_no}", "missing or mismatch")
        if symbol is not None:
            if record.get("symbol") != symbol or not isinstance(record.get("symbol"), str):
                identity_ok = False
                state.fail("symbol_consistency", f"journal:L{line_no}", "missing or mismatch")
        if event != "run_manifest":
            if record.get("duration_seconds") != duration or not _is_positive_int(
                record.get("duration_seconds")
            ):
                identity_ok = False
                state.fail(
                    "duration_consistency",
                    f"journal:L{line_no}",
                    "missing or mismatch",
                )
    if identity_ok:
        state.pass_("identity_consistency", "journal", "ok")

    report_run_id = report.get("run_id")
    if report_run_id != run_id:
        state.fail("report_run_id", "report.run_id", "mismatch")
    report_manifest = report.get("manifest")
    if not isinstance(report_manifest, dict) or report_manifest.get("run_id") != run_id:
        state.fail("report_manifest_run_id", "report.manifest", "mismatch")
    report_identity = report.get("identity")
    if not isinstance(report_identity, dict):
        state.fail("report_identity", "report.identity", "invalid")
    else:
        if report_identity.get("mode") != mode:
            state.fail("report_identity_mode", "report.identity", "mismatch")
        if symbol is not None and report_identity.get("symbol") != symbol:
            state.fail("report_identity_symbol", "report.identity", "mismatch")

    initial_snapshot = _require_snapshot(start, state, location="journal.run_started")
    finish_snapshot = _require_snapshot(finish, state, location="journal.run_finished")

    # Decision semantics (runner.py _process_update / _emit protocol).
    bar_ids: set[str] = set()
    set_target_count = 0
    set_target_to_zero = 0
    last_requested_target: int | None = None
    target_task_finished_observed = False
    for index, decision in enumerate(decisions):
        location = f"journal.decision[{index}]"
        bar_id = decision.get("bar_datetime")
        if not isinstance(bar_id, str) or not bar_id or bar_id == "nan":
            state.fail("decision_bar_id", location, "empty")
        elif bar_id in bar_ids:
            state.fail("decision_bar_id_duplicate", location, "duplicate")
        else:
            bar_ids.add(bar_id)

        action = decision.get("action")
        block_reason = decision.get("block_reason")
        position = decision.get("position")
        current_position = decision.get("current_position")
        signal_target = decision.get("signal_target")
        risk_target = decision.get("risk_target")
        requested_target = decision.get("requested_target")
        last_price = decision.get("last_price")

        expected_risk_target, expected_risk_reason = _recompute_risk_decision(
            signal_target,
            max_abs_position=max_abs_position,
            allow_short=allow_short,
        )
        if risk_target != expected_risk_target:
            state.fail("decision_risk_target", location, "does not match signal")
        if decision.get("risk_reason") != expected_risk_reason:
            state.fail("decision_risk_reason", location, "does not match signal")

        for field_name, value in (
            ("position", position),
            ("current_position", current_position),
            ("signal_target", signal_target),
            ("risk_target", risk_target),
            ("requested_target", requested_target),
        ):
            if value is None:
                continue
            if not _is_int(value):
                continue
            if field_name != "signal_target":
                if allow_short is False and value < 0:
                    state.fail("long_only", location, field_name)
                if _is_positive_int(max_abs_position) and abs(value) > max_abs_position:
                    state.fail("max_position", location, field_name)

        if position != current_position:
            state.fail("decision_position_consistency", location, "position!=current_position")

        position_ready = _is_int(position) and _is_int(current_position)
        price_ready = _is_finite_number(last_price)
        if action == "blocked" and block_reason == "position_not_ready":
            if position is not None or current_position is not None:
                state.fail(
                    "blocked_position_not_ready",
                    location,
                    "position must be null",
                )
        else:
            if not position_ready:
                state.fail("decision_position_ready", location, "position must be ready")
            if action == "blocked" and block_reason == "quote_not_ready":
                if price_ready:
                    state.fail(
                        "blocked_quote_not_ready",
                        location,
                        "last_price still valid",
                    )
            elif not price_ready:
                state.fail("decision_price_ready", location, "last_price must be finite")

        expected_action: str
        expected_block_reason: str | None = None
        next_requested_target = last_requested_target
        if current_position is None:
            expected_action = "blocked"
            expected_block_reason = "position_not_ready"
        elif not price_ready:
            expected_action = "blocked"
            expected_block_reason = "quote_not_ready"
        elif expected_risk_target is None:
            expected_action = "no_signal"
        elif target_task_finished_observed or (
            action == "blocked" and block_reason == "target_task_finished"
        ):
            # Target-task completion is not independently observable in evidence.
            expected_action = "blocked"
            expected_block_reason = "target_task_finished"
            target_task_finished_observed = True
        elif expected_risk_target == last_requested_target:
            expected_action = "target_already_requested"
        elif (
            last_requested_target is None
            and expected_risk_target == current_position
        ):
            expected_action = "already_at_target"
            next_requested_target = expected_risk_target
        else:
            expected_action = "set_target"
            next_requested_target = expected_risk_target

        if action != expected_action or block_reason != expected_block_reason:
            state.fail("decision_action_transition", location, "runner branch mismatch")
        if requested_target != next_requested_target:
            state.fail("decision_requested_state", location, "state mismatch")
        last_requested_target = next_requested_target

        if action == "set_target":
            set_target_count += 1
            if risk_target != requested_target:
                state.fail("set_target_requested", location, "mismatch")
            elif risk_target == 0 and requested_target == 0:
                set_target_to_zero += 1
        elif action == "target_already_requested":
            if risk_target != requested_target:
                state.fail("target_already_requested", location, "risk!=requested")
        elif action == "already_at_target":
            if not (
                risk_target == requested_target == current_position
                and _is_int(risk_target)
            ):
                state.fail("already_at_target", location, "targets mismatch")
        elif action == "no_signal":
            if signal_target is not None or risk_target is not None:
                state.fail("no_signal", location, "targets must be null")
            if block_reason is not None:
                state.fail("no_signal", location, "block_reason")
        elif action == "blocked":
            if block_reason == "target_task_finished":
                if not _is_int(risk_target):
                    state.fail(
                        "blocked_target_task_finished",
                        location,
                        "risk_target must be int",
                    )
            elif block_reason not in {"position_not_ready", "quote_not_ready"}:
                state.fail("decision_block_reason", location, "invalid")

    state.pass_("decision_schema", "journal", "ok")

    if len(decisions) < min_decisions:
        state.fail(
            "min_decisions",
            "journal",
            f"count={len(decisions)} min={min_decisions}",
        )
    else:
        state.pass_("min_decisions", "journal", str(len(decisions)))

    if set_target_count < 1:
        state.fail("set_target_required", "journal", "missing")
    else:
        state.pass_("set_target_required", "journal", str(set_target_count))
    if set_target_to_zero < 1:
        state.fail("set_target_to_zero", "journal", "missing")
    else:
        state.pass_("set_target_to_zero", "journal", str(set_target_to_zero))

    if decisions:
        state.pass_("decision_semantics", "journal", f"count={len(decisions)}")

    finish_reason = finish.get("reason")
    if finish_reason != "backtest_finished":
        state.fail("finish_reason", "journal.run_finished", "invalid")
    else:
        state.pass_("finish_reason", "journal.run_finished", "backtest_finished")

    updates = finish.get("updates")
    decision_count = finish.get("decisions")
    target_changes = finish.get("target_changes")
    final_position = finish.get("final_position")
    finish_position = finish.get("position")

    if not _is_nonneg_int(updates):
        state.fail("finish_updates", "journal.run_finished", "invalid")
    if not _is_int(decision_count):
        state.fail("finish_decisions", "journal.run_finished", "invalid")
    elif decision_count != len(decisions):
        state.fail(
            "finish_decisions",
            "journal.run_finished",
            f"recorded={decision_count} actual={len(decisions)}",
        )
    else:
        state.pass_("finish_decisions", "journal.run_finished", str(decision_count))

    if not _is_int(target_changes):
        state.fail("finish_target_changes", "journal.run_finished", "invalid")
    elif target_changes != set_target_count:
        state.fail(
            "finish_target_changes",
            "journal.run_finished",
            f"recorded={target_changes} actual={set_target_count}",
        )
    else:
        state.pass_("finish_target_changes", "journal.run_finished", str(target_changes))

    if _is_int(updates) and _is_int(decision_count):
        if updates < decision_count:
            state.fail("updates_ge_decisions", "journal.run_finished", "violated")
        else:
            state.pass_("updates_ge_decisions", "journal.run_finished", "ok")

    if not _is_int(final_position) or not _is_int(finish_position):
        state.fail("final_position", "journal.run_finished", "invalid")
    else:
        if final_position != finish_position:
            state.fail("final_position_consistency", "journal.run_finished", "mismatch")
        else:
            state.pass_("final_position_consistency", "journal.run_finished", "ok")
        if allow_short is False and final_position < 0:
            state.fail("final_position_long_only", "journal.run_finished", "negative")
        if _is_positive_int(max_abs_position) and abs(final_position) > max_abs_position:
            state.fail("final_position_cap", "journal.run_finished", "exceeded")

    # last snapshot from selected order: last record that has full snapshot fields.
    last_snapshot = None
    for _line_no, record in selected:
        snap = _snapshot_if_complete(record)
        if snap is not None:
            last_snapshot = snap
    if last_snapshot is None:
        state.fail("last_snapshot", "journal", "missing")
        last_snapshot = finish_snapshot

    if initial_snapshot is not None and _is_finite_number(initial_balance):
        if not _numbers_close(initial_snapshot.get("balance"), initial_balance):
            state.fail("start_balance_vs_manifest", "journal.run_started", "mismatch")
        else:
            state.pass_("start_balance_vs_manifest", "journal.run_started", "ok")

    final_summary = {field_name: finish.get(field_name) for field_name in _SUMMARY_FIELDS}
    selected_records = len(selected)
    recomputed = {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "journal": journal_canonical,
        "run_id": run_id,
        "manifest": dict(manifest),
        "identity": {
            "mode": _single_value(mode_values),
            "symbol": _single_value(symbol_values),
        },
        "integrity": {
            "selected_records": selected_records,
            "foreign_records": foreign_records,
            "malformed_lines": malformed_lines,
            "non_object_lines": non_object_lines,
            "blank_lines": blank_lines,
            "unknown_events": unknown_events,
            "manifest_count": len(manifests),
            "run_started_count": len(starts),
            "run_finished_count": len(finishes),
            "shutdown_count": len(shutdowns),
        },
        "lifecycle": {
            "state": (
                "complete"
                if (
                    len(manifests) == 1
                    and len(starts) == 1
                    and len(finishes) == 1
                    and len(shutdowns) == 0
                    and mode == "backtest"
                    and isinstance(symbol, str)
                    and symbol
                )
                else "incomplete"
            ),
            "run_complete": len(starts) == 1 and len(finishes) == 1,
            "shutdown_complete": None,
        },
        "events": {
            "counts": dict(sorted(event_counts.items())),
            "decision_actions": dict(sorted(action_counts.items())),
            "blocked_reasons": dict(sorted(blocked_reason_counts.items())),
        },
        "account_snapshots": {
            "start": initial_snapshot,
            "last_recorded": last_snapshot,
        },
        "run_summary": final_summary,
    }

    if recomputed["lifecycle"]["state"] == "complete":
        state.pass_("lifecycle_complete", "journal", "complete")
    else:
        state.fail("lifecycle_complete", "journal", recomputed["lifecycle"]["state"])

    _compare_report(report, recomputed, journal_path, state)
    return recomputed


def _require_snapshot(
    record: Mapping[str, Any], state: VerificationState, *, location: str
) -> dict[str, Any] | None:
    snapshot: dict[str, Any] = {}
    ok = True
    for field_name in _SNAPSHOT_FIELDS:
        if field_name not in record:
            state.fail("snapshot_missing", location, field_name)
            ok = False
            continue
        value = record[field_name]
        if field_name == "position":
            if not _is_int(value):
                state.fail("snapshot_type", location, field_name)
                ok = False
                continue
        elif not _is_finite_number(value):
            state.fail("snapshot_type", location, field_name)
            ok = False
            continue
        snapshot[field_name] = value
    return snapshot if ok else None


def _snapshot_if_complete(record: Mapping[str, Any]) -> dict[str, Any] | None:
    if not all(field_name in record for field_name in _SNAPSHOT_FIELDS):
        return None
    snapshot: dict[str, Any] = {}
    for field_name in _SNAPSHOT_FIELDS:
        value = record[field_name]
        if field_name == "position":
            if not _is_int(value):
                return None
        elif not _is_finite_number(value):
            return None
        snapshot[field_name] = value
    return snapshot


def _compare_report(
    report: Mapping[str, Any],
    recomputed: Mapping[str, Any],
    journal_path: Path,
    state: VerificationState,
) -> None:
    for key in _REPORT_TOP_LEVEL:
        if key not in report:
            raise VerificationError(
                "report_schema",
                "missing top-level key",
                location=f"report.{key}",
            )

    for key in (
        "schema_version",
        "run_id",
        "identity",
        "integrity",
        "lifecycle",
        "events",
        "run_summary",
        "manifest",
    ):
        if not _type_sensitive_equal(report.get(key), recomputed.get(key)):
            state.fail("report_mismatch", f"report.{key}", "mismatch")
        else:
            state.pass_("report_match", f"report.{key}", "ok")

    # account_snapshots: full field set + numeric tolerance (never exact-dict first).
    report_accounts = report.get("account_snapshots")
    expected_accounts = recomputed.get("account_snapshots")
    if not isinstance(report_accounts, dict) or not isinstance(expected_accounts, dict):
        raise VerificationError(
            "report_schema",
            "account_snapshots invalid",
            location="report.account_snapshots",
        )
    if set(report_accounts) != {"start", "last_recorded"}:
        raise VerificationError(
            "report_schema",
            "account_snapshots keys",
            location="report.account_snapshots",
        )
    for snap_name in ("start", "last_recorded"):
        left = report_accounts.get(snap_name)
        right = expected_accounts.get(snap_name)
        if not isinstance(left, dict) or not isinstance(right, dict):
            raise VerificationError(
                "report_schema",
                "snapshot missing",
                location=f"report.account_snapshots.{snap_name}",
            )
        if set(left) != set(_SNAPSHOT_FIELDS) or set(right) != set(_SNAPSHOT_FIELDS):
            raise VerificationError(
                "report_schema",
                "snapshot fields",
                location=f"report.account_snapshots.{snap_name}",
            )
        snap_ok = True
        for field_name in _SNAPSHOT_FIELDS:
            left_value = left.get(field_name)
            right_value = right.get(field_name)
            if field_name == "position":
                if not _is_int(left_value) or not _is_int(right_value):
                    raise VerificationError(
                        "report_schema",
                        "position type",
                        location=f"report.account_snapshots.{snap_name}.position",
                    )
                if left_value != right_value:
                    snap_ok = False
                    state.fail(
                        "account_snapshot_mismatch",
                        f"report.account_snapshots.{snap_name}.{field_name}",
                        "mismatch",
                    )
            else:
                if not _numbers_close(left_value, right_value):
                    snap_ok = False
                    state.fail(
                        "account_snapshot_mismatch",
                        f"report.account_snapshots.{snap_name}.{field_name}",
                        "mismatch",
                    )
        if snap_ok:
            state.pass_(
                "account_snapshot_match",
                f"report.account_snapshots.{snap_name}",
                "ok",
            )

    report_journal = report.get("journal")
    if not isinstance(report_journal, str) or not report_journal:
        raise VerificationError(
            "report_schema",
            "journal missing",
            location="report.journal",
        )
    if _paths_bound_equal(report_journal, journal_path):
        state.pass_("report_journal", "report.journal", "matched")
    else:
        state.fail("report_journal", "report.journal", "path mismatch")


def _parse_and_validate_console_evidence(
    tokens: Mapping[str, str], state: VerificationState
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Parse each uniquely located console payload independently, exactly once."""
    done: dict[str, Any] | None = None
    done_payload = tokens.get("BACKTEST_DONE")
    if done_payload is not None:
        done = parse_run_summary(done_payload)
        state.pass_("done_parse", "stdout.BACKTEST_DONE", "ok")

    stat: dict[str, Any] | None = None
    stat_payload = tokens.get("BACKTEST_STAT")
    if stat_payload is not None:
        stat = parse_backtest_stat(stat_payload)
        state.pass_("stat_parse", "stdout.BACKTEST_STAT", "ok")
        _validate_stat_schema(stat, state)
    return done, stat


def _validate_done_and_stat(
    done: Mapping[str, Any] | None,
    stat: Mapping[str, Any] | None,
    summary: Mapping[str, Any],
    report: Mapping[str, Any],
    state: VerificationState,
    *,
    expected: ExpectedParams,
) -> None:
    if done is not None:
        # Exact field set already enforced by the intrinsic parser.
        run_summary = summary.get("run_summary") or {}
        for field_name in _SUMMARY_FIELDS:
            left = done.get(field_name)
            right = run_summary.get(field_name)
            if not _type_sensitive_equal(left, right):
                state.fail("done_summary_mismatch", f"done.{field_name}", "mismatch")
            else:
                state.pass_("done_summary_match", f"done.{field_name}", "ok")

    if stat is None:
        return

    manifest = summary.get("manifest") or {}
    backtest = manifest.get("backtest") if isinstance(manifest.get("backtest"), dict) else {}
    manifest_start = backtest.get("start")
    manifest_end = backtest.get("end")
    if stat.get("start_date") != manifest_start:
        state.fail("stat_start_date", "stdout.BACKTEST_STAT", "mismatch")
    else:
        state.pass_("stat_start_date", "stdout.BACKTEST_STAT", "ok")
    if stat.get("end_date") != manifest_end:
        state.fail("stat_end_date", "stdout.BACKTEST_STAT", "mismatch")
    else:
        state.pass_("stat_end_date", "stdout.BACKTEST_STAT", "ok")
    if expected.start_date is not None and stat.get("start_date") != expected.start_date:
        state.fail("stat_start_date_expected", "stdout.BACKTEST_STAT", "mismatch")
    if expected.end_date is not None and stat.get("end_date") != expected.end_date:
        state.fail("stat_end_date_expected", "stdout.BACKTEST_STAT", "mismatch")

    last = (summary.get("account_snapshots") or {}).get("last_recorded")
    start_snap = (summary.get("account_snapshots") or {}).get("start")
    report_last = (report.get("account_snapshots") or {}).get("last_recorded")
    report_start = (report.get("account_snapshots") or {}).get("start")
    if not isinstance(last, dict) or not isinstance(start_snap, dict):
        state.fail("account_snapshot_missing", "summary", "missing")
        return
    if not isinstance(report_last, dict) or not isinstance(report_start, dict):
        state.fail("account_snapshot_missing", "report", "missing")
        return

    for field_name in _SNAPSHOT_FIELDS:
        if field_name not in last or field_name not in report_last:
            state.fail(
                "account_snapshot_missing",
                f"last_recorded.{field_name}",
                "missing",
            )
            continue
        if field_name == "position":
            if last.get(field_name) != report_last.get(field_name):
                state.fail(
                    "account_snapshot_mismatch",
                    f"report.last_recorded.{field_name}",
                    "mismatch",
                )
        elif not _numbers_close(last.get(field_name), report_last.get(field_name)):
            state.fail(
                "account_snapshot_mismatch",
                f"report.last_recorded.{field_name}",
                "mismatch",
            )
        else:
            state.pass_(
                "account_snapshot_match",
                f"report.last_recorded.{field_name}",
                "ok",
            )

    execution = manifest.get("execution") if isinstance(manifest.get("execution"), dict) else {}
    initial_balance = execution.get("initial_balance")
    for stat_key, expected_value in (
        ("init_balance", initial_balance),
        ("start_balance", start_snap.get("balance")),
        ("balance", last.get("balance")),
        ("end_balance", last.get("balance")),
    ):
        if expected_value is None or stat_key not in stat:
            state.fail("native_stat_mismatch", f"stat.{stat_key}", "missing")
            continue
        if not _numbers_close(stat.get(stat_key), expected_value):
            state.fail("native_stat_mismatch", f"stat.{stat_key}", "mismatch")
        else:
            state.pass_("native_stat_match", f"stat.{stat_key}", "ok")

    state.pass_("stat_finite", "stdout.BACKTEST_STAT", "ok")


def _validate_stat_schema(stat: Mapping[str, Any], state: VerificationState) -> None:
    keys = set(stat)
    if not _STAT_REQUIRED.issubset(keys):
        raise VerificationError(
            "stat_schema",
            "missing required",
            location="stdout.BACKTEST_STAT",
        )
    unknown = keys - _STAT_ALLOWED
    if unknown:
        raise VerificationError(
            "stat_schema",
            "unknown keys",
            location="stdout.BACKTEST_STAT",
        )
    for key, value in stat.items():
        if key in _STAT_STRING_KEYS:
            if not isinstance(value, str):
                raise VerificationError(
                    "stat_type",
                    "invalid",
                    location=f"stat.{key}",
                )
            if key in {"start_date", "end_date"} and not _is_iso_date(value):
                raise VerificationError(
                    "stat_type",
                    "date",
                    location=f"stat.{key}",
                )
        elif key in _STAT_INT_KEYS:
            if not _is_int(value):
                raise VerificationError(
                    "stat_type",
                    "invalid",
                    location=f"stat.{key}",
                )
        else:
            if not _is_finite_number(value):
                raise VerificationError(
                    "stat_type",
                    "invalid",
                    location=f"stat.{key}",
                )
    state.pass_("stat_schema", "stdout.BACKTEST_STAT", "ok")


def parse_run_summary(payload: str) -> dict[str, Any]:
    """Safely parse RunSummary(...) text from BACKTEST_DONE."""
    text = payload.strip()
    try:
        node = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise VerificationError(
            "done_ast_error",
            "BACKTEST_DONE is not a valid expression",
            location="stdout.BACKTEST_DONE",
        ) from exc
    _assert_ast_bounds(node, location="stdout.BACKTEST_DONE")
    if not isinstance(node, ast.Expression):
        raise VerificationError(
            "done_ast_error",
            "BACKTEST_DONE rejected",
            location="stdout.BACKTEST_DONE",
        )
    call = node.body
    if not isinstance(call, ast.Call):
        raise VerificationError(
            "done_ast_error",
            "BACKTEST_DONE must be a RunSummary call",
            location="stdout.BACKTEST_DONE",
        )
    if not isinstance(call.func, ast.Name) or call.func.id != "RunSummary":
        raise VerificationError(
            "done_ast_error",
            "BACKTEST_DONE callee must be RunSummary",
            location="stdout.BACKTEST_DONE",
        )
    if call.args or call.keywords is None:
        raise VerificationError(
            "done_ast_error",
            "BACKTEST_DONE must use keyword arguments only",
            location="stdout.BACKTEST_DONE",
        )
    result: dict[str, Any] = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            raise VerificationError(
                "done_ast_error",
                "BACKTEST_DONE rejects **kwargs",
                location="stdout.BACKTEST_DONE",
            )
        if keyword.arg not in _RUN_SUMMARY_FIELDS:
            raise VerificationError(
                "done_ast_error",
                "unexpected field",
                location="stdout.BACKTEST_DONE",
            )
        if keyword.arg in result:
            raise VerificationError(
                "done_ast_error",
                "duplicate field",
                location="stdout.BACKTEST_DONE",
            )
        result[keyword.arg] = _safe_literal(
            keyword.value, location="stdout.BACKTEST_DONE"
        )
    missing = _RUN_SUMMARY_FIELDS - set(result)
    if missing:
        raise VerificationError(
            "done_ast_error",
            "missing fields",
            location="stdout.BACKTEST_DONE",
        )
    if set(result) != _RUN_SUMMARY_FIELDS:
        raise VerificationError(
            "done_ast_error",
            "field set mismatch",
            location="stdout.BACKTEST_DONE",
        )
    if not isinstance(result["reason"], str):
        raise VerificationError(
            "done_ast_error",
            "reason type",
            location="stdout.BACKTEST_DONE",
        )
    for field_name in ("updates", "decisions", "target_changes", "final_position"):
        if not _is_int(result[field_name]):
            raise VerificationError(
                "done_ast_error",
                f"{field_name} type",
                location="stdout.BACKTEST_DONE",
            )
        if abs(result[field_name]) > MAX_SAFE_ABS_POSITION and field_name == "final_position":
            raise VerificationError(
                "done_ast_error",
                "final_position bounds",
                location="stdout.BACKTEST_DONE",
            )
        if field_name != "final_position" and result[field_name] < 0:
            raise VerificationError(
                "done_ast_error",
                f"{field_name} bounds",
                location="stdout.BACKTEST_DONE",
            )
    return result


def parse_backtest_stat(payload: str) -> dict[str, Any]:
    """Safely parse the BACKTEST_STAT mapping text."""
    text = payload.strip()
    try:
        node = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise VerificationError(
            "stat_ast_error",
            "BACKTEST_STAT is not a valid expression",
            location="stdout.BACKTEST_STAT",
        ) from exc
    _assert_ast_bounds(node, location="stdout.BACKTEST_STAT")
    if not isinstance(node, ast.Expression):
        raise VerificationError(
            "stat_ast_error",
            "BACKTEST_STAT rejected",
            location="stdout.BACKTEST_STAT",
        )
    value = _safe_stat_node(node.body, location="stdout.BACKTEST_STAT")
    if not isinstance(value, dict):
        raise VerificationError(
            "stat_ast_error",
            "BACKTEST_STAT must be a mapping",
            location="stdout.BACKTEST_STAT",
        )
    if not value:
        raise VerificationError(
            "stat_ast_error",
            "BACKTEST_STAT empty mapping",
            location="stdout.BACKTEST_STAT",
        )
    return value


def _assert_ast_bounds(node: ast.AST, *, location: str) -> None:
    count = 0

    def walk(current: ast.AST, depth: int) -> None:
        nonlocal count
        count += 1
        if depth > MAX_AST_DEPTH or count > MAX_AST_NODES:
            raise VerificationError(
                "ast_too_complex",
                "expression too complex",
                location=location,
            )
        for child in ast.iter_child_nodes(current):
            walk(child, depth + 1)

    walk(node, 0)


def _safe_stat_node(node: ast.AST, *, location: str) -> Any:
    if isinstance(node, ast.Dict):
        result: dict[str, Any] = {}
        for key_node, value_node in zip(node.keys, node.values):
            if key_node is None:
                raise VerificationError(
                    "stat_ast_error",
                    "unpacking rejected",
                    location=location,
                )
            key = _safe_literal(key_node, location=location)
            if not isinstance(key, str):
                raise VerificationError(
                    "stat_ast_error",
                    "keys must be strings",
                    location=location,
                )
            if key in result:
                raise VerificationError(
                    "stat_ast_error",
                    "duplicate key",
                    location=location,
                )
            result[key] = _safe_stat_node(value_node, location=location)
        return result
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_safe_stat_node(item, location=location) for item in node.elts]
    return _safe_literal(node, location=location)


def _safe_literal(node: ast.AST, *, location: str) -> Any:
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, float):
            try:
                if not math.isfinite(value):
                    raise VerificationError(
                        "nonfinite_number",
                        "non-finite number rejected",
                        location=location,
                    )
            except (OverflowError, ValueError) as exc:
                raise VerificationError(
                    "nonfinite_number",
                    "non-finite number rejected",
                    location=location,
                ) from exc
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            if abs(value) > MAX_SAFE_INT:
                raise VerificationError(
                    "integer_too_large",
                    "integer out of bounds",
                    location=location,
                )
            return value
        if isinstance(value, (str, bool)) or value is None:
            return value
        raise VerificationError(
            "ast_value_rejected",
            "unsupported constant",
            location=location,
        )
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _safe_literal(node.operand, location=location)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise VerificationError(
                "ast_value_rejected",
                "invalid unary operand",
                location=location,
            )
        try:
            result = value if isinstance(node.op, ast.UAdd) else -value
            if isinstance(result, float) and not math.isfinite(result):
                raise VerificationError(
                    "nonfinite_number",
                    "non-finite number rejected",
                    location=location,
                )
            if isinstance(result, int) and not isinstance(result, bool):
                if abs(result) > MAX_SAFE_INT:
                    raise VerificationError(
                        "integer_too_large",
                        "integer out of bounds",
                        location=location,
                    )
            return result
        except (OverflowError, ValueError) as exc:
            raise VerificationError(
                "integer_too_large",
                "numeric conversion failed",
                location=location,
            ) from exc
    if isinstance(node, ast.Call):
        func_name = _call_name(node.func)
        if func_name not in (_NP_FLOAT_CALLS | _NP_INT_CALLS):
            raise VerificationError(
                "ast_call_rejected",
                "call not allowed",
                location=location,
            )
        if len(node.args) != 1 or node.keywords:
            raise VerificationError(
                "ast_call_rejected",
                "numeric call must have one positional argument",
                location=location,
            )
        value = _safe_literal(node.args[0], location=location)
        if func_name in _NP_INT_CALLS:
            if not _is_int(value):
                raise VerificationError(
                    "ast_call_rejected",
                    "np.int64 requires integer argument",
                    location=location,
                )
            if abs(value) > MAX_SAFE_INT:
                raise VerificationError(
                    "integer_too_large",
                    "integer out of bounds",
                    location=location,
                )
            return int(value)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise VerificationError(
                "ast_call_rejected",
                "numeric call argument invalid",
                location=location,
            )
        try:
            as_float = float(value)
        except (OverflowError, ValueError) as exc:
            raise VerificationError(
                "nonfinite_number",
                "numeric conversion failed",
                location=location,
            ) from exc
        if not math.isfinite(as_float):
            raise VerificationError(
                "nonfinite_number",
                "non-finite number rejected",
                location=location,
            )
        return as_float
    if isinstance(node, (ast.Attribute, ast.Name, ast.Subscript, ast.BinOp, ast.Lambda)):
        raise VerificationError(
            "ast_value_rejected",
            "expression form rejected",
            location=location,
        )
    raise VerificationError(
        "ast_value_rejected",
        "expression form rejected",
        location=location,
    )


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    return ""


def _compare_snapshots(
    start: Mapping[str, FileIdentity],
    end: Mapping[str, FileIdentity],
    state: VerificationState,
) -> None:
    for name in start:
        if (
            start[name].sha256 != end[name].sha256
            or start[name].size != end[name].size
            or start[name].st_dev != end[name].st_dev
            or start[name].st_ino != end[name].st_ino
            or start[name].path != end[name].path
        ):
            state.fail("input_mutated", name, "changed during verification")
        else:
            state.pass_("input_stable", name, "unchanged")


def _collect_identity(
    record: Mapping[str, Any], mode_values: set[str], symbol_values: set[str]
) -> None:
    mode = record.get("mode")
    symbol = record.get("symbol")
    if isinstance(mode, str) and mode:
        mode_values.add(mode)
    if isinstance(symbol, str) and symbol:
        symbol_values.add(symbol)


def _single_value(values: set[str]) -> str | None:
    return next(iter(values)) if len(values) == 1 else None


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _numbers_close(left: Any, right: Any) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        try:
            return math.isclose(
                float(left),
                float(right),
                rel_tol=NUMERIC_REL_TOL,
                abs_tol=NUMERIC_ABS_TOL,
            )
        except (OverflowError, ValueError):
            return False
    return False
