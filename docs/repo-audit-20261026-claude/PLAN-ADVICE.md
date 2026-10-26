# ADVICE.md — 对 Astra phase0 增补草案的独立计划意见

- 评审席：独立 ZCode GLM-5.3/max 计划意见席（未参与该计划编写，未参与任何相关产品开发或修复）。
- 评审对象：`C:/AKWorktrees/aktr-round2-20261026/astra-phase0-plan-001/PHASE0-PLAN.md`，SHA-256 `f1855d6b…8aa61`，与 `ROOT-RECOVERY-NOTE-001.json` 记录的作者终稿哈希一致——本意见覆盖的即恢复草案原文，无需重新生成。
- 日期：2026-10-26。性质：**仅意见，无 PASS/FAIL 门**；是否及如何修改完全由原 Astra 作者决定。本席未运行产品测试，未修改任何产品文件。

方法：对照主树 `891e4de` 当前源码与本轮证据（`baseline-m1-m4-001/SUMMARY.json`、`root-reproduction-001/RESULT.json`、`A2-BASELINE-BYTE-SNAPSHOT.json`、`docs/development-20261025-zcode-r3/C05-REVIEW.md`、`docs/repo-audit-20261026-claude/RULE-CONFLICTS.md`）做静态核对。

## 一、核对结论：计划关键主张与代码/证据一致

以下主张逐条核实为准确，处置时可直接引用：

1. **A2 缺陷真实且定位精确。** `registry.py:1314-1315` 在同 bar 重送时原样返回旧 setup，但 `reversion.py:84`、`grid.py:92`、`martingale.py:106` 仍无条件 `state.confirmation_count + 1`；`trend.py:125-133`、`pullback.py:98-106`、`breakout.py:186-191` 已有同 bar 保持逻辑。修复真正决定 OPEN 的 `confirmation_count`（而非只改 `setup.consecutive_confirmations`）是对症的。
2. **helper 计数优先级规则与 BREAKOUT 现行已接受语义逐字对应**（gate 失败→0、created→1、同 bar→保持、其余→+1，见 `breakout.py:184-191`），向 helper 委托不改变既有语义。
3. **代码身份级联主张成立。** `registry.py:186-198` 的 `code_sha256_for` 对家族模块+`registry.py`+`state.py` 取哈希，改 `registry.py` 确实改变全部七族×两频身份；§4“旧身份不自动获得新代码信用”的立场有技术依据。
4. **A4 三处缺口核实为真。** `shadow.py:932` 以 5 个位置参数调用，而 `artifacts.py:1517-1523` 要求 keyword-only 的 `exit_registry_sha256` 与 `exit_binding`（达到即 TypeError）；`experiments.py:603` 的 `_reg` 在其所在函数 `run_shadow_replay_experiment`（def 于 561 行）内未建立（仅在 1128/1306 行其他函数内定义），且位于 586-588 行模型训练之后；`decision/ledger.py:321-336、414-423` 的 checkpoint/restore 均要求并校验 `exit_registry_sha256`。
5. **A4 CLI 提前拒绝的插入点与行为在现有代码形状下成立。** `scripts/research_probability_v2.py` 第 53-56 行（feed/reference 关系检查）与第 57 行（`output = _path(...)`）之间正是计划所述位置；`StageIncompleteError`→原生 3（112-114 行）；拒绝先于 output 赋值，故 `output is None` 不建目录（124 行守卫）。计划“本轮只交付方案、产品 NOT_IMPLEMENTED、不关闭 P0-2/P1-2”的边界诚实且与缺口事实一致。
6. **A3 两文件范围可执行。** README 第 93 行“最新核验（2026-10-25）”段即替换目标；替换文案中的 C05 数字（6,015 执行 / 5,754 passed / 171 failed / 60 errors / 30 skipped / 3 收集失败）与 `C05-REVIEW.md:21-22`、`C05-ACCEPTANCE.json:46` 实测完全一致。r3-001 `IMPLEMENTATION-PLAN.md` 六处旧 Sol 角色文字全部存在（第 9、18、41、42、44、60 行），逐一对应计划表格。
7. **证据数字一致。** 外部探针 3 测试×3 参数化=9 项，Root 复现 raw 1、3 passed/6 failed；主基线 866/852/14（raw 1），14 个失败全为 M4 `forwarded_binding`，与 §3“原 14 项 payoff 历史失败逐节点保留、不刷哈希”条款自洽。A2 候选已镜像主树原始字节（抽查 `scripts/acquire_futures_free.py`：主树=候选=快照哈希一致，均 LF）；M3B 测试用代码内 SYNTHETIC 构造器，不依赖主树未镜像的未跟踪 fixture，候选内可独立跑 M3B 前基线。

