**无限易执行域开发决策规格（2026-09-14 冻结）**

本规格定义可直接实现、离线硬验收的期货执行闭环；它不构成实盘提交、撤单或启用交易的授权。本次文档任务唯一写入 `docs/infiny-execution-development.md`，不启动客户端、不调用 native、不读取凭据、不联网、不训练、不调用外部 agent。
后续开发仅在下列独占文件内进行；禁止修改 `src/ak_trading/v2/`、`tests/test_v2_*`、`docs/devplan-v2/`、模型配置及实际交易开关。本文的“节点”是开发分工，不要求本次启动其他 agent。

**1. 证据与当前边界。** 以下区分静态核实、简报转述和待验证事实。

- 项目依据：[AIBridge.py](../integrations/infiny/AIBridge.py) 的 `update_status_bar`、`build_order_preview`、`_build_envelope`、`_remember`、`_read_position_snapshot`、`_worker_loop`；[protocol.py](../src/ak_trading/infiny_bridge/protocol.py)、[service.py](../src/ak_trading/infiny_bridge/service.py)、[decision.py](../src/ak_trading/infiny_bridge/decision.py)、[history.py](../src/ak_trading/infiny_bridge/history.py)。
- SDK 仅静态读取 `C:/Users/Administrator/AppData/Roaming/InfiniTrader_WxyAllProgramX64/pyStrategy/pythongo/` 下 `infini.py`、`const.py`、`classdef/position.py`、`classdef/order.py`、`classdef/trade.py`，没有 import 或执行它们。
- [执行简报](../data/maintenance_artifacts/infiny-reconnect-20260914T030742Z/codex-execution-brief.md) 记录客户端 PID 10048、17778 服务 PID 18996、历史截至 2026-09-11 共 161 根日线、30 秒热更新、当日 client receipt 尚缺；这些运行事实本次未复验，不能据此标记连接验收成功。
- `update_status_bar` 源码已使用 `_native.updateState(self.strategy_id, {...})` 两个位置参数；简报报告相关 41 项离线测试通过、准备部署，不能写成已部署或本次重跑通过。
- `AIBridge` 与 `AKObserverBridge` 的提交、底层报单、自动平仓、撤单入口均阻断；`BridgeService` 的 `/analyze`、`/heartbeat` 始终返回仅含 `signal=hold`、`reason` 的响应。旧 `sell` 预览表示卖出开仓，不表示平多。
- `active_orders.count=None/status=unknown` 正确表达未知；512 条缓存、最近 20 条传输及 `callbacks_since_start` 都不是全量账本。`_remember` 会被迟到状态覆盖，成交缓存不会核销订单，不能复用为执行对账器。
- 当前持仓归一化只有总量、今昨量，未保留平仓冻结；空列表目前显示 ready/零，但公开查询没有完成屏障，执行域不得将该结果视为柜台确认空仓。观察协议还会滤掉 `Memo/FrontID/SessionID`，不能作为执行事件来源。
- SDK 支持查询投资者、资金、持仓、合约及行情订阅；公开接口没有全量委托/成交补查、查询完成或重连完成回调。回调类型不明确给出账户/策略归属，`TradeData` 也没有 `FrontID/SessionID`；接收实例不能自动证明事件归属。

**2. 决策 A：连接、规划、执行分开判断。** `Readiness` 同时返回 `connection`、`state`、`can_plan`、`can_submit`、`can_cancel_owned`、`blockers[]`、`mode`、`coverage`、`evidence_id`；单个绿色状态不代表全部能力。

| 状态 | 判定证据 | 允许行为与阻断 |
| --- | --- | --- |
| `DISCONNECTED` | 无匹配当前会话的有效往返，或往返超过 15 秒 | 查看历史证据、诊断；提交关闭，撤单仍需独立路由判定 |
| `CONNECTED` | 当前实例/会话请求已收到合法 hold 回复，服务 receipt 可关联 | 只证明本地双向传输；进程、端口、HTTP 200、source header 均不能单独证明原生连接 |
| `OBSERVING` | 当前会话有可归属的只读快照或行情，保留每项时效和来源 | 可显示观察数据；缺失字段显示 unknown，合成来源不能标为真实客户端 |
| `PLAN_READY` | 指定意图所需身份、行情、合约、持仓/资金及规则通过纯校验 | 可产出有到期时间的计划；账户订单覆盖不足仍可预览，明确 `can_submit=false` |
| `EXECUTABLE` | 模式、授权范围、线程、时效、资源与对账证据全部通过 | 本期仅模拟器可到达；必须标 `mode=simulation`，不可表示原生账户可交易 |
| `RECONCILE_REQUIRED` | 有提交未知、事件归属冲突、丢回报、账本损坏、重启未决或基线缺口 | 锁存阻断新提交/反手后腿/自动恢复；允许诊断规划；仅独立校验通过的自有订单可申请撤单 |

