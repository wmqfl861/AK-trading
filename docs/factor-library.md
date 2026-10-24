# 中国期货因子库：保存结果与项目使用方法

本次已将用户提供的《中国期货量化因子库_完整数据包_20260913.zip》登记为项目的**研究因子定义库**。原件、公式文本、来源、适用条件和包内状态均保留；已有14项可执行因子的计算口径与版本保持独立。

## 1. 已保存什么

版本ID：`china_futures_v1_20260913`，源版本1.0，源核验日期2026-09-13。

| 内容 | 核实数量 |
| --- | ---: |
| 因子定义 | 605 |
| 品种/合约研究规格 | 91：88项常规、3项月均价 |
| 因子家族 | 21 |
| 品种—因子条件关系 | 18,153 |
| 品种产业指标需求 | 316 |
| 来源入口 | 43 |
| 首批研究候选 | 30 |
| 历史原始观测 / 历史因子值 | 0 / 0 |

605项由249项通用或类别定义、316项品种产业特征、40项跨品种配方组成。它们包含收益预测、行情状态、风险与交易执行等不同用途；不是605个独立Alpha，也不是已经实现的605个计算器。

完整档案在 [`data/factor_library/china_futures_v1_20260913/`](../data/factor_library/china_futures_v1_20260913/)：

- `original/中国期货量化因子库_完整数据包_20260913.zip`：原ZIP的逐字节副本。
- `source/中国期货因子库_v1.0/`：20个原始文件，包含Excel、CSV、JSON、SQLite及原说明。此目录仅保留原件。
- `reports/`：本项目独立验收、接入计划与口径对照；不会修改原包自带的验收结论。
- [`config/factor_library.json`](../config/factor_library.json)：登记版本、路径、SHA-256及研究状态。
- [`src/ak_trading/factor_library.py`](../src/ak_trading/factor_library.py)：项目只读查询API。
- [`scripts/query_factor_library.py`](../scripts/query_factor_library.py)：统一命令行入口。

原ZIP SHA-256：`9a62a670064b89964577dc197065101c4d31b97d1def9fa188057d4bb2c9b43c`。

不把定义文本写入行情表；原SQLite的`raw_observations`和`factor_values`保持空表，未填造历史数据。目录检索不连接PostgreSQL或交易账户，也不执行包内Python脚本或公式字符串。

## 2. 如何查因子

在项目目录执行，输出为JSON，包含目录版本、研究状态及限量返回信息：

```powershell
python scripts/query_factor_library.py info
python scripts/query_factor_library.py verify
python scripts/query_factor_library.py products
python scripts/query_factor_library.py show MOM001
python scripts/query_factor_library.py search --product SHFE.CU --family 库存 --limit 20
python scripts/query_factor_library.py search --product CFFEX.T --family 国债 --limit 30
python scripts/query_factor_library.py search --product DCE.L_AVG --family 月均价
python scripts/query_factor_library.py starters --product DCE.M
```

`show`返回全部23个定义字段、品种适用条件和来源证据。`search`不指定品种时每个因子只返回一次；指定品种后保留该品种的条件。筛选条件是参数化查询，`family`和`keyword`按字面子串匹配，不把`%`当通配符。`limit`为1至1000，`truncated=true`表示仍有未返回定义；当前没有翻页游标，需增大limit或缩小筛选范围，`search --limit 1000`可一次获取全部605项。

Python调用：

```python
from ak_trading.factor_library import FactorLibrary

library = FactorLibrary()
record = library.factor("MOM001")
print(record["factor"]["formula"])
print(record["factor"]["available_at_rule"])

candidates = library.search(product="SHFE.CU", family="库存", limit=20)
for row in candidates["rows"]:
    print(row["factor_id"], row["name"], row["condition"])
```

SQLite以`mode=ro`、`query_only=ON`、`trusted_schema=OFF`打开；查询有5秒运行预算。每次打开核对SQLite注册哈希，`verify`还核对原ZIP、19项包内校验和、目录逻辑键和映射关系。读取或校验不会升级任何因子的实现/验证状态。

