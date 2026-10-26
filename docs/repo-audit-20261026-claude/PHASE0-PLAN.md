**PHASE0-PLAN.md — r3-001 第二轮审计增补，2026-10-26**

本增补基于 main `891e4de`，补充既有 r3-001，不替换蓝图或重开 T7 计划。授权范围为 A2 重复 bar 修复、A3 两处文档同步、A4 方案与修复顺序裁定，以及 B1 并行分诊。Astra/max 负责计划与意见处置；专用 ZCode 会话实施；另一独立 ZCode 会话进行成果硬审；未另行指定的辅助任务使用 OpenCode 默认配置。独立计划意见由本作者处置，不设计划 PASS 门或重复求 PASS 的循环。

依据为本轮外部 `REVIEW.md`、`BLUEPRINT.md`、原样探针，以及 r3-001 和 `docs/devplan-v2/04-M3B-策略库.md` §6、§7、§10。外部材料不是有效计划或产品接受。Root 已实际复现探针：**raw 1，9 selected，3 passed、6 failed**；派工沿用其原始绝对路径附件。本作者只作静态核对，未重跑该探针。图索引为 `2026-10-24T00:18:52Z`；相关源码均为 `metadata_changed`，本判定采用当前源码直接读取，不宣称图覆盖完整。

**1. 外部建议处置**

| 建议 | 作者决定 | 本批落点及理由 |
| --- | --- | --- |
| A2/N1：区间三族去重、共享确认函数、七族回归、修正 MARTINGALE 断言 | **ADOPTED** | 已有真实反例，违反原 M3B §10.11。共享计算只统一确认计数，保留各族结构与门控语义。 |
| 修复后删除外部九项探针 | **NOT_ADOPTED** | 原样保留，独立验证必须得到 9 passed；永久回归另增。 |
| A3/N5：入口文档一致性 | **PARTIALLY_ADOPTED** | 只修改 README 的测试状态与 r3 实施计划六处旧 Sol 角色文字。CLAUDE.md 冲突列出待用户决定，不改规则。 |
| A4：完整 shadow 接线或提前拒绝 | **PARTIALLY_ADOPTED** | 选择 **CLI 提前明确拒绝 `shadow-replay`** 的有限保护方案；本轮交付方案及排程，不授权完整 shadow 实施，不修改旧 T8 验收。 |
| B1/N2：分诊现有失败 | **ADOPTED** | 直接并行分析现有原始结果；区分测试节点、收集文件和共同根因。 |
| 用 `xfail(strict=True)` 获得绿色基线；立即改测试根或重写启动器 | **NOT_ADOPTED（本批）** | 不掩盖失败，不扩大已接受 C05 两文件修复范围。具体缺陷再交对应 ZCode owner。 |
| N4/B4、B6、N8 及新增平台、依赖、架构工作 | **NOT_ADOPTED（本批）** | 保留原支撑修复或提案归属，不成为 A2 前置，不迁移哈希口径或依赖版本。 |
| A1/P0-1、A5/F1–F6、B3 分支保护、B4 规范化、B6 Python 支持范围 | **保留用户决定** | 不因外部建议自动实施；外部日期、并行上限和“全绿”目标也不成为新增验收门槛。 |

**2. A2：精确归属与实现**

Root 从 `891e4de` 建立 `C:/AKWorktrees/aktr-round2-a2-20261026`，绑定一个专用 ZCode owner、实际 SID、原 cwd 和独立固定 store；本计划不虚构会话身份。该候选只有此 owner 写产品文件。

产品写集限以下 **10 文件**：