`connection=offline|service_only|roundtrip|stale` 独立保留；有未决风险时 `state=RECONCILE_REQUIRED` 优先，不被断线或新 heartbeat 清掉。无未决意图时按证据展示连接/观察/规划状态，并列出执行缺口。
`coverage=unknown|callbacks_since_start|own_journal|full_account` 必须携带范围、会话、交易日、来源、截至时刻及完成证据；`own_active_count=0` 永远不能推出 `account_active_count=0`。
**冻结：仅本策略 journal 不足以宣称全账户可交易。** 人工勾选“无挂单”、再次登录、延长静默时间、未入金、历史刷新成功，都不填补账户全量订单/成交与查询完成缺口。
原生 `submit_enabled=false`；禁止新开、自动平仓/反手、自动恢复重发、全账户撤单、凭净仓推断平仓、未知订单撤单。`cancel_owned` 是独立能力，当前也未获实盘授权；将来不要求行情新鲜或全账户零挂单，但必须验证自身归属、当前撤单路由及明确撤单授权。
`ReconciliationProof={proof_id,source,account_key,trading_day,session_epoch,as_of,order_inventory,trade_inventory,position_snapshot_id,complete_through,exclusive_writer_verified,verified}`；`complete_through` 为来源明确保证完整覆盖的事件水位，接收序号不能冒充源水位。本期仅模拟器可提供完整屏障；原生证据固定 `verified=false/complete_through=null`，不得靠布尔配置或 Memo 伪造。
以后若厂商提供可验证全量查询/导出与完成屏障，或另行审批有限范围、独占账户的人工接管方案，须先记录范围、外部交易隔离、基线时点及失效条件并更新验收；不能把该未来方案当作当前平台能力。

**3. 决策 B：四个节点、文件独占。** 只用标准库数据类/枚举、Decimal、SQLite 和可注入接口，不引入调度平台、通用事件总线、新数据库服务或执行 HTTP 路由。

下表 `E/` 指 `src/ak_trading/infiny_execution/`，`B/` 指 `src/ak_trading/infiny_bridge/`，`I/` 指 `integrations/infiny/`，`T/` 指 `tests/`；列出的新文件是后续开发产物，不表示已经存在。

| 节点与交付 | 独占生产文件 | 独占测试文件 |
| --- | --- | --- |
| N1 连接可靠性、数据契约 | 新 `E/__init__.py`（无副作用）、`E/contracts.py`、`B/connection.py`；改 `I/AIBridge.py`、`B/protocol.py`、`B/service.py` | 新 `T/test_infiny_connection_reliability.py`；仅本节点可改既有 `T/test_infiny_adapter.py`、`T/test_infiny_heartbeat.py`、`T/test_infiny_extended_protocol.py`、`T/test_infiny_realtime_extensions.py`、`T/test_infiny_load_compatibility.py` |
| N2 纯订单规划、今昨规则 | 新 `E/planning.py` | 新 `T/test_infiny_execution_planning.py` |
| N3 持久意图、资源、回报对账 | 新 `E/ledger.py`、`E/reconcile.py` | 新 `T/test_infiny_execution_ledger.py` |
| N4 禁用映射、集成、模拟闭环 | 新 `E/native.py`、`E/executor.py`、`E/simulation.py`、`I/AKExecutionBridge.py` | 新 `T/test_infiny_execution_e2e.py` |

先由 N1 单独提交冻结的 `contracts.py` 接口，再允许 N2/N3 按契约开发，N4 用假端口集成；跨节点修改通过文件所有者完成。N4 只整合集成结果，不共同编辑 N1/N3 文件；新增所有权必须先写入此表。
`AKExecutionBridge.py` 是独立且默认 disabled 的装配入口，不继承或覆写 observer 的执行禁用语义；不自动安装到客户端、不注册自动交易实例。N1 不改 `decision.py/history.py` 的决策及历史语义，旧服务候选不能接入新提交入口。

**4. N1 接口和连接时效。** 新接口均为开发约定，不是假称 SDK 已有的方法。

- `ConnectionMonitor.note_request(meta) / note_response(meta, result) / status(now)`；`SnapshotPump.capture_on_callback() / drain_on_callback()`；注入时钟和 transport，采样与响应均带 `instance_id, session_epoch, sequence, request_id`。
- 每次 start/reconnect 产生新 `session_epoch`，重复 start 不重复订阅；响应附带本地请求上下文后入队，旧会话/旧序列响应不能覆盖新状态。stop 仅清理自身线程/订阅，失效旧会话，不能释放执行未决资源。
- worker 每 5 秒发送独立 heartbeat，HTTP 总期限仍不超过 3 秒；只复制最后一次不可变快照，更新 `sent_at` 和请求序列，绝不改写 `sampled_at/source_at`。无快照也可发送明确 unknown 的连接心跳。
- `TimingPolicy` 默认秒数：`link_ttl=15, packet_ttl=10, quote_ttl=3, account_ttl=5, position_ttl=5, contract_ttl=3600, future_skew=2`。判断使用 `age > ttl` 失效；缺时刻或超过未来容差阻断。该执行时效独立于观察协议现有 30 秒行情阈值。
- UTC 日历时刻用于跨进程数据年龄；本地 monotonic 用于期限及超时，不跨进程比较 monotonic。交易日独立校验，夜盘不能以 `TradingDay+UpdateTime` 伪造日历时间。重连、换账户/合约或交易日变更使旧快照、计划和证明失效。
- 每项记录 `sampled_at, source_at|null, received_at, snapshot_id, source, completeness`；资金/持仓接口无服务器时刻时只可宣称“刚读取”，不能宣称数据源已刷新或查询已完成。行情用真实 tick 时刻，心跳不能替它续期。
- native 读取、订阅、日志、`updateState`、订单映射调用仅在经过线程校验的客户端回调执行；worker 仅网络、时钟和线程安全内存，不读取 native、不更新 GUI。
- 当前未证实安全周期 GUI 回调：`ui_idle_refresh=unsupported`。无 tick 时服务状态仍更新，客户端界面在下一合法回调 drain；不发明 `on_timer`，不恢复 numpy/ctypes/pythongo.base/ui/PyQt 依赖。空闲后采样陈旧时执行保持阻断。
- observer 请求仅增加白名单 `connection` 元数据；N1 同步协议保留和状态汇总，回复仍严格两个字段。`/status` 区分 TEST/合成与未核实客户端；只有 UI 实例日志、版本/会话/请求标识、服务 receipt 与真实回调关联后才记录独立客户端验证证据。

