# N3 持久订单账本精确下游接口

权威规格 sha256 `08fa6c85435dab5834e051b98af0e323b6bb4b724383832a3ab9ab96c53353a9`。本文件是 N4 的调用约定；实现位于 `src/ak_trading/infiny_execution/ledger.py` 和 `reconcile.py`。仅标准库、SQLite 文件和 Decimal，无 native 端口、联网或真实交易能力。本文中所有契约类型从 `ak_trading.infiny_execution.contracts` 导入，下面的结果类型从 `ledger` 导入，CallbackContext 从 `reconcile` 导入。接口版本 v3（NE1 C2：dispatch/控制/许可/结算/迁移 + C1.2 dispatch-window seam；v1/v2 语义不变）。

## NE1 C2 增量（v3）

### DispatchJournal（`dispatch_journal.py`，C1 冻结签名）

`DispatchJournal(ledger: Ledger, *, evidence_registry: native_evidence.EvidenceRegistry | None = None)`。三方法委托同库同事务的 Ledger 实现；transport stage 只是审计记录，不新增业务终态。

| 方法 | 返回与约束 |
| --- | --- |
| `begin_dispatch(offer: ExecutionOffer, context: ExecutionContext, *, expected_control_revision: int, expected_context: ExecutionContext \| None = None)` | `DispatchCommand`；C1.2 DTTL-04 单条 BEGIN IMMEDIATE：锁后采样 t_issue → offer 身份 → 幂等重放（原时间戳不变，窗口已关闭的重放同样抑制交付）→ control CAS → context CAS → kind 推导/许可核验/预算 → 与 begin_attempt/request_cancel 完全相同的门序列 → attempt + 扣减 + 唯一票据 → DTTL-03 截止 `min(offer.expires_at, t_issue+1500ms, D_current)`（等号即过期；CANCEL 仅取 H24 适用截止）→ 提交后重采样，过期抛 `LedgerError(BLOCKED, ('DISPATCH_EXPIRED',))` 抑制交付但保留全部已提交事实 |
| `record_receipt(receipt: DispatchReceipt)` | `SubmitResult`；CONSUMED 仅标记 consumed；RETURNED/NO_CALL 关闭票据并按 ResultKind 投影到既有 `record_submit_result/record_cancel_result` 语义（LOCAL_ACK→本地编号、LOCAL_REJECTED→-1、EXCEPTION/TIMEOUT/INVALID_RETURN/NO_CALL→未知）。同 receipt_id 同 payload 幂等；改 payload 锁存 OWNERSHIP_CONFLICT |
| `pending_commands(binding: Binding)` | `tuple[DispatchCommand, ...]`；只读，重放不可再消费 |

`expected_context` 语义（C1.2 冻结）：N4 在同一操作中从持久 scope 读到的完整不可变 ExecutionContext。`None` 保持既有严格路径（要求 context 已按原样注册，不做任何隐式刷新）。非 None 时 N3 在事务内先与持久 context 做完整 canonical 比较——不一致即 `CONTEXT_CONFLICT` 零写；候选的 `mode/binding/policy/authorization/route_ownership` 控制字段必须与 expected 完全一致（观察刷新永不搬运控制权），随后观察部分（snapshot/proof/clock/view）经全部既有校验并在同一事务写入。三段 N4/N3 写合并为一次 revision bump；拒绝路径零 bump、零观察落盘、零 attempt、零扣减。

DTTL-03 `D_current` 推导（n3-r3，依 C1.3 RD-04/CANCEL-D-01）：SUBMIT 取 `intent.expires_at、policy.valid_until、authorization.expires_at（双路由必含）、permit/evidence 适用截止（经 `EvidenceRegistry.applicable_deadline(permit_id, *, action, now= t_issue)` 公共接口在事务内消费——查询异常或 `validation` 不通过即零提交 `NATIVE_DEADLINE_UNAVAILABLE`）、quote/account/positions/contract/packet 组件时效`的最小值；CANCEL 按 CANCEL-D-01 适用表取 `intent.expires_at、authorization.expires_at、permit/evidence 适用截止（action=CANCEL_OWNED）`，不含 `policy.valid_until`。n3-r2 登记的"permit 截止不可推导"限制已随 C1.3 接口关闭。

