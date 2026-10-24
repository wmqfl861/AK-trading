---
name: tqsdk-trading-and-data
description: "Explain, implement, or debug TqSdk / 天勤量化 Python workflows: installation and upgrades, TqAuth authentication, wait_update and is_changing loops, market or historical data, account selection, funds/positions/orders/trades, TargetPosTask and scheduled execution, TqScenario margin and risk trials, simulation, backtest, and SDK errors. Use for TqApi, TqSim, TqKq, TqAccount, DataDownloader, 行情、K线、保证金、持仓、下单、撤单、调仓 or WAITING_AUTH diagnosis in a TqSdk application. Recognize TqTradingUnit only for legacy compatibility; its multi-strategy feature is no longer supported."
---

# TqSdk Trading and Data

Use this skill to answer TqSdk questions with verified package APIs and official documentation. Verified against TqSdk 3.10.3 on 2026-10-24 UTC. Prefer minimal runnable snippets, keep futures and stock behavior separate, and explain the update loop explicitly whenever the user's issue depends on `wait_update()`. Source paths such as `doc/...` and `tqsdk/...` below refer to the official SDK repository, not necessarily the user's project checkout.

## Route The Request First

Read only the references needed for the user's question.

For installation, version differences, authentication inputs, application states such as `WAITING_AUTH`, or offline validation, first read [references/install-auth-and-validation.md](references/install-auth-and-validation.md). It includes the verified 3.10.2/3.10.3 changes and the local versioned interpreter entry point.

1. Read [references/wait-update-and-update-loop.md](references/wait-update-and-update-loop.md) for `wait_update`, `is_changing`, `deadline`, async update notifications, Jupyter caveats, or backtest progression questions.
2. Read [references/market-data.md](references/market-data.md) for `get_quote`, `get_kline_serial`, `get_tick_serial`, contract discovery, symbol metadata, and `DataDownloader`.
3. Read [references/account-type-matrix.md](references/account-type-matrix.md) for `TqAccount`, `TqKq`, `TqKqStock`, `TqSim`, `TqSimStock`, legacy `TqTradingUnit` (stopped support; do not use in new strategies), OTG account classes, and `TqMultiAccount`.
4. Read [references/accounts-and-trading.md](references/accounts-and-trading.md) for account, position, order, and trade getters plus multi-account getter patterns.
5. Read [references/scenario-and-margin.md](references/scenario-and-margin.md) for `TqScenario`, real-account margin-rate lookup, margin occupancy calculation, risk-ratio what-if analysis, and limited built-in margin discount rules.
6. Read [references/order-functions-and-position-tools.md](references/order-functions-and-position-tools.md) for `insert_order`, `cancel_order`, `TargetPosTask`, `support_open_min_volume`, `TargetPosScheduler`, `Twap`, and advanced execution helpers.
7. Read [references/object-fields.md](references/object-fields.md) when the user asks what fields mean on `Quote`, K-line or tick rows, `Account`, `Position`, `Order`, `Trade`, or their stock variants.
8. Read [references/simulation-and-backtest.md](references/simulation-and-backtest.md) for local sim, Quick sim, stock sim, backtest, and cross-account backtest limits.
9. Read [references/error-faq.md](references/error-faq.md) when the user asks about common TqSdk failures, confusing behavior, or exception messages.
10. Read [references/example-map.md](references/example-map.md) when you want a repository-backed example or doc page to imitate.

## Before Writing Code

1. Identify the task mode first: market data only, local sim, Quick sim, real account, stock trading, futures trading, backtest, or margin trial.
2. Treat existing fixed contract/date examples as historical templates, not current-contract or historical-coverage evidence. If an example needs a current futures or option contract, verify it with `query_quotes(..., expired=False)`, `query_options(..., expired=False)`, or a main-contract symbol before using it.
3. Prefer main-contract symbols such as `KQ.m@SHFE.rb` for market-data and historical-download examples when the user does not need a tradable delivery month.
4. Use a concrete non-expired delivery-month contract for order, position, `TargetPosTask`, and margin examples.
5. Use placeholders or inputs explicitly injected by the authorized application. Do not automatically discover, read, load, or print `.env`, `TQ_USER`, `TQ_PASS`, or other credentials; even a presence check reads a real secret input.
6. Begin with offline imports, signatures, dependency checks, and syntax checks. A quote subscription or backtest data run uses online services and requires the corresponding authorized inputs; it is a separate validation step. Never construct `TqApi` merely to prove an offline import works.

