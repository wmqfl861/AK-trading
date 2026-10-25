# r3 修复开发批次当前状态

更新于 2026-10-25 20:17 UTC。实际开发/修复使用专用 ZCode 会话；Astra/max 负责原计划及契约处置；另一独立 ZCode GLM-5.3/max 会话做成果硬审核。当前权威为 PROJECT-MODEL-ROLES.md、PROJECT-WORKFLOW-RULES.md。

## 当前并行工作

| 模块 | 当前实际阶段 | 接续动作与限制 |
| --- | --- | --- |
| T7-A 四策略合同接受 | owner 原七条单文件及合并命令全 raw0，209通过/0失败/0跳过。独立 reviewer 的七条单文件和恢复后的合并命令也已 raw0；尚无最终产品判决。原 reviewer003 于20:10 UTC续接。 | 完成独立反例与 helper 审查，发布实际判决；已闭合的八条独立验证不因服务中断重跑。当前源码零生产改动。 |
| T7-B MINUTE/DAILY夹具 | 原 owner 已写出具体 M1-GAP：旧 F 格式与当前 verifier 不符、测试引用不存在的锁路径。004于20:10 UTC继续实际实现；20:15采样时 tracked 源码尚无差异。 | 只改原三测试/支持文件和明确夹具。最终清单绑定等待A实际接受；不能把现存旧锁直接称为最终新锁，保留R0→L0→R1→L1→R2。 |
| C05 导入隔离 | 两文件006冻结；owner完整命令结束：6015选中/运行，5754通过、171失败、30跳过、60错误、3个收集错误，raw2。原独立reviewer005于19:59 UTC启动。 | 收齐原始结果后直接硬审，无需为补摘要再调用owner。独立执行规定验证，检查正反序多出的一项collector失败，并处理第一份运行退出未知/曾重叠的真实限制。 |
| DISPATCH | 准备002因成员契约和未定义sha失败，属于第七次准备失败且已消耗。author056明确实际纯函数预检和竞态/捕获修复；原owner007于20:08 UTC实施，已有源码进度记录。 | 先修完整缺陷并实际预检，再执行唯一新准备003。成功后Root才执行原owner005完整验证与独立硬审。产品验证尚NOT_RUN。 |
| N8 | 原reviewer043因HTTP502结束，并保留两次编辑错误；未执行precheck004/final002。原author067已native0/receiver0；reviewer044于20:14 UTC实际启动。 | 保留A42旧草稿，只在harness-044修复审核工具。原25s/35s正式人口不变；仅满足精确条件时可续045，不自动重试。F02仍FAIL，F01仍OPEN/INSUFFICIENT，runtime002未运行。 |
| M5 C1 | reviewer004的HTTP502终态及615成员冻结保留；正式验证阶段尚未执行。 | 原2GiB RAM已临时给A，当前余量不足以再加2GiB；重新满足资源条件后恢复原reviewer。affected/generation真实PASS后才full100。 |
| Forward paper | helper004六文件准备完成，35静态检查与4正例/93反例通过，仅静态/SYNTHETIC信用。 | 等C05实际接受并集成，再绑定原owner与原独立reviewer的未消耗full13-002。 |

本次采样五路实际外部调用：A独立硬审、B开发、C05独立硬审、DISPATCH修复、N8独立硬审。不同模块使用独立会话库/写集；这些外部CLI不会显示为Codex原生子代理。真实身份和终态以各任务START/TERMINAL及本地CURRENT-RUN.json为准。已经结束的计划作者不计入活跃数。

ZCode服务仍有重复HTTP502/native1，尚未证明已修复。保留每次实际进度，续接原会话，不把CLI退出与测试结果/产品判决混为一谈。当前没有新增无条件产品PASS；不能用已通过的单项验证或诊断文档增加产品完成率。

T7有效计划、一次计划意见及作者处置见T7-PLAN.md、T7-PLAN-ADVICE.md、T7-PLAN-DISPOSITION.md。旧M3B r2的三测试窄范围PASS复用，但不扩为四生产源接受。真实RB日历/规则已交付A，原三个测试均实际执行，无人为缺输入skip。

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

仍使用capacity040，既有冻结请求不改。RAM总预留18,253,611,008字节、D扣账18,790,481,920字节，保留6GiB RAM/4GiB D底线及C盘10GiB预留。所有UNKNOWN、历史实际最大占用和失败保留；不删除文件或降低底线凑并发。M5恢复需要重新预留2GiB；不同模块可做轻量开发，但重型冷链须避免资源与共享staging冲突。

完成实施及必要验证即独立硬审，FAIL回原ZCode开发者。计划意见、封存、CLI native0和开发者自测均不替代产品PASS。普通可重复离线验证不另加一次性审批；已有特殊一次授权按实际消费状态处理。

交易安全：enable_trading=False，observer=hold，broker=NOT_RUN，真实send/cancel=0。P0-1仍由用户亲自操作；G治理建议不自行实施。
