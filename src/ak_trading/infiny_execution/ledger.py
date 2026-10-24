"""File-backed, fail-closed simulation journal. Never owns a native port."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from typing import Callable

from . import contracts as c


@dataclass(frozen=True, slots=True, kw_only=True)
class LedgerStatus:
    revision: int
    reconcile: c.ReconcileState
    closed: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class RegisterResult:
    code: c.ResultCode
    intent: c.OrderIntent
    intent_hash: str
    revision: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextReceipt:
    snapshot_id: str
    revision: int


@dataclass(frozen=True, slots=True, kw_only=True)
class LegRecord:
    intent: c.OrderIntent
    plan: c.Plan
    leg: c.Leg
    state: c.OrderState
    attempt_no: int
    attempt_id: str | None
    order_id: int | None
    order_sys_id: str | None
    reported_filled: int
    trade_filled: int
    risk_filled: int
    trade_gap: int
    cancel_volume: int | None
    cancel_state: c.CancelState
    settled: bool
    reconcile_required: bool
    ownership_verified: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class AttemptResult:
    code: c.ResultCode
    send_permitted: bool
    attempt_id: str
    attempt_no: int
    leg: LegRecord
    revision: int


@dataclass(frozen=True, slots=True, kw_only=True)
class SubmitResult:
    code: c.ResultCode
    leg: LegRecord
    revision: int


@dataclass(frozen=True, slots=True, kw_only=True)
class IngestResult:
    code: c.ResultCode
    event_id: str
    leg_id: str | None
    reason: str | None
    revision: int


@dataclass(frozen=True, slots=True, kw_only=True)
class CancelAttempt:
    code: c.ResultCode
    cancel_permitted: bool
    cancel_id: str
    order_id: int
    leg: LegRecord
    revision: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ActorRecord:
    actor_id: str
    account_key: str
    bindings: tuple
    registration_revision: int
    enabled: bool
    operator_action_id: str
    manifest_sha256: str
    contract_lock_sha256: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryResult:
    code: c.ResultCode
    reconcile: c.ReconcileState
    revision: int
    unresolved_leg_ids: tuple[str, ...]
    unresolved_command_ids: tuple[str, ...] = ()


class LedgerError(RuntimeError):
    def __init__(self, code: c.ResultCode, blockers: tuple[str, ...]):
        self.code, self.blockers = code, blockers
        super().__init__(code.value + ":" + ",".join(blockers))


def _reject(*blockers: str, code: c.ResultCode = c.ResultCode.BLOCKED):
    raise LedgerError(code, tuple(blockers))


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _now(clock) -> datetime:
    value = clock()
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError("clock requires aware datetime")
    return value.astimezone(timezone.utc)


def _side(leg: c.Leg) -> c.Side:
    return c.Side.LONG if (leg.direction is c.Direction.BUY) == (leg.phase is c.Phase.OPEN) else c.Side.SHORT


def _bucket_available(pos: c.PositionBucket, bucket: c.Bucket | None) -> int:
    td_avail = (pos.PositionClose - pos.YdPositionClose) - (pos.FrozenClosing - pos.YdFrozenClosing)
    yd_avail = pos.YdPositionClose - pos.YdFrozenClosing
    if bucket is c.Bucket.TD:
        return td_avail
    if bucket is c.Bucket.YD:
        return yd_avail
    return td_avail + yd_avail


def _baseline_for(leg: c.Leg, snapshot: c.ExecutionSnapshot) -> str:
    """SDK truth captured at reserve time; absorption is measured against it, never assumed."""
    pos = next(x for x in snapshot.positions if x.side is _side(leg))
    if leg.phase is c.Phase.CLOSE:
        return _json({"kind": "close", "bucket_available": _bucket_available(pos, leg.position_bucket), "position": pos.Position})
    cash = snapshot.account.available if snapshot.account is not None else None
    return _json({"kind": "open", "cash_available": None if cash is None else str(cash), "position": pos.Position})



def _absorption_verdict(record: LegRecord, snapshot: c.ExecutionSnapshot, baseline, cash_reserve: Decimal):
    """Direct-evidence absorption test shared by simulation barriers and native settlement."""
    pos = next((x for x in snapshot.positions if x.side is _side(record.leg)), None)
    absorbed = False
    released = False
    if baseline is not None and pos is not None and pos.Position is not None:
        if record.leg.phase is c.Phase.CLOSE:
            if all(pos.__getattribute__(name) is not None for name in ("PositionClose", "YdPositionClose", "FrozenClosing", "YdFrozenClosing")):
                bucket = record.leg.position_bucket
                remaining = record.leg.volume - record.risk_filled
                bucket_frozen = (pos.FrozenClosing - pos.YdFrozenClosing if bucket is c.Bucket.TD
                                 else pos.YdFrozenClosing if bucket is c.Bucket.YD else pos.FrozenClosing)
                # The exchange must actually freeze this leg's unfilled lots;
                # a smaller available alone can be nothing but our own fill.
                absorbed = bucket_frozen >= remaining and pos.Position <= baseline["position"] - record.risk_filled
                # Filled close lots must actually be gone from the position.
                released = pos.Position <= baseline["position"] - record.risk_filled
        else:
            cash_now = snapshot.account.available if snapshot.account is not None else None
            if baseline["cash_available"] and cash_now is not None:
                absorbed = cash_now <= Decimal(baseline["cash_available"]) - cash_reserve
            released = pos.Position >= baseline["position"] + record.risk_filled
    return pos, absorbed, released


def _scope(binding: c.Binding):
    return (binding.account_key, binding.exchange, binding.instrument_id, binding.hedge_flag)


def _scope_key(binding: c.Binding) -> str:
    """Deterministic storage key for the stable (account,strategy,exchange,instrument,hedge) scope."""
    return "|".join((binding.account_key, binding.strategy_key, binding.exchange,
                     binding.instrument_id, binding.hedge_flag))


def _context_key(binding: c.Binding) -> str:
    return "context:" + _hash("|".join((binding.account_key, binding.strategy_key, binding.exchange,
                                      binding.instrument_id, binding.hedge_flag)))


def _snapshot_business(snapshot: c.ExecutionSnapshot) -> str:
    return c.encode(replace(snapshot, ledger_revision=0))


def _plan_identity(plan: c.Plan) -> str:
    def decimal_text(value):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    expiry = plan.expires_at.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    legs = ";".join(",".join((x.leg_id, x.phase.value, x.direction.value, x.offset.value, str(x.volume),
                              decimal_text(x.price), x.position_bucket.value if x.position_bucket else "-",
                              decimal_text(x.cash_reserve), "+".join(sorted(x.depends_on)) or "-")) for x in plan.legs)
    return _hash("|".join(("infiny_execution_plan_v1", plan.intent_hash, plan.snapshot_id,
                           str(plan.ledger_revision), plan.policy_id, expiry, legs)))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
 id TEXT PRIMARY KEY, account_key TEXT NOT NULL, strategy_key TEXT NOT NULL,
 intent_id TEXT NOT NULL, source_key TEXT NOT NULL, payload TEXT NOT NULL,
 payload_hash TEXT NOT NULL, plan TEXT, receipt TEXT,
 UNIQUE(account_key,strategy_key,intent_id), UNIQUE(account_key,strategy_key,source_key));
CREATE TABLE IF NOT EXISTS legs (
 leg_id TEXT PRIMARY KEY, intent_key TEXT NOT NULL REFERENCES intents(id),
 plan_id TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
 attempt_no INTEGER NOT NULL DEFAULT 0, attempt_id TEXT, attempted_at TEXT,
 order_id INTEGER, order_sys_id TEXT, front_id INTEGER, session_id INTEGER,
 reported_filled INTEGER NOT NULL DEFAULT 0, trade_filled INTEGER NOT NULL DEFAULT 0,
 cancel_volume INTEGER, cancel_state TEXT NOT NULL DEFAULT 'NONE', cancel_id TEXT,
 owned INTEGER NOT NULL DEFAULT 0, confirmed INTEGER NOT NULL DEFAULT 0,
 settled INTEGER NOT NULL DEFAULT 0, unknown INTEGER NOT NULL DEFAULT 0,
 submit_result TEXT);
CREATE TABLE IF NOT EXISTS events (
 event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
 payload_hash TEXT NOT NULL, leg_id TEXT, reason TEXT, applied INTEGER NOT NULL DEFAULT 0,
 recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trades (
 account_key TEXT NOT NULL, trading_day TEXT NOT NULL, exchange TEXT NOT NULL,
 instrument_id TEXT NOT NULL, trade_id TEXT NOT NULL, leg_id TEXT NOT NULL REFERENCES legs(leg_id),
 payload TEXT NOT NULL, volume INTEGER NOT NULL, price TEXT NOT NULL,
 PRIMARY KEY(account_key,trading_day,exchange,instrument_id,trade_id));
CREATE TABLE IF NOT EXISTS reservations (
 reservation_id TEXT PRIMARY KEY, leg_id TEXT NOT NULL REFERENCES legs(leg_id),
 resource_key TEXT NOT NULL, state TEXT NOT NULL, volume INTEGER NOT NULL,
 cash_reserve TEXT NOT NULL, not_proven_reflected INTEGER NOT NULL,
 reflected_evidence_id TEXT, reflected_snapshot_id TEXT, baseline TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS active_reservation_leg ON reservations(leg_id) WHERE state!='RELEASED';
"""

