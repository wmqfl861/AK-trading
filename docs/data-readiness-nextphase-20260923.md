# 数据就绪度、来源分级与使用资格清单（nextphase 2026-09-23）

- **工单**：`docs/devplan-nextphase-20260923-r1/work-orders/P02-data-readiness.md`（P02，数据证据负责人）。
- **本文件性质**：纯文档/允许证据盘点。**它不是数据完整性 PASS、不是 M5 PASS、不是 R8 结案，不升级任何既有资格。**
- **证据包**：`docs/nextphase-evidence-20260923-r1/P02/r1/`（gap matrix JSON/CSV、feed-input-contract 草案、逐文件 SHA-256、完成报告）。
- **方法边界**：无网络、无 DB/在线采集、无 SDK 实连、无凭据读取、无 git 写。所有"观察值"均来自**带日期的证据文件快照**（含其 SHA-256），不代表当前数据库状态；没有允许来源的格子一律 `UNKNOWN`（null 不是 0，也不是"不存在"）。
- **本文引用的源码行号**均为当前主仓字节下的实测行号（见证据包 `file-hashes.json`）。

---

## 1. 边界核实（工单"当前可核实的边界"逐条复核）

| # | 工单引用 | 当前字节复核 | 结论 |
| --- | --- | --- | --- |
| B1 | `docs/acquisition-free-runbook.md:3` | `:3` 仍为 `Status: DEVELOPMENT ONLY. No independent review seal exists…` | 属实。**但与维护工件存在状态矛盾，见 §5.3** |
| B2 | `acquisition/tq_adapter.py:5` | `src/ak_trading/acquisition/tq_adapter.py:6` "real TqApi adapter … **written but never executed**" | 属实（行号 6，docstring） |
| B3 | `acquisition/cli.py:1001` | `src/ak_trading/acquisition/cli.py:1012` `evidence_integrity_verdict="NOT_RUN"`（记录块自 `:1001` 起） | 属实。产品报告仍以 NOT_RUN 发布，不得由源码存在推导已在线采集/验收 |
| B4 | `acquisition/storage.py:811` / `:74` | `:811` `export_existing_versions` 仅导出 `datetime_nano/content_sha256/row_status/evidence_version/version_id` 五列（无 OHLC/时钟）；`:74` `bar_versions` 表定义含 `open_s/high_s/low_s/...` 字段 | 属实。判为**待对齐接口**，不将版本盘点 API 直接判错 |
| B5 | `devplan-v2/06-M5:310/314` | `:312` 无合格 feed 时 runtime 仍 `SHADOW_PENDING`、M5 不能 PASS；`:314` 连续 20 交易日、日/夜盘、完整率≥0.995、P99≤5s、独立 ReceiverClock | 属实。离线测试不能替代 |
| B6 | `data-recovery-status-2026-09-12.md:53` | `:54` "后来的失败运行会覆盖先前成功运行的捕获文件" | 属实（行号 54）。**历史问题登记见 §6.2，本次未映射脚本、不声称现版复现或已修** |

## 2. Gap matrix（逐产品 × 日/夜盘 × 字段族）

机器可读版：`E/P02/r1/gap-matrix.json`（81 品种 × 8 数据类 = 648 格）+ `E/P02/r1/gap-matrix.csv`；由 `E/P02/r1/tools/generate_gap_matrix.py` 离线生成（只读允许文件，可复算）。每格保留：期望范围、观察数、重复/乱序/缺时钟、来源文件+SHA-256、来源可见时间与导入时间之别、calendar/PIT/UNKNOWN/SYNTHETIC 分级、当前状态与 owner。

### 2.1 覆盖骨架（观察值全部来自 2026-09-04 快照文件）

