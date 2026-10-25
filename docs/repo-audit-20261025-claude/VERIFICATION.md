# 外部静态审核逐项核实（2026-10-25）

本文件核实 [REPORT.md](REPORT.md) 的 22 条问题。原报告由 claude.ai 对 `b98ef21` 做静态审核，作者未运行测试；原报告及本核实文档均不是本项目的计划审核或独立产品硬审核结论，不能换算成产品 PASS/FAIL。

## 核实基线与边界

- 已先执行 `git pull --ff-only origin main`，原始退出码 **0**，从 `b98ef21` 快进到 **`8ac6c71f72e8aca08c6d2b7531152d81e761a27d`**。
- `git merge-base --is-ancestor 8ac6c71 main` 退出 **0**；`git diff --name-status b98ef21 HEAD` 仅显示新增 `docs/repo-audit-20261025-claude/REPORT.md`。本次当前源码与被报告审核的快照没有 Git 内容差异。
- 已完整阅读原报告，核对本地现行规则、当前源码、必要的仓库外修订与冻结状态。以本机实际文件为准，未把仓库外未合入的修复算作当前代码已修复。
- 使用 codebase-memory 定位符号与调用链，索引 generation 为 `2026-10-24T00:18:52Z`。对 499 个受检 Python 文件分批检查覆盖：497 个显示 `metadata_changed`，2 个为 excluded/not_tracked；SQL 文件有部分解析缺口。因此重要结论均回到当前源文件、AST 或实际探针验证，不以图谱的无结果证明代码不存在。
- 未读取、打印或修改真实 `.env`、账号密码、令牌或数据库连接凭据。测试子进程只注入明确列出的非敏感系统变量；项目 offline guard 在收集测试前安装，拦截真实 dotenv、外网、DNS 与数据库连接。未运行行情登录、真实 broker、交易或部署；`enable_trading=False`、observer=`hold` 的边界未改变。
- 未修改源码、测试、配置、规则文件、原报告或任何审核冻结文件。本轮仓库内新增交付为本文件；诊断工具、缓存及原始输出放在 `D:/Maintenance/aktr-repo-audit-20261025/`。未新增开发蓝图，未自动修改治理规则或 GitHub 可见性。
- 原报告 SHA-256 保持 `92065bb87eef2c53a742782efba569f98d5a284740c079b887f73e1550baa936`，33,291 bytes。

**逐项结论：11 项成立，11 项部分成立；没有项目可以据本次证据标为整体“不成立”或“已修复”。** “部分成立”表示问题中的某些断言需要纠正或缺少运行证据，不表示核心风险可以忽略。源码中的错误已存在于原快照，本轮没有直接修复它们。

## 已执行命令与原始结果

解释器为 `D:/Programs/Python/Python312/python.exe`，Python **3.12.10**；pytest **9.1.1**。本机原来没有 ruff/mypy，已在仓库外的 `tools-ruff0169-mypy231` 独立 venv 安装与报告相同的 **ruff 0.16.9、mypy 2.3.1**，没有改变项目依赖或默认解释器。下表中的 ruff/mypy 使用该 venv 的绝对可执行路径。

原始输出目录统一为 `D:/Maintenance/aktr-repo-audit-20261025/`。捕获器成功只说明输出收集成功；下表保留的是子命令的真实退出码，绝不把检查失败改记为成功。

