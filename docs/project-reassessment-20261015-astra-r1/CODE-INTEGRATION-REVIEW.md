# Astra/max 实际开发成果与代码集成复核

- 复核日期：2026-10-15
- 复核者：Astra/max 独立重新评估；本报告不是正式 ZCode 硬审
- 目标：找出会阻止首个真实策略研究/回测结果的实际源码、接口和集成缺陷，并核对蓝图是否正确接入已完成局部成果。
- 本报告只写本文件；源码、计划、冻结包、历史报告和外部候选树均未修改。

## 复核边界和证据方法

我先读取了项目 AGENTS.md、PROJECT-WORKFLOW-RULES.md、两份 20261004 maintenance incident，并加载 codebase-memory 与 tqsdk-trading-and-data 技能。TqSdk 只作为业务边界参考，本轮没有连接 TqSdk、数据库、客户端、服务或真实账户。

图谱 D-Projects-AK-trading 报告 generation 2026-10-13T01:30:24Z、19519 nodes、112916 edges、ready。对主线文件执行了 search_graph → trace_path → get_code_snippet → check_index_coverage。相关主线文件均返回 metadata_changed，因此源码结论以随后直接读取的实际文件为准；D:/AKS261014、D:/AKF261014 等外部候选树为 outside_project，不能算图谱已覆盖。没有把图谱的“无记录问题”当作完整性证明。

本轮实际执行的只读验证：

- 解析并读取 M1 P1、M2/P2、DAILY、M3C 的既有 JSON/文本证据，保留原始退出码和语义。
- 对 experiments.py 做 Python AST 小探针：确认 run_shadow_replay_experiment 对 _reg 只有一次 Load，函数内没有 Store，模块顶层也没有 _reg。
- 读取 ContinuousLedger.checkpoint 的真实签名，确认 exit_registry_sha256 是必填 keyword-only 参数。
- 在隔离 Python 子进程中对外部快照候选的 FeatureSnapshot 紧凑存储做 SYNTHETIC 有界探针（N=128、256、512、1024、2048）；没有运行原 25,679 人口测试、没有 fit、没有下载数据，也没有写产品文件。
- 没有运行全仓 pytest、正式研究命令、外部 CLI、ZCode、Grok、数据下载、服务重启或交易操作。

## 决定性源码缺陷

### F01 — R5 shadow replay 使用未定义 _reg（严重度：P0，实际代码阻塞）

准确位置：

- src/ak_trading/v2/validation/experiments.py:561-618
- 触发点：src/ak_trading/v2/validation/experiments.py:603

实际语句为：

    ledger = ContinuousLedger(
        account, bundle.instruments, run_policy, lock_hash,
        registry_hash_for(specs), exit_registry_sha256=_reg
    )

AST 结果是 run_shadow_replay_experiment 对 _reg 只有一处读取、没有本地赋值。模块顶层也没有 _reg；同名赋值只存在于别的函数 _training_opportunity 和 _replay_fold 的局部作用域，不能被该函数捕获。因而一旦锁、训练计划和 fit 通过，执行到初始化 ledger 时必然 NameError: name '_reg' is not defined。

触发条件是调用 run_shadow_replay_experiment 且前置锁/训练输入足以越过早期 StageIncompleteError。影响是 shadow replay 在真正消费模型、payoff 和账本前崩溃，不能产生 shadow 账户结果、回放段或可复用的 shadow acceptance 证据。这不是文档或历史证据序列化问题。

最短修复方向是沿已有 _exit_pack(specs) 接口取得真实退出注册闭包：

    _reg, _es, _bs = _exit_pack(specs)

并把同一闭包的 hash 和 bindings 传进 checkpoint。不能用零 hash、手填常量或复制旧 receipt 规避。

是否阻止首个策略结果：它直接阻止 R5 ordinary shadow；按当前蓝图 R5 在正式离线 walk-forward 之后，因此单独不应被说成已经阻止所有首个 walk-forward 结果。但一旦首个结果的验收路径要求 shadow replay 或恢复段，它是硬阻塞。

