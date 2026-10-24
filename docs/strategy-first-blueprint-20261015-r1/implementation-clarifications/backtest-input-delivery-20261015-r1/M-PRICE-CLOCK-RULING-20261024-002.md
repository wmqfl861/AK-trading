# M 日线重建研究政策、真实会话缺口与下一执行单

原 BIP1 作者：Astra/max `/root/astra_backtest_input_delivery_plan_20261015`。

**本作者现在冻结 M 专属政策 `m-daily-price-reconstructed-v1`：对固定 tqsdk 来源、日期标签核对一致且通过真实日历等原门的日线，研究可用时点为其合法最后会话结束后 10,800 秒。** 这是事前研究假设，不是观察到的市场首见、已测发布延迟或保证修订结束的上界。日期标签解释采用下面限定的当前 SDK 协议约定；合法会话和完整人口仍须真实证据。当前冻结包没有通过这些门，因此本文件不填任何实际行的 A、不发 READY、不启动 raw-bound/fit。

这项决定终结 M 的一般 SDK 时钟文档补取。下一最小动作改为 DCE 真实历史休市和会话事实的有界获取；不抓 DataDownloader 页，不再重抓 API 或 mddatas，不建立另一个零合格价格 consumer。MINUTE 与其他 DAILY 输入准备按其自身条件推进，M 未齐不是全局停工条件。

本单接续而不覆盖 `M-PRICE-CLOCK-RULING-20261024-001.md` 及其 facts/plan/disposition。001 中“尚未发布数值政策”的历史决定保留；本单仅对后续生产者赋予下面的正式政策。既有 61 文件的原字段、null、失败和研究信用均不改写。作者技术决定不代替独立产品硬审，也不重新开计划 PASS 或 Root/用户确认门。

## 1. source002 的终态说明了什么

source002 的一次实际请求为 `m-price-semantics-doc-003`，原 URL 含 fragment；native0 / supervisor0 / HTTP200 / API success / source status200，4.765 秒，07:28:09.587082 UTC 终止。16 文件已冻结，M 来源消费累计 **1 query / 3 URLs**，该预算没有余额。source001+provenance001 的独立联合审核是无条件工程 PASS；它只接受原 45 文件的来源/谱系交付。source002 独立硬审在本裁决时为 PENDING，不借旧 PASS 计入。

作者已全文读取 source002 保存的 247 行 markdown，核对主要 FileRef。L186–193 是通用 K 线起点/范围/终点字段；L201–206 有 86400 日线示例；L210–223 是更新和新 bar 信号。L175–180 的对齐是两个黄金合约的 5 秒例子，L80 是主力切换，L139–143 的夜盘是 SHFE.cu2607 quote 示例，均不授予 M 历史会话。HTML 的 `v3.10.3` badge 只绑定此次文档版本，不绑定旧下载运行版本；正文没有日线午夜对齐、M 历史会话、发布或最终修订时点。

source002 原 VERIFICATION 保留 shell writer 的 pre-process policy rejection（无 native、无代码执行、无输出、无请求）以及 verifier 把空 list 与数字 0 比较导致的 native1。修正为 list 类型和 len 检查后 native0，未改捕获内容、未重放请求；作者不把原失败改记成功。

## 2. 已补充的本地证据和适用解释

M1 §5.3 的实质条款在 `01-M1-共用基础与时钟契约.md:882`：历史价格 reconstructed 可按收盘加事前冻结延迟建立研究时钟，同时保留 imported_at。§5.1、§5.2、§5.6 仍要求真实会话、明确夜盘交易日归属及最后合法会话；重建不豁免这些要求。RB 旧裁决只是证明这种契约机制存在，其 60 秒与其他参数均不移用。

此次新增的本地事实如下；没有执行或导入 SDK，没有联网：

