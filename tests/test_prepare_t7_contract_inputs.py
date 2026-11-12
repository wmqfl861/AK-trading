"""Real preparation of the generic acceptance-input manifest F (r6 §6 layer 2).

Exercises scripts/prepare_t7_contract_inputs.py against full temporary source
copies carrying the REAL six T7 attachments and the legacy manifest: the red
state before F, the genuine packaging run, the strict green verification and
lock round-trip, refusal/atomicity counterexamples, the forbidden-phase traps
(fit / outer stand-in / cold chain / legacy freeze are made to blow up while
the real packaging path runs), and the content discipline of the generated F.
All trees live under tmp_path; the repository's own fixture directories are
never written by these tests.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest
import v2_ep1_t7_support as t7_support

from ak_trading.v2 import artifacts as a
from ak_trading.v2 import schemas as s

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/prepare_t7_contract_inputs.py"
OLD_DIR_REL = "tests/fixtures/v2/ep1_t7"
NEW_DIR_REL = "tests/fixtures/v2/ep1_t7_contract"
ZERO = "0" * 64


def _tree_hashes(tree: Path, relative: str) -> dict[str, str]:
    root = tree / Path(relative)
    return {p.relative_to(tree).as_posix(): sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def _build_real_tree(tree: Path) -> Path:
    """Full source copy with the real legacy inputs, stubs for not-yet-landed
    lock-required deliverables, and NO new-namespace directory."""
    from test_v2_m1_artifacts import _mirror_lock_tree
    _mirror_lock_tree(tree)
    # The mirror stages only the lock-bound subset; the packaging script (and
    # its import-root discipline) needs the complete current source tree.
    shutil.copytree(ROOT / "src", tree / "src", dirs_exist_ok=True)
    shutil.rmtree(tree / NEW_DIR_REL)
    shutil.rmtree(tree / OLD_DIR_REL)
    shutil.copytree(ROOT / OLD_DIR_REL, tree / OLD_DIR_REL)
    shutil.copyfile(ROOT / "tests/fixtures/v2/m5_research.json",
                    tree / "tests/fixtures/v2/m5_research.json")
    shutil.copyfile(ROOT / "tests/v2_ep1_t7_support.py",
                    tree / "tests/v2_ep1_t7_support.py")
    shutil.copyfile(SCRIPT, tree / "scripts/prepare_t7_contract_inputs.py")
    shutil.copyfile(Path(__file__), tree / "tests/test_prepare_t7_contract_inputs.py")
    return tree


def _run_script(tree: Path) -> subprocess.CompletedProcess:
    import os
    environment = {key: value for key, value in os.environ.items()
                   if key not in ("PYTHONPATH", "PYTHONHOME")}
    return subprocess.run(
        [sys.executable, "-B", "-X", "utf8", str(tree / "scripts/prepare_t7_contract_inputs.py"),
         "--project-root", str(tree)],
        capture_output=True, text=True, cwd=str(tree), check=False, env=environment)


@pytest.fixture(scope="module")
def prepared_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    tree = _build_real_tree(tmp_path_factory.mktemp("prepared") / "tree")
    before = _tree_hashes(tree, OLD_DIR_REL)
    result = _run_script(tree)
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["ok"] is True
    assert report["plan_count"] == 7
    assert report["run_policies"] == 2
    assert _tree_hashes(tree, OLD_DIR_REL) == before  # legacy inputs untouched
    return tree


def test_red_state_keeps_exact_not_frozen_error(tmp_path: Path) -> None:
    # F10: the legacy manifest exists at the old path, no F in the new
    # namespace — the generic consumer keeps the exact frozen message and
    # never falls back to the old path or guesses the T7 schema.
    tree = _build_real_tree(tmp_path / "tree")
    with pytest.raises(a.ArtifactsError, match="acceptance_input_manifest_not_frozen"):
        a.verify_acceptance_input_manifest(tree)


def test_real_preparation_green_and_lock_roundtrip(prepared_tree: Path,
                                                   tmp_path: Path) -> None:
    tree = prepared_tree
    h_f = a.verify_acceptance_input_manifest(tree)
    assert h_f == a.acceptance_input_manifest_sha256(tree)
    assert h_f == a._fixture_hashes(tree)[a.ACCEPTANCE_INPUT_MANIFEST_PATH]
    # The real F feeds the real lock pipeline over the same tree.
    report_path = tmp_path / "R0.md"
    report_path.write_bytes(b"R0 frozen-input check record\n")
    lock = a.build_contract_lock(tree, "r6-prep-r1", a.sha256_hex(report_path.read_bytes()))
    lock_path = tmp_path / "contract-lock.json"
    lock_path.write_bytes(s.canonical_json_bytes(lock))
    a.verify_contract_lock(lock_path, tree)
    a.verify_contract_lock_with_report(lock_path, tree, report_path)


def test_generated_manifest_content_discipline(prepared_tree: Path) -> None:
    document = s.parse_json_bytes(
        (prepared_tree / a.ACCEPTANCE_INPUT_MANIFEST_PATH).read_bytes())
    assert set(document) == a._ACCEPTANCE_MANIFEST_KEYS
    assert document["labels"] == ["SYNTHETIC", "SCENARIO"]
    assert document["generation"]["seed"] == 7
    assert document["inputs"]["rules"] == document["inputs"]["costs"] == \
        [f"{OLD_DIR_REL}/inputs/rule.json"]
    assert document["splits"]["world_days"] == 433
    assert document["splits"]["outer_train_days"] == 426
    assert document["selection"]["plan_count"] == 7
    assert document["selection"]["selection_rule"] == "FIRST_FEASIBLE_P1_OPEN_PER_WINDOW"
    assert len(document["selection"]["run_policies"]) == 2
    binding = document["bindings"][0]
    assert binding["exit_binding_sha256"] != ZERO and len(binding["exit_binding_sha256"]) == 64
    assert binding["strategy"] and binding["exit_binding"]
    # The legacy T7 manifest itself is a listed original attachment (F6) and
    # the new legal recipe copy equals the frozen POSITIVE_RECIPE.
    table = {entry["path"] for entry in document["files"]}
    assert f"{OLD_DIR_REL}/acceptance-input-manifest.json" in table
    assert a.ACCEPTANCE_INPUT_MANIFEST_PATH not in table
    recipe = s.parse_json_bytes(
        (prepared_tree / NEW_DIR_REL / "recipe.json").read_bytes())
    assert recipe == t7_support.POSITIVE_RECIPE
    with pytest.raises(json.JSONDecodeError):
        json.loads((prepared_tree / OLD_DIR_REL / "recipe.json").read_bytes())
    # Plans decode through the production codec to exactly seven records.
    plans = s.decode_payoff_training_plan_file(
        (prepared_tree / NEW_DIR_REL / "payoff-training-plans.json").read_bytes())
    assert len(plans) == 7


def test_legacy_manifest_entry_is_mandatory(prepared_tree: Path, tmp_path: Path) -> None:
    # F6 on the real F: dropping the legacy manifest from the table fails the
    # dual-directory completeness check even though every other attachment is
    # intact.
    tree = tmp_path / "copy"
    shutil.copytree(prepared_tree, tree)
    path = tree / a.ACCEPTANCE_INPUT_MANIFEST_PATH
    document = s.parse_json_bytes(path.read_bytes())
    document["files"].remove(next(entry for entry in document["files"]
                                  if entry["path"] == f"{OLD_DIR_REL}/acceptance-input-manifest.json"))
    path.write_bytes(s.canonical_json_bytes(document))
    with pytest.raises(a.ArtifactsError, match="unlisted_fixture_file"):
        a.verify_acceptance_input_manifest(tree)


def test_preparation_refusals_and_atomicity(tmp_path: Path) -> None:
    tree = _build_real_tree(tmp_path / "tree")
    # An existing target directory is refused before anything is staged.
    (tree / NEW_DIR_REL).mkdir(parents=True)
    result = _run_script(tree)
    assert result.returncode == 2
    assert "target_directory_already_exists" in result.stdout
    shutil.rmtree(tree / NEW_DIR_REL)
    # A tampered frozen input aborts with a pin mismatch, publishes nothing
    # and leaves no staging residue.
    tampered = _build_real_tree(tmp_path / "tampered")
    (tampered / OLD_DIR_REL / "partition-params.json").write_bytes(b"{}\n")
    result = _run_script(tampered)
    assert result.returncode == 3
    assert "pin_mismatch" in result.stdout
    assert not (tampered / NEW_DIR_REL).exists()
    assert not (tampered / "tests/fixtures/v2/.ep1_t7_contract.staging").exists()
    # No undocumented manifest-path/input-list/force options exist.
    import os
    forced = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", str(tree / "scripts/prepare_t7_contract_inputs.py"),
         "--project-root", str(tree), "--manifest", "x.json"],
        capture_output=True, text=True, check=False,
        env={key: value for key, value in os.environ.items()
             if key not in ("PYTHONPATH", "PYTHONHOME")})
    assert forced.returncode == 2
    assert "unrecognized arguments" in forced.stderr


def test_real_preparation_never_enters_forbidden_phases(
        tmp_path: Path) -> None:
    # F5 runtime proof on the real packaging path: model fitting, the outer
    # stand-in, the cold-chain smoke, the validation executor, replay and the
    # legacy freeze are replaced with traps; the genuine packaging must still
    # succeed without touching any of them.
    tree = _build_real_tree(tmp_path / "tree")
    wrapper = tmp_path / "guarded_prepare.py"
    wrapper.write_text(
        "import runpy, sys\n"
        "from pathlib import Path\n"
        "root = Path(sys.argv[1])\n"
        "sys.path[:0] = [str(root / 'src'), str(root / 'tests')]\n"
        "from ak_trading.v2.probability import models as _models\n"
        "from ak_trading.v2.validation import experiments as _experiments\n"
        "from ak_trading.v2.decision import replay as _replay\n"
        "import v2_ep1_t7_support as _support\n"
        "def _forbidden(*args, **kwargs):\n"
        "    raise AssertionError('forbidden_phase_entered')\n"
        "_models.fit_forecast_models = _forbidden\n"
        "_experiments._execute_validation = _forbidden\n"
        "_replay.replay_payoff_training_window = _forbidden\n"
        "_support.fit_outer_stand_in = _forbidden\n"
        "_support.execute_positive_smoke = _forbidden\n"
        "_support.freeze_acceptance_manifest = _forbidden\n"
        "sys.argv = ['prepare_t7_contract_inputs.py', '--project-root', str(root)]\n"
        "runpy.run_path(str(root / 'scripts/prepare_t7_contract_inputs.py'),\n"
        "               run_name='__main__')\n",
        encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", str(wrapper), str(tree)],
        capture_output=True, text=True, cwd=str(tree), check=False,
        env={key: value for key, value in __import__("os").environ.items()
             if key not in ("PYTHONPATH", "PYTHONHOME")})
    assert result.returncode == 0, result.stderr + result.stdout
    assert a.verify_acceptance_input_manifest(tree)


def _junction_argument_order() -> bool:
    """Probe _winapi.CreateJunction's (target, link) argument order safely."""
    import tempfile
    import _winapi
    base = Path(tempfile.mkdtemp(prefix="junction-probe-"))
    real = base / "real"
    real.mkdir()
    link = base / "link"
    try:
        try:
            _winapi.CreateJunction(str(real), str(link))
        except OSError:
            return False
        return link.exists() and link.resolve() == real.resolve()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _make_junction(link: Path, target: Path) -> None:
    """Create a real Windows junction at ``link`` pointing to ``target``."""
    import _winapi
    if _junction_argument_order():
        _winapi.CreateJunction(str(target.resolve()), str(link))
    else:
        _winapi.CreateJunction(str(link), str(target.resolve()))
    if link.resolve() != target.resolve():
        raise RuntimeError("junction_creation_unverified")


