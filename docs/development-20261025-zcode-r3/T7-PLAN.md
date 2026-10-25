**P1-3/T7 执行补充 r1，2026-10-25。** 作者为现有 Astra 规划席位。本补充落实已授权的 r3 P1-3，开发由专用 ZCode owner 承担，产品硬审由未参与开发的独立 ZCode 承担。Root 获取一次计划意见后交本作者处置，随即派工；不要求计划 PASS，也不新增用户确认。

**1. 作者处置与接受证据**

以 `D:/Maintenance/aktr-development-20261025/aux-t7-acceptance-lineage-001/CURRENT-DRIFTS.json` 的快照为起点：原清单 64 pins，当前 12 项漂移；审计 C07 的 11 项漂移和 raw exit 1 保留为历史事实。辅助报告用于定位，不能代替独立产品接受。

下列六个文件复用已核接受链，派发时程序化比对候选实际字节与记录，不重复审核未变化的已接受增量：

| 文件，相对 `src/ak_trading/v2/` | 原接受记录及有效范围 |
| --- | --- |
| `decision/gates.py`、`decision/pipeline.py` | `D:/AKS261014/DAILY/mainline-seam-r3/hard-review-r1-acceptance-followup-002/VERDICT.json`：DAILY 六文件 seam 的精确范围。 |
| `decision/payoffs.py` | 上述 DAILY 前驱，加 `D:/AKS261015/M4/Z2-hard-review-r1/VERDICT.json`：接受到当前字节的一行谓词修复；不扩大为整个 M4。 |
| `decision/replay.py` | 仓库 `docs/development-20261025-zcode-r3/REPLAY-VERDICT.json`、`REPLAY-ACCEPTANCE.json`：已集成 `3336c62` 的两文件窄修复。 |
| `export.py` | 仓库 `docs/nextphase-contract-lock-20260923-r1/routing-acceptance.json` 的 P04/F07 精确源文件接受；不是 release 整体接受。 |
| `factor_specs.py` | 仓库 `docs/parallel-development-20261004-r1/dispatch/aktr-zcode-r1-m2-partial-product-hard-review-20261014-001/evidence/VERDICT.json` 及同目录 `PROBE-BASELINE-REGISTRY.json`：仅 docstring 增量，probe 绑定当前字节。 |

其余六个文件按**当前字节缺少所需生产接受证据**办理，不据此预判代码有缺陷：

- `strategies/{breakout,pullback,registry,trend}.py`；
- `validation/experiments.py`；
- `tests/test_v2_m5_payoff_integration.py`。

M3B r2 的三测试夹具 PASS 不覆盖四个生产文件；旧 forward 的 `RETIRED_INCOMPLETE_NO_VERDICT`、旧 T7 的 `PASS_STATIC_ONLY`/`PREPARE_ONLY`、Git 发布和 CURRENT 夹具的 `GENERATED_UNVALIDATED` 均不升级为接受。

**2. 并行单元 A：四个策略文件的实际合同接受**

四文件可作为一个有用的独立单元：它们通过共享 registry、冻结上下文及策略状态协作，拆成四个 writer 没有收益。依据 `docs/devplan-v2/04-M3B-策略库.md` §2、§4–§7、§9–§11，以及 `docs/strategy-research-blueprint-20261010-r2/implementation-clarifications/m3b-p0-plan-20261014/PLAN.md` 的适用合同；旧计划的计划 PASS 和 Sol 路由文字由当前规则取代。

- **入场与责任：** Root 从包含 `3336c62` 的实际 tracked HEAD 创建独立候选，绑定实际 SID、cwd、基线及逐文件 pins；显式记录本候选对 P0-BRK、P0-M3B/G2a 有关文件的有限接续，保留历史作者身份。既有公共合同、实际退出 pack 和适用 ownership 原件必须可核对，不虚构已完成的锁更正。
- **生产写集：** `src/ak_trading/v2/strategies/breakout.py`、`pullback.py`、`registry.py`、`trend.py`。
- **测试写集：** `tests/test_v2_m3b_registry.py`、`tests/test_v2_m3b_directional.py`、`tests/test_v2_m3b_matrix.py`、`tests/test_v2_m3b_causality.py`。只为实际缺陷补必要回归；其余策略、state、M1/M3C/M4/M5 文件只读。
- **交付：** 先验证当前四源；若没有缺陷，可零生产改动提交精确字节接受。若暴露缺陷，在上述范围按原条款修复。保持真实冻结区间、连续确认、多空镜像、时钟先选管理期限、缺失/过期证据拒绝、既有预算与退出身份不变、未来追加因果性。发现其他模块问题退其 owner。
- **验证与完成：** 执行 P0 计划 §10.2 的原七个单文件命令及完整合并命令；保留 registry、matrix、directional、reversion_grid、martingale、state、causality 全人口。沿已核七文件工作单的 600 秒、2 GiB Job 内存、1 GiB 输出约束，按实际收集节点与结果集合核对，不把历史 209 当本次完成证据。三个历史 real-RB skips 仅在原前置确实缺失时按原理由保留，不给 PIT 信用。冻结后立即独立硬审并做合同反例。