| 证据编号 | 实际命令或探针 | 原始结果 | 原始记录 |
| --- | --- | --- | --- |
| C01 | `ruff check --isolated --select F,E9 src scripts tools integrations` | **exit 1**，140 项：F401=96、F841=31、F821=6、F822=1、F541=4、F811=2。另有两条无效 noqa 警告。 | `ruff-F-E9.stdout.txt`、`.stderr.txt`、`.result.json` |
| C02 | 在 `src` 中运行 `mypy --ignore-missing-imports --python-version 3.12 -p ak_trading` | **exit 1**，**1,983 errors / 115 files，checked 184 source files**。不能直接把全部类型诊断认定为运行缺陷。 | `mypy-src.stdout.txt`、`.result.json` |
| C02a | 对 C02 完整输出执行报告附录原样的 grep 筛选（含“（见在线文档）”） | **grep exit 1**，0 行；报告把“（见在线文档）”带进了匹配条件。 | `mypy-filter-bash.result.json`、`mypy-bash-appendix_literal.stdout.txt` |
| C02b | 对 C02 完整输出执行修正后的 grep 筛选，准确命令见下方代码块 | **grep exit 0**，17 行。P0-2 的四条缺参、P1-2 的 `_reg` 和账本缺参均出现。 | `mypy-bash-corrected.stdout.txt` |
| C03 | `ruff check --isolated --target-version py311 --preview --select E9 src` | **exit 1**，44 项 Python 3.11 不支持的语法诊断。 | `ruff-py311.stdout.txt`、`.result.json` |
| C04 | `git ls-files --eol`，其输出交 `grep -c 'i/crlf'` | **git exit 0 / grep exit 0**，计数 **120**。Git 的 `i/mixed` 另有 7 个，不在此 grep 计数中。 | `git-eol.stdout.txt`、`git-eol.result.json` |
| C05 | **`python scripts/test_offline.py -q`** | **exit 2**。**174 errors during collection；pytest 92.13 秒，含启动的捕获总时长 97.906 秒。未进入测试执行，不能报告全套测试通过率。** | `offline-full.stdout.txt`、`.stderr.txt`、`.start.json`、`.result.json` |
| C06 | `python -X utf8 -`：安装 offline guard，调用真实 validator；再执行从当前 replay/shadow 提取、未修改的资格 try/except AST。输入为明确标记的 SYNTHETIC 对象。 | **probe exit 0** 表示复现断言成立；两处真实调用均抛缺两个必填 keyword-only 参数的 `TypeError`，原 except 块均得到 `QUALIFICATION_EVIDENCE_INVALID`。不是有效资格端到端交易测试。 | `p0-2-minimal-repro.json` |
| C07 | `python scripts/test_offline.py -q tests/test_v2_m5_t7_evidence.py::test_t7e1_acceptance_input_manifest_matches_tree` | **exit 1，1 failed，0 deselected**；确认 11 条 `code_drift`，不是 skip。pytest 3.16 秒。 | `p1-3-targeted.stdout.txt`、`.result.json`、`p1-3-code-drift.json` |
| C08 | 使用独立 venv 的 pglast 8.7，离线解析原 SQL 中的 DO 块，并与仅替换占位符的 SYNTHETIC 对照比较 | 原块 `ParseError: syntax error at or near ":"`；对照可解析。**probe exit 0**；没有连接或修改 PostgreSQL，没有执行 CREATE/ALTER ROLE。 | `p1-6-sql-parse.json` |
| C09 | 两个临时 loopback TCP socket，系统分配端口；第一个默认 reuse=0，第二个 reuse=1 尝试绑定同一地址 | **probe exit 0**；第二个 bind 返回 **WinError 10013**，未发生重复监听。不是对 17779 或真实服务的试启动，socket 已关闭。 | `p1-5-synthetic-socket-probe.json` |
| C10 | offline guard 下导入真实 `ak_trading.futures.data`，检查属性并执行 `from ... import *` | **probe exit 0**；`__all__` 含名称、模块没有对应属性；星号导入实际抛 `AttributeError`。 | `runtime-export-probe.json` |
| C11 | `ruff check --isolated --select C901,BLE001,E701,E702 --output-format json src scripts tools integrations` | **exit 1**；C901=439、BLE001=209、E701=184、E702=129；目标验证函数复杂度 138。 | `ruff-structure.stdout.json`、`.result.json` |
| C12 | `git ls-files -z` 后对当前跟踪源码做 AST、行数、导入、skip 和 Markdown 显式链接统计 | **exit 0**；口径与具体结果在下文说明，不假定与原报告未公开的统计脚本完全相同。 | `static-metrics.json`、`scope-metrics.json`、`dependency-imports.json` |
| C13 | 只读 `Get-NetTCPConnection`、`Get-NetFirewallRule`/AddressFilter/PortFilter、`Get-ScheduledTask` | 命令成功；监听 `0.0.0.0:17779`，对应入站规则 Enabled=True、Allow、RemoteAddress=Any；计划任务在运行。云侧公网可达性 **NOT_VERIFIED**。 | `p0-1-local-state.json` |
| C14 | `gh api repos/wmqfl861/AK-trading --jq '{full_name: .full_name, visibility: .visibility, private: .private, default_branch: .default_branch, license: .license}'` | **exit 0**；public、private=false、main、license=null。 | 本轮工具输出；未改变仓库设置 |

C02 的修正筛选命令如下（Git Bash），对已保存的完整输出执行。它的退出码只表示筛选结果，不能覆盖 C02 已记录的 mypy 退出码 **1**；PowerShell 可以使用 `Select-String` 筛选同一文件：

```bash
grep -E '\[(call-arg|name-defined)\]' '/d/Maintenance/aktr-repo-audit-20261025/mypy-src.stdout.txt'
```

一次 Windows 原生 Python→MSYS grep 参数转交曾返回 grep=2 / `Invalid range end`；它是核实工具的参数传递问题，不是产品问题。该失败保留在 `mypy-filter.result.json`；随后经 Bash 正确执行原样与修正 pattern，结果以上表为准，没有掩盖原失败。

### C05 额外发现：全套测试收集阻塞

首个错误来自 `tests/datapipeline/fixtures/DP08/bootstrap.py:274`：当前 `src/ak_trading/datapipeline/collector.py` 的哈希与 DP03 清单不同。3 个收集错误直接报告这一漂移。另有 `tests/datapipeline/fixtures/DP03/bootstrap.py:112–123` 在收集时删除 `sys.modules` 中全部 `ak_trading.*` 并把不完整 staging 根插入 `sys.path[0]`；随后出现来自该 staging 包的 `monitoring` 导入错误和大量正常业务包 `ModuleNotFoundError`。这解释了收集错误的明显级联，**不是已证明存在 174 个互相独立的缺陷**。

建议把历史候选包测试放在独立子进程/隔离导入域，避免污染同一 pytest 收集进程；先核对 DP03 原接受版本和当前修改来源，不能直接改哈希或把失败测试忽略掉。影响范围是测试引导与集成基线；作为 P1-1/P1-3 的新增核实事实登记，本轮未修复，也没有偷偷加排除参数重跑后宣称全绿。

## P0 优先核实

### P0-1 — 部分成立；需要用户亲自操作

**证据。** `docs/mcp-server.md:65–82` 记载明文公网接入方案，`133–135` 记载历史属主账号连接；C13 实际确认全网卡监听和任意来源本机入站放行。`scripts/run_market_mcp.py:65–67` 使用普通 HTTP 的 uvicorn 启动，没有 TLS 参数。`src/ak_trading/mcp_server/server.py:47` 未向 `write_audit` 传 `client`；`audit.py:142,164` 默认写 null。`server.py:196–198` 空白名单放行所有来源。以上当前源码事实成立。

