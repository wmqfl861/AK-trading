from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def disable_application_database_for_tests():
    """Keep unit and CLI tests from inheriting the production database."""
    variable = "AK_DATABASE_URL"
    previous = os.environ.get(variable)
    os.environ[variable] = ""
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(variable, None)
        else:
            os.environ[variable] = previous


# --- W2 acquisition collector fixtures (appended by ACQ-W2 migration r1) ---
# Source: data/maintenance_artifacts/acquisition-w2-collector-20260919-r1/
# tests/conftest.py (sha256 233ef4225b51cf00529e93ecebcf997e015ab88510fdb8e80dab3152d8e82551).
# The original self-contained sys.path bootstrap was dropped: repository
# pyproject.toml already sets pythonpath = ["src"], so ak_trading.acquisition
# resolves without path mutation. The three fixtures below are byte-equivalent
# in behavior; only the FROZEN_POLICY/FIXTURES location constants were
# re-pointed at the repository layout.

REPO_ROOT = Path(__file__).resolve().parents[1]
FROZEN_POLICY = REPO_ROOT / "config" / "execution-policy.frozen.json"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def frozen_policy_path() -> Path:
    return FROZEN_POLICY


@pytest.fixture(scope="session")
def synthetic_inventory_payload():
    import json

    return json.loads((FIXTURES / "synthetic_inventory.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def synthetic_calendar_payload():
    import json

    return json.loads(
        (FIXTURES / "synthetic_calendar_evidence.json").read_text(encoding="utf-8")
    )
