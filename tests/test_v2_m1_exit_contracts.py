"""M1 exit-contract tests (08-M3C / 007 §5, M1-H10).

Contract-only P0 coverage: exact parameter whitelists, the 30-key
EXITSTATE registry and reserved namespaces, the OQ-4 binding hash formula
and vectors, OQ-6 ExitConfirmation/ExitState nesting, OQ-1 ContinuationEvidence,
ExitProposal/ExitExecutionLink structural rules, the checkpoint
exit collections and recovery fields, the incremental composite-identity
fields, and schema/version matrix assertions. All fixtures are SYNTHETIC.
"""
from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
from decimal import Decimal

import pytest

from ak_trading.v2 import CONTRACT_VERSION, DOCUMENT_VERSION, M1_NODE_VERSION
from ak_trading.v2.contracts import (
    EXITCTX_RESERVED_KEYS,
    EXITMKT_RESERVED_KEYS,
    EXIT_PARAMETER_WHITELISTS,
    EXIT_STATE_KEYS,
    ContinuationEvidence,
    DataQuality,
    ExitConfirmation,
    ExitExecutionLink,
    ExitFeatureSpec,
    ExitFeatureSnapshot,
    ExitFeatureValue,
    ExitLadderStep,
    ExitPolicyBinding,
    ExitProposal,
    ExitSpec,
    ExitState,
    exit_binding_content_hash,
    exit_observation_slot_id,
)
from ak_trading.v2.schemas import (
    CHECKPOINT_SCHEMA_ID,
    EXIT_BINDINGS_SCHEMA_ID,
    EXIT_STATES_SCHEMA_ID,
    METRICS_SCHEMA_ID,
    decode_record,
    encode_record,
)

H64 = "a" * 64
H64_B = "b" * 64
H64_C = "c" * 64
NANO = 1_767_920_700_000_000_000
DQ = DataQuality(status="READY", reasons=(), temporal_basis="observed",
                 input_hash=H64)


def _spec(**overrides) -> ExitSpec:
    values = dict(
        exit_policy_id="SYNTHETIC:exit_trailing_m1_v1",
        family="TRAILING_STOP",
        mode="MINUTE",
        version="1.0.0",
        parameter_version="SYNTHETIC:exit_trailing_m1_v1:params-1",
        code_sha256=H64,
        parameters={"arm_profit_r": "0.5", "trail_atr_multiple": "1",
                    "atr_basis": "FROZEN_ATR", "peak_source": "COMPLETED_CLOSE"},
        required_market_keys=("BASE:atr14:v2.1.0:d60", "BASE:close:v2.1.0:d60"),
        required_state_keys=("EXITSTATE:peak_favorable_price:v1.0.0:d60",
                             "EXITSTATE:trailing_atr_distance:v1.0.0:d60"),
        applicable_entry_families=("TREND",),
        observation_duration_seconds=60,
        execution_timing="NEXT_LEGAL_WINDOW",
        validation_stage="RESEARCH",
    )
    values.update(overrides)
    return ExitSpec(**values)


def _binding(**overrides) -> ExitPolicyBinding:
    values = dict(
        strategy_id="SYNTHETIC:entry-1",
        entry_spec_sha256=H64,
        exit_spec_sha256s=(H64_B, H64_C),
        sizing_spec_sha256=None,
        priority=("SYNTHETIC:legacy", "SYNTHETIC:trailing"),
        legacy_rules_version="1.0.0",
    )
    values.update(overrides)
    sha = exit_binding_content_hash(**values)
    return ExitPolicyBinding(binding_sha256=sha, **values)


def _state(**overrides) -> ExitState:
    values = dict(
        account_id="SYN-ACCOUNT-1",
        contract_id="SYN.rb2609",
        strategy_id="SYNTHETIC:entry-1",
        episode_id="SYN-EP-1",
        first_fill_nano=NANO,
        exit_binding_sha256=_binding().binding_sha256,
        realized_gross_cny=Decimal("0"),
        fees_paid_cny=Decimal("0"),
        price_peak=None,
        price_peak_source_nano=None,
        price_peak_source_hash=None,
        net_peak_r=None,
        net_peak_source_nano=None,
        net_peak_source_hash=None,
        armed=False,
        protection_stop_price=None,
        protection_line_version=None,
        protection_effective_from_nano=None,
        confirmations=(),
        no_more_add=False,
        qbase=None,
        closed_lots=0,
        ladder_progress=(),
        phase="MANAGING",
        last_committed_event_id=None,
        state_prefix_hash=H64,
    )
    values.update(overrides)
    return ExitState(**values)


