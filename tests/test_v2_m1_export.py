"""M1-04 export: fake connection, READ ONLY verification, paging and limits."""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

from ak_trading.v2 import contracts as c
from ak_trading.v2 import data as d
from ak_trading.v2 import export as e
from ak_trading.v2 import schemas as s

M = 60_000_000_000
H = 3_600_000_000_000
ZERO = "0" * 64


def unano(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 1, day, hour, minute, tzinfo=timezone.utc) - timedelta(hours=8)


def quality() -> c.DataQuality:
    return c.DataQuality(status="READY", reasons=(), temporal_basis="observed",
                         input_hash=hashlib.sha256(b"export-test").hexdigest())


def instrument(contract_id: str = "SYN.rb2609") -> c.Instrument:
    return c.Instrument(
        contract_id=contract_id, raw_symbol=contract_id, product="SYN.rb",
        sector="synthetic", risk_groups=("SYN",), listing_nano=0, expiry_nano=None,
        last_trade_nano=None, delivery_cutoff_nano=None, quality=quality())


def sessions() -> tuple[c.SessionWindow, ...]:
    spans = [(9, 0, 10, 15), (10, 30, 11, 30), (13, 30, 15, 0)]
    return tuple(
        c.SessionWindow(session_id=f"D09-{idx}", product="SYN.rb", trading_day=date(2026, 1, 9),
                        start_nano=_nano(unano(9, a, b)), end_nano=_nano(unano(9, x, y)),
                        kind="DAY", calendar_version="synthetic-v1",
                        evidence_status="SCENARIO")
        for idx, (a, b, x, y) in enumerate(spans))


def _nano(moment: datetime) -> int:
    delta = moment - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 10**9


def rule(contract_id: str = "SYN.rb2609") -> c.TradingRule:
    leg = c.FeeLeg(per_lot_cny=Decimal("0"), notional_rate=Decimal("0.0001"),
                   explicit_zero=False)
    return c.TradingRule(
        rule_id="RL", contract_id=contract_id, effective_from_nano=0,
        effective_until_nano=_nano(unano(30, 15)), available_at_nano=0,
        multiplier=Decimal("10"), price_tick=Decimal("1"), lot_step=1, min_open_lots=1,
        open_fee=leg, close_fee=leg, close_today_fee=leg, long_margin_rate=Decimal("0.09"),
        short_margin_rate=Decimal("0.09"), limit_up=None, limit_down=None,
        slippage_ticks=Decimal("1"), impact_ticks=Decimal("0"),
        participation_rate=Decimal("0.1"), evidence="SCENARIO", source_sha256=ZERO)


def db_rows(count: int, symbol: str = "SYN.rb2609", duration: int = 60) -> list[dict]:
    rows = []
    for i in range(count):
        start = _nano(unano(9, 9)) + i * M
        moment = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=start // 1000)
        imported = moment + timedelta(seconds=90)
        rows.append({
            "symbol": symbol, "duration_seconds": duration, "datetime_nano": start,
            "ts": moment, "trading_day": date(2026, 1, 9), "trading_day_source": "exchange",
            "open": Decimal(100 + i), "high": Decimal(101 + i), "low": Decimal(99 + i),
            "close": Decimal(100 + i), "volume": Decimal(i + 1), "open_oi": Decimal(20),
            "close_oi": Decimal(21), "source": "fake-db", "batch_id": "b1",
            "imported_at": imported,
        })
    return rows


class FakeResult:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def scalar_one(self):
        return self.rows[0] if self.rows else None

    def mappings(self):
        return self

    def all(self):
        return [dict(row) for row in self.rows]


class FakeTransaction:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self.rolled_back = False
        self.committed = False

    def rollback(self) -> None:
        self.rolled_back = True

    def commit(self) -> None:
        self.committed = True


