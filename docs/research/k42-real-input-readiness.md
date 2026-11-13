# K42：真实输入就绪清单（2026-11-13 批）

两份候选当前均为 **BLOCKED_NON_HOLDOUT_BOUNDARY_MISSING**。本次核对了 2 个 collection manifest、60 个分片 manifest 及既有构建/独审元数据；r1/r2 价格载荷打开数为 **0**、整包 loader 调用数为 **0**。因此这里能交付具体差项，不能交付新测得的非 holdout K 线质量、延迟或可见比例。

**r1/r2 非 holdout 边界无法证成，整包 loader 会读取全量 bars，故不能安全启动该生产路径。** 不能先加载整包再按日期过滤；没有运行真实数据预检或 `synthetic=false` 实验，也没有测得 `visible_bars` 对全部日线拒绝。

以下区分三种证据：**本次核对**指 M01 对 manifest/元数据的读取与哈希核对；**构建声明**是已冻结 build-report 的数字；**旧独审**是 2026-09-15 独立审核的全包数字，未在本次重扫价格。表中的 M01 raw 0 只证明元数据作业完成，不把旧审计 PASS 或构建声明升级为本次非 holdout 验收。NA 表示本次范围内无法证成，绝不表示 0。授权来自 DECISIONS J10 和 NEXT-BATCH §4 K42；只汇总，不补字段。

表 1：候选布局与请求边界。本次核对；每份 30 个具体合约 SHFE.rb2301—SHFE.rb2506，每个分片同一个 bars.csv 混装 60/86400 秒两个周期。声明的 bars 内容哈希尚未在本次重算，manifest 一致性不能代替价格内容验收。

| 候选 | collection manifest SHA-256 | 分片 manifest 核对 | 输入种类 / 合同版本 | 命令/原生退出 |
| --- | --- | ---: | --- | --- |
| r1 | `09e7353c3094d42e777ce87bcd54a90a3384d2050005cd422fc073db32951e8e` | 30/30，差异 0 | HISTORICAL_RECONSTRUCTED / v2.1.2 | M01 / 0 |
| r2 | `6c9e3e4e59927779a77519e0e5ed930796559cd446c6885d1a72ca9142d6cef4` | 30/30，差异 0 | HISTORICAL_RECONSTRUCTED / v2.1.2 | M01 / 0 |

两个请求的 start=`1641700800000000000`（2022-01-09T12:00:00+08:00）、end=`1750651200000000000`（2025-06-23T12:00:00+08:00）、as_of=`1731654000000000000`（2024-11-15T15:00:00+08:00）一致。这是请求/快照元数据，**不是实际 bar 覆盖范围，更不是非 holdout 范围**。collection schema 为 `ak_probability_input_collection_v2.1.0`。

表 2：按候选和周期列出 K 线差项。数量来自旧独审全包；r2 独审记录 30 份 bars.csv 与 r1 逐字节相同，故引用 r1 周期计数。其余列没有可证明的安全非 holdout 分母，不从全包总数推算。

| 候选/周期 | 旧独审全包行数 | 本次非 holdout 行数/日期覆盖 | 质量×周期 / 时间依据×周期 / 降级原因×周期 | available_at−end 最小/中位/最大 | visible_bars 放行比例 | 命令/原生退出 |
| --- | ---: | --- | --- | --- | --- | --- |
| r1 / DAILY (86400s) | 7,268 | NA / NA | NA / NA / NA | NA / NA / NA | NA | M01 / 0；旧独审引用；载荷 NOT_RUN |
| r1 / MINUTE (60s) | 2,482,408 | NA / NA | NA / NA / NA | NA / NA / NA | NA | M01 / 0；旧独审引用；载荷 NOT_RUN |
| r2 / DAILY (86400s) | 7,268 | NA / NA | NA / NA / NA | NA / NA / NA | NA | M01 / 0；旧独审引用；载荷 NOT_RUN |
| r2 / MINUTE (60s) | 2,482,408 | NA / NA | NA / NA / NA | NA / NA / NA | NA | M01 / 0；旧独审引用；载荷 NOT_RUN |

表 3：已有全包质量汇总，不按周期或非 holdout 重计。构建声明逐合约求和与旧独审总数一致；这些维度不是独立可相加的样本。

| 全包维度 | r1 | r2 | 证据及命令/原生退出 |
| --- | ---: | ---: | --- |
| bars 总数 | 2,489,676 | 2,489,676 | 构建声明 + 旧独审；M01 / 0 |
| READY / temporal_basis=reconstructed | 1,788,600 / 1,788,600 | 1,788,600 / 1,788,600 | 同上；M01 / 0 |
| DEGRADED / temporal_basis=unknown | 701,076 / 701,076 | 701,076 / 701,076 | 同上；M01 / 0 |
| 原因 calendar_coverage_missing | 701,076 | 701,076 | 构建声明；M01 / 0 |
| invalid / grid_misaligned / outside_session_window / imported_at_missing | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | 构建声明；M01 / 0 |
| 其他质量状态/时间依据/原因及其非 holdout 交叉计数 | NA | NA | 本次不读价格；M01 / 0；载荷 NOT_RUN |

