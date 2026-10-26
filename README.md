# AK-trading

本地中国期货数据与策略研究项目：PostgreSQL 保存历史行情和会员排名，冻结文件承载可复现的策略研究；TqSdk 在线模拟属于另一条显式入口。

**数据库：`127.0.0.1:5432/ak_trading`；本机数据目录：`D:\PostgreSQLData\18`；连接变量：项目 `.env` 中的 `AK_DATABASE_URL`。** 2026-09-08 实查数据库约 180 GB，行情约 4.22 亿行、会员排名约 2,051 万行（行数为数据库统计估算）。

## 从哪里开始

- [策略优先蓝图（2026-10-25 r3-001）](docs/strategy-first-blueprint-20261015-r1/revisions/r3-001/BLUEPRINT.md)：当前有效规划，围绕正确策略、分钟与日线独立回测及本地 paper 组织交付；版本与意见处置见[当前规划入口](docs/strategy-first-blueprint-20261015-r1/CURRENT.md)。
- [实施计划](docs/strategy-first-blueprint-20261015-r1/revisions/r3-001/IMPLEMENTATION-PLAN.md)与[修复顺序](docs/strategy-first-blueprint-20261015-r1/revisions/r3-001/REPAIR-ORDER.md)：七个里程碑、实际消费依赖、文件归属与 22 项问题处置；本次规划发布不自动启动开发或运行。
- [外部审计逐项核实（2026-10-25）](docs/repo-audit-20261025-claude/VERIFICATION.md)：11 项成立、11 项部分成立及实际核验结果，不是产品硬审核。
- [项目历史状态快照（保留至 2026-10-18 的记录）](PROJECT-STATUS.md)：历史成果、审核范围与运行过程；当前规划及新核验事实以以上入口为准。
- [上一版现状审核与工程积压](docs/development-blueprint-20261010-r1/REVIEW-AND-PLAN.md)：保留原 32 条工作线的证据核对；调度优先级以策略研究主线蓝图为准。
- [数据库、历史行情、策略与回测操作手册](docs/data-and-backtesting.md)：地址、连接、字段、查询、Python API、冻结导出、真实日线回测及策略开发。
- [运维与当前缺口](docs/operations.md)：计划任务、备份、迁移和诊断边界。
- [概率驱动策略研究](docs/probability-strategy-guide.md)：因子注册、多期限路径概率、策略净EV、最高EV/概率抽样对照及新鲜度检查。
- [中国期货因子库与使用建议](docs/factor-library.md)：605条研究定义、91条品种/规格、18,153条适用关系，原包保真归档、版本登记、只读查询和现有14项计算器口径对照。
- [概率策略第一版计划](docs/probability-strategy-plan.md) 与 [首轮真实实验结果](docs/probability-strategy-results-2026-09-08.md)。
- [本轮项目复盘](docs/project-review-2026-09-08.md)：问题、修复、验证结果和仍未实现的能力。
- [无限易 PythonGO 观察桥接](docs/infiny-bridge.md)：本地17778服务、独立观察策略、账户/行情只读接收与模拟订单验证；真实报单关闭。
- [行情数据 MCP 服务](docs/mcp-server.md)：17779 端口只读 MCP(Streamable HTTP + Bearer Token),远程 agent 接入、安全护栏与审计。
- [econ 宏观新闻与分析 MCP](docs/econ-mcp.md)：项目级 HTTPS 接入、9项工具实测、宏观因子使用方法及会议降级/预测时效边界。
- [接手快照](TAKEOVER_2026-09-08.md)：修复前已核验的历史状态。

旧 [HANDOVER.md](HANDOVER.md)、[PROJECT_PLAN.md](PROJECT_PLAN.md) 是 2026-08-08 阶段记录，不能作为当前运行说明。改版前的完整 README 保存在 [历史说明](docs/archive/README_2026-09-08_before-review.md)，已有实验产物保持原样。

## 1. 检查环境与数据库

```powershell
cd D:\Projects\AK-trading
python -m pip install -e ".[test,postgres]"
python scripts/manage_database.py status
python scripts/manage_database.py inspect
python scripts/manage_database.py inspect --symbol SHFE.rb2101 --duration 86400 --limit 1000
```

已有 `.env` 不需要重建。检查输出不包含数据库密码。`inspect` 默认读取表统计，具体合约检查只读取有限样本；不会先扫描整个行情库。

## 2. 调用历史行情

```powershell
python scripts/query_market_data.py list --symbol SHFE.rb2101 --duration 86400 --start 2020-01-01T00:00:00+08:00 --end 2021-02-01T00:00:00+08:00 --limit 5
```

