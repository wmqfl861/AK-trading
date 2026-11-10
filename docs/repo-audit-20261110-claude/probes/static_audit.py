"""Round-4 external probe: static audit over the repository's Python sources.

Read-only AST checks for src/, scripts/, tools/, integrations/, migrations/
and tests/: intra-package call-signature mismatches (missing or unknown
arguments on calls that resolve to a top-level function of ak_trading),
exception handlers that swallow TypeError / NameError / AttributeError,
broad handlers, functions longer than 150 lines, multi-statement lines,
function-level imports, eval/exec, shell=True, SQL built with f-strings,
hard-coded secret look-alikes and Windows absolute paths.

Findings are leads to read, not verdicts: calls inside pytest.raises are
reported too, and a "possible_hardcoded_secret" in tests is usually a fixture.

Usage (repository root):
    python docs/repo-audit-20261110-claude/probes/static_audit.py . audit.json
"""
import ast, sys, json, pathlib, re, collections

root = pathlib.Path(sys.argv[1]).resolve()
out = pathlib.Path(sys.argv[2])
SCOPES = ["src", "scripts", "tools", "integrations", "migrations", "tests"]

files = []
for sc in SCOPES:
    d = root / sc
    if d.exists():
        files += sorted(d.rglob("*.py"))

def rel(p): return str(p.relative_to(root))

def complexity(fn):
    c = 1
    for n in ast.walk(fn):
        if isinstance(n, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.With, ast.AsyncWith, ast.IfExp, ast.Assert, ast.comprehension)):
            c += 1
        elif isinstance(n, ast.BoolOp):
            c += len(n.values) - 1
        elif isinstance(n, ast.match_case):
            c += 1
    return c

res = collections.defaultdict(list)
stats = {}
defs = {}      # module -> {name: FunctionDef}
mod_of = {}    # file -> dotted module (for src only)
trees = {}

