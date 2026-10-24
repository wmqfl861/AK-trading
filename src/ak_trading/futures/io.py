"""Canonical serialization, input-bundle publishing/reading and signals output.

Offline module (finalized-plan §六.10-11, §十三.7): no database imports, no
network, no wall-clock reads inside payloads. Deterministic payloads contain
no current timestamps, absolute output paths or random ids; ``created_at``
lives only in the run manifest and is passed explicitly by callers.
"""
from __future__ import annotations

import csv
import hashlib
import io as _io
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Mapping, Sequence

from ak_trading.futures.spec import ContractBar

__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "BARS_CSV_COLUMNS",
    "SIGNALS_CSV_COLUMNS",
    "canonical_json_bytes",
    "canonical_json_string",
    "sha256_bytes",
    "sha256_file",
    "OutputDirectoryError",
    "assert_output_directory_is_new",
    "bars_rows_from_bars",
    "bars_from_rows",
    "write_canonical_csv",
    "read_canonical_csv",
    "InputBundle",
    "publish_input_bundle",
    "read_input_bundle",
    "publish_signals_bundle",
]

BUNDLE_SCHEMA_VERSION = "futures_bundle_v1"

BARS_CSV_COLUMNS: tuple[str, ...] = (
    "symbol",
    "duration_seconds",
    "datetime_nano",
    "ts",
    "trading_day",
    "trading_day_source",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_oi",
    "close_oi",
    "source",
    "batch_id",
    "imported_at",
    # Derived conservative availability bound (pipeline import time).
    "available_at",
)

SIGNALS_CSV_COLUMNS: tuple[str, ...] = (
    "scenario",
    "strategy_id",
    "family",
    "exchange",
    "product",
    "contract_instance_id",
    "raw_symbol",
    "decision_at",
    "clock_status",
    "completed_bar_id",
    "target_position",
    "status",
    "reason",
    "diagnostics",
)


class OutputDirectoryError(RuntimeError):
    """Raised when an output directory exists or overlaps forbidden paths."""


