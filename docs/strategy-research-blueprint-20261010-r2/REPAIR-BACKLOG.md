# 面向下一实验的修复积压

版本20261010-r2；本轮为文档重排，未运行产品或修复源码。目标与首批矩阵见[蓝图](BLUEPRINT.md)，源owner/验证/衔接见[实施计划](IMPLEMENTATION-PLAN.md)。状态事实沿用已审核[20261010-r1现状及证据](../development-blueprint-20261010-r1/REVIEW-AND-PLAN.md)和[原32线/支持切片台账](../development-blueprint-20261010-r1/workstreams.json)；这两份冻结历史不回写。当前未核实项保持UNKNOWN/STALE，不把读取日当运行日。

## 1. 排序原则及当前P0

四类表示当前研究调度：**直接研究关键**是A/B实际消费的输入、生产和评价；**条件依赖**只在选定实验确实消费该产物时前置；**后置**等待明确后续研究/影子/部署消费者；**停止扩张**停止把管理或通用能力不断增加为研究共同前置。后两类不豁免原合同、不关闭FAIL，也不取消有效修复计划。发现实际消费缺陷立即提升给原owner，不用“可先研究”绕过。

| 顺序 | 真实缺口、现状与范围 | 负责人、解除动作与出口 |
| --- | --- | --- |
| P0-1 输入/同源交付 | 当前合格真实RB双频覆盖未建立；SC-M1源304/三工具PASS及SC-M2准备PASS，不等于实际G4/G6 | Root按有效G3/G4发布并取得终态；原M2 owner exact304、final run、原H01–H08/D02、新完整硬审；数据owner只补选定输入确切缺口 |
| P0-2 DAILY天然链 | 已读主线及容量/SC候选仅MINUTE background，DAILY可能UNKNOWN拒绝所有自然OPEN/OBS；本次最终selected source状态仍UNKNOWN | 本轮已裁决首批DAILY5用独立DAY20/ATR_1背景；M4核source后修pipeline，M3A/M3B各修自己的接口；天然正向/冲突/缺失/成熟/过期/未来反例、真实DAILY fit及硬审，不能强置ALIGNED |
| P0-3 EP1实际fit | source/capacity scoped PASS；generation/registration/fit/lock未获执行信用；容量三test lease禁fit/generation | 沿既有RESEARCH r2/EP1计划补精确操作admission；原producer与唯一M5 writer真实PF/fit/观察漏斗；不拿合成recipe当RB数据，不以首条观察发READY |
| P0-4 所消费M3A–M4 | M3A十二文件仍review-preparation/admission pending；M3B仅plan PASS、P0 lock/H10/ownership/来源未闭；所选完整组合接受未建立 | 各原owner解决实际来源/合同差异、验证冻结，独立ZCode审核所消费源与风险；不复制旧PASS或伪造开发前baseline |
| P0-5 下一实际实验 | CLI已有两配置选三族能力，但真实配置/输入/run尚未执行；预测/模型/账户无实际新结论 | 唯一M5 owner冻结A/B两config、独立run_id/split_id、四政策及六情景，真实walk-forward交双频结果/失败与48单元状态；不新增研究框架 |

P0任务分给原所属模块并行：SC交付、DAILY窄修、M3A/M3B来源与review准备、M5真实配置/split/EP1 admission准备可同时推进。昂贵fit/独占源只守真实资源和write-freeze门。R4消费前必须收齐其真实依赖，不能用prepare包或日志替代；不要求所有旧MW先PASS。

下一增量只为预登记C/D/E0/E1：十因子同样本配对；M5显式`legacy/trailing/time_decay`绑定接线及匹配payoff；E0原LEGACY回归/网格/有限马丁在A/B后按自身完整依赖就绪与C/D并行，不等未消费的新trailing/time/P2。E1新增退出/部分数量组合仍按P1/P2顺序，消费新P0仍守门；EP1 OPEN_INITIAL不能充作ADD/多层证据。训练全无模型或空观察保持当前工程FAIL出口，实际合法OOS全现金允许完整报告。选定CLI若保留正常walk-forward与StageIncomplete都exit3的实现，按实施计划R4保存精确原生码并由M5 owner窄修，工程/统计/阶段分开。shadow `_reg`/exit pack缺陷只在R5前修；远端宏观、通用运行和客户端不抢A/B前置资源。

