"""Counterexample: package whose __all__ imports outside the allowlist.

``broken_pkg.unlisted`` is not (and must not be) an allowlisted entry; the
bounded re-export rule has to reject this instead of scanning the repository
for the name.
"""

from .unlisted import thing

__all__ = ["thing"]
