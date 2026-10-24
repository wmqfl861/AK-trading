"""C0 contract tests: policy validation, canonical hashing, immutable records.

All fixtures and probes are SYNTHETIC. Static/security checks here mirror the
reviewer counterexamples in dev-plan section 8 (H01/H14 rows).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ak_trading.acquisition import contracts as c
from ak_trading.acquisition import tq_adapter as tq


# ---------------------------------------------------------------------------
# Policy loading and tamper detection (H01: CLI must not bypass the policy)
# ---------------------------------------------------------------------------

class TestPolicyValidation:
    def test_frozen_policy_loads_and_validates(self, frozen_policy_path):
        policy = c.Policy.from_file(
            frozen_policy_path, expected_sha256=c.FROZEN_POLICY_SHA256
        ).validate()
        assert policy.policy_id == "ACQ-EXEC-20260919-r1"
        assert policy.request_data_length == 8192
        assert policy.durations == (60, 86400)
        assert policy.target_a_trading_day == "2026-09-18"
        budget = policy.budget
        assert budget["serial_request_attempts_per_run_max"] == 24
        assert budget["run_wall_seconds_max"] == 1800
        assert budget["active_symbols_max"] == 4
        assert budget["active_serials_max"] == 8

    def test_hash_mismatch_is_rejected(self, frozen_policy_path, tmp_path):
        payload = json.loads(frozen_policy_path.read_text(encoding="utf-8"))
        payload["budget"]["serial_request_attempts_per_run_max"] = 999
        tampered = tmp_path / "tampered-policy.json"
        tampered.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(c.ContractError, match="frozen policy"):
            c.Policy.from_file(tampered, expected_sha256=c.FROZEN_POLICY_SHA256)

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda p: p["budget"].update({"run_wall_seconds_max": 9999}),
            lambda p: p["safety"].update({"enable_trading": True}),
            lambda p: p["safety"].update({"DataDownloader_allowed": True}),
            lambda p: p["scope"].update({"duration_seconds": [60, 86400, 300]}),
            lambda p: p["source_policy"].update({"request_data_length": 10000}),
            lambda p: p["targets"].update({"scenario_generation": True}),
            lambda p: p["bar_policy"].update({"daily_start_plus_86400_rule_allowed": True}),
            lambda p: p["storage"].update({"write_schema": "public"}),
            lambda p: p["retry"].update({"backoff_seconds": [30, 60, 90]}),
        ],
    )
    def test_policy_invariant_violations_fail_closed(
        self, frozen_policy_path, tmp_path, mutate
    ):
        payload = json.loads(frozen_policy_path.read_text(encoding="utf-8"))
        mutate(payload)
        path = tmp_path / "mutated.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(c.ContractError):
            c.Policy.from_file(path).validate()

    def test_status_must_stay_runtime_not_authorized(self, frozen_policy_path, tmp_path):
        payload = json.loads(frozen_policy_path.read_text(encoding="utf-8"))
        payload["status"] = "RUNTIME_AUTHORIZED"
        path = tmp_path / "status-flip.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(c.ContractError, match="DEVELOPMENT_AUTHORIZED"):
            c.Policy.from_file(path).validate()


# ---------------------------------------------------------------------------
# Integer nanosecond discipline (H07)
# ---------------------------------------------------------------------------

class TestNanosecondDiscipline:
    def test_float_nanoseconds_rejected(self):
        with pytest.raises(c.ContractError):
            c.validate_nano_int(1.5)
        with pytest.raises(c.ContractError):
            c.validate_nano_int(True)
        with pytest.raises(c.ContractError):
            c.validate_nano_int("1760000000000000000")

    def test_iso_roundtrip_without_float_drift(self):
        value = c.iso_instant_to_nano("2026-09-18T21:00:00+08:00")
        assert isinstance(value, int)
        assert c.nano_to_iso(value) == "2026-09-18T21:00:00+08:00"
        precise = value + 123_456_789
        assert c.nano_to_iso(precise) == "2026-09-18T21:00:00.123456789+08:00"

    def test_civil_date_midnight(self):
        from datetime import date

        assert c.civil_date_to_nano(date(2026, 9, 18)) == c.iso_instant_to_nano(
            "2026-09-18T00:00:00+08:00"
        )

    def test_content_hash_excludes_observation_metadata(self):
        common = dict(
            duration_seconds=60,
            datetime_nano=176_000_000_000_000_000,
            bar_start_nano=176_000_000_000_000_000,
            bar_end_nano=176_000_000_000_000_000 + 60 * 10**9,
            trading_day="2026-09-18",
            values={k: "1.5" for k in c.BAR_VALUE_FIELDS},
        )
        first = c.canonical_bar_content(**common)
        assert first == c.canonical_bar_content(**common)
        drifted = dict(common)
        drifted["values"] = dict(common["values"], close="1.50000001")
        assert c.canonical_bar_content(**drifted) != first


# ---------------------------------------------------------------------------
# Records: immutability and invariants
# ---------------------------------------------------------------------------

class TestRecords:
    def _meta(self, **kwargs):
        base = dict(
            task_id="t", attempt_id="a", source_symbol="SHFE.rb2601",
            duration_seconds=60, requested_length=8192, returned_length=10,
            frame_rows=10, padding_rows=5, invalid_rows=2, valid_rows=3,
            observed_at_nano=1, sdk_version="x", serializer_version="y",
            raw_path="p", raw_sha256="h", raw_bytes=1,
            first_valid_nano=None, last_valid_nano=None,
        )
        base.update(kwargs)
        return base

    def test_raw_snapshot_meta_conservation(self):
        with pytest.raises(c.ContractError, match="conserved"):
            c.RawSnapshotMeta(**self._meta(valid_rows=4))

    def test_requested_length_fixed_at_8192(self):
        with pytest.raises(c.ContractError, match="8192"):
            c.RawSnapshotMeta(**self._meta(requested_length=1000, returned_length=0, frame_rows=0, padding_rows=0, invalid_rows=0, valid_rows=0))

    def test_coverage_unknown_denominator_never_zero(self):
        result = c.CoverageResult(
            target_kind=c.TargetKind.B, product_key="SHFE.rb", duration_seconds=60,
            expected_slots=None, actual_slots=500, missing_slots=None,
            unknown_slot_count=3, conflicts=0, raw_watermark="RAW_VERIFIED",
            db_watermark="INGEST_VERIFIED", source_availability="ATTEMPTED",
        )
        assert result.coverage_status() is c.CoverageStatus.UNKNOWN
        with pytest.raises(c.ContractError):
            c.CoverageResult(
                target_kind=c.TargetKind.B, product_key="SHFE.rb", duration_seconds=60,
                expected_slots=None, actual_slots=0, missing_slots=(),
                unknown_slot_count=None, conflicts=0, raw_watermark="NONE",
                db_watermark="NONE", source_availability="NOT_RUN",
            )

    def test_coverage_status_exclusivity(self):
        def make(expected, **kwargs):
            base = dict(
                target_kind=c.TargetKind.B, product_key="p", duration_seconds=60,
                actual_slots=10, missing_slots=() if expected is not None else None,
                unknown_slot_count=0,
                conflicts=0, raw_watermark="RAW_VERIFIED",
                db_watermark="INGEST_VERIFIED", source_availability="ATTEMPTED",
            )
            base.update(kwargs)
            return c.CoverageResult(expected_slots=expected, **base).coverage_status()

        assert make(10) is c.CoverageStatus.COMPLETE
        assert make(12) is c.CoverageStatus.PARTIAL
        assert make(10, missing_slots=("x",)) is c.CoverageStatus.PARTIAL
        assert make(10, conflicts=1) is c.CoverageStatus.PARTIAL
        assert make(10, raw_watermark="NONE") is c.CoverageStatus.PARTIAL
        assert make(None) is c.CoverageStatus.UNKNOWN
        assert (
            make(10, raw_watermark="NONE", db_watermark="NONE",
                 source_availability="NOT_RUN", actual_slots=0)
            is c.CoverageStatus.NOT_RUN
        )

    def test_run_authorization_refuses_agent_selfsign_and_missing_protection(self):
        base = dict(
            confirmation_ref="user message ref", confirmed_at_nano=1,
            approver="HUMAN_USER",
            scope_policy_sha256="a" * 64, scope_review_seal_sha256="b" * 64,
            scope_code_manifest_sha256="d" * 64, scope_plan_sha256="e" * 64,
            output_root="out", db_target_non_sensitive="host:5432/db",
            session_window_start_nano=10, session_window_end_nano=20,
            max_serial_attempts=24, max_wall_seconds=1800,
            allow_subsequent_batches=False,
            protected_paths=("C:/protected",), protected_db_objects=("market_bars",),
        )
        c.RunAuthorization(**base)
        with pytest.raises(c.ContractError, match="HUMAN_USER"):
            c.RunAuthorization(**{**base, "approver": "ZCODE_SUBAGENT"})
        with pytest.raises(c.ContractError, match="H12"):
            c.RunAuthorization(**{**base, "protected_paths": ()})

    def test_authorization_cannot_exceed_policy_budget(self, frozen_policy_path):
        policy = c.Policy.from_file(frozen_policy_path).validate()
        auth = c.RunAuthorization(
            confirmation_ref="r", confirmed_at_nano=1, approver="HUMAN_USER",
            scope_policy_sha256="a" * 64, scope_review_seal_sha256="b" * 64,
            scope_code_manifest_sha256="d" * 64, scope_plan_sha256="e" * 64,
            output_root="out", db_target_non_sensitive="host:5432/db",
            session_window_start_nano=10, session_window_end_nano=20,
            max_serial_attempts=100, max_wall_seconds=1800,
            allow_subsequent_batches=False,
            protected_paths=("p",), protected_db_objects=("o",),
        )
        assert auth.within_budget(policy) == [
            "authorization serial attempts exceed policy cap"
        ]

    def test_acceptance_record_requires_independence_declaration(self):
        kwargs = dict(
            author="reviewer", policy_sha256="0" * 64, code_manifest_sha256="0" * 64,
            input_hashes=(), db_verification_snapshot_sha256="0" * 64,
            coverage_status=c.CoverageStatus.PARTIAL,
            scope_statement="s", gaps=(), next_steps=(),
        )
        c.AcceptanceRecord(
            independence_declaration="reviewer took no part in implementation",
            evidence_integrity_verdict="INSUFFICIENT", **kwargs
        )
        with pytest.raises(c.ContractError):
            c.AcceptanceRecord(
                independence_declaration="", evidence_integrity_verdict="INSUFFICIENT",
                **kwargs
            )
        with pytest.raises(c.ContractError, match="automated"):
            c.AcceptanceRecord(
                independence_declaration="took no part",
                author="COLLECTOR_PRE_ACCEPTANCE",
                evidence_integrity_verdict="VERIFIED",
                **{k: v for k, v in kwargs.items() if k != "author"},
            )


# ---------------------------------------------------------------------------
# H01 static security posture
# ---------------------------------------------------------------------------

class TestH01SecurityPosture:
    def test_no_paid_channel_references_in_package(self):
        package = Path(__file__).resolve().parents[1] / "src" / "ak_trading" / "acquisition"
        offenders = []
        for path in package.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            # Instantiation/import of the paid downloader anywhere is banned;
            # string mentions inside exclusion guards are acceptable.
            if "DataDownloader(" in text or "import DataDownloader" in text:
                offenders.append(path.name)
        assert offenders == []

    def test_no_tqaccount_tqkq_trading_calls(self):
        package = Path(__file__).resolve().parents[1] / "src" / "ak_trading" / "acquisition"
        for path in package.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "TqAccount(" not in text, path.name
            assert "TqKq(" not in text, path.name
            assert "insert_order" not in text, path.name
            assert "cancel_order" not in text, path.name

    def test_no_dotenv_reads(self):
        package = Path(__file__).resolve().parents[1] / "src" / "ak_trading" / "acquisition"
        for path in package.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert "load_dotenv" not in text, path.name

    def test_adapter_error_names_variables_not_values(self):
        err = tq.CredentialMissingError(["TQ_USER", "TQ_PASS"])
        assert "TQ_USER" in err.safe_message
        assert len(err.safe_message) < 200

    def test_decimal_string_is_traceable(self):
        assert c.decimal_string(2) == "2"
        assert c.decimal_string(1.5) == "1.5"
        assert c.decimal_string(0.1 + 0.2) == "0.30000000000000004"
        with pytest.raises(c.ContractError):
            c.decimal_string(float("nan"))
        with pytest.raises(c.ContractError):
            c.decimal_string(True)


# ---------------------------------------------------------------------------
# F3 counterexamples (review ACQ-W2-REVIEW-20260919-r1): truncation is not
# redaction. Every secret shape below must be ABSENT from the sanitized
# string while the exception type and known-safe diagnostics SURVIVE.
# ---------------------------------------------------------------------------

class TestSanitizeErrorTextRedaction:
    # -- secret shapes: connection strings ---------------------------------
    def test_postgres_connection_string_userinfo_is_masked(self):
        exc = RuntimeError(
            "connect failed: postgresql://ak_svc:Hunter2Secret@db.internal:5432/akdb timeout"
        )
        out = tq.sanitize_error_text(exc)
        assert "Hunter2Secret" not in out
        assert "ak_svc" not in out
        # known-safe fields survive: scheme, host, port, database name
        assert "postgresql://***@db.internal:5432/akdb" in out
        assert out.startswith("RuntimeError: ")

    def test_wss_url_path_token_is_masked_reviewer_decoy_shape(self):
        # Exact shape from review probe2: non-auth exception whose free text
        # carries an opaque token inside a URL path.
        decoy = "RZPROBE-DECOY-" + "a1b2c3d4e5f6a7b8"
        exc = RuntimeError(f"connection reset by peer url=wss://host/{decoy}/stream")
        out = tq.sanitize_error_text(exc)
        assert decoy not in out
        assert "a1b2c3d4e5f6a7b8" not in out
        assert "RZPROBE" not in out
        # diagnosability survives: type, cause words, url host
        assert out.startswith("RuntimeError: ")
        assert "connection reset by peer" in out
        assert "wss://host/" in out

    # -- secret shapes: credential-named fields ----------------------------
    def test_credential_key_value_forms_are_masked(self):
        exc = OSError(
            "mount //win/ak failed user=AK\\svc_collector password=S3cret-Pa55 "
            "token=abc123DEF456 timeout=30"
        )
        out = tq.sanitize_error_text(exc)
        assert "S3cret-Pa55" not in out
        assert "svc_collector" not in out
        assert "abc123DEF456" not in out
        assert "user=***" in out and "password=***" in out and "token=***" in out
        assert "timeout=30" in out  # safe field survives
        assert out.startswith("OSError: ")

    def test_known_env_credential_names_are_masked_with_values(self):
        exc = RuntimeError(
            "env dump: AK_DATABASE_URL=postgres://svc:hunter2@h/db "
            "TQ_USER=ak_collector TQ_PASS=LeftAsAnExercise2001 ok"
        )
        out = tq.sanitize_error_text(exc)
        for secret in ("hunter2", "ak_collector", "LeftAsAnExercise2001"):
            assert secret not in out
        assert "AK_DATABASE_URL=***" in out
        assert "TQ_USER=***" in out
        assert "TQ_PASS=***" in out

    def test_smb_style_bare_userinfo_is_masked(self):
        # paths carrying credentials without a URL scheme (user:pass@host)
        exc = RuntimeError(
            "mount share failed for ak\\collector:TopSecret99@file.internal retry=3"
        )
        out = tq.sanitize_error_text(exc)
        assert "TopSecret99" not in out
        assert "collector" not in out
        assert "file.internal" in out  # host survives
        assert "retry=3" in out

    # -- secret shapes: bearer / long free text / control chars ------------
    def test_bearer_token_is_masked(self):
        token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig"
        out = tq.sanitize_error_text(RuntimeError(f"401 retry with Bearer {token}"))
        assert token not in out and "eyJhbGci" not in out
        assert "Bearer ***" in out

    def test_zero_width_character_evasion_is_defeated(self):
        # A zero-width char inside a credential key or value must not split
        # the secret out of the masks (whitelist filtering runs FIRST).
        marker = "ZeroWidthEvade1"
        exc = RuntimeError(f"pass\u200bword={marker} state=1730000000000000000ns")
        out = tq.sanitize_error_text(exc)
        assert marker not in out
        assert "\u200b" not in out
        assert "password=***" in out
        assert "1730000000000000000ns" in out  # timestamps survive

    def test_long_free_text_is_bounded_and_multiline_dropped(self):
        secret = "PrefixLeak0123456789ABCDEF"
        exc = RuntimeError(
            "handshake failed session_id=" + secret + " then\nsecond line password=leaked"
        )
        out = tq.sanitize_error_text(exc)
        assert len(out) <= 200
        assert secret not in out
        assert "second line" not in out and "leaked" not in out
        assert "handshake failed" in out

    def test_empty_exception_text_yields_type_name_only(self):
        assert tq.sanitize_error_text(Exception()) == "Exception"

    # -- diagnosability preserved (classification keywords + safe fields) --
    @pytest.mark.parametrize(
        "exc,needles",
        [
            (TimeoutError("wait_update timed out after 30s ws://quote.host:7777"),
             ["TimeoutError", "timed out", "quote.host:7777"]),
            (RuntimeError("行情服务器返回: 请求过于频繁, retry later"), ["频繁", "retry"]),
            (RuntimeError("登录失败: 账户或密码错误"), ["登录", "账户"]),
            (ConnectionResetError("connection reset by peer errno=10054"),
             ["ConnectionResetError", "errno=10054"]),
            (RuntimeError("symbol SHFE.rb2601 not support"), ["SHFE.rb2601", "not support"]),
        ],
    )
    def test_safe_diagnostics_and_classifier_keywords_survive(self, exc, needles):
        out = tq.sanitize_error_text(exc)
        for needle in needles:
            assert needle in out, f"{needle!r} lost from {out!r}"

    # -- classification surface integration --------------------------------
    def test_non_auth_decoy_never_reaches_safe_message_via_classify(self):
        decoy = "RZPROBE-DECOY-" + "9f8e7d6c5b4a3210"
        exc = RuntimeError(f"connection reset by peer url=wss://host/{decoy}/stream")
        err = tq.RealTqApiSession._classify(object(), exc, "SHFE.rb2601")
        assert isinstance(err, tq.TqAdapterError)
        assert decoy not in err.safe_message
        assert "9f8e7d6c5b4a3210" not in err.safe_message
        assert "connection reset by peer" in err.safe_message

    def test_auth_keyword_exception_with_secrets_is_denied_and_clean(self):
        marker = "S3cretPassValue123"
        exc = RuntimeError(f"login auth failed user=probe password={marker}")
        out = tq.sanitize_error_text(exc)
        assert marker not in out
        err = tq.RealTqApiSession._classify(object(), exc, None)
        assert isinstance(err, tq.AccountDeniedError)
        assert marker not in err.safe_message

    def test_rate_limit_and_unsupported_classification_keywords_survive(self):
        rate = tq.RealTqApiSession._classify(
            object(), RuntimeError("请求过于频繁"), "SHFE.rb2601"
        )
        assert isinstance(rate, tq.RateLimitError)
        unsup = tq.RealTqApiSession._classify(
            object(), RuntimeError("symbol not support here"), "SHFE.rb9999"
        )
        assert isinstance(unsup, tq.SymbolUnsupportedError)
        assert "SHFE.rb9999" in unsup.safe_message
