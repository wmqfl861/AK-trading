# 策略优先实施计划：核验驱动修订 r3-001

日期：2026-10-25。状态：**EFFECTIVE_PLANNING_ONLY，当前有效规划**。对应[蓝图](BLUEPRINT.md)、[22 项处置表](REPAIR-ORDER.md)与[作者处置](DISPOSITION.md)。以下安排只在各任务原有授权和实际依赖成立时适用；本次发布规划与导航，不执行开发、测试、研究或服务动作，不自动恢复暂停工作。

## 1. 复用计划，先绑定实际负责人和写集

继承[研究实施计划 R1–R5](../../../strategy-research-blueprint-20261010-r2/IMPLEMENTATION-PLAN.md)、[策略优先 r2 实施计划](../r2-001/IMPLEMENTATION-PLAN.md)的模块边界、必要测试人口、源谱系和预算。新事实以 [VERIFICATION](../../../repo-audit-20261025-claude/VERIFICATION.md)为准。本文不是新的通配 lease，也不是 M4/SH 公共合同或历史 owner 的交接。

职责按[现行模型规则](../../../../PROJECT-MODEL-ROLES.md)和[工作流](../../../../PROJECT-WORKFLOW-RULES.md)：Astra/max 负责计划和本职合同差异；Sol 开发明确 `model=gpt-6.1-sol`、`reasoning_effort=max`、`fork_turns=none`；独立 ZCode GLM-5.3/max 给计划意见及产品硬审。计划作者自主处置意见，不设计划 PASS 门。新产品 owner 独立于计划作者及 reviewer；修复续接同一实际 Sol owner，必要替换先明确记录，不编造会话连续性。

| 单元 | 负责人绑定 | 受影响文件与边界，非新增写权 |
| --- | --- | --- |
| M4 replay | 原 M4 职责，当前实际 actor 待 Root 绑定/显式移交。 | `src/ak_trading/v2/decision/replay.py` 及原 M4 replay/causality 等必要测试；M5 不代写 ledger，forward 不代写 replay。 |
| R-SHADOW | 原 R-SHADOW 职责，当前实际 actor 待绑定。 | `src/ak_trading/v2/validation/shadow.py`、`tests/test_v2_m5_shadow.py` 与原计划逐路径列出的 fixtures；不得取得整个 validation 目录写权。 |
| M5 调用端与研究 | 当前 owner `/root/sol_m5_existing_recovery_20261023`。 | `validation/experiments.py` 与所属 M5 测试；B8 若涉及 `reports.py`、CLI 及测试，仍由该唯一 writer 或显式 filewise transfer 后的 owner 接续。 |
| SH 公共契约 | 原 M1-PUBLIC/SH-M1 角色，实际 actor 按原权威记录绑定。 | 原公共声明、decoder、fixtures、required_tests、ownership 和候选/发布锁；具体路径由 R20 裁定及 R21 lease 给出，本文不发行新 schema。 |
| C05 / P1-3 | 原 DP03/DP08 测试引导及原 M5 T7 基线责任方分别绑定。 | 两份 bootstrap、各自原清单和对应测试是不同归属；先确定已接受版本与变更来源，跨节点代码问题退原 owner。 |
| P2-7 与支撑项 | 原接口/模块 owner；确无当前 actor 时由 Root 显式绑定独立 Sol。 | P2-7 限 `src/ak_trading/futures/data.py` 公开导出与必要消费验证。依赖、SQL、端口、CI 各自列精确文件，避免与冻结集重叠。 |
| forward feed | 沿现任 forward owner 的原记录。 | 只限其 `feed.py/test_feed.py`；核验协助不扩权至 replay、shadow、M5 或公共合同。 |

派发前从权威记录程序化取得具体 source/revision、逐文件 hash、owner、文件释放状态、cwd/argv、原必需人口、资源预算、实际输出路径及 ABSENT/EXISTS 前置。将原失败、已消耗的一次性命令和验证状态一并交接。当前 canonical 377 写冻结有效；隔离候选或审核重切须有合法记录，不能先复制修补再补授权。不同 owner 不重叠写，共享 M5 文件顺序接续。

