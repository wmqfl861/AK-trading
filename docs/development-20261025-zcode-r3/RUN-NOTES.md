# 本批运行问题与避免方式

- P1-6首次调用及原会话一次续接均因模型服务HTTP502失败，raw native1、stdout为空，trace分别为`1a0523da-81b8-4132-90ee-30146fc752aa`和`12e34cda-174d-49ed-893e-4260c66d0919`。两次输入均未漂移、候选无代码改动。此事是外部模型请求失败，不是产品测试失败或rg无匹配。原字节日志留在本地，发布摘要不带headers、cookie、请求体或凭据。保留同一SID/store/cwd，先确认前次终止，再有限恢复；重复服务错误应暂列阻塞，不能无限重试、换模型或改配置掩盖。
- replay计划意见调用原生0并产生建议，但作者在调用期间将计划从9273改为9311字节，监督器正确返回98。原失败保留；原Astra作者已对最终文档逐项确认建议适用性并发布处置，没有假报输入哈希一致，也没有把咨询意见升级成产品信用。以后调用前使用作者已停止写入的版本或独立固定快照；不要以“文件已出现”代替“此版本已固定”。这不增加计划PASS门槛。
- 外部Astra workspace-write运行曾被操作系统拒绝读取用户目录中的skill。保留原生1；按已提供的图谱覆盖事实和当前源码作限定后备读取，未改权限/模型配置。以后把任务必要的非敏感skill指引随工作单提供到可读工作区，避免反复探测同一拒绝路径。

后续恢复依据：P2-7硬审与C05计划意见分别于09:24和09:25 UTC真实native0完成；SQL据此启动一次原会话、源码优先的小步骤恢复（p16-owner-003），并非在无新证据时无限重试。其结果仍须单独验证，不能由其他任务成功推定。

- 后续真实进展：`p16-owner-003`源码阶段native0，12项离线通过/3项隔离测试待执行。`p16-owner-004`完成新建独立PostgreSQL集群、直接pytest的15项全通过、角色新建/重复执行和自身集群关闭后，CLI再次因HTTP502/native1终止。原guard入口为12 passed/3 failed，直接隔离入口为15 passed/raw0；保留两者，不削弱guard。Root核对终止、准确四文件写集与实际结果后发布机械冻结收据并立即独立硬审；没有把CLI失败改记成功，也没有重跑测试只为补一份交付文档。
- `c05-owner-001`在HTTP502后native1，trace=`4bc63956-5ca0-4d4e-8da0-413b9827ecbc`，当时尚无产品源码变更。核实前次终止、输入未变、原库及SID后，基于同时间其他调用的实际进展，仅作一次分步恢复`c05-owner-002`，先交真实源码与焦点测试，后续原验收不削减；源码改动已出现。模型/provider配置未改。
- `replay-owner-002`沿原SID恢复后真实native0；完整八文件raw1的14项失败与干净基线相同，均属旧退出绑定hash常量对照。开发者保留失败并冻结两文件提交，独立审核已启动。后续不可通过刷新常量、删除测试或将CLI native0等同产品PASS来掩盖基线问题。

