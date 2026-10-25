# r3 修复开发批次当前状态

更新于 2026-10-25 23:17 UTC。开发与产品修复由专用 ZCode 会话执行，原 Astra/max 作者负责现有计划或契约澄清，另一独立 ZCode GLM-5.3/max 会话负责硬审核。以 PROJECT-MODEL-ROLES.md、PROJECT-WORKFLOW-RULES.md 的2026-10-25规则为准。

## 实际并行工作

| 模块 | 当前实际阶段 | 下一步及边界 |
| --- | --- | --- |
| Forward paper | C05已独立PASS并集成，原owner004已恢复；完成真实源闭包、节点人口、最终helper和送审就绪材料。 | 沿author004立即衔接原独立full13-002；该正式运行尚未消耗。helper004此前35静态、4正例/93反例通过，仅静态/SYNTHETIC信用。 |
| T7-B MINUTE/DAILY夹具 | 007完成DAILY容量构造及真实测试源码，N3150/f0.92/V225/E1；2个manifest节点通过。008已接收A/C05十文件精确接受字节并继续开发。 | 修正候选正向路径仍调用_bypass_lock的既有计划违例，冻结真实F，再走独立R0与M1生成L0。未启动无有效锁的昂贵冷链；200样本/30日期块/DAILY20/完整组合义务不降低。 |
| T7-M1契约锁 | 新绑定专用ZCode owner001，在B开发同时准备实际合法L0/L1路径和已有生成器/校验器的精确接口。 | 没有已证实的M1生产源码缺口；真实R0后才生成L0，R1后生成L1，再独立R2。不是新开发计划。 |
| M5 C1 | 原reviewer007发生HTTP502/native1；36工具中完成3处审核探针修改，未执行正式affected/generation。008已续接原审核者。 | 先原affected一次，再条件generation一次；实际两项PASS后才Root绑定full100。原615源冻结和全部失败保留。 |
| DISPATCH | 原author058已处置具体schema/key/failed_history错误；原ZCode owner011已实际启动最小修复。 | 准备004为第九次已消费失败，raw2/zerochild；修正真正入口并完成纯检查后仅准备005一次。ownerverification005仍NOT_RUN；35测试/624反例和完整原预算保留。 |
| N8 | reviewer047 native0但正式FAIL，原因在审核器：构造001期望错误、构造002的连续时钟把编写时间计入60s。两构造位置已消费，零候选谓词执行。 | 原Astra author070处理同故障的有限时钟/失败发布澄清；未新启动048。precheck005/final002未消耗。F02FAIL、F01OPEN/INSUFFICIENT、PABSENT、runtime002NOT_RUN。 |

本次核对有5路ZCode及1路Astra实际活跃，均以START绑定PID和完整创建时间核对。不同模块使用独立store和写集，同库最多一调用。外部CLI不显示在Codex原生子代理列表。后续终态以本地CURRENT-RUN、每次TERMINAL与实际产物为准。

## 已独立接受并推送

| 成果 | 接受范围与限制 |
| --- | --- |
| T7-A重复bar确认修复 | 独立无条件PASS；TREND/PULLBACK重复同bar不再推进确认。原八条命令raw0，合并212通过/0skip，11独立修复反例通过。main d46274c。只覆盖准确策略合同范围，不代表整个策略有效性或T7完成。 |
| C05离线导入隔离 | 两文件独立无条件PASS并集成main3238ff5；focused45通过。完整运行raw2，6015运行、5754通过、171失败、30跳过、60错误及3收集错误全部保留；正反序失败节点一致。窄PASS不代表完整项目测试已绿。 |
| P2-7公共导出 | 独立PASS；焦点30、消费者267通过/1既有缺夹具skip，main3336c62。 |
| P0-2 replay窄修复 | 两文件独立PASS；焦点20、反例24/24。完整八文件仍191通过/14旧基线失败，未关闭完整M4或shadow。 |
| P1-6 SQL helper | 四文件独立PASS，临时库15通过/0skip；生产采用NOT_RUN。 |
| DP08 r3接续包 | 九文件独立PASS，117/117 CLEAN、38结构检查及4反例；完整DP08/V15/live/DB/SDK未升级。 |
| TqSdk工具与skill | 独立PASS及65发布文件相符；仅离线工具/文档，在线接受未升级。 |
| 免费行情补全 | 明确离线路线38成员独立PASS、22发布文件接受。重算10,128条LR、306,930条分钟尾窗、2,640条日线；2,100未知trading_day保留，在线SDK/全历史完整性未证明。 |

最新已推送产品提交3238ff5，远端github.com/wmqfl861/AK-trading。各项ACCEPTANCE/REVIEW/VERDICT位于本目录；外部候选/原始运行流为LOCAL_ONLY。历史FAIL/UNKNOWN不被后来PASS改写。

## 调度和剩余边界

capacity049前瞻调度：RAM预留19,327,352,832字节，D扣账18,924,699,648字节；保持6GiB RAM/4GiB D底线和C盘10GiB预留/4GiB底线。完成C05的RAM转回Forward；完成A的2GiB中1GiB分配M1，其余释放空闲RAM。所有历史磁盘、UNKNOWN和最大占用保留。B冷链避开其他重型批次；M5当前只安排原affected/条件generation。

ZCode仍间歇出现服务端HTTP502，尚无根治证明。分别记录CLI原生退出、工具失败、测试结果和产品判决。按原SID/store/cwd有界续接，完成验证即独立硬审；不重复已完成的大批测试，不为计划取得PASS增加回合。

交易保持enable_trading=False、observer=hold、broker=NOT_RUN、真实send/cancel=0。P0-1仍仅给用户操作步骤；G治理建议不自行实施。
