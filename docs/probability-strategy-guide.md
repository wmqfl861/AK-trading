# 概率驱动策略研究使用说明

这是一条离线、可复现的研究流程：因子描述当前行情，模型估计未来路径概率，策略把路径转为净损益，再比较最高净EV选择、概率抽样、固定策略和现金。不连接交易账户，也不承诺提升胜率。

实施需求与口径见 [第一版计划](probability-strategy-plan.md)。

## 快速运行

```powershell
cd D:\Projects\AK-trading
python scripts/research_probability_strategies.py --input-bundle data/strategy_artifacts/futures-lib-20260907-codex-01/real-input-02 --config config/probability_rb_example.json --output data/research_runs/probability-rb-first
```

输出目录必须不存在。示例固定选择 RB2101、RB2105、RB2110、RB2201 四个合约，费用、乘数、滑点和保证金均为显式情景假设。当前一份配置只接受同品种多个合约，不能拿螺纹钢乘数去计算其他品种的人民币收益。

数据来源也可以是 [历史行情手册](data-and-backtesting.md) 中导出的新 `futures_bundle_v1`。选择合约至少有120根观察、最多12合约、单合约最多10000根、总计最多30000根；输入保留独立实例身份和原始时间，不拼接主连。

## 因子与事件

`factor_registry.json` 记录定义、窗口、单位与版本。首批包括收盘价、过去20根高低点、ATR14、ER20、波动率、MA20/MA60、均线差/ATR、z-score20、距前高/前低的ATR距离、成交量比例和5根持仓变化。

前高前低与z-score参考窗排除当前bar；均线可包含当前已完成收盘。ATR和所有窗口只看当前前缀。无效行和预热行被保留并说明状态，不删除后压缩时间。量仓缺失只影响对应可选因子。

事件位置标为 `near_high`、`near_low` 或 `middle`；当前 `uptrend/downtrend/range/transition` 由ER和均线定义，是描述行情的启发式。预测的未来状态则由未来期限价格相对决策时ATR的位移定义，两者不能混为一谈。

## 不同期限与幅度的概率

示例期限为1/5/20根，障碍距离为0.5/1/2倍决策时ATR，共9个场景。下一根开盘是参考起点，每个场景输出：

- `up_first`：先触及上障碍。
- `down_first`：先触及下障碍。
- `neither`：期限内均未触及。
- `ambiguous`：同根高低跨过两障碍、开盘也无法确定先后。

开盘若已越过一个障碍，该开盘事件先于同根高低，因此可确定首达方向。价格缺失、时间/身份错误、尾部未成熟独立标记，不归入neither。

标签还保存首次触达步数、上下障碍价格、终点涨跌点数与收益率、MFE/MAE，并在 `labels.csv` 给出基于决策时ATR的 `terminal_atr/mfe_atr/mae_atr`。预测诊断中保留训练桶的触达时间分布和幅度分位数；这是历史条件描述统计，不是经验证的预测区间。

## 训练、校准和评估

完整输入的全局原始时间轴按60%训练、20%校准、20%测试切分。事件最长标签/策略窗口跨界就清除，不用随机拆行。合约同时刻共用同一分区。窗口按存储观察根数计，不推断历史特殊交易日。

概率基线采用 `品种+事件位置+当前状态` 条件频率，样本不足时回退品种或全场景频率，使用平滑并报告回退层级和数量。在独立校准区间从固定候选温度选择参数，训练计数不使用校准集重拟合。最终测试才计算多类Brier、log loss和可靠性统计，并与仅用训练全局频率的基线比较。

`calibrated` 表示执行了校准拟合，不代表测试集证明校准完美。正常公开预测要求晚于训练及校准标签的最晚可用终点。无校准数据或训练不足时会降级，默认路由不使用未校准模型交易。

完整输入区间和所选合约本身是回溯研究设定；阶段内标签仍可能重叠、有相关性，误差估计不能假设独立。已有数据在前期研究中被查看过，本次测试不是全新未见的投资留出；发布后标记 consumed。相同输入重放是复算，不会产生新的样本外证据。

## 策略和参数

每个事件从独立固定资金、空仓开始，窗口结束平仓。费用采用 `每手费用 + 名义金额费率` 相加，所有成交额外记滑点；使用固定手数和最大保证金比例。真实官方历史规则、涨跌停容量与成交队列未认证。

