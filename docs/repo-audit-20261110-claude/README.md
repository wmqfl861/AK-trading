# 第四轮外部审核（2026-11-10）

审核对象：`main@b75d220` 全仓库 · 审核者：Claude（用户委托的外部审核）· 前三轮：[10-25](../repo-audit-20261025-claude/REPORT.md)、[10-26](../repo-audit-20261026-claude/REVIEW.md)、[11-07](../repo-audit-20261107-claude/REVIEW.md)

> 本目录下的文件都是外部建议，不是有效计划，也不是产品硬审核。有效计划仍是 [r3-001](../strategy-first-blueprint-20261015-r1/CURRENT.md) 及其 H1–H8 增补。本轮没有改动任何产品代码、测试、规则文件或冻结工件。
>
> 蓝图和开发计划是讨论稿：项目所有者表示看完功能说明后再定优化方向。

## 一段话结论

v2 策略库在真实时钟和真实日历下开不出仓：趋势和突破凑不齐入场确认（10 月 26 日的 `b653f7e` 引入，48 项前瞻测试自那时起失败），日线在多交易节的日历上所有策略都凑不齐，震荡三族永远过不了准入。这些在单元测试里看不到，因为夹具用的是理想化的时间和手工构造的状态。在修好之前，继续推进验收和报告得到的只会是零成交。分钟线慢的主要原因也已定位：交易时钟函数每次调用都重建整本日历。

## 文件

| 文件 | 给谁看 | 内容 |
| --- | --- | --- |
| [PROJECT-FUNCTIONS.md](PROJECT-FUNCTIONS.md) | 项目所有者 | 项目现在能做什么，重点是三代策略各自的规则、参数和进度 |
| [REVIEW.md](REVIEW.md) | 所有人 | 完整审核：策略能否开仓、速度、仓库与工程、安全、蓝图与流程；对前几轮的更正；复核命令 |
| [OPTIMIZATION.md](OPTIMIZATION.md) | 项目所有者 | 优化方案：八个方向的选项、代价和建议；需要拍板的 J1–J8 |
| [BLUEPRINT.md](BLUEPRINT.md) | 所有者、计划作者 | 优化蓝图 v4：路线图、出口条件、与 r3-001 的对应、跟踪指标、问题跟踪表 |
| [FIXES.md](FIXES.md) | 计划作者、开发、审核 | 逐项的修改与修复建议：位置、改法、验收 |
| [DEVELOPMENT-PLAN.md](DEVELOPMENT-PLAN.md) | 计划作者、Root | 后续开发计划：依赖、分阶段任务、写集、工作量、检查点、停止条件 |
| [HANDOFF.md](HANDOFF.md) | Root | 交接：开工前检查、第一批任务、边界、复现命令 |
| [AGENT-PROMPT.md](AGENT-PROMPT.md) | 项目所有者 | 发给本地 agent 的提示词 |

## 探针

都在 [probes/](probes/) 下。只读：导入产品代码，不写任何文件，不改任何东西。输入都是合成数据。

| 文件 | 验证什么 | 在 `b75d220` 上的结果 |
| --- | --- | --- |
| `test_entry_confirmation_reachability_probe.py` | 观察期在最后一次确认被观察到时是否还有效（X01） | 5 失败 4 通过 |
| `test_range_family_cross_timeframe_probe.py` | 震荡三族在生产流程给出的状态下能否开仓（X02） | 7 失败 4 通过 |
| `clock_horizon_bench.py` | 交易时钟函数的单次耗时随日历长度的变化；索引版本的等价性（X10） | 250 个交易日时 16 毫秒/次；索引版本 0.3 微秒/次，3,000 组输入逐项相同 |
| `budget_feasibility.py` | 冻结的风控参数下开 1 手所需的最低权益（X04） | 见 REVIEW；内置参数是示意值，请代入真实参数 |
| `static_audit.py` | 调用签名、异常处理、超长函数等静态检查 | 源码里唯一的缺参调用是 `shadow.py:932` |
| `missing_modules.py` | 源码和测试导入的、仓库里不存在的模块（X21） | `ak_trading.datapipeline` 下缺 10 个模块 |

两个探针测试断言的是可交易的策略需要的行为，所以现在失败。失败就是发现；对应的缺陷修好后它们应当通过（K02 的四个状态用例要按计划作者裁定的语义改写预期）。

运行方式：

```bash
python scripts/test_offline.py -q docs/repo-audit-20261110-claude/probes/test_entry_confirmation_reachability_probe.py
python scripts/test_offline.py -q docs/repo-audit-20261110-claude/probes/test_range_family_cross_timeframe_probe.py
python docs/repo-audit-20261110-claude/probes/clock_horizon_bench.py
python docs/repo-audit-20261110-claude/probes/budget_feasibility.py
python docs/repo-audit-20261110-claude/probes/static_audit.py . audit.json
python docs/repo-audit-20261110-claude/probes/missing_modules.py .
```

## 审核环境

Linux、2 核、Python 3.13.18、只装 pytest。没有 tqsdk、sqlalchemy、fastmcp、PostgreSQL，没有访问项目所有者的本机工作区。因此看不到 H5–H8 的候选代码、真实输入包和合同锁；依赖这些的结论在 REVIEW 里都标明了是推断还是实测。
