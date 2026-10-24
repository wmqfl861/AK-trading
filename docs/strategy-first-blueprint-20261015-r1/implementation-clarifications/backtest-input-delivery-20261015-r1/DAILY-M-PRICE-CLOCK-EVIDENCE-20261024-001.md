# DAILY M：先解除价格时钟证据缺口的最小执行单

作者：原 BIP1 Astra/max `/root/astra_backtest_input_delivery_plan_20261015`。沿现有 strategy-first r2-001、BIP1、FIRSTFOLD、AFTER-312 和 AU 人口裁定；本单只新增 M 价格来源核验及有限文档取证。独立 ZCode 一次计划意见，由本作者处置；有效稿和实际前置 ready 即续接原 Sol，无 plan-PASS、额外用户/Root许可或重复计划审核。

**立即执行本地 M 价格谱系核验与一次官方数据说明取证。不要先造 M consumer/转换包，不再用当前合约公告代替价格 A 证据。** 前置事实更正见本目录 `DAILY-M-PRICE-A-FACTS-20261024-001.md`。目前没有可立即用于完整历史回测的 M 输入；此单的直接目的，是为原作者一次 M 专属时钟技术决策取得可复算的实际证据，随后继续真实日历/人口与完整输入交付。

## 1. 固定原件与明确的决策缺口

价格候选固定 `data/strategy_artifacts/futures-lib-20260907-codex-01/real-input-02/` 的完整五原件。这是先于容量的来源谱系选择，不是已接受 FrozenInput 或完整 T。保留所有原行/列/类型/版本，不按可见性、缺失或到期删原数据。既存全部 M 行的 2026 available_at 是 pipeline import bound；同期 handover-1 明证该映射，既不覆盖该列，也不称它为市场首次公开 A。created_at、import bound、actual first_seen、reconstructed research A 必须分开。

作者已本地定位新的实质证据：`backfill_contract_progress.jsonl` 第2578—2632行共55条 M/86400 日线完成记录，含 ts/status/rows/batch_id；备份亦保留这组记录。第一条 DCE.m2011 的 ledger ts 为 2026-09-01T16:20:24+08:00，与首个原包导入下界在同一秒但不同精度。它支持系列级采集关系，**不证明每个 CSV 行的精确 batch_id**。不可从近似时间或同一 symbol 反填缺失原字段。

已查全 M 来源时间标签：W 内 source family 为 tqsdk；edb_free 仅出现在2026-09-01至09-04标签。该事实仅划明来源范围，不改变研究 W/T/R 或删除后缀。旧临时目录28份CSV头只有一份M2611，不能拿它替代历史M原件。没有已证明的当年观察 first_seen。

M1 §5.3 允许正式 reconstructed 研究时钟，未要求一切历史价都等于本地导入日；RB的60秒政策只属于RB。**本单的 `m_reconstruction_delay_seconds=null`，不复制60秒，也不把 SDK 日期标签加86400当 end。** 需补的证据和决定精确为：

| 问题 | 本次取证/核验 | 原作者随后必须明确的决定 |
| --- | --- | --- |
| 原列是何种时间 | 五原件＋同期handover＋55条进度记录，保留不匹配/精度差/无逐行batch关系 | 哪些原值仅作导入审计，哪些是真正 observed/late/revision；不把两类混用 |
| 日线datetime所指 | 官方TqSdk说明、确切字段/单位/时区、具体合约/86400来源、当前安装代码的明确文字；与原包时间标签核对 | 标签如何关联交易日；仍以M合法会话求day end，不套RB日历 |
| 完成/可变/修订 | 官方说明对最后一根/历史日线的完成、更新或修订语义；已存来源能实际证明的例外 | 某行是否可进入reconstructed候选；已知晚到/修订怎样保留其更晚A或拒收 |
| 固定延迟的依据 | 原文是否给出发布/完成时点或延迟界；没有则如实注明未测量/未证明 | 若证据足以识别完成边界，本作者事前选择唯一、明确理由的研究延迟与限制；它是模型假设，不冒充实测上界。单有字段名、SDK可导入或最新兼容不能触发自动60秒 |