- `replay-hard-review-002`与`p16-hard-review-002`均由原独立审核者续接，真实native0、输入未变，并给出对应窄写集无条件PASS。复用了前次已经完成且输入未变的焦点20项、SQL15项，不因调用恢复重复成功测试；各次HTTP502/native1原件保留。replay独立完整回归仍是191 passed/14旧基线failed，SQL生产采用仍为NOT_RUN。
- replay审核者手写报告时把测试文件SHA-256的一位抄错。Root在提交前逐项比较实际字节、原REQUEST、FREEZE与VERDICT，发现差异；原审核者以`replay-review-pin-correction-003`追加机器生成的更正，native0、输入未变，确认是报告抄写错误，原报告不覆盖、产品不重跑。以后报告中的pins直接由权威记录及实际文件计算生成，禁止手抄长hash。
- Root提交replay前的一次JSON读取（工具记录`d5a18b`）因`Path.read_text()`未指定编码而使用Windows默认GBK，遇UTF-8标点触发`UnicodeDecodeError`，raw1。失败发生在任何git add/commit/push之前，没有部分提交。后继命令显式`encoding="utf-8-sig"`并以`python -B -X utf8 -`运行，完成原冻结字节核对、提交和推送，raw0（`472cc4`，提交`f979e88`）。以后本批所有Python协调命令均启用`-X utf8`，文本读取明确编码；保留原失败，不用忽略解码错误掩盖数据问题。
- 原forward执行作者003明确允许结束旧的未完成审核并重切范围。Root已确认旧watchdog/CLI/helper/worker及已知后代均不存在；最初按原深目录复制快照时，Windows长路径导致`FileNotFoundError`、raw1（`eb8445`），当时没有最终快照清单、closeout或源码写入。保留该部分副本，后继采用`D:/AKSN261025/FWD-R7-002/files/<sha256>.bin`短路径及完整原路径映射，实际核对377源码和458份源/审核成员，raw0（`45a5b1`）。旧规则的三份字节从Git b98ef21精确恢复到快照，现行规则不回滚。以后Windows快照采用短目标或经过验证的长路径API，不机械叠加深前缀；复制失败不宣告冻结解除。
- `forward-recovery-owner-001`于2026-10-25 12:42:57 UTC因服务HTTP502终止，native1、stdout为空、输入未变，新helper尚未产生。实际trace为`31699e39-ce85-40bd-9aed-7388eb80163f`。Root只读本任务独立库的session ID元数据，确认原owner为`sess_43fc3d2f-dc0c-4e1b-858d-28ffe727c1e2`，未查询消息、模型配置或凭据；身份记录在本地ROOT-FAILURE-IDENTITY.json。暂不连续重提相同调用；等待其他真实调用的成功进展后，才有界续接原SID/store/cwd。C05本地完整验证继续，不能把此服务失败当成helper产品FAIL。

- `c05-reviewer-prepare-002`在36.8分钟间隔后的原会话有界续接真实native0，输入未变；这只是源码预检，未运行测试或签发产品verdict。它指出域名扁平化碰撞和无参数规划失败误报空选择两个既有计划缺陷。Root核对具体进程创建身份后受控停止旧验证004的19个进程，监督器保留真实raw15；该完整命令只记STARTED_INCOMPLETE，不记通过，也不把它解释为产品超时。原40项焦点和6010项收集结果保留；同一ZCode owner005修复原两文件。之后先用原独立审核者的廉价反例，再执行原完整验证，不删验收要求。
- 两路OpenCode只读辅助任务在首次JSON事件前被自动快照的全仓`git ls-files --others`拖住，Root仅停止准确绑定的CLI及其git后代，保留raw15，并让watchdog正常捕获/释放。新attempt改在无Git的小工作目录启动，以绝对路径读项目的有限原计划；已核实项目级OpenCode配置文件不存在，未读改模型、provider或持久配置，未加选择器。以后辅助只读核查不要把巨大历史证据目录作为自动快照工作树。

- M5 C1审核准备001于2026-10-25 14:53:46 UTC实际native1、stdout0，615冻结成员及所有输入未变；尚无review root或产品执行。精确trace581937ee-b40a-40f3-8573-5ae9039dabcf记录上游Bad Gateway/HTTP502；原流保留本地，不输出headers/cookie。此事不是测试失败或NO_MATCH。其他多路同窗出现502，暂停盲目增开；等待真实完成证据后，仅允许有界原SID/store/cwd续接，不换模型或配置。DP08随后真实native0完成，不等于服务已彻底恢复。

- DP08 r3首次Git发布检查raw2，仅因已审冻结文件release-verification-summary.md末尾原有空行。未改已审字节、未提交或推送。后继对其余精确写集保持原严格whitespace检查，仅对此一个冻结文档关闭blank-at-eof检查，并核index blob与接受hash一致；该格式例外不豁免产品测试、漂移或其他格式错误。

- Root接续时误将RUN-NOTES.md定位到Maintenance根，Get-Content原生1；实际文件位于docs/development-20261025-zcode-r3。只读失败没有修改产品。后继先枚举已知小目录确认路径再读取，raw0；今后从已发布路径取得输入，对可选发现先检查存在性，不把推测路径当必需文件批量读取。

