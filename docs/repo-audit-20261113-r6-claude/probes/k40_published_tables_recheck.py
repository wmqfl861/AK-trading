"""Independent arithmetic re-check of the published K40 tables (standard library only).

It reads only the files committed under ``docs/research/``. The accepted per-day
outputs are outside the repository, so this checks what any reader of the
repository can check:

* every figure printed in ``v1-daily-baseline-rb.md`` equals the CSV cell it comes from;
* the CSV files agree with one another (yearly rows add up to the seven-year totals,
  net = gross - fee - slippage, doubled fee/slippage change the net by exactly the
  base fee/slippage, the pairwise and control tables are differences of the totals);
* every reference recorded in ``CHECK.json`` points at a cell holding that value.

Run from the repository root:

    python docs/repo-audit-20261113-r6-claude/probes/k40_published_tables_recheck.py

Exit 0 = every check passed. At ``3fc62d2``: 7,195 checks, 0 failed.
"""
from __future__ import annotations

import csv
import json
import sys
from decimal import Decimal as D
from pathlib import Path

ROOT = Path("docs/research")
DIR = ROOT / "v1-daily-baseline-rb"
STRATS = ("donchian_trend", "ma_trend", "zscore_reversion")
checks = 0
failures: list[str] = []

def load(name):
    with open(DIR / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))

def dec(text):
    return None if text in ("", None) else D(text)

def ok(cond, label):
    global checks
    checks += 1
    if not cond:
        failures.append(label)

year = {v: load(f"year-{v.lower()}.csv") for v in ("ALL", "PRIMARY")}
paired = load("paired-years.csv")
concl = load("conclusions.csv")
summ = load("sensitivity-summary.csv")
syear = load("sensitivity-year.csv")
ctrl = load("control-comparison.csv")
bconcl = load("b-conclusions.csv")
scen = load("scenario-base.csv")
cap = load("capital-stress.csv")
causal = load("causal-regime.csv")
scope = {(r["source"], r["metric"]): r["value"] for r in load("scope.csv")}
bscope = {(r["source"], r["metric"]): r["value"] for r in load("b-scope.csv")}

# 1. every row: net = gross - fee - slippage ; profit+zero+loss = n
for name, rows, netcol in (("year-all", year["ALL"], "net_sum"), ("year-primary", year["PRIMARY"], "net_sum"),
                           ("paired", paired, "net_sum"), ("scenario-base", scen, "net_sum"),
                           ("capital-stress", cap, "net_sum"), ("causal", causal, "net_sum"),
                           ("sens-summary", summ, "net_sum"), ("sens-year", syear, "net_sum")):
    for i, r in enumerate(rows):
        g, f, s, n = dec(r.get("gross_sum")), dec(r.get("fee_sum")), dec(r.get("slippage_sum")), dec(r[netcol])
        if None not in (g, f, s, n):
            ok(g - f - s == n, f"{name}[{i}] net=gross-fee-slip")
        if all(k in r for k in ("profit", "zero", "loss", "n")) and r["n"] != "":
            ok(int(r["profit"]) + int(r["zero"]) + int(r["loss"]) == int(r["n"]), f"{name}[{i}] counts")

# 2. seven-year totals from the A tables equal the B matrix at C1000000/BASE
idx_sum = {(r["capital"], r["scenario"], r["method"], r["view"]): r for r in summ}
for view in ("ALL", "PRIMARY"):
    for s in STRATS:
        rows = [r for r in year[view] if r["strategy"] == s]
        ok(len(rows) == 7, f"{view}/{s} seven years")
        total = sum(dec(r["net_sum"]) for r in rows)
        b = idx_sum[("1000000", "BASE", s, view)]
        ok(total == dec(b["net_sum"]), f"A yearly total == B base {view}/{s}: {total} vs {b['net_sum']}")
        for col in ("gross_sum", "fee_sum", "slippage_sum"):
            ok(sum(dec(r[col]) for r in rows) == dec(b[col]), f"{view}/{s} {col}")
        ok(sum(int(r["fills"]) for r in rows) == int(b["fills"]), f"{view}/{s} fills")
        ok(sum(int(r["observations"]) for r in rows) == int(b["observations"]), f"{view}/{s} observations")

# 3. B yearly rows sum to B summary rows; A yearly == B yearly at C1000000/BASE
idx_year = {}
for r in syear:
    idx_year.setdefault((r["capital"], r["scenario"], r["method"], r["view"]), []).append(r)
