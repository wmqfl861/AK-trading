**剩余技术阻塞的有限修复裁定与实施计划**

本次批准修复**通用合同锁清单与T7夹具清单占用同一路径的契约冲突，并补齐真实、可验证的通用清单生产步骤**。这项源码修复可以实施；它不恢复H5/H6/H7/H8作业，也不代表研究或正向冷链验收通过。

旧`r4-lock-ruling03`禁止的是在当时批次中擅自迁移清单。本次已有新的有限修复授权，因此仅撤销其对**下述固定路径分离和清单生产修复**的范围限制。旧失败、锁与研究分开验收、禁止假输入及禁止发布输入包和锁的规则全部保留。

**一、已确认根因及修复边界**

已读取`r6-blocker-repro01/RESULT.json`及当前精确源码。真实`build_contract_lock`退出1，停在通用清单根字段检查，未生成锁。

当前两个消费者的合同不同：

- `artifacts.py:133–173、1132–1262`要求`ak_probability_acceptance_input_manifest_v1`，完整绑定配方、五类输入、分区、选择、策略绑定、源码和测试附件。
- `tests/v2_ep1_t7_support.py:487–618`在同一路径生产和消费`ak_ep1_t7_acceptance_input_v1`，其结构是T7输入及历史源码冻结记录。
- M1测试的临时最小世界仅验证机械合同，不能替代真实T7材料。

另有两项实际发现必须保留：

1. 旧清单列出的六份附件均存在，长度和原始SHA256全部匹配；不能称输入丢失。
2. 旧清单64项源码绑定中，24项与当前工作树不同；当前源码范围另有5个新增文件。旧`recipe.json`也不是合法JSON：旧冻结器对整个JSON执行逗号替换，在`shape`字符串内插入了裸换行。标准JSON读取实际报告`Invalid control character`，不是产品构建成功。

因此，**不能改schema字符串，不能把旧`code_files`批量刷新后称原验收有效，不能借用临时测试世界**。本次采取新清单包装真实旧材料、明确当前源码绑定的路线。

本次图工具已恢复返回，但相关索引为旧元数据；本裁定依据实际源码，不声称图覆盖完整。

**二、采用两个固定命名空间，旧消费者保持原合同**

**ADOPTED：固定路径分离。**

| 对象 | 生效路径与处理 |
|---|---|
| 旧T7清单 | 保留`tests/fixtures/v2/ep1_t7/acceptance-input-manifest.json`及其schema、六份附件、历史源码哈希，全部原字节不变 |
| 新通用清单F | 固定为`tests/fixtures/v2/ep1_t7_contract/acceptance-input-manifest.json` |
| 新制包附件 | 仅在上述新目录生成合法配方副本及真实选择／计划元数据 |
| 新合同锁 | 仓库外、本地、新运行标识；不进入两个输入目录 |

`ACCEPTANCE_INPUT_MANIFEST_PATH`改为新固定路径；不增加任意manifest参数，不自动尝试旧路径，不按schema猜测消费者。通用schema ID、根字段、锁schema、公共API签名及版本规则保持原样。

通用验证器继续执行全部严格检查，并对**旧T7目录和新通用目录两处**执行完整文件清单检查。旧目录中的T7清单本身也必须作为原始附件列入F；新F不得列自身。不能通过迁移路径撤掉旧目录的“未列文件拒绝”。

T7原读写函数继续使用旧路径，不消费新F，不把通用验证成功替代其历史源码验证。旧T7的`code_drift`及冷链失败仍可真实出现。

唯一预先批准的冻结断言修改是：

- `tests/test_v2_m1_payoff_path_contracts.py::test_t7_acceptance_lock_registration_wiring`中的路径期待，从`ep1_t7/acceptance-input-manifest.json`改为`ep1_t7_contract/acceptance-input-manifest.json`。
- 原因是两个不同schema分离固定命名空间；其余schema、required tests、非循环和源码范围断言不得放宽。

**三、单一负责人及精确写集**

由原T7／清单相关ZCode负责人接续；不能接续时，Root显式交接给一位ZCode负责人。该负责人承担整个修复，避免把路径与生产者拆给不同写者。另一独立ZCode GLM-5.3/max审核，不参与实现。

产品和测试写集仅为：

1. `src/ak_trading/v2/artifacts.py`：固定路径变更、两处目录的完整附件校验；其余验证强度保持。
2. `tests/test_v2_m1_artifacts.py`：路径分离、双目录完整性及真实清单连接回归。
3. `tests/test_v2_m1_payoff_path_contracts.py`：上述明确路径断言及相应连接反例。
4. 新增`scripts/prepare_t7_contract_inputs.py`。
5. 新增`tests/test_prepare_t7_contract_inputs.py`。

`tests/v2_ep1_t7_support.py`及旧T7输入**只读**；本次不调用其`freeze`命令，不修写其历史配方文件，不改价格、人口、拟合参数或正向期待。新脚本避免旧序列化错误即可，不顺带重构旧生产者。

新增清单和输入附件只在独立本地源码副本中生成，不属于可推送写集。Root只集成接受源码并维护CURRENT-STATUS。G1保持一条关键开发、一条独立审核、一条支撑线；支撑负责既有捕获和集成，不另开产品模块。

**四、真实制包步骤与字段来源**

接口固定为：

```text
python -B -X utf8 scripts/prepare_t7_contract_inputs.py --project-root <独立源码副本>
```

输出位置由代码固定，不接受任意清单路径、任意输入列表或强制覆盖。核实导入根与`project-root`一致。目标目录必须是新目录；输入不完整、源码来源不符或已有输出时拒绝，不自动补造。

脚本只准备输入元数据。不得调用模型拟合、`outer_stand_in`、`execute_positive_smoke`、实验注册、研究入口、回放或holdout消费。

字段按以下来源构造：

| F内容 | 必须采用的实际来源 |
|---|---|
| `dataset_kind`、`labels`、seed | 旧T7清单与`POSITIVE_RECIPE`一致的SYNTHETIC／SCENARIO、seed7；不是历史研究输入 |
| `generation.recipe` | 新目录中的合法JSON配方副本，内容来自未修改的`POSITIVE_RECIPE` |
| minute／daily／calendar | 原`inputs/bars-minute.json`、`bars-daily.json`、`sessions.json` |
| rules／costs | 均引用原`inputs/rule.json`；该文件实际包含手续费、保证金及滑点字段，不另造空costs文件 |
| `splits` | 原`partition-params.json`、实际日历及现有分区函数；保留433日、426外层TRAIN及原分区边界 |
| `selection` | `positive_config`／`declare_plan_set`及生产`_payoff_training_plans`的真实声明；选择规则取实际协议常量 |
| `bindings` | 当前真实`build_strategy_spec`、`select_exit_binding`生成的完整对象及内容身份，不填占位哈希 |
| `files` | 两个目录的全部实际附件、完整源码floor、两个runner、全部required tests，以及本脚本、其测试和真实生产依赖 |

制包必须先完成以下验证：

- 旧清单原始SHA256为`01fa369f…5d4e0a`；逐项验证六份附件的原长度与完整哈希。固定历史标识不能由“当前读取值”自证。
- 未修改的支持源SHA256为`7f164017…8eb29`。用其真实`build_positive_world()`和既有序列化函数在内存中重建输入，核对原日历、规则及两频率bar的完整字节；不拟合。任何差异必须停止，不能重写输入使之匹配。
- 对非法旧recipe，不使用宽松JSON解析掩盖错误。由原源码常量重现旧序列化表达式，并证明所得字节等于旧recipe；再用`canonical_json_bytes(POSITIVE_RECIPE)`生成新合法副本。只改变序列化表示，不改变配方字段、recipe_id或种子。旧文件仍作为原始附件绑定。
- 旧`derived_identity.bundle_manifest_sha256`是夹具逻辑身份，不能冒称原始输入文件的内容清单哈希；两者分别保留。

允许调用已有**纯声明**辅助生成计划元数据，并使用真实codec写入`payoff-training-plans.json`。不得为了获得七计划／两策略政策而调整窗口、现金、参数或筛选结果。应核对原规定的七个计划、两项run policy；任何不一致都作为实际制包阻塞报告。

