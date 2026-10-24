# Installation, Authentication, And Offline Validation

Read this reference for SDK upgrades, interpreter mismatches, authentication inputs, application states such as `WAITING_AUTH`, and validation that must avoid network connections or credentials.

## Verified Version And Sources

Verified on 2026-10-24 UTC against TqSdk **3.10.3**. The release is dated 2026/10/16 in the official version history. The wheel declares Python **>=3.9**; inspect its `METADATA` for direct dependency constraints and use the selected environment's dependency resolver and `pip check` for actual compatibility.

- [Official version history](https://doc.shinnytech.com/tqsdk/latest/version.html): release intent and documented behavior changes.
- [PyPI 3.10.3](https://pypi.org/project/tqsdk/3.10.3/): distribution version, download links, and file hashes.
- [Official source repository](https://github.com/shinnytech/tqsdk-python): source paths cited by the other references. A moving repository or `latest` documentation page may describe a different revision; inspect the installed package when exact API availability matters.
- [TqTradingUnit stopped-support notice](https://doc.shinnytech.com/tqsdk/latest/advanced/tq_trading_unit.html): the feature has stopped support. Do not use it in new strategies. The class is retained for existing strategies, and instantiation emits `UserWarning`.

The verified `tqsdk-3.10.3-py3-none-any.whl` has SHA-256 `860a37dc9ebb4c2d5ef585981654731ca649e371b50b8c2067de5371d8c13058`, matching the PyPI file listing. Its `METADATA`, `RECORD`, and actual imports/signatures are package evidence; those checks do not prove a trading or authentication regression has passed. The official repository `.skills` URL checked during this maintenance returned HTTP 404. This update merges the existing local skill; it is not a downloaded official skill bundle.

Relevant release changes, as stated by the official version history:

| Version | Changes relevant to this skill |
| --- | --- |
| 3.10.3 | Fixes a possible `TargetPosTask` stall when canceling a local order before it has an exchange order identifier; reports the MAC address of the interface used by the current trading connection; documentation updates. |
| 3.10.2 | Adds SHFE/INE combination-arbitrage instructions to `insert_order`; adds `price_decs` to `query_symbol_info` output; adds ETF option underlyings SSE.588000, SSE.588080, and SZSE.159901 to `query_all_level_finance_options`. |
| 3.10.1 | Adds local empty account/password validation to `TqAuth`. This does not make upgrading a remedy for an application's missing input values. |

For combination instructions or other specialized behavior, inspect the public API for the installed version and the relevant official documentation before writing an example. Basic single-contract examples elsewhere in this skill do not enumerate every supported instruction or metadata field.

## Select An Interpreter Before Installing

Use an explicit interpreter and a versioned ordinary venv. Do not silently change PATH, the default Python, a project's lock/pin, or an environment currently used by other work. Check that a proposed new directory does not already exist before creating it. Install from the official PyPI index or a verified official wheel; let required dependencies resolve inside the new venv. Do not add the discontinued `tqsdk-zq` service for a public class import/signature check.

Local maintenance example in PowerShell:

```powershell
& 'D:/Programs/Python/Python312/python.exe' -I -X utf8 -m venv 'D:/Tools/tqsdk-3.10.3'
& 'D:/Tools/tqsdk-3.10.3/Scripts/python.exe' -I -X utf8 -m pip --isolated install --disable-pip-version-check --index-url https://pypi.org/simple tqsdk==3.10.3
& 'D:/Tools/tqsdk-3.10.3/Scripts/python.exe' -I -X utf8 -m pip --isolated check
```

The maintenance itself reused the already downloaded, hash-verified wheel rather than downloading it again. Keep `include-system-site-packages = false` in `pyvenv.cfg`, save the actual installed versions and package sources, and check for import failures in addition to the dependency check. For a future version, create a new versioned directory and verify that version; the version above is the verified target, not a promise that it remains latest.

This machine's explicit entry point is `D:/Tools/tqsdk-3.10.3/Scripts/python.exe`. For an ordinary script that is compatible with this environment:

```powershell
& 'D:/Tools/tqsdk-3.10.3/Scripts/python.exe' -X utf8 'D:/path/to/your_script.py'
```

Local scope at this maintenance: the default Python retains TqSdk **3.10.2**, and AK-Trading's dependency files still pin **3.10.1**. The independent tool is updated; the project has not migrated. Do not use the new interpreter as an implicit replacement for the project's reviewed runtime. The pre-existing global `pip check` conflict between `ak-trading`'s pin and global TqSdk remains a separate recorded failure; the new venv must pass its own dependency check.

## Authentication And Application Input States

`TqAuth` identifies a Tianqin/Quick service user for data and permissions. It is separate from a broker account's login details. `TqSim` simulates local matching/account state; it does not remove authentication or connectivity requirements for market data and historical backtest data. `TqKq` is remote simulation; `TqAccount` is a real broker account. Choose `TqSim` for new local simulation examples unless the request needs a different account mode. Project-specific order authorization and review rules remain project rules, not general SDK requirements.

Do not automatically search for, read, load, or print `.env`, `TQ_USER`, `TQ_PASS`, or other secrets. A diagnostic that only prints whether a real secret exists still reads that input. Use placeholders or have the authorized application inject its inputs explicitly; offline validation needs no credentials. Respect each application's input-loading policy rather than prescribing dotenv for every project.

Separate these possible stages:

1. **Application input check.** For example, an application may emit `WAITING_AUTH` if either of two inherited strings is empty after `.strip()`. That status proves only that this process lacks at least one usable input. It does not establish machine-wide absence, an SDK login attempt, or a server rejection.
2. **SDK local validation.** `TqAuth` has local empty-input checks, present since 3.10.1. Keep any actual exception and identify its source before calling it a server login failure.
3. **Online authentication.** Server rejection or success requires evidence from an authorized online attempt. Package installation and imports cannot establish it.

Trace the application's documented launch and injection chain without inspecting secret values. A child normally inherits the environment of the process that launches it. Changing a different terminal or changing settings after a host starts does not update that already-running host's environment; a user-authorized host restart may be needed for new children to inherit new values. Upgrading TqSdk does not inject credentials or fix `WAITING_AUTH` by itself.

## Offline Checks And Their Limits

Use the intended interpreter in isolated mode for package checks. Version and source inspection needs no SDK connection:

```python
import sys
from importlib import metadata, util

print(sys.executable)
print(sys.prefix)
print(metadata.version('tqsdk'))
print(util.find_spec('tqsdk').origin)
```

Public import/signature inspection also avoids constructing an API or account:

```python
from inspect import signature
from tqsdk import TqApi, TqAuth, TqSim, TqTradingUnit

for entry in (TqApi, TqAuth, TqSim, TqTradingUnit):
    print(entry.__name__, signature(entry))
```

`TqTradingUnit` here is legacy identification only: do not instantiate it or use it in a new strategy. Distinguish the public class from optional stopped-support runtime services; preserve an actual traceback instead of swallowing every import failure as an optional skip.

Check Python example syntax with `ast.parse` or `compile`, check relative Markdown links, and compare synchronized skill file hashes. Compile examples without executing them. Resolve and validate dependency requirements in the selected interpreter; dependency checks and successful imports cover different failure modes.

Do not construct `TqApi(auth=None)` as an alleged universal offline test. Import, signature, syntax, and dependency checks establish only those facts. They do not validate live authentication, market data, historical downloads, backtest data access, broker connectivity, or order/cancel behavior. Those online and trading checks were **NOT_RUN** during this maintenance. Synthetic authentication counterexamples must use clearly labeled `SYNTHETIC` literals and must never inspect real process secret inputs.
