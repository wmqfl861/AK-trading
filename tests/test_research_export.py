"""Unit tests for the read-only PostgreSQL research export (no live database)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import count
from pathlib import Path
import json
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine

from ak_trading.market_reference import SHANGHAI_TZ
from ak_trading.persistence.market_data import datetime_from_nano
from ak_trading.persistence.schema import market_bars
from ak_trading.research import ResearchConfig, run_research
from ak_trading.research_io import load_config, load_csv
from ak_trading.research_export import (
    ResearchExportError,
    build_research_universe,
    demo_config_for,
    export_research_input_csv,
    map_research_symbol,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "export_research_input.py"
SYMBOLS = ("KQ.m@SHFE.rb", "KQ.m@CZCE.TA", "KQ.m@DCE.m")
MAPPED = ("CZCE.TA", "DCE.m", "SHFE.rb")
_ID_COUNTER = count(1)


def _nano(local_text: str) -> int:
    local = datetime.fromisoformat(local_text).replace(tzinfo=SHANGHAI_TZ)
    return int(local.timestamp()) * 1_000_000_000


def _session_nanos(day: str, hhmm: str, count: int) -> list[int]:
    start = datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=SHANGHAI_TZ)
    return [
        int((start + timedelta(minutes=i)).timestamp()) * 1_000_000_000
        for i in range(count)
    ]


def _ohlc(index: int, base: float = 3000.0) -> tuple[float, float, float, float]:
    o = base + (index % 7) * 0.5
    c = o + 0.25
    return o, max(o, c) + 0.5, min(o, c) - 0.5, c


def _insert_bars(engine, symbol: str, nanos, *, duration: int = 60, prices=None) -> None:
    # SQLite does not materialize the PostgreSQL Identity() primary key, so the
    # rows carry explicit ids from a process-wide counter.
    rows = []
    for index, nano in enumerate(nanos):
        o, h, l, c = prices[index] if prices else _ohlc(index)
        rows.append(
            {
                "id": next(_ID_COUNTER),
                "symbol": symbol,
                "duration_seconds": duration,
                "datetime_nano": nano,
                "ts": datetime_from_nano(nano),
                "trading_day": None,
                "trading_day_source": "test",
                "open": Decimal(str(o)),
                "high": Decimal(str(h)),
                "low": Decimal(str(l)),
                "close": Decimal(str(c)),
                "volume": Decimal("1"),
                "open_oi": Decimal("1"),
                "close_oi": Decimal("1"),
                "source": "test",
                "batch_id": "batch-1",
            }
        )
    with engine.begin() as connection:
        connection.execute(market_bars.insert(), rows)


@pytest.fixture()
def engine():
    engine = create_engine("sqlite:///:memory:")
    market_bars.create(engine)
    return engine


@pytest.fixture()
def night_engine(engine):
    # One 120-bar synchronized night session plus shorter synchronized day runs.
    for symbol in SYMBOLS:
        _insert_bars(engine, symbol, _session_nanos("2026-06-23", "21:00", 120))
        _insert_bars(engine, symbol, _session_nanos("2026-06-24", "13:30", 45))
        _insert_bars(engine, symbol, _session_nanos("2026-06-24", "09:00", 40))
    return engine


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("KQ.m@SHFE.rb", "SHFE.rb"),
        ("KQ.m@CZCE.TA", "CZCE.TA"),
        ("SHFE.rb2610", "SHFE.rb2610"),
        (" kq.plain ", "kq.plain"),
    ],
)
def test_map_research_symbol_accepts_deterministic_mappings(raw, expected):
    assert map_research_symbol(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "KQ.i@SHFE.rb",  # index continuous series
        "SHFE@rb",  # embedded @ without the main prefix
        "KQ.m@",  # maps to empty
        "KQ.m@KQ.rb",  # maps back onto the rejected KQ. prefix
        "KQ.m@SHFE/rb",  # illegal charset
        "KQ.m@上海rb",  # non-ascii
        "",
        "   ",
    ],
)
def test_map_research_symbol_rejects_unmappable_symbols(raw):
    with pytest.raises(ResearchExportError):
        map_research_symbol(raw)


def test_universe_rejects_duplicates_and_collisions():
    with pytest.raises(ResearchExportError, match="duplicate symbol"):
        build_research_universe(["KQ.m@SHFE.rb", "KQ.m@SHFE.rb"])
    with pytest.raises(ResearchExportError, match="collision"):
        build_research_universe(["KQ.m@SHFE.rb", "SHFE.rb"])
    universe = build_research_universe(list(SYMBOLS))
    assert tuple(universe.values()) == MAPPED


def test_universe_enforces_instrument_bound():
    five = ["KQ.m@SHFE.rb", "KQ.m@CZCE.TA", "KQ.m@DCE.m", "KQ.m@SHFE.cu", "KQ.m@SHFE.ag"]
    with pytest.raises(ResearchExportError, match="between 1 and 4"):
        build_research_universe(five, max_instruments=4)
    assert len(build_research_universe(five, max_instruments=5)) == 5
    with pytest.raises(ResearchExportError):
        build_research_universe([])


def test_close_time_is_open_plus_duration_minus_one_second(night_engine, tmp_path):
    result = export_research_input_csv(
        night_engine, tmp_path / "input.csv", symbols=",".join(SYMBOLS), duration_seconds=60
    )
    lines = (tmp_path / "input.csv").read_text(encoding="utf-8").splitlines()
    assert lines[1].startswith("CZCE.TA,2026-06-23T21:00:00+08:00,2026-06-23T21:00:59+08:00")
    assert lines[3].startswith("SHFE.rb,2026-06-23T21:00:00+08:00,2026-06-23T21:00:59+08:00")
    assert result.frames == 120


def test_longest_gap_free_run_is_selected_with_diagnostics(night_engine, tmp_path):
    result = export_research_input_csv(
        night_engine, tmp_path / "input.csv", symbols=",".join(SYMBOLS), duration_seconds=60
    )
    assert result.segment == {
        "start": "2026-06-23T21:00:00+08:00",
        "end": "2026-06-23T22:59:00+08:00",
        "frames": 120,
    }
    assert result.gap_count >= 1
    listed = [run["frames"] for run in result.runs]
    assert max(listed) == 120 and len(listed) >= 3


def test_missing_instrument_timestamp_splits_the_run(night_engine, tmp_path):
    split = _nano("2026-06-23T22:00:00")
    with night_engine.begin() as connection:
        connection.execute(
            market_bars.delete().where(
                market_bars.c.symbol == "KQ.m@DCE.m",
                market_bars.c.datetime_nano == split,
            )
        )
    result = export_research_input_csv(
        night_engine, tmp_path / "input.csv", symbols=",".join(SYMBOLS), duration_seconds=60
    )
    assert result.frames == 60
    assert result.segment["end"] == "2026-06-23T21:59:00+08:00"


def test_export_round_trips_through_load_csv_and_runs_research(night_engine, tmp_path):
    target = tmp_path / "input.csv"
    result = export_research_input_csv(
        night_engine,
        target,
        symbols=",".join(SYMBOLS),
        duration_seconds=60,
        config_out=tmp_path / "config.json",
    )
    assert result.rows == 360
    bars, _raw = load_csv(target)
    assert len(bars) == 360
    config = load_config(tmp_path / "config.json")
    assert config.train_bars == 100
    report = run_research(bars, config)
    assert report["summary"]["dynamic"]["bars"] == 20


def test_demo_config_arithmetic():
    assert demo_config_for(101) == {"train_bars": 100}
    assert demo_config_for(120) == {"train_bars": 100}
    for frames in (100, 1):
        with pytest.raises(ResearchExportError, match="requires at least 101"):
            demo_config_for(frames)


def test_row_guard_rejects_oversized_export(night_engine, tmp_path):
    with pytest.raises(ResearchExportError, match="row guard exceeded"):
        export_research_input_csv(
            night_engine,
            tmp_path / "input.csv",
            symbols=",".join(SYMBOLS),
            duration_seconds=60,
            max_rows=200,
        )
    assert not (tmp_path / "input.csv").exists()


def test_output_and_config_target_guards(night_engine, tmp_path):
    existing = tmp_path / "existing.csv"
    existing.write_text("x")
    with pytest.raises(ResearchExportError, match="already exists"):
        export_research_input_csv(
            night_engine, existing, symbols=",".join(SYMBOLS), duration_seconds=60
        )
    with pytest.raises(ResearchExportError, match="\\.csv"):
        export_research_input_csv(
            night_engine, tmp_path / "out.txt", symbols=",".join(SYMBOLS), duration_seconds=60
        )
    target = tmp_path / "input.csv"
    export_research_input_csv(
        night_engine,
        target,
        symbols=",".join(SYMBOLS),
        duration_seconds=60,
        config_out=tmp_path / "config.json",
    )
    assert target.exists() and (tmp_path / "config.json").exists()
    with pytest.raises(ResearchExportError, match="already exists"):
        export_research_input_csv(
            night_engine,
            tmp_path / "input2.csv",
            symbols=",".join(SYMBOLS),
            duration_seconds=60,
            config_out=tmp_path / "config.json",
        )
    assert not (tmp_path / "input2.csv").exists()


def test_auto_symbols_prefer_main_continuous_for_duration(night_engine, tmp_path):
    _insert_bars(night_engine, "SHFE.rb2610", _session_nanos("2026-06-25", "21:00", 120))
    _insert_bars(
        night_engine,
        "KQ.m@SHFE.cu",
        [nano + 86_400_000_000_000 for nano in _session_nanos("2026-06-25", "09:00", 5)],
        duration=86400,
    )
    result = export_research_input_csv(
        night_engine, tmp_path / "input.csv", symbols="auto", duration_seconds=60
    )
    assert result.raw_symbols == ("KQ.m@CZCE.TA", "KQ.m@DCE.m", "KQ.m@SHFE.rb")


def test_auto_falls_back_to_plain_symbols_and_daily_duration(engine, tmp_path):
    days = ["2026-06-23", "2026-06-24", "2026-06-25", "2026-06-26", "2026-06-27"]
    nanos = [_nano(f"{day}T15:00:00") for day in days]
    _insert_bars(engine, "SHFE.rb2610", nanos, duration=86400)
    result = export_research_input_csv(
        engine, tmp_path / "daily.csv", symbols="auto", duration_seconds=86400
    )
    assert result.symbols == ("SHFE.rb2610",)
    assert result.frames == 5
    bars, _raw = load_csv(tmp_path / "daily.csv")
    assert bars[0].close_time.isoformat() == "2026-06-24T14:59:59+08:00"


def test_single_instrument_needs_four_frames(engine, tmp_path):
    nanos = _session_nanos("2026-06-23", "21:00", 3)
    _insert_bars(engine, "SHFE.rb2610", nanos)
    with pytest.raises(ResearchExportError, match="required minimum"):
        export_research_input_csv(
            engine, tmp_path / "input.csv", symbols=["SHFE.rb2610"], duration_seconds=60
        )
    _insert_bars(engine, "SHFE.rb2610", _session_nanos("2026-06-23", "21:03", 1))
    result = export_research_input_csv(
        engine, tmp_path / "input2.csv", symbols=["SHFE.rb2610"], duration_seconds=60
    )
    assert result.frames == 4


def test_null_ohlc_is_reported_with_symbol_and_timestamp(night_engine, tmp_path):
    with night_engine.begin() as connection:
        connection.execute(
            market_bars.update()
            .where(
                market_bars.c.symbol == "KQ.m@CZCE.TA",
                market_bars.c.datetime_nano == _nano("2026-06-23T21:30:00"),
            )
            .values(close=None)
        )
    with pytest.raises(ResearchExportError, match="null close for KQ.m@CZCE.TA at 2026-06-23T21:30:00"):
        export_research_input_csv(
            night_engine, tmp_path / "input.csv", symbols=",".join(SYMBOLS), duration_seconds=60
        )
    assert not (tmp_path / "input.csv").exists()


def test_non_positive_price_is_rejected(engine, tmp_path):
    nanos = _session_nanos("2026-06-23", "21:00", 120)
    prices = [_ohlc(i) for i in range(120)]
    prices[10] = (3000.0, 3001.0, 2999.0, 0.0)
    _insert_bars(engine, "SHFE.rb2610", nanos, prices=prices)
    with pytest.raises(ResearchExportError, match="non-finite or non-positive close"):
        export_research_input_csv(
            engine, tmp_path / "input.csv", symbols=["SHFE.rb2610"], duration_seconds=60
        )


def test_consumer_validation_failure_is_wrapped_without_output(night_engine, tmp_path):
    with night_engine.begin() as connection:
        connection.execute(
            market_bars.update()
            .where(
                market_bars.c.symbol == "KQ.m@SHFE.rb",
                market_bars.c.datetime_nano == _nano("2026-06-23T21:30:00"),
            )
            .values(
                open=Decimal("30000"),
                high=Decimal("30001"),
                low=Decimal("29999"),
                close=Decimal("30000.25"),
            )
        )
    with pytest.raises(ResearchExportError, match="strict research contract"):
        export_research_input_csv(
            night_engine, tmp_path / "input.csv", symbols=",".join(SYMBOLS), duration_seconds=60
        )
    assert not (tmp_path / "input.csv").exists()


def run_cli(*args):
    env = dict(
        os.environ,
        PYTHONDONTWRITEBYTECODE="1",
        AK_DATABASE_URL="",
        AK_TEST_DATABASE_URL="",
    )
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_without_database_settings_fails_cleanly(tmp_path):
    target = tmp_path / "input.csv"
    completed = run_cli(
        "--symbols", "KQ.m@SHFE.rb", "--duration", "60", "--output", target
    )
    assert completed.returncode == 2
    payload = json.loads(completed.stdout)
    assert payload["status"] == "error"
    assert not target.exists()


@pytest.mark.parametrize(
    "args",
    [
        ("--duration", "30", "--output", "x.csv"),
        ("--symbols", "", "--duration", "60", "--output", "x.csv"),
        ("--duration", "60", "--output", "x.txt"),
    ],
)
def test_cli_rejects_invalid_arguments(tmp_path, args):
    completed = run_cli(*args)
    assert completed.returncode == 2
    stream = completed.stdout.strip() or completed.stderr.strip()
    assert json.loads(stream)["status"] == "error"
