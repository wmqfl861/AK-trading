# 策略优先蓝图：核验驱动修订 r3-001

日期：2026-10-25。状态：**EFFECTIVE_PLANNING_ONLY，当前有效规划**。作者：本次显式接任并续接的 Astra/max 蓝图文档作者。配套：[实施计划](IMPLEMENTATION-PLAN.md)、[22 项处置与修复顺序](REPAIR-ORDER.md)、[计划意见处置](DISPOSITION.md)。

本次交接仅限蓝图文档作者职责：历史 r2 作者不在可续接的 live collaboration 列表，历史署名、模块合同作者和产品 owner 均保留。本 r3 作者已完成独立计划意见处置，[CURRENT](../../CURRENT.md)与 README 已同步指向本版；Root 按用户已明确授权提交推送。**本次请求仅为更新蓝图并推送，不授予新的开发、研究、测试、服务、部署或交易启动权限，也不自动恢复暂停任务或撤销历史已授权任务。** 规则、源码、测试和冻结候选不变。

## 1. 事实基础与此次改变

主要依据为本次随文发布的 [VERIFICATION](../../../repo-audit-20261025-claude/VERIFICATION.md)。[REPORT](../../../repo-audit-20261025-claude/REPORT.md)提供外部静态建议，不是计划或产品验收权威。已完整读取的 [r2 蓝图](../r2-001/BLUEPRINT.md)、[实施计划](../r2-001/IMPLEMENTATION-PLAN.md)、[修复顺序](../r2-001/REPAIR-ORDER.md)和[作者处置](../r2-001/DISPOSITION.md)保留历史；本版更新事实、修复优先级与交付条件。

| 已核事实 | 对排程的含义 |
| --- | --- |
| 核验时点基线 main `8ac6c71f72e8aca08c6d2b7531152d81e761a27d`；当时相对 `b98ef21` 仅新增 REPORT。本批另纳入 VERIFICATION、r3 规划/处置及意见材料，并更新导航。22 项中 11 项成立、11 项部分成立。 | 不把外部建议全盘实施，也不把“部分成立”记成已修复；逐项处置见修复顺序。 |
| C05：`python scripts/test_offline.py -q` 原生退出 2，174 个收集错误，未执行任何测试。DP03/DP08 基线漂移与导入污染形成级联。 | 优先恢复可信测试入口；不是 174 个独立缺陷，也没有当前全套通过率。 |
| C07：T7 清单单项测试原生退出 1，1 failed，11 条 code_drift。 | 与 DP03 清单分别核对接受版本；禁止直接刷新哈希、删断言或加排除获得假通过。 |
| P0-2：replay/shadow 缺 `exit_registry_sha256`、`exit_binding`；P1-2：`_reg` 未定义及 ledger 调用缺参。 | 立即进入规划关键队列，修复实际消费闭包；空资格测试、预期 NameError 和最小复现均不足以证明可用。 |
| 当前外部 M5 候选也未修复该接线；SHCP7 仅有 replay 局部修补，ordinary shadow 仍不完整。canonical 377 写冻结有效。 | 不整包照搬、不合并未接受候选；reviewer CLI 原生退出 1 不是产品判决，更不是解冻依据。 |

[GitHub 快照](../../../delivery-20261024/GITHUB-SNAPSHOT-20261024.md)仍限定外部 M5、DISPATCH、N8 和 forward 的交付信用；仓库外未合入修复不算当前源码已修复。既有 RB 真实价格研究仅有其原范围的 SCENARIO/CALENDAR_UNVERIFIED 结论；正式策略有效性仍未建立。M/DCE 工程接受不等于研究输入已交付。以上沿用已核证据，本轮未重新审计源码或运行验证。

## 2. 目标与不变的策略合同

主线是**正确的策略实现 → 可复现的历史回测 → 本地 paper 模拟**。每一步交付可核对的产物，允许结论为失败或不足。优先 RB；其覆盖、成熟度或规则不足时保留缺口，按同一事前标准考察其余品种，不看收益选源、不无限等待 RB。

硬合同精确继承[研究蓝图 §1、§3–§5](../../../strategy-research-blueprint-20261010-r2/BLUEPRINT.md)及[研究实施计划 R1–R5](../../../strategy-research-blueprint-20261010-r2/IMPLEMENTATION-PLAN.md)。原 M1–M5、变更 007、M3C 阶段约束继续适用；旧文档的角色或“计划须 PASS”措辞按现行规则解释。

