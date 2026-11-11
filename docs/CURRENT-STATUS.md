本周能看到什么：修复与预检集成硬审通过，全量运行中。
卡在哪：AFTER待终态；合同锁单独阻塞。
需要我做什么：确认H7输入；锁另待真实v1 manifest。

# 第四轮第一批修复与防护

范围仅 K03→K01/K02/K05→K04，完成或具体阻塞后停止。有效计划为 r3-001、H1–H8 及原 Astra 本批裁定；外审不是计划或产品验收。恢复 Astra/max 计划、ZCode 开发、另一 ZCode 独立硬审。G1 一条关键开发、一条审核、一条支撑；G2 工程可重跑；G3 单作业宽松墙钟；G4 只维护本页、原始日志压缩后放仓库外。最终仍须无条件硬审 PASS、合入 main、CI 通过、全量无新增失败/错误/收集错误。

## 外审取得与 HANDOFF §8 实测

fetch 与 ff-only 合入 raw0；外审分支 bbc85e0 已包含，取得后 HEAD `c1904354f99fbc350ad49c0e72e7f1ce5dc1207a`，仅新增外审目录15文件。五份建议已按序读完，外审文件不改。表中命令前缀为 `python -B -X utf8`，`$R4` 仅缩写 `docs/repo-audit-20261110-claude`。

| 原命令 | 原始退出 | passed/failed/errors | 实测与差异 |
|---|---:|---|---|
| `scripts/test_offline.py -q tests/test_v2_forward_paper.py` | 1 | 20/48/0 | 与外审一致 |
| `scripts/test_offline.py -q $R4/probes/test_entry_confirmation_reachability_probe.py` | 1 | 4/5/0 | 一致 |
| `scripts/test_offline.py -q $R4/probes/test_range_family_cross_timeframe_probe.py` | 1 | 4/7/0 | 一致 |
| `$R4/probes/clock_horizon_bench.py` | 0 | 非 pytest；3000组 | 值与 ClockError 全等；250日 MINUTE 本机114.074ms，外审约16ms；K10不在本批 |
| `$R4/probes/budget_feasibility.py --name RB --price <真实值> --atr <真实值> --multiplier 10 --tick 1 --margin <真实值> --fee-rate <真实值>` | NOT_RUN | N/A | H7 selected_input=null，缺正式输入和配置；不用示意价格/ATR替代 |

## K01–K05 当前阶段（截至 2026-11-11 04:56 UTC）

| 项目 | 真实成果及独立审核 | 产品合入/下一步 |
|---|---|---|
| K03 | 脚本、52项测试、真实 BEFORE JSON 已获无条件范围 PASS；真实输入审发现的 CE 分支及父域计数漏洞已由原 owner 修复复审 | 完整 AFTER 与最终对照待完成，尚未产品合入 |
| K01 | 原 b653f7e 开发会话完成组合修复；23文件冻结、独立源码/定向无条件 PASS | 待集成全量及最终验收 |
| K02 | 原 Astra 选方案甲，随组合修复获同一源码/定向 PASS | 同上；外部探针六项不兼容断言保留 |
| K05 | 真实 SYNTHETIC 自然链六交易族 OPEN、WAIT 不开；CI 已加入 Forward 与自然链；随组合获源码/定向 PASS | 待完整候选与实际远端 CI |
| K04 | 多合约计数、品种/模式/期限精确匹配及 >7日延迟问题已修，34项测试及反例获无条件范围 PASS | 原 owner 已接入真实日历 helper，35项自测/raw0；独立重跑35P/raw0，补充反例审核中；H7仍缺输入 |

组合独立重跑：M1–M4 raw0，1082 selected/ran、1079P、3原有 REAL_SHARD skip、0F/0E/0CE，墙钟1091秒；Forward68P、自然链57P、确认探针9P、第二轮重复探针9P，合计143P/raw0，墙钟709秒；现有重复K线96P包含于 M1–M4。审核者17项时钟、日历、发布、可用性与幂等反例全部通过。跨周期外部探针实际 raw1/5P6F，与绑定裁定一致，原始失败不改写。Windows时间不是 Linux CI验收；保留现有20分钟时限，远端运行待实际核验。

