"""Unit tests for scripts/preflight_v2_research.py (K04).

Every input bundle, config and read-scope used here is a SYNTHETIC fixture
built through the production encoders (``encode_record_file`` /
``encode_csv_bytes``) and is never historical PIT evidence. The tests pin the
read-scope authorization gate (no payload read without proven non-holdout
coverage), the production money APIs behind the capital check, the conservative
bounds, output confinement and the exact CLI exit codes (0/2/3/4 asserted as
raw native codes in a capturing harness).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "preflight_v2_research.py"
FIXTURE_CONFIG = Path(__file__).parent / "fixtures" / "v2" / "m5_research.json"

sys.path.insert(0, str(ROOT / "scripts"))
import preflight_v2_research as pfr  # noqa: E402

from ak_trading.v2 import CONTRACT_VERSION, INPUT_SCHEMA_VERSION  # noqa: E402
from ak_trading.v2.contracts import (  # noqa: E402
    DataQuality,
    FeeLeg,
    Instrument,
    MarketBar,
    SessionWindow,
    TradingRule,
)
from ak_trading.v2.schemas import (  # noqa: E402
    AVAILABILITY_SCHEMA_ID,
    BARS_SCHEMA_ID,
    INSTRUMENTS_SCHEMA_ID,
    MACRO_ITEMS_SCHEMA_ID,
    RULES_SCHEMA_ID,
    SESSIONS_SCHEMA_ID,
    encode_csv_bytes,
    encode_record_file,
)

DAY_NANO = 86_400_000_000_000
BASE_NANO = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()) * 10**9
PRODUCT = "SYN.rb"
CONTRACT = "SYN.rb2609"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _quality() -> DataQuality:
    return DataQuality("READY", ("SYNTHETIC",), "reconstructed", "a" * 64)


def _sessions(days: int, *, evidence: str = "SCENARIO", product: str = PRODUCT, prefix: str = "d"):
    result = []
    for index in range(days):
        day_start = BASE_NANO + index * DAY_NANO
        start = day_start + 9 * 3600 * 10**9
        end = start + 4 * 3600 * 10**9
        result.append(SessionWindow(
            f"{prefix}{index}", product, date(2024, 1, 1) + timedelta(days=index),
            start, end, "DAY", "synthetic-k04", evidence))
    return tuple(result)


def _bars(days: int, minute_bars: int, sessions, *, price=D("100"), span=D("1.2"),
          attach_session=True, contract=CONTRACT, product=PRODUCT, delay_nanos=0):
    bars = []
    for index in range(days):
        session = sessions[index]
        session_id = session.session_id if attach_session else None
        for slot in range(minute_bars):
            start = session.start_nano + slot * 60 * 10**9
            end = start + 60 * 10**9
            level = price + D(index) / 100 + D(slot) / 1000
            bars.append(MarketBar(
                f"{contract}-m{index}-{slot}", contract, contract, product, 60, end, start, end,
                session.trading_day, session_id, end + delay_nanos, end + delay_nanos,
                level, level + span / 2, level - span / 2, level,
                D("1000"), D("1000"), D("1000"), True, 1, 1, "SYNTHETIC", _quality()))
        level = price + D(index) / 100
        bars.append(MarketBar(
            f"{contract}-d{index}", contract, contract, product, 86400, session.end_nano,
            session.start_nano, session.end_nano, session.trading_day, session_id,
            session.end_nano + delay_nanos, session.end_nano + delay_nanos,
            level, level + span / 2, level - span / 2, level,
            D("1000"), D("1000"), D("1000"), True, 240, 240, "SYNTHETIC", _quality()))
    return tuple(bars)


def _rule(*, open_fee_none=False, close_fee_none=False, contract=CONTRACT,
          rule_id="syn-rule"):
    leg = FeeLeg(D("1"), D("0"), False)
    return TradingRule(
        rule_id, contract, 0, BASE_NANO + 10**19, 0,
        D("10"), D("0.1"), 1, 1,
        None if open_fee_none else leg,
        None if close_fee_none else leg,
        leg, D("0.1"), D("0.1"), None, None, D("0"), D("0"), D("0.1"),
        "SCENARIO", "b" * 64)


def _write_package(directory: Path, *, sessions, bars, rules, instruments):
    """Write one SYNTHETIC frozen package from explicit record collections."""
    directory.mkdir(parents=True, exist_ok=False)
    payloads = {
        "instruments.json": encode_record_file("Instrument", INSTRUMENTS_SCHEMA_ID, tuple(instruments)),
        "sessions.json": encode_record_file("SessionWindow", SESSIONS_SCHEMA_ID, tuple(sessions)),
        "macro_items.json": encode_record_file("MacroItem", MACRO_ITEMS_SCHEMA_ID, ()),
        "rules.json": encode_record_file("TradingRule", RULES_SCHEMA_ID, tuple(rules)),
        "bars.csv": encode_csv_bytes(MarketBar, tuple(bars)),
    }
    availability = {
        "schema_version": AVAILABILITY_SCHEMA_ID,
        "input_manifest_sha256": None,
        "contract_status": {i.contract_id: "SYNTHETIC_COMPLETE" for i in instruments},
        "minute_history_status": "SYNTHETIC",
        "realtime_status": "NOT_APPLICABLE",
        "reasons": ["SYNTHETIC K04 unit-test fixture; not historical PIT data"],
    }
    import ak_trading.v2.schemas as schema_module
    payloads["availability.json"] = schema_module.canonical_json_bytes(availability)
    files = {name: _sha(payload) for name, payload in payloads.items()}
    start = min(session.start_nano for session in sessions)
    end = max(session.end_nano for session in sessions)
    request = {
        "contract_ids": [i.contract_id for i in instruments],
        "raw_symbols": [i.contract_id for i in instruments],
        "start_nano": start, "end_nano": end + 1,
        "as_of_nano": end + 1, "durations": [60, 86400],
        "page_size": 1000, "maximum_rows": 1_000_000, "maximum_bytes": 100_000_000,
    }
    manifest = {
        "schema_version": INPUT_SCHEMA_VERSION, "contract_version": CONTRACT_VERSION,
        "dataset_kind": "SYNTHETIC", "created_at_nano": end + 1,
        "request": request,
        "read_only_transaction": {"isolation": "NOT_APPLICABLE", "read_only": True, "verified": False},
        "files": files, "source_hashes": {"synthetic-world": "c" * 64},
        "limitations": ["SYNTHETIC K04 unit-test fixture; not historical PIT data"],
    }
    payloads["manifest.json"] = schema_module.canonical_json_bytes(manifest)
    for name, payload in payloads.items():
        (directory / name).write_bytes(payload)
    return {"manifest": manifest, "request": request}


def _write_bundle(directory: Path, *, days, minute_bars=12, sessions=None, bars=None,
                  rule=None, evidence="SCENARIO", product=PRODUCT):
    sessions = sessions if sessions is not None else _sessions(days, evidence=evidence, product=product)
    bars = bars if bars is not None else _bars(days, minute_bars, sessions)
    instrument = Instrument(CONTRACT, CONTRACT, PRODUCT, "synthetic", ("synthetic",),
                            BASE_NANO, None, None, None, _quality())
    return _write_package(directory, sessions=sessions, bars=bars,
                          rules=[rule or _rule()], instruments=[instrument])


def _write_multi_bundle(directory: Path, *, worlds, reverse=False, delay_nanos=0):
    """SYNTHETIC multi-contract/multi-product world.

    ``worlds`` is a list of ``(contract_id, product, days, minute_bars)``
    tuples; sessions belong to the product and are shared by its contracts;
    ``reverse`` flips the declaration order to prove order invariance.
    """
    products_days: dict[str, int] = {}
    for contract, product, days, minute_bars in worlds:
        products_days[product] = max(products_days.get(product, 0), days)
    sessions_by_product = {
        product: _sessions(days, product=product, prefix=product.replace(".", "_") + "-")
        for product, days in products_days.items()
    }
    sessions = [session for product in sessions_by_product for session in sessions_by_product[product]]
    bars, rules, instruments = [], [], []
    ordered = list(reversed(worlds)) if reverse else list(worlds)
    for contract, product, days, minute_bars in ordered:
        product_sessions = sessions_by_product[product][:days]
        bars.extend(_bars(days, minute_bars, product_sessions,
                          contract=contract, product=product, delay_nanos=delay_nanos))
        rules.append(_rule(contract=contract, rule_id="syn-rule-" + contract))
        instruments.append(Instrument(
            contract, contract, product, "synthetic", ("synthetic",),
            BASE_NANO, None, None, None, _quality()))
    return _write_package(directory, sessions=sessions, bars=bars,
                          rules=rules, instruments=instruments)


def _write_config(path: Path, *, equity="1000000", strategy_ids, days,
                  fit_template=None, final_holdout=False, contracts=None):
    template = json.loads(FIXTURE_CONFIG.read_text(encoding="utf-8"))
    fit_template = fit_template or template["model_fit_configs"]["synthetic-short"]
    first = BASE_NANO
    last = BASE_NANO + (days - 1) * DAY_NANO + 13 * 3600 * 10**9
    if last <= BASE_NANO + max(2, days // 2) * DAY_NANO:
        # Tiny worlds (fewer days than the staged split needs): stage the
        # three boundaries hourly inside the first day so ordering stays valid.
        train_end = BASE_NANO + 10 * 3600 * 10**9
        calibration_end = BASE_NANO + 11 * 3600 * 10**9
    else:
        train_end = BASE_NANO + max(1, days // 3) * DAY_NANO
        calibration_end = BASE_NANO + max(2, days // 2) * DAY_NANO
    test_end = last + 1
    split = {
        "split_id": "k04-synthetic",
        "train_start_nano": first, "train_end_nano": train_end,
        "calibration_start_nano": train_end, "calibration_end_nano": calibration_end,
        "test_start_nano": calibration_end, "test_end_nano": test_end,
        "embargo_minutes": 60, "embargo_days": 20, "final_holdout": final_holdout,
    }
    fit = dict(fit_template)
    fit["training_end_nano"] = train_end
    fit["calibration_end_nano"] = calibration_end
    fit["seed"] = template["seed"]
    fit["model_valid_until_nano"] = test_end + 365 * DAY_NANO
    config = dict(template)
    config["initial_cash_cny"] = equity
    config["strategy_ids"] = list(strategy_ids)
    config["split_specs"] = [split]
    config["model_fit_configs"] = {"k04-synthetic": fit}
    config["evaluation_as_of_nano"] = last + 1
    if contracts is not None:
        from ak_trading.v2.validation.experiments import HoldoutScope, digest as scope_digest
        contract_ids = [entry[0] for entry in contracts]
        products = sorted({entry[1] for entry in contracts})
        scope_raw = dict(template["holdout_scope"])
        scope_raw["products"] = products
        scope_raw["contract_ids"] = contract_ids
        scope = HoldoutScope(**{**scope_raw, "products": tuple(products),
                                "contract_ids": tuple(contract_ids)})
        config["contract_ids"] = contract_ids
        config["holdout_scope"] = scope_raw
        config["holdout_id"] = scope_digest(scope)
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return config


def _write_scope(path: Path, bundle_dir: Path, config_path: Path, *, reserved=(),
                 coverage=None, members=None, config_sha=None, manifest_sha=None):
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    request = manifest["request"]
    span = coverage or (request["start_nano"], request["end_nano"])
    scope = {
        "schema_version": "read-scope-v1",
        "authorization_reference": "K04 unit-test synthetic authorization (SYNTHETIC evidence only)",
        "config_sha256": config_sha or _sha(config_path.read_bytes()),
        "bundle_manifest_sha256": manifest_sha or _sha((bundle_dir / "manifest.json").read_bytes()),
        "permitted_members": members or [
            {"path": name, "sha256": digest, "coverage": {"start_nano": span[0], "end_nano": span[1]}}
            for name, digest in sorted(manifest["files"].items())
        ],
        "reserved_final_holdout_intervals": [
            {"start_nano": lo, "end_nano": hi} for lo, hi in reserved
        ],
    }
    path.write_text(json.dumps(scope, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return scope


def _run(*, bundle: Path, config: Path, output: Path, scope: Path | None = None) -> int:
    arguments = [
        "--input-bundle", str(bundle), "--config", str(config), "--output", str(output),
    ]
    if scope is not None:
        arguments += ["--read-scope", str(scope)]
    return pfr.main(arguments)


def _report(output: Path) -> dict:
    return json.loads((output / "preflight.json").read_text(encoding="utf-8"))


@pytest.fixture()
def possible_world(tmp_path: Path):
    """SYNTHETIC world with enough days, samples and capital for a trend cell."""
    bundle_dir = tmp_path / "bundle"
    _write_bundle(bundle_dir, days=220, minute_bars=12)
    config_path = tmp_path / "config.json"
    _write_config(config_path, strategy_ids=["trend_m1_v2"], days=220)
    scope_path = tmp_path / "read-scope.json"
    _write_scope(scope_path, bundle_dir, config_path)
    return bundle_dir, config_path, scope_path


# ---------------------------------------------------------------------------
# Unit-level checks of the money/geometry machinery
# ---------------------------------------------------------------------------

from ak_trading.v2.strategies.registry import strategy_specs_v2  # noqa: E402


def test_admissible_lot_plans_preserve_registered_multipliers():
    specs = {spec.strategy_id: spec
             for mode in ("MINUTE", "DAILY") for spec in strategy_specs_v2(mode)}
    rule = _rule()
    trend = pfr._admissible_lot_plans(specs["trend_m1_v2"], rule)
    grid = pfr._admissible_lot_plans(specs["grid_m1_v2"], rule)
    martingale = pfr._admissible_lot_plans(specs["martingale_m1_v2"], rule)
    assert trend == [(1,), (2,), (3,), (4,), (5,), (6,), (7,)]
    assert grid == [(1, 1, 1), (2, 2, 2)]  # all three layers, research cap 7
    assert martingale == [(1, 2, 4)]  # registered 1+2+4, sum exactly at the cap


def test_universal_fee_floor_requires_every_leg():
    assert pfr._universal_fee_floor(_rule(), (1,)) == D("2")
    assert pfr._universal_fee_floor(_rule(close_fee_none=True), (1,)) is None


# ---------------------------------------------------------------------------
# Positive pipeline and verdict semantics
# ---------------------------------------------------------------------------

def test_possible_case_witnesses_production_money(tmp_path: Path, possible_world):
    bundle, config, scope = possible_world
    output = tmp_path / "out-possible"
    # Integrated expectation (was exit 4 / confirmation UNKNOWN while the K01
    # helper was pending): with the reviewed helper transferred, the
    # legitimate synthetic possible case completes with exit 0.
    assert _run(bundle=bundle, config=config, output=output, scope=scope) == 0
    report = _report(output)
    capital = report["checks"]["capital"]
    assert capital["verdict"] == "POSSIBLE"
    witness = capital["evidence"]["trend_m1_v2|SYN.rb2609"]["witness"]
    assert witness["lots"] == [1]  # smallest legal plan suffices at 1,000,000 equity
    assert D(witness["planned_loss_cny"]) <= D(report["checks"]["capital"]["evidence"]["trend_m1_v2|SYN.rb2609"]["caps_cny"]["round"])
    assert report["checks"]["calibration"]["verdict"] == "POSSIBLE"
    assert report["checks"]["payoff_support"]["verdict"] == "POSSIBLE"
    assert report["checks"]["confirmation"]["verdict"] == "POSSIBLE"
    confirmation = report["checks"]["confirmation"]["evidence"]["trend_m1_v2|SYN.rb"]
    assert confirmation["helper"] == "ak_trading.v2.strategies.registry._planned_setup_expiry_nano"
    assert confirmation["witness"]["planned_expiry_nano"] > confirmation["witness"]["establishment_bar_end_nano"]
    assert confirmation["mode"] == "MINUTE"
    assert report["structurally_empty"] is False
    assert report["exit"] == 0
    markdown = (output / "preflight.md").read_text(encoding="utf-8")
    assert "SYNTHETIC" in markdown
    assert (output / "preflight.json").exists()


def test_confirmation_uses_reviewed_k01_helper_with_daily_semantics(tmp_path: Path):
    """The confirmation check must CALL the actual reviewed helper with the
    production argument convention (establishment bar end + confirmation
    count) and record the helper's source identity."""
    bundle_dir = tmp_path / "bundle"
    _write_bundle(bundle_dir, days=40, minute_bars=6)
    config_path = tmp_path / "config.json"
    _write_config(config_path, strategy_ids=["trend_m1_v2", "trend_d1_v2"], days=40)
    scope_path = tmp_path / "read-scope.json"
    _write_scope(scope_path, bundle_dir, config_path)
    output = tmp_path / "out-k01"
    assert _run(bundle=bundle_dir, config=config_path, output=output, scope=scope_path) == 0
    report = _report(output)
    evidence = report["checks"]["confirmation"]["evidence"]
    import hashlib as _hashlib
    registry_sha = _hashlib.sha256(
        (Path(__file__).parents[1] / "src/ak_trading/v2/strategies/registry.py").read_bytes()
    ).hexdigest()
    for key, mode, setup_bars in (
        ("trend_m1_v2|SYN.rb", "MINUTE", 3),
        ("trend_d1_v2|SYN.rb", "DAILY", 3),
    ):
        record = evidence[key]
        assert record["mode"] == mode
        assert record["confirmation_bars"] == setup_bars
        assert record["helper_source_sha256"] == registry_sha
        assert record["verdict"] == "POSSIBLE"
        # DAILY: expiry is the close of the 3rd distinct trading day after the
        # establishment bar (establishment day counts as #1).
        if mode == "DAILY":
            witness = record["witness"]
            day_nano = 86_400_000_000_000
            assert witness["planned_expiry_nano"] - witness["establishment_bar_end_nano"] == 2 * day_nano