`kind` 推导固定：leg 未尝试 → SUBMIT；已尝试且 cancel_state=NONE → CANCEL；其余 BLOCKED_DISPATCH。许可预算 send/cancel 分列，扣减与 attempt 同事务，不足即 `PERMIT_EXHAUSTED` 且零 attempt。pilot（control_state 带许可）时 submit 门序列以注册许可替代 readiness/proof 门槛（其余门全保留）；cancel 门不变。

### Ledger 新增持久 API

| 方法 | 返回与约束 |
| --- | --- |
| `register_control_update(control: native_contracts.ControlState, *, permit: NativePilotPermit \| None = None, evidence_registry=None)` | `int`（control_revision）；控制变更唯一入口，scope=`account\|strategy\|exchange\|instrument\|hedge` 规范键，revision 必须严格递增；native 许可必须经 registry 接受且 `verify_permit_active` 通过，预算存入 control_state |
| `control_revision(binding)` | `int \| None` |
| `pending_dispatch_commands(binding)` / `ack_outbox(binding, event_id)` | 只读票据 / ACK 水位只前进（同刻或更旧返回 False，未知事件 NOT_FOUND） |
| `register_native_route(evidence: NativeRouteEvidence, *, evidence_registry)` / `register_settlement_evidence(evidence, *, evidence_registry)` | 持久不可变证据（同 id 不同 payload 锁存冲突）；registry 拒绝即 `NATIVE_ROUTE_UNREGISTERED/NATIVE_SETTLEMENT_UNREGISTERED` |
| `absorb_settlement(evidence: NativeSettlementEvidence, *, evidence_registry=None)` | `RecoveryResult`；只释放列明 covered legs/events：清单与投影逐项相等、终态数量守恒、持仓/冻结满足与 simulation 屏障相同的基线纪律；不升 FULL_ACCOUNT、不动未列明资源 |
| `assert_replaceable(leg_id)` | `LegRecord`；replacement 前置：终态 + settled + trade_gap=0 + cancel 终值 |
| `Ledger(path, *, clock, busy_timeout_ms, schema="v2")` | v1 journal 对 dispatch/控制 API 一律 `SCHEMA_UNSUPPORTED`；迁移用 `scripts/migrate_infiny_execution.py`（backup API 一致性副本 + 守恒断言 + 原库只读） |

`RecoveryResult` 新增 `unresolved_command_ids: tuple[str, ...]`；`recover()` 返回未关闭票据。reconcile `CallbackContext` 新增 `native_route_evidence_id`（仅 PYTHONGO 且非模拟 scope）；`normalize_event` 生成 `native-route:<id>` 归属声明，账本只接受已注册、未过期、binding/epoch/front/session 匹配的路由证据；TradeData 无 Front/Session 仍走已验证 order alias。

### NE1 C1.1 登记类（RULING 04/05/06/07/09）