**5. N2 输入、输出与纯规划规则。** `contracts.py` 冻结以下结构，未知用 null，不以默认零补全；价格/资金用 Decimal 的规范十进制字符串序列化。

| 类型 | 必填字段与语义 |
| --- | --- |
| `Binding` | `account_key`（不透明且稳定）、`strategy_key`（持久）、`strategy_id`（native int）、`instance_id,session_epoch,trading_day,exchange,instrument_id,hedge_flag`；账户标签不作身份 |
| `ExecutionSnapshot` | `snapshot_id,binding,source,times,quote,contract,account,positions,coverage,proof_id,ledger_revision`；`source=simulation\|pythongo`，时间和完整性按组件保存 |
| `quote / contract` | quote：`time,last,bid1,ask1,bid_volume1,ask_volume1,upper_limit,lower_limit,market_status`；contract：`exchange,instrument_id,asset_class,expiry,price_tick,multiplier,min_limit_order_volume,max_limit_order_volume` 及已核实额外手数限制 |
| `account / positions` | account：`available,currency`；每个方向及 hedge 桶：`Position,PositionClose,FrozenClosing,YdPositionClose,YdFrozenClosing` 和身份/采样信息；缺桶不能按零推断 |
| `OrderIntent` | `schema_version=infiny_execution_v1,intent_id,source_key,binding,action,volume,prices,close_policy,order_type,created_at,expires_at`；`source_key` 为同一策略决定/人工请求的稳定键 |
| `Action` | `OPEN_LONG,OPEN_SHORT,CLOSE_LONG,CLOSE_SHORT,REVERSE_TO_LONG,REVERSE_TO_SHORT`；普通 volume 为请求手数，反手 volume 为平掉全部原方向后新开手数 |
| `prices / policy` | `prices.open`、`prices.close` 按动作必填；`close_policy=AUTO_YD_FIRST\|TODAY_ONLY\|YESTERDAY_ONLY`；`order_type=GFD\|FAK`，`market=False` |
| `RiskPolicy` | `policy_id,valid_until,max_leg_qty,max_gross_position,max_inflight_legs,max_cash_reserve,cash_per_open_lot,fee_per_close_lot,cash_bound_source`；开仓现金上界须覆盖保证金、费用及缓冲且有可信来源，不能拿账户 Fee/Margin 金额当费率 |
| `Plan / Leg` | Plan：`plan_id,intent_hash,snapshot_id,ledger_revision,policy_id,expires_at,legs,blockers`；Leg：`leg_id,index,phase,depends_on,direction,offset,volume,price,position_bucket,cash_reserve`；`offset=OPEN\|CLOSE\|CLOSE_TODAY\|CLOSE_YESTERDAY` |

`plan(intent, snapshot, reservation_view, policy, now) -> Plan` 不做 I/O、不预留、不接触 sender；plan_id 为规范意图、快照、规则及账本版本的 hash，leg_id 由 intent_id、phase、桶和拆分序号确定。`validate_plan(plan, latest_context, now)` 在实际尝试前重验；计划到期取意图、规则、各必需组件时效的最早期限。
`ExecutionContext={mode,binding,snapshot,proof,policy,authorization,reservation_receipt,owner_thread_id,readiness}`；同意图后续更新快照不改变已冻结腿的方向/数量/身份/限价，若新手数规则需要改变拆分拓扑则阻断，不能生成替代已尝试腿。
一期支持已明确绑定、未到期且非交割日的具体单腿期货合约；主连、指数、猜测合约、期权/组合执行均返回明确 unsupported。保留既有期权观察能力。
账户必须由客户端显式选择并与 `getInvestorList` 的实际 InvestorID 匹配，只在客户端内存映射 `account_key -> Investor`；不得选择列表第一项或把 `account_tag` 当账号。多个账户、回报范围或 hedge 归属不明确时阻断执行。
合约查询返回身份必须和意图逐项一致；缺交易所、合约、PriceTick、有效手数边界、到期信息均不能靠默认值补成可执行。行情、资金、持仓不能跨账户/合约/会话复用。
价格必须有限正数且 `price % price_tick == 0`，处于有效上下限内；不静默四舍五入。默认仅连续交易，拒绝竞价/闭市/状态未知、行情处于涨跌停、买卖倒挂、所需对手档无价格或无量、价差超过 10 tick。
手数须为正整数且不是 bool；每个拆分子单满足交易所与 RiskPolicy 的有效 min/max。超 max 可确定性拆分且不改变总量；不能形成合法尾单则整份计划失败，不偷偷放大、缩小或部分执行。

