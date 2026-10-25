# AK-trading 仓库审核报告与优化建议

> - 日期：2026-10-25（Asia/Taipei）
> - 审核对象：`main` 分支提交 `b98ef21`
> - 方法：静态分析与人工阅读，未运行测试；凡写“按静态推断”的结论都需要在本机复核
> - 审核者：Claude（claude.ai 会话），不属于项目内的计划、开发或硬审核角色
> - 在线版本（含图表与可更新状态的问题清单，需登录 claude.ai，仅作者可见）：https://claude.ai/artifact/PT8tgMfDRH5Z89dndXkL6h

## 结论摘要

交易安全做得扎实，但有一个对公网明文开放的服务需要本周处理，还有一处影子运行上线前必须修的静默缺陷；更大的问题是流程负担远超产出。

- **安全**：按 `docs/mcp-server.md`，行情 MCP 服务自 2026-09-12 起以明文 HTTP 在 `0.0.0.0:17779` 对公网开放，只靠一个 Bearer 令牌，并以数据库属主账号连接。
- **潜伏缺陷**：v2 决策回放和影子运行调用 `verify_qualification_evidence` 时漏了两个必填参数，产生的 `TypeError` 被 `except (ValueError, TypeError)` 吞掉；一旦影子运行接入策略资格，所有资格都会被判为“证据无效”。目前唯一的调用方和测试都只传空资格，所以还没暴露。
- **工程基础**：仓库没有 CI、类型检查和锁文件；声明支持 Python 3.10，核心模块却用了 3.12 语法；这次快照推送前没有重跑测试。
- **运维隐患**：MCP 与 NE1 执行服务默认共用 17779 端口；客户端观察和执行策略把已到期的 `rb2610` 写死为部署默认值。
- **流程负担**：流程、计划、审核类文档约 1.1 万行，是产品与运维文档的 3 倍；10 月 15–18 日开发代理至少换了 3 次；大量修复在 Git 之外完成。10 月 15 日的项目复评仍认定，没有任何策略经真实历史数据验证有效。

建议的前五项行动：

1. 今天撤掉 17779 的公网放行，改走 VPN 或 SSH 隧道，并轮换 `AK_MCP_TOKEN`。
2. 接通影子运行之前，补上资格校验的两个参数，收窄异常捕获，并加一个“有效资格能通过”的正向测试。
3. 本周接入 CI：ruff、mypy（先只阻断 `call-arg` 和 `name-defined`）、离线 pytest；`requires-python` 改为 `>=3.12`。
4. 两周内以“分支 + PR + CI”作为唯一事实来源，停用仓库外的修复目录和逐文件哈希冻结。
5. 一个月内在保守成本假设下跑出 RB 的第一份样本外结论，再决定哪些平台工作值得继续加固。

## 审核范围、方法与局限

审核对象是 `main` 分支提交 `b98ef21`（2026-10-24 的发布快照，也是仓库唯一一次提交），以静态分析和人工阅读为主，没有运行测试。

- **读了什么**：`src/`、`integrations/`、`scripts/`、`tests/`、`migrations/`、`config/` 和全部文档；重点是 v2 决策与验证链、MCP 服务、无限易观察桥与执行客户端、数据库迁移与行情写入。
- **用了什么工具**：ruff 0.16.9（pyflakes、复杂度、安全规则）、mypy 2.3.1（`--ignore-missing-imports`，按 Python 3.12），以及自写的 AST 和文本脚本（导入关系、文档引用、哈希比对）。
- **为什么没跑测试**：本次审核环境不允许安装和执行仓库代码，也无法访问 PyPI。文中凡写“按静态推断会失败”的，都没有实际运行。
- **没有覆盖**：本机 PostgreSQL 数据、正在运行的服务、仓库外的修复与审核目录（`D:\AK…`）。部署状态以文档记载为准，可能已经变化。
- **关于 mypy**：1,985 条报错里有不少是变量复用或 Optional 未收窄造成的误报；本文只采用逐条核对过的问题。

## 项目概况

仓库约有 25 万行 Python：生产代码约 10.1 万行，测试代码约 11.4 万行，测试投入比生产代码还多。

| 项目 | 数值 |
| --- | --- |
| 跟踪文件 | 746 个；工作区 36 MB，其中一个测试夹具 19 MB |
| 测试代码 | 约 11.4 万行，3,618 个测试函数 |
| 脚本与工具 | `scripts/` 约 1.9 万行（61 个 Python、13 个 PowerShell）；`tools/` 约 1.1 万行；`integrations/` 7,030 行 |
| 文档 | 102 个 Markdown 文件，17,214 行 |
| Git 历史 | 1 次提交（发布快照） |
| CI、lint、类型检查、锁文件、LICENSE | 都没有 |
| 行情库（据 README，2026-09-08 实查） | 约 180 GB；行情约 4.22 亿行，会员排名约 2,051 万行 |

