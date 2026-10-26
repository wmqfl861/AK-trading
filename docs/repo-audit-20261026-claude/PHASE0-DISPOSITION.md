**PHASE0-DISPOSITION.md — r3-001 phase 0 作者最终处置，2026-10-26**

状态：**EFFECTIVE**。原 Astra 作者确认 `astra-phase0-plan-001/PHASE0-PLAN.md` 与本文件共同构成有效增补；下列明确修订优先，其余原计划继续有效。Root 立即按此绑定专用 ZCode 开发及独立 ZCode 成果审核，不再进行计划 PASS 轮次。

本文件的 **F1–F6 指 `phase0-plan-advice-002/ADVICE.md` 的意见编号，与外部蓝图的治理 F1–F6 无关**。治理提案仍未获决定。

`ROOT-RECOVERY-NOTE-001.json` 记录原作者调用 native **0**、receiver **1**，后者来自已恢复的 reconnect；最终输出等于最终消息、turn 已完成、该次 current failed tools 为 **0**。结合 Root 提供的输入未变确认，本作者确认恢复稿为本次处置原文；其记录哈希为 `f1855d6bdc55d59a85d30c907f929ed343bf7b8140c7ea670dcd8c9a0058aa61`。保留原退出码及失败准备记录，不重跑作者或评审，不产生产品信用。

| 意见 | 作者决定 | 生效处置 |
| --- | --- | --- |
| F1：过期重建后的计数继承 | **ADOPTED** | 明确禁止把已过期 setup 的确认计数带入新 setup，具体规则及控制见下文。 |
| F2：artifacts 追加范围 | **ADOPTED** | 唯一追加项为 `tests/test_v2_m3b_duplicate_bar.py`。不追加 MARTINGALE 既有文件或 A4 未来测试。 |
| F3：预算与已完成基线 | **ADOPTED** | 直接复用封存基线；完整 M1–M4 每次 **3600s**，聚焦、探针及完整 M3B 每次 **600s**。 |
| F4：CLAUDE 冲突状态 | **PARTIALLY_ADOPTED** | 采纳更新过时清单；拒绝“停止措辞仍待决定”的判断。用户已批准两处修改，`c151d5c` 已完成，不存在第三项待决。 |
| F5：探针与矩阵构造器耦合 | **ADOPTED（记录风险）** | 保留原样九项探针及其依赖，不增加本批改动。未来构造器变更须保留并验证该人口，不能以删除探针处理耦合。 |
| F6：未来 A4 测试登记 | **ADOPTED（无当前改动）** | A4 继续仅为方案，产品 **NOT_IMPLEMENTED**；未来登记按实际归属处理，本次不实施 shadow 或扩展 artifacts 列表。 |

**F1 的精确计数裁定**

原 M3B §7 规定 setup 超期取消，§6.6 要求有效 setup 内先触后穿和连续三次资格确认，§10.11 禁止重复同 bar 多计。原文没有逐字规定跨期计数初值；本作者据这些合同明确裁定：**已取消观察期的确认信用不转入下一观察期**。不把历史实现的继承行为当作已接受的合同要求。

替换原计划 §2 第 3 条中的过期分支：

- 始终从**调用前的 `state.setup`** 判定旧观察期；`now_nano > expires_at_nano` 才属于本条过期，等于 expiry 不提前判过期。
- 门控失败优先返回 `0`。
- 旧 setup 已过期：若本族尚不能建立新的有效 setup，观察分支返回 WAIT、计数 `0`；若已按本族原构造规则建立有效新 setup，首个合格观察计为 `1`，不使用旧计数加一。不能把仍然过期的旧对象当成新 setup。
- BREAKOUT 的真实 `created=True → 1` 保留。
- 未过期同 bar 保持旧计数，未过期的不同合格 bar 按原规则加一。不得根据刚返回 setup 的 `last_bar_id` 判断重复。
- 该到期处理适用于六个入场族；TREND/PULLBACK 的未过期行为保持原样。必要改动限原写集内的观察/重建分支与 helper，不改变结构触发、期限算法、执行态、预算、ADD 或退出。WAIT 仍不进入该计数过程。

在原计划已要求的 expiry 控制中钉住以下结果，不另开验收体系：

| 前态及输入 | 期望 |
| --- | --- |
| 旧 setup 计数为 2，`now == expiry`，同 bar 重送且门控合格 | 计数保持 2，不因重送 OPEN。 |
| 旧 setup 计数为 2，已过期；新 setup 合法 arming → 同 bar 重送 → 新 recross/确认 bar → 重送 → 第三根合格 bar | 计数依次 **1、1、2、2、3**；结构及其他门满足时，仅最后一步允许 OPEN。 |
| 过期后无有效新结构，随后才出现合法 arming | 先 WAIT/0，再从 1 开始。 |
| 新观察门控失败，随后同 bar 恢复合格 | 先清零，同 bar 不补计；后续不同合格 bar 才推进。 |

这些是对原计数规则的明确修订，包含真实 `confirmation_count` 与动作断言；既有清洁三 bar、重启、门控及全部原人口继续保留，不以只测 helper 返回值代替策略行为。

