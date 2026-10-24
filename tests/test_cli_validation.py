import importlib.util
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("TQ_USER", None)
    environment.pop("TQ_PASS", None)
    environment["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=environment,
    )


def _load_script(name: str):
    script = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return script, module


def _project_import_closure(paths: list[Path]) -> set[str]:
    pending = list(paths)
    visited: set[Path] = set()
    imported: set[str] = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
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
                    candidate = ROOT / "src" / Path(*name.split(".")).with_suffix(".py")
                    if candidate.exists():
                        pending.append(candidate)
    return imported


def test_sim_rejects_duration_before_loading_credentials():
    result = _run("scripts/run_sim_strategy.py", "--duration", "0")

    assert result.returncode == 2
    assert "duration must be a positive integer" in result.stdout
    assert "Missing TQ_USER" not in result.stdout


def test_backtest_rejects_invalid_lot_cap_before_loading_credentials():
    result = _run(
        "scripts/backtest_strategy.py",
        "--symbol",
        "DCE.m2609",
        "--start",
        "2026-01-05",
        "--end",
        "2026-01-30",
        "--max-abs-position",
        "0",
    )

    assert result.returncode == 2
    assert "max_abs_position must be a positive integer" in result.stdout
    assert "Missing TQ_USER" not in result.stdout


def test_backtest_api_creation_failure_writes_incomplete_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    script, module = _load_script("backtest_strategy")

    journal_path = tmp_path / "failed.jsonl"
    report_path = tmp_path / "failed.summary.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(script),
            "--symbol",
            "DCE.m2609",
            "--start",
            "2026-01-05",
            "--end",
            "2026-01-30",
            "--journal",
            str(journal_path),
            "--report",
            str(report_path),
        ],
    )
    monkeypatch.setattr(
        module,
        "load_settings",
        lambda: SimpleNamespace(tq_user="user-a", tq_pass="pass-a"),
    )
    monkeypatch.setattr(module, "TqSim", lambda **kwargs: object())
    monkeypatch.setattr(module, "TqBacktest", lambda **kwargs: object())

    def fail_create_api(*args, **kwargs):
        raise RuntimeError("authentication failed for user-a with pass-a")

    monkeypatch.setattr(module, "create_api", fail_create_api)

    assert module.main() == 2
    output = capsys.readouterr().out
    assert "user-a" not in output
    assert "pass-a" not in output
    assert output.count("[REDACTED]") == 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["integrity"]["manifest_count"] == 1
    assert report["integrity"]["run_started_count"] == 0
    assert report["lifecycle"]["state"] == "incomplete"


def test_cli_rejects_same_journal_and_report_before_credentials(tmp_path: Path):
    collision = str(tmp_path / "collision.jsonl")
    sim = _run(
        "scripts/run_sim_strategy.py",
        "--journal",
        collision,
        "--report",
        collision,
    )
    backtest = _run(
        "scripts/backtest_strategy.py",
        "--symbol",
        "DCE.m2609",
        "--start",
        "2026-01-05",
        "--end",
        "2026-01-30",
        "--journal",
        collision,
        "--report",
        collision,
    )

    for result in (sim, backtest):
        assert result.returncode == 2
        assert "journal and report paths must be different" in result.stdout
        assert "Missing TQ_USER" not in result.stdout
    assert not Path(collision).exists()


