**SC 日期标签与研究可用时点：原 BIP1 作者最终有效处置**

原作者：Astra/max `/root/astra_backtest_input_delivery_plan_20261015`。本件仅处置本次独立咨询 F1–F8；不新增数据获取、产品计划或运行授权。

**状态：EFFECTIVE。`sc-tqsdk-daily-date-label-v1` 与 `sc-daily-price-reconstructed-v1` 按下文正式生效。** 已审核的 `SC-DISPOSITION.md`、`SC-ASSESSMENT.json` 保留原字节、原提案状态和全部诊断；本件的最终政策及开发路由解释优先于其冲突文字，其余数据缺口、输入 pins、写集边界、人口/成熟要求继续有效。不再求计划 PASS 或重复咨询。

本作者全文读取的独立意见为6项建议F1–F5/F8、2项确认F6/F7、0 blockers。原生TERMINAL/RESULT均0，actual SID=`sess_bf8cf7ce-3cce-452e-b039-d87786d46278`、session_identity_match及inputs_unchanged为true；detached记录child0/supervisor0、job_active_after=0、315.421秒、errors=[]。这些是咨询真实结束与其记录的输入完整性，不是产品PASS。咨询者未执行命令、数据复核或hash校验，原限制保持；本作者本轮只重新核对下面十个具名文件，不重扫29734行。

**逐项自主决定**

| 意见 | 处置 | 最终影响 |
| --- | --- | --- |
| F1：3200填充标签不是独立来源意图证据 | ADOPTED | 从政策依据中撤去独立确认含义。全部午夜及零内部矛盾仅支持可检验的存储形状；本政策是作者研究解释。3200项reference_calendar_date一致只作非矛盾观察。 |
| F2：公式排除导入/导出时钟 | ADOPTED | 公式内显式定义允许的market-facing availability/revision界限集合；pipeline_import_bound、created_at、导出/抓取/本地摄取时钟不属于该集合。 |
| F3：VERIFIED和集合完整性绑定 | ADOPTED | 绑定M1 SessionWindow.evidence_status=VERIFIED、SC本品种及trading_day=D、适用calendar_version，并证明D的应有会话集合完整；与MarketBar.complete区分。 |
| F4：午夜只作源身份，禁止24小时假区间 | ADOPTED | datetime_nano原值保留；start_nano/end_nano由真实已验证会话派生，不取午夜与午夜+86400。 |
| F5：滞后量级及无DST前提 | PARTIALLY_ADOPTED | 采纳固定elapsed86400秒、Asia/Shanghai及实际滞后后果；不将其写成恰晚一个交易日或固定D+2夜盘可用，也不预设E(D)=15:00。 |
| F6：PIT/晚版本/边界确认 | ADOPTED（确认，原门不减） | A-1ns不可见、A恰边界只过时间门；intraday当前交易日终值过滤、版本冲突拒收及旧快照不可改继续。 |
| F7：条件性与A0确认 | ADOPTED（确认） | 现有E(D)未定义、A0、INPUT_NOT_DELIVERED、无consumer/fit保持；政策本身不产生输入。 |
| F8：标签拒绝反例 | ADOPTED | 加入明确SYNTHETIC拒绝清单，属于将来真实获准实现的原验证义务；本次不运行或派发测试。 |

**最终政策一：`sc-tqsdk-daily-date-label-v1`**

适用对象固定为原五文件 `real-input-02` 中已有 `INE.sc` 个别期货、`source` 表明tqsdk并与行symbol/duration匹配、`duration_seconds=86400` 的行。输入manifest SHA继续为 `99366eb8310222957d2dc896df1e6781982b9995a91c352f106bfbbfd77493a3`，完整五文件pins由原SC-ASSESSMENT保留；不得扩到另一包、别品种、分钟、edb_free或主连合约。

对时间戳显式带时区、按Asia/Shanghai解释恰为当地00:00、且datetime_nano和ts表示同一UTC时点的适用行，将当地日期D解释为这个固定源日线意图表示的交易日标签。已存在的非空trading_day必须与D一致；为空时只可在未来派生字段表达D，不回填原列。非午夜、缺时区、来源/身份/周期异常或已有标签冲突都逐行拒绝映射，保留原行与拒绝原因，不靠舍入、偏移、改日期、清空冲突值或删行恢复通过。

这是作者为该固定数据表示选择的研究解释。原观察为SC29734行上海午夜一致，其中tqsdk29658行；3200个tqsdk非空trading_day均标 `reference_calendar_date`，不能把参考日历比对/填充产生的一致再次作为来源意图的独立验证。零metadata mismatch只说明所查元数据内部无矛盾，不证明官方交易日、来源vintage或历史SDK运行语义。即便解释得到D，D是否为合法SC交易日、哪一夜盘属于D仍由独立历史会话证据决定。

