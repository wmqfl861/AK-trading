# 原全局审核修复计划进展核对（2026-10-15）

**直接回答：修复已经做了，其中若干项已经开发并通过独立产品硬审；但原全局修复计划没有全部完成。** 不能把“所有修复完成”作为当前状态，也不能说“只改了蓝图、完全没修代码”。下面沿原 MW01—MW12 逐项核对，并把后继增量分开列出。

本次只读核对，未开发、运行测试、访问网络/DB/SDK/凭据或操作部署。原计划、原报告和已审包均未修改。最新职责为 Astra/max 负责计划与必要裁定，ZCode（GLM-5.3/max）负责实际开发、另一个独立 ZCode 做产品硬审；原 Sol WIP 的历史作者身份保留，停止交接不算完成。

核对基准是 [20261010 全局 workstreams.json](../development-blueprint-20261010-r1/workstreams.json) 的 12 组 `modification_work_orders`，并对照 [原修复积压](../strategy-research-blueprint-20261010-r2/REPAIR-BACKLOG.md)、[20261015 复评汇总](REASSESSMENT-REPORT.md)、当前 `PROJECT-STATUS.md` 顶部增量和最新 verdict/接收 JSON。当前顶部终态优先于其下保留的“运行中”历史段；上午复评的暂停状态不代表最新 Goal 仍暂停。

“已完成”只用于相应精确修复范围已获独立产品 PASS；计划 PASS、自测通过、审核启动、代码交接均不算产品完成。`UNKNOWN` 表示本次有限核对未取得该项新终态，不能反推绝无开发，也不能计为已完成。

| 原修复组 | 实际完成了什么 | 仍未闭合什么 | 本次状态 |
| --- | --- | --- | --- |
| **MW01：SC G3/G4/G6 与共享研究依赖** | 原 SC scoped PASS 保留；后继 M1 public B、M1 P1 公共增量已取得接受证据，M1 P1 独立硬审 PASS。 | 原 G4 发布/终态 lock→G6 最终 M2 消费及完整接受仍没有本次可据以闭合的终态；当前 G4 有资源等待。M1 P2 真实 300 秒超时，M5 full100 未启动。 | **部分已修，整组未完成** |
| **MW02：DISPATCH H3/H12** | 实际修复已经完成；独立 reviewer 已签无条件产品 PASS。必要 45+7+11 验证、重建链和原 candidate verify-final 13 项均有独立执行记录。 | Root 接收仍待原 reviewer 对 `read_file_too_large` 和 cx-001 原生退出码与文字差异作最小事实补充；不能把 reviewer 的 PASS 和 Root 最终接收混为一项。 | **产品 PASS，接收未闭合** |
| **MW03：MASTER 六缺陷与八场景** | **六缺陷代码修复已完成并硬审 PASS**：完整 99 项（原 84+新增 15）及 reviewer 新写的 87 个反例/控制全部通过。 | verdict 明确不授八个真实发行场景、完整 MASTER 发布信用；历史 L01 仍 `INCOMPLETE_EVIDENCE`。 | **六缺陷已完成；原整组仍部分未完成** |
| **MW04：DATA-D1 materializer/helper** | 已形成修复裁定、隔离工作和 helper WIP；原已通过 source/suite 保留。 | 六 helper 入口增量停止于交接，未完成独立产品硬审；原 11 helper/34 output 及 full 阶段不能因 WIP 而计完成。 | **有实现，未验收** |
| **MW05：Q00 Q-H09 漏 payload** | 原 FAIL/缺件证据保留。 | 本次未取得 fresh bounded revision 的完整包及独立产品 PASS；无法确认原两 payload 漏项已闭合。 | **UNKNOWN；不得计完成** |
| **MW06：D2 / session-labels 审核与采用** | 原 D2 8+2 和 labels 实现/自测/G3 证据保留。另有 DATA-D6 15 source+4 helper 新增量已硬审 PASS。 | **D6 的 PASS 不覆盖原 D2/labels**。原完整产品接受、获准 PG 检查、G5/采用的最新终态本次未核到；原 skip 不能转 PASS。 | **D2/labels 整组未确认闭合；D6 局部已完成** |
| **MW07：APUB malformed publisher/F2 与 DATA 采用** | 原 guard 已独立 PASS；Root 最新转述的原作者复核确认，被引用的历史审核 artifact 缺 revision，不能据此要求再修一个已确认的当前 publisher 缺陷。 | 该条旧“malformed publisher F2”不能继续当当前已确认代码 bug；selected DATA 组合采用仍须自己的证据，不能由 guard PASS 代替。 | **旧缺陷归因需纠正，不应新增该修复；采用未确认** |
| **MW08：EX06 同版集成** | 修复与集成确实推进，C1/Root binding 后已完整跑到 C2 56 phase 终态；当前汇总为 **53 native 0 / 3 native 1**。 | NP04 阶段的两项 S2 expiry、raw-sink E 的 queue-full 645、M11 cancel0 未闭合。NP04 单文件有 WIP；另两项窄工作单已冻，尚无修复硬审 PASS。完整 EX06 未接受。 | **已实施并实测，仍有失败** |
| **MW09：RUNTIME-R5 / QMH** | 清理修复有实际代码，owner 27/27、cleanup 7/7 的结果保留；独立 reviewer 也复现 cleanup 7/7。 | 独立必需 controls 为 **22/27、native 1**，正式 verdict **FAIL / RF01**。不能以 owner 通过或“主机繁忙”解释自动消除 FAIL；case05/08 有限诊断裁定有效但尚未执行，旧等待根因仍未确定。 | **有实现，独立硬审 FAIL，未完成** |
| **MW10：MACRO producer / remote / cards** | 本地 MR2 的历史有限 PASS 保留，producer r5 原冻结自测保留。 | 本次未取得覆盖原 r3 F01 的 r5 新独立产品 PASS；远端/cards/宏观增量也无新接受终态。 | **UNKNOWN；不得计完成** |
| **MW11：EP1 / FPC / 真实研究集成** | M1 P1、DAILY 六文件、M3B live-clock、RB chronological-transfer 等精确增量已硬审 PASS；见下表。 | M1 P2 超时、full100 未启动；M4 新修复九文件验证超时；真实 M5 A/B、原 FPC final run/copy 和整研究闭环未完成。RB SCENARIO 不能替代这些。 | **多个增量完成，最终研究交付未完成** |
| **MW12：版本/资格/feed/shadow/S3 等** | 个别工具和历史有限范围接受继续有效；forward-paper 六文件宿主已有 WIP。 | 宿主未完成产品硬审，完整 feed/shadow/S3 与所选实际消费者采用没有本次新完整接受。首单仍未授权；没有证据可签整组完成。 | **有 WIP及 UNKNOWN 项，整组未完成** |