- N8 transport039首次Root启动前算术检查`cd3cc2` raw1：current033保留已结束OpenCode槽的1GiB磁盘扣账，但Root重算只加活跃lane，漏了这笔保留量；在admission文件、watchdog或CLI启动前即停止，CLI/验证消耗均0。完整旧REQUEST、ROOT-LAUNCH和失败说明以无损ZIP保存并逐字节回验，计入author063已有失败增长额度；仅修正尚未派发的Root控制文件，产品和旧038未改。后继从033固定的032原lane程序化取保留量，先验总和再执行，真实039已START。以后资源重算必须包含保留历史扣账，不能把活跃lane之和当总额，也不能为消除断言删掉保留量。

- 行情交付发布预检`f202eb` raw1发生于提交前：Root错误要求所有22个发布文件都出现在Git staged diff，但它们已被跟踪且字节未变，真实差异仅为新增审核记录和Root状态。后继同时核对“实际staged差异不越精确allowlist”和“全部发布文件的index blob等于已审字节”，复用已有跟踪文件，不造空修改、不更改冻结字节。

- M5 hard-review002于16:25 UTC native1，精确trace `6fbca89a-9f89-4436-937f-039f34019fba`：18个模型请求、82次模型内部重试、16个工具，存在HTTP502/429和socket中断；输入不变，无正式affected/generation启动。Root按精确SID/trace只读工具记录核消费，不读取配置或推理消息。审核者一条错误签名探针产生TypeError，被`2>&1 | head`掩盖为shell0，故明确记审核者失败、不计产品验证；后续普通leaf观察也不计正式阶段。原探针留快照，原SID在17.7分钟间隔后仅作一次有界003续接。今后验证不得用管道尾命令的退出码代表Python/测试结果。
- Root准备M5续接的`88aea3` raw1来自真实内存余量低于033完整预留+6GiB；发生在新目录、REQUEST或CLI创建前，启动/验证消耗均0。034仅回收已终止且已接受的行情审核1GiB RAM；035临时回收等待C05的已停写Forward2GiB RAM。磁盘与UNKNOWN全部保留，未来Forward需原合同重新预留。资源前置失败不能归为模型或产品失败，也不能通过降低底线处理。
- Root可选WMI进程枚举`bf31fa` raw1返回`Call cancelled`和不完整一行；未将其当成完整进程清单。后续用原START绑定的准确PID及psutil子树核对C05实际测试仍在计算，不再全局WMI枚举，不终止无关进程。
- N8 transport039于16:45 UTC再次native1，trace `60655964-478f-4bab-8933-8030a8904687`，8模型请求/49内部重试，上游502；实际4条只读Bash及1条Todo。Root连同038原5条Bash逐命令核对，两项正式验证均未调用，所有失败和输入保持。063只授一次039且禁止自动040，故交原Astra作者064处理既有恢复边界，未擅自再启动040或降低验证标准。

- C05 owner007在约16:47 UTC依据一次不可靠的CIM/进程枚举阴性，误判完整测试已停止，启动第二份。Root此前按准确PID观察到第一份仍计算；owner于16:57 UTC发现第一份PID139612及子142388仍在，执行taskkill终止旧树。重复运行窗口约10分钟，原第一份无真实捕获终态、无验证信用；其后写入的exit.txt=1不能等同原进程实际退出码。owner称两份都在normal域、尚未碰DP staging，但该说法不足以独立证明完全无干扰，必须交原reviewer处置。第二份当前PID136012/create_time1792947105.6197352继续，不再次启动。避免方式：保留原Popen/系统进程句柄和创建身份；一次枚举阴性、空stdout或壳结束不证明测试结束；确认真实终态前不得重新启动，同一套验证不允许重叠。
- DISPATCH owner003于16:47 UTC因HTTP502/native1结束，trace `94a298c7-3d76-4f12-a861-2ba0c787a259`；实际工具无写入，九文件及文件身份与preimages一致。原owner004同SID/store/cwd于17:00 UTC有界续接，已产生实际源码改动；已完成分析复用，不把服务错误转为产品FAIL或旧owner005重试。
- M5 reviewer003于16:58 UTC再次HTTP502/native1，trace `a8c8fdc3-5e67-4278-b43b-5feeb789fba2`，644输入未变。三次工具调用含自有probe改动及又一次普通leaf探索，均不替代正式affected/generation。当前暂列服务阻塞，不无依据重提第四次或换模型。
- N8 author064实际native0/receiver0后发布有限040及条件041恢复；040于17:14 UTC启动。Root准备时`08d630` raw1为猜错admission文件名，`027dfe` raw1为多层生成Python命令的换行语法错误，均发生于CLI启动前，消费0；后继从实际路径与已验证模板取值，完整compile后才发布/执行，原失败保留。这些是Root准备错误，不归为ZCode服务失败。

