"""C2 SDK adapter layer: SYNTHETIC fake and real TqApi adapter (strictly split).

- :class:`FakeTqApi` is the offline SYNTHETIC test double. It never imports
  tqsdk and cannot open real connections. Test frames are labelled SYNTHETIC.
- :class:`RealTqApiFactory` is the only path that touches tqsdk. It is
  **written but never executed** during development/tests: the CLI run gates
  (H15) refuse before adapter construction while independent review and user
  confirmation are outstanding. It reads credentials only from
  ``os.environ`` (TQ_USER / TQ_PASS) - never from .env (H01) - and never logs
  credential values or raw SDK exception text.

H01/H12 safety properties of the real adapter:

- ``TqApi(auth=TqAuth(user, pass))`` with the default local sim: no
  ``TqAccount``/``TqKq``, no trading calls, ``enable_trading`` stays false.
- ``DataDownloader`` and every paid channel are permanently excluded; there
  is no fallback to them on failure (verified by static tests).
- Single symbol string per ``get_kline_serial`` call; no list subscription.
- ``sanitize_error_text`` performs real REDACTION, not truncation (review
  ACQ-W2-REVIEW-20260919-r1 F3): exception type name, known-safe diagnostic
  tokens, and whitelisted character classes survive; credential-shaped
  values and opaque secret-shaped runs are masked before any text may
  reach ``safe_message``/stderr.
"""

from __future__ import annotations

import math
import os
import re
import time
import unicodedata
from typing import Any, Callable, Mapping, Optional, Sequence

from .contracts import FailureClass, NANOSECOND

TQ_USER_ENV = "TQ_USER"
TQ_PASS_ENV = "TQ_PASS"

_BANNED_REFERENCES = ("DataDownloader",)


class TqAdapterError(RuntimeError):
    """Sanitized, classified adapter error (never carries secrets)."""

    def __init__(self, failure_class: FailureClass, safe_message: str) -> None:
        super().__init__(safe_message)
        self.failure_class = failure_class
        self.safe_message = safe_message


class RateLimitError(TqAdapterError):
    def __init__(self, retry_after_seconds: float) -> None:
        super().__init__(
            FailureClass.RATE_LIMITED,
            "provider rate limit; pausing worker (retry-after honored)",
        )
        self.retry_after_seconds = float(retry_after_seconds)


class AccountDeniedError(TqAdapterError):
    def __init__(self) -> None:
        super().__init__(
            FailureClass.ACCOUNT_DENIED,
            "account authentication or account-level permission denied; "
            "stopping account tasks for this run",
        )


class SymbolUnsupportedError(TqAdapterError):
    def __init__(self, symbol: str) -> None:
        super().__init__(
            FailureClass.SYMBOL_UNSUPPORTED,
            f"source reports symbol unsupported: {symbol}",
        )


class NotReadyTimeoutError(TqAdapterError):
    def __init__(self) -> None:
        super().__init__(
            FailureClass.TRANSIENT, "serial not ready within the bounded budget"
        )


class CredentialMissingError(TqAdapterError):
    def __init__(self, variable_names: Sequence[str]) -> None:
        super().__init__(
            FailureClass.ACCOUNT_DENIED,
            f"required environment variables not set: {', '.join(variable_names)}",
        )


# ---------------------------------------------------------------------------
# Secret redaction for exception text (H01; review r1 finding F3)
# ---------------------------------------------------------------------------
# Policy: whitelist character classes + field-level masking + secret-shape
# masking. What SURVIVES: the exception type name, the first line, printable
# ASCII and CJK characters, known-safe diagnostic tokens (hosts, ports,
# errno-style codes, classification keywords such as auth/登录/频繁/不支持).
# What is MASKED ("***"): URL userinfo (scheme://user:pass@host), values of
# credential-named fields (TQ_USER/TQ_PASS/AK_DATABASE_URL and the generic
# password/token/api-key family, in key=value / key:value / JSON-key forms),
# bearer/basic/digest credentials, and opaque high-entropy runs in free text
# (the reviewer decoy shape). Known limit: this is pattern-based defense in
# depth, not a cryptographic secret detector - a short unknown secret with
# no key context and no entropy signature can still pass; it never reaches
# persisted artifacts (task_results store failure_class only).

