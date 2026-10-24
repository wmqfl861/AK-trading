"""C4 CLI tests: gate ordering, offline plan/verify, SYNTHETIC end-to-end run.

The end-to-end path exercises the FULL pipeline (capture -> raw publish ->
ingest -> reconcile -> product artifacts -> summary) entirely offline with
FakeTqApi and an attached-schema SQLite engine. The real adapter/engine
factories are monkeypatched to raise if ever reached - proving gates refuse
BEFORE any connection object exists (H01/H15).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from ak_trading.acquisition import cli
from ak_trading.acquisition import contracts as c
from ak_trading.acquisition import storage as sto
from ak_trading.acquisition import tq_adapter as tq

FIXTURES = Path(__file__).parent / "fixtures"
FRIDAY_AFTERNOON = c.iso_instant_to_nano("2026-09-18T16:00:00+08:00")


def _nano(iso: str) -> int:
    return c.iso_instant_to_nano(iso)


@pytest.fixture()
def policy(frozen_policy_path):
    return c.Policy.from_file(frozen_policy_path).validate()


@pytest.fixture()
def plan_dir(tmp_path, frozen_policy_path):
    out = tmp_path / "plan"
    code = cli.main(
        [
            "plan",
            "--policy", str(frozen_policy_path),
            "--inventory", str(FIXTURES / "synthetic_inventory.json"),
            "--calendar-evidence", str(FIXTURES / "synthetic_calendar_evidence.json"),
            "--output", str(out),
        ]
    )
    assert code == 0
    return out


def _seal_and_authorization(
    tmp_path, *, plan_path, policy_sha, manifest_sha, verdict="PASS"
):
    seal = {
        "review_id": "SYNTHETIC-REVIEW-r1",
        "verdict": verdict,
        "scope": {"covers_package": "ak_acquisition_w2", "code_manifest_sha256": manifest_sha},
    }
    seal_path = tmp_path / "seal.json"
    seal_path.write_text(json.dumps(seal), encoding="utf-8")
    auth = {
        "confirmation_ref": "user chat confirmation (SYNTHETIC fixture)",
        "confirmed_at_nano": str(_nano("2026-09-18T15:00:00+08:00")),
        "approver": "HUMAN_USER",
        "scope_policy_sha256": policy_sha,
        "scope_review_seal_sha256": c.sha256_file(seal_path),
        "scope_code_manifest_sha256": manifest_sha,
        "scope_plan_sha256": c.sha256_file(plan_path),
        "output_root": str(tmp_path / "runout"),
        "db_target_non_sensitive": "localhost:5433/acq_w2_test",
        "session_window_start": "2026-09-18T09:00:00+08:00",
        "session_window_end": "2026-09-18T23:00:00+08:00",
        "max_serial_attempts": 24,
        "max_wall_seconds": 1800,
        "allow_subsequent_batches": False,
        "protected_paths": [str(tmp_path / "protected")],
        "protected_db_objects": ["public.market_bars"],
        "expires_at": "2026-09-18T23:59:00+08:00",
        "health_checks": [
            {"name": "marker", "kind": "file_exists", "target": str(tmp_path / "marker")}
        ],
    }
    (tmp_path / "marker").write_text("cold-chain marker", encoding="utf-8")
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps(auth), encoding="utf-8")
    return seal_path, auth_path


class TestPlanCommand:
    def test_plan_offline_matrix_and_hashes(self, plan_dir, frozen_policy_path):
        plan = json.loads((plan_dir / "plan.json").read_text(encoding="utf-8"))
        assert plan["baseline_products"] == 81
        assert plan["synthetic"] is True
        assert len(plan["tasks"]) == 162  # 81 products x 2 durations
        assert plan["rollup"]["cells"]["NOT_RUN"] == 162
        assert plan["denominator"]["quarantined"].__len__() == 42
        assert plan["policy_sha256"] == c.sha256_file(frozen_policy_path)
        manifest = json.loads(
            (plan_dir / "plan-manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["plan_sha256"] == c.sha256_file(plan_dir / "plan.json")

    def test_plan_refuses_nonempty_output(self, plan_dir, frozen_policy_path):
        code = cli.main(
            [
                "plan",
                "--policy", str(frozen_policy_path),
                "--inventory", str(FIXTURES / "synthetic_inventory.json"),
                "--output", str(plan_dir),
            ]
        )
        assert code == cli.EXIT_FAILURE


class TestRunGatesRefuseBeforeConnections:
    def test_missing_authorization_file(self, plan_dir, tmp_path):
        auth, problems = cli.run_gates(
            plan_path=plan_dir / "plan.json",
            authorization_path=tmp_path / "nope.json",
            review_seal_path=tmp_path / "nope2.json",
            now_nano=FRIDAY_AFTERNOON,
        )
        assert auth is None
        assert any("unreadable" in p for p in problems)

    def test_gate_failures_never_construct_adapter_or_engine(
        self, plan_dir, tmp_path, monkeypatch
    ):
        def _must_not_build(*args, **kwargs):
            raise AssertionError("gate failure must not construct real objects")

        monkeypatch.setattr(tq, "RealTqApiSession", _must_not_build)
        monkeypatch.setattr(sto, "build_project_engine", _must_not_build)

        manifest_sha = cli.compute_code_manifest(
            Path(__file__).resolve().parents[1] / "src" / "ak_trading" / "acquisition"
        )["manifest_sha256"]
        policy = json.loads((plan_dir / "plan.json").read_text(encoding="utf-8"))
        seal_path, auth_path = _seal_and_authorization(
            tmp_path,
            plan_path=plan_dir / "plan.json",
            policy_sha=policy["policy_sha256"],
            manifest_sha=manifest_sha,
        )
        # Tamper the plan after authorization binding -> hash mismatch.
        tampered_dir = tmp_path / "tampered"
        tampered_dir.mkdir()
        tampered_plan = tampered_dir / "plan.json"
        payload = dict(policy)
        payload["extra"] = "tampered"
        tampered_plan.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

        auth, problems = cli.run_gates(
            plan_path=tampered_plan,
            authorization_path=auth_path,
            review_seal_path=seal_path,
            now_nano=FRIDAY_AFTERNOON,
        )
        assert auth is None
        assert any("plan hash mismatch" in p for p in problems)

    def test_run_command_refuses_without_credentials(
        self, plan_dir, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("TQ_USER", raising=False)
        monkeypatch.delenv("TQ_PASS", raising=False)
        monkeypatch.delenv("AK_DATABASE_URL", raising=False)
        code = cli.main(
            [
                "run",
                "--plan", str(plan_dir / "plan.json"),
                "--authorization", str(tmp_path / "missing.json"),
                "--review-seal", str(tmp_path / "missing.json"),
            ]
        )
        assert code == cli.EXIT_GATE_REFUSED

    def test_seal_not_pass_refuses(self, plan_dir, tmp_path, monkeypatch):
        monkeypatch.setenv("TQ_USER", "fixture-name-only")
        monkeypatch.setenv("TQ_PASS", "fixture-name-only")
        monkeypatch.setenv("AK_DATABASE_URL", "postgresql://u:p@localhost:5433/acq_w2_test")
        manifest_sha = cli.compute_code_manifest(
            Path(__file__).resolve().parents[1] / "src" / "ak_trading" / "acquisition"
        )["manifest_sha256"]
        policy = json.loads((plan_dir / "plan.json").read_text(encoding="utf-8"))
        seal_path, auth_path = _seal_and_authorization(
            tmp_path,
            plan_path=plan_dir / "plan.json",
            policy_sha=policy["policy_sha256"],
            manifest_sha=manifest_sha,
            verdict="FAIL",
        )
        auth, problems = cli.run_gates(
            plan_path=plan_dir / "plan.json",
            authorization_path=auth_path,
            review_seal_path=seal_path,
            now_nano=FRIDAY_AFTERNOON,
        )
        assert auth is None
        assert any("review seal is not PASS" in p for p in problems)

    def test_saturday_execution_refused(self, plan_dir, tmp_path):
        saturday = c.iso_instant_to_nano("2026-09-19T10:00:00+08:00")
        manifest_sha = "0" * 64
        policy = json.loads((plan_dir / "plan.json").read_text(encoding="utf-8"))
        seal_path, auth_path = _seal_and_authorization(
            tmp_path,
            plan_path=plan_dir / "plan.json",
            policy_sha=policy["policy_sha256"],
            manifest_sha=manifest_sha,
        )
        auth, problems = cli.run_gates(
            plan_path=plan_dir / "plan.json",
            authorization_path=auth_path,
            review_seal_path=seal_path,
            now_nano=saturday,
        )
        assert auth is None
        assert any("Saturday" in p for p in problems)


class TestSyntheticEndToEnd:
    def _small_plan(self, tmp_path, policy):
        return {
            "plan_id": "SYNTHETIC-e2e",
            "synthetic": True,
            "policy_path": str(
                Path(__file__).resolve().parents[1] / "config/execution-policy.frozen.json"
            ),
            "policy_sha256": policy.sha256,
            "universe_version": "synthetic-universe-v1",
            "inventory_sha256": "0" * 64,
            "calendar_evidence_sha256": "synthetic-calendar",
            "calendar_evidence_path": str(FIXTURES / "synthetic_calendar_evidence.json"),
            "baseline_products": 2,
            "tasks": [
                {
                    "product_key": "SHFE.rb",
                    "priority": "P0",
                    "duration_seconds": 60,
                    "concrete_symbols": ["SHFE.rb2601", "SHFE.rb2605"],
                },
                {
                    "product_key": "DCE.i",
                    "priority": "P1",
                    "duration_seconds": 60,
                    "concrete_symbols": ["DCE.i2601"],
                },
            ],
        }

    def test_full_offline_pipeline_and_verify_package(self, tmp_path, policy):
        observed = _nano("2026-09-18T15:30:00+08:00")
        frames = {}
        for symbol in ("SHFE.rb2601", "SHFE.rb2605", "DCE.i2601"):
            frames[(symbol, 60)] = tq.make_fake_frame(
                symbol=symbol, duration_seconds=60,
                start_nano=_nano("2026-09-18T14:00:00+08:00"),
                rows=6, closed_rows=6, padding_rows=2,
            )
        fake = tq.FakeTqApi(frames, clock=lambda: observed, updates_until_ready=2)
        schema_file = tmp_path / "acq.db"
        engine = create_engine(f"sqlite:///{tmp_path/'main.db'}")
        sto.attach_sqlite_schema(engine, str(schema_file).replace("\\", "/"))

        plan = self._small_plan(tmp_path, policy)
        auth = c.RunAuthorization(
            confirmation_ref="SYNTHETIC", confirmed_at_nano=observed,
            approver="HUMAN_USER", scope_policy_sha256=policy.sha256,
            scope_review_seal_sha256="0" * 64, scope_code_manifest_sha256="0" * 64,
            scope_plan_sha256="0" * 64, output_root=str(tmp_path / "run"),
            db_target_non_sensitive="synthetic",
            session_window_start_nano=observed - 10**12,
            session_window_end_nano=observed + 10**12,
            max_serial_attempts=24, max_wall_seconds=1800,
            allow_subsequent_batches=False,
            protected_paths=("protected",), protected_db_objects=("market_bars",),
        )
        code = cli.execute_run(
            plan=plan, policy=policy, auth=auth,
            output_root=tmp_path / "run",
            adapter_factory=lambda: fake,
            engine_factory=lambda: engine,
            clock=lambda: observed,
        )
        assert code == cli.EXIT_OK
        # H13 per-product artifact set.
        product_dir = tmp_path / "run" / "products" / "SHFE_rb"
        for name in (
            "identity.json", "raw-manifest.json", "reconciliation.json",
            "coverage.json", "gaps.json", "acceptance.json",
        ):
            assert (product_dir / name).is_file(), name
        raw_files = list((product_dir / "raw").glob("*.raw.json"))
        assert len(raw_files) == 2  # two concrete contracts of SHFE.rb
        for raw in raw_files:
            record = json.loads(raw.read_text(encoding="utf-8"))
            assert record["meta"]["synthetic_label"] == "SYNTHETIC"
        summary = json.loads(
            (tmp_path / "run" / "run-summary.json").read_text(encoding="utf-8")
        )
        assert summary["collector_run"] == "SYNTHETIC_OFFLINE"
        assert summary["coverage_status_counts"].get("INGEST_VERIFIED") == 3
        # verify-package passes on the synthetic package.
        assert cli.main(["verify-package", "--package", str(tmp_path / "run"), "--offline"]) == 0

    def test_budget_exhaustion_marks_deferred(self, tmp_path, policy):
        observed = _nano("2026-09-18T15:30:00+08:00")
        # Only one symbol has frames; the rest exhaust the 24-attempt budget.
        frames = {("SHFE.rb2601", 60): tq.make_fake_frame(
            symbol="SHFE.rb2601", duration_seconds=60,
            start_nano=_nano("2026-09-18T14:00:00+08:00"), rows=3, closed_rows=3,
        )}
        fake = tq.FakeTqApi(frames, clock=lambda: observed, updates_until_ready=2)
        schema_file = tmp_path / "acq.db"
        engine = create_engine(f"sqlite:///{tmp_path/'main.db'}")
        sto.attach_sqlite_schema(engine, str(schema_file).replace("\\", "/"))

        plan = {
            "synthetic": True,
            "policy_path": str(
                Path(__file__).resolve().parents[1] / "config/execution-policy.frozen.json"
            ),
            "policy_sha256": policy.sha256,
            "universe_version": "u", "inventory_sha256": "0" * 64,
            "calendar_evidence_sha256": "NONE_UNKNOWN",
            "tasks": [
                {
                    "product_key": f"SHFE.p{i%2}",
                    "priority": "P2",
                    "duration_seconds": 60,
                    "concrete_symbols": [f"SHFE.s{i:03d}"],
                }
                for i in range(30)
            ]
            + [{"product_key": "SHFE.rb", "priority": "P0",
                "duration_seconds": 60, "concrete_symbols": ["SHFE.rb2601"]}],
        }
        auth = c.RunAuthorization(
            confirmation_ref="SYNTHETIC", confirmed_at_nano=observed,
            approver="HUMAN_USER", scope_policy_sha256=policy.sha256,
            scope_review_seal_sha256="0" * 64, scope_code_manifest_sha256="0" * 64,
            scope_plan_sha256="0" * 64, output_root=str(tmp_path / "run"),
            db_target_non_sensitive="synthetic",
            session_window_start_nano=observed - 10**12,
            session_window_end_nano=observed + 10**12,
            max_serial_attempts=24, max_wall_seconds=1800,
            allow_subsequent_batches=False,
            protected_paths=("protected",), protected_db_objects=("x",),
        )
        code = cli.execute_run(
            plan=plan, policy=policy, auth=auth,
            output_root=tmp_path / "run",
            adapter_factory=lambda: fake,
            engine_factory=lambda: engine,
            clock=lambda: observed,
        )
        assert code == cli.EXIT_DEFERRED_BUDGET
        summary = json.loads(
            (tmp_path / "run" / "run-summary.json").read_text(encoding="utf-8")
        )
        assert summary["coverage_status_counts"].get("DEFERRED_BUDGET", 0) > 0
        # Attempt state persisted: restart does not reset counters (H11).
        state = json.loads(
            (tmp_path / "run" / "run-state.json").read_text(encoding="utf-8")
        )
        assert state["tasks"], "attempt state must persist for resume"

    def test_account_denial_blocks_run(self, tmp_path, policy):
        observed = _nano("2026-09-18T15:30:00+08:00")
        denial = {("SHFE.rb2601", 60): tq.AccountDeniedError()}
        fake = tq.FakeTqApi({}, clock=lambda: observed, failures=denial)
        schema_file = tmp_path / "acq.db"
        engine = create_engine(f"sqlite:///{tmp_path/'main.db'}")
        sto.attach_sqlite_schema(engine, str(schema_file).replace("\\", "/"))
        plan = {
            "synthetic": True,
            "policy_path": str(
                Path(__file__).resolve().parents[1] / "config/execution-policy.frozen.json"
            ),
            "policy_sha256": policy.sha256,
            "tasks": [{"product_key": "SHFE.rb", "priority": "P0",
                        "duration_seconds": 60,
                        "concrete_symbols": ["SHFE.rb2601", "SHFE.rb2605"]}],
        }
        auth = c.RunAuthorization(
            confirmation_ref="SYNTHETIC", confirmed_at_nano=observed,
            approver="HUMAN_USER", scope_policy_sha256=policy.sha256,
            scope_review_seal_sha256="0" * 64, scope_code_manifest_sha256="0" * 64,
            scope_plan_sha256="0" * 64, output_root=str(tmp_path / "run"),
            db_target_non_sensitive="synthetic",
            session_window_start_nano=observed - 10**12,
            session_window_end_nano=observed + 10**12,
            max_serial_attempts=24, max_wall_seconds=1800,
            allow_subsequent_batches=False,
            protected_paths=("protected",), protected_db_objects=("x",),
        )
        code = cli.execute_run(
            plan=plan, policy=policy, auth=auth,
            output_root=tmp_path / "run",
            adapter_factory=lambda: fake,
            engine_factory=lambda: engine,
            clock=lambda: observed,
        )
        assert code == cli.EXIT_BLOCKED
        summary = json.loads(
            (tmp_path / "run" / "run-summary.json").read_text(encoding="utf-8")
        )
        assert summary["coverage_status_counts"].get("PERMISSION_DENIED", 0) >= 1


class TestVerifyPackage:
    def test_detects_tampered_raw_and_missing_files(self, tmp_path):
        products = tmp_path / "products" / "SHFE_rb"
        (products / "raw").mkdir(parents=True)
        raw = products / "raw" / "x.raw.json"
        raw.write_text("{}", encoding="utf-8")
        (products / "raw-manifest.json").write_text(
            json.dumps(
                {"entries": [
                    {"raw_path": str(raw),
                     "raw_sha256": c.sha256_bytes(b"{}")}
                ]}
            ),
            encoding="utf-8",
        )
        code = cli.main(["verify-package", "--package", str(tmp_path), "--offline"])
        assert code == cli.EXIT_FAILURE  # missing identity/rows/etc.

    def test_credential_scan(self, tmp_path):
        products = tmp_path / "products" / "SHFE_rb"
        products.mkdir(parents=True)
        (products / "identity.json").write_text("TQ_PASS=leaked", encoding="utf-8")
        code = cli.main(["verify-package", "--package", str(tmp_path), "--offline"])
        assert code == cli.EXIT_FAILURE
