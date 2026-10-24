"""M1-03 data: PIT visibility, bad rows, missing rows, causality, collections."""
from __future__ import annotations

import dataclasses
import hashlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ak_trading.v2 import CONTRACT_VERSION
from ak_trading.v2 import clock as k
from ak_trading.v2 import contracts as c
from ak_trading.v2 import data as d
from ak_trading.v2 import schemas as s
from ak_trading.v2.artifacts import ArtifactsError, publish_bundle

M = 60_000_000_000
H = 3_600_000_000_000
CALENDAR = "synthetic-calendar-v2.1.0"


def unano(day: int, hour: int, minute: int = 0, second: int = 0) -> int:
    moment = datetime(2026, 1, day, hour, minute, second, tzinfo=timezone.utc) - timedelta(hours=8)
    delta = moment - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 10**9


def quality() -> c.DataQuality:
    return c.DataQuality(status="READY", reasons=(), temporal_basis="reconstructed",
                         input_hash=hashlib.sha256(b"data-test").hexdigest())


def sessions_for_day(day: int) -> tuple[c.SessionWindow, ...]:
    spans = [(9, 0, 10, 15), (10, 30, 11, 30), (13, 30, 15, 0)]
    return tuple(
        c.SessionWindow(session_id=f"D{day:02d}-{idx}", product="SYN.rb",
                        trading_day=date(2026, 1, day), start_nano=unano(day, a, b),
                        end_nano=unano(day, x, y), kind="DAY", calendar_version=CALENDAR,
                        evidence_status="SCENARIO")
        for idx, (a, b, x, y) in enumerate(spans))


def minute_bars(day: int, count: int, *, start_hour: int = 9, start_minute: int = 0,
                missing: set[int] = frozenset(), available_delay_seconds: int = 60) \
        -> list[c.MarketBar]:
    bars = []
    for i in range(count):
        if i in missing:
            continue
        start = unano(day, start_hour, start_minute) + i * M
        bars.append(c.MarketBar(
            bar_id=f"B{day}-{i}", contract_id="SYN.rb2609", raw_symbol="SYN.rb2609",
            product="SYN.rb", duration_seconds=60, datetime_nano=start,
            start_nano=start, end_nano=start + M, trading_day=date(2026, 1, day),
            session_id=f"D{day:02d}-{0 if start_minute + i < 75 else 1}",
            available_at_nano=start + available_delay_seconds * 10**9,
            imported_at_nano=start + M,
            open=Decimal(100 + i), high=Decimal(101 + i), low=Decimal(99 + i),
            close=Decimal(100 + i), volume=Decimal(i + 1), open_oi=Decimal(20),
            close_oi=Decimal(21), complete=True, expected_minutes=1, actual_minutes=1,
            source="data-test", quality=quality()))
    return bars


def instrument() -> c.Instrument:
    return c.Instrument(
        contract_id="SYN.rb2609", raw_symbol="SYN.rb2609", product="SYN.rb", sector="synthetic",
        risk_groups=("SYN",), listing_nano=unano(5, 9), expiry_nano=None,
        last_trade_nano=None, delivery_cutoff_nano=None, quality=quality())


def rule() -> c.TradingRule:
    leg = c.FeeLeg(per_lot_cny=Decimal("0"), notional_rate=Decimal("0.0001"),
                   explicit_zero=False)
    return c.TradingRule(
        rule_id="RL-1", contract_id="SYN.rb2609", effective_from_nano=unano(5, 9),
        effective_until_nano=unano(30, 15), available_at_nano=unano(5, 9),
        multiplier=Decimal("10"), price_tick=Decimal("1"), lot_step=1, min_open_lots=1,
        open_fee=leg, close_fee=leg, close_today_fee=leg, long_margin_rate=Decimal("0.09"),
        short_margin_rate=Decimal("0.09"), limit_up=None, limit_down=None,
        slippage_ticks=Decimal("1"), impact_ticks=Decimal("0"), participation_rate=Decimal("0.1"),
        evidence="SCENARIO", source_sha256="a" * 64)