- M5 reviewer004的恢复依据：Root再次按exactSID/trace核对003的三条工具，正式两阶段均未调用，644旧输入全部重核匹配，旧CLI及supervisor均不存在；9248字节reviewer自有probe另存pre004，不回滚为7624字节。DISPATCH随后于17:23:52 UTC产生实际新源码进展；间隔2294秒后只授一次原SID/store/cwd续接，于17:37 UTC真实启动，采用含T7辅助的当前036完整资源总额。服务仍可能失败，不自动循环、换模型或重放已完成的验证；新工单直接要求原affected/generation，不重复探索性fit或源码阅读。


2026-10-25 19:00 UTC：T7一次计划意见完成，原Astra disposition002 native0/receiver0，A/B立即实际派给两个ZCode开发会话。旧r2确有窄PASS，意见A1误判已由原作者纠正；真实RB输入已交付A，禁止故意缺数据沿用skip。当前5路调用，详见STATUS。

Root N8启动准备错误保留：2f93fc REQUEST超过原尺寸上限；e0b62d沿交接摘要使用了缺失-001后缀的记录名；377d80误用capacity036.new_lane（真实字段additional_lane）。均发生在CLI前，启动数0。eae297/400cdc/b0a2a9是归档容量预检失败、发布数0。原REQUEST/ROOT-LAUNCH精确字节已保存在042/ROOT-PREFLIGHT-001-PRESERVED.zip；同文件身份更新、保留实际分配最大值，显式收紧未用的launch上限并在原131072控制总额内容纳预映像。41184b才是真正审核CLI启动，句柄62334。避免办法：枚举真实文件名、读取实际记录schema后生成；全部序列化/编译/容量校验在发布和启动前完成，不把摘要字段当权威。不宣称这些Root错误为产品缺陷或服务错误。

Root只读7e703b误将ZCode stdout.json写为Astra式stdout.ndjson，raw1保留，随后按实际目录识别stdout.json。不要混用两种CLI流命名。DISPATCH owner005准备失败保留为真实已消耗事件，交原作者055处理，未擅自重跑。

2026-10-25 19:23 UTC：A已完成7单文件和完整合并209/0/0验证，原3个realRBskip实际执行通过；19:22:43立即启动独立硬审38807，无产品PASS。DISPATCH055完成0/0，原owner006已运行84328。N8 reviewer042为HTTP502/native1、精确trace工具0、A42ABSENT，原作者066运行62041。root d41597资源预检raw1发生在任何attempt创建/CLI前；capacity040依据实际已闭合T7任务仅收回未用未来增长预留，所有文件/产品/UNKNOWN保持。

### 2026-10-25 20:17 UTC：真实并行接续与失败分类