def test_confirmation_insufficient_daily_calendar_stays_unknown(tmp_path: Path):
    """A DAILY strategy on a calendar with fewer distinct trading days than
    the confirmation span gets helper None everywhere: UNKNOWN, never a
    manufactured expiry or wall-day extrapolation."""
    worlds = [("SYN.rb2609", "SYN.rb", 2, 12)]
    bundle = tmp_path / "bundle"
    _write_multi_bundle(bundle, worlds=worlds)
    config = tmp_path / "config.json"
    _write_config(config, strategy_ids=["trend_d1_v2"], days=2, contracts=worlds)
    scope = tmp_path / "read-scope.json"
    _write_scope(scope, bundle, config)
    output = tmp_path / "out-insufficient"
    assert _run(bundle=bundle, config=config, output=output, scope=scope) == 3
    report = _report(output)
    record = report["checks"]["confirmation"]["evidence"]["trend_d1_v2|SYN.rb"]
    assert record["verdict"] == "UNKNOWN"
    assert "witness" not in record
    assert "insufficient" in record["reason"]
    # The cell is proven empty through calibration (2 days), never through a
    # fabricated timing verdict.
    row = report["cells"][0]
    assert row["verdicts"]["confirmation"] == "UNKNOWN"
    assert row["proven_impossible"] is True