def test_exit_parameter_whitelists_exact_key_sets() -> None:
    spec = _spec()
    assert set(spec.parameters) == EXIT_PARAMETER_WHITELISTS["TRAILING_STOP"]
    with pytest.raises(ValueError, match="exit_parameters_keys"):
        _spec(parameters={"arm_profit_r": "0.5"})                     # missing keys
    with pytest.raises(ValueError, match="exit_parameters_keys"):
        _spec(parameters={**spec.parameters, "confirmation_bars": 1})  # extra key
    with pytest.raises(ValueError, match="exit_parameter_atr_basis"):
        _spec(parameters={**spec.parameters, "atr_basis": "BASE_ATR14"})
    with pytest.raises(ValueError, match="exit_parameter_peak_source"):
        _spec(parameters={**spec.parameters, "peak_source": "INTRADAY"})
    with pytest.raises(ValueError, match="must_be_positive"):
        _spec(parameters={**spec.parameters, "arm_profit_r": "0"})
    ladder = {"profit_levels_r": ["0.5", "1.0", "1.5"],
              "cumulative_close_fractions": ["0.33", "0.67", "1"],
              "confirmation_bars": 1}
    registered = _spec(family="PROFIT_LADDER", parameters=ladder,
                       required_state_keys=("EXITSTATE:closed_fraction:v1.0.0:d60",),
                       exit_policy_id="SYNTHETIC:exit_profit_ladder_m1_v1")
    # defensive freeze stores lists as tuples; JSON decode yields lists
    assert dict(registered.parameters) == {
        "profit_levels_r": ("0.5", "1.0", "1.5"),
        "cumulative_close_fractions": ("0.33", "0.67", "1"),
        "confirmation_bars": 1}
    with pytest.raises(ValueError, match="strictly_increasing"):
        _spec(family="PROFIT_LADDER",
              parameters={**ladder, "profit_levels_r": ["1.0", "1.0", "1.5"]},
              required_state_keys=("EXITSTATE:closed_fraction:v1.0.0:d60",),
              exit_policy_id="SYNTHETIC:exit_profit_ladder_m1_v1")
    with pytest.raises(ValueError, match="last_must_be_one"):
        _spec(family="PROFIT_LADDER",
              parameters={**ladder, "cumulative_close_fractions": ["0.33", "0.67", "0.9"]},
              required_state_keys=("EXITSTATE:closed_fraction:v1.0.0:d60",),
              exit_policy_id="SYNTHETIC:exit_profit_ladder_m1_v1")
    with pytest.raises(ValueError, match="equal_length"):
        _spec(family="PROFIT_LADDER",
              parameters={**ladder, "profit_levels_r": ["0.5", "1.0"]},
              required_state_keys=("EXITSTATE:closed_fraction:v1.0.0:d60",),
              exit_policy_id="SYNTHETIC:exit_profit_ladder_m1_v1")
    with pytest.raises(ValueError, match="invalid_json_value_type"):
        _spec(family="PROFIT_LADDER",
              parameters={**ladder, "confirmation_bars": True},
              required_state_keys=("EXITSTATE:closed_fraction:v1.0.0:d60",),
              exit_policy_id="SYNTHETIC:exit_profit_ladder_m1_v1")
    with pytest.raises(ValueError, match="must_be_int_at_least_1"):
        _spec(family="PROFIT_LADDER",
              parameters={**ladder, "confirmation_bars": 0},
              required_state_keys=("EXITSTATE:closed_fraction:v1.0.0:d60",),
              exit_policy_id="SYNTHETIC:exit_profit_ladder_m1_v1")
    with pytest.raises(ValueError, match="invalid_json_value_type"):
        _spec(parameters={**_spec().parameters, "arm_profit_r": 0.5})  # float, not str
    with pytest.raises(ValueError, match="must_be_decimal_string"):
        _spec(parameters={**_spec().parameters, "arm_profit_r": "not-a-number"})
    decay = {"soft_time_fractions": ["0.75"], "minimum_progress_r": ["0.50"],
             "cumulative_close_fractions": ["1"]}
    _spec(family="TIME_DECAY", parameters=decay,
          required_state_keys=("EXITSTATE:elapsed_fraction:v1.0.0:d60",),
          exit_policy_id="SYNTHETIC:exit_time_decay_m1_v1")
    with pytest.raises(ValueError, match="within_0_1"):
        _spec(family="TIME_DECAY",
              parameters={**decay, "soft_time_fractions": ["1.5"]},
              required_state_keys=("EXITSTATE:elapsed_fraction:v1.0.0:d60",),
              exit_policy_id="SYNTHETIC:exit_time_decay_m1_v1")
    assert EXIT_PARAMETER_WHITELISTS["LEGACY"] == frozenset()
    with pytest.raises(ValueError, match="whitelist"):
        _spec(family="LEGACY", parameters={"x": "1"}, required_state_keys=(),
              required_market_keys=(), exit_policy_id="SYNTHETIC:exit_legacy_m1_v1")
    # mode/duration/key-suffix consistency and WAIT exclusion
    with pytest.raises(ValueError, match="observation_duration_seconds"):
        _spec(observation_duration_seconds=86400)
    with pytest.raises(ValueError, match="duration_suffix_mismatch"):
        _spec(required_market_keys=("BASE:close:v2.1.0:d86400",))
    with pytest.raises(ValueError, match="duration_suffix_mismatch"):
        _spec(required_state_keys=("EXITSTATE:peak_favorable_price:v1.0.0:d86400",
                                   "EXITSTATE:trailing_atr_distance:v1.0.0:d60"))
    with pytest.raises(ValueError, match="WAIT"):
        _spec(applicable_entry_families=("WAIT",))
    daily = _spec(mode="DAILY", observation_duration_seconds=86400,
                  required_market_keys=("BASE:close:v2.1.0:d86400",),
                  required_state_keys=("EXITSTATE:peak_favorable_price:v1.0.0:d86400",),
                  exit_policy_id="SYNTHETIC:exit_trailing_d1_v1")
    assert daily.observation_duration_seconds == 86400


