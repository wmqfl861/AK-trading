# 策略研究主线实施计划

版本20261010-r2；对应[研究蓝图](BLUEPRINT.md)，修复分流见[积压表](REPAIR-BACKLOG.md)。本次只提交规划及独立ZCode计划审核。下列产品工作在用户授权实施范围内按有效计划释放；本文件不授数据库、SDK、现场操作、部署或交易权限。计划通过不计产品完成。

## 1. 五个自然模块及唯一文件归属

共同目标是从合格冻结历史输入得到分钟与日线的实际预测、训练收益样本、含成本的连续账户及可否证结果。复用既有M1–M5职责，不建立另一套研究管理系统。以下逻辑路径相对`src/ak_trading/v2/`，实际写入只在Root选定、独占的新候选revision；历史源、已审包与原lock只读。模块包可含多个原owner，但每个文件只有一名writer。

| 模块 | 产品目标与唯一源文件归属 | 消费接口及硬依赖 |
| --- | --- | --- |
| R1 研究输入与特征 | 数据原owner只写已有D1/D2/labels工单的精确集合；M1原owner独占`contracts.py/artifacts.py`等公共契约的原受权集合；M2 owner独占`factor_specs.py/factors.py/seasonality.py` | 合法FrozenInput、时钟/规则及真实M2特征/季节性；主选SC交付按G3→G4→G6，非全平台采用 |
| R2 概率生产 | M3A owner独占`probability/{__init__,labels,models,calibration,forecast,regimes}.py` | FeatureSnapshot/ForecastRequest→标签、模型、校准、MarketForecast/RegimeAssessment；依赖真实R1输出 |
| R3 策略、退出与账本 | M3B：`strategies/{registry,state,trend,breakout,pullback,reversion,grid,martingale,wait,replay}.py`；M3C：`exits/{registry,legacy,features,state,arbitration,sizing,trailing,time_decay,profit_ladder,volatility_stop,drawdown,signal_reversal,replay}.py`；M4：`decision/{pipeline,replay,payoff_paths,payoffs,gates,routing,risk,ledger,execution}.py` | StrategySpec/ExitBinding、真实payoff生产、共同准入、四路由及唯一连续账本；DAILY seam归M4，其他语义归各原owner |
| R4 实际fit与研究评价 | 一名M5 owner独占`validation/{experiments,splits,metrics,reports}.py`和`scripts/research_probability_v2.py`；冻结真实配置、注册和run输出也归该owner | 复用EP1训练编排、walk-forward及报告；消费R1/R2/R3冻结实物，不改其文件 |
| R5 后续历史模拟与影子接线 | 同一M5 owner顺序接管`validation/shadow.py`及R4公共调用文件；客户端/执行域仍由原执行owners写原集合 | 离线反事实与操作资格分开；ordinary shadow消费SHCP恢复、完整退出pack、实际feed/formal门；部署模拟另等执行域 |

R4的`experiments.py`/CLI不能再分给概率、退出或另一个评价owner；R5触及这些文件时先等R4终态和按文件释放。M4的DAILY修复不能由M5顺手完成。`__init__`、公共类型、fixture/decoder/lock若需增量，沿原M1写权；此表不是跨节点万能lease。新物化候选按选定原计划的精确source清单，不能只复制看起来较新的文件、拼PYTHONPATH或暗中导入主线。

必要测试分别归源owner；共享测试按归属保留旧断言：R2的六个`test_v2_m3a_{labels,models,calibration,regimes,forecast,causality}.py`；R3的M3B registry/directional/reversion_grid/martingale/causality、M3C legacy/registry/state/trailing/time_decay/causality、M4 payoff_paths/replay/payoffs/gates/ledger/causality；R4的M5 cli/experiments/payoff_integration/t7_evidence/splits/metrics/reports；R5的M5 shadow及原host/恢复测试。实际存在的名称由原计划required_tests和选定源定位，不将义务ID猜成pytest函数。新增测试只证明真实修复及反例，原计划强制full命令不被这份入口摘要削减。

