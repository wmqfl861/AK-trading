# 交接：第三轮外部审核之后的重启（2026-11-07）

给本地 agent（Root）。用户已于 11-07 停止全部任务。本文件说明重启的顺序、范围和边界。依据见 [REVIEW.md](REVIEW.md)，蓝图见 [BLUEPRINT.md](BLUEPRINT.md)。

> 本文件与 REVIEW、BLUEPRINT 都是外部建议，不是有效计划，也不是产品硬审核。有效计划仍是 r3-001 及其增补。本批的授权范围以用户发给你的提示词为准；提示词没有写到的事项，按现行规则处理，不因本文件自动采纳。

## 1. 停止时的状态

**已在 main 并通过独立审核**：

| 内容 | 提交 |
| --- | --- |
| A2 区间三族重复 bar（共享确认计数） | `b653f7e` |
| A4 CLI 提前拒绝 `shadow-replay` | `c152e33` |
| M4 绑定测试修复 | `a84d966` |
| T7 CPU 优化 | `943ac06` |
| T7 记录存储与校准一致性 | `7a19c77` |
| T7 fold 内存接线 | `39296d9` |
| C05 测试输出流式化 | `c054bd3` |
| M5 walk-forward 退出码 | `ca84554` |

另有 A3 入口文档同步、B1 的 234 项分诊。

**未完成**：

- T7 完整运行至少 6 次停在第 17/24 个节点的 `positive_cold_run` 夹具，正式 066 已记为消费失败。
- N8、DISPATCH 没有最终结论；M5 性能候选 CDF082 已关闭。
- Forward 与本地 paper 未开始；RB 报告 0 份。
- 仓库外工作区里的候选代码不在 main。

**只能由用户处理**：

- A1：仓库可见性与 P0-1。
- G1–G6 流程调整。
- 分支保护、换行规范化、Python 支持版本。
- Windows 长路径设置。

## 2. 重启前检查（Root，不改产品）

1. `git pull --ff-only origin main`，记录 HEAD。
2. 确认 11-07 停止前的子代理和进程都已结束。不续用任何已消费的运行机会或已过期的窗口，也不自动恢复 T7、N8、DISPATCH、M5 的旧任务。
3. 检查一次磁盘和内存余量，并记录下来。
4. 读取用户在提示词里给出的 G 项决定。没有给出的 G 项不采纳，相关任务按现行规则走计划作者处置。

## 3. 本批任务（按顺序）

每项都按现行分工：计划作者处置计划问题，开发 owner 实施，另一独立审核者做成果硬审。

| 编号 | 任务 | 范围 | 完成标准 | 需要的决定 |
| --- | --- | --- | --- | --- |
| H1 | 一页状态页 | 新建一页状态页（位置由 Root 定）；Goal 目录的 STATUS.md 冻结，不再追加 | 一页内列出 HEAD、未完成项与负责人、BLUEPRINT v3 的八个指标 | 无 |
| H2 | CI | 审核 [ci/offline-v2.yml](ci/offline-v2.yml) 后复制到 `.github/workflows/` | main 上作业全绿，用时不超过 10 分钟；分支保护不在本批 | 无（分支保护需 G6） |
| H3 | 三处小修 | `v2/export.py` 的 sqlalchemy 导入放进 `TYPE_CHECKING`；离线守卫放行 `socket.socketpair()` 和回环自连接；为 `replay.py` 调用 `verify_qualification_evidence` 处加签名契约测试 | 无数据库依赖时 M1 的 2 项锁测试通过；B1 中 socketpair 类 10 项在 Windows 上通过；新测试在参数缺失时失败 | 无 |
| H4 | `visible_bars` 热点 | 修 `v2/data.py` 的 `visible_bars` 与容量路径：对每个（bundle、合约、频率）预建与决策时点无关的过滤结果，二分取可见前缀，给 `capacity_before` 提供“最近 N 根”；参考 [probes/visible_bars_bench.py](probes/visible_bars_bench.py) | 等价测试证明各调用形状的输出逐项相同；v2 M1–M4 结果不变；基准脚本显示单次调用不再随数据量超线性增长 | 计划作者确认写集 |
| H5 | T7 可续跑 | 冷链验收从 `positive_cold_run` 会话夹具拆成可续跑的独立作业，测试只读它写出的记录；正式套件逐节点运行、逐节点记录；`--basetemp` 用短路径，节点内目录名不超过 16 个字符 | 冷链在一次运行内完成；任一节点可单独重跑 | G2，或计划作者为新的正式运行另行处置 |
| H6 | T7 DAILY 验收 | 按频率拆开验收，DAILY 先过 | DAILY 节点全部通过，独立审核 PASS | 计划作者确认按频率排程 |
| H7 | 第一份 RB DAILY 报告 | 用 `scripts/research_probability_v2.py --mode walk-forward`；现有三类策略加对照，四种政策、六种成本场景 | 报告与 manifest 入库，可从提交号复现；必须报告资金与保证金敏感性（至少 1.5 万、5 万、10 万元三档）和样本外结果；结论可以是失败或证据不足 | 无 |
| H8 | MINUTE profile | H4 完成后，对一个有代表性的 MINUTE fold 做 cProfile 和 tracemalloc | 耗时与内存前 20 项提交到仓库，附方向建议 | 改数据表示需用户批准 |

