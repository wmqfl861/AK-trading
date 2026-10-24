# v2 风险指标口径说明 sidecar（NF30 / C30）

本目录说明 `src/ak_trading/v2/validation/risk_semantics.py` 与 `scripts/explain_v2_risk.py`
提供的**只读**风险指标作用域与来源解释 sidecar。它不重算风险、不修改任何生产者产物
（`metrics.py` / `experiments.py` / `reports.py` / `decision/replay.py`）、不改变
metrics/risk_report 的既有 schema、group keys、资格判定、回放结果或冻结 lock，也不向旧
schema 追加字段。校验 sidecar 不构成任何 statistical PASS、SHADOW_ACCEPTED 或交易资格。

## 解决的问题

- M5 `experiments.py:1306-1341`（`score_strategy_group`）按 `strategy_id` 计算完整轮损益、
  EV、CVaR，但同一 strategy_groups 行在 `:1338-1340` 又原样复制整个 RunPolicy 账户的
  回撤、保证金、库存与恢复期。BEST_EV 账户运行多个策略时，并列字段没有逐字段
  scope/basis，读者可能把账户回撤误读成单策略回撤。
- M4 `replay.py:619` 的 `risk_report.loss_cvar95_cny` 是**提案模型** CVaR 的最大值；M5
  `metrics.py:339-341`（账户日权益 CVaR）与 `metrics.py:348-357` / `experiments.py:1314-1319`
  （完整轮 CVaR）是不同总体的样本统计。同名 CVaR 不可互相比较或合并。

## 冻结的口径区分（C30 契约）

| 口径 | scope/basis | 分母/限制 |
| --- | --- | --- |
| 普通成本场景的单策略完整轮损益、EV、尾损失 | `STRATEGY_CLOSED_EPISODES` / `CLOSED_EPISODE_CVAR` | 完整 episode 数、对应日期块；未闭合仓不混入 |
| RunPolicy 账户权益回撤与日损益尾损失 | `RUN_POLICY_ACCOUNT_EQUITY` / `ACCOUNT_DAILY_EQUITY_CVAR` | 该账户有效日权益样本；不能分摊给其中各策略 |
| 准入候选的模型尾损失 | `PROPOSAL_MODEL` / `PROPOSAL_MODEL_CVAR`（保留 max 聚合说明） | 提案模型估计，不是已实现的损失样本 |
| 保证金占用 | `RUN_POLICY_ACCOUNT`；`ACTUAL_MARGIN_ONLY`（risk_report）vs `ACTUAL_PLUS_RESERVED_MARGIN`（metrics） | 显示实际 producer 定义；不同 basis 不得自动合并 |
| GAP/NO_STOP 压力行损益、短缺、峰值保证金、最大压力损失比 | `RUN_POLICY_BASE_COHORT_STRESS` | BASE 完整轮构造的整账户压力 cohort；不能按 strategy_id 过滤 |
| GAP/NO_STOP 压力行的 max_drawdown_ratio / longest_recovery_days | `RUN_POLICY_BASE_COHORT_STRESS` / `PLACEHOLDER_SEED_NOT_EVALUATED` | 生产者不对压力行评估该两字段（复制循环只复制 5 键、`g.update(stress)` 不写它们、评分循环对压力行不执行）；值保持 `reports.py:98-101` 占位种子 `0.0`/`null`，不代表实测零回撤或恢复期结论 |
| maximum_inventory / max_inventory | `ACCOUNT_GROSS_LOTS_MAX` | 单位 lots；不是名义金额或跨品种可比的风险敞口 |

补充语义：

- **压力行字段来源（按场景门控，F01 修复）**：GAP/NO_STOP 行上 `peak_margin_cny` /
  `margin_shortfall_cny` **只**输出压力路径口径条目（`STRESS_PATH_PEAK_MARGIN_MAX` /
  `BASE_COHORT_STRESS_PATH_SHORTFALL_MAX`，`experiments.py:1481` 的 `g.update(stress)` 用
  `evaluate_stress_cohort` :1262-1268 的路径极值覆盖了该值），**不**再输出普通行的账户
  `ACTUAL_PLUS_RESERVED_MARGIN` / `ACCOUNT_MARGIN_PLUS_RESERVED_MINUS_EQUITY_MAX` 条目——
  普通场景的账户快照取数（`metrics.py:317-320` → `experiments.py:1338-1340`）只在评分循环
  （:1496-1500）执行，压力分支在 :1484 `continue` 跳过。`max_inventory` 是复制循环
  （:1479-1480）5 键之一：值为 BASE 回放的账户 lots 峰值，经 BASE 组行复制到压力行。
- **FIXED 一对一**：只有当源材料证明策略与账户精确一对一时（routing FIXED、
  fixed_strategy_id 匹配、同 policy 下没有其他策略贡献 episode），账户字段才标注
  `scope_relation=EXACT_SINGLE_STRATEGY_ACCOUNT`；scope 仍保留账户级。
- **空压力明细**：`risk_report.stress_losses` / `margin_shortfalls` 为空对象时，产物内
  无法区分 NO_BUDGETS 与 RULES_NOT_SUPPLIED（`replay.py:608` 的 `if rules:` 需调用证据），
  记 `UNKNOWN_EMPTY_REASON`；空 ≠ 零压力损失。
- **null 恢复期**：`longest_recovery_days=null` 表示期末仍未恢复的回撤
  （`metrics.py:383`），长度未知，不能当作 0 天。
