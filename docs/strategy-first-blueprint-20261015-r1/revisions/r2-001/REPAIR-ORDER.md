# 策略优先最短修复队列：r2-001

本表是[实施计划](IMPLEMENTATION-PLAN.md)的排程摘要，沿该文件 §1 的具体模块计划、source lease 和 filewise release 执行，不创建新工单体系。**开发 pause 仍在；本次不启动修复、测试、研究、模拟、SDK 或账户操作。** 恢复后只处理实际消费者必需缺口。

## 1. 并行准备，ready 即交付

| 队列 | 原 owner 与动作 | 消费影响/交付 |
| --- | --- | --- |
| 数据与缺口 | M1/M2/R1 按事前覆盖、成熟度、日历/费用、availability、seen 记录选自然源；先 RB，不足再按同规则看其他品种。 | 各频率分别交 accepted input/lock 和缺口；不按收益选源，不因 RB 不足无限等待。 |
| 必要代码/性能 | 仅所选来源确实使用的 M1 P1、snapshot 或 M3A/M3B/M4 缺陷进入主线，其他候选保持原状态。 | 各节点独占写集并行；公共文件依原计划顺序，不拼接局部 PASS。 |
| 策略报告 | 唯一 M5 owner 准备并执行 ready 的频率。 | MINUTE 不等 DAILY、DAILY 不等 MINUTE，先交单频率；A/B 总批仍需两份。 |
| 模拟消费准备 | 复用既有账本/入口核对出口、NO_TRADE、feed/clock 记录接口。 | 不相干写集可提前；修改 M5 共用文件须等该文件释放，不另造平台。 |

## 2. M1 P1：只阻消费此候选的路径

沿 [formal-adoption-001/PLAN.md](../../../strategy-research-blueprint-20261010-r2/implementation-clarifications/m1-p1-open-public-successor-20261014/revisions/r2/formal-adoption-001/PLAN.md)的原 M1 owner 和实际有效 revision 修复。旧 required map 已是 42，测试仍断言 41，exact set 还漏 `test_v2_m1_p1_open_contracts.py`；原 184P/1F、native1 保留。

同时同步 public 集合、计数和 exact set/测试名，重跑原聚合人口并独立硬审；只改 41→42 不够。只有实际消费者需要这项公共增量时才合版；已合法接受且不消费它的来源不等该候选。

## 3. snapshot P2 与 M5 full100：两种预算不能互相套用

| 权威记录 | 事实/原约束 | 修复出口 |
| --- | --- | --- |
| `D:/AKS261014/M1/snapshot-provenance-r1/owner-evidence/runs/p2-001/RESULT.json` 及该支持命令的原工作单 | 25,679 population；该次命令 deadline 300s，elapsed 308.719s，native1，COMMAND_DEADLINE_EXCEEDED；peak 121106432 bytes、未观测 OOM。 | 支撑人口仍 PARTIAL_UNVERIFIED；分段定位 prefix、feature、identity/hash、展开/回收成本，按其原命令预算重新验证。 |
| [SNAPSHOT-MEMORY-HANDOFF-002.json](../../../strategy-research-blueprint-20261010-r2/implementation-clarifications/m5-ab-experiment-plan-20261014/SNAPSHOT-MEMORY-HANDOFF-002.json) | `unchanged_memory_limit_bytes=2147483648`、`unchanged_output_limit_bytes=1073741824`、`unchanged_full100_timeout_seconds=3600`。 | original full100 保留原人口和 2 GiB/1 GiB/3600s 门，不能用 P2 的 300s 覆盖。 |

二者是不同命令/阶段，不据此宣称原报告必然矛盾。实际派发从权威记录绑定预算；不得机械写通用 300 秒，也不得提高 timeout、缩人口、删断言后把原失败改成成功。有界诊断不等于正式全人口通过。

