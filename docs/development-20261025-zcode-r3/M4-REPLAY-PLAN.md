# P0-2：M4 replay 资格证据接线修复

日期：2026-10-25。有效版本：2026-10-25 作者处置版（EFFECTIVE）。独立 ZCode 的一次计划意见已由 Astra 作者[逐项处置](M4-REPLAY-DISPOSITION.md)，本计划立即供 ZCode 开发使用，不设计划 PASS 门槛，不等待 C05。本文件不宣告产品修复或硬审核通过。

## 1. 授权、契约与依赖裁定

本次仅承接本目录 `DISPATCH.json`（当前 LOCAL_ONLY 派工记录）的 replay 分离规划；作者为 Astra/max 会话 `01a171b2-3f04-7413-85f6-fb5423ee127d`，原模块规划者 `/root/plan_research_astra` 当前不可恢复，不冒称原身份。实现归专用 ZCode 开发会话，硬审归另一独立 ZCode GLM-5.3/max，会话/store/cwd 由 Root 派工时绑定。当前[角色](../../PROJECT-MODEL-ROLES.md)及[流程](../../PROJECT-WORKFLOW-RULES.md)覆盖历史 Sol/Claude 开发指派。

继承 [r3 实施计划](../strategy-first-blueprint-20261015-r1/revisions/r3-001/IMPLEMENTATION-PLAN.md)和[修复顺序 P0-2](../strategy-first-blueprint-20261015-r1/revisions/r3-001/REPAIR-ORDER.md)。[已核实审计](../repo-audit-20261025-claude/VERIFICATION.md)确认 replay 已计算退出注册表和绑定表，但调用真实资格 verifier 缺少两个必填关键字，TypeError 被记为坏证据。本轮不重复审计诊断。

**裁定：本修复可独立于 R19→R20→R21 开发及硬审。** 原 [M4 计划](../devplan-v2/05-M4-联合决策与账本.md) §4、§5.2、§10.13 和 H10 已要求完整资格证据准入；[变更申请 007](../devplan-v2/变更申请-007.md) §5.4、§7.1 已明确 `verify_qualification_evidence` 的必填 `exit_registry_sha256` / `exit_binding`，并要求 replay 显式消费 `exit_specs` / `exit_bindings`。当前类型与 verifier 已有这些接口，仅补调用接线，不消费 ordinary SH checkpoint/schema，不新增公共类型或默认绑定。故不需要 SH-M1 schema 裁决，也不取得其所有权。R19 文件释放、R20 schema 裁决、R21 SH-M1 冻结继续约束真正消费它们的 ordinary-shadow 路径；本成果不能据以验收 shadow、SHCP7 或 M5。

## 2. 候选基线与精确写集

Root 从 tracked `0cc305454d06643e93b14e426d04a6a755094d84` 建立独立 worktree，记录基线与输入逐文件 SHA-256；canonical 377 文件冻结持续有效。这是新候选切分，不是释放旧冻结、采纳外部 M5 候选或回写 canonical。旧实现者确认停写并显式移交后才由新 ZCode owner 写入。

Root 在实施派工时新建本目录 `REPLAY-HANDOFF.json`，记录 replay candidate 路径、branch、上述基线、下表两文件 write_set、实际 ZCode owner/session/store/cwd、有效计划版本及 hash，并记录旧 writer 已停写/移交，或核实“无现任 writer”的事实。该文件是 Root 待生成的派工输出，本文不假定已存在；原 `DISPATCH.json` 已被活跃调用固定，保持原字节。此记录补足现有派工证据，不新增审批或重复计划审核。

仅允许改两个候选文件：

| 路径 | 变更 |
| --- | --- |
| `src/ak_trading/v2/decision/replay.py` | 为现有 verifier 传递同次回放的真实退出依赖；拒绝会被字典折叠的重复策略绑定 |
| `tests/test_v2_m4_replay.py` | 非空有效资格证据回归、错误证据及绑定反例；需要的纯内存 fixture 私有 helper 写在此文件 |