必须验证：在修复候选冻结后，先用窄 SYNTHETIC checkpoint smoke 验证能越过 ledger 初始化，再运行计划规定的 shadow/recovery 反例；核对 exit_registry_sha256 与全量 registry、bindings 及 checkpoint hash 一致。本报告不实施修复。

### F02 — 即使补 _reg，checkpoint 调用仍缺必填参数且没有绑定集合（严重度：P0，实际代码阻塞）

准确位置：

- src/ak_trading/v2/validation/experiments.py:604
- 签名定义：src/ak_trading/v2/decision/ledger.py:321-327

实际调用为：

    checkpoint = ledger.checkpoint(
        (), (), dependencies, {}, pending_remaining_lots={}
    )

真实签名要求：

    checkpoint(..., *, pending_remaining_lots,
               exit_registry_sha256: str,
               exit_bindings: tuple = (), ...)

因此即使 F01 先被修掉，当前调用仍会以 TypeError 缺少 exit_registry_sha256 终止。更深一层，shadow 集成没有从 _exit_pack(specs) 传递 exit_bindings；checkpoint 缺少策略到退出绑定的完整身份，不能证明 M3C/M4 退出集成已接线。ContinuousLedger.checkpoint 在 ledger.py:336-380 会校验退出 hash，并将 bindings 写入 LedgerCheckpoint；这不是可省略的 metadata。

最短修复方向是同一处显式构造 _reg, _es, _bs，再调用：

    checkpoint = ledger.checkpoint(
        (), (), dependencies, {},
        pending_remaining_lots={},
        exit_registry_sha256=_reg,
        exit_bindings=_bs,
    )

是否阻止首个策略结果：与 F01 相同，直接阻止 shadow replay；不应被回填成“R5 已接线”。

必须验证：检查 checkpoint 解码后的退出 registry hash、所有 binding hash、策略集合和后续 run_shadow 接收的 specs 完全一致；至少加入“缺 hash、缺 binding、错误 hash”的负例。不能只验证函数返回不抛异常。

### F03 — M1 P1 新增测试已进入锁清单，消费方断言仍冻结在旧的 41 文件集合（严重度：P1，阻止 M1 候选接受）

准确位置：

- 候选测试：D:/AKF261014/M1-PUBLIC/p1-open-public-r1/source/tests/test_v2_m1_artifacts.py:542-578
- 锁清单定义：D:/AKF261014/M1-PUBLIC/p1-open-public-r1/source/src/ak_trading/v2/artifacts.py:131-142、1113-1120
- 新测试：D:/AKF261014/M1-PUBLIC/p1-open-public-r1/source/tests/test_v2_m1_p1_open_contracts.py

artifacts.py:131-142 已将 test_v2_m1_p1_open_contracts.py 放入 PUBLIC_TEST_FILES，_required_tests_map() 因而实际返回 42 个文件。可是 test_v2_m1_artifacts.py:556-568 的消费者断言仍只列旧的 9 个 M1 public 文件、旧 downstream 和 7 个 M3B 文件，并硬编码：

    assert len(required) == 41
    assert set(required) == set(public + previous_downstream + test_files)

该断言没有把新 P1 文件加入 public，所以即使只把 41 改成 42，下一条 exact-set 断言仍会失败。实际 owner 证据：

- D:/AKF261014/M1-PUBLIC/p1-open-public-r1/evidence/runs/owner-m1-r1/RESULT.json
- native exit 1
- 185 selected，184 passed，1 failed，0 errors，0 skipped
- 唯一失败为 test_public_unblock_m3b_complete_ownership_and_required_tests
- product_hard_review: NOT_RUN

同一候选的窄 P1 测试为 owner-p1-r1：118 passed/native0；M2 causality 为 owner-m2-r1：18 passed/native0。这两个局部 PASS 不能抵消 M1 聚合测试失败。

