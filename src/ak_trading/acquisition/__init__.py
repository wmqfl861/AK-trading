"""W2 free-channel futures acquisition collector (development package).

Implements arbitration ACQ-EXEC-20260919-r1 (decision.md H01-H15, dev-plan.md
ACQ-W2-DEV-20260919-r1, execution-policy.json ACQ-EXEC-20260919-r1).

Placement note: dev-plan section 1 assigns module paths under
``src/ak_trading/acquisition/``; this delivery is self-contained inside the
owner's dispatch directory ``data/maintenance_artifacts/acquisition-w2-collector-20260919-r1/``
because the dispatching work order bounds all writes to that directory. Module
names and semantics follow dev-plan; relocation into the source tree is a
mechanical move reserved for the coordinator after independent review.

Runtime status: DEVELOPMENT_ONLY. No real TqApi connection, no production
database access, no capture has been performed by this package (NOT_RUN).
"""

from __future__ import annotations

PACKAGE_VERSION = "acq-w2-collector-20260919-r1"

__all__ = ["PACKAGE_VERSION"]
