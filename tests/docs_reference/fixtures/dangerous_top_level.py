"""Counterexample: importing this module would execute top-level side effects.

The static build must parse this file without ever importing or executing it.
If anything executes this module during a build, the sentinel file
``dangerous_top_level.py.EXECUTED`` appears next to it and the safety test
fails. No network access is performed by the fixture itself.
"""

SENTINEL_MARKER = "executes-on-import"

open(__file__ + ".EXECUTED", "w", encoding="utf-8").write("import-ran")


def guarded_api(x: int = 3) -> int:
    """Documented fixture function."""
    return x
