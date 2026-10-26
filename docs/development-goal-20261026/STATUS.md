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
| T7 / M4 | 原 Astra 已明确历史/当前绑定合同；原 M4 owner002 正在独立候选修复测试并验证。B012 已修复两处不存在的账本别名断言并准备新候选，尚无产品 PASS。 | M4 接受后立即合流 B，新 F → R0 → L0；日线四节点可独立推进。原分钟线 7.81 小时及超内存失败保留，不原样重跑。 |
| T7 分钟线性能 | 原作者给出四文件窄优化；独立 ZCode 已提供一次意见。其约 2.7 GiB 来源 ID 指针下界属于静态估算，尚非本轮运行测量。 | 同一 Astra 作者正在处置意见、明确可立即实施的等价优化及必要内存方案；不提高预算或缩小样本来凑通过。 |
| DISPATCH | 原作者保留 25 次历史失败，明确 allocation002 的有限后继；原 ZCode owner017 正在修复。 | 完成真实正反例和冻结后交独立审核；Root owner005 尚未执行。 |
| N8 F02 | 原作者确认 053 拒绝来自审核夹具命名空间不一致，未据此证明产品缺陷。原独立 reviewer054 已续接，产品仍冻结。 | 修正真实 resolver 夹具并在原限额内执行就绪检查；只有完整 PASS 才运行已授权的 008/final002。F02 仍 FAIL，F01 仍 OPEN / INSUFFICIENT。 |
| M5 / 研究输入 | 原作者保留历史 124158 秒下界及未知部分，另给有限前瞻执行预算；原独立 reviewer019 已续接。 | 完成两处既有审核控制修复后，Root 绑定同一尚未执行的 affected003；generation/full100 仍须满足原前置条件。 |

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
