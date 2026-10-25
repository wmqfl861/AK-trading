# r3 修复开发批次

2026-10-25 用户恢复开发并将开发者改为 ZCode。生效规则提交 `0cc3054` 已推送 GitHub main。Astra/max 计划、独立 ZCode GLM-5.3/max 产品硬审、其他原规则保持。

| 模块 | 当前实际状态 | 候选 |
| --- | --- | --- |
| P2-7 公共导出 | 独立ZCode无条件PASS；焦点30 passed、消费者267 passed/1既有缺夹具skip；已推送修复分支，提交8cb767b | `D:/AKWorktrees/aktr-r3-p27-20261025` |
| P0-2 replay | 两文件已实现并冻结，原owner调用native0；焦点20 passed，完整八文件191 passed/14 failed；相同14项退出绑定基线失败已在干净基线复现。独立ZCode硬审已启动，尚无判决 | `D:/AKWorktrees/aktr-r3-replay-20261025` |
| C05导入隔离 | 有效计划、87份输入已齐备；首次调用HTTP502/native1，原会话已分步续接并产生实际启动器改动；焦点和完整验证尚未完成 | `D:/AKWorktrees/aktr-r3-c05-20261025` |
| P1-6 SQL helper | 四文件已实现；一次性PostgreSQL集群内15 passed、零skip，角色新建/重复执行与关闭自身集群均raw0；随后CLI遇HTTP502/native1。Root按实际终止字节冻结，独立ZCode硬审已启动，尚无判决 | `D:/AKWorktrees/aktr-r3-p16-20261025` |

canonical377原冻结仍有效；开发仅写登记的独立候选。P0-1生产操作、G治理提案、大规模架构变更、真实交易均不纳入本批。C05隔离与完整基线修复分列，旧pin错误不因隔离修复自动关闭。

派工记录：DISPATCH.json、REPLAY-HANDOFF.json、C05-HANDOFF.json。原始调用和验证记录位于 `D:/Maintenance/aktr-development-20261025/`；仅P2-7已有独立硬审PASS；其他模块不以开发自测或CLI退出0替代产品结论。

截至2026-10-25 10:38 UTC，活跃工作为两路独立产品硬审（`replay-hard-review-001`、`p16-hard-review-001`）与一路ZCode开发（`c05-owner-002`）。每路使用独立固定session store，同一owner沿原SID续接，没有派Sol或Claude Code继续开发。

剩余基线问题分列：replay的14项失败属于`test_forwarded_binding_keeps_frozen_baseline_value`旧值对照，未修改或刷新常量；完整M4不记PASS。旧forward的`independent-full13-001`目录已有Job登记和部分日志，但没有RESULT，单次执行已消耗，不能直接重放或据此解除377冻结。该旧审核恢复与本批隔离候选分别处理。
