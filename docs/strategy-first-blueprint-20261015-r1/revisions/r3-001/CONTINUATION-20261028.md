**CONTINUATION.md — r3-001 开发续接裁定，2026-10-28；状态：EFFECTIVE**

用户最新 Goal 已覆盖“按照更新后的蓝图开发至项目完成、适时推送、按最新规则尽量并行”。此前“本批完成后停止”不再限制后续蓝图工作。继续以 [r3-001 实施计划](/D:/Projects/AK-trading/docs/strategy-first-blueprint-20261015-r1/revisions/r3-001/IMPLEMENTATION-PLAN.md)、BLUEPRINT、REPAIR-ORDER 及有效作者增补为标准；本件只明确剩余交付和下一步，不替换原合同。

Astra 负责计划与裁定，专用 ZCode 开发会话使用已有配置，另一独立 ZCode GLM-5.3/max 对实际冻结成果硬审。旧计划中的 Sol 路由和计划 PASS 门按现行规则解释。以下依据用户提供的 main/origin `1fda5af`、具名接受记录及必要当前源码；STATUS 仅用于理解时间顺序，不作冻结输入或接受权威。

| r3 里程碑 | 当前可确认的完成范围 | 尚需完成的交付 |
|---|---|---|
| **1．可信离线入口与合法接受基线** | C05 导入隔离、后续双流输出修复已接受；最新两文件记录见 [C05 streaming ACCEPTANCE](/D:/Projects/AK-trading/docs/development-goal-20261026/c05-streaming-hard-review-007/ACCEPTANCE.json)。DP08 r3 仅有其九文件清单后继的 scoped PASS。 | **PARTIAL。** T7 CPU/完整冷链仍待完成；R0review009 与新 L0 仅为前置。既有全仓失败、DP08 未执行义务及旧超时继续保留，不能将 launcher PASS 记成全仓绿。 |
| **2．非空合法资格 replay** | [REPLAY-ACCEPTANCE](/D:/Projects/AK-trading/docs/development-20261025-zcode-r3/REPLAY-ACCEPTANCE.json)、后续 [M4-ACCEPTANCE](/D:/Projects/AK-trading/docs/development-goal-20261026/M4-ACCEPTANCE.json) 和 [M4-CAUSALITY-ACCEPTANCE](/D:/Projects/AK-trading/docs/development-goal-20261026/M4-CAUSALITY-ACCEPTANCE.json) 分别证明 replay 接线、历史/当前绑定断言修复及缓存接口适配；后两项完整 M4 九文件均有 259P。 | 本里程碑的**已审 replay 切片完成**，复用对应字节，不重审已关闭旧14项。ordinary shadow 不在此接受范围。 |
| **3．逐频率输入与正确自然链** | [A2-ACCEPTANCE](/D:/Projects/AK-trading/docs/repo-audit-20261026-claude/A2-ACCEPTANCE.json) 已接受七策略重复 bar/过期计数修复；所述 M4、DAILY seam 和其他 scoped 接受按消费者复用。 | **PARTIAL。** 新源码身份仍须消费者后继接受。MINUTE 必要规则/可用时点、完整输入回读等缺口未被这些 PASS 填补；DAILY 各品种人口、会话、来源和时钟按原门完成。 |
| **4．MINUTE、DAILY 实际历史报告** | [CHRONOLOGICAL-ACCEPTANCE](/D:/Projects/AK-trading/docs/development-goal-20261026/CHRONOLOGICAL-ACCEPTANCE.json) 已无条件接受固定三策略的 SCENARIO 成果：252 子调用、1523 文件精确重现，含已披露 namespace adaptation。 | **PARTIAL。** 此报告保持 `SCENARIO/CALENDAR_UNVERIFIED/NOT_ASSESSED`，不替代正式双频 A/B。所读逐频绑定仍为未准入模板；本次没有取得两份完整正式报告的接受证据。 |
| **5．ordinary shadow 同 pack 闭包** | [A4-ACCEPTANCE](/D:/Projects/AK-trading/docs/development-goal-20261026/A4-ACCEPTANCE.json) 仅接受 CLI 提前拒绝保护。 | **未闭合。** 当前 `experiments.py` 仍使用未定义 `_reg`；shadow 非空资格调用仍缺退出身份参数，原 B0–B8 义务继续有效。 |
| **6．本地 paper 开始记录** | [FORWARD-ACCEPTANCE](/D:/Projects/AK-trading/docs/development-20261025-zcode-r3/FORWARD-ACCEPTANCE.json) 已接受 integrated306、378 文件闭包，独立完整人口 306P。 | **工程完成、真实启动未证明。** 该接受明确 live SDK `NOT_RUN`；下一交付应是合法绑定后的实际 capture/首份报告，随后才是具备真实模型与规则的账户激活。 |
| **7．完整 SHADOW_ACCEPTED** | 原验收合同保留。 | **未完成。** 正式资格、固定真实前瞻 cohort、20 交易日及全部期限成熟、会话完整性、≥99.5% 按时率、P99≤5秒、>120秒禁新仓、UTC≤100ms 和独立验收均不能由测试或回放替代。 |