def _real_symlink_available() -> bool:
    probe = Path(__import__("tempfile").mkdtemp(prefix="symlink-probe-")) / "target"
    probe.write_text("x", encoding="utf-8")
    try:
        probe.with_name("link").symlink_to(probe)
    except OSError:
        shutil.rmtree(probe.parent, ignore_errors=True)
        return False
    shutil.rmtree(probe.parent, ignore_errors=True)
    return True


def test_junction_boundary_reproduction(tmp_path: Path) -> None:
    # R6-BOUNDARY-01 reproduction with a REAL Windows junction: content-identical
    # materials behind an aliased tests/fixtures/v2 must be refused before any
    # read or write — nothing published inside the tree, outside it, and no
    # staging residue. A real symlink variant runs too when the OS allows it
    # (no skip markers: the junction path is always exercised).
    tree = _build_real_tree(tmp_path / "tree")
    outside = tmp_path / "outside" / "v2"
    outside.mkdir(parents=True)
    shutil.copytree(tree / OLD_DIR_REL, outside / "ep1_t7")
    for name in ("m5_research.json", "shared.json"):
        shutil.copyfile(tree / "tests/fixtures/v2" / name, outside / name)
    shutil.rmtree(tree / "tests/fixtures/v2")
    _make_junction(tree / "tests/fixtures/v2", outside)

    def assert_refused(label: str) -> None:
        result = _run_script(tree)
        assert result.returncode == 3, label
        assert ("path_is_reparse_point" in result.stdout
                or "path_escapes_declared_root" in result.stdout), result.stdout
        assert not (tree / NEW_DIR_REL).exists()
        assert not (outside / "ep1_t7_contract").exists()
        assert not (tree / "tests/fixtures/v2/.ep1_t7_contract.staging").exists()

    assert_refused("junction")
    if _real_symlink_available():
        import os
        # os.rmdir removes the junction reparse point itself; rmtree would
        # recurse through it into the outside tree.
        os.rmdir(tree / "tests/fixtures/v2")
        (tree / "tests/fixtures/v2").symlink_to(outside, target_is_directory=True)
        assert_refused("symlink")


