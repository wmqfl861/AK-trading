"""Fixture implementation module for the pkg_safe package fixture."""

PUBLIC_CONST = 41 + 1


def public_fn(value: int, factor: int = 1) -> int:
    """Fixture public function."""
    return value * factor


alias_fn = public_fn
