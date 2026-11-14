本周：修订预测检验设计，完成六品种报告第二版。
卡点：设计已定，进入工具开发及报告复算。
请你：仓库转私有、换令牌、收紧入口、开长路径。

# 第七轮外审第四批：执行中

本批仅 K45a、K45b、K45c、K46、S4b，全部完成或阻塞后停止，不自动开始下一批。外审是建议与用户采纳的决定，不是有效开发计划或产品硬审。计划/开发使用 Astra/max，独立审核/辅助使用 Sol6.1/max，依据 PROJECT-MODEL-ROLES.md 顶部现行表；规则文件本批不改。

**取得文档**：`git fetch origin`、`git merge --ff-only origin/claude/repo-audit-20261114`、`git push origin main` 均原始退出0；原HEAD `b08c444c35ab14656a68b5b970db6e99c43fc443`，合入/已推送HEAD `8c55c476d3b369f35d80fa1c2f547505a6ab16b0`。逐项验证仅新增指定外审目录11文件。已按顺序完整读 DECISIONS、NEXT-BATCH、REVIEW。

**开工资源**：空闲内存约33.1GiB，C盘47.573GiB，D盘42.089GiB。上一批开发/审核已结束。原始日志压缩后留仓库外 `C:/AKR71114/`。

| 任务 | 当前阶段、负责人和依赖 |
| --- | --- |
| K45a | 原 Astra 作者已发布有效v2，并采纳唯一独立意见A01；原文38719字节保持不变。固定r1、N=5、研究编排方向拟合、半宽0.02、TRAIN内固定预演；不改src。修订随本次提交入main，不再设计划PASS门。 |
| K45b | 独立 Astra 开发者已完成只读准备，K45a入库后立即实施；限两个研究工具及两个合成测试文件，Root同步CI。阶段一/二不查看TEST标签、价格变动或其统计，硬审PASS前不启动R001。 |
| K45c | 尚未开始；仅在 K45b 独立硬审无条件PASS后，执行一次K45-R001。 |
| K46 | 唯一独立计划意见已完成，无追加建议；原作者发布有效稿后立即交原Astra开发者实施。不新回放，独立复算排在K45b硬审前。 |
| S4b | 已完成并关闭：隔离安装967.322秒，正式15项运行raw1，5通过/10失败/0错误；均在FastMCP初始化时被离线凭据守卫拦截，未到鉴权断言。不继续安装、重跑或修测试/服务。 |

**六条开工复现**：由 Sol 辅助者从仓库根使用 `C:/AKR41110/venv-full/Scripts/python.exe -B -X utf8` 串行执行下列外审原探针，记录原生退出与压缩双流。原始退出1可表示探针发现问题，不改写为成功；非pytest不虚报passed/failed。

| 命令（均在 docs/repo-audit-20261114-claude/probes/） | 原始退出 | 实测 |
| --- | --- | --- |
| k44_published_tables_recheck.py | 0 | 32467项检查/0不一致，6.294秒；旧r1/r2工具与测试2项build pin差异再次确认。 |
| minute_anchor_availability_probe.py | 1 | Z23复现，47.479秒；60秒延迟的节内65候选/0可用、600逐秒时刻0可用；1/30/59秒各65/65可用及标签。暂缓条件未触发。 |
| k45_fit_cost_probe.py | 0 | 206.701秒；方向49.2–112.4分钟，全任务39.9–113.0小时/约49–60倍。合成缩量外推，非真实拟合；高于外审全任务7–17小时。 |
| direction_only_equivalence_probe.py | 0 | 84.284秒；2单元×4候选=8方向模型、2选中，参数/损失/标记/温度差异0；全任务120模型72.1秒，方向路径12模型4.7秒。 |
| feature_history_scaling_probe.py | 1 | Z28复现，112.877秒；120候选全READY，历史810→82575根时顺序92.23→1342.04ms、重复90.97→5389.30ms；高于外审耗时。 |
| model_hash_residual_bench.py | 1 | Z26/Z27复现，54.578秒；读回10万样本预测69.085ms，hash5190.8ms/直接881.3ms约5.9倍；收益/行情参考第二hash697.5/103.07ms。无数值不一致(native非2)。 |

**S4b命令及失败清单**：`C:/AKR71114/s4b-r1/venv/Scripts/python.exe -B -X utf8 scripts/test_offline.py -q tests/test_mcp_auth.py --basetemp C:/AKR71114/s4b-r1/test-temp-02`，raw1、8.358秒、5 passed/10 failed。官方索引安装raw1后，在同一20分钟窗口用允许的镜像安装raw0、pip check raw0；首次收集raw2（缺tzdata，0项运行），补齐该导入依赖raw0后才执行上述15项。原始记录均保留在仓库外。以下节点均属`tests/test_mcp_auth.py`，逐项原因相同：FastMCP→pydantic-settings尝试非夹具dotenv路径，被守卫在实际读取之前拒绝；真实凭据未读，鉴权行为未完成验证。

| 失败节点 | 原因 |
| --- | --- |
| test_healthz_open_without_token | 初始化触发受禁dotenv读取，未到断言。 |
| test_mcp_requires_token | 初始化触发受禁dotenv读取，未到断言。 |
| test_mcp_rejects_wrong_token | 初始化触发受禁dotenv读取，未到断言。 |
| test_mcp_accepts_correct_token | 初始化触发受禁dotenv读取，未到断言。 |
| test_allowlist_rejects_other_clients | 初始化触发受禁dotenv读取，未到断言。 |
| test_allowlist_rejects_unparseable_client | 初始化触发受禁dotenv读取，未到断言。 |
| test_allowlist_accepts_listed_client | 初始化触发受禁dotenv读取，未到断言。 |
| test_allowlist_accepts_cidr_client | 初始化触发受禁dotenv读取，未到断言。 |
| test_rate_limit_returns_429 | 初始化触发受禁dotenv读取，未到断言。 |
| test_rate_limit_applies_to_unauthorized_floods | 初始化触发受禁dotenv读取，未到断言。 |

**基线与改动**：当前无产品代码改动，K45有效计划也不改src。本批入口为已接受r8；其中完整full_suite为6370通过/145失败/60错误/33跳过/8收集错误文件（raw1），连同三个探针的四成员合计6393通过/151失败/60错误/33跳过、6637selected。两种口径分开记录。不以工具定向测试冒充新全量；如后续确需改产品，须先由作者裁定并执行前后全量、逐例比较新增失败为0。

**待决**：暂无DECISIONS未覆盖且确需用户决定的新事项。

**用户专属未确认完成**：仓库转私有；轮换行情服务令牌并重启；收紧公网入口；Windows长路径；可选分支保护；实际资金与研究100万元是否不同；遗留分支处置。

**边界**：不看/打印/改动.env或凭据，不改r1/r2，不导新行情、不新回放，不读最终holdout2026-09-05及以后；阶段三前不查看TEST标签或价格变动。enable_trading=False、observer=hold、broker=NOT_RUN、真实send/cancel=0/0。暂停工程及N8/DISPATCH、shadow/Forward/paper均不恢复。公开仓库只推授权工具、测试、配置、汇总与精简CHECK；不推输入、逐笔、候选分支、合同锁、验收包；旧CHECK不删除。