**需要限定的部分。** 云安全组/NAT/外部实测没有核实，不能把“本机监听 + Any 防火墙”直接写成已证明全球公网可达。当前真实令牌长度、白名单值和数据库登录角色未查，因为这需要越过本次凭据边界；属主账号现状仅有历史文档支持。源码默认 host 是回环地址，当前全网卡绑定来自部署选择，不能说代码默认就是公网。

**建议与影响。** 先撤掉公网入口并改用回环+隧道，然后轮换令牌。MCP 远程消费者会短暂中断；本地观察桥 17778、采集写库身份和交易配置应保持原边界。只读账号迁移依赖 P1-6 及服务配置隔离。短令牌拒绝、请求上下文到 audit 的 client 传递属于后续代码修复，需要正常开发和审核，不由本次人工操作表擅自实现。

**用户决策/操作。** 必须由用户执行防火墙、云侧设置、凭据轮换和服务重启；需要用户选择 SSH/VPN 或确有必要的 TLS 公网方案。建议默认 SSH/VPN。数据再分发许可也由用户确认，本次未替用户作法律判断。

#### 供用户执行的操作步骤（本轮未执行）

1. 在管理员 PowerShell 只读确认规则与监听，记录需恢复的非敏感设置；另在云控制台确认 TCP/17779 的安全组、NAT 和端口转发。不要改动 SSH/RDP 管理入口。
2. 撤掉本项目的公网入站放行，同时删除/关闭云侧对应的公网入站和 NAT 转发。当前本机规则的精确 Name 为 `{12405e43-e621-42d0-acaf-4b6c5701f7e2}`。先核对 Name 和显示名一致，再执行下方 Disable；存在其他宽泛 Allow 规则时一并由用户评估，单禁用一条不保证所有路径关闭。
3. 用户停止 MCP 计划任务，并通过已有安装脚本把启动参数显式改为 loopback；这会修改任务定义，因此只由用户执行。暂保留 MCP 17779，NE1 的独立端口方案另在 P1-5 中处理。
4. 用户在本机秘密管理/编辑工具中生成至少 **32 个随机字节**的新令牌，直接更新服务与被授权客户端；不要把值贴到聊天、Git、命令行参数或日志。撤销旧令牌。配置项清单见下表。
5. 用户配置 SSH 隧道或 VPN，重启 MCP 任务；先用不带令牌的 `/healthz` 核实回环服务。外部独立网络应无法连接公网 17779；隧道内无令牌调用应被拒绝，新令牌应可用、旧令牌应不可用。任何鉴权验证由用户完成，不把 token 放进可见命令历史。
6. P1-6 完成开发、验证和独立审核后，再由用户创建/设置专用只读角色，并把 **MCP 进程自己的**数据库连接切换过去。不要直接把所有采集进程共享的 `AK_DATABASE_URL` 改成只读角色，否则可能使采集写库失败。

```powershell
# 以下修改命令仅供用户本人审核后执行；代理本轮未执行。
Get-NetFirewallRule -Name '{12405e43-e621-42d0-acaf-4b6c5701f7e2}' |
    Select-Object Name, DisplayName, Enabled, Direction, Action
Disable-NetFirewallRule -Name '{12405e43-e621-42d0-acaf-4b6c5701f7e2}'
Stop-ScheduledTask -TaskName 'AK-trading Market MCP Server'
powershell -NoProfile -File D:\Projects\AK-trading\scripts\install_market_mcp_task.ps1 -Apply -Port 17779 -Host_ 127.0.0.1
# 完成用户自己的令牌/客户端配置和隧道配置后：
Start-ScheduledTask -TaskName 'AK-trading Market MCP Server'
Get-NetTCPConnection -LocalPort 17779 -State Listen |
    Select-Object LocalAddress, LocalPort, OwningProcess
# 在远端建立 SSH 隧道，前提是用户已确认可用 SSH 服务与认证：
# ssh -N -L 17779:127.0.0.1:17779 <user>@<server>
```

| 要核对/更改的配置 | 目标 | 执行人及约束 |
| --- | --- | --- |
| Windows 入站规则、云安全组、NAT 17779 | 不直接对公网开放 | 用户；不要关闭远程管理通道 |
| MCP 任务启动参数 `--host` / `--port` | `127.0.0.1` / 暂用 17779 | 用户；脚本 CLI 参数优先于环境值 |
| `AK_MCP_HOST`、`AK_MCP_PORT` | 与任务实际参数一致 | 用户在私有配置中核对；本次未读这些值 |
| `AK_MCP_TOKEN` 及客户端认证配置 | 高熵新令牌，旧令牌失效 | 用户；不得提交或打印实际值 |
| `AK_MCP_ALLOWED_IPS` | 按实际隧道/VPN来源限定；loopback隧道可限定 loopback | 用户；不能随意信任客户端自报的代理头 |
| MCP 专用进程的 `AK_DATABASE_URL` | 经验证的 `mcp_readonly` 身份 | 用户；等 P1-6 与专用注入方案就绪，不覆盖采集者的共享配置 |
| SSH/VPN 或 TLS 代理 | 经过认证的加密访问 | 用户选择；公网 TLS 方案仍需来源限制和令牌 |

### P0-2 — 成立；当前未修复

