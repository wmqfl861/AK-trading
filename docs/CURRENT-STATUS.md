本周：六品种第二版已交付，K45按停止点收尾。
卡点：K45来源处理规划超72小时，R001未运行。
请你：仓库转私有、换令牌、收紧入口、开长路径。

6个品种、3个规则，2020-07-20 至 2026-09-04，每个合约固定 1 手，规则和参数没有调整。
按当天主力合约算，排除能计算的天数不足一半的组合后，18 个“品种×规则”中 8 个赚、10 个亏、0 个持平，好过一直持有同品种主力合约的有 3 个。
把各品种折成同样的名义金额、每天等权平均后相加：突破 21.38%，均线 -6.02%，回归 -12.28%，一直持有 44.40%；这不是年化收益率。
各品种的三个规则都达到至少一半主力日可计算的门槛，合并时仍按每天实际有主力合约的品种数平均。
数字没有扣换月成本，指标只用单个合约自己的历史，也没有做统计检验；它们只作为今后研究的基准线。

# 第七轮外审第四批：完成或阻塞，停止

本批只含K45a/b/c、K46、S4b；完成项已推送，阻塞项如实保留，不自动开始下一批。计划/开发=Astra/max，独立审核/辅助=Sol6.1/max；现行分工表和规则文件未改。外审意见不是产品硬审。

| 任务 | 最终状态、提交与证据 |
| --- | --- |
| K45a | 有效v2已提交/推送`d3e3908`，容量裁定§10.11已推送`e0e3a2b`，两次CI均success；原作者停止裁定§10.12随本次收尾提交。原文及此前有效章节均逐字节保留。 |
| K45b | **BLOCKED / 产品硬审NOT_ISSUED**。按§10.8/10.11/10.12停止：全候选来源往返规划超过72小时。四个工具/测试文件仅本地source-r3冻结，未合入/推送，不冒充验收。开发者44基础+4全oracle测试通过；独立审核仅核验停止事实成立。 |
| K45c | **NOT_RUN**。阶段二真实拟合、真实TRAIN预演和阶段三均未执行；R001与最终holdout未消费，没有研究评分报告或科学结论。 |
| K46 | **无条件硬审PASS，合入/推送`1bbba89`**，远端HEAD已核。CI `43568563999` success：1697通过/4跳过、345.96秒。8CSV及112报告数据行110380项比较0差异；独立四文件250P/0F，22+23反例通过。判决SHA `241864b3525435959c5fac8d24a3aa0e7871c75432bae70a659674bc377a168c`。 |
| S4b | **已运行并关闭**，随`d3e3908`推送：隔离安装967.322秒，最终raw1、5P/10F/0E；FastMCP初始化触发非夹具dotenv路径，被守卫在读取前拒绝。鉴权行为未完成验证，不再修测试/服务。 |

取得文档：`git fetch origin`、`git merge --ff-only origin/claude/repo-audit-20261114`、`git push origin main`均raw0；原HEAD `b08c444`，合入HEAD `8c55c476d3b369f35d80fa1c2f547505a6ab16b0`，仅指定外审目录新增11文件，按序读完DECISIONS/NEXT-BATCH/REVIEW。

**开工六条复现**：均用`C:/AKR41110/venv-full/Scripts/python.exe -B -X utf8 docs/repo-audit-20261114-claude/probes/<下列文件>`，各一次。不是pytest，不虚报passed/failed。原始1按探针契约保留，不能改成成功。

