"""Run the NE1 local execution service on 127.0.0.1:17779 (plan section 12.1).

The launcher never imports a trading SDK, never reads credentials, and never
touches the 17778 observer service or its process. Operator registration
subcommands (RULING 05) take exclusive journal maintenance while the service
is stopped; they never stop a process themselves. ``--help`` and ``--dry-run``
write nothing: no journal schema, no service epoch, no pairing token, no
registrations, no network beyond a loopback port probe.

NE1 pairing last-mile (NE1-ARB-PAIRING-LASTMILE-001 RULING 03, LM-S):
  * ``serve`` and every ``register-*`` share the OS-exclusive lock file
    ``<journal>.runtime.lock`` taken before the journal is opened; an
    occupied lock fails with ``JOURNAL_RUNTIME_BUSY`` and is never taken by
    killing a process, deleting the lock file, or swapping the journal. The
    OS releases the lock when the holder exits or crashes.
  * ``register-actor`` accepts ``--expected-registration-revision`` (strictly
    positive integer) and passes it through to the frozen ledger CAS; new
    actors register without it, updates must state the currently verified
    revision, and the operation receipt records expected/actual revision and
    the manifest hash.
  * ``serve`` preflights a NEW EMPTY ``--pairing-dir`` (reparse-point and
    stray-file rejections), tightens and verifies its private DACL before any
    secret is written, writes frozen ``ne1_service_pairing_v2`` files through
    the exclusive writer, and only after every token is durably private and
    the listener is up does it print ``execution_started`` - the line the
    main session saves verbatim as the non-secret ``service-start.json``
    (it carries service_version, contract_lock_sha256, pairing_dir and
    per-instance session/token-file references, never a token or a secret
    digest).

NE1 arming mechanism (NE1-ARB-ARMING-001 RULING 03 AR3.3, ARM-LM-S,
2026-09-18): ``serve`` additionally accepts the optional non-secret
``--arming-readiness PATH`` pointing at a frozen
``ne1_arming_prerequisites_v1`` anchor. The launcher only performs version
and trusted-metadata wiring: the file bytes are fixed (sha256) at startup
and strictly validated (field set, hashes, strict-UTC window, evidence kind
coverage, contract-lock and running client_service.py hash agreement);
omission, expiry or any validation failure NEVER stops the service - it
keeps the original disabled rules with production arming permanently
BLOCKED and reports the reason on stderr. When valid, the non-secret
``execution_started`` line gains exactly one new top-level field
``arming_prerequisites`` (``null``, or the strict object
``{prerequisites_id, prerequisites_sha256, client_file_sha256}``); the six
routes, the DTOs, the stopped-service registration discipline and the
default DISABLED assembly are unchanged. The service release identifier is
``ne1-service-20260918-arming-v1``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17779
OBSERVER_PORT = 17778
RECORD_CATEGORIES = {
    "register-actor": "ActorRegistration",
    "register-evidence": ("NativeBaselineEvidence", "NativeRouteEvidence",
                          "NativeSettlementEvidence", "ClientThreadEvidence"),
    "register-authorization": "NativeAuthorization",
    "register-permit": "NativePilotPermit",
}


class JournalRuntimeLock:
    """OS-exclusive lock over ``<journal>.runtime.lock`` (RULING 03).

    ``serve`` holds it for its whole process lifetime (until service and
    journal close); ``register-*`` holds it across journal maintenance. The
    lock file is created-if-missing but never deleted, and no process is ever
    killed for holding it: an occupied lock raises ``Busy`` so the caller can
    report ``JOURNAL_RUNTIME_BUSY``. The byte-range OS lock is released by
    the operating system when the holder exits or crashes, so a crashed
    predecessor's leftover file does not deadlock the next run. Used only at
    process lifecycle boundaries - never on the per-dispatch write path.
    """

    class Busy(RuntimeError):
        pass

    def __init__(self, journal_path) -> None:
        self.path = Path(str(journal_path) + ".runtime.lock")
        self._handle = None

    def acquire(self) -> "JournalRuntimeLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - deployment host is Windows
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise JournalRuntimeLock.Busy("JOURNAL_RUNTIME_BUSY") from None
        self._handle = handle
        return self

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - deployment host is Windows
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass  # closing the handle always releases the OS lock
        finally:
            handle.close()

    def __enter__(self) -> "JournalRuntimeLock":
        return self.acquire()

    def __exit__(self, *_exc) -> bool:
        self.release()
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the loopback-only NE1 execution service (17779)")
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="serve /execution/v1/ (default)")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--journal", type=Path, required=True)
    serve.add_argument("--audit-log", type=Path, required=True)
    serve.add_argument("--pairing-dir", type=Path, required=True)
    serve.add_argument("--registry-dir", type=Path, required=True)
    serve.add_argument("--contract-lock", type=Path, required=True)
    serve.add_argument("--initialize", action="store_true",
                       help="create the journal when it does not exist")
    serve.add_argument("--append", action="store_true",
                       help="append to an existing audit log")
    serve.add_argument("--max-workers", type=int, default=8)
    serve.add_argument("--arming-readiness", type=Path, default=None,
                       help="optional non-secret ne1_arming_prerequisites_v1 "
                            "anchor; invalid or missing keeps the disabled "
                            "service with arming BLOCKED")
    serve.add_argument("--dry-run", action="store_true")
    serve.set_defaults(command="serve")

    for name, help_text in (
        ("register-actor", "register an account actor from a verified manifest"),
        ("register-evidence", "register native evidence from a verified manifest"),
        ("register-authorization", "register a NativeAuthorization record"),
        ("register-permit", "register a NativePilotPermit record"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--journal", type=Path, required=True)
        sub.add_argument("--manifest", type=Path, required=True)
        sub.add_argument("--contract-lock", type=Path, required=True)
        sub.add_argument("--registry-dir", type=Path,
                         required=(name != "register-actor"))
        sub.add_argument("--initialize", action="store_true")
        if name == "register-actor":
            # RULING 03: accepted by register-actor only. Strictly positive
            # integer; omit for a new actor, must equal the currently
            # verified registration_revision for any update (frozen ledger
            # CAS - never auto-read, never bypassed by changing actor_id).
            sub.add_argument("--expected-registration-revision", type=int,
                             default=None)
    return parser


def _fail(label: str, detail: str | None = None) -> int:
    print(json.dumps({"event": "failed", "reason": label, "detail": detail},
                     ensure_ascii=True, separators=(",", ":")))
    return 2


def _status_line(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _validate_serve(args: argparse.Namespace) -> str | None:
    if args.host != DEFAULT_HOST:
        return "HOST_REJECTED"
    if isinstance(args.port, bool) or not 1 <= args.port <= 65535:
        return "PORT_INVALID"
    if args.port == OBSERVER_PORT:
        return "OBSERVER_PORT_FORBIDDEN"
    if args.journal is None:
        return "JOURNAL_REQUIRED"
    if args.journal.exists() and not args.journal.is_file():
        return "JOURNAL_INVALID"
    if not args.journal.exists() and not args.initialize:
        return "JOURNAL_MISSING_USE_INITIALIZE"
    if args.journal.exists() and args.initialize:
        return "JOURNAL_EXISTS"
    if args.audit_log.exists() and not args.append:
        return "AUDIT_LOG_EXISTS_USE_APPEND"
    if args.audit_log.exists() and not args.audit_log.is_file():
        return "AUDIT_LOG_INVALID"
    if args.pairing_dir is None or str(args.pairing_dir).strip() == "":
        return "PAIRING_DIR_REQUIRED"
    if args.pairing_dir.exists():
        # RULING 02/03 preflight: every run must select a NEW empty pairing
        # directory; leftover files of a previous/failed run never count as
        # served tokens, and link/junction redirections are refused.
        if args.pairing_dir.is_symlink():
            return "PAIRING_DIR_INVALID"
        metadata = os.lstat(args.pairing_dir)
        if stat.S_ISLNK(metadata.st_mode) or metadata.st_reparse_tag:
            return "PAIRING_DIR_INVALID"
        if not args.pairing_dir.is_dir():
            return "PAIRING_DIR_INVALID"
        if any(args.pairing_dir.iterdir()):
            return "PAIRING_DIR_NOT_EMPTY"
    if args.registry_dir is None or str(args.registry_dir).strip() == "":
        return "REGISTRY_DIR_REQUIRED"
    if not args.contract_lock.is_file():
        return "CONTRACT_LOCK_MISSING"
    if isinstance(args.max_workers, bool) or not 1 <= args.max_workers <= 8:
        return "WORKER_LIMIT_INVALID"
    return None


def _probe_port(host: str, port: int) -> str | None:
    """Detect an existing listener; never kill it and never change the port."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError:
        return "PORT_IN_USE"
    finally:
        probe.close()
    return None


