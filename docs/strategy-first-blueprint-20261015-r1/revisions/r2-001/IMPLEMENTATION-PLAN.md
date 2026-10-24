# 策略优先实施计划：审后修订 r2-001

对应：[蓝图](BLUEPRINT.md)、[修复顺序](REPAIR-ORDER.md)。本次写入仅为规划；开发 pause 保留，尚不派产品实现、不运行研究或模拟。以下是在用户恢复产品工作后立即执行的排程，不另问已授权的策略优先偏好。

## 1. 继承具体模块计划和写权

本文件不能代替具体模块的锁、阶段、lease 或操作 admission。沿用下列原件及其已经生效的后继；未接受候选不因被引用而获得信用：

- [旧实施计划 §1、R1–R5](../../../strategy-research-blueprint-20261010-r2/IMPLEMENTATION-PLAN.md)：模块边界、唯一 writer、所选 source 和必需测试人口。
- [M5 A/B PLAN](../../../strategy-research-blueprint-20261010-r2/implementation-clarifications/m5-ab-experiment-plan-20261014/PLAN.md)：真实 A/B 配置、来源、required 人口及操作条件。
- [RESEARCH r2 PLAN](../../../parallel-development-20261004-r1/plans/RESEARCH/r2/PLAN.md)及其 `plan.json`：所消费 EP1/SH 阶段、精确原义务、文件释放与 B0–B8；治理以当前 AGENTS 为准。
- [M1 P1 formal-adoption PLAN](../../../strategy-research-blueprint-20261010-r2/implementation-clarifications/m1-p1-open-public-successor-20261014/revisions/r2/formal-adoption-001/PLAN.md)：实际需要 P1 合版的公共契约及 exact required map。
- [snapshot 源 lease](../../../parallel-development-20261004-r1/coordination/m1-snapshot-provenance-root-source-lease-20261014-001/LEASE.json)和 [HANDOFF-002](../../../strategy-research-blueprint-20261010-r2/implementation-clarifications/m5-ab-experiment-plan-20261014/SNAPSHOT-MEMORY-HANDOFF-002.json)：该候选的精确 source、支撑写集、预算和集成次序。支撑/容量 lease 不授权真实 fit 或 generation。

Root 派发时从选定模块有效记录取得精确 source、owner、文件集、输入 pin、cwd/argv、输出前置、资源预算及释放状态。已有有效接受和 lease 满足时直接沿用；缺项只补该操作必需的 admission，不用本蓝图另开通用写权，也不先把所有候选合版。

| 责任单元 | 唯一文件归属；实际写入仍限原计划/lease |
| --- | --- |
| M1/M2/R1 | M1 的 `src/ak_trading/v2/contracts.py`、`artifacts.py` 及其精确公共集合；M2 的 `factor_specs.py`、`factors.py`、`seasonality.py`；数据仅原 D1/D2/labels 工作单集合。 |
| M3A/M3B/M3C/M4 | 各自原 `probability/`、`strategies/`、`exits/`、`decision/` 精确集合及其测试；DAILY seam 归原 M4，M3A 只核对概率接口。目录名不是通配写权。 |
| M5 研究 | 原授权的 `validation/{experiments,splits,metrics,reports}.py`、`scripts/research_probability_v2.py`、配置、注册和运行输出；其他节点不直接改 M5 文件。 |
| R5 续接 | `validation/shadow.py` 及确需修改的 M5 调用文件/测试，在 M5 研究阶段释放后顺序接续；客户端/执行源仍归原执行 owner。 |

`src/ak_trading/v2/validation/experiments.py` 在任一时刻只有一个 M5 writer。R5 默认由该 M5 owner 顺序继续；若 owner 不同，Root 必须在原 filewise release 记录中明确旧/新 owner、路径和接受版本，完成 transfer 后才写入。活动硬审期间该文件对 owner 同样冻结。

## 2. 先决定实际消费者，不建立共同平台门

按旧蓝图事前规则登记六品种两频率的覆盖、成熟尾段、规则可用性及已使用范围，优先 RB。RB 不足时保留缺口，并以同一规则检查其他品种；禁止收益选源、拼凑绿色文件或重置 seen/留出状态。

每频率列出实际 FrozenInput、contract lock、source identity、calendar/available_at、fees/margin/multiplier/tick/limit/capacity、StrategySpec、payoff plan 和 LEGACY binding。只沿该消费链保留 M1/M2/G3→G4→G6 等必要边。未消费 P1/snapshot、APUB、EX06、远端宏观或 runtime 的合法路径不等待它们；选择其他源仍须通过同样的输入和代码接受条件。

如果所选消费者使用 HANDOFF-002 的合版候选，严格复用其原顺序：`accepted M1 B delta → accepted additive P1 delta → accepted memory support exact hunks → unified source compatibility/admission → original full100 → frozen actual integration hard review`。本版不改此顺序、不用其他候选局部 PASS 代替。需要公共合同变更时回原 M1 有效计划，不由 M5 临时补 schema。

## 3. 最短并行队列与逐频率运行