def build_package(output: Path, *, bars: list[c.MarketBar], instruments=None,
                  sessions=None, rules=None, macro_items=(), dataset_kind: str = "SYNTHETIC") -> Path:
    instruments = [instrument()] if instruments is None else instruments
    sessions = sessions_for_day(9) if sessions is None else sessions
    rules = [rule()] if rules is None else rules
    payloads = {
        "bars.csv": s.encode_csv_bytes(c.MarketBar, tuple(bars)),
        "instruments.json": s.encode_record_file("Instrument", s.INSTRUMENTS_SCHEMA_ID, instruments),
        "sessions.json": s.encode_record_file("SessionWindow", s.SESSIONS_SCHEMA_ID, list(sessions)),
        "macro_items.json": s.encode_record_file("MacroItem", s.MACRO_ITEMS_SCHEMA_ID, list(macro_items)),
        "rules.json": s.encode_record_file("TradingRule", s.RULES_SCHEMA_ID, rules),
        "availability.json": s.canonical_json_bytes({
            "schema_version": s.AVAILABILITY_SCHEMA_ID, "input_manifest_sha256": None,
            "contract_status": {"SYN.rb2609": "EXPORTED" if bars else "NO_ROWS"},
            "minute_history_status": "EXPORTED" if bars else "NO_MINUTE_ROWS",
            "realtime_status": "NOT_ACCEPTED", "reasons": ["synthetic_fixture"]}),
    }
    provenance = {
        "schema_version": "ak_probability_input_v2.1.0", "contract_version": CONTRACT_VERSION,
        "dataset_kind": dataset_kind, "created_at_nano": unano(9, 16),
        "request": {"contract_ids": ["SYN.rb2609"], "raw_symbols": ["SYN.rb2609"],
                    "start_nano": 0, "end_nano": unano(9, 16), "as_of_nano": unano(9, 16),
                    "durations": [60, 86400], "page_size": 5000, "maximum_rows": 200000,
                    "maximum_bytes": 268435456},
        "read_only_transaction": {"isolation": "NOT_APPLICABLE", "read_only": True,
                                  "verified": False},
        "source_hashes": {}, "limitations": ["synthetic_input"],
    }
    publish_bundle(output, payloads, provenance)
    return output


def test_pit_visibility_and_daily_isolation(tmp_path: Path) -> None:
    # A 09:01 bar only becomes visible at 09:03; the 09:02 snapshot has no row.
    bars = minute_bars(9, 5, available_delay_seconds=120)
    daily = c.MarketBar(
        bar_id="D9", contract_id="SYN.rb2609", raw_symbol="SYN.rb2609", product="SYN.rb",
        duration_seconds=86400, datetime_nano=unano(9, 9), start_nano=unano(9, 9),
        end_nano=unano(9, 15), trading_day=date(2026, 1, 9), session_id=None,
        available_at_nano=unano(9, 15, 1), imported_at_nano=unano(9, 15),
        open=Decimal("3900"), high=Decimal("3980"), low=Decimal("3880"),
        close=Decimal("3950"), volume=Decimal("1000"), open_oi=Decimal("180000"),
        close_oi=Decimal("180500"), complete=True, expected_minutes=225,
        actual_minutes=225, source="data-test", quality=quality())
    root = build_package(tmp_path / "pkg", bars=bars + [daily])
    bundle = d.read_frozen_input(root)
    # PIT: the 09:01 bar is only visible from 09:03; at 09:02 it is absent.
    at_two = d.visible_bars(bundle, "SYN.rb2609", 60, unano(9, 9, 2), intraday=False)
    assert [bar.bar_id for bar in at_two] == ["B9-0"]
    at_three = d.visible_bars(bundle, "SYN.rb2609", 60, unano(9, 9, 3, 1), intraday=False)
    assert [bar.bar_id for bar in at_three] == ["B9-0", "B9-1"]
    # Intraday snapshots never see the current trading day's final daily bar...
    assert d.visible_bars(bundle, "SYN.rb2609", 86400, unano(9, 16), intraday=True) == ()
    # ...even though the offline file already carries it; non-intraday sees it.
    daily_visible = d.visible_bars(bundle, "SYN.rb2609", 86400, unano(9, 16), intraday=False)
    assert [bar.bar_id for bar in daily_visible] == ["D9"]
    # On the next trading day the completed daily background becomes legal.
    later_sessions = sessions_for_day(9) + (
        c.SessionWindow(session_id="N12", product="SYN.rb", trading_day=date(2026, 1, 12),
                        start_nano=unano(9, 21), end_nano=unano(9, 23), kind="NIGHT",
                        calendar_version=CALENDAR, evidence_status="SCENARIO"),)
    root2 = build_package(tmp_path / "pkg2", bars=bars + [daily], sessions=later_sessions)
    bundle2 = d.read_frozen_input(root2)
    visible_later = d.visible_bars(bundle2, "SYN.rb2609", 86400, unano(9, 21, 30),
                                   intraday=True)
    assert [bar.bar_id for bar in visible_later] == ["D9"]
    # visible_rule: effective and available -> the rule; not yet available -> None.
    assert d.visible_rule(bundle2, "SYN.rb2609", unano(9, 10)) is not None
    unavailable_rule = c.TradingRule(
        rule_id="RL-2", contract_id="SYN.rb2609", effective_from_nano=unano(5, 9),
        effective_until_nano=unano(30, 15), available_at_nano=unano(10, 9),
        multiplier=Decimal("10"), price_tick=Decimal("1"), lot_step=1, min_open_lots=1,
        open_fee=rule().open_fee, close_fee=rule().close_fee,
        close_today_fee=rule().close_today_fee, long_margin_rate=Decimal("0.09"),
        short_margin_rate=Decimal("0.09"), limit_up=None, limit_down=None,
        slippage_ticks=Decimal("1"), impact_ticks=Decimal("0"),
        participation_rate=Decimal("0.1"), evidence="SCENARIO", source_sha256="a" * 64)
    root4 = build_package(tmp_path / "pkg4", bars=bars, rules=[unavailable_rule])
    assert d.visible_rule(d.read_frozen_input(root4), "SYN.rb2609", unano(9, 10)) is None


