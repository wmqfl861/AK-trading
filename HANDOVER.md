# AK-trading 项目交接手册

> **历史阶段记录（2026-08-08）**：本文保留当时的状态与证据，不作为当前操作入口。2026-09-08 后的数据库位置、连接、历史行情和策略回测请阅读 [当前操作手册](docs/data-and-backtesting.md) 与 [项目复盘](docs/project-review-2026-09-08.md)。下文中的待办、权限和完成状态均属于原阶段。

> 快照日期：2026-08-08（Asia/Shanghai）  
> 文档版本：1.3  
> 项目目录：`D:\Projects\AK-trading`  
> 配套计划：[PROJECT_PLAN.md](PROJECT_PLAN.md)

## 1. 先看结论

### 1.1 当前阶段

项目处于“交易规则证据工作流已实现、官方数值待人工审核、正式策略开发前”的停点。

已经完成：

- Python/TqSdk 基础会话、行情订阅、具体合约解析和本地 `TqSim` 运行框架。
- 策略运行器、目标仓位、最小仓位风控、事件 journal、JSON 报告和离线证据校验。
- 本机 PostgreSQL 数据库、Alembic 迁移、参考数据和行情导入/查询。
- RB、TA、M 三个主连的近期一分钟 K 线快照。
- 开市前参考报告和旧版 `market_reference.json` 的版本化存储。
- 中国境内六家交易所（SHFE、INE、DCE、CZCE、CFFEX、GFEX）的期货目录边界、81 个期货产品、6 个显式 `excluded_option` 记录、官方域校验和每交易所日历。
- futures-only catalog 的官方证据留存、产品/合约/交易所级候选字段、字段级 scope 审核、不可变 catalog run manifest 和字段级 scoped-current provenance。
- 六交易所 coverage matrix、紧凑工作台 state、只读分页 API、legacy RB/TA/M collector 的显式 `--legacy-three-products` 分支，以及默认 dry-run 的 Windows 调度器安装脚本。
- 交易所规则的可选 PostgreSQL 审计表/迁移/导入 API、隔离 PostgreSQL 测试、备份恢复演练和此前完成的 Claude Code 只读硬审计。

尚未完成：

- 目录包含 81 个期货产品和每交易所一个代表性 option exclusion，但它不是完整期权家族库存；确定性交易所目录刷新 adapter、产品生命周期刷新和 live official collection 尚未实现。
- 新收集的官方规则仍可能是 `candidate` 或 `review_required`；没有人工字段审核就不会进入任何 scoped current。`unknown` 和 `review_required` 不能被批准为数值。
- 旧版 `preopen_market_report.py` 仍只读取 `config/market_reference.json`，本阶段没有直接接入 catalog 或 scoped-current 规则。
- catalog v2 manifests 和 scoped-current pointers 的 canonical source 是本地 `data/exchange_rules/`；PostgreSQL v2 schema/importer 尚未设计、迁移或应用。
- Windows Task Scheduler 没有注册；只有显式使用 `-Apply` 才会执行系统注册。
- 本轮 catalog、workbench 和 legacy-manifest compatibility 改动已完成 focused 验证；完整全量验证、scheduler dry-run 复核和本轮 Claude Code 硬审计仍待完成。
- 六项 PostgreSQL 集成测试因未提供 `AK_TEST_DATABASE_URL` 而跳过；它们没有被记为通过。
- 没有用户确认的正式交易策略规格，也没有策略参数优化、样本外回测或收益/回撤结论。
- 没有 Quick `TqKq` 账户、真实 `TqAccount` 或任何实际下单。
- PostgreSQL 当前不是 `TqBacktest` 的直接行情输入源。

### 1.2 当前必须先处理的阻断项

当前 RB、TA、M 的参考资料状态为：

- `config/market_reference.json` 中费用和保证金来源的 `values_verified` 仍为 `false`。
- 已生成开市前报告中的 `reconciled_with_broker_statement` 仍为 `false`。
- `must_reverify_before` 为 `2026-08-04`，已经早于本手册快照日期。

因此，这些数值不能直接作为新策略的最终成本、保证金或风控输入。下一步必须先重新检查官方交易所来源、适用合约、生效日期和经纪商实际收费，再生成新的不可变参考快照。旧快照保留为历史记录，不能直接覆盖。

### 1.3 给接手人的最短路径

1. 阅读本手册和 [PROJECT_PLAN.md](PROJECT_PLAN.md)。
2. 阅读 [README.md](README.md) 的安装、脚本和数据库命令。
3. 确认本机 `.env` 存在，但不要把内容打印或提交。
4. 执行 `python scripts\manage_database.py status`，只检查安全状态输出。
5. 在策略工作前完成参考数据刷新。
6. 让用户确认第一套策略的完整规则。
7. 按计划先规格审查，再实现和测试；在此之前不要开始策略代码扩展。

## 2. 文档和证据边界

本手册只记录不敏感的项目事实、路径、版本、计数、校验值和流程。以下内容明确不写入仓库：

- `.env` 的实际内容。
- `TQ_USER`、`TQ_PASS` 的值。
- PostgreSQL 应用角色密码或超级用户密码。
- Quick/真实账户凭据、API key、token、会话信息。
- 可用于登录或下单的命令行参数。

密码只允许在本机的授权进程中通过环境变量、交互式 PostgreSQL 提示或明确授权的本地流程使用。桌面上的 PostgreSQL 凭据文件只供本机管理员操作，不是项目配置，也不应复制到交接文档。