## 2. R1：只补当前实验真正需要的输入

先读元数据和已接受来源记录，给六品种两频率全部期限列覆盖、成熟度、日历/费用版本、已使用区间及缺口。先评RB是否满足252/60/60及真实最长影响；按同一事前标准选择合约，不读策略收益。冻结输入必须可读回并证实completed bar、available_at、交易日/会话、合约规则和原始来源。后续需要DB/SDK导出时单独遵守已有操作授权；本轮和纯离线准备不读取凭据。

SC-M1三工具已有硬PASS，不先改工具。Root取得关闭进程的custody后执行有效G4发布/完整verifier/supervisor并得到实际terminal release/STOP/new lock；只从权威记录程序化取得cwd、argv和hash。G4完成立即让原M2 owner执行G6 exact304 copy、最终run、原H01–H08/D02，冻结后独立完整M2硬审。M2已经通过的prepare和16组SYNTHETIC人口保留，不能冒充final delivery，也不重复unchanged plan review。

当前存在1795项fixture中368 VERIFIED/1427 SCENARIO及9项late close_today费的有限记录；它们不能自动提升为历史PIT。实际冻结RB输入若消费D2 r3、标签r4或D1产物，立即安排相应独立review/修复；若来源已合格且不消费这些产物，它们按条件积压。D2原8+2已经执行，不重新写成NOT_RUN；labels已有24文件和Root G3，不等PG事务或主线apply才审核其受权源。日历空白照实留空，历史宏观不由当前答案补齐。

特征先冻结现有有效BASE定义，核对同源seasonality和available_at；旧v1“14计算器”不是后来15 BASE的自动证据。首批十因子MOM001/003/010、REV001、VOL001/004/005、VOI001/007/009作为下一组单变项对照，缺原字段则明确禁用该组。无需先完成所有新因子、盘口或宏观。

验收出口：真实可读输入与适用来源/规则接受、精确覆盖/缺口、M2必要因果回归、实际G4/G6输出及所属完整硬审。可以有品种/期限不足，但不能拿空目录、future hash或准备receipt充作交付；足够的分钟及日线输入各自立即交消费者。

## 3. R2：得到可使用且可评价的概率

核对M3A既有十二文件baseline、作者来源、依赖和当前hash。其continuation只是review-preparation，不能继承为产品PASS；不能回填开发前baseline或把新owner伪称旧作者。用当前冻结源和可追溯修订补实际review admission，原已通过源若确实兼容可按精确范围消费。入口为现有模型/标签/校准API，先使用受支持的简单频率基线，不凭本计划增加模型枚举。

在训练区间产标签与参数；未成熟、缺字段、源迟到、障碍先后歧义分别排除。按分钟与日线独立fit/calibration，保留全部8 horizon和原duration集合。普通与马丁校准证据、高概率桶、Brier、分位数覆盖和日期块严格用原门；样本不足仍输出明确状态。所需背景预测只能消费当时已训练、已校准、已完成且PIT有效的输入，不能使用未来日线收盘。

测试必须能证伪未来数据泄漏：追加未来不改历史快照；同日相关合约不跨split；分钟会话/夜盘边界正确；label-only尾段不拟合；同障碍触达累计概率满足原一致性。校准不足时不伪造高置信度，不降低0.70/0.85。来源/规则未满足的降级如PRICE_ONLY只按策略原资格，不变成通用放行。

验收出口：M3A真实可导入producer、原必要回归及独立硬审，分钟和日线分别可生成按事件/期限定义的合法预测与不足诊断。产品PASS不意味着预测统计PASS；本模块不负责最终账户报告，也不等待remote macro。

## 4. R3：修通日线自然链，保持同风险与唯一账本