`src/ak_trading` 各包代码行数（含空行和注释，提交 b98ef21）：v2 研究栈 3.6 万行，占生产代码的 36%。

| 包 | 行数 | 备注 |
| --- | ---: | --- |
| v2 | 36,019 | 当前研究主线 |
| 顶层模块（28 个文件） | 18,127 | |
| infiny_execution | 11,001 | |
| futures | 8,591 | v1 研究栈 |
| datapipeline | 7,240 | 与 acquisition 功能重叠 |
| acquisition | 6,047 | 与 datapipeline 功能重叠 |
| persistence | 4,656 | |
| infiny_bridge | 3,269 | |
| probability | 2,750 | v1 研究栈 |
| readiness | 1,788 | 不含包内测试 |
| mcp_server | 894 | |
| testing | 374 | |
| 合计 | 100,756 | |

v1 的 futures 与 probability 合计约 1.1 万行，运行中的观察桥仍在调用；acquisition 与 datapipeline 两套采集合计约 1.3 万行（见 P2-1、P2-2）。

## 做得好的地方

交易安全的默认值和测试隔离比多数个人量化项目更严格，后续精简流程时应当保留。

- **默认不能下单**：各处 `enable_trading=False`；观察策略直接拦截 `send_order` 和 `cancel_order`；`src/ak_trading/session.py` 只接受精确的 `TqSim` 实例；执行客户端另有 8 个状态的本地武装流程。
- **离线测试守卫**：`scripts/test_offline.py` 清除凭据和代理变量，拦截网络与 DNS，并强制排除数据库测试。
- **执行建模细致**：`v2/decision/execution.py` 在下一个合法开盘成交，检查最小变动价位、涨跌停封板、容量、手数步长、手续费和滑点；成本规则未知就拒绝成交。
- **研究可复现**：冻结输入包、因果性测试、按交易日分块的 bootstrap。
- **密钥卫生**：数据库 URL 不进 `repr`，日志只打印安全位置；MCP 审计不记录参数值。扫描未发现提交进仓库的真实密钥。
- **MCP 查询层**：全部参数化 SQL、输入正则校验、查询形状白名单、语句超时和连接级只读。

## P0：本周处理

一件事正在造成风险：对公网明文开放的数据服务。另一件是影子运行上线前必须修的静默缺陷，修复只要几行。

### P0-1 行情 MCP 服务对公网明文开放

- `docs/mcp-server.md` 记载：2026-09-12 起服务绑定 `0.0.0.0:17779`，Windows 防火墙放行任意来源，传输是明文 HTTP；文档自己也写了“令牌和数据在公网可被链路嗅探”。
- 连接用的是数据库属主账号（同一文档：“当前用属主连接 + 连接级只读”），`AK_MCP_ALLOWED_IPS` 默认为空。
- 审计追溯不到来源：`mcp_server/server.py` 的 `_tool_call` 调用 `write_audit` 时从不传 `client`，每条审计的 `client` 都是 null。
- 仓库公开后，端口、内网地址 `10.0.75.2` 和防火墙规则名都能被检索到。

建议：

1. 立即撤掉公网放行，改用 WireGuard、Tailscale 或文档里已有的 SSH 隧道方案；确需公网时，前置 Caddy 或 Nginx 提供 TLS，并配置 IP 白名单。
2. 轮换 `AK_MCP_TOKEN`；启动时拒绝过短的令牌（例如少于 32 字节）。
3. 修好只读角色脚本（P1-6）后，改用 `mcp_readonly` 账号连接。
4. 把 ASGI `scope["client"]` 里的地址传给审计。
5. 若行情来自 TqSdk 或快期账号，对外提供前先确认数据许可是否允许再分发。

### P0-2 资格校验的参数缺失被静默吞掉

变更申请-007 给 `verify_qualification_evidence`（`v2/artifacts.py:1517`）加了两个必填关键字参数 `exit_registry_sha256` 和 `exit_binding`，两个调用点没有同步：

- `src/ak_trading/v2/decision/replay.py:293`
- `src/ak_trading/v2/validation/shadow.py:932`

