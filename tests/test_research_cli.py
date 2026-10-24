import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "research_multistrategy.py"


def run_cli(*args):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", AK_DATABASE_URL="", AK_TEST_DATABASE_URL="")
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)


def test_demo_cli_replays_byte_identically_and_refuses_overwrite(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    completed = run_cli("--demo", "--output", first)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads((first / "report.json").read_text())
    assert report["provenance"]["source_digest"]
    assert report["provenance"]["csv_sha256"]
    assert all(report["summary"][m]["turnover"] > 0 for m in ("dynamic", "fixed"))
    replay = run_cli("--csv", first / "input.csv", "--config", first / "config.json", "--output", second)
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert (first / "report.json").read_bytes() == (second / "report.json").read_bytes()
    before = (first / "report.json").read_bytes()
    assert run_cli("--demo", "--output", first).returncode == 2
    assert (first / "report.json").read_bytes() == before


def test_invalid_csv_config_never_writes_successful_report(tmp_path):
    from ak_trading.research_io import demo_bars, csv_bytes, load_csv
    source = tmp_path / "bad.csv"
    raw = csv_bytes(demo_bars(400))
    source.write_bytes(raw.replace(b"normalized_research", b"raw_continuous", 1))
    with pytest.raises(ValueError):
        load_csv(source)
    output = tmp_path / "out"
    assert run_cli("--csv", source, "--output", output).returncode == 2
    assert not output.exists()
    source.write_bytes(raw)
    config = tmp_path / "bad.json"
    for text in ('{"train_bars": 99}', '{"gross_cap": NaN}', '{"unknown": 1}', '{"train_bars":300,"train_bars":301}'):
        config.write_text(text)
        assert run_cli("--csv", source, "--config", config, "--output", output).returncode == 2
        assert not output.exists()