| 合同维度 | 保留范围与首批交付 |
| --- | --- |
| 六品种 | SHFE.RB、SHFE.CU、SHFE.AU、DCE.M、DCE.I、INE.SC；具体合约 symbol 保留原大小写，未运行项如实列状态。 |
| 策略与退出 | TREND、BREAKOUT、PULLBACK、REVERSION、GRID、有限 MARTINGALE、WAIT/CASH；保留 14 个既有 m1/d1 StrategySpec。首批三方向族，合法 LEGACY 绑定；M3C 八退出族及 P0→P1→P2→P3→P4 顺序沿原合同，E0 原独立 LEGACY 基准不等待未消费的新退出。 |
| 八预测期限 | MINUTE：1/5/15/30/60 交易分钟；DAILY：1/5/20 交易日。独立训练、校准、评价，保留原 duration 集合，不缩放日线概率冒充分钟预测。 |
| 四政策 | BEST_EV、同一合格集的 PROBABILITY_SAMPLE、事前固定真实 TREND 的 FIXED、CASH；同候选流、同风险预算、固定 seed 和实际 entry×exit 身份。 |
| 六场景 | BASE、FEE_X2、SLIPPAGE_X2、MARGIN_X1_5、GAP、NO_STOP；前四项完整账户重放，后两项评估 BASE cohort。每频率原定 24 单元、双频 48，不新增六品种×七族×八期限×所有退出的全量笛卡尔积。 |
| 时间、统计与风险 | 每 fold 252/60/60、PIT/available_at、成熟尾段、purge/embargo、相关合约同日分区、唯一连续账本及永久留出保护不变。普通/马丁概率、校准样本和日期块、净 EV、CVaR、压力、资金及风险阈值全部沿上述 §4 原值，不降门、不加 epsilon。 |

首批继续用 `walk-forward`、`final_holdout=false`、`RunPolicy.purpose=RESEARCH_EVALUATION`；真实价格输入保持 `synthetic=false`。最终 holdout 只有正式候选冻结后的独立阶段才可能按原规则使用，本批及开发小批不 reserve/begin/consume。已使用区间不得重置为 UNSEEN。

**MINUTE 和 DAILY 各自满足实际输入、代码与运行条件后独立推进、先交单频率报告；双频总批完成仍需两份实际报告。** DAILY 复用已修 scoped seam 和相应接受范围，保留独立 `TRADING_DAY20/ATR_1`、一交易日背景年龄门及 UNKNOWN 拒绝，不重新开发已接受代码。

情景研究可在其原有授权、所消费代码已接受及来源合法的范围内继续；真实价格不自动证明历史费用/日历 PIT。假设成本、重建日历明确标 SCENARIO/CALENDAR_UNVERIFIED，合成验证标 SYNTHETIC，二者均不提供正式策略资格。外部报告举例的成本倍数不自动成为新的研究设定。

## 3. 只保留实际消费者的依赖

首要并行事项是 P0-2/P1-2 的合法接线修复准备、C05 与接受清单核对、P2-7 导出修复；所选频率的数据缺口与报告准备可在不重叠写集上推进。具体进入条件、验证与证据见[实施计划 §3](IMPLEMENTATION-PLAN.md)。本批只写排程，不派工。

replay 窄修复须先由原模块计划方确认兼容源、文件归属和合法隔离范围；本次蓝图交接不转移 M4/SH 合同裁定权。ordinary shadow 仍沿 B0–B8、R19 filewise release → R20 原作者 SH-SCHEMA 裁定 → R21 SH-M1 冻结，随后实现及硬审。该链只约束消费它的路径，不作为所有 walk-forward 的总门。

M5 当前 owner 为 `/root/sol_m5_existing_recovery_20261023`，共享 `experiments.py` 始终只有一个 writer。M4、R-SHADOW 当前实际 actor 尚待 Root 绑定或显式交接；forward owner 仅拥有其 `feed.py/test_feed.py` 写集，不代修 replay/shadow。canonical 377 及所有活动审核写集保持冻结；隔离候选、重切审核范围和释放必须有原规则下的具体记录，不由 CLI 失败、本文或旧 scoped PASS 自动授予。

