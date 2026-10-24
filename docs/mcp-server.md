# 行情数据 MCP 服务(远程 agent 只读接入)

把 ak_trading 行情库(PostgreSQL,~180GB)以 MCP(Model Context Protocol)
Streamable HTTP 形式暴露给本地和远程 agent,只读、带护栏、带审计。

- 端点:`http://127.0.0.1:17779/mcp`(Bearer Token 鉴权,token 在 `.env` 的 `AK_MCP_TOKEN`)
- 健康检查:`http://127.0.0.1:17779/healthz`(免鉴权,无数据)
- 常驻方式:Windows 计划任务 `AK-trading Market MCP Server`(开机/登录自启,失败 1 分钟后自动重启,最多 3 次)
- 代码:`src/ak_trading/mcp_server/`;入口 `scripts/run_market_mcp.py`;审计 `data/mcp_audit/audit-YYYY-MM.jsonl`

## 工具一览

| 工具 | 说明 | 护栏 |
|---|---|---|
| `get_coverage(symbols)` | 每符号日线/分钟线最新到哪天 | ≤50 符号;每符号两次索引探测 |
| `get_daily_bars(symbol, start, end, limit)` | 日线 OHLCV+OI | 窗口 ≤800 天,≤4000 行 |
| `get_minute_bars(symbol, start, end, limit)` | 分钟线 | 窗口 ≤31 天,≤6000 行 |
| `search_symbols(pattern, limit)` | 前缀搜符号(仅日线维度) | 纯前缀、禁通配符、长度 6..60,≤100 行 |
| `get_member_ranks(trading_day, ...)` | 会员持仓排名 | 交易所白名单,≤2000 行 |
| `get_settlement_reference(day, symbol)` | 官方/参考保证金率与手续费 | 文件快照,不查库 |
| `get_trading_calendar(start, end)` | 交易日历 | ≤800 天 |
| `list_products()` | 81 个期货品种目录 | 无参 |

注意:主连 `KQ.m@EXCHANGE.product` 只有分钟线(无日线),期权只有日线;
`search_symbols` 只在日线维度发现符号,主连符号请直接按规则构造。

## 安全模型(三层)

1. **传输/鉴权**:绑定回环 127.0.0.1;`Authorization: Bearer <AK_MCP_TOKEN>`
   常数时间比较;跨公网必须走 SSH 隧道/VPN(见下),不开公网监听。
2. **服务端只读**:每个连接 `default_transaction_read_only=on`
   ——注入的写 SQL 在 PostgreSQL 内部被拒绝;`statement_timeout`
   (默认 15s,可经 `AK_MCP_STATEMENT_TIMEOUT_MS` 调,硬上限 60s);
   `idle_in_transaction_session_timeout=30s`。
3. **查询形状白名单**:所有 SQL 为参数化模板;输入经正则校验
   (符号/日期/交易所/品种);拒绝一切已知的"拖库查询"——
   `trading_day × duration` 组合聚合(BitmapAnd 全表扫)、
   DISTINCT 符号全扫、通配符 LIKE。超窗请求返回明确的
   `{"error": ...}`,不抛异常。

审计:每次调用(含被拒)写一行 JSONL——时间、工具、参数、行数、错误摘要;
不含凭据、不含数据行。

## 远程 agent 接入

给远程 agent 的领域说明(符号规则/时间语义/数据边界/常见错误)见 [mcp-agent-guide.md](mcp-agent-guide.md),建议随接入配置一并提供给 agent 上下文。

### 同私网(10.0.75.0/24,VPC 内的其他机器)— 已开通

服务绑定全网卡(`0.0.0.0:17779`),Windows 防火墙规则
`AK-trading Market MCP (VPC)` 仅放行来源 `10.0.75.0/24` 的 TCP/17779。
本机地址:**`http://10.0.75.2:17779/mcp`**(实测 8 工具、取数、401 拒绝均正常)。

