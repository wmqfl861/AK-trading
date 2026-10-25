# M4 replay 计划意见作者处置

日期：2026-10-25。作者：Astra/max，会话 `01a171b2-3f04-7413-85f6-fb5423ee127d`。有效版本为 [M4-REPLAY-PLAN.md](M4-REPLAY-PLAN.md) 的“2026-10-25 作者处置版（EFFECTIVE）”，立即供 ZCode 开发使用；无需再取计划 PASS、重复审核或等待 C05。

本次只处置三条独立计划意见，作者按当前最终文档自主决定。意见输入为 `D:/Maintenance/aktr-development-20261025/replay-plan-advice-001/OWNER-REPORT.md`（LOCAL_ONLY，4811 字节，SHA-256 `2127614ddda001891c92898bcac95216bd3b0810c4ee3d0f9d6eb19532081937`），不复制或修改原意见包。其“无阻断级发现”是咨询结论，不是实施许可或产品验收。

| 发现 | 作者决定 | 理由与落实 |
| --- | --- | --- |
| F01：T7 后续 pin 漂移 | ADOPTED | 处置前计划 §5 尚缺接受后与 P1-3/T7 的衔接，意见仍适用。已补充候选合法接受/集成后的变更来源登记，T7 后继清单须晚于或明确纳入接受版本；不使 T7 成为 replay 前置，不改旧 pin，也不把既有 11 drifts 记为已修。 |
| F02：候选与 owner 派工记录 | PARTIALLY_ADOPTED | 采纳 candidate/branch/两文件 write_set/owner 及停写移交可追溯要求；原 DISPATCH 的 replay 条目缺口仍存在。但不向被活跃调用固定的 `DISPATCH.json` 追写，由 Root 新建 `REPLAY-HANDOFF.json` 绑定实际会话、store/cwd 和有效计划 hash。作者不虚构尚未派定的 owner 或候选，不新增审批。 |
| F03：现有异常捕获路径 | ADOPTED | 处置前 §3.3 只解释 TypeError，尚未解释 verifier 正常拒绝的继承关系，意见仍适用。已补明 `ArtifactsError(ValueError)` 已进入现有捕获及 `invalid_q` 的 missing/invalid 记录；不改异常框架或 validator。 |

## 审核输入变化的真实记录

审核启动后，本作者对计划作了 38 字节文档增量，审核输入从 **9273 字节变为 9311 字节**。据 Root 本次交接，advisory CLI 原生退出 **0**，使用有效原 SID 并产生真实意见；wrapper 因输入变化退出 **98**。这两种退出结果分别保留，不能把 wrapper98 改写成成功，也不能声称原 pins 始终一致。本文没有补造未读取的原请求、SID 字符串或 wrapper 过程证明。

本次处置前实际读取的 9311 字节计划 SHA-256 为 `cf5d41b5620f056dd981fe8e3c297bbbd95840a25284f70a788ca6a192021cb3`。作者逐项对照该最终文档和实际意见：§5 仍缺 T7 后继协调，§2 仍缺 replay 实际派工记录落点，§3.3 仍未写明 ArtifactsError 捕获路径；三条建议的对象、理由与本轮允许范围均继续成立。故采纳范围以上表为准，输入字节变化不被隐去，也无需以重审替代作者判断。随本处置发布的是已落实三项澄清的新有效版本，不冒称为 reviewer 原先固定的文档。

## 交付与边界

本轮仅更新计划并新增本处置。Root 按有效计划补 `REPLAY-HANDOFF.json`、建立 tracked `0cc305454d06643e93b14e426d04a6a755094d84` 的独立候选，并立即交专用 ZCode 开发会话实施。canonical 377 冻结、原公共契约、真实非空资格正反例、原 M4 八文件完整验证及独立产品硬审要求继续有效；R19–R21 仍仅约束实际消费 ordinary SH 契约的路径。

本次没有产品实现、测试执行或成果硬审核，产品信用为零；不宣告 [VERIFICATION](../repo-audit-20261025-claude/VERIFICATION.md) 中任何缺陷已修复。C05 计划及其独立意见流程不在本次写集。交易仍为 `enable_trading=False`、observer=`hold`、broker=`NOT_RUN`、real send/cancel=`0/0`；原计划所载命令退出码、事故引用、配置与凭据保护继续适用。
