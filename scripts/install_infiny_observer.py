"""Install and remove the separate, inert InfiniTrader observer strategy."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import secrets
import tempfile

ROOT = Path(__file__).resolve().parents[1]
_OBSERVER_NAME = "AKObserverBridge.py"
_OPERATION = "exclusive_observer_install_v2"
_MARKER_PREFIX = "# AK_OBSERVER_INSTALL_ID="


def _safe_local_path(value: Path, *, allow_missing: bool = True) -> Path:
    raw = str(value)
    windows = PureWindowsPath(raw)
    if not raw or windows.drive.startswith("\\\\"):
        raise ValueError("local path required")
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
    reserved.update(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789¹²³")
    for part in windows.parts:
        if part in (windows.anchor, ".", ".."):
            continue
        stem = part.split(".", 1)[0].rstrip(" ").upper()
        if (part.rstrip(" .") != part or stem in reserved
                or any(ord(char) < 32 or ord(char) == 127 or char in '<>:"|?*' for char in part)):
            raise ValueError("reserved or invalid local path")
    absolute = Path(value).absolute()
    for ancestor in reversed((absolute, *absolute.parents)):
        if ancestor.is_symlink() or (hasattr(ancestor, "is_junction") and ancestor.is_junction()):
            raise ValueError("path cannot traverse a link or junction")
    if not allow_missing and not absolute.exists():
        raise ValueError("path does not exist")
    return absolute


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _atomic_create(path: Path, payload: bytes) -> None:
    """Create a new file without replacing a concurrent or existing file."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _validate_template(raw: bytes) -> bytes:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("observer template must be UTF-8") from exc
    if text.count("class AKObserverBridge") != 1:
        raise ValueError("observer template must define AKObserverBridge exactly once")
    for marker in ("def send_order", "def cancel_order", "def make_order_req", "def auto_close_position"):
        if text.count(marker) != 1:
            raise ValueError("observer template lacks one inert order guard")
    if "enable_trading" not in text or "self.trading = False" not in text:
        raise ValueError("observer template lacks forced observer mode")
    old_url = "http://127.0.0.1:17777/analyze"
    new_url = "http://127.0.0.1:17778/analyze"
    if text.count(old_url) == 1 and text.count(new_url) == 0:
        text = text.replace(old_url, new_url, 1)
    elif text.count(old_url) != 0 or text.count(new_url) != 1:
        raise ValueError("observer template must contain one approved loopback endpoint")
    return text.encode("utf-8")


def deploy_observer(strategy_directory: Path, receipt: Path) -> dict:
    directory = _safe_local_path(Path(strategy_directory), allow_missing=False)
    if not directory.is_dir():
        raise ValueError("PythonGO self_strategy directory does not exist")
    receipt_path = _safe_local_path(Path(receipt))
    source_path = _safe_local_path(ROOT / "integrations/infiny/AIBridge.py", allow_missing=False)
    target = directory / _OBSERVER_NAME
    if target.exists() or target.is_symlink() or receipt_path.exists() or receipt_path.is_symlink():
        raise ValueError("observer target or receipt already exists")

    source_bytes = source_path.read_bytes()
    payload_without_marker = _validate_template(source_bytes)
    install_id = secrets.token_hex(16)
    payload = (f"{_MARKER_PREFIX}{install_id}\n".encode("ascii") + payload_without_marker)
    compile(payload, _OBSERVER_NAME, "exec")
    target_hash = _hash(payload)
    record = {
        "schema_version": 2,
        "operation": _OPERATION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "install_id": install_id,
        "install_root": str(directory),
        "target_relative": _OBSERVER_NAME,
        "target_sha256": target_hash,
        "source_relative": str(source_path.relative_to(ROOT)).replace("\\", "/"),
        "source_sha256": _hash(source_bytes),
        "api_url": "http://127.0.0.1:17778/analyze",
        "execution_enabled": False,
    }
    try:
        _atomic_create(target, payload)
        if _hash(target.read_bytes()) != target_hash:
            raise OSError("installed observer hash mismatch")
        _atomic_create(receipt_path, _canonical_json(record))
    except BaseException:
        try:
            if target.is_file() and not target.is_symlink() and _hash(target.read_bytes()) == target_hash:
                target.unlink()
        except OSError:
            pass
        raise
    return {**record, "target": str(target), "receipt": str(receipt_path)}


def remove_observer(receipt: Path, strategy_directory: Path) -> dict:
    receipt_path = _safe_local_path(Path(receipt), allow_missing=False)
    if not receipt_path.is_file():
        raise ValueError("observer receipt must be a regular file")
    directory = _safe_local_path(Path(strategy_directory), allow_missing=False)
    if not directory.is_dir():
        raise ValueError("strategy directory does not exist")
    raw = receipt_path.read_bytes()
    if len(raw) > 32 * 1024:
        raise ValueError("observer receipt is too large")
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid observer receipt") from exc
    if not isinstance(record, dict) or record.get("schema_version") != 2 or record.get("operation") != _OPERATION:
        raise ValueError("invalid observer receipt")
    if record.get("execution_enabled") is not False or record.get("target_relative") != _OBSERVER_NAME:
        raise ValueError("receipt is not an observer installation")
    if record.get("install_root") != str(directory) or not re.fullmatch(r"[0-9a-f]{32}", str(record.get("install_id", ""))):
        raise ValueError("receipt is not bound to this strategy directory")
    target = directory / _OBSERVER_NAME
    if target.is_symlink() or not target.is_file():
        raise ValueError("installed observer is missing or linked")
    expected_marker = f"{_MARKER_PREFIX}{record['install_id']}\n".encode("ascii")
    before = target.stat()
    with target.open("rb") as handle:
        payload = handle.read()
    if not payload.startswith(expected_marker) or _hash(payload) != record.get("target_sha256"):
        raise ValueError("installed observer changed; refusing to remove")
    after = target.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("installed observer changed during removal")
    target.unlink()
    return {"status": "removed", "target": str(target), "receipt": str(receipt_path), "receipt_retained": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    install = sub.add_parser("install")
    install.add_argument("--strategy-directory", type=Path, required=True)
    install.add_argument("--receipt", type=Path, required=True)
    remove = sub.add_parser("remove")
    remove.add_argument("--receipt", type=Path, required=True)
    remove.add_argument("--strategy-directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "install":
            result = deploy_observer(args.strategy_directory, args.receipt)
        else:
            result = remove_observer(args.receipt, args.strategy_directory)
    except (OSError, ValueError, KeyError, SyntaxError, TypeError) as exc:
        print(json.dumps({"status": "rejected", "error_type": type(exc).__name__}))
        return 2
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