旧独审全扫说明 reconstructed 行的 `available_at=end+60 秒`，unknown 上下文行保留 `available_at=imported_at`；这是重建政策和旧审计结果，不能据此声称全包或非 holdout 的最小/中位/最大延迟都是 60 秒。首发与修订历史未证明；imported_at 也不是历史 first-seen。生产 [visible_bars](../../src/ak_trading/v2/data.py:435) 及索引构造还检查 READY、已知时间依据、complete、available_at 和时钟网格；intraday 会排除当日日线，放行比例依赖具体决策时刻和调用参数。没有合法范围、截止序列和实际调用时，该比例为 NA。

表 4：交易节（旧独审全包），同一产品日历被分片共享，1,795 不是将 30 份重复日历累加。r2 独审证明 sessions/calendar-assembly 与 r1 字节相同。

| 候选 | SessionWindow 总数 | VERIFIED / SCENARIO | 交易日覆盖及日历版本 | 命令/原生退出 |
| --- | ---: | --- | --- | --- |
| r1 | 1,795 | 368 / 1,427 | 2023-01-03—2024-11-15，共 452 日；assembly v1.1.0，SHFE-rb-v1.0.0 | M01 / 0；旧独审引用 |
| r2 | 1,795 | 368 / 1,427 | 同 r1，旧独审逐字节一致 | M01 / 0；旧独审引用 |

其中 2023-01-03—2023-05-25 为 94 个 VERIFIED 交易日，2023-05-26—2024-11-15 为 358 个 SCENARIO 交易日，保留原证据状态。452 日不能统称“已核实日历”。生命周期原始行在日历窗口前后仍被保留，相关上下文 DEGRADED/unknown；不能把请求 start/end 冒充这个交易节范围。选约证据另有 904 个槽（452 日×2 周期）、6 个入选合约，非全体 30 个；计数日历额外引用 139 个 DB-observed 日（2024-11-18—2025-06-16），合计 591，不升级成 VERIFIED SessionWindow。

表 5：交易规则全包字段（旧 H1 元数据/旧独审，非本次逐规则重扫）。两份均 244 条，证据均为 SCENARIO；以下 null 是未知，不替换为零。

| 字段/属性 | r1 | r2 | 证据及命令/原生退出 |
| --- | --- | --- | --- |
| 规则证据 | SCENARIO 244 | SCENARIO 244 | H01 + 旧独审；M01 / 0 |
| open_fee 缺失 | 0/244 | 0/244 | H01；M01 / 0 |
| close_fee 缺失 | 244/244 | 244/244 | H01；M01 / 0 |
| close_today_fee 缺失 | 244/244 | 235/244（另 9 条已填） | H01 + r2 旧独审；M01 / 0 |
| long_margin_rate / short_margin_rate 缺失 | 0/244；0/244 | 0/244；0/244 | H01；M01 / 0 |
| 保证金率的旧集合 | .06/.07/.08/.09/.10/.12/.13/.15/.20 | 同左 | H01；M01 / 0 |
| participation_rate 缺失 / 零 | 0/244；244/244 | 0/244；244/244 | H01；M01 / 0 |
| slippage / impact | 声明置零假设；逐字段 null 计数 NA | 声明置零假设；逐字段 null 计数 NA | 分片 limitation；M01 / 0 |
| available_at 晚于 effective end | 0/244 | 9/244 | H01；M01 / 0 |

表 6：逐合约规则差项。每行 A 表示：旧汇总中所有规则均 SCENARIO，open_fee 和双向 margin 无 null、close_fee 全 null、participation 全零；slippage/impact/participation 的零是假设，未获历史执行证据。每个合约的规则条数、完整生效区间及非 holdout 子区间未由本次安全元数据证成，均 NA；不能由全包边界补写。r2 九条平今费填充值按旧独审分配列出，其余规则仍 null。

| 合约 | 全体通用字段 | r1 平今费 | r2 平今费 | 已知区间事实（旧独审）；完整非 holdout 生效区间 | 命令/原生退出 |
| --- | --- | --- | --- | --- | --- |
| SHFE.rb2301 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2302 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2303 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2304 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2305 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2306 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2307 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2308 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2309 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2310 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2311 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2312 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2401 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2402 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2403 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2404 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2405 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2406 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2407 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2408 | A | 全 null | 2 条已填；其他 null | 九条范围起于 2024-07-30 21:00+08；该合约末条尾端 2024-08-15T15:00:00+08:00；NA | M01 / 0；旧元数据引用 |
| SHFE.rb2409 | A | 全 null | 3 条已填；其他 null | 九条范围起于 2024-07-30 21:00+08；该合约末条尾端 2024-09-18T15:00:00+08:00；NA | M01 / 0；旧元数据引用 |
| SHFE.rb2410 | A | 全 null | 4 条已填；其他 null | 九条范围起于 2024-07-30 21:00+08；该合约末条尾端 2024-10-15T15:00:00+08:00；NA | M01 / 0；旧元数据引用 |
| SHFE.rb2411 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2412 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2501 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2502 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2503 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2504 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2505 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |
| SHFE.rb2506 | A | 全 null | 全 null | 完整生效区间 NA；非 holdout NA | M01 / 0；旧元数据引用 |

