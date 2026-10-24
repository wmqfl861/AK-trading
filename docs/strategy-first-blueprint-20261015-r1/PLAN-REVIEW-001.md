原始 ZCode response 的文本提取；原始 stdout/终态不改。Root 在终态后核对三文档与启动前工具记录中的 SHA-256 一致；审核者独立计算 hash 为 NOT_RUN。

# 独立计划审核报告：strategy-first-blueprint-20261015-r1

## 结论：无条件 PASS

三份文件（`BLUEPRINT.md`、`IMPLEMENTATION-PLAN.md`、`REPAIR-ORDER.md`）通过全部五项审核问题，无阻断问题。以下为逐项核验证据，以及三条按规则只作记录、不构成额外审批门的非阻断观察。

## 审核范围与限制

- 只读审核，未修改任何文件，未运行测试、fit、数据下载或任何研究/网络/客户端/交易操作。
- 三份文件已全文读取：`BLUEPRINT.md`（88 行）、`IMPLEMENTATION-PLAN.md`（93 行）、`REPAIR-ORDER.md`（63 行）。
- 对照证据：`docs/project-reassessment-20261015-astra-r1/` 四份只读复评报告，以及对主树源码的直接读取（`src/ak_trading/v2/validation/experiments.py`、`src/ak_trading/v2/decision/ledger.py` 等）。
- **哈希记录为 NOT_RUN**：本会话处于计划模式且无 shell 工具，无法计算三份文档的 SHA-256；以完整路径＋行数＋全文读取作为本次审核的对象标识。此限制不影响任何结论。

## 逐问题核验

**问题 1：RB-A/B-1 范围是否满足既有策略、PIT、风险、证据、交易安全契约 — 满足。**
交易安全边界与 `AGENTS.md` §4 完全一致：`enable_trading=False` 默认、observer 恒答 hold、模拟 acceptance 需研究出口冻结＋execution hard PASS＋20 交易日 feed/clock/shadow＋真实 send/cancel=0＋用户明确授权与 UI 开启，broker 保持 `NOT_RUN`（BLUEPRINT §7、IMPLEMENTATION-PLAN §6、REPAIR-ORDER P3）。研究不变量（252/60/60、purge/embargo 沿现有契约、永久留出、基线与压力并报、连续账本、未知即空仓、SYNTHETIC 标注、日历缺口如实记录）与 STRATEGY-REVIEW“应保留的部分”及 M5 A/B 计划合同一致。首批收窄到三族并保留 `NOT_RUN/BLOCKED/INSUFFICIENT` 真实状态，是再评估报告明确认可的排程（“不会降低最终门，只减少浪费”），不构成降标。

**问题 2：依赖、文件归属、接口、步骤、验证、停止条件可执行，未新设门、未降标 — 成立。**
写集引用的文件全部实际存在于主树：`contracts.py`、`artifacts.py`、`probability/`、`strategies/`（含首批 `trend.py`/`breakout.py`/`pullback.py`）、`decision/`、`validation/experiments.py`、`splits.py`、`metrics.py`、`reports.py`、`scripts/research_probability_v2.py`。验证要求全部源自既有合同与事故规则（exit 1 不按 `rg NO_MATCH` 解释对应 AGENTS §7；生成输出先记 `ABSENT`、产生者终止后封存对应 §8；M1 计数与 exact set 同步来自 CODE-INTEGRATION-REVIEW 修正 3；P2 分段 profile 且保留原预算来自其修正 4）。实施顺序（IMPLEMENTATION-PLAN §4）遵守门禁同时允许门前准备，§4.5–4.6 的立即硬审与 FAIL 回原 owner 追加修复符合 AGENTS §9。未发现任何新增验收标准或放宽。

**问题 3：scenario/工程证据与真实历史研究、模拟 acceptance 的区分 — 正确。**
BLUEPRINT §2 将既有 RB causal/scenario/capital 成果限定为 `SCENARIO/CALENDAR_UNVERIFIED`，§8 明确 41/42 属“待修复契约差异，不是策略失败证据”；REPAIR-ORDER“禁止的捷径”禁止把 SCENARIO 或局部工程 PASS 改写为历史策略 PASS；IMPLEMENTATION-PLAN §6 将四种研究出口与 `ENGINEERING_FAIL` 分列，模拟 acceptance 另行设门。与 REASSESSMENT-REPORT 结论段的证据级别一一对应。

