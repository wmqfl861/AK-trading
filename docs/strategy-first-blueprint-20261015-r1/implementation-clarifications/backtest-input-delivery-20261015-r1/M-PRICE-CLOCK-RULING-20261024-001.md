# M 日线价格时钟：实际证据裁决与一次精确补证

原 BIP1 作者：Astra/max `/root/astra_backtest_input_delivery_plan_20261015`。本裁决接续 `DAILY-M-PRICE-A-FACTS-20261024-001.md`、`DAILY-M-PRICE-CLOCK-EVIDENCE-20261024-001.md` 及其 `-DISPOSITION.md`；三个既有文件原字节保留。一次独立计划意见已经处置，本次是预声明的来源终态后作者裁决，不重开计划 PASS 门。

**裁决：原包 M 的 2026 `available_at` 确认为 pipeline import bound；它不构成历史市场晚到证明。现有证据尚不能把 M 原始日线标签关联到合法交易日及完成时点，因此本次不发布数值延迟或可消费的 reconstructed research A。立即由原 Sol 读取已捕获官方正文中的一个真实未消费链接，专门解除该对应关系；无新 query，不制作零合格 consumer。**

本文件是作者技术裁决和精确后继工单，不是两份产品的独立硬审核。现有来源 28 文件、谱系 17 文件已终态且 WRITE_STOP，Root 应立即交独立 ZCode 联合硬审；新增动作只写新的 successor，不改变正在审核的文件。硬审结果独立记账，作者不发产品 PASS。

## 已核对的证据与唯一生效决定

本作者读取两份实际产品的相关字段、原始正文关键段落、实际 locator，并复核下列主要 FileRef。谱系的完整逐行重算、14 个反例及源请求的完整验证是开发者报告，待独立硬审；作者本次没有重跑两条谱系命令，也没有声称逐个独立审完全部 36 个 citation。下表的 C 编号对应已冻结 `M-PRICE-CLOCK-EVIDENCE.json`，P 指已冻结 `M-PRICE-PROVENANCE.json`。