def test_sim_rejects_blank_resolved_symbol_before_api(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    script, module = _load_script("run_sim_strategy")
    monkeypatch.setattr(sys, "argv", [str(script)])
    monkeypatch.setattr(
        module,
        "load_settings",
        lambda: SimpleNamespace(
            tq_user="user-a", tq_pass="pass-a", default_symbol="   "
        ),
    )

    def unexpected_create_api(*args, **kwargs):
        pytest.fail("create_api must not run for a blank resolved symbol")

    monkeypatch.setattr(module, "create_api", unexpected_create_api)

    assert module.main() == 2
    assert "symbol is required" in capsys.readouterr().out


def test_sim_keyboard_interrupt_returns_nonzero_after_clean_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    script, module = _load_script("run_sim_strategy")
    journal_path = tmp_path / "interrupt.jsonl"
    report_path = tmp_path / "interrupt.summary.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(script),
            "--symbol",
            "SHFE.rb2610",
            "--journal",
            str(journal_path),
            "--report",
            str(report_path),
        ],
    )
    monkeypatch.setattr(
        module,
        "load_settings",
        lambda: SimpleNamespace(
            tq_user="user-a", tq_pass="pass-a", default_symbol="SHFE.rb2610"
        ),
    )

    quote = SimpleNamespace(
        datetime="2026-07-23 09:00:00",
        ins_class="FUTURE",
        underlying_symbol="",
        instrument_id="SHFE.rb2610",
        expired=False,
        last_price=100.0,
    )

    class FakeApi:
        def __init__(self):
            self.closed = False

        def get_quote(self, symbol):
            return quote

        def close(self):
            self.closed = True

    api = FakeApi()
    monkeypatch.setattr(module, "create_api", lambda *args, **kwargs: api)
    monkeypatch.setattr(module, "wait_until", lambda *args, **kwargs: True)

    class InterruptingRunner:
        def __init__(self, *args, **kwargs):
            pass

        def run(self):
            raise KeyboardInterrupt

        def shutdown_live(self, *, timeout):
            return SimpleNamespace(flattened=True, task_finished=True)

    monkeypatch.setattr(module, "StrategyRunner", InterruptingRunner)

    assert module.main() == 130
    assert api.closed
    assert report_path.exists()


def test_backtest_database_failure_occurs_after_file_manifest_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    script, module = _load_script("backtest_strategy")
    journal_path = tmp_path / "file-first.jsonl"
    report_path = tmp_path / "file-first.summary.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(script),
            "--symbol",
            "DCE.m2609",
            "--start",
            "2026-01-05",
            "--end",
            "2026-01-06",
            "--journal",
            str(journal_path),
            "--report",
            str(report_path),
        ],
    )
    monkeypatch.setattr(
        module,
        "load_settings",
        lambda: SimpleNamespace(tq_user="user-a", tq_pass="pass-a"),
    )
    database_settings = SimpleNamespace(secrets=())
    monkeypatch.setattr(module, "load_database_settings", lambda: database_settings)

    class FakeEngine:
        def __init__(self):
            self.disposed = False

        def dispose(self):
            self.disposed = True

    engine = FakeEngine()
    monkeypatch.setattr(module, "create_database_engine", lambda settings: engine)

    def database_sink(event):
        rows = [
            json.loads(line)
            for line in journal_path.read_text(encoding="utf-8").splitlines()
        ]
        assert rows == [event]
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(module, "PostgresJournal", lambda *args, **kwargs: database_sink)
    monkeypatch.setattr(
        module,
        "TqSim",
        lambda **kwargs: pytest.fail("TqSim must not start after database failure"),
    )

    assert module.main() == 2
    assert engine.disposed is True
    assert "JOURNAL_SETUP_FAIL" in capsys.readouterr().out
    assert journal_path.exists()
    assert not report_path.exists()