该单元不等待 M5 C1、C05 或完整 T7。其接受仅证明四源在声明合同范围内正确，不授予完整 M3B、历史回测或收益资格。

**3. 并行单元 B：T7 后继夹具、清单及 M5 消费路径**

依据本机原件：

- `data/maintenance_artifacts/v2-ep1-training-path-20260917-r1/decision.md`，E-P1 RULING 6；
- `data/maintenance_artifacts/v2-ep1-t7-form-20260917-r1/decision.md`，§3–§7。

上述材料及外部接受包为 **LOCAL_ONLY / NEEDED_AT_DISPATCH**；Root 提供实际原件，不宣称 fresh clone 已具备它们。

**T7 专用 ZCode owner 的精确写集：**

- `tests/test_v2_m5_payoff_integration.py`
- `tests/test_v2_m5_t7_evidence.py`
- `tests/v2_ep1_t7_support.py`
- `tests/fixtures/v2/ep1_t7/` 下七个既有文件：
  `acceptance-input-manifest.json`、`recipe.json`、`partition-params.json`、`inputs/bars-minute.json`、`inputs/bars-daily.json`、`inputs/sessions.json`、`inputs/rule.json`
- DAILY 独立 profile 如需独立输入，使用新目录 `tests/fixtures/v2/ep1_t7_daily_r3_001/` 下同名七文件；首次写入前核实 ABSENT。其他附件不获得通配写权。

该 owner 不写任何生产源、离线 launcher 或 C05 文件。`experiments.py` 的规范主线字节为只读消费对象，其 T7 消费范围与最终 integration 测试可在**同一次 T7 独立硬审**中补齐接受，避免重复开发和重复冷拟合；不能据此给整个 M5 发 PASS。

若发现 `experiments.py` 的真实产品缺陷，交现任专用 M5 ZCode owner，按原 M5/E-P1 条款在独立主线候选追加精确修复和回归。不得由 T7 夹具作者修改生产逻辑遮错。`D:/AKF261018/M5-CDF-BATCH-SOURCE-025-R16-R4/source` 及 C1 controls 615 保持原冻结；reviewer004 的 502/无阶段产物保持原记录，不复制其未接受产品字节，不在那里建立第二 owner。

实施顺序：

1. **封存前驱。** 候选改动前保存旧 F 的实际 bytes/hash、旧成员和失败记录，历史副本放任务历史目录，不能放进活动 fixture 子树；当前 verifier 会把额外文件判错。保留旧版本，不倒写旧接受状态。
2. **修正验收夹具。** 当前 `test_t7e1_daily_positive_path_structurally_blocked_pinned` 仍断言 DAILY 被阻断。将其迁移为原合同要求的真实 DAILY 正例，并保留“缺必需背景/证据仍拒绝”的负例；不简单删除断言。MINUTE 原正向路径和 30 分钟负面世界保留。
3. **先容量预检，再真实冷链。** 每频率事前固定 recipe、日历、split、成熟尾段、候选选择规则及真实校准分组容量。保持原政策 200 样本/30 日期块和适用更严门槛、原 fixture 下限、真实 purge/embargo；不得用重复日期、重复 barrier、未来数据或手填 VALID 凑数。复用原 TREND/BREAKOUT/PULLBACK × MINUTE/DAILY 六格能力表，不新增研究笛卡尔积。
4. **生成候选 F。** 六条已有接受链、单元 A 的最终接受、M5 消费路径的待审范围逐项对应实际 bytes。只重绑定有来源的变化；其余原 pins 保持，确有新增行为成员按原 §5.3 列入并解释。既不能盲刷新 64 hashes，也不能把成员数锁死为 64 而漏掉实际新文件。原 schema、精确哈希、成员检查和重生成身份检查保持。
5. **候选锁与最终锁沿原 M1 责任执行。** 优先复用现有合法生成路径；若缺 T7 required_tests/F 引用，仅由 M1 的专用 ZCode owner处理 `src/ak_trading/v2/artifacts.py` 及原分配的 `tests/test_v2_m1_artifacts.py`、`tests/test_v2_m1_payoff_path_contracts.py`，候选/最终锁由 M1 在原目录分配未占用 revision。T7 owner 不代写公共 schema、版本或锁。按原 §5.4 的 R0→L0→R1→L1→R2 顺序，候选及发布接受路径使用真实 verifier，不能靠 `_bypass_lock` 或版本 monkeypatch。

