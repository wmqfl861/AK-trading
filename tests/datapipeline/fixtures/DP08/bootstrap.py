"""DP08 integration bootstrap: assemble the full six-node package.

Every node delivered its code to a different authoritative location (DP01 /
DP02 / DP05 / DP07 staged inside their evidence packages under
``docs/devplan-datapipeline-20260923-r1/<node>/r1/**``; DP03 / DP04 / DP06
wrote directly into the repository working tree).  Until the candidate tree
is materialized (DP00 NR-05), this bootstrap assembles the INTEGRATED
package under ``.tmp/datapipeline/DP08/staging/src``:

- every file is copied from its AUTHORITATIVE location;
- every file's sha256 is verified against the OWNING node's freeze manifest,
  read programmatically (no hand-copied hashes anywhere);
- DP08 r2 pin overlay (X-08-1/2/3/5): the two owner-fixed files (DP03
  ``collector.py`` @ f5cf3d0e, DP02 ``readback.py`` @ 986edefe) carry their
  POST-FIX sha256 from the DP-X08FIX r1 fix manifest - the authoritative
  record until each owning node re-freezes its own r2 manifest.  Exactly
  those two paths are overlaid; every other r1 freeze pin below stays
  enforced verbatim (drift still refused);
- the DP01-owned package files (``__init__`` / contracts / ports / policy)
  are additionally cross-checked against the staged copies DP02 / DP05
  pinned in their manifests (byte-identical dependency pins);
- DP07's STAGING-ONLY ``datapipeline/__init__.py`` scaffold (which extends
  ``__path__`` into the DP01 evidence dir) is deliberately NOT used: in the
  integrated tree DP01's frozen package init is authoritative, exactly as
  DP07's own manifest note states ("dropped when the candidate tree is
  materialized").

The staging directory lives under the DP08 runtime whitelist
(``.tmp/datapipeline/DP08/**``) and outside every frozen face.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

FIXTURE_DIR = Path(__file__).resolve().parent
REPO_ROOT = FIXTURE_DIR.parents[3]
DEVPLAN = REPO_ROOT / "docs" / "devplan-datapipeline-20260923-r1"
EVIDENCE = REPO_ROOT / "docs" / "datapipeline-evidence-20260923-r1"
STAGING_ROOT = REPO_ROOT / ".tmp" / "datapipeline" / "DP08" / "staging"
STAGING_SRC = STAGING_ROOT / "src"

#: authoritative location + freeze manifest for every node's deliverables
NODES: Dict[str, Dict[str, Any]] = {
    "DP01": {
        "root": DEVPLAN / "DP01" / "r1",
        "manifest": "dp01-freeze-manifest.json",
    },
    "DP02": {
        "root": DEVPLAN / "DP02" / "r1",
        "manifest": "dp02-freeze-manifest.json",
    },
    "DP03": {
        "root": REPO_ROOT,
        "manifest": EVIDENCE / "DP03" / "r1" / "file-manifest.json",
    },
    "DP04": {
        "root": REPO_ROOT,
        "manifest": EVIDENCE / "DP04" / "r1" / "dp04-delivery-manifest.json",
    },
    "DP05": {
        "root": DEVPLAN / "DP05" / "r1",
        "manifest": "dp05-freeze-manifest.json",
    },
    "DP06": {
        "root": REPO_ROOT,
        "manifest": EVIDENCE / "DP06" / "r1" / "file-hashes.json",
    },
    "DP07": {
        "root": DEVPLAN / "DP07" / "r1",
        "manifest": "dp07-freeze-manifest.json",
    },
}

#: integrated-tree file selection: (node, authoritative-relative path) per
#: staged path.  Only source/config/migration files are assembled here; the
#: release verifier (scripts/verify_datapipeline_release.py) checks the FULL
#: delivered write sets (including tests/fixtures) in place.
PACKAGE_FILES: List[Tuple[str, str]] = [
    # DP01 frozen contract package (+ root staging scaffold)
    ("DP01", "src/ak_trading/__init__.py"),
    ("DP01", "src/ak_trading/datapipeline/__init__.py"),
    ("DP01", "src/ak_trading/datapipeline/contracts.py"),
    ("DP01", "src/ak_trading/datapipeline/ports.py"),
    ("DP01", "src/ak_trading/datapipeline/policy.py"),
    ("DP01", "config/datapipeline/policy.example.json"),
    ("DP01", "config/datapipeline/universe.example.json"),
    # DP02 storage + isolated migration chain
    ("DP02", "src/ak_trading/datapipeline/models.py"),
    ("DP02", "src/ak_trading/datapipeline/storage.py"),
    ("DP02", "src/ak_trading/datapipeline/migrate.py"),
    ("DP02", "src/ak_trading/datapipeline/readback.py"),
    ("DP02", "migrations/datapipeline/env.py"),
    ("DP02", "migrations/datapipeline/script.py.mako"),
    ("DP02", "migrations/datapipeline/versions/20260923_dp01.py"),
    # DP03 session collection
    ("DP03", "src/ak_trading/datapipeline/session_factory.py"),
    ("DP03", "src/ak_trading/datapipeline/scheduler.py"),
    ("DP03", "src/ak_trading/datapipeline/collector.py"),
    ("DP03", "scripts/collect_datapipeline.py"),
    # DP04 calendar evidence
    ("DP04", "src/ak_trading/datapipeline/calendar.py"),
    ("DP04", "config/datapipeline/calendar-evidence.schema.json"),
    # DP05 completeness/quality
    ("DP05", "src/ak_trading/datapipeline/validation.py"),
    ("DP05", "src/ak_trading/datapipeline/gaps.py"),
    ("DP05", "scripts/validate_datapipeline.py"),
    # DP06 monitoring/reporting/dashboard
    ("DP06", "src/ak_trading/datapipeline/monitoring.py"),
    ("DP06", "src/ak_trading/datapipeline/reporting.py"),
    ("DP06", "src/ak_trading/datapipeline/dashboard.py"),
    ("DP06", "scripts/report_datapipeline.py"),
    ("DP06", "scripts/run_datapipeline_daily.ps1"),
    ("DP06", "scripts/install_datapipeline_task.ps1"),
    # DP07 legacy remediation (staging __init__ scaffold intentionally NOT used)
    ("DP07", "src/ak_trading/datapipeline/legacy.py"),
    ("DP07", "src/ak_trading/datapipeline/rank_recovery.py"),
    ("DP07", "config/datapipeline/known-gaps.json"),
    ("DP07", "scripts/plan_rank_recovery.py"),
]

#: DP07's staging-only package init: present in its manifest write set but
#: explicitly NOT assembled (DP01's frozen init is the integrated authority).
DP07_STAGING_INIT = "src/ak_trading/datapipeline/__init__.py"

#: DP-X08FIX r1 fix manifest (authoritative post-fix pins for the two
#: owner-fixed files; X-08-1/2/3/5).  Read programmatically - no hash is
#: ever hand-copied into this bootstrap.
X08FIX_FIX_MANIFEST = EVIDENCE / "DP-X08FIX" / "r1" / "fix-manifest.json"

_X08FIX_OVERLAY_CACHE: Optional[Dict[str, str]] = None


def _x08fix_pin_overlay() -> Dict[str, str]:
    """``{path: post-fix sha256}`` for the X-08-fixed files (r2 overlay).

    The overlay wins for EXACTLY the paths the fix manifest lists and only
    where the owning node's r1 freeze manifest already pins that path - no
    path is silently added or renamed.  A missing/empty fix manifest is a
    hard error: r2 integration inputs are undefined without it.
    """
    global _X08FIX_OVERLAY_CACHE
    if _X08FIX_OVERLAY_CACHE is None:
        payload = json.loads(X08FIX_FIX_MANIFEST.read_text(encoding="utf-8"))
        overlay: Dict[str, str] = {}
        for entry in payload.get("files") or ():
            if isinstance(entry, Mapping) and "path" in entry:
                overlay[entry["path"]] = entry["sha256_after"]
        if not overlay:
            raise RuntimeError(f"no file pins in {X08FIX_FIX_MANIFEST}")
        _X08FIX_OVERLAY_CACHE = overlay
    return _X08FIX_OVERLAY_CACHE


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_path(node: str) -> Path:
    spec = NODES[node]
    manifest = spec["manifest"]
    if isinstance(manifest, str):
        return spec["root"] / manifest
    return manifest


def _load_manifest(node: str) -> Mapping[str, Any]:
    return json.loads(_manifest_path(node).read_text(encoding="utf-8"))


def _manifest_pins(node: str) -> Dict[str, str]:
    """Every ``path -> sha256`` pin the node's manifest carries (flattened).

    Only paths that live under the node's delivered write set are pinned
    (evidence-directory self files are kept too - harmless for lookups).
    Staged-evidence nodes (DP01/02/05/07) pin some paths candidate-relative
    (e.g. DP07's write_set); those are ALSO aliased under the evidence-dir
    prefix so authoritative lookups succeed either way.
    """
    manifest = _load_manifest(node)
    pins: Dict[str, str] = {}

    def _add(path: str, sha: str) -> None:
        pins[path] = sha
        if node in ("DP01", "DP02", "DP05", "DP07") and not path.startswith("docs/"):
            prefix = f"docs/devplan-datapipeline-20260923-r1/{node}/r1/"
            if not path.startswith(prefix):
                pins.setdefault(prefix + path, sha)

    for key in ("files", "write_set_files"):
        for entry in manifest.get(key) or ():
            if isinstance(entry, Mapping) and "path" in entry:
                _add(entry["path"], entry["sha256"])
    write_set = manifest.get("write_set")
    if isinstance(write_set, Mapping):
        for entries in write_set.values():
            for entry in entries or ():
                if isinstance(entry, Mapping) and "path" in entry:
                    _add(entry["path"], entry["sha256"])
    for entry in manifest.get("staging_scaffold_not_in_candidate_write_set") or ():
        if isinstance(entry, Mapping) and "path" in entry:
            _add(entry["path"], entry["sha256"])
    for entry in manifest.get("staged_dependencies_byte_identical_to_dp01") or ():
        if isinstance(entry, Mapping) and "path" in entry:
            # path is candidate-relative here; authoritative copy is DP01's
            pins.setdefault(
                f"docs/devplan-datapipeline-20260923-r1/DP01/r1/{entry['path']}",
                entry["sha256"],
            )
    # DP08 r2 X-08FIX overlay: the owner-fixed files pin to their POST-FIX
    # sha256 (fix-manifest.json) instead of the owning node's r1 freeze
    # value.  Only already-pinned paths of THIS node are overridden.
    for overlay_path, overlay_sha in _x08fix_pin_overlay().items():
        if overlay_path in pins:
            pins[overlay_path] = overlay_sha
    return pins


def _authoritative_path(node: str, relative: str) -> Tuple[Path, str]:
    """Resolve a delivered file path to its authoritative on-disk location.

    Returns ``(path, manifest_key)`` where manifest_key is the path exactly
    as pinned in the node's freeze manifest.
    """
    spec = NODES[node]
    root: Path = spec["root"]
    if node in ("DP01", "DP02", "DP05", "DP07"):
        prefix = f"docs/devplan-datapipeline-20260923-r1/{node}/r1/"
        return root / relative, prefix + relative
    if node in ("DP03", "DP04", "DP06"):
        return root / relative, relative
    raise KeyError(node)


def expected_hashes() -> Dict[str, Dict[str, str]]:
    """``{staged_relative: {node, manifest_key, sha256}}`` for the package."""
    out: Dict[str, Dict[str, str]] = {}
    for node, relative in PACKAGE_FILES:
        _, manifest_key = _authoritative_path(node, relative)
        pins = _manifest_pins(node)
        if manifest_key not in pins:
            raise RuntimeError(
                f"{node} freeze manifest does not pin {manifest_key}; refusing to stage"
            )
        out[relative] = {
            "node": node,
            "manifest_key": manifest_key,
            "sha256": pins[manifest_key],
        }
    if len(out) != len(PACKAGE_FILES):
        raise RuntimeError("duplicate staged paths in PACKAGE_FILES")
    return out


def assemble(force: bool = False) -> Path:
    """Hash-verify and copy every integrated file into the staging tree."""
    pins = expected_hashes()
    if force and STAGING_SRC.exists():
        shutil.rmtree(STAGING_ROOT)
    STAGING_SRC.mkdir(parents=True, exist_ok=True)
    for relative, pin in sorted(pins.items()):
        node = pin["node"]
        source, _ = _authoritative_path(node, relative)
        if not source.is_file():
            raise RuntimeError(f"{node} delivered file missing: {source}")
        actual = _sha256(source)
        if actual != pin["sha256"]:
            raise RuntimeError(
                f"{node} file {relative} DRIFTED: manifest {pin['sha256']} vs "
                f"actual {actual} (integration inputs are BLOCKED)"
            )
        target = STAGING_ROOT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if _sha256(target) != pin["sha256"]:
            raise RuntimeError(f"staged copy drifted for {relative}")
    return STAGING_SRC


def ensure_importable() -> Path:
    """Make the assembled integrated package importable.

    Idempotent within a pytest session: when ``ak_trading.datapipeline``
    already resolves into THIS staging tree the imported modules stay put
    (evicting them would make exception classes diverge across test
    modules); an ``ak_trading`` resolving elsewhere is evicted first.
    """
    src_root = assemble()
    current = sys.modules.get("ak_trading.datapipeline.contracts")
    if current is not None and hasattr(current, "__file__") and current.__file__:
        current_dir = Path(current.__file__).resolve().parent
        if current_dir.is_relative_to(STAGING_SRC.resolve()):
            if str(src_root) not in sys.path:
                sys.path.insert(0, str(src_root))
            return src_root
    for name in [m for m in list(sys.modules) if m == "ak_trading" or m.startswith("ak_trading.")]:
        del sys.modules[name]
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))
    return src_root


def staged_file_hashes() -> Dict[str, Dict[str, str]]:
    """Provenance map for tests/evidence: staged file -> (node, sha256)."""
    pins = expected_hashes()
    return {
        relative: {
            "node": pin["node"],
            "sha256": _sha256(STAGING_ROOT / relative),
        }
        for relative, pin in sorted(pins.items())
    }


# ---------------------------------------------------------------------------
# DP02's pinned acquisition loader (read-only reuse, hash-verified)
# ---------------------------------------------------------------------------

_PINNED_ACQUISITION_REL = (
    "docs/devplan-datapipeline-20260923-r1/DP02/r1/tests/datapipeline/fixtures/"
    "DP02/pinned_acquisition.py"
)


def pinned_acquisition():
    """Load the DP00-pinned acquisition module through DP02's own helper.

    The helper file is reused READ-ONLY from DP02's frozen evidence package
    (its sha256 is re-verified against DP02's freeze manifest here); it
    re-reads the DP00 baseline manifest programmatically and refuses on any
    drift before loading ``src/ak_trading/acquisition`` under an isolated
    package name.
    """
    pins = _manifest_pins("DP02")
    key = "docs/devplan-datapipeline-20260923-r1/DP02/r1/tests/datapipeline/fixtures/DP02/pinned_acquisition.py"
    if key not in pins:
        raise RuntimeError("DP02 freeze manifest does not pin pinned_acquisition.py")
    path = REPO_ROOT / _PINNED_ACQUISITION_REL
    actual = _sha256(path)
    if actual != pins[key]:
        raise RuntimeError(
            f"pinned_acquisition.py drifted (manifest {pins[key]} vs actual {actual})"
        )
    spec = importlib.util.spec_from_file_location("dp08_pinned_acquisition_helper", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError("cannot load pinned_acquisition helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_pinned_acquisition()


def load_module(name: str):
    """Load a DP08 fixture module by file stem (kept off sys.path)."""
    path = FIXTURE_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"dp08_fixtures_{name}", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError(f"cannot load fixture module {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