第一优先是DAILY seam。已读当前主线及EP1容量/SC来源候选仍仅为MINUTE构造background，DAILY可能自然被`CROSS_TIMEFRAME_NOT_VERIFIED`拒绝。先核此次选定源；若已修，消费实际修复与对应硬审，不重复开发。若仍有缺口，由M4 owner在`pipeline.py`构造符合原StrategySpec声明的日线因果背景，把所用completed/available_at、请求及forecast identity传到原状态与准入链。不得新增未经裁决字段、强置ALIGNED或删除UNKNOWN拒绝。

本轮规划裁决：仅首批DAILY5入场模板，统一独立请求`TRADING_DAY20/ATR_1`预测作长周期方向背景；MINUTE继续原DAY1背景。主DAY5与背景DAY20为不同horizon真实记录，不复制主预测改名，也不宣称统计独立。用决策前最后完成且可见的d86400 bar构造因果特征，`at=max(end,available)`；保留原calibration_end、reference.available_at、READY及`published<=decision<expires`检查。`daily_background_max_trading_days=1`保持为背景时效年龄门，按交易日历检查，正常休市不按自然日误报；DAY20是预测horizon，不延长背景可用年龄，不以20日window_end代替staleness。M4接线保留最新合法完成日bar与原时效门，选定源若仅靠expiry隐含时效，须明确补齐原年龄检查。DAY20缺失/未成熟/过期仍UNKNOWN，禁止回退DAY1/DAY5或放宽准入。原`assess_regime`判定ALIGNED/COUNTERTREND；全部1/5/20预测继续输出。未来DAILY20入场不得用同一DAY20主预测自证，另按其当时明确规则扩展，不是本次首批入口。

M4实现调度、身份与时点，M3A核对所选源的合法DAY20生产及既有判定。长周期MarketForecast与M2“前日日线FeatureSnapshot”槽分开；`factors.py`要求后者早于当前交易日，不能塞入同日背景或放宽此规则。若实际source/接口改变须更新选定lock，沿M1原冻结流程；DAY20本身不要求扩大背景年龄值或新policy协议。任何未预见协议冲突仍以具体差异交Codex裁定，不能由owner改语义。验收用同一冻结输入检查天然可达正例、反向/缺失/未成熟/过期拒绝、背景年龄超1交易日拒绝、未来追加不改旧判断；固定主DAY5、只改变背景方向必须触发原冲突门。完整链为DAILY context→regime→admission→OPEN→真实fill/closed path→EP1 observation；工程正向fixture显式SYNTHETIC，不证明历史收益。真实DAILY outer+inner fit另由R4完成，MINUTE stand-in不能外推。

首批使用现有`trend_m1_v2/breakout_m1_v2/pullback_m1_v2`及日线`trend_d1_v2/breakout_d1_v2/pullback_d1_v2`，经济模板30分钟/5日，现有WAIT/CASH保留。M3B P0 lock/H10/ownership/source attribution实门先补所选版本；只存在14项注册不计H01–H10接受。R3按原阶段选择LEGACY pack；退出实现存在不代表阶段PASS，目标协议如condition/2.1.4未发布不作为已有合同。

真实训练计量遵循`replay_payoff_training_window`及冻结PayoffTrainingPlan，候选和退出不读未来标签；真实producer以正确entry×exit×费用身份生成观察。记录NO_CANDIDATE/PATH_INCOMPLETE/PRODUCER_EXCLUDED/OBSERVATION与分区排除计数、path/opportunity/plan/source。不要用空model普通replay造候选、mock生产或注入标签。不同退出必须重建18项exact-key收益和资格；OPEN_INITIAL/ADD_PRE_V1与CONTINUING_V1不混经济目标。

M4共同准入先数据/模型/状态、再trigger、净EV与尾部/资金风险；四政策只改变选择方法。MARTINGALE保持1/2/4三层、总7单位，与不加仓/等量/固定网格按整轮预算配对；整数手数、保证金及所有路径在入场前检查。原stop/target/最晚期限字节不变，动态保护只收紧，退出不自动反手。实际Fill、pending、原子checkpoint、partial close、CLOSE/CLOSETODAY和no_more_add latch进入唯一账本；策略切换/重启不重置损失或层数。

