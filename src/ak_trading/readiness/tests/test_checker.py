"""SYNTHETIC-input verification of the read-only readiness checker.

Covers, per Codex review report r1 section C6:
- each of the five checks fires on the seeded error class and nothing else;
- the clean fixture produces zero contradictions (no false positives);
- preparation states are never upgraded to completed;
- inputs stay byte-identical (read-only tool);
- malformed inputs fail fast instead of being reinterpreted;
- module sources contain no network/process capabilities.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

from ak_trading.readiness import (
    PLAN_ID,
    ReadinessInputError,
    load_inputs,
    run_checks,
    write_report,
)
from ak_trading.readiness.checker import (
    OUTPUT_CONTRADICTIONS_NAME,
    OUTPUT_SNAPSHOT_NAME,
    main as cli_main,
)
from ak_trading.readiness.models import classify_timestamp

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"


@pytest.fixture()
def broken_inputs():
    return load_inputs(
        FIXTURES / "register-snapshot-broken.json",
        FIXTURES / "identity-table.json",
        FIXTURES / "gate-table.json",
        FIXTURES / "file-table.json",
    )


@pytest.fixture()
def clean_inputs():
    return load_inputs(
        FIXTURES / "register-snapshot-clean.json",
        FIXTURES / "identity-table.json",
        FIXTURES / "gate-table.json",
        FIXTURES / "file-table.json",
    )


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_fixtures_declare_synthetic_provenance(broken_inputs, clean_inputs):
    for source in (
        broken_inputs.register,
        broken_inputs.identities,
        broken_inputs.gates,
        broken_inputs.files,
        clean_inputs.register,
    ):
        assert source.provenance == "SYNTHETIC"


class TestBrokenFixture:
    """Each seeded error class is reported exactly once, nothing else fires."""

    @pytest.fixture()
    def report(self, broken_inputs):
        return run_checks(broken_inputs)

    def test_totals(self, report):
        assert report.totals == {
            "CHK-01": 1,
            "CHK-02": 1,
            "CHK-03": 1,
            "CHK-04": 3,
            "CHK-05": 1,
        }

    def test_chk01_retired_identity_marked_current(self, report):
        findings = report.findings_for("CHK-01")
        assert [finding.code for finding in findings] == ["RETIRED_IDENTITY_MARKED_CURRENT"]
        finding = findings[0]
        assert finding.record == "syn_rec_01_retired_current"
        assert finding.detail["marked_current_value"] == "freeze-r2"
        assert finding.detail["table_current"] == "freeze-r3"

    def test_chk01_historical_mention_not_flagged(self, report):
        records = {finding.record for finding in report.findings}
        assert "syn_rec_01_historical_mention_ok" not in records

    def test_chk02_successor_ready_with_auth_only_prerequisite(self, report):
        findings = report.findings_for("CHK-02")
        assert len(findings) == 1
        finding = findings[0]
        assert finding.code == "SUCCESSOR_READY_WITH_INCOMPLETE_PREREQUISITE"
        assert finding.record == "syn_rec_02_gate_jump"
        assert finding.detail["gate"] == "B-G10-R1"
        assert finding.detail["declared_status"] == "READY"
        assert finding.detail["uncompleted_prerequisites"][0]["gate"] == "B-G09-R1"
        assert finding.detail["uncompleted_prerequisites"][0]["declared_statuses"] == [
            "AUTH_ISSUED"
        ]

    def test_chk03_frozen_written_as_sealed(self, report):
        findings = report.findings_for("CHK-03")
        assert [finding.code for finding in findings] == ["FROZEN_WRITTEN_AS_SEALED"]
        finding = findings[0]
        assert finding.record == "syn_rec_03_sealed_claim"
        assert finding.detail["authoritative_state"] == "FROZEN_UNSEALED_FAILED_PRESEAL"
        assert finding.detail["claimed_state"] == "SEALED"

    def test_chk04_transcriptions(self, report):
        findings = report.findings_for("CHK-04")
        codes = sorted(finding.code for finding in findings)
        assert codes == [
            "PATH_TRANSCRIPTION_MISMATCH",
            "REVISION_TRANSCRIPTION_MISMATCH",
            "SHA256_TRANSCRIPTION_MISMATCH",
        ]
        by_code = {finding.code: finding for finding in findings}
        path_finding = by_code["PATH_TRANSCRIPTION_MISMATCH"]
        assert "rounds/" in path_finding.summary and "runs/" in path_finding.summary
        assert path_finding.detail["note"].startswith("authoritative layout uses 'rounds/'")
        revision_finding = by_code["REVISION_TRANSCRIPTION_MISMATCH"]
        assert revision_finding.detail["expected"] == "r_10"
        assert revision_finding.detail["actual"] == "r10"
        assert "lexicographic" in revision_finding.summary
        sha_finding = by_code["SHA256_TRANSCRIPTION_MISMATCH"]
        assert sha_finding.detail["expected"] != sha_finding.detail["actual"]
        for finding in findings:
            assert finding.record == "syn_rec_04_transcription"

    def test_chk05_placeholder_utc_claimed_precise(self, report):
        findings = report.findings_for("CHK-05")
        assert [finding.code for finding in findings] == ["PLACEHOLDER_UTC_CLAIMED_PRECISE"]
        finding = findings[0]
        assert finding.record == "syn_rec_05_utc_placeholder_precise"
        assert finding.detail["value"] == "TO_BE_DETERMINED_AT_LAUNCH"

    def test_kept_placeholder_is_informational_not_contradiction(self, report):
        assert "syn_rec_05_utc_placeholder_kept_ok" not in {
            finding.record for finding in report.findings
        }
        assert any(
            item.code == "PLACEHOLDER_TIMESTAMP_KEPT"
            and item.record == "syn_rec_05_utc_placeholder_kept_ok"
            for item in report.informational
        )

    def test_healthy_preparation_records_produce_no_findings(self, report):
        assert "syn_rec_preparation_ok" not in {
            finding.record for finding in report.findings
        }

    def test_prose_only_records_are_counted_never_interpreted(self, report):
        assert report.register_stats["total_records"] == 9
        assert report.register_stats["prose_only_records"] == 1
        assert report.register_stats["records_with_structured_blocks"] == 8
        assert "syn_prose_only_record" not in {
            finding.record for finding in report.findings
        }


class TestCleanFixture:
    def test_zero_contradictions(self, clean_inputs):
        report = run_checks(clean_inputs)
        assert report.findings == ()
        assert sum(report.totals.values()) == 0

    def test_auth_issued_never_completes_a_gate(self, clean_inputs):
        report = run_checks(clean_inputs)
        gate_view = report.gate_view
        assert gate_view["B-G09-R1"]["declared_statuses"] == ["AUTH_ISSUED"]
        assert gate_view["B-G09-R1"]["phase"] == "preparation"
        assert gate_view["B-G09-R1"]["conservative_completed"] is False

    def test_prepared_not_ready_stays_preparation(self, clean_inputs):
        report = run_checks(clean_inputs)
        view = report.gate_view["B-G10-R1"]
        assert view["declared_statuses"] == ["PREPARED_NOT_READY"]
        assert view["phase"] == "preparation"
        assert view["conservative_completed"] is False

    def test_closed_gate_is_completed_only_when_declared_closed(self, clean_inputs):
        report = run_checks(clean_inputs)
        view = report.gate_view["B-G08"]
        assert view["phase"] == "terminal"
        assert view["conservative_completed"] is True

    def test_precise_utc_claim_on_real_value_passes(self, clean_inputs):
        report = run_checks(clean_inputs)
        assert report.findings_for("CHK-05") == ()


class TestNoUpgrade:
    """The checker must not upgrade preparation into completion, ever."""

    def test_mixed_declarations_take_the_conservative_minimum(self, tmp_path):
        register = {
            "snapshot_kind": "coordination-readiness-register-snapshot",
            "snapshot_version": 1,
            "provenance": "SYNTHETIC",
            "records": {
                "rec_early": {"gate_states": {"B-G10-R1": {"status": "PREPARED"}}},
                "rec_late": {"gate_states": {"B-G10-R1": {"status": "CLOSED"}}},
            },
        }
        path = tmp_path / "register.json"
        path.write_text(json.dumps(register), encoding="utf-8")
        inputs = load_inputs(
            path,
            FIXTURES / "identity-table.json",
            FIXTURES / "gate-table.json",
            FIXTURES / "file-table.json",
        )
        report = run_checks(inputs)
        view = report.gate_view["B-G10-R1"]
        assert view["phase"] == "mixed"
        # A PREPARED declaration blocks completion even next to a CLOSED one.
        assert view["conservative_completed"] is False
        # The CLOSED declaration is still a CHK-02 contradiction while
        # B-G09-R1 has no completed declaration.
        chk02 = report.findings_for("CHK-02")
        assert any(
            finding.record == "rec_late" and finding.detail["gate"] == "B-G10-R1"
            for finding in chk02
        )
        # The PREPARED declaration itself is not a trigger.
        assert not any(finding.record == "rec_early" for finding in chk02)

    def test_unknown_status_is_never_completed(self, tmp_path):
        register = {
            "snapshot_kind": "coordination-readiness-register-snapshot",
            "snapshot_version": 1,
            "provenance": "SYNTHETIC",
            "records": {
                "rec_unknown": {
                    "gate_states": {
                        "B-G09-R1": {"status": "SOMETHING_NEW"},
                        "B-G10-R1": {"status": "READY"},
                    }
                }
            },
        }
        path = tmp_path / "register.json"
        path.write_text(json.dumps(register), encoding="utf-8")
        inputs = load_inputs(
            path,
            FIXTURES / "identity-table.json",
            FIXTURES / "gate-table.json",
            FIXTURES / "file-table.json",
        )
        report = run_checks(inputs)
        assert report.gate_view["B-G09-R1"]["conservative_completed"] is False
        assert report.gate_view["B-G09-R1"]["phase"] == "mixed"
        # Unknown prerequisite + READY successor still triggers CHK-02.
        assert len(report.findings_for("CHK-02")) == 1


class TestTimestampClassification:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2026-09-27T12:34:56Z", "precise_utc"),
            ("2026-09-26T14:06:43.595354+00:00", "precise_utc"),
            (None, "missing"),
            ("", "missing"),
            ("  ", "missing"),
            ("TBD", "placeholder"),
            ("TO_BE_DETERMINED_AT_LAUNCH", "placeholder"),
            ("tbd", "placeholder"),
            ("<run-id>", "placeholder"),
            ("RESERVED", "placeholder"),
            ("1970-01-01T00:00:00Z", "placeholder"),
            ("2026-09-27", "date_only"),
            ("2026-09-27T12:34:56", "datetime_no_tz"),
            ("around noon that day", "non_temporal"),
            (12345, "non_temporal"),
        ],
    )
    def test_classify(self, value, expected):
        assert classify_timestamp(value) == expected

    def test_date_only_claimed_precise_fires(self, tmp_path):
        register = {
            "snapshot_kind": "coordination-readiness-register-snapshot",
            "snapshot_version": 1,
            "provenance": "SYNTHETIC",
            "records": {
                "rec": {
                    "timestamps": {"closed_on": "2026-09-27"},
                    "timestamp_precision_claims": {"closed_on": "precise"},
                }
            },
        }
        path = tmp_path / "register.json"
        path.write_text(json.dumps(register), encoding="utf-8")
        inputs = load_inputs(
            path,
            FIXTURES / "identity-table.json",
            FIXTURES / "gate-table.json",
            FIXTURES / "file-table.json",
        )
        findings = run_checks(inputs).findings_for("CHK-05")
        assert [finding.code for finding in findings] == ["DATE_ONLY_CLAIMED_PRECISE"]

    def test_naive_datetime_claimed_precise_fires(self, tmp_path):
        register = {
            "snapshot_kind": "coordination-readiness-register-snapshot",
            "snapshot_version": 1,
            "provenance": "SYNTHETIC",
            "records": {
                "rec": {
                    "timestamps": {"at": "2026-09-27T12:34:56"},
                    "timestamp_precision_claims": {"at": "precise"},
                }
            },
        }
        path = tmp_path / "register.json"
        path.write_text(json.dumps(register), encoding="utf-8")
        inputs = load_inputs(
            path,
            FIXTURES / "identity-table.json",
            FIXTURES / "gate-table.json",
            FIXTURES / "file-table.json",
        )
        findings = run_checks(inputs).findings_for("CHK-05")
        assert [finding.code for finding in findings] == ["NAIVE_DATETIME_CLAIMED_PRECISE"]


class TestOutputsAndReadOnly:
    def test_inputs_are_byte_identical_after_run(self, broken_inputs, tmp_path):
        before = {
            source.path: _file_sha256(source.path) for source in (
                broken_inputs.register,
                broken_inputs.identities,
                broken_inputs.gates,
                broken_inputs.files,
            )
        }
        report = run_checks(broken_inputs)
        snapshot_path, contradictions_path = write_report(report, tmp_path)
        for path, digest in before.items():
            assert _file_sha256(path) == digest

    def test_output_directory_contains_exactly_the_two_reports(
        self, broken_inputs, tmp_path
    ):
        report = run_checks(broken_inputs)
        snapshot_path, contradictions_path = write_report(report, tmp_path)
        assert snapshot_path.name == OUTPUT_SNAPSHOT_NAME
        assert contradictions_path.name == OUTPUT_CONTRADICTIONS_NAME
        assert {item.name for item in tmp_path.iterdir()} == {
            OUTPUT_SNAPSHOT_NAME,
            OUTPUT_CONTRADICTIONS_NAME,
        }

    def test_state_snapshot_shape(self, broken_inputs, tmp_path):
        report = run_checks(broken_inputs)
        snapshot_path, _ = write_report(report, tmp_path)
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        assert snapshot["plan_id"] == PLAN_ID
        assert snapshot["synthetic_inputs"] is True
        assert snapshot["total_findings"] == 7
        assert snapshot["register_stats"]["prose_only_records"] == 1
        assert set(snapshot["check_summary"]) == {
            "CHK-01",
            "CHK-02",
            "CHK-03",
            "CHK-04",
            "CHK-05",
        }
        assert snapshot["gate_view"]["B-G10-R1"]["conservative_completed"] is False
        assert "limitations" in snapshot

    def test_contradictions_markdown_lists_every_finding(self, broken_inputs, tmp_path):
        report = run_checks(broken_inputs)
        _, contradictions_path = write_report(report, tmp_path)
        text = contradictions_path.read_text(encoding="utf-8")
        for check_id in ("CHK-01", "CHK-02", "CHK-03", "CHK-04", "CHK-05"):
            assert check_id in text
        assert "RETIRED_IDENTITY_MARKED_CURRENT" in text
        assert "SUCCESSOR_READY_WITH_INCOMPLETE_PREREQUISITE" in text
        assert "FROZEN_WRITTEN_AS_SEALED" in text
        assert "PATH_TRANSCRIPTION_MISMATCH" in text
        assert "PLACEHOLDER_UTC_CLAIMED_PRECISE" in text
        assert "read-only" in text.lower()
        assert "does not change any gate state" in text

    def test_cli_end_to_end(self, tmp_path):
        out_dir = tmp_path / "out"
        exit_code = cli_main(
            [
                "--register",
                str(FIXTURES / "register-snapshot-broken.json"),
                "--identities",
                str(FIXTURES / "identity-table.json"),
                "--gates",
                str(FIXTURES / "gate-table.json"),
                "--files",
                str(FIXTURES / "file-table.json"),
                "--out-dir",
                str(out_dir),
            ]
        )
        assert exit_code == 0
        assert (out_dir / OUTPUT_SNAPSHOT_NAME).is_file()
        assert (out_dir / OUTPUT_CONTRADICTIONS_NAME).is_file()

    def test_cli_clean_fixture_exit_zero_no_findings(self, tmp_path):
        out_dir = tmp_path / "out"
        exit_code = cli_main(
            [
                "--register",
                str(FIXTURES / "register-snapshot-clean.json"),
                "--identities",
                str(FIXTURES / "identity-table.json"),
                "--gates",
                str(FIXTURES / "gate-table.json"),
                "--files",
                str(FIXTURES / "file-table.json"),
                "--out-dir",
                str(out_dir),
            ]
        )
        assert exit_code == 0
        snapshot = json.loads(
            (out_dir / OUTPUT_SNAPSHOT_NAME).read_text(encoding="utf-8")
        )
        assert snapshot["total_findings"] == 0


class TestFailFastInputs:
    """Malformed inputs raise ReadinessInputError; nothing is guessed."""

    def _run_with_register(self, tmp_path, register):
        path = tmp_path / "register.json"
        path.write_text(json.dumps(register), encoding="utf-8")
        inputs = load_inputs(
            path,
            FIXTURES / "identity-table.json",
            FIXTURES / "gate-table.json",
            FIXTURES / "file-table.json",
        )
        # Block-internal shapes are validated while the checks run.
        run_checks(inputs)

    def test_wrong_snapshot_kind_rejected(self, tmp_path):
        with pytest.raises(ReadinessInputError, match="snapshot_kind"):
            self._run_with_register(
                tmp_path,
                {"snapshot_kind": "something-else", "snapshot_version": 1, "records": {}},
            )

    def test_missing_records_rejected(self, tmp_path):
        with pytest.raises(ReadinessInputError, match="records"):
            self._run_with_register(
                tmp_path,
                {"snapshot_kind": "coordination-readiness-register-snapshot", "snapshot_version": 1},
            )

    def test_identity_table_current_listed_retired_rejected(self, tmp_path):
        table = json.loads(
            (FIXTURES / "identity-table.json").read_text(encoding="utf-8")
        )
        table["slots"]["fpcold_freeze"]["retired"].append("freeze-r3")
        path = tmp_path / "identities.json"
        path.write_text(json.dumps(table), encoding="utf-8")
        with pytest.raises(ReadinessInputError, match="retired"):
            load_inputs(
                FIXTURES / "register-snapshot-clean.json",
                path,
                FIXTURES / "gate-table.json",
                FIXTURES / "file-table.json",
            )

    def test_gate_table_unknown_prerequisite_rejected(self, tmp_path):
        table = json.loads((FIXTURES / "gate-table.json").read_text(encoding="utf-8"))
        table["gates"]["B-G10-R1"]["prerequisites"].append("NO-SUCH-GATE")
        path = tmp_path / "gates.json"
        path.write_text(json.dumps(table), encoding="utf-8")
        with pytest.raises(ReadinessInputError, match="NO-SUCH-GATE"):
            load_inputs(
                FIXTURES / "register-snapshot-clean.json",
                FIXTURES / "identity-table.json",
                path,
                FIXTURES / "file-table.json",
            )

    def test_file_table_bad_sha256_rejected(self, tmp_path):
        table = json.loads((FIXTURES / "file-table.json").read_text(encoding="utf-8"))
        table["files"]["fp-launch-binding"]["sha256"] = "NOT-A-DIGEST"
        path = tmp_path / "files.json"
        path.write_text(json.dumps(table), encoding="utf-8")
        with pytest.raises(ReadinessInputError, match="sha256"):
            load_inputs(
                FIXTURES / "register-snapshot-clean.json",
                FIXTURES / "identity-table.json",
                FIXTURES / "gate-table.json",
                path,
            )

    def test_malformed_record_block_rejected(self, tmp_path):
        register = {
            "snapshot_kind": "coordination-readiness-register-snapshot",
            "snapshot_version": 1,
            "provenance": "SYNTHETIC",
            "records": {"rec": {"identity_refs": {"fpcold_freeze": "freeze-r2"}}},
        }
        with pytest.raises(ReadinessInputError, match="identity_refs"):
            self._run_with_register(tmp_path, register)

    def test_invalid_precision_claim_rejected(self, tmp_path):
        register = {
            "snapshot_kind": "coordination-readiness-register-snapshot",
            "snapshot_version": 1,
            "provenance": "SYNTHETIC",
            "records": {
                "rec": {
                    "timestamps": {"at": "TBD"},
                    "timestamp_precision_claims": {"at": "exact-ish"},
                }
            },
        }
        with pytest.raises(ReadinessInputError, match="timestamp_precision_claims"):
            self._run_with_register(tmp_path, register)

    def test_missing_file_rejected(self, tmp_path):
        with pytest.raises(ReadinessInputError, match="not a file"):
            load_inputs(
                tmp_path / "absent.json",
                FIXTURES / "identity-table.json",
                FIXTURES / "gate-table.json",
                FIXTURES / "file-table.json",
            )

    def test_cli_returns_two_on_input_error(self, tmp_path, capsys):
        exit_code = cli_main(
            [
                "--register",
                str(tmp_path / "absent.json"),
                "--identities",
                str(FIXTURES / "identity-table.json"),
                "--gates",
                str(FIXTURES / "gate-table.json"),
                "--files",
                str(FIXTURES / "file-table.json"),
                "--out-dir",
                str(tmp_path / "out"),
            ]
        )
        assert exit_code == 2
        assert "input error" in capsys.readouterr().err


class TestForbiddenCapabilities:
    """Codex C6: no register writes, no processes, no network, no sampling."""

    MODULE_FILES = [
        Path(__file__).resolve().parents[1] / name
        for name in ("__init__.py", "models.py", "checker.py")
    ]
    FORBIDDEN_MODULES = {
        "socket",
        "subprocess",
        "urllib",
        "urllib.request",
        "http",
        "http.client",
        "requests",
        "ftplib",
        "telnetlib",
        "smtplib",
    }

    def test_module_sources_import_no_network_or_process_modules(self):
        for path in self.MODULE_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert alias.name.split(".")[0] not in self.FORBIDDEN_MODULES, (
                            f"{path.name} imports forbidden module {alias.name}"
                        )
                elif isinstance(node, ast.ImportFrom):
                    root = (node.module or "").split(".")[0]
                    assert root not in self.FORBIDDEN_MODULES, (
                        f"{path.name} imports from forbidden module {node.module}"
                    )

    def test_no_input_paths_are_ever_written(self, broken_inputs, tmp_path):
        """The only writes land in the output directory (regression guard)."""
        written: list[Path] = []
        original_write_text = Path.write_text

        def spy_write_text(self, data, *args, **kwargs):
            written.append(self)
            return original_write_text(self, data, *args, **kwargs)

        Path.write_text = spy_write_text  # type: ignore[method-assign]
        try:
            report = run_checks(broken_inputs)
            snapshot_path, contradictions_path = write_report(report, tmp_path)
        finally:
            Path.write_text = original_write_text  # type: ignore[method-assign]
        assert {path.resolve() for path in written} == {
            snapshot_path.resolve(),
            contradictions_path.resolve(),
        }

    def test_package_exports_plan_identifier(self):
        import ak_trading.readiness as package

        assert package.PLAN_ID == "COORDINATION-READINESS-REPORT-r1"
        assert package.TOOL_NAME == "coordination-readiness-checker"


# Q70 successor regressions. All original tests/fixtures above remain unchanged.
# Every filesystem input below is a disposable SYNTHETIC stand-in.
def _q70_inputs(root, records=None, references=None, source_register=None):
    root.mkdir(parents=True, exist_ok=True)
    documents = [
        {"snapshot_kind": "coordination-readiness-register-snapshot", "snapshot_version": 1,
         "provenance": "SYNTHETIC", "records": records or {}},
        {"table_kind": "coordination-readiness-identity-table", "table_version": 1,
         "provenance": "SYNTHETIC", "slots": {}},
        {"table_kind": "coordination-readiness-gate-table", "table_version": 1,
         "provenance": "SYNTHETIC", "gates": {}},
        {"table_kind": "coordination-readiness-file-table", "table_version": 1,
         "provenance": "SYNTHETIC", "files": references or {}, "payloads": {}},
    ]
    if source_register is not None:
        documents[0]["source_register"] = source_register
    paths = [root / name for name in ("register.json", "identities.json", "gates.json", "files.json")]
    for path, document in zip(paths, documents):
        path.write_text(json.dumps(document), encoding="utf-8")
    return load_inputs(*paths)


def _q70_assert_rejected_without_writes(inputs, output, monkeypatch):
    sources = (inputs.register, inputs.identities, inputs.gates, inputs.files)
    before = {source.path: source.path.read_bytes() for source in sources}
    writes = []
    original_write_text = Path.write_text
    original_mkdir = Path.mkdir

    def write_spy(path, *args, **kwargs):
        writes.append(("write_text", str(path)))
        return original_write_text(path, *args, **kwargs)

    def mkdir_spy(path, *args, **kwargs):
        writes.append(("mkdir", str(path)))
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_spy)
    monkeypatch.setattr(Path, "mkdir", mkdir_spy)
    with pytest.raises(ReadinessInputError):
        write_report(run_checks(inputs), output)
    assert writes == []
    argv = ["--register", str(inputs.register.path), "--identities", str(inputs.identities.path),
            "--gates", str(inputs.gates.path), "--files", str(inputs.files.path), "--out-dir", str(output)]
    assert cli_main(argv) == 2
    assert writes == []
    assert before == {source.path: source.path.read_bytes() for source in sources}


@pytest.mark.parametrize("target", [OUTPUT_SNAPSHOT_NAME, OUTPUT_CONTRADICTIONS_NAME])
def test_q70_existing_target_rejects_both_before_any_write(tmp_path, monkeypatch, target):
    inputs = _q70_inputs(tmp_path / "inputs")
    output = tmp_path / "out"
    output.mkdir()
    sentinel = output / target
    sentinel.write_bytes(b"SYNTHETIC protected target\n")
    _q70_assert_rejected_without_writes(inputs, output, monkeypatch)
    assert sentinel.read_bytes() == b"SYNTHETIC protected target\n"
    assert sorted(path.name for path in output.iterdir()) == [target]


@pytest.mark.parametrize("kind", ["source", "source-relative", "file", "file-relative", "file-directory", "source-directory", "dotdot", "case"])
@pytest.mark.parametrize("target", [OUTPUT_SNAPSHOT_NAME, OUTPUT_CONTRADICTIONS_NAME])
def test_q70_absent_protected_reference_rejects_without_mkdir(tmp_path, monkeypatch, kind, target):
    output = tmp_path / "out"
    protected = output / target
    references = {}
    source = None
    if kind == "source":
        source = str(protected)
    elif kind == "source-relative":
        source = "../out/" + target
    elif kind == "source-directory":
        source = str(output)
    else:
        value = str(protected)
        if kind == "file-relative":
            value = "../out/" + target
        elif kind == "file-directory":
            value = str(output)
        elif kind == "dotdot":
            value = str(tmp_path / "spelling" / ".." / "out" / target)
        elif kind == "case":
            value = str(protected).upper()
        references = {"SYNTHETIC-file": {"path": value}}
    inputs = _q70_inputs(tmp_path / "inputs", references=references, source_register=source)
    _q70_assert_rejected_without_writes(inputs, output, monkeypatch)
    assert not output.exists()
    assert not protected.exists()


@pytest.mark.parametrize("role", ["register", "identities", "gates", "files"])
def test_q70_all_input_roles_protected_as_second_target(tmp_path, monkeypatch, role):
    from dataclasses import replace

    inputs = _q70_inputs(tmp_path / "inputs")
    output = tmp_path / "out"
    output.mkdir()
    source = getattr(inputs, role)
    second = output / OUTPUT_CONTRADICTIONS_NAME
    second.write_bytes(source.path.read_bytes())
    inputs = replace(inputs, **{role: replace(source, path=second)})
    protected = second.read_bytes()
    _q70_assert_rejected_without_writes(inputs, output, monkeypatch)
    assert not (output / OUTPUT_SNAPSHOT_NAME).exists()
    assert second.read_bytes() == protected


@pytest.mark.parametrize("kind", ["directory-symlink", "ancestor-symlink", "directory-junction", "ancestor-junction", "target-symlink", "dangling-target", "hardlink"])
def test_q70_output_links_stay_inside_scratch_and_are_rejected(tmp_path, monkeypatch, kind):
    import os

    inputs = _q70_inputs(tmp_path / "inputs")
    destination = tmp_path / "SYNTHETIC-destination"
    destination.mkdir()
    protected = destination / "sentinel.json"
    protected.write_bytes(b"SYNTHETIC linked bytes\n")
    output = tmp_path / "out"
    if kind == "directory-symlink":
        output.symlink_to(destination, target_is_directory=True)
    elif kind == "ancestor-symlink":
        output.symlink_to(destination, target_is_directory=True)
        output = output / "nested"
    elif kind in {"directory-junction", "ancestor-junction"}:
        import _winapi

        _winapi.CreateJunction(str(destination), str(output))
        assert output.is_junction()
        if kind == "ancestor-junction":
            output = output / "nested"
    else:
        output.mkdir()
        target = output / OUTPUT_CONTRADICTIONS_NAME
        if kind == "hardlink":
            os.link(protected, target)
        else:
            target.symlink_to(protected if kind == "target-symlink" else destination / "absent")
    _q70_assert_rejected_without_writes(inputs, output, monkeypatch)
    assert protected.read_bytes() == b"SYNTHETIC linked bytes\n"
    assert sorted(path.name for path in destination.iterdir()) == ["sentinel.json"]
    assert not (output / OUTPUT_SNAPSHOT_NAME).exists()


@pytest.mark.parametrize("target", [OUTPUT_SNAPSHOT_NAME, OUTPUT_CONTRADICTIONS_NAME])
def test_q70_protected_reference_resolves_local_link_ancestor(tmp_path, monkeypatch, target):
    output = tmp_path / "out"
    output.mkdir()
    alias = tmp_path / "SYNTHETIC-reference-alias"
    alias.symlink_to(output, target_is_directory=True)
    inputs = _q70_inputs(tmp_path / "inputs", references={"f": {"path": str(alias / target)}})
    _q70_assert_rejected_without_writes(inputs, output, monkeypatch)
    assert list(output.iterdir()) == []


def test_q70_reference_metadata_never_reads_reference_contents(tmp_path, monkeypatch):
    protected = tmp_path / "SYNTHETIC-frozen.bin"
    protected.write_bytes(b"SYNTHETIC must not read these referenced bytes")
    inputs = _q70_inputs(tmp_path / "inputs", references={
        "file": {"path": str(protected)}, "symbolic": {"path": "rounds/<run-id>/binding.json"}})
    original_read = Path.read_bytes

    def read_guard(path):
        assert path != protected, "referenced content was read"
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", read_guard)
    write_report(run_checks(inputs), tmp_path / "reports")
    assert inputs.files.payload["files"]["symbolic"]["path"] == "rounds/<run-id>/binding.json"
    assert protected.stat().st_size == len(b"SYNTHETIC must not read these referenced bytes")


def test_q70_unc_reference_rejected_before_metadata(tmp_path, monkeypatch):
    inputs = _q70_inputs(tmp_path / "inputs", references={"f": {"path": "//SYNTHETIC-host/share/file.json"}})
    original_lstat = Path.lstat

    def local_only(path, *args, **kwargs):
        assert not str(path).replace("\\", "/").startswith("//"), "UNC metadata attempted"
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", local_only)
    _q70_assert_rejected_without_writes(inputs, tmp_path / "out", monkeypatch)


def test_q70_relative_cli_paths_anchor_at_invocation_cwd(tmp_path, monkeypatch):
    _q70_inputs(tmp_path / "inputs", source_register="../out/contradictions.md")
    monkeypatch.chdir(tmp_path)
    assert cli_main(["--register", "inputs/register.json", "--identities", "inputs/identities.json",
                     "--gates", "inputs/gates.json", "--files", "inputs/files.json", "--out-dir", "out"]) == 2
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(("expected", "actual", "count"), [
    ("/root/f", "root/f", 1), ("//host/share/f", "host/share/f", 1),
    ("D:/root/f", "D:root/f", 1), ("//host/share-a/f", "//host/share-b/f", 1),
    ("a//b", "a/b", 1), ("a/./b", "a/b", 1), ("a/../b", "b", 1),
    ("Case/f", "case/f", 1), ("a/b/", "a/b", 1),
    ("D:\\root\\f", " D:/root/f ", 0), ("\\\\host\\share\\f", "//host/share/f", 0),
])
def test_q70_path_transcription_is_lexical(tmp_path, expected, actual, count):
    inputs = _q70_inputs(tmp_path, records={"SYNTHETIC-record": {"file_transcriptions": {"f": {"path": actual}}}},
                        references={"f": {"path": expected}})
    assert len(run_checks(inputs).findings_for("CHK-04")) == count


@pytest.mark.parametrize("field", ["path", "revision"])
@pytest.mark.parametrize("value", [False, None, 21, [], {}, "", " \t"])
def test_q70_malformed_present_field_has_one_finding_with_original_value(tmp_path, field, value):
    inputs = _q70_inputs(tmp_path, records={"SYNTHETIC-record": {"file_transcriptions": {"f": {field: value}}}},
                        references={"f": {"path": "valid/path", "revision": "r_21"}})
    findings = run_checks(inputs).findings_for("CHK-04")
    assert len(findings) == 1
    finding = findings[0]
    assert finding.code == field.upper() + "_TRANSCRIPTION_MALFORMED"
    assert finding.record == "SYNTHETIC-record"
    assert finding.detail == {"file": "f", "field": field, "actual": value}
    assert type(finding.detail["actual"]) is type(value)


@pytest.mark.parametrize("field", ["path", "revision", "sha256"])
@pytest.mark.parametrize("authority", ["missing-field", "null", "blank", "missing-file"])
def test_q70_supplied_field_without_authority_is_unverifiable(tmp_path, field, authority):
    entry = {} if authority == "missing-field" else {field: None if authority == "null" else ""}
    if field == "sha256" and authority == "blank":
        # Existing authoritative SHA schema deliberately rejects empty hashes.
        with pytest.raises(ReadinessInputError):
            _q70_inputs(tmp_path, references={"f": entry})
        return
    actual = hashlib.sha256(b"SYNTHETIC").hexdigest() if field == "sha256" else "SYNTHETIC-value"
    inputs = _q70_inputs(tmp_path, records={"r": {"file_transcriptions": {"f": {field: actual}}}},
                        references={} if authority == "missing-file" else {"f": entry})
    findings = run_checks(inputs).findings_for("CHK-04")
    assert len(findings) == 1
    assert findings[0].code == field.upper() + "_TRANSCRIPTION_UNVERIFIABLE"
    assert findings[0].detail["actual"] == actual


def test_q70_missing_optional_fields_stay_absent(tmp_path):
    inputs = _q70_inputs(tmp_path, records={"r": {"file_transcriptions": {"f": {}}}}, references={"f": {}})
    assert run_checks(inputs).findings_for("CHK-04") == ()


@pytest.mark.parametrize(("value", "expected"), [
    ("1970-01-01T00:00:00.000Z", "placeholder"),
    ("1970-01-01T00:00:00.000+00:00", "placeholder"),
    ("1970-01-01T01:00:00.000+01:00", "placeholder"),
    ("1969-12-31T19:00:00-05:00", "placeholder"),
    ("0001-01-01T00:00:00.000000000Z", "placeholder"),
    ("0001-01-01T01:00:00+01:00", "placeholder"),
    ("1970-01-01T00:00:00.000000001Z", "precise_utc"),
    ("0001-01-01T00:00:00.000000001Z", "precise_utc"),
    ("1969-12-31T23:59:59.999999999Z", "precise_utc"),
    ("1970-01-01T00:00:01Z", "precise_utc"),
    ("1900-01-01T00:00:00Z", "precise_utc"),
    ("2000-02-29T12:34:56Z", "precise_utc"),
    ("2024-02-29T12:34:56+05:30", "precise_utc"),
    ("9999-99-99T99:99:99Z", "non_temporal"),
    ("1900-02-29T00:00:00Z", "non_temporal"),
    ("2023-02-29T00:00:00Z", "non_temporal"),
    ("2024-02-29T24:00:00Z", "non_temporal"),
    ("2024-02-29T23:60:00Z", "non_temporal"),
    ("2024-02-29T23:59:60Z", "non_temporal"),
    ("2024-02-29T00:00:00+24:00", "non_temporal"),
    ("2024-02-29T00:00:00+00:60", "non_temporal"),
    ("2024-02-29T00:00:00-01:99", "non_temporal"),
    ("2023-02-29", "non_temporal"),
    ("2024-02-29T25:00:00", "non_temporal"),
])
def test_q70_real_calendar_and_full_fraction_sentinel_controls(tmp_path, value, expected):
    assert classify_timestamp(value) == expected
    inputs = _q70_inputs(tmp_path, records={"r": {"timestamps": {"at": value},
                        "timestamp_precision_claims": {"at": "precise"}}})
    findings = run_checks(inputs).findings_for("CHK-05")
    assert len(findings) == (0 if expected == "precise_utc" else 1)


@pytest.mark.parametrize("claim", ["precise", "placeholder", None])
def test_q70_json_markdown_precision_truth_and_mixed_fields(tmp_path, claim):
    record = {"timestamps": {"SYNTHETIC-claimed": "TBD", "SYNTHETIC-kept": "RESERVED",
                             "SYNTHETIC-real": "2000-02-29T12:00:00Z"},
              "timestamp_precision_claims": {"SYNTHETIC-kept": "placeholder", "SYNTHETIC-real": "precise"}}
    if claim is not None:
        record["timestamp_precision_claims"]["SYNTHETIC-claimed"] = claim
    inputs = _q70_inputs(tmp_path / "inputs", records={"SYNTHETIC-record": record})
    report = run_checks(inputs)
    snapshot, markdown = write_report(report, tmp_path / "out")
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    text = markdown.read_text(encoding="utf-8")
    assert data["check_summary"]["CHK-05"]["findings"] == (1 if claim == "precise" else 0)
    assert data["informational_count"] == (1 if claim == "precise" else 2)
    kept = {item.detail["field"] for item in report.informational if item.code == "PLACEHOLDER_TIMESTAMP_KEPT"}
    assert "SYNTHETIC-kept" in kept
    assert ("SYNTHETIC-claimed" in kept) is (claim != "precise")
    info_text = text.split("## Informational (not contradictions)", 1)[1]
    assert ("SYNTHETIC-claimed" in info_text) is (claim != "precise")
    assert "SYNTHETIC-kept" in info_text
    assert ("PLACEHOLDER_UTC_CLAIMED_PRECISE" in text) is (claim == "precise")