原 `datetime_nano` 始终只是源身份。日线的86400是周期标识，不表示全天24小时连续交易；不得以 `start=D午夜`、`end=D午夜+86400秒` 或原timestamp当完成时刻。未来 `MarketBar.start_nano/end_nano` 必须从D的完整合法会话集合确定实际边界；保存全部片段及休息/跨夜关系，不能把最早至最晚边界之间的休息全填成交易分钟。

**最终政策二：`sc-daily-price-reconstructed-v1`**

本政策沿M1 §5.3允许的历史价格重建研究时钟，固定 `reconstruction_delay_seconds=86400`。它是本作者事前选择的等待延迟，不是实测发布分布、保证最终修订完成的上界或历史观察PIT；没有根据收益、容量或样本量挑选参数。不继承M三小时、RB60秒或AU公告时钟。

该参数为UTC纳秒轴上的固定elapsed时间 `86400000000000` 纳秒。标签解释仅在此固定SC源的Asia/Shanghai日期范围内使用；这个范围无DST，因而elapsed一天与同一当地时刻的次自然日对应。它不是“下一交易日”，跨周末和节假日仍加固定秒数；不自动泛化到其他时区或DST区间，扩大范围不是本政策授权。

定义 `S_SC(D)` 为未来实际冻结 `sessions.json` 中：product与INE.SC规范身份一致、`trading_day == D`、`evidence_status == "VERIFIED"`，且calendar_version及其原件证据确实覆盖D的全部应有合法SessionWindow记录。该集合必须非空，日/夜片段、跨午夜关联、休息、节前停复和休市归属由历史证据完整确定；不是先过滤掉UNKNOWN/SCENARIO后把剩余VERIFIED子集称完整。每条已存在记录VERIFIED仍不证明没有漏掉一条应有记录。

未来计算须固定实际sessions文件FileRef、采用的calendar_version、各session_id/记录位置与历史原件关系；这些是已有完整日历/引用闭包义务，不新建管理框架。若集合缺失、不完整、冲突或含所需但未验证片段，则 `E(D)` 未定义，A保持未知并拒收该计算。此处“集合完整”区别于 `MarketBar.complete`：后者仍须单独通过原完成/质量检查，不能因日历全而把缺数据bar自动设complete=True。

定义 `B_market(row_version)` 为已取得可靠证据、实际绑定到这条源内容版本的市场/供应商对外可用、晚到补齐或修订下界。允许进入集合的证据必须证明内容版本及对外可用关系；不能只因为某时间较晚就纳入，也不能以是本地记录为由忽略其中确实证明的市场修订。缺乏内容/时点对应关系或有未解决矛盾时拒绝该版本，不能用空集合掩盖已知晚版。

```text
D = accepted source date under sc-tqsdk-daily-date-label-v1
S_SC(D) = COMPLETE legally applicable SC SessionWindow set
          with trading_day == D and evidence_status == VERIFIED
E(D) = max(s.end_nano for s in S_SC(D))   # only after completeness verified
delta_nano = 86400000000000             # fixed elapsed 86400 seconds

B_market(row_version) = only proven external market/provider
                       availability, late-completion or revision bounds
                       applicable to this exact content version
# EXCLUDED from B_market as mere pipeline/administrative timestamps:
# pipeline_import_bound, local_imported_at, retrieval/ingestion clock,
# manifest.created_at, export/serialization clocks

A_base = E(D) + delta_nano
A_research = max({A_base} union B_market(row_version))
quality.temporal_basis = reconstructed
```

若在真实核对后没有任何已观察市场晚到/修订界限，`B_market`可以为空，公式取A_base，并保留 `actual_market_first_seen=UNKNOWN/null` 和未知revision history；不新增“必须恢复每个历史首见”的统一门。可靠后来修订或更晚公开界限必须纳入对应版本，既有快照及原A不可改早。仅存储/import/export时间不是市场界限，不能把2026本地导入机械max进2023–2024研究A；原值仍单独保存、绝不抹除或反填原空imported_at/batch_id列。

四类信息始终分开：原pipeline_import_bound（及可证原imported_at）、实际市场first_seen/版本界限（无证据为UNKNOWN）、派生reconstructed A、原manifest/export时钟。本公式只决定研究A，不直接改变原CSV的available_at语义。它也不授任何公共schema改动。

仅作为算术示例：若未来证据真的给某D的E(D)=当地15:00，固定缓冲会到次自然日15:00；这不是当前SC E(D)证据。其最早可消费决策还要按真实SC会话、发布先于预测槽、原新鲜度和其他门检查，不能统称固定晚一个交易日或必在D+2夜盘可用。M1 `daily_background_max_trading_days=1` 仍按实际已完成交易日判断，正常休市不按自然日误报；本政策不延长新鲜度、不缩短延迟救数据，真实产生STALE/INSUFFICIENT须照实报告。

