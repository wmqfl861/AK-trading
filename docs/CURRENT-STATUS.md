本周能看到什么：确认缺陷已复现，对照工具45项通过。
卡在哪：全量基线运行中，H7未正式选定输入。
需要我做什么：安全处理完成后通知；提供H7正式输入。

# 第四轮第一批修复与防护

有效计划为r3-001、H1–H8及原Astra本批裁定；外审仅为建议。范围仅K03→K01/K02/K05→K04，全部完成或具体阻塞后停止。已恢复Astra/max计划→ZCode开发→另一ZCode独立硬审；三份规则仅移除临时Sol头。G1一条关键开发、一条审核、一条支撑；G2工程可重跑；G3单作业宽松墙钟；G4仅本页状态、日志在仓库外。完成仍须无条件硬审PASS、合入main、CI通过及全量无新增失败/错误/收集错误。

## 取得与本机实测

fetch、两次ff-only合入均raw0；外审分支bbc85e0已包含，取得后HEAD **c1904354f99fbc350ad49c0e72e7f1ce5dc1207a**，审核分支仅增加外审目录15文件。按序读完五份建议，外审文件未改。2026-11-10 23:55 UTC启动前可用RAM27.89GiB、C47.81GiB、D10.19GiB。

下表命令前缀均为`python -B -X utf8`；`$R4`表示`docs/repo-audit-20261110-claude`，仅为本表缩写。

| HANDOFF §8命令 | 原始退出 | passed/failed/errors | 结果与外审差异 |
|---|---:|---|---|
| `scripts/test_offline.py -q tests/test_v2_forward_paper.py` | 1 | 20/48/0 | 与外审一致，68项实跑 |
| `scripts/test_offline.py -q $R4/probes/test_entry_confirmation_reachability_probe.py` | 1 | 4/5/0 | 一致 |
| `scripts/test_offline.py -q $R4/probes/test_range_family_cross_timeframe_probe.py` | 1 | 4/7/0 | 一致 |
| `$R4/probes/clock_horizon_bench.py` | 0 | 非pytest，3000组 | 值和ClockError全等；250日MINUTE本机114.074ms，外审约16ms；本批不做K10 |
| `$R4/probes/budget_feasibility.py --name RB --price <真实值> --atr <真实值> --multiplier 10 --tick 1 --margin <真实值> --fee-rate <真实值>` | NOT_RUN | N/A | H7的selected_input=null，缺正式配置；未用示意价格/ATR替代 |

退出1均保留真实失败含义，不当作CLI成功或rg无匹配。日志、源快照及身份核对保存在仓库外。

## K01–K05状态

| 项目 | 当前阶段 | 产品合入/阻塞 |
|---|---|---|
| K03基线对照 | ZCode已实现脚本和测试，开发与独立重跑均raw0/45P0F0E；源码阶段独立无条件PASS | 完整baselineJSON待BEFORE终态；尚无最终PASS/产品提交 |
| K01确认可达 | 原b653f7e开发者已按原会话启动K01/K02/K05实际修复 | 源码阶段前置已满足；完整候选/全量验收仍待完成 |
| K02跨周期 | 原Astra已选方案甲，与K01同候选 | 同上；保留原矩阵默认值为单测缝，不算自然链 |
| K05真实夹具/CI | 与K01/K02同一候选、同一原开发者 | 真实六交易族OPEN/WAIT不OPEN，已启动实施 |
| K04预检 | 原ZCode支撑owner已启动独立输入校验、报告及测试 | 最终时序接入待组合修复；真实H7缺正式输入/配置 |

K03源码与定向测试已获独立无条件PASS（两冻结文件、45P重跑、真实48回归及审核者反例），这是原Astra明确允许的阶段范围，不是K03最终PASS；完整基线、候选全量对照和最终独立审核仍为合入前置。历史真实对照：b653f7e^的68P/raw0与b653f7e的20P48F/raw1；工具识别48个新增failed nodeid（compare raw1），反向48个resolved（raw0），JSON/raw对照一致，原pytest退出码未改。历史环境仅互比，禁止与完整环境混比。

**原Astra裁定摘要及外审差异：** K01按最新已完成、可用、同频bar的end严格大于expiry判过期，等号有效；无可用bar WAIT0。DAILY按trading_day的最后收盘计数，建立日算1；日历缺失/不足返回None、WAIT0，拒绝固定日数外推，MINUTE不改。保留重复K线幂等、真实新周期1、闸门重置与冻结几何。K02选甲：非趋势结构+有效中性背景ALIGNED，方向明确CONFLICT，缺失/无效UNKNOWN；趋势与入场闸门不变，压缩BREAKOUT同此表。故不可改外审range探针预期5P6F（修复前4P7F），六项相冲突外部断言仍保留，不能追求全绿。额外冻结预期须旧值/新值/原因交作者确认，14个历史M4常量不刷。K04拒绝读取测试/holdout价格ATR，拒绝以训练天数÷最长持有期冒充回合上限；不足则UNKNOWN。

## 全量对照、K04全文与既有运行

完整依赖环境Python3.13.15、pytest9.2.0、项目TqSdk3.10.1，依赖检查raw0，H8环境未改。精简环境仅收集5128项/39收集错误；完整环境仅收集6033selected/9既有deselected/8收集错误（缺datapipeline源码），均0ran，均不是执行基线。用户明确允许全量测试触发的positive_cold_run夹具，正式H5仍不新开。固定跟踪树下`python scripts/test_offline.py -q --continue-on-collection-errors --basetemp C:/AKR41110/fb01`完整BEFORE运行中，单一72h墙钟。相同完整环境另行实跑的三条不可改探针为raw0/9P、raw1/4P5F、raw1/4P7F，最终按独立成员一并纳入。

**改动前/后完整passed、failed、errors、collection_errors仍未取得；新增数UNKNOWN。** 不加skip/xfail/不稳定豁免，不放宽门槛。K04对H7的真实输出全文：**尚不存在**；两份留存均selected_input=null，仅有候选，不伪造选择或结果，不启动H7。

H5两条既有cold均结束：进程raw0但engineering FAIL/m5_complete=false；后续节点各raw1，33selected/33ran/23P/0F/10setupE，M5/single/audit NOT_RUN，独立运行FAIL保持。两条有实际fit，七个payoff窗口NO_CANDIDATE。H8 profile09继续原72h作业，尚无终态，不重启；仅撤销未启动的profile10队列。H6/H7暂缓，支撑不等H8。

已推送main：**e62177b**（路由恢复/实测）、**67cbe07**（有效裁定/实际开工状态）；offline-v2 CI分别成功42927490129、42932802169，均非产品硬审。用户未通知安全处理完成，不推候选分支/输入包/锁；新v2.1.7锁仅在规定审核后本地生成。安全边界保持false/hold/NOT_RUN/send0/cancel0；未读取、打印、修改凭据，未操作服务、防火墙、可见性、DB角色、系统或UI。无N8/DISPATCH、完整shadow、Forward采集、paper、K06/K10+或下一批。

协调诊断保留真实结果：存活流混前导JSON解析1、主树查不存在树外脚本rg2、自然退出进程监视竞态1；已按实际流格式/明确路径/终态与进程身份处理。恢复后误用预设产物路径也保留缺文件诊断（含raw1），改为先确认实际清单；交付在dispatch根、stdout为stdout.json。均非产品测试结论，不改写为通过。
