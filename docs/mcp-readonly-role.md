# mcp_readonly 专用只读角色（P1-6 修复）

日期：2026-10-25。本文件说明 `scripts/sql/create_mcp_readonly_role.sql` 的正确用法与安全边界，
并取代旧 SQL 头部带缺陷的示例命令（嵌套单引号、口令进 argv）。
背景与核验结论见 `docs/repo-audit-20261025-claude/VERIFICATION.md` P1-6：旧脚本把 psql 变量
`:'role_password'` 放进美元引号 DO 体内部，psql 从不在那里插值，导致语法错误且从未可执行。

## 修复后的机制

- 口令只通过 psql 进程的环境变量 `AK_MCP_ROLE_PASSWORD` 传入（psql ≥ 15 的 `\getenv`），
  绝不出现在命令行参数、进程列表、shell 历史或本脚本的任何输出中。
- `:'role_password'` 全文只在美元引号块之外出现一次：`SELECT set_config('ak.role_password',
  :'role_password', false)`，由 psql 以正确的 SQL 字面量转义；结果用 `\gset` 捕获，不回显。
- DO 块内用 `current_setting()` 读取并以 `format('%L')` 受控拼入 `CREATE/ALTER ROLE ... PASSWORD`，
  对任意口令字符（引号、分号、注释符、`$$`）均安全。
- 空口令或未设置环境变量时 DO 块显式 `RAISE EXCEPTION`，配合 `\set ON_ERROR_STOP on`，
  psql 以非零退出，整个脚本处于单事务中并全部回滚；绑定后立即 `\unset` 并把会话 GUC 清空。
- 幂等：角色不存在则创建（`LOGIN` + 最小默认权限），已存在则仅改口令；授权语句可重复执行。
- 授权范围不变：`CONNECT`、`USAGE ON SCHEMA public`、四张表白名单
  `market_bars`、`member_position_ranks`、`reference_closed_dates`、`reference_products`；
  `reference_snapshots.raw_json`（完整交易所快照）明确不授权；默认权限显式 REVOKE，
  授予之后新建的表对角色不可见。

## 推荐执行方式（隐藏输入）

```powershell
python scripts\setup_mcp_readonly.py --database "postgresql://postgres@127.0.0.1:5432/ak_trading"
```

helper 用 `getpass` 隐藏输入并二次确认，口令只写入子进程 psql 的环境变量（argv 不含口令），
退出码透传 psql。也可以不用 helper 手动执行：

```powershell
$env:AK_MCP_ROLE_PASSWORD = Read-Host 'mcp_readonly 口令'   # 注意：Read-Host 明文回显，介意回显请用 helper
psql -X "postgresql://postgres@127.0.0.1:5432/ak_trading" -f scripts/sql/create_mcp_readonly_role.sql
Remove-Item Env:AK_MCP_ROLE_PASSWORD
```

前置条件：psql ≥ 15；超级用户或 CREATEROLE 连接；脚本内数据库名固定为 `ak_trading`
（隔离验证时集群中需创建同名库）。注意：PostgreSQL 服务器若配置 `log_statement='all'` 等全量
语句日志，包含口令字面量的语句可能进服务器日志——请在可信管理连接上执行并确认日志策略；
这与任何 `CREATE ROLE ... PASSWORD` 写法相同，非本脚本特有。

## 切换 MCP 专用进程连接（仅用户执行）

- 只允许切换 **MCP 服务进程自身** 的数据库连接：在该进程的启动环境（计划任务/服务级环境变量）
  中把 `AK_DATABASE_URL` 设为 `postgresql://mcp_readonly:<口令>@127.0.0.1:5432/ak_trading`。
  `run_market_mcp.py` 的 `load_dotenv` 默认不覆盖已存在的进程环境变量，进程级设置优先于 `.env`。
- **不要**把采集等进程共享的 `.env` 中 `AK_DATABASE_URL` 改成只读角色，否则采集写库会失败；
  采集侧继续使用属主连接。禁止让任何 agent 读取或编辑 `.env`、打印凭据或代用户执行生产切换；
  生产角色创建、口令设置与服务重启均由用户本人操作（见 VERIFICATION P0-1/P1-6 待决表）。
- 连接级只读（服务端 default_transaction_read_only）保持不变，角色级最小权限是第二道防线。

## 验证设计状态与边界

- `tests/test_mcp_readonly_role.py` 含三层：SQL 结构静态断言（离线）、helper 桩测试
  （离线，不启动进程）、一次性隔离集群端到端断言（新建/已存在重复执行、敌意引号口令、
  真实最小权限读/写、强制错误整体回滚）。e2e 由
  `AK_MCP_READONLY_E2E_DSN` / `AK_MCP_READONLY_E2E_READ_DSN` 两个环境变量门控，
  指向任务本地一次性集群（仅 SYNTHETIC 口令），未设置时自动跳过。
- 本文不声明任何验证结果；各层实际执行与否、原始退出码与输出以当次交付证据目录
  （STEP-DELIVERY / DELIVERY、FREEZE）为准，生产数据库执行与切换保持 NOT_RUN，
  等用户按上节亲自操作后另行记录。
