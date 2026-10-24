# 无限易执行域共用契约（N1b 冻结）

权威来源：[infiny-execution-development.md](infiny-execution-development.md)（sha256 `08fa6c85435dab5834e051b98af0e323b6bb4b724383832a3ab9ab96c53353a9`，2026-09-14 冻结）。本文固化 `src/ak_trading/infiny_execution/contracts.py` 的精确类型、序列化、hash 与校验函数；N2（planning）、N3（ledger/reconcile）、N4（native/executor/simulation）必须使用本文类型，禁止各自发明 dict 协议或改名。冲突时以冻结规格为准并记录主会话。

本文不构成实盘授权。`mode=native` 的执行上下文本期不可构造；真实报单/撤单能力表固定 `unsupported`。

## 1. 标量表示与通用规则

| 概念 | Python 表示 | 序列化 | 硬规则 |
| --- | --- | --- | --- |
| 金额/价格/费率/TTL | `Decimal` | 规范十进制字符串（去尾零，无指数；`"3500"`、`"0.5"`） | 必须 `type(x) is Decimal` 且有限；正数场景禁止 0/负；`float`/`bool` 一律拒绝；精度上限 64 位数字/指数 128 |
| 时刻 | `datetime`（tz-aware） | ISO-8601 UTC，`2026-09-14T01:30:00.000000Z` | naive 拒绝；本地时区输入归一化为 UTC 存储；跨进程年龄比较只用 UTC 日历时刻 |
| 交易日 | `date` | `2026-09-14` | 独立校验，夜盘不得用 `TradingDay+UpdateTime` 伪造 |
| 计数/序号 | `int` | JSON number | `bool` 不冒充 int；负数拒绝（显式字段另有非负/正下界） |
| 集合 | 不可变 `tuple[...]` | JSON array | 禁止 list 入构造器 |
| 未知/缺失 | `None` | `null` | 禁止补 0、补空串、猜默认 |
| 枚举 | `str` Enum | 其 value 字符串 | 未知名拒绝，不猜 |
| 标识符 | `str` | 原样 | 须匹配 `[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}`；保留大小写与前导零 |

所有契约 dataclass 均 `@dataclass(frozen=True, slots=True, kw_only=True)`，继承 `ContractData`：构造时逐字段类型校验 + 语义 `_validate`，任何矛盾抛 `ContractError(ValueError)`，不静默 clamp/四舍五入。

## 2. 信封、序列化与规范 hash

```python
SCHEMA_VERSION = "infiny_execution_v1"
MAX_MESSAGE_BYTES = 1_048_576
MAX_NESTING = 32

def encode(value: ContractData) -> str: ...
def decode(payload: str | bytes, expected_type: type[T]) -> T: ...
def canonical_hash(value: ContractData) -> str: ...   # sha256(encode(value).encode("utf-8")).hexdigest()
```

- 信封固定 `{"schema_version": "infiny_execution_v1", "type": "<类名>", "data": {…}}`，键排序、紧凑分隔符、`ensure_ascii=False`、`allow_nan=False`。
- `decode` 拒绝：信封外键/缺键、`schema_version` 不符、`type` 与期望类不符、未知/缺失 data 字段、重复 JSON 键、NaN/Infinity、非字符串 Decimal、无时区 datetime、bool 冒充 int、非法枚举、超长/超深消息。不做 `eval`，不 import native。
- `canonical_hash` 是 `intent_hash`、`plan_id` 等身份字段的唯一来源；数值相等即字符串相等（`3500` 与 `3.5E3` 同 hash）。两类 hash 用途严格区分：`canonical_hash(dataclass)` 对任意契约对象做**全字段**绑定（含 `blockers`、`plan_id` 等输出字段），用于落盘完整性/重放一致性校验；`plan_id` 是 §4.4 定义的**内容寻址标识**（去自引原像，只绑执行语义输入）。N3 落盘校验必须用 `canonical_hash(Plan)`，不得拿 `plan_id` 充当防篡改证明。

## 3. 枚举与状态常量

```python
class ExecutionMode(str, Enum): DISABLED="disabled"; SIMULATION="simulation"; NATIVE="native"
class Source(str, Enum): SIMULATION="simulation"; PYTHONGO="pythongo"
class Action(str, Enum): OPEN_LONG; OPEN_SHORT; CLOSE_LONG; CLOSE_SHORT; REVERSE_TO_LONG; REVERSE_TO_SHORT
class ClosePolicy(str, Enum): AUTO_YD_FIRST; TODAY_ONLY; YESTERDAY_ONLY
class OrderType(str, Enum): GFD="GFD"; FAK="FAK"          # FOK 本期 unsupported
class Direction(str, Enum): BUY="BUY"; SELL="SELL"
class Offset(str, Enum): OPEN; CLOSE; CLOSE_TODAY; CLOSE_YESTERDAY
class Side(str, Enum): LONG="long"; SHORT="short"
class Bucket(str, Enum): TD="TD"; YD="YD"; ALL="ALL"
class Phase(str, Enum): CLOSE="CLOSE"; OPEN="OPEN"
class Completeness(str, Enum): UNKNOWN; PARTIAL; COMPLETE
class CoverageLevel(str, Enum): UNKNOWN; CALLBACKS_SINCE_START; OWN_JOURNAL; FULL_ACCOUNT
class MarketStatus(str, Enum): CONTINUOUS; AUCTION; CLOSED; UNKNOWN
class AssetClass(str, Enum): FUTURE; OPTION; INDEX; CONTINUOUS; COMBINATION; UNKNOWN
class ConnectionState(str, Enum): OFFLINE; SERVICE_ONLY; ROUNDTRIP; STALE
class ReadinessState(str, Enum): DISCONNECTED; CONNECTED; OBSERVING; PLAN_READY; EXECUTABLE; RECONCILE_REQUIRED
class OrderState(str, Enum): RESERVED; SUBMITTING; SUBMITTED_LOCAL; WORKING; PARTIAL; FILLED;
                            LOCAL_REJECTED; REJECTED; CANCELLED; PARTIAL_CANCELLED; SUBMIT_UNKNOWN; EXPIRED
class CancelState(str, Enum): NONE; REQUESTED; CONFIRMED; UNKNOWN
class ReservationState(str, Enum): HELD; REFLECTED; RELEASED
class ReconcileReason(str, Enum): SUBMIT_UNKNOWN; OWNERSHIP_CONFLICT; EVENT_GAP; LEDGER_FAILURE; LEDGER_CORRUPT;
                                  RESTART_UNRESOLVED; BASELINE_GAP; TRADE_GAP; QUANTITY_CONFLICT; CANCEL_UNKNOWN
class AuthorizationAction(str, Enum): OPEN_LONG; OPEN_SHORT; CLOSE_LONG; CLOSE_SHORT; REVERSE_TO_LONG; REVERSE_TO_SHORT; CANCEL_OWNED
class EventKind(str, Enum): ORDER="order"; TRADE="trade"; CANCEL="cancel"; ERROR="error"
class Ownership(str, Enum): OWNED; FOREIGN; UNKNOWN
class ResultCode(str, Enum): OK; IDEMPOTENT; BLOCKED; INTENT_CONFLICT; REVISION_CONFLICT; NOT_FOUND; PERSISTENCE_ERROR; QUARANTINED

TERMINAL_ORDER_STATES: frozenset[OrderState]   # FILLED, LOCAL_REJECTED, REJECTED, CANCELLED, PARTIAL_CANCELLED, EXPIRED
INFLIGHT_ORDER_STATES: frozenset[OrderState]   # SUBMITTING, SUBMITTED_LOCAL, WORKING, PARTIAL, SUBMIT_UNKNOWN
BLOCKER_CODES: frozenset[str]                  # Plan/Readiness blockers 的封闭白名单（见 contracts.py；新增须先过 N1b）
NATIVE_FULL_ACCOUNT_RECONCILIATION = "unsupported"
NATIVE_SUBMIT_ENABLED = False
NATIVE_CANCEL_OWNED_ENABLED = False
```

`BLOCKER_CODES` 为封闭集合（`ACCOUNT_MISMATCH`、`EVENT_GAP`、`QUOTE_STALE`、`INSUFFICIENT_POSITION`、`VOLUME_SPLIT_IMPOSSIBLE`、`CASH_BOUND_UNVERIFIED`、`TRADE_GAP`、`PROOF_REQUIRED` 等，全表见源码）。`Plan.blockers`/`Readiness.blockers` 只能取白名单值；动态时效码（`STALE_*`/`FUTURE_*`/`MISSING_*`）仅出现在 `ValidationResult.blockers`。

## 4. 数据契约字段表

除标注默认值外全部为必填关键字参数；`?` 表示可空（未知时必须 `None`，不得补 0）。

### 4.1 身份与时效

```python
@Binding: account_key: str; strategy_key: str; strategy_id: int; instance_id: str; session_epoch: str;
          trading_day: date; exchange: str; instrument_id: str; hedge_flag: str
@Symbol: exchange: str; instrument_id: str
@TimingPolicy: link_ttl: Decimal=15; packet_ttl: Decimal=10; quote_ttl: Decimal=3; account_ttl: Decimal=5;
               position_ttl: Decimal=5; contract_ttl: Decimal=3600; future_skew: Decimal=2   # 秒；age > ttl 失效
@ComponentTiming: sampled_at: datetime?; source_at: datetime?; received_at: datetime?;
                  snapshot_id: str?; source: Source; completeness: Completeness
@SnapshotTimes: sampled_at: datetime?; sent_at: datetime?; received_at: datetime?;
                quote/contract/account/positions: ComponentTiming   # 逐组件保存，心跳不续期 source_at
@SourceWatermark: source: Source; stream_id: str; value: str; as_of: datetime   # 源保证水位；接收序号不得冒充
@ClockReading: now: datetime; monotonic_ns: int; domain_id: str   # monotonic 不跨进程比较
```

### 4.2 快照族（§5 冻结字段）

```python
@Quote: binding: Binding; time: datetime?; last/bid1/ask1/upper_limit/lower_limit: Decimal?;
        bid_volume1/ask_volume1: int?; market_status: MarketStatus?      # time 必须等于 times.quote.source_at（真实 tick 时刻）
@VolumeLimit: evidence_id: str; min_volume: int; max_volume: int; volume_step: int; verified: bool
@Contract: exchange/instrument_id: str?; asset_class: AssetClass?; expiry: date?; price_tick/multiplier: Decimal?;
           min_limit_order_volume/max_limit_order_volume: int?; additional_volume_limits: tuple[VolumeLimit, ...]=()
@Account: binding: Binding; available: Decimal?; currency: str?      # currency 须 [A-Z]{3}
@PositionBucket: binding: Binding; side: Side; timing: ComponentTiming;
                 Position/PositionClose/FrozenClosing/YdPositionClose/YdFrozenClosing: int?   # SDK 原始键名；缺桶不按 0 推断
@ExecutionSnapshot: snapshot_id: str; binding: Binding; source: Source; times: SnapshotTimes;
                    quote: Quote?; contract: Contract?; account: Account?; positions: tuple[PositionBucket, ...];
                    coverage: Coverage; proof_id: str?; ledger_revision: int
```

`PositionBucket` 层级矛盾直接 `ContractError`（不 clamp）：`PositionClose<=Position`、`YdPositionClose<=PositionClose`、`FrozenClosing<=PositionClose`、`YdFrozenClosing<=FrozenClosing`、`YdFrozenClosing<=YdPositionClose`、今冻结不超今可平。可用量派生公式（N2 使用，原始字段保留）：`td_close=PositionClose-YdPositionClose`、`td_frozen=FrozenClosing-YdFrozenClosing`、`td_available=td_close-td_frozen`、`yd_available=YdPositionClose-YdFrozenClosing`。

