"""Prepare the generic acceptance-input manifest F in its fixed new namespace.

r6 fixed-namespace separation (docs/repair-blocking-contract-inputs-20261112.md
§4): the legacy T7 manifest keeps ``tests/fixtures/v2/ep1_t7`` with its own
schema, attachments and historical source hashes, all original bytes; this
script produces the NEW generic manifest
``tests/fixtures/v2/ep1_t7_contract/acceptance-input-manifest.json`` and its
two attachments (a legal JSON copy of the unmodified ``POSITIVE_RECIPE`` and
the real ``payoff-training-plans.json`` written through the production codec)
from the actual frozen materials of the tree it is pointed at.

Boundary contract (r6-critical-repair02/03): every read and write — the declared
project root, each pinned/manifest-listed input, the source floor, the output
parent/target and the staging directory — is resolved and verified to stay
inside the declared root with no symlink/junction/reparse alias anywhere in
its component chain and no writable hard-link alias on any bound or walked
regular file (st_nlink > 1 refused), BEFORE anything is created; a
pre-existing staging directory is refused, never deleted. Content equality
never substitutes for containment. The offline guard is never disabled: stale
ak_trading import state is reset in sys.modules only, leaving the guard's
meta-path finder, audit hooks and module patches fully installed for this
process and its children.

The script only prepares input metadata. It never fits models, never calls
the outer stand-in, the cold-chain smoke, experiment registration, research
entry points, replay or holdout consumption. Output location is fixed by the
code; an existing target directory, incomplete inputs, or any pin/world
mismatch aborts without publishing anything.

Usage:
    python -B -X utf8 scripts/prepare_t7_contract_inputs.py --project-root <copy>
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import stat
import sys
import time
from hashlib import sha256
from pathlib import Path

OLD_DIR = "tests/fixtures/v2/ep1_t7"
NEW_DIR = "tests/fixtures/v2/ep1_t7_contract"
OLD_MANIFEST_REL = f"{OLD_DIR}/acceptance-input-manifest.json"
OLD_RECIPE_REL = f"{OLD_DIR}/recipe.json"
OLD_PARTITION_REL = f"{OLD_DIR}/partition-params.json"
NEW_RECIPE_REL = f"{NEW_DIR}/recipe.json"
NEW_PLANS_REL = f"{NEW_DIR}/payoff-training-plans.json"
M5_RESEARCH_REL = "tests/fixtures/v2/m5_research.json"
SUPPORT_REL = "tests/v2_ep1_t7_support.py"
THIS_SCRIPT_REL = "scripts/prepare_t7_contract_inputs.py"
THIS_TEST_REL = "tests/test_prepare_t7_contract_inputs.py"
PACKAGING_RUN_ID = "t7-contract-inputs-prep-r1"

# Bound authoritative input pins (ACTUAL-TREE-PINS.json, 2026-11-12; full
# SHA-256 values, compared byte-exact — never prefixes).
PIN_OLD_MANIFEST = "01fa369f5c2fcaa4eed05a647d0640c37b9155b5c40712700a114d242f5d4e0a"
PIN_SUPPORT = "7f1640176969392931a5fa8fced4da712afe9f60ed0d861bf7cf55982bc8eb29"
PIN_OLD_RECIPE = "9c625aebc76cc903b4989394fc9bda2143d3f1703657986cd4dbbada1c306290"
PIN_OLD_PARTITION = "dc1b885cbe8779d6a535927734054a650ed125917de981a70f1802980c61c639"
PIN_M5_RESEARCH = "18be290be95b52723400d19e19a122a9e4ff7022b9f4b2c9344e922a576ccce6"

EXPECTED_PLAN_COUNT = 7
EXPECTED_RUN_POLICY_COUNT = 2


class PreparationError(RuntimeError):
    """Packaging abort with a precise, reportable cause."""


def _reparse_point(path: Path) -> bool:
    """True for symlinks, junctions and any other reparse alias."""
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return True
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _single_link(path: Path, label: str) -> None:
    """Reject a regular file carrying a writable hard-link alias.

    A hard link (os.link) shares the file's inode: no reparse point, identical
    resolution, identical bytes — the only detectable signal is the link
    count, so any bound or walked file with st_nlink > 1 is refused before it
    is read or any output is generated. Nothing is modified to mask an alias.
    """
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise PreparationError(f"input_unreadable:{label}:{error}") from None
    if getattr(info, "st_nlink", 1) > 1:
        raise PreparationError(f"file_has_multiple_links:{label}")


def _boundary_checked(project_root: Path, relative: object, label: str) -> Path:
    """Resolve one in-tree path and enforce the containment contract.

    Rejects non-path types, absolute paths and ``..`` lexically, any reparse
    point (symlink/junction/other) on an existing component of the chain from
    the declared root down to the target, and any resolution that escapes the
    declared root. Returns the resolved path; never creates anything.
    """
    if not isinstance(relative, (str, Path)):
        raise PreparationError(f"path_type_invalid:{label}")
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise PreparationError(f"path_not_in_tree:{label}:{relative}")
    root_resolved = project_root.resolve()
    chain = [project_root]
    for part in candidate.parts:
        chain.append(chain[-1] / part)
    for component in chain[:-1]:
        if _reparse_point(component):
            raise PreparationError(f"path_is_reparse_point:{label}:{component.name}")
    if _reparse_point(chain[-1]):
        raise PreparationError(f"path_is_reparse_point:{label}:{candidate.as_posix()}")
    resolved = chain[-1].resolve()
    if not resolved.is_relative_to(root_resolved):
        raise PreparationError(f"path_escapes_declared_root:{label}:{candidate.as_posix()}")
    return resolved


def _real_files_under(project_root: Path, directory: str) -> list[str]:
    """Every real single-link file under an in-tree directory, rejecting
    reparse aliases and writable hard-link aliases.

    A symlink/junction directory or file inside the walked tree is a boundary
    violation and aborts — it is never silently skipped or followed. A walked
    file with more than one hard link is a writable alias and aborts too.
    """
    root = _boundary_checked(project_root, directory, "walk_root")
    found: list[str] = []
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        here = Path(current)
        for name in list(dirnames):
            if _reparse_point(here / name):
                raise PreparationError(
                    f"path_is_reparse_point:walk:{(here / name).relative_to(project_root)}")
        for name in filenames:
            file_path = here / name
            if _reparse_point(file_path):
                raise PreparationError(
                    f"path_is_reparse_point:walk:{file_path.relative_to(project_root)}")
            if not file_path.resolve().is_relative_to(project_root.resolve()):
                raise PreparationError(
                    f"path_escapes_declared_root:walk:{file_path.relative_to(project_root)}")
            _single_link(file_path, file_path.relative_to(project_root).as_posix())
            found.append(file_path.relative_to(project_root).as_posix())
    return sorted(found)


def _sha(data: bytes) -> str:
    return sha256(data).hexdigest()


def _read(project_root: Path, relative: str) -> bytes:
    resolved = _boundary_checked(project_root, relative, "input")
    _single_link(resolved, relative)
    try:
        return resolved.read_bytes()
    except OSError as error:
        raise PreparationError(f"input_unreadable:{relative}:{error}") from None


def _require_sha(project_root: Path, relative: str, expected: str, label: str) -> bytes:
    payload = _read(project_root, relative)
    actual = _sha(payload)
    if actual != expected:
        raise PreparationError(f"pin_mismatch:{label}:{relative}:{actual}!={expected}")
    return payload


def _reset_ak_trading_import_state() -> None:
    """Re-import ak_trading from the declared root WITHOUT weakening offline
    protection.

    The offline launcher pre-imports ak_trading (its guard module) from the
    launching repository and pins PYTHONPATH for python descendants. The
    guard's meta-path finder, audit hooks and dotenv/database/subprocess/
    network module patches stay installed for this process and its children;
    only the stale ak_trading module entries are dropped from sys.modules, so
    the next import resolves from the declared root (inserted ahead of every
    inherited path entry) and any re-imported guarded module is still wrapped
    by the guard loader.
    """
    for name in [name for name in sys.modules
                 if name == "ak_trading" or name.startswith("ak_trading.")]:
        del sys.modules[name]


def main() -> int:
    started = time.perf_counter()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True,
                        help="independent source copy that receives the new F directory")
    arguments = parser.parse_args()
    if not isinstance(arguments.project_root, str) or not arguments.project_root:
        print(json.dumps({"ok": False, "error": "project_root_must_be_non_empty_str"}))
        return 2
    project_root = Path(arguments.project_root)
    if _reparse_point(project_root):
        print(json.dumps({"ok": False, "error": "path_is_reparse_point:project_root"}))
        return 3
    project_root = project_root.resolve()
    if not project_root.is_dir():
        print(json.dumps({"ok": False, "error": f"project_root_not_a_directory:{project_root}"}))
        return 2
    target_dir = project_root / Path(NEW_DIR)
    if target_dir.exists() or _reparse_point(target_dir):
        print(json.dumps({"ok": False, "error": "target_directory_already_exists",
                          "target": NEW_DIR}))
        return 2
    for required in (OLD_MANIFEST_REL, OLD_RECIPE_REL, OLD_PARTITION_REL,
                     SUPPORT_REL, M5_RESEARCH_REL,
                     "src/ak_trading/v2/artifacts.py", "scripts/test_offline.py"):
        if not (project_root / Path(required)).is_file():
            print(json.dumps({"ok": False, "error": f"required_input_missing:{required}"}))
            return 2

    # Import discipline: every ak_trading import must resolve inside the
    # declared project root, never an installed or sibling copy — with the
    # offline guard left fully installed (see _reset_ak_trading_import_state).
    _reset_ak_trading_import_state()
    for entry in (str(project_root / "src"), str(project_root / "tests")):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    import ak_trading.v2.artifacts as artifacts  # noqa: E402
    from ak_trading.v2 import schemas  # noqa: E402
    from ak_trading.v2.contracts import PAYOFF_TRAINING_PLAN_SELECTION_RULE  # noqa: E402
    from ak_trading.v2.strategies.registry import (  # noqa: E402
        build_strategy_spec, select_exit_binding, strategy_specs_v2)
    from ak_trading.v2.validation.experiments import (  # noqa: E402
        _payoff_training_plans, content, payoff_training_plan_set_sha256)
    from ak_trading.v2.validation.splits import SplitSpec  # noqa: E402
    import v2_ep1_t7_support as support  # noqa: E402

    resolved_artifacts = Path(artifacts.__file__).resolve()
    resolved_support = Path(support.__file__).resolve()
    if resolved_artifacts.parents[2] != (project_root / "src").resolve():
        print(json.dumps({"ok": False, "error": "import_root_mismatch:artifacts",
                          "resolved": str(resolved_artifacts)}))
        return 2
    if resolved_support.parent != (project_root / "tests").resolve():
        print(json.dumps({"ok": False, "error": "import_root_mismatch:support",
                          "resolved": str(resolved_support)}))
        return 2

    # 1. Bound pins over the original materials, then the old manifest's own
    #    attachment table item by item (bytes and full SHA-256).
    _require_sha(project_root, SUPPORT_REL, PIN_SUPPORT, "support")
    old_manifest_payload = _require_sha(
        project_root, OLD_MANIFEST_REL, PIN_OLD_MANIFEST, "old_manifest")
    old_recipe_payload = _require_sha(
        project_root, OLD_RECIPE_REL, PIN_OLD_RECIPE, "old_recipe")
    old_partition_payload = _require_sha(
        project_root, OLD_PARTITION_REL, PIN_OLD_PARTITION, "old_partition")
    _require_sha(project_root, M5_RESEARCH_REL, PIN_M5_RESEARCH, "m5_research")
    old_manifest = json.loads(old_manifest_payload.decode("utf-8"))
    if old_manifest.get("schema_version") != "ak_ep1_t7_acceptance_input_v1":
        raise PreparationError("old_manifest_schema_unexpected")
    if old_manifest.get("seed") != support.POSITIVE_RECIPE["seed"]:
        raise PreparationError("old_manifest_seed_mismatch")
    if old_manifest.get("labels") != list(support.POSITIVE_RECIPE["labels"]):
        raise PreparationError("old_manifest_labels_mismatch")
    for relative, meta in sorted(old_manifest["files"].items()):
        payload = _read(project_root, f"{OLD_DIR}/{relative}")
        if len(payload) != meta["bytes"]:
            raise PreparationError(f"old_attachment_length:{relative}")
        if _sha(payload) != meta["sha256"]:
            raise PreparationError(f"old_attachment_hash:{relative}")
    # Every file in the legacy fixture tree is a real, non-alias file.
    legacy_files = _real_files_under(project_root, OLD_DIR)

    # Zero-change snapshot of every pre-existing file in the fixture area.
    watch = {}
    for directory in sorted({OLD_DIR, "tests/fixtures/v2"}):
        for relative in _real_files_under(project_root, directory):
            watch[relative] = _sha(_read(project_root, relative))

    # 2. Rebuild the SYNTHETIC world with the unmodified support helpers and
    #    require full-byte equality with the frozen input files (no fitting;
    #    any difference stops the packaging instead of rewriting inputs).
    bundle, sessions = support.build_positive_world()
    if len(sessions) != support.POSITIVE_RECIPE["world_days"]:
        raise PreparationError("world_day_count_mismatch")
    rebuilt = {
        f"{OLD_DIR}/inputs/sessions.json": schemas.canonical_json_bytes(
            {"sessions": [content(s) for s in sessions]}),
        f"{OLD_DIR}/inputs/bars-minute.json": schemas.canonical_json_bytes(
            {"bars": [content(b) for b in bundle.bars if b.duration_seconds == 60]}),
        f"{OLD_DIR}/inputs/bars-daily.json": schemas.canonical_json_bytes(
            {"bars": [content(b) for b in bundle.bars if b.duration_seconds == 86400]}),
        f"{OLD_DIR}/inputs/rule.json": schemas.canonical_json_bytes(
            {"rules": [content(r) for r in bundle.rules]}),
    }
    for relative, expected_bytes in sorted(rebuilt.items()):
        if _read(project_root, relative) != expected_bytes:
            raise PreparationError(f"world_rebuild_bytes_mismatch:{relative}")

    # 3. Legacy recipe: reproduce the historical serialization expression from
    #    the source constants, prove it equals the frozen bytes, then build the
    #    new legal copy with the production canonical serializer — same fields,
    #    recipe_id and seed; only the serialization representation changes.
    reproduced = schemas.canonical_json_bytes(
        support.POSITIVE_RECIPE).replace(b",", b",\n")
    if reproduced != old_recipe_payload:
        raise PreparationError("old_recipe_serialization_not_reproduced")
    try:
        schemas.parse_json_bytes(old_recipe_payload)
    except schemas.SchemaError:
        pass
    else:
        raise PreparationError("old_recipe_unexpectedly_legal_json")
    new_recipe_payload = schemas.canonical_json_bytes(support.POSITIVE_RECIPE)
    if schemas.parse_json_bytes(new_recipe_payload) != support.POSITIVE_RECIPE:
        raise PreparationError("new_recipe_copy_not_byte_faithful")

    # 4. Partition: recompute the real outer bounds from the actual calendar
    #    and require equality with the frozen partition parameters (433-day
    #    world, 426-day outer TRAIN, original boundaries).
    partition = schemas.parse_json_bytes(old_partition_payload)
    bounds = support.outer_split_bounds(sessions)
    frozen = partition["split"]
    for name, value in zip(("train_start_nano", "train_end_nano", "calibration_start_nano",
                            "calibration_end_nano", "test_start_nano", "test_end_nano"),
                           bounds):
        if frozen[name] != value:
            raise PreparationError(f"partition_bound_mismatch:{name}")
    head = support.POSITIVE_RECIPE["head_days"]
    train_days = support.POSITIVE_RECIPE["train_days"]
    if (frozen["train_start_nano"] != sessions[head].start_nano
            or frozen["train_end_nano"] != sessions[head + train_days - 1].end_nano):
        raise PreparationError("partition_train_segment_mismatch")

    # 5. Real pure declarations: the production config path (which reads the
    #    authorized test material) and the real payoff training plans; exactly
    #    the prescribed seven plans and two run policies, nothing adjusted.
    config, declared_digest = support.positive_config(bundle, sessions, PACKAGING_RUN_ID)
    specs = tuple(s for mode in ("MINUTE", "DAILY") for s in strategy_specs_v2(mode)
                  if s.strategy_id in config["strategy_ids"])
    planned, skipped = _payoff_training_plans(
        bundle, config, SplitSpec(**config["split_specs"][0]), specs)
    plans = [entry[3] for entry in planned]
    recomputed_digest = payoff_training_plan_set_sha256(plans)
    if recomputed_digest != declared_digest or config["payoff_training_plan_sha256"] != declared_digest:
        raise PreparationError("plan_set_digest_inconsistent")
    if len(plans) != EXPECTED_PLAN_COUNT:
        raise PreparationError(f"plan_count:{len(plans)}!={EXPECTED_PLAN_COUNT}")
    if len(config["run_policies"]) != EXPECTED_RUN_POLICY_COUNT:
        raise PreparationError(
            f"run_policy_count:{len(config['run_policies'])}!={EXPECTED_RUN_POLICY_COUNT}")

    # 6. Bindings from the current real producers: full objects and content
    #    identities, no placeholder hashes.
    spec = build_strategy_spec("BREAKOUT", "MINUTE")
    binding = select_exit_binding(spec)
    if spec.strategy_id not in config["strategy_ids"]:
        raise PreparationError("binding_strategy_not_declared")
    if binding.binding_sha256 != config["run_policies"][1]["fixed_exit_binding_sha256"]:
        raise PreparationError("binding_sha_not_matching_declared_policy")
    bindings = [{
        "strategy_id": spec.strategy_id,
        "strategy_code_sha256": spec.code_sha256,
        "exit_binding_sha256": binding.binding_sha256,
        "strategy": content(spec),
        "exit_binding": content(binding),
    }]

    # 7. Plans file through the production codec, with strict readback.
    plans_payload = schemas.encode_record_file(
        "PayoffTrainingPlan", schemas.PAYOFF_TRAINING_PLAN_SCHEMA_ID, tuple(plans))
    decoded = schemas.decode_payoff_training_plan_file(plans_payload)
    if tuple(decoded) != tuple(plans):
        raise PreparationError("plans_codec_roundtrip_mismatch")

    # 8. Assemble F (nine root fields; costs and rules both reference the one
    #    real rule document — it already carries fees, margins and slippage).
    new_files = {NEW_RECIPE_REL: new_recipe_payload, NEW_PLANS_REL: plans_payload}
    source_floor = _real_files_under(project_root, "src/ak_trading/v2")
    table_paths = sorted({
        OLD_MANIFEST_REL, OLD_RECIPE_REL, OLD_PARTITION_REL,
        f"{OLD_DIR}/inputs/sessions.json", f"{OLD_DIR}/inputs/bars-minute.json",
        f"{OLD_DIR}/inputs/bars-daily.json", f"{OLD_DIR}/inputs/rule.json",
        NEW_RECIPE_REL, NEW_PLANS_REL, SUPPORT_REL, M5_RESEARCH_REL,
        THIS_SCRIPT_REL, THIS_TEST_REL,
        *source_floor,
        artifacts.ACCEPTANCE_RUNNER_PATH,
        artifacts.ACCEPTANCE_OFFLINE_RUNNER_PATH,
        *artifacts.PUBLIC_TEST_FILES,
        *artifacts.DOWNSTREAM_TEST_FILES,
    })
    entries = []
    for relative in table_paths:
        payload = new_files.get(relative)
        if payload is None:
            payload = _read(project_root, relative)
        entries.append({"path": relative, "bytes": len(payload), "sha256": _sha(payload)})
    document = {
        "schema_version": artifacts.ACCEPTANCE_INPUT_MANIFEST_SCHEMA_ID,
        "dataset_kind": "SYNTHETIC",
        "labels": list(support.POSITIVE_RECIPE["labels"]),
        "generation": {"recipe": NEW_RECIPE_REL, "seed": support.POSITIVE_RECIPE["seed"]},
        "inputs": {
            "minute_bars": [f"{OLD_DIR}/inputs/bars-minute.json"],
            "daily_bars": [f"{OLD_DIR}/inputs/bars-daily.json"],
            "calendar": [f"{OLD_DIR}/inputs/sessions.json"],
            "rules": [f"{OLD_DIR}/inputs/rule.json"],
            "costs": [f"{OLD_DIR}/inputs/rule.json"],
        },
        "splits": {
            "source": OLD_PARTITION_REL,
            "split_id": partition["split_id"],
            "world_days": support.POSITIVE_RECIPE["world_days"],
            "outer_train_days": train_days,
            "split": frozen,
            "fit_config_overrides": partition["fit_config_overrides"],
            "policy_floors_unchanged": partition["policy_floors_unchanged"],
        },
        "selection": {
            "selection_rule": PAYOFF_TRAINING_PLAN_SELECTION_RULE,
            "plan_set_sha256": declared_digest,
            "plans_file": NEW_PLANS_REL,
            "plan_count": len(plans),
            "skipped_windows": [{"strategy_id": s, "window_start_nano": w, "reason": r}
                                for s, w, r in skipped],
            "run_policies": config["run_policies"],
        },
        "bindings": bindings,
        "files": entries,
    }
    manifest_payload = schemas.canonical_json_bytes(document)
    if schemas.parse_json_bytes(manifest_payload) != document:
        raise PreparationError("manifest_roundtrip_mismatch")

    # 9. Atomic publication under the boundary contract: the whole publish
    #    chain is resolved and verified BEFORE anything is created; a
    #    pre-existing staging directory is refused (never deleted); only the
    #    staging created by this run is cleaned up on failure.
    output_parent = _boundary_checked(project_root, "tests/fixtures/v2", "output_parent")
    final_dir = _boundary_checked(project_root, NEW_DIR, "output_target")
    staging = output_parent / ".ep1_t7_contract.staging"
    if staging.exists() or _reparse_point(staging):
        raise PreparationError("staging_already_exists")
    staging_created = False
    try:
        staging.mkdir()
        staging_created = True
        (staging / "recipe.json").write_bytes(new_recipe_payload)
        (staging / "payoff-training-plans.json").write_bytes(plans_payload)
        (staging / "acceptance-input-manifest.json").write_bytes(manifest_payload)
        for name, expected in (("recipe.json", new_recipe_payload),
                               ("payoff-training-plans.json", plans_payload),
                               ("acceptance-input-manifest.json", manifest_payload)):
            if (staging / name).read_bytes() != expected:
                raise PreparationError(f"staging_reread_mismatch:{name}")
        if (project_root / Path(NEW_DIR)).exists() or _reparse_point(project_root / Path(NEW_DIR)):
            raise PreparationError("target_directory_appeared_during_packaging")
        staging.rename(final_dir)
    finally:
        if staging_created and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    # 10. Real strict verification over the published tree and the zero-change
    #     proof for every pre-existing input file.
    h_f = artifacts.verify_acceptance_input_manifest(project_root)
    for relative in sorted(watch):
        current = _sha(_read(project_root, relative))
        if current != watch[relative]:
            raise PreparationError(f"preexisting_input_changed:{relative}")
    print(json.dumps({
        "ok": True,
        "project_root": str(project_root),
        "manifest": artifacts.ACCEPTANCE_INPUT_MANIFEST_PATH,
        "h_f": h_f,
        "files_listed": len(entries),
        "legacy_files_seen": len(legacy_files),
        "plan_count": len(plans),
        "run_policies": len(config["run_policies"]),
        "skipped_windows": len(skipped),
        "python": platform.python_version(),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PreparationError as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        raise SystemExit(3) from None