def test_sim_database_import_starts_only_after_shutdown_close_and_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    script, module = _load_script("run_sim_strategy")
    journal_path = tmp_path / "post-shutdown.jsonl"
    report_path = tmp_path / "post-shutdown.summary.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(script),
            "--symbol",
            "SHFE.rb2610",
            "--journal",
            str(journal_path),
            "--report",
            str(report_path),
        ],
    )
    monkeypatch.setattr(
        module,
        "load_settings",
        lambda: SimpleNamespace(
            tq_user="user-a", tq_pass="pass-a", default_symbol="SHFE.rb2610"
        ),
    )
    database_settings = SimpleNamespace(secrets=())
    monkeypatch.setattr(module, "load_database_settings", lambda: database_settings)
    order = []
    quote = SimpleNamespace(
        datetime="2026-07-23 09:00:00",
        ins_class="FUTURE",
        underlying_symbol="",
        instrument_id="SHFE.rb2610",
        expired=False,
        last_price=100.0,
    )

    class FakeApi:
        closed = False

        def get_quote(self, symbol):
            return quote

        def close(self):
            self.closed = True
            order.append("api_close")

    api = FakeApi()
    monkeypatch.setattr(module, "create_api", lambda *args, **kwargs: api)
    monkeypatch.setattr(module, "wait_until", lambda *args, **kwargs: True)

    class CompletingRunner:
        def __init__(self, *args, **kwargs):
            self.sink = kwargs["decision_sink"]
            self.run_id = kwargs["run_id"]

        def run(self):
            assert order == []
            self.sink(
                {
                    "event": "run_started",
                    "run_id": self.run_id,
                    "mode": "sim",
                    "symbol": "SHFE.rb2610",
                }
            )
            self.sink(
                {
                    "event": "run_finished",
                    "run_id": self.run_id,
                    "mode": "sim",
                    "symbol": "SHFE.rb2610",
                    "reason": "test",
                }
            )
            return SimpleNamespace(reason="test")

        def shutdown_live(self, *, timeout):
            assert order == []
            self.sink(
                {
                    "event": "shutdown",
                    "run_id": self.run_id,
                    "mode": "sim",
                    "symbol": "SHFE.rb2610",
                    "flattened": True,
                    "task_finished": True,
                }
            )
            order.append("shutdown")
            return SimpleNamespace(flattened=True, task_finished=True)

    monkeypatch.setattr(module, "StrategyRunner", CompletingRunner)

    class FakeEngine:
        def dispose(self):
            order.append("database_dispose")

    def create_database_engine(settings):
        assert order == ["shutdown", "api_close"]
        assert report_path.exists()
        order.append("database_connect")
        return FakeEngine()

    def import_artifacts(engine, journal, report, *, settings):
        assert order == ["shutdown", "api_close", "database_connect"]
        assert Path(journal) == journal_path
        assert Path(report) == report_path
        order.append("database_import")
        return SimpleNamespace(events_read=4), SimpleNamespace(identity="report-id")

    monkeypatch.setattr(module, "create_database_engine", create_database_engine)
    monkeypatch.setattr(module, "import_run_artifacts", import_artifacts)

    assert module.main() == 0
    assert order == [
        "shutdown",
        "api_close",
        "database_connect",
        "database_import",
        "database_dispose",
    ]


def test_recent_snapshot_rejects_bounds_before_credentials_or_tqsdk():
    result = _run(
        "scripts/snapshot_recent_market_data.py",
        "--symbol",
        "KQ.m@SHFE.rb",
        "--duration",
        "60",
        "--rows",
        "10001",
    )

    assert result.returncode == 2
    assert "--rows must be between 1 and 10000" in result.stdout
    assert "Missing TQ_USER" not in result.stdout
    assert "tqsdk" not in result.stderr.lower()


def test_query_cli_rejects_naive_time_before_database_configuration():
    result = _run(
        "scripts/query_market_data.py",
        "list",
        "--symbol",
        "KQ.m@SHFE.rb",
        "--duration",
        "60",
        "--start",
        "2026-07-29T09:01:00",
    )

    assert result.returncode == 2
    assert "timezone offset" in result.stdout
    assert "AK_DATABASE_URL is required" not in result.stdout


def test_query_cli_import_closure_has_no_tqsdk_or_trading_credentials():
    script = ROOT / "scripts" / "query_market_data.py"
    imported = _project_import_closure([script])

    assert "tqsdk" not in imported
    assert "ak_trading.config" not in imported
    assert "ak_trading.session" not in imported
    combined = "\n".join(
        [
            script.read_text(encoding="utf-8"),
            (ROOT / "src" / "ak_trading" / "persistence" / "query.py").read_text(
                encoding="utf-8"
            ),
        ]
    )
    assert "TQ_USER" not in combined
    assert "TQ_PASS" not in combined


