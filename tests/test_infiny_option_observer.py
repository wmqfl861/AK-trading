from datetime import date

from ak_trading.infiny_bridge.decision import DailyObserver
from test_infiny_decision import make_history
from test_infiny_extended_protocol import option_payload


def test_option_telemetry_does_not_enter_future_strategy_or_prevent_service_start(tmp_path):
    bundle = tmp_path / "history"
    make_history(bundle)
    observer = DailyObserver(bundle, ["SHFE.rb2610", "SHFE.rb2610C3100"], expected_history_date=date(2026, 9, 8))
    decision = observer.evaluate(option_payload())
    assert decision["candidate_signal"] == "hold"
    assert decision["reason"] == "option_observation_only"
    assert observer.status()["observation_only_symbols"] == ["SHFE.rb2610C3100"]
