# AK-trading 修改与修复清单（第四轮）

日期：2026-11-10 · 依据：[REVIEW.md](REVIEW.md) · 排期：[DEVELOPMENT-PLAN.md](DEVELOPMENT-PLAN.md)

> 这是建议，不是工单。每一项是否做、写集怎么划、语义怎么定，由计划作者按现行规则处置。行号对应 `main@b75d220`。
> 我没有改任何产品代码。下面的“改法”是我读代码后认为可行的方向，其中的代码片段是示意，没有在产品树里运行过；探针里的参考实现运行过，会单独说明。

编号规则：K0x 策略正确性，K1x 速度，K2x 仓库与工程，K3x 安全。优先级 P0 表示不修就没有有效的研究结果。

## 一览

| 编号 | 优先级 | 内容 | 对应发现 | 需要的决定 |
| --- | --- | --- | --- | --- |
| K01 | P0 | 入场确认在真实时钟和日历下可达 | X01 | 计划作者裁定语义 |
| K02 | P0 | 震荡状态的跨周期结论可被准入接受 | X02 | 计划作者裁定语义 |
| K03 | P0 | 全量回归基线与失败清单对照 | X20、Y03 | 无 |
| K04 | P0 | 研究运行前的可达性预检 | X03、X04、X05、Y01 | 无 |
| K05 | P1 | 真实形状的测试夹具与自然链冒烟测试 | X01、X02 | 无 |
| K06 | P2 | 策略库的三处小问题 | X06 | 无 |
| K10 | P0（分钟线） | 日历索引 | X10 | 计划作者确认写集 |
| K11 | P1 | 决策上下文的共享与缓存 | X11 | 计划作者确认写集 |
| K12 | P1 | 短日历多尺度 profile | Y02 | 无 |
| K13 | P1 | 模型拟合向量化 | X12 | J4 |
| K14 | P2 | `visible_bars_recent` 的起点二分；因子缓存统一清空 | X13 | 无 |
| K20 | P1 | 研究所需的小文件入库 | X21 | J1、J7 |
| K21 | P1 | 数据管线与因子库目录补齐或归档 | X21 | 你定去留 |
| K22 | P1 | CI 第二层；可选依赖缺失时跳过而不是收集失败 | X20、X21 | 无 |
| K23 | P2 | 依赖声明、Python 版本、`.gitattributes` | X23 | J8 |
| K24 | P2 | 入口文档与规则文件的失效指针 | X25 | 规则文件需你同意 |
| K30 | P0 | 仓库可见性、令牌、文档里的公网地址 | X30 | 你本人执行 |
| K31 | P1 | 行情服务与执行服务的默认端口分开 | X31 | 你定端口与部署窗口 |
| K32 | P1 | 客户端策略的合约改为部署时必填 | X32 | 你确认合约 |
| K33 | P1 | 影子运行接线 | X33 | 排在报告之后 |
| K34 | P2 | 不再把 `TypeError` 记成证据无效 | X34 | 计划作者裁定 |

## K01 入场确认在真实时钟和日历下可达（P0）

**问题**：见 REVIEW 的 X01。两个独立的原因，要一起修。

**涉及文件**：`src/ak_trading/v2/strategies/registry.py`、`trend.py`、`breakout.py`、`pullback.py`、`reversion.py`、`grid.py`、`martingale.py`，以及对应的 M3B 测试。

### 第一部分：到期判断改为“看到了哪根 K 线”，而不是“现在几点”

观察期的本意是“连续 n 根 K 线之内”。现在用决策时刻和到期时刻比较，把可用延迟也算了进去。

建议的语义：**观察期在出现一根收盘时刻晚于到期时刻的已完成 K 线时才算过期。**

涉及的比较（都把 `context.now_nano` 换成最后一根已完成同频 K 线的 `end_nano`）：

| 位置 | 现在 | 建议 |
| --- | --- | --- |
| `registry.py:1384` `advance_confirmation_count` | `context.now_nano > previous.expires_at_nano` | `bars[-1].end_nano > previous.expires_at_nano`（`bars` 在上面第 1380 行已经取到） |
| `registry.py:1312` `range_entry_setup` | `context.now_nano > previous.expires_at_nano` | 同上，用该函数第 1299 行取到的 `bars[-1]` |
| `trend.py:167` | `context.now_nano < previous.expires_at_nano` | `bar.end_nano <= previous.expires_at_nano` |
| `breakout.py:108` | `context.now_nano > setup.expires_at_nano` | `bars[-1].end_nano > setup.expires_at_nano` |
| `pullback.py:144` | `context.now_nano > previous.expires_at_nano` | `bar.end_nano > previous.expires_at_nano` |
| `reversion.py:80`、`grid.py:89`、`martingale.py:103` | `context.now_nano > state.setup.expires_at_nano` | 用最后一根已完成 K 线的 `end_nano` |

