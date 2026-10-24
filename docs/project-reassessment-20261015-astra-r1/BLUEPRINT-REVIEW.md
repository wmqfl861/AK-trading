# AK-Trading 蓝图与真实交付目标复评（Astra/max，2026-10-15）

**性质。** 这是本次用户授权的 Astra/max 重新评估，不是独立 ZCode 正式计划审核或产品硬审。Goal 仍暂停；本文件只提出可直接转成用户决策的精简修订，不恢复 owner、不启动 CLI 审核、不实施功能、不访问 DB/SDK/网络/凭据、不部署、不交易。

**读取范围。** 读取了 `AGENTS.md`、`PROJECT-WORKFLOW-RULES.md`、`docs/strategy-research-blueprint-20261010-r2/{BLUEPRINT,IMPLEMENTATION-PLAN,REPAIR-BACKLOG}.md`、`docs/probability-strategy-v2-plan.md`、`docs/devplan-v2/00-开发总纲.md`、`PROJECT-STATUS.md`，以及 10 月 14 日 M1/M3/M5/DAILY/RB 交付目录和硬审结果。既有证据按原文件状态引用；未重跑产品命令。`rg` 发现命令 raw exit=0、stderr 为空；本次没有把可选空结果当验收。涉及的维护规则仍以 `docs/maintenance/20261004-rg-no-match-r1/INCIDENT.md` 和 `docs/maintenance/20261004-grok-headless-cancel-r1/INCIDENT.md` 为准。

## 结论先行

r2 蓝图的研究方向是对的，真正的问题是它仍然容易被解释为“先把依赖平台和审计链全部收齐，再给用户第一份策略证据”。当前最短路径应改成：**先形成一个同源、可接受、可复现的 RB 双频 M5 A/B 真实结果；其余模块只在它是实际消费者的硬依赖时阻断。** 这不会降低 PIT、留出、成本、风险、连续账本或独立硬审标准，也不会从首版六品种和七类入口中删除任何范围；只改变调度和里程碑的含义。

现有 RB daily capital-stress、scenario-compare、causal-regime 的局部产品硬审均为无条件 PASS，causal-regime 真实运行有 51 个账户、7,590 条 ALL 归因行、4,785 条 PRIMARY 行和 612 个组行。这些报告同时明确 `SCENARIO`、`CALENDAR_UNVERIFIED`、`broker=NOT_RUN`、`enable_trading=False`，所以它们是工程/账本证据，不能升级为 v2 统计资格、PIT 或策略有效结论。

给用户的直接决策文本应是：**“优先完成 RB 双频真实 A/B；其他工作只在它是 A/B 的硬依赖时进入主线；结果无论 PASS、FAIL 还是 INSUFFICIENT 都形成第一份可用研究证据。”** 这项决定不会承诺收益，也不会提前开放交易。

若当前没有一棵可接受的同源 RB source/lock，唯一诚实出口是 `BLOCKED_BY_SOURCE`，然后修 M1/M2；不能用已有三项 RB SCENARIO 产物占位。若 source/lock 已闭合，则不应再等待无关平台统一收尾。

## 1. 应保持的蓝图部分

1. **原策略范围保持不变。** RB/CU/AU/M/I/SC 六品种，分钟和日线分别建模、校准、评价；分钟 1/5/15/30/60 交易分钟，日线 1/5/20 交易日；不能把日线概率缩放成分钟概率。
2. **七类入口保持不变。** TREND、BREAKOUT、PULLBACK、REVERSION、GRID、有限 MARTINGALE、WAIT/CASH 及既有 StrategySpec 命名继续作为首版合同；不能因首批只跑三族而悄悄删掉其余族。
3. **风险和研究底线保持不变。** 真实可见性、purge/embargo、永久留出、六种成本/压力情景、最小整数手、整轮预算、保证金缺口、单一连续账户账本、相关合约去重、未知即不新增风险、`observer=hold`、`enable_trading=False`、`broker=NOT_RUN` 继续有效。
4. **证据分层保持不变。** 工程 PASS、计划 PASS、局部硬审 PASS、SCENARIO、统计 PASS、影子接受和实盘许可必须分开；全现金、零成交、第一条 observation、静态容量或 planning-only PASS 都不构成统计 PASS。
5. **简单基线优先保持不变。** 先 legacy 以及明确的三族 A/B，再做退出 P1、E0 原有回归/网格/有限马丁，最后才是 P2/P3/P4。不能以新退出选择器或复杂因子扩展来替代第一份真实基线。
6. **交易安全和审阅纪律保持不变。** 计划通过不计产品完成；实际源冻结后才硬审；FAIL 回同一 owner append-only 修复；活动审阅写集冻结；原失败证据只读保留。

