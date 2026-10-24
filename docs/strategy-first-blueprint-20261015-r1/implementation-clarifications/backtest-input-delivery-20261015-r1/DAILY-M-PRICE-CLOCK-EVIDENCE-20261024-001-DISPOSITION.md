# M 价格时钟证据计划：原作者一次意见处置

原作者：BIP1 Astra/max `/root/astra_backtest_input_delivery_plan_20261015`。

**执行计划与事实澄清原字节保留；连同本处置立即成为有效方案。原两 Sol 立即实施各自 M 来源/谱系分支，不等额外批准，不重复计划意见。** 本单纠正55条记录的状态分类，采纳可机械复核的证据表达；不发布M数值时钟，不授产品PASS。

已全文读取独立BIP1咨询`D:/AKRV261024/R1-M-CLOCK-PLAN-001/OWNER-REPORT.md`。实际RESULT/TERMINAL为native0、原SID匹配、冻结输入未变。其PASS-equivalent仅为意见标签。reviewer未执行hash、未扫描bars/备份，io.py越EOF探针在正确futures_research.py读取后纠正；原失败/限制和Root custody均保留，不冒称独立重跑或产品审核。

## 作者处置

| 意见 | 处置和生效语义 |
| --- | --- |
| F1：55记录包含skipped_db | **PARTIALLY_ADOPTED。** 直接复核原JSONL，2578—2632为55条M/86400任务记录：54 `ok`、1 `skipped_db`。2604行为DCE.m2401、ts=2026-09-01T16:21:06+08:00、rows=0，无batch_id。两原文中“55条下载完成/日线完成记录”等概括均按本单纠正为该状态分布；错误原文留存，不覆盖。该skipped记录不是成功下载回执，也不能证明bundle中的m2401行来自本块。reviewer进一步说“必定来自早期导入、回执在本块外”的绝对推断不予采用为已证事实：仅凭skipped状态不足以定位当前冻结行的实际批次/修订链，作为待核对线索保留。 |
| 四时钟分别表达 | **ADOPTED。** 每个源系列关联逐行原`available_at`/import bound，不以min/max替代原值；市场first_seen=null并给reason；reconstructed research A=null、未采用policy version；manifest.created_at单独表达。若某已存记录有真正observed/late/revision证据，单独绑定原件并优先保留更晚时间，不能因一般导入映射抹去它。 |
| REQUESTS中记录去重范围 | **ADOPTED。** 主owner记录实际核对的RB/CU/AU REQUESTS/终态、18 snapshots、TqSdk skill/维护来源及同原件迁移/转换别名；原始退出/无匹配语义保留。新query仍只有原计划精确一条，最多2官方URL；本地足够则不请求，不增加中性控制、重启或重试。 |
| 裁定证据行→决定/版本/缺项 | **ADOPTED。** 未来本作者裁定逐项绑定证据位置、支持的技术决定、适用源/版本及未支持字段和实际locator；明确m2401状态、版本不可回溯与当前字段null。delay仍null，不能由owner、审核建议或非空文本自动变60。 |
| 其余风险/验证限制 | **RECORDED。** query可能失败、当前文档不能证明旧运行版本、同秒不同精度不能证明逐行batch。不是额外审批或计划PASS门；若某问题未被来源证实，明确保留它，不能将UNKNOWN重命名为合格输入。 |

## 对实施的精确补充

谱系owner仍消费完整55条记录并保留顺序/原值/所有状态；不得为了满足54个ok而删除m2401。至少分别输出`task_record_status`、`series_receipt_match`和`row_batch_identity_proven`。只有真实ok记录才可进入成功回执候选；ok也不自动证明逐行batch。skipped_db的`series_receipt_match=false`，其当前冻结行的来源关系保持`UNRESOLVED_FROM_THIS_BLOCK`；rows=0只描述这个任务，不意味着bundle不存在m2401 bar或研究人口无该合约。

原计划两条required离线命令和七类反例不删。追加两个有意义的SYNTHETIC拒绝：把skipped_db转换为ok/成功下载；利用rows=0或无batch_id删除已有m2401行、伪造其逐行batch来源。备份自身状态分布按实际记录核对，冲突保留，不把当前日志覆盖备份。所有原CSV/2026值/空列只读，不重跑exporter、DB或SDK。

精确文件owner和两个新根继续按原计划§3，不扩公共代码或任何consumer/MarketBar/FrozenInput写集。主Sol `/root/sol_backtest_input_continuation_20261024` 继续唯一网络owner；原child `/root/sol_backtest_input_continuation_20261024/cu_calendar_prepare_20261024` 接M谱系，AU文件保持冻结。两者均续接原`gpt-6.1-sol/max`身份，无模型配置改动，不另建替代writer，不回滚他人文件。

