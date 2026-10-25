# r3 修复开发批次

2026-10-25 用户恢复开发并将开发者改为 ZCode。生效规则提交 `0cc3054` 已推送 GitHub main。Astra/max 计划、独立 ZCode GLM-5.3/max 产品硬审、其他原规则保持。

| 模块 | 当前实际状态 | 候选 |
| --- | --- | --- |
| P2-7 公共导出 | 独立ZCode无条件PASS；焦点30 passed、消费者267 passed/1既有缺夹具skip；已推送修复分支，提交8cb767b | `D:/AKWorktrees/aktr-r3-p27-20261025` |
| P0-2 replay | 两文件修复独立ZCode无条件PASS；独立焦点20 passed、反例24/24；原审核者已追加机器生成的报告hash更正。修复分支已推送，提交f979e88。完整八文件仍191 passed/14旧基线failed，不授完整M4或shadow接受 | `D:/AKWorktrees/aktr-r3-replay-20261025` |
| C05导入隔离 | 有实际两文件实现；Root焦点诊断40 selected/36 passed/4 failed、raw1，源码前后不变；原ZCode owner003正在修复这四项并重跑完整焦点，随后仍须完整收集、完整执行、置换验证及独立硬审 | `D:/AKWorktrees/aktr-r3-c05-20261025` |
| P1-6 SQL helper | 四文件独立ZCode无条件PASS；独立临时库15 passed/0 skip，SCRAM真实认证、轮换、权限拒绝及回滚反例通过。已合入并推送main，源码ddf5c67、审核记录10cd59f；生产采用NOT_RUN | `D:/AKWorktrees/aktr-r3-p16-20261025` |

canonical377原冻结仍有效；P27、replay、C05仅写登记的独立候选。SQL四文件逐项确认均不在377内，合法合入main，未泛化成全仓冻结。P0-1生产操作、G治理提案、大规模架构变更、真实交易均不纳入本批。C05隔离与完整基线修复分列，旧pin错误不因隔离修复自动关闭。

派工记录：DISPATCH.json、REPLAY-HANDOFF.json、C05-HANDOFF.json。原始调用和验证记录位于 `D:/Maintenance/aktr-development-20261025/`；P2-7、P0-2 replay切片、P1-6已有独立硬审PASS，分别见P27、REPLAY、P16的ACCEPTANCE/REVIEW/VERDICT。原始流为LOCAL_ONLY；replay原报告保留，通过CORRECTION与最终VERDICT追加更正。C05没有开发或独立产品完成结论。

截至2026-10-25 11:45 UTC，两路独立硬审及replay报告更正已经终止，原ZCode开发`c05-owner-003`继续；原forward执行作者通过既有Astra身份续接一次窄恢复处置，没有新建产品计划。各ZCode owner/reviewer使用独立固定session store，同一owner沿原SID续接，没有派Sol或Claude Code继续开发。服务多次HTTP502的原生失败与产品测试结果分别保留。

剩余基线问题分列：replay的14项失败属于`test_forwarded_binding_keeps_frozen_baseline_value`旧值对照，未修改或刷新常量；完整M4不记PASS。旧forward的`independent-full13-001`目录已有Job登记和部分日志，但没有RESULT，单次执行已消耗，不能直接重放或据此解除377冻结。该旧审核恢复与本批隔离候选分别处理。
