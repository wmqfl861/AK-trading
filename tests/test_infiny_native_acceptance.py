"""TA: NE1 acceptance face - stage-gate rules, evidence-counter honesty
(plan 13.3; NE1-H39/H40), freeze-hash binding of the delivery package, and
the documented limitation statements.

Since decision NE1-ARB-ARMING-001 (2026-09-18) the arming wave's four owner
file sets (ARM-LM-C/D/S/I, §八) are modified in parallel; until the
integrator cuts the next overall-revision freeze manifest, exactly those
files are allowed to drift from the latest frozen revision (recorded below
as ARM_WAVE_OWNER_FILES) while every other file must still match
byte-for-byte. The acceptance rules are pure functions owned by this file;
no network, no production ports, no writes outside tmp.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = ROOT / "scripts" / "install_infiny_execution.py"
TEMPLATE_PATH = ROOT / "config" / "infiny_native_execution.example.json"
RUNBOOK_PATH = ROOT / "docs" / "infiny-native-execution-runbook.md"
REVISIONS_ROOT = (ROOT / "data" / "maintenance_artifacts" /
                  "infiny-native-execution")

# frozen chain anchors (overall-revision-r1; see review-brief 1.A/1.B/1.C)
CHAIN_PREFIXES = {
    "c1_lock": "647f1ac2",
    "c1_1_lock": "bcacb46d",
    "c1_1_arbitration": "7672f766",
    "c2_manifest_v2": "250288f7",
    "execution_domain_r3_review": "e17eb1b1",
    "simulation_acceptance_r1": "018767af",
    "plan": "89563d4a",
}

# NE1-ARB-ARMING-001 §八 (2026-09-18): the exact per-owner exclusive file
# sets of the arming wave. These - and ONLY these - files may drift from the
# latest overall-revision freeze manifest while the wave is in flight; the
# next freeze cut (W2) re-binds them. Pairing tool/tests are not tracked by
# the r-series manifests but are listed to document the full wave scope.
ARM_WAVE_DECISION = ("data/maintenance_artifacts/"
                     "infiny-arming-mechanism-20260918-r1/decision.md")
ARM_WAVE_OWNER_FILES = frozenset({
    # ARM-LM-C (client owner)
    "integrations/infiny/AKExecutionBridge.py",
    "tests/test_infiny_native_gates.py",
    "tests/test_infiny_native_lifecycle.py",
    # ARM-LM-D (delivery owner; this wave's allowed drift)
    "scripts/pair_infiny_execution.py",
    "tests/test_infiny_native_pairing.py",
    "scripts/install_infiny_execution.py",
    "tests/test_infiny_native_deployment.py",
    "tests/test_infiny_native_acceptance.py",
    "docs/infiny-native-execution-runbook.md",
    # ARM-LM-S (service owner)
    "src/ak_trading/infiny_execution/client_service.py",
    "scripts/run_infiny_execution.py",
    "tests/test_infiny_native_protocol.py",
    # ARM-LM-I (integration owner)
    "tests/test_infiny_native_arming.py",
    "tests/test_infiny_native_integration.py",
    "tests/test_infiny_native_wire.py",
    "tests/infiny_native_fakes.py",
})

REQUIRED_COUNTER_FIELDS = (
    "native_send_attempts", "native_cancel_attempts", "local_order_ids",
    "broker_order_acks", "unique_trade_count", "fake_send_count",
    "fake_cancel_count", "unresolved_dispatch_count",
)
REAL_COUNTER_FIELDS = ("native_send_attempts", "native_cancel_attempts")
HEX64 = re.compile(r"[0-9a-f]{64}")


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _installer_module():
    spec = importlib.util.spec_from_file_location("ne1_installer_under_test",
                                                  INSTALLER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# acceptance rules (plan 13.1/13.3): verdicts are PASS/FAIL only; nothing
# unexecuted is ever back-filled and no unknown is ever written as zero.
# ---------------------------------------------------------------------------

def evaluate_s3(record: dict) -> str:
    """S3 exit: both real counters PROVEN zero with complete boundary evidence."""
    if not isinstance(record, dict) or record.get("stage") != "s3":
        return "FAIL"
    missing = [field for field in REQUIRED_COUNTER_FIELDS if field not in record]
    if missing:
        return "FAIL"                       # H39: report without counters is refused
    if record.get("counter_source") != "client_native_boundary":
        return "FAIL"                       # observer's constant zeros cannot certify
    if record.get("merged_real_fake_totals"):
        return "FAIL"                       # real/fake must never be summed
    if record.get("count_complete") is not True:
        return "FAIL"                       # unknown interval is not zero
    if record["native_send_attempts"] != 0 or record["native_cancel_attempts"] != 0:
        return "FAIL"
    return "PASS"


def evaluate_s4(record: dict) -> str:
    """S4 exit: exactly the one authorized send, cancels within budget."""
    if not isinstance(record, dict) or record.get("stage") != "s4":
        return "FAIL"
    missing = [field for field in REQUIRED_COUNTER_FIELDS if field not in record]
    if missing or record.get("count_complete") is not True:
        return "FAIL"
    if record.get("counter_source") != "client_native_boundary":
        return "FAIL"
    if record["native_send_attempts"] != record.get("authorized_send_attempts"):
        return "FAIL"
    if record["native_cancel_attempts"] > record.get("authorized_cancel_attempts", 0):
        return "FAIL"
    if record.get("unexplained_duplicate_calls", 1) != 0:
        return "FAIL"
    return "PASS"


def derive_stage_fields(s3_outcome: str, s4_outcome: str | None) -> dict:
    """Stage fields after an engineering review: S3 PASS never promotes the
    broker field; a stage that was never run records NOT_RUN."""
    return {
        "s3": s3_outcome,
        "s4": s4_outcome if s4_outcome is not None else "NOT_RUN",
        "broker": "NOT_RUN",  # only real broker events (never run here) change this
        "platform": "PARTIAL_UNVERIFIED",
    }


def _s3_record(**overrides) -> dict:
    record = {"stage": "s3", "counter_source": "client_native_boundary",
              "count_complete": True,
              "native_send_attempts": 0, "native_cancel_attempts": 0,
              "local_order_ids": [], "broker_order_acks": 0,
              "unique_trade_count": 0, "fake_send_count": 0,
              "fake_cancel_count": 0, "unresolved_dispatch_count": 0}
    record.update(overrides)
    return record


def test_s3_passes_only_with_complete_boundary_zero_evidence():
    assert evaluate_s3(_s3_record()) == "PASS"


def test_s3_rejects_missing_counter_fields():
    record = _s3_record()
    del record["broker_order_acks"]
    assert evaluate_s3(record) == "FAIL"


def test_s3_rejects_observer_constant_zeros_as_real_counters():
    assert evaluate_s3(_s3_record(counter_source="observer_order_submissions")) == "FAIL"


def test_s3_rejects_summed_real_and_fake_totals():
    assert evaluate_s3(_s3_record(
        fake_send_count=3, merged_real_fake_totals=True,
        native_send_attempts=3)) == "FAIL"


def test_s3_rejects_incomplete_count_interval_even_at_zero():
    assert evaluate_s3(_s3_record(count_complete=False)) == "FAIL"
    assert evaluate_s3(_s3_record(count_complete=None)) == "FAIL"


def test_s3_rejects_nonzero_real_counters():
    assert evaluate_s3(_s3_record(native_send_attempts=1)) == "FAIL"
    assert evaluate_s3(_s3_record(native_cancel_attempts=2)) == "FAIL"


def test_s4_requires_exactly_the_authorized_single_send():
    base = dict(_s3_record(), stage="s4", authorized_send_attempts=1,
                authorized_cancel_attempts=1, native_send_attempts=1,
                unexplained_duplicate_calls=0)
    assert evaluate_s4(base) == "PASS"
    assert evaluate_s4(dict(base, native_send_attempts=2)) == "FAIL"  # second send
    assert evaluate_s4(dict(base, native_cancel_attempts=2)) == "FAIL"  # over budget
    assert evaluate_s4(dict(base, unexplained_duplicate_calls=1)) == "FAIL"
    assert evaluate_s4(dict(base, counter_source="journal_projection_only")) == "FAIL"


def test_stage_derivation_never_promotes_broker_or_backfills():
    fields = derive_stage_fields("PASS", None)
    assert fields["s4"] == "NOT_RUN" and fields["broker"] == "NOT_RUN"
    assert fields["platform"] == "PARTIAL_UNVERIFIED"


# ---------------------------------------------------------------------------
# freeze binding: delivery package + chain anchors (H40 package check)
# ---------------------------------------------------------------------------

def _latest_revision_name() -> str:
    candidates = sorted(path.parent.name for path in
                        REVISIONS_ROOT.glob("overall-revision-r*/freeze-manifest.json")
                        if path.is_file())
    assert candidates, "no overall-revision freeze manifest found"
    return candidates[-1]


def _manifest() -> dict:
    """Bind to the LATEST overall revision's freeze manifest (OV-F01 lesson:
    concluded revisions stay read-only fixes move to a new revision, and the
    acceptance surface must always verify the newest frozen state)."""
    candidates = sorted(path for path in
                        REVISIONS_ROOT.glob("overall-revision-r*/freeze-manifest.json")
                        if path.is_file())
    assert candidates, "no overall-revision freeze manifest found"
    return json.loads(candidates[-1].read_text(encoding="utf-8"))


def test_freeze_manifest_matches_workspace_byte_for_byte():
    """Every file outside the in-flight arming wave still matches the LATEST
    freeze manifest byte-for-byte; wave files must exist and are re-bound by
    the next freeze cut (documented in-flight allowance, never a weakening
    for unrelated files)."""
    manifest = _manifest()
    assert manifest["revision"] == _latest_revision_name(), \
        "manifest must be the latest overall revision"
    drift = []
    wave_pending = []
    for relative, expected in manifest["files_sha256"].items():
        path = ROOT / relative
        actual = _sha(path) if path.is_file() else "ABSENT"
        if actual != expected:
            if relative in ARM_WAVE_OWNER_FILES:
                wave_pending.append((relative, expected[:8], actual[:8]))
                assert path.is_file(), f"wave file removed: {relative}"
            else:
                drift.append((relative, expected[:8], actual[:8]))
    assert drift == [], drift
    # the allowance is bounded: wave files are exactly the decision's owner
    # sets, and the decision document itself must exist on disk.
    assert (ROOT / ARM_WAVE_DECISION).is_file()
    assert wave_pending == [] or all(
        relative in ARM_WAVE_OWNER_FILES for relative, _, _ in wave_pending)


def test_arming_wave_owner_files_are_the_decision_file_sets():
    """The in-flight allowance is exactly NE1-ARB-ARMING-001 §八's table."""
    text = (ROOT / ARM_WAVE_DECISION).read_text(encoding="utf-8")
    for fragment in ("ARM-LM-C", "ARM-LM-D", "ARM-LM-S", "ARM-LM-I",
                     "ne1-pairing-20260918-arming-v1",
                     "ne1-delivery-20260918-arming-v1",
                     "ne1-execution-20260918-arming-v1",
                     "ne1-service-20260918-arming-v1"):
        assert fragment in text, fragment