最短修复方向是由 M1 owner 在同一有效计划的 append-only revision 中，将 test_v2_m1_p1_open_contracts.py 纳入预期 public 集合，并重新核对 expected count、exact set 和 required test names。不要删掉 P1 测试、降低 required_tests，也不要改成跳过。PLAN.md §4、§5 已明确新增测试登记和严格 required map，因此这是消费方断言未随已授权新增文件同步的问题。

是否阻止首个策略结果：它阻止 M1 P1 候选形成可发布的正式 required-tests/lock 输入；在 contract lock、M1 G3/G4 仍未发布的现状下，首个正式研究结果仍不能合法出具。它本身不是策略数学错误，但会阻断集成资格。

必须验证：修复后重跑精确 owner M1 命令、逐文件 SHA-256 freeze、独立 ZCode 硬审；必须确认 42 文件 exact set 与新测试函数清单一致，并保留原 native1 证据。

### F04 — _build_snapshots 逐条重建全部可见前缀，P2 大人口验证超出原 300 秒预算（严重度：P1/P2，性能与验收阻塞；未证明内存耗尽）

准确位置：

- src/ak_trading/v2/validation/experiments.py:687-733
- D:/AKS261014/M1/snapshot-provenance-r1/owner-evidence/test_snapshot_p2.py
- D:/AKS261014/M1/snapshot-provenance-r1/owner-evidence/runs/p2-001/RESULT.json

当前 _build_snapshots 对每个 bar 追加 prefix，然后调用 build_feature_snapshot(contract, tuple(prefix), ...)。这会让每个历史点重新传入完整前缀，并在因子层执行 PIT 可见性、特征准备、内容链/seasonality 等工作。外部快照候选虽然在 contracts.py:722-840 对 FeatureSnapshot.source_bar_ids 增加了 256 宽度块和弱引用池，但 P2 测试仍对 25,679 个增长前缀逐项展开、逐项 tuple equality、逐项 element identity 和 canonical digest 校验；块存储本身不是全人口复杂度通过的证明。

真实 P2 证据是：

- population 25,679
- 原 deadline 300s
- elapsed 308.719s
- raw native exit 1
- semantic TEST_OR_RESOURCE_FAILURE
- resource abort COMMAND_DEADLINE_EXCEEDED
- peak committed bytes 121,106,432（约 115.5 MiB）
- MemoryError_observed: false
- stdout 594 bytes，stderr 0 bytes
- full_P2_population_acceptance: PARTIAL_UNVERIFIED

P2-ATTEMPT-001-DISPOSITION.json 同样明确保留该分类。因此当前证据支持“时间预算未满足”，不支持“MemoryError”或“应当无测量地调大 timeout”。

我实际运行了隔离、无写入的 SYNTHETIC 表示层探针，使用外部候选 contracts.py 直接构造增长前缀：

- N=128：构造约 0.0034s，展开约 0.00027s；
- N=256：构造约 0.0142s，展开约 0.00076s；
- N=512：构造约 0.1086s，展开约 0.0664s；
- N=1024：构造约 0.3851s，展开约 0.0978s；
- N=2048：构造约 1.4049s，展开约 0.2886s。

这些只是表示层的有界合成测量，不能外推成 25,679 人口通过；完整人口复跑和 profile 没有运行。它说明块存储本身没有立即异常，但不能证明构造、因子计算、身份校验和测试 digest 的总复杂度满足预算。

最短修复方向不是直接放宽 timeout。应先对 _build_snapshots/P2 测试分阶段计时：prefix 构造、factor snapshot、identity/hash、公开展开和回收分别记录；确认绑定小样本输出和实际 bar-ID 对象身份后，才决定是否复用增量因子状态或把验证拆成等价 bounded checks。任何算法变化都必须以字节、source IDs、PIT 和 identity 反例回归为前提。

