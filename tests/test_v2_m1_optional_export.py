"""H3 optional-export regression: v2 export imports and lock-signs without SQLAlchemy.

Proves the three effective-plan requirements: (1) module import and lock
signature generation succeed with the optional SQLAlchemy extra absent,
(2) the canonical callable contract is unchanged (exact ``Connection`` token,
parameter kinds, no defaults, all other annotations), and (3) an unrelated
unresolved annotation is still rejected — the narrow evaluation namespace
covers only the declared export token and nothing else.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ak_trading.v2 import artifacts as a
from ak_trading.v2 import export

ROOT = Path(__file__).resolve().parents[1]

# A child with a meta-path blocker is the honest proof for environments where
# SQLAlchemy happens to be installed: the module must still import and the
# lock signature map must still build with the extra forcibly absent.
_BLOCKED_IMPORT_CHILD = '''
import importlib.abc
import json
import sys


class _Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "sqlalchemy" or fullname.startswith("sqlalchemy."):
            raise ImportError("synthetic sqlalchemy blocker")
        return None


sys.meta_path.insert(0, _Blocker())
import ak_trading.v2.export as export  # noqa: E402
from ak_trading.v2.artifacts import _public_signature_map  # noqa: E402

payload = {
    "annotation": export.export_frozen_input.__annotations__["connection"],
    "signature": _public_signature_map()["ak_trading.v2.export.export_frozen_input"],
    "persistence_absent": export.begin_read_only_snapshot is None
                          and export.select_contract_bars is None,
    "connection_residue": "Connection" in vars(export),
}
print("PAYLOAD " + json.dumps(payload))
'''


def test_import_and_lock_signature_generation_without_sqlalchemy(tmp_path):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    child = subprocess.run(
        [sys.executable, "-B", "-c", _BLOCKED_IMPORT_CHILD],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60, check=False, env=environment,
    )
    assert child.returncode == 0, child.stdout + child.stderr
    marker = next(line for line in child.stdout.splitlines() if line.startswith("PAYLOAD "))
    payload = json.loads(marker[len("PAYLOAD "):])
    assert payload["annotation"] == "Connection"
    assert payload["signature"] == (
        "export_frozen_input(connection: Connection, "
        "request: ExportRequestV2, output: Path) -> FrozenInput"
    )
    assert payload["persistence_absent"] is True
    assert payload["connection_residue"] is False


def test_canonical_export_signature_unchanged_in_this_environment():
    signature = a._public_signature_map()["ak_trading.v2.export.export_frozen_input"]
    assert signature == (
        "export_frozen_input(connection: Connection, "
        "request: ExportRequestV2, output: Path) -> FrozenInput"
    )
    assert "Connection" not in vars(export)


def test_declared_callable_contract_kinds_defaults_and_annotations_unchanged():
    parameters = inspect.signature(export.export_frozen_input, eval_str=False).parameters
    assert list(parameters) == ["connection", "request", "output"]
    declared = {"connection": "Connection", "request": "ExportRequestV2", "output": "Path"}
    for name, parameter in parameters.items():
        assert parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD, name
        assert parameter.default is inspect.Parameter.empty, name
        assert parameter.annotation == declared[name], name
    full = inspect.signature(export.export_frozen_input, eval_str=False)
    assert full.return_annotation == "FrozenInput"


def test_unrelated_unresolved_annotations_are_still_rejected():
    # Bare names under PEP 563 single-stringify exactly like export.py's
    # ``connection: Connection``; evaluation then fails on unknown names.
    def probe(value: DefinitelyNotImportedAnywhere): ...

    def foreign(connection: Connection): ...

    for function in (probe, foreign):
        with pytest.raises(a.ArtifactsError) as report:
            a.canonical_function_signature(function)
        assert "signature_evaluation_failed" in str(report.value)


def test_optional_persistence_fallback_refuses_export(monkeypatch, tmp_path):
    """The absent-extra branch keeps refusing instead of touching a driver.

    The monkeypatch reproduces exactly the fallback state the blocked-import
    child observes for real: both persistence entry points unavailable.
    """
    from ak_trading.v2 import contracts as c

    monkeypatch.setattr(export, "begin_read_only_snapshot", None)
    monkeypatch.setattr(export, "select_contract_bars", None)
    quality = c.DataQuality(status="READY", reasons=(), temporal_basis="observed",
                            input_hash=hashlib.sha256(b"optional-export-test").hexdigest())
    instrument = c.Instrument(
        contract_id="SYN.rb2609", raw_symbol="SYN.rb2609", product="SYN.rb",
        sector="synthetic", risk_groups=("SYN",), listing_nano=0, expiry_nano=None,
        last_trade_nano=None, delivery_cutoff_nano=None, quality=quality)
    request = export.ExportRequestV2(
        contract_ids=("SYN.rb2609",), raw_symbols=("SYN.rb2609",),
        start_nano=1, end_nano=2, as_of_nano=3, durations=(60, 86400),
        instruments=(instrument,), sessions=(), rules=(),
        page_size=1, maximum_rows=1, maximum_bytes=1)
    with pytest.raises(export.ExportError) as report:
        export.export_frozen_input(object(), request, tmp_path / "bundle")
    assert "persistence_futures_research_unavailable" in str(report.value)
    assert not (tmp_path / "bundle").exists()