- T7-A reviewer001/002均HTTP502/native1，原七单文件raw0保留，002完成独立合并209通过/raw0。r1-c8无RESULT且实际Python生产者已不在，保留UNKNOWN；r2-c8为新的明确恢复验证，未覆盖前者。003续原SID/store补反例和最终结论。
- T7-B 002/003HTTP502/native1；003只完成M1-GAP写入，无tracked源码改动。004交回原owner立即实施，不把诊断算完成。旧F格式/锁路径具体问题保留；不能以既存锁代替合法候选/最终锁。
- C05 owner007/008因HTTP502退出，真实完整运行和四置换已有关闭的原始结果。Root机械交接给原reviewer005，省去无产品收益的摘要重试。完整raw2以及171失败/60错误保持；正反序差异为test_restart_resumes_pointer_and_keeps_gaps_scheduled。首份运行曾与第二份重叠、原生退出未捕获，手写exit1无效；交独立reviewer处置。
- DISPATCH preparation002为第七次失败，原交付误写第六保留。author056native0/receiver1源于Split-Path参数集错误，作者已修正读取，Root从实际CHILD-START路径复核并写独立接收记录，未改写TERMINAL。007只按056修复并实际非消费预检，再允许preparation003一次。
- N8043有6工具、两个编辑raw1及HTTP502，未运行正式位置；author067native0/receiver0明确原地保存69480B草稿，044写新H44。044于20:14实际启动；045只在067的有限条件成立后使用，无046。F10/010 launch_identity程序化比对保持一致。
- 保持同SID/store/cwd、独立reviewer、原rawcode。五路实际ZCode调用；无原生Sol开发者，无持久模型配置变更。

- T7-A独立003实际native0但产品FAIL F01：TREND/PULLBACK重复同一bar会累计确认并提前OPEN。209项原测试通过不能覆盖反例；11项probe中的两项断言的是坏行为复现，也不能解读为产品PASS。原owner002的修复调用随后HTTP502/native1、11模型请求/4内部重试/零工具，源码未改；003保持原SID/store/cwd有界续接，不改配置、不另开计划。审核报告的published_utc晚于实际终态，保留原件并以原生进程/文件时间记录真实顺序。
- N8 reviewer044实际native0但正式FAIL/REVIEWER_SIDE：Path(plan_root)与str(leaf)恒不等，precheck004在子进程前失败且已消费；publisher检查.log却pin.json，发布曾失败。A42完整失败包只读，final002未消费；045的纯服务恢复条件不成立。原author068处理同一工具修复，提出正式消费前验证实际纯函数/合成路径和发布逻辑，完整产品人口及25s/35s预算不降低。
- Root启动author068的工具b31082 raw1来自author_receiver.py:22的真实物理资源准入，发生在PRELAUNCH/CLI前，model/runtime启动数均0。保留原REQUEST与错误；041只暂停已结束N8审核的空闲2GiB RAM、磁盘不释放，作者沿既有EX-SIM槽启动。随后042恢复M5原2GiB并通过新采样，不降低6GiB/4GiB底线。资源准入失败不是模型或产品失败，恢复审核前必须重新预留。
- Root只读查询工具4e3905 raw1把容量032的日期猜成20261025；实际权威输入列明20261024。未改文件或启动任务。后继从实际035的base_authority及原REQUEST取得引用，禁止按序号猜日期/路径，先核具体引用存在再读取。

- C05 reviewer005于20:42 UTC HTTP502/native1结束，但原batch supervisor PID139988/创建20:15:34.229928 UTC及full子进程146392、工作进程142488在20:50仍活跃。模型退出不等于测试结束；继续原进程，禁止重新启动整套验证。M5 reviewer005于20:53 UTC再次HTTP502/native1，trace67ae79d2-04fc-43de-ae98-f4c6c2840076，1请求/10内部重试/零工具；正式阶段均未消费，不立即盲重试。
- DISPATCH owner007在HTTP502前已真实运行preparation003一次：helper005:464调用旧late receipt，当前contract:18428拒绝R52_AUTHENTIC_LATE_COLLECTION_AND_CURRENT_VALIDATOR_REQUIRED，child1/wrapper1，成为第八次失败。Bash末尾tail使工具显示0，原WRAPPER_EXIT=1及OUTER-NATIVE必须保留。另发现owner重复删除重建失败的SYNTHETIC precheck目录，违反056保留要求；42条精确工具记录已保存，不能伪造已删字节。部分PREIMAGES003/derivation002/controls已经存在，禁止把它们当未创建输出重用。原author057正在处置相同修复，owner005仍NOT_RUN。

- T7-A owner003于20:55:30 UTC又遇HTTP502/native1，trace f2febee7-e7c8-48d6-8a14-42877b79692c，11模型请求/5内部重试/零工具/无上下文超限，仍无源码修复。停止连续盲重试，保留原会话、写集和F01，不以并发数量代替实际交付。

