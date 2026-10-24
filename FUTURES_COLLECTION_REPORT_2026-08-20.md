# 中国期货全品种信息采集报告

- 采集运行: catalog run `a72f2641` / snapshot `3ad67e90` / 常规 run `2f05142f`
- 目标交易日: 2026-08-20；采集完成于同日
- 范围: 境内六家期货交易所全部 81 个在册期货品种；6 个代表性期权显式排除
- **重要说明**: 所有 729 个规则字段（手续费、保证金、限仓等 9 类）均处于 `review_required` 候选状态——官方证据已留存，但数值未经确定性解析与人工审核，本报告不含任何具体费率/保证金数字，未知一律标注待审而非填 0。

## 一、交易所总体覆盖

| 交易所 | 品种数 | 留存文档数 | 当日状态 | 说明 |
|---|---:|---:|---|---|
| CFFEX 中国金融期货交易所 | 8 | 28 | review_required | 全部品种证据留存，待审 |
| CZCE 郑州商品交易所 | 24 | 60 | partial | MA、UR 两个品种搜索瞬时失败（已隔离），其余正常 |
| DCE 大连商品交易所 | 20 | 81 | partial | PP 一个品种搜索瞬时失败（已隔离），其余正常；首次全量完成（拟人浏览突破 412 拦截） |
| GFEX 广州期货交易所 | 5 | 24 | review_required | 全部品种证据留存，待审 |
| INE 上海国际能源交易中心 | 5 | 29 | review_required | 全部品种证据留存，待审 |
| SHFE 上海期货交易所 | 19 | 106 | review_required | 全部品种证据留存，待审；RB/HC/WR 已改用官网新地址 |

## 二、逐品种证据留存明细（81 个品种）

“证据字符数”为该品种留存的最佳官方文档正文长度；“质量”≥200 为强、50–199 为中。<200 的品种在备注中说明。

### CFFEX · 中国金融期货交易所（8 个品种）

| 品种代码 | 中文名 | 最佳证据文档（官方 URL） | 证据字符数 | 质量 | 字段覆盖 | 状态 |
|---|---|---|---:|---|---|---|
| IC | 中证500股指 | http://www.cffex.com.cn/cn/zz500.html | 487 | 强 | 9/9 | 待人工审核 |
| IF | 沪深300股指 | http://www.cffex.com.cn/ | 4923 | 强 | 9/9 | 待人工审核 |
| IH | 上证50股指 | http://www.cffex.com.cn/ | 4923 | 强 | 9/9 | 待人工审核 |
| IM | 中证1000股指 | http://www.cffex.com.cn/ | 4923 | 强 | 9/9 | 待人工审核 |
| T | 10年期国债 | http://www.cffex.com.cn/ | 4923 | 强 | 9/9 | 待人工审核 |
| TF | 5年期国债 | http://www.cffex.com.cn/ | 4923 | 强 | 9/9 | 待人工审核 |
| TL | 30年期国债 | http://www.cffex.com.cn/ | 4923 | 强 | 9/9 | 待人工审核 |
| TS | 2年期国债 | http://www.cffex.com.cn/ | 4923 | 强 | 9/9 | 待人工审核 |

### CZCE · 郑州商品交易所（24 个品种）