建议把这个判断收成 `registry.py` 里的一个函数（比如 `setup_expired(context, setup)`），六个文件都调用它，避免以后再出现不一致。

为什么这样改是安全的：

- 决策时刻等于收盘时刻时，新旧判断的结果完全相同。现有 M3B 测试都落在这种情况，预期不变。
- 策略评估由 K 线的可用事件触发。没有新 K 线时不会有新的评估，所以“按 K 线判断”不会让观察期在没人看的时候无限延长；下一根 K 线一到，它的收盘时刻晚于到期时刻，观察期照样过期。
- `trend.py:167` 原来用的是严格小于，决策恰好等于到期时刻时会新建观察期，而 `advance_confirmation_count` 用的是严格大于，认为还没过期。两处本来就不一致，统一后消除。

另一种改法是把到期时刻整体后移一根 K 线并保持按时刻比较。它能容忍不超过一根 K 线的延迟，但延迟更长时问题会回来，所以不建议。

### 第二部分：日线的到期时刻按交易日算

`registry.py:459–467` 现在返回“之后第 n 个交易节的开始时刻”。建议改成和分钟线同样的含义：**建立观察期的那根 K 线算第 1 根，到期时刻是第 n 根日线的收盘**。

示意：

```python
# DAILY：按交易日分组，取每个交易日最后一个交易节的结束时刻
day_close: dict[date, int] = {}
for session in ordered:
    day_close[session.trading_day] = max(day_close.get(session.trading_day, 0), session.end_nano)
closes = sorted(day_close.values())
# start_nano 是建立观察期的那根日线的收盘时刻；它本身是第 1 根
later = [close for close in closes if close > start_nano]
remaining = bars - 1
if remaining == 0:
    return start_nano
if remaining <= len(later):
    return later[remaining - 1]
return start_nano + bars * DAY_NANO   # 日历不够时沿用原来的回退
```

在每天一个交易节的合成日历上，配合第一部分的判断，开仓时点与现在相同。但 `StrategySetup.expires_at_nano` 的数值会变（从“第 n 天的开盘”变成“第 n−1 天之后的收盘”），**凡是把日线状态序列化后比对哈希的冻结预期都会变**。请先清点这些预期，由计划作者决定按新版本重新冻结。

### 验收

1. `probes/test_entry_confirmation_reachability_probe.py` 由 5 失败 4 通过变为 9 通过。
2. `python scripts/test_offline.py -q tests/test_v2_forward_paper.py` 回到 68 通过（`b653f7e^` 的水平）。
3. v2 M1–M4 的通过数不减少。因日线到期时刻变化而改变的预期逐条列出，不批量刷新。
4. 第二轮的重复 K 线探针和 `tests/test_v2_m3b_duplicate_bar.py` 的 96 项仍然通过：同一根 K 线重复送达不推进计数，这一点不能丢。
5. 新增永久回归：七类策略、分钟和日线两种模式、延迟取 0、1 纳秒、1 秒、59 秒，日历取“每天一节”和“夜盘加日盘三节”。断言每类策略在应当确认的那根 K 线上达到要求的确认数。
6. K03 的失败清单没有新增。

### 注意

- 这是对 `b653f7e` 的修正，按规则应回到原开发者，并由独立审核者复审。审核范围请包含前瞻测试。
- 改动会改变 `registry.py`、`state.py` 所在策略族的 `code_sha256`，需要新的合同锁。

## K02 震荡状态的跨周期结论可被准入接受（P0）

**问题**：见 REVIEW 的 X02。

**涉及文件**：`src/ak_trading/v2/probability/regimes.py:420–432`，`tests/test_v2_m3a_regimes.py`，M3B 的共用夹具。

**建议的语义**（方案甲，推荐）：让状态识别对非趋势状态也给出有内容的跨周期结论，并且启用合同里已有但从未产生的 `CONFLICT`。