## 2. 现状必须改写的状态、里程碑和依赖

下表是需要写回蓝图/实施计划的最小事实校正。它不把局部 PASS 拼成全节点 PASS。

| 项目 | 当前真实状态 | 蓝图修订建议 |
| --- | --- | --- |
| M5 A/B | `m5-ab-experiment-plan-20261014/PLAN.md` 仍为 `PLAN_REVIEW_PENDING`；MINUTE/DAILY 真实 fit、payoff observation、连续账户和统计结论尚未产生。 | 将“首批 A/B”里程碑改名为“**首份真实 M5 A/B（RB 双频）**”，出口必须包含真实命令、全 required 人口、完整状态矩阵和独立成果硬审；在此之前只能写准备/阻断。 |
| full100/快照内存 | `SNAPSHOT-MEMORY-HANDOFF-002.json` 明确 full100 rerun、product hard review 为 `NOT_RUN`。当前 25,679 population 的 P1 22 项通过、P2 300 秒超时，已知 elapsed 308.719 秒、peak 121,106,432；这是算法/复制路径的计算瓶颈，**不是 OOM**。 | 先做有界 profile 和降低逐元素复制/重复展开的实现修复，再保留 2 GiB、1 GiB、3,600 秒原合同重跑；禁止只增 timeout、改人口或删断言。 |
| M1 P1 | isolated candidate 与 snapshot/M3C 版本不是自然同一接受树；自测仍有固定 41/42 计数问题，exact set 还缺 `test_v2_m1_p1_open_contracts.py`；正式 hard review 未跑。 | 状态写为 `CANDIDATE/PARTIAL_NOT_ACCEPTED`。先选择一棵自然合版源，补 exact test set 和真实计数，再做 M1 冻结/硬审；只改计数不能闭环。 |
| RB 三个局部实验 | capital-stress、scenario-compare、causal-regime 均有局部独立硬审 PASS 和真实产物；causal 的 51/7,590/4,785/612 数字已核实于 verdict。 | 状态写为 `ENGINEERING/PER-PLAN PASS; SCENARIO; CALENDAR_UNVERIFIED; NO V2 QUALIFICATION`。它们可作为 M5 输入/账本回归参考，不能作为历史 PIT、收益筛品种或策略资格。 |
| RB chronological-transfer | 10 月 14 日计划和 review-route prepare 存在，尚未完成计划审核或开发。 | 列 `PLAN_PENDING`；不把它放在 RB A/B 之前，除非 A/B 的实际 accepted input 明确需要该转移。 |
| DAILY seam | 六文件有 scoped 独立 PASS；workorder 因 follow-up verdict schema/native3 与两次已纠正 Bash 失败仍为 `INCOMPLETE_NOT_ACCEPTED`。 | 将代码状态与流程状态拆开：保留 scoped PASS，流程包重跑后才允许消费；不把流程失败写成代码 FAIL，也不把 scoped PASS 写成全链通过。 |
| M3A | `m3a-p0-plan-20261014/PLAN.md` 的关键阻断是 M2 accepted current-lock 未闭。 | `BLOCKED_BY_M2_LOCK`，不得以旧 lock、静态因子 PASS 或 planning-only 证据放行；M2 lock 闭合后立即接 M3A。 |
| M3B | P0 计划已写，仍等待 M1 有效锁/来源门。 | `PLAN_READY_WAIT_M1`；M3B 可做只读 prepare/test skeleton，但不得从另一棵树写产品。 |
| M3C P1 | 有局部准备/验证，完整 G3/G4 集成接受未闭；M5 selector 尚未形成。 | `PARTIAL_PREPARE`；legacy A/B 不等待 selector，P1/D 研究等待真实接线和合格输入。 |
| R1 风险/合同 | literal 来源规则 hard PASS 只证明文本/来源，不证明 2023 可用 A。CU 全合约生命周期仍缺 accepted source、calendar、A、完整 T/R。 | R1-archive 继续 `UNKNOWN/NOT_ACCEPTED`；CU 保持 blocked/preparation，不得用文字 PASS 或 SCENARIO 补历史资格。 |
| RUNTIME-R5 | 窄 schema 计划 review 已 native 0、无条件 PASS、`requires_plan_update=false`；这是计划信用，不是产品修复或运行接受。 | 标为 `PLAN_PASS_NO_PRODUCT_CREDIT`，从 RB A/B 硬依赖图移出。 |