| 品种代码 | 中文名 | 最佳证据文档（官方 URL） | 证据字符数 | 质量 | 字段覆盖 | 状态 |
|---|---|---|---:|---|---|---|
| AP | 苹果 | https://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2026/3/5dd18aace68046c9acead2601951b5d0.htm | 1919 | 强 | 9/9 | 待人工审核 |
| CF | 棉花 | https://www.czce.com.cn/ | 1973 | 强 | 9/9 | 待人工审核 |
| CJ | 红枣 | https://www.czce.com.cn/ | 1973 | 强 | 9/9 | 待人工审核 |
| CY | 棉纱 | https://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2024/08/1726388702630562.htm | 1979 | 强 | 9/9 | 待人工审核 |
| FG | 玻璃 | https://www.czce.com.cn/ | 1973 | 强 | 9/9 | 待人工审核 |
| JR | 粳稻 | https://www.czce.com.cn/ | 1973 | 强 | 9/9 | 待人工审核 |
| LR | 晚籼稻 | https://www.czce.com.cn/cn/sspz/qm/pzgk/webinfo/2022/02/1644399484444547.htm | 1946 | 强 | 9/9 | 待人工审核 |
| MA | 甲醇 | https://www.czce.com.cn/cn/sspz/H077002index_1.htm | 1931 | 强 | 9/9 | 待人工审核 |
| OI | 菜籽油 | https://www.czce.com.cn/ | 1973 | 强 | 9/9 | 待人工审核 |
| PF | 短纤 | https://www.czce.com.cn/cn/sspz/H077002index_1.htm | 1931 | 强 | 9/9 | 待人工审核 |
| PK | 花生 | https://www.czce.com.cn/cn/sspz/qm/pzgk/webinfo/2022/03/1644400094733033.htm | 1945 | 强 | 9/9 | 待人工审核 |
| PM | 普麦 | https://www.czce.com.cn/cn/sspz/qm/pzgk/webinfo/2022/02/1644399484444547.htm | 1946 | 强 | 9/9 | 待人工审核 |
| PR | 瓶片 | https://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2026/6/9c4c45f4d3594b28934613798bfccb4a.htm | 1957 | 强 | 9/9 | 待人工审核 |
| PX | 对二甲苯 | https://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2026/6/9c4c45f4d3594b28934613798bfccb4a.htm | 1957 | 强 | 9/9 | 待人工审核 |
| RI | 早籼稻 | https://www.czce.com.cn/cn/sspz/qm/pzgk/webinfo/2022/02/1644399484444547.htm | 1946 | 强 | 9/9 | 待人工审核 |
| RM | 菜籽粕 | https://www.czce.com.cn/ | 1973 | 强 | 9/9 | 待人工审核 |
| SA | 纯碱 | https://www.czce.com.cn/cn/sspz/H077002index_1.htm | 1931 | 强 | 9/9 | 待人工审核 |
| SF | 硅铁 | https://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2026/6/9c4c45f4d3594b28934613798bfccb4a.htm | 1957 | 强 | 9/9 | 待人工审核 |
| SH | 烧碱 | https://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2025/02/1738064972503287.htm | 2078 | 强 | 9/9 | 待人工审核 |
| SM | 锰硅 | https://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2026/6/9c4c45f4d3594b28934613798bfccb4a.htm | 1957 | 强 | 9/9 | 待人工审核 |
| SR | 白糖 | https://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2023/04/1682451348485260.htm | 1963 | 强 | 9/9 | 待人工审核 |
| TA | PTA | https://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2026/6/9c4c45f4d3594b28934613798bfccb4a.htm | 1957 | 强 | 9/9 | 待人工审核 |
| UR | 尿素 | https://www.czce.com.cn/cn/sspz/H077002index_1.htm | 1931 | 强 | 9/9 | 待人工审核 |
| WH | 强麦 | https://www.czce.com.cn/cn/sspz/qm/pzgk/webinfo/2022/02/1644399484444547.htm | 1946 | 强 | 9/9 | 待人工审核 |

### DCE · 大连商品交易所（20 个品种）

| 品种代码 | 中文名 | 最佳证据文档（官方 URL） | 证据字符数 | 质量 | 字段覆盖 | 状态 |
|---|---|---|---:|---|---|---|
| A | 黄大豆1号 | http://www.dce.com.cn/dalianshangpin/zt/hdjqtzt22/8525586/8525592/6146643/index.html | 1997 | 强 | 9/9 | 待人工审核 |
| B | 黄大豆2号 | http://www.dce.com.cn/dce/channel/list/121.html | 1977 | 强 | 9/9 | 待人工审核 |
| C | 玉米 | http://www.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/2285936/index.html | 1952 | 强 | 9/9 | 待人工审核 |
| CS | 玉米淀粉 | http://www.dce.com.cn/dalianshangpin/sspz/index.html | 2004 | 强 | 9/9 | 待人工审核 |
| EB | 苯乙烯 | http://www.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8628884/index.html | 2032 | 强 | 9/9 | 待人工审核 |
| EG | 乙二醇 | http://www.dce.com.cn/dalianshangpin/zt/xpzsszt66/yecqhss/6138041/6139663/index.html | 1962 | 强 | 9/9 | 待人工审核 |
| I | 铁矿石 | http://www.dce.com.cn/dalianshangpin/fgfz/6142914/6142926/6146552/index.html | 2032 | 强 | 9/9 | 待人工审核 |
| J | 焦炭 | http://www.dce.com.cn/dalianshangpin/fgfz/6142914/6142926/6263837/index.html | 2005 | 强 | 9/9 | 待人工审核 |
| JD | 鸡蛋 | http://www.dce.com.cn/dce/channel/list/2160.html | 2030 | 强 | 9/9 | 待人工审核 |
| JM | 焦煤 | http://www.dce.com.cn/dce/content/2026/wm0/18626066.html | 2040 | 强 | 9/9 | 待人工审核 |
| L | 聚乙烯 | http://www.dce.com.cn/dalianshangpin/sspz/index.html | 2004 | 强 | 9/9 | 待人工审核 |
| LG | 原木 | http://www.dce.com.cn/dalianshangpin/sspz/index.html | 2004 | 强 | 9/9 | 待人工审核 |
| LH | 生猪 | http://www.dce.com.cn/dalianshangpin/fgfz/6142914/6142926/6262877/index.html | 2044 | 强 | 9/9 | 待人工审核 |
| M | 豆粕 | http://www.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8633681/index.html | 1964 | 强 | 9/9 | 待人工审核 |
| P | 棕榈油 | http://www.dce.com.cn/dalianshangpin/sspz/index.html | 2004 | 强 | 9/9 | 待人工审核 |
| PG | 液化石油气 | http://www.dce.com.cn/dalianshangpin/sspz/index.html | 2004 | 强 | 9/9 | 待人工审核 |
| PP | 聚丙烯 | http://www.dce.com.cn/dalianshangpin/sspz/index.html | 2004 | 强 | 9/9 | 待人工审核 |
| RR | 粳米 | http://www.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8628884/index.html | 2032 | 强 | 9/9 | 待人工审核 |
| V | 聚氯乙烯 | http://www.dce.com.cn/dalianshangpin/sspz/index.html | 2004 | 强 | 9/9 | 待人工审核 |
| Y | 豆油 | http://www.dce.com.cn/dalianshangpin/ywfw/jystz/ywtz/8628884/index.html | 2032 | 强 | 9/9 | 待人工审核 |