**问题 4：四项缺陷描述是否准确 — 全部准确，且经源码直接核实。**
- **M1 41/42**：CODE-INTEGRATION-REVIEW F03 证实——候选 `artifacts.py:131-142` 已纳入新测试使 required map 实为 42 文件，`test_v2_m1_artifacts.py:556-568` 仍断言 `len(required)==41` 且 exact set 未含该文件，owner 运行 native 1、184 passed/1 failed。BLUEPRINT §2 与 REPAIR-ORDER P1（“同时纳入预期 public 集合、计数和 exact set；不能只把 41 改成 42”）均与证据吻合。
- **snapshot P2 超时**：F04 记录 population 25,679、deadline 300s、elapsed 308.719s、native 1、`COMMAND_DEADLINE_EXCEEDED`、peak 121,106,432 bytes、`MemoryError_observed=false`、`PARTIAL_UNVERIFIED`。BLUEPRINT 的“超时不是 OOM”及 REPAIR-ORDER 的“不得用增加 timeout、减少人口或删断言替代修复”与原始 RESULT 证据一致。
- **DAILY seam**：状态 `SCOPED_INDEPENDENT_REVIEWER_PASS / INCOMPLETE_NOT_ACCEPTED / NOT_RELEASED`（relay adapter native 3）。三份文件均要求 scoped PASS 与整链放行分别记录，不得自动升级，与 CODE-INTEGRATION-REVIEW 修正 5 一致。
- **R5 `_reg`**：我直接读取 `experiments.py:561-618`——第 603 行确实以 `exit_registry_sha256=_reg` 传入 `ContinuousLedger`，函数体内无 `_reg` 赋值；第 604 行 `ledger.checkpoint((),(),dependencies,{},pending_remaining_lots={})` 对照 `ledger.py:321-327` 真实签名（`exit_registry_sha256: str` 为无默认值的必填 keyword-only 参数）必然 TypeError。修复方向真实可行：`_exit_pack(specs)` 确实存在于 `experiments.py:1282`。REPAIR-ORDER P2 的条件前置（仅在真实消费者使用 shadow 时）与再评估“不应成为全项目串行门”的裁定一致。

**问题 5：真实 M5 A/B 的具体必需结果与安全模拟过渡 — 已定义。**
REPAIR-ORDER P0（M5 A/B）给出具体出口：MINUTE/DAILY required population、三族、四个 policy、六个 scenario、冻结候选、不读最终留出，产出五态（`RESEARCH_PASS/STATISTICAL_FAIL/INSUFFICIENT/BLOCKED/ENGINEERING_FAIL`）之一的真实研究包并完成独立 ZCode 硬审，且明示为“本批的完成条件”。安全过渡由 P3 与 IMPLEMENTATION-PLAN §6 定义：研究出口冻结＋execution hard PASS＋20 交易日 feed/clock/shadow＋send/cancel=0＋observer hold＋用户明确授权与客户端 UI 操作为最后门，真实订单明确排除在本批之外。

## 非阻断观察（仅记录，不构成新审批门）

1. **P2 原预算数字在上下文报告间不一致**：REASSESSMENT-REPORT.md 第 25 行写“原 2 GiB/1 GiB/**3,600 秒**”，而 CODE-INTEGRATION-REVIEW F04 与原始 RESULT 证据为 **300 秒** deadline（elapsed 308.719s 已超）。计划正确采用了 300 秒；建议后续 snapshot 修复工单显式 pin 权威预算记录（`D:/AKS261014/M1/snapshot-provenance-r1/owner-evidence/runs/p2-001/RESULT.json`）以消除歧义。
2. **`experiments.py` 的归属表述**：IMPLEMENTATION-PLAN §3.D 将 `validation/experiments.py` 归入“唯一 M5 owner”写集，REPAIR-ORDER P2 负责人为“R5 原 owner”。若两者非同一 owner，需按 BLUEPRINT §5 的文件所有者规则（“R5 的 `_reg` 修复只能由其文件所有者处理”）裁决；建议派发时显式写明身份关系。
3. **`exit_bindings` 的失败语义**：`ledger.py:325` 中 `exit_bindings` 带默认值 `()`，硬 TypeError 仅由缺失 `exit_registry_sha256` 触发；BLUEPRINT“缺 `exit_registry_sha256` 与 `exit_bindings`”在集成身份层面成立（两者均未传入），但下游修复工单宜沿用 F02 的精确表述（hash 缺失致 TypeError 与绑定身份缺失分开验证）。

## 界限声明

本次为**计划审核 PASS**，仅覆盖上述三份规划文件的内容正确性。按工作单与项目规则，本 PASS 不授予任何实现授权、研究结论或产品硬审信用；实现后仍需冻结写集并经独立 ZCode 产品硬审，模拟交易与真实订单另需既有门与用户授权。
