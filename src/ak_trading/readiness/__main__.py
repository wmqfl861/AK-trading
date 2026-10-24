"""CLI entry: ``python -m ak_trading.readiness``.

Delegates to :func:`ak_trading.readiness.checker.main`. Using the package as
the ``-m`` target avoids re-executing ``checker.py`` after ``__init__`` has
already imported it.
"""

from __future__ import annotations

from .checker import main

if __name__ == "__main__":
    raise SystemExit(main())