**原 Astra 裁定摘要及外审差异：** K01 只按最新已完成、可用、同频 bar.end 严格大于 expiry 判过期，等号有效；无可用 bar 为 WAIT0。DAILY 按 trading_day 的最后收盘计数，建立日算1；日历缺失/不足为 None、WAIT0、SETUP_CALENDAR_INSUFFICIENT，MINUTE 不改。保留重复幂等、闸门重置、真实新周期1及冻结几何。K02 选甲：非趋势结构加有效已发布中性背景为 ALIGNED，明确方向为 CONFLICT，缺失/无效为 UNKNOWN；趋势及闸门不改，压缩 BREAKOUT 同表。保留矩阵 ALIGNED 默认值仅作单测缝，不能充当自然链证据。外部跨周期探针有六项与裁定不兼容，故保留5P6F，不修改探针求全绿。14个历史M4常量不刷新；当前 BEFORE 中 M1–M4无失败，历史失败不构成豁免。K04拒绝读取holdout价格ATR，拒绝用训练天数除以最长持有期冒充回合上限。

## 完整回归与剩余前置

固定完整环境 Python3.13.15、pytest9.2.0、项目 TqSdk3.10.1，依赖检查 raw0，H8环境未改。历史对照同环境实跑 b653f7e^ 的68P/raw0与 b653f7e 的20P48F/raw1；K03准确识别48新增失败，反向48解决，JSON/raw一致；历史环境禁止与完整环境混比。精简环境只收集证据不作基线。

实际完整 BEFORE 命令为 `python -B -X utf8 scripts/test_offline.py -q --continue-on-collection-errors --basetemp C:/AKR41110/fb01`：raw1，6033 selected/ran、5699P、241F、60E、33原有skip、8收集错误文件；10域全部启动，0空域跳过/未运行/启动失败/未解析，墙钟4795.41秒。三份独立成员探针依次为 duplicate raw0/9P、confirmation raw1/4P5F、range raw1/4P7F。四成员合计6062 selected/ran、5716P、253F、60E、33skip、8CE。8个收集错误来自实际缺失的 datapipeline 源码；如实保留。外审3933ran/3405P/216F/273E/39skip/22CE来自不同分组/环境，不当作本机前后差额。

**AFTER 尚未取得，新增失败数 UNKNOWN。** 原作者已裁定：先源码定向审核，再 K04 接入，冻结 K03+组合修复+最终K04的同一候选；独立审核线执行一次完整 AFTER 兼集成回归，再加三份不改的探针作四成员对照。新失败/错误/收集错误必须为0，必要缺陷解决必须实测。现有范围 PASS 均不是最终全量产品 PASS。用户已明确允许回归触发 positive_cold_run 夹具，正式 H5 不新开；不加 skip/xfail、不减人口、不放宽门槛。新 report-bound v2.1.7 合同锁仅本地生成和核验。

K04 对 H7 的真实输出全文：**尚不存在，缺少正式输入和配置。** 两份留存记录均 selected_input=null，仅列候选；不自行选 r1/r2，不伪造价格、ATR、预检或研究结果。

## 本批收尾阻塞（2026-11-11 05:10 UTC）

K04 集成审核已无条件 PASS：union 上 35P/raw0，集成反例 11/11 PASS，28/28 依赖 pin 与 import origin 均核对。原 owner 生成本地 `devplan-v2.1.7` 锁的第一次有效调用由 ZCode 返回 native0，但内部锁构建命令返回 **raw1**，没有写锁；实际错误为 `ArtifactsError: acceptance_manifest_root_fields_must_be_exactly_['bindings','dataset_kind','files','generation','inputs','labels','schema_version','selection','splits']`。重试前的调度输入错误（把目录当普通文件 pin）在启动前返回 **raw96 / NO_LAUNCH PermissionError**，未启动模型；第二次已修正并真实执行，仍保留原始尝试。