def test_output_parent_and_project_root_junctions_rejected(tmp_path: Path) -> None:
    tree = _build_real_tree(tmp_path / "tree")
    outside = tmp_path / "outside" / "fixtures"
    outside.mkdir(parents=True)
    shutil.copytree(tree / "tests/fixtures/v2", outside / "v2")
    shutil.rmtree(tree / "tests/fixtures")
    _make_junction(tree / "tests/fixtures", outside)
    result = _run_script(tree)
    assert result.returncode == 3
    assert "path_is_reparse_point" in result.stdout
    assert not (outside / "v2" / "ep1_t7_contract").exists()

    real = _build_real_tree(tmp_path / "real")
    link = tmp_path / "root-link"
    _make_junction(link, real)
    run_linked = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", str(real / "scripts/prepare_t7_contract_inputs.py"),
         "--project-root", str(link)],
        capture_output=True, text=True, check=False, cwd=str(real))
    assert run_linked.returncode == 3
    assert "path_is_reparse_point:project_root" in run_linked.stdout
    assert not (real / NEW_DIR_REL).exists()


def test_preexisting_staging_refused_not_deleted(tmp_path: Path) -> None:
    tree = _build_real_tree(tmp_path / "tree")
    staging = tree / "tests/fixtures/v2/.ep1_t7_contract.staging"
    staging.mkdir()
    sentinel = staging / "unknown-file.txt"
    sentinel.write_text("operator data", encoding="utf-8")
    result = _run_script(tree)
    assert result.returncode == 3
    assert "staging_already_exists" in result.stdout
    assert staging.is_dir() and sentinel.read_text(encoding="utf-8") == "operator data"
    assert not (tree / NEW_DIR_REL).exists()