是否阻止首个策略结果：当前 P2 是快照内存/性能支持门，不等于所有 M5 walk-forward 都已被证明不可运行；但它使该 snapshot candidate 的原人口验收未闭，若首个实验依赖该候选的正式 memory acceptance，则会阻塞。不能把 timeout 记成 PASS，也不能把 bounded probe 记成 full-population PASS。

## 已有成果可以复用，但信用范围必须保持

- P1 窄公共测试 118 passed/native0 可复用作局部类型/hash 回归输入。
- M2 causality 18 passed/native0 可复用作该 owner 运行范围证据；不能替代 M2 全节点或真实输入资格。
- M1 owner 聚合中的 184 个通过可复用为成功案例集合；该命令整体 native1，唯一 required-map 失败必须保留。
- DAILY frozen revision 010 的 owner 命令证据包括 28 passed、228 passed、62 passed、209 passed、50 passed、82 passed 等局部工程运行；READY.json 明确 product_hard_review: NOT_RUN、source freeze active、SYNTHETIC engineering only，不能写成整个 DAILY 链已硬审 PASS。
- DAILY 后续独立 reviewer 的 scoped fact 是 followup_verdict: PASS，但 relay 的旧 adapter 读取 verdict 字段，产生 native3；两个当前 follow-up 工具尝试 raw native1 也被保留。因此当前 disposition 是 SCOPED_INDEPENDENT_REVIEWER_PASS / INCOMPLETE_NOT_ACCEPTED / NOT_RELEASED，不能释放整个链。
- M3C P1 的独立 verdict 是 scoped PASS，但 G3 formal lock、G4 M4 binding literals、M2/M3A population、M5 formal run 和 later-stage fixtures 仍是 unresolved dependencies；不能升级为全节点或正式 lock PASS。

## 对蓝图和实施计划的必要修正

1. 将 run_shadow_replay_experiment 的 _reg 未定义和 checkpoint 缺 exit_registry_sha256 明确提升为 R5 代码集成 P0；同时写明必须传 exit_bindings。
2. 为 shadow 入口增加 fit 前 cheap static/contract smoke，确保 _exit_pack、ledger checkpoint 签名和 run_shadow 参数闭包在昂贵 fit 前即可被窄验证；这不能替代正式硬审。
3. 将 M1 P1 测试登记消费方断言纳入同一写集：新增 PUBLIC_TEST_FILES 后，所有 expected count/exact-set 断言必须由同一来源生成或显式同步。不能只修计数，必须修 exact set。
4. 将 P2 计划拆为“原 25,679 人口正式门”和“有界诊断门”。诊断门只能回答瓶颈假设，不能授予 full-population acceptance；原 300 秒、2 GiB memory、1 GiB output 等预算保持事实，是否调整需在阶段 profile 后裁定。
5. DAILY 计划状态保留 scoped reviewer PASS 与 work-order/relay NOT_ACCEPTED 两个独立事实；不能因字段适配或收尾把整条天然链升级为可放行。

## 首个策略结果的总体判断

当前不能声称项目已经得到首个可复现的真实 PIT 策略结果。直接原因仍包括：M1 formal contract lock 未发布、实际输入/人口与 M5 运行资格未闭、M1 聚合 required-map 失败，以及 R5 shadow 入口的两个确定性代码错误。F01/F02 主要阻塞 shadow/recovery；F03 阻塞 M1 候选资格；F04 使快照大人口验收保持 PARTIAL_UNVERIFIED。它们分别属于代码集成、测试/登记同步和性能验收，不能互相抵消。

本次结论不是产品硬审 verdict，也没有给任何节点发 PASS。下一步若获授权，应由对应原 owner 在冻结规则下修复 F01/F02 或 F03，P2 则先按现有计划补充分阶段测量；每个修复都要保留原失败证据、重新 freeze，随后交独立 ZCode 硬审。