**证据。** `src/ak_trading/v2/artifacts.py:1517–1523` 要求 `exit_registry_sha256` 与 `exit_binding`；`decision/replay.py:293`、`validation/shadow.py:932` 均没有提供；随后各自捕获 `(ValueError, TypeError)`。C02b 的四条缺参诊断和 C06 动态复现相互印证。C06 在所需 evidence 键均存在的 SYNTHETIC 情形下，原 try/except 仍把编程错误映射为 `QUALIFICATION_EVIDENCE_INVALID`。

**影响边界。** 这是非空资格路径的确定性接线缺陷，不是“策略真实未过统计门槛”的证据。目前 `experiments.py:1307` 仍传空资格；普通 shadow 还会先受到 P1-2 的其它断点影响。因此不能据此声称真实模拟账户已经发生全部空仓，也不能把最小探针当作完整有效资格/开仓测试。

**建议与影响。** replay 已有 `exit_registry` 和 `bindings_by_strategy`（252–255），应按唯一策略绑定传入这两个参数；shadow 需要沿原契约完整传递退出注册表与绑定，不能临时猜一个 hash 或默认 binding。增加真正有效且非空的资格正例，并保留缺证据、错误 hash、错误 binding、失效资格等反例。异常边界应区分“程序调用错误”和“无效证据”；validator 本身在 `1547–1558` 也合法抛 `TypeError`，所以不要机械删掉捕获后遗漏输入校验语义。具体异常类型方案交计划作者决定。影响 replay、ordinary shadow 及对应 M4/M5 契约测试；不改真实交易安全门。

**仓库外与冻结核对。** 原 forward Sol owner 定点核对：当前 M5 C1 实际源码 `D:/AKF261018/M5-CDF-BATCH-SOURCE-025-R16-R4/source` 仍有同样缺口。旧 `D:/AKF261001/SHCP7/source` 只在 replay 中补过两个参数，其 ordinary shadow 仍不完整，不能整体照搬。C1 尚无产品 PASS。canonical 377 文件本次逐成员核验无漂移，但写冻结仍有效；最新 forward 审核调用退出 1/1、无正式产品结论，不会自动解除冻结。

**用户决策。** 修复明确的接线错误本身不需要新的业务决策，用户已要求按流程处理；若要改资格门槛、契约、交易权限或治理规则则另行决定。本轮按“先核实、不直接改代码”完成核实和衔接准备，未启动产品修改或冒称硬审核。

#### P0-2 的现行流程衔接

这不是 Root 另写的一份详细开发计划；应复用已经存在的计划与责任边界：

| 阶段 | 现有依据/负责人 | 当前状态与下一动作 |
| --- | --- | --- |
| 计划与契约澄清 | `docs/strategy-first-blueprint-20261015-r1/implementation-clarifications/ordinary-shadow-20261015-r1/CLARIFICATION.md` 的 B0–B8；同目录 `SOURCE-STATUS.md:33–42` 已登记本次缺口。RESEARCH/r2 原 planner 为 `/root/plan_research_astra`。 | 原作者目前不在可续接的 live 列表；Root 应先核实可恢复会话，确实不可用才显式转交 Astra/max。不能把 backtest 作者的窄执行澄清移交扩大为整个 M4/SH 契约写权。 |
| 计划意见 | 独立 ZCode，按现行规则仅给意见；Astra 自主处置 | 复用有效结论，只审真实新增范围/契约差异；不以计划 PASS 为开发门槛。 |
| 冻结与接口前置 | 377 canonical 冻结；ordinary SH 的 R19 filewise release → R20 原作者 schema 裁定 → R21 SH-M1 冻结 | 不修改现有冻结文件。由原计划方确认能否把 replay 窄修复独立切出，使用隔离候选，或由 Root 明确重切审核范围；不能以原 CLI 失败自动解冻。 |
| 实施归属 | M4 Sol owner 管 replay；R-SHADOW Sol owner 管 shadow；M5 当前 owner `/root/sol_m5_existing_recovery_20261023` 管 experiments 调用端 | M4/R-SHADOW 的当前实际 actor 尚需绑定/显式移交，不编造连续性。开发模型必须 `gpt-6.1-sol/max`，新建时 `fork_turns=none`。原 forward owner 只拥有 feed.py/test_feed.py，已续接完成本轮只读核实，不能越权修改 replay/shadow。 |
| 成果审核 | 独立 ZCode `GLM-5.3/max` | 开发、必要验证和新版本冻结完成后立即审核实际写集。复现非空资格正例与反例、独立重跑计划命令；失败交原 Sol owner 修复后复审。本次外部报告、C06 或 developer 自测均不提供产品 PASS。 |

## P1 正确性与工程基础

### P1-1 — 成立

**证据：** 当前没有 `.github/workflows`、pre-commit、ruff/mypy 项目配置或锁文件；`pyproject.toml:26–34` 只有包发现和 pytest 设置。`docs/delivery-20261024/GITHUB-SNAPSHOT-20261024.md` 明示当次未重跑全套测试。C01/C11 复现报告的大部分静态计数；C02 为 1,983 而非 1,985，类型诊断总数受环境/stub影响，不把两条差异包装成修复。C05 进一步暴露测试收集阻塞。

**建议/范围：** 先修复收集污染与必要依赖，再增加 Windows/Linux CI，保留现有失败基线并使新增错误可见；优先处理实际 `call-arg`、`name-defined`。CI不能单纯用忽略列表掩盖本次失败。涉及 CI、依赖和测试引导。

**用户决定：** 常规检查建设无需用户逐条决定；分支保护、强制 PR、必需检查策略涉及仓库治理，须由用户确认，本轮未设置。

### P1-2 — 成立