| 方法 | 返回与约束 |
| --- | --- |
| `begin_maintenance() -> str` / `end_maintenance(token)` / `maintenance()` 上下文 | journal 内独占维护访问；已有持有者即 `MAINTENANCE_HELD`，token 不匹配拒绝释放 |
| `register_actor(actor_id, account_key, bindings, *, operator_action_id, manifest_sha256, contract_lock_sha256, expected_registration_revision=None, enabled=True) -> int` | 必须持有维护权；同内容幂等返回原 revision；内容变化必须带精确 expected revision，新 revision=旧+1（首个=1）；`MAX_ACCOUNT_ACTORS=8` 容量；bindings 唯一且同账户；仅表示 actor 存在，不是交易授权 |
| `actors() -> tuple[ActorRecord]` | 从 journal 重建的启用 actor（含 bindings/revision/审计 hash） |
| `register_operator_manifest(manifest: OperatorRegistrationManifest)` | 停服维护权下持久 manifest；同 id 同内容幂等、异内容 `MANIFEST_CONFLICT`；路径必须是显式本地文件——裸点分名（如 `modules.actor`）拒 `MANIFEST_PATH_INVALID`（C1.1 审查 F2 加严） |
| `allocate_service_epoch() -> int` / `service_epoch()` | 每次 true start 严格递增持久分配，首个为 `INITIAL_SERVICE_EPOCH=1`，重开 journal 继续递增；无法推进即阻塞执行流量 |
| `record_http_request(*, instance_id, session_epoch, generation, service_epoch, request_id, fingerprint, response=None) -> (ResultCode, str \| None)` 与 `store_http_response(...)` | 重放命名空间 `(instance,session,generation,service_epoch,request_id)`；同 fingerprint 幂等返回缓存响应，异 fingerprint `REQUEST_ID_CONFLICT` 零覆写 |
| `register_capture(snapshot, *, instance_id, session_epoch, generation) -> datetime` | RULING06 按值采集：客户端 supplied `received_at`（含 positions 组件）必须为 null，否则 `SNAPSHOT_INVALID`；服务记录可信接收时刻与原始 canonical hash；同 id 同 scope 同内容幂等保留原时刻，异 scope/内容 `SNAPSHOT_CONFLICT` |
| `capture_registered(snapshot_id) -> bool` | ReadyChallenge.capture_id 引用查询 |
| `record_event_envelopes(envelopes: tuple[RawCallbackEnvelope], *, instance_id) -> AckWatermark` | RULING09 连续游标：持久 `(stream,sequence)->(event_id,hash)`；单批单流；同序同内容幂等，改内容/同 id 异序 `EVENT_CONFLICT` 并锁存 EVENT_GAP；水位只跨持久连续前缀（存 3 缺 2 停在 1，补 2 越过 3）；返回前先落盘 |
| `ack_watermark(*, event_stream_id) -> AckWatermark \| None` / `validate_client_ack(client_ack)` | 客户端水位高于或不一致即 `ACK_WATERMARK_INVALID`；更低仅 stale；未知流 sequence>0 拒绝 |
| `execution_counters(binding) -> ExecutionCounters` | RULING01/H39 计数事实：committed/consumed/returned/no_call/unresolved 为 journal 事实；native_send/cancel_attempts 为区间下界（value=None），`count_complete=False` 直到客户端边界证据对账；`local_order_ids` 为已归属非负本地编号，broker/fake 字段 None；真实与模拟绝不相加 |
| `pending_reconcile_count(binding) -> int` | 该账户持久隔离事件数 |

登记/操作员/游标实现以 C1.1 e5c44742/c6b2f583 为基准；操作员 CLI（register-actor 等）由服务所有者在 `scripts/run_infiny_execution.py` 实现，仅消费上述 journal API。

### NE1 C1.4 持久首offer截止（OFD-01..07）

| 方法 | 返回与约束 |
| --- | --- |
| `record_offer(offer, context, *, first_issued_at, kind, expected_control_revision, expected_context, cancel_id=None)` | `OfferDeadlineRecord`；恰一笔首记录事务（BEGIN IMMEDIATE）：锁后采样 lock_now，要求 `first_issued_at <= lock_now < offer.expires_at <= first_issued_at+1500ms`（未来 first_issued_at 拒 `OFFER_DEADLINE_CLOCK_INVALID`，无 H07 宽容）；完整 expected-context canonical 比较与控制 CAS；binding/intent/plan/leg/action 一致性；CANCEL 需 journal 已验证自有单与 cancel 身份、SUBMIT 需腿未尝试；独立执行全部适用原始截止（durable plan/context/C1.3 registry），超出即拒；action_key 唯一——同 id 同内容幂等读（零变更提交）、同 id 异内容或同 action 异 id 拒 `OFFER_DEADLINE_CONFLICT`；tombstone 命中拒 `OFFER_DEADLINE_UNPROVEN`。无 attempt/扣减/context 刷新/资源 revision 副作用 |
| `get_offer(offer_id)` | `OfferDeadlineRecord \| None`；只读，过期记录原样返回；None 仅表示缺失且不授权创建；完整性失败抛 LEDGER_CORRUPT |
| `begin_dispatch`（签名不变） | OFD-04：事务内先读持久首记录——缺失拒 `OFFER_DEADLINE_REQUIRED`（ready 不得惰性创建），字节/身份不符拒 `OFFER_DEADLINE_CONFLICT`，检查先于重放分支；kind/cancel 身份与记录一致；command 截止 = `min(record.offer.expires_at, t_issue+1500, D_current)` |