def test_short_window_is_provably_empty(tmp_path: Path):
    bundle_dir = tmp_path / "bundle"
    _write_bundle(bundle_dir, days=8, minute_bars=4)
    all_fourteen = json.loads(FIXTURE_CONFIG.read_text(encoding="utf-8"))["strategy_ids"]
    config_path = tmp_path / "config.json"
    _write_config(config_path, strategy_ids=all_fourteen, days=8)
    scope_path = tmp_path / "read-scope.json"
    _write_scope(scope_path, bundle_dir, config_path)
    output = tmp_path / "out-empty"
    assert _run(bundle=bundle_dir, config=config_path, output=output, scope=scope_path) == 3
    report = _report(output)
    assert report["structurally_empty"] is True
    assert report["checks"]["calibration"]["verdict"] == "IMPOSSIBLE"
    evidence = next(iter(report["checks"]["calibration"]["evidence"].values()))
    assert evidence["unique_trading_day_blocks"] == 8 < evidence["minimum_blocks"]
    wait_note = [note for note in report["notes"] if note.startswith("WAIT strategies")]
    assert wait_note, "WAIT must be excluded from risk cells explicitly"
    grid_record = report["checks"]["capital"]["evidence"].get("grid_m1_v2|SYN.rb2609")
    assert grid_record["admissible_lot_plans"] == [[1, 1, 1], [2, 2, 2]]


