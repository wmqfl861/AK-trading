# NE1 原生执行通道操作单

> 上位文档：`docs/infiny-native-execution-plan.md`（`infiny-native-execution-plan-v1.0`，
> sha256 `89563d4a4566613282105ead6b14c92c7be374f901d159e916d6b063c53431fd`）。
> 本操作单覆盖计划 §12（部署、用户参数与回滚）与 §13（S1—S4 分阶段上线）中
> 主会话与用户各自执行的动作；**不授予任何真实报单/撤单权限**。
> 当前阶段：工程完成（S2 前置）。S3/S4 未执行，均 `NOT_RUN`；`broker=NOT_RUN`。

## 1. 范围与红线

- 部署对象**只有一个文件**：客户端 `AKExecutionBridge.py`（源
  `integrations/infiny/AKExecutionBridge.py`，sha256
  `c0bbfee0b4f47e9e23a27469265cd45a5e3723623494f1a1e6f012d871a3aeca`），目标为
  `C:\Users\Administrator\AppData\Roaming\InfiniTrader_WxyAllProgramX64\pyStrategy\self_strategy\AKExecutionBridge.py`。
- observer 双文件（同目录 `AKObserverBridge.py` + `AIBridge.py`）必须保持自包含
  双类 observer、逐字节相同、版本 `observer-20260914-reconnect-v5`。安装器内嵌的
  期望基线为 v5 验收时点记录值 `2ae4981ef49f99cc12fc39f92aff4d5655f7d9d46191acbd8c54e5ae8b221fb1`
  （采样 2026-09-14，`infiny-v5-observer-acceptance`）；**部署时的权威值是实测
  双文件哈希 + 采样时刻**（记录进 deployment.json 的 `observer_pair.sampled_at`）。
  双文件漂移即阻断，须先另立 observer 维护记录再以新基线放行。
- **17778 observer 服务是项目基础设施**：不得为完成本节点而结束/重启它；如确需
  维护 17778，另记录维护目的、前后版本和连接验收。
- 客户端 UI 的加载、停止、重载、启动和交易开关**全部由用户操作**；主会话与
  agent 不做任何 UI 自动化，不读取配对令牌、InvestorID、账户密码。
- 17779 部署前检查端口占用；占用即 `PORT_IN_USE`（退出码 2），**禁止结束占用
  进程或悄悄换端口**（计划 §3.1）。

## 2. 端口与进程隔离

| 维度 | 17778（observer，既有） | 17779（execution，本节点新增） |
| --- | --- | --- |
| 进程 | `scripts/run_infiny_bridge.py`，不受本节点影响 | `scripts/run_infiny_execution.py serve`，独立进程 |
| 路由 | `POST /analyze`、`POST /heartbeat`（语义零变化） | `POST /execution/v1/{poll,ready,results,events,control}`、`GET /execution/v1/status` |
| 启停 | 只由主会话按既有纪律操作；本操作单不包含对它的任何命令 | 启动/停止/重启由主会话执行并记录 maintenance artifact |
| 认证 | 既有 observer 机制 | 每实例一次性配对令牌（常时比较）；拒绝 Origin/Referer、非 loopback Host；无 CORS；首次握手 `service_epoch=null` 仅 disabled |
| 交集 | 无共享线程池/evaluator/停机路径 | 同左；服务进程不 import INFINIGO、不持 Investor、不直连柜台 |

**已文档化的 C3 偏差**：计划 §12.3 设想的"启动时 allowlist 三方核对"在冻结 C3
启动器中**未实现为 CLI 参数**（`run_infiny_execution.py` 无 allowlist 项）；符号
控制由 Binding 精确匹配 + N2 政策 scope 承担（H05 服务面由 TP 覆盖）。此偏差在
overall-revision-r1 冻结清单中如实记录，不由操作单掩饰。

## 3. 冻结链与前置条件（缺一不部署）

- 契约线：C1 锁 `647f1ac2…` → C1.1 锁 `bcacb46d…`（裁定 `7672f766…`）；
  C2 四线清单 v2（21 文件）；C3 服务三件（下表）；计划 `89563d4a…`。
- **哈希权威源是 overall-revision 系列最新 revision 的冻结清单**（`…/infiny-native-execution/overall-revision-r*/freeze-manifest.json`
  取最新；r1 整体审已结论 FAIL，修复波次 r2 生效后以 r2 为准，以此类推）；
  本表是操作员快照，revision 递进时必须随清单重算，不得沿用旧值
  （r1 整体审 OV-F01 教训：安装器修复后快照未刷新成孤值）。
- **武装波次进行中（NE1-ARB-ARMING-001，2026-09-18）**：下表仍是 r4 冻结快照，
  但安装器已升 `ne1-delivery-20260918-arming-v1`、配对工具已升
  `ne1-pairing-20260918-arming-v1`，客户端/服务/TP 由同波次 ARM-LM-C/S 以
  `ne1-execution-20260918-arming-v1` / `ne1-service-20260918-arming-v1` 升级；
  这些文件相对 r4 清单的漂移属本波次预期，**下一 overall-revision 冻结清单切割后
  必须随清单重算本表**，未重算前本表 installer 行不代表当前部署件。
- S1/S2 出口证据与独立硬审核按 overall-revision 冻结清单核对；任何文件相对
  冻结态漂移即停止部署。

| C3 冻结件（r4 pairing-v2 修复波现值） | sha256 |
| --- | --- |
| `src/ak_trading/infiny_execution/client_service.py` | `c63bff0498680edbfc8d8aa760134b54e6f9235a227f9c2f9d767a7073097c19` |
| `scripts/run_infiny_execution.py` | `4d98889d98fddf5125774e27c18a2133ea65baf4005ba8c6c15e08ac0366674f` |
| `tests/test_infiny_native_protocol.py`（TP） | `6cbfa563a579bd450db7edf691bbd826bd7e96f3f3f2e37dcab4fcf99ef73552` |
| `scripts/install_infiny_execution.py`（本操作单配套安装器） | `5f1bfaf526558a244cba4cc09b109d6511153365622dab116b93bfa9e7d55cc7` |
| `integrations/infiny/AKExecutionBridge.py`（客户端发行件，r4b pairing-v2b 参数默认值终值） | `c0bbfee0b4f47e9e23a27469265cd45a5e3723623494f1a1e6f012d871a3aeca` |
| `src/ak_trading/infiny_execution/ledger.py`（C1.2/n3-r8 终值） | `adaaa8b08376ad3dc646dea5ea817ad5a01cfdd1e535a986863d42f7e1d17d31` |
| `src/ak_trading/infiny_execution/executor.py`（C1.2/n4-r5 终值） | `c03efc7bc4d980d136c877f0851d3299dfdc2d36b44408723dd02b69247f2397` |
| `config/infiny_native_execution.example.json`（§7 模板） | `4a09e345ceaae5bb5116485067b4a5327b1387746e7edbfc6c5d6f3854d83f7d` |

