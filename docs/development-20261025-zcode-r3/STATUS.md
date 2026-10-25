# r3 修复开发批次

2026-10-25 用户恢复开发并将开发者改为 ZCode。生效规则提交 `0cc3054` 已推送 GitHub main。Astra/max 计划、独立 ZCode GLM-5.3/max 产品硬审、其他原规则保持。

| 模块 | 当前实际状态 | 候选 |
| --- | --- | --- |
| P2-7 公共导出 | 独立ZCode无条件PASS；焦点30 passed、消费者267 passed/1既有缺夹具skip；已推送修复分支，提交8cb767b | `D:/AKWorktrees/aktr-r3-p27-20261025` |
| P0-2 replay | Astra有效计划及意见处置完成；专用ZCode开发已启动，不依赖ordinary SH的R19–R21 | `D:/AKWorktrees/aktr-r3-replay-20261025` |
| C05导入隔离 | Astra意见处置完成、有效计划已固定；87份输入已核对，专用ZCode开发已启动 | `D:/AKWorktrees/aktr-r3-c05-20261025` |
| P1-6 SQL helper | 原会话两次HTTP502后，另两路ZCode已实际native0；基于此新证据进行一次分步恢复，先实现源码、再验证；尚无交付 | `D:/AKWorktrees/aktr-r3-p16-20261025` |

canonical377原冻结仍有效；开发仅写登记的独立候选。P0-1生产操作、G治理提案、大规模架构变更、真实交易均不纳入本批。C05隔离与完整基线修复分列，旧pin错误不因隔离修复自动关闭。

派工记录：DISPATCH.json、REPLAY-HANDOFF.json、C05-HANDOFF.json。原始调用和验证记录位于 `D:/Maintenance/aktr-development-20261025/`；仅P2-7已有独立硬审PASS；其他模块不以开发自测或CLI退出0替代产品结论。