**基线、输入补齐与预算修订**

`baseline-m1-m4-001/SUMMARY.json` 已封存 `891e4de` 完整结果：**raw 1；866 selected、852 passed、14 failed；0 skipped/errors/collection；1683.875s**。原计划“等待基线”已满足，**不重跑基线**。

失败对照严格绑定该 JSON 的全部 `failed_nodeids` 和同目录原始 `stdout.txt`，不能只比较数量。十四项均为 `test_forwarded_binding_keeps_frozen_baseline_value` 的七族×两频节点；既有原因是当前 StrategySpec 代码身份参与真实绑定哈希，却与固定历史 `_L2_BASELINE_BINDING_SHA256` 比较。A2 改变 registry 后实际哈希可能再次改变，但不得刷历史表、删除断言或更改生产绑定算法。**同一节点若出现新的异常或不同断言原因，也不能借用这十四项基线豁免。**

候选 M3B 前基线 **raw 0、212 selected、209 passed、3 skipped、12.609s** 保留为真实记录；主树相同 212 节点全部通过。评审关于“无外部 fixture 依赖”的结论不完整：三个 skip 来自 `tests/test_v2_m3b_matrix.py` 的真实 RB 输入适配测试。原计划“原有八文件”亦更正为既有七个 M3B 文件，新 `duplicate_bar` 为第八文件；完整人口以封存 argv/node 集合为准。

**批准 Root 在实现前提供小型、逐字节、只读测试输入切片**，不复制 1.9GB 完整集合：

- 源根：主树 `data/research_runs/v2/real-input-rb-r2/collection`。
- 精确复制集合：该测试 `REAL_SHARD` 常量指定主 shard 的 `sessions.json`，加源 `collection/shards` **每个实际 shard 目录的 `rules.json`**。不能只取主 shard 的规则。
- 目标为 A2 候选内相同相对路径。实际成员从现有目录和测试常量取得；在候选随附的输入交接记录中保存源绝对路径、目标路径、字节数、SHA-256 和 `LOCAL_ONLY / TEST_INPUT_SLICE` 标记。
- 验证源/副本字节一致；已存在目标仅在字节相同时复用，不覆盖不同内容。复制后只读，不改测试路径、skip 条件、数据记录或任何研究 manifest，也不把切片宣称为完整研究集合。

补齐后，修复及独立审核的完整 M3B 必须执行并通过原 **212 节点**及新增人口，三个真实输入测试不得继续跳过。完整 M1–M4 保留原 866 节点并加入新增人口，比较精确失败节点和原因；外部探针仍必须原样 **9 passed**。

**3600s 完整 / 600s 聚焦及 M3B** 是依据已完成基线落实原预算条款，直接供 owner 与 reviewer 使用，不需额外批准。进程树 **2 GiB RAM**、本批 **1 GiB 临时输出**不变。采用候选本地 `src` 并确认实际模块路径，禁止借 MAIN 导入；普通必要离线复验不套用历史一次性诊断限制。

**其余状态及立即交接**

- A2 仍限原计划十个产品文件；`artifacts.py` 只追加字符串 **`"tests/test_v2_m3b_duplicate_bar.py"`**。当前候选为 `C:/AKWorktrees/aktr-round2-a2-20261026`，从 `891e4de` 建立；采用 Root 已确认的源码、测试、脚本原字节镜像及 diff 0 前态，不做规范化。
- A3 的 README 与实施计划六处替换继续有效。`RULE-CONFLICTS.md` 与当前 CLAUDE.md 已确认路由、当前批次范围两处都已修正；`c151d5c` 不授权治理 F1–F6，AGENTS/PROJECT 规则不变。
- B1 已由 `c5185e3` 完成并推送 **234 条精确记录：171 failed、60 error、3 collection**；不重做分诊，不把它记为产品修复。
- A4 维持原方案和 **NOT_IMPLEMENTED** 状态，不派发额外 shadow 实现、不修改 T8。
- 原 A2 独立冻结、独立硬审、精确字节集成及消费者后继接受要求继续有效。T7-B 冻结树和在途绑定不动；其他模块完成与否不成为此次 A2 派发等待项。

Root 现可补齐只读输入并立即派发专用 ZCode owner；实现、必要验证和冻结后立即独立成果硬审。没有第二轮计划审核或计划 PASS 门。

本轮只读定位曾将恢复记录误定位到作者子目录，首次预检 raw **2** 保留；随后已读取批次根下正确原件，该定位错误与原作者 native 0／receiver 1 分列。所有后续命令继续保留原生退出、stdout、stderr及既有事故要求，特别是 `20261004-rg-no-match-r1`、`20261004-grok-headless-cancel-r1` 和 manifest-recovery-002；仅可选 rg 1 且空输出/错误流可记 NO_MATCH。

本处置没有运行产品、修改产品或授予产品 PASS。`enable_trading=False`、observer=`hold`、broker=`NOT_RUN`、真实 send/cancel=`0/0`；凭据、配置、服务、数据库和客户端边界不变。