- 用户已在 UI **停止将被替换的执行实例**（若有旧实例），主会话取得停止
  evidence（含 generation 与停止日志引用）写入 `stopped-evidence.json`。
- 全新证据目录已规划：`data/maintenance_artifacts/infiny-native-execution/<rev>/`。

## 4. 部署（安装器三段式，升级版 ne1-delivery-20260918-arming-v1）

安装器 `scripts/install_infiny_execution.py`（版本
`ne1-delivery-20260918-arming-v1`，配对最后一公里决定
`NE1-ARB-PAIRING-LASTMILE-001` §六 LM-D 行 + 武装机制裁定
`NE1-ARB-ARMING-001` §八 ARM-LM-D 行）提供 `plan / apply / rollback`。
相对旧版新增确定性 store 槽位纪律：

- **客户端默认 store 槽位**为
  `<self_strategy>\.ak_execution\strategy-<十进制 strategy_id>`（决定
  RULING 01 R1.1）。`plan`/`apply` 均接受非秘密 `--strategy-id INTEGER`
  用于核对默认槽位；生产命令必须传入。
- **`--client-store absent` 只在实测该槽位不存在时接受**：任何
  `strategy-*` 槽位实际存在时，声明 `absent` 即拒绝
  （`CLIENT_STORE_SLOT_EXISTS_MUST_BE_EXPLICIT`）；已存在必须传精确实际
  目录（`--strategy-id` 与声明不符、或指向槽位之外的目录均拒绝
  `CLIENT_STORE_SLOT_MISMATCH`）。不能把已存在 store 写成 absent。
- **plan 只实测、不创建**：store 由客户端首次有效 `on_start` 创建
  （R1.2/R1.3），部署阶段槽位不存在是首次部署的正常状态。
- **apply 不动接线与凭据**：store 一致性备份只覆盖 `receipts.db` /
  `outbox.db`；`pairing.json`、`identity.json`、`pairing-request.json`
  **以及武装机制新增的全部 `arming-*` / `disarm.json` / `arming.json`
  槽位**（AR4.1）绝不进入普通证据备份（安装器带硬守卫证明备份树只含
  两库），apply 也不修改任何 store 文件与策略接线；rollback 同样只恢复
  代码字节，从不恢复/修复/倒退武装状态或凭据（AR4.3、AR-M13）。

以下命令在 `D:\Projects\AK-trading` 执行（`<rev>` = 新证据 revision 目录；
**`<新冻结哈希>` 必须从最新 overall-revision 冻结清单取值填入，禁止沿用
本文任何旧示例哈希**——r1 教训 OV-F01）：

```powershell
# 1) 只读计划：列出全部核对项与 blockers（写全新 evidence 目录）
python -B scripts/install_infiny_execution.py plan `
  --strategy-directory "C:\Users\Administrator\AppData\Roaming\InfiniTrader_WxyAllProgramX64\pyStrategy\self_strategy" `
  --release-directory "integrations\infiny" `
  --evidence-directory "data\maintenance_artifacts\infiny-native-execution\<rev>\deploy-plan" `
  --strategy-id <框架实际 strategy_id> `
  --journal "data\execution_runtime\journal\exec.db" `
  --client-store absent `
  --pending-state "data\maintenance_artifacts\infiny-native-execution\<rev>\pending-state.json" `
  --expected-source-sha256 <新冻结清单中的客户端 release sha256> `
  --expected-target-sha256 <当前目标文件实测 sha256 或 ABSENT>

# 2) 正式部署：备份→一致性备份→原子替换→核验→deployment.json
python -B scripts/install_infiny_execution.py apply `
  --strategy-directory "C:\Users\Administrator\AppData\Roaming\InfiniTrader_WxyAllProgramX64\pyStrategy\self_strategy" `
  --release-directory "integrations\infiny" `
  --evidence-directory "data\maintenance_artifacts\infiny-native-execution\<rev>\deploy" `
  --expected-source-sha256 <新冻结清单中的客户端 release sha256> `
  --expected-target-sha256 <当前目标文件实测 sha256 或 ABSENT> `
  --strategy-id <框架实际 strategy_id> `
  --stopped-evidence "data\maintenance_artifacts\infiny-native-execution\<rev>\stopped-evidence.json" `
  --journal "data\execution_runtime\journal\exec.db" `
  --client-store <槽位已存在时必须传 .ak_execution\strategy-<id> 精确目录，否则 absent> `
  --pending-state "data\maintenance_artifacts\infiny-native-execution\<rev>\pending-state.json" `
  --sdk-version "<实测 SDK 版本>" `
  --client-python-version "<客户端嵌入 Python 版本>"