class FakeConnection:
    dialect = type("Dialect", (), {"driver": "offline-fixture"})()

    def __init__(self, minute_rows: list[dict], daily_rows: list[dict] | None = None,
                 *, verified: bool = True, fail_on_query: bool = False) -> None:
        self.minute_rows = list(minute_rows)
        self.daily_rows = list(daily_rows or [])
        self.verified = verified
        self.fail_on_query = fail_on_query
        self.statements: list[str] = []

    def begin(self) -> FakeTransaction:
        return FakeTransaction(self)

    def rollback(self) -> None:
        pass

    def commit(self) -> None:
        pass

    def execute(self, statement, params=None) -> FakeResult:
        text = str(statement)
        self.statements.append(text)
        if text.startswith("SET TRANSACTION"):
            return FakeResult([])
        if text.startswith("SHOW transaction_isolation"):
            return FakeResult(["repeatable read" if self.verified else "read committed"])
        if text.startswith("SHOW transaction_read_only"):
            return FakeResult(["on" if self.verified else "off"])
        if self.fail_on_query:
            raise RuntimeError("query failed")
        params = params or {}
        rows = self.minute_rows if params.get("duration") == 60 else self.daily_rows
        symbols = list(params.get("symbols") or [])
        start = params.get("start_nano", 0)
        end = params.get("end_nano", 2**62)
        last_symbol = params.get("last_symbol")
        last_nano = params.get("last_nano")
        filtered = [row for row in rows
                    if row["symbol"] in symbols
                    and start <= row["datetime_nano"] < end
                    and (last_symbol is None or (row["symbol"], row["datetime_nano"])
                         > (last_symbol, last_nano))]
        return FakeResult(filtered[:params.get("page_size", 5000)])


def make_request(**overrides) -> e.ExportRequestV2:
    values = dict(
        contract_ids=("SYN.rb2609",), raw_symbols=("SYN.rb2609",),
        start_nano=_nano(unano(5, 9)), end_nano=_nano(unano(9, 16)),
        as_of_nano=_nano(unano(9, 16)), durations=(60, 86400),
        instruments=(instrument(),), sessions=sessions(), rules=(rule(),),
        page_size=e.DEFAULT_PAGE_SIZE, maximum_rows=e.DEFAULT_MAXIMUM_ROWS,
        maximum_bytes=e.DEFAULT_MAXIMUM_BYTES)
    values.update(overrides)
    return e.ExportRequestV2(**values)


