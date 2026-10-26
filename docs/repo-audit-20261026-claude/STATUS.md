# 第二轮外部审核：阶段 0 状态

基点：`main@891e4de`；已执行 `git pull --ff-only origin main`（原始退出码 0），并确认 `891e4de` 是 main 的祖先（原始退出码 0）。`REVIEW.md`、外部 `BLUEPRINT.md` 与探针仅作为建议/复现输入；有效计划仍为 r3-001，其原 Astra 作者负责本次增补与意见处置。

## 当前交付

| 项目 | 状态 | 说明 |
| --- | --- | --- |
| A2 / N1 | **ZCode 正在实施** | 原作者已处置独立意见；十文件写集，独立候选，修复后立即硬审 |
| A3 | **文档同步完成** | README 更新 C05 的真实结果；r3 实施计划六处角色文字同步；CLAUDE 两处按用户确认修改 |
| A4 | **方案及修复顺序完成；产品 NOT_IMPLEMENTED** | 原作者选择 CLI 提前明确拒绝 shadow-replay；本批不实施完整 shadow、不关闭 P0-2 shadow/P1-2 |
| B1 | **分诊完成，234/234 校验通过** | [机器清单](TRIAGE.json)及[简报](TRIAGE.md)；原失败保留，未修改产品或测试 |

## 运行记录

| 时点/输入 | 命令（仓库根目录，Git Bash） | 原始退出码 | passed / failed / skipped / errors | 结果 |
| --- | --- | --- | --- | --- |
| 修复前，891e4de，2026-10-26 UTC | `python scripts/test_offline.py -q docs/repo-audit-20261026-claude/probes/test_range_duplicate_bar_probe.py` | **1** | **3 / 6 / 0 / 0** | 与外部预期一致；9 selected，pytest 1.91s；保留原失败 |
| 修复前，891e4de | `python scripts/test_offline.py -q tests/test_v2_m[1-4]*.py` | **1** | **852 / 14 / 0 / 0** | 866 selected/ran；0 collection errors；1683.875 秒；14 个失败均在 `test_v2_m4_payoffs.py`，修复后逐项比较 |

C05 历史全量结果（供 B1 分诊，不是本轮重新执行）：`python -B -X utf8 -u scripts/test_offline.py -q`，原始退出码 **2**，6,015 selected/ran，5,754 passed、171 failed、30 skipped、60 errors，另 3 collection_error_files。原始记录与逐项 nodeid 保留；分类意见不会改变这些历史结果。

## 待用户决定与操作

- A1：仓库可见性与 P0-1 的用户操作/完成确认。公开仓库只记录完成状态，不记网络暴露核实细节。服务重启、生产数据库角色切换仍由用户本人执行。
- A5 / F1–F6：角色稳定期、并行上限、失败重试、证据存放、事实来源、状态页频率；均未采纳为规则。
- B3 分支保护、B4 换行规范化、B6 Python 支持版本；均未实施。
- 用户已明确同意 [CLAUDE 两处同步提议](RULE-CONFLICTS.md)，Root 已执行；没有采纳 F1–F6，没有改 AGENTS.md 或 PROJECT 规则。

安全边界：`enable_trading=False`、`observer=hold`、`broker=NOT_RUN`、真实 send/cancel `0/0`。未读取、打印或修改凭据；旧 REPORT.md 未改；仅按用户新增确认同步 CLAUDE.md 两处，其他规则文件未改，未做全库换行规范化。

提交与推送记录在各项完成后追加；独立硬审结论与开发者自测分别记录。

### B1 校验与调用记录

- OpenCode 默认配置辅助分诊：原始 CLI 退出码 0，无失败工具，输入未变。
- `python -X utf8 C:/AKWorktrees/aktr-round2-20261026/validate-published-b1.py`：原始退出码 0，234 条与原始失败集合完全对应；分类为环境/工件依赖 171、哈希/接受链 49、产品缺陷类别 10、时间敏感 1、未定 3。分类带置信度，不能直接计为已修复或独立产品 PASS。
- 发布时修正辅助报告对空 stderr 的术语：EMPTY_STDERR；NO_MATCH 仅适用于可选 rg。原始辅助产物保留。

### 协调异常（不计入产品测试）

原 Astra 调用原生退出码 0、接收器退出码 1：一次流中断后 CLI 重连完成，最终正文与最终会话消息一致，0 失败工具、输入不变。保留接收器原结果，将文本作为草案送意见，并要求原作者在意见处置时确认有效版本。Root 随后的计划意见准备脚本因接收器非零退出 1，又错误触发了缺少 REQUEST 的检查（退出 1、ZCode 启动 0 次）。后继调用先检查准备命令原始退出码和完整 REQUEST，再启动，现已实际运行。未重跑基线、未覆盖旧失败、未改变任何模型配置。

### 已推送提交

- `c5185e3`：B1 的 234 条分诊、简报及修复前测试状态；`git push origin main` 原始退出码 0。

### 有效计划与 A3/A4 交付

- r3-001 继续有效；[阶段 0 增补](PHASE0-PLAN.md)与[原作者最终处置](PHASE0-DISPOSITION.md)共同生效，处置优先。[独立计划意见](PLAN-ADVICE.md)仅为建议，未当作产品判决或计划 PASS 门。
- 意见 F1–F6 是此次计划意见编号，与外部 BLUEPRINT 的治理 F1–F6 不同；治理提案仍待用户决定。
- A4 选择在 CLI 参数关系检查后、任何输入/输出/训练之前拒绝 shadow-replay，保留 StageIncompleteError 原生退出码 3；未来写集/验证已写明。本次只交付方案，产品缺口未关闭。
- A3 按原作者文案完成 README 状态更新及 r3 实施计划六处角色替换；没有更改验收条件。CLAUDE 两处已获用户确认并于 c151d5c 推送，推送原始退出码 0。
- 修复前候选 M3B：首次 209 passed / 3 skipped，raw 0，12.609s；确认缺少历史工件后，提供 31 个只读原字节输入文件（568,809 bytes），同命令重跑得 **212 passed / 0 skipped / 0 failed / 0 errors，raw 0，13.203s**。未放宽跳过条件或复制完整研究数据。
- 原作者处置调用 native 0、receiver 1：首次只读定位错误 raw 2，随后读取正确固定输入，最终文本明确纠正并确认 EFFECTIVE。原错误保留，不重开计划轮次。owner 首次启动前 watchdog 因混合测试/CLI 目录格式报 raw 1、CLI 0 次；独立 CLI 记录父目录后实际启动，无配置/监督器代码变更。

- 927f97f：A3 的 README/r3 角色同步，以及 A4/阶段 0 有效增补与作者处置；推送原始退出码 0。README 原有混合换行的未改内容随后恢复原字节，避免文案编辑连带产生格式差异。
