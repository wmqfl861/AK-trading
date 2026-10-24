# AK-Trading 策略研究成果及验证有效性重新评估

审阅者：独立 Astra/max 研究审阅（2026-10-15，Asia/Shanghai）  
范围：策略研究成果、历史证据、可否证路径、走向模拟交易的前置条件。  
权限边界：本次只读审阅；没有恢复开发、fit、数据采集、数据库/SDK、网络、服务、客户端、部署、交易或审核 CLI。  
本报告不是 ZCode 产品硬审，也不替代已有模块硬审结论。

## 结论先行

截至本次审阅，没有证据可以把 AK-Trading 的任一策略记为“已在真实历史数据上验证有效”，也没有证据可以进入模拟交易。已有最接近可否证策略的路径是：冻结、可复核的 RB 日线 legacy 三策略情景结果 → 保持完整轨迹的时间分块/跨合约描述性迁移 → 在合格 PIT 输入上执行 M5 的真实训练、校准、walk-forward、连续账户和未使用最终留出。该路径仍在计划或准备状态；RB 情景产物只能用于缩小假设和发现失败模式。

本次最有方向性的事实如下。

* `rb-daily-causal-regime-20261014-review-r1` 的 51 个账户、7,590 行 ALL、4,785 行 PRIMARY、612 个组和 313 个 payload 均可由独立读回与小型复算支持。它是 `SCENARIO`、`historical_strict_execution=false`、`assumptions_verification=not_historical_verified`、`statistical_qualification=NOT_ASSESSED`；其硬审报告也明确写出这些边界（该报告第 101–104 行）。
* `rb-daily-scenario-compare-20261014-review-r1` 的 14 个共同覆盖合约中，BASE 的 Donchian 中位净损益为 4072.020，正/零/负为 10/1/3；MA 为 126.271、7/1/6；z-score 为 -1999.2945、2/1/11。z-score 在事前 `RANGE` 标签的 PRIMARY 贡献中位数为 -505.471，14 个相关合约中仅 1 个正、3 个零、10 个负。这是“继续迁移检验 Donchian、优先否证当前 z-score 横盘假设”的研究线索，不能当作 OOS 收益率或组合回报。
* 现有 M3A 真实重建文件有模型、标签、校准和少量 forecast 记录，但第一批 12 个校准记录中包含 `INSUFFICIENT`、`FAILED` 和少数 `VALID`；例如普通策略 `ATR_1`/`ATR_2` 的单分钟 PATH 条目为 `VALID`，但该记录只有 299 个样本、75 个有效日期块，马丁条目仍为 `INSUFFICIENT`；持续事件高桶只有 51 个样本、43 个块，且校准误差 0.31298。另一个 run 的样本只有 151、40 块，forecast 数为 0。它们不能支持交易门槛或“已完成 fit”。
* RB 规则事实仍未闭合：`FROZEN-DELIVERY.json` 的 `qualified_value` 全部为 null，`A_status=UNKNOWN`，`historical_A_assignments=0`，`absolute_limits_materialized=0`，`fit=NOT_RUN`。这使费用、保证金、涨跌停、tick/rounding 与 PIT 可用性仍不能被当作历史回测事实。
* CU 仍是 `INPUT_DEFINITION_MISSING`/`CU_SOURCE_ROUTE_UNESTABLISHED`；T、R、S(t) 为 null。M5 的真实 full100 也没有运行：P1 只是 22 项支撑测试，P2 以 native 1、`COMMAND_DEADLINE_EXCEEDED` 结束，结果文件显式 `M5_overall_PASS_claim=false`。

所以当前策略状态应写为：**工程账本和场景描述有局部实物；历史策略有效性为 NOT_ESTABLISHED；正式 M5 研究为 NOT_RUN/INSUFFICIENT；模拟交易资格为 NOT_AUTHORIZED。**

## 审阅方法和证据边界