以下是可明确确认的已完成增量，不能因它们不等于整组完成而抹掉真实开发成果：

| 已完成的精确范围 | 直接证据与验证事实 | 不覆盖的范围 |
| --- | --- | --- |
| **MASTER 六缺陷** | [独立 VERDICT](D:/AKR261015/MASTER6/20261015T101030Z-4c799603/evidence/VERDICT.json)：`PRODUCT_HARD_REVIEW / COMPLETE / PASS / unconditional=true`；99 项和 87 个独立反例。 | 八真实场景、完整发行、L01 历史证据。 |
| **M1 P1 public contract 九文件** | [独立审核报告](../parallel-development-20261004-r1/dispatch/aktr-zcode-m1-p1-open-public-hard-review-20261015T073638Z-0ff79d04/OWNER-REPORT.md)：P1 118/118，完整 M1 185/185，M2 causality 18/18，118 个 reviewer 检查；无条件 PASS。 | consumer runtime、checkpoint embedding、lock/publication、完整 P1/M1/M3B/M5。旧 41/42/集合错误不应继续写成该冻结候选当前尚未修好。 |
| **DAILY seam 六文件及必要接收补充** | [follow-up 002 receipt](../parallel-development-20261004-r1/dispatch/aktr-zcode-daily-mainline-acceptance-followup-20261015-002/VERDICT-RECEIPT.json)：ZCode product verdict `PASS`，six-source/snapshots unchanged，current failed tools=0，mechanical relay `COMPLETE`；从该 JSON 取得 verdict 路径并核对 bytes/SHA-256 一致。 | full v2、真实 M5 fit、历史 PIT/统计资格。旧 `INCOMPLETE_NOT_ACCEPTED` 已有新终态，不能当当前仍未处理。 |
| **M3B live-clock 五文件增量** | [Root 接收](../parallel-development-20261004-r1/coordination/live-clock-runtime-terminal-root-reception-20261015-001/RECEPTION.json) 与其指向的真实 verdict：`INDEPENDENT_PRODUCT_PASS_EXACT_FIVE_FILE_DELTA_ONLY`。 | forward-paper 宿主、payoff 后继、真实三族前瞻策略验证。 |
| **DATA-D6 15 source+4 helper 隔离增量** | [Root 接收](../parallel-development-20261004-r1/coordination/rb-d6-root-acceptance-20261015-001/ACCEPTANCE.json) 与其实际 verdict：无条件产品 PASS。 | full-D6、LIVE/PG、采用、D2/labels 全节点。 |
| **RB chronological-transfer 三文件** | 同一 [RB/D6 接收](../parallel-development-20261004-r1/coordination/rb-d6-root-acceptance-20261015-001/ACCEPTANCE.json) 明确 accepted exact frozen product；真实独立 verdict PASS。 | 仍 `SCENARIO/CALENDAR_UNVERIFIED`，无 PIT/OOS、策略资格或交易信用。 |
| **R1 MOL24：min_open_lots 单字段** | [独立 VERDICT](D:/AKRV261015/MOLH1/r/VERDICT.json) 与 [审核报告](../parallel-development-20261004-r1/dispatch/aktr-zcode-r1-min-open-lots-product-hard-review-20261015T114752Z-f8824dec/OWNER-REPORT.md)：24 文件无条件 PASS，三命令重放 byte-identical，26 个独立反例。 | 剩余七未知字段、完整输入集合、TradingRule、readback/capacity/fit；collection 仍 false。 |
| **R1 CU003：24 文件来源事实增量** | [独立 VERDICT](D:/AKRV261015/CUMH1/VERDICT.json) 与 [审核报告](../parallel-development-20261004-r1/dispatch/aktr-zcode-cu-manual-product-hard-review-20261015T114806Z-e41a9875/OWNER-REPORT.md)：无条件 PASS；三命令重放及来源链/25 个引文检查完成。 | 历史 A、L/X/T/R/S、完整 census/collection/fit；Jina 转换不等于原 PDF 字节。 |