证据状态采用以下标记：

- `本次核验`：在本次文档工作中重新执行并得到的结果。
- `历史核验`：此前已完成且在交接摘要中有记录，但本次没有重新执行。
- `配置事实`：直接来自当前仓库配置或数据库记录。
- `待复核`：不能作为下一阶段的安全输入。

## 3. 当前工作区基线

### 3.1 Git 状态

本项目目前还没有任何 Git commit：

- 当前分支：`master`。
- `HEAD` 不存在，不能用提交号作为可靠恢复点。
- 文档编写前的 `git status --porcelain` 统计：81 条。
  - `A `：24 条已暂存新增。
  - `AM`：12 条已暂存且工作区继续修改。
  - `??`：45 条未跟踪。

这些改动属于现有工作区内容，接手或继续开发时不得使用 `git reset --hard`、`git clean`、`git checkout --` 等破坏性操作。创建提交前应先重新记录状态，并与用户确认提交边界。

### 3.2 主要目录

| 路径 | 责任 |
| --- | --- |
| `src/ak_trading/` | 核心配置、会话、策略、runner、风控、journal、报告、参考数据和持久化 |
| `src/ak_trading/persistence/` | PostgreSQL 配置、连接、schema、行情导入/查询、exchange-rule 文件证据导入 |
| `scripts/` | 数据库管理、exchange-rule 收集/审核/工作台、调度器 dry-run、历史下载、行情查询、策略模拟/回测、报告和证据验证入口 |
| `tests/` | 单元、CLI、exchange-rule 源适配器、工作台、行情、参考数据、runner、报告和 PostgreSQL 隔离测试 |
| `migrations/` | Alembic 环境和数据库迁移版本 |
| `config/market_reference.json` | 2026 年旧版参考日历、产品、交易时段、费用和保证金快照源 |
| `config/china_futures_catalog.json` | 六交易所的 futures-only 目录、官方域、日历、81 个期货产品和显式 option exclusions |
| `config/exchange_rule_sources.json` | legacy RB/TA/M 的官方域名和发现配置；只在 `--legacy-three-products` 路径使用 |
| `data/market_reference/` | 本地开市前 JSON/Markdown 产物；被 Git 忽略 |
| `data/exchange_rules/` | 交易所文档、快照、legacy/catalog runs、scope 审核记录和 current pointers；本地 canonical，Git 忽略 |
| `data/db_backups/` | PostgreSQL dump；被 Git 忽略 |
| `data/runs/`、`data/backtests/` | 运行 journal 和报告；被 Git 忽略 |

## 4. 系统结构

### 4.1 逻辑分层

| 层 | 关键模块 | 作用 |
| --- | --- | --- |
| TqSdk 会话层 | `config.py`, `session.py`, `runtime.py`, `symbols.py` | 读取环境变量、创建带 `TqSim` 的 `TqApi`、等待更新、解析主连到具体 FUTURE |
| 策略与风控层 | `strategy.py`, `risk.py`, `runner.py` | 只消费完成 K 线，计算信号，限制目标仓位，驱动 `TargetPosTask` |
| 证据层 | `journal.py`, `artifacts.py`, `verification.py` | 脱敏事件、按 run 写 journal、生成报告、离线重算和验证证据 |
| 参考数据层 | `market_reference.py`, `market_notices.py` | 维护旧版产品、交易日、来源、费用、保证金、时段和开市前报告 |
| 交易所规则层 | `exchange_rules.py`, `exchange_rule_sources.py`, `exchange_rule_artifacts.py`, `exchange_rule_collection.py` | 只收集官方交易所证据，生成候选/审核状态、字段级差异和不可变本地记录 |
| 工作台层 | `exchange_rule_workbench.py` | 绑定 `127.0.0.1` 的只读 HTTP/HTML 检视界面 |
| PostgreSQL 层 | `persistence/config.py`, `database.py`, `schema.py`, `market_data.py`, `query.py`, `storage.py`, `exchange_rules.py` | 迁移、幂等行情、不可变参考快照、交易所规则审计、运行事件和报告存储 |
| CLI 层 | `scripts/*.py` | 把操作边界显式化，检查参数和安全失败 |

### 4.2 数据流

```text
TqSdk / DataDownloader
          |
          v
  临时 CSV 或内存快照 ----> 严格解析与日历检查 ----> PostgreSQL market_bars

config/market_reference.json
          |
          +----> 本地 preopen.json / preopen.md
          +----> PostgreSQL reference_*（保留原始字节和 SHA-256）

config/china_futures_catalog.json
          |
          +----> 六交易所 futures-only 范围、官方域/日历和 explicit option exclusions
          +----> 本机 websearch 官方 URL 发现/抓取
          +----> data/exchange_rules/ 文档、catalog run、候选快照、scope 决策和 scoped-current pointers

config/exchange_rule_sources.json + config/market_reference.json
          |
          +----> 仅在 --legacy-three-products 下保留 RB/TA/M legacy collector
          +----> 旧 global current 与 pre-open 路径，不能作为全市场 catalog 范围依据

TqSim / TqBacktest
          |
          v
StrategyRunner -> JSONL journal -> JSON report -> PostgreSQL strategy_*/run_reports
                                      |
                                      v
                         verify_backtest_artifacts.py（离线证据校验）
```

### 4.3 全品种期货 catalog 与审核边界