每次调用都会抛 `TypeError`。外层写的是 `except (ValueError, TypeError)`，于是每个资格都被记成 `QUALIFICATION_EVIDENCE_MISSING` 或 `QUALIFICATION_EVIDENCE_INVALID`；在 SHADOW\_OPERATIONAL 运行中，开仓和加仓随即被判为不合格，结果是全部空仓：看上去像策略没过门槛，实际是接线错误。

目前还没有生效：`run_decision_replay` 唯一的调用方（`v2/validation/experiments.py:1307`）传入的是空资格，影子链路也还没接通（见 P1-2）。正因为它会静默生效，应在接通之前修好。

现有测试发现不了：`tests/test_v2_m4_replay.py` 调用 `run_decision_replay` 时，`qualifications` 都传空元组 `()`。

`replay.py` 的作用域里已经有所需变量，修复只需：

```python
verify_qualification_evidence(
    q, matching[0], qualification_evidence, contract_lock_sha256, registry,
    exit_registry_sha256=exit_registry,
    exit_binding=bindings_by_strategy[q.strategy_id],
)
```

同时把捕获范围收窄到校验自身的异常类型（至少把 `TypeError` 移出去），并补一个“有效资格能通过并允许开仓”的正向测试。`shadow.py` 还需要先接入退出注册表和绑定（见 P1-2）。这类签名漂移，mypy 的 `call-arg` 检查一条命令就能报出来。

## P1：正确性与工程基础

这些问题大多能在提交时被自动检查拦下；先补 CI 和类型检查，再逐项修复。

### P1-1 没有 CI，推送前没有重跑测试

仓库里没有 `.github/workflows`、pre-commit、ruff 或 mypy 配置，也没有锁文件。`docs/delivery-20261024/GITHUB-SNAPSHOT-20261024.md` 写明，这次推送“没有为立即推送重新执行全项目测试”。

| 检查（src、scripts、tools、integrations） | 数量 |
| --- | --- |
| 未使用的导入 / 变量（F401 / F841） | 96 / 31 |
| 未定义名称 / `__all__` 导出未定义名称（F821 / F822） | 6 / 1 |
| 圈复杂度超过 10 的函数（C901） | 439 |
| 宽泛的 `except Exception`（BLE001） | 209 |
| 一行多条语句（E701 / E702） | 313 |
| 仅 src 的 mypy 报错（`--ignore-missing-imports`） | 1,985 |

建议用 GitHub Actions 在 Windows 和 Linux 上各跑一遍：`ruff check --select F,E9,B`、mypy、`python scripts/test_offline.py`。mypy 以现有报错数为基线，只阻止新增；`call-arg` 和 `name-defined` 从第一天起清零。`main` 开启分支保护，只能经 PR 合入。

### P1-2 影子回放整条链路不可用

- 已知问题：`v2/validation/experiments.py:603` 使用了从未赋值的 `_reg`。测试 `test_t8_shadow_replay_consumes_training_then_hits_ruling5_gap` 用 `pytest.raises(NameError)` 把它固定成了“预期行为”。
- 本次新发现：`run_shadow` 中的 `ContinuousLedger.restore`（`shadow.py:975`、`1059`）和 `ledger.checkpoint`（`shadow.py:999`）都缺必填参数 `exit_registry_sha256`。即使补上 `_reg`，这里也会继续抛 `TypeError`。
- `scripts/research_probability_v2.py` 仍对外提供 `--mode shadow-replay`。

建议：接线完成前，入口直接抛出写明原因的 `NotImplementedError`，或从 CLI 去掉这个模式；把钉住 `NameError` 的测试改为断言这条明确的错误。

### P1-3 一个验收测试按静态推断会失败

`tests/fixtures/v2/ep1_t7/acceptance-input-manifest.json` 钉住了 64 个代码文件的 SHA-256，其中 11 个与当前内容不符。差异来自内容改动，不是换行符，例如 `v2/decision/payoffs.py`、`v2/strategies/registry.py`、`v2/validation/experiments.py`。

`test_t7e1_acceptance_input_manifest_matches_tree` 的跳过条件检查的两个函数都已存在，所以它会运行，并断言问题列表为空。

全仓库至少有 14 个测试文件读取源码字节计算哈希。建议源码一致性交给 Git 提交号，测试只钉数据夹具；否则每次重构都要连带更新夹具。

### P1-4 Python 版本与依赖声明不符合实际

