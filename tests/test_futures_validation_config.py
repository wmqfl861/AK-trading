from __future__ import annotations

import importlib.util
from pathlib import Path
from decimal import Decimal

import pytest

ROOT = Path(__file__).resolve().parents[1]


def cli():
    spec = importlib.util.spec_from_file_location("futures_config_tests", ROOT / "scripts/research_futures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_policy_values_are_applied_and_defaults_recorded():
    module = cli()
    policy = module._policy_from_config({"policy": {"target_daily": 0.001, "initial_nav_cny": "12345", "base_slippage_ticks": 3}})
    assert policy.target_daily == 0.001
    assert policy.initial_nav_cny == Decimal("12345")
    assert policy.base_slippage_ticks == 3
    payload = module._policy_payload(policy)
    assert payload["target_daily"] == 0.001
    assert payload["initial_nav_cny"] == "12345"
    assert payload["gross_cap"] == 0.8


@pytest.mark.parametrize("values", [{"unknown": 3}, {"target_daily": True}, {"initial_nav_cny": "NaN"},
                                   {"target_daily": float("inf")}, {"participation_cap": 2},
                                   {"base_slippage_ticks": 1.5}, {"pair_gap_days_target": 999},
                                   {"initial_nav_cny": "-10"}])
def test_invalid_or_ignored_policy_values_rejected(values):
    with pytest.raises(ValueError):
        cli()._policy_from_config({"policy": values})


@pytest.mark.parametrize("section", ["rules", "contract", "session", "roll"])
def test_unknown_nested_replay_config_is_rejected(section):
    root = ROOT / "data/strategy_artifacts/futures-lib-20260907-codex-01/demo-final-main"
    if not root.exists():
        pytest.skip("retained demo fixture unavailable")
    import json
    config = json.loads((root / "config.json").read_text())
    first = next(iter(config["contracts"].values()))
    target = {"rules": first["rules"], "contract": first,
              "session": config["session_template"], "roll": config["roll"]}[section]
    target["ignored_override"] = 100
    with pytest.raises(ValueError, match="unsupported"):
        cli()._inputs_from_bundle(root / "input", config)


def test_reconstructed_fixture_bars_have_evidenced_session_clocks():
    root = ROOT / "data/strategy_artifacts/futures-lib-20260907-codex-01/demo-final-main"
    if not root.exists():
        pytest.skip("retained demo fixture unavailable")
    import json
    module = cli()
    inputs = module._inputs_from_bundle(root / "input", json.loads((root / "config.json").read_text()))
    assert inputs.source_manifest_sha256
    for series in inputs.bars.values():
        for bar in series.values():
            assert bar.session_open == bar.ts
            assert bar.session_close is not None
            assert bar.available_at >= bar.session_close