## 2. 两个接线问题立即规划，合同与冻结按原路径解决

**P0-2 replay。** 在原 M4/公共合同权限内确认可单独修复的兼容源与合法候选。使用已有 frozen exit registry 和按 strategy 唯一匹配的 binding，显式传给真实 qualification verifier；不填零 hash、空 binding、默认 LEGACY 或另一策略身份。该窄修复若不消费 ordinary SH 新接口，不附加 R19–R21 总门；但必须由原模块计划方确认这项范围，本文不创设 source-only 例外。

**P0-2 shadow 与 P1-2。** 直接复用普通 shadow B0–B8：先 R19 实际产品 filewise release，再 R20 原合同作者对真实 source/ExperimentSpec/config 作 SH-SCHEMA 终局，随后 R21 SH-M1 公共候选冻结；满足相应写集释放后才实现。缺模块作者可恢复性或 actor 绑定时，Root 只处理该项明确交接；本次蓝图作者接任不替代模块合同作者。已有前驱接受证据可按精确范围复用，不重复未变化的计划意见。

B0 在昂贵 fit 前核验并冻结同一 `(registry, exit_specs, exit_bindings)`，传遍真实训练、genesis、replay 和 runner；B1/B3 恢复以入口 pack 为独立期望；B2/B4 checkpoint 保存真实 registry、完整 bindings、states 与 execution links；B5/B7 用真实非空资格及严格激活原件；B6/B8 的公开签名与调用者按原冻结合同接齐。保留 SHCP7 已接受恢复语义、durable originals、pending、fill/fee 去重；不能整文件覆盖当前 M5 修复。

异常整改以原语义为边界：合法坏证据的 ValueError/TypeError 拒绝仍有效；缺参、未定义名、错误 pack 接线不能被报告成策略统计不达标。先用显式参数、入口身份核验和调用约定正反例封住已知缺口，不机械删除全部 TypeError 捕获、不放宽 validator 或 ledger 必填参数。确需新异常协议才交原模块合同作者作具体差异裁定，不为泛化异常重构延迟既有接线。

若完整普通 shadow 尚被真实前驱阻断，保持不可用/BLOCKED 并列出准确原因；不把期待 NameError 的旧测试当成完成。明确提前拒绝入口只能作为原作者裁定的有限保护，不能关闭 P1-2 的接线义务；改变对外模式范围须有相应产品决定。本批未授权新增禁用模式或删除 CLI。

## 3. 以可交付产物划分里程碑

下表是依赖关系，不是要求逐行串行执行。独立写集可并行；1、2 为当前工程优先项，3、4 按实际消费路径推进，5 只约束 ordinary shadow，6、7 分别是模拟启动和完整验收。P0-1 用户操作及 §5 支撑项独立排队。