| 文件 | 原始退出 | 结果与本机差异 |
| --- | ---: | --- |
| k44_published_tables_recheck.py | 0 | 32467检查/0差异；旧2处build pins差异仍存在，6.294秒。 |
| minute_anchor_availability_probe.py | 1 | 60秒延迟：节内0/65、逐秒0/600；1/30/59秒均65/65可用及标签，47.479秒。暂缓N变换的条件未触发。 |
| k45_fit_cost_probe.py | 0 | 206.701秒；仅方向外推49.2–112.4分钟，全任务39.9–113小时、49–60倍，高于外审7–17小时；非真实fit耗时。 |
| direction_only_equivalence_probe.py | 0 | 84.284秒；8方向模型/2selected差异0；全任务72.1秒、方向4.7秒，预期校准异常被捕获。 |
| feature_history_scaling_probe.py | 1 | 120/120 READY；历史810→82575时顺序92.23→1342.04ms、重复90.97→5389.30ms，112.877秒，高于外审耗时。 |
| model_hash_residual_bench.py | 1 | 54.578秒；10万样本读回hash5190.8ms/直接881.3ms=5.9倍，预测69.085ms；未出现数值不一致原码2。 |

**K45停止前漏斗**：以下是元数据候选，不是可用或成熟样本。分钟唯一候选日期未保存，记NOT_CAPTURED；日线每日期最多一锚点。后续特征可用、实际请求、合法请求、成熟标签、方向样本均NOT_RUN；TEST标签保持TEST_SEALED。

| 模式/段 | 候选锚点 | 候选日期数 | 请求形状上界（非生成数） |
| --- | ---: | --- | ---: |
| MINUTE/TRAIN | 17909 | NOT_CAPTURED | 268635 |
| MINUTE/CAL | 3919 | NOT_CAPTURED | 58785 |
| MINUTE/TEST | 5911 | NOT_CAPTURED | 88665 |
| DAILY/TRAIN | 278 | 278 | 2502 |
| DAILY/CAL | 61 | 61 | 549 |
| DAILY/TEST | 92 | 92 | 828 |

停止依据：完整来源往返合成209694880逻辑字节/31.663678秒，约150.998814秒/十进制GB；单写14.659438秒。正式分钟逻辑展开1497853164240字节，外推62.826125小时；预演841867930800字节，外推35.311405小时；合计98.137530小时，加固定100%余量196.275061小时。含DAILY则98.140242/196.280485小时。它是包含不可用候选的保守规划，不是实际合法人口耗时下界或磁盘占用；尚未含其余计算，不能把未测项填0。原作者据既定停止点停止整体K45，不再测实际人口救预算、不拆日线绕行。独立停止事实核验SHA `daae66124b5c52cc417dcc389271afecb68b832eee24be3e572abc470609053a`，不构成产品PASS。

资源一次核验可行：整树预测23.89GiB，计划24GiB上限+4GiB留量；C盘新增峰值26.69GiB+4GiB留量。实测可用内存33.43GB、C50.35GB、D44.57GB；未因停止再启动此作业。8GiB仅旧监督器默认，不是研究硬限。

**K45真实耗时与失败保留**：metadata002 raw0/955.03秒；costs003 raw0/258.98秒；最终基础44P/0F、105.96秒；新I/O全8期限×4候选oracle 4P/0F、1100.85秒；生命周期合成探针raw0/3.32秒。15个已捕获工程job墙钟之和5508.66秒，含重叠/失败，不能当会话或真实阶段耗时。旧stage1-001工具raw1、SUPERVISOR_NOT_CAPTURED、child退出和墙钟UNKNOWN，生成产物0，TEST调用计数NOT_CAPTURED，日志无TEST值；不补伪结果。tests002的2个夹具失败、tests005的19个重复换行writer失败及修复后结果保留；辅助metadata001终止child15、无残留，新002保留独立身份。没有完整阶段一、二、三实测耗时。

**作者选择与差异**：r1（规则可见性仅元数据裁定）；N=5秒内存情景，原输入不改；只编排生产方向原语，src不改；半宽阈值0.02，保留正/提示/混合优先级；预演TRAIN=2023-01-03—10-31，CAL=2023-11-01—12-26，假TEST=2023-12-27—2024-02-29，40日只作工程预演，不降低正式60日门槛。A01已采纳：两对照各用生产key/SELECTED及深隔离；“候选损失”仅四算法development_score。容量修订复用既有FoldRecords/RecordView与流式来源节点，未恢复暂停src性能工程。成本工具314字节差异仅预演对象生命周期，独立AST与原timings吻合，保留旧pins不重跑。§10.12是执行原停止点，不改72小时阈值或采样。