与蓝图 v3 的对应：H1=S1，H2=S2，H3=S4，H4 与 H5=S3，H6=C1a，H7=C2a，H8=P1。

H4、H5、H6、H7 是关键路径，按顺序做；H2、H3 走支撑线，可以与之并行；H8 在 H4 之后任何时候做。如果用户采纳 G1，同一时间最多 1 条关键路径开发线、1 条审核线和 1 条支撑线。

## 4. 本批不做

- N8、DISPATCH 的续接。
- 完整 ordinary shadow，包括 `shadow.py:932` 与 `experiments.py:609`。
- Forward 采集与本地 paper。
- 重开 M5 的 CDF082。
- 架构调整。H8 只提交测量结果和建议，是否改数据表示由用户批准。
- 没有用户决定时修改 AGENTS.md、PROJECT-MODEL-ROLES.md、PROJECT-WORKFLOW-RULES.md、CLAUDE.md 的规则内容。
- 全库换行规范化。

## 5. 边界

- 不读取、不打印、不修改 `.env` 和任何凭据（TQ_USER/TQ_PASS、数据库口令、AK_MCP_TOKEN）。
- 只由用户本人执行：仓库可见性、P0-1 的防火墙与令牌、MCP 服务重启、生产数据库角色切换、Windows 系统设置。你只准备步骤。
- 保持 `enable_trading=False`、observer=`hold`、broker=`NOT_RUN`、真实 send/cancel `0/0`。
- 不修改前几轮外部审核写的文件：`repo-audit-20261025-claude/REPORT.md` 已被钉住哈希；`repo-audit-20261026-claude/` 下的 REVIEW.md、BLUEPRINT.md 与探针作为历史保留。
- 仓库仍是公开的：不把网络暴露的核实细节、私有路径下的会话信息写进仓库。

## 6. 交付与记录

- **每项完成就提交并推送 main**：提交说明写清改动、运行的命令、原始退出码和 passed/failed 数。不再为每一步新建过程文档。
- **原始日志**：压缩后放仓库外，仓库里只放摘要。
- **状态页**：只更新 H1 的那一页，每周覆盖一次。
- **全部完成或被阻塞时停止**，给用户一页摘要，写清 H1–H8 各自的状态、阻塞原因，以及仍需用户决定的事项（A1、G1–G6）。

## 7. 检查点

| 日期 | 应达到 |
| --- | --- |
| 11-15 | CI 全绿；H3、H4 已入库；冷链能一次跑完，T7 能按节点重跑 |
| 11-22 | T7 DAILY 通过；MINUTE profile 已提交 |
| 11-29 | 第一份 RB DAILY 报告入库 |
