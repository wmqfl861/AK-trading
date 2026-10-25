# 当前策略优先规划

生效规划版本：`revisions/r3-001`；日期：2026-10-25；状态：**EFFECTIVE_PLANNING_ONLY**。

- [蓝图](revisions/r3-001/BLUEPRINT.md)：以核验事实组织正确策略、可复现历史回测与本地 paper；MINUTE/DAILY 各自就绪交付，模拟启动与完整 20 交易日验收分开。
- [实施计划](revisions/r3-001/IMPLEMENTATION-PLAN.md)：七个里程碑的交付、进入条件、必要验证、完成证据及责任角色；只约束实际消费者的依赖。
- [修复顺序](revisions/r3-001/REPAIR-ORDER.md)：22 项核验结论、处理范围与关闭条件；P0-2/P1-2、C05/清单漂移及 P2-7 优先，G 项与大规模变更仍待用户决定。
- [作者处置](revisions/r3-001/DISPOSITION.md)：同一 r3 作者对 F01–F06 的采纳决定及理由。
- [独立 ZCode 计划意见](revisions/r3-001/PLAN-ADVICE.md)与[收据](revisions/r3-001/PLAN-ADVICE-RECEIPT.json)：仅供咨询，不是计划 PASS 门或产品硬审核；评审员 hash/tests/Git 跟踪检查均为 NOT_RUN。
- [外部审计逐项核实](../repo-audit-20261025-claude/VERIFICATION.md)：本版事实基础；22 项中 11 项成立、11 项部分成立，未签发产品 PASS/FAIL。

本版经作者处置确认有效，无需二次计划审。用户本次明确请求仅为“更新蓝图并推送到仓库”，由 Root 提交推送；**此发布不自动启动或恢复开发、研究、测试、服务或部署，也不撤销历史已授权任务。** 仓库可见性、许可证及治理提案仍待用户决定，此次推送不是这些设置的变更，也不新增审批门。

当前职责以[项目模型与职责规则](../../PROJECT-MODEL-ROLES.md)和[工作流](../../PROJECT-WORKFLOW-RULES.md)为准：Astra/max 负责计划及意见处置；Codex Sol `gpt-6.1-sol/max` 子代理负责开发和修复；独立 ZCode `GLM-5.3/max` 提供计划意见并对实际冻结成果硬审核。计划不要求 PASS，产品接受仍须独立硬审。本次仅同步导航文字，不修改治理规则、模块合同或产品 owner。

[历史 r2 蓝图](revisions/r2-001/BLUEPRINT.md)及其[作者处置](revisions/r2-001/DISPOSITION.md)、r1 原件和既有审核记录均不改写。历史计划 PASS 只保留其咨询性计划结论，不能充当产品信用或今日启动门。策略、数据、风险及验收硬合同继续继承[20261010-r2 研究蓝图](../strategy-research-blueprint-20261010-r2/BLUEPRINT.md)；缺失的本地模块原件按 r3 的 LOCAL_ONLY 清单在实际派发时提供，不能声称新克隆具备全部运行条件。

canonical 377 及活动审核写冻结、单一 writer、R19→R20→R21 等实际消费门不变；`enable_trading=False`、observer=`hold`、broker=`NOT_RUN`、真实 send/cancel=`0/0`。本次文档发布不增加缺陷修复、研究、模拟或策略资格信用。
