"""Drift fixture base revision.

``_helper`` is intentionally outside the allowlist: editing only its body must
surface as input-hash drift (same-version proof refused) without any
tracked-symbol drift.
"""


def _helper(value: int) -> int:
    """Helper."""
    return value + 1


def public_api(value: int, factor: int = 2) -> int:
    """Original docstring."""
    return value * factor


class Kit:
    """Fixture class."""

    label: str = "kit"

    def render(self) -> str:
        """Render."""
        return self.label