修订历史不完整保留披露；不得把“没有完整vintage”新增为一切重建的统一拒绝门。已知实际修订/延迟却不得忽略。若当前来源语义无法区分上述情况，则保留精确 UNKNOWN；成功HTTP或非空文本不能替代证据。

## 2. 两个可立即并行的有界动作

**离线谱系核验**：原 child 对完整原包M行做metadata-only扫描，不读收益/模型输出、不作raw-bound/capacity。验证manifest闭包、源family和日期标签、原A与created_at分离、空batch/imported原列，以及55条ledger和备份的逐项同异。每个被引用记录带原文件FileRef、CSV物理行或JSONL行号和原字段；`series_receipt_match`与`row_batch_identity_proven`分开，后者未证明保持false。只引用白名单 ts/task/symbol/duration_seconds/status/rows/batch_id/attempts；不输出detail、日志异常正文、环境或凭据。不得修改/重跑旧exporter，亦不重新访问数据库。

**官方来源取证**：主owner先复用固定本地TqSdk3.10.3源码和skill references的日线说明。3.10.3说明只能按其实际版本使用，不称它是2026-09-01下载的版本。若已有可核验原文回答全部所需问题，不用网络。否则只允许一次定向query：

`site:doc.shinnytech.com/tqsdk 日线 K线 时间戳 完成 更新`

limit=5，不加自动scrape。从真实结果中按返回顺序选择直接说明上述字段/完成/更新语义的官方内容，最多2个此前未消费URL；也可用第一份正文实际链接的第二份必要官方说明。仅`doc.shinnytech.com`内容，原结果/正文必须绑定locator。导航、只有示例、登录/付费墙、无相关内容即如实停止；不抓数据行情、不找合约、不开DataDownloader/TqApi、不测实时延迟、不请求账户。

**M首次独立上限1 query/2 URLs**，失败照计。它是价格语义取证，不借用或重置MINUTE/CU/AU余额。事前检查旧R1 REQUEST/RESULT/SOURCES、18个exchange_rules snapshot、已存TqSdk技能/维护来源及同原件host迁移/转换别名；已有终态复用，query或URL均不得重放、改写同义词重试或换输送途径。若实际结果给出更多必要来源，只登记具体未消费locator与作用，不越限。

唯一入口本机web-search-stack `127.0.0.1:3002`；复用已接受HTTP capture机制，105秒socket/115秒process/retries=0/shell=false；至多3请求串行、总process上限345秒。HTTP/API/content/native各轴保留。RB/AU原native22/HTTP502/API false不改记NO_MATCH；Root的中性Python查询native0只排除“全服务已证实宕机”说法，不解释原严格query失败。本单不重启或修改搜索服务。

## 3. 原owner与精确写集

沿当前职责：两位均为既有 `gpt-6.1-sol/max`，用followup_task，不另建替代owner；两者不是代码库唯一工作者，不回滚别人文件。作者/审核者不实现。

| 原owner | 唯一新根和产品文件 |
| --- | --- |
| `/root/sol_backtest_input_continuation_20261024` | `D:/AKS261024/R1/m-price-clock-source-001/`：`REQUESTS.json, CAPTURE-TERMINAL.json, M-PRICE-CLOCK-EVIDENCE.json`；实际请求ID固定`m-price-semantics-search-001, m-price-semantics-doc-001, m-price-semantics-doc-002`，每个实际请求仅`raw/<ID>/{REQUEST.json,RESPONSE.json,RESULT.json,stdout.txt,stderr.txt}`；非空实际字符串才生成`source.markdown.txt/source.html.txt`。 |
| `/root/sol_backtest_input_continuation_20261024/cu_calendar_prepare_20261024` | 明确从已冻结AU消费者转接新的M谱系核验，不改AU。`D:/AKS261024/R1/m-price-provenance-001/`：`reconcile_m_price_provenance.py, verify_m_price_provenance.py, M-PRICE-PROVENANCE.json`。这两个脚本只核验来源证据，不materialize MarketBar/FrozenInput。 |