我先读取了仓库 `AGENTS.md`、`PROJECT-WORKFLOW-RULES.md`、`C:/Users/Administrator/.agents/skills/tqsdk-trading-and-data/SKILL.md`、`C:/Users/Administrator/.agents/skills/codebase-memory/SKILL.md` 以及两份 2026-10-04 维护 incident。结构图谱项目 `D-Projects-AK-trading` 状态为 ready，generation 为 `2026-10-13T01:30:24Z`，但图谱提示 `scenario_backtest.py` 的 metadata 已变化，实验脚本为 not_tracked；因此所有重要结论均回到实际文件读取，图谱只用于定位 `_replay` 的调用关系。索引报告列出的 parse_partial、parse_unusable、skipped 文件也没有被用来支撑本报告的事实。

本次新验证是无副作用的本地 Python 读取/计算：

```text
kind=READ_ONLY_REASSESSMENT_NO_ENGINE_RUN
rows=7590; accounts=51; primary_rows=4785; price_observations=2530
raw_date_span=2020-01-02 .. 2021-01-29
primary_regime_counts=MIXED 690, RANGE 468, TREND_UP 370, TREND_DOWN 67
primary_unique_raw_dates=MIXED 151, RANGE 101, TREND_UP 96, TREND_DOWN 15
range_zscore_median_recomputed=-505.471
range_zscore_positive_zero_negative=1/3/10
account_and_label_problems=[]
payload_hashes_checked=312, payload_hash_drift=[]
source_hashes_checked=11, source_hash_drift=[]
native exit=0
```

复算从 `data/research_runs/project-review-20260908-rb-input/bars.csv` 直接按 `datetime_nano` 排序，以 `tools/experiments/historical_daily_causal_regime.py:228-256` 的公开规则独立计算 prior-21 efficiency-ratio 状态；再核对 `daily_attribution.csv` 的每行 `net_delta = gross_delta - fee_delta - slippage_delta`、账户净损益与 `E[last]-E[59]`。没有重跑昂贵 fit、没有调用引擎、没有读凭据，也没有把复算结果写回原结果树。

## 当前已验证成果清单