| 证据 | 作者采用的具体解释 | 不由它证明的事项 |
| --- | --- | --- |
| 本机 3.10.3 `tqsdk/backtest/backtest.py:560,577–578` | 该实现从服务 serial 取 item，对 duration≥86400 秒把 `item.datetime` 作为 `_get_trading_day_start_time` 的交易日参数。日线标签不是实际开盘或收盘时刻。 | 不是 2026-09-01 下载的运行版本证明；不是 M 法律会话。 |
| 同文件 635–637；`tqsdk/datetime.py:13–14,70–94` | 当前 SDK 的一般回放使用 UTC+8 日期标签，以及通常从前日18时至当日18时附近的合成交易日包络。`_get_trading_day_from_timestamp` 输出按上海午夜表示的日期。 | 18 时是 SDK 时间组织/回放约定，不是 M 市场发布、结算或最后合法交易时刻；该 helper 的周末算法不覆盖法定假日，不可用于本项目 VERIFIED 日历。 |
| 硬审已通过的 `M-PRICE-PROVENANCE.json:/rows`；作者 metadata-only 全量回读 f7034a/native0 | 11,819 条 tqsdk M 原 ts 都为 `00:00:00+08:00`；其中 1,280 个非空原 trading_day 全等于该日期，冲突0。另32条 edb_free亦保留，但不纳入本政策。 | 一致性不证明 census、真实交易日存在、完整会话或每行 batch；作者未查看 OHLC/策略收益/容量来挑政策。 |
| 已存 DCE `e4d13ae…8591.json:/markdown` L183–217 | 当前保存的 M 合约文本确实给出日盘三节9:00–10:15、10:30–11:30、13:30–15:00及夜盘21:00–23:00。可作为待核历史版本的具体规则内容。 | 该原件无已建立的 W 历史生效链；F/DCE M004-2020 是交割质量标准标识，不自动给整页会话条款赋2020历史有效性。不能据此直接发行W的 sessions。 |

作者据前两项协议行为和第三项冻结数据的一致性，采用 `m-tqsdk-daily-date-label-v1`：在该固定源中，将上海午夜原标签的当地日期解释为源日线意图表示的 trading_day。它是可审计的研究输入解释，采用当前协议约定；不声称逐行历史 runtime 已证明。**不再要求供应商某一文档必须逐字写出“午夜”才允许此解释。** 日期是否为合法 M 交易日、其全部实际会话和合法结束，继续由独立历史日历证据决定。

每行须同时满足：literal `DCE.mYYMM` 身份及源字符串相符、duration=86400、原 datetime_nano 与 ts 为同一 UTC 纳秒、上海当地时间恰为00:00、非空原 trading_day 与当地日期一致。任何矛盾、时区缺失、非午夜标签或来源非 tqsdk 都不靠舍入/偏移/换日纠正；原行留存并具体拒收该映射。空 trading_day 可在派生记录中按本政策得到 source-date label，原空值不回填。已知实际源版本若证明采用另一对齐规则，则该版本不适用本政策，须保留冲突证据，不能以当前约定覆盖它。

## 3. 唯一固定的 M 价格研究时钟

```text
product_scope = DCE M individual futures only
source_scope = fixed real-input-02 tqsdk rows, duration_seconds = 86400
dataset_kind = HISTORICAL_RECONSTRUCTED
daily_label_policy_version = m-tqsdk-daily-date-label-v1
availability_policy_version = m-daily-price-reconstructed-v1
reconstruction_delay_seconds = 10800
reconstruction_delay_nano = 10800000000000

D = source date under the label policy above
E(D) = maximum end_nano of the complete, historically VERIFIED M
       SessionWindow set explicitly assigned to trading_day D
A_base = E(D) + 10800000000000
A_research = max(A_base, every actually evidenced later availability/revision bound
                applicable to this exact source-row version)
quality.temporal_basis = reconstructed
```

10,800 秒是作者为日终批处理选择的固定三小时缓冲。在正常最后会话确证为15:00的日期，公式给18:00，与上述 SDK 一般日线回放包络相容，并给日终处理留出显式时段。这里没有把 SDK 回放时刻认作市场发布事实，也没有从其实现推导交易所日历。三小时不是优化后的参数，不开展60秒/1小时/3小时的结果比较，不因容量或收益不足改短。若某日真实 E 不是15:00，仍使用 E+3小时；不能把 E 硬改成15:00以得到想要的18:00。

