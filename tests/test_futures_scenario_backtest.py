"""Public, offline scenario replay oracles using the actual fixed strategies."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from ak_trading.futures.io import BUNDLE_SCHEMA_VERSION, InputBundle
from ak_trading.futures.spec import ContractBar
from ak_trading.futures.scenario_backtest import ScenarioConfig, run_scenario_backtest

D = Decimal
SYMBOL = "SHFE.rb2405"
EXPIRY = "2024-05-15T15:00:00+08:00"
INSTANCE = f"{SYMBOL}|{EXPIRY}"


def assumptions(**overrides):
    values = dict(initial_cash="100000", lots=2, multiplier="10", price_tick="0.5",
                  fee_per_lot="3", fee_rate="0.001", slippage_ticks="1",
                  margin_rate="0.1", max_margin_ratio="0.4")
    values.update(overrides)
    return values


def bars_from_prices(closes, opens=None):
    bars = []
    first = datetime(2023, 11, 1, tzinfo=timezone.utc)
    for index, raw in enumerate(closes):
        close = D(str(raw))
        opening = close if opens is None else D(str(opens[index]))
        ts = first + timedelta(days=index)
        bars.append(ContractBar(
            symbol=SYMBOL, duration_seconds=86400,
            datetime_nano=int(ts.timestamp()) * 1_000_000_000,
            ts=ts, source="generated_test_prices", open=opening, close=close,
            high=max(opening, close) + D("0.1"), low=min(opening, close) - D("0.1"),
            volume=D("1000"),
        ))
    return tuple(bars)


def bundle_for(bars, contracts=None):
    return InputBundle(
        root=Path("unused-in-memory"),
        manifest={"schema_version": BUNDLE_SCHEMA_VERSION, "dataset_kind": "real_export",
                  "status": "complete"},
        bars=tuple(bars),
        contracts=tuple(contracts) if contracts is not None else ({
            "instance_id": INSTANCE, "raw_symbol": SYMBOL, "product": "SHFE.RB",
            "expiry_at": EXPIRY, "identity_status": "reconstructed_static",
        },),
        metadata={}, availability={},
    )


def replay(bars, strategy="donchian_trend", **overrides):
    return run_scenario_backtest(bundle_for(bars), symbol=SYMBOL, strategy=strategy,
                                 config=ScenarioConfig.from_mapping(assumptions(**overrides)))


def test_completed_signal_executes_next_open_and_reconciles_costed_gain():
    # Forty lagged highs, then the first breakout at index 40. Buying at that
    # same close (140) would create a false gain: the next open is actually 150.
    closes = list(range(100, 141)) + [160, 165]
    opens = closes.copy()
    opens[41] = 150
    bars = bars_from_prices(closes, opens)
    result = replay(bars)
    opening, terminal = result.fills
    assert (opening.observation_index, opening.signal_index, opening.ideal_price) == (41, 40, D("150"))
    assert (terminal.observation_index, terminal.phase, terminal.ideal_price) == (42, "terminal_close", D("165"))
    assert (opening.effective_price, terminal.effective_price) == (D("150.5"), D("164.5"))
    assert (opening.fee, terminal.fee) == (D("9"), D("9.3"))
    assert opening.slippage == terminal.slippage == D("10")
    first_mark = result.equity[41]
    assert (first_mark.cash, first_mark.unrealized_pnl, first_mark.equity) == (D("99981"), D("200"), D("100181"))
    assert result.report["gross_pnl"] == "300"
    assert result.report["fees"] == "18.3"
    assert result.report["slippage"] == "20"
    assert result.report["net_pnl"] == "261.7"
    assert result.equity[-1].cash == result.equity[-1].equity == D("100261.7")
    assert result.equity[-1].position_lots == 0
    assert all(result.report["reconciliation"].values())
    assert all(d.decision_at is None and d.clock_status == "observation_only" for d in result.decisions)


def test_reversal_closes_then_opens_and_charges_both_legs():
    closes = list(range(100, 141)) + [10, 6]
    opens = closes.copy()
    opens[41:43] = [150, 8]
    result = replay(bars_from_prices(closes, opens))
    assert [(f.observation_index, f.offset, f.side, f.lots) for f in result.fills] == [
        (41, "open", 1, 2), (42, "close", -1, 2),
        (42, "open", -1, 2), (42, "close", 1, 2),
    ]
    assert result.report["gross_pnl"] == "-2800"
    assert result.report["fees"] == "27.44"
    assert result.report["slippage"] == "40"
    assert result.report["net_pnl"] == "-2867.44"
    assert result.report["turnover_lots"] == 8
    assert result.equity[-1].equity == D("97132.56")
    assert all(result.report["reconciliation"].values())


@pytest.mark.parametrize("direction", [1, -1])
def test_short_and_long_gain_loss_and_daily_drawdown_have_signed_pnl(direction):
    closes = [200 + direction * i for i in range(61)] + [200 + direction * 65, 200 + direction * 55]
    opens = closes.copy()
    opens[60] = 200 + direction * 62
    result = replay(bars_from_prices(closes, opens), "ma_trend", fee_per_lot="0", fee_rate="0", slippage_ticks="0")
    assert result.fills[0].side == direction
    assert result.equity[60].unrealized_pnl == D("-40")
    assert result.equity[61].unrealized_pnl == D("60")
    assert result.report["gross_pnl"] == "-140"
    assert result.report["net_pnl"] == "-140"
    assert result.report["max_drawdown"] == "200"
    assert D(result.report["total_return"]) == D("-0.0014")
    assert all(result.report["reconciliation"].values())


def test_future_suffix_and_permuted_input_do_not_change_prior_decisions_or_fills():
    original = bars_from_prices(list(range(100, 180)))
    changed = original[:65] + bars_from_prices(list(range(100, 165)) + [40] * 15)[65:]
    first, second = replay(original), replay(changed)
    assert first.decisions[:65] == second.decisions[:65]
    assert first.equity[:65] == second.equity[:65]
    assert [f for f in first.fills if f.observation_index < 65] == [f for f in second.fills if f.observation_index < 65]
    extended = replay(original + bars_from_prices(list(range(100, 181)))[80:])
    # Truncating the horizon intentionally moves its terminal close; everything
    # strictly before that precommitted terminal observation is invariant.
    assert first.decisions[:79] == extended.decisions[:79]
    assert first.equity[:79] == extended.equity[:79]
    assert replay(tuple(reversed(original))) == first


def test_only_previous_signal_controls_next_open_despite_same_bar_close_change():
    original = bars_from_prices(list(range(100, 141)) + [160, 165], list(range(100, 141)) + [150, 165])
    changed = list(original)
    changed[41] = replace(changed[41], close=D("10"), low=D("9"))
    assert replay(original).fills[0] == replay(changed).fills[0]


def test_margin_blocks_opens_but_does_not_erase_logical_signal():
    result = replay(bars_from_prices(list(range(100, 150))), initial_cash="100")
    assert result.fills == ()
    assert result.report["opening_blocks"] == 9
    assert all(row.equity == D("100") for row in result.equity)
    assert all(row.execution_status == "opening_margin_blocked" for row in result.decisions[41:])
    assert result.decisions[-1].logical_direction == 1
    assert result.report["status"] == "complete"


def test_margin_breach_does_not_invent_liquidation_and_losing_close_is_permitted():
    closes = list(range(100, 141)) + [10, 6]
    opens = closes.copy()
    opens[41:43] = [150, 8]
    result = replay(bars_from_prices(closes, opens), initial_cash="1000", lots=1,
                    fee_per_lot="0", fee_rate="0", slippage_ticks="0")
    assert [(f.observation_index, f.offset) for f in result.fills] == [(41, "open"), (42, "close")]
    assert result.equity[41].position_lots == 1
    assert result.equity[41].margin_breached is True
    assert result.equity[-1].cash == D("-420")
    assert result.decisions[42].execution_status == "opening_costs_exhaust_equity"
    assert result.report["opening_blocks"] == 1


def test_opening_admission_reserves_both_fees_and_slippage():
    bars = bars_from_prices(list(range(100, 143)))
    result = replay(bars, initial_cash="365", lots=1, fee_per_lot="10", fee_rate="0.01", slippage_ticks="5")
    # At 141, margin 141 > .4 * (365 - 10 - 14.1 - 25) =126.36.
    assert not result.fills
    assert result.decisions[41].opening_equity_after_costs == D("315.9")
    assert result.decisions[41].opening_required_margin == D("141")


@pytest.mark.parametrize("strategy", ["donchian_trend", "ma_trend", "zscore_reversion"])
def test_no_signal_has_header_ready_empty_fill_ledger_and_constant_equity(strategy):
    result = replay(bars_from_prices([100] * 90), strategy)
    assert result.fills == ()
    assert result.report["net_pnl"] == result.report["max_drawdown"] == "0"
    assert len(result.equity) == len(result.decisions) == 90
    assert all(row.cash == row.equity == D("100000") for row in result.equity)


def test_donchian_maintains_state_inside_channel_and_exits_at_exit_channel():
    closes = list(range(100, 145)) + [143, 144, 141, 142, 110, 109]
    result = replay(bars_from_prices(closes))
    assert [d.candidate_direction for d in result.decisions[40:49]] == [1] * 9
    assert result.decisions[49].candidate_direction == 0
    assert result.fills[1].observation_index == 50
    assert result.fills[1].phase == "next_open"


def test_zscore_preserves_direction_after_entry_and_uses_existing_fixed_age_exit():
    held_path = [
        "175", "191.986974", "173.445298", "189.782708", "169.541526",
        "187.218806", "165.131489", "184.266881", "160.240740", "180.906650",
        "154.929778", "177.130883", "149.309274", "172.952992", "143.564039",
        "168.419209", "137.993433", "163.629750", "133.087504", "158.781052",
        "129.698616", "154.322523", "123.883333", "149.540049", "118.004624",
    ]
    bars = bars_from_prices([200 + (-1) ** i * 10 for i in range(20)] + held_path)
    result = replay(bars, "zscore_reversion")
    assert [d.signal_age for d in result.decisions[20:41]] == list(range(21))
    assert all(d.candidate_direction == 1 for d in result.decisions[20:41])
    assert result.decisions[41].evaluator_candidate_direction == 1
    assert result.decisions[41].candidate_direction == 0
    assert result.decisions[41].reason == "max_signal_age_reached"
    assert result.fills[1].observation_index == 42


@pytest.mark.parametrize("name,value", [("open", "0"), ("close", "-1"), ("high", "NaN"),
                                          ("low", "Infinity"), ("close", None), ("high", "1")])
def test_invalid_interior_prices_reject_instead_of_dropping_observations(name, value):
    bars = list(bars_from_prices(list(range(100, 180))))
    bars[45] = replace(bars[45], **{name: D(value) if value is not None else None})
    with pytest.raises(ValueError, match="OHLC"):
        replay(bars)


def test_duplicate_timestamps_reject_and_nonfinite_volume_does_not_create_capacity():
    bars = bars_from_prices(list(range(100, 150)))
    with pytest.raises(ValueError, match="duplicate datetime_nano"):
        replay(bars + bars[20:21])
    changed = tuple(replace(bar, volume=D("sNaN"), open_oi=D("Infinity"), close_oi=D("-1")) for bar in bars)
    result = replay(changed)
    assert result.fills == replay(bars).fills
    assert result.report["unusable_nonpricing_fields"] == {"volume": 50, "open_oi": 50, "close_oi": 50}


def test_multiple_full_instances_for_same_raw_symbol_are_ambiguous():
    source = bundle_for(bars_from_prices(list(range(100, 150))))
    later = dict(source.contracts[0], expiry_at="2034-05-15T15:00:00+08:00", instance_id=f"{SYMBOL}|2034-05-15T15:00:00+08:00")
    with pytest.raises(ValueError, match="ambiguous"):
        run_scenario_backtest(replace(source, contracts=source.contracts + (later,)), symbol=SYMBOL,
                              strategy="donchian_trend", config=ScenarioConfig.from_mapping(assumptions()))


@pytest.mark.parametrize("symbol", ["KQ.m@SHFE.rb", "KQ.i@SHFE.rb", "SHFE.rb", "SHFE.rb2405C3000", "SHFE.rb2405,SHFE.rb2410", "SHFE.rb2413"])
def test_nonconcrete_or_option_symbols_reject(symbol):
    with pytest.raises(ValueError):
        run_scenario_backtest(bundle_for(bars_from_prices([100])), symbol=symbol,
                              strategy="donchian_trend", config=ScenarioConfig.from_mapping(assumptions()))


def test_carry_is_explicitly_unsupported():
    with pytest.raises(ValueError, match="carry requires far legs"):
        replay(bars_from_prices([100]), "term_structure_carry")


@pytest.mark.parametrize("changes", [
    {"lots": True}, {"lots": 1.0}, {"lots": 0}, {"lots": 1_000_001},
    {"initial_cash": "NaN"}, {"initial_cash": "0"}, {"multiplier": "-1"},
    {"price_tick": "0"}, {"fee_per_lot": "-1"}, {"fee_rate": "1.01"},
    {"margin_rate": "1.01"}, {"max_margin_ratio": "-0.1"},
    {"slippage_ticks": "Infinity"}, {"fee_rate": True}, {"fee_rate": "1e-999999"},
    {"unknown_assumption": 1},
])
def test_config_rejects_invalid_or_unknown_economic_assumptions(changes):
    with pytest.raises(ValueError):
        ScenarioConfig.from_mapping(assumptions(**changes))


def test_all_economic_keys_required_and_zero_costs_or_margin_are_explicit():
    for key in assumptions():
        missing = assumptions()
        del missing[key]
        with pytest.raises(ValueError, match="missing keys"):
            ScenarioConfig.from_mapping(missing)
    assert ScenarioConfig.from_mapping(assumptions(fee_rate="0", margin_rate="0", max_margin_ratio="0")).margin_rate == 0


def test_private_decimal_context_does_not_depend_on_callers_precision():
    bars = bars_from_prices(list(range(100, 150)))
    expected = replay(bars, fee_rate="0.000000123456", multiplier="123.123456789012")
    with localcontext() as context:
        context.prec = 6
        actual = replay(bars, fee_rate="0.000000123456", multiplier="123.123456789012")
    assert actual == expected


@pytest.mark.parametrize("field,value", [("open", 150.0), ("close", "150"), ("high", True)])
def test_public_api_requires_decimal_price_values_for_exact_accounting(field, value):
    bars = list(bars_from_prices(list(range(100, 180))))
    bars[50] = replace(bars[50], **{field: value})
    with pytest.raises(ValueError, match="OHLC"):
        replay(bars)


def test_too_large_adverse_slippage_rejects_impossible_effective_prices():
    with pytest.raises(ValueError, match="non-positive effective sell price"):
        replay(bars_from_prices(list(range(100, 150))), price_tick="100", slippage_ticks="2")