| 成果 | 本次确认的实物 | 它实际证明什么 | 不能证明什么 |
| --- | --- | --- | --- |
| RB 日线 causal-regime | `data/research_runs/rb-daily-causal-regime-20261014-review-r1/summary.json`、`coverage.json`、`daily_attribution.csv`、`regime_summary.csv`、`report.md`；`hard-review-r1/VERDICT.json` 为无条件硬审 PASS | 51 个具体合约×legacy 策略的因果状态标签、连续 scenario ledger、成本与逐日归因在冻结输入上可复现 | 交易日历、上市/可见时点、实际费用/保证金、PIT、OOS、独立样本、组合风险、策略资格 |
| RB scenario compare | `data/research_runs/rb-daily-scenario-compare-20261014-review-r1/report.md:1-5,8-42`、`summary.json` | 17 个有 bars 的具体合约×3 legacy 策略×4 情景共 204 个描述性单元，14 个共同覆盖合约可对照，成本/滑点情景会改变结果 | 合约不是独立 episode；没有组合 NAV、显著性、Sharpe 或 qualification；仍是 unverified scenario |
| RB capital stress | `data/research_runs/rb-daily-capital-stress-20261014-review-r1/report.md:1-30`、`summary.json`、`coverage.json` | C5000/C15000/C1000000 与 BASE/MARGIN_X1_5 下的开仓阻断、保证金超限、固定 1 手账本及结果差异可复核；C5000 全部不能开仓，C15000 的 MARGIN_X1_5 只剩少量可开账户 | “最低安全资金”、历史真实保证金、组合可交易性或收益资格；统计人口仍是重叠合约 |
| RB chronological transfer | `implementation-clarifications/rb-daily-chronological-transfer-20261014-r1/PLAN.md:1-12,20-32,75-89,114-133` | 计划定义了完整合约轨迹后按 raw-year 增量归因，明确不切年、不重置账户、不伪称 OOS | 计划状态为 `DRAFT_PENDING_INDEPENDENT_ZCODE_PLAN_REVIEW`；没有实现、没有 252 次 replay、没有 transfer 结果 |
| M3A historical reconstructed probability | `data/research_runs/v2/m3a-real-80d/run_summary.json`、`calibration.json`、`forecasts.json`、`diagnostics.json`、`manifest-v2.json` | 已有某些模型/标签/校准/forecast 文件，且 provenance 标为 `HISTORICAL_RECONSTRUCTED`；可以发现样本不足和模型诊断问题 | 完整 PIT、真实费用门、稳定跨品种预测、策略连续账户；`published_forecasts=3` 的单次 run、另一 run 的 0 不能代表上线可用 |
| M5 A/B 计划 | `implementation-clarifications/m5-ab-experiment-plan-20261014/PLAN.md:11-23,67-86,100-110` | 明确真实 MINUTE/DAILY fit 尚未执行，并保留 252/60/60、200/30、500/60、PIT、purge/embargo、48 单元等合同 | 计划本身没有 fit、研究结果或硬审信用 |
| M1 snapshot support | `D:/AKS261014/M1/snapshot-provenance-r1/owner-evidence/runs/p1-002/RESULT.json`、`p2-001/RESULT.json` | P1 的 22 个支撑测试命令成功；P2 资源/时间失败被诚实保留 | M5 full100、真实 feature fit、模型统计或策略结果 |
| RB historical rule source | `D:/AKS261014/R1/rb2305-jan03-risk-limit-originals-001/FROZEN-DELIVERY.json`、`D:/AKS261014/R1/rb-limit-rounding-primary-acquisition-002/SOURCE-NOTES.json` | 111 文件部分事实包和独立工程硬审 PASS，且缺口被完整记录 | 任何风险/涨跌停/tick/rounding 字段已采用；A、first_seen、适用版本和 fit 均未闭合 |
| CU population | `D:/AKS261014/R1/cu-daily-population-001/CU-GAPS.md:1-28` | 明确输入定义缺失和下一步所需 lifecycle/calendar/source closure | CU population、T/R/S(t)、M5 capacity 或任何 CU 策略结果 |

## 实际策略结果说明

### RB 情景账本的有效信息

RB scenario engine 的 `_replay` 在 `src/ak_trading/futures/scenario_backtest.py:293-453` 中执行完整存储轨迹：下一根 observation open 执行前一根 target，反转先平后开；费用和滑点按每条腿收取；收盘 mark、终点 close、cash/equity/gross/net 的 Decimal reconciliation 都被保留。输入与输出报告也明确：这是单合约 retrospective scenario，不使用结算价、交易日历、成交量容量、队列、价格限制或 point-in-time 可见性。

因此，RB 结果很适合回答“当前固定 legacy 假设在这组价格轨迹上有哪些可复现的失败/差异”，不适合回答“可实盘策略在哪些行情赚钱”。本次复算进一步确认：

1. 51 个账户的标签、PRIMARY 口径和净损益闭合无差异，312 个 payload 与 11 个源码 pin 无 drift。这提高了工程结果的可信度，但不改变证据级别。
2. 状态覆盖高度不均衡。PRIMARY 的 TREND_DOWN 只有 67 个合约观测、15 个去重 raw 日期；`RANGE` 有 468 个观测、101 个 raw 日期；这些是存储日期，不是已验证交易日。任何“下跌趋势策略有效/无效”的结论都会受小样本和合约重叠影响。
3. causal-regime 的状态标签不驱动交易，`prior21_er20_v1` 只是事后归因维度；因此状态组里的 PnL 不是一个已登记的状态路由策略回测。
4. z-score 在 RANGE 的负中位数和 10/14 负合约是有用的否证信号；它足以要求冻结当前版本、做时间外迁移和成本敏感性，而不足以推出“所有均值回归/网格/有限马丁均无效”。