G3→G4→G6、M1 公共增量、snapshot/full100 及 EX06 等只约束实际选择的消费者。已接受且兼容的其他路径不等待未使用候选；所选候选原必需人口、预算和集成顺序也不得省略。SQL、依赖、端口、CI 是范围明确的支撑修复，不集中升级为“先完成全部平台再做策略”的门槛。

P0-1 的防火墙、云端入口、令牌与服务动作由用户亲自决定和执行，与离线工作并行；其完成不阻断使用合格冻结文件的离线回测。G-1～G-3 以及大规模架构、模型、精度、存储或治理改变均为待用户决定的提案，未获本批实施授权。

## 4. 本地模拟启动、正式验收与实盘分开

| 活动 | 进入条件 | 完成证据与边界 |
| --- | --- | --- |
| 历史回放／本地虚拟撮合 | 所消费代码与输入已接受，原运行权限、成本及账本身份就绪。 | 用既有离线引擎交研究结果及可复现账户；无需先有 20 天前瞻记录或实盘 UI 授权。 |
| 前瞻 paper/shadow 开始记录 | 自身代码、输入、feed/clock、实际运行权限就绪；只在消费 ordinary shadow 时满足其闭包依赖。 | 可以开始积累真实记录；统计 FAIL/INSUFFICIENT 的 SHADOW_OPERATIONAL 保持 NO_TRADE，未知、过期、OOD、冲突不增加风险。 |
| 完整 SHADOW_ACCEPTED | 原正式研究资格、所需代码接受及真实前瞻证据齐备。 | 原 20 交易日、各品种/会话完整、按时率≥99.5%、P99≤5秒、>120秒禁新仓、UTC 误差≤100ms 等全门满足；synthetic/replay 不替代真实记录。 |
| 真实订单／撤单 | 继续执行 [AGENTS §4](../../../../AGENTS.md)及 native adapter 自身 plan→develop→review。 | execution-domain 硬审、部署/模拟验收、用户明确授权和客户端 UI 开启均不可省略；本版不进入此阶段。 |

全程 `enable_trading=False`、observer=`hold`、broker=`NOT_RUN`、真实 send/cancel=`0/0`；虚拟成交另列。若以后明确选用 TqSdk，本地 TqSim、远端 TqKq 与真实账户严格区分，在线数据获取也不是离线验证。本批不引入 SDK 迁移或账户操作。

## 5. 发布与可核对的状态

每次交付分别报告工程实现/独立硬审、研究统计、模拟运行三个轴。有效路径的 STATISTICAL_FAIL/INSUFFICIENT 不等于产品工程 FAIL；产品 PASS 也不证明策略有效。StageIncomplete、空训练模型或缺 payoff 观察不能改称完整的 OOS 全现金结果。未执行的项目保留 NOT_RUN，缺证据保留 BLOCKED/UNKNOWN，不虚构总进度百分比。

本批未产生新的工程修复、研究或模拟信用。独立 ZCode 的[计划意见](PLAN-ADVICE.md)已由本 r3 作者逐项[处置](DISPOSITION.md)，本版确认为有效规划；**计划不要求 PASS，本次无需第二轮计划审核**。[收据](PLAN-ADVICE-RECEIPT.json)保留评审输入和执行限制，不将计划意见算作产品硬审。Root 将 VERIFICATION 与本批文档一并提交推送；此授权不包含改变仓库可见性、许可证或历史。后续已授权产品工作仍按[模型职责](../../../../PROJECT-MODEL-ROLES.md)和[工作流](../../../../PROJECT-WORKFLOW-RULES.md)执行：Sol `gpt-6.1-sol/max` 实施，独立 ZCode GLM-5.3/max 硬审实际冻结成果，FAIL 回原 owner。

GitHub 的主要知识入口使用本版相对链接。首次克隆能读取规划与已发布核验，不能据此声称具备本机全部合同、行情或外部候选；`LOCAL_ONLY / NEEDED_AT_DISPATCH` 清单见[实施计划 §6](IMPLEMENTATION-PLAN.md)。[README](../../../../README.md)与[PROJECT-STATUS](../../../../PROJECT-STATUS.md)保留其日期与历史，旧角色和测试计数不能覆盖现行规则及 C05/C07 新事实。待用户决定的精确事项集中列于[修复顺序 §4](REPAIR-ORDER.md)，本批不改全局规则。