def _decode_binding(item: dict):
    from ak_trading.infiny_execution import contracts as c
    packet = {"schema_version": getattr(c.Binding, "schema", c.SCHEMA_VERSION),
              "type": "Binding", "data": item}
    return c.decode(json.dumps(packet), c.Binding)


def _decode_manifest(manifest_path: Path, lock_path: Path):
    from ak_trading.infiny_execution import contracts as c
    from ak_trading.infiny_execution.native_evidence import OperatorRegistrationManifest
    text = manifest_path.read_text(encoding="utf-8")
    manifest = c.decode(text, OperatorRegistrationManifest, schema_version=c.V2_SCHEMA_VERSION)
    if _sha256_file(lock_path) != manifest.contract_lock_sha256:
        raise ValueError("CONTRACT_LOCK_MISMATCH")
    for label, path, expected in (
        ("record", Path(manifest.record_path), manifest.record_sha256),
        ("authorization_artifact", Path(manifest.authorization_artifact_path),
         manifest.authorization_artifact_sha256),
    ):
        if not path.is_file() or _sha256_file(path) != expected:
            raise ValueError(f"{label.upper()}_HASH_MISMATCH")
    for artifact in manifest.artifacts:
        path = Path(artifact.path)
        if not path.is_file() or _sha256_file(path) != artifact.sha256:
            raise ValueError("ARTIFACT_HASH_MISMATCH")
    return manifest