Root029已把M source＋metadata provenance纳入原策略lane：每命令1GiB RAM/256MiB输出，最多2个并行、network max1；总reservation/floors/旧UNKNOWN charges不变。沿既有fresh物理与每命令门执行，不等待无关模块或新Root确认。独立计划咨询已结束；成果审核仍独占该lane。029仅是资源调度，来源预算/产品权限仍取本有效方案。

完成必要验证和实际写集/hash冻结后立即独立ZCode成果硬审；FAIL回同实际owner追加修复。来源终态回本作者，立即按原计划给M时钟裁定；reviewer意见不替代作者技术决定，原字段/真正迟到/修订优先级不变。保持完整输入未交付/研究信用0的事实，不以来源工程PASS或SCENARIO冒充真实策略回测。

本单续用当前角色/流程权威及三事件：`docs/maintenance/20261004-rg-no-match-r1/INCIDENT.md`、`docs/maintenance/20261004-grok-headless-cancel-r1/INCIDENT.md`、`docs/maintenance/20261016-r1-custody-path-normalization-r1/INCIDENT.md`。仅optional rg raw1且空stderr为NO_MATCH；源/child/receiver/custody/artifact/verdict分轴；输出先声明、Path归一化、完整序列化再写、进程结束后封存流、保留所有失败与一次性请求。trading=false、observer=hold、broker=NOT_RUN、real send/cancel=0；凭据/DB/SDK/fit/服务/UI均不在本单动作范围。

## 实际字节引用

```json
[
  {
    "role": "original_plan",
    "path": "D:/Projects/AK-trading/docs/strategy-first-blueprint-20261015-r1/implementation-clarifications/backtest-input-delivery-20261015-r1/DAILY-M-PRICE-CLOCK-EVIDENCE-20261024-001.md",
    "bytes": 17390,
    "sha256": "2c35a4a0931737a0d6ea3cd4ae66f71b3ba7ae06c42518733175e62b11114b36"
  },
  {
    "role": "original_fact_clarification",
    "path": "D:/Projects/AK-trading/docs/strategy-first-blueprint-20261015-r1/implementation-clarifications/backtest-input-delivery-20261015-r1/DAILY-M-PRICE-A-FACTS-20261024-001.md",
    "bytes": 9225,
    "sha256": "f52d41940aea6266c0e3cee5f4eeb2cc7c254b122e6700a0ec3351344131da3d"
  },
  {
    "role": "advisory_only",
    "path": "D:/AKRV261024/R1-M-CLOCK-PLAN-001/OWNER-REPORT.md",
    "bytes": 8104,
    "sha256": "8ab5c60a66e36ac483daf563f3b92343976c10d71ac3d21f3d208f794298685c"
  },
  {
    "role": "actual_advisory_result",
    "path": "D:/AKRV261024/R1-M-CLOCK-PLAN-001/RESULT.json",
    "bytes": 862,
    "sha256": "4cfea68dabaa99ba6781727cb4a52f295ddae2cbff6c844abb1f2df96bdd641e"
  },
  {
    "role": "actual_advisory_terminal",
    "path": "D:/AKRV261024/R1-M-CLOCK-PLAN-001/TERMINAL.json",
    "bytes": 349,
    "sha256": "6b44e1aefc8226794da83e12051d6dfc1638c161c2862690a354f881d4df4337"
  },
  {
    "role": "source_records",
    "path": "D:/Projects/AK-trading/data/downloads/backfill_contract_progress.jsonl",
    "bytes": 3712001,
    "sha256": "759502e2b9c6b36a537847ed841e266d2e880040c4311b2ca8b73e948b760c9c"
  },
  {
    "role": "preserved_record_version",
    "path": "D:/Projects/AK-trading/data/downloads/backfill_contract_progress.jsonl.bak_2356",
    "bytes": 4087392,
    "sha256": "0c3180b9acebd79b48903b1767681a87c0bb91120db04568eab8110765b421e5"
  },
  {
    "role": "Root_M_resource_scope",
    "path": "D:/Projects/AK-trading/docs/parallel-development-20261004-r1/coordination/current-zcode-capacity-20261015-001/PARALLEL-CAPACITY-FACTS-20261024-029.json",
    "bytes": 5415,
    "sha256": "0c86790a4861b1fcb9c6621db4fdcf91ba5585980dfe41c25d32b6d47eda58b4"
  }
]
```

作者发布 UTC：2026-10-24T05:27:34.732149+00:00。