```

重新部署（槽位已存在）时 `--client-store` 必须改为精确槽位目录；安装器
会实测记录 `existing_slots` / `slot_exists` / `measured_at` 并按上述规则
放行或拒绝。安装器固定步骤（计划 §12.2）不变：①路径安全校验（拒
UNC/链接/junction/保留名/越界）+ observer 双文件核对（不同则报告并拒绝，
绝不悄悄同步）；②全新维护目录备份当前 `AKExecutionBridge.py`（不存在记
ABSENT）与两个 observer 文件原始字节，备份未过校验不替换；③journal 与
客户端 outbox 一致性备份（SQLite backup API，非裸文件复制；凭据文件不进
备份）；④临时文件 fsync + 替换前复验旧 hash 的原子替换，失败恢复备份字节；
⑤终验（执行文件 hash、observer 双文件不变且相同）后发布 `deployment.json`
（含代码/SDK/嵌入 Python 版本、源/目标/备份、模板 hash、确定性槽位实测
记录、计数起点占位、`CLIENT_RELOAD_PENDING`）。**S3 前 journal 存在真实
未决 attempt 即拒绝部署。**

### 4.1 pending-state 的产出方式（C3 裁决，如实记录）

冻结 C3 启动器**没有离线自省子命令**；`--pending-state` 由操作员产出，权威来源是
**服务进程运行中**经认证的 `GET /execution/v1/status` 快照（`execution_counters`
与 `pending_reconcile_count` 为持久 journal 事实）加客户端 store 计数：
`journal_pending_attempts` ← status 的
`counters.unresolved_dispatch_count + pending_reconcile_count`（按部署判定口径
取用并注明）；`real_native_send_attempts/real_native_cancel_attempts` ←
`counters.send_attempts/cancel_attempts`（区间值取上界时须注明
`count_complete=false` 语义）；`client_unacked_events` ← 客户端 outbox.db 实测。
记录必须带 `source`（`service_status|client_store_counts|manual_journal_review`）
与 `sampled_at`。**已知限制（进程内视图）**：`/events` 响应中的重复事件检测与
status 的 ACK 水位查询是进程内视图（冻结 journal API 无按实例流读取器）；服务
重启后这些视图清零而 journal 事实保留——不得以重启后"无重复标记"证明未重复，
持久去重仍以 journal 事件流键为准。服务已停且未留快照时，`manual_journal_review`
必须逐项留痕；journal 非 absent 而 pending-state 无 source/sampled_at 时安装器
拒绝（`PENDING_STATE_SOURCE_REQUIRED_FOR_JOURNAL`）。

## 5. 等待-登记-启动 17779（RULING 03 闭环；角色分明）

配对最后一公里决定（`NE1-ARB-PAIRING-LASTMILE-001` RULING 03）修订了首次
登记与启动的**唯一顺序**。核心变化：**客户端先启动进入 `WAIT_PAIRING` 等待，
登记与令牌生成后到**；服务启动前必须先完成 actor 登记；journal 由
`serve` 与所有 `register-*` 共用的 `<journal>.runtime.lock` OS 排他锁互斥
（占用即 `JOURNAL_RUNTIME_BUSY`，不杀进程、不删锁、不偷换 journal）。

角色分工（全程不变的红线）：主会话执行全部命令行操作并留痕；**用户只做 UI
操作（停止/重载/启动执行实例）和亲自执行 §6 的令牌交接命令**；agent 不读
令牌、不代填参数、不碰 17778。

冻结 CLI（`scripts/run_infiny_execution.py`，release
`ne1-service-20260917-pairing-v2`）= `serve` 子命令 + 四个操作员登记子命令
（`register-actor` / `register-evidence` / `register-authorization` /
`register-permit`，仅在服务停止时做排他 journal 维护，从不自行停进程）：

```powershell
cd D:\Projects\AK-trading
python --version   # 记录精确 3.12.x 到证据目录

# 1) 主会话：从仍在 WAIT_PAIRING 的客户端取非秘密身份，生成登记输入
#    （binding-template 为用户核实的非秘密绑定资料；instance/session 一律
#     取当前 active 请求，不得预填猜测）
python -B scripts/pair_infiny_execution.py build-actor-manifest `
  --client-request "<store>\pairing-request.json" `
  --binding-template "<经用户核实的非秘密绑定资料.json>" `
  --contract-lock "<冻结 contract-lock.json>" `
  --operator-action-id "<唯一操作ID>" `
  --operator-authorization "<本次离线登记授权记录.json>" `
  --output-directory "<新rev>\registration\<request_id>"

# 2) 主会话：服务已正常停止；当前生产 journal 已存在，禁止 --initialize。
#    新增 actor 不传 revision；更新同一 actor 的 Binding 必须传当前核实值。
python -B scripts/run_infiny_execution.py register-actor `
  --journal "data\execution_runtime\journal\exec.db" `
  --manifest "<新rev>\registration\<request_id>\register-actor-manifest.json" `
  --contract-lock "<冻结 contract-lock.json>" `
  [--expected-registration-revision <当前已核实的 registration_revision>]

# 3) 主会话：全新 run-id + 全新空私密 pairing-dir 启动；audit 存在则显式 --append
python -B scripts/run_infiny_execution.py serve `
  --host 127.0.0.1 --port 17779 `
  --journal "data\execution_runtime\journal\exec.db" `
  --audit-log "data\execution_runtime\audit\exec-audit.jsonl" --append `
  --pairing-dir "<私密runtime>\pairing-runs\<新run-id>\pairing" `
  --registry-dir "data\execution_runtime\registry" `
  --contract-lock "<冻结 contract-lock.json>"
