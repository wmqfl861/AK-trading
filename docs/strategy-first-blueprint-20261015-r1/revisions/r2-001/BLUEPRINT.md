# 策略优先蓝图：审后修订 r2-001

日期：2026-10-15。作者：原 Astra/max 计划作者。关联：[实施计划](IMPLEMENTATION-PLAN.md)、[修复顺序](REPAIR-ORDER.md)、[审后处置](DISPOSITION.md)。

本版追加修订[冻结 r1](../../BLUEPRINT.md)的排程与模拟边界；旧正文、复评和原 planning PASS 保留。用户已授权本次蓝图排程修订，不再就同一偏好确认。**产品开发原 pause 仍在；本版只发布规划，恢复产品工作另等用户指令。** r2 的实质修正交一次必要窄复审，不借此重开未变计划的审核。

## 1. 先交付可复现策略证据

首选 RB-A/B-1：同一合法自然来源下，MINUTE 与 DAILY 各自训练、校准、回放和报告，首批为 TREND/BREAKOUT/PULLBACK。**任一频率满足自身输入、代码、计划和运行条件后立即运行并交报告，MINUTE 不等 DAILY seam，DAILY 不等 MINUTE。** 双频总批完成仍要求两份实际报告齐备；只有一频率结果时如实报部分交付。

按[旧蓝图 §3](../../../strategy-research-blueprint-20261010-r2/BLUEPRINT.md)的事前来源、覆盖、成熟尾段、日历/费用、可用性和既往使用规则选择具体合约或合法序列，不看收益挑 RB。RB 不足则保留其 BLOCKED/INSUFFICIENT，按同一规则考察其他五品种；RB 是首选，不是无限等待条件。换品种时记录实际品种、来源与独立批次，不把不同品种结果拼成同源 RB A/B。

交付内容是实际输入身份、预测与校准、payoff 观察、准入漏斗、四政策连续账户、六场景结果及不足/失败原因。先复用现有离线账本和模拟入口，逐频率交付研究证据及可消费接口，不另建模拟平台，不承诺收益或完成日期。

## 2. 保留原合同，不要求全量笛卡尔积

范围与门槛继续引用[旧蓝图 §1、§3–§5](../../../strategy-research-blueprint-20261010-r2/BLUEPRINT.md)、[旧实施计划 R1–R5](../../../strategy-research-blueprint-20261010-r2/IMPLEMENTATION-PLAN.md)及其原 M1–M5 模块计划：

| 合同维度 | 保留内容与首批执行方式 |
| --- | --- |
| 六品种 | SHFE.RB、SHFE.CU、SHFE.AU、DCE.M、DCE.I、INE.SC；未运行项保留 NOT_RUN/BLOCKED/INSUFFICIENT。 |
| 七入口族 | TREND、BREAKOUT、PULLBACK、REVERSION、GRID、有限 MARTINGALE、WAIT/CASH；首批只跑前三族，保留现有 14 个 StrategySpec 命名。 |
| 八预测期限 | 分钟 1/5/15/30/60 交易分钟；日线 1/5/20 交易日；独立建模、校准和评价，原 duration 集合保留。 |
| 四政策 | BEST_EV、同一合格集的 PROBABILITY_SAMPLE、事前固定真实 TREND 的 FIXED、CASH；固定候选、风险预算、seed 和合法 LEGACY 绑定。 |
| 六场景 | BASE/FEE_X2/SLIPPAGE_X2/MARGIN_X1_5/GAP/NO_STOP；压力结果不回写基线候选，零组保留。 |
| 时间与证据 | 每 fold 252/60/60、PIT、成熟度、purge/embargo、相关合约同日分区、连续账本与永久留出保护。 |

首批使用现有 `walk-forward`，`split_specs.final_holdout=false`、`RunPolicy.purpose=RESEARCH_EVALUATION`、真实输入 `synthetic=false`。最终 holdout 是正式候选冻结后的另一个阶段，不能因报告含留出字段而提前 reserve/begin/consume。普通及马丁的概率、校准、经济、风险门沿原合同，不改阈值、不加 epsilon、不以腿数或相关合约凑独立样本。

每频率四政策×六场景为原定 24 单元，双频合计 48；其中四成本场景完整重放，GAP/NO_STOP 按原 BASE cohort 压力评价。六品种、七族、八期限保留状态矩阵，不要求把它们与全部退出再做全量笛卡尔积；后续退出按原阶段扩展。

## 3. 最短队列及真实依赖

恢复产品工作后，在不相干写集上并行推进：

