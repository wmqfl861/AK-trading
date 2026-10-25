# DP08 r3 · Release manifest 重钉（记账项）— 完成报告

- Owner：DP08 r3 release manifest 重钉记账席（路由：DP-HARD-r2 verdict PASS，记账项「DP08 release manifest 需重钉 DP03 r2 三件」）
- 模式：OFFLINE / 记账（bookkeeping）— 无网络、无 DB、无 SDK、不读 .env、无 git 写、无新测试腿；对仓库源文件与 DP03 r2 树零写入（DP03/r2/file-manifest.json 仅只读)
- 证据目录：`docs/datapipeline-evidence-20260923-r1/DP08/r3/**`（本目录）
- 日期：2026-09-23（UTC）；Windows 10.0.22624，Python 3.12.10

## 0. 一句话结论

r3 release manifest 按 r2 构建器的程序化等价路径（`verify_datapipeline_release.py` 的 `build_manifest()` 只读导入复跑 + DP03 r2 pin overlay 叠加，来源 `DP03/r2/file-manifest.json` 前后哈希对照）重钉恰三件：collector.py `64387dda…`、synthetic_doubles.py `c9318daf…`、test_collector.py `3ca9617b…`；其余 114 件与 r2 manifest 逐字节相同；验证器 verify 模式独立两跑均 **117/117 CLEAN、drift 0、missing 0、依赖钉交叉 0、gate errors 0、V15=NOT_RUN，exit 0**。

## 1. 程序化构建路径（零手抄）

1. 只读导入 `scripts/verify_datapipeline_release.py`，调 `build_manifest()`——与 r2 完全同源（七节点 freeze manifest + DP-X08FIX overlay + DP08 写集取最新 file-manifest，r2 覆盖 r1）；构建器源文件零改动。复跑基线与冻结的 r2 manifest 文件钉 117/117 全同（构建确定性证明）。
2. 叠加 DP03 r2 pin overlay（逐项断言，任一失败即中止构建）：
   - overlay 路径必须已被钉（绝不新增/改名路径）；
   - `sha256_before_r2` == 被替换钉（CAS 链闭合：collector.py 的 before 即 X-08FIX-after `f5cf3d0e…`，另两件的 before 即 DP03 r1 冻结钉）；
   - 活树重哈希 == `sha256_after_r2`，字节数一致；
   - 改动集合恰为路由三件（对 `EXPECTED_OVERLAYS` 断言）；
   - DP03 r2 未改动 9 件的 before==after==manifest 钉==活树。
3. `pin_overlays` 段记录 dp03_r2 overlay（来源 manifest 路径+sha256、逐件 before/after、collector.py 三段链 `82e5615b→f5cf3d0e→64387dda`）；x08fix 段中被取代的 collector.py 条目标注 `superseded_by_dp03_r2_overlay`。readback.py 的 X-08FIX overlay 钉仍生效。

## 2. 差分恰三件（manifest-diff-r2-r3.json）

| # | 路径（DP03） | r2 钉 → r3 钉 | 字段变化 |
| --- | --- | --- | --- |
| 1 | `src/ak_trading/datapipeline/collector.py` | `f5cf3d0efd03…` → `64387dda306a…` | pin_source, sha256 |
| 2 | `tests/datapipeline/fixtures/DP03/synthetic_doubles.py` | `5afba7eb72cd…` → `c9318daf65fe…` | pin_source, sha256 |
| 3 | `tests/datapipeline/test_collector.py` | `8363b95c6bef…` → `3ca9617b973f…` | pin_source, sha256 |

- 文件数：r2 = r3 = **117**，路径集合相同；改动条目 **3**，逐字节相同条目 **114**（117 = 3 + 114）。
- r3 manifest sha256：`943dbe26e3b80fe9ea46d916c2b6e6efef0940297f91c9845974cb5307b80980`（r2 基线 `e8de5ade5d6a…`）。
- 非 files 顶层段变化：`built_at_utc`（新时间戳）、`pin_overlays`（新增 dp03_r2 记录+取代标注）、`notes`（追加 r3 重钉说明）；其余段（schema/v_matrix/node_test_results/dp08/repo_root）与 r2 全同。
- 构建时前置复核（`r2-manifest-recheck.json`）：冻结 r2 manifest 对活树 verify 重放 exit 1，drift 恰为三件且 actual==r2 新钉——与 DP-HARD-r2 审席 `logs/release-117-recheck.json` 结论一致。

## 3. 验证器 verify 模式重跑（verification-runs.json）

- 主跑：117/117 逐字节一致，CLEAN，exit 0（输出 `.tmp/datapipeline/DP08/r3/verification-output`，副本 `release-verification.json` / `release-verification-summary.md`）。
- 独立二跑（异目录）：同样 117/117 CLEAN，exit 0（`.tmp/datapipeline/DP08-r3-independent-check`）。
- V15 gate：NOT_RUN（无 canary 证据，验证器拒任何 RUN 推断）。

## 4. NOT_RUN 沿袭（not-run-register.json）

- r2 登记册 8 项逐字继承（含 NR-DP08-04 RESOLVED_AT_R2）：r3 未执行也未授权任何新腿，无状态变更、无新增、无删减；继承源文件 sha256 记录在册。

## 5. 边界与诚实声明

- 零源文件改动：`scripts/verify_datapipeline_release.py` 及全部被钉 117 件仅只读（verify CLEAN 本身即逐字节证明）；DP03 r2 证据树零写入。
- 本席写入仅限 `DP08/r3/**` 证据目录与 `.tmp/datapipeline/DP08/r3/**`、`.tmp/datapipeline/DP08-r3-independent-check` 白名单运行根。
- r3 为记账重钉：未跑任何测试腿（r2 的 48/1 owner 自测结果原样随册，不构成新运行声明）；DP08 r3 独立硬审沿袭 NR-DP08-08 的 NOT_RUN 状态。
- 本目录自清单外文件（build_evidence.py、本报告、manifest 与各登记副本）按仓例不入自 manifest。

## 6. Reviewer 重跑指引（r3）

1. `python -B scripts/verify_datapipeline_release.py --manifest docs/datapipeline-evidence-20260923-r1/DP08/r3/release-manifest.json --output <新目录（冻结面外）>` → 期望 exit 0 / CLEAN 117/117。
2. `python -B docs/datapipeline-evidence-20260923-r1/DP08/r3/build_evidence.py` → 期望重建全部 r3 产物（时间戳外逐字节同构，构建器含全部断言）。
3. 反例建议：对任一非三件文件改动一字节（verify 报精确路径 DRIFT）；篡改 DP03/r2/file-manifest.json 的 after 值（overlay CAS/活树断言响亮中止）。
