# r3 修复开发批次当前状态

更新于 2026-10-25 21:28 UTC。实际开发/修复使用专用 ZCode 会话；Astra/max 负责原计划及契约处置；另一独立 ZCode GLM-5.3/max 会话做成果硬审核。当前权威为 PROJECT-MODEL-ROLES.md、PROJECT-WORKFLOW-RULES.md。

## 当前并行工作

| 模块 | 当前实际阶段 | 接续动作与限制 |
| --- | --- | --- |
| T7-A 四策略合同接受 | 独立 reviewer003 已于20:26 UTC结束，真实无条件FAIL F01：TREND/PULLBACK重复同bar会多计确认。原209项通过没有覆盖这一缺陷。原owner修复002及003均HTTP502、零工具；实际观察到其他ZCode新工具操作后，004于21:25 UTC接续原开发者。 | 按既有合同修复同bar幂等性、补新bar/重启回归、验证新字节并立即复审。保留原SID/store/cwd，不开新计划或换模型；不把调用启动计为缺陷已修复。 |
| T7-B MINUTE/DAILY夹具 | 004已真实修改support源码（587新增/104删除），随后HTTP502。daily探针真实raw3，69组件insufficient_inner_split；原结果和工具记录已保存。005于21:13 UTC接续原开发者。 | 修SYNTHETIC夹具构造，不降样本/成熟/purge/embargo/DAILY20门槛，不改生产模型。最终清单等待A修复后的实际接受及合法R0→L0→R1→L1→R2；重型批次门未开前不启动探索或冷fit。 |
| C05 导入隔离 | 两文件006冻结；owner完整命令raw2：6015选中/运行，5754通过、171失败、30跳过、60错误、3个收集错误。原reviewer005会话HTTP502结束，但独立batch原supervisor及full子进程仍运行；focused raw0、collect-full raw2已闭合。 | 继续原完整进程，不因模型退出重复启动。随后完成规定置换和collector差异检查，由原reviewer接收实际结果；保留owner旧重叠限制，不能无证据把全部失败称为基线。 |
| DISPATCH | author057已实际native0/receiver0，明确原路径FileRef与同字节保存副本的路径不同，导致整FileRef误比较。原owner008于21:12 UTC接续并已有实际工具操作。 | 只修原身份比较与保存映射验证，旧receipt和当前validator分别保持；完整纯函数预检后才一次preparation004。preparation003已消费、部分产物保留；owner005产品验证仍NOT_RUN。 |
| N8 | author068已完成原修复处置，Root按完整产物与真实命令接收，并保留receiver1的已恢复SSE提示。原reviewer046于21:09 UTC启动，已创建新A46并写审核工具。 | 旧A42只读，045不合格。按068先有限构造检查，再完整precheck005及条件final002，合计60s、资源限额不变；不授权047/P/runtime002。F02仍FAIL，F01仍OPEN/INSUFFICIENT。 |
| M5 C1 | 原reviewer005于20:53 UTC再次HTTP502/native1，零工具；657既有输入重核一致、615源冻结保持。新实际服务进展后，006于21:26 UTC接续原独立审核者。 | capacity044重新预留原2GiB RAM；直接执行未消耗的affected一次及条件generation一次，不重复探索fit。两项真实PASS后才绑定full100。 |
| Forward paper | helper004六文件准备完成，35静态检查与4正例/93反例通过，仅静态/SYNTHETIC信用。 | 等C05实际接受并集成，再绑定原owner与原独立reviewer的未消耗full13-002。 |

当前五路ZCode实际并行：T7-A开发、T7-B开发、DISPATCH修复、N8独立审核、M5独立审核；另有C05原独立完整测试进程继续。不同模块使用独立会话库和写集；这些外部CLI不会显示为Codex原生子代理。真实身份和终态以各任务START/TERMINAL及本地CURRENT-RUN.json为准。已经结束的会话不计入活跃数。

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

前瞻调度现使用capacity044，既有冻结请求不改。RAM总预留20,401,094,656字节、D扣账18,790,481,920字节，保留6GiB RAM/4GiB D底线及C盘10GiB预留。043恢复N8并暂停M5空闲RAM；044在真实余量足够时恢复M5原2GiB，N8继续保留，Forward空闲槽仍暂停。所有UNKNOWN、历史实际最大占用和失败保留；不删除文件或降低底线凑并发。B冷拟合仍须满足真实输入和重型批次调度条件。

完成实施及必要验证即独立硬审，FAIL回原ZCode开发者。计划意见、封存、CLI native0和开发者自测均不替代产品PASS。普通可重复离线验证不另加一次性审批；已有特殊一次授权按实际消费状态处理。

交易安全：enable_trading=False，observer=hold，broker=NOT_RUN，真实send/cancel=0。P0-1仍由用户亲自操作；G治理建议不自行实施。