两根各只再允许`WORK-ORDER-BINDING.json, INPUT-PINS.json, OUTPUT-PRECONDITIONS.json, VERIFICATION.json, FROZEN-DELIVERY.json, SUBMISSION.md`。离线required命令`reconcile-001, verify-001`，各自仅`evidence/<ID>/{COMMAND.json,RESULT.json,stdout.txt,stderr.txt}`；SYNTHETIC反例并入verify，不建大型测试框架。source facts无新的消费者逻辑时不强造单独套件。作者已检查两根不存在；Root/owner在实际执行前再绑定exact argv/cwd和全部生成输出，冲突用明确新后继，旧目录不覆盖。

每个离线命令300秒、1GiB RAM/256MiB临时输出；网络分支同1GiB/256MiB。并行最多2命令、总2GiB/512MiB，网络max1，沿Root既有策略lane的真实admission，不增资源账本、不借M5/full100。AU联合硬审已终结PASS并释放lane，原32文件仍冻结；Root一次计划咨询或下一次成果审核占lane时不并行跑这两个负载。此约束不阻独立静态准备。

本单没有新M消费者、sessions/population、bars转换、公共schema/M5源码、DB/SDK、凭据、fit/holdout、服务/UI/部署写权。模型配置零改动，trading=false、observer=hold、broker=NOT_RUN、real send/cancel=0。

## 4. 验收及来源终态后的立即衔接

谱系核验须真实重跑并核对全部M原记录、55条ledger与备份、五输入hash、每条证据定位、原A/空原字段和不能建立的关系；不得只看第一个样本。verify至少拒绝以下标记SYNTHETIC的错误主张：import A冒充市场first_seen；从同symbol/同秒回填逐行batch；抹去edb_free版本；raw timestamp+86400冒充合法日线end；当前3.10.3冒充旧运行版本；忽略明确晚到/修订；在delay=null/未证实day语义时宣称输入READY。测试不构造“历史事实”，不运行产品/SDK/网络。

`M-PRICE-CLOCK-EVIDENCE.json`逐问题记录原文定位、版本范围、支持/不支持的结论、取得时间、已知更晚信息和未解关系；publication/actual first_seen/derived研究A各自保留，缺项null。它不是M参数/日历/可见性消费者，不能写入一个临时60秒。

完成验证、真实文件/hash冻结后立即交独立ZCode GLM-5.3/max成果硬审：独立重跑两条离线命令、自写反例、逐项重读来源、无条件PASS/FAIL；不重发网络。FAIL回同实际文件owner追加修复，旧失败/请求预算保留。PASS只接受来源交付；完整输入未齐仍INPUT_NOT_DELIVERED，研究/fit信用0，不把这套核验说成历史回测。

**来源终态后本作者立即续接，同一任务内给出一次证据约束的M时钟裁定，不再等用户/Root批准或plan-PASS。** 预声明作者输出为本目录`M-PRICE-CLOCK-RULING-20261024-001.md`，尚未创建，禁止提前Read。裁定必须给唯一适用品种/源版本、来源日期映射、所选固定研究延迟及理由、真实late/revision优先规则、保留原值位置、质量/限制、成功/拒绝分支；无法支持的字段保持未定并指出实际缺哪个关系及可获取的精确locator，不能制造一个数值消灭UNKNOWN。必要实质新证据在既定预算内直接用，不重复审核未变计划。

若M可合法采用reconstructed，只有新派生记录在其政策和真实M会话下拥有研究A；原2026列和原first_seen未知留存，真实观察/晚到不可提前，原公共visible_bars等门不改。该裁定解除的是来源时钟定义问题，不自动交付完整输入。随后原R1按已有效`individual-lifecycle-intersection-v1`与真实M源补C/L/X/精确T-R/会话/完成/质量及实际bar依赖；原M5依既定顺序raw-bound→未拒绝才mixed容量/envelope，规则/费用/绝对limits/执行事实齐后原R1完整public FrozenInput回读及硬审，最后才进入原fit排程。未满足真实前置不执行昂贵下游，也不把source-only兼容或SCENARIO当最终目标。

