# r3 修复开发批次当前状态

更新于 2026-10-25 17:39 UTC。用户已恢复开发并指定实际开发/修复统一调用 ZCode；当前权威为 PROJECT-MODEL-ROLES.md 和 PROJECT-WORKFLOW-RULES.md。Astra/max 负责计划与契约澄清，独立 ZCode GLM-5.3/max 负责成果硬审。开发和审核分别使用固定独立会话库，不恢复旧 Sol/Claude 开发。

## 当前并行工作

| 模块 | 当前真实阶段 | 下一步 |
| --- | --- | --- |
| C05 导入隔离 | 原 ZCode owner007 的第二份完整执行正在运行。source006 焦点45项全通过；独立 reviewer004 已确认 RF1 碰撞修复。完整收集6015 selected/9既有 deselected，10域全派发，3个历史DP收集错误，unresolved0。owner误判第一份已死而重复启动，随后终止第一份；该事实及其未知退出码保留，未获验证信用。 | 保留当前进程，完成原验证后立即交原独立 reviewer；审核必须处置重复运行窗口及证据限制，不刷新历史pin、不删失败。 |
| DISPATCH | 原 owner003因上游502/native1结束，未写产品。原 ZCode owner004于17:00 UTC沿原SID/store续接author053，已写出六个源码文件的受控注册、owner绑定与新版本校验，尚未交付运行结果。 | 接收真实交付后，Root执行原计划未消耗的 owner005，再立即独立硬审。 |
| M5 C1 | 原reviewer004于17:37 UTC实际启动。此前003再次502/native1，Root逐工具核实正式阶段未启动、644输入未变；基于后续实际peer源码进展、已完成probe和38分钟间隔，进行一次有限原会话续接，无服务已修好宣称。 | 直接完成原affected与generation；真实通过后才绑定唯一full100。禁止额外探索性fit、盲重启或以自测替代硬审。 |
| N8 | 原 Astra author064已完成有效处置；同一独立 reviewer的transport040于17:14 UTC实际启动，仍运行，保持原11文件和F10。 | 等真实终态；仅在064全部条件满足时允许一次041，不允许042循环，不重放已消耗验证。 |
| Forward paper | 原 ZCode owner已完成 helper004的6文件源码准备，35静态检查及4正例/93反例通过；只属静态/SYNTHETIC信用。 | 等C05真实接受并集成，原owner立即完成无环最终绑定，再交原独立reviewer执行唯一full13-002。 |
| T7 接受版本核对 | 默认配置 OpenCode辅助任务于17:25 UTC实际启动；专门追溯64pins中的12个当前漂移，包括新接受的replay变更。无产品写权限。 | 交付逐文件原版本、当前来源、实际独立接受范围及缺失项，让原计划方据此明确最小修复。 |

C05、DISPATCH、N8、M5是当前四个实际运行的 ZCode 调用，另有一个OpenCode辅助调用，共五路。旧M5 reviewer003与Astra064均已结束，不重复计数。Forward源码已停写等待真实依赖，没有重复派writer。当前调用与工具句柄保存在本地 `D:/Maintenance/aktr-development-20261025/CURRENT-RUN.json`，终态及产物才是完成依据。

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

当前新派工采用 PARALLEL-CAPACITY-FACTS-20261025-036，在035上新增2GiB RAM/256MiB磁盘的有限只读辅助槽；旧活动请求的035固定输入未改。已结束行情审核释放1GiB RAM；Forward因等待C05而临时释放2GiB RAM，真正接续前须按原合同重新预留。全部历史磁盘和UNKNOWN扣账保留；内存/磁盘底线不降低。C05仍在运行，不因空缓冲输出或一次进程枚举阴性重复启动。

T7接受基线、ordinary shadow的R19→R20→R21以及R1历史输入仍有具体证据/契约/数据依赖。仅对实际消费者阻断，不让它们阻塞互不干扰的已授权工作。未为了增加数量派重复writer或新功能。

旧forward审核已由原作者003合法重切，377源文件和旧审核保留于原快照；旧full13-001已消耗，49完整通过/50启动、无终态和产品判决，不重放。新full13-002仍未消耗，保持原完整人口及2100秒/2GiB/1GiB预算。

交易安全边界不变：enable_trading=False，observer=hold，broker=NOT_RUN，真实send/cancel=0。P0-1由用户亲自执行，G治理建议及大范围架构/配置变更不纳入本次派工。
