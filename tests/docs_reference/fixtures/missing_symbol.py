"""Counterexample: the allowlist asks for a symbol this module does not define.

The generator must fail with symbol_missing and list the names that ARE
present, so a human can adjudicate the whitelist correction. It must never
guess a near-miss name.
"""


def alpha() -> None:
    """Doc."""


def beta() -> None:
    """Doc."""
