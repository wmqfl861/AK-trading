# 第二轮外部审核：阶段 0 状态

基点：`main@891e4de`；已执行 `git pull --ff-only origin main`（原始退出码 0），并确认 `891e4de` 是 main 的祖先（原始退出码 0）。`REVIEW.md`、外部 `BLUEPRINT.md` 与探针仅作为建议/复现输入；有效计划仍为 r3-001，其原 Astra 作者负责本次增补与意见处置。

## 当前交付

| 项目 | 状态 | 说明 |
| --- | --- | --- |
| A2 / N1 | **独立硬审无条件 PASS，主树集成通过** | 已接受十一文件；外部探针 9/9、M3B 308/308，完整 962 中 948 passed / 14 同节点同因既有 failed / 0 errors |
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

### A2 实施及必要契约澄清（2026-10-26 UTC）

- 首版开发者实测：专项 `python scripts/test_offline.py -q tests/test_v2_m3b_duplicate_bar.py tests/test_v2_m3b_martingale.py` 为 **110 passed，raw 0**；原样外部探针 **9 passed，raw 0**；完整 M3B **308 passed，raw 0**。新增永久测试 96 项，原 M3B 212 项全部保留并执行。
- 首版完整 `python scripts/test_offline.py -q tests/test_v2_m[1-4]*.py`：**raw 1，962 ran，922 passed、14 failed、26 errors、0 skipped**，1736.609 秒。十四项为原基线同节点历史绑定比较失败；26 个新错误不能豁免，均来自 `natural_nonempty_range_case`。保留该失败版本。
- [原 Astra 作者的生效裁定](A2-M4-CONTRACT-RULING.md) 保留到期清零规则，只追加 M4 正例夹具的局部时序与边界控制写权。旧夹具第三次决策晚于期限 **1 秒**（旧诊断把单位写成 1ns，已明确纠正，原件保留）；正例改在完成 bar 的边界，另从第二步真实状态测试期限后 **1 纳秒** 必须 WAIT/1。生产期限、十四个历史哈希常量及原节点不变。作者调用 native 0、receiver 0，无失败工具；本文件不是产品 PASS。
- 原 ZCode owner 续接修复，先按 `FACTS.json.error_nodes` 的精确 26 节点运行 `python scripts/test_offline.py -q <26 nodeids>`：**26 passed，raw 0，28.515 秒**。
- 修复后依次重跑：专项 **110 passed，raw 0，10.610 秒**；外部探针 **9 passed，raw 0，4.937 秒**；`python scripts/test_offline.py -q tests/test_v2_m3b_*.py` **308 passed，raw 0，17.406 秒**。以上均无 failed/errors/skipped。
- 本轮完整 M1–M4 于 09:14 UTC 启动，当前仍运行；此时不预写结果。独立 ZCode 正并行准备新增夹具检查及自己的反例，尚未发出成果硬审核结论。
- 42b5a58：恢复 README 未改区域的原有混合换行并同步既有提交记录；推送 raw 0。

### A2 最终冻结与独立硬审启动

- 修复后完整 `python scripts/test_offline.py -q tests/test_v2_m[1-4]*.py` 已实际结束：**raw 1，962 ran，948 passed、14 failed、0 errors、0 skipped、0 collection errors，1644.203 秒**。原 866 项加 96 项永久回归；十四项失败节点与基线集合完全一致，开发者逐项确认仍为历史绑定常量比较。Root 校验了全部原始输出的严格 UTF-8 解码及流哈希；开发者自测不是产品 PASS。
- 原开发会话纠正 M4 编辑造成的未改行换行变化，保留旧版本，证明恢复前后 `splitlines()` 内容一致、所有未改行与原件字节一致；其余十文件未变。恢复只涉及本文件，不实施 B4 全库规范化。
- **09:56 UTC 已启动独立 ZCode 成果硬审**，绑定最终十一文件及三份原作者有效文本；独立重跑 26 / 110 / 9 / 308 / 962 项人口，并执行审核者自己的 15 个反例。审核尚未完成，尚无产品 PASS。
- `1314fc7`：发布原作者 M4 时序裁定与修复过程实测；`git push origin main` raw 0。

### 阶段 0 最终交付

- **A2：独立 ZCode 无条件 PASS（限本项有效计划范围）**。见 [审核报告](a2-review/REVIEW.md)、[有效判决](a2-review/VERDICT.json)、[本次新增接受清单](A2-ACCEPTANCE.json)及[机器运行记录](a2-review/VALIDATION.json)。完整独立复跑 **962 ran / 948 passed / 14 failed / 0 errors / 0 skipped / 0 collection errors，raw 1，1661.078 秒**；十四项与修复前逐节点同原因，历史绑定常量未刷新。
- 独立短项：26 passed / 110 passed / 外部 9 passed / M3B 308 passed，各 raw 0。反例实际参数化为 **17**（准备稿 15 为计数笔误）：首轮 16 passed / 1 failed，raw 1，系审核者对 BREAKOUT 过期后仍 OPEN 的错误预期；原探针和失败保留，另建符合原合同的修订，17 passed，raw 0，未改产品来迎合测试。
- 原判决 JSON 有一处人工转录的 M4 哈希错误，已由**同一独立审核者**另发[更正](a2-review/CORRECTION.md)，旧判决保留。更正值及十一文件/三份计划哈希由程序取值，写后逐项核验；不重复已完成的测试。该错误只在判决字段，测试前后实际文件核对始终一致。
- 最终十一文件原字节集成到 main 后，`python scripts/test_offline.py -q <26 affected nodeids>` 得 **26 passed，raw 0，22.782 秒**；`python scripts/test_offline.py -q tests/test_v2_m3b_*.py docs/repo-audit-20261026-claude/probes/test_range_duplicate_bar_probe.py` 得 **317 passed，raw 0，16.625 秒**。均 0 failed/errors/skipped，集成后所有文件哈希仍与硬审冻结一致。
- **A3 已完成**：README 测试状态、r3 六处旧开发角色文字、用户批准的 CLAUDE 两处同步均已落实。**A4 方案及顺序已完成，产品仍 NOT_IMPLEMENTED**：选择 CLI 提前拒绝 shadow-replay；本次不关闭 P0-2 shadow/P1-2。**B1 已完成**：234/234 条分诊；分类不等于修复。
- 新源代码接受没有改写历史 T7/M5 冻结清单，也不自动接受旧消费者的新身份。之后实际迁移消费者时，仍按原计划重新冻结与接受。
- 待用户本人操作/确认：**A1** 仓库可见性及 P0-1；MCP 重启、生产数据库角色切换仍归用户。待用户决定：**A5 / 外部治理 F1–F6、B3 分支保护、B4 换行规范化、B6 Python 支持版本**。本批未采纳这些治理提案。
- 本批安全边界保持 False / hold / NOT_RUN / 真实 send-cancel 0/0。未读取或修改凭据，旧 REPORT.md 和外部输入未改。阶段 0 交付后停止，不启动 A4 产品实现或新的研究/治理任务。
- `144e6ec`：记录开发者完整回归与独立硬审启动；推送 raw 0。产品交付提交号将在推送成功后追加。