def test_small_capital_universal_bound_proves_impossible(tmp_path: Path):
    bundle_dir = tmp_path / "bundle"
    _write_bundle(bundle_dir, days=40, minute_bars=6)
    config_path = tmp_path / "config.json"
    # equity 10: round cap = 10 * 0.005 = 0.05, far below the 2 CNY per-lot fee floor.
    _write_config(config_path, equity="10", strategy_ids=["trend_m1_v2"], days=40)
    scope_path = tmp_path / "read-scope.json"
    _write_scope(scope_path, bundle_dir, config_path)
    output = tmp_path / "out-small"
    assert _run(bundle=bundle_dir, config=config_path, output=output, scope=scope_path) == 3
    report = _report(output)
    record = report["checks"]["capital"]["evidence"]["trend_m1_v2|SYN.rb2609"]
    assert record["verdict"] == "IMPOSSIBLE"
    assert "universal lower bound" in record["reason"]
    assert D(record["universal_fee_floor_cny"]) == D("2")
    assert D(record["caps_cny"]["round"]) == D("0.050")


def test_threshold_equality_is_not_impossibility(tmp_path: Path):
    bundle_dir = tmp_path / "bundle"
    _write_bundle(bundle_dir, days=40, minute_bars=6)
    config_path = tmp_path / "config.json"
    # equity 400: round cap = 400 * 0.005 = 2.0 == universal fee floor.
    _write_config(config_path, equity="400", strategy_ids=["trend_m1_v2"], days=40)
    scope_path = tmp_path / "read-scope.json"
    _write_scope(scope_path, bundle_dir, config_path)
    output = tmp_path / "out-equality"
    assert _run(bundle=bundle_dir, config=config_path, output=output, scope=scope_path) == 4
    record = _report(output)["checks"]["capital"]["evidence"]["trend_m1_v2|SYN.rb2609"]
    assert D(record["universal_fee_floor_cny"]) == D("2")
    assert D(record["caps_cny"]["round"]) == D("2.000")
    assert record["verdict"] == "UNKNOWN"  # equality passes the cap; not proven impossible
    assert record["geometries_evaluated"] > 0  # exhaustive witness search ran


def test_missing_fees_yield_unknown_not_zero(tmp_path: Path):
    bundle_dir = tmp_path / "bundle"
    _write_bundle(bundle_dir, days=40, minute_bars=6, rule=_rule(open_fee_none=True))
    config_path = tmp_path / "config.json"
    _write_config(config_path, strategy_ids=["trend_m1_v2"], days=40)
    scope_path = tmp_path / "read-scope.json"
    _write_scope(scope_path, bundle_dir, config_path)
    output = tmp_path / "out-fees"
    assert _run(bundle=bundle_dir, config=config_path, output=output, scope=scope_path) == 4
    record = _report(output)["checks"]["capital"]["evidence"]["trend_m1_v2|SYN.rb2609"]
    assert record["verdict"] == "UNKNOWN"
    assert "missing fees/margin inputs" in record["reason"]


