# 数据补齐核查与修复记录(2026-09-12,周六)

目标:核实各类数据是否更新到 2026-09-11(周五,最近完成交易日),缺则补齐。

## 结论一览

| 数据类别 | 覆盖 | 证据 |
|---|---|---|
| 合约日线(780 符号) | ✅ 到 09-11 | 任务日志 776 符号 latest=09-11;DB 跨 6 所抽样 20/20 通过 |
| 合约分钟线(780 符号) | ✅ 到 09-11 | 日志 ok160+stale616(全量 09-11);DB 抽样 30/30 通过 |
| KQ 指数日线(81 符号) | ✅ 到 09-11 | KQI_SUMMARY done=81 failed=0 |
| 主连分钟线(KQ.m) | ✅ 到 09-11(含周五夜盘) | 17:30 任务成功;rb 1380 根/cu 1920 根完整 |
| 期权日线(逐档,~7000 符号) | ✅ 到 09-11 | 18:30 任务 6423 ok + 两轮 retry;30/30 抽样通过;余 25 个 AP 远月深档从未有过成交(库里零历史,属死档非缺口) |
| 会员排名 CFFEX/CZCE/GFEX/INE/SHFE | ✅ 到 09-11 | 15280 行(INE sc/ec 不披露,已知) |
| 会员排名 DCE | ❌ 09-11 缺 | 源端故障(见下);最新到 09-10 |
| 结算参数快照 | ✅ 到 20260911 | data/settlement_params/ 每日目录 |

## 修复 1:EDB 任务周末/假日新鲜度误报

**现象**:`run_edb_update_daily.ps1`(每天 18:00)把 `--end`(日历日)当作新鲜度基准。
周六/假日运行时基准=非交易日,EDB 最新数据只到上一交易日,全部符号被误判
`stale`,两个合约阶段 exit 1。数据本身已写入,纯误报——但每个非交易日任务都
显示"failed",会掩盖真故障。2026-09-12 运行:contract_daily stale=776、
contract_minute stale=616,即此误报。

**修复**:
- 新增 `scripts/compute_last_session.py`:用 config/china_futures_catalog.json 的
  交易日历计算"最后一个已收盘交易日"(15:00 前不含当天;回退到上一交易日;
  跳过周末与休市日)。失败时退出非零,由包装器回退旧行为。
- `run_edb_update_daily.ps1` 现在把 `--expected-through <最后已收盘交易日>`
  传给两个 EDB 阶段;长假期中 lookback 起点若晚于该日,自动回拉 start 以满足
  `start <= expected-through <= end` 约束。
- 回归测试 `tests/test_compute_last_session.py`(7 例:周末/收盘后/早盘/国庆/
  15:00 边界/无时区/目录缺失)。

## 已确认问题 2:DCE 排名源端故障(非本项目问题)

**现象**:09-11 21:30 起,DCE 排名采集对整个回看窗口(含历史已成功的日期)
全部返回 "dce disclosure empty"。09-10 21:30 同一套代码还全量成功。

**根因(2026-09-12 用 Scrapling 探针实证)**:DCE 披露后端
`/dcereport/publicweb/maxTradeDate` 返回 `{"tradeDate":"20260914"}` ——
下周一(未来日期)。SPA 默认查询 20260914 → 无数据 → 页面"暂无数据",
memberDealPosi 对历史日期也返回空列表(`qtyFutureList:null`)。
即交易所侧披露系统在周末滚动到下一交易日但没有数据,整站无排名可查。
探针产物:`data/rank_probe/dce_endpoint_probe/`(probe.py + result.json)。

**处置**:采集器行为正确(记 unavailable,不崩溃,不写脏数据)。
每日 21:30 的 "AK-trading Member Rank DCE" 任务(Lookback 5)无需改动;
预计 DCE 修复后,周一 09-14 晚间运行会同时补回 09-11 与 09-14。
若周一晚间仍全窗口空,再排查 DCE 是否改版接口。

注意:DCE 捕获文件按目标日命名(`data/rank_probe/dce_profiles/<YYYYMMDD>/`),
后来的失败运行会覆盖先前成功运行的捕获文件——排查历史问题时要意识到这一点。

## 运维注意

- EDB 两个合约阶段在非交易日之后不会再因新鲜度误报而 failed
  (2026-09-12 起)。若再出现 stale 批量告警,先查 expected-through 日志行。
- 大表查询禁令(经验教训,2026-09-12):对 market_bars 做
  `trading_day+duration_seconds` 组合聚合会触发 BitmapAnd 全表扫描
  (~180GB,数十分钟不返回,还会留下并行 worker 孤儿拖垮整库)。
  新鲜度检查一律用每符号 `max(ts)` 索引探测。