- `pyproject.toml` 声明 `requires-python = ">=3.10"`，但 `v2/contracts.py` 从第 22 行起使用 3.12 的 `type` 别名语句，`v2/schemas.py` 用了 3.12 的类型参数语法，`exchange_rule_workbench.py` 用了 3.12 才允许的 f-string 写法。在 3.10 或 3.11 上，这些模块一导入就报 `SyntaxError`。
- 只声明了 `tqsdk` 和 `python-dotenv`；代码还直接导入 pandas、psutil、httpx、openpyxl、fastmcp、starlette、uvicorn、scrapling、akshare、requests。部分包可能经 tqsdk 间接装上，但不应依赖这一点。README 里的 `pip install -e ".[test,postgres]"` 装不全运行所需的依赖。
- TqSdk 版本不一致：项目锁定 `tqsdk==3.10.1`，而项目内 skill 和 10 月 24 日的独立审核针对的是另一个虚拟环境里的 3.10.3。

建议：改为 `requires-python = ">=3.12"`；按用途拆分 extras（如 `mcp`、`scraping`、`research`）；用 uv 或 pip-tools 生成锁文件；统一 TqSdk 版本，并让 CI 安装同一版本。

### P1-5 两个服务默认共用 17779 端口

- 行情 MCP（`scripts/run_market_mcp.py`、`AK_MCP_PORT`）和 NE1 执行服务（`infiny_execution/client_service.py:156`、`scripts/run_infiny_execution.py:62`）的默认端口都是 17779；执行客户端 `AKExecutionBridge.py:92` 也指向 `127.0.0.1:17779/execution/v1/poll`。
- 两个服务都设置了 `allow_reuse_address = True`（即 `SO_REUSEADDR`）。这个选项在 Windows 上的语义与 Linux 不同，可能让第二个进程也绑定成功；冲突会表现为请求落到错误的服务上，而不是启动失败。

建议：执行服务换一个独立端口；所有端口登记在一个配置文件里；Windows 上改用 `SO_EXCLUSIVEADDRUSE`，启动时自检端口归属。

### P1-6 只读数据库角色脚本跑不通

`scripts/sql/create_mcp_readonly_role.sql` 在 `DO $$ … $$` 块里写了 `PASSWORD :'role_password'`。psql 不会替换引号或美元引号字符串里的变量，服务器收到的是字面量，会报语法错误。这与文档中“当前用属主连接”的现状吻合。

修复思路：块外先执行 `SELECT set_config('ak.role_password', :'role_password', false);`，块内用 `EXECUTE format('CREATE ROLE mcp_readonly LOGIN PASSWORD %L', current_setting('ak.role_password'));`；或者改用 `\gexec`。顺带去掉用法示例里多套的一层单引号。

### P1-7 客户端策略写死了已到期合约

- 观察桥 `integrations/infiny/AIBridge.py:178` 和执行桥 `AKExecutionBridge.py:102` 都写着 `DEFAULT_INSTRUMENT_ID = "rb2610"`。代码注释说明，这个无限易版本没有策略参数表单，部署值只能来自这些默认值。
- rb2610 已于 2026 年 10 月中旬到期（上期所螺纹钢的最后交易日是合约月份的 15 日）。照仓库里的版本部署，观察桥会订阅一个已经不交易的合约。
- 执行桥还写死了 `DEFAULT_INVESTOR_ID = "15000180250"`（注释称是模拟账户），已随公开仓库发布。

建议：安装脚本在部署时写入当前主力合约，或由客户端向本地服务查询；合约到期前一周告警；账户号移到本地配置。

### P1-8 交易所网站反爬绕过的合规风险

- `scripts/probe_rank_slider.py` 会自动识别并拖动上期所、上期能源、大商所排名页的滑块验证；`member_rank.py` 和 `exchange_rule_sources.py` 用 Scrapling 的 `StealthySession`（真实 Chrome、拟人鼠标轨迹）穿过网站防火墙。
- 规避网站的技术防护来抓取数据，可能违反网站条款，也可能触及反不正当竞争等法律规定。我不是律师，建议咨询专业意见；代码随公开仓库发布，风险更高。
- `exchange_rule_sources.py:269` 在运行时把 `C:\Users\Administrator\.scrapling\Lib\site-packages` 插到 `sys.path` 最前面，会让另一个解释器的包遮蔽本项目的依赖。

建议：会员排名优先走授权渠道，例如项目已有的 `backfill_tq_rankings.py`（TqSdk `query_symbol_ranking`），或交易所、期货公司、商业数据商的正式接口；抓取代码移到私有仓库；删除 `sys.path` 注入，统一用代码里已有的子进程方式调用那个解释器。

### P1-9 换行符混用

746 个文件里有 127 个用 CRLF，其余用 LF，仓库没有 `.gitattributes`。项目大量按字节比对 SHA-256，换行符一变哈希就变；10 月 24 日的 TqSdk 审核记录就专门解释过 263 个 CR 字节。

