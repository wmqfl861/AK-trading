# AK-trading

Domestic futures quant scaffolding on [TqSdk](https://www.shinnytech.com/tqsdk).

## Project documents

- [HANDOVER.md](HANDOVER.md) - current stage, database/data snapshot, operating runbook, verification evidence, audit history, limits, and handoff checklist.
- [PROJECT_PLAN.md](PROJECT_PLAN.md) - detailed phase plan, gates, acceptance criteria, risks, and the sequence before formal strategy development.

## Setup

```powershell
cd D:\Projects\AK-trading
python -m pip install -e ".[test]"
copy .env.example .env
# edit .env: TQ_USER / TQ_PASS when using TqSdk data or simulation
```

Default trading mode is local `TqSim` only.

## PostgreSQL storage

PostgreSQL support is optional. Leave `AK_DATABASE_URL` empty to keep the
existing JSON/JSONL/Markdown-only behavior. Install the extra when database
storage is needed:

```powershell
python -m pip install -e ".[test,postgres]"
```

The database URL belongs in `.env` and must not be pasted into source files,
shell history, logs, or chat. Use a dedicated application role and database.
The project does not create roles or databases automatically. As a PostgreSQL
administrator, provision them interactively so the password is not embedded in
the command line:

```text
CREATE ROLE ak_trading LOGIN;
\password ak_trading
CREATE DATABASE ak_trading OWNER ak_trading;
```

Then set `AK_DATABASE_URL` in `.env`. Percent-encode reserved characters in the
password when putting it in a URL, for example `@` becomes `%40`:

```text
AK_DATABASE_URL=postgresql://ak_trading:YOUR_ENCODED_PASSWORD@127.0.0.1:5432/ak_trading
```

Apply and inspect the schema with the management script:

```powershell
python scripts\manage_database.py migrate
python scripts\manage_database.py status
python scripts\manage_database.py import-reference
python scripts\manage_database.py import-existing
```

`import-existing` is idempotent. It imports journals from
`data/runs/` and `data/backtests/`, their matching summaries, and pre-open
reports from `data/market_reference/`. Reference snapshots retain the exact
source bytes and SHA-256; a changed payload under an existing immutable identity
is rejected.

## Daily exchange-rules collection

The default exchange-rules workflow is independent from the legacy
`config/market_reference.json` pre-open report. It loads the futures-only catalog
at `config/china_futures_catalog.json` and covers the six mainland exchanges:
SHFE, INE, DCE, CZCE, CFFEX, and GFEX.

The checked-in catalog contains 81 futures products and six representative,
explicit `excluded_option` records, one for each exchange. An option cannot enter
the futures rule universe, candidate snapshots, scope review, or scoped current
pointers. These exclusions make the present futures-only boundary auditable; they
are not an inventory of every option family. Catalog membership remains subject to
official-directory review until deterministic exchange-specific refresh adapters
are implemented.

The default target is determined per exchange from its own catalog calendar. A
catalog run records every expected product as `processed_futures`,
`closed_futures`, or `unresolved_futures`; `processed` only means the product
entered the collection path, not that its values have been approved. A failed or
ambiguous extraction remains `review_required` or `unknown`, never zero. Search
results are discovery-only: product and numeric facts must come from retained
official exchange documents. Close-today is a separate fee field, and
same-contract lock margin is calculated only when an explicit exchange formula is
available.

Run the all-futures catalog collector with the local artifact store:

```powershell
python scripts\collect_exchange_rules.py
```

The prior RB/TA/M collector remains available only as an explicit legacy path. It
continues to use `config/exchange_rule_sources.json` and
`config/market_reference.json`; it does not define the all-futures universe:

```powershell
python scripts\collect_exchange_rules.py --legacy-three-products `
  --now 2026-07-28T20:30:00+08:00 `
  --trading-day 2026-07-29 `
  --fixture SHFE=tests\fixtures_exchange_rules_shfe.json `
  --fixture CZCE=tests\fixtures_exchange_rules_czce.json `
  --fixture DCE=tests\fixtures_exchange_rules_dce.json
```

The collector stores immutable documents, snapshots, legacy runs, and catalog run
manifests under `data/exchange_rules/`. The legacy global `current.json` is left
unchanged by catalog collection. Catalog approvals are field-level and scoped to
one exchange, product, or contract, with a catalog ID and candidate snapshot as
provenance:

```powershell
python scripts\review_exchange_rules.py `
  --snapshot CANDIDATE_SHA256 `
  --reviewer REVIEWER_NAME `
  --reason "Official product fee checked" `
  scope-approve `
  --catalog-id CATALOG_SHA256 `
  --scope-kind product `
  --exchange SHFE `
  --product RB `
  --field fee:open
```

Use `scope-reject` with the same scope and explicit fields to retain a rejection.
Only deterministic `candidate` fields can be scope-approved. A whole-scope
approval requires both `--all-scope-fields` and `--confirm-all-scope-fields`, and
is refused when that scope contains a `review_required` field. Legacy
`approve`/`reject` remain available for the legacy global snapshot lifecycle.

The local account estimates remain separate from exchange facts: fee is exchange
fee plus CNY 0.01 per lot per charged direction, and margin is exchange margin
rate plus 0.03 (three percentage points). PostgreSQL is optional. Existing
exchange-rule import remains an explicit audit action after an authorized
migration; the catalog v2 manifests and scoped-current pointers are currently
canonical local artifacts and are not automatically imported into PostgreSQL.

Start the read-only local workbench on `127.0.0.1`:

```powershell
python scripts\exchange_rule_workbench.py
```

Open `http://127.0.0.1:8765/`. The initial `/api/state` response is a compact
summary with six-exchange coverage and scope counts. Full rows are available only
through bounded, read-only, paginated resources: `/api/rules`, `/api/documents`,
`/api/catalog-products`, `/api/excluded-options`, `/api/review-queue`, and
`/api/history?kind=SNAPSHOTS|RUNS|CATALOG_RUNS|APPROVALS|SCOPE_APPROVALS|SCOPE_CURRENTS`.
Use `offset` and `limit`; `limit` must be 1 through 100. The browser has no
approval or rejection write action.

The Windows Task Scheduler installer is dry-run by default. It describes the
`20:30 Asia/Shanghai` task, explicit Python/working-directory paths, and
`IgnoreNew` overlap behavior without registering anything:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_exchange_rules_task.ps1
```

Registration is deliberately gated behind `-Apply` and requires separate explicit
authorization. The task command contains no TQ or database credentials.

The persisted responsibilities are deliberately narrow:

| PostgreSQL table | Stored responsibility |
| --- | --- |
| `reference_snapshots`, `reference_products`, `reference_closed_dates` | Versioned fees, margin rules, sessions, calendar, provenance, and raw reference JSON |
| `exchange_rule_source_contents`, `exchange_rule_source_documents` | Content-addressed official evidence bytes and source metadata |
| `exchange_rule_snapshots`, `exchange_rule_items`, `exchange_rule_runs` | Immutable candidate/current/legacy snapshots, field-level rules, diffs, and collection outcomes |
| `exchange_rule_approvals`, `exchange_rule_current` | Append-only review decisions and the validated current-approved pointer |
| `strategy_runs`, `strategy_events`, `run_reports` | Run manifests, event evidence, lifecycle reports, and conflict-checked sequence numbers |
| `preopen_reports` | Generated JSON/Markdown report content tied to one reference snapshot |
| `ingestion_batches`, `market_bars` | Idempotent TqSdk minute-or-other-positive-duration K-line imports keyed by `datetime_nano` |

JSON/JSONL/Markdown files remain export and audit artifacts. The backtest
workflow writes the file journal first and synchronously writes the same event
to PostgreSQL; a database failure makes the run fail. The live `TqSim` workflow
does no database I/O inside `wait_update()`: it safely flattens and closes,
writes the report, then imports the completed artifacts. The pre-open report
always calculates from the canonical JSON file; PostgreSQL is an audit store,
not a replacement source for safety-critical calculations.

Download and import one explicit historical K-line range with
`DataDownloader`:

```powershell
python scripts\download_market_data.py `
  --symbol KQ.m@SHFE.rb `
  --start 2026-01-05 `
  --end 2026-01-30 `
  --duration 60 `
  --acknowledge-data-cost
```

The command requires the explicit symbol, date range, and duration, uses the
TqSdk `DataDownloader` CSV schema, parses decimal values without floats, and
uses `datetime_nano` as the authoritative unique time key. A temporary CSV is
removed after the import; pass `--csv-output` when a CSV export should be
retained. A bar outside the configured calendar coverage, or on a configured
closed day, gets a `NULL` `trading_day`; holidays are never guessed.

`DataDownloader` is the only supported long-range path and may require a paid
TqSdk data entitlement. The command never bypasses that entitlement. A denied
account returns `DOWNLOAD_FAIL` with exit code 2 and writes no ingestion batch
or bars.

When paid history is unavailable, snapshot the free recent in-memory window
(at most 10,000 bars per symbol) instead:

```powershell
python scripts\snapshot_recent_market_data.py `
  --symbol KQ.m@SHFE.rb `
  --symbol KQ.m@CZCE.TA `
  --symbol KQ.m@DCE.m `
  --duration 60 `
  --rows 10000
```

This is a bounded recent snapshot, not unlimited history. Every requested
symbol must download and strict-validate in a system temporary directory before
the first database connection. Imports then use independent per-symbol
transactions: a later database failure can leave earlier reported batches
committed. Repeating an unchanged snapshot is idempotent; a shifting recent
window creates another audit batch and upserts overlapping bars. Temporary CSVs
are always removed.

The initial local baseline created on 2026-08-01 contains 10,000 one-minute bars
for each of RB, TA, and M main contracts (30,000 total), covering
2026-06-23 09:05 through 2026-07-31 22:59 Asia/Shanghai. A requested
2026-05-01 through 2026-07-31 DataDownloader baseline could not be created
because the configured TqSdk account has no paid historical-download entitlement.

Read and inspect stored bars without importing TqSdk or trading credentials:

```powershell
python scripts\query_market_data.py quality

python scripts\query_market_data.py list `
  --symbol KQ.m@SHFE.rb --duration 60 `
  --start 2026-07-01T09:00:00+08:00 `
  --end 2026-07-01T10:00:00+08:00 --limit 100

python scripts\query_market_data.py export `
  --symbol KQ.m@SHFE.rb --duration 60 `
  --start 2026-07-01T09:00:00+08:00 `
  --end 2026-07-01T10:00:00+08:00 `
  --output data\exports\rb_20260701.csv
```

Time ranges are start-inclusive and end-exclusive. ISO boundaries must contain
a timezone offset; list limits are 1 through 10,000. Export uses a same-directory
temporary file and refuses to overwrite unless `--force` is supplied. Quality
output reports the largest interval observed between stored rows, but it does
not call that interval a missing-bar defect: exact gap classification requires
complete product/session and exceptional-calendar data that this phase does not
yet model.

The first phase stores minute K-lines and reference/run/report data. It does not
store the full Tick stream, does not use PostgreSQL as a TqBacktest data feed,
does not replace TqSdk's `TqBacktest`, and does not enable real accounts.

For a backup, use PostgreSQL's client tools and let them prompt for the role
password rather than putting it in the command line:

```powershell
pg_dump --host 127.0.0.1 --port 5432 --username ak_trading --password `
  --dbname ak_trading --format custom `
  --file data\db_backups\ak_trading_20260730.dump
```

Restore into a separately provisioned empty database first, validate it, and
only then switch `AK_DATABASE_URL`; this keeps the existing database recoverable
while a backup is being checked:

```powershell
pg_restore --host 127.0.0.1 --port 5432 --username ak_trading --password `
  --dbname ak_trading_restore data\db_backups\ak_trading_20260730.dump
```

The ordinary `ak_trading` role cannot create or drop databases. A PostgreSQL
administrator must provision and later remove the isolated restore target:

```sql
CREATE DATABASE ak_trading_restore OWNER ak_trading;
-- Run pg_restore with --no-owner --no-privileges, then compare data.
DROP DATABASE ak_trading_restore;
```

Never restore over the active `ak_trading` database. Do not put an administrator
password in `.env`, a command-line argument, or a committed script. Let PostgreSQL
client tools prompt for it, or supply it only to one explicitly authorized local
process. Before dropping the verification database, confirm its exact name and
terminate sessions connected only to that verification database.

Integration tests use `AK_TEST_DATABASE_URL` only when it points to a different
database whose name ends in `_test`. They never accept the application database
as a test target.

## Member position rank collection (多空持仓排名)

Daily per-member top-20 disclosures of volume, long open interest, and short
open interest, stored in `member_position_ranks` (migration
`20260831_0003`) with raw official payloads kept under
`data/member_ranks/{EXCHANGE}/{YYYYMMDD}/`.

```powershell
# latest three days, all implemented exchanges, evidence + idempotent upsert
python scripts\collect_member_ranks.py
# explicit window / subset / evidence-only
python scripts\collect_member_ranks.py --start 2026-08-01 --end 2026-08-29 --exchange SHFE --exchange CZCE --dry-run
```

Endpoint map and caveats (`src/ak_trading/member_rank.py` docstring):

- SHFE `pm{YYYYMMDD}.dat` — direct JSON; covers SHFE products plus INE
  `bc`/`lu`/`nr`; includes contract totals (RANK 999) and the two product
  subtotal rows. INE publishes the same format with `bc`/`lu`/`nr` only;
  `sc`/`ec` ranking is not in any `pm` file (documented gap).
- CFFEX per-product XML (`datatypeid` 0=volume double counted, 1=long,
  2=short; mapping verified against official daily stats).
- GFEX POST interfaces (`loadListContract_id` for contracts; `data_type`
  1/2/3); server skips contracts below its disclosure threshold.
- CZCE `FutureDataHolding.htm` via the humanlike browser; product-aggregate
  and per-contract blocks; `PTA` header maps to product code `TA`.
- DCE drives the official SPA in the humanlike browser (date typed into the
  picker, per-variety route reload). The Ruishu WAF rate-limits this flow;
  run DCE sparingly and expect cooldowns after repeated probing.
- `bc`/`lu`/`nr` are disclosed by both SHFE and INE files; consumers should
  pick one source per product (e.g. prefer INE rows).

Factor helper: `ak_trading.persistence.member_rank.top20_net_by_product(
engine, day)` returns per-product top-20 net position (aggregate-level
products directly, contract-only sources rolled up).

### Daily automation

Two Windows scheduled tasks (registered 2026-08-31 via
`scripts\install_member_rank_task.ps1 -Apply`, remove with
`Unregister-ScheduledTask`):

- **AK-trading Member Rank Collection** — 18:15 Asia/Shanghai daily; runs
  `scripts\run_member_rank_daily.ps1` with `--lookback 5` for
  CFFEX/CZCE/GFEX/INE/SHFE. The 5-day self-healing window re-collects after
  holidays or offline days (upserts are idempotent).
- **AK-trading Member Rank DCE** — 21:30 Asia/Shanghai daily, best effort;
  the Ruishu WAF throttles the DCE SPA, so failures just skip the day and
  the next run retries.

Logs land in `data\member_ranks\task_logs\` (90 days retained);
`bootstrap.log` records every task start. Historical availability of the
official endpoints: SHFE from 2015, CFFEX from 2010, INE from 2018 (crude
launch), GFEX from 2022-12; backfill any window with
`python scripts\collect_member_ranks.py --start ... --end ...`.

## Scripts

```powershell
python scripts\verify_env.py
python scripts\minimal_quote.py --timeout 60 --updates 5
python scripts\minimal_sim.py --timeout 60
python scripts\manage_database.py status
```

- Main-contract symbols (`KQ.m@...`) are for quotes / resolution.
- `minimal_sim` resolves to a concrete non-expired FUTURE before `TargetPosTask`.
- Scripts use wall-clock `--timeout` around `wait_update(deadline=...)`.

## Dual-MA engineering strategy

The first strategy is a long/flat engineering smoke test, not a profitability
claim. It uses only completed K-lines: when a new `klines.iloc[-1]` appears,
the signal input explicitly excludes that incomplete last row.

Live market data with a disposable local `TqSim` account:

```powershell
python scripts\run_sim_strategy.py --duration 300 --fast 5 --slow 20
```

The configured main contract is resolved once at startup. The resulting
delivery-month contract is then fixed for the whole process; automatic roll is
not implemented. On interruption the runner targets zero, waits for a flat
position, cancels `TargetPosTask`, and drains its cancellation before closing.

Historical backtest with an explicit concrete contract and date range:

```powershell
python scripts\backtest_strategy.py `
  --symbol DCE.m2609 `
  --start 2026-01-05 `
  --end 2026-01-30
```

Backtests end by catching TqSdk's `BacktestFinished`. They record the final
position and `TqSim` statistics but do not pretend to fill a flattening order
after historical data has ended. Choose a delivery contract that was active in
the requested period.

Decision journals are written under `data/runs/` or `data/backtests/`; both are
ignored by Git. Default paths carry a UTC timestamp and opaque run ID, so runs
do not append to each other; pass `--journal` to choose a stable path and
`--report` to choose its JSON summary location. Each run records a credential-
free manifest, event evidence, and a report containing lifecycle state,
recorded account snapshots, decision actions, blocked reasons, and the runner's
final summary. Reports deliberately do not reconstruct fills, trades, P&L, or
performance metrics from the journal.

Reusing the same explicit `--journal` path appends another run to that physical
file. Every record carries an opaque `run_id`, and the corresponding report is
filtered to the current run; choose a new path when one run per file is desired.

Use `--max-abs-position` to raise the fixed target-position cap for a local
simulation or backtest. It is a lot cap, not a margin-aware sizing model; the
default remains one lot.

## Offline backtest evidence verification

Validate an existing stdout/stderr/journal/report bundle without network access,
TqSdk construction, or loading settings/credentials. The verifier never reads
`.env` or project settings. It may optionally scan the current process
environment values of `TQ_USER` / `TQ_PASS` as known secrets; when those env
values are absent, `secret_scan` is `skipped`, which only means the structural
scan finished - it does **not** prove that unmarked secrets are absent.

```powershell
python scripts\verify_backtest_artifacts.py `
  --stdout path\to\stdout.log `
  --stderr path\to\stderr.log `
  --journal path\to\run.jsonl `
  --report path\to\run.summary.json `
  --exit-code 0 `
  --min-decisions 1
```

Optional identity checks: `--expected-symbol`, `--expected-start-date`,
`--expected-end-date`, `--expected-duration`, `--expected-fast-window`,
`--expected-slow-window`, `--expected-max-position`, `--expected-run-id`.
`--min-decisions` is a positive non-bool int and defaults to `1`.

Exit codes:

- `0` / `pass`: structures, types, and recomputed evidence all match.
- `1` / `invalid`: structures and types are correct for supported v1, but
  values, order, identity, domain, expected params, counts, paths, dates,
  account/stat relations, or producer exit code disagree.
- `2` / `error`: usage, I/O, decode, resource/internal failure, unsupported
  version, malformed report/top-level, or supported-v1 missing/extra/wrong-type
  report, manifest, selected-event, BACKTEST_DONE/BACKTEST_STAT, or account
  schema. Journal raw malformed/blank/non-object/duplicate/unknown lines remain
  `invalid` (`1`) by contract.

Output is stable ASCII-safe JSON written once to stdout. Usage/argparse errors
use a fixed detail and never echo raw argv or secrets.

## RB / TA / M pre-open reference report

Generate the daily reference report before the night session. The command checks
indexed pages on the three official exchange domains in parallel through the
workspace `websearch` service, then writes a dated JSON file and a Markdown file:

```powershell
python scripts\preopen_market_report.py `
  --trade-price RB=3100 --trade-price TA=5700 --trade-price M=3150 `
  --settlement-price RB=3095 --settlement-price TA=5680 `
  --settlement-price M=3140
```

When `AK_DATABASE_URL` is configured, the same command stores the exact
reference snapshot and the generated report after both local artifacts have
been written. A database write failure is reported as a nonzero result, while
the local report remains available for inspection.

Output is stored under `data/market_reference/<trading-day>/`. Runs before 15:00
target the current open trading day; runs after 15:00 target the next configured
trading day. The bundled holiday calendar is intentionally bounded to 2026 and
the command fails outside that coverage instead of guessing.

The report uses explicit exchange-source snapshots in
`config/market_reference.json`. Indexed notice results are discovery evidence
only: the program never copies a number out of a search snippet. When a newer
dated official result is found, the exit code is `1` (`review_required`). A
failed notice check, expired research contract, expired source review date, or
calendar error returns `2`. Use `--offline` only to reproduce the checked-in
snapshot without the daily official-domain check.

Fee calculations keep exchange fees and the user-specified broker markup
separate. "Exchange fee + 1 fen" is currently modeled as CNY 0.01 per lot per
charged side, so a complete open/close round trip adds CNY 0.02. RB fees use the
trade price because the exchange fee is ad valorem. Exchange margin estimates
use the settlement price. Broker margin, broker minimum fees, slippage, spread,
order/cancellation fees, delivery fees, and funding cost are not inferred.

Current transcriptions are marked `values_verified: false` and
`reconciled_with_broker_statement: false`. To approve an update, re-read the
linked official notice, change only the applicable contract-scoped record,
record its publication/effective/review dates, have a second person check the
number and unit, and then set `values_verified` deliberately. Never turn a
missing fee component into zero; use `unknown` unless the notice explicitly
states zero.

The scheduler installer described above is the only scheduler integration in this
phase. The collector's trading-day and source-review checks remain authoritative
when a task is triggered.

## Offline multi-strategy research prototype

This is a **normalized-price allocation experiment, not a futures backtest or
profitability claim**. It creates no TqSdk account, reads no `.env`, and does no
network or database I/O. The existing dual-MA smoke strategy is unchanged.

```powershell
python scripts\research_multistrategy.py --demo --output data\research_demo_01
python scripts\research_multistrategy.py --csv data\research_demo_01\input.csv --config data\research_demo_01\config.json --output data\research_replay_01
```

Use a new output directory for every run; existing directories are refused.
Each bundle contains `input.csv`, resolved `config.json`, and deterministic
`report.json` (schema version 1). Replaying the retained input and configuration
with unchanged code produces byte-identical reports. SHA-256 values identify
input bytes, canonical inputs/configuration, training data/model, and source files.
The stdout JSON summarizes dynamic and fixed-risk OOS results. Invalid arguments,
input, configuration, or I/O return exit code 2, never a successful report.

**Holding-period convention:** a completed bar's decision enters at the next
bar's open and has a precommitted exit at that bar's close. This is only the
prototype's accounting convention, **not a decision by the user to forbid
night/overnight exposure or choose a real strategy holding period**. Normalized
initial capital is 1, signed entry exposure becomes `units = exposure / open`,
and gross PnL is `units * (close - open)`. Exposures do not reinvest profits.
Both entry and exit absolute notionals pay fees and slippage, each defaulting to
1 basis point per side. Signed expert exposures are netted before these costs.
Cash capacity is `1 - gross exposure`, not a broker cash or margin balance.

Breakout and bounded mean-reversion experts run concurrently, without random
selection, pyramiding, grid or martingale. Breakout compares the current completed
close with the preceding `lookback` closes. Range uses clipped negative z-score
against that preceding window. Three nonnegative normalized **uncalibrated
heuristic scores** are smoothed causally: trend/range are the two learnable
market states, while `uncertain` is a **reserved cash/abstention score**, not a
third learned market forecast. Its training bucket stays empty/unavailable and
never contributes learned edge. Normalized entropy is reported separately from
economic edge. Efficiency `E` is absolute net close
movement divided by total absolute close movement over `lookback` increments.
Raw scores are `0.05+0.9*E`, `0.05+0.9*(1-E)`, and
`0.05+1.8*E*(1-E)`, then normalized. EMA alpha defaults to 0.25; the display/training
state switches only when a competing score leads by at least 0.1. The third raw
score is at most 0.5 while the first two sum to 1; after normalization and EMA,
it cannot dominate either market state. It explicitly reserves capital instead.
Model/report metadata lists `learnable_states` and `reserved_cash_state`.

`train_bars` defaults to 300 (minimum 100); training rows are `0..T-1`.
Last training decision is `T-2`, with its outcome ending at `T-1` close. First OOS
decision uses `T-1` close and executes bar `T`. Training statistics/correlations
are frozen thereafter. All reported performance is OOS only. Per-state evidence
needs 30 active observations by default (configurable minimum 2 for worked tests).
Counts/means/variance/downside and a mean-minus-standard-error edge heuristic are
reported; these counts/penalties do not prove statistical validity or calibration.

The dynamic allocator softly combines the **entire three-state vector** with
training conditional payoff/loss evidence, not just its dominant state. Missing
state mass is never renormalized: it stays cash, with an evidence-mass gate
(default 0.5). Positive conservative edge determines relative budgets, but their
total is attenuated by `min(1, max_edge / edge_full_scale)` (default 0.002 per
research bar), known evidence mass and `1 - entropy`. Tiny positive edge therefore
cannot become a full budget by normalization. Nonpositive edge means cash.
The fixed baseline has one fixed `1/(2 * instrument_count)` risk slot per expert;
unsupported mass/ineligible slots remain cash, with **no edge-sign gate** and no
redistribution. Both share signals, data/evidence/uncertainty gates, past volatility
sizing, costs and execution schedule. This is a fixed-slot benchmark with shared
availability gates, not an unconditional always-invested benchmark. **Identical
risk limits do not mean identical realized risk or exposure.** Compare mean/max
gross exposure, turnover and drawdown alongside net PnL. The bundled synthetic
example's dynamic portfolio uses substantially more average exposure than fixed;
a larger net PnL cannot establish allocator alpha or risk-adjusted superiority.
No ex-post realized-risk matching or parameter tuning is performed.

Risk only scales down: per-expert cap, signed instrument netting, per-instrument
cap, gross cap, absolute-correlation connected-cluster cap, then conservative
`sum(abs(exposure) * volatility)` cap. Defaults are 0.25/expert, 0.4/instrument,
0.8 gross and 0.5/cluster. Historical correlations are frozen from training. If any instrument has unknown correlations, the cluster
cap conservatively applies to the entire portfolio, without hedge credit. A permanent drawdown latch (default 0.1)
blocks the next decision after breach; `kill_switch=true` blocks all exposure.
Neither is intrabar loss protection. Requested budgets, capped targets, reasons,
costs, turnover, equity, drawdown, and complete state series remain auditable.

### CSV contract and limitations

Exact UTF-8 header (one row per instrument per synchronized bar):

```csv
symbol,open_time,close_time,open,high,low,close,series_kind
DEMO_A,2020-01-01T00:00:00+00:00,2020-01-01T00:59:00+00:00,100,102,99,101,normalized_research
```

Input must explicitly use `normalized_research`, with finite positive OHLC,
valid OHLC ordering, timezone-aware open/close timestamps, constant duration,
a fixed synchronized instrument universe and a regular grid. A close must precede
the next open. Duplicates, gaps, missing instruments and malformed/nonfinite values
fail the run before output. Bounds: 1-16 instruments, 200,000 rows, 32 MiB CSV,
prices `1e-9..1e12`, high/low at most 2 and next-open/previous-close `0.5..2`.
This structural validation does not certify economic data quality. Raw continuous
`KQ.*` symbols are rejected; relabeling such prices does not make them valid.
Real input requires a separately validated, point-in-time normalized series;
this CLI is not a direct importer of TqSdk or database CSV exports.

Macro and carry report `unavailable_pending_valid_input`: they await actual
publication/vintage timestamps and synchronized dated near/far contract prices
with expiries/costs, respectively. They are not fabricated or ruled out as future
strategies. No exchange sessions, trading-day calendar, settlement, multipliers,
lot rounding, margin, rolls, price limits, fill liquidity or market impact are
modeled. Synthetic data cannot establish profitability, and two experts cannot
establish broad diversification. No real-data coverage or backfill certification
is implied. Production futures research needs those missing inputs and models.

### Exporting a synchronized research input from PostgreSQL

`scripts\export_research_input.py` is a read-only bridge that produces the CSV
above directly from the `market_bars` PostgreSQL table, so the research CLI
itself stays a pure offline consumer with no settings, database, or credentials.
It issues SELECT statements only (via the existing `AK_DATABASE_URL`
configuration helpers), never writes to the database, imports no TqSdk, and
redacts credentials in every error path.

```powershell
python scripts\export_research_input.py --symbols KQ.m@SHFE.rb,KQ.m@CZCE.TA,KQ.m@DCE.m --duration 60 --output data\research_input_01\input.csv --config-out data\research_input_01\config.json
python scripts\research_multistrategy.py --csv data\research_input_01\input.csv --config data\research_input_01\config.json --output data\research_run_01
```

`--symbols auto` selects distinct main-continuous (`KQ.m@*`) symbols for the
requested duration, alphabetically, capped by `--max-instruments` (1..16,
default 3). The `KQ.m@` prefix is stripped deterministically (`KQ.m@SHFE.rb`
becomes `SHFE.rb`); any other `@`-bearing symbol (for example the `KQ.i@` index
series) is rejected, and two requested symbols that would map to the same name
are refused. Bars keep the database bar-open timestamp; `close_time` is derived
as open time plus `duration - 1` seconds, matching the convention above.

Because Chinese futures sessions leave gaps (day 09:00-10:15, 10:30-11:30,
13:30-15:00; night 21:00-23:00), the exporter exports **only one gap-free
synchronized segment**: the longest contiguous run in which every requested
instrument has a bar exactly `--duration` apart. It never fabricates bars and
never splices across a gap; when no run reaches the structural minimum, it fails
with the run boundaries and gap locations so the window can be narrowed.
The night session caps a one-minute synchronized run at 120 bars, so the
default `train_bars=300` can never fit real minute data; `--config-out` writes
the companion `{"train_bars": 100}` config (the schema floor, which requires at
least 101 frames) for the export at hand. Daily (`--duration 86400`) runs break
across weekends and holidays for the same reason and usually stay very short.
Output targets must not exist and are published atomically after the full
bundle passes the same strict validation the research CLI applies; guards cap
instruments (16), rows (200,000), and CSV size (32 MiB). This still only feeds
the normalized-price prototype: no sessions, settlement, multipliers, or margin
enter the exported data, and none of the prototype limitations above are lifted.

## Tests

```powershell
pytest
```

## Futures strategy library (first batch, 2026-09)

`ak_trading.futures` is a contract-level daily futures research library built
as three pure layers on top of a frozen spec (`futures/spec.py`):

- `data.py` — product/contract identity from the FUTURE universe cache
  (`ProductKey` normalizes `SHFE.rb` == `SHFE.RB`; stored symbols keep their
  raw case and are never upper()-ed), the `calendar_union_v1` common day
  sequence D with per-day hash/construction digests (any unknown day makes D
  unavailable — a partial D is never assembled), the raw observation grid
  used when no verified calendar exists, `past_liquidity_forward_roll`
  selection, near/far carry pair selection with two-observation
  confirmation, and the normalized execution-readiness gate table
  (§七 of the finalized plan) whose leaf verdicts are only `ready` or
  `blocked` with explicit missing/invalid field lists.
- `structures.py` — causal per-contract computations: daily_vol20 (21
  closes, sample std), ER20 regime labels (uncalibrated heuristic), entry/
  exit channels that exclude the current bar, regime-gated MAs that include
  it, z-score windows, and the ACT/365 `near_far_curve_ACT365` carry factor.
- `strategies.py` — four implementations (`donchian_trend`, `ma_trend`,
  `zscore_reversion`, `term_structure_carry`) plus the
  `legacy_dual_ma_reference` adapter. One entry point
  `evaluate(context, previous) -> StrategyResult`; `target_position` is a
  direction risk unit in {-1, 0, +1, None}; warmup/unavailable produce null
  targets with reasons, never zero.
- `portfolio.py` — admission before risk (warmup/unavailable requests are
  dropped with reasons; held positions get independent reduce-only close
  intents), fixed 3-family × 6-product slot budgets (trend variants split a
  slot), same-contract signed netting (different expiries never offset), a
  narrow wrapper over `research.apply_risk` (instrument 0.20 / gross 0.80 /
  cluster 0.50 / portfolio-vol cap, all six products as one conservative
  cluster) and toward-zero lot quantization with `below_minimum_lot`.
- `backtest.py` — in-memory ideal-price daily ledger: next-open execution
  with publication-delay handling, limit-band, shared capacity, margin-ratio
  and unknown-rule fail-closed semantics, FIFO lots with close_today/
  history fee legs, kill latch, insolvency, terminal close-only sessions,
  two-contract roll ledgers and exact cash/equity/fee/slip reconciliation.
- `validation.py` — fixed-252 rolling folds right-aligned to a 126-day
  final holdout (≥441 D days required), full splits traceability and
  metrics with explicit null semantics (zero-variance Sharpe, insolvent
  turnover, empty trade samples).

**Input schema** is `futures_bundle_v1` (see `futures/io.py`): `bars.csv`
with the raw market_bars fields plus a conservative `available_at` derived
from `imported_at`, `contracts.json`, `metadata.json` with graded sources,
`availability.json`, and a manifest published last with the complete
marker. Old normalized-research CSVs are rejected. Outputs are always
exclusive-create directories; canonical JSON is key-sorted, NaN-free, UTF-8
with LF.

**Real data status:** the read-only exporter
(`persistence/futures_research.py`, CLI `scripts/export_futures_input.py`)
opens exactly one connection, sets `SET TRANSACTION ISOLATION LEVEL
REPEATABLE READ READ ONLY` before the first business SELECT, verifies it
with `SHOW`, and pages by keyset. The frozen six-product export
(92,461 daily bars, 439 contracts, 2020-07-20..2026-09-05) lives in
`data/strategy_artifacts/futures-lib-20260907-codex-01/real-input-02`.
Its bars are observation-only: no verified historical session/calendar
evidence exists (D unavailable), pre-2026 rows carry no trading_day, and
multiplier/tick/fees/margin/limits have no approved historical evidence, so
strict execution gates stay blocked and `real-signals-01` reports null
targets with non-actionable candidate directions only.

**CLI** (`scripts/research_futures.py`): `--demo` and `--input-bundle` are
mutually exclusive and one is required; `--mode signals|backtest|readiness`;
`--validation smoke|walk-forward` (backtest only); `--config` for frozen
reproduction. Walk-forward performs actual independent 252-day training and
63-day OOS replays for retained folds, followed by a separate 126-day final
holdout. Parameters are fixed; no optimizer is run. Indicator history warms
without fills or scored marks, and account/position state resets per window.
Only development OOS enters the unit-index aggregate; any incomplete fold
nulls that portfolio's aggregate instead of being discarded. Final holdout
is explicitly marked consumed and is never pooled into development results.
Smoke runs publish a full backtest bundle
(`scenarios/{base,stress}/{portfolio_id}/` ledgers, baselines incl.
long_only_rolled_contract, cash and family standpoints, `legacy_reference/`
marked `comparable_to_futures: false`). Frozen-bundle replays are
INPUT-DRIVEN: bars and contracts are read from the supplied bundle and
rules/policy from the supplied config (unsupported config keys are rejected
with exit 2), and the reproduced payloads are byte-identical to the source
run. Exit codes: 0 completed, 2 usage/schema/conflict,
3 legitimate-but-blocked (missing inputs, empty real input, deferred real
strict backtest).

**Validation commands** (output directories must be new):

```powershell
python scripts/research_futures.py --demo --mode backtest --validation walk-forward --output data/strategy_runs/wf-demo
python scripts/research_futures.py --input-bundle data/strategy_runs/wf-demo/input --config data/strategy_runs/wf-demo/config.json --mode backtest --validation walk-forward --output data/strategy_runs/wf-replay
python scripts/research_futures.py --input-bundle data/strategy_artifacts/futures-lib-20260907-codex-01/real-input-02 --mode readiness --output data/strategy_runs/real-readiness
```

Walk-forward output contains `development/fold-NN/train`, `development/fold-NN/oos`
and `final_holdout`, each with independent scenario/portfolio ledgers. The root
manifest binds all payloads plus both the original caller input and republished
input manifests. Full contract instance IDs remain distinct from raw symbols.
Nested unknown config fields are rejected; accepted risk-policy fields are
applied and recorded in `effective_policy`.

Readiness is a frozen-input inventory, not historical event-time certification.
It reports missing rows, clocks, field evidence and blocked actions without
creating accounts or enabling strict trading. In the retained input there are
439 declared contracts but only 438 with bars; 81,313 rows lack a trading day,
268 have availability after the request's `as_of`, and no historical session
open/close evidence is present. `--mode readiness` writes only diagnostic
report/manifest files; publication success does not mean execution readiness.

**Retrospective real-price study** (no account execution):

```powershell
python scripts/study_futures_signals.py --input-bundle data/strategy_artifacts/futures-lib-20260907-codex-01/real-input-02 --output data/strategy_runs/real-price-study
```

This separate CLI selects at most three concrete contracts per fixed product,
with at least 120 stored observations, ordered by reference expiry and full
instance ID. It is a coverage-convenience sample with survival/coverage bias.
Four fixed observation-only strategies produce independent candidate events;
carry alone retains prefix-local pair confirmation. Prices, volumes and far
legs use only the current raw-timestamp prefix. No executable target is emitted.

For each signal at index `i`, horizons 1/5/20 use `close[i+h]/open[i+1]-1` within
one contract. Decision, reference entry and terminal must all fit the same
per-contract 60/20/20 observation partition. Invalid/interior missing values
are retained and invalidate labels rather than compressing time. Statistics
compare nonzero ready candidates with a buy-direction baseline on exactly the
same events; flat/unavailable/unmatured cases remain counted. Horizons are
observations, not certified trading days; overlapping labels are dependent.
These are descriptive price associations without costs, fills or portfolio
returns. Used holdout observations are marked consumed, and real strict
execution remains disabled. Outputs `sample.json`, `signals.csv`,
`outcomes.csv`, `summary.json`, `report.json`, `config.json` and a final hash
manifest are deterministic and must be written to a new directory.

**Deferred:** real strict backtests (AC10: requires future authorization
plus per-contract session/fee/margin/limit evidence), member-rank factors
(not requested), options families, runtime/TqSdk hosting. See
`data/strategy_artifacts/futures-lib-20260907-codex-01/` for the staged
handover chain (handover-1/2/3.json) and test evidence.