“保守”仅表示相对收盘即用增加了事前缓冲，不保证覆盖所有实际时延和修订。对于没有观察首见的合格最终历史源版本，M1允许上述研究假设；不因完整 vintage 不可恢复而将它们全部永久拒收。但有可靠实际晚到、补齐或修订证据时，必须绑定到具体版本，不能提前。无法确定该证据和行内容的版本关系则拒收该版本的重建，不能忽略矛盾；没有任何观察证据时 first_seen 仍是 null，不伪造历史观察。

四种时钟仍分别保存：原 CSV available_at 为导入边界且原字节留存；actual market first_seen 单列且当前null；派生 A_research 只在原门满足时计算；manifest.created_at仍为原导出时间。当前 source001/source002/provenance001 是冻结历史交付，里面的 delay/policy/A null 不改写。本单只令**前瞻政策参数**确定，不让参数存在本身变成“实际A已交付”。

该政策只覆盖 tqsdk 的 M 日线价、量、仓；它不覆盖32条edb_free、SC/I/CU/AU/RB、分钟或宏观/规则/费用。所有原行仍在原件/审计人口中，不能因为未适用而删行、抹版本或缩分母。保持54 ok+1 skipped、m2401的242行和 `UNRESOLVED_FROM_THIS_BLOCK`，不把系列候选变成逐行batch证明。

未来生产者只能写新派生包，并将实际源、行身份/版本、标签政策、会话证据和时钟政策放入原有 source_hashes/附属证据闭包。公共schema、visible_bars、当前目标槽、PIT、READY/complete、跨日背景与冲突门不改。d=A−1ns不可见，d=A只通过时间门；intraday仍排除当前交易日最终日线；未知或不完整会话保持不可用。后续即使修订被发现，也不能改旧快照或重算成更早A，须新包、新身份并保留原版本。

## 4. 下一项实际动作：DCE 历史日历事实，最多三查询四原件

**立即续接原主 Sol `/root/sol_backtest_input_continuation_20261024` 执行该来源任务，模型/effort仍为 gpt-6.1-sol/max。** 作者没有委派消费者或公共产品修改。网络 owner 不更换，原 child 维持其冻结产品 WRITE_STOP；两位都不是代码库唯一工作者，不覆盖别人编辑。

当前确切缺口已从“无法选择M时钟”收敛为：M在相关日期的历史有效会话规则、夜盘归属/节前停开及2023–2024休市安排；更早完整前缀、真实最长尾段与个约C/L/X/T/R仍必须后续按实际输入闭合。本单不把W两年的年度通知冒称完整prefix/tail，也不从标准月份或已有55个symbol生成census。

先复用本地原件和所有旧请求终态，包括上述M合约正文、2022〔99〕公告、2024〔419〕风险公告及其各自真实版本限制。现有M标准文本明确有会话内容但历史链未定，不能把此前另一2018路径的JS挑战当作所有本地表示都不可读。已存e4正文不得重抓。2024-419的参数生效不是休市日期/会话证明，2022-99的坏解码.doc不可反向恢复，也不在本单重放。

新增scope=`M_DCE_CALENDAR_FACTS_001`，仅以下三个精确定向查询，各limit=5、不自动scrape，按序串行：

1. `site:dce.com.cn "2023年" "部分节假日休市安排"`
2. `site:dce.com.cn "2024年" "部分节假日休市安排"`
3. `site:dce.com.cn "豆粕" "夜盘" "交易时间"`

只取真实结果中按返回顺序首个能直接回答对应历史安排/规则版本关系的 DCE 官方正文；导航、搜索片段、当前参数页、期权或其他品种例子都不能替代目标事实。允许正文实际绑定且必要的一个附件或历史版本链接；所有实际URL合计最多4个，失败照计。第三查询针对原e4会话内容的历史出处/适用关系，不是再获取同一现页。若结果没有合格内容locator，记录该查询实际结果后停止该分支；不改词重试、不猜ID、不按日期扫日行情、不crawl，也不转SDK文档。