必要反例覆盖窄区间扣费亏损、V形、破位、休市跳空、限价无流动性、最后层、无法止损、信号过期、收盘禁入、未来追加、分批/重启重复成交。压力超过stop预算真实报告，不能把计划损失当保证。分钟与日线各有自然正向及拒绝路径；零交易不能盖掉天然链不可达。

验收出口：实际消费M3B/M3C/M4写集必要验证、冻结及独立硬审，合法LEGACY双频率训练/决策/连续回放能到达；收益不要求正、合法OOS可全现金。A/B后E0原独立LEGACY回归/网格/有限马丁基准按自身就绪与C/D并行，须接受所选源码/接口、原多层及ADD payoff、风险、成本和连续账本；不等待未消费的新增trailing/time/P2。P1按legacy/trailing/time_decay独立绑定后比较；新增P2退出/部分数量组合按单层分批→REVERSION/GRID→MARTINGALE，实际消费新P0仍守门。P3/P4另守原门，各准备工作不得写入活动review文件。

## 5. R4：真正fit、运行和交付双频率研究

复用[RESEARCH r2](../parallel-development-20261004-r1/plans/RESEARCH/r2/PLAN.md)及原EP1权威计划。EP1 source/capacity scoped PASS不包含实际generation/registration/fit。容量LEASE仅三test文件且禁止fit/generation；Root应给既有有效plan补精确实际操作admission，固定selected source、适用lock、PF结果、输入F、输出前置、cwd/argv、预算及single writer，不能扩容量lease。

原EP1工程路径PF→R13 generation→R14–R19 cold/fit/review/lock/filewise release按原要求完成；world630/2467/4467及six配方是SYNTHETIC工程验证，不代替RB历史输入，也不新设全平台首个实验前置。该固定候选路径只消费裸OPEN_INITIAL单层首次OPEN，不能以其通过冒称ADD_PRE_V1、多层、CONTINUING_V1或P4已支持；E0须用原独立版本完整多层/ADD生产和报告。若当前真实CLI消费该工程后继，必须具有它实际接受的合同和fit能力；不能拿尚未发行`contracts-2.1.5/2.1.6`路径或旧2.1.2文件名当兼容。锁验证包括实际public files、schema/signatures、fixture、ownership、requiredtests/policy，必须验证选定tree的字节。

M5 owner先prepare真实配置及split预检，随后在R1/R2/R3交付后按固定顺序执行真实fit。配置从现有decoder接受的schema建立，模板`tests/fixtures/v2/m5_research.json`是SYN.rb/旧2.1.0合成配置，不能直接改名充作真实RB；所选2.1.3配置需真实`payoff_training_plan_sha256`等已发行必填项。模式固定为walk-forward、final_holdout=false、RESEARCH_EVALUATION、synthetic=false；不发明development模式或新purpose。

真实调用形式为：`python scripts/research_probability_v2.py --mode walk-forward --input-bundle <真实冻结v2目录> --config <真实冻结配置> --output <尚不存在run目录>`。尖括号是未来运行工单待程序化绑定的参数，不能原样启动。本轮不执行。配置使用既有实验/run目录和`experiment-registry.jsonl`，不新建ledger/框架；保存实际schema/registry/lock/输入/代码/hash/seed、预登记矩阵及已使用区间。研究输入、registry和运行目录写权在实际admission中限定；输出未生成前不作为Read输入。