**证据：** `validation/experiments.py:603` 的 `_reg` 未赋值，604 还缺 checkpoint 的退出注册表；`shadow.py:975/999/1059` 缺账本必填参数。C02b 确认这些类型诊断，C06 用真实 ledger 签名对三处调用 bind 均失败。`tests/test_v2_m5_payoff_integration.py:538–558` 明确用 `pytest.raises(NameError, match="_reg")` 固定已知未完成边界；`scripts/research_probability_v2.py` 仍列 `shadow-replay` 模式。普通 shadow 不可用成立，不能把相关已接受的恢复语义子模块一并宣布无效。

**建议/范围：** 与 P0-2 共用原 B0–B8 接线计划，完整传递同一个 frozen exit pack；若完整接线尚受真实依赖阻塞，先由计划作者决定是否增加清晰的“未实现”入口拒绝，避免先训练后撞 NameError。影响 ordinary shadow、experiments、CLI 与对应测试，不能放宽公共 ledger 参数或统计验收。

**用户决定：** 不需要用户选择修哪一个缺参；若改变对外可用模式/批次范围，需要用户确认产品取舍。现有“先核实”阶段未动入口。

### P1-3 — 成立

**证据：** `tests/fixtures/v2/ep1_t7/acceptance-input-manifest.json` 有 64 个 code pins，C07 实跑失败且不跳过；`tests/test_v2_m5_t7_evidence.py:250–257` 报 11 个 `code_drift`。漂移路径为 decision 下 gates/payoffs/pipeline，v2/export、factor_specs，strategies 下 breakout/pullback/registry/trend，validation/experiments，以及 `tests/test_v2_m5_payoff_integration.py`。当前文件按 LF 读写不会自动消除内容变化。

**建议/范围：** 查明每个已集成变更与原接受范围，只有在原计划要求的验证/审核满足后发布新基线，保留旧基线；不能“把清单全部刷新为当前 hash”充当修复。与 C05 的 DP03 清单漂移是不同清单问题。影响验收夹具、集成版本与测试入口。

**用户决定：** 正常版本核对不用用户决定；报告建议取消逐文件哈希、完全以提交号替代，是 G-2 治理变更，须等用户决定。

### P1-4 — 成立（SDK 升级边界需澄清）

**证据：** `pyproject.toml:10–23` 声明 Python >=3.10，仅基础 tqsdk/dotenv 和 test/postgres extras。C03 实际发现 44 条 3.11 不兼容语法，包括 `v2/contracts.py:22` 的 type alias、`v2/schemas.py:501` 的类型参数。C12 检出报告列出的十种直接依赖均有实际 import，例如 `mcp_server/server.py:61,151`、`scripts/run_market_mcp.py:63`、`exchange_rule_sources.py:1213`。

项目声明 TqSdk 3.10.1；默认解释器实装 3.10.2；已独立审核的工具环境 `D:/Tools/tqsdk-3.10.3/` 为 3.10.3。工具 skill 的安装参考明确说明“工具更新，不迁移项目 pin”，所以不能把版本不一致误称为此前工具升级未执行。

**建议/范围：** 将项目 Python 最低版本与实际语法对齐，按 mcp/research/scraping 等实际入口声明直接依赖与 extras，构建干净环境验证；项目 TqSdk 迁移单独验证后再统一 pin/锁文件。无需在线登录或更改凭据。

**用户决定：** 是否继续支持 3.10/3.11、SDK迁移目标、uv/pip-tools选择涉及支持范围；建议采用 >=3.12 并迁移到已验证的目标版本，但本轮不改。

### P1-5 — 部分成立

**证据：** `scripts/run_market_mcp.py:66`、`infiny_execution/client_service.py:156`、`scripts/run_infiny_execution.py:62` 默认都为 17779；`integrations/infiny/AKExecutionBridge.py:92` 指向该端口的执行 API。执行 HTTP server 在 `client_service.py:2531` 开启 `allow_reuse_address=True`。

**纠正：** MCP 不是同一个 ThreadingHTTPServer 实现。当前 uvicorn 0.52.0 的普通单进程启动走 `uvicorn/server.py:173` 的 `loop.create_server`；本机 Python `asyncio/base_events.py:1524–1525` 在 Windows 默认不启用 reuse_address。不能把 uvicorn 另一个 bind_socket 分支等同于本项目默认路径。C09 的最小本机探针第二次 bind 被 WinError 10013 拒绝，未复现报告推测的双监听/请求串流。

**建议/范围：** 默认端口冲突仍应修复，统一登记 MCP、observer、NE1 端口并在启动时确认服务身份；是否采用 Windows 独占 socket 需在真实服务启动路径验证。涉及服务端、执行桥和安装/配对配置，不能直接重启当前服务试错。

**用户决定：** 最终端口与部署窗口需用户确认；建议给 NE1 独立端口，不占用 MCP 或 observer 端口。本轮未操作真实执行服务。

### P1-6 — 成立

**证据：** `scripts/sql/create_mcp_readonly_role.sql:16–24` 将 psql `:'role_password'` 放入美元引号 DO 字符串。psql 不在该字符串内部替换变量；C08 对原 PL/pgSQL 块实际解析报冒号处语法错误，SYNTHETIC 字面量对照可解析。第 8 行示例还有不必要的嵌套单引号。本次没有用生产数据库运行脚本。

**建议/范围：** 改用块外绑定值与块内受控动态 SQL，或用正确引用的 `\gexec`；同时防止口令进入命令行/回显/日志。专用 role 的现有、重复执行、最小授权和失败回滚都应在隔离数据库测试。只改 SQL/helper 与说明，不自行更新生产账号。

