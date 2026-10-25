# C05：离线测试导入域隔离

日期：2026-10-25。有效版本：2026-10-25 作者处置版（EFFECTIVE）。独立 ZCode 的一次计划意见已由 Astra 作者[逐项处置](C05-DISPOSITION.md)，本计划立即供 ZCode 开发使用，不设计划 PASS 门槛。交付目标是可独立验收的导入隔离及真实总体结果，不宣称全部历史基线已修复。

## 1. 授权与已知边界

依本目录 `DISPATCH.json`（当前 LOCAL_ONLY 派工记录），Astra/max 会话 `01a171b2-3f04-7413-85f6-fb5423ee127d` 仅承接 C05 测试隔离规划；原模块规划者不可恢复，不冒称原身份或接管 DP08 release。实际开发由 Root 绑定专用 ZCode owner/session/store/cwd，独立 ZCode GLM-5.3/max 硬审；历史 Sol/Claude 开发工单以当前[角色](../../PROJECT-MODEL-ROLES.md)和[流程](../../PROJECT-WORKFLOW-RULES.md)为准。

继承 [r3 实施计划](../strategy-first-blueprint-20261015-r1/revisions/r3-001/IMPLEMENTATION-PLAN.md)及[修复顺序 P1-1/P1-3、C05](../strategy-first-blueprint-20261015-r1/revisions/r3-001/REPAIR-ORDER.md)。[VERIFICATION](../repo-audit-20261025-claude/VERIFICATION.md) 已确认原完整入口 raw2、174 collection errors、0 executed；不重跑旧诊断。这包含历史 pin 漂移与导入污染级联，不能解释为 174 个独立业务缺陷。C07/T7 的 raw1、11 drifts 是独立问题，不当作 DP03 漂移或本修复验收。

DP03 bootstrap 清除全体 `ak_trading` 模块并前置不完整 staging；DP04 同样替换模块域；DP08 组装另一套历史包并强制 pin。普通业务测试不能与这些收集期导入共享解释器。解决边界为测试启动器的进程隔离，保留原测试、原 pin 和失败语义，不建设新的测试平台。

## 2. collector 来源结论及尚未闭合的基线

以下是本机 **LOCAL_ONLY、派工时需随候选提供**的既有记录，未跟踪材料不提供伪 GitHub 链接：

| 记录 | 确定事实 |
| --- | --- |
| `docs/datapipeline-evidence-20260923-r1/DP03/r1/file-manifest.json` | collector 原 pin 为 `82e5615b…` |
| `docs/datapipeline-evidence-20260923-r1/DP-X08FIX/r1/fix-manifest.json` | 合法修复链从前值到 `f5cf3d0e…`；DP08 当前 bootstrap 正在消费这个 overlay |
| `docs/datapipeline-evidence-20260923-r1/DP03/r2/file-manifest.json` | F-HARD-01 后继把 collector 从 `f5cf3d0e…` 改为 `64387dda…`；同时变更 `synthetic_doubles.py` 和 `test_collector.py`，其他声明文件不变 |
| `docs/datapipeline-review-20260923-r1/DP-HARD-r2/DP-HARD-r2-verdict.json` 及同目录 `DP-HARD-r2-manifest.json` | 独立硬审历史 PASS 明确绑定三份 r2 字节；当前 collector SHA-256 与 `reviewed_bytes` 精确相同，verdict 自身也匹配该包 manifest pin。`carry_forward` 明确把三个 DP03 r2 文件的 release 重钉交给 release owner |

因此不能称当前 collector 为“未接受实现”，也不能称 DP08 的旧 pin 已自动失效：DP03 接受的是源码修复，DP08 r2 清单仍描述旧消费集合。**本计划采用已接受的 tracked collector 基线，保留 DP08 的真实 mismatch，不修改历史 manifest 或 bootstrap pin。** 合法后续是 release owner 基于上述三文件接受证据发布新消费清单、验证集成并独立硬审；不得刷新成任意当前 hash、回滚 collector 到旧缺陷版本，或将 DP-HARD-r2 的资源阶段建议当成本轮操作授权。

DP03 所消费 DP01 包自身的 hash/缺文件失败也保持原样；本次没有证明其所有漂移已获接受。完整 C05 关闭仍需对应 owner 解决这些具体依赖及全量验证；导入隔离可以先完成，不等待这些 pin 问题或 M4 修复。

