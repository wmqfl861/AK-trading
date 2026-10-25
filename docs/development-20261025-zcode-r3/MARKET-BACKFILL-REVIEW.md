# 20261024-001 免费行情补全交付 · 独立离线硬审报告（market-backfill-hard-review-002）

审核者：独立 ZCode `GLM-5.3/max` 新会话，未参与本交付任何开发或修复（工单确认无先前绑定的
审核者 SID/store，本会话即该交付的实际审核者）。前次派发 `market-backfill-hard-review-001`
于上游 HTTP 502/native 1 中断，空 stdout、输入未变、无工作产物；本报告为退避后的一次有界
续审，完整执行原工单的离线范围，不重复已消耗的 run001/run002，不改模型/凭据/上下文配置。

依据：`REVIEW-HANDOFF.md`（冻结字节由 `FILE-HASHES.json` 固定）与既有 Astra 仲裁
`data/maintenance_artifacts/acquisition-execution-arbitration-20260919-r1/decision.md`
的 H01（免费通道/无交易/凭据注入边界）、H03.4（不得以 MAX+1 冒充完整、重叠去重）、
H07（权威 nano 整数键、原值精确验证、真实失败保留）。用户当前禁止读取凭据，Root 采用
handoff 明确允许的完全离线审核路线；handoff 第 92 段规定该路线下独立 DB 七字段重跑必须
如实标注 NOT_RUN，不得以重读旧汇总冒充。

## 终审结论

**无条件 PASS —— 限定于本工单声明的离线可审范围。** 未发现范围 内产品缺陷；全部必需
离线核查与反例如约完成并留证。独立 DB 七字段重跑、网络/SDK 实跑、DB 现场状态与全史
完整性为明确 NOT_RUN/未验证（见第 7 节），本 PASS 不授予其中任何一项信用，也不得被
引申为"全库历史齐全"或"数据库逐值相等"。

## 1. 冻结字节与清单完整性

- `FILE-HASHES.json`（sha256 见 VERDICT.json）共 **38** 个成员：PUBLICATION 21、
  LOCAL_ONLY_MARKET_PAYLOAD 4（两对 raw/closed LR CSV）、EXISTING_READ_ONLY_INPUT 13，
  无重复路径、无数据库/流文件。
- 官方离线重跑（第 2 节）在 manifest 循环中对全部 38 个成员逐一复验
  size+sha256 通过 —— 这是本审核对 Root "checked38" 的独立复验。
- `FILE-HASHES.json` 按声明自我排除（无法哈希自身），其 sha256 已由本审核另行计算并
  钉入 VERDICT.json；发布清单 22 文件 = 21 个 PUBLICATION 钉定成员 + 该清单自身，
  唯一差额即此自我排除，与声明一致（probes/manifest-check.json 全 12 项检查通过）。
- `REVIEW-HANDOFF.md` 自身哈希在提取阶段先行核验与其 pin 相符后才作为提取源。

## 2. 官方 OFFLINE-RECHECK 块的独立重跑

- 从冻结 handoff **程序化字节切片**提取 powershell 块（不经手抄）：
  `work/scratch/offline-recheck-exact.ps1`，块 sha256
  `b049180cc00beca78d8be138fc7a9df789bf8c53ba3ba32ad4a2e33e43b4a30a`（LF 74 行，
  提取记录 `work/scratch/block-extract.json`）。
