# 第六轮外部审核（2026-11-13）

审核对象：`main@3fc62d2`。上一轮：[repo-audit-20261113-claude](../repo-audit-20261113-claude/README.md)（第五轮，审核的是 `90cb0e9`）。

这些文件是用户委托的外部审核意见，不是项目规则中的产品硬审核。本轮没有修改任何产品代码、测试、规则文件或冻结工件。

## 一段话结论

第二批做完了，仓库和 GitHub 上能核对的说法全部属实；Linux 上前后各跑一次全量，共有的 5,741 项用例没有一项变化。第一份策略报告已经入库，结论是负面的：三个 v1 日线规则在螺纹钢 84 个合约上的净额合计全部为负，不如买入持有，也不如不交易。这份结果只来自一个品种的一种口径，所以下一批不调参数，先补主力合约口径和另外五个品种。v2 这边，时钟的问题已经解决，另外找到三处随训练样本量增长的开销，都能改成结果逐位相同的写法；现有的真实输入离正式实验的门槛很远，所以 v2 分钟线的工程项暂停，先设计一次便宜的检验，看 v2 的预测在真实数据上有没有信息量。安全项连续第六轮未动。

## 文件

| 文件 | 给谁 | 内容 |
| --- | --- | --- |
| [DECISIONS.md](DECISIONS.md) | 用户、本地 agent | 本轮的决定 J19–J31；此前 J1–J18 现在的状态；哪些事不适用默认值；只有用户本人能做的事；规则文件的两处替换 |
| [REVIEW.md](REVIEW.md) | 用户、计划作者 | 第二批的逐项核对；第一份策略报告怎么读；新发现 Z13–Z22、Y13；优化意见；对上一轮建议的更正；复核命令 |
| [NEXT-BATCH.md](NEXT-BATCH.md) | 计划作者、本地 agent | 第三批的任务、写集、验收、检查点；第四批的候选；问题跟踪表 |
| [AGENT-PROMPT.md](AGENT-PROMPT.md) | 用户 | 发给本地 agent 的提示词 |

## 探针

都在 [probes/](probes/) 下，只读，不修改产品树。在仓库根目录运行。

| 文件 | 对应 | 在 `3fc62d2` 上的结果 |
| --- | --- | --- |
| `k40_published_tables_recheck.py` | 第一份策略报告 | 退出码 0：7,195 项算术核对全部通过 |
| `pipeline_context_reuse_bench.py` | K10b | 退出码 0：第一个上下文之后不再重建时钟索引；250 个交易日的日历上每个上下文从约 56 毫秒降到约 27 毫秒 |
| `empirical_apply_scaling_bench.py` | Z13 | 退出码 1（问题存在）：训练样本多 100 倍，一次预测慢约 100 倍；模型集哈希第二次调用和第一次一样慢 |
| `context_cost_profile.py` | Z13、Z20 | 只是测量：一个上下文的耗时花在哪里 |

```bash
python docs/repo-audit-20261113-r6-claude/probes/k40_published_tables_recheck.py
python docs/repo-audit-20261113-r6-claude/probes/pipeline_context_reuse_bench.py
python docs/repo-audit-20261113-r6-claude/probes/empirical_apply_scaling_bench.py
python docs/repo-audit-20261113-r6-claude/probes/context_cost_profile.py MINUTE 250 TREND
```

上一轮的探针现在的结果：标签缓存探针的退出码由 1 变为 0（K10b 已修复）；跨周期可达性探针仍是 16 败 14 过，等 K45 给出模型概率分布之后再裁定（DECISIONS 的 J22）。