- N8 author068真实native0，receiver1只来自一次已恢复的SSE idle-timeout提示；Root逐条核39输入不变、原thread、7个命令全0、唯一turn.completed及完整输出，追加精确接收收据，原receiver1不改写、不重跑作者。046按068明确的新A46/构造检查/完整precheck005与迁移final002放行，仍不授权P或runtime002。capacity043仅把M5已结束的空闲2GiB RAM转回N8，其他预留/底线不变。
- DISPATCH author057 native0/receiver0确认具体原因：late receipt绑定原路径FileRef，比较器却返回相同bytes/hash的保存副本路径，完整FileRef不相等。只修原身份比较与保存映射验证，旧receipt及当前validator完整性分别保持；008已交原ZCode owner，PREIMAGES004/derivation003/verification005-003保留003部分产物，preparation004一次且完整前检先行。
- T7-B004因HTTP502/native1结束前已修改support源码，34工具记录已保留。daily probe真实raw3、69个组件FAILED:insufficient_inner_split，尾部命令0不能记预检成功；原结果有覆写风险，现存源码/探针/result已保存精确快照。005继续原owner，按原样本/成熟/purge/embargo门修SYNTHETIC构造，禁止降阈值、模型改写或把fit藏在“预检”名下；新检查唯一目录，最终接受仍等A修复和合法M1链。

- 2026-10-25 21:25–21:26 UTC：Root核对N8046/DISPATCH008的新实际工具完成后，在前次失败30余分钟后分别有界续接T7-A原owner004和M5原reviewer006。A八文件仍与修复前映像一致；M5的657旧输入全部匹配、005精确trace零工具，正式阶段消费0。两路实际START已生成，未换SID/store/cwd或模型配置。capacity044在真实物理余量足够时恢复M5原2GiB RAM，N8仍保留；所有磁盘与UNKNOWN扣账保持。五路ZCode和C05原独立完整测试同时运行，不代表服务已根治或任何新产品PASS。

- T7-B005于21:31 HTTP502/native1，11模型请求/18内部重试/零工具，无上下文超限记录；004的支持源码逐字节未变。006在观察到其他会话真实源码编辑后有界接续原SID，不重启冷fit、不修改模型配置。DISPATCH008虽native0，却只交付部分修改，并改变了057明确要求保留的访问器语义；Root定点核对原文和实际源码后交原owner009修复，没有把自述或CLI0当成完成。
- N8046的HTTP502/native1与两个实际源工具raw1分开保留：publisher8406B超过8192限制、一次trim发生AST错误，随后同名源和plan被原地改写。15条精确工具记录保留，但不冒称覆盖过的文件字节仍在。构造及正式目录均未创建；069原作者只处置已有中断，047未启动。Root工具459879因199628B的工具记录超过本地控制预算在发布和调用前raw1，任何author/CLI/正式检查启动数均0；9280b4用无损gzip保留完全相同记录，解压字节和hash回验，控制包43198B。不是改变代理上下文或压缩配置。
- T7-A004八条必需命令真实raw0、合并212通过。首次c3监督器目录统计与pytest临时清理竞争而未生成RESULT，保留UNKNOWN；开发者修改了旧runner，当前hash和旧9833B hash差异显式纳入新独立复审，未将旧输入漂移涂改为不变。21:52原reviewer在新审计根启动，原FAIL包保留；源码与helper全部停写。


2026-10-25 23:16 UTC：T7-A原reviewer005补齐终结评估，复用已执行212通过/11反例，不重跑，独立无条件PASS并推送d46274c；报告手填时刻偏差保留，以真实终态和hash解释。C05原独立batch自然结束22:33:16，reviewer006 native0并签两文件窄PASS，集成推送3238ff5；完整raw2/171失败/60错误/3收集错误保留。Git检查对已审CRLF报raw2时，未改冻结字节，仅命令级cr-at-eol重新检查raw0，无持久配置改变。