def test_writable_hardlink_alias_refused(tmp_path: Path) -> None:
    # R6-WRITABLE-ALIAS-02: a bound or walked input carrying a writable
    # hard-link alias (os.link, st_nlink > 1 — inside or outside the tree) is
    # refused before anything is published; neither the aliased material nor
    # the alias is modified. The single-link control state is asserted before
    # each alias is made (the green tests prove genuine single-link packaging).
    import os
    tree = _build_real_tree(tmp_path / "tree")
    pinned = tree / "tests/fixtures/v2/m5_research.json"
    walked = tree / "tests/fixtures/v2/shared.json"
    pinned_before = pinned.read_bytes()
    walked_before = walked.read_bytes()
    assert os.stat(pinned).st_nlink == 1 and os.stat(walked).st_nlink == 1

    outside = tmp_path / "outside"
    outside.mkdir()
    # Pinned-input alias outside the tree: refused at the pin read.
    pinned_alias = outside / "m5-research-alias.json"
    os.link(pinned, pinned_alias)
    result = _run_script(tree)
    assert result.returncode == 3
    assert "file_has_multiple_links" in result.stdout
    assert not (tree / NEW_DIR_REL).exists()
    assert pinned.read_bytes() == pinned_before
    assert pinned_alias.read_bytes() == pinned_before
    os.unlink(pinned_alias)

    # Walked (not pinned) file alias outside the tree: refused by the watch
    # walk even though every pinned input is single-link.
    walked_alias = outside / "shared-alias.json"
    os.link(walked, walked_alias)
    result = _run_script(tree)
    assert result.returncode == 3
    assert "file_has_multiple_links" in result.stdout
    assert not (tree / NEW_DIR_REL).exists()
    assert walked.read_bytes() == walked_before
    os.unlink(walked_alias)

    # In-tree alias on a pinned input: same refusal, alias left in place.
    inside_alias = tree / "tests" / "m5-research-alias.json"
    os.link(pinned, inside_alias)
    result = _run_script(tree)
    assert result.returncode == 3
    assert "file_has_multiple_links" in result.stdout
    assert not (tree / NEW_DIR_REL).exists()
    assert pinned.read_bytes() == pinned_before
    assert inside_alias.read_bytes() == pinned_before
    os.unlink(inside_alias)
    # With every alias removed the very same tree packages for real.
    green = _run_script(tree)
    assert green.returncode == 0, green.stderr + green.stdout
    assert (tree / NEW_DIR_REL / "acceptance-input-manifest.json").is_file()


