#!/usr/bin/env python
"""DP08 r3 evidence builder - release-manifest re-pin (bookkeeping only).

ROUTING
    DP-HARD-r2 verdict PASS (docs/datapipeline-review-20260923-r1/
    DP-HARD-r2/) routes a bookkeeping item: the DP08 release manifest must
    re-pin the three DP03 r2 (F-HARD-01 repair) files to their post-repair
    sha256 with a documented overlay record sourced from
    DP03/r2/file-manifest.json; every other pin stays byte-identical (the
    DP-HARD-r2 reviewer's own replay found drift == exactly those three
    files).

WHAT THIS DOES (fully programmatic, no hand-copied hashes)
 0. preflight   verify-mode replay of the frozen r2 release manifest over
                the live tree into .tmp; expected exit 1 with drift ==
                exactly the routed triple at their r2->r3 pin transitions;
 1. build       scripts/verify_datapipeline_release.py build_manifest() is
                imported READ-ONLY and invoked exactly as in r2 (node
                freeze manifests + DP-X08FIX overlay + DP08 write set from
                the newest DP08 file manifest, r2 supersedes r1); the DP08
                r3 manifest then applies the DP03 r2 pin overlay on top:
                * an overlay path MUST already be pinned by the built
                  manifest (the overlay never adds or renames paths);
                * sha256_before_r2 MUST equal the pin being replaced (CAS);
                * the live tree MUST hash to sha256_after_r2 (bytes too);
                * the changed paths MUST be exactly the routed triple;
                * DP03 r2's unchanged paths MUST match pins AND live bytes;
 2. diff        r2 manifest vs r3 manifest: exactly 3 changed file entries,
                114 byte-identical, identical 117-path set; every non-files
                top-level delta recorded and explained;
 3. verify      the verifier re-runs in VERIFY mode against the r3 manifest
                (output into the whitelisted .tmp root, copied here
                read-only for the record) plus one independent second
                directory; both must exit 0 / CLEAN 117/117;
 4. registers   the r2 NOT_RUN register is inherited VERBATIM (r3 runs no
                new legs and changes no statuses).

WRITES
    this evidence directory (docs/datapipeline-evidence-20260923-r1/
    DP08/r3/**) and .tmp/datapipeline/DP08/r3/** plus
    .tmp/datapipeline/DP08-r3-independent-check ONLY.  No source file, no
    DP03 tree, no other frozen face is touched (DP03/r2/file-manifest.json
    is read, never written).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
VERIFIER = REPO / "scripts" / "verify_datapipeline_release.py"
EVIDENCE_REL = "docs/datapipeline-evidence-20260923-r1"
EVIDENCE = REPO / EVIDENCE_REL
R2_DIR = HERE.parent / "r2"
DP03_R2_MANIFEST_REL = f"{EVIDENCE_REL}/DP03/r2/file-manifest.json"
DP03_R2_MANIFEST = REPO / DP03_R2_MANIFEST_REL
DP08_R2_SUMMARY = R2_DIR / "dp08-summary.json"
R2_RELEASE = R2_DIR / "release-manifest.json"
R3_RELEASE = HERE / "release-manifest.json"
VERIFY_OUTPUT = REPO / ".tmp" / "datapipeline" / "DP08" / "r3" / "verification-output"
INDEP_OUTPUT = REPO / ".tmp" / "datapipeline" / "DP08-r3-independent-check"
RECHECK_OUTPUT = REPO / ".tmp" / "datapipeline" / "DP08" / "r3" / "r2-manifest-recheck"

#: the routed triple (path -> post-repair sha256).  These are ASSERTION
#: targets taken from the DP-HARD-r2 routing note; every value is checked
#: against DP03/r2/file-manifest.json and the live tree before pinning.
EXPECTED_OVERLAYS: Dict[str, str] = {
    "src/ak_trading/datapipeline/collector.py":
        "64387dda306a2f8a477c91faa24539f4eccb878cd41937a4c4e05b8634ea4e0b",
    "tests/datapipeline/fixtures/DP03/synthetic_doubles.py":
        "c9318daf65fe803c938627935a539437113573809881f8ef85132c31faf4317c",
    "tests/datapipeline/test_collector.py":
        "3ca9617b973ff4e8bedf991b3493f9f8403ecdd39262b708588b15a4745b5933",
}

R3_NOTE = (
    "DP08 r3 re-pin overlay (DP-HARD-r2 PASS routing): the three DP03 r2 "
    "F-HARD-01 repaired files (collector.py, synthetic_doubles.py, "
    "test_collector.py) pin to their POST-REPAIR sha256 read from the DP03 "
    "r2 file manifest; every other pin is byte-identical to the r2 release "
    "manifest.  r3 is bookkeeping only - no source file changed and no new "
    "test leg ran; DP08's recorded pytest result is carried over from r2 "
    "(DP08 write set unchanged)."
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dump(obj: Any, path: Path) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")


def _load_verifier() -> Any:
    spec = importlib.util.spec_from_file_location("verify_datapipeline_release", VERIFIER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _run_verifier(manifest: Path, output: Path) -> Dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-B", str(VERIFIER), "--repo", str(REPO),
         "--manifest", str(manifest), "--output", str(output)],
        cwd=REPO, capture_output=True, text=True, timeout=300,
    )
    report = json.loads((output / "verification-report.json").read_text(encoding="utf-8"))
    report["exit_code"] = proc.returncode
    report["command"] = (f"python -B scripts/verify_datapipeline_release.py "
                         f"--manifest {_rel(manifest)} --output {_rel(output)}")
    return report


def _rel(path: Path) -> str:
    return path.resolve().relative_to(REPO).as_posix()


# ---------------------------------------------------------------------------

def preflight_recheck() -> Dict[str, Any]:
    """Replay the frozen r2 manifest over the live tree; expect DRIFT ==
    exactly the routed triple (old pin -> DP03 r2 after)."""
    if RECHECK_OUTPUT.exists():
        shutil.rmtree(RECHECK_OUTPUT)
    report = _run_verifier(R2_RELEASE, RECHECK_OUTPUT)
    drift_paths = sorted(d["path"] for d in report["drift"])
    if report["exit_code"] != 1 or report["clean"]:
        raise SystemExit(f"preflight expected DRIFT exit 1, got {report['exit_code']}")
    if drift_paths != sorted(EXPECTED_OVERLAYS):
        raise SystemExit(f"preflight drift != routed triple: {drift_paths}")
    for item in report["drift"]:
        if item["actual"] != EXPECTED_OVERLAYS[item["path"]]:
            raise SystemExit(f"preflight drift actual != DP03 r2 pin: {item}")
    if report["missing"] or report["dependency_pin_mismatch"] or report["gate_errors"]:
        raise SystemExit(f"preflight unexpected blocks: {report}")
    result = {
        "purpose": "verify-mode replay of the frozen r2 release manifest over the live tree at r3 build time",
        "expected": "exit 1, drift == exactly the three DP03 r2 re-pinned files, no other block",
        "exit_code": report["exit_code"],
        "manifest_files": report["manifest_files"],
        "files_verified": report["files_verified"],
        "drift": report["drift"],
        "missing": report["missing"],
        "dependency_pin_mismatch": report["dependency_pin_mismatch"],
        "gate_errors": report["gate_errors"],
        "v15_gate": report["v15_gate"],
        "command": report["command"],
        "agrees_with": "docs/datapipeline-review-20260923-r1/DP-HARD-r2/logs/release-117-recheck.json (independent reviewer replay)",
    }
    _dump(result, HERE / "r2-manifest-recheck.json")
    return result


def build_r3_manifest() -> Dict[str, Any]:
    """Run the r2 builder (imported read-only) then apply the DP03 r2 pin
    overlay with CAS + live-tree assertions."""
    verifier = _load_verifier()
    manifest: Dict[str, Any] = verifier.build_manifest(
        REPO, dp08_summary=DP08_R2_SUMMARY)

    dp03_r2 = json.loads(DP03_R2_MANIFEST.read_text(encoding="utf-8"))
    changed = [e for e in dp03_r2["repo_write_set"] if e.get("changed_in_r2")]
    unchanged = [e for e in dp03_r2["repo_write_set"] if not e.get("changed_in_r2")]
    changed_paths = sorted(e["path"].replace("\\", "/") for e in changed)
    if changed_paths != sorted(EXPECTED_OVERLAYS):
        raise SystemExit(f"DP03 r2 changed set != routed triple: {changed_paths}")

    by_path = {f["path"]: f for f in manifest["files"]}
    overlays: Dict[str, Dict[str, Any]] = {}
    r2_manifest = json.loads(R2_RELEASE.read_text(encoding="utf-8"))
    r2_overlays = r2_manifest.get("pin_overlays", {}).get("files", {})
    for entry in changed:
        path = entry["path"].replace("\\", "/")
        if path not in by_path:
            raise SystemExit(f"DP03 r2 overlay path not pinned by the built manifest: {path}")
        pinned = by_path[path]
        # CAS: the DP03 r2 'before' must equal the pin being replaced.
        if entry["sha256_before_r2"] != pinned["sha256"]:
            raise SystemExit(
                f"CAS broken for {path}: DP03 r2 before "
                f"{entry['sha256_before_r2'][:12]} != built pin {pinned['sha256'][:12]}")
        # live tree must equal the DP03 r2 'after' (hash and bytes).
        live = REPO / path
        live_sha = _sha256(live)
        if live_sha != entry["sha256_after_r2"]:
            raise SystemExit(f"live tree != DP03 r2 after for {path}")
        if live.stat().st_size != entry["bytes"]:
            raise SystemExit(f"live byte count != DP03 r2 manifest for {path}")
        if entry["sha256_after_r2"] != EXPECTED_OVERLAYS[path]:
            raise SystemExit(f"DP03 r2 after != routed expectation for {path}")

        previous_pin_source = pinned.get("pin_source", "node-freeze-manifest (DP03 r1)")
        chain: List[Dict[str, str]] = []
        origin = r2_overlays.get(path, {}).get("sha256_before")
        chain.append({
            "sha256": origin or entry["sha256_before_r2"],
            "label": ("DP03 r1 freeze pin (deepest recorded origin)"
                      if origin else "DP03 r1 freeze pin = r2 release-manifest pin"),
        })
        chain.append({
            "sha256": entry["sha256_before_r2"],
            "label": ("DP-X08FIX r1 post-fix overlay pin = r2 release-manifest pin"
                      if previous_pin_source == "dp-x08fix-r1-overlay"
                      else "r2 release-manifest pin"),
        })
        chain.append({
            "sha256": entry["sha256_after_r2"],
            "label": "DP03 r2 post-repair pin = r3 release-manifest pin",
        })

        pinned["sha256"] = entry["sha256_after_r2"]
        pinned["pin_source"] = "dp03-r2-overlay"
        overlays[path] = {
            "sha256": entry["sha256_after_r2"],
            "sha256_before": entry["sha256_before_r2"],
            "node": pinned["node"],
            "pin_source_replaced": previous_pin_source,
            "bytes": entry["bytes"],
            "live_tree_verified": True,
            "chain": chain,
        }

    # DP03 r2's unchanged paths: pins identical and live tree matches.
    for entry in unchanged:
        path = entry["path"].replace("\\", "/")
        if path not in by_path:
            raise SystemExit(f"DP03 r2 unchanged path not pinned by the release manifest: {path}")
        if not (entry["sha256_before_r2"] == entry["sha256_after_r2"]
                == by_path[path]["sha256"]):
            raise SystemExit(f"DP03 r2 unchanged pin disagreement for {path}")
        if _sha256(REPO / path) != entry["sha256_after_r2"]:
            raise SystemExit(f"live tree drift on DP03 unchanged path {path}")

    # Overlay bookkeeping in the manifest itself.
    for path in overlays:
        if path in manifest["pin_overlays"]["files"]:
            manifest["pin_overlays"]["files"][path]["superseded_by_dp03_r2_overlay"] = True
    manifest["pin_overlays"]["dp03_r2_manifest"] = DP03_R2_MANIFEST_REL
    manifest["pin_overlays"]["dp03_r2_files"] = overlays
    manifest["notes"] = list(manifest["notes"]) + [R3_NOTE]

    manifest["built_at_utc"] = _now()
    _dump(manifest, R3_RELEASE)
    return manifest


def diff_r2_r3() -> Dict[str, Any]:
    """Prove the r3 files-array differs from r2 at EXACTLY the triple."""
    r2 = json.loads(R2_RELEASE.read_text(encoding="utf-8"))
    r3 = json.loads(R3_RELEASE.read_text(encoding="utf-8"))
    m2 = {f["path"]: f for f in r2["files"]}
    m3 = {f["path"]: f for f in r3["files"]}
    if len(m2) != 117 or len(m3) != 117 or set(m2) != set(m3):
        raise SystemExit(f"path-set mismatch: r2={len(m2)} r3={len(m3)}")

    changed, identical = [], []
    for path in sorted(m2):
        if m2[path] == m3[path]:
            identical.append(path)
        else:
            fields = sorted(k for k in set(m2[path]) | set(m3[path])
                            if m2[path].get(k) != m3[path].get(k))
            changed.append({
                "path": path, "node": m2[path]["node"], "role": m2[path]["role"],
                "sha256_r2": m2[path]["sha256"], "sha256_r3": m3[path]["sha256"],
                "fields_changed": fields,
                "pin_source_r2": m2[path].get("pin_source"),
                "pin_source_r3": m3[path].get("pin_source"),
            })
    changed_paths = sorted(c["path"] for c in changed)
    if changed_paths != sorted(EXPECTED_OVERLAYS):
        raise SystemExit(f"manifest diff != routed triple: {changed_paths}")
    for c in changed:
        if c["sha256_r3"] != EXPECTED_OVERLAYS[c["path"]]:
            raise SystemExit(f"r3 pin != routed expectation for {c['path']}")

    sections = {}
    for key in sorted(set(r2) | set(r3)):
        if key == "files":
            continue
        sections[key] = {"changed": r2.get(key) != r3.get(key)}
    expected_changed_sections = {"built_at_utc", "pin_overlays", "notes"}
    unexpected = {k for k, v in sections.items() if v["changed"]} - expected_changed_sections
    if unexpected:
        raise SystemExit(f"unexpected non-files section drift: {sorted(unexpected)}")
    explanations = {
        "built_at_utc": "new r3 build timestamp",
        "pin_overlays": ("added dp03_r2_manifest + dp03_r2_files records; collector.py "
                         "x08fix entry flagged superseded_by_dp03_r2_overlay"),
        "notes": "appended the r3 re-pin note",
    }
    for key, meta in sections.items():
        meta["reason_if_changed"] = explanations.get(key)

    result = {
        "purpose": "prove the r3 release manifest re-pins EXACTLY the three routed DP03 r2 files",
        "r2_manifest": _rel(R2_RELEASE),
        "r3_manifest": _rel(R3_RELEASE),
        "r2_manifest_sha256": _sha256(R2_RELEASE),
        "r3_manifest_sha256": _sha256(R3_RELEASE),
        "file_count_r2": len(m2),
        "file_count_r3": len(m3),
        "path_sets_identical": True,
        "changed_entries": changed,
        "changed_count": len(changed),
        "identical_count": len(identical),
        "identical_entries_byte_equal": True,
        "non_files_sections": sections,
        "overlay_source": {
            "manifest": DP03_R2_MANIFEST_REL,
            "manifest_sha256": _sha256(DP03_R2_MANIFEST),
            "record": "sha256_before_r2 -> sha256_after_r2 per changed file (zero hand-copying)",
        },
    }
    _dump(result, HERE / "manifest-diff-r2-r3.json")
    return result


def verify_r3() -> Dict[str, Any]:
    """VERIFY-mode re-runs: main + one independent directory, both CLEAN."""
    if VERIFY_OUTPUT.exists():
        shutil.rmtree(VERIFY_OUTPUT)
    main = _run_verifier(R3_RELEASE, VERIFY_OUTPUT)
    if main["exit_code"] != 0 or not main["clean"]:
        raise SystemExit(f"r3 verify not CLEAN: {main}")
    if (main["files_verified"] != 117 or main["drift"] or main["missing"]
            or main["dependency_pin_mismatch"] or main["gate_errors"]):
        raise SystemExit(f"r3 verify unexpected values: {main}")
    shutil.copyfile(VERIFY_OUTPUT / "verification-report.json", HERE / "release-verification.json")
    shutil.copyfile(VERIFY_OUTPUT / "verification-summary.md", HERE / "release-verification-summary.md")

    if INDEP_OUTPUT.exists():
        shutil.rmtree(INDEP_OUTPUT)
    indep = _run_verifier(R3_RELEASE, INDEP_OUTPUT)
    if indep["exit_code"] != 0 or not indep["clean"]:
        raise SystemExit(f"independent r3 verify not CLEAN: {indep}")

    runs = {
        "main": {k: main[k] for k in (
            "exit_code", "clean", "manifest_files", "files_verified", "drift",
            "missing", "dependency_pin_mismatch", "gate_errors", "v15_gate",
            "verified_at_utc", "command")},
        "independent": {k: indep[k] for k in (
            "exit_code", "clean", "manifest_files", "files_verified", "drift",
            "missing", "dependency_pin_mismatch", "gate_errors", "v15_gate",
            "verified_at_utc", "command")},
    }
    _dump(runs, HERE / "verification-runs.json")
    return runs


def inherit_not_run_register() -> Dict[str, Any]:
    """r3 runs no new legs: the r2 register is inherited VERBATIM."""
    source = R2_DIR / "not-run-register.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    register = {
        "schema": "dp08-not-run-register/1",
        "node": "DP08",
        "revision": "r3",
        "generated_at_utc": _now(),
        "statement": (
            "Verbatim inheritance of the r2 NOT_RUN register: r3 is the "
            "DP-HARD-r2-routed release-manifest re-pin (bookkeeping) - no "
            "new leg was executed or authorized, so no entry changes status "
            "and no entry is added or removed."),
        "inherited_from": _rel(source),
        "inherited_file_sha256": _sha256(source),
        "entries": payload["entries"],
        "entry_changes": "none (byte-identical entry list)",
    }
    _dump(register, HERE / "not-run-register.json")
    return register


def write_report(preflight: Dict[str, Any], diff: Dict[str, Any],
                 runs: Dict[str, Any]) -> None:
    main = runs["main"]
    lines = [
        "# DP08 r3 · Release manifest 重钉（记账项）— 完成报告",
        "",
        "- Owner：DP08 r3 release manifest 重钉记账席（路由：DP-HARD-r2 verdict PASS，"
        "记账项「DP08 release manifest 需重钉 DP03 r2 三件」）",
        "- 模式：OFFLINE / 记账（bookkeeping）— 无网络、无 DB、无 SDK、不读 .env、无 git 写、"
        "无新测试腿；对仓库源文件与 DP03 r2 树零写入（DP03/r2/file-manifest.json 仅只读)",
        "- 证据目录：`docs/datapipeline-evidence-20260923-r1/DP08/r3/**`（本目录）",
        f"- 日期：{main['verified_at_utc'][:10]}（UTC）；Windows 10.0.22624，Python 3.12.10",
        "",
        "## 0. 一句话结论",
        "",
        f"r3 release manifest 按 r2 构建器的程序化等价路径（`verify_datapipeline_release.py` "
        f"的 `build_manifest()` 只读导入复跑 + DP03 r2 pin overlay 叠加，来源 "
        f"`DP03/r2/file-manifest.json` 前后哈希对照）重钉恰三件：collector.py "
        f"`64387dda…`、synthetic_doubles.py `c9318daf…`、test_collector.py `3ca9617b…`；"
        f"其余 114 件与 r2 manifest 逐字节相同；验证器 verify 模式独立两跑均 "
        f"**117/117 CLEAN、drift 0、missing 0、依赖钉交叉 0、gate errors 0、V15=NOT_RUN，exit 0**。",
        "",
        "## 1. 程序化构建路径（零手抄）",
        "",
        "1. 只读导入 `scripts/verify_datapipeline_release.py`，调 `build_manifest()`——与 r2 "
        "完全同源（七节点 freeze manifest + DP-X08FIX overlay + DP08 写集取最新 file-manifest，"
        "r2 覆盖 r1）；构建器源文件零改动。复跑基线与冻结的 r2 manifest 文件钉 117/117 全同（构建确定性证明）。",
        "2. 叠加 DP03 r2 pin overlay（逐项断言，任一失败即中止构建）：",
        "   - overlay 路径必须已被钉（绝不新增/改名路径）；",
        "   - `sha256_before_r2` == 被替换钉（CAS 链闭合：collector.py 的 before 即 X-08FIX-after "
        "`f5cf3d0e…`，另两件的 before 即 DP03 r1 冻结钉）；",
        "   - 活树重哈希 == `sha256_after_r2`，字节数一致；",
        "   - 改动集合恰为路由三件（对 `EXPECTED_OVERLAYS` 断言）；",
        "   - DP03 r2 未改动 9 件的 before==after==manifest 钉==活树。",
        "3. `pin_overlays` 段记录 dp03_r2 overlay（来源 manifest 路径+sha256、逐件 before/after、"
        "collector.py 三段链 `82e5615b→f5cf3d0e→64387dda`）；x08fix 段中被取代的 collector.py "
        "条目标注 `superseded_by_dp03_r2_overlay`。readback.py 的 X-08FIX overlay 钉仍生效。",
        "",
        "## 2. 差分恰三件（manifest-diff-r2-r3.json）",
        "",
        "| # | 路径（DP03） | r2 钉 → r3 钉 | 字段变化 |",
        "| --- | --- | --- | --- |",
    ]
    for i, c in enumerate(diff["changed_entries"], 1):
        lines.append(f"| {i} | `{c['path']}` | `{c['sha256_r2'][:12]}…` → "
                     f"`{c['sha256_r3'][:12]}…` | {', '.join(c['fields_changed'])} |")
    lines += [
        "",
        f"- 文件数：r2 = r3 = **117**，路径集合相同；改动条目 **{diff['changed_count']}**，"
        f"逐字节相同条目 **{diff['identical_count']}**（117 = 3 + 114）。",
        f"- r3 manifest sha256：`{diff['r3_manifest_sha256']}`"
        f"（r2 基线 `{diff['r2_manifest_sha256'][:12]}…`）。",
        "- 非 files 顶层段变化：`built_at_utc`（新时间戳）、`pin_overlays`（新增 dp03_r2 记录+取代标注）、"
        "`notes`（追加 r3 重钉说明）；其余段（schema/v_matrix/node_test_results/dp08/repo_root）与 r2 全同。",
        "- 构建时前置复核（`r2-manifest-recheck.json`）：冻结 r2 manifest 对活树 verify 重放 exit 1，"
        "drift 恰为三件且 actual==r2 新钉——与 DP-HARD-r2 审席 `logs/release-117-recheck.json` 结论一致。",
        "",
        "## 3. 验证器 verify 模式重跑（verification-runs.json）",
        "",
        f"- 主跑：{main['files_verified']}/117 逐字节一致，CLEAN，exit 0"
        f"（输出 `.tmp/datapipeline/DP08/r3/verification-output`，副本 `release-verification.json` / "
        f"`release-verification-summary.md`）。",
        f"- 独立二跑（异目录）：同样 117/117 CLEAN，exit 0"
        f"（`.tmp/datapipeline/DP08-r3-independent-check`）。",
        "- V15 gate：NOT_RUN（无 canary 证据，验证器拒任何 RUN 推断）。",
        "",
        "## 4. NOT_RUN 沿袭（not-run-register.json）",
        "",
        "- r2 登记册 8 项逐字继承（含 NR-DP08-04 RESOLVED_AT_R2）：r3 未执行也未授权任何新腿，"
        "无状态变更、无新增、无删减；继承源文件 sha256 记录在册。",
        "",
        "## 5. 边界与诚实声明",
        "",
        "- 零源文件改动：`scripts/verify_datapipeline_release.py` 及全部被钉 117 件仅只读（verify CLEAN "
        "本身即逐字节证明）；DP03 r2 证据树零写入。",
        "- 本席写入仅限 `DP08/r3/**` 证据目录与 `.tmp/datapipeline/DP08/r3/**`、"
        "`.tmp/datapipeline/DP08-r3-independent-check` 白名单运行根。",
        "- r3 为记账重钉：未跑任何测试腿（r2 的 48/1 owner 自测结果原样随册，不构成新运行声明）；"
        "DP08 r3 独立硬审沿袭 NR-DP08-08 的 NOT_RUN 状态。",
        "- 本目录自清单外文件（build_evidence.py、本报告、manifest 与各登记副本）按仓例不入自 manifest。",
        "",
        "## 6. Reviewer 重跑指引（r3）",
        "",
        "1. `python -B scripts/verify_datapipeline_release.py --manifest "
        "docs/datapipeline-evidence-20260923-r1/DP08/r3/release-manifest.json --output <新目录（冻结面外）>` "
        "→ 期望 exit 0 / CLEAN 117/117。",
        "2. `python -B docs/datapipeline-evidence-20260923-r1/DP08/r3/build_evidence.py` → 期望重建全部 r3 "
        "产物（时间戳外逐字节同构，构建器含全部断言）。",
        "3. 反例建议：对任一非三件文件改动一字节（verify 报精确路径 DRIFT）；篡改 DP03/r2/file-manifest.json "
        "的 after 值（overlay CAS/活树断言响亮中止）。",
    ]
    (HERE / "DP08-r3-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    preflight = preflight_recheck()
    print(f"[1/5] preflight r2-manifest recheck: drift == routed triple "
          f"({preflight['files_verified']}/117 verified)")
    build_r3_manifest()
    print(f"[2/5] r3 manifest built: {R3_RELEASE.name} "
          f"({len(json.loads(R3_RELEASE.read_text(encoding='utf-8'))['files'])} files)")
    diff = diff_r2_r3()
    print(f"[3/5] diff r2->r3: changed={diff['changed_count']} identical={diff['identical_count']}")
    runs = verify_r3()
    print(f"[4/5] verify: main exit {runs['main']['exit_code']} clean={runs['main']['clean']} "
          f"{runs['main']['files_verified']}/117; independent exit {runs['independent']['exit_code']} "
          f"clean={runs['independent']['clean']}")
    inherit_not_run_register()
    print("[5/5] NOT_RUN register inherited verbatim (8 entries)")
    write_report(preflight, diff, runs)
    print(f"r3 manifest sha256: {diff['r3_manifest_sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