现有行情MCP仍提供原来的8项行情/参考查询工具。本次新增的是本地定义库入口，**尚未新增远程因子检索或计算工具**。远程agent可拿到本指南与原始包，在其服务器查询定义，再通过行情MCP获取必要价格；仅安装行情MCP不会自动读到本地文件。

## 3. 与现有14项因子怎样共存

现有执行注册表在 [`probability/factors.py`](../src/ak_trading/probability/factors.py)，仍是14项版本1计算器。本包ID使用独立的`MOM001`、`VOL004`等命名空间，不自动替换旧项。

| 新包定义 | 现有项 | 必须区分的口径 |
| --- | --- | --- |
| MOM010 路径效率比 | er20 | 新包分子保留方向；现有分子取绝对值。同窗口同输入时，取新值绝对值才对应旧ER；原0.3/0.5门槛不能直接用于带符号值。 |
| REV002／OP004 z-score | zscore20 | 新包统计窗口含当前观测，REV002另有反转负号；现有窗口排除当前，使用此前20根。 |
| VOL004／OP009 ATR相对价格 | atr14 | 新包用Wilder递推ATR并除以绝对价格，参数网格20/60/120；现有ATR14用TR算术平均，单位为价格点。初始化与热身需单独固定。 |
| VOL001 历史实现波动 | vol20 | 新包用对数收益并按√A年化；现有用简单收益，未年化。A不能临时随意选。 |
| VOI001 相对成交量 | volume_ratio20 | n=20且输入一致时，新值等于旧值减1；主要是口径映射，不是新增信息。 |
| VOI007 持仓变化率 | oi_change5 | 比率结构相同，包参数为20/60/120，旧项为5期；窗口不同不能混作同列。 |
| MOM004 均线相关定义 | ma20/ma60/ma_spread_atr | 新包涉及EMA和ATR(slow)，现有为算术均线和ATR14。 |
| MOM007／MOM008 通道与突破 | prior_high20/prior_low20/distance_high_atr/distance_low_atr | 保留排除当前的边界语义；通道位置不是两侧ATR距离，MOM008与现上界距离分子符号相反。 |

现`close`是输入价，不是独立预测因子。CUR001的ACT/365曲线公式与现期货结构模块相关，但近远月配对、可用时点、到期间隔与选月确认仍须遵守原模块约束。

新包还存在需明确的研究参数：REV001公式使用k但参数栏列n；MOM005名称为斜率而公式是斜率/标准误的t型特征；Wilder种子、年化A、fast/slow组合需要写入实现版本。本次保留原定义，并把这些记作实施前待决口径，不擅自改公式。

## 4. 建议怎样使用

### 第一批：10项量价研究基线

这些字段可由现有OHLCV/OI数据结构承接；这不等于已完成逐日期覆盖、发布时间或计算器验收。

| 候选 | 用途 | 重点 |
| --- | --- | --- |
| MOM001 累计合约收益动量 | 方向预测 | 同一实际合约的累计对数收益，不能跨换月价格相除。 |
| MOM003 波动调整动量 | 方向预测 | 与MOM001高度相关，比较增量贡献，不重复加权。 |
| MOM010 路径效率比 | 行情状态 | 保留方向与绝对趋势强度的区别。 |
| REV001 短期收益反转 | 方向预测 | 先固定k及观察期限，再做训练窗内选择。 |
| VOL001 历史实现波动 | 风险/状态 | 年化、收益类型、零波动规则先统一。 |
| VOL004 ATR相对价格 | 风险/仓位 | 适合比较不同价位的波动，严格区分Wilder与旧ATR14。 |
| VOL005 下行半方差 | 风险 | 平均负收益平方，不再开方；空头风险另看上涨尾部。 |
| VOI001 相对成交量 | 状态 | 可以对照旧volume_ratio20，避免误算成新增Alpha。 |
| VOI007 持仓变化率 | 状态 | 增仓不是净多资金流入，需结合价格与其他证据。 |
| VOI009 成交持仓比 | 活跃度/执行过滤 | 是活跃度代理，不能代替点差、深度或冲击成本。 |

