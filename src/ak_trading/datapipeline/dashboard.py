"""DP06 offline-browsable HTML dashboard (H06.1, H06.7, H06.8).

Renders the SAME report facts the JSON and Markdown artifacts render, so
all three formats carry identical numbers (V11).  The page is a single
self-contained HTML document:

- offline: no external scripts, styles, fonts or network references of any
  kind (checked by tests);
- escaped: every symbol, cause, evidence and error string passes through
  ``html.escape`` before insertion (H06.8);
- read-only observability: no trading buttons, no "permit trading" state,
  no order actions anywhere (H06.7 / D05); the page is market-data
  monitoring only.

UNKNOWN and NOT_RUN render with explicit neutral styling and text -- never
green, never 0%/100% (Q07, H06.3).

Import hygiene: importing this module performs no I/O.
"""

from __future__ import annotations

import html
from typing import Any, List, Mapping, Sequence

from .monitoring import MonitoringError

__all__ = ["render_html", "DASHBOARD_SCHEMA_VERSION"]

DASHBOARD_SCHEMA_VERSION = "dp06-dashboard/1"

#: Terms that must never appear as actionable controls on the page (H06.7).
#: Rendered text is checked against the action-button vocabulary, not the
#: whole document, so plain-language statements about the boundary remain
#: legal.
_FORBIDDEN_CONTROL_MARKERS: Sequence[str] = (
    "insert_order",
    "cancel_order",
    "target_postask",
    "enable_trading",
    "permit_trading",
    "place-order",
    "order-button",
)

_CSS = """
body { font-family: Consolas, 'Courier New', monospace; margin: 2rem; color: #1a1a1a; background: #fafafa; }
h1 { font-size: 1.3rem; } h2 { font-size: 1.05rem; margin-top: 1.6rem; }
table { border-collapse: collapse; margin-top: 0.6rem; }
th, td { border: 1px solid #bbb; padding: 0.25rem 0.55rem; font-size: 0.85rem; text-align: left; }
th { background: #eee; }
td.unknown, td.not_run { background: #e8e8e8; color: #555; }
td.ok { background: #e2efda; } td.stale { background: #fff2cc; }
td.degraded { background: #fde9d9; } td.failing { background: #f4cccc; }
.meta { color: #444; font-size: 0.8rem; }
.footer { margin-top: 2rem; font-size: 0.75rem; color: #666; border-top: 1px solid #ccc; padding-top: 0.6rem; }
.synthetic { display: inline-block; background: #7a3e9d; color: white; padding: 0.1rem 0.5rem; border-radius: 3px; font-weight: bold; }
"""


def _e(value: Any) -> str:
    """Escape any value for HTML text content (H06.8)."""
    return html.escape("" if value is None else str(value), quote=True)


def _cell(value: Any, css_class: str = "") -> str:
    if value is None:
        return f'<td class="unknown">null</td>'
    return f'<td class="{css_class}">{_e(value)}</td>' if css_class else f"<td>{_e(value)}</td>"