不改 `artifacts.py`、公共 schema/类型、`pipeline.py`、ledger/gates、M5、普通 shadow、其他 owner 测试或 frozen manifest。新发现必须跨界改动时保留复现并交所属 owner，不能以扩大本写集解决。

必读输入为上述原 M4/007 对应条款、`src/ak_trading/v2/artifacts.py::verify_qualification_evidence`、现有 replay、`tests/test_v2_m4_replay.py`、`tests/test_v2_m4_ledger.py::exit_fixtures_for`、`tests/test_v2_m4_gates.py::example`，以及 `tests/test_v2_m1_artifacts.py` 的 `_evidence_chain`、`_exit_kwargs` 和 `test_verify_qualification_evidence_chain`。它们均为仓库跟踪文件；本轮候选无需复制 LOCAL_ONLY 历史数据、ordinary-shadow 契约或外部候选。fixture 用现有冻结类型、规范编码和生产哈希函数生成，明确 `SYNTHETIC/SCENARIO`，不发布统计资格或消费 holdout。§4 的八个原 M4 测试文件亦均已跟踪。

## 3. 实现要求

1. 沿用 `exit_registry_hash_for(exit_specs)` 的本次计算结果；资格验证、账本构造/恢复和回放上下文引用同一退出注册表。对唯一匹配的 strategy，传 `exit_binding=bindings_by_strategy[q.strategy_id]` 和 `exit_registry_sha256=exit_registry`；不从资格本身取值反向充当可信依赖，不隐读文件/全局缓存，不临时生成 legacy 绑定。
2. 在建立绑定字典前核对重复 strategy_id，连同已有 exact-spec-set 检查拒绝重复、缺失、多余或错误归属；不以 dict 覆盖把歧义变成“唯一”。既有 spec、entry code、exit spec 集和资格范围的真实核验仍交原 verifier。保留公共位置参数及必填关键字 API，不加默认值。
3. 保留坏证据的 fail-closed 语义。verifier 对非法输入类型本来就可能抛 TypeError，不能机械删除所有 TypeError 捕获或改造异常框架。身份/hash/解析拒绝使用的 `ArtifactsError` 继承 `ValueError`，已由 replay 的 `except (ValueError, TypeError)` 捕获，经 `invalid_q` 映射为 `QUALIFICATION_EVIDENCE_MISSING/INVALID`，负例须验证该拒绝可见。本修复消除调用漏参；不放松 verifier、资格有效期、六场景、报告独立性、完整预登记组或统计阈值。
4. OPEN/ADD 仍走原 `evaluate_gate → enforce_account_risk → choose_proposal`；错误/过期资格不新增风险，REDUCE/EXIT 的原安全通道不受资格缺失阻断。研究与影子目的标签保持，修复不自动授予运行资格。

## 4. 必要验证与原 M4 总体

新增回归必须在同一可交易的确定性 fixture 上作正反对照；只断言函数被调用或空资格 WAIT/CASH 成功不足以验收：

| 场景 | 必须观察到的结果 |
| --- | --- |
| 非空、完整有效资格 | 至少一个 qualification；完整 `experiment/metrics/review` 原字节及 SHA-256、同次 entry/exit 注册表、模型束、范围、预登记组和六场景一致。先以真实 verifier 验证可接受，再走真实 replay；资格不被误记 missing/invalid，原其他门槛满足时 SHADOW_OPERATIONAL 的 OPEN/ADD 可以正常准入 |
| 缺证据/篡改字节/错误 hash | 分别缺失被引用文件、原键下改字节、重算 hash 后改变语义范围；真实 verifier/gate 拒绝，拒绝原因可见，新增风险权重为 0 |
| 错绑定/错注册表/错 spec | 与本次冻结依赖不匹配即拒绝；不能用另一策略或另一退出版本的 PASS 替代 |
| 绑定缺失/多余/重复 | 在 replay 边界明确拒绝，包含重复同策略绑定被 dict 折叠的反例 |
| 资格有效期 | `issued_at ≤ decision < valid_until`；过期及尚未生效不得新增风险，边界按原契约；REDUCE/EXIT 仍可降风险 |
| 原语义回归 | RESEARCH_EVALUATION 标签、四政策、已有 episode 绑定、恢复/因果性/费用/尾部风险沿原测试；无公共 schema、validator 或阈值变化 |

