"""K43: aggregate accepted daily accounts using previous stored-bar open interest.

Standard library only. No engine, account, network, environment or subprocess API.
Historical information availability is NOT verified. This is a SCENARIO accounting
selection, not a continuous capital replay. A separate terminated-process receipt
is required by the ``seal`` command before a successful CHECK can be published.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, Inexact, InvalidOperation, ROUND_HALF_EVEN, localcontext
from pathlib import Path
from types import SimpleNamespace


STRATEGIES = ("donchian_trend", "ma_trend", "zscore_reversion")
CONTROLS = ("buy_hold_control", "flat_control")
METHODS = STRATEGIES + CONTROLS
NAMES = dict(zip(METHODS, ("唐奇安突破", "均线", "Z 分数回归", "买入持有", "始终空仓")))
YEARS = tuple(range(2020, 2027))
CUTOFF = date(2026, 9, 5)
SHANGHAI = timezone(timedelta(hours=8))
INITIAL = Decimal("1000000")
ZERO = Decimal(0)
SCHEMA = "aktr-v1-daily-baseline-report/v3"
RULE = "previous_stored_completed_bar_close_oi_v1"
REPORT = "v1-daily-baseline-rb.md"
NEW_CSV = ("dominant-summary.csv", "dominant-year.csv", "dominant-scope.csv")
OLD_CSV = ("year-all.csv", "year-primary.csv", "paired-years.csv", "scenario-base.csv",
           "capital-stress.csv", "causal-regime.csv", "conclusions.csv", "scope.csv",
           "sensitivity-summary.csv", "sensitivity-year.csv", "control-comparison.csv",
           "b-conclusions.csv", "b-scope.csv")
GOAL = Path("docs/development-goal-20261026/CHRONOLOGICAL-ACCEPTANCE.json")
PUBLIC = Path("docs/research/v1-daily-baseline-rb")
SAFETY = dict(enable_trading=False, observer="hold", broker="NOT_RUN",
              real_order_send_calls=0, real_order_cancel_calls=0)
MODE = dict(new_simulation=False, new_training=False, holdout_read=False,
            evidence_level="SCENARIO", statistical_qualification="NOT_ASSESSED",
            timing_verified=False, historical_known_at=None,
            information_time_basis="PREVIOUS_STORED_COMPLETED_BAR_SCENARIO")


class ValidationError(ValueError):
    """A source, support, accounting or custody requirement was not met."""


def need(condition, message):
    if not condition:
        raise ValidationError(message)


def eq(actual, expected, message):
    need(actual == expected, message)


def path(value):
    need(isinstance(value, (str, os.PathLike)) and bool(str(value)), "invalid path type/value")
    p = Path(value)
    need(".." not in p.parts, "path traversal is not allowed")
    return p.resolve()


def child(root, relative):
    root = path(root)
    p = Path(relative)
    need(not p.is_absolute() and ".." not in p.parts, "invalid relative source member")
    result = path(root / p)
    need(result.is_relative_to(root), "source member escapes root")
    return result


def pin(raw):
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def unique(pairs):
    result = {}
    for k, v in pairs:
        need(k not in result, "duplicate JSON key: " + k)
        result[k] = v
    return result


def loads(raw):
    def invalid(value):
        raise ValidationError("nonfinite JSON value: " + value)
    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def rows(raw):
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    need(reader.fieldnames and len(reader.fieldnames) == len(set(reader.fieldnames)), "invalid CSV columns")
    result = list(reader)
    need(all(None not in r and all(v is not None for v in r.values()) for r in result), "ragged CSV")
    return result


def amount(value):
    need(isinstance(value, (str, Decimal)) and str(value).strip() == str(value) and bool(str(value)),
         "amount must be an exact nonempty string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValidationError("invalid amount") from exc
    need(result.is_finite(), "nonfinite amount")
    return result


def integer(value):
    need(type(value) is int or isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]*", value),
         "invalid nonnegative integer")
    result = int(value)
    need(result >= 0, "negative integer")
    return result


def flag(value):
    need(value in ("true", "false"), "invalid boolean string")
    return value == "true"


def cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        need(value.is_finite(), "nonfinite output")
        return "0" if not value else format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else format(value, "f")
    return str(value)


def csv_bytes(records, columns=None):
    need(records or columns, "CSV needs explicit columns when empty")
    columns = list(columns or records[0])
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for record in records:
        eq(set(record), set(columns), "CSV record columns")
        writer.writerow({k: cell(record[k]) for k in columns})
    return buffer.getvalue().encode("utf-8")


def write_new(target, raw):
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as stream:
        stream.write(raw)


def atomic_new(target, raw):
    need(not target.exists(), "final output already exists")
    pending = target.with_name(target.name + ".tmp")
    need(not pending.exists(), "residual final temporary file")
    write_new(pending, raw)
    # os.rename refuses an existing destination on Windows; the preceding guard
    # also makes accidental reuse explicit on other supported platforms.
    need(not target.exists(), "final output appeared during publication")
    pending.rename(target)


class Sources:
    def __init__(self):
        self.records = {}
        self.locations = {}

    def read(self, name, filename, expected):
        p = path(filename)
        need(p.is_file(), "missing required source: " + name)
        raw = p.read_bytes()
        expected = {"sha256": expected} if isinstance(expected, str) else expected
        need(isinstance(expected, dict) and re.fullmatch(r"[0-9a-f]{64}", expected.get("sha256", "")),
             "invalid frozen source hash: " + name)
        actual = pin(raw)
        for k in ("sha256", "bytes"):
            if k in expected:
                eq(actual[k], expected[k], "source pin mismatch: " + name + "." + k)
        if name in self.records:
            eq(actual, self.records[name], "repeated source drift: " + name)
            eq(str(p), self.locations[name], "repeated source identity: " + name)
        self.records[name] = actual
        self.locations[name] = str(p)
        return raw

    def verify(self):
        for name, expected in self.records.items():
            p = Path(self.locations[name])
            need(p.is_file(), "source disappeared: " + name)
            eq(pin(p.read_bytes()), expected, "source changed: " + name)


def manifest_hash(statement):
    found = re.findall(r"\bmanifest ([0-9a-f]{64})(?![0-9a-f])", statement)
    need(len(found) == 1, "ambiguous accepted manifest reference")
    return found[0]


def accepted(verdict):
    need(verdict.get("verdict") == "PASS" and verdict.get("unconditional") is True
         and verdict.get("conditional") is False, "A/B acceptance is not unconditional PASS")


def load_sources(source_repo, source_bindings, bindings_sha256, output_dir, *, sealing=False):
    repo, binding_path, output = map(path, (source_repo, source_bindings, output_dir))
    need(repo.is_dir(), "missing source repository")
    need(output.is_dir() if sealing else not output.exists(), "invalid output directory state")
    sources = Sources()
    b = loads(sources.read("source-bindings", binding_path, bindings_sha256))
    eq(b["schema"], "k43-author-plan-source-bindings/v1", "binding schema")

    def bound(key, discovered=None, expected=None):
        record = b[key]
        if discovered is not None:
            eq(path(discovered), path(record["path"]), "binding locator: " + key)
        if expected is not None:
            expected = {"sha256": expected} if isinstance(expected, str) else expected
            for k in ("bytes", "sha256"):
                if k in expected:
                    eq(record[k], expected[k], "binding accepted pin: " + key)
        return sources.read(key, record["path"], record)

    acceptance = loads(bound("acceptance", repo / GOAL))
    need(acceptance["independent_verdict"] == "PASS" and acceptance["conditional"] is False,
         "chronological acceptance is not PASS")
    authority = acceptance["published_evidence"]
    verdict = loads(bound("chronological_verdict", authority["VERDICT.json"]["source"]["path"],
                          authority["VERDICT.json"]["source"]))
    need(verdict["verdict"] == "PASS" and verdict["conditional"] is False, "chronological verdict")
    command = loads(bound("root_command", authority["ROOT-COMMAND.json"]["source"]["path"],
                          authority["ROOT-COMMAND.json"]["source"]))
    # Interpret the old command strictly as data; never execute it.
    argv = command["argv"]
    binds = [argv[i + 1].rsplit("=", 1) for i, token in enumerate(argv[:-1]) if token == "--bind-file"]
    staging_refs = [(p, h) for p, h in binds if Path(p).name == "STAGING.json"]
    need(len(staging_refs) == 1, "one accepted STAGING reference required")
    staging = loads(bound("staging", staging_refs[0][0], staging_refs[0][1]))
    eq(path(command["cwd"]), path(staging["candidate"]), "historical cwd identity")
    eq(path(command["cwd"]), path(b["historical_cwd"]), "binding historical cwd")
    original = child(path(b["chronological_verdict"]["path"]).parent,
                     verdict["basis"]["independent_fresh_replay"]["output_root"])
    eq(verdict["basis"]["independent_fresh_replay"]["output_root"],
       acceptance["validation"]["independent_fresh_replay"]["output_root"], "accepted replay root")
    mh = manifest_hash(acceptance["validation"]["reproduction"])
    eq(mh, manifest_hash(verdict["basis"]["reproduction"]), "chronological manifest references")
    manifest = loads(bound("chronological_manifest", original / "manifest.json", mh))
    coverage = loads(bound("chronological_coverage", original / "coverage.json", manifest["content_hashes"]["coverage.json"]))
    daily_raw = bound("chronological_daily", original / "daily_attribution.csv", manifest["content_hashes"]["daily_attribution.csv"])
    inputs_refs = [m for m in staging["members"] if Path(m["path"]).name == "INPUTS.json"]
    need(len(inputs_refs) == 1, "one original INPUTS required")
    inputs_ref = inputs_refs[0]
    eq(inputs_ref["sha256"], manifest["input_inventory_sha256"], "INPUTS accepted inventory pin")
    inputs = loads(sources.read("original.INPUTS", inputs_ref["path"], inputs_ref))
    eq(inputs["schema"], "rb-daily-chronological-transfer-inputs/v1", "INPUTS schema")
    bundle_root = path(inputs["bundle"]["path"])
    input_manifest = loads(bound("input_manifest", bundle_root / "manifest.json", inputs["bundle"]["manifest_sha256"]))
    eq(b["input_manifest"]["sha256"], manifest["input_manifest_sha256"], "accepted input manifest")
    for expected in (inputs["bundle"]["content_hashes"], manifest["input_content_hashes"]):
        eq(input_manifest["content_hashes"], expected, "accepted four input payloads")
    eq(set(input_manifest["content_hashes"]), {"bars.csv", "contracts.json", "metadata.json", "availability.json"}, "input members")
    input_raw = {}
    for name, expected in sorted(input_manifest["content_hashes"].items()):
        record = b["input_payloads"][name]
        eq(path(record["path"]), bundle_root / name, "bound input member")
        eq(record["sha256"], expected, "bound input hash")
        input_raw[name] = sources.read("input." + name, record["path"], record)
    a = loads(bound("K40A_verdict")); accepted(a)
    afref = a["frozen_owner_manifest"]
    af = loads(sources.read("K40A.freeze", afref["path"], afref))
    aroot = path(afref["path"]).parent / "files"
    prior = {}
    for relative, expected in a["reviewed_files"].items():
        eq(af["files"][relative], expected, "accepted A frozen member")
        raw = sources.read("A." + relative, child(aroot, relative), expected)
        if relative.startswith("docs/research/"):
            prior[Path(relative).name] = raw
    ac = loads(prior["CHECK.json"])
    achain = {x["logical_id"]: x["actual"] for x in ac["sources"]}
    for aid, bid in (("chronological.acceptance", "acceptance"), ("chronological.manifest", "chronological_manifest"),
                     ("chronological.daily_attribution.csv", "chronological_daily")):
        eq(achain[aid], sources.records[bid], "A accepted chronological lineage")
    bv = loads(bound("K40B_verdict")); accepted(bv)
    bf = loads(bound("K40B_freeze", bv["frozen_owner_manifest"]["path"], bv["frozen_owner_manifest"]))
    bmref = bf["research_payload_manifest"]
    eq({k: bmref[k] for k in ("bytes", "sha256")},
       {k: bv["research_manifest"][k] for k in ("bytes", "sha256")}, "B owner/reviewer manifest identity")
    bm = loads(bound("K40B_manifest", bmref["path"], bmref))
    broot = path(bmref["path"]).parent
    bc = loads(bound("K40B_CHECK", bf["final_CHECK"]["path"], bv["final_CHECK"]))
    eq(bc["raw_exit"], 0, "B aggregation exit")
    eq(bc["status"], "SEALED_MATRIX_EXIT_0", "B sealed status")
    eq(bc["new_simulation"], True, "historical B simulation fact")
    eq(bc["matrix_manifest"], sources.records["K40B_manifest"], "B CHECK manifest")
    eq(bc["sources"]["chronological.manifest"]["sha256"], mh, "B/A chronological lineage")
    for relative, expected in bv["reviewed_files"].items():
        eq(bf["files"][relative], expected, "B frozen reviewed member")
        if relative.startswith("docs/research/") and Path(relative).name != "CHECK.json":
            name = Path(relative).name
            eq(bm["payloads"]["publish/" + name], expected, "B published manifest pin")
            prior[name] = sources.read("B.publish." + name, broot / "publish" / name, expected)
    prior["CHECK.json"] = json_bytes(bc)
    # The published old CSV and old tools/tests/config must remain byte-identical.
    protected = {k: v for k, v in af["files"].items() if k.endswith(".py") or k.endswith(".csv")}
    protected.update({k: v for k, v in bf["files"].items() if k.endswith((".py", ".csv")) or k.startswith("config/")})
    for relative, expected in sorted(protected.items()):
        sources.read("protected." + relative, child(repo, relative), expected)
    eq({Path(k).name for k in protected if k.endswith(".csv")}, set(OLD_CSV), "13 preserved CSV files")
    contracts = coverage["contracts"]
    need(len(contracts) == 84, "fixed accepted support must contain 84 contracts")
    symbols = {c["symbol"]: c["contract_instance_id"] for c in contracts}
    eq(len(symbols), len(contracts), "unique accepted symbols")
    controls = {k: v for k, v in bm["payloads"].items() if re.fullmatch(
        r"accounts/C1000000/BASE/[^/]+/(buy_hold_control|flat_control)/ledger\.csv", k)}
    eq(controls, b["K40B_controls"], "exact accepted control ledger set")
    expected_controls = {f"accounts/C1000000/BASE/{s}/{m}/ledger.csv" for s in symbols for m in CONTROLS}
    eq(set(controls), expected_controls, "two BASE control accounts per accepted contract")
    control_raw = {name: sources.read("control." + name, child(broot, name), expected)
                   for name, expected in sorted(controls.items())}
    source_paths = [Path(p) for p in sources.locations.values()]
    for root in (repo, path(__file__).parents[2], binding_path.parent, broot, original, bundle_root,
                 path(afref["path"]).parent, path(b["K40B_freeze"]["path"]).parent):
        need(not output.is_relative_to(root) and not root.is_relative_to(output), "output overlaps protected source")
    need(not any(p.is_relative_to(output) or output == p for p in source_paths), "output overlaps source file")
    tool_raw = Path(__file__).read_bytes()
    sources.read("K43.tool", Path(__file__), pin(tool_raw))
    sources.verify()
    return SimpleNamespace(repo=repo, output=output, bindings=b, bindings_sha256=bindings_sha256,
                           sources=sources, symbols=symbols, coverage=coverage, daily_raw=daily_raw,
                           control_raw=control_raw, input_raw=input_raw, prior=prior, prior_check=bc,
                           acceptance=acceptance, historical_a=pin(json_bytes(ac)))


def timestamp(value):
    need(isinstance(value, str) and value, "missing timestamp")
    parsed = datetime.fromisoformat(value)
    need(parsed.utcoffset() == timedelta(hours=8), "raw timestamp must carry +08:00")
    return parsed


def nano(value):
    delta = timestamp(value).astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000


def guard_dates(records, raw_field):
    """Inspect every market date before any new accounting or selection statistic."""
    for record in records:
        need(timestamp(record[raw_field]).date() < CUTOFF, "HOLDOUT_BOUNDARY: raw market date")
        if record.get("trading_day"):
            need(date.fromisoformat(record["trading_day"]) < CUTOFF, "HOLDOUT_BOUNDARY: trading_day")


def identity(row):
    return tuple(row[k] for k in ("symbol", "contract_instance_id", "datetime_nano", "raw_ts", "raw_date", "j"))


def prepare_data(context):
    bar_rows, a_rows = rows(context.input_raw["bars.csv"]), rows(context.daily_raw)
    control_rows = {name: rows(raw) for name, raw in context.control_raw.items()}
    guard_dates(bar_rows, "ts")
    guard_dates(a_rows, "raw_ts")
    for records in control_rows.values():
        guard_dates(records, "raw_ts")
    declarations = loads(context.input_raw["contracts.json"])
    declared = {x["raw_symbol"]: x["instance_id"] for x in declarations if x["raw_symbol"] in context.symbols}
    eq(declared, context.symbols, "accepted contract declaration identity")
    eq(len([x for x in declarations if x["raw_symbol"] in context.symbols]), len(declared), "duplicate declaration")
    bars = defaultdict(list)
    for row in bar_rows:
        symbol = row["symbol"]
        if symbol not in context.symbols:
            continue
        eq(integer(row["duration_seconds"]), 86400, "daily bar duration")
        value = row["close_oi"]
        oi = None if value in ("", "NULL", "null") else amount(value)
        need(oi is None or oi >= 0, "negative close OI")
        ns = integer(row["datetime_nano"])
        eq(ns, nano(row["ts"]), "bar nano timestamp identity")
        raw_date = timestamp(row["ts"]).date().isoformat()
        need(date.fromisoformat(raw_date).year in YEARS, "unexpected observation year")
        bars[symbol].append(dict(symbol=symbol, contract_instance_id=context.symbols[symbol],
                                 datetime_nano=ns, raw_ts=row["ts"], raw_date=raw_date,
                                 close_oi=oi, available_at=row["available_at"]))
    eq(set(bars), set(context.symbols), "every accepted contract needs bars")
    base_index = {}
    for symbol, records in sorted(bars.items()):
        records.sort(key=lambda r: r["datetime_nano"])
        need(len({r["raw_date"] for r in records}) == len(records), "duplicate contract raw date")
        for j, record in enumerate(records):
            record["j"] = j
            if j:
                need(record["raw_date"] > records[j - 1]["raw_date"], "nonincreasing bar date")
            key = identity(record)
            need(key not in base_index, "duplicate bar identity")
            base_index[key] = record
    raw_accounts = defaultdict(list)
    for row in a_rows:
        need(row["strategy"] in STRATEGIES, "unexpected A strategy")
        raw_accounts[(row["symbol"], row["strategy"])].append(row)
    for name, records in control_rows.items():
        pieces = name.split("/")
        symbol, method = pieces[3], pieces[4]
        need(records, "empty control ledger")
        for row in records:
            eq((row["capital"], row["scenario"], row["symbol"], row["method"]),
               ("1000000", "BASE", symbol, method), "control method/capital/scenario identity")
        raw_accounts[(symbol, method)].extend(records)
    eq(set(raw_accounts), {(s, m) for s in bars for m in METHODS}, "five-method account support")
    accounts, nets = {}, {m: {} for m in METHODS}
    for (symbol, method), records in sorted(raw_accounts.items()):
        records.sort(key=lambda r: integer(r["j"]))
        eq(len(records), len(bars[symbol]), "ledger row support")
        previous_equity, previous_gross, previous_fee, previous_slip = INITIAL, ZERO, ZERO, ZERO
        converted = []
        for j, original in enumerate(records):
            row = dict(original)
            row["j"] = integer(row["j"])
            row["datetime_nano"] = integer(row["datetime_nano"])
            eq(row["j"], j, "continuous unique ledger j")
            eq(identity(row), identity(bars[symbol][j]), "exact OI/ledger support identity")
            eq(integer(row["raw_year"]), date.fromisoformat(row["raw_date"]).year, "raw year")
            eq(flag(row["primary_support"] if method in STRATEGIES else row["primary"]), j >= 60, "j60 boundary")
            eq(flag(row["terminal"]), j == len(records) - 1, "terminal row identity")
            gross_name, fee_name, net_name = ("gross_pnl", "fees", "net_pnl") if method in STRATEGIES else ("gross", "fee", "net")
            fields = {"equity", "previous_equity", "net_delta", "gross_delta", "fee_delta", "slippage_delta",
                      gross_name, fee_name, "slippage", net_name, "cash", "realized_pnl", "unrealized_pnl"}
            for f in fields:
                row[f] = amount(row[f])
            eq(row["previous_equity"], previous_equity, "previous equity across year/j60")
            eq(row["net_delta"], row["equity"] - previous_equity, "net equity increment")
            eq(row["net_delta"], row["gross_delta"] - row["fee_delta"] - row["slippage_delta"], "net increment accounting")
            eq(row[net_name], row["equity"] - INITIAL, "cumulative account net")
            eq(row["equity"], row["cash"] + row["unrealized_pnl"], "cash and unrealized equity")
            eq(row[gross_name], row["realized_pnl"] + row["unrealized_pnl"], "gross account accounting")
            for value, prior, delta in ((row[gross_name], previous_gross, row["gross_delta"]),
                                        (row[fee_name], previous_fee, row["fee_delta"]),
                                        (row["slippage"], previous_slip, row["slippage_delta"])):
                eq(value - prior, delta, "cumulative ledger increments")
            need(row["fee_delta"] >= 0 and row["slippage_delta"] >= 0, "negative ledger cost")
            if method in STRATEGIES:
                eq(amount(row["equity_delta"]), row["net_delta"], "A equity_delta")
            else:
                for f in ("terminal_close_fee_delta", "terminal_close_slippage_delta"):
                    value = amount(row[f])
                    need(value >= 0 and (j == len(records) - 1 or value == 0), "terminal cost identity")
            terminal_fills = integer(row["terminal_close_fills"])
            need(j == len(records) - 1 or terminal_fills == 0, "nonterminal close marker")
            key = identity(row)
            need(key not in nets[method], "duplicate ledger identity")
            nets[method][key] = row["net_delta"]
            previous_equity, previous_gross, previous_fee, previous_slip = row["equity"], row[gross_name], row[fee_name], row["slippage"]
            converted.append(row)
        accounts[(symbol, method)] = converted
    for method in METHODS:
        eq(set(nets[method]), set(base_index), "five methods share full observation support")
    return dict(bars), nets, accounts, len(bar_rows)


def select_dominant(bars):
    by_day = defaultdict(list)
    for symbol in sorted(bars):
        for j, current in enumerate(bars[symbol]):
            prior = bars[symbol][j - 1] if j else None
            record = dict(symbol=symbol, contract_instance_id=current["contract_instance_id"],
                          j=current["j"], datetime_nano=current["datetime_nano"], raw_ts=current["raw_ts"],
                          raw_date=current["raw_date"], prior_j=prior["j"] if prior else None,
                          prior_raw_ts=prior["raw_ts"] if prior else None,
                          prior_raw_date=prior["raw_date"] if prior else None,
                          prior_close_oi=cell(prior["close_oi"]) if prior and prior["close_oi"] is not None else None,
                          prior_available_at=prior["available_at"] if prior else None,
                          historical_known_at=None, timing_verified=False,
                          status="FIRST_STORED_BAR" if not prior else "MISSING_PREVIOUS_OI" if prior["close_oi"] is None else "ELIGIBLE")
            by_day[current["raw_date"]].append(record)
    selection = []
    for day, candidates in sorted(by_day.items()):
        candidates.sort(key=lambda r: (r["symbol"], r["contract_instance_id"]))
        eligible = [r for r in candidates if r["status"] == "ELIGIBLE"]
        maximum = max((amount(r["prior_close_oi"]) for r in eligible), default=None)
        tied = [r for r in eligible if amount(r["prior_close_oi"]) == maximum]
        chosen = tied[0] if tied else None
        selection.append(dict(raw_date=day, selected_contract=chosen["symbol"] if chosen else None,
                              selected_instance=chosen["contract_instance_id"] if chosen else None,
                              selected_key=list(identity(chosen)) if chosen else None,
                              status="SELECTED" if chosen else "NO_ELIGIBLE_PREVIOUS_OI",
                              eligible_candidates=len(eligible), max_prior_oi=cell(maximum) if maximum is not None else None,
                              max_oi_ties=len(tied), selected_by_tie_rule=len(tied) > 1,
                              candidates=candidates))
    return selection


def rounded_ratio(numerator, denominator, places, multiplier=1):
    need(denominator > 0, "nonpositive ratio denominator")
    with localcontext() as ctx:
        ctx.prec = 160
        ctx.traps[Inexact] = False
        value = numerator * Decimal(multiplier) / Decimal(denominator)
        return format(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN), "f")


def aggregate_tables(bars, nets, selection, accepted_summary):
    days = [x["raw_date"] for x in selection]
    covered = sum(x["selected_key"] is not None for x in selection)
    all_counts = len(bars)
    need(bool(days) and all_counts > 0, "empty observed support")
    summaries, annual, curves = [], [], []
    for method in METHODS:
        year_sum = {y: ZERO for y in YEARS}
        year_covered = {y: 0 for y in YEARS}
        cumulative, peak, maximum_dd = ZERO, ZERO, ZERO
        duration = maximum_duration = 0
        for item in selection:
            key = tuple(item["selected_key"]) if item["selected_key"] is not None else None
            selected_net = nets[method][key] if key is not None else None
            year = date.fromisoformat(item["raw_date"]).year
            if selected_net is not None:
                cumulative += selected_net
                year_sum[year] += selected_net
                year_covered[year] += 1
            peak = max(peak, cumulative)
            dd = peak - cumulative
            duration = duration + 1 if dd > 0 else 0
            maximum_duration = max(maximum_duration, duration)
            maximum_dd = max(maximum_dd, dd)
            curves.append(dict(method=method, raw_date=item["raw_date"], status=item["status"],
                               selected_contract=item["selected_contract"], selected_instance=item["selected_instance"],
                               selected_net=selected_net, cumulative_net=cumulative if covered else None,
                               running_peak=peak if covered else None, drawdown=dd if covered else None,
                               underwater_observations=duration if covered else None))
        eq(sum(year_sum.values(), ZERO), cumulative, "annual net conservation")
        observed_years = [y for y in YEARS if year_covered[y]]
        worst = min(observed_years, key=lambda y: (year_sum[y], y)) if observed_years else None
        all_net = sum(nets[method].values(), ZERO)
        accepted_rows = [r for r in accepted_summary if (r["method"], r["capital"], r["scenario"], r["view"]) ==
                         (method, "1000000", "BASE", "ALL")]
        need(len(accepted_rows) == 1, "one accepted full-account summary per method")
        eq(all_net, amount(accepted_rows[0]["net_sum"]), "accepted full-account net anchor")
        summaries.append(dict(product="SHFE.RB", method=method, method_name=NAMES[method],
                              status="COVERED" if covered else "NO_COVERAGE", net=cumulative if covered else None,
                              positive_years=sum(year_sum[y] > 0 for y in observed_years), covered_years=len(observed_years),
                              worst_year=worst, worst_year_net=year_sum[worst] if worst is not None else None,
                              max_drawdown=maximum_dd if covered else None,
                              longest_drawdown_observations=maximum_duration if covered else None,
                              open_drawdown_at_end=duration > 0 if covered else None,
                              covered_observations=covered, total_observations=len(days),
                              coverage_percent=rounded_ratio(Decimal(covered), len(days), 6, 100),
                              all_accounts=all_counts, all_accounts_net=all_net,
                              all_accounts_mean_numerator=all_net, all_accounts_mean_denominator=all_counts,
                              all_accounts_mean_display=rounded_ratio(all_net, all_counts, 3)))
        for year in YEARS:
            annual.append(dict(product="SHFE.RB", method=method, year=year,
                               year_status="PARTIAL_INPUT_WINDOW" if year in (2020, 2026) else "OBSERVED_RAW_YEAR",
                               status="COVERED" if year_covered[year] else "NO_COVERAGE",
                               net=year_sum[year] if year_covered[year] else None,
                               covered_observations=year_covered[year],
                               total_observations=sum(date.fromisoformat(d).year == year for d in days)))
    previous, switches, gaps, in_gap = None, 0, 0, False
    for item in selection:
        current = (item["selected_contract"], item["selected_instance"]) if item["selected_key"] else None
        if current is None:
            gaps += int(not in_gap)
            in_gap = True
        else:
            switches += int(previous is not None and current != previous)
            previous, in_gap = current, False
    candidates = [r for item in selection for r in item["candidates"]]
    distances = [(date.fromisoformat(r["raw_date"]) - date.fromisoformat(r["prior_raw_date"])).days
                 for r in candidates if r["prior_raw_date"] is not None]
    scope_values = dict(selection_rule=RULE, input_window_start=days[0], input_window_end=days[-1],
                        account_count=all_counts, method_count=len(METHODS), initial_cash=INITIAL, lots=1,
                        observations=len(days), covered_observations=covered, uncovered_observations=len(days) - covered,
                        coverage_numerator=covered, coverage_denominator=len(days),
                        coverage_percent=rounded_ratio(Decimal(covered), len(days), 6, 100),
                        switches=switches, uncovered_segments=gaps,
                        maximum_oi_tie_days=sum(x["selected_by_tie_rule"] for x in selection),
                        first_bar_exclusions=sum(r["status"] == "FIRST_STORED_BAR" for r in candidates),
                        missing_previous_oi_exclusions=sum(r["status"] == "MISSING_PREVIOUS_OI" for r in candidates),
                        zero_previous_oi_candidates=sum(r["status"] == "ELIGIBLE" and amount(r["prior_close_oi"]) == 0 for r in candidates),
                        maximum_previous_bar_calendar_gap_days=max(distances, default=0),
                        raw_year_start=YEARS[0], raw_year_end=YEARS[-1], **MODE, **SAFETY)
    scope = [dict(metric=k, value=cell(v)) for k, v in scope_values.items()]
    return {NEW_CSV[0]: summaries, NEW_CSV[1]: annual, NEW_CSV[2]: scope}, curves


class ReportValues:
    def __init__(self, tables):
        self.tables, self.refs = tables, []

    def value(self, filename, selector, field):
        matches = [(i, r) for i, r in enumerate(self.tables[filename]) if all(cell(r[k]) == cell(v) for k, v in selector.items())]
        need(len(matches) == 1, "ambiguous report cell reference")
        index, row = matches[0]
        value = cell(row[field])
        self.refs.append(dict(csv=filename, row=index + 2, column=field, value=value))
        return value or "空"

    def scope(self, metric):
        return self.value("dominant-scope.csv", {"metric": metric}, "value")

    def summary(self, method, field):
        return self.value("dominant-summary.csv", {"method": method}, field)


NUMBER = re.compile(r"(?<![A-Za-z0-9_])(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|-?[0-9]+(?:\.[0-9]+)?)(?![A-Za-z0-9_])")


def numeric_mapping(markdown, tables):
    """Map each visible numeric token to an exact published CSV cell.

    Markdown destinations are not prose. Dates are one token, never three
    independently invented numbers. Repeated equal cells use deterministic CSV
    ordering; semantic table/value references are also retained separately.
    """
    lookup = {}
    for filename, records in tables.items():
        for i, row in enumerate(records):
            for column, value in row.items():
                value = cell(value)
                if NUMBER.fullmatch(value):
                    lookup.setdefault(value, dict(csv=filename, row=i + 2, column=column, value=value))
    result = []
    for line_number, original in enumerate(markdown.splitlines(), 1):
        visible = re.sub(r"(?<=\]\()[^)]+(?=\))", lambda m: " " * len(m.group()), original)
        for match in NUMBER.finditer(visible):
            token = match.group()
            need(token in lookup, f"unmapped report number at line {line_number}: {token}")
            result.append(dict(line=line_number, start=match.start(), end=match.end(), **lookup[token]))
    return result


def render_report(tables, prior, prior_check):
    v = ReportValues(tables)
    s = v.summary
    lines = ["# 螺纹钢日线既有结果", "",
             f"本报告沿用 {v.scope('input_window_start')} 至 {v.scope('input_window_end')} 的 {v.scope('account_count')} 个螺纹钢合约，比较既定的五种做法，每个原账户初始资金 {v.scope('initial_cash')} 元、固定 {v.scope('lots')} 手，规则和参数未调整。",
             "主力口径的全期净额分别为" + "、".join(f"{NAMES[m]} {s(m, 'net')} 元" for m in METHODS) + "。",
             "有覆盖年份中净额为正的年数及最差年份分别为" + "；".join(
                 f"{NAMES[m]} {s(m, 'positive_years')}/{s(m, 'covered_years')} 年、{s(m, 'worst_year')} 年 {s(m, 'worst_year_net')} 元" for m in METHODS) + "。",
             "选择使用前一根已存完整日线的收盘持仓量，历史可用时刻没有核实，当天有日线也是事后支持条件，因此这里只作情景归集。",
             "合约切换直接采用新合约原账户的既有状态，保留原终点成本且没有新增换月成本，这不是一笔资本连续运行的可执行回放。", "",
             "| 品种与做法 | 主力净额（元） | 全部合约净额（元） | 正年数/有覆盖年数 | 最差年份 | 最差年净额（元） | 主力最大回撤（元） |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for m in METHODS:
        values = [s(m, f) for f in ("net", "all_accounts_net")]
        values += [s(m, "positive_years") + "/" + s(m, "covered_years")]
        values += [s(m, f) for f in ("worst_year", "worst_year_net", "max_drawdown")]
        lines.append("| 螺纹钢 · " + NAMES[m] + " | " + " | ".join(values) + " |")
    lines += ["", "## 主力持仓量归集", "",
              f"全部观察共有 {v.scope('observations')} 个观察日，有覆盖 {v.scope('covered_observations')} 日，覆盖率 {v.scope('coverage_percent')}%；无覆盖 {v.scope('uncovered_observations')} 日、分成 {v.scope('uncovered_segments')} 段，选中合约切换 {v.scope('switches')} 次。",
              f"最大前根持仓量并列共 {v.scope('maximum_oi_tie_days')} 日；并列按原始合约代码和实例身份升序取首项，首根排除 {v.scope('first_bar_exclusions')} 次，紧邻前根持仓量缺失排除 {v.scope('missing_previous_oi_exclusions')} 次，前后观测最大的日历间隔为 {v.scope('maximum_previous_bar_calendar_gap_days')} 天。",
              "观察日沿用输入日线日期，不是已核实的交易日历；候选只取当天有日线且存在紧邻前根的合约，前根持仓量为零仍有效，缺失不填零、不向更早记录回溯。",
              "五种做法使用同一选择序列；无覆盖日不补造零收益，累计曲线沿用上一值。最大回撤以累计归集净额从零起峰计算，最长持续数是连续严格低于峰值的观察日数，回到等高峰值当天不计入水下段，跨年不重置，末尾未恢复照计。", "",
              "| 做法 | 主力全期净额（元） | 全部合约平均每账户净额（元） | 最长回撤持续观察日 | 末尾仍未恢复 |",
              "| --- | ---: | ---: | ---: | --- |"]
    for m in METHODS:
        fields = [s(m, f) for f in ("net", "all_accounts_mean_display", "longest_drawdown_observations")]
        open_value = s(m, "open_drawdown_at_end")
        lines.append("| " + " | ".join([NAMES[m], *fields, {"true": "是", "false": "否", "空": "无覆盖"}[open_value]]) + " |")
    lines += ["", f"账户均值的分母固定为全部 {v.scope('account_count')} 个有行账户，包括短合约和没有成交的账户；均值显示到小数点后三位，不能与主力归集当作同一笔资金的表现。", "",
              "| 年份 | 唐奇安突破净额 | 均线净额 | Z 分数回归净额 | 买入持有净额 | 始终空仓净额 | 覆盖观察日 |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for y in YEARS:
        label = v.value("dominant-year.csv", {"method": METHODS[0], "year": y}, "year")
        values = [v.value("dominant-year.csv", {"method": m, "year": y}, "net") for m in METHODS]
        count = v.value("dominant-year.csv", {"method": METHODS[0], "year": y}, "covered_observations")
        lines.append("| " + " | ".join([label + ("*" if y in (2020, 2026) else ""), *values, count]) + " |")
    lines += ["", "带星号的年份输入窗口不满自然年；无覆盖年份以空值展示，不计正年数或最差年，有覆盖的零净额保持真零。",
              "历史最早可知事件只能表述为前一根日线完成且其持仓量发布之后，真实时刻未知；导入时间没有改写为历史发布时间，原始零点时间也没有冒充实际开盘或收盘。归集使用各原账户当天净额增量，切换时不以两个账户的权益相减，不重建持仓、成交或换月费用。",
              "[主力全期](v1-daily-baseline-rb/dominant-summary.csv)、[主力分年](v1-daily-baseline-rb/dominant-year.csv) 与 [口径及覆盖](v1-daily-baseline-rb/dominant-scope.csv) 保存精确金额、比例分子分母和限制。", "",
              "## 前两版已接受结果", "", "以下保留原数值表和不同窗口的支持范围；全部合约账户的金额相加不代表连续组合净值。", ""]
    old = prior[REPORT].decode("utf-8").splitlines()[2:]
    replacements = [("C1000000", "1000000 元初始资金"), ("MARGIN_X1_5", "保证金加半"),
                    ("SLIPPAGE_X2", "滑点加倍"), ("FEE_X2", "手续费加倍"),
                    ("PARTIAL_INPUT_WINDOW", "输入窗口不满全年"), ("FROZEN_REAL_EXPORT", "已冻结真实导出"),
                    ("CALENDAR_UNVERIFIED", "交易日历未核实"), ("NOT_ASSESSED", "未作资格评估"),
                    ("INSUFFICIENT", "支持不足"), ("PRIMARY", "满 60 根之后"), ("ALL", "全部观察"),
                    ("SCENARIO", "情景假设"), ("BASE", "基准成本"), ("TREND_UP", "上涨趋势"),
                    ("TREND_DOWN", "下跌趋势"), ("UNKNOWN", "未知"), ("FLAT", "平坦"),
                    ("RANGE", "震荡"), ("MIXED", "混合"), ("本次", "既有矩阵"),
                    ("new_simulation=true、new_training=false、holdout_read=false", "运行过新模拟、未训练、未读取留出集")]
    for original in old:
        for before, after in replacements:
            original = original.replace(before, after)
        if "唐奇安=`" in original:
            original = "[^方法]: " + original
        lines.append(original)
    lines += ["", "本轮只读取已接受账本进行归集，未运行新模拟或训练，未读取留出集；前一版矩阵曾运行新模拟的历史事实保留在复核摘要中。", "",
              "[^证据]: SCENARIO / NOT_ASSESSED；timing_verified=false，historical_known_at=null；全部观察对应 ALL，满 60 根之后对应 PRIMARY，主力归集不另做该筛选。", ""]
    markdown = "\n".join(lines)
    # Old refs remain semantic source references only when their value is still
    # visible; recheck every reference against the preserved source CSV.
    old_refs = []
    for ref in prior_check["report_value_refs"]:
        eq(cell(tables[ref["csv"]][ref["row"] - 2][ref["column"]]), ref["value"], "accepted report reference")
        if ref["value"] and ref["value"] in markdown:
            old_refs.append(ref)
    return markdown.encode("utf-8"), old_refs + v.refs, numeric_mapping(markdown, tables)


def aggregate(context):
    with localcontext() as arithmetic:
        arithmetic.prec = 160
        arithmetic.traps[Inexact] = True
        bars, nets, accounts, all_rows = prepare_data(context)
        selection = select_dominant(bars)
        tables, curves = aggregate_tables(bars, nets, selection, rows(context.prior["sensitivity-summary.csv"]))
        scope_extra = dict(primary_excluded_observations=60, first_primary_observation_number=61,
                          input_all_bar_rows=all_rows, selected_contract_bar_rows=sum(len(x) for x in bars.values()),
                          input_manifest_sha256=context.bindings["input_manifest"]["sha256"],
                          chronological_manifest_sha256=context.bindings["chronological_manifest"]["sha256"],
                          K40A_verdict_sha256=context.bindings["K40A_verdict"]["sha256"],
                          K40B_verdict_sha256=context.bindings["K40B_verdict"]["sha256"],
                          K40B_manifest_sha256=context.bindings["K40B_manifest"]["sha256"],
                          source_bindings_sha256=context.bindings_sha256)
        tables[NEW_CSV[2]].extend(dict(metric=k, value=cell(v)) for k, v in scope_extra.items())
        for name in OLD_CSV:
            tables[name] = rows(context.prior[name])
        report, refs, numeric_refs = render_report(tables, context.prior, context.prior_check)
        payloads = {"publish/" + name: csv_bytes(tables[name]) for name in NEW_CSV}
        payloads.update({"publish/" + name: context.prior[name] for name in OLD_CSV})
        payloads["publish/" + REPORT] = report
        payloads["selection.json"] = json_bytes(selection)
        payloads["daily-curves.json"] = json_bytes([{k: cell(v) if isinstance(v, Decimal) else v for k, v in r.items()} for r in curves])
        reconciliation = dict(strict_five_method_support=True, exact_ledger_increments=True,
                              accepted_all_accounts_net=True, annual_net_conservation=True,
                              one_selection_per_observation=True, common_five_method_selection=True,
                              source_market_dates_checked_before_statistics=True,
                              input_rows_date_checked=all_rows, account_count=len(bars), methods=len(METHODS),
                              original_account_rows=sum(len(v) for v in accounts.values()),
                              summary_rows=len(tables[NEW_CSV[0]]), annual_rows=len(tables[NEW_CSV[1]]),
                              observation_days=len(selection), report_numeric_tokens=len(numeric_refs))
        sources_before = dict(context.sources.records)
        context.sources.verify()
        payloads["source-validation.json"] = json_bytes(dict(status="ALL_PINS_MATCH_BEFORE_AND_AFTER", sources=sources_before,
                                                             safety=SAFETY, holdout_read=False))
        check = dict(schema=SCHEMA, latest_operation="K43", status="PENDING_AGGREGATION_TERMINAL_SEAL",
                     **MODE, k43=dict(**MODE, reconciliation=reconciliation), safety=SAFETY,
                     historical_versions={"K40A": dict(new_simulation=False, new_training=False,
                                                       verdict=context.sources.records["K40A_verdict"]),
                                          "K40B": dict(new_simulation=True, new_training=False,
                                                       CHECK=context.sources.records["K40B_CHECK"],
                                                       report=context.sources.records["B.publish." + REPORT],
                                                       verdict=context.sources.records["K40B_verdict"],
                                                       manifest=context.sources.records["K40B_manifest"])},
                     sources=sources_before, reconciliation=reconciliation,
                     report_value_refs=refs, report_numeric_tokens=numeric_refs,
                     command_templates={"aggregate": ["<PYTHON>=3.13", "-B", "-X", "utf8", "tools/experiments/v1_daily_baseline_dominant.py", "aggregate",
                                                      "--source-repo", "<repo>", "--source-bindings", "<frozen external bindings>",
                                                      "--source-bindings-sha256", "<frozen digest>", "--output-dir", "<new external dir>"],
                                        "seal": ["<PYTHON>=3.13", "-B", "-X", "utf8", "tools/experiments/v1_daily_baseline_dominant.py", "seal",
                                                 "--aggregation-dir", "<same external dir>", "--aggregation-receipt", "<terminated receipt>",
                                                 "--source-bindings", "<same bindings>", "--source-bindings-sha256", "<same digest>"]},
                     command_templates_are_actual_commands=False,
                     receipt_reference="External terminated aggregation and detached seal receipts; raw_exit subject is aggregation_child.",
                     output_hashes={name: pin(raw) for name, raw in sorted(payloads.items()) if name.startswith("publish/")})
        payloads["check-draft.json"] = json_bytes(check)
        # Publish a complete directory only after computation, all checks and
        # serialization succeeded. Failed staging remains identifiable evidence.
        output = context.output
        staging = output.with_name(output.name + ".pending")
        need(not output.exists() and not staging.exists(), "output or pending directory already exists")
        staging.mkdir(parents=True)
        for name, raw in sorted(payloads.items()):
            write_new(child(staging, name), raw)
        write_new(staging / "runtime/source-locations.json", json_bytes(context.sources.locations))
        context.sources.verify()
        manifest = dict(schema="k43-aggregation/v1", status="AGGREGATION_COMPLETE_PENDING_SEAL",
                        payloads={name: pin(raw) for name, raw in sorted(payloads.items())}, reconciliation=reconciliation,
                        source_bindings_sha256=context.bindings_sha256)
        write_new(staging / "manifest.json", json_bytes(manifest))
        need(not output.exists(), "output appeared during publication")
        staging.rename(output)
        return reconciliation


class Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            parser.error("repeated option: " + option_string)
        setattr(namespace, self.dest, values)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    sub = parser.add_subparsers(dest="operation", required=True)
    for operation, names in (("aggregate", ("source-repo", "source-bindings", "source-bindings-sha256", "output-dir")),
                             ("seal", ("aggregation-dir", "aggregation-receipt", "source-bindings", "source-bindings-sha256"))):
        cmd = sub.add_parser(operation, allow_abbrev=False)
        for name in names:
            cmd.add_argument("--" + name, required=True, action=Once)
    return parser.parse_args(argv)


def seal(aggregation_dir, aggregation_receipt, source_bindings, source_bindings_sha256):
    output, receipt_path = path(aggregation_dir), path(aggregation_receipt)
    need(output.is_dir() and receipt_path.is_file(), "missing aggregation or terminal receipt")
    final = output / "publish/CHECK.json"
    need(not final.exists() and not final.with_name("CHECK.json.tmp").exists(), "final CHECK or residual temp exists")
    receipt_raw = receipt_path.read_bytes()
    receipt = loads(receipt_raw)
    eq(receipt["schema"], "k43-aggregation-terminal/v1", "terminal receipt schema")
    need(receipt["raw_exit_subject"] == "aggregation_child" and type(receipt["raw_exit"]) is int and receipt["raw_exit"] == 0
         and receipt["terminated"] is True and receipt["streams_closed"] is True, "aggregation did not terminate successfully")
    need(integer(receipt["pid"]) > 0 and isinstance(receipt["task_id"], str) and receipt["task_id"], "receipt process identity")
    start, end = datetime.fromisoformat(receipt["started_utc"]), datetime.fromisoformat(receipt["ended_utc"])
    need(start.tzinfo is not None and end.tzinfo is not None and end >= start, "receipt process times")
    eq(path(receipt["output_dir"]), output, "receipt output identity")
    eq(receipt["source_bindings_sha256"], source_bindings_sha256, "receipt binding hash")
    argv, cwd = receipt["argv"], path(receipt["cwd"])
    need(isinstance(argv, list) and len(argv) == 14 and argv[1:4] == ["-B", "-X", "utf8"] and argv[5] == "aggregate", "actual aggregation argv pattern")
    eq(path(cwd / argv[4]), path(__file__), "actual aggregation tool")
    args = parse_args(argv[5:])
    eq(path(cwd / args.output_dir), output, "argv aggregation directory")
    eq(path(cwd / args.source_bindings), path(source_bindings), "argv bindings identity")
    eq(args.source_bindings_sha256, source_bindings_sha256, "argv binding hash")
    eq(set(receipt["closed_streams"]), {"stdout", "stderr"}, "two closed streams required")
    for name, record in receipt["closed_streams"].items():
        p = path(record["path"])
        need(p.is_file(), "missing terminated stream")
        eq(pin(p.read_bytes()), {k: record[k] for k in ("bytes", "sha256")}, "closed stream hash: " + name)
    manifest_raw = (output / "manifest.json").read_bytes()
    eq(pin(manifest_raw), receipt["manifest"], "terminal aggregation manifest")
    manifest = loads(manifest_raw)
    eq(manifest["status"], "AGGREGATION_COMPLETE_PENDING_SEAL", "aggregation completion marker")
    eq(manifest["source_bindings_sha256"], source_bindings_sha256, "manifest bindings")
    all_files = {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()}
    eq(all_files, set(manifest["payloads"]) | {"manifest.json", "runtime/source-locations.json"}, "complete pending file set")
    for relative, expected in manifest["payloads"].items():
        eq(pin(child(output, relative).read_bytes()), expected, "sealed payload: " + relative)
    draft = loads((output / "check-draft.json").read_bytes())
    eq(draft["schema"], SCHEMA, "CHECK schema")
    eq(draft["status"], "PENDING_AGGREGATION_TERMINAL_SEAL", "CHECK pending state")
    eq(draft["reconciliation"], manifest["reconciliation"], "aggregation reconciliation identity")
    context = load_sources(path(cwd / args.source_repo), source_bindings, source_bindings_sha256, output, sealing=True)
    eq(context.sources.records, draft["sources"], "source pins at seal")
    eq(context.sources.locations, loads((output / "runtime/source-locations.json").read_bytes()), "physical source locations at seal")
    context.sources.verify()
    eq(receipt_path.read_bytes(), receipt_raw, "terminal receipt changed during seal")
    draft.update(status="SEALED_AGGREGATION_EXIT_0", raw_exit=receipt["raw_exit"], raw_exit_subject="aggregation_child",
                 aggregation_manifest=pin(manifest_raw))
    final_raw = json_bytes(draft)
    atomic_new(final, final_raw)
    return dict(status=draft["status"], CHECK=pin(final_raw))


def main(argv=None):
    args = parse_args(argv)
    try:
        if args.operation == "aggregate":
            result = aggregate(load_sources(args.source_repo, args.source_bindings, args.source_bindings_sha256, args.output_dir))
            result = dict(status="AGGREGATION_COMPLETE_PENDING_SEAL", counts=result, new_simulation=False)
        else:
            result = seal(args.aggregation_dir, args.aggregation_receipt, args.source_bindings, args.source_bindings_sha256)
    except (ValidationError, ValueError, KeyError, TypeError, ArithmeticError, OSError) as exc:
        print(json.dumps(dict(status="VALIDATION_FAILED", type=type(exc).__name__, reason=str(exc)), ensure_ascii=True), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
