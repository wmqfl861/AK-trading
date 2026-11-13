"""Read accepted RB outputs and reproduce a descriptive report without replaying them.

Only the standard library is used. The CLI never runs another command. A caller
captures the terminated CLI's native result and then calls ``seal_check``.
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
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, Inexact, InvalidOperation, localcontext
from pathlib import Path


STRATEGIES = ("donchian_trend", "ma_trend", "zscore_reversion")
NAMES = dict(zip(STRATEGIES, ("唐奇安", "均线", "Z 回归")))
YEARS = tuple(range(2020, 2027))
VIEWS = ("ALL", "PRIMARY")
PAIRS = ((STRATEGIES[0], STRATEGIES[1]), (STRATEGIES[0], STRATEGIES[2]),
         (STRATEGIES[1], STRATEGIES[2]))
REGIMES = ("UNKNOWN", "FLAT", "TREND_UP", "TREND_DOWN", "RANGE", "MIXED")
CAPITALS = ("C5000", "C15000", "C1000000")
MARGINS = {"BASE": Decimal("0.12"), "MARGIN_X1_5": Decimal("0.18")}
GOAL = Path("docs/development-goal-20261026")
CAUSAL_REVIEW = Path("docs/strategy-research-blueprint-20261010-r2/"
                     "implementation-clarifications/rb-daily-causal-regime-20261014/"
                     "hard-review-r1")
REPORT = "v1-daily-baseline-rb.md"
CSV_NAMES = ("year-all.csv", "year-primary.csv", "paired-years.csv",
             "scenario-base.csv", "capital-stress.csv", "causal-regime.csv",
             "conclusions.csv", "scope.csv")
MONEY = ("net", "gross", "fee", "slippage")
ZERO = Decimal(0)
SCHEMA = "aktr-v1-daily-baseline-report/v1"


class ValidationError(ValueError):
    """A failed input, arithmetic, provenance, or publication invariant."""

    def __init__(self, reason, *, source=None, expected=None, actual=None):
        super().__init__(reason)
        self.detail = {"reason": reason, "source": source,
                       "expected": expected, "actual": actual}


def require(condition, reason, **detail):
    if not condition:
        raise ValidationError(reason, **detail)


def equal(actual, expected, source):
    require(actual == expected, "value mismatch", source=source,
            expected=str(expected), actual=str(actual))


def path_value(value):
    require(isinstance(value, (str, Path)), "path must be a string or Path")
    p = Path(value)
    require(not any(part.lower().startswith(".env") or "holdout" in part.lower()
                    for part in p.parts), "forbidden source path", source=str(p))
    return p


def beneath(root, value, *, parent_links=False):
    root, relative = path_value(root).resolve(), path_value(value)
    require(not relative.is_absolute() and not relative.drive,
            "expected a relative source path", source=str(relative))
    require(parent_links or ".." not in relative.parts,
            "path traversal rejected", source=str(relative))
    result = (root / relative).resolve()
    require(result.is_relative_to(root), "source escaped its root", source=str(result))
    return result


def digest(raw):
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _unique_object(items):
    result = {}
    for key, value in items:
        require(key not in result, "duplicate JSON key", source=key)
        result[key] = value
    return result


def json_load(raw):
    try:
        return json.loads(raw, object_pairs_hook=_unique_object,
                          parse_float=Decimal,
                          parse_constant=lambda x: (_ for _ in ()).throw(
                              ValidationError("nonfinite JSON number", actual=x)))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("invalid JSON", actual=str(exc)) from exc


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                       allow_nan=False) + "\n").encode("utf-8")


def csv_load(raw):
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""))
        require(reader.fieldnames is not None and len(set(reader.fieldnames)) ==
                len(reader.fieldnames), "missing or duplicate CSV columns")
        rows = list(reader)
        require(all(None not in r and None not in r.values() for r in rows),
                "ragged CSV row")
        return rows
    except (UnicodeError, csv.Error) as exc:
        raise ValidationError("invalid CSV", actual=str(exc)) from exc


def amount(value):
    require(isinstance(value, (str, int, Decimal)) and not isinstance(value, bool),
            "invalid amount type", actual=repr(value))
    require(str(value) != "" and str(value).strip() == str(value), "empty/whitespace amount")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValidationError("invalid amount", actual=str(value)) from exc
    require(result.is_finite(), "nonfinite amount", actual=str(value))
    return result


def integer(value):
    require(not isinstance(value, bool) and re.fullmatch(r"0|[1-9][0-9]*", str(value))
            is not None, "invalid nonnegative integer", actual=str(value))
    return int(value)


def flag(value):
    require(value in ("true", "false"), "invalid boolean", actual=str(value))
    return value == "true"


def text(value):
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return "0" if value == 0 else format(value, "f").rstrip("0").rstrip(".") \
            if "." in format(value, "f") else format(value, "f")
    return str(value)


def distribution(values, *, empty_sum=None):
    ordered = sorted(values)
    n = len(ordered)
    middle = None if not n else ordered[n // 2] if n % 2 else \
        (ordered[n // 2 - 1] + ordered[n // 2]) / 2
    return {"n": n, "net_sum": sum(ordered, ZERO) if n else empty_sum,
            "median": middle, "min": ordered[0] if n else None,
            "max": ordered[-1] if n else None,
            "profit": sum(v > 0 for v in ordered), "zero": sum(v == 0 for v in ordered),
            "loss": sum(v < 0 for v in ordered)}


def keyed(rows, fields, source):
    result = {}
    for row in rows:
        key = tuple(row[f] for f in fields)
        require(key not in result, "duplicate row key", source=source, actual=str(key))
        result[key] = row
    return result


class Sources:
    """Read only explicitly selected records and pinned payloads; verify twice."""

    def __init__(self):
        self.records = []

    def read(self, logical_id, path, authority, expected=None):
        p = path_value(path).resolve()
        require(p.is_file(), "required source is missing", source=str(p))
        raw = p.read_bytes()
        actual = digest(raw)
        if expected is not None:
            expected = {"sha256": expected} if isinstance(expected, str) else expected
            require(re.fullmatch(r"[0-9a-f]{64}", expected["sha256"]) is not None,
                    "invalid accepted SHA-256", source=logical_id)
            for key in ("bytes", "sha256"):
                if key in expected:
                    equal(actual[key], expected[key], f"{logical_id}.{key}")
        require(not any(r["logical_id"] == logical_id for r in self.records),
                "duplicate logical source", source=logical_id)
        self.records.append({"logical_id": logical_id, "path": str(p),
                             "record_source": authority, "expected": expected,
                             "actual": actual, "verification": "MATCH" if expected else
                             "authority_record_current_bytes", "post_read_unchanged": False})
        return raw

    def verify_again(self):
        for record in self.records:
            path = Path(record["path"])
            require(path.is_file(), "source disappeared", source=record["logical_id"])
            equal(digest(path.read_bytes()), record["actual"], record["logical_id"])
            record["post_read_unchanged"] = True


def _manifest_pin(statement):
    matches = re.findall(r"\bmanifest ([0-9a-f]{64})(?![0-9a-f])", statement)
    require(len(matches) == 1, "expected one accepted manifest pin")
    return matches[0]


def load_sources(repo, sources):
    repo = path_value(repo).resolve()
    require(repo.is_dir(), "source repository is missing", source=str(repo))
    acceptance = json_load(sources.read("chronological.acceptance", repo / GOAL /
                                       "CHRONOLOGICAL-ACCEPTANCE.json", "root acceptance"))
    require(acceptance["independent_verdict"] == "PASS" and
            acceptance["conditional"] is False, "chronological acceptance is not unconditional PASS")
    verdict_pin = acceptance["published_evidence"]["VERDICT.json"]["source"]
    verdict_path = path_value(verdict_pin["path"])
    require(verdict_path.is_absolute() and verdict_path.name == "VERDICT.json" and
            ".." not in verdict_path.parts, "invalid accepted verdict path")
    verdict = json_load(sources.read("chronological.verdict", verdict_path,
                                    "chronological.acceptance.published_evidence", verdict_pin))
    require(verdict["verdict"] == "PASS" and verdict["conditional"] is False,
            "chronological verdict is not unconditional PASS")
    output_ref = verdict["basis"]["independent_fresh_replay"]["output_root"]
    equal(output_ref, acceptance["validation"]["independent_fresh_replay"]["output_root"],
          "chronological.output_root")
    root = beneath(verdict_path.parent, output_ref)
    manifest_pin = _manifest_pin(acceptance["validation"]["reproduction"])
    equal(manifest_pin, _manifest_pin(verdict["basis"]["reproduction"]),
          "chronological.accepted_manifest")
    manifest = json_load(sources.read("chronological.manifest", root / "manifest.json",
                                     "acceptance and verdict reproduction", manifest_pin))
    chrono = {"acceptance": acceptance, "manifest": manifest}
    for filename in ("daily_attribution.csv", "contract_year.csv", "year_summary.csv",
                     "paired_summary.csv", "base_config.json", "chronology-precommit.json",
                     "coverage.json"):
        raw = sources.read("chronological." + filename, root / filename,
                           "chronological.manifest.content_hashes", manifest["content_hashes"][filename])
        chrono[filename] = csv_load(raw) if filename.endswith(".csv") else json_load(raw)

    markdown = sources.read("previous.report", repo / GOAL / "STRATEGY-RESULTS.md",
                            "previous results entry point").decode("utf-8")
    previous = json_load(sources.read("previous.check", repo / GOAL /
                                     "STRATEGY-RESULTS-CHECK.json", "previous results entry point"))
    comparisons = {}
    for kind in ("scenario-compare", "capital-stress"):
        pins = [p for p in previous["source_pins"] if re.fullmatch(
            rf"data/research_runs/rb-daily-{kind}-[^/]+/comparison.csv",
            p["path"].replace("\\", "/"))]
        require(len(pins) == 1, "expected one accepted comparison source", source=kind)
        comparisons[kind] = csv_load(sources.read(kind, beneath(repo, pins[0]["path"]),
                                                "previous.check.source_pins", pins[0]))
    links = re.findall(r"\[因果行情归因\]\(([^)]+)\)", markdown)
    require(len(links) == 1, "expected one causal attribution link")
    # The accepted Markdown link contains ../../; normalize it within the repository.
    link_path = beneath(repo, str(GOAL / links[0]), parent_links=True)
    require(link_path.name == "report.md", "invalid causal report link")
    causal_verdict = json_load(sources.read("causal.verdict", repo / CAUSAL_REVIEW /
                                           "VERDICT.json", "causal hard review authority"))
    require(causal_verdict["verdict"] == "PASS" and causal_verdict["unconditional"] is True,
            "causal verdict is not unconditional PASS")
    causal_root = beneath(repo, causal_verdict["required_command_results"]
                          ["real_51_account_review_run"]["output_root"])
    equal(causal_root, link_path.parent, "causal.link_output_root")
    require(causal_root.parent == repo / "data/research_runs" and
            causal_root.name.startswith("rb-daily-causal-regime-"), "invalid causal output path")
    pin_record = json_load(sources.read("causal.pin_verification", repo / CAUSAL_REVIEW /
                                       "PIN-VERIFICATION.json", "causal hard review authority"))
    pin = pin_record["checks"]["owner_result_manifest/owner"]
    require(pin["ok"] is True and pin["actual"] == pin["expected"], "causal accepted pin mismatch")
    causal_manifest = json_load(sources.read("causal.manifest", causal_root / "manifest.json",
                                            "causal.pin_verification.owner_result_manifest/owner",
                                            pin["expected"]))
    causal = {"verdict": causal_verdict, "manifest": causal_manifest}
    for filename in ("daily_attribution.csv", "regime_summary.csv"):
        causal[filename] = csv_load(sources.read("causal." + filename, causal_root / filename,
                                                "causal.manifest.content_hashes",
                                                causal_manifest["content_hashes"][filename]))
    return chrono, previous, comparisons, causal


def accounts_from_daily(rows, initial_cash, *, causal=False):
    index_name, primary_name = ("observation_index", "primary") if causal else ("j", "primary_support")
    accounts = defaultdict(list)
    identities = {}
    for original in rows:
        row = dict(original)
        symbol, instance, strategy = (row[x] for x in ("symbol", "contract_instance_id", "strategy"))
        require(strategy in STRATEGIES and symbol and instance.startswith(symbol + "|"),
                "invalid daily identity")
        if symbol in identities:
            equal(instance, identities[symbol], "symbol instance identity")
        identities[symbol] = instance
        row["j"] = integer(row[index_name])
        row["primary"] = flag(row[primary_name])
        equal(row["primary"], row["j"] >= 60, "primary mask")
        ts = datetime.fromisoformat(row["raw_ts"])
        require(ts.utcoffset() == timedelta(hours=8), "raw_ts must have +08:00 offset")
        equal(row["raw_date"], ts.date().isoformat(), "raw_date")
        row["raw_year"] = integer(row["raw_year"]) if not causal else ts.year
        equal(row["raw_year"], ts.year, "raw_year")
        require(causal or ts.year in YEARS, "unexpected raw year")
        row["datetime_nano"] = integer(row["datetime_nano"])
        epoch = ts.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
        equal(row["datetime_nano"], (epoch.days * 86400 + epoch.seconds) * 10**9 +
              epoch.microseconds * 1000, "datetime_nano")
        fields = ("equity", "gross_pnl", "fees", "slippage", "net_delta", "gross_delta",
                  "fee_delta", "slippage_delta")
        fields += ("terminal_close_fees", "terminal_close_slippage") if causal else \
            ("previous_equity", "equity_delta", "net_pnl", "cash", "realized_pnl", "unrealized_pnl")
        for field in fields:
            row[field] = amount(row[field])
        require(row["fee_delta"] >= 0 and row["slippage_delta"] >= 0, "negative execution cost")
        row["terminal"] = flag(row["terminal"])
        if causal:
            require(row["regime"] in REGIMES, "unknown accepted regime")
            row["active_observation"] = flag(row["active_observation"])
            for field in ("fill_legs", "terminal_close_legs"):
                row[field] = integer(row[field])
        else:
            for field in ("fills_at_j", "terminal_close_fills"):
                row[field] = integer(row[field])
        key = (symbol, instance, strategy)
        prior = accounts[key][-1] if accounts[key] else None
        equal(row["j"], len(accounts[key]), "ordered unique observation index")
        if prior:
            require(row["datetime_nano"] > prior["datetime_nano"], "unordered timestamps")
        previous_equity = prior["equity"] if prior else initial_cash
        if not causal:
            equal(row["previous_equity"], previous_equity, "previous equity, including year/PRIMARY boundary")
            equal(row["equity_delta"], row["equity"] - previous_equity, "equity_delta")
            equal(row["net_pnl"], row["equity"] - initial_cash, "cumulative net_pnl")
        row["previous_equity"] = previous_equity
        equal(row["net_delta"], row["equity"] - previous_equity, "net equity increment")
        equal(row["net_delta"], row["gross_delta"] - row["fee_delta"] - row["slippage_delta"],
              "net = gross - fee - slippage")
        for cumulative, delta in (("gross_pnl", "gross_delta"), ("fees", "fee_delta"),
                                  ("slippage", "slippage_delta")):
            equal(row[cumulative] - (prior[cumulative] if prior else ZERO), row[delta], cumulative)
        accounts[key].append(row)
    require(bool(accounts), "no daily observations")
    for symbol, instance in sorted(identities.items()):
        support = None
        for strategy in STRATEGIES:
            key = (symbol, instance, strategy)
            require(key in accounts, "missing strategy support", actual=str(key))
            account = accounts[key]
            equal([r["terminal"] for r in account], [False] * (len(account) - 1) + [True],
                  "terminal row identity")
            fields = ("j", "raw_ts", "datetime_nano", "raw_date", "raw_year", "primary")
            if causal:
                fields += ("regime",)
            current = [tuple(r[f] for f in fields) for r in account]
            if support is not None:
                equal(current, support, "three-strategy observation support")
            support = current
    return dict(accounts)


def cell_totals(rows):
    return {name: sum((r[name + "_delta"] for r in rows), ZERO) if rows else None for name in MONEY}


def compare_distribution(source, result, *, prefix):
    for field, column in (("min", prefix + "min"), ("median", prefix + "median"),
                          ("max", prefix + "max")):
        equal(None if source[column] == "" else amount(source[column]), result[field], column)
    for field, column in (("profit", "positive_contract_years"), ("zero", "zero_contract_years"),
                          ("loss", "negative_contract_years")):
        equal(integer(source[column]), result[field], column)


def reproduce_chronology(data):
    base = data["base_config.json"]
    cash = amount(base["initial_cash"])
    precommit, coverage = data["chronology-precommit.json"], data["coverage.json"]
    equal(precommit["chronology"]["raw_years"], list(YEARS), "accepted raw years")
    equal(precommit["chronology"]["primary_mask"], "zero_based_observation_index >= 60", "primary rule")
    universe = {(r["symbol"], r["contract_instance_id"]) for r in precommit["universe"]}
    equal(len(universe), len(precommit["universe"]), "unique universe")
    accounts = accounts_from_daily(data["daily_attribution.csv"], cash)
    equal({k[:2] for k in accounts}, universe, "accepted universe support")
    contract_source = keyed(data["contract_year.csv"],
                            ("symbol", "contract_instance_id", "strategy", "raw_year", "view"),
                            "contract_year")
    cells, expected_keys = {}, set()
    for key, account in sorted(accounts.items()):
        for year in YEARS:
            for view in VIEWS:
                group = [r for r in account if r["raw_year"] == year and
                         (view == "ALL" or r["primary"])]
                source_key = (*key, str(year), view)
                expected_keys.add(source_key)
                require(source_key in contract_source, "missing contract-year cell", actual=str(source_key))
                src, sums = contract_source[source_key], cell_totals(group)
                equal(integer(src["observations"]), len(group), "contract-year observations")
                for name in MONEY:
                    column = "year_" + name + "_delta"
                    equal(None if src[column] == "" else amount(src[column]), sums[name], column)
                for column, expected in (("first_j", group[0]["j"] if group else None),
                                          ("last_j", group[-1]["j"] if group else None),
                                          ("previous_equity", group[0]["previous_equity"] if group else None),
                                          ("end_equity", group[-1]["equity"] if group else None)):
                    equal(None if src[column] == "" else amount(src[column]), expected, column)
                equal(src["first_raw_ts"], group[0]["raw_ts"] if group else "", "first_raw_ts")
                equal(src["last_raw_ts"], group[-1]["raw_ts"] if group else "", "last_raw_ts")
                for column, expected in (("raw_dates", len({r["raw_date"] for r in group})),
                                          ("fills", sum(r["fills_at_j"] for r in group)),
                                          ("terminal_close_fills", sum(r["terminal_close_fills"] for r in group))):
                    equal(integer(src[column]), expected, "contract_year." + column)
                cells[(*key, year, view)] = (group, sums)
    equal(set(contract_source), expected_keys, "complete contract-year matrix")

    year_source = keyed(data["year_summary.csv"], ("raw_year", "strategy", "view"), "year_summary")
    years, year_index = [], {}
    for year in YEARS:
        for strategy in STRATEGIES:
            for view in VIEWS:
                groups = [cells[(symbol, instance, strategy, year, view)] for symbol, instance in sorted(universe)]
                observed = [sums for rows, sums in groups if rows]
                rows = [r for group, _ in groups for r in group]
                result = {"raw_year": year, "strategy": strategy, "view": view,
                          **distribution([s["net"] for s in observed]), "observations": len(rows),
                          "raw_dates": len({r["raw_date"] for r in rows}),
                          "fills": sum(r["fills_at_j"] for r in rows),
                          "terminal_close_fills": sum(r["terminal_close_fills"] for r in rows),
                          "status": ("PARTIAL_INPUT_WINDOW" if year in (2020, 2026) else "OBSERVED")
                          if rows else "PRIMARY_EMPTY" if view == "PRIMARY" else "NO_ROWS",
                          "year_status": precommit["chronology"]["year_status"][str(year)]}
                for name in ("gross", "fee", "slippage"):
                    result[name + "_sum"] = sum((s[name] for s in observed), ZERO) if rows else None
                src = year_source[(str(year), strategy, view)]
                for column, expected in (("contract_year_cells", result["n"]), ("distinct_contracts", result["n"]),
                                          ("observations", len(rows)), ("raw_dates", result["raw_dates"]),
                                          ("fills", result["fills"]), ("terminal_close_fills", result["terminal_close_fills"])):
                    equal(integer(src[column]), expected, "year_summary." + column)
                for name in MONEY:
                    column = "independent_account_descriptive_" + name + "_sum"
                    equal(None if src[column] == "" else amount(src[column]), result[name + "_sum"], column)
                compare_distribution(src, result, prefix="contract_year_net_")
                equal(src["status"], result["status"], "year_summary.status")
                equal(src["year_status"], result["year_status"], "year_summary.year_status")
                years.append(result)
                year_index[(year, strategy, view)] = result
    equal(len(year_source), len(years), "year summary matrix")

    paired_source = keyed(data["paired_summary.csv"], ("raw_year", "view", "strategy_a", "strategy_b"),
                          "paired_summary")
    paired = []
    for year in YEARS:
        for view in VIEWS:
            for left, right in PAIRS:
                differences, pair_rows = [], []
                sums = dict.fromkeys(MONEY, ZERO)
                for symbol, instance in sorted(universe):
                    a, sa = cells[(symbol, instance, left, year, view)]
                    b, sb = cells[(symbol, instance, right, year, view)]
                    equal(len(a), len(b), "pair support")
                    if a:
                        differences.append(sa["net"] - sb["net"])
                        pair_rows.extend(a)
                        for name in MONEY:
                            sums[name] += sa[name] - sb[name]
                result = {"raw_year": year, "view": view, "strategy_a": left, "strategy_b": right,
                          **distribution(differences), "matched_observations": len(pair_rows),
                          "unmatched_observations": 0, "raw_dates": len({r["raw_date"] for r in pair_rows}),
                          "status": "MATCHED" if pair_rows else "PRIMARY_EMPTY" if view == "PRIMARY" else "NO_ROWS"}
                for name in ("gross", "fee", "slippage"):
                    result[name + "_sum"] = sums[name] if differences else None
                if differences:
                    equal(result["net_sum"], year_index[(year, left, view)]["net_sum"] -
                          year_index[(year, right, view)]["net_sum"], "paired total cross-check")
                src = paired_source[(str(year), view, left, right)]
                for column, expected in (("matched_contract_years", result["n"]), ("distinct_contracts", result["n"]),
                                          ("matched_observations", len(pair_rows)), ("unmatched_observations", 0),
                                          ("raw_dates", result["raw_dates"])):
                    equal(integer(src[column]), expected, "paired_summary." + column)
                for name in MONEY:
                    column = "independent_account_descriptive_" + name + "_difference_sum"
                    equal(None if src[column] == "" else amount(src[column]), result[name + "_sum"], column)
                compare_distribution(src, result, prefix="contract_year_difference_")
                equal(src["status"], result["status"], "paired_summary.status")
                paired.append(result)
    equal(len(paired_source), len(paired), "paired summary matrix")
    one_strategy = [r for k, rows in accounts.items() if k[2] == STRATEGIES[0] for r in rows]
    counts = {"rb_declarations": len(universe), "rb_bars": len(one_strategy),
              "rb_distinct_raw_dates": len({r["raw_date"] for r in one_strategy}),
              "child_calls": len(accounts), "daily_rows_ALL": sum(len(r) for r in accounts.values()),
              "primary_rows_j_ge_60": sum(r["primary"] for rows in accounts.values() for r in rows),
              "contract_year_cells": len(cells), "year_summary_rows": len(years),
              "paired_summary_rows": len(paired),
              "paired_observation_rows": sum(r["matched_observations"] for r in paired)}
    accepted = data["acceptance"]["validation"]["counts"]
    for key, value in counts.items():
        equal(value, integer(accepted[key]), "acceptance.counts." + key)
    short = {k[0]: len(rows) for k, rows in accounts.items() if k[2] == STRATEGIES[0] and len(rows) < 60}
    equal(short, accepted["short_contracts"], "accepted short contracts")
    equal(accepted["pair_classifications"], {"MATCHED": counts["paired_observation_rows"],
          "MISSING_A": 0, "MISSING_B": 0, "IDENTITY_MISMATCH": 0,
          "NONFINITE_OR_INVALID_AMOUNT": 0, "SUPPORT_MISMATCH": 0}, "accepted pair classifications")
    for record in (data["manifest"]["actual_counts"], data["manifest"]["expected_counts"],
                   precommit["expected_counts"], coverage["expected_counts"]):
        for key, accepted_key in (("daily_rows", "daily_rows_ALL"), ("primary_rows", "primary_rows_j_ge_60"),
                                  ("child_calls", "child_calls"), ("contract_year_cells", "contract_year_cells"),
                                  ("year_summary_rows", "year_summary_rows"), ("paired_summary_rows", "paired_summary_rows"),
                                  ("paired_observation_rows", "paired_observation_rows")):
            equal(integer(record[key]), counts[accepted_key], "output counts." + key)
    for key in ("rb_declarations", "rb_bars", "rb_distinct_raw_dates"):
        equal(integer(coverage[key]), counts[key], "coverage." + key)
    equal(coverage["rb_raw_ts_min"], min(r["raw_ts"] for r in one_strategy), "coverage start")
    equal(coverage["rb_raw_ts_max"], max(r["raw_ts"] for r in one_strategy), "coverage end")
    return years, paired, counts, accounts


def comparison_rows(rows, key_fields):
    keyed(rows, key_fields, "comparison")
    converted = []
    for source in rows:
        row = dict(source)
        require(row["strategy"] in STRATEGIES and row["evidence_level"] == "SCENARIO",
                "invalid comparison identity/evidence")
        for field in ("initial_cash", "ending_equity", "gross_pnl", "fees", "slippage", "net_pnl"):
            row[field] = amount(row[field])
        for field in ("observations", "closed_trades", "opening_blocks", "fills"):
            row[field] = integer(row[field])
        equal(row["net_pnl"], row["gross_pnl"] - row["fees"] - row["slippage"], "comparison costs")
        equal(row["net_pnl"], row["ending_equity"] - row["initial_cash"], "comparison equity")
        equal(row["coverage_status"] == "MEETS_60_OBSERVATIONS", row["observations"] >= 60,
              "comparison common coverage")
        for field in ("first_raw_ts", "last_raw_ts"):
            require(datetime.fromisoformat(row[field]).utcoffset() == timedelta(hours=8),
                    "comparison timestamp must have +08:00")
        require(row["first_raw_ts"] <= row["last_raw_ts"], "inverted comparison interval")
        converted.append(row)
    return converted


def reproduce_previous(previous, comparisons, base_config):
    scenario = comparison_rows(comparisons["scenario-compare"],
                               ("symbol", "contract_instance_id", "strategy", "scenario"))
    capital = comparison_rows(comparisons["capital-stress"],
                              ("symbol", "contract_instance_id", "strategy", "capital_id", "scenario"))
    selected = [r for r in scenario if r["scenario"] == "BASE" and
                r["coverage_status"] == "MEETS_60_OBSERVATIONS"]
    common = None
    base_rows = []
    old_groups = keyed(previous["groups"], ("strategy",), "previous BASE groups")
    for strategy in STRATEGIES:
        group = [r for r in selected if r["strategy"] == strategy]
        identities = {(r["symbol"], r["contract_instance_id"]) for r in group}
        require(bool(identities), "empty BASE common sample")
        if common is not None:
            equal(identities, common, "BASE three-strategy common identities")
        common = identities
        require(all(r["initial_cash"] == amount(base_config["initial_cash"]) for r in group),
                "BASE initial cash differs from accepted base config")
        result = {"strategy": strategy, **distribution([r["net_pnl"] for r in group]),
                  "closed_trades": sum(r["closed_trades"] for r in group),
                  "gross_sum": sum((r["gross_pnl"] for r in group), ZERO),
                  "fee_sum": sum((r["fees"] for r in group), ZERO),
                  "slippage_sum": sum((r["slippage"] for r in group), ZERO)}
        old = old_groups[(strategy,)]
        for field, old_field in (("n", "cells"), ("profit", "positive"), ("zero", "zero"),
                                  ("loss", "negative"), ("closed_trades", "closed_trades")):
            equal(result[field], integer(old[old_field]), "previous.BASE." + old_field)
        equal(result["net_sum"], amount(old["net_sum"]), "previous.BASE.net_sum")
        equal(result["median"], amount(old["net_median"]), "previous.BASE.median")
        base_rows.append(result)
    equal(len(old_groups), len(STRATEGIES), "previous BASE groups count")
    selected_capital = [r for r in capital if (r["symbol"], r["contract_instance_id"]) in common]
    for row in capital:
        require(row["capital_id"] in CAPITALS and row["scenario"] in MARGINS,
                "unexpected capital/margin scenario")
        equal(row["initial_cash"], amount(row["capital_id"][1:]), "capital initial cash")
        row["margin_rate"] = amount(row["margin_rate"])
        equal(row["margin_rate"], MARGINS[row["scenario"]], "capital margin rate")
        equal(amount(row["max_margin_ratio"]), amount(base_config["max_margin_ratio"]), "margin budget")
        for field in ("opening_fills", "opening_attempts", "opening_margin_blocked", "opening_costs_exhaust_equity"):
            row[field] = integer(row[field])
        equal(row["opening_attempts"], row["opening_fills"] + row["opening_blocks"], "opening attempts")
        equal(row["opening_blocks"], row["opening_margin_blocked"] + row["opening_costs_exhaust_equity"],
              "opening block reasons")
    capital_rows = []
    for capital_id in CAPITALS:
        for margin in MARGINS:
            for strategy in STRATEGIES:
                group = [r for r in selected_capital if (r["capital_id"], r["scenario"], r["strategy"]) ==
                         (capital_id, margin, strategy)]
                equal({(r["symbol"], r["contract_instance_id"]) for r in group}, common,
                      "capital common identities")
                require(all(r["coverage_status"] == "MEETS_60_OBSERVATIONS" for r in group),
                        "capital coverage mismatch")
                result = {"capital_id": capital_id, "scenario": margin, "strategy": strategy,
                          "initial_cash": amount(capital_id[1:]), "margin_rate": MARGINS[margin],
                          **distribution([r["net_pnl"] for r in group]),
                          "opening_blocks": sum(r["opening_blocks"] for r in group),
                          "opening_fills": sum(r["opening_fills"] for r in group),
                          "opening_attempts": sum(r["opening_attempts"] for r in group),
                          "closed_trades": sum(r["closed_trades"] for r in group),
                          "gross_sum": sum((r["gross_pnl"] for r in group), ZERO),
                          "fee_sum": sum((r["fees"] for r in group), ZERO),
                          "slippage_sum": sum((r["slippage"] for r in group), ZERO)}
                result["execution_note"] = "NO_OPENING_FILLS" if result["opening_fills"] == 0 else "OBSERVED_OPENING_FILLS"
                if capital_id == "C1000000" and margin == "BASE":
                    old = next(r for r in base_rows if r["strategy"] == strategy)
                    for field in ("n", "net_sum", "median", "profit", "zero", "loss", "closed_trades",
                                  "gross_sum", "fee_sum", "slippage_sum"):
                        equal(result[field], old[field], "capital BASE cross-reference." + field)
                    base_lookup = {(r["symbol"], r["contract_instance_id"]): r for r in selected
                                   if r["strategy"] == strategy}
                    for row in group:
                        reference = base_lookup[(row["symbol"], row["contract_instance_id"])]
                        for field in ("net_pnl", "gross_pnl", "fees", "slippage", "closed_trades",
                                      "observations", "first_raw_ts", "last_raw_ts"):
                            equal(row[field], reference[field], "capital/base cell." + field)
                capital_rows.append(result)
    old = previous["capital_stress"]
    highlighted = [r for r in capital_rows if r["strategy"] == previous["filter"]["capital_stress"]["strategy"]
                   and r["initial_cash"] == amount(old["initial_cash"]) and r["margin_rate"] == amount(old["margin_rate"])]
    equal(len(highlighted), 1, "previous pressure selected group")
    for field, old_field in (("n", "cells"), ("opening_blocks", "opening_blocks")):
        equal(highlighted[0][field], integer(old[old_field]), "previous.capital." + old_field)
    equal(highlighted[0]["net_sum"], amount(old["net_sum"]), "previous.capital.net_sum")
    equal(sum(r["opening_fills"] for r in capital if r["capital_id"] == "C5000"),
          integer(previous["C5000_opening_fills"]), "previous C5000 no opening fact")
    return base_rows, capital_rows, selected, selected_capital


def reproduce_causal(data, initial_cash):
    accounts = accounts_from_daily(data["daily_attribution.csv"], initial_cash, causal=True)
    source = keyed(data["regime_summary.csv"],
                   ("symbol", "contract_instance_id", "strategy", "window", "regime"), "regime_summary")
    cells, expected_keys = {}, set()
    for key, account in sorted(accounts.items()):
        for view in VIEWS:
            window = [r for r in account if view == "ALL" or r["primary"]]
            total = ZERO
            for regime in REGIMES:
                group = [r for r in window if r["regime"] == regime]
                source_key = (*key, view, regime)
                expected_keys.add(source_key)
                require(source_key in source, "missing causal summary cell", actual=str(source_key))
                src = source[source_key]
                sums = {name: sum((r[name + "_delta"] for r in group), ZERO) for name in MONEY}
                for name, column in (("net", "net_pnl"), ("gross", "gross_pnl"), ("fee", "fees"),
                                      ("slippage", "slippage")):
                    equal(amount(src[column]), sums[name], "causal summary." + column)
                counters = {"observation_count": len(group), "raw_date_count": len({r["raw_date"] for r in group}),
                            "active_observations": sum(r["active_observation"] for r in group),
                            "fill_legs": sum(r["fill_legs"] for r in group),
                            "terminal_observations": sum(r["terminal"] for r in group),
                            "terminal_close_legs": sum(r["terminal_close_legs"] for r in group)}
                for field, value in counters.items():
                    equal(integer(src[field]), value, "causal summary." + field)
                for field in ("terminal_close_fees", "terminal_close_slippage"):
                    equal(amount(src[field]), sum((r[field] for r in group), ZERO), "causal summary." + field)
                equal(src["statistical_qualification"], "NOT_ASSESSED", "causal qualification")
                cells[source_key] = (group, sums)
                total += sums["net"]
            equal(total, sum((r["net_delta"] for r in window), ZERO), "six-regime partition per account")
    equal(set(source), expected_keys, "complete causal matrix")
    results = []
    for view in VIEWS:
        for regime in REGIMES:
            for strategy in STRATEGIES:
                groups = [value for key, value in cells.items() if key[2:] == (strategy, view, regime)]
                observed = [s for rows, s in groups if rows]
                result = {"window": view, "regime": regime, "strategy": strategy,
                          **distribution([s["net"] for s in observed], empty_sum=ZERO),
                          "observations": sum(len(rows) for rows, _ in groups),
                          "gross_sum": sum((s["gross"] for s in observed), ZERO),
                          "fee_sum": sum((s["fee"] for s in observed), ZERO),
                          "slippage_sum": sum((s["slippage"] for s in observed), ZERO),
                          "status": "OBSERVED_DESCRIPTIVE_ONLY" if observed else "INSUFFICIENT",
                          "statistical_qualification": "NOT_ASSESSED"}
                results.append(result)
    for view in VIEWS:
        for strategy in STRATEGIES:
            equal(sum((r["net_sum"] for r in results if (r["window"], r["strategy"]) == (view, strategy)), ZERO),
                  sum((r["net_delta"] for key, account in accounts.items() if key[2] == strategy
                       for r in account if view == "ALL" or r["primary"]), ZERO), "six-regime aggregate conservation")
    accepted = data["verdict"]["required_command_results"]["real_51_account_review_run"]["compact_return"]
    for name, actual in (("accounts", len(accounts)), ("attribution_rows", sum(len(a) for a in accounts.values())),
                         ("primary_attribution_rows", sum(r["primary"] for a in accounts.values() for r in a)),
                         ("regime_summary_rows", len(cells))):
        equal(actual, integer(accepted[name]), "causal accepted count." + name)
    return results, accounts


def make_conclusions(years):
    result = []
    for strategy in STRATEGIES:
        for view in VIEWS:
            rows = [r for r in years if r["strategy"] == strategy and r["view"] == view and r["n"] > 0]
            minimum = min((r["net_sum"] for r in rows), default=None)
            tied = [r["raw_year"] for r in rows if r["net_sum"] == minimum]
            result.append({"strategy": strategy, "view": view, "calendar_year_slots": len(YEARS),
                           "observed_years": len(rows), "empty_years": len(YEARS) - len(rows),
                           "positive_years": sum(r["net_sum"] > 0 for r in rows),
                           "zero_years": sum(r["net_sum"] == 0 for r in rows),
                           "negative_years": sum(r["net_sum"] < 0 for r in rows),
                           "worst_year": tied[0] if tied else None, "worst_net": minimum,
                           "all_tied_worst_years": ";".join(map(str, tied)),
                           "worst_description": "最差年" if minimum is not None and minimum < 0 else "最低净额年",
                           "status": "OBSERVED" if rows else "INSUFFICIENT"})
    return result


def make_scope(chrono, accounts, base_rows, capital_rows, causal_accounts):
    result = []

    def put(source, metric, value, unit="", description=""):
        result.append({"source": source, "metric": metric, "value": value,
                       "unit": unit, "description": description})

    def dates(source, rows, first="raw_date", last="raw_date"):
        put(source, "first_date", min(r[first][:10] for r in rows))
        put(source, "last_date", max(r[last][:10] for r in rows))

    for view in VIEWS:
        rows = [r for k, a in accounts.items() if k[2] == STRATEGIES[0] for r in a if view == "ALL" or r["primary"]]
        if rows:
            dates("chronological_" + view, rows)
        else:
            put("chronological_" + view, "first_date", None)
            put("chronological_" + view, "last_date", None)
        put("chronological_" + view, "observed_contracts", len({r["contract_instance_id"] for r in rows}), "contracts")
        put("chronological_" + view, "observed_years", len({r["raw_year"] for r in rows}), "years")
        put("chronological_" + view, "observations_per_strategy", len(rows), "rows")
        causal_rows = [r for k, a in causal_accounts.items() if k[2] == STRATEGIES[0]
                       for r in a if view == "ALL" or r["primary"]]
        if causal_rows:
            dates("causal_" + view, causal_rows)
        else:
            put("causal_" + view, "first_date", None)
            put("causal_" + view, "last_date", None)
        put("causal_" + view, "observed_contracts", len({r["contract_instance_id"] for r in causal_rows}), "contracts")
        put("causal_" + view, "observations_per_strategy", len(causal_rows), "rows")
    for source, rows in (("previous_BASE", base_rows), ("previous_capital", capital_rows)):
        dates(source, rows, "first_raw_ts", "last_raw_ts")
        put(source, "common_contracts", len({r["contract_instance_id"] for r in rows}), "contracts")
    put("chronological", "declared_contracts", len(chrono["chronology-precommit.json"]["universe"]), "contracts")
    put("chronological", "calendar_year_slots", len(YEARS), "years")
    put("chronological", "first_year", YEARS[0], "year")
    put("chronological", "last_year", YEARS[-1], "year")
    put("method", "primary_zero_based_index", 60, "rows", "full path retained; no rerun/reset")
    put("method", "primary_first_ordinal", 61, "rows")
    put("method", "common_coverage_threshold", 60, "rows", "BASE/capital >= threshold; PRIMARY is j>=threshold")
    for name, value in chrono["base_config.json"].items():
        put("assumptions", name, integer(value) if name == "lots" else amount(value))
    for name, value in (("evidence_level", "SCENARIO"), ("statistical_qualification", "NOT_ASSESSED"),
                        ("calendar_status", "CALENDAR_UNVERIFIED"), ("data_kind", chrono["coverage.json"]["data_kind"])):
        put("limits", name, value)
    return result


def csv_bytes(rows):
    require(bool(rows), "aggregate CSV must retain empty groups")
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: text(value) for key, value in row.items()})
    return stream.getvalue().encode("utf-8")


class ReportValues:
    def __init__(self, tables):
        self.tables = tables
        self.refs = []

    def value(self, filename, selector, column):
        matches = [(i, r) for i, r in enumerate(self.tables[filename])
                   if all(r[k] == v for k, v in selector.items())]
        require(len(matches) == 1, "ambiguous report reference", source=f"{filename}:{selector}")
        index, row = matches[0]
        value = text(row[column])
        self.refs.append({"csv": filename, "row": index + 2, "column": column, "value": value})
        return value if value else "空"

    def scope(self, source, metric):
        return self.value("scope.csv", {"source": source, "metric": metric}, "value")


def render_report(tables):
    refs = ReportValues(tables)
    v, s = refs.value, refs.scope
    conclusions = []
    for view in VIEWS:
        values = [f"{NAMES[strategy]} {v('conclusions.csv', {'strategy': strategy, 'view': view}, 'positive_years')}/"
                  f"{v('conclusions.csv', {'strategy': strategy, 'view': view}, 'calendar_year_slots')}"
                  for strategy in STRATEGIES]
        conclusions.append(f"{view} 正净额年数为" + "、".join(values))
    first = "在有观测的原始年份中，" + "；".join(conclusions) + "。"
    worst = []
    for strategy in STRATEGIES:
        selector = {"strategy": strategy, "view": "ALL"}
        worst.append(f"{NAMES[strategy]}{v('conclusions.csv', selector, 'worst_description')} "
                     f"{v('conclusions.csv', selector, 'worst_year')} 年 "
                     f"{v('conclusions.csv', selector, 'worst_net')} 元")
    second = "ALL 的" + "；".join(worst) + "（并列时列最早年）。"
    third = (f"这些是 {s('limits', 'evidence_level')} 描述结果，不是样本外检验、历史实盘或组合收益，"
             "不能由此给出交易建议。")
    data_kind = s("limits", "data_kind")
    lines = ["# RB 日线既有结果", "", first, second, third, "",
             "唐奇安=`donchian_trend`，均线=`ma_trend`，Z 回归=`zscore_reversion`；金额单位为元。",
             f"主体实际观察区间 {s('chronological_ALL', 'first_date')} 至 {s('chronological_ALL', 'last_date')}，"
             f"共 {s('chronological', 'declared_contracts')} 个声明且有行合约；"
             f"{s('chronological_ALL', 'observed_years')} 个有观测年份不代表完整自然年，来源类型 {data_kind}。",
             f"ALL 保留完整轨迹；PRIMARY 为零基 j≥{s('method', 'primary_zero_based_index')}，"
             f"即第 {s('method', 'primary_first_ordinal')} 根起，继承先前持仓和权益。",
             "年度每格为：有行合约数 n；净额合计；单合约年度净额中位数；盈/平/亏合约数。空单元不计作持平。", ""]
    for view, filename in (("ALL", "year-all.csv"), ("PRIMARY", "year-primary.csv")):
        lines += [f"**年度 {view}**（标 * 的年份为 PARTIAL_INPUT_WINDOW）", "",
                  "| 原始年份 | 唐奇安 | 均线 | Z 回归 |", "| --- | --- | --- | --- |"]
        for year in YEARS:
            label = v(filename, {"raw_year": year, "strategy": STRATEGIES[0]}, "raw_year")
            if year in (YEARS[0], YEARS[-1]):
                label += "*"
            cells = []
            for strategy in STRATEGIES:
                selector = {"raw_year": year, "strategy": strategy}
                cells.append("；".join(v(filename, selector, field) for field in ("n", "net_sum", "median")) + "；" +
                             "/".join(v(filename, selector, field) for field in ("profit", "zero", "loss")))
            lines.append("| " + " | ".join([label, *cells]) + " |")
        lines.append("")
    lines += ["**两两有向净额差：左减右**；每格依次为 ALL / PRIMARY 合计差，逐合约差值中位数与计数保留在 CSV。", "",
              "| 原始年份 | 唐奇安−均线 | 唐奇安−Z 回归 | 均线−Z 回归 |", "| --- | --- | --- | --- |"]
    for year in YEARS:
        label = v("paired-years.csv", {"raw_year": year, "view": "ALL", "strategy_a": PAIRS[0][0],
                                      "strategy_b": PAIRS[0][1]}, "raw_year")
        cells = [" / ".join(v("paired-years.csv", {"raw_year": year, "view": view, "strategy_a": a,
                                                  "strategy_b": b}, "net_sum") for view in VIEWS) for a, b in PAIRS]
        lines.append("| " + " | ".join([label, *cells]) + " |")
    lines += ["", "**较早窗口的既有因果行情归因**", "",
              f"归因 ALL 的 {s('causal_ALL', 'observed_contracts')} 个有行合约覆盖 "
              f"{s('causal_ALL', 'first_date')} 至 {s('causal_ALL', 'last_date')}；"
              f"下表 PRIMARY 实际有行合约为 {s('causal_PRIMARY', 'observed_contracts')} 个，"
              f"日期 {s('causal_PRIMARY', 'first_date')} 至 {s('causal_PRIMARY', 'last_date')}，各状态 n 见 CSV。",
              "沿用前一日之前的固定价格前缀标签，只归集已有净额；空状态是加法分解的空和，"
              "标为 INSUFFICIENT，中位数为空。各状态之和等于该口径净额；"
              "不是仅交易该状态的回测，不相加回撤，不能解释七年年度变化；资格仍为 "
              f"{s('limits', 'statistical_qualification')}。", "",
              "| 既有状态（PRIMARY） | 唐奇安净额 | 均线净额 | Z 回归净额 |", "| --- | --- | --- | --- |"]
    for regime in REGIMES:
        cells = [v("causal-regime.csv", {"window": "PRIMARY", "regime": regime, "strategy": st}, "net_sum")
                 for st in STRATEGIES]
        lines.append("| " + " | ".join([regime, *cells]) + " |")
    lines += ["", "**较早窗口的既有 BASE 与资金压力**", "",
              f"BASE 和资金压力分别覆盖 {s('previous_BASE', 'first_date')} 至 {s('previous_BASE', 'last_date')}、"
              f"{s('previous_capital', 'first_date')} 至 {s('previous_capital', 'last_date')}，"
              f"各用共同 {s('previous_BASE', 'common_contracts')} / {s('previous_capital', 'common_contracts')} 个合约，"
              f"筛选门槛为至少 {s('method', 'common_coverage_threshold')} 根，与归因 ALL 的支持集合不同。",
              "每格为净额合计；开仓阻断次数。无开仓组的零损益不表示安全有效。"
              "本表仅整理旧资金档，并未补做待定资金档实验。", "",
              "| 初始资金；保证金率；情景 | 唐奇安 | 均线 | Z 回归 |", "| --- | --- | --- | --- |"]
    for capital in CAPITALS:
        for margin in MARGINS:
            selector = {"capital_id": capital, "scenario": margin, "strategy": STRATEGIES[0]}
            label = "；".join((v("capital-stress.csv", selector, "initial_cash"),
                              v("capital-stress.csv", selector, "margin_rate"), margin))
            cells = ["；".join(v("capital-stress.csv", {"capital_id": capital, "scenario": margin, "strategy": st}, f)
                               for f in ("net_sum", "opening_blocks")) for st in STRATEGIES]
            lines.append("| " + " | ".join([label, *cells]) + " |")
    base_capital_label = v("capital-stress.csv", {"capital_id": "C1000000", "scenario": "BASE", "strategy": STRATEGIES[0]}, "capital_id")
    input_description = "SYNTHETIC 合成夹具" if data_kind == "SYNTHETIC" else "真实冻结价格导出"
    lines += ["", f"资金表 {base_capital_label}/BASE 与 [scenario-base.csv](v1-daily-baseline-rb/scenario-base.csv) "
              "逐合约及汇总交叉一致；BASE 单合约中位数依次为 " + "、".join(
                  f"{NAMES[st]} {v('scenario-base.csv', {'strategy': st}, 'median')}" for st in STRATEGIES) + " 元。", "",
              f"限制：{input_description}上的情景假设；固定 {s('assumptions', 'lots')} 手、"
              f"初始资金 {s('assumptions', 'initial_cash')} 元（旧资金压力另列）。独立单合约账户的窗口重叠，"
              "金额合计不是组合净值；不换月，手续费、滑点和保证金是假设，交易日历未核实 "
              f"({s('limits', 'calendar_status')})。年度分组不是严格样本外，结果已见过，"
              "不形成新资格或统计显著性证据。", "",
              "来源：已接受的七年主体、较早情景比较/资金压力及因果归因输出；只读精确复算，"
              "未运行新模拟或训练。全部聚合与逐个正文数值映射见 "
              "[汇总和 CHECK](v1-daily-baseline-rb/CHECK.json)。", ""]
    return "\n".join(lines).encode("utf-8"), refs.refs


def validate_safety(chrono, causal):
    data_kind = chrono["coverage.json"]["data_kind"]
    require(data_kind in ("FROZEN_REAL_EXPORT", "SYNTHETIC"), "unrecognized input evidence type")
    for name, record in (("chronological manifest", chrono["manifest"]),
                          ("chronology precommit", chrono["chronology-precommit.json"]),
                          ("coverage", chrono["coverage.json"]), ("causal manifest", causal["manifest"])):
        for field, expected in (("evidence_level", "SCENARIO"), ("enable_trading", False),
                                ("broker", "NOT_RUN"), ("historical_strict_execution", False),
                                ("real_order_send_calls", 0), ("real_order_cancel_calls", 0)):
            equal(record[field], expected, name + "." + field)
        # The older causal manifest has no data_kind field. Its accepted verdict,
        # pinned payloads and the safety fields above are its authority.
        if name != "causal manifest":
            equal(record["data_kind"], data_kind, name + ".data_kind")


def build_report(source_repo, output_dir):
    repo, output = path_value(source_repo).resolve(), path_value(output_dir).resolve()
    require(not output.exists(), "output directory already exists", source=str(output))
    require(not output.is_relative_to(repo) and not repo.is_relative_to(output), "output overlaps source repository")
    sources = Sources()
    with localcontext() as ctx:
        ctx.prec = 100
        ctx.traps[Inexact] = True
        chrono, previous, comparisons, causal = load_sources(repo, sources)
        for source in sources.records:
            directory = Path(source["path"]).parent
            require(not output.is_relative_to(directory) and not directory.is_relative_to(output),
                    "output overlaps a consumed source directory", source=str(directory))
        validate_safety(chrono, causal)
        years, paired, counts, accounts = reproduce_chronology(chrono)
        base, capital, selected, selected_capital = reproduce_previous(previous, comparisons, chrono["base_config.json"])
        regimes, causal_accounts = reproduce_causal(causal, amount(chrono["base_config.json"]["initial_cash"]))
        tables = {"year-all.csv": [r for r in years if r["view"] == "ALL"],
                  "year-primary.csv": [r for r in years if r["view"] == "PRIMARY"],
                  "paired-years.csv": paired, "scenario-base.csv": base, "capital-stress.csv": capital,
                  "causal-regime.csv": regimes, "conclusions.csv": make_conclusions(years),
                  "scope.csv": make_scope(chrono, accounts, selected, selected_capital, causal_accounts)}
        report, refs = render_report(tables)
        payloads = {name: csv_bytes(tables[name]) for name in CSV_NAMES}
        payloads[REPORT] = report
    sources.verify_again()
    check = {"schema": SCHEMA, "status": "PENDING_TERMINAL_SEAL", "sources": sources.records,
             "source_repo": str(repo), "tool": {"path": str(Path(__file__).resolve()),
                                                  **digest(Path(__file__).read_bytes())},
             "source_code_commit": None, "formula_version": "exact-decimal-full-path-j60-v1",
             "filters": {"annual_ALL": "all accepted rows", "annual_PRIMARY": "zero based j >= 60",
                         "previous_BASE": "scenario=BASE AND coverage_status=MEETS_60_OBSERVATIONS",
                         "previous_capital": "same three-strategy common identity set as previous BASE",
                         "causal_ALL": "all accepted attribution rows; separate earlier population",
                         "causal_PRIMARY": "observation_index >= 60; original labels unchanged"},
             "acceptance_count_reconciliation": counts,
             "reconciliation": {"annual_rows": len(years), "paired_rows": len(paired), "base_rows": len(base),
                                "capital_rows": len(capital), "regime_rows": len(regimes),
                                "all_consumed_amounts_and_counts_exact": True,
                                "source_children_or_input_bundle_verified": False,
                                "regime_summary_fields": ["gross_pnl", "fees", "slippage", "net_pnl",
                                   "observation_count", "raw_date_count", "active_observations", "fill_legs",
                                   "terminal_observations", "terminal_close_legs", "terminal_close_fees", "terminal_close_slippage"],
                                "price_regime_labels_recomputed": False},
             "artifacts": {name: digest(raw) for name, raw in payloads.items()}, "report_value_refs": refs,
             "command": None, "new_simulation": False, "new_training": False, "holdout_read": False,
             "evidence_level": "SCENARIO", "statistical_qualification": "NOT_ASSESSED",
             "safety": {"enable_trading": False, "observer": "hold", "broker": "NOT_RUN",
                        "real_order_send_calls": 0, "real_order_cancel_calls": 0,
                        "credentials_read": False, "network_calls": 0, "services_or_UI_changed": False}}
    payloads["CHECK.json"] = json_bytes(check)
    # Serialize everything before creating the destination. Keep a failed staging
    # directory as evidence; an incomplete destination is never published.
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=output.name + ".staging-", dir=output.parent))
    for name, raw in payloads.items():
        (staging / name).write_bytes(raw)
    require(not output.exists(), "output appeared during publication", source=str(output))
    staging.rename(output)
    return {"status": "GENERATED_PENDING_TERMINAL_SEAL", "aggregates": len(CSV_NAMES),
            "annual_rows": len(years), "paired_rows": len(paired), "new_simulation": False}


def seal_check(output_dir, command_record):
    """Seal an already terminated successful CLI result; never execute commands."""
    output = path_value(output_dir).resolve()
    require(isinstance(command_record, dict), "command record must be an object")
    require(type(command_record.get("raw_exit")) is int and command_record["raw_exit"] == 0,
            "nonzero or unknown child exit cannot be sealed", actual=command_record.get("raw_exit"))
    check_path = output / "CHECK.json"
    require(check_path.is_file(), "pending CHECK is missing")
    check = json_load(check_path.read_bytes())
    require(check["schema"] == SCHEMA and check["status"] == "PENDING_TERMINAL_SEAL", "CHECK is not awaiting seal")
    expected_files = {REPORT, *CSV_NAMES, "CHECK.json"}
    equal({p.name for p in output.iterdir()}, expected_files, "complete output set")
    equal(set(check["artifacts"]), expected_files - {"CHECK.json"}, "artifact pin set")
    for name, pin in check["artifacts"].items():
        equal(digest((output / name).read_bytes()), pin, "artifact before seal." + name)
    tool = path_value(check["tool"]["path"]).resolve()
    equal(digest(tool.read_bytes()), {k: check["tool"][k] for k in ("bytes", "sha256")}, "tool before seal")
    argv = command_record.get("argv")
    require(isinstance(argv, list) and len(argv) == 6 and all(isinstance(x, str) for x in argv), "invalid command argv")
    cwd = path_value(command_record.get("cwd")).resolve()
    equal((cwd / argv[1]).resolve(), tool, "sealed command tool")
    equal(argv[2], "--source-repo", "sealed command option")
    equal(path_value(argv[3]).resolve(), Path(check["source_repo"]), "sealed source repository")
    equal(argv[4], "--output-dir", "sealed command option")
    equal(path_value(argv[5]).resolve(), output, "sealed output directory")
    commit = command_record.get("source_code_commit")
    require(isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) is not None,
            "source code commit is required for sealing")
    for source in check["sources"]:
        equal(digest(Path(source["path"]).read_bytes()), source["actual"], "source before seal." + source["logical_id"])
    check["status"] = "SEALED_CHILD_EXIT_0"
    check["source_code_commit"] = commit
    check["command"] = {"argv": argv, "cwd": str(cwd), "raw_exit": command_record["raw_exit"],
                        "summary": "read-only report generated; all aggregate checks exact; no product hard-review claim"}
    raw = json_bytes(check)
    fd, temporary = tempfile.mkstemp(prefix=".CHECK-seal-", suffix=".tmp", dir=output)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
    os.replace(temporary, check_path)
    return check


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--source-repo", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    try:
        result = build_report(args.source_repo, args.output_dir)
    except (ValidationError, KeyError, ValueError, ArithmeticError) as exc:
        detail = exc.detail if isinstance(exc, ValidationError) else {"reason": str(exc), "type": type(exc).__name__}
        print(json.dumps({"status": "VALIDATION_FAILED", **detail}, ensure_ascii=True), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"status": "UNEXPECTED_ERROR", "type": type(exc).__name__, "reason": str(exc)},
                         ensure_ascii=True), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
