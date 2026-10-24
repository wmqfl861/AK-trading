"""Counterexample: alias cycle reachable from __all__.

``first`` and ``second`` alias each other; resolution must detect the cycle
and fail instead of looping or picking an arbitrary endpoint.
"""

first = second
second = first

__all__ = ["first", "second"]