首批A/B分别使用现有`config.strategy_ids`选三族：分钟m1、日线d1，两个独立config/run_id/split_id，保留全部8预测期限/durations。各run FIXED事前固定TREND及实际legacy binding hash，BEST_EV/PROBABILITY_SAMPLE/CASH的fixed字段为null；所有policy为RESEARCH_EVALUATION，PRICE_ONLY仅按原资格有效，seed固定。不能发明RunPolicy.frequency/mode/candidate_pool，也不能在单config混六项后声称两周期账户独立。配置每频4路由×6cost共24 RunPolicy，合计48单元：8 BASE账户组、四成本各完整回放总32账户结果、GAP/NO_STOP按BASE cohort共16压力结果。费用/滑点/保证金情景不是固定BASE成交重评分，也不是额外调参机会。

先真实基线，再按预登记增量C；该候选池已有解码支持，无须新增选族开关。P1新增profile显式绑定归M5：冻结同一exit pack供fit/replay/report，不能通过FIXED hash代替profile。P1公共schema若缺已发行表达，由M1原owner按窄裁决冻结，不挡P0的A/B。

核对实际根输出`experiment.json/metrics.json/readiness.json/report.md/manifest.json`，fold内features/requests/labels/forecasts/models及诊断、training_paths、各policy账户、stress、splits。当前空`model_activations.json`或N=0宏观比较不能获得正式接入信用。训练全无模型会StageIncomplete/error.json，任何收益模型无观察会engineering FAIL及PAYOFF_TRAINING_EPISODES_NOT_AVAILABLE，保留原事实与漏斗，不冒称正常现金run；原因是结构问题则修，是真实机会/成熟度不足则诚实报告此实现出口与统计不足，不暗改合同。

未来运行工单必须固定所选CLI hash及实际exit契约。当前脚本第106行仅smoke/reproduce且engineering PASS可返回0；walk-forward/final-holdout/shadow-replay报告结果返回3，StageIncomplete也返回3但为error.json/engineering FAIL。这与06-M5第330行“请求阶段完整执行并发布报告=0、阶段未完成=3”有实现不匹配：原生3原样保存，分别核对请求阶段、真实产物、engineering、statistics、runtime，不改记0或笼统成功。若实际消费此实现，由唯一M5 owner在既有CLI写集修退出判定并完成必要正/负回归，使完整阶段与真实不足按原合同区分；统计FAIL/INSUFFICIENT本身不决定阶段未完成。本轮只记录该条件，不修改源码或执行probe。

报告必须显示逐层分母：源/时钟有效→预测成熟→校准与持续概率→行情资格→触发→训练path/真实观察→经济资格→整数/风险→可成交→closed mature episode。按品种/模式/期限/会话/年份/状态/高桶列n、日期块、排除和零组；统计FAIL/INSUFFICIENT不发qualification PASS。有效路径中的OOS全现金可以完成实验，路由候选单一时承认比较退化。

验收分两步：先必要产品验证/冻结/独立ZCode实际写集硬审；再依据被接受的精确代码和输入执行实际研究并验证复现与统计口径。开发期结果不得冒称最终留出，错误产物/未运行范围照实保留。首批交付须两种频率都有实际fit/预测/账户或明确失败证据，不以只有计划、支持PASS、一分钟stand-in或日志存在计完成。研究结论可以FAIL/INSUFFICIENT，不能以追求PASS为由无限扩大试验。

## 6. R5与调度衔接

历史模拟先沿已接受离线引擎核对相同账本和成本；若需要TqSdk simulation/backtest，先读skill并取得对应操作授权，TqSim/TqKq/历史导出不互相顶替。不要求为一次纯FrozenInput研究先接broker。

进入ordinary shadow前修当前`experiments.py`的未定义`_reg`、checkpoint缺exit registry/bindings及runner缺exit specs/bindings，按已有RESEARCH r2 B0–B8与公共SH-SCHEMA实施。`test_v2_m5_payoff_integration.py`当前期待NameError是已知缺口记录，应替换为真实完整正向而保留旧证据。SHCP5 E9恢复PASS保留其原人口/语义；合入新候选时验证实际当前occurrence、durable originals、pending和exit links，不重复fill/fee或以monitor动态播种。此修复只阻shadow-replay，不阻完整exit pack已接线的walk-forward。