还没完成的最近工作，按真实状态列明；这里不新增计划，只指出已有工作尚欠什么：

- **M1 P2 性能**：正常完整人口实测仍在原 300 秒预算超时，FAIL 保留。新的窄性能计划已冻待计划审，实际新修复未启动；M5 full100 也未启动。
- **M4 新 payoff 修复**：两文件 WIP；新增 26、完整 payoff 80、gates 21 已过，但原九文件 combined 600 秒超时。新的原九文件分组命令（每条 600 秒）已有授权，后继 ZCode 开发尚未启动，未硬审 PASS。这与已 PASS 的旧 DAILY 六文件不同。
- **D1 helper、R1 lot_step、EX-SERVICE NP04、forward-paper 六文件宿主**：都有 WIP/交接事项，没有新无条件产品硬审 PASS，交接 ZCode 后仍须完成原验证与独立审。
- **DISPATCH**：只欠针对现存工具失败和退出码文字差异的原 reviewer 补充及 Root 接收；当前没有理由把整个已审实现重新做一轮。
- **EX06**：剩三失败 phase 内的明确问题，NP04 已有 WIP，raw-sink/M11 是新窄修复待实施；不是再重做所有 56 phase 的计划。
- **RUNTIME-R5**：RF01 真实 FAIL 未关；先完成已裁定的 case05/08 有限诊断，再依据实际证据决定修复/必要重审。不可把根因猜测当通过。
- **Q00、原 D2/labels、MACRO r5、八真实 MASTER 场景和完整采用/资格**：本次有限核对未取得新闭合证据，保持未确认；不拿后继相似模块的 PASS 代替。

结论可以对用户直接表述为：**“做了，MASTER 六缺陷以及 M1 P1、DAILY、M3B 时钟、D6、RB 转移和两项 R1 输入增量已完成各自硬审；但全局还没修完。现在未闭的主要是 M1 P2/M5、M4 新 payoff、D1/lot_step/宿主、EX06 剩余失败和 RUNTIME RF01，另有几个原计划项目尚未核到新终态。DISPATCH 是已获产品 PASS、接收补充未完；APUB 那条旧缺陷经核对不应继续作为新修复。”**

证据范围说明：原 12 MW 已逐项读机器台账。MASTER/MOL24/CU003/DAILY 直接读取实际 verdict；RB、D6、live-clock、RUNTIME 的 verdict 路径、bytes、expected hash 从最新接收 JSON 程序化提取并核对，四份全部一致，DAILY verdict 也核对一致。没有重哈希全部审计包或重跑产品。APUB 原作者结论、M1 普通 P2 最新失败、M4/EX06 最新终态及 WIP/新 ZCode 尚未启动状态采用 Root 本轮已核实事实，明确不是我独立重跑得出的结论。

命令退出纪律沿 `docs/maintenance/20261004-rg-no-match-r1/INCIDENT.md` 和 `docs/maintenance/20261004-grok-headless-cancel-r1/INCIDENT.md`：本次发现命令逐项保留 raw exit/stdout/stderr，`rg --files` 均 raw0、stderr 空；不存在目录先判存在性；生成报告先确认尚未创建。本次没有把测试/CLI 非零解释为 NO_MATCH，没有将计划 PASS 记作产品 PASS，没有虚构完成百分比。
