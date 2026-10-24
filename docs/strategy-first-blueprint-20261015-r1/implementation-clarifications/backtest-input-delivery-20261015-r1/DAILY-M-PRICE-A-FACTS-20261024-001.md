# M/SC 原始价格可用时间：原作者事实更正

作者：原 BIP1 Astra/max `/root/astra_backtest_input_delivery_plan_20261015`。本单是事实澄清，继续下一份可执行来源计划；不是任务终点，不新增批准门，不发布 M 数值时钟。

**已存 M/SC 的 2026 `available_at` 是 legacy 导出器有意采用的本地 pipeline 导入可见下界。它使这份原包无法直接用于 2023—2024 决策，但不是已观察到的当年市场迟到/首次公开时间。** 作者此前将整列概称为实际历史 late A，语义范围过宽，本单明确更正。原 CSV、2026 值、旧失败和声明全部保留。

## 1. 已核验的完整原件与字段语义

`data/strategy_artifacts/futures-lib-20260907-codex-01/real-input-02/` 的五文件全部匹配其 manifest。该原件为 `futures_bundle_v1/real_export`，legacy `complete` 不表示 v2 合格；含 M/SC，不含 I。作者仅遍历 symbol、时间、来源列，未读取 OHLC 数值、策略收益或容量统计来选择来源。

| 产品 | 全部已存行 available_at 最小值 | 最大值 | 有任何 A≤W.end 的行 |
| --- | --- | --- | --- |
| DCE.m | 2026-09-01T14:52:47.954783+08:00 | 2026-09-06T18:09:41.177986+08:00 | 否 |
| INE.sc | 2026-09-01T17:00:54.266627+08:00 | 2026-09-06T18:13:11.106288+08:00 | 否 |

A 均非空；全范围 source family 包含 `tqsdk` 与 `edb_free`；部分行有 trading_day，不能从首条空值推出全表皆空。M/SC 首条分别为 DCE.m2011/INE.sc2010，其日期标签不自动变合法 session end。W 仍为原 `[1672707600000000000,1729256400000000000)`；这只是来源可用性核验，不是 raw-bound/capacity 运行。

当时的 `handover-1.json:/export_evidence/real_input_01/defect` 明写第一份导出未把 imported_at 映射至 available_at；`/export_evidence/real_input_02` 明确修正后第二次导出及同一 manifest SHA。该同期声明把当前原件与导入映射联系起来；不能把当前代码 hash 冒称 2026-09-07 的运行代码 hash。

当前实际源码进一步解释这条语义：`persistence/futures_research.py:318–336` 只从 imported_at 构造 ContractBar.available_at；`futures/io.py:61` 明注 pipeline import time，179–204 保留 available_at；导出调用 931 行没有传 extra，因此 batch_id/imported_at 原始列未经这个中间对象保留。CSV 的空原列不是数据库对应值为空的证明，不能反向补写它们。manifest.created_at 是另一个导出时钟，也不能代替该列。

## 2. 当前合同究竟允许什么

M1 §5.3 明确区分：实际迟到/补齐/修订按实际可见时点；历史价格可以在正式、事前冻结的政策下采用 `reconstructed` 收盘加延迟，并保留原 imported_at。§9.5 同时禁止把历史导入时间充当首发时间。因此现行 v2 合同并未要求所有历史研究都把本地导入日等同于当时市场公开日，也未授予自动改写字段的权限。

已接受 `rb-price-availability-v1` 的 60 秒是固定 RB 源的研究假设，原裁决明说它不是实测延迟或安全上界，不能继承给 M/SC。现有 AU 公告 A 政策亦不能扩到价格。当前 M 没有已生效的专属数值价格时钟，作者本单不凭空补一个。

未来若有 M 合法重建后继，必须保存原 bytes/列/语义及取得时间，在新派生记录中分开表达原导入下界、真实观察 A（有证据才有）和明确标记的 reconstructed research A；不能宣称历史观察 PIT，也不能覆盖真实晚到或已知修订。新政策不豁免真实会话/完成/质量/人口/来源/费用/限价等门。

## 3. 现有原件能证明和不能证明的关系

- manifest、五原件及同期 handover 支持真实导出谱系、导入时钟语义和完整内容 pin；不支持当年市场 first_seen。
- `metadata.json:/revision_history_unavailable=true` 保留。源字符串只能定位数据系列，不能证明原发布 vintage 或不存在修订。未知修订史如实披露，不新增“必须恢复全部当年系统首见”的统一门；已知修订不得忽略。
- source bundle 明记 `bar_completion=raw_timestamp_only_no_session_evidence`、缺 session_windows；cache listing_at 缺失。现有样本日期、交易月份规律和 SDK expiry 不能替代 M 真实日历及法律 L/X。
- DCE〔2024〕419 原通知有 2024-09-24 签署和 09-27 结算起的 M 风控参数；其 snapshot 自动 item 却写 effective_from=2024-09-02，不能用 item。通知不能解决价格发布/修订、完整人口或全历史会话。〔2022〕99 的 M 规则附件 lead 同样不是价格 A；保存的一个 .doc 响应是 UTF8 replacement 解码串，不是可恢复 Word 原件。这些原件不重抓。

**截至本单，没有已验证可立即喂给 v2 真实回测的 M/SC 输入。** 这既不是市场从未公开这些价格的结论，也不是全部研究永久不可行。下一份最小计划必须直接处理 M 源行语义、完成/发布/修订与可采用时钟的证据和技术决策；单独继续当前规则公告或生命周期收集不能代替它。完整目标仍为正确策略与真实回测/模拟，不以兼容、封存或 SCENARIO 自称完成。