def test_range_family_geometry_stays_unknown_without_frozen_range(tmp_path: Path):
    bundle_dir = tmp_path / "bundle"
    _write_bundle(bundle_dir, days=40, minute_bars=6)
    config_path = tmp_path / "config.json"
    _write_config(config_path, strategy_ids=["grid_m1_v2"], days=40)
    scope_path = tmp_path / "read-scope.json"
    _write_scope(scope_path, bundle_dir, config_path)
    output = tmp_path / "out-grid"
    assert _run(bundle=bundle_dir, config=config_path, output=output, scope=scope_path) == 4
    record = _report(output)["checks"]["capital"]["evidence"]["grid_m1_v2|SYN.rb2609"]
    assert record["verdict"] == "UNKNOWN"
    assert "frozen range" in record["reason"]


def test_unknown_calendar_evidence_blocks_data_check_only(tmp_path: Path):
    bundle_dir = tmp_path / "bundle"
    sessions = _sessions(40, evidence="UNKNOWN")
    _write_bundle(bundle_dir, days=40, minute_bars=6, sessions=sessions,
                  bars=_bars(40, 6, sessions))
    config_path = tmp_path / "config.json"
    _write_config(config_path, strategy_ids=["trend_m1_v2"], days=40)
    scope_path = tmp_path / "read-scope.json"
    _write_scope(scope_path, bundle_dir, config_path)
    output = tmp_path / "out-cal"
    assert _run(bundle=bundle_dir, config=config_path, output=output, scope=scope_path) == 4
    report = _report(output)
    assert report["checks"]["data_calendar"]["verdict"] == "UNKNOWN"
    assert report["checks"]["data_calendar"]["evidence"]["dataset_kind"] == "SYNTHETIC"
    assert report["checks"]["data_calendar"]["evidence"]["relabeling_guard"].startswith("dataset_kind")


def test_missing_sessions_for_product_keep_payoff_unknown(tmp_path: Path):
    bundle_dir = tmp_path / "bundle"
    # The bundle's sessions cover another product; SYN.rb bars carry no
    # session reference, so no calendar bounds exist for the cell's product.
    other_sessions = _sessions(40, product="OTHER.product")
    bars = _bars(40, 6, other_sessions, attach_session=False)
    _write_bundle(bundle_dir, days=40, minute_bars=6, sessions=other_sessions, bars=bars)
    config_path = tmp_path / "config.json"
    _write_config(config_path, strategy_ids=["trend_m1_v2"], days=40)
    scope_path = tmp_path / "read-scope.json"
    _write_scope(scope_path, bundle_dir, config_path)
    output = tmp_path / "out-nosessions"
    assert _run(bundle=bundle_dir, config=config_path, output=output, scope=scope_path) == 4
    payoff = _report(output)["checks"]["payoff_support"]
    record = payoff["evidence"]["trend_m1_v2|SYN.rb"]
    assert record["verdict"] == "UNKNOWN"
    assert "no calendar sessions" in record["reason"]


# ---------------------------------------------------------------------------
# Read-scope authorization gate: no payload read before proven coverage
# ---------------------------------------------------------------------------

def _recorder(monkeypatch):
    calls = []

    def forbidden(path):
        calls.append(str(path))
        raise AssertionError("read_frozen_input must not be called without authorized coverage")

    monkeypatch.setattr(pfr, "read_frozen_input", forbidden)
    return calls


def test_no_read_scope_is_unknown_before_any_payload_read(tmp_path: Path, possible_world, monkeypatch):
    bundle, config, _scope = possible_world
    calls = _recorder(monkeypatch)
    output = tmp_path / "out-noscope"
    assert _run(bundle=bundle, config=config, output=output) == 4
    assert calls == []
    report = _report(output)
    assert report["inputs"]["bundle"]["loaded"] is False
    assert report["inputs"]["read_scope"]["provided"] is False
    for check in report["checks"].values():
        assert check["verdict"] == "UNKNOWN"


def test_holdout_overlap_refuses_payload_read(tmp_path: Path, possible_world, monkeypatch):
    bundle, config, _scope = possible_world
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    request = manifest["request"]
    bad_scope = tmp_path / "scope-overlap.json"
    _write_scope(bad_scope, bundle, config,
                 reserved=[(request["start_nano"], request["start_nano"] + DAY_NANO)])
    calls = _recorder(monkeypatch)
    output = tmp_path / "out-overlap"
    assert _run(bundle=bundle, config=config, output=output, scope=bad_scope) == 4
    assert calls == []


def test_scope_config_hash_mismatch_refuses_payload_read(tmp_path: Path, possible_world, monkeypatch):
    bundle, config, _scope = possible_world
    bad_scope = tmp_path / "scope-cfg.json"
    _write_scope(bad_scope, bundle, config, config_sha="0" * 64)
    calls = _recorder(monkeypatch)
    output = tmp_path / "out-cfg"
    assert _run(bundle=bundle, config=config, output=output, scope=bad_scope) == 4
    assert calls == []


def test_final_holdout_split_intersecting_coverage_refuses_read(tmp_path: Path, monkeypatch):
    bundle_dir = tmp_path / "bundle"
    _write_bundle(bundle_dir, days=40, minute_bars=6)
    config_path = tmp_path / "config.json"
    _write_config(config_path, strategy_ids=["trend_m1_v2"], days=40, final_holdout=True)
    scope_path = tmp_path / "read-scope.json"
    _write_scope(scope_path, bundle_dir, config_path)  # covers the whole request range
    calls = _recorder(monkeypatch)
    output = tmp_path / "out-holdout-split"
    assert _run(bundle=bundle_dir, config=config_path, output=output, scope=scope_path) == 4
    assert calls == []


