"""Counterexample: defaults/annotations that LOOK like side-effect calls.

The static build must record these expressions as source text only. Calling
``boom()`` would raise; a build that evaluates defaults or annotations fails
loudly instead of silently producing a value.
"""


def boom() -> None:
    raise RuntimeError("side effect executed")


def api(value: int = boom() or 7, tag=boom() or "t") -> "boom() and 0":
    """Docstring for api."""
    return value