升级与保留（OFD-06）：v2 journal 打开时原子安装 `ofd_feature` 标记；安装前已存在的每条腿的 SUBMIT/CANCEL action 全部保守 tombstone（恢复/迁移时间不得铸币）；已提交命令/收据/预留字节保持；迁移脚本对 v1 副本执行同规则。备选 ID 不可续用同一 action 的预算。

### NE1 C1.5 单事务原生 SUBMIT 准备（AP-01..04）

| 方法 | 返回与约束 |
| --- | --- |
| `prepare_native_submission(intent, context, *, expected_control_revision, expected_context, resources=(), evidence_registry=None)` | `(Plan, ExecutionContext)`；恰一笔 BEGIN IMMEDIATE（synchronous=FULL，锁后采样可信时间）内完成：完整 expected-context canonical 比较（缺失 scope 非初始化许可，观察永不搬运控制字段）→ 持久控制身份/revision 重验 + permit `verify_permit_active` → intent 注册或校验（source 唯一，同 payload 幂等；已预留意图返回存储 Plan 零重规划零新写）→ 观察/resources 采纳 → 当前 revision 与 view 推导 → 既有纯规划规则计算 plan（blockers 即整回滚）→ 与 `reserve` 相同校验后腿/资源预留 → 持久化唯一最终 context（含 ReservationReceipt 与预留后 ReservationView）。非 SUBMIT/反手动作（CLOSE 类）拒 `UNSUPPORTED_CLOSE_POLICY`——自有 CANCEL 不经过本 seam |
| `DispatchJournal.prepare_native_submission(...)` | 纯转发（registry 来自构造器），不开事务 |

AP-02：任何校验/CAS/过期失败将 intent 注册、观察、plan、预留作为整体回滚（无部分准备事实逃逸）；不确定提交从持久事实裁决（重开幂等返回原 Plan/receipt，不重预留不换 ID）；SQLite 锁不跨任何让出；registry 查询保持本地读。AP-03：offer 记录独立（`record_offer` 不变，不在准备事务内折叠）。AP-04：准备/记录/尝试生命周期相互独立——过期或 NO_CALL 不退还不重置不自动重武装；快prepare不制造新鲜行情证据（沿用原始 sample/source 时效）。

---

## 类型与错误（v1/v2 保持）

所有下面的结果类均为 `@dataclass(frozen=True, slots=True, kw_only=True)`。枚举直接使用共用契约，不返回可变 dict。数据库失败抛 `LedgerError`，不会返回成功。业务拒绝也抛 `LedgerError`，其公开属性为 `code: ResultCode`、`blockers: tuple[str, ...]`；错误文本只包含这些安全代码。

| 类型 | 完整字段 |
| --- | --- |
| LedgerStatus | `revision: int; reconcile: ReconcileState; closed: bool` |
| RegisterResult | `code: ResultCode; intent: OrderIntent; intent_hash: str; revision: int` |
| ContextReceipt | `snapshot_id: str; revision: int` |
| LegRecord | `intent: OrderIntent; plan: Plan; leg: Leg; state: OrderState; attempt_no: int; attempt_id: str | None; order_id: int | None; order_sys_id: str | None; reported_filled: int; trade_filled: int; risk_filled: int; trade_gap: int; cancel_volume: int | None; cancel_state: CancelState; settled: bool; reconcile_required: bool; ownership_verified: bool` |
| AttemptResult | `code: ResultCode; send_permitted: bool; attempt_id: str; attempt_no: int; leg: LegRecord; revision: int` |
| SubmitResult | `code: ResultCode; leg: LegRecord; revision: int` |
| IngestResult | `code: ResultCode; event_id: str; leg_id: str | None; reason: str | None; revision: int` |
| CancelAttempt | `code: ResultCode; cancel_permitted: bool; cancel_id: str; order_id: int; leg: LegRecord; revision: int` |
| RecoveryResult | `code: ResultCode; reconcile: ReconcileState; revision: int; unresolved_leg_ids: tuple[str, ...]` |

## Ledger 方法签名

下表是精确签名；`datetime` 必须有时区，所有序列使用 tuple。可调用时钟默认 UTC 当前时刻，测试必须注入 FakeClock；单个 Ledger 对象由内部锁串行访问，多个对象/进程通过 SQLite BEGIN IMMEDIATE 串行写事务。

