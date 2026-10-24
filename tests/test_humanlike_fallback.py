from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import ak_trading.exchange_rule_sources as source_module
from ak_trading.exchange_rule_sources import (
    DocumentFetchError,
    LocalWebSearchDocumentFetcher,
)


ROOT = Path(__file__).resolve().parents[1]
URL = "https://www.dce.com.cn/dalianshangpin/sspz/487180/index.html"
HOMEPAGE = "http://www.dce.com.cn/dalianshangpin/sspz/index.html"
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def case_root():
    path = Path(tempfile.mkdtemp(prefix="_review_tmp_humanlike_", dir=ROOT))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _completed(payload: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["websearch"],
        returncode=0,
        stdout=json.dumps(payload, ensure_ascii=False),
        stderr="",
    )


def _empty_websearch_response() -> subprocess.CompletedProcess[str]:
    return _completed(
        {
            "success": True,
            "data": {
                "markdown": "",
                "metadata": {"sourceURL": URL, "title": "DCE"},
            },
        }
    )


class _Response:
    def __init__(self, status: int, text: str = "") -> None:
        self.status = status
        self.text = text


class _Session:
    instances: list["_Session"] = []
    responses: list[_Response] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.started = False
        self.closed = False
        type(self).instances.append(self)

    def start(self) -> None:
        self.started = True

    def fetch(self, url: str, **kwargs: object) -> _Response:
        self.calls.append((url, kwargs))
        return type(self).responses.pop(0)

    def close(self) -> None:
        self.closed = True


def _fetcher(tmp_path: Path, *, sleep_calls: list[float] | None = None):
    return LocalWebSearchDocumentFetcher(
        executable="websearch",
        runner=lambda command, timeout: _empty_websearch_response(),
        now=lambda: NOW,
        exchange="DCE",
        exchange_homepage=HOMEPAGE,
        official_domains=("dce.com.cn",),
        profile_root=tmp_path / "profiles",
        sleep_fn=(sleep_calls.append if sleep_calls is not None else lambda _: None),
    )


def test_humanlike_success_retains_document_and_observability(
    case_root: Path, monkeypatch: pytest.MonkeyPatch
):
    _Session.instances = []
    _Session.responses = [_Response(200), _Response(200, "DCE official rule content")]
    monkeypatch.setattr(source_module, "_load_scrapling_session", lambda: _Session)
    monkeypatch.setattr(source_module.shutil, "which", lambda name: None)
    sleeps: list[float] = []

    document = _fetcher(case_root, sleep_calls=sleeps).fetch(URL)

    payload = json.loads(document.content)
    session = _Session.instances[0]
    assert [call[0] for call in session.calls] == [HOMEPAGE, URL]
    assert session.calls[0][1] == {"network_idle": True, "wait": 4000}
    assert session.calls[1][1]["network_idle"] is True
    assert session.calls[1][1]["wait"] == 2500
    assert session.calls[1][1]["page_action"] is source_module.human_scroll
    assert session.kwargs == {
        "headless": True,
        "real_chrome": True,
        "user_data_dir": str(case_root / "profiles" / "DCE"),
        "block_webrtc": True,
        "hide_canvas": True,
        "locale": "zh-CN",
        "timeout": 90000,
        "google_search": False,
    }
    assert sleeps == [3.5]
    assert session.started is True
    assert session.closed is True
    assert (case_root / "profiles" / "DCE").is_dir()
    assert payload["markdown"] == "DCE official rule content"
    assert payload["metadata"]["fetcher"] == "scrapling_humanlike"
    assert payload["humanlike_retry_attempted"] is True
    assert payload["humanlike_retry_error"] is None