_REDACTED = "***"
_SAFE_TEXT_MAX = 160

# scheme://user:pass@... or scheme://user@...  ->  scheme://***@...
_URL_USERINFO_RE = re.compile(
    r"(?i)(?P<scheme>[a-z][a-z0-9+.\-]*://)"
    r"(?P<user>[^\s/:@?\"']+)(?::(?P<password>[^\s/@?\"']*))?@"
)

# Bare user:pass@domain.tld (no scheme, e.g. SMB-style) -> ***@domain.tld
_BARE_USERINFO_RE = re.compile(
    r"(?<![\w.@])(?P<userinfo>[\w.\\\-]+(?::[^\s/@,\"']{3,})?)@"
    r"(?=[\w.\-]+\.[\w.\-])"
)

# Credential-named field whose VALUE must be masked, in the forms
# key=value / key:value / key = "value" / "key": 'value' (JSON-ish).
_CRED_KEY_VALUE_RE = re.compile(
    r"""(?ix)\b(?P<key>
        tq_?user(?:name)?|tq_?pass(?:word|wd)?|ak_?database_?url|
        database_?url|db_?url|connection_?string|conn(?:ection)?_?string|
        dsn|user(?:name)?|pass(?:word|wd)?|pwd|secret|token|api[_\-]?key|
        access[_\-]?key|auth(?:orization)?|credential(?:s)?|
        private[_\-]?key|session[_\-]?id|session[_\-]?key|cookie
    )["']?\s*(?P<sep>[=:])\s*
    (?P<value>\"[^\"]*\"|'[^']*'|[^\s,;&\"']+)"""
)

# Bearer/Basic/Digest challenge values and "token <value>" headers.
_AUTH_SCHEME_VALUE_RE = re.compile(
    r"(?i)\b(?P<scheme>bearer|basic|digest|token)\s+"
    r"(?P<value>[A-Za-z0-9\-._+/=]{8,})"
)

# Opaque run of credential-alphabet characters (catch-all for free text).
# Delimiters '/', '=', ':' are excluded so hosts, URLs, and key=value
# assignments are not glued into one over-masked run.
_OPAQUE_RUN_RE = re.compile(r"[A-Za-z0-9+_\\-]{16,}")
_HEX_RUN_RE = re.compile(r"[0-9A-Fa-f]+")

# Character whitelist: printable ASCII plus CJK ideographs/punctuation (the
# classifier keys on 登录/频繁/账户/不支持). Control, format (zero-width,
# bidi-override), surrogate, and private-use codepoints are DROPPED so raw
# SDK bytes cannot smuggle an invisible payload into a log line.
def _char_allowed(ch: str) -> bool:
    code = ord(ch)
    if 0x20 <= code <= 0x7E:
        return True
    return (
        0x3400 <= code <= 0x4DBF  # CJK extension A
        or 0x4E00 <= code <= 0x9FFF  # CJK unified ideographs
        or 0x3000 <= code <= 0x303F  # CJK punctuation
        or 0xFF01 <= code <= 0xFF5E  # fullwidth forms
    )


def _run_is_secret_shaped(run: str) -> bool:
    """Heuristic: is this opaque token run more secret than diagnostic?"""
    if len(run) >= 24:
        return True
    has_alpha = any(ch.isalpha() for ch in run)
    if not has_alpha:
        return False  # pure digits (timestamps, errnos, ports) survive
    if len(run) >= 16 and _HEX_RUN_RE.fullmatch(run):
        return True  # 16+ hex chars with letters: digest / hex key shape
    has_digit = any(ch.isdigit() for ch in run)
    has_lower = any(ch.islower() for ch in run)
    has_upper = any(ch.isupper() for ch in run)
    return has_digit and has_lower and has_upper  # mixed-entropy token