## 3. 应删除的串行门和必须保留的硬门

当前不合理的是把平台完成度当成研究消费者的通用前置。下面这些不应阻断离线 RB A/B：APUB 实时发布、EX06 部署/管理工具、未消费的通用 runtime、完整 realtime feed、远端宏观 producer、客户端 UI、执行适配器、其他五品种的全链接受、M3C P2–P4 和 RUNTIME-R5 产品接线。它们仍保留各自合同和失败状态，排程上延后即可。

RB A/B 仍然必须通过以下硬门：

- 一棵同源、字节冻结的 RB accepted input/source tree；不能把 isolated M1、snapshot M1、M3C P1 的绿色文件拼装。
- 当前有效 contract lock、calendar/availability、fees/margin/multiplier/tick/limit/capacity、payoff plan、StrategySpec、LEGACY binding 和 source identity 全部一致。
- M1/M2 的实际消费者输入已接受；M2 lock 不是只读文本存在，而是 current-lock 闭合。
- M3A 概率、M3B 三个首批入口、M4 natural continuous ledger 的真实接口可运行；DAILY 必须使用独立 `TRADING_DAY20/ATR_1`，缺失/过期仍 UNKNOWN。
- M5 计划有效、required 测试人口完整、walk-forward 每 fold 的原合同窗口仍为 252 train/60 calibration/60 rolling；29 blocks 仍 `INSUFFICIENT`，不凑数。
- 真实运行、输出/manifest/hash 冻结、native exit 和独立 ZCode 成果硬审无条件 PASS；统计不足仍只报 `INSUFFICIENT`。

这组硬门足以保护研究正确性，但不会把不相关平台改造成首个实验的串行门。若实际选定的 RB input 仍依赖 G3→G4→G6，则只对该消费者保留对应边；不能继续把旧 SC 路径宣称为所有研究的总前置。

计划文字还应把以下三件事分开写，避免后续又把流程混成产品状态：

- **来源可消费**：source、lock、calendar、payoff 和 owner 写集一致；这是 M5 的输入条件。
- **产品可接受**：真实命令、测试、冻结、独立硬审完成；这是一个模块的交付条件。
- **研究可解释**：样本成熟、统计门、成本和风险都满足；这是策略证据条件。

任一层未满足，只降低相应层的状态，不得向上“继承” PASS。局部硬审 PASS 只覆盖其计划和 write-set；它不能替代来源可消费或研究可解释。

## 4. 最近一批最小但真实的研究交付

建议将下一交付定义为 **RB-A/B-1：一份双频、同源、完整 required M5 人口的真实研究包**。它是小范围（一个预登记 RB 合法来源），不是降低验收的短回测。

1. MINUTE 和 DAILY 使用独立 config、run_id、split_id、模型和账户；MINUTE 预测 1/5/15/30/60，DAILY 预测 1/5/20。DAILY5 只消费已成熟的 `TRADING_DAY20/ATR_1` 背景，不回退 DAY1/DAY5。
2. 首批入口固定 TREND/BREAKOUT/PULLBACK，策略族范围表仍列出另外四族的 `NOT_RUN/BLOCKED/INSUFFICIENT` 状态。四政策为 BEST_EV、同一合格集的 PROBABILITY_SAMPLE、真实 TREND FIXED、CASH；不因结果改候选区间、阈值、seed 或合约。
3. 六种场景 `BASE/FEE_X2/SLIPPAGE_X2/MARGIN_X1_5/GAP/NO_STOP` 使用相同冻结候选、风险预算和连续账户。GAP/NO_STOP 暴露压力，不能回写 BASE；所有 zero group 保留。
4. 报告必须给出实际预测、校准样本、训练 payoff 观察、准入漏斗、候选/阻断原因、四政策账户、交易/未成交/保证金/尾损/回撤和每品种×频率×期限×族状态。结果不足写 `INSUFFICIENT`，未运行写 `NOT_RUN`，工程错误写 `FAIL/BLOCKED`。
5. 该包只能回答“这份冻结 RB 输入上的工程和研究证据是什么”；不能回答全六品种首版完成、影子接受、策略有效或可交易。首批 A/B 仍然不调用最终留出、不消费 reserve/begin/holdout。