完整shadow仍等正式研究、真实feed/clock及原20日门；操作模式遇统计不足/失败NO_TRADE。客户端部署模拟等EX06全同版接受与原S3，真实send/cancel=0；实盘首单需用户明确授权和UI开启。本计划不启动服务或操作UI。

Root在授权实施后立即并行：R1实际释放；R2归属/回归prepare；R3 DAILY修复与候选/LEGACY验证；R4配置、split、报告及EP1操作admission prepare。只做为明确A/B必要的prepare，不先生成泛化框架。R2/R3分别吃到所需冻结依赖后立即实施，R4在合法输入/源齐备后运行；A/B后E0按自身完整依赖就绪与C/D并行，新增P1/P2、影子及现场队列按真实门后继。若共用大表导出/昂贵fit窗口，运行遵守原排他资源要求，其余独占文件prepare并行。

计划独立ZCode PASS→原作者必要处置→有效plan及前置ready→立即独立Sol owner实施→必要验证及逐文件SHA-256冻结→立即独立ZCode产品硬审。FAIL回同owner append-only修复，重冻后fresh review；active-review文件对owner也只读。无需第二轮unchanged规划/审核或重复用户确认；本轮尚未授权的产品/现场动作不能由这条衔接规则扩权。原一次性命令失败保留，成功命令不为补证据重跑；生产进程终止后封stdout/stderr，不pin仍开的日志。

每个新工单携带原计划精确写集、输出前置、可选搜索exit语义及两次CLI incident规则。`rg`可选无匹配仅native1且stderr空时记NO_MATCH；其他原生错误保留真实exit，tests/CLI exit1不享此例外。参考[rg incident](../maintenance/20261004-rg-no-match-r1/INCIDENT.md)、[CLI incident及USAGE](../maintenance/20261004-grok-headless-cancel-r1/INCIDENT.md)。本轮作者一次文档诊断误按list读取dict，native1/AttributeError已保留工具输出；随后按实际schema读取成功，不计产品失败或验证信用。

## 原作者审核意见处置

[attempt-001实际报告](zcode-review/attempt-001/REVIEW.md)为无条件planning-only PASS、无material finding；原输入文本保存在`zcode-review/prior-reviewed-inputs-001/`，原报告不改。本次合并以下必要文字处置及Astra的E0/E1界限，修后供同一独立ZCode窄复审；本轮仍无产品验证/资格信用。

| 非阻断观察 | 原作者处置 |
| --- | --- |
| 1 导航指针 | ADOPTED；由Root修PROJECT-STATUS，本作者不写两导航文件 |
| 2 CLI exit 3 | PARTIALLY_ADOPTED；采纳实际返回码事实及前瞻pin要求，补R4合同/实现差异和条件修复；不把native3泛称成功或改记0，StageIncomplete与正常结果分别记录 |
| 3 背景policy值1 | PARTIALLY_ADOPTED；保留现有值的事实，拒绝“DAY20必须放宽/升级此policy”的推断。01-M1:945定义背景时效年龄；pipeline:311单独指定horizon，regimes:272–282取方向，forecast:143–148按请求horizon匹配证据，models:2213–2251分开horizon/expiry。R3明确年龄1不变及独立staleness，不凭空创建policy变更 |
| 4 后续退出族文件 | ADOPTED；仅记录现有家族与后续P2/P4状态，不把未实施目标文件当回归或前置 |

Astra范围意见ADOPTED：08-M3C:189/397保留原LEGACY各族及独立多层对照；463不让EP1单层OPEN解锁ADD；469–471继续约束所消费新P0/新退出/部分数量管理。E0是否已满足完整接受仍UNKNOWN，不预写READY/PASS。此次源码核对使用codebase-memory；图谱generation为2026-10-09，相关源metadata_changed/外部路径coverage unavailable，事实依直接读取，不声称图谱完整或产品硬审。