| 实际证据位置 | 本作者采纳的决定 | 适用范围 / 版本 | 仍不支持的字段或关系 |
| --- | --- | --- | --- |
| C02–C04；同期 `handover-1.json:/export_evidence/real_input_01/defect`、`/real_input_02/deviation_note` 与 `/real_input_02/manifest_sha256`；P `/clock_policy_state` | real-input-02 修正的是 `available_at ← imported_at` 映射。逐行原值永久留存为导入审计边界；不能把 2026 导入日期称作 2023–2024 市场实际晚到。 | 固定五原件及其同期 handover；当前源码只作解释性佐证。 | actual market first_seen；旧运行源码/SDK 精确版本；逐行 batch 身份。 |
| P `/metadata_summary`、`/task_receipts`、`/series_receipt_relationships`；谱系 VERIFICATION `/verification_report` | 55 条当前记录与 55 条备份均为 54 `ok` + 1 `skipped_db`，无白名单字段冲突。54 条仅是系列候选。 | 当前冻结 CSV 与这一个固定 JSONL 记录块。 | 54 个候选都不证明逐行 batch。空导出 imported_at/batch_id 不证明数据库原列为空，不可回填。 |
| JSONL 第 2604 行；谱系 VERIFICATION `/skipped_task_relationship` | `DCE.m2401` 为 `skipped_db`、rows=0、无 batch_id；`series_receipt_match=false`。其 242 条冻结元数据保留，关系为 `UNRESOLVED_FROM_THIS_BLOCK`。 | 该任务与现存行的关系，不能外推其他批次。 | 不得推断这些行必来自此前某次导入；不得删除 bars 或人口成员。 |
| doc001 第 63、67 行；doc002 第 129–154 行；C10–C13 | 86400 是 API 日线周期参数；datetime 是 K 线起点时间，Unix epoch 纳秒，文档采用北京时间表述；close 是 K 线结束时刻的最新价。 | doc001 明确 1.7.0；doc002 是此次捕获的 latest，未给数字版本。 | 日线标签到 M 交易日、跨夜/节假日归属及合法 daily end 的对应关系。不能用 raw timestamp + 86400。 |
| C14–C16、C24；已固定本机 3.10.3 downloader 说明 | date 输入指交易日、输出保留源 datetime 的事实，只用于理解当前 API 的输入/输出表达。 | 本机 3.10.3 文档/代码，历史运行关系 UNPROVEN。 | 输入 date 的语义不自动证明旧 CSV 输出标签映射；不证明旧下载使用同版本/路径。 |
| doc002 第 418、434–439 行；C21–C27 | `is_serial_ready` 和 downloader readiness 只证明已收到订阅数据；收到网络更新不证明所有请求结束。 | 各 API 实际说明的对象和请求范围。 | M 市场日线完成、发布或最终修订时点，不能由客户端 readiness 推出。 |
| doc002 第 176–190 行；C28 | `get_kline_data_series` 的“不更新”描述的是返回对象。 | 这是另一 API 的对象语义。 | 不是服务器历史 vintage 不可变证明，也不证明旧下载用了该 API。 |
| doc002 `query_symbol_ranking`，第 508–533 行；C29–C30 | 18:30–19:00 是合约成交/持仓排名数据更新时间。 | 排名接口及该段自身范围；接口名以实际正文为准。 | 不能作为 M OHLC 发布时点、19:00 价格 A 或价格安全上界。 |
| C31–C33；`known_later_information` | 保留已捕获 release-history 的具体修复与适用版本，不把一般修复说明硬配到某个 CSV 行。 | 实际版本/功能范围；捕获时间不是历史市场观察。 | 没有定位任何具体冻结 M 行的修订链。未知完整 vintage 仍是限制，不新增统一拒绝门。 |
| C06；原 manifest `/clock_policy`；C34 | 原件自己披露 `raw_timestamp_only_no_session_evidence`，目前没有可用 M 完成边界。RB 的 60 秒仅属于 `rb-price-availability-v1` 研究假设。 | 当前 M 证据范围；RB、AU 既定政策不扩用。 | 本次无可支持的 M 数值延迟；不能把 HTTP 200 或字段名变成 60 秒。 |

按 P 的 metadata-only 核验，原包保留 11,851 条 M 记录、55 个字面 symbol，其中 tqsdk 11,819 条、edb_free 32 条；按原时间标签处于 W 的 3,448 条均为 tqsdk。上述数字不是 census、T、样本数、容量或来源完整性结论。所有原行、版本、顺序、2026 值与空原字段继续保留，不能用这些数字删除前缀、最长尾段或到期成员。

四类时钟的正式状态分别为：

1. `original_import_bound`：原 CSV `available_at`，逐行原值只读保留，不改名冒充市场观察，不用 aggregate min/max 替换原值。
2. `actual_market_first_seen`：当前没有历史观察证据，保持 null，reason=`NO_OBSERVED_MARKET_RECEIPT_IN_PINNED_EVIDENCE`。
3. `reconstructed_research_A`、`adopted_reconstructed_policy_version`、`m_reconstruction_delay_seconds`：本裁决均为 null，decision=`M_DAY_LABEL_COMPLETION_RELATION_NOT_YET_ESTABLISHED`。该 decision 是裁决状态，不是可运行的价格政策版本。
4. `manifest_export_clock`：原 manifest.created_at=`2026-09-07T11:07:18+08:00`，仅表示导出时钟。

M1 §5.3 允许正式选定的重建研究时钟。本作者不要求每个历史 bar 必须有当年观察 first_seen，也不要求完整 provider revision history 后才准许任何重建。当前没有采用延迟的具体原因，是标签→交易日→合法完成的关键关系尚未成立；不是以“没有实测固定延迟”或“vintage 不全”新增普遍拒绝标准。关系补齐后，作者可以事前选定并说明研究假设及限制，不能把假设冒充实测最大时延。任何实际已知 late/revision/observed 信息都须绑定原记录，保留更晚界限，无法兼容则明确拒收该版本，不能由一般导入解释或重建假设提前。

