"""Read-only progress and identity consistency checker (isolated tool).

Plan identifier: ``COORDINATION-READINESS-REPORT-r1``
(Codex project review report r1, section C6).

Small, isolated, read-only tool. It consumes explicitly passed JSON inputs
(register snapshot, confirmed identity table, confirmed gate table, file
reference table) and writes ``state-snapshot.json`` + ``contradictions.md``
into an explicit output directory. It never writes the register, never
modifies frozen packages, never starts processes, never samples, never issues
authorizations, and never changes gate states. See ``README.md`` for the input
schemas and usage.
"""

from __future__ import annotations

from .checker import (
    CHECK_IDS,
    CHECK_TITLES,
    CheckResult,
    ReadinessReport,
    build_parser,
    main,
    render_contradictions_markdown,
    render_state_snapshot,
    run_checks,
    write_report,
)
from .models import (
    PLAN_ID,
    SCHEMA_VERSION,
    TOOL_NAME,
    Finding,
    Informational,
    ReadinessInputError,
    ReadinessInputs,
    load_inputs,
)

__all__ = [
    "CHECK_IDS",
    "CHECK_TITLES",
    "CheckResult",
    "Finding",
    "Informational",
    "PLAN_ID",
    "ReadinessInputError",
    "ReadinessInputs",
    "ReadinessReport",
    "SCHEMA_VERSION",
    "TOOL_NAME",
    "build_parser",
    "load_inputs",
    "main",
    "render_contradictions_markdown",
    "render_state_snapshot",
    "run_checks",
    "write_report",
]
