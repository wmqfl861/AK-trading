import ast
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "preopen_market_report.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("test_preopen_market_report_cli", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(*arguments: str, env: dict[str, str] | None = None):
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    if env:
        environment.update(env)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=environment,
    )


def test_offline_cli_writes_three_product_report_without_credentials(tmp_path: Path):
    secret_user = "DO_NOT_PRINT_USER_42"
    secret_pass = "DO_NOT_PRINT_PASS_42"

    result = _run(
        "--offline",
        "--now",
        "2026-07-28T08:00:00+08:00",
        "--output-dir",
        str(tmp_path),
        "--trade-price",
        "RB=3100",
        "--settlement-price",
        "RB=3095",
        env={"TQ_USER": secret_user, "TQ_PASS": secret_pass},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert secret_user not in result.stdout + result.stderr
    assert secret_pass not in result.stdout + result.stderr
    payload = json.loads(
        (tmp_path / "2026-07-28" / "preopen.json").read_text(encoding="utf-8")
    )
    assert payload["expected_products"] == ["RB", "TA", "M"]
    assert [product["code"] for product in payload["products"]] == ["RB", "TA", "M"]
    assert payload["notice_check"]["status"] == "offline_not_checked"
    assert payload["products"][0]["fees"]["actions"]["open"]["total_cny"] == "3.11"


def test_cli_after_close_targets_next_trading_day(tmp_path: Path):
    result = _run(
        "--offline",
        "--now",
        "2026-07-28T20:30:00+08:00",
        "--output-dir",
        str(tmp_path),
    )

    assert result.returncode == 0
    assert (tmp_path / "2026-07-29" / "preopen.json").exists()


def test_cli_returns_error_but_preserves_product_evidence_when_source_expired(
    tmp_path: Path,
):
    result = _run(
        "--offline",
        "--trading-day",
        "2026-08-05",
        "--now",
        "2026-08-05T08:00:00+08:00",
        "--output-dir",
        str(tmp_path),
    )

    assert result.returncode == 2
    payload = json.loads(
        (tmp_path / "2026-08-05" / "preopen.json").read_text(encoding="utf-8")
    )
    assert payload["status"] == "error"
    assert len(payload["products"]) == 3
    assert all(product["errors"] for product in payload["products"])


def test_duplicate_price_fails_before_writing(tmp_path: Path):
    result = _run(
        "--offline",
        "--now",
        "2026-07-28T08:00:00+08:00",
        "--output-dir",
        str(tmp_path),
        "--trade-price",
        "RB=3100",
        "--trade-price",
        "RB=3101",
    )

    assert result.returncode == 2
    assert "duplicate --trade-price for RB" in result.stdout
    assert not list(tmp_path.rglob("preopen.json"))


def test_offline_flag_never_invokes_notice_checker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = _load_script()

    def unexpected_check(*args, **kwargs):
        pytest.fail("notice checker must not run in offline mode")

    monkeypatch.setattr(module, "check_official_notices", unexpected_check)

    assert (
        module.main(
            [
                "--offline",
                "--now",
                "2026-07-28T08:00:00+08:00",
                "--output-dir",
                str(tmp_path),
            ]
        )
        == 0
    )


def test_new_notice_status_returns_review_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    module = _load_script()
    monkeypatch.setattr(
        module,
        "check_official_notices",
        lambda *args, **kwargs: {
            "status": "review_required",
            "checked_at": "2026-07-29T08:00:00+08:00",
            "products": [],
        },
    )

    result = module.main(
        [
            "--now",
            "2026-07-29T08:00:00+08:00",
            "--output-dir",
            str(tmp_path),
        ]
    )

    assert result == 1
    payload = json.loads(
        (tmp_path / "2026-07-29" / "preopen.json").read_text(encoding="utf-8")
    )
    assert payload["status"] == "review_required"


def _project_import_closure(paths: list[Path]) -> tuple[set[str], set[Path]]:
    pending = list(paths)
    visited_paths: set[Path] = set()
    imported: set[str] = set()
    while pending:
        path = pending.pop()
        if path in visited_paths:
            continue
        visited_paths.add(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                imported.add(name)
                if name.startswith("ak_trading."):
                    relative = Path(*name.split("."))
                    candidate = ROOT / "src" / relative.with_suffix(".py")
                    if candidate.exists():
                        pending.append(candidate)
    return imported, visited_paths


def test_transitive_import_graph_has_no_sdk_credentials_or_http_clients():
    imported, paths = _project_import_closure([SCRIPT])
    forbidden = {
        "tqsdk",
        "requests",
        "httpx",
        "urllib.request",
        "ak_trading.config",
        "ak_trading.session",
    }

    assert not (imported & forbidden)
    combined_source = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert "TQ_USER" not in combined_source
    assert "TQ_PASS" not in combined_source
