# 支持模块成果与缺陷重新评估（Astra/max，2026-10-15）

本报告是只读复核，不是正式 ZCode 硬审，不恢复暂停任务，不授予实现、采用、部署、运行或交易信用。复核先读 AGENTS.md、docs/strategy-research-blueprint-20261010-r2/BLUEPRINT.md、REPAIR-BACKLOG.md 及两份 2026-10-04 incident；结构查询使用 codebase-memory。图索引项目为 D-Projects-AK-trading，generation 2026-10-13T01:30:24Z，19,519 nodes / 112,916 edges，状态 ready。索引对外部 D:/AKF* 树返回 outside_project/freshness unavailable，因此外部源码结论均来自实际文件读取；仓库内 scripts/research_probability_v2.py 做了 coverage 检查，但结果为 metadata_changed，已再读实际源码。没有产品导入、测试运行、SDK/数据库/网络/服务/客户端/交易/凭证/配置操作。

## 结论与研究影响

蓝图把 A/B 离线研究的共同关键依赖收窄为选定真实输入、M2/M3A/M3B 实际消费闭包、EP1 实际 fit/观察生产和 M5 真实 walk-forward；APUB 实时发布、通用 runtime、EX06 部署、远端宏观、管理工具和完整 shadow 是按消费者条件或后置项处理。这个调度方向成立，但“条件后置”不等于缺陷关闭，也不把有限 PASS 拼成节点 PASS。

当前没有证据证明 A/B 已执行，也没有证据证明所选 RB 双频输入、G4/G6 发布、EP1 fit 或 M5 实际 run 已完成。因此研究结论仍是 NOT_RUN/UNKNOWN；支持模块可否不挡研究必须按实际消费者重新核定。

## 决定性发现

| 优先级 | 模块、当前事实和证据 | 原 owner 下一步 | 是否挡离线策略 |
|---|---|---|---|
| P0 | DATA-D6 原审 OWNER-REPORT.md（docs/parallel-development-20261004-r1/dispatch/aktr-zcode-data-r2-d6-isolated-plan-review-20261014-001-cc57f669）给 F01/F02：PublisherRawBundle.origin 曾写成 str，而 APUB6 producer 的 NESTED_KEYS origin 是精确 {source_id,evidence_version}；D6_ISOLATED 物化/复制基线没有枚举。当前 revisions/d6-isolated-clarification-20261014-r1/plan-delta.json 已写入嵌套类型、禁止 stringify/flatten、D6 materialization keys，状态 AUTHOR_DISPOSITION_COMPLETE_FRESH_INDEPENDENT_PLAN_REVIEW_PENDING；目录实际只有 WRITE-SCOPE.json、INPUT-PINS.json、plan-delta.json。 | 原 Astra 计划作者完成 fresh independent plan review；通过后才可按 AGENTS §9 进入独立 Sol 实现，随后再独立硬审。D3/D6 owner 按新绑定分别实现，不能由实施者自行解释类型。 | 不挡所有离线研究。若 A/B 输入不消费 D6 live publisher，则后置；若消费 D6 live lineage，F01/F02 是该输入的直接前置。 |
| P0 | RUNTIME-R5 独立审阅 dispatch/aktr-zcode-runtime-r5-schema-escape-r2-plan-review-20261014-001-95a574d3/{RESULT.json,OWNER-REPORT.md} 为无条件 plan PASS，requires_plan_update=false，且 product_review_credit=false。我按 pin 读取外部 schema：43,914 bytes、SHA-256 9734bf38…e26fdc，模式 ^[A-Z]:/[^\\]+$，Python re.compile 实际仍为 re.error: unterminated character set at position 8。PLAN.json 的 43,916-byte postimage 是 prospective，written=false；修复目录仍为 schema_implementation=NOT_RUN。 | 按已通过窄计划完成一次精确、隔离、授权的 schema 写入和验证；冻结实际 write-set 后独立 ZCode 硬审。不得把内存 transform 的成功写成真实 schema 已修。 | 通常不挡 A/B 离线研究；挡消费该通用 runtime/schema 的研究命令或运行窗口。 |
| P0 | EX06 r7-005 verdict（coordination/aktr-zcode-ex06-r7-005.../reviewer-r1/VERDICT.json）的唯一 R7R1-01 是 PLAN-VALIDATION.json 把 R7-005-VALIDATION-ATTEMPTS-DISPOSITION.json pin 为 13,111 bytes / 180c...，实际文件为 18,560 bytes / e5fb...；其它 artifacts map、PACKAGE-MANIFEST、PREPARATION-RECEIPT 指向实际值，validator 未覆盖该冲突。这是计划 custody/摘要问题。另有历史 EXI-C2-R1/HANDOFF.md 的真实 NP02 collect native 2（EX-SIM guard）和 NP06 1/249（NP03 hash stale），并非由 stale summary 产生。 | 原 EX06 owner 对摘要 pin 做 append-only 修复并 fresh review；执行域另按 stopped-delta→C1→Rootbind→C2→full hard-review 顺序修复 NP02/NP06。不能以摘要修复覆盖实际执行缺陷。 | 不挡纯离线研究账本；挡 EX06 同版执行集成、部署和运行验收。 |

## 其它支持模块状态