```

要点：

- `--host` 固定 `127.0.0.1`（否则 `HOST_REJECTED`）；`--port` 禁止 17778
  （`OBSERVER_PORT_FORBIDDEN`）；占用即 `PORT_IN_USE` **退出码 2**，不结束占用
  进程、不换端口。journal 不存在须显式 `--initialize`，已存在则禁止 `--initialize`
  （**生产 journal 已存在，因此生产 serve/register 一律不带 `--initialize`**）。
- `--expected-registration-revision` 仅为 `register-actor` 接受，严格正整数；
  值来自上一次登记回执的 `registration_revision`（CAS），缺失/过期/错误的更新
  拒绝（`REGISTRATION_BLOCKED: ACTOR_CONFLICT`）；同一 manifest 重试幂等且
  revision 不变。不得静默读当前 revision 自动覆盖，不得换 actor_id 绕过冲突。
- **每次 serve 必须选择全新的空 `--pairing-dir`**（推荐
  `<runtime>\pairing-runs\<run-id>\pairing\`）：目录非空、含链接或遗留
  `*.token` 即拒绝（`PAIRING_DIR_NOT_EMPTY` / `PAIRING_FILE_EXISTS`）；失败
  run 目录不重用。配对目录及令牌文件先建立并验证仅当前用户可访问的受保护
  DACL，全部令牌私密落盘、监听建立之后才输出 `execution_started`。
- 启动状态行 `execution_started`（含 service_epoch、service_version、
  service `instance_id`（`execution-<hex>` 形态）、actors、instances、
  audit_log、`contract_lock_sha256`、`pairing_dir` 与 `pairings[]`（每个
  instance 的 session_epoch 与 token 文件**路径引用**、diagnostic_only）；
  `execution_enabled=false`、`observer_port_untouched=17778`）由主会话**逐字**
  保存为 `<rev>\runs\<run-id>\service-start.json`——它不含令牌或其摘要，
  是 §6 交接命令的输入。无已登记 actor 时生成进程级诊断令牌
  （`unregistered`，`diagnostic_only=true`），业务路由一律 `ACTOR_UNREGISTERED`。
- 配对令牌写 `<pairing-dir>\<instance_id>.token`（`ne1_service_pairing_v2`
  严格 JSON；仅当前 Windows 用户可读；已存在即 `PAIRING_FILE_EXISTS` 拒绝启动）；
  令牌绝不进 URL/日志/参数模板/产物/service-start。
- 操作员登记（S4 前置，全部离线、经 manifest 哈希核验、绑定 contract-lock）：
  `register-actor`（账户 actor；绑定须唯一且共享同一 account_key）、
  `register-evidence`（基线/路由/清算/线程证据四类）、`register-authorization`、
  `register-permit`（试运行许可；SYNTHETIC 来源一律 `SYNTHETIC_REFUSED`）。
- **armed 全链路属 S4 前置（文档化裁决）**：服务 readiness 的 "armed" 仅表示
  journal context.mode ≠ disabled（`can_plan` 可真），**`can_submit` 与
  `can_cancel_owned` 恒为 false**；生产编排器以 `port=None, mode=DISABLED` 装配，
  任何 submit 尝试得到声明式 `MODE_DISABLED` 阻断。真实 dispatch 需要：离线
  登记的 permit/authorization/证据（RULING 05）+ §13.2 清单全过 + **§6A 的
  用户本地控制台交互 arm（AR1.2/AR1.3，替换原 UI 表单确认）**。
  **armed ≠ 可下单**；S4 前置未满足时 armed 状态只代表规划就绪。
- 停机顺序（§5.3）：停止签发 → actor 有界 join → journal 完整性检查 → close；
  不清账本、不撤单、不动客户端 outbox、不动 17778。17779 重启后
  service_epoch 递增；仍在等待的客户端不会自动复用旧令牌，按 §6 的
  stop/start 序列重新配对。

## 6. 用户重载与配对交接（完整顺序与角色分工，计划 §12.4 + RULING 03）

本节是配对最后一公里的**完整闭环顺序**；任何一步缺位即为未配对，不得把
"已加载"或"已部署"写成"已配对"。首次部署按 1→7 顺序执行；此后正常
stop/start 重启按"重启子序列"执行。

1. **主会话**完成 §3 冻结链核对与 §4 部署（版本/备份/路径/槽位实测）；
   **用户**在 UI 停止旧执行实例（若有）并由主会话记录停止 evidence；
   17778 observer 全程不动。
2. **用户**保持 `execution_mode=disabled`、`enable_trading=false`、真实次数
   上限 0，在 UI 重载策略列表、单独选择 `AKExecutionBridge` 并加载实例，
   核对日志中的版本（新 release 的 `ADAPTER_VERSION`，配对升级版为
   `ne1-execution-20260917-pairing-v2`）、class_name 与 instance。
   客户端首次有效 `on_start` 在默认槽位
   `<self_strategy>\.ak_execution\strategy-<strategy_id>` 创建/复用 store
   （`identity.json` / `receipts.db` / `outbox.db` / `client.lock`），生成本次
   `session_epoch` / `generation` / `request_id`，发布非秘密
   `pairing-request.json` 并进入 **`WAIT_PAIRING`**。**此时不要求服务已启动，
   也不要求已有令牌**；等待期间客户端不发任何匿名 poll/control/events。
   **用户让这个客户端会话保持运行等待**——为"取身份"而再次 stop/start 会使
   该请求作废。
3. **主会话**用 `inspect` 读取非秘密身份（零写入、不打开凭据文件）确认
   active 请求与完整 instance/session/generation，再按 §5 第 1 步用同一请求
   生成 actor manifest；**用户**核实 binding-template 中的
   account_key/strategy_key/交易日等非秘密绑定资料。
4. **主会话**在服务停止且 journal 排他维护下执行 `register-actor`（§5 第 2
   步；绑定取同一请求的 instance/session，记录 registration_revision）。
5. **主会话**以全新 run-id + 全新空私密 pairing-dir 启动 17779（§5 第 3 步），
   为该 Binding 生成新令牌；保存不含秘密的 `service-start.json`。
6. **用户本人**（运行 InfiniTrader 的同一 Windows 用户，在主会话交付的受审
   命令上）执行交接；主会话不代执行、不读令牌：

   ```powershell
   python -B scripts/pair_infiny_execution.py install `
     --client-store "<self_strategy>\.ak_execution\strategy-<strategy_id>" `
     --client-request "<store>\pairing-request.json" `
     --source "<私密runtime>\pairing-runs\<run-id>\pairing\<instance_id>.token" `
     --service-start "<新rev>\runs\<run-id>\service-start.json" `
     --contract-lock "<冻结 contract-lock.json>" `
     --expected-client-sha256 "<新冻结清单中的客户端完整 sha256>" `
     --receipt "<新rev>\runs\<run-id>\pairing-transfer.json"
   ```

   退出码 0 仅表示原子交接成功（receipt
   `TRANSFERRED_AWAITING_HANDSHAKE`）；2 为校验/安全拒绝（如
   `PAIRING_ACL_RESTRICTED`、`PAIRING_FORMAT_INVALID`、
   `PAIRING_DIAGNOSTIC_ONLY`、`PAIRING_SERVICE_MISMATCH`、
   `PAIRING_CONTEXT_MISMATCH`、`PAIRING_REQUEST_INACTIVE/CHANGED`），
   3 为 I/O/发布失败。工具在发布前后复验 active 请求、槽位身份、release
   hash 与 Windows DACL；同槽位新 active 请求可轮换旧凭据，同请求凭据冲突
   或 foreign 文件拒绝。
7. **验证握手**：客户端在下一合规调度/合法回调激活凭据，完成 null-epoch
   disabled poll、学习并核对 service epoch，进入 **`PAIRED_DISABLED`**；随后
   control/events/results/status 按原认证/绑定契约运行。两端非秘密
   receipt（`pairing-transfer.json` + `service-start.json`）关联同一完整身份
   与代码 hash，核对默认禁用与真实调用计数为 0。**配对成功仅关闭通信缺口**：
   NativeAuthorization、NativePilotPermit、原生证据与用户 UI 交易授权仍是
   各自独立前置（§9）。

**重启子序列（RULING 03 R3.2，正常 stop/start 或 17779 重启后）**：用户先
正常 stop 客户端 → 主会话停 17779 → 用户 start 进入 `WAIT_PAIRING` → 主会话
重新生成 manifest、以**原 actor_id + `--expected-registration-revision <当前
值>`** 更新 Binding → 全新 pairing-dir 启服务 → 用户交接 → 握手。
**这次 stop 之后不要再为"让令牌生效"而再次 stop/start**——那会生成新
session，回到绑定不匹配。服务单独重启（epoch 变化）时客户端关闸并保持
`RESTART_REQUIRED`，按同一子序列恢复；不支持旧 active 会话上的自动热换令牌。