| 动作 | 方向 | 开平与阶段 |
| --- | --- | --- |
| 开多 / 开空 | BUY / SELL | OPEN |
| 平多 / 平空 | SELL / BUY | 从对应 long / short 桶分配 CLOSE_TODAY、CLOSE_YESTERDAY 或通用 CLOSE |
| 多反空 / 空反多 | 先 SELL 平多 / BUY 平空；后 SELL 开空 / BUY 开多 | 先关闭全部原方向，再按 intent.volume 开新方向；已有双向仓或无法全部预留原仓则拒绝该反手计划 |

平仓量基于 SDK 原始字段，逐方向/hedge 计算：`td_close=PositionClose-YdPositionClose`，`td_frozen=FrozenClosing-YdFrozenClosing`，`td_available=td_close-td_frozen`，`yd_available=YdPositionClose-YdFrozenClosing`。
先校验所有量为非负整数、`PositionClose<=Position`、昨量不超总可平量、各冻结不超对应可平量、`YdFrozenClosing<=FrozenClosing`；任一矛盾返回 invalid，不用 clamp 隐藏坏数据。
SHFE/INE：默认先昨后今，昨仓用 CLOSE_YESTERDAY（原生 `'1'`），今仓用 CLOSE_TODAY（`'3'`），分成独立订单；TODAY_ONLY/YESTERDAY_ONLY 不足时拒绝，不偷偷切桶。
其他交易所：AUTO 用通用 CLOSE（`'1'`）并预留合并桶 `ALL`，今昨分配不可承诺；严格 YESTERDAY_ONLY 拒绝为 unsupported，TODAY_ONLY 也须有交易所支持证据，本期缺证据时拒绝。
反手开仓腿必须等所有平仓腿成交量达到原持仓、无未决撤单/提交、成交明细齐全且新完整持仓快照证明原方向为零，再用新行情/资金重规划并提交；部分成交、撤单、拒单、过期均不自动触发开仓。

**6. N3 资源与持久意图账本。** `Ledger(path)` 使用 SQLite 本地文件，`synchronous=FULL`；写入失败、损坏、锁超时立即关闭提交，禁止回退到易失内存。

- 五张表：`intents,legs,events,trades,reservations`，证明及快照引用保存在事件/意图内；UNIQUE `(account_key,strategy_key,intent_id)` 和 `(account_key,strategy_key,source_key)`，leg_id 全局唯一。不沿用观察窗口、不截断未决记录。
- `register(intent)`：规范 payload hash 相同返回原记录；同键不同 payload 返回 `INTENT_CONFLICT`；换 UUID 不能绕过相同 source_key 的未决动作。创建新请求不得自动替代未知结果的原请求。
- `reserve(plan, expected_revision)` 在 `BEGIN IMMEDIATE` 中校验版本、当前快照、账户总预算并原子预留全部腿；失败整笔回滚，成功返回 `ReservationReceipt={plan_id,reservation_ids,revision}`。后续重验以该收据认领本计划已占资源，不能再次扣同一预留；其他版本变化须重新核算。同账户策略共享预算，外部写入方仍需独占范围证据。
- 平仓本地余量 `max(0,sdk_available - local_not_proven_reflected)`；资源键为 `(account_key,exchange,instrument_id,hedge_flag,side,TD|YD|ALL)`。开仓预留 `volume*cash_per_open_lot`，平仓另预留费用上界；跨合约汇总现金，按总多空量而非净仓检查仓位上限。
- 资金/持仓快照可能已含本单冻结，必须保留吸收关系证据：只有完成屏障明确覆盖指定 leg/event 才把预留标为 reflected；不能凭聚合冻结量或 available 变化猜测。缺证据时继续扣本地预留，允许保守重复占用，不冒险释放额度。
- 状态 `HELD -> REFLECTED|RELEASED`；成交部分转为待快照吸收的占用，不能立即成为可再次平仓量。取消成功也只在最终成交核对、冻结解除快照及覆盖屏障一致后释放未用部分；未尝试过且过期/撤回的计划可立即释放。
- 反手待开腿初始也占预算，不提前抵扣预计平仓释放的资金；后续重规划在同一事务替换该腿预留，保留稳定 leg_id 和从未提交证明。预算不足时保持阻断，不执行半套反手。
- `begin_attempt(leg_id, context) -> attempt` 持久提交 `SUBMITTING/attempt_no=1` 后才允许调用端口一次；`record_submit_result`、`ingest_event`、`recover`、`view` 为其余入口。同事务检查账户在途腿上限（默认1，RESERVED 不算在途，UNKNOWN 仍算）；串行调度提交/对账，不把 SQLite 锁持有到 native 调用期间。
- `Executor.submit_once(intent_id, leg_id, plan_id, context)` 先验前置依赖、版本/时效/模式/证明，再持久锁存 attempt；任何重入、并发、重启只能读取已有结果。断电可能发生在锁存后、调用前，允许漏发并待对账，不能承诺远端 exactly-once。
- native 返回非负 int（排除 bool）只记 `SUBMITTED_LOCAL` 和 local OrderID；SDK 约定 `-1` 为 `LOCAL_REJECTED`，无矛盾回报时释放该腿，仍不自动重试。异常、超时、未知返回值、调用后落盘失败一律 `SUBMIT_UNKNOWN`。
- 10 秒无可归属委托确认进入待对账，native 阻塞也不能启动第二个发送线程；恢复时遗留 SUBMITTING 视同 SUBMIT_UNKNOWN，已知本地编号但无终态也必须核对。HTTP 成功、后来的价格、资金变化都不是重发依据。
- 重启、重连、交易日切换不清空未决记录/预留；`recover(proof, events)` 只核对，不调 sender/canceller。证明缺失维持阻断；清账、删除 journal 或手填零不能作为恢复方法。