使用该候选时，沿 [snapshot lease](../../../parallel-development-20261004-r1/coordination/m1-snapshot-provenance-root-source-lease-20261014-001/LEASE.json)和原 M1 接受流程：accepted M1 B delta → accepted additive P1 delta → accepted memory support exact hunks → unified source compatibility/admission → original full100 → frozen actual integration hard review。只在所选消费者需要时推进，不将这个候选变成所有回测的共同门；不得用支持 lease 扩权 fit/generation。

## 4. DAILY：复用已修实现，关闭必要流程缺口

原 M4 owner 复用 scoped 已修源和对应审查，M3A 核对 `TRADING_DAY20/ATR_1` 概率接口。已知 scoped PASS 与 `INCOMPLETE_NOT_ACCEPTED/NOT_RELEASED` 分别记录，只闭真实消费所需 work-order/relay 项，不重做已修 seam、不重审未变内容。

保留自然 context→regime→admission→OPEN→payoff observation→ledger、年龄门和 UNKNOWN 拒绝。若发现选定 source 有新的兼容缺口，拿精确证据退原 owner；该缺口只阻对应 DAILY 路径，不阻已 ready MINUTE。

## 5. 真实 M5 A/B：逐频率执行，不等待无关平台

沿 [M5 A/B PLAN](../../../strategy-research-blueprint-20261010-r2/implementation-clarifications/m5-ab-experiment-plan-20261014/PLAN.md)和实际操作 admission，使用三族、四政策、六场景及完整必需人口；八期限、252/60/60、PIT、purge/embargo、成本/压力和连续账本保留。`walk-forward`、`RESEARCH_EVALUATION`、`final_holdout=false`；最终 holdout 另阶段。

各频率在自身条件就绪后立即运行、产报告及对应审查；结果为研究支持、STATISTICAL_FAIL、INSUFFICIENT、BLOCKED、ENGINEERING_FAIL 或 NOT_RUN，各轴实报。报告状态不替代独立 ZCode 产品硬审 PASS。不存在“必须收益为正才算交付”；未运行、场景数据或空训练结构也不能冒充真实完成。

## 6. R5 与模拟：按实际入口续接

若选用 ordinary shadow，先按既有 [RESEARCH r2](../../../parallel-development-20261004-r1/plans/RESEARCH/r2/PLAN.md)处理真实闭包：未定义 `_reg` 导致 NameError；checkpoint 缺 `exit_registry_sha256` 导致 TypeError；默认 `exit_bindings=()` 的缺绑定属于完整身份问题。验证真实 registry/bindings 与 ledger、checkpoint、runner 一致，以及缺/错 hash、缺 binding 的反例；不只检查“不抛异常”。

`experiments.py` 归唯一 M5 writer；R5 在 M5 释放后顺序续接，若换 owner 先显式 file transfer，禁止并发修改。该项不阻不消费 ordinary shadow 的 walk-forward；其他模拟准备可在不重叠写集并行。

历史回放/本地虚拟撮合按 RESEARCH_EVALUATION，在所消费代码/输入与运行权限 ready 后可先开始。前瞻 paper/shadow 也在自身运行条件就绪后开始积累 feed/clock 记录；统计 FAIL/INSUFFICIENT 的 SHADOW_OPERATIONAL 仍 NO_TRADE。原 20 交易日、时延/时钟等只约束完整 SHADOW_ACCEPTED，不是开始记录门。

execution hard PASS、部署/模拟真实 send/cancel=0、明确用户授权及 UI 开启属于真实订单/撤单门。真实执行适配器/EX06 仅在具体客户端/路径消费时前置；本批默认交易关闭、broker NOT_RUN，复用现有离线入口，不调用 SDK/账户/配置。

## 7. 停止范围

真实输入/代码/lease/运行权限缺失、活动审阅冻结、source 漂移或必要硬审 FAIL 时停对应消费者，其他 ready 队列继续。FAIL 回同一 owner 追加修复，新冻结后立即独立复审；不足保留不足，旧失败与既有审查只读。不得把 APUB、EX06、通用 runtime、宏观、全品种或所有候选合版重设为共同前置。
