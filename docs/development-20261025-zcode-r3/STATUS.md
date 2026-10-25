# r3 修复开发批次

2026-10-25 用户恢复开发并将开发者改为 ZCode。生效规则提交 `0cc3054` 已推送 GitHub main。Astra/max 计划、独立 ZCode GLM-5.3/max 产品硬审、其他原规则保持。

| 模块 | 当前实际状态 | 候选 |
| --- | --- | --- |
| P2-7 公共导出 | 独立ZCode无条件PASS；焦点30 passed、消费者267 passed/1既有缺夹具skip；原owner完成canonical精确集成和30项smoke，源码已合main提交3336c62；原修复分支8cb767b保留 | `D:/AKWorktrees/aktr-r3-p27-20261025` |
| P0-2 replay | 两文件修复独立ZCode无条件PASS；独立焦点20 passed、反例24/24；报告hash更正已接受。原owner完成canonical精确集成和20项smoke，源码已合main提交3336c62；原修复分支f979e88保留。完整八文件仍191 passed/14旧基线failed，不授完整M4或shadow接受 | `D:/AKWorktrees/aktr-r3-replay-20261025` |
| C05导入隔离 | 原独立reviewer003复现路径边界碰撞并给出FAIL RF1；同一ZCode owner006已运行45项焦点测试全部通过，仍待冻结交付、独立短复核、原完整验证与最终硬审。旧004完整验证raw15/INCOMPLETE及历史DP失败保留 | `D:/AKWorktrees/aktr-r3-c05-20261025` |
| P1-6 SQL helper | 四文件独立ZCode无条件PASS；独立临时库15 passed/0 skip，SCRAM真实认证、轮换、权限拒绝及回滚反例通过。已合入并推送main，源码ddf5c67、审核记录10cd59f；生产采用NOT_RUN | `D:/AKWorktrees/aktr-r3-p16-20261025` |

原forward执行作者003已明确重切旧审核。Root逐一核实旧进程不存在并保存可恢复的377源文件和旧审核材料后，追加OLD-SUBMISSION-CLOSEOUT，将旧审核记为RETIRED_INCOMPLETE_NO_VERDICT / NOT_ACCEPTED，仅解除它的canonical写冻结；旧快照和审核保持只读，其他所有权/冻结仍有效。P27/replay原ZCode owner据此集成通过的精确字节。SQL四文件原本不在377内。C05继续在独立候选验证。P0-1生产操作、G治理提案、大规模架构变更、真实交易不纳入本批。

派工记录：DISPATCH.json、REPLAY-HANDOFF.json、C05-HANDOFF.json。原始调用和验证记录位于 `D:/Maintenance/aktr-development-20261025/`；P2-7、P0-2 replay切片、P1-6已有独立硬审PASS，分别见P27、REPLAY、P16的ACCEPTANCE/REVIEW/VERDICT。原始流为LOCAL_ONLY；replay原报告保留，通过CORRECTION与最终VERDICT追加更正。C05没有开发或独立产品完成结论。

原ZCode开发会话`c05-owner-source-repair-005`处理独立源码预检发现的两项计划缺陷，旧004完整验证保留为未完成；新ZCode专用owner按原Astra作者003准备机械后继`verify_forward_acceptance_003.py`，明确从已停写Sol交接，不修改产品或执行runtime。待集成和必要验证齐备，冻结真实新版本，独立审核才可执行唯一新full13-002：2100秒、2GiB、1GiB，保留原298项及集成所需新增节点/断言。没有为此新增计划PASS或helper先审PASS门槛。各ZCode owner/reviewer使用独立固定store；未派Sol/Claude继续开发。

剩余基线问题分列：replay的14项失败属于`test_forwarded_binding_keeps_frozen_baseline_value`旧值对照，完整M4不记PASS；shadow切片仍未关闭。C05隔离与完整基线分列，不刷新历史pin掩盖错误。旧forward的full13-001确已消耗，只有49项完整通过、50项启动，没有RESULT/session finish或产品verdict；旧raw1及未知child/helper事实保留，不能冒充产品FAIL/PASS或重放。新集成forward尚未取得独立产品PASS。集成见MAIN-INTEGRATION-001.json；旧快照与原始流为LOCAL_ONLY。


2026-10-25 并行续接：C05 owner005已修复并冻结两文件，焦点43项通过；原独立reviewer003正在执行短反例，完整验证仍待完成。Forward原作者004已完成无环绑定协议裁定，同一ZCode owner003已立即实施helper004及限定静态/SYNTHETIC协议验证，不消耗正式full13。DP08 r3九文件release-manifest接续包已进入独立硬审，不代表整个DP08真实存储接受。M5发现已完成的R16-R4-C1后继，615个冻结成员已核对一致；原独立reviewer已恢复源码审查和运行准备，正式测试等待真实资源底线满足。两路OpenCode按默认配置只读核对其他既有交付与可释放的已结束预留；未新增产品计划。

2026-10-25 DP08 r3接续包独立硬审已无条件PASS，实际CLI native0、137输入不变、9个接受文件逐一与当前字节匹配。独立verifier重跑117/117 CLEAN，38项结构检查及4项反例通过；原DP08全模块、V15/live/DB/SDK等NOT_RUN保留。见DP08R3-REVIEW.md、DP08R3-VERDICT.json、DP08R3-ACCEPTANCE.json。

2026-10-25 15:45 UTC并行状态：C05 owner006、Forward owner003、DISPATCH owner002正在实施/修复；M5 C1 reviewer002、行情补全reviewer002正在独立审核；N8原Astra作者063正在处置既有审核038的服务失败恢复。每路使用独立固定会话库和不重叠写集。M5已绑定当前C1后继，旧producer-index问题不再派修；前两阶段通过后才进入唯一full100。Forward最终集成仍等C05接受，不提前消耗full13-002。多路HTTP502是真实上游调用失败，不能称服务已根治或产品测试失败，原SID有界恢复与原始失败均保留。

TqSdk 3.10.3工具及五处skill发布已复用原独立无条件PASS并完成Root接收，当前65份发布文件与原冻结一致；未重复派审。见[TQSDK-ACCEPTANCE](TQSDK-ACCEPTANCE.json)及[TQSDK-VERDICT](TQSDK-VERDICT.json)。范围仅离线工具安装与文档，在线认证、行情、回测、broker仍为NOT_RUN；不替代正在审核的免费行情补全交付，也不改变项目原SDK依赖。
