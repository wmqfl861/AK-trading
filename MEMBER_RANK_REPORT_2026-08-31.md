# 每日多空持仓排名采集 — 交付报告（2026-08-31）

## 结论

原以为的"滑块验证码"障碍实际不存在：上期所/能源中心官网的 HTML 页面虽有防护，
但其**数据端点 `.dat` 可直接访问**，无需任何浏览器验证。六家交易所的持仓排名
端点已全部破解，其中五家（SHFE/INE/CFFEX/GFEX/CZCE）实现稳定采集并已完成
两个月历史回补入库；大商所端点与采集机制已验证，当日因瑞数 WAF 限流暂缓。

## 一、端点地图（全部官方来源，2026-08-31 验证）

| 交易所 | 端点 | 方式 | 粒度 | 覆盖品种 |
|---|---|---|---|---|
| SHFE | `/data/tradedata/future/dailydata/pm{YYYYMMDD}.dat` | 直连 GET | 品种合计 + 合约 + 总计/小计行 | 22 品种（含 INE 的 bc/lu/nr） |
| INE | 同上格式（www.ine.cn） | 直连 GET | 同上 | bc/lu/nr（sc/ec 无披露，见缺口） |
| CFFEX | `/sj/ccpm/{YYYYMM}/{DD}/{PRODUCT}.xml` | 直连 GET | 合约级 | 全部 8 品种 |
| GFEX | POST `loadListContract_id` + `loadList` (data_type 1/2/3) | 直连 POST | 合约级 | si/lc/ps（pt/pd 暂无披露） |
| CZCE | `/cn/DFSStaticFiles/Future/{YYYY}/{YYYYMMDD}/FutureDataHolding.htm` | 拟人浏览器 | 品种 + 合约 | 21 品种 |
| DCE | SPA `/frontend/dcereport/#/zh/memberDealPosiQuotes`（API `dailystat/memberDealPosi`） | 拟人浏览器驱动 UI | 品种合计 | 机制验证成功；当日被限流 |

## 二、字段语义（均经数值交叉验证）

- **SHFE/INE pm**：RANK 1-20 会员行（CJ1 成交量 / CJ2 持买 / CJ3 持卖，三榜并列）；
  RANK 999 = 合约/品种总计；-1 = 期货公司会员小计；0 = 非期货公司会员小计。
  榜单不足 20 名时尾部单元格留空（已按空处理）。
- **CFFEX**：datatypeid **0=成交量（双边计）、1=持买仓量、2=持卖仓量**
  （以 IF2609 官方日统计验证：type1/type2 合计占持仓 74%/83%，type1 若为成交量
  则超过 100% 而排除）。
- **GFEX**：data_type 1/2/3 = 成交量/持买/持卖；末行 `总计` 为合计；成交量低于
  披露阈值的合约官方直接不返回数据。
- **CZCE**：品种块（`品种：苹果AP`）+ 合约块（`合约：AP610`）；每行三段
  （交易量/持买仓量/持卖仓量各 会员+量+增减）；显示名 PTA 映射产品代码 TA。
- **DCE**：单响应含 qtyFutureList/buyFutureList/sellFutureList 三榜（各 20+合计）。

## 三、交付物

| 组件 | 说明 |
|---|---|
| `src/ak_trading/member_rank.py` | 六交易所采集器 + 解析器（未知值绝不填零，非交易日返回 unavailable） |
| `src/ak_trading/persistence/member_rank.py` | 幂等 upsert 导入器 + `top20_net_by_product` 净持仓因子查询 |
| `migrations/versions/20260831_0003_member_position_ranks.py` | 纯增量建表（已应用；未触碰待授权的 20260807_0002 规则表链） |
| `scripts/collect_member_ranks.py` | CLI：`--start/--end/--exchange/--dry-run`，证据先行（原始载荷存 `data/member_ranks/`） |
| `tests/test_member_rank.py` | 24 项测试（解析器/抓取器 mock/导入幂等）全部通过；全套 383 通过 |

## 四、入库成果（member_position_ranks）

- **616,636 行，43 个交易日（2026-07-01 → 2026-08-28），57 个品种-交易所组合**
- CFFEX 56,220 行（8 品种）· CZCE 315,137 行（21 品种）· GFEX 27,216 行（3 品种）
  · INE 13,348 行（3 品种）· SHFE 204,715 行（22 品种）
