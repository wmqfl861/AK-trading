"""compute_last_session.py CLI scenarios against the bundled catalog."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "compute_last_session.py"
CATALOG = ROOT / "config" / "china_futures_catalog.json"

pytestmark = pytest.mark.skipif(
    not CATALOG.exists(), reason="china_futures_catalog.json not present"
)


def run_cli(now: str) -> str:
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPT), "--now", now],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_weekend_falls_back_to_friday():
    assert run_cli("2026-09-12T19:00:00+08:00") == "2026-09-11"


def test_weekday_after_close_is_same_day():
    assert run_cli("2026-09-10T18:00:00+08:00") == "2026-09-10"


def test_morning_before_close_uses_previous_session():
    assert run_cli("2026-09-14T10:00:00+08:00") == "2026-09-11"


def test_holiday_falls_back_to_last_session():
    assert run_cli("2026-10-01T18:00:00+08:00") == "2026-09-30"


def test_sessions_boundary_minutes():
    assert run_cli("2026-09-11T14:59:00+08:00") == "2026-09-10"
    assert run_cli("2026-09-11T15:00:00+08:00") == "2026-09-11"


def test_naive_now_is_treated_as_cst():
    assert run_cli("2026-09-12T19:00:00") == "2026-09-11"


def test_missing_catalog_exits_nonzero(tmp_path):
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPT),
         "--now", "2026-09-12T19:00:00+08:00", "--catalog", str(tmp_path / "nope.json")],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT),
    )
    assert result.returncode == 1
    assert result.stdout.strip() == ""