**用户决定：** 修复脚本不需要新的业务决定；生产角色创建、密码设置与切换必须由用户执行，且与 P0-1 的进程独立配置配套。

### P1-7 — 部分成立

**证据：** `integrations/infiny/AIBridge.py:178`、`AKExecutionBridge.py:102` 确实硬编码 `rb2610`，后者 `93–104` 写明部署默认并包含一个实际模拟账号标识。本文件不再次抄录该账号。按报告给出的合约月 15 日规则，该默认合约在本次核实日期应已过最后交易日。

**限制：** 本轮未登录行情系统、未读取客户端私有配置，也未取最新官方合约元数据；不能据仓库默认值断言当前客户端实际仍订阅该合约。代码中的固定默认和公开标识问题成立，真实部署状态待用户核对。

**建议/范围：** 部署时由明确的配置输入提供经过到期校验的具体合约和本地账户标识；只读行情可用主连发现实际合约，不能把不可交易的主连符号直接塞给执行桥。增加到期前提示。影响两桥、部署/配置与离线校验；不启用交易、不替用户重载客户端策略。

**用户决定：** 用户确认实际订阅合约、账户标识和客户端部署；实际主力合约需用当日权威元数据核对。

### P1-8 — 部分成立

**证据：** `scripts/probe_rank_slider.py:126–184,224` 有自动拖动滑块的实现；`member_rank.py:699,849,973` 和 `exchange_rule_sources.py:282` 使用 StealthySession。`exchange_rule_sources.py:269–280` 确实把另一解释器的 site-packages 放到 sys.path 最前面。C12 对这些代码进行了静态核对，未执行抓取或绕过。

**限制：** 是否违反具体站点条款、数据许可或法律，不能由上述代码存在性直接判定；也不能把所有 StealthySession 调用都等同于已成功绕过。本轮没有作法律结论。

**建议/范围：** 数据源选择优先按已获许可的来源；移除跨解释器 sys.path 注入，复用隔离子进程入口并明确缺依赖错误。涉及抓取适配器和运行环境，不能静默更换数据来源或把当前数据标为有授权。

**用户决定：** 数据来源/许可、抓取功能是否保留及是否公开由用户决定；通常的解释器隔离修复无需新的业务选择。

### P1-9 — 成立

**证据：** 无根 `.gitattributes`；C04 精确命令得到 **120 个 i/crlf**，另有 **7 个 i/mixed**。原报告的 127 可解释为“含 CRLF”的合计，但不能用其附录 `grep -c i/crlf` 直接得到 127。换行混用与按原始字节计算 hash 的冲突确实存在。

**建议/范围：** 先确定文本换行政策，再在单独、可审阅的规范化变更中处理，并同步确认仍有效的冻结输入；不要现在对审核中的 377 文件执行全库 renormalize。`.ps1` 采用哪种换行也应统一到工具约束，不以习惯改动所有文件。

**用户决定：** 一次性全库规范化及它对冻结规则的迁移属于广泛变更，需要用户确认时机。本轮未创建 `.gitattributes`。

### P1-10 — 成立

**证据：** C14 当前 GitHub 仓库为 public、license=null；跟踪文件无 LICENSE。`docs/mcp-server.md:50–82`、桥接默认配置、源码与文档确实公开了部署信息。内网地址/账号标识并不等同于密码，但需要决定其公开范围。

**建议/范围：** 若无明确开源目标，建议先由用户改为私有，再决定许可、可公开代码/文档范围和历史清理。仅在最新提交删除敏感配置不能从历史中撤回已公开内容；本轮没有重写历史或改变仓库可见性。

**用户决定：** **必须决定 public/private、许可证和历史清理策略**；这是仓库所有者的选择，不由代理自动执行。

## G 流程与治理：仅整理，不修改规则

### G-1 — 部分成立

**证据：** `PROJECT-STATUS.md:95–112` 等段落大量记录会话/路由/进程和本地证据路径；`PROJECT-MODEL-ROLES.md:3–23` 保留模型与职责调整记录；README 的“当前路线”指向 10 月 10 日，而 `docs/strategy-first-blueprint-20261015-r1/CURRENT.md:3–11` 指向 r2-001。C12 在加入原报告后统计 103 个 Markdown、17,568 行。

**限制/纠正：** “流程文档是产品文档的 3 倍”依赖未提供的分类口径；文档行数与工时、策略产出没有直接等价关系。路由变化有用户明确指令来源，不能一概归因于代理自主折腾；可以核实变化事实，不能据此取消用户规则。

**建议/范围：** 保留安全和独立审核要求，另提议将产品状态与运行日志分开、修正唯一入口、归档过期说明，并用真实数据/策略/缺陷指标说明进展。

**用户决定：** **需要用户决定治理规则、角色稳定期和文档政策**。未修改 AGENTS、模型规则或 workflow。

### G-2 — 部分成立

**证据：** 发布说明和原 owner 核对均确认 M5/DISPATCH/N8 存在仓库外待审修复；Git 当前只有两个提交，第二个只增加报告。C12 对跟踪文档（排除新增原报告）中的显式 Markdown 链接统计为 **352 处：199 指向 Git 内文件、66 不在跟踪文件集合、87 为 Windows 绝对路径**。此口径不包括代码跨度内的裸路径，不能替代原报告未公开算法的 851/305 或 36%。Git 中缺失也不等于本机不存在。