## 2. 原12个MW的处置

不把12项换名后全部放在首个实验之前。下表逐项给当前类别；复用原有效plan/精确lease，不重新建立一轮统一规划。

| 原MW | 当前类别 | 本版具体处置及原义务 |
| --- | --- | --- |
| MW01 SC G3/G4/G6 | 直接研究关键 | 实际共享依赖交付，按已有arbitration即时G4→G6；三工具/prepare的PASS不复审同范围 |
| MW02 DISPATCH | 停止扩张 | H3/H12 FAIL保留；不新增统一调度工程或设所有研究共同门。若真实选定dispatch工具消费此缺陷，原owner先修该范围 |
| MW03 MASTER-B-TOOLS | 停止扩张 | 六缺陷/r2六文件修复及八真实MASTER场景保留；A/B不走该发行工具则不前置，不扩大成新管理平台 |
| MW04 DATA-D1 | 条件依赖 | 只有当前输入需要未物化产物才前置；原one-use native1 collision保留，原owner新revision修，旧成功命令不重放 |
| MW05 Q00 | 停止扩张 | Q-H09漏两existing payload仍FAIL；按原包修复，不设通用Q00/递归seal门；实际消费相关包时不能忽略不完整性 |
| MW06 D2 / labels | 条件依赖 | 当前输入用D2/labels则立即审已冻结实际写集；D2原8+2 DONE、labels实施/G3已完成，PG/主线apply另守门；不用则后置其review |
| MW07 APUB / DATA采用 | 条件依赖 | 选定来源若需真实publisher，malformed/F2必须先修；纯合格FrozenInput不等待未消费的实时组合 |
| MW08 EX06 | 后置 | 执行同版集成与S3队列；保持外部stopped delta→C1→Rootbind→C2→full review，不作为离线账本研究前置 |
| MW09 Runtime transfer | 后置 | 10-09 transfer立即有效，未来按行政撤销→321监督snapshot→LC并行/WC后继；不重复计划审核，不等失联旧producer STOP；实际研究若消费相应runtime再按原门前置 |
| MW10 MACRO producer/remote/cards | 后置 | r5冻结但r3独立F01 FAIL未关闭；MR2本地PASS可由自身消费者采用，不等远端；首次价格研究不等宏观全链，增量研究消费前完整修审 |
| MW11 EP1/FPC/research | 直接研究关键 | EP1实际fit与所选研究组合P0；FPC最终run/copy仅在该入口实际消费时依原plan完成，P2 PASS不冒充正式运行 |
| MW12 version/qualification/feed/shadow/S3 | 后置 | 只收当前消费者源/资格；完整feed/shadow/S3各自后继，NF/KEYSET等停止新增通用前置。原六品种首版范围与现场门仍保留 |

## 3. 原32工作线逐项分类

每条保留原ID，不用百分比或新增分母制造进度。有限工程PASS、owner自测、计划PASS、采用/部署及统计资格不同；此表中的PASS全部是已有原证据范围，不是本轮复跑。