def test_scope_missing_member_refuses_payload_read(tmp_path: Path, possible_world, monkeypatch):
    bundle, config, _scope = possible_world
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    members = [
        {"path": name, "sha256": digest,
         "coverage": {"start_nano": manifest["request"]["start_nano"], "end_nano": manifest["request"]["end_nano"]}}
        for name, digest in sorted(manifest["files"].items())
    ][:-1]
    partial_scope = tmp_path / "scope-partial.json"
    _write_scope(partial_scope, bundle, config, members=members)
    calls = _recorder(monkeypatch)
    output = tmp_path / "out-partial"
    assert _run(bundle=bundle, config=config, output=output, scope=partial_scope) == 4
    assert calls == []


# ---------------------------------------------------------------------------
# Write discipline
# ---------------------------------------------------------------------------

def _tree_snapshot(root: Path) -> dict:
    return {
        item.relative_to(root).as_posix(): _sha(item.read_bytes())
        for item in sorted(root.rglob("*")) if item.is_file()
    }


def test_inputs_never_written_and_output_confined(tmp_path: Path, possible_world):
    bundle, config, scope = possible_world
    before = _tree_snapshot(tmp_path)
    output = tmp_path / "out-confined"
    # Integrated expectation: the full possible case now completes exit 0 and
    # must still write nothing outside the two output files.
    assert _run(bundle=bundle, config=config, output=output, scope=scope) == 0
    written = sorted(item.relative_to(tmp_path).as_posix() for item in tmp_path.rglob("*") if item.is_file())
    allowed = sorted(set(before) | {"out-confined/preflight.json", "out-confined/preflight.md"})
    assert written == allowed
    assert set(item.name for item in output.iterdir()) == {"preflight.json", "preflight.md"}


def test_output_inside_bundle_rejected(tmp_path: Path, possible_world):
    bundle, config, scope = possible_world
    output = bundle / "nested-out"
    assert _run(bundle=bundle, config=config, output=output, scope=scope) == 2


def test_existing_output_rejected(tmp_path: Path, possible_world):
    bundle, config, scope = possible_world
    output = tmp_path / "already-there"
    output.mkdir()
    assert _run(bundle=bundle, config=config, output=output, scope=scope) == 2


# ---------------------------------------------------------------------------
# H7 and invalid inputs
# ---------------------------------------------------------------------------

def test_h7_null_selection_is_missing_prerequisite(tmp_path: Path):
    metadata = tmp_path / "h7-metadata.json"
    metadata.write_text(json.dumps({"task": "H7", "selected_input": None}), encoding="utf-8")
    config_path = tmp_path / "config.json"
    _write_config(config_path, strategy_ids=["trend_m1_v2"], days=10)
    output = tmp_path / "out-h7"
    assert _run(bundle=metadata, config=config_path, output=output) == 4
    assert not output.exists()


def test_invalid_config_cash_rejected(tmp_path: Path):
    template = json.loads(FIXTURE_CONFIG.read_text(encoding="utf-8"))
    template["initial_cash_cny"] = "0"
    config_path = tmp_path / "config-bad.json"
    config_path.write_text(json.dumps(template), encoding="utf-8")
    assert _run(bundle=tmp_path, config=config_path, output=tmp_path / "o") == 2


def test_unknown_strategy_id_rejected(tmp_path: Path):
    config_path = tmp_path / "config.json"
    _write_config(config_path, strategy_ids=["nonexistent_m1_v2"], days=10)
    assert _run(bundle=tmp_path, config=config_path, output=tmp_path / "o") == 2


def test_duplicate_config_key_rejected(tmp_path: Path):
    config_path = tmp_path / "config-dup.json"
    config_path.write_text(
        '{"schema_version": "a", "schema_version": "a"}', encoding="utf-8")
    assert _run(bundle=tmp_path, config=config_path, output=tmp_path / "o") == 2


def test_malformed_read_scope_rejected(tmp_path: Path, possible_world):
    bundle, config, _scope = possible_world
    bad_scope = tmp_path / "scope-bad.json"
    bad_scope.write_text('{"schema_version": "read-scope-v1",', encoding="utf-8")
    assert _run(bundle=bundle, config=config, output=tmp_path / "o", scope=bad_scope) == 2


def test_source_has_no_fit_registration_or_runner_references():
    text = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("fit_forecast", "register_experiment", "run_shadow", "research_probability_v2",
                      "publish_input_collection"):
        assert forbidden not in text, forbidden


# ---------------------------------------------------------------------------
# CLI exit codes as exact native codes in a capturing harness
# ---------------------------------------------------------------------------