`positive_config`依赖的既有测试配置文件属于需要明确绑定的来源，不能从记忆补齐其中的金额或参数。开发工单必须明确该**测试材料**的读取范围；此计划不授权读取运行、服务、模型或凭据配置。若该依赖不在已授权材料中，先完成其它步骤并报告精确缺项，不虚构默认值。

新F保留原九个根字段；配方、计划文件及必要声明均列入附件。禁止嵌入最终锁哈希、审核报告哈希、readiness或运行输出。旧清单、源码和输入流向F，F及独立报告流向锁；不得反向形成循环。

所有JSON先完整序列化、严格解析验证，再发布新目录。对附件用原始字节哈希，只有F自身沿用现有LF归一化规则。拒绝路径逃逸和可写别名；不把源码树外文件偷偷复制进来。

**五、源码漂移的作者裁定**

**批准当前已验收基点作为新F的源码来源；不批准修改旧T7清单的任何源码期待。** 下列24项逐项确认为“历史绑定与当前绑定不同”，允许在新F分别绑定当前实际字节；这不声称旧新实现等价，也不转移旧冷链接受状态。

以下为哈希前缀索引，完整值必须从原清单与冻结源码程序化取得并列入现有外置交付证据：

| 文件；未注明者相对`src/ak_trading/v2/` | 历史 → 本次读取的当前值 |
|---|---|
| `scripts/research_probability_v2.py` | `33351e7acd00` → `580c2f32d2f6` |
| `artifacts.py` | `3ee742013664` → `7fc5b8418714` |
| `clock.py` | `793771e4633a` → `1ee22027c648` |
| `data.py` | `16bb89c5a78c` → `426aea6c733c` |
| `decision/gates.py` | `4b3db13f5c42` → `490e1c970d90` |
| `decision/payoff_paths.py` | `c5993cccea6b` → `4077f0244bb4` |
| `decision/payoffs.py` | `2f22820639da` → `7d6d3b96e7e3` |
| `decision/pipeline.py` | `328f342b7782` → `33d37167e826` |
| `decision/replay.py` | `f196a593270d` → `fcba09e0c766` |
| `export.py` | `fe16e2ee996b` → `0d0e56da344b` |
| `factor_specs.py` | `4f805702637a` → `deed3712ea8e` |
| `factors.py` | `bf20860d2477` → `c1e435c139d3` |
| `probability/calibration.py` | `49757c0993be` → `962eb84cc2e8` |
| `probability/models.py` | `1e9197ddd678` → `3dc31eed9363` |
| `probability/regimes.py` | `d646588f6efc` → `010c7df0dd19` |
| `strategies/breakout.py` | `829efdd024c9` → `e98f5bf8e06b` |
| `strategies/grid.py` | `302fdef41289` → `0796314eb2d4` |
| `strategies/martingale.py` | `1052ba633aed` → `cb08bb0d90f0` |
| `strategies/pullback.py` | `b6101923ab6b` → `fd0706ad3d62` |
| `strategies/registry.py` | `7ff35ba5b811` → `f491ff80db16` |
| `strategies/reversion.py` | `abfd41ca0368` → `0355ce272151` |
| `strategies/trend.py` | `68efde9bb844` → `68a89f4c49e7` |
| `validation/experiments.py` | `700294c9a7f6` → `e5c4f3159a9f` |
| `tests/test_v2_m5_payoff_integration.py` | `52a62b1f7dea` → `069b620a25f3` |

新增源码floor成员`_fold_records.py`以及`validation/{acquisition_feed,forward_paper,forward_paper_feed,risk_semantics}.py`必须完整列入；绑定文件不等于授权运行这些模块。

上述理由统一且明确：**新F记录新的已验收源码快照，旧清单继续记录旧快照**。本批允许改动的文件再按最终冻结字节绑定并接受独立审核。未授权源码变化不能混入；若副本与上述工作树只差换行，须逐文件证明并保留两个真实哈希，禁止批量规范化或盲目刷新。

**六、从红到绿的最小验证与锁生成顺序**

保留原raw1复现。修复后的无新F源码副本应报告`acceptance_input_manifest_not_frozen`，不能自动把旧T7 schema当成新F接受。

必要验证分三层：

1. **源码及负例。** 原M1合同／工件测试继续执行；新增错误schema放错路径、两目录未列文件、附件同长度篡改、缺失源码floor、配方缺失／不一致、bool种子、伪绑定、路径逃逸及循环字段反例。证明旧T7消费者仍读原文件、旧源码漂移仍被报告。为准备脚本设置禁止拟合／注册／回放的测试断言，证明其没有昂贵阶段或越界写入。
2. **真实输入准备。** 在完整当前源码副本上使用真实六份T7附件运行新脚本；不得调用`_stage_acceptance_world`或`_mirror_lock_tree`替代。严格验证F、全部来源及输入零变化。返回0只表示制包成功，不是模型、冷链或研究PASS。
3. **真实报告绑定锁。** 源码和F冻结，独立审核其实际来源与契约并封存报告后，调用现有`build_contract_lock(project_root, reviewed_revision, reviewer_report_sha256)`。序列化到新的仓库外路径，执行`verify_contract_lock_with_report(lock_path, project_root, report_path)`。必须实际成功，不能只看返回字典或CLI0。

锁绑定先完成的独立源码／输入报告；最终验收报告再引用该报告与锁，不回写已绑定报告形成循环。生成F后源码、required tests或附件有变化，必须形成新冻结版本，旧锁不能继续引用。

完整回归复用`full-suite-windows-20261111-r5.json`作为BEFORE：full6254项、5994P/167F/60E/33skip/8CE，四成员6283项、6017P/173F。固定原环境，先做定向验证，再由独立审核线控制一次完整候选AFTER及原三探针；该次同时承担集成和独立全量验证。保留全量所需夹具冷链许可，**不启动新的正式H5冷链**。

K03必须证明完整人口及零新增failed／phase-error／CE；稳定标签沿用，不新增桥接、skip或xfail。旧失败不能靠修改期待消除。实际缺陷修复导致的失败减少须有对应证据。最终独立产品PASS、接受源码推送main及实际HEAD绿色CI之后，才报告源码修复完成。

**七、可以解除与仍然保留的阻塞**

本次目标是取得：

- 源码层面：两个清单不再抢占固定路径，有真实且可重复验证的通用清单生产者。
- 本地工件层面：在确切冻结源码、真实输入和独立报告上，真实锁build／verify成功。

两者分别报告。若制包因实际材料缺失而不能完成，最短缺口清单只列：**具体文件／字段、应有来源、已验证到哪一步及负责人**。不得为了输出绿色结果生成占位输入或放松验证。当前已验证存在的六份附件无需重新索取；选择／绑定及实际生产依赖尚须按上述流程验证，本文不预先宣称它们已生成成功。

以下不能通过本次源码修复消除：

- H7尚未选择正式输入与配置。r1/r2仍为provisional，规则缺费率／参与率或可用性问题不得自动修填；实际预检仍NOT_RUN。
- H5旧engineering FAIL及节点23P/10setupE，不因K01或锁修复升级；正式重跑需要后续明确授权。
- H8 profile09的raw2／磁盘耗尽／`phases=[]`保持FAILED。资源恢复不等于profile完成，不恢复09或启动10。
- H6/H7、研究选择与最终holdout继续暂缓。

本计划完成一次独立意见及原作者处置后，Root立即交ZCode实施；不追求计划PASS，不开启一般性蓝图或重复历史调查。候选、输入包及锁全部留本地，只发布接受的安全源码；公开main缺少本地F时仍可诚实报告未准备工件，不冒称可从裸克隆直接运行研究。

遵守G1–G4及AGENTS§7–8、两份20261004事故和manifest-recovery-002：真实退出与各层结果分别保留；Path正规化，JSON完整验证后原子发布，不固定活动流。状态只维护CURRENT-STATUS。保持`enable_trading=False / observer=hold / broker=NOT_RUN / send=cancel=0`，不触凭据、运行配置、规则、外审、服务、DB、系统或UI，不启动任何未授权长作业。

---

**原作者意见处置与生效补充**

本补充立即生效，`r6-blocker-plan01/PLAN.md`除下述澄清外继续有效。已核对原计划、独立FEEDBACK、`ACTUAL-TREE-PINS.json`、r6准备记录及授权测试材料。来源争议已解决，Root立即交原ZCode负责人实施；不等待计划PASS，不追加意见轮次。制包运行前落实输入pins、导入根与输出保护检查，相关测试属于本次实施内容。

