"""NF30 risk-metric scope & provenance sidecar (C30 contract ``ak_v2_risk_semantics_v1``).

Pure read-only mapping over existing M4/M5 producer artifacts plus sidecar
validation.  This module NEVER recomputes risk, never mutates producer
artifacts, never touches the frozen risk_report/metrics schemas or any lock,
and issues no statistical PASS / SHADOW_ACCEPTED / trading qualification.

Frozen distinctions implemented here (work order NF30, C30 table):

* per-strategy closed-episode PnL/EV/tail loss (normal cost scenarios)
  -> ``STRATEGY_CLOSED_EPISODES`` / ``CLOSED_EPISODE_CVAR``
* RunPolicy account equity drawdown & daily tail loss
  -> ``RUN_POLICY_ACCOUNT_EQUITY`` / ``ACCOUNT_DAILY_EQUITY_CVAR``
* admission-candidate model tail loss -> ``PROPOSAL_MODEL`` /
  ``PROPOSAL_MODEL_CVAR`` (producer max aggregation preserved)
* margin usage -> ``RUN_POLICY_ACCOUNT`` with ``ACTUAL_MARGIN_ONLY`` vs
  ``ACTUAL_PLUS_RESERVED_MARGIN`` (never auto-merged)
* GAP/NO_STOP stress rows -> ``RUN_POLICY_BASE_COHORT_STRESS``; on those
  rows ``peak_margin_cny``/``margin_shortfall_cny`` carry ONLY stress-path
  bases (never the normal-row account bases), ``max_drawdown_ratio``/
  ``longest_recovery_days`` keep the producer placeholder seeds
  (``PLACEHOLDER_SEED_NOT_EVALUATED``), and ``max_inventory`` is copied
  from the BASE group row by the producer copy loop
* maximum_inventory / max_inventory -> ``ACCOUNT_GROSS_LOTS_MAX`` (lots)

Unknown producer versions or sources are rejected (non-zero exit); they never
silently inherit this worktree's explanation.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Mapping

from ak_trading.v2.schemas import canonical_json_bytes, parse_json_bytes

SIDECAR_SCHEMA_VERSION = "ak_v2_risk_semantics_v1"
METRICS_SCHEMA_VERSION = "ak_probability_metrics_v2.1.4"
RISK_REPORT_SCHEMA_VERSION = "ak_probability_risk_report_v2.1.0"
CATALOG_ID = "c30-ak-v2-risk-semantics-v1"

NORMAL_COST_SCENARIOS = ("BASE", "FEE_X2", "SLIPPAGE_X2", "MARGIN_X1_5")
STRESS_COST_SCENARIOS = ("GAP", "NO_STOP")

# ---------------------------------------------------------------------------
# Frozen producer identity (C30: producer_identity binds artifact schema
# versions + producer release + source manifest + relevant source hashes).
# Provenance kind is WORKTREE_SNAPSHOT: this is NOT a claim that these bytes
# are the finally released producer build; callers binding real releases must
# re-freeze an independently verified catalog (see limitations[0]).
# ---------------------------------------------------------------------------
FROZEN_RELEVANT_SOURCE_HASHES: dict[str, str] = {
    "src/ak_trading/v2/validation/metrics.py":
        "36b7b84610b23175dbb75bf3c869d068b6f1e576cef05ebac8253e09acbad7f6",
    "src/ak_trading/v2/validation/experiments.py":
        "700294c9a7f6492c838b2435b475d57e40710c931a8fa2f4bcfb60b23cf6917b",
    "src/ak_trading/v2/validation/reports.py":
        "0d28dbeb67baf91e3d128b53b22fe462f40258cbb3f58b3e20074112af573fa5",
    "src/ak_trading/v2/contracts.py":
        "bcd290615ea837dd086c4bb4166ed6bd2dc474a410da8501c7040474b1860f15",
    "src/ak_trading/v2/decision/replay.py":
        "f196a593270d6a6fc14389fb21fd6f5d856d31f60641da2045e963fb9de46528",
    "src/ak_trading/v2/decision/execution.py":
        "f06be6642c07e8698d41ebf02c30acb39a3976faf9571c9ad7bf408e559772aa",
}

SOURCE_MANIFEST: list[dict[str, str]] = [
    {"logical_module": "M5-validation-metrics", "role": "account/episode risk metrics",
     "source_relative_path": "src/ak_trading/v2/validation/metrics.py"},
    {"logical_module": "M5-validation-experiments", "role": "strategy group scoring + stress cohort",
     "source_relative_path": "src/ak_trading/v2/validation/experiments.py"},
    {"logical_module": "M5-validation-reports", "role": "metrics schema + readiness + qualification",
     "source_relative_path": "src/ak_trading/v2/validation/reports.py"},
    {"logical_module": "v2-contracts", "role": "RunPolicy/record contracts",
     "source_relative_path": "src/ak_trading/v2/contracts.py"},
    {"logical_module": "M4-decision-replay", "role": "risk_report producer",
     "source_relative_path": "src/ak_trading/v2/decision/replay.py"},
    {"logical_module": "M4-decision-execution", "role": "stress path simulation",
     "source_relative_path": "src/ak_trading/v2/decision/execution.py"},
]

FROZEN_DISTINCTIONS: list[dict[str, str]] = [
    {"scope_kind": "STRATEGY_CLOSED_EPISODES", "basis": "CLOSED_EPISODE_CVAR",
     "applies_to": "普通成本场景的单策略完整轮损益、EV、尾损失",
     "denominator_rule": "完整 episode 数、对应日期块；未闭合仓不混入"},
    {"scope_kind": "RUN_POLICY_ACCOUNT_EQUITY", "basis": "ACCOUNT_DAILY_EQUITY_CVAR",
     "applies_to": "RunPolicy 账户权益回撤与日损益尾损失",
     "denominator_rule": "该账户有效日权益样本；不能分摊给其中各策略"},
    {"scope_kind": "PROPOSAL_MODEL", "basis": "PROPOSAL_MODEL_CVAR",
     "applies_to": "准入候选的模型尾损失",
     "denominator_rule": "提案模型估计（保留生产者 max 聚合说明），不是已实现的损失样本"},
    {"scope_kind": "RUN_POLICY_ACCOUNT", "basis": "ACTUAL_MARGIN_ONLY / ACTUAL_PLUS_RESERVED_MARGIN",
     "applies_to": "保证金占用",
     "denominator_rule": "显示实际 producer 定义；不同 basis 不得自动合并"},
    {"scope_kind": "RUN_POLICY_BASE_COHORT_STRESS", "basis": "BASE_CLOSED_EPISODE_STRESS_COHORT",
     "applies_to": "GAP/NO_STOP 压力行的损益、短缺、峰值保证金、最大压力损失比",
     "denominator_rule": "BASE 完整轮构造的整账户压力 cohort；不能套普通行按 strategy_id 过滤的口径"},
    {"scope_kind": "RUN_POLICY_BASE_COHORT_STRESS", "basis": "PLACEHOLDER_SEED_NOT_EVALUATED",
     "applies_to": "GAP/NO_STOP 压力行的 max_drawdown_ratio / longest_recovery_days",
     "denominator_rule": "生产者不对压力行评估该两字段（复制循环只复制 5 键、g.update(stress) "
                         "不写它们、评分循环对压力行不执行）；值保持 reports.py 占位种子 "
                         "0.0/null，不代表实测零回撤或恢复期结论"},
    {"scope_kind": "ACCOUNT_GROSS_LOTS_MAX", "basis": "GROSS_LOTS",
     "applies_to": "maximum_inventory / max_inventory",
     "denominator_rule": "单位 lots；不是名义金额或跨品种可比较的风险敞口"},
]

ANCHOR_CHECKS: list[dict[str, Any]] = [
    {"source_relative_path": "src/ak_trading/v2/validation/experiments.py", "line": 1308,
     "anchor": 'rows=[r for r in economic["episode_records"] if r["strategy_id"]==group["strategy_id"]'},
    {"source_relative_path": "src/ak_trading/v2/validation/experiments.py", "line": 1319,
     "anchor": "cvar=total/mass if mass else None"},
    {"source_relative_path": "src/ak_trading/v2/validation/experiments.py", "line": 1331,
     "anchor": 'stress=max((r["stress_loss_ratio"] for r in rows)'},
    {"source_relative_path": "src/ak_trading/v2/validation/experiments.py", "line": 1338,
     "anchor": '"margin_shortfall_cny":economic["margin_shortfall_cny"]'},
    {"source_relative_path": "src/ak_trading/v2/validation/experiments.py", "line": 1262,
     "anchor": 'worst=max((Decimal(r["path"]["loss_cny"])/Decimal(r["budget"]["equity_at_start_cny"])'},
    {"source_relative_path": "src/ak_trading/v2/validation/experiments.py", "line": 1481,
     "anchor": "g.update(stress,model_bundle_sha256=bundle_hash)"},
    {"source_relative_path": "src/ak_trading/v2/validation/experiments.py", "line": 1483,
     "anchor": 'g["net_pnl_cny"]=str(-sum((Decimal(r["path"]["loss_cny"])'},
    {"source_relative_path": "src/ak_trading/v2/validation/experiments.py", "line": 1479,
     "anchor": 'for key in ("episodes","blocks","admission_rate","no_trade_rate","max_inventory")'},
    {"source_relative_path": "src/ak_trading/v2/validation/experiments.py", "line": 1484,
     "anchor": "continue"},
    {"source_relative_path": "src/ak_trading/v2/validation/metrics.py", "line": 317,
     "anchor": "drawdown = max(drawdown, (peak-a.equity_cny)/peak)"},
    {"source_relative_path": "src/ak_trading/v2/validation/metrics.py", "line": 318,
     "anchor": "peak_margin = max(peak_margin, a.margin_cny + a.reserved_margin_cny)"},
    {"source_relative_path": "src/ak_trading/v2/validation/metrics.py", "line": 319,
     "anchor": "max_inventory = max(max_inventory, sum(l.lots for l in a.lots))"},
    {"source_relative_path": "src/ak_trading/v2/validation/metrics.py", "line": 320,
     "anchor": "shortfall = max(shortfall, a.margin_cny+a.reserved_margin_cny-a.equity_cny)"},
    {"source_relative_path": "src/ak_trading/v2/validation/metrics.py", "line": 339,
     "anchor": "losses = sorted((max(Decimal(0), -v) for v in daily_changes)"},
    {"source_relative_path": "src/ak_trading/v2/validation/metrics.py", "line": 341,
     "anchor": "cvar = sum(tail)/len(tail) if tail else None"},
    {"source_relative_path": "src/ak_trading/v2/validation/metrics.py", "line": 357,
     "anchor": "episode_cvar=tail_loss/tail_mass if tail_mass else None"},
    {"source_relative_path": "src/ak_trading/v2/validation/metrics.py", "line": 370,
     "anchor": '"stress_loss_ratio":float(budget.stress_loss_cny/budget.equity_at_start_cny)'},
    {"source_relative_path": "src/ak_trading/v2/validation/metrics.py", "line": 383,
     "anchor": '"longest_recovery_days": None if recovery_start else longest'},
    {"source_relative_path": "src/ak_trading/v2/decision/replay.py", "line": 619,
     "anchor": "cvars=[p.payoff.loss_cvar95_cny"},
    {"source_relative_path": "src/ak_trading/v2/decision/replay.py", "line": 622,
     "anchor": '"peak_margin_cny":str(max((a.margin_cny for a in result.equity)'},
    {"source_relative_path": "src/ak_trading/v2/decision/replay.py", "line": 623,
     "anchor": '"loss_cvar95_cny":str(max(cvars))'},
    {"source_relative_path": "src/ak_trading/v2/decision/replay.py", "line": 608,
     "anchor": "if rules:"},
    {"source_relative_path": "src/ak_trading/v2/validation/reports.py", "line": 194,
     "anchor": 'g["routing_mode"] == "FIXED"'},
    {"source_relative_path": "src/ak_trading/v2/validation/reports.py", "line": 99,
     "anchor": "conservative_ev_cny=None, max_drawdown_ratio=0.0"},
]

PRODUCER_RELEASE_SHA256 = hashlib.sha256(
    canonical_json_bytes(FROZEN_RELEVANT_SOURCE_HASHES)).hexdigest()
SOURCE_MANIFEST_SHA256 = hashlib.sha256(
    canonical_json_bytes(SOURCE_MANIFEST)).hexdigest()
SEMANTICS_CATALOG = {
    "catalog_id": CATALOG_ID,
    "producer_release_sha256": PRODUCER_RELEASE_SHA256,
    "source_manifest_sha256": SOURCE_MANIFEST_SHA256,
    "frozen_distinctions": FROZEN_DISTINCTIONS,
    "anchor_checks": ANCHOR_CHECKS,
}
SEMANTICS_CATALOG_SHA256 = hashlib.sha256(canonical_json_bytes(SEMANTICS_CATALOG)).hexdigest()

# Frozen producer field-shape knowledge (mirrors reports.py STRATEGY_FIELDS /
# replay_summaries schema / replay_payloads risk_report keys; bound by the
# producer source hashes above, so a producer that changes shape is rejected
# by the identity check before these lists can mislead).
METRICS_TOP_LEVEL_REQUIRED = ("schema_version", "strategy_groups", "replay_summaries", "model_bundles")
STRATEGY_GROUP_FIELDS = frozenset(
    "group_key split_id model_bundle_sha256 run_policy_sha256 routing_mode fixed_strategy_id "
    "account_id strategy_id product contract_id mode horizon_unit horizon_value macro_mode "
    "cost_scenario exit_binding_sha256 exit_spec_sha256s fixed_exit_binding_sha256 episodes "
    "blocks admission_rate no_trade_rate net_pnl_cny conservative_ev_cny max_drawdown_ratio "
    "cvar95_cny maximum_stress_loss_ratio margin_shortfall_cny peak_margin_cny max_inventory "
    "longest_recovery_days status reasons".split())
REPLAY_SUMMARY_FIELDS = frozenset(
    "account_id initial_equity_cny terminal_equity_cny closed_episode_net_pnl_cny "
    "open_episode_realized_net_pnl_cny terminal_unrealized_cny open_episode_ids "
    "pending_intent_ids reconciliation_status status reasons split_id run_policy_sha256 "
    "model_bundle_sha256".split())
RISK_REPORT_FIELDS = frozenset(
    "schema_version status maximum_inventory peak_margin_cny loss_cvar95_cny stress_losses "
    "margin_shortfalls blocked_counts open_positions reconciliation".split())
BINDING_FIELDS = ("artifact_sha256", "split_id", "run_policy_sha256", "account_id",
                  "model_bundle_sha256", "cost_scenario")

_ROOT_KEYS = frozenset(
    "schema_version semantics_catalog_sha256 producer_identity inputs artifact_bindings "
    "field_semantics joins limitations mapping_status".split())
_ENTRY_KEYS = frozenset(
    "artifact_sha256 json_pointer record_identity metric_name scope_kind basis population "
    "aggregation unit sample_unit denominator group_or_account_ref producer_identity "
    "producer_rule_id completeness null_semantics source_evidence explanation".split())

# Path-safety: inputs and outputs may never touch these (case-insensitive,
# Windows-aware; boundaries doc 01 §5 / H30.6).
_FORBIDDEN_REPO_DIRS = (
    "data/research_runs", "data/holdouts", "maintenance_artifacts",
    "data/maintenance_artifacts", ".git",
)
_FORBIDDEN_EXTERNAL_ROOTS = ("d:/akf260920",)
_FORBIDDEN_HOME_DIRS = (".zcode", ".codex", ".claude", ".opencode", ".config")
_FORBIDDEN_FILE_NAMES = (".env",)


class RiskSemanticsError(Exception):
    """Rejection; the CLI turns this into a non-zero exit with no success marker."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}:{detail}" if detail else code)
        self.code = code
        self.detail = detail


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------
def _norm_key(path: Path) -> str:
    text = str(path).replace("\\", "/")
    return text.lower().rstrip("/")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _reject_forbidden_root(resolved: Path, role: str, raw: str) -> None:
    key = _norm_key(resolved)
    for forbidden in _FORBIDDEN_EXTERNAL_ROOTS:
        if key == forbidden or key.startswith(forbidden + "/"):
            raise RiskSemanticsError("forbidden_root_rejected", f"{role}:{raw}")
    home = Path.home().resolve()
    home_key = _norm_key(home)
    for part in _FORBIDDEN_HOME_DIRS:
        if key == f"{home_key}/{part}" or key.startswith(f"{home_key}/{part}/"):
            raise RiskSemanticsError("forbidden_root_rejected", f"{role}:{raw}")
    repo = _repo_root()
    for forbidden in _FORBIDDEN_REPO_DIRS:
        forbidden_key = _norm_key(repo / forbidden)
        if key == forbidden_key or key.startswith(forbidden_key + "/"):
            raise RiskSemanticsError("forbidden_root_rejected", f"{role}:{raw}")