本scope最多 **3 queries / 4 URLs**；旧时钟scope的已用 **1 query / 3 URLs** 永久保留。因此本轮结束后M合计最多 **4 queries / 7 URLs**；不是旧预算复活或其他品种额度转入。query也须检查旧精确请求，URL按资源身份去重（http/https、host迁移、fragment和转换表示不能作为新的重试机会）。已有终态复用；从预算中未消费不代表应该用满。M当前源码/规则没有新的数据下载权限。

唯一新写根为作者已检查不存在的 `D:/AKS261024/R1/m-dce-calendar-source-001/`。仅允许：

- `WORK-ORDER-BINDING.json, INPUT-PINS.json, OUTPUT-PRECONDITIONS.json, REQUESTS.json, CAPTURE-TERMINAL.json, M-DCE-CALENDAR-FACTS.json, VERIFICATION.json, FROZEN-DELIVERY.json, SUBMISSION.md`。
- query IDs固定为 `m-dce-calendar-search-001`、`002`、`003`；内容IDs按实际消费顺序固定为 `m-dce-calendar-doc-001`至`004`。每个实际请求仅 `raw/<ID>/{REQUEST.json,RESPONSE.json,RESULT.json,stdout.txt,stderr.txt}`；实际非空字符串才生成 `source.markdown.txt/source.html.txt`。未运行ID不伪造response/result。

`M-DCE-CALENDAR-FACTS.json` 逐个真实来源记录：原文、准确pointer/quote、出版/生效/版本、适用交易所和品种、闭市日期、明确停开/恢复夜盘、各连续片段及归属规则、冲突与未知、依赖哪个原件。只交证据确立的事实；可以取得必要历史component，但不能自动写 sessions、C/T/R、A或完整研究输入。官方通知若仅有发布日期且后续确需动态可见性，本单不顺手移用AU/RB公告时钟；静态历史日历事实和动态PIT用途分别标明。

无新业务产品逻辑时不造测试框架；实际验证必须核对请求身份和预算、响应字节与引用切片、版本/用途范围、输入pins不变、每条真实终态。常规原文success不等于qualified；但事实真的足够时也不能故意保留null。结束并逐文件freeze后立即交独立ZCode产品硬审，不重跑网络；FAIL回同owner追加修复，旧请求不重用。source002自己的待审16文件不随本scope改动或被默认为已审。

沿既有已接受capture、唯一127.0.0.1:3002：105s socket、115s process、retries=0、shell=false，每次1GiB RAM/256MiB输出、network max1，最多7次请求总process上限805s；离线封存每命令≤300s。总目录≤256MiB。复用当前Root策略lane和完整reservations、floors、旧UNKNOWN charges，任何更晚Root容量记录须实际绑定；不新建资源额度，不借M5/full100。source002硬审或forward bootstrap验证占用该独占槽时只准备静态控制文件，真实释放且fresh admission后立即启动。这是运行约束，不等新批准或plan-PASS。

本scope终态后作者直接按已得事实确定下一最小动作；不再次开启一般SDK语义研究。若仍未形成必要历史关系，如实结束本scope，保留精确缺失，继续下节允许的独立线路。没有任何事实成立时不制作空consumer来充数。

## 5. 其他线路及原阶段顺序

M未交付不阻原A MINUTE：FIRSTFOLD-AFTER-312本来就按消费者分别判断。原M5已有MINUTE容量结论不重跑；R1若有新的合格规则/绝对limit/执行事实，即在自身工单下消费，完整public readback、原checks、独立硬审和M5 admission齐备后立即运行原A。M政策或其calendar不是RB MINUTE的额外依赖。此次原owner明确报告没有MINUTE完整输入新变化，不能据M补证声称A已ready，也不重复产同值包。