1. **F1 — PARTIALLY_ADOPTED。** 采纳必须明确来源并核验原始字节的要求；不采纳“原计划当前pins错误”及重钉旧副本的建议。七树记录证明当前main与`C:/AKR61112/before`、`candidate`一致：旧T7清单为8421字节、`01fa369f…5d4e0a`，support为29951字节、`7f164017…8eb29`，原计划绑定正确。审核意见实际核查的是旧r4/r5的CRLF副本，不能代表本次来源。完整SHA按`ACTUAL-TREE-PINS.json`逐项读取比较，禁止仅比较前缀。FEEDBACK原文及旧树保持冻结；换行差异保留为真实来源差异，不伪称哈希等价。

2. **F2 — ADOPTED。** 原计划24项表格仅为**修复前历史漂移记录**，不是新F的最终哈希表。新F必须绑定本次源码、测试和真实依赖全部冻结后的实际完整SHA；包括本次修改及新增的五个文件。历史表不能替代最终计算，也不授权改写旧T7的`code_files`。

3. **F3 — ADOPTED。** 明确只读授权`tests/fixtures/v2/m5_research.json`作为本次测试材料，允许`positive_config`按原调用路径读取。当前r6候选实际为73536字节，SHA256为：
   `18be290be95b52723400d19e19a122a9e4ff7022b9f4b2c9344e922a576ccce6`。
   已核验字节哈希及JSON可解析；与真实生产声明的契合仍由制包和定向测试证明。该文件及全部实际声明依赖须绑定新F；不得据此寻找或读取真实运行、服务、模型或凭据配置，不补造选择、资金或绑定参数。

4. **F4 — ADOPTED。** 顺序固定为：**完成实现及定向验证 → 冻结执行源码和测试 → 从冻结字节生成新F及附件 → 独立审核源码与真实输入、封存报告 → 真实build并序列化新锁 → `verify_contract_lock_with_report`**。F不列自身，不绑定最终锁或审核报告，保持非循环。生成后任何相关源码、测试或附件变化，须使用新的完整独立副本和新attempt重新生成；不得覆盖旧F、锁、报告或失败证据。

5. **F5 — PARTIALLY_ADOPTED。** 允许一次制包中的原433日日历、bar枚举、字节重建核对及真实纯计划声明；不把这些工作误称为无计算成本。测试必须在真实制包路径上设置可检测的禁调用断言，证明未进入fit、outer stand-in、register、replay、cold或研究阶段。仅执行枚举不构成越界。保留G3每job一次启动资源检查和一个宽松墙钟上限，不新增逐函数、逐工具或累计秒预算。

6. **F6 — ADOPTED。** 明确断言旧T7清单本身作为原始附件进入F。删除其条目，即使其余附件齐全，也必须因双目录完整性检查失败；新F自身仍排除。旧清单及六份附件原字节不变。

7. **F7 — ADOPTED。** 在既定五文件测试写集内证明：通用锁消费者只使用新固定路径，旧T7消费者仍使用旧路径，两者不猜测schema、不回退串用。不得为落实此意见修改support或扩大旧消费者测试的写集；旧`code_drift`继续如实报告。

8. **F8 — PARTIALLY_ADOPTED。** 采纳逻辑身份与内容身份须可审计地区分；不新增非法根字段或另造聚合身份。F严格保留原九根字段。夹具逻辑身份由已列入F的原T7清单保留；各文件内容身份由F现有`files`条目的原始字节SHA表达；F自身身份按既有规则在外部计算并供锁绑定。不得把逻辑bundle标识改称文件内容哈希，也不得形成自引用。

9. **F9 — ADOPTED。** 对五个新增floor成员及整个实际源码floor逐项验证存在性与最终字节，缺项立即真实失败，不能占位或省略。绑定这些文件不授权运行其所属模块。

10. **F10 — PARTIALLY_ADOPTED。** 采纳固定新路径的行为验证；不采纳给既有异常文字追加路径的建议。保留精确错误`acceptance_input_manifest_not_frozen`。测试分别断言新路径常量，以及“旧T7清单存在、新F不存在”时仍产生该错误；随后用真实新F验证严格校验路径，不改冻结异常合同。

唯一实施目标为`C:/AKR61112/candidate`；`before`、`source-before.zip`、PREPARATION及旧r4/r5材料保持只读。精确源码／测试写集仍只有：

- `src/ak_trading/v2/artifacts.py`
- `tests/test_v2_m1_artifacts.py`
- `tests/test_v2_m1_payoff_path_contracts.py`
- `scripts/prepare_t7_contract_inputs.py`
- `tests/test_prepare_t7_contract_inputs.py`

新F及附件仅在本地固定`ep1_t7_contract`目录生成，锁在仓库外新路径生成；候选、输入和锁均不推送。复用r5真实完整baseline及固定环境，保留原来源记录；全量人口完整、零新增failed／phase-error／CE、独立无条件硬审、接受安全源码main与绿色CI要求不变。

G1–G4及原事故规则继续生效；保留全部原始退出和失败，Path正规化、完整JSON验证后原子发布，不钉活动流。H5/H6/H7/H8不恢复，正式研究、holdout及其它排除范围不启动。保持`enable_trading=False / observer=hold / broker=NOT_RUN / send=cancel=0`。源码修复、本地锁验证和研究状态分别报告，不转移旧失败的验收结论。

---

## 原 Astra 作者有效补充 ruling06（原文）

原作者：Astra/max；来源：`C:/AKH1107/cli-dispatch/r6-infiny-ruling06/PLAN.md`；原始 SHA-256：`58b271bd75287b7070b15e2a167696b0c38148d7f1a8e2d2fcd32b50948ff1a2`。下列内容按原作者发布文本收录；历史角色称谓保留，本批直接用户分工见交接文档。

**ADOPTED：立即授权原 ZCode owner 修复这两处测试前置条件。** 本补充限于既有 r6 验收阻塞，不启动新模块、不要求计划PASS。after02 的原生退出1、严格比较新增失败2/0/0及全量FAIL保留；不再运行未修改的 repair03 全量来寻求通过。

1. **诊断与意见处置。** 采纳实际边界诊断，不采纳“已知不稳定人口”豁免。h21正常对照通过，而真实CONSUMED写入后追加0.45秒延迟使400ms命令到期，生产fence返回EXPIRED并记录NO_CALL；这支持修复测试对写盘／调度速度的隐含依赖。m01的12次原模式诊断中，9次在启动器wait完成后仍遇到真实字节锁拒绝；稍后的进程死亡观察不能反推此前锁已释放。该证据支持修复退出同步，尚不证明生产锁有缺陷。两项孤立通过记录均不代表已修复。

   九个基准失败未在after02复现，上一裁定的条件未触发，**本次不修改任何benchmark文件**。

2. **负责人、目标和精确写集。** Root按本工单核实原owner的同一SID、literal cwd及固定store，立即从repair03冻结字节建立无可写别名的`C:/AKR61112/repair04`。repair03及既有报告、F、锁、baseline保持不变。本次唯一新增写集为：

   - `tests/test_infiny_native_integration.py`：h21测试内时钟控制、持久化顺序观察及对应反例。
   - `tests/test_infiny_native_lifecycle.py`：m01测试内持锁进程握手、退出／资源释放同步及对应反例。

   所需小辅助限定在各自测试文件内；当前无需修改`tests/infiny_native_fakes.py`。不修改`AKExecutionBridge.py`、`offline_guard.py`、生产门禁、锁实现或先前五文件修复。