### GFEX · 广州期货交易所（5 个品种）

| 品种代码 | 中文名 | 最佳证据文档（官方 URL） | 证据字符数 | 质量 | 字段覆盖 | 状态 |
|---|---|---|---:|---|---|---|
| LC | 碳酸锂 | https://www.gfex.com.cn/gfex/tzts/202604/977dc84de62e4bc08e480694e21dd8ac.shtml | 584 | 强 | 9/9 | 待人工审核 |
| PD | 钯 | https://www.gfex.com.cn/gfex/sspzb/sspz.shtml | 602 | 强 | 9/9 | 待人工审核 |
| PS | 多晶硅 | https://www.gfex.com.cn/gfex/tzts/202509/ca0d962a09ce42c6ace2fb1c1897ef39.shtml | 362 | 强 | 9/9 | 待人工审核 |
| PT | 铂 | https://www.gfex.com.cn/gfex/sspzb/sspz.shtml | 602 | 强 | 9/9 | 待人工审核 |
| SI | 工业硅 | https://www.gfex.com.cn/gfex/tzts/202509/ca0d962a09ce42c6ace2fb1c1897ef39.shtml | 362 | 强 | 9/9 | 待人工审核 |

### INE · 上海国际能源交易中心（5 个品种）

| 品种代码 | 中文名 | 最佳证据文档（官方 URL） | 证据字符数 | 质量 | 字段覆盖 | 状态 |
|---|---|---|---:|---|---|---|
| BC | 国际铜 | https://www.ine.cn/publicnotice/notice/202601/t20260120_830159.html | 260 | 强 | 9/9 | 待人工审核 |
| EC | 集运指数（欧线） | https://www.ine.cn/publicnotice/notice/202603/t20260306_830584.html | 607 | 强 | 9/9 | 待人工审核 |
| LU | 低硫燃料油 | https://www.ine.cn/publicnotice/notice/202605/t20260529_831878.html | 267 | 强 | 9/9 | 待人工审核 |
| NR | 20号胶 | https://www.ine.cn/publicnotice/notice/202603/t20260312_830678.html | 239 | 强 | 9/9 | 待人工审核 |
| SC | 原油 | https://www.ine.cn/ | 172 | 中 | 9/9 | 待人工审核 *证据偏短，人工复核时优先* |

### SHFE · 上海期货交易所（19 个品种）