`ExecutionSnapshot` 构造即校验：coverage 与 binding 同账户/会话/交易日/来源；quote/account/positions 内层 binding 与顶层一致；contract 交易所/合约与 binding 一致或 None；全部组件 timing.source 与快照 source 一致；`(side, hedge_flag)` 桶唯一；quote.time 与 times.quote.source_at 相同（心跳不能替行情续期）。

### 4.3 覆盖与对账证明

```python
@Coverage: level: CoverageLevel; account_key: str; strategy_key: str?; session_epoch: str; trading_day: date;
           source: Source; as_of: datetime?; complete_through: SourceWatermark?; completion_evidence_id: str?;
           own_active_count: int?; account_active_count: int?     # own=0 永不推出 account=0
@OrderInventoryItem: account_key; trading_day: date; exchange; instrument_id; hedge_flag: str;
                     direction: Direction; offset: Offset; order_id: int?; order_sys_id: str?;
                     total_volume/traded_volume/cancel_volume: int; active: bool      # traded+cancel<=total
@TradeInventoryItem: account_key; trading_day: date; exchange; instrument_id; hedge_flag: str;
                     trade_id: str; order_sys_id: str?; order_id: int?; direction: Direction;
                     offset: Offset; price: Decimal; volume: int
@ReconciliationProof: proof_id: str; source: Source; account_key: str; trading_day: date; session_epoch: str;
                      as_of: datetime; order_inventory: tuple[OrderInventoryItem, ...]?;
                      trade_inventory: tuple[TradeInventoryItem, ...]?; position_snapshot_id: str?;
                      complete_through: SourceWatermark?; exclusive_writer_verified: bool; verified: bool;
                      covered_event_ids: tuple[str, ...]=(); covered_leg_ids: tuple[str, ...]=()
```

冻结约束（构造期强制）：`source=pythongo` 的 proof 必须 `verified=false` 且 `complete_through=None`（原生无完整 query 屏障，本期不可用布尔解锁）；`verified=true` 仅 simulation 且要求完整三清单 + 独占写手证据 + 水位；watermark.source 必须与 proof/coverage 同源（simulation 水位不得流入 native 证据）；`FULL_ACCOUNT` 覆盖仅 simulation 屏障可得；非 FULL_ACCOUNT 覆盖禁止携带 `account_active_count`。

### 4.4 意图、规则与计划（N2 输入/输出）

```python
@Prices: open: Decimal?; close: Decimal?      # 正数；按动作必填见下
@OrderIntent: intent_id: str; source_key: str; binding: Binding; action: Action; volume: int;
              prices: Prices; close_policy: ClosePolicy; order_type: OrderType;
              created_at: datetime; expires_at: datetime; schema_version: str=SCHEMA_VERSION; market: bool=False
@CashBoundSource: source: Source; evidence_id: str; verified: bool    # verified 仅 simulation；金额(Fee/Margin)不是费率
@RiskPolicy: policy_id: str; valid_until: datetime; max_leg_qty/max_gross_position/max_inflight_legs: int?;
             max_cash_reserve/cash_per_open_lot/fee_per_close_lot: Decimal?;
             cash_bound_source: CashBoundSource?; timing: TimingPolicy=TimingPolicy()
@Leg: leg_id: str; index: int; phase: Phase; depends_on: tuple[str, ...]; direction: Direction; offset: Offset;
      volume: int; price: Decimal; position_bucket: Bucket?; cash_reserve: Decimal      # cash_reserve>=0
@Plan: plan_id: str(64hex); intent_hash: str(64hex); snapshot_id: str; ledger_revision: int; policy_id: str;
       expires_at: datetime; legs: tuple[Leg, ...]; blockers: tuple[str, ...]           # blockers ⊆ BLOCKER_CODES
```

- `OrderIntent`：OPEN 类动作要求 `prices.open` 非 None，CLOSE 类要求 `prices.close`，REVERSE 两者都要；`volume>=1` 非 bool；`expires_at>created_at`；`market=True` 一律拒绝（本期仅限价）。
- `Plan`：`blockers` 非空的拒绝计划允许 `legs=()`（安全表示"无可执行腿"，不得为被拒输入伪造 Leg）；`blockers=()` 的可执行计划必须 `legs` 非空。
- `Leg` 方向/开平白名单：`(BUY,OPEN) (SELL,OPEN) (SELL,CLOSE*) (BUY,CLOSE*)`；OPEN 腿 `position_bucket=None`，CLOSE 腿必须有桶；`depends_on` 只能引用更早 index 的腿（Plan 校验存在性）。`CLOSE_YESTERDAY` 仅 SHFE/INE 由 N2/N4 依据合约校验。
- `plan_id`/`leg_id` 规范算法（N2 必须实现并测试）：
  - `intent_hash = canonical_hash(OrderIntent)`。
  - `plan_id = sha256("infiny_execution_plan_v1|" + intent_hash + "|" + snapshot_id + "|" + str(ledger_revision) + "|" + policy_id + "|" + expires_at串 + "|" + ";".join(leg 规范串))`，其中 `expires_at串` 为 UTC ISO-8601（`...Z`）规范串；leg 规范串 = `leg_id + "," + phase.value + "," + direction.value + "," + offset.value + "," + str(volume) + "," + 价格规范串 + "," + (position_bucket.value 或 "-") + "," + 现金预留规范串 + "," + ("+".join(depends_on) 或 "-")`（价格/现金预留用与 encode 相同的 Decimal 规范串；依赖按 leg_id 字典序）。
  - `leg_id = f"{intent_id}:{phase.value}:{position_bucket.value if position_bucket else '-'}:{split_index}"`，`split_index` 为同 (phase,bucket) 内 0 起拆分序号。
  - 内容寻址边界：`plan_id` 是内容寻址标识，原像去自引（不含 plan_id 自身），绑定全部执行语义字段（意图、快照 id、账本版本、规则、到期、每腿方向/开平/量/价/桶/现金/依赖）。它**不构成防篡改证明**——篡改检测依赖 N3 账本落盘的 `canonical_hash(Plan)`（绑定含 `blockers` 在内的全部字段）与独占写手证据；`snapshot_id`/`policy_id` 为生产者签发的标识符，其内容绑定由快照/规则构造期校验保证。

### 4.5 资源预留视图（N3 输入/输出）

```python
@ReservationKey: account_key: str; exchange: str; instrument_id: str; hedge_flag: str; side: Side; bucket: Bucket
@Reservation: resource_key: ReservationKey; leg_id: str; state: ReservationState; volume: int;
              cash_reserve: Decimal; not_proven_reflected: int=0; reflected_evidence_id: str?
@ReservationReceipt: plan_id: str(64hex); reservation_ids: tuple[str, ...]; revision: int
@ReconcileState: required: bool=False; reasons: tuple[ReconcileReason, ...]=(); since: datetime?=None; evidence_id: str?=None
@ReservationView: account_key: str; snapshot_id: str?; ledger_revision: int; reservations: tuple[Reservation, ...];
                  cash_reserved: Decimal; gross_position: int; inflight_legs: int; reconcile: ReconcileState
```

锁存规则以类型表达：`required=True` 必须携带 reasons 与 since；`required=False` 不得残留任何 reason/evidence。`REFLECTED` 预留必须携带屏障证据 id；`not_proven_reflected <= volume`（未证明吸收量不得超过预留量）。`RELEASED` 预留保留历史 `volume/cash_reserve` 仅供审计——`ReservationView` 的 `cash_reserved/gross_position/inflight_legs` 风险占用必须排除全部 `RELEASED` 记录；同一 `leg_id` 至多持有一个活跃（非 RELEASED）预留。无吸收证据时本地余量按 `max(0, sdk_available - not_proven_reflected)` 保守扣减。收据认领：同一 `ReservationReceipt` 重验不重复扣同一预留（E28）。

### 4.6 回报事件（N3 ingest 输入）

```python
@OwnershipEvidence: journal_leg_id: str; matched_alias: str("local"|"exchange");
                    callback_account_verified: bool; strategy_scope_verified: bool; evidence_id: str
@ExecutionEvent: event_id: str; kind: EventKind; received_at: datetime; callback_session_epoch: str;
                 account_key: str?; ownership: Ownership; exchange: str?; instrument_id: str?; hedge_flag: str?;
                 order_id: int?; order_sys_id: str?; front_id: int?; session_id: int?; memo: str?;
                 trade_id: str?; order_time: datetime?; trade_time: datetime?; trading_day: date?;
                 status: OrderState?; total_volume: int?; traded_volume: int?; cancel_volume: int?;
                 price: Decimal?; volume: int?; error_code: str?; direction: Direction?; offset: Offset?;
                 ownership_evidence: OwnershipEvidence?
```

构造规则（保存门槛，宽松以承载原始残缺回报）：`OrderSysID` 去全部空白（保留大小写/前导零），`TradeID`/`Memo`/`ErrorCode` 仅 trim；各文本字段（含 `account_key`）非 None 时必须通过 §1 白名单标识格式（任意 memo/原始 errMsg 不入任何字段）；数量字段非 None 时必须是 int（bool 拒绝）——**UNKNOWN/FOREIGN 允许非负含零**（`(0,0,0,0)` 原样保留为隔离前证据，不得变 None 或被下界拒绝），`price` 有限正 Decimal。**UNKNOWN/FOREIGN 事件不做业务完整性强制**——数量不守恒（如 total=4/traded=3/cancel=2）、TRADE 缺业务键、ORDER/CANCEL 缺别名、ERROR 缺 `error_code` 均以 typed 形式原样保留，供 N3 持久隔离（矛盾数量锁存 `QUANTITY_CONFLICT`，缺键待对账），不得在解析阶段因构造拒绝而丢弃或编造 None/错误码。**OWNED 事件完整强制**：归属证据（账户+策略范围验证，Memo 不算）；身份键 `account_key/trading_day/exchange/instrument_id` 必须全部非 None（TRADE 另需 `trade_id`，五键均须标识格式）；数量守恒且 `total_volume/volume >= 1`；TRADE 五业务键（`trade_id/volume/price/direction/offset`）；ORDER/CANCEL 别名；ERROR `error_code`。入账/归属门槛由 N3 强制：UNKNOWN 投影先隔离并锁存 `QUANTITY_CONFLICT`，不入账、不认领、不污染其他桶；禁止补造 StrategyID/InvestorID。身份两层别名（N3 对账键）：本地 `(account_key, session_epoch, order_id)`，柜台 `(account_key, trading_day, exchange, order_sys_id)`；成交唯一键 `(account_key, trading_day, exchange, instrument_id, trade_id)`。

### 4.7 授权、就绪与执行上下文

```python
@PriceBound: symbol: Symbol; lower: Decimal; upper: Decimal
@Authorization: authorization_id: str; mode: ExecutionMode; account_key: str; strategy_key: str;
                session_epoch: str; symbols: tuple[Symbol, ...]; actions: tuple[AuthorizationAction, ...];
                max_volume: int; price_bounds: tuple[PriceBound, ...]; expires_at: datetime
@Readiness: connection: ConnectionState; state: ReadinessState; can_plan/can_submit/can_cancel_owned: bool;
            blockers: tuple[str, ...]; mode: ExecutionMode; coverage: Coverage; evidence_id: str?
@RouteOwnership: route: str("submit"|"cancel"); evidence_id: str; verified: bool   # 本期仅 cancel 可 verified
@ExecutionContext: mode: ExecutionMode; binding: Binding; snapshot: ExecutionSnapshot; proof: ReconciliationProof?;
                   policy: RiskPolicy; authorization: Authorization?; reservation_receipt: ReservationReceipt?;
                   owner_thread_id: int; current_thread_id: int; clock: ClockReading; ledger_revision: int;
                   route_ownership: RouteOwnership?; readiness: Readiness; reconcile: ReconcileState;
                   intent: OrderIntent? = None; reservation_view: ReservationView? = None
@ValidationResult: blockers: tuple[str, ...]=()    # .ok 属性 == not blockers
```

