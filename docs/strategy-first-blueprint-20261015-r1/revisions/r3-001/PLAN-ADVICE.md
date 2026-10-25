# r3-001 独立计划意见

**性质**：仅咨询性计划意见（advisory），不构成计划 PASS 门，更不是产品硬审核；无任何产品 PASS/FAIL。评审员未运行任何命令、测试或哈希核验（hash/tests 复跑一律 **NOT_RUN**），未读取凭据、`.env`、数据库或网络。比对基础：WORKORDER、r3 三文件、VERIFICATION、AGENTS/PROJECT-MODEL-ROLES/PROJECT-WORKFLOW-RULES，以及 r3 点名的相对输入（r2 蓝图/实施计划/修复顺序/处置、ordinary-shadow CLARIFICATION 与 SOURCE-STATUS、GITHUB-SNAPSHOT-20261024、研究蓝图及其存在性确认）。

**总体判断**：r3 三文件与 VERIFICATION 事实、现行角色规则和原 SH 合同链高度一致，未发现虚构事实、越权授权或验收降级。以下为逐判据核对与 6 条改进建议。

## 逐判据核对（WORKORDER 七点）

1. **VERIFICATION 事实基础**：符合。11 成立/11 部分成立的计数我逐行核对 REPAIR-ORDER §2 全 22 行标签与 VERIFICATION 一致；C05 如实记为 raw exit 2/174 收集错误/0 测试执行（级联而非 174 独立缺陷）、C07 记 11 条 code_drift、C06 P0-2/P1-2 真复现、外部 M5/SHCP7 边界与 377 冻结表述均与 VERIFICATION 原文一致。
2. **策略优先与里程碑**：符合。7 个里程碑各有交付物/进入条件/必要验证/完成证据/责任角色；MINUTE/DAILY 独立交付、双频需两份实际报告；§5 明确“产品 PASS 也不证明策略有效”。
3. **P0-1 与治理提案**：符合。P0-1 由用户亲办、与离线工作并行、不阻回测；全文无凭据读取或防火墙操作；G-1～G-3 与模型/精度/存储大改全部列为待用户提案（REPAIR-ORDER §4）。
4. **22 项映射与冻结纪律**：符合。P0-2 replay 窄修复不附 R19–R21 总门但保留原模块计划方确认；shadow 沿 B0–B8/R19→R20→R21——我对照 CLARIFICATION §2/§4 逐条核验（B0–B8 语义、178 人口含 fresh10/join10、错误四分类、run_shadow 18 位置参数+activation_request）均忠实；377 冻结保留、P1-3 明确不盲刷 hash。
5. **合同继承与阶段区分**：符合。六品种/七族/14 spec/八期限/四政策/六场景/252/60/60/48 单元、holdout 与 `enable_trading=False` 边界以引用精确继承；SCENARIO/SYNTHETIC 不升格；§4 四行表区分回放、开始记录、完整 SHADOW_ACCEPTED（20 日/99.5%/P99≤5s/UTC≤100ms）与真实订单 AGENTS §4 门。
6. **现行角色**：符合。Astra 计划、Sol `gpt-6.1-sol/max/fork_turns=none` 实施、独立 ZCode 意见/硬审；计划不要求 PASS、不反复送审；本批不改规则与 CURRENT/README。
7. **GitHub 可读性与 actor**：基本符合。相对链接结构一致、LOCAL_ONLY 表只给文字定位；r2 SOURCE-STATUS 旧 actor（`/root/m5_sol_implementation_prepare`）与现任（`/root/sol_m5_existing_recovery_20261023`）的漂移已由 §6 表显式纠正且不改原件；M4/R-SHADOW 如实记“待绑定”，无虚构身份。残留核证限制见 F06。

## 发现与建议

**F01（中）里程碑 4/5 进入条件未显式点名 P1-3 清单闭合。**
位置：`IMPLEMENTATION-PLAN.md` §3 里程碑 4 行（“该频率所消费输入/代码和原实际运行 admission 齐备”）与里程碑 5 行；分流规则只写在 §3 末段及 `REPAIR-ORDER.md` P1-3 行。
风险：M5 回测消费的 `validation/experiments.py` 等正处 T7 64 pins 中 11 项 code_drift 状态，清单漂移未闭合时“代码已接受”存在解释空间——实现者可能把漂移字节当“已接受”，或反向把里程碑 4 误当成 C05/P1-3 总门。
建议：在里程碑 4/5 进入条件中显式写明“所消费代码对应的接受清单（T7/DP03/DP08）已按 REPAIR-ORDER §3 闭合或经原规则接受新版本”。这是措辞显式化，不是新验收门。

