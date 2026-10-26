# REVIEW.md — A2 最终独立产品硬审（a2-hard-review-001，2026-10-26）

- 席位：独立 ZCode GLM-5.3/max 成果审核（sess_89678d66，未参与 A2 开发/修复；本席仅做过计划意见与静态准备）。
- 对象：冻结候选 `C:/AKWorktrees/aktr-round2-a2-20261026` 的 **11 文件**写集（`zcode-dispatch/a2-owner-eol-004/evidence/SOURCE-HASHES.json`）。
- 判定依据（三份生效作者文本，均在批次根下）：`astra-phase0-plan-001/PHASE0-PLAN.md`（sha `f1855d6b…8aa61`）、`astra-phase0-disposition-002/PHASE0-DISPOSITION.md`、`astra-a2-m4-contract-ruling-004/A2-M4-CONTRACT-RULING.md`。
- 本审**无条件、限 A2 范围**：不等于全套测试通过、不授予 shadow 可用性、不构成旧消费者对新代码身份的接受。

## 1. 冻结与身份核验

- **before/after 双相针**（`evidence/00-pins/`）：11 个冻结文件（M4 `d62959fb…`，其余十文件与静态准备 001/002 哈希一致）+ 三个输入针（我的探针 `3221405a…`、外部探针与批次基线逐字节相同、`.review-inputs/A2-TEST-INPUT-SLICE.json`）在全部测试**前后**均逐字节匹配（`all_match: true`，14/14 行）。评审期间无人改写被审对象。
- **环境护栏**：`evidence/00-env-guard/` — 显式 env 增量 `PYTHONPATH=<候选>/src`、`PYTHONUTF8=1`；实际导入的 `registry/artifacts/state` 模块绝对路径均属候选。探针内置 registry 来源守卫在离线守卫上下文内再次生效（步骤 02/02b 通过即证明）。`capture_run.py` 的 `env_pythonpath` 字段（=sys.path[0]）按工单不作为环境证明，仅以上述双护栏为准。
- **当前代码身份**（`evidence/08-code-identities/`，只读记录，未触碰任何旧绑定/锁/消费者清单）：7 个族级 `code_sha256_for`（族模块+registry+state；每族两频共享），14 个现行 `legacy_exit_binding` 活值——其前缀与完整运行中 14 项失败断言的活值逐一吻合（如 breakout_m1 `0118e630a937…`）。

## 2. 独立必需测试（全部本席自有命令与原始流；owner 结果未替代）

全部经 `scripts/test_offline.py`，候选 cwd，精确展开 argv 见 `evidence/expanded-argv.json`；原始 stdout/stderr 分流、sha256、原生退出码见各目录 `*.RESULT.json`；16 个流全部整体严格 UTF-8 可解码（`evidence/09-stream-decode-verify/`）。预算：完整 3600s、其余 600s（完整实跑 1661.1s）。

| # | 人口 | 结果 | 原生退出 |
| --- | --- | --- | --- |
| 1 | FACTS `error_nodes` 精确 26 节点（去 ` (setup)`） | 26 selected/ran/passed，0 失败/跳过/错误 | 0 |
| 2a | 本席反例（原版 `…prepare-002/probes/test_a2_reviewer_probes.py`） | 17 selected，16 passed，1 failed（见 §3） | 1 |
| 2b | 本席反例修订 r2（`counterexamples/test_a2_reviewer_probes_r2.py`） | 17 selected/ran/passed，0 失败/跳过/错误 | 0 |
| 3 | duplicate_bar + martingale | 110/110 passed | 0 |
| 4 | 原样外部探针（与基线钉定副本逐字节相同） | 9/9 passed | 0 |
| 5 | 全部 `tests/test_v2_m3b_*.py`（8 文件） | 308/308 passed，0 skip/error（3 个真实输入测试实跑） | 0 |
| 6 | 全部 `tests/test_v2_m[1-4]*.py` | 962 ran，948 passed，**14 failed**（下节），0 error/skip/collection | 1（预期保留） |

## 3. 反例腿与唯一评审侧事件

工单/准备稿称"15 反例"，实际参数化为 **17 节点**（C2 为 10 非 8：6 族×MINUTE + TREND/REVERSION×双频锚定）——本席计数笔误，如实更正。