构造期门槛（不可被 payload 绕过）：

- `mode=native` 的 `ExecutionContext`/`Readiness`/`Authorization` 一律 `ContractError`——本期原生门槛未满足，模式字符串不能解锁真实执行；`disabled` 是默认态。
- `can_submit=True` 仅当 `mode=simulation` ∧ `state=EXECUTABLE` ∧ `blockers=()` ∧ `coverage.level=FULL_ACCOUNT`；`RECONCILE_REQUIRED` 状态强制 `can_submit=False`。
- `ExecutionContext`：`readiness.mode` 必须同 mode；`proof.source` 必须等于 `snapshot.source` 且同账户（simulation 证明不得流入 native）；锁存 `reconcile.required=True` 时 readiness 必须呈现 `RECONCILE_REQUIRED` 且不可提交。
- `intent` 非 None 时其 `binding` 必须与执行 scope 完全相等（账户/策略/实例/会话/交易日/交易所/合约/hedge，E05：换账户 B 的一致 context 无法搭载 A 的意图/计划）；`reservation_view` 非 None 时必须同账户、同 `ledger_revision`，且 `snapshot_id` 非 None 时与 `snapshot.snapshot_id` 相等（E28 复检资源用）。两者默认 None：disabled/诊断合法；提交与 `validate_plan` 复验必须显式提供（`require_attempt=True` 时缺失分别阻断 `INTENT_REQUIRED`/`RESERVATION_VIEW_REQUIRED`）。
- `ReservationReceipt` 自报不构成认领证据——收据有效性由 N3 在 journal 内核对（计划/预留/版本真实存在且未重复扣减），payload 不能凭收据越过总预算。
- 授权缺省 `None` 合法（validate 阶段报 `AUTHORIZATION_REQUIRED`）；不继承“可开发”许可。

## 5. 纯校验函数（contracts.py 已实现）

```python
def validate_timing(timing: ComponentTiming, now: datetime, ttl: Decimal, *,
                    require_source: bool = False, policy: TimingPolicy = TimingPolicy()) -> ValidationResult
def validate_snapshot_timing(snapshot: ExecutionSnapshot, now: datetime,
                             policy: TimingPolicy = TimingPolicy()) -> ValidationResult
def validate_context(context: ExecutionContext, now: datetime, *,
                     action: AuthorizationAction | None = None,
                     require_attempt: bool = False) -> ValidationResult
```

- `validate_timing`：`age > ttl` 失效（毫秒精确）；缺 sampled_at/received_at（或 require_source 时 source_at）→ `MISSING_*`；超前超 `future_skew` → `FUTURE_*`；`source_at > sampled_at+skew`、`sampled_at > received_at+skew` 为矛盾。行情组件必须 `require_source=True`（真实 tick 时刻，心跳不续期）。
- `validate_snapshot_timing`：对 quote/account/positions/contract 组件 + 包级 sampled/received（packet_ttl）逐一判定，blocker 带组件前缀（`QUOTE_STALE_SAMPLED_AT` 等）。
- `validate_context` 提交/规划路径（`action` 非 CANCEL_OWNED）：`MODE_DISABLED`、`RECONCILE_REQUIRED`（含 reservation_view.reconcile 锁存）、`THREAD_INVALID`、`POLICY_EXPIRED`、`LEDGER_REVISION_MISMATCH`、快照时效透传、授权检查（`AUTHORIZATION_REQUIRED/EXPIRED`、`ACCOUNT_MISMATCH`/`SESSION_CHANGED`/`OWNERSHIP_CONFLICT`/`INSTRUMENT_MISMATCH`/`AUTHORIZATION_ACTION`）、`PROOF_REQUIRED`、simulation 未验证证明（`ACCOUNT_RECONCILIATION_UNSUPPORTED`）、`readiness.can_submit` 阻断。`require_attempt=True`（提交或 `validate_plan` 复验）另查：`INTENT_REQUIRED`（含 intent 过期 → `EXPIRED`、binding 不符 → `OWNERSHIP_CONFLICT`）、`RESERVATION_VIEW_REQUIRED`。预留收据由 N3 `Executor.submit_once` 在提交路径以 journal 为准单独校验。
- `validate_context(action=CANCEL_OWNED)` **独立判定**（E22）：不依赖 quote/快照 TTL、不要求 full_account proof、不受 `can_submit=False` 影响；必须满足：非 disabled（native 本期完全禁）、owner thread == current thread、授权有效且含 `CANCEL_OWNED` 动作与 scope/会话匹配、`readiness.can_cancel_owned=True`（否则 `CANCEL_NOT_AVAILABLE`）、`route_ownership` 为已验证 cancel 路由（否则 `CANCEL_ROUTE_UNVERIFIED`）、journal 持久能力可用（reconcile 锁存原因为 `LEDGER_FAILURE/LEDGER_CORRUPT` → `LEDGER_FAILURE`）、账本版本一致。非 LEDGER 类锁存（如 `EVENT_GAP`）不自动阻断自有撤单——归属/唯一性由 N3 journal 独立校验，锁存不被清除也不隐式放行入账。

## 6. 下游必须实现的接口（contracts.py 不实现）

### N2 `E/planning.py`

```python
def plan(intent: OrderIntent, snapshot: ExecutionSnapshot, reservation_view: ReservationView,
         policy: RiskPolicy, now: datetime) -> Plan          # 纯函数：无 I/O、不预留、不触 sender
def validate_plan(plan: Plan, latest_context: ExecutionContext, now: datetime) -> ValidationResult
```

`validate_plan` 复验绑定（`require_attempt=True` 路径）：`canonical_hash(latest_context.intent) == plan.intent_hash`（意图一致，换 intent_id/binding/payload 即拒，E05/E13）；`plan.policy_id == latest_context.policy.policy_id`；`plan.expires_at <= min(intent.expires_at, policy.valid_until)`；快照与意图 binding 逐项相等。**revision 层级语义（E28，取代旧等式）**：`Plan.ledger_revision` 冻结的是 `reserve` 事务**前**的账本版本（pre-transaction rev，参与 plan_id 原像）；`ReservationReceipt.revision` 是事务**后**版本（post rev），可以 `<=` 当前 trusted `ReservationView.ledger_revision`（view 可能已随后续事务推进）；复验时最新的 context/snapshot/view revision 必须一致（ExecutionContext 构造期强制 view==context，`validate_context` 以 `LEDGER_REVISION_MISMATCH` 核 snapshot==context）。**不得以 `plan.ledger_revision == context.ledger_revision` 作复验等式**——资源是否可认领由 N3 在 journal 内实际核对 receipt 与 Plan 来源后重算，payload 自报的 receipt/revision 无效。普通重验允许使用更新的合法快照（同 binding/会话/交易日，构造期保证）重验时效与价格边界，**不改变原 Plan 及其冻结价格/数量/拓扑**（plan_id/snapshot_id/pre rev 保持不变）；反手后腿等显式 refresh 场景必须产出新 Plan：新 `snapshot_id` 取刷新快照、冻结新 pre-rev、按最新持仓/行情重算腿（N2 生成，旧 Plan 不被改写或替代已尝试腿）。到期取 intent、policy、各必需组件时效的最早期限。价格须有限正数、`price % price_tick == 0`、上下限内、连续交易、非涨跌停/倒挂/空档/超 10 tick 价差；超 max 确定性拆分（如 11=min5 → 5+3+3），无合法尾单整计划失败；`unsupported` 明确返回（主连/期权/组合/指数/到期/猜测合约、DCE 严格昨仓、FOK）。

### N3 `E/ledger.py` / `E/reconcile.py`

```python
class Ledger:
    def __init__(self, path) -> None: ...                    # SQLite, synchronous=FULL；失败关闭提交，禁内存兜底
    def register(self, intent: OrderIntent) -> ...           # 同 payload 幂等返回原记录；同键不同 payload -> INTENT_CONFLICT
    def reserve(self, plan: Plan, expected_revision: int) -> ReservationReceipt   # BEGIN IMMEDIATE 原子全腿预留
    def begin_attempt(self, leg_id: str, context: ExecutionContext) -> ...        # 持久 SUBMITTING/attempt_no=1 后才可调端口
    def record_submit_result(...) -> ...; def ingest_event(self, event: ExecutionEvent) -> ...
    def recover(self, proof: ReconciliationProof, events: Sequence[ExecutionEvent]) -> ...
    def view(self, ...) -> ReservationView

def normalize_event(raw: Mapping, callback_context: Mapping) -> ExecutionEvent     # 白名单原始键 -> ExecutionEvent
class Reconciler:
    def ingest(self, event: ExecutionEvent) -> ...           # 去重/归属/状态机；与 events 投影同事务
```

状态机严格按 §3 常量：终态不回退、迟到 pending 不复活、`reported_filled=max(历史 TradedVolume)` 与 `trade_filled=sum(唯一 TradeID)` 取 max、`trade_gap` 阻断、`EXPIRED` 仅限未尝试腿、`cancel_state` 与订单状态独立。

### N4 `E/native.py` / `E/executor.py` / `E/simulation.py` / `I/AKExecutionBridge.py`

```python
class NativePort:
    def send_order(self, request: Mapping) -> int: ...       # 非负 int=本地编号；-1=LOCAL_REJECTED；其余 SUBMIT_UNKNOWN
    def cancel_order(self, order_id: int) -> int: ...        # 返回值不作最终撤单成功证据
def build_native_request(leg: Leg, binding: Binding, investor_handle: object, *,
                         order_type: OrderType, price_tick: Decimal,
                         verified_hedge_flags: tuple[str, ...]) -> Mapping   # 纯函数
class Executor:
    def submit_once(self, intent_id: str, leg_id: str, plan_id: str, context: ExecutionContext) -> ...
def request_cancel(cancel_id: str, intent_id: str, leg_id: str, context: ExecutionContext) -> ...
```

`build_native_request` 参数来源与约束：`leg/binding` 取自原意图与已冻结 Plan（同 binding）；`investor_handle` 仅为已绑定客户端内存中的 Investor 句柄对象本身，**不得借句柄夹带其他数据**；`order_type` 来自 `OrderIntent.order_type`（GFD/FAK，FOK unsupported）；`price_tick` 来自与意图逐项匹配的 `ExecutionSnapshot.contract.price_tick`（非 None，`leg.price % price_tick == 0` 在 mapper 内复验，浮点转换改变 tick 值即拒绝）；`verified_hedge_flags` 为装配器已验证的 hedge 白名单（非空 tuple，`binding.hedge_flag` 必须在其中，不猜默认值）。纯函数：无 I/O、不触端口、不读环境；输出 Mapping 仅含 §8 映射表冻结键。

映射冻结值：BUY=`'0'` SELL=`'1'`；OPEN=`'0'` CLOSE/CLOSE_YESTERDAY=`'1'` CLOSE_TODAY=`'3'`（CLOSE_YESTERDAY 仅 SHFE/INE）；GFD=`'0'` FAK=`'1'`；`Market=False`；`Memo` 只放 leg_id；Investor 只取已绑定客户端内存句柄。disabled 在解析 native 对象前拒绝；native 拒绝 simulation 来源快照/证明/授权与自报 verified；模拟 PASS 不改能力表。`request_cancel` 走 `validate_context(action=CANCEL_OWNED)` 独立判定（见 §5）：过期行情、`can_submit=False`、非 full_account proof 均不阻断；journal 归属/当前撤单路由/线程/授权必须有效；本期 native 撤单完全禁用，模拟器覆盖此分支。

## 7. 变更记录

