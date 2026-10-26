**A2-M4-CONTRACT-RULING.md — 原作者生效裁定，2026-10-26**

**裁定：保留 phase 0 F1 的过期清零规则，修正自然 M4 正例 fixture 的决策时点。** 本文件增补原计划及处置，立即生效；无需新一轮计划意见或计划 PASS。

`FACTS.json` 及其指向的完整结果记录为 **raw 1：962 selected、922 passed、14 个既有失败、26 个新 setup errors**。26 项共用 `natural_nonempty_range_case`，在准备自然 OPEN 时失败，不能按既有十四项失败豁免。

源码中的 `setup_expiry_nano` 将创建 bar 算作第 1 根；该 TREND setup 的第三根期限为 `base + 100 * _RANGE_MINUTE`。fixture 当前将三次决策设在分钟边界之后，第三次已超期，F1 要求计数回到 1，因此轨迹 `1,2,1` 符合本次有效裁定。单位同时澄清：当前 `_RANGE_NS = 1_000_000_000`，原 fixture 加的是 **1 秒**；FACTS 中“1ns”的表述不作为数值依据，原记录保留。

合法的正例应在有效期限内完成三个不同 bar 的确认。当前公共路径允许已完成且 `available_at_nano <= decision_nano` 的 bar；发布时钟可等于决策时刻，预测窗口起点严格晚于决策。因此在完成 bar 的边界决策符合现有合同，**无需延长生产 expiry 或放宽实时迟到守卫**。

**1. 精确追加写权**

在原十文件 lease 上，仅追加第十一文件：

`C:/AKWorktrees/aktr-round2-a2-20261026/tests/test_v2_m4_payoffs.py`

允许改动只限 **`natural_nonempty_range_case` 内的决策时序、对应注释及下述时序断言/独立边界分支**，当前约 1060–1087 行。不授予整个文件的一般修复权。

保留 `(98, 99, 100)` 三次观察，将：

```python
now = base + minute_index * _RANGE_MINUTE + _RANGE_NS
```

改为：

```python
now = base + minute_index * _RANGE_MINUTE
```

provider 继续使用该次实际传入的 `now`，真实调用 `predict_market` / `publish_market_forecast`。原 FrozenInput、行情、会话、模型、证据、策略规格及 SYNTHETIC/SCENARIO 标签不变；不得改 `_RANGE_NS` 常量、缩短确认要求、操纵计数、伪造 OPEN 或修改预测结果时间字段。

**十四个历史绑定常量、全部原节点、其他断言以及 fixture 返回接口保持不变。**

**2. 必须补实的边界控制**

在该 fixture 的时序局部保存真实前态及期限，并检查：

- 三次实际使用的最后完成 bar 为不同 ID，均已完成、已可见，`end_nano == now`。
- 保存首次建立 setup 的真实 `expires_at_nano`；三次正例决策均不超过该期限，第三次恰等于它。比较保存的原期限，不能借第三次返回的新 setup 期限证明合法。
- 原断言继续要求 **`WAIT, WAIT, OPEN`、计数 `1,2,3`**，以及原 inference/publication 调用、无预算提交和无 pending 意图断言。
- 从**第二次确认后的同一真实前态**另作独立晚到分支：在保存期限 **`+1` 纳秒**重新构建 context，使用该分支自己的发布时钟，走相同真实推理、发布、策略评估及观察提交。应为 **WAIT、计数 1**，不得 OPEN。该分支不得写回正例状态或污染正例 trace、context、proposal。
- 现有 A2 同 bar 去重及过期控制全部保留。上述断言放在既有 fixture 内，不增删测试节点，保持预期总人口 962。

这使 `<= expiry` 的合法正例与 `> expiry` 的拒绝控制同时成立。若真实路径不满足这些断言，按实际失败处理；不得改生产期限、伪造预测或削弱其余 26 项验证来通过。

**3. 同一 owner 续接与验证顺序**

只续接 ZCode owner **`sess_e3bb79de-f13b-48fc-9db9-a485edfccb7f`**，保留原 REQUEST 的精确 cwd 和固定 store `zcode-dispatch/a2-owner-002/session/session.sqlite`。Root 须等当前调用自然终止、交接材料封存后再 resume；不得并发写入或另换 owner。

执行顺序固定：

1. **先验证受影响的 26 项**：按 `FACTS.json.error_nodes` 的节点集合执行，去除报告附加的 ` (setup)` 标记。必须恢复为 26 passed、无 setup error，并完成上述期限两侧控制。
2. 通过后执行原规定 **focused 110、原样外部 probe 9、完整 M3B 308**，全部通过且无 skip。
3. 再执行原完整 M1–M4 **962** 人口，预期 **948 passed、14 个同节点同原因的既有失败、0 error、0 skipped、0 collection error，raw 1**。不先耗用完整运行去调试尚未通过的 fixture。
4. 保留每次真实 argv、模块来源、原生退出及输出。十四项须逐节点核对失败原因；仅数量相同不足以证明未新增回归，不刷绑定哈希。

完整调用预算 **3600s**，聚焦/M3B **600s**，原资源限制不变。候选仍使用其本地 `src`，确认实际导入路径，禁止借 MAIN 的 editable install。

原 `922P/14F/26E` 结果及此前静态评阅记录保留。修复后重新冻结实际十一文件提交范围，立即交独立 ZCode 硬审；此前静态评阅没有正式 verdict，修改后的 fixture 不能借用旧接受信用。旧 T7-B 冻结树、MAIN 既有 M4 文件及历史接受记录不由本裁定直接修改。

本作者未执行测试、未修改产品、未签产品 PASS。原命令退出及事故规则继续有效，包括 `20261004-rg-no-match-r1`、`20261004-grok-headless-cancel-r1`、manifest-recovery-002 和 custody 规则。交易边界保持 `enable_trading=False`、observer=`hold`、broker=`NOT_RUN`、真实 send/cancel=`0/0`。