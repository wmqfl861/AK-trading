"""Round-4 external probe: ak_trading imports that do not resolve to a tracked file.

Lists every import ak_trading... / from ak_trading... import name in
src/, scripts/, tools/, tests/, integrations/ and migrations/ whose target
module (or submodule) does not exist under src/, and packages that are
imported but have no __init__.py. Read-only.

Usage (repository root):
    python docs/repo-audit-20261110-claude/probes/missing_modules.py .
"""
import ast, sys, pathlib, collections
root = pathlib.Path(sys.argv[1]).resolve()
src = root / "src"
def exists(mod):
    parts = mod.split(".")
    p = src.joinpath(*parts)
    return (p.with_suffix(".py")).exists() or (p / "__init__.py").exists() or p.is_dir()
def is_pkg_without_init(mod):
    p = src.joinpath(*mod.split("."))
    return p.is_dir() and not (p / "__init__.py").exists()
missing = collections.defaultdict(list); noinit = collections.defaultdict(list)
missing_attr = collections.defaultdict(list)
for sc in ["src", "scripts", "tools", "tests", "integrations", "migrations"]:
    for p in sorted((root / sc).rglob("*.py")):
        try: t = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError: continue
        r = str(p.relative_to(root))
        for n in ast.walk(t):
            mods = []
            if isinstance(n, ast.Import):
                mods = [(a.name, None) for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                mods = [(n.module, [a.name for a in n.names])]
            elif isinstance(n, ast.ImportFrom) and n.level > 0 and r.startswith("src/"):
                base = r[4:-3].split("/")
                if base[-1] == "__init__": base = base[:-1]
                else: base = base[:-1]
                up = base[: len(base) - (n.level - 1)] if n.level > 1 else base
                m = ".".join(up + ([n.module] if n.module else []))
                mods = [(m, [a.name for a in n.names])]
            for m, names in mods:
                if not m.startswith("ak_trading"): continue
                if not exists(m):
                    missing[m].append((r, n.lineno))
                    continue
                if is_pkg_without_init(m): noinit[m].append((r, n.lineno))
                if names:
                    for nm in names:
                        sub = m + "." + nm
                        pp = src.joinpath(*m.split("."))
                        if pp.is_dir() and not exists(sub):
                            # name may be defined in __init__
                            init = pp / "__init__.py"
                            ok = False
                            if init.exists():
                                it = ast.parse(init.read_text(encoding="utf-8", errors="replace"))
                                for x in ast.walk(it):
                                    if isinstance(x, (ast.FunctionDef, ast.ClassDef)) and x.name == nm: ok = True
                                    if isinstance(x, ast.Assign):
                                        for tg in x.targets:
                                            if isinstance(tg, ast.Name) and tg.id == nm: ok = True
                                    if isinstance(x, ast.ImportFrom):
                                        for a in x.names:
                                            if (a.asname or a.name) == nm: ok = True
                                    if isinstance(x, ast.AnnAssign) and isinstance(x.target, ast.Name) and x.target.id == nm: ok = True
                            if not ok: missing_attr[sub].append((r, n.lineno))
print("== imported ak_trading modules that do not exist in the tree ==")
for m, v in sorted(missing.items()):
    print(f"  {m}: {len(v)} import sites; e.g. {v[:3]}")
print("== submodule/name imports not resolvable (pkg dir exists, member missing) ==")
for m, v in sorted(missing_attr.items()):
    print(f"  {m}: {len(v)} sites; e.g. {v[:3]}")
print("== packages imported but lacking __init__.py (namespace packages) ==")
for m, v in sorted(noinit.items()):
    print(f"  {m}: {len(v)} sites")