- 以 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File` 原样执行，仅按工单
  将 stdout/stderr 捕获重定向至本 work 根；**native exit 0**，stderr 为空，stdout：
  `{"status": "OFFLINE_RECHECK_OK", "lr_source_rows": 10128, "minute_tail_rows": 306930,
  "index_daily_rows": 2640, "stored_unknown_trading_day_rows": 2100}` —— 与 handoff
  预期逐值一致（`work/results/offline-recheck-r1.*`）。
- 该单次运行独立覆盖：两脚本 `ast.parse` 语法；四份 CSV 对 run002 RESULT 的 sha 钉定；
  raw（科学计数法）→ Decimal 精确整数解析与 closed 七字段逐值相等（10,128 行）；
  nano 唯一/严格递增/分钟对齐/日线边界；OHLCV/OI 非负与高低开收关系；三份 jsonl 的
  81×3 行数、306,930/2,640/26,250 算术、at_limit 全 false、2,100 NULL trading_day、
  RB/TA/M 三网格 4,020×3 零缺零多；LR 唯一缺参考日、其余 33 行；overlap
  10,128/0/0 与 NO_IMPORT_NEEDED；run001 native 2 与 run002 native 0 的原始保留；
  38 钉全验。

## 3. 工具域静态核查（两份操作脚本及其执行面）

第一遍文本扫描（`probes/static-scan.json`，保留首跑原样）：两份操作脚本
`download_recent_run001.py` / `download_recent_run002.py` 在全部 8 类禁用模式
（付费 DataDownloader、真实/远程账户、下单撤单调仓、TLS 校验关闭、凭据发现、
环境变量读取、DB 写/engine、意外网络库）下 **零命中**；期望正向锚点（stdin
`json.load(sys.stdin)`、`TqApi(account=TqSim(), auth=TqAuth(...))`、`payload.clear()`、
`return 2 if failed else 0`、enable_trading/broker/real_sends/real_cancels 四旗标）
全部在位。

第一遍在既有 helper 模块上的文本命中经第二遍精确执行面分析
（`probes/static-scan-r2.json`）逐项定性：

- 操作脚本实际导入的 9 个 helper 函数（`fetch_snapshot`、`select_closed_frame`、
  `load_reference_book`、`fetch_recent_frame`、`select_valid_rows`、
  `write_recent_kline_frame_csv`、`datetime_from_nano`、`trading_day_for_timestamp`，
  含再导出链解析到定义文件）函数体内 **零禁用调用**。
- 涉及模块（fill_recent_main_minute.py、backfill_recent_free.py、market_data.py、
  market_reference.py）模块顶层 **无 engine/连接/API 创建**，import 时惰性。
- 文本命中的真实来源：helper 模块内**未被操作脚本导入**的既有 DB 导入 CLI 函数
  （`import_market_bars`/`store_reference_snapshot` 定义）、backfill 模块 docstring
  中"付费 DataDownloader 路径刻意不用"的说明文字、只读 SELECT helper 的
  `connection.execute`。均不在本交付执行路径上；market_data.py 含旧 importer 一事
  仲裁 §1.6 早有记录。
- `import ssl` 仅用于异常类型分类（SSLCertVerificationError/SSLError 重试判定），
  无任何 TLS 配置/绕过；`requests.exceptions` 仅函数内导入异常类，无网络 I/O。
- run002 的 `safe_error` 仅记录异常类型/host 后缀/reason 令牌并对秘密做替换脱敏；
  run001 失败仅记 `exception_type`。stdout 只打印状态 JSON。无秘密输出路径。
- 判别器验证（SYNTHETIC）：评审者自写 `synthetic_bad.py`（含 DataDownloader、
  TqAccount、verify=False、load_dotenv、os.environ[TQ_USER]、insert_order/cancel_order、
  create_engine/to_sql/commit）触发全部 7 类告警；镜像真实模式的
  `synthetic_clean.py` 零告警 —— 检测器具备判别力，非空转。

## 4. 科学计数法 nano 解析政策（数据证据）

`probes/sci-nano-probe.json`：两份 raw CSV 的 `datetime` 文本 **10,000/128 全部为
科学计数法**（如 `1.763004e+18`）；对全部单元直接 `int(text)` 均 ValueError ——
owner 首次离线重算的初始失败（原件 `OFFLINE-RECHECK-RESULT.json` 保留）确为真实
失败模式，继任者改用 Decimal 精确整数解析是正确修复。诚实边界：在本数据集上
`int(float(text))` 恰与精确值全部一致（0 处偏差）——本审核不声称 float 在此数据上
已造成或必然造成损坏，只确认 Decimal 是**构造性精确**的政策而 float 仅为巧合精确；
closed CSV 的 `datetime_nano` 全为纯整数文本。

## 5. SYNTHETIC 反例与对照（`probes/counterexamples.json`，全组 10/10 达预期）

副本树全部位于 `work/probes/<case>/`，冻结输入零改动；replica 由官方块程序化派生
（修改仅：root 行参数化；data replica 另去掉两条 sha 钉定使数据断言可被直接触及；
records 含每只 replica 的 sha256 与逐条修改说明）。未突变对照 `ctrl-d`/`ctrl-r`
在 replica 下 native 0、stderr 空，证明 replica 本身不误杀。

| 案例 | 突变（副本） | 预期断言 | 结果 |
| --- | --- | --- | --- |
| ce-00 raw-tamper | index raw volume+1 | raw sha256 钉定 | exit 1 AssertionError ✓ |
| ce-01 duplicate-nano | closed 倒数第二行 nano:=末行 | nano 唯一/严格递增 | exit 1 ✓ |
| ce-02 unaligned-minute | 中间行 nano+30s（显示时间同步+30s） | 分钟对齐 `nano % 60e9` | exit 1 ✓ |
| ce-03 ohlc-violation | high:=low/2 | `l<=min(o,c)<=max(o,c)<=h` | exit 1 ✓ |
| ce-04 value-mismatch | closed volume+1 | closed==raw 七字段逐值 | exit 1 ✓ |
| ce-06 at-limit | minute jsonl 某行 at_limit:=true | coverage 算术 at_limit 断言 | exit 1 ✓ |
| ce-07 overlap-missing | total_missing_rows:=1 | overlap 全等断言 | exit 1 ✓ |
| ce-08 run001-exit-swap | run001 terminal exit:=0 | `raw_native_exit == 2` | exit 1 ✓ |

每个子进程的 stdout/stderr/exit 原样保存在 `work/probes/ce-*.‹stdout|stderr|exit›.txt`，
traceback 精确命中预期断言行（已抽检 ce-00/02/08）。

## 6. 报告口径与声明边界核查

- **MAX+1/10000 窗口上限**：minute-tail/latest81 与 overlap 的 `at_limit` 全 false
  （官方重跑断言 + ce-06 反例证判别）；run002 请求 10,000 根恰为 `get_kline_serial`
  data_length 文档上限（skill market-data 参考），交付将其表述为有界近期窗口、
  免费序列不可向过去翻页，未冒充完整历史；RB/TA/M 之外的网格完整性以限制条款如实
  未主张。未见任何 MAX(trading_day)+1 式完整性推导。
- **LR 源端观察**：主连末端 2026-01-16 14:59、指数末日 2026-01-16、underlying
  `CZCE.LR601`、未到期 LR 期货查询返回空列表，均只作源端观察并附"不推导永久退市"
  免责（RESULT.json `lr_free_source`）。
- **2,100 行 NULL trading_day**：13 个跨午夜系列价格行在分钟尾窗检查中如实计数并
  保留，未删除未隐藏，限制条款在列。
- **入库 0**：`database_rows_imported=0`、`database_write=NOT_RUN(_NOT_NEEDED)`、
  overlap `disposition=NO_IMPORT_NEEDED`；执行面无 DB 写调用（第 3 节）。DB 现场
  状态本身属离线不可验，见第 7 节。
- **发布清单卫生**：22 文件全在任务目录内，无 CSV、无 redacted 流、无数据库；
  四份大 CSV 明确列为 excluded_local_payloads 且具 LOCAL_ONLY 角色。
- **失败保留**：run001 SSLError/native 2 与 run002 native 0 分列，TLS 根因
  UNDETERMINED 如实保留；三条诊断异常（docs 超时、None 比较 TypeError、3.10.3
  dotenv 探针）原样在册。

## 7. NOT_RUN / 未验证边界（本 PASS 不覆盖）

1. **独立 DB 七字段重跑：NOT_RUN**（离线路线明确允许并要求如此标注）。本报告未将
   `LR-OVERLAP-COMPARISON.json` 旧汇总读回计为独立 DB 证据；库内逐值相等、缺失/
   冲突/额外键为零等结论保持 owner 实跑声明，未经独立复验。
2. 网络/SDK/TqApi 实际行为、免费源当前可达性：NOT_RUN（不构造 TqApi、不联网、
   不重放 run001/run002）。
3. 数据库当前未被写入的现场核验：离线不可达（静态面已证脚本无 DB 写路径）。
4. 全史/全具体合约完整性：产品未主张，本审核亦不验证、不授予。
5. 2026 前历史日期标签、2018 异常源片段、观测窗口外范围：未重扫（产品限制条款如实）。
6. run001 TLS 首败根因：保持 UNDETERMINED，本离线审核不新增归因。

## 8. 评审侧保留失败（harness 自身，均已后继修复，原始证据保全）

- `scan_static.py` 首跑 native 1（结论聚合器对 hits 列表的 TypeError）与第二跑
  native 1（同族 AttributeError）：纯评审 harness 缺陷，扫描逻辑与被审文件无关；
  第三跑 native 0 落盘 `static-scan.json`（其中两个 false 结论源于模块级遍历器
  误入函数体的缺陷，由 r2 精确执行面分析取代并解释）。
- `scan_static_r2.py` 首跑 native 1（`datetime_from_nano` 再导出链未解析的
  AssertionError）；补递归解析后 native 0。
- 全部失败命令的原生退出码见第 9 节；`static-scan.json` 首跑记录未改动保留。

## 9. 命令与原生退出码日志（全部输出在本 work 根）

| # | 命令 | native exit | 产物 |
| --- | --- | --- | --- |
| 1 | `python -B -I -X utf8 scratch/extract_block.py` | 0 | offline-recheck-exact.ps1 + block-extract.json |
| 2 | `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scratch/offline-recheck-exact.ps1`（stdout/stderr 重定向至 results/） | 0 | results/offline-recheck-r1.{stdout,stderr,exit}.txt |
| 3-5 | `python ... scratch/scan_static.py`（三次：1/1/0） | 1,1,0 | probes/static-scan.json |
| 6-7 | `python ... scratch/scan_static_r2.py`（两次：1/0） | 1,0 | probes/static-scan-r2.json |
| 8 | `python ... scratch/sci_nano_probe.py` | 0 | probes/sci-nano-probe.json |
| 9 | `python ... scratch/prep_and_run_ce.py` | 0（all_met=true） | probes/counterexamples.json + ce-* 树/流 |
| 10 | `python ... scratch/manifest_check.py` | 0 | probes/manifest-check.json |
| 11 | `python ... scratch/make_verdict.py` | 0 | VERDICT.json |

只读发现类命令（ls / rg 定位定义与 import，均 exit 0）用于输入核对，不构成验收。
解释器统一 `D:/Programs/Python/Python312/python.exe`；无网络、无 DB、无凭据读取、
无 git/service/UI 操作、无嵌套代理；交易恒 false、观察者 hold、broker NOT_RUN、
真实发送/撤单 0。

## 10. 终审声明

对 `data/market_data_backfill/20261024-001` 冻结交付（FILE-HASHES.json v38）在
本工单授权的完全离线路线上：**PASS（无条件，限定上述已验证范围）**。操作工具、
证据真实性与已验证数据范围同第 2–6 节分列；未验证范围同第 7 节分列且不因本
PASS 而关闭。审核期间冻结字节零改动；审核产物全部位于
`D:/Maintenance/aktr-development-20261025/market-backfill-hard-review-002/work/`。