| 原ID | 当前类别 | 当前事实及实际激活条件 |
| --- | --- | --- |
| ACQD1 | 条件依赖 | 历史finite PASS、采用UNKNOWN；选定历史输入若消费其转换，绑定接受字节/来源，否则等数据采用 |
| APUB3 | 条件依赖 | F1 guard scoped PASS，实际malformed publisher/F2未闭；当前来源用该包则修审，不推全publisher PASS |
| P05O2 | 条件依赖 | 分钟消费者历史finite PASS、handover skip保留；实时报文/producer接入或该输入消费者需要时激活 |
| WAL2 | 后置 | 历史finite PASS、采用UNKNOWN；执行集成消费时选版，不挡现有离线M4账本 |
| SHCP5 | 条件依赖 | E9 unconditional scoped PASS，E8 FAIL保留；ordinary shadow恢复消费时接入，不外推真实feed/统计资格 |
| MR2 | 后置 | 本地原98/修订50/4probe独立finite PASS；自身采用不等producer；宏观增量需要时消费 |
| FPC | 条件依赖 | FPC3 P2 PASS；final run/copy/publication/full-node NOT_RUN；所选正式运行入口消费它时补实际交付 |
| RUNTIME-R5 | 后置 | source四反例FAIL、transfer plan已采纳；未来按有效321隔离路线推进，不把通用运行闭环设A/B前置 |
| MH02 | 后置 | 当前采用/交付UNKNOWN/STALE；客户端及执行同版消费者到来时核精确源与dual hash |
| MH03 | 条件依赖 | 历史静态finite PASS，实际窗口UNKNOWN；仅所选命令原合同要求的资源/运行窗口前置 |
| MH04S3 | 条件依赖 | r4-003 SOURCE_STATIC/有限执行PASS，product NOT_ISSUED；实际消费其收尾路径时按原范围验证 |
| MH05 | 条件依赖 | 当前UNKNOWN/STALE；实际研究命令若要求该运行附件，补具体缺件，不等全runtime组合 |
| KEYSET | 停止扩张 | 固定集合/身份UNKNOWN；停止增加通用集合管理工程，实际命令所需原identity仍必须满足 |
| EP1 | 直接研究关键 | source/capacity PASS、实际fit未运行；P0真实训练、分区及观察生产，不能扩大capacity lease |
| P4 | 后置 | 历史收敛接收不等发布；M3C P4等P0–P3及嵌套prefix-only条件收益，未完成不得启用 |
| SNAPSHOT | 后置 | 客户端生命周期UNKNOWN/STALE；EX06/S3消费者需要时核同版，不参与A/B离线输入 |
| DP-RELEASE | 条件依赖 | D2原8+2 DONE，仅source-intake PASS，full review未跑；当前输入消费DP组合时前置相应接受 |
| DATA-ADOPTION | 条件依赖 | ACQD/APUB/P05/DP当前组合UNKNOWN；用该组合才完整接收，不挡另一已合格冻结历史路线 |
| RESEARCH-INTEGRATION | 直接研究关键 | shared delivery open；G4/G6、M3A/B真实源/合同及R3/R4接线后审实际消费闭包 |
| EXECUTION-INTEGRATION | 后置 | EX06 C2失败、全同版未接受；不以真实执行域代替离线模拟账本，后续按原固定集成顺序 |
| DATA-QUALIFICATION | 条件依赖 | partial/PG资格UNKNOWN；选定输入必须证实历史资格，PG/SDK现场门只约束其真实消费者 |
| RUNTIME-QUALIFICATION | 停止扩张 | QMH tools-only PASS、158-node闭环未真完成；停止万能native/通用资格前置，保留每个实际命令原要求 |
| FEED-QUALIFICATION | 后置 | 实际完整20日UNKNOWN；完整shadow消费时原品种/会话/活跃合约范围全验，历史查询不顶替 |
| FORMAL-RESEARCH | 直接研究关键 | 先真实开发期walk-forward及否证，冻结候选后才最终留出；当前full正式run UNKNOWN，不让后续留出预消费 |
| SHADOW-ACCEPTANCE | 后置 | SHCP有限PASS非完整shadow；等formal/feed/clock及R5真实接线、生命周期与恢复 |
| NP06-S3 | 后置 | 同版部署模拟UNKNOWN；等执行hard PASS及原运行门，真实send/cancel=0，UI由用户操作 |
| NP07 | 后置 | 首单无本轮授权；所有原hard review/S3门、明确用户授权与UI开启仍必要 |
| ECON-PRODUCTION | 后置 | 10-01零报告是历史，当前远端UNKNOWN；需要宏观增量时先获准只读核实，不盲重启/重登记 |
| MACRO-CARDS | 后置 | producer未独立接受，backendUNKNOWN/narrativeUNVERIFIED/historicalPIT false；可靠报告源及本地消费者接受后前瞻积累 |
| MACRO-VALIDATION | 后置 | 有/无宏观OOS未验证；基线后做单变项前瞻对照，不以专家confidence当预测胜率 |
| NF-DELIVERY | 停止扩张 | 现有API/基准/风险说明接受UNKNOWN；停止先扩交付平台，真正消费者需要的原功能/风险说明仍须交付 |
| RESEARCH-INPUT | 直接研究关键 | fixture窄VERIFIED/SCENARIO及recent-data不等完整PIT；P0选定真实输入、合法使用范围/费用/日历/成熟度 |

