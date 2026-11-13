# K25：r6 基线失败分诊（2026-11-13 批）

r6 最终完整 full member 的 **31 个问题文件**已逐文件列入下表，合计 **145 failed、60 errors、8 个收集失败文件**，与入库基线一致。按本批给定的研究路径定义，其中 **8 个文件、110F/16E/0CE** 在研究路径上。主要可证成的问题是未随冻结源树供给的工件和源码绑定漂移；本表不把这些前置失败当作各被测业务已运行失败。

唯一基准为 [full-suite-windows-20261113-r6.json](../../tests/baselines/full-suite-windows-20261113-r6.json)，SHA-256 `ea2122c5b7b762c45a5552046ca07cb853038c61beb57e93e6f1100a209d2a87`，与原 `full-after-baseline.json` 字节相同。只计 `capture_id=r6-sol-final-full09-01` / `kind=full_suite`：**6055 passed / 145 failed / 60 errors / 33 skipped / 6293 ran，原生退出 1**；normal 域 139F/60E，task-installation 域另 6F，另外八个域各有一个收集失败文件。CE 单列，既不是 8 个运行错误，也不加入 ran。

三个外审 probe member 单独保留：duplicate=9P/0F/raw0，confirmation=9P/0F/raw0，range=5P/6F/raw1；这 29 个 ran 和 6F **均不混入本表**。本次未重新执行它们或任何 pytest。

表中 F/E/CE 来自 full member 的逐项 nodeid；213 个问题记录已与该真实失败流的异常段逐一匹配，未匹配数 0。`S:L数字` 定位原 `C:/AKH1107/r6-sol-final-full09-01/stdout.txt`，SHA-256 `c4cb610e3b99ac1d8b8a26e0f55fdd13bf8179221c4e865ffac7a06e6271c87b`；每行给代表性错误及位置，完整 nodeid/分组留仓库外。

“缺仓库外文件”表示所需工件未随本次冻结源树供给，不是全机不存在。对十一项具体缺件做了文件元数据和原 source.zip 成员核对，未读行情载荷。结构查询使用 codebase-memory，37 条路径 coverage 均 metadata_changed，experiments.py 另有 1566—1568 部分解析缺口；已读取实际 r6 源码对应段，37 个源码文件均与原封存 source.zip 字节一致，不把当前图谱当作旧运行的完整证明。

下表只作分诊，建议交下一轮裁定。分类保留多因和 UNDETERMINED；未改测试、未加 skip/xfail、未刷新 pins 或实施建议。研究路径严格取 `tests/experiments/`、`tests/risk_semantics/`、`tests/test_v2_m5*`、`tests/test_v2_forward*`，不是对其他文件的重要性判断。表的生成依据为 T01 `triage.py extract`（最终 extraction-002）、T02 `triage.py context`，二者原生退出均 **0**。