def test_scrapling_unavailable_preserves_error_observability(
    case_root: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(source_module, "_load_scrapling_session", lambda: None)
    monkeypatch.setattr(
        source_module, "_SCRAPLING_PYTHON", ROOT / "_no_such_scrapling_python_.exe"
    )
    monkeypatch.setattr(source_module.shutil, "which", lambda name: None)
    fetcher = _fetcher(case_root)

    with pytest.raises(DocumentFetchError, match="scrapling-unavailable") as caught:
        fetcher.fetch(URL)

    assert caught.value.humanlike_retry_attempted is True
    assert caught.value.humanlike_retry_error == "scrapling-unavailable"
    assert caught.value.observability == {
        "humanlike_retry_attempted": True,
        "humanlike_retry_error": "scrapling-unavailable",
    }
    assert fetcher.last_observability == caught.value.observability


def test_homepage_non_200_stops_before_target_and_records_reason(
    case_root: Path, monkeypatch: pytest.MonkeyPatch
):
    _Session.instances = []
    _Session.responses = [_Response(412)]
    monkeypatch.setattr(source_module, "_load_scrapling_session", lambda: _Session)
    monkeypatch.setattr(source_module.shutil, "which", lambda name: None)

    with pytest.raises(DocumentFetchError, match="homepage-status-412") as caught:
        _fetcher(case_root).fetch(URL)

    assert caught.value.humanlike_retry_error == "homepage-status-412"
    assert [call[0] for call in _Session.instances[0].calls] == [HOMEPAGE]


def test_empty_target_content_records_empty_content(
    case_root: Path, monkeypatch: pytest.MonkeyPatch
):
    _Session.instances = []
    _Session.responses = [_Response(200), _Response(200, " \n\t")]
    monkeypatch.setattr(source_module, "_load_scrapling_session", lambda: _Session)
    monkeypatch.setattr(source_module.shutil, "which", lambda name: None)

    with pytest.raises(DocumentFetchError, match="empty-content") as caught:
        _fetcher(case_root).fetch(URL)

    assert caught.value.humanlike_retry_error == "empty-content"
    assert [call[0] for call in _Session.instances[0].calls] == [HOMEPAGE, URL]


def test_waf_challenge_markdown_falls_through_to_humanlike(
    case_root: Path, monkeypatch: pytest.MonkeyPatch
):
    challenge = "$_ts=window['$_ts'];if(!$_ts)$_ts={};$_ts.nsd=78297;$_ts.cd='obfuscated';"
    _Session.instances = []
    _Session.responses = [
        _Response(200),
        _Response(200, "交易单位 10吨/手\n最低交易保证金 合约价值的5%"),
    ]
    monkeypatch.setattr(source_module, "_load_scrapling_session", lambda: _Session)
    monkeypatch.setattr(source_module.shutil, "which", lambda name: None)

    document = _fetcher(case_root).fetch(URL)

    payload = json.loads(document.content)
    assert "10吨/手" in payload["markdown"]
    assert challenge not in payload["markdown"]
    assert payload["humanlike_retry_attempted"] is True
    assert payload["humanlike_retry_error"] is None
    assert [call[0] for call in _Session.instances[0].calls] == [HOMEPAGE, URL]


def test_waf_challenge_without_humanlike_raises_empty_content(
    case_root: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(source_module, "_load_scrapling_session", lambda: None)
    monkeypatch.setattr(
        source_module, "_SCRAPLING_PYTHON", ROOT / "_no_such_scrapling_python_.exe"
    )
    monkeypatch.setattr(source_module.shutil, "which", lambda name: None)

    fetcher = LocalWebSearchDocumentFetcher(
        executable="websearch",
        runner=lambda command, timeout: _completed(
            {
                "success": True,
                "data": {
                    "markdown": "$_ts=window['$_ts'];$_ts.cd='blocked';",
                    "metadata": {"sourceURL": URL, "title": "DCE"},
                },
            }
        ),
        now=lambda: NOW,
        exchange="DCE",
        exchange_homepage=HOMEPAGE,
        official_domains=("dce.com.cn",),
        profile_root=case_root / "profiles",
    )

    with pytest.raises(DocumentFetchError, match="empty normalized content"):
        fetcher.fetch(URL)


def test_humanlike_subprocess_fallback_used_when_inprocess_unavailable(    case_root: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(source_module, "_load_scrapling_session", lambda: None)
    fake_python = case_root / "scrapling_python.exe"
    fake_python.write_bytes(b"")
    monkeypatch.setattr(source_module, "_SCRAPLING_PYTHON", fake_python)
    monkeypatch.setattr(source_module.shutil, "which", lambda name: None)

    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)

        class Completed:
            returncode = 0
            stdout = json.dumps(
                {"home_status": 200, "status": 200, "text": "交易单位 10吨/手"},
                ensure_ascii=False,
            )
            stderr = ""

        return Completed()

    monkeypatch.setattr(source_module.subprocess, "run", fake_run)

    document = _fetcher(case_root).fetch(URL)

    payload = json.loads(document.content)
    assert "10吨/手" in payload["markdown"]
    assert payload["humanlike_retry_attempted"] is True
    assert payload["humanlike_retry_error"] is None
    assert commands and str(fake_python) in commands[0]


def test_normal_markdown_never_attempts_humanlike(
    case_root: Path, monkeypatch: pytest.MonkeyPatch
):
    called = False

    def unavailable_loader():
        nonlocal called
        called = True
        raise AssertionError("human-like fallback must not run for usable markdown")

    monkeypatch.setattr(source_module, "_load_scrapling_session", unavailable_loader)
    monkeypatch.setattr(
        source_module, "_SCRAPLING_PYTHON", ROOT / "_no_such_scrapling_python_.exe"
    )

    document = LocalWebSearchDocumentFetcher(
        executable="websearch",
        runner=lambda command, timeout: _completed(
            {
                "success": True,
                "data": {
                    "markdown": "# Existing normalized content",
                    "metadata": {"sourceURL": URL, "title": "DCE"},
                },
            }
        ),
        now=lambda: NOW,
        exchange_homepage=HOMEPAGE,
    ).fetch(URL)

    payload = json.loads(document.content)
    assert payload["markdown"] == "# Existing normalized content"
    assert "humanlike_retry_attempted" not in payload
    assert called is False


def test_catalog_collection_parameter_and_cli_flag_disable_humanlike(
    monkeypatch: pytest.MonkeyPatch, case_root: Path
):
    script_path = ROOT / "scripts" / "collect_exchange_rules.py"
    spec = importlib.util.spec_from_file_location("test_humanlike_collect_cli", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    captured: dict[str, object] = {}

    def fake_collect(**kwargs: object):
        captured.update(kwargs)
        return SimpleNamespace(
            run={
                "run_id": "run",
                "status": "success",
                "target_trading_day": "2026-08-20",
            },
            snapshot=None,
            previous_snapshot=None,
            changes=(),
            run_path=case_root / "run.json",
            documents_written=(),
            snapshot_path=None,
            catalog_run=None,
            catalog_run_path=None,
            exit_code=0,
        )

    monkeypatch.setattr(module, "collect_exchange_rules_from_catalog", fake_collect)
    assert module.main(
        [
            "--artifact-root",
            str(case_root / "artifacts"),
            "--catalog",
            str(ROOT / "config" / "china_futures_catalog.json"),
            "--now",
            "2026-08-20T20:30:00+08:00",
            "--trading-day",
            "2026-08-20",
            "--no-humanlike",
        ]
    ) == 0
    assert captured["humanlike_enabled"] is False