def test_exit_binding_hash_vectors_and_rejections() -> None:
    # OQ-4 SYNTHETIC canonical vector (with trailing LF).
    got = exit_binding_content_hash(
        "SYNTHETIC:entry-1", "11" * 32, ("22" * 32, "33" * 32), None,
        ("SYNTHETIC:legacy", "SYNTHETIC:trailing"), "1.0.0")
    assert got == "be0cf5030962ee6ec30ba6036b41c0b870f2af3dd0aaf0260f716c3d17d54c61"
    payload = {
        "strategy_id": "SYNTHETIC:entry-1",
        "entry_spec_sha256": "11" * 32,
        "exit_spec_sha256s": sorted(["22" * 32, "33" * 32]),
        "sizing_spec_sha256": None,
        "priority": ["SYNTHETIC:legacy", "SYNTHETIC:trailing"],
        "legacy_rules_version": "1.0.0",
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False) + "\n"
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == got
    assert hashlib.sha256(canonical.rstrip("\n").encode("utf-8")).hexdigest() == \
        "bcdcd5cf465f8775769e0b8768f126381b81a86dbb4bf6eedeff07bcd6947338"
    # exit hash order-insensitive; priority order-sensitive; null key kept.
    assert exit_binding_content_hash("s", H64, (H64_B, H64_C), None, ("p1",), "1") == \
        exit_binding_content_hash("s", H64, (H64_C, H64_B), None, ("p1",), "1")
    assert exit_binding_content_hash("s", H64, (H64_B, H64_C), None, ("p1", "p2"), "1") != \
        exit_binding_content_hash("s", H64, (H64_B, H64_C), None, ("p2", "p1"), "1")
    assert exit_binding_content_hash("s", H64, (H64_B,), None, ("p",), "1") != \
        exit_binding_content_hash("s", H64, (H64_B,), H64_C, ("p",), "1")
    with pytest.raises(ValueError, match="duplicates"):
        exit_binding_content_hash("s", H64, (H64_B, H64_B), None, ("p",), "1")
    with pytest.raises(ValueError, match="duplicates"):
        exit_binding_content_hash("s", H64, (H64_B,), None, ("p", "p"), "1")
    with pytest.raises(ValueError):
        exit_binding_content_hash("", H64, (H64_B,), None, ("p",), "1")
    binding = _binding()
    assert binding.exit_spec_sha256s == (H64_B, H64_C)
    with pytest.raises(ValueError, match="content_hash"):
        ExitPolicyBinding(binding_sha256=H64, strategy_id="SYNTHETIC:entry-1",
                          entry_spec_sha256=H64, exit_spec_sha256s=(H64_B, H64_C),
                          sizing_spec_sha256=None,
                          priority=("SYNTHETIC:legacy", "SYNTHETIC:trailing"),
                          legacy_rules_version="1.0.0")
    with pytest.raises(ValueError, match="sorted"):
        _binding(exit_spec_sha256s=(H64_C, H64_B))
    encoded = decode_record(ExitPolicyBinding, encode_record(binding))
    assert encoded == binding


