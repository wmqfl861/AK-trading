import json
import math
from pathlib import Path

import pytest

from ak_trading.journal import (
    CompositeJournal,
    JsonlJournal,
    redact_text,
    sanitize_event,
)


def test_jsonl_journal_flushes_and_redacts(tmp_path: Path):
    path = tmp_path / "nested" / "events.jsonl"
    journal = JsonlJournal(path)
    journal({"event": "decision", "value": math.nan, "tq_pass": "secret"})
    journal({"event": "shutdown", "token": "hidden"})

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows == [
        {"event": "decision", "value": None, "tq_pass": "[REDACTED]"},
        {"event": "shutdown", "token": "[REDACTED]"},
    ]


def test_redact_text_replaces_exact_diagnostic_secrets():
    message = "authentication failed for user-a with pass-a"
    assert redact_text(message, secrets=("user-a", "pass-a", "")) == (
        "authentication failed for [REDACTED] with [REDACTED]"
    )


def test_database_credential_keys_are_redacted_but_public_urls_remain():
    sanitized = sanitize_event(
        {
            "database_url": "postgresql://user:pass@host/db",
            "ak_database_url": "secret",
            "dsn": "secret",
            "conninfo": "secret",
            "source_url": "https://www.shfe.com.cn/publicnotice/notice/",
        }
    )

    assert sanitized == {
        "database_url": "[REDACTED]",
        "ak_database_url": "[REDACTED]",
        "dsn": "[REDACTED]",
        "conninfo": "[REDACTED]",
        "source_url": "https://www.shfe.com.cn/publicnotice/notice/",
    }


def test_composite_journal_preserves_sink_order_and_propagates_failure():
    calls = []

    def file_sink(event):
        calls.append(("file", dict(event)))

    def database_sink(event):
        calls.append(("database", dict(event)))
        raise RuntimeError("database unavailable")

    journal = CompositeJournal(file_sink, database_sink)
    event = {"event": "run_manifest", "run_id": "run-1"}

    with pytest.raises(RuntimeError, match="database unavailable"):
        journal(event)

    assert calls == [("file", event), ("database", event)]
