# econ 宏观新闻与分析 MCP：项目接入说明

本项目已安装用户指定的 `econ` 远程 MCP，采用 Streamable HTTP 与 HTTPS：

`https://econnews.85.137.246.111.sslip.io/mcp`

用途是获取宏观新闻、会议简报、已有投资观点和按问题生成的宏观分析。它可以为中国期货研究提供外部信息；目前没有把其回答自动写入行情库、因子值表或交易路由。

## 1. 安装位置与加载

- 项目实际配置：`.zcode/config.json` → `mcp.servers.econ`。
- 本机配置含用户提供的 Bearer Token，已通过 `.git/info/exclude` 排除出版本控制；没有在文档或示例中复制令牌。
- 可共享配置模板：[`config/econ_mcp.example.json`](../config/econ_mcp.example.json)。模板中令牌是占位符。
- 项目级配置不会修改用户全局 MCP、模型、供应商或推理设置。没有更改远端服务配置。
- 当前工具调用预算设为 `timeoutMs=120000`。实测 `ask_econ` 一次用时约63秒，原60秒预算不足。服务未承诺每次都在120秒内完成。

ZCode在项目会话启动时自动读取项目配置。如果当前会话工具列表仍没有econ，在本项目新建会话，或在“设置 → MCP”中查看并重新连接。**本次已通过独立MCP客户端验证相同项目配置可用，没有宣称当前会话已经热加载新工具。**

若在另一台机器使用，将模板转换为该客户端支持的MCP格式，并在那台机器安全填写令牌；不要把真实配置提交Git。`.git/info/exclude`是本机设置，不会随仓库克隆自动传播。

## 2. 实际发现的9个工具

| 工具 | 实际参数 | 返回与用途 |
| --- | --- | --- |
| `get_server_status` | 无 | MCP版本、服务检查时间和远端组件状态 |
| `get_pipeline_status` | 无 | 各采集任务最近成功时间、失败次数及活动告警 |
| `get_frontpage_news` | `limit=10` | `hero`与`featured`新闻，含来源、时间、标题、摘要、URL和重要性分数 |
| `get_latest_briefing` | 无 | 最新简报的`meeting_id`、`generated_at`及Markdown正文 |
| `list_briefings` | `since_iso=""`, `limit=10` | 指定生成时间之后的简报。实测限量结果按生成时间升序；不要假定第一项是最新。 |
| `get_recent_meetings` | `limit=5` | 状态、降级标记、有效专家数、业务日、完成/发布时间 |
| `get_investments` | `limit=10` | 已有会议观点：资产、方向、信心、期限、理由、概率和结果状态 |
| `ask_econ` | 必填 `question` | 汇总已有简报、建议、相关新闻和日历的生成式分析；含`answer`与`grounding` |
| `wait_for_new_briefing` | 必填 `since_iso`，`timeout_seconds=300` | 等待新简报或返回超时，不会创建本地计划任务 |

服务器可能升级工具，运行时仍以`list_tools`返回的实际schema为准。`limit`的计数语义不能推断为恰好返回N条；例如头版由hero和featured两部分组成。

长轮询建议显式设`timeout_seconds=30`，给传输预留余量；默认300秒长于当前项目120秒调用预算。实测2秒超时返回`{"status":"timeout", ...}`，这是“没有新简报”，不是服务错误。

## 3. 给agent的直接使用流程

1. 调`get_pipeline_status`与`get_server_status`，检查新闻更新、会议状态和可用性。服务active不等于最新预测完整有效。
2. 调`get_frontpage_news(limit=5)`，保留新闻ID、来源、原始时间和URL。重要性分数用于阅读排序，不是涨跌概率。
3. 调`get_recent_meetings(limit=5)`，再结合`get_latest_briefing`和`get_investments`，核对`meeting_id`、`published_at`、`degradation_state`、预测期限；不能把“最新简报”和“最近一次已发布建议”混为同一场。
4. 按明确品种、决策时间和观察期限调用`ask_econ`。要求事实与判断分开，并明确原始证据、概率含义、单位和失效条件。
5. 通过本项目行情MCP的`get_coverage/get_daily_bars/get_minute_bars`获取实际合约行情，再做验证。宏观观点不覆盖当前报价、可交易性和成本检查。

可直接给agent的请求：

> 使用econ MCP读取当前新闻和最近会议。分析未来1—5个中国交易日美元、实际利率、通胀及能源供给对SHFE.au、SHFE.cu、INE.sc的传导。先报告信息截止时间、会议ID、发布日期和降级状态。将新闻事实与模型意见分开，为关键事实列原始链接；缺链接则标记未核实。每个概率注明事件、资产、期限、来源与校准状态，不把海外资产概率直接用于中国合约。给出上行和下行条件、失效条件及未知项。国外价格门槛保留原资产、币种和单位。正文尽量控制在1200字以内；不能确认时明确写未知。最后用行情MCP核对实际合约与数据新鲜度。

程序调用示例（项目现有环境已具备`fastmcp`）：

```python
import asyncio
import json
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

async def main():
    config_path = Path("D:/Projects/AK-trading/.zcode/config.json")
    cfg = json.loads(config_path.read_text(encoding="utf-8"))["mcp"]["servers"]["econ"]
    transport = StreamableHttpTransport(url=cfg["url"], headers=cfg["headers"])
    async with Client(transport, init_timeout=25, timeout=120) as client:
        tools = await client.list_tools()
        print([tool.name for tool in tools])
        status = await client.call_tool("get_pipeline_status", {})
        print(status.data)
        news = await client.call_tool("get_frontpage_news", {"limit": 3})
        print(news.data)
        result = await client.call_tool("ask_econ", {
            "question": "简述未来1至5个中国交易日对SHFE.au的宏观影响。"
                        "请注明证据时间、链接、会议降级状态、海外价格单位和观点失效条件。"
                        "无可靠概率就写未知，控制在800字内。"
        }, timeout=110)
        print(result.data)

if __name__ == "__main__":
    asyncio.run(main())
```