DAILY仍按RB→CU→AU→M→I→SC的既定来源次序和每品种唯一输入定义。RB必要上界拒绝保留；CU/AU当前真实缺口保留；M当前是政策已定但历史会话/人口未齐。I在该固定原包没有bars，SC有原始候选但没有自己的完整人口、会话和政策。这些状态不是容量0或统计FAIL。

I/SC的既有非侵入来源准备可以与M并行；原计划没有要求等M全成功才读已授权本地原件。后继若真的先满足完整数据和实际消费者条件，应按固定顺序记录前面品种当时的具体不具备条件，再采用首个满足项；不要求先对一个输入未齐品种运行不合法raw-bound，也不越过一个已经ready的前序品种。当前没有证据宣布任何后继ready，本单没有给I/SC新网络或自动继承M政策。

实际DAILY阶段继续是：完整C及合法L/X→固定T/R/source→所需本品种合法日历、原bar身份/质量/完成/上述适用A→原M5便宜raw bound→未拒绝才mixed容量/plan/envelope→补齐实际规则/费用/绝对limits/执行事实与尾段→原R1完整public FrozenInput回读/原checks/独立硬审→原M5研究admission。不可用metadata计数替代该链，也不为不具备输入的品种运行昂贵下游。

W、原252/60/60、same312绝对边界、inner/nested、全部horizons/durations、完整前缀和真实最长尾段、200/30与500/60、purge/embargo、seen/holdout不变。`individual-lifecycle-intersection-v1`不变。完整输入当前仍`INPUT_NOT_DELIVERED`、研究信用0；无fit或回测完成声明。

## 6. 执行纪律与引用

本单是作者计划/裁定。没有SDK构造、DB、凭据或secret-env存在性检查、模型配置或公共源码修改。trading=false、observer=hold、broker=NOT_RUN、real send/cancel=0。

继承当前PROJECT-MODEL-ROLES / PROJECT-WORKFLOW-RULES、AGENTS §§7–10，以及 `docs/maintenance/20261004-rg-no-match-r1/INCIDENT.md`、`docs/maintenance/20261004-grok-headless-cancel-r1/INCIDENT.md`（含USAGE）、`docs/maintenance/20261016-r1-custody-path-normalization-r1/INCIDENT.md`。仅optional rg raw1且stderr空为NO_MATCH；native/HTTP/API/custody/artifact/product分开。cwd/argv/hashes程序化取权威记录；Path归一化，不用$PID；生成输出先查ABSENT，JSON完整序列化后发布；失败残件保留；流等producer终止再封存，不pin活动parent日志，不覆盖旧成功/失败。

作者的SDK结构核对使用codebase-memory Verify：项目AK-trading-current-goal-20261016，generation=2026-10-15T17:59:22Z；外部SDK路径均outside_project，已定点读真实源码，不声称图覆盖。可选 `tqsdk/backtest.py` 不存在，记录NOT_PRESENT_OPTIONAL，随后定位实际 `backtest/backtest.py`。DCE本地发现中一次输出过宽的native0诊断（3988b8）被工具截断，后续以窄字段/精确文件复核；不以该截断输出声称全量文档已审。已核对的e4正文与M业务细则现版本均只按其实际范围使用。

以下主要FileRef与之前冻结闭包共同组成依据；原M1和RB裁决只提供契约机制，绝不以RB参数替代本单参数。

