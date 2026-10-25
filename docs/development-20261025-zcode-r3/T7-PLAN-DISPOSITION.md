**P1-3/T7 作者处置：EFFECTIVE，立即生效。** 已完整读取[现有计划](D:/Maintenance/aktr-development-20261025/astra-t7-existing-plan-detail-001/T7-EXISTING-REPAIR-PLAN.md)、[独立计划意见](D:/Maintenance/aktr-development-20261025/t7-plan-advice-001/PLAN-ADVICE.md)及当前角色、流程规则。原计划继续有效，以下仅澄清接续、派工和依赖；原写集、合同、预算及完整测试人口保持。此次意见已处置完毕，无需再次计划审核或用户确认。

1. **A1 — PARTIALLY_ADOPTED。采纳显式候选接续，纠正“r2 审核未落地”的判断。**  
   已读取旧 `READY.json`、`SOURCE-FREEZE.json` 和 [r2 REVIEW-REPORT.json](D:/AKS261016/M3B/directional-fixture-r2/hard-review-r1/REVIEW-REPORT.json)。READY 保留送审时状态，但后续报告已明确给出无条件 **PASS**，范围仅为三测试夹具增量，且写明审核完成并停止。它不覆盖四个策略生产文件。

   本作者现明确授权：Root 从包含 `3336c62` 的 tracked HEAD 建立单元 A 独立候选，对原计划的**四源、四测试写集**作有限接续并建立新的独立硬审范围。旧 r2 源树、冻结清单、READY、报告和失败证据保持不可变；新审核不覆盖、撤销或冒充旧审核。旧三测试中，A 可写范围仅重叠 directional、matrix；martingale 仍只运行、不写入。

   历史开发 SID `sess_7cb10507-709e-48fc-bb87-2107831fc0b5` 仅作为来源记录，不能自动认作本次 actor。Root 绑定实际 A owner、store、cwd，并确认没有重叠活跃 writer；若发现另一项仍在运行的重叠审核，须先明确重切其范围，不能覆盖其输入。本次接续不恢复旧 canonical 377 总冻结，也不要求重做已结束的 r2 审核。

2. **B1 — ADOPTED。补全所有生产缺陷的路由。**  
   B 若在 `experiments.py`、DAILY `pipeline.py/gates.py`、策略或其他生产消费者中发现真实缺陷，立即将最小复现、候选 pins 和适用原条款交 Root。**Root 核实并绑定该文件的实际既有 owner／合法接续者、SID、store、cwd、候选和精确写集后，才开展生产修复。** B 不通过改夹具、放宽断言或扩大自身写权掩盖缺陷。A 已获绑定的四源范围内修复沿其有效工单进行，不为每个同范围缺陷新增一轮审批；跨节点问题同样退 Root 路由。

3. **C1 — PARTIALLY_ADOPTED。补齐真正需要的身份，不把所有潜在 owner 设为启动门。**

   | 阶段 | 实际前置条件 |
   | --- | --- |
   | **A 立即验证与开发** | A 实际 owner 的 SID/store/cwd、tracked 基线与 pins、原合同及必要输入、四源四测归属、本次有限接续和无并发写冲突。 |
   | **B 立即实施不相交夹具** | B 实际 owner 的 SID/store/cwd、独立候选、原计划测试/夹具写集、必要本地合同与输入、输出存在性前置。无需等待 A 接受。 |
   | **B 最终冻结及组合验收** | A 已接受的实际字节、所消费接受链与候选匹配、实际所需 M1 覆盖和候选锁条件，以及原计划完整验证义务。 |
   | **发现生产缺陷或确需 M1 改动时** | Root 在修复前补齐对应 M5、DAILY/M4、M1 或其他节点的实际 owner 绑定；这些尚未触发的修复身份不是 A/B 初始开发门。 |

   外部原件使用原计划列出的绝对路径；旧 r2 明确定位为 `D:/AKS261016/M3B/directional-fixture-r2/` 下上述记录。仅缺少本阶段实际必需原件时阻断相应步骤，不虚构 SID，不以未绑定的潜在修复负责人阻塞并行工作。

4. **D1 — ADOPTED。接受字节不匹配时转原 owner，不盲刷新。**  
   候选文件若不匹配既有接受记录，标为“当前字节未获该记录覆盖”，保留原接受范围并交 Root 路由相应 owner；不得沿用旧 PASS、回退他人改动或直接更新 hash。影响仅限实际消费该文件的步骤。六条接受链全部齐备不是 A 的统一入场门；若某差异确实影响 A 的必要输入，必须指出具体文件和消费依赖，不能概括成无关总门。

5. **E1 — PARTIALLY_ADOPTED。采纳并行 prepare 检查，不预判必须改 M1。**  
   B 可与 A 并行只读核对当前 `artifacts.py`、required_tests、F/DAILY 附件覆盖及合法锁生成路径，尽早把实际缺口交 Root。旧裁决描述的历史缺项不能证明当前仍缺。已有覆盖满足则直接复用；确需 M1 产品改动时，才由绑定后的 M1 owner 在原计划精确范围内实施并接受，T7 owner 不代写公共文件。

6. **F1 — ADOPTED。重型运行单独排期，原预算不变。**  
   B 的真实冷链避开其他重型批次的资源争用；A 验证及 B 夹具开发继续并行。owner、reviewer 各自保留每 profile **4 小时、两 profile 共 8 小时**；快检每 profile **15 分钟**；最终锁复验仅在真实冻结 fit 复用条件成立时使用 **15 分钟**，否则沿冷链预算。A 保留 **600 秒／2 GiB／1 GiB** 约束。超时仍记失败或未完成，不缩人口、不降断言；意见中的历史耗时概括不成为新增预算依据。

**Root 现在即可派发 A 开始验证、派发 B 实施不相交夹具，并并行安排必要的 M1 只读准备。** A 保留七个单文件测试及完整合并命令；B 保留原 T7 目标、两文件完整命令、M1 五文件／M4 九文件／M5 七文件原完整组及目标锁 required_tests。B 最终冻结等待 A 的实际接受；现存业务失败、M4 的 14 个基线失败和 M5 C1 冻结继续如实记录，不扩大任何窄范围 PASS。

此次独立意见使用 **plan mode，只读提供建议，没有执行测试或 hash 命令**；其“checks passed”不能计作运行验证。本作者本轮也只读取记录，未重新计算产品 hashes、运行测试、导入产品或写入文件。派工继续携带 AGENTS §7/§8、两份 20261004 事故记录及 `20261016-r1-custody-path-normalization-r1/INCIDENT.md`，保留原生退出与失败证据。保持 `enable_trading=False`、observer=`hold`、broker=`NOT_RUN`、真实 send/cancel=`0`。