for key, rows in idx_year.items():
    s = idx_sum[key]
    for col in ("net_sum", "gross_sum", "fee_sum", "slippage_sum"):
        ok(sum(dec(r[col]) for r in rows) == dec(s[col]), f"B year->summary {key} {col}")
    for col in ("fills", "opening_fills", "opening_attempts", "opening_blocks", "observations", "terminal_close_fills"):
        ok(sum(int(r[col]) for r in rows) == int(s[col]), f"B year->summary {key} {col}")
for view in ("ALL", "PRIMARY"):
    for a in year[view]:
        b = [r for r in idx_year[("1000000", "BASE", a["strategy"], view)] if r["raw_year"] == a["raw_year"]]
        ok(len(b) == 1, "one B row per A year")
        for ca, cb in (("net_sum", "net_sum"), ("median", "net_median"), ("min", "net_min"), ("max", "net_max"),
                       ("n", "n"), ("profit", "profit"), ("zero", "zero"), ("loss", "loss"), ("fills", "fills"),
                       ("gross_sum", "gross_sum"), ("fee_sum", "fee_sum"), ("slippage_sum", "slippage_sum")):
            ok(dec(a[ca]) == dec(b[0][cb]), f"A==B {view} {a['raw_year']} {a['strategy']} {ca}")

# 4. cost identities across scenarios (signals are stated to be identical across cells)
for capital in ("1000000", "300000", "100000", "50000"):
    for m in STRATS + ("flat_control", "buy_hold_control"):
        for view in ("ALL", "PRIMARY"):
            base = idx_sum[(capital, "BASE", m, view)]
            fee2 = idx_sum[(capital, "FEE_X2", m, view)]
            sl2 = idx_sum[(capital, "SLIPPAGE_X2", m, view)]
            mg = idx_sum[(capital, "MARGIN_X1_5", m, view)]
            ok(dec(fee2["fee_sum"]) == 2 * dec(base["fee_sum"]), f"fee x2 {capital}/{m}/{view}")
            ok(dec(sl2["slippage_sum"]) == 2 * dec(base["slippage_sum"]), f"slip x2 {capital}/{m}/{view}")
            ok(dec(fee2["net_sum"]) == dec(base["net_sum"]) - dec(base["fee_sum"]), f"fee x2 net {capital}/{m}/{view}")
            ok(dec(sl2["net_sum"]) == dec(base["net_sum"]) - dec(base["slippage_sum"]), f"slip x2 net {capital}/{m}/{view}")
            ok(dec(mg["net_sum"]) == dec(base["net_sum"]), f"margin x1.5 net {capital}/{m}/{view}")
            ok(dec(base["slippage_sum"]) == 10 * int(base["fills"]), f"slippage = 10 per fill {capital}/{m}/{view}")
            for other in (fee2, sl2, mg):
                ok(other["gross_sum"] == base["gross_sum"] and other["fills"] == base["fills"], f"same trades {capital}/{m}/{view}")
            c1 = idx_sum[("1000000", "BASE", m, view)]
            ok(base["net_sum"] == c1["net_sum"] and base["fills"] == c1["fills"], f"capital-invariant {capital}/{m}/{view}")

# 5. paired tables = differences of yearly sums
for r in paired:
    ya = [x for x in year[r["view"]] if x["raw_year"] == r["raw_year"] and x["strategy"] == r["strategy_a"]][0]
    yb = [x for x in year[r["view"]] if x["raw_year"] == r["raw_year"] and x["strategy"] == r["strategy_b"]][0]
    ok(dec(r["net_sum"]) == dec(ya["net_sum"]) - dec(yb["net_sum"]), f"paired {r['raw_year']} {r['view']} {r['strategy_a']}-{r['strategy_b']}")

# 6. conclusions.csv recomputed from the yearly tables
for r in concl:
    rows = [x for x in year[r["view"]] if x["strategy"] == r["strategy"]]
    pos = sum(dec(x["net_sum"]) > 0 for x in rows); neg = sum(dec(x["net_sum"]) < 0 for x in rows)
    worst = min(rows, key=lambda x: dec(x["net_sum"]))
    ok(int(r["positive_years"]) == pos and int(r["negative_years"]) == neg, f"conclusions counts {r['strategy']}/{r['view']}")
    ok(r["worst_year"] == worst["raw_year"] and dec(r["worst_net"]) == dec(worst["net_sum"]), f"conclusions worst {r['strategy']}/{r['view']}")

