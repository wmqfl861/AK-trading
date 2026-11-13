# K12 分钟线三档 profile：BLOCKED

日期：2026-11-13（UTC）。本表的 5、20、60 个交易日均指**完整 workload 日历的总长度**。本次仅完成来源核验与 `SYNTHETIC_STATIC_ARITHMETIC` 前置计算，未构造三档数据包、未执行生产 fold 或性能测量。**阻塞表已交付，K12 性能任务仍为 BLOCKED。**

有效方法为本机 `C:/AKH71113/plans/k12/METHOD.md`，10,268 bytes，SHA-256 `c6854e5fc9339b5f60607cdb8c9fe0a29dbd88c27940f86dcf1e8aa6ed027826`。口径依据为 [第五轮 K12 与执行边界](../repo-audit-20261113-claude/NEXT-BATCH.md)；[J4](../repo-audit-20261113-claude/DECISIONS.md) 要求实际量到单个 fold 的模型拟合占比超过 30% 才能启动 K13，本表不能作该判断。

原 H8 配置的四项覆盖字段为 `inner_split_fraction=0.86`、`inner_minimum_train_days=10`、`inner_minimum_validation_days=51`、`inner_embargo_days=1`。[生产函数 `_payoff_inner_split`](../../src/ak_trading/v2/validation/experiments.py#L1062) 从 outer TRAIN 内取得 L 个完整交易日，使用以下必要条件：

```text
border = int(0.86 * L)
minimum = 51 + 1 = 52
border >= 10 且 L - border >= minimum + 2 = 54
```

若完整日历只有 N 日，则 `0 <= L <= N`。下表枚举了该范围的所有整数 L；即使把整个日历都给 TRAIN，仍无法满足必要条件。54 来自生产原式中的 51 个验证日、1 个 embargo 日及额外的 2 日，没有调整任何参数。

| 完整日历 N（日） | TRAIN 日数上界 | L=N 时 border | 所有 L 的剩余日数上界 | 必需剩余日数 | 可行 TRAIN 日数集合 | 算术命令 raw | K12 状态 |
| ---: | ---: | ---: | ---: | ---: | --- | ---: | --- |
| 5 | 5 | 4 | 1 | 54 | 空 | 0 | BLOCKED |
| 20 | 20 | 17 | 3 | 54 | 空 | 0 | BLOCKED |
| 60 | 60 | 51 | 9 | 54 | 空 | 0 | BLOCKED |

5 日档的 `border=4` 也不足最少训练日数 10。其余两档同样受剩余日数限制。[`_payoff_training_plans`](../../src/ak_trading/v2/validation/experiments.py#L1106) 在 inner 不存在时返回空计划集。此证明只检查完整 fold 的一个必要条件，未声称其他输入、模型、日历或锁前置已经通过。

| 完整日历（日） | cProfile／tracemalloc 执行 | 累计耗时 Top20 | 函数耗时增长倍数 | 峰值内存 | 单个 fold 模型拟合占比 |
| ---: | --- | --- | --- | --- | --- |
| 5 | NOT_RUN | NOT_RUN | NOT_MEASURED | NOT_MEASURED | NOT_MEASURED |
| 20 | NOT_RUN | NOT_RUN | NOT_MEASURED | NOT_MEASURED | NOT_MEASURED |
| 60 | NOT_RUN | NOT_RUN | NOT_MEASURED | NOT_MEASURED | NOT_MEASURED |

没有三档实际 profile 样本，未计算增长率、内存或拟合比例，未用旧长作业代替。旧 H8 工具在结构分类为 `PROVED_IMPOSSIBLE` 时于计时前返回 3，这是源码的退出契约；**本轮没有实际 profile raw 3**，也未声称三份未生成输入已实际运行到该分支。锁或输入检查可能更早拒绝。

来源通过原工单 argv 程序化定位，并逐字节计算 hash。身份核验命令 raw 0，stderr 为空；未导入或调用产品。

| 来源 | 路径 | SHA-256 |
| --- | --- | --- |
| 原 H8 工单 | `C:/AKH1107/sol-handoff-20261110/h8-support-sol/h8-profile09-cdfsol-dev-job.json` | `357eb8ee1addc55a259074220b49aa202fc4d172edc4fb5e9f9800aa57620cb4` |
| 原 H8 config | `C:/AKH/h8cdf1/config.json` | `d778c207e4cf2ba7703bb1a154b3042c97d0f8496b199e344ddb119394e0976c` |
| 保留的 H8 工具 | `C:/AKH1107/h8next04/scripts/profile_v2_fold.py` | `ac8900a10f8e49391b12d879f8d0257c08bf1564517244a3af147c21e444ffe8` |
| 生产前置公式 | `src/ak_trading/v2/validation/experiments.py` | `e5c4f3159a9f3ecee4f359419fdfc6523971b3daaa14e062e48a6aaab7c91b65` |

本次成功采集标识为 `k12-preflight-20261113T063130Z-289a3e2f`，cwd 为 `D:/Projects/AK-trading`，解释器为 `C:/AKR41110/venv-full/Scripts/python.exe`（Python 3.13.15）。实际调用使用参数数组，无 shell 拼接；每个命令的完整 argv、原始 stdout/stderr、原生退出码及终态分别保存在仓库外的 `*-command.json`、`*-stdout.txt`、`*-stderr.txt`、`*-result.json`。

| 命令 | 实际调用与脚本体来源 | 原生退出码 | stderr | 含义 |
| --- | --- | ---: | --- | --- |
| 来源核验 | `C:/AKR41110/venv-full/Scripts/python.exe -B -X utf8 -c`，单一脚本参数为 `identity-command.json` 的 `argv[5]` | 0 | 空 | 上述源码、工单、config、METHOD 身份和四字段一致 |
| 纯合成算术 | `C:/AKR41110/venv-full/Scripts/python.exe -B -X utf8 -c`，单一脚本参数为下方完整代码，也是 `arithmetic-command.json` 的 `argv[5]` | 0 | 空 | 枚举上界与两项断言通过；性能任务仍 BLOCKED |

算术命令的完整脚本体直接取自有效 METHOD 的 Python 代码块：

```python
import json
fraction = 0.86
train_floor = 10
required_tail = 51 + 1 + 2
rows = []
for total_days in (5, 20, 60):
    tails = [length - int(fraction * length)
             for length in range(total_days + 1)]
    feasible = [length for length in range(total_days + 1)
                if int(fraction * length) >= train_floor
                and length - int(fraction * length) >= required_tail]
    rows.append({"total_days": total_days, "train_days_upper_bound": total_days,
                 "tail_upper_bound": max(tails), "required_tail": required_tail,
                 "feasible_train_days": feasible})
assert [row["tail_upper_bound"] for row in rows] == [1, 3, 9]
assert all(not row["feasible_train_days"] for row in rows)
print(json.dumps({"evidence_kind": "SYNTHETIC_STATIC_ARITHMETIC",
                  "profile_executed": False, "rows": rows}))
```

成功原始捕获及压缩归档均留在本机 `C:/AKH71113/k12/`。归档为 `k12-preflight-20261113T063130Z-289a3e2f.zip`，SHA-256 `46c86881718da2df2db03914cef33c21b714da62b9ff5ee6c402774fb2751e4d`；所有子进程终止后才封存，ZIP 校验通过。

另保留一次真实采集失败：`k12-preflight-20261113T062647Z-0daec562` 的来源核验在原 METHOD pin `2297bb45c9e2756657fd33b1aa005e109adcd93c1811a668f45d21a955aa4b4d` 与作者补来源后的最终版 hash 不一致时以 raw **1** 终止，stderr 为 hash 断言失败；该次算术命令未启动。失败归档 `k12-preflight-20261113T062647Z-0daec562.zip` 的 SHA-256 为 `f7dc8cc43266917ca98d71096660d70fba47500e6f4a2d1d7fd970f19e162dc2`。后续用新标识绑定作者已发布的最终版，未覆盖失败；raw 1 是采集失败，不是 NO_MATCH 或产品 profile 结果。

解除本项覆盖阻塞，需要下一轮明确新的测量口径：在生产门槛下采用足够长的完整 fold，或另行定义“固定合法训练前缀下的短评估区间成本”。本批不自行选择、不以组件 profile 或长 TRAIN 加短评估替代完整 5／20／60 日 fold，不降低 floor、改变 split fraction、embargo、窗口、模型工作量或统计政策。未来合法性能运行还须具备 K10b 接受 main／CI 及既定新完整副本、generic F、独立 R、完整锁的真实前置；这些版本工件不能消除当前覆盖冲突，本次均未生成。

本次唯一仓库写入为本报告，未改旧 T7、源码、测试、配置、输入或 pins，未恢复 profile09、未启动 profile10，未读凭据或最终 holdout。交易边界保持 `enable_trading=False / observer=hold / broker=NOT_RUN / real send=0 / real cancel=0`。本报告按本批测量表规则无需独立产品硬审；K12 性能任务继续以 BLOCKED 记录。