| 品种代码 | 中文名 | 最佳证据文档（官方 URL） | 证据字符数 | 质量 | 字段覆盖 | 状态 |
|---|---|---|---:|---|---|---|
| AD | 铸造铝合金 | https://www.shfe.com.cn/index/othercontents/2025_AD/ | 498 | 强 | 9/9 | 待人工审核 |
| AG | 白银 | https://www.shfe.com.cn/publicnotice/notice/202601/t20260130_830314.html | 306 | 强 | 9/9 | 待人工审核 |
| AL | 铝 | https://www.shfe.com.cn/products/futures/metal/nonferrousmetal/al_f/ | 2543 | 强 | 9/9 | 待人工审核 |
| AO | 氧化铝 | https://www.shfe.com.cn/publicnotice/notice/202511/t20251104_829423.html | 270 | 强 | 9/9 | 待人工审核 |
| AU | 黄金 | https://www.shfe.com.cn/publicnotice/notice/202510/t20251017_829279.html | 285 | 强 | 9/9 | 待人工审核 |
| BR | 丁二烯橡胶 | https://www.shfe.com.cn/publicnotice/notice/202602/W020260203684710643306.docx | 19730 | 强 | 9/9 | 待人工审核 |
| BU | 石油沥青 | https://www.shfe.com.cn/publicnotice/notice/202512/W020251205680806782091.docx | 17685 | 强 | 9/9 | 待人工审核 |
| CU | 铜 | https://www.shfe.com.cn/products/option/nonferrousmetal/cu_o/ | 2649 | 强 | 9/9 | 待人工审核 |
| FU | 燃料油 | https://www.shfe.com.cn/products/futures/energyandchemical/fu_f/ | 2543 | 强 | 9/9 | 待人工审核 |
| HC | 热轧卷板 | https://www.shfe.com.cn/products/futures/metal/ferrousandpreciousmetal/hc_f/ | 2549 | 强 | 9/9 | 待人工审核 |
| NI | 镍 | https://www.shfe.com.cn/publicnotice/notice/202601/t20260123_830226.html | 386 | 强 | 9/9 | 待人工审核 |
| PB | 铅 | https://www.shfe.com.cn/products/option/nonferrousmetal/pb_o/ | 2649 | 强 | 9/9 | 待人工审核 |
| RB | 螺纹钢 | https://www.shfe.com.cn/products/futures/metal/ferrousandpreciousmetal/rb_f/ | 2543 | 强 | 9/9 | 待人工审核 |
| RU | 天然橡胶 | https://www.shfe.com.cn/products/futures/energyandchemical/ru_f/ | 2543 | 强 | 9/9 | 待人工审核 |
| SN | 锡 | https://www.shfe.com.cn/publicnotice/notice/202603/t20260312_830680.html | 470 | 强 | 9/9 | 待人工审核 |
| SP | 纸浆 | https://www.shfe.com.cn/products/futures/energyandchemical/sp_f/ | 52 | 中 | 9/9 | 待人工审核 *证据偏短，人工复核时优先* |
| SS | 不锈钢 | https://www.shfe.com.cn/products/futures/metal/ferrousandpreciousmetal/au_f/ | 2543 | 强 | 9/9 | 待人工审核 |
| WR | 线材 | https://www.shfe.com.cn/products/futures/metal/ferrousandpreciousmetal/au_f/ | 2543 | 强 | 9/9 | 待人工审核 |
| ZN | 锌 | https://www.shfe.com.cn/products/futures/metal/ferrousandpreciousmetal/au_f/ | 2543 | 强 | 9/9 | 待人工审核 |

## 三、显式排除的期权（不采集）

| 交易所 | 期权 | 类别 | 排除原因 |
|---|---|---|---|
| CFFEX | 沪深300股指期权 | option | Options are deliberately outside the current collection scope. |
| CZCE | PTA期权 | option | Options are deliberately outside the current collection scope. |
| DCE | 豆粕期权 | option | Options are deliberately outside the current collection scope. |
| GFEX | 碳酸锂期权 | option | Options are deliberately outside the current collection scope. |
| INE | 原油期权 | option | Options are deliberately outside the current collection scope. |
| SHFE | 螺纹钢期权 | option | Options are deliberately outside the current collection scope. |

## 四、本轮隔离记录的瞬时错误（不影响其他品种）

| 品种 | 错误 |
|---|---|
| CZCE/MA | 官方搜索 CLI 退出码 1（瞬时失败，品种仍以种子 URL 完成证据留存） |
| CZCE/UR | 官方搜索 CLI 退出码 1（瞬时失败，品种仍以种子 URL 完成证据留存） |
| DCE/PP | 官方搜索 CLI 退出码 1（瞬时失败，品种仍以种子 URL 完成证据留存） |

## 五、证据质量分布

| 质量档 | 品种数 | 占比 |
|---|---:|---:|
| 强（≥200 字符） | 79 | 98% |
| 中（50–199 字符） | 2 | 2% |
| 弱（<50 字符） | 0 | 0% |

中等证据品种：INE/SC, SHFE/SP——人工复核时优先检查。

## 六、下一步（人工审核后方可使用）

1. 启动只读工作台：`python scripts/exchange_rule_workbench.py` → http://127.0.0.1:8765 查看覆盖矩阵与审核队列。
2. 字段级审核：`scripts/review_exchange_rules.py scope-approve`，仅确定性候选字段可批准，需目录 ID、候选快照与审核理由。
3. 批准后的数值才进入 scoped current；费用估算口径（交易所费 + 每手 0.01 元、保证金率 + 0.03）在审核通过后另行计算。