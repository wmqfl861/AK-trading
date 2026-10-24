# Account Type Matrix

## Use This Reference For

- Choosing the right account mode before writing code
- Explaining the difference among `TqAccount`, `TqKq`, `TqKqStock`, `TqSim`, `TqSimStock`, legacy `TqTradingUnit` (stopped support; not for new strategies), enterprise OTG accounts, and `TqMultiAccount`
- Answering "which account class should I use?"

## Quick Selection

Use the smallest account type that satisfies the request.

| Account type | Use when | Futures or stock | Needs `TqAuth` | Notes |
| --- | --- | --- | --- | --- |
| `TqApi(auth=TqAuth(...))` | Data access or disposable examples | Defaults to futures-style local sim | Yes | If no explicit account is given, TqSdk creates `TqSim()` |
| `TqSim()` | Local simulation, strategy development, futures backtest | Futures | Usually yes for data | Local simulated account |
| `TqKq()` | Quick simulated trading that should match Quick clients | Futures | Yes | Remote simulated account in the Quick ecosystem |
| `TqAccount(...)` | Broker live trading | Futures | Yes | Real trading account |
| `TqTradingUnit(...)` | Legacy compatibility only; support has stopped | Futures | Yes | Do not use in new strategies; instantiation emits `UserWarning` |
| `TqSimStock()` | Local stock simulation or stock backtest | Stock | Usually yes for data | Stock-only simulated account |
| `TqKqStock()` | Quick stock simulation | Stock | Yes | Remote stock sim |
| `TqMultiAccount([...])` | One API driving multiple accounts | Mixed | Yes | Pass `account=` for account-sensitive operations |

## Other Supported Real-Account Classes

These are valid when the user explicitly asks about them or already uses them:

- `TqCtp`
- `TqRohon`
- `TqJees`
- `TqYida`
- `TqZq`

Do not lead with these unless the user needs that exact connectivity.

## Important Distinctions

### `TqSim` vs `TqKq`

- `TqSim` is local and disposable.
- `TqKq` is Quick simulation tied to the user's Quick account.
- If the user expects account data to appear in official Quick clients, use `TqKq`, not `TqSim`.

### `TqSimStock` vs `TqKqStock`

- Both are stock-mode accounts.
- Stock accounts return `SecurityAccount`, `SecurityPosition`, `SecurityOrder`, `SecurityTrade`.
- Stock trading does not use `offset`.

### `TqAccount` vs `TqApi(auth=...)`

- `TqApi(auth=...)` alone is not a real broker account.
- It is enough for data questions and defaults to a local simulated account.
- Use `TqAccount(...)` only when the user wants live trading through a supported broker.

### Legacy `TqTradingUnit`: Support Has Stopped

- Do not use `TqTradingUnit` in new strategies. The class is retained only so existing strategies do not immediately break after upgrading; instantiation emits `UserWarning`.
- Historically it represented front accounts backed by a local multi-strategy system managed by `tqsdk-zq`. This history is not a recommendation to install or update that stopped-support service.
- For an existing strategy, consult the official stopped-support notice in `doc/advanced/tq_trading_unit.rst` and [install-auth-and-validation.md](install-auth-and-validation.md). Import/signature inspection does not validate or start the old service.

### `TqMultiAccount`

Use this when one strategy must operate more than one account at once.

Rules:

- pass `account=` to `insert_order`, `cancel_order`, `get_account`, `get_position`, `get_order`, `get_trade`, and similar account-sensitive APIs
- or use `account_obj.get_account()`, `account_obj.get_position()`, `account_obj.get_order()`, `account_obj.get_trade()`
- if the user forgets this, they will hit the common "多账户模式下, 需要指定账户实例 account" error

## Selection Advice To Reuse

- Data only: `TqApi(auth=TqAuth(...))`
- Futures local sim: `TqSim()`
- Futures Quick sim: `TqKq()`
- Stock local sim or stock backtest: `TqSimStock()`
- Stock Quick sim: `TqKqStock()`
- Broker live trading: `TqAccount(...)`
- Legacy multi-strategy front account: `TqTradingUnit(...)` is compatibility-only; do not use in new strategies (`UserWarning` on instantiation)
- One API, many accounts: `TqMultiAccount([...])`

## Repository Sources

- `tqsdk/__init__.py`
- `tqsdk/tradeable/__init__.py`
- `tqsdk/tradeable/otg/tqtradingunit.py`
- `doc/advanced/tq_trading_unit.rst`
- `doc/usage/framework.rst`
- `doc/usage/shinny_account.rst`
- `doc/reference/tqsdk.tqkq.rst`
- `doc/reference/tqsdk.sim.rst`
- `tqsdk/api.py`