**7. 回报归属、去重和状态机。** `normalize_event(raw, callback_context)`、`Reconciler.ingest(event)` 直接消费新执行适配器的原始白名单回报，不消费 observer 的 20 条窗口。

`ExecutionEvent={event_id,kind,received_at,callback_session_epoch,account_key|null,ownership,exchange,instrument_id,hedge_flag,order_id,order_sys_id,front_id,session_id,memo,trade_id,order_time,trade_time,trading_day,status,total_volume,traded_volume,cancel_volume,price,volume,error_code}`；所有原生可缺字段保留 null，禁止补出不存在的 StrategyID/InvestorID。
字段归一化按 SDK 原始键或对应属性读取，例如 `OrderID/order_id`、`OrderSysID/order_sys_id`、`TradeID/trade_id`、`Hedgeflag/hedgeflag -> hedge_flag`；报单参数的 `HedgeFlag` 大小写与回报字段不同。接收上下文的账户/会话标记只记录来源声明，不能替代 ownership 证据。
身份分两层：本地别名 `(account_key,session_epoch,order_id)`，柜台别名 `(account_key,trading_day,exchange,order_sys_id)`；FrontID/SessionID 存作路由证据。OrderSysID 依 SDK 去空格、TradeID trim，保留大小写及前导零；不跨日/会话仅凭 local ID 合并。
归属需匹配持久提交记录、完整合约/方向/开平/hedge，并有已验证的回调账户/策略范围及唯一别名；Memo 只含稳定 leg 标识，用于相关性校验，Memo 或 OrderID 单独都不证明 ownership。平台未核实回传/归属时保持 unknown。
交易回报没有 FrontID/SessionID，先通过已验证 order 别名关联；先成交后委托、调用返回前同步回调、未知 Memo/编号均持久暂存隔离，待唯一关联后再计账。不能给迟到回报贴当前 epoch 就认领；不明确归属的回报不能影响其他账户/合约的数量或触发撤单。
成交唯一键 `(account_key,trading_day,exchange,instrument_id,trade_id)`；同键同业务字段不重复加仓/扣仓/计费，同键不同数量/价/订单等为冲突并阻断，不用最近值覆盖。不具备键或交易日证据的成交保留待对账，不靠接收日期猜交易日。
订单累计 `reported_filled=max(历史有效 TradedVolume)`，成交明细 `trade_filled=sum(唯一 TradeID 的 Volume)`，风险量取两者 max 而非相加；`trade_gap=max(reported_filled-trade_filled,0)`。超过委托量、订单总量/身份变化或数量不守恒立即隔离。
委托状态：`RESERVED -> SUBMITTING -> SUBMITTED_LOCAL -> WORKING -> PARTIAL -> FILLED`；分支 `LOCAL_REJECTED,REJECTED,CANCELLED,PARTIAL_CANCELLED,SUBMIT_UNKNOWN,EXPIRED`。EXPIRED 只适用于尚未尝试的腿；订单状态与 `reconcile_required`、`cancel_state` 独立。
`已报入未应答/交易员已报入` 只记 submitted；`待报入` 不算柜台接受；`未成交/部分成交/全部成交/已撤销/部成部撤/投资经理驳回/交易员驳回` 按现有已知词表归一化；其余队列中部分撤单保留 working 和数量，未知字符串不得猜成功/终结。
FILLED 不回退，累计成交不减少；迟到 pending 不复活任何终态。终态后唯一归属的真实成交仍可补齐甚至达到 FILLED；与终态最终数量冲突则待对账，绝不恢复为可继续下单的 working。
`settled=true` 要求终态数量守恒、trade_gap=0、无未知归属/撤单冲突且快照吸收证据通过；“订单全部成交”的回报先到也可显示 FILLED，但缺成交明细时不释放资源或启动反手后腿。
`events`、成交去重和订单投影在同一事务提交；重放结果与首次一致。落盘前队列满/事件丢失必须锁存 `EVENT_GAP`，不能像观察行情队列那样丢旧回报继续执行；日志只留白名单、错误码和不透明标识，不存凭据、原始 errMsg 或任意 Memo。