模板、UI 保存值和运行值需逐项一致（§7）；重载后若恢复了 true，启动逻辑必须
关回 false 并要求新确认。无安全周期 GUI 回调时，状态变化在下一合法回调展示；
`ui_idle_refresh` 保持 `UNSUPPORTED`。

**武装确认入口的显式替换（NE1-ARB-ARMING-001 AR1.2，2026-09-18）**：原计划
§7.1/§7.2 的"实际 UI enable_trading"、§12.4/§13.2 的"实例 UI 输入 challenge
并设 true"，以及本节旧文本"用户在执行实例提交新的参数确认……最后**亲自**设置
`enable_trading=true`"，因本构建无参数表单（F12：证据等级
`USER_REPORTED / ORIGINAL_UI_EVIDENCE_MISSING`）**全部替换为**：**同一
Windows 用户在受审本地控制台亲自核对完整作用域并交互确认；客户端验证一次性
凭据后形成仅当前生命期有效的用户准入状态。** 用户仍亲自操作 InfiniTrader 的
加载/启动/停止；本替换只改变人工确认载体，不降低 S4、注册链或 native 最终
门禁。wire 字段 `ParameterConfirmation.explicit_ui_action` 语义扩为"本次显式
本地用户界面动作（含受审控制台）"；本地 receipt 记
`action_source="USER_LOCAL_CONSOLE"`，不得填 GUI_CONFIRMED。完整武装流程见
§6A；**该阶段当前 `NOT_RUN`**。

## 6A. 武装（arming，NE1-ARB-ARMING-001；本节为未来操作规范，未执行）

工具：`scripts/pair_infiny_execution.py`（版本
`ne1-pairing-20260918-arming-v1`）新增 `prepare-arm / arm / disarm` 三命令，
`inspect` 扩充非秘密武装状态。核心原则（AR1.1）：**预备与激活分离**——
prepare-arm 只递交候选参数，arm 才是用户动作，disarm 只撤销本地准入；
服务不得自行发起武装，任何 agent、安装器、启动脚本、定时任务或 UI 自动化
**不得代执行生产 arm**；主会话只准备经审命令与非秘密材料，不能调用生产
arm、填写确认短语或读取武装秘密（challenge）。

**双 hash（AR2.1）**：`pairing_params_sha256` 是本次 on_start 已冻结的安全
参数 hash，继续用于 pairing-request/pairing.json 验证及禁用握手，**不变**；
`target_params_sha256` 是客户端在同一生命期基于冻结参数副本计算的待确认
有效参数 hash（覆盖字段严格只有 `execution_mode="native"`、`risk_policy_id`、
`authorization_id`、`max_native_sends=1`、`max_native_cancels=0|1` 五项）。
**客户端是目标 hash 的唯一计算者**；工具先递交 proposal、读取客户端发布的
非秘密 plan，再据此准备登记材料；NativeAuthorization/NativePilotPermit/
ParameterConfirmation 与后续 offer/dispatch 一律绑定 target hash，配对文件仍
绑定 pairing hash。仅"写 arm 文件再改 Params"会破坏配对绑定链，不允许。

**唯一八步时序（AR3.2）**——任何一步的服务重启、认证拒绝、epoch 变化或
客户端 session/generation 变化都使本轮候选与凭据失效，回原 R3
stop/start→CAS 登记→新配对序列；不得"先配对再停服补登记、回来沿用旧 arm"：

1. 完成新 release 的 owner 开发、独立审核与同版本必要阶段证据；已有旧
   服务/会话时用户先停客户端、主会话正常停 17779；用户按 R3 启动新客户端
   到 WAIT_PAIRING 并保持会话不再 stop/start。
2. 主会话据 active pairing-request 与经用户核实的 Binding 生成非秘密
   candidate，调用 `prepare-arm`（命令见下）；客户端在仍禁用时校验它、
   计算 target hash、发布 `arming-plan.json`（`PREPARATION_SUBMITTED` 只表示
   proposal 已递交，计划由客户端异步发布）。
3. 保持 17779 停止，journal 维护锁下按 **register-actor → register-evidence
   → register-authorization → register-permit** 登记（§5；authorization/
   permit 使用同一 plan 的完整 Binding 和 target hash；actor 更新用原
   actor_id + 显式 `--expected-registration-revision`；原 journal 存在时不带
   `--initialize`）。
4. 注册必须发生在启动将要用于 arm 的服务之前；服务从该 registry/journal
   加载（LM-S 版本须支持 `--arming-readiness` 并在 service-start 记录
   `arming_prerequisites`）。
5. 主会话以新 run-id、新空私密 pairing-dir 启动服务（`--arming-readiness`
   指向已批准 prerequisites 文件），保存非秘密 service-start；用户执行既有
   `install`（§6 第 6 步）；客户端按 null-epoch disabled 握手进入
   PAIRED_DISABLED（仍使用 pairing hash）。
6. 客户端为冻结 plan 发布本次 `arming-request.json` + 私密
   `arming-challenge.json`（激活窗口 ≤300 秒）。**用户本人**在同一 Windows
   用户下运行 `arm`：工具核对本次服务身份、当前请求、已登记记录的非秘密
   导出与回执、S4 前置引用，展示最终作用域（账户标识、完整 Binding、双
   hash、模式、动作、数量、价格范围、到期、authorization/permit、send=1、
   显式 cancel=0/1、本次生命期唯一激活），用户现场输入确认短语
   **`ARM <arming request_id 末 8 位>`** 后才发布一次性 `arming.json`。
   随机 challenge 不显示、不让用户复制；工具拒绝 stdin 重定向/管道/无控制
   台/`--yes`/预填确认/非交互模式（`ARM_INTERACTIVE_REQUIRED`），重试不能
   自动回答提示；**工具没有任何自动确认接口**。
7. 客户端消费凭据、持久记录确认 ID 并销毁秘密，发送既有
   PARAMS_CONFIRMED；服务按 AR3.3 独立核验。客户端收到匹配 ACK 后在下一
   合法回调再检时效/撤销/绑定才进入 ARMED。工具 receipt
   （`ARM_SUBMITTED_AWAITING_CLIENT`）、服务 RECORDED、客户端 ARMED 三个
   事实分别记录，**不能互相代替**；仅服务 ACK 或文件 receipt 仍算 pending。
8. 只有独立 S4 门和可信 native 装配都已满足，后续合法 offer→ready→dispatch
   才可能调用 native；预算由登记 authorization/permit 与 N3 持久 attempt
   约束（send=1；cancel 仅在独立授权时 0/1）。