def validate_readable_file(raw: str | Path, *, role: str) -> Path:
    """Validate one explicit input path; reject traversal/UNC/forbidden roots."""
    if not isinstance(raw, (str, Path)) or not str(raw).strip():
        raise RiskSemanticsError("invalid_input_path", f"{role}:empty")
    text = str(raw)
    if ".." in Path(text).parts:
        raise RiskSemanticsError("path_traversal_rejected", f"{role}:{text}")
    path = Path(text)
    if path.drive.startswith("\\\\"):
        raise RiskSemanticsError("unc_path_rejected", f"{role}:{text}")
    if path.name.lower() in _FORBIDDEN_FILE_NAMES:
        raise RiskSemanticsError("forbidden_input_file", f"{role}:{path.name}")
    resolved = path.resolve(strict=True)
    _reject_forbidden_root(resolved, role, text)
    if not resolved.is_file():
        raise RiskSemanticsError("input_not_a_file", f"{role}:{text}")
    return resolved


def validate_new_output_root(raw: str | Path) -> Path:
    if not isinstance(raw, (str, Path)) or not str(raw).strip():
        raise RiskSemanticsError("invalid_output_root", "empty")
    text = str(raw)
    if ".." in Path(text).parts:
        raise RiskSemanticsError("path_traversal_rejected", f"output:{text}")
    path = Path(text)
    if path.drive.startswith("\\\\"):
        raise RiskSemanticsError("unc_path_rejected", f"output:{text}")
    resolved = path.resolve(strict=False)
    _reject_forbidden_root(resolved, "output", text)
    if resolved.exists():
        raise RiskSemanticsError("output_root_exists", f"refusing_to_overwrite:{text}")
    return resolved


# ---------------------------------------------------------------------------
# Producer identity
# ---------------------------------------------------------------------------
def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _verify_anchor_checks(source_root: Path) -> None:
    for check in ANCHOR_CHECKS:
        path = source_root / check["source_relative_path"]
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) < check["line"] or check["anchor"] not in lines[check["line"] - 1]:
            raise RiskSemanticsError(
                "producer_anchor_mismatch", f'{check["source_relative_path"]}:{check["line"]}')


def verify_producer_identity(source_root: Path) -> dict[str, Any]:
    """Actual relevant-source hashes must equal the frozen catalog registration."""
    root_key = _norm_key(source_root.resolve(strict=True)) + "/"
    for relative in FROZEN_RELEVANT_SOURCE_HASHES:
        candidate = (source_root / relative).resolve(strict=True)
        if not _norm_key(candidate).startswith(root_key):
            raise RiskSemanticsError("source_path_escape_rejected", relative)
        actual = _sha256_bytes(candidate.read_bytes())
        expected = FROZEN_RELEVANT_SOURCE_HASHES[relative]
        if actual != expected:
            raise RiskSemanticsError("unknown_producer_version",
                                     f"{relative}:expected:{expected}:actual:{actual}")
    _verify_anchor_checks(source_root)
    return {
        "artifact_schema_versions": [METRICS_SCHEMA_VERSION, RISK_REPORT_SCHEMA_VERSION],
        "producer_release_sha256": PRODUCER_RELEASE_SHA256,
        "source_manifest_sha256": SOURCE_MANIFEST_SHA256,
        "relevant_source_hashes": dict(FROZEN_RELEVANT_SOURCE_HASHES),
        "provenance_kind": "WORKTREE_SNAPSHOT",
        "declared_by": "NF30-C30-frozen-catalog",
        "verification": "actual_source_hashes_match_catalog",
    }


# ---------------------------------------------------------------------------
# JSON Pointer (RFC 6901)
# ---------------------------------------------------------------------------
def resolve_pointer(document: Any, pointer: str) -> tuple[bool, Any]:
    """RFC 6901 resolution; returns (resolved, value)."""
    if pointer == "":
        return True, document
    if not pointer.startswith("/"):
        return False, None
    node = document
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping):
            if token not in node:
                return False, None
            node = node[token]
        elif isinstance(node, list):
            if not token.isdigit():
                return False, None
            index = int(token)
            if index >= len(node):
                return False, None
            node = node[index]
        else:
            return False, None
    return True, node


# ---------------------------------------------------------------------------
# Small builders
# ---------------------------------------------------------------------------
def _denominator(kind: str, status: str, value: Any, pointer: str | None, reason: str) -> dict[str, Any]:
    assert status in ("EXPLICIT", "KNOWN_NOT_EXPOSED", "NOT_APPLICABLE", "UNKNOWN"), status
    return {"kind": kind, "status": status, "value": value, "json_pointer": pointer, "reason": reason}


def _unknown_denominator(kind: str, reason: str) -> dict[str, Any]:
    return _denominator(kind, "UNKNOWN", None, None, reason)


def _na_denominator(reason: str) -> dict[str, Any]:
    return _denominator("none", "NOT_APPLICABLE", None, None, reason)


def _producer_ref() -> dict[str, str]:
    return {"producer_release_sha256": PRODUCER_RELEASE_SHA256,
            "source_manifest_sha256": SOURCE_MANIFEST_SHA256}


