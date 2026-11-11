本周能看到什么：K01–K05源码硬审通过并已推送。
卡在哪：合同锁与H7正式输入仍缺前置。
需要我做什么：本批无新增授权请求。

# 第四轮第一批状态（2026-11-11）

范围仅K03→K01/K02/K05→K04；有效计划r3-001、H1–H8及原Astra裁定。Astra/max计划，ZCode开发，另一独立ZCode GLM-5.3/max硬审。离线源码/工具及实际暂存发布版本均**无条件PASS**。产品提交 **`d614360dda423a8ef35fbf581afd43c2fe28fcf3`** 已推送main；[CI 42985971212](https://github.com/wmqfl861/AK-trading/actions/runs/42985971212)**success：1205 passed / 4 skipped / 0 failed，测试219.60s**。K01–K05离线源码/工具已完成硬审、推送及CI；本批停止，不开下一批。

| 项目 | 成果与独立验收 | 剩余限制 |
|---|---|---|
| K03 | 严格对照工具、52项测试、真实完整基线；PASS | 两个UUID收集编号导致compare均raw2；精确人口等价27/27独立核实，不宣称自动compare成功 |
| K01 | 六族观察bar过期与DAILY交易日日历，66项时钟测试；PASS | MINUTE、风险和统计门槛不改 |
| K02 | 方案甲的ALIGNED/CONFLICT/UNKNOWN区分；PASS | 外部跨周期六项未采纳断言保留 |
| K05 | SYNTHETIC真实形状自然链57项，六族OPEN、WAIT不开；CI增加Forward/自然链/原重复探针；PASS | 不冒充历史收益或研究验收 |
| K04 | 预检、35项测试及11项集成反例，接入日历helper；PASS | 对真实H7的预检NOT_RUN，输入/配置未选 |

**作者语义与外审差异：** 最新已完成、可用、同频bar.end严格大于expiry才过期，等号有效；无合格bar为WAIT0。DAILY建立日算1，按trading_day最后收盘取第n日；日历不足返回None/WAIT0/SETUP_CALENDAR_INSUFFICIENT，不外推墙钟日。K02选甲：已发布中性背景ALIGNED，明确方向CONFLICT，缺失/无效UNKNOWN；保留矩阵ALIGNED单测缝，不替代自然链。K04不读holdout价格/ATR，不用训练日数除最长持有期冒充回合上限。14个历史M4常量保留。

## 首次复现及全量对照

外审分支bbc85e0已合入，取得后HEAD `c1904354f99fbc350ad49c0e72e7f1ce5dc1207a`，仅新增外审目录15文件。命令前缀`python -B -X utf8`，`$R4=docs/repo-audit-20261110-claude`。

| HANDOFF §8首次命令 | 原始退出及实测 |
|---|---|
| `scripts/test_offline.py -q tests/test_v2_forward_paper.py` | raw1，20P/48F，与外审一致 |
| `scripts/test_offline.py -q $R4/probes/test_entry_confirmation_reachability_probe.py` | raw1，4P/5F，一致 |
| `scripts/test_offline.py -q $R4/probes/test_range_family_cross_timeframe_probe.py` | raw1，4P/7F，一致 |
| `$R4/probes/clock_horizon_bench.py` | raw0，3000组值/ClockError等价；250日MINUTE本机114.074ms，外审约16ms；K10不在本批 |
| `$R4/probes/budget_feasibility.py`（需真实参数） | NOT_RUN：H7未选输入/配置，不虚构价格/ATR |

固定完整环境Python3.13.15/pytest9.2.0/TqSdk3.10.1，依赖检查raw0。全量命令`python -B -X utf8 scripts/test_offline.py -q --continue-on-collection-errors --basetemp <独立空目录>`；精确argv和原始日志压缩留仓库外。用户允许全量夹具冷链，正式H5不新开。

| 范围 | selected/ran | passed | failed | errors | skip | 收集错误文件 | 原始退出 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 改前全量 | 6033 | 5699 | 241 | 60 | 33 | 8 | 1 |
| 改后全量 | 6244 | 5958 | 193 | 60 | 33 | 8 | 1 |
| 改前全量＋3探针 | 6062 | 5716 | 253 | 60 | 33 | 8 | 各自保留 |
| 改后全量＋3探针 | 6273 | 5981 | 199 | 60 | 33 | 8 | 各自保留 |

前后10域全启动，0未运行/空域跳过/启动失败/未解析；墙钟4795.41s→6075.81s。**新增failed/阶段errors/收集错误0/0/0，真实解决54项（Forward48＋确认5＋range1）；真实新增211项测试全通过，原33项skip身份不变。** K03 record raw0；JSON及rawcapture compare均raw2且无diff：同一未改测试的两个UUID标签变化。原作者限于这两对裁定，独立审核重建全体七用例的进度顺序、F/E与计数，证明实际均PASS且无原逻辑用例丢失；基线/raw2不改写。8项原收集错误来自缺失datapipeline源码。

独立定向：M1–M4 raw0，1079P＋3原REAL_SHARD skip；Forward68＋自然链57＋确认9＋原重复探针9＝143P/raw0；现有重复K线96项包含于M1–M4。跨周期仍5P/6F/raw1，六项不兼容断言保留；原17项审核反例及最终27项人口证据通过。实际发布28文件：27个保留候选原字节、1个仅CRLF/LF转换；21个实际改动均独立核对，未做全库renormalize。

## 单独阻塞与停止边界

`LOCAL_LOCK_BLOCKED`：T7现有`ak_ep1_t7_acceptance_input_v1`不满足builder固定路径要求的真实probability v1 manifest；builder raw1，未写锁，verify NOT_RUN。不迁移schema、不借测试夹具。作者允许离线源码独立接受，锁/研究包仍BLOCKED。

**K04对H7预检结果全文：NOT_RUN — 缺少正式选定输入与配置，两份记录selected_input均为null；未读取价格/ATR，未生成预检或研究结论。** H6/H7暂缓；H5两条旧冷链engineering FAIL保留，节点各23P/10setupE，不新开正式作业。H8 profile09沿原输入及72h超时继续，不重启、不等待其推进本批、不启动profile10。

保持`enable_trading=False / observer=hold / broker=NOT_RUN / send=0 / cancel=0`。不推候选分支、行情输入包或锁；不读改凭据、外审、规则、系统或服务。本批不新增决策请求；锁与恢复H7另行安排。
