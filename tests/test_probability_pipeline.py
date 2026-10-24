from __future__ import annotations

import pytest

from ak_trading.probability.pipeline import ResearchConfig


ECONOMICS = {
    "initial_cash": "1000000", "lots": 1, "multiplier": "10", "price_tick": "1",
    "fee_per_lot": "0.01", "fee_rate": "0.0001", "slippage_ticks": "1",
    "margin_rate": "0.12", "max_margin_ratio": "0.40",
}


def config():
    return {
        "symbols": ["SHFE.rb2101"], "economics": ECONOMICS,
        "horizons": [1, 5, 20], "barriers_atr": [0.5, 1.0, 2.0],
        "action_horizon": 5, "routing_barrier_atr": 1.0,
        "min_samples": 30, "min_win_probability": 0.5, "min_ev": 0.0,
        "max_ambiguous_probability": 0.25, "seed": 7,
    }


def test_configuration_requires_explicit_economics_and_route_scenario():
    parsed = ResearchConfig.from_mapping(config())
    assert len(parsed.path_scenarios) == 9
    assert parsed.route_scenario.scenario_id == "h5-u1-d1"
    assert parsed.symbols == ("SHFE.rb2101",)
    wrong = config()
    wrong["routing_barrier_atr"] = 3
    with pytest.raises(ValueError):
        ResearchConfig.from_mapping(wrong)


def test_unknown_fields_and_continuous_symbols_are_rejected():
    wrong = config()
    wrong["silent_tune"] = True
    with pytest.raises(ValueError):
        ResearchConfig.from_mapping(wrong)
    wrong = config()
    wrong["symbols"] = ["KQ.m@SHFE.rb"]
    with pytest.raises(ValueError):
        ResearchConfig.from_mapping(wrong)


def test_mixed_product_config_cannot_reuse_one_contract_cost_assumption():
    wrong = config()
    wrong["symbols"] = ["SHFE.rb2101", "DCE.m2101"]
    with pytest.raises(ValueError, match="same product"):
        ResearchConfig.from_mapping(wrong)


def _bundle(path, rows=360):
    import math
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal
    from ak_trading.futures.io import bars_rows_from_bars, publish_input_bundle
    from ak_trading.futures.spec import ContractBar

    bars = []
    for i in range(rows):
        stamp = datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(days=i)
        opening = Decimal(str(round(100 + 5 * math.sin(i / 7) + i * 0.03, 4)))
        closing = opening + Decimal(str(round(math.sin(i / 3), 4)))
        width = Decimal("8") if i % 13 == 0 else Decimal("1")
        bars.append(ContractBar(
            "SHFE.rb2101", 86400, int(stamp.timestamp()) * 10**9, stamp, "synthetic_fixture",
            open=opening, close=closing, high=max(opening, closing) + width,
            low=min(opening, closing) - width, volume=Decimal(1000 + i), close_oi=Decimal(5000 + i),
        ))
    publish_input_bundle(
        path, dataset_kind="synthetic_fixture", created_at="2020-01-01T00:00:00Z",
        bars_rows=bars_rows_from_bars(bars),
        contracts=[{"raw_symbol": "SHFE.rb2101", "instance_id": "SHFE.rb2101|2021-01-15T15:00:00+08:00", "product": "SHFE.RB", "expiry_at": "2021-01-15T15:00:00+08:00"}],
        metadata={}, availability={}, request_info={}, source_hashes={}, read_only_transaction={}, clock_policy={},
    )


def test_offline_pipeline_produces_calibrated_probabilities_and_replays(tmp_path):
    import json
    from ak_trading.probability.pipeline import run_probability_research

    source = tmp_path / "input"
    _bundle(source)
    spec = config()
    spec["min_samples"] = 4
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(spec), encoding="utf-8")
    first, replay = tmp_path / "first", tmp_path / "replay"
    result = run_probability_research(source, first, cfg)
    assert result["status"] == "complete"
    report = json.loads((first / "report.json").read_bytes())
    assert report["counts"]["factor_rows"] == 360
    assert report["counts"]["scheduled_test_opportunities"] > 0
    assert report["partition_counts"]["purged"] == 40
    assert report["historical_strict_execution"] is False
    run_probability_research(source, replay, cfg)
    assert {p.name for p in first.iterdir()} == {p.name for p in replay.iterdir()}
    assert all(p.read_bytes() == (replay / p.name).read_bytes() for p in first.iterdir())


def test_hash_valid_duplicate_contract_key_is_rejected_before_output(tmp_path):
    import hashlib
    import json
    from ak_trading.probability.pipeline import run_probability_research

    source = tmp_path / "input"
    _bundle(source)
    raw = b'[{"raw_symbol":"SHFE.rb2201","raw_symbol":"SHFE.rb2101","instance_id":"fixture-rb","product":"SHFE.RB"}]'
    (source / "contracts.json").write_bytes(raw)
    manifest = json.loads((source / "manifest.json").read_bytes())
    manifest["content_hashes"]["contracts.json"] = hashlib.sha256(raw).hexdigest()
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(config()), encoding="utf-8")
    output = tmp_path / "out"
    with pytest.raises(ValueError, match="duplicate JSON"):
        run_probability_research(source, output, cfg)
    assert not output.exists()


def test_forged_product_option_and_instance_metadata_are_rejected(tmp_path):
    import hashlib
    import json
    from ak_trading.probability.pipeline import run_probability_research

    source = tmp_path / "input"
    _bundle(source)
    raw = json.dumps([{"raw_symbol": "SHFE.rb2101", "instance_id": "unrelated", "product": "DCE.M", "asset_class": "OPTION"}]).encode()
    (source / "contracts.json").write_bytes(raw)
    manifest = json.loads((source / "manifest.json").read_bytes())
    manifest["content_hashes"]["contracts.json"] = hashlib.sha256(raw).hexdigest()
    (source / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(config()), encoding="utf-8")
    with pytest.raises(ValueError, match="contract"):
        run_probability_research(source, tmp_path / "out", cfg)


def test_financial_json_literals_keep_decimal_precision():
    from decimal import Decimal
    from ak_trading.probability.artifacts import strict_json
    value = strict_json(b'{"initial_cash":100000000000000.000001}', decimal_floats=True)
    assert value["initial_cash"] == Decimal("100000000000000.000001")
