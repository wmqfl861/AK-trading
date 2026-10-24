# 策略优先修复顺序

本文件只列 RB-A/B-1 的最短修复闭环。每项修复仍需沿现有计划→独立 ZCode 审核→原 owner 修复→fresh 复审流程执行；本文件不授实现、数据库、部署或交易权限。

## P0：统一研究消费者输入

负责人：原 M1/M2/R1 owner。  
动作：选择一棵自然 RB source tree，核对 contract lock、calendar、available_at、fees、margin、multiplier、tick、limit、capacity、payoff 和 StrategySpec；生成新的 current lock，保留旧树和失败证据。  
出口：source 可消费、身份一致、实际消费者清单明确；否则状态为 `BLOCKED_BY_SOURCE`。  
研究影响：所有正式 M5 运行的硬依赖。

## P1：修复 M1 P1 required-test exact set

负责人：原 M1 P1 owner。  
动作：在同一有效 revision 中把 `test_v2_m1_p1_open_contracts.py` 同时纳入预期 public 集合、计数和 exact set；保留旧 184P/1F 证据。  
验证：重跑原聚合命令、逐文件 hash freeze、独立 M1 hard review。  
出口：42 项 required map 与测试函数一致；不能只把 41 改成 42。  
研究影响：M1 current lock 未闭时阻止 M5 正式输入。

## P1：解决 snapshot P2 性能瓶颈

负责人：snapshot memory 原 owner。  
动作：先对 25,679 population 的 prefix 构造、feature、identity/hash、公开展开和回收分段 profile；按结果修复重复逐元素构造/展开路径。  
约束：保留原 300 秒、2 GiB、1 GiB output 和全人口契约；不得用增加 timeout、减少人口或删断言替代修复。  
出口：原 P2 required 命令通过，或留下精确的 `PARTIAL_UNVERIFIED`/`COMMAND_DEADLINE_EXCEEDED` 结果。  
研究影响：若 M5 依赖该候选，阻止该候选被宣布可用。

## P0：闭合 DAILY seam

负责人：原 M4 seam owner，M3A 负责概率输入核对。  
动作：完成独立 `TRADING_DAY20/ATR_1` 背景的 context→regime→admission→OPEN→payoff observation→ledger 路径，保留缺失/过期/冲突 `UNKNOWN`。  
验证：正向、反向、未成熟、过期、年龄超限和未来追加反例；修正 follow-up verdict/schema 和 relay 的实际工单证据。  
出口：代码状态和流程状态分别达到可消费；不能把 scoped PASS 自动升级为整链 PASS。  
研究影响：RB 双频 A/B 的 DAILY 硬依赖。

## P1：闭合概率、策略和连续账本接口

负责人：M3A/M3B/M4 原 owner。  
动作：在 current lock 上核对预测、校准、payoff、entry/exit binding、cost policy、risk budget 和 ledger identity；不扩展新模型和新退出族。  
出口：MINUTE/DAILY 首批三族可以产生真实、可复核的候选和 `NO_TRADE`/`INSUFFICIENT` 状态。  
研究影响：M5 full100 的直接前置。

## P2：按消费者决定是否修 R5 shadow

负责人：R5 原 owner。  
动作：若 RB-A/B-1 或其验收明确消费 shadow，修复 `experiments.py` 中未定义 `_reg`，并为 `ledger.checkpoint` 传入真实 `exit_registry_sha256` 和 `exit_bindings`；加入缺 hash/错误 hash/缺 binding 反例。  
出口：冻结实际 write-set 后独立 ZCode hard review PASS。  
研究影响：只阻消费 R5 shadow 的路径，不成为全项目通用门。

## P0：执行首个真实 M5 A/B

负责人：唯一 M5 owner。  
动作：在上述硬依赖通过后执行 MINUTE/DAILY required population、三个首批策略族、四个 policy 和六个 scenario；固定候选，不读取最终留出。  
出口：产生真实研究包，状态为 `RESEARCH_PASS`、`STATISTICAL_FAIL`、`INSUFFICIENT`、`BLOCKED` 或 `ENGINEERING_FAIL` 之一，并完成独立 ZCode hard review。  
研究影响：这是本批的完成条件。

## P3：准备模拟交易 acceptance

仅在 M5 研究包完成并具备 execution hard review PASS 后进入；按原 20 个交易日 feed/clock/shadow、send/cancel=0、observer hold 和用户 UI 授权门执行。实盘订单仍需后续单独授权，不属于本修复顺序。

## 禁止的捷径

不把已有 SCENARIO 或局部工程 PASS 改写成历史策略 PASS；不把超时改称 OOM 或成功；不删样本、缩短窗口、取消 purge/embargo、提前读取永久留出；不把 shadow、APUB、EX06、远端宏观和全品种扩展重新设成 RB-A/B-1 的无条件共同前置；不为此批新建管理或封存体系。