原包首批列表有30项，本项目建议先以以上10项组成小基线，不一次把605项全投入模型。先选螺纹、铜、豆粕等已有历史合约样本，检查各自历史与数据质量；品种只是研究候选，不代表推荐交易。

### 第二批：补充曲线、排名和产业输入

- **CUR001近远月年化价差**：有同期两腿正价、可信到期日、因果选月后再接；股指、国债和航运不能机械解释成商品库存便利收益。
- **LIQ001相对买卖价差**：有有效同刻bid1/ask1历史后再接，不能从分钟OHLC重建盘口。
- **POS001会员净持仓比**：项目有排名数据模块，但需要统一披露成员/合约口径、发布时间和缺失日处理，不能用盘后数据预测同日收盘。
- **BAS/INV/供需成本家族、品种产业特征及XSP**：先建立可追溯的现货、库存、产能、成本和利润原始序列。包内316项产业指标是数据需求清单，不是已采购数据。
- 股指、国债、月均价、航运、期权链、逐单和另类数据按各自制度实施；普通快照不等于逐单事件，也不能从总持仓推断dealer净gamma。

### 研究验证顺序

1. **固定口径与数据键**：`factor_id + 参数版本 + 算子版本 + 合约/品种ID`，保存公式原文、单位、缺失策略、源哈希和代码哈希。
2. **按可用时间计算**：每项输入的`available_at <= decision_ts`；补采时间不是历史首见时间。低频序列按真实发布频率计算，前向携带时附age，缺失不补零。
3. **比较增量价值**：现有14项作基准，分别加入动量、反转、量仓、风险家族。按相关性归并，先检验IC/RankIC、衰减与稳定性，再检验含成本策略表现。
4. **时间外验证**：按时间滚动训练/校准/测试，重叠标签做purge/embargo；标准化、去极值和参数筛选只用训练信息。记录全部试验，防止605次试验中只挑赢家。
5. **接入模型与策略分工**：预测因子用于方向/幅度/期限任务；状态因子用于行情分层或策略资格；风险/执行因子约束仓位、成本与可交易性。现模型只使用品种/位置/状态桶，存入目录不会使605项自动进入训练。
6. **先影子验证**：稳定后生成带原因、时间和数据版本的候选决策，继续核对策略资格与成本，再评估是否具备执行条件。

## 5. 品种目录差异

新库91项与项目当前81项按交易所＋根代码正规化后，交集81，旧独有0。新增的研究目录项：

- 常规7项：`SHFE.OP`、`DCE.BB`、`DCE.BZ`、`DCE.FB`、`CZCE.PL`、`CZCE.RS`、`CZCE.ZC`。
- 月均价3项：`DCE.L_AVG`、`DCE.V_AVG`、`DCE.PP_AVG`。

这是目录差异，不是新上市、已下载、当前活跃或可交易认证。本次保留现有交易目录；月均价ID不能直接当实际行情符号或委托代码，实际月份、F后缀及观察日历要按合约主数据映射。

## 6. 本次验收与边界

- 原ZIP复制与20个解压文件逐字节一致，19项包内SHA-256通过。
- 独立检查12份CSV与JSON/SQLite逐行逐字段一致，逻辑键无重复或空值；来源、品种、因子和配方引用有效。
- Excel12张工作表，11张数据表与CSV一致；1,595个统计公式的引用/缓存结果经独立计算核对，缓存错误0；未重写或调用Excel引擎重算工作簿。
- SQLite完整性`ok`。目录表并没有全面声明主键、NOT NULL或外键；本次做了数据层引用核对。不能将该只读研究目录直接当生产因子值写入库。
- 原包34项实施/回测/上线门槛仍是待验证。本次通过的是保存、结构与查询验收，没有认证605条数学实现、历史覆盖或收益表现。

查询与旧因子回归：

```powershell
python -m pytest tests/test_factor_library.py tests/test_probability_factors.py -q
python scripts/query_factor_library.py verify
```