**8. N4 默认禁用原生映射及撤单。** `NativePort` 定义 `send_order(request) / cancel_order(order_id)`；`build_native_request(leg,binding,investor_handle)` 是纯函数，只有独立执行装配器持有端口。

`ExecutionMode=disabled|simulation|native` 默认 disabled；disabled 在解析 native 对象/获取方法前就拒绝。模拟器显式注入 FakeNative；native 每个动作均需符合授权、绑定、线程及 Readiness。submit 另需全账户完整证明；cancel_owned 另需已验证归属/当前撤单路由，不依赖全账户完整证明。当前两项原生门槛均未满足，不能通过模式字符串解锁。
native 拒绝 simulation 来源的快照/证明/授权，也不接受外部 payload 自报 verified；来源与能力由可信装配器决定，当前 native 能力表固定 `full_account_reconciliation=unsupported`。模拟 PASS 不得改变此能力表。
`Authorization={authorization_id,mode,account_key,strategy_key,session_epoch,symbols,actions,max_volume,price_bounds,expires_at}` 在每次尝试前复核；本次没有创建该授权，不能继承“可开发”的许可。停止/禁用会阻断新尝试，继续保存回报与未决状态，不自动撤单。

| 映射 | 精确值/调用约束 |
| --- | --- |
| 提交 | `INFINIGO.sendOrder(StrategyID=..., Exchange=..., InstrumentID=..., Volume=..., Price=..., Direction=..., OrderType=..., Investor=..., HedgeFlag=..., Offset=..., Market=False, Memo=...)` |
| 买卖 | BUY=`'0'`，SELL=`'1'`；不可传旧预览字符串 buy/sell |
| 开平 | OPEN=`'0'`，CLOSE/CLOSE_YESTERDAY=`'1'`，CLOSE_TODAY=`'3'`；CLOSE_YESTERDAY 仅 SHFE/INE |
| 指令 | GFD=`'0'`，FAK=`'1'`；const.py 的 FOK=`'2'`，简报指出 types.py 列 `'3'` 冲突，本期 FOK/裸 `'3'` 一律 unsupported |
| 其他 | Investor 只取已绑定客户端内存句柄；HedgeFlag 必须为已验证白名单值，不猜默认；Price 转 float 后 Decimal 字符串回读须保持原 tick 值，否则拒绝；Memo 只用 leg_id，长度/回传仍需平台核实 |
| 撤单 | `INFINIGO.cancelOrder(OrderID=...)`，无账户参数；返回 int 的成功语义未获证实，不能将返回值当最终撤单成功 |

`request_cancel(cancel_id, intent_id, leg_id, context)` 只接受 journal 已验证为自身、当前 native 路由唯一且尚有可撤剩余量的订单；不接受任意裸 OrderID。缺本地编号、会话已换且编号不可验证、foreign/unowned/unknown 订单均拒绝；不枚举或撤销全账户单。
取消权限独立于新增订单权限，行情过期不阻断已验证自有订单撤单；路由/归属/线程/模式/授权/持久能力仍必须有效。实际原生取消本期保持禁用，模拟器覆盖此分支。
`cancel_state=NONE -> REQUESTED -> CONFIRMED|UNKNOWN`；先持久取消 attempt，单腿只允许一个未决撤单尝试，同 cancel_id 幂等、换 ID 不绕过未决锁。超时/异常不自动重撤；仅后来明确拒绝且重新授权的动作可另建尝试。
`on_cancel/CancelOrderData` 可能缺 Status/TotalVolume/TradedVolume，仅证明收到撤单相关回调；不能直接推断全部撤销。收到可归属最终委托状态及完整成交/取消数量并对账后才确认；撤单期间成交照常去重、更新，剩余归零时不得继续撤单。

**9. 决策 C：最小硬验收。** 下表是后续实现的必测条目，不是本次已经执行的测试报告。

所有 E 测试使用 FakeClock、临时 SQLite、合成账号/合约与 FakeNative；真实 INFINIGO 必须在加载前被哨兵替换。禁止读取客户端数据目录、联网、真实账户查询、提交或取消；临时文件只属于后续测试任务，本次不运行测试。
基准夹具 `F`：模拟账户 A/策略 S/实例 I/strategy_id=23/会话 E/SHFE.rb2610/交易日 2026-09-14，当前时刻 09:30:00+08:00；模拟白名单 hedge=`'1'`（不构成原生值验证），完整多空零仓桶、零委托及完整模拟屏障，全部样本当前，连续交易，last=3500,bid=3500,ask=3501,两档量=10,上下限=3000/4000,tick=1,min=1,max=5,multiplier=10,到期=2026-10-15。
F 的 currency=CNY,available=100000,开仓现金上界=1000/手,平仓费用上界=10/手,max_leg_qty=5,max_gross_position=100,max_inflight_legs=1,max_cash_reserve=100000,ledger_revision=0；资金上界来源为模拟已验证，规则/模拟授权/意图均在当前时刻后24小时到期。仅依赖被测条件的字段在各用例显式覆盖。

