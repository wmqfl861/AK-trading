# AK-Trading 成果与蓝图 Astra/max 复评汇总

日期：2026-10-15（Asia/Shanghai）  
性质：用户授权的四条独立 Astra/max 只读复评，不是 ZCode 正式计划审核或产品硬审核。Goal 继续保持 `paused`；本次没有恢复开发、fit、数据采集、数据库/SDK、网络、服务、部署、客户端或交易。

## 结论

目前没有证据把任何策略记为“真实历史数据验证有效”，也没有证据允许进入模拟交易。已有最有价值的成果是 RB 日线 causal-regime、scenario-compare 和 capital-stress 的局部工程产品及独立硬审；它们的证据级别仍是 `SCENARIO` / `CALENDAR_UNVERIFIED`，不提供 PIT、OOS、统计资格或交易许可。

复评确认的最短研究路线是：选定一棵自然、同源、可接受的 RB 输入和 contract lock，闭合 M1/M2/M3A/M3B/M4 的实际消费链，先完成一个双频（MINUTE + DAILY）的真实 M5 A/B，保留完整 required 人口、252/60/60 窗口、purge/embargo、成本与压力场景、连续账本和最终留出边界。结果可以是 `RESEARCH_PASS`、`STATISTICAL_FAIL`、`INSUFFICIENT` 或 `BLOCKED`；任何一个诚实出口都比继续积累计划或工程封存更能回答策略问题。

这条路线不删除六品种、双频率、七类入口、分阶段退出或风险合同，只把“首个可判定研究结果”提前到真实 RB 消费者，并把不被该消费者使用的平台工作移出通用串行门。是否正式采用该排程，仍需后续计划/用户确认；本报告没有授权实施。

## 已确认的成果和边界

- RB causal-regime 硬审报告和本次独立只读复算都支持 51 个账户、7,590 条 ALL 归因、4,785 条 PRIMARY 归因、612 个组；312 个 payload 和 11 个源码 hash 无漂移。复算区间为 2020-01-02 至 2021-01-29。报告明确 `SCENARIO`、`historical_strict_execution=false`、`CALENDAR_UNVERIFIED`、`statistical_qualification=NOT_ASSESSED`。
- scenario-compare 的 14 个共同覆盖合约中，Donchian/MA/z-score 的正、零、负分别为 10/1/3、7/1/6、2/1/11；z-score 在 RANGE 的中位净贡献为 -505.471。这只能生成“继续迁移和优先否证横盘假设”的线索，不能当独立样本或组合收益。
- M3A 历史重建文件存在，但样本和日期块不足，含 `INSUFFICIENT`/`FAILED`；不能接入交易门。M5 full100 尚未完成，snapshot P2 的 25,679 population 在 300 秒预算内超时，peak 121,106,432 bytes，未观察到 MemoryError。
- RB 风险规则包虽有来源/文本硬审 PASS，`qualified_value` 仍为 null、`A_status=UNKNOWN`、`historical_A_assignments=0`、`fit=NOT_RUN`。CU 的 T/R/S(t)、accepted source/calendar/A 和完整 population 仍未建立。

## 必须修复或闭合的事项