| 方法 | 返回 |
| --- | --- |
| `Ledger(path: str | Path, *, clock: Callable[[], datetime] | None = None, busy_timeout_ms: int = 1000)` | Ledger；拒绝 `:memory:` |
| `close()` | `None`；重复 close 安全，关闭后任何读写拒绝 |
| `__enter__()` / `__exit__(exc_type, exc, traceback)` | `Ledger` / `None` |
| `check()` | `LedgerStatus`；执行完整性检查、10 秒确认超时检查；不清锁存 |
| `register(intent: OrderIntent)` | `RegisterResult`；相同规范 payload 返回 IDEMPOTENT；任一 intent/source 键不同 payload 抛 INTENT_CONFLICT |
| `register_context(context: ExecutionContext, *, resources: tuple[ExecutionSnapshot, ...] = ())` | `ContextReceipt`；持久化完整 typed context 及同账户额外合约快照。主 snapshot 自动纳入 resources |
| `initialize_context(context: ExecutionContext, *, resources: tuple[ExecutionSnapshot, ...] = ())` | `ContextReceipt`；单条 BEGIN IMMEDIATE 内完成"scope 不存在才注册"：该 `(account_key,strategy_key,exchange,instrument_id,hedge_flag)` scope 已有任何 context 时抛 LedgerError(BLOCKED, ('CONTEXT_CONFLICT',))，零写、绝不覆写现存控制；成功即成为该 scope 的初始控制。用于首次初始化，消除"context()读到 None→register_context"的跨连接 TOCTOU。后续更新仍用 compare_and_register_context |
| `compare_and_register_context(context: ExecutionContext, expected_context: ExecutionContext, *, resources: tuple[ExecutionSnapshot, ...] = ())` | `ContextReceipt`；同一 BEGIN IMMEDIATE 首先核对当前持久context完整canonical hash==expected，要求mode/binding/policy/authorization/route与expected不变；随后原子登记新观察/收据/view。错配抛 LedgerError(BLOCKED, ('CONTEXT_CONFLICT',))，零写 |
| `context(binding: Binding)` | `ExecutionContext | None`；取 `(account_key,strategy_key,exchange,instrument_id,hedge_flag)` 持久scope最新上下文；可能已是新epoch/day/instance，调用者必须比较返回binding，不能假定旧绑定仍有效 |
| `view(account_key: str, *, snapshot_id: str | None = None)` | `ReservationView`；账户跨策略/合约汇总、包含历史预留，风险合计排除 RELEASED |
| `reserve(plan: Plan, expected_revision: int)` | `ReservationReceipt`；原子校验/全腿预留。缺 register(intent) 或 register_context 阻断；绝不隐式创建意图 |
| `get_leg(leg_id: str)` | `LegRecord`；缺失抛 NOT_FOUND |
| `begin_attempt(leg_id: str, context: ExecutionContext)` | `AttemptResult`；只有首次成功持久化 SUBMITTING/attempt_no=1 才 `send_permitted=True`；重入返回 IDEMPOTENT/False |
| `record_submit_result(leg_id: str, result: object = None, *, error_code: str | None = None)` | `SubmitResult`；公开注入假端口返回，不接受端口或回调函数 |
| `ingest_event(event: ExecutionEvent)` | `IngestResult`；同事务持久事件、成交去重、订单投影；不完整/未知/冲突持久隔离 |
| `recover(proof: ReconciliationProof | None = None, events: tuple[ExecutionEvent, ...] = ())` | `RecoveryResult`；仅核对、吸收或释放，不发送；proof 对应的最新快照必须先登记 |
| `refresh_pending_open(leg_id: str, context: ExecutionContext)` | `ReservationReceipt`；反手所有 CLOSE 已 FILLED/settled/trade_gap=0 且新完整屏障证明原向零仓后，原子重算未尝试 OPEN 承诺。相同刷新幂等；任何 OPEN 已尝试均拒绝 |
| `release_unattempted(plan_id: str)` | `ReservationReceipt`；仅整份计划所有腿从未尝试时撤回/到期释放，否则 BLOCKED |
| `request_cancel(cancel_id: str, intent_id: str, leg_id: str, context: ExecutionContext)` | `CancelAttempt`；首次持久 REQUESTED 才 `cancel_permitted=True`；相同 id 重入 False，更换 id 不能绕过锁 |
| `record_cancel_result(cancel_id: str, result: object = None, *, error_code: str | None = None)` | `SubmitResult`；数字仅记录已调用，不确认撤销；异常/超时记 UNKNOWN |
| `note_event_gap(evidence_id: str)` | `LedgerStatus`；持久锁存 EVENT_GAP |

