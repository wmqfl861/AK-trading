# 策略优先蓝图持续开发状态

更新于 2026-10-26 18:48 UTC。当前 Goal 按有效 r3-001 蓝图继续开发至项目完成，适时推送；上一批 phase 0 已完成，不能把其停止状态解释为新 Goal 暂停。本文是执行状态，不替代 Astra 有效计划、作者处置或独立产品硬审。

角色继续使用现行 PROJECT-MODEL-ROLES.md：Astra/max 负责计划与契约；专用 ZCode 会话开发；另一独立 ZCode 硬审；OpenCode 默认配置负责未指定的辅助任务。计划意见不设 PASS 门。

## 已接受的起点

- main / origin/main 起点为 `71ac137cfd07be89e1ae2c649b7b2c35639c5c42`。
- A2 七策略同 bar 确认修复已通过独立硬审并推送。独立完整 M1–M4 运行 raw 1：962 ran / 948 passed / 14 个既有失败 / 0 error / 0 skip；不能宣称全套全绿。
- A3 文档同步、用户批准的 CLAUDE 两处同步和 B1 的 234 条分诊已完成。A4 先前只完成了有限入口保护方案，本 Goal 开始实际实现。
- C05、P0-2 replay、P2-7、SQL helper、forward 工程集成等已有接受范围继续有效；这些窄 PASS 不证明研究统计或完整前瞻模拟已通过。

## 当前工作

| 模块 | 已核实际状态 | 正在执行的下一步 |
| --- | --- | --- |
| A4 CLI 入口保护 | **独立 ZCode 无条件 PASS，已推送 `c152e33`。** 开发者、独立审核、main 集成各 18 passed / raw 0；独立真实 CLI 反例 11/11 符合预期。 | 有限入口保护已完成；完整 ordinary shadow 接线仍未完成。见 [接受记录](A4-ACCEPTANCE.json)。 |
| T7 / M4 与 DAILY | **M4 独立无条件 PASS 已推送 `a84d966`；r5 的 R0 和真实 L0 已生成。DAILY014 实测为 raw 1、1 passed / 3 setup errors、2880.4 秒。** 正式验证因配置只带 `[86400]` 而违反固定六周期合同，在解析阶段失败。 | **原 owner015 已在独立 r6 候选启动实际修复**：恢复六周期，把真实解析移到 outer stand-in 准备前，追加两项廉价回归。新字节须重新冻结、R0、L0 后再跑完整四节点；旧 r5 与旧 L0 保留。不能把失败进程的耗时和内存外推为完整 DAILY 容量。 |
| T7 分钟线 CPU | 独立 reviewer003 判 **FAIL**：乱序插入重排前缀后错误继承按索引记录的覆盖检查。原开发者002已修复，**M2 96 passed、M3A 165 passed、M5 experiments 33 passed，均 raw 0**，四文件已重新冻结。 | **原独立 reviewer004 已启动复审**，重新验证新源码、必要套件和反例。开发 CLI 在交付文件写成后遭遇502/raw1；实际文件、测试和冻结已核对，原调用失败保留，不为补最终聊天回复重跑开发。 |
| T7 分钟线内存 | 独立审核完成 **182 passed / raw 0**、存储及编码反例、真实容量核算，但正式结论为 **FAIL**：公开校准入口在组合错误输入上改变了报错优先级，且 tuple/view 路径不一致。 | **原 ZCode owner002 已启动修复**，仅改校准实现及其回归测试，随后重新冻结六文件并复审。未变的存储、编码和容量证据按依赖复用；完整 MINUTE 输出容量和 experiments 接线仍未完成。 |
| DISPATCH | 原 owner022 已完成真实生命周期局部检查，但正式 readiness 仍发现 source audit 数量和成员集合不一致。随后 CLI 502/raw1；截至续接前，027 的正式 claim、outer、administrative 均未产生。 | **同一 owner023 正在修复剩余两处闭包问题**，仅在真正就绪后消费原一次027。Root owner005 仍 NOT_RUN；旧025/026失败与所有局部失败保留。 |
| N8 F02 | 原 owner006 已完成五文件 repair011、31项 owner 检查及冻结；CLI native0，输入未变。 | **原独立 reviewer056 已实际启动**，先验证修复及审核探针，再按原单次授权处理后续步骤。尚无本轮 PASS；F02仍FAIL、F01仍OPEN/INSUFFICIENT。 |
| M5 | affected003 保留 **11P/1F、raw 1 / 214.141秒**。reviewer023 已完成004-R2控制包和16项局部检查，正确绑定021探针，产品615仍冻结。 | DAILY 已结束，M5获得下一重型时段；实际004仍 NOT_RUN。原作者003已处置 Root 前期计时缺口，在原预算内给出明确后继额度。下一步是实际封装、004，完整通过后才generation001；full100尚未授予。 |
| 已有策略结果与下一交付 | 三个接受的 RB 日线 SCENARIO 结果见 [策略结果一页说明](STRATEGY-RESULTS.md)。历史 chronological-transfer 的三文件及既有252子回放结果已交独立审核。 | **chronological 原 reviewer002 正在继续源码、既有输出与适配后的离线检查**，完整独立回放等待重型时段。**M 春节公告来源增量已完成作者最终处置，专用 ZCode owner001 已开始实施**；仅两条限定查询、最多四个新官方原件，不把工程接受计作正式历史或 paper 验收。 |