- **已知小计**：回放不完整时（`status!=COMPLETE`）的金额只能当“已知小计、不可作完整
  风险结论”，不得升级为完整风险或 PASS（`reports.py:60-67` 终端恒等式仅在 COMPLETE 时
  强制）。
- **分母纪律**：denominator 状态只有 `EXPLICIT / KNOWN_NOT_EXPOSED / NOT_APPLICABLE /
  UNKNOWN`；只有输入明确提供的数可填 EXPLICIT，其余保持未知，不从总交易数、日期跨度或
  字段名猜测。

## 用法

```
python -B scripts/explain_v2_risk.py \
  --metrics <metrics.json> \              # ak_probability_metrics_v2.1.4
  [--risk-report <risk_report.json> \     # ak_probability_risk_report_v2.1.0
   --binding <binding.json>] \            # 显式只读绑定（见下）
  --output-root <新目录>                  # 必须不存在；绝不覆盖既有证据
```

binding JSON 必须显式给出 `artifact_sha256, split_id, run_policy_sha256, account_id,
model_bundle_sha256, cost_scenario`（`risk_report.json` 自身缺少 run/group 关联字段，不从
目录名推导）。字段齐全且与 metrics 内容一致 → `mapping_status=MAPPED`；缺字段 →
`binding_status=UNBOUND`、`mapping_status=PARTIAL_UNBOUND`（只给字段级说明，禁止具体
账户/策略 join）；字段被伪造（hash 不符、账户/策略组合不存在）→ 拒绝并非零退出。

输出（写入显式新输出根，内容确定、无时间戳）：

- `risk-semantics.json` — schema `ak_v2_risk_semantics_v1`，根对象固定为
  `schema_version, semantics_catalog_sha256, producer_identity, inputs, artifact_bindings,
  field_semantics, joins, limitations, mapping_status`；每条字段说明含
  `artifact_sha256, json_pointer, record_identity（保留 group_key）, metric_name,
  scope_kind, basis, population, aggregation, unit, sample_unit, denominator,
  group_or_account_ref, producer_identity, producer_rule_id, completeness, null_semantics,
  source_evidence, explanation`。原值一律指针引用（RFC 6901 转义、逐个实际解析），不重算
  或篡改精度。
- `risk-semantics.zh.md` — 中文导读（三类 CVaR 对照、账户 vs 策略、压力行、null/空值语义、
  限制）。
- `input-manifest.json` / `output-manifest.json` — 每个输入的
  `logical_module / source_relative_path / sha256 / bytes / owner / release_id /
  provenance_kind` receipt；输出 manifest 记录 sidecar 哈希与“输入未变”核验。

确定性口径说明：manifest 与 receipt 内嵌输入文件的**绝对路径**（`str(path)`），因此同一输入
**字节**放在不同路径会产生不同的 sidecar 字节；“同输入字节级确定”的成立条件是同一输入路径
（复现：同输入两个输出根，全部输出文件字节一致）。

## 拒绝条件（非零退出，不写完成标记）

source-map/生产者版本未知（6 个相关源码哈希与冻结 catalog 不符，或锚点行漂移）、产物
被篡改（binding hash ≠ 实际 hash）、缺必需 JSON pointer、未声明的 producer 混版
（schema 版本不符）、伪造账户关联、路径越界/UNC/forbidden 根（research_runs、holdouts、
maintenance_artifacts、`.env`、AKF260920、agent 配置目录）、输出根已存在（覆盖既有证据）。
生产者身份绑定 `artifact_schema_version + producer_release_sha256 +
source_manifest_sha256 + relevant_source_hashes`；同 schema 的不同实现字节不自动兼容。

## 测试（仅本节点）

```
python -B -m pytest -p no:cacheprovider \
  -c tests/risk_semantics/pytest.ini tests/risk_semantics \
  --confcutdir=tests/risk_semantics
```

覆盖 H30.1–H30.8：共享 BEST_EV 账户不产生伪单策略回撤、FIXED 一对一与跨 run 拒绝、三类
CVaR 区分、未闭合持仓盯市与终端恒等式按原产物引用、INCOMPLETE 传播与 null 恢复期、七类
拒绝、同输入字节确定与无资格重签发、压力 cohort/空压力原因/保证金 basis/手数口径。
另含 F01 修复反例（NF30-HARD-r1）：逐字段断言**全部** entries——压力行不得含账户 basis 条目
（含被 `g.update(stress)` 覆盖的保证金两字段与从不评估的 dd/recovery 两字段）、BASE 行账户值
与压力行压力值并存时逐条 pointer 回源核对、占位语义（`PLACEHOLDER_SEED_NOT_EVALUATED`，
fixture 保真 reports.py:98-101 占位种子）与复制语义（5 键复制循环 + `max_inventory` 账户 lots
复制来源）区分。fixtures 均为 SYNTHETIC。

## 限制与后续

- producer 身份是 WORKTREE_SNAPSHOT 绑定（冻结的 6 个源码文件 SHA-256），不是 M4/M5 最终
  获审发行字节的声明；producer revision 变化时本工具拒绝运行，须先由 owner 更新映射并
  独立验证，不在运行时猜测。
- 未持久化的账户级日权益 CVaR 与账户级完整轮 CVaR（metrics.py 内计算）只以
  `KNOWN_NOT_EXPOSED` 说明，不补算数值。
- 将 sidecar 强制接入原报告/CLI 须由原 owner 就精确 producer/consumer 范围另行提交 CR；
  本单完成不宣称旧消费者已升级。
