from ak_trading.probability.contracts import ActionSpec, PathScenario


def test_distinct_barrier_parameters_keep_distinct_model_identities():
    a = PathScenario(5, 1.0000001, 1.0000001)
    b = PathScenario(5, 1.0000002, 1.0000002)
    assert a.scenario_id != b.scenario_id
    assert PathScenario(5, 1.0, 1.0).scenario_id == "h5-u1-d1"


def test_distinct_grid_parameters_keep_distinct_strategy_identities():
    a = ActionSpec("grid", grid_step_atr=0.50000001)
    b = ActionSpec("grid", grid_step_atr=0.50000002)
    assert a.action_id != b.action_id