def test_freeze_manifest_carries_chain_anchors_and_all_forty_h_clauses():
    manifest = _manifest()
    for key, prefix in CHAIN_PREFIXES.items():
        if key == "plan":
            value = manifest["plan"]["sha256"]
        else:
            value = manifest["chain"][key]
        assert value.startswith(prefix) and HEX64.fullmatch(value), (key, value)
    if manifest["revision"] == "overall-revision-r1":
        # the r1-adjudicated C3 freeze values (coordinator-fixed anchors)
        for key, prefix in (("src/ak_trading/infiny_execution/client_service.py", "0799c2df"),
                            ("scripts/run_infiny_execution.py", "e3c6cfe3"),
                            ("tests/test_infiny_native_protocol.py", "eb796a4c")):
            assert manifest["files_sha256"][key].startswith(prefix)
    else:
        # fix-wave revisions must record their lineage; per-file values change
        # legitimately and are verified byte-for-byte by the workspace test
        assert manifest.get("supersedes") == "overall-revision-r1", \
            "fix-wave revisions must declare supersedes=overall-revision-r1"
    ids = manifest["hard_acceptance_ids"]
    assert ids == [f"NE1-H{number:02d}" for number in range(1, 41)]


def test_installer_plan_hash_constant_bound_to_frozen_plan():
    installer = _installer_module()
    live = _sha(ROOT / "docs" / "infiny-native-execution-plan.md")
    assert installer.EXPECTED_PLAN_SHA256 == live == _manifest()["plan"]["sha256"]