默认 collector 使用 `config/china_futures_catalog.json`，把 SHFE、INE、DCE、CZCE、CFFEX、GFEX 的 81 个当前登记期货产品作为预期范围。每家交易所均有官方目录、日历和规则查询边界；每个产品 source URL 也经过对应交易所官方域校验。当前静态 catalog 的成员资格仍是 `review_required`，直到确定性交易所目录 adapter 刷新并归档当期官方目录原文，因此不得把配置文件本身描述为一次 live 官方全量核验。

期权不在当前工作范围。每家交易所保留一个代表性 `excluded_option` 记录（共 6 个），用于证明它已被显式识别；期权从 futures product list、规则 source、候选、scope 审核和 scoped current 中结构性排除。它不是完整期权家族清单，也不采集期权佣金、保证金或规则。

catalog collector 对开市交易所的每个期货产品生成产品级规则 source（`contract=null`）；真实合约差异可另用 contract scope 保存。`catalog_runs` manifest 中：

- `expected_futures` 是本次目录预期范围。
- `processed_futures` 表示产品已进入采集路径，仍可能含 `review_required` 字段，并不表示数值已批准。
- `closed_futures` 表示该产品所在交易所按自己的日历闭市。
- `unresolved_futures` 是既未处理也未闭市的显式缺口，不能默认为零或成功。
- `excluded_options` 是本轮明确排除的 option evidence。

scope current 以字段为单位保存 candidate snapshot、scope approval record、catalog ID 和审批时间；后续 partial approval 只覆盖相同字段，且不能以更早的审批时间回退。只有 `candidate` 字段可以进入 scope current，`unknown`、`review_required`、缺证据和冲突值不能批准。

旧 v2 manifest 若使用历史字段 `collected_futures`，读取时仅在内存中正规化为 `processed_futures`；原始 JSON 字节和原始 `run_id` 不会被重写，且读取仍验证 hash 和 canonical coverage 语义。legacy global snapshot/current 与 catalog scoped-current 是并存且独立的两条审计路径。

### 4.4 关键运行语义

- TqSdk 的 `get_quote`、`get_kline_serial`、账户和订单对象是随 `wait_update()` 更新的 live reference，不是一次性快照。
- runner 只在新 K 线出现时处理，并把最后一行视为尚未完成的 K 线，不作为信号输入。
- `TargetPosTask` 的请求需要后续 `wait_update()` 才会继续处理。
- 本地模拟结束时先目标归零，等待持仓和活动订单归零，再取消 target task。
- 历史回测结束时捕获 `BacktestFinished`；不会假装在历史数据结束后还能完成一个新的平仓成交。

## 5. PostgreSQL 现状

### 5.1 连接和版本

以下信息来自本次只读 `python scripts\manage_database.py status` 输出：

| 项目 | 值 |
| --- | --- |
| 主机/端口 | `127.0.0.1:5432` |
| 数据库 | `ak_trading` |
| 应用角色 | `ak_trading` |
| PostgreSQL | `18.4` |
| Alembic | `20260730_0001` |
| 连接配置 | `.env` 的 `AK_DATABASE_URL`（实际 URL 不写入文档） |
| 应用角色权限 | 普通应用角色，不负责创建/删除数据库 |

数据库状态命令只输出脱敏位置和版本，不应把 `.env` 或密码传给 shell history、日志或聊天。

### 5.2 实际业务记录数

以下计数于 2026-08-05 通过应用数据库连接只读查询重新核验：

| 表 | 记录数 | 责任 |
| --- | ---: | --- |
| `reference_snapshots` | 1 | 参考 JSON 原始字节、规范化 JSON、日历范围和 SHA-256 |
| `reference_products` | 3 | RB、TA、M 的产品与合约元数据 |
| `reference_closed_dates` | 19 | 2026 年配置的闭市日期 |
| `strategy_runs` | 2 | 两次运行 manifest、模式、合约和版本 |
| `strategy_events` | 420 | 运行事件和按 run 的序号/哈希证据 |
| `run_reports` | 2 | 两份完整运行报告 |
| `preopen_reports` | 2 | 两份开市前报告，当前状态均为 `warning` |
| `ingestion_batches` | 3 | 三次独立行情导入批次 |
| `market_bars` | 30,000 | 三个主连各 10,000 根一分钟 K 线 |

### 5.3 数据库责任边界

- PostgreSQL 是运行、参考数据和历史行情的审计/查询存储。
- 文件 journal 和报告先写本地文件，再按运行流程导入数据库；数据库写入失败不能静默当作成功。
- 开市前报告的安全计算仍以规范 JSON 文件为 canonical source；PostgreSQL 不是安全关键计算的替代源。
- 当前没有把 Tick 全量流存入 PostgreSQL。
- 当前没有把 PostgreSQL 直接接入 TqSdk `TqBacktest`。
- 测试必须使用独立、名称以 `_test` 结尾的数据库；测试夹具会阻止误连应用库。

## 6. 参考数据、费用、保证金和交易时间

### 6.1 当前快照身份

| 项目 | 值 |
| --- | --- |
| 源文件 | `config/market_reference.json` |
| schema version | `1` |
| 日历覆盖 | 2026-01-01 至 2026-12-31 |
| 最近审阅日期 | 2026-07-28 |
| 数据库 snapshot id | `1` |
| 内容 SHA-256 | `0ee5168017f4a6aad7be743b147b45fced791a4426b94d7c9310f78cb79a459e` |
| 数据库导入时间 | 2026-08-01 20:57:45 +08:00 |

### 6.2 产品和研究合约