- r1（初版冻结）：§1–§6 全量类型、信封/hash、门槛；62 项测试。
- r2（联调修复）：`Plan` 拒绝计划允许 `legs=()`；`Reservation` 删恒真断言、`not_proven_reflected<=volume`、每腿单一活跃预留；`plan_id` 原像补绑 `depends_on/cash_reserve/expires_at` 并区分 `canonical_hash` 全字段绑定与内容寻址标识。
- r3（N2/N3 集成缺口）：`ExecutionContext` 增 `intent`/`reservation_view`（默认 None，提交/复验缺失阻断，binding/账户/快照/版本构造期精确核对，E05/E28）；`ExecutionEvent` 未知事件允许缺 `exchange/instrument_id/hedge_flag`、TRADE 业务键与 ORDER/CANCEL 别名（typed 隔离保存，入账门槛仍由 N3 完整强制，E17/E18）；`validate_context` 增 `require_attempt`，`CANCEL_OWNED` 独立判定（不依赖 quote TTL/full account proof/can_submit；保留 simulation-only、路由/线程/授权/持久能力，E22）；新增白名单码 `INTENT_REQUIRED/RESERVATION_VIEW_REQUIRED/CANCEL_NOT_AVAILABLE`。
- r4：UNKNOWN/FOREIGN 事件不再构造期强制数量守恒与 ERROR `error_code`——数量矛盾（total=4/traded=3/cancel=2）与缺 code 的异常回报 typed roundtrip 原样隔离（类型/finite/安全文本仍严格），N3 投影锁存 `QUANTITY_CONFLICT` 不入账；OWNED 事件完整校验保持不变（归属证据、守恒、TRADE 五键、别名、error_code）。68 项测试。
- r5（N2 复验 revision 语义 + N4 mapper 签名，仅文档）：删除 §6 旧等式 `plan.ledger_revision == context.ledger_revision`——Plan 冻结 pre-transaction rev，`ReservationReceipt.revision` 为 post rev 且可 `<=` 当前 trusted view rev，最新 context/snapshot/view rev 必须一致；资源认领由 N3 在 journal 实际核对 receipt/Plan 来源后重算，payload 自报无效（E28）。普通重验可用更新的合法快照重验时效/边界而不改原 Plan 冻结价量/拓扑；反手 refresh 显式产新 Plan（新 snapshot_id + 新 pre rev）。`build_native_request` 冻结为 `(leg, binding, investor_handle, *, order_type, price_tick, verified_hedge_flags) -> Mapping`，句柄不夹带数据，参数来自原 intent+匹配 contract+hedge 证据。代码与公共字段无变更。
- r5.1（缺陷修复）：`validate_snapshot_timing` 组件级 `validate_timing` 漏传 `policy`，自定义 `future_skew` 与包级/`validate_context` 不一致；已透传并加回归。
- r6（硬审 r1 F06/F05）：OWNED 事件身份键 `account_key/trading_day/exchange/instrument_id` 全部强制非 None（TRADE 另需 `trade_id`），`account_key` 纳入 identifier 白名单（含空格账号标签拒绝）；UNKNOWN/FOREIGN 数量字段非 None 时允许非负含零（`(0,0,0,0)` 原样隔离保存），OWNED 数量保持 `>=1` 与守恒——两域语义分开，互不放松。contracts 71 项 + N2 planning 联测 310 passed。
- r7（NE1-C1，本版）：新增 `infiny_execution_v2` 与 `infiny_client_execution_transport_v1` 命名空间（§8）；v1 wire 字节不变、四执行域测试文件 543 项零回归；contracts.py 仅增 `V2_SCHEMA_VERSION`、schema 感知 encode/decode 与公开校验助手别名。新文件 native_contracts.py/native_evidence.py 与 TC 19 项 + SYNTHETIC fixture；能力来源、CAS/dispatch 事务边界与 H 映射冻结于 §8.5–§8.7；contract-lock 见 `data/maintenance_artifacts/infiny-native-execution/c1/contract-lock.json`。
- r8（C1.1，NE1-ARB-C1.1-001）：transport 升级 `infiny_client_execution_transport_v1_1`；六响应 DTO+TransportErrorResponse+错误码表+GET-header 映射+CallCount/ExecutionCounters+SubmitProjection+request_fingerprint；EventCursor/AckWatermark 连续游标、RawCallbackEnvelope 流序号、PollRequest 快照按值与 received_at null、OperatorRegistrationManifest；冻结 INITIAL_SERVICE_EPOCH/事件流/限流/配对常量。TC 28 项 + fixture 16 黄金向量；修订 lock 见 `data/maintenance_artifacts/infiny-native-execution/c1.1-r1/contract-lock.json`（绑定父 lock 与裁定原文）。
- r9（C1.2，NE1-ARB-C1.2-OVF06-001）：`DISPATCH_TTL_MS` 500→1500 固定最大值；§8.9 冻结 DTTL-01..08、offer/command 截止继承计算器与 admission 校验器、N3 单事务 seam 签名（`expected_context`）；fixture 增满窗命令 case 与 dispatch_window 边界向量；TC 32 项。lock 见 `data/maintenance_artifacts/infiny-native-execution/c1.2-r1/contract-lock.json`（parent=C1.1 `bcacb46d…`，绑定本裁定实际哈希）。
- r10（C1.3，BUNDLED-ARB-20260915-R2 Q1/Q2）：§8.10 冻结 RD-01..RD-05（`EvidenceDeadlineResult`/`EvidenceRegistry.applicable_deadline` 接口、失败语义表与两个新诊断码、D_registry 公式与黄金向量、N3/N4 消费规则）与 CANCEL-D-01 适用表（CANCEL 不含 policy.valid_until 与 SUBMIT 专属快照/证据新鲜度）；fixture 增 registry_deadlines/cancel_d01 向量；TC 35 项。lock 见 `data/maintenance_artifacts/infiny-native-execution/c1.3-r1/contract-lock.json`（parent=C1.2 `ec4569e7…`，绑定本裁定实际哈希）。
- r11（C1.4，NE1-ARB-C1.4-N4R3-F2-001）：§8.11 冻结 OFD-01..07 与 Option A 持久首 offer 绝对截止；新 `OfferDeadlineRecord`（`infiny_execution_local_v1` 本地命名空间，transport/v1/v2 解码器全部拒绝，transport 编解码包装器收紧为 transport 专用）与 `encode_local/decode_local`；`DispatchJournal/Ledger.record_offer/get_offer` seam 冻结（C1.2/C1.3 签名零变更）；四诊断码 `OFFER_DEADLINE_*`；fixture 增 offer_deadline 向量（R3-P14+零漂移）与本地记录 case；TC 39 项。lock 见 `data/maintenance_artifacts/infiny-native-execution/c1.4-r1/contract-lock.json`（parent=C1.3 `5e73632e…`，绑定本裁定实际哈希）。
- r11.1（c1.4-r2，独立审 F1 修复 + F2 优先级闭合）：编码包装器恢复按类型 schema 命名空间分发——v2 记录（ControlState/证据/manifest）既有编码字节路径完全恢复（N3 control journal 回归），journal-local 命名空间仍专用 `encode_local/decode_local`（通用包装器显式拒绝），跨命名空间隔离由引擎双侧校验保证；多重无效输入优先级冻结入 fixture `vectors.offer_deadline.precedence` 并由 TC 执行（结构类型错误→非正 lifetime→超预算→kind/撤销身份），消除 §8.11 虚声明。TC 40 项。lock 见 `data/maintenance_artifacts/infiny-native-execution/c1.4-r2/contract-lock.json`（predecessor=c1.4-r1）。
- r12（C1.5，NE1-ARB-C1.5-OVF06-ANCHOR-001）：§8.12 冻结 option b 准备事务合并——`DispatchJournal/Ledger.prepare_native_submission` 返回 `(Plan, ExecutionContext)` 本地元组 seam、AP-01..04 事务契约、公式零变更重申（`E_first_SUBMIT=min(D_S,f+tau,s+tau)`）、写组 8→3 计数；fixture 增 `vectors.preparation_seam`（签名/公式/计数/ANC-T01 分析用例 1154ms→1500 非 2654）；TC 42 项；无 production 常量/类型/编解码变更。lock 见 `data/maintenance_artifacts/infiny-native-execution/c1.5-r1/contract-lock.json`（parent=C1.4-r2 `71d9e1ba…`，绑定本裁定实际哈希）。

## 8. NE1-C1：v2 核心与客户端传输契约（冻结）

权威来源：[infiny-native-execution-plan.md](infiny-native-execution-plan.md) §2.3/§3.3/§5/§7。实现：`src/ak_trading/infiny_execution/native_contracts.py`（v2 核心 + transport DTO）与 `src/ak_trading/infiny_execution/native_evidence.py`（scope 证据与能力来源）。本节类型由 N1b 独占维护；N2/N3/N4/客户端直接消费，不得各自发明 dict。

### 8.1 三个互斥 schema 命名空间

| 命名空间 | 常量 | 内容 |
| --- | --- | --- |
| v1 核心 | `infiny_execution_v1` | §1–§7 全部既有类型，wire 字节不变，仅离线可读 |
| v2 核心 | `infiny_execution_v2`（`contracts.V2_SCHEMA_VERSION`） | ControlState、V2Readiness、NativeAuthorization、native_evidence 全部证据类型 |
| 客户端传输 | `infiny_client_execution_transport_v1_1`（`native_contracts.TRANSPORT_SCHEMA_VERSION`，C1.1 修订） | §3.3 全部 DTO 与 wire 消息、响应族与传输错误 |

引擎复用 `contracts.encode/decode/canonical_hash`（新增可选 `schema_version` 参数，v1 默认行为不变）：每个类型经 `ContractData.schema` 类属性绑定命名空间，**跨命名空间 encode/decode 一律 ContractError**（v1 类型不能乘 v2/transport 信封，反之亦然；v1 解码无法带入任何 v2 能力）。既有标量规则（Decimal 规范串、UTC datetime、bool 不冒充 int、未知字段/重复键/NaN 拒绝、超深超长拒绝）原样适用。

### 8.2 冻结常量（transport）

```python
MAX_REQUEST_BYTES = 262144        # 256 KiB
MAX_RESPONSE_BYTES = 65536        # 64 KiB
MAX_NESTING = 32
MAX_EVENTS_PER_BATCH = 128
MAX_RAW_FIELDS_PER_EVENT = 64
DISPATCH_TTL_MS = 1500            # C1.2（NE1-ARB-C1.2-OVF06-001）：固定最大值，无任何 override
POLL_ARMED_MIN_INTERVAL_MS = 250  # armed poll 最快间隔
POLL_DISABLED_INTERVAL_MS = 5000  # disabled poll 间隔
# C1.1（NE1-ARB-C1.1-001）追加冻结：
HTTP_REQUEST_FINGERPRINT_VERSION = "infiny_http_request_fingerprint_v1"
INITIAL_SERVICE_EPOCH = 1
EVENT_SEQUENCE_START = 1
STATUS_MAX_HEADER_BYTES = 8192
MAX_HTTP_WORKERS = 8
MAX_ACCOUNT_ACTORS = 8
MAX_ARMED_NATIVE_ACCOUNTS = 1
EXECUTION_TOKEN_HEADER = "X-Execution-Token"
PAIRING_TOKEN_BYTES = 32          # secrets.token_hex(32) → 64 位小写 hex
PAIRING_TOKEN_HEX_LENGTH = 64
```

### 8.3 v2 核心类型

```python
class ExecutionScope(str, Enum): NONE; SIMULATION_FULL_ACCOUNT; NATIVE_PILOT
class Origin(str, Enum): PRODUCTION; SYNTHETIC

@ControlState: control_revision: int; binding: Binding; mode: ExecutionMode; policy_id: str;
               authorization_id: str?; native_permit_id: str?; params_sha256: str(64hex);
               generation: str; updated_at: datetime; disabled_reason: str?
@V2Readiness: connection: ConnectionState; state: ReadinessState; can_plan/can_submit/can_cancel_owned: bool;
              blockers: tuple[str, ...]; mode: ExecutionMode; coverage: Coverage?;
              evidence_id: str?; execution_scope: ExecutionScope; native_permit_id: str? = None
@NativeAuthorization: authorization_id: str; binding: Binding; symbols: tuple[Symbol, ...];
                      actions: tuple[AuthorizationAction, ...]; max_send_attempts: int(>=1);
                      max_cancel_attempts: int(>=0); max_volume: int(>=1);
                      price_bounds: tuple[PriceBound, ...]; expires_at: datetime;
                      origin: Origin; params_sha256: str(64hex)
```