建议：加 `.gitattributes`（`* text=auto eol=lf`，`*.ps1 text eol=crlf`），一次性规范化后再更新哈希基线。

### P1-10 公开仓库的范围

仓库是公开的，但没有 LICENSE，却包含内网地址、模拟账户号、公网服务的安全细节、Windows 用户路径和反爬代码。没有许可证时默认保留全部权利，他人并不能合法复用这些代码，公开带来的主要是暴露面。

建议：除非打算开源，先改为私有；若要公开，先清理上述内容，再加 LICENSE。

## P2：架构、性能与数据存储

生产代码里有不少平行实现和超大函数，研究计算全部是纯 Python 加 Decimal；这两点决定了后续的维护成本和运行速度。

### P2-1 v1 与 v2 两套研究栈并存

- v2（约 3.6 万行）是当前研究主线；v1 的 `futures/`、`probability/` 以及顶层的 `research.py`、`strategy.py` 等仍在使用。
- 运行中的观察桥用的是 v1 策略：`infiny_bridge/decision.py` 导入 `futures.strategies.default_registry`。
- `v2/factor_specs.py` 逐字转写了 v1 `probability/factors.py` 的 14 个因子定义，两份需要人工同步。

建议：定下 v1 的冻结和下线日期；观察桥要么改用 v2 接口，要么只做数据采集；共用定义放进一个包里导入，不要转写。

### P2-2 重复的子系统与倒置的分层

- `datapipeline/scheduler.py` 的文档字符串写明，它从 acquisition 调度器“继承语义并重新实现”。两套采集代码合计约 1.3 万行。
- 交易日历与交易时段逻辑至少分布在 5 个模块：`acquisition/calendar.py`、`datapipeline/calendar.py`（1,975 行）、`market_reference.py`、`futures_catalog.py`、`v2/clock.py`。
- 存储层反向依赖业务层：`persistence` 导入了 `futures` 和顶层模块。为绕开循环导入，45 个模块里有 372 处函数内导入，`v2/validation/experiments.py` 一个文件就有 86 处。
- 根因很可能是“按文件归属分工、不得修改他人文件”的规则：遇到共享需求，最省事的做法就是复制。

建议：建一个 `core` 共享层（时间、交易日历、合约、费用规则），采集合并为一条管线；在 CI 里用 import-linter 一类工具约束依赖方向。

### P2-3 超大函数、压缩写法和工单式注释

- 47 个函数超过 150 行，其中 9 个超过 300 行；最长的 `verification._validate_journal_and_report` 有 607 行，圈复杂度 138。
- 最大的单文件是 `AKExecutionBridge.py`（4,603 行）、`v2/contracts.py`（3,596 行）、`verification.py`（3,381 行）和 `infiny_execution/client_service.py`（2,801 行）。
- `v2/validation/` 下大量用分号连写语句；`reports.py` 有 13% 的代码行超过 120 字符，最长一行 511 字符。
- 源码注释和文档字符串里有 600 多处指向工单条款、裁定编号或审核轮次（如 `§5.3`、`RULING 2`、`arbitration`），其中很多文档没有发布进仓库。

建议：引入 ruff format 统一格式；按职责拆分大文件和大函数；注释解释“为什么”，设计决策写成仓库内的 ADR，代码里只引用 ADR 编号。

### P2-4 研究计算的性能

- `src/` 里只有 2 个文件用到 numpy 或 pandas。v2 的概率模型是手写的纯 Python：`v2/probability/models.py:527` 的 `_build_tree` 对每个特征、每个候选阈值都重新划分全部样本，并两遍计算 SSE，单个节点的代价约为“特征数 × 阈值数 × 样本数 × 类别数”。
- 回测在 128 位精度的 Decimal 上下文中运算（`futures/scenario_backtest.py`）。
- 项目状态里已有记录：快照 P2 在 300 秒预算内超时，M4 完整验证超时。

建议：统计和模型改用 numpy 加 scikit-learn 或 LightGBM，固定版本和随机种子即可保证可复现；Decimal 只留在资金账本。若坚持自研树模型，按特征预排序后用前缀和一次扫描全部阈值，代价可降到“特征数 × 样本数 × log(样本数)”。

### P2-5 行情表设计与写入语义