3. **h21：控制合成命令时间，保持真实执行链。** 保留原未武装零调用分支及真实合成arming流程。正向命令仍由现有`armed_dispatch`生成，TTL严格保持400ms，命令及offer期限不续期、不重写。

   在该命令进入`_receive_dispatch`至`_pump_once`返回的窄作用域内，以隔离客户端模块现有`_now_utc`为测试接缝：回调线程使用从该命令`issued_at`取得的固定有效时间；其它线程继续调用原时钟。用局部context确保异常时也恢复。不修改系统时钟、全局datetime、monotonic或线程等待，不关闭arming、freshness、admission或post-fsync fence。

   原断言全部保留；可在索引`invoked[0]`前增加长度断言，避免IndexError掩盖真正失败。必须仍证明一次模拟send、同步`on_trade`在send返回前完成持久化、event_gap为false。持久化顺序可通过调用真实`append_event`后记录返回事件来观察，并读取真实提交记录；不能伪造写入成功或用稍后上传代替“返回前已持久化”。

   保留原延迟反例作为红证据；新测试在真实CONSUMED写入后加入同样0.45秒墙钟延迟，受控命令时间不前进时仍完成原正向断言。另用独立新用例在真实CONSUMED返回后将合成时间推进到`expires_at`及其后，要求真实fence拒绝、模拟send为0、保留CONSUMED→NO_CALL；原专用到期测试也继续执行。不得在原正向用例上参数化改名而丢失旧nodeid。

4. **m01：等待实际持锁者及OS资源释放。** 保留同一`sys.executable`、离线launcher和guard。子脚本在真实`msvcrt.locking`成功后，通过原管道发送`LOCKED`及实际`os.getpid()`。在持锁者仍活着时绑定其进程身份／句柄，核实属于本测试启动的解释器链；不能只信`Popen.pid`，不能以换解释器绕过guard。

   原“持锁时拒绝、未running、同一个client.lock文件存在”断言保持。终止阶段只处理本测试核准的启动器及实际持锁解释器，等待实际进程退出。两者等待及必要的OS锁释放同步**共用原5秒期限**，不增加超时。若进程已退出但资源仍在收尾，可在测试侧对同一路径、同一字节范围做有界非阻塞探测：只对已确认的锁占用重试，成功立即解锁并关闭探测fd；其它错误直接失败。不得循环调用生产`contender.on_start()`直到成功，不修改生产锁的轮询／拒绝行为，不用固定sleep冒充释放证明。

   确认退出及释放后，仅执行原重启动作，断言running、原slot路径及warm实例的持久身份不变，并完成clean stop。所有分支均清理本测试进程、句柄和桥接实例；超时仍是真实失败。反例须覆盖实际持锁者尚未退出时不能提前宣布就绪，以及实际退出后同槽恢复；保留原诊断9F/3P，不重跑它来筛选绿色样本。

5. **审核、绑定与最终验收。** 两文件定向验证、冻结后立即交独立ZCode审核：重跑两个原测试、上述延迟／到期／真实锁反例、受影响文件测试、必要的既有source/input检查及原三份外部策略探针。保留所有旧身份、33项既有skip、冻结期待、阈值和r5 baseline；不加skip、xfail、比较别名或豁免。

   已读取当前F的119项实际绑定，两个新增写集文件均不在其中。因此此次两文件修复**不重生成F**，验证原F及附件字节不变即可；未变化的五文件source/input证据按原范围复用。新的独立报告必须明确接受这两项测试修复及完整候选来源，再按实际新revision和报告SHA生成、验证新的本地报告绑定锁。旧报告／锁继续只代表原冻结版本，不回写或盲刷。

   最终冻结版本由独立审核线执行**一次完整AFTER**，与原r5 baseline严格比较，新增failed／phase-error／CE必须为0/0/0且人口完整。定向通过及锁验证不能代替此条件。满足后仍须独立无条件产品PASS、精确main集成和push、实际HEAD绿色CI；本文不签发产品PASS。若仍失败，依据具体失败回原owner诊断修复，不用连续全量重跑替代修复。

G1–G4及原安全范围不变；候选、输入和锁留本地，H5正式作业/H6/H7/H8/N8/DISPATCH/Forward/paper/研究/holdout不启动。模拟send与真实计数分开，真实仍为`False / hold / NOT_RUN / send=cancel=0`。沿用AGENTS§7–8、两份20261004事故及manifest-recovery-002规则，保留原始退出，Path正规化、完整JSON验证后原子发布，不固定活动日志。


---

## 原 Astra 作者有效补充 ruling07（原文）

原作者：Astra/max；来源：`C:/AKH1107/cli-dispatch/r6-infiny-ruling07/PLAN.md`；原始 SHA-256：`4a78c07fbcec373b98daae7cf4597234fecc35bc22dda00f54c7767bc10433e8`。下列内容按原作者发布文本收录；历史角色称谓保留，本批直接用户分工见交接文档。

**本补充立即生效：owner05尚未完成ruling06要求，原时钟与持久化观察必须补齐；另授权同一两文件内的arming夹具同步修复。** 不设新计划PASS门槛。保留54P/1F/raw1、诊断06/07的raw1及所有旧证据，不采纳“已接受flakiness”的解释。

1. **证据与边界。** 已核对当前规则、ruling06、DELIVERY/FREEZE及实际源码，两文件SHA与freeze04一致。诊断07通过真实发布函数构造了request/challenge与内存不一致，继而得到`ARMING_CHALLENGE_MISMATCH`；它证明一种真实机制，不能断言未插桩的组合运行使用了完全相同的调度。本次消除手动夹具与后台pump的双重驱动，不宣称修复了生产函数的任意并发调用。

   本补充立即传原owner；当前独立review06照常完成。**待其终态解除审核写冻结后**，原owner按原SID、literal cwd及固定store，在新的完整、无可写别名successor实施。repair03/04及旧证据永不覆盖。写集仍仅：
   - `tests/test_infiny_native_integration.py`
   - `tests/test_infiny_native_lifecycle.py`

   不修改生产bridge、guard、共享fakes、benchmark或原五文件修复；不改变其它测试的默认夹具行为。

2. **采用h21专用、每bridge的RLock串行夹具。** 在原h21及其新增慢写、到期用例加载的**隔离客户端类上**，于任何`on_start`之前安装测试包装：所有`_process_arming_files`调用先取得该实例唯一的RLock，再完整调用原函数。实例登记和锁创建必须线程安全；可用局部`on_start`委托包装在调用真实`on_start`前登记实例，兼顾启动失败后的清理。禁止启动后台后再补装包装。

   h21专用准备辅助复用原启动流程，取得同一锁后，**一次持锁调用完整原`arm_bridge_to_armed`**，从proposal发布直到真实HTTP ACK、合法回调安装ARMED及原断言全部结束。手动pump可重入；后台调用排队，不能被丢弃、提前return或伪造成功。已经进入包装的后台调用先正常完成，手动方取得锁后才开始整条流程；不能持有生产`_admission`或`_state_lock`等待该测试锁。

   包装保留至本测试全部桥接线程停止。成功或异常均先退出手动独占区、释放反例barrier，再在**不持测试锁**时执行stop/join；最后恢复包装。即使辅助尚未返回bridge，已登记实例也必须清理。不得持锁等待排队worker完成，不得修写challenge、直接设置armstate、跳过验证、重试直到ARMED或延长窗口。原未武装零调用分支保留。

3. **完成原h21的时钟和“返回前持久化”要求。** `_CommandClockSeam`必须实际包住**原node**的`_receive_dispatch→_pump_once`，不能只存在于新增用例。TTL保持400ms，回调线程使用该命令的受控时间，其它线程仍用原时钟；异常退出也恢复，arming准备阶段不冻结时间。

   原h21及慢写正向用例都要在模拟`send_with_sync_callback`内部、`on_trade`返回后且send返回前验证：
   - 真实`append_event`已经返回；
   - 对应`SIM_TRADE_1`事件已在真实提交后的outbox记录中存在，读取不能只筛`acked=0`；
   - 然后才记录send返回，仍只有一次模拟send、event_gap为false。

   可以包装真实`append_event`观察提交完成，但必须调用原写入，不能制造成功标记。CONSUMED/RETURNED receipt和send结束后的上传仅作补充，不能替代此时点证明。保留原断言及nodeid；独立反例应令回调写入缺失或延迟至send返回之后，证明该顺序断言确实拒绝它。

