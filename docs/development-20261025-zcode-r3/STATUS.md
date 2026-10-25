# r3 修复开发批次当前状态

更新于 2026-10-25 19:23 UTC。实际开发/修复调用专用 ZCode 会话；Astra/max 负责计划和契约处置，另一独立 ZCode GLM-5.3/max 会话硬审核。当前职责权威为 PROJECT-MODEL-ROLES.md、PROJECT-WORKFLOW-RULES.md。

## 当前并行工作

| 模块 | 当前实际阶段 | 接续动作 |
| --- | --- | --- |
| T7-A 四策略合同接受 | 专用ZCode owner于19:14 UTC交付零生产改动冻结版；七单文件及完整合并命令全raw0，209通过/0失败/0跳过。另一独立ZCode于19:22:43 UTC实际启动硬审。 | 独立重跑规定人口并增加审核者反例；尚无产品PASS。通过后B消费精确接受字节，不需重复开发当前正确代码。 |
| T7-B MINUTE/DAILY夹具 | 另一ZCode owner已于18:57:09 UTC启动，独立工作区，只写三测试/支持文件和明确列出的夹具。与A不重叠。 | 立即修复DAILY正例/负例及清单，提前检查M1锁引用；最终冻结消费A已接受字节。重型冷链避开当前C05批次，保留原4h/profile预算。 |
| C05 导入隔离 | 原owner007第二份完整验证继续运行；焦点45通过，完整收集6015selected/9既有deselected，3个历史DP收集错误。 | 不重启现进程。完成后原独立reviewer必须处置曾重复运行/终止第一份及未知真实退出的限制，不能宣传干净全套通过。 |
| N8 | 原reviewer042于18:59 UTC因HTTP502/native1结束；精确trace内0工具调用、A42未创建，原11文件/F10不变。原Astra066于19:20 UTC处理这一服务中断。 | 040的REVIEW_INCOMPLETE/REVIEWER_DEFECT保留；042未消耗precheck004/final002，也无产品结论。只在原作者明确有效续接后恢复，runtime002仍NOT_RUN。 |
| DISPATCH | 原owner005于18:25 UTC交付源码，但唯一行政准备因外层目录与子进程ABSENT契约冲突失败，包装器也未完整捕获终态。 | author055已native0/receiver0完成，指出六处准备/捕获确定性问题。原ZCode owner006于19:11:37 UTC实际续接，一次修复后再执行唯一新准备002；owner005产品验证尚未运行。 |
| M5 C1 | reviewer004于17:51 UTC再次HTTP502/native1结束，正式验证阶段未执行，冻结615成员不变。 | 暂挂闲置RAM并保留全部历史扣账；恢复前重新入场，完成原affected/generation后才full100。无服务已修好或产品PASS宣称。 |
| Forward paper | helper004六文件准备完成，35静态检查与4正例/93反例通过，仅静态/SYNTHETIC信用。 | 等C05真实接受并集成，原owner绑定，原独立reviewer执行未消耗的full13-002。 |

当前五路实际调用：A独立硬审、B开发、C05验证、DISPATCH006修复四个ZCode，以及N8原Astra服务恢复处置。T7计划作者和计划意见会话均已结束，不重复计入。外部CLI并行不会显示为Codex原生子代理；真实任务和进程身份保存在各自START/TERMINAL以及本地CURRENT-RUN.json。

T7有效执行补充、一次独立意见及作者处置分别见T7-PLAN.md、T7-PLAN-ADVICE.md、T7-PLAN-DISPOSITION.md。作者已纠正意见A1：旧M3B r2确有结束后的窄范围无条件PASS；复用其三测试接受，不把旧READY当成活跃审核，也不把该PASS扩成四源接受。A已交付当前真实RB日历/规则输入，不用隔离目录缺数据制造历史skip。

## 已接受并推送的成果

| 成果 | 已接受范围及必要限制 |
| --- | --- |
| P2-7 公共导出 | 独立无条件PASS；焦点30通过、消费者267通过/1既有缺夹具skip。原owner精确集成，main3336c62；原分支8cb767b保留。 |
| P0-2 replay窄修复 | 两文件独立无条件PASS；焦点20通过、反例24/24，审核报告hash更正已接收；main3336c62。完整八文件仍191通过/14旧基线失败，不关闭完整M4或shadow。 |
| P1-6 SQL helper | 四文件独立无条件PASS，独立临时库15通过/0skip；源码ddf5c67、证据10cd59f。生产采用NOT_RUN。 |
| DP08 r3接续包 | 九文件独立无条件PASS，verifier117/117 CLEAN、38结构检查和4反例通过；e196b8b。完整DP08、NR-DP08-08、V15/live/DB/SDK仍按原范围NOT_RUN。 |
| TqSdk工具与skill | 复用独立无条件PASS，65份发布skill字节与冻结一致；5425b0a。仅离线工具安装/文档接受，项目SDK pin未改，在线功能不因本次接受升级。 |
| 免费行情补全 | 38冻结成员的明确离线路线独立无条件PASS，22文件发布清单已接受；71ee657。重算10,128条LR源数据、306,930条分钟尾窗、2,640条日线；2,100条未知trading_day保留，DB七字段独立重跑/在线SDK/全历史完整性未获得本次证明。 |

各项 ACCEPTANCE、REVIEW、VERDICT 位于本目录；原始CLI流、CSV及外部候选为LOCAL_ONLY。已有PASS只覆盖准确发布范围，不扩展到整个项目或策略有效性。

## 依赖与资源

当前新派工使用capacity040；旧活动请求固定的容量文件保持不变。M5已结束调用的2GiB闲置RAM临时让给T7-A，T7计划完成释放的2GiB给T7-B；M5恢复前必须重新预留。全部产品、UNKNOWN及历史实际占用保留。新增候选和测试输出在C盘独立worktree，总预留10GiB C及128MiB D。040核对四个已结束T7辅助/计划/意见/处置任务后，测得实际原生占用6,638,592字节；仍保留128MiB覆盖全部文件及历史WAL余量，只收回384MiB未使用的未来增长预留，删除或移动文件0。RAM/D/C底线保持。

A/B分别绑定真实owner SID、独立store、cwd和写集。准备和计划意见不算开发成果硬审，CLI native0也不算产品PASS。谁完成实施和必要验证就立即独立审核，不等待其他模块。普通可重复离线验证不新增逐次一次性审批。

交易安全边界：enable_trading=False，observer=hold，broker=NOT_RUN，真实send/cancel=0。P0-1由用户亲自执行，G治理建议不擅自实施。