`register_context` 是显式可信控制登记/初始化入口；当前契约没有独立control sequence，不靠本地clock猜控制新旧。N4普通submit/cancel/行情更新必须使用 `compare_and_register_context(new_context, expected_context)`；它在数据库 BEGIN IMMEDIATE 内按稳定scope比较完整canonical上下文，另一个连接撤权/降预算/新epoch/禁用后，旧调用CAS拒绝且不能写入任何快照/资源。CAS只允许同control更新观察、receipt、view，合法控制变更通过独立显式register_context执行。begin_attempt在同一BEGIN IMMEDIATE内比较完整最新context；request_cancel在同事务核对已登记控制字段、当前account+epoch+order_id唯一以及已知FrontID/SessionID无冲突。order回报路由变化持久隔离，本地编号冲突/隔离回报阻断撤单；不靠payload OWNED/route布尔承诺native所有权。

`register_context` 必须在每次预留/首次尝试前提供最新快照、政策、绑定、proof 和 clock。它不续期 sampled_at/source_at，不清除对账锁存，也不占用资源 revision。资源事务递增 revision；快照/上下文必须声明当前 revision；同 snapshot_id 的业务字段不得悄悄改变（仅 ledger_revision 更新例外）。不同合同 snapshot 用 resources 显式传入，资源范围含已登记的全部同账户合同；不得把“本合约零仓”当全账户零仓。现金按账户共享，总仓位按全部 long+short 计量。收到新上下文后原计划的 snapshot_id/policy_id 仍须匹配，begin_attempt 用新上下文重验冻结腿，不改变拓扑。

资源收据 `ReservationReceipt.reservation_ids` 精确等于 `tuple(leg.leg_id for leg in plan.legs)`（同序）；SQLite reservation_id 直接使用 leg_id，Reservation.leg_id 是 N2/N4 的 typed 映射键。`Plan.ledger_revision` 是 reserve/refresh 事务前版本，Receipt.revision 是成功事务后版本，view 是当前版本；N3 查库核对完整收据，不信 payload 自填。`ReservationView.gross_position` 仅为实际多头+空头总持仓，不包含 OPEN 承诺；OPEN 承诺由活跃 reservations 单独计算。`view.cash_reserved` 是 HELD+REFLECTED 的现金总承诺，供 policy.max_cash_reserve 检查；SDK available 仅扣缺当前吸收证据的现金。HELD 无论 not_proven_reflected 是否误报零都按 volume 保守扣减；只有合法屏障 REFLECTED 可消除重复占用。OPEN resource_key.bucket 固定 ALL，其 leg_id 规范后缀为 `:OPEN:-:[0-9]+`；CLOSE 为 `:CLOSE:(TD|YD|ALL):[0-9]+`，N3 强制规范 leg_id，N2 可按后缀判断 phase。

`reflected_evidence_id` 精确为 proof.proof_id；当前snapshot.proof_id必须同值且proof具体覆盖该leg或全部相关event，N3 additionally 查持久 proof、inventory与snapshot实际匹配。吸收以reserve/refresh时持久化的SDK基线衡量，且必须是直接证据：活跃 CLOSE 腿要求当前快照对应今昨桶冻结量 >= 本腿未成交余量（余量含部分成交后剩余；可用量下降可能只是本腿成交，不构成冻结证据），且持仓已体现成交减少（<= 基线持仓 − risk_filled）；活跃 OPEN 腿要求当前 available <= 基线 available − 本腿现金上界。终态释放同样要求持仓实际变化（CLOSE 持仓 <= 基线 − risk_filled，OPEN 持仓 >= 基线 + risk_filled）且平仓桶冻结解除。证据不足保持 HELD/未 settled，绝不消除占用；后续不相同snapshot或新成交使原吸收证据不足时继续保守占用。RELEASED保留原腿、数量、现金和proof_id审计；不等价于删除记录。