| 维度 | 证据 | 观察值 | 当前（2026-09-23）状态 |
| --- | --- | --- | --- |
| 主连分钟 × 品种指数日线（81 品种） | `data/DATA_COMPLETENESS_20260904.json`（SHA-256 见证据包） | `bars_total=421,718,005`，`null_ohlc=0`，`negative_volume=0`；主连 vs 指数缺日合计 171（JR 139 / zn 21 / WH 11） | **UNKNOWN**：W0（2026-09-19）确认 81/81 品种 freshness UNKNOWN，P02 未重查 |
| 合约级日线 vs 分钟（4,935 合约） | 同上 | full=4935，partial=0，no_minute=0，缺日 0 | 聚合值**不能**分摊到具体品种/合约/日期（W0 Q0 check 原文） |
| 会员排名（六所 2020-07-20 起） | 同上 | 逐年 days/rows 全量在文件内 | CZCE 412 反爬、GFEX 无披露日（`docs/data-recovery-status-2026-09-10.md`）；sc 全渠道无披露、ec 止 2026-04（不可补登记簿） |
| 日/夜盘会话（81 品种） | `night-session-matrix.json`（2026-09-19，官方页证据） | 夜盘：SHFE 18/19、INE 4/5、DCE 17/20、CZCE 13/24、CFFEX 0/8、GFEX 0/5；夜盘归属上一工作日 21:00 起 | **CALENDAR 级**（密封证据），仅证明会话结构，不证明 bar 存在 |
| 结算参数（费率/保证金） | `data/settlement_params/latest.json`（day=2026-09-23；目录 20260905–20260923 共 19 日） | official 30 品种、reference 90、交叉核对 25、**1 项不一致（INE.ec 档位 0.19/0.22/0.30 vs 参考 0.28）未隐藏** | 当日快照可用；**历史 PIT 未知**（§3.4） |
| 具体合约端点探测（11 合约：rb2305–rb2501、cu2405、au2412、m2409、i2409、sc2409） | `v2-real-input-coverage-20260914.json` | 各合约 60s/86400s 首末 bar 在文件内；`internal_completeness=not_checked`；明列 5 项 not_verified（内部完整度/PIT/闭环/会话映射/可交易性） | UNKNOWN |

### 2.2 不可补缺口登记簿（回测/使用必须遵守，来源 `DATA_COMPLETENESS_REPORT_2026-09-04.md` §三）

`BAD_SOURCE_DAY_20180806`（DCE.p/SHFE.zn 主连分钟坏行日，当前免费权限 403 不可补，09-10 状态文复证）｜`SC_RANKING_NOT_DISCLOSED`｜`EC_RANKING_2026_05_PLUS`｜`TRADING_DAY_NULL_PRE_2026`（`trading_day` 仅 2026 有值，`ts` 权威）｜`CFFEX_VOLUME_DOUBLE_COUNTED`（排名成交量双边口径）｜`RANK_HISTORY_STARTS_2020_07`。另：`MAIN_CONT_MISSING_171_DAYS`（分钟级无可信来源可补；日线代理=品种指数）。

### 2.3 字段级说明

- OHLC/volume/OI 列在表定义与快照中齐备（`null_ohlc=0`）；**独立接收时钟对历史导入不存在**（bar 时钟独立性 NOT_RUN）。
- `export_existing_versions`（`storage.py:811`）不含 OHLC/时钟列——feed 交接用途待冻结（P05 契约，§7），此形态不判错。
- 已消费留出：`experiment-registry.jsonl` 5 条 holdout 消费登记继续有效；本矩阵**不**重新授予任何已消费数据新资格。

## 3. 数据消费者资格清单

原则：**数据库有行 ≠ 资格成立**；历史缺口不自动阻断纯 fake 工程；一旦某数据进入真实控制/风险链路，即成为对应 `G_real` 的**硬依赖**。

### 3.1 研究回放（research replay）

- **SHFE.rb（具体合约、HISTORICAL_RECONSTRUCTED）**：唯一有完整冻结证据链的品种——`real-input-rb-r3`（bars 2,489,676；collection manifest 逐分片 SHA-256）、`m2-real-track-run-r1`（contract v2.1.2，绑定 lock `ac211ec9…e93b66`）、`m3a-real-80d`（contract v2.1.3；manifest 含逐文件与代码哈希）。**限制原样保留**：availability 为 `end_nano+60s` 重构（`rows_reconstructed=82484`），"not observed publication or measured latency bound"；`imported_at` 是审计值不是历史首见；原始发布/修订史 NOT_PROVEN。→ 资格：**仅重构口径的历史研究**，非 PIT。
- **其余 80 品种**：Q1（M1 可读冻结输入）80/81 UNKNOWN（W0）。→ 资格：**未成立**（旧日存在性代理不升级）。
- **SYNTHETIC**：`synthetic-input-452d-r1..r3`、`m5-synthetic-*` 等夹具一律标 SYNTHETIC，仅限工程自测。

