from __future__ import annotations

import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "research_futures.py"


def test_real_strict_backtest_reports_capability_not_invented_data_gaps(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("research_capabilities_cli", SCRIPT)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    bundle = tmp_path / "real"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(
        json.dumps({"dataset_kind": "real_export"}), encoding="utf-8"
    )
    output = tmp_path / "out"
    code = cli.main([
        "--input-bundle", str(bundle), "--mode", "backtest", "--output", str(output)
    ])
    result = json.loads(capsys.readouterr().out)
    assert code == 3
    assert result["reason"] == "real_strict_backtest_not_implemented"
    assert result["implementation_status"] == "not_implemented"
    assert result["data_readiness"] == "not_assessed"
    assert "missing_inputs" not in result
    assert "backtest_history.py" in result["available_alternative"]
    assert not output.exists()
