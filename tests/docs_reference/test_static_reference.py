"""NF10 static API reference tests (tests/docs_reference island).

Every test drives the real CLI (``scripts/generate_api_reference.py``) via
subprocess. Fixture modules are never imported; the generator must parse them
statically. These tests cover the NF10 work-order acceptance items:

- signature/alias/docstring/field spot-checks against the real frozen surface
- every first-batch allowlist symbol registered exactly once; no dangling links
- determinism of pages/inventory with receipts kept separate
- drift check distinguishes breaking signature changes from doc-only updates
  and refuses same-version proof on input-hash drift
- safety counterexamples: top-level sentinel code never executed, side-effect
  defaults recorded as text, dynamic __all__, cross-overlay duplicate module,
  missing symbol, hash mismatch, path escape, broken alias / cross-allowlist
  re-export / alias cycle
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = REPO_ROOT / "scripts" / "generate_api_reference.py"
REAL_SURFACE = REPO_ROOT / "docs" / "api-reference-config" / "surface.json"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

V2_CONSTANTS = [
    "CONTRACT_LOCK_SCHEMA_VERSION",
    "CONTRACT_VERSION",
    "DOCUMENT_VERSION",
    "INPUT_SCHEMA_VERSION",
    "M1_NODE_VERSION",
    "SHARED_FIXTURE_SCHEMA_VERSION",
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def run_cli(*argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", str(GENERATOR), *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd or REPO_ROOT),
    )


def build(output_root: Path, build_id: str, surface: Path) -> subprocess.CompletedProcess:
    return run_cli(
        "build", "--surface", str(surface), "--output-root", str(output_root), "--build-id", build_id
    )


def check(surface: Path, baseline: Path, report: Path | None = None) -> subprocess.CompletedProcess:
    argv = ["check", "--surface", str(surface), "--baseline", str(baseline)]
    if report is not None:
        argv += ["--report", str(report)]
    return run_cli(*argv)


def copy_fixture(rel: str, dest_dir: Path) -> str:
    """Copy fixtures/<rel> to <dest_dir>/<rel>; return the workspace-relative path."""
    source = FIXTURES / rel
    dest = dest_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    return rel


def make_entry(workspace: Path, module: str, rel_path: str, **overrides) -> dict:
    """Build a surface entry pinning the bytes of <workspace>/<rel_path>.

    The surface's source_root is the workspace itself, so source_path values
    must be workspace-relative (e.g. ``src/drift_mod.py``).
    """
    data = (workspace / rel_path).read_bytes()
    entry = {
        "module": module,
        "source_path": rel_path,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "stability": "MODULE_API",
        "export_mode": "explicit_symbols",
        "symbols": [],
        "owner": "nf10-tests",
        "contract_ref": "tests/docs_reference/test_static_reference.py",
    }
    entry.update(overrides)
    return entry


def make_surface(tmp_path: Path, entries: list[dict], pending: list[dict] | None = None) -> Path:
    surface = {
        "surface_id": "nf10-test-surface",
        "source_root": ".",
        "generator_min_version": "1.0.0",
        "entries": entries,
        "pending": pending or [],
    }
    path = tmp_path / "surface.json"
    path.write_text(json.dumps(surface, indent=2), encoding="utf-8")
    return path


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    return tmp_path


@pytest.fixture(scope="class")
def real_build(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("nf10-real")
    proc = build(root, "real1", REAL_SURFACE)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return root / "real1"


# ---------------------------------------------------------------------------
# real-surface build: coverage, spot checks, links, determinism
# ---------------------------------------------------------------------------


class TestRealSurface:
    def test_build_ok_and_pending_ledger(self, real_build: Path) -> None:
        """Post-r3-flip state (2026-09-24): the 7 PENDING_SOURCE_HANDOFF items
        were consumed from the DP release manifest (W03 consumption package),
        so the build carries no pending ledger, the index has no pending
        table, and surface.json records the consumed ledger with one
        cleared_by row per covering DP module (11 rows / 11 modules)."""
        import re

        report = json.loads((real_build / "build-report.json").read_text(encoding="utf-8"))
        assert report["totals"]["entries"] == 9
        assert report["totals"]["first_batch_scope_complete"] is True
        assert report["totals"]["pending_items"] == 0
        assert report["pending"] == []
        index = (real_build / "index.md").read_text(encoding="utf-8")
        assert "Pending coverage (no pages generated)" not in index
        assert "PENDING_SOURCE_HANDOFF" not in index

        surface = json.loads(REAL_SURFACE.read_text(encoding="utf-8"))
        assert surface["pending"] == []
        consumed = surface["consumed"]
        assert consumed["state"] == "CONSUMED_FROM_RELEASE_MANIFEST"
        assert consumed["provenance"] == "RELEASE_MANIFEST"
        assert consumed["release_id"] == (
            "dp08-r3-release-manifest-sha256-"
            "943dbe26e3b80fe9ea46d916c2b6e6efef0940297f91c9845974cb5307b80980"
        )
        assert consumed["w03_build_id"] == "2026-09-24-w03-dp-consumption-r1"
        assert consumed["w02_intake"]["status"] == "FINALIZED_ACCEPTED"

        items = consumed["items"]
        assert len(items) == 7
        former_ids = {item["id"] for item in items}
        assert len(former_ids) == 7
        assert "dp01-package-all" in former_ids
        assert "dp02-storage-readback" in former_ids
        assert all(item["former_reason"] == "PENDING_SOURCE_HANDOFF" for item in items)
        assert all(item["state"] == "COVERED_BY_RELEASE_MANIFEST" for item in items)

        rows = [row for item in items for row in item["cleared_by"]]
        assert len(rows) == 11
        assert {row["module"] for row in rows} == {
            "ak_trading.datapipeline",
            "ak_trading.datapipeline.calendar",
            "ak_trading.datapipeline.collector",
            "ak_trading.datapipeline.contracts",
            "ak_trading.datapipeline.monitoring",
            "ak_trading.datapipeline.policy",
            "ak_trading.datapipeline.ports",
            "ak_trading.datapipeline.readback",
            "ak_trading.datapipeline.reporting",
            "ak_trading.datapipeline.session_factory",
            "ak_trading.datapipeline.storage",
        }
        roles = [row["role"] for row in rows]
        assert sum(role.startswith("PUBLIC_TARGET") for role in roles) == 8
        assert sum(role.startswith("REEXPORT_SUPPORT") for role in roles) == 3
        reexport_rows = [row for row in rows if row["role"].startswith("REEXPORT_SUPPORT")]
        assert all(
            row["semantic_ack"].startswith("CONFIRMED_BY_SUCCESSOR") for row in reexport_rows
        )
        for row in rows:
            assert row["provenance"] == consumed["provenance"]
            assert row["release_id"] == consumed["release_id"]
            assert row["page"] == (
                f"reference/{consumed['w03_build_id']}/modules/{row['module']}.md"
            )
            assert re.fullmatch(r"[0-9a-f]{64}", row["source_sha256"])
            assert isinstance(row["source_bytes"], int) and row["source_bytes"] > 0
            assert isinstance(row["exports_selected"], int) and row["exports_selected"] >= 1

    def test_every_allowlist_symbol_registered_exactly_once(self, real_build: Path) -> None:
        inventory = json.loads((real_build / "inventory.json").read_text(encoding="utf-8"))
        qualified = [s["qualified_name"] for s in inventory["symbols"]]
        assert len(qualified) == len(set(qualified)), "duplicate registrations"

        surface = json.loads(REAL_SURFACE.read_text(encoding="utf-8"))
        registered = set(qualified)
        for entry in surface["entries"]:
            for name in entry.get("symbols", []):
                assert f"{entry['module']}.{name}" in registered
        for name in V2_CONSTANTS:
            assert f"ak_trading.v2.{name}" in registered

        exports = inventory["exports"]["ak_trading.infiny_bridge"]
        export_names = [e["name"] for e in exports]
        assert len(export_names) == len(set(export_names)) == 23
        # every export resolves to exactly one defining record
        assert all(e["defines"] in registered for e in exports)

    def test_signature_spot_checks_match_source(self, real_build: Path) -> None:
        inventory = json.loads((real_build / "inventory.json").read_text(encoding="utf-8"))
        by_name = {s["qualified_name"]: s for s in inventory["symbols"]}

        sig = by_name["ak_trading.v2.decision.risk.enforce_account_risk"]["signature"]
        assert sig["rendered"] == (
            "enforce_account_risk(proposals: tuple[StrategyProposal, ...], "
            "gates: tuple[GateDecision, ...], account: AccountSnapshot, "
            "instruments: tuple[Instrument, ...], states: tuple[StrategyState, ...], "
            "pending_intents: tuple[OrderIntent, ...], rules: tuple[TradingRule, ...], "
            "capacity_remaining: Mapping[str, int], policy: Mapping[str, JsonValue], *, "
            "valuations: tuple[ValuationMark, ...]) -> tuple[GateDecision, ...]"
        )

        sig = by_name["ak_trading.v2.validation.metrics.block_bootstrap"]["signature"]
        assert sig["rendered"] == "block_bootstrap(values, blocks, seed: int) -> tuple[float | None, float | None]"

        sig = by_name["ak_trading.v2.validation.reports.validate_metrics"]["signature"]
        assert sig["rendered"] == "validate_metrics(metrics: Mapping, experiment: ExperimentSpec) -> None"

        sig = by_name["ak_trading.v2.factors.compute_factor"]["signature"]
        assert sig["rendered"] == (
            "compute_factor(spec: FactorSpec, bars: tuple[MarketBar, ...], decision_nano: int, "
            "*, sessions: tuple[SessionWindow, ...]) -> FactorValue"
        )

        value = by_name["ak_trading.infiny_bridge.service.DEFAULT_PORT"]["signature"]
        assert value["value_text"] == "17777"
        value = by_name["ak_trading.infiny_bridge.service.DEFAULT_HOST"]["signature"]
        assert value["value_text"] == '"127.0.0.1"'
        value = by_name["ak_trading.v2.CONTRACT_VERSION"]["signature"]
        assert value["value_text"] == '"ak_probability_strategy_contract_v2.1.3"'

    def test_aliases_link_no_duplicates(self, real_build: Path) -> None:
        inventory = json.loads((real_build / "inventory.json").read_text(encoding="utf-8"))
        by_name = {s["qualified_name"]: s for s in inventory["symbols"]}
        expected_aliases = {
            "ak_trading.infiny_bridge.protocol.normalize_request": "ak_trading.infiny_bridge.protocol.normalize_payload",
            "ak_trading.infiny_bridge.protocol.parse_payload": "ak_trading.infiny_bridge.protocol.parse_json_body",
            "ak_trading.infiny_bridge.service.ObservationBridge": "ak_trading.infiny_bridge.service.BridgeService",
        }
        for alias_qn, target_qn in expected_aliases.items():
            record = by_name[alias_qn]
            assert record["kind"] == "alias"
            assert record["alias_of"] == target_qn
            assert record["signature"] is None, "aliases must not duplicate the implementation"
            assert by_name[target_qn]["kind"] in {"function", "class"}
        # the defining symbols stay unique implementations
        assert by_name["ak_trading.infiny_bridge.protocol.normalize_payload"]["kind"] == "function"
        page = (real_build / "modules" / "ak_trading.infiny_bridge.protocol.md").read_text(encoding="utf-8")
        assert "Alias of" in page

    def test_docstring_and_field_quality(self, real_build: Path) -> None:
        inventory = json.loads((real_build / "inventory.json").read_text(encoding="utf-8"))
        by_name = {s["qualified_name"]: s for s in inventory["symbols"]}
        replay = by_name["ak_trading.v2.decision.replay.ReplayResult"]
        assert replay["documented"] is False
        assert replay["signature"]["dataclass"] is True
        assert [f["name"] for f in replay["signature"]["fields"]] == [
            "features", "forecasts", "regimes", "proposals", "decisions", "intents",
            "fills", "equity", "checkpoints", "execution_results", "reconciliation", "status",
        ]
        normalized = by_name["ak_trading.infiny_bridge.protocol.NormalizedRequest"]
        assert [f["name"] for f in normalized["signature"]["fields"]][-1] == "blocked_reason"
        assert normalized["signature"]["fields"][-1]["default"] == "None"
        page = (real_build / "modules" / "ak_trading.v2.decision.replay.md").read_text(encoding="utf-8")
        assert "**UNDOCUMENTED**" in page
        assert "Declared members only" in page
        assert "`MODULE_API`: curated per the NF10 work order" in (
            real_build / "modules" / "ak_trading.v2.decision.replay.md"
        ).read_text(encoding="utf-8")

    def test_no_dangling_links(self, real_build: Path) -> None:
        pages = {}
        for path in real_build.rglob("*.md"):
            pages[str(path.relative_to(real_build)).replace("\\", "/")] = path.read_text(encoding="utf-8")
        import re

        slugs: dict[str, set[str]] = {}
        for name, text in pages.items():
            found = set()
            for line in text.splitlines():
                if line.startswith("#"):
                    heading = line.lstrip("#").strip().replace("`", "").lower()
                    slug = re.sub(r"[^a-z0-9_\-]+", "", heading.replace(".", "-").replace(" ", "-"))
                    found.add(slug)
            slugs[name] = found
        dangling = []
        for name, text in pages.items():
            for target, anchor in re.findall(r"\]\(([^)#\s]+\.md)(#[a-z0-9_\-]+)?\)", text):
                key = target
                if key not in pages:
                    dangling.append(f"{name} -> {target}")
                    continue
                if anchor and anchor[1:] not in slugs[key]:
                    dangling.append(f"{name} -> {target}{anchor}")
        assert not dangling
        report = json.loads((real_build / "build-report.json").read_text(encoding="utf-8"))
        assert report["link_check"]["dangling"] == []
        assert report["link_check"]["links_checked"] > 0

    def test_determinism_across_build_ids(self, real_build: Path, tmp_path: Path) -> None:
        proc = build(tmp_path, "second", REAL_SURFACE)
        assert proc.returncode == 0
        second = tmp_path / "second"
        for rel in ["inventory.json", "build-report.json", "index.md"] + [
            str(p.relative_to(real_build)).replace("\\", "/")
            for p in (real_build / "modules").glob("*.md")
        ]:
            assert (real_build / rel).read_bytes() == (second / rel).read_bytes(), rel
        receipt_a = json.loads((real_build / "receipt.json").read_text(encoding="utf-8"))
        receipt_b = json.loads((second / "receipt.json").read_text(encoding="utf-8"))
        assert receipt_a["build_id"] != receipt_b["build_id"]
        assert receipt_a["created_utc"] != receipt_b["created_utc"]

    def test_existing_build_id_refused(self, tmp_path: Path) -> None:
        assert build(tmp_path, "dup", REAL_SURFACE).returncode == 0
        proc = build(tmp_path, "dup", REAL_SURFACE)
        assert proc.returncode == 2
        payload = json.loads(proc.stdout)
        assert payload["code"] == "build_id_exists"
        assert (tmp_path / "dup" / "inventory.json").is_file()

    def test_check_roundtrip_identical(self, real_build: Path, tmp_path: Path) -> None:
        proc = check(REAL_SURFACE, real_build / "inventory.json", report=tmp_path / "rt.json")
        assert proc.returncode == 0
        report = json.loads(proc.stdout)
        assert report["same_version_proof"] is True
        assert report["summary"] == {
            "breaking": 0,
            "non_breaking": 0,
            "doc_only": 0,
            "removed": 0,
            "input_hash_drift": 0,
        }


# ---------------------------------------------------------------------------
# safety counterexamples (work-order "必要反例")
# ---------------------------------------------------------------------------


class TestSafetyCounterexamples:
    def test_top_level_dangerous_code_not_executed(self, workspace: Path) -> None:
        copy_fixture("dangerous_top_level.py", workspace / "src")
        surface = make_surface(
            workspace,
            [make_entry(workspace, "dangerous", "src/dangerous_top_level.py", symbols=["guarded_api"])],
        )
        proc = build(workspace, "b1", surface)
        assert proc.returncode == 0, proc.stdout
        assert not (workspace / "src" / "dangerous_top_level.py.EXECUTED").exists(), (
            "the build must never execute documented code"
        )

    def test_side_effect_defaults_kept_as_text(self, workspace: Path) -> None:
        copy_fixture("side_effect_defaults.py", workspace / "src")
        entry = make_entry(
            workspace, "side_effect", "src/side_effect_defaults.py", symbols=["api"]
        )
        surface = make_surface(workspace, [entry])
        proc = build(workspace, "b1", surface)
        assert proc.returncode == 0, proc.stdout
        inventory = json.loads((workspace / "b1" / "inventory.json").read_text(encoding="utf-8"))
        params = inventory["symbols"][0]["signature"]["parameters"]
        assert params[0]["name"] == "value"
        assert params[0]["default"] == "boom() or 7"
        assert params[1]["default"] == 'boom() or "t"'
        # the return annotation is a string literal in the source; its exact
        # text (quotes included) must be preserved without evaluation
        assert inventory["symbols"][0]["signature"]["returns"] == '"boom() and 0"'

    def test_dynamic_all_rejected(self, workspace: Path) -> None:
        copy_fixture("dynamic_all/__init__.py", workspace / "src")
        entry = make_entry(
            workspace, "dynamic_all", "src/dynamic_all/__init__.py",
            export_mode="package_all", symbols=[],
        )
        surface = make_surface(workspace, [entry])
        proc = build(workspace, "b1", surface)
        assert proc.returncode == 2
        payload = json.loads(proc.stdout)
        assert payload["code"] == "dynamic_all_unsupported"

    def test_cross_overlay_duplicate_module_rejected(self, workspace: Path) -> None:
        copy_fixture("overlay_a.py", workspace / "src")
        copy_fixture("overlay_b.py", workspace / "src")
        surface = make_surface(
            workspace,
            [
                make_entry(workspace, "conflict.mod", "src/overlay_a.py", symbols=["which"]),
                make_entry(workspace, "conflict.mod", "src/overlay_b.py", symbols=["which"]),
            ],
        )
        proc = build(workspace, "b1", surface)
        assert proc.returncode == 2
        payload = json.loads(proc.stdout)
        assert payload["code"] == "module_duplicate"
        assert payload["details"]["module"] == "conflict.mod"

    def test_missing_symbol_rejected_with_present_names(self, workspace: Path) -> None:
        copy_fixture("missing_symbol.py", workspace / "src")
        entry = make_entry(
            workspace, "missing", "src/missing_symbol.py", symbols=["alpha", "gamma"]
        )
        surface = make_surface(workspace, [entry])
        proc = build(workspace, "b1", surface)
        assert proc.returncode == 2
        payload = json.loads(proc.stdout)
        assert payload["code"] == "symbol_missing"
        assert payload["details"]["missing_symbol"] == "gamma"
        assert set(payload["details"]["top_level_names_present"]) >= {"alpha", "beta"}

    def test_input_hash_mismatch_rejected(self, workspace: Path) -> None:
        copy_fixture("pkg_safe/impl.py", workspace / "src")
        entry = make_entry(
            workspace, "pkg.impl", "src/pkg_safe/impl.py",
            symbols=["PUBLIC_CONST", "alias_fn", "public_fn"],
        )
        tampered = entry["sha256"][:8] + ("0" if entry["sha256"][8] != "0" else "1") + entry["sha256"][9:]
        entry["sha256"] = tampered
        surface = make_surface(workspace, [entry])
        proc = build(workspace, "b1", surface)
        assert proc.returncode == 2
        payload = json.loads(proc.stdout)
        assert payload["code"] == "input_hash_mismatch"
        assert payload["details"]["module"] == "pkg.impl"

    @pytest.mark.parametrize(
        "bad_path",
        ["../escape.py", "a/../../escape.py", "/abs/path.py", "C:/abs/path.py", "back\\slash.py"],
    )
    def test_path_escape_rejected(self, workspace: Path, bad_path: str) -> None:
        entry = {
            "module": "mod",
            "source_path": bad_path,
            "sha256": "0" * 64,
            "stability": "MODULE_API",
            "export_mode": "explicit_symbols",
            "symbols": ["x"],
        }
        surface = make_surface(workspace, [entry])
        proc = build(workspace, "b1", surface)
        assert proc.returncode == 2
        payload = json.loads(proc.stdout)
        assert payload["code"] in {"path_escape", "path_invalid"}, payload

    def test_broken_reexport_cross_allowlist_rejected(self, workspace: Path) -> None:
        copy_fixture("broken_pkg/__init__.py", workspace / "src")
        copy_fixture("broken_pkg/unlisted.py", workspace / "src")
        entry = make_entry(
            workspace, "broken_pkg", "src/broken_pkg/__init__.py",
            export_mode="package_all", symbols=[],
        )
        surface = make_surface(workspace, [entry])
        proc = build(workspace, "b1", surface)
        assert proc.returncode == 2
        payload = json.loads(proc.stdout)
        assert payload["code"] == "cross_allowlist_import"
        assert payload["details"]["origin_module"] == "broken_pkg.unlisted"

    def test_alias_cycle_rejected(self, workspace: Path) -> None:
        copy_fixture("cycle.py", workspace / "src")
        entry = make_entry(
            workspace, "cycle", "src/cycle.py", export_mode="module_all", symbols=[]
        )
        surface = make_surface(workspace, [entry])
        proc = build(workspace, "b1", surface)
        assert proc.returncode == 2
        payload = json.loads(proc.stdout)
        assert payload["code"] == "alias_cycle"

    def test_alias_outside_allowlist_rejected(self, workspace: Path) -> None:
        content = (
            '"""Alias whose target is not selected."""\n\n'
            "def real() -> None:\n    \"\"\"Doc.\"\"\"\n\n\n"
            "short = real\n\n__all__ = [\"short\"]\n"
        )
        (workspace / "src").mkdir(exist_ok=True)
        (workspace / "src" / "partial_alias.py").write_text(content, encoding="utf-8")
        entry = make_entry(workspace, "partial", "src/partial_alias.py", export_mode="module_all", symbols=[])
        surface = make_surface(workspace, [entry])
        proc = build(workspace, "b1", surface)
        assert proc.returncode == 2
        payload = json.loads(proc.stdout)
        assert payload["code"] == "alias_outside_allowlist"


# ---------------------------------------------------------------------------
# happy-path fixture build (alias resolution end-to-end)
# ---------------------------------------------------------------------------


class TestFixtureHappyPath:
    def test_package_all_with_alias_and_reexport(self, workspace: Path) -> None:
        copy_fixture("pkg_safe/__init__.py", workspace / "src")
        copy_fixture("pkg_safe/impl.py", workspace / "src")
        surface = make_surface(
            workspace,
            [
                make_entry(
                    workspace, "pkg.impl", "src/pkg_safe/impl.py",
                    stability="PACKAGE_EXPORT",
                    symbols=["PUBLIC_CONST", "alias_fn", "public_fn"],
                ),
                make_entry(
                    workspace, "pkg", "src/pkg_safe/__init__.py",
                    stability="PACKAGE_EXPORT", export_mode="package_all", symbols=[],
                ),
            ],
        )
        proc = build(workspace, "b1", surface)
        assert proc.returncode == 0, proc.stdout
        inventory = json.loads((workspace / "b1" / "inventory.json").read_text(encoding="utf-8"))
        by_name = {s["qualified_name"]: s for s in inventory["symbols"]}
        assert by_name["pkg.impl.alias_fn"]["alias_of"] == "pkg.impl.public_fn"
        exports = {e["name"]: e for e in inventory["exports"]["pkg"]}
        assert exports["alias_fn"]["defines"] == "pkg.impl.alias_fn"
        assert exports["public_fn"]["defines"] == "pkg.impl.public_fn"
        assert exports["PUBLIC_CONST"]["defines"] == "pkg.impl.PUBLIC_CONST"
        assert by_name["pkg.impl.PUBLIC_CONST"]["signature"]["value_text"] == "41 + 1"
        # 3 defining/alias records in impl, none duplicated in the package
        assert all(r["module"] == "pkg.impl" for r in inventory["symbols"])


# ---------------------------------------------------------------------------
# drift checks (H10.6)
# ---------------------------------------------------------------------------


DRIFT_BASE = (FIXTURES / "drift_base.py").read_text(encoding="utf-8")
BREAKING = DRIFT_BASE.replace(
    "def public_api(value: int, factor: int = 2) -> int:",
    "def public_api(value: int, factor: int = 3, *, scale: int = 1) -> int:",
)
DOC_ONLY = DRIFT_BASE.replace('"""Original docstring."""', '"""Updated docstring."""')
HELPER_EDIT = DRIFT_BASE.replace("return value + 1", "return value + 2")
REMOVED = DRIFT_BASE.replace(
    'def public_api(value: int, factor: int = 2) -> int:\n    """Original docstring."""\n    return value * factor\n\n\n',
    "",
)


def drift_workspace(
    tmp_path: Path,
    new_text: str,
    symbols: list[str],
    new_symbols: list[str] | None = None,
) -> tuple[Path, Path]:
    src = tmp_path / "src"
    src.mkdir()
    (src / "drift_mod.py").write_text(DRIFT_BASE, encoding="utf-8", newline="\n")
    old_surface = make_surface(
        tmp_path, [make_entry(tmp_path, "drift.mod", "src/drift_mod.py", symbols=symbols)]
    )
    proc = build(tmp_path, "base", old_surface)
    assert proc.returncode == 0, proc.stdout
    baseline = tmp_path / "base" / "inventory.json"

    (src / "drift_mod.py").write_text(new_text, encoding="utf-8", newline="\n")
    new_surface = make_surface(
        tmp_path,
        [
            make_entry(
                tmp_path, "drift.mod", "src/drift_mod.py",
                symbols=new_symbols if new_symbols is not None else symbols,
            )
        ],
    )
    return new_surface, baseline


class TestDriftCheck:
    def test_breaking_signature_change(self, tmp_path: Path) -> None:
        surface, baseline = drift_workspace(
            tmp_path, BREAKING, symbols=["Kit", "public_api"]
        )
        proc = check(surface, baseline)
        assert proc.returncode == 1
        report = json.loads(proc.stdout)
        changed = {c["qualified_name"]: c for c in report["symbols_changed"]}
        entry = changed["drift.mod.public_api"]
        assert entry["classification"] == "BREAKING"
        fields = {c["field"] for c in entry["changes"]}
        assert "parameters" in fields
        assert "defaults" in fields
        assert report["same_version_proof"] is False
        assert report["summary"]["breaking"] >= 1

    def test_doc_only_change(self, tmp_path: Path) -> None:
        surface, baseline = drift_workspace(tmp_path, DOC_ONLY, symbols=["Kit", "public_api"])
        proc = check(surface, baseline)
        assert proc.returncode == 1
        report = json.loads(proc.stdout)
        changed = {c["qualified_name"]: c for c in report["symbols_changed"]}
        entry = changed["drift.mod.public_api"]
        assert entry["classification"] == "DOC_ONLY"
        assert {c["field"] for c in entry["changes"]} == {"docstring"}
        assert report["summary"]["doc_only"] == 1

    def test_untracked_body_edit_refuses_same_version_proof(self, tmp_path: Path) -> None:
        surface, baseline = drift_workspace(tmp_path, HELPER_EDIT, symbols=["Kit", "public_api"])
        proc = check(surface, baseline)
        assert proc.returncode == 1
        report = json.loads(proc.stdout)
        assert report["symbols_changed"] == []
        assert report["symbols_removed"] == []
        assert len(report["inputs_changed"]) == 1
        assert report["inputs_changed"][0]["module"] == "drift.mod"
        assert report["same_version_proof"] is False

    def test_removed_symbol(self, tmp_path: Path) -> None:
        # removing a symbol is an allowlist correction: the new surface drops
        # it, and the drift report shows it as removed versus the baseline
        surface, baseline = drift_workspace(
            tmp_path, REMOVED, symbols=["Kit", "public_api"], new_symbols=["Kit"]
        )
        proc = check(surface, baseline)
        assert proc.returncode == 1
        report = json.loads(proc.stdout)
        removed = [item["qualified_name"] for item in report["symbols_removed"]]
        assert "drift.mod.public_api" in removed
        assert report["same_version_proof"] is False

    def test_identical_inputs_no_drift(self, tmp_path: Path) -> None:
        surface, baseline = drift_workspace(tmp_path, DRIFT_BASE, symbols=["Kit", "public_api"])
        proc = check(surface, baseline)
        assert proc.returncode == 0
        report = json.loads(proc.stdout)
        assert report["same_version_proof"] is True

    def test_allowlist_growth_shows_added_symbol(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "drift_mod.py").write_text(DRIFT_BASE, encoding="utf-8", newline="\n")
        small = make_surface(
            tmp_path, [make_entry(tmp_path, "drift.mod", "src/drift_mod.py", symbols=["public_api"])]
        )
        assert build(tmp_path, "small", small).returncode == 0
        big = make_surface(
            tmp_path,
            [make_entry(tmp_path, "drift.mod", "src/drift_mod.py", symbols=["Kit", "public_api"])],
        )
        proc = check(big, tmp_path / "small" / "inventory.json")
        assert proc.returncode == 1
        report = json.loads(proc.stdout)
        added = [item["qualified_name"] for item in report["symbols_added"]]
        assert "drift.mod.Kit" in added
        assert report["summary"]["non_breaking"] >= 1
