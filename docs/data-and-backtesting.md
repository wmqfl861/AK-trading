# 数据库、历史行情与策略回测操作手册

本手册对应当前代码入口，核验日期为 2026-09-08。命令从 `D:\Projects\AK-trading` 执行；示例输出目录必须尚不存在。研究输出只写文件，不会向交易账户下单。

## 1. 数据究竟在哪里

### PostgreSQL 主库

| 项目 | 本机核验结果 |
| --- | --- |
| 主机 | `127.0.0.1` |
| 端口 | `5432` |
| 数据库 | `ak_trading` |
| 应用角色 | `ak_trading` |
| PostgreSQL | `18.4` |
| 服务名 | `postgresql-x64-18` |
| 服务程序 | `D:\PostgreSQL\18\bin\pg_ctl.exe` |
| 数据目录 | `D:\PostgreSQLData\18` |
| 服务器时区 | `Asia/Shanghai` |
| 数据库大小 | 约 180 GB，2026-09-08 查询值 |
| 连接配置 | 项目根目录 `.env` 中的 `AK_DATABASE_URL`；进程环境可以覆盖它 |

数据目录由 Windows 服务启动参数只读核验。普通应用角色无权 `SHOW data_directory`，这属于正常权限边界；检查命令遇到这种情况会报告不可查询，不会因此认定数据库不存在。不要直接修改或搬动 PostgreSQL 数据目录中的文件。

`.env` 不提交 Git，也不要把完整 URL 粘贴到日志。以下只是格式示意：

```text
AK_DATABASE_URL=postgresql://ak_trading:<本机密码>@127.0.0.1:5432/ak_trading
```

密码包含 `@` 等字符时需要 URL 编码。日常历史查询只需要数据库连接；`TQ_USER` / `TQ_PASS` 用于连接行情服务，与 PostgreSQL 密码不同。

### 主要数据库表

| 表 | 内容 | 2026-09-08 核验 |
| --- | --- | --- |
| `market_bars` | 具体合约、主连、指数、期权等行情，按 symbol 和周期区分 | 估算 422,015,552 行，含索引约 173 GB |
| `member_position_ranks` | 会员成交量、多头和空头排名 | 估算 20,507,948 行，约 6.6 GB |
| `ingestion_batches` | 导入来源、数量、状态与时间 | 估算 219,044 行 |
| `reference_snapshots` | 不可变参考 JSON 快照 | 1 条，日历只覆盖 2026 年 |
| `reference_products` | 参考快照内的合约参数 | RB、TA、M 三个产品的旧记录 |
| `reference_closed_dates` | 该参考日历中的闭市日期 | 19 条 |
| `strategy_runs` / `strategy_events` / `run_reports` | 旧运行框架的运行与事件证据 | 独立于新离线研究结果 |

大表行数取 PostgreSQL 统计估算，不冒充精确 `COUNT(*)`。新增 `inspect` 默认不扫描全部行情。迁移版本字符串也不保证每个可选表存在，检查结果会单独列实际表和缺失表。

### 本地文件资料

| 路径 | 内容 |
| --- | --- |
| `data/downloads/backfill_contract_universe.json` | 5,062 个合约身份/到期缓存，含过期合约；不是历史费率表 |
| `config/china_futures_catalog.json` | 六交易所品种目录、参考日历和来源边界 |
| `data/settlement_params/YYYYMMDD/` | 已下载的逐合约参数原始 CSV/JSON 与汇总 |
| `data/exchange_rules/` | 官方原文、候选规则和审核记录，主要保存在本地文件 |
| `data/strategy_artifacts/` | 旧阶段冻结输入、研究报告、复现及验收证据 |
| `data/research_runs/` | 本手册建议的新研究输出位置 |
| `data/maintenance_artifacts/project-review-20260908/` | 本轮复盘、修复和实际验收记录 |
| `data/db_backups/` | 仓库已登记的 PostgreSQL 备份文件 |

2026-09-08 看到的结算参数采集文件夹为 9 月 5、6、7 日，交易所原始结算日期实际只有 9 月 4、7 日。它们对近期参数有价值，但不能替代 2020 年以来每天的历史标准。OpenCTP 的逐合约原始值与品种汇总可能不同；接入应保留合约与来源，不能直接套用品种汇总。

## 2. 安装与检查连接

```powershell
cd D:\Projects\AK-trading
python -m pip install -e ".[test,postgres]"
python scripts/manage_database.py status
python scripts/manage_database.py inspect
```