W、原252/60/60与same312绝对边界、inner/nested、全部horizons/durations、完整前缀与真实最长尾段、200/30和500/60、purge/embargo、seen/holdout及M→I→SC次序不变。每品种至多一个研究定义；不借输入补证重置用量、换窗、挑收益、增加第七RB或删TRAIN到期成员。I/SC可继续非侵入准备，不得绕次序采用其容量或新请求。

继承AGENTS §§7–10、当前PROJECT-MODEL-ROLES/PROJECT-WORKFLOW-RULES，以及`docs/maintenance/20261004-rg-no-match-r1/INCIDENT.md`、`docs/maintenance/20261004-grok-headless-cancel-r1/INCIDENT.md`、`docs/maintenance/20261016-r1-custody-path-normalization-r1/INCIDENT.md`。仅optional rg raw1且空stderr为NO_MATCH；进程/HTTP/工具/产物/verdict分开。实际argv/cwd/pins从权威记录程序化绑定；Path归一化，不用$PID，JSON完整序列化再写/原子发布；失败残件保留；生产进程退出后封存日志，不pin尚打开流。本作者此单仅本地读与计划写，网络/SDK/DB/fit/实现均未运行。

## 5. 实际输入pins与一次计划意见重点

一次意见仅聚焦：导入/市场可见/重建时钟分离是否准确；原始字段及已知修订有无被提前；query/URL身份去重与预算；两owner写集；是否把不能证明的关系留空；来源终态如何直接驱动原作者裁定和真实下游。不要要求计划PASS，不为本次来源证据补出全窗工程包。下列refs及事实澄清内的五原件/当前源码pins为required inputs；原AU计划+disposition继续固定未变blueprint/人口/其他预算引用。