1. 数据 owner 核对来源与缺口；必要代码/性能 owner 在各自 lease 内处理实际依赖；M5 准备各频率配置、split、报告；模拟 owner 只读映射现有接口。上述不相干写集可并行。
2. 复用 scoped DAILY 已修代码和原反例证据，只闭消费所需的 work-order/relay 状态。DAY5 仍以独立、已成熟、可见的 `TRADING_DAY20/ATR_1` 为背景；缺失/过期/冲突仍 UNKNOWN，禁止回退 DAY1/DAY5。没有新缺陷不重做 seam。
3. MINUTE 自身依赖就绪即运行 A 并交报告；DAILY 自身依赖就绪即运行 B 并交报告。不因另一频率未 ready 而退回纯 prepare，也不要求两频率在同一时间启动。共享资源按原排他预算排队，不改成新的业务依赖。
4. 每频率使用独立 config/run_id/split_id、模型和连续账户；首批三族分别使用现有 m1/d1 StrategySpec，经济模板仍为 30 交易分钟/5 交易日。八期限及原 duration 不删。
5. 模式为 `walk-forward`、`final_holdout=false`、`RESEARCH_EVALUATION`、真实输入 `synthetic=false`。每 fold 保留 252/60/60、purge/embargo、PIT、同日相关合约分区；不调用最终 holdout reserve/begin/consume。
6. 四政策及六场景按原矩阵执行；每频率 24 单元、双频 48。训练期冻结候选/阈值/成本/seed，校准期只校准概率；四成本完整回放，GAP/NO_STOP 按原 BASE cohort 评估。不是对全部族、品种、退出做新笛卡尔积。
7. 只运行该有效计划要求的完整人口。snapshot P2 支撑测试或某 full100 候选未过，只阻依赖它的候选；所选路径本身要求的 full100 不得省略。预检、bounded probe、单条 observation 均不替代正式人口。

报告沿现有 `experiment.json/metrics.json/readiness.json/report.md/manifest.json`、fold 输出与实验注册机制，不新建管理账本。每频率交付来源/规则、模型与校准、payoff 漏斗、候选/阻断、四政策连续账户、交易与未成交、成本/保证金/尾损/回撤、场景及完整状态矩阵；总批另列两份报告齐备情况。

## 4. 原验证、独立硬审与研究状态

代码变更先完成所属计划的必需正反例/回归，冻结实际写集后立即独立 ZCode 硬审；已接受且未改变的代码不重复开发或重复未变审核。只有该消费路径所需代码接受后才用于正式研究。研究运行产物依原范围做读回、复现/复算和必要成果硬审，不以开发者自测替代独立结论。

必须核对 available_at、完成时点、session/calendar、payoff/entry/exit/cost/risk/ledger identity；未来追加不改变历史决策，未成熟标签和永久留出不进入拟合。原普通/马丁样本、概率、经济及风险门继续适用；n=0、零组及失败记录保留。

每条命令原样记录 stdout/stderr、native exit、阶段完成、实际产物及研究状态。`StageIncomplete`/空训练模型/无 payoff 观察不能改称正常 OOS 现金结果；路径完整后的零成交可以是合法研究结果。统计 FAIL/INSUFFICIENT 与工程 FAIL、硬审 verdict 分轴报告，不为了“首批通过”降低门槛。

生成输出先按实际 existence 声明，不能读未创建输出当输入；源码在审前冻结，运行日志在生产进程终止后封存。沿既有证据格式，不新建封存体系。新派发单继承 [rg exit 规则](../../../maintenance/20261004-rg-no-match-r1/INCIDENT.md)及 [CLI/输出与审批恢复规则](../../../maintenance/20261004-grok-headless-cancel-r1/INCIDENT.md)：可选 rg native1 且 stderr 空才是 NO_MATCH，测试/CLI 失败绝不借用该解释。

## 5. 模拟准备可以并行，真正消费按自身条件推进

历史回放与本地虚拟撮合先复用既有离线账本，以 `RESEARCH_EVALUATION` 产出研究证据。代码/输入及运行权限就绪即可做，不要求先有 execution hard PASS、20 日记录或实盘 UI 开启；真实 broker 禁止。正式研究不足时仍诚实报告，不伪造统计资格。

前瞻 paper/shadow 在所消费代码、输入、feed/clock 接口和运行权限具备后开始记录；不要求先有完整影子验收才能开始累计。正式 `SHADOW_OPERATIONAL` 对统计 FAIL/INSUFFICIENT 保持 NO_TRADE。研究出口消费和完整 `SHADOW_ACCEPTED` 仍按旧蓝图的研究资格、真实 20 交易日、完整会话、时延/时钟等门验收，回放不能替代前瞻数据。

只有实际选用 ordinary shadow 路径时，才按原 RESEARCH r2 B0–B8 与 SH 公共契约修复其调用闭包：取得真实 `_exit_pack(specs)`，同一 registry/specs/bindings 传给 ledger、checkpoint 和 shadow runner。未定义 `_reg` 是 NameError；缺必填 `exit_registry_sha256` 是 TypeError；`exit_bindings=()` 有默认值，缺 bindings 是身份完整性问题，不称参数缺失 TypeError。

R5 修复先做计划要求的廉价闭包/签名正反例，再做 shadow/recovery 必要验证；不为该修复启动昂贵 fit，也不让它阻止不消费此入口的 walk-forward。M5 与 R5 共用文件顺序续接，其余准备可并行。

真实执行适配器、EX06、客户端部署只对实际消费它们的路径前置。真实订单/撤单另须 AGENTS §4 全部门和用户 UI 开启，本计划不提供授权。全程默认交易关闭、observer hold、真实 send/cancel=0，既有 SDK/账户/服务/客户端权限不变。

## 6. 交付和停止

单频率报告先交；A/B 总批要求两频率实际报告及对应必要审查齐备，统计结果可支持、失败或不足，未运行部分仍 NOT_RUN。缺依赖只停其消费者；跨节点缺陷退所属 owner，硬审 FAIL 回同一 owner 追加修复后立即 fresh 独立复审。

本版发布及窄规划复审不解除原产品暂停。恢复指令到达后，对已有效计划和 ready 前置直接接实现/运行及硬审，不再增加同偏好的确认或第二轮未变规划。