## 立即执行的一个后继来源动作

**唯一实际动作：复用已经捕获的 locator，读取官方“合约, 行情和历史数据”页。** 其作用直接对应缺失的 K 线标签、交易日、时间对齐、交易时段与日线组成说明，是否充分须看实际正文。不是新搜索、换词重试或重新抓已成功的 API 页。

- locator 原件：`m-price-clock-source-001/raw/m-price-semantics-doc-002/source.html.txt`，404976 bytes，SHA-256 `1229c727991c7b3786a7d236dd51e5f69952e727bcdcec432529bb0d15d1ec13`。
- 整个 anchor 的 Unicode codepoint 范围 `[316313,316428)`；其中 href 字符串从 316349 开始，两者含义不同。作者已重新切片验证整个 anchor 与证据记录相同。
- 原 href：`../usage/mddatas.html#mddatas`；解析 URL：`https://doc.shinnytech.com/tqsdk/latest/usage/mddatas.html#mddatas`。
- 原文：`<a class="reference internal" href="../usage/mddatas.html#mddatas"><span class="std std-ref">合约, 行情和历史数据</span></a>`。
- request ID：`m-price-semantics-doc-003`。保留实际 URL/fragment 与去重后的资源身份；禁止以去除 fragment、换 host/格式/输送方式作为第二次机会。

本裁决单独追加 **0 query / 最多 1 URL**，scope=`M_LABEL_COMPLETION_RELATION_001`。先前 M 初始预算已用 **1/1 query + 2/2 URLs**，没有余额；该记录永久保留。后继完成后 M 累计最多 **1 query + 3 URLs**，失败同样消费，不自动重试。其他品种余额完全不变。已记录的 `tqsdk.tools.download.html` 不在这一次 URL 授权内；不得把导航链接自动变成 crawl、第二页或新搜索。

原主 owner `/root/sol_backtest_input_continuation_20261024` 继续 `gpt-6.1-sol/max`，用 followup_task；不另建替代 writer。唯一写根为此前不存在的 `D:/AKS261024/R1/m-price-clock-source-002/`，精确允许：

- `WORK-ORDER-BINDING.json, INPUT-PINS.json, OUTPUT-PRECONDITIONS.json, REQUESTS.json, CAPTURE-TERMINAL.json, M-PRICE-DAY-SEMANTICS.json, VERIFICATION.json, FROZEN-DELIVERY.json, SUBMISSION.md`。
- `raw/m-price-semantics-doc-003/{REQUEST.json,RESPONSE.json,RESULT.json,stdout.txt,stderr.txt}`；只有实际响应含非空相应字符串时，才生成 `source.markdown.txt` / `source.html.txt`。

不新增可消费 bars、MarketBar/FrozenInput、日历/人口包或公共代码。原 provenance owner 维持 WRITE_STOP，硬审若有失败由同一实际 owner 修复。主 owner 不是唯一工作者，不得覆盖其他人的编辑或旧证据。必要失败残件留在对应的已声明新输出中；不能为隐藏失败覆盖旧路径。

执行前读取当前角色/流程权威和 TqSdk、web-search-stack skill；按既有 REQUESTS 的真实输入范围核对旧 RB/CU/AU/M 请求、18 snapshots 与已存 TqSdk/维护来源的相同资源身份。若已有相同原件，复用并记录 0 新请求；若当前预算身份已经有终态，不重放。无需广搜文件系统或查看凭据。将必需输入与所有生成输出先分清，前者须存在且 pins 匹配，后者先检查不存在，不以失败 Read 探测未来文件。

继续使用原已接受 capture 机制，唯一入口为本机 `http://127.0.0.1:3002`；仅上述官方 URL，105 秒 socket、115 秒 process、retries=0、shell=false，实际网络 max1。无新的 health/query/中性控制、服务重启、SDK、行情下载或数据请求。一次请求的最大 process 总时长就是 115 秒；离线封存检查至多 300 秒。每次至多 1 GiB RAM / 256 MiB 输出，原 Root029 完整 reservations、6 GiB/4 GiB floors 及旧 UNKNOWN charges 全部保留。