| 当前周期的状态 | 日线背景 | 跨周期结论 |
| --- | --- | --- |
| 上升或下降趋势 | 没有发布 | `UNKNOWN`（不变） |
| 上升或下降趋势 | 方向相同 | `ALIGNED`（不变） |
| 上升或下降趋势 | 方向相反 | `COUNTERTREND`（不变） |
| 上升或下降趋势 | 已发布但没有明确方向 | `UNKNOWN`（不变） |
| 有序震荡、压缩、宽幅震荡 | 没有发布 | `UNKNOWN` |
| 有序震荡、压缩、宽幅震荡 | 已发布且没有明确方向 | `ALIGNED`：日线不成趋势，与区间判断一致 |
| 有序震荡、压缩、宽幅震荡 | 已发布且方向明确 | `CONFLICT`：日线在走趋势，区间策略不应开仓 |
| 未知 | 任意 | `UNKNOWN` |

`CONFLICT` 已经被两道闸门拒绝（`registry.py:804`、`gates.py:80`），不需要改闸门。这样做也符合 `docs/devplan-v2/04-M3B-策略库.md` 第 165–166 行“趋势中逆势回归、网格、马丁默认禁”的表述。

实现时要区分“背景没有发布”和“背景已发布但没有明确方向”。`_background_trend` 对两者都返回 `None`，所以判断要直接用 `background is None`。

**方案乙**：不改状态识别，在 `admission_decision` 里对震荡三族放行 `UNKNOWN`。改动更小，但等于取消了这三类策略的跨周期检查，不推荐。

**需要计划作者一并处理**：

- `tests/test_v2_m3a_regimes.py:394` 断言“有序震荡 + 中性背景”的结果是 `UNKNOWN`。它固定的正是现在的行为，方案甲下要改成 `ALIGNED`。
- BREAKOUT 允许“压缩”状态。按上表，压缩状态下日线方向明确时是 `CONFLICT`，突破会被拦下；日线没有明确方向时才放行。这是否符合突破策略的本意，请计划作者确认。
- 跨周期结论进入 `RegimeAssessment` 的标识哈希（`regimes.py:455–458`），已有的冻结预期会变。

**验收**：

1. `probes/test_range_family_cross_timeframe_probe.py`：三个开仓用例通过；四个状态用例按上表改写预期后通过（中性背景 `ALIGNED`，明确方向 `CONFLICT`，无背景 `UNKNOWN`）。
2. 新增自然链测试：状态由 `assess_regime` 产生，三类震荡策略在“有序震荡 + 已发布的中性背景”下能走到 `OPEN`，在“日线方向明确”和“无背景”下得到对应的拒绝原因。
3. M3B 的共用夹具 `regime()`（`tests/test_v2_m3b_matrix.py:130`）不再默认 `ALIGNED`，调用方显式传入。
4. K03 的失败清单没有新增。

## K03 全量回归基线与失败清单对照（P0）

**问题**：见 REVIEW 的 X20、Y03。

**做法**：

1. 新增 `scripts/compare_test_baselines.py`。输入两份离线入口的输出日志，解析其中每个域的 `OFFLINE-DOMAIN-STATS {...}` 行（已经包含 `failed_nodeids`、`error_nodeids`、`collection_errors`），输出新增的失败、新增的错误、新增的收集失败和消失的失败。有任何新增时退出码为 1。
2. 在本机固定环境下对当前 HEAD 跑一次全量，把三类编号存成 `tests/baselines/full-suite-windows.json`，连同命令、Python 版本、提交号。
3. 之后每个产品改动：合入前在候选上跑一次全量，与基线比较；新增必须为零。修掉的失败随同一个提交从基线里删除。
4. 基线里已有的失败不要求立刻修，但清单只减不增。

**验收**：脚本自身的测试覆盖“新增失败、新增错误、新增收集失败、减少、完全相同”五种情况；对 `b653f7e^` 和 `b653f7e` 的两份日志运行，能报出 48 项新增。

**注意**：全量里有少数与时间或机器负载有关的用例（分诊清单里标了 1 项时间敏感、1 项 p99）。连续两次结果不同的用例单独列为“不稳定”，不计入新增，但要有名单。

## K04 研究运行前的可达性预检（P0）

**问题**：见 OPTIMIZATION 的 O2。