| 里程碑与具体交付 | 进入条件 | 必要验证 | 完成证据与状态 | 责任角色 |
| --- | --- | --- | --- | --- |
| 1. 可解释的离线收集与接受基线：C05、P1-3 分项闭合。 | 核对 DP03/DP08、T7 原接受版本及当前差异，取得各自精确修复写集。 | 隔离历史候选导入域，验证收集顺序不污染业务包；逐条追溯 T7 11 项漂移；重跑原目标命令及修复后完整离线入口，不删测试遮错。 | C05：预期人口可收集、无污染级联；T7：合法基线对应原单项验收；完整运行记录原生退出、执行数及剩余失败。收集恢复不自动等于全套通过或 P1-1 全关闭。 | 原测试引导/基线 Sol owners；独立 ZCode 审核实际改动。 |
| 2. replay 能消费真正有效的非空资格，P0-2 replay 切片闭合。 | 原 M4 actor、兼容 exit pack、合法候选/释放与有效计划齐备。 | 真实 verifier + 合法非空资格正例；缺证据、错 hash、错/缺 binding、过期等反例；其余门齐备时自然允许预期虚拟入场，原 M4 必需回归。 | 冻结写集、独立重跑与无条件产品判决；只授本切片工程信用，不关闭 shadow 切片或签策略资格。 | M4 Sol；公共差异归原 M1/Astra；独立 ZCode。 |
| 3. 每频率可消费输入与正确自然链。 | 所选 source/lock、数据与相关代码已接受；仅沿所需 G3→G4→G6、M1/M2/M3/M4 依赖。 | 按事前覆盖/成熟/规则/seen 选择来源；时间/PIT/身份、未来追加因果反例；DAILY 复用 scoped seam 并闭合真实消费所需 relay。 | 各频率输入身份、覆盖/缺口、所消费接受版本及 READY/BLOCKED/INSUFFICIENT；准备包、工具 PASS 不算真实输入交付。 | 原数据/R1、M1/M2/M3A/M3B/M3C/M4 owners，各守写集。 |
| 4. MINUTE、DAILY 各自的实际历史回测报告。 | 该频率所消费输入/代码和原实际运行 admission 齐备；涉及的 T7/DP03/DP08 接受清单按修复顺序 §3 闭合，或依原规则接受新版本；不消费的清单、另一频率及无关支撑工程不作门。 | 原 A/B 完整人口、真实 fit/校准/训练观察、四政策六场景、输出读回与精确复现；保留统计失败、零组与永久留出。 | 每频率实际报告与 manifest、账户和漏斗；工程 verdict、RESEARCH_PASS/STATISTICAL_FAIL/INSUFFICIENT、运行状态分列。两报告齐备才记双频总批完成。 | 当前 M5 Sol owner；原范围要求的独立 ZCode 成果核验。 |
| 5. ordinary shadow 同 pack 闭包可用。 | R19→R20→R21、真实 source 及文件释放成立，M4/R-SHADOW/M5 actor 已绑定；所消费代码涉及的 T7/DP03/DP08 接受清单按修复顺序 §3 闭合，或依原规则接受新版本；不消费的清单不作门。 | 原 SH-T01–08、真实非空训练/runner/事件/恢复、至少两策略同 pack、合法激活与资格正反例；保留所消费 SHCP 原人口。 | 新冻结版、完整必要验证及独立产品判决；CLI 不抛异常、空资格、局部恢复 PASS 均不算闭合。 | M5 与 R-SHADOW 不重叠实施，M1 依原门发布公共锁；独立 ZCode。 |
| 6. 本地 paper 开始并交首份可读运行记录。 | 自身代码/输入、feed/clock、实际运行权限和必要 host 接口就绪；若用 ordinary shadow，则里程碑 5 就绪。 | 原 lifecycle、时钟/时效、NO_TRADE、重启/去重及账本核对；区分虚拟成交和真实 send/cancel。 | 真实启动/终态与首份 feed/clock/账户记录；状态为记录中或实际失败，不能提前标 SHADOW_ACCEPTED。 | 原 paper/host/M5 owners 按文件；服务主会话、客户端用户操作各守原权限。 |
| 7. 完整 SHADOW_ACCEPTED。 | 原正式研究资格与所需产品接受齐备，已累计真实前瞻窗口及全部必要会话。 | 原 20 交易日、完整性、≥99.5% 按时率、P99≤5秒、>120秒禁新仓、UTC≤100ms 等全门及原独立验收。 | 真实前瞻证据、正式工程/研究/模拟各轴和独立判决；回放/SYNTHETIC 不替代，真实 broker 仍 NOT_RUN。 | 原 shadow/研究 owners；独立 ZCode；Root 仅接收既有范围。 |

C05/清单未闭合只阻依赖该集成基线的候选；另一条已独立接受、未受影响的消费路径仍可按其完整原要求推进。全仓 CI 不绿必须如实报告，但不能把无关诊断全部变成每个实验的共同入口门。任何当前必须测试的真实失败也不能被上述分流豁免。

