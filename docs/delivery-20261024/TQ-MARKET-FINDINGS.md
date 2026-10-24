# TQ 免费行情补全成果独立硬审报告（HARD-001 完成 + HARD-002 恢复收口）

- 判定：**PASS（无条件）**，范围限于 `data/market_data_backfill/20261024-001` 这次确切免费行情操作、两份单次脚本及诚实报告；**不构成**全历史/全部具体合约完整性验收。
- 审核人独立，未实施该产品；HARD-001 因网络中断（trace 58d74f84，model.request.failed）native1，非产品判定；本次按 HARD-002 指令复用其已完成成功证据并只补齐剩余项，未重跑离线命令、DB 比对或任何 SDK/下载操作。

## 一、已完成并复用的独立证据（HARD-001）

1. **OFFLINE-RECHECK 原命令**（与交接块逐字一致的 ps1，转写校验留证）：native exit 0，输出 `OFFLINE_RECHECK_OK` 且四个计数（10128/306930/2640/2100）与预期逐字一致，stderr 空。
2. **冻结输入审前钉**：38/38（大小+SHA-256）全匹配。
3. **三次有界 DB 独立重读**（参数全部程序化取自 `LR-OVERLAP-COMPARISON.json`；应用内 `load_database_settings/create_database_engine/query_market_bars`；连接级 REPEATABLE READ + READ ONLY + statement_timeout 5s 经 SHOW 验证）：返回 5000/5000/128 行，**10128 个 nano 键与七字段 Decimal 原值全部相等，缺失/冲突/额外键均为 0**，用时 4.75s；无 DB 写、无 SDK、无 run001/run002 重放。
4. **脚本检查**：run001/run002 凭据仅来自 Root 注入 stdin 且用后即清、错误链只留类型/主机/令牌；TqSim、`enable_trading=False`、broker NOT_RUN、真实发/撤单为 0；无 TLS/proxy 变更、无 `.env`/账号自动发现；单次使用防护与 SDK 3.10.2 门禁在位。

## 二、本次恢复段新完成（HARD-002，仅此三项）

5. **SYNTHETIC 反例**（首次执行，exit 0，全部确认；副本与钉住原件仅差输出绑定一行，diff 留证）：
   - S1：科学计数法 nano 用 Decimal 精确解析；float 在 19 位奇数 nano 上实测丢精度；+1ns 键移位被精确判为 1 缺失+1 额外；1e-9 Decimal 值差被精确判为 1 冲突。
   - S2：干净 50 行切片零发现；注入的重复键/逆序对/非法 OHLC（high<low）被逐一精确检出（1/2/1）。
   - S3：冻结输入的字节篡改 scratch 副本被哈希旗标（同尺寸），真冻结件仍匹配。
6. **发布清单检查**：22/22 在盘；4 份本地市场 CSV（raw/closed × 2）与 `LOCAL_ONLY_MARKET_PAYLOAD` 钉完全一致且不在提交清单；无凭据/.env/DB/捕获流路径可发布；清单各项（除 FILE-HASHES.json 自除外）均有冻结钉。
7. **冻结输入审后复审**：38/38 全匹配——整个审核过程未改动任何冻结字节。

## 三、审查方 harness 诚实记录（均已分类，非产品缺陷）

- db-reread attempt1 AssertionError：会话级 SET 在 psycopg 非 autocommit 下被裹进旧读写事务（diag 证据显示 default_* 已设而活动事务仍 read committed/off）；attempt3 以临时 autocommit 施加 SET 修复。attempt1/attempt2 证据保留，HARD-002 未重跑 DB。
- synthetic_probes.py 执行前修正：S2 注入行原会带出第二个重复 nano，改为分配中点新键；修正先于任何执行，无被隐藏的失败运行。
- allowlist_check.py 运行前清理一处链式比较写法，无失败执行。

## 四、如实保留的产品侧事实（非本次审核失败）

run001 会话阶段 SSLError native2（根因未定、未复现、不称账户拒绝）；owner 离线重算首次 native1（int 解析科学计数法 nano）留原件、后继 -002 native0；三处诊断 native1（docs 超时、None 到期 TypeError、3.10.3 dotenv helper 导入）留 RESULT.json；run002 native0（56.172s，峰值 RSS 246,267,904B，TLS/proxy 未改）。

## 五、验收分类与剩余限制

- 工程与工具验收：**PASS**（脚本按报告行为运行、凭据链安全、失败如实保留）。
- 数据核验：**PASS**（离线算术独立复算 + 10128 行 DB 逐值全等复核，"实际入库 0 行/NO_IMPORT_NEEDED" 在已验证窗口内成立）。
- 历史覆盖：**不声称**。剩余限制：LR 免费源末端仅至 2026-01-16（且不据此断言永久退市）；2100 行 NULL trading_day 保留未修（13 系列）；仅 RB/TA/M 三个 4020 行精确会话网格被验证；2026 前标签、2018 异常片段与窗口外历史未重扫；run001 TLS 根因未定。

审核期间无网络、无 SDK、无 DB 写、无凭据读取、无模型配置变更、无嵌套代理；全部 Python 调用带 `-B`。证据哈希见 `evidence-hashes.json`（在全部子进程终止后计算）。