4. **m01修正导入、错误分类和所有退出分支。** 将`msvcrt`移至实际Windows锁测试／私有辅助内部，不新增平台skip，不让其它节点在收集时因该导入失败。

   用一个外层清理范围覆盖握手、身份核对、首次拒绝、进程终止、锁探测和重启；holder、launcher、contender、fd/句柄均先初始化并逐项清理。清理某一项失败不得跳过其余项，也不得遮掉原失败。实际持锁解释器与启动器都必须完成终止等待，不能只kill不wait。

   首次进入终止阶段时建立唯一`monotonic()+5`截止点；正常路径和异常清理的所有进程等待、释放探测只用剩余时间。删除finally中新开5秒的等待；超时真实失败，不能续时后称成功。

   **EACCES本身不等于锁占用。** 若保留有界释放重试，采用测试侧同一fd、offset0/length1的非阻塞Windows `LockFileEx`探测，仅原生`ERROR_LOCK_VIOLATION(33)`可等待重试；成功立即`UnlockFileEx`并关闭，AccessDenied及其它错误直接失败。生产锁仍用原实现，最终`contender.on_start()`只执行一次，继续证明同slot、同持久身份及clean stop；不换解释器绕过guard。

5. **定向反例必须证明实际边界。** 原owner及独立reviewer至少落实以下已有合同的真实验证：
   - 用event barrier迫使后台通过真实pump入口与整条手动arming竞争；证明第二publisher未进入、发布对与内存一致、真实credential消费/HTTP ACK/合法回调断言成立。barrier记录“尝试进入”，不能在持锁时等待被阻塞方进入函数体。保留诊断07原红样，不修改其输出。
   - 对**原h21节点**在真实CONSUMED写入后插入0.45秒墙钟延迟，验证时钟接缝有效及同步trade在send返回前持久化；验证其它线程时钟与异常恢复。
   - 到期等号及之后仍由真实fence产生NO_CALL、模拟send为0。
   - 活持锁者确实阻止访问；实际进程退出与OS字节释放后同槽恢复；覆盖握手／断言／等待异常的清理、共享5秒及非锁错误拒绝。证明无新增非Windows收集依赖。

   两个原节点、两文件完整组合和必要反例通过后才进入最终全量；孤立通过不替代组合验证。

6. **验收链不变。** 仅两测试变化且F的实际绑定不变时，F及附件原字节复用；实际SHA程序化冻结，未变五文件证据按原范围复用。独立ZCode完成受影响源码、定向重跑、反例及原三份策略探针，封存新报告，再真实生成和验证新报告绑定本地锁。随后在最终冻结源码上执行**一次完整AFTER**，对原r5 baseline证明人口完整、原33skip不增、added F/E/CE为0/0/0；无skip/xfail、alias、阈值或baseline修改。最终仍须独立无条件产品PASS、精确main集成/push及实际HEAD绿色CI。未完成定向验证的候选不跑全量寻求PASS。

G1–G4、所有正式作业／研究排除范围及凭据、系统、服务边界不变；候选、输入、锁不推送。真实交易保持`False/hold/NOT_RUN/send=cancel=0`，模拟调用单列。沿用AGENTS§7–8、两份20261004事故和manifest-recovery-002，保留原退出与失败，Path正规化、完整JSON后原子发布，不固定活动日志。


---

## 原 Astra 作者有效补充 ruling08（原文）

原作者：Astra/max；来源：`C:/AKH1107/cli-dispatch/r6-infiny-ruling08/PLAN.md`；原始 SHA-256：`6c7a9938707414ddfe671f949167d06e072c34840b5c8f9c322f208262883b50`。下列内容按原作者发布文本收录；历史角色称谓保留，本批直接用户分工见交接文档。

**PARTIALLY_ADOPTED：授权仅让h31选择ruling07的串行夹具。** 保留55项、53P/2F/raw1及独立FAIL；不采纳“m01导致h31”的因果断言。实际失败发生于arming准备，尚未执行关门断言；旧失败的具体并发调度仍未证明。

原owner在当前repair05正常续接时实施，不中断正在进行的工作。维持原两文件写集：在`tests/test_infiny_native_lifecycle.py`的私有`_armed`增加显式关键字参数`_serialize_arming=False`，仅h31及其反例传True；该参数不得进入`**overrides`或生产Params，其它调用者默认行为不变。

选择True时，在`on_start`前安装每bridge RLock包装，所有真实`_process_arming_files`调用串行、后台排队不丢弃。手动独占覆盖proposal发布至原ARMED／有效性断言；**随后释放锁，再执行`_nudge_control`及原3秒`context_ready.wait`**，避免阻塞后台完成响应。包装保留至线程停止；准备失败也须先释放锁，再stop/join并恢复包装。沿用既有合成transport、真实验证、ACK处理和合法回调，不新增真实HTTP要求，不改状态、challenge或窗口，不重试直到armed。

保留h31原nodeid、1500ms dispatch、撤销动作及零模拟send／DISARMED_TERMINAL／无效arming全部断言；不移植h21的时间冻结。独立竞争反例须证明手动与后台竞争时准备一致，随后确实执行关门零调用断言；任何准备拒绝仍报失败。

其余ruling07完全保留：组合与反例通过后冻结；绑定不变则F不变；独立审核、新报告及真实本地锁、一次最终全量严格r5新增F/E/CE为0/0/0、无条件PASS后main push及HEAD绿色CI。不加skip/xfail或豁免；安全、排除范围和事故规则不变，无计划PASS门槛。


---

## 继任 Astra 作者有效裁定 ruling09（原文）

原作者：Astra/max；来源：`C:/AKH1107/cli-dispatch/r6-added-regression-ruling09/PLAN.md`；原始 SHA-256：`ca9d416dad73c32d17ec1a1cf15d29c36b1f189364538b39ec932813eb935004`。下列内容按原作者发布文本收录；历史角色称谓保留，本批直接用户分工见交接文档。

**ruling09：既有 r6 新增回归的有限修复裁定，立即生效。**

本裁定承接同一七文件修复批次；不是新模块计划。Root 已明确移交原不可续接 Astra 作者的裁定职责给本 Astra/max 作者，并将已终止 ZCode owner 的实际修复职责显式移交给独立 Astra/max owner `/root/astra_regression_owner_20261112`；独立 Sol/max 做计划意见和产品硬审。用户本轮角色指令覆盖仓库旧路由文字，禁止改持久化模型配置。原计划 `docs/repair-blocking-contract-inputs-20261112.md`、ruling06/07/08 中未被本条明确修改的要求全部保留。计划意见只供作者处置，无计划 PASS 门槛。

1. **实际失败与证据。** source08 全量已终止：原生 1，6279 ran、6012 passed、174 failed、60 errors、33 skipped、8 collection-error files，6907.3 秒，残余 PID 为 0。对固定 r5 baseline 的严格比较为新增 F/E/CE=28/0/0、解决 F=21、人口遗漏=0；独立 `r6-infiny-sol-final01/work/VERDICT.json` 为真实最终 FAIL。1180 源文件及 F119 未变化。旧 FAIL、报告、锁、baseline、source06 的旧通过记录全部保留；后者不替代 source08 验收。

   - **26 个 ACL/启动失败：已证实。** `C:/AKH1107/r6-acl-diagnosis-sol01/DIAGNOSIS.json`、`metadata-probe.json`、`actual-verify.json`：当前 26 个失败目录的 owner 为 BA（Administrators），旧 source06 对应目录 owner 为当前 TokenUser；真实只读 `verify_private` 在 guard 下拒绝当前 26 个、接受旧 26 个。当前 TokenOwner=BA、TokenUser 为用户 SID。`client_service.py` 新建后只写 DACL，不设置 owner；启动器先走该检查，失败在 listener/event 之前返回。原生 1 是保留的预期负例结果。**不是 WinDLL 泄漏，也不是允许把 BA 当用户。**
   - **文档收据：机制已复现。** 原全量 happy-path 子进程报 OfflineViolation。作者 `docs-probe01` 原节点委托孤立 1P/raw0 不消除原失败。`docs-probe02` 在同一真实 CLI 子进程仅注入 SYNTHETIC WMI OSError，得到 1F/raw1 及完整链：`cmd_build:1233 -> platform.platform -> _win32_ver -> _syscmd_ver -> shell ver -> offline_guard:268`。这证明该平台信息调用含禁止的环境依赖；不宣称旧未插桩执行的具体 WMI 故障已被记录。
   - **raw-sink：仍待因果诊断。** 原 30 秒、16 symbols、十倍 burst 的真实记录在约 t=7.439–9.204 秒停在 persisted=545/durable=472，队列达到 256，最终 queue_full=142。相应早期操作事件被既有 4096 项诊断 ring 淘汰；不能据剩余 fsync 样本断言停顿原因，不能将其定性为可豁免 flake。独立支撑线正在 `C:/AKH1107/r6-rawsink-diagnosis-sol01` 做有界诊断；本条不授权 raw-sink 产品/测试修改或降低任何负载、阈值。