| 文件 | 允许改动 |
| --- | --- |
| `src/ak_trading/v2/strategies/registry.py` | 新增内部确认计数 helper；不改策略注册身份规则、绑定算法或公共 schema。 |
| `src/ak_trading/v2/strategies/reversion.py` | `_observe` 使用 helper，补必要导入。 |
| `src/ak_trading/v2/strategies/grid.py` | 同上；不改 ADD、层数或预算。 |
| `src/ak_trading/v2/strategies/martingale.py` | 同上；本次明确授予超出旧 T7-A lease 的该文件窄写权。 |
| `src/ak_trading/v2/strategies/trend.py` | 将已接受的去重计数等价委托给 helper。 |
| `src/ak_trading/v2/strategies/pullback.py` | 同上。 |
| `src/ak_trading/v2/strategies/breakout.py` | 仅委托计数计算，保留真实 `created` 分支及原结构逻辑。 |
| `tests/test_v2_m3b_martingale.py` | 加强原重复 bar 用例；保留原节点名和有效断言。 |
| `tests/test_v2_m3b_duplicate_bar.py`，新建 | 七族永久参数化行为回归。创建前确认目标不存在。 |
| `src/ak_trading/v2/artifacts.py` | **仅向 `DOWNSTREAM_TEST_FILES` 追加上述新测试路径**；不删旧成员，不改生成器、校验器、版本或锁。 |

最后一项是本次必需测试登记的精确范围，由 Root 在派工中明确记录；不产生其他 M1 公共文件写权。`wait.py`、`state.py`、既有矩阵与方向族测试作为只读输入。

实现按以下规则完成：

1. 当前 `range_entry_setup` 在有效同 bar 时返回旧 setup，但三调用者仍执行 `state.confirmation_count + 1`。修复真正决定 OPEN 的 `confirmation_count`，不能只改 `setup.consecutive_confirmations`。
2. helper 使用**调用前的 `state.setup`**、当前最后一根可见且完成的同频率 bar，以及 `context.now_nano`。不得拿刚返回的新 setup 的 `last_bar_id` 判断重复。
3. 计数优先级固定为：门控失败 → `0`；BREAKOUT 真实 `created=True` → `1`；旧 setup 未过期且 bar ID 相同 → 保持旧计数；其余合格新观察 → 原计数加 `1`。同 bar 判断沿用 `now_nano <= expires_at_nano`。无可用 bar、结构失效、过期取消与重建仍走各族原路径，不延长 expiry，不把所有族统一成 BREAKOUT 的创建规则。
4. 保留三方向族结构确认、区间先触后穿、冻结 range/MA/ATR、门控失败清零、下一合法窗口执行、成本及风险限制。helper 不决定 OPEN，不改预算、ADD、退出或提交状态机制。
5. WAIT 不调用入场 helper，其原行为保持 `action=WAIT`、`target_lots=0`、`confirmation_count=0`；不得据 WAIT 推断已有账户仓位已平。

**3. A2：永久回归与验收**

新文件从真实注册表覆盖 TREND、BREAKOUT、PULLBACK、REVERSION、GRID、MARTINGALE、WAIT；MINUTE/DAILY 使用各自已注册规格和合法同频率 SYNTHETIC context。调用真实 `evaluate` 与 `commit_observation`，逐步检查实际计数和动作，不 mock 计数或强造 OPEN。

每个入场族至少有下列轨迹；清洁序列须满足该族自身结构与全部准入条件：

| 轨迹 | 必须证明 |
| --- | --- |
| `0,1,2` | 计数 `1,2,3`，在第三根不同的合格 bar 才产生 OPEN。 |
| `0,1,1,2` | 回穿/确认 bar 重送保持 `1,2,2,3`，重复步骤不提前 OPEN。 |
| `0,0,0,1,2` | arming/首次观察重送保持 `1,1,1,2,3`。 |
| 保存观察态、重建策略对象后重送 | 与连续执行的计数和 OPEN 时点一致。 |

WAIT 对相同送达轨迹均验证等待、无入场和计数为零，不编造 arming/recross。另保留门控失败优先清零、同 bar 恢复合格不额外加一、expiry 边界、BREAKOUT 新 setup 从 1 开始的针对性控制；原多空、时序、风险及恢复人口完整执行。

MARTINGALE 原用例须在第三根确认前检查 arming 与 recross 重送，保留 setup 断言，并新增 `state.confirmation_count`、无提前 OPEN、第三根清洁确认可 OPEN 的断言。不能继续只在完整序列结束后检查结构计数。