## 4. 必要验证和结果口径

1. 接线正例必须构造**真实类型、合法且非空的 qualification/evidence**，调用真实 validator、ledger 和选定 runner；明确 SYNTHETIC 的工程 fixture 不提供历史研究信用。正例既验被接受，也验实际使用同一 registry/binding；负例覆盖缺失、错身份/哈希、失效、恢复与 checkpoint 不一致。默认空元组、stub 成功或仅“不抛异常”均不满足。
2. 按原 SH 附件区分缺必填 keyword 的 TypeError、`checkpoint_dependency_mismatch`、`checkpoint_hash_mismatch`、`checkpoint_exit_bindings_mismatch`；ledger 的 `exit_bindings=()` 有默认值，缺绑定是身份完整性问题。所消费完整 SHCP 七人口为 178，已含 fresh10/join10，不重复加成，也不以 smoke 替代。合法激活原件缺失则明确 NOT_RUN/BLOCKED。
3. C05 修复先核已接受的 DP03 collector 版本与后继来源，再隔离历史 staging 导入；DP08 校验与业务导入均应正确。T7 的 64 pins/11 drifts 逐项给差异来源、接受范围和处置。只有对应版本验证/审核满足才发布新清单，旧清单和 C05/C07 原失败不覆写。
4. 研究继续按[研究蓝图 §3–§5](../../../strategy-research-blueprint-20261010-r2/BLUEPRINT.md)：每频独立 config/run_id/split_id、同风险四政策、六场景、原 252/60/60、八期限、因果/PIT/成熟和持仓规则。模型全无、StageIncomplete、PAYOFF_TRAINING_EPISODES_NOT_AVAILABLE 与有效 OOS 全现金分开；SCENARIO、SYNTHETIC、正式历史 PIT 不互相升格。
5. 沿用 `experiment.json/metrics.json/readiness.json/report.md/manifest.json`、fold 输出与现有实验注册，不另造管理账本。报告列输入/代码/锁/seed、训练到成交的分母、零组、四政策连续账户及六场景；精确复现为 `new_statistical_evidence=false`，不重置已消费留出。
6. 原 snapshot P2 的 300s 与 snapshot/HANDOFF-002 谱系 full100 的 3600s、2 GiB 内存、1 GiB 输出是不同命令约束；这些字面值不外推到 SOURCE-025 恢复谱系。各谱系均从所选操作的实际有效工作单、lease/预算及合法后继记录程序化绑定，不互串、不采用未核验的建议数值，不缩人口或擅自提高预算消除历史失败。已接受后继可按原范围沿用，不把 r2 旧失败标签当成今天的新运行结果。

先做有判别力的廉价接线验证，再做原计划必需完整人口；昂贵 fit 只在相应运行授权与资源前置成立后执行。源码先冻结，stdout/stderr 在生产进程结束后封存；成果硬审独立重跑并做反例，PASS/FAIL 不附条件。FAIL 回同一 Sol owner 追加修复、重新冻结、立即独立复审；不重复未变化的计划意见来求 PASS。

## 5. 支撑修复与停止范围

P2-7 是明确公共导出错误，核原接口后做最小兼容处理，不需要先重构 v1/v2。P1-4 只补实际入口依赖及用户确认的支持版本，工具 TqSdk 3.10.3 不自动迁移项目 pin。P1-6 限 SQL/helper/说明与隔离库验证；生产账号创建和切换由用户执行。P1-5 限端口一致配置、服务身份和离线/隔离启动验证，真实端口及部署窗口待用户决定。P1-1 在收集可用后建立如实反映失败基线的检查，不用忽略列表掩盖失败；强制 PR/分支保护另待用户决定。

上述支撑工作仅在实际入口消费它时成为前置。G 项和 P2 大改动保留提案，不新建 core 包、不替换模型、不迁移存储、不全库格式化/renormalize。输入漂移、必要接受缺失、活动冻结、实际硬依赖或授权缺口只停对应消费者；跨节点问题带最小复现退原 owner，其他已获授权且 ready 的独立范围按原规则继续。