def _run_cli(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", "-X", "utf8", str(SCRIPT), *arguments],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )


def test_cli_exact_exit_codes(tmp_path: Path, possible_world):
    bundle, config, scope = possible_world
    completed = _run_cli(
        "--input-bundle", str(bundle), "--config", str(config),
        "--output", str(tmp_path / "cli-possible"), "--read-scope", str(scope))
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    assert "structurally_empty=False" in completed.stdout

    completed = _run_cli(
        "--input-bundle", str(bundle), "--config", str(config),
        "--output", str(tmp_path / "cli-noscope"))
    assert completed.returncode == 4
    assert "INVALID" not in completed.stderr

    completed = _run_cli(
        "--input-bundle", str(bundle), "--config", str(config),
        "--output", str(tmp_path / "cli-exists"), "--read-scope", str(scope))
    assert completed.returncode == 0  # first run created it
    completed = _run_cli(
        "--input-bundle", str(bundle), "--config", str(config),
        "--output", str(tmp_path / "cli-exists"), "--read-scope", str(scope))
    assert completed.returncode == 2
    assert "already exists" in completed.stderr

    bad_config = tmp_path / "bad.json"
    bad_config.write_text("{}", encoding="utf-8")
    completed = _run_cli(
        "--input-bundle", str(bundle), "--config", str(bad_config),
        "--output", str(tmp_path / "cli-bad"))
    assert completed.returncode == 2

    metadata = tmp_path / "h7.json"
    metadata.write_text(json.dumps({"selected_input": None}), encoding="utf-8")
    completed = _run_cli(
        "--input-bundle", str(metadata), "--config", str(config),
        "--output", str(tmp_path / "cli-h7"))
    assert completed.returncode == 4
    assert "selected_input=null" in completed.stderr


def test_cli_short_window_proven_empty_exit_three(tmp_path: Path):
    bundle_dir = tmp_path / "bundle"
    _write_bundle(bundle_dir, days=8, minute_bars=4)
    config_path = tmp_path / "config.json"
    _write_config(config_path, strategy_ids=["trend_m1_v2", "wait_m1_v2"], days=8)
    scope_path = tmp_path / "read-scope.json"
    _write_scope(scope_path, bundle_dir, config_path)
    completed = _run_cli(
        "--input-bundle", str(bundle_dir), "--config", str(config_path),
        "--output", str(tmp_path / "cli-empty"), "--read-scope", str(scope_path))
    assert completed.returncode == 3, (completed.stdout, completed.stderr)
    report = json.loads((tmp_path / "cli-empty" / "preflight.json").read_text(encoding="utf-8"))
    assert report["structurally_empty"] is True


# ---------------------------------------------------------------------------
# F1/F2 repair regressions (review r4-k04-source-review01, SYNTHETIC worlds)
# ---------------------------------------------------------------------------

PR2_WORLDS = [
    ("SYN.rb2609", "SYN.rb", 40, 3),   # 120 eligible minute bars each
    ("SYN.rb2701", "SYN.rb", 40, 3),   # aggregate 240 >= 200; unique days 40
]


def test_f1_pr2_two_contracts_aggregate_not_first_contract(tmp_path: Path):
    """PR2: the sample bound must sum over configured contracts (240, not the
    first contract's 120) and unique trading-day blocks stay 40 (not 80)."""
    bundle = tmp_path / "bundle"
    _write_multi_bundle(bundle, worlds=PR2_WORLDS)
    config = tmp_path / "config.json"
    _write_config(config, strategy_ids=["trend_m1_v2"], days=40, contracts=PR2_WORLDS)
    scope = tmp_path / "read-scope.json"
    _write_scope(scope, bundle, config)
    output = tmp_path / "out-pr2"
    # Integrated expectation (was exit 4 pending K01): with confirmation
    # integrated the aggregated legitimate cell completes exit 0.
    assert _run(bundle=bundle, config=config, output=output, scope=scope) == 0
    report = _report(output)
    assert report["structurally_empty"] is False
    evidence = report["checks"]["calibration"]["evidence"]
    record = evidence["trend_m1_v2|SYN.rb|MINUTE|TRADING_MINUTE=30"]
    assert record["sample_upper_bound"] == 240
    assert record["unique_trading_day_blocks"] == 40
    assert record["per_contract_counts"] == {"SYN.rb2609": 120, "SYN.rb2701": 120}
    assert record["verdict"] == "POSSIBLE"
    row = next(row for row in report["cells"] if row["cell"] == "trend_m1_v2|SYN.rb2609")
    assert row["verdicts"]["calibration"] == "POSSIBLE"
    assert row["proven_impossible"] is False


def test_f1_contract_order_invariance(tmp_path: Path):
    """Inverse declaration order must not change bounds, verdicts or exit."""
    results = []
    for reverse in (False, True):
        bundle = tmp_path / f"bundle-{'rev' if reverse else 'fwd'}"
        _write_multi_bundle(bundle, worlds=PR2_WORLDS, reverse=reverse)
        config = tmp_path / f"config-{'rev' if reverse else 'fwd'}.json"
        _write_config(config, strategy_ids=["trend_m1_v2"], days=40, contracts=PR2_WORLDS)
        scope = tmp_path / f"scope-{'rev' if reverse else 'fwd'}.json"
        _write_scope(scope, bundle, config)
        output = tmp_path / f"out-{'rev' if reverse else 'fwd'}"
        code = _run(bundle=bundle, config=config, output=output, scope=scope)
        report = _report(output)
        record = report["checks"]["calibration"]["evidence"]["trend_m1_v2|SYN.rb|MINUTE|TRADING_MINUTE=30"]
        results.append((code, report["structurally_empty"], record["sample_upper_bound"],
                        record["unique_trading_day_blocks"], record["verdict"]))
    assert results[0] == results[1]
    assert results[0] == (0, False, 240, 40, "POSSIBLE")


def test_f1_duplicate_configuration_does_not_inflate_bound(tmp_path: Path):
    """Two strategies sharing one product population must not double-count it."""
    bundle = tmp_path / "bundle"
    _write_multi_bundle(bundle, worlds=PR2_WORLDS)
    config = tmp_path / "config.json"
    _write_config(config, strategy_ids=["trend_m1_v2", "pullback_m1_v2"], days=40,
                  contracts=PR2_WORLDS)
    scope = tmp_path / "read-scope.json"
    _write_scope(scope, bundle, config)
    output = tmp_path / "out-dup"
    assert _run(bundle=bundle, config=config, output=output, scope=scope) == 0
    evidence = _report(output)["checks"]["calibration"]["evidence"]
    for key in ("trend_m1_v2|SYN.rb|MINUTE|TRADING_MINUTE=30",
                "pullback_m1_v2|SYN.rb|MINUTE|TRADING_MINUTE=30"):
        assert evidence[key]["sample_upper_bound"] == 240  # not 480
        assert evidence[key]["unique_trading_day_blocks"] == 40  # not 80


def test_f2_pr1_product_b_never_borrows_product_a(tmp_path: Path):
    """PR1: product A short (96 < 200), product B ample (2640 >= 200). B's row
    must carry its OWN verdict; no false proven-empty from borrowing."""
    worlds = [
        ("SYN.rbA2609", "SYN.rbA", 8, 12),      # 96 eligible minute bars
        ("SYN.rbB2701", "SYN.rbB", 220, 12),    # 2640 eligible minute bars
    ]
    bundle = tmp_path / "bundle"
    _write_multi_bundle(bundle, worlds=worlds)
    config = tmp_path / "config.json"
    _write_config(config, strategy_ids=["trend_m1_v2"], days=220, contracts=worlds)
    scope = tmp_path / "read-scope.json"
    _write_scope(scope, bundle, config)
    output = tmp_path / "out-pr1"
    # Integrated expectation (was exit 4 pending K01): B's cell is now fully
    # possible, so the run completes exit 0 — still never a false exit 3.
    assert _run(bundle=bundle, config=config, output=output, scope=scope) == 0
    report = _report(output)
    assert report["structurally_empty"] is False
    evidence = report["checks"]["calibration"]["evidence"]
    assert evidence["trend_m1_v2|SYN.rbA|MINUTE|TRADING_MINUTE=30"]["sample_upper_bound"] == 96
    assert evidence["trend_m1_v2|SYN.rbA|MINUTE|TRADING_MINUTE=30"]["verdict"] == "IMPOSSIBLE"
    assert evidence["trend_m1_v2|SYN.rbB|MINUTE|TRADING_MINUTE=30"]["sample_upper_bound"] == 2640
    assert evidence["trend_m1_v2|SYN.rbB|MINUTE|TRADING_MINUTE=30"]["verdict"] == "POSSIBLE"
    row_a = next(row for row in report["cells"] if row["cell"] == "trend_m1_v2|SYN.rbA2609")
    row_b = next(row for row in report["cells"] if row["cell"] == "trend_m1_v2|SYN.rbB2701")
    assert row_a["verdicts"]["calibration"] == "IMPOSSIBLE"
    assert row_b["verdicts"]["calibration"] == "POSSIBLE"
    assert row_b["proven_impossible"] is False
    assert row_b["fully_possible"] is True


def test_f2_horizon_join_is_exact_not_first_configured(tmp_path: Path):
    """The daily strategy (horizon 5) must join its own population (196 < 200
    IMPOSSIBLE), never the first configured daily horizon's (200)."""
    worlds = [("SYN.rb2609", "SYN.rb", 201, 1)]
    bundle = tmp_path / "bundle"
    _write_multi_bundle(bundle, worlds=worlds)
    config = tmp_path / "config.json"
    _write_config(config, strategy_ids=["trend_m1_v2", "trend_d1_v2"], days=201,
                  contracts=worlds)
    scope = tmp_path / "read-scope.json"
    _write_scope(scope, bundle, config)
    output = tmp_path / "out-horizon"
    assert _run(bundle=bundle, config=config, output=output, scope=scope) == 0
    report = _report(output)
    evidence = report["checks"]["calibration"]["evidence"]
    own = evidence["trend_d1_v2|SYN.rb|DAILY|TRADING_DAY=5"]
    assert own["sample_upper_bound"] == 196  # 201 days minus 5 immature tail days
    assert own["verdict"] == "IMPOSSIBLE"
    populations = report["checks"]["calibration"]["population_bounds"]
    assert populations["SYN.rb|DAILY|TRADING_DAY=1"]["sample_bound"] == 200
    row_d1 = next(row for row in report["cells"] if row["cell"] == "trend_d1_v2|SYN.rb2609")
    assert row_d1["verdicts"]["calibration"] == "IMPOSSIBLE"
    row_m1 = next(row for row in report["cells"] if row["cell"] == "trend_m1_v2|SYN.rb2609")
    assert row_m1["verdicts"]["calibration"] == "POSSIBLE"


def test_delay_beyond_seven_days_still_counted(tmp_path: Path):
    """A bar available 8 days after its end is still a permitted decision;
    the removed undocumented 7-day cap must not understate the bound."""
    worlds = [("SYN.rb2609", "SYN.rb", 40, 6)]
    bundle = tmp_path / "bundle"
    _write_multi_bundle(bundle, worlds=worlds, delay_nanos=8 * DAY_NANO)
    config = tmp_path / "config.json"
    _write_config(config, strategy_ids=["trend_m1_v2"], days=40, contracts=worlds)
    scope = tmp_path / "read-scope.json"
    _write_scope(scope, bundle, config)
    output = tmp_path / "out-delay"
    assert _run(bundle=bundle, config=config, output=output, scope=scope) == 0
    report = _report(output)
    record = report["checks"]["calibration"]["evidence"]["trend_m1_v2|SYN.rb|MINUTE|TRADING_MINUTE=30"]
    assert record["sample_upper_bound"] == 240
    assert record["verdict"] == "POSSIBLE"
    delays = report["checks"]["data_calendar"]["evidence"]["availability_delay_nanos"]
    assert delays["max"] == 8 * DAY_NANO
    assert delays["median"] == 8 * DAY_NANO
    assert delays["min"] == 8 * DAY_NANO


def test_pr3_corrupt_payload_with_bad_scope_refused_before_read(tmp_path: Path):
    """PR3 on real bytes: hash-contradicting scope plus corrupt payload bytes
    on disk must refuse (exit 4, loaded=false) without reading any payload."""
    worlds = [("SYN.rb2609", "SYN.rb", 40, 6)]
    bundle = tmp_path / "bundle"
    _write_multi_bundle(bundle, worlds=worlds)
    config = tmp_path / "config.json"
    _write_config(config, strategy_ids=["trend_m1_v2"], days=40, contracts=worlds)
    (bundle / "bars.csv").write_bytes(b"CORRUPTED-BYTES\n")
    before = {p.name: _sha(p.read_bytes()) for p in sorted(bundle.iterdir())}
    bad_scope = tmp_path / "scope-bad.json"
    _write_scope(bad_scope, bundle, config, config_sha="0" * 64)
    output = tmp_path / "out-pr3"
    assert _run(bundle=bundle, config=config, output=output, scope=bad_scope) == 4
    report = _report(output)
    assert report["inputs"]["bundle"]["loaded"] is False
    assert report["inputs"]["read_scope"]["status"] == "REFUSED"
    after = {p.name: _sha(p.read_bytes()) for p in sorted(bundle.iterdir())}
    assert after == before