M holiday-source 的 [接受记录](/D:/Projects/AK-trading/docs/development-goal-20261026/BIP1-M-SOURCE-ACCEPTANCE.json) 是 scoped 工程 PASS，但仍为 `INPUT_NOT_DELIVERED`：新增合格来源、日历关系均为0，累计6 queries/6 URLs 已关闭。M42、chronological、A2、A4、M4、C05、forward 的上述已完成审核不重复派发。`STRATEGY-RESULTS.md` 的历史收益比较不提供正式策略资格，也不用于事后择源或改参数。

**四个在途模块继续原工单，不因本件重新冻结或等待。** T7 CPU014、M5 control046、N8 c079、DISPATCH owner013/036 保留原 SID/cwd/store、实际限额、消费次数和失败。M5、N8、DISPATCH 分别继续已发布的 [M5-023](/D:/Projects/AK-trading/docs/development-goal-20261026/repair-dispositions-20261028/M5-023.md)、[N8-025](/D:/Projects/AK-trading/docs/development-goal-20261026/repair-dispositions-20261028/N8-025.md)、[DISPATCH-020](/D:/Projects/AK-trading/docs/development-goal-20261026/repair-dispositions-20261028/DISPATCH-020.md)。T7 保持其完整人口、资源及 `F→R0→L0→owner/R1→L1→R2`；本件不补发任何已消费运行。Root 按最多三个已验证独立 ZCode store 调度，准备和实际重运行分别核算资源。

接下来按以下四个自然单元推进；它们不共享写集，实际运行仍服从各自依赖。

1. **M5：先修明确的 CLI 完成语义，再逐频交真实报告。**

   有效依据是原 [M5 逐频交接](/D:/Projects/AK-trading/docs/strategy-first-blueprint-20261015-r1/implementation-clarifications/m5-frequency-immediate-handoff-20261015-r1/HANDOFF.md)、其 `BINDINGS.json`、M5 A/B PLAN 与研究实施计划 R4。原合同 [06-M5 §8](/D:/Projects/AK-trading/docs/devplan-v2/06-M5-验证与影子运行.md:330) 要求“请求阶段完整执行并发布报告”返回0，不以统计有效为条件；当前 [CLI](/D:/Projects/AK-trading/scripts/research_probability_v2.py:116) 却将正常完成的 `walk-forward` 一律返回3。**这是已规划的条件修复现已确认适用，可直接开发，无需新增设计或计划意见。**

   产品写集限独立后继候选中的 `scripts/research_probability_v2.py`、`tests/test_v2_m5_cli.py`。归原 M5 调用端开发职责；Root 从逐频交接的 `m5_writer_transfer` 及合法后继恢复实际专用 ZCode，尚未完成角色交接时明确承接，不能虚构 SID、把控制 reviewer 当开发者，或给 CPU owner 取得这两个文件。当前 CPU014、M5 冻结候选及其 pins 不改。

   修复只纠正阶段完成与退出码映射：完整报告即使统计 FAIL/INSUFFICIENT 也按原合同返回0；StageIncomplete、输入错误、留出重复消费和内部失败分别保持3/2/4/1。不得用 `m5_complete` 的双频总批含义判断单次阶段是否完成。A4 提前拒绝仍保持3，不开放 shadow 或 holdout。

   必要验证为完整 `test_v2_m5_cli.py`、`test_v2_m5_shadow_entry_guard.py` 及新增受影响退出分支，经 `scripts/test_offline.py` 执行；核实际原生码、报告/error 产物和没有新增副作用。CLI 单元替身只证明分支映射，不提供真实 fit 信用；不替换真实锁 verifier。冻结后立即独立 ZCode 重跑并做相反结果控制。运行使用该切片既有有效验证权限及实际余额，本件不借用 CPU、reviewer 或 full100 预算。

   后续真实回测仍由同一 M5 writer 独占配置、注册和输出。首个实际 READY 频率直接交付原24单元、完整 fit/校准/训练观察、账户、压力、漏斗、读回及复现；另一频率不作前置。`BINDINGS.json` 中尚为 null 的输入、锁、cwd、正式命令和预算必须由真实交付补齐，不能启动占位模板。双频48单元、原全部期限/durations、252/60/60及原接受后继、purge/embargo、风险门、最终留出保护均保留。

