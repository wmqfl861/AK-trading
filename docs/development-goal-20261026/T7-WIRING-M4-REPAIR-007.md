AUTHOR-DISPOSITION · T7 接线验收与 M4 集成修复

**状态：EFFECTIVE。** 本文澄清既有 MEMORY003、PERFORMANCE002、EXECUTION001 和 author006 的实施、验收顺序，不增加计划审核轮次。当前接线005的 CLI0、冻结输入未变、实际测试和探针结果全部保留；其现有“无条件 PASS”尚不能解除下列契约及验证缺口，Root 暂不据此接受或合入接线。

1. **受控启动器验证必须补齐，直接 pytest 结果保留原身份。**

   wiring005 的 WORKORDER 明确要求 `scripts/test_offline.py`，实际 VERDICT 记录的是 `python -B -X utf8 -m pytest`。现有96P、183P、35P是有效的直接 pytest 结果，不能改记为受控启动器结果。启动器承担参数约束、受控子进程、导入域隔离及真实结果汇总；仅凭测试数量相同不足以证明履行了该执行契约。

   **本次选择补跑指定受控命令，不以静态“等价证明”替代，也不再为此建立证明或计划评审轮次。** 在下述必要修复完成、新冻结形成后，独立 reviewer 按原完整文件集合执行：

   ```text
   python -B -X utf8 scripts/test_offline.py -q <原 M2 六文件>
   python -B -X utf8 scripts/test_offline.py -q <原 M3A 六文件>
   python -B -X utf8 scripts/test_offline.py -q tests/test_v2_m5_experiments.py
   ```

   六文件路径从既有封存 argv 逐项取得，不用 `-k`、排除项或相同数量的其他节点替代。原96、183和35个节点全部保留，新增回归另计；每次绑定实际候选 `src`、模块来源和独立 basetemp，保存父子进程 argv、原始退出、节点、失败、跳过及两路输出。开发者必要自测和独立复核均使用这一入口。既有3600秒/套件、2GiB子树限制不变；620.9秒的 M5套件不能误套为600秒诊断超限。

2. **W1：采纳发现，不采纳“不阻断”的处置。原接线 owner 修复两文件内的确定语义。**

   MEMORY003 §3 明文要求 `fs(name)` 保留**首次键顺序、最后值**。冻结 `experiments.py:923` 起的实现先收集 ID，再选择整个 store 的 `found[0]`，不满足该要求；改成整个 store 的最后项同样不正确。

   最小修复是在该 partition 的实际请求顺序中，用每条请求 metadata 的真实 `features_ref` 更新 ID→句柄映射；重复赋值保留首次键位置、替换为最后请求所引用的准确值，再构造有序视图。不得构造全量 snapshot 字典、物化完整来源元组、修改已接受的六文件 API，或用 ID/哈希相等代替值及编码语义。

   在 `tests/test_v2_m5_experiments.py` 增加小型回归：顺序 `A₁、B、A₂` 应返回键顺序 `A、B`，值为 `A₂、B`；全 store 中 partition 外的同 ID 记录不得左右选择；等值异编码记录仍选择原最后表示。复用既有同 ID 异值及连接错误语义，不制造或假设真实 SHA-256 碰撞。自然生成路径通常唯一，不能取消明确写入的字典语义。

3. **接线单元接受还须补足实际消费者生命周期；完整 T7 在后继验收，二者不能互相冒充。**

   独立接受接线两文件前，除上述受控三组和 W1外，必须补齐 MEMORY003 §3 的真实内层消费边界验证：

   - 原 owner 在现有 `test_v2_m5_experiments.py` 内增加有界回归，正常调用 `_fit_training_payoffs`，由其实际 inner split/config 调用真实 `_fit_fold`，到达有真实模型、非空记录及实际 capture/payoff 消费的分支。可用只记录、不替换结果的调用包装观察先后顺序。
   - 验证真实消费者读取时 store仍可用，`InnerFitCapture` 人口真实；成功及适用异常退出后释放正确，已提取的 models/reference 等合法消费者仍有效，关闭视图不返回假空集合。不能用无 inner split、无模型的提前返回、空 frozen fold或替换 payoff 消费者来充数。
   - 这是生命周期验证，不要求在小工程 fixture 中伪造统计资格；若路径合法返回覆盖不足，应如实报告。自然 OPEN、非零 Fill、maturity及完整政策人口的正向证据仍必须由后继 T7交付，不能从本回归推导。

   沿用既有小型真实 fixture及正常离线验证额度，保留原模型、阈值和分割规则；不得把已失败的43分钟/240秒尝试改名后原样重跑。先确认 fixture能到达所需分支；若仍不能在既有额度内完成，报告确切未覆盖分支与实际成本，接线接受继续未完成，不能自行把它移入 NOT_RUN后签完整单元接受。本裁定不增加运行预算或重训练时段。

   **复用已有证据的边界：** W4 的真实434 snapshots/2136 requests旧侧基准、W5的非空42文件输出，以及 W2/W6的有效证据全部保留。W1改变 `_fit_fold` 后，新侧等价必须针对修复字节验证；已封存的完整旧侧预期可直接复用，不必重新拟合旧侧。未改流式实现及已接受内存单元的证据，在 reviewer确认相应输入、依赖和执行路径确实未变后复用；只复核受本次改动影响的部分。它们不能替代此前明确未运行的真实内层消费者检查。

   上述缺口关闭并独立硬审无条件接受后，Root可接受**接线两文件单元**并解除 B 的接线依赖。这不宣告原 M1/M4/M5集成群、121个T7节点或目标锁验收完成。