| 代码 | 交易所 | 主连（行情） | 研究合约（需重新确认是否仍有效） | 合约乘数 |
| --- | --- | --- | --- | ---: |
| RB | SHFE | `KQ.m@SHFE.rb` | `SHFE.rb2610` | 10 吨 |
| TA | CZCE | `KQ.m@CZCE.TA` | `CZCE.TA609` | 5 吨 |
| M | DCE | `KQ.m@DCE.m` | `DCE.m2609` | 10 吨 |

主连用于行情发现和历史快照；下单、持仓、`TargetPosTask` 和保证金示例必须使用经过合约发现的具体、未过期 FUTURE。

### 6.3 配置中的旧值（仅历史记录）

这些值展示当前配置的含义，不代表已经批准用于新策略：

| 品种 | 费用模型 | 配置费用 | 配置保证金 | 交易时段 |
| --- | --- | --- | --- | --- |
| RB | 名义金额比例 | 开/平/平今 `0.0001` | 名义金额 `0.07` | 日 `09:00-10:15`, `10:30-11:30`, `13:30-15:00`；夜 `21:00-23:00` |
| TA | 元/手 | 开 `3`，平 `3`，平今 `0` | 名义金额 `0.10` | 日 `09:00-10:15`, `10:30-11:30`, `13:30-15:00`；夜 `21:00-23:00` |
| M | 元/手 | 开/平/平今 `1.5` | 名义金额 `0.07` | 日 `09:00-10:15`, `10:30-11:30`, `13:30-15:00`；夜 `21:00-23:00` |

另外配置了“每个收费侧每手 CNY 0.01”的 broker markup 解释，但 `confirmed_with_broker` 为 `false`。RB 的交易所费用按成交名义金额计算；保证金估算使用结算价。经纪商最低收费、经纪商保证金、滑点、点差、撤单费、交割费和资金成本没有被推断。

### 6.4 参考数据刷新流程

1. 使用本机 `web-search-stack` 检查官方交易所域名，不使用普通网页工具。
2. 只从官方公告/业务规则读取数值，不复制搜索摘要中的数字。
3. 为每个适用合约保存来源、发布日期、生效日期、审阅日期和单位。
4. 对交易所费用与 broker markup 分开记录。
5. 对未知费用保留 `unknown`，不能填 `0`。
6. 生成新的 JSON/Markdown 产物并导入新 snapshot；禁止篡改已有 snapshot。
7. 通过开市前报告和数据库状态命令复核导入结果。

## 7. 历史行情现状

### 7.1 已保存内容

当前 `market_bars` 中有三组独立快照：

| 符号 | 周期 | 数量 | 最早时间 | 最晚时间 |
| --- | ---: | ---: | --- | --- |
| `KQ.m@SHFE.rb` | 60 秒 | 10,000 | 2026-06-23 09:05 +08:00 | 2026-07-31 22:59 +08:00 |
| `KQ.m@CZCE.TA` | 60 秒 | 10,000 | 2026-06-23 09:05 +08:00 | 2026-07-31 22:59 +08:00 |
| `KQ.m@DCE.m` | 60 秒 | 10,000 | 2026-06-23 09:05 +08:00 | 2026-07-31 22:59 +08:00 |

每个导入批次 `rows_read=10000`、`rows_written=10000`、`status=completed`、`null_trading_days=0`。夜盘行的 `trading_day` 可能属于下一个交易日，所以数据库的 `trading_day` 最大值为 2026-08-03，而最后一根 K 线的实际时间仍是 2026-07-31 22:59。

### 7.2 当前数据来源限制

- 近期快照来自 TqSdk 内存窗口，每个符号最多 10,000 根；它不是无限历史。
- 账户的 `DataDownloader` 付费历史权限不足，先前请求的 2026-05-01 至 2026-07-31 长历史基线未能建立。
- 未经权限和完整性验证，不得把三组近期快照称为完整回测历史。
- 精确缺口分类需要完整产品交易时段和例外日历；质量命令报告最大观察间隔，但不会自动把它标为缺失缺陷。

### 7.3 本次质量检查

2026-08-05 通过 `python scripts\query_market_data.py quality` 重新检查：

- 每个符号均为 10,000 行，`distinct_datetime_nanos=10000`。
- 重复身份、非递增时间、无效 OHLC、无效成交量/持仓量、空交易日均为 0。
- 三个符号的最早/最晚时间一致。
- 最大观察间隔被报告为 `208860000000000` 纳秒；该工具明确要求结合完整交易时段和例外日历解释，不能仅凭该数字宣称存在缺失 K 线。

## 8. 运行与策略代码现状

### 8.1 默认账户边界

`session.create_api()` 默认创建本地 `TqSim(init_balance=1_000_000.0)`，并使用 `.env` 中的 `TQ_USER` / `TQ_PASS` 进行 TqSdk 数据认证。当前没有 Quick 或真实账户路径被启用。

### 8.2 现有双均线烟雾测试

`DualMAStrategy` 当前默认：

- 快均线窗口：5 根已完成收盘价。
- 慢均线窗口：20 根已完成收盘价。
- 快线高于慢线时目标仓位为 1，否则为 0。
- 历史不足、收盘价无效或非有限时不产生信号。
- 这是 long/flat 工程信号，不能据此宣称盈利。

`StrategyRunner` 当前提供：