| ID / 所属测试文件简称 | 可复现实验 | 必须断言 |
| --- | --- | --- |
| E01 / connection | 无 tick，假时钟推进 0、5、10 秒，transport 合法 hold；随后停回包至最后成功+15.001 秒 | 三次独立心跳；sampled_at 不变；状态 stale，native/GUI worker 调用为 0 |
| E02 / connection | 旧 epoch 响应晚于新 epoch 成功返回；重复 start，再 stop | 旧响应不能覆盖；仅一组自身订阅/worker；停止后无心跳；未决执行账本不被清空 |
| E03 / connection | TEST、伪 source header、仅 health 成功、UI 未关联的 receipt | 不标真实 client_verified；新元数据经协议保留；hold 回复仍仅两个字段 |
| E04 / planning | F 各自替换 quote 年龄 3/3.001 秒、account/position 5/5.001 秒、contract 3600/3600.001 秒、packet 10/10.001 秒、未来 2/2.001 秒 | 边界内允许校验，超界/缺时刻阻断；新 heartbeat 不改变旧 quote 年龄；夜盘日历时刻和交易日分别校验 |
| E05 / planning | F 换账户 B、合约/交易所错配、会话变化、多投资者未绑定、缺 hedge；换主连/期权/已到期合约 | 对应明确 blocker，send=0；标签相同不能通过账户身份校验 |
| E06 / planning | tick=0.2，价格 3500.2/3500.1，再输入 NaN/Inf/哨兵/越涨跌停价、倒挂、空档、竞价/状态未知 | 仅合法 tick 价格通过；不静默取整；各非法分支 send=0 |
| E07 / planning | min=3,max=5,volume=11；再 min=4,max=5,volume=6；另输入 bool/0/-1/1.5 | 首例合法拆为 5+3+3；第二例整计划失败；非法量拒绝，绝不改变总量 |
| E08 / planning | long:Position=10,PositionClose=10,Yd=6,Frozen=3,YdFrozen=2；SHFE/INE 平多 6 | 可用昨4/今3；先 SELL/CLOSE_YESTERDAY 4，再 SELL/CLOSE_TODAY 2；平空镜像 BUY |
| E09 / planning | E08 严格平今4、严格平昨5、平仓总8；再令 YdFrozen>Frozen 或任一字段缺失 | 全部拒绝；负可用/未知不被 clamp 或补零伪装；无持仓不得用开仓补平仓 |
| E10 / planning | DCE 同数量 AUTO、YESTERDAY_ONLY、缺证据的 TODAY_ONLY | AUTO 只出通用 CLOSE/ALL，按 max 拆分；严格今/昨均 unsupported，不声称强制只平昨 |
| E11 / ledger | 两个连接同账户同桶可用3，各并发预留2；另并发跨合约耗用共同现金 | 只一个平仓预留成功，现金合计不超上限；失败事务没有残留腿/资源 |
| E12 / ledger | SDK 今可用3，本地未吸收预留2；新快照已含本单冻结但无吸收屏障；再给覆盖证明 | 无证据余量仍按1保守计算；证明后才消除重复占用；部分成交/未知结果不释放占用 |
| E13 / ledger | 同 intent/source_key 相同 payload 调用100次；同键改价/量，及换 intent_id 复用 source_key | 仅一条业务意图、每腿至多一次 send；变更冲突，不能绕过幂等 |
| E14 / ledger | 分别在预留后、SUBMITTING 落盘后、fake 已接收但返回前、返回后结果落盘前注入崩溃，再恢复/重复提交 | 未尝试且记录完整者可重验后首次调用；所有已锁存 attempt 都不重发、未决预留保留 |
| E15 / ledger | fake 返回7、-1、None/bool，抛异常、丢回包，10秒无委托确认 | 7仅本地编号；-1本地拒绝；其他及超时待对账；恢复 send 次数不增长 |
| E16 / ledger | 订单量4，回放 partial(1)、filled(4)、迟到 pending(0)及重复；成交 T1=1,T2=3 再各重复 | 状态不回退，风险成交量4而非8；明细未齐前 trade_gap 阻断，齐后数量守恒 |
| E17 / ledger | T1 同键同内容重放，再同键改数量/价格；缺 TradeID/交易日、累计成交5但订单4 | 重复不累计；冲突/缺键/超量隔离并阻断；重启重放得到相同投影 |
| E18 / ledger | 先 trade 后 order、send 返回前全部成交回调；另一账户/合约/日/epoch 复用 local ID，伪 Memo | 唯一关联后只入账一次，FILLED不被稍后本地返回值覆盖；不明归属不认领、不污染其他桶、不触发撤单 |
| E19 / ledger | journal 写失败/损坏/锁超时、事件队列溢出；随后收到新 heartbeat | send=0；已有未决资源保留，阻断锁存不被心跳清掉；无内存兜底 |
| E20 / e2e | 六类动作与 GFD/FAK、SHFE/INE 今昨映射，注入只接受准确关键字的 FakeNative；再 FOK/market=True/未验证 hedge | 方法名/参数名/字符串枚举全相符，非法映射调用数0；浮点转换改变 tick 值则拒绝 |
| E21 / e2e | disabled 下端口属性访问即抛错；native 只有模式字符串、注入模拟完整证明或自报verified；observer 收 buy/enable=True | 端口解析/submit/cancel均0；模拟证据不能越界；新旧类禁用边界独立；observer仍hold |
| E22 / e2e | 自有当前会话未完单与 foreign/未知/旧会话/已填满单分别请求取消；重复/更换cancel_id及超时/崩溃后恢复；过期行情 | 仅模拟自有有效路由单一次 cancel，未知取消不重发；过期行情可撤；其他0；返回数字不算撤销终态 |
| E23 / e2e | 撤单在途发生部分成交，随后 CancelOrderData 无完整数量，最后最终委托和成交明细到达 | 不误判全部撤销；取消期间成交不丢不重；终态/快照屏障齐备才释放剩余资源 |
| E24 / e2e | F long3 反手 short2：只成交1、余单撤/拒/过期；另一轮全部平3但缺成交明细/新持仓，最后补齐 | 前者开仓数0；后者证据齐全并用新快照重新校验后才一次 SELL/OPEN2；预算/行情变化重新阻断 |
| E25 / e2e | own_journal 空但全账户模拟有 foreign挂单；断线后持仓/订单变化，再给仅自有回放 | 不宣称全账户零单或 EXECUTABLE；新增提交保持关闭；完整模拟屏障及一致对账后才可恢复 |
| E26 / e2e | 已预留后 quote 到期/授权到期/禁用/预算版本变化/重连/交易日变化；阻断 GUI/numpy/ctypes/pythongo 导入 | 最后调用前重验拦截，send=0；标准库+假native可加载，GUI/native均只在合法回调线程 |
| E27 / planning | 开仓现金上界缺失/未核实、仅有 Fee/Margin 金额、可用资金或总仓位预算不足；另仅平仓且费用/可平量充分 | 开仓有明确 blocker且send=0，不把金额当费率；单独平仓不要求开仓保证金费率 |
| E28 / ledger | 可用3，为同计划预留2，随后持有效ReservationReceipt再次校验和提交；再并发改变其他预留/快照 | 自身预留不会再次扣2导致误拒；外部变化仍重新核算，不能凭旧收据越过总预算 |