# 7. b-conclusions recomputed
for r in bconcl:
    m = r["method"]
    cells = [dec(idx_sum[(c, s, m, "ALL")]["net_sum"]) for c in ("1000000", "300000", "100000", "50000")
             for s in ("BASE", "FEE_X2", "SLIPPAGE_X2", "MARGIN_X1_5")]
    ok(len(cells) == 16 and min(cells) == dec(r["ALL_min_net"]) and max(cells) == dec(r["ALL_max_net"]), f"b-conclusions range {m}")
    blocks = max(int(idx_sum[(c, s, m, "ALL")]["opening_blocks"]) for c in ("1000000", "300000", "100000", "50000")
                 for s in ("BASE", "FEE_X2", "SLIPPAGE_X2", "MARGIN_X1_5"))
    ok(blocks == int(r["ALL_max_opening_blocks"]), f"b-conclusions blocks {m}")
    bh = dec(idx_sum[("1000000", "BASE", "buy_hold_control", "ALL")]["net_sum"])
    ok(dec(idx_sum[("1000000", "BASE", m, "ALL")]["net_sum"]) - bh == dec(r["BASE_ALL_strategy_minus_buy_hold"]), f"b-conclusions vs buy-hold {m}")
    for view, col in (("ALL", "ALL_positive_years"), ("PRIMARY", "PRIMARY_positive_years")):
        pos = sum(dec(x["net_sum"]) > 0 for x in idx_year[("1000000", "BASE", m, view)])
        ok(pos == int(r[col]), f"b-conclusions positive years {m}/{view}")

# 8. control comparison = strategy minus control, per scope
ctrl_idx = {(r["capital"], r["scenario"], r["strategy"], r["control"], r["view"], r["scope"]): r for r in ctrl}
for (capital, scenario, s, control, view, sc), r in ctrl_idx.items():
    if sc == "ALL_YEARS":
        left = dec(idx_sum[(capital, scenario, s, view)]["net_sum"])
        right = dec(idx_sum[(capital, scenario, control, view)]["net_sum"])
    else:
        left = [dec(x["net_sum"]) for x in idx_year[(capital, scenario, s, view)] if x["raw_year"] == sc][0]
        right = [dec(x["net_sum"]) for x in idx_year[(capital, scenario, control, view)] if x["raw_year"] == sc][0]
    ok(dec(r["net_sum"]) == left - right, f"control {capital}/{scenario}/{s}/{control}/{view}/{sc}")

# 9. matrix scope arithmetic
ok(int(bscope[("matrix", "accounts")]) == 84 * 5 * 16, "accounts")
ok(int(bscope[("matrix", "strategy_accounts")]) == 84 * 3 * 16, "strategy accounts")
ok(int(bscope[("matrix", "ALL_observations")]) == 17847 * 5 * 16, "ALL observations")
ok(int(bscope[("matrix", "contract_year_cells")]) == 84 * 5 * 16 * 7 * 2, "contract-year cells")
ok(scope[("chronological_ALL", "observed_contracts")] == "84" and scope[("chronological_ALL", "observations_per_strategy")] == "17847", "A scope")

# 10. every reference in CHECK.json points at a CSV cell holding that value
#     ("row" counts like a spreadsheet: the header is row 1)
check = json.loads((DIR / "CHECK.json").read_text(encoding="utf-8"))
report = (ROOT / "v1-daily-baseline-rb.md").read_text(encoding="utf-8")
refs = check["report_value_refs"]
tables: dict[str, list[dict[str, str]]] = {}
wrong = 0
for ref in refs:
    rows = tables.setdefault(ref["csv"], load(ref["csv"]))
    index = ref["row"] - 2
    if not 0 <= index < len(rows) or str(rows[index].get(ref["column"])) != str(ref["value"]):
        wrong += 1
ok(wrong == 0, f"CHECK.json references match their CSV cells (mismatches {wrong})")
not_printed = sorted({str(r["value"]) for r in refs if str(r["value"]) not in report})

# 11. report tables parsed independently and compared with the CSVs
def table_after(marker):
    """Body rows of the first table that follows the line containing ``marker``."""
    lines = report[report.index(marker):].splitlines()
    out, seen = [], False
    for line in lines[1:]:
        if line.startswith("|"):
            seen = True
            out.append([c.strip() for c in line.strip().strip("|").split("|")])
        elif seen:
            break
    return out[2:]  # drop the header and the separator


def table_headed(header_prefix):
    """Body rows of the table whose header line starts with ``header_prefix``."""
    lines = report.splitlines()
    start = next(k for k, line in enumerate(lines) if line.startswith(header_prefix))
    out = []
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        out.append([c.strip() for c in line.strip().strip("|").split("|")])
    return out