def _evidence(path: str, lines: str, function: str, anchor: str) -> dict[str, str]:
    return {"source_relative_path": path, "lines": lines, "function": function, "anchor": anchor}


def _entry(*, artifact_sha256: str, json_pointer: str | None, record_identity: Any,
           metric_name: str, scope_kind: str, basis: str, population: str, aggregation: str,
           unit: str, sample_unit: str, denominator: dict[str, Any], group_or_account_ref: Any,
           producer_rule_id: str, completeness: str, null_semantics: str,
           source_evidence: dict[str, str], explanation: str) -> dict[str, Any]:
    entry = {
        "artifact_sha256": artifact_sha256,
        "json_pointer": json_pointer,
        "record_identity": record_identity,
        "metric_name": metric_name,
        "scope_kind": scope_kind,
        "basis": basis,
        "population": population,
        "aggregation": aggregation,
        "unit": unit,
        "sample_unit": sample_unit,
        "denominator": denominator,
        "group_or_account_ref": group_or_account_ref,
        "producer_identity": _producer_ref(),
        "producer_rule_id": producer_rule_id,
        "completeness": completeness,
        "null_semantics": null_semantics,
        "source_evidence": source_evidence,
        "explanation": explanation,
    }
    if set(entry) != _ENTRY_KEYS:
        raise RiskSemanticsError("sidecar_entry_fields_invalid", metric_name)
    return entry


def _file_receipt(role: str, logical_module: str, path: Path, owner: str,
                  release_id: str | None, provenance_kind: str,
                  relative: str | None = None) -> dict[str, Any]:
    payload = path.read_bytes()
    return {"role": role, "logical_module": logical_module,
            "source_relative_path": relative or path.name,
            "path": str(path), "sha256": _sha256_bytes(payload), "bytes": len(payload),
            "owner": owner, "release_id": release_id, "provenance_kind": provenance_kind}


# ---------------------------------------------------------------------------
# Artifact structural validation (frozen producer shapes)
# ---------------------------------------------------------------------------
def load_metrics(path: Path) -> dict[str, Any]:
    document = parse_json_bytes(path.read_bytes())
    if not isinstance(document, dict):
        raise RiskSemanticsError("artifact_schema_mismatch", "metrics:not_an_object")
    if document.get("schema_version") != METRICS_SCHEMA_VERSION:
        raise RiskSemanticsError("artifact_schema_mismatch", f'metrics:{document.get("schema_version")!r}')
    for field in METRICS_TOP_LEVEL_REQUIRED:
        if field not in document:
            raise RiskSemanticsError("required_pointer_missing", f"metrics:{field}")
    for index, group in enumerate(document["strategy_groups"]):
        if not isinstance(group, dict) or set(group) != STRATEGY_GROUP_FIELDS:
            raise RiskSemanticsError("required_pointer_missing",
                                     f"metrics:strategy_groups/{index}:field_set_mismatch")
    for index, summary in enumerate(document["replay_summaries"]):
        if not isinstance(summary, dict) or set(summary) != REPLAY_SUMMARY_FIELDS:
            raise RiskSemanticsError("required_pointer_missing",
                                     f"metrics:replay_summaries/{index}:field_set_mismatch")
    return document


def load_risk_report(path: Path) -> dict[str, Any]:
    document = parse_json_bytes(path.read_bytes())
    if not isinstance(document, dict) or set(document) != RISK_REPORT_FIELDS:
        raise RiskSemanticsError("artifact_schema_mismatch", "risk_report:field_set_mismatch")
    if document.get("schema_version") != RISK_REPORT_SCHEMA_VERSION:
        raise RiskSemanticsError("artifact_schema_mismatch",
                                 f'risk_report:{document.get("schema_version")!r}')
    return document


def load_binding(path: Path) -> dict[str, Any]:
    document = parse_json_bytes(path.read_bytes())
    if not isinstance(document, dict):
        raise RiskSemanticsError("invalid_binding", "not_an_object")
    return document


# ---------------------------------------------------------------------------
# Binding consistency (H30.6 forged association -> reject; missing -> UNBOUND)
# ---------------------------------------------------------------------------
def _check_binding(binding: Mapping, risk_sha256: str, metrics: Mapping) -> dict[str, Any]:
    declared_role = binding.get("artifact_role", "risk_report")
    if declared_role != "risk_report":
        raise RiskSemanticsError("invalid_binding", f"artifact_role:{declared_role}")
    declared_hash = binding.get("artifact_sha256")
    if declared_hash is not None and declared_hash != risk_sha256:
        raise RiskSemanticsError("artifact_hash_mismatch", f"risk_report:{declared_hash}")
    missing = [field for field in BINDING_FIELDS if binding.get(field) in (None, "")]
    if missing:
        return {"binding_status": "UNBOUND",
                "declared": {field: binding.get(field) for field in BINDING_FIELDS},
                "missing_fields": missing,
                "consistency_checks": ["not_evaluated_missing_binding_fields"]}
    # Full binding: every declared association must be corroborated by metrics content.
    matches = [s for s in metrics["replay_summaries"]
               if s["split_id"] == binding["split_id"]
               and s["run_policy_sha256"] == binding["run_policy_sha256"]
               and s["account_id"] == binding["account_id"]
               and s["model_bundle_sha256"] == binding["model_bundle_sha256"]]
    group_match = [g for g in metrics["strategy_groups"]
                   if g["split_id"] == binding["split_id"]
                   and g["run_policy_sha256"] == binding["run_policy_sha256"]
                   and g["account_id"] == binding["account_id"]
                   and g["cost_scenario"] == binding["cost_scenario"]]
    checks = {
        "replay_summary_tuple_found": bool(matches),
        "strategy_group_tuple_found": bool(group_match),
        "model_bundle_registered": binding["model_bundle_sha256"] in metrics.get("model_bundles", {}),
        "cost_scenario_known": binding["cost_scenario"] in NORMAL_COST_SCENARIOS + STRESS_COST_SCENARIOS,
    }
    if not all(checks.values()):
        failed = [key for key, value in checks.items() if not value]
        raise RiskSemanticsError("forged_account_association", ",".join(failed))
    return {"binding_status": "BOUND",
            "declared": {field: binding[field] for field in BINDING_FIELDS},
            "consistency_checks": checks}


# ---------------------------------------------------------------------------
# Field semantics construction
# ---------------------------------------------------------------------------
def _summary_for(metrics: Mapping, split_id: str, run_policy_sha256: str) -> Mapping | None:
    for summary in metrics["replay_summaries"]:
        if summary["split_id"] == split_id and summary["run_policy_sha256"] == run_policy_sha256:
            return summary
    return None


def _row_completeness(group: Mapping, summary: Mapping | None) -> str:
    if summary is not None and summary["status"] != "COMPLETE":
        return "INCOMPLETE"
    if "REPLAY_INCOMPLETE" in group.get("reasons", []):
        return "INCOMPLETE"
    return "COMPLETE"


def _fixed_scope_relation(metrics: Mapping, group: Mapping) -> str | None:
    """EXACT_SINGLE_STRATEGY_ACCOUNT only when source material proves 1:1."""
    if group["routing_mode"] != "FIXED" or group["fixed_strategy_id"] != group["strategy_id"]:
        return None
    peers = [g for g in metrics["strategy_groups"]
             if g["split_id"] == group["split_id"]
             and g["run_policy_sha256"] == group["run_policy_sha256"]]
    if not peers:
        return None
    for peer in peers:
        if peer["episodes"] > 0 and peer["strategy_id"] != group["fixed_strategy_id"]:
            return None
    return "EXACT_SINGLE_STRATEGY_ACCOUNT"


_ACCOUNT_FIELD_SPECS = (
    ("max_drawdown_ratio", "ACCOUNT_PEAK_TO_TROUGH_EQUITY_RATIO", "RATIO",
     "ACCOUNT_EQUITY_OBSERVATION", "MAX_OF_RUNNING_DRAWDOWN",
     "整个 RunPolicy 回放期内账户权益峰谷回撤最大值（含未闭合持仓盯市影响）。",
     _denominator("running_peak_equity", "UNKNOWN", None, None,
                  "逐点运行峰值权益未暴露于产物，分母未知；不得用初始/期末权益替代。")),
    ("peak_margin_cny", "ACTUAL_PLUS_RESERVED_MARGIN", "CNY",
     "ACCOUNT_EQUITY_OBSERVATION", "MAX",
     "账户占用保证金峰值＝margin_cny+reserved_margin_cny 之和的最大值（metrics.py:318）。"
     "与 risk_report.peak_margin_cny（仅实际 margin，ACTUAL_MARGIN_ONLY）basis 不同，不得自动合并。",
     _na_denominator("绝对金额（MAX），无除法分母。")),
    ("margin_shortfall_cny", "ACCOUNT_MARGIN_PLUS_RESERVED_MINUS_EQUITY_MAX", "CNY",
     "ACCOUNT_EQUITY_OBSERVATION", "MAX",
     "账户 margin+reserved−equity 的最大值（metrics.py:320），正值表示权益不足覆盖占用。",
     _na_denominator("绝对金额（MAX），无除法分母。")),
    ("max_inventory", "GROSS_LOTS", "LOTS",
     "ACCOUNT_EQUITY_OBSERVATION", "MAX",
     "账户最大总持仓手数（所有合约 lots 之和）。单位 lots；不是名义金额，"
     "也不是跨品种可比较的风险敞口。",
     _na_denominator("绝对手数（MAX），无除法分母。")),
    ("longest_recovery_days", "DRAWDOWN_RECOVERY_TRADING_DAYS", "TRADING_DAYS",
     "ACCOUNT_TRADING_DAY", "MAX",
     "账户权益自回撤谷底恢复到前高的最长交易日间隔；仅统计已恢复区间。",
     _denominator("account_trading_day_count", "UNKNOWN", None, None,
                  "账户有效交易日样本数未暴露于产物，分母未知。")),
)


