"""Read-only access to registered research factor definitions, not factor values."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY = ROOT / "config" / "factor_library.json"
MAX_LIMIT = 1000
_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_.]{0,79}")
_DIGEST = re.compile(r"[a-f0-9]{64}")
_TABLES = (
    "products", "factors", "product_factor_map", "product_family_map",
    "product_index", "raw_metric_requirements", "data_dictionary",
    "operators_parameters", "sources", "spread_recipes", "starter_factors",
    "validation_checklist", "raw_observations", "factor_values",
)


class FactorLibraryError(ValueError):
    """The catalog, its registration, or the requested filter is invalid."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _local_file(root: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise FactorLibraryError("catalog paths must be relative local paths")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(p in {".", ".."} or p.startswith(".env") for p in relative.parts):
        raise FactorLibraryError("catalog path leaves the registered directory")
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise FactorLibraryError("catalog files must not be symbolic links")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise FactorLibraryError("registered catalog file is missing or outside its directory")
    return resolved


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value.upper()):
        raise FactorLibraryError(f"{name} must be a catalog ID")
    return value.upper()


def _text(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 120 or any(ord(c) < 32 for c in value):
        raise FactorLibraryError(f"{name} must contain 1..120 printable characters")
    return value


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_LIMIT:
        raise FactorLibraryError(f"limit must be an integer in 1..{MAX_LIMIT}")
    return value


class FactorLibrary:
    def __init__(self, registry_path: Path = DEFAULT_REGISTRY, *, project_root: Path = ROOT):
        try:
            registry = json.loads(Path(registry_path).read_text(encoding="utf-8"))
            if registry["schema_version"] != "ak_factor_library_registry_v1":
                raise FactorLibraryError("unsupported factor library registry version")
            catalogs = registry["catalogs"]
            selected = [c for c in catalogs if c["catalog_id"] == registry["active_catalog"]]
            if len(selected) != 1:
                raise FactorLibraryError("active catalog must identify exactly one registration")
            self._entry = selected[0]
            self.catalog_id = self._entry["catalog_id"]
            self._archive = _local_file(project_root, self._entry["archive_path"])
            self._db = _local_file(project_root, self._entry["sqlite_path"])
            self._checksums = _local_file(project_root, self._entry["checksums_path"])
            self._source = self._checksums.parent
            for key in ("archive_sha256", "sqlite_sha256", "checksums_sha256"):
                if not _DIGEST.fullmatch(self._entry[key]):
                    raise FactorLibraryError("invalid registered SHA-256 digest")
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise FactorLibraryError("malformed factor library registration") from exc
        self._check_digest(self._checksums, "checksums_sha256")
        self._check_digest(self._db, "sqlite_sha256")

    def _check_digest(self, path: Path, key: str) -> None:
        if _sha256(path) != self._entry[key]:
            raise FactorLibraryError(f"catalog integrity mismatch: {path.name}")

    @contextmanager
    def _connection(self):
        self._check_digest(self._db, "sqlite_sha256")
        # No WAL pragma here by design: this connection is mode=ro/query_only and
        # the catalog bytes are digest-pinned, so a journal-mode switch would raise
        # (read-only) and any byte change would break the registered sha256 check.
        # A longer busy timeout still covers transient external locks.
        conn = sqlite3.connect(self._db.as_uri() + "?mode=ro", uri=True, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA trusted_schema=OFF")
            expires = time.monotonic() + 5
            conn.set_progress_handler(lambda: int(time.monotonic() > expires), 10000)
            yield conn
        except sqlite3.Error as exc:
            raise FactorLibraryError("factor catalog query failed") from exc
        finally:
            conn.close()

    def _metadata(self) -> dict[str, Any]:
        return {
            "catalog_id": self.catalog_id,
            "catalog_version": self._entry["version"],
            "as_of": self._entry["as_of"],
            "status": "research_definitions_only",
            "calculator_status": "not_integrated",
            "historical_factor_values": False,
            "profitability_validated": False,
        }

    @staticmethod
    def _product(conn, product: str | None) -> str | None:
        if product is None:
            return None
        product = _identifier(product, "product")
        if conn.execute("SELECT 1 FROM products WHERE product_id=?", (product,)).fetchone() is None:
            raise FactorLibraryError("unknown product ID; use products to discover research IDs")
        return product

    def _page(self, conn, sql: str, params: list, limit: int) -> dict:
        rows = [dict(row) for row in conn.execute(sql + " LIMIT ?", [*params, limit + 1])]
        return {**self._metadata(), "rows": rows[:limit], "row_count": min(len(rows), limit),
                "truncated": len(rows) > limit, "limit": limit}

    def info(self) -> dict:
        with self._connection() as conn:
            counts = {table: conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                      for table in _TABLES}
            families = [dict(row) for row in conn.execute(
                "SELECT family, count(*) AS factor_count FROM factors GROUP BY family ORDER BY family")]
            roles = [dict(row) for row in conn.execute(
                "SELECT role, count(*) AS factor_count FROM factors GROUP BY role ORDER BY role")]
        return {**self._metadata(), "counts": counts, "families": families, "roles": roles,
                "executable_registry": "ak_trading.probability.factors.factor_registry",
                "notes": self._entry["notes"]}

    def products(self, *, limit: int = 100) -> dict:
        limit = _limit(limit)
        with self._connection() as conn:
            return self._page(conn, "SELECT * FROM products ORDER BY product_id", [], limit)

    def search(self, *, product: str | None = None, family: str | None = None,
               keyword: str | None = None, priority: str | None = None,
               limit: int = 30) -> dict:
        limit = _limit(limit)
        family, keyword = _text(family, "family"), _text(keyword, "keyword")
        if priority is not None and priority not in {"P0", "P1", "P2"}:
            raise FactorLibraryError("priority must be P0, P1 or P2 (implementation priority, not expected return)")
        with self._connection() as conn:
            product = self._product(conn, product)
            select = "SELECT f.*"
            clauses, params = [], []
            if product:
                select += ", m.product_id, m.application, m.condition FROM factors f JOIN product_factor_map m ON m.factor_id=f.factor_id"
                clauses.append("m.product_id=?")
                params.append(product)
            else:
                select += " FROM factors f"
            if family:
                clauses.append("instr(f.family,?)>0")
                params.append(family)
            if keyword:
                clauses.append("(instr(f.name,?)>0 OR instr(lower(f.factor_id),lower(?))>0)")
                params.extend([keyword, keyword])
            if priority:
                clauses.append("f.priority=?")
                params.append(priority)
            sql = select + (" WHERE " + " AND ".join(clauses) if clauses else "")
            return self._page(conn, sql + " ORDER BY f.factor_id", params, limit)

    def factor(self, factor_id: str) -> dict:
        factor_id = _identifier(factor_id, "factor_id")
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM factors WHERE factor_id=?", (factor_id,)).fetchone()
            if row is None:
                raise FactorLibraryError("unknown factor ID")
            definition = dict(row)
            mappings = [dict(row) for row in conn.execute(
                "SELECT product_id, application, condition FROM product_factor_map WHERE factor_id=? ORDER BY product_id",
                (factor_id,))]
            source_ids = [value.strip() for value in definition["source_ids"].split(",") if value.strip()]
            sources = []
            for source_id in source_ids:
                source = conn.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
                if source is not None:
                    sources.append(dict(source))
        return {**self._metadata(), "factor": definition, "applications": mappings, "sources": sources}

    def starters(self, *, product: str | None = None, limit: int = 30) -> dict:
        limit = _limit(limit)
        with self._connection() as conn:
            product = self._product(conn, product)
            sql = ('SELECT s.*, f.formula, f.inputs, f.parameters, f.available_at_rule, '
                   'f.implementation, f.backtest')
            if product:
                sql += ', m.product_id, m.application, m.condition'
            sql += ' FROM starter_factors s JOIN factors f ON s.factor_id=f.factor_id'
            params = []
            if product:
                sql += " JOIN product_factor_map m ON m.factor_id=f.factor_id WHERE m.product_id=?"
                params.append(product)
            return self._page(conn, sql + ' ORDER BY s."order", s.factor_id', params, limit)

    def verify(self) -> dict:
        self._check_digest(self._archive, "archive_sha256")
        self._check_digest(self._checksums, "checksums_sha256")
        verified = []
        for line in self._checksums.read_text(encoding="utf-8-sig").splitlines():
            digest, name = line.split("  ", 1)
            path = _local_file(self._source, name)
            if not _DIGEST.fullmatch(digest) or _sha256(path) != digest or name in verified:
                raise FactorLibraryError("source file checksum mismatch or duplicate entry")
            verified.append(name)
        source_files = {p.relative_to(self._source).as_posix() for p in self._source.rglob("*") if p.is_file()}
        if source_files != set(verified) | {"SHA256SUMS.txt"}:
            raise FactorLibraryError("source directory has missing or unexpected files")
        with self._connection() as conn:
            if [row[0] for row in conn.execute("PRAGMA integrity_check")] != ["ok"]:
                raise FactorLibraryError("SQLite integrity check failed")
            for table, columns in (("products", "product_id"), ("factors", "factor_id"),
                                   ("sources", "source_id"), ("product_factor_map", "product_id,factor_id")):
                duplicates = conn.execute(
                    f'SELECT count(*) FROM (SELECT {columns} FROM "{table}" GROUP BY {columns} HAVING count(*)>1)'
                ).fetchone()[0]
                if duplicates:
                    raise FactorLibraryError(f"duplicate catalog identity in {table}")
            orphans = conn.execute(
                "SELECT count(*) FROM product_factor_map m LEFT JOIN factors f ON m.factor_id=f.factor_id "
                "LEFT JOIN products p ON m.product_id=p.product_id WHERE f.factor_id IS NULL OR p.product_id IS NULL"
            ).fetchone()[0]
            if orphans:
                raise FactorLibraryError("unresolved factor or product references")
        result = self.info()
        if result["counts"] != self._entry["table_counts"]:
            raise FactorLibraryError("catalog counts do not match registration")
        return {**result, "validation": "structure_and_integrity_pass",
                "source_files_verified": len(verified), "archive_verified": True,
                "formula_validation": "not_mathematically_or_empirically_validated"}