合法输入仍须 `MarketBar.complete=True`、`end_nano <= decision_nano`、`A_research <= decision_nano` 及全部原身份/质量/数据门。d=A-1ns不可见；d=A仅满足时间边界，不代表可成交或合格forecast。intraday当前交易日最终日线过滤保留，预测/发布必须符合下一未开始完整槽与原有效期规则，真实持仓/标签/尾段不得为延迟裁短。

SC原76条edb_free行及其他未适用行留在原数据/审计人口；两个政策都不覆盖它们。实际消费者若依赖其内容，保持具体不具备条件，不能默删、不动分母、不移用tqsdk或别品种时钟。

**将来实现时沿原验证义务增加的明确反例**

全部只在真实获准的SC实现中以SYNTHETIC反例验证，本次不执行：非午夜；无时区；nano与ts矛盾；已有trading_day与当地日期冲突；edb_free或别品种越入；source格式异常/缺失或symbol/duration不符；把reference_calendar_date一致当官方历史会话；午夜到午夜+86400伪造区间；每条VERIFIED但遗漏D应有夜盘；UNKNOWN/SCENARIO混入或被删后称完整；错产品/D/calendar_version；空集合max；日历完整却bar不complete；2026导入/created/export时钟误纳入B_market；同一版本真实更晚修订被忽略；A-1ns/A边界与当前交易日过滤。保留不变正例，明确拒绝原因，无关异常不算反例通过；原完整人口/prefix/tail与public readback检查照常。

**前瞻开发路由与当前有证据停点**

本轮Root明确传达用户最新要求改回原Sol/max owner开发路线，优先于原稿及本轮读到的10月25日磁盘ZCode开发文字；本作者没有修改那三份规则文件，也不声称它们已经更新。后续SC实际开发/修复归原R1实际Sol owner，模型 `gpt-6.1-sol`、reasoning `max`，按原owner连续性接续；Root负责核实真实身份、无并写、有效写集及已消费授权。不得把本次ZCode咨询/成果reviewer变成开发者，或假称一个旧ZCode SID就是原Sol身份。原作者仍Astra，独立成果硬审仍为未参与开发的ZCode GLM-5.3/max。历史作者、旧SID/cwd/store、失败/consumption保持，模型/凭据等持久配置无改动授权。

两个政策的计划前置至此完成；不再发相同意见、等待计划PASS或重开数据设计。当前有证据停点仍为：92候选listing_at全空、完整census未证、cache expiry不构成法律X、T/R未冻结、SC历史sessions缺失、完整prefix/tail与completion未证。故本轮 `actual_A_rows=0 / INPUT_NOT_DELIVERED / product_write_set=[] / consumer=NOT_CREATED / raw_bound=NOT_RUN / fit=NOT_RUN`。政策生效只确定将来合法生产者的解释参数，不把这些缺口改成READY。

下游最小动作是原R1 owner只消费已经在其合法范围内、能实际补上述缺口的具名接受证据：完整SC census与潜在成员L/X、适用历史会话/休市/夜盘归属及prefix/tail/完成关系。由Root沿既有交接提供exact FileRef、pointer和接受范围；没有这样的新增证据时，本SC产品物化就停在此处，不重扫同一五文件、生成空consumer或复制同值事实包。此停点不暂停其他ready模块，也不发新来源搜索、查询、URL、DB、SDK或下载权限。

真实前置满足后，沿原SC裁定的条件source+consumer写集、原格式派生成功分支、全量public readback、必要验证/实际hash freeze直接推进，由原Sol owner执行，再独立ZCode硬审；工程FAIL回同一实际Sol owner追加修复。Root按实际输入冻结exact路径与运行admission，不以本文件授权未来未知文件或运行命令。C/L/X→T/R/source→本品种日历/质量/完成/A→便宜raw-bound→未拒绝才mixed/envelope→规则与尾段→完整读回/检查/硬审→M5 admission不变。

保留W、same312、252/60/60及其有效后继、全部horizons/durations、成熟/人口、prefix/tail、200/30和500/60、seen/holdout及RB→CU→AU→M→I→SC自然顺序；不重跑RB312/153/51，不续发旧CU/AU/M请求，不把只限本包无I扩大为全机不存在。T7/M5/N8/DISPATCH活动冻结和消费不变。

**证据与出版纪律**