def test_missing_rows_and_aggregation_causality(tmp_path: Path) -> None:
    # 09:03 missing -> the 09:00 5-minute aggregate is MISSING with null OHLC.
    bars = minute_bars(9, 10, missing={3})
    root = build_package(tmp_path / "pkg", bars=bars)
    bundle = d.read_frozen_input(root)
    aggregated = k.aggregate_bars(bundle.bars, bundle.sessions, 300, unano(9, 9, 12))
    first = [bar for bar in aggregated if bar.start_nano == unano(9, 9)][0]
    assert first.quality.status == "MISSING"
    assert first.open is None and first.high is None and first.low is None and first.close is None
    assert first.complete is False
    assert first.actual_minutes == 4 and first.expected_minutes == 5
    # The missing slot still counts for horizons (planned grid semantics).
    window = k.horizon_window(unano(9, 9, 2), "SYN.rb",
                              c.Horizon(unit="TRADING_MINUTE", value=5), bundle.sessions)
    assert window == (unano(9, 9, 3), unano(9, 9, 8))

    # Causality: bytes at T stay identical after future rows, a revision and
    # the same-day final daily bar are appended to a fresh package.
    decision = unano(9, 9, 6)
    def snapshot_bytes(b: c.FrozenInput) -> bytes:
        visible = d.visible_bars(b, "SYN.rb2609", 60, decision, intraday=True)
        return s.canonical_json_bytes([s.encode_record(bar) for bar in visible])

    before = snapshot_bytes(bundle)
    extra = minute_bars(9, 30, start_hour=10, start_minute=30)
    revised = minute_bars(9, 5, available_delay_seconds=120)  # later availability
    daily = [c.MarketBar(
        bar_id="D9", contract_id="SYN.rb2609", raw_symbol="SYN.rb2609", product="SYN.rb",
        duration_seconds=86400, datetime_nano=unano(9, 9), start_nano=unano(9, 9),
        end_nano=unano(9, 15), trading_day=date(2026, 1, 9), session_id=None,
        available_at_nano=unano(9, 15, 1), imported_at_nano=unano(9, 15),
        open=Decimal("3900"), high=Decimal("3980"), low=Decimal("3880"),
        close=Decimal("3950"), volume=Decimal("1000"), open_oi=Decimal("1"),
        close_oi=Decimal("1"), complete=True, expected_minutes=225, actual_minutes=225,
        source="data-test", quality=quality())]
    extended = build_package(tmp_path / "extended",
                             bars=bars + extra + revised[5:] + daily)
    after = snapshot_bytes(d.read_frozen_input(extended))
    assert after == before

    # Identical duplicates are idempotent; conflicting duplicates are INVALID.
    duplicate_root = build_package(tmp_path / "dup", bars=bars + list(bars))
    assert d.read_frozen_input(duplicate_root).bars == tuple(
        sorted(bars, key=lambda b: (b.duration_seconds, b.start_nano)))
    conflicting = minute_bars(9, 10, missing={3})
    conflicting[4] = dataclasses.replace(
        conflicting[4], open=Decimal("999"), high=Decimal("1000"), low=Decimal("998"),
        close=Decimal("999"))
    conflicting_root = build_package(tmp_path / "conflict", bars=bars + conflicting)
    with pytest.raises(ArtifactsError):
        d.read_frozen_input(conflicting_root)

    # Malformed packages are rejected: hash mismatch, bad header, traversal.
    manifest = s.parse_json_bytes((root / "manifest.json").read_bytes())
    tampered = dict(manifest)
    tampered["files"] = dict(manifest["files"])
    tampered["files"]["bars.csv"] = "0" * 64
    broken = tmp_path / "broken"
    broken.mkdir()
    for name in ("bars.csv", "instruments.json", "sessions.json", "macro_items.json",
                 "rules.json", "availability.json"):
        (broken / name).write_bytes((root / name).read_bytes())
    (broken / "manifest.json").write_bytes(s.canonical_json_bytes(tampered))
    with pytest.raises(ArtifactsError):
        d.read_frozen_input(broken)
    escaping = dict(tampered)
    escaping["files"]["../secret.json"] = "0" * 64
    (broken / "manifest.json").write_bytes(s.canonical_json_bytes(escaping))
    with pytest.raises(ArtifactsError):
        d.read_frozen_input(broken)
    bad_header = build_package(tmp_path / "bad_header", bars=bars)
    lines = (bad_header / "bars.csv").read_text(encoding="utf-8").splitlines()
    lines[0] = ",".join(reversed(lines[0].split(",")))
    (bad_header / "bars.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ArtifactsError):
        d.read_frozen_input(bad_header)
    with pytest.raises(s.SchemaError):
        s.parse_json_bytes(b'{"schema_version":"a","schema_version":"b"}')


def test_publish_input_collection_merges_shards(tmp_path: Path) -> None:
    first = minute_bars(8, 10)
    second = minute_bars(9, 10)
    shard_a = build_package(tmp_path / "shard-a", bars=first,
                            sessions=sessions_for_day(8), dataset_kind="SYNTHETIC")
    shard_b = build_package(tmp_path / "shard-b", bars=second, dataset_kind="SYNTHETIC")
    collection = d.publish_input_collection((shard_a, shard_b), tmp_path / "collection")
    assert len(collection.bars) == 20
    assert collection.manifest_sha256 == hashlib.sha256(
        (tmp_path / "collection" / "manifest.json").read_bytes()).hexdigest()
    reloaded = d.read_frozen_input(tmp_path / "collection")
    assert reloaded.bars == collection.bars
    assert reloaded.manifest_sha256 == collection.manifest_sha256
    # Deduplication: the same shard twice collapses to one copy.
    again = d.publish_input_collection((shard_a, shard_b, shard_a),
                                       tmp_path / "collection2")
    assert len(again.bars) == 20
    # A conflicting bar key across shards fails the collection.
    conflicting = minute_bars(9, 10)
    conflicting[0] = dataclasses.replace(
        conflicting[0], open=Decimal("777"), high=Decimal("778"), low=Decimal("776"),
        close=Decimal("777"))
    shard_c = build_package(tmp_path / "shard-c", bars=conflicting)
    with pytest.raises(ArtifactsError):
        d.publish_input_collection((shard_b, shard_c), tmp_path / "collection3")
    # Mixed evidence levels never merge.
    pit = build_package(tmp_path / "shard-pit", bars=first, sessions=sessions_for_day(8),
                        dataset_kind="HISTORICAL_PIT")
    with pytest.raises(ArtifactsError):
        d.publish_input_collection((shard_b, pit), tmp_path / "collection4")
    # The collection output directory must be new.
    with pytest.raises(ArtifactsError):
        d.publish_input_collection((shard_a,), tmp_path / "collection")