def test_exit_state_feature_types_and_keys() -> None:
    assert len(EXIT_STATE_KEYS) == 30
    assert len(set(EXIT_STATE_KEYS)) == 30
    assert not any("{d}" in key for key in EXIT_STATE_KEYS)
    assert "EXITSTATE:age_trading_minutes:v1.0.0:d60" in EXIT_STATE_KEYS
    assert "EXITSTATE:age_trading_days:v1.0.0:d86400" in EXIT_STATE_KEYS
    assert "EXITSTATE:age_trading_minutes:v1.0.0:d86400" not in EXIT_STATE_KEYS
    assert len(EXITCTX_RESERVED_KEYS) == 6 and len(EXITMKT_RESERVED_KEYS) == 6
    feature_spec = ExitFeatureSpec(
        feature_key="EXITSTATE:trailing_atr_distance:v1.0.0:d60",
        formula_version="v1.0.0", unit="ATR", window_kind="EPISODE_PREFIX",
        minimum_source_facts=1, source_scope="EPISODE", clock_unit="NONE",
        required_source_keys=("BASE:atr14:v2.1.0:d60",))
    assert feature_spec.minimum_source_facts == 1
    with pytest.raises(ValueError, match="reserved_namespace"):
        ExitFeatureSpec(feature_key=EXITCTX_RESERVED_KEYS[0], formula_version="v1.0.0",
                        unit="cny", window_kind="CURRENT_STATE", minimum_source_facts=1,
                        source_scope="EPISODE", clock_unit="NONE",
                        required_source_keys=())
    with pytest.raises(ValueError, match="version"):
        ExitFeatureSpec(feature_key=feature_spec.feature_key, formula_version="v0.9.0",
                        unit="ATR", window_kind="EPISODE_PREFIX", minimum_source_facts=1,
                        source_scope="EPISODE", clock_unit="NONE",
                        required_source_keys=())
    with pytest.raises(ValueError, match="at_least_1"):
        ExitFeatureSpec(feature_key=feature_spec.feature_key, formula_version="v1.0.0",
                        unit="ATR", window_kind="EPISODE_PREFIX", minimum_source_facts=0,
                        source_scope="EPISODE", clock_unit="NONE",
                        required_source_keys=())
    value = ExitFeatureValue(feature_key=feature_spec.feature_key, value=Decimal("1.5"),
                             observed_at_nano=NANO, available_at_nano=NANO + 1,
                             quality=DQ, source_hashes={"bar": H64})
    with pytest.raises(ValueError, match="quality_reasons"):
        ExitFeatureValue(feature_key=feature_spec.feature_key, value=None,
                         observed_at_nano=NANO, available_at_nano=NANO,
                         quality=DQ, source_hashes={})
    snapshot = ExitFeatureSnapshot(
        snapshot_id="SYN-SNAP-1", account_id="SYN-ACCOUNT-1", contract_id="SYN.rb2609",
        strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
        exit_binding_sha256=_binding().binding_sha256, duration_seconds=60,
        observed_at_nano=NANO, available_at_nano=NANO + 1, values=(value,),
        source_state_sha256=H64)
    assert snapshot.values[0].feature_key.endswith(":d60")
    with pytest.raises(ValueError, match="duration_suffix_mismatch"):
        ExitFeatureSnapshot(snapshot_id="SYN-SNAP-1", account_id="SYN-ACCOUNT-1",
                            contract_id="SYN.rb2609", strategy_id="SYNTHETIC:entry-1",
                            episode_id="SYN-EP-1",
                            exit_binding_sha256=_binding().binding_sha256,
                            duration_seconds=86400, observed_at_nano=NANO,
                            available_at_nano=NANO + 1, values=(value,),
                            source_state_sha256=H64)
    with pytest.raises(ValueError, match="sorted_unique"):
        ExitFeatureSnapshot(snapshot_id="SYN-SNAP-1", account_id="SYN-ACCOUNT-1",
                            contract_id="SYN.rb2609", strategy_id="SYNTHETIC:entry-1",
                            episode_id="SYN-EP-1",
                            exit_binding_sha256=_binding().binding_sha256,
                            duration_seconds=60, observed_at_nano=NANO,
                            available_at_nano=NANO + 1, values=(value, value),
                            source_state_sha256=H64)
    with pytest.raises(ValueError, match="exceed_snapshot"):
        ExitFeatureSnapshot(snapshot_id="SYN-SNAP-1", account_id="SYN-ACCOUNT-1",
                            contract_id="SYN.rb2609", strategy_id="SYNTHETIC:entry-1",
                            episode_id="SYN-EP-1",
                            exit_binding_sha256=_binding().binding_sha256,
                            duration_seconds=60, observed_at_nano=NANO,
                            available_at_nano=NANO,
                            values=(ExitFeatureValue(
                                feature_key=feature_spec.feature_key, value=Decimal("1"),
                                observed_at_nano=NANO, available_at_nano=NANO + 1,
                                quality=DQ, source_hashes={}),),
                            source_state_sha256=H64)
    with pytest.raises(ValueError, match="precede"):
        ExitFeatureSnapshot(snapshot_id="SYN-SNAP-1", account_id="SYN-ACCOUNT-1",
                            contract_id="SYN.rb2609", strategy_id="SYNTHETIC:entry-1",
                            episode_id="SYN-EP-1",
                            exit_binding_sha256=_binding().binding_sha256,
                            duration_seconds=60, observed_at_nano=NANO + 1,
                            available_at_nano=NANO, values=(), source_state_sha256=H64)
    # OQ-6: ExitConfirmation nesting, ordering, dedup, observation slots.
    slot = exit_observation_slot_id("SYN.rb2609", 60, NANO + 60_000_000_000)
    state = _state(confirmations=(
        ExitConfirmation(exit_policy_id="SYNTHETIC:exit_signal_reversal_m1_v1",
                         rule_id="trigger", count=2, last_observation_id=slot),))
    assert state.confirmations[0].count == 2
    with pytest.raises(ValueError, match="sorted_unique"):
        _state(confirmations=(
            ExitConfirmation(exit_policy_id="p", rule_id="a", count=0,
                             last_observation_id=None),
            ExitConfirmation(exit_policy_id="p", rule_id="a", count=1,
                             last_observation_id=slot)))
    with pytest.raises(ValueError, match="requires_last_observation_id"):
        ExitConfirmation(exit_policy_id="p", rule_id="trigger", count=1,
                         last_observation_id=None)
    with pytest.raises(ValueError, match="non_negative"):
        ExitConfirmation(exit_policy_id="p", rule_id="trigger", count=-1,
                         last_observation_id=None)
    with pytest.raises(ValueError, match="must_be_canonical"):
        _state(confirmations=(
            ExitConfirmation(exit_policy_id="p", rule_id="trigger", count=1,
                             last_observation_id='["SYN.rb2609", 60, 1] '),))
    with pytest.raises(ValueError, match="match_parent_state"):
        _state(confirmations=(
            ExitConfirmation(exit_policy_id="p", rule_id="trigger", count=1,
                             last_observation_id=exit_observation_slot_id(
                                 "SYN.other", 60, NANO)),))
    with pytest.raises(ValueError, match="duration_must_be"):
        _state(confirmations=(
            ExitConfirmation(exit_policy_id="p", rule_id="trigger", count=1,
                             last_observation_id='["SYN.rb2609",300,%d]' % NANO),))
    # ExitState nullable combinations.
    with pytest.raises(ValueError, match="armed_requires"):
        _state(armed=True)
    with pytest.raises(ValueError, match="unarmed_requires"):
        _state(protection_stop_price=Decimal("100"))
    with pytest.raises(ValueError, match="price_peak_none_requires"):
        _state(price_peak_source_nano=NANO)
    with pytest.raises(ValueError, match="qbase_none_requires"):
        _state(closed_lots=1)
    with pytest.raises(ValueError, match="closed_lots_must_not_exceed"):
        _state(qbase=2, closed_lots=3)
    with pytest.raises(ValueError, match="closed_phase_requires"):
        _state(phase="CLOSED")
    with pytest.raises(ValueError, match="not_precede_first_fill"):
        _state(price_peak=Decimal("101"), price_peak_source_nano=NANO - 1,
               price_peak_source_hash=H64)
    assert _state(armed=True, protection_stop_price=Decimal("98"),
                  protection_line_version="line-1",
                  protection_effective_from_nano=NANO).armed is True
    ladder = _state(qbase=7, closed_lots=2, ladder_progress=(
        ExitLadderStep(level_index=0, triggered=True, requested_lots=2,
                       completed_lots=2),))
    assert ladder.ladder_progress[0].completed_lots == 2
    with pytest.raises(ValueError, match="completed_lots_must_not_exceed"):
        ExitLadderStep(level_index=0, triggered=True, requested_lots=1,
                       completed_lots=2)
    # OQ-1: ContinuationEvidence structure (context gate covered by the
    # P0 rule: ExitContext.continuation must be None until P4).
    evidence = ContinuationEvidence(evidence_sha256=H64, version="1.0.0",
                                    kind="P4_RESERVED")
    assert evidence.version == "1.0.0"
    with pytest.raises(ValueError):
        ContinuationEvidence(evidence_sha256=H64, version="2.0.0", kind="P4_RESERVED")
    with pytest.raises(ValueError):
        ContinuationEvidence(evidence_sha256="zz", version="1.0.0", kind="P4_RESERVED")
    # ExitProposal / ExitExecutionLink structure (OQ-3 embedding).
    proposal = ExitProposal(
        proposal_id="SYN-EXIT-P1", strategy_id="SYNTHETIC:entry-1", episode_id="SYN-EP-1",
        contract_id="SYN.rb2609", exit_binding_sha256=_binding().binding_sha256,
        action="EXIT", target_lots=0, triggering_policy_id="SYNTHETIC:exit_trailing_m1_v1",
        input_sha256=H64, observed_at_nano=NANO, earliest_execution_nano=NANO + 1,
        protection=None, next_state=_state(), reasons=("trailing_stop_touched",))
    assert proposal.next_state.episode_id == proposal.episode_id
    with pytest.raises(ValueError, match="zero_target_lots"):
        dataclasses.replace(proposal, target_lots=3)
    with pytest.raises(ValueError, match="precede_observation"):
        dataclasses.replace(proposal, earliest_execution_nano=NANO - 1)
    with pytest.raises(ValueError, match="scope_must_match"):
        dataclasses.replace(proposal, next_state=_state(episode_id="SYN-EP-2"))
    link = ExitExecutionLink(
        link_id="SYN-LINK-1", exit_proposal_id="SYN-EXIT-P1",
        strategy_proposal_id="SYN-PROP-1", decision_id="SYN-DEC-1",
        intent_id="SYN-INT-1", exit_binding_sha256=_binding().binding_sha256,
        action="EXIT", target_lots=0, requested_lots=1,
        execution_kind="NEXT_WINDOW_CLOSE")
    assert link.requested_lots == 1
    with pytest.raises(ValueError, match="reduce_or_exit"):
        dataclasses.replace(link, action="KEEP")
    with pytest.raises(ValueError, match="requested_lots"):
        dataclasses.replace(link, requested_lots=0)