`60` 是一分钟，`86400` 是日线。具体合约保留大小写；时间区间左闭右开。`trading_day` 为空可能是旧日历覆盖不足，不能因此认为价格未下载。数据库字段和 Python 示例见[操作手册](docs/data-and-backtesting.md#3-找到并读取一段历史行情)。

## 3. 冻结数据库输入

```powershell
python scripts/export_futures_input.py --env-file .env --products SHFE.rb --start 2020-01-01T00:00:00+08:00 --end 2021-02-01T00:00:00+08:00 --duration 86400 --universe-cache data/downloads/backfill_contract_universe.json --catalog config/china_futures_catalog.json --output data/research_runs/rb-history-input
```

导出查询使用只读事务。此后计算只读输入文件，不再需要数据库或 TqSdk 连接。输出目录必须不存在；已有冻结包可以直接复用。

## 4. 用真实日线做策略回测

```powershell
python scripts/backtest_history.py --input-bundle data/research_runs/rb-history-input --symbol SHFE.rb2101 --strategy ma_trend --config config/scenario_rb_example.json --output data/research_runs/rb2101-ma-scenario
```

这是**显式经济假设下的单合约情景回测**：上一根完成日线产生信号、下一根开盘执行，包含手数、开平仓、手续费、滑点、保证金检查和权益账本。示例配置中的经济参数是演示假设，不是逐日历史官方参数认证。支持 `donchian_trend`、`ma_trend`、`zscore_reversion`；不自动换月，不调用交易账户。

结果写入 `report.json`、`decisions.csv`、`fills.csv`、`equity.csv`、`config.json`、`manifest.json`。同一输入和配置写到另一个新目录即可验证可复现性。

## 概率与策略选择研究

```powershell
python scripts/research_probability_strategies.py --input-bundle data/strategy_artifacts/futures-lib-20260907-codex-01/real-input-02 --config config/probability_rb_example.json --output data/research_runs/probability-rb-first
python scripts/check_data_freshness.py --expected-through 2026-09-07 --symbol SHFE.rb2610 --duration 86400 --exchange DCE
```

新流程计算14项因子、首达障碍/期限概率和条件策略净EV，比较突破、回归、回踩、有限库存网格、概率抽样与现金。概率抽样固定种子并保留完整权重；经济参数是显式假设。首轮真实研究中选择器按预设门槛全部空仓，不能宣称已有盈利或胜率提升，详情见[结果记录](docs/probability-strategy-results-2026-09-08.md)。新鲜度检查只查询指定样本，过期返回非零，不重启或执行下载。

## 其他研究入口

| 命令 | 用途 |
| --- | --- |
| `scripts/study_futures_signals.py --input-bundle ... --output ...` | 真实价格候选事件及 1/5/20 根观察标签，没有账户收益 |
| `scripts/research_futures.py --input-bundle ... --mode readiness --output ...` | 冻结包来源、字段和实现能力盘点 |
| `scripts/research_futures.py --demo --mode backtest --validation walk-forward --output ...` | 合成数据上的多合约滚动回测验证 |
| `scripts/backtest_strategy.py --symbol ... --start ... --end ...` | TqSdk 在线历史回放，需要行情认证；不会自动使用本地数据库 |

历史官方规则逐日匹配的真实严格回测适配器尚未实现；相应 CLI 明确报告功能状态，不再用固定“缺数据”清单代替实际判断。已有规则原始文件的存在、是否载入、历史覆盖和审核状态是不同事实。

## 测试

```powershell
python scripts/test_offline.py -q
```

此入口隔离项目凭据并排除真实数据库/外网测试。

**测试状态更新（2026-10-26，引用已完成的 C05 独立审核）**：C05 两文件导入隔离修复已接受，范围限离线启动器及其测试，不代表全套测试通过。完整入口原生退出码为 **2**：6,015 项进入执行，5,754 passed、171 failed、60 errors、30 skipped，另有 3 个文件收集失败。详见 [C05 独立审核](docs/development-20261025-zcode-r3/C05-REVIEW.md)与[接受记录](docs/development-20261025-zcode-r3/C05-ACCEPTANCE.json)。这些是该次冻结候选的历史实测，不冒称当前 HEAD 已全量复验。

**修复前核验历史（2026-10-25）**：C05 曾为 raw 2、174 个收集错误、0 个测试执行；C07 曾为 raw 1、11 条 T7 code_drift，见[原逐项核实](docs/repo-audit-20261025-claude/VERIFICATION.md)。这些记录保留溯源，已不代表 C05 隔离修复后的执行状态。

**历史记录**：2026-09-08 概率研究第一版曾完成 **1,589 passed，9 deselected，0 failed**，真实研究15文件重放一致，详见[概率研究验收](docs/probability-strategy-results-2026-09-08.md)。该记录不代表当前代码的绿色基线。此前工程修复阶段的1,273项回归、三组审核及六文件回放记录保留在[项目复盘](docs/project-review-2026-09-08.md)。新增策略位于 `src/ak_trading/futures/strategies.py`，数据类型在 `spec.py`，单合约研究回测在 `scenario_backtest.py`；测试放在 `tests/`。数据库查询、策略计算和实验输出分别负责自己的输入输出，详细调用契约见[操作手册](docs/data-and-backtesting.md#7-在哪里写策略)。
