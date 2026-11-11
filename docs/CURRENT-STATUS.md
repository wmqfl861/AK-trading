本周能看到什么：确认与震荡缺陷复现，修复已重启。
卡在哪：全量基线运行中，H7未选定输入。
需要我做什么：安全处理完成后告知。

# 第四轮第一批修复与防护 — 进行中

有效计划为 r3-001 及 H1–H8 增补；外审只是建议。本页替换另一执行环境的阻塞快照，其原文保留在 c190435。当前已接回原 Windows 工作区，不再沿用“无法本机执行”的阻塞。批次仅 K03 → K01/K02/K05 → K04，全部完成或具体阻塞后停止。

最新用户指令恢复原路由：Astra/max 计划与语义裁定；ZCode 实施；另一独立 ZCode 硬审核。三份规则仅删除临时 Sol 路由头，不改其他规则和任何模型配置。G1–G4保持：关键开发一条、独立审核一条、支撑一条。完成须独立无条件 PASS、合入 main、CI通过、全量无新增失败/错误/收集错误；计划意见不设 PASS 门槛。

## 本机取得、资源与复现

`git fetch origin`、`git merge --ff-only origin/main`、`git merge --ff-only origin/claude/repo-audit-20261110` 均原始退出0。审核分支 bbc85e0 已包含于合入后 HEAD **c1904354f99fbc350ad49c0e72e7f1ce5dc1207a**；审核分支只增加外审目录下15个文件。按顺序读取五份外部建议，未修改外审文件。

2026-11-10 23:55 UTC启动前：可用内存27.89GiB、C盘47.81GiB、D盘10.19GiB。项目Python为既有3.13虚拟环境。下列命令统一前缀 `python -B -X utf8`，均在本机实际执行；日志压缩保存在仓库外。

| HANDOFF §8 | 命令 | 原始退出 | passed / failed / errors | 实测结论 |
| --- | --- | --- | --- | --- |
| 1 | `scripts/test_offline.py -q tests/test_v2_forward_paper.py` | 1 | 20 / 48 / 0 | 与外审一致，68项均实际运行 |
| 2 | `scripts/test_offline.py -q docs/repo-audit-20261110-claude/probes/test_entry_confirmation_reachability_probe.py` | 1 | 4 / 5 / 0 | 与外审一致 |
| 3 | `scripts/test_offline.py -q docs/repo-audit-20261110-claude/probes/test_range_family_cross_timeframe_probe.py` | 1 | 4 / 7 / 0 | 与外审一致 |
| 4 | `docs/repo-audit-20261110-claude/probes/clock_horizon_bench.py` | 0 | 非pytest；3000组等价比较 | 全部值及ClockError原因一致；250日MINUTE本机114.074ms，高于外审约16ms；本批不做K10 |
| 5 | `docs/repo-audit-20261110-claude/probes/budget_feasibility.py --name RB --price <真实值> --atr <真实值> --multiplier 10 --tick 1 --margin <真实值> --fee-rate <真实值>` | NOT_RUN | N/A | 原H7元数据selected_input=null，未发现正式选定配置；不以候选或示意参数替代 |

前三项退出1均为真实测试失败，不是CLI调用失败，不改写退出码。时钟末行为 `All sampled results equal the product implementation (values and ClockError reasons).`；本机性能差异如上，不照抄外审倍数。

## 本批任务

| 顺序 | 任务 | 阶段、负责人及当前前置 | 产品合入 |
| --- | --- | --- | --- |
| 1 | K03全量基线与对照 | 原Astra已处置独立意见，ZCode支撑owner已启动实际脚本/测试开发；完整before并行运行，历史Forward48新增实证已就绪 | 无 |
| 2 | K01确认可达性 | 五项缺陷已复现；Astra裁定按可见完成bar判断过期、DAILY按交易日最终收盘。原b653f7e ZCode owner已按原会话/cwd/库完成准备，新目标为独立当前源码快照，旧脏工作区不改 | 无 |
| 3 | K02跨周期语义 | Astra选甲；中性背景ALIGNED、方向明确CONFLICT、缺失UNKNOWN；压缩态BREAKOUT同此规则，与K01同候选 | 无 |
| 随行 | K05真实夹具/自然链CI | 随K01/K02实施；六交易族OPEN与WAIT不OPEN须真实链验证 | 无 |
| 4 | K04可达性预检 | 等K03后走支撑；合成验收可先做。真实H7调用缺正式输入/配置，不能给伪结果 | 无 |

