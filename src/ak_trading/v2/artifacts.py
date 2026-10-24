"""Hashing, atomic bundle publication and the contract lock (M1 §7).

Publication writes payload files into a brand-new directory and commits
``manifest.json`` last via an atomic replace, so a bundle either has a
complete manifest or none at all. ``publish_bundle`` accepts the strict
input-export provenance shape and the unified M2-M5 run-manifest shape;
``contract_version`` and ``files`` are always derived, never caller-filled.
The contract lock binds public file bytes, canonical type/function
signatures, schema descriptors, fixtures, ownership, tests, check ids and
the frozen research policy. Building the lock here does not publish it:
publication happens only after the independent hard review passes. The
T7 acceptance input manifest F (v2-ep1-t7-form-20260917-r1 §5.1/§5.3)
joins FIXTURE_FILES: ``fixture_files[F]`` binds H_F (the manifest's
UTF-8/LF-normalized bytes) and the attachment table is verified
recursively — per-file existence/exact bytes, exact sets, unlisted or
missing entries, path escapes and strict schema — so the lock never
binds a bare unchecked index, and F stays lock-independent (no final
lock hash, no future report hash, no post-run readiness, at any depth).
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

from ak_trading.v2 import (
    CONTRACT_LOCK_SCHEMA_VERSION,
    CONTRACT_VERSION,
    DOCUMENT_VERSION,
    INPUT_SCHEMA_VERSION,
)
from ak_trading.v2.schemas import (
    RECORD_TYPES,
    SchemaError,
    canonical_json_bytes,
    canonical_schema_descriptor,
    canonical_type_signature,
    parse_json_bytes,
    type_to_str,
)
from ak_trading.v2.contracts import (
    ExitPolicyBinding,
    JsonValue,
    StrategyQualification,
    StrategySpec,
)
from ak_trading.v2.policy import research_policy

PUBLIC_CODE_FILES: tuple[str, ...] = (
    "src/ak_trading/v2/__init__.py",
    "src/ak_trading/v2/contracts.py",
    "src/ak_trading/v2/schemas.py",
    "src/ak_trading/v2/clock.py",
    "src/ak_trading/v2/data.py",
    "src/ak_trading/v2/export.py",
    "src/ak_trading/v2/macro.py",
    "src/ak_trading/v2/artifacts.py",
    "src/ak_trading/v2/policy.py",
)

DOWNSTREAM_TEST_FILES: tuple[str, ...] = (
    "tests/test_v2_m2_seasonality.py",
    "tests/test_v2_m2_causality.py",
    "tests/test_v2_m3a_models.py",
    "tests/test_v2_m3a_calibration.py",
    "tests/test_v2_m3a_forecast.py",
    "tests/test_v2_m3b_state.py",
    "tests/test_v2_m3b_causality.py",
    # 08-M3C §3 / OQ-2: the six P1-stage M3C test files enter the lock at
    # P0 as contract-level assertions (owner M3C; P1 behavior tests are
    # frozen separately and never back-filled as a "correction").
    "tests/test_v2_m3c_registry.py",
    "tests/test_v2_m3c_features.py",
    "tests/test_v2_m3c_state.py",
    "tests/test_v2_m3c_trailing.py",
    "tests/test_v2_m3c_time_decay.py",
    "tests/test_v2_m3c_causality.py",
    # 08-M3C §3 P2/P3 stage files (unlocked at their stage gates; the
    # opportunity-cost P4 file enters this list only at P4).
    "tests/test_v2_m3c_ladder.py",
    "tests/test_v2_m3c_sizing.py",
    "tests/test_v2_m3c_volatility_stop.py",
    "tests/test_v2_m3c_drawdown.py",
    "tests/test_v2_m3c_signal.py",
    "tests/test_v2_m4_payoffs.py",
    "tests/test_v2_m4_gates.py",
    "tests/test_v2_m4_replay.py",
    # T7 form arbitration §5.3 item 1 (v2-ep1-t7-form-20260917-r1): the
    # payoff-path measurement test files join every future lock's
    # required_tests alongside all existing entries (nothing dropped). Until
    # the M5 fixture work order lands tests/test_v2_m5_t7_evidence.py a lock
    # build against the live tree honestly fails with
    # required_test_file_missing — no lock is published from this state.
    "tests/test_v2_m4_payoff_paths.py",
    "tests/test_v2_m5_experiments.py",
    "tests/test_v2_m5_payoff_integration.py",
    "tests/test_v2_m5_t7_evidence.py",
    "tests/test_v2_m5_shadow.py",
    "tests/test_v2_m5_reports.py",
)

PUBLIC_TEST_FILES: tuple[str, ...] = (
    "tests/test_v2_m1_contracts.py",
    "tests/test_v2_m1_clock.py",
    "tests/test_v2_m1_data.py",
    "tests/test_v2_m1_export.py",
    "tests/test_v2_m1_macro.py",
    "tests/test_v2_m1_artifacts.py",
    "tests/test_v2_m1_policy.py",
    "tests/test_v2_m1_exit_contracts.py",
    "tests/test_v2_m1_payoff_path_contracts.py",
)

# ─── T7 acceptance input manifest F (v2-ep1-t7-form-20260917-r1 §5.1/§5.3) ───
# F is the lock-independent acceptance input list. Its content is owned by
# the M5 fixture work order (§7.1); M1 owns the lock-side reference and the
# mechanically checkable schema below. F is frozen BEFORE any candidate lock
# exists, so until it lands on disk every lock build fails honestly with
# acceptance_input_manifest_not_frozen (NOT_FROZEN, never pre-filled hashes).
ACCEPTANCE_INPUT_MANIFEST_SCHEMA_ID = "ak_probability_acceptance_input_manifest_v1"
ACCEPTANCE_INPUT_MANIFEST_PATH = "tests/fixtures/v2/ep1_t7/acceptance-input-manifest.json"
# §5.3 item 3: F's behavioral-source floor covers everything code_hashes()
# binds today (validation/experiments.py: all src/ak_trading/v2 *.py plus the
# research runner), plus the offline test runner; add/remove on either side
# is drift.
ACCEPTANCE_SOURCE_ROOT = "src/ak_trading/v2"
ACCEPTANCE_RUNNER_PATH = "scripts/research_probability_v2.py"
ACCEPTANCE_OFFLINE_RUNNER_PATH = "scripts/test_offline.py"

FIXTURE_FILES: tuple[str, ...] = (
    "tests/fixtures/v2/shared.json",
    # §5.3 item 2: F joins FIXTURE_FILES so fixture_files[F] = H_F, with the
    # whole attachment table recursively bound and verified — never a bare
    # unchecked index.
    ACCEPTANCE_INPUT_MANIFEST_PATH,
)

_ACCEPTANCE_MANIFEST_KEYS = frozenset({
    "schema_version", "dataset_kind", "labels", "generation", "inputs",
    "splits", "selection", "bindings", "files",
})
_ACCEPTANCE_GENERATION_KEYS = frozenset({"recipe", "seed"})
_ACCEPTANCE_INPUT_SECTION_KEYS = frozenset({
    "minute_bars", "daily_bars", "calendar", "rules", "costs",
})
_ACCEPTANCE_ATTACHMENT_KEYS = frozenset({"path", "bytes", "sha256"})
_ACCEPTANCE_LABELS = frozenset({"SYNTHETIC", "SCENARIO"})
# §5.1 non-circularity: F never carries the final lock hash, a future
# reviewer report hash, an evidence-package hash or post-run readiness —
# at any depth, under any of these key names.
_ACCEPTANCE_FORBIDDEN_KEY_NAMES = frozenset({
    "contract_lock_sha256", "final_lock_sha256", "lock_sha256",
    "reviewer_report_sha256", "evidence_manifest_sha256", "readiness",
})
# §5.1/§5.4: the attachment table binds inputs only (source, tests, helpers,
# recipe, config, frozen rule documents). Published run outputs — locks,
# sources, run bundles — are future lock-dependent bytes; listing one would
# express a lock→output→lock cycle and is rejected before existence checks.
_ACCEPTANCE_FORBIDDEN_ATTACHMENT_PREFIXES = ("data/research_runs/",)
_ACCEPTANCE_FIXTURE_DIR = "tests/fixtures/v2/ep1_t7"

DATASET_KINDS = frozenset({"HISTORICAL_RECONSTRUCTED", "HISTORICAL_PIT", "SYNTHETIC", "FORWARD_SHADOW"})
_INPUT_MANIFEST_KEYS = frozenset({
    "schema_version", "contract_version", "dataset_kind", "created_at_nano", "request",
    "read_only_transaction", "source_hashes", "limitations",
})
_RUN_CONTROL_KEYS = frozenset({"schema_version", "created_at_nano", "provenance"})
_RUN_PROVENANCE_KEYS = frozenset({
    "node", "dataset_kind", "input_hashes", "code_hashes", "parameter_hashes",
    "model_hashes", "policy_versions", "seed", "limitations",
    "previous_segment_manifest_sha256", "recovery_source_sha256", "ignored_tail_sha256",
})
_LOCK_KEYS = frozenset({
    "schema_version", "contract_version", "document_version", "public_files",
    "public_signatures", "schemas", "fixture_files", "ownership", "required_tests",
    "required_check_ids", "policy", "reviewed_revision", "reviewer_report_sha256",
})
_REVIEW_CHECK_KEYS = (
    "scope_registered", "groups_complete", "metrics_recomputed", "thresholds_unchanged",
    "holdout_once", "causality_passed", "source_hashes_match",
)
_METRICS_ROOT_KEYS = frozenset({
    "schema_version", "research_policy_version", "macro_policy_version", "cost_scenarios",
    "required_group_keys", "model_bundles", "forecast_groups", "strategy_groups",
    "replay_summaries", "exclusions", "bootstrap",
    # 007 §8.1: exit identity, full bindings by hash and exit groups.
    "exit_registry_sha256", "exit_bindings", "exit_groups",
})
_REPLAY_SUMMARY_KEYS = frozenset({
    "account_id", "initial_equity_cny", "terminal_equity_cny",
    "closed_episode_net_pnl_cny", "open_episode_realized_net_pnl_cny",
    "terminal_unrealized_cny", "open_episode_ids", "pending_intent_ids",
    "reconciliation_status", "status", "reasons", "split_id", "run_policy_sha256",
    "model_bundle_sha256",
})
_REPLAY_SUMMARY_STATUSES = frozenset({"COMPLETE", "INCOMPLETE", "FAILED"})
_REVIEW_ROOT_KEYS = frozenset({
    "schema_version", "scope", "verdict", "independent", "reviewer_id", "report_sha256",
    "contract_lock_sha256", "registry_sha256", "experiment_sha256", "model_hashes",
    "research_policy_version", "macro_policy_version", "cost_scenarios", "source_hashes",
    "required_group_keys", "checks", "reasons",
    # 007 §8.1: the review binds the exit registry and every binding hash.
    "exit_registry_sha256", "exit_binding_sha256s",
})
_EXPERIMENT_SCHEMA_ID = "ak_probability_experiment_v2.1.1"
_EXPERIMENT_SPEC_KEYS = frozenset({
    "experiment_id", "run_id", "input_manifest_hashes", "contract_lock_sha256",
    "source_sha256", "parameter_sha256", "split_specs", "holdout_id", "holdout_scope",
    "run_policies", "required_group_keys", "seed", "research_policy_version",
    "macro_policy_version", "macro_mode", "evaluation_as_of_nano",
    # 007 §8.1: exit registry + full binding objects.
    "exit_registry_sha256", "exit_bindings",
})
_FORECAST_GROUP_KEYS = frozenset({
    "group_key", "split_id", "model_bundle_sha256", "product", "contract_id", "mode",
    "horizon_unit", "horizon_value", "session", "year", "regime", "event", "policy_id",
    "barrier_key", "geometry_key", "bucket", "calibration_error", "n", "blocks", "brier",
    "log_loss", "ece", "pinball", "coverage", "ci_lower", "ci_upper", "status",
})
_STRATEGY_GROUP_KEYS = frozenset({
    "group_key", "split_id", "model_bundle_sha256", "run_policy_sha256", "routing_mode",
    "fixed_strategy_id", "account_id", "strategy_id", "product", "contract_id", "mode",
    "horizon_unit", "horizon_value", "macro_mode", "cost_scenario", "episodes", "blocks",
    "admission_rate", "no_trade_rate", "net_pnl_cny", "conservative_ev_cny",
    "max_drawdown_ratio", "cvar95_cny", "maximum_stress_loss_ratio", "margin_shortfall_cny",
    "peak_margin_cny", "max_inventory", "longest_recovery_days", "status", "reasons",
    # 007 §8.1: composite entry×exit identity per strategy group.
    "exit_binding_sha256", "exit_spec_sha256s", "fixed_exit_binding_sha256",
})
_SIX_COST_SCENARIOS = ("BASE", "FEE_X2", "SLIPPAGE_X2", "MARGIN_X1_5", "GAP", "NO_STOP")
_METRICS_SCHEMA_ID = "ak_probability_metrics_v2.1.4"
_REVIEW_SCHEMA_ID = "ak_probability_qualification_review_v2.1.1"
_HEX = frozenset("0123456789abcdef")

# required_check_ids per 07 §R1: every node's full H list plus its applicable
# domain items. Values are check IDs, not test paths; the fixed inventory is
# verified verbatim by build/verify so no node's clause set can drift.
NODE_H_ITEMS: dict[str, tuple[str, ...]] = {
    "M1": tuple(f"M1-H{index:02d}" for index in range(1, 11)),
    "M2": tuple(f"M2-H{index:02d}" for index in range(1, 9)),
    "M3A": tuple(f"M3A-H{index:02d}" for index in range(1, 11)),
    "M3B": tuple(f"M3B-H{index:02d}" for index in range(1, 11)),
    "M3C": tuple(f"M3C-H{index:02d}" for index in range(1, 17)),
    "M4": tuple(f"M4-H{index:02d}" for index in range(1, 13)),
    "M5": tuple(f"M5-H{index:02d}" for index in range(1, 13)),
}
NODE_DOMAIN_ITEMS: dict[str, tuple[str, ...]] = {
    "M1": ("D01", "D02", "D07"),
    "M2": ("D02",),
    "M3A": ("D01", "D02", "D03", "D04"),
    "M3B": ("D01", "D02", "D05", "D06"),
    # 007 §9.2: M3C = D01,D02,D04,D05,D06,D08,D09,D10.
    "M3C": ("D01", "D02", "D04", "D05", "D06", "D08", "D09", "D10"),
    "M4": ("D01", "D02", "D04", "D05", "D06", "D07", "D08", "D09"),
    "M5": tuple(f"D{index:02d}" for index in range(1, 12)),
}


def required_check_ids_map() -> dict[str, list[str]]:
    """node -> its complete H clause list followed by its applicable D items."""
    return {
        node: [*NODE_H_ITEMS[node], *NODE_DOMAIN_ITEMS[node]]
        for node in ("M1", "M2", "M3A", "M3B", "M3C", "M4", "M5")
    }


# M1-internal evidence binding (not a lock field): which M1 tests evidence
# each M1 H clause. Kept so the lock builder can sanity-check that every M1
# clause has real, existing test coverage in this repository.
M1_CHECK_TEST_EVIDENCE: dict[str, tuple[str, ...]] = {
    "M1-H01": (
        "tests/test_v2_m1_artifacts.py::test_build_and_verify_contract_lock_roundtrip",
    ),
    "M1-H02": (
        "tests/test_v2_m1_contracts.py::test_records_are_frozen_and_defensively_copied",
        "tests/test_v2_m1_contracts.py::test_encode_decode_roundtrip_all_registered_records",
    ),
    "M1-H03": (
        "tests/test_v2_m1_artifacts.py::test_build_and_verify_contract_lock_roundtrip",
    ),
    "M1-H04": (
        "tests/test_v2_m1_clock.py::test_hand_aggregation_matches_spec",
        "tests/test_v2_m1_clock.py::test_minute_and_daily_horizon_windows",
    ),
    "M1-H05": (
        "tests/test_v2_m1_data.py::test_pit_visibility_and_daily_isolation",
    ),
    "M1-H06": (
        "tests/test_v2_m1_export.py::test_fake_connection_read_only_export",
    ),
    "M1-H07": (
        "tests/test_v2_m1_policy.py::test_frozen_policy_values",
    ),
    "M1-H08": (
        "tests/test_v2_m1_artifacts.py::test_build_and_verify_contract_lock_roundtrip",
    ),
    "M1-H09": (
        "tests/test_v2_m1_contracts.py::test_field_constraints_and_enum_rejections",
        "tests/test_v2_m1_policy.py::test_calculate_stress_loss_hand_cases",
        "tests/test_v2_m1_macro.py::test_macro_hand_times_and_states",
    ),
    # 007 §5.4 M1-H10: exit contract types/keys/whitelists, dual registry/
    # binding/execution-link identity, checkpoint recovery fields.
    "M1-H10": (
        "tests/test_v2_m1_exit_contracts.py::test_exit_parameter_whitelists_exact_key_sets",
        "tests/test_v2_m1_exit_contracts.py::test_exit_binding_hash_vectors_and_rejections",
        "tests/test_v2_m1_exit_contracts.py::test_exit_state_feature_types_and_keys",
        "tests/test_v2_m1_exit_contracts.py::test_checkpoint_exit_collections_and_recovery_fields",
    ),
}

# Frozen declaration of M3A's build_calibration_evidence per 变更申请-002 §2:
# keyword-only required features, no default, original six positional params
# in order. M1 registers the declaration; M3A's independent acceptance
# re-checks the actual function signature against it.
BUILD_CALIBRATION_EVIDENCE_DECLARATION_KEY = (
    "ak_trading.v2.probability.calibration.build_calibration_evidence")
BUILD_CALIBRATION_EVIDENCE_DECLARATION = (
    "build_calibration_evidence(predictions: tuple[MarketForecast, ...], "
    "labels: tuple[PathLabel, ...], block_ids: Mapping[str, str], "
    "policy: ConfidencePolicy, as_of_nano: int, valid_until_nano: int, "
    "*, features: tuple[FeatureSnapshot, ...]) -> tuple[CalibrationEvidence, ...]"
)

# Frozen declarations of the M4 ContinuousLedger revisions per 变更申请-003 §2.
# M1 registers the declarations; M4's independent acceptance re-checks the
# actual methods against them.
M4_LEDGER_DECLARATIONS: dict[str, str] = {
    "ak_trading.v2.decision.ledger.ContinuousLedger.apply_fill": (
        "ContinuousLedger.apply_fill(self, fill: Fill, "
        "rule: TradingRule) -> bool"
    ),
    "ak_trading.v2.decision.ledger.ContinuousLedger.mark": (
        "ContinuousLedger.mark(self, bars: Mapping[str, MarketBar | None], "
        "as_of_nano: int, trading_day: date, "
        "rules: tuple[TradingRule, ...]) -> AccountSnapshot"
    ),
    "ak_trading.v2.decision.ledger.ContinuousLedger.checkpoint": (
        "ContinuousLedger.checkpoint(self, states: tuple[StrategyState, ...], "
        "pending_intents: tuple[OrderIntent, ...], "
        "model_hashes: Mapping[str, str], "
        "qualification_hashes: Mapping[str, str], "
        "*, pending_remaining_lots: Mapping[str, int]) -> LedgerCheckpoint"
    ),
}

# Frozen declarations of the 变更申请-004 public interfaces. M1 registers the
# exact declarations; each owner's independent acceptance re-checks the actual
# implementations against them (no immature downstream import happens here).
V004_DECLARATIONS: dict[str, str] = {
    # §4 publication and context/gate/risk plumbing
    "ak_trading.v2.probability.forecast.publish_market_forecast": (
        "publish_market_forecast(models: ForecastModelSet, request: ForecastRequest, "
        "regime: RegimeAssessment, *, clock_nano: Callable[[], int]) -> MarketForecast"
    ),
    "ak_trading.v2.decision.pipeline.build_strategy_context": (
        "build_strategy_context(bundle: FrozenInput, contract_id: str, "
        "decision_nano: int, account: AccountSnapshot, "
        "state: StrategyState | None, forecast_models: ForecastModelSet, "
        "payoff_model: PathPayoffModel, spec: StrategySpec, "
        "seasonality_model: SeasonalityModel | None, "
        "regime_reference: RegimeReference, run_policy: RunPolicy, "
        "qualifications: tuple[StrategyQualification, ...], "
        "*, forecast_provider: Callable[[ForecastModelSet, ForecastRequest, "
        "RegimeAssessment], MarketForecast]) -> StrategyContext"
    ),
    "ak_trading.v2.decision.gates.evaluate_gate": (
        "evaluate_gate(proposal: StrategyProposal, context: StrategyContext, "
        "policy: Mapping[str, JsonValue], *, checked_at_nano: int) -> GateDecision"
    ),
    "ak_trading.v2.decision.risk.enforce_account_risk": (
        "enforce_account_risk(proposals: tuple[StrategyProposal, ...], "
        "gates: tuple[GateDecision, ...], account: AccountSnapshot, "
        "instruments: tuple[Instrument, ...], states: tuple[StrategyState, ...], "
        "pending_intents: tuple[OrderIntent, ...], rules: tuple[TradingRule, ...], "
        "capacity_remaining: Mapping[str, int], policy: Mapping[str, JsonValue], "
        "*, valuations: tuple[ValuationMark, ...]) -> tuple[GateDecision, ...]"
    ),
    # §2 replay statistics and §5 shadow activation
    "ak_trading.v2.validation.metrics.evaluate_replay": (
        "evaluate_replay(result: ReplayResult, initial_equity: Decimal, "
        "block_ids: Mapping[str, str], seed: int, "
        "*, rules: tuple[TradingRule, ...]) -> Mapping[str, JsonValue]"
    ),
    "ak_trading.v2.validation.shadow.run_shadow": (
        "run_shadow(feed: MinuteFeed, bundle: FrozenInput, "
        "forecast_models: ForecastModelSet, payoff_model: PathPayoffModel, "
        "seasonality_model: SeasonalityModel | None, "
        "regime_reference: RegimeReference, specs: tuple[StrategySpec, ...], "
        "checkpoint: LedgerCheckpoint | None, run_policy: RunPolicy, "
        "policy: Mapping[str, JsonValue], "
        "qualifications: tuple[StrategyQualification, ...], "
        "qualification_evidence: Mapping[str, bytes], receiver_clock: ReceiverClock, "
        "contract_lock: Path, previous_segment_manifest: Path | None, "
        "recovery_segment: Path | None, output: Path, until_nano: int, "
        "*, activation_request: ModelActivationRequest | None) "
        "-> Mapping[str, JsonValue]"
    ),
    "ak_trading.v2.validation.shadow.ModelActivationRequest": (
        "ModelActivationRequest(previous_checkpoint_hash: str, "
        "requested_at_nano: int, experiment_sha256: str, "
        "old_model_hashes: Mapping[str, str], new_model_hashes: Mapping[str, str], "
        "new_qualification_hashes: Mapping[str, str], "
        "source_hashes: Mapping[str, str])"
    ),
    # §3 producer content hashes and registry projection
    "ak_trading.v2.probability.models.ForecastModelSet.content_hash": (
        "ForecastModelSet.content_hash(self) -> str"
    ),
    "ak_trading.v2.probability.regimes.RegimeReference.content_hash": (
        "RegimeReference.content_hash(self) -> str"
    ),
    "ak_trading.v2.seasonality.SeasonalityModel.content_hash": (
        "SeasonalityModel.content_hash(self) -> str"
    ),
    "ak_trading.v2.decision.payoffs.PathPayoffModel.content_hash": (
        "PathPayoffModel.content_hash(self) -> str"
    ),
    "ak_trading.v2.decision.pipeline.model_hashes_for": (
        "model_hashes_for(forecast_models: ForecastModelSet, "
        "payoff_model: PathPayoffModel, regime_reference: RegimeReference, "
        "seasonality_model: SeasonalityModel | None) -> Mapping[str, str]"
    ),
    "ak_trading.v2.decision.pipeline.registry_hash_for": (
        "registry_hash_for(specs: tuple[StrategySpec, ...]) -> str"
    ),
    "ak_trading.v2.decision.payoffs.estimate_proposal_payoff": (
        "estimate_proposal_payoff(model: PathPayoffModel, proposal: StrategyProposal, "
        "context: StrategyContext, opportunity_set_id: str) -> PayoffEstimate"
    ),
}

# Frozen declarations of the 08-M3C §4.2 pure-function interfaces (P0,
# per OQ-2: declaration texts frozen from 08/007/rulings; the actual exits/*
# implementations are P1 work verified independently against these texts).
M3C_PURE_FUNCTION_DECLARATIONS: dict[str, str] = {
    "ak_trading.v2.exits.registry.ExitPolicy.evaluate": (
        "ExitPolicy.evaluate(self, context: ExitContext, "
        "previous: ExitState | None) -> ExitProposal"
    ),
    "ak_trading.v2.exits.registry.ExitPolicy.spec": (
        "ExitPolicy.spec: property -> ExitSpec"
    ),
    "ak_trading.v2.exits.registry.exit_registry_v1": (
        "exit_registry_v1(mode: Literal['MINUTE', 'DAILY']) "
        "-> Mapping[str, ExitPolicy]"
    ),
    "ak_trading.v2.exits.registry.evaluate_exit": (
        "evaluate_exit(context: ExitContext, previous: ExitState | None) "
        "-> tuple[ExitProposal, ...]"
    ),
    "ak_trading.v2.exits.features.exit_feature_registry_v1": (
        "exit_feature_registry_v1(mode: Literal['MINUTE', 'DAILY']) "
        "-> tuple[ExitFeatureSpec, ...]"
    ),
    "ak_trading.v2.exits.features.build_exit_feature_snapshot": (
        "build_exit_feature_snapshot(context: ExitContext, "
        "previous: ExitState | None) -> ExitFeatureSnapshot"
    ),
    "ak_trading.v2.exits.arbitration.arbitrate_exit": (
        "arbitrate_exit(proposals: tuple[ExitProposal, ...], "
        "context: ExitContext) -> ExitProposal"
    ),
    "ak_trading.v2.exits.state.commit_exit_observation": (
        "commit_exit_observation(previous: ExitState, proposal: ExitProposal) -> ExitState"
    ),
    "ak_trading.v2.exits.state.commit_selected_exit": (
        "commit_selected_exit(previous: ExitState, proposal: ExitProposal, "
        "links: tuple[ExitExecutionLink, ...]) -> ExitState"
    ),
    "ak_trading.v2.exits.state.apply_exit_execution": (
        "apply_exit_execution(previous: ExitState, result: ExecutionResult, "
        "link: ExitExecutionLink, account: AccountSnapshot, "
        "now_nano: int) -> ExitState"
    ),
    "ak_trading.v2.exits.state.validate_restored_exit_state": (
        "validate_restored_exit_state(state: ExitState, "
        "checkpoint: LedgerCheckpoint) -> None"
    ),
}

# Frozen declarations of the 变更申请-006 / arbitration decision (devplan-v2.1.5).
V006_DECLARATIONS: dict[str, str] = {
    "ak_trading.v2.probability.models.fit_forecast_models": (
        "fit_forecast_models(training_features: tuple[FeatureSnapshot, ...], "
        "training_regimes: tuple[RegimeAssessment, ...], "
        "training_requests: tuple[ForecastRequest, ...], "
        "training_labels: tuple[PathLabel, ...], "
        "calibration_features: tuple[FeatureSnapshot, ...], "
        "calibration_regimes: tuple[RegimeAssessment, ...], "
        "calibration_requests: tuple[ForecastRequest, ...], "
        "calibration_labels: tuple[PathLabel, ...], "
        "fit_config: Mapping[str, JsonValue], reference: RegimeReference, "
        "*, seasonality: SeasonalityModel | None) -> ForecastModelSet"
    ),
    "ak_trading.v2.factor_specs.baseline_registry_v2": (
        "baseline_registry_v2(duration_seconds: int) -> tuple[FactorSpec, ...]"
    ),
}

# Frozen declarations of the 变更申请-005 M2-facing public interfaces (required
# keyword-only calendar/previous-bar inputs, no defaults) plus the original
# M2 entry points and seasonality types verified verbatim per 005 §5.
V005_DECLARATIONS: dict[str, str] = {
    "ak_trading.v2.factors.compute_factor": (
        "compute_factor(spec: FactorSpec, bars: tuple[MarketBar, ...], "
        "decision_nano: int, *, sessions: tuple[SessionWindow, ...]) -> FactorValue"
    ),
    "ak_trading.v2.baseline.baseline_values_v2": (
        "baseline_values_v2(bars: tuple[MarketBar, ...], decision_nano: int, "
        "*, sessions: tuple[SessionWindow, ...]) -> tuple[FactorValue, ...]"
    ),
    "ak_trading.v2.baseline.legacy_daily_baseline": (
        "legacy_daily_baseline(contract_id: str, bars: tuple[MarketBar, ...], "
        "*, decision_nano: int, "
        "sessions: tuple[SessionWindow, ...]) -> tuple[FeatureSnapshot, ...]"
    ),
    "ak_trading.v2.seasonality.seasonal_values": (
        "seasonal_values(model: SeasonalityModel, bar: MarketBar, "
        "sessions: tuple[SessionWindow, ...], decision_nano: int, "
        "*, previous_bar: MarketBar | None) -> tuple[FactorValue, ...]"
    ),
    "ak_trading.v2.factors.build_feature_snapshot": (
        "build_feature_snapshot(contract_id: str, bars: tuple[MarketBar, ...], "
        "decision_nano: int, specs: tuple[FactorSpec, ...], "
        "daily_background: FeatureSnapshot | None, seasonal: SeasonalityModel | None, "
        "rule: TradingRule | None, "
        "sessions: tuple[SessionWindow, ...]) -> FeatureSnapshot"
    ),
    "ak_trading.v2.seasonality.fit_seasonality": (
        "fit_seasonality(bars: tuple[MarketBar, ...], "
        "sessions: tuple[SessionWindow, ...], fit_start_nano: int, "
        "fit_end_nano: int) -> SeasonalityModel"
    ),
    "ak_trading.v2.seasonality.SeasonalBucket": (
        "SeasonalBucket(product: str, session_kind: str, slot_index: int, "
        "duration_seconds: int, median_volume: Decimal | None, "
        "median_abs_return: float | None, sample_days: int)"
    ),
    "ak_trading.v2.seasonality.SeasonalityModel": (
        "SeasonalityModel(model_id: str, fit_start_nano: int, fit_end_nano: int, "
        "calendar_version: str, buckets: tuple[SeasonalBucket, ...], "
        "input_sha256: str)"
    ),
}

# ─── E-P1 (v2-ep1-training-path-20260917-r1): M1-frozen declarations and the
# contract-target allocation record for the fixed-candidate offline path
# measurement source. The allocation below is a written record, not a
# published lock; publication belongs to the later EP1-REVIEW flow.
EP1_CONTRACT_TARGET_ALLOCATION: Mapping[str, str] = MappingProxyType({
    # Baseline inventory (2026-09-17 freeze): contracts-2.1.3 chain published
    # contract-lock.json, p1-r1, p2-r1, p3-r1; p4-r1 is RESERVED (unpublished)
    # for the E1/E2 emergency payoff-producer revision per
    # v2-emergency-payoff-import-arbitration-20260917-r1 RULING 4.1(4).
    "predecessor_published_lock":
        "data/research_runs/v2/contracts-2.1.3/contract-lock.p3-r1.json",
    "reserved_revisions":
        "contracts-2.1.3 p4-r1 (E1/E2 emergency producer revision, "
        "unpublished at EP1-M1 freeze); contracts-2.1.4 chain + "
        "experiment/research_config v2.1.2 (C1 condition protocol per "
        "v2-arbitration-legacy-binding-convergence-20260917-r1 RULING 2.3)",
    # Unique unoccupied target allocated to THIS increment's new public
    # structures (PayoffTrainingPlan/PayoffPath*/PayoffReceiptCommit, the two
    # v1 source schemas and the payoff_training_plan_sha256 registration).
    "contract_chain": "data/research_runs/v2/contracts-2.1.5",
    "contract_version_target": "ak_probability_strategy_contract_v2.1.5",
    "lock_path": "data/research_runs/v2/contracts-2.1.5/contract-lock.ep1-r1.json",
    "reviewed_revision_target": "ep1-r1",
    # New strict registration field target schemas: v2.1.3 is the next real
    # unoccupied version of the experiment/research_config lineage
    # (v2.1.1 -> 007 exit registration, v2.1.2 -> C1 condition protocol);
    # C1's v2.1.2 target is NOT taken over and C1 is NOT claimed done.
    "experiment_schema_target": "ak_probability_experiment_v2.1.3",
    "research_config_schema_target": "ak_probability_research_config_v2.1.3",
})

# §2.1/§2.4 producer entry points and the §2.2 plan-registration strict field,
# frozen as declarations; the implementations are EP1-M4 (decision/*) and
# EP1-M5 (validation/experiments.py) work and are checked against these texts.
EP1_PAYOFF_PATH_DECLARATIONS: dict[str, str] = {
    "ak_trading.v2.decision.payoff_paths.replay_payoff_training_window": (
        "replay_payoff_training_window(bundle: FrozenInput, "
        "forecast_models: ForecastModelSet, spec: StrategySpec, "
        "initial_account: AccountSnapshot, run_policy: RunPolicy, "
        "policy: Mapping[str, JsonValue], "
        "seasonality_model: SeasonalityModel | None, "
        "regime_reference: RegimeReference, contract_lock_sha256: str, "
        "plan: PayoffTrainingPlan, *, exit_specs: tuple[ExitSpec, ...], "
        "exit_binding: ExitPolicyBinding) -> PayoffPathResult"
    ),
    "ak_trading.v2.decision.payoffs.observation_from_replay": (
        "observation_from_replay(opened: StrategyProposal, "
        "context: StrategyContext, request: ForecastRequest, "
        "label: PathLabel, replay: ReplayResult | PayoffPathResult, "
        "training_end_nano: int, block_id: str, "
        "*, rule_originals: tuple[TradingRule, ...] | None = None) "
        "-> PayoffObservation"
    ),
    "ak_trading.v2.decision.payoffs.payoff_observation_source": (
        "payoff_observation_source(opened: StrategyProposal, "
        "context: StrategyContext, request: ForecastRequest, "
        "label: PathLabel, replay: ReplayResult | PayoffPathResult, "
        "training_end_nano: int, block_id: str, "
        "*, rule_originals: tuple[TradingRule, ...] | None = None) -> bytes"
    ),
}

EP1_PLAN_REGISTRATION_DECLARATIONS: dict[str, str] = {
    "ak_trading.v2.validation.experiments.ExperimentSpec.payoff_training_plan_sha256": (
        "ExperimentSpec.payoff_training_plan_sha256: str "
        "(strict required; schema ak_probability_experiment_v2.1.3; "
        "64-hex SHA-256 of the canonical frozen plan-set bytes, fixed before "
        "expensive fitting and before reading OOS results)"
    ),
    "ak_trading.v2.validation.experiments.research_config.payoff_training_plan_sha256": (
        "research_config.payoff_training_plan_sha256: str "
        "(strict required; schema ak_probability_research_config_v2.1.3; "
        "64-hex SHA-256 of the canonical frozen plan-set bytes)"
    ),
}


class ArtifactsError(ValueError):
    """Fixed-reason publication/verification violation."""


def sha256_hex(payload: bytes) -> str:
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("payload_must_be_bytes")
    return hashlib.sha256(bytes(payload)).hexdigest()


def stable_id(category: str, content: str | bytes) -> str:
    """``<category>:<sha256>`` from canonical content; no wall clock or randomness.

    Stable identities only hash the actually consumed historical prefix and
    its dependencies; the whole-package ``FrozenInput.manifest_sha256`` never
    enters a historical identity (devplan-v2.1.0 §4).
    """
    if not isinstance(category, str) or not category:
        raise TypeError("category_must_be_non_empty_str")
    payload = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    return f"{category}:{sha256_hex(payload)}"


canonical_json_bytes = canonical_json_bytes  # single canonical codec lives in schemas


def _is_hex_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in _HEX for char in value)


def _safe_relative(name: str) -> Path:
    if not isinstance(name, str) or not name:
        raise ArtifactsError("file_name_must_be_non_empty_str")
    if name.startswith(("/", "\\")) or ":" in name or ".." in Path(name).parts:
        raise ArtifactsError(f"unsafe_file_name:{name}")
    candidate = Path(name)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise ArtifactsError(f"unsafe_file_name:{name}")
    return candidate


def _write_file_atomic(target: Path, payload: bytes) -> None:
    temporary = target.with_name(target.name + ".tmp")
    with open(temporary, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def _str_dict(value: object, name: str) -> dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
    ):
        raise ArtifactsError(f"{name}_must_be_dict_str_str")
    return value


def _validate_input_provenance(provenance: Mapping[str, Any]) -> dict[str, Any]:
    if set(provenance.keys()) != _INPUT_MANIFEST_KEYS:
        raise ArtifactsError(f"provenance_keys_must_be_exactly_{sorted(_INPUT_MANIFEST_KEYS)}")
    if provenance["schema_version"] != INPUT_SCHEMA_VERSION:
        raise ArtifactsError("manifest_schema_version_mismatch")
    if provenance["contract_version"] != CONTRACT_VERSION:
        raise ArtifactsError("manifest_contract_version_mismatch")
    if provenance["dataset_kind"] not in DATASET_KINDS:
        raise ArtifactsError("dataset_kind_not_in_allowed_enum")
    if isinstance(provenance["created_at_nano"], bool) or not isinstance(provenance["created_at_nano"], int):
        raise ArtifactsError("created_at_nano_must_be_int")
    request = provenance["request"]
    if not isinstance(request, dict):
        raise ArtifactsError("request_must_be_object")
    # F10(r3): the input manifest request must carry the full ExportRequestV2
    # schema (minus instruments/sessions/rules) with exact field names/types.
    _REQUEST_SCHEMA_KEYS = frozenset({
        "contract_ids", "raw_symbols", "start_nano", "end_nano", "as_of_nano",
        "durations", "page_size", "maximum_rows", "maximum_bytes",
    })
    if set(request.keys()) != _REQUEST_SCHEMA_KEYS:
        raise ArtifactsError("request_fields_must_match_export_request_v2_schema")
    for field in ("contract_ids", "raw_symbols"):
        if (not isinstance(request[field], list) or not request[field]
                or any(not isinstance(item, str) for item in request[field])):
            raise ArtifactsError(f"request_{field}_must_be_non_empty_str_list")
    for field in ("start_nano", "end_nano", "as_of_nano", "page_size",
                  "maximum_rows", "maximum_bytes"):
        if isinstance(request[field], bool) or not isinstance(request[field], int):
            raise ArtifactsError(f"request_{field}_must_be_int_not_bool")
    if request["durations"] != [60, 86400]:
        raise ArtifactsError("request_durations_must_be_exactly_60_86400")
    transaction = provenance["read_only_transaction"]
    if not isinstance(transaction, dict) or set(transaction.keys()) != {"isolation", "read_only", "verified"}:
        raise ArtifactsError("read_only_transaction_fields_must_be_exactly_isolation_read_only_verified")
    if not isinstance(transaction["isolation"], str):
        raise ArtifactsError("read_only_isolation_must_be_str")
    if transaction["read_only"] is not True:
        raise ArtifactsError("read_only_must_be_true")
    if not isinstance(transaction["verified"], bool):
        raise ArtifactsError("verified_must_be_bool")
    # F10(r3): SYNTHETIC inputs must declare NOT_APPLICABLE/true/false; a
    # real-database transaction claim on a synthetic bundle is invalid.
    if provenance["dataset_kind"] == "SYNTHETIC":
        if (transaction["isolation"] != "NOT_APPLICABLE"
                or transaction["read_only"] is not True
                or transaction["verified"] is not False):
            raise ArtifactsError("synthetic_input_must_declare_not_applicable_unverified")
    source_hashes = _str_dict(provenance["source_hashes"], "source_hashes")
    if any(not _is_hex_hash(value) for value in source_hashes.values()):
        raise ArtifactsError("source_hashes_values_must_be_sha256")
    limitations = provenance["limitations"]
    if not isinstance(limitations, list) or any(not isinstance(item, str) for item in limitations):
        raise ArtifactsError("limitations_must_be_list_str")
    return dict(provenance)


def _validate_run_provenance(provenance: Mapping[str, Any]) -> dict[str, Any]:
    if set(provenance.keys()) != _RUN_CONTROL_KEYS:
        raise ArtifactsError(f"run_provenance_control_keys_must_be_exactly_{sorted(_RUN_CONTROL_KEYS)}")
    if not isinstance(provenance["schema_version"], str) or not provenance["schema_version"]:
        raise ArtifactsError("run_schema_version_must_be_non_empty_str")
    if isinstance(provenance["created_at_nano"], bool) or not isinstance(provenance["created_at_nano"], int):
        raise ArtifactsError("created_at_nano_must_be_int")
    body = provenance["provenance"]
    if not isinstance(body, dict) or set(body.keys()) != _RUN_PROVENANCE_KEYS:
        raise ArtifactsError(f"run_provenance_fields_must_be_exactly_{sorted(_RUN_PROVENANCE_KEYS)}")
    for field in ("node", "dataset_kind"):
        if not isinstance(body[field], str) or not body[field]:
            raise ArtifactsError(f"run_provenance_{field}_must_be_non_empty_str")
    if body["dataset_kind"] not in DATASET_KINDS:
        raise ArtifactsError("dataset_kind_not_in_allowed_enum")
    for field in ("input_hashes", "limitations"):
        if not isinstance(body[field], list) or any(not isinstance(item, str) for item in body[field]):
            raise ArtifactsError(f"run_provenance_{field}_must_be_list_str")
    for field in ("code_hashes", "parameter_hashes", "model_hashes"):
        mapping = _str_dict(body[field], f"run_provenance_{field}")
        if any(not _is_hex_hash(value) for value in mapping.values()):
            raise ArtifactsError(f"run_provenance_{field}_values_must_be_sha256")
    _str_dict(body["policy_versions"], "run_provenance_policy_versions")
    if any(not _is_hex_hash(item) for item in body["input_hashes"]):
        raise ArtifactsError("run_provenance_input_hashes_must_be_sha256")
    seed = body["seed"]
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        raise ArtifactsError("run_provenance_seed_must_be_int_or_null")
    for field in ("previous_segment_manifest_sha256", "recovery_source_sha256", "ignored_tail_sha256"):
        if body[field] is not None and not _is_hex_hash(body[field]):
            raise ArtifactsError(f"run_provenance_{field}_must_be_hex_or_null")
    return {
        "schema_version": provenance["schema_version"],
        "contract_version": CONTRACT_VERSION,
        "created_at_nano": provenance["created_at_nano"],
        "provenance": body,
    }


def publish_bundle(output: Path, payloads: Mapping[str, bytes],
                   provenance: Mapping[str, JsonValue]) -> str:
    """Publish a frozen bundle; returns the manifest SHA-256.

    The output directory must not exist; payload files are written first and
    ``manifest.json`` is committed last atomically. Manifest hashes cover
    every payload file but never the manifest itself (no self-reference).
    F09(r3): a single defensive byte snapshot is taken ONCE — validation,
    hashing and disk writes all consume the same frozen bytes; a Mapping
    whose items() yields different bytes per iteration cannot desynchronize
    the manifest. Case-insensitive manifest.json reserved-name aliases are
    rejected on every platform.
    """
    if not isinstance(output, Path):
        raise TypeError("output_must_be_Path")
    if not isinstance(payloads, Mapping):
        raise TypeError("payloads_must_be_mapping")
    if output.exists() or output.is_symlink():
        raise ArtifactsError("output_directory_exists")
    # F09: case-insensitive manifest reserved name (Windows/POSIX portable).
    if any(str(name).lower() == "manifest.json" for name in payloads):
        raise ArtifactsError("manifest_name_reserved")
    if set(provenance.keys()) == _RUN_CONTROL_KEYS:
        manifest = _validate_run_provenance(provenance)
    else:
        manifest = _validate_input_provenance(provenance)
    # F09: single defensive snapshot — iterate items() exactly once and
    # freeze the results; hash and write from the same staged list.
    staged: list[tuple[str, bytes]] = []
    seen_lower: set[str] = set()
    for name, payload in payloads.items():
        _safe_relative(name)
        if not isinstance(payload, (bytes, bytearray)):
            raise ArtifactsError(f"payload_must_be_bytes:{name}")
        lowered = str(name).lower()
        if lowered in seen_lower:
            raise ArtifactsError(f"payload_name_case_duplicate:{name}")
        seen_lower.add(lowered)
        staged.append((name, bytes(payload)))
    files: dict[str, str] = {name: sha256_hex(payload) for name, payload in staged}
    output.mkdir(parents=True, exist_ok=False)
    for name, payload in staged:
        relative = _safe_relative(name)
        target = output / relative
        if target.exists() or target.is_symlink():
            raise ArtifactsError(f"payload_target_exists:{name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "xb") as handle:
            handle.write(payload)
        if target.is_symlink():
            raise ArtifactsError(f"symlink_rejected:{name}")
    manifest["files"] = files
    manifest_bytes = canonical_json_bytes(manifest)
    _write_file_atomic(output / "manifest.json", manifest_bytes)
    return sha256_hex(manifest_bytes)


def canonical_function_signature(function: Any) -> str:
    """Canonical text form of a public function/method signature.

    Retains parameter kinds (``*`` before the first keyword-only parameter,
    ``*args``/``**kwargs`` spellings), default value reprs and ``async`` so
    the lock binds the real callable contract, not a lossy projection.
    """
    try:
        signature = inspect.signature(function, eval_str=True)
    except (TypeError, NameError) as exc:
        raise ArtifactsError(
            f"signature_evaluation_failed:{getattr(function, '__qualname__', function)}") from exc
    parts: list[str] = []
    seen_keyword_only = False
    for parameter_name, parameter in signature.parameters.items():
        prefix = ""
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            prefix = "*"
        elif parameter.kind is inspect.Parameter.VAR_KEYWORD:
            prefix = "**"
        elif parameter.kind is inspect.Parameter.KEYWORD_ONLY and not seen_keyword_only:
            parts.append("*")
            seen_keyword_only = True
        rendered = prefix + parameter_name
        if parameter.annotation is not inspect.Parameter.empty:
            rendered += f": {type_to_str(parameter.annotation)}"
        if parameter.default is not inspect.Parameter.empty:
            rendered += f" = {parameter.default!r}"
        parts.append(rendered)
    name = getattr(function, "__qualname__", getattr(function, "__name__", str(function)))
    if inspect.iscoroutinefunction(function):
        name = f"async {name}"
    result = f"{name}(" + ", ".join(parts) + ")"
    if signature.return_annotation is not inspect.Signature.empty:
        result += f" -> {type_to_str(signature.return_annotation)}"
    return result


def _public_signature_map() -> dict[str, str]:
    from ak_trading.v2.schemas import PAYOFF_PATH_RECORD_TYPES
    signatures: dict[str, str] = {}
    for kind in sorted({**RECORD_TYPES, **PAYOFF_PATH_RECORD_TYPES}):
        signatures[kind] = canonical_type_signature(
            {**RECORD_TYPES, **PAYOFF_PATH_RECORD_TYPES}[kind])
    exported_functions: tuple[tuple[str, str], ...] = (
        ("ak_trading.v2.clock", "aggregate_bars"),
        ("ak_trading.v2.clock", "horizon_window"),
        ("ak_trading.v2.clock", "holding_deadline"),
        ("ak_trading.v2.clock", "can_open_before_close"),
        ("ak_trading.v2.data", "read_frozen_input"),
        ("ak_trading.v2.data", "publish_input_collection"),
        ("ak_trading.v2.data", "visible_bars"),
        ("ak_trading.v2.data", "visible_rule"),
        ("ak_trading.v2.schemas", "encode_record"),
        ("ak_trading.v2.schemas", "decode_record"),
        ("ak_trading.v2.schemas", "validate_record"),
        ("ak_trading.v2.schemas", "calibration_geometry_key"),
        ("ak_trading.v2.contracts", "exit_binding_content_hash"),
        ("ak_trading.v2.artifacts", "canonical_json_bytes"),
        ("ak_trading.v2.artifacts", "publish_bundle"),
        ("ak_trading.v2.artifacts", "verify_contract_lock"),
        ("ak_trading.v2.artifacts", "verify_qualification_evidence"),
        ("ak_trading.v2.policy", "confidence_policy"),
        ("ak_trading.v2.policy", "holding_policy"),
        ("ak_trading.v2.policy", "stress_policy"),
        ("ak_trading.v2.policy", "calculate_stress_loss"),
        ("ak_trading.v2.policy", "research_policy"),
        ("ak_trading.v2.export", "export_frozen_input"),
        ("ak_trading.v2.macro", "refresh_macro_cache"),
        ("ak_trading.v2.macro", "derive_macro_tags"),
    )
    for module_name, attribute in exported_functions:
        module = importlib.import_module(module_name)
        function = getattr(module, attribute)
        signatures[f"{module_name}.{attribute}"] = canonical_function_signature(function)
    macro_module = importlib.import_module("ak_trading.v2.macro")
    signatures["ak_trading.v2.macro.EconTransport.call_tool"] = canonical_function_signature(
        macro_module.EconTransport.call_tool)
    for method in ("__init__", "append", "snapshot"):
        signatures[f"ak_trading.v2.macro.MacroCache.{method}"] = canonical_function_signature(
            getattr(macro_module.MacroCache, method))
    export_module = importlib.import_module("ak_trading.v2.export")
    signatures["ExportRequestV2"] = canonical_type_signature(export_module.ExportRequestV2)
    signatures[BUILD_CALIBRATION_EVIDENCE_DECLARATION_KEY] = \
        BUILD_CALIBRATION_EVIDENCE_DECLARATION
    signatures.update(M4_LEDGER_DECLARATIONS)
    signatures.update(V004_DECLARATIONS)
    signatures.update(V005_DECLARATIONS)
    signatures.update(V006_DECLARATIONS)
    signatures.update(M3C_PURE_FUNCTION_DECLARATIONS)
    signatures.update(EP1_PAYOFF_PATH_DECLARATIONS)
    signatures.update(EP1_PLAN_REGISTRATION_DECLARATIONS)
    return signatures


def _schema_hash_map() -> dict[str, str]:
    from ak_trading.v2.schemas import (
        AVAILABILITY_SCHEMA_ID, BARS_SCHEMA_ID, CHECKPOINT_SCHEMA_ID,
        CONTRACT_LOCK_SCHEMA_ID, EXIT_AUDIT_SCHEMA_ID, EXIT_BINDINGS_SCHEMA_ID,
        EXIT_EXECUTION_LINKS_SCHEMA_ID, EXIT_FEATURES_SCHEMA_ID,
        EXIT_FEATURE_SPECS_SCHEMA_ID, EXIT_LOGICAL_REPLAY_SCHEMA_ID,
        EXIT_PROPOSALS_SCHEMA_ID, EXIT_RUN_SCHEMA_ID, EXIT_SPECS_SCHEMA_ID,
        EXIT_STATES_SCHEMA_ID, FACTOR_REGISTRY_SCHEMA_ID, INPUT_COLLECTION_SCHEMA_ID,
        INSTRUMENTS_SCHEMA_ID, MACRO_ITEMS_SCHEMA_ID, MANIFEST_SCHEMA_ID, METRICS_SCHEMA_ID,
        MODEL_ACTIVATIONS_SCHEMA_ID, MODEL_ACTIVATION_REQUEST_SCHEMA_ID,
        PAYOFF_PATH_SCHEMA_ID, PAYOFF_TRAINING_PLAN_SCHEMA_ID,
        RULES_SCHEMA_ID, SEASONALITY_SCHEMA_ID, SESSIONS_SCHEMA_ID,
        SHADOW_CHECKPOINT_SCHEMA_ID, SHADOW_EVENT_SCHEMA_ID, SHARED_FIXTURE_SCHEMA_ID,
    )
    schema_ids = (
        BARS_SCHEMA_ID, INSTRUMENTS_SCHEMA_ID, SESSIONS_SCHEMA_ID, MACRO_ITEMS_SCHEMA_ID,
        RULES_SCHEMA_ID, AVAILABILITY_SCHEMA_ID, MANIFEST_SCHEMA_ID, SHARED_FIXTURE_SCHEMA_ID,
        CONTRACT_LOCK_SCHEMA_ID, CHECKPOINT_SCHEMA_ID, SHADOW_CHECKPOINT_SCHEMA_ID,
        METRICS_SCHEMA_ID, SHADOW_EVENT_SCHEMA_ID, MODEL_ACTIVATION_REQUEST_SCHEMA_ID,
        MODEL_ACTIVATIONS_SCHEMA_ID, SEASONALITY_SCHEMA_ID, INPUT_COLLECTION_SCHEMA_ID,
        FACTOR_REGISTRY_SCHEMA_ID,
        EXIT_SPECS_SCHEMA_ID, EXIT_FEATURE_SPECS_SCHEMA_ID, EXIT_BINDINGS_SCHEMA_ID,
        EXIT_FEATURES_SCHEMA_ID, EXIT_STATES_SCHEMA_ID, EXIT_PROPOSALS_SCHEMA_ID,
        EXIT_EXECUTION_LINKS_SCHEMA_ID, EXIT_AUDIT_SCHEMA_ID,
        EXIT_LOGICAL_REPLAY_SCHEMA_ID, EXIT_RUN_SCHEMA_ID,
        PAYOFF_TRAINING_PLAN_SCHEMA_ID, PAYOFF_PATH_SCHEMA_ID,
    )
    return {
        schema_id: sha256_hex(canonical_schema_descriptor(schema_id).encode("utf-8"))
        for schema_id in schema_ids
    }


def _collect_test_names(source: str) -> list[str]:
    tree = ast.parse(source)
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            names.append(node.name)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name.startswith("test_"):
                    names.append(f"{node.name}::{child.name}")
    return names


def _required_tests_map(project_root: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for test_file in (*PUBLIC_TEST_FILES, *DOWNSTREAM_TEST_FILES):
        path = project_root / test_file
        if not path.is_file():
            raise ArtifactsError(f"required_test_file_missing:{test_file}")
        result[test_file] = _collect_test_names(path.read_text(encoding="utf-8"))
    return result


def _required_check_id_map() -> dict[str, list[str]]:
    return required_check_ids_map()


def _verify_check_id_references(check_ids: Mapping[str, list[str]],
                                 tests: Mapping[str, list[str]]) -> None:
    """The lock's required_check_ids must equal the fixed all-node inventory
    (each node's full H list plus applicable D items), and every M1 clause in
    the internal evidence mapping must be covered by existing M1 tests."""
    if check_ids != required_check_ids_map():
        raise ArtifactsError("required_check_ids_inventory_mismatch")
    node_index = {
        f"{test_file}::{name}": None for test_file, names in tests.items() for name in names
    }
    for check_id, references in M1_CHECK_TEST_EVIDENCE.items():
        for reference in references:
            if reference not in node_index:
                raise ArtifactsError(f"required_check_id_references_unknown_test:{reference}")


def _hash_files(project_root: Path, files: tuple[str, ...]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name in files:
        path = project_root / name
        if not path.is_file() or path.is_symlink():
            raise ArtifactsError(f"public_file_missing_or_symlink:{name}")
        hashes[name] = sha256_hex(path.read_bytes())
    return hashes


def _lf_normalized(payload: bytes) -> bytes:
    """UTF-8 payload with CRLF line endings normalized to LF (the H_F basis)."""
    return bytes(payload).replace(b"\r\n", b"\n")


def _read_acceptance_manifest(project_root: Path) -> tuple[bytes, Any]:
    path = project_root / ACCEPTANCE_INPUT_MANIFEST_PATH
    try:
        payload = path.read_bytes()
    except OSError:
        raise ArtifactsError("acceptance_input_manifest_not_frozen") from None
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        raise ArtifactsError("acceptance_input_manifest_not_utf8") from None
    try:
        document = parse_json_bytes(payload)
    except SchemaError as exc:
        raise ArtifactsError(f"acceptance_manifest_invalid_json:{exc}") from None
    if not isinstance(document, dict):
        raise ArtifactsError("acceptance_manifest_root_must_be_object")
    return payload, document


def _acceptance_attachment_path(name: object) -> str:
    """Safe, canonical, forward-slash, non-circular attachment path."""
    if not isinstance(name, str) or not name:
        raise ArtifactsError("acceptance_manifest_path_must_be_non_empty_str")
    if "\\" in name:
        raise ArtifactsError(f"acceptance_manifest_path_must_use_forward_slashes:{name}")
    _safe_relative(name)  # rejects absolute paths, "..", drive letters
    if PurePosixPath(name).as_posix() != name:
        raise ArtifactsError(f"acceptance_manifest_path_not_canonical:{name}")
    for prefix in _ACCEPTANCE_FORBIDDEN_ATTACHMENT_PREFIXES:
        if name.startswith(prefix):
            raise ArtifactsError(f"acceptance_manifest_attachment_path_forbidden:{name}")
    if name == ACCEPTANCE_INPUT_MANIFEST_PATH:
        raise ArtifactsError("acceptance_manifest_must_not_list_itself")
    return name


def _scan_acceptance_circular_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _ACCEPTANCE_FORBIDDEN_KEY_NAMES:
                raise ArtifactsError(f"acceptance_manifest_circular_field_forbidden:{key}")
            _scan_acceptance_circular_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _scan_acceptance_circular_keys(item)


def acceptance_input_manifest_sha256(project_root: Path) -> str:
    """H_F: SHA-256 over F's UTF-8 bytes with CRLF normalized to LF.

    Only the manifest itself is line-ending normalized; attachments are
    always hashed over their original raw bytes (§5.1: never re-serialized
    fake originals). Raises when F is absent or not strict UTF-8 JSON.
    """
    if not isinstance(project_root, Path):
        raise TypeError("project_root_must_be_Path")
    payload, _ = _read_acceptance_manifest(project_root)
    return sha256_hex(_lf_normalized(payload))


def verify_acceptance_input_manifest(project_root: Path) -> str:
    """Validate the frozen acceptance input manifest F and return H_F.

    §5.1/§5.3 contract enforced here (lock-side, content stays M5-owned):
    strict root schema; SYNTHETIC/SCENARIO labelling with SYNTHETIC present;
    a fixed generation recipe plus integer seed; the five input sections
    (minute/daily bars, calendar, rules, costs); partition and selection
    parameters; spec/binding references; and a complete attachment table
    (relative path, byte length, SHA-256 per attachment, hashed over the
    attachment's original bytes, sorted, unique, no self-listing). F never
    carries lock/report/readiness fields at any depth and never lists
    published run outputs, so no lock→output→lock cycle is expressible.
    The behavioral-source floor requires every src/ak_trading/v2 *.py file,
    both runners and every lock-required test file to be listed; files on
    disk but unlisted, listed but missing, drifted bytes, extra files in
    the fixture directory and path escapes are all rejected.
    """
    if not isinstance(project_root, Path):
        raise TypeError("project_root_must_be_Path")
    payload, document = _read_acceptance_manifest(project_root)
    if set(document.keys()) != _ACCEPTANCE_MANIFEST_KEYS:
        raise ArtifactsError(
            "acceptance_manifest_root_fields_must_be_exactly_"
            f"{sorted(_ACCEPTANCE_MANIFEST_KEYS)}")
    if document["schema_version"] != ACCEPTANCE_INPUT_MANIFEST_SCHEMA_ID:
        raise ArtifactsError("acceptance_manifest_schema_version_mismatch")
    if document["dataset_kind"] != "SYNTHETIC":
        raise ArtifactsError("acceptance_manifest_dataset_kind_must_be_synthetic")
    labels = document["labels"]
    if (not isinstance(labels, list) or not labels
            or any(not isinstance(item, str) for item in labels)
            or len(set(labels)) != len(labels)
            or any(item not in _ACCEPTANCE_LABELS for item in labels)):
        raise ArtifactsError("acceptance_manifest_labels_must_be_unique_synthetic_or_scenario")
    if "SYNTHETIC" not in labels:
        raise ArtifactsError("acceptance_manifest_labels_must_include_synthetic")
    generation = document["generation"]
    if not isinstance(generation, dict) or set(generation.keys()) != _ACCEPTANCE_GENERATION_KEYS:
        raise ArtifactsError("acceptance_manifest_generation_fields_mismatch")
    if not isinstance(generation["recipe"], str) or not generation["recipe"]:
        raise ArtifactsError("acceptance_manifest_recipe_must_be_non_empty_str")
    if isinstance(generation["seed"], bool) or not isinstance(generation["seed"], int):
        raise ArtifactsError("acceptance_manifest_seed_must_be_int_not_bool")
    inputs = document["inputs"]
    if not isinstance(inputs, dict) or set(inputs.keys()) != _ACCEPTANCE_INPUT_SECTION_KEYS:
        raise ArtifactsError("acceptance_manifest_input_sections_mismatch")
    for section in sorted(_ACCEPTANCE_INPUT_SECTION_KEYS):
        paths = inputs[section]
        if (not isinstance(paths, list) or not paths
                or any(not isinstance(item, str) or not item for item in paths)
                or len(set(paths)) != len(paths)):
            raise ArtifactsError(
                f"acceptance_manifest_input_{section}_must_be_unique_non_empty_str_list")
    for section in ("splits", "selection"):
        body = document[section]
        if (not isinstance(body, dict) or not body
                or any(not isinstance(key, str) or not key for key in body)):
            raise ArtifactsError(f"acceptance_manifest_{section}_must_be_non_empty_dict")
    bindings = document["bindings"]
    if (not isinstance(bindings, list) or not bindings
            or any(not isinstance(item, dict) for item in bindings)):
        raise ArtifactsError("acceptance_manifest_bindings_must_be_non_empty_list_of_objects")
    _scan_acceptance_circular_keys(document)
    entries = document["files"]
    if not isinstance(entries, list) or not entries:
        raise ArtifactsError("acceptance_manifest_files_must_be_non_empty_list")
    table: dict[str, tuple[int, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry.keys()) != _ACCEPTANCE_ATTACHMENT_KEYS:
            raise ArtifactsError("acceptance_manifest_entry_fields_must_be_path_bytes_sha256")
        name = _acceptance_attachment_path(entry["path"])
        size = entry["bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ArtifactsError("acceptance_manifest_entry_bytes_must_be_non_negative_int")
        if not _is_hex_hash(entry["sha256"]):
            raise ArtifactsError("acceptance_manifest_entry_sha256_must_be_64_lowercase_hex")
        if name in table:
            raise ArtifactsError(f"acceptance_manifest_duplicate_attachment:{name}")
        table[name] = (size, entry["sha256"])
    if list(table) != sorted(table):
        raise ArtifactsError("acceptance_manifest_files_must_be_sorted_by_path")
    referenced = {generation["recipe"]}
    for paths in inputs.values():
        referenced.update(paths)
    for reference in sorted(referenced):
        if reference not in table:
            raise ArtifactsError(f"acceptance_manifest_reference_not_listed:{reference}")
    # §5.3 item 3: behavioral-source floor — everything code_hashes() covers
    # today, the offline runner and every lock-required test file.
    required_sources: set[str] = {
        p.relative_to(project_root).as_posix()
        for p in (project_root / ACCEPTANCE_SOURCE_ROOT).rglob("*.py")
        if p.is_file() and not p.is_symlink()
    }
    required_sources.add(ACCEPTANCE_RUNNER_PATH)
    required_sources.add(ACCEPTANCE_OFFLINE_RUNNER_PATH)
    required_sources.update((*PUBLIC_TEST_FILES, *DOWNSTREAM_TEST_FILES))
    for missing in sorted(required_sources - set(table)):
        raise ArtifactsError(f"acceptance_manifest_required_source_not_listed:{missing}")
    # Per-attachment verification: existence, exact byte length and SHA-256
    # over the attachment's original raw bytes.
    for name in sorted(table):
        target = project_root / name
        if not target.is_file() or target.is_symlink():
            raise ArtifactsError(f"acceptance_manifest_attachment_missing:{name}")
        content = target.read_bytes()
        size, digest = table[name]
        if len(content) != size:
            raise ArtifactsError(f"acceptance_manifest_attachment_size_mismatch:{name}")
        if sha256_hex(content) != digest:
            raise ArtifactsError(f"acceptance_manifest_attachment_hash_mismatch:{name}")
    # Exact set inside the T7 fixture directory: every file on disk is either
    # F itself or a listed attachment — no unlisted extras.
    fixture_root = project_root / _ACCEPTANCE_FIXTURE_DIR
    if fixture_root.is_dir():
        present = {
            p.relative_to(project_root).as_posix()
            for p in fixture_root.rglob("*") if p.is_file() and not p.is_symlink()
        }
        for extra in sorted(present - {ACCEPTANCE_INPUT_MANIFEST_PATH} - set(table)):
            raise ArtifactsError(f"acceptance_manifest_unlisted_fixture_file:{extra}")
    return sha256_hex(_lf_normalized(payload))


def _fixture_hashes(project_root: Path) -> dict[str, str]:
    """fixture_files map: shared.json keeps its raw-bytes hash; F is bound to
    H_F (UTF-8/LF-normalized manifest bytes) with its whole attachment table
    recursively verified, so both lock build and verify enforce §5.3 item 2."""
    hashes = _hash_files(project_root, tuple(
        name for name in FIXTURE_FILES if name != ACCEPTANCE_INPUT_MANIFEST_PATH))
    hashes[ACCEPTANCE_INPUT_MANIFEST_PATH] = verify_acceptance_input_manifest(project_root)
    return hashes


DECLARATION_OWNERSHIP: dict[str, list[str]] = {
    "M1": [
        "ak_trading.v2.schemas.encode_record",
        "ak_trading.v2.schemas.decode_record",
        "ak_trading.v2.schemas.validate_record",
        "ak_trading.v2.schemas.calibration_geometry_key",
        "ak_trading.v2.artifacts.canonical_json_bytes",
        "ak_trading.v2.artifacts.publish_bundle",
        "ak_trading.v2.artifacts.verify_contract_lock",
        "ak_trading.v2.artifacts.verify_qualification_evidence",
        "ak_trading.v2.data.read_frozen_input",
        "ak_trading.v2.data.publish_input_collection",
        "ak_trading.v2.data.visible_bars",
        "ak_trading.v2.data.visible_rule",
        "ak_trading.v2.clock.aggregate_bars",
        "ak_trading.v2.clock.horizon_window",
        "ak_trading.v2.clock.holding_deadline",
        "ak_trading.v2.clock.can_open_before_close",
        "ak_trading.v2.export.export_frozen_input",
        "ak_trading.v2.export.ExportRequestV2",
        "ak_trading.v2.macro.refresh_macro_cache",
        "ak_trading.v2.macro.derive_macro_tags",
        "ak_trading.v2.macro.EconTransport.call_tool",
        "ak_trading.v2.macro.MacroCache.__init__",
        "ak_trading.v2.macro.MacroCache.append",
        "ak_trading.v2.macro.MacroCache.snapshot",
        "ak_trading.v2.policy.confidence_policy",
        "ak_trading.v2.policy.holding_policy",
        "ak_trading.v2.policy.stress_policy",
        "ak_trading.v2.policy.calculate_stress_loss",
        "ak_trading.v2.policy.research_policy",
    ],
    "M2": [
        "ak_trading.v2.seasonality.SeasonalityModel",
        "ak_trading.v2.seasonality.SeasonalBucket",
        "ak_trading.v2.seasonality.fit_seasonality",
        "ak_trading.v2.seasonality.seasonal_values",
        "ak_trading.v2.seasonality.SeasonalityModel.content_hash",
        "ak_trading.v2.factors.compute_factor",
        "ak_trading.v2.factors.build_feature_snapshot",
        "ak_trading.v2.baseline.baseline_values_v2",
        "ak_trading.v2.baseline.legacy_daily_baseline",
        "ak_trading.v2.factor_specs.baseline_registry_v2",
    ],
    "M3A": [
        "ak_trading.v2.probability.forecast.publish_market_forecast",
        "ak_trading.v2.probability.models.ForecastModelSet.content_hash",
        "ak_trading.v2.probability.regimes.RegimeReference.content_hash",
        "ak_trading.v2.probability.calibration.build_calibration_evidence",
        "ak_trading.v2.probability.models.fit_forecast_models",
    ],
    "M3B": [
        "ak_trading.v2.strategies.trend",
        "ak_trading.v2.strategies.breakout",
        "ak_trading.v2.strategies.pullback",
        "ak_trading.v2.strategies.reversion",
        "ak_trading.v2.strategies.grid",
        "ak_trading.v2.strategies.martingale",
    ],
    "M3C": [
        *M3C_PURE_FUNCTION_DECLARATIONS,
        "ak_trading.v2.contracts.ExitSpec",
        "ak_trading.v2.contracts.ExitFeatureSpec",
        "ak_trading.v2.contracts.ExitFeatureValue",
        "ak_trading.v2.contracts.ExitFeatureSnapshot",
        "ak_trading.v2.contracts.ExitPolicyBinding",
        "ak_trading.v2.contracts.ExitContext",
        "ak_trading.v2.contracts.ExitProposal",
        "ak_trading.v2.contracts.ExitProtectionRecord",
        "ak_trading.v2.contracts.ExitState",
        "ak_trading.v2.contracts.ExitExecutionLink",
        "ak_trading.v2.contracts.ExitConfirmation",
        "ak_trading.v2.contracts.ContinuationEvidence",
    ],
    "M4": [
        "ak_trading.v2.decision.pipeline.build_strategy_context",
        "ak_trading.v2.decision.pipeline.model_hashes_for",
        "ak_trading.v2.decision.pipeline.registry_hash_for",
        "ak_trading.v2.decision.gates.evaluate_gate",
        "ak_trading.v2.decision.risk.enforce_account_risk",
        "ak_trading.v2.decision.ledger.ContinuousLedger.apply_fill",
        "ak_trading.v2.decision.ledger.ContinuousLedger.mark",
        "ak_trading.v2.decision.ledger.ContinuousLedger.checkpoint",
        "ak_trading.v2.decision.payoffs.PathPayoffModel.content_hash",
        "ak_trading.v2.decision.payoffs.estimate_proposal_payoff",
        # E-P1 §2.1/§2.4 measurement-path producer entry points.
        "ak_trading.v2.decision.payoff_paths.replay_payoff_training_window",
        "ak_trading.v2.decision.payoffs.observation_from_replay",
        "ak_trading.v2.decision.payoffs.payoff_observation_source",
    ],
    "M5": [
        "ak_trading.v2.validation.metrics.evaluate_replay",
        "ak_trading.v2.validation.shadow.run_shadow",
        "ak_trading.v2.validation.shadow.ModelActivationRequest",
        # E-P1 §2.2 payoff_training_plan_sha256 strict registration fields.
        "ak_trading.v2.validation.experiments.ExperimentSpec.payoff_training_plan_sha256",
        "ak_trading.v2.validation.experiments.research_config.payoff_training_plan_sha256",
    ],
}


def _declaration_ownership_with_files() -> dict[str, list[str]]:
    """The full expected ownership table: files stay with their own nodes;
    declarations bind each cross-node interface to its owner (007 §9.2 adds
    the M3C exit-library owner)."""
    return {
        "M1": [*PUBLIC_CODE_FILES, *PUBLIC_TEST_FILES, *FIXTURE_FILES,
               *DECLARATION_OWNERSHIP["M1"]],
        "M2": DECLARATION_OWNERSHIP["M2"],
        "M3A": DECLARATION_OWNERSHIP["M3A"],
        "M3B": DECLARATION_OWNERSHIP["M3B"],
        "M3C": DECLARATION_OWNERSHIP["M3C"],
        "M4": DECLARATION_OWNERSHIP["M4"],
        "M5": DECLARATION_OWNERSHIP["M5"],
    }


def _declaration_ownership() -> dict[str, list[str]]:
    """Node ownership of the frozen cross-node declarations (files stay with
    their own nodes; this table binds each declaration to its owner)."""
    return {node: list(keys) for node, keys in DECLARATION_OWNERSHIP.items()}


def build_contract_lock(project_root: Path, reviewed_revision: str,
                        reviewer_report_sha256: str) -> dict[str, Any]:
    """Build (never publish) the contract lock over the current tree."""
    if not isinstance(project_root, Path):
        raise TypeError("project_root_must_be_Path")
    if not isinstance(reviewed_revision, str) or not reviewed_revision:
        raise ValueError("reviewed_revision_must_be_non_empty_str")
    if not _is_hex_hash(reviewer_report_sha256):
        raise ValueError("reviewer_report_sha256_must_be_64_lowercase_hex")
    signatures = _public_signature_map()
    required_tests = _required_tests_map(project_root)
    required_checks = _required_check_id_map()
    _verify_check_id_references(required_checks, required_tests)
    return {
        "schema_version": CONTRACT_LOCK_SCHEMA_VERSION,
        "contract_version": CONTRACT_VERSION,
        "document_version": DOCUMENT_VERSION,
        "public_files": _hash_files(project_root, PUBLIC_CODE_FILES),
        "public_signatures": {
            name: sha256_hex(signature.encode("utf-8")) for name, signature in signatures.items()
        },
        "schemas": _schema_hash_map(),
        "fixture_files": _fixture_hashes(project_root),
        "ownership": {
            "M1": [*PUBLIC_CODE_FILES, *PUBLIC_TEST_FILES, *FIXTURE_FILES,
                   *DECLARATION_OWNERSHIP["M1"]],
            "M2": DECLARATION_OWNERSHIP["M2"],
            "M3A": DECLARATION_OWNERSHIP["M3A"],
            "M3B": DECLARATION_OWNERSHIP["M3B"],
            "M3C": DECLARATION_OWNERSHIP["M3C"],
            "M4": DECLARATION_OWNERSHIP["M4"],
            "M5": DECLARATION_OWNERSHIP["M5"],
        },
        "required_tests": required_tests,
        "required_check_ids": required_checks,
        "policy": dict(research_policy()),
        "reviewed_revision": reviewed_revision,
        "reviewer_report_sha256": reviewer_report_sha256,
    }


def verify_contract_lock(path: Path, project_root: Path) -> None:
    """Verify a lock file against the live tree; raise ``ArtifactsError`` on drift."""
    if not isinstance(path, Path) or not isinstance(project_root, Path):
        raise TypeError("path_and_project_root_must_be_Path")
    try:
        document = parse_json_bytes(path.read_bytes())
    except OSError:
        raise ArtifactsError("lock_file_not_readable") from None
    except SchemaError as exc:
        raise ArtifactsError(f"lock_file_invalid_json:{exc}") from None
    if not isinstance(document, dict) or set(document.keys()) != _LOCK_KEYS:
        raise ArtifactsError("lock_root_fields_mismatch")
    if document["schema_version"] != CONTRACT_LOCK_SCHEMA_VERSION:
        raise ArtifactsError("lock_schema_version_mismatch")
    if document["contract_version"] != CONTRACT_VERSION:
        raise ArtifactsError("lock_contract_version_mismatch")
    if document["document_version"] != DOCUMENT_VERSION:
        raise ArtifactsError("lock_document_version_mismatch")
    if not isinstance(document["reviewed_revision"], str) or not document["reviewed_revision"]:
        raise ArtifactsError("reviewed_revision_must_be_non_empty_str")
    if not _is_hex_hash(document["reviewer_report_sha256"]):
        raise ArtifactsError("reviewer_report_sha256_must_be_64_hex")
    for field, expected in (("public_files", _hash_files(project_root, PUBLIC_CODE_FILES)),
                            ("fixture_files", _fixture_hashes(project_root))):
        actual = document[field]
        if not isinstance(actual, dict) or any(not _is_hex_hash(v) for v in actual.values()):
            raise ArtifactsError(f"{field}_malformed")
        if actual != expected:
            raise ArtifactsError(f"{field}_hash_mismatch")
    expected_signatures = {
        name: sha256_hex(signature.encode("utf-8"))
        for name, signature in _public_signature_map().items()
    }
    if document["public_signatures"] != expected_signatures:
        raise ArtifactsError("public_signatures_mismatch")
    if document["schemas"] != _schema_hash_map():
        raise ArtifactsError("schemas_mismatch")
    ownership = document["ownership"]
    if not isinstance(ownership, dict):
        raise ArtifactsError("ownership_must_be_dict")
    expected_ownership = _declaration_ownership_with_files()
    if {node: sorted(keys) for node, keys in ownership.items()} !=             {node: sorted(keys) for node, keys in expected_ownership.items()}:
        raise ArtifactsError("ownership_table_mismatch")
    for name in ownership["M1"]:
        if name in DECLARATION_OWNERSHIP["M1"]:
            continue
        if not (project_root / name).is_file():
            raise ArtifactsError(f"owned_file_missing:{name}")
    required_tests = _required_tests_map(project_root)
    if document["required_tests"] != required_tests:
        raise ArtifactsError("required_tests_mismatch")
    if document["required_check_ids"] != _required_check_id_map():
        raise ArtifactsError("required_check_ids_mismatch")
    _verify_check_id_references(document["required_check_ids"], required_tests)
    if document["policy"] != dict(research_policy()):
        raise ArtifactsError("policy_mismatch")


def verify_reviewer_report_binding(document: Mapping[str, Any], report_path: Path) -> str:
    """Quick-check H(R) wiring (§5.3 item 5): hash the independent reviewer
    report's actual bytes and require equality with the lock's
    ``reviewer_report_sha256``. Returns H(R). No new lock root field is
    introduced for this; the report's own content/schema belongs to the
    review work order (§7.3).
    """
    if not isinstance(document, Mapping):
        raise TypeError("document_must_be_mapping")
    if not isinstance(report_path, Path):
        raise TypeError("report_path_must_be_Path")
    cited = document.get("reviewer_report_sha256")
    if not _is_hex_hash(cited):
        raise ArtifactsError("reviewer_report_sha256_must_be_64_hex")
    try:
        payload = report_path.read_bytes()
    except OSError:
        raise ArtifactsError("reviewer_report_not_readable") from None
    actual = sha256_hex(payload)
    if actual != cited:
        raise ArtifactsError("reviewer_report_sha256_mismatch")
    return actual


def verify_evidence_manifest_binding(cited_sha256: str, evidence_manifest_path: Path) -> str:
    """Quick-check H_E wiring (§5.1/§5.3): a hash cited by a report as an
    evidence-package identity must equal the SHA-256 of that package's
    ``evidence-manifest.json`` actual bytes. The evidence manifest itself is
    never recursively self-hashed. Returns H_E.
    """
    if not _is_hex_hash(cited_sha256):
        raise ArtifactsError("cited_evidence_manifest_sha256_must_be_64_hex")
    if not isinstance(evidence_manifest_path, Path):
        raise TypeError("evidence_manifest_path_must_be_Path")
    try:
        payload = evidence_manifest_path.read_bytes()
    except OSError:
        raise ArtifactsError("evidence_manifest_not_readable") from None
    actual = sha256_hex(payload)
    if actual != cited_sha256:
        raise ArtifactsError("evidence_manifest_sha256_mismatch")
    return actual


def verify_contract_lock_with_report(path: Path, project_root: Path,
                                     report_path: Path) -> None:
    """Published-state quick check (§5.3 item 5): the full real lock verifier
    — schema/document versions, public files, signatures, schemas,
    fixture_files with H_F and the recursively verified attachment table,
    ownership, required tests/check ids, policy — plus the H(R) report-byte
    binding. R-side per-file hash checking, H_E bindings for each cited
    evidence package (via ``verify_evidence_manifest_binding``) and the
    recomputation of source/semantics remain the reviewer's own obligations;
    this composite never replaces the cold-chain runs (§3.1).
    """
    if not isinstance(path, Path) or not isinstance(project_root, Path):
        raise TypeError("path_and_project_root_must_be_Path")
    verify_contract_lock(path, project_root)
    try:
        document = parse_json_bytes(path.read_bytes())
    except OSError:
        raise ArtifactsError("lock_file_not_readable") from None
    except SchemaError as exc:
        raise ArtifactsError(f"lock_file_invalid_json:{exc}") from None
    verify_reviewer_report_binding(document, report_path)


def _parse_strict(payload: bytes, name: str) -> Any:
    try:
        return parse_json_bytes(payload)
    except SchemaError as exc:
        raise ArtifactsError(f"{name}_parse_failed:{exc}") from exc


def verify_qualification_evidence(qualification: StrategyQualification,
                                   spec: StrategySpec,
                                   evidence: Mapping[str, bytes],
                                   contract_lock_sha256: str,
                                   registry_sha256: str, *,
                                   exit_registry_sha256: str,
                                   exit_binding: ExitPolicyBinding) -> None:
    """Verify the publication evidence chain of one strategy qualification.

    ``evidence`` maps content SHA-256 to original bytes and must contain the
    referenced metrics.json, qualification_review.json and experiment.json.
    Files are re-hashed, strictly parsed against the public M5 schemas and
    cross-checked: review scope/verdict/independence/fixed checks, report/
    lock/registry/experiment hash consistency, the pre-registered
    required_group_keys full set (experiment == metrics == review, no dropped
    groups), policy versions, source versions vs the spec, the model_hashes
    bundle, and this qualification's own scope (strategy/product/mode/
    macro_mode over its declared horizons, bounded by the contract whitelist):
    every registered scope key must PASS under the bundle and each horizon
    must pass all six fixed cost scenarios. Registered groups of other scopes
    stay recorded with their true status — they neither block this
    qualification nor authorize it. This only gates release qualification;
    raw statistics are recomputed by the independent reviewer.

    007 §5.4: ``exit_registry_sha256`` and ``exit_binding`` are required
    keyword-only inputs; the composite entry×exit identity must match the
    qualification and the bound evidence byte-for-byte.
    """
    from ak_trading.v2.contracts import Horizon, StrategyQualification, StrategySpec

    if not isinstance(qualification, StrategyQualification):
        raise TypeError("qualification_must_be_StrategyQualification")
    if not isinstance(spec, StrategySpec):
        raise TypeError("spec_must_be_StrategySpec")
    if not isinstance(evidence, Mapping):
        raise TypeError("evidence_must_be_mapping")
    if not _is_hex_hash(contract_lock_sha256) or not _is_hex_hash(registry_sha256):
        raise ValueError("hashes_must_be_64_lowercase_hex")
    if not _is_hex_hash(exit_registry_sha256):
        raise ValueError("exit_registry_sha256_must_be_64_lowercase_hex")
    if not isinstance(exit_binding, ExitPolicyBinding):
        raise TypeError("exit_binding_must_be_ExitPolicyBinding")
    if qualification.exit_registry_sha256 != exit_registry_sha256:
        raise ArtifactsError("qualification_exit_registry_mismatch")
    if qualification.exit_binding_sha256 != exit_binding.binding_sha256:
        raise ArtifactsError("qualification_exit_binding_mismatch")
    if list(qualification.exit_spec_sha256s) != list(exit_binding.exit_spec_sha256s):
        raise ArtifactsError("qualification_exit_spec_set_mismatch")
    if exit_binding.entry_spec_sha256 != spec.code_sha256:
        raise ArtifactsError("exit_binding_entry_spec_mismatch")
    for key, payload in evidence.items():
        if not _is_hex_hash(key):
            raise ArtifactsError("evidence_key_must_be_64_hex")
        if not isinstance(payload, (bytes, bytearray)):
            raise ArtifactsError("evidence_value_must_be_bytes")
        if sha256_hex(bytes(payload)) != key:
            raise ArtifactsError("evidence_hash_mismatch")
    required_files = (qualification.report_sha256, qualification.review_sha256,
                      qualification.experiment_sha256)
    for required in required_files:
        if required not in evidence:
            raise ArtifactsError("required_evidence_file_missing")
    metrics = _parse_strict(bytes(evidence[qualification.report_sha256]), "metrics")
    review = _parse_strict(bytes(evidence[qualification.review_sha256]), "review")
    experiment = _parse_strict(bytes(evidence[qualification.experiment_sha256]), "experiment")

    if not isinstance(metrics, dict) or set(metrics.keys()) != _METRICS_ROOT_KEYS:
        raise ArtifactsError("metrics_root_fields_mismatch")
    if metrics["schema_version"] != _METRICS_SCHEMA_ID:
        raise ArtifactsError("metrics_schema_version_mismatch")
    # 变更申请-004 §2/§7: replay_summaries is required (empty list allowed when
    # no replay) and each item carries the exact fixed projection fields.
    replay_summaries = metrics["replay_summaries"]
    if not isinstance(replay_summaries, list):
        raise ArtifactsError("replay_summaries_must_be_list")
    for summary in replay_summaries:
        if not isinstance(summary, dict) or set(summary.keys()) != _REPLAY_SUMMARY_KEYS:
            raise ArtifactsError("replay_summary_fields_mismatch")
        if summary["status"] not in _REPLAY_SUMMARY_STATUSES:
            raise ArtifactsError("replay_summary_status_invalid")
        for field in ("initial_equity_cny", "terminal_equity_cny",
                      "closed_episode_net_pnl_cny", "open_episode_realized_net_pnl_cny",
                      "terminal_unrealized_cny"):
            if summary[field] is not None and not isinstance(summary[field], str):
                raise ArtifactsError(f"replay_summary_{field}_must_be_str_or_null")
        for field in ("account_id", "reconciliation_status", "split_id",
                      "run_policy_sha256", "model_bundle_sha256"):
            if not isinstance(summary[field], str):
                raise ArtifactsError(f"replay_summary_{field}_must_be_str")
        for field in ("open_episode_ids", "pending_intent_ids", "reasons"):
            if not isinstance(summary[field], list) or any(
                    not isinstance(item, str) for item in summary[field]):
                raise ArtifactsError(f"replay_summary_{field}_must_be_list_str")
    if not isinstance(review, dict) or set(review.keys()) != _REVIEW_ROOT_KEYS:
        raise ArtifactsError("review_root_fields_mismatch")
    if review["schema_version"] != _REVIEW_SCHEMA_ID:
        raise ArtifactsError("review_schema_version_mismatch")
    if review["scope"] != "OFFLINE_STATISTICS":
        raise ArtifactsError("review_scope_must_be_OFFLINE_STATISTICS")
    if review["verdict"] != "PASS":
        raise ArtifactsError("review_verdict_must_be_PASS")
    if review["independent"] is not True:
        raise ArtifactsError("review_must_be_independent")
    checks = review["checks"]
    if not isinstance(checks, dict) or set(checks.keys()) != set(_REVIEW_CHECK_KEYS):
        raise ArtifactsError("review_checks_keys_mismatch")
    if any(value != "PASS" for value in checks.values()):
        raise ArtifactsError("review_checks_must_all_be_PASS")
    if review["report_sha256"] != qualification.report_sha256:
        raise ArtifactsError("report_sha256_inconsistent")
    if review["contract_lock_sha256"] != contract_lock_sha256:
        raise ArtifactsError("contract_lock_sha256_inconsistent")
    if review["registry_sha256"] != registry_sha256:
        raise ArtifactsError("registry_sha256_inconsistent")
    if review["experiment_sha256"] != qualification.experiment_sha256:
        raise ArtifactsError("experiment_sha256_inconsistent")
    if review["model_hashes"] != dict(qualification.model_hashes):
        raise ArtifactsError("review_model_hashes_mismatch")
    if (review["research_policy_version"] != qualification.research_policy_version
            or review["macro_policy_version"] != qualification.macro_policy_version):
        raise ArtifactsError("review_policy_versions_mismatch")
    if tuple(review["cost_scenarios"]) != tuple(qualification.cost_scenarios):
        raise ArtifactsError("review_cost_scenarios_mismatch")
    if (metrics["research_policy_version"] != qualification.research_policy_version
            or metrics["macro_policy_version"] != qualification.macro_policy_version):
        raise ArtifactsError("metrics_policy_versions_mismatch")
    if tuple(metrics["cost_scenarios"]) != tuple(qualification.cost_scenarios):
        raise ArtifactsError("metrics_cost_scenarios_mismatch")

    if not isinstance(experiment, dict) or set(experiment.keys()) != {"schema_version", "spec"}:
        raise ArtifactsError("experiment_root_fields_mismatch")
    if experiment["schema_version"] != _EXPERIMENT_SCHEMA_ID:
        raise ArtifactsError("experiment_schema_version_mismatch")
    experiment_spec = experiment["spec"]
    if not isinstance(experiment_spec, dict) or set(experiment_spec.keys()) != _EXPERIMENT_SPEC_KEYS:
        raise ArtifactsError("experiment_spec_fields_mismatch")
    # ExperimentSpec.source_sha256/parameter_sha256 are experiment-level
    # identities covering the whole cross-strategy experiment (06 §ExperimentSpec
    # and experiment-registry); they are NOT any single strategy's code hash.
    # M1 checks their structural form and the policy binding; the independent
    # reviewer re-derives their content from the registered experiment.
    for field in ("source_sha256", "parameter_sha256"):
        if not _is_hex_hash(experiment_spec[field]):
            raise ArtifactsError(f"experiment_{field}_must_be_64_hex")
    if (experiment_spec["research_policy_version"] != qualification.research_policy_version
            or experiment_spec["macro_policy_version"] != qualification.macro_policy_version):
        raise ArtifactsError("experiment_policy_versions_mismatch")

    required_keys = review["required_group_keys"]
    if not isinstance(required_keys, list) or any(not isinstance(k, str) for k in required_keys):
        raise ArtifactsError("required_group_keys_must_be_list_str")
    if sorted(required_keys) != sorted(metrics["required_group_keys"]):
        raise ArtifactsError("metrics_required_group_keys_mismatch")
    if sorted(required_keys) != sorted(experiment_spec["required_group_keys"]):
        raise ArtifactsError("experiment_required_group_keys_mismatch")

    model_bundle_sha256 = sha256_hex(canonical_json_bytes(dict(qualification.model_hashes)))
    bundles = metrics["model_bundles"]
    if not isinstance(bundles, dict) or bundles.get(model_bundle_sha256) != dict(qualification.model_hashes):
        raise ArtifactsError("model_bundle_mismatch")

    forecast_groups = metrics["forecast_groups"]
    strategy_groups = metrics["strategy_groups"]
    for groups, expected_fields, label in (
        (forecast_groups, _FORECAST_GROUP_KEYS, "forecast_groups"),
        (strategy_groups, _STRATEGY_GROUP_KEYS, "strategy_groups"),
    ):
        if not isinstance(groups, list) or any(
            not isinstance(group, dict) or set(group.keys()) != expected_fields for group in groups
        ):
            raise ArtifactsError(f"{label}_shape_mismatch")
    forecast_by_key = {group["group_key"]: group for group in forecast_groups}
    strategy_by_key = {group["group_key"]: group for group in strategy_groups}
    # Anti-dropping: every registered key must exist in its metrics table with
    # its true status (n=0/INSUFFICIENT/FAIL stay recorded, never deleted).
    # Only THIS qualification's scope has to PASS, checked below.
    for key in required_keys:
        try:
            parsed = parse_json_bytes(key.encode("utf-8"))
        except SchemaError as exc:
            raise ArtifactsError(f"required_group_key_not_canonical_json:{exc}") from exc
        if not isinstance(parsed, list) or not parsed or not isinstance(parsed[0], str):
            raise ArtifactsError("required_group_key_must_be_json_array")
        table = forecast_by_key if parsed[0] == "FORECAST" else strategy_by_key
        if table.get(key) is None:
            raise ArtifactsError(f"required_group_missing:{key}")

    if (qualification.strategy_id != spec.strategy_id
            or qualification.strategy_version != spec.version
            or qualification.parameter_version != spec.parameter_version
            or qualification.code_sha256 != spec.code_sha256
            or qualification.mode != spec.mode
            or spec.max_holding not in qualification.horizons):
        raise ArtifactsError("qualification_spec_mismatch")
    for horizon in qualification.horizons:
        if not isinstance(horizon, Horizon):
            raise ArtifactsError("qualification_horizons_must_be_Horizon")

    # Scope selection (06 §6.1): qualifications issue per
    # strategy/product/mode/macro_mode over the horizons they actually passed,
    # bounded by the registered contract whitelist. Registered keys of other
    # strategies/products/modes/macro modes/horizons/contracts belong to other
    # scopes: they stay recorded but neither block nor authorize this one.
    scope_keys: list[str] = []
    for key in required_keys:
        try:
            parsed = parse_json_bytes(key.encode("utf-8"))
        except SchemaError:
            raise ArtifactsError("required_group_key_not_canonical_json") from None
        if (not isinstance(parsed, list) or len(parsed) != 11
                or parsed[0] != "STRATEGY"):
            continue
        (_, _split_id, _run_policy_hash, key_strategy, key_product, key_contract,
         key_mode, key_horizon_unit, key_horizon_value, key_macro, _scenario) = parsed
        if key_strategy != qualification.strategy_id:
            continue
        if str(key_product).upper() != qualification.product.upper():
            continue
        if key_mode != qualification.mode or key_macro != qualification.macro_mode:
            continue
        if key_contract is not None and key_contract not in qualification.contract_ids:
            continue
        if Horizon(str(key_horizon_unit), int(key_horizon_value)) not in qualification.horizons:
            continue
        scope_keys.append(key)
    if not scope_keys:
        raise ArtifactsError("qualification_scope_not_registered")

    # Every registered key of THIS scope must PASS under this model bundle;
    # exact-key matching means a PASS group of another scope can never
    # substitute (cross-scope theft rejected).
    for key in scope_keys:
        group = strategy_by_key.get(key)
        if group is None:
            raise ArtifactsError(f"scope_group_missing:{key}")
        if group["status"] != "PASS":
            raise ArtifactsError(f"scope_group_not_passing:{key}")
        if group["model_bundle_sha256"] != model_bundle_sha256:
            raise ArtifactsError(f"scope_group_model_bundle_mismatch:{key}")

    # F04-1: a PASS economic scope must come from THIS strategy's own FIXED
    # counterfactual account (routing FIXED, fixed_strategy_id bound to the
    # qualified strategy); BEST_EV samples or foreign fixed ids never back it.
    # F04(r3): every scope group must ALSO have a COMPLETE replay_summary
    # matching ALL FOUR axes (account_id, split_id, run_policy_sha256,
    # model_bundle_sha256); existence is required — no match at all, or any
    # INCOMPLETE match, is rejected.
    for key in scope_keys:
        group = strategy_by_key[key]
        if group["routing_mode"] != "FIXED":
            raise ArtifactsError(f"scope_group_routing_must_be_fixed:{key}")
        if group["fixed_strategy_id"] != qualification.strategy_id:
            raise ArtifactsError(f"scope_group_fixed_strategy_mismatch:{key}")
        has_complete_match = False
        for summary in replay_summaries:
            same_all_axes = (summary["account_id"] == group["account_id"]
                             and summary["split_id"] == group["split_id"]
                             and summary["run_policy_sha256"] == group["run_policy_sha256"]
                             and summary["model_bundle_sha256"] == group["model_bundle_sha256"])
            if not same_all_axes:
                continue
            if summary["status"] != "COMPLETE":
                raise ArtifactsError(
                    f"matching_incomplete_replay_cannot_back_pass:{key}")
            has_complete_match = True
        if not has_complete_match:
            raise ArtifactsError(
                f"scope_group_requires_complete_same_account_replay:{key}")

    # F04-2: every group body must reconstruct its registered key fields;
    # a body that silently moves to another contract/split/policy is rejected
    # (field-wise comparison, whitespace-agnostic over the canonical array).
    for group in strategy_groups:
        try:
            registered = json.loads(group["group_key"])
        except ValueError:
            raise ArtifactsError(
                f"group_key_not_json:{group['group_key']}") from None
        body = [group["split_id"], group["run_policy_sha256"],
                group["strategy_id"], group["product"], group["contract_id"],
                group["mode"], group["horizon_unit"], group["horizon_value"],
                group["macro_mode"], group["cost_scenario"]]
        if (not isinstance(registered, list) or len(registered) != 11
                or registered[0] != "STRATEGY"
                or registered[1:] != body):
            raise ArtifactsError(
                f"group_body_does_not_reconstruct_key:{group['group_key']}")

    # F04-3: experiment, review and the caller argument must agree on the
    # contract lock identity the evidence chain was produced under.
    if experiment_spec["contract_lock_sha256"] != review["contract_lock_sha256"]:
        raise ArtifactsError("experiment_contract_lock_mismatch_with_review")
    if experiment_spec["contract_lock_sha256"] != contract_lock_sha256:
        raise ArtifactsError("experiment_contract_lock_mismatch_with_argument")

    # Each qualified horizon must pass ALL six fixed cost scenarios under this
    # model bundle; picking the best scenario is not allowed (06 §6.1).
    for horizon in qualification.horizons:
        for scenario in _SIX_COST_SCENARIOS:
            covered = any(
                group["status"] == "PASS"
                and group["model_bundle_sha256"] == model_bundle_sha256
                and group["strategy_id"] == qualification.strategy_id
                and group["product"].upper() == qualification.product.upper()
                and group["mode"] == qualification.mode
                and group["horizon_unit"] == horizon.unit
                and group["horizon_value"] == horizon.value
                and group["macro_mode"] == qualification.macro_mode
                and group["cost_scenario"] == scenario
                for group in strategy_groups
            )
            if not covered:
                raise ArtifactsError(
                    f"qualification_horizon_scenario_not_passing:"
                    f"{horizon.unit}:{horizon.value}:{scenario}")