**做法**：新增只读脚本 `scripts/preflight_v2_research.py --input-bundle <包> --config <配置> --output <目录>`，不拟合、不注册实验、不触碰留出集，输出 `preflight.json` 和一页 `preflight.md`。

| 检查 | 算法 | 输出 |
| --- | --- | --- |
| 资金 | 对每个“合约 × 策略族 × 频率”，用 `strategies.state.round_loss_cny`、`peak_margin_cny` 和 `policy.calculate_stress_loss`，取测试窗口内的参考价和 ATR 的中位数，反推开 1 手（马丁为 1+2+4 手）的最低权益 | 最低权益、哪一条上限起约束、配置里的资金是否足够 |
| 证据样本上限 | 对每个“品种 × 频率 × 期限”，校准窗口内的决策点数减去标签未成熟的尾部，再对配置里的合约求和 | 上限值，对照 200（马丁 500）。这是不分状态、不分概率区间的上限，实际只会更少 |
| 有效交易日 | 校准窗口内的交易日数 | 对照 30（马丁 60） |
| 收益回合上限 | 训练窗口的交易日数除以最长持有期，再乘合约数 | 对照 200 |
| 日历与数据 | 交易节各证据状态的数量；K 线各质量状态、各时间依据的数量；可用时刻减收盘时刻的最小值、中位数、最大值 | `synthetic=false` 时是否会在 `experiments.py:1497` 退出；有多少 K 线会被 `visible_bars` 拒绝 |
| 确认可达 | 在输入包自己的日历上，对每个策略族取若干建立时刻，比较 `setup_expiry_nano` 与第 n 次观察的时刻 | 每个策略族是否凑得齐确认 |

汇总一个字段 `structurally_empty`：没有任何“策略 × 合约”单元格可能成交时为真。

**验收**：在现有合成夹具上运行得到“可达”；在一个刻意构造的小资金、短窗口配置上得到 `structurally_empty=true` 并列出原因；脚本对输入包和配置零写入。

**用法约定**：预检结果写进每份研究报告的第一节。`structurally_empty` 为真时不启动正式运行，先把预检结果报给你。

## K05 真实形状的测试夹具与自然链冒烟测试（P1）

1. 在 `tests/` 下新增共用夹具模块，提供：带夜盘和三个日盘交易节的日历；可用时刻晚于收盘的 K 线（延迟可参数化）；由 `assess_regime` 生成的状态。
2. M3B 里每个“某类策略能开仓”的用例，增加在这组夹具上的版本。
3. 新增 `tests/test_v2_natural_chain_smoke.py`：小规模合成数据，走真实的 `build_strategy_context → evaluate → evaluate_gate → choose_proposal`，断言趋势、突破、回踩、回归、网格、马丁各自至少出现一次 `OPEN`，并断言等待策略从不开仓。放进 CI。

这三条的目的是把“零件都通过、合起来走不通”这一类问题挡在 CI 里。

## K06 策略库的三处小问题（P2）

| 位置 | 建议 |
| --- | --- |
| `strategies/pullback.py:82` | 去掉重复的 `ma20 is None`。`ma60` 是否仍应列为 PULLBACK 的必需因子，由计划作者确认：代码不读它，但它在 `required_factor_keys` 里，缺失时会被 `gates.py:67–70` 拒绝 |
| `strategies/registry.py:1131–1134` | `make_hold_proposal` 里止损价和止盈价的传参位置与 `make_exit_proposal` 相反。改正会改变 HOLD 提议的标识，建议记录在案，随下一次策略版本号变更一起改，不单独动 |
| `strategies/registry.py:86` | `allow_countertrend` 没有被读取。要么在 `admission_decision` 里按它拒绝 `COUNTERTREND`（现在是 M4 的 `gates.py:80` 在拦），要么在参数说明里写明它由 M4 执行 |

## K10 日历索引（分钟线 P0）

**问题**：见 REVIEW 的 X10。

**涉及文件**：`src/ak_trading/v2/clock.py`；调用点 `decision/pipeline.py:390`、`476`，`strategies/registry.py:428–458`，`decision/replay.py:377`，`decision/payoff_paths.py:435`，`validation/forward_paper.py:1601`。

**做法**：