- `sim` 和 `backtest` 两种模式。
- 默认最大绝对仓位 1 手。
- 默认不允许做空。
- 只在新 K 线出现时处理上一根已完成 K 线。
- 记录 `run_started`、`decision`、`run_finished`、`shutdown` 等事件。
- 本地模拟关闭时归零目标、等待持仓和活动订单清空、取消任务并记录结果。

### 8.3 已有入口

| 命令 | 用途 | 重要边界 |
| --- | --- | --- |
| `python scripts\manage_database.py status` | 查看脱敏数据库状态 | 不打印密码 |
| `python scripts\manage_database.py migrate` | 应用 Alembic 迁移 | 生产库操作前确认 URL |
| `python scripts\manage_database.py import-exchange-rules` | 将本地交易所规则证据导入 PostgreSQL 审计表 | 必须先完成迁移；显式执行，不接入交易或开市前安全计算 |
| `python scripts\collect_exchange_rules.py` | 默认收集六交易所 futures-only catalog，生成不可变文档、候选快照和 catalog coverage | 不读 `.env`；每个期货必须是 processed/closed/unresolved；option 显式排除；`review_required`/`unknown` 不自动批准 |
| `python scripts\collect_exchange_rules.py --legacy-three-products` | 运行 legacy RB/TA/M source registry/reference collector | 仅保留兼容路径；不能作为全市场 catalog 范围或 pre-open 自动接入依据 |
| `python scripts\review_exchange_rules.py scope-approve/scope-reject` | 对一个 exchange/product/contract scope 的显式 candidate 字段做 append-only 审核 | 必须带 catalog ID；只有 deterministic candidate 可批准；字段级 scoped current 不能时间回退 |
| `python scripts\review_exchange_rules.py approve/reject` | 记录 legacy global candidate snapshot 的人工批准或拒绝 | 候选快照不原地改名；仅影响 legacy global current |
| `python scripts\exchange_rule_workbench.py` | 启动本地只读交易所规则工作台 | 只绑定 `127.0.0.1`；`/api/state` 紧凑；详细资源分页；浏览器没有审核写入动作 |
| `powershell -ExecutionPolicy Bypass -File scripts\install_exchange_rules_task.ps1` | 输出 Windows Task Scheduler dry-run 定义 | 不带 `-Apply` 不注册任务；不得把凭据放入任务参数 |
| `python scripts\download_market_data.py` | 明确日期范围的 `DataDownloader` 导入 | 需要数据权限和显式确认 |
| `python scripts\snapshot_recent_market_data.py` | 近期内存窗口快照 | 有行数上限，不等于长历史 |
| `python scripts\query_market_data.py quality` | 行情质量检查 | 不自动判断交易时段缺口 |
| `python scripts\query_market_data.py list/export` | 查询或导出已存 K 线 | 不创建 TqSdk 会话 |
| `python scripts\preopen_market_report.py` | 生成参考报告 | 当前来源状态需先刷新；本阶段仍只读旧版 JSON |
| `python scripts\backtest_strategy.py` | 具体合约的 TqBacktest 工程入口 | 当前不读取 PostgreSQL 作为 feed |
| `python scripts\run_sim_strategy.py` | 本地 TqSim 运行 | 不接 Quick/真实账户 |
| `python scripts\verify_backtest_artifacts.py` | 离线验证证据包 | 不读取 `.env` |

运行 TqSdk、行情、账户、保证金或交易相关任务前，先读取仓库规定的 `tqsdk-trading-and-data` 技能说明和所需 reference；不要自行发明 TqSdk API。

## 9. 本地文件与数据库的关系

| 内容 | Canonical/主要来源 | PostgreSQL 中的副本/审计 |
| --- | --- | --- |
| 费用、保证金、时段、日历 | `config/market_reference.json` | `reference_snapshots`、`reference_products`、`reference_closed_dates` |
| legacy 交易所规则文档、候选快照、字段差异和 global 审核指针 | `data/exchange_rules/` | `exchange_rule_source_contents`、`exchange_rule_source_documents`、`exchange_rule_snapshots`、`exchange_rule_items`、`exchange_rule_runs`、`exchange_rule_approvals`、`exchange_rule_current` |
| catalog v2 run、scope decisions 和 scoped-current fields | `data/exchange_rules/catalog_runs/`、`scope_approvals/`、`scope_current/` | 尚未设计或导入 PostgreSQL v2；本地 artifacts 是 canonical |
| 开市前报告 | `data/market_reference/<trading-day>/preopen.json` 和 `.md` | `preopen_reports` |
| 运行事件 | `data/runs/*.jsonl`、`data/backtests/*.jsonl` | `strategy_runs`、`strategy_events` |
| 运行摘要 | 相邻 `.summary.json` | `run_reports` |
| 历史 K 线导入 | TqSdk CSV 或近期快照临时文件 | `ingestion_batches`、`market_bars` |

`data/runs`、`data/backtests`、`data/market_reference`、`data/db_backups` 和导出目录被 `.gitignore` 忽略。它们不能因为“未出现在 Git”就被当作不存在；备份和运行证据需要单独保管和校验。

## 10. 数据库运维手册

### 10.1 安装和迁移

```powershell
cd D:\Projects\AK-trading
python -m pip install -e ".[test,postgres]"
python scripts\manage_database.py migrate
python scripts\manage_database.py status
```