**PENDING 与关闸确认（AR5.2/AR5.3）**：`DISARM_REQUESTED/REVOKE_PENDING`
只表示撤销请求已安全提交；客户端关闸 ACK 与服务未决票据/结果屏障分开显示。
本地安全已关闭但服务断连时显示 `LOCAL_GATE_CLOSED / SERVICE_ACK_PENDING`；
`DISABLED_CONFIRMED` 只有本地关闸与服务屏障均有证据、未决项按契约闭合后才
给出——**unresolved 或缺计数不得写零**。进程卡住或控制线程未处理请求时工具
保持 PENDING/UNKNOWN，不冒充即时完成。退出码：0=本命令阶段完成（状态是
阶段事实，arm 永不返回 ARMED）；2=安全/格式/身份/授权预检拒绝；
3=I/O/发布失败；等待最终 ACK 超时用明确 PENDING 状态与非零退出，不假装
成功或自动再投递。

**store 槽位与 ACL（AR4.1/AR4.3）**：客户端 store 白名单新增
`arming-proposal.json`（prepare-arm 写）、`arming-plan.json` /
`arming-request.json` / `arming-state.json`（客户端写，非秘密）、
`arming-challenge.json`（客户端写，私密）、`arming.json`（用户 arm 写，
私密一次性）、`disarm.json`（disarm 写）、`arming-transfer.lock`（短时传递
锁）。全部 ≤64 KiB 严格 UTF-8 JSON、未知 schema 拒绝；私密/非秘密槽均要求
同域受保护 Windows DACL（R2.4 同标准），权限先于秘密进入进程/落盘；秘密值
（challenge）绝不进 stdout/stderr/receipt/备份/URL/环境变量。

命令（`<rev>` 为本轮证据目录；全部 PATH 引用，无秘密值参数）：

```powershell
# 主会话：递交非秘密候选（输入 candidate 为 ne1_arming_proposal_candidate_v1）
python -B scripts/pair_infiny_execution.py prepare-arm `
  --client-store "<self_strategy>\.ak_execution\strategy-<strategy_id>" `
  --client-request "<store>\pairing-request.json" `
  --proposal "<rev>\arming\candidate.json" `
  --contract-lock "<冻结 contract-lock.json>" `
  --expected-client-sha256 "<新冻结清单客户端 sha256>" `
  --receipt "<rev>\arming\prepare-arm-receipt.json"

# 用户本人（本地控制台交互；工具版本 hash 须与 prerequisites 一致）
python -B scripts/pair_infiny_execution.py arm `
  --client-store "<store>" `
  --arming-request "<store>\arming-request.json" `
  --service-start "<rev>\runs\<run-id>\service-start.json" `
  --authorization-record "<rev>\registration\authorization.json" `
  --permit-record "<rev>\registration\permit.json" `
  --registration-receipts "<rev>\arming\registrations.json" `
  --s4-readiness "<rev>\arming\prerequisites.json" `
  --contract-lock "<冻结 contract-lock.json>" `
  --expected-client-sha256 "<新冻结清单客户端 sha256>" `
  --receipt "<rev>\arming\arm-receipt.json"

# 用户本人：撤销（离线幂等；不要求服务在线，不下撤单）
python -B scripts/pair_infiny_execution.py disarm `
  --client-store "<store>" `
  --client-request "<store>\pairing-request.json" `
  --receipt "<rev>\arming\disarm-receipt.json"

# 主会话：只读非秘密武装状态（含私密槽存在性，绝不打开）
python -B scripts/pair_infiny_execution.py inspect --client-store "<store>"
```

两份新非秘密清单由主会话按 AR5.3 冻结格式准备并经用户核对：
`registrations.json`（`ne1_arming_registrations_v1`，本轮 actor/证据/
authorization/permit 登记回执索引，逐项 record/manifest/receipt 哈希）与
`prerequisites.json`（`ne1_arming_prerequisites_v1`，五类 S4 前置证据引用：
RELEASE_REVIEW、S3_ZERO_NATIVE、MANUAL_TAKEOVER_APPROVAL、SINGLE_ORDER_
APPROVAL、NATIVE_ASSEMBLY_ACCEPTANCE；服务 `--arming-readiness` 必须加载同一
文件）。清单只是索引/锚点，不授工具权威，不能引用空占位或把 NOT_RUN 当
通过；材料缺失或新 release 尚无 S3/S4 前置时 arm 拒绝，且**没有生产
`--force/--skip-s4`**。

**生产 arm 的 W5 门槛**：仅当 S4 全部前置、真实可信装配、同版证据和用户对
该单的独立授权均满足，用户才可执行生产 arm；本节全部内容当前
`arming_implementation` 以各 owner 冻结交付为准，`production_arming=NOT_RUN`、
`broker=NOT_RUN`。

## 7. 用户参数模板（§12.3）

无凭据模板 `config/infiny_native_execution.example.json`（sha256
`4a09e345…d83f7d`，与安装器内置校验器逐键一致）：

- `execution_api_url` 默认 `http://127.0.0.1:17779/execution/v1/poll`；`api_url`
  保持 observer 默认 `http://127.0.0.1:17778/analyze`（兼容保留）。
- `enable_trading` 默认 `false`；`execution_mode` 默认 `disabled`（**不是能力
  许可**，见 §5 armed 裁决）；`max_native_sends` / `max_native_cancels` 默认 `0`。
- `volume` 仅是 `order_volume` 的旧别名，不在模板中；同时提供两个不同值时客户端
  拒绝。
- `investor_id` 由用户在客户端本地选择，agent 不读取其内容；传输与证据使用不
  透明 `account_key`。`account_tag` 永远只是备注。
- 价格、具体 Action、close_policy、source_key 和有效期属于用户操作单/OrderIntent，
  不从 `order_volume` 或 buy/sell 猜测；风险数值由 `risk_policy_id` 指向已审政策。
- 参数 hash 覆盖账户绑定引用、策略/合约/hedge、URL、数量、类型、限价范围、
  预算政策和版本，并绑定 execution_mode、authorization_id 引用与 send/cancel
  次数上限；不含确认 challenge 本身或瞬时 enable_trading 值。换账户/合约/URL
  必须 stop/reload，原参数确认立即失效。

## 8. 状态边界与真实计数（计划 §13.3）

