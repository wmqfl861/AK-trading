# 当前策略优先规划

生效规划版本：`revisions/r3-001`；日期：2026-10-25；规划已生效；2026-10-25 用户明确恢复开发，当前状态：**DEVELOPMENT_RESUMED**。r3 原始发布记录仍保留当时的 EFFECTIVE_PLANNING_ONLY 状态。

- [蓝图](revisions/r3-001/BLUEPRINT.md)：以核验事实组织正确策略、可复现历史回测与本地 paper；MINUTE/DAILY 各自就绪交付，模拟启动与完整 20 交易日验收分开。
- [实施计划](revisions/r3-001/IMPLEMENTATION-PLAN.md)：七个里程碑的交付、进入条件、必要验证、完成证据及责任角色；只约束实际消费者的依赖。
- [修复顺序](revisions/r3-001/REPAIR-ORDER.md)：22 项核验结论、处理范围与关闭条件；P0-2/P1-2、C05/清单漂移及 P2-7 优先，G 项与大规模变更仍待用户决定。
- [作者处置](revisions/r3-001/DISPOSITION.md)：同一 r3 作者对 F01–F06 的采纳决定及理由。
- [独立 ZCode 计划意见](revisions/r3-001/PLAN-ADVICE.md)与[收据](revisions/r3-001/PLAN-ADVICE-RECEIPT.json)：仅供咨询，不是计划 PASS 门或产品硬审核；评审员 hash/tests/Git 跟踪检查均为 NOT_RUN。
- [外部审计逐项核实](../repo-audit-20261025-claude/VERIFICATION.md)：本版事实基础；22 项中 11 项成立、11 项部分成立，未签发产品 PASS/FAIL。

本版经作者处置确认有效，无需二次整轮计划审。蓝图已经提交推送（`a91a637`）；随后用户明确要求继续开发，并将开发者改为 ZCode。现在按 r3 优先范围并行推进已有计划的实际缺陷修复，逐模块确认真实前置条件和文件归属后立即实施、验证及独立硬审核。仓库可见性、许可证、G 治理及大规模架构变更仍为待决提案，不因恢复开发而自动采纳。

当前会话职责按用户最新直接提供的 AGENTS 指令执行：Astra/max 计划与契约，原生 gpt-6.1-sol/max 子代理开发和修复，独立 ZCode GLM-5.3/max 硬审核。磁盘[项目模型与职责规则](../../PROJECT-MODEL-ROLES.md)和[工作流](../../PROJECT-WORKFLOW-RULES.md)中的旧开发路由尚未同步；本页仅报告当前派工，不修改规则或历史作者记录。计划意见不设 PASS 门，产品接受仍须独立硬审。

[历史 r2 蓝图](revisions/r2-001/BLUEPRINT.md)及其[作者处置](revisions/r2-001/DISPOSITION.md)、r1 原件和既有审核记录均不改写。历史计划 PASS 只保留其咨询性计划结论，不能充当产品信用或今日启动门。策略、数据、风险及验收硬合同继续继承[20261010-r2 研究蓝图](../strategy-research-blueprint-20261010-r2/BLUEPRINT.md)；缺失的本地模块原件按 r3 的 LOCAL_ONLY 清单在实际派发时提供，不能声称新克隆具备全部运行条件。

canonical 377 及活动审核写冻结、单一 writer、R19→R20→R21 等实际消费门不变；`enable_trading=False`、observer=`hold`、broker=`NOT_RUN`、真实 send/cancel=`0/0`。恢复开发和更新路由本身不增加产品硬审、研究、模拟或策略资格信用。

本次恢复开发的具体模块、已完成硬审和剩余问题见[2026-10-25 开发批次状态](../development-20261025-zcode-r3/STATUS.md)；P0-2 replay 与 C05 的窄执行计划及原作者处置位于同目录。


2026-10-28 续接：用户已启用覆盖完整有效蓝图的新 Goal，旧“本批四项完成后停止”不再限制本 Goal。原 r3 作者已发布[可直接执行的续接裁定](revisions/r3-001/CONTINUATION-20261028.md)，[接收记录](revisions/r3-001/CONTINUATION-20261028-RECEIPT.json)保留 native0/receiver1 及已恢复的 SSE 事件。按裁定并行推进 M5 CLI、forward 实际 capture、SC 现存输入和普通 shadow 的真实前驱；既有四项修复继续原工单，不重做已接受工作。377 为历史冻结说明，forward 当前采用已接受 integrated306 的精确378文件闭包，不能把历史 PASS 借给变化后的 main。各项用户专属决定和交易边界保持。

2026-10-28 会话派工补充：本次用户提供的开发角色为原生 `gpt-6.1-sol/max` 子代理；后续派工按该明确指令和显式移交执行。磁盘旧路由正文尚未同步，历史作者文件及审核事实保留；独立 ZCode 硬审核不变。M5 CLI 两文件已接受并集成，详见当前 Goal 的 STATUS 与 ACCEPTANCE。


2026-10-28 17:26 UTC 进度：天勤认证已经实际成功；Forward 当前阻塞是超时收尾，其540+60秒修复完成58项自测并进入原独立 ZCode 复审。M5 的正式审核被审核 helper 的 common 作用域错误中断，正在原审核会话修复；DISPATCH 修复精确历史映射，T7 处理分钟日历热点，N8 准备完整双流程验证。三路独立 ZCode 并行，具体原始结果和限制见[Goal 状态](../development-goal-20261026/STATUS.md)。这些局部进展不代表完整策略或模拟交易验收通过。


Forward003 采集监督/收尾修复已获独立无条件 PASS：58项完整测试、13项独立验证通过，CLI/outer均0且冻结输入未变。详见[接受记录](../development-goal-20261026/forward-runtime-hard-review-002/ACCEPTANCE.json)。该结论不代表在线采集或模拟交易验收完成；其他既有修复继续。


SC 日期标签与研究可用时点政策已由原 Astra 作者处置独立建议并正式生效，见[最终政策](implementation-clarifications/backtest-input-delivery-20261015-r1/SC-POLICY-FINAL-DISPOSITION-20261028-001.md)。这不补齐实际输入：A仍为0、INPUT_NOT_DELIVERED，缺少的合法合约及历史会话等证据仍须实际取得。T7新F已进入原独立R0；其余既有修复按当前Goal状态并行推进。


2026-10-28 20:00 UTC：Forward run003现已正常收尾（child/supervisor/Root原生0，568.469秒，API已关闭），但窗口内只有bootstrap与CLOCK，真实新OPEN/CLOSED均0，因此尚未开始forward capture或模拟账户。T7新F/R0/L0齐备，5项affected最新覆盖全部通过（含一次Windows短路径单项重跑）；24项完整payoff验证正在实际运行。M5、N8、DISPATCH保留真实失败并继续原模块修复，尚未取得完整产品PASS。详见[当前Goal状态](../development-goal-20261026/STATUS.md)及[真实采集结果](../development-goal-20261026/forward-capture-run003/RESULT.json)。

2026-10-28 21:13 UTC 最新状态：M5审核工具r18已33/33通过并获原独立ZCode的无条件限定PASS，正式007尚待重任务时段及资源准入；T7完整payoff仍实际计算。N8保留085准备中断并由原作者处理，DISPATCH由原Sol继续有界性能测量与修复。上述工具PASS不构成完整策略、正式历史报告或paper验收。详见[当前状态表](../development-goal-20261026/STATUS.md)。