```json
{
  "mcpServers": {
    "ak-market-data": {
      "url": "http://10.0.75.2:17779/mcp",
      "headers": {"Authorization": "Bearer <AK_MCP_TOKEN 的值>"}
    }
  }
}
```

### 跨公网 — 已开放(2026-09-12)

面向公网远程服务器接入的当前配置:

- 服务绑定 `0.0.0.0:17779`;Windows 防火墙规则已放行任意来源 TCP/17779。
- **云侧安全组/NAT 端口映射不在本机控制范围内**:需要在云控制台把公网
  端口(TCP/17779)映射/放行到本机。完成后远程服务器用
  `http://<公网IP>:17779/mcp` 接入,验证命令
  `curl http://<公网IP>:17779/healthz`。
- 应用层防线(公网场景实际依赖的):
  1. Bearer Token(常数时间比较);
  2. 每 IP 限流(默认 120 次/分钟,`AK_MCP_RATE_PER_MINUTE`,硬区间 10..10000;
     对未授权洪泛同样生效,先限流后鉴权);
  3. 可选 IP/CIDR 白名单:`.env` 的 `AK_MCP_ALLOWED_IPS`(逗号分隔,
     留空=允许所有来源,仅靠令牌;**建议填上你远程服务器的固定 IP**);
  4. 服务端只读 + 查询形状白名单 + 语句超时(见"安全模型")。
- 已知残余风险:传输为明文 HTTP,令牌和数据在公网可被链路嗅探。
  数据本身是只读行情,泄露面有限;若要消除,后续可加 TLS 或改走 SSH 隧道。

防火墙收紧/放开示例:

```powershell
Set-NetFirewallRule -DisplayName 'AK-trading Market MCP (VPC)' -RemoteAddress 10.0.75.0/24,<你的服务器IP>/32
Set-NetFirewallRule -DisplayName 'AK-trading Market MCP (VPC)' -RemoteAddress Any
```

Python 客户端示例:

```python
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

transport = StreamableHttpTransport(
    url="http://127.0.0.1:17779/mcp",
    headers={"Authorization": "Bearer <token>"},
)
async with Client(transport) as client:
    result = await client.call_tool("get_coverage", {"symbols": ["SHFE.rb2610"]})
```

### 跨公网(推荐 SSH 隧道)

在远程机器上:

```
ssh -N -L 17779:127.0.0.1:17779 user@<这台机器>
```

然后远程 agent 按 `http://127.0.0.1:17779/mcp` 接入,数据库零改动。
(本机 OpenSSH Server 当前未安装,装好即可用,见上节。)

## 运维

- 启停:`Start-ScheduledTask/'Stop-ScheduledTask -TaskName 'AK-trading Market MCP Server'`
- 重装(改端口/绑定):`powershell -File scripts/install_market_mcp_task.ps1 -Apply [-Port 17779 -Host_ 127.0.0.1]`
- 日志:计划任务 stdout 进任务历史;审计见 `data/mcp_audit/`
- 测试:`python -m pytest tests/test_mcp_queries.py tests/test_mcp_auth.py`(37 例)

## 已知实现要点(为什么这样做)

- **为什么 search 用范围而不是 LIKE(2026-09-13 起库级已根治)**:库排序规则为 ICU `en-US`,`LIKE 'prefix%'`
  无法下推索引(实测种子查询 >15s 超时)。实现改为"封闭字母表后继":
  首次连接向数据库查询合法字符集的真实排序,把前缀家族转成
  `symbol >= prefix AND symbol < successor` 精确范围,纯索引扫描
  (实测 0.01s)。
- **为什么 coverage 拆两条**:`max(ts) FILTER (WHERE duration=...)`
  会扫该符号全部历史(296ms 且冷缓存易超时);拆成
  `(symbol, duration)` 等值两次探测 0.4ms。
- **为什么未来要换 mcp_readonly 角色**:当前用属主连接 + 连接级只读;
  `scripts/sql/create_mcp_readonly_role.sql` 备好,拿到 postgres
  管理员密码后一条命令升级为角色级最小权限(并保持连接级只读双保险)。
