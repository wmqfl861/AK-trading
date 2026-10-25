# r3 修复开发批次

2026-10-25 用户恢复开发并将开发者改为 ZCode。生效规则提交 `0cc3054` 已推送 GitHub main。Astra/max 计划、独立 ZCode GLM-5.3/max 产品硬审、其他原规则保持。

| 模块 | 当前实际状态 | 候选 |
| --- | --- | --- |
| P2-7 公共导出 | 独立ZCode无条件PASS；焦点30 passed、消费者267 passed/1既有缺夹具skip；原owner完成canonical精确集成和30项smoke，源码已合main提交3336c62；原修复分支8cb767b保留 | `D:/AKWorktrees/aktr-r3-p27-20261025` |
| P0-2 replay | 两文件修复独立ZCode无条件PASS；独立焦点20 passed、反例24/24；报告hash更正已接受。原owner完成canonical精确集成和20项smoke，源码已合main提交3336c62；原修复分支f979e88保留。完整八文件仍191 passed/14旧基线failed，不授完整M4或shadow接受 | `D:/AKWorktrees/aktr-r3-replay-20261025` |
| C05导入隔离 | 四项夹具/测试替身问题已由原ZCode owner修复，完整焦点40 passed/raw0，原失败保留。原会话owner-validation-004立即接续完整收集、完整执行及真实顺序置换；尚待冻结及独立硬审 | `D:/AKWorktrees/aktr-r3-c05-20261025` |
| P1-6 SQL helper | 四文件独立ZCode无条件PASS；独立临时库15 passed/0 skip，SCRAM真实认证、轮换、权限拒绝及回滚反例通过。已合入并推送main，源码ddf5c67、审核记录10cd59f；生产采用NOT_RUN | `D:/AKWorktrees/aktr-r3-p16-20261025` |

原forward执行作者003已明确重切旧审核。Root逐一核实旧进程不存在并保存可恢复的377源文件和旧审核材料后，追加OLD-SUBMISSION-CLOSEOUT，将旧审核记为RETIRED_INCOMPLETE_NO_VERDICT / NOT_ACCEPTED，仅解除它的canonical写冻结；旧快照和审核保持只读，其他所有权/冻结仍有效。P27/replay原ZCode owner据此集成通过的精确字节。SQL四文件原本不在377内。C05继续在独立候选验证。P0-1生产操作、G治理提案、大规模架构变更、真实交易不纳入本批。

派工记录：DISPATCH.json、REPLAY-HANDOFF.json、C05-HANDOFF.json。原始调用和验证记录位于 `D:/Maintenance/aktr-development-20261025/`；P2-7、P0-2 replay切片、P1-6已有独立硬审PASS，分别见P27、REPLAY、P16的ACCEPTANCE/REVIEW/VERDICT。原始流为LOCAL_ONLY；replay原报告保留，通过CORRECTION与最终VERDICT追加更正。C05没有开发或独立产品完成结论。

原ZCode开发会话`c05-owner-validation-004`执行剩余验证；新ZCode专用owner按原Astra作者003准备机械后继`verify_forward_acceptance_003.py`，明确从已停写Sol交接，不修改产品或执行runtime。待集成和必要验证齐备，冻结真实新版本，独立审核才可执行唯一新full13-002：2100秒、2GiB、1GiB，保留原298项及集成所需新增节点/断言。没有为此新增计划PASS或helper先审PASS门槛。各ZCode owner/reviewer使用独立固定store；未派Sol/Claude继续开发。

剩余基线问题分列：replay的14项失败属于`test_forwarded_binding_keeps_frozen_baseline_value`旧值对照，完整M4不记PASS；shadow切片仍未关闭。C05隔离与完整基线分列，不刷新历史pin掩盖错误。旧forward的full13-001确已消耗，只有49项完整通过、50项启动，没有RESULT/session finish或产品verdict；旧raw1及未知child/helper事实保留，不能冒充产品FAIL/PASS或重放。新集成forward尚未取得独立产品PASS。集成见MAIN-INTEGRATION-001.json；旧快照与原始流为LOCAL_ONLY。