本机已有 `.env` 时不需要重建或覆盖。`status` 显示脱敏连接和版本；`inspect` 列表、估算行数、参考资料范围、实际表存在性及可访问的存储信息。连接失败时先核对本机 PostgreSQL 服务状态、端口和 `.env` 的变量名，再处理身份验证。

数据库常用脚本默认通过 `load_database_settings()` 读取项目 `.env`，不覆盖已存在的进程环境变量。期货导出器保留历史上的“默认只读进程环境”行为，但现在支持显式 `--env-file .env`，所以无须手工复制密码到 shell。指定 env 文件时，已经设置的 `AK_DATABASE_URL` 仍优先；进程中显式空字符串也不会偷偷回退。

可只读查看服务定义：

```powershell
Get-CimInstance Win32_Service |
  Where-Object Name -eq 'postgresql-x64-18' |
  Select-Object Name, State, PathName
```

## 3. 找到并读取一段历史行情

具体合约代码保留原始大小写，例如 `SHFE.rb2101`、`DCE.m2609`、`CZCE.TA609`。品种选择参数 `SHFE.rb` 可正规化；数据库 symbol 不能一律转成大写。周期 `60` 表示一分钟，`86400` 表示日线。

先看单合约有限样本的覆盖情况：

```powershell
python scripts/manage_database.py inspect --symbol SHFE.rb2101 --duration 86400 --limit 1000
```

`--limit` 控制最多检查的最早匹配行数，超过时明确标记截断。采样日期范围不能冒充整个合约的全历史范围。需要窄区间时加带时区的 `--start` / `--end`。

读取前五根日线：

```powershell
python scripts/query_market_data.py list --symbol SHFE.rb2101 --duration 86400 --start 2020-01-01T00:00:00+08:00 --end 2021-02-01T00:00:00+08:00 --limit 5
```

导出单合约原始 K 线 CSV：

```powershell
python scripts/query_market_data.py export --symbol SHFE.rb2101 --duration 86400 --start 2020-01-01T00:00:00+08:00 --end 2021-02-01T00:00:00+08:00 --output data/research_runs/rb2101-bars.csv
```

这个 CSV 用于检查/外部分析；下面的策略回测读取包含合约身份和清单的 `futures_bundle_v1`，不能把普通 CSV 当成冻结研究包。

`quality` 是完整质量计算，开销高于 `inspect`。在 4 亿行表上使用时先限定合约和周期：

```powershell
python scripts/query_market_data.py quality --symbol SHFE.rb2101 --duration 86400
```

### 行情字段含义

- `symbol`：实际保存的具体合约、主连或指数代码。
- `datetime_nano`：权威纳秒时间及去重身份；`ts` 是 PostgreSQL 微秒时间。
- `open/high/low/close`：K 线价格；`volume/open_oi/close_oi`：成交量及持仓量。
- `trading_day`：派生交易日，可为空；`trading_day_source` 说明推导依据。
- `imported_at`：本系统写入时间；不是历史市场首次公开时间。
- `source/batch_id`：来源和导入批次关联。

例如 `SHFE.rb2101` 的 243 根日线在库中，覆盖 2020-01-16 至 2021-01-15；其 `trading_day` 全为空，是因为旧导入使用的日历只覆盖 2026 年。不要把空交易日理解成价格丢失。夜盘交易日也不能简单取时间戳的自然日期。

### Python 中调用

```python
from ak_trading.persistence.config import load_database_settings
from ak_trading.persistence.database import create_database_engine
from ak_trading.persistence.query import parse_aware_datetime_nano, query_market_bars

settings = load_database_settings(required=True)
engine = create_database_engine(settings)
try:
    bars = query_market_bars(
        engine,
        symbol="SHFE.rb2101",
        duration_seconds=86400,
        start_nano=parse_aware_datetime_nano("2020-01-01T00:00:00+08:00", "start"),
        end_nano=parse_aware_datetime_nano("2021-02-01T00:00:00+08:00", "end"),
        limit=1000,
        settings=settings,
    )
    records = [bar.as_dict() for bar in bars]
finally:
    engine.dispose()
```

接口只查询，不执行迁移或导入；价格输出为十进制字符串，计算前按需要转换为 `Decimal`。不要打印 `settings.url`。

## 4. 从数据库冻结研究输入

