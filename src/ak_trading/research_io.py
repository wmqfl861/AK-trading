"""Strict offline inputs and reproducible artifacts for normalized research."""
from __future__ import annotations

import csv
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import math
from pathlib import Path

from ak_trading.research import Bar, ResearchConfig, digest, validate_bars

COLUMNS = ("symbol", "open_time", "close_time", "open", "high", "low", "close", "series_kind")
MAX_CSV_BYTES = 32 * 1024 * 1024


def demo_bars(count: int = 720) -> list[Bar]:
    """Deterministic synthetic paths, not a fitted market model or profit evidence."""
    if isinstance(count, bool) or not isinstance(count, int) or not 4 <= count <= 10000:
        raise ValueError("demo count must be 4..10000")
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    prices = {"DEMO_A": 100.0, "DEMO_B": 200.0}
    result = []
    for t in range(count):
        for index, symbol in enumerate(prices):
            op = prices[symbol] * (1 + 0.0002 * math.sin(t * 0.3 + index))
            if (t // 100 + index) % 2 == 0:
                ret = (0.003 + 0.001 * math.sin(t * 1.7)) * (1 if (t // 200) % 2 == 0 else -1)
            else:
                ret = 0.01 * math.sin(t * 1.2 + index)
            cl = op * (1 + ret)
            result.append(Bar(symbol, start + timedelta(hours=t), start + timedelta(hours=t, minutes=59),
                              op, max(op, cl) * 1.0005, min(op, cl) * 0.9995, cl))
            prices[symbol] = cl
    return result


def csv_bytes(bars: list[Bar]) -> bytes:
    validate_bars(bars)
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(b.record() for b in bars)
    return stream.getvalue().encode("utf-8")


def load_csv(path: Path) -> tuple[list[Bar], bytes]:
    if path.suffix.lower() != ".csv" or path.stat().st_size > MAX_CSV_BYTES:
        raise ValueError("input must be a CSV no larger than 32 MiB")
    raw = path.read_bytes()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    if reader.fieldnames != list(COLUMNS):
        raise ValueError("CSV header must exactly match normalized research schema")
    bars = []
    for row in reader:
        if len(bars) >= 200000 or None in row or any(v is None or not v.strip() for v in row.values()):
            raise ValueError("invalid CSV row or row limit exceeded")
        bars.append(Bar(row["symbol"], datetime.fromisoformat(row["open_time"]), datetime.fromisoformat(row["close_time"]),
                        *(float(row[k]) for k in ("open", "high", "low", "close")), row["series_kind"]))
    validate_bars(bars)
    return bars, raw


def load_config(path: Path | None) -> ResearchConfig:
    if path is None:
        return ResearchConfig()
    if path.suffix.lower() != ".json" or path.stat().st_size > 65536:
        raise ValueError("config must be a JSON object no larger than 64 KiB")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate config key")
            result[key] = value
        return result
    def invalid_constant(_):
        raise ValueError("nonfinite JSON constant")
    parsed = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs, parse_constant=invalid_constant)
    if not isinstance(parsed, dict):
        raise ValueError("config must be an object")
    return ResearchConfig(**parsed)


def write_bundle(output: Path, raw: bytes, report: dict) -> None:
    """Create an exclusive directory after validation; never overwrite any files."""
    root = Path(__file__).resolve().parents[2]
    source_files = [Path(__file__).resolve(), Path(__file__).with_name("research.py"), root / "scripts" / "research_multistrategy.py"]
    sources = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}
    report["provenance"] = {"csv_sha256": hashlib.sha256(raw).hexdigest(), "config_digest": digest(report["config"]),
                            "source_digest": digest(sources), "source_files": sources,
                            "input_contract": "strict synchronized normalized OHLC; not validated exchange futures feed"}
    encoded = (json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    config = (json.dumps(report["config"], sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    output.mkdir(parents=True, exist_ok=False)
    for name, content in (("input.csv", raw), ("config.json", config), ("report.json", encoded)):
        with (output / name).open("xb") as stream:
            stream.write(content)