1. 在 `clock.py` 里加一个内部索引类，按“交易节元组的身份 + 品种”缓存，做法与 H4 在 `data.py:557–631` 的可见性索引相同：缓存项持有元组的引用，取用时做身份比较；有容量上限和清空入口。
2. 索引的内容：
   - 整个元组只做一次类型检查和重叠检查（现在的 `_check_sessions` 对所有品种检查重叠，这个行为要保留）。
   - 本品种的交易节按开始时刻排序。
   - 分钟格的开始、结束两个有序数组。交易节不是整分钟时，只在分钟类查询时报 `session_not_minute_aligned`，与现在一致。
   - 交易日列表，以及每个交易日的最早开始和最晚结束。
3. 用二分重写 `horizon_window`、`holding_deadline`、`can_open_before_close`。对外签名、返回值、报错类型和报错文字都不变。原实现改名保留，只供等价测试调用。
4. `pipeline.py:390` 每次生成新元组会让缓存失效。改为按“输入包的交易节元组身份 + 品种”取同一个元组对象。
5. `pipeline.py:476` 的“取截止时刻之前倒数第 n 个分钟格”、`registry.py:438–458` 的分钟观察期到期、`replay.py:377` 等三处的“下一个分钟格”，都改成对索引的查询。

`probes/clock_horizon_bench.py` 里的 `CalendarIndex` 是一个可运行的参考：它对 `horizon_window` 的两类期限在 5 种日历长度、各 600 组输入上与产品实现逐项相同。它没有覆盖 `holding_deadline` 和重叠、非整分钟这些异常日历，这些要在正式实现里补上。

**验收**：

1. 等价测试：随机日历（含午休、夜盘、缺失交易日、多品种、重叠、非整分钟、空日历）乘随机决策时刻（含每个交易节的开始、结束及其前后 1 纳秒），新旧实现的返回值和异常逐项相同。
2. v2 M1–M4 结果不变。这一项不改变任何数值，所有冻结预期应当原样通过。
3. 基准：`horizon_window` 和 `can_open_before_close` 的单次耗时不再随日历长度增长，在 250 个交易日的日历上低于 10 微秒。
4. K03 的失败清单没有新增。

## K11 决策上下文的共享与缓存（P1）

**问题**：见 REVIEW 的 X11。

**涉及文件**：`src/ak_trading/v2/decision/pipeline.py`。

**做法**：不改 `build_strategy_context` 的签名（它在合同锁的公共签名里），在模块内部加有界缓存：

| 缓存内容 | 键 | 说明 |
| --- | --- | --- |
| 合约对应的品种、交易节元组 | 输入包身份、合约 | 整个回放期间不变 |
| 可见 K 线、交易规则、当前周期的因子快照 | 输入包身份、合约、决策时刻、频率 | 七个策略共用 |
| 分钟模式下的日线背景（因子、状态、预测） | 输入包身份、合约、来源日线的标识、模型身份 | 一个交易日内只算一次 |
| 日线模式下的背景 | 上一项的键再加置信政策的标识 | 普通策略和马丁用的政策不同 |
| 截止时刻 | 合约、决策时刻、持仓政策 | 依赖 K10 |

缓存只放与策略状态无关的部分。`state`、持仓、资格这些仍然每次计算。提供清空入口，并且和 K14 的因子缓存一起由一个函数清空。

**验收**：

1. 同一段回放，关闭缓存和开启缓存各跑一次，`StrategyContext` 序列化后逐字节相同，回放产物的清单哈希相同。
2. 冷缓存和热缓存各跑一次，结果相同（第三轮提到的 T7 CPU 审核抓到过冷热不一致的缺陷，这里要有同样的检查）。
3. v2 M1–M4 结果不变；K03 没有新增。
4. 每个决策事件的平均耗时前后对比入库。

## K12 短日历多尺度 profile（P1）

在 K10、K11 合入之后，用 5、20、60 个交易日三种长度的合成数据各做一次 cProfile 和 tracemalloc，每次几分钟。交付一页表格：三种长度下累计耗时前 20 的函数、每个函数随长度的增长倍数、峰值内存。增长倍数接近长度比的平方的函数就是下一批要处理的。

H8 当前那个 568 天的作业不必等它跑完。它的脚本可以直接用来跑这三个短的。

## K13 模型拟合向量化（P1，需要 J4）

**涉及文件**：`src/ak_trading/v2/probability/models.py` 的 `_softmax_gd`（366 行）、`_build_tree` 和 `_sse`（525–577 行）、`_fit_gbm`（589 行）、`_fit_linear_quantile`（718 行）。