阻塞文件 `tests/fixtures/v2/ep1_t7/acceptance-input-manifest.json` 的当前真实 schema 是 `ak_ep1_t7_acceptance_input_v1`，根字段为 `code_files、derived_identity、files、labels、not_contained、profile、recipe_sha256、schema_version、seed、synthetic`；锁 builder 要求的真实 `ak_probability_acceptance_input_manifest_v1` 九字段版本不存在于仓库或冻结 union。只找到的九字段文件在 `C:/AKH1107/bt0` 测试临时镜像中，是 `_mirror_lock_tree` 生成的 SYNTHETIC 测试夹具，不能作为产品输入。根据计划和边界，不复制、改写、伪造或放宽 lock verifier，也不把旧 T7 manifest冒充 v1。

最初按lock-before-launch把整个候选记为阻塞；该调度结论已由原Astra的本批裁定更正：四条离线命令不消费拟建外部锁，允许先运行AFTER，锁依赖单独保留。已完成并独立 PASS 的范围：K03 修复工具及真实 BEFORE 范围；组合 K01/K02/K05 23文件源码/定向范围；最终 K04 两文件集成范围。解除动作：由 M5 acceptance-input owner 提供并独立审核真实 `ak_probability_acceptance_input_manifest_v1`，或由 Astra 计划作者对该既有 lock 前置作出明确裁定后，再从原 ZCode owner 续接 lock；不得用测试临时镜像替代。


H5两条既有冷链均结束：cold进程raw0但 engineering FAIL/m5_complete=false；后续 nodes 各 raw1，33 selected/ran、23P、0F、10 setupE，M5/single/audit NOT_RUN；七个payoff窗口 NO_CANDIDATE，原独立FAIL保持。H8 profile09继续原72小时作业，尚无终态，不重启；未启动的 profile10 队列已撤销。H6/H7暂缓，支撑不等H8。

已推送 main 的状态/路由提交：e62177b（CI42927490129）、67cbe07（CI42932802169）、47278b1（CI42939755983）、ec7db7c（CI42946822804），四次CI均成功，均不冒称本批产品验收。d131a49（组合源码范围PASS/接入状态）亦已推送，CI42960085240成功。本批产品尚未合入。安全处理未收到完成通知，候选分支、输入包、锁不推远端；已接受安全源码可按计划推 main。始终 false/hold/NOT_RUN/send0/cancel0；未读写凭据、未操作服务/防火墙/可见性/DB角色/系统/UI，未开启本批外模块。原始失败及协调/审核者探针修正均留存在仓库外；不把协调诊断改写为产品通过。

**原Astra合同锁裁定（已生效，2026-11-11）：** 不替换真实T7 manifest，不授权本批M5 schema迁移；记为 `LOCAL_LOCK_BLOCKED`。四个已核定AFTER作业可立即在不变union上运行，只覆盖原执行记录的“锁先于启动”条件。源码/工具在完整四成员对照零新增失败/错误/收集错误、必要缺陷实测解决、独立无条件最终PASS后，可推main并验证CI；这不构成锁/研究包接受。后续仅锁/报告元数据操作、且源码测试夹具选择环境不变时，不要求重复全量。无需新计划审核循环。

AFTER已于2026-11-11 05:43:10 UTC实际启动：原`test_offline.py -q --continue-on-collection-errors`、固定完整环境、同10域、新fa01基目录、独立8GiB保护与72h墙钟；用户已授权回归夹具冷链。三探针终态分别raw0/9P、raw0/9P、raw1/5P6F；全量尚未终态，新增失败数仍UNKNOWN。当前main109bb86的CI42964447592成功，属于状态提交，不能代替产品验收。