## 3. 候选、写集与 LOCAL_ONLY 复制清单

Root 已从 tracked `0cc305454d06643e93b14e426d04a6a755094d84` 建立 C05 独立 worktree，保留 canonical 377 冻结；按派工记录核实旧实现者停写并显式交接后启动 ZCode owner。仅允许改：

| 路径 | 变更 |
| --- | --- |
| `scripts/test_offline.py` | 在既有 guard 之内按导入域启动独立 pytest 子进程，完整路由原选择并汇总原生结果 |
| `tests/test_offline_launcher.py` | 导入隔离、选择/计数、顺序置换、失败传播及既有安全入口的回归 |

不改业务源码、`offline_guard.py`、DP03/DP04/DP08 bootstrap、历史测试与 pin、release verifier、旧证据或其他 owner 文件。运行日志/临时测试材料写 Root 为本候选分配的新运行目录；不是额外产品写集。

未补本地材料的新 checkout 已有 `tests/datapipeline` 的九个测试和 DP03/DP04/DP06/DP08 fixtures，但没有其引用的历史交付包。本机以下 LOCAL_ONLY 材料须保持原相对路径、原字节，只读复制，不能把它们平铺覆盖 `src/` 或将旧数据冒充新交付：

1. **固定有限集合 A**：读取 `docs/datapipeline-evidence-20260923-r1/DP08/r2/release-manifest.json` 的 `files[*].path`（原记录 117 项），仅取前缀为 `docs/devplan-datapipeline-20260923-r1/` 或 `docs/datapipeline-evidence-20260923-r1/` 的 **69 个精确路径**。本次已查这些路径均在本机存在；这份 manifest 的列举就是复制 allowlist，不递归复制目录。该集合包含 DP01 的五份包源文件/示例配置、DP02 存储/迁移及 `pinned_acquisition.py`、DP05/DP07 源码与 fixtures，以及 `test_release.py` 逐项复制时必需的历史交付文件。其余 48 项沿 tracked 基线，不能以历史副本覆盖。
2. **固定补充集合 B**（下表每个单元格中的名称逐项展开；不含目录通配符）：

| 相对目录 | 精确文件名 |
| --- | --- |
| `docs/devplan-datapipeline-20260923-r1/DP00/r1/` | `baseline-manifest.json` |
| `docs/devplan-datapipeline-20260923-r1/DP01/r1/` | `dp01-freeze-manifest.json` |
| `docs/devplan-datapipeline-20260923-r1/DP02/r1/` | `dp02-freeze-manifest.json`、`pytest-run-r1.log` |
| `docs/devplan-datapipeline-20260923-r1/DP05/r1/` | `dp05-freeze-manifest.json` |
| `docs/devplan-datapipeline-20260923-r1/DP07/r1/` | `dp07-freeze-manifest.json` |
| `docs/datapipeline-evidence-20260923-r1/DP03/r1/` | `file-manifest.json` |
| `docs/datapipeline-evidence-20260923-r1/DP03/r2/` | `file-manifest.json` |
| `docs/datapipeline-evidence-20260923-r1/DP04/r1/` | `dp04-delivery-manifest.json`、`pytest-run-r1.log` |
| `docs/datapipeline-evidence-20260923-r1/DP06/r1/` | `file-hashes.json` |
| `docs/datapipeline-evidence-20260923-r1/DP-X08FIX/r1/` | `fix-manifest.json` |
| `docs/datapipeline-evidence-20260923-r1/DP08/r1/` | `file-manifest.json`、`pytest-run-r1.log` |
| `docs/datapipeline-evidence-20260923-r1/DP08/r2/` | `file-manifest.json`、`release-manifest.json` |
| `docs/datapipeline-review-20260923-r1/DP-HARD-r2/` | `DP-HARD-r2-verdict.json`、`DP-HARD-r2-manifest.json` |