原Astra已发布本批有效增补并处置独立ZCode意见（F1–F3/F5–F7/F9–F10采纳，F4/F8部分采纳）；K03实际开发已启动，不等计划PASS。K03源码/定向测试经独立审核且真实48项回归正确识别后，立即启动原owner的K01/K02/K05，不等长BEFORE结束；完整基线、完整对照与最终独立验收仍是任何产品接受/合入的必要条件，不把阶段性源码审核当最终PASS。K01与外建议差异：DAILY日历不足返回无setup并WAIT，不按固定日数外推。K02保留旧矩阵regime默认参数，因不可改外审重复探针依赖此接口；它仅为单测缝，不算自然链证据。K02不可改外审探针按裁定预计5P/6F，待修复后实跑，不追求错误语义全绿。K04不读测试/holdout窗口价格ATR，不把最长持有期除法冒充回合上限。额外冻结预期仍须旧值/新值/原因逐条由作者确认。独立硬审范围明确包括Forward68项、M3B重复K线测试及第二轮重复K线探针。新锁仅允许一次本地生成，不推送。

## 全量对照与K04全文

路由/实测状态提交 **e62177b** 已推送main，offline-v2 CI成功（运行42927490129），这不是产品硬审。历史Forward实际二分：144e6ec（b653f7e^）raw0/68P0F0E，b653f7e raw1/20P48F0E。

完整依赖环境Python3.13.15、pytest9.2.0、项目声明TqSdk3.10.1，独立目录创建，依赖检查raw0；H8原环境未改。精简环境收集为5128项/39收集错误；完整依赖后6033selected/9既有deselected/8收集错误（缺datapipeline源码），均0ran，仅收集不是基线。用户随后明确允许全量回归触发的positive_cold_run夹具，正式H5作业仍不新开；完整before已按固定跟踪树、完整环境、继续收集错误和短basetemp启动，单一72h墙钟。三条外审探针在相同环境另行实跑为raw0/9P、raw1/4P5F、raw1/4P7F，将完整纳入基线，不遗漏。

改动前/后全量 passed、failed、errors、collection_errors：**尚未取得**，新增数UNKNOWN，不能宣称0。K03尚未实施，修复前不会跳过它直接改产品；外审不稳定用例豁免未采纳，不加skip/xfail、不放宽门槛。

K04对H7实际输出全文：**尚不存在**。现有H7留存只列r1/r2候选，明确selected_input=null；未伪造价格、ATR或输入选择，不启动H7研究。

## 既有运行与边界

- H5两条既有cold均已结束：进程退出0，但engineering FAIL/m5_complete=false；两条节点作业各退出1、33selected/33ran/23passed/0failed/10setup errors，后续M5/single/audit NOT_RUN。独立运行结论FAIL；既有源码工具PASS不等于运行PASS。两条均有实际fit，七个payoff窗口NO_CANDIDATE。保留能力，不启动新cold。
- H8现有profile09仍在原72小时作业中；不重启、不等待它才做支撑。仅已核对身份的待运行profile10自动队列被撤销，profile10未启动，profile09未受控制。H6/H7暂缓。
- 用户尚未通知安全处理完成：不推候选分支、输入包或锁文件，不公开私有会话信息。仓库外日志保留，仓库仅摘要。
- 未读取、打印或修改.env及凭据，未操作服务/防火墙/可见性/数据库角色/系统/客户端。enable_trading=False、observer=hold、broker=NOT_RUN，本会话真实send/cancel=0/0。
- 不续N8/DISPATCH，不做shadow/Forward采集/paper、不做K06或K10+，不自动下一批。

协调诊断保留3次原始非零：存活流混有watchdog前导JSON导致解析1、仅主树查找本机树外脚本rg2、自然结束进程造成监视竞态1。分别改为终态/实际流格式核对、先验证明确路径、先读终态并容忍已退出进程；它们不是产品测试结论，不改写成测试通过。