def yearly(view):
    rows = table_after(f"**年度 {view}**")
    n = 0
    for row in rows:
        y = row[0].rstrip("*")
        for s, cellx in zip(STRATS, row[1:]):
            cnt, net, med, pzl = cellx.split("；")
            src = [x for x in year[view] if x["raw_year"] == y and x["strategy"] == s][0]
            ok(int(cnt) == int(src["n"]) and D(net) == dec(src["net_sum"]) and D(med) == dec(src["median"])
               and pzl == f"{src['profit']}/{src['zero']}/{src['loss']}", f"report yearly {view} {y} {s}")
            n += 1
    ok(n == 21, f"report yearly {view} has 21 cells")
yearly("ALL"); yearly("PRIMARY")

rows = table_after("**两两有向净额差")
pairs = (("donchian_trend", "ma_trend"), ("donchian_trend", "zscore_reversion"), ("ma_trend", "zscore_reversion"))
for row in rows:
    for (a, b), cellx in zip(pairs, row[1:]):
        allv, priv = [D(x.strip()) for x in cellx.split("/")]
        for view, val in (("ALL", allv), ("PRIMARY", priv)):
            src = [x for x in paired if x["raw_year"] == row[0] and x["view"] == view and x["strategy_a"] == a and x["strategy_b"] == b][0]
            ok(val == dec(src["net_sum"]), f"report paired {row[0]} {view} {a}-{b}")

for row in table_headed("| 初始资金（元）"):
    for s, cellx in zip(STRATS, row[1:]):
        net, med, frac = cellx.split("；")
        src = idx_sum[(row[0], "BASE", s, "ALL")]
        ok(D(net) == dec(src["net_sum"]) and D(med) == dec(src["net_median"])
           and frac == f"{src['opening_blocks']}/{src['opening_attempts']}", f"report capital {row[0]} {s}")
for row in table_headed("| 单项成本情景"):
    for s, cellx in zip(STRATS, row[1:]):
        net, diff = [D(x) for x in cellx.split("；")]
        src = idx_sum[("1000000", row[0], s, "ALL")]
        ok(net == dec(src["net_sum"]) and diff == dec(src["vs_same_capital_BASE_net_sum"])
           and diff == net - dec(idx_sum[("1000000", "BASE", s, "ALL")]["net_sum"]), f"report cost {row[0]} {s}")
names = {"唐奇安": "donchian_trend", "均线": "ma_trend", "Z 回归": "zscore_reversion", "始终空仓": "flat_control", "买入持有": "buy_hold_control"}
for row in table_headed("| 方法 | 净额合计"):
    m = names[row[0]]
    src = idx_sum[("1000000", "BASE", m, "ALL")]
    ok(D(row[1]) == dec(src["net_sum"]) and D(row[2]) == dec(src["net_median"]) and int(row[3]) == int(src["opening_accounts_in_view"])
       and int(row[4]) == int(src["opening_fills"]) and int(row[5]) == int(src["opening_blocks"]), f"report control row {m}")
    if m in STRATS:
        ok(D(row[6]) == dec(src["net_sum"]) - dec(idx_sum[("1000000", "BASE", "buy_hold_control", "ALL")]["net_sum"]), f"report minus buy-hold {m}")
for row in table_headed("| 初始资金；保证金率；情景"):
    cash, rate, scenario = row[0].split("；")
    for s, cellx in zip(STRATS, row[1:]):
        net, blocks = cellx.split("；")
        src = [x for x in cap if x["initial_cash"] == cash and x["scenario"] == scenario and x["strategy"] == s][0]
        ok(D(net) == dec(src["net_sum"]) and int(blocks) == int(src["opening_blocks"]) and D(rate) == dec(src["margin_rate"]), f"report old capital {row[0]} {s}")
for row in table_headed("| 既有状态（PRIMARY）"):
    for s, cellx in zip(STRATS, row[1:]):
        src = [x for x in causal if x["window"] == "PRIMARY" and x["regime"] == row[0] and x["strategy"] == s][0]
        ok(D(cellx) == dec(src["net_sum"]), f"report causal {row[0]} {s}")

print(f"checks run: {checks}; failed: {len(failures)}")
for f in failures[:40]:
    print("  FAIL", f)
print(f"CHECK.json references: {len(refs)}; values no longer printed in the report: {not_printed}")
# figures used in the audit text
for s in STRATS + ("buy_hold_control",):
    r = idx_sum[("1000000", "BASE", s, "ALL")]
    print(f"{s:18s} net={r['net_sum']:>12s} per-account={dec(r['net_sum'])/84:.1f} gross={r['gross_sum']} fee={r['fee_sum']} slip={r['slippage_sum']} opens={r['opening_fills']} winners/zero/losers={r['profit']}/{r['zero']}/{r['loss']}")
sys.exit(1 if failures else 0)
