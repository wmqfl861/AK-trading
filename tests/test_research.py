from dataclasses import replace

import pytest

from ak_trading.research import settle_bar


def sample_bars(count=420):
    from datetime import datetime, timedelta, timezone
    import math
    from ak_trading.research import Bar
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    result = []
    for t in range(count):
        phase = t % 100
        close = 100 + 0.09 * phase + 2 * math.sin(t * 0.6)
        op = close / (1 + 0.004 * math.sin((t - 1) * 0.6))
        for symbol, scale in (("DEMO_A", 1), ("DEMO_B", 2)):
            result.append(Bar(symbol, start + timedelta(hours=t), start + timedelta(hours=t, minutes=59),
                              op * scale, max(op, close) * scale, min(op, close) * scale, close * scale))
    return result


def test_training_labels_stop_before_oos_and_fit_is_frozen():
    from ak_trading.research import ResearchConfig, fit_model, validate_bars
    bars = sample_bars()
    config = ResearchConfig(train_bars=200, min_samples=2)
    model = fit_model(bars, config)
    frames = validate_bars(bars)
    assert model["last_label_close"] == frames[config.train_bars - 1]["DEMO_A"].close_time.isoformat()
    assert model["last_signal_close"] == frames[config.train_bars - 2]["DEMO_A"].close_time.isoformat()
    assert model["training_frames"] == 200
    changed = bars[:400] + [replace(b, open=b.open * 1.01, high=b.high * 1.01, low=b.low * 1.01, close=b.close * 1.01) for b in bars[400:]]
    assert fit_model(changed, config) == model
    assert fit_model(bars[:600], config) == model
    assert model["correlation"]["DEMO_A"]["DEMO_B"] == pytest.approx(1)
    for bad in (bars[:-1], [replace(bars[0], close=float("nan")), *bars[1:]], [replace(bars[0], symbol="KQ.m@SHFE.rb"), *bars[1:]], [bars[0], *bars]):
        with pytest.raises(ValueError):
            validate_bars(bad)
    with pytest.raises(ValueError):
        validate_bars(bars[:40] + bars[42:])



def test_experts_are_concurrent_bounded_and_fail_closed():
    from ak_trading.research import ResearchConfig, evaluate_experts
    config = ResearchConfig(lookback=4)
    up = evaluate_experts([100, 101, 100, 101, 103], config)
    assert up["signals"] == {"breakout": 1.0, "range": -1.0}
    down = evaluate_experts([100, 99, 100, 99, 97], config)
    assert down["signals"] == {"breakout": -1.0, "range": 1.0}
    for values in ([1, 2], [1, 1, 1, 1, 1], [1, 2, 3, float("nan"), 4], [1, 2, 3, float("inf"), 4]):
        assert evaluate_experts(values, config)["signals"] == {"breakout": 0.0, "range": 0.0}
    assert sum(up["scores"].values()) == pytest.approx(1)
    assert min(up["scores"].values()) >= 0
    assert 0 <= up["uncertainty"] <= 1
    assert up["score_kind"] == "uncalibrated_heuristic"
    for kwargs in ({"train_bars": 99}, {"min_samples": 1}, {"fee_bps": float("nan")}, {"gross_cap": 2}, {"lookback": True}):
        with pytest.raises(ValueError):
            ResearchConfig(**kwargs)



def test_soft_state_mixture_fixed_slots_and_tiny_edge_damping():
    from ak_trading.research import ResearchConfig, allocate
    config = ResearchConfig(min_samples=2, uncertainty_limit=1)
    state = {"reason": "ready", "signals": {"breakout": 1.0, "range": -1.0},
             "scores": {"trend": 0.6, "range": 0.3, "uncertain": 0.1},
             "state": "trend", "uncertainty": 0.2, "volatility": 0.005}
    def cell(edge, available=True):
        return {"count": 50 if available else 0, "available": available,
                "mean": edge, "variance": 0.0001, "downside": 0.001, "edge": edge if available else None}
    table = {e: {"trend": cell(0.002), "range": cell(-0.01), "uncertain": cell(0, False)} for e in ("breakout", "range")}
    model = {"tables": {"A": table}, "clusters": [["A"]]}
    dynamic = allocate({"A": state}, model, config, mode="dynamic")
    fixed = allocate({"A": state}, model, config, mode="fixed")
    assert dynamic["budgets"]["A"]["breakout"]["evidence"]["mass"] == pytest.approx(0.9)
    assert dynamic["budgets"]["A"]["breakout"]["evidence"]["edge"] == pytest.approx(-0.0018)
    assert dynamic["budgets"]["A"]["breakout"]["risk_budget"] == 0
    assert fixed["budgets"]["A"]["breakout"]["risk_budget"] == pytest.approx(0.45)
    for expert in table.values():
        expert["range"] = cell(0.002)
    positive = allocate({"A": state}, model, config, mode="dynamic")
    assert all(positive["budgets"]["A"][e]["risk_budget"] > 0 for e in table)
    for expert in table.values():
        expert["trend"] = cell(1e-10)
        expert["range"] = cell(1e-10)
    tiny = allocate({"A": state}, model, config, mode="dynamic")
    assert sum(v["risk_budget"] for v in tiny["budgets"]["A"].values()) < 1e-6
    tiny_fixed = allocate({"A": state}, model, config, mode="fixed")
    assert tiny_fixed["budgets"]["A"]["breakout"]["risk_budget"] == pytest.approx(0.45)
    # Missing evidence stays cash without redistributing the other fixed slot.
    table["range"] = {r: cell(0, False) for r in state["scores"]}
    missing = allocate({"A": state}, model, config, mode="fixed")
    assert missing["budgets"]["A"]["range"]["risk_budget"] == 0
    assert missing["budgets"]["A"]["breakout"]["risk_budget"] == pytest.approx(0.45)