构造期硬规则：**v2 允许构造 native 类型表达被阻断状态，但构造成功不授予执行权**。

- `ControlState` 是 CAS 记录：`mode=native` 必须引用 `native_permit_id`；非 native 不得携带；CAS 按 `canonical_hash(ControlState)` 全值比对（expected 必须是本操作读取的完整持久值；native 控制变更只经单一 control actor、单调 `control_revision` 进入 `register_context`，普通 poll/events 不得触碰）。
- `V2Readiness.execution_scope` 派生：`NATIVE_PILOT` → mode 必须 native；`can_submit=True` 仅当引用有效 permit ∧ `state=EXECUTABLE` ∧ `blockers=()` ∧ coverage 非 None 且**不是** FULL_ACCOUNT（pilot 永不升级 coverage）。`SIMULATION_FULL_ACCOUNT` → 沿用 v1 全账户门槛（EXECUTABLE+无 blocker+coverage=FULL_ACCOUNT+mode=simulation，且不得携带 permit）。`NONE` → 永不 can_submit、不得携带 permit。
- `NativeAuthorization` 的 send/cancel 次数分别计数；`CANCEL_OWNED` 权限永不隐含于 send。

### 8.4 transport DTO（§3.3 冻结字段）

wire 消息继承 `TransportMessage`（每条消息携带完整信封字段 `request_id,instance_id,session_epoch,generation,service_epoch,sequence,sent_at,params_sha256`；`service_epoch=None` 仅表示首次握手，只能取得 disabled 状态，不能领取票据）。嵌套 body 独立成类型：

```python
@ParameterConfirmation: confirmation_id; binding; params_sha256(64hex); adapter_sha256(64hex);
                        confirmed_at; expires_at; explicit_ui_action: bool   # 必须 True；恢复参数不签发
@NativePilotPermit: permit_id; authorization_id; binding; control_revision: int;
                    baseline_evidence_id; route_evidence_id; cash_bound_evidence_id; source_key;
                    actions: tuple[AuthorizationAction, ...]; max_send_attempts(>=1);
                    max_cancel_attempts(>=0); max_volume(>=1); price_bounds; expires_at;
                    params_sha256(64hex); origin: Origin = PRODUCTION
@ExecutionOffer: offer_id; intent_id; leg_id; plan_id(64hex); intent_hash(64hex); leg_hash(64hex);
                 binding; snapshot_id; policy_id; params_sha256(64hex); control_revision: int; expires_at
@ReadyChallenge: offer_id; capture_id; thread_evidence_id; latest_params_sha256(64hex);
                 enable_trading: bool; unconfirmed_event_watermark: str?; generation
@ContractLimit: min_volume; max_volume; volume_step   # 均 >=1
@DispatchCommand: command_id; kind: CommandKind(SUBMIT|CANCEL); attempt_id; offer_id; intent_id; leg_id;
                  plan_id(64hex); binding; leg: Leg; order_type: OrderType; price_tick: Decimal;
                  contract_limits: ContractLimit; control_revision: int; permit_id; params_sha256(64hex);
                  issued_at; expires_at; order_id: int?; route_evidence_id: str?; cancel_id: str?
@DispatchReceipt: receipt_id; command_id; attempt_id; binding; generation; stage: ReceiptStage
                  (CONSUMED|RETURNED|NO_CALL); local_sequence(>=1); recorded_at;
                  result_kind: ResultKind?; result_value: int?; error_code: str?;
                  request_sha256(64hex); previous_receipt_sha256: str?
@RawCallbackField: key: str; value: str
@RawCallbackEnvelope: event_id; kind: EventKind; captured_at; callback_generation; callback_session_epoch;
                      route_evidence_id: str?; raw_fields: tuple[RawCallbackField, ...](1..64)
```

wire 消息：`PollRequest`（+`snapshot_id?/client_state_digest?/ack_watermark?`）、`ReadyRequest`（+`offer_id`、`challenge: ReadyChallenge`，challenge 必须指向同一 offer）、`ResultsRequest`（+`receipt: DispatchReceipt`）、`EventBatch`（+`events` 1..128、批内 event_id 唯一）、`ControlReport`（+`kind: ControlKind(PARAMS_CONFIRMED|GATE_CLOSED|STOPPED|GENERATION_REPORT)`；仅 PARAMS_CONFIRMED 携带 `confirmation`）、`StatusRequest`、`PollResponse`（+`received_at/ledger_revision/control_revision/kind: ResponseKind(IDLE|BLOCKED|OFFER)/offer?/blockers`；仅 OFFER 携带 offer）。

构造期硬规则：

- `DispatchCommand`：有效期必须为正窗口且 ≤ `DISPATCH_TTL_MS`（C1.2 起 1500ms 固定最大值，见 §8.9；恰等于上限有效，更大拒绝；不刷新）；`leg.price % price_tick == 0`（tick 网格复验）；CANCEL 必须携带 journal 认证的 `order_id/route_evidence_id/cancel_id` 三键（不接受裸 OrderID 指令），SUBMIT 不得携带任何 cancel 键。
- `DispatchReceipt`：`ResultKind ∈ {LOCAL_ACK, LOCAL_REJECTED, EXCEPTION, TIMEOUT, INVALID_RETURN}`；`LOCAL_ACK` 要求 `result_value` 非负 int（bool 除外）；`LOCAL_REJECTED` 不得伪造 `-1` 为值；`EXCEPTION/TIMEOUT/INVALID_RETURN` 必须带安全 `error_code`（白名单标识格式，原始 errMsg 不入任何字段）；`NO_CALL` 不携带任何 result。`previous_receipt_sha256` 形成每实例本地收据哈希链。
- `ReadyChallenge.enable_trading` 只是声明——服务不得仅凭该字段认定权限。

### 8.5 native scope 证据与能力来源（native_evidence.py）

```python
class EvidenceSourceKind(str, Enum): TERMINAL_EXPORT; BROKER_EXPORT   # 人工声明不可表示
class CallbackKind(str, Enum): ON_INIT..SET_PARAMS（§5.1 白名单 12 项）
@NativeBaselineEvidence: evidence_id; source_kind; artifact_sha256(64hex); account_key; trading_day: date;
                         session_epoch; as_of; order_inventory/trade_inventory/positions（复用 v1 清单项）;
                         coverage_description; completion_basis; isolation_evidence_id; approved_scope_id;
                         expires_at; origin = PRODUCTION
@NativeRouteEvidence:   evidence_id; framework_sha256; sdk_sha256; binding; callback_kinds;
                        account_scope_basis; strategy_scope_basis; local_alias_scope; front_id: int?;
                        session_id: int?; memo_limit(>=1); memo_roundtrip_verified: bool;
                        validated_at; expires_at; origin = PRODUCTION
@ClientThreadEvidence:  evidence_id; instance_id; session_epoch; generation; process_instance_id;
                        callback_thread_id: int; callback_kind; observed_at; origin = PRODUCTION
@NativeSettlementEvidence: evidence_id; baseline_evidence_id; binding; as_of; artifact_sha256;
                           covered_leg_ids(非空唯一); covered_event_ids(非空唯一); order_inventory;
                           trade_inventory; position_snapshot_id; completion_basis;
                           isolation_evidence_id; origin = PRODUCTION
validate_baseline/validate_route/validate_settlement(evidence, *, now) -> ValidationResult  # 仅期限/结构
```

- `NativeBaselineEvidence.completion_basis` 必须是具体完整性依据文本；人工"无挂单"勾选在 source_kind 枚举中不可表示。
- `NativeRouteEvidence` 缺原生字段可为 null，但归属组合必须成立：`(front_id 且 session_id) 或 local_alias_scope`，且 account/strategy scope basis 非空——缺少足以证明归属/路由的组合即构造拒绝。
- `ClientThreadEvidence` 强制 `process_instance_id`：线程整数不跨进程比较。
- `NativeSettlementEvidence` 只证明列明 scope，永不把 own_journal 升为 FULL_ACCOUNT、不在 scope 外释放资源。

**能力来源（C1 冻结）**：`ProductionEvidenceRegistry` 在 C1 拒绝一切 register/verify（生产登记属于 C3 可信装配器，对 artifact_sha256 与外部原件核实；HTTP body 自报 verified 无效，received sequence 不得改名 complete_through）；`Origin.SYNTHETIC` 永不可登记为 production。`TestEvidenceFactory(purpose="SYNTHETIC_TEST_ONLY")` 仅测试进程内注入（签发 origin=SYNTHETIC 的许可/证据），生产 CLI/HTTP 无实例化路径；测试许可不能通过生产验证器（H34/H35）。

### 8.6 CAS 与 dispatch 事务边界（C1 冻结，N3/N4 实现）

```python
# N4 executor.py（拟开发接口；公共结果/DTO 类型由本包提供）
Executor.prepare_native_offer(intent: OrderIntent, context: ExecutionContext, *, resources=()) -> ExecutionOffer
Executor.commit_native_offer(offer_id: str, challenge: ReadyChallenge, context: ExecutionContext) -> DispatchCommand
Executor.prepare_native_cancel(cancel_id: str, intent_id: str, leg_id: str, context: ExecutionContext) -> ExecutionOffer
Executor.complete_native_dispatch(receipt: DispatchReceipt) -> SubmitResult

# N3 dispatch_journal.py（事务与投影归 N3；HTTP handler 不得直写内部表）
DispatchJournal.begin_dispatch(offer: ExecutionOffer, context: ExecutionContext, *,
                               expected_control_revision: int) -> DispatchCommand
DispatchJournal.record_receipt(receipt: DispatchReceipt) -> SubmitResult
DispatchJournal.pending_commands(binding: Binding) -> tuple[DispatchCommand, ...]
```

事务边界（计划 §5.2 步骤 4，违反即 H16 FAIL；C1.2 见 §8.9）：`begin_dispatch` 必须在**同一个 BEGIN IMMEDIATE** 内完成——重读持久控制（CAS：`canonical_hash(ControlState)` 全值比对 + `expected_control_revision` 单调）、最新资源与许可次数核算、`SUBMITTING/attempt_no=1` 锁存、许可次数占用、唯一 dispatch 记录写出；事务提交后才返回 `DispatchCommand`；不得把 SQLite 锁持有到网络/native 等待。`begin_dispatch` 复用 `begin_attempt/request_cancel` 的同一内部事务校验，不得拆成两笔事务假称原子。offer 阶段无调用权；dispatch 有效期按 §8.9 继承 offer 绝对截止（不刷新、不重授）。客户端在合法回调内复核后先 fsync `CONSUMED` 收据再调用一次；同 command_id/attempt_id 的重复响应只能补传收据（`RETURNED`/`NO_CALL`），不得再次调用（H17）。transport stage（offer/CONSUMED/RETURNED/NO_CALL）是 N3 events/dispatch 的附属审计状态，不新增绕开 `OrderState/CancelState` 的业务终态（计划 §8.1 故障表全部落入既有 N3 语义）。

### 8.7 H 条款 → 测试文件映射（计划 §10.1）