Forward004在C05解除依赖后立即恢复。T7-B007完成实际DAILY源码与容量/清单验证，Root交付已接受A/C05精确十文件（五个不同字节）；008修原计划要求的真实候选锁校验，禁止以旧2.1.2锁或_bypass_lock给昂贵冷链接受信用。M1专用ZCode001并行准备合法锁接口，未新开计划。M5原reviewer007 HTTP502/native1，精确trace36工具只有只读核查和三处reviewer探针编辑；两个发现raw1保留，affected/generation未运行，008继续真实审核。

DISPATCH准备004实际raw2/zerochild、第九次失败；author058定位schema和helper key双端失配及failed_history先使用后赋值，011原owner实施最小修复及真正入口纯检查，再仅准备005一次。058 native0但receiver1是已恢复SSE提示，原值保留、Root独立接收，不为此重跑作者。N8047 native0但正式FAIL/审核工具原因，两构造位已消费；070原作者有限澄清计时与失败发布，产品谓词尚未执行，不归咎于候选，也不发布P/runtime。

Root本轮两项调用错误保留：工具fe61e7假设T7-B007产物在work目录，PowerShell给MissingPath而后续目录枚举0不能证明该读取成功；按实际派工目录读取完成。327885把watchdog/Codex混合JSON流当逐行NDJSON解析而raw1，发生在派工/接收文件发布前。实际流先有合法多行codex bundle JSON，再有typed事件；复用现有receiver的JSONDecoder.raw_decode顺序解析并严格核bundle字段和唯一turn.completed，3efa99成功。以后从真实schema与已验证解析器取得数据，不按文件后缀猜格式；原失败及首次诊断“不含JSON”的不准确表述以追加更正保留。

Root状态文档提交第一次raw128：仓库没有持久user.name/user.email，裸git commit未取得作者身份，没有生成提交或推送。后继核3238ff5实际作者，沿用wmqfl861及其GitHub noreply邮箱，以命令级-c提供身份后提交，不修改全局/仓库配置。后续本仓提交须显式使用已核身份。

2026-10-25 23:41 UTC：Root恢复后新增调用前置错误如实保留。391372误猜M1 DELIVERY在work子目录，实际在派工根；PowerShell后续命令0不代表首个读取成功。fd5958又误猜Forward003 STEP-DELIVERY在派工根，实际路径已在REQUEST.inputs中且位于work；原raw1保留。4bbd49在构造T7作者003请求时假定旧legacy-binding原裁决也已交付候选，实际仅main本地有原件，raw1发生于REQUEST发布/模型启动前；已追加ROOT-PREPARATION-FAILURE-001并改用核实原件。避免方式：优先由权威REQUEST/FileRef程序化取得路径，对未知目录先存在检查和一次有限发现，不再按其他模块的目录习惯拼接。没有把这些Root路径错误算成产品或ZCode失败。N8048和Forward005已按原SID/store/cwd实际启动；T7原作者003集中处理现有14M4断言、版本及L0/L1路径，不改门槛、不新增未来产品。

2026-10-26T00: M1 review transport001 watchdog raw1: Root omitted the required nullable session_id_expected key. CLI launches=0; START/store absent. Preserve001 REQUEST; successor002 adds null and checks every required watchdog schema field before launch. Fresh dispatches must derive the full schema from the real wrapper, not assume optional keys. No product failure or test run occurred.

2026-10-26 01:16 UTC: Root optional discovery tool30ff8f raw1 came from assuming strategy-first README.md existed and using SilentlyContinue. The directory listing succeeded, but the absent optional read remained a command failure; no development/review/publish action ran in that command. This is Root discovery misuse, not ZCode/product failure, and is not reclassified as rg NO_MATCH. Actual entry is CURRENT.md from the returned directory. Future optional paths use Test-Path/Path.is_file before Read; do not suppress stderr or append an unclassified optional Read.

2026-10-26 01:19 UTC：Forward integrated306与T7-M4独立审核均真实native0/同SID/输入不变/无条件PASS；Root重核378源和37审核成员、M4九文件及真实裁决后接收。M4精确一文件已交B，M1五文件此前已交付。原B010、DISPATCH013、N8050、M5012实际并行启动。M5011 sealer发现条件表达式优先级绕过头部验证、将Markdown authority按JSON读取两处确定错误，交原reviewer在正式调用前修复，未消耗affected002。原代码和不准确010时间字段说明保留。