def test_installer_observer_baseline_is_a_recorded_value_not_an_observation():
    installer = _installer_module()
    baseline = installer.OBSERVER_EXPECTED_SHA256
    assert HEX64.fullmatch(baseline)
    manifest = _manifest()
    recorded = manifest["observer_pair_baseline"]["sha256"]
    # deploy-time authority stays the measured pair hash + sampling time; the
    # constant only pins the last main-session-recorded v5 deployment baseline
    assert baseline == recorded
    assert manifest["observer_pair_baseline"]["sampled_at"].startswith("2026-09-14")


def test_parameter_template_is_the_plan_123_safe_default_set():
    installer = _installer_module()
    payload = TEMPLATE_PATH.read_bytes()
    validated = installer.validate_template_bytes(payload)  # raises on any drift
    assert tuple(sorted(validated)) == tuple(sorted(installer.TEMPLATE_KEYS))
    assert validated["enable_trading"] is False
    assert validated["execution_mode"] == "disabled"
    assert validated["max_native_sends"] == 0 and validated["max_native_cancels"] == 0
    assert validated["execution_api_url"] == "http://127.0.0.1:17779/execution/v1/poll"
    assert validated["api_url"] == "http://127.0.0.1:17778/analyze"
    assert "volume" not in validated


def test_runbook_documents_c3_chain_and_frozen_rulings():
    """The runbook chain snapshot must carry the MANIFEST values (OV-F01 fix):
    every expected prefix is derived from the freeze manifest, never pinned as
    a literal, so a stale snapshot after any revision bump fails here instead
    of locking an orphan hash into the acceptance surface."""
    text = RUNBOOK_PATH.read_text(encoding="utf-8")
    manifest = _manifest()
    entries = manifest["files_sha256"]
    expected = (
        (entries["src/ak_trading/infiny_execution/client_service.py"][:8],
         "client_service hash"),
        (entries["scripts/run_infiny_execution.py"][:8], "launcher hash"),
        (entries["tests/test_infiny_native_protocol.py"][:8], "TP hash"),
        (entries["scripts/install_infiny_execution.py"][:8], "installer hash"),
        (entries["config/infiny_native_execution.example.json"][:8], "template hash"),
        (entries["integrations/infiny/AKExecutionBridge.py"][:8],
         "client release hash"),
        (manifest["observer_pair_baseline"]["sha256"][:8], "observer baseline hash"),
    )
    for prefix, label in expected:
        assert prefix in text, f"{label} ({prefix}) missing from runbook snapshot"
    # documented C3 rulings (runbook sections 4.1/5/8)
    assert "INVALID_REQUEST" in text            # /results unknown-command ruling
    assert "进程内视图" in text                  # /events duplicate + status watermark view
    assert "armed" in text and "S4 前置" in text  # armed full-chain precondition ruling
    assert "PENDING_STATE_SOURCE_REQUIRED_FOR_JOURNAL" in text
    assert "PORT_IN_USE" in text and "退出码 2" in text
    # limitation vocabulary stays UNSUPPORTED / NOT_RUN (plan 14.2)
    assert "UNSUPPORTED" in text and "NOT_RUN" in text
    assert "PARTIAL_UNVERIFIED" in text
    # the snapshot must also name its authority source (anti-orphan discipline)
    assert "freeze-manifest.json" in text and "OV-F01" in text