def _mask_opaque_runs(text: str) -> str:
    return _OPAQUE_RUN_RE.sub(
        lambda m: _REDACTED if _run_is_secret_shaped(m.group(0)) else m.group(0),
        text,
    )


def sanitize_error_text(exc: BaseException) -> str:
    """Return a redacted diagnostic string; raw text is never emitted (H01).

    Pipeline (order matters - character whitelisting runs FIRST so that
    zero-width characters cannot split a secret in half to evade the
    pattern masks):
    first line -> character whitelist -> URL/bare userinfo masking ->
    credential key=value masking -> bearer/token masking -> opaque
    secret-shaped run masking -> length cap -> exception type prefix.
    """
    text = str(exc)
    type_name = type(exc).__name__
    if not text:
        return type_name
    text = text.splitlines()[0]
    text = "".join(ch for ch in text if _char_allowed(ch))
    text = _URL_USERINFO_RE.sub(
        lambda m: m.group("scheme") + _REDACTED + "@", text
    )
    text = _BARE_USERINFO_RE.sub(_REDACTED + "@", text)
    text = _CRED_KEY_VALUE_RE.sub(
        lambda m: m.group("key") + m.group("sep") + _REDACTED, text
    )
    text = _AUTH_SCHEME_VALUE_RE.sub(
        lambda m: m.group("scheme") + " " + _REDACTED, text
    )
    text = _mask_opaque_runs(text)
    text = text.strip()
    if not text:
        return type_name
    return f"{type_name}: {text[:_SAFE_TEXT_MAX].rstrip()}"


# ---------------------------------------------------------------------------
# SYNTHETIC fake (offline tests only)
# ---------------------------------------------------------------------------

class FakeTqApi:
    """Deterministic offline SDK stand-in. SYNTHETIC; never a real connection."""

    is_synthetic = True

    def __init__(
        self,
        frames: Mapping[tuple[str, int], Any],
        *,
        clock: Callable[[], int],
        updates_until_ready: int = 2,
        failures: Optional[Mapping[tuple[str, int], TqAdapterError]] = None,
    ) -> None:
        import pandas as pd  # local import: only synthetic path needs frames

        self._pd = pd
        self._live_frames: dict[tuple[str, int], Any] = {
            key: value for key, value in frames.items()
        }
        self._clock = clock
        self._updates_until_ready = updates_until_ready
        self._failures = dict(failures or {})
        self._update_counts: dict[tuple[str, int], int] = {}
        self.request_log: list[dict[str, Any]] = []
        self.closed = False

    # -- SDK-shaped API -----------------------------------------------------
    def get_kline_serial(
        self, symbol: str, duration_seconds: int, data_length: int = 8192
    ) -> Any:
        if self.closed:
            raise TqAdapterError(FailureClass.LOCAL_ERROR, "fake adapter closed")
        if not isinstance(symbol, str):
            raise TqAdapterError(
                FailureClass.LOCAL_ERROR, "symbol must be a single string (H10/C2)"
            )
        key = (symbol, duration_seconds)
        self.request_log.append(
            {
                "symbol": symbol,
                "duration_seconds": duration_seconds,
                "data_length": data_length,
            }
        )
        failure = self._failures.get(key)
        if failure is not None:
            raise failure
        if key not in self._live_frames:
            raise SymbolUnsupportedError(symbol)
        # The SDK hands back a live, in-place-updating frame. The caller must
        # deep-copy once ready (H06); the fake returns the SAME object so the
        # drift counterexample in tests is meaningful.
        return self._live_frames[key]

    def wait_update(self, deadline: Optional[float] = None) -> bool:
        return True

    def is_serial_ready(self, serial: Any) -> bool:
        # Track readiness per live object identity via a side table.
        for key, frame in self._live_frames.items():
            if frame is serial:
                count = self._update_counts.get(key, 0) + 1
                self._update_counts[key] = count
                return count >= self._updates_until_ready
        return True

    def close(self) -> None:
        self.closed = True

    # -- helpers for tests ---------------------------------------------------
    def advance_updates_until_ready(self, count: int) -> None:
        self._updates_until_ready = count

    @property
    def now_nano(self) -> int:
        return int(self._clock())