**先定标准再动手**。两种可选的等价标准：

- 逐字节相同：numpy 的求和顺序和纯 Python 不同，做不到。
- 固定精度相同：模型参数和预测概率在发布前统一舍入到固定小数位（比如 12 位），纯 Python 实现保留为参考，两者在舍入后比对。舍入规则一旦加入，模型的哈希会整体变化一次。

建议第二种，由计划作者写成变更申请。在标准确定之前，可以先做不改变数值的优化：`_sse` 里每次分裂重新求和的部分改成前缀和，仍是纯 Python、结果逐位相同。

## K14 两个遗留项（P2）

1. `v2/data.py:503–510`：`visible_bars_recent` 从最新一根往回扫。在索引里预存“截止时刻的后缀最小值”数组（它单调不减），对决策时刻做二分，就能直接跳到第一根可能可见的位置。等价测试沿用 H4 的那一套。
2. `v2/factors.py`：新增公开的 `clear_feature_caches()`，清空第 107、135、154、177、287、289、668、675、714、1168 行的全部缓存；在 CI 里加一个冷热缓存逐字节比对的测试。

## K20 研究所需的小文件入库（P1）

1. 新建被跟踪的 `research/` 目录：`research/locks/`（合同锁及其对应的审核报告）、`research/configs/`（研究配置）、`research/reports/<运行名>/`（`manifest.json`、`metrics.json`、`readiness.json`、`report.md`、预检结果）。
2. 研究配置里的 `contract_lock_path` 改指 `research/locks/` 下的文件。
3. 输入数据包不入库。报告的 manifest 里已有输入包的哈希；另加一份 `research/inputs/README.md` 记录每个输入包的哈希、生成命令和存放位置。
4. 本机工作区里的候选分支每天推到远端。

第 1、4 条在仓库转私有（J1）之后做。`v2/artifacts.py:168` 目前禁止验收清单引用 `data/research_runs/` 下的附件，新目录不受这条限制，但请计划作者确认没有别处写死了路径。

## K21 数据管线与因子库目录（P1）

`python docs/repo-audit-20261110-claude/probes/missing_modules.py .` 列出了全部缺口：`ak_trading.datapipeline` 下缺 `contracts`、`policy`、`ports`、`storage`、`models`、`migrate`、`readback`、`validation`、`gaps`、`legacy`，且没有 `__init__.py`。

二选一，由你决定：

- **还在用**：把这些模块从本机的 `docs/devplan-datapipeline-20260923-r1/DP0x/r1/src/` 移进 `src/ak_trading/datapipeline/` 并跟踪；去掉测试夹具里重建包路径的做法（`tests/datapipeline/fixtures/DP08/bootstrap.py`），离线入口里为它做的按文件隔离也可以随之简化。
- **不再用**：把仓库里的 7 个模块、`tests/datapipeline/`、相关脚本移到 `archive/` 并从测试路径里去掉。

因子库同理：`config/factor_library.json` 登记的目录文件不在仓库里，补上或者让 `tests/test_factor_library.py` 在文件缺失时跳过并说明原因。

## K22 CI 第二层与可选依赖（P1）

1. 14 个因缺少可选依赖而收集失败的测试文件，在文件顶部改用 `pytest.importorskip`（sqlalchemy、tqsdk、httpx、fastmcp），让缺依赖变成有说明的跳过。文件名单见 REVIEW 附录第 4 条命令的输出。
2. `tests/test_infiny_native_pairing.py` 在非 Windows 上收集时抛 `RuntimeError`，改为模块级的平台跳过。
3. `.github/workflows/` 加第二个作业：安装 `.[test,postgres]` 及 K23 补齐的可选依赖，运行 `tests/test_v2_m5*.py`、`tests/test_v2_forward*.py`、K05 的冒烟测试。依赖本机工件的用例用统一的标记跳过。
4. 第一个作业不变，仍然只装 pytest，保证核心库在没有可选依赖时可用。

## K23 依赖声明、Python 版本、`.gitattributes`（P2，需要 J8）

| 项目 | 建议 |
| --- | --- |
| `pyproject.toml` | `requires-python = ">=3.13"`。3.11 解析不了源码，3.12 建不出合同锁 |
| 可选依赖 | 增加 `mcp = ["fastmcp>=4.0"]`；`test` 里补上测试实际导入的 httpx、psutil；`requirements.txt` 只留一行指向 `pip install -e ".[...]"`，不再单独列包 |
| `.gitattributes` | 只写一行 `* -text`。它让 Git 在任何机器上都不做换行转换，仓库里的字节和工作区的字节始终相同。这正是逐文件哈希所需要的，而且不需要对现有文件做任何规范化 |