**唯一失败 = 本席构造缺陷，非产品缺陷**：原版 `test_c3_breakout_gate_failure_beats_created_restart` 向 BREAKOUT 送第 4 根延续 bar（s3），但 3 根 setup 窗口只覆盖创建+2 延续（`setup_expiry_nano` 将创建 bar 计为第 1 根，永久套件同类测试注释明示此点并改用新建步骤）。产品在 s3 的实际行为完全正确：过期 setup 取消、不转信用、WAIT/0、不 OPEN（实测轨迹 `[0,0,1,2,0]` 全 WAIT）。处置按工单：原版与其 raw 流原样保全（步骤 2a，raw 1），修正仅入**新本地修订 r2**（`counterexamples/test_a2_reviewer_probes_r2.py`：第 5 送改合法新建 `arm`，期望 `[0,0,1,2,1]` 全 WAIT + 新 setup_id；其余 16 探针逻辑逐字未动），r2 全绿（步骤 2b，raw 0）。无任何断言被弱化以掩盖产品失败；无产品修复。

通过的反例判别力（对旧缺陷代码均会失败/提前 OPEN）：C1 陈旧信用转移（计数 2→过期→重启重建→首个 recross **必须不 OPEN**）；C2 过期同 bar 重送六族（不 OPEN、计数≤1、为 1 必属新观察期）；C3a 门控清零优先+同 bar 恢复不补计；C3b 创建遇门控失败仍为 0。

## 4. 完整 M1–M4 与基线逐项对照（`evidence/failure-comparison.json`）

- 计数：962/948/14/0/0，raw 1，与封存基线（866/852/14）同构扩容 +96 新人口，零新增失败/错误/跳过/收集错误。
- **节点集**：14 个失败节点与 `baseline-m1-m4-001/SUMMARY.json.failed_nodeids` **逐节点完全相等**（七族×两频 `test_forwarded_binding_keeps_frozen_baseline_value`）。
- **原因**：14 项全部为同一冻结常量比较断言（`pipeline.legacy_exit_binding(spec).binding_sha256 == _L2_BASELINE_BINDING_SHA256[…] == select_exit_binding(…)`，`tests/test_v2_m4_payoffs.py:243` AssertionError，活值≠冻结值）。冻结侧常量未被刷新（如 breakout_m1 冻结值 `081f58e3c681…9e55a2e28363d` 与基线原值相同）；活值随源码变化属裁定明示的允许差异，未借 14 项豁免任何新失败（无新失败可豁免）。
- 工单的 `raw 1` 为预期旧测试结果，未与 CLI/任务失败混淆；完整运行仅在短项/反例全过后执行。

## 5. 计划符合性（静态复查结论保持，此处仅引证）

- 10 文件 A2 主体与 M4 修复全部在三份生效文本授权内：helper/F1 规则、四钉住控制、artifacts 单项追加、M4 边界两侧控制、EOL 字节恢复后 M4 哈希 `d62959fb…` 为最终冻结值。低级观察 F-M4-1（fixture 内 import）经工单裁定为授权内必要支持；F-M4-2（EOL）已由同一 owner 恢复并冻结。
- 交易边界全程 `enable_trading=False`/observer hold/broker NOT_RUN/真实 send-cancel 0/0；未读凭据/.env/配置；未改候选/主树/旧证据/旧清单；无 git 操作。

## 6. 评审侧披露（不影响判定）

- 步骤 1 报告行 `unresolved_files 1`（m4_payoffs）：节点 ID 级选择下 runner 的文件级归集归因产物；selected/ran/passed 26/26/0 错误为无歧义结果。
- `compare_failures.py` 经两次修正（短名匹配、多行断言检测、ok 布尔化）；最终 `failure-comparison.json` 出自修正版，`ok: true`。
- 工单预期"15 反例"与实际 17 的差异（§3）；`record_identities.py` 首版误用 `code_sha256_for(family, mode)` 签名，修正为族级 7 值 + 14 活绑定双记录。
- Root 收据 `ROOT-OWNER-REPAIR003-VALIDATION.json` 在位（2387 B）；owner 数字（26/110/9/308、962/948/14 raw1、1644.2s）与本席独立复跑全部一致。

## 7. 范围与结论

A2 有效计划要求的全部可执行验收——外部探针 9、永久回归（M3B 308 零跳过）、聚焦 110、受影响 26、完整 M1–M4 962 仅存 14 项同节点同原因历史绑定失败、无新失败/错误/跳过、写集与冻结一致、旧常量未刷新、T7-B/M5 冻结输入未动——均由本席独立命令与原始流证实。判定见 `VERDICT.json`：**无条件 PASS（限 A2 范围）**。该 PASS 不关闭 P0-2/P1-2 shadow、不代表全套测试绿色、不授予旧绑定/锁/消费者对新身份的信用；后续消费者按裁定走真实新身份再冻结与接受。