def make_fake_frame(
    *,
    symbol: str,
    duration_seconds: int,
    start_nano: int,
    rows: int,
    closed_rows: int,
    padding_rows: int = 0,
    duplicate_keys: int = 0,
    invalid_rows: int = 0,
    price: float = 4000.0,
) -> Any:
    """Build a SYNTHETIC kline frame shaped like a tqsdk serial.

    Layout mirrors the observed SDK serial: padding rows (NaN datetime) at the
    head, then closed bars, then optionally live (not yet closed) bars. The
    last closed/live boundary is expressed by the caller via ``closed_rows``.
    """
    import pandas as pd

    total = padding_rows + rows
    data = {
        "datetime": [float("nan")] * padding_rows,
        "open": [float("nan")] * padding_rows,
        "high": [float("nan")] * padding_rows,
        "low": [float("nan")] * padding_rows,
        "close": [float("nan")] * padding_rows,
        "volume": [float("nan")] * padding_rows,
        "open_oi": [float("nan")] * padding_rows,
        "close_oi": [float("nan")] * padding_rows,
    }
    for index in range(rows):
        bar_start = start_nano + index * duration_seconds * NANOSECOND
        data["datetime"].append(float(bar_start))
        step = 1.0 + index * 0.5
        if index < invalid_rows:
            # Deliberately broken OHLC (high below low) for INVALID probes.
            data["open"].append(price + step)
            data["high"].append(price + step - 5.0)
            data["low"].append(price + step + 5.0)
            data["close"].append(price + step)
        else:
            data["open"].append(price + step)
            data["high"].append(price + step + 3.0)
            data["low"].append(price + step - 3.0)
            data["close"].append(price + step + 1.0)
        data["volume"].append(100.0 + index)
        data["open_oi"].append(5000.0 + index)
        data["close_oi"].append(5010.0 + index)
    for _ in range(duplicate_keys):
        # Duplicate of the LAST closed bar (same datetime): tests duplicate
        # accounting without breaking frame length expectations.
        data["datetime"].append(float(start_nano + (closed_rows - 1) * duration_seconds * NANOSECOND))
        data["open"].append(price)
        data["high"].append(price + 1.0)
        data["low"].append(price - 1.0)
        data["close"].append(price)
        data["volume"].append(7.0)
        data["open_oi"].append(10.0)
        data["close_oi"].append(11.0)
    return pd.DataFrame(data)


def fake_closed_and_live_boundary(frame_rows: int, closed_rows: int) -> int:
    """Index of the first live (not yet closed) bar within the frame."""
    if closed_rows > frame_rows:
        raise ValueError("closed rows cannot exceed frame rows")
    return frame_rows - closed_rows


# ---------------------------------------------------------------------------
# Real adapter (written, gated, NOT RUN during development)
# ---------------------------------------------------------------------------

