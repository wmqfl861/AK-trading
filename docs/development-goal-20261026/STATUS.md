# 策略优先蓝图持续开发状态

2026-10-26。当前 Goal 按有效 r3-001 蓝图继续开发至项目完成，适时推送；上一批 phase 0 已完成，不能把其停止状态解释为新 Goal 暂停。本文是执行状态，不替代 Astra 有效计划、作者处置或独立产品硬审。

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
| T7 / M4 | **M4 一文件独立无条件 PASS：14 / 80 / 259 项均 raw 0，无失败/跳过/错误。** main 精确字节集成及 14 项定向检查 raw 0。 | 原 B013 已接到接受后的 M4 和补齐输入，正在重建真实 producer 绑定及新 F；之后立即 R0 → L0，日线四节点优先。 |
| T7 分钟线性能 | CPU 四文件 owner001 与内存六文件 owner001 并行开发；均已有原 Astra 生效处置，不等待重复计划审核。 | 完整等价/因果及原测试人口；内存单元先给精确输出容量证据。原超内存、超时记录保留，未重跑完整分钟冷链。 |
| DISPATCH | 原 owner018 按作者新有限 allocation003 修复；旧 allocation002 已耗尽。 | 先完成真实本地检查，025/026最多两次；Root owner005 仍未执行，之后独立硬审。 |
| N8 F02 | reviewer055 在真实静态预检中发现绑定仍钉住旧规则文件，FAIL / INPUT_DRIFT；precheck009/final002均未调用。 | 原 Astra author003 明确最小产品绑定修复及后继独立复核；不重跑055，不改回旧规则。F02 FAIL，F01 OPEN / INSUFFICIENT。 |
| M5 | reviewer019 控制修复完成，11项本地检查通过；Root 使用冻结工具实际封装003准入 raw 0。 | **原独立 reviewer020 已开始 affected003**；只有完整通过才 generation001，再由 Root 按原规则绑定 full100。 |
| 已有策略结果 | 已复核三个接受的 RB 日线 SCENARIO 实验，见 [策略结果一页说明](STRATEGY-RESULTS.md)。 | Donchian 在共同样本领先，但资金压力下反转；仍需正式历史输入与独立统计验证，不能计为 paper 或正式历史验收。 |

运行请求与原始流位于本机 `C:/AKWorktrees/aktr-goal-20261026/`；外部 CLI 不显示为 Codex 原生子代理。各模块使用独立会话库与不重叠写集；不得凭 CLI 原生 0 认定任务完成。

## 本次已推送与实际命令

- `c152e33`：A4 两文件产品及独立接受；`git push origin main` 原始退出码 **0**，目标 `https://github.com/wmqfl861/AK-trading`。
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
