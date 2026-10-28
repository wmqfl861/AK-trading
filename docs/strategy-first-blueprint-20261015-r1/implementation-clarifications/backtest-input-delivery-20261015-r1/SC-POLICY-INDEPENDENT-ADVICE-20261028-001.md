我已经阅读了所有附加的策略文档（SC-DISPOSITION §3 提案、SC-ASSESSMENT 证据、BIP1 计划、R1 处置，以及完整的 M1 合同文档）。以下是要求的单一狭义独立咨询计划评审。本次交互完全是只读的：没有运行任何命令、测试或哈希校验，没有数据访问，没有写入。本评审不具有约束力——不存在 PASS 门槛，最终的采纳/有效版本决定权完全属于原始 Astra 作者。

---

## 独立窄域计划咨询意见：BIP1 SC §3 日期标签与 availability 提案（仅咨询）

**范围**：仅评审 `SC-DISPOSITION.md` §三（L49–69）的 `sc-tqsdk-daily-date-label-v1` 与 `sc-daily-price-reconstructed-v1`，对照 M1 契约（`01-M1-共用基础与时钟契约.md` §4.1/§5.3/§5.6/§6）、`SC-ASSESSMENT.json`、BIP1 `PLAN.md` 及 R1-NEXT 处置。按工单检查 lookahead/PIT、晚版本、时间戳语义、边界、证据限制与可执行性。

**总评**：未发现对现行合同的阻断性违反（blocking violations = 0）。政策定位为研究解释而非法律日历、对 M1 §5.3 "收盘＋事前冻结延迟" reconstructed 通道的引用正确、四时钟分离（A / pipeline_import_bound / first_seen=UNKNOWN / created_at）明确、条件性诚实（当前 actual_A_rows=0，采纳不消除人口/会话缺口）。以下 F1–F8 中，F6/F7 为无缺陷确认，其余为文本精度与可执行性建议，均由作者自行处置。

**F1（建议·证据表述）** §三 L51 对照 §一 L17。§三将"3200 个非空标签一致"列为标签解释依据，但 SC-ASSESSMENT 显示非空 trading_day 全部为 `reference_calendar_date` 来源（3276 = 3200 tqsdk + 76 edb），即非空值的存在本身来自与参考日历的比对/填充，其与当地日期的一致是构造性结果（或差异被置空），不能当作来源意图的独立确认；§一 L17 自身已限定"只证明存储一致性"，两处存在张力。建议把依据改述为"29734 行午夜打戳全一致 ＋ 零内部矛盾（metadata_mismatches={}）"，3200 仅作非矛盾佐证。不影响政策成立（已拒绝升格为日历接受，冲突行拒绝映射保守）。

**F2（建议·公式精度）** §三 L62–63 公式与 L67 散文存在可误读张力。公式字面 "max(A_base, every actually evidenced later availability/revision bound)" 未排除 `pipeline_import_bound`；本源 29734 行 available_at 全部非空且为 2026 导入界（§一 L18），若按字面执行会把 2026 导入时间 max 进 2023–2024 行的 A，整体研究窗 INSUFFICIENT——正是 L67 明令禁止的读法。建议在公式行内显式限定为 market-facing availability/revision bounds，排除 pipeline_import_bound/created_at/导出时钟，消除实现歧义。

**F3（建议·M1 词汇绑定）** §三 L58。"COMPLETE historically VERIFIED SC sessions" 未绑定 M1 `SessionWindow.evidence_status` 枚举（VERIFIED/SCENARIO/UNKNOWN，M1 L97）与未来 sessions.json 工件；"COMPLETE" 修饰对象（D 的会话集合完整性）易与 M1 行级字段 `MarketBar.complete`（M1 L120）混淆。建议显式：E(D) = max(end_nano) 取自 evidence_status=VERIFIED、trading_day==D 且完整覆盖 D 法定会话的 SessionWindow 记录，防止未来第二轮解释。

**F4（建议·区间语义）** §三 L57 对照 M1 §4.1 L743（"datetime_nano 保留源身份；start_nano/end_nano 承担区间语义，日线的 86400 标识不代表自然 24 小时会话长度"）。机制上正确，但未明说派生 MarketBar 的 start_nano/end_nano 必须来自已验证会话、午夜戳仅保留为 datetime_nano 源身份；实现者若取 start=午夜D、end=午夜D+1 即伪造自然 24 小时会话区间。建议补一句显式排除。

**F5（建议·后果声明与时区前提）** §三 L53、L60–61、L69。(a) 以 SC 常见会话形态作量级说明（仅为说明，非既定事实，E(D) 仍须来自已验证会话）：E(D)≈当日日盘收盘 15:00，A_base≈次自然日 15:00，即 D 日日线最早约在 D+1 收盘后（D+2 夜盘决策）可用；与 M1 §6 `daily_background_max_trading_days=1`（M1 L945）叠加成约 +1 个交易日的系统性滞后，跨周末/长假按墙钟自然日计。政策已接受 INSUFFICIENT 而不缩短延迟（L69），建议把该滞后量级写入文本，防止后续误诊为数据缺陷。(b) "一完整自然日"以固定 86400s 实现，仅在无 DST 时区与自然日恒等；Asia/Shanghai 适用，机制若将来泛化需改日历日算法，建议在 delta 定义处加一句前提。

**F6（无缺陷确认·PIT/晚版本/时间戳）** §三 L51、L67、L69 对照 M1 §5.3 L882、§5.6 L885–886。标签政策不触碰可用性，PIT 由 A 单独执法；d<A 不可见、d=A 恰等可见与 M1 `available_at_nano<=decision_nano` 闭端点一致，§四 L84 已列 A-1ns/A 反例；intraday 当前交易日终值过滤保留；不硬设 15:00；晚版不借早 A、版本冲突拒收；2026 导入不 max 也不抹除。未发现 lookahead 或修订覆盖通道。

**F7（无缺陷确认·条件性）** §三 L67/L69 对照 §一 L22、§二 L42 及 SC-ASSESSMENT decisions（actual_A_rows=0、sessions=MISSING、INPUT_NOT_DELIVERED）。E(D) 当前对全部 D 未定义，政策是参数化定义而非当前计算，不派空 consumer、不解除 census/L/X/sessions 缺口。自洽。

**F8（建议·反例清单）** §四 L84 对照 BIP1 PLAN §7（L117）反例纪律。条件验证清单覆盖 A 边界/时钟分离/晚版/人口守恒，但未显式列入标签政策的拒绝路径反例：非午夜行、缺时区行、trading_day≠当地日期冲突行、edb_free 行越入 tqsdk 标签映射、source 字段异常。当前真实数据零 mismatch，拒绝路径只能靠 SYNTHETIC 反例首测；建议按既有 SYNTHETIC 纪律补入（是否纳入由作者决定，不构成新门）。

**结论**：0 项阻断、6 项建议（F1–F5、F8）、2 项确认（F6–F7）。政策在 M1 §5.3 框架内以作者事前冻结参数的方式成立；处置（ADOPTED/PARTIALLY/NOT 与有效版本）归原作者。

---

此评审文本即为交付物，供接收方（receiver）保存；本人不进行报告写入，也不申请任何执行权限。