def test_offline_guard_preserved_for_script_style_children() -> None:
    # R6-GUARD-STRIP-01 counterexample: a child launched exactly like the
    # preparation script keeps the offline guard installed — the guard finder
    # is on sys.meta_path, external DNS and shell subprocesses stay blocked,
    # without touching real credentials, brokers or networks.
    code = (
        "import socket, subprocess, sys\n"
        "from ak_trading.testing.offline_guard import OfflineViolation, require_installed\n"
        "require_installed()\n"
        "finders = [type(finder).__module__ for finder in sys.meta_path]\n"
        "assert 'ak_trading.testing.offline_guard' in finders, finders\n"
        "try:\n"
        "    socket.getaddrinfo('boundary-invalid.invalid.example', 80)\n"
        "except OfflineViolation:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('external DNS not blocked')\n"
        "try:\n"
        "    subprocess.Popen('echo hi', shell=True)\n"
        "except OfflineViolation:\n"
        "    pass\n"
        "else:\n"
        "    raise AssertionError('shell subprocess not blocked')\n"
        "print('guard-active')\n"
    )
    result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", code],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "guard-active" in result.stdout


def test_script_source_discipline() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("fit_forecast_models", "outer_stand_in", "execute_positive_smoke",
                      "positive_cold_run", "_execute_validation", "_stage_acceptance_world",
                      "_mirror_lock_tree", "freeze_acceptance_manifest"):
        assert forbidden not in source
    assert "--manifest" not in source and "--force" not in source and "--inputs" not in source
    # The offline guard is never stripped: the script must not touch the
    # meta-path or reference the guard finder at all.
    assert "sys.meta_path" not in source and "_GuardFinder" not in source
