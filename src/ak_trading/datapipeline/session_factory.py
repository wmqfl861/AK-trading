"""DP03 market-data session factory (work order H03.1/H03.4/H03.10).

The factory builds sessions that implement the DP01-frozen
``MarketDataSession`` facade (ports.MARKET_DATA_SESSION_SURFACE: subscribe /
wait_update / ready / copy_snapshot / close - nothing else).  Trading
capability cannot leak:

- the facade never delegates attributes to the wrapped SDK object (no
  ``__getattr__`` forwarding), so ``insert_order``/``get_account``/... are
  unreachable through it;
- :func:`ak_trading.datapipeline.ports.assert_no_trading_surface` runs on
  every facade instance at construction (D05/V12 aspect owned by DP03);
- the REAL TqSdk entry (:class:`RealSdkSessionFactory`) is the only place a
  real SDK can appear.  It requires an explicit
  ``USER_CONTROLLED_INJECTION`` authorization and is NOT_RUN in r1: with no
  authorization the factory refuses BEFORE importing tqsdk, opening a socket
  or reading credentials (H03.10; dispatch note "真实 TqSdk 会话属验收阻断项
  NOT_RUN").

Error model (H03.10): SDK exception TEXT is never propagated.  Exceptions are
classified into the frozen ``FailureClass`` taxonomy and surfaced as
sanitized :class:`SessionError` values carrying only the exception TYPE NAME
and a stable reason string.

Import hygiene: importing this module performs no I/O (H01.6 discipline).
tqsdk is imported lazily inside the authorized real path only; pandas is
imported lazily only when a synthetic frame is actually built.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from .contracts import (
    NANOSECOND,
    DataOrigin,
    ErrorCode,
    FailureClass,
    _fail,
    validate_duration_seconds,
)
from .ports import (
    MARKET_DATA_SESSION_SURFACE,
    SUBSCRIBE_DATA_LENGTH_R1,
    ConnectionBaseline,
    SerialRef,
    SnapshotCopy,
    SubscriptionKey,
    assert_no_trading_surface,
)

__all__ = [
    "SDK_FRAME_COLUMNS",
    "SessionError",
    "TransportError",
    "RateLimitError",
    "PermissionDeniedError",
    "SymbolUnsupportedError",
    "NotReadyTimeoutError",
    "classify_sdk_exception",
    "SdkSessionFacade",
    "RealSourceAuthorization",
    "RealSdkSessionFactory",
    "SyntheticTimelineSession",
    "SyntheticSessionFactory",
    "build_synthetic_frame",
    "SDK_SYNTHETIC_VERSION",
]


#: Column contract of a tqsdk kline serial frame (r1 whitelist surface).
SDK_FRAME_COLUMNS: Tuple[str, ...] = (
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_oi",
    "close_oi",
)

SDK_SYNTHETIC_VERSION = "SYNTHETIC-dp03-r1"


# ---------------------------------------------------------------------------
# Sanitized session errors (H03.10: safe enums instead of SDK exception text)
# ---------------------------------------------------------------------------


class SessionError(RuntimeError):
    """Sanitized, classified session failure (never carries raw SDK text)."""

    failure_class: FailureClass = FailureClass.TRANSIENT

    def __init__(self, reason: str, *, sdk_type_name: str = "") -> None:
        prefix = f"{sdk_type_name}: " if sdk_type_name else ""
        super().__init__(f"{prefix}{reason}")
        self.reason_sanitized = f"{prefix}{reason}"
        self.sdk_type_name = sdk_type_name


class TransportError(SessionError):
    """Bounded-retry transport failure (reconnect + resubscribe path)."""

    failure_class = FailureClass.TRANSIENT


class RateLimitError(SessionError):
    """Provider rate limit: conservative pause, cross-run budget respected."""

    failure_class = FailureClass.RATE_LIMITED

    def __init__(self, retry_after_seconds: float = 60.0) -> None:
        super().__init__("provider rate limit; pausing (retry-after honored)")
        self.retry_after_seconds = float(retry_after_seconds)


class PermissionDeniedError(SessionError):
    """Auth/account permission denial: circuit breaker, never retried here."""

    failure_class = FailureClass.ACCOUNT_DENIED

    def __init__(self) -> None:
        super().__init__(
            "account authentication or account-level permission denied; "
            "stopping account tasks for this run"
        )


class SymbolUnsupportedError(SessionError):
    failure_class = FailureClass.SYMBOL_UNSUPPORTED

    def __init__(self, symbol: str) -> None:
        super().__init__(f"source reports symbol unsupported: {symbol}")


class NotReadyTimeoutError(SessionError):
    """Serial did not become ready inside the bounded budget."""

    failure_class = FailureClass.TRANSIENT

    def __init__(self) -> None:
        super().__init__("serial not ready within the bounded budget")


def classify_sdk_exception(exc: BaseException) -> SessionError:
    """Classify a raw SDK exception into a sanitized SessionError.

    The raw message is inspected for classification keywords ONLY and is
    never attached to the resulting error (H03.10).  The exception TYPE NAME
    survives as low-entropy diagnostic context.
    """
    type_name = type(exc).__name__
    text = str(exc)
    lowered = text.lower()
    if "too many" in lowered or "rate" in lowered or "频繁" in text:
        return RateLimitError(retry_after_seconds=60.0)
    if (
        "auth" in lowered
        or "登录" in text
        or "password" in lowered
        or "账户" in text
        or "permission" in lowered
        or "401" in text
        or "403" in text
    ):
        return PermissionDeniedError()
    if "不支持" in text or "not support" in lowered:
        return SymbolUnsupportedError("")
    return TransportError(f"transport failure ({type_name})", sdk_type_name=type_name)


# ---------------------------------------------------------------------------
# SDK facade (wraps an injected SDK-like object; zero tqsdk imports)
# ---------------------------------------------------------------------------


def _deep_copy_frame_rows(frame: Any) -> Tuple[Mapping[str, Any], ...]:
    """Deep-copy SDK frame rows into immutable-valued row mappings.

    H03.3/02 blueprint: the SDK's live DataFrame is deep-copied BEFORE the
    data crosses toward the storage thread; the caller may keep mutating the
    frame afterwards without affecting the copy.  NaN is preserved verbatim
    (padding rows are classified downstream, never silently dropped).
    """
    records: list
    to_dict = getattr(frame, "to_dict", None)
    if callable(to_dict):  # pandas DataFrame
        records = list(frame.to_dict(orient="records"))
    elif isinstance(frame, Mapping):
        # column -> sequence form
        columns = [c for c in SDK_FRAME_COLUMNS if c in frame]
        length = max((len(frame[c]) for c in columns), default=0)
        records = [{c: frame[c][i] for c in columns} for i in range(length)]
    else:
        records = list(frame)
    rows: list = []
    for record in records:
        row: dict = {}
        for column in SDK_FRAME_COLUMNS:
            value = record.get(column) if isinstance(record, Mapping) else getattr(record, column, None)
            if isinstance(value, bool):
                row[column] = value
            elif isinstance(value, float):
                row[column] = float(value)  # NaN survives verbatim
            elif isinstance(value, int):
                row[column] = int(value)
            else:
                row[column] = value
        rows.append(row)
    return tuple(rows)


class SdkSessionFacade:
    """MarketDataSession facade over an injected SDK-like api object.

    The wrapped object only needs ``get_kline_serial`` / ``wait_update`` /
    ``is_serial_ready`` / ``close``.  This class performs NO import of tqsdk
    and NO attribute delegation, so trading methods on the underlying api are
    unreachable through the facade (D05).  ``wait_update`` clamps every
    deadline to the frozen ``wait_update_deadline_seconds_max`` budget, and
    ``False`` means "wait deadline reached", NOT a disconnect (H03.3).
    """

    is_synthetic = False
    sdk_version = "SDK-INJECTED"

    def __init__(
        self,
        api: Any,
        *,
        connection_epoch: int,
        clock_nano: Callable[[], int],
        wait_deadline_max: float = 5.0,
    ) -> None:
        for name in ("get_kline_serial", "wait_update", "is_serial_ready", "close"):
            if not callable(getattr(api, name, None)):
                _fail(
                    ErrorCode.E_RUN_STATE_INVALID,
                    f"injected SDK api lacks required market-data method {name!r}",
                )
        self._api = api
        self._clock_nano = clock_nano
        self._wait_deadline_max = float(wait_deadline_max)
        self.connection_epoch = int(connection_epoch)
        self._next_serial_id = 1
        self._serials: dict[int, Tuple[SubscriptionKey, Any]] = {}
        self._update_seq = 0
        self._closed = False
        self._baseline_recorded = False
        self._baseline: Optional[ConnectionBaseline] = None
        self._lock = threading.RLock()
        # Facade surface check (V12 aspect): no trading method may exist on
        # the facade itself, ever.
        assert_no_trading_surface(self)

    # -- MarketDataSession surface -------------------------------------------

    def subscribe(
        self, key: SubscriptionKey, data_length: int = SUBSCRIBE_DATA_LENGTH_R1
    ) -> SerialRef:
        if data_length != SUBSCRIBE_DATA_LENGTH_R1:
            _fail(
                ErrorCode.E_REQUEST_LENGTH_NOT_8192,
                f"subscribe data_length is pinned to {SUBSCRIBE_DATA_LENGTH_R1}",
            )
        # H03.1: per-task period validation BEFORE any network request.
        validate_duration_seconds(key.coordinate.duration_seconds)
        with self._lock:
            if self._closed:
                raise TransportError("session already closed")
            try:
                frame = self._api.get_kline_serial(
                    key.coordinate.source_symbol,
                    key.coordinate.duration_seconds,
                    data_length,
                )
            except SessionError:
                raise
            except Exception as exc:  # sanitized: raw SDK text never leaves
                raise classify_sdk_exception(exc) from None
            serial_id = self._next_serial_id
            self._next_serial_id += 1
            self._serials[serial_id] = (key, frame)
            return SerialRef(
                serial_id=serial_id,
                coordinate=key.coordinate,
                data_length=SUBSCRIBE_DATA_LENGTH_R1,
            )

    def wait_update(self, deadline_seconds: float) -> bool:
        bounded = min(max(float(deadline_seconds), 0.0), self._wait_deadline_max)
        with self._lock:
            if self._closed:
                raise TransportError("session already closed")
        deadline_abs = time.time() + bounded
        try:
            got_update = bool(self._api.wait_update(deadline=deadline_abs))
        except Exception as exc:
            raise classify_sdk_exception(exc) from None
        if got_update:
            with self._lock:
                self._update_seq += 1
        return got_update

    def ready(self, serial: SerialRef) -> bool:
        with self._lock:
            entry = self._serials.get(serial.serial_id)
        if entry is None:
            _fail(ErrorCode.E_RUN_STATE_INVALID, f"unknown serial {serial.serial_id}")
        _key, frame = entry
        try:
            return bool(self._api.is_serial_ready(frame))
        except Exception as exc:
            raise classify_sdk_exception(exc) from None

    def copy_snapshot(self, serial: SerialRef) -> SnapshotCopy:
        with self._lock:
            entry = self._serials.get(serial.serial_id)
            update_seq = self._update_seq
        if entry is None:
            _fail(ErrorCode.E_RUN_STATE_INVALID, f"unknown serial {serial.serial_id}")
        key, frame = entry
        # Deep copy happens HERE, in the collection thread, before the data
        # crosses toward the storage thread (H03.3; E_NOT_DETACHED otherwise).
        rows = _deep_copy_frame_rows(frame)
        return SnapshotCopy(
            serial=SerialRef(
                serial_id=serial.serial_id,
                coordinate=key.coordinate,
                data_length=SUBSCRIBE_DATA_LENGTH_R1,
            ),
            rows=rows,
            copied_at_update_seq=update_seq,
            detached=True,
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            api, self._api = self._api, None
        try:
            api.close()
        except Exception as exc:
            raise classify_sdk_exception(exc) from None
        finally:
            with self._lock:
                self._serials.clear()

    # -- Baseline / evidence helpers (Q02) ------------------------------------

    def record_baseline(self, baseline_snapshot_id: str) -> ConnectionBaseline:
        """Record the subscribe-ready baseline once per connection epoch."""
        with self._lock:
            if self._baseline_recorded and self._baseline is not None:
                return self._baseline
            self._baseline = ConnectionBaseline(
                connection_epoch=self.connection_epoch,
                baseline_snapshot_id=baseline_snapshot_id,
                ready_update_seq=self._update_seq,
            )
            self._baseline_recorded = True
            return self._baseline

    @property
    def baseline(self) -> Optional[ConnectionBaseline]:
        return self._baseline

    @property
    def update_seq(self) -> int:
        with self._lock:
            return self._update_seq

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def now_nano(self) -> int:
        return int(self._clock_nano())

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - safety net
        # NO delegation to the wrapped api: unknown attributes must fail, so
        # trading surfaces can never be reached through the facade (D05).
        raise AttributeError(
            f"market-data session facade has no attribute {name!r} "
            f"(surface is exactly {MARKET_DATA_SESSION_SURFACE})"
        )


# ---------------------------------------------------------------------------
# Real SDK entry (controlled; NOT_RUN in r1 without authorization)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RealSourceAuthorization:
    """User-controlled injection record for a REAL source session.

    Authority must be ``USER_CONTROLLED_INJECTION`` and ``granted_by`` names
    the human authorization.  No credential VALUE ever lives here.
    """

    authority: str = "USER_CONTROLLED_INJECTION"
    granted_by: str = ""
    grant_id: str = ""

    def validate(self) -> None:
        if self.authority != "USER_CONTROLLED_INJECTION" or not self.granted_by or not self.grant_id:
            _fail(
                ErrorCode.E_RUN_STATE_INVALID,
                "real TqSdk session requires USER_CONTROLLED_INJECTION "
                "authorization (granted_by + grant_id); r1 default is refusal "
                "(SYNTHETIC sessions only)",
            )


class RealSdkSessionFactory:
    """The ONLY path that may touch tqsdk (H03.10; NOT_RUN in r1).

    Without a valid :class:`RealSourceAuthorization` the factory refuses
    BEFORE importing tqsdk or reading credential environment variables.  With
    authorization it builds a quotes-only ``TqApi`` (no TqAccount/TqKq, no
    trading calls) wrapped in :class:`SdkSessionFacade`.
    """

    def __init__(
        self,
        authorization: Optional[RealSourceAuthorization] = None,
        *,
        wait_deadline_max: float = 5.0,
        clock_nano: Optional[Callable[[], int]] = None,
        connect_timeout_budget: float = 90.0,
    ) -> None:
        self._authorization = authorization
        self._wait_deadline_max = wait_deadline_max
        self._clock_nano = clock_nano or time.time_ns
        self._connect_timeout_budget = connect_timeout_budget
        self._epochs = 0

    def create(self) -> SdkSessionFacade:
        if self._authorization is None:
            _fail(
                ErrorCode.E_RUN_STATE_INVALID,
                "real TqSdk session NOT authorized: r1 delivers SYNTHETIC "
                "sessions only; real construction is the acceptance blocker "
                "NOT_RUN (dispatch DP03)",
            )
        self._authorization.validate()
        # -- authorized path: written, NOT exercised in r1 (NOT_RUN) --------
        import os

        user = os.environ.get("TQ_USER", "").strip()
        password = os.environ.get("TQ_PASS", "").strip()
        if not user or not password:
            raise PermissionDeniedError()
        try:
            from tqsdk import TqApi, TqAuth  # noqa: imported lazily on purpose
        except ImportError as exc:
            raise SessionError(
                "tqsdk not installed in this environment",
                sdk_type_name=type(exc).__name__,
            ) from None
        try:
            # Quotes-only: auth object + default local sim; web_gui disabled;
            # no TqAccount/TqKq, zero trading calls (D05).
            api = TqApi(auth=TqAuth(user, password), web_gui=False)
        except Exception as exc:
            raise classify_sdk_exception(exc) from None
        self._epochs += 1
        return SdkSessionFacade(
            api,
            connection_epoch=self._epochs,
            clock_nano=self._clock_nano,
            wait_deadline_max=self._wait_deadline_max,
        )


# ---------------------------------------------------------------------------
# SYNTHETIC scripted session (offline pipeline execution; marked SYNTHETIC)
# ---------------------------------------------------------------------------


def build_synthetic_frame(
    *,
    start_nano: int,
    duration_seconds: int,
    closed_rows: int,
    live_rows: int = 0,
    padding_rows: int = 0,
    invalid_rows: int = 0,
    duplicate_rows: int = 0,
    price: float = 4000.0,
) -> Any:
    """Build a SYNTHETIC SDK-shaped kline frame (pandas, lazily imported).

    Layout mirrors the observed SDK serial: padding (NaN) head, closed bars,
    then live (in-formation) bars; optional deliberately-broken OHLC rows and
    same-key duplicate rows for conflict vectors.
    """
    try:
        import pandas as pd
    except ImportError:  # pragma: no cover - pandas pinned in requirements
        raise SessionError("pandas unavailable for SYNTHETIC frame") from None

    data: dict = {column: [float("nan")] * padding_rows for column in SDK_FRAME_COLUMNS}
    total_rows = closed_rows + live_rows
    for index in range(total_rows):
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
    for _ in range(duplicate_rows):
        # Duplicate of the LAST closed bar (same datetime key).
        data["datetime"].append(float(start_nano + (closed_rows - 1) * duration_seconds * NANOSECOND))
        data["open"].append(price)
        data["high"].append(price + 1.0)
        data["low"].append(price - 1.0)
        data["close"].append(price)
        data["volume"].append(7.0)
        data["open_oi"].append(10.0)
        data["close_oi"].append(11.0)
    return pd.DataFrame(data)


class SyntheticTimelineSession:
    """Scripted SYNTHETIC MarketDataSession for offline execution and tests.

    The frame updates IN PLACE over ``wait_update`` calls (SDK-like).  The
    timeline is a list of steps, each applied when ``wait_update`` is called:
    each step is a mapping ``{"event": ..., ...}`` with events:

    - ``update``: append/advance live bars for subscribed serials (uses the
      injected clock as bar anchor), ``wait_update`` returns True;
    - ``deadline``: ``wait_update`` returns False (wait deadline, H03.3);
    - ``disconnect``: raise TransportError (reconnect path);
    - ``rate_limit``: raise RateLimitError;
    - ``permission_denied``: raise PermissionDeniedError.

    Readiness: a serial becomes ready after ``ready_after_updates`` updates
    (0 = immediate); ``never_ready_keys`` stay not-ready forever (V06).
    """

    is_synthetic = True
    sdk_version = SDK_SYNTHETIC_VERSION
    data_origin = DataOrigin.SYNTHETIC

    def __init__(
        self,
        *,
        frames: Mapping[Tuple[str, int], Any],
        timeline: Sequence[Mapping[str, Any]],
        clock_nano: Callable[[], int],
        ready_after_updates: int = 0,
        never_ready_keys: Sequence[Tuple[str, int]] = (),
        subscribe_errors: Optional[Mapping[Tuple[str, int], SessionError]] = None,
    ) -> None:
        self._frames = dict(frames)
        self._timeline = list(timeline)
        self._clock_nano = clock_nano
        self._ready_after_updates = int(ready_after_updates)
        self._never_ready = set(never_ready_keys)
        self._subscribe_errors = dict(subscribe_errors or {})
        self._updates = 0
        self._step = 0
        self._next_serial_id = 1
        self._serials: dict[int, Tuple[SubscriptionKey, Any]] = {}
        self._update_counts: dict[Tuple[str, int], int] = {}
        self._closed = False
        self._live_bar_counter: dict[Tuple[str, int], int] = {}
        self.subscribe_log: list[dict[str, Any]] = []
        self.close_count = 0
        self.connection_epoch = 0
        self._baseline: Optional[ConnectionBaseline] = None
        self._baseline_recorded = False
        assert_no_trading_surface(self)

    # -- MarketDataSession surface -------------------------------------------

    def subscribe(
        self, key: SubscriptionKey, data_length: int = SUBSCRIBE_DATA_LENGTH_R1
    ) -> SerialRef:
        if data_length != SUBSCRIBE_DATA_LENGTH_R1:
            _fail(
                ErrorCode.E_REQUEST_LENGTH_NOT_8192,
                f"subscribe data_length is pinned to {SUBSCRIBE_DATA_LENGTH_R1}",
            )
        validate_duration_seconds(key.coordinate.duration_seconds)
        if self._closed:
            raise TransportError("session already closed")
        pair = (key.coordinate.source_symbol, key.coordinate.duration_seconds)
        self.subscribe_log.append({"symbol": pair[0], "duration": pair[1], "data_length": data_length})
        error = self._subscribe_errors.get(pair)
        if error is not None:
            raise error
        if pair not in self._frames:
            raise SymbolUnsupportedError(pair[0])
        serial_id = self._next_serial_id
        self._next_serial_id += 1
        self._serials[serial_id] = (key, self._frames[pair])
        self._update_counts.setdefault(pair, 0)
        self._live_bar_counter.setdefault(pair, 0)
        return SerialRef(serial_id=serial_id, coordinate=key.coordinate, data_length=data_length)

    def wait_update(self, deadline_seconds: float) -> bool:
        if self._closed:
            raise TransportError("session already closed")
        if self._step >= len(self._timeline):
            return False
        step = dict(self._timeline[self._step])
        self._step += 1
        event = step.get("event")
        if event == "update":
            self._maybe_set_clock(step)
            self._advance_frames(step)
            self._updates += 1
            return True
        if event == "deadline":
            return False
        if event == "disconnect":
            raise TransportError("synthetic scripted disconnect")
        if event == "rate_limit":
            raise RateLimitError(float(step.get("retry_after_seconds", 60.0)))
        if event == "permission_denied":
            raise PermissionDeniedError()
        raise SessionError(f"unknown synthetic timeline event {event!r}")

    def _maybe_set_clock(self, step: Mapping[str, Any]) -> None:
        """Timeline step hook: move a MUTABLE injected clock to a scripted
        instant before appending bars (deterministic P->C chains in tests)."""
        target = step.get("set_clock_nano")
        if target is not None and hasattr(self._clock_nano, "now"):
            self._clock_nano.now = int(target)

    def ready(self, serial: SerialRef) -> bool:
        entry = self._serials.get(serial.serial_id)
        if entry is None:
            _fail(ErrorCode.E_RUN_STATE_INVALID, f"unknown serial {serial.serial_id}")
        key, _frame = entry
        pair = (key.coordinate.source_symbol, key.coordinate.duration_seconds)
        if pair in self._never_ready:
            return False
        return self._update_counts.get(pair, 0) >= self._ready_after_updates

    def copy_snapshot(self, serial: SerialRef) -> SnapshotCopy:
        entry = self._serials.get(serial.serial_id)
        if entry is None:
            _fail(ErrorCode.E_RUN_STATE_INVALID, f"unknown serial {serial.serial_id}")
        key, frame = entry
        rows = _deep_copy_frame_rows(frame)
        return SnapshotCopy(
            serial=SerialRef(
                serial_id=serial.serial_id,
                coordinate=key.coordinate,
                data_length=SUBSCRIBE_DATA_LENGTH_R1,
            ),
            rows=rows,
            copied_at_update_seq=self._updates,
            detached=True,
        )

    def close(self) -> None:
        self._closed = True
        self.close_count += 1
        self._serials.clear()

    # -- helpers ---------------------------------------------------------------

    def _advance_frames(self, step: Mapping[str, Any]) -> None:
        """SDK-like in-place frame update: append bars to every live serial.

        Each ``update`` step appends ``advance_bars`` rows starting at
        ``bar_start_nano`` (default: the duration grid floor of the injected
        clock).  Rows appended here are the live/in-formation bars; whether a
        row is PROVISIONAL or CLOSED is decided downstream from the observed
        clock, exactly like a real SDK frame.
        """
        advance = int(step.get("advance_bars", 1))
        for _serial_id, (key, frame) in list(self._serials.items()):
            pair = (key.coordinate.source_symbol, key.coordinate.duration_seconds)
            if pair in self._never_ready:
                continue
            self._update_counts[pair] = self._update_counts.get(pair, 0) + 1
            duration = key.coordinate.duration_seconds
            default_start = (int(self._clock_nano()) // (duration * NANOSECOND)) * duration * NANOSECOND
            bar_start = int(step.get("bar_start_nano", default_start))
            counter = self._live_bar_counter.get(pair, 0)
            for offset in range(advance):
                counter += 1
                price = 4000.0 + counter
                frame.loc[len(frame)] = {  # type: ignore[index]
                    "datetime": float(bar_start + offset * duration * NANOSECOND),
                    "open": price,
                    "high": price + 3.0,
                    "low": price - 3.0,
                    "close": price + 1.0,
                    "volume": 100.0 + counter,
                    "open_oi": 5000.0 + counter,
                    "close_oi": 5010.0 + counter,
                }
            self._live_bar_counter[pair] = counter

    def record_baseline(self, baseline_snapshot_id: str) -> ConnectionBaseline:
        if not self._baseline_recorded or self._baseline is None:
            self._baseline = ConnectionBaseline(
                connection_epoch=self.connection_epoch,
                baseline_snapshot_id=baseline_snapshot_id,
                ready_update_seq=self._updates,
            )
            self._baseline_recorded = True
        return self._baseline

    @property
    def baseline(self) -> Optional[ConnectionBaseline]:
        return self._baseline

    @property
    def update_seq(self) -> int:
        return self._updates

    @property
    def closed(self) -> bool:
        return self._closed

    def now_nano(self) -> int:
        return int(self._clock_nano())


class SyntheticSessionFactory:
    """Factory producing :class:`SyntheticTimelineSession` instances.

    Every ``create`` call yields a FRESH session with a new internal epoch
    and a fresh timeline cursor; the frames dict is shared by reference so
    in-place mutation across sessions stays observable (SDK-like).  Sessions
    are SYNTHETIC and the engine marks every row/envelope accordingly.
    """

    def __init__(
        self,
        *,
        frames: Mapping[Tuple[str, int], Any],
        timelines: Sequence[Sequence[Mapping[str, Any]]],
        clock_nano: Callable[[], int],
        ready_after_updates: int = 0,
        never_ready_keys: Sequence[Tuple[str, int]] = (),
        subscribe_errors: Optional[Mapping[Tuple[str, int], SessionError]] = None,
    ) -> None:
        self._frames = dict(frames)
        self._timelines = list(timelines)
        self._clock_nano = clock_nano
        self._ready_after_updates = ready_after_updates
        self._never_ready = tuple(never_ready_keys)
        self._subscribe_errors = dict(subscribe_errors or {})
        self.sessions_created = 0
        self.created_sessions: list[SyntheticTimelineSession] = []

    def create(self) -> SyntheticTimelineSession:
        timeline_index = min(self.sessions_created, len(self._timelines) - 1)
        session = SyntheticTimelineSession(
            frames=self._frames,
            timeline=self._timelines[timeline_index],
            clock_nano=self._clock_nano,
            ready_after_updates=self._ready_after_updates,
            never_ready_keys=self._never_ready,
            subscribe_errors=self._subscribe_errors,
        )
        self.sessions_created += 1
        session.connection_epoch = self.sessions_created
        self.created_sessions.append(session)
        return session