## Core Rules

1. Treat `get_*` results as live references, not snapshots. Explain that they refresh during `wait_update()`.
2. Explain `wait_update()` whenever the user is confused by missing data, stale fields, orders not leaving the client, or `TargetPosTask` not acting.
3. Distinguish futures and stock workflows:
   - Futures accounts and objects use `Account`, `Position`, `Order`, `Trade`.
   - Stock accounts and objects use `SecurityAccount`, `SecurityPosition`, `SecurityOrder`, `SecurityTrade`.
   - Stock trading does not use `offset`, and `TargetPosTask` is not for stock trading.
4. Choose account type before writing code. Default new simulation examples to local `TqSim`; `TqKq` is remote simulation and `TqAccount` is a real broker account. Local matching does not make market data or backtest history retrieval offline. `TqTradingUnit` multi-strategy support has stopped: do not use it in new strategies; the class remains for legacy compatibility and instantiation emits `UserWarning`.
5. In multi-account mode, pass `account=` for getters and trading calls, or use the account object's own `get_account`, `get_position`, `get_order`, and `get_trade`.
6. For current market examples, avoid expired contracts. Prefer contract discovery APIs or main-contract symbols.
7. When the user asks for field meanings, explain the smallest relevant field set first and say whether the object is futures or stock.
8. When the user asks for long historical ranges, prefer `DataDownloader` over pretending `get_kline_serial` is an unlimited history API.
9. When the user asks for advanced execution, prefer public helpers first:
   - `TargetPosTask` for target net position.
   - `TargetPosTask(..., support_open_min_volume=True)` only for contracts with exchange minimum opening size rules when approximate completion is acceptable.
   - `TargetPosScheduler` plus `twap_table` or `vwap_table` from `tqsdk.algorithm` for scheduled execution.
   - `TargetPosScheduler(..., support_open_min_volume=True)` when the scheduled execution targets a contract with exchange minimum opening size rules.
   - Direct `tqsdk.algorithm.Twap(..., support_open_min_volume=True)` only when the user asks for the older/direct TWAP helper; note that it is not for backtest.
   - Mention `InsertOrderTask` and `InsertOrderUntilAllTradedTask` as internal or advanced helpers, not the default answer.
10. Use `TqScenario` for synchronous what-if margin and risk trials, not for live order placement. It is futures-only, single-account, and updates the trial snapshot immediately after each call.
11. Explain the margin-rate source in every `TqScenario` answer:
   - `account=None` or `TqSim()` uses `Quote` margin data.
   - `TqAccount(...)` or `TqKq()` queries account-specific rates synchronously and may fall back to `Quote` margin if lookup fails.
12. Treat margin discounts conservatively. Reuse one `TqScenario` object for step-by-step trial actions, and do not promise broker-specific preferential rules beyond the limited built-in rules modeled by TqSdk.
13. Preserve exchange-specific close semantics in `TqScenario`. For SHFE or INE futures, keep `CLOSE` versus `CLOSETODAY` consistent with the imported position snapshot.

## Answering Style

- Prefer imports from `tqsdk.__init__` for top-level APIs. When an API is documented under a submodule, use that official submodule path such as `tqsdk.tools` or `tqsdk.algorithm`.
- Prefer short, correct code blocks over broad pseudo-code.
- Name the exact API the user should call next.
- If behavior differs in live trading, Quick sim, local sim, stock sim, or backtest, say so explicitly.
- If the answer depends on a common pitfall, state the pitfall directly instead of burying it in examples.
- If the answer uses `TqScenario`, say what snapshot goes into `positions`, what balance goes into `init_balance`, and whether the result comes from quote margin or account-specific margin rates.