这一步查询数据库，随后研究与回测只读冻结文件。导出使用同一只读、可重复读事务，并在清单里记录证据；不会修改行情表。

```powershell
python scripts/export_futures_input.py --env-file .env --products SHFE.rb --start 2020-01-01T00:00:00+08:00 --end 2021-02-01T00:00:00+08:00 --duration 86400 --universe-cache data/downloads/backfill_contract_universe.json --catalog config/china_futures_catalog.json --output data/research_runs/rb-history-input
```

`--products` 选择品种，不是某一个交割月；输出可能包含这个品种在区间内的多个合约。回测时再用 `--symbol` 明确选一个合约。

输出至少包含 `bars.csv`、`contracts.json`、`metadata.json`、`availability.json`、最后发布的 `manifest.json`。原始行情、合约身份、来源哈希和限制都保留。旧冻结输入也可继续使用，例如 `data/strategy_artifacts/futures-lib-20260907-codex-01/real-input-02`。

需要清点已有规则文件时，可加重复的 `--local-metadata`：

```powershell
python scripts/export_futures_input.py --env-file .env --products SHFE.rb --start 2020-01-01T00:00:00+08:00 --end 2021-02-01T00:00:00+08:00 --universe-cache data/downloads/backfill_contract_universe.json --catalog config/china_futures_catalog.json --local-metadata data/settlement_params/20260907/openctp.csv --output data/research_runs/rb-history-with-source-inventory
```

该参数现在实际读取允许的资料并记录内容哈希、字段、日期和合约存在性。它提供来源盘点，不会把 2026 年参数批准为 2020 年历史值，也不会自动解锁严格成交模型。

## 5. 使用真实历史价格做策略回测

`backtest_history.py` 是单合约、日线、显式参数的情景回测。它复用现有策略，提供成交和权益账本。费用、保证金、滑点来自用户配置，结果明确标记为假设下的研究结果。

先创建一份配置；下例是 RB 研究的演示假设，不是历史官方参数认证：

```json
{
  "initial_cash": "1000000",
  "lots": 1,
  "multiplier": "10",
  "price_tick": "1",
  "fee_per_lot": "0.01",
  "fee_rate": "0.0001",
  "slippage_ticks": "1",
  "margin_rate": "0.12",
  "max_margin_ratio": "0.40"
}
```

保存为 `config/scenario_rb_example.json` 后运行：

```powershell
python scripts/backtest_history.py --input-bundle data/research_runs/rb-history-input --symbol SHFE.rb2101 --strategy ma_trend --config config/scenario_rb_example.json --output data/research_runs/rb2101-ma-scenario
```

三个可选策略：`donchian_trend`、`ma_trend`、`zscore_reversion`。期限结构 carry 依赖远月合约，未纳入这个单合约入口。这里不使用主连作为可成交合约，不自动换月。

行为约定：

1. 每根日线完成后计算信号，下一根存储日线的开盘执行；不在产生信号的同一根开盘成交。
2. 手数固定，反向时先平旧仓再开新仓；保证金不足时拒绝加仓，仍允许减仓和平仓。
3. 每次成交收取 `手数 × fee_per_lot + 成交名义金额 × fee_rate`，并单独记滑点。两种收费分量相加；此模式统一所有平仓费用，没有冒充历史平今费率。
4. 每根观察结束按收盘价计权益；预先约定最后一根收盘平仓，终止行为及成本写入报告。
5. 缺失或不合法的所选合约价格会拒绝输入，不删除坏行后压缩时间。时间按原始观察排序，未声称完成历史夜盘/特殊日历认证。

输出：`decisions.csv`、`fills.csv`、`equity.csv`、`report.json`、`config.json` 和哈希清单。查看实际成交数、毛收益、手续费、滑点、净收益、回撤及保证金阻断；没有成交就是没有成交，不能据此宣称有效策略。

用相同输入和配置换一个新输出目录重跑，结果应逐字节一致。不要覆盖已经观察过的实验结果，也不要把已使用过的留出样本再次称为未见样本。

## 6. 三种研究路径的区别

| 入口 | 读取数据 | 能回答的问题 |
| --- | --- | --- |
| `study_futures_signals.py` | 冻结真实日线 | 候选方向后续 1/5/20 根观察的价格关联；没有持仓净收益 |
| `backtest_history.py` | 冻结真实日线 + 显式经济假设 | 单合约策略在这些假设下的成交、成本和权益 |
| `research_futures.py --demo --mode backtest` | 合成数据及规则 | 多合约、组合、滚动验证账本的工程正确性 |