connection/planning/ledger/e2e 分别对应 N1/N2/N3/N4 的新增测试文件；E04 的传输时间边界由 N1 用相同夹具补测。既有观察回归覆盖加载、参数恢复、多合约隔离、未知持仓、HTTP限制、历史热更新和 hold-only，不能用删旧断言换通过。
后续工程 PowerShell 命令：`$infinyTests = @(rg --files tests -g 'test_infiny_*.py'); python -B -m pytest -q -p no:cacheprovider @infinyTests`。所有用例预先注入假 native，不启动生产服务/客户端；不以全仓库含 v2 的测试作为本次必要依赖。

**10. 决策 D：交付与验收互不冒充。** 报告固定记录 `engineering,platform,broker` 三栏，PASS 必须限定层级；本文只冻结规格，不将未来实现/测试表写成已经交付的软件。

| 层级 | 当日可完成/需后续的内容 | PASS / 缺口记录 |
| --- | --- | --- |
| 工程 | 四节点完整代码、纯规划、持久账本/对账、默认禁用映射、模拟六动作闭环及 E01–E28、既有观察回归 | 全部通过可 `engineering=PASS`；不需要也禁止为工程 PASS 下真实单；输出测试数、退出码、源码版本、模拟调用计数与未覆盖项 |
| 平台只读 | 用户实际 UI 加载/重载观察类、启动自身实例、关联当日 receipt、真实 tick、绑定账户和合约快照；核实线程/嵌入解释器及回调归属 | 本次 `platform=PARTIAL_UNVERIFIED`；无全量查询/完成屏障明确 `account_reconciliation=UNSUPPORTED`，不能以工程模拟补齐 |
| 柜台真实 | 在另有明确账户/合约/价格/手数/动作授权且平台门槛满足后，核实报单接受/拒绝、部分成交、撤单、重连恢复与对账 | 本次 `broker=NOT_RUN`；不借未入金或预期拒单试报，不以本地编号或函数存在宣称闭环完成 |

用户 UI 下一步仅为连接验收：在自身 `AKObserverBridge` 实例保持 `enable_trading=False`，按部署收据核对版本；若需重载由用户操作，启动后核对日志中的实例/会话/合约与17778当日 receipt，交易时段看真实 tick 增长及各快照年龄。未收到 receipt 时逐项定位加载、实例参数、启动日志和 HTTP 错误；禁止合成请求冒充客户端证据。
当日软件可以完成以上限定期货功能及故障模拟，原生全账户自动对账仍是已知平台缺口；原生验收项保留未验证不会使合格工程实现被迫下真实单。任何未来启用必须重新核实账户能力与具体授权，当前 observer 始终不可下单。
