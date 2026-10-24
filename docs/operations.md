# 运维与当前缺口

核验日期：2026-09-08。数据库使用和研究命令见 [数据与回测手册](data-and-backtesting.md)。本页区分实际运行观察与尚未完成的维护工作。

## 数据服务

PostgreSQL 服务 `postgresql-x64-18` 正在运行，数据目录 `D:\PostgreSQLData\18`，数据库 `ak_trading` 位于 `127.0.0.1:5432`。程序目录 `D:\PostgreSQL\18\bin`。日常使用不需要重启服务。

```powershell
python scripts/manage_database.py status
python scripts/manage_database.py inspect
Get-Service postgresql-x64-18
```

`inspect` 默认读取数据库元数据和小型参考表；大表是统计估算。需要质量分析时优先指定具体合约与周期，避免无意对约 4.22 亿行情行运行聚合或全表窗口。

## 数据更新任务

2026-09-08 只读查询 Windows Task Scheduler 看到六个已注册任务：

| 任务名 | 查询时状态 | 最近任务结果 |
| --- | --- | --- |
| AK-trading EDB Contract Bar Update | Running | 267009（调度器运行中状态，不能据此判定失败） |
| AK-trading Main Minute Fill | Ready | 0 |
| AK-trading Member Rank Collection | Ready | 0 |
| AK-trading Member Rank DCE | Ready | 0 |
| AK-trading Option Daily Update | Ready | 15（需要结合该脚本和日志解释） |
| AK-trading Settlement Params Collection | Ready | 0 |

这些是查询时的状态快照，不保证每个来源都全量成功。任务退出码为 0 也不替代业务行数或来源覆盖审计。当前代码目录提供对应的 `run_*_daily.ps1` 和安装脚本；本轮没有重新注册、取消或重启任务。

进一步核查（11:41–11:46 CST）：EDB 分钟更新进程已运行约 17.6 小时，40 秒内 CPU 和 I/O 仍增长；当前日志被进程独占，因此尚不能确认业务进度，也没有将其判为死锁或终止。任务采用 `IgnoreNew`，若到下一次触发仍未结束会跳过新实例。另发现已完成的 9 月 6 日日线日志含 782 个成功、17 个失败，但原 Python 和 PowerShell 退出逻辑可能仍返回成功；本轮修复了这类失败状态传播，当前已启动进程不被重启。

期权任务最近的 15 并非当前代码定义的成功或部分完成状态；历史任务事件为 `0x8007000F`，但代码在该次任务之后已有修改，不能断言错误根因。六个当晚分片日志均缺少 `DONE`，后续成功报告仅覆盖 6 个符号、90 行，不能据此声称全量更新成功。本轮保留这一运维未决事项，不重跑任务。

只读列出任务：

```powershell
Get-ScheduledTask | Where-Object TaskName -like 'AK-trading*' |
  Select-Object TaskName, State
```

在任务运行状态与实际日志进度矛盾时，先核对日志是否增长、关联进程是否仍在执行及具体重试原因；不要仅凭显示 Running 就终止，也不要在已有任务运行时重复手工启动同一采集器。

## 备份现状

本次只盘点了项目登记的 `data/db_backups/`，其中两份 dump：

- `ak_trading_20260801T233241_a8dfe82d.dump`：834,212 字节。
- `ak_trading_final_20260802T000653_d1b6fedd.dump`：827,253 字节。

它们对应 8 月初小规模数据。当前数据库查询大小约 180 GB，因此旧文档的“最终备份”不能作为现库可恢复证明。本次没有检查所有磁盘是否另有备份，也没有启动高容量备份/恢复作业。

新备份应选择容量充足的独立目的盘，记录创建时间、数据库/迁移身份和内容校验，随后恢复到隔离数据库验证。不要覆盖业务库，不要把密码写在命令参数。下面是交互式示意，路径需要按实际存储容量选择：

```powershell
D:\PostgreSQL\18\bin\pg_dump.exe --host 127.0.0.1 --port 5432 --username ak_trading --password --dbname ak_trading --format custom --file E:\Backups\ak_trading_YYYYMMDD.dump
```

示意中的 `E:\Backups` 不是已确认存在的备份位置。没有足够磁盘空间与明确目的地时，不应自动运行这一示例。

## 迁移与实际表

迁移版本不是表存在性的替代证据。当前 `20260831_0003` 的历史分支从 `20260730_0001` 继承，交易所规则 `20260807_0002` 是另一条分支；因此 live DB 显示 0003 与没有 exchange-rule 表可以同时成立。

只读检查默认显示实际表与代码预期表的差异。本轮没有自动合并迁移分支或在 180 GB 业务库应用迁移。未来引入数据库规则存储时，应先完成隔离测试、增量迁移和备份设计。已有本地规则文档不会因为缺少审计表而消失。

## 仍需保持清晰的边界

1. 历史行情大量已下载，`trading_day` 等派生字段未完善不能被报告成行情不存在。
2. 近期参数原始文件已保存，但多年逐日规则覆盖和版本生效仍需整理。OpenCTP 参考、交易所规则、经纪商实际收费应分开记录。
3. 原始逐合约参数与品种汇总可能不同。研究要明确使用哪个来源，不能默认以汇总替代合约明细。
4. `backtest_history.py` 可运行历史价格情景回测；历史官方规则的真实严格适配仍未实现。这是两种不同精度的研究能力。
5. 主连适合行情研究与选约，不应伪装成一个始终可成交的具体合约；单合约回测不自动跨月。
6. 上一阶段留出已经使用，后续实验不能将其称为未见样本。
7. 项目尚无首个 Git 提交，现有工作区有大量既有内容。任何整理、提交或恢复必须保留现有文件；本轮没有执行提交、清理或重置。

旧数据完整性报告 [DATA_COMPLETENESS_REPORT_2026-09-04.md](../DATA_COMPLETENESS_REPORT_2026-09-04.md) 是当日的采集质量证据，其中后续已注册任务等事项须以当前运行状态为准。它没有证明历史交易费用或保证金序列完整。
## 数据库维护


### 索引与 LIKE 前缀(2026-09-13 根治)

库排序规则为 ICU `en-US`,默认 B-tree 下 `LIKE '前缀%'` 无法用索引(字节序
与排序规则序不一致),曾导致多次数十秒级全索引扫。
`market_bars` 现有专用索引:

    ix_market_bars_symbol_pattern_duration (symbol text_pattern_ops, duration_seconds)

3.0GB,2026-09-13 以并行构建建(88 核/16GB 排序内存,耗时 65 分钟)。
`symbol LIKE '前缀%'` 查询现在走纯索引扫描(实测热缓存 0.015s)。
注意:`text_pattern_ops` 索引按字节序排序,不能用于 `ORDER BY symbol` 的
排序规则序输出——DISTINCT/排序仍会加小排序节点,属正常。
写入代价:行情导入多维护一个索引(约 10% 写放大)。