for p in files:
    try:
        src = p.read_text(encoding="utf-8", errors="replace")
        t = ast.parse(src)
    except SyntaxError as e:
        res["syntax_error"].append((rel(p), e.lineno, e.msg)); continue
    trees[p] = (t, src)
    r = rel(p)
    if r.startswith("src/"):
        mod = r[4:-3].replace("/", ".")
        if mod.endswith(".__init__"): mod = mod[:-9]
        mod_of[p] = mod
        defs[mod] = {n.name: n for n in t.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

def sig_info(fn):
    a = fn.args
    npos = len(a.posonlyargs) + len(a.args)
    ndef = len(a.defaults)
    req_pos = npos - ndef
    pos_names = [x.arg for x in a.posonlyargs + a.args]
    kwonly_req = [k.arg for k, d in zip(a.kwonlyargs, a.kw_defaults) if d is None]
    kwonly_all = [k.arg for k in a.kwonlyargs]
    return dict(npos=npos, req_pos=req_pos, pos_names=pos_names, kwonly_req=kwonly_req,
                kwonly_all=kwonly_all, vararg=a.vararg is not None, kwarg=a.kwarg is not None)

for p, (t, src) in trees.items():
    r = rel(p)
    lines = src.splitlines()
    s = dict(loc=len(lines), funcs=0, long_funcs=0, max_cc=0, semis=0, fn_imports=0, crlf=("\r\n" in p.read_bytes().decode("latin-1")))
    # multi-statement lines (semicolon style)
    for n in ast.walk(t):
        pass
    # statements per line
    per_line = collections.Counter()
    for n in ast.walk(t):
        if isinstance(n, ast.stmt) and not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            per_line[n.lineno] += 1
    s["semis"] = sum(1 for k, v in per_line.items() if v > 1)
    s["long_lines"] = sum(1 for l in lines if len(l) > 160)
    # imported name resolution (module-level and function-level)
    imported = {}
    for n in ast.walk(t):
        if isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            for a in n.names:
                imported[a.asname or a.name] = (n.module, a.name)
    local_defs = {n.name: n for n in t.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for n in ast.walk(t):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            s["funcs"] += 1
            end = getattr(n, "end_lineno", n.lineno)
            ln = end - n.lineno + 1
            cc = complexity(n)
            s["max_cc"] = max(s["max_cc"], cc)
            if ln > 150:
                s["long_funcs"] += 1
                res["long_function"].append((r, n.lineno, n.name, ln, cc))
            elif cc > 40:
                res["complex_function"].append((r, n.lineno, n.name, ln, cc))
            for m in ast.walk(n):
                if isinstance(m, (ast.Import, ast.ImportFrom)):
                    s["fn_imports"] += 1
            # mutable defaults
            for d in n.args.defaults + [k for k in n.args.kw_defaults if k is not None]:
                if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                    res["mutable_default"].append((r, n.lineno, n.name))
        if isinstance(n, ast.ExceptHandler):
            ty = n.type
            names = []
            if ty is None: names = ["<bare>"]
            elif isinstance(ty, ast.Name): names = [ty.id]
            elif isinstance(ty, ast.Tuple): names = [getattr(e, "id", getattr(e, "attr", "?")) for e in ty.elts]
            elif isinstance(ty, ast.Attribute): names = [ty.attr]
            body_pass = all(isinstance(b, (ast.Pass, ast.Continue)) or (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant)) for b in n.body)
            reraises = any(isinstance(m, ast.Raise) for b in n.body for m in ast.walk(b))
            if "<bare>" in names:
                res["bare_except"].append((r, n.lineno))
            if any(x in ("Exception", "BaseException") for x in names):
                res["broad_except_reraise" if reraises else ("broad_except_silent" if body_pass else "broad_except_handled")].append((r, n.lineno, ",".join(names)))
            if "TypeError" in names and not reraises:
                res["typeerror_swallowed"].append((r, n.lineno, ",".join(names)))
            if any(x in ("NameError", "AttributeError") for x in names) and not reraises:
                res["name_attr_error_swallowed"].append((r, n.lineno, ",".join(names)))
        if isinstance(n, ast.Call):
            f = n.func
            fname = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
            if fname in ("eval", "exec") and isinstance(f, ast.Name):
                res["eval_exec"].append((r, n.lineno, fname))
            if fname in ("loads", "load") and isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in ("pickle", "marshal", "shelve"):
                res["pickle_load"].append((r, n.lineno))
            for kw in n.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    res["shell_true"].append((r, n.lineno))
                if kw.arg == "verify" and isinstance(kw.value, ast.Constant) and kw.value.value is False:
                    res["tls_verify_false"].append((r, n.lineno))
            if fname in ("utcnow",):
                res["utcnow"].append((r, n.lineno))
            if fname == "now" and isinstance(f, ast.Attribute) and not n.args and not n.keywords:
                res["naive_now"].append((r, n.lineno))
            if fname == "system" and isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "os":
                res["os_system"].append((r, n.lineno))
            # intra-package call signature check
            if isinstance(f, ast.Name) and not any(isinstance(a, ast.Starred) for a in n.args) and not any(k.arg is None for k in n.keywords):
                target = None
                if f.id in imported:
                    m, nm = imported[f.id]
                    if m in defs and nm in defs[m]: target = (m, defs[m][nm])
                elif f.id in local_defs:
                    target = ("<local>", local_defs[f.id])
                if target:
                    fn = target[1]
                    if fn.decorator_list:  # decorated → skip (may change signature)
                        pass
                    else:
                        si = sig_info(fn)
                        npass = len(n.args); kws = [k.arg for k in n.keywords]
                        problems = []
                        if npass > si["npos"] and not si["vararg"]:
                            problems.append(f"too_many_positional({npass}>{si['npos']})")
                        covered = set(si["pos_names"][:npass]) | set(kws)
                        missing_pos = [x for x in si["pos_names"][:si["req_pos"]] if x not in covered]
                        if missing_pos: problems.append("missing_positional:" + ",".join(missing_pos))
                        missing_kw = [x for x in si["kwonly_req"] if x not in kws]
                        if missing_kw: problems.append("missing_kwonly:" + ",".join(missing_kw))
                        unknown = [k for k in kws if k not in si["pos_names"] and k not in si["kwonly_all"]]
                        if unknown and not si["kwarg"]: problems.append("unknown_kw:" + ",".join(unknown))
                        if problems:
                            res["call_signature_mismatch"].append((r, n.lineno, f"{target[0]}.{fn.name}", ";".join(problems)))
        if isinstance(n, ast.Assert) and r.startswith("src/") and "/tests/" not in r:
            res["assert_in_src"].append((r, n.lineno))
        if isinstance(n, ast.Global):
            res["global_stmt"].append((r, n.lineno, ",".join(n.names)))
    stats[r] = s
    # regex-based
    for i, l in enumerate(lines, 1):
        if re.search(r"\b(TODO|FIXME|HACK|XXX)\b", l):
            res["todo"].append((r, i, l.strip()[:120]))
        if re.search(r"0\.0\.0\.0", l):
            res["bind_all"].append((r, i, l.strip()[:140]))
        if re.search(r"(?i)(password|passwd|secret|token|api[_-]?key)\s*=\s*[\"'][^\"'{}$<> ]{6,}[\"']", l) and "os.environ" not in l and "getenv" not in l:
            res["possible_hardcoded_secret"].append((r, i, l.strip()[:140]))
        if re.search(r"(execute|text)\(\s*f[\"']", l) or re.search(r"(?i)[\"'](select|insert|update|delete)\b[^\"']*[\"']\s*(%|\.format\()", l):
            res["sql_string_format"].append((r, i, l.strip()[:160]))
        if re.search(r"[A-Za-z]:[\\/](Projects|PostgreSQLData|AK[A-Z0-9]+|Maintenance|Users)", l):
            res["windows_abs_path"].append((r, i, l.strip()[:140]))

summary = {k: len(v) for k, v in res.items()}
out.write_text(json.dumps({"summary": summary, "findings": res, "stats": stats}, ensure_ascii=False, indent=1))
print(json.dumps(summary, indent=1, ensure_ascii=False))
tot = collections.Counter()
for r, s in stats.items():
    top = r.split("/")[0]
    tot[top + "_files"] += 1; tot[top + "_loc"] += s["loc"]; tot[top + "_funcs"] += s["funcs"]; tot[top + "_long"] += s["long_funcs"]; tot[top + "_semis"] += s["semis"]; tot[top + "_fnimports"] += s["fn_imports"]; tot[top + "_crlf"] += int(s["crlf"])
print(json.dumps(dict(sorted(tot.items())), indent=1))