Root 程序化展开 A∪B，记录源/目标/实际 SHA-256 及与原 pin 是否一致，拒绝越界/重复冲突；目标已存在时仅在源/目标字节一致后复用，否则记录冲突并拒绝覆盖，尤其不能覆盖 tracked 文件。不执行历史 build/probe 脚本，不为复制而改 pin。缺失或不匹配必须显式列明，不能标跳过或用空 stub 代替。DP02 helper 还会校验 tracked acquisition 的八文件旧 pin；保留该校验，不复制别版 acquisition。其他全库测试若缺 LOCAL_ONLY 输入，逐项记录为独立输入阻塞，不扩大成本轮任意证据目录复制。fresh clone 没有 A/B 时不能宣称可完整运行这些历史候选测试；隔离启动器的纯合成回归不依赖 A/B。

据 Root 本次交接，C05 worktree 已建立，A∪B 的 87 项输入已复制，`C05-LOCAL-INPUTS.json` 记录哈希、零历史材料不匹配及上述已有目标保护。该结果只证明这批复制材料一致，不消除 §2 中 tracked collector 与 DP08 旧消费 pin 的差异。沿用已准备材料，不重复复制；`C05-HANDOFF.json` 是现有准备记录，由 Root 绑定本有效计划及实际 ZCode 会话后立即激活，无需额外审批。

## 4. 最小隔离实现

1. 普通测试使用一个受保护的 pytest 子进程；`tests/datapipeline/` 下选中的每个测试文件分别使用新的受保护子进程，顺序运行，避免共享 `.tmp/datapipeline/DP*/staging` 写入。当前九文件为 `test_boundaries.py`、`test_calendar.py`、`test_collector.py`、`test_end_to_end.py`、`test_monitoring.py`、`test_release.py`、`test_reporting.py`、`test_scheduler.py`、`test_task_installation.py`。文件级隔离使 DP03、DP04、DP06、DP08 不相互替换模块；普通域不导入历史 bootstrap。未来此目录的新测试也按同一确定规则纳入，不能因固定列表漏收集。
2. 在 pytest 收集导入前完成路径归属判定。无路径默认总体及目录参数采用静态文件级枚举，按已固定 `-c pyproject.toml` 的 `testpaths/python_files/norecursedirs` 展开；显式文件及 `::node` 后缀保留原选择语义。复用 pytest 自身参数解析区分位置参数与选项值，保留现有参数保护，不在父进程导入测试或执行其 conftest；允许启动器内的轻量 collection 插件，不用清空/恢复 `sys.modules` 冒充隔离。支持 `-k`、`-m`、参数化 node、`--collect-only/--co` 和现有报告选项。每个原选择只落一个域；枚举与真实收集的分歧必须显式记录为该域原生结果（含合法空选择 raw5）或“未解析总体”，不能静默漏项或以未解析冒充完整通过。分区路由不新增 `skip/xfail/deselect` 规避失败。
3. 全部子进程沿用现有 `sitecustomize → offline_guard.install → require_installed → OfflinePlugin` 启动链与参数保护、凭据过滤、无网络/实库限制；保留既有九项危险节点排除及其真实 deselected 计数，不增加排除。父环境不修改、不输出。`--basetemp` 仍只允许一次，调用者提供的请求路径若已存在仍拒绝；接受的新请求路径保留，每域使用 `<请求路径>/<确定的域名>`，未指定时使用 `<临时运行根>/pytest/<确定的域名>`。传给 pytest 的每域目录在启动前必须不存在，不能把请求父目录交给 pytest 清理或删除调用者目录；测试临时材料仍只在 guard 允许根内。
4. 默认完整运行中，一个域 collection raw2 或测试 raw1 后继续其他域并保留各自 stdout/stderr/raw exit；父进程 `KeyboardInterrupt` 保持既有返回码 **130**，真实用户中断停止后续派发并列出尚未运行域。显式 `-x/--maxfail` 保留用户停止语义并标明未执行量，不能报完整执行。每域原始 pytest 结果及总体汇总同时可见，原历史日志不作本次执行结果。
5. 总体返回确定的非零状态：父进程中断按上一条返回 130；无法启动则单列原始启动诊断并使启动器非零，不伪造不存在的子进程 raw exit。已启动 pytest 自身返回的 2 仍记录为 2，不重标成父进程 130。正常 pytest 多域结果按 usage/guard 4、internal 3、collection/interrupted 2、test failure 1 的优先顺序传播，保留所有 raw 值；均无错误且有实际选中测试才返回 0。分区中因合法选择为空的 raw5 仅在其他域确有选中测试且无错误时可中性汇总，全部为空仍返回 5。`--collect-only` 用真实 collected/selected 判定，不要求 executed>0；不得把 collection 错误或缺输入当作空选择。
6. 用 pytest 事件统计每域 collected、selected、deselected、实际进入运行的节点、passed/failed/skipped、setup/teardown errors 与 collection errors；汇总按 nodeid/所属文件去重。尚未成功收集的文件不能臆造测试数，应列入“未解析总体”，不能把其他域通过数宣传成全部通过。过程日志可以写既有运行目录，不新增长期管理系统。