`.env` 应包含 `AK_DATABASE_URL`，但实际密码只留在本机。数据库和应用角色由 PostgreSQL 管理员交互式创建，应用角色不应是超级用户。新增的 `migrations/versions/20260807_0002_exchange_rule_storage.py` 是只增不改的交易所规则存储迁移；先执行 `migrate`，再按需执行 `import-exchange-rules`。本手册第 5.1 节记录的是上次实际核验的数据库版本，不把尚未应用的迁移文件写成已应用状态。

### 10.2 导入参考和已有文件

```powershell
python scripts\manage_database.py import-reference
python scripts\manage_database.py import-existing
```

参考快照按内容 SHA-256 和不可变身份冲突检查；重复导入应保持幂等。导入前先确认源 JSON、匹配 Markdown、目标数据库和当前迁移版本。

交易所规则证据保持本地文件为 canonical source；PostgreSQL 导入是显式的可选审计副本：

```powershell
python scripts\manage_database.py migrate
python scripts\manage_database.py import-exchange-rules
```

导入器会校验内容 SHA-256、相对 `content_path`、父快照顺序、候选/批准快照身份和当前批准指针。数据库不可用时不影响本地收集、审核 CLI 或只读工作台；导入失败必须保留明确的失败状态，不得宣称已同步。

### 10.3 导入行情

长历史：

```powershell
python scripts\download_market_data.py `
  --symbol KQ.m@SHFE.rb `
  --start 2026-01-05 `
  --end 2026-01-30 `
  --duration 60 `
  --acknowledge-data-cost
```

近期窗口：

```powershell
python scripts\snapshot_recent_market_data.py `
  --symbol KQ.m@SHFE.rb `
  --symbol KQ.m@CZCE.TA `
  --symbol KQ.m@DCE.m `
  --duration 60 `
  --rows 10000
```

长历史权限失败时，命令应返回明确的 `DOWNLOAD_FAIL`，不能伪造数据或把未知异常吞掉。近期快照会先在临时目录完成所有符号下载和严格校验，再建立数据库连接；每个符号独立事务，后续失败可能留下前面已提交的批次，必须查看批次状态。

### 10.4 查询和导出

```powershell
python scripts\query_market_data.py quality

python scripts\query_market_data.py list `
  --symbol KQ.m@SHFE.rb --duration 60 `
  --start 2026-07-01T09:00:00+08:00 `
  --end 2026-07-01T10:00:00+08:00 --limit 100
```

时间范围是左闭右开；ISO 时间必须带时区偏移；导出使用同目录临时文件，除非明确 `--force`，不会覆盖已有文件。

## 11. 备份、恢复和灾难处理

### 11.1 当前最终备份

本机已存在以下 custom-format dump：

```text
data/db_backups/ak_trading_final_20260802T000653_d1b6fedd.dump
```

本次核验文件 SHA-256：

```text
9b3beab6e41f0163e848fc52510bb140e5cec604b658a15643ab4ff8f5dca4c
```

文件大小约 827 KB。该备份已在此前完成隔离恢复演练；恢复库的 Alembic 版本和业务表逐行哈希与生产库一致，验证库随后已删除。恢复演练属于 `历史核验`，如生产代码、迁移或数据库内容发生变化，应重新演练。

目录中较早的 dump 可能包含测试残留。恢复时优先使用带 `final` 的文件，并先用 `Get-FileHash` 比对 SHA-256。

### 11.2 备份

让 PostgreSQL 客户端交互式提示角色密码，不把密码放到命令行：

```powershell
pg_dump --host 127.0.0.1 --port 5432 --username ak_trading --password `
  --dbname ak_trading --format custom `
  --file data\db_backups\ak_trading_<timestamp>.dump
```

### 11.3 隔离恢复

永远恢复到单独的新数据库，不覆盖活动库：

```sql
CREATE DATABASE ak_trading_restore OWNER ak_trading;
```

```powershell
pg_restore --host 127.0.0.1 --port 5432 --username ak_trading --password `
  --dbname ak_trading_restore --no-owner --no-privileges `
  data\db_backups\ak_trading_<timestamp>.dump