### Scenario compare 和 capital stress 的差异

`report.md:8-42` 的 14 合约共同覆盖表是本批最接近策略比较的结果。它显示 Donchian 的正/零/负合约为 10/1/3，MA 为 7/1/6，z-score 为 2/1/11；费用翻倍和滑点翻倍没有把 Donchian 的中位数抹掉，却也没有消除负合约。我的重算得到相同分母和中位数，并把策略交易腿总数作为会计检查使用；交易腿数量不是独立样本数。

Capital stress 提供的是资金约束的反例：C5000 BASE 的 14 个共同覆盖账户全部没有开仓；C15000 BASE 的 Donchian 有 13 个账户开仓，而 MARGIN_X1_5 只有 3 个开仓、13 个账户发生阻断/差异，报告还记录 12 个关闭观察的保证金 breach。它说明“初始现金和保证金假设会改变可执行路径”，不能被改写成“C15000 是安全最低资本”或“策略在压力下仍然有效”。

### M3A 结果不能替代 M5

`m3a-real-80d/run_summary.json` 的关键字段为 `training_requests=4965`、`calibration_requests=897`、`published_forecasts=3`、`skipped_labels=5923`、`calibration_days_effective=75`。它的 `calibration.json` 首批 12 条记录中，普通 `ATR_1`/`ATR_2` PATH 条目为 `VALID`，但 `sample_count=299`、`effective_blocks=75`；普通 `ATR_0.5` 为 `FAILED`；马丁 PATH 全为 `INSUFFICIENT`；`PERSIST:COMPRESSION` 高桶只有 51 个样本/43 个块，误差 0.3129815，远不足以支持 0.85 马丁门槛。另一 `m3a-real-6c9e3e4e` run 只有 151 个样本/40 块，`published_forecasts=0`。

这些文件的 provenance 还列出 `SCENARIO session calendar`、fee rules not visible at decision time、M2 warmup bypass、隔日采样和 expiry-filtered decisions 等限制。它们是历史重建工程资产和不足诊断，不是完成的策略研究。蓝图和 M5 计划要求分钟/日线独立 fit、完整 maturity、PIT、calibration、连续账户、压力情景和未使用留出；当前没有一组产物同时满足这些条件。

## 缺陷和验证欠账（按策略有效性影响排序）

严重度采用对“能否找到/否证策略、能否做真实历史回测、能否走向模拟交易”的影响，而不是对工程文件是否整洁的影响。

### S0 — 在没有真实研究资格前不能进入模拟交易

证据：RB 产物全部 `SCENARIO`/`not_historical_verified`/`NOT_ASSESSED`；M5 计划第 15–23 行写明真实 fit 尚未执行；AGENTS 的交易边界要求 execution hard review、simulation acceptance、用户明确授权、真实 send/cancel=0。当前没有连续账户的研究资格，也没有 SHADOW_OPERATIONAL 的完整输入和 20 个交易日 feed 证据。

影响：任何把 scenario/PASS/工程测试当作模拟交易准入的动作都会跳过策略有效性和执行假设验证。

### S1 — 历史可用性、规则和成本未闭合，无法称为真实历史回测

证据：RB `bars.csv` 的样例行 `available_at=2026-09-01T17:38:25.565772+08:00`，而 bar 的 raw ts 为 2020-01-02；`trading_day` 为空、`trading_day_source=outside_reference_calendar`。RB 规则包的 `qualified_value=null`、`historical_A_assignments=0`、`absolute_limits_materialized=0`；rounding handoff 明确 exact rule/version/applicability/availability 均 UNKNOWN。

影响：当前 PnL 的开盘/收盘、手续费、保证金、滑点、涨跌停和可见性都只能作为固定 scenario 假设；不能用于 PIT 回测、资金建议或真实成本 EV。

### S1 — 当前样本不是独立的时间外测试集