运行请求与原始流位于本机 `C:/AKWorktrees/aktr-goal-20261026/`；外部 CLI 不显示为 Codex 原生子代理。各模块使用独立会话库与不重叠写集；不得凭 CLI 原生 0 认定任务完成。

## 本次已推送与实际命令

- `c152e33`：A4 两文件产品及独立接受；`git push origin main` 原始退出码 **0**，目标 `https://github.com/wmqfl861/AK-trading`。
- `a84d966`：M4 已审修复、独立接受证据及既有策略结果重算已推送；`git push origin main` 原始退出码 **0**。此前用户批准的 CLAUDE.md 两处同步已在 `c151d5c` 提交，不重复修改规则。
- 独立审核与 main 集成均运行 `python -B -X utf8 scripts/test_offline.py -q tests/test_v2_m5_shadow_entry_guard.py tests/test_v2_m5_cli.py`，各 **18 passed / 0 failed / 0 errors / 0 skipped，raw 0**。main 用时 84.469 秒，实测进程树峰值 121978880 bytes。源码暂存字节 SHA-256 与审核冻结逐项相同。
- Root 的精简候选遗漏了非清单中的必需本地输入：A4 六个 chunk 配置、T7 六个配置/测试文件均已按原字节补齐；保留原失败，不称为产品回归。后续候选预检必须覆盖有效 required_tests、跨测试导入及 pyproject/conftest，不能只依赖旧快照路径清单。
- Root 启动前容量检查的非零退出发生于发布/启动前，没有消耗产品一次性命令。通过已结束任务的真实终态、实测占用，释放未用未来预留并复用空出的内存槽；未删除历史证据或降低门槛。一次终态字段名误用已记录，后续按实际接收器 schema 区分 `native_exit_code`、`native_exit`、`raw_native_exit`。

## 蓝图完成标准

七个里程碑继续按 r3 实施计划验收：离线收集及合法接受基线、有效非空资格 replay、每频率可消费输入和自然链、MINUTE/DAILY 实际历史报告、ordinary shadow 闭包、本地 paper 首份真实运行记录、满足原 20 交易日等门槛的完整 SHADOW_ACCEPTED。当前没有两份完整正式历史报告或完整前瞻接受的新增证据；不能用入口保护、计划或测试准备代替这些交付。

保留用户专属决定/操作：P0-1、防火墙/令牌、仓库可见性、MCP 重启、生产数据库角色切换，以及 F1–F6、分支保护、全库换行规范化和 Python 支持版本。它们只阻塞实际依赖的任务，不统一阻塞离线研究。

安全状态持续为 `enable_trading=False`、observer=`hold`、broker=`NOT_RUN`、真实 send/cancel=`0/0`；不读取、打印或修改凭据。真实交易不在本次开发授权内。

## 本轮新增交付与诊断

M4 原十四个失败源于测试额外要求当前绑定必须与历史不同；修复使用真实生产者重算并比较完整六字段投影，保留十四个历史常量与篡改反例。独立 M4 九文件 **259 passed / raw 0 / 1108.70 秒**，实测进程树峰值 539.2 MiB。见 [M4 接受记录](M4-ACCEPTANCE.json)与[独立报告](m4-review/REVIEW.md)。这关闭 M4 本项既有基线失败，不等于重跑全库或完成 T7。

