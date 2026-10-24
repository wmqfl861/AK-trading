# 无限易 PythonGO 观察桥接

本项目以后以天勤作为数据下载渠道，以无限易 PythonGO/东海 CTP 作为执行端。当前交付是**观察连接与模拟订单接口验收**：本机服务给客户端的响应固定为 `hold`，已安装的独立观察策略不会调用真实 `send_order` 或撤单函数。

## 当前部署关系

- 项目：`D:\Projects\AK-trading`
- 原桥接：`127.0.0.1:17777`，原 WorkBuddy `bridge_server.py`，未覆盖或重启。
- 新观察桥接：`127.0.0.1:17778`，由项目 `scripts/run_infiny_bridge.py` 启动。
- 新策略类：`AKObserverBridge`，独立文件安装到无限易 `pyStrategy/self_strategy/AKObserverBridge.py`。
- 原 `AIBridge.py` 保持原始内容。本轮开始时两份外部文件已备份到 `data/maintenance_artifacts/infiny-bridge-20260909/`。

独立安装是因为当前工具无法核实无限易 UI 中实际实例的参数：Windows UI Automation 只能看到顶层控件，截图接口不支持，激活失败；框架 `instance_files` 也没有保存的 AIBridge 配置。因此不能声称已验证原实例的 `enable_trading` 或真实订单状态。

## 启动本机服务

```powershell
cd D:\Projects\AK-trading
python scripts/run_infiny_bridge.py --port 17778 --symbol SHFE.rb2610 --history-bundle data/maintenance_artifacts/infiny-bridge-20260909/history-rb --history-through 2026-09-08 --audit-log data/maintenance_artifacts/infiny-bridge-20260909/observer-audit-final-v2.jsonl
```

具体启动日志和 PID 以部署收据为准，不要在端口已监听时重复启动。服务只绑定 loopback，不调用远程大模型，不读取或修改 LLM 配置。

- GET `/health`：服务版本、观察模式和执行禁用状态。
- GET `/status`：请求计数、最近接收时间及范围有限的脱敏状态。
- POST `/heartbeat`：策略初始化/运行后的只读状态快照。
- POST `/analyze`：行情批次，回复严格为 `{"signal":"hold","reason":"20字以内"}`。

TEST 请求只证明本地协议，不能充当真实 CTP 行情证据。请求中的非 TEST 标识也只是发送者声明，只有与客户端实例日志和时刻关联才能确认来自无限易。

## 本次实际验收（2026-09-09）

- 最终 `AKObserverBridge.py` 已安装到上述 PythonGO 目录，与 `install-receipt-final-v2.json` 及项目源码哈希一致。
- 新观察服务已在 `127.0.0.1:17778` 运行；原 `17777` 服务和原 `AIBridge.py` 的哈希均未改变。
- `/health` 与 `/status` 正常，本地合成请求返回纯JSON hold。实际适配器 transport 也已通过真实回环HTTP验证，使用的是假SDK对象，没有调用无限易原生交易函数。
- 完整离线回归为 `1650 passed, 9 deselected`；其后最后两项协议/HTTP修复及新增回归，在全部桥接专项中得到 `66 passed`。没有将后续专项结果伪写成另一次全量结果。
- 独立复核确认观察类订单入口不可执行，协议竞价/限价与订单数量问题已修复。真实回环测试另外发现并修复HTTP完成后操作已关闭socket的问题。
- 最后检查时未发现运行中的无限易进程；此前UIA仅可见顶层控件且截图/激活不可用。**本轮未在真实客户端加载/启动观察实例，未确认收到真实tick或真实账户/持仓快照，也未提交或取消真实订单。**

本地测试请求使用TEST标记；`X-Infiny-Source` 只是发送者声明，因此测试使用的相同标记不能证明是PythonGO原生进程发送。`live_client_verified=false` 和原生订单计数unknown按实际保留。

待客户端重新打开后，只需按下节在PythonGO新建/启动 `AKObserverBridge` 观察实例，不必重新安装已存在文件，也不必重复启动17778服务。保留 `enable_trading=False`。本轮提供模拟订单参数验证，没有开放实盘订单执行。

证据目录：`data/maintenance_artifacts/infiny-bridge-20260909/`，包括 `final-state.json`、`adapter-transport-verification.json`、`final-focused-v2.log`、`full-regression-final.log`、`install-receipt-final-v2.json` 和 `execution-result.json`。

未知持仓不会再让整条心跳失败：`status=unknown` 且数量为 `null` 表示“无法确认”，服务返回 `200 + {"signal":"hold","reason":"heartbeat"}`，决策层继续阻断开仓；只有非整数、负数或多空净仓不一致才返回结构化 `invalid_position`。协议错误会在 `/status` 保留脱敏 endpoint、instrument、payload hash 和 reason，不记录原始账号或密码。

本次修复后的本地端到端回归为 `10 passed`（未知持仓心跳、协议拒绝记录、未知/非法持仓、账户信息保留）；完整观察桥接相关专项回归为 `76 passed`。实际回环服务验证返回 HTTP 200，订单提交数仍为0。


## 2026-09-10 未知持仓心跳修复

无限易实例曾经发来两次心跳，但桥接返回 `400 invalid_position`。原因是客户端在尚未取得持仓明细时发送 `status=unknown` 和各数量 `null`，协议把“未知”误当成“非法”。现已修复：未知状态保留为 unknown，心跳返回 `200 + hold`；非整数、负数或多空净仓不一致仍拒绝，决策层继续阻断开仓。