- 抽查验证：CZCE 苹果 AP 品种第 1 名成交量 56,306 与页面逐字一致；
  CFFEX IF 前 20 持买/持卖合计 188,388/215,267 与源文件逐位一致；
  GFEX si2610 前 20 成交量合计 28,510 与官方总计一致。
- 净持仓因子示例（2026-08-28，前 20 会员 多头合计−空头合计）：
  CZCE/MA +114,695（偏多）、CZCE/FG −265,969（偏空）、CFFEX/IF −26,879、SHFE/cu −21,788。

## 五、已知缺口与后续

1. **INE sc（原油）/ ec（欧线）排名无 pm 披露**（shfe/ine 双侧确认）——
   官网页面数据即来自 pm 文件，页面同样不显示；后续查"数据下载"月度归档是否补充。
2. **DCE 当日被瑞数 WAF 限流**（重载+输日期机制已验证、曾成功取回 b/lh 两品种
   完整数据）；建议数小时后用全新 profile 单日单次重试，避免高频触发。
3. **GFEX pt/pd（铂/钯）暂无披露**（新品种，成交量未达披露阈值，官方即不发布）。
4. **bc/lu/nr 双源重复披露**（SHFE 与 INE 的 pm 均含）：消费侧应固定取单一来源
   （建议 INE 行），避免重复计入。
5. **CZCE 出现目录外新品种**：丙烯 PL、菜籽 RS——`china_futures_catalog.json`
   需要刷新（独立任务）。
6. **无成交老品种无披露**（CZCE JR/LR/PM/RI/WH、SHFE wr 等）属数据现实，非故障。

## 六、运行方式

```powershell
python scripts\collect_member_ranks.py                      # 最近三天全部交易所
python scripts\collect_member_ranks.py --start 2026-09-01 --end 2026-09-30
python scripts\collect_member_ranks.py --exchange DCE --start 2026-08-28 --end 2026-08-28
```

每日收市结算后数据生成（约 15:35-17:00 各所陆续就绪）；周末/节假日返回
unavailable 属正常。

## 七、每日自动化（已注册，2026-08-31 验证通过）

| 任务 | 时间（Asia/Shanghai） | 内容 | 状态 |
|---|---|---|---|
| AK-trading Member Rank Collection | 每天 18:15 | 五所 `--lookback 5`（5 天自愈窗口，幂等 upsert） | 手动触发验证 exit 0，当日数据已入库 |
| AK-trading Member Rank DCE | 每天 21:30 | DCE 尽力采集（瑞数限流敏感，失败自动跳过次日重试） | 已注册并启动验证 |

实现：`scripts/run_member_rank_daily.ps1`（带日志轮转 90 天）+
`scripts/install_member_rank_task.ps1`（S4U 主体免密运行；重装加 `-Apply`，
卸载 `Unregister-ScheduledTask`）。日志在 `data/member_ranks/task_logs/`。

注册过程中修复的三个 Windows 计划任务坑（已写入脚本注释）：默认主体无交互
会话拒绝启动（改 S4U）；S4U 环境无用户 PATH（安装时固化 python 绝对路径）；
`powershell -File` 下重复数组参数绑定失败/位置参数错绑（改单字符串空格分隔）。

## 八、历史深度与深回补（已完成）

官方端点历史可用性（HEAD 探测验证）：**SHFE 2015 起、CFFEX 2010 起（沪深300
上市日）、INE 2018 起（原油上市）、GFEX 2022-12 起**。深回补于 2026-09-01 完成，
与 K 线库起点对齐：

**最终入库：8,459,247 行 · 645 个交易日（2024-01-02 → 2026-08-31）· 零缺口**
（CZCE 覆盖了窗口内每一个 SHFE 交易日，EXCEPT 校验 = 0）

| 交易所 | 交易日 | 品种数 | 行数 |
|---|---:|---:|---:|
| SHFE | 645 | 23 | 3,048,017 |
| CZCE | 645 | 22 | 3,880,731 |
| CFFEX | 645 | 8 | 832,812 |
| GFEX | 639 | 5 | 394,065 |
| INE | 645 | 4 | 303,601 |
| DCE | 1（尽力任务） | 1 | 21 |

GFEX/INE 的品种数大于当前活跃集是因为历史窗口含今已摘牌/停牌品种及
pt/pd、bc、ec 等阶段性披露。若需更早（2015/2010 起）可用同一命令扩窗回补，
代价是 CZCE 浏览器逐日约 20-25 秒。