| H 条款 | 契约层贡献（TC=tests/test_infiny_native_contracts.py） |
| --- | --- |
| NE1-H03 | 信封严格拒绝（重复键/未知字段/版本/NaN/bool/无时区/129 事件）、合法 roundtrip 与 hash 稳定 |
| NE1-H17 | Receipt stage/ResultKind 精确语义；NO_CALL 不伪造 -1 |
| NE1-H24 | DispatchCommand CANCEL 三键、SUBMIT 零 cancel 键 |
| NE1-H31 | ControlState CAS 记录与 permit 引用规则 |
| NE1-H34 | 三命名空间隔离；v1 解码不带 v2 能力；production registry 拒绝一切 |
| NE1-H35 | baseline source_kind 白名单；SYNTHETIC 许可仅测试工厂且不可转 production |
| 其余 H01–H40 | 归 TG/TM/TD/TR/TL/TI/TE/TA（计划 §10.1），消费本节冻结类型与常量 |

SYNTHETIC fixture：`tests/fixtures/infiny_native_execution/synthetic_cases.json`（manifest=SYNTHETIC；offer/dispatch/receipt/permit/baseline/poll 六类用例，全部虚构身份）。

### 8.8 C1.1 修订（NE1-ARB-C1.1-001，绑定裁定原文）

本节为 [裁定](../data/maintenance_artifacts/infiny-ne1-service-prep/arbitration-decision.md) RULING 01–10 中归 N1b 的强制增量；C1 基线（父 lock `647f1ac2…`）其余内容不变，旧 transport 字节仅作存档证据。

**RULING 01 响应族**：`TransportResponse` 公共基（`request_id/instance_id/session_epoch/generation/sequence/params_sha256` 精确回显请求、`service_epoch>=1`、`request_service_epoch` 回显含 null、服务端 `sent_at/received_at`、注册 scope `binding`、`ledger_revision/control_revision` 为 N3 实际值）。六个响应：`PollResponse(kind=IDLE|BLOCKED|OFFER, offer, readiness, blockers)`、`ReadyResponse(kind=DISPATCH|BLOCKED, dispatch, readiness, blockers)`、`ResultsResponse(receipt_id/command_id/attempt_id/receipt_sha256=canonical_hash(receipt), persisted=True, duplicate, submit_result: SubmitProjection)`、`EventsResponse(event_stream_id, accepted_event_ids, duplicate_event_ids⊆accepted, persisted=True, ack_watermark)`、`ControlResponse(kind=RECORDED|BLOCKED, report_kind, confirmation_id(仅 PARAMS_CONFIRMED), control_phase, readiness, blockers)`、`StatusResponse(control_phase, readiness, blockers, counters, pending_reconcile_count, ack_watermark?)`。全部分支 `blockers == readiness.blockers`；OFFER/DISPATCH 恰一载荷且零 blocker；BLOCKED 无载荷且非空 blocker；嵌套 offer/dispatch 的 binding 必须等于响应 scope。`ControlPhase ∈ {DISABLED, ARMED, REVOKE_PENDING, DISABLED_CONFIRMED}` 是受审计控制投影，不是新订单状态机。`CallCount{value,lower_bound,upper_bound}`（精确=value=lower=upper；不确定 value=null）；`ExecutionCounters`（13 字段，`count_complete` 要求精确真实计数+两端证据齐全；收据计数非原生入口计数；真实/模拟永不相加；`local_order_ids` 唯一排序）。`SubmitProjection` 是 N3 `SubmitResult{code,leg,revision}` 的 transport 投影（N3 转换其 LegRecord）。HTTP 请求重放指纹 `request_fingerprint(method, route_path, request)`：sha256(version+"\n"+METHOD+"\n"+route+"\n"+encode(request))，覆盖全部 typed 字段、排除 token/连接头；重放命名空间 `(instance_id, session_epoch, generation, service_epoch, request_id)`，指纹变化=REQUEST_ID_CONFLICT。

**RULING 02 GET /status**：空 body、无 query；服务端由 `STATUS_REQUEST_HEADERS`（10 个 `X-Execution-*` 头，token 不在映射内）构造 `StatusRequest` 并按同一 C1.1 规则校验；`STATUS_MAX_HEADER_BYTES=8192`；拒绝缺失/重复/非空 body/query 用 `INVALID_REQUEST`，超限 `REQUEST_TOO_LARGE`。Status 无副作用（不初始化/不准备/不预留/不推进 ACK/不更新 poll 节拍）。

**RULING 03 传输错误**：`TransportErrorCode`（28 码）与不可变 `TRANSPORT_ERROR_HTTP_STATUS` 映射（POLL_TOO_FAST 专属于 429；业务拒绝走 200+BLOCKED 分支+BLOCKER_CODES，不把传输码塞进 V2Readiness.blockers）。`TransportErrorResponse`：公共字段名同响应族但 correlation/scope 与 revisions 可空（认证解析前显式 null，不编造），`error_code/http_status(必须等于表值)/retry_after_ms(None 或 >=1)`；无 message/URL/原始异常。限速仅针对新 admit 的 poll（精确重放先返回缓存；`retry_after_ms=ceil(interval-elapsed)`；拒绝流量不重置节拍时钟）。

**RULING 05 操作员清单**：`OperatorRegistrationManifest`（schema `infiny_native_operator_registration_v1`；`registration_id/record_type(枚举 7 类)/record_path/record_sha256/contract_lock_sha256/operator_action_id/authorization_artifact_path+sha256/artifacts[{path,sha256}]`）——显式本地文件路径，拒绝 URL/UNC/环境展开/可执行；清单是停机 CLI 的操作员输入，运行时/HTTP 不得读取或自动导入。

**RULING 06 快照按值**：`PollRequest.snapshot: ExecutionSnapshot|None`（完整 v1 嵌套字段）+ 一致性规则（有 snapshot → `snapshot_id==snapshot.snapshot_id`；无 → `snapshot_id` 必须 null）；客户端 supplied 的全部 `received_at`（包级+组件+持仓 timing）必须 null，服务首收时盖可信接收时间；同 ID 同内容幂等、内容变化=SNAPSHOT_CONFLICT；`ReadyChallenge.capture_id` 用 snapshot-ID 命名空间（可比 offer 的 snapshot 新）。

**RULING 07/09/10**：`INITIAL_SERVICE_EPOCH=1`（null epoch 仅 poll/status 首握手）；`EventCursor/AckWatermark{event_stream_id,instance_id,session_epoch,generation,sequence,event_id?,event_sha256?}`（sequence 0=空前缀两 null；正序列=事件 ID+canonical_hash）；`RawCallbackEnvelope` 增 `event_stream_id/event_sequence>=1`；`EventBatch` 单流单会话单 generation、序列唯一；`PollRequest.ack_watermark: AckWatermark?`、`ReadyChallenge.unconfirmed_event_watermark: EventCursor?`；配对常量（token header/32 字节/64 hex，运行时须 `hmac.compare_digest`，由服务实现）。

fixture 黄金向量（17 case + vectors）：response 族、429 错误、GET-header 映射、错误码全表、快照按值、首握手 epoch、连续 ACK 游标、operator manifest、满窗 dispatch 命令。

### 8.9 C1.2：dispatch 生命周期与原子 context/dispatch 修订（NE1-ARB-C1.2-OVF06-001）

权威原文：[裁定](../data/maintenance_artifacts/infiny-ne1-service-prep/dispatch-ttl-arbitration.md)（A+B 强制组合）。本节为规范性 DTTL-01..08 条款的契约落点，取代本文件与计划 §5.3、C1.1 RULING 01 中的 500ms 数值表述；其余条款不变。无新 wire 字段/协议版本（transport 保持 `v1_1`）；历史票据（含旧 500ms）保留原截止，部署/重启不得延长或复用其权限。

- **DTTL-01** `DISPATCH_TTL_MS = 1500`（毫秒，SUBMIT 与自有 CANCEL 同值；固定最大值，无运行时/环境/CLI/UI/每请求 override；服务端与自包含客户端镜像同值）。编码 lifetime 恰 1500ms 有效，更大无效；实际使用要求 `issued_at <= now < expires_at`，等于 expiry 即过期。
- **DTTL-02** `compute_offer_deadline(t_offer, original_deadlines)`：`offer.expires_at = min(t_offer + 1500ms, D_original)` 一次设定；同一 offer ID 经重复 poll/ready/恢复/新请求 ID 不得获得更晚截止；offer 无调用权。
- **DTTL-03** `compute_command_deadline(offer_expires_at, t_issue, current_deadlines)`：`command.expires_at = min(offer.expires_at, t_issue + 1500ms, D_current)`，`t_issue` 为唯一 dispatch 事务内取锁后采样的可信服务时间；新观察只能收紧、不能延长原预算或改冻结腿；CANCEL 仅用 H24 适用截止；持久化命令时间戳/ID 在重放时不可变。缺必需截止或剩余寿命非正即阻断。
- **DTTL-04**（seam 冻结，N3 实现/N4 消费）首个成功 native-ready 操作在 dispatch 发布关键路径上恰执行**一次** N3 `BEGIN IMMEDIATE`（完整期望 context CAS + 控制 CAS/revision + 观察/视图刷新 + 动作预检 + attempt 锁存 + 许可扣减 + 唯一 dispatch 插入）；无独立 N4 context 刷新写提交、无同步服务重放缓存写。拒绝不得留下任何部分刷新/attempt/许可/dispatch 提交；重复只可读取既有事实。冻结签名：
  ```python
  DispatchJournal.begin_dispatch(offer, context, *, expected_control_revision, expected_context=None) -> DispatchCommand
  Ledger.begin_dispatch(offer, context, *, expected_control_revision,
                        evidence_registry=None, expected_context=None) -> DispatchCommand
  ```
  `expected_context` 为 N4 同操作读取的完整不可变 `ExecutionContext`（native-ready 路径调用点强制提供；N3 在事务内与实际完整持久 context 比对后才刷新；不把传入控制权写入持久控制）。默认 None 保持既有严格已注册 context 路径，绝不构成隐式未检刷新；`DispatchJournal` 原样转发不开新事务。
- **DTTL-05** dispatch 仅在成功持久提交后且截止仍有效时发布；客户端 `CONSUMED` fsync 后、native 入口前，同一合法回调在 admission 屏障下重验到期与时间敏感门槛（`validate_dispatch_admission` 为纯检查供两端复用）；任一持久化边界耗尽截止即零调用。已提交 attempt/许可占用/未决结果/预留仍按 H16/H17 处理；过期或 `NO_CALL` 不产生重发、退款或自动释放。
- **DTTL-06** 加宽上限至多新增 1000ms 潜在年龄与最大未决票据撤回暴露；不改变提交前控制拒绝、本地关闸/admission 屏障、单次 native 尝试，或 `REVOKE_PENDING→DISABLED_CONFIRMED` 所需证据；已进入的 native 调用在票据过期后仍在途。
- **DTTL-07** H01/H16 需部署主机上可运行的完整 armed 正路径与 fail-closed 反路径证据；H07/H13/H14/H15/H17/H23/H24/H30/H31/H32/H33/H34/H35/H39/H40 不变量适用于修订后字节；仅因全部请求过期而通过的套件不能关闭 OV-F06。
- **DTTL-08** C1.2 是 C1.1 的版本化语义/契约修订：DTO 字段集、路由、schema 名全部不变；只匹配经独立审查 PASS 的 C1.2 server/client 发布才可被接受执行。

工程预算（设计算术，非实测保证）：单 N3 事务 400ms + 客户端收据持久化 400ms + offer 响应尾/HTTP/排队/两次合法回调往返合计 500ms + 200ms 余量 = 1500ms；外部验收门槛 p95(offer 签发→native 入口) ≤ 1300ms 由裁定 Order 4 单独执行。fixture `vectors.dispatch_window` 冻结边界（500/500.001/1499.999/1500/1500.001ms）与规则镜像。

### 8.10 C1.3：registry 适用截止暴露与 CANCEL 截止范围（BUNDLED-ARB-20260915-R2）

权威原文：[裁定](../data/maintenance_artifacts/v2-real-data-track/bundled-arbitration-r2.md)（Q1/Q2；Q3 属 M1 不在本链）。parent=C1.2 `ec4569e7…`。

