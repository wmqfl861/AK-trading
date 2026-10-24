# Example Map

Use this file when you need a concrete official SDK repository example or doc page before writing or revising an answer. `doc/...`, `design/...`, and `tqsdk/...` paths are relative to the official [shinnytech/tqsdk-python](https://github.com/shinnytech/tqsdk-python) source tree; do not assume they exist in the current project or every installed wheel. Resolve the installed package with `importlib.util.find_spec("tqsdk")` and check a path before reading it.

For current version notes, installation, authentication, and the verified 3.10.3 wheel versus online-document boundary, use [install-auth-and-validation.md](install-auth-and-validation.md). This skill is a local maintained merge; the checked official repository `.skills` URL returned HTTP 404, so no official skill bundle was downloaded or claimed identical.

## If The User Asks About `wait_update`

- `doc/usage/framework.rst`
  - core update-loop explanation
- `doc/advanced/for_vnpy_user.rst`
  - practical `wait_update` and `is_changing` framing for strategy users
- `doc/usage/jupyter.rst`
  - notebook caveats and `deadline`

## If The User Asks About Market Data

- `tqsdk/demo/tutorial/t10.py`
  - minimal quote read
- `tqsdk/demo/tutorial/t30.py`
  - quote, tick, and K-line updates with `is_changing`
- `tqsdk/demo/tutorial/underlying_symbol.py`
  - main-contract `underlying_symbol`
- `doc/usage/mddatas.rst`
  - broader market-data usage patterns

## If The User Asks About Funds, Positions, Orders, Or Trades

- `tqsdk/demo/tutorial/t40.py`
  - account, position, and order state in the update loop
- `tqsdk/demo/download_orders.py`
  - export orders and trades
- `tqsdk/tradeable/mixin.py`
  - account-object getter patterns

## If The User Asks About Manual Orders

- `tqsdk/demo/tutorial/t41.py`
  - open then close with manual orders
- `tqsdk/demo/tutorial/t60.py`
  - strategy using `insert_order`
- `tqsdk/api.py`
  - exact `insert_order` and `cancel_order` semantics

## If The User Asks About `TargetPosTask` Or Scheduled Execution

- `tqsdk/demo/tutorial/t70.py`
  - basic `TargetPosTask` strategy
- `doc/usage/targetpostask.rst`
  - `TargetPosTask` rules and caveats, including `support_open_min_volume`
- `doc/advanced/targetpostask2.rst`
  - `cancel()` and `is_finished()`
- `doc/advanced/scheduler.rst`
  - `TargetPosScheduler`
- `tqsdk/algorithm/twap.py`
  - direct `Twap` behavior, including `support_open_min_volume`
- `design/target_pos_task_support_open_min_limit.md`
  - design notes for contracts with minimum opening volume rules
- `tqsdk/test/target_pos_task/test_open_min_volume.py`
  - regression cases for `support_open_min_volume`

## If The User Asks About Margin Trials Or `TqScenario`

- `tqsdk/scenario/tqscenario.py`
  - public `TqScenario` docstrings and method semantics
- `design/scenario.md`
  - scenario design, margin-rate source rules, and discount-model notes
- `tqsdk/test/scenario/test_scenario.py`
  - runnable what-if patterns for margin and risk changes
- `tqsdk/test/scenario/test_pre_insert_order.py`
  - real-account margin-rate lookup examples and expected values
- `doc/reference/tqsdk.scenario.rst`
  - public API reference entry

## If The User Asks About Account Types Or Multi-Account

- `doc/usage/shinny_account.rst`
  - Quick account versus real account login
- `tqsdk/demo/multiaccount.py`
  - one API with multiple accounts
- `doc/reference/tqsdk.multiaccount.rst`
  - `TqMultiAccount`
- `doc/reference/tqsdk.tqkq.rst`
  - `TqKq` and `TqKqStock`
- `doc/reference/tqsdk.sim.rst`
  - `TqSim` and `TqSimStock`
- `doc/advanced/tq_trading_unit.rst`
  - stopped-support notice for legacy `TqTradingUnit`: do not use in new strategies; instantiation emits `UserWarning`; former multi-strategy workflows are historical only

## If The User Asks About Backtest

- `tqsdk/demo/tutorial/backtest.py`
  - futures backtest setup
- `doc/usage/backtest.rst`
  - backtest behavior, wait-update progression, stock backtest notes

## If The User Asks About Common Errors

- `tqsdk/lib/utils.py`
  - parameter validation errors
- `tqsdk/lib/target_pos_task.py`
  - `TargetPosTask` misuse and lifecycle errors
- `tqsdk/tools/downloader.py`
  - download permissions and parameter errors
- `tqsdk/tradeable/mixin.py`
  - "未初始化 TqApi" style errors