| 模块 | 当前可核实事实 | 严重度/研究关系 |
|---|---|---|
| D1 materializer | D:/AKF261004/DATA/D1/r3-continuation-r1/evidence/ACTUAL-MATERIALIZATION-REPORT.md 记录唯一授权物化器 native 1：预写 COMMAND.json/START.json 被错误当成已存在输出，11 helpers、34/39 outputs 未物化，未运行 suite/phase/product runtime。 | 高；只有消费这些 D1 产物的输入被挡，不能当成全数据链已交付。 |
| D2 / labels | D2 handoff 是 558 次离线 SYNTHETIC 工程执行（547 非 skip、11 原批准 PG skip），原产品 FAIL/I1_NOT_ENTERED，source-intake scoped PASS；labels 有 owner/G3 记录，但 DB/mainline/apply 未跑。 | 中到高；选定输入若依赖正式采用则前置，不能把有限 PASS 当完整 M2。 |
| APUB3 | APUB3 独立实现审阅为 F1 seal-guard unconditional PASS，但明确 product/runtime/integration/adoption 未发行；旧事实仍是真实 malformed publisher/F2 未闭。 | 条件依赖；FrozenInput 离线研究可不等 APUB live publisher，live 使用时必须修 F2 并复审。 |
| M2 / SC-M1/SC-M2 | 仅支持 SC-M1 source304 + 三工具 scoped PASS、SC-M2 preparation scoped PASS，不是 G4/G6 实际发布或 full M2 PASS。 | 高；蓝图明确把 G4/G6、exact304 consumption、final source/run 放在实际研究关键链。 |
| EP1/FPC | EP1 只有 source/capacity scoped PASS，实际 generation/registration/fit/lock 未运行；FPC3 为 adapter/route/admission/semantic scoped PASS，final run/copy/publication/full-node 未发行。 | 高；EP1 实际 fit/观察生产是首批研究直接前置，FPC 仅在实际入口消费时前置。 |
| MR2 | 独立原98、修订50、4 probe 全 PASS；明确不授 mainline adoption、runtime、deployment、strategy qualification、真实报告信用。 | 低到条件；可作有限组件证据，不能代表整条研究链或经济资格。 |
| SHCP5 | E9 是 unconditional scoped PASS，75 pins/2904 manifest/6 commands 闭合；E8 历史 FAIL 保留，product credit false。 | 后置/条件；有限 shadow 支持不等完整 feed、20 日、时钟或交易资格。 |
| DISPATCH | r2 verdict 仍 FAIL H3/H12，required_read_ack_completion=PARTIAL_THIS_TASK，无部署/交易信用。 | 后置；只有选定研究入口消费该 dispatcher 才前置。 |
| MASTER-B-TOOLS | 独立报告列六个真实缺陷：partial 首因丢失、junction 越界、NOT_RUN 非空时错误退出 1、错误 skip reason、deselection 空记录、cwd 在 release tree 外；84 测试绿不覆盖 probes。 | 后置；不走该发行工具的离线账本不应被阻，真实消费时必须原 owner 修复/复审。 |
| Q00 | verdict FAIL Q-H09：manifest 漏两份 existing payload；七项 formal command 绿不能补行。 | 后置/条件；相关资格包消费时挡。 |
| MACRO producer | r5 handoff 是 owner 19-command native 0、待独立审；最近独立 r3 仍 F01 FAIL，backend UNKNOWN、PIT false、research/economic NOT_RUN。 | 后置；首批 A/B 不应等远端宏观，增加宏观因子时才前置。 |
| QMH/runtime qualification | 旧证据仅 tools-only PASS，158-node/ABC/CBA 闭环未执行；RUNTIME-R5 plan PASS 不改此事实。 | 后置；不设通用 runtime 资格为离线研究共同门。 |

## 防止反复 schema/exit/admission 循环的少量措施

1. 每个 schema 计划复审前读取真实 pinned preimage，执行 stdlib parse/compile，并把 prospective postimage 与实际写入状态分开；计划审核不产生产品信用。
2. 每个生成输出记录机器可读生命周期：启动前 ABSENT/PRESENT、产生者终止时间、最终 size/hash；禁止 Read 尚未生成的 output。rg 仅把 (1, stderr 为空) 归类 NO_MATCH，其它命令沿自身退出合同保留原码。
3. validator 每次从实际文件重算摘要并拒绝 stale size/hash；摘要、artifacts map、receipt 由同一 frozen source 生成，冲突直接 FAIL。
4. admission 拆开 plan_review / implementation / product_test / adoption / deployment / statistical_qualification；scoped PASS 和 owner native 0 不得聚合成节点 PASS。
5. 研究调度只检查实际消费者闭包：未消费的 live publisher、EX06、宏观、管理工具继续保留原 FAIL，但不成为 A/B 共同前置；选中消费者后按具体 write-set、来源、风险账本和 fresh independent review 激活。

以上是修复优先级和防复发建议，不是放行、豁免或新的审批门。

## 明确未查 / UNKNOWN

未运行实验、walk-forward、EP1 fit、G4/G6 发布、数据库/SDK/网络、20 日 feed/shadow、客户端部署、服务重启或真实订单。未逐项重建数十万历史证据，也未把 2026-10-10 蓝图快照推断为 2026-10-15 产品状态。选定 RB 双频输入是否冻结、M2 exact304 是否实际消费、DAILY 跨周期天然链是否修、M3A/M3B 实际 source/contract 是否接受，均为 UNKNOWN，需下一实际消费者工单按原 owner 核实。