def test_risk_overlay_caps_correlation_volatility_and_kill():
    from ak_trading.research import ResearchConfig, apply_risk
    config = ResearchConfig(instrument_cap=0.4, gross_cap=0.8, cluster_cap=0.3, portfolio_vol_cap=0.004)
    result = apply_risk({"A": 0.8, "B": 0.8}, {"A": 0.01, "B": 0.01}, [["A", "B"]], config)
    assert result["targets"] == pytest.approx({"A": 0.15, "B": 0.15})
    assert "cluster_cap" in result["limits"]
    uncorrelated = apply_risk({"A": 0.8, "B": 0.8}, {"A": 0.01, "B": 0.01}, [["A"], ["B"]], config)
    assert sum(map(abs, uncorrelated["targets"].values())) == pytest.approx(0.4)
    assert "portfolio_vol_cap" in uncorrelated["limits"]
    assert apply_risk({"A": 0.8}, {"A": 0.01}, [["A"]], config, killed=True)["targets"] == {"A": 0.0}
    assert apply_risk({"A": 0.8}, {"A": float("nan")}, [["A"]], config)["targets"] == {"A": 0.0}


def test_runner_oos_timing_repeatability_and_future_prefix_invariance():
    from ak_trading.research import ResearchConfig, run_research
    bars = sample_bars(330)
    config = ResearchConfig(train_bars=200, min_samples=2, uncertainty_limit=1)
    report = run_research(bars, config)
    assert report == run_research(bars, config)
    assert report["rows"][0]["decision_close"] == bars[399].close_time.isoformat()
    assert report["rows"][0]["execution_open"] == bars[400].open_time.isoformat()
    assert len(report["rows"]) == 130
    prefix = run_research(bars[:540], config)
    assert prefix["rows"] == report["rows"][:70]
    changed = bars[:540] + [replace(b, open=b.open * 1.02, high=b.high * 1.02, low=b.low * 1.02, close=b.close * 1.02) for b in bars[540:]]
    assert run_research(changed, config)["rows"][:70] == prefix["rows"]
    for mode in ("dynamic", "fixed"):
        summary = report["summary"][mode]
        assert summary["bars"] == 130
        assert summary["final_equity"] == pytest.approx(1 + summary["net_pnl"])
        assert summary["net_pnl"] == pytest.approx(summary["gross_pnl"] - summary["fees"] - summary["slippage"])
        assert summary["final_exposure"] == 0
        assert summary["turnover"] > 0
    assert report["availability"]["carry"]["status"] == "unavailable_pending_valid_input"
    assert report["availability"]["macro"]["status"] == "unavailable_pending_valid_input"
    killed = run_research(bars, replace(config, kill_switch=True))
    assert all(killed["summary"][m]["turnover"] == 0 for m in ("dynamic", "fixed"))
    drawdown = run_research(bars, replace(config, max_drawdown=1e-9))
    for mode in ("dynamic", "fixed"):
        stopped = [i for i, row in enumerate(drawdown["rows"]) if row["portfolios"][mode]["killed"]]
        assert stopped
        assert all(row["portfolios"][mode]["turnover"] == 0 for row in drawdown["rows"][stopped[0]:])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, 0, True])
def test_settlement_rejects_invalid_prices(value):
    with pytest.raises(ValueError):
        settle_bar([0.5], value, 100, fee_bps=1, slippage_bps=1)


@pytest.mark.parametrize("change", [
    {"low": 0}, {"high": 1}, {"close": float("inf")},
    {"series_kind": "raw_futures"}, {"symbol": "KQ.i_SHFE.rb"},
])
def test_invalid_bar_cannot_enter_research(change):
    from ak_trading.research import ResearchConfig, run_research
    bars = sample_bars(210)
    bars[0] = replace(bars[0], **change)
    with pytest.raises(ValueError):
        run_research(bars, ResearchConfig(train_bars=200))


def test_minimum_samples_remain_unavailable_not_zero_edge():
    from ak_trading.research import ResearchConfig, fit_model, run_research
    bars = sample_bars(210)
    config = ResearchConfig(train_bars=200, min_samples=180, min_evidence_mass=1)
    model = fit_model(bars, config)
    assert any(cell["edge"] is None for t in model["tables"].values() for e in t.values() for cell in e.values())
    report = run_research(bars, config)
    assert all(report["summary"][mode]["turnover"] == 0 for mode in ("dynamic", "fixed"))