main 全 payoffs 检查输出报告80 passed，但 Root 监控在进程正常退出与 psutil.children 查询之间发生竞态，监督器 raw1，原生子进程退出码未捕获。原失败与日志保留；随后用能完整收回 returncode 的短命令运行受影响14项，**14 passed / raw0 / 6.406秒**。以后资源采样须捕获 NoSuchProcess，执行结果回收放入 guaranteed-finally；采样不能控制退出码回收。此问题未改产品或独立审核结论，详见 [事故记录](m4-review/ROOT-SUPERVISOR-INCIDENT.json)。

策略说明是既有结果的只读重算，两个 CSV 金额使用 Decimal，命令 raw0；不是新增实验或新计划。当前仍未达到正式历史报告或 paper 完整验收。此批接受项提交并推送后继续当前 Goal，不自动宣告项目完成。


## 2026-10-26 本次衔接

T7 的新 R0 已由原独立 reviewer 完成；两次审核脚本自身的 schema 错误保留，修正后的输入审核通过。原 M1 随即生成新的 L0，生成约6.134秒、另一进程验证7.000秒，均 raw0；没有复制旧锁或改写旧 F。随后原 B 已开始 DAILY014。内存 owner 结束后立即启动独立审核，未等待其他模块统一完成。

两次502分别发生于 N8开发和CPU审核的真实外部CLI，均为 raw1、空stdout、输入未漂移。恢复沿用原会话、原目录与原库；原失败不改成成功，已完成检查不无依据重跑。N8有既定单次调用约束，因此由原作者给出精确006续接，保留已耗执行量；没有修改模型或凭据配置。

容量检查使用保留全部历史费用的CAPACITY020；当前重型日线运行独占，M5只进行工具修复，CPU与内存仅在各自预留中运行有界检查。实际并发包含开发、独立审核及原作者的必要澄清；外部CLI会话不显示为Codex原生子代理。

## 2026-10-26 18:48 后继修复

以上17:00衔接段保留当时状态，当前状态以本页表格为准。DAILY014四节点命令使用 `python -B -X utf8 scripts/test_offline.py -q`，选择 `test_t7e2_daily_cold_chain_produces_real_observation`、`test_t7e2_daily_inner_calibration_evidence_meets_policy_floors`、`test_t7e2_daily_missing_background_still_rejects`、`test_t7e2_daily_manifest_matches_tree`，并指定新 basetemp。实际 raw1、1P/3E。正式验证尚未进入锁验证和内层 fit；配置前的 outer stand-in 准备可以调用真实拟合，因此整次进程不能记为“零拟合”。原 owner 在失败后短暂改动冻结 support 并回退的事实保留；端点17个哈希相同不证明整个期间没有写入。后继使用独立 r6 候选和新接受链。

CPU原开发者的三条真实命令为 `python -B -X utf8 -m pytest -q` 加原 M2 六文件、原 M3A 六文件、`tests/test_v2_m5_experiments.py`，实际分别96P/165P/33P、raw0。新的独立复审不使用旧 factors 字节下的测试结果替代新版本必需验证。内存独立审核保留182P/raw0及正式FAIL；组合错误优先级修复已交回原开发者。

本轮若干CLI在完成部分工作后返回HTTP502/raw1/空stdout。原会话、目录、库和失败流均保留；有完整真实交付的CPU直接进入独立复审，没有交付完整结论的审核只续接尚缺步骤。模型配置未修改，不能保证上游502不再发生，也不把它改记为产品成功。Astra的M5预算处置出现一次SSE重连事件，随后真实turn完成、native0、文件交付；接收器raw1及该事件保留。

CAPACITY028保留历史文件与UNKNOWN费用，复用已结束任务的资源。DAILY r6只做1GiB内的局部修复和无拟合预检；M5下一重型运行前须恢复其临时暂停的2GiB预留并重新核对实际余量。M来源实际执行单独预留1GiB内存及256MiB输出，查询和URL上限不因旧失败而重置。没有删除证据、扩大交易权限或启动真实broker。