| 策略 | 第一版规则 |
| --- | --- |
| 突破 | 当前收盘突破过去高/低点且均线同向，下一开盘进场，冻结ATR止盈止损 |
| 价格回归 | 当前range/transition且z-score越过±2，下一开盘逆向入场 |
| 回踩 | 当前趋势方向已知，冻结回踩水平；未来已完成收盘穿越后下一开盘进场 |
| 网格 | 冻结中心和ATR间距，收盘确认偏离后下一开盘调库存；最多2层、总止损3ATR，不能加倍摊平 |
| 现金 | 不交易，净损益为0 |

止盈止损同根无法判断顺序时策略收益用预先约定的止损优先，并记录歧义次数；跳空损失按开盘先处理。网格属于日线收盘确认近似，不是对盘中挂单网格的成交承诺。

当前策略参数由 `ActionSpec` 固定并输出，不对最终测试择优。配置支持期限、路径障碍和选择门槛；更丰富参数搜索应仅在训练/开发阶段进行，不能观察测试后调参再宣称样本外。

## 条件净EV与按概率选择

用训练样本中每种策略在四类路径结果下的完整净损益分布，结合当前预测的四类路径概率，计算条件平均净收益、净盈利概率、标准误惩罚后的保守EV、低端损益分位数。没有交易的有效事件保留0；无效或未成熟事件不算0收益。币值证据只能在同品种内汇总。

某个结果有正概率而训练中没有对应策略收益支持时，输出 `insufficient_evidence` 和缺失概率质量，不重归一化或假设缺项为0。标准误扣减是相关观察下的启发式风险惩罚，不是置信区间。

决策方法：

- `best_ev`：在校准、样本量、净盈利概率、正保守EV和歧义上限都通过时选最高保守EV；否则现金。
- `probability_sample`：在相同条件下按超额保守EV与净盈利概率生成权重，同时保留现金概率；用 seed+事件ID 的稳定哈希抽样并记录抽样值。
- `fixed:<strategy>`：同一批机会固定采用一种策略，不经过概率/EV门槛，作为基准。
- `cash`：不交易基准。

抽样权重是透明的决策启发式，不能解释成“某策略真实最优的概率”。用户可以检验抽样是否优于最高EV，而不是预先认定随机化有利。默认门槛包括最少30个样本、净盈利概率至少0.5、保守EV大于0、歧义概率不超过0.25；它们是预先固定的示例研究参数，不是最优参数结论。

比较按每合约固定不重叠事件窗口，同一事件集合用于所有方法。统计按合约报告，跨合约不相加成人民币组合权益；累计事件损益不是复利账户NAV。缺少选中结果会标记incomplete，不丢掉亏损或坏样本。

## 输出文件

- `config.json` / `factor_registry.json` / `strategy_specs.json`：参数与版本。
- `sample.json` / `splits.json`：明确合约、时间窗口、清除与消耗状态。
- `factors.csv` / `labels.csv`：逐事件因子和全部场景标签。
- `models.json`：训练计数、校准参数、时间界限与模型身份。
- `predictions.csv`：路径/未来状态概率、回退及时间幅度诊断。
- `action_outcomes.csv`：事件级完整策略损益和成交明细。
- `payoff_estimates.csv` / `selections.csv`：条件净EV、证据门槛、权重及抽样记录。
- `calibration.json` / `report.json`：校准指标、基线比较和按合约策略结果。
- `manifest.json`：最后发布的完整标记，绑定输入、配置、源码和全部输出哈希。

## 数据更新新鲜度

在指定参考日期后检查是否已有数据：

```powershell
python scripts/check_data_freshness.py --expected-through 2026-09-07 --symbol SHFE.rb2610 --duration 86400 --exchange DCE
python scripts/check_data_freshness.py --expected-through 2026-09-07 --symbol KQ.m@SHFE.rb --duration 60 --exchange SHFE
```

必须显式选择期望日期，程序不猜节假日。退出0=全部fresh；1=stale/empty；2=查询或请求错误。只检查指定符号/周期或交易所的最新一行，不能替代全市场完整性认证。它读取数据库但不重启定时任务、不下载或写行情。

## 测试

```powershell
python scripts/test_offline.py -q tests/test_probability_factors.py tests/test_probability_labels.py tests/test_probability_models.py tests/test_probability_episodes.py tests/test_probability_routing.py tests/test_probability_pipeline.py
python scripts/test_offline.py -q
```

最终实验与独立审核记录保存在 `data/research_artifacts/probability-v1-20260908/`。遇到输入/运行错误返回2；成功发布但对照不完整返回3；完整研究返回0。所有目录需新建，不覆盖旧研究。