2. **立即实施的精确写集与来源。** 独立 owner 从 `C:/AKH1107/r6-freeze08/SOURCE.json` 的 1180 项实际字节建立完整、无可写别名的全新 `C:/AKR61112/repair09`，冻结 source08 永不修改。既有七文件字节作为已交接输入保留。本裁定只增加以下五个可写文件；其它产品、测试、配置均只读：

   - `src/ak_trading/infiny_execution/client_service.py`：新建私有目录及排他新建凭据文件的 ownership 初始化。
   - `tests/test_infiny_native_protocol.py`：真实 ownership、失败顺序、排他创建和已有对象反例，原测试/nodeid/断言保留。
   - `tests/infiny_native_fakes.py`：仅让 `ArmingAclSandbox` 自己排他创建的空临时文件使用相同真实新对象初始化；不得改 verifier、默认 arming、transport、窗口或其它夹具行为。
   - `scripts/generate_api_reference.py`：只改收据的平台描述取得方式。
   - `tests/docs_reference/test_static_reference.py`：上述收据路径的离线反例和原 happy-path 证明。

   因公开应用函数本身承诺在写密钥前建立当前用户 owner，裁定采用真实新对象初始化；不加入全局创建拦截、测试 token/default-owner 调整或子进程特殊权限。此写集扩展仅修复本次实际新增失败，不授权服务部署、系统 ACL、Windows token/权限、生产凭据或其它模块变更。

3. **ACL 修复合同。** `secure_pairing_directory` 与 `write_pairing_file` 必须证明目标确由当前调用新建，再建立真实 TokenUser owner + protected sole-user DACL，完成真实验证，才允许写任何密钥字节。当前 TokenOwner 为 BA 的环境必须实际通过正向用例。不得修改 `verify_private` 对 owner、foreign trustee、NULL DACL、inheritance、ACE 的拒绝强度。

   - 新建证明必须来自成功的排他创建，不能用先 `exists()==False` 后 `exist_ok=True` 的结果冒充。目录/文件应采用创建时 security descriptor，或持有与排他创建绑定的原生对象句柄完成初始化及身份验证；仅按可替换路径盲目改 owner 不合格。禁止跟随 reparse/symlink/junction；目标或已检查祖先被替换时必须拒绝，不能修改替换者。
   - **已有 wrong-owner 目录在任何 ACL/owner 变更前拒绝，保持其描述符和内容不变。** 已有合法 user-owner 目录可保留原有的 tighten-and-verify 合同，不扩大既有接受条件。不得通过修复 owner 接管原有 BA/foreign 对象。已有凭据文件仍在读取/写入/owner 改动之前拒绝；排他创建失败的竞态赢家同样保留。
   - 新文件在空且仍由本调用持有时先完成 owner/DACL/verify，然后沿用真实 write/flush/fsync 及完成后的验证。初始化失败必须零密钥字节、无成功返回，关闭全部句柄。只清理由本次排他创建且身份仍相同的空对象；不能删除/修改竞态替换者或原有对象，不能为清理开宽权限。写入后其它失败按原拒绝合同诚实报告，不伪造成功。
   - 共有 toolkit 的普通 `apply_protected_dacl` 不得无条件修正 owner。新对象初始化使用受限私有入口，调用点明确持有排他创建证据。`ArmingAclSandbox` 只在自己的 `xb` 成功且尚未写合成内容时调用；其它历史 private-DACL 辅助不批量重写。
   - 真实测试至少证明：当前 BA 默认 owner 下新目录/文件变成用户 owner 且 sole-user/private；第一次密钥 write 之前真实验证已完成；注入 owner/ACL/verify 失败时零密钥字节；已有 wrong-owner 目录、已有凭据字节和描述符不变；排他创建竞态/reparse 替换拒绝且替换者未被改动；原 BA-owner/foreign-trustee/NULL-DACL/inheritance 负例继续拒绝。反例使用新 SYNTHETIC 临时对象；不改进程 TokenOwner 或系统设置。

4. **文档修复合同。** 收据 `platform` 保持真实字符串字段，直接取进程内的 `sys.platform` 即可；不再调用可能访问 WMI、注册表或 shell 的 `platform.platform()`，不为附属收据放开离线 guard。此处无需新增环境探测框架；其余内容、schema、版本、抽取/别名/导出、原子发布及确定性保持原合同。

   保留原 `test_package_all_with_alias_and_reexport` 完整断言。增加同一真实 CLI 子进程中 WMI 不可用的可重复反例，证明完成 build、收据存在、alias/reexport 正确，且无需原生 shell；另以可观察的拒绝器证明收据路径不尝试 WMI/原生命令，而非吞掉 OfflineViolation。继续运行同文件的静态不执行、路径/allowlist 拒绝和确定性用例；原有其它失败按固定 baseline 比较，不能修写其输入哈希以求绿。

5. **执行顺序与未决项。** owner 立即开始第 2–4 条，不等待 raw-sink 诊断或计划意见 PASS。先保留实际红证据，再运行精确原失败组、上述新边界反例、受影响文件组合以及原 ruling06–08 要求的 h21/m01/h31 两文件组合/反例。所有调用保留真实 native exit、passed/failed/error；不以孤立通过替代组合。支撑线可继续 F04 的只读/外置诊断，提出最小实际修复后由本作者补充裁定；**在 F04 尚无有效处置时不启动最终全量或声称本批完成。** 若实现必须越出第 2 条写集，先携带精确因果证据交本作者，不能默改其它文件。

6. **冻结、报告绑定和最终交付不变。** 本作者已程序化确认上述五文件均不在当前 F119 的 `files` 条目中。因此仅这些变化时，原 F 及六份旧输入/新附件保持原字节，不重生成、不刷新期待。owner 再按实际最终写集核对；如发现真实绑定变化，先报告事实由作者处置，不盲刷。独立审核必须覆盖全部最终写集、来源、真实定向命令及 reviewer 自写反例；封存新的整体 revision 报告后，真实生成并验证新本地报告绑定锁。旧报告/锁仅代表旧冻结 revision。

   所有必要修复、定向及组合验证、来源/报告/锁检查完成并冻结后，独立 Sol 在该最终冻结候选执行**一次完整 AFTER**，对原 r5 baseline（SHA `8733347028dbf277393a07236c51dc77b459ea67f4e75fd6771dfe0ec3590602`）要求新增 F/E/CE=0/0/0、人口完整、原 33 skip 不增。不得增加 skip/xfail、比较别名、豁免、统计阈值、时窗、容量或重写 baseline；不连续重跑同一未修复全量寻找绿样本。工程诊断可用新标识有据重跑，保留所有失败。独立无条件产品 PASS 后，Root 精确合入 main、push、验证实际 HEAD CI 通过，维护唯一 CURRENT-STATUS 并交接后停止；当前公共 CI 1224P 不替代上述失败清零。

7. **证据和安全。** 图索引 generation 为 2026-11-08，相关覆盖均 `metadata_changed`；已用冻结 source08 直接读代码与实际失败核验，图只用于定位，不宣称覆盖完整。所有新 work order 继续带 AGENTS §7–8、20261004-rg-no-match、20261004-grok-headless-cancel 及 manifest-recovery-002 事故规则：原生退出立即捕获；仅 optional rg 1/空 stderr 可记 NO_MATCH；错误不洗白；Path 边界正规化、完整 JSON 验证后发布、不钉活动日志、不用保留自动变量。作者一次只读 SOURCE 默认 GBK 解码 raw1 保留为诊断错误，已用 UTF-8 读取成功，与产品 FAIL 无关。

   不读取/打印 `.env`、真实凭据、模型配置；不启停服务、UI、DB 或改变机器安全设置。H5/H6/H7/H8/N8/DISPATCH/Forward/paper/研究/holdout 均不启动；本条的既有 raw-sink 工程测试诊断不构成 N8 作业授权。保持 `enable_trading=False / observer=hold / broker=NOT_RUN / real_send=real_cancel=0`，SYNTHETIC 模拟计数单列。原始日志留仓库外，不新增过程文档树。