```

恢复后依次检查：

1. `alembic_version` 与生产库一致。
2. 9 张业务表计数与生产库一致。
3. 关键表逐行哈希或等价内容校验一致。
4. `manage_database.py status` 指向明确的恢复库。
5. 确认没有需要保留的会话后，再终止恢复库连接并删除恢复库。

应用角色不能自行创建或删除数据库；管理员操作前必须再次确认目标数据库的精确名称。

## 12. 验证记录

以下记录属于 catalog v2 扩展之前的历史证据，不能作为本轮 futures catalog、scope current、workbench 分页 API 或 legacy-manifest compatibility 的接受结论。

| 验证项 | 结果 | 时间/证据状态 |
| --- | --- | --- |
| 隔离 PostgreSQL 全量测试 | `255 passed` | 历史核验；随机 `_test` 数据库已删除 |
| 交易所规则 CLI 边界测试 | `6 passed` | 历史核验；覆盖当时 collector/review main、退出码、前缀、审核生命周期和敏感值不回显 |
| 默认全量测试 | `279 passed, 6 skipped` | 历史核验；6 项为未提供 `AK_TEST_DATABASE_URL` 时跳过的 PostgreSQL 集成测试，未计为通过 |
| `python -m compileall -q src scripts tests` | 通过 | 历史核验 |
| `python -m alembic check` | 未通过：目标数据库不是最新版本 | 历史核验；数据库 current=`20260730_0001`，仓库 head=`20260807_0002`；迁移未应用是明确的 PostgreSQL 使用前置门 |
| PowerShell scheduler dry-run | 通过 | 历史核验；输出 `EXCHANGE_RULES_SCHEDULER_DRY_RUN`，未调用 `Register-ScheduledTask` |
| `git diff --check` | 通过 | 历史核验；仅有 LF/CRLF 转换提示 |
| 交易所规则 Claude Code 硬审计 | `ACCEPT` | 历史核验；RF-01 已修复，无 required fix；审计范围不包含本轮 catalog v2 改动 |

本轮验证已完成：`test_catalog_collection.py`、`test_exchange_rules.py`、`test_exchange_rule_cli.py`、`test_exchange_rule_sources.py` 和 `test_exchange_rule_workbench.py` 共 `30 passed`；完整 `python -m pytest -q` 为 `293 passed, 6 skipped`（6 项缺少 `AK_TEST_DATABASE_URL` 的 PostgreSQL 集成测试未计为通过）；`python -m compileall -q src scripts tests` 通过；`git diff --check` 无 whitespace error，仅输出既有 LF/CRLF 转换提示；scheduler dry-run 输出 `EXCHANGE_RULES_SCHEDULER_DRY_RUN`，未注册任务。本轮 Claude Code 硬审计仍待记录。生产数据、迁移、运行代码或文档发生变化后，应按影响范围重新执行验证；历史核验不是永久保证。

## 13. Claude Code 审核记录

以下 Claude Code 结论都是 catalog v2 扩展之前的历史审计，不覆盖本轮 futures catalog、scope current、workbench API 或 legacy-manifest compatibility 改动。本轮 required hard audit 未完成：两次读取型 Claude CLI dispatch 都在模型开始前失败，未生成可接受的 review conclusion。第一次被 CLI 拒绝 JSON Schema `$schema` identifier（`no schema with key or ref "https://json-schema.org/draft/2020-12/schema"`）；移除该 identifier 后，第二次因 watchdog retry 未保留 piped stdin 而失败（`Input must be provided either through stdin or as a prompt argument when using --print`）。两次 raw failure 均已观察，未发出第三次 dispatch；不得将此记为审计通过。

Claude Code 已实际完成代码审核，不是只提出计划：

- 模型：`claude-opus-5`。
- 推理强度：`max`。
- 工具权限：只读 `Read`、`Glob`、`Grep`。
- 第一轮发现两项 HIGH：下载脚本和近期行情快照脚本的异常处理可能掩盖未知程序错误。
- 修复：新增 `src/ak_trading/tqsdk_errors.py`，只转换明确的 TqSdk 权限/认证错误；未知异常继续原样抛出。
- 第二轮硬审计：`ACCEPT`。
- P1-P7：全部 `PASS`。
- AC1-AC12：全部 `PASS`。
- 没有剩余 HIGH 或 CRITICAL 问题。
- 非阻塞 LOW 建议：未来可给查询模块增加 debug 模式，在保护凭据的同时保留底层 traceback。

catalog v2 扩展之前的交易所规则最终硬审计：

- 复核范围：交易所规则域模型、官方来源/本机 websearch 适配器、不可变 artifact store、collector/review CLI、只读工作台、PostgreSQL schema/importer、Task Scheduler dry-run、测试和文档。
- 复核结论：`ACCEPT`；RF-01（CLI main 边界测试缺失）已通过 `tests/test_exchange_rule_cli.py` 修复并独立验证；没有 confirmed required fix。
- 计划矩阵：步骤 1、2、4、5、6 通过；步骤 3、7 因可选 PostgreSQL 迁移未应用和六项 live-DB 测试跳过而标记为部分完成，不影响本地 artifact store 的接受。
- 剩余可选改进：DCE seed URL 的 HTTPS、一致性保护 PostgreSQL current 指针、替换非常规 generator.throw 写法、验证 scheduler 的 Python 可执行性。
- 操作门：在任何 `import-exchange-rules` 前必须先应用 `20260807_0002`；本次没有应用迁移，也没有注册调度任务。

原始审计临时文件已按规则清理，因此本手册保留的是结论和修复摘要，而不是可重新解析的审计 JSON。若后续代码改变下载、快照、持久化、runner、报告或凭据处理逻辑，应重新进行计划审查和硬审计。

## 14. 已知限制和未决事项

1. 参考费用、保证金和交易时段资料已过复核截止日，必须刷新。
2. 当前账户没有付费 `DataDownloader` 历史权限。
3. 近期快照只有每品种 10,000 根一分钟 K 线，不能代表完整历史。
4. 日历配置只覆盖 2026 年。
5. 费用模型没有自动推断 broker 最低收费、滑点、点差、撤单费、交割费或资金成本。
6. 当前没有 Tick 全量存储。
7. PostgreSQL 尚未成为 `TqBacktest` 的直接 feed。
8. 双均线仅是工程烟雾测试，没有盈利、回撤或实盘证明。
9. 交易所规则解析对 HTML、PDF、表格和一般公告保持保守；格式或语义不明确时会保留证据并标记 `review_required`，不会自动生成数值。
10. catalog 当前是官方 URL 有界的静态 81-futures 配置，不是确定性实时目录 adapter；活跃/新上市/摘牌 lifecycle 和 live official full-catalog collection 尚未完成。
11. 期权在本轮被结构性排除，但仅保存 6 个代表性 option exclusions，并不是完整 option family inventory；不采集期权费用、保证金或规则。
12. catalog run 和 scope-current 工作台 API 虽然分页响应，但 `_artifact_view()` 仍遍历本地历史 artifact，尚未实现存储级 lazy pagination。
13. 交易所规则 PostgreSQL v1 表和迁移已实现，但 catalog v2 PostgreSQL schema/importer 尚未设计；数据库导入仍是显式可选步骤，本地 artifact store 仍为 canonical source。
14. 生产 PostgreSQL 尚未应用 `20260807_0002`；在迁移前不得执行 `import-exchange-rules`，六项 live-DB 集成测试也尚未运行。
15. PostgreSQL legacy `current` 指针缺少旧批准记录的时间单调性保护，当前列为可选改进；本地 scope-current pointer 已有该保护。
16. DCE 两个 seed URL 仍使用 HTTP，列为可选传输改进。
17. 调度器安装脚本已实现但默认只 dry-run；本次没有注册 Windows Task Scheduler，实际注册需另行显式授权。
18. 没有 Quick 或真实账户接入验收。
19. 项目暂无 Git commit，任何恢复和差异比较都必须依赖文件/数据库/备份哈希。
20. 本轮 catalog/workbench/compatibility 改动尚未完成新的 Claude Code 硬审计；此前审计结论不能延伸到本轮之后的代码。

## 15. 下一步清单

### 已实现但仍保持非激活状态

- [x] 配置六家交易所的官方目录域、日历、规则查询入口和 futures-only catalog。
- [x] 记录 81 个期货产品、6 个显式 `excluded_option` records、内容 SHA-256、候选快照、字段差异、catalog runs、scope decisions 和 scoped-current provenance。
- [x] 提供显式 legacy `approve`/`reject` 以及 catalog `scope-approve`/`scope-reject` CLI、只读 coverage matrix 和分页工作台 API。
- [x] 兼容旧 `collected_futures` manifest：读取时正规化为 `processed_futures`，不重写旧磁盘 JSON 或 run hash。
- [x] 提供默认 `20:30 Asia/Shanghai` 的 Task Scheduler dry-run 安装器。
- [ ] 未经用户明确授权，不注册实际 Windows 任务。

### 官方审核和参考数据前置

- [ ] 用当前官方目录逐家确认 81 个期货产品的 listed/active/inactive/delisted 状态，并实现或审查确定性刷新 adapter。
- [ ] 人工检查每个实际使用 candidate 字段的官方文档、适用合约、单位和生效日期。
- [ ] 对明确接受的 catalog candidate 执行 `scope-approve`；对冲突或不足证据执行 `scope-reject`，不把 `unknown`/`review_required` 变成零或 current。
- [ ] 重新核验旧版 `market_reference.json` 的费用、保证金和交易时段，并保留旧快照。
- [ ] 确认 broker markup 与结算单是否一致；该核对不替代交易所官方规则。
- [ ] 更新本手册的 snapshot 身份、有效期和验证记录。

### 最终工程验收

- [x] compatibility regression、catalog、artifact、CLI、source 和 workbench focused suites：`30 passed`；`python -m compileall -q src scripts tests` 通过。
- [x] 完成本轮完整验证：`python -m pytest -q` 为 `293 passed, 6 skipped`；`compileall` 通过；`git diff --check` 无 whitespace error；scheduler dry-run 未注册任务。6 个缺少 `AK_TEST_DATABASE_URL` 的 PostgreSQL 集成测试仍未记为通过。
- [ ] 对全品种 catalog/workbench/compatibility 改动执行 Claude Code 只读硬审计；不得把先前审计的 `ACCEPT` 延伸到新增代码。
- [ ] 如要启用 PostgreSQL exchange-rule 审计副本，先应用 `20260807_0002`，配置隔离 `_test` 数据库并补跑六项 live-DB 集成测试；catalog v2 PostgreSQL 设计另行授权，本项未执行。
- [ ] 如要将 catalog rules 或 scoped current 接入生产开市前流程，另行设计和审查集成方案。

### 明确延期

- [ ] 将 catalog rules 或 scoped current 直接接入 `preopen_market_report.py`；本阶段保持旧版 `config/market_reference.json` 路径不变。
- [ ] 全量 option family inventory 和期权规则/费用/保证金采集。
- [ ] catalog v2 PostgreSQL schema、导入器和 storage-level lazy pagination。

### 等待用户确认的策略工作

- [ ] 第一套策略交易品种/具体合约、K 线周期和回测区间。
- [ ] 开仓、平仓、止盈、止损、费用、保证金、滑点和交易时段处理。
- [ ] 仓位、资金、风险上限、评价指标和通过阈值。
- [ ] 正式策略规格、实现、测试、历史回放/回测、本地 `TqSim` 验证和策略硬审计。

### 当前禁止

- [ ] 不使用 Quick 或真实账户。
- [ ] 不下单。
- [ ] 不把未复核的费率/保证金写成已确认事实。
- [ ] 不将近期快照包装成完整历史。
- [ ] 不把密码或 `.env` 内容写入文档、日志、提交或聊天。

## 16. 维护规则

每次阶段变化都要更新：

1. 本手册顶部快照日期。
2. 第 1 节当前结论和第 15 节清单。
3. 第 3、5、7、12 节的实际证据。
4. [PROJECT_PLAN.md](PROJECT_PLAN.md) 的阶段状态、风险和验收结果。
5. `README.md` 中受影响的操作说明。

更新时必须保留旧数据库快照、备份和审核结论的身份；不通过覆盖历史记录来“抹平”差异。任何无法重新执行的历史结论都标记为 `历史核验`，不得写成当前实时状态。
