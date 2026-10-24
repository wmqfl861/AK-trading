# 项目复盘与修复 · 2026-09-08

本轮由用户要求进行整体复盘、修复和优化。复盘确认：此前数据下载成果存在，主要问题包括文档滞后、连接入口不一致、数据来源未接入就绪诊断、真实回测能力说明失真和运维失败状态传播不完整。

## 实际资产与问题

数据库实查为 `127.0.0.1:5432/ak_trading`，PostgreSQL 18.4，服务数据目录 `D:\PostgreSQLData\18`，约 180 GB。`market_bars` 估算 422,015,552 行，`member_position_ranks` 估算 20,507,948 行。`SHFE.rb2101` 的 243 根历史日线可查询，交易日为空来自旧日历覆盖限制；不能再把这些元数据空值解释为行情未下载。

| 问题 | 本轮处理 |
| --- | --- |
| 旧阶段文档被当成当前状态，基本入口依赖会话记忆 | README 改为操作导航，新增中文数据/回测及运维手册，历史内容归档保留 |
| 数据库脚本只有 status，没有可用的数据盘点 | 新增只读 inspect，显示实际表、估算规模、参考覆盖和有限合约样本 |
| 导出连接方式与查询脚本不同 | 导出新增显式 `--env-file`，保留进程环境优先，错误不暴露凭据 |
| `--local-metadata` 接受文件却不读取 | 实际捕获允许的资料、哈希、字段、日期和合约存在性，并明确未完成历史认证 |
| 非合成回测直接报固定缺数清单 | 改成 `real_strict_backtest_not_implemented`，数据就绪明确为未评估 |
| 已有真实行情没有可执行的策略账本入口 | 新增单合约日线情景回测，显式配置经济参数，记录决策、成交、成本和权益 |
| 离线测试依赖历史产物目录里的 guard | 新增长期稳定离线启动器，隔离凭据并显式排除真实服务用例 |
| EDB 部分失败可被汇报成成功 | Python 返回非零，PowerShell 检查和传播阶段结果 |

## 本轮实现边界

真实历史情景回测复用现有策略，按已完成日线决策、下一根开盘执行。金额用 Decimal，平仓和反手分别记成本，固定手数，开仓检查保证金。所有费用、滑点和保证金参数显式写入配置及结果，不伪称历史官方标准。它不会连接 TqSdk 或下单。

严格历史规则适配器仍未实现；当前程序会清楚区别能力未实现、传入包中缺少字段、存在参考来源但历史适用未核验。这比仅显示全部 blocked 更能指明实际工作。

数据库未执行迁移或大表回填，已有参数和实验未覆盖，现有采集进程未被重启。没有修改模型、全局规则或凭据，没有 Git 提交。

## 验收记录

实施期间的首个行为回归已复现原错误：真实回测入口把能力未实现返回为缺数据/待授权。修复后专项测试通过。历史行情查询示例已在实际数据库执行并返回正确的五条日线。

实际端到端验收已完成：

- `manage_database.py inspect` 默认及 `SHFE.rb2101` 限定查询均成功；事务实际为 repeatable read/read only，限定查询返回 243 行且未截断。
- 使用显式 `--env-file .env` 从业务库只读导出 RB 品种区间，共 2,530 根日线、84 个声明合约。其中 17 个合约在该区间有行情；声明的其他交割月没有区间行情不构成缺失证明。
- `SHFE.rb2101` 双均线情景回测使用 243 根日线，16 条成交；毛损益 -2,160 元、费用 60.348 元、滑点 160 元，净损益 **-2,380.348 元**。示例配置为演示假设，未根据亏损更换样本或调参。
- 最终输出与独立目录重放的六个文件逐字节一致。主代理另从成交文件复算费用、滑点、平仓损益、现金和前一根信号关系，全部一致。
- 元数据导出实际读取 866 合约 OpenCTP CSV，原始文件哈希吻合；就绪报告显示 `source_inventory_status=loaded`、`source_presence=present_reference_only`、`historical_metadata_adapter=not_implemented`，不再把来源未接入与数据库无数据混淆。
- 20 个本轮 Python 文件语法解析及文档本地链接检查通过；`git diff --check` 无 whitespace error，仅有原有换行格式提示。

三组独立审核均为 `PASS_SCOPE`：数据库/文档、回测会计/因果/发布、导出/元数据/离线隔离。已修复审核发现的五个问题：盘点快照不一致、search_path 表定位、manifest 冲突覆盖、合约 JSON 重复键、经济字段适用合约串用。回测审核还用独立 Fraction 运算复算 72 次回放、10,800 个权益点和 412 条成交，并比较 2,648 个策略决策/年龄转移，全部通过。

最终完整离线回归：**1,273 passed，9 deselected，0 failed，0 skipped**，耗时 638.55 秒，命令退出码 0。相较修复前最后一轮选中的 1,058 项，增加 215 项测试；9 项 PostgreSQL/真实外网用例仍未计为通过。

运行命令：

```powershell
python scripts/test_offline.py -q --basetemp data/maintenance_artifacts/project-review-20260908/tmp-full-regression-final
```

该 `--basetemp` 已用于本轮验收，重跑时应换新目录或省略该选项。原始日志为 `data/maintenance_artifacts/project-review-20260908/full-regression-final.log`，独立审核见 `review-summary.json`，真实端到端及重放核验见 `e2e-verification.json`，最终文件与结果清单见 `execution-result.json`。

实际示例产物：

- 冻结输入：`data/research_runs/project-review-20260908-rb-input/`。
- 参数来源盘点输入：`data/research_runs/project-review-20260908-rb-metadata-input/`。
- 最终回测：`data/research_runs/project-review-20260908-rb2101-ma-final/`。
- 确定性重放：`data/research_runs/project-review-20260908-rb2101-ma-replay/`。
- 来源和能力就绪诊断：`data/research_runs/project-review-20260908-rb-readiness/`。

## 仍需关注的事项

- 规则原始文件确实已采集，近期结算参数覆盖尚不构成多年逐日规则库。不得将 2026 年合约参数自动套用到 2020 年。
- 仓库登记的备份只有 8 月初约 0.8 MB 的 dump，不代表现有约 180 GB 数据库；没有对其他磁盘备份情况做推断。
- EDB 任务运行超时长但进程仍活动；未证实卡死。本轮修复未来运行的失败报告，不重复启动现有任务。
- 期权最近全量任务日志不完整，后续 6 符号验证不代表全量成功。历史退出码来源受其后脚本修改影响，尚无已证实根因。
- 多合约换月、逐日历史官方参数严格回放、实盘账户和策略盈利认证均不包含在本次“工程入口修复已完成”的含义内。

计划和机器可读证据保存在 `data/maintenance_artifacts/project-review-20260908/`。实际操作入口见 [数据库与回测手册](data-and-backtesting.md)，运维事实见 [运维与当前缺口](operations.md)。