r2 填入的九条规则 first-seen 是 **2026-09-15T06:11:21+08:00**（1789423881000000000），晚于 2024 年生效窗口；旧独审的 `visible_rule` 在窗口内返回 None，在 2026 年 first-seen 时窗口已过，也无适用规则。因此“填了 9 条”不等于历史决策可见。r1 末规则尾端原为请求 end；r2 对全部 30 合约作生命周期尾裁剪，未改为 PIT。其余规则 `available_at=effective` 仍是 SCENARIO 假设；price limits 未知、首发及修订历史未证明，r2 增量没有补齐其余执行字段。

表 7：留出范围及停止位置。边界未知本身是实际 blocker，不能通过空目录或未搜到字符串自动解除。

| 项目 | r1 / r2 现状 | 证据及命令/原生退出 |
| --- | --- | --- |
| 最终 holdout 预留起止 | NA；本次核实范围未提供可绑定区间 | M01 / 0；两次候选内可选 rg raw 1，空 stderr = NO_MATCH |
| 非 holdout 可读覆盖、分片/文件、截止范围 | NA；无可验证的独立 read-scope | M01 / 0；不读价格 |
| 权威 holdouts 目录 | 元数据检查时未存在；不是读取许可 | M01 / 0 |
| experiment-registry | 5 个历史 smoke 条目有 holdout ID，无可绑定的时间区间 | M01 / 0；仅注册元数据 |
| 本次实际停止处 | 价格加载之前：BLOCKED_NON_HOLDOUT_BOUNDARY_MISSING | M01 / 0；生产/预检 NOT_RUN |
| 未来满足其他前提后的 synthetic=false 日历门槛 | bundle 含 SCENARIO sessions，将触发 HISTORICAL_CALENDAR_COVERAGE_NOT_VERIFIED | 当前源码静态核对；实验 NOT_RUN |

停止顺序的依据是 [整包读取](../../src/ak_trading/v2/data.py:99) 会遍历 manifest 文件 `read_bytes()` 并解码完整 bars，无法用载入后过滤保护混装数据；[真实预检](../../scripts/preflight_v2_research.py) 要求另行绑定安全读取范围。若配置、合同锁、payoff 依赖等其他前置条件均成立，M5 [experiments.py:1497](../../src/ak_trading/v2/validation/experiments.py:1497) 明确检查 `not synthetic` 且任一 session 证据不是 VERIFIED 就抛上述 StageIncompleteError。此处是静态的条件判断，不宣称已运行 CLI，更不宣称它是所有配置下第一个无条件失败点。下游还保留平仓费未知、九条规则历史不可见、执行参数零假设等差项，不修填、不跳过。

M01 命令（cwd=`D:/Projects/AK-trading`）：`C:/AKR41110/venv-full/Scripts/python.exe -B -X utf8 C:/AKH71113/measurements/measurement.py metadata`，原生退出 **0**；检查 60 个分片 manifest，哈希差异 **0**；价格载荷/全包 loader **0/0**。不是 pytest，passed/failed 不适用。两次可选搜索的精确 argv、原生退出 **1**、空 stdout/stderr 及 NO_MATCH 分类保留在外置 `source-metadata.json`。这两次都是各候选目录内 .json/.md 的有限搜索，不是全机无留出边界的证明。旧独审原始 CLI exit 未在本次记录中重取，不用其 verdict PASS 替代 native exit。

H01 为 `C:/AKH1107/h7-rule-metadata.json`；另引用同目录 `h7-readiness-metadata.json`、`h7-input-layout-metadata-02.json`，本次逐一核对既有哈希。主元数据来源为 r1/r2 的 build-report.json、review/review.json 及 collection/shard manifest（完整 pins 在外置记录）；其中旧独审不是当前源码、边界和时钟条件下的重新验收。codebase-memory 查询与 coverage 后，对 metadata_changed 源码及 experiments.py 部分解析缺口作了精确源码补读；没有重索引或服务操作。

本次元数据结果 `source-metadata.json` SHA-256 为 `efc8b35c75583962c9a3b5dff75051703e34ce0bb9a1bd8b40e62365fa8ef82f`。脚本、source pins、精确命令、原始日志和封存副本置于仓库外 `C:/AKH71113/measurements/`；仓库仅留本汇总。未访问网络、DB、TqApi、凭据或 .env，未训练、推理、回放、回测、消费最终 holdout 或重启服务。保持 `enable_trading=False / observer=hold / broker=NOT_RUN / real send/cancel=0/0`。
