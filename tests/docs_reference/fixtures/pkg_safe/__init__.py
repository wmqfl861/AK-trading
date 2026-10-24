"""Fixture package with a literal __all__ and allowlisted re-exports.

Happy-path fixture for tests/docs_reference: the package __all__ resolves into
the impl module inside the same allowlist, including one static alias.
"""

from .impl import PUBLIC_CONST, alias_fn, public_fn

__all__ = ["PUBLIC_CONST", "alias_fn", "public_fn"]