def render_html(facts: Mapping[str, Any]) -> str:
    """Render the deterministic offline dashboard for one report facts doc."""
    coverage = facts["coverage"]
    known = coverage["known_scope_aggregate"]
    overall_state = coverage["overall_coverage_state"]
    overall_check = coverage["overall_check_execution"]
    data_origin = str(facts.get("data_origin", "SYNTHETIC"))

    parts: List[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append('<html lang="en"><head><meta charset="utf-8">')
    parts.append(f"<title>DP06 dashboard {_e(facts['report_id'])}</title>")
    parts.append(f"<style>{_CSS}</style></head><body>")
    parts.append("<h1>Datapipeline monitoring dashboard</h1>")
    if data_origin in ("SYNTHETIC", "SCENARIO"):
        parts.append(f'<p><span class="synthetic">{_e(data_origin)}</span> '
                     "fixtures and reports are engineering evidence, never historical market fact.</p>")

    # --- meta / binding (H06.5) ------------------------------------------
    parts.append('<h2>Report binding</h2>')
    parts.append('<table><tr><th>field</th><th>value</th></tr>')
    for label, value in (
        ("report_id", facts["report_id"]),
        ("monitor", facts["monitor_id"]),
        ("as_of", f"{facts['as_of_iso']} ({facts['as_of_nano']} ns)"),
        ("wall_clock_used", facts["wall_clock_used"]),
    ):
        parts.append(f"<tr><th>{_e(label)}</th><td>{_e(value)}</td></tr>")
    for name, value in facts["evidence_hashes"].items():
        parts.append(f"<tr><th>hash {_e(name)}</th><td>{_e(value if value is not None else 'UNKNOWN')}</td></tr>")
    parts.append("</table>")

    # --- coverage (Q07: unknown/not_run never green, no 0/100%) ----------
    parts.append('<h2>Coverage</h2>')
    parts.append('<table><tr><th>aspect</th><th>value</th></tr>')
    parts.append(
        f"<tr><th>overall check_execution</th><td class=\"{'not_run' if overall_check != 'COMPLETED' else ''}\">"
        f"{_e(overall_check)}</td></tr>"
    )
    overall_css = {"COMPLETE": "ok", "PARTIAL": "degraded", "BLOCKED": "failing"}.get(str(overall_state), "unknown")
    parts.append(
        f"<tr><th>overall coverage_state</th><td class=\"{overall_css}\">"
        f"{_e(overall_state if overall_state is not None else 'null')}</td></tr>"
    )
    parts.append(
        f"<tr><th>scopes</th><td>{_e(coverage['scope_count'])} total / "
        f"{_e(coverage['completed_scope_count'])} COMPLETED / "
        f"{_e(coverage['not_run_scope_count'])} NOT_RUN</td></tr>"
    )
    if overall_check != "COMPLETED":
        parts.append('<tr><th>overall percentage</th><td class="not_run">not rendered (overall check did not COMPLETED; partial checks never display 0%/100%)</td></tr>')
    pct = known["known_subset_valid_closed_pct"]
    pct_text = "n/a (unknown denominator)" if pct is None else f"{pct}% (KNOWN SUBSET)"
    parts.append(
        f"<tr><th>known-subset percentage</th><td>{_e(pct_text)}<br>"
        f"<span class=\"meta\">{_e(known['label'])}</span></td></tr>"
    )
    parts.append("</table>")

    # --- alerts -----------------------------------------------------------
    summary = facts["alert_summary"]
    parts.append('<h2>Alerts (local events only; no outbound delivery)</h2>')
    parts.append(
        f"<p>active (OPEN+ACKNOWLEDGED): {_e(summary['active'])} "
        f"(CRITICAL={_e(summary['by_severity']['CRITICAL'])}, "
        f"WARNING={_e(summary['by_severity']['WARNING'])}, "
        f"INFO={_e(summary['by_severity']['INFO'])}); "
        f"resolved: {_e(summary['by_state']['RESOLVED'])}</p>"
    )
    if facts["alerts"]:
        parts.append("<table><tr><th>key</th><th>reason</th><th>severity</th><th>status</th>"
                     "<th>first_raised</th><th>last_raised</th><th>occurrences</th><th>suppressed</th><th>cause</th></tr>")
        for key, record in facts["alerts"].items():
            css = "failing" if record["severity"] == "CRITICAL" and record["status"] in ("OPEN", "ACKNOWLEDGED") else ""
            parts.append(
                "<tr>"
                + _cell(key)
                + _cell(record["reason"])
                + _cell(record["severity"], css)
                + _cell(record["status"])
                + _cell(record["first_raised_nano"])
                + _cell(record["last_raised_nano"])
                + _cell(record["occurrence_count"])
                + _cell(record["suppressed_count"])
                + _cell(record["cause"])
                + "</tr>"
            )
        parts.append("</table>")

    # --- per symbol x period matrix (H06.1) -------------------------------
    parts.append("<h2>Tasks: symbol &times; period</h2>")
    parts.append(
        "<table><tr><th>symbol</th><th>kind</th><th>period_s</th><th>state</th><th>health</th>"
        "<th>target window [start,end)</th><th>last_observed</th><th>last_commit</th>"
        "<th>latency_s</th><th>queued</th><th>retry_max</th><th>closed</th><th>conflicts</th>"
        "<th>missing (in-window)</th><th>outside-window</th><th>cal-unknown ranges</th></tr>"
    )
    health_css = {"OK": "ok", "STALE": "stale", "DEGRADED": "degraded", "FAILING": "failing", "UNKNOWN": "unknown"}
    for row in facts["tasks"]:
        window = row["target_window"]
        window_text = f"{window['start_nano'] or '-'},{window['end_nano'] or '-'}"
        parts.append(
            "<tr>"
            + _cell(row["source_symbol"])
            + _cell(row["instrument_kind"])
            + _cell(row["duration_seconds"])
            + _cell(row["acquisition_state"])
            + _cell(row["health"], health_css.get(row["health"], "unknown"))
            + _cell(window_text)
            + _cell(row["last_observed_nano"] or "-")
            + _cell(row["last_commit_nano"] or "-")
            + _cell(row["latency_seconds"] if row["latency_seconds"] is not None else "null")
            + _cell(row["queued_events"])
            + _cell(row["retry_count_max"])
            + _cell(row["row_closed"])
            + _cell(row["conflicts"])
            + _cell(row["gap_missing_in_window"])
            + _cell(row["gap_outside_window_events"])
            + _cell(row["calendar_unknown_ranges"])
            + "</tr>"
        )
    parts.append("</table>")

    # --- health reasons (explicit causes; UNKNOWN stays visible) ---------
    parts.append("<h2>Health reasons</h2>")
    parts.append("<table><tr><th>task_key</th><th>health</th><th>reasons</th></tr>")
    for row in facts["tasks"]:
        reasons = "; ".join(row["health_reasons"]) or "-"
        parts.append(
            "<tr>" + _cell(row["task_key"]) + _cell(row["health"], health_css.get(row["health"], "unknown")) + _cell(reasons) + "</tr>"
        )
    parts.append("</table>")

    # --- consistency block (V11: identical numbers in JSON/MD/HTML) ------
    parts.append('<h2 id="consistency">Consistency metrics</h2>')
    parts.append('<dl>')
    for name, value in facts["consistency_metrics"].items():
        parts.append(f"<dt>{_e(name)}</dt><dd>{_e(value)}</dd>")
    parts.append("</dl>")

    # --- drill-down (non-secret evidence refs) ---------------------------
    parts.append("<h2>Drill-down evidence sources</h2>")
    parts.append("<ul>")
    for source in facts["source_query_scope"]["sources_read"]:
        digest = source["sha256"] or "n/a"
        parts.append(f"<li><code>{_e(source['path'])}</code> <span class=\"meta\">sha256 {_e(digest)}</span></li>")
    parts.append("</ul>")

    parts.append(
        '<p class="footer">Offline document: no external resources, no scripts, no trading controls. '
        "Alerts are local events only (H06.7); ACK changes presentation, never data quality (H06.4). "
        f"dashboard schema {_e(DASHBOARD_SCHEMA_VERSION)}.</p>"
    )
    parts.append("</body></html>")
    document = "\n".join(parts) + "\n"

    _assert_no_forbidden_controls(document)
    return document


def _assert_no_forbidden_controls(document: str) -> None:
    """Guard: no trading-surface control markers in button/action form."""
    lowered = document.lower()
    for marker in _FORBIDDEN_CONTROL_MARKERS:
        if marker in lowered:
            raise MonitoringError(
                "E_DP06_TRADING_SURFACE_IN_DASHBOARD",
                f"dashboard contains forbidden control marker {marker!r}",
            )
