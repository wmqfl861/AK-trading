#!/usr/bin/env python3
"""NF10 static API reference generator (work order: docs/devplan-next-frontier-20260924-r1/work-orders/NF10-api-reference.md).

Contract (H10.1-H10.7), enforced by design of this file:

* Pure static analysis: the documented source files are read as bytes and parsed
  with ``ast.parse`` only. This script never imports, imports, evaluates or
  executes any documented module (no importlib/inspect/get_type_hints, no CLI
  ``--help`` probing, no evaluation of defaults/annotations/decorators).
  Annotations and default expressions are preserved as exact source text via
  ``ast.get_source_segment``.
* Bounded inputs: only the source files listed in the frozen surface JSON are
  read. Paths are validated (no ``..``, no absolute/backslash/UNC forms, no
  symlink/junction escape outside the declared source root) and every file's
  SHA-256 must match the frozen surface before parsing. Logical module names
  must be unique across entries (no cross-overlay shadowing).
* Bounded exports: literal ``__all__`` lists only; relative/absolute re-exports
  resolve only inside the active allowlist; static aliases link to their
  defining declaration instead of duplicating it; dynamic ``__all__``, alias
  cycles and cross-allowlist dependencies fail with explicit diagnostics.
* Determinism: page and inventory bytes depend only on the surface bytes and
  the generator version. Build ids, timestamps, paths and environment go to a
  separate receipt file. Output is written to a staging directory and renamed
  into a fresh ``<build-id>`` directory only after full success.
* Drift: ``check`` compares a baseline inventory with a fresh extraction and
  reports added/removed symbols, signature/default/annotation/export changes,
  doc-only changes and input hash drift; it refuses "same version" proof when
  baseline-recorded input hashes no longer match current bytes.
* Data boundary: no runtime config, credentials, .env, LLM files, databases,
  research outputs or network access. Only listed source files are read.

Exit codes: build -> 0 ok / 2 error; check -> 0 identical / 1 drift found / 2 error.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import platform
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

GENERATOR_NAME = "nf10-api-reference-generator"
GENERATOR_VERSION = "1.0.0"
INVENTORY_SCHEMA = "nf10-inventory/1"
BUILD_REPORT_SCHEMA = "nf10-build-report/1"
DRIFT_REPORT_SCHEMA = "nf10-drift-report/1"

STABILITY_LABELS = {
    "PACKAGE_EXPORT",
    "MODULE_API",
    "FROZEN_CONTRACT",
    "OPERATIONAL_INTERNAL",
}
EXPORT_MODES = {"explicit_symbols", "package_all", "module_all"}
PENDINGS_ALLOWED = {"PENDING_SOURCE_HANDOFF"}

SLUG_KEEP = re.compile(r"[^a-z0-9_\-]+")


class SurfaceError(Exception):
    """Diagnostic failure with a machine-readable code (never a stack trace)."""

    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def fail(code: str, message: str, details: dict | None = None) -> "SurfaceError":
    return SurfaceError(code, message, details)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_file_bytes(path: Path) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def read_json_file(path: Path) -> object:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def dump_json_canonical(obj: object) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def heading_slug(text: str) -> str:
    slug = text.replace("`", "").strip().lower()
    slug = slug.replace(".", "-").replace(" ", "-")
    slug = SLUG_KEEP.sub("", slug)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


def validate_rel_source_path(rel: str) -> list[str]:
    """Validate a forward-slash repo-relative source path; return its parts."""
    if not isinstance(rel, str) or not rel:
        raise fail("path_invalid", "source_path must be a non-empty string", {"value": repr(rel)})
    if "\\" in rel:
        raise fail("path_invalid", "source_path must use forward slashes", {"value": rel})
    if rel.startswith("/"):
        raise fail("path_escape", "absolute source_path rejected", {"value": rel})
    if re.match(r"^[A-Za-z]:", rel):
        raise fail("path_escape", "drive-letter source_path rejected", {"value": rel})
    if rel.startswith("//"):
        raise fail("path_escape", "UNC source_path rejected", {"value": rel})
    parts: list[str] = []
    for part in rel.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise fail("path_escape", "'..' component in source_path rejected", {"value": rel})
        if ":" in part:
            raise fail("path_invalid", "':' component in source_path rejected", {"value": rel})
        parts.append(part)
    if not parts:
        raise fail("path_invalid", "source_path has no real components", {"value": rel})
    return parts


def resolve_under_root(root: Path, rel: str) -> Path:
    validate_rel_source_path(rel)
    candidate = root / rel
    root_real = Path(os.path.realpath(root))
    candidate_real = Path(os.path.realpath(candidate))
    root_key = os.path.normcase(str(root_real))
    cand_key = os.path.normcase(str(candidate_real))
    if cand_key != root_key and not cand_key.startswith(root_key + os.sep):
        raise fail(
            "path_escape",
            "resolved source path escapes the declared source root (symlink/junction/..?)",
            {"root": str(root_real), "resolved": str(candidate_real)},
        )
    return candidate


# --------------------------------------------------------------------------
# per-module source model (static only)
# --------------------------------------------------------------------------

DYNAMIC = object()  # sentinel for a non-literal __all__ (never executed)


class ModuleSource:
    """Top-level binding table of one parsed module."""

    def __init__(self, module: str, source_path: str, text: str, tree: ast.Module, file_sha: str, file_size: int) -> None:
        self.module = module
        self.source_path = source_path
        self.text = text
        self.tree = tree
        self.file_sha = file_sha
        self.file_size = file_size
        self.defs: dict[str, ast.stmt] = {}
        self.constants: dict[str, ast.stmt] = {}
        self.aliases: dict[str, str] = {}
        self.import_from: dict[str, tuple[int, str | None, str]] = {}
        self.import_abs: dict[str, str] = {}
        self.all_names: list[str] | object | None = None
        self.all_node_lineno: int | None = None
        self._index()

    def _index(self) -> None:
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.defs[node.name] = node
            elif isinstance(node, ast.Assign):
                self._index_assign(node)
            elif isinstance(node, ast.AnnAssign):
                self._index_ann_assign(node)
            elif isinstance(node, ast.AugAssign):
                if isinstance(node.target, ast.Name) and node.target.id == "__all__":
                    self.all_names = DYNAMIC
                    self.all_node_lineno = node.lineno
            elif isinstance(node, ast.ImportFrom):
                self._index_import_from(node)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    bound = alias.asname or alias.name.split(".")[0]
                    self.import_abs[bound] = alias.name

    def _index_assign(self, node: ast.Assign) -> None:
        name_targets = [t for t in node.targets if isinstance(t, ast.Name)]
        if not name_targets:
            return
        if len(name_targets) == 1 and name_targets[0].id == "__all__":
            self._capture_all(node.value, node.lineno)
            return
        for target in name_targets:
            name = target.id
            if name == "__all__":
                self._capture_all(node.value, node.lineno)
                continue
            if isinstance(node.value, ast.Name):
                self.aliases[name] = node.value.id
            else:
                self.constants[name] = node

    def _index_ann_assign(self, node: ast.AnnAssign) -> None:
        if not isinstance(node.target, ast.Name):
            return
        name = node.target.id
        if name == "__all__":
            self._capture_all(node.value, node.lineno)
            return
        if isinstance(node.value, ast.Name):
            self.aliases[name] = node.value.id
        else:
            self.constants[name] = node

    def _capture_all(self, value: ast.expr | None, lineno: int) -> None:
        literal: list[str] | None = []
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            for element in value.elts:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    literal.append(element.value)
                else:
                    literal = None
                    break
        else:
            literal = None
        if literal is None:
            self.all_names = DYNAMIC
        else:
            if self.all_names is not None:
                # a second literal assignment makes the effective value ambiguous
                self.all_names = DYNAMIC
            else:
                self.all_names = literal
        self.all_node_lineno = lineno

    def _index_import_from(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == "*":
                continue
            bound = alias.asname or alias.name
            self.import_from[bound] = (node.level, node.module, alias.name)

    def resolve_import_module(self, bound_name: str, package_of_self: str | None) -> str | None:
        """Resolve the logical module an ImportFrom binding comes from."""
        if bound_name not in self.import_from:
            return None
        level, module, _origin = self.import_from[bound_name]
        if level == 0:
            return module
        base_parts = self.module.split(".")
        if self.source_path.endswith("__init__.py"):
            pkg_parts = base_parts
        else:
            pkg_parts = base_parts[:-1]
        if level - 1 > len(pkg_parts):
            return None
        base = pkg_parts[: len(pkg_parts) - (level - 1)] if level > 1 else pkg_parts
        if module:
            return ".".join(base + module.split("."))
        return ".".join(base)

def segment(text: str, node: ast.AST) -> str:
    seg = ast.get_source_segment(text, node)
    if seg is None:
        raise fail("internal", "source segment unavailable for node", {"node": type(node).__name__})
    return seg


def _docstring_line_ranges(node: ast.AST) -> list[tuple[int, int]]:
    """Line ranges of docstrings that belong to a def/class segment.

    Covers the node's own docstring and, for classes, the direct methods'
    docstrings, so that a doc-only edit never masquerades as a body change.
    """
    ranges: list[tuple[int, int]] = []

    def maybe(child: ast.AST) -> None:
        body = getattr(child, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            first = body[0]
            if first.lineno is not None and first.end_lineno is not None:
                ranges.append((first.lineno, first.end_lineno))

    maybe(node)
    if isinstance(node, ast.ClassDef):
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                maybe(stmt)
    return ranges


def segment_body_sha256(text: str, node: ast.AST) -> str:
    """SHA-256 of a def/class segment with docstring lines removed."""
    if node.lineno is None or node.end_lineno is None:  # pragma: no cover
        return sha256_bytes(b"")
    lines = text.splitlines()
    drop = set()
    for start, end in _docstring_line_ranges(node):
        drop.update(range(start, end + 1))
    kept = [lines[index] for index in range(node.lineno - 1, node.end_lineno) if index + 1 not in drop]
    return sha256_bytes("\n".join(kept).encode("utf-8"))


def dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        if base:
            return f"{base}.{node.attr}"
    return None


def one_line(text: str) -> str:
    return " ".join(part.strip() for part in text.strip().splitlines() if part.strip())


# --------------------------------------------------------------------------
# symbol records
# --------------------------------------------------------------------------


def extract_params(node: ast.FunctionDef | ast.AsyncFunctionDef, text: str) -> list[dict]:
    args = node.args
    params: list[dict] = []

    def add(name: ast.arg, kind: str, default: ast.expr | None) -> None:
        params.append(
            {
                "name": name.arg,
                "kind": kind,
                "annotation": one_line(segment(text, name.annotation)) if name.annotation else None,
                "default": one_line(segment(text, default)) if default is not None else None,
            }
        )

    positional = args.posonlyargs + args.args
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    for arg, default in zip(positional, defaults):
        limit = "positional-only" if args.posonlyargs and arg in args.posonlyargs else "positional-or-keyword"
        add(arg, limit, default)
    if args.vararg:
        add(args.vararg, "vararg", None)
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        add(arg, "keyword-only", default)
    if args.kwarg:
        add(args.kwarg, "kwarg", None)
    return params


def render_signature(name: str, params: list[dict], returns: str | None) -> str:
    parts: list[str] = []
    seen_kw_only_marker = False
    for param in params:
        if param["kind"] == "keyword-only" and not seen_kw_only_marker:
            parts.append("*")
            seen_kw_only_marker = True
        chunk = param["name"]
        if param["annotation"]:
            chunk += f": {param['annotation']}"
        if param["default"] is not None:
            chunk += f" = {param['default']}"
        parts.append(chunk)
    rendered = f"{name}({', '.join(parts)})"
    if returns:
        rendered += f" -> {returns}"
    return rendered


def function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef, text: str) -> dict:
    params = extract_params(node, text)
    returns = one_line(segment(text, node.returns)) if node.returns else None
    return {
        "kind": "async-function" if isinstance(node, ast.AsyncFunctionDef) else "function",
        "parameters": params,
        "returns": returns,
        "rendered": render_signature(node.name, params, returns),
        "decorators": [one_line(segment(text, dec)) for dec in node.decorator_list],
        "is_async": isinstance(node, ast.AsyncFunctionDef),
    }


def decorator_basename(dec: ast.expr) -> str | None:
    """Base name of a decorator, unwrapping a call like ``@dataclass(frozen=True)``."""
    target = dec.func if isinstance(dec, ast.Call) else dec
    dotted = dotted_name(target)
    return dotted.split(".")[-1] if dotted else None


def class_signature(node: ast.ClassDef, text: str) -> dict:
    decorators = [one_line(segment(text, dec)) for dec in node.decorator_list]
    is_dataclass = any(decorator_basename(dec) == "dataclass" for dec in node.decorator_list)
    fields: list[dict] = []
    attributes: list[dict] = []
    methods: list[dict] = []
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            annotation = one_line(segment(text, stmt.annotation)) if stmt.annotation else None
            default = one_line(segment(text, stmt.value)) if stmt.value is not None else None
            entry = {"name": stmt.target.id, "annotation": annotation, "default": default, "lineno": stmt.lineno}
            if "ClassVar" in (annotation or ""):
                attributes.append(entry)
            else:
                fields.append(entry)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    attributes.append(
                        {
                            "name": target.id,
                            "annotation": None,
                            "default": one_line(segment(text, stmt.value)),
                            "lineno": stmt.lineno,
                        }
                    )
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(stmt, clean=False)
            methods.append(
                {
                    "name": stmt.name,
                    "signature": function_signature(stmt, text),
                    "lineno": stmt.lineno,
                    "end_lineno": stmt.end_lineno,
                    "docstring_sha256": sha256_bytes(doc.encode("utf-8")) if doc is not None else None,
                    "documented": doc is not None,
                    "is_property": any(
                        decorator_basename(dec) in ("property", "classmethod", "staticmethod")
                        for dec in stmt.decorator_list
                    ),
                }
            )
    return {
        "kind": "class",
        "bases": [one_line(segment(text, base)) for base in node.bases],
        "keywords": [
            f"{kw.arg}={one_line(segment(text, kw.value))}" for kw in node.keywords if kw.arg
        ],
        "decorators": decorators,
        "dataclass": is_dataclass,
        "fields": fields,
        "class_attributes": attributes,
        "declared_members_only": True,
        "methods": methods,
    }


def docstring_info(node: ast.AST) -> tuple[str | None, str | None, bool]:
    doc = ast.get_docstring(node, clean=False)
    if doc is None:
        return None, None, False
    return doc, sha256_bytes(doc.encode("utf-8")), True


# --------------------------------------------------------------------------
# surface loading and validation (H10.1)
# --------------------------------------------------------------------------


def load_surface(surface_path: Path) -> tuple[dict, Path, str]:
    if not surface_path.is_file():
        raise fail("surface_missing", "surface file not found", {"path": str(surface_path)})
    surface_bytes = read_file_bytes(surface_path)
    try:
        surface = json.loads(surface_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise fail("surface_invalid", f"surface JSON unreadable: {exc}") from exc
    if not isinstance(surface, dict):
        raise fail("surface_invalid", "surface must be a JSON object")
    for key in ("surface_id", "source_root", "entries"):
        if key not in surface:
            raise fail("surface_invalid", f"surface missing required key '{key}'")
    source_root_value = surface["source_root"]
    if not isinstance(source_root_value, str) or not source_root_value:
        raise fail("surface_invalid", "source_root must be a non-empty string")
    root = Path(os.path.realpath(surface_path.parent / source_root_value))
    if not root.is_dir():
        raise fail("surface_invalid", "source_root does not resolve to a directory", {"root": str(root)})
    surface_sha = sha256_bytes(surface_bytes)

    seen_modules: dict[str, str] = {}
    for index, entry in enumerate(surface["entries"]):
        if not isinstance(entry, dict):
            raise fail("surface_invalid", f"entries[{index}] must be an object")
        for key in ("module", "source_path", "sha256", "stability", "export_mode"):
            if key not in entry:
                raise fail("surface_invalid", f"entries[{index}] missing required key '{key}'")
        module = entry["module"]
        if not isinstance(module, str) or not module:
            raise fail("surface_invalid", f"entries[{index}].module must be a non-empty string")
        if module in seen_modules:
            raise fail(
                "module_duplicate",
                "logical module listed by multiple entries (cross-overlay shadowing is forbidden)",
                {
                    "module": module,
                    "first_source_path": seen_modules[module],
                    "second_source_path": entry["source_path"],
                },
            )
        seen_modules[module] = entry["source_path"]
        if entry["stability"] not in STABILITY_LABELS:
            raise fail(
                "surface_invalid",
                f"entries[{index}].stability must be one of {sorted(STABILITY_LABELS)}",
                {"value": entry["stability"]},
            )
        if entry["export_mode"] not in EXPORT_MODES:
            raise fail(
                "surface_invalid",
                f"entries[{index}].export_mode must be one of {sorted(EXPORT_MODES)}",
                {"value": entry["export_mode"]},
            )
        if entry["export_mode"] == "explicit_symbols":
            symbols = entry.get("symbols")
            if not isinstance(symbols, list) or not symbols or not all(
                isinstance(item, str) and item for item in symbols
            ):
                raise fail(
                    "surface_invalid",
                    f"entries[{index}] with export_mode=explicit_symbols requires a non-empty symbols list",
                )
            if len(set(symbols)) != len(symbols):
                raise fail(
                    "surface_invalid",
                    f"entries[{index}].symbols contains duplicates",
                    {"symbols": symbols},
                )
    pending = surface.get("pending", [])
    if not isinstance(pending, list):
        raise fail("surface_invalid", "pending must be a list")
    for index, item in enumerate(pending):
        if not isinstance(item, dict) or "id" not in item or "reason" not in item:
            raise fail("surface_invalid", f"pending[{index}] must be an object with id and reason")
        if item["reason"] not in PENDINGS_ALLOWED:
            raise fail(
                "surface_invalid",
                f"pending[{index}].reason must be one of {sorted(PENDINGS_ALLOWED)}",
                {"value": item["reason"]},
            )
    return surface, root, surface_sha


def load_module_source(entry: dict, root: Path, modules: dict[str, ModuleSource]) -> ModuleSource:
    module = entry["module"]
    if module in modules:
        return modules[module]
    path = resolve_under_root(root, entry["source_path"])
    if not path.is_file():
        raise fail("source_missing", "listed source file does not exist", {"module": module, "path": entry["source_path"]})
    data = read_file_bytes(path)
    actual_sha = sha256_bytes(data)
    if actual_sha != entry["sha256"]:
        raise fail(
            "input_hash_mismatch",
            "current bytes of a listed source do not match the frozen surface hash; "
            "re-freeze the surface with a human adjudication record instead of guessing",
            {
                "module": module,
                "path": entry["source_path"],
                "frozen_sha256": entry["sha256"],
                "actual_sha256": actual_sha,
            },
        )
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise fail("parse_error", f"source is not valid UTF-8: {exc}", {"module": module}) from exc
    try:
        tree = ast.parse(text, filename=entry["source_path"])
    except SyntaxError as exc:
        raise fail(
            "parse_error",
            f"source does not parse: {exc}",
            {"module": module, "path": entry["source_path"], "lineno": exc.lineno},
        ) from exc
    source = ModuleSource(module, entry["source_path"], text, tree, actual_sha, len(data))
    modules[module] = source
    return source


# --------------------------------------------------------------------------
# symbol selection and alias/re-export resolution (H10.3, H10.4)
# --------------------------------------------------------------------------


def package_of(module: str, source_path: str) -> str | None:
    if source_path.endswith("__init__.py"):
        return module
    parts = module.split(".")
    return ".".join(parts[:-1]) if len(parts) > 1 else None


class Extraction:
    def __init__(self, surface: dict, root: Path, surface_sha: str) -> None:
        self.surface = surface
        self.root = root
        self.surface_sha = surface_sha
        self.modules: dict[str, ModuleSource] = {}
        self.entries_by_module: dict[str, dict] = {e["module"]: e for e in surface["entries"]}
        self.records: dict[str, dict] = {}
        self.exports: dict[str, list[dict]] = {}
        self.inputs: list[dict] = []
        self.selected: dict[str, set[str]] = {}

    # -- binding kinds -----------------------------------------------------
    def run(self) -> None:
        for entry in self.surface["entries"]:
            self._load_entry(entry)
        for entry in self.surface["entries"]:
            self._resolve_entry(entry)

    def _load_entry(self, entry: dict) -> None:
        module = entry["module"]
        source = load_module_source(entry, self.root, self.modules)
        self.inputs.append(
            {
                "module": module,
                "source_path": entry["source_path"],
                "sha256": source.file_sha,
                "bytes": source.file_size,
                "stability": entry["stability"],
                "export_mode": entry["export_mode"],
                "owner": entry.get("owner"),
                "contract_ref": entry.get("contract_ref"),
                "status": "OK",
            }
        )
        self.selected[module] = set(entry.get("symbols") or []) if entry["export_mode"] == "explicit_symbols" else set()

    def _selected_names(self, entry: dict, source: ModuleSource) -> list[str]:
        if entry["export_mode"] == "explicit_symbols":
            return list(entry["symbols"])
        if source.all_names is DYNAMIC or source.all_names is None:
            raise fail(
                "dynamic_all_unsupported",
                "export_mode requires a literal __all__ list of string constants; "
                "dynamic/computed/augmented __all__ must be reported, not simulated",
                {
                    "module": entry["module"],
                    "path": entry["source_path"],
                    "__all__ lineno": source.all_node_lineno,
                },
            )
        names = list(source.all_names)
        if len(set(names)) != len(names):
            raise fail(
                "dynamic_all_unsupported",
                "__all__ contains duplicate names",
                {"module": entry["module"], "names": names},
            )
        return names

    def _resolve_entry(self, entry: dict) -> None:
        module = entry["module"]
        source = self.modules[module]
        names = self._selected_names(entry, source)
        if entry["export_mode"] != "explicit_symbols":
            self.selected[module] = set(names)
        present = sorted(
            set(source.defs) | set(source.constants) | set(source.aliases) | set(source.import_from)
        )
        exports: list[dict] = []
        for name in names:
            binding = self._resolve_name(module, name, chain=[])
            if binding["kind"] == "missing":
                raise fail(
                    "symbol_missing",
                    "allowlist names a symbol that does not exist; correct the whitelist with a "
                    "human adjudication record instead of guessing a name",
                    {
                        "module": module,
                        "missing_symbol": name,
                        "top_level_names_present": present,
                    },
                )
            exports.append({"name": name, "defines": binding["qualified_name"], "via": binding["via"]})
        self.exports[module] = exports

    def _resolve_name(self, module: str, name: str, chain: list[str]) -> dict:
        """Resolve one allowlisted name to its defining record (aliases collapse)."""
        key = f"{module}::{name}"
        if key in chain:
            raise fail(
                "alias_cycle",
                "alias/re-export resolution cycle detected",
                {"chain": chain + [key]},
            )
        source = self.modules[module]
        entry = self.entries_by_module[module]
        via: list[str] = [f"{module}.{name}"]

        if name in source.defs:
            node = source.defs[name]
            return self._record_for_def(module, name, node, entry, via)
        if name in source.constants:
            node = source.constants[name]
            return self._record_for_constant(module, name, node, entry, via, source)
        if name in source.aliases:
            target = source.aliases[name]
            if target not in self.selected.get(module, set()):
                raise fail(
                    "alias_outside_allowlist",
                    "alias target is not part of the same entry's allowlist; bounded exports refuse "
                    "to chase unselected names",
                    {"module": module, "alias": name, "target": target},
                )
            inner = self._resolve_name(module, target, chain + [key])
            record = {
                "qualified_name": f"{module}.{name}",
                "module": module,
                "name": name,
                "kind": "alias",
                "stability": entry["stability"],
                "source_path": entry["source_path"],
                "lineno": self._alias_lineno(source, name),
                "end_lineno": self._alias_lineno(source, name),
                "source_segment_sha256": self._alias_segment_sha(source, name),
                "signature": None,
                "docstring_sha256": None,
                "documented": False,
                "alias_of": inner["qualified_name"],
                "alias_chain": via + inner["via"],
                "fields": [],
                "class_attributes": [],
                "methods": [],
            }
            self.records[record["qualified_name"]] = record
            return {"qualified_name": record["qualified_name"], "via": via, "kind": "resolved"}
        if name in source.import_from:
            origin_module = source.resolve_import_module(name, package_of(module, entry["source_path"]))
            origin_name = source.import_from[name][2]
            if origin_module is None or origin_module not in self.entries_by_module:
                raise fail(
                    "cross_allowlist_import",
                    "re-export originates from a module outside the active allowlist; bounded exports "
                    "refuse to scan the repository",
                    {
                        "module": module,
                        "name": name,
                        "origin_module": origin_module,
                        "origin_name": origin_name,
                    },
                )
            if origin_name not in self.selected.get(origin_module, set()):
                raise fail(
                    "reexport_outside_origin_allowlist",
                    "re-exported name is not selected in its origin entry's allowlist",
                    {
                        "module": module,
                        "name": name,
                        "origin_module": origin_module,
                        "origin_name": origin_name,
                    },
                )
            origin_entry = self.entries_by_module[origin_module]
            inner = self._resolve_name(origin_module, origin_name, chain + [key])
            return {"qualified_name": inner["qualified_name"], "via": via + inner["via"], "kind": "resolved"}
        raise fail(
            "symbol_missing",
            "allowlist names a symbol with no top-level binding",
            {"module": module, "missing_symbol": name, "top_level_names_present": sorted(set(source.defs) | set(source.constants) | set(source.aliases))},
        )

    def _alias_lineno(self, source: ModuleSource, name: str) -> int:
        for node in source.tree.body:
            if isinstance(node, ast.Assign):
                if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                    return node.lineno
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id == name:
                    return node.lineno
        return 0

    def _alias_segment_sha(self, source: ModuleSource, name: str) -> str | None:
        for node in source.tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets
            ):
                return sha256_bytes(segment(source.text, node).encode("utf-8"))
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
                return sha256_bytes(segment(source.text, node).encode("utf-8"))
        return None

    def _record_for_def(
        self, module: str, name: str, node: ast.stmt, entry: dict, via: list[str]
    ) -> dict:
        qualified = f"{module}.{name}"
        if qualified in self.records:
            return {"qualified_name": qualified, "via": via, "kind": "resolved"}
        doc, doc_sha, documented = docstring_info(node)
        if isinstance(node, ast.ClassDef):
            signature = class_signature(node, self.modules[module].text)
        else:
            signature = function_signature(node, self.modules[module].text)  # type: ignore[arg-type]
        record = {
            "qualified_name": qualified,
            "module": module,
            "name": name,
            "kind": signature["kind"],
            "stability": entry["stability"],
            "source_path": entry["source_path"],
            "lineno": node.lineno,
            "end_lineno": node.end_lineno,
            "source_segment_sha256": sha256_bytes(segment(self.modules[module].text, node).encode("utf-8")),
            "body_sha256": segment_body_sha256(self.modules[module].text, node),
            "signature": signature,
            "docstring_sha256": doc_sha,
            "documented": documented,
            "alias_of": None,
            "alias_chain": via,
        }
        self.records[qualified] = record
        remember_docstring(qualified, doc)
        return {"qualified_name": qualified, "via": via, "kind": "resolved"}

    def _record_for_constant(
        self, module: str, name: str, node: ast.stmt, entry: dict, via: list[str], source: ModuleSource
    ) -> dict:
        qualified = f"{module}.{name}"
        if qualified in self.records:
            return {"qualified_name": qualified, "via": via, "kind": "resolved"}
        if isinstance(node, ast.Assign):
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            value = node.value
        else:  # pragma: no cover - constants table only holds Assign/AnnAssign
            value = None
        value_text = one_line(segment(source.text, value)) if value is not None else None
        record = {
            "qualified_name": qualified,
            "module": module,
            "name": name,
            "kind": "constant",
            "stability": entry["stability"],
            "source_path": entry["source_path"],
            "lineno": node.lineno,
            "end_lineno": node.end_lineno,
            "source_segment_sha256": sha256_bytes(segment(source.text, node).encode("utf-8")),
            "signature": {"kind": "constant", "value_text": value_text},
            "docstring_sha256": None,
            "documented": False,
            "alias_of": None,
            "alias_chain": via,
        }
        self.records[qualified] = record
        return {"qualified_name": qualified, "via": via, "kind": "resolved"}

    # -- outputs -----------------------------------------------------------
    def inventory(self) -> dict:
        return {
            "schema": INVENTORY_SCHEMA,
            "generator_version": GENERATOR_VERSION,
            "surface_id": self.surface.get("surface_id"),
            "surface_sha256": self.surface_sha,
            "inputs": sorted(self.inputs, key=lambda item: item["module"]),
            "pending": sorted(self.surface.get("pending", []), key=lambda item: item["id"]),
            "symbols": sorted(self.records.values(), key=lambda item: item["qualified_name"]),
            "exports": {
                module: sorted(exports, key=lambda item: item["name"])
                for module, exports in sorted(self.exports.items())
            },
        }

    def build_report(self) -> dict:
        per_entry = []
        for entry in self.surface["entries"]:
            module = entry["module"]
            per_entry.append(
                {
                    "module": module,
                    "status": "OK",
                    "export_mode": entry["export_mode"],
                    "stability": entry["stability"],
                    "selected_symbols": len(self.exports.get(module, [])),
                    "defined_records": sum(1 for r in self.records.values() if r["module"] == module),
                    "undocumented": sum(
                        1
                        for name in [e["name"] for e in self.exports.get(module, [])]
                        for r in [self.records.get(f"{module}.{name}")]
                        if r is not None and not r["documented"]
                    ),
                }
            )
        return {
            "schema": BUILD_REPORT_SCHEMA,
            "generator_version": GENERATOR_VERSION,
            "surface_id": self.surface.get("surface_id"),
            "surface_sha256": self.surface_sha,
            "entries": sorted(per_entry, key=lambda item: item["module"]),
            "pending": sorted(self.surface.get("pending", []), key=lambda item: item["id"]),
            "totals": {
                "entries": len(per_entry),
                "symbol_records": len(self.records),
                "exports_registered": sum(len(v) for v in self.exports.values()),
                "pending_items": len(self.surface.get("pending", [])),
                "first_batch_scope_complete": len(self.surface.get("pending", [])) == 0,
            },
            "diagnostics": [],
            "notes": [
                "Static extraction only; documented code was never imported or executed.",
                "Classes list declared members only (inheritance is not expanded).",
                "Links reference local path + line + full file sha256 (no fabricated commit URLs).",
            ],
        }


# --------------------------------------------------------------------------
# markdown rendering
# --------------------------------------------------------------------------


def docstring_block(doc: str | None) -> str:
    if doc is None:
        return "- Docstring: **UNDOCUMENTED**"
    escaped = doc.replace("```", "`​``")
    return "- Docstring (original):\n\n```text\n" + escaped + "\n```"


def render_symbol_section(record: dict, inputs_by_module: dict[str, dict]) -> str:
    lines: list[str] = []
    lines.append(f"## `{record['name']}`")
    lines.append("")
    lines.append(f"- Kind: `{record['kind']}`")
    lines.append(f"- Stability: `{record['stability']}`")
    file_sha = inputs_by_module[record["module"]]["sha256"]
    lines.append(
        f"- Defined at: `{record['source_path']}` line {record['lineno']}..{record['end_lineno']} "
        f"(file sha256 `{file_sha}`)"
    )
    lines.append(f"- Definition segment sha256: `{record['source_segment_sha256']}`")
    if record.get("body_sha256"):
        lines.append(f"- Body sha256 (docstring lines excluded): `{record['body_sha256']}`")
    if record["alias_of"]:
        target_page = record["alias_of"].rsplit(".", 1)[0]
        anchor = heading_slug(record["alias_of"].rsplit(".", 1)[1])
        lines.append(f"- **Alias of** [`{record['alias_of']}`](modules/{target_page}.md#{anchor}) — same declaration, no duplicate implementation.")
        lines.append("")
        return "\n".join(lines) + "\n"
    signature = record["signature"]
    if record["kind"] == "constant":
        lines.append(f"- Value (source text, never evaluated): `{signature['value_text']}`")
    else:
        code = []
        for decorator in signature.get("decorators", []):
            code.append(f"@{decorator}")
        if record["kind"] == "class":
            base_text = ""
            if signature["bases"] or signature["keywords"]:
                inner = ", ".join(signature["bases"] + signature["keywords"])
                base_text = f"({inner})"
            code.append(f"class {record['name']}{base_text}:")
        else:
            code.append(f"def {signature['rendered']}:")
        lines.append("- Signature (annotations/defaults preserved as source text):")
        lines.append("")
        lines.append("```python")
        lines.extend(code)
        lines.append("```")
        if record["kind"] == "class":
            lines.append("")
            lines.append("> Declared members only — inheritance is **not** expanded; this is not a claim about the full runtime API.")
            if signature.get("dataclass"):
                lines.append("")
                lines.append("- Dataclass (statically detected decorator): yes")
            if signature.get("fields"):
                lines.append("")
                lines.append("- Declared fields:")
                lines.append("")
                lines.append("| Field | Annotation (source text) | Default (source text) |")
                lines.append("| --- | --- | --- |")
                for field_info in signature["fields"]:
                    lines.append(
                        f"| `{field_info['name']}` | `{field_info['annotation']}` | "
                        f"{'`' + field_info['default'] + '`' if field_info['default'] is not None else '—'} |"
                    )
            if signature.get("class_attributes"):
                lines.append("")
                lines.append("- Declared class attributes:")
                lines.append("")
                lines.append("| Attribute | Annotation (source text) | Value (source text) |")
                lines.append("| --- | --- | --- |")
                for attr in signature["class_attributes"]:
                    annotation = f"`{attr['annotation']}`" if attr["annotation"] else "—"
                    value = f"`{attr['default']}`" if attr["default"] is not None else "—"
                    lines.append(f"| `{attr['name']}` | {annotation} | {value} |")
            if signature.get("methods"):
                lines.append("")
                lines.append("- Declared methods:")
                lines.append("")
                for method in signature["methods"]:
                    lines.append(
                        f"#### {record['name']}.{method['name']}"
                    )
                    lines.append("")
                    lines.append(f"- Line {method['lineno']}..{method['end_lineno']}")
                    lines.append("")
                    lines.append("```python")
                    lines.append(f"def {method['signature']['rendered']}:")
                    lines.append("```")
                    lines.append("")
                    lines.append(
                        f"- Docstring: {'present (sha256 `' + method['docstring_sha256'] + '`)' if method['documented'] else '**UNDOCUMENTED**'}"
                    )
                    lines.append("")
    lines.append("")
    if record["kind"] == "constant":
        lines.append("- Docstring: n/a (constants carry no docstrings in Python)")
    else:
        lines.append(docstring_block(None if not record["documented"] else _docstore_get(record)))
    return "\n".join(lines) + "\n"


# Docstring texts are cached at record-creation time for page rendering; the
# inventory stores only their hashes to keep diffs small and deterministic.
_DOC_CACHE: dict[str, str] = {}


def remember_docstring(qualified_name: str, doc: str | None) -> None:
    if doc is not None:
        _DOC_CACHE[qualified_name] = doc


def _docstore_get(record: dict) -> str | None:
    return _DOC_CACHE.get(record["qualified_name"])


def render_module_page(entry: dict, extraction: Extraction) -> str:
    module = entry["module"]
    source = extraction.modules[module]
    inputs_by_module = {item["module"]: item for item in extraction.inputs}
    input_info = inputs_by_module[module]
    lines: list[str] = []
    lines.append(f"# API reference · `{module}`")
    lines.append("")
    lines.append(f"- Stability: `{entry['stability']}`")
    lines.append(f"- Export mode: `{entry['export_mode']}`")
    lines.append(f"- Source: `{entry['source_path']}` (file sha256 `{input_info['sha256']}`, {input_info['bytes']} bytes)")
    if entry.get("owner"):
        lines.append(f"- Owner: {entry['owner']}")
    if entry.get("contract_ref"):
        lines.append(f"- Authoritative contract: `{entry['contract_ref']}` (this generated page documents, it does not replace it)")
    lines.append(f"- Generated by {GENERATOR_NAME}/{GENERATOR_VERSION} — static AST analysis, documented code never executed.")
    lines.append("")
    if entry["stability"] == "MODULE_API":
        lines.append("> `MODULE_API`: curated per the NF10 work order. This listing must not be read as a promise of external stability.")
        lines.append("")
    if entry["export_mode"] in ("package_all", "module_all"):
        lines.append(f"`__all__` exports (literal, resolved statically; {len(extraction.exports[module])} names):")
        lines.append("")
        lines.append("| Export | Kind | Defines | Stability |")
        lines.append("| --- | --- | --- | --- |")
        for export in extraction.exports[module]:
            record = extraction.records[export["defines"]]
            anchor = heading_slug(record["name"])
            link = f"[`{record['qualified_name']}`](modules/{record['module']}.md#{anchor})"
            kind = record["kind"]
            if record["kind"] != "alias" and export["name"] != record["name"]:
                kind = f"{kind} (exported as `{export['name']}`)"
            lines.append(f"| `{export['name']}` | `{kind}` | {link} | `{record['stability']}` |")
        lines.append("")
        source_records = [
            extraction.records[q]
            for q in sorted(extraction.records)
            if extraction.records[q]["module"] == module
        ]
        if source_records:
            lines.append("## Locally declared symbols in this file")
            lines.append("")
            for record in source_records:
                lines.append(f"- [`{record['name']}`](#{heading_slug(record['name'])}) — `{record['kind']}`")
            lines.append("")
    if module == "ak_trading.v2":
        lines.append("> This package intentionally does not re-export submodules (see its `__init__.py` docstring); the constants below are its whole public surface.")
        lines.append("")
    for record in sorted(
        (extraction.records[q] for q in extraction.records if extraction.records[q]["module"] == module),
        key=lambda r: r["name"],
    ):
        lines.append(render_symbol_section(record, inputs_by_module))
    return "\n".join(lines).rstrip() + "\n"


def render_index(extraction: Extraction, build_id: str | None) -> str:
    _ = build_id  # deliberately not embedded: page bytes stay content-deterministic
    lines: list[str] = []
    lines.append("# NF10 static API reference (first batch)")
    lines.append("")
    lines.append(
        f"Generated by {GENERATOR_NAME}/{GENERATOR_VERSION} from frozen surface "
        f"`{extraction.surface.get('surface_id')}` (surface sha256 `{extraction.surface_sha}`). "
        "Pure static AST extraction — no documented module was imported or executed."
    )
    lines.append("")
    lines.append("| Module | Stability | Export mode | Source | Symbols | Page |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for entry in sorted(extraction.surface["entries"], key=lambda e: e["module"]):
        module = entry["module"]
        count = len(extraction.exports.get(module, []))
        lines.append(
            f"| `{module}` | `{entry['stability']}` | `{entry['export_mode']}` | "
            f"`{entry['source_path']}` | {count} | [page](modules/{module}.md) |"
        )
    pending = extraction.surface.get("pending", [])
    if pending:
        lines.append("")
        lines.append("## Pending coverage (no pages generated)")
        lines.append("")
        lines.append("| Pending item | Reason | Owner | Note |")
        lines.append("| --- | --- | --- | --- |")
        for item in sorted(pending, key=lambda p: p["id"]):
            lines.append(
                f"| `{item['id']}` | `{item['reason']}` | {item.get('owner', '—')} | {item.get('note', '—')} |"
            )
        lines.append("")
        lines.append(
            "The first-batch scope is **not complete** while the table above is non-empty; "
            "these pages require the DP owner's released source-map before generation."
        )
    lines.append("")
    lines.append("## Drift checking")
    lines.append("")
    lines.append(
        "Run `python scripts/generate_api_reference.py check --surface docs/api-reference-config/surface.json "
        "--baseline <build>/inventory.json` to compare current sources against a baseline build."
    )
    lines.append("")
    return "\n".join(lines)


def collect_markdown_links(text: str) -> list[str]:
    return re.findall(r"\]\(([^)#\s]+\.md)(#[a-z0-9_\-]+)?\)", text)


def validate_links(pages: dict[str, str]) -> dict:
    slugs: dict[str, set[str]] = {}
    for name, text in pages.items():
        found = set()
        for line in text.splitlines():
            if line.startswith("#"):
                heading = line.lstrip("#").strip()
                if heading:
                    found.add(heading_slug(heading))
        slugs[name] = found
    dangling: list[str] = []
    checked = 0
    for name, text in pages.items():
        for target, anchor in collect_markdown_links(text):
            checked += 1
            target_name = target[:-3] if target.endswith(".md") else target
            if target_name not in {p[:-3] for p in pages}:
                dangling.append(f"{name} -> {target}")
                continue
            if anchor:
                slug = anchor[1:]
                page_key = next(p for p in pages if p[:-3] == target_name)
                if slug not in slugs[page_key]:
                    dangling.append(f"{name} -> {target}{anchor}")
    return {"links_checked": checked, "dangling": dangling}


# --------------------------------------------------------------------------
# build command
# --------------------------------------------------------------------------


def cmd_build(args: argparse.Namespace) -> int:
    surface_path = Path(args.surface).resolve()
    surface, root, surface_sha = load_surface(surface_path)
    extraction = Extraction(surface, root, surface_sha)
    extraction.run()

    pages: dict[str, str] = {}
    for entry in surface["entries"]:
        module = entry["module"]
        pages[f"modules/{module}.md"] = render_module_page(entry, extraction)
    pages["index.md"] = render_index(extraction, args.build_id)

    link_report = validate_links(pages)
    if link_report["dangling"]:
        raise fail("dangling_link", "generated pages contain dangling internal links", link_report)

    inventory = extraction.inventory()
    build_report = extraction.build_report()
    build_report["link_check"] = link_report

    output_root = Path(args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    final_dir = output_root / args.build_id
    if final_dir.exists():
        raise fail(
            "build_id_exists",
            "build-id directory already exists; builds never silently overwrite previous pages",
            {"path": str(final_dir)},
        )
    staging = output_root / f".staging-{args.build_id}"
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "modules").mkdir(parents=True)
    for rel, content in pages.items():
        target = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    (staging / "inventory.json").write_text(dump_json_canonical(inventory), encoding="utf-8", newline="\n")
    (staging / "build-report.json").write_text(dump_json_canonical(build_report), encoding="utf-8", newline="\n")
    receipt = {
        "build_id": args.build_id,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generator": f"{GENERATOR_NAME}/{GENERATOR_VERSION}",
        "generator_file_sha256": sha256_bytes(read_file_bytes(Path(__file__).resolve())),
        "argv": sys.argv,
        "cwd": os.getcwd(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "surface_path": str(surface_path),
        "output_root": str(output_root),
        "final_dir": str(final_dir),
        "staging_dir": str(staging),
        "note": "receipt is excluded from determinism comparisons (observed build metadata only)",
    }
    (staging / "receipt.json").write_text(dump_json_canonical(receipt), encoding="utf-8", newline="\n")
    os.rename(staging, final_dir)

    summary = {
        "ok": True,
        "build_id": args.build_id,
        "final_dir": str(final_dir),
        "entries": len(surface["entries"]),
        "symbol_records": len(extraction.records),
        "exports_registered": sum(len(v) for v in extraction.exports.values()),
        "pending": len(surface.get("pending", [])),
        "links_checked": link_report["links_checked"],
        "dangling_links": 0,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


# --------------------------------------------------------------------------
# check command (H10.6)
# --------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    surface_path = Path(args.surface).resolve()
    baseline_path = Path(args.baseline).resolve()
    baseline = read_json_file(baseline_path)
    if not isinstance(baseline, dict) or baseline.get("schema") != INVENTORY_SCHEMA:
        raise fail("baseline_invalid", "baseline is not an nf10 inventory", {"path": str(baseline_path)})
    surface, root, surface_sha = load_surface(surface_path)
    extraction = Extraction(surface, root, surface_sha)
    extraction.run()

    current = extraction.inventory()
    report = {
        "schema": DRIFT_REPORT_SCHEMA,
        "generator_version": GENERATOR_VERSION,
        "baseline_path": str(baseline_path),
        "surface_path": str(surface_path),
        "baseline_surface_sha256": baseline.get("surface_sha256"),
        "current_surface_sha256": surface_sha,
        "inputs_added": [],
        "inputs_removed": [],
        "inputs_changed": [],
        "exports_changed": [],
        "symbols_added": [],
        "symbols_removed": [],
        "symbols_changed": [],
        "pending_changed": [],
    }

    base_inputs = {item["module"]: item for item in baseline.get("inputs", [])}
    cur_inputs = {item["module"]: item for item in current["inputs"]}
    for module in sorted(set(base_inputs) - set(cur_inputs)):
        report["inputs_removed"].append({"module": module, "sha256": base_inputs[module]["sha256"]})
    for module in sorted(set(cur_inputs) - set(base_inputs)):
        report["inputs_added"].append({"module": module, "sha256": cur_inputs[module]["sha256"]})
    for module in sorted(set(base_inputs) & set(cur_inputs)):
        old_sha = base_inputs[module]["sha256"]
        new_sha = cur_inputs[module]["sha256"]
        if old_sha != new_sha:
            report["inputs_changed"].append(
                {
                    "module": module,
                    "baseline_sha256": old_sha,
                    "current_sha256": new_sha,
                    "note": "baseline input hash no longer matches current bytes; same-version proof refused",
                }
            )

    base_exports = baseline.get("exports", {})
    cur_exports = current["exports"]
    base_pending = {item["id"]: item for item in baseline.get("pending", [])}
    cur_pending = {item["id"]: item for item in current["pending"]}
    if base_pending != cur_pending:
        report["pending_changed"] = {
            "baseline_ids": sorted(base_pending),
            "current_ids": sorted(cur_pending),
            "classification": "NON_BREAKING",
        }
    for module in sorted(set(base_exports) | set(cur_exports)):
        old_names = {e["name"] for e in base_exports.get(module, [])}
        new_names = {e["name"] for e in cur_exports.get(module, [])}
        added = sorted(new_names - old_names)
        removed = sorted(old_names - new_names)
        if added or removed:
            report["exports_changed"].append(
                {
                    "module": module,
                    "added": added,
                    "removed": removed,
                    "classification": "BREAKING" if removed else "NON_BREAKING",
                }
            )

    base_symbols = {s["qualified_name"]: s for s in baseline.get("symbols", [])}
    cur_symbols = {s["qualified_name"]: s for s in current["symbols"]}
    for qualified in sorted(set(cur_symbols) - set(base_symbols)):
        report["symbols_added"].append({"qualified_name": qualified, "kind": cur_symbols[qualified]["kind"]})
    for qualified in sorted(set(base_symbols) - set(cur_symbols)):
        report["symbols_removed"].append(
            {"qualified_name": qualified, "kind": base_symbols[qualified]["kind"]}
        )
    for qualified in sorted(set(base_symbols) & set(cur_symbols)):
        old = base_symbols[qualified]
        new = cur_symbols[qualified]
        changes: list[dict] = []
        if old.get("kind") != new.get("kind"):
            changes.append({"field": "kind", "baseline": old.get("kind"), "current": new.get("kind")})
        if old.get("lineno") != new.get("lineno") or old.get("end_lineno") != new.get("end_lineno"):
            changes.append(
                {
                    "field": "location",
                    "baseline": [old.get("lineno"), old.get("end_lineno")],
                    "current": [new.get("lineno"), new.get("end_lineno")],
                }
            )
        old_sig = old.get("signature") or {}
        new_sig = new.get("signature") or {}
        if old.get("kind") == "alias" or new.get("kind") == "alias":
            if old.get("alias_of") != new.get("alias_of"):
                changes.append(
                    {"field": "alias_target", "baseline": old.get("alias_of"), "current": new.get("alias_of")}
                )
        elif old.get("kind") == "constant" and new.get("kind") == "constant":
            if old_sig.get("value_text") != new_sig.get("value_text"):
                changes.append(
                    {"field": "value_text", "baseline": old_sig.get("value_text"), "current": new_sig.get("value_text")}
                )
        elif old_sig or new_sig:
            old_params = old_sig.get("parameters", [])
            new_params = new_sig.get("parameters", [])
            param_shape_changed = [
                (p.get("name"), p.get("kind")) for p in old_params
            ] != [(p.get("name"), p.get("kind")) for p in new_params]
            defaults_changed = [p.get("default") for p in old_params] != [p.get("default") for p in new_params]
            annotations_changed = [p.get("annotation") for p in old_params] != [
                p.get("annotation") for p in new_params
            ] or old_sig.get("returns") != new_sig.get("returns")
            rendered_changed = old_sig.get("rendered") != new_sig.get("rendered")
            if param_shape_changed:
                changes.append(
                    {
                        "field": "parameters",
                        "baseline": [(p["name"], p["kind"]) for p in old_params],
                        "current": [(p["name"], p["kind"]) for p in new_params],
                    }
                )
            if defaults_changed:
                changes.append(
                    {
                        "field": "defaults",
                        "baseline": [p.get("default") for p in old_params],
                        "current": [p.get("default") for p in new_params],
                    }
                )
            if annotations_changed:
                changes.append(
                    {
                        "field": "annotations",
                        "baseline": {"params": [p.get("annotation") for p in old_params], "returns": old_sig.get("returns")},
                        "current": {"params": [p.get("annotation") for p in new_params], "returns": new_sig.get("returns")},
                    }
                )
            if rendered_changed and not (param_shape_changed or defaults_changed or annotations_changed):
                changes.append(
                    {"field": "signature_rendered", "baseline": old_sig.get("rendered"), "current": new_sig.get("rendered")}
                )
            if old_sig.get("decorators") != new_sig.get("decorators"):
                changes.append(
                    {"field": "decorators", "baseline": old_sig.get("decorators"), "current": new_sig.get("decorators")}
                )
            if old_sig.get("kind") == "class" and new_sig.get("kind") == "class":
                if old_sig.get("bases") != new_sig.get("bases"):
                    changes.append({"field": "bases", "baseline": old_sig.get("bases"), "current": new_sig.get("bases")})
                if [f["name"] for f in old_sig.get("fields", [])] != [f["name"] for f in new_sig.get("fields", [])]:
                    changes.append(
                        {
                            "field": "fields",
                            "baseline": [f["name"] for f in old_sig.get("fields", [])],
                            "current": [f["name"] for f in new_sig.get("fields", [])],
                        }
                    )
                if [m["name"] for m in old_sig.get("methods", [])] != [m["name"] for m in new_sig.get("methods", [])]:
                    changes.append(
                        {
                            "field": "methods",
                            "baseline": [m["name"] for m in old_sig.get("methods", [])],
                            "current": [m["name"] for m in new_sig.get("methods", [])],
                        }
                    )
        if old.get("docstring_sha256") != new.get("docstring_sha256"):
            changes.append({"field": "docstring", "baseline": old.get("docstring_sha256"), "current": new.get("docstring_sha256")})
        if old.get("source_segment_sha256") != new.get("source_segment_sha256"):
            if old.get("body_sha256") != new.get("body_sha256"):
                changes.append(
                    {"field": "body", "note": "definition body bytes changed beyond any docstring edit"}
                )
        if old.get("stability") != new.get("stability"):
            changes.append({"field": "stability", "baseline": old.get("stability"), "current": new.get("stability")})
        if changes:
            hard_fields = {"parameters", "defaults", "annotations", "signature_rendered", "value_text", "alias_target", "bases", "fields", "kind"}
            if any(c["field"] in hard_fields for c in changes):
                classification = "BREAKING"
            elif all(c["field"] == "docstring" for c in changes):
                classification = "DOC_ONLY"
            else:
                classification = "NON_BREAKING"
            report["symbols_changed"].append(
                {"qualified_name": qualified, "changes": changes, "classification": classification}
            )

    drift_found = any(
        report[key]
        for key in (
            "inputs_added",
            "inputs_removed",
            "inputs_changed",
            "exports_changed",
            "symbols_added",
            "symbols_removed",
            "symbols_changed",
            "pending_changed",
        )
    )
    report["same_version_proof"] = not drift_found
    if report["same_version_proof"] and baseline.get("surface_sha256") != surface_sha:
        report["same_version_proof"] = False
        report["same_version_proof_refused_reason"] = "surface bytes changed between baseline and check"
    report["summary"] = {
        "breaking": sum(
            1
            for item in report["symbols_changed"]
            if item["classification"] == "BREAKING"
        )
        + sum(1 for item in report["exports_changed"] if item["classification"] == "BREAKING"),
        "non_breaking": sum(
            1
            for item in report["symbols_changed"]
            if item["classification"] == "NON_BREAKING"
        )
        + sum(1 for item in report["exports_changed"] if item["classification"] == "NON_BREAKING")
        + len(report["symbols_added"])
        + len(report["inputs_added"]),
        "doc_only": sum(1 for item in report["symbols_changed"] if item["classification"] == "DOC_ONLY"),
        "removed": len(report["symbols_removed"]) + len(report["inputs_removed"]),
        "input_hash_drift": len(report["inputs_changed"]),
    }
    payload = dump_json_canonical(report)
    if args.report:
        Path(args.report).resolve().write_text(payload, encoding="utf-8", newline="\n")
    print(payload)
    return 0 if not drift_found else 1


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="generate_api_reference.py", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"{GENERATOR_NAME}/{GENERATOR_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="generate pages + inventory into a fresh build-id directory")
    build.add_argument("--surface", required=True, help="path to the frozen surface JSON")
    build.add_argument("--output-root", required=True, help="directory that will contain <build-id>/")
    build.add_argument("--build-id", required=True, help="fresh build identifier (must not already exist)")
    build.set_defaults(func=cmd_build)

    check = sub.add_parser("check", help="diff current sources against a baseline inventory")
    check.add_argument("--surface", required=True)
    check.add_argument("--baseline", required=True, help="path to a baseline inventory.json")
    check.add_argument("--report", default=None, help="optional path to also write the drift report JSON")
    check.set_defaults(func=cmd_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):  # pragma: no cover - non-tty streams
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SurfaceError as exc:
        print(
            json.dumps(
                {"ok": False, "code": exc.code, "message": exc.message, "details": exc.details},
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    except OSError as exc:
        print(json.dumps({"ok": False, "code": "io_error", "message": str(exc)}, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