必需验证顺序：

1. **基线**：等待 Root 正在 `891e4de` 执行的完整 M1–M4 命令自然结束，读取真实结果后比较；不把“预计 14 个 payoff 失败”预写为实际结果。复用其已封存结果；M3B 前基线若无独立完整记录，在未修改源码的 A2 候选执行一次。
2. **聚焦修复**：新回归文件、完整 MARTINGALE 文件和原样外部探针。
3. **完整 M3B**：原有八文件全部保留：`causality`、`directional`、`martingale`、`matrix`、`registry`、`reversion_grid`、`state` 对应文件，以及实际基线枚举中其他 M3B 文件；加新回归。以实际枚举清单为准，不用数字裁剪人口。
4. **完整 v2 M1–M4**：执行与 Root 基线相同的全部 `tests/test_v2_m[1-4]*.py`，加新匹配文件；记录完整节点及失败差异。
5. 冻结后由独立 ZCode 重跑上述必需人口，并增加其自己的反例。

可使用 Git Bash 的真实文件展开：

```bash
python scripts/test_offline.py -q tests/test_v2_m3b_duplicate_bar.py tests/test_v2_m3b_martingale.py
python scripts/test_offline.py -q docs/repo-audit-20261026-claude/probes/test_range_duplicate_bar_probe.py
python scripts/test_offline.py -q tests/test_v2_m3b_*.py
python scripts/test_offline.py -q tests/test_v2_m[1-4]*.py
```

调用前保存实际展开的 argv；PowerShell 使用显式文件数组，不能传未展开通配符。每个进程设置临时 `PYTHONPATH=<实际候选>/src`，在同一离线受守卫环境确认实际加载的策略和 artifacts 模块绝对路径属于该候选，防止借用 MAIN 的 editable install；不改持久化环境。

A2 必须满足：**外部探针 9 passed；永久回归通过；完整 M3B 不新增失败/错误/跳过；完整 M1–M4 无新增非 A2 失败，全部 A2 缺陷已闭合。** 原 14 项 payoff 历史失败若仍存在，逐节点保留及解释，不刷新其哈希，不计作全 M4 通过。其他真实新失败按归属处理，不能笼统称为“哈希变化”。

每次必需调用预算 **600 秒**，进程树内存 **2 GiB**，本批临时输出 **1 GiB**；不启动额外重训练或 T7 冷链。如果 Root 同命令基线实测证明 600 秒不足，按真实耗时记录合理后继预算，并用于可比复验，不缩人口、不降门槛、不另设用户审批。普通离线回归可按修复需要重复；历史已消耗的特殊一次性动作仍保留原限制。

**4. A2 接受与 T7 在途边界**

A2 新建独立冻结与独立成果接受，保留 `891e4de` 前态、逐文件差异、实际测试结果和失败集合。独立接受后，Root 在主树基线运行结束及写入边界接收上述实际改变的精确字节；接受文档使用既有交付流程，不能由开发者自签独立 PASS。

`code_sha256_for` 包含共享 `registry.py`，因此本次会影响全部七族、两个频率的代码身份，即使 WAIT 源文件未改。A2 接受不能自动授予旧 binding、qualification、合同锁或消费者清单对新身份的信用。

T7-B011 在 `C:/AKWorktrees/aktr-r3-t7-fixtures-20261025` 的冷链继续使用其原冻结字节。A2 不写入该树，不改历史 T7-A/B 接受副本、M5 冻结候选、在途消费者 binding 或任何旧 manifest，也不因本修复取消既有运行。

后续消费者确需采用 A2 时，由原 owner 在调用结束后交接接受字节，生成真实新身份及相应清单、重新冻结并履行实际受影响的接受要求。涉及 T7 合同锁时继续执行 **F → R0 → L0 → R1 → L1 → R2**；不借旧锁、不旁路校验、不把旧接受改署为新字节通过。未消费新字节的原接受范围保留。