def test_download_plain_permission_exception_is_controlled_and_redacted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _, module = _load_script("download_market_data")
    tq_settings = SimpleNamespace(tq_user="user-a", tq_pass="pass-a")
    db_settings = SimpleNamespace(secrets=())
    monkeypatch.setattr(module, "load_settings", lambda: tq_settings)
    monkeypatch.setattr(module, "load_database_settings", lambda **kwargs: db_settings)
    monkeypatch.setattr(
        module,
        "database_status",
        lambda settings: {"alembic_revision": "20260730_0001"},
    )
    monkeypatch.setattr(module, "load_reference_book", lambda path: object())

    class FakeEngine:
        def dispose(self):
            pass

    monkeypatch.setattr(module, "create_database_engine", lambda settings: FakeEngine())
    monkeypatch.setattr(
        module,
        "store_reference_snapshot",
        lambda *args, **kwargs: SimpleNamespace(content_sha256="a" * 64),
    )

    import tqsdk
    import tqsdk.tools

    class FakeApi:
        def close(self):
            pass

    monkeypatch.setattr(tqsdk, "TqApi", lambda **kwargs: FakeApi())
    monkeypatch.setattr(tqsdk, "TqAuth", lambda *args, **kwargs: object())

    def denied(*args, **kwargs):
        raise Exception(
            "您的账户不支持下载历史数据功能，需要购买后才能使用。"
            "升级网址：https://www.shinnytech.com/tqsdk-buy/ user-a pass-a"
        )

    monkeypatch.setattr(tqsdk.tools, "DataDownloader", denied)

    assert (
        module.main(
            [
                "--symbol",
                "KQ.m@SHFE.rb",
                "--start",
                "2026-05-01",
                "--end",
                "2026-07-31",
                "--duration",
                "60",
                "--acknowledge-data-cost",
            ]
        )
        == 2
    )
    output = capsys.readouterr().out
    assert "DOWNLOAD_FAIL TqSdkOperationError" in output
    assert "Traceback" not in output
    assert "user-a" not in output
    assert "pass-a" not in output

    def programming_error(*args, **kwargs):
        raise AttributeError("unexpected downloader defect")

    monkeypatch.setattr(tqsdk.tools, "DataDownloader", programming_error)
    with pytest.raises(AttributeError, match="unexpected downloader defect"):
        module.main(
            [
                "--symbol",
                "KQ.m@SHFE.rb",
                "--start",
                "2026-05-01",
                "--end",
                "2026-07-31",
                "--duration",
                "60",
                "--acknowledge-data-cost",
            ]
        )


