# 交接：第四轮外部审核之后的工作（2026-11-10）

给本地 agent（Root）。依据见 [REVIEW.md](REVIEW.md)，蓝图见 [BLUEPRINT.md](BLUEPRINT.md)，每项的改法见 [FIXES.md](FIXES.md)，排期见 [DEVELOPMENT-PLAN.md](DEVELOPMENT-PLAN.md)。

> 本目录下的文件都是外部建议，不是有效计划，也不是产品硬审核。有效计划仍是 r3-001 及其 H1–H8 增补。本批的授权范围以用户发给你的提示词为准；提示词没有写到的事项，按现行规则处理，不因本文件自动采纳。
>
> 用户表示看完功能说明后还要讨论优化方向。所以任务分成两批：第一批是缺陷修复和防护，不依赖那次讨论；第二批等用户的决定。

## 1. 本轮发现里你最需要知道的五件事

1. **`b653f7e` 引入了回归。** `tests/test_v2_forward_paper.py` 在它之前 68 项全过，从它开始 20 过 48 败，原因都是 `WAITING_ENTRY_CONFIRMATION`。趋势和突破的观察期在第 3 根 K 线收盘时到期，决策晚于收盘就凑不齐确认。日线在多交易节的日历上所有策略都凑不齐。（REVIEW 的 X01）
2. **震荡三族在生产流程里过不了准入。** `assess_regime` 对有序震荡永远给 `cross_timeframe="UNKNOWN"`，`admission_decision` 拒绝它。M3B 的夹具手工填了 `ALIGNED`。（X02）
3. **H7 按现有规格跑完几乎必然零成交。** 资金三档低于最低权益，日线样本量达不到门槛，再加上第 1 条。（Y01）
4. **时钟函数每次调用都重建日历。** `clock.horizon_window` 在 250 个交易日的日历上 16 毫秒一次。索引后结果逐项相同，快四到五个数量级。（X10）
5. **`b653f7e` 之后没有在 HEAD 上跑过全量测试。** 第 1 条因此存在了 15 天。（X20）

这五条都可以在你的机器上复现，命令在第 8 节。请先复现，再决定怎么报告给计划作者。

## 2. 开工前检查（Root，不改产品）

1. 取得本目录：`git fetch origin` 后把分支 `claude/repo-audit-20261110` 合入 main（只新增 `docs/repo-audit-20261110-claude/` 下的文件，不会有冲突）。记录合入后的 HEAD。
2. 按第 8 节跑四条复现命令，把原始退出码和通过、失败数记进状态页。结果与本文不一致时，以你的实测为准，并把差异写出来。
3. 记录 H5 两条冷链、H8 profile 作业的当前状态。不要因为本文件而中断它们；是否停止由用户在提示词里决定。
4. 读取用户在提示词里给出的 J1–J8 决定和对 H5–H8 的处置。没有给出的项不采纳。

## 3. 第一批任务（不依赖讨论）

每项都按现行分工：涉及语义的由计划作者裁定，开发负责人实施，另一独立审核者做成果硬审。

| 顺序 | 编号 | 任务 | 线 | 完成标准 | 需要的裁定或授权 |
| --- | --- | --- | --- | --- | --- |
| 1 | K03 | 全量回归基线与对照脚本 | 支撑 | 基线文件入库；脚本能在 `b653f7e` 前后报出 48 项新增 | 无 |
| 2 | K01 | 入场确认在真实时钟和日历下可达 | 关键 | `probes/test_entry_confirmation_reachability_probe.py` 9 项全过；前瞻测试 68 项全过；重复 K 线 96 项仍全过；全量无新增失败 | 计划作者裁定两点语义；属于对 `b653f7e` 的修正，回原开发者 |
| 3 | K02 | 震荡状态的跨周期结论 | 关键 | `probes/test_range_family_cross_timeframe_probe.py` 按裁定后的语义全过；自然链上三类震荡策略能走到 `OPEN` | 计划作者在方案甲、乙之间选择 |
| 4 | K05 | 真实形状夹具与自然链冒烟测试 | 随 K01、K02 | 冒烟测试在 CI 里，六类交易策略各至少一次 `OPEN` | 无 |
| 5 | K04 | 可达性预检脚本 | 支撑 | 合成夹具上“可达”；小资金短窗口配置上 `structurally_empty=true`；对 H7 已选定的输入和配置跑一次并把结果报给用户 | 无 |

K01 和 K02 建议在同一个候选里完成、一起审核、只建一次新锁。

K01、K02 的审核范围请写明包含 `tests/test_v2_forward_paper.py`、`tests/test_v2_m3b_duplicate_bar.py` 和第二轮的重复 K 线探针。

用户同意后还可以顺手做的一件小事：把 `docs/mcp-agent-guide.md` 第 4 行的公网地址换成占位符。令牌轮换和仓库可见性由用户本人处理。