**5. A3：Root 可执行的两文件文档替换**

只允许 Root 修改 `README.md` 与 r3-001 `IMPLEMENTATION-PLAN.md`，独立于 A2 候选，不改规则、历史意见或其他蓝图文件。

README 当前“最新核验”段替换为：

> **测试状态更新（2026-10-26，引用已完成的 C05 独立审核）**：C05 两文件导入隔离修复已接受，范围限离线启动器及其测试，不代表全套测试通过。完整入口原生退出码为 **2**：6,015 项进入执行，5,754 passed、171 failed、60 errors、30 skipped，另有 3 个文件收集失败。详见 [C05 独立审核](docs/development-20261025-zcode-r3/C05-REVIEW.md)与[接受记录](docs/development-20261025-zcode-r3/C05-ACCEPTANCE.json)。这些是该次冻结候选的历史实测，不冒称当前 HEAD 已全量复验。
>
> **修复前核验历史（2026-10-25）**：C05 曾为 raw 2、174 个收集错误、0 个测试执行；C07 曾为 raw 1、11 条 T7 code_drift，见[原逐项核实](docs/repo-audit-20261025-claude/VERIFICATION.md)。这些记录保留溯源，已不代表 C05 隔离修复后的执行状态。

旧 `1,589 passed` 段继续标为历史，不改其他 README 命令或产品说明。

实施计划仅同步以下六处角色文字：

| 位置 | 替换内容 |
| --- | --- |
| §1 职责段 | Astra/max 负责计划；实际开发及修复由专用 ZCode CLI 会话使用已有配置；另一独立 ZCode GLM-5.3/max 提供计划意见与成果硬审；其他未指定辅助任务使用 OpenCode 默认配置。修复续接实际 ZCode owner，保持精确 SID/cwd/store；必要替换明确交接。删除旧 Sol 子代理模型参数要求。 |
| §1 P2-7/支撑项 owner 单元格 | “Root 显式绑定独立 Sol”改为“Root 显式绑定专用 ZCode 开发会话”。 |
| 里程碑 1 | “原测试引导/基线 Sol owners”改为“原测试引导/基线职责对应的 ZCode owners”。 |
| 里程碑 2 | “M4 Sol”改为“M4 专用 ZCode owner”。 |
| 里程碑 4 | “当前 M5 Sol owner”改为“现行明确交接绑定的 M5 ZCode owner”。 |
| §4 末段 | “FAIL 回同一 Sol owner”改为“FAIL 回同一实际 ZCode owner”。 |

历史 owner 路径字符串保留溯源，不批量改名或伪造 SID；新调度依据现行明确交接。Root 检查两文件差异及相对链接，不运行产品测试。

CLAUDE.md 的冲突清单仅供用户决定是否同步：

- 第 6–15 行仍把 10-18 Sol 开发指令称为最新，并要求移交 Sol、覆盖 ZCode；与当前 AGENTS §1、模型职责规则的 ZCode 开发路由冲突。
- 第 20–21 行“不得新增计划、完成本批即停止”不能阻断用户此次明确授权的 phase 0；该覆盖只限本次范围。
- Astra 计划、独立 ZCode 审核、OpenCode 默认辅助和配置保护部分不构成此次冲突。

本批不编辑 CLAUDE.md、AGENTS.md 或 PROJECT 规则，也不据此实施 F1–F6。

**6. A4：选择 CLI 提前拒绝，完整 shadow 仍未闭合**

静态核对确认三个缺口仍在：

| 调用点 | 未满足的契约 |
| --- | --- |
| `validation/shadow.py:932` | 真实 qualification verifier 必需的 `exit_registry_sha256`、`exit_binding` 未传入。 |
| shadow ledger restore/checkpoint 及 experiments genesis checkpoint | 缺必需 `exit_registry_sha256`；完整 bindings/states/links 闭包也不能靠填空值完成。 |
| `validation/experiments.py:603` | 使用未在本函数建立的 `_reg`，且位于模型训练之后。 |