**F02（中低）§4.6 预算字面未区分 M5 谱系。**
位置：`IMPLEMENTATION-PLAN.md` §4 第 6 条（“M5 full100 的 3600s、2 GiB、1 GiB”）；该数字继承自 `r2-001/DISPOSITION.md` 非阻断 1（HANDOFF-002 snapshot 谱系）。
风险：据项目既有硬审记录（本评审 NOT_RUN，仅作提示），现任 M5 owner 所属 SOURCE-025 恢复谱系的 full100 墙钟死线为约 14400s、内存 2 GiB；两条谱系预算不同，按文档字面取值会错绑 4 倍时限。
建议：§4.6 补明“snapshot/HANDOFF-002 谱系与 SOURCE-025 恢复谱系的 full100 预算各自以原权威记录程序化绑定，不得互串”，与既有“数字/哈希严禁手抄”教训一致（该条已有“从原权威记录绑定”大方向，只需把谱系区分写显式）。

**F03（中低）关键路径首步的“原计划方可恢复性核实→显式转交”未列为具名子步骤。**
位置：`REPAIR-ORDER.md` §1 第 1 条；`IMPLEMENTATION-PLAN.md` §1 表 M4/R-SHADOW/SH 公共合同三行、§2 末段；依据 VERIFICATION P0-2 衔接表（原作者不在 live 列表，Root 先核实、不可恢复才显式转交 Astra/max）。
风险：“本批只写排程，不派工”之下，若无人认领这一核实动作，里程碑 2/5 会在“待绑定”状态静默悬空而报告仍显示“已规划”。
建议：在 §1.1 首条补半句具名第一子步骤（Root 核实原 RESEARCH r2 计划方会话可恢复性并记录结论，不可恢复时按 VERIFICATION 衔接表显式转交），并与 SOURCE-STATUS §1 已登记的 `ROOT_BIND_ACTUAL_CURRENT_ACTOR_BEFORE_DISPATCH` 对接。

**F04（低）同批推送将在 P1-10 未决期间向 public 仓库新增基础设施细节。**
位置：`BLUEPRINT.md` §5（“Root 将 VERIFICATION 纳入同批提交并推送”）与 `REPAIR-ORDER.md` P1-10 行。
风险：推送本身已获用户授权，不构成新门；但 VERIFICATION 含端口、计划任务名、防火墙规则 GUID、内部路径等（无秘密值），在 public/private 决定未做前持续追加部署细节，与 VERIFICATION 自身对公开部署信息的风险判断方向相反。
建议：Root 执行该已授权推送时向用户作一句知情提示（或顺势先做 P1-10 决定）；这是知情提示，不是新增审批环节。

**F05（低，措辞）基线表述即将过时。**
位置：`BLUEPRINT.md` §1 表首行“相对 `b98ef21` 仅新增 REPORT”。同批加入 VERIFICATION.md 与 r3 三文件后，首次克隆读者易混淆核验快照与本批新增物。
建议：改为“核验时点……仅新增 REPORT（本批另增 VERIFICATION 与本三件）”，与 VERIFICATION 自述“本轮仓库内新增交付为本文件”对齐。

**F06（低，审查限制与留痕建议）git-tracked 断言 NOT_RUN。**
位置：`IMPLEMENTATION-PLAN.md` §7 已声明完成 Markdown 链接检查。
说明：本评审无 shell 权限，未验证各链接目标的 Git 跟踪状态；VERIFICATION C12 已证既有跟踪文档中有 66 处链接指向未跟踪文件，该风险在本仓库真实存在（本地存在性我已抽验 r2-001/REPAIR-ORDER.md 与研究实施计划，均在）。
建议：Root 同批提交前用 `git ls-files` 对 r3 三文件全部相对链接目标做程序化断言并留痕，把 §7 的声明升级为可复核证据。

## 结语

未发现需要撤回重写的问题；以上建议均为增补与显式化，处置权归 r3 作者（ADOPTED/PARTIALLY_ADOPTED/NOT_ADOPTED 均可），不要求重新送审，也不构成任何开发或产品验收门槛。
