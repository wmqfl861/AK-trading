"""Q31-W10: C20-1.0.1 external mirror alignment tests.

The authoritative structural validator is
``tools.benchmarks.contracts.validate_result_record``;
``docs/benchmarks/result-schema.json`` is its human-readable external mirror
and ``tools/benchmarks/cli.py --help`` is its user-facing version mirror.  The
NF20 r2 integration window (C20-1.0.1, 2026-09-24) deliberately left both
mirrors at C20-1.0.0 (both files were outside the r2 write set); the Q31-W10
finite alignment work order closed that lag.  These tests pin the alignment:

* the mirror's ``contract_version`` const equals ``contracts.CONTRACT_VERSION``
  and its title names the behavior version;
* the mirror ACCEPTS records shaped like real C20-1.0.1 runner output:
  formal batches carrying per-batch ``verify_status`` (``null`` for timed-out
  or never-run batches, the missing-``rc`` pre-empted shape included) and
  smoke batch records without ``verify_status``;
* the mirror REJECTS wrong contract versions, formal records missing the
  per-batch ``verify_status``, records missing required batch fields, and
  records with batch field type errors;
* ``cli.py --help`` reports the behavior version and no longer mentions
  C20-1.0.0 (copy-only alignment - the frozen budget/state-machine tests in
  test_harness.py own the control-flow surface);
* the frozen repetition minimum (5 batches x 20 operations) and the F01
  INVALID semantics referenced by the mirror description remain the code's
  own frozen constants (tripwire against smuggling rule changes in through
  the schema sync).

Uses the ``jsonschema`` package (test-only dependency for EXTERNAL mirror
validation; the harness itself never imports it).  Every record built here is
a TEST_SIMULATED synthetic record for FORMAT VALIDATION ONLY - never a
benchmark measurement and never baseline-eligible.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator  # test-only (external mirror validation)

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import conftest as helpers  # noqa: E402
from conftest import build_result_record, run_cli  # noqa: E402
from tools.benchmarks import contracts as C  # noqa: E402

SCHEMA_PATH = REPO_ROOT / "docs" / "benchmarks" / "result-schema.json"

_SHA = "9" * 64
_OMIT = object()  # sentinel: build the batch WITHOUT the verify_status key


# ---------------------------------------------------------------------------
# mirror loading
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def mirror_schema() -> dict:
    schema = C.load_json_file(SCHEMA_PATH, "result schema mirror")
    Draft7Validator.check_schema(schema)
    return schema


@pytest.fixture(scope="module")
def mirror(mirror_schema) -> Draft7Validator:
    return Draft7Validator(mirror_schema)


def _error_paths(validator: Draft7Validator, record: dict) -> list:
    return sorted(e.json_path for e in validator.iter_errors(record))


def _formal_batches(statuses: list) -> list:
    """Batch-record shapes exactly as runner.execute_formal_batches writes
    them under C20-1.0.1 (``_OMIT`` -> smoke-era shape without the key)."""
    batches = []
    for index, status in enumerate(statuses):
        batch = {
            "index": index,
            "process": "subprocess",
            "sample_count": 20,
            "failure_count": 0,
            "digest": _SHA,
            "elapsed_ns": 1_000_000,
            "rc": 0,
            "timed_out": False,
        }
        if status is not _OMIT:
            batch["verify_status"] = status
        batches.append(batch)
    return batches


# ---------------------------------------------------------------------------
# version alignment (mirror + CLI help copy)
# ---------------------------------------------------------------------------

class TestMirrorVersionAlignment:
    def test_schema_const_matches_behavior_version(self, mirror_schema):
        assert mirror_schema["properties"]["contract_version"]["const"] == C.CONTRACT_VERSION
        assert C.CONTRACT_VERSION == "C20-1.0.1"  # pin: r2-released behavior
        assert C.CONTRACT_VERSION in mirror_schema["title"]

    def test_record_schema_version_unchanged(self, mirror_schema):
        # RESULT_SCHEMA_VERSION did NOT bump with C20-1.0.0 -> C20-1.0.1 and
        # the mirror must keep tracking it exactly.
        const = mirror_schema["properties"]["schema_version"]["const"]
        assert const == C.RESULT_SCHEMA_VERSION == "ak-benchmark-result/1.0.0"

    def test_cli_help_reports_behavior_version(self):
        rc, out, err = run_cli(["--help"])
        assert rc == 0, err
        assert C.CONTRACT_VERSION in out
        assert "C20-1.0.0" not in out  # stale literal removed (copy-only change)


# ---------------------------------------------------------------------------
# acceptance: the new mirror accepts C20-1.0.1-shaped records
# ---------------------------------------------------------------------------

class TestMirrorAcceptsC2001Records:
    def test_formal_all_pass_record_accepted(self, mirror):
        record = build_result_record(
            measurement_over={"batches": _formal_batches(["PASS"] * 5)},
        )
        assert _error_paths(mirror, record) == []

    def test_formal_batch_verify_fail_invalid_record_accepted(self, mirror):
        # F01 outcome: same digest everywhere, one batch verify FAIL -> the
        # case is INVALID; the record (and its per-batch FAIL status) must
        # still be schema-valid.
        record = build_result_record(
            status="INVALID",
            status_reason="BATCH_VERIFY_FAILED: formal batch(es) [3] returned "
                          "verify FAIL while emitting the same digest as the "
                          "other batches (hold constraint violated (TEST))",
            measurement_over={
                "batches": _formal_batches(
                    ["PASS", "PASS", "PASS", "FAIL", "PASS"]
                )
            },
        )
        assert _error_paths(mirror, record) == []

    def test_formal_timed_out_batches_null_verify_status_accepted(self, mirror):
        # killed child: rc null, verify_status null; pre-empted batch: no rc
        # key at all, note CASE_DEADLINE_REACHED_BEFORE_BATCH.
        killed = {
            "index": 4, "process": "subprocess", "sample_count": 7,
            "failure_count": 0, "digest": _SHA, "verify_status": None,
            "elapsed_ns": 1, "rc": None, "timed_out": True,
            "note": "BATCH_KILLED_ON_CASE_DEADLINE",
        }
        preempted = {
            "index": 5, "process": "subprocess", "sample_count": 0,
            "failure_count": 0, "digest": None, "verify_status": None,
            "elapsed_ns": 0, "timed_out": True,
            "note": "CASE_DEADLINE_REACHED_BEFORE_BATCH",
        }
        record = build_result_record(
            status="TIMEOUT",
            status_reason="CASE_DEADLINE_REACHED; raw samples preserved",
            measurement_over={
                "batches": _formal_batches(["PASS"] * 4) + [killed, preempted]
            },
        )
        assert _error_paths(mirror, record) == []

    def test_smoke_inprocess_batch_without_verify_status_accepted(self, mirror):
        # smoke path is unchanged by F01: single in-process batch record,
        # no verify_status key anywhere.
        record = build_result_record(
            scope="smoke",
            measurement_over={
                "batches": [{
                    "index": 0, "process": "inprocess", "sample_count": 100,
                    "failure_count": 0, "digest": _SHA, "elapsed_ns": 1,
                    "timed_out": False,
                }]
            },
        )
        assert _error_paths(mirror, record) == []


# ---------------------------------------------------------------------------
# rejection: wrong version / missing required batch fields / type errors
# ---------------------------------------------------------------------------

class TestMirrorRejection:
    def test_wrong_contract_version_rejected(self, mirror):
        for wrong in ("C20-1.0.0", "C20-2.0.0"):
            record = build_result_record(
                measurement_over={"batches": _formal_batches(["PASS"] * 5)},
            )
            record["contract_version"] = wrong
            paths = _error_paths(mirror, record)
            assert "$.contract_version" in paths, wrong

    def test_formal_batch_missing_verify_status_rejected(self, mirror):
        # the C20-1.0.0-era shape (no per-batch verify_status) must NOT pass
        # as a formal C20-1.0.1 record.
        record = build_result_record(
            measurement_over={"batches": _formal_batches([_OMIT] * 5)},
        )
        paths = _error_paths(mirror, record)
        assert len(paths) == 5
        assert all(p.startswith("$.measurement.batches[") for p in paths)

    def test_formal_batch_missing_required_field_rejected(self, mirror):
        batches = _formal_batches(["PASS"] * 5)
        del batches[2]["digest"]
        record = build_result_record(measurement_over={"batches": batches})
        assert "$.measurement.batches[2]" in _error_paths(mirror, record)

    def test_formal_batch_type_error_rejected(self, mirror):
        batches = _formal_batches(["PASS"] * 5)
        batches[1]["index"] = "1"  # string, not integer
        record = build_result_record(measurement_over={"batches": batches})
        assert "$.measurement.batches[1].index" in _error_paths(mirror, record)

    def test_formal_batch_bad_verify_status_type_rejected(self, mirror):
        batches = _formal_batches(["PASS"] * 5)
        batches[0]["verify_status"] = "pass"  # not in {PASS, FAIL, null}
        record = build_result_record(measurement_over={"batches": batches})
        assert "$.measurement.batches[0].verify_status" in _error_paths(mirror, record)


# ---------------------------------------------------------------------------
# end-to-end: real runner output passes the new external mirror
# ---------------------------------------------------------------------------

class TestRealRunnerOutputPassesMirror:
    def test_core_smoke_record_validates_against_mirror(self, tmp_path, mirror):
        rc, out, err = run_cli(
            [
                "run",
                "--source-map", "tests/benchmarks/fixtures/core/source-map.json",
                "--suite", "tools/benchmarks/suite.json",
                "--case", "core/counter_digest",
                "--output-root", str(tmp_path / "out"),
                "--profile", "smoke",
            ],
            timeout=60,
        )
        assert rc == 0, err
        record = helpers.load_case_record(
            tmp_path / "out", helpers.only_run_id(tmp_path / "out"),
            "core/counter_digest",
        )
        assert record["status"] == "OK"
        assert record["contract_version"] == C.CONTRACT_VERSION
        assert _error_paths(mirror, record) == []


# ---------------------------------------------------------------------------
# frozen-rule tripwires (schema sync must not change measurement rules)
# ---------------------------------------------------------------------------

class TestFrozenRulesTripwire:
    def test_formal_repetition_minimum_unchanged(self):
        # 5 batches x 20 operations (H20.4) - the schema sync may not touch it.
        assert C.FORMAL_MIN_BATCHES == 5
        assert C.FORMAL_MIN_OPS_PER_BATCH == 20
        assert C.FORMAL_MIN_TOTAL_SAMPLES == 100

    def test_status_vocabulary_unchanged(self):
        # any-batch verify FAIL -> INVALID stays in the frozen vocabulary;
        # the mirror description may only REFLECT this, not redefine it.
        assert C.CASE_STATUSES == (
            "OK", "INVALID", "TIMEOUT", "ERROR", "DRIFT",
        )