**RD-01/02（接口与语义，native_evidence.py）**：新本地不可变值 `EvidenceDeadlineResult{validation: ValidationResult, deadline: datetime|None}`（非 transport DTO；`validation.ok` 当且仅当 `deadline is not None`，None 永不表示无约束；成功要求 aware 且严格晚于查询提供的可信 now）；`EvidenceRegistry.applicable_deadline(permit_id, *, action: AuthorizationAction, now) -> EvidenceDeadlineResult`（既有 `verify_permit_active` 与全部注册签名不变；不加 HTTP 字段/路由/journal schema/可配 TTL）。失败语义表（按序首行匹配）：未注册→`NATIVE_PERMIT_UNREGISTERED`；permit 自身到期（含等于）→`NATIVE_PERMIT_EXPIRED`；动作未授权→`AUTHORIZATION_ACTION`；依赖缺失/不可验证/类型范围不符→`NATIVE_DEADLINE_UNAVAILABLE`；适用依赖截止 ≤ now→`NATIVE_DEADLINE_EXPIRED`；全有效→零 blocker 且 deadline=RD-03 最小值。基类实现返回 `NATIVE_DEADLINE_UNAVAILABLE`；`ProductionEvidenceRegistry` 返回 `NATIVE_PERMIT_UNREGISTERED` 并继续拒绝一切生产登记与 SYNTHETIC（加方法不使其成为可用生产验证器）；具体 `ProductionServiceRegistry` 由服务在其所有权文件实现；测试 registry 用显式 SYNTHETIC 证据实现同一语义。消费者对异常与畸形结果同样 fail-closed。

**RD-03（适用界）**：`D_registry(SUBMIT)=min(P.expires_at, A.expires_at, B.expires_at, R.expires_at, C.applicable_deadline)`；`D_registry(CANCEL_OWNED)=min(P.expires_at, A.expires_at, R.expires_at)`。C 记号是可信登记中可导出的现金/费用界（`CashBoundSource` 无到期字段、不新增）；SUBMIT 无法确立该界即 `NATIVE_DEADLINE_UNAVAILABLE`，不得以 permit 寿命顶替。CANCEL 不含 baseline/cash（其后期到期不阻止撤销已证明自有余量），但 permit/撤销授权/当前路由必含；permit 原始更短寿命仍然胜出。`ClientThreadEvidence` 无 TTL（generation 绑定），历史 settlement 不发明 dispatch TTL。查询只读、不引入 artifact 拉取/写盘/HTTP 等待/第二持久事务；撤销/控制变化仍走 journal/CAS。SYNTHETIC 黄金向量（fixture `vectors.registry_deadlines`）：offer=t+1500ms、ctx 授权 t+800、登记授权 t+900、permit t+700、baseline t+300、route t+500、cash t+400 → **SUBMIT=t+300；CANCEL=t+500**。

**RD-04（消费）**：N4 首次签发 offer 时把成功查询的 deadline 计入 `D_original`；N3 在既有唯一 `BEGIN IMMEDIATE` 内取锁后以 `t_issue` 再查询并计入 `D_current`，另独立计入 `context.authorization.expires_at`（SUBMIT/CANCEL 双路由必含，缺授权即阻断——B1 修复既成）与实际用于证明撤销的更紧当前路由界；仅复用 N4 早期查询不足。DTTL 公式与 `DISPATCH_TTL_MS=1500` 不变；查询失败/无 registry/旧 registry/非法截止 → 许可承载 dispatch 阻断且零提交；不需要 native 许可的 simulation 路径不要求发明许可。post-commit/post-CONSUMED 抑制、不可变重放、预算消耗与禁止重武装仍为 DTTL-04/05 义务。

**Q2 CANCEL-D-01（CANCEL 的 D_original/D_current 适用表）**：含——原自有腿 `OrderIntent.expires_at`（不续期）、`context.authorization.expires_at`（显式 CANCEL_OWNED，缺/过期即拒）、经 Q1 的 permit 及其引用撤销授权（有效撤销动作与剩余撤销次数必含）、当前登记路由/归属证据的有限适用截止（旧/缺/歧义即拒）、journal 实际使用的更紧 `D_route`。**不含**——`RiskPolicy.valid_until`（policy 过期本身既不拒 CANCEL 也不缩短其票据；policy 身份仍是 CAS/binding 保护字段）、quote/account/positions/contract/packet 快照 TTL、SUBMIT readiness/full-account proof、仅用于 SUBMIT 的 baseline/cash 新鲜度。`D_current_CANCEL=min(intent.expires_at, context.authorization.expires_at, D_registry(CANCEL_OWNED), D_route)`，全部必需界必须存在；原 offer 截止与 1500ms 上限仍约束命令，等于 expiry 即阻断；不得经由原提交 `Plan.expires_at` 间接重新引入已过期的 SUBMIT policy/quote；已签发 offer/command 保留原更紧截止（不续期）。线程/generation/scope、控制 CAS、实际 UI enable、归属、剩余量、持久能力、撤销唯一性/次数与客户端 admission/连接门槛全部保留。fixture `vectors.cancel_d01` 冻结该表。

### 8.11 C1.4：持久化首 offer 绝对截止（NE1-ARB-C1.4-N4R3-F2-001，Option A）

权威原文：[F2 裁定](../data/maintenance_artifacts/infiny-native-execution/dispatch-window/n4-r3/review/arbitration-f2.md)。parent=C1.3 `5e73632e…`。被禁表达式：`E(r)=min(s,T+r)+1500ms`（内存 `_offers` 字典仅保一个 executor 生命周期，恢复以 `min(sampled_at, now)` 重锚即漂移，`sup E(r)=T+delta+1500ms` 违反 DTTL-02/03 与获准动作的时间界）；Option B（H07 域内放行）与 Option C（由既有持久数据猜锚）均被拒。

- **OFD-01 首次签发与固定预算**：每个确定性 offer ID，签发 N4 操作在预检完成后、构造并持久记录首个 offer 候选前采样一个可信服务时间 `t_first`（仅经本地 seam，绝不来自 HTTP/客户端时钟）。`D_ceiling=compute_offer_deadline(t_first, D_original)`；SUBMIT 保留保守首次签发上限 `E_first=min(D_ceiling, min(sampled_at, t_first)+1500ms)`（仅对从未签发候选计算，绝不重建已知 offer）；自有 CANCEL `E_first=D_ceiling` 仅用 CANCEL-D-01 适用界（packet/sample 锚与原 SUBMIT `Plan.expires_at` 不得回流）。
- **OFD-02 持久不可变**：offer 进入 N4 缓存/服务响应/重放缓存/任何客户端可见通道**之前**，N3 必须在同一 SQLite journal（与 dispatch/attempt 同库）原子持久化不可变 offer-deadline 记录：完整 canonical `ExecutionOffer`、`first_issued_at=t_first`、动作 kind、（CANCEL 时）撤销身份；journal 存储并校验 canonical payload hash；一个 offer ID 恰一条记录；并发首候选按 journal 唯一键串行化；完全相同记录幂等、任何不可变内容不同即冲突（永不替换）；过期记录保留；任何 poll/ready/换 transport request ID/缓存驱逐/context 刷新/重连/恢复/迁移/重启不得更新其截止、删除以允许重发、或创建别名 offer 续预算。记录不授予调用权/attempt/许可扣减/资源释放/重武装。
- **OFD-03 记录与恢复**：首记录在恰一笔 N3 `BEGIN IMMEDIATE` 内写入（完整期望 context 与 control_revision 比对、binding/intent/plan/leg/action 一致性、动作专属截止检查、`synchronous=FULL`）；N3 取锁后采样可信时间并要求 `first_issued_at <= lock_now < offer.expires_at <= first_issued_at+1500ms`；未来 `first_issued_at` 无 H07 容差；不得把传入控制权写入 journal；提交前失败零发布零部分记录，不确定/已完成提交以读取原持久记录裁决；提交后 N4 发布前重查时间（过期抑制发布但不删记录、不启新时钟）；恢复读取存储 offer 字节与时间戳（含过期记录）并在任何可用返回前重验当前 scope/控制门；journal 缺失/完整性失败/首签发来源不可证 → fail-closed。
- **OFD-04 权威 dispatch 继承**：既有唯一 `begin_dispatch` 事务内，N3 必须读取对应不可变 offer 记录、校验传入 offer 的完整 canonical 字节/hash 与 action/binding/leg/control 身份、并使用记录的绝对截止；同 ID 传入更晚/更早截止即冲突；缺记录不能由 `/ready` 补造；新 dispatch `command.expires_at=min(record.offer.expires_at, t_issue+1500ms, D_current)`（取锁后采样，重验全部 C1.3 当前界）；命令重放分支前也先查记录（防改 offer 走早期重放绕过校验）；既有命令 ID/时间戳不可变；提交前过期/冲突零新事实，提交后/CONSUMED 后过期按 DTTL-05/H16/H17 抑制并保留已提交事实。
- **OFD-05 事务与时序账**：首 offer 记录在发布前增加恰一次同步服务 journal 写；自 `t_first` 起的全部时间（含该写锁等待与 commit/fsync）消耗原 1500ms 预算（不得在写后重置 `t_first`、不得把签发定义为响应送达）；首个成功 `/ready` 发布路径仍是恰一笔成功持久写事务（DTTL-04）；读 offer 记录不加第二 ready 事务、N4 无 context 刷新/replay-cache 写；精确重复读零变更提交；把 offer 记录折叠进既有 N3 准备事务的优化必须原子提交完整记录且若改公开签名需另行冻结 seam；部署主机门槛不变（≥100 预声明合格周期、`t_first`→native 入口最近秩 p95 ≤1300ms、无入口计为无穷）。
- **OFD-06 升级与保留**：C1.4 需版本化本地 journal 存储特性；升级后的 C1.2/C1.3 journal 中缺记录不构成"从未签发"证据；启用新签发前原子建立特性与 legacy 资格标记，对每个缺乏可信首 offer 证据的既有潜在已 offer 动作/腿保守 tombstone（覆盖底层动作与腿而非猜测 offer ID）；恢复时间/迁移时间/快照时间/`command.expires_at` 不得重标为历史首签发；未知 legacy offer 不得创建新 dispatch；既有已提交命令（含旧 500ms）保留原字节/收据/截止/对账事实；新创建要求可证明从未签发的 C1.4 动作走通常注册/控制/来源唯一规则；任何自动新 ID/source key/授权/客户端重武装都不是迁移补救；记录/tombstone 保留至 journal 保留期，删除/压缩/回滚不得使旧 ID 重新可用。
- **OFD-07 版本与收口**：C1.4 是对 C1.3 父的加性本地契约与 journal 存储修订；`DISPATCH_TTL_MS=1500`、H07 容差、既有 `ExecutionOffer`/命令 wire 字段、`/execution/v1/` 路由、`infiny_client_execution_transport_v1_1` 与 v1/v2 核心标识全部不变；offer 记录是本地 journal 数据，非新 transport DTO 或权限来源；`begin_dispatch` 调用签名不变但语义变更；旧 lock/组件历史 PASS/未变 wire 版本不证明 C1.4 发布；F2 仅在针对修订规范与精确依赖哈希的全新独立 N3 与 N4 审查（及后续联合/整体门）通过后关闭。

**冻结本地类型与 seam（N1b 所有权；N3 实现、N4 消费）**：