计数必须在客户端实际原生边界记录，**不能复用 observer 恒定 0 的
`order_submissions`**。服务侧 `GET /execution/v1/status` 暴露 journal 事实计数
（`execution_counters`：`committed_dispatch_count`、`consumed_receipt_count`、
`send_attempts/cancel_attempts`（`CallCount` 区间：无客户端边界证据时
`value=null` 只有 lower_bound）、`local_order_ids`、`unique_trade_count`、
`unresolved_dispatch_count`）与 `pending_reconcile_count`、`ack_watermark`
（后者进程内视图，见 §4.1）。验收最少记录字段：

| 字段 | 语义 |
| --- | --- |
| `native_send_attempts` | 进入真实 sendOrder 调用的次数（含本地拒绝/异常后），不等于柜台接受数 |
| `native_cancel_attempts` | 真实 cancelOrder 调用次数，同上 |
| `local_order_ids` | 本地受理返回的非负 OrderID 清单（仅本地受理证据） |
| `broker_order_acks` | 有归属证明的柜台委托确认数 |
| `unique_trade_count` | 去重成交笔数 |
| `fake_send_count` / `fake_cancel_count` | 模拟调用计数，**与真实字段绝不相加** |
| `unresolved_dispatch_count` | 已 commit 未闭合票据数 |
| `count_complete` | 边界日志/ACK 是否齐备；缺失时报计数区间且为 false |

验收规则：

- 客户端在调用边界记录 command/attempt/本地序号/线程/generation/请求 hash，
  服务用已 commit 票据及 RETURNED 收据核对；启动/停止**不得重置计数**，按证据
  窗口取累计增量。
- CONSUMED 之后崩溃、边界日志缺失或 ACK 未齐 → 报告计数区间且
  `count_complete=false`；**不能把潜在调用算作 0**。
- S3 要求完整证据证明真实 send=0、cancel=0；S4 要求实际 send 恰为授权的一次、
  cancel 不超授权，且无未解释重复调用。
- 非负 OrderID 仅是本地受理：无可归属柜台回报时 `broker=PARTIAL_UNVERIFIED`，
  不得 PASS。没有发生的分支（部分成交/撤单/FAK 等）分别标 `NOT_RUN`。
- **`/results` 未知 command 的文档化裁决**：journal 在 results 接缝对"本 journal
  从未 commit 过的 command"返回 NOT_FOUND，映射为传输错误 `INVALID_REQUEST`
  （400）——冻结错误表未声明 not-found 传输码。客户端/操作员看到
  `INVALID_REQUEST` 必须理解为"该 command 非本 journal 签发"，**绝不据此释放
  资源或推断柜台结果**（计划 §8.1：不解析错误文本猜结果）。

阶段字段（`acceptance-s3.json` / `acceptance-s4.json`）：

| 阶段 | 允许执行 | 出口证据 | 禁止推导 |
| --- | --- | --- | --- |
| S1 离线单元 | 临时 SQLite、FakeClock、FakeNative | §10 单元组、全部适用 H 证据、冻结源码；真实 send=0/cancel=0 | 单元 PASS 不当作客户端/broker 验收 |
| S2 fake-framework | 真实交付客户端源码 + fake INFINIGO + 测试自身 loopback | H01—H40 全覆盖、全部命令及 observer 回归、独立硬审核 PASS + release lock | SYNTHETIC 回报不当真实柜台事实 |
| S3 客户端加载、交易禁用 | 用户实际加载、只读采样/网络往返、独立 simulation journal | 同版本部署收据、真实 UI 版本/实例 receipt、≥10 分钟交易时段日志与目标合约 tick、正常 stop/start、一次受控 17779 断连恢复；两端真实 send=0/cancel=0 且计数完整 | 已加载/simulation 成交/observer 恒 0 不推出真实执行已授权 |
| S4 首次真实订单 | 单账户单实例单具体合约，最多 1 次真实 send；CANCEL_OWNED 单独授权且最多 1 次 | S2 PASS、S3 零真实调用、§9 清单、原生边界收据、原始回报与 N3 投影/计数相符 | 一次成功不证明全账户对账/自动交易/所有订单类型/重连恢复/盈利 |

无真实 tick 时 S3 平台验收保持 `PARTIAL_UNVERIFIED`，不能用 TEST 请求补足。
S4 未执行时总体报告只写 `NOT_RUN`。全账户对账、GUI 无 tick 刷新、期权/组合/
主连、FOK/市价、自动追单与全账户撤单均为 `UNSUPPORTED`（§14.2）。

## 9. 首次真实订单授权清单（计划 §13.2，S4 逐项留证，缺一不发单）

- [ ] 精确源码/客户端部署 hash、执行契约 lock（`bcacb46d…`）、依赖硬审核及
  NE1 独立工程 PASS 有效；代码改变后重新取得受影响审核和 S3 证据。
- [ ] S3 在同一 release 下完成，真实 send/cancel 均为 0 且没有未确认计数缺口；
  客户端确实重载并运行该版本。
- [ ] 用户单独批准有限范围人工接管；证据包明确委托/成交/持仓冻结基线、来源、
  完整性和外部写入隔离；不能只有"无挂单"勾选。无法提供完成依据即停在 S3。
- [ ] 基线在武装时不早于 5 分钟，期间无外部交易/人工操作/重连/事件缺口；
  实际 quote/资金/持仓仍满足 3/5 秒时效。
- [ ] SDK 版本和真实账户/策略 callback 路由已核实，Investor 匹配用户选择；
  hedge、Memo 长度/回传、合约 tick/min/max、订单类型支持及现金上界均有证据。
- [ ] 用户给出明确单次操作单：不透明账户标识、策略/实例/会话、完整合约、
  OPEN_LONG/OPEN_SHORT/CLOSE_LONG/CLOSE_SHORT 之一、具体数量、限价及允许范围、
  GFD 或 FAK、有效期、source_key；数量满足交易所下限且无需拆单。
- [ ] 操作单明确允许的真实 send 次数=1；是否允许自有撤单及其次数单列，不能
  默认授权。首次验收不含自动改价、补单、反手或第二次发送。
- [ ] N3 无未解释 attempt/事件缺口/资源冲突；当前数据/风险/CAS 通过；运行时
  重新解析的参数与用户确认 hash 一致。
- [ ] 离线登记链完整（register-actor → evidence → authorization → permit，
  §5）且 permit 未消耗，登记绑定同一 plan 的完整 Binding 与 target hash
  （§6A 双 hash）；`armed` readiness 出现且 §5 裁决的全部 S4 前置满足。