不建议用 `* text=auto` 或 `eol=lf`：它们会在检出时改写工作区字节，142 个 CRLF 文件的哈希会全部变化。

## K24 入口文档与规则文件的失效指针（P2）

| 文件 | 建议 |
| --- | --- |
| `docs/DEVELOPMENT_PLAN.md` | 改成一行，指向 `docs/strategy-first-blueprint-20261015-r1/CURRENT.md` |
| `AGENTS.md` §6 | 把合同锁路径和文档版本改成现行值，或者删掉这一节并指向状态页。属于规则文件，需要你同意 |
| `AGENTS.md` §7、§8 引用的 10 个缺失文件 | 这些事故记录在本机。规则要求新工单携带它们，所以要么入库，要么把要点直接写进规则正文 |
| `PROJECT-STATUS.md` 等 8 份历史文档 | 移到 `docs/archive/`，README 里的链接同步修改。不删除 |

## K30 仓库可见性、令牌、公网地址（P0，只能由你执行）

1. GitHub 仓库设置里改为 Private。
2. 重新生成 `AK_MCP_TOKEN`，写入本机 `.env`，重启行情服务的计划任务。旧令牌作废。
3. 让 agent 把 `docs/mcp-agent-guide.md` 第 4 行的地址换成占位符（比如 `http://<你的入口>:<端口>/mcp`）。Git 历史里仍有旧地址，所以第 2 步不能省。
4. 入口最好改成 SSH 隧道或 VPN，不再把明文 HTTP 端口直接暴露在公网上。防火墙规则由你操作。

## K31 行情服务与执行服务的默认端口分开（P1）

建议改行情服务的默认值，因为执行服务的 17779 写在客户端脚本、安装器及其校验里，改动面更大。

| 文件 | 改动 |
| --- | --- |
| `scripts/run_market_mcp.py:7`、`25`、`66` | 默认端口改为 17780 |
| `.env.example` | `AK_MCP_PORT=17780` |
| `scripts/install_market_mcp_task.ps1:3` | 默认参数同步 |
| `docs/mcp-server.md`、`docs/mcp-agent-guide.md` | 文字同步 |

你本机 `.env` 里如果显式写了 `AK_MCP_PORT=17779`，现有部署不受影响。另外在两个服务启动时各加一次“端口已被占用则退出并说明”的检查。

## K32 客户端策略的合约改为部署时必填（P1）

`integrations/infiny/AIBridge.py:178` 和 `AKExecutionBridge.py:102` 的默认合约改为空字符串。策略启动时合约为空或已过最后交易日就拒绝运行并在界面上提示。安装脚本（`scripts/install_infiny_observer.py`、`install_infiny_execution.py`）要求显式传入合约。`AGENTS.md` §5 要求 `AKObserverBridge.py` 与 `AIBridge.py` 字节一致，改动后按原流程备份并重新部署，客户端界面由你操作。

## K33 影子运行接线（P1，排在报告之后）

`v2/validation/shadow.py:932` 补上 `exit_registry_sha256` 和 `exit_binding` 两个关键字参数；`v2/validation/experiments.py:609` 的 `_reg` 改为真实的退出规则注册表。沿用 r3-001 实施计划第 2 节的 B0–B8。在接线完成之前，建议在 `run_shadow` 和 `run_shadow_replay_experiment` 的函数入口直接抛 `StageIncompleteError`，让从代码导入调用的路径和命令行得到同样的拒绝。

## K34 不再把 `TypeError` 记成证据无效（P2）

`decision/replay.py:306` 和 `validation/shadow.py:933` 捕获 `(ValueError, TypeError)`。`verify_qualification_evidence` 对坏证据抛的是 `ArtifactsError`（`ValueError` 的子类），对传错类型抛 `TypeError`。建议只捕获 `ValueError`，让 `TypeError` 直接暴露。r3-001 实施计划第 2 节写了“不机械删除全部 TypeError 捕获”，所以这一条需要计划作者逐处裁定；H3 已经为 replay 的调用点加了签名契约测试，可以作为先例推广到 shadow。