原 Root029 的策略 lane 已覆盖 M 来源动作；实际启动仍使用其 fresh 物理 admission 和唯一占用规则。独立硬审或 forward capture 活跃时，新来源进程排队；静态读取与新根准备可以先完成。该排队只由真实运行占用引起，不是新增批准。Root 记录原来源释放/审核结束或 capture 结束后，owner 在现有授权内立即执行，不等另一轮确认。旧冻结两根任何时候都不再写。

`M-PRICE-DAY-SEMANTICS.json` 至少逐项输出：日线 datetime 的具体对齐规则与时区；它和交易日/夜盘/节假日的关系；源文说明的 bar 时间范围/结束与更新条件；适用品种/接口/版本；原文是否只是一例或一般描述。每项附原件 FileRef、准确行/Unicode quote 或 JSON pointer，明确 supported/unsupported 和 source publication unknown。若文档解释普通时间对齐却未给 M 历史合法会话，必须把两种关系分开，不能自动补历史会话或批量生成 A。

验收是实际请求身份、预算、状态四轴、原响应字节、引用切片、版本范围、输入 pins 与冻结闭包能复算；没有新增产品逻辑时不制造镜像测试。原 source seal 曾在 CreateProcess 前遭 Windows 206 长命令拒绝，code_executed=false、0 输出、0 replay；以及 exploratory `LATEST_TITLE None` 纠正均保留在旧 VERIFICATION，不将其改成原生成功。后继也须分别记录进程退出、HTTP、API、内容、artifact 和产品 verdict。所有生产进程终止后才封存其流，不能 pin 活跃父 CLI 的空日志。

完成后立即通知本原作者并交 Root 安排独立产品硬审；不重发网络。作者读取实际新正文后，直接追加明确的适用决定：若补齐标签关系，则采用该关系并继续真实 M 会话/完成依据与唯一研究政策；若仍不足，则明确只有哪个关系被解除、哪个仍缺，以及实际可获取的下一项源，不用 consumer 或空兼容包掩盖缺口。任何更大预算、其他 URL 或新的产品实现必须有对应的窄作者裁决，不能在本单下自动扩展。

## 研究与运行边界

完整输入仍为 `INPUT_NOT_DELIVERED`、研究信用 0；raw-bound、容量/envelope、fit、holdout 未执行。本单解除的是来源语义关系，不提供 C/L/X、完整 T/R、历史 sessions、规则/费用/绝对 limits 或最终 FrozenInput 接受。后续保持已有效 `individual-lifecycle-intersection-v1` 与先 raw-bound、未拒绝才 mixed 容量的顺序，不从当前元数据计数推容量。

W=`[1672707600000000000,1729256400000000000)`，原 252/60/60、same312 绝对边界、inner/nested、全部 horizons/durations、完整前缀/真实最长尾段、200/30、500/60、purge/embargo、seen/holdout 与 M→I→SC 固定次序均保留。trading=false、observer=hold、broker=NOT_RUN、real send/cancel=0。没有模型配置、DB、SDK 构造、凭据或秘密环境存在性检查、服务/UI/部署写权。

所有续接继承 AGENTS §§7–10 和当前 PROJECT-MODEL-ROLES / PROJECT-WORKFLOW-RULES，特别是 `docs/maintenance/20261004-rg-no-match-r1/INCIDENT.md`、`docs/maintenance/20261004-grok-headless-cancel-r1/INCIDENT.md`、`docs/maintenance/20261016-r1-custody-path-normalization-r1/INCIDENT.md`：仅 optional rg raw1 且 stderr 空才是 NO_MATCH；其他退出不抹除。实际 cwd/argv/pins 从权威记录程序化绑定；Path 输入先归一化；不用 `$PID` 等自动变量；JSON 完整序列化验证后再创建最终路径或原子发布；失败残件保留，日志等 producing process 终止后封存。不能因 child0 隐藏 custody/产物失败，不能重用已消费的一次性请求。

