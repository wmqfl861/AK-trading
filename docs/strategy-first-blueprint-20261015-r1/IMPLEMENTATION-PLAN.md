# 策略优先首批实施计划

版本：strategy-first-blueprint-20261015-r1  
对应蓝图：[BLUEPRINT.md](BLUEPRINT.md)  
性质：可供独立 ZCode 计划审核的实施计划。计划通过不代表实现授权、研究 PASS 或模拟交易授权。

## 1. 首批目标和范围

首批只交付 RB-A/B-1：一棵同源 RB 输入、MINUTE 与 DAILY 两个独立频率、三个首批策略族 TREND/BREAKOUT/PULLBACK 的真实 M5 A/B。

首批研究必须使用现有有效契约、可追溯 source identity、PIT 输入、252/60/60 时间窗口、purge/embargo、永久留出、成本与压力情景和连续账本。所有不足、阻断和未运行组合保留原状态。

首批不扩展七族全组合、不做全品种笛卡尔积、不等待远端宏观、客户端部署、通用 runtime、APUB、EX06 或真实执行适配器。它们只在 RB-A/B-1 的实际消费者闭包明确使用时重新进入依赖图。

## 2. 真实交付物

1. RB 输入和契约身份：source tree、contract lock、calendar/availability、fees、margin、multiplier、tick、limit、capacity、payoff plan、StrategySpec 和每项 SHA-256。
2. MINUTE/DAILY 独立配置：各自 run_id、split_id、频率、horizon、seed、输入范围、训练/校准/滚动窗口和最终留出标记。
3. 三个首批策略族的真实预测、校准、payoff observation、准入漏斗和连续账户结果。
4. `BASE`、`FEE_X2`、`SLIPPAGE_X2`、`MARGIN_X1_5`、`GAP`、`NO_STOP` 的同候选压力结果；压力场景不能回写基线候选。
5. 每个频率、horizon、策略、退出、policy、scenario 的状态矩阵，区分 `PASS`、`FAIL`、`INSUFFICIENT`、`NOT_RUN`、`BLOCKED`。
6. 真实命令的 stdout/stderr、native exit、运行身份、输入/输出 hash 和产生者终止后的冻结写集，供独立 ZCode 硬审。
7. 研究出口消费契约：accepted source/lock、模型/校准 evidence id、entry×exit×cost policy、risk budget、holdout verdict 和执行审查引用。

## 3. 模块和文件归属

### A. 输入和契约闭合

负责人：原 M1/M2/R1 数据与契约 owner；Root 只协调，不跨写集修复。  
写集：沿现有授权 lease 的 `src/ak_trading/v2/contracts.py`、`artifacts.py`、M2 因子/来源文件和对应契约测试；不得把外部候选树直接拼入主线。

交付：一棵自然 source tree、current lock、RB 输入适用范围、日历和可见性说明。  
依赖：已有有效计划、精确输入 pin、M1/M2 owner identity。  
验收：required map exact set、公共签名/ schema、来源 hash、calendar/availability 和锁值一致；M1/M2 独立 ZCode 硬审前不放行消费者。

### B. DAILY seam

负责人：原 M4 seam owner，M3A 只负责其概率接口；M5 不修改 seam 文件。  
写集：既有 pipeline/regime/admission/ledger 文件和其授权测试，保持原文件归属。

契约：DAILY5 只能消费独立、已成熟的 `TRADING_DAY20/ATR_1` 背景；缺失、未成熟、过期和冲突都产生 `UNKNOWN`，不回退 DAY1/DAY5，不强置 `ALIGNED`。

验收：完整 context→regime→admission→OPEN→payoff observation→ledger 的正向和拒绝路径；未来追加不改变旧判断；scoped reviewer PASS 与 work-order/relay 状态分别记录，流程未闭合时不得发布整条链。

### C. 策略、概率和账本消费闭合

负责人：原 M3A、M3B、M4 owner，各自只写计划授权的文件。  
写集：`src/ak_trading/v2/probability/`、`strategies/`、`decision/` 中原 owner 的精确集合；不新增未经裁决的模型、退出族或风险字段。