### 3.2 分钟 feed（MinuteFeed 实时链路）

- `realtime_status=NOT_ACCEPTED`（所有 export 输出）；M5：无合格 feed → runtime `SHADOW_PENDING`（`06-M5:312`）。
- 20 交易日/品种×日/夜盘验收：**0/20 天**；重复/乱序/断流/重启恢复证据：NOT_RUN；独立 ReceiverClock：NOT_RUN。
- W2 采集链：软件评审 PASS（`ACQ-W2-REVIEW-20260919-r1`，明确"不授权真实连接/不认证覆盖/不预批 run order"）；run order FINAL + 用户确认（2026-09-19）已记录；2026-09-20 预检 `CONDITIONALLY_READY`（1 RED：PostgreSQL 服务停机 + 3 AMBER）；专用输出根 `data/acquisition_runs/20260921-acq-w2-ro1-ro3/` **仅含 OUTPUT_ROOT_MARKER.json，无任何采集产品**。→ 资格：**NOT_ACCEPTED / NOT_RUN**；不得宣称已在线采集。

### 3.3 当前 native 快照（账户/持仓/合约/行情不可变快照）

- 客户端 poll 仍 `snapshot=None`（F03）；平台 UI/账户路由/费用上界未现场核验（G02）。→ 资格：**INSUFFICIENT**。观察者原始批次（`data/observer_raw_batches/`，5 段 sealed，末段 2026-09-17）是观察面证据（P01 域），**不**升级执行快照资格。

### 3.4 费用/保证金上界

- 当日：官方 js.dat 快照 2026-09-23 可用（30 official/90 reference/1 mismatch 不隐藏），19 个日目录（20260905–20260923）。
- 历史 PIT：**未成立**。rb-r3 `fee_conflict_1` 的 9 条 2024 年费率规则首见界=2026-09-15T06:11:21+08:00（首次本地可见），**不是**其 2024 发布时间；"no_backdating_granted; first_seen_bound_not_publication_proof"。W0 Q5：81/81 INSUFFICIENT。
- → 一旦用于真实风控/ sizing（NP04/S4、G02），即成 `G_real` 硬依赖，需另行建立 PIT 或上界证明。

## 4. 已consumed 留出 / not_implemented / SHADOW_PENDING / 未审 W2（原样标出，不升级）

| 项 | 状态（原样） |
| --- | --- |
| 已消费 holdout | `experiment-registry.jsonl` 5 条登记有效；已消费槽位不因本文重新开放 |
| `historical_metadata_adapter` | `not_implemented`（`futures/readiness.py:487`、`persistence/futures_research.py:973`）；`historical_gate_evaluation=not_implemented_by_frozen_inventory` |
| runtime | `SHADOW_PENDING`（`06-M5:312`），直到真实 feed 验收 |
| W2 数据验收 | 采集产品**未观察到**（输出根仅 marker）；数据独立验收 NOT_RUN；软件评审 PASS 不等于数据验收 |

## 5. 声明矛盾显式清单（不许只取最乐观字段）

### 5.1 F07 · v2 导出 availability 矛盾（已确认，转 P04）

`src/ak_trading/v2/export.py` 同一导出内三处互相矛盾：
- `:225` bar 级：`availability_unknown_not_reconstructed`（`temporal_basis="unknown"`）；
- `:367` `availability.json` reasons：`availability_reconstructed_from_imported_at`；
- `:387` limitations：`bar_availability_unknown_not_reconstructed_ready`。

bar/manifest 说"未重构"，availability.json 说"由 imported_at 重构"——**同一目录三处不一致**。处置：**转 P04**（写锁释放后合成导出验证三处一致）；不插入 R8 在途修复，不影响 native 工单。当前字节 SHA-256 见证据包。

### 5.2 完整度"100%"声明的适用边界