## 本次主要实际输入 pins

原计划 17390 bytes / `2c35a4a0931737a0d6ea3cd4ae66f71b3ba7ae06c42518733175e62b11114b36`；原事实澄清 9225 bytes / `f52d41940aea6266c0e3cee5f4eeb2cc7c254b122e6700a0ec3351344131da3d`；原 disposition 7877 bytes / `2cdca586f923f2211f7f58af83c9cf572234f65f7d15305b7da89aada4fe4908`。固定五原件及先前权威通过这些文件与两路 FROZEN 继续闭包，不改旧引用。

```json
[
  {"path":"D:/AKS261024/R1/m-price-clock-source-001/CAPTURE-TERMINAL.json","bytes":15899,"sha256":"a6f2056971c4357b46b87df056766a8b72f95dbdcd5b31f0132d39793c968258"},
  {"path":"D:/AKS261024/R1/m-price-clock-source-001/M-PRICE-CLOCK-EVIDENCE.json","bytes":79276,"sha256":"6e33851f3d1640a79009a6df7ad8d9618586831e6d026b19e244e5e29c42e348"},
  {"path":"D:/AKS261024/R1/m-price-clock-source-001/FROZEN-DELIVERY.json","bytes":10207,"sha256":"92b66d88a16d1b96c559008a2e8b00914d6277e992f5758078185dc72d7a9db2"},
  {"path":"D:/AKS261024/R1/m-price-clock-source-001/VERIFICATION.json","bytes":7990,"sha256":"916c5eb58e091761a9979c34f9e863828e5ef869f58497d109180af1736057ad"},
  {"path":"D:/AKS261024/R1/m-price-clock-source-001/REQUESTS.json","bytes":311371,"sha256":"44d214d71b5762bb8855efd1363bb4e17e10ed1c211d6d39bec97f7aab11ef98"},
  {"path":"D:/AKS261024/R1/m-price-provenance-001/FROZEN-DELIVERY.json","bytes":21409,"sha256":"2f36c074c8c0b5bebe8eb851469d46a801d0ddff6a41e7104e912e6cf2b96b36"},
  {"path":"D:/AKS261024/R1/m-price-provenance-001/M-PRICE-PROVENANCE.json","bytes":23087537,"sha256":"6134efc843241fd99c23f7c08d2096506f671c19b94d9c7dbe05a877334e3d8a"},
  {"path":"D:/AKS261024/R1/m-price-provenance-001/VERIFICATION.json","bytes":10951,"sha256":"524d03afde6cae3547a5c7dbf42912a4fcad06507160dab90040d1eae36b75c0"},
  {"path":"D:/AKS261024/R1/m-price-clock-source-001/raw/m-price-semantics-doc-001/source.markdown.txt","bytes":32319,"sha256":"d8cf42968924e87b991fb0bc2836a848a744f1cef6d7b83da6ada0d5624e1228"},
  {"path":"D:/AKS261024/R1/m-price-clock-source-001/raw/m-price-semantics-doc-002/source.markdown.txt","bytes":116752,"sha256":"f863e81c48c1c49e9b6072f8bcb12d91d7703173cd3d84159fb83fca440c2608"},
  {"path":"D:/AKS261024/R1/m-price-clock-source-001/raw/m-price-semantics-doc-002/source.html.txt","bytes":404976,"sha256":"1229c727991c7b3786a7d236dd51e5f69952e727bcdcec432529bb0d15d1ec13"},
  {"path":"D:/Projects/AK-trading/docs/parallel-development-20261004-r1/coordination/current-zcode-capacity-20261015-001/PARALLEL-CAPACITY-FACTS-20261024-029.json","bytes":5415,"sha256":"0c86790a4861b1fcb9c6621db4fdcf91ba5585980dfe41c25d32b6d47eda58b4"}
]
```

作者证据复核 UTC：2026-10-24T06:26:10.369188+00:00。实际文件创建时间以后续作者 hash 回读为准，不将上述复核时点回填为发布时点。