示例读取本机现有配置，不输出令牌。不要关闭TLS证书验证；不要把`.env`、交易账户、持仓或数据库连接串放入问题。

## 4. 本次实际验证结果

验证时间记录为UTC，完整响应保存在 [`data/maintenance_artifacts/econ-mcp/`](../data/maintenance_artifacts/econ-mcp/) 下。

- HTTPS握手约0.69秒；服务自报`econ/1.1`；无令牌请求HTTP 401。
- 9项工具均已实际调用。新闻/状态/会议查询通常小于0.4秒；长轮询2秒正常返回没有新简报。
- `ask_econ`对中国黄金、铜和原油的问题返回了传导、方向条件、失效条件和证据范围，用时63.234秒。
- `grounding`自报使用12项建议、8条新闻、10个日历条目，并给出信息截止时间；这些是远端合成回答的来源统计，不是本项目独立核实了全部原始资料。

不能忽略的限制：

1. 最新会议`MTG-510ff6a76d5644fa`的`state="published"`，但`published_at=null`，`degradation_state="degraded_two_expert_non_resolution"`且有效专家2。简报也写“published（未发布）”。因此不能仅凭state字符串将其作为正常发布的三方一致结论。
2. `get_investments`返回的建议来自另一场9月12日会议。沿用旧意见时必须核对各自期限和失效条件，不因为接口返回最新就认为意见仍有效。
3. `ask_econ`正文明确说其证据中没有新闻原始链接。头版部分URL也为空，来源媒体名称不能替代可追溯URL；本次没有独立核查所有新闻事实。
4. 概率没有校准方法或历史准确率证据。一项0.56若描述WTI某情景，不能改写成INE.sc上涨概率；`conviction`、新闻分数、情景权重和策略盈利概率是不同量。
5. 回答引用了9月10—11日的24小时SPX窗口来讨论未来1—5个中国交易日；较新SPX建议已明确指出反弹证伪此前单日下跌观点。这种已到期、已被后续意见否定的判断不能继续支撑未来方向。原油概率前文为`0.56=0.44+0.12`，后文却沿用旧版`0.42+0.10=0.52`；必须回到对应会议的原始定义核对，不能混加跨版本权重。
6. 国际黄金4438—4600等门槛没有完整附币种与单位，不能作为SHFE.au的人民币/克触发价。国内汇率、基差、税费及合约单位需要独立处理。回答用两年期名义国债收益率论证实际利率变化时，还缺少通胀预期调整证据。
7. 本次长回答末尾在“不”处终止，疑似输出截断；服务没有明确truncated标志。本项目未修改远程服务，建议短问题分品种问，不将不完整长文直接结构化成信号。

## 5. 与中国期货因子库怎样结合

本次适合先作为“事件与宏观研究输入”。使用605条因子目录时要保留以下区别：

| 因子方向 | econ当前能够帮助什么 | 仍缺什么 |
| --- | --- | --- |
| `SEA010`新闻方向 | 新闻标题/摘要/来源、初步方向解释 | 完整原文、首发与首见时间、去重、固定版本的品种相关性和方向评分 |
| `SEA011`新闻新颖性 | 提供近期可见新闻候选 | 历史语料、稳定去重键和只能引用当时历史的相似度计算 |
| `SEA008/SEA009`事件与政策意外 | 问答可参考未来日历、解释政策影响 | 结构化事件日历、原定时间、事件前一致预期和首次公布值；本次9工具没有独立日历/预期数据接口 |
| `SEA012`供应中断暴露 | 发现供应事故、路线风险和恢复条件 | 可核验产能/运量分母、受影响份额、预计持续时间，不能从文字强填数值 |
| `MAC009/MAC012/MAC013/MAC017`资金、美元、实际利率、通胀预期 | 提供宏观解释和相关证据线索 | 对应可追溯数值序列、单位、发布与修订历史；当前工具不是通用宏观时间序列查询API |

宏观变量同一天对多个品种可能相同，需要研究各品种敏感度与交互，不能仅凭一段宏观看法给所有品种相同方向。

推荐接入顺序：先保存带`retrieved_at`、新闻/会议ID、原始发布时间、预测起止时间、来源URL、降级状态和原响应哈希的只读快照；明确区分事实、远端模型意见与项目派生标签。随后先做研究期的影子观察和事后评分，再决定是否进入概率模型。历史补采不能伪装为当时已知信息；现存概率研究模型也不会因安装MCP而自动增加宏观输入。

## 6. 故障判断

- 401：检查本机令牌配置，不在日志中打印令牌。
- TLS/连接失败：保留原错误，核对DNS、时间和证书；不要绕过证书校验。
- ask超时：先查服务/管线状态，缩小问题和输出长度；避免并发重复生成同一长分析。
- 会议降级或无建议：保留未知/降级标记；不要用上一场意见填成新预测。
- 当前agent看不到econ工具：项目配置在新会话启动时加载。本次独立客户端实测成功与界面工具热加载是两个验收项。

没有创建定时轮询、没有远程下单、没有修改任何agent的模型配置，也没有写入主行情数据库。