def _record_types() -> dict[str, type]:
    from ak_trading.infiny_execution import native_contracts as nc
    from ak_trading.infiny_execution.native_evidence import (
        ClientThreadEvidence, NativeBaselineEvidence, NativeRouteEvidence,
        NativeSettlementEvidence,
    )
    return {
        "NativeBaselineEvidence": NativeBaselineEvidence,
        "NativeRouteEvidence": NativeRouteEvidence,
        "NativeSettlementEvidence": NativeSettlementEvidence,
        "ClientThreadEvidence": ClientThreadEvidence,
        "NativeAuthorization": nc.NativeAuthorization,
        "NativePilotPermit": nc.NativePilotPermit,
    }


def _decode_record(path: Path, kind: str):
    from ak_trading.infiny_execution import contracts as c
    expected = _record_types()[kind]
    return c.decode(path.read_text(encoding="utf-8"), expected,
                    schema_version=getattr(expected, "schema", c.V2_SCHEMA_VERSION))


def _register(args: argparse.Namespace) -> int:
    from ak_trading.infiny_execution.client_service import ProductionServiceRegistry
    from ak_trading.infiny_execution.ledger import Ledger, LedgerError
    from ak_trading.infiny_execution.native_evidence import OperatorRecordType

    if not args.journal.exists() and not args.initialize:
        return _fail("JOURNAL_MISSING_USE_INITIALIZE")
    if args.journal.exists() and args.initialize:
        return _fail("JOURNAL_EXISTS")
    if not args.manifest.is_file():
        return _fail("MANIFEST_MISSING")
    category = RECORD_CATEGORIES[args.command]
    try:
        manifest = _decode_manifest(args.manifest, args.contract_lock)
    except ValueError as exc:
        return _fail(str(exc))
    except Exception:
        return _fail("MANIFEST_INVALID")
    record_type = manifest.record_type.value if isinstance(manifest.record_type,
                                                           OperatorRecordType) \
        else str(manifest.record_type)
    if isinstance(category, str):
        if record_type != category:
            return _fail("RECORD_TYPE_MISMATCH")
    elif record_type not in category:
        return _fail("RECORD_TYPE_MISMATCH")
    if record_type != "ActorRegistration" and args.registry_dir is None:
        return _fail("REGISTRY_DIR_REQUIRED")
    expected_revision = getattr(args, "expected_registration_revision", None)
    if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 1):
        return _fail("EXPECTED_REVISION_INVALID")
    registry = ProductionServiceRegistry(args.registry_dir) \
        if record_type != "ActorRegistration" else None
    journal = None
    try:
        with JournalRuntimeLock(args.journal):
            try:
                journal = Ledger(args.journal, schema="v2")
            except Exception:
                return _fail("JOURNAL_OPEN_FAILED")
            try:
                with journal.maintenance():
                    journal.register_operator_manifest(manifest)
                    if record_type == "ActorRegistration":
                        payload = json.loads(Path(manifest.record_path).read_text(encoding="utf-8"))
                        bindings = tuple(_decode_binding(item) for item in payload["bindings"])
                        revision = journal.register_actor(
                            payload["actor_id"], payload["account_key"], bindings,
                            operator_action_id=manifest.operator_action_id,
                            manifest_sha256=_sha256_file(args.manifest),
                            contract_lock_sha256=manifest.contract_lock_sha256,
                            expected_registration_revision=expected_revision)
                        # operation receipt: expected/actual revision + manifest hash
                        print(_status_line({
                            "event": "registered", "record_type": record_type,
                            "actor_id": payload["actor_id"],
                            "registration_revision": revision,
                            "expected_registration_revision": expected_revision,
                            "manifest_sha256": _sha256_file(args.manifest)}))
                        return 0
                    record = _decode_record(Path(manifest.record_path), record_type)
                    if record_type == "NativeBaselineEvidence":
                        registry.register_baseline(record)
                    elif record_type == "NativeRouteEvidence":
                        registry.register_route(record)
                        journal.register_native_route(record, evidence_registry=registry)
                    elif record_type == "NativeSettlementEvidence":
                        registry.register_settlement(record)
                        journal.register_settlement_evidence(record, evidence_registry=registry)
                    elif record_type == "ClientThreadEvidence":
                        registry.register_thread(record)
                    elif record_type == "NativeAuthorization":
                        registry.register_authorization(record)
                    elif record_type == "NativePilotPermit":
                        registry.register_permit(record)
                    else:  # pragma: no cover - guarded by category check
                        return _fail("RECORD_TYPE_MISMATCH")
                    print(_status_line({"event": "registered", "record_type": record_type,
                                        "registration_id": manifest.registration_id}))
                    return 0
            except LedgerError as exc:
                return _fail("REGISTRATION_" + exc.code.name, detail="|".join(exc.blockers))
            except Exception as exc:
                reason = "SYNTHETIC_REFUSED" if "SYNTHETIC" in str(exc) else "REGISTRATION_INVALID"
                return _fail(reason, detail=type(exc).__name__)
            finally:
                journal.close()
    except JournalRuntimeLock.Busy:
        return _fail("JOURNAL_RUNTIME_BUSY")


