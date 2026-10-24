# 本次成果独立审核交接

Root 使用独立 ZCode GLM-5.3/max 原审核会话和原 store；该会话不得参与本次两份操作脚本或报告的开发。此文件是现有任务的审核入口说明，不是新开发计划，也不扩大验收条件。本 owner 不启动 CLI、不新增联网尝试。

审核对象为 `PUBLICATION-FILES.json` 中的两份单次数据操作脚本及实际紧凑报告，冻结字节见 `FILE-HASHES.json`。四份本地 CSV 也是只读审核证据，大小/哈希在该清单中；它们不提交 GitHub。owner 在发布该冻结清单并通知 Root 后停止写入该文件集，FAIL 由 Root 续同 Sol owner，在独立新修订中修复，保留原失败与原字节。

标准沿既有 Astra `data/maintenance_artifacts/acquisition-execution-arbitration-20260919-r1/decision.md` 的 H01、H03.4、H07 与当前 AGENTS：只用免费公开 SDK、不得用 MAX+1 冒充完整、权威 nano 去重/原值验证、真实失败如实保留、无凭据暴露与交易。范围和限制以 `RESULT.md` / `RESULT.json` 为准，不能新增“所有历史/所有具体合约必须齐”的新验收，也不能把 80/81 物理窗口有数据等同全场完整性。

请独立核查：两脚本只接收 Root 注入的 stdin，不自动发现账号或读取 `.env`；不调用 main 的生产导入、订单、撤单、付费下载或关闭 TLS 校验；run001 SSLError/native2 与 run002 native0 分开；LR 仅报告源末端 1 月 16 日与查询空列表；实际入库为 0；2,100 行已有价格但日期标签未知未被隐藏；提交清单排除原始大 CSV、DB 与捕获日志。必要的最小反例可针对被改动的本地副本验证，标明 SYNTHETIC，不能写入冻结输入。

以下命令只读本地文件，不构造 SDK、不访问网络或数据库、不读取凭据。它重算两脚本语法、四份 CSV 哈希与原始/闭合对应、10,128 根源行的时间/值/键、紧凑覆盖报告算术及冻结清单。预期 `native exit 0`，输出 `OFFLINE_RECHECK_OK`，`lr_source_rows=10128`、`minute_tail_rows=306930`、`index_daily_rows=2640`、`stored_unknown_trading_day_rows=2100`。

<!-- OFFLINE-RECHECK-BEGIN -->
```powershell
@'
import ast, csv, hashlib, json
from pathlib import Path
from decimal import Decimal
from datetime import datetime, timezone, timedelta
root = Path('D:/Projects/AK-trading')
task = root / 'data/market_data_backfill/20261024-001'
read = lambda name: json.loads((task / name).read_text(encoding='utf-8'))
lines = lambda name: [json.loads(x) for x in (task / name).read_text(encoding='utf-8').splitlines()]
for name in ('download_recent_run001.py', 'download_recent_run002.py'):
    ast.parse((task / name).read_text(encoding='utf-8'))
source = read('download-run002/RESULT.json')
count = 0
for entry in source['series']:
    closed_path = root / Path(entry['closed_path'])
    raw_path = root / Path(entry['raw_path'])
    assert hashlib.sha256(closed_path.read_bytes()).hexdigest() == entry['closed_sha256']
    assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == entry['raw_sha256']
    with closed_path.open(encoding='utf-8', newline='') as handle:
        rows = list(csv.DictReader(handle))
    with raw_path.open(encoding='utf-8', newline='') as handle:
        raw = list(csv.DictReader(handle))
    assert len(rows) == entry['selected_closed_rows'] == entry['valid_rows']
    assert len(raw) == entry['frame_rows']
    raw_nanos = [Decimal(row['datetime']) for row in raw]
    assert all(nano.is_finite() and nano == int(nano) for nano in raw_nanos)
    by_raw = {int(nano): row for nano, row in zip(raw_nanos, raw)}
    nanos = [int(row['datetime_nano']) for row in rows]
    assert len(nanos) == len(set(nanos)) and all(a < b for a, b in zip(nanos, nanos[1:]))
    assert nanos[0] == entry['first_datetime_nano'] and nanos[-1] == entry['last_datetime_nano']
    fields = ['open', 'high', 'low', 'close', 'volume', 'open_oi', 'close_oi']
    for row, nano in zip(rows, nanos):
        display = datetime.fromisoformat(row['datetime']).replace(tzinfo=timezone(timedelta(hours=8)))
        assert int(display.timestamp()) * 1000000000 == nano
        if entry['duration_seconds'] == 86400:
            assert (nano + 8 * 3600 * 1000000000) % (86400 * 1000000000) == 0
        else:
            assert nano % (entry['duration_seconds'] * 1000000000) == 0
        values = [Decimal(row[entry['symbol'] + '.' + field]) for field in fields]
        assert all(value.is_finite() and value >= 0 for value in values)
        o, h, l, c = values[:4]
        assert min(o, h, l, c) > 0 and l <= min(o, c) <= max(o, c) <= h
        assert all(value == Decimal(by_raw[nano][field]) for value, field in zip(values, fields))
    count += len(rows)
assert count == 10128
minute = lines('minute-tail-quality-run001.jsonl')
daily = lines('index-daily-continuity-run001.jsonl')
latest = lines('latest81-coverage-run001.jsonl')
assert len(minute) == len(daily) == len(latest) == 81
assert sum(row['rows'] for row in minute) == 306930
assert sum(row['rows'] for row in daily) == 2640
assert sum(row['market_coverage']['rows'] for row in latest) == 26250
assert all(not row[key] for row in minute for key in ('duplicate_nano_keys', 'non_increasing_nano_keys', 'unaligned_minute_nanos', 'invalid_ohlcv_oi_rows', 'at_limit'))
assert sum(row['null_trading_day_rows'] for row in minute) == 2100
grids = [row['reference_grid_check'] for row in minute if 'reference_grid_check' in row]
assert len(grids) == 3 and all(row['expected_rows'] == 4020 and row['missing_nano_count'] == row['unexpected_nano_count'] == 0 for row in grids)
assert all(row['duplicate_nano_keys'] == row['invalid_ohlcv_oi_rows'] == 0 for row in daily)
assert [row['symbol'] for row in daily if row['missing_reference_calendar_days']] == ['KQ.i@CZCE.LR']
assert all(row['rows'] == 33 for row in daily if row['symbol'] != 'KQ.i@CZCE.LR')
overlap = read('LR-OVERLAP-COMPARISON.json')
assert overlap['total_source_rows'] == 10128 and overlap['total_missing_rows'] == overlap['total_conflicting_rows'] == 0
assert overlap['disposition'] == 'NO_IMPORT_NEEDED'
assert read('ROOT-RUN001-TERMINAL.json')['raw_native_exit'] == 2
assert read('ROOT-RUN002-TERMINAL.json')['raw_native_exit'] == 0
manifest = task / 'FILE-HASHES.json'
if manifest.exists():
    for item in json.loads(manifest.read_text(encoding='utf-8'))['files']:
        path = root / Path(item['path'])
        assert path.stat().st_size == item['bytes']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']
print(json.dumps({'status': 'OFFLINE_RECHECK_OK', 'lr_source_rows': count, 'minute_tail_rows': 306930, 'index_daily_rows': 2640, 'stored_unknown_trading_day_rows': 2100}))
'@ | & 'D:/Programs/Python/Python312/python.exe' -B -I -X utf8 -
$nativeCode = $LASTEXITCODE
exit $nativeCode
```
<!-- OFFLINE-RECHECK-END -->