**可验收出口。** accepted input/source、config、run、report、manifest、split、模型激活、账本和日志各有 SHA-256；产生者终止后才封存；所有 required 命令和测试记录真实 native exit；无未来字段/同日相关合约泄漏；连续账本和六场景可由独立复算；独立 ZCode 对实际 write-set 给出无条件 PASS。若任一前置未满足，出口是带精确原因的 `BLOCKED/NOT_RUN`，不能用 static PASS、SCENARIO 或 timeout 调大替代。

RB-A/B-1 的最小报告索引应固定为：

- `input_identity`、contract/calendar/availability/fees/margin/payoff/source hashes；
- MINUTE 与 DAILY 各自的 `config/run_id/split_id/seed` 和 `final_holdout=false`；
- 每个 fold 的 train/calibration/rolling 起止、purge、embargo、mature status；
- 每个 entry×exit×policy×scenario 的 candidate、blocked、episode、交易和未成交计数；
- probability/calibration/payoff/risk/ledger 的分层状态；
- 六品种×两频率×全部期限×七入口的 `PASS/FAIL/INSUFFICIENT/NOT_RUN/BLOCKED` 矩阵；
- 工程测试、真实 run、硬审 verdict 和所有失败命令的 raw stdout/stderr/hash。

该索引是研究结果的可读出口，不是新的管理或封存体系；沿现有 M5 report/manifest 结构落地即可。

## 5. 并行如何落地而不追求“槽位全满”

并行目标是减少真实消费者等待，而不是填满 agent 数量。每条线必须有价值、明确 writer 和可交付出口：

| 并行线 | 可立即做的工作 | 真正硬依赖 |
| --- | --- | --- |
| M1 合版/快照内存 | 选自然 source tree、补 exact test、profile P2、修复制路径、准备冻结 | M2 current-lock、M3A/M3B/M5 |
| M2/R1 RB 输入 | 只读核对 accepted RB source、calendar/A/fees/margin 和 payoff identity | M1 accepted lock；不能用 SCENARIO 代替 |
| M3A/M3B | 依据有效锁准备测试和接口；锁闭后立即实现 | M1/M2 |
| M3C/M4 DAILY seam | 复核 DAY20/ATR_1、UNKNOWN、自然 OPEN→payoff→observation 和账本 | M3A/M4 公共接口；不由 M5 顺手改 |
| M5 A/B | 先完成 memory/source admission，随后真实 fit、报告和冻结 | 上述四项；是最近交付主线 |
| 后置模块 | RUNTIME-R5、APUB、EX06、宏观 producer、CU 生命周期只做各自 prepare/修复 | 不阻断 RB A/B，除非该实验明确消费它 |

建议的依赖图是 `M1 natural source + M2 lock → M3A/M3B/M4 → M5 A/B`；DAILY seam 是 B 的硬边，MINUTE 可提前做只读/工程准备，但双频包的最终出口等 B。M3C P2–P4、D selector、shadow、部署和执行域都在该图外。活动审阅写集、必要用户授权和真实跨模块契约冲突仍可阻断对应线；非阻断观察不再生成额外审批轮次。

每条并行线开始前只回答三个问题：它是否直接解除 A/B 的具体硬依赖？它是否有独占 writer 和可冻结文件集？它是否能在不改模型配置、不读取凭据的情况下产出可核验结果？三个问题都没有肯定答案的工作放入后置队列，不能因为有空闲 agent 就启动。

## 6. 延后、冻结与必须保留

**延后或冻结：** 全六品种扩展、CU 全生命周期 accepted A/T/R、远端宏观回填、APUB/EX06/运行平台、RUNTIME 产品接线、shadow/部署、M3C P2–P4、机会成本/P4、非 legacy 退出 selector。它们是后续合同，不是本次 RB A/B 的通用门。