`DATA_COMPLETENESS_REPORT_2026-09-04.md` 的 100%/0 缺日是**当日 DB 快照的聚合口径**；W0（2026-09-19）按品种逐格复核后 81 品种 freshness 全部 UNKNOWN、Q1 80 UNKNOWN。两份文件不矛盾，但**取用必须按保守者**：行数存在≠可用资格。

### 5.3 W2 runbook 状态行 vs 维护工件

runbook `:3` 声称"无独立评审 seal"，而维护工件已记录：评审 PASS（含 r2 delta 复审）、run-order FINAL、用户确认（2026-09-19）。**P02 不裁决谁对**：runbook 状态行可能滞后。判定仍以 B1–B3 的保守结论为准——因为输出根无产品，在线采集未发生。矛盾登记转 **acquisition 域文档 owner**（devplan-datapipeline 域，不在 P02 写集）修正 runbook 状态行。

### 5.4 imported_at ≠ 首次市场可见

一切 availability 以 `available_at_nano=end_nano+60s` 重构或 imported_at 审计值呈现（export.py `:221-225`、rb-r3 shard limitations）。**本文档不把任何 imported_at 升级为首见/发布时间。**

## 6. 单独登记项（不混入 R8 修复或 native 工单）

### 6.1 F07 → P04

见 §5.1。最小复现：读同一次 `export_frozen_input` 产物三处字段即可对照；修复 owner=P04，验收=三处一致 + 合成导出验证。

### 6.2 DCE 覆盖历史问题（G04）

`docs/data-recovery-status-2026-09-12.md:54`：`data/rank_probe/dce_profiles/<YYYYMMDD>/` 按目标日命名、后续失败运行覆盖先前成功捕获。**本次未映射对应脚本、未复现现版行为**；保留为历史问题，待精确源码映射后另立 Codex 修复工单。在映射完成前，DCE 排名探针历史捕获文件**不可作为时点证据**使用。

## 7. feed-input-contract 草案（供 Codex 冻结；P05 冻结前仅 prepare）

草案文件：`E/P02/r1/feed-input-contract.md` + `.json`（**DRAFT，未冻结**）。要点：完整已关闭 bar（`bar.end_nano<=watermark`）、具体合约身份、稳定版本/sequence、源时钟+独立接收时钟、origin 与 source-hash、水位/重复语义。**不能推断 R8 输出字段**；R8 责任链（infiny-arm-r8-*, 在途）未提供已发布交接 → P05 维持 prepare 态，实现需待契约冻结。

## 8. 后续 owner 清单

| 事项 | owner | 门 |
| --- | --- | --- |
| feed-input-contract 冻结 | Codex（以 `E/P02/r1/feed-input-contract.*` 草案为输入） | 冻结后 P05 方可离线实现 |
| W2 真实采集与数据独立验收 | acquisition 域（devplan-datapipeline） | H15 门序 + 其自身计划；P02 不执行 |
| runbook 状态行修正 | acquisition 域文档 owner | 与 §5.3 登记一并处理 |
| F07 三处一致修复 | P04 | 写锁释放 |
| DCE 覆盖源码映射+修复单 | 待 Codex 新工单 | §6.2 |
| 历史 PIT 费率/保证金（若入真实风险链） | NP04/G02 证据线 | `G_real` 硬依赖 |
| M5 真实 feed 20 日验收 | M5 验收 owner（另单） | `06-M5:314` 全部门槛 |

## 9. NOT_RUN / 限制

- NOT_RUN：任何 DB 查询/在线采集/SDK 实连/凭据读取；20 日 feed 观测；数据独立验收；F07 修复；DCE 脚本映射；R8 材料读取（互斥）。
- 所有观察值来自日期快照文件；当前库内状态与新鲜度一律 UNKNOWN。
- gap-matrix 为**证据索引与资格判定**，不补 0、不代查生产库；后续任何 DB/在线核验按验收条款另立运行工单及其门。

---

*P02 r1 · 2026-09-23 · 唯一写集：本文件 + `docs/nextphase-evidence-20260923-r1/P02/r1/**`。出口=缺口/资格/owner 清单，非任何 PASS。*