def test_smoothing_hysteresis_and_scale_invariance_are_causal():
    from ak_trading.research import ResearchConfig, evaluate_experts
    config = ResearchConfig(lookback=4)
    history = [100, 101, 100, 101, 103]
    a = evaluate_experts(history, config)
    scaled = evaluate_experts([v * 10 for v in history], config)
    assert a["signals"] == scaled["signals"]
    assert a["scores"] == pytest.approx(scaled["scores"])
    retained = evaluate_experts(history + [102], replace(config, hysteresis=1), a)
    assert retained["state"] == a["state"]
    unsmoothed = evaluate_experts(history + [102], replace(config, ema_alpha=1), a)
    assert retained["scores"] != unsmoothed["scores"]


def test_fixed_volatility_sizing_and_uncertainty_gate_without_edge_gate():
    from ak_trading.research import ResearchConfig, allocate
    config = ResearchConfig(min_samples=2, expert_cap=1, instrument_cap=1, cluster_cap=1, gross_cap=1, portfolio_vol_cap=1)
    cell = {"count": 30, "available": True, "mean": -0.01, "variance": 0.0001, "downside": 0.01, "edge": -0.02}
    model = {"tables": {"A": {e: {r: cell for r in ("trend", "range", "uncertain")} for e in ("breakout", "range")}}, "clusters": [["A"]]}
    state = {"reason": "ready", "signals": {"breakout": 1, "range": 0}, "scores": {"trend": 0.8, "range": 0.1, "uncertain": 0.1}, "state": "trend", "uncertainty": 0.3, "volatility": 0.01}
    low = allocate({"A": state}, model, config, mode="fixed")
    high = allocate({"A": {**state, "volatility": 0.02}}, model, config, mode="fixed")
    assert low["targets"]["A"] == pytest.approx(0.225)
    assert high["targets"]["A"] == pytest.approx(0.1125)
    assert allocate({"A": state}, model, config, mode="dynamic")["targets"]["A"] == 0
    for mode in ("dynamic", "fixed"):
        gated = allocate({"A": {**state, "uncertainty": 1.0}}, model, config, mode=mode)
        assert gated["targets"]["A"] == 0
        assert gated["budgets"]["A"]["breakout"]["reason"] == "uncertain"


def test_reserved_uncertain_score_never_becomes_learned_edge():
    from ak_trading.research import ResearchConfig, fit_model, mix_evidence, run_research
    config = ResearchConfig(train_bars=200, min_samples=2)
    bars = sample_bars(210)
    model = fit_model(bars, config)
    assert model["learnable_states"] == ["trend", "range"]
    assert model["reserved_cash_state"] == "uncertain"
    for experts in model["tables"].values():
        for table in experts.values():
            assert table["uncertain"] == {"count": 0, "available": False, "mean": None, "variance": None, "downside": None, "edge": None}
    # Even a malformed caller-supplied cash cell must not manufacture learnable edge.
    cell = {"available": True, "count": 50, "mean": 1.0, "variance": 0.1, "downside": 0, "edge": 1.0}
    evidence = mix_evidence({"trend": 0, "range": 0, "uncertain": 1}, dict.fromkeys(("trend", "range", "uncertain"), cell), config)
    assert evidence["mass"] == 0
    assert evidence["edge"] is None
    report = run_research(bars, config)
    assert report["state_semantics"]["uncertain"] == "reserved_cash_score_not_a_learned_market_regime"
    assert any("not realized-risk matched" in text for text in report["limitations"])
    for row in report["rows"]:
        for state in row["states"].values():
            if state["reason"] == "ready":
                assert state["state"] in ("trend", "range")
                assert state["scores"]["uncertain"] <= 1 / 3 + 1e-12


def test_signed_netting_and_two_sided_notional_costs():
    # +0.8 and -0.3 experts net to +0.5 entry notional. Units=0.005.
    result = settle_bar([0.8, -0.3], 100.0, 110.0, fee_bps=10, slippage_bps=20)
    assert result["exposure"] == pytest.approx(0.5)
    assert result["units"] == pytest.approx(0.005)
    assert result["gross_pnl"] == pytest.approx(0.05)
    assert result["turnover"] == pytest.approx(1.05)
    assert result["fees"] == pytest.approx(0.00105)
    assert result["slippage"] == pytest.approx(0.0021)
    assert result["net_pnl"] == pytest.approx(0.04685)
    short = settle_bar([-1.0], 100, 90, fee_bps=10, slippage_bps=0)
    assert short["gross_pnl"] == pytest.approx(0.1)
    assert short["fees"] == pytest.approx(0.0019)
    assert short["net_pnl"] == pytest.approx(0.0981)
    assert settle_bar([-1.0], 100, 110, fee_bps=0, slippage_bps=0)["net_pnl"] == pytest.approx(-0.1)
    assert settle_bar([0.5, -0.5], 100, 110, fee_bps=10, slippage_bps=10)["turnover"] == 0