```json
[
  {
    "role": "facts",
    "path": "D:/Projects/AK-trading/docs/strategy-first-blueprint-20261015-r1/implementation-clarifications/backtest-input-delivery-20261015-r1/DAILY-M-PRICE-A-FACTS-20261024-001.md",
    "bytes": 9225,
    "sha256": "f52d41940aea6266c0e3cee5f4eeb2cc7c254b122e6700a0ec3351344131da3d"
  },
  {
    "role": "population_and_inherited_authority",
    "path": "D:/Projects/AK-trading/docs/strategy-first-blueprint-20261015-r1/implementation-clarifications/backtest-input-delivery-20261015-r1/DAILY-AU-NEXT-INPUT-20261024-001.md",
    "bytes": 18081,
    "sha256": "2f4243ff32ef019c6ed46d5be21d152df64f2ac463fa7e7648d704667fed1f16"
  },
  {
    "role": "effective_AU_disposition",
    "path": "D:/Projects/AK-trading/docs/strategy-first-blueprint-20261015-r1/implementation-clarifications/backtest-input-delivery-20261015-r1/DAILY-AU-NEXT-INPUT-20261024-001-DISPOSITION.md",
    "bytes": 6556,
    "sha256": "72717973fd5137adcb7a6ccebcf86a6c7f89c6c623bab7d110cfcf8bc271e033"
  },
  {
    "role": "raw_series_receipts",
    "path": "D:/Projects/AK-trading/data/downloads/backfill_contract_progress.jsonl",
    "bytes": 3712001,
    "sha256": "759502e2b9c6b36a537847ed841e266d2e880040c4311b2ca8b73e948b760c9c"
  },
  {
    "role": "preserved_receipt_version",
    "path": "D:/Projects/AK-trading/data/downloads/backfill_contract_progress.jsonl.bak_2356",
    "bytes": 4087392,
    "sha256": "0c3180b9acebd79b48903b1767681a87c0bb91120db04568eab8110765b421e5"
  },
  {
    "role": "same_window_anchors",
    "path": "D:/Projects/AK-trading/docs/strategy-research-blueprint-20261010-r2/implementation-clarifications/cu-daily-population-preregistration-20261014-r1/POLICY.json",
    "bytes": 8296,
    "sha256": "937e24d93bbfe14b75ccf63cb61a306ab9237f2a98a243d0866aac17c73dd628"
  },
  {
    "role": "neutral_control_only",
    "path": "D:/Projects/AK-trading/docs/maintenance/20261024-websearch-backend-r1/neutral-control-001.json",
    "bytes": 1251,
    "sha256": "59f67f086f815a3b6ae31ef71bf2c0b35676453f7fd1304c734cf1ee2ef3c659"
  },
  {
    "role": "current_role_authority",
    "path": "D:/Projects/AK-trading/PROJECT-MODEL-ROLES.md",
    "bytes": 5548,
    "sha256": "762a86e3e114c226ea1af52938dc8fe94bf9bd797517633292d1532883c3ecf9"
  },
  {
    "role": "current_workflow_authority",
    "path": "D:/Projects/AK-trading/PROJECT-WORKFLOW-RULES.md",
    "bytes": 4932,
    "sha256": "642025635ab4bdb9129c743ab19978cfaec5d11ae442b05c518a2631c91af19d"
  },
  {
    "role": "current_SDK_documentary_source_only",
    "path": "D:/Tools/tqsdk-3.10.3/Lib/site-packages/tqsdk/tools/downloader.py",
    "bytes": 19394,
    "sha256": "12bd121175f943b0e895c8f5e771dc8d149e8aafa6d8c221d6dc47f38973d675"
  },
  {
    "role": "current_SDK_documentary_source_only",
    "path": "D:/Tools/tqsdk-3.10.3/Lib/site-packages/tqsdk/api.py",
    "bytes": 232153,
    "sha256": "3b822aa7acd3212ccb22f0a64679e1bc232c462ede75ca50eff7375ce57efd36"
  },
  {
    "role": "skill",
    "path": "C:/Users/Administrator/.agents/skills/tqsdk-trading-and-data/SKILL.md",
    "bytes": 8729,
    "sha256": "2feced9f7f6c5cee9928b1fbc44b3466b488b71303e3d7875052f3da371852e2"
  },
  {
    "role": "skill_market_reference",
    "path": "C:/Users/Administrator/.agents/skills/tqsdk-trading-and-data/references/market-data.md",
    "bytes": 4829,
    "sha256": "5c8aaf8589140ec977267da8c2cbc0c50519abd3fe0ef0ac66be7857173cf94e"
  },
  {
    "role": "skill_field_reference",
    "path": "C:/Users/Administrator/.agents/skills/tqsdk-trading-and-data/references/object-fields.md",
    "bytes": 6969,
    "sha256": "d077978b1fec7764843d86c35bea12508fb12656b1d0ff40f64318580dee3c92"
  },
  {
    "role": "AU_spent_requests",
    "path": "D:/AKS261024/R1/au-original-capture-001/REQUESTS.json",
    "bytes": 43050,
    "sha256": "cbb761036f44422b5822bc4bfcdb82bb02e508cbe87a02a2f63b619f873acb62"
  },
  {
    "role": "AU_retained_failure",
    "path": "D:/AKS261024/R1/au-original-capture-001/CAPTURE-TERMINAL.json",
    "bytes": 8578,
    "sha256": "cd63d3c1f74809dc1fbcd2fa03955f22ca6034d929391b458098e88101930526"
  },
  {
    "role": "AU_product_PASS_only",
    "path": "D:/AKRV261024/R1-AU-HARD-001/VERDICT.json",
    "bytes": 5074,
    "sha256": "50b9e4724732e2dba0a8b11688d6dfde9c7128c302bc1f236f28226eac77c6ba"
  }
]
```

作者发布 UTC：2026-10-24T04:36:51.168650+00:00。
