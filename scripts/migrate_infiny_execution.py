"""NE1 v1->v2 journal migration: consistent copy, conservation-checked, read-only source."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

V1_TABLES = ("intents", "legs", "events", "trades", "reservations")

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


def _counts(db: sqlite3.Connection) -> dict:
    return {name: db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0] for name in V1_TABLES}


def _unresolved(db: sqlite3.Connection) -> dict:
    return {
        "attempted_legs": db.execute("SELECT COUNT(*) FROM legs WHERE attempt_no>0").fetchone()[0],
        "pending_cancels": db.execute("SELECT COUNT(*) FROM legs WHERE cancel_state IN ('REQUESTED','UNKNOWN')").fetchone()[0],
        "held_reservations": db.execute("SELECT COUNT(*) FROM reservations WHERE state!='RELEASED'").fetchone()[0],
        "quarantined_events": db.execute("SELECT COUNT(*) FROM events WHERE kind='CALLBACK' AND applied=0").fetchone()[0],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Migrate an infiny execution journal copy to v2")
    parser.add_argument("--source", required=True, help="existing journal (opened read-only)")
    parser.add_argument("--output", required=True, help="migration target (must not exist)")
    args = parser.parse_args(argv)
    source, output = Path(args.source), Path(args.output)
    if not source.is_file():
        print(json.dumps({"error": "source_missing"}))
        return 4
    if output.exists():
        print(json.dumps({"error": "output_exists"}))
        return 4
    uri = "file:" + str(source).replace("\\", "/") + "?mode=ro"
    # Read-only source: timeout only. A WAL pragma is impossible here (it raises
    # "attempt to write a readonly database"); the source keeps whatever journal
    # mode its writer left it in and must stay byte-identical.
    src = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        tables = {row[0] for row in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if any(name not in tables for name in V1_TABLES) or "ledger_meta" in tables:
            print(json.dumps({"error": "unexpected_schema", "tables": sorted(tables)}))
            return 4
        if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            print(json.dumps({"error": "integrity_failed"}))
            return 4
        before, unresolved_before = _counts(src), _unresolved(src)
        dst = sqlite3.connect(str(output), timeout=30)
        try:
            with dst:
                src.backup(dst)
            # The writable v2 copy runs in WAL like the live ledger; set it after
            # the backup so page 1 copied from the source cannot mask the mode.
            dst.execute("PRAGMA journal_mode=WAL")
            checks = [dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"]
            after, unresolved_after = _counts(dst), _unresolved(dst)
            checks.append(after == before and unresolved_after == unresolved_before)
            dst.executescript(_SCHEMA_V2)
            dst.execute("INSERT OR REPLACE INTO ledger_meta VALUES('schema','v2')")
            # OFD-06: mark the feature and conservatively tombstone every migrated
            # action whose first issuance cannot be proven from v1 facts. The v1
            # identity columns are introspected: a full legacy journal yields exact
            # (account,intent,leg) plain keys; a reduced v1 shape still leaves a
            # durable legacy guard row per leg instead of crashing.
            dst.execute("INSERT OR REPLACE INTO ledger_meta VALUES('ofd_feature','installed')")
            leg_columns = {row[1] for row in dst.execute("PRAGMA table_info(legs)")}
            intent_columns = {row[1] for row in dst.execute("PRAGMA table_info(intents)")}
            leg_identity = "leg_id" if "leg_id" in leg_columns else "id" if "id" in leg_columns else None
            joinable = ("intent_key" in leg_columns and "id" in intent_columns
                        and "account_key" in intent_columns)
            if leg_identity is not None:
                if joinable:
                    rows = dst.execute(
                        f"SELECT l.{leg_identity}, i.account_key, i.intent_id FROM legs l "
                        "JOIN intents i ON i.id = l.intent_key").fetchall()
                else:
                    rows = [(row[0], None, None) for row in
                            dst.execute(f"SELECT {leg_identity} FROM legs").fetchall()]
                for leg_id, account_key, intent_id in rows:
                    for kind_value in ("SUBMIT", "CANCEL"):
                        if account_key is not None and intent_id is not None:
                            key = f"{account_key}|{intent_id}|{leg_id}|{kind_value}"
                            reason = "legacy-unproven"
                        else:
                            key = f"legacy-shape|{leg_id}|{kind_value}"
                            reason = "legacy-unproven-reduced"
                        dst.execute("INSERT OR IGNORE INTO offer_action_tombstones VALUES(?,?,?)",
                                    (key, reason, "migrated"))
            dst.commit()
            final = _counts(dst)
            checks.append(final == before)
            report = {
                "source": str(source), "output": str(output), "schema": "v2",
                "counts_before": before, "counts_after": final,
                "unresolved_before": unresolved_before, "unresolved_after": _unresolved(dst),
                "conserved": all(checks), "native_enabled": False,
            }
            print(json.dumps(report, indent=2))
            return 0 if all(checks) else 5
        finally:
            dst.close()
    finally:
        src.close()


if __name__ == "__main__":
    sys.exit(main())