```json
[
 {"path":"D:/AKS261024/R1/m-price-clock-source-002/M-PRICE-DAY-SEMANTICS.json","bytes":39689,"sha256":"cb1172ec72253a9b7b8a2cb360c4e1234c7f5fde22a8e049340368ad697b3a68"},
 {"path":"D:/AKS261024/R1/m-price-clock-source-002/CAPTURE-TERMINAL.json","bytes":11100,"sha256":"c4f3fc4eb8a858fa28ff1fc0279d33681a360717f3e8a6fb6119dcd256b5ad42"},
 {"path":"D:/AKS261024/R1/m-price-clock-source-002/FROZEN-DELIVERY.json","bytes":7711,"sha256":"704d19119d8192350991d890f0f896e971ca879a71165070f0597080df7dbd0c"},
 {"path":"D:/AKS261024/R1/m-price-clock-source-002/VERIFICATION.json","bytes":43738,"sha256":"dc4c6c2635138796c48abf7808643ccefe8d9da77115bc9cb961eeec86631353"},
 {"path":"D:/AKS261024/R1/m-price-clock-source-002/raw/m-price-semantics-doc-003/source.markdown.txt","bytes":12606,"sha256":"01747d51a56032636717fed82542ab9778c96f15ff1805d8dfd02f698217e349"},
 {"path":"D:/AKS261024/R1/m-price-clock-source-002/raw/m-price-semantics-doc-003/source.html.txt","bytes":47636,"sha256":"f2fb8dd679fd9fac35ff3b815670cab6aa32ed618dddfb5c98e0e0b8c7ac1f33"},
 {"path":"D:/AKS261024/R1/m-price-provenance-001/M-PRICE-PROVENANCE.json","bytes":23087537,"sha256":"6134efc843241fd99c23f7c08d2096506f671c19b94d9c7dbe05a877334e3d8a"},
 {"path":"D:/AKRV261024/R1-M-CLOCK-HARD-001/VERDICT.json","bytes":7836,"sha256":"8b2698191afc80fc95bb0e88f3fbeb0e70930dcd00af87f086bbeb9cbf042287"},
 {"path":"D:/Tools/tqsdk-3.10.3/Lib/site-packages/tqsdk/backtest/backtest.py","bytes":44886,"sha256":"99dce8fa5f1c91de27dc14b9daa66dbb95ae03cb7c609f00341e84370fe1c9ca"},
 {"path":"D:/Tools/tqsdk-3.10.3/Lib/site-packages/tqsdk/datetime.py","bytes":7173,"sha256":"450050b72c98762c0dc32b2a4c8c0516da77ba5ca8642dc1d87b132a5d2fa8dd"},
 {"path":"D:/Projects/AK-trading/data/exchange_rules/documents/e4/e4d13ae3602d1aa9ce60c9f7c9e1da2fab2bae2a70b6e27fb7e8a02a37ae8591.json","bytes":7754,"sha256":"e4d13ae3602d1aa9ce60c9f7c9e1da2fab2bae2a70b6e27fb7e8a02a37ae8591"},
 {"path":"D:/Projects/AK-trading/docs/devplan-v2/01-M1-共用基础与时钟契约.md","bytes":70562,"sha256":"b736cc9bf433b910bfb3972b0d4d6f5c7b921a14d62037461d1c0ecd35ef584a"},
 {"path":"D:/Projects/AK-trading/data/maintenance_artifacts/v2-real-data-track/input-builder-prep-m1/policy-arbitration.md","bytes":27043,"sha256":"154f09d5888f6f80399bf9e76e6e58f4600b42432e22762758ca2e6e64613bd4"},
 {"path":"D:/Projects/AK-trading/docs/strategy-first-blueprint-20261015-r1/implementation-clarifications/backtest-input-delivery-20261015-r1/M-PRICE-CLOCK-RULING-20261024-001.md","bytes":17480,"sha256":"7ccdd948d4c16df2e061f8eacfeb3bc289712c5c97024a6247040f202d0666c8"},
 {"path":"D:/Projects/AK-trading/docs/strategy-research-blueprint-20261010-r2/implementation-clarifications/STRATEGY-FIRSTFOLD-AFTER-312.md","bytes":17025,"sha256":"041c5fd624df4fb0fe6f5f30e8bd37fd1f4341c860ae18306c775823a499b101"},
 {"path":"D:/Projects/AK-trading/docs/strategy-first-blueprint-20261015-r1/implementation-clarifications/backtest-input-delivery-20261015-r1/DAILY-AU-NEXT-INPUT-20261024-001.md","bytes":18081,"sha256":"2f4243ff32ef019c6ed46d5be21d152df64f2ac463fa7e7648d704667fed1f16"}
]
```