def test_fake_connection_read_only_export(tmp_path: Path) -> None:
    minute = db_rows(10)
    daily_row = dict(db_rows(1, duration=86400)[0])
    daily_row["datetime_nano"] = _nano(unano(9, 9))
    connection = FakeConnection(minute, [daily_row])
    output = tmp_path / "bundle"
    frozen = e.export_frozen_input(connection, make_request(), output)
    # READ ONLY + REPEATABLE READ verified via SHOW before any business SELECT.
    first_set = next(i for i, text in enumerate(connection.statements)
                     if text.startswith("SET TRANSACTION"))
    first_show = next(i for i, text in enumerate(connection.statements)
                      if text.startswith("SHOW"))
    first_select = next(i for i, text in enumerate(connection.statements)
                        if text.upper().startswith("SELECT"))
    assert first_set < first_show < first_select
    assert all("OFFSET" not in text.upper() for text in connection.statements)
    # Both fixed durations were queried through the injected connection.
    assert len(frozen.bars) == 11
    assert {bar.duration_seconds for bar in frozen.bars} == {60, 86400}
    # Availability keeps realtime NOT_ACCEPTED; synthetic-free real export shape.
    availability = s.parse_json_bytes((output / "availability.json").read_bytes())
    assert availability["realtime_status"] == "NOT_ACCEPTED"
    assert availability["contract_status"] == {"SYN.rb2609": "EXPORTED"}
    # bars.csv column order is exactly the MarketBar declaration order.
    header = (output / "bars.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header.split(",") == [f.name for f in __import__("dataclasses").fields(c.MarketBar)]
    # The returned FrozenInput equals an independent verified re-read.
    assert frozen == d.read_frozen_input(output)
    # Availability audit bound: available_at follows imported_at (unknown basis, not reconstructed).
    assert all(bar.available_at_nano is not None and bar.available_at_nano >= bar.end_nano
               for bar in frozen.bars if bar.duration_seconds == 60)

    # Unverified read-only snapshots block the export without any manifest.
    unverified = FakeConnection(minute, [daily_row], verified=False)
    with pytest.raises(e.ExportError):
        e.export_frozen_input(unverified, make_request(), tmp_path / "blocked")
    assert not (tmp_path / "blocked").exists()

    # Keyset paging: several small pages return every row exactly once.
    paged = FakeConnection(minute, [daily_row])
    frozen_paged = e.export_frozen_input(
        paged, make_request(page_size=3), tmp_path / "paged")
    assert len(frozen_paged.bars) == 11

    # Query failure is a fixed-reason export error, not a driver leak.
    with pytest.raises(e.ExportError):
        e.export_frozen_input(FakeConnection(minute, [daily_row], fail_on_query=True),
                              make_request(), tmp_path / "failed")

    # Identical duplicate rows are idempotent; conflicting rows are INVALID.
    duplicated = FakeConnection(minute + list(minute), [daily_row, dict(daily_row)])
    frozen_dup = e.export_frozen_input(duplicated, make_request(), tmp_path / "dup")
    assert len(frozen_dup.bars) == 11
    conflict = dict(minute[0])
    conflict["open"] = Decimal("999")
    conflict["high"] = Decimal("1000")
    conflict["low"] = Decimal("998")
    conflict["close"] = Decimal("999")
    with pytest.raises(e.ExportError):
        e.export_frozen_input(FakeConnection([minute[0], conflict], []),
                              make_request(), tmp_path / "conflict")

    # Limits: rows, bytes, contracts, durations, output-directory existence.
    with pytest.raises(e.ExportError):
        e.export_frozen_input(FakeConnection(minute, []), make_request(maximum_rows=5),
                              tmp_path / "rows-limit")
    assert not (tmp_path / "rows-limit").exists()
    with pytest.raises(e.ExportError):
        e.export_frozen_input(FakeConnection(minute, []), make_request(maximum_bytes=200),
                              tmp_path / "bytes-limit")
    assert not (tmp_path / "bytes-limit").exists()
    with pytest.raises(e.ExportError):
        e.export_frozen_input(
            FakeConnection([], []),
            make_request(
                contract_ids=tuple(f"C{i}" for i in range(7)),
                raw_symbols=tuple(f"C{i}" for i in range(7)),
                instruments=tuple(instrument(f"C{i}") for i in range(7)),
                rules=tuple(rule(f"C{i}") for i in range(7))),
            tmp_path / "contracts-limit")
    with pytest.raises(e.ExportError):
        e.export_frozen_input(FakeConnection([], []), make_request(durations=(60,)),
                              tmp_path / "durations")
    output.mkdir(exist_ok=True)
    with pytest.raises(e.ExportError):
        e.export_frozen_input(FakeConnection(minute, [daily_row]), make_request(), output)
    # The module never touches configuration loaders or dotenv.
    source = Path(e.__file__).read_text(encoding="utf-8")
    assert "dotenv" not in source and "ak_trading.config" not in source


def test_reconstruction_policy_and_forming_daily_boundaries(tmp_path: Path) -> None:
    """01 §5.3: explicit PRICE_RECONSTRUCTED clock; conservative imported bound
    otherwise; forming daily bars never claimed closed."""
    # A 2024 historical minute row imported in 2026 (audit field far later).
    bar_start = _nano(datetime(2024, 3, 4, 1, 0, tzinfo=timezone.utc))  # 09:00 CST
    imported = datetime(2026, 9, 14, 4, 0, tzinfo=timezone.utc)
    minute_row = {
        "symbol": "SYN.rb2609", "duration_seconds": 60, "datetime_nano": bar_start,
        "ts": datetime(2024, 3, 4, 1, 0, tzinfo=timezone.utc),
        "trading_day": date(2024, 3, 4), "trading_day_source": "exchange",
        "open": Decimal("100"), "high": Decimal("101"), "low": Decimal("99"),
        "close": Decimal("100"), "volume": Decimal("1"), "open_oi": Decimal("20"),
        "close_oi": Decimal("21"), "source": "fake-db", "batch_id": "b1",
        "imported_at": imported,
    }
    daily_row = dict(minute_row, duration_seconds=86400)
    export_clock = _nano(datetime(2026, 9, 14, 5, 0, tzinfo=timezone.utc))
    request = e.ExportRequestV2(
        contract_ids=("SYN.rb2609",), raw_symbols=("SYN.rb2609",),
        start_nano=1, end_nano=export_clock, as_of_nano=export_clock,
        durations=(60, 86400), instruments=(instrument(),),
        sessions=tuple(
            c.SessionWindow(
                session_id=f"D0403-{index}", product="SYN.rb",
                trading_day=date(2024, 3, 4),
                start_nano=_nano(datetime(2024, 3, 4, a, b, tzinfo=timezone.utc)),
                end_nano=_nano(datetime(2024, 3, 4, x, y, tzinfo=timezone.utc)),
                kind="DAY",
                calendar_version="synthetic-v1", evidence_status="SCENARIO")
            for index, (a, b, x, y) in enumerate(
                [(1, 0, 1, 15), (1, 30, 2, 0), (4, 30, 6, 0)])),
        rules=(rule(),), page_size=5000, maximum_rows=200000,
        maximum_bytes=268435456)

    # Without the declared policy: availability stays the imported-at audit
    # bound with an unknown temporal basis — the 2024 decision sees nothing.
    plain = e.export_frozen_input(FakeConnection([minute_row], []),
                                  request, tmp_path / "plain")
    bar = plain.bars[0]
    assert bar.imported_at_nano == _nano(imported)
    assert bar.available_at_nano == _nano(imported)
    assert bar.quality.temporal_basis == "unknown"
    assert d.visible_bars(plain, "SYN.rb2609", 60,
                          bar.end_nano + 10**9, intraday=False) == ()
    manifest = s.parse_json_bytes((tmp_path / "plain" / "manifest.json").read_bytes())
    # Unknown availability basis never claims PIT: conservative reconstructed
    # kind with an explicit not-ready marker (imported_at is audit only).
    assert manifest["dataset_kind"] == "HISTORICAL_RECONSTRUCTED"
    assert "bar_availability_unknown_not_reconstructed_ready" in manifest["limitations"]

    # F02(r3): the reconstruction keyword extension is reverted to the frozen
    # 3-parameter interface; default (no policy) bars are DEGRADED/unknown and
    # not consumable via visible_bars.
    assert bar.quality.status == "DEGRADED"

    # A forming daily bar (its day still open at the export clock) is never
    # complete: actual minutes count planned slots elapsed so far.
    forming_clock = _nano(datetime(2024, 3, 4, 5, 0, tzinfo=timezone.utc))
    forming_request = e.ExportRequestV2(
        contract_ids=("SYN.rb2609",), raw_symbols=("SYN.rb2609",),
        start_nano=1, end_nano=forming_clock, as_of_nano=forming_clock,
        durations=(60, 86400), instruments=(instrument(),),
        sessions=request.sessions, rules=(rule(),), page_size=5000,
        maximum_rows=200000, maximum_bytes=268435456)
    forming = e.export_frozen_input(FakeConnection([], [daily_row]), forming_request,
                                    tmp_path / "forming")
    dbar = forming.bars[0]
    assert dbar.complete is False
    assert "bar_not_closed_at_export_clock" in dbar.quality.reasons
    assert 0 < dbar.actual_minutes < dbar.expected_minutes
    # And it is invisible to point-in-time consumers regardless of availability.
    assert d.visible_bars(forming, "SYN.rb2609", 86400,
                          forming_clock + 10**9, intraday=False) == ()


def assert_provenance_consistent(bundle_root: Path, frozen: c.FrozenInput) -> None:
    """F07 (P04): bar quality, availability.json and manifest.json must all
    state the same provenance fact: availability unknown / not reconstructed;
    imported_at is an import audit bound, never a reconstruction claim."""
    availability = s.parse_json_bytes((bundle_root / "availability.json").read_bytes())
    manifest = s.parse_json_bytes((bundle_root / "manifest.json").read_bytes())
    reasons = list(availability["reasons"])
    limitations = list(manifest["limitations"])
    # 1. Every bar: unknown basis, not reconstructed — therefore DEGRADED.
    for bar in frozen.bars:
        assert bar.quality.temporal_basis == "unknown"
        assert "availability_unknown_not_reconstructed" in bar.quality.reasons
        assert bar.quality.status == "DEGRADED"
    # 2. availability.json states the same fact and no reconstruction claim.
    assert "availability_unknown_not_reconstructed" in reasons
    assert "reconstructed_from" not in "".join(reasons)
    assert availability["realtime_status"] == "NOT_ACCEPTED"
    # 3. manifest limitations state the same fact and no reconstruction claim.
    assert "bar_availability_unknown_not_reconstructed_ready" in limitations
    assert "availability_temporal_basis_unknown_not_first_publication_pit" in limitations
    assert "reconstructed_from" not in "".join(limitations)
    # 4. Cross-product: the same semantic token in bar and availability
    #    reasons (when any bar exists); the F07 contradiction string appears
    #    in none of the three declarations. An empty bundle vacates the bar
    #    checks but must not issue stronger claims than a populated one.
    joined = "".join(reasons) + "".join(limitations) + "".join(
        reason for bar in frozen.bars for reason in bar.quality.reasons)
    assert "availability_reconstructed_from_imported_at" not in joined
    if frozen.bars:
        bar_reasons = {reason for bar in frozen.bars for reason in bar.quality.reasons}
        assert "availability_unknown_not_reconstructed" in bar_reasons
        assert "availability_unknown_not_reconstructed" in reasons


def test_t1_provenance_consistency_baseline(tmp_path: Path) -> None:
    """T1: a mixed minute+daily export declares one consistent provenance
    fact across bar quality, availability.json and manifest.json."""
    minute = db_rows(10)
    daily_row = dict(db_rows(1, duration=86400)[0])
    daily_row["datetime_nano"] = _nano(unano(9, 9))
    root = tmp_path / "t1-baseline"
    frozen = e.export_frozen_input(FakeConnection(minute, [daily_row]), make_request(), root)
    assert_provenance_consistent(root, frozen)
    manifest = s.parse_json_bytes((root / "manifest.json").read_bytes())
    assert manifest["dataset_kind"] == "HISTORICAL_RECONSTRUCTED"
    # The returned FrozenInput equals an independent verified re-read.
    assert frozen == d.read_frozen_input(root)


def test_t2_missing_imported_at(tmp_path: Path) -> None:
    """T2 (counterexample): a row without the imported_at audit field still
    exports (None availability timestamps) with consistent declarations and
    stays invisible to point-in-time consumers."""
    row = dict(db_rows(1)[0])
    del row["imported_at"]
    root = tmp_path / "t2-missing-imported-at"
    frozen = e.export_frozen_input(FakeConnection([row], []), make_request(), root)
    bar = frozen.bars[0]
    assert bar.available_at_nano is None and bar.imported_at_nano is None
    assert_provenance_consistent(root, frozen)
    assert d.visible_bars(frozen, "SYN.rb2609", 60, bar.end_nano + 10**9,
                          intraday=False) == ()


def test_t3_missing_imported_at_and_source(tmp_path: Path) -> None:
    """T3 (counterexample): additionally missing the source key falls back to
    the "database" source while the declarations stay consistent."""
    row = dict(db_rows(1)[0])
    del row["imported_at"]
    del row["source"]
    root = tmp_path / "t3-missing-imported-at-and-source"
    frozen = e.export_frozen_input(FakeConnection([row], []), make_request(), root)
    bar = frozen.bars[0]
    assert bar.source == "database"
    assert bar.available_at_nano is None and bar.imported_at_nano is None
    assert_provenance_consistent(root, frozen)
    assert d.visible_bars(frozen, "SYN.rb2609", 60, bar.end_nano + 10**9,
                          intraday=False) == ()


def test_t4_naive_vs_aware_imported_at_boundary(tmp_path: Path) -> None:
    """T4 (counterexample): a naive imported_at is read as +08:00 wall clock,
    so the same wall time differs from the aware-UTC form by exactly 8h;
    both bundles stay consistently declared."""
    aware = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    naive = aware.replace(tzinfo=None)
    observed: dict[str, int] = {}
    for name, stamp in (("t4-naive-wallclock", naive), ("t4-aware-utc", aware)):
        row = dict(db_rows(1)[0], imported_at=stamp)
        root = tmp_path / name
        frozen = e.export_frozen_input(FakeConnection([row], []), make_request(), root)
        bar = frozen.bars[0]
        assert bar.available_at_nano == bar.imported_at_nano
        observed[name] = bar.imported_at_nano
        assert_provenance_consistent(root, frozen)
    # naive 12:00 wall clock -> 04:00Z; aware 12:00+00:00 -> 12:00Z; delta 8h.
    assert observed["t4-naive-wallclock"] == 1_789_358_400_000_000_000
    assert observed["t4-aware-utc"] == 1_789_387_200_000_000_000
    assert (observed["t4-aware-utc"] - observed["t4-naive-wallclock"]
            == 8 * 3600 * 10**9)


def test_t5_empty_data(tmp_path: Path) -> None:
    """T5 (counterexample): zero rows still export; the empty bundle must not
    issue stronger provenance claims than a populated one."""
    root = tmp_path / "t5-empty"
    frozen = e.export_frozen_input(FakeConnection([], []), make_request(), root)
    assert len(frozen.bars) == 0
    availability = s.parse_json_bytes((root / "availability.json").read_bytes())
    assert availability["contract_status"] == {"SYN.rb2609": "NO_ROWS"}
    assert availability["minute_history_status"] == "NO_MINUTE_ROWS"
    assert_provenance_consistent(root, frozen)


def test_t6_replay_determinism_and_no_overwrite(tmp_path: Path) -> None:
    """T6: the same rows and request replay byte-identically into a new
    directory, and an existing bundle is refused, never overwritten."""
    minute = db_rows(10)
    daily_row = dict(db_rows(1, duration=86400)[0])
    daily_row["datetime_nano"] = _nano(unano(9, 9))
    first = tmp_path / "t6-first"
    e.export_frozen_input(FakeConnection(minute, [daily_row]), make_request(), first)
    second = tmp_path / "t6-second"
    e.export_frozen_input(FakeConnection(list(minute), [dict(daily_row)]),
                          make_request(), second)
    for name in ("availability.json", "bars.csv", "manifest.json",
                 "instruments.json", "sessions.json", "rules.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
    before = {path.name: path.read_bytes() for path in first.iterdir()}
    with pytest.raises(e.ExportError):
        e.export_frozen_input(FakeConnection(minute, [daily_row]), make_request(), first)
    assert {path.name: path.read_bytes() for path in first.iterdir()} == before