A、B 可并行开发；B 的最终冻结消费 A 已接受的精确字节。M1 可并行准备必要引用，仅在实际需要时承担上述既有锁侧任务。源码/F 改变后，不能沿用先前组合运行的接受信用。

**4. 必要验证与接受边界**

T7 owner 和独立 reviewer 均在各自新进程、新输出目录执行：

```text
python scripts/test_offline.py -q tests/test_v2_m5_t7_evidence.py::test_t7e1_acceptance_input_manifest_matches_tree

python -B -X utf8 scripts/test_offline.py -q tests/test_v2_m5_payoff_integration.py tests/test_v2_m5_t7_evidence.py --basetemp <本次独占的新绝对目录>
```

同时保留 E-P1 RULING 6 所列 **M1 五文件、M4 九文件、M5 七文件原完整命令**及实际目标锁的 required_tests；从原记录取得完整 argv，不以两文件命令替代，不删参数化实例或用筛选绿项代替人口。验收至少包括：

- MINUTE、DAILY 各一条自然 OPEN、实际非零 Fill、闭合成熟、真实 producer observation；真实内层 fit、分区、有效模型/校准原件和正常登记一次可追溯。
- T01 全 WAIT 与“有自然 OPEN、普通 empty-payoff gate 拒绝”两分支分开证明；空模型、缺接口、错/缺 binding、错 hash、未知错误传播及 EP1-T03–T13 适用反例保留。
- 从 source 原 bytes 重建并重算字段、ID、费用、账本守恒和释放事实；原件缺失、篡改、成员增加/删除及身份变化被检测。当前 integration 中 canonical reconciliation/最终 checkpoint 的断言不得退回虚构别名。
- reviewer 自有冷链与反例，最终锁下另做真实复验；当前 T8 对 `_reg` 的预期 NameError 只记录未修 shadow 边界，不给予 shadow 接受。

预算直接继承已读 T7 原裁决 §7.4：owner/reviewer 冷链各为每 profile 最多 **4 小时**、两 profile 共 **8 小时**；原件快检每 profile **15 分钟**；最终锁复验仅在冻结 fit 复用条件真实成立时用 **15 分钟**，否则用冷链预算。这些是该 T7 谱系的原预算，不是性能实测，也不借用 M5 C1/full100 的预算。超时保留失败，不降门槛或缩人口。

原单项 raw 0 只能证明相应输入一致性结果。新基线提升须有对应版本的原验证和独立产品接受；完整 T7 还须完成原组合义务与最终锁复验。**现存业务失败、full M4 的 14 个基线失败及 C05/DP03/DP08 状态分别保留。** 若某失败位于完整 T7 的必需命令内，完整 T7 仍未完成；它不抹去可独立成立的四策略或其他窄范围接受，也不能被“历史失败”标签豁免。

**5. 立即交接与执行纪律**

Root 在本作者处置一次独立计划意见后，立即绑定 A、B 的实际 owner/候选/精确写集并并行派工。选定目标的隔离测试不等待 C05；不得使用 C05 正在变化的 staging 或未接受 launcher。M5/M1 问题只交对应唯一 owner，硬审 FAIL 回实际开发 owner，修复后冻结新版本再独立复审。

普通可重复离线验证按项目正常授权执行，不增加逐次一次性审批循环；历史已消费特殊授权原样保留。工单携带：

- `docs/maintenance/20261004-rg-no-match-r1/INCIDENT.md`
- `docs/maintenance/20261004-grok-headless-cancel-r1/INCIDENT.md`，含 manifest-recovery-002 纪律。

分别保存原生 stdout、stderr、exit；仅可选 rg 的 raw 1 且空输出/空 stderr 可记 NO_MATCH。输入先查存在，未来输出不作为必需输入读取，进程结束后才封存日志。保持 `enable_trading=False`、observer=`hold`、broker=`NOT_RUN`、真实 send/cancel=`0`。

本轮仅完成静态计划，没有文件写入、产品导入、测试或运行。只读处理中一处摘录 raw 1 和两次可选路径查找 raw 2 均保留为工具失败，未改记 NO_MATCH；上述关键合同与接受记录已定向读取，未定位的接受链仍按缺口处理。本补充不签产品 PASS。