# v2 (NE1) additions: dispatch/outbox audit tables and the control CAS record.
# They are created only for fresh journals; v1 journals keep refusing dispatch
# APIs until the dedicated migration copies them forward.
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS control_state (
 scope TEXT PRIMARY KEY, control_revision INTEGER NOT NULL, payload TEXT NOT NULL,
 permit_id TEXT, send_budget INTEGER, cancel_budget INTEGER, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dispatch_commands (
 command_id TEXT PRIMARY KEY, attempt_id TEXT UNIQUE, kind TEXT NOT NULL, offer_id TEXT NOT NULL,
 intent_id TEXT NOT NULL, leg_id TEXT NOT NULL, plan_id TEXT NOT NULL, binding_hash TEXT NOT NULL,
 control_revision INTEGER NOT NULL, permit_id TEXT, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
 issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0, closed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS dispatch_receipts (
 receipt_id TEXT PRIMARY KEY, command_id TEXT NOT NULL REFERENCES dispatch_commands(command_id),
 stage TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox_ack (
 binding_hash TEXT PRIMARY KEY, acked_event_id TEXT NOT NULL, acked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS actor_registrations (
 actor_id TEXT PRIMARY KEY, account_key TEXT NOT NULL, registration_revision INTEGER NOT NULL,
 payload TEXT NOT NULL, payload_hash TEXT NOT NULL, enabled INTEGER NOT NULL,
 operator_action_id TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, contract_lock_sha256 TEXT NOT NULL,
 registered_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operator_manifests (
 registration_id TEXT PRIMARY KEY, payload TEXT NOT NULL, payload_hash TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_extra (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS http_requests (
 namespace TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT, recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshot_captures (
 snapshot_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, session_epoch TEXT NOT NULL,
 generation TEXT NOT NULL, raw_hash TEXT NOT NULL, trusted_received_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event_stream_state (
 event_stream_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL, session_epoch TEXT NOT NULL,
 generation TEXT NOT NULL, last_sequence INTEGER NOT NULL DEFAULT 0,
 acked_sequence INTEGER NOT NULL DEFAULT 0, acked_event_id TEXT, acked_event_hash TEXT);
CREATE TABLE IF NOT EXISTS event_stream_events (
 event_stream_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_id TEXT NOT NULL,
 event_sha256 TEXT NOT NULL, PRIMARY KEY(event_stream_id, sequence));
CREATE UNIQUE INDEX IF NOT EXISTS event_stream_event_id ON event_stream_events(event_stream_id, event_id);
CREATE TABLE IF NOT EXISTS offer_deadlines (
 offer_id TEXT PRIMARY KEY, action_key TEXT NOT NULL, kind TEXT NOT NULL, cancel_id TEXT,
 payload TEXT NOT NULL, payload_hash TEXT NOT NULL, first_issued_at TEXT NOT NULL,
 offer_expires_at TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS offer_action_identity ON offer_deadlines(action_key);
CREATE TABLE IF NOT EXISTS offer_action_tombstones (
 action_key TEXT PRIMARY KEY, reason TEXT NOT NULL, recorded_at TEXT NOT NULL);
"""



class Ledger:
    def __init__(self, path: str | Path, *, clock: Callable[[], datetime] | None = None,
                 busy_timeout_ms: int = 30000, schema: str = "v2"):
        if str(path) == ":memory:" or not str(path) or str(path).startswith("file:"):
            raise ValueError("a local SQLite file is required")
        if schema not in ("v1", "v2"):
            raise ValueError("schema must be 'v1' or 'v2'")
        if type(busy_timeout_ms) is not int or busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms requires a nonnegative integer")
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._closed = False
        self._fault_reason: c.ReconcileReason | None = None
        self._conn = None
        try:
            self._conn = sqlite3.connect(str(self.path), timeout=busy_timeout_ms / 1000,
                                         isolation_level=None, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            # WAL keeps external readers (migration, evidence export) lock-free
            # against this journal's writes; busy_timeout above is already set,
            # so the one-time mode switch of an existing journal waits politely.
            self._conn.execute("PRAGMA journal_mode=WAL")
            if self._conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("corrupt journal")
            fresh = self._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intents'").fetchone() is None
            self._conn.executescript(_SCHEMA)
            marker = None
            if not fresh:
                try:
                    marker = self._conn.execute("SELECT value FROM ledger_meta WHERE key='schema'").fetchone()
                except sqlite3.Error:
                    marker = None
            if fresh and schema == "v2":
                self._conn.executescript(_SCHEMA_V2)
                self._conn.execute("INSERT OR REPLACE INTO ledger_meta VALUES('schema','v2')")
                self._conn.execute("INSERT OR REPLACE INTO ledger_meta VALUES('ofd_feature','installed')")
            elif marker is not None and marker[0] == "v2":
                # An already-v2 journal adopts newer v2 tables idempotently; v1 stays untouched.
                self._conn.executescript(_SCHEMA_V2)
                self._install_first_offer_feature()

            if "baseline" not in {row[1] for row in self._conn.execute("PRAGMA table_info(reservations)")}:
                self._conn.execute("ALTER TABLE reservations ADD COLUMN baseline TEXT")
            with self._tx():
                pending = self._conn.execute("SELECT leg_id,state,cancel_state FROM legs WHERE attempt_no>0 AND settled=0").fetchall()
                for row in pending:
                    if row["state"] == c.OrderState.SUBMITTING.value:
                        self._conn.execute("UPDATE legs SET state='SUBMIT_UNKNOWN',unknown=1 WHERE leg_id=?", (row["leg_id"],))
                    if row["cancel_state"] == c.CancelState.REQUESTED.value:
                        self._conn.execute("UPDATE legs SET cancel_state='UNKNOWN' WHERE leg_id=?", (row["leg_id"],))
                if pending:
                    self._latch(c.ReconcileReason.RESTART_UNRESOLVED, "restart")
                    self._bump()
        except sqlite3.Error as exc:
            if self._conn is not None:
                self._conn.close()
            self._closed = True
            raise LedgerError(c.ResultCode.PERSISTENCE_ERROR, ("LEDGER_CORRUPT",)) from exc

    def __enter__(self) -> Ledger:
        self._ensure()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def _ensure(self):
        if self._closed:
            _reject("LEDGER_CLOSED", code=c.ResultCode.PERSISTENCE_ERROR)

    @contextmanager
    def _tx(self):
        with self._lock:
            self._ensure()
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                if self._fault_reason is not None:
                    self._latch(self._fault_reason, "storage")
                yield
                self._conn.execute("COMMIT")
            except sqlite3.Error as exc:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                reason = c.ReconcileReason.LEDGER_CORRUPT if "malformed" in str(exc) or "not a database" in str(exc) else c.ReconcileReason.LEDGER_FAILURE
                self._fault_reason = reason  # A failure latch only, never a successful projection.
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    self._latch(reason, "storage")
                    self._conn.execute("COMMIT")
                except sqlite3.Error:
                    try:
                        self._conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise LedgerError(c.ResultCode.PERSISTENCE_ERROR, (reason.value,)) from exc
            except BaseException as exc:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                pending = getattr(self, "_pending_latches", None)
                if isinstance(exc, LedgerError) and pending:
                    # Business rejections roll back; their failure latches survive.
                    self._pending_latches = []
                    try:
                        self._conn.execute("BEGIN IMMEDIATE")
                        for reason, evidence, leg_id in pending:
                            self._latch(reason, evidence)
                            if leg_id is not None:
                                self._conn.execute("UPDATE legs SET unknown=1 WHERE leg_id=?", (leg_id,))
                        self._conn.execute("COMMIT")
                    except sqlite3.Error:
                        if self._conn.in_transaction:
                            try:
                                self._conn.execute("ROLLBACK")
                            except sqlite3.Error:
                                pass
                raise

    def _revision(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def _bump(self) -> int:
        value = self._revision() + 1
        self._conn.execute(f"PRAGMA user_version={value}")
        return value

    def _event(self, identity, kind, payload, *, leg_id=None, reason=None, applied=True):
        self._conn.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?)",
                           (identity, kind, payload, _hash(payload), leg_id, reason, int(applied), _now(self._clock).isoformat()))

    def _latch(self, reason: c.ReconcileReason, evidence_id: str):
        identity = "latch:" + reason.value
        if self._conn.execute("SELECT 1 FROM events WHERE event_id=?", (identity,)).fetchone() is None:
            self._event(identity, "LATCH", _json({"evidence_id": evidence_id}), reason=reason.value)

    def _reconcile(self) -> c.ReconcileState:
        reasons = {c.ReconcileReason(row[0]) for row in self._conn.execute("SELECT reason FROM events WHERE kind='LATCH' AND applied=1")}
        if self._fault_reason:
            reasons.add(self._fault_reason)
        for row in self._conn.execute("SELECT state,unknown,reported_filled,trade_filled,cancel_state FROM legs WHERE attempt_no>0"):
            if row["unknown"] or row["state"] == c.OrderState.SUBMIT_UNKNOWN.value:
                reasons.add(c.ReconcileReason.SUBMIT_UNKNOWN)
            if row["reported_filled"] > row["trade_filled"]:
                reasons.add(c.ReconcileReason.TRADE_GAP)
            if row["cancel_state"] == c.CancelState.UNKNOWN.value:
                reasons.add(c.ReconcileReason.CANCEL_UNKNOWN)
        if self._conn.execute("SELECT 1 FROM events WHERE kind='CALLBACK' AND applied=0 LIMIT 1").fetchone():
            reasons.add(c.ReconcileReason.OWNERSHIP_CONFLICT)
        if not reasons:
            return c.ReconcileState()
        row = self._conn.execute("SELECT MIN(recorded_at) FROM events").fetchone()
        return c.ReconcileState(required=True, reasons=tuple(sorted(reasons, key=lambda r: r.value)),
                                since=datetime.fromisoformat(row[0]) if row[0] else _now(self._clock), evidence_id="journal")

    def _timeout(self):
        now = _now(self._clock)
        changed = False
        for row in self._conn.execute("SELECT * FROM legs WHERE attempt_no=1 AND confirmed=0 AND state IN ('SUBMITTING','SUBMITTED_LOCAL')").fetchall():
            if now - datetime.fromisoformat(row["attempted_at"]) >= timedelta(seconds=10):
                self._conn.execute("UPDATE legs SET state='SUBMIT_UNKNOWN',unknown=1 WHERE leg_id=?", (row["leg_id"],))
                changed = True
        if changed:
            self._bump()

    def check(self) -> LedgerStatus:
        with self._tx():
            if self._conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("malformed journal")
            self._timeout()
            result = LedgerStatus(revision=self._revision(), reconcile=self._reconcile(), closed=False)
        return result

    def register(self, intent: c.OrderIntent) -> RegisterResult:
        payload, digest = c.encode(intent), c.canonical_hash(intent)
        b = intent.binding
        with self._tx():
            rows = self._conn.execute("SELECT * FROM intents WHERE account_key=? AND strategy_key=? AND (intent_id=? OR source_key=?)",
                                      (b.account_key, b.strategy_key, intent.intent_id, intent.source_key)).fetchall()
            if rows:
                if len(rows) != 1 or rows[0]["payload_hash"] != digest:
                    _reject("INTENT_CONFLICT", code=c.ResultCode.INTENT_CONFLICT)
                result = RegisterResult(code=c.ResultCode.IDEMPOTENT, intent=c.decode(rows[0]["payload"], c.OrderIntent), intent_hash=digest, revision=self._revision())
            else:
                self._conn.execute("INSERT INTO intents(id,account_key,strategy_key,intent_id,source_key,payload,payload_hash) VALUES(?,?,?,?,?,?,?)",
                                   (_hash("|".join((b.account_key, b.strategy_key, intent.intent_id))), b.account_key, b.strategy_key,
                                    intent.intent_id, intent.source_key, payload, digest))
                result = RegisterResult(code=c.ResultCode.OK, intent=intent, intent_hash=digest, revision=self._revision())
        return result

    def _adopt_snapshots(self, context: c.ExecutionContext, resources: tuple) -> None:
        """AP-01 step 4: adopt the candidate observation/resources inside the
        caller's transaction. Registers the snapshots (idempotent on identical
        business content, conflict on changed content) WITHOUT writing the
        context row, so later in-transaction checks consume the adopted
        observation instead of requiring a pre-registered persisted snapshot."""
        if context.binding != context.snapshot.binding:
            _reject("ACCOUNT_MISMATCH")
        for snapshot in (context.snapshot,) + resources:
            if type(snapshot) is not c.ExecutionSnapshot:
                raise TypeError("ExecutionSnapshot required")
            if (snapshot.binding.account_key, snapshot.binding.session_epoch, snapshot.binding.trading_day) != \
                    (context.binding.account_key, context.binding.session_epoch, context.binding.trading_day):
                _reject("ACCOUNT_MISMATCH", "SESSION_CHANGED")
            identity = "snapshot:" + snapshot.snapshot_id
            existing = self._conn.execute("SELECT payload FROM events WHERE event_id=?", (identity,)).fetchone()
            if existing:
                if _snapshot_business(c.decode(existing[0], c.ExecutionSnapshot)) != _snapshot_business(snapshot):
                    _reject("SNAPSHOT_CONFLICT")
            else:
                self._event(identity, "SNAPSHOT", c.encode(snapshot))

    def _store_context(self, context: c.ExecutionContext, resources: tuple[c.ExecutionSnapshot, ...]) -> ContextReceipt:
        if type(context) is not c.ExecutionContext or type(resources) is not tuple:
            raise TypeError("typed context and tuple resources required")
        if context.binding != context.snapshot.binding:
            _reject("ACCOUNT_MISMATCH")
        self._adopt_snapshots(context, resources)
        identity = _context_key(context.binding)
        self._conn.execute("DELETE FROM events WHERE event_id=?", (identity,))
        self._event(identity, "CONTEXT", c.encode(context))
        return ContextReceipt(snapshot_id=context.snapshot.snapshot_id, revision=self._revision())

    def register_context(self, context: c.ExecutionContext, *, resources: tuple[c.ExecutionSnapshot, ...] = ()) -> ContextReceipt:
        with self._tx():
            result = self._store_context(context, resources)
        return result

    def compare_and_register_context(self, context: c.ExecutionContext, expected_context: c.ExecutionContext, *,
                                     resources: tuple[c.ExecutionSnapshot, ...] = ()) -> ContextReceipt:
        if type(context) is not c.ExecutionContext or type(expected_context) is not c.ExecutionContext:
            raise TypeError("typed context and expected_context required")
        with self._tx():
            current = self._context(expected_context.binding)
            if current is None or c.canonical_hash(current) != c.canonical_hash(expected_context):
                _reject("CONTEXT_CONFLICT")
            if any(getattr(context, field) != getattr(expected_context, field)
                   for field in ("mode", "binding", "policy", "authorization", "route_ownership")):
                _reject("CONTEXT_CONFLICT")
            result = self._store_context(context, resources)
        return result

    def initialize_context(self, context: c.ExecutionContext, *, resources: tuple[c.ExecutionSnapshot, ...] = ()) -> ContextReceipt:
        """Atomically create the scope's first context; an existing scope is never overwritten."""
        if type(context) is not c.ExecutionContext:
            raise TypeError("typed context required")
        with self._tx():
            current = self._context(context.binding)
            if current is not None:
                # The winner's controls stand; late initializers get a conflict, not a write.
                _reject("CONTEXT_CONFLICT")
            result = self._store_context(context, resources)
        return result

    def _context(self, binding: c.Binding) -> c.ExecutionContext | None:
        row = self._conn.execute("SELECT payload FROM events WHERE event_id=?", (_context_key(binding),)).fetchone()
        return c.decode(row[0], c.ExecutionContext) if row else None

    def context(self, binding: c.Binding) -> c.ExecutionContext | None:
        with self._tx():
            return self._context(binding)

    def _snapshots(self, account_key: str, *, epoch=None, day=None) -> tuple[c.ExecutionSnapshot, ...]:
        latest = {}
        for row in self._conn.execute("SELECT payload FROM events WHERE kind='SNAPSHOT' ORDER BY rowid"):
            snapshot = c.decode(row[0], c.ExecutionSnapshot)
            b = snapshot.binding
            if b.account_key != account_key or (epoch is not None and b.session_epoch != epoch) or (day is not None and b.trading_day != day):
                continue
            key = _scope(b)
            old = latest.get(key)
            if old is None or (snapshot.times.sampled_at is not None and
                               (old.times.sampled_at is None or snapshot.times.sampled_at >= old.times.sampled_at)):
                latest[key] = snapshot
        return tuple(latest.values())

    def _view(self, account_key: str, snapshot_id=None) -> c.ReservationView:
        reservations = []
        cash = Decimal(0)
        opens = 0
        for row in self._conn.execute("SELECT r.*,l.payload AS leg FROM reservations r JOIN legs l USING(leg_id) JOIN intents i ON i.id=l.intent_key WHERE i.account_key=?", (account_key,)):
            key = c.decode(row["resource_key"], c.ReservationKey)
            leg = c.decode(row["leg"], c.Leg)
            reservation = c.Reservation(resource_key=key, leg_id=row["leg_id"], state=c.ReservationState(row["state"]),
                                        volume=row["volume"], cash_reserve=Decimal(row["cash_reserve"]),
                                        not_proven_reflected=row["not_proven_reflected"], reflected_evidence_id=row["reflected_evidence_id"])
            reservations.append(reservation)
            if reservation.state is not c.ReservationState.RELEASED:
                cash += reservation.cash_reserve
                if leg.phase is c.Phase.OPEN:
                    opens += reservation.volume
        snapshots = self._snapshots(account_key)
        gross = sum(item.Position or 0 for snap in snapshots for item in snap.positions)
        inflight = sum(c.OrderState(row[0]) in c.INFLIGHT_ORDER_STATES for row in self._conn.execute(
            "SELECT state FROM legs l JOIN intents i ON i.id=l.intent_key WHERE i.account_key=?", (account_key,)))
        return c.ReservationView(account_key=account_key, snapshot_id=snapshot_id, ledger_revision=self._revision(),
                                  reservations=tuple(reservations), cash_reserved=cash, gross_position=gross,
                                  inflight_legs=inflight, reconcile=self._reconcile())

    def view(self, account_key: str, *, snapshot_id: str | None = None) -> c.ReservationView:
        with self._tx():
            self._timeout()
            if snapshot_id is not None:
                row = self._conn.execute("SELECT payload FROM events WHERE event_id=?", ("snapshot:" + snapshot_id,)).fetchone()
                if row is None or c.decode(row[0], c.ExecutionSnapshot).binding.account_key != account_key:
                    _reject("ACCOUNT_MISMATCH")
            return self._view(account_key, snapshot_id)

    def _row(self, leg_id):
        row = self._conn.execute("SELECT * FROM legs WHERE leg_id=?", (leg_id,)).fetchone()
        if row is None:
            _reject("LEG_NOT_FOUND", code=c.ResultCode.NOT_FOUND)
        return row

    def _record(self, leg_id: str) -> LegRecord:
        row = self._row(leg_id)
        intent = self._conn.execute("SELECT * FROM intents WHERE id=?", (row["intent_key"],)).fetchone()
        gap = max(row["reported_filled"] - row["trade_filled"], 0)
        plan_row = self._conn.execute("SELECT payload FROM events WHERE event_id=?", ("plan:" + row["plan_id"],)).fetchone()
        return LegRecord(intent=c.decode(intent["payload"], c.OrderIntent), plan=c.decode(plan_row[0] if plan_row else intent["plan"], c.Plan),
                         leg=c.decode(row["payload"], c.Leg), state=c.OrderState(row["state"]), attempt_no=row["attempt_no"],
                         attempt_id=row["attempt_id"], order_id=row["order_id"], order_sys_id=row["order_sys_id"],
                         reported_filled=row["reported_filled"], trade_filled=row["trade_filled"],
                         risk_filled=max(row["reported_filled"], row["trade_filled"]), trade_gap=gap,
                         cancel_volume=row["cancel_volume"], cancel_state=c.CancelState(row["cancel_state"]),
                         settled=bool(row["settled"]), reconcile_required=bool(row["unknown"] or gap or row["cancel_state"] == "UNKNOWN"),
                         ownership_verified=bool(row["owned"]))

    def get_leg(self, leg_id: str) -> LegRecord:
        with self._tx():
            return self._record(leg_id)

    def _intent_for_plan(self, plan: c.Plan):
        row = self._conn.execute("SELECT * FROM intents WHERE payload_hash=?", (plan.intent_hash,)).fetchone()
        if row is None:
            _reject("INTENT_NOT_REGISTERED", code=c.ResultCode.NOT_FOUND)
        return row, c.decode(row["payload"], c.OrderIntent)

    def _budget(self, intent: c.OrderIntent, legs: tuple[c.Leg, ...], context: c.ExecutionContext,
                *, claimed: tuple[str, ...] = ()):
        b, p, s = intent.binding, context.policy, context.snapshot
        now = _now(self._clock)
        if context.binding != b or s.binding != b:
            _reject("ACCOUNT_MISMATCH")
        timing = c.validate_snapshot_timing(s, now, p.timing)
        if not timing.ok:
            _reject(*timing.blockers)
        if now >= intent.expires_at or now >= p.valid_until:
            _reject("EXPIRED")
        if s.account is None or s.account.available is None or p.max_cash_reserve is None:
            _reject("INSUFFICIENT_CASH")
        if s.contract is None or s.contract.min_limit_order_volume is None or s.contract.max_limit_order_volume is None or p.max_leg_qty is None:
            _reject("CONTRACT_MISSING")
        if p.max_gross_position is None:
            _reject("GROSS_POSITION_LIMIT")
        snaps = self._snapshots(b.account_key, epoch=b.session_epoch, day=b.trading_day)
        for snap in snaps:
            if {x.side for x in snap.positions} != {c.Side.LONG, c.Side.SHORT} or any(x.Position is None for x in snap.positions):
                _reject("MISSING_POSITION_BUCKET")
            for item in snap.positions:
                if not c.validate_timing(item.timing, now, p.timing.position_ttl, policy=p.timing).ok:
                    _reject("SNAPSHOT_STALE")
        current = next((x for x in snaps if _scope(x.binding) == _scope(b)), None)
        if current is None or _snapshot_business(current) != _snapshot_business(s):
            _reject("SNAPSHOT_STALE")
        view = self._view(b.account_key, s.snapshot_id)
        other_cash = Decimal(0)
        committed_cash = Decimal(0)
        other_opens = 0
        occupied = {}
        for item in view.reservations:
            if item.leg_id in claimed or item.state is c.ReservationState.RELEASED:
                continue
            # Reflection belongs to a particular proven snapshot, never inferred from balances.
            reflection = self._conn.execute("SELECT reflected_snapshot_id FROM reservations WHERE leg_id=? AND state!='RELEASED'", (item.leg_id,)).fetchone()
            key = item.resource_key
            committed_cash += item.cash_reserve
            reflected = item.state is c.ReservationState.REFLECTED and any(x.snapshot_id == reflection[0] and x.proof_id == item.reflected_evidence_id for x in snaps)
            existing_leg = self._record(item.leg_id)
            if existing_leg.leg.phase is c.Phase.OPEN:
                other_opens += item.volume - existing_leg.risk_filled if reflected else item.volume
            if not reflected:
                other_cash += item.cash_reserve
                if existing_leg.leg.phase is c.Phase.CLOSE:
                    occupied[key] = occupied.get(key, 0) + item.volume
        cash = Decimal(0)
        new_opens = 0
        needed = {}
        for leg in legs:
            if leg.volume < s.contract.min_limit_order_volume or leg.volume > min(s.contract.max_limit_order_volume, p.max_leg_qty):
                _reject("VOLUME_INVALID")
            for limit in s.contract.additional_volume_limits:
                if not limit.verified or not limit.min_volume <= leg.volume <= limit.max_volume or leg.volume % limit.volume_step:
                    _reject("VOLUME_INVALID")
            if leg.phase is c.Phase.OPEN:
                if p.cash_per_open_lot is None or p.cash_bound_source is None or not p.cash_bound_source.verified or p.cash_bound_source.source is not c.Source.SIMULATION:
                    _reject("CASH_BOUND_UNVERIFIED")
                required = p.cash_per_open_lot * leg.volume
                new_opens += leg.volume
            else:
                if p.fee_per_close_lot is None:
                    _reject("CASH_BOUND_MISSING")
                required = p.fee_per_close_lot * leg.volume
                key = c.ReservationKey(account_key=b.account_key, exchange=b.exchange, instrument_id=b.instrument_id,
                                       hedge_flag=b.hedge_flag, side=_side(leg), bucket=leg.position_bucket)
                needed[key] = needed.get(key, 0) + leg.volume
            if leg.cash_reserve < required:
                _reject("INSUFFICIENT_CASH")
            cash += leg.cash_reserve
        available_cash = min([s.account.available] + [x.account.available for x in snaps if x.account is not None and x.account.available is not None])
        if other_cash + cash > available_cash or committed_cash + cash > p.max_cash_reserve:
            _reject("INSUFFICIENT_CASH")
        gross = sum(x.Position for snap in snaps for x in snap.positions)
        if gross + other_opens + new_opens > p.max_gross_position:
            _reject("GROSS_POSITION_LIMIT")
        for key, qty in needed.items():
            pos = next((x for x in s.positions if x.side is key.side), None)
            if pos is None or any(getattr(pos, name) is None for name in ("Position", "PositionClose", "FrozenClosing", "YdPositionClose", "YdFrozenClosing")):
                _reject("MISSING_POSITION_BUCKET")
            td = pos.PositionClose - pos.YdPositionClose - pos.FrozenClosing + pos.YdFrozenClosing
            yd = pos.YdPositionClose - pos.YdFrozenClosing
            available = td if key.bucket is c.Bucket.TD else yd if key.bucket is c.Bucket.YD else td + yd
            # ALL overlaps both today/yesterday; refuse cross-policy overlap too.
            used = sum(amount for other, amount in occupied.items() if
                       (other.account_key, other.exchange, other.instrument_id, other.hedge_flag, other.side) ==
                       (key.account_key, key.exchange, key.instrument_id, key.hedge_flag, key.side) and
                       (other.bucket is key.bucket or c.Bucket.ALL in (other.bucket, key.bucket)))
            if qty > max(0, available - used):
                _reject("INSUFFICIENT_POSITION")

    def reserve(self, plan: c.Plan, expected_revision: int) -> c.ReservationReceipt:
        if type(expected_revision) is not int:
            raise TypeError("integer revision required")
        with self._tx():
            row, intent = self._intent_for_plan(plan)
            if row["plan"]:
                previous = c.decode(row["plan"], c.Plan)
                if previous == plan:
                    return c.decode(row["receipt"], c.ReservationReceipt)
                _reject("PLAN_ALREADY_RESERVED")
            if expected_revision != self._revision() or plan.ledger_revision != expected_revision:
                _reject("LEDGER_REVISION_MISMATCH", code=c.ResultCode.REVISION_CONFLICT)
            if self._reconcile().required:
                _reject("RECONCILE_REQUIRED")
            context = self._context(intent.binding)
            if context is None:
                _reject("CONTEXT_REQUIRED")
            if plan.blockers:
                _reject(*plan.blockers)
            if plan.snapshot_id != context.snapshot.snapshot_id or plan.policy_id != context.policy.policy_id:
                _reject("SNAPSHOT_STALE")
            if context.ledger_revision != expected_revision or context.snapshot.ledger_revision != expected_revision:
                _reject("LEDGER_REVISION_MISMATCH", code=c.ResultCode.REVISION_CONFLICT)
            if plan.expires_at <= _now(self._clock):
                _reject("EXPIRED")
            self._validate_shape(intent, plan.legs)
            self._budget(intent, plan.legs, context)
            ids = []
            for leg in plan.legs:
                if self._conn.execute("SELECT 1 FROM legs WHERE leg_id=?", (leg.leg_id,)).fetchone():
                    _reject("LEG_CONFLICT")
                self._conn.execute("INSERT INTO legs(leg_id,intent_key,plan_id,payload,state) VALUES(?,?,?,?,?)",
                                   (leg.leg_id, row["id"], plan.plan_id, c.encode(leg), c.OrderState.RESERVED.value))
                key = c.ReservationKey(account_key=intent.binding.account_key, exchange=intent.binding.exchange,
                                       instrument_id=intent.binding.instrument_id, hedge_flag=intent.binding.hedge_flag,
                                       side=_side(leg), bucket=leg.position_bucket or c.Bucket.ALL)
                identity = leg.leg_id
                self._conn.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?,?)", (identity, leg.leg_id, c.encode(key),
                                   c.ReservationState.HELD.value, leg.volume, str(leg.cash_reserve), leg.volume, None, None,
                                   _baseline_for(leg, context.snapshot)))
                ids.append(identity)
            receipt = c.ReservationReceipt(plan_id=plan.plan_id, reservation_ids=tuple(ids), revision=self._bump())
            self._conn.execute("UPDATE intents SET plan=?,receipt=? WHERE id=?", (c.encode(plan), c.encode(receipt), row["id"]))
            self._event("plan:" + plan.plan_id, "PLAN", c.encode(plan))
        return receipt

    def _validate_shape(self, intent: c.OrderIntent, legs: tuple[c.Leg, ...]):
        open_side = c.Direction.BUY if intent.action in (c.Action.OPEN_LONG, c.Action.REVERSE_TO_LONG) else c.Direction.SELL
        close_side = c.Direction.SELL if intent.action in (c.Action.CLOSE_LONG, c.Action.REVERSE_TO_SHORT) else c.Direction.BUY
        opens = [x for x in legs if x.phase is c.Phase.OPEN]
        closes = [x for x in legs if x.phase is c.Phase.CLOSE]
        reverse = intent.action in (c.Action.REVERSE_TO_LONG, c.Action.REVERSE_TO_SHORT)
        expected_open = intent.action in (c.Action.OPEN_LONG, c.Action.OPEN_SHORT) or reverse
        if (sum(x.volume for x in opens) != (intent.volume if expected_open else 0)) or \
                (not reverse and sum(x.volume for x in closes) != (0 if expected_open else intent.volume)):
            _reject("QUANTITY_CONFLICT")
        counters = {}
        for leg in legs:
            bucket = leg.position_bucket.value if leg.position_bucket is not None else "-"
            group = (leg.phase, bucket)
            index = counters.get(group, 0)
            counters[group] = index + 1
            if leg.leg_id != f"{intent.intent_id}:{leg.phase.value}:{bucket}:{index}":
                _reject("LEG_ID_INVALID")
            if leg.direction is not (open_side if leg.phase is c.Phase.OPEN else close_side) or leg.price != (intent.prices.open if leg.phase is c.Phase.OPEN else intent.prices.close):
                _reject("QUANTITY_CONFLICT")
            if leg.phase is c.Phase.CLOSE:
                expected = {c.Bucket.TD: c.Offset.CLOSE_TODAY, c.Bucket.YD: c.Offset.CLOSE_YESTERDAY} if intent.binding.exchange in ("SHFE", "INE") else {c.Bucket.ALL: c.Offset.CLOSE}
                if expected.get(leg.position_bucket) is not leg.offset:
                    _reject("UNSUPPORTED_CLOSE_POLICY")
        if reverse and (not closes or any(set(x.depends_on) != {y.leg_id for y in closes} for x in opens)):
            _reject("QUANTITY_CONFLICT")

    def _receipt(self, record: LegRecord, context: c.ExecutionContext):
        receipt = context.reservation_receipt
        stored = self._conn.execute("SELECT receipt FROM intents WHERE payload_hash=?", (c.canonical_hash(record.intent),)).fetchone()
        actual = c.decode(stored[0], c.ReservationReceipt)
        if receipt != actual:
            _reject("RESERVATION_REQUIRED")
        rows = self._conn.execute("SELECT r.* FROM reservations r JOIN legs l USING(leg_id) JOIN intents i ON i.id=l.intent_key WHERE i.payload_hash=?", (c.canonical_hash(record.intent),)).fetchall()
        if receipt.plan_id != record.plan.plan_id:
            _reject("RESERVATION_REQUIRED")
        if set(receipt.reservation_ids) != {x["reservation_id"] for x in rows} or not any(x["leg_id"] == record.leg.leg_id and x["state"] != "RELEASED" for x in rows):
            _reject("RESERVATION_REQUIRED")

    def begin_attempt(self, leg_id: str, context: c.ExecutionContext) -> AttemptResult:
        with self._tx():
            record = self._record(leg_id)
            if record.attempt_no:
                return AttemptResult(code=c.ResultCode.IDEMPOTENT, send_permitted=False, attempt_id=record.attempt_id,
                                     attempt_no=record.attempt_no, leg=record, revision=self._revision())
            self._timeout()
            if self._reconcile().required:
                _reject("RECONCILE_REQUIRED")
            if record.state is not c.OrderState.RESERVED:
                _reject("EXPIRED")
            latest = self._context(record.intent.binding)
            if latest is None or latest != context:
                _reject("CONTEXT_REQUIRED")
            if context.ledger_revision != self._revision() or context.snapshot.ledger_revision != self._revision():
                _reject("LEDGER_REVISION_MISMATCH", code=c.ResultCode.REVISION_CONFLICT)
            validation = c.validate_context(context, _now(self._clock), action=c.AuthorizationAction(record.intent.action.value), require_attempt=True)
            if context.intent != record.intent or context.reservation_view != self._view(record.intent.binding.account_key, context.snapshot.snapshot_id):
                _reject("RESERVATION_VIEW_REQUIRED")
            if not validation.ok:
                _reject(*validation.blockers)
            self._proof_gate(context)
            self._receipt(record, context)
            if record.plan.expires_at <= _now(self._clock):
                _reject("EXPIRED")
            if context.authorization.max_volume < record.leg.volume:
                _reject("VOLUME_INVALID")
            bounds = [x for x in context.authorization.price_bounds if (x.symbol.exchange, x.symbol.instrument_id) == (context.binding.exchange, context.binding.instrument_id)]
            if len(bounds) != 1 or not bounds[0].lower <= record.leg.price <= bounds[0].upper:
                _reject("PRICE_OUT_OF_BOUNDS")
            self._price(record.leg, context)
            deps = [self._record(x) for x in record.leg.depends_on]
            if deps:
                if any(x.state is not c.OrderState.FILLED or not x.settled or x.cancel_state in (c.CancelState.REQUESTED, c.CancelState.UNKNOWN) for x in deps):
                    _reject("RECONCILE_REQUIRED")
                side = _side(deps[0].leg)
                if any(x.side is side and x.Position != 0 for x in context.snapshot.positions):
                    _reject("RECONCILE_REQUIRED")
            active = tuple(x for x in record.plan.legs if self._row(x.leg_id)["state"] == "RESERVED")
            self._budget(record.intent, active, context, claimed=tuple(x.leg_id for x in active))
            view = self._view(record.intent.binding.account_key)
            if view.inflight_legs >= (context.policy.max_inflight_legs or 1):
                _reject("INFLIGHT_LIMIT")
            attempt_id = "a:" + _hash(leg_id)
            self._conn.execute("UPDATE legs SET state='SUBMITTING',attempt_no=1,attempt_id=?,attempted_at=? WHERE leg_id=?",
                               (attempt_id, _now(self._clock).isoformat(), leg_id))
            self._event(attempt_id, "ATTEMPT", c.encode(context), leg_id=leg_id)
            result = AttemptResult(code=c.ResultCode.OK, send_permitted=True, attempt_id=attempt_id, attempt_no=1,
                                   leg=self._record(leg_id), revision=self._bump())
        return result

    def _proof_gate(self, context: c.ExecutionContext):
        p, s, b = context.proof, context.snapshot, context.binding
        if context.mode is not c.ExecutionMode.SIMULATION or s.source is not c.Source.SIMULATION:
            _reject("NATIVE_DISABLED")
        if p is None or not p.verified or p.source is not c.Source.SIMULATION or not p.exclusive_writer_verified or \
                p.complete_through is None or (p.account_key, p.session_epoch, p.trading_day) != (b.account_key, b.session_epoch, b.trading_day) or \
                p.position_snapshot_id != s.snapshot_id or s.proof_id != p.proof_id or s.coverage.level is not c.CoverageLevel.FULL_ACCOUNT or \
                p.complete_through != s.coverage.complete_through:
            _reject("PROOF_REQUIRED")
        # Unowned active orders invalidate the exclusive account budget.
        for order in p.order_inventory:
            if order.active and not any(self._inventory_match(self._record(x[0]), order) for x in self._conn.execute("SELECT leg_id FROM legs WHERE owned=1")):
                _reject("BASELINE_GAP")

    def _price(self, leg: c.Leg, context: c.ExecutionContext):
        q, contract = context.snapshot.quote, context.snapshot.contract
        if q is None or contract is None or contract.price_tick is None:
            _reject("QUOTE_MISSING")
        if contract.asset_class is not c.AssetClass.FUTURE or contract.expiry is None or contract.expiry <= context.binding.trading_day:
            _reject("INSTRUMENT_UNSUPPORTED")
        if leg.price % contract.price_tick:
            _reject("TICK_MALIGNED")
        if q.market_status is not c.MarketStatus.CONTINUOUS:
            _reject("MARKET_STATUS_UNKNOWN")
        if None in (q.bid1, q.ask1, q.upper_limit, q.lower_limit, q.last) or not q.bid_volume1 or not q.ask_volume1:
            _reject("BOOK_EMPTY")
        if q.bid1 > q.ask1 or q.ask1 - q.bid1 > 10 * contract.price_tick:
            _reject("BOOK_CROSSED")
        if not q.lower_limit < q.last < q.upper_limit or not q.lower_limit <= leg.price <= q.upper_limit:
            _reject("PRICE_OUT_OF_BOUNDS")

    def record_submit_result(self, leg_id: str, result: object = None, *, error_code: str | None = None) -> SubmitResult:
        with self._tx():
            return self._record_submit_result_locked(leg_id, result, error_code)

    def _record_submit_result_locked(self, leg_id: str, result: object, error_code: str | None) -> SubmitResult:
        numeric = type(result) is int
        outcome = "local" if numeric and result >= 0 and result <= 2**63 - 1 and error_code is None else "rejected" if numeric and result == -1 and error_code is None else "unknown"
        payload = _json({"outcome": outcome, "order_id": result if outcome == "local" else None})
        row = self._row(leg_id)
        if row["attempt_no"] != 1:
            _reject("ATTEMPT_REQUIRED")
        if row["submit_result"] is not None:
            if row["submit_result"] == payload:
                return SubmitResult(code=c.ResultCode.IDEMPOTENT, leg=self._record(leg_id), revision=self._revision())
            self._latch(c.ReconcileReason.OWNERSHIP_CONFLICT, "submit-conflict")
            self._conn.execute("UPDATE legs SET unknown=1 WHERE leg_id=?", (leg_id,))
        else:
            self._conn.execute("UPDATE legs SET submit_result=? WHERE leg_id=?", (payload, leg_id))
            if outcome == "local":
                r = self._record(leg_id)
                collision = [x[0] for x in self._conn.execute("SELECT leg_id FROM legs WHERE order_id=? AND leg_id!=?", (result, leg_id))
                             if (lambda other: (other.intent.binding.account_key, other.intent.binding.session_epoch) == (r.intent.binding.account_key, r.intent.binding.session_epoch))(self._record(x[0]))]
                if collision or (row["order_id"] is not None and row["order_id"] != result):
                    self._latch(c.ReconcileReason.OWNERSHIP_CONFLICT, "local-alias")
                    self._conn.execute("UPDATE legs SET unknown=1 WHERE leg_id=?", (leg_id,))
                else:
                    self._conn.execute("UPDATE legs SET order_id=?,state=CASE WHEN state IN ('SUBMITTING','SUBMIT_UNKNOWN') THEN 'SUBMITTED_LOCAL' ELSE state END WHERE leg_id=?", (result, leg_id))
                    self._retry_pending()
            elif outcome == "rejected":
                # A synchronous callback (even still quarantined) contradicts local rejection.
                pending = [c.decode(x[0], c.ExecutionEvent) for x in self._conn.execute("SELECT payload FROM events WHERE kind='CALLBACK'")]
                binding = self._record(leg_id).intent.binding
                attempted_at = datetime.fromisoformat(row["attempted_at"])
                unsafe_evidence = any(x in self._reconcile().reasons for x in (c.ReconcileReason.EVENT_GAP, c.ReconcileReason.LEDGER_FAILURE, c.ReconcileReason.LEDGER_CORRUPT))
                contradiction = unsafe_evidence or row["confirmed"] or any(x.memo == leg_id or (
                    x.received_at >= attempted_at and x.account_key in (None, binding.account_key) and
                    x.callback_session_epoch == binding.session_epoch and x.exchange in (None, binding.exchange) and
                    x.instrument_id in (None, binding.instrument_id)) for x in pending)
                if contradiction:
                    self._conn.execute("UPDATE legs SET unknown=1 WHERE leg_id=?", (leg_id,))
                    self._latch(c.ReconcileReason.OWNERSHIP_CONFLICT, "local-reject-conflict")
                else:
                    self._conn.execute("UPDATE legs SET state='LOCAL_REJECTED',settled=1,unknown=0 WHERE leg_id=?", (leg_id,))
                    self._conn.execute("UPDATE reservations SET state='RELEASED',not_proven_reflected=0 WHERE leg_id=?", (leg_id,))
            else:
                self._conn.execute("UPDATE legs SET unknown=1,state=CASE WHEN state IN ('SUBMITTING','SUBMITTED_LOCAL') THEN 'SUBMIT_UNKNOWN' ELSE state END WHERE leg_id=?", (leg_id,))
        return SubmitResult(code=c.ResultCode.OK, leg=self._record(leg_id), revision=self._bump())

    def refresh_pending_open(self, leg_id: str, context: c.ExecutionContext) -> c.ReservationReceipt:
        with self._tx():
            record = self._record(leg_id)
            if record.leg.phase is not c.Phase.OPEN or not record.leg.depends_on:
                _reject("DEPENDENCY_REQUIRED")
            row, intent = self._intent_for_plan(record.plan)
            if context.intent != intent or context.binding != intent.binding:
                _reject("INTENT_REQUIRED")
            if self._context(intent.binding) != context:
                _reject("CONTEXT_REQUIRED")
            pending = tuple(x for x in record.plan.legs if x.phase is c.Phase.OPEN)
            if any(self._row(x.leg_id)["attempt_no"] for x in pending):
                _reject("ATTEMPT_EXISTS")
            if context.snapshot.snapshot_id == record.plan.snapshot_id and context.policy.policy_id == record.plan.policy_id:
                refresh = self._conn.execute("SELECT 1 FROM events WHERE event_id=?", ("refresh:" + record.plan.plan_id,)).fetchone()
                if refresh:
                    return c.decode(row["receipt"], c.ReservationReceipt)
            if self._reconcile().required or context.ledger_revision != self._revision():
                _reject("RECONCILE_REQUIRED")
            deps = [self._record(x) for x in record.leg.depends_on]
            if any(x.state is not c.OrderState.FILLED or not x.settled or x.trade_gap or x.cancel_state in (c.CancelState.REQUESTED, c.CancelState.UNKNOWN) for x in deps):
                _reject("RECONCILE_REQUIRED")
            self._proof_gate(context)
            opposite = next((x for x in context.snapshot.positions if x.side is _side(deps[0].leg)), None)
            if opposite is None or opposite.Position != 0:
                _reject("INSUFFICIENT_POSITION")
            if context.policy.cash_per_open_lot is None:
                _reject("CASH_BOUND_UNVERIFIED")
            refreshed = tuple(replace(x, cash_reserve=context.policy.cash_per_open_lot * x.volume) if x.phase is c.Phase.OPEN else x for x in record.plan.legs)
            new_opens = tuple(x for x in refreshed if x.phase is c.Phase.OPEN)
            self._budget(intent, new_opens, context, claimed=tuple(x.leg_id for x in pending))
            for leg in new_opens:
                self._price(leg, context)
            times, timing = context.snapshot.times, context.policy.timing
            components = ((times.quote.source_at, timing.quote_ttl), (times.account.sampled_at, timing.account_ttl),
                          (times.positions.sampled_at, timing.position_ttl), (times.contract.sampled_at, timing.contract_ttl),
                          (times.sampled_at, timing.packet_ttl))
            # The refreshed plan lives exactly as long as every required component stays valid.
            expiry = min([intent.expires_at, context.policy.valid_until] +
                         [stamp + timedelta(seconds=float(ttl)) for stamp, ttl in components if stamp is not None])
            new_plan = replace(record.plan, snapshot_id=context.snapshot.snapshot_id,
                               policy_id=context.policy.policy_id, ledger_revision=self._revision(), expires_at=expiry, legs=refreshed)
            plan_id = _plan_identity(new_plan)
            new_plan = replace(new_plan, plan_id=plan_id)
            for leg in new_opens:
                self._conn.execute("UPDATE legs SET plan_id=?,payload=? WHERE leg_id=?", (plan_id, c.encode(leg), leg.leg_id))
                self._conn.execute("UPDATE reservations SET state='HELD',cash_reserve=?,not_proven_reflected=volume,reflected_evidence_id=NULL,reflected_snapshot_id=NULL,baseline=? WHERE leg_id=?",
                                   (str(leg.cash_reserve), _baseline_for(leg, context.snapshot), leg.leg_id))
            receipt = c.ReservationReceipt(plan_id=plan_id, reservation_ids=tuple(x.leg_id for x in refreshed), revision=self._bump())
            self._conn.execute("UPDATE intents SET plan=?,receipt=? WHERE id=?", (c.encode(new_plan), c.encode(receipt), row["id"]))
            self._event("plan:" + plan_id, "PLAN", c.encode(new_plan))
            self._event("refresh:" + plan_id, "REFRESH", _json({"old_plan_id": record.plan.plan_id, "new_plan_id": plan_id}))
        return receipt

    def release_unattempted(self, plan_id: str) -> c.ReservationReceipt:
        with self._tx():
            row = self._conn.execute("SELECT i.* FROM intents i JOIN legs l ON i.id=l.intent_key WHERE l.plan_id=? LIMIT 1", (plan_id,)).fetchone()
            if row is None:
                _reject("PLAN_NOT_FOUND", code=c.ResultCode.NOT_FOUND)
            if self._conn.execute("SELECT 1 FROM legs WHERE plan_id=? AND attempt_no>0", (plan_id,)).fetchone():
                _reject("ATTEMPT_EXISTS")
            self._conn.execute("UPDATE legs SET state='EXPIRED',settled=1 WHERE plan_id=?", (plan_id,))
            self._conn.execute("UPDATE reservations SET state='RELEASED',not_proven_reflected=0 WHERE leg_id IN (SELECT leg_id FROM legs WHERE plan_id=?)", (plan_id,))
            old = c.decode(row["receipt"], c.ReservationReceipt)
            return replace(old, revision=self._bump())

    def note_event_gap(self, evidence_id: str) -> LedgerStatus:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", evidence_id):
            raise ValueError("opaque evidence id required")
        with self._tx():
            self._latch(c.ReconcileReason.EVENT_GAP, evidence_id)
            result = LedgerStatus(revision=self._bump(), reconcile=self._reconcile(), closed=False)
        return result

    # ---- NE1 v2: control CAS, dispatch tickets, native evidence (audit only) ----

    def _require_v2(self, blocker: str = "SCHEMA_UNSUPPORTED") -> None:
        try:
            row = self._conn.execute("SELECT value FROM ledger_meta WHERE key='schema'").fetchone()
        except sqlite3.Error:
            row = None
        if row is None or row[0] != "v2":
            _reject(blocker)

    def _install_first_offer_feature(self) -> None:
        """OFD-06 upgrade: atomically mark the feature and tombstone every
        preexisting action whose first issuance cannot be proven."""
        try:
            installed = self._conn.execute("SELECT value FROM ledger_meta WHERE key='ofd_feature'").fetchone()
        except sqlite3.Error:
            installed = None
        if installed is not None:
            return
        with self._tx():
            again = self._conn.execute("SELECT value FROM ledger_meta WHERE key='ofd_feature'").fetchone()
            if again is not None:
                return
            self._conn.execute("INSERT OR REPLACE INTO ledger_meta VALUES('ofd_feature','installed')")
            stamp = _now(self._clock).isoformat()
            for row in self._conn.execute("SELECT l.leg_id,l.plan_id,i.account_key,i.intent_id FROM legs l JOIN intents i ON i.id=l.intent_key").fetchall():
                for kind_value in ("SUBMIT", "CANCEL"):
                    action_key = "|".join((row["account_key"], row["intent_id"], row["leg_id"], kind_value))
                    self._conn.execute("INSERT OR IGNORE INTO offer_action_tombstones VALUES(?,?,?)",
                                       (action_key, "legacy-unproven", stamp))
            self._event("ofd-feature:installed", "OFD_FEATURE", _json({"tombstoned": "preexisting-legs"}))

    def _v2_enabled(self) -> bool:
        try:
            return (self._conn.execute("SELECT value FROM ledger_meta WHERE key='schema'").fetchone() or [None])[0] == "v2"
        except sqlite3.Error:
            return False

    def register_control_update(self, control, *, permit=None, evidence_registry=None) -> int:
        """Single control-change entry: monotonic control_revision per scope."""
        from . import native_contracts as nc
        from .native_contracts import ControlState, NativePilotPermit
        if type(control) is not ControlState:
            raise TypeError("ControlState required")
        if permit is not None and type(permit) is not NativePilotPermit:
            raise TypeError("NativePilotPermit required")
        with self._tx():
            self._require_v2("CONTROL_SCHEMA_UNSUPPORTED")
            scope = _scope_key(control.binding)
            row = self._conn.execute("SELECT control_revision,permit_id,send_budget,cancel_budget FROM control_state WHERE scope=?", (scope,)).fetchone()
            if row is not None and control.control_revision <= row[0]:
                _reject("CONTEXT_CONFLICT")
            permit_id = send_budget = cancel_budget = None
            if control.native_permit_id is not None:
                if permit is None or evidence_registry is None:
                    _reject("NATIVE_PERMIT_UNREGISTERED")
                if permit.permit_id != control.native_permit_id or permit.control_revision != control.control_revision:
                    _reject("CONTEXT_CONFLICT")
                if permit.binding != control.binding:
                    _reject("OWNERSHIP_CONFLICT")
                try:
                    evidence_registry.register_permit(permit)
                    active = evidence_registry.verify_permit_active(permit.permit_id, now=_now(self._clock))
                except Exception as exc:
                    raise LedgerError(c.ResultCode.BLOCKED, ("NATIVE_PERMIT_UNREGISTERED",)) from exc
                if not active.ok:
                    _reject("NATIVE_PERMIT_UNREGISTERED", *active.blockers)
                if row is not None and row[1] == permit.permit_id:
                    # Consumed attempts never re-arm by re-registering the same permit.
                    send_budget, cancel_budget = row[2], row[3]
                else:
                    send_budget, cancel_budget = permit.max_send_attempts, permit.max_cancel_attempts
                permit_id = permit.permit_id
            self._conn.execute("INSERT OR REPLACE INTO control_state VALUES(?,?,?,?,?,?,?)",
                               (scope, control.control_revision, nc.encode(control), permit_id, send_budget,
                                cancel_budget, _now(self._clock).isoformat()))
            self._event("control:" + scope + ":" + str(control.control_revision), "CONTROL", nc.encode(control))
            self._bump()
            return control.control_revision

    def control_revision(self, binding: c.Binding) -> int | None:
        with self._tx():
            try:
                row = self._conn.execute("SELECT control_revision FROM control_state WHERE scope=?",
                                         (_scope_key(binding),)).fetchone()
            except sqlite3.Error:
                return None
            return row[0] if row else None

    # ---- NE1 C1.4 durable first-offer deadlines (OFD-01..07) ----

    @staticmethod
    def _action_key(binding: c.Binding, intent_id: str, leg_id: str, kind_value: str) -> str:
        return "|".join((c.canonical_hash(binding), intent_id, leg_id, kind_value))

    def get_offer(self, offer_id: str):
        """OFD-02/03 read-only lookup: expired records return unchanged; None
        only means missing and authorizes nothing."""
        from . import native_contracts as nc
        if type(offer_id) is not str:
            raise TypeError("offer id required")
        with self._tx():
            self._require_v2()
            row = self._conn.execute("SELECT payload,payload_hash FROM offer_deadlines WHERE offer_id=?",
                                     (offer_id,)).fetchone()
            if row is None:
                return None
            payload = nc.encode_local(nc.decode_local(row[0], nc.OfferDeadlineRecord))
            if _hash(payload) != row[1]:
                raise LedgerError(c.ResultCode.PERSISTENCE_ERROR, ("LEDGER_CORRUPT",))
            return nc.decode_local(row[0], nc.OfferDeadlineRecord)

    def record_offer(self, offer, context: c.ExecutionContext, *, first_issued_at, kind,
                     expected_control_revision: int, expected_context: c.ExecutionContext,
                     cancel_id: str | None = None, evidence_registry=None):
        """OFD-02/03: one first-record transaction — atomic insert-or-read.

        No attempt, debit, context refresh or resource-revision side effect;
        duplicates perform no mutating commit; changed bytes are conflicts."""
        from . import native_contracts as nc
        if type(offer) is not nc.ExecutionOffer:
            raise TypeError("ExecutionOffer required")
        if type(context) is not c.ExecutionContext or type(expected_context) is not c.ExecutionContext:
            raise TypeError("ExecutionContext required")
        if type(first_issued_at) is not datetime:
            raise TypeError("aware datetime required for first_issued_at")
        if type(kind) is not nc.CommandKind:
            raise TypeError("CommandKind required")
        if type(expected_control_revision) is not int:
            raise TypeError("integer control revision required")
        record_value = nc.OfferDeadlineRecord(offer=offer, first_issued_at=first_issued_at,
                                              kind=kind, cancel_id=cancel_id)
        with self._tx():
            self._require_v2()
            lock_now = _now(self._clock)
            if first_issued_at > lock_now:
                _reject(nc.OFFER_DEADLINE_CLOCK_INVALID)  # H07 supplies no grace here.
            persisted = self._context(offer.binding)
            if persisted is None or c.canonical_hash(persisted) != c.canonical_hash(expected_context):
                _reject("CONTEXT_CONFLICT")
            if any(getattr(context, field) != getattr(expected_context, field)
                   for field in ("mode", "binding", "policy", "authorization", "route_ownership")):
                _reject("CONTEXT_CONFLICT")
            control = self._conn.execute("SELECT * FROM control_state WHERE scope=?",
                                         (_scope_key(offer.binding),)).fetchone()
            if control is None:
                _reject("CONTEXT_REQUIRED")
            if expected_control_revision != control["control_revision"] or offer.control_revision != control["control_revision"]:
                _reject("CONTEXT_CONFLICT", code=c.ResultCode.REVISION_CONFLICT)
            stored_control = nc.decode(control["payload"], nc.ControlState)
            if stored_control.disabled_reason is not None:
                _reject("MODE_DISABLED")
            leg = self._record(offer.leg_id)
            if leg.intent.intent_id != offer.intent_id or leg.plan.plan_id != offer.plan_id or offer.binding != leg.intent.binding:
                _reject("OWNERSHIP_CONFLICT")
            if offer.intent_hash != c.canonical_hash(leg.intent) or offer.leg_hash != c.canonical_hash(leg.leg):
                _reject("OWNERSHIP_CONFLICT")
            if kind is nc.CommandKind.CANCEL:
                if not leg.ownership_verified or leg.order_id is None or leg.state in c.TERMINAL_ORDER_STATES or leg.risk_filled >= leg.leg.volume:
                    _reject("CANCEL_NOT_AVAILABLE")
            elif leg.attempt_no:
                _reject("BLOCKED_DISPATCH")
            if not (first_issued_at < offer.expires_at and lock_now < offer.expires_at):
                _reject("EXPIRED")
            # Independently enforce every applicable original bound. CANCEL-D-01:
            # the original SUBMIT plan deadline is NOT a CANCEL bound — an owned
            # cancel stays recordable after its plan has expired.
            original = [leg.intent.expires_at,
                        context.authorization.expires_at if context.authorization is not None else None]
            if kind is nc.CommandKind.SUBMIT:
                original.append(leg.plan.expires_at)
                original.append(context.policy.valid_until)
                times, timing = context.snapshot.times, context.policy.timing
                original += [stamp + timedelta(seconds=float(ttl)) for stamp, ttl in
                             ((times.quote.source_at, timing.quote_ttl),
                              (times.account.sampled_at, timing.account_ttl),
                              (times.positions.sampled_at, timing.position_ttl),
                              (times.contract.sampled_at, timing.contract_ttl),
                              (times.sampled_at, timing.packet_ttl)) if stamp is not None]
            if control["permit_id"] is not None:
                if evidence_registry is None:
                    _reject("NATIVE_PERMIT_UNREGISTERED")
                try:
                    registry_bound = evidence_registry.applicable_deadline(
                        control["permit_id"],
                        action=c.AuthorizationAction(leg.intent.action.value) if kind is nc.CommandKind.SUBMIT else c.AuthorizationAction.CANCEL_OWNED,
                        now=lock_now)
                except Exception as exc:
                    raise LedgerError(c.ResultCode.BLOCKED, ("NATIVE_DEADLINE_UNAVAILABLE",)) from exc
                if not registry_bound.validation.ok:
                    _reject("NATIVE_DEADLINE_UNAVAILABLE", *registry_bound.validation.blockers)
                original.append(registry_bound.deadline)
            if any(offer.expires_at > bound for bound in original if bound is not None):
                _reject("EXPIRED")
            action_key = self._action_key(offer.binding, offer.intent_id, offer.leg_id, kind.value)
            # Tombstones cover both the canonical and the migration plain key form.
            plain_key = "|".join((leg.intent.binding.account_key, offer.intent_id, offer.leg_id, kind.value))
            tombstoned = any(self._conn.execute("SELECT 1 FROM offer_action_tombstones WHERE action_key=?",
                                                (key,)).fetchone() for key in (action_key, plain_key))
            if tombstoned:
                _reject(nc.OFFER_DEADLINE_UNPROVEN)  # legacy issuance cannot be minted
            clash = self._conn.execute("SELECT offer_id FROM offer_deadlines WHERE action_key=?", (action_key,)).fetchone()
            if clash is not None and clash[0] != offer.offer_id:
                _reject(nc.OFFER_DEADLINE_CONFLICT)  # an alternate ID cannot renew the action
            payload = nc.encode_local(record_value)
            existing = self._conn.execute("SELECT payload,payload_hash FROM offer_deadlines WHERE offer_id=?",
                                          (offer.offer_id,)).fetchone()
            if existing is not None:
                if existing[1] != _hash(payload):
                    _reject(nc.OFFER_DEADLINE_CONFLICT)
                return nc.decode_local(existing[0], nc.OfferDeadlineRecord)  # idempotent read, no commit
            self._conn.execute("INSERT INTO offer_deadlines VALUES(?,?,?,?,?,?,?,?,?)",
                               (offer.offer_id, action_key, kind.value, cancel_id, payload, _hash(payload),
                                first_issued_at.isoformat(), offer.expires_at.isoformat(),
                                _now(self._clock).isoformat()))
            self._event("offer:" + offer.offer_id, "OFFER", payload)
            return record_value


    def _pilot_gates(self, record: LegRecord, context: c.ExecutionContext) -> tuple[str, ...]:
        """Pilot submit gates: the permit substitutes readiness/proof; nothing else drops."""
        now = _now(self._clock)
        blockers = []
        if context.owner_thread_id != context.current_thread_id:
            blockers.append("THREAD_INVALID")
        if context.policy.valid_until <= now:
            blockers.append("POLICY_EXPIRED")
        if context.ledger_revision != self._revision() or context.snapshot.ledger_revision != context.ledger_revision:
            blockers.append("LEDGER_REVISION_MISMATCH")
        auth = context.authorization
        if auth is None:
            blockers.append("AUTHORIZATION_REQUIRED")
        else:
            if auth.expires_at <= now:
                blockers.append("AUTHORIZATION_EXPIRED")
            b = context.binding
            if auth.account_key != b.account_key:
                blockers.append("ACCOUNT_MISMATCH")
            if auth.session_epoch != b.session_epoch:
                blockers.append("SESSION_CHANGED")
            if auth.strategy_key != b.strategy_key:
                blockers.append("OWNERSHIP_CONFLICT")
            if (b.exchange, b.instrument_id) not in {(s.exchange, s.instrument_id) for s in auth.symbols}:
                blockers.append("INSTRUMENT_MISMATCH")
            if c.AuthorizationAction(record.intent.action.value) not in auth.actions:
                blockers.append("AUTHORIZATION_ACTION")
        blockers.extend(c.validate_snapshot_timing(context.snapshot, now, context.policy.timing).blockers)
        if context.intent != record.intent or \
                context.reservation_view != self._view(record.intent.binding.account_key, context.snapshot.snapshot_id):
            blockers.append("RESERVATION_VIEW_REQUIRED")
        return tuple(dict.fromkeys(blockers))

    def _dispatch_submit_locked(self, leg_id: str, context: c.ExecutionContext, *, pilot: bool) -> str:
        """Same gate sequence as begin_attempt inside the dispatch transaction."""
        record = self._record(leg_id)
        if record.attempt_no:
            _reject("BLOCKED_DISPATCH")
        self._timeout()
        if self._reconcile().required:
            _reject("RECONCILE_REQUIRED")
        if record.state is not c.OrderState.RESERVED:
            _reject("EXPIRED")
        latest = self._context(record.intent.binding)
        if latest is None or latest != context:
            _reject("CONTEXT_REQUIRED")
        if context.ledger_revision != self._revision() or context.snapshot.ledger_revision != self._revision():
            _reject("LEDGER_REVISION_MISMATCH", code=c.ResultCode.REVISION_CONFLICT)
        if pilot:
            # The registered pilot permit substitutes exactly readiness/proof gates.
            blockers = self._pilot_gates(record, context)
        else:
            validation = c.validate_context(context, _now(self._clock),
                                             action=c.AuthorizationAction(record.intent.action.value), require_attempt=True)
            if context.intent != record.intent or context.reservation_view != self._view(record.intent.binding.account_key, context.snapshot.snapshot_id):
                _reject("RESERVATION_VIEW_REQUIRED")
            blockers = validation.blockers
        if blockers:
            _reject(*blockers)
        if not pilot:
            self._proof_gate(context)
        self._receipt(record, context)
        if record.plan.expires_at <= _now(self._clock):
            _reject("EXPIRED")
        if context.authorization.max_volume < record.leg.volume:
            _reject("VOLUME_INVALID")
        bounds = [x for x in context.authorization.price_bounds if (x.symbol.exchange, x.symbol.instrument_id) == (context.binding.exchange, context.binding.instrument_id)]
        if len(bounds) != 1 or not bounds[0].lower <= record.leg.price <= bounds[0].upper:
            _reject("PRICE_OUT_OF_BOUNDS")
        self._price(record.leg, context)
        deps = [self._record(x) for x in record.leg.depends_on]
        if deps:
            if any(x.state is not c.OrderState.FILLED or not x.settled or x.trade_gap or x.cancel_state in (c.CancelState.REQUESTED, c.CancelState.UNKNOWN) for x in deps):
                _reject("RECONCILE_REQUIRED")
            side = _side(deps[0].leg)
            if any(x.side is side and x.Position != 0 for x in context.snapshot.positions):
                _reject("RECONCILE_REQUIRED")
        active = tuple(x for x in record.plan.legs if self._row(x.leg_id)["state"] == "RESERVED")
        self._budget(record.intent, active, context, claimed=tuple(x.leg_id for x in active))
        view = self._view(record.intent.binding.account_key)
        if view.inflight_legs >= (context.policy.max_inflight_legs or 1):
            _reject("INFLIGHT_LIMIT")
        attempt_id = "a:" + _hash(leg_id)
        self._conn.execute("UPDATE legs SET state='SUBMITTING',attempt_no=1,attempt_id=?,attempted_at=? WHERE leg_id=?",
                           (attempt_id, _now(self._clock).isoformat(), leg_id))
        self._event(attempt_id, "ATTEMPT", c.encode(context), leg_id=leg_id)
        return attempt_id

    def _dispatch_cancel_locked(self, leg_id: str, context: c.ExecutionContext, *, pilot: bool) -> tuple[str, int, str]:
        """Same gate sequence as request_cancel inside the dispatch transaction."""
        del pilot  # The pilot adds the permit budget on top; every cancel gate stays.
        record = self._record(leg_id)
        if self._fault_reason or any(x in self._reconcile().reasons for x in (c.ReconcileReason.LEDGER_FAILURE, c.ReconcileReason.LEDGER_CORRUPT)):
            _reject("LEDGER_FAILURE")
        if record.cancel_state is not c.CancelState.NONE:
            _reject("CANCEL_UNKNOWN")
        if context.binding != record.intent.binding:
            _reject("OWNERSHIP_CONFLICT")
        latest = self._context(record.intent.binding)
        if latest is None or any(getattr(latest, field) != getattr(context, field)
                                 for field in ("mode", "binding", "policy", "authorization", "route_ownership")):
            _reject("CONTEXT_REQUIRED")
        validation = c.validate_context(context, _now(self._clock), action=c.AuthorizationAction.CANCEL_OWNED)
        if not validation.ok:
            _reject(*validation.blockers)
        if context.ledger_revision != self._revision():
            _reject("LEDGER_REVISION_MISMATCH", code=c.ResultCode.REVISION_CONFLICT)
        if not record.ownership_verified or record.order_id is None or record.state in c.TERMINAL_ORDER_STATES or record.risk_filled >= record.leg.volume:
            _reject("CANCEL_NOT_AVAILABLE")
        aliases = [self._record(x[0]) for x in self._conn.execute("SELECT leg_id FROM legs WHERE order_id=?", (record.order_id,))]
        if sum((x.intent.binding.account_key, x.intent.binding.session_epoch) ==
               (record.intent.binding.account_key, record.intent.binding.session_epoch) for x in aliases) != 1:
            _reject("CANCEL_ROUTE_UNVERIFIED")
        quarantined = [c.decode(x[0], c.ExecutionEvent) for x in self._conn.execute("SELECT payload FROM events WHERE kind='CALLBACK' AND applied=0")]
        if any(x.order_id == record.order_id or (x.order_sys_id is not None and x.order_sys_id == record.order_sys_id) or
               x.memo == leg_id for x in quarantined):
            _reject("CANCEL_ROUTE_UNVERIFIED")
        cancel_id = "dc-" + leg_id
        attempt_id = "ca:" + _hash(cancel_id)
        self._conn.execute("UPDATE legs SET cancel_state='REQUESTED',cancel_id=? WHERE leg_id=?", (cancel_id, leg_id))
        self._event("cancel:" + cancel_id, "CANCEL_ATTEMPT", c.encode(context), leg_id=leg_id)
        return attempt_id, record.order_id, cancel_id

    def begin_dispatch(self, offer, context: c.ExecutionContext, *, expected_control_revision: int,
                       evidence_registry=None, expected_context=None):
        """One BEGIN IMMEDIATE (C1.2 DTTL-04): control CAS + context CAS + gates
        + attempt latch + permit debit + unique ticket. expected_context is the
        complete immutable context N4 read from the persisted scope; None keeps
        the strict already-registered path without any implicit refresh."""
        from . import native_contracts as nc
        if type(offer) is not nc.ExecutionOffer:
            raise TypeError("ExecutionOffer required")
        if type(context) is not c.ExecutionContext:
            raise TypeError("ExecutionContext required")
        if expected_context is not None and type(expected_context) is not c.ExecutionContext:
            raise TypeError("ExecutionContext required for expected_context")
        if type(expected_control_revision) is not int:
            raise TypeError("integer control revision required")
        with self._tx():
            self._require_v2()
            now = _now(self._clock)  # DTTL-03: sampled after the transaction lock is held.
            record = self._record(offer.leg_id)
            if record.intent.intent_id != offer.intent_id or record.plan.plan_id != offer.plan_id or offer.binding != record.intent.binding:
                _reject("OWNERSHIP_CONFLICT")
            if offer.intent_hash != c.canonical_hash(record.intent) or offer.leg_hash != c.canonical_hash(record.leg):
                _reject("OWNERSHIP_CONFLICT")
            # OFD-04: the durable first-offer record is authoritative and is
            # checked BEFORE any replay branch; ready cannot lazily create it.
            offer_row = self._conn.execute("SELECT payload,payload_hash FROM offer_deadlines WHERE offer_id=?",
                                           (offer.offer_id,)).fetchone()
            if offer_row is None:
                _reject(nc.OFFER_DEADLINE_REQUIRED)
            first_offer = nc.decode_local(offer_row[0], nc.OfferDeadlineRecord)
            if nc.encode_local(first_offer) != offer_row[0] or _hash(offer_row[0]) != offer_row[1]:
                raise LedgerError(c.ResultCode.PERSISTENCE_ERROR, ("LEDGER_CORRUPT",))
            if c.canonical_hash(first_offer.offer) != c.canonical_hash(offer):
                _reject(nc.OFFER_DEADLINE_CONFLICT)
            existing = self._conn.execute("SELECT payload FROM dispatch_commands WHERE offer_id=? OR (leg_id=? AND closed=0)",
                                          (offer.offer_id, offer.leg_id)).fetchone()
            if existing is not None:
                # Replay keeps the original timestamps and never renews capability;
                # a replay whose window has closed is suppressed the same way.
                replayed = nc.decode(existing[0], nc.DispatchCommand)
                if replayed.expires_at <= now:
                    _reject("DISPATCH_EXPIRED")
                return replayed
            control = self._conn.execute("SELECT * FROM control_state WHERE scope=?", (_scope_key(context.binding),)).fetchone()
            if control is None:
                _reject("CONTEXT_REQUIRED")
            if expected_control_revision != control["control_revision"] or offer.control_revision != control["control_revision"]:
                _reject("CONTEXT_CONFLICT", code=c.ResultCode.REVISION_CONFLICT)
            stored_control = nc.decode(control["payload"], nc.ControlState)
            if stored_control.disabled_reason is not None:
                _reject("MODE_DISABLED")
            if expected_context is not None:
                persisted = self._context(context.binding)
                if persisted is None:
                    _reject("CONTEXT_REQUIRED")
                if c.canonical_hash(persisted) != c.canonical_hash(expected_context):
                    _reject("CONTEXT_CONFLICT")
                if any(getattr(context, field) != getattr(expected_context, field)
                       for field in ("mode", "binding", "policy", "authorization", "route_ownership")):
                    # Observation refresh never copies control authority.
                    _reject("CONTEXT_CONFLICT")
                # Derive the view FIRST and rebuild the context in ONE replace so
                # reservation_view.ledger_revision/snapshot stay aligned with the
                # refreshed revision even after legal interleaved writes bumped
                # the journal between prepare and ready (record_http_request/
                # events/results/control all bump without touching the context).
                fresh_view = self._view(context.binding.account_key, context.snapshot.snapshot_id)
                refreshed = replace(context, ledger_revision=self._revision(),
                                    snapshot=replace(context.snapshot, ledger_revision=self._revision()),
                                    reservation_view=fresh_view)
                self._store_context(refreshed, ())
                context = refreshed
            permit_id, pilot = control["permit_id"], control["permit_id"] is not None
            if pilot:
                if evidence_registry is None:
                    _reject("NATIVE_PERMIT_UNREGISTERED")
                try:
                    active = evidence_registry.verify_permit_active(permit_id, now=now)
                except Exception as exc:
                    raise LedgerError(c.ResultCode.BLOCKED, ("NATIVE_PERMIT_UNREGISTERED",)) from exc
                if not active.ok:
                    _reject("NATIVE_PERMIT_UNREGISTERED", *active.blockers)
            if record.attempt_no == 0:
                kind = nc.CommandKind.SUBMIT
            elif record.attempt_no >= 1 and record.cancel_state is c.CancelState.NONE:
                kind = nc.CommandKind.CANCEL
            else:
                _reject("BLOCKED_DISPATCH")
            if first_offer.kind is not kind:
                _reject(nc.OFFER_DEADLINE_CONFLICT)
            if kind is nc.CommandKind.CANCEL:
                if first_offer.cancel_id != "dc-" + offer.leg_id:
                    _reject(nc.OFFER_DEADLINE_CONFLICT)
            column = "send_budget" if kind is nc.CommandKind.SUBMIT else "cancel_budget"
            if pilot and (control[column] is None or control[column] < 1):
                _reject("PERMIT_EXHAUSTED")
            if kind is nc.CommandKind.SUBMIT:
                attempt_id = self._dispatch_submit_locked(offer.leg_id, context, pilot=pilot)
                order_id, cancel_id, route_evidence = None, None, None
                times, timing = context.snapshot.times, context.policy.timing
                component_deadlines = [stamp + timedelta(seconds=float(ttl)) for stamp, ttl in
                                       ((times.quote.source_at, timing.quote_ttl),
                                        (times.account.sampled_at, timing.account_ttl),
                                        (times.positions.sampled_at, timing.position_ttl),
                                        (times.contract.sampled_at, timing.contract_ttl),
                                        (times.sampled_at, timing.packet_ttl)) if stamp is not None]
                current_deadlines = [record.intent.expires_at, context.policy.valid_until] + component_deadlines
            else:
                attempt_id, order_id, cancel_id = self._dispatch_cancel_locked(offer.leg_id, context, pilot=pilot)
                route_evidence = (context.route_ownership.evidence_id if context.route_ownership is not None else None) or (permit_id and ("permit:" + permit_id))
                # H24: owned cancel is judged on its own route; SUBMIT-only data
                # freshness deadlines are not imported into CANCEL. Policy expiry
                # stays out pending the registered C1.2 ambiguity ruling.
                current_deadlines = [record.intent.expires_at]
            if context.authorization is not None:
                # DTTL-03: an earlier applicable current authorization deadline wins
                # over the full ticket budget on both routes (auth expiry is a gate
                # on the cancel route as well).
                current_deadlines.append(context.authorization.expires_at)
            if pilot:
                # C1.3 RD-04: the permit/evidence applicability bound is consumed
                # inside this transaction at t_issue through the public registry
                # interface; an unavailable or failed answer commits nothing.
                registry_action = c.AuthorizationAction(record.intent.action.value) \
                    if kind is nc.CommandKind.SUBMIT else c.AuthorizationAction.CANCEL_OWNED
                try:
                    registry_deadline = evidence_registry.applicable_deadline(
                        permit_id, action=registry_action, now=now)
                except Exception as exc:
                    raise LedgerError(c.ResultCode.BLOCKED, ("NATIVE_DEADLINE_UNAVAILABLE",)) from exc
                if not registry_deadline.validation.ok:
                    _reject("NATIVE_DEADLINE_UNAVAILABLE", *registry_deadline.validation.blockers)
                if registry_deadline.deadline is not None:
                    current_deadlines.append(registry_deadline.deadline)
            if pilot:
                self._conn.execute(f"UPDATE control_state SET {column}={column}-1 WHERE scope=?", (_scope_key(context.binding),))
            contract = context.snapshot.contract
            # DTTL-03/OFD-04: inherit the durable first-offer deadline; the
            # caller-supplied bytes were verified identical above.
            command_expires = nc.compute_command_deadline(first_offer.offer.expires_at, now, current_deadlines)
            if command_expires <= now:
                _reject("EXPIRED")
            command = nc.DispatchCommand(
                command_id="dc-" + attempt_id, kind=kind, attempt_id=attempt_id, offer_id=offer.offer_id,
                intent_id=record.intent.intent_id, leg_id=offer.leg_id, plan_id=record.plan.plan_id,
                binding=record.intent.binding, leg=record.leg, order_type=record.intent.order_type,
                price_tick=contract.price_tick,
                contract_limits=nc.ContractLimit(min_volume=contract.min_limit_order_volume,
                                                 max_volume=contract.max_limit_order_volume, volume_step=1),
                control_revision=control["control_revision"],
                permit_id=permit_id or "sim-scope", params_sha256=offer.params_sha256,
                issued_at=now, expires_at=command_expires,
                order_id=order_id, route_evidence_id=route_evidence, cancel_id=cancel_id)
            payload = nc.encode(command)
            self._conn.execute("INSERT INTO dispatch_commands VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (command.command_id, attempt_id, kind.value, offer.offer_id, command.intent_id, offer.leg_id,
                                record.plan.plan_id, c.canonical_hash(command.binding), control["control_revision"],
                                permit_id, payload, _hash(payload), now.isoformat(), command.expires_at.isoformat(), 0, 0))
            self._bump()
        # DTTL-05: publish only while the effective deadline is still valid; the
        # committed facts survive even when delivery is suppressed here.
        if command.expires_at <= _now(self._clock):
            raise LedgerError(c.ResultCode.BLOCKED, ("DISPATCH_EXPIRED",))
        return command

    def record_dispatch_receipt(self, receipt) -> SubmitResult:
        from . import native_contracts as nc
        if type(receipt) is not nc.DispatchReceipt:
            raise TypeError("DispatchReceipt required")
        with self._tx():
            self._require_v2()
            row = self._conn.execute("SELECT * FROM dispatch_commands WHERE command_id=?", (receipt.command_id,)).fetchone()
            if row is None:
                _reject("DISPATCH_NOT_FOUND", code=c.ResultCode.NOT_FOUND)
            if row["attempt_id"] != receipt.attempt_id:
                _reject("OWNERSHIP_CONFLICT")
            payload = nc.encode(receipt)
            prior = self._conn.execute("SELECT payload_hash FROM dispatch_receipts WHERE receipt_id=?", (receipt.receipt_id,)).fetchone()
            if prior is not None:
                if prior[0] != _hash(payload):
                    if not hasattr(self, "_pending_latches"):
                        self._pending_latches = []
                    self._pending_latches.append((c.ReconcileReason.OWNERSHIP_CONFLICT, "receipt-conflict", row["leg_id"]))
                    _reject("OWNERSHIP_CONFLICT")
                return SubmitResult(code=c.ResultCode.IDEMPOTENT, leg=self._record(row["leg_id"]), revision=self._revision())
            self._conn.execute("INSERT INTO dispatch_receipts VALUES(?,?,?,?,?,?)",
                               (receipt.receipt_id, receipt.command_id, receipt.stage.value, payload,
                                _hash(payload), _now(self._clock).isoformat()))
            if receipt.stage is nc.ReceiptStage.CONSUMED:
                self._conn.execute("UPDATE dispatch_commands SET consumed=1 WHERE command_id=?", (receipt.command_id,))
                return SubmitResult(code=c.ResultCode.OK, leg=self._record(row["leg_id"]), revision=self._bump())
            self._conn.execute("UPDATE dispatch_commands SET closed=1 WHERE command_id=?", (receipt.command_id,))
            command = nc.decode(row["payload"], nc.DispatchCommand)
            if command.kind is nc.CommandKind.SUBMIT:
                if receipt.stage is nc.ReceiptStage.NO_CALL:
                    return self._record_submit_result_locked(command.leg_id, None, "NO_CALL")
                if receipt.result_kind is nc.ResultKind.LOCAL_ACK:
                    return self._record_submit_result_locked(command.leg_id, receipt.result_value, None)
                if receipt.result_kind is nc.ResultKind.LOCAL_REJECTED:
                    return self._record_submit_result_locked(command.leg_id, -1, None)
                return self._record_submit_result_locked(command.leg_id, None, receipt.error_code or receipt.result_kind.value)
            if receipt.stage is nc.ReceiptStage.NO_CALL:
                return self._record_cancel_result_locked(command.cancel_id, None, "NO_CALL")
            if receipt.result_kind is nc.ResultKind.LOCAL_ACK:
                return self._record_cancel_result_locked(command.cancel_id, receipt.result_value, None)
            if receipt.result_kind is nc.ResultKind.LOCAL_REJECTED:
                return self._record_cancel_result_locked(command.cancel_id, -1, None)
            return self._record_cancel_result_locked(command.cancel_id, None, receipt.error_code or receipt.result_kind.value)

    def pending_dispatch_commands(self, binding: c.Binding) -> tuple:
        from . import native_contracts as nc
        if type(binding) is not c.Binding:
            raise TypeError("Binding required")
        with self._tx():
            self._require_v2()
            rows = self._conn.execute("SELECT payload FROM dispatch_commands WHERE binding_hash=? AND closed=0 ORDER BY rowid",
                                      (c.canonical_hash(binding),)).fetchall()
            return tuple(nc.decode(row[0], nc.DispatchCommand) for row in rows)

    def ack_outbox(self, binding: c.Binding, event_id: str) -> bool:
        with self._tx():
            self._require_v2()
            event = self._conn.execute("SELECT recorded_at FROM events WHERE event_id=?", (event_id,)).fetchone()
            if event is None:
                _reject("EVENT_NOT_FOUND", code=c.ResultCode.NOT_FOUND)
            key = c.canonical_hash(binding)
            row = self._conn.execute("SELECT acked_at FROM outbox_ack WHERE binding_hash=?", (key,)).fetchone()
            if row is not None and event[0] <= row[0]:
                return False
            self._conn.execute("INSERT OR REPLACE INTO outbox_ack VALUES(?,?,?)", (key, event_id, event[0]))
            self._bump()
            return True

    def register_native_route(self, evidence, *, evidence_registry) -> None:
        from .native_evidence import NativeRouteEvidence, validate_route
        if type(evidence) is not NativeRouteEvidence:
            raise TypeError("NativeRouteEvidence required")
        with self._tx():
            self._require_v2()
            if not validate_route(evidence, now=_now(self._clock)).ok:
                _reject("ROUTE_EVIDENCE_EXPIRED")
            if evidence_registry is None:
                _reject("NATIVE_ROUTE_UNREGISTERED")
            try:
                evidence_registry.register_route(evidence)
            except Exception as exc:
                raise LedgerError(c.ResultCode.BLOCKED, ("NATIVE_ROUTE_UNREGISTERED",)) from exc
            self._store_immutable("nroute:" + evidence.evidence_id,
                                  c.encode(evidence, schema_version=c.V2_SCHEMA_VERSION))

    def register_settlement_evidence(self, evidence, *, evidence_registry) -> None:
        from .native_evidence import NativeSettlementEvidence
        if type(evidence) is not NativeSettlementEvidence:
            raise TypeError("NativeSettlementEvidence required")
        with self._tx():
            self._require_v2()
            if evidence_registry is None:
                _reject("NATIVE_SETTLEMENT_UNREGISTERED")
            try:
                evidence_registry.register_settlement(evidence)
            except Exception as exc:
                raise LedgerError(c.ResultCode.BLOCKED, ("NATIVE_SETTLEMENT_UNREGISTERED",)) from exc
            self._store_immutable("nsettle:" + evidence.evidence_id,
                                  c.encode(evidence, schema_version=c.V2_SCHEMA_VERSION))

    def _store_immutable(self, identity: str, payload: str) -> None:
        prior = self._conn.execute("SELECT payload FROM events WHERE event_id=?", (identity,)).fetchone()
        if prior is not None:
            if prior[0] != payload:
                self._latch(c.ReconcileReason.OWNERSHIP_CONFLICT, identity)
                _reject("OWNERSHIP_CONFLICT")
            return
        self._event(identity, "NATIVE_EVIDENCE", payload)

    def assert_replaceable(self, leg_id: str) -> LegRecord:
        with self._tx():
            record = self._record(leg_id)
            if record.state not in c.TERMINAL_ORDER_STATES or not record.settled or record.trade_gap or \
                    record.cancel_state not in (c.CancelState.NONE, c.CancelState.CONFIRMED):
                _reject("RECONCILE_REQUIRED")
            return record

    def absorb_settlement(self, evidence, *, evidence_registry=None) -> RecoveryResult:
        """Scoped native settlement: releases exactly the covered legs, never FULL_ACCOUNT."""
        from .native_evidence import NativeSettlementEvidence, validate_settlement
        if type(evidence) is not NativeSettlementEvidence:
            raise TypeError("NativeSettlementEvidence required")
        with self._tx():
            self._require_v2()
            if not validate_settlement(evidence, now=_now(self._clock)).ok:
                _reject("SETTLEMENT_FUTURE")
            if evidence_registry is not None:
                try:
                    evidence_registry.register_settlement(evidence)
                except Exception as exc:
                    raise LedgerError(c.ResultCode.BLOCKED, ("NATIVE_SETTLEMENT_UNREGISTERED",)) from exc
                self._store_immutable("nsettle:" + evidence.evidence_id,
                                      c.encode(evidence, schema_version=c.V2_SCHEMA_VERSION))
            else:
                row = self._conn.execute("SELECT payload FROM events WHERE event_id=?",
                                         ("nsettle:" + evidence.evidence_id,)).fetchone()
                if row is None or row[0] != c.encode(evidence, schema_version=c.V2_SCHEMA_VERSION):
                    _reject("NATIVE_SETTLEMENT_UNREGISTERED")
            if any(x in self._reconcile().reasons for x in (c.ReconcileReason.LEDGER_FAILURE, c.ReconcileReason.LEDGER_CORRUPT,
                                                            c.ReconcileReason.EVENT_GAP, c.ReconcileReason.QUANTITY_CONFLICT)):
                _reject("RECONCILE_REQUIRED")
            snapshot_row = self._conn.execute("SELECT payload FROM events WHERE event_id=?",
                                              ("snapshot:" + evidence.position_snapshot_id,)).fetchone()
            if snapshot_row is None:
                _reject("SNAPSHOT_STALE")
            snapshot = c.decode(snapshot_row[0], c.ExecutionSnapshot)
            if evidence.binding != snapshot.binding:
                _reject("OWNERSHIP_CONFLICT")
            for leg_id in evidence.covered_leg_ids:
                record = self._record(leg_id)
                if record.intent.binding.account_key != snapshot.binding.account_key:
                    _reject("OWNERSHIP_CONFLICT")
                if not record.ownership_verified:
                    continue
                matches = [x for x in evidence.order_inventory if self._inventory_match(record, x)]
                if len(matches) != 1:
                    continue
                order = matches[0]
                if order.total_volume != record.leg.volume or order.traded_volume != record.risk_filled:
                    self._latch(c.ReconcileReason.QUANTITY_CONFLICT, evidence.evidence_id)
                    continue
                trades = [x for x in evidence.trade_inventory if self._inventory_match(record, x)]
                stored = self._conn.execute("SELECT trade_id,volume,price FROM trades WHERE leg_id=?", (leg_id,)).fetchall()
                if {x.trade_id for x in trades} != {x[0] for x in stored} or any(
                        not any(x.trade_id == item[0] and x.volume == item[1] and x.price == Decimal(item[2]) for x in trades)
                        for item in stored):
                    continue
                applied = {x[0] for x in self._conn.execute(
                    "SELECT event_id FROM events WHERE kind='CALLBACK' AND leg_id=? AND applied=1", (leg_id,))}
                if not applied <= set(evidence.covered_event_ids):
                    continue
                reservation = self._conn.execute("SELECT baseline,cash_reserve FROM reservations WHERE leg_id=? AND state!='RELEASED'",
                                                 (leg_id,)).fetchone()
                baseline = json.loads(reservation["baseline"]) if reservation is not None and reservation["baseline"] else None
                pos, absorbed, released = _absorption_verdict(record, snapshot, baseline,
                                                              Decimal(reservation["cash_reserve"]) if reservation else Decimal(0))
                if record.state in c.TERMINAL_ORDER_STATES:
                    if order.active or record.cancel_volume is None or record.cancel_volume != order.cancel_volume or \
                            record.risk_filled + order.cancel_volume != record.leg.volume:
                        continue
                    if record.leg.phase is c.Phase.CLOSE:
                        if pos is None or pos.FrozenClosing is None or pos.YdFrozenClosing is None:
                            continue
                        frozen = pos.FrozenClosing - pos.YdFrozenClosing if record.leg.position_bucket is c.Bucket.TD else \
                            pos.YdFrozenClosing if record.leg.position_bucket is c.Bucket.YD else pos.FrozenClosing
                        if frozen:
                            continue
                    if not released:
                        continue
                    self._conn.execute("UPDATE reservations SET state='RELEASED',not_proven_reflected=0,reflected_evidence_id=?,reflected_snapshot_id=? WHERE leg_id=?",
                                       (evidence.evidence_id, snapshot.snapshot_id, leg_id))
                    self._conn.execute("UPDATE legs SET settled=1,unknown=0,cancel_state=CASE WHEN cancel_state IN ('REQUESTED','UNKNOWN') THEN 'CONFIRMED' ELSE cancel_state END WHERE leg_id=?", (leg_id,))
                elif order.active and record.cancel_state is c.CancelState.NONE and not record.reconcile_required and absorbed:
                    self._conn.execute("UPDATE reservations SET state='REFLECTED',not_proven_reflected=0,reflected_evidence_id=?,reflected_snapshot_id=? WHERE leg_id=?",
                                       (evidence.evidence_id, snapshot.snapshot_id, leg_id))
            unresolved = tuple(x[0] for x in self._conn.execute(
                "SELECT leg_id FROM legs WHERE attempt_no>0 AND settled=0 ORDER BY leg_id"))
            if not unresolved:
                self._conn.execute("UPDATE events SET applied=0 WHERE event_id='latch:RESTART_UNRESOLVED'")
            return RecoveryResult(code=c.ResultCode.BLOCKED if self._reconcile().required else c.ResultCode.OK,
                                  reconcile=self._reconcile(), revision=self._bump(),
                                  unresolved_leg_ids=unresolved,
                                  unresolved_command_ids=self._open_command_ids())
    def _open_command_ids(self) -> tuple[str, ...]:
        if not self._v2_enabled():
            return ()
        return tuple(x[0] for x in self._conn.execute(
            "SELECT command_id FROM dispatch_commands WHERE closed=0 ORDER BY rowid"))

    # ---- NE1 C1.5 one-transaction native SUBMIT preparation (AP-01..04) ----

    def prepare_native_submission(self, intent: c.OrderIntent, context: c.ExecutionContext, *,
                                  expected_control_revision: int, expected_context: c.ExecutionContext,
                                  resources: tuple = (), evidence_registry=None) -> tuple:
        """AP-01: exactly one BEGIN IMMEDIATE covering intent registration,
        context CAS, observation adoption, plan computation, reservation and the
        final context persistence. AP-02: any failure rolls the whole unit back;
        AP-04: record_offer/begin_dispatch lifetimes stay independent."""
        from . import native_contracts as nc
        from . import planning
        if type(intent) is not c.OrderIntent:
            raise TypeError("OrderIntent required")
        if type(context) is not c.ExecutionContext or type(expected_context) is not c.ExecutionContext:
            raise TypeError("ExecutionContext required")
        if type(resources) is not tuple:
            raise TypeError("tuple resources required")
        if type(expected_control_revision) is not int:
            raise TypeError("integer control revision required")
        if intent.action not in (c.Action.OPEN_LONG, c.Action.OPEN_SHORT,
                                 c.Action.REVERSE_TO_LONG, c.Action.REVERSE_TO_SHORT):
            # Owned CANCEL/closing preparation does not pass through this seam.
            _reject("UNSUPPORTED_CLOSE_POLICY")
        with self._tx():
            self._require_v2()
            now = _now(self._clock)  # sampled after the transaction lock is held
            # (1) Complete expected-context comparison; a missing scope is not
            # initialization permission and incoming control is never adopted.
            persisted = self._context(context.binding)
            if persisted is None or c.canonical_hash(persisted) != c.canonical_hash(expected_context):
                _reject("CONTEXT_CONFLICT")
            if any(getattr(context, field) != getattr(expected_context, field)
                   for field in ("mode", "binding", "policy", "authorization", "route_ownership")):
                _reject("CONTEXT_CONFLICT")
            # (2) Persistent control identity/revision and permit authority.
            control = self._conn.execute("SELECT * FROM control_state WHERE scope=?",
                                         (_scope_key(context.binding),)).fetchone()
            if control is None:
                _reject("CONTEXT_REQUIRED")
            if expected_control_revision != control["control_revision"]:
                _reject("CONTEXT_CONFLICT", code=c.ResultCode.REVISION_CONFLICT)
            if nc.decode(control["payload"], nc.ControlState).disabled_reason is not None:
                _reject("MODE_DISABLED")
            if control["permit_id"] is not None:
                if evidence_registry is None:
                    _reject("NATIVE_PERMIT_UNREGISTERED")
                try:
                    active = evidence_registry.verify_permit_active(control["permit_id"], now=now)
                except Exception as exc:
                    raise LedgerError(c.ResultCode.BLOCKED, ("NATIVE_PERMIT_UNREGISTERED",)) from exc
                if not active.ok:
                    _reject("NATIVE_PERMIT_UNREGISTERED", *active.blockers)
            # (3) Register-or-validate the intent with source uniqueness.
            b = intent.binding
            digest = c.canonical_hash(intent)
            row = self._conn.execute(
                "SELECT * FROM intents WHERE account_key=? AND strategy_key=? AND (intent_id=? OR source_key=?)",
                (b.account_key, b.strategy_key, intent.intent_id, intent.source_key)).fetchone()
            already_reserved = False
            if row is not None:
                if row["payload_hash"] != digest:
                    _reject("INTENT_CONFLICT", code=c.ResultCode.INTENT_CONFLICT)
                already_reserved = row["plan"] is not None
                if already_reserved:
                    # Idempotent re-preparation: the stored plan stays authoritative.
                    return c.decode(row["plan"], c.Plan), persisted
            else:
                key = _hash("|".join((b.account_key, b.strategy_key, intent.intent_id)))
                self._conn.execute(
                    "INSERT INTO intents(id,account_key,strategy_key,intent_id,source_key,payload,payload_hash) VALUES(?,?,?,?,?,?,?)",
                    (key, b.account_key, b.strategy_key, intent.intent_id, intent.source_key,
                     c.encode(intent), digest))
                row = self._conn.execute("SELECT * FROM intents WHERE id=?", (key,)).fetchone()
            # (4/5) Adopt the observation/resources INSIDE this transaction (the
            # budget parity check below consumes this adoption, never requiring a
            # pre-registered persisted snapshot), then derive the current view.
            revision = self._revision()
            candidate = replace(context, ledger_revision=revision,
                                snapshot=replace(context.snapshot, ledger_revision=revision),
                                reservation_view=None)
            self._adopt_snapshots(candidate, resources)
            # Validation parity with _store_context happens at write time below;
            # derive the pre-reservation view the planner consumes.
            view = self._view(b.account_key, candidate.snapshot.snapshot_id)
            planned = replace(candidate, reservation_view=view)
            # (6) Compute the plan with the existing pure planning rules.
            plan = planning.plan(intent, planned.snapshot, view, planned.policy, now)
            if plan.blockers:
                _reject(*plan.blockers)
            # (7) Reserve: same checks as Ledger.reserve, one unit with (3).
            if self._reconcile().required:
                _reject("RECONCILE_REQUIRED")
            if plan.ledger_revision != revision:
                _reject("LEDGER_REVISION_MISMATCH", code=c.ResultCode.REVISION_CONFLICT)
            if plan.snapshot_id != planned.snapshot.snapshot_id or plan.policy_id != planned.policy.policy_id:
                _reject("SNAPSHOT_STALE")
            if planned.ledger_revision != revision or planned.snapshot.ledger_revision != revision:
                _reject("LEDGER_REVISION_MISMATCH", code=c.ResultCode.REVISION_CONFLICT)
            if plan.expires_at <= now:
                _reject("EXPIRED")
            self._validate_shape(intent, plan.legs)
            self._budget(intent, plan.legs, planned)
            ids = []
            for leg in plan.legs:
                if self._conn.execute("SELECT 1 FROM legs WHERE leg_id=?", (leg.leg_id,)).fetchone():
                    _reject("LEG_CONFLICT")
                self._conn.execute("INSERT INTO legs(leg_id,intent_key,plan_id,payload,state) VALUES(?,?,?,?,?)",
                                   (leg.leg_id, row["id"], plan.plan_id, c.encode(leg), c.OrderState.RESERVED.value))
                key = c.ReservationKey(account_key=b.account_key, exchange=b.exchange,
                                       instrument_id=b.instrument_id, hedge_flag=b.hedge_flag,
                                       side=_side(leg), bucket=leg.position_bucket or c.Bucket.ALL)
                self._conn.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?,?)",
                                   (leg.leg_id, leg.leg_id, c.encode(key), c.ReservationState.HELD.value,
                                    leg.volume, str(leg.cash_reserve), leg.volume, None, None,
                                    _baseline_for(leg, planned.snapshot)))
                ids.append(leg.leg_id)
            receipt = c.ReservationReceipt(plan_id=plan.plan_id, reservation_ids=tuple(ids),
                                           revision=self._bump())
            self._conn.execute("UPDATE intents SET plan=?,receipt=? WHERE id=?",
                               (c.encode(plan), c.encode(receipt), row["id"]))
            self._event("plan:" + plan.plan_id, "PLAN", c.encode(plan))
            # (8) Persist ONE final context with its receipt and post-reserve view;
            # resources are validated here, adding no extra refresh commit.
            final_view = self._view(b.account_key, planned.snapshot.snapshot_id)
            final = replace(planned, ledger_revision=receipt.revision,
                            snapshot=replace(planned.snapshot, ledger_revision=receipt.revision),
                            reservation_receipt=receipt, reservation_view=final_view)
            self._store_context(final, resources)
            return plan, final

    # ---- NE1 C1.1 RULING 04/05/06/07/09: durable registrations, epochs, replay,
    # capture identity, contiguous event cursors and counter facts ----

    def _extra(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM ledger_extra WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _set_extra(self, key: str, value: str) -> None:
        self._conn.execute("INSERT OR REPLACE INTO ledger_extra VALUES(?,?)", (key, value))

    def begin_maintenance(self) -> str:
        """Exclusive journal maintenance; a live holder makes registration fail."""
        with self._tx():
            self._require_v2("CONTROL_SCHEMA_UNSUPPORTED")
            if self._extra("maintenance_token") is not None:
                _reject("MAINTENANCE_HELD")
            token = secrets.token_hex(16)
            self._set_extra("maintenance_token", token)
            return token

    def end_maintenance(self, token: str) -> None:
        with self._tx():
            self._require_v2("CONTROL_SCHEMA_UNSUPPORTED")
            held = self._extra("maintenance_token")
            if held != token:
                _reject("MAINTENANCE_TOKEN_MISMATCH")
            self._conn.execute("DELETE FROM ledger_extra WHERE key='maintenance_token'")

    @contextmanager
    def maintenance(self):
        token = self.begin_maintenance()
        try:
            yield token
        finally:
            try:
                self.end_maintenance(token)
            except LedgerError:
                pass

    def _require_maintenance(self) -> None:
        if self._extra("maintenance_token") is None:
            _reject("MAINTENANCE_REQUIRED")

    def register_actor(self, actor_id: str, account_key: str, bindings: tuple[c.Binding, ...], *,
                       operator_action_id: str, manifest_sha256: str, contract_lock_sha256: str,
                       expected_registration_revision: int | None = None, enabled: bool = True) -> int:
        from .native_contracts import MAX_ACCOUNT_ACTORS
        if type(bindings) is not tuple or not bindings:
            raise TypeError("nonempty tuple of Bindings required")
        for name, value in (("actor_id", actor_id), ("account_key", account_key), ("operator_action_id", operator_action_id)):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", value or ""):
                raise ValueError(f"opaque {name} required")
        for name, value in (("manifest_sha256", manifest_sha256), ("contract_lock_sha256", contract_lock_sha256)):
            if not re.fullmatch(r"[0-9a-f]{64}", value or ""):
                raise ValueError(f"{name} must be sha256 hex")
        keys = [(b.account_key, b.strategy_key, b.exchange, b.instrument_id, b.hedge_flag) for b in bindings]
        if len(set(keys)) != len(keys) or any(b.account_key != account_key for b in bindings):
            raise ValueError("bindings must be unique and share the registered account")
        record = {"actor_id": actor_id, "account_key": account_key,
                  "bindings": [c.encode(b) for b in bindings], "enabled": bool(enabled),
                  "operator_action_id": operator_action_id, "manifest_sha256": manifest_sha256,
                  "contract_lock_sha256": contract_lock_sha256, "revision": 0}
        payload = _json(record)
        digest = _hash(payload)
        with self._tx():
            self._require_v2("CONTROL_SCHEMA_UNSUPPORTED")
            self._require_maintenance()
            row = self._conn.execute("SELECT registration_revision,payload_hash FROM actor_registrations WHERE actor_id=?",
                                     (actor_id,)).fetchone()
            if row is None:
                revision = 1
                if expected_registration_revision is not None:
                    _reject("ACTOR_CONFLICT")
            else:
                if row[1] == digest:
                    return row[0]  # idempotent: same identity and content
                revision = row[0] + 1
                if expected_registration_revision != row[0]:
                    _reject("ACTOR_CONFLICT")
            active = self._conn.execute(
                "SELECT COUNT(DISTINCT actor_id) FROM actor_registrations WHERE actor_id!=? AND enabled=1",
                (actor_id,)).fetchone()[0]
            if enabled and active >= MAX_ACCOUNT_ACTORS:
                _reject("ACTOR_CAPACITY")
            self._conn.execute("INSERT OR REPLACE INTO actor_registrations VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (actor_id, account_key, revision, _json({**record, "revision": revision}), digest,
                                int(enabled), operator_action_id,
                                manifest_sha256, contract_lock_sha256, _now(self._clock).isoformat()))
            self._event(f"actor:{actor_id}:{revision}", "ACTOR_REG", payload)
            self._bump()
            return revision

    def actors(self) -> tuple:
        with self._tx():
            self._require_v2("CONTROL_SCHEMA_UNSUPPORTED")
            rows = self._conn.execute("SELECT payload FROM actor_registrations WHERE enabled=1 ORDER BY actor_id").fetchall()
            result = []
            for row in rows:
                record = json.loads(row[0])
                result.append(ActorRecord(
                    actor_id=record["actor_id"], account_key=record["account_key"],
                    bindings=tuple(c.decode(item, c.Binding) for item in record["bindings"]),
                    registration_revision=record["revision"], enabled=record["enabled"],
                    operator_action_id=record["operator_action_id"], manifest_sha256=record["manifest_sha256"],
                    contract_lock_sha256=record["contract_lock_sha256"]))
            return tuple(result)

    def register_operator_manifest(self, manifest) -> None:
        from .native_evidence import OperatorRegistrationManifest
        if type(manifest) is not OperatorRegistrationManifest:
            raise TypeError("OperatorRegistrationManifest required")
        paths = [manifest.record_path, manifest.authorization_artifact_path] + [item.path for item in manifest.artifacts]
        for path in paths:
            # F2 hardening: bare dot-names (module-like) are not explicit local files.
            if not any(marker in path for marker in ("/", "\\", ":")):
                _reject("MANIFEST_PATH_INVALID")
        payload = c.encode(manifest, schema_version=c.V2_SCHEMA_VERSION)
        digest = _hash(payload)
        with self._tx():
            self._require_v2("CONTROL_SCHEMA_UNSUPPORTED")
            self._require_maintenance()
            prior = self._conn.execute("SELECT payload_hash FROM operator_manifests WHERE registration_id=?",
                                       (manifest.registration_id,)).fetchone()
            if prior is not None:
                if prior[0] != digest:
                    _reject("MANIFEST_CONFLICT")
                return
            self._conn.execute("INSERT INTO operator_manifests VALUES(?,?,?,?)",
                               (manifest.registration_id, payload, digest, _now(self._clock).isoformat()))
            self._event("manifest:" + manifest.registration_id, "MANIFEST", payload)
            self._bump()

    def allocate_service_epoch(self) -> int:
        with self._tx():
            self._require_v2("CONTROL_SCHEMA_UNSUPPORTED")
            from .native_contracts import INITIAL_SERVICE_EPOCH
            current = self._extra("service_epoch")
            value = (int(current) + 1) if current else INITIAL_SERVICE_EPOCH
            self._set_extra("service_epoch", str(value))
            self._bump()
            return value

    def service_epoch(self) -> int | None:
        with self._tx():
            try:
                current = self._extra("service_epoch")
            except sqlite3.Error:
                return None
            return int(current) if current else None

    def record_http_request(self, *, instance_id: str, session_epoch: str, generation: str,
                            service_epoch: int, request_id: str, fingerprint: str,
                            response: str | None = None) -> tuple[c.ResultCode, str | None]:
        if type(service_epoch) is not int or service_epoch < 1:
            raise ValueError("service_epoch must be a positive integer")
        with self._tx():
            self._require_v2("CONTROL_SCHEMA_UNSUPPORTED")
            namespace = "|".join((instance_id, session_epoch, generation, str(service_epoch), request_id))
            row = self._conn.execute("SELECT fingerprint,response FROM http_requests WHERE namespace=?", (namespace,)).fetchone()
            if row is not None:
                if row[0] != fingerprint:
                    _reject("REQUEST_ID_CONFLICT")
                return c.ResultCode.IDEMPOTENT, row[1]
            self._conn.execute("INSERT INTO http_requests VALUES(?,?,?,?)",
                               (namespace, fingerprint, response, _now(self._clock).isoformat()))
            self._bump()
            return c.ResultCode.OK, None

    def store_http_response(self, *, instance_id: str, session_epoch: str, generation: str,
                            service_epoch: int, request_id: str, fingerprint: str, response: str) -> None:
        with self._tx():
            self._require_v2("CONTROL_SCHEMA_UNSUPPORTED")
            namespace = "|".join((instance_id, session_epoch, generation, str(service_epoch), request_id))
            row = self._conn.execute("SELECT fingerprint FROM http_requests WHERE namespace=?", (namespace,)).fetchone()
            if row is None or row[0] != fingerprint:
                _reject("REQUEST_ID_CONFLICT")
            self._conn.execute("UPDATE http_requests SET response=? WHERE namespace=?", (response, namespace))

    def register_capture(self, snapshot: c.ExecutionSnapshot, *, instance_id: str, session_epoch: str,
                         generation: str) -> datetime:
        """RULING 06 by-value capture: trusted receipt time, immutable identity."""
        if type(snapshot) is not c.ExecutionSnapshot:
            raise TypeError("ExecutionSnapshot required")
        times = snapshot.times
        for stamp in (times.received_at, times.quote.received_at, times.contract.received_at,
                      times.account.received_at, times.positions.received_at,
                      *(item.timing.received_at for item in snapshot.positions)):
            if stamp is not None:
                _reject("SNAPSHOT_INVALID")
        raw_hash = _hash(c.encode(snapshot))
        with self._tx():
            self._require_v2("CONTROL_SCHEMA_UNSUPPORTED")
            prior = self._conn.execute("SELECT * FROM snapshot_captures WHERE snapshot_id=?",
                                       (snapshot.snapshot_id,)).fetchone()
            if prior is not None:
                same = (prior["instance_id"], prior["session_epoch"], prior["generation"], prior["raw_hash"]) == \
                       (instance_id, session_epoch, generation, raw_hash)
                if not same:
                    _reject("SNAPSHOT_CONFLICT")
                return datetime.fromisoformat(prior["trusted_received_at"])  # idempotent: never refreshed
            now = _now(self._clock)
            self._conn.execute("INSERT INTO snapshot_captures VALUES(?,?,?,?,?,?)",
                               (snapshot.snapshot_id, instance_id, session_epoch, generation, raw_hash,
                                now.isoformat()))
            self._bump()
            return now

    def capture_registered(self, snapshot_id: str) -> bool:
        with self._tx():
            self._require_v2("CONTROL_SCHEMA_UNSUPPORTED")
            return self._conn.execute("SELECT 1 FROM snapshot_captures WHERE snapshot_id=?", (snapshot_id,)).fetchone() is not None

    def record_event_envelopes(self, envelopes: tuple, *, instance_id: str):
        """RULING 09: persist (stream,sequence)->(id,hash); advance the contiguous prefix."""
        from . import native_contracts as nc
        if type(envelopes) is not tuple or not envelopes:
            raise TypeError("nonempty tuple of RawCallbackEnvelope required")
        if not isinstance(instance_id, str) or not instance_id:
            raise ValueError("instance_id required")
        with self._tx():
            self._require_v2("CONTROL_SCHEMA_UNSUPPORTED")
            streams = {(item.event_stream_id, item.callback_session_epoch, item.callback_generation) for item in envelopes}
            if len(streams) != 1:
                _reject("EVENT_STREAM_MIXED")
            stream_id, session_epoch, generation = streams.pop()
            state = self._conn.execute("SELECT * FROM event_stream_state WHERE event_stream_id=?", (stream_id,)).fetchone()
            if state is None:
                self._conn.execute("INSERT INTO event_stream_state VALUES(?,?,?,?,0,0,NULL,NULL)",
                                   (stream_id, instance_id, session_epoch, generation))
                state = self._conn.execute("SELECT * FROM event_stream_state WHERE event_stream_id=?", (stream_id,)).fetchone()
            elif (state["instance_id"], state["session_epoch"], state["generation"]) != (instance_id, session_epoch, generation):
                _reject("EVENT_STREAM_CONFLICT")
            for envelope in envelopes:
                payload_hash = _hash(nc.encode(envelope))
                row = self._conn.execute("SELECT event_id,event_sha256 FROM event_stream_events WHERE event_stream_id=? AND sequence=?",
                                         (stream_id, envelope.event_sequence)).fetchone()
                if row is not None:
                    if (row[0], row[1]) != (envelope.event_id, payload_hash):
                        if not hasattr(self, "_pending_latches"):
                            self._pending_latches = []
                        self._pending_latches.append((c.ReconcileReason.EVENT_GAP, "event-stream-conflict", None))
                        _reject("EVENT_CONFLICT")
                    continue  # idempotent duplicate batch
                clash = self._conn.execute("SELECT sequence FROM event_stream_events WHERE event_stream_id=? AND event_id=?",
                                           (stream_id, envelope.event_id)).fetchone()
                if clash is not None:
                    if not hasattr(self, "_pending_latches"):
                        self._pending_latches = []
                    self._pending_latches.append((c.ReconcileReason.EVENT_GAP, "event-stream-conflict", None))
                    _reject("EVENT_CONFLICT")
                self._conn.execute("INSERT INTO event_stream_events VALUES(?,?,?,?)",
                                   (stream_id, envelope.event_sequence, envelope.event_id, payload_hash))
                if envelope.event_sequence > state["last_sequence"]:
                    self._conn.execute("UPDATE event_stream_state SET last_sequence=? WHERE event_stream_id=?",
                                       (envelope.event_sequence, stream_id))
            # The checkpoint only crosses sequences that are durably present.
            acked = state["acked_sequence"]
            while True:
                following = self._conn.execute(
                    "SELECT event_id,event_sha256 FROM event_stream_events WHERE event_stream_id=? AND sequence=?",
                    (stream_id, acked + 1)).fetchone()
                if following is None:
                    break
                acked += 1
                self._conn.execute("UPDATE event_stream_state SET acked_sequence=?,acked_event_id=?,acked_event_hash=? WHERE event_stream_id=?",
                                   (acked, following[0], following[1], stream_id))
            state = self._conn.execute("SELECT * FROM event_stream_state WHERE event_stream_id=?", (stream_id,)).fetchone()
            return nc.AckWatermark(event_stream_id=stream_id, instance_id=state["instance_id"],
                                   session_epoch=state["session_epoch"], generation=state["generation"],
                                   sequence=state["acked_sequence"], event_id=state["acked_event_id"],
                                   event_sha256=state["acked_event_hash"])

    def ack_watermark(self, *, event_stream_id: str):
        from . import native_contracts as nc
        with self._tx():
            self._require_v2("CONTROL_SCHEMA_UNSUPPORTED")
            state = self._conn.execute("SELECT * FROM event_stream_state WHERE event_stream_id=?", (event_stream_id,)).fetchone()
            if state is None:
                return None
            return nc.AckWatermark(event_stream_id=event_stream_id, instance_id=state["instance_id"],
                                   session_epoch=state["session_epoch"], generation=state["generation"],
                                   sequence=state["acked_sequence"], event_id=state["acked_event_id"],
                                   event_sha256=state["acked_event_hash"])

    def validate_client_ack(self, client_ack) -> None:
        """Above/inconsistent claims are invalid; lower claims are merely stale."""
        from . import native_contracts as nc
        if type(client_ack) is not nc.AckWatermark:
            raise TypeError("AckWatermark required")
        with self._tx():
            self._require_v2("CONTROL_SCHEMA_UNSUPPORTED")
            state = self._conn.execute("SELECT * FROM event_stream_state WHERE event_stream_id=?",
                                       (client_ack.event_stream_id,)).fetchone()
            durable = None
            if state is not None:
                durable = nc.AckWatermark(event_stream_id=client_ack.event_stream_id, instance_id=state["instance_id"],
                                          session_epoch=state["session_epoch"], generation=state["generation"],
                                          sequence=state["acked_sequence"], event_id=state["acked_event_id"],
                                          event_sha256=state["acked_event_hash"])
            if durable is None:
                if client_ack.sequence > 0:
                    _reject("ACK_WATERMARK_INVALID")
                return
            if client_ack.sequence > durable.sequence:
                _reject("ACK_WATERMARK_INVALID")
            if client_ack.sequence == durable.sequence and (
                    client_ack.event_id != durable.event_id or client_ack.event_sha256 != durable.event_sha256):
                _reject("ACK_WATERMARK_INVALID")

    def execution_counters(self, binding: c.Binding):
        """RULING 01 counter facts: journal facts only, never invented precision."""
        from . import native_contracts as nc
        if type(binding) is not c.Binding:
            raise TypeError("Binding required")
        with self._tx():
            self._require_v2("CONTROL_SCHEMA_UNSUPPORTED")
            binding_hash = c.canonical_hash(binding)
            commands = self._conn.execute(
                "SELECT kind,command_id,closed FROM dispatch_commands WHERE binding_hash=?", (binding_hash,)).fetchall()
            committed = len(commands)
            open_commands = sum(1 for row in commands if not row["closed"])
            receipts = self._conn.execute(
                "SELECT r.stage FROM dispatch_receipts r JOIN dispatch_commands d USING(command_id) WHERE d.binding_hash=?",
                (binding_hash,)).fetchall()
            consumed = sum(1 for row in receipts if row["stage"] == "CONSUMED")
            returned = sum(1 for row in receipts if row["stage"] == "RETURNED")
            no_call = sum(1 for row in receipts if row["stage"] == "NO_CALL")
            submit_command_ids = {row["command_id"] for row in commands if row["kind"] == "SUBMIT"}
            cancel_command_ids = {row["command_id"] for row in commands if row["kind"] == "CANCEL"}
            consumed_commands = {row[0] for row in self._conn.execute(
                "SELECT r.command_id FROM dispatch_receipts r JOIN dispatch_commands d USING(command_id) "
                "WHERE d.binding_hash=? AND r.stage IN ('CONSUMED','RETURNED')", (binding_hash,))}
            send_lower = len(submit_command_ids & consumed_commands)
            cancel_lower = len(cancel_command_ids & consumed_commands)
            # Without reconciled client-boundary evidence the count stays an interval.
            send_attempts = nc.CallCount(value=None, lower_bound=send_lower, upper_bound=None)
            cancel_attempts = nc.CallCount(value=None, lower_bound=cancel_lower, upper_bound=None)
            local_ids = tuple(sorted({row[0] for row in self._conn.execute(
                "SELECT l.order_id FROM legs l JOIN intents i ON i.id=l.intent_key "
                "WHERE i.account_key=? AND l.order_id IS NOT NULL AND l.owned=1", (binding.account_key,))}))
            trades = self._conn.execute("SELECT COUNT(*) FROM trades WHERE account_key=?", (binding.account_key,)).fetchone()[0]
            return nc.ExecutionCounters(
                committed_dispatch_count=committed, consumed_receipt_count=consumed,
                returned_receipt_count=returned, no_call_receipt_count=no_call,
                native_send_attempts=send_attempts, native_cancel_attempts=cancel_attempts,
                local_order_ids=local_ids, broker_order_acks=None, unique_trade_count=trades,
                fake_send_count=None, fake_cancel_count=None,
                unresolved_dispatch_count=open_commands, count_complete=False)

    def pending_reconcile_count(self, binding: c.Binding) -> int:
        with self._tx():
            self._require_v2("CONTROL_SCHEMA_UNSUPPORTED")
            return self._conn.execute(
                "SELECT COUNT(*) FROM events e JOIN legs l ON l.leg_id=e.leg_id JOIN intents i ON i.id=l.intent_key "
                "WHERE e.kind='CALLBACK' AND e.applied=0 AND i.account_key=?", (binding.account_key,)).fetchone()[0]


    def _associate(self, event: c.ExecutionEvent) -> tuple[str | None, str | None]:
        from .reconcile import simulation_evidence_id
        if event.error_code == "QUANTITY_CONFLICT" or (event.total_volume is not None and
                ((event.traded_volume is not None and event.traded_volume > event.total_volume) or
                 (event.cancel_volume is not None and event.cancel_volume > event.total_volume) or
                 (event.traded_volume is not None and event.cancel_volume is not None and event.traded_volume + event.cancel_volume > event.total_volume))):
            return None, "QUANTITY_CONFLICT"
        if event.kind is c.EventKind.ERROR and event.error_code is None:
            return None, "MISSING_ERROR_CODE"
        if event.ownership is c.Ownership.FOREIGN:
            return None, "FOREIGN_EVENT"
        if any(value is None for value in (event.account_key, event.exchange, event.instrument_id,
                                          event.hedge_flag, event.trading_day, event.direction, event.offset)):
            return None, "MISSING_IDENTITY"
        if event.kind is c.EventKind.TRADE and (event.volume is None or event.volume < 1 or event.price is None or event.trade_id is None):
            return None, "MISSING_TRADE_KEY"
        evidence = event.ownership_evidence
        if evidence is None or not evidence.callback_account_verified or not evidence.strategy_scope_verified:
            return None, "OWNERSHIP_UNVERIFIED"
        native_route = None
        if evidence.evidence_id.startswith("native-route:"):
            route_id = evidence.evidence_id.split("native-route:", 1)[1]
            row = self._conn.execute("SELECT payload FROM events WHERE event_id=?", ("nroute:" + route_id,)).fetchone()
            if row is None:
                return None, "OWNERSHIP_UNVERIFIED"
            from .native_evidence import NativeRouteEvidence
            route = c.decode(row[0], NativeRouteEvidence, schema_version=c.V2_SCHEMA_VERSION)
            if route.expires_at <= _now(self._clock):
                return None, "OWNERSHIP_UNVERIFIED"
            native_route = route
        candidates = []
        for row in self._conn.execute("SELECT leg_id FROM legs WHERE attempt_no=1").fetchall():
            record = self._record(row[0])
            b, leg = record.intent.binding, record.leg
            if (event.account_key, event.callback_session_epoch, event.trading_day, event.exchange, event.instrument_id,
                event.hedge_flag, event.direction, event.offset) != (b.account_key, b.session_epoch, b.trading_day,
                                                                    b.exchange, b.instrument_id, b.hedge_flag, leg.direction, leg.offset):
                continue
            ctx = self._context(b)
            if ctx is None:
                continue
            if native_route is None:
                if ctx.mode is not c.ExecutionMode.SIMULATION or ctx.snapshot.source is not c.Source.SIMULATION:
                    continue
            elif ctx.snapshot.source is not c.Source.PYTHONGO:
                continue
            if native_route is not None:
                rb = native_route.binding
                if (rb.account_key, rb.session_epoch, rb.exchange, rb.instrument_id, rb.hedge_flag) != \
                        (b.account_key, b.session_epoch, b.exchange, b.instrument_id, b.hedge_flag):
                    continue
                if event.front_id is not None and native_route.front_id is not None and event.front_id != native_route.front_id:
                    return None, "ALIAS_CONFLICT"
                if event.session_id is not None and native_route.session_id is not None and event.session_id != native_route.session_id:
                    return None, "ALIAS_CONFLICT"
            elif evidence.evidence_id != simulation_evidence_id(b.strategy_key):
                continue
            local_match = event.order_id is not None and record.order_id == event.order_id
            exchange_match = event.order_sys_id is not None and record.order_sys_id == event.order_sys_id
            if not local_match and not exchange_match:
                continue
            if record.order_id is not None and event.order_id is not None and event.order_id != record.order_id:
                return None, "ALIAS_CONFLICT"
            if record.order_sys_id is not None and event.order_sys_id is not None and event.order_sys_id != record.order_sys_id:
                return None, "ALIAS_CONFLICT"
            if event.memo is not None and event.memo != leg.leg_id:
                return None, "MEMO_CONFLICT"
            if evidence.journal_leg_id not in ("unassociated", leg.leg_id):
                return None, "MEMO_CONFLICT"
            # A trade without FrontID/SessionID associates through the verified order
            # alias; the durable local/exchange alias match above IS that evidence.
            candidates.append(leg.leg_id)
        return (candidates[0], None) if len(candidates) == 1 else (None, "ALIAS_AMBIGUOUS" if candidates else "ALIAS_PENDING")

    def _quarantine(self, event_id: str, reason: str, leg_id=None):
        self._conn.execute("UPDATE events SET applied=0,reason=?,leg_id=? WHERE event_id=?", (reason, leg_id, event_id))
        if reason in ("QUANTITY_CONFLICT", "TRADE_CONFLICT", "EVENT_CONFLICT"):
            self._latch(c.ReconcileReason.QUANTITY_CONFLICT, event_id)
        elif reason in ("ALIAS_CONFLICT", "ALIAS_AMBIGUOUS", "MEMO_CONFLICT"):
            self._latch(c.ReconcileReason.OWNERSHIP_CONFLICT, event_id)

    def _trade_payload(self, event: c.ExecutionEvent, leg_id: str) -> str:
        return _json({"leg_id": leg_id, "account": event.account_key, "day": event.trading_day.isoformat(),
                      "exchange": event.exchange, "instrument": event.instrument_id, "hedge": event.hedge_flag,
                      "trade_id": event.trade_id, "order_id": event.order_id, "order_sys_id": event.order_sys_id,
                      "direction": event.direction.value, "offset": event.offset.value,
                      "price": format(event.price.normalize(), "f"), "volume": event.volume})

    def _apply(self, event: c.ExecutionEvent) -> tuple[str | None, str | None]:
        leg_id, reason = self._associate(event)
        if reason is not None:
            self._quarantine(event.event_id, reason)
            return None, reason
        record = self._record(leg_id)
        row = self._row(leg_id)
        volume = record.leg.volume
        if event.total_volume is not None and event.total_volume != volume:
            self._quarantine(event.event_id, "QUANTITY_CONFLICT", leg_id)
            return leg_id, "QUANTITY_CONFLICT"
        if event.kind is c.EventKind.ORDER:
            if any(event_value is not None and row[column] is not None and event_value != row[column]
                   for column, event_value in (("front_id", event.front_id), ("session_id", event.session_id))):
                self._quarantine(event.event_id, "ALIAS_CONFLICT", leg_id)
                return leg_id, "ALIAS_CONFLICT"
            if event.price is None:
                self._quarantine(event.event_id, "INCOMPLETE_ORDER", leg_id)
                return leg_id, "INCOMPLETE_ORDER"
            if event.price != record.leg.price:
                self._quarantine(event.event_id, "QUANTITY_CONFLICT", leg_id)
                return leg_id, "QUANTITY_CONFLICT"
            if event.total_volume is None or event.traded_volume is None or event.status is None:
                self._quarantine(event.event_id, "INCOMPLETE_ORDER", leg_id)
                return leg_id, "INCOMPLETE_ORDER"
            filled = max(record.reported_filled, event.traded_volume)
            if filled > volume or (event.cancel_volume is not None and event.traded_volume + event.cancel_volume > volume):
                self._quarantine(event.event_id, "QUANTITY_CONFLICT", leg_id)
                return leg_id, "QUANTITY_CONFLICT"
            terminal = event.status in c.TERMINAL_ORDER_STATES
            if event.status in (c.OrderState.LOCAL_REJECTED, c.OrderState.EXPIRED):
                self._quarantine(event.event_id, "INVALID_CALLBACK_STATE", leg_id)
                return leg_id, "INVALID_CALLBACK_STATE"
            if event.status is c.OrderState.FILLED and event.traded_volume != volume:
                self._quarantine(event.event_id, "QUANTITY_CONFLICT", leg_id)
                return leg_id, "QUANTITY_CONFLICT"
            if terminal and event.status is not c.OrderState.FILLED and (event.cancel_volume is None or event.traded_volume + event.cancel_volume != volume):
                self._quarantine(event.event_id, "INCOMPLETE_TERMINAL", leg_id)
                return leg_id, "INCOMPLETE_TERMINAL"
            if terminal and event.status is not c.OrderState.FILLED and record.trade_filled > event.traded_volume:
                self._quarantine(event.event_id, "QUANTITY_CONFLICT", leg_id)
                return leg_id, "QUANTITY_CONFLICT"
            if record.state in c.TERMINAL_ORDER_STATES:
                # Persisted final quantities bind every late event, terminal or not.
                persisted_cancel = 0 if record.state is c.OrderState.FILLED else record.cancel_volume
                contradiction = event.traded_volume + (persisted_cancel or 0) > volume
                if terminal and event.traded_volume < record.risk_filled:
                    contradiction = True
                if terminal and record.cancel_volume is not None and event.cancel_volume not in (None, record.cancel_volume):
                    contradiction = True
                if event.cancel_volume is not None and record.cancel_volume is not None and event.cancel_volume > record.cancel_volume:
                    contradiction = True
                if contradiction:
                    self._quarantine(event.event_id, "QUANTITY_CONFLICT", leg_id)
                    return leg_id, "QUANTITY_CONFLICT"
                state = record.state
                cancel_volume = persisted_cancel
            else:
                state = event.status
                if filled == volume or record.trade_filled == volume:
                    state = c.OrderState.FILLED
                elif filled or record.trade_filled:
                    if not terminal:
                        state = c.OrderState.PARTIAL
                elif record.state in (c.OrderState.WORKING, c.OrderState.PARTIAL) and event.status is c.OrderState.SUBMITTED_LOCAL:
                    state = record.state
                cancel_volume = event.cancel_volume if terminal else record.cancel_volume
                if state is c.OrderState.FILLED:
                    cancel_volume = 0
            if filled + (cancel_volume or 0) > volume:
                self._quarantine(event.event_id, "QUANTITY_CONFLICT", leg_id)
                return leg_id, "QUANTITY_CONFLICT"
            if event.order_sys_id is not None:
                collisions = [self._record(x[0]) for x in self._conn.execute("SELECT leg_id FROM legs WHERE order_sys_id=? AND leg_id!=?", (event.order_sys_id, leg_id))]
                if any((x.intent.binding.account_key, x.intent.binding.trading_day, x.intent.binding.exchange) ==
                       (event.account_key, event.trading_day, event.exchange) for x in collisions):
                    self._quarantine(event.event_id, "ALIAS_CONFLICT", leg_id)
                    return leg_id, "ALIAS_CONFLICT"
            if filled > record.reported_filled or state in c.TERMINAL_ORDER_STATES:
                self._conn.execute("UPDATE reservations SET state='HELD',not_proven_reflected=volume WHERE leg_id=? AND state='REFLECTED'", (leg_id,))
            confirmed = event.status not in (c.OrderState.SUBMITTED_LOCAL, c.OrderState.SUBMITTING, c.OrderState.RESERVED)
            self._conn.execute("UPDATE legs SET state=?,reported_filled=?,cancel_volume=?,order_sys_id=COALESCE(order_sys_id,?),front_id=COALESCE(front_id,?),session_id=COALESCE(session_id,?),owned=1,confirmed=MAX(confirmed,?),unknown=CASE WHEN ? THEN 0 ELSE unknown END WHERE leg_id=?",
                               (state.value, filled, cancel_volume, event.order_sys_id, event.front_id, event.session_id, int(confirmed), int(confirmed), leg_id))
        elif event.kind is c.EventKind.TRADE:
            key = (event.account_key, event.trading_day.isoformat(), event.exchange, event.instrument_id, event.trade_id)
            payload = self._trade_payload(event, leg_id)
            existing = self._conn.execute("SELECT payload FROM trades WHERE account_key=? AND trading_day=? AND exchange=? AND instrument_id=? AND trade_id=?", key).fetchone()
            if existing:
                if existing[0] != payload:
                    self._quarantine(event.event_id, "TRADE_CONFLICT", leg_id)
                    return leg_id, "TRADE_CONFLICT"
            else:
                filled = record.trade_filled + event.volume
                if filled > volume or (record.state in c.TERMINAL_ORDER_STATES and record.cancel_volume is not None and filled + record.cancel_volume > volume):
                    self._quarantine(event.event_id, "QUANTITY_CONFLICT", leg_id)
                    return leg_id, "QUANTITY_CONFLICT"
                self._conn.execute("INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?)", key + (leg_id, payload, event.volume, str(event.price)))
                self._conn.execute("UPDATE reservations SET state='HELD',not_proven_reflected=volume WHERE leg_id=? AND state='REFLECTED'", (leg_id,))
                state = record.state
                if filled == volume:
                    state = c.OrderState.FILLED
                elif state not in c.TERMINAL_ORDER_STATES:
                    state = c.OrderState.PARTIAL
                self._conn.execute("UPDATE legs SET trade_filled=?,state=?,settled=0 WHERE leg_id=?", (filled, state.value, leg_id))
            self._conn.execute("UPDATE reservations SET state='HELD',not_proven_reflected=volume WHERE leg_id=? AND state='REFLECTED'", (leg_id,))
        elif event.kind is c.EventKind.ERROR:
            self._conn.execute("UPDATE legs SET unknown=1 WHERE leg_id=?", (leg_id,))
        # CANCEL notifications contain no final-state guarantee and do not mutate order quantities.
        self._conn.execute("UPDATE events SET applied=1,reason=NULL,leg_id=? WHERE event_id=?", (leg_id, event.event_id))
        return leg_id, None

    def _retry_pending(self):
        # Order aliases establish provenance before trade replay, including synchronous callbacks.
        rows = self._conn.execute("SELECT payload FROM events WHERE kind='CALLBACK' AND applied=0 AND reason IN ('ALIAS_PENDING','INCOMPLETE_ORDER','INCOMPLETE_TERMINAL') ORDER BY rowid").fetchall()
        events = [c.decode(x[0], c.ExecutionEvent) for x in rows]
        for event in sorted(events, key=lambda e: e.kind is c.EventKind.TRADE):
            self._apply(event)

    def ingest_event(self, event: c.ExecutionEvent) -> IngestResult:
        if type(event) is not c.ExecutionEvent:
            raise TypeError("ExecutionEvent required")
        # Raw arbitrary Memo/errMsg never enters the database through this public typed path either.
        from .reconcile import _MEMO
        if event.memo is not None and not _MEMO.fullmatch(event.memo):
            event = replace(event, memo=None, ownership=c.Ownership.UNKNOWN, ownership_evidence=None)
        payload = c.encode(event)
        with self._tx():
            existing = self._conn.execute("SELECT * FROM events WHERE event_id=?", (event.event_id,)).fetchone()
            if existing:
                if existing["payload_hash"] != _hash(payload) or existing["kind"] != "CALLBACK":
                    conflict_id = "conflict:" + _hash(event.event_id + payload)
                    if not self._conn.execute("SELECT 1 FROM events WHERE event_id=?", (conflict_id,)).fetchone():
                        self._event(conflict_id, "CONFLICT", payload, reason="EVENT_CONFLICT", applied=False)
                    self._latch(c.ReconcileReason.QUANTITY_CONFLICT, conflict_id)
                    return IngestResult(code=c.ResultCode.QUARANTINED, event_id=event.event_id, leg_id=None,
                                        reason="EVENT_CONFLICT", revision=self._bump())
                return IngestResult(code=c.ResultCode.IDEMPOTENT, event_id=event.event_id, leg_id=existing["leg_id"],
                                    reason=existing["reason"], revision=self._revision())
            self._event(event.event_id, "CALLBACK", payload, applied=False)
            leg_id, reason = self._apply(event)
            if reason is None and event.kind is c.EventKind.ORDER:
                self._retry_pending()
            result = IngestResult(code=c.ResultCode.OK if reason is None else c.ResultCode.QUARANTINED,
                                  event_id=event.event_id, leg_id=leg_id, reason=reason, revision=self._bump())
        return result

    def _inventory_match(self, record: LegRecord, item) -> bool:
        b, leg = record.intent.binding, record.leg
        return (item.account_key, item.trading_day, item.exchange, item.instrument_id, item.hedge_flag, item.direction, item.offset) == \
               (b.account_key, b.trading_day, b.exchange, b.instrument_id, b.hedge_flag, leg.direction, leg.offset) and \
               ((item.order_sys_id is not None and record.order_sys_id == item.order_sys_id) or
                (item.order_id is not None and record.order_id == item.order_id)) and \
               not (item.order_id is not None and record.order_id is not None and item.order_id != record.order_id) and \
               not (item.order_sys_id is not None and record.order_sys_id is not None and item.order_sys_id != record.order_sys_id)

    def recover(self, proof: c.ReconciliationProof | None = None, events: tuple[c.ExecutionEvent, ...] = ()) -> RecoveryResult:
        if type(events) is not tuple:
            raise TypeError("tuple events required")
        for event in events:
            self.ingest_event(event)
        with self._tx():
            self._timeout()
            self._retry_pending()
            if proof is not None:
                self._absorb(proof)
            unresolved = tuple(x[0] for x in self._conn.execute("SELECT leg_id FROM legs WHERE attempt_no>0 AND settled=0 ORDER BY leg_id"))
            if not unresolved:
                self._conn.execute("UPDATE events SET applied=0 WHERE event_id='latch:RESTART_UNRESOLVED'")
            reconcile = self._reconcile()
            result = RecoveryResult(code=c.ResultCode.BLOCKED if reconcile.required else c.ResultCode.OK,
                                    reconcile=reconcile, revision=self._bump(), unresolved_leg_ids=unresolved,
                                    unresolved_command_ids=self._open_command_ids())
        return result

    def _absorb(self, proof: c.ReconciliationProof):
        if type(proof) is not c.ReconciliationProof:
            raise TypeError("ReconciliationProof required")
        if proof.source is not c.Source.SIMULATION or not proof.verified or not proof.exclusive_writer_verified or proof.complete_through is None:
            return
        snapshot_row = self._conn.execute("SELECT payload FROM events WHERE event_id=?", ("snapshot:" + proof.position_snapshot_id,)).fetchone()
        if snapshot_row is None:
            return
        snapshot = c.decode(snapshot_row[0], c.ExecutionSnapshot)
        b = snapshot.binding
        if (proof.account_key, proof.session_epoch, proof.trading_day) != (b.account_key, b.session_epoch, b.trading_day) or snapshot.source is not c.Source.SIMULATION or snapshot.proof_id != proof.proof_id:
            return
        if snapshot.coverage.level is not c.CoverageLevel.FULL_ACCOUNT or snapshot.times.positions.sampled_at is None or snapshot.times.positions.sampled_at > proof.as_of:
            return
        if snapshot.coverage.complete_through != proof.complete_through or proof.as_of > _now(self._clock) or \
                any(x in self._reconcile().reasons for x in (c.ReconcileReason.EVENT_GAP, c.ReconcileReason.LEDGER_FAILURE,
                                                           c.ReconcileReason.LEDGER_CORRUPT, c.ReconcileReason.QUANTITY_CONFLICT)):
            return
        latest = self._snapshots(b.account_key, epoch=b.session_epoch, day=b.trading_day)
        if not any(x.snapshot_id == snapshot.snapshot_id for x in latest):
            return
        identity = "proof:" + proof.proof_id
        prior = self._conn.execute("SELECT payload FROM events WHERE event_id=?", (identity,)).fetchone()
        if prior and prior[0] != c.encode(proof):
            self._latch(c.ReconcileReason.OWNERSHIP_CONFLICT, "proof-conflict")
            return
        if not prior:
            self._event(identity, "PROOF", c.encode(proof))
        for row in self._conn.execute("SELECT leg_id FROM legs WHERE attempt_no=1").fetchall():
            record = self._record(row[0])
            rb = record.intent.binding
            if (rb.account_key, rb.session_epoch, rb.trading_day, _scope(rb)) != (b.account_key, b.session_epoch, b.trading_day, _scope(b)):
                continue
            if not record.ownership_verified:
                continue
            relevant = [x[0] for x in self._conn.execute("SELECT event_id FROM events WHERE kind='CALLBACK' AND leg_id=? AND applied=1", (record.leg.leg_id,))]
            if record.leg.leg_id not in proof.covered_leg_ids and (not relevant or not set(relevant) <= set(proof.covered_event_ids)):
                continue
            last_event = self._conn.execute("SELECT MAX(recorded_at) FROM events WHERE kind='CALLBACK' AND leg_id=?", (record.leg.leg_id,)).fetchone()[0]
            if last_event and datetime.fromisoformat(last_event) > proof.as_of:
                continue
            matches = [x for x in proof.order_inventory if self._inventory_match(record, x)]
            if len(matches) != 1:
                continue
            order = matches[0]
            if order.total_volume != record.leg.volume or order.traded_volume != record.risk_filled:
                self._latch(c.ReconcileReason.QUANTITY_CONFLICT, proof.proof_id)
                continue
            trades = [x for x in proof.trade_inventory if self._inventory_match(record, x)]
            stored = self._conn.execute("SELECT * FROM trades WHERE leg_id=?", (record.leg.leg_id,)).fetchall()
            if {x.trade_id for x in trades} != {x["trade_id"] for x in stored} or any(not any(
                    x.trade_id == item["trade_id"] and x.volume == item["volume"] and x.price == Decimal(item["price"]) for x in trades) for item in stored):
                continue
            pending = self._conn.execute("SELECT 1 FROM events WHERE kind='CALLBACK' AND applied=0 LIMIT 1").fetchone()
            if pending or record.trade_gap:
                continue
            terminal = record.state in c.TERMINAL_ORDER_STATES
            reservation = self._conn.execute("SELECT baseline,cash_reserve FROM reservations WHERE leg_id=? AND state!='RELEASED'", (record.leg.leg_id,)).fetchone()
            baseline = json.loads(reservation["baseline"]) if reservation is not None and reservation["baseline"] else None
            pos, absorbed, released = _absorption_verdict(record, snapshot, baseline,
                                                          Decimal(reservation["cash_reserve"]) if reservation else Decimal(0))
            if terminal:
                if order.active or record.cancel_volume is None or record.cancel_volume != order.cancel_volume or record.risk_filled + order.cancel_volume != record.leg.volume:
                    continue
                # A completed snapshot must explicitly show the close bucket's freeze released.
                if record.leg.phase is c.Phase.CLOSE:
                    if pos is None or pos.FrozenClosing is None or pos.YdFrozenClosing is None:
                        continue
                    frozen = pos.FrozenClosing - pos.YdFrozenClosing if record.leg.position_bucket is c.Bucket.TD else pos.YdFrozenClosing if record.leg.position_bucket is c.Bucket.YD else pos.FrozenClosing
                    if frozen:
                        continue
                if not released:
                    continue
                self._conn.execute("UPDATE reservations SET state='RELEASED',not_proven_reflected=0,reflected_evidence_id=?,reflected_snapshot_id=? WHERE leg_id=?",
                                   (proof.proof_id, snapshot.snapshot_id, record.leg.leg_id))
                self._conn.execute("UPDATE legs SET settled=1,unknown=0,cancel_state=CASE WHEN cancel_state IN ('REQUESTED','UNKNOWN') THEN 'CONFIRMED' ELSE cancel_state END WHERE leg_id=?", (record.leg.leg_id,))
            elif order.active and record.cancel_state is c.CancelState.NONE and not record.reconcile_required and absorbed:
                self._conn.execute("UPDATE reservations SET state='REFLECTED',not_proven_reflected=0,reflected_evidence_id=?,reflected_snapshot_id=? WHERE leg_id=?",
                                   (proof.proof_id, snapshot.snapshot_id, record.leg.leg_id))

    def request_cancel(self, cancel_id: str, intent_id: str, leg_id: str, context: c.ExecutionContext) -> CancelAttempt:
        if type(cancel_id) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", cancel_id):
            raise ValueError("opaque cancel id required")
        with self._tx():
            record = self._record(leg_id)
            if record.intent.intent_id != intent_id:
                _reject("OWNERSHIP_CONFLICT")
            identity = "cancel:" + cancel_id
            prior = self._conn.execute("SELECT * FROM events WHERE event_id=?", (identity,)).fetchone()
            if prior:
                if prior["leg_id"] != leg_id:
                    _reject("CANCEL_CONFLICT")
                return CancelAttempt(code=c.ResultCode.IDEMPOTENT, cancel_permitted=False, cancel_id=cancel_id,
                                     order_id=record.order_id, leg=record, revision=self._revision())
            if self._fault_reason or any(x in self._reconcile().reasons for x in (c.ReconcileReason.LEDGER_FAILURE, c.ReconcileReason.LEDGER_CORRUPT)):
                _reject("LEDGER_FAILURE")
            if record.cancel_state is not c.CancelState.NONE:
                _reject("CANCEL_UNKNOWN")
            if context.mode is not c.ExecutionMode.SIMULATION or context.binding != record.intent.binding:
                _reject("OWNERSHIP_CONFLICT")
            latest = self._context(record.intent.binding)
            if latest is None or any(getattr(latest, field) != getattr(context, field)
                                     for field in ("mode", "binding", "policy", "authorization", "route_ownership")):
                _reject("CONTEXT_REQUIRED")
            validation = c.validate_context(context, _now(self._clock), action=c.AuthorizationAction.CANCEL_OWNED)
            if not validation.ok:
                _reject(*validation.blockers)
            if context.ledger_revision != self._revision():
                _reject("LEDGER_REVISION_MISMATCH")
            if not record.ownership_verified or record.order_id is None or record.state in c.TERMINAL_ORDER_STATES or record.risk_filled >= record.leg.volume:
                _reject("CANCEL_NOT_AVAILABLE")
            # cancelOrder has no account parameter. Recheck the persistent alias,
            # not merely the caller's RouteOwnership boolean.
            aliases = [self._record(x[0]) for x in self._conn.execute("SELECT leg_id FROM legs WHERE order_id=?", (record.order_id,))]
            if sum((x.intent.binding.account_key, x.intent.binding.session_epoch) ==
                   (record.intent.binding.account_key, record.intent.binding.session_epoch) for x in aliases) != 1:
                _reject("CANCEL_ROUTE_UNVERIFIED")
            quarantined = [c.decode(x[0], c.ExecutionEvent) for x in self._conn.execute("SELECT payload FROM events WHERE kind='CALLBACK' AND applied=0")]
            if any(x.order_id == record.order_id or (x.order_sys_id is not None and x.order_sys_id == record.order_sys_id) or
                   x.memo == leg_id for x in quarantined):
                _reject("CANCEL_ROUTE_UNVERIFIED")
            self._conn.execute("UPDATE legs SET cancel_state='REQUESTED',cancel_id=? WHERE leg_id=?", (cancel_id, leg_id))
            self._event(identity, "CANCEL_ATTEMPT", c.encode(context), leg_id=leg_id)
            result = CancelAttempt(code=c.ResultCode.OK, cancel_permitted=True, cancel_id=cancel_id,
                                   order_id=record.order_id, leg=self._record(leg_id), revision=self._bump())
        return result

    def record_cancel_result(self, cancel_id: str, result: object = None, *, error_code: str | None = None) -> SubmitResult:
        with self._tx():
            return self._record_cancel_result_locked(cancel_id, result, error_code)

    def _record_cancel_result_locked(self, cancel_id: str, result: object, error_code: str | None) -> SubmitResult:
        payload = _json({"returned_integer": type(result) is int, "unknown": error_code is not None or type(result) is not int})
        row = self._conn.execute("SELECT leg_id FROM events WHERE event_id=? AND kind='CANCEL_ATTEMPT'", ("cancel:" + cancel_id,)).fetchone()
        if row is None:
            _reject("CANCEL_NOT_FOUND", code=c.ResultCode.NOT_FOUND)
        leg_id = row[0]
        prior = self._conn.execute("SELECT payload FROM events WHERE event_id=?", ("cancel-result:" + cancel_id,)).fetchone()
        if prior:
            return SubmitResult(code=c.ResultCode.IDEMPOTENT, leg=self._record(leg_id), revision=self._revision())
        self._event("cancel-result:" + cancel_id, "CANCEL_RESULT", payload, leg_id=leg_id)
        if error_code is not None or type(result) is not int:
            self._conn.execute("UPDATE legs SET cancel_state='UNKNOWN' WHERE leg_id=? AND cancel_state!='CONFIRMED'", (leg_id,))
        return SubmitResult(code=c.ResultCode.OK, leg=self._record(leg_id), revision=self._bump())
