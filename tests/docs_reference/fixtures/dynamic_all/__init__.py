"""Counterexample: a non-literal (computed) __all__.

``__all__`` is built by concatenation, so its value cannot be known without
executing code. The generator must refuse this with a diagnostic instead of
simulating the execution or scanning the repository.
"""

_BASE = ["public_fn"]

__all__ = _BASE + ["extra"]
