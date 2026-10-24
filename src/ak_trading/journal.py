from __future__ import annotations

import json
import math
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Iterable, Mapping

_SENSITIVE_KEYS = frozenset(
    {
        "ak_database_url",
        "api_key",
        "apikey",
        "conninfo",
        "database_url",
        "dsn",
        "password",
        "secret",
        "token",
        "tq_pass",
    }
)


def redact_text(value: object, *, secrets: Iterable[str] = ()) -> str:
    """Redact exact sensitive values before emitting diagnostic text."""
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "[REDACTED]")
    return text


def sanitize_event(value: Any) -> Any:
    """Return a JSON-safe value with credential-bearing keys redacted."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            text_key = str(key)
            if text_key.lower() in _SENSITIVE_KEYS:
                result[text_key] = "[REDACTED]"
            else:
                result[text_key] = sanitize_event(item)
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize_event(item) for item in value]
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return str(value)
    return number if math.isfinite(number) else None


class JsonlJournal:
    """Append one flushed JSON object per event without retaining credentials."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = Lock()

    def __call__(self, event: Mapping[str, Any]) -> None:
        payload = json.dumps(
            sanitize_event(event), ensure_ascii=True, separators=(",", ":")
        )
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.write("\n")


class CompositeJournal:
    """Send each event to ordered sinks, propagating the first sink failure."""

    def __init__(self, *sinks: Callable[[Mapping[str, Any]], None]) -> None:
        if not sinks:
            raise ValueError("at least one journal sink is required")
        self._sinks = sinks

    def __call__(self, event: Mapping[str, Any]) -> None:
        for sink in self._sinks:
            sink(event)