- `market_bars` 是一张未分区的大表（README：行情约 4.22 亿行，库约 180 GB）。价格用 `NUMERIC(28,10)`；每行重复存 `symbol` 和 `source` 文本；还有冗余的自增 `id`、同义的 `ts` 与 `datetime_nano` 两个时间列，以及分别建在两者上的索引。按 180 GB 粗算，每行约 400 字节（含索引和其他表）。
- 写入用无条件的 `ON CONFLICT DO UPDATE`（`persistence/market_data.py:358`）：重复导入也会重写整行，造成表膨胀和大量 WAL；较晚的数据源会静默覆盖旧值、不留修订记录，不利于时点一致（PIT）研究。
- MCP 每次请求都新建数据库连接，没有连接池。

建议：

1. 合约字典表加整数外键；价格用 `float8` 或按最小变动价位缩放的 `int8`；以 `(symbol_id, duration, ts)` 为主键，去掉冗余列和索引；按周期和年份分区，时间列加 BRIN 索引。
2. 或者把分钟历史放进 Parquet，用 DuckDB 读给研究；PostgreSQL 只放元数据和日线。
3. 更新时加 `WHERE (…) IS DISTINCT FROM (…)`，值真的变了才写，并把旧值写进修订表。
4. MCP 改用 `psycopg_pool`。

粗估：行存优化后体积可降到原来的三分之一左右，列存压缩还能再降一个数量级，需要实测确认。

### P2-6 运行环境与测试资产

- 16 个模块用 `Path(__file__).parents[n]` 推算仓库根目录，打包安装后会找不到数据和配置；`scripts/minute_workers_watchdog.py` 写死了 `D:\Programs\Python\Python312\python.exe`。
- `scripts/` 平铺 61 个 Python 脚本和 13 个 PowerShell 脚本，夹杂一次性脚本（如 `sweep_retry2.py`、`mopup_rank_symbols.py`）。
- 21 处 `pytest.skip` 依赖本机冻结包或证据目录，干净克隆里这些集成测试都会跳过；一个 19 MB 的 JSON 夹具占了测试目录的大半体积。
- `src/ak_trading/readiness/tests/` 把测试放进了发布包。

建议：用环境变量（如 `AK_DATA_ROOT`）定位数据目录；脚本按 `ops/`、`backfill/`、`research/` 分组，一次性脚本归档；合成夹具由代码生成，大夹具压缩或改用 Git LFS。

## 流程与项目治理

最大的优化空间在流程：现行流程产出的文档、证据和交接，远多于可以检验的策略结论。

- **文档构成**：按文件名粗分，流程、计划、审核和状态类文档约 11,200 行，产品与运维文档约 3,600 行。`AGENTS.md` 有整节规定 `rg` 的退出码、PowerShell 的 `$PID` 变量和 Grok 会话恢复。
- **状态页记录进程而非产品**：`PROJECT-STATUS.md` 满是 PID、会话 ID、`D:/AKRV…` 路径和审核路由，看不到数据覆盖率、回测指标或缺陷趋势。README 指向的“当前路线”是 10 月 10 日的蓝图，而状态页说当前入口是 10 月 15 日的修订版。
- **事实不在仓库里**：发布说明写明 M5、DISPATCH、N8 的修复在“外部修复目录”，没有合入；证据放在 `D:\AK…` 目录；仓库只有一次快照提交。851 处文档内部引用中有 305 处在仓库里解析不到：117 处指向被忽略的 `data/`，87 处是 Windows 绝对路径，59 处指向未发布的流程目录，42 处是其他缺失文件。
- **哈希冻结在重复 Git 的功能**：逐文件 SHA-256 清单、写冻结、一次性命令授权，提供的正是 Git 提交号已有的内容寻址快照；却缺了 Git 加 CI 最有价值的部分，即每次提交自动重跑全部检查。P0-2 这样的签名漂移在这套流程下仍然进了主分支，而 mypy 一条命令就能发现。
- **代理路由频繁变化**（摘自 `PROJECT-MEMORY.md` 和 `PROJECT-STATUS.md`）：

| 日期 | 变化 |
| --- | --- |
| 2026-10-20 | 再次确认开发用 gpt-6.1-sol/max |
| 2026-10-18 | 开发先改为 Claude Code，同日又改为 gpt-6.1-sol/max；4 个 Claude 开发调用以退出码 130 中止并留档 |
| 2026-10-18 | 计划审核改为只提供意见 |
| 2026-10-17 | 未指定模型的辅助任务改用 OpenCode 默认配置 |
| 2026-10-16 | 未指定模型的辅助任务改用 Sol/max |
| 2026-10-15 | 开发由 Sol 改为 ZCode GLM-5.3/max |

- **产出**：首轮真实研究（9 月 8 日）的选择器全部空仓；10 月 15 日的复评认定没有任何策略经真实历史数据验证有效；10 月 24 日 M/DCE 交付的研究完成记分为 0。