历史官方规则逐日匹配的真实严格回测适配器目前尚未实现。`research_futures.py --input-bundle ... --mode backtest` 会明确返回 `real_strict_backtest_not_implemented` 和 `data_readiness=not_assessed`，不再把固定清单伪装成对数据库的缺项审计。

真实输入盘点仍可运行：

```powershell
python scripts/research_futures.py --input-bundle data/research_runs/rb-history-input --mode readiness --output data/research_runs/rb-readiness
python scripts/study_futures_signals.py --input-bundle data/research_runs/rb-history-input --output data/research_runs/rb-price-study
```

就绪报告盘点的是传入的冻结包，不能代替对整个在线数据库或所有本地文件的搜索。

旧 `scripts/backtest_strategy.py` 是 TqSdk 在线历史回放，需要 TqSdk 认证和相应数据权限。它不自动使用 PostgreSQL 中已有的行情。`run_sim_strategy.py` 连接本地 TqSim；上述离线研究不需要运行它。

## 7. 在哪里写策略

核心实现位于 `src/ak_trading/futures/`：

- `spec.py`：合约、K线、策略上下文、结果、费用和风险类型。
- `structures.py`：因果指标窗口。
- `strategies.py`：策略注册与 `evaluate(context, previous)`，信号只消费当前之前的已完成数据。
- `portfolio.py` / `backtest.py` / `walkforward.py`：组合分配、严格账本、滚动窗口。
- `scenario_backtest.py`：单合约历史价格情景回测及报告。
- `io.py`：冻结输入、哈希检查和输出格式。
- `readiness.py`：输入及实现能力诊断。

开发一个策略时，先明确所需行情、指标预热、开仓、退出、持仓状态和费用假设，再实现纯策略逻辑和无未来数据的测试；不要把数据库连接写进每根K线的策略计算。数据库导出与离线计算分开后，同一实验可以复现。

## 8. 验证和常见问题

```powershell
python scripts/test_offline.py -q
python scripts/test_offline.py -q tests/test_futures_scenario_backtest.py tests/test_backtest_history_cli.py
```

离线启动器隔离项目凭据，排除明确的真实数据库/外网测试。排除项会在结果里显示，不算通过。PostgreSQL 集成测试须使用独立、名字以 `_test` 结尾的库，不能把业务库填进测试配置。

强制排除范围为所有 `postgres` 标记测试，以及这三个会访问真实数据库或外部来源的历史用例：

- `tests/test_member_rank.py::TestMemberRankImport::test_import_is_idempotent_and_upserts`
- `tests/test_catalog_collection.py::test_catalog_collection_keeps_other_products_after_one_search_failure`
- `tests/test_catalog_collection.py::test_legacy_three_product_collection_consumes_isolated_search_errors`

该启动器保护可信测试的意外 I/O，不是操作系统沙箱。EDB 任务退出码测试使用显式登记、哈希匹配的临时 PowerShell fixture；仅该上下文允许运行对应脚本，其 Python 子进程仍继承隔离。默认不允许任意原生命令或 shell。`--basetemp` 仅接受尚不存在的目录，防止 pytest 清理已有资料。

| 现象 | 含义及检查位置 |
| --- | --- |
| `AK_DATABASE_URL_not_present_in_process_environment` | 导出默认只读进程环境；本机配置用 `--env-file .env` |
| 查询返回零行 | 先核对具体 symbol 大小写、周期和时区区间，不先重下全库 |
| `trading_day` 为空 | 看 `trading_day_source`；旧参考日历只覆盖 2026 |
| `available_at` 晚于行情年份 | 历史导入时间，不能理解为市场当年不知道价格 |
| `real_strict_backtest_not_implemented` | 当前入口缺少真实规则适配，使用适合目的的情景回测或事件研究 |
| 新输出目录已存在 | 换新实验名，保留旧结果 |
| 元数据文件被拒绝 | 检查格式、大小和允许字段；不将任意凭据文件当作行情参数 |
| 数据库 `data_directory` 无权限 | 应用角色权限正常；按本机 Windows 服务定义定位目录 |

仓库内最近登记的两个数据库 dump 都来自 8 月初、约 0.8 MB，不能代表现在约 180 GB 的数据库。这里没有对其他磁盘的备份作推断，也没有启动全库备份或恢复；详见 [运维与当前缺口](operations.md)。