4. **M4：新增一个测试文件的有限归属，修测试对已接受私有表示的依赖。**

   需要先纠正节点名称。已读取的 reviewer 原始复现 `work/probes/rv5-m4-t7cpu.stdout.txt` 实际失败是：

   - `test_daily_source_preflight_rejects_first_conflict_before_any_provider[True]`
   - `test_daily_source_preflight_consumes_equal_anchor_once_in_snapshot_and_context`

   两处分别在 `test_v2_m4_causality.py:598、615` 对 `_PrefixState` 使用 `state[0]` 而触发 TypeError。用户提到的 `test_daily_source_preflight_covers_three_snapshots_at_their_own_boundaries` 未出现在这份失败清单中，源码也没有该索引；将它保留为必要边界控制，不把它改记为已复现失败。

   **明确扩展原 M4 owner 本轮写集：仅增加 `tests/test_v2_m4_causality.py`，修改上述两处预热存在性断言。** 使用已接受表示的 `state.prefix` 保留原 `len(...) > 16` 条件；不增加兼容旧 tuple 的宽松分支，不删除预热断言。历史 `test_v2_m4_payoffs.py` 写集本轮无需再次改动，`factors.py`及所有生产文件只读。

   必须保留的语义判据是：

   - cold/warm冲突均在新增 provider调用之前以原 `conflicting_duplicate_bar` 拒绝，已发生的合法 warm publication保持；
   - 相同 anchor只消费一次，仍是592条可见 bars，snapshot来源、值、质量及 context/request规范字节与基准完全相等；
   - 三次 snapshot各用自身时间边界：原调用顺序、三次规范化、可见性、`end_nano/available_at_nano <= decision`及对象归属断言全部不变。

   原 owner先通过受控入口验证上述四个节点——冲突测试的两种参数、相同 anchor、三边界控制——随后执行完整 `tests/test_v2_m4_causality.py`及原 M4九文件群；完整九文件运行覆盖前述文件时可合并安排，避免无意义重复。目标是原259节点均保持有效，不更新历史绑定哈希、不改 chronology、不删/跳过节点。独立 ZCode对该测试修复重复必要受控检查后接受。若仍出现真正语义失败，保留并按具体原因处理，不能归入“都是缓存表示变化”。

5. **集成顺序及立即交接。**

   接线修复继续由原 ZCode owner `sess_9b7570de-440a-4f1b-92e7-df957c15aa77`，原 cwd `C:\AKWorktrees\aktr-goal-t7cpu-20261026`、原固定 store续接；唯一产品写集仍是 `validation/experiments.py`和 `tests/test_v2_m5_experiments.py`。005已终止后，保留 owner003冻结包和 review005原文，在新修订中修复、验证、冻结，再交原独立 reviewer `sess_7229454f-0586-4611-a73b-1b9a4ee0ce23`按其原 cwd/store完成复审。Astra不代签产品 verdict。

   M4可同时交回 `sess_c7ffc367-5b7a-466a-b779-8ed1e0424829`，原 cwd `C:\AKWorktrees\aktr-r3-t7-m4-baseline-20261025`、store `D:\Maintenance\aktr-development-20261025\t7-m4-baseline-owner-001\session.sqlite`，在独立候选只完成上述测试适配，由另一独立 ZCode审核。不得写接线候选或 B活动文件。

   B018的11项监督自测保留为自测证据；author006规定的独立检查不变。B support/F工作仍等待接线实际接受。接线接受后可开始 support合流；M4测试修复的接受字节须在 B最终 F前纳入。author006的性能依赖仍为**10路径并集**，M4这一测试修复作为单独接受的集成修复交付，不扩大 CPU/内存 owner写集。

   原 M1/M4/M5完整集成群按原文件/节点清单在合法合流候选完成受控验证；确已完成且消费字节相同的独立结果可以复用，owner自测或旧直接 pytest不能改记为独立受控接受。P02缺失输入已有准确补交，保留旧失败并使用真实输入验证；既有6个明确设计跳过保持原原因，不能新增跳过。依赖新锁的 target-lock及完整 T7冷链继续在相应阶段执行：**F → R0 → M1 L0 → R1 → L1 → R2**。

   3182日世界、N3150/19窗口、200/30及34/10/1 floors、完整121节点、自然 OPEN/Fill/maturity、4小时/profile、2GiB子树和C10GiB累计输出均不变。本轮不启动完整冷链、不重开资源或一次性授权；Root立即并行续派上述两个原开发 owner，完成各自必要验证后直接独立硬审，无需新的计划 PASS。

本轮仅静态读取，无产品执行或文件修改。一次章节选择器因标题格式不匹配原始退出1，随后直接读取完成所需条款；失败记录保留，不冒称零失败工具。相关候选在图外，已做覆盖检查并使用实际源码与原始日志回退。AGENTS §7/8、20261004 rg/Grok及 manifest-recovery-002、20261016路径/JSON/关闭流规则继续适用；`enable_trading=False`、`observer=hold`、`broker=NOT_RUN`、真实 send/cancel=`0/0`。