`refresh_pending_open` 保留完整Plan的legs/index/depends_on以及原intent/source_key，只更新未尝试OPEN现金承诺及当前Plan的snapshot/policy/expiry/revision/plan_id；新expiry不晚于原intent。原/新Plan规范payload/hash永久存储在events，REFRESH记录old_plan_id→new_plan_id；已尝试CLOSE腿继续关联原Plan。get_leg(OPEN).plan为新Plan；N4取新receipt后重新登记含最新view的context再begin_attempt，旧receipt拒绝。原向未清、明细缺口、未决撤单/提交或额度不足整事务拒绝。

N4 顺序为 register → 显式register_context初始化 → view/纯 planning → reserve → 用收据和最新 revision 更新 context/snapshot 的 ledger_revision → compare_and_register_context(new_context, expected_context) → begin_attempt → 仅 send_permitted 时调用 FakeNative 一次 → record_submit_result。新行情快照可供 begin_attempt 重验；价格、方向、数量、leg_id 保持冻结。begin_attempt 校验完整收据及全账户当前占用，自身预留认领一次；其它事务或快照变化仍重算。

所有已锁存 attempt 在新进程中都不重发。首次打开含未决 attempt 的 journal 会标记恢复需求；只有明确模拟完整屏障可解除可恢复缺口。写失败/损坏/锁超时立即关闭本对象的提交权限；底层无法写时仅保留失败锁存，绝无内存成功投影。EVENT_GAP/身份或数量冲突不能被 heartbeat 或简单 recover 清除。

## 回报与模拟范围

`CallbackContext` 为 frozen/slots/kw_only 数据类，完整字段为 `event_id: str; kind: EventKind; received_at: datetime; session_epoch: str; source: Source; account_key: str | None = None; strategy_key: str | None = None; simulation_scope: bool = False`。simulation_scope 仅表示由离线模拟装配器注入的范围；source 非 SIMULATION 时为 True 一律拒绝。它不能承诺 native ownership。

`normalize_event(raw: Mapping[str, object] | object, callback_context: CallbackContext) -> ExecutionEvent` 仅读取原始字段白名单，忽略任意 raw 账户/策略/ownership/verified/密码/errMsg。不使用 callback_context 补合约、hedge、交易日或成交 id。可识别的数值字符串/SDK 枚举转换为契约字段；日期仅来自显式 TradingDay，日内时分秒不拼接接收日期。Memo 原文不持久存储；只保留合乎稳定 leg_id 格式的关联值。未知状态为 None，不猜成功。生成的 ownership 始终 UNKNOWN；simulation 范围通过 evidence_id 前缀及明确策略标识进入持久事件，N3 仍需校验 journal、全部业务身份及唯一别名后才可认领。原生事件无这种模拟证据，只能隔离。

`Reconciler(ledger: Ledger)` 是无端口委托器；`ingest(event: ExecutionEvent) -> IngestResult` 调用 ledger.ingest_event。N4 可直接调用 Ledger.ingest_event，或使用 Reconciler。

关联必须具有 simulation 来源范围、匹配已提交腿及唯一已知别名；先成交/调用返回前回报先隔离，record_submit_result 或已唯一关联的 order 事件会重试待归属事件。另一 epoch/account/day 不复用 local ID；交易唯一键缺字段时持久隔离。订单累计和唯一成交之和取 max；trade_gap 明细未齐不 settled。撤单状态独立，交易照常入账。REFLECTED/RELEASED 仅 recover 的完整模拟 inventory+具体 leg/event 覆盖+匹配持仓快照可产生，本地 -1 且无矛盾事件是唯一已尝试的即时释放分支。

## 契约协调记录

2026-09-14 N3 已报告 N1b：原 ExecutionEvent 强制 TRADE id/业务字段及 ORDER/CANCEL alias、身份字段非空，与缺字段持久隔离相冲突；需 UNKNOWN 允许 null，N3 不编造字段。另已报告 CANCEL_OWNED 校验需独立于行情时效/submit readiness/全账户 proof。N3 不编辑 contracts.py。