def test_checkpoint_exit_collections_and_recovery_fields() -> None:
    from ak_trading.v2.contracts import LedgerCheckpoint, PayoffEstimate, RunPolicy

    field_names = [f.name for f in dataclasses.fields(LedgerCheckpoint)]
    for required in ("exit_registry_sha256", "exit_bindings", "exit_states",
                     "exit_execution_links"):
        assert required in field_names
    assert field_names.index("exit_registry_sha256") == \
        field_names.index("registry_sha256") + 1
    assert field_names.index("exit_states") == field_names.index("states") + 2
    assert CHECKPOINT_SCHEMA_ID == "ak_probability_checkpoints_v2.1.3"
    assert EXIT_STATES_SCHEMA_ID == "ak_probability_exit_states_v1.0.0"
    assert EXIT_BINDINGS_SCHEMA_ID == "ak_probability_exit_bindings_v1.0.0"
    assert METRICS_SCHEMA_ID == "ak_probability_metrics_v2.1.4"
    assert CONTRACT_VERSION == "ak_probability_strategy_contract_v2.1.3"
    assert DOCUMENT_VERSION == "devplan-v2.1.7"
    assert M1_NODE_VERSION == "M1-v2.1.3"
    # Composite identity fields (007 §5.3): required, no defaults.
    payoff_fields = [f.name for f in dataclasses.fields(PayoffEstimate)]
    assert "exit_binding_sha256" in payoff_fields
    assert payoff_fields.index("exit_state_condition") == \
        payoff_fields.index("opportunity_set_id") + 2
    runpolicy_fields = [f.name for f in dataclasses.fields(RunPolicy)]
    assert runpolicy_fields.index("fixed_exit_binding_sha256") == \
        runpolicy_fields.index("fixed_strategy_id") + 1
    with pytest.raises(TypeError):
        RunPolicy(purpose="RESEARCH_EVALUATION", routing_mode="FIXED", seed=7,
                  fixed_strategy_id="SYNTHETIC:entry-1",
                  research_policy_version="research-policy-v2.1.0",
                  macro_policy_version="macro-risk-v2.1.0", macro_mode="PRICE_ONLY",
                  cost_scenario="BASE")  # missing fixed_exit_binding_sha256
    with pytest.raises(ValueError, match="FIXED_mode_requires"):
        RunPolicy(purpose="RESEARCH_EVALUATION", routing_mode="FIXED", seed=7,
                  fixed_strategy_id="SYNTHETIC:entry-1",
                  fixed_exit_binding_sha256=None,
                  research_policy_version="research-policy-v2.1.0",
                  macro_policy_version="macro-risk-v2.1.0", macro_mode="PRICE_ONLY",
                  cost_scenario="BASE")
    with pytest.raises(ValueError, match="only_for_FIXED"):
        RunPolicy(purpose="RESEARCH_EVALUATION", routing_mode="BEST_EV", seed=7,
                  fixed_strategy_id=None, fixed_exit_binding_sha256=H64,
                  research_policy_version="research-policy-v2.1.0",
                  macro_policy_version="macro-risk-v2.1.0", macro_mode="PRICE_ONLY",
                  cost_scenario="BASE")
    # verify_qualification_evidence keyword-only gate (007 §5.4).
    from ak_trading.v2.artifacts import verify_qualification_evidence
    signature = inspect.signature(verify_qualification_evidence)
    assert signature.parameters["exit_registry_sha256"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["exit_binding"].kind is inspect.Parameter.KEYWORD_ONLY
    # 8-condition no-smuggling boundary: frozen axes stay untouched.
    from ak_trading.v2.contracts import STRATEGY_FAMILIES
    from ak_trading.v2.schemas import (FACTOR_REGISTRY_SCHEMA_ID,
                                       INPUT_COLLECTION_SCHEMA_ID,
                                       MANIFEST_SCHEMA_ID,
                                       SEASONALITY_SCHEMA_ID,
                                       SHARED_FIXTURE_SCHEMA_ID)
    assert STRATEGY_FAMILIES == frozenset(
        {"TREND", "BREAKOUT", "PULLBACK", "REVERSION", "GRID", "MARTINGALE", "WAIT"})
    assert MANIFEST_SCHEMA_ID == "ak_probability_input_v2.1.0"
    assert SHARED_FIXTURE_SCHEMA_ID == "ak_probability_shared_fixture_v2.1.0"
    assert SEASONALITY_SCHEMA_ID == "ak_probability_seasonality_v2.1.1"
    assert FACTOR_REGISTRY_SCHEMA_ID == "ak_probability_factor_registry_v2.1.1"
    assert INPUT_COLLECTION_SCHEMA_ID == "ak_probability_input_collection_v2.1.0"
    spec = _spec()
    assert "exit_policy_id" not in [
        f.name for f in dataclasses.fields(
            __import__("ak_trading.v2.contracts", fromlist=["StrategySpec"]).StrategySpec)]