修复回归为 `76 passed`，其中包含适配器实际生成心跳发送到本地服务的端到端测试、账户信息保留、未知持仓、非法持仓和脱敏拒绝记录。服务当前最新记录为主代理本地验证的 SHFE/rb2610 心跳，`source_header` 为空，不能冒充无限易原生请求；服务没有收到新的行情批次，真实账户/持仓读取仍未完成。

客户端曾在加载旧 `AIBridge` 时，经 `pythongo.base → utils` 或 `pythongo.ui.widget` 导入 NumPy/ctypes 报错。独立 Python 的 ctypes/NumPy 导入正常，问题发生在客户端嵌入解释器的该依赖路径，尚未对底层二进制兼容原因作进一步断言。

桥接不需要绘图与数值依赖，现改为标准库加客户端原生 `INFINIGO` 回调接口，去掉 `pythongo.base/ui`、Pydantic、NumPy和PyQt的加载依赖。`AIBridge.py` 和 `AKObserverBridge.py` 两个文件已同步更新，保持纯观察模式；原版保存在 `data/maintenance_artifacts/infiny-load-fix-20260909/`。旧的安装哈希收据因此已被本次部署记录取代，勿再用旧收据删除新版文件。

使用与客户端相同的 `SourceFileLoader.load_module` 加载这两份部署文件并实例化，已在阻断上述可选依赖且模拟原生接口的环境中通过。该验证不冒充客户端原生加载成功。客户端列表若保留“加载失败”，选择策略后点击“加载”（已加载旧版则先停止对应观察实例再“重载”）。这次修改没有修补全局 Python、ctypes 或任何模型配置。

## 在无限易加载独立观察实例

1. 打开 PythonGO 策略管理，刷新策略列表，选择 `AKObserverBridge`。
2. 新建独立实例；设置 `exchange=SHFE`、`instrument_id=rb2610`、`api_url=http://127.0.0.1:17778/analyze`，保留 `enable_trading=False`。
3. 启动该观察实例。其网络请求异步排队，不在 tick 回调内等待 HTTP。启动/行情回调采集允许的账户、持仓和合约信息；无法获取的字段保持 unknown，不自动写成零。
4. 查看客户端日志与服务 `/status` 是否出现匹配的心跳和批次；交易时段观察到真实 tick 后，再核对行情计数增长。
5. 停止时只停止这个新观察实例，不停止或更改其他策略。

该观察实现即便收到旧服务的 buy/sell 或 UI 误把交易参数改为 True，也不执行真实报单。其订单预览功能只构造参数，用于离线假发送器验证 OPEN/FAK 方向、价格与手数，不代表真实报单已被柜台接受。

## 量化规则接入方式

`src/ak_trading/infiny_bridge/decision.py` 调用现有日线策略注册表，可选 `ma_trend`、`donchian_trend`、`zscore_reversion`。启动时读取冻结的具体合约日线，只有已完成历史满足周期、身份和截止日条件时才计算内部候选；十个 tick 不会替代60根日线。

示例导出中 rb2610 有87根日线，最新2026-09-04。预期历史截至2026-09-08，因此当前数据过期，不会用于产生候选交易。资金为零、持仓未知、有现存持仓/未完成订单、竞价/限价/报价无效或市场状态未知也会阻断。

候选 buy/sell 只用于内部观察日志；对客户端的执行响应始终hold。现有旧协议中的sell表示卖出开仓，不能拿它平多仓。策略产生退出、反手、网格减仓等意图需要独立的开平语义与今昨仓处理，这一观察交付不将它们误映射为开仓。

## 安装与恢复

项目模板位于 `integrations/infiny/AIBridge.py`，安装器只新建独立类，不覆盖原文件：

```powershell
python scripts/install_infiny_observer.py install --strategy-directory "C:\Users\Administrator\AppData\Roaming\InfiniTrader_WxyAllProgramX64\pyStrategy\self_strategy" --receipt data/maintenance_artifacts/infiny-bridge-20260909/install-receipt-final-v2.json
```

文件存在则拒绝安装。安装收据记录内容哈希。以后停止对应观察实例后，可以按收据移除本轮安装的文件；若内容已改动则拒绝删除：

```powershell
python scripts/install_infiny_observer.py remove --strategy-directory "C:\Users\Administrator\AppData\Roaming\InfiniTrader_WxyAllProgramX64\pyStrategy\self_strategy" --receipt data/maintenance_artifacts/infiny-bridge-20260909/install-receipt-final-v2.json
```

删除文件不等于卸载内存中的实例，也不会终止本地服务。关闭服务应核实部署收据的 PID、端口和脚本身份后，仅停止本轮观察进程。

## 验收层级与限制

交付报告分别记录服务连通、合成协议、SDK模拟接口、客户端心跳、真实行情、只读账户/持仓、真实订单回报。未观察到的项目明确写未验证。

账户未入金不等于不会报单，也不能用真实柜台拒单作测试。当前没有打开真实交易开关，没有提交或撤销真实订单。后续实盘应先完成具体策略、合约、数量、价格、总风险、重复控制、开平/今昨仓、订单成交回报和人工确认流程；不能仅凭 `send_order` 方法存在认定交易功能验收完成。