证据：RB scenario compare 报告明确 `overlapping_contract_coverage_not_independent_episodes`；14 个合约的区间重叠，raw 日期未核验为交易日，2020–2021 仅为已看过的导出区间。causal-regime 报告第 4–5 行明确不构成 OOS、最终留出或独立统计证据。

影响：正/负合约个数和中位数不能估计未来策略命中率或置信区间；不能由 Donchian 的 10/14 正合约推出稳健优势。

### S1 — 日线状态路由没有被研究成可交易策略

证据：causal-regime 的 `regime` 是 `prior21_er20_v1` 的事前价格路径标签；报告第 7 行明确“标签从不驱动交易”。scenario compare 的策略为 `donchian_trend`、`ma_trend`、`zscore_reversion`，不是 v2 的 TREND/BREAKOUT/PULLBACK/REVERSION/GRID/MARTINGALE 路由。

影响：只能说某 legacy evaluator 在某标签下的归因结果，不能说“趋势/横盘识别后已自动选择对应策略”。用户提出的“无法判断则空仓”尚未在真实概率门和连续账户中被验证。

### S1 — 概率门和收益模型尚未形成可用于准入的证据

证据：M3A calibration records 的 `sample_count`/`effective_blocks` 低于蓝图普通 200/30、马丁 500/60；多项状态为 `INSUFFICIENT` 或 `FAILED`。M5 计划第 84 行仍要求 252 train/60 calibration/60 rolling，并禁止把 29 blocks 改造成可用样本。

影响：不能把 forecast 的概率字段、`VALID` 单条记录或少量历史 reconstructed forecast 接到开仓门；也没有训练期冻结的净 EV、CVaR、压力损失或连续账户结果。

### S2 — M5 输入人口和研究链仍有硬缺口

证据：M5 计划第 15–20 行列出首批真实 MINUTE/DAILY fit 未执行、多个历史失败和 R1 日候选 29 个 calibration 日期块不足；CU gaps 第 7–10、18–24 行仍为 input/source/T/R/S(t) unknown；P2 RESULT 第 39–42 行是 native 1、deadline exceeded、`M5_overall_PASS_claim=false`。

影响：没有满足输入定义和容量门，就无法解释“没有发现策略”与“尚未运行”之间的差别；当前不能将研究空白误记为策略失败，也不能把工程支撑 PASS 记为研究成功。

### S2 — 真实规则事实与研究输入之间仍没有单一 accepted lock

证据：R1 risk package 是“partial source recovery, not rule qualification”，硬审限制明确无 field adoption；rounding source notes `source_acceptance=false`、`fit_readiness_claimed=false`。蓝图第 21–23 行要求选定源、完整验证和新 lock 后才消费。

影响：不同模块可能用不同 fee/margin/calendar assumptions，导致策略比较不能共享同一经济合同。

### S3 — 已有工程硬审覆盖的是实现契约，不是研究假设

证据：causal-regime hard review 第 17–34 行复核 pins、186 tests、真实 run 和 payload；第 97–104 行仍明确 PASS 只覆盖 H01–H10、无交易/OOS/qualification credit。capital-stress 和 scenario-compare 的 owner/hard-review 也在报告中标注 scenario 和 no qualification。

影响：无需重做这些工程硬审；应把它们作为可靠的测量工具，直接进入新的历史数据和统计研究，而不是重复封存或把 PASS 升级成策略结论。

## 恢复后的优先实验/修复（建议，不是本次授权）

下面按“最少新增工作换取最多策略可判定性”排序。每项都应在恢复后按项目既有 plan→Sol 实施→独立 ZCode hard review 流程执行；本报告没有授权实施。