def build_metrics_field_semantics(metrics: Mapping, metrics_sha256: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for index, group in enumerate(metrics["strategy_groups"]):
        pointer = f"/strategy_groups/{index}"
        stress = group["cost_scenario"] in STRESS_COST_SCENARIOS
        summary = _summary_for(metrics, group["split_id"], group["run_policy_sha256"])
        completeness = _row_completeness(group, summary)
        identity = {"group_key": group["group_key"], "strategy_id": group["strategy_id"],
                    "split_id": group["split_id"], "run_policy_sha256": group["run_policy_sha256"],
                    "account_id": group["account_id"], "cost_scenario": group["cost_scenario"]}
        relation = _fixed_scope_relation(metrics, group)
        account_ref = {"kind": "RUN_POLICY_ACCOUNT", "split_id": group["split_id"],
                       "run_policy_sha256": group["run_policy_sha256"],
                       "account_id": group["account_id"]}
        if relation:
            account_ref["scope_relation"] = relation
        strategy_ref = {"kind": "STRATEGY_GROUP", "group_key": group["group_key"],
                        "strategy_id": group["strategy_id"], "split_id": group["split_id"],
                        "run_policy_sha256": group["run_policy_sha256"],
                        "account_id": group["account_id"], "cost_scenario": group["cost_scenario"]}
        account_note = "账户级字段：整个 RunPolicy 账户的量，不可分摊或归因到本行的单个策略。"
        if relation:
            account_note += f" 本行 FIXED 且源材料证明策略-账户精确一对一（{relation}），但口径仍保留账户 scope。"
        episodes_den = _denominator(
            "closed_episode_count", "EXPLICIT", group["episodes"], f"{pointer}/episodes",
            "该策略过滤后的完整轮（closed episode）行数，由产物 episodes 字段显式提供；未闭合仓不混入。")
        blocks_den = _denominator(
            "independent_trading_day_block_count", "EXPLICIT", group["blocks"], f"{pointer}/blocks",
            "对应独立交易日日期块数，由产物 blocks 字段显式提供。")
        if not stress:
            entries.append(_entry(
                artifact_sha256=metrics_sha256, json_pointer=f"{pointer}/episodes",
                record_identity=identity, metric_name="episodes",
                scope_kind="STRATEGY_CLOSED_EPISODES", basis="CLOSED_EPISODE_COUNT",
                population="该 strategy_id/product/(contract_id)/mode/horizon 在该成本场景下的完整轮",
                aggregation="COUNT", unit="episodes", sample_unit="CLOSED_EPISODE",
                denominator=blocks_den, group_or_account_ref=strategy_ref,
                producer_rule_id="M5:experiments.score_strategy_group:rows_filter",
                completeness=completeness,
                null_semantics="恒为整数；0 表示该策略在该组无完整轮样本。",
                source_evidence=_evidence("src/ak_trading/v2/validation/experiments.py",
                                          "1308-1310", "score_strategy_group",
                                          'rows=[r for r in economic["episode_records"]'),
                explanation="单策略完整轮样本数（普通成本场景）。按 strategy_id 等键过滤 episode_records "
                            "后计数（experiments.py:1308-1310）；相邻分钟与加仓不构成独立轮。"))
            entries.append(_entry(
                artifact_sha256=metrics_sha256, json_pointer=f"{pointer}/net_pnl_cny",
                record_identity=identity, metric_name="net_pnl_cny",
                scope_kind="STRATEGY_CLOSED_EPISODES", basis="CLOSED_EPISODE_NET_PNL_SUM",
                population="同上过滤后的完整轮净损益（CNY，含费用/滑点）",
                aggregation="SUM", unit="CNY", sample_unit="CLOSED_EPISODE",
                denominator=_na_denominator("净和值无除法分母；样本量限制见同字段组 episodes/blocks 条目。"),
                group_or_account_ref=strategy_ref,
                producer_rule_id="M5:experiments.score_strategy_group:net_pnl",
                completeness=completeness,
                null_semantics="生产者总写字符串；null 表示未知/未评估。",
                source_evidence=_evidence("src/ak_trading/v2/validation/experiments.py",
                                          "1311,1335", "score_strategy_group", 'pnls=[Decimal(r["net_pnl_cny"])'),
                explanation="单策略完整轮净损益之和。只统计已闭合轮；未闭合持仓的盯市损益不计入该字段，"
                            "但影响账户权益（见账户回撤字段）。"))
            entries.append(_entry(
                artifact_sha256=metrics_sha256, json_pointer=f"{pointer}/conservative_ev_cny",
                record_identity=identity, metric_name="conservative_ev_cny",
                scope_kind="STRATEGY_CLOSED_EPISODES", basis="BLOCK_BOOTSTRAP_EV_LOWER_BOUND",
                population="同上完整轮净损益样本", aggregation="BOOTSTRAP_LOWER_BOUND",
                unit="CNY", sample_unit="CLOSED_EPISODE", denominator=episodes_den,
                group_or_account_ref=strategy_ref,
                producer_rule_id="M5:experiments.score_strategy_group:ev_lower",
                completeness=completeness,
                null_semantics="null＝块自助法未返回下界（样本不足或无样本）。",
                source_evidence=_evidence("src/ak_trading/v2/validation/experiments.py",
                                          "1313,1336", "score_strategy_group", "lower,_=block_bootstrap"),
                explanation="同一样本上按交易日块自助法的保守期望下界；分母为该完整轮样本均值基数。"))
            entries.append(_entry(
                artifact_sha256=metrics_sha256, json_pointer=f"{pointer}/cvar95_cny",
                record_identity=identity, metric_name="cvar95_cny",
                scope_kind="STRATEGY_CLOSED_EPISODES", basis="CLOSED_EPISODE_CVAR",
                population="同上完整轮净损益的损失尾部", aggregation="CVAR_5PCT_TAIL_MEAN",
                unit="CNY", sample_unit="CLOSED_EPISODE",
                denominator=_denominator(
                    "closed_episode_count", "EXPLICIT", group["episodes"], f"{pointer}/episodes",
                    "CVaR 除数为该完整轮数上的 5% 尾部质量（含小数加权，生产者规则 "
                    "experiments.py:1314-1319）；基数由产物 episodes 字段显式提供。"),
                group_or_account_ref=strategy_ref,
                producer_rule_id="M5:experiments.score_strategy_group:cvar95",
                completeness=completeness,
                null_semantics="null＝无完整轮样本（尾部质量为 0），不代表尾损失为零。",
                source_evidence=_evidence("src/ak_trading/v2/validation/experiments.py",
                                          "1314-1319", "score_strategy_group",
                                          "cvar=total/mass if mass else None"),
                explanation="单策略完整轮 CVaR（5% 尾部平均损失）。与 risk_report.loss_cvar95_cny"
                            "（提案模型估计最大值）和账户日权益 CVaR 不是同一总体，禁止同名合并比较。"))
            entries.append(_entry(
                artifact_sha256=metrics_sha256, json_pointer=f"{pointer}/maximum_stress_loss_ratio",
                record_identity=identity, metric_name="maximum_stress_loss_ratio",
                scope_kind="STRATEGY_CLOSED_EPISODES", basis="PROPOSAL_BUDGET_STRESS_RATIO_MAX",
                population="该策略各完整轮开仓提案的预算压力比",
                aggregation="MAX", unit="RATIO", sample_unit="PROPOSAL_BUDGET",
                denominator=_unknown_denominator(
                    "episode_budget_equity_at_start",
                    "各条预算的 equity_at_start_cny 未暴露于产物，分母保持未知；不得用其他权益数替代。"),
                group_or_account_ref=strategy_ref,
                producer_rule_id="M5:experiments.score_strategy_group:stress_max",
                completeness=completeness,
                null_semantics="生产者写数值；0.0 为缺省（无样本）而非实测零压力。",
                source_evidence=_evidence("src/ak_trading/v2/validation/experiments.py",
                                          "1331", "score_strategy_group",
                                          'stress=max((r["stress_loss_ratio"] for r in rows)'),
                explanation="普通成本场景下该值为各完整轮提案预算 stress_loss/equity_at_start 的最大值"
                            "（metrics.py:370 提供逐轮预算比）——预算（估计）口径，不是 GAP/NO_STOP "
                            "模拟路径结果，也不是账户压力结论。"))
            # F01 repair (NF30-HARD-r1): account-basis entries are emitted ONLY
            # on normal-scenario rows.  On GAP/NO_STOP rows the producer
            # overwrites peak_margin_cny/margin_shortfall_cny with stress-path
            # maxima (g.update(stress), experiments.py:1481, fed by
            # evaluate_stress_cohort :1262-1268) and the account-snapshot
            # scoring loop (experiments.py:1496-1500, fed by metrics.py:317-320
            # via :1338-1340) never executes (the stress branch 'continue's at
            # :1484), so an ACTUAL_PLUS_RESERVED_MARGIN /
            # ACCOUNT_MARGIN_PLUS_RESERVED_MINUS_EQUITY_MAX entry here would
            # state a producer definition that does not apply to the record.
            for spec in _ACCOUNT_FIELD_SPECS:
                field, basis, unit, sample_unit, agg, expl, den = spec
                if field == "longest_recovery_days":
                    null_sem = ("null＝期末仍处于未恢复回撤（recovery_start 未清空，metrics.py:383），"
                                "恢复期长度未知；不能把“期末未恢复”猜成 0 天。")
                else:
                    null_sem = "生产者写字符串/数值；null（如出现）表示回放/估值不完整下的未知值。"
                # C30 frozen table: maximum_inventory / max_inventory -> ACCOUNT_GROSS_LOTS_MAX.
                scope = "ACCOUNT_GROSS_LOTS_MAX" if field == "max_inventory" else "RUN_POLICY_ACCOUNT_EQUITY"
                entries.append(_entry(
                    artifact_sha256=metrics_sha256, json_pointer=f"{pointer}/{field}",
                    record_identity=identity, metric_name=field,
                    scope_kind=scope, basis=basis,
                    population="该 RunPolicy 账户全部权益快照（所有策略合并）", aggregation=agg,
                    unit=unit, sample_unit=sample_unit, denominator=den,
                    group_or_account_ref=account_ref,
                    producer_rule_id=f"M5:experiments.score_strategy_group:account_copy"
                                     f"+M5:metrics.evaluate_replay:{field}",
                    completeness=completeness, null_semantics=null_sem,
                    source_evidence=_evidence(
                        "src/ak_trading/v2/validation/experiments.py", "1338-1340", "score_strategy_group",
                        '"margin_shortfall_cny":economic["margin_shortfall_cny"]'),
                    explanation=account_note + expl))
        else:
            cohort_ref = {"kind": "RUN_POLICY_BASE_COHORT_STRESS", "split_id": group["split_id"],
                          "run_policy_sha256": group["run_policy_sha256"],
                          "account_id": group["account_id"],
                          "scenario": group["cost_scenario"]}
            entries.append(_entry(
                artifact_sha256=metrics_sha256, json_pointer=f"{pointer}/net_pnl_cny",
                record_identity=identity, metric_name="net_pnl_cny",
                scope_kind="RUN_POLICY_BASE_COHORT_STRESS", basis="BASE_COHORT_STRESS_PATH_LOSS_TOTAL",
                population="BASE 完整轮构造的整账户压力 cohort 的全部模拟路径",
                aggregation="NEGATED_SUM", unit="CNY", sample_unit="STRESS_PATH",
                denominator=_unknown_denominator(
                    "base_cohort_mature_episode_count",
                    "压力 cohort 的成熟 BASE 完整轮数未暴露于产物（本行 episodes 是从 BASE 组复制的"
                    "策略级计数），分母未知。"),
                group_or_account_ref=cohort_ref,
                producer_rule_id="M5:experiments.attach_stress:net_pnl",
                completeness=completeness,
                null_semantics="生产者总写字符串；null 表示未知。",
                source_evidence=_evidence("src/ak_trading/v2/validation/experiments.py",
                                          "1483", "_execute_validation",
                                          'g["net_pnl_cny"]=str(-sum((Decimal(r["path"]["loss_cny"])'),
                explanation="GAP/NO_STOP 压力行净损益＝整账户 BASE cohort 全部压力路径损失之和取负"
                            "（experiments.py:1483）。这是整账户压力 cohort 口径，不能按 strategy_id "
                            "过滤成单策略损益，也不能与普通场景的完整轮损益并列比较。"))
            entries.append(_entry(
                artifact_sha256=metrics_sha256, json_pointer=f"{pointer}/maximum_stress_loss_ratio",
                record_identity=identity, metric_name="maximum_stress_loss_ratio",
                scope_kind="RUN_POLICY_BASE_COHORT_STRESS", basis="BASE_COHORT_STRESS_PATH_WORST_RATIO",
                population="同一压力 cohort 的每条模拟路径", aggregation="MAX",
                unit="RATIO", sample_unit="STRESS_PATH",
                denominator=_unknown_denominator("episode_budget_equity_at_start",
                                                 "逐条路径的预算 equity_at_start_cny 未暴露于产物，分母未知。"),
                group_or_account_ref=cohort_ref,
                producer_rule_id="M5:experiments.evaluate_stress_cohort:worst",
                completeness=completeness,
                null_semantics="0.0 为空 cohort 缺省（NO_MATURE_BASE_EPISODE_STRESS_PATH），非实测零压力。",
                source_evidence=_evidence(
                    "src/ak_trading/v2/validation/experiments.py", "1262", "evaluate_stress_cohort",
                    'worst=max((Decimal(r["path"]["loss_cny"])/Decimal(r["budget"]["equity_at_start_cny"])'),
                explanation="压力 cohort 内最差路径损失/开仓预算权益比（experiments.py:1262）；"
                            "阈值 0.02 触发 FAIL 是生产者资格逻辑，本 sidecar 不重申任何 PASS/FAIL。"))
            for field, basis in (("margin_shortfall_cny", "BASE_COHORT_STRESS_PATH_SHORTFALL_MAX"),
                                 ("peak_margin_cny", "STRESS_PATH_PEAK_MARGIN_MAX")):
                entries.append(_entry(
                    artifact_sha256=metrics_sha256, json_pointer=f"{pointer}/{field}",
                    record_identity=identity, metric_name=field,
                    scope_kind="RUN_POLICY_BASE_COHORT_STRESS", basis=basis,
                    population="同一压力 cohort 的模拟路径", aggregation="MAX",
                    unit="CNY", sample_unit="STRESS_PATH",
                    denominator=_na_denominator("绝对金额（MAX），无除法分母。"),
                    group_or_account_ref=cohort_ref,
                    producer_rule_id=f"M5:experiments.evaluate_stress_cohort:{field}",
                    completeness=completeness,
                    null_semantics="生产者总写字符串；0 表示 cohort 中无该值样本。",
                    source_evidence=_evidence("src/ak_trading/v2/validation/experiments.py",
                                              "1263-1264", "evaluate_stress_cohort", "shortfall=max("),
                    explanation=f"{field}（GAP/NO_STOP 压力 cohort 口径，experiments.py:1263-1264）；"
                                "峰值保证金为路径口径，区别于普通行账户 ACTUAL_PLUS_RESERVED_MARGIN。"))
            entries.append(_entry(
                artifact_sha256=metrics_sha256, json_pointer=f"{pointer}/cvar95_cny",
                record_identity=identity, metric_name="cvar95_cny",
                scope_kind="RUN_POLICY_BASE_COHORT_STRESS", basis="PRODUCER_EXPLICIT_NULL",
                population="无（压力行不产出该统计）", aggregation="NONE",
                unit="CNY", sample_unit="NOT_APPLICABLE",
                denominator=_na_denominator("生产者显式置 null，无统计总体。"),
                group_or_account_ref=cohort_ref,
                producer_rule_id="M5:experiments.attach_stress:null_ev_cvar",
                completeness=completeness,
                null_semantics="null＝生产者对 GAP/NO_STOP 行显式置空（无普通场景统计）；不是零损失。",
                source_evidence=_evidence("src/ak_trading/v2/validation/experiments.py",
                                          "1482", "_execute_validation",
                                          'g["conservative_ev_cny"]=None;g["cvar95_cny"]=None'),
                explanation="压力行 CVaR 被生产者显式置 null（experiments.py:1482）；"
                            "不得把它当作零或与普通行 CVaR 比较。"))
            for field, unit, sample_unit in (("episodes", "episodes", "CLOSED_EPISODE"),
                                             ("blocks", "blocks", "CLOSED_EPISODE"),
                                             ("admission_rate", "RATIO", "DECISION_GATE"),
                                             ("no_trade_rate", "RATIO", "DECISION_GATE")):
                entries.append(_entry(
                    artifact_sha256=metrics_sha256, json_pointer=f"{pointer}/{field}",
                    record_identity=identity, metric_name=field,
                    scope_kind="STRATEGY_CLOSED_EPISODES", basis="COPIED_FROM_BASE_GROUP_ROW",
                    population="匹配 BASE 组同策略行", aggregation="COPY",
                    unit=unit, sample_unit=sample_unit,
                    denominator=_na_denominator("复制值，无独立除法分母；样本基数见 BASE 行同名字段。"),
                    group_or_account_ref=strategy_ref,
                    producer_rule_id="M5:experiments.attach_stress:copy_base_fields",
                    completeness=completeness,
                    null_semantics="复制值不会为 null（0/0.0 为 BASE 缺省）。",
                    source_evidence=_evidence(
                        "src/ak_trading/v2/validation/experiments.py", "1479-1480", "_execute_validation",
                        'for key in ("episodes","blocks","admission_rate","no_trade_rate","max_inventory")'),
                    explanation=f"{field}（由生产者从匹配 BASE 组复制，experiments.py:1479-1480）；"
                                "该值描述 BASE 场景的策略样本，不是压力模拟产生的计数。"))
            # F01 repair (NF30-HARD-r1): stress rows carry neither account-basis
            # margin entries (their values are stress-path maxima overwritten by
            # g.update(stress), experiments.py:1481) nor account dd/recovery
            # entries: the producer never evaluates those fields on stress rows.
            # The copy loop (:1479-1480) copies only episodes/blocks/
            # admission_rate/no_trade_rate/max_inventory, so dd/recovery keep
            # the placeholder seeds from reports.py:98-101, and max_inventory
            # carries the account value measured on the BASE replay.
            for field, seed_null_sem in (
                    ("max_drawdown_ratio", "0.0＝reports.py:98-101 的占位种子（不是实测零回撤）。"),
                    ("longest_recovery_days",
                     "null＝reports.py:98-101 的占位种子；与普通行的“期末未恢复回撤”（metrics.py:383）"
                     "语义不同，两种 null 不得混用。")):
                entries.append(_entry(
                    artifact_sha256=metrics_sha256, json_pointer=f"{pointer}/{field}",
                    record_identity=identity, metric_name=field,
                    scope_kind="RUN_POLICY_BASE_COHORT_STRESS",
                    basis="PLACEHOLDER_SEED_NOT_EVALUATED",
                    population="无（生产者不对 GAP/NO_STOP 压力行评估该统计）",
                    aggregation="NONE",
                    unit="RATIO" if field == "max_drawdown_ratio" else "TRADING_DAYS",
                    sample_unit="NOT_APPLICABLE",
                    denominator=_na_denominator("占位值无统计总体。"),
                    group_or_account_ref=cohort_ref,
                    producer_rule_id="M5:reports.empty_registered_metrics:placeholder_seed",
                    completeness=completeness, null_semantics=seed_null_sem,
                    source_evidence=_evidence("src/ak_trading/v2/validation/reports.py",
                                              "98-101", "empty_registered_metrics",
                                              "conservative_ev_cny=None, max_drawdown_ratio=0.0"),
                    explanation=f"{field}（GAP/NO_STOP 压力行占位语义）：复制循环只复制 5 键"
                                "（experiments.py:1479-1480，不含本字段），g.update(stress)（:1481）"
                                "不写本字段，评分循环（:1496-1500）因 :1484 continue 对压力行不执行；"
                                "真实产物上该值保持 reports.py:98-101 的占位种子，"
                                "不得读作账户回撤/恢复期或压力路径结论。"))
            entries.append(_entry(
                artifact_sha256=metrics_sha256, json_pointer=f"{pointer}/max_inventory",
                record_identity=identity, metric_name="max_inventory",
                scope_kind="ACCOUNT_GROSS_LOTS_MAX", basis="GROSS_LOTS",
                population="BASE 回放的该 RunPolicy 账户全部权益快照（所有策略合并；经 BASE 组行复制）",
                aggregation="MAX", unit="LOTS", sample_unit="ACCOUNT_EQUITY_OBSERVATION",
                denominator=_na_denominator("绝对手数（MAX），无除法分母。"),
                group_or_account_ref=account_ref,
                producer_rule_id="M5:experiments.attach_stress:copy_base_fields"
                                 "+M5:metrics.evaluate_replay:max_inventory",
                completeness=completeness,
                null_semantics="整数；0 为空仓缺省。",
                source_evidence=_evidence(
                    "src/ak_trading/v2/validation/experiments.py", "1479-1480", "_execute_validation",
                    'for key in ("episodes","blocks","admission_rate","no_trade_rate","max_inventory")'),
                explanation="压力行 max_inventory 是复制循环（experiments.py:1479-1480，5 键之一）从匹配"
                            "BASE 组行复制的账户最大总持仓手数：值来自 BASE 回放的账户权益快照"
                            "（metrics.py:319），不是压力路径模拟的产物。单位 lots；不是名义金额，"
                            "也不是跨品种可比较的风险敞口。"))
    for index, summary in enumerate(metrics["replay_summaries"]):
        pointer = f"/replay_summaries/{index}"
        identity = {"account_id": summary["account_id"], "split_id": summary["split_id"],
                    "run_policy_sha256": summary["run_policy_sha256"],
                    "model_bundle_sha256": summary["model_bundle_sha256"]}
        account_ref = {"kind": "RUN_POLICY_ACCOUNT", "split_id": summary["split_id"],
                       "run_policy_sha256": summary["run_policy_sha256"],
                       "account_id": summary["account_id"]}
        complete = summary["status"] == "COMPLETE"
        incomplete_note = "" if complete else (
            " 回放/估值不完整（status!=COMPLETE）：INCOMPLETE 传播；若源中仍有小计，"
            "只能当“已知小计、不可作完整风险结论”，不得升级为完整风险或 PASS。")
        summary_specs = (
            ("initial_equity_cny", "RUN_POLICY_ACCOUNT", "ACCOUNT_INITIAL_EQUITY",
             "账户初始权益", "VALUE", "CNY", "ACCOUNT",
             "账户初始权益（回放构造，experiments.py:1296-1298）。"),
            ("terminal_equity_cny", "RUN_POLICY_ACCOUNT_EQUITY", "TERMINAL_ACCOUNT_EQUITY",
             "期末账户权益快照", "VALUE", "CNY", "ACCOUNT",
             "期末权益；未闭合持仓盯市损益已计入权益但不计入完整轮净损益。"),
            ("closed_episode_net_pnl_cny", "RUN_POLICY_ACCOUNT", "CLOSED_EPISODE_NET_PNL_SUM",
             "该账户全部已闭合轮（跨策略合并）", "SUM", "CNY", "CLOSED_EPISODE",
             "账户级完整轮净损益（全部策略合并）。不能按 strategy_id 拆分自本字段；"
             "单策略口径见 strategy_groups.net_pnl_cny。"),
            ("open_episode_realized_net_pnl_cny", "RUN_POLICY_ACCOUNT", "OPEN_EPISODE_REALIZED_NET_PNL_SUM",
             "期末仍未闭合轮的已实现现金流", "SUM", "CNY", "OPEN_EPISODE",
             "未闭合轮已实现净损益（小计）。"),
            ("terminal_unrealized_cny", "RUN_POLICY_ACCOUNT", "TERMINAL_UNREALIZED_MARK_TO_MARKET",
             "期末未闭合持仓盯市", "VALUE", "CNY", "OPEN_POSITION", "期末未实现盯市（小计）。"),
        )
        for field, scope, basis, pop, agg, unit, sample_unit, expl in summary_specs:
            value_null = summary[field] is None
            entries.append(_entry(
                artifact_sha256=metrics_sha256, json_pointer=f"{pointer}/{field}",
                record_identity=identity, metric_name=field, scope_kind=scope, basis=basis,
                population=pop, aggregation=agg, unit=unit, sample_unit=sample_unit,
                denominator=_na_denominator("金额（值/求和），无除法分母。"),
                group_or_account_ref=account_ref,
                producer_rule_id="M5:metrics._episode_accounting:summary",
                completeness="COMPLETE" if (complete and not value_null) else "INCOMPLETE",
                null_semantics="null＝估值/来源不完整（VALUATION_SUBTOTAL_INCOMPLETE 或回放未完成）"
                               "下的未知金额；不可当零，也不可将已知小计当完整 NAV。",
                source_evidence=_evidence("src/ak_trading/v2/validation/metrics.py", "473-478",
                                          "_episode_accounting",
                                          '"closed_episode_net_pnl_cny":str(closed_net) if known else None'),
                explanation=expl + incomplete_note))
        entries.append(_entry(
            artifact_sha256=metrics_sha256, json_pointer=f"{pointer}/open_episode_ids",
            record_identity=identity, metric_name="open_episode_ids",
            scope_kind="RUN_POLICY_ACCOUNT", basis="OPEN_EPISODE_ID_LIST",
            population="期末仍未闭合的轮", aggregation="LIST", unit="episode_ids",
            sample_unit="OPEN_EPISODE",
            denominator=_denominator("open_episode_count", "EXPLICIT",
                                     len(summary["open_episode_ids"]), f"{pointer}/open_episode_ids",
                                     "列表长度由产物显式提供。"),
            group_or_account_ref=account_ref,
            producer_rule_id="M5:metrics._episode_accounting:open_ids",
            completeness="COMPLETE" if complete else "INCOMPLETE",
            null_semantics="空列表＝期末无未闭合轮；非空时其盯市影响已在账户权益内。",
            source_evidence=_evidence("src/ak_trading/v2/validation/metrics.py", "459-471",
                                      "_episode_accounting", "open_ids=sorted("),
            explanation="未闭合轮清单：这些轮的已实现+盯市损益影响账户权益与回撤，"
                        "但不进入任何完整轮（episode）统计。" + incomplete_note))
        entries.append(_entry(
            artifact_sha256=metrics_sha256, json_pointer=pointer,
            record_identity=identity, metric_name="terminal_equity_identity",
            scope_kind="RUN_POLICY_ACCOUNT", basis="TERMINAL_CASHFLOW_IDENTITY",
            population="同一账户回放的期初/期末与轮现金流", aggregation="IDENTITY",
            unit="CNY", sample_unit="ACCOUNT",
            denominator=_na_denominator("恒等式，无除法分母。"),
            group_or_account_ref=account_ref,
            producer_rule_id="M5:reports.validate_metrics:summary_reconciliation",
            completeness="COMPLETE" if complete else "INCOMPLETE",
            null_semantics="status=COMPLETE 时金额不得为 null（reports.py:63-67）；"
                           "非 COMPLETE 时金额可为 null 且恒等式不构成结论。",
            source_evidence=_evidence("src/ak_trading/v2/validation/reports.py", "60-67",
                                      "validate_metrics",
                                      "terminal-initial!=closed+open_net+unrealized"),
            explanation="终端恒等式：terminal−initial = closed + open_net + unrealized"
                        "（生产者在 COMPLETE 时强制校验）。本 sidecar 仅按指针引用原值，"
                        "不重算、不改字节、不重签发任何 readiness 结果。" + incomplete_note))
    return entries


def build_unexposed_account_cvar_entries(metrics: Mapping,
                                         metrics_sha256: str) -> list[dict[str, Any]]:
    """metrics.py computes account-level daily-equity CVaR (:339-341) and
    account-level full-episode CVaR (:351-357), but metrics v2.1.4 persists
    neither; document them as KNOWN_NOT_EXPOSED so the three-CVaR distinction
    is complete (H30.3)."""
    entries: list[dict[str, Any]] = []
    for summary in metrics["replay_summaries"]:
        identity = {"account_id": summary["account_id"], "split_id": summary["split_id"],
                    "run_policy_sha256": summary["run_policy_sha256"],
                    "model_bundle_sha256": summary["model_bundle_sha256"]}
        account_ref = {"kind": "RUN_POLICY_ACCOUNT", "split_id": summary["split_id"],
                       "run_policy_sha256": summary["run_policy_sha256"],
                       "account_id": summary["account_id"]}
        entries.append(_entry(
            artifact_sha256=metrics_sha256, json_pointer=None, record_identity=identity,
            metric_name="account_daily_equity_cvar95 (computed, not persisted)",
            scope_kind="RUN_POLICY_ACCOUNT_EQUITY", basis="ACCOUNT_DAILY_EQUITY_CVAR",
            population="该账户逐交易日末权益变化的损失尾部（所有策略合并）",
            aggregation="CVAR_5PCT_TAIL_MEAN", unit="CNY", sample_unit="ACCOUNT_DAY",
            denominator=_denominator(
                "account_valid_trading_day_count", "UNKNOWN", None, None,
                "账户有效日权益样本数未持久化于 metrics v2.1.4，分母保持未知；"
                "不得从总交易数或日期跨度推算。"),
            group_or_account_ref=account_ref,
            producer_rule_id="M5:metrics.evaluate_replay:daily_cvar",
            completeness="KNOWN_NOT_EXPOSED",
            null_semantics="无持久化值；null/缺失即未暴露，不是零。",
            source_evidence=_evidence("src/ak_trading/v2/validation/metrics.py", "334-341",
                                      "evaluate_replay",
                                      "losses = sorted((max(Decimal(0), -v) for v in daily_changes)"),
            explanation="账户日权益 CVaR 在 evaluate_replay 内计算（metrics.py:334-341），"
                        "但未写入 metrics v2.1.4 的任何字段。列出此处仅为完成三类 CVaR 的口径区分："
                        "其 sample_unit 是账户交易日，不同于策略完整轮 CVaR（CLOSED_EPISODE）与 "
                        "risk_report 提案模型 CVaR（PROPOSAL）。不能分摊给账户内各策略。"))
        entries.append(_entry(
            artifact_sha256=metrics_sha256, json_pointer=None, record_identity=identity,
            metric_name="account_closed_episode_cvar95 (computed, not persisted)",
            scope_kind="RUN_POLICY_ACCOUNT", basis="CLOSED_EPISODE_CVAR",
            population="该账户全部成熟轮净损益（跨策略合并）的损失尾部",
            aggregation="CVAR_5PCT_TAIL_MEAN", unit="CNY", sample_unit="CLOSED_EPISODE",
            denominator=_denominator(
                "account_closed_episode_count", "KNOWN_NOT_EXPOSED", None, None,
                "账户级（跨策略合并）完整轮数未作为独立字段持久化；strategy_groups 各行只含策略子集计数。"),
            group_or_account_ref=account_ref,
            producer_rule_id="M5:metrics.evaluate_replay:episode_cvar",
            completeness="KNOWN_NOT_EXPOSED",
            null_semantics="无持久化值；缺失即未暴露，不是零。",
            source_evidence=_evidence("src/ak_trading/v2/validation/metrics.py", "348-357",
                                      "evaluate_replay",
                                      "episode_cvar=tail_loss/tail_mass if tail_mass else None"),
            explanation="账户级完整轮 CVaR 在 evaluate_replay 内计算（metrics.py:348-357，全部策略"
                        "合并），同样未持久化；strategy_groups.cvar95_cny 是各策略子集的重新计算值"
                        "（experiments.py:1314-1319），两者总体不同。"))
    return entries


def build_risk_report_field_semantics(risk: Mapping, risk_sha256: str,
                                      binding_info: Mapping) -> list[dict[str, Any]]:
    bound = binding_info["binding_status"] == "BOUND"
    identity = dict(binding_info["declared"]) if bound else None
    if bound:
        account_ref = {"kind": "RUN_POLICY_ACCOUNT",
                       "split_id": binding_info["declared"]["split_id"],
                       "run_policy_sha256": binding_info["declared"]["run_policy_sha256"],
                       "account_id": binding_info["declared"]["account_id"]}
    else:
        account_ref = None
    unbound_note = "" if bound else (
        "（binding 缺字段：本条仅字段级说明，禁止任何具体账户/策略关联；"
        "mapping_status=PARTIAL_UNBOUND。）")
    entries = [
        _entry(
            artifact_sha256=risk_sha256, json_pointer="/loss_cvar95_cny",
            record_identity=identity, metric_name="loss_cvar95_cny",
            scope_kind="PROPOSAL_MODEL", basis="PROPOSAL_MODEL_CVAR",
            population="回放中各开仓提案的 payoff 模型损失 CVaR 估计",
            aggregation="MAX_OVER_PROPOSALS", unit="CNY", sample_unit="PROPOSAL",
            denominator=_unknown_denominator(
                "proposal_count", "提案数未暴露于 risk_report，分母保持未知；不得以决策数或轮数替代。"),
            group_or_account_ref=account_ref,
            producer_rule_id="M4:decision.replay.risk_report:loss_cvar95_max",
            completeness="COMPLETE" if risk["loss_cvar95_cny"] is not None else "INCOMPLETE",
            null_semantics="null＝无任何提案 payoff 估计（cvars 为空），不是零尾损失。",
            source_evidence=_evidence("src/ak_trading/v2/decision/replay.py", "619-623",
                                      "replay_payloads", '"loss_cvar95_cny":str(max(cvars))'),
            explanation="准入候选的提案模型尾损失最大值：对每个开仓提案的 payoff 模型 "
                        "loss_cvar95_cny 取 max（replay.py:619,623）。这是模型估计的包络，"
                        "不是账户实际或组合已实现 CVaR——不得与 strategy_groups.cvar95_cny"
                        "（完整轮样本）或账户日权益 CVaR 同名合并或对比。" + unbound_note),
        _entry(
            artifact_sha256=risk_sha256, json_pointer="/peak_margin_cny",
            record_identity=identity, metric_name="peak_margin_cny",
            scope_kind="RUN_POLICY_ACCOUNT", basis="ACTUAL_MARGIN_ONLY",
            population="账户全部权益快照的 margin_cny", aggregation="MAX",
            unit="CNY", sample_unit="ACCOUNT_EQUITY_OBSERVATION",
            denominator=_na_denominator("绝对金额（MAX），无除法分母。"),
            group_or_account_ref=account_ref,
            producer_rule_id="M4:decision.replay.risk_report:peak_margin",
            completeness="COMPLETE",
            null_semantics="生产者总写字符串（空序列缺省 '0'）。",
            source_evidence=_evidence("src/ak_trading/v2/decision/replay.py", "622",
                                      "replay_payloads",
                                      '"peak_margin_cny":str(max((a.margin_cny for a in result.equity)'),
            explanation="risk_report 口径的峰值保证金只取实际占用 margin_cny（ACTUAL_MARGIN_ONLY，"
                        "replay.py:622）。metrics.strategy_groups.peak_margin_cny 是 "
                        "margin+reserved（ACTUAL_PLUS_RESERVED_MARGIN，metrics.py:318）。"
                        "同名不同 basis，两值不得自动合并或互相替换。" + unbound_note),
        _entry(
            artifact_sha256=risk_sha256, json_pointer="/maximum_inventory",
            record_identity=identity, metric_name="maximum_inventory",
            scope_kind="ACCOUNT_GROSS_LOTS_MAX", basis="GROSS_LOTS",
            population="账户各权益快照的全部持仓 lots 之和", aggregation="MAX",
            unit="LOTS", sample_unit="ACCOUNT_EQUITY_OBSERVATION",
            denominator=_na_denominator("绝对手数（MAX），无除法分母。"),
            group_or_account_ref=account_ref,
            producer_rule_id="M4:decision.replay.risk_report:maximum_inventory",
            completeness="COMPLETE",
            null_semantics="整数；0 为空仓缺省。",
            source_evidence=_evidence("src/ak_trading/v2/decision/replay.py", "621",
                                      "replay_payloads",
                                      '"maximum_inventory":max((sum(l.lots for l in a.lots)'),
            explanation="账户最大总持仓手数（所有合约 lots 求和再取 max）。单位 lots；"
                        "不是名义金额，也不是跨品种可比较的风险敞口。" + unbound_note),
    ]
    for field, basis, what in (
        ("stress_losses", "STRESS_PATH_SIMULATED_LOSS_MAP",
         "各开仓预算在模拟压力路径下的损失（键 budget_id|scenario）"),
        ("margin_shortfalls", "STRESS_PATH_MARGIN_SHORTFALL_MAP",
         "各开仓预算在模拟压力路径下的保证金短缺（键 budget_id|scenario）"),
    ):
        entries.append(_entry(
            artifact_sha256=risk_sha256, json_pointer=f"/{field}",
            record_identity=identity, metric_name=field,
            scope_kind="RUN_POLICY_ACCOUNT", basis=basis,
            population="按预算逐条模拟的压力路径（GAP/NO_STOP 等 scenario 维度）",
            aggregation="MAP", unit="CNY", sample_unit="STRESS_PATH",
            denominator=_unknown_denominator(
                "budget_count", "预算数未暴露于 risk_report（仅在提案 payoff/预算明细中存在），分母未知。"),
            group_or_account_ref=account_ref,
            producer_rule_id=f"M4:decision.replay.risk_report:{field}",
            completeness="COMPLETE",
            null_semantics=("空对象 {} 的空原因未定：仅凭调用证据才能区分 NO_BUDGETS（提案无预算）"
                            "与 RULES_NOT_SUPPLIED（replay_payloads 未传入 rules，replay.py:608）；"
                            "产物内无法区分时为 UNKNOWN_EMPTY_REASON，且空 ≠ 零压力损失。"
                            if not risk[field] else
                            "对象为逐预算映射；缺某 scenario 键表示该预算无该路径模拟。"),
            source_evidence=_evidence("src/ak_trading/v2/decision/replay.py", "606-618",
                                      "replay_payloads", "if rules:"),
            explanation=what + "。注意区分：这不是 strategy_groups GAP/NO_STOP 行的整账户 BASE "
                               "cohort 汇总值，而是逐预算明细。" + unbound_note))
    entries.append(_entry(
        artifact_sha256=risk_sha256, json_pointer="/open_positions",
        record_identity=identity, metric_name="open_positions",
        scope_kind="RUN_POLICY_ACCOUNT", basis="TERMINAL_NET_LOTS_BY_CONTRACT",
        population="期末账户持仓", aggregation="NET_SUM_BY_CONTRACT", unit="LOTS",
        sample_unit="OPEN_POSITION",
        denominator=_na_denominator("手数净值映射，无除法分母。"),
        group_or_account_ref=account_ref,
        producer_rule_id="M4:decision.replay.risk_report:open_positions",
        completeness="COMPLETE",
        null_semantics="空对象＝期末无持仓。",
        source_evidence=_evidence("src/ak_trading/v2/decision/replay.py", "602-605",
                                  "replay_payloads",
                                  "positions[lot.contract_id]=positions.get(lot.contract_id,0)"),
        explanation="期末各合约净持仓（side*lots 求和）。" + unbound_note))
    return entries


# ---------------------------------------------------------------------------
# Joins
# ---------------------------------------------------------------------------
def build_joins(metrics: Mapping, risk_bound: bool, risk_supplied: bool) -> list[dict[str, Any]]:
    joins: list[dict[str, Any]] = []
    account_keys: set[tuple] = set()
    for group in metrics["strategy_groups"]:
        account_keys.add((group["split_id"], group["run_policy_sha256"], group["account_id"]))
    for summary in metrics["replay_summaries"]:
        key = (summary["split_id"], summary["run_policy_sha256"], summary["account_id"])
        if key in account_keys:
            joins.append({
                "join_kind": "STRATEGY_GROUP_TO_REPLAY_SUMMARY",
                "status": "ALLOWED",
                "keys": {"split_id": key[0], "run_policy_sha256": key[1], "account_id": key[2]},
                "rule": "strategy_groups 行与 replay_summaries 行可按 (split_id, run_policy_sha256, "
                        "account_id) 关联；关联结果只表达“这些策略行共享该账户的账户级量”，"
                        "不产生任何单策略的账户级归因。",
            })
    for group in metrics["strategy_groups"]:
        if group["cost_scenario"] not in STRESS_COST_SCENARIOS:
            continue
        base = [g for g in metrics["strategy_groups"]
                if g["split_id"] == group["split_id"]
                and g["strategy_id"] == group["strategy_id"]
                and g["product"] == group["product"]
                and g["contract_id"] == group["contract_id"]
                and g["mode"] == group["mode"]
                and g["horizon_unit"] == group["horizon_unit"]
                and g["horizon_value"] == group["horizon_value"]
                and g["macro_mode"] == group["macro_mode"]
                and g["cost_scenario"] == "BASE"]
        joins.append({
            "join_kind": "STRESS_GROUP_TO_BASE_GROUP",
            "status": "ALLOWED" if base else "NO_BASE_PEER",
            "keys": {"stress_group_key": group["group_key"],
                     "match": "split_id+strategy_id+product+contract_id+mode+horizon_unit"
                              "+horizon_value+macro_mode+cost_scenario=BASE",
                     "producer_rule": "生产者按 digest(BASE policy) 精确匹配（experiments.py:1474-1477）"},
            "rule": "GAP/NO_STOP 行的部分字段由 BASE 行复制；该 join 只解释复制来源，"
                    "不把压力 cohort 结果变成策略级口径。",
        })
    joins.append({
        "join_kind": "SAME_STRATEGY_ACROSS_RUNS",
        "status": "REJECTED",
        "rule": "同名 strategy_id 在不同 (split_id, run_policy_sha256, account_id, "
                "model_bundle_sha256, cost_scenario) 组合之间禁止合并或比较。",
    })
    joins.append({
        "join_kind": "RISK_REPORT_TO_STRATEGY_ATTRIBUTION",
        "status": "REJECTED",
        "rule": "risk_report 字段无策略维度；禁止将 risk_report 任何值归因到具体 strategy_id"
                "（含 loss_cvar95_cny 的“组合实际 CVaR”式解读）。",
    })
    joins.append({
        "join_kind": "SAME_NAMED_CVAR_MERGING",
        "status": "REJECTED",
        "rule": "cvar95_cny（策略完整轮）/ risk_report.loss_cvar95_cny（提案模型 max）/ 账户日权益 "
                "CVaR 三者总体、样本单位、分母不同，禁止同名合并。",
    })
    if risk_supplied and not risk_bound:
        joins.append({
            "join_kind": "RISK_REPORT_ACCOUNT_BINDING",
            "status": "UNBOUND",
            "rule": "risk_report 已提供但未完整绑定（缺 binding 字段）：只允许字段级说明，"
                    "禁止任何具体账户/策略 join。",
        })
    return joins


# ---------------------------------------------------------------------------
# Sidecar assembly
# ---------------------------------------------------------------------------
def build_risk_semantics(*, metrics_path: str | Path, risk_report_path: str | Path | None = None,
                         binding_path: str | Path | None = None,
                         source_root: str | Path | None = None) -> dict[str, Any]:
    """Build the deterministic sidecar document; rejections raise, never write."""
    repo = _repo_root()
    src_root = Path(source_root).resolve(strict=True) if source_root else repo
    producer_identity = verify_producer_identity(src_root)
    inputs: list[dict[str, Any]] = [
        _file_receipt("producer_source", entry["logical_module"],
                      (src_root / entry["source_relative_path"]).resolve(strict=True),
                      owner=entry["logical_module"], release_id=PRODUCER_RELEASE_SHA256,
                      provenance_kind="WORKTREE_SNAPSHOT",
                      relative=entry["source_relative_path"])
        for entry in SOURCE_MANIFEST]
    metrics_file = validate_readable_file(metrics_path, role="metrics")
    metrics = load_metrics(metrics_file)
    metrics_sha256 = _sha256_bytes(metrics_file.read_bytes())
    inputs.append(_file_receipt("metrics_artifact", "M5-validation", metrics_file,
                                owner="M5-validation", release_id=None,
                                provenance_kind="CALLER_DECLARED", relative=metrics_file.name))
    risk_file: Path | None = None
    risk: dict[str, Any] | None = None
    risk_sha256: str | None = None
    binding_info: dict[str, Any] | None = None
    mapping_status = "MAPPED"
    limitations: list[str] = [
        "producer 身份为 WORKTREE_SNAPSHOT 绑定（本 sidecar 冻结的源码哈希），不代表 M4/M5 最终获审"
        "发行字节；绑定真实发行须由原 owner 提供发行清单并重新冻结 catalog 后独立验证。",
        "本 sidecar 只读解释：不重算风险、不改 metrics/risk_report schema、group keys、资格判定、"
        "回放结果、冻结 lock 或旧报告；不向旧 schema 增加字段。",
        "校验 sidecar 不重新签发 statistical PASS、SHADOW_ACCEPTED 或任何交易资格；"
        "mapping_status 不是工程/统计 PASS/FAIL。",
        "账户级日权益 CVaR 与账户级完整轮 CVaR 由 metrics.py 计算但未持久化，本 sidecar 仅以 "
        "KNOWN_NOT_EXPOSED 说明，不补算数值。",
        "denominator UNKNOWN/KNOWN_NOT_EXPOSED 项不从总交易数、日期跨度或字段名猜测。",
        "空 stress_losses/margin_shortfalls 的空原因未定（UNKNOWN_EMPTY_REASON），不代表零压力损失。",
    ]
    if risk_report_path is not None:
        risk_file = validate_readable_file(risk_report_path, role="risk_report")
        risk = load_risk_report(risk_file)
        risk_sha256 = _sha256_bytes(risk_file.read_bytes())
        inputs.append(_file_receipt("risk_report_artifact", "M4-decision-replay", risk_file,
                                    owner="M4-decision-replay", release_id=None,
                                    provenance_kind="CALLER_DECLARED", relative=risk_file.name))
        if binding_path is not None:
            binding_file = validate_readable_file(binding_path, role="binding")
            inputs.append(_file_receipt("binding", "NF30-caller", binding_file,
                                        owner="NF30-caller", release_id=None,
                                        provenance_kind="CALLER_DECLARED",
                                        relative=binding_file.name))
            binding = load_binding(binding_file)
        else:
            binding = {}
        binding_info = _check_binding(binding, risk_sha256, metrics)
        if binding_info["binding_status"] != "BOUND":
            mapping_status = "PARTIAL_UNBOUND"
            limitations.append(
                "risk_report 绑定缺字段（" + ",".join(binding_info["missing_fields"]) +
                "）：该 artifact 仅给字段级说明，禁止具体账户/策略 join；不代表完整映射。")
    else:
        limitations.append("未提供 risk_report：PROPOSAL_MODEL CVaR 与逐预算压力明细的条目不在本 "
                           "sidecar 中。")
    artifact_bindings: list[dict[str, Any]] = [{
        "artifact_role": "metrics", "artifact_sha256": metrics_sha256,
        "artifact_schema_version": METRICS_SCHEMA_VERSION,
        "binding_status": "BOUND_BY_CONTENT",
        "association_source": "artifact_native_fields",
    }]
    if risk_file is not None:
        artifact_bindings.append({
            "artifact_role": "risk_report", "artifact_sha256": risk_sha256,
            "artifact_schema_version": RISK_REPORT_SCHEMA_VERSION,
            "binding": binding_info,
        })
    field_semantics = build_metrics_field_semantics(metrics, metrics_sha256)
    field_semantics += build_unexposed_account_cvar_entries(metrics, metrics_sha256)
    if risk is not None:
        field_semantics += build_risk_report_field_semantics(
            risk, risk_sha256, binding_info or {"binding_status": "UNBOUND", "declared": {}})
    # H30.6/H30.7: every non-null pointer must actually resolve in its artifact.
    documents = [(metrics, metrics_sha256)]
    if risk is not None:
        documents.append((risk, risk_sha256))
    for artifact, sha in documents:
        for entry in field_semantics:
            if entry["artifact_sha256"] != sha:
                continue
            pointer = entry["json_pointer"]
            if pointer is None:
                if entry["completeness"] != "KNOWN_NOT_EXPOSED":
                    raise RiskSemanticsError("required_pointer_missing",
                                             f'{entry["metric_name"]}:null_pointer')
                continue
            resolved, _value = resolve_pointer(artifact, pointer)
            if not resolved:
                raise RiskSemanticsError("required_pointer_missing",
                                         f'{entry["metric_name"]}:{pointer}')
    if _sha256_bytes(metrics_file.read_bytes()) != metrics_sha256:
        raise RiskSemanticsError("input_mutated_during_run", "metrics")
    if risk_file is not None and _sha256_bytes(risk_file.read_bytes()) != risk_sha256:
        raise RiskSemanticsError("input_mutated_during_run", "risk_report")
    sidecar = {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "semantics_catalog_sha256": SEMANTICS_CATALOG_SHA256,
        "producer_identity": producer_identity,
        "inputs": inputs,
        "artifact_bindings": artifact_bindings,
        "field_semantics": field_semantics,
        "joins": build_joins(
            metrics,
            risk_bound=bool(binding_info and binding_info["binding_status"] == "BOUND"),
            risk_supplied=risk_file is not None),
        "limitations": limitations,
        "mapping_status": mapping_status,
    }
    if set(sidecar) != _ROOT_KEYS:
        raise RiskSemanticsError("sidecar_root_fields_invalid", "internal")
    return sidecar


# ---------------------------------------------------------------------------
# Chinese walkthrough (导读)
# ---------------------------------------------------------------------------
def render_walkthrough_zh(sidecar: Mapping) -> str:
    lines: list[str] = []
    lines.append("# v2 风险指标口径导读（NF30 sidecar）\n")
    lines.append(f"- sidecar schema：`{sidecar['schema_version']}`；"
                 f"catalog SHA-256：`{sidecar['semantics_catalog_sha256'][:16]}…`")
    identity = sidecar["producer_identity"]
    lines.append(f"- producer 身份：release `{identity['producer_release_sha256'][:16]}…`"
                 f"（{identity['provenance_kind']}，实际源码哈希与 catalog 一致才允许生成本文件）。")
    lines.append(f"- mapping_status：`{sidecar['mapping_status']}`（不是工程/统计 PASS/FAIL）。\n")
    lines.append("## 三类 CVaR 对照（禁止同名合并）\n")
    lines.append("| 指标 | scope/basis | 样本单位 | 分母状态 |")
    lines.append("| --- | --- | --- | --- |")
    for entry in sidecar["field_semantics"]:
        if "cvar" in entry["metric_name"].lower():
            lines.append(f"| {entry['metric_name']} | {entry['scope_kind']}/{entry['basis']} | "
                         f"{entry['sample_unit']} | {entry['denominator']['status']} |")
    lines.append("\n## 账户级 vs 策略级\n")
    seen_account: set[str] = set()
    for entry in sidecar["field_semantics"]:
        if entry["scope_kind"] in ("RUN_POLICY_ACCOUNT", "RUN_POLICY_ACCOUNT_EQUITY",
                                   "ACCOUNT_GROSS_LOTS_MAX") and entry["metric_name"] not in seen_account:
            seen_account.add(entry["metric_name"])
            ref = entry["group_or_account_ref"]
            relation = ref.get("scope_relation") if isinstance(ref, dict) else None
            lines.append(f"- `{entry['metric_name']}`：{entry['scope_kind']}/{entry['basis']}"
                         + (f"（{relation}）" if relation else "")
                         + f" — {entry['explanation'].split('。')[0]}。")
    stress = [e for e in sidecar["field_semantics"] if e["scope_kind"] == "RUN_POLICY_BASE_COHORT_STRESS"]
    if stress:
        lines.append("\n## GAP/NO_STOP 压力行（整账户 BASE cohort）\n")
        emitted: set[str] = set()
        for entry in stress:
            if entry["metric_name"] in emitted:
                continue
            emitted.add(entry["metric_name"])
            lines.append(f"- `{entry['metric_name']}`（{entry['basis']}）："
                         f"{entry['explanation'].split('。')[0]}。")
    lines.append("\n## null 与空值语义要点\n")
    lines.append("- `longest_recovery_days=null`＝期末未恢复的回撤，长度未知，不是 0 天。")
    lines.append("- 压力行 `cvar95_cny=null`＝生产者显式置空，不是零损失。")
    lines.append("- 空的 `stress_losses`/`margin_shortfalls`＝空原因未定（NO_BUDGETS 与 "
                 "RULES_NOT_SUPPLIED 需调用证据区分），不是零压力损失。")
    lines.append("- 回放不完整时的金额为 null 或小计：只可当“已知小计、不可作完整风险结论”。")
    lines.append("\n## 限制\n")
    for limitation in sidecar["limitations"]:
        lines.append(f"- {limitation}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Output writing
# ---------------------------------------------------------------------------
def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def write_outputs(sidecar: Mapping, output_root: str | Path) -> dict[str, Any]:
    """Write sidecar + Chinese walkthrough + input/output manifests to a NEW root.

    Deterministic: same inputs produce byte-identical risk-semantics.json,
    walkthrough and manifests contain no timestamps.
    """
    root = validate_new_output_root(output_root)
    root.mkdir(parents=True)
    sidecar_bytes = canonical_json_bytes(sidecar)
    walkthrough = render_walkthrough_zh(sidecar)
    _atomic_write(root / "risk-semantics.json", sidecar_bytes)
    _atomic_write(root / "risk-semantics.zh.md", walkthrough.encode("utf-8"))
    input_manifest = {"schema_version": SIDECAR_SCHEMA_VERSION,
                      "kind": "input_manifest",
                      "semantics_catalog_sha256": SEMANTICS_CATALOG_SHA256,
                      "producer_release_sha256": PRODUCER_RELEASE_SHA256,
                      "inputs": sidecar["inputs"]}
    _atomic_write(root / "input-manifest.json", canonical_json_bytes(input_manifest))
    # H30.7: inputs must be unchanged by this run; re-verify after writing.
    for record in sidecar["inputs"]:
        if record["role"] not in ("metrics_artifact", "risk_report_artifact", "binding"):
            continue
        path = Path(record["path"])
        if _sha256_bytes(path.read_bytes()) != record["sha256"]:
            raise RiskSemanticsError("input_mutated_during_run", record["source_relative_path"])
    outputs = {}
    for name in ("risk-semantics.json", "risk-semantics.zh.md", "input-manifest.json"):
        payload = (root / name).read_bytes()
        outputs[name] = {"sha256": _sha256_bytes(payload), "bytes": len(payload)}
    output_manifest = {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "kind": "output_manifest",
        "mapping_status": sidecar["mapping_status"],
        "semantics_catalog_sha256": SEMANTICS_CATALOG_SHA256,
        "producer_release_sha256": PRODUCER_RELEASE_SHA256,
        "sidecar_sha256": outputs["risk-semantics.json"]["sha256"],
        "inputs_unchanged_after_write": True,
        "outputs": outputs,
        "completion": "NF30_SIDECAR_WRITTEN",
    }
    _atomic_write(root / "output-manifest.json", canonical_json_bytes(output_manifest))
    final_payload = (root / "output-manifest.json").read_bytes()
    output_manifest_hash = _sha256_bytes(final_payload)
    return {"output_root": str(root), "sidecar_sha256": outputs["risk-semantics.json"]["sha256"],
            "output_manifest_sha256": output_manifest_hash,
            "mapping_status": sidecar["mapping_status"]}