---

## ruling09 原作者意见处置（原文）

原作者：Astra/max；来源：`C:/AKH1107/cli-dispatch/r6-added-regression-ruling09/ADVICE-DISPOSITION.json`；原始 SHA-256：`148c1f709b7a2e155c3a6d3c629f0ae66edd2eb5fdc84b2f3415dd4913c74f2a`。下列内容按原作者发布文本收录；历史角色称谓保留，本批直接用户分工见交接文档。

```json
{
  "author_role": "Astra/max planning and semantic authority",
  "advice_path": "C:\\AKH1107\\cli-dispatch\\r6-added-regression-advice09\\ADVICE.json",
  "advice_sha256": "5a078bbc271b1d76aa5189d0c10e87e24a4ab174d65f286459e998592951c236",
  "disposition": "ADOPTED",
  "reason": "All three points clarify obligations already explicit in ruling09 sections 3 and 4; original exclusive object identity must persist through initialization, writes, verification and cleanup; no path-only owner normalization; receipt tests must expose forbidden attempts.",
  "plan_changed": false,
  "effective_plan_path": "C:\\AKH1107\\cli-dispatch\\r6-added-regression-ruling09\\PLAN.md",
  "effective_plan_sha256": "ca9d416dad73c32d17ec1a1cf15d29c36b1f189364538b39ec932813eb935004",
  "implementation_may_continue": true,
  "plan_pass_gate": false,
  "product_verdict": "NOT_ISSUED_BY_AUTHOR",
  "original_product_fail_preserved": true
}
```


---

## ruling09 原作者有界诊断处置（原文）

原作者：Astra/max；来源：`C:/AKH1107/cli-dispatch/r6-added-regression-ruling09/F04-DIAGNOSTIC-DISPOSITION-01.json`；原始 SHA-256：`d3eacfe80595bf85dec5d9a7cfca92bc62fd48fcf65e97e26eedc212945fe42e`。下列内容按原作者发布文本收录；历史角色称谓保留，本批直接用户分工见交接文档。

```json
{
  "author_role": "Original ruling09 Astra/max semantic authority, continued by Root",
  "disposition": "ADOPTED_EXECUTE_BOUNDED_DIAGNOSTIC",
  "request": "C:\\AKH1107\\r6-rawsink-diagnosis-sol01\\targeted-probe01-request.json",
  "pins": [
    {
      "path": "C:\\AKH1107\\r6-rawsink-diagnosis-sol01\\targeted-probe01-request.json",
      "sha256": "091913c1a7e2b6d1a6b01579eb31f2809d1a01b6356f5036264035b7bf529c4a"
    },
    {
      "path": "C:\\AKH1107\\r6-rawsink-diagnosis-sol01\\probe_launcher.py",
      "sha256": "227bf0eaa904f112998bcde4c4c9a4727fa69b069bb70bd69ee4a49079c8972a"
    },
    {
      "path": "C:\\AKH1107\\r6-rawsink-diagnosis-sol01\\diagnostic_overlay.py",
      "sha256": "239ce9e7c1b67079c79df59257e0fb36651eccc72946fdac6c54acbf1cac81e2"
    }
  ],
  "authorized_execution_count_for_this_request": 1,
  "argv": [
    "C:/AKR41110/venv-full/Scripts/python.exe",
    "-B",
    "-X",
    "utf8",
    "C:/AKH1107/measure.py",
    "C:/AKH1107/r6-rawsink-diagnosis-sol01/targeted-probe01-request.json"
  ],
  "wall_seconds": 120,
  "scope": "Single unchanged original sustained-load node under frozen offline guard, same SYNTHETIC workload; external pass-through diagnostic instrumentation only.",
  "reason": "Original queue accounting identifies a dequeued but uncommitted row546; bounded original timing ring evicted the causally relevant operation. Active phase and all-run slow facts discriminate pending causes.",
  "unchanged": [
    "source08",
    "30-second workload",
    "16 symbols",
    "tenfold burst",
    "256 queue capacity",
    "real writes/flush/fsync",
    "zero-drop and high-water gates",
    "all original test assertions",
    "fixed baseline",
    "original full failure",
    "ruling09 PLAN and advisory disposition"
  ],
  "result_interpretation": "Preserve original pytest/native codes. Either FAIL or isolated PASS is diagnostic evidence, never product PASS or waiver of original142-drop failure. No unchanged rerun selection.",
  "limits": "Phase wall time/threadCPU and Python stack do not alone distinguish kernel I/O service time from descheduling. No payload/locals capture, no resource/system configuration changes, no real credentials/services/DB.",
  "postconditions": [
    "capturing process and all child job PIDs terminated",
    "source pins checked unchanged",
    "logs sealed only after producers exit",
    "diagnostic JSON serialization validated before publication"
  ]
}
```


---

## ruling09 原作者 F04 有效补充（原文）

原作者：Astra/max；来源：`C:/AKH1107/cli-dispatch/r6-added-regression-ruling09/F04-SUPPLEMENT-01.md`；原始 SHA-256：`4f6f36a1d330385d235e07fedd72225c735c189b6c5f44a220db8bd8edd7b3c8`。下列内容按原作者发布文本收录；历史角色称谓保留，本批直接用户分工见交接文档。

**F04 作者处置：PARTIALLY_ADOPTED；只修复已证实的诊断证据丢失，不推断生产根因。**

本条是同一 r6 回归修复的补充，原 `PLAN.md`（SHA `ca9d416dad73c32d17ec1a1cf15d29c36b1f189364538b39ec932813eb935004`）及原作者意见处置保持原字节。独立辅助证据为 `C:/AKH1107/r6-rawsink-diagnosis-sol01/DIAGNOSIS.json`（SHA `8dfdc77d5c7f635930fd18f73c73e9b5633c67fbbfa66ec02720d679848f18e3`）；它不是产品硬审。

1. **事实与不采纳项。** 原完整运行 queue_full=142/raw1 保留。计数证明候选 append546 已出队但尚未提交的状态至少持续约 1.51 秒；这排除了“前一行545的周期 fsync 尚未返回”解释，未区分546写入、包装、调度等具体子阶段。原有 4096 事件尾部淘汰 23194 项，早期停顿的操作证据确实丢失。

   唯一已授权外置诊断已终止：1P/0F/0E、pytest raw0，监督器36.35秒、PID0，未出现饱和。虽保留原30秒、16品种、十倍burst和256容量，实际到达率为111.887/s，原失败为158.791/s；3357 ticks 对4764。两次运行的观测开销与执行上下文影响未分离。**不采纳孤立PASS作为修复证明，不归因为宿主抖动，不授权原始sink生产逻辑、容量、durability或工作负载修改。**

2. **唯一新增写集。** 同一实际 Astra owner 在其既有完整 successor 中，仅新增 `tests/test_infiny_raw_sink_faults.py` 的 `_BoundedSinkProbe`／`_TimedFile` 诊断小范围修改。原 testcase 函数体、nodeid、实际sink/service、负载、窗口、睡眠、心跳、drain、fsync、队列容量及全部断言保持原样。不得复制外置重型 overlay，不加后台采样线程、逐操作磁盘日志、额外真实操作或整个进程的通用 tracing。其它新文件和产品写集不扩展。

3. **最小诊断行为。** 复用既有每操作 timing、event、锁和线程接缝，新增恒定空间的每类全程最慢事件（含真实开始/结束时间、已有 sequence、结果/异常）；原尾部统计继续明确标为保留窗口，不冒充全程。无须新增每次 thread-CPU 读取、额外嵌套包装、每次排序或top-K框架；一个每类max即可满足本次证据需要。

   在已有写入、flush、fsync包装边界保留一个当前 writer 阶段/sequence，异常退出必须恢复；不得持诊断锁调用真实I/O或等待生产锁。首次实际 `queue_full` 已发生后，只在内存中冻结一次当时阶段、时间、队列/累计计数及小型已有事件上下文，明确这是观测时刻。若保留栈，只允许这一失败后时刻、固定帧数、文件/函数/行号；无locals/负载/凭据。成功路径不新增栈采集。正常JSON输出仍在原测试finally，writer已停止之后；无热路径写盘。

   所有真实被包操作仍恰好调用一次，返回值与异常原样传播。全程max只描述观测时间，不能凭 wall/CPU 宣称内核I/O或调度根因。首次失败后的诊断不得逆向解释为首次失败的原因。

