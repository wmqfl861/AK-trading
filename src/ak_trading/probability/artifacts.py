"""Strict frozen input capture and exclusive deterministic research publication."""
from __future__ import annotations

import csv
from dataclasses import asdict, is_dataclass
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path, PureWindowsPath
import re
from typing import Mapping

from ak_trading.futures.io import BARS_CSV_COLUMNS, BUNDLE_SCHEMA_VERSION, InputBundle, bars_from_rows

ROOT = Path(__file__).resolve().parents[3]


def _check_local_name(path: Path) -> None:
    value = str(path)
    windows = PureWindowsPath(value)
    if not value or windows.drive.startswith("\\\\"):
        raise ValueError("research requires a local filesystem path")
    reserved = {"NUL", "CON", "PRN", "AUX", "CONIN$", "CONOUT$", "CLOCK$"}
    reserved.update(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789¹²³")
    for part in windows.parts:
        if part == windows.anchor or part in (".", ".."):
            continue
        name = part.lower()
        if name == ".env" or name.startswith(".env.") or name.endswith(".env"):
            raise ValueError("research does not read credential files")
        if (part.rstrip(" .") != part or part.split(".", 1)[0].rstrip(" ").upper() in reserved
                or any(ord(char) < 32 or ord(char) == 127 or char in '<>:"|?*' for char in part)):
            raise ValueError("reserved or invalid local path")


def safe_path(path: Path) -> Path:
    path = Path(path)
    _check_local_name(path)
    absolute = path.absolute()
    for ancestor in reversed((absolute, *absolute.parents)):
        if ancestor.is_symlink() or (hasattr(ancestor, "is_junction") and ancestor.is_junction()):
            raise ValueError("research paths cannot traverse links or junctions")
    resolved = path.resolve()
    _check_local_name(resolved)
    return resolved


def strict_json(payload: bytes, *, decimal_floats: bool = False) -> object:
    def pairs(values):
        out = {}
        for key, value in values:
            if key in out:
                raise ValueError("duplicate JSON key")
            out[key] = value
        return out
    def constant(value):
        raise ValueError("nonfinite JSON constant")
    def number(value):
        import math
        parsed = Decimal(value) if decimal_floats else float(value)
        if not (parsed.is_finite() if isinstance(parsed, Decimal) else math.isfinite(parsed)):
            raise ValueError("nonfinite JSON numeric value")
        return parsed
    return json.loads(payload.decode("utf-8-sig"), object_pairs_hook=pairs, parse_constant=constant,
                      parse_float=number)


def jsonable(value):
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("nonfinite research value")
        return format(value, "f")
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [jsonable(item) for item in value]
    return value


def json_bytes(value) -> bytes:
    return (json.dumps(jsonable(value), sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def load_frozen_input(path: Path) -> tuple[InputBundle, str]:
    source = safe_path(path)
    if not source.is_dir():
        raise ValueError("input directory missing")
    manifest_path = safe_path(source / "manifest.json")
    if manifest_path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("manifest too large")
    raw_manifest = manifest_path.read_bytes()
    manifest = strict_json(raw_manifest)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION or manifest.get("status") != "complete":
        raise ValueError("complete futures_bundle_v1 required")
    hashes = manifest.get("content_hashes")
    core = ("bars.csv", "contracts.json", "metadata.json", "availability.json")
    if not isinstance(hashes, dict) or not set(core) <= set(hashes):
        raise ValueError("input manifest missing core payload hashes")
    captured = {}
    for name, expected in hashes.items():
        if not isinstance(name, str) or name in {"", ".", "..", "manifest.json"} or any(c in name for c in "/\\:") or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("invalid input hash entry")
        target = safe_path(source / name)
        if target.parent != source or target.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("input payload outside bundle or over 64 MiB")
        raw = target.read_bytes()
        if digest(raw) != expected:
            raise ValueError("input payload hash mismatch")
        if name in core:
            captured[name] = raw
    contracts = strict_json(captured["contracts.json"])
    metadata = strict_json(captured["metadata.json"])
    availability = strict_json(captured["availability.json"])
    if not isinstance(contracts, list) or not all(isinstance(row, dict) for row in contracts) or not isinstance(metadata, dict) or not isinstance(availability, dict):
        raise ValueError("invalid input JSON structure")
    rows = []
    try:
        reader = csv.DictReader(io.StringIO(captured["bars.csv"].decode("utf-8"), newline=""))
        if tuple(reader.fieldnames or ()) != BARS_CSV_COLUMNS:
            raise ValueError("unexpected bars header")
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("malformed bars row")
            rows.append(row)
            if len(rows) > 200000:
                raise ValueError("input exceeds 200000 rows")
    except csv.Error as exc:
        raise ValueError("invalid bars CSV") from exc
    try:
        bars = tuple(bars_from_rows(rows))
    except (ArithmeticError, ValueError, TypeError, OverflowError) as exc:
        raise ValueError("invalid numeric field in input bars") from exc
    return InputBundle(source, manifest, bars, tuple(contracts), metadata, availability), digest(raw_manifest)


def rows_csv(rows: list[dict], columns: list[str] | None = None) -> bytes:
    names = columns or sorted(set().union(*(row.keys() for row in rows)))
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=names, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        clean = {}
        for name in names:
            value = jsonable(row.get(name))
            clean[name] = "" if value is None else json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")) if isinstance(value, (list, dict)) else str(value)
        writer.writerow(clean)
    return stream.getvalue().encode("utf-8")


def validate_output(output: Path, source: Path, config: Path) -> Path:
    out = safe_path(output)
    if Path(output).is_symlink():
        raise ValueError("output cannot be a symlink")
    if out.exists():
        raise ValueError("output directory exists")
    for other in (safe_path(source), safe_path(config), ROOT / "src", ROOT / "scripts", ROOT / "tests", ROOT / "config"):
        if out == other or out in other.parents or other in out.parents:
            raise ValueError("output overlaps protected input/source")
    return out


def publish_result(output: Path, payloads: Mapping[str, bytes], provenance: Mapping[str, object]) -> None:
    output.mkdir(parents=True, exist_ok=False)
    written = {}
    try:
        for name, raw in payloads.items():
            if "/" in name or "\\" in name or name == "manifest.json":
                raise ValueError("invalid output payload name")
            with (output / name).open("xb") as handle:
                handle.write(raw)
            if digest((output / name).read_bytes()) != digest(raw):
                raise OSError("output hash mismatch")
            written[name] = digest(raw)
        marker = json_bytes(dict(provenance, status="complete", content_hashes=written))
        with (output / "manifest.json").open("xb") as handle:
            try:
                handle.write(marker)
                handle.flush()
            except BaseException:
                try:
                    handle.seek(0)
                    handle.truncate()
                    handle.write(json_bytes(dict(provenance, status="incomplete", content_hashes=written)))
                    handle.flush()
                except OSError:
                    pass
                raise
    except BaseException as exc:
        try:
            with (output / "manifest.json").open("xb") as handle:
                handle.write(json_bytes(dict(provenance, status="incomplete", error_type=type(exc).__name__, content_hashes=written)))
        except OSError:
            pass
        raise
