"""DP03 test bootstrap: assemble an importable candidate-tree package.

The DP03 source modules live in the repository at
``src/ak_trading/datapipeline/{collector,scheduler,session_factory}.py`` but
the package's frozen DP01 files (``__init__``, ``contracts``, ``ports``,
``policy``) are only staged inside the DP01 r1 freeze package
(``docs/devplan-datapipeline-20260923-r1/DP01/r1/src/...``).  Until the
candidate tree is materialized by an authorized revision, this bootstrap
assembles a hash-verified staging copy under
``.tmp/datapipeline/DP03/staging/src``:

- DP01 frozen files are copied byte-identical and verified against the
  sha256 values in ``DP01/r1/dp01-freeze-manifest.json`` (read
  programmatically - no hand-copied hashes);
- the DP01 staging scaffold ``src/ak_trading/__init__.py`` is verified
  against the manifest's ``staging_scaffold_not_in_candidate_write_set``;
- the three DP03 modules are copied from the repository source tree and the
  copy is verified byte-equal to the live files.

``ensure_importable()`` inserts the staging root at ``sys.path[0]`` and
evicts any previously imported ``ak_trading`` modules so imports resolve to
the assembled package.  The staging directory is OUTSIDE every frozen face.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Dict

FIXTURE_DIR = Path(__file__).resolve().parent
REPO_ROOT = FIXTURE_DIR.parents[3]
DP01_DIR = REPO_ROOT / "docs" / "devplan-datapipeline-20260923-r1" / "DP01" / "r1"
FREEZE_MANIFEST = DP01_DIR / "dp01-freeze-manifest.json"
STAGING_ROOT = REPO_ROOT / ".tmp" / "datapipeline" / "DP03" / "staging"
STAGING_SRC = STAGING_ROOT / "src"

#: repository (authoritative) locations of the DP03 modules
DP03_MODULES = (
    "src/ak_trading/datapipeline/collector.py",
    "src/ak_trading/datapipeline/scheduler.py",
    "src/ak_trading/datapipeline/session_factory.py",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _expected_hashes() -> Dict[str, str]:
    manifest = json.loads(FREEZE_MANIFEST.read_text(encoding="utf-8"))
    expected: Dict[str, str] = {}
    for entry in manifest.get("files", []):
        path = entry.get("path", "")
        if "/DP01/r1/src/" in path:
            expected[path.split("/DP01/r1/", 1)[1]] = entry["sha256"]
    for entry in manifest.get("staging_scaffold_not_in_candidate_write_set", []):
        path = entry.get("path", "")
        if "/DP01/r1/src/" in path:
            expected[path.split("/DP01/r1/", 1)[1]] = entry["sha256"]
    if not any(k.startswith("src/ak_trading/datapipeline/") for k in expected):
        raise RuntimeError("freeze manifest carries no DP01 src hashes; refusing to stage")
    return expected


def assemble() -> Path:
    expected = _expected_hashes()
    STAGING_SRC.mkdir(parents=True, exist_ok=True)
    copied: list = []
    for relative, sha in sorted(expected.items()):
        source = DP01_DIR / relative
        if not source.is_file():
            raise RuntimeError(f"frozen file missing: {source}")
        actual = _sha256(source)
        if actual != sha:
            raise RuntimeError(
                f"frozen file hash mismatch for {relative}: manifest {sha} vs actual {actual}"
            )
        target = STAGING_ROOT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if _sha256(target) != sha:
            raise RuntimeError(f"staged copy drifted for {relative}")
        copied.append(relative)
    for relative in DP03_MODULES:
        source = REPO_ROOT / relative
        if not source.is_file():
            raise RuntimeError(f"DP03 module missing from repository: {source}")
        target = STAGING_ROOT / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if _sha256(target) != _sha256(source):
            raise RuntimeError(f"staged copy drifted for {relative}")
        copied.append(relative)
    return STAGING_SRC


def ensure_importable() -> Path:
    """Make the assembled staging package importable.

    Idempotent within a pytest session: when ``ak_trading.datapipeline``
    already resolves into THIS staging tree (pytest imports every test module
    during collection, so the second test module must NOT evict the first
    one's classes - exception classes would diverge and ``pytest.raises``
    would stop catching), the already-imported modules stay put; only a
    ``ak_trading`` that resolves elsewhere is evicted.
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


# ---------------------------------------------------------------------------
# Self-verification hook (used by tests to prove provenance programmatically)
# ---------------------------------------------------------------------------


def staged_file_hashes() -> Dict[str, str]:
    hashes: Dict[str, str] = {}
    for relative in list(_expected_hashes()) + list(DP03_MODULES):
        hashes[relative] = _sha256(STAGING_ROOT / relative)
    return hashes


def load_module(name: str):
    """Load a fixture module by file stem (kept off sys.path for clarity)."""
    path = FIXTURE_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"dp03_fixtures_{name}", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError(f"cannot load fixture module {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