| 文件 | F | E | CE | 原因与代表性报错 | 类别 | 研究路径 | 建议处置（下一轮） | 原始流定位 |
| --- | ---: | ---: | ---: | --- | --- | --- | --- | --- |
| [tests/benchmarks/test_compare.py](../../tests/benchmarks/test_compare.py) | 1 | 0 | 0 | core smoke run 已过，compare 返回 2、期望 0；原失败流未展开 compare stderr；所需 baseline-policy.json 在原源树/快照均缺。compare 更细根因 UNDETERMINED。 | 缺仓库外文件；细因 UNDETERMINED | 否 | 补齐 policy 工件，再定位 compare rc=2；不改期望值。 | S:L2097 |
| [tests/benchmarks/test_datapipeline_workload.py](../../tests/benchmarks/test_datapipeline_workload.py) | 2 | 0 | 0 | 2F 同为 SOURCE_HASH_MISMATCH：workloads/datapipeline.py 与 source-map 记录的哈希及大小不符（12760→69576 B）；其中 smoke 子命令 rc=2。 | 钉住的哈希过期 | 否 | 下一轮按有效源码重新审定 benchmark source-map 绑定。 | S:L2138；S:L2159 |
| [tests/benchmarks/test_harness.py](../../tests/benchmarks/test_harness.py) | 2 | 0 | 0 | 2F 的 formal 子进程均报 ModuleNotFoundError: No module named 'tools'，返回 3 而期望 0；这是项目模块的子进程导入路径问题，非缺第三方包。具体路径传播归属 UNDETERMINED。 | 依赖机器状态（子进程 import 路径） | 否 | 定位 formal 子进程 cwd/PYTHONPATH 传播后修复；保留原 rc=3。 | S:L2189；S:L2222 |
| [tests/benchmarks/test_schema_mirror.py](../../tests/benchmarks/test_schema_mirror.py) | 0 | 12 | 0 | 12E 在模块 fixture 读取 result-schema.json 时统一抛 ContractError: result schema mirror not found。 | 缺仓库外文件 | 否 | 补供已定义的 schema mirror；不替换为临时空 schema。 | S:L110 |
| [tests/benchmarks/test_v2_workload.py](../../tests/benchmarks/test_v2_workload.py) | 2 | 0 | 0 | 2F 同为 v2 source-map 漂移；代表 SOURCE_HASH_MISMATCH: src/ak_trading/v2/artifacts.py，smoke 子命令 rc=2。 | 钉住的哈希过期 | 否 | 下一轮审定 v2 benchmark 源码清单与绑定。 | S:L2238；S:L2263 |
| [tests/datapipeline/test_boundaries.py](../../tests/datapipeline/test_boundaries.py) | 0 | 0 | 1 | 收集时 FileNotFoundError: DP01/r1/dp01-freeze-manifest.json；未进入测试用例。 | 缺仓库外文件 | 否 | 补供 DP01 冻结依赖后恢复收集。 | S:L8737 |
| [tests/datapipeline/test_calendar.py](../../tests/datapipeline/test_calendar.py) | 0 | 0 | 1 | 收集时 FileNotFoundError: DP01/r1/dp01-freeze-manifest.json；未进入测试用例。 | 缺仓库外文件 | 否 | 补供 DP01 冻结依赖后恢复收集。 | S:L8769 |
| [tests/datapipeline/test_collector.py](../../tests/datapipeline/test_collector.py) | 0 | 0 | 1 | 收集时 FileNotFoundError: DP01/r1/dp01-freeze-manifest.json；未进入测试用例。 | 缺仓库外文件 | 否 | 补供 DP01 冻结依赖后恢复收集。 | S:L8801 |
| [tests/datapipeline/test_end_to_end.py](../../tests/datapipeline/test_end_to_end.py) | 0 | 0 | 1 | 收集时 FileNotFoundError: DP01/r1/dp01-freeze-manifest.json；未进入测试用例。 | 缺仓库外文件 | 否 | 补供 DP01 冻结依赖后恢复收集。 | S:L8839 |
| [tests/datapipeline/test_monitoring.py](../../tests/datapipeline/test_monitoring.py) | 0 | 0 | 1 | 收集先遇 cannot import name 'contracts'，回退加载又抛 E_DP06_FROZEN_MANIFEST_MISSING；终点仍是缺 DP01 freeze manifest，不把该 ImportError 当成缺 pip 包。 | 缺仓库外文件 | 否 | 补供 DP01 契约/冻结依赖并核对加载边界。 | S:L8853；S:L8864 |
| [tests/datapipeline/test_release.py](../../tests/datapipeline/test_release.py) | 0 | 0 | 1 | 收集时 FileNotFoundError: DP01/r1/dp01-freeze-manifest.json；未进入测试用例。 | 缺仓库外文件 | 否 | 补供 DP01 冻结依赖后恢复收集。 | S:L8902 |
| [tests/datapipeline/test_reporting.py](../../tests/datapipeline/test_reporting.py) | 0 | 0 | 1 | 收集先遇 cannot import name 'contracts'，随后 E_DP06_FROZEN_MANIFEST_MISSING：缺 DP01 freeze manifest；不是第三方依赖诊断。 | 缺仓库外文件 | 否 | 补供 DP01 契约/冻结依赖并核对加载边界。 | S:L8916；S:L8927 |
| [tests/datapipeline/test_scheduler.py](../../tests/datapipeline/test_scheduler.py) | 0 | 0 | 1 | 收集时 FileNotFoundError: DP01/r1/dp01-freeze-manifest.json；未进入测试用例。 | 缺仓库外文件 | 否 | 补供 DP01 冻结依赖后恢复收集。 | S:L8958 |
| [tests/datapipeline/test_task_installation.py](../../tests/datapipeline/test_task_installation.py) | 6 | 0 | 0 | 6F 均在原生命令启动边界被 OfflineViolation 拒绝：only guarded Python or registered native fixtures are allowed；任务安装语义尚未执行。 | 依赖机器状态（离线原生夹具许可） | 否 | 明确所需原生测试夹具并接入既有许可机制；保留离线守卫。 | S:L9001 |
| [tests/docs_reference/test_static_reference.py](../../tests/docs_reference/test_static_reference.py) | 1 | 8 | 0 | 8E 的公共 build fixture 返回 surface_missing / surface file not found；另 1F 在首次 build 就 rc=2，尚未测到重复 build-id 拒绝；均缺 api-reference-config/surface.json。 | 缺仓库外文件 | 否 | 补供 surface 配置后再核对文档生成与重复 ID 测试。 | S:L332；S:L336；S:L2276 |
| [tests/experiments/test_historical_daily_chronological_transfer.py](../../tests/experiments/test_historical_daily_chronological_transfer.py) | 63 | 7 | 0 | 61F+7E 直接报 effective plan/input inventory pin mismatch: PLAN-F01-20261015-r2.md；另 2F 因同一前置错误与预期业务报错不同。原快照未含四份计划/清单，首个分支为缺文件，不能笼统算哈希漂移。 | 缺仓库外文件 | 是 | 由 K40-B 计划裁定四份工件的供给/绑定方式，再恢复这些测试；保留当前拒绝。 | S:L524；S:L2302；S:L2326；S:L2363 |
| [tests/risk_semantics/test_risk_semantics.py](../../tests/risk_semantics/test_risk_semantics.py) | 27 | 0 | 0 | 19F 直接 unknown_producer_version: validation/experiments.py 的预期/实际哈希不同；另 8F 因先遇到此版本拒绝而未返回各自预期的篡改/关联等错误。 | 钉住的哈希过期 | 是 | 修复版本化 producer 证据与有效源码的绑定；不删除版本检查。 | S:L6420；S:L6536；S:L6688；S:L6706；S:L6724；S:L6756；S:L6828 |
| [tests/test_acquisition_postgres.py](../../tests/test_acquisition_postgres.py) | 0 | 7 | 0 | 7E 的模块 fixture 已找到 initdb，但原生启动被 OfflineViolation 拒绝；未到数据库建表/外键断言，不能记作数据库产品错误或缺 PostgreSQL。 | 依赖机器状态（离线原生夹具许可） | 否 | 明确真实 PG 测试环境及原生夹具许可；不改数据库契约或守卫。 | S:L716；S:L730；S:L739 |
| [tests/test_factor_library.py](../../tests/test_factor_library.py) | 1 | 17 | 0 | 1F 为原始因子库 zip 的 FileNotFoundError；17E 为 FactorLibraryError: registered catalog file is missing or outside its directory，日志不能进一步区分目录越界与缺 catalog。 | 缺仓库外文件（17E 细因未定） | 否 | 补供已登记 catalog/原始包并核对目录约束；不生成替代数据。 | S:L1019；S:L7223 |
| [tests/test_infiny_connection_liveness.py](../../tests/test_infiny_connection_liveness.py) | 1 | 0 | 0 | tail cadence broken：healthy 仅 3 次（要求至少 4），间隔约 0.690/1.365s（上限 1.2s）。真实墙钟/并发调度相关；单次失败不能区分机器负载、测试时序与产品缺陷，根因 UNDETERMINED。 | 依赖机器状态 / UNDETERMINED | 否 | 下一轮定位调度与负载来源；不直接放宽时间阈值。 | S:L7273；S:L7274；S:L7275 |
| [tests/test_infiny_native_acceptance.py](../../tests/test_infiny_native_acceptance.py) | 6 | 0 | 0 | 5F：no overall-revision freeze manifest found；另 1F：infiny-arming-mechanism-20260918-r1/decision.md 的 FileNotFoundError。两组都是验收工件未供给，不据此判断原生执行已坏。 | 缺仓库外文件 | 否 | 补供对应修订的冻结工件与 decision；是否归档旧验收由下一轮定。 | S:L7300；S:L7334 |
| [tests/test_main_minute_refill.py](../../tests/test_main_minute_refill.py) | 1 | 0 | 0 | 读取 data/downloads/kqm_symbols.txt 抛 symbols file must be a readable UTF-8 file up to 64 KiB；该文件在原快照/留存树均缺，未到 81-symbol/CLI 后续断言。 | 缺仓库外文件 | 否 | 补供明确的符号范围夹具，或下一轮裁定去除对本机下载产物的依赖。 | S:L7437；S:L7456 |
| [tests/test_mainline_mcp_boundaries.py](../../tests/test_mainline_mcp_boundaries.py) | 1 | 0 | 0 | 断言模块必须在 D:/Projects/AK-trading/src，实际导入为本次授权冻结树 C:/AKR61112/repair09/src；is_relative_to=False 是硬编码检出位置不适配。 | 测试过期（固定检出路径） | 否 | 让导入边界断言绑定本次明确源树，保留源树隔离检查。 | S:L7462；S:L7465；S:L7467 |
| [tests/test_mcp_auth.py](../../tests/test_mcp_auth.py) | 10 | 0 | 0 | 10F 均为 ModuleNotFoundError: No module named 'fastmcp'，在服务导入阶段停止；不能据此断言 token、限流等行为失败。 | 缺可选依赖 | 否 | 补 MCP 可选依赖；是否显式记录缺依赖而跳过由下一轮定。 | S:L7499 |
| [tests/test_offline_launcher.py](../../tests/test_offline_launcher.py) | 1 | 0 | 0 | 子 calendar 因缺 DP01 manifest 收集失败，真实子退出 2；外层测试固定断言退出 1，故 assert 2==1。底层缺工件与外层固定预期均保留，未见吞掉失败的证据。 | 缺仓库外文件；测试预期待复核 | 否 | 补依赖，并让下一轮裁定该测试应验证的退出场景；保留原 2。 | S:L7800；S:L7812；S:L7814 |
| [tests/test_v2_forward_paper_feed.py](../../tests/test_v2_forward_paper_feed.py) | 4 | 0 | 0 | 4F 均在开始处 assert supervisor_script.is_file()；缺 owner-evidence/r1-001/verify_job_004.py，尚未启动 fake-SDK 监督器。 | 缺仓库外文件 | 是 | 补供被测试的监督器工件，或将已授权工具作为可检出的测试依赖。 | S:L7840；S:L7842 |
| [tests/test_v2_m5_acquisition_feed.py](../../tests/test_v2_m5_acquisition_feed.py) | 1 | 0 | 0 | FileNotFoundError: P02/r1/feed-input-contract.json；在读取草案合同时停止，尚未验证 prepare-only gate。 | 缺仓库外文件 | 是 | 补供 P02 合同工件；保留 prepare-only 安全边界。 | S:L7955 |
| [tests/test_v2_m5_cli.py](../../tests/test_v2_m5_cli.py) | 1 | 0 | 0 | chunk-configs/m5-r4 下 glob 为空，期望 chunk01—chunk06 六份 skeleton：assert [] == [...]；不是模型拟合或 CLI 执行报错。 | 缺仓库外文件 | 是 | 补供六份已定义 skeleton，或下一轮裁定归档旧 series 测试。 | S:L7968；S:L7971 |
| [tests/test_v2_m5_experiments.py](../../tests/test_v2_m5_experiments.py) | 10 | 0 | 0 | 6F 直接缺 contract-lock.p3-r1.json；2F 期望 PAYOFF_TRAINING_DEPENDENCY_MISSING 却先得到 M1_CONTRACT_LOCK_NOT_PUBLISHED；1F 直接同一 M1 gate；另 1F 缺旧 inert starter（is_file=False）。 | 缺仓库外文件 | 是 | 补供对应 contract lock 和旧 starter 工件，或裁定旧 series 归档；不改依赖检查顺序。 | S:L8014；S:L8234；S:L8268；S:L8343 |
| [tests/test_v2_m5_payoff_integration.py](../../tests/test_v2_m5_payoff_integration.py) | 3 | 4 | 0 | 2F+4E 直接 M1_CONTRACT_LOCK_NOT_PUBLISHED；另 1F 期望 TRAIN_OR_CALIBRATION_INSUFFICIENT_NO_MODEL_ARTIFACT，实际仍是同一缺锁前置错误，未到拟合/模型不足分支。 | 缺仓库外文件 | 是 | 补供锁及对应有效夹具，再验证 payoff 分支；不把当前错误改成模型失败。 | S:L1618；S:L8368；S:L8421 |
| [tests/test_v2_m5_t7_evidence.py](../../tests/test_v2_m5_t7_evidence.py) | 1 | 5 | 0 | 1F：验收输入 code_drift（含 scripts/research_probability_v2.py 等）；另 5E：公共 cold-chain fixture 先遇 M1_CONTRACT_LOCK_NOT_PUBLISHED。哈希漂移与缺锁两因分开。 | 钉住的哈希过期（1F）；缺仓库外文件（5E） | 是 | 按已排期 J18 处理重冻结，并补齐锁依赖；本批不重跑冷链或刷新 pins。 | S:L1838；S:L8485 |
| **合计：31 文件** | **145** | **60** | **8** | **与 r6 full member 逐项对账** | — | **研究路径 8 文件：110F/16E/0CE** | 本批只分诊 | T01/T02 raw 0 |