**裁定选择有限入口保护。本轮 A4 完成物是该方案和修复顺序，产品状态仍为 NOT_IMPLEMENTED；不授予完整接线或修改 T8 的权限。**

以后实施该有限保护时，精确写集为：

- `scripts/research_probability_v2.py`：保留模式名称与原参数语法检查；在 feed/reference 参数关系检查之后、`output = _path(...)` 和任何 config/bundle/feed/lock 读取之前，抛出 `StageIncompleteError("ORDINARY_SHADOW_REPLAY_NOT_AVAILABLE")`。复用现有错误处理返回原生 **3**，明确工程不可用，不创建输出目录，不触发训练、注册或 holdout。
- 新 `tests/test_v2_m5_shadow_entry_guard.py`：验证完整合法参数请求提前拒绝、输入读取/fit/runner/注册均未调用、输出不存在，以及原参数错误仍保留既有退出码。普通其他模式继续受原 CLI 完整测试约束。

由 Root 绑定实际 CLI 的 ZCode owner，在独立候选及合法写入边界实施。验收为新测试、原 CLI 完整人口和独立反例；旧 T8 原件与 fail-closed 接受不改。入口保护接受只能关闭“晚失败且提示不明确”的保护切片，**不能关闭 P0-2 shadow 或 P1-2**。完整 ordinary shadow 继续沿原 B0–B8 和实际适用的合同依赖，要求真实同 pack、非空资格、训练、恢复、checkpoint、SH-T01–08 及独立硬审。

**7. 修复顺序增补与并行安排**

本段作为 r3 修复顺序的增补，原 22 项核验历史不改写：

1. **新增 A2/N1，P0**：新隔离候选立即进入一次计划意见及作者处置，随后专用 ZCode 修复、完整必要验证、冻结和独立硬审。所有采用区间三族的新研究/paper 消费者须先取得修复接受；未受影响的既有三方向族冻结工作可继续。
2. **A3 与 B1 并行**：A3 只做上述两文件文档同步。B1 可立即由默认 OpenCode 辅助者读取现有 C05 原始结果，按节点/收集文件列出类别、证据、实际 owner 或待绑定项及下一步；不等待本计划意见，不写产品。
3. **B1 保留原红色基线**：171 failed、60 errors、3 collection errors 不能自动解释成 234 个独立根因。哈希接受链、环境依赖、时间敏感和产品缺陷分别核对；外部报告的因果推测不替代复现证据。不删除测试、不加 xfail/忽略、不改阈值或 pins；确认的修复回实际模块 ZCode owner。
4. **T7-B011 按原冻结继续；A4 先记录有限保护方案**。不为 A2 重跑在途冷链；以后消费 A2 时再完成精确后继接受。完整 shadow 仅阻断其实际消费者。
5. **用户操作与提案独立保留**：A1/P0-1、仓库可见性/许可、F1–F6、分支保护、规范化和支持版本未获本批实施决定，不作为离线 A2 总门。

原策略、统计、时序和研究合同继续有效；本增补不消费 holdout，不降低样本/日期块/成熟度门槛，不把 SYNTHETIC 变成历史 PIT，也不启动新的研究或 paper。

所有后续工单保留原生 stdout、stderr、exit；仅可选 `rg` 的 raw 1 且空 stdout/stderr 可记 NO_MATCH。必需输入先确认存在，生成输出不是输入；历史失败及特殊一次性授权原件保留。继续执行 `20261004-rg-no-match-r1/INCIDENT.md`、`20261004-grok-headless-cancel-r1/INCIDENT.md`（含 manifest-recovery-002 exit 3）及 `20261016-r1-custody-path-normalization-r1/INCIDENT.md`。

本作者本轮仅提供计划文本，没有产品写入、运行或产品 PASS。全程保持 `enable_trading=False`、observer=`hold`、broker=`NOT_RUN`、真实 send/cancel=`0/0`；不读取凭据或配置，不操作网络服务、生产数据库或客户端。