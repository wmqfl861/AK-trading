"""Read an immutable history pointer in the background; swap only valid snapshots."""
from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import threading
from typing import Sequence

from ak_trading.probability.artifacts import safe_path
from .decision import DailyObserver


class ReloadingDailyObserver:
    def __init__(self, pointer: Path, allowed_symbols: Sequence[str], strategy: str = "ma_trend"):
        self.pointer = safe_path(pointer)
        self.allowed_symbols = tuple(allowed_symbols)
        self.strategy = strategy
        self._observer = DailyObserver(None, self.allowed_symbols, strategy=strategy)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._digest = None
        self._error = None
        self._thread = None
        self.refresh()

    def refresh(self):
        try:
            if self.pointer.stat().st_size > 65536:
                raise ValueError("history pointer too large")
            raw = self.pointer.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            with self._lock:
                if digest == self._digest:
                    return False
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("history pointer must be an object")
            if value.get("schema_version") != "infiny_history_pointer_v1":
                raise ValueError("unsupported history pointer")
            through = date.fromisoformat(value["expected_history_date"])
            bundle = safe_path(Path(value["bundle"]))
            if not bundle.is_relative_to(self.pointer.parent):
                raise ValueError("history bundle outside configured root")
            candidate = DailyObserver(bundle, self.allowed_symbols, self.strategy, expected_history_date=through)
            if candidate.history_hash != value["manifest_sha256"]:
                raise ValueError("history hash mismatch")
            for symbol, bars in candidate.bars.items():
                days = [bar.trading_day or bar.ts.date() for bar in bars]
                if max(days, default=None) != through or len(bars) < candidate.strategy.spec.required_bars:
                    raise ValueError("history is incomplete")
            with self._lock:
                old = self._observer.expected_history_date
                if old is not None and through < old:
                    raise ValueError("history pointer moved backward")
                self._observer = candidate
                self._digest = digest
                self._error = None
            return True
        except (OSError, ValueError, KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
            with self._lock:
                self._error = type(exc).__name__
            return False

    def evaluate(self, payload):
        with self._lock:
            observer = self._observer
        return observer.evaluate(payload)

    def status(self):
        with self._lock:
            return {**self._observer.status(), "history_refresh_error": self._error,
                    "history_pointer": str(self.pointer), "refresh_interval_seconds": 30}

    def start(self):
        def watch():
            while not self._stop.wait(30):
                try:
                    self.refresh()
                except Exception:  # one malformed read must not stop later valid reloads
                    with self._lock:
                        self._error = "refresh_failed"
        self._thread = threading.Thread(target=watch, daemon=True, name="observer-history-refresh")
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