**建议/范围：** 提议把可交付源码与最小必要证据放入可克隆的版本化位置，未合并外部成果显式登记；分支/PR 可改善审阅。Git 提交能绑定受跟踪内容，但不能自动覆盖未跟踪输入、数据库快照、外部运行状态或一次性操作授权；不能直接把现行 hash/冻结全部删除。

**用户决定：** **Git/PR/CI 是否作为唯一事实来源、何时迁移外部成果、哪些冻结要求保留，由用户决定**。本轮只提议。

### G-3 — 部分成立

**证据：** `docs/project-reassessment-20261015-astra-r1/STRATEGY-REVIEW.md:10–20,46–53` 明确正式策略有效性 NOT_ESTABLISHED，但也列出真实价格上的 RB SCENARIO 比较、资金压力和归因结果。当前交付仍没有把 M/DCE 数据输入记为正式研究完成；M5 C1 也未形成接受的研究结论。

**纠正：** “尚无经规定门槛确认有效、可据此模拟/交易的策略”有依据；“完全没有真实历史研究或任何策略结论”不准确。本次没有重跑历史研究，也不声称已经穷尽本机一切未登记实验。

**建议/范围：** 先复用已有 RB 情景结果形成下一项可否证问题，按有效计划跑严格标注成本假设的样本外比较。假设成本实验应保留 SCENARIO 标签，不能替代 PIT、独立样本或既定资格门。

**用户决定：** **研究优先级、允许的成本假设、是否改变现行验收门槛由用户决定**；不能由外部报告自动降低标准。

## P2 架构、性能与存储

### P2-1 — 成立

**证据：** `src/ak_trading/infiny_bridge/decision.py:11,59` 使用 v1 `futures.strategies.default_registry`；v1/v2 目录均在当前源码中。`v2/factor_specs.py:505–525` 明写对 v1 的 14 项定义只读转写，并保留 legacy-v1 对比标签，v2 新增 opening_gap 不算同一组。C12 AST/源码核对支持并存与复制事实。

**建议/范围：** 先标出必须保持不变的 v1 对照语义，再建立可共享定义或生成/一致性检查，逐步迁移消费者；不能为了消除重复而改变已冻结的研究对照。影响观察桥、因子定义与两套研究入口。

**用户决定：** v1 停止支持时间、观察桥改接 v2 或只采集，需要用户确认产品取舍。

### P2-2 — 部分成立

**证据：** `datapipeline/scheduler.py:1–9` 明写继承 acquisition 语义后重新实现；报告列出的五个日历相关模块均存在。`persistence/futures_research.py:23–38` 依赖 futures 类型/IO，`persistence/market_data.py:14` 依赖 market_reference。C12 对 src 的 AST 统计 **384 处函数内 import，67 个文件**；experiments 为 **80**，与报告 372/45/86 的口径不一致。

**限制：** 多个日历模块可能分别处理来源采集、交易所事实和研究时钟，不能仅凭文件数认定可以合并；函数内导入也可能用于可选依赖和测试隔离。现有证据不能证明根因就是文件归属规则，存储适配器依赖领域类型也不必然是分层错误。

**建议/范围：** 先列出重复数据契约与实际依赖环，再确定共享层边界和迁移顺序；只在保持时区、可见时间和 PIT 语义后合并。属于跨采集/日历/存储的架构变更。

**用户决定：** 大规模合并的范围与优先级需用户决定；本轮不创建 core 包或改规则。

### P2-3 — 成立（统计口径限 src）

**证据：** C12 在 src 中复核 **47 个函数超过 150 行、9 个超过 300 行**；扩到 scripts/tools/integrations 则是 67/11，不能混用。`verification.py:2089–2695` 为 607 行，C11 复杂度 **138**。`v2/validation/reports.py` 265 行中 33 行超过 120 字符，最长 **511**，约 12.5%。工单/裁定标记很多；按本轮较窄的 `§|RULING数字|arbitration` 模式找到 544 行，不声称复现未知口径的“600 多处”。

**建议/范围：** 针对高修改率和实际失败点做小步拆分、保持接口和行为测试，先建立回归基线。不能在写冻结期间执行全库 ruff format；ADR 可作为提议，不代替既有契约。

**用户决定：** 大规模格式化/架构调整和工期安排需要用户确定；小范围修复按原计划执行。

### P2-4 — 部分成立

**证据：** `v2/probability/models.py:527–565` 在特征/阈值循环中重复分组并计算 SSE；`futures/scenario_backtest.py:36` 使用 `Context(prec=128)`。C12 确认 src 中直接 import numpy/pandas 的文件仅 2 个。既有文档记录超时，当前 M5 外部性能修复仍待接受。

**纠正/限制：** `prec=128` 指 **128 位十进制有效数字**，不是 128 bit。没有本次 profile，不能把所有超时归因于树实现或 Decimal。更换 scikit-learn/LightGBM 会改变模型与训练/预测契约；固定版本和随机种子也不自动保证跨平台、并行实现逐字节一致。

**建议/范围：** 先以现有冻结负载测量热点，优先等价预排序/前缀统计等窄优化；模型替换另做行为、统计和稳定性评估，账本精度保持契约要求。影响模型、实验基线和数值结果。

**用户决定：** 是否更换模型家族、数值精度或验收口径需用户决定；已有等价性能修复继续遵守原计划。

### P2-5 — 部分成立

**证据：** `migrations/versions/20260730_0001_postgres_storage.py:182–223` 定义普通 market_bars 表、NUMERIC(28,10)、双时间字段及索引；`persistence/market_data.py:358–377` 的行级 ON CONFLICT UPDATE 无值差异条件；`mcp_server/queries.py:177–207` 每次使用 psycopg.connect，没有默认连接池。

