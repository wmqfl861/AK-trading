from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "probe_exchange_rule_sources.py"


def _module():
    spec = importlib.util.spec_from_file_location("probe_exchange_rule_sources_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_response_analysis_accepts_raw_fallback_without_calling_it_normalized():
    module = _module()
    result = module.analyze_scrape_payload(
        {"success": True, "data": {"markdown": "  ", "html": "<p>raw</p>", "metadata": {"charset": "utf-8", "format": "html"}}}
    )
    assert result["status"] == "empty-normalized"
    assert result["normalized_content_length"] == 0
    assert result["raw_content_length"] == len("<p>raw</p>")
    assert result["charset"] == "utf-8"
    assert result["format"] == "html"


def test_response_analysis_reports_empty_everything():
    module = _module()
    result = module.analyze_scrape_payload({"success": True, "data": {}})
    assert result["status"] == "empty-normalized"
    assert result["normalized_content_length"] == 0
    assert result["raw_content_length"] == 0


def test_response_analysis_reports_normal_content_and_metadata_flags():
    module = _module()
    result = module.analyze_scrape_payload(
        {
            "success": True,
            "data": {
                "markdown": "# Rules\nFee",
                "html": "<h1>Rules</h1><p>Fee</p>",
                "metadata": {"statusCode": 200, "charset": "UTF-8", "format": "markdown", "cached": True},
                "fetcher": "scrapling",
            },
        }
    )
    assert result["status"] == "ok"
    assert result["normalized_content_length"] == len("# Rules\nFee")
    assert result["raw_content_length"] == len("<h1>Rules</h1><p>Fee</p>")
    assert result["source_http_status"] == 200
    assert result["flags"]["fetcher"] == "scrapling"
    assert result["flags"]["cached"] is True


def test_response_analysis_reports_malformed_json():
    module = _module()
    result = module.analyze_scrape_payload(b"{malformed")
    assert result["status"] == "error"
    assert result["error"] == "malformed-json"


def test_run_isolated_from_network_and_returns_zero_when_each_exchange_has_content(monkeypatch):
    module = _module()

    def fake_request(url, timeout):
        return 200, {"success": True, "data": {"markdown": "content", "html": "raw"}}, None

    monkeypatch.setattr(module, "_request_scrape", fake_request)
    code, output = module.run(["--catalog", str(ROOT / "config" / "china_futures_catalog.json"), "--exchange", "SHFE", "--sample-per-exchange", "1"])
    assert code == 0
    assert output["summary"]["per_exchange"]["SHFE"]["ok"] == 1
    assert len(output["results"]) == 1


def test_main_emits_ascii_json_for_transport_total_failure(monkeypatch, capsys):
    module = _module()

    def failed_request(url, timeout):
        return None, None, "transport-URLError"

    monkeypatch.setattr(module, "_request_scrape", failed_request)
    code = module.main(["--catalog", str(ROOT / "config" / "china_futures_catalog.json"), "--exchange", "SHFE", "--sample-per-exchange", "1"])
    assert code == 2
    captured = capsys.readouterr().out
    json.loads(captured)
    assert captured.isascii()


def test_main_returns_usage_code_for_invalid_sample_bound():
    module = _module()
    assert module.main(["--catalog", str(ROOT / "config" / "china_futures_catalog.json"), "--sample-per-exchange", "11"]) == 2