1. **先完成 RB legacy 的 chronological transfer 描述性实验。** 先审并生效 `rb-daily-chronological-transfer-20261014-r1/PLAN.md`，执行完整 84×3 轨迹一次、按 raw-year 增量归因，不切年、不重置账户、不把后期报告复制到早期。它不能制造 OOS，但能回答 Donchian/MA/z-score 的收益是否集中在单一合约或单一年份，并提供最便宜的否证筛选。必须把 partial-window、calendar unavailable、相关合约和短合约显式保留。
2. **固定一个最小 legacy 研究问题，先做真实 PIT/成本闭合。** 在同一 accepted RB source/lock 上只比较 Donchian 与 z-score 两个相反方向的假设，保留 MA 作为简单 baseline；冻结 fees、tick、margin、limit、available_at、交易日/session 和初始风险预算。若这些字段继续 UNKNOWN，就将实验状态标为 BLOCKED/INSUFFICIENT，而不是用 scenario 数字顶替。这个切片优先回答“是否存在可否证的方向差异”，不先扩七族、八退出和宏观。
3. **把 M3A 的概率证据收敛为一个可判定的校准单元。** 选择一个 horizon、一个 barrier、一个频率和一个策略门，确保普通达到 200 mature events/30 date blocks、马丁达到 500/60；输出 Brier、high-bucket error、Q05–Q95 coverage、按年份/状态的块级结果和 `INSUFFICIENT`。禁止把已看过的 75/40 blocks、隔日采样或 `VALID` 单条记录拼成资格。
4. **在概率门完成后只做一个真实连续账户 replay。** 使用冻结模型和固定四 policy（BEST_EV/PROBABILITY_SAMPLE/FIXED/CASH），先跑 BASE、FEE_X2、SLIPPAGE_X2、MARGIN_X1_5；GAP/NO_STOP 只作风险暴露。报告候选/阻断/成交/未完成退出/资金和回撤，明确全现金为合法结果。若模型全无、payoff observation 为空或人口不足，保留原始 `StageIncomplete`/`INSUFFICIENT`。
5. **以时间外迁移作为真正的否证门，再考虑其他品种。** 在首个不参与模型选择的完整 holdout 上锁定策略/阈值/退出和风险预算，至少满足蓝图的 60 mature decision days、20-day label-only tail、purge/embargo 和相关合约同日分区；失败就停止该假设或记录仅研究用途。CU 只有在 `CU-GAPS.md` 的 T/R/S(t)、source/lock/calendar/A 真实闭合后才进入相同流程。
6. **最后才做 shadow operational acceptance。** 只在上述某一精确策略/频率/品种通过统计和真实历史研究后，按蓝图 20 个交易日、各会话完整、到时率≥99.5%、P99≤5 秒、>120 秒禁新仓、UTC 误差≤100ms、observer=hold、send/cancel=0 的门执行。没有这些证据时，模拟交易状态保持 NOT_AUTHORIZED。

## 对蓝图的修正建议

### 应保留的部分

* `BLUEPRINT.md:5-8` 的“先回答哪些行情有样本外优势、哪些失败、哪些证据不足”方向正确，且比继续扩平台工程更符合用户优先级。
* `BLUEPRINT.md:17-23` 对 v1 探索、源码存在、工程接受、实际 fit 执行三者分账清晰；`BLUEPRINT.md:50-72` 的样本、概率、EV、CVaR、压力、全现金、NOT_RUN/INSUFFICIENT 状态规则应继续作为研究合同。
* `BLUEPRINT.md:76-86` 的最终留出、purge/embargo、`RESEARCH_EVALUATION` 与 `SHADOW_OPERATIONAL` 分离、`enable_trading=False`/`broker=NOT_RUN` 边界应保留。
* 现有 RB causal-regime、scenario-compare、capital-stress 的工程设计可保留为测量组件：它们有明确 Decimal 账本、hash closure、反例和成本/资金压力输出；不需要重复建立新的封存或管理系统。
* 用户要求的分钟和日线并行、各自训练/校准/评估以及不确定时空仓，在 `BLUEPRINT.md:9-13`、`probability-strategy-v2-plan.md:29-41,123-139,190-196` 中是合理方向，但必须等各自的真实输入和样本门满足。

### 建议改动的部分