- [ ] 用户按 §6A 八步时序亲自完成本地控制台交互 arm：核对完整作用域后现场
  输入 `ARM <request_id 末 8 位>` 确认短语；客户端验证一次性凭据（challenge
  不显示、不可复制）后形成仅本次生命期有效的用户准入（AR1.2/AR1.3 替换原
  "UI challenge + enable_trading=true"要求）。代码/安装脚本/agent 不能代开，
  receipt `action_source=USER_LOCAL_CONSOLE`，绝不填 GUI_CONFIRMED。
- [ ] 用户已知超时或不明结果时不重发，且需要保留未决记录；约定挂起订单的
  授权撤单或用户手动处置方式，不能在无回报时用额外实单探测。

许可的 send 和 cancel 额度分别持久消费。第一次 send 后立即锁住后续 send；
测试结束用户关闭开关，记录 `DISABLED_CONFIRMED` 和未决情况。任何自动/外部
介入使本轮独占许可失效并要求对账。

## 10. 回滚（计划 §12.5）

前置：用户关闭执行开关 → 主会话停止签发（停服或撤控）→ 取得 §5.3 的
关闸/停机证据（`gate-evidence.json`：`gate_closed=true`、
`service_stopped_issuing=true`、`evidence_refs` 指向 GATE_CLOSED 收据/停机记录；
`REVOKE_PENDING` 未闭合时如实记录阶段）。**若有未决真实订单，先记录其状态和
用户处置方式（`pending-disposition.json`），不能靠部署回退解除风险。**

```powershell
python scripts/install_infiny_execution.py rollback `
  --strategy-directory "C:\Users\Administrator\AppData\Roaming\InfiniTrader_WxyAllProgramX64\pyStrategy\self_strategy" `
  --evidence-directory "data\maintenance_artifacts\infiny-native-execution\<rev>\deploy" `
  --rollback-evidence-directory "data\maintenance_artifacts\infiny-native-execution\<rev2>\rollback" `
  --gate-evidence "data\maintenance_artifacts\infiny-native-execution\<rev2>\gate-evidence.json" `
  --journal "data\maintenance_artifacts\infiny-native-execution\<rev>\journal\execution.sqlite" `
  --client-store "<客户端实际 store 目录或 absent>" `
  [--pending-state "<rev2>\pending-state.json" --pending-disposition "<rev2>\pending-disposition.json"] `
  [--final-counts "<rev2>\final-counts.json"]
```

回滚器固定行为：①先把**当前失败版本字节**及新事实（journal、客户端 outbox
当时状态）备份到全新 rollback 目录——代码回滚不删除订单、回报、授权消费或
审计包；②核对当前目标 hash 与 deployment.json 一致，漂移则拒绝（显式
`--accept-target-drift` 须留痕）；③按收据旧 hash 恢复，原先 ABSENT 时仅移除
本次安装且 hash 仍匹配的单个文件；④复验 observer 双文件不变且相同（记录
采样时刻）；⑤发布 `rollback.json`（关联部署收据、旧/新 hash、未决订单、真实
send/cancel 计数、待核对项）。

用户随后在 UI 重载并确认旧类 disabled/observer 状态；本地服务停止 native
路由，账本保留只读/恢复能力。**新实单之后不自动恢复旧数据库**；有真实尝试后
回滚代码不回滚事实。

## 11. 故障快速索引（详见计划 §8.1）

| 现象 | 边界动作 | 禁止 |
| --- | --- | --- |
| `PORT_IN_USE`（17779 被占，退出码 2） | 停止部署，查明占用者 | 结束占用进程、悄悄换端口、动 17778 |
| 客户端 outbox 超限/写失败 | 即时关闸、`EVENT_GAP` 锁存 | 丢最旧事件继续交易、heartbeat 清锁 |
| dispatch 已 commit 未闭合 | `SUBMIT_UNKNOWN` 锁存、预留保留 | 换 intent 重发、以新 ID 补发 |
| commit 后撤权 | 服务 `REVOKE_PENDING` 至关闸 ACK+收据核齐 → `DISABLED_CONFIRMED` | 提前显示"已彻底停止" |
| `/results` 返回 `INVALID_REQUEST` | 该 command 非本 journal 签发；走核对路径 | 据此释放资源/推断柜台结果（§8 裁决） |
| 服务断连/重启（新 epoch） | 客户端关调用；恢复先上传未 ACK 事件；新 epoch 初始 disabled | 离线撤单、伪造水位、把进程内视图清零当事实消失 |
| 未知回报/归属不明 | 持久隔离（QUARANTINED） | 贴新 epoch 认领、按最后回调覆盖 |

## 12. 证据目录结构（每个 revision 新建，不得覆盖）

```text
data/maintenance_artifacts/infiny-native-execution/<rev>/
  release-notes or 冻结引用        # 指向 overall-revision-r1 冻结清单
  journal/execution.sqlite         # 服务 N3 唯一业务账本（--initialize 创建）
  pairing/<instance_id>.token      # 配对令牌（仅当前用户可读；agent 不读取）
  registry/                        # 离线登记的证据/授权/许可制品
  service-audit.jsonl              # 服务审计日志（--append 续用）
  deploy-plan/deployment-plan.json # 安装器 plan 产物
  deploy/deployment.json           # 部署收据（备份清单、模板 hash、采样时刻）
  deploy/service-start.json        # execution_started 状态行/PID/计数起点
  deploy/backup/                   # 三文件字节备份 + journal/outbox 一致性副本
  stopped-evidence.json / pending-state.json / gate-evidence.json
  arming/candidate.json            # prepare-arm 输入（非秘密 candidate）
  arming/registrations.json        # ne1_arming_registrations_v1 登记索引
  arming/prerequisites.json        # ne1_arming_prerequisites_v1 S4 前置清单
  arming/prepare-arm-receipt.json / arm-receipt.json / disarm-receipt.json
  acceptance-s3.json / acceptance-s4.json / limitations.json
  rollback/                        # 回滚目录（另一 revision）
```

注：客户端 store 内的 `arming-*` / `disarm.json` / `arming.json` 槽位属客户端
私有域，**绝不进入本证据目录**；此处只收集工具侧非秘密 receipt（AR4.3）。

## 13. 哈希与版本记录

冻结链完整值见 `data/maintenance_artifacts/infiny-native-execution/overall-revision-r1/freeze-manifest.json`
与 `data/maintenance_artifacts/infiny-ne1-delivery-prep/hash-slots-checklist.md`
（含来源纪律：禁止把观测值升格为冻结值；observer 双文件哈希以部署/服务启动时
实测为准并注明采样时刻）。