```python
# native_contracts.py —— 本地不可变值，schema=infiny_execution_local_v1，
# 不继承 transport 基类、不被任何 transport/v1/v2 解码器接受为 HTTP payload。
@OfferDeadlineRecord: offer: ExecutionOffer; first_issued_at: datetime;
                      kind: CommandKind; cancel_id: str | None = None
# 构造校验：aware UTC；正 lifetime；offer.expires_at <= first_issued_at+1500ms；
# SUBMIT cancel_id=None；CANCEL 需有效 journal 撤销身份。
encode_local/decode_local —— infiny_execution_local_v1 信封专用编解码。

# N3 facade（registry 来自其可信构造，同 begin_dispatch）
DispatchJournal.record_offer(offer, context, *, first_issued_at, kind,
                             expected_control_revision, expected_context,
                             cancel_id=None) -> OfferDeadlineRecord
DispatchJournal.get_offer(offer_id: str) -> OfferDeadlineRecord | None
# N3 实现；恰一笔首记录事务，无 facade 事务
Ledger.record_offer(offer, context, *, first_issued_at, kind,
                    expected_control_revision, expected_context,
                    cancel_id=None, evidence_registry=None) -> OfferDeadlineRecord
Ledger.get_offer(offer_id: str) -> OfferDeadlineRecord | None
# C1.2 begin_dispatch 与 C1.3 applicable_deadline 签名零变更。
```

`record_offer` 是原子 insert-or-read（同 journal 锁下比对：全字段相同→返回原记录零变更提交，含已过期——历史数据不绕过 N4 发布门；任何字段不同（含 expires_at/first_issued_at）→冲突零写入）；`get_offer` 只读、过期记录原样返回、仅缺记录返回 None（None 本身不授权创建）；完整性失败抛错不表现为缺失。首记录事务无 attempt/撤销 attempt/许可扣减/资源释放/context 刷新/资源 revision 副作用；`expected_context` 在新记录 seam 为必填；N3 须加持久唯一 `offer_id` 键与 binding/action/intent/leg 唯一性/tombstone 守卫（换 ID 不得续同一动作预算；SUBMIT 与 CANCEL 是不同动作；新 cancel ID 不是新预算）。

**冻结诊断码**（映射既有 blocked/conflict 结果传输，无新 HTTP schema；缺失/非法截止永不当无穷）：`OFFER_DEADLINE_REQUIRED`（新 dispatch 无必需持久 offer 记录；ready 不得惰性创建）、`OFFER_DEADLINE_CONFLICT`（同 ID 记录字节/hash/动作/撤销身份/首时间不同，或同已 offer 动作的别名 ID）、`OFFER_DEADLINE_UNPROVEN`（legacy 首签发或从未签发资格不可证；迁移/恢复不得铸造）、`OFFER_DEADLINE_CLOCK_INVALID`（本地 `first_issued_at` 晚于 N3 可信锁时间或可信时间序不可验证；H07 无宽容）。**多重无效输入优先级**（N1b 向量冻结于 fixture `vectors.offer_deadline.precedence`，TC `test_c1_4_frozen_precedence_for_multiply_invalid_records` 执行）：结构类型错误（引擎 ContractError）→ 非正 lifetime → 超预算 lifetime → kind/撤销身份。OFD→H 映射：主 H01/H03/H07/H13/H15/H16/H17/H24/H32/H33/H35/H36。fixture `vectors.offer_deadline` 冻结 R3-P14 场景（首签发 01:29:59Z、原 1500ms、恢复偏移 400/1400/1500/1600/2100/3000ms、零漂移规则、1000/2000/2000.001ms skew）、`E_first` 公式、seam 镜像、诊断码与 `offer_deadline_record_synthetic` 本地记录 case。F2-T01 归 N1b（本文档随 TC 冻结）；F2-T02..T11 按裁定归 N3/N4/集成。**编码包装器（c1.4-r2/F1）**：`nc.encode/nc.decode` 按各类型自身 schema 命名空间分发——v2 记录（ControlState、证据、manifest）保持既有编码字节路径，journal-local 命名空间专用 `encode_local/decode_local`（通用包装器对其显式拒绝）；跨命名空间隔离由引擎双侧 schema 校验保证。

### 8.12 C1.5：准备事务合并（NE1-ARB-C1.5-OVF06-ANCHOR-001，option b）

权威原文：[锚定裁定](../data/maintenance_artifacts/infiny-native-execution/dispatch-window/joint-r1/arbitration-anchor.md)。parent=C1.4-r2 `71d9e1ba…`。**本修订是本地接口/原子性修订，不是锚点、TTL、transport 或 journal 存储格式修订**：`DISPATCH_TTL_MS=1500`、C1.4 journal 特性、既有 wire 字段/路由/schema 标识与全部 deadline 公式不变；option (a)（去 OFD-01 样本上限）与 option (c)（再增 TTL）均被拒。

**公式冻结重申（修订前后均绑定）**：记 `tau=1500ms`、`s`=原始必需 packet `sampled_at`、`f=t_first`、`D_S/D_C`=全部原始适用 SUBMIT/CANCEL 截止最小值（缺失/非法即阻断，绝不用无穷或新时间戳顶替）：

```text
E_first_SUBMIT = min(D_S, f + tau, s + tau)        # 首签发上限 ∩ 保守样本上限
E_first_CANCEL = min(D_C, f + tau)                 # 无 packet/样本上限
command.expires_at = min(stored.offer.expires_at, t_issue + tau, D_current_for_action)
```

`compute_offer_deadline(f, D_S ∪ {s+tau})` 即 `E_first_SUBMIT`（等价组合，公式无任何变更）；N4 `_first_deadline` 保留 SUBMIT 额外最小项；旧 `_offer_deadline` 调用点必须迁往同一 C1.4 语义与持久记录纪律（重命名或以 `now` 顶替 `sampled_at` 不是补救）。分析反例（ANC-T01 冻结）：`s=T, f=T+1154ms, D_S=T+3000ms` → 冻结截止 `T+1500ms`（非 issuance-only 的 `T+2654ms`）；R3-P15 旧样本（`s=f-500ms`）→ `f+1000ms`；`s=f` 时仅在其他界允许下取满上限。CANCEL 不得经共享 helper 泄入 SUBMIT 专属 policy/plan/数据新鲜度。

**冻结 seam（N3 实现、N4 消费；元组为既有不可变本地域值，非 HTTP DTO/序列化能力/控制源/native 许可；N4 可包入既有 `PreparedExecution`，N3 不得 import executor 制造包装）**：

```python
# 返回 (reserved original Plan, final persisted ExecutionContext)；
# context 含其 ReservationReceipt 与最终 ReservationView。
DispatchJournal.prepare_native_submission(
    intent: c.OrderIntent, context: c.ExecutionContext, *,
    expected_control_revision: int, expected_context: c.ExecutionContext,
    resources: tuple[c.ExecutionSnapshot, ...] = (),
) -> tuple[c.Plan, c.ExecutionContext]

Ledger.prepare_native_submission(
    intent: c.OrderIntent, context: c.ExecutionContext, *,
    expected_control_revision: int, expected_context: c.ExecutionContext,
    resources: tuple[c.ExecutionSnapshot, ...] = (),
    evidence_registry=None,
) -> tuple[c.Plan, c.ExecutionContext]
```

facade 转发其可信 registry 与参数、不开事务。`record_offer/get_offer/begin_dispatch` 与证据查询签名不变；既有公共 simulation/一般 preparation/refresh API 行为保留。

- **AP-01 单准备事务**：成功的新鲜 native SUBMIT 准备恰执行一笔 N3-owned `BEGIN IMMEDIATE`（`synchronous=FULL`），事务内完成：与同操作必填 `expected_context` 的完整持久 context 比对；重读并校验持久控制身份/revision 与适用许可权威；intent 注册或校验与 source 唯一性；观察/resources 校验与采纳；推导当前 revision/reconciliation/reservation view；按既有规划规则计算 plan；校验并预留其腿/资源；连同 receipt 与更新 view 持久化最终 context。取锁后采样可信校验时间并在接受前重查时间敏感前提；不得把传入控制权写入 journal；缺已注册 scope 不是初始化许可。须用 N3 内部事务域原语（复用既有校验逻辑），不得调用各自开事务的公共方法；四个中间 context 提交被一个原子可见的最终 context 取代（保留其比较与资源推导）；非空 `resources` 在事务内校验、不得额外刷新提交；保留 source/intent 冲突、plan 形态/hash、量/现金核算、未试腿选择、revision 语义与确定性身份；幂等准备下既有存储 plan/receipt 权威（不重规划已预留动作、不造新 ID、不增 attempt/许可计数）。
- **AP-02 全有或全无**：业务校验/CAS/过期失败时，新 intent 注册、观察/context 变更、plan 与预留作为一个单元回滚，零部分结果外泄；既有必需存储/reconciliation 失败锁存保留其独立 fail-closed 处理并如实上报（不得伪装成零写）；不确定提交时停止签发并从已提交 journal 事实裁决（不得假设回滚并造替代动作）；返回的 context/view 必须描述实际已提交状态；无 SQLite 锁跨越 caller 让出/HTTP/回调等待/外部 registry 拉取/native 调用（registry 截止查询仍是本地读）。
- **AP-03 独立 offer 记录保留**：N4 native offer 路径以新准备 seam 取代 `self.prepare` 的六次写提交；准备提交且前提成立后，N4 采样一次 `t_first`、按上式构造首 offer、并在发布前使用**不变的** C1.4 `record_offer` 事务；消费持久胜者并在任何可用返回前重查当前 scope/控制与时间；既有 offer 从持久态按原字节与时间戳读取（内存不是替代截止权威）；任何重试不得重锚已过期/不确定 offer。**本修订不把 offer 记录折叠进准备**（折叠会改变签发位置/记录 seam，需 OFD-05 下另行 Codex 规格；分开保留 OFD-03"record_offer 无 context/预留副作用"契约，避免 N4 回调执行于 N3 事务内）。
- **AP-04 记录/attempt 生命周期独立于准备成败**：准备提交耗尽样本上限则不发布 offer；offer 记录提交耗尽则保留已提交记录并抑制发布；已提交预留按既有 reconciliation/释放规则保留；过期、失败计时周期或 `NO_CALL` 永不退还 native 许可、重置 attempt、释放未知暴露或授权自动重武装；新鲜度按原始 source/sample 时间判定——更快的 prepare 不制造新鲜行情证据。

**写组计数（已识别的干净首 offer 路径）**：intent 注册+四次 context CAS+预留 6 写→**1 原子准备提交**；首 C1.4 offer 记录 1（不变）；poll 需要时 capture 注册 1（不变）；已识别首 offer 写合计 8→**3**；首个成功 ready 发布 1（不变）。该削减（串行提交 7→2）是选定工程补救，**不构成** 1300ms 门槛将通过的声明；每笔额外写与争用须单独计量。主机门槛不变：自实际 `t_first` 至有效 native 入口的最近秩 p95 ≤1300ms、≥100 预声明合格周期（无入口计无穷；样本上限可能使签发后不足 1300ms——满足名义指标绝不授权越过更早截止）。

ANC-T01 归 N1b（本节+fixture `vectors.preparation_seam`+TC 冻结公式/seam/边界；保留 500/500.001/1499.999/1500/1500.001ms 向量）；ANC-T02..T10 按裁定归 N3/N4/集成（CANCEL 不经新 SUBMIT seam：ANC-T08）。AP/ANC→H 映射：主 H01/H03/H07/H11/H13–H17/H23/H24/H32/H33/H35/H36。

## 9. 离线测试基线

命令：`python scripts/test_offline.py -q tests/test_infiny_execution_contracts.py`（虚构账户/合约，不读 .env/native/网络）。当前覆盖：字段往返与不可变、来源/身份/层级/时区/布尔冒充拒绝、schema 版本与重复键防护、hash 规范性、六动作价格必填、native proof 屏障与来源隔离、own=0 不推 account=0、时效边界（3/5/3600/10 秒 + 2 秒未来容差）、锁存上下文、线程/版本/授权/证明门槛、事件归属证据与数量守恒。N2/N3/N4 的 E01–E28 用例归属各自节点测试文件，不重复本文件。
