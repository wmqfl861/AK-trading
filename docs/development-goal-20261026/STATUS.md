# 策略优先蓝图持续开发状态

更新于 2026-10-26 21:33 UTC。当前 Goal 按有效 r3-001 蓝图继续开发至项目完成，适时推送；上一批 phase 0 已完成，不能把其停止状态解释为新 Goal 暂停。本文是执行状态，不替代 Astra 有效计划、作者处置或独立产品硬审。

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
| T7 / M4 与 DAILY | M4 PASS 已推送。r6b 六周期/真实配置预检修复已完成；独立 R0 reviewer005 完成，原 M1 新 L0 生成及独立进程验证 raw0。005 交接 JSON 语法错误由原 owner006 追加纠正，锁字节未变。 | 完整 DAILY016 四节点已于21:25 UTC交原 B 会话运行；chronological 进程已受控终止并逐项核实退出，容量重新放行；冻结 r6b 不变。 |
| T7 分钟线 CPU | **原独立 reviewer004 无条件 PASS**，四文件已按接受字节合入 main。独立 M2 96P、M3A 165P、M5 experiments 33P，均 raw0；main 集成 **60P/raw0/133.765秒**。 | 本项 CPU 优化完成，见 [接受记录](T7-CPU-ACCEPTANCE.json)。MINUTE 内存、接线和完整冷链仍须单独完成。 |
| T7 分钟线内存 | **独立 reviewer004 无条件 PASS：183P/raw0；main 集成 120P/raw0/276.094秒。** 六文件按审核字节合入。 | 原 CPU owner 已立即开始独立候选中的调用端接线，见 [内存接受记录](T7-MEMORY-ACCEPTANCE.json)。完整 MINUTE 输出/冷链仍受容量及合法后继接受限制。 |
| DISPATCH | preparation028 child1/wrapper1：发布后命中旧 PREIMAGES 引用；allocation005耗尽，历史33。部分 derivation/controls 保留，owner005仍NOT_RUN。 | 原 Astra005 已把完整元数据构造和真实依赖闭包校验移到正式调用前，并给出有界029–031后继；**原 ZCode owner027 正在续接实施（026上游网关错误/native1保留，不重置额度）**。 |
| N8 F02 | 057 CLI native1/HTTP502，固定输入未变，无最终结论；不得凭输出缺失判定额度未消耗。F02仍FAIL，F01OPEN/INSUFFICIENT。 | reviewer058 的消费核对不足，Root已受控停止；原 Astra008 正处置原有故障的后续方式，不自动059、不计硬审通过。 |
| M5 | affected004 已消耗FAIL，pytest未启动；native receipt 指向旧证据目录。原 reviewer024 控制修复已完成 native0，正式执行数0。 | 发布前发现005前置条件分支及预算汇总不一致，原独立 reviewer025 正在其剩余原额度内修正控制工具；seal005及正式005尚未消耗。正式运行等待DAILY重型时段结束。 |
| 已有策略结果与下一交付 | 三个既有 RB 日线 SCENARIO 结果仍有效。**M 春节来源增量获独立限定工程 PASS**，但0新合格来源/0新关系，12缺口仍在，INPUT_NOT_DELIVERED。 | chronological reviewer004 的完整回放超过600秒上限，Root已停止并保留部分输出；原 Astra002 正处理现实可执行的有界复核，未计PASS。M来源接受见 [记录](BIP1-M-SOURCE-ACCEPTANCE.json)，不计历史输入或 paper 交付。 |

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

## 2026-10-26 19:27 已接受的 CPU 交付

CPU四文件主分支合入前逐项核对原基线与独立冻结字节，并确认没有与活跃审核输入相交。独立反例证明乱序插入后的覆盖缓存已正确失效，旧失败版本仍能复现。main命令为 `python -B -X utf8 scripts/test_offline.py -q tests/test_v2_m2_causality.py tests/test_v2_m5_experiments.py --basetemp <独立临时目录>`，60 passed / 0 failed / 0 errors / 0 skipped，raw0；实测峰值181035008 bytes。未以本项PASS替代MINUTE容量、完整冷链或paper接受。

历史chronological审核已完成namespace-adapted H10四文件272P/raw0，另一次106P/raw0；随后CLI502保留，完整独立252子回放尚未执行。M来源owner001只完成部分前置工作后502，实际原SID已确认，owner002继续同一限定来源任务；旧query/URL消费上限不重置。

## 2026-10-26 21:03 UTC 已接受内存与限定来源核查

内存独立审核确认组合缺陷的错误顺序与真实 baseline 完全一致，公开 tuple 和内部 view 路径均通过；原模型、数值、规范输出及异常契约保持。主分支运行 `python -B -X utf8 scripts/test_offline.py -q tests/test_v2_m3a_models.py tests/test_v2_m3a_calibration.py tests/test_v2_m5_experiments.py --basetemp <独立目录>`，120 selected/ran/passed，0 failed/errors/skipped，raw0，进程树峰值178909184字节。无条件 PASS 只覆盖六文件内存单元，调用端接线和完整冷链另行审核。

M来源工程审核独立完成20项语义检查及10项反例；原监督器捕获缺口、冻结文件作者偏离和旧失败保留。2024查询的HTTP502/native22仍记QUERY_FAILED，不能把本项PASS改称数据补齐。

Root 在接线候选准备中错误地假设旧输入清单全部位于main，缺少DAILY清单时停止；又未先检查准备失败便调用watchdog，后者因REQUEST缺失在启动ZCode之前退出。两次raw1与部分候选保留。已按原清单的真实冻结来源补齐，先完整验证全部输入，再序列化发布REQUEST；后续只有准备进程终态raw0且REQUEST真实存在、pins通过才启动依赖调用。原M1的无效交接JSON则由同一owner以新006文件纠正，原文件和错误不覆盖。


## 2026-10-26 21:33 UTC 实际运行与执行问题

内存六文件及其独立审核、main集成已推送 `7a19c77`，主分支集成120P/raw0。调用端接线在独立候选持续开发，已实际修改 experiments.py；尚未提交硬审核，不计入接受。DAILY016原开发会话已启动，四个既定节点使用冻结r6b和新L0，仍须完成验证及原后继审核链。

chronological004的独立回放 Python 缺少有效的外部时限监督。Root发现时，实际子进程已运行2232.074秒，超过工单600秒；核对PID、创建身份、父子关系、argv后仅停止该任务的进程，子进程及CLI均实际退出15，watchdog退出1。原输入未变，r1/r2部分输出保留；不能计完整252回放或独立PASS。随后确认12个原任务子进程身份均不再存活，才释放重型槽给DAILY。原作者正修订这一已有复核的执行方式；后继必须从启动前监督真实进程，并在上限到达时终止及捕获结果，不能只给Bash观察窗口或在main返回后补写记录。

N8 reviewer058的核对只列9项摘要，未完成规定的11项实际命令／结果和关闭区间核对，并在closure UNKNOWN时声称120秒上界；这不足以证明剩余额度。Root受控停止该调用，原native15及部分r16/measure-002保留，未新增059。原作者008处理有界恢复；不能从缺少输出推断从未消耗，也不能把估计当测量。

M5发布前发现RESEAL-013的前置检查仅进入旧004分支，005未触发预期前置检查；预算汇总也写成16800+780=17400。已交原审核者在新r2控制修订中修正，保留原冻结版本；正式005没有因此被执行。DISPATCH026的native1属于上游网关错误，027用原会话接续，所有原限额和失败保留。