建议：

1. **Git 作为唯一事实来源**：每项工作一个分支、一个 PR；PR 描述写计划要点和验收标准；CI 全绿且一名审核者批准就合入。提交号就是冻结点，不再另做逐文件哈希清单。
2. **角色少而稳**：一个规划者、一个实现者、一个审核者，模型选择写进一个配置文件，至少一个季度不改；审核对象是 PR 的 diff。
3. **状态页只放产品指标**：数据覆盖率与新鲜度、CI 通过率、每个策略的样本外结果、未关闭缺陷数。PID 和会话 ID 写进日志，不进文档。
4. **文档分层**：README 讲怎么跑，`docs/architecture.md` 讲模块和依赖，`docs/adr/` 每个决策一页，`docs/runbooks/` 讲运维；历史蓝图、审核报告和事故记录归档到单独目录或另一个仓库。`AGENTS.md` 只保留与代码有关的约束。
5. **研究先行**：在保守成本假设下（例如手续费按现行标准的 1.5 倍、保证金率上浮 5 个百分点、每边滑点 1–2 跳）先跑出 RB 的第一份样本外 A/B 结论，PIT 规则证据并行补齐；只有显示出优势的策略，才值得投入平台级加固。这与 10 月 15 日复评提出的“RB-A/B-1 先行”路线一致。

## 建议路线图

1. **第 1–2 周 · 止血**：关闭 MCP 公网端口；轮换令牌，启用只读账号；补齐资格校验的两个参数；影子回放显式禁用或接线；执行服务改用独立端口；部署时写入当前主力合约。
    - 出口：公网不可达，回放能接受有效资格。
2. **第 3–4 周 · 自动化**：CI（ruff、mypy、离线测试）；Python ≥ 3.12，补全依赖；生成依赖锁文件；`.gitattributes` 统一换行；外部修复目录改走 PR；测试不再钉源码哈希。
    - 出口：`main` 每次提交 CI 全绿。
3. **第 2–3 个月 · 收敛**：RB 首份样本外 A/B 结论；v1 冻结，观察桥迁到 v2；合并采集管线与交易日历；模型改用 LightGBM 等库；行情表分区或转 Parquet；删除重复实现与一次性脚本。
    - 出口：拿到一份可判定的策略结论。

第一阶段处理会造成损害的问题，第二阶段让同类问题在提交时自动暴露，第三阶段再回到研究结论与架构收敛。前一阶段的出口没达到，就不要开始下一阶段的大改动。

## 附录：问题清单与复核命令

22 个问题按优先级排列，状态列可以直接下拉更新；编号与正文小节对应，G 开头的三项来自“流程与项目治理”。