1. **把“已看过的 RB scenario 线索”从蓝图的首批交付叙述中单独标成“候选假设生成器”。** Donchian 的正向线索和 z-score 的负向线索应进入下一实验登记，但不应在同一表格中与真实历史 M5 结果并列。新增列 `evidence_role: hypothesis_screening_only`、`sample_unit: overlapping_contract_descriptive`、`not_oos=true` 会减少后续误读。
2. **把 chronological transfer 提升为首个实际研究闸门，但明确它不替代 PIT/OOS。** 该计划能以小成本检测收益是否依赖年份/单合约；蓝图现有“先做获准历史模拟/离线重放”方向可具体化为此一步，而不是直接进入七族×八退出的笛卡尔积。
3. **将输入事实闭合和统计资格分成两个明确状态轴。** 当前文件已有 `BLOCKED`/`INSUFFICIENT`，建议结果 schema 同时写 `data_validity`（PIT/calendar/rules）与 `statistical_status`（sample/calibration/economic gates），防止“工程 complete + data unverified”被读成“研究 complete”。
4. **把 M3A 现有 reconstructed run 作为 failure/insufficiency regression fixture，而不是成功样例。** `published_forecasts=3`、`skipped_labels=5923`、校准不足、SCENARIO calendar 和 fee limitations 应在报告模板中强制显示；任何 policy 读取这些记录时遇 `INSUFFICIENT`/`FAILED` 必须输出 NO_TRADE。
5. **缩小首批 A/B 的默认范围。** M5 计划的 48 policy 单元、完整七族/八退出/六品种仍可作为最终合同，但恢复后的第一真实消费者先做一个已闭合品种、一个频率、两个相反 legacy 假设和固定 baseline；其余矩阵保持 `NOT_RUN`，不因“完整矩阵”压力而用未验证输入填空。这样不会降低最终门，只减少在数据事实未闭合时的浪费。
6. **把模拟交易入口写成研究结果的显式引用链。** 每个 shadow candidate 应绑定：accepted source/lock hash、model/calibration evidence id、exact entry×exit×cost policy、holdout verdict、risk budget、execution hard-review verdict。没有其中任一项就只能 `NO_TRADE`，而不是由 observer/bridge 的工程 PASS 推断可用。

## 可判定的下一步与停止条件

恢复后，Root 可以先执行第 1 项 chronological transfer 的计划审查和独立实现交接；该步骤不依赖 CU，也不要求先运行 full M5。若 transfer 显示结果集中于单一年度、单一合约或成本翻倍即消失，应把该假设记为失败/仅描述性，不扩展模型。若 transfer 通过工程和描述性检查，也只能解锁同一 accepted source 上的真实 PIT/成本闭合与 M5 研究，不解锁模拟交易。

任何研究命令出现 native 非 0、资源超时、必要输入缺失、source/lock drift 或校准不足，都应保留原始结果并按其自身命令契约分类；不要把 `rg` 的 `NO_MATCH` 语义扩展到 tests、fit 或研究 CLI。生成输出先记录不存在，不用故意失败的 Read 探测。报告中的数值均来自已读取文件或本次可复算检查，没有估造总百分比。

## 尚未核实和明确未做的工作

* 没有执行新的 fit、walk-forward、最终留出、M5 full100、模型选择、分钟真实回放或新的数据下载。
* 没有验证交易日历、session、上市/最后交易日、历史 first_seen/available_at、fee/margin/tick/limit 的完整 A 链；R1/RB rounding/CU 事实缺口仍有效。
* 没有读取任何凭据、`.env`、数据库或 TqSdk 账户；没有网络访问、服务/UI/部署、下单或撤单。
* 已有 hard-review PASS 只按它们各自的工程 write-set 和 acceptance clauses 计入；没有把它们升级为策略有效性或交易许可。
* 图谱对 `scenario_backtest.py` 报 metadata_changed、实验脚本报 not_tracked；涉及这些源码的结论已经直接读取实际文件，不能声称图谱覆盖完整。