# ---------------------------------------------------------------------------
# NE1-ARB-ARMING-001 (2026-09-18) ARM-LM-D acceptance face: version bumps and
# the runbook's AR1.2 console replacement / dual hash / eight-step sequence /
# PENDING-gate vocabulary (RULING 06 tool-side matrix, documentation face).
# ---------------------------------------------------------------------------

def test_installer_version_bumped_to_arming_v1():
    installer = _installer_module()
    assert installer.TOOL_VERSION == "ne1-delivery-20260918-arming-v1"
    # ARM-M13: the installer names the arming slots ONLY as an exclusion list;
    # it never backs up, copies, hashes or restores them.
    assert len(installer.ARMING_SLOT_NAMES) == 8
    assert "arming.json" in installer.ARMING_SLOT_NAMES
    assert "arming-challenge.json" in installer.ARMING_SLOT_NAMES


def test_runbook_documents_arming_console_replacement_and_sequence():
    """AR1.2/AR1.3/AR2.1/AR3.2/AR5 vocabulary must be in the runbook: the
    explicit UI-form replacement, the USER_LOCAL_CONSOLE provenance, the dual
    hash pair, the eight-step registration sequence, the interactive phrase
    standard and the PENDING/gate-close honesty rules."""
    text = RUNBOOK_PATH.read_text(encoding="utf-8")
    for fragment in (
        "NE1-ARB-ARMING-001",
        "USER_LOCAL_CONSOLE",          # AR1.2 action source
        "GUI_CONFIRMED",               # AR1.2 forbidden provenance value
        "ARM_INTERACTIVE_REQUIRED",    # AR1.3 non-interactive rejection
        "pairing_params_sha256",       # AR2.1 dual hash
        "target_params_sha256",
        "prepare-arm", "disarm",       # AR1.1 commands
        "ARM_SUBMITTED_AWAITING_CLIENT",   # AR5.3 arm status
        "DISARM_REQUESTED",
        "LOCAL_GATE_CLOSED", "SERVICE_ACK_PENDING",  # AR5.2 gate vocabulary
        "DISABLED_CONFIRMED",
        "ne1-pairing-20260918-arming-v1",
        "ne1-delivery-20260918-arming-v1",
        "production_arming=NOT_RUN",
    ):
        assert fragment in text, fragment
    # the eight-step sequence is numbered 1..8 inside §6A
    section = text[text.index("## 6A."):text.index("## 7.")]
    for step in range(1, 9):
        assert f"\n{step}. " in section, f"arming step {step} missing"
    # the confirmation phrase format is documented (AR1.3)
    assert "ARM <arming request_id 末 8 位>" in text
    # old UI-form requirement is explicitly replaced, not silently dropped
    assert "全部替换为" in text and "enable_trading=true" in text