## 二、意见（编号，含 severity、证据、建议）

**F1（Medium，A2 语义未钉住：过期重建后的计数继承）。**
证据：`registry.py:1309-1317` 在 `now_nano > expires_at_nano` 时置 `previous=None` 并重建新 setup（cc=0）；`reversion.py:77` 的无结构 WAIT 原样携带 `state.confirmation_count`；计划规则 3 只写“其余合格新观察 → 原计数加 1”，而 BREAKOUT 有 created→1 的显式重置，区间族新 setup 无对应条款。
风险：setup A 计数到 2 后过期，下一根 bar 成为 setup B 的 arming：helper 给 2+1=3（当根被 `consecutive_confirmations<1` 挡下，但计数 3 已提交），再下一根不同 recross bar 即 cc=1、计数≥3 → 新 setup 仅 2 根不同 bar 即 OPEN。trend/pullback 已接受实现同样带此继承，故不必然是缺陷，但当前文本留有解释空间。
建议：作者处置时明确该语义（继承现状，或重建时归零/归一），并把“过期→重建→计数终值”的期望值写进 §3 已列的“expiry 边界”针对性控制。落在既有控制项内即可，不必新增验收门。

**F2（Low，A2 artifacts.py 追加范围有歧义）。**
证据：§2 表末行“仅向 DOWNSTREAM_TEST_FILES 追加上述新测试路径”；当前 `DOWNSTREAM_TEST_FILES`（`artifacts.py:67-106`）M3B 只登记 state 与 causality，`tests/test_v2_m3b_martingale.py` 存在但未登记。
风险：“新测试路径”若读作也含被强化的 martingale 文件，会把一个既有文件新纳入每个未来 lock 的必需测试底线（人口扩张）；若仅指新建 duplicate_bar 文件则最小。
建议：处置时枚举精确追加路径（最可能仅 `tests/test_v2_m3b_duplicate_bar.py`），避免 owner 自行解释。

**F3（Low，A2 预算与基线衔接）。**
证据：Root 基线实测 1683.875s（full M1–M4）> 计划默认每次 600s；计划已含“实测不足→按真实耗时记录后继预算”条款；SUMMARY.json 已封存，§3 第 1 条“等待基线自然结束”已满足。
建议：派工单直接携带衍生预算（本轮工单建议 full 3600s、focused/M3B 600s），避免 owner 把一次必需调用耗在 600s 超时上；基线结果直接复用封存件，14 个失败集合作对照原样保留。

**F4（Medium，A3 的 CLAUDE.md 冲突清单已部分过时）。**
证据：草案之后用户已明确“同意仅同步这两处（推荐）”，Root 已执行（`RULE-CONFLICTS.md:3、30`）；当前 CLAUDE.md 第 6–15 行已是 2026-10-25 ZCode 路由的正确表述，“仍把 10-18 Sol 开发指令称为最新”的冲突已不存在；“完成本批即停止”措辞仍在（现约第 20–21 行），属 F1–F6 未决用户决定。
建议：作者处置时把 §5 该段更新为“首条冲突已由用户批准的两处同步解决，剩余停止措辞留用户决定”，防止后续读者按旧清单再动 CLAUDE.md。A3 的两文件写集本身不受影响（见一、6）。

**F5（Info，A2 探针保留带来的维护耦合）。**
证据：探针经 `sys.path` 从 `tests/test_v2_m3b_matrix.py` 导入构造器（probe 第 31-43 行）；NOT_ADOPTED 删除使其成为长期 9/9 验收人口。
说明：作为独立验证保险可接受；仅提示未来 matrix 构造器重构可能连带探针失败，届时需同修而非删除探针。

**F6（Info，A4 未来实施小注）。**
证据：拟新建 `tests/test_v2_m5_shadow_entry_guard.py` 未列入 `DOWNSTREAM_TEST_FILES`，不进未来 lock 必需集，与 artifacts.py 现注释体系（M5 测试登记属 M5 fixture 工单域）一致。
说明：无需改计划；将来实施工单如需登记再按原归属处理。

## 三、结语

计划的事实基础、写集边界、验收人口与安全边界（enable_trading=False、observer hold、broker NOT_RUN、不读凭据、不动 T7-B011 冻结树）均与代码和证据相符，可进入作者处置。以上 F1–F6 均为建议，不构成开发前置门；无新增验收标准。