**K46验证命令**：基准Python执行`-B -X utf8 scripts/test_offline.py -q tests/experiments/test_v1_daily_baseline_universe_v2.py tests/experiments/test_v1_daily_baseline_universe.py tests/experiments/test_v1_daily_baseline_dominant.py tests/experiments/test_v1_daily_baseline_sensitivity.py --basetemp <各自独立合成目录>`。owner最终raw0/250P/415.36秒，reviewer最终raw0/250P/303.32秒。原owner244P/1F行序夹具失败、reviewer临时路径/JUnit守卫失败均保留；未改算术预期或产品guard。aggregate/seal raw0，无新回放。报告/总表/合并表在[六品种第二版](research/v1-daily-baseline-universe.md)，旧CSV/CHECK不动。

**S4b**：`C:/AKR71114/s4b-r1/venv/Scripts/python.exe -B -X utf8 scripts/test_offline.py -q tests/test_mcp_auth.py --basetemp C:/AKR71114/s4b-r1/test-temp-02`，raw1/8.358秒、5P10F。官方索引安装raw1后同一20分钟窗口内镜像安装raw0，pip check raw0；首轮收集raw2/0项运行，补导入缺失tzdata raw0后运行15项。下列失败均因FastMCP初始化尝试非夹具dotenv，被离线守卫在实际读取前拒绝，未到鉴权断言。

| tests/test_mcp_auth.py失败节点 | 一句话原因 |
| --- | --- |
| test_healthz_open_without_token | 初始化dotenv被守卫拒绝，未到断言。 |
| test_mcp_requires_token | 初始化dotenv被守卫拒绝，未到断言。 |
| test_mcp_rejects_wrong_token | 初始化dotenv被守卫拒绝，未到断言。 |
| test_mcp_accepts_correct_token | 初始化dotenv被守卫拒绝，未到断言。 |
| test_allowlist_rejects_other_clients | 初始化dotenv被守卫拒绝，未到断言。 |
| test_allowlist_rejects_unparseable_client | 初始化dotenv被守卫拒绝，未到断言。 |
| test_allowlist_accepts_listed_client | 初始化dotenv被守卫拒绝，未到断言。 |
| test_allowlist_accepts_cidr_client | 初始化dotenv被守卫拒绝，未到断言。 |
| test_rate_limit_returns_429 | 初始化dotenv被守卫拒绝，未到断言。 |
| test_rate_limit_applies_to_unauthorized_floods | 初始化dotenv被守卫拒绝，未到断言。 |

**全量基线**：本批没有修改src产品代码，未新跑前后全量，不以定向或CI结果冒充全量比较。r8继续有效：full_suite=6370P/145F/60E/33skip/8个收集错误文件、raw1；含三个探针的四成员合计6393P/151F/60E/33skip、6637selected。已发布K46独立定向0失败、CI1697P/4skip；没有生成新全量基线或声称本批独立测得全量新增0。

**待决（全部）**：暂无DECISIONS未覆盖且确需用户决定的新事项。K45已按现行停止规则阻塞，后续是否优化/重启由下一批授权，当前不自动开始。

**用户专属未确认完成**：仓库转私有；轮换行情服务令牌并重启；收紧公网入口；Windows长路径；可选分支保护；实际资金与研究100万元是否不同（30万元以下需说明）；遗留分支处置。本批均未代做。

**边界与本地交接**：未读/打印/改.env或凭据，不改r1/r2、不新导出、不新回放、不读最终holdout；enable_trading=False、observer=hold、broker=NOT_RUN、send/cancel=0/0。原始日志压缩在仓库外C:/AKR71114。K45本地source-r3包含4个未验收文件（两研究工具及两测试），清单SHA `3bb493bf242584c8f85a2cf3a0235a0769415399dfb2874ba59d42ec29978487`；旧失败、输入、锁和审核结论均保留。仅已通过K46实现与设计/状态入库，不推本地K45冻结实现；暂停模块不恢复，所有本批执行者停止，不开下一批。