## 6. GitHub 可用性与派发时必须补齐的本地材料

本版主知识链接指向既有跟踪文档或本批发布的 r3 规划、处置、计划意见/收据与 VERIFICATION；Root 在提交前核对最终索引成员。以下文件本机存在但当前不在 Git 跟踪集合，**LOCAL_ONLY / NEEDED_AT_DISPATCH**；只给文字定位，不制造 GitHub 可点击死链接。本次不导入外部产品文件、冻结源或运行包。缺必需原件只能阻对应派发，不能宣称首次克隆可执行该模块。

| 本地材料定位（相对仓库根） | 何时必须取得的具体内容 |
| --- | --- |
| `docs/strategy-first-blueprint-20261015-r1/implementation-clarifications/ordinary-shadow-20261015-r1/CLARIFICATION.md` 及 `SOURCE-STATUS.md` | ordinary SH 派发前读取 B0–B8、释放顺序和接受范围；本作者已读，旧 actor 以本版核验事实纠正，不改原件。 |
| `docs/parallel-development-20261004-r1/plans/RESEARCH/r2/PLAN.md`、`plan.json`；`docs/parallel-development-20261004-r1/plans/RESEARCH/r1/acceptance/shadow-original-contract.json` | 原 SH 公开签名、精确 SH-T01–08、源成员、R19/R20/R21 记录及实际 actor/filewise release。派发时须提供有效原件及后继，不用本文摘要代替。 |
| `docs/strategy-research-blueprint-20261010-r2/implementation-clarifications/m5-ab-experiment-plan-20261014/PLAN.md` 及 `SNAPSHOT-MEMORY-HANDOFF-002.json` | 所选 A/B 来源、原必需人口、实际运行 admission；使用该 snapshot 候选时补原 source lease、预算和固定集成顺序。 |
| `docs/maintenance/20261004-rg-no-match-r1/INCIDENT.md`；`docs/maintenance/20261004-grok-headless-cancel-r1/INCIDENT.md` | 新工单必须携带两事故规则；确需 CLI 调度再绑定现行 watchdog/USAGE 和原授权，不从旧会话推定新权限。 |

外部候选和核验原始输出也为 LOCAL_ONLY：VERIFICATION 指定的 `D:/AKF261018/M5-CDF-BATCH-SOURCE-025-R16-R4/source`、`D:/AKF261001/SHCP7/source`、`D:/Maintenance/aktr-repo-audit-20261025/` 仅作已核事实定位，本轮未读取产品字节或重跑探针。派发者须绑定真实终态、现行 pins、接受范围与冻结状态；外部目录存在不代表已接受或已入库。

命令纪律随所有未来工单传递：立即分别捕获 raw native exit、stdout、stderr；rg 0 为匹配，仅可选发现的 rg 1 且 stdout/stderr 为空记 NO_MATCH，rg 2 及其他错误保留。测试/CLI 非零不享该例外；必需输入缺失是错误，生成输出先检查存在性，不作尚不存在的输入读取；native exit、工具结果、产物完整性与产品 verdict 分列，成功一次性命令不为补记录重跑。

## 7. 本次文档交付验收

本批交付本目录三份有效规划、[DISPOSITION](DISPOSITION.md)及同步后的 CURRENT/README 入口；[计划意见](PLAN-ADVICE.md)和[收据](PLAN-ADVICE-RECEIPT.json)保持原件。文档检查限最终链接、22 项结论、修改范围与状态文字；Root 已检查初稿 33 个链接，提交前再核最终 Git 索引成员并纳入 VERIFICATION。评审员未运行 hash、测试或 Git 跟踪检查，不能将作者/Root 检查记在评审员名下。作者处置已完成，本版无需二次计划审；本批不运行产品测试、网络或数据库，不签产品 PASS，不声称任一缺陷已修复。
