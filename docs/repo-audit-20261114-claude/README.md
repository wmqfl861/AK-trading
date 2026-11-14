# 第七轮外部审核（2026-11-14）

审核对象：`main@b08c444`。上一轮：[repo-audit-20261113-r6-claude](../repo-audit-20261113-r6-claude/README.md)（第六轮，审核的是 `3fc62d2`）。

这些文件是用户委托的外部审核意见，不是项目规则中的产品硬审核。本轮没有修改任何产品代码、测试、规则文件或冻结工件。

## 一段话结论

第三批做完了，能核对的说法全部属实；Linux 上前后各跑一次全量，共有的 5,819 项用例没有一项变化，新增的 237 项全部通过。六个品种的日线基准已经入库，结论是三个规则没有显示出稳定的优势：18 个“品种 × 规则”里按主力口径 8 个赚、10 个亏，好过“一直持有主力合约”的只有 3 个；其中股指的结果不能用，因为规则在大多数合约上从未开仓。这条线出完第二版报告就收尾，不再调参数。K14 达到了目的。K45 的设计在统计方法上是严谨的，但照现在的写法实施会白跑：r2 的分钟线“收盘后 60 秒可用”，按生产规则交易节内做不出任何可用的特征快照；设计还让生产函数把 23 类任务全部拟合一遍，比只拟合方向贵约 60 到 70 倍。本轮把这两处改掉，第四批把检验真正跑一次。安全项连续第七轮未动。

## 文件

| 文件 | 给谁 | 内容 |
| --- | --- | --- |
| [DECISIONS.md](DECISIONS.md) | 用户、本地 agent | 本轮的决定 J32–J42；此前 J1–J31 现在的状态；哪些事不适用默认值；只有用户本人能做的事 |
| [REVIEW.md](REVIEW.md) | 用户、计划作者 | 第三批的逐项核对；六个品种的报告怎么读；新发现 Z23–Z31；优化意见；对上一轮建议的更正；复核命令 |
| [NEXT-BATCH.md](NEXT-BATCH.md) | 计划作者、本地 agent | 第四批的任务、写集、验收、检查点；第五批的候选；问题跟踪表 |
| [AGENT-PROMPT.md](AGENT-PROMPT.md) | 用户 | 发给本地 agent 的提示词 |

## 探针

都在 [probes/](probes/) 下，只读，不修改产品树。在仓库根目录运行。后四个用合成数据调用生产函数。

| 文件 | 对应 | 在 `b08c444` 上的结果 |
| --- | --- | --- |
| `k44_published_tables_recheck.py` | K43、K44 的报告 | 退出码 0：32,467 项算术和引用核对全部一致 |
| `minute_anchor_availability_probe.py` | Z23 | 退出码 1（问题存在）：分钟线收盘后 60 秒可用时，交易节内 65 个取样时刻里可用的是 0 个；延迟 1 到 59 秒时 65 个全部可用 |
| `k45_fit_cost_probe.py` | Z24 | 只是测量：分钟模式五个期限，全部任务 7 到 17 小时，只拟合方向 7 到 14 分钟 |
| `feature_history_scaling_probe.py` | Z28 | 退出码 1（问题存在）：可见历史长 102 倍，一个特征快照慢约 13 到 15 倍；同一时刻重复构造再慢 6 到 7 倍 |
| `model_hash_residual_bench.py` | Z26、Z27 | 退出码 1（问题存在）：从 JSON 读回的模型集，内容哈希每次调用是直接重算的约 6 倍；收益模型和行情状态参考的哈希没有缓存 |

```bash
python docs/repo-audit-20261114-claude/probes/k44_published_tables_recheck.py
python docs/repo-audit-20261114-claude/probes/minute_anchor_availability_probe.py
python docs/repo-audit-20261114-claude/probes/k45_fit_cost_probe.py
python docs/repo-audit-20261114-claude/probes/feature_history_scaling_probe.py
python docs/repo-audit-20261114-claude/probes/model_hash_residual_bench.py
```

上一轮的探针现在的结果：`empirical_apply_scaling_bench.py` 的退出码由 1 变为 0（K14 已修复）；`pipeline_context_reuse_bench.py` 和标签缓存探针仍是 0；`context_cost_profile.py` 量到的一个上下文从约 28 毫秒降到约 17 毫秒（分钟模式）。`k40_published_tables_recheck.py` 是按螺纹钢报告的旧版正文写的，报告改版后它找不到小标题而报错，螺纹钢的数字改由本轮的 `k44_published_tables_recheck.py` 覆盖。