本轮仅精确读取已审提案/附件、咨询终态与M1指定条款，未作结构查询或新的源行扫描。沿原coverage：docs excluded、外部候选outside_project，直接bounded文件读取，不声称图覆盖或全库证明。原SC-ASSESSMENT中截断、native1空行解析、其后继处置及编排错误均保留；本轮不把它们重写成产品成功。独立咨询native0和本稿出版native0也不发产品PASS。

AGENTS7/8、20261004 rg/Grok及manifest-recovery002、20261016 custody纪律保持：仅optional rg1且空stderr可NO_MATCH；native/工具/custody/artifact/verdict分轴；Path边界归一化；未来输出只查元数据不先Read；完整内容后exclusive创建，旧失败保留；不自哈希、不pin开放流。引用的detached流已有真实终态，本稿没有固定其仍运行的自身输出。

本次唯一作者输出为 `SC-FINAL-DISPOSITION-001.md`，下方输入只读保持；没有规则修改、产品实现、测试、fit、网络、SDK、DB、子代理/CLI派发、凭据或.env及其存在性探测、模型配置修改。安全为 `enable_trading=False / observer=hold / broker=NOT_RUN / real send-cancel=0/0`。


实际固定输入：

[
  {
    "role": "reviewed_author_proposal",
    "path": "C:/AKWorktrees/aktr-goal-20261026/astra-bip1-sc-input-20261028-001/SC-DISPOSITION.md",
    "bytes": 13546,
    "sha256": "d1ca10acdc3b9976825afd7b4e7a0844212955280841c37b0d5947c1a26771fb"
  },
  {
    "role": "reviewed_author_assessment",
    "path": "C:/AKWorktrees/aktr-goal-20261026/astra-bip1-sc-input-20261028-001/SC-ASSESSMENT.json",
    "bytes": 18147,
    "sha256": "8d895a5ec02233209a2fb5e07bec84f8bc8f16002c28af1d972cf82600d346cb"
  },
  {
    "role": "independent_advice",
    "path": "C:/AKWorktrees/aktr-goal-20261026/zcode-dispatch/bip1-sc-plan-advice-001/OWNER-REPORT.md",
    "bytes": 6286,
    "sha256": "c7380cb4c17769314f6c4b157bd899c2d96b37a9cd4b64eec5f52fb2d89db4b4"
  },
  {
    "role": "advice_terminal",
    "path": "C:/AKWorktrees/aktr-goal-20261026/zcode-dispatch/bip1-sc-plan-advice-001/TERMINAL.json",
    "bytes": 328,
    "sha256": "efa3babf6d7b14b7b6f539063137da13c0c54afe22f1a4fd77a0af46b1236ffe"
  },
  {
    "role": "advice_result",
    "path": "C:/AKWorktrees/aktr-goal-20261026/zcode-dispatch/bip1-sc-plan-advice-001/RESULT.json",
    "bytes": 834,
    "sha256": "3315ab4eee3689bac731e85427057039a6667d06b4ab35034edb1cd56eb12286"
  },
  {
    "role": "detached_closed_supervisor",
    "path": "C:/AKWorktrees/aktr-goal-20261026/zcode-dispatch/bip1-sc-plan-advice-001/ROOT-DETACHED.stdout.txt",
    "bytes": 1070,
    "sha256": "5147e868e9f903d4f8137b49f041a40fa08379d3fb15d08808fd46c8aeb205a1"
  },
  {
    "role": "M1_contract",
    "path": "D:/Projects/AK-trading/docs/devplan-v2/01-M1-共用基础与时钟契约.md",
    "bytes": 70562,
    "sha256": "b736cc9bf433b910bfb3972b0d4d6f5c7b921a14d62037461d1c0ecd35ef584a"
  },
  {
    "role": "disk_AGENTS_20261025_read_unchanged",
    "path": "D:/Projects/AK-trading/AGENTS.md",
    "bytes": 21520,
    "sha256": "224d213d60107599a6590b64394215b92a1b9c109072eed8bb13eb79f6327bf2"
  },
  {
    "role": "disk_roles_20261025_read_unchanged",
    "path": "D:/Projects/AK-trading/PROJECT-MODEL-ROLES.md",
    "bytes": 5172,
    "sha256": "e2766d80af5d4df4add39ab31d94d21dd94cf3721dd079267c6dd3092f4f7b06"
  },
  {
    "role": "disk_workflow_20261025_read_unchanged",
    "path": "D:/Projects/AK-trading/PROJECT-WORKFLOW-RULES.md",
    "bytes": 4999,
    "sha256": "5d9003614c82b29f2d566f32ec5ac0e1b53b1b1ba575f67e072422f6c34a9ca6"
  }
]

原作者发布UTC：2026-10-28T18:42:04.227395+00:00。最终状态EFFECTIVE；产品verdict=NOT_ISSUED。
