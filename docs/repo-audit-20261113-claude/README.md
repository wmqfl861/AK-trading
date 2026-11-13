# 第五轮外部审核（2026-11-13）

审核对象：`main@90cb0e9`。上一轮：[repo-audit-20261110-claude](../repo-audit-20261110-claude/README.md)。

这些文件是用户委托的外部审核意见，不是项目规则中的产品硬审核。本轮没有修改任何产品代码、测试、规则文件或冻结工件。

## 一段话结论

上一轮交出去的第一批做完了，另外还多做了两批，三次产品改动都过了独立硬审和 CI，交接文档里能核对的说法全部属实；我在 Linux 上对改动前后各跑一次全量，共有的 5,479 项用例里没有一项从通过变成失败。离你最近的产出是一份早就跑完、通过了审核却没人读出来的 v1 七年分年结果，所以下一批的关键线只做一件事：把它写成一页报告。v2 这边，震荡三族的修复只在手工设定的模型上成立，时钟索引在真实调用方式下只快了 6 到 8 倍，真实数据进入 v2 的口径还没有定义；下一批先把这几件事量清楚，产品代码只做一处不改数值的小提速。安全项连续第五轮未动。

## 文件

| 文件 | 给谁 | 内容 |
| --- | --- | --- |
| [DECISIONS.md](DECISIONS.md) | 用户、本地 agent | 按用户 11-13 的指示登记的决定 J1–J18；哪些事不适用默认值；只有用户本人能做的事；规则文件的改法 |
| [REVIEW.md](REVIEW.md) | 用户、计划作者 | 对三批交付的逐项核对；新发现 Z01–Z12、Y09–Y12；优化意见；对上一轮建议的更正；复核命令 |
| [NEXT-BATCH.md](NEXT-BATCH.md) | 计划作者、本地 agent | 第二批的任务、写集、验收、检查点；第三批的候选；问题跟踪表 |
| [AGENT-PROMPT.md](AGENT-PROMPT.md) | 用户 | 发给本地 agent 的提示词 |

## 探针

都在 [probes/](probes/) 下，只读，不修改产品树。在仓库根目录运行。

| 文件 | 对应 | 在 `90cb0e9` 上的结果 |
| --- | --- | --- |
| `test_cross_timeframe_reachability_probe.py` | Z01、Z08 | 16 失败、14 通过。失败就是发现 |
| `clock_tuple_identity_bench.py` | Z02 | 250 个交易日的日历上，每个上下文新建元组约 20 毫秒，复用同一个元组约 0.02 毫秒 |
| `label_session_cache_identity_probe.py` | Z09 | 退出码 1：50 次里 50 次取到已释放日历的结果 |
| `preflight_capital_tiers_demo.py` | Z03 | 五档资金各跑一次预检：10 万元及以下报“未知”、退出码 4；校准样本上限按整个窗口计 |
| `compare_offline_logs.py` | Z07 | 工具，不是探针：按用例比较两份离线入口日志 |

```bash
python scripts/test_offline.py -q -s docs/repo-audit-20261113-claude/probes/test_cross_timeframe_reachability_probe.py
python docs/repo-audit-20261113-claude/probes/clock_tuple_identity_bench.py
python docs/repo-audit-20261113-claude/probes/label_session_cache_identity_probe.py
python docs/repo-audit-20261113-claude/probes/preflight_capital_tiers_demo.py
python docs/repo-audit-20261113-claude/probes/compare_offline_logs.py BEFORE.log AFTER.log
```

上一轮的两个探针现在的结果：入场确认 9 项全过；震荡族跨周期 5 过 6 败。后者的 6 项失败是探针按修复前的行为写死的预期，在已生效的裁定下应当失败，留在基线里即可，不需要处理。