**必须现在保留：** 原七族和六品种承诺；MINUTE/DAILY 独立语义；PIT、永久留出、成本/压力、风险预算、整数手、相关样本去重、未知空仓、连续账本、native exit、证据冻结、独立 ZCode 硬审和交易关闭边界。延后是调度决定，不是范围删除。

## 7. 剩余阻断的最短闭环

1. **统一源。** Root 选出一棵真实自然来源；合并 M1 P1 与 snapshot memory 只允许在该树中按计划增量完成，生成新的 current lock；旧树和失败证据只读保留。
2. **先解内存瓶颈。** 对 25,679 population 做有界 profile，减少逐元素校验/pack/再展开的重复工作；按原资源上限重跑 P1/P2。超时仍失败就报告算法瓶颈，不能把 300 秒改成“通过”。
3. **闭 M1/M2。** 补 `test_v2_m1_p1_open_contracts.py` exact set 和 41/42 真实计数，完成 M1 hard review；current-lock 通过后立即放行 M3A/M3B，不等待无关平台。
4. **闭 DAILY seam。** 由 M4/原 owner 修天然 DAY20/ATR_1 context→regime→admission→OPEN→payoff observation；修正 workorder 的 verdict/native3 证据后重新跑必要命令。代码结果与流程纠正分别记账。
5. **跑 M5 A/B。** 先以 accepted RB bundle 跑真实 MINUTE/DAILY required population；保留所有 native 失败和不足，生成双频状态矩阵、连续账户和六场景报告；完成冻结后马上交独立 ZCode 成果硬审。
6. **再按结果决定。** 只有 RB A/B 包达到研究出口，才决定是否扩到其余品种或恢复退出/宏观/影子工作；失败或不足也算有效研究交付，下一步应由具体缺口决定。

最短闭环后的状态判定应只有以下几种：

- `BLOCKED`：必要 source/lock/calendar/接口未接受，未运行研究命令；
- `ENGINEERING_FAIL`：消费链、时间、账本或输出完整性错误，退原 owner；
- `INSUFFICIENT`：路径正确但样本、成熟事件或日期块不足，保留 n=0 和原因；
- `STATISTICAL_FAIL`：样本充分但校准、覆盖、净 EV 或风险门失败；
- `RESEARCH_PASS`：只对精确 RB/频率/期限/entry×exit/费用版本成立，仍不等于影子或交易许可。

`NOT_RUN` 只表示没有执行，不得被流程等待改写为 FAIL；`SCENARIO` 只描述情景假设，不得被报告标题改写为 PIT。

## 8. 需要以后单独确认的约束变化

- 把旧 `G3→G4→G6` 从“所有研究总前置”改成“按实际消费者绑定”的调度规则，可能需要更新已有计划/契约引用；本报告提出但不替用户批准。
- 将 RB-A/B-1 定为双频首个交付、暂缓其他品种和平台，是排程调整，不删除首版范围；若要改成正式里程碑或改变原合同文字，应由后续 Codex 计划/用户确认。
- 任何放宽窗口、概率/经济门、内存上限、超时、PIT/留出、风险预算、source schema、lock 或真实执行边界，都不是本复评授权范围，必须单独提出并重新计划/审核。
- 不改变任何模型/provider/API/reasoning 配置，不授权数据库、SDK、部署、客户端 reload、订单或撤单。

## 未核实限制

本复评没有运行源码、M5 fit、内存 profile、测试、数据库或网络，也没有替代独立 ZCode 硬审。RB 数字、M1 exact-set、M5 timeout 和各节点状态引用已有 owner/root 证据；它们支持调度判断，不构成我重新执行后的产品 verdict。正式产品状态仍以冻结文件、真实命令退出、独立复算和独立 ZCode 无条件 PASS 为准。

报告写入后不触发任何后续任务。下次获得授权时，Root 只需据此确认一项调度选择：

1. 接受“RB-A/B-1 先行”的顺序，并修订依赖表；或
2. 保留旧总前置，但明确这会继续延后第一份真实策略证据。

无论选择哪一项，原策略合同、风险门、交易关闭和模型配置保护都保持不变。