4. **验证与因果界限。** owner和独立reviewer用外置SYNTHETIC探针证明：早期慢事件之后超过4096个快事件，原尾部已淘汰慢事件，而新全程max及其sequence仍准确保留；包操作成功/异常均保持一次调用及原结果；只冻结首次真实饱和、数据量有界、无I/O时持诊断锁。此验证针对已证实的证据缺口，不新增低价值提交测试。先验证定向受影响代码及保留原负载的真实节点，所有原生结果及实际到达量保留。若真实节点失败，使用新增因果证据交本作者，不以重跑选绿替代修复。

   **诊断完善本身不关闭F04。** 本条是原计划第5条所需的有效F04处置：允许在ACL/docs等实际修复、必要定向/组合验证、诊断验证、来源/报告/锁及独立冻结条件全部满足后，按原计划执行一次最终完整AFTER，以原人口和原断言作最终检验。不能把本次1P合并进最终证据，不能提前声称142条丢失已被修复。最终完整运行必须实际通过本节点的零丢失、高水位、持久化、16品种和heartbeat断言，同时固定r5严格新增F/E/CE=0/0/0；仍须独立无条件产品PASS。若完整运行再次失败，应保留此次运行及新诊断，回原owner进行证据支持的修复，不能重跑未修复版本求绿。

5. **其余约束保留。** 此测试文件不在F119内，实际最终写集复核后继续保留原F/附件字节；新完整revision由独立报告覆盖，并真实生成/验证新本地报告绑定锁。原失败、baseline、输入、旧报告和锁不改；无skip/xfail、别名、豁免、阈值/时间窗/容量变化。原计划的角色、独立性、工程原生退出、封存、main/push/HEAD CI、唯一状态页和交接后停止要求不变。原始日志仓库外，SYNTHETIC与真实计数分开，真实交易仍False/hold/NOT_RUN/send=cancel=0，不触碰凭据、服务、UI、DB、系统/token/default-owner或模型配置。


---

## ruling09 原作者 F04 有效澄清（原文）

原作者：Astra/max；来源：`C:/AKH1107/cli-dispatch/r6-added-regression-ruling09/F04-CLARIFICATION-02.md`；原始 SHA-256：`c304a6de7f89a0435e1b9f4bc16f2458e84e8a538d2cc702e62a4f4106d5a205`。下列内容按原作者发布文本收录；历史角色称谓保留，本批直接用户分工见交接文档。

**F04 作者澄清：最终实际完整通过是本批有效验收，不要求补造历史因果。**

本条补充已生效 `F04-SUPPLEMENT-01.md`（SHA `4f6f36a1d330385d235e07fedd72225c735c189b6c5f44a220db8bd8edd7b3c8`）；原裁定、补充和失败全部保留。

1. 在原负载参数、原testcase/nodeid、原全部断言、原guard及完整人口不变，且新诊断没有节流、等待、额外真实操作、返回值替换或伪造成功的前提下，**修复后的最终冻结候选，一次完整AFTER真实通过该原节点及所有原硬断言、固定r5严格比较新增F/E/CE=0/0/0、独立产品无条件PASS，构成本批有效验收。** 历史546停顿具体子因未知必须如实交接，不再另设“必须重现历史同一调度”的隐形门槛。实际发送量/到达率如实报告，不能声称不同运行等压，更不能通过改变原负载或诊断等待来降低压力。孤立绿色样本不满足以上条件；完整失败后不得连续重跑未修复版本求绿。

2. 在同一owner外置SYNTHETIC验证中，除超过4096事件的保留证明外，再执行一个**有控制的真实写入停顿反例**：在既有writer的一个明确sequence、真实write进入前用event barrier暂挂该writer，producer仍走原真实offer；保持原256容量和真实fsync等操作。等待真实queue_full发生且新诊断已冻结首次阶段，再释放barrier，让原真实write恰好执行一次并完成清理。反例需要证明首次捕获为对应write阶段/sequence，真实drop计数非零，原零丢失断言确实拒绝，而非吞掉失败。全部等待有同一宽松作业期限，异常也释放barrier并close/join；不设置系统优先级、磁盘故障或全局时钟。

   这是诊断反例，不修改原接受节点，不以模拟停顿冒称旧历史事件的原因。可用外置wrapper调用原测试或同一真实sink路径；必须保留原生预期失败退出及精确失败断言，另记录反例验证是否完成，不能把raw1改成原测试PASS。新增诊断只在已发生首次饱和后采集一次上下文，不能倒置因果。

3. 接续顺序明确为：轻量诊断实现 → 事件保留/真实停顿反例 → 原真实节点及受影响组合 → 完整候选来源与独立定向审核/报告/锁/冻结 → 一次最终完整AFTER及严格比较 → 独立无条件产品PASS → main/push/实际HEAD CI/交接后停止。任何新增实证失败回原owner按有效计划修复；未知历史子因本身不阻止满足原完整验收条件的候选进入这条链。

写集仍只增加既有 `tests/test_infiny_raw_sink_faults.py` 的诊断辅助；不修改sink产品、容量、窗口、durability、输入、baseline、skip/xfail或任何原断言。无新模块、计划PASS门槛、服务/系统/凭据/模型配置动作。


---

## ruling09 原作者 F04 意见处置（原文）

原作者：Astra/max；来源：`C:/AKH1107/cli-dispatch/r6-added-regression-ruling09/F04-ADVICE-DISPOSITION-03.json`；原始 SHA-256：`225f793e94ac394037fbfb97d96616f311d2aacef7feca5710a373e321fa02b5`。下列内容按原作者发布文本收录；历史角色称谓保留，本批直接用户分工见交接文档。

```json
{
  "author_role": "Same ruling09 Astra/max semantic authority",
  "advice_path": "C:\\AKH1107\\cli-dispatch\\r6-added-regression-advice09\\ADVICE-F04.json",
  "advice_sha256": "a845cf5dddd40a1d917d5bee78f0bc3a64681025c356b029689ea0b71091da0a",
  "disposition": "ADOPTED",
  "reason": "Nested phase restoration, actual queue_full discrimination and observation timing, separate retained-window vs all-run facts, and honest realized-load reporting implement existing F04 requirements; no new acceptance threshold or waiver.",
  "plan_or_supplement_changed": false,
  "effective_pins": [
    {
      "path": "C:\\AKH1107\\cli-dispatch\\r6-added-regression-ruling09\\PLAN.md",
      "sha256": "ca9d416dad73c32d17ec1a1cf15d29c36b1f189364538b39ec932813eb935004"
    },
    {
      "path": "C:\\AKH1107\\cli-dispatch\\r6-added-regression-ruling09\\ADVICE-DISPOSITION.json",
      "sha256": "148c1f709b7a2e155c3a6d3c629f0ae66edd2eb5fdc84b2f3415dd4913c74f2a"
    },
    {
      "path": "C:\\AKH1107\\cli-dispatch\\r6-added-regression-ruling09\\F04-SUPPLEMENT-01.md",
      "sha256": "4f6f36a1d330385d235e07fedd72225c735c189b6c5f44a220db8bd8edd7b3c8"
    },
    {
      "path": "C:\\AKH1107\\cli-dispatch\\r6-added-regression-ruling09\\F04-CLARIFICATION-02.md",
      "sha256": "c304a6de7f89a0435e1b9f4bc16f2458e84e8a538d2cc702e62a4f4106d5a205"
    }
  ],
  "acceptance_decision": "The changed frozen candidate can be accepted only after the original full node and assertions actually pass, fixed-baseline strict added F/E/CE are0/0/0, and independent product review is unconditionalPASS. Unknown historical subcause is reported; no isolated green substitute or unchanged full retry.",
  "controlled_counterexample": "External real write barrier, actual first queue_full/capture, release and original write once; preserve expected native failure separately from diagnostic verification.",
  "implementation_continue": true,
  "product_verdict": "NOT_ISSUED_BY_AUTHOR",
  "plan_pass_gate": false
}
```