## 4. 第二批任务（等用户的决定）

| 编号 | 任务 | 取决于 |
| --- | --- | --- |
| K40 | v1 日线基线报告（RB，随后 M、I） | J2 选 A |
| H6′ | T7 DAILY 验收，夹具改为真实形状的日历 | K01、K02 合入 |
| K10、K11、K12 | 日历索引、上下文共享、多尺度 profile | 计划作者确认写集；可在支撑线先行 |
| K13 | 模型拟合向量化 | J4 |
| H5′、K41 | T7 MINUTE 验收、RB MINUTE 报告 | K01、K02、K10、K11；预检通过 |
| K20–K24、K31、K32 | 仓库自足、CI 第二层、依赖、文档指针、端口、默认合约 | 各自的决定，见 FIXES |

## 5. 本批不做

- 不启动 H6、H7（除非用户在提示词里要求按原规格继续）。
- 不启动新的 H5 冷链。K01、K02 会改变策略库的哈希，现有的锁和冷链记录之后都要重做。
- 不续接 N8、DISPATCH，不做完整 shadow、Forward 采集、本地 paper。
- 不改统计门槛、风控参数、窗口长度、留出集边界。预检发现达不到门槛时，报告给用户，不调整门槛。
- 不为了让测试变绿而改测试夹具的预期、刷新冻结哈希、加 skip 或 xfail。
- 没有用户授权时不修改四份规则文件的规则内容。
- 不做全库换行规范化。

## 6. 边界

- 不读取、不打印、不修改 `.env` 和任何凭据（TQ_USER/TQ_PASS、数据库口令、AK_MCP_TOKEN）。
- 只由用户本人执行：仓库可见性、令牌、防火墙、MCP 服务重启、生产数据库角色切换、Windows 系统设置、客户端界面。你只准备步骤。
- 保持 `enable_trading=False`、observer=`hold`、broker=`NOT_RUN`、真实 send/cancel `0/0`。
- 不修改前几轮和本轮外部审核写的文件。探针测试在缺陷修复后应当通过；如果裁定后的语义和探针的预期不同（比如 K02 的四个状态用例），在你自己的测试里按新语义写，并在状态页说明探针的哪几项因此不再适用。
- 仓库在转私有之前仍是公开的：不把网络入口的核实细节、输入数据包、私有路径下的会话信息写进仓库。

## 7. 交付与记录

- 每项完成就提交并推送 main。提交说明写清改动、运行的命令、原始退出码、passed/failed 数，以及全量对照的新增失败数。
- 状态只更新 `docs/CURRENT-STATUS.md`。建议在页首加三行给用户看的话：本周能看到什么、卡在哪、要你做什么，每行不超过 40 个字。
- 候选分支在仓库转私有后每天推到远端。
- 第一批全部完成或被阻塞时停止，给用户一页摘要：K01–K05 各自的状态、阻塞原因、K04 对 H7 输入的预检结果，以及仍需用户决定的 J 项。

## 8. 复现命令

在仓库根目录、项目的 Python 3.13 环境里执行。每条命令单独捕获原始退出码。

```powershell
# 1. 回归：预期 20 passed / 48 failed，失败原因都是 WAITING_ENTRY_CONFIRMATION
& $py -B -X utf8 scripts/test_offline.py -q tests/test_v2_forward_paper.py

# 2. 入场确认可达性探针：预期 5 failed / 4 passed
& $py -B -X utf8 scripts/test_offline.py -q docs/repo-audit-20261110-claude/probes/test_entry_confirmation_reachability_probe.py

# 3. 震荡族跨周期探针：预期 7 failed / 4 passed
& $py -B -X utf8 scripts/test_offline.py -q docs/repo-audit-20261110-claude/probes/test_range_family_cross_timeframe_probe.py

# 4. 时钟函数基准：单次耗时随交易日数增长；末行应为 "All sampled results equal the product implementation"
& $py -B -X utf8 docs/repo-audit-20261110-claude/probes/clock_horizon_bench.py

# 5. 最低权益（把参数换成选定输入里的真实值）
& $py -B -X utf8 docs/repo-audit-20261110-claude/probes/budget_feasibility.py --name RB --price <参考价> --atr <日线ATR> --multiplier 10 --tick 1 --margin <保证金率> --fee-rate <费率>
```

第 1 条的二分（可选）：在 `b653f7e^` 上同一条命令应为 68 passed。

## 9. 检查点

| 日期 | 应达到 |
| --- | --- |
| 11-14 | K03 基线入库；K04 对 H7 输入的预检结果已报给用户 |
| 11-21 | K01、K02、K05 合入；前瞻测试 68 通过；冒烟测试在 CI 里 |