验收：MINUTE 与 DAILY 的预测、标签、校准、payoff 和 continuous ledger 接口可以在冻结输入上互相读回；概率不足时必须进入 `INSUFFICIENT`/`NO_TRADE`；entry、exit、cost、risk 和账本 identity 一致。

### D. M5 真实 A/B

负责人：唯一 M5 owner。  
写集：原 `validation/experiments.py`、`splits.py`、`metrics.py`、`reports.py` 和 `scripts/research_probability_v2.py` 的授权集合；不修改 M1–M4 源码。

步骤：先对 MINUTE/DAILY 做配置和 split 预检；确认输入、模型、payoff、seam 和账本闭合后执行 required population；分别生成训练、校准、滚动、压力、账户和报告输出；保留全部失败和不足状态。

验收：每个 fold 的窗口、purge、embargo、mature status 可复核；没有未来字段、同日相关合约泄漏或最终留出预消费；基线与压力场景共享冻结候选；输出能由独立读回重算关键账本字段。

## 4. 实施顺序和可并行工作

1. 输入 owner 选择并冻结自然 RB source/lock；同时 M1 owner 可修 required-test exact set，M5 owner 可做只读配置准备。
2. M1/M2 current-lock 闭合后，M3A/M3B/M4 立即验证实际接口；不等待 APUB、EX06、runtime、宏观或其它品种。
3. DAILY seam 与 MINUTE 准备可并行；双频最终运行必须等两者各自契约满足。
4. M5 owner 先运行低成本输入/配置预检，再运行完整 required M5 A/B；不以预检或局部测试充当完整结果。
5. M5 写集冻结并完成必要验证后，Root 立即派独立 ZCode 硬审核；不等待其它后置模块。
6. FAIL 回同一实现 owner 做 append-only 修复，重新冻结后 fresh 独立复审；PASS 只对本写集和本研究范围生效。
7. 只有真实研究出口和执行域全部满足原规则后，才准备模拟 acceptance；用户明确授权和客户端 UI 操作仍是最后门。

## 5. 必要验证

- 每个源码/配置/脚本写集记录计划版本、文件清单和 SHA-256。
- 每个命令立即捕获 native exit、stdout、stderr；测试/研究 CLI 的 exit 1 不按 `rg NO_MATCH` 解释。
- 生成输出启动前记录 `ABSENT`，只在产生者终止后读取和封存；不对未生成输出故意 Read。
- M1 required map 同时验证计数和 exact set；新增 `test_v2_m1_p1_open_contracts.py` 后预期集合必须完整包含它。
- snapshot P2 先分段 profile prefix、feature、identity/hash、展开和回收；保留 25,679 全人口原预算，不能只改 timeout。
- M5 复核输入的 available_at、交易日/session、费用、保证金、tick、limit 和 payoff identity；不满足即 `BLOCKED` 或 `INSUFFICIENT`。
- 独立硬审必须重跑计划要求命令，加入与风险相称的反例，检查实际 write-set 和新冻结 hash，并给出无条件 PASS/FAIL。

## 6. 研究出口和模拟交易出口

研究出口允许四种有效结果：`RESEARCH_PASS`、`STATISTICAL_FAIL`、`INSUFFICIENT`、`BLOCKED`；工程错误单独记 `ENGINEERING_FAIL`。任何一种都必须说明输入、人口、窗口、成本、风险和限制。

模拟 acceptance 只有在研究出口已冻结、execution hard review PASS、部署/模拟期间 send/cancel 均为 0、20 个交易日 feed/clock/shadow 证据满足原门、observer 始终 hold 后才可审查。交易仍需用户明确授权和 UI 开启；本计划不授权真实订单。

## 7. 停止条件

source/lock/calendar/availability 或 DAILY seam 未接受时停止 M5 正式运行，继续非写入准备。  
样本不足、校准不足、费用/规则未知时保持 `INSUFFICIENT`/`BLOCKED`，不能删样本或降低门槛。  
工程命令失败、输入漂移、hash 不一致、账本绑定缺失或 hard review FAIL 时停止对应发布，回原 owner 修复。  
RB-A/B-1 交付并完成独立硬审后，本批停止，不自动扩展下一品种、下一退出族或部署任务。