上面的离线命令不会独立证明数据库逐值相等：`LR-OVERLAP-COMPARISON.json` 保存的是实跑比较汇总，没有落库内逐行快照。要独立重跑该结论，审核者可按 Root 已授权的本地应用连接边界，使用既有 `load_database_settings` / `create_database_engine` / `query_market_bars`，只在应用内部取得连接，使用 REPEATABLE READ READ ONLY 和 5 秒 statement timeout，不打印异常文本或任何凭据。三个准确半开区间从 `LR-OVERLAP-COMPARISON.json#/series/*/bounded_queries` 程序化取；symbol、duration、limit 亦取自该文件。按源 CSV 的 nano 和七字段比较 Decimal 原值。预期依序返回 5,000/5,000/128 行、10,128 全等、缺失/冲突/额外键全为 0。不运行全库 quality/group-by，不新增 SDK 会话，不读取 `.env` 到模型，不重放 run001/run002。若 Root 限制审核完全离线，则这项独立 DB 重跑必须明确 NOT_RUN，不能用重读汇总冒充完成。

实际诊断异常保留：可选 docs 文件内容发现曾在 25 秒超时 native1；一次本地候选清单诊断因 `expire_datetime_nano=None` 的比较 TypeError native1，后续读操作明确区分 unknown，未据此改数据或人口；独立 3.10.3 工具环境缺 dotenv 的 helper 导入探针未通过，实际在线操作复用原项目 3.10.2。首次 owner 离线重算错误地对原始 CSV 的科学计数法 nano 文本直接调用 int，native1，原件 `OFFLINE-RECHECK-RESULT.json` 保留；只改离线读取为 Decimal 精确整数解析，原始 CSV 未改变，后继结果另存 `OFFLINE-RECHECK-RESULT-002.json`。这些不是业务覆盖验收成功证据，也不是账户拒绝。原始工具结果保留在当前会话，简报列于 RESULT.json。

审核必须产出无条件 PASS 或 FAIL，分开说明操作工具、证据真实性、已验证数据范围与未验证范围；不能签“全库历史齐全”。审核结束前不修改冻结字节。原生退出码按 AGENTS §7/§8 与 `docs/maintenance/20261004-rg-no-match-r1/INCIDENT.md`、`docs/maintenance/20261004-grok-headless-cancel-r1/INCIDENT.md` 处理；只有可选 rg raw1+空 stderr 可叫 NO_MATCH，SDK/测试/CLI失败不是 NO_MATCH；输出先 exists 检查，不对尚未创建的审核输出故意失败 Read；保留 session/store/cwd 身份，不改模型或绕过拒绝。日志只在生产进程终止后固定，清单不固定自己或父 CLI 的活动流。