def _serve(args: argparse.Namespace) -> int:
    invalid = _validate_serve(args)
    if invalid is not None:
        return _fail(invalid)
    port_error = _probe_port(DEFAULT_HOST, args.port)
    if port_error is not None:
        return _fail(port_error)
    if args.dry_run:
        # Superseded side-effectful dry-run (RULING 05): no journal writes,
        # no epoch, no tokens, no SDK imports, no observer contact.
        print(_status_line({"event": "execution_dry_run", "host": DEFAULT_HOST,
                            "port": args.port, "journal": str(args.journal),
                            "audit_log": str(args.audit_log),
                            "pairing_dir": str(args.pairing_dir),
                            "registry_dir": str(args.registry_dir),
                            "execution_enabled": False, "native": "disabled"}))
        return 0

    # The only application imports, deliberately after CLI validation.
    from ak_trading.infiny_execution import client_service as client_service_module
    from ak_trading.infiny_execution.client_service import (
        SERVICE_VERSION, ExecutionService, N4BackedOrchestrator,
        ProductionServiceRegistry, build_execution_server,
        generate_pairing_token, load_arming_prerequisites,
        secure_pairing_directory, serve_forever, write_pairing_file,
    )
    from ak_trading.infiny_execution.ledger import Ledger

    journal = None
    service = None
    server = None
    try:
        with JournalRuntimeLock(args.journal):
            # serve holds the runtime lock for its whole process lifetime:
            # the shared close-finally below runs INSIDE the with, so the
            # lock is released only after service and journal have closed
            try:
                try:
                    journal = Ledger(args.journal, schema="v2")
                except Exception:
                    return _fail("JOURNAL_OPEN_FAILED")
                registry = ProductionServiceRegistry(args.registry_dir)
                actors = journal.actors()
                pairing_tokens: dict[str, str] = {}
                pairing_sessions: dict[str, str | None] = {}
                for record in actors:
                    for binding in record.bindings:
                        if binding.instance_id in pairing_tokens:
                            return _fail("DUPLICATE_INSTANCE",
                                         detail=binding.instance_id)
                        pairing_tokens[binding.instance_id] = generate_pairing_token()
                        # normal actors bind the token to their registered
                        # Binding session (RULING 02 R2.2)
                        pairing_sessions[binding.instance_id] = binding.session_epoch
                if not pairing_tokens:
                    # No registered actor: a process-level token serves transport
                    # diagnostics only; every business route stays ACTOR_UNREGISTERED
                    # until an actor is registered (RULING 05/07).
                    pairing_tokens["unregistered"] = generate_pairing_token()
                    pairing_sessions["unregistered"] = None
                # RULING 03 preflight: establish and verify the private DACL of
                # the pairing directory BEFORE any secret byte is written
                # (RULING 02 R2.4); a failed run directory is never reused.
                try:
                    secure_pairing_directory(args.pairing_dir)
                except OSError:
                    return _fail("PAIRING_DIR_ACL_REJECTED")
                contract_lock_sha256 = _sha256_file(args.contract_lock)
                # ARM-LM-S (NE1-ARB-ARMING-001 RULING 03 AR3.3): optional
                # non-secret S4 arming anchor. Metadata wiring only - the
                # bytes are fixed and strictly verified once at startup; any
                # omission/failure keeps the disabled service running with
                # production arming BLOCKED (reported on stderr, never fatal).
                arming_prerequisites = None
                if args.arming_readiness is not None:
                    try:
                        arming_prerequisites = load_arming_prerequisites(
                            args.arming_readiness,
                            contract_lock_sha256=contract_lock_sha256,
                            service_file_sha256=_sha256_file(
                                Path(client_service_module.__file__)),
                            now=datetime.now(timezone.utc))
                    except (OSError, ValueError) as exc:
                        print(json.dumps(
                            {"event": "arming_prerequisites_rejected",
                             "reason": str(exc)},
                            ensure_ascii=True, separators=(",", ":")),
                            file=sys.stderr, flush=True)
                service = ExecutionService(
                    journal,
                    orchestrator=N4BackedOrchestrator(journal,
                                                      evidence_registry=registry),
                    audit_log=args.audit_log, tokens=pairing_tokens,
                    evidence_registry=registry,
                    arming_prerequisites=arming_prerequisites)
                instance_ids = []
                try:
                    for instance_id, token in pairing_tokens.items():
                        target = args.pairing_dir / (instance_id + ".token")
                        if target.exists():
                            return _fail("PAIRING_FILE_EXISTS", detail=str(target))
                        write_pairing_file(
                            target, instance_id, token,
                            service_instance_id=service.instance_id,
                            service_epoch=service.service_epoch,
                            session_epoch=pairing_sessions[instance_id],
                            contract_lock_sha256=contract_lock_sha256)
                        if instance_id != "unregistered":
                            instance_ids.append(instance_id)
                except FileExistsError:
                    return _fail("PAIRING_FILE_EXISTS", detail=str(args.pairing_dir))
                except ValueError:
                    return _fail("PAIRING_FILE_INVALID")
                except OSError:
                    return _fail("PAIRING_FILE_ACL_REJECTED")
                # every token is durably private; establish the listener, then
                # (and only then) emit the non-secret startup line the main
                # session saves verbatim as service-start.json
                server = build_execution_server(host=DEFAULT_HOST, port=args.port,
                                                service=service,
                                                max_workers=args.max_workers)
                # AR3.3: exactly one new top-level field - null, or the strict
                # non-secret anchor object; never an execution enablement
                arming_prerequisites_line = None if arming_prerequisites is None else {
                    "prerequisites_id": arming_prerequisites.prerequisites_id,
                    "prerequisites_sha256": arming_prerequisites.prerequisites_sha256,
                    "client_file_sha256": arming_prerequisites.client_file_sha256,
                }
                print(_status_line({
                    "event": "execution_started", "host": DEFAULT_HOST,
                    "port": server.server_address[1],
                    "service_epoch": service.service_epoch,
                    "service_version": SERVICE_VERSION,
                    "instance_id": service.instance_id,
                    "execution_enabled": False,
                    "observer_only": True,
                    "observer_port_untouched": OBSERVER_PORT,
                    "actors": [record.actor_id for record in actors],
                    "instances": sorted(instance_ids),
                    "audit_log": str(args.audit_log),
                    "contract_lock_sha256": contract_lock_sha256,
                    "arming_prerequisites": arming_prerequisites_line,
                    "pairing_dir": str(args.pairing_dir),
                    "pairings": [
                        {"instance_id": instance_id,
                         "session_epoch": pairing_sessions[instance_id],
                         "token_file": str(args.pairing_dir / (instance_id + ".token")),
                         "diagnostic_only": instance_id == "unregistered"}
                        for instance_id in sorted(pairing_tokens)],
                }), flush=True)
                serve_forever(server)
            finally:
                if server is not None:
                    try:
                        server.server_close()
                    except OSError:
                        pass
                if service is not None:
                    service.close()
                if journal is not None:
                    journal.close()
    except JournalRuntimeLock.Busy:
        return _fail("JOURNAL_RUNTIME_BUSY")
    except KeyboardInterrupt:
        return 130
    except OSError:
        return _fail("PORT_IN_USE")
    except Exception:
        return _fail("EXECUTION_STARTUP_FAILED")
    return 0


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    return _register(args)


if __name__ == "__main__":
    raise SystemExit(main())