61F+7E 的 chronology 前置异常与另 2F 的报错不符属于同一缺计划链，不能只看异常名中的 pin mismatch 就算成“哈希过期”。同样，risk_semantics 的 19 个直接版本拒绝和 8 个预期错误不符共计 27F，不是另有 8 项未计入的失败。T7 的 1F code_drift 与 5E 缺锁则确为两组。本表保留这些区别，没有为每文件强配单一根因。

节拍失败只证明当次未达阈值，不能据单次运行排除产品缺陷或断定机器抖动；benchmark compare 的原始 stderr 未在失败报告展开，缺 policy 事实之外的细因仍未定。当前没有补跑来消除这些不确定性。

生成命令（cwd=`D:/Projects/AK-trading`；固定 Python 3.13.15）：`C:/AKR41110/venv-full/Scripts/python.exe -B -X utf8 C:/AKH71113/k25/render.py`，原生退出 **0**。上游 T01/T02 同解释器及 `-B -X utf8`，末项分别为 `C:/AKH71113/k25/triage.py extract` / `context`，原生退出各 **0**；读取/计数检查 **31 文件、213/213 异常匹配、145F/60E/8CE**。这些是报告作业结果，**没有新的 pytest passed/failed**，旧 full raw1 保持失败事实。

最初 extraction-001 仅匹配 85/213，未作为结论；原 raw0/部分匹配记录保留，最终使用修正后的 extraction-002。所有工具命令、原退出码、输入 pins、分组记录和终止后日志封存在 `C:/AKH71113/k25/`；仓库仅保留本汇总表，不复制 raw log、输入数据、锁或验收工件。所用 discovery 的原生退出均保留；仅 optional rg raw1 且空 stderr 才能作 NO_MATCH，本次查找授权段落的 rg raw0。

未访问凭据/.env、行情载荷、最终 holdout、网络、DB 或服务，未读取或修改模型配置。保持 `enable_trading=False / observer=hold / broker=NOT_RUN / real send/cancel=0/0`。