允许复用现有 scheduler 的确定性预测/策略 fixture；资格 verifier、资格 gate、路由不得 mock 成成功。测试证据中的 PASS 是合成校验输入，不是真实研究或独立产品意见；不导入未验收 M5 产品来生成它。若用 spy 记录入参，必须原样委托真实 verifier。

先运行新回归及完整 replay 文件，再运行原 M4 §9 M4-09 的**八文件完整总体**，不使用 `-k` 缩减总体：

```text
python scripts/test_offline.py -q tests/test_v2_m4_replay.py
python scripts/test_offline.py -q tests/test_v2_m4_payoffs.py tests/test_v2_m4_gates.py tests/test_v2_m4_routing.py tests/test_v2_m4_risk.py tests/test_v2_m4_execution.py tests/test_v2_m4_ledger.py tests/test_v2_m4_replay.py tests/test_v2_m4_causality.py
```

验收保持原 M4 §10/H08 的完整因果性、恢复、手算及尾部压力要求。本接线不是 M5 fit/full100 任务，不移植 M5 的耗时预算，也不采用未经证实的约 14400 秒。原 M4 条款规定测试总体，未在所引条款给出墙钟上限；Root 如设置运行监督期限，必须绑定该 M4 lineage 的实际记录并允许完整执行。超时是未完成验证，不能计成功或缩减总体。此处显式测试路径不收集 DP03/DP08，因此 C05 隔离修复不是本模块前置。

## 5. 完成证据与交接

开发者提交精确两文件 diff、前后 SHA-256、使用的基线/规则/原契约、每条命令的 stdout/stderr/raw exit、实际 collected/executed/pass/fail/skip 数，以及正例真实 verifier 接受和各负例拒绝证据。原八组失败不得隐藏；独立阻塞逐项列明，不能给完整 M4 验收。实现、必要验证和新候选冻结后，立即交独立 ZCode 按本写集与继承要求硬审；FAIL 回同一 ZCode owner，以新冻结修订复审。作者处置计划意见不等于成果 PASS。

T7 的 `tests/fixtures/v2/ep1_t7/acceptance-input-manifest.json` 消费 replay 源码 pin；本候选合法接受并集成时，将其记录为 P1-3/T7 关闭队列中的新变更来源。T7 后继基线发布须晚于本修复集成，或显式纳入本修复的接受版本与实际 hash，避免立即再次漂移。既有 T7 11 drifts 仍按原事实逐项处理，不盲刷 pin；该协调不是 replay 开发或硬审前置，也不授权修改旧 T7 清单。

完成范围仅为 replay 接线及其回归；P0-2 的 shadow 分支另行闭环。研究状态/模拟状态均不因本修复升级；`enable_trading=False`、observer=`hold`、broker=`NOT_RUN`、real send/cancel=`0/0`。禁止 SDK、网络、真实 broker、服务、部署、DB、凭据/.env 或持久模型配置操作。

命令纪律继承 `docs/maintenance/20261004-rg-no-match-r1/INCIDENT.md` 和 `docs/maintenance/20261004-grok-headless-cancel-r1/INCIDENT.md`（均为 LOCAL_ONLY，Root 派工需提供），包括 manifest-recovery-002：立即分别捕获原生退出码、stdout、stderr；仅可选 `rg` 的 raw1 且空输出/空 stderr 可记 NO_MATCH，测试/CLI raw1 不适用。必需输入先查存在，缺失是错误；生成输出创建前不作为输入读取。保留原失败、会话和已消费的一次性命令；产物完成、进程退出、证据封存与产品 verdict 分开，不重跑已消费动作，不把仍打开的日志封成最终 hash。