def canonical_json_string(payload: object) -> str:
    """Stable-key, NaN-free JSON text with LF endings."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    )


def canonical_json_bytes(payload: object) -> bytes:
    text = canonical_json_string(payload)
    return (text + "\n").encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_output_directory_is_new(
    output: Path, forbidden_overlaps: Sequence[Path] = ()
) -> None:
    """Output must not exist and must not overlap inputs/sources/outputs."""
    if output.exists():
        raise OutputDirectoryError(f"output directory already exists: {output}")
    for other in forbidden_overlaps:
        if other == output or other in output.parents or output in other.parents:
            raise OutputDirectoryError(
                f"output directory overlaps existing path: {output} vs {other}"
            )


# ---------------------------------------------------------------------------
# CSV primitives (Decimal strings; null => empty field)
# ---------------------------------------------------------------------------


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        raise ValueError("booleans are never serialized as numbers")
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def write_canonical_csv(path: Path, columns: Sequence[str], rows: Sequence[Mapping[str, object]]) -> str:
    """Write UTF-8 LF CSV with a fixed column order; returns file sha256."""
    buffer = _io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(list(columns))
    for row in rows:
        writer.writerow([_cell(row.get(column)) for column in columns])
    payload = buffer.getvalue().encode("utf-8")
    path.write_bytes(payload)
    return sha256_bytes(payload)


def read_canonical_csv(path: Path, columns: Sequence[str]) -> list[dict[str, str]]:
    """Read back a canonical CSV; empty fields stay empty strings."""
    return _parse_canonical_csv(path.read_bytes(), columns, path.name)


def _parse_canonical_csv(payload: bytes, columns: Sequence[str], name: str) -> list[dict[str, str]]:
    text = payload.decode("utf-8")
    reader = csv.reader(_io.StringIO(text, newline=""))
    header = next(reader, None)
    if header != list(columns):
        raise ValueError(f"unexpected CSV header in {name}: {header!r}")
    rows: list[dict[str, str]] = []
    for raw in reader:
        if len(raw) != len(columns):
            raise ValueError(f"malformed CSV row in {name}: {raw!r}")
        rows.append(dict(zip(columns, (cell or None for cell in raw))))
    return rows


# ---------------------------------------------------------------------------
# Bars <-> rows
# ---------------------------------------------------------------------------


def bars_rows_from_bars(
    bars: Sequence[ContractBar], extra: Mapping[str, object] | None = None
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for bar in bars:
        row: dict[str, object] = {
            "symbol": bar.symbol,
            "duration_seconds": bar.duration_seconds,
            "datetime_nano": bar.datetime_nano,
            "ts": bar.ts,
            "trading_day": bar.trading_day,
            "trading_day_source": bar.trading_day_source,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "open_oi": bar.open_oi,
            "close_oi": bar.close_oi,
            "source": bar.source,
            "available_at": bar.available_at,
        }
        if extra:
            for key, value in extra.items():
                row.setdefault(key, value)
        rows.append(row)
    return rows


def _optional_decimal(text: str | None) -> Decimal | None:
    if text is None or text == "":
        return None
    return Decimal(text)


def _optional_datetime(text: str | None) -> datetime | None:
    if text is None or text == "":
        return None
    return datetime.fromisoformat(text)


def bars_from_rows(rows: Sequence[Mapping[str, object]]) -> list[ContractBar]:
    bars: list[ContractBar] = []
    for row in rows:
        trading_day = row.get("trading_day")
        bars.append(
            ContractBar(
                symbol=str(row["symbol"]),
                duration_seconds=int(row["duration_seconds"]),  # type: ignore[arg-type]
                datetime_nano=int(row["datetime_nano"]),  # type: ignore[arg-type]
                ts=_optional_datetime(row.get("ts")) or datetime.fromtimestamp(0),
                source=str(row.get("source") or ""),
                trading_day=date.fromisoformat(trading_day)
                if isinstance(trading_day, str) and trading_day
                else None,
                trading_day_source=str(row.get("trading_day_source") or ""),
                session_open=_optional_datetime(row.get("session_open")),
                session_close=_optional_datetime(row.get("session_close")),
                available_at=_optional_datetime(row.get("available_at")),
                open=_optional_decimal(row.get("open")),  # type: ignore[arg-type]
                high=_optional_decimal(row.get("high")),  # type: ignore[arg-type]
                low=_optional_decimal(row.get("low")),  # type: ignore[arg-type]
                close=_optional_decimal(row.get("close")),  # type: ignore[arg-type]
                volume=_optional_decimal(row.get("volume")),  # type: ignore[arg-type]
                open_oi=_optional_decimal(row.get("open_oi")),  # type: ignore[arg-type]
                close_oi=_optional_decimal(row.get("close_oi")),  # type: ignore[arg-type]
            )
        )
    return bars


# ---------------------------------------------------------------------------
# Input bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InputBundle:
    """In-memory read view of a published input bundle."""

    root: Path
    manifest: Mapping[str, object]
    bars: tuple[ContractBar, ...]
    contracts: tuple[Mapping[str, object], ...]
    metadata: Mapping[str, object]
    availability: Mapping[str, object]


def publish_input_bundle(
    output: Path,
    *,
    dataset_kind: str,
    created_at: str,
    bars_rows: Sequence[Mapping[str, object]],
    contracts: Sequence[Mapping[str, object]],
    metadata: Mapping[str, object],
    availability: Mapping[str, object],
    request_info: Mapping[str, object],
    source_hashes: Mapping[str, str],
    read_only_transaction: Mapping[str, object],
    clock_policy: Mapping[str, object],
    session_windows_rows: Sequence[Mapping[str, object]] | None = None,
    limits: Mapping[str, int] | None = None,
    forbidden_overlaps: Sequence[Path] = (),
    notes: Mapping[str, str] | None = None,
) -> Mapping[str, object]:
    """Publish a futures_bundle_v1 input bundle exclusively (§六.11).

    Payload files are written and hash-verified first; the manifest with the
    complete marker is written last. Any failure leaves an incomplete error
    marker and never a successful bundle.
    """
    assert_output_directory_is_new(output, forbidden_overlaps)
    output.mkdir(parents=True)

    content_hashes: dict[str, str] = {}

    def write_payload(name: str, payload: bytes) -> None:
        path = output / name
        path.write_bytes(payload)
        if sha256_file(path) != sha256_bytes(payload):
            raise RuntimeError(f"hash verification failed for {name}")

    bars_csv = output / "bars.csv"
    content_hashes["bars.csv"] = write_canonical_csv(
        bars_csv, BARS_CSV_COLUMNS, list(bars_rows)
    )
    write_payload("contracts.json", canonical_json_bytes(list(contracts)))
    content_hashes["contracts.json"] = sha256_file(output / "contracts.json")
    write_payload("metadata.json", canonical_json_bytes(dict(metadata)))
    content_hashes["metadata.json"] = sha256_file(output / "metadata.json")
    write_payload("availability.json", canonical_json_bytes(dict(availability)))
    content_hashes["availability.json"] = sha256_file(output / "availability.json")

    missing_sidecars: dict[str, str] = {}
    if session_windows_rows is not None:
        content_hashes["session_windows.csv"] = write_canonical_csv(
            output / "session_windows.csv",
            ("product", "trading_day", "session_open", "session_close", "source"),
            list(session_windows_rows),
        )
    else:
        missing_sidecars["session_windows"] = "not_present_no_verifiable_input"

    manifest = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "dataset_kind": dataset_kind,
        "status": "complete",
        "created_at": created_at,
        "request": dict(request_info),
        "row_counts": {
            "bars": len(bars_rows),
            "contracts": len(contracts),
        },
        "source_hashes": dict(source_hashes),
        "content_hashes": dict(content_hashes),
        "clock_policy": dict(clock_policy),
        "read_only_transaction": dict(read_only_transaction),
        "member_ranks": "not_requested",
        "missing_sidecars": missing_sidecars,
        "limits": dict(limits or {}),
        "notes": dict(notes or {}),
    }
    (output / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    return manifest


def read_input_bundle(path: Path) -> InputBundle:
    """Read and verify a complete input bundle; round-trip validated."""
    manifest_path = path / "manifest.json"
    if not manifest_path.exists():
        raise ValueError(f"no manifest.json in bundle: {path}")
    manifest = json.loads(manifest_path.read_bytes().decode("utf-8"))
    if manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported bundle schema: {manifest.get('schema_version')!r}; "
            "legacy normalized_research CSV is not accepted"
        )
    if manifest.get("status") != "complete":
        raise ValueError(f"bundle is not complete: {path}")
    content_hashes = manifest.get("content_hashes") or {}
    payloads: dict[str, bytes] = {}
    for name in ("bars.csv", "contracts.json", "metadata.json", "availability.json"):
        if name not in content_hashes:
            raise ValueError(f"manifest missing content hash for {name}")
        payloads[name] = (path / name).read_bytes()
        if sha256_bytes(payloads[name]) != content_hashes[name]:
            raise ValueError(f"content hash mismatch for {name}")
    rows = _parse_canonical_csv(payloads["bars.csv"], BARS_CSV_COLUMNS, "bars.csv")
    # Round-trip: re-serialize the parsed string cells with the same writer
    # and compare bytes against the published payload.
    buffer = _io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(list(BARS_CSV_COLUMNS))
    for row in rows:
        writer.writerow(
            [
                row.get(column) if row.get(column) is not None else ""
                for column in BARS_CSV_COLUMNS
            ]
        )
    if sha256_bytes(buffer.getvalue().encode("utf-8")) != content_hashes["bars.csv"]:
        raise ValueError("bars.csv round-trip verification failed")
    contracts = json.loads(payloads["contracts.json"].decode("utf-8"))
    metadata = json.loads(payloads["metadata.json"].decode("utf-8"))
    availability = json.loads(payloads["availability.json"].decode("utf-8"))
    if not isinstance(contracts, list) or not isinstance(metadata, dict) or not isinstance(availability, dict):
        raise ValueError("invalid input bundle payload structure")
    try:
        bars = tuple(bars_from_rows(rows))
    except (DecimalException, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid bar field in input bundle") from exc
    return InputBundle(
        root=path,
        manifest=manifest,
        bars=bars,
        contracts=tuple(contracts),
        metadata=metadata,
        availability=availability,
    )


# ---------------------------------------------------------------------------
# Signals bundle (S1 writer; account artifacts explicitly not requested)
# ---------------------------------------------------------------------------


def publish_signals_bundle(
    output: Path,
    *,
    created_at: str,
    config: Mapping[str, object],
    report: Mapping[str, object],
    availability: Mapping[str, object],
    signals_rows: Sequence[Mapping[str, object]],
    carry_pairs_rows: Sequence[Mapping[str, object]] | None = None,
    descriptive_feasibility_rows: Sequence[Mapping[str, object]] | None = None,
    forbidden_overlaps: Sequence[Path] = (),
) -> Mapping[str, object]:
    """Publish a signals-only bundle (§十三.7): no fills/equity/trades."""
    assert_output_directory_is_new(output, forbidden_overlaps)
    output.mkdir(parents=True)

    content_hashes: dict[str, str] = {}
    (output / "config.json").write_bytes(canonical_json_bytes(dict(config)))
    content_hashes["config.json"] = sha256_file(output / "config.json")
    (output / "availability.json").write_bytes(canonical_json_bytes(dict(availability)))
    content_hashes["availability.json"] = sha256_file(output / "availability.json")
    content_hashes["signals.csv"] = write_canonical_csv(
        output / "signals.csv", SIGNALS_CSV_COLUMNS, list(signals_rows)
    )
    optional: dict[str, str] = {}
    if carry_pairs_rows is not None:
        optional["carry_pairs.csv"] = "present"
        content_hashes["carry_pairs.csv"] = write_canonical_csv(
            output / "carry_pairs.csv",
            (
                "decision_at",
                "pair_id",
                "expiry_near",
                "expiry_far",
                "gap_days",
                "raw_log_spread",
                "carry_annualized",
                "selection_reason",
                "confirmation_count",
                "pair_changed",
            ),
            list(carry_pairs_rows),
        )
    if descriptive_feasibility_rows is not None:
        optional["descriptive_feasibility.csv"] = "present"
        content_hashes["descriptive_feasibility.csv"] = write_canonical_csv(
            output / "descriptive_feasibility.csv",
            ("product", "contract_instance_id", "metric", "value", "source_grade"),
            list(descriptive_feasibility_rows),
        )

    manifest = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "dataset_kind": "signals",
        "status": "complete",
        "created_at": created_at,
        "content_hashes": dict(content_hashes),
        "optional_outputs": optional,
        "account_artifacts": "not_requested",
    }
    # The run report is published after payloads; the manifest marker last.
    (output / "report.json").write_bytes(canonical_json_bytes(dict(report)))
    (output / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    return manifest


# ===========================================================================
# S2 additions (finalized-plan §十二.13): synthetic full-session demo fixture
# and backtest/report writers. Additive only; the S1 input/signals schema and
# functions above are unchanged.
# ===========================================================================

FILLS_CSV_COLUMNS: tuple[str, ...] = (
    "fill_id", "decision_id", "product", "contract_instance_id", "raw_symbol",
    "event_time", "trading_day", "side", "offset", "lots", "ideal_open",
    "effective_price", "multiplier", "fee_cny", "slippage_cost",
    "rule_evidence_ids", "used_bar_id",
)

EQUITY_CSV_COLUMNS: tuple[str, ...] = (
    "trading_day", "cash", "equity", "unrealized_pnl", "gross_notional",
    "required_margin", "estimated_available_equity", "invested", "status",
)

TRADES_CSV_COLUMNS: tuple[str, ...] = (
    "trade_id", "strategy_id", "product", "contract_instance_id", "raw_symbol",
    "opened_event_time", "closed_event_time", "direction", "lots",
    "entry_ideal_price", "exit_ideal_price", "multiplier", "opening_fees",
    "closing_fees", "opening_slippage", "closing_slippage", "gross_pnl",
    "net_pnl",
)

ROLLS_CSV_COLUMNS: tuple[str, ...] = (
    "decision_at", "product", "old_contract_instance_id",
    "new_contract_instance_id", "reason", "decision_id",
)

DEMO_SCHEMA_NOTE = (
    "synthetic parameters are fixture_valid only under "
    "dataset_kind=synthetic_demo; they are not approved real evidence"
)


def fills_rows(fills: Sequence[object]) -> list[dict[str, object]]:
    from ak_trading.futures.spec import ProductKey  # local: spec only

    rows: list[dict[str, object]] = []
    for fill in fills:
        rows.append(
            {
                "fill_id": fill.fill_id,
                "decision_id": fill.decision_id,
                "product": str(fill.product),
                "contract_instance_id": fill.contract_instance_id,
                "raw_symbol": fill.raw_symbol,
                "event_time": fill.event_time,
                "trading_day": fill.trading_day,
                "side": fill.side,
                "offset": fill.offset,
                "lots": fill.lots,
                "ideal_open": fill.ideal_open,
                "effective_price": fill.effective_price,
                "multiplier": fill.multiplier,
                "fee_cny": fill.fee_cny,
                "slippage_cost": fill.slippage_cost,
                "rule_evidence_ids": ";".join(fill.rule_evidence_ids),
                "used_bar_id": fill.used_bar_id or "",
            }
        )
    return rows


def equity_rows(marks: Sequence[object]) -> list[dict[str, object]]:
    return [
        {
            "trading_day": mark.trading_day,
            "cash": mark.cash,
            "equity": mark.equity,
            "unrealized_pnl": getattr(mark, "unrealized_pnl", ""),
            "gross_notional": getattr(mark, "gross_notional", ""),
            "required_margin": mark.required_margin,
            "estimated_available_equity": (
                mark.equity - mark.required_margin
                if mark.required_margin is not None
                else None
            ),
            "invested": "true" if getattr(mark, "invested", False) else "false",
            "status": mark.status,
        }
        for mark in marks
    ]


def trades_rows(trades: Sequence[object]) -> list[dict[str, object]]:
    return [
        {
            "trade_id": trade.trade_id,
            "strategy_id": trade.strategy_id,
            "product": str(trade.product),
            "contract_instance_id": trade.contract_instance_id,
            "raw_symbol": trade.raw_symbol,
            "opened_event_time": trade.opened_event_time,
            "closed_event_time": trade.closed_event_time,
            "direction": trade.direction,
            "lots": trade.lots,
            "entry_ideal_price": trade.entry_ideal_price,
            "exit_ideal_price": trade.exit_ideal_price,
            "multiplier": trade.multiplier,
            "opening_fees": trade.opening_fees,
            "closing_fees": trade.closing_fees,
            "opening_slippage": trade.opening_slippage,
            "closing_slippage": trade.closing_slippage,
            "gross_pnl": trade.gross_pnl,
            "net_pnl": trade.net_pnl,
        }
        for trade in trades
    ]


def publish_backtest_bundle(
    output: Path,
    *,
    created_at: str,
    config: Mapping[str, object],
    report: Mapping[str, object],
    availability: Mapping[str, object],
    splits: Mapping[str, object],
    signals_rows: Sequence[Mapping[str, object]],
    rolls_rows: Sequence[Mapping[str, object]],
    input_manifest_sha256: str,
    scenario_ledgers: Mapping[str, Mapping[str, Mapping[str, object]]],
    forbidden_overlaps: Sequence[Path] = (),
) -> Mapping[str, object]:
    """Publish the §十三.7 backtest layout exclusively.

    ``scenario_ledgers[scenario][portfolio_id]`` carries fills/equity/trades
    rows plus a per-ledger report; each independent ledger lands under
    ``scenarios/{scenario}/{portfolio_id}/`` with its own report.json.
    The manifest with the complete marker is written last.
    """
    assert_output_directory_is_new(output, forbidden_overlaps)
    output.mkdir(parents=True)
    content_hashes: dict[str, str] = {}

    (output / "config.json").write_bytes(canonical_json_bytes(dict(config)))
    content_hashes["config.json"] = sha256_file(output / "config.json")
    (output / "availability.json").write_bytes(canonical_json_bytes(dict(availability)))
    content_hashes["availability.json"] = sha256_file(output / "availability.json")
    (output / "splits.json").write_bytes(canonical_json_bytes(dict(splits)))
    content_hashes["splits.json"] = sha256_file(output / "splits.json")
    content_hashes["signals.csv"] = write_canonical_csv(
        output / "signals.csv", SIGNALS_CSV_COLUMNS, list(signals_rows)
    )
    content_hashes["rolls.csv"] = write_canonical_csv(
        output / "rolls.csv", ROLLS_CSV_COLUMNS, list(rolls_rows)
    )

    ledger_digests: dict[str, dict[str, str]] = {}
    for scenario, portfolios in sorted(scenario_ledgers.items()):
        for portfolio_id, ledger in sorted(portfolios.items()):
            directory = output / "scenarios" / scenario / portfolio_id
            directory.mkdir(parents=True)
            hashes: dict[str, str] = {}
            hashes["fills.csv"] = write_canonical_csv(
                directory / "fills.csv", FILLS_CSV_COLUMNS, list(ledger["fills_rows"])
            )
            hashes["equity.csv"] = write_canonical_csv(
                directory / "equity.csv", EQUITY_CSV_COLUMNS, list(ledger["equity_rows"])
            )
            hashes["trades.csv"] = write_canonical_csv(
                directory / "trades.csv", TRADES_CSV_COLUMNS, list(ledger["trades_rows"])
            )
            (directory / "report.json").write_bytes(
                canonical_json_bytes(dict(ledger["report"]))
            )
            hashes["report.json"] = sha256_file(directory / "report.json")
            ledger_digests.setdefault(scenario, {})[portfolio_id] = (
                hashlib.sha256(
                    "".join(
                        hashes[name] for name in sorted(hashes)
                    ).encode("utf-8")
                ).hexdigest()
            )

    (output / "report.json").write_bytes(canonical_json_bytes(dict(report)))
    content_hashes["report.json"] = sha256_file(output / "report.json")
    manifest = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "dataset_kind": "backtest",
        "status": "complete",
        "created_at": created_at,
        "content_hashes": dict(content_hashes),
        "input_manifest_sha256": input_manifest_sha256,
        "scenario_ledger_digests": ledger_digests,
        "accounting": "ideal_price_cny_ledger",
        "notes": {"demo_schema": DEMO_SCHEMA_NOTE},
    }
    (output / "manifest.json").write_bytes(canonical_json_bytes(manifest))
    return manifest


# ---------------------------------------------------------------------------
# Deterministic synthetic demo fixture (S2): full-session, fixture_valid only
# under dataset_kind=synthetic_demo. No clock reads, no randomness: every
# value is a pure function of the day index (§十三.3).
# ---------------------------------------------------------------------------


def build_demo_fixture(total_days: int = 630) -> Mapping[str, object]:
    """Build the synthetic full-session demo fixture as plain data.

    Layout:
      dates: D (weekdays from 2024-01-01, Asia/Shanghai 09:00-15:00);
      contracts: per-instance product, expiry day index and rule payload;
      bars: per-instance {iso day -> ContractBar};
      rules: per-instance plain rule payload (multiplier/tick/fees/margin/
      lot step/limit bands + median volume) for backtest.ContractRuleSet;
      roll: RB active contract switches demoRB1 -> demoRB2 at day 450.
    Prices: RB trends up (trend families long), IF oscillates (z-score
    trades), each far contract prices at near/1.011 (backwardation carry
    that clears the 0.03 annualized threshold at the ~126-day gap).
    """
    from datetime import timedelta, timezone

    from ak_trading.futures.spec import ContractBar

    sh = timezone(timedelta(hours=8))
    first = date(2024, 1, 1)
    dates: list[date] = []
    cursor = first
    while len(dates) < total_days:
        if cursor.weekday() < 5:
            dates.append(cursor)
        cursor += timedelta(days=1)

    def session_times(day: date) -> tuple[datetime, datetime]:
        base = datetime.combine(day, datetime.min.time(), tzinfo=sh)
        return base + timedelta(hours=9), base + timedelta(hours=15)

    def rb_price(index: int) -> float:
        return 3000.0 * (1.0 + 0.0009 * index) + 8.0 * math_sin(index / 9.0)

    def if_price(index: int) -> float:
        return 4000.0 + 60.0 * math_sin(index / 7.0)

    def math_sin(x: float) -> float:
        import math

        return math.sin(x)

    rb1_start, rb2_start, rb3_start = 0, 380, 500
    # Short fixtures clamp the roll/expiry indices so truncated demos stay
    # internally consistent (the roll simply never happens within 450 days).
    roll_day_index = min(450, total_days - 1)
    rb1_expiry_index = min(460, total_days - 1)
    rb2_expiry_index = min(550, total_days + 90)
    rb3_expiry_index = min(640, total_days + 180)

    def make_series(
        symbol: str,
        start_index: int,
        price_fn,
        *,
        scale: float = 1.0,
    ) -> dict[str, ContractBar]:
        bars: dict[str, ContractBar] = {}
        previous_close: float | None = None
        for index in range(start_index, total_days):
            day = dates[index]
            session_open, session_close = session_times(day)
            close = price_fn(index) * scale
            if previous_close is None:
                open_price = close
            else:
                open_price = previous_close * (1.0 + 0.0004 * math_sin(index / 5.0))
            high = max(open_price, close) * 1.001
            low = min(open_price, close) * 0.999
            nano = int(session_close.timestamp() * 1_000_000_000)
            bars[day.isoformat()] = ContractBar(
                symbol=symbol,
                duration_seconds=86400,
                datetime_nano=nano,
                ts=session_close - timedelta(hours=6),
                source="demo_fixture",
                trading_day=day,
                trading_day_source="demo_fixture_calendar",
                session_open=session_open,
                session_close=session_close,
                available_at=session_close,
                open=_dec(open_price),
                high=_dec(high),
                low=_dec(low),
                close=_dec(close),
                volume=Decimal("50000"),
            )
            previous_close = close
        return bars

    def _dec(value: float) -> Decimal:
        return Decimal(str(round(value, 4)))

    rb1 = make_series("SHFE.demoRB1", rb1_start, rb_price)
    rb2 = make_series("SHFE.demoRB2", rb2_start, rb_price, scale=1 / 1.011)
    rb3 = make_series("SHFE.demoRB3", rb3_start, rb_price, scale=1 / (1.011 * 1.011))
    if1 = make_series("CFFEX.demoIF1", 0, if_price)

    def expiry(index: int) -> str:
        day = dates[min(index, total_days - 1)]
        _, session_close = session_times(day)
        return session_close.isoformat()

    def rule_payload(multiplier: str, tick: str) -> dict[str, object]:
        return {
            "multiplier": multiplier,
            "price_tick": tick,
            "lot_step": 1,
            "min_open_lots": 1,
            "fee_open_cny_per_lot": "5.0",
            "fee_close_cny_per_lot": "5.0",
            "fee_close_today_cny_per_lot": "5.0",
            "margin_long_rate": "0.08",
            "margin_short_rate": "0.08",
            "limit_band_ratio": "0.04",
            "median_volume20_override": 50000.0,
        }

    contracts = {
        "SHFE.demoRB1": {
            "product": "SHFE.RB",
            "raw_symbol": "SHFE.demoRB1",
            "expiry_at": expiry(rb1_expiry_index),
            "active_from_index": rb1_start,
            "active_through_index": roll_day_index - 1,
            "rules": rule_payload("10", "1"),
        },
        "SHFE.demoRB2": {
            "product": "SHFE.RB",
            "raw_symbol": "SHFE.demoRB2",
            "expiry_at": expiry(rb2_expiry_index),
            "active_from_index": rb2_start,
            "active_through_index": total_days - 1,
            "rules": rule_payload("10", "1"),
        },
        "SHFE.demoRB3": {
            "product": "SHFE.RB",
            "raw_symbol": "SHFE.demoRB3",
            "expiry_at": expiry(rb3_expiry_index),
            "active_from_index": rb3_start,
            "active_through_index": total_days - 1,
            "rules": rule_payload("10", "1"),
        },
        "CFFEX.demoIF1": {
            "product": "CFFEX.IF",
            "raw_symbol": "CFFEX.demoIF1",
            "expiry_at": expiry(total_days + 30),
            "active_from_index": 0,
            "active_through_index": total_days - 1,
            "rules": rule_payload("300", "0.2"),
        },
    }
    bars = {
        "SHFE.demoRB1": rb1,
        "SHFE.demoRB2": rb2,
        "SHFE.demoRB3": rb3,
        "CFFEX.demoIF1": if1,
    }
    sessions: dict[str, dict[str, tuple[str, str]]] = {}
    for instance_id, series in bars.items():
        per_day: dict[str, tuple[str, str]] = {}
        for iso_day, bar in series.items():
            per_day[iso_day] = (bar.session_open.isoformat(), bar.session_close.isoformat())
        sessions[instance_id] = per_day

    return {
        "dataset_kind": "synthetic_demo",
        "note": DEMO_SCHEMA_NOTE,
        "dates": [d.isoformat() for d in dates],
        "sessions": sessions,
        "bars": bars,
        "contracts": contracts,
        "roll": {
            "product": "SHFE.RB",
            "day": dates[roll_day_index].isoformat(),
            "old": "SHFE.demoRB1",
            "new": "SHFE.demoRB2",
            "reason": "past_liquidity_forward_roll_fixture",
        },
        "initial_nav_cny": "400000000",
    }