| 编号 | 优先级 | 位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| P0-1 | P0 | `docs/mcp-server.md`、`mcp_server/server.py` | MCP 以明文 HTTP 对公网开放；属主账号连库；审计无来源 IP | 撤掉公网放行，改走隧道；轮换令牌；只读账号；审计记 IP | 未开始 |
| P0-2 | P0 | `v2/decision/replay.py:293`、`v2/validation/shadow.py:932` | 资格校验缺两个必填参数，`TypeError` 被吞；影子运行时资格会全部判无效 | 补参数；收窄异常；加正向测试 | 未开始 |
| P1-1 | P1 | 仓库根目录 | 无 CI、lint、类型检查和锁文件；推送前未重跑测试 | GitHub Actions：ruff、mypy 基线、离线 pytest；保护 `main` | 未开始 |
| P1-2 | P1 | `experiments.py:603`、`shadow.py:975/999/1059` | 影子回放：`_reg` 未定义，3 处账本调用缺参数；测试把 `NameError` 当预期 | 先显式禁用该模式，再接线 | 未开始 |
| P1-3 | P1 | `tests/fixtures/v2/ep1_t7/acceptance-input-manifest.json` | 64 个钉住的源码哈希中 11 个已漂移，对应测试按静态推断会失败 | 源码一致性交给提交号，测试只钉数据 | 未开始 |
| P1-4 | P1 | `pyproject.toml` | 声明 Python ≥3.10 却用 3.12 语法；10 个直接依赖未声明；TqSdk 3.10.1 与 3.10.3 不一致 | `requires-python >=3.12`；补 extras 和锁文件；统一 TqSdk | 未开始 |
| P1-5 | P1 | `run_market_mcp.py`、`client_service.py:156`、`AKExecutionBridge.py:92` | MCP 与执行服务默认同用 17779，且都开 `SO_REUSEADDR` | 执行服务换端口；集中登记端口；Windows 独占绑定 | 未开始 |
| P1-6 | P1 | `scripts/sql/create_mcp_readonly_role.sql` | psql 变量写在 `DO` 块里不会被替换，脚本必然失败 | 用 `set_config` 加 `current_setting`，或 `\gexec` | 未开始 |
| P1-7 | P1 | `AIBridge.py:178`、`AKExecutionBridge.py:102–104` | 部署默认合约 rb2610 已到期；账户号写在源码里 | 部署时写入主力合约并提示到期；账户号移出源码 | 未开始 |
| P1-8 | P1 | `probe_rank_slider.py`、`member_rank.py`、`exchange_rule_sources.py:269` | 拟人浏览器绕过交易所防护；运行时注入外部 site-packages | 改用授权数据源；抓取代码私有化；删除注入 | 未开始 |
| P1-9 | P1 | 全仓库 | 127 个 CRLF 文件与 LF 混用，没有 `.gitattributes` | 加 `.gitattributes`，一次性规范化 | 未开始 |
| P1-10 | P1 | 仓库可见性 | 公开仓库没有 LICENSE，含内网地址、账户号和安全细节 | 改为私有，或清理后加 LICENSE | 未开始 |
| G-1 | P1 | `PROJECT-STATUS.md`、`AGENTS.md`、`PROJECT-MEMORY.md` | 流程文档约为产品文档 3 倍；状态页记进程；4 天内开发代理至少换 3 次 | 角色固定；状态页只放产品指标；文档分层归档 | 未开始 |
| G-2 | P1 | 仓库外 `D:\AK…` 目录 | 修复与证据在 Git 之外；36% 的文档内部引用解析不到 | 以分支、PR 和 CI 作为唯一事实来源 | 未开始 |
| G-3 | P1 | 研究路线 | 还没有经真实数据验证的策略结论 | 保守成本假设下先跑 RB 样本外 A/B | 未开始 |
| P2-1 | P2 | `futures/`、`probability/`、`infiny_bridge/decision.py`、`v2/factor_specs.py` | v1 与 v2 并存；观察桥用 v1 策略；因子定义逐字转写 | 定 v1 下线日期；共用定义抽成包 | 未开始 |
| P2-2 | P2 | `acquisition/`、`datapipeline/`、日历相关模块 | 采集与调度重复实现；5 处日历逻辑；分层倒置；372 处函数内导入 | 建 `core` 共享层；合并管线；CI 约束依赖方向 | 未开始 |
| P2-3 | P2 | `verification.py`、`AKExecutionBridge.py`、`v2/validation/` | 47 个函数超 150 行，最高复杂度 138；最长一行 511 字符；600 多处工单引用 | ruff format；拆分大文件；改用 ADR | 未开始 |
| P2-4 | P2 | `v2/probability/models.py:527`、`futures/scenario_backtest.py` | 手写纯 Python 模型与 128 位 Decimal 运算，已有超时记录 | numpy 加 scikit-learn 或 LightGBM；Decimal 只留账本 | 未开始 |
| P2-5 | P2 | `migrations/versions/20260730_0001_postgres_storage.py`、`persistence/market_data.py:358`、`mcp_server/queries.py` | 行情表未分区，冗余列与索引；无条件覆盖写入；MCP 无连接池 | 紧凑表结构加分区或 Parquet；`IS DISTINCT FROM` 与修订表；`psycopg_pool` | 未开始 |
| P2-6 | P2 | 16 个模块、`scripts/`、`tests/` | 依赖仓库目录结构；脚本平铺；21 处 skip 依赖本机冻结包；19 MB 夹具 | `AK_DATA_ROOT`；脚本分组归档；夹具由代码生成或用 LFS | 未开始 |
| P2-7 | P2 | `futures/data.py:49` | `__all__` 导出了不存在的 `instance_observation_start` | 删除该项或补上实现 | 未开始 |

复核本报告中静态数字所用的命令（在仓库根目录执行）：

```bash
# 未定义名称、未使用导入等（pyflakes 规则）
ruff check --isolated --select F,E9 src scripts tools integrations
# 签名漂移：缺参数、未定义名称
cd src && mypy --ignore-missing-imports --python-version 3.12 -p ak_trading | grep -E "\[(call-arg|name-defined)]（见在线文档）"
# 在 Python 3.11 下能否解析（应改为 >=3.12 的依据）
ruff check --isolated --target-version py311 --preview --select E9 src
# 换行符分布
git ls-files --eol | grep -c "i/crlf"
```