def test_recent_snapshot_download_failure_happens_before_database_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _, module = _load_script("snapshot_recent_market_data")
    monkeypatch.setattr(module, "load_reference_book", lambda path: object())
    monkeypatch.setattr(
        module,
        "load_settings",
        lambda: SimpleNamespace(tq_user="user-a", tq_pass="pass-a"),
    )

    import tqsdk

    class FakeApi:
        calls = 0

        def get_kline_serial(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise Exception(
                    "您的账户不支持查看 KQ.m@DCE.m 的行情数据，"
                    "需要购买后才能使用。升级网址："
                    "https://www.shinnytech.com/tqsdk-buy/ user-a pass-a"
                )
            return object()

        def close(self):
            pass

    monkeypatch.setattr(tqsdk, "TqApi", lambda **kwargs: FakeApi())
    monkeypatch.setattr(tqsdk, "TqAuth", lambda *args, **kwargs: object())
    staged_paths = []

    def write_frame(frame, path, *, symbol):
        path = Path(path)
        path.write_text("validated later", encoding="utf-8")
        staged_paths.append(path)
        return SimpleNamespace(
            rows=2, first_datetime_nano=1, last_datetime_nano=2
        )

    monkeypatch.setattr(module, "write_recent_kline_frame_csv", write_frame)
    monkeypatch.setattr(
        module,
        "load_database_settings",
        lambda **kwargs: pytest.fail("database must not load after download failure"),
    )

    assert (
        module.main(
            [
                "--symbol",
                "KQ.m@SHFE.rb",
                "--symbol",
                "KQ.m@DCE.m",
                "--duration",
                "60",
                "--rows",
                "2",
            ]
        )
        == 2
    )
    output = capsys.readouterr().out
    assert "RECENT_SNAPSHOT_FAIL TqSdkOperationError" in output
    assert "user-a" not in output
    assert "pass-a" not in output
    assert staged_paths and all(not path.exists() for path in staged_paths)

    class BrokenApi:
        def get_kline_serial(self, *args, **kwargs):
            raise AttributeError("unexpected recent-window defect")

        def close(self):
            pass

    monkeypatch.setattr(tqsdk, "TqApi", lambda **kwargs: BrokenApi())
    with pytest.raises(AttributeError, match="unexpected recent-window defect"):
        module.main(
            [
                "--symbol",
                "KQ.m@SHFE.rb",
                "--duration",
                "60",
                "--rows",
                "2",
            ]
        )


def test_recent_snapshot_reports_partial_commit_without_masking_programming_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    _, module = _load_script("snapshot_recent_market_data")
    monkeypatch.setattr(module, "load_reference_book", lambda path: SimpleNamespace(calendar=None))
    monkeypatch.setattr(
        module,
        "load_settings",
        lambda: SimpleNamespace(tq_user="user-a", tq_pass="pass-a"),
    )
    database_settings = SimpleNamespace(secrets=())
    monkeypatch.setattr(
        module, "load_database_settings", lambda **kwargs: database_settings
    )

    import tqsdk

    class FakeApi:
        def get_kline_serial(self, *args, **kwargs):
            return object()

        def close(self):
            pass

    monkeypatch.setattr(tqsdk, "TqApi", lambda **kwargs: FakeApi())
    monkeypatch.setattr(tqsdk, "TqAuth", lambda *args, **kwargs: object())

    def write_frame(frame, path, *, symbol):
        path = Path(path)
        path.write_text("validated later", encoding="utf-8")
        return SimpleNamespace(
            rows=2, first_datetime_nano=1, last_datetime_nano=2
        )

    monkeypatch.setattr(module, "write_recent_kline_frame_csv", write_frame)
    monkeypatch.setattr(module, "iter_tqsdk_kline_csv", lambda *args, **kwargs: iter((1, 2)))

    class FakeEngine:
        def dispose(self):
            pass

    monkeypatch.setattr(module, "create_database_engine", lambda settings: FakeEngine())
    monkeypatch.setattr(
        module,
        "store_reference_snapshot",
        lambda *args, **kwargs: SimpleNamespace(content_sha256="a" * 64),
    )
    calls = 0

    def import_bars(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise AttributeError("unexpected second-import defect")
        return SimpleNamespace(
            batch_id="first-batch",
            inserted=True,
            rows_read=2,
            rows_written=2,
            null_trading_days=0,
        )

    monkeypatch.setattr(module, "import_market_bars", import_bars)

    with pytest.raises(AttributeError, match="unexpected second-import defect"):
        module.main(
            [
                "--symbol",
                "KQ.m@SHFE.rb",
                "--symbol",
                "KQ.m@DCE.m",
                "--duration",
                "60",
                "--rows",
                "2",
            ]
        )
    output = capsys.readouterr().out
    assert "RECENT_SNAPSHOT_PARTIAL" in output
    assert '"batch_id": "first-batch"' in output
    assert "RECENT_SNAPSHOT_FAIL" not in output