2. **Forward paper：立即续接已接受宿主的运行绑定，优先取得真实 capture。**

   复用 [forward-paper PLAN](/D:/Projects/AK-trading/docs/strategy-first-blueprint-20261015-r1/implementation-clarifications/forward-paper-start-20261015-r1/PLAN.md)、CLARIFICATION-001、LIVE-CLOCK-PLAN-001 及已接受 integrated306 后继，不再重做宿主设计或历史 full13。

   原计划作者为 `/root/astra_forward_paper_start_plan_20261015`。当前实际接续 owner 由 [forward owner005 REQUEST](/D:/Maintenance/aktr-development-20261025/forward-recovery-owner-005/REQUEST.json) 绑定：SID `sess_43fc3d2f-dc0c-4e1b-858d-28ffe727c1e2`，cwd `D:\Projects\AK-trading`，store `D:\Maintenance\aktr-development-20261025\forward-recovery-owner-001\session.sqlite`。立即续接绑定工作；本件不扩其产品源码写权。

   写入仅限原 owner-evidence 的新运行绑定/必要交付及 `data/research_runs/v2/forward-paper/<新run_id>/`。使用实际接受的完整闭包；main 已有新字节时不能把旧378文件 PASS 借给当前树，也不能恢复已退休 canonical377 gate。精确旧闭包可独立使用；若选择新消费版本，按真实差异由原 owner 完成后继接受。

   当前 [preflight](/D:/Projects/AK-trading/src/ak_trading/v2/validation/forward_paper.py:751) 明确允许缺 activation 时的 capture，但仍要求合法 `runtime_authorization` 和 `runtime_closure`。Root 直接绑定已有实际 SDK 操作授权；已有授权不再询问。确实没有授权时，仅真实 SDK 启动待该项决定，离线绑定继续；代理不读取凭据或探测其是否存在。

   原首跑600秒、后续单次最多3600秒、2GiB、1GiB输出、单 TqApi/最多四订阅不变；不是新发或重置额度。先交至少一条真实新 CLOSED bar 和封闭后的 REPORT/终态；零 bar、UNKNOWN clock、WAITING_AUTH 或 WAITING_INPUT 如实保留。缺模型时保持 `strategy_evaluation=NOT_RUN / paper_account=NOT_ACTIVATED`，不把采集说成策略评估。真实 forecast/payoff/regime、当前具体合约规则/会话、warmup及有效 clock 到位后再按原 activation 开四账户。N8、DISPATCH、ordinary shadow 和未消费的全历史输入不成为该 capture 的总门。

3. **R1：把下一输入工作收敛到现存 SC 原件的可消费性，停止重复失败来源轮次。**

   原输入合同继续用 [BIP1 PLAN](/D:/Projects/AK-trading/docs/strategy-first-blueprint-20261015-r1/implementation-clarifications/backtest-input-delivery-20261015-r1/PLAN.md)、R1-NEXT及处置、M-PRICE-CLOCK-RULING-002。原作者为 `/root/astra_backtest_input_delivery_plan_20261015`；其作者身份与 R1 产品 owner 不由本件接任。

   **现在直接交原作者一个精确差异任务：** 在既定 RB→CU→AU→M→I→SC 顺序下，确定现存 `data/strategy_artifacts/futures-lib-20260907-codex-01/real-input-02/` 的 SC 行能否形成自己的输入定义，以及唯一需要补定的 SC 日期标签/可用时点政策。该实际 manifest 声明 SC，未声明 I，且明确缺 SessionWindow、completion 只有原时间戳；这只证明此包的范围，不证明其他地方没有 I 数据或 SC 原件。

   原作者必须给出可执行的 source/prefix/tail、C/L/X/T/R、会话与 availability 采用结论，或点明阻止其中哪一项的具体事实；不得继承 M 三小时政策、RB 时钟或依据价格收益选源。保留同一 W、same312及全部成熟/人口要求，不重新运行 RB MINUTE312或 DAILY153/51拒绝。

   若所存原件足够，沿原表示直接实施来源适配、会话/输入物化成功分支和 public readback；建议增量写集严格限定为新 R1 后继内的 `materialize_sc_source_facts.py`、`verify_sc_source_facts.py`、`SC-SOURCE-DEFINITION.json`、实际 source facts，以及**条件成立才生成**的人口、sessions和原格式派生载荷。最终逐路径集合由原作者绑定；不写 M1 公共 schema、原 collection、M5 或其他品种包。原件不足则不开发空 consumer、不复制同值包，不发起另一轮泛搜。

   这是需要原输入作者裁定的具体品种增量；如产生新政策，收一次独立意见，由该作者处置后立即交专用 ZCode，完成 source＋consumer 联合硬审。沿原记录显式转交已停止的历史 Sol 职责，不能假称原 ZCode SID 连续。**本件没有续发任何 RB/CU/AU/M 查询、URL、SDK或数据库额度，也没有为 SC 创造新获取额度。**