| 队列 | 最近交付 | 只保留的实际依赖 |
| --- | --- | --- |
| 数据与缺口 | 分频率覆盖、成熟度、PIT/规则、source/lock 和缺口清单 | 所选自然源的原 M1/M2/R1 契约与接受状态。 |
| 必要代码/性能 | 只修所消费路径的缺陷并完成原必要验证 | 对应模块有效计划、精确 lease、唯一 writer、活动审阅写冻结。 |
| 策略回测报告 | 频率 ready 即真实运行、读回并发布该频率报告 | 该频率所消费输入/代码已接受及实际运行 admission；不等另一频率。 |
| 模拟消费准备 | 现有账本/出口映射、NO_TRADE、feed/clock 记录准备 | 所消费接口、代码和运行权限；共用 M5 文件顺序接续。 |

公共源合版仅在选定消费者确实需要时，沿原 M1 计划和已规定的集成顺序完成；不把候选 P2/full100 支撑验收变成所有路径总门。G3→G4→G6 仍按实际消费者绑定。APUB、EX06、通用 runtime、远端宏观、全品种、部署和真实执行适配器均不作共同前置；对应客户端或执行路径真正消费它们时才保留依赖。

scoped DAILY 修复已有，应复用已修字节及 scoped 审查，闭合消费所必需的 work-order/relay 接受事项；不重新开发 seam。其 `TRADING_DAY20/ATR_1`、时效和 UNKNOWN 语义不变，未闭事项只阻相应 DAILY 消费路径。

## 4. 分清开始模拟、完整影子验收与真实订单

| 活动 | 可以开始的条件 | 完成/禁止边界 |
| --- | --- | --- |
| 历史回放、本地虚拟撮合 | 现有离线入口的所消费代码、输入及运行权限就绪；用途为 RESEARCH_EVALUATION。 | 可先产研究支持、失败或不足；虚拟成交与真实 send/cancel 分列，真实 broker 禁止。无需先取得 20 天记录或实盘 UI 授权。 |
| 前瞻 paper/shadow 记录 | 所消费代码/输入、feed/clock 接口和实际运行权限具备即可开始收集。 | 不等待尚未产生的 20 天记录；正式 SHADOW_OPERATIONAL 统计 FAIL/INSUFFICIENT 仍 NO_TRADE，未知/过期/OOD/冲突不得新增风险。 |
| 完整 SHADOW_ACCEPTED | 满足原正式研究、所需代码接受和真实前瞻证据。 | 原 20 交易日、品种/会话完整、按时率≥99.5%、P99≤5秒、>120秒禁新仓、UTC误差≤100ms 等是完整验收门；synthetic/replay 不替代真实记录。 |
| 真实订单/撤单 | 依 AGENTS §4：execution-domain hard review PASS；部署及模拟验收真实 send/cancel=0；用户明确授权并在客户端 UI 开启交易。 | native adapter 还须通过自身 plan→develop→review。以上是实盘动作门，本规划不授予任何真实交易权限。 |

全程保留 `enable_trading=False`、observer=`hold`、真实 broker=`NOT_RUN` 和真实 send/cancel=0，直至独立满足原实盘许可。为此只澄清业务边界：技能中 TqSim 是本地模拟、TqKq 是远端模拟；本版不引入 SDK 迁移，不调用 SDK/账户，不改配置，不操作客户端。

## 5. 状态、验收与停止

已有 RB 局部硬审只证明其原范围的 SCENARIO/CALENDAR_UNVERIFIED；M5 full100 未运行。研究报告使用 RESEARCH_PASS、STATISTICAL_FAIL、INSUFFICIENT、BLOCKED、ENGINEERING_FAIL/NOT_RUN，并分别记录工程硬审 verdict、统计状态和运行状态。**硬审 PASS 不等于策略有效；有效路径的统计 FAIL/INSUFFICIENT 也不自动等于产品硬审 FAIL。**

输入漂移、非法 source/lock、PIT 或账本身份错误、必要代码未接受、活动写冻结、真实硬依赖或缺少运行授权时，只停止相应消费者。合法样本不足保留原状态；工程结构缺陷退所属 owner。未知即不新增风险，已有仓位按原减仓/退出合同处理。

代码变更完成原验证并冻结实际写集后立即独立 ZCode 硬审；FAIL 回同一 owner 追加修复，重冻后独立复审。未变合同不加第二轮规划，不以非阻断观察增加审批门。原开发暂停、本轮仅规划、旧证据只读三项继续生效。