class RealTqApiSession:
    """Bounded single-connection TqApi session for quotes only (H01).

    Constructed only after every CLI run gate passes. The credentials exist
    only inside this process; nothing about them is logged or persisted.
    """

    is_synthetic = False

    def __init__(self, *, connect_timeout_budget: float = 90.0) -> None:
        user = os.environ.get(TQ_USER_ENV, "").strip()
        password = os.environ.get(TQ_PASS_ENV, "").strip()
        missing = [
            name
            for name, value in ((TQ_USER_ENV, user), (TQ_PASS_ENV, password))
            if not value
        ]
        if missing:
            raise CredentialMissingError(missing)
        try:
            from tqsdk import TqApi, TqAuth  # noqa: imported lazily on purpose
        except ImportError as exc:
            raise TqAdapterError(
                FailureClass.LOCAL_ERROR, "tqsdk not installed in this environment"
            ) from None
        try:
            # Quotes-only session: auth object + default local sim. No
            # TqAccount/TqKq, no trading calls (H01). web_gui disabled.
            self._api = TqApi(auth=TqAuth(user, password), web_gui=False)
        except TqAdapterError:
            raise
        except Exception as exc:  # sanitized: raw SDK text is not persisted
            safe = sanitize_error_text(exc)
            lowered = safe.lower()
            if "auth" in lowered or "登录" in safe or "password" in lowered:
                raise AccountDeniedError() from None
            raise TqAdapterError(FailureClass.TRANSIENT, f"api connect failed: {safe}") from None
        self._connect_started = time.monotonic()

    # -- SDK passthrough with classification --------------------------------
    def get_kline_serial(
        self, symbol: str, duration_seconds: int, data_length: int = 8192
    ) -> Any:
        """Subscribe synchronously: create_task + bounded wait_update pump."""
        if not isinstance(symbol, str):
            raise TqAdapterError(
                FailureClass.LOCAL_ERROR, "symbol must be a single string (H10/C2)"
            )
        try:
            task = self._api.create_task(
                self._subscribe(symbol, duration_seconds, data_length)
            )
            while not task.done():
                self._api.wait_update(deadline=time.time() + 5.0)
            return task.result()
        except TqAdapterError:
            raise
        except Exception as exc:
            raise self._classify(exc, symbol) from None

    async def _subscribe(self, symbol: str, duration_seconds: int, data_length: int):
        return self._api.get_kline_serial(symbol, duration_seconds, data_length=data_length)

    def wait_update(self, deadline: Optional[float] = None) -> bool:
        try:
            return self._api.wait_update(deadline=deadline)
        except Exception as exc:
            raise self._classify(exc, None) from None

    def is_serial_ready(self, serial: Any) -> bool:
        try:
            return bool(self._api.is_serial_ready(serial))
        except Exception as exc:
            raise self._classify(exc, None) from None

    def close(self) -> None:
        try:
            self._api.close()
        except Exception as exc:  # cleanup failures are sanitized, not fatal
            raise TqAdapterError(
                FailureClass.LOCAL_ERROR, f"api close failed: {sanitize_error_text(exc)}"
            ) from None

    def _classify(self, exc: Exception, symbol: Optional[str]) -> TqAdapterError:
        safe = sanitize_error_text(exc)
        lowered = safe.lower()
        if "too many" in lowered or "rate" in lowered or "频繁" in safe:
            retry_after = 60.0
            return RateLimitError(retry_after)
        if "auth" in lowered or "登录" in safe or "password" in lowered or "账户" in safe:
            return AccountDeniedError()
        if symbol is not None and ("不支持" in safe or "not support" in lowered):
            return SymbolUnsupportedError(symbol)
        return TqAdapterError(FailureClass.TRANSIENT, f"adapter failure: {safe}")


def assert_no_paid_channel_references(source: str) -> None:
    """Static guard: paid channels stay unreferenceable in this package."""
    for banned in _BANNED_REFERENCES:
        if banned in source:
            raise TqAdapterError(
                FailureClass.LOCAL_ERROR,
                f"paid channel reference '{banned}' is permanently excluded (H01)",
            )


__all__ = [
    "AccountDeniedError",
    "CredentialMissingError",
    "FakeTqApi",
    "NotReadyTimeoutError",
    "RateLimitError",
    "RealTqApiSession",
    "SymbolUnsupportedError",
    "TQ_PASS_ENV",
    "TQ_USER_ENV",
    "TqAdapterError",
    "assert_no_paid_channel_references",
    "fake_closed_and_live_boundary",
    "make_fake_frame",
    "sanitize_error_text",
]