4. **Ordinary shadow：保留现成实施方案，只解除真实接口前驱。**

   复用 [ordinary-shadow CLARIFICATION](/D:/Projects/AK-trading/docs/strategy-first-blueprint-20261015-r1/implementation-clarifications/ordinary-shadow-20261015-r1/CLARIFICATION.md)、RESEARCH r2 与原 `shadow-original-contract.json`。B0–B8 已足够具体，不再编第二份功能计划。

   Root 向原 RESEARCH/SH-SCHEMA 作者交实际可用的 R19 filewise release 后继，要求只裁定该 source、ExperimentSpec/config 与公共 schema 的精确差异；R20终局后由原 M1 发布 R21公共候选。T7 R0/L0、M5 控制 PASS或目录存在都不是这条释放链。若已有有效后继，直接核用，不重复前驱审核。

   文件归属仍明确分开：R-SHADOW 的 ZCode owner 只写 `validation/shadow.py`、`tests/test_v2_m5_shadow.py` 及原附件逐项授予的 fixtures；M5 唯一 writer 写 `experiments.py` 和其 experiments/payoff-integration 测试，B8 所需 reports/CLI 顺序接续；公共声明、decoder、required-tests、fixtures/锁归原 M1。当前可并行完成调用差异和受影响测试准备，实际写入等对应释放，不能动活动冻结。

   接受仍需同一真实 exit pack 穿过训练/genesis/replay/runner/恢复，真实非空 qualification、至少两策略、SH-T01–08、真实 T8及所消费完整 SHCP178人口。A4保护只在该入口真正接受后按原权属处理。此模块不阻断未调用 ordinary shadow 的历史研究或 forward capture。

Root 现在继续四个在途模块，并在空出的合适 store 中优先派 M5 CLI 窄修复和 forward 绑定；同时把上述 SC 精确问题交回原输入作者。SH 按真实 R19/R20/R21 解除依赖，不占用重型窗口做泛化准备。已有效计划直接派工；只有实际新增合同增量收一次意见，作者处置后立即实施，不求计划 PASS。每项开发验证、冻结完成即独立硬审，FAIL 回原实际 owner；接受且集成字节相符后适时推送，不等待所有模块一起完成。

Root 可将本件作为 r3 追加导航并更新 CURRENT/STATUS 的旧停止说明，保留历史正文、接受范围和失败。保留完整策略/回测/paper目标；真实统计失败或不足必须如实交付，不能为了“项目完成”降低资格门或把20日验收改成短采集。外部治理 F1–F6、B3/B4/B6、P0-1 用户操作、真实 broker及最终 holdout均未被本 Goal 自动批准。

本轮仅作静态读取与 Tier2 图谱查询；相关源码覆盖为 `metadata_changed`，已对所依赖的 CLI、shadow和 forward 入口作实际源码回读。文档、外部包及未检查路径不作完整性断言。没有产品运行、测试、fit、源码修改或产品 PASS。后续工单继续携带 AGENTS7/8、两项20261004事故及manifest-recovery002、20261016 Path/JSON/closed-stream纪律；原生码、工具结果、产物和 verdict 分列，消费记录不重置。全程保持 `enable_trading=False / observer=hold / broker=NOT_RUN / real send-cancel=0/0`。