**明确反证：** `market_data.py:382–436` 先按 `(kind, source_sha256)` 为批次去重；同一已完成 CSV 且元数据一致会直接返回 `inserted=False`，不会重写该文件全部行情。所以“重复导入必然重写整行”不成立。不同源文件哈希、重叠行情走行级 upsert 时，冗余更新/覆盖来源的隐患仍在。

**限制：** 未连接生产库，当前实际分区/索引、WAL/膨胀和各表体积均未实测。180 GB 是全库历史记录，不能除以行情行数得到可靠的每行成本；“缩到三分之一/一个数量级”的收益尚无证据。

**建议/范围：** 先验证不同批次覆盖与来源优先级，加入值差异更新和可追溯修订，再测池化、分区/列存收益。价格类型不可仅为压缩擅自改成 float；影响存储schema、导入语义、PIT和运维迁移。

**用户决定：** 存储架构、价格精度、修订保留政策和生产迁移窗口需要用户决定。常规重复写避免可先提窄修复，但本轮未改数据库。

### P2-6 — 部分成立

**证据：** C12 检出 src 中 17 个相关 `Path(__file__).parents[...]` 文件，若不计包内 test_checker 则为 16；`scripts/minute_workers_watchdog.py:24` 写死本机 Python 路径。最大夹具 `tests/fixtures/v2/ep1_t7/inputs/bars-minute.json` 为 **19,177,621 bytes**。`src/ak_trading/readiness/tests/` 存在，`pyproject.toml:26–27` 没有明确排除包内测试。

**纠正：** 确有 **21 处 pytest.skip**，但并非全部因本机冻结包：还包括 PostgreSQL配置/二进制、PowerShell、symlink/hardlink能力等条件；例如 `tests/test_verification.py:705,718` 是硬链接能力。不能写成“干净克隆时这21处都会因为缺证据跳过”。C05 实际还暴露了收集失败，而不只是 skip。

**建议/范围：** 将运行资产位置变为可配置/可打包资源；分类条件性集成测试，区分未安装数据、平台能力和必需失败。大 SYNTHETIC 夹具可评估压缩或可重复生成，不把新生成结果冒充原已审核字节。影响打包、测试引导、脚本与资产交付。

**用户决定：** 数据资产如何分发、LFS/生成方式、支持平台和脚本归档范围需要用户确认。

### P2-7 — 成立

**证据：** `src/ak_trading/futures/data.py:49` 导出 `instance_observation_start`，模块实际没有这个属性。C01 报 F822；C10 星号导入真实失败：`AttributeError: module 'ak_trading.futures.data' has no attribute 'instance_observation_start'`。

**建议/范围：** 先核对原公开接口是否要求该函数；若是过期导出则删除该项，若契约仍要求则补实现及行为验证。影响该模块公共导出和依赖它的消费者，不应为“填名字”随便加空实现。

**用户决定：** 通常不需要用户业务决策，由原计划/接口负责人选择正确兼容处理；本轮只核实。

## 需要用户决定或亲自操作的事项

| 事项 | 对应编号 | 建议 |
| --- | --- | --- |
| 防火墙、云侧端口、令牌轮换、MCP重启、只读身份切换 | P0-1、P1-6 | 用户本人执行上面的操作步骤；先关闭公网入口，不等数据库迁移 |
| 仓库私有化、许可证、公开范围与历史处理 | P1-10、P1-8 | 无明确开源目标时建议先设 private；本轮未操作 |
| 数据许可/抓取功能、实际合约与客户端配置 | P1-7、P1-8 | 用户确认，代理不读凭据或替用户重载策略 |
| CI保护策略、冻结规则、角色和文档治理 | P1-1、P1-3、P1-9、G-1、G-2 | 只提交提议，保留现行规则，等用户决定 |
| 支持Python/SDK版本、端口部署窗口 | P1-4、P1-5 | 建议 >=3.12，按用途明确依赖，服务端口分开 |
| 研究验收取舍及大规模架构/数值/数据迁移 | G-3、P2-1…P2-6 | 先解决确定性阻塞，再用真实策略实验驱动选择；不把外部建议自动变成新开发批次 |

## 建议的下一步顺序

1. **用户先执行 P0-1 的网络收紧与令牌轮换。** 并决定仓库可见性，避免把此事拖到代码整改结束。
2. **Root 续接 P0-2/P1-2 的原计划与责任边界。** 必要时显式转交 Astra；确认 replay 的窄修复与 ordinary shadow 依赖如何拆分，使用隔离候选保护冻结版本。有效计划和前置条件就绪即交 Sol，实现完成立即独立 ZCode 硬审，不额外等待计划 PASS。
3. **并行修复测试收集污染、接受版本/清单漂移及 P2-7 的明确导出错误。** 保留 C05/C07 原始失败，不以删除测试或盲目刷新 hash 得到假通过。随后再完整运行离线测试。
4. **完成 P1-6、P1-4、P1-5 的必要工程修复与审核，再由用户做部署变更。** P1-7 的实际合约和本地标识由用户确认；CI随后以可执行的测试入口建立真实基线。
5. **G项与大架构建议等待用户取舍。** 优先复用现有 RB 研究路线产出可判定结果，避免此次核实自动扩成新的平台重写项目。

本次未给产品硬审核签发任何 PASS/FAIL；原有未完成模块、旧失败、已耗用一次性操作和冻结版本均保留。上面的修复建议和衔接表不替代 Astra 的有效实施计划或独立 ZCode 成果审核。