## 4. 保留的具体FAIL与条件工作

| 事项 | 必须保留的失败/未验证事实 | 原owner的后续动作及与首批关系 |
| --- | --- | --- |
| DATA-D1 | continuation materializer native1，预写COMMAND.json被absence collision；one-use已消耗，11helpers/34outputs未物化 | 用新bounded revision修前置、原full验证和独立review；只在当前输入需这些产物时前置 |
| DATA-D2 / labels | D2 558=547P+11原批准PGskip、BUILD/VERIFY0 CLEAN117/117；labels 42P+6PGskip/24P+3PGskip、Root G3 PASS；均无新完整产品hard PASS | 选定数据消费则立即独立review；PG事务九skip、DB/mainline/apply NOT_RUN保留，不用自测替代审，不强加未消费PG门 |
| DISPATCH | 实际独立H3/H12 FAIL，continuation“无usable verdict”与原件矛盾；5/10/42仅owner证据 | 原owner修行为、原计划作者处置矛盾，fresh review；选定新派发不使用缺陷工具时不阻A/B |
| MASTER-B-TOOLS | 六反例：partial首因、junction、blocked raw exit、skip reason、deselection、cwd；绿84不关闭FAIL | 沿既有r2六文件修审，真实八scenario另验；停止给A/B附加管理发行工具门 |
| Q00 | Q-H09两现存payload漏manifest，183数字不是新独立验证；disposition未授metadata-only repair | 同owner新revision修完整包/fresh review，旧包只读；不把其全局化 |
| EX06 | NP02 aggregate guard collect2；NP06 stale NP03 hash 1F；56phase只有54native0 | 原EX-SIM/EX-CLIENT各修自文件，stopped delta→C1exact137→Rootbind→C2exact31/13/56→full硬审；执行D1不是DATA-D1 |
| MACRO producer | r5 owner19native0/frozen，最近实际独立r3仍F01 FAIL；r5 hard review未跑 | 消费前审r5实际write-set，FAIL回原owner；本地MR2 PASS不关闭producer、远端或历史PIT |
| RUNTIME / QMH | R5 source FAIL/旧native closure0保留；QMH仅tools PASS，actual158/ABC/CBA未执行 | 未来走已生效transfer、逐命令所需窗口；不重审有效plan、不伪造旧STOP，停止通用runtime成为首个研究门 |
| LEGACY / P1退出 / ordinary shadow | 默认legacy存在不代表原多层/ADD完整接受；P1 profile未在M5显式选择；shadow `_reg`及退出参数缺口有源码/测试记录 | E0原独立版本先证实原多层/ADD、所选接口及风险账本，不等无关新增P2；P1接线在D前修；shadow按B0–B8在R5前修，不阻已有完整exit pack的walk-forward |

原证明路径在r1 evidence_registry E01–E34及各plan/lease中，报告时带精确scope/revision/裁决。SC-M1原路径full8独立87/88，末项carried bytes+owner88/88+等价probe的处置保留，不能改写成原路径独立88/88；M2 tooling194/194不是完整节点。FPC、SHCP、MR2等有限PASS不重开同范围审核，也不删除其剩余原门。

停止增加无明确消费者的参数扫网、全七族×八退出笛卡尔积、远端宏观扩建、统一管理工具、全仓递归seal、强制commit或新JSON台账。旧FAIL保留，必要修复按真实消费者激活；E0 LEGACY按自身源码/接口、原多层/ADD payoff、风险/成本/连续账本及独立接受就绪，新增退出组合按其阶段就绪，分别在有效授权内立即开发、必要验证冻结、独立ZCode硬审，FAIL退原owner。数据不足、工程阻断、统计失败、全部现金都如实交付证据，不用新的审批轮或无限调参拖延下一份实际结果。