## 5. 必要验证与完成标准

启动器回归必须含真实子进程的最小合成反例：历史域主动删除 `ak_trading.*`/插入残缺包，普通域仍使用正确 `__file__` 与同一类身份；历史域故意 collection-error/raw2，普通域仍执行并留下证据。仅 mock subprocess 调用次数不足以证明隔离。对正常域/污染域交换顺序、反转历史文件顺序，比较真实 nodeid 集和结果，证明无跨域污染且各节点一次。再覆盖同目录与显式 node 选择、带值选项不误作路径、过滤导致一个/全部空域、失败传播及父进程 KeyboardInterrupt=130、每域 basetemp 派生/请求目录保留与 guard 原回归；生产 pin 故意错配的样例必须仍失败。

实现后执行以下验证，逐命令保留实际结果：

```text
python scripts/test_offline.py -q tests/test_offline_launcher.py
python scripts/test_offline.py --collect-only -q
python scripts/test_offline.py -q
```

顺序置换另以相同选中文件/node 集的正序/反序命令验证 collection 与执行；至少含普通 launcher 回归、DP03 `test_collector.py`/`test_scheduler.py`、DP04 `test_calendar.py`、DP08 `test_end_to_end.py`。保留真实 pin 失败，不为置换检查暂时换 pin。纯合成隔离回归须通过；实际 DP 域的 pin/fixture 失败须在两种顺序中保持同类且不污染普通域。最后一条必须是原完整入口，无额外 `-k`、新增忽略项或“代表样本”替代；原 guard 下无法运行的项目仍据实列 NOT_RUN。不得为全量运行启动服务、连 DB/SDK 或取消安全保护。

| 交付层次 | 关闭条件与证据 |
| --- | --- |
| 本轮 C05 导入隔离 | 两文件实际 diff 与前后 hash；合成污染/失败传播及原 launcher 回归通过；分区完整性、顺序置换、实际完整命令及每域原生结果可复核；独立 ZCode 对该 bounded write-set 无条件硬审 PASS。历史 pin 错误被准确保留本身不使隔离成果成为“条件 PASS” |
| 完整 C05 基线 | 所有必需历史输入具备合法来源/消费版本，原完整入口无 collection errors、选中总体实际执行，剩余测试失败按 owner 闭环；本计划不预判绿色。DP08 successor、DP01 pin、T7 或其他业务失败不能记成本轮已修 |
| 研究与运行 | 均不获新资格，不构成历史回测/模拟/部署验收；`enable_trading=False`、observer=`hold`、broker=`NOT_RUN`、real send/cancel=`0/0` |

实现、必要验证和新候选冻结完成即独立硬审，FAIL 回同一 ZCode 开发会话修复；不重审整套 r3、不等待计划 PASS、不把 release owner 后续工作变成本隔离实现前置。

命令与证据纪律依 `docs/maintenance/20261004-rg-no-match-r1/INCIDENT.md` 和 `docs/maintenance/20261004-grok-headless-cancel-r1/INCIDENT.md`（均为 LOCAL_ONLY，Root 派工需提供），包括 manifest-recovery-002：立即分别捕获 stdout、stderr、原生退出码；可选 rg 的 raw1 且空 stdout/stderr 才是 NO_MATCH，测试/CLI 不适用。必需输入先查存在，生成输出创建前不 Read；保留失败、原会话和一次性动作，不借新 worktree 重跑已消费命令。新测试验证是本修复的普通可重复验证；旧审核探针不重跑。进程终止后封存日志，进程/接收器/工具/产物/verdict 分开记录，不将 custody 成功等同产品 PASS。