作者本轮 network/SDK/DB/凭据/产品修改/fit=NOT_RUN。图谱 Verify：AK-trading-current-goal-20261016，generation 2026-10-15T17:59:22Z；五证据路径 coverage 均 metadata_changed，已用实际源码定点核对。一次整个 docs 的可选文字搜索被 30 秒超时终止，Python harness native1/TIMEOUT，未取得正常 rg 终态；保留工具 `28cf87`，不冒称 NO_MATCH。另一个本地 Python 正则诊断输出 SyntaxWarning、native0，原 warning 保留，不称无诊断。未重跑被消费的网络请求。

## 精确引用

```json
[
  {
    "path": "D:/Projects/AK-trading/docs/devplan-v2/01-M1-共用基础与时钟契约.md",
    "bytes": 70562,
    "sha256": "b736cc9bf433b910bfb3972b0d4d6f5c7b921a14d62037461d1c0ecd35ef584a"
  },
  {
    "path": "D:/Projects/AK-trading/data/maintenance_artifacts/v2-real-data-track/input-builder-prep-m1/policy-arbitration.md",
    "bytes": 27043,
    "sha256": "154f09d5888f6f80399bf9e76e6e58f4600b42432e22762758ca2e6e64613bd4"
  },
  {
    "path": "D:/Projects/AK-trading/data/strategy_artifacts/futures-lib-20260907-codex-01/handover-1.json",
    "bytes": 13952,
    "sha256": "261c7c68ef6f0488d086db48063b38db20aa9e6f016019335f871c6e79b01d16"
  },
  {
    "path": "D:/Projects/AK-trading/src/ak_trading/persistence/futures_research.py",
    "bytes": 46120,
    "sha256": "261bd7e3e3cbc0c781242f07415f718ef285d2153de28ea5c7b873ebd5f51213"
  },
  {
    "path": "D:/Projects/AK-trading/src/ak_trading/futures/io.py",
    "bytes": 33454,
    "sha256": "b4498c0ee5ddb68cf4c744d24decd963a3d238f58eca4e51b3e458c8c023f35b"
  },
  {
    "path": "D:/Projects/AK-trading/src/ak_trading/v2/data.py",
    "bytes": 25102,
    "sha256": "16bb89c5a78cbb35a084ee2ef122838b91cf18c33597430db910583fce402e3d"
  },
  {
    "path": "D:/Projects/AK-trading/data/strategy_artifacts/futures-lib-20260907-codex-01/real-input-02/manifest.json",
    "bytes": 1696,
    "sha256": "99366eb8310222957d2dc896df1e6781982b9995a91c352f106bfbbfd77493a3"
  },
  {
    "path": "D:/Projects/AK-trading/data/strategy_artifacts/futures-lib-20260907-codex-01/real-input-02/bars.csv",
    "bytes": 25605988,
    "sha256": "d77cf7e6574dfb702ef039d3f1c3c16004078015ebebd7e6be68ebd1cd2cf800"
  },
  {
    "path": "D:/Projects/AK-trading/data/strategy_artifacts/futures-lib-20260907-codex-01/real-input-02/contracts.json",
    "bytes": 158883,
    "sha256": "c70e09b15927f6f93ea5ba7368c2ec3b88f76780a83cc174f6a68effc30304c2"
  },
  {
    "path": "D:/Projects/AK-trading/data/strategy_artifacts/futures-lib-20260907-codex-01/real-input-02/availability.json",
    "bytes": 8145,
    "sha256": "7e963796a1a41c51cd08da8d430954385d25cc1a4a26364ec20d91352348ae0f"
  },
  {
    "path": "D:/Projects/AK-trading/data/strategy_artifacts/futures-lib-20260907-codex-01/real-input-02/metadata.json",
    "bytes": 384,
    "sha256": "398e792e7f8d3e58fdbec46fa6cd3694b27d7ffd8741abd9298a987bff8a8660"
  },
  {
    "path": "D:/Projects/AK-trading/data/exchange_rules/documents/1b/1b96c3766d2e6e160e797f32e09689eb42c55bc3c650113ab00a647b7a96b3aa.json",
    "bytes": 8312,
    "sha256": "1b96c3766d2e6e160e797f32e09689eb42c55bc3c650113ab00a647b7a96b3aa"
  },
  {
    "path": "D:/Projects/AK-trading/data/exchange_rules/snapshots/dcccbd3d33cdd9a59a0a7b6d404548073c8bed2e920867340fdabe411f41bbe8.json",
    "bytes": 942056,
    "sha256": "c8b5acdb09e06e467e23043dcec14ffa430ec3a9c5ec65d661c0dea44c75c8e8"
  },
  {
    "path": "D:/Projects/AK-trading/data/exchange_rules/documents/d4/d4a4942e6a0a4a0c1b18f231d4daf2515a5c0d8e74dabc3dc42cd7fc51aaefbb.json",
    "bytes": 5807,
    "sha256": "d4a4942e6a0a4a0c1b18f231d4daf2515a5c0d8e74dabc3dc42cd7fc51aaefbb"
  },
  {
    "path": "D:/Projects/AK-trading/data/exchange_rules/documents/19/19e4208f6ce7e2a6455f69b5eb4bbe394c894d6d911bc1aaaffe767144acd77a.json",
    "bytes": 399130,
    "sha256": "19e4208f6ce7e2a6455f69b5eb4bbe394c894d6d911bc1aaaffe767144acd77a"
  }
]
```

作者发布 UTC：2026-10-24T04:28:53.902003+00:00。