1. **P0 代码集成：shadow replay。** `src/ak_trading/v2/validation/experiments.py` 中 `run_shadow_replay_experiment` 读取未定义的 `_reg`；同处 `ledger.checkpoint` 还没有传必填 `exit_registry_sha256` 和 `exit_bindings`。这会在真正进入 shadow 账本时分别触发 `NameError` 和 `TypeError`。按原 owner append-only 修复，并用窄反例验证 registry/binding hash 后再独立 ZCode 硬审。它直接阻断 R5 shadow；若首个离线 A/B 不消费 shadow，则不应成为全项目串行门。
2. **P1 M1 P1 required-test 断言。** 候选 `artifacts.py` 已把 `test_v2_m1_p1_open_contracts.py` 纳入 required map，实际为 42 个文件；`test_v2_m1_artifacts.py` 仍硬编码 41 且 exact set 未加入该文件。现有聚合结果为 184 passed / 1 failed，正式 hard review 未跑。修复必须同步 count 和 exact set，不能只改数字或删测试。
3. **P1 快照性能验收。** 先对 prefix 构造、feature snapshot、identity/hash、展开和回收分段 profile，确认重复逐元素处理的瓶颈；再在原 2 GiB/1 GiB/3,600 秒等合同下重跑。不得只增 timeout、缩人口或删断言；有界探针不等于 25,679 population 通过。
4. **P0 输入和 lock。** 选定一棵自然合版 source tree，闭合 M1/M2 current-lock、calendar/availability、fees/margin/tick/limit、payoff、StrategySpec 与 owner identity。不能把 isolated M1、snapshot M1、M3C P1 的绿色文件静默拼装。
5. **P0 DAILY seam。** 保留独立 `TRADING_DAY20/ATR_1` 背景和缺失/过期时的 `UNKNOWN`；修复代码状态与 work-order relay 状态混淆。当前 scoped PASS 仍是 `INCOMPLETE_NOT_ACCEPTED`，不可放行整条自然链。
6. **条件支持项。** DATA-D6 计划 F01/F02 已有作者修订但 fresh plan review 和产品实现未完成；RUNTIME-R5 仅计划 PASS、`product_review_credit=false`，真实 schema 仍未写入；EX06 的 stale summary pin 要与历史 NP02/NP06 执行缺陷分开修复。这些只有在实际消费者使用时才进入 RB A/B 硬依赖。

## 蓝图调整建议

保留六品种、分钟/日线独立、七入口、PIT/永久留出、成本/压力、整数手、连续账本、未知空仓、`observer=hold`、`enable_trading=False`、`broker=NOT_RUN` 和独立 ZCode 硬审。把现有 RB 三个局部产品明确标成“假设筛选和测量组件”，不要与真实 M5 结果并列。

将最近里程碑改为 **RB-A/B-1：一份同源、双频、完整 required M5 人口的真实研究包**。首批可以固定 TREND/BREAKOUT/PULLBACK，其他入口保留 `NOT_RUN/BLOCKED/INSUFFICIENT`；四个政策和六种场景仍使用同一冻结候选与风险预算。该包必须输出预测、校准、payoff observation、准入漏斗、连续账户、回撤/保证金/尾部、每维度状态矩阵、真实 native exit、冻结 hash，并交独立 ZCode 硬审。

把 `G3→G4→G6` 改为按实际消费者绑定的依赖。APUB 实时发布、EX06、未消费 runtime、远端宏观、客户端 UI、执行适配器、完整 shadow、其它品种的全链接受和 M3C P2–P4 继续保留其原合同与 FAIL，但不再成为所有离线 RB A/B 的通用串行门。若选定输入确实消费其中某项，该项才回到硬依赖图。

状态轴要分开记录：`source_consumable`、`product_accepted`、`research_interpretable`。`PLAN_PASS`、局部 hard PASS、owner native 0、SCENARIO 和静态 capacity 不能跨轴继承 PASS。

## 复评报告和证据

- [策略研究复评](D:/Projects/AK-trading/docs/project-reassessment-20261015-astra-r1/STRATEGY-REVIEW.md) — 178 行，SHA-256 `cd66682a3a1d41dfa1cb45feb5eceffb674816248ed17628e35fc37d4b4452f8`
- [代码与集成复评](D:/Projects/AK-trading/docs/project-reassessment-20261015-astra-r1/CODE-INTEGRATION-REVIEW.md) — 具体记录 `_reg`、checkpoint、M1 exact-set 和 P2 性能事实
- [支持模块复评](D:/Projects/AK-trading/docs/project-reassessment-20261015-astra-r1/SUPPORT-REVIEW.md) — 49 行，SHA-256 `F648A85FE0B8C940C18DF720775122FFBEFD17E9D6F37137372C351593BCC549`
- [蓝图复评](D:/Projects/AK-trading/docs/project-reassessment-20261015-astra-r1/BLUEPRINT-REVIEW.md) — 149 行，SHA-256 `6782A588173CCCB6C326C1A58977463738248AA2C818C5103550B74E12EDCC22`

四份报告均由独立 `gpt-6-astra` / `max` 子代理完成；本轮没有修改既有源码、计划、冻结包或模型配置，也没有启动后续开发。

## 当前停止点

复评已完成，开发仍暂停。恢复后应先决定是否采用“RB-A/B-1 先行”排程；在该决定和相应计划审核/作者处置完成前，不应自动启动新的开发任务或把上述建议当作已授权实现。
