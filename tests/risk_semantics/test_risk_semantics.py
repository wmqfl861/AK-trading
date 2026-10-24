"""NF30 risk-semantics sidecar acceptance probes (H30.1-H30.8).

Run from the repository root with this node's own config only:

    python -B -m pytest -p no:cacheprovider \
        -c tests/risk_semantics/pytest.ini tests/risk_semantics \
        --confcutdir=tests/risk_semantics

Fixtures under ``fixtures/`` are SYNTHETIC producer-shaped documents; they are
not historical PIT data and carry no statistical meaning.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ak_trading.v2.validation.risk_semantics import (
    METRICS_SCHEMA_VERSION,
    RISK_REPORT_SCHEMA_VERSION,
    RiskSemanticsError,
    build_risk_semantics,
    resolve_pointer,
    write_outputs,
)

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
REPO = HERE.parents[1]
CLI = REPO / "scripts" / "explain_v2_risk.py"

SHARED_METRICS = FIXTURES / "metrics-best-ev-shared.json"
FIXED_METRICS = FIXTURES / "metrics-fixed-single.json"
BOUND_RISK = FIXTURES / "risk-report-bound.json"
EMPTY_RISK = FIXTURES / "risk-report-empty-stress.json"
FULL_BINDING = FIXTURES / "binding-full.json"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-B", str(CLI), *args],
                          capture_output=True, text=True, encoding="utf-8")


def _build_shared(binding: str | Path = FULL_BINDING,
                  risk: str | Path = BOUND_RISK) -> dict:
    return build_risk_semantics(metrics_path=SHARED_METRICS,
                                risk_report_path=risk, binding_path=binding)


def _entries(doc: dict, metric_name: str) -> list[dict]:
    return [e for e in doc["field_semantics"] if e["metric_name"] == metric_name]


def _row_entries(doc: dict, group_key: str, metric_name: str) -> list[dict]:
    return [e for e in _entries(doc, metric_name)
            if isinstance(e["record_identity"], dict)
            and e["record_identity"].get("group_key") == group_key]


# ---------------------------------------------------------------------------
# H30.1 two strategies share one BEST_EV account
# ---------------------------------------------------------------------------
def test_h30_1_shared_account_no_fake_strategy_attribution():
    doc = _build_shared()
    assert doc["mapping_status"] == "MAPPED"
    for group_key in ("gk-s1-base", "gk-s2-base"):
        drawdowns = _row_entries(doc, group_key, "max_drawdown_ratio")
        assert len(drawdowns) == 1
        entry = drawdowns[0]
        # Account scope, not a strategy-scoped drawdown.
        assert entry["scope_kind"] == "RUN_POLICY_ACCOUNT_EQUITY"
        assert entry["basis"] == "ACCOUNT_PEAK_TO_TROUGH_EQUITY_RATIO"
        assert entry["group_or_account_ref"]["kind"] == "RUN_POLICY_ACCOUNT"
        assert "scope_relation" not in entry["group_or_account_ref"]
        assert "不可分摊或归因到本行的单个策略" in entry["explanation"]
        # The per-strategy episode statistics stay per-strategy.
        pnl = _row_entries(doc, group_key, "net_pnl_cny")
        assert pnl[0]["scope_kind"] == "STRATEGY_CLOSED_EPISODES"
        assert pnl[0]["record_identity"]["strategy_id"] in ("s1-trend", "s2-reversion")
    # Both strategies reference the SAME account ref: one account, no per-strategy drawdown.
    refs = [_row_entries(doc, key, "max_drawdown_ratio")[0]["group_or_account_ref"]
            for key in ("gk-s1-base", "gk-s2-base")]
    assert refs[0] == refs[1]
    identities = {_row_entries(doc, key, "net_pnl_cny")[0]["record_identity"]["strategy_id"]
                  for key in ("gk-s1-base", "gk-s2-base")}
    assert identities == {"s1-trend", "s2-reversion"}
    allowed = [j for j in doc["joins"]
               if j["join_kind"] == "STRATEGY_GROUP_TO_REPLAY_SUMMARY" and j["status"] == "ALLOWED"]
    assert allowed and allowed[0]["keys"]["account_id"] == "account:best-ev-shared"


# ---------------------------------------------------------------------------
# H30.2 FIXED one-to-one; cross-run merges rejected
# ---------------------------------------------------------------------------
def test_h30_2_fixed_exact_single_strategy_account():
    doc = build_risk_semantics(metrics_path=FIXED_METRICS)
    entry = _row_entries(doc, "gk-s9-base", "max_drawdown_ratio")[0]
    ref = entry["group_or_account_ref"]
    assert ref["scope_relation"] == "EXACT_SINGLE_STRATEGY_ACCOUNT"
    # Even when proven 1:1 the scope stays account-level.
    assert entry["scope_kind"] == "RUN_POLICY_ACCOUNT_EQUITY"
    assert "仍保留账户 scope" in entry["explanation"]


def test_h30_2_no_exact_relation_when_second_strategy_trades(tmp_path):
    mutated = json.loads(FIXED_METRICS.read_text(encoding="utf-8"))
    peer = json.loads(json.dumps(mutated["strategy_groups"][0]))
    peer.update(group_key="gk-s8-base", strategy_id="s8-grid", episodes=5, blocks=3)
    mutated["strategy_groups"].append(peer)
    path = tmp_path / "metrics-two-strategies.json"
    path.write_text(json.dumps(mutated), encoding="utf-8")
    doc = build_risk_semantics(metrics_path=path)
    ref = _row_entries(doc, "gk-s9-base", "max_drawdown_ratio")[0]["group_or_account_ref"]
    assert "scope_relation" not in ref


def test_h30_2_cross_run_strategy_merge_rejected():
    doc = _build_shared()
    rejected = [j for j in doc["joins"] if j["status"] == "REJECTED"]
    kinds = {j["join_kind"] for j in rejected}
    assert "SAME_STRATEGY_ACROSS_RUNS" in kinds
    assert "SAME_NAMED_CVAR_MERGING" in kinds


def test_h30_2_binding_with_foreign_run_policy_rejected(tmp_path):
    binding = json.loads(FULL_BINDING.read_text(encoding="utf-8"))
    binding["run_policy_sha256"] = "9" * 64
    path = tmp_path / "binding-foreign-run.json"
    path.write_text(json.dumps(binding), encoding="utf-8")
    with pytest.raises(RiskSemanticsError) as info:
        build_risk_semantics(metrics_path=SHARED_METRICS,
                             risk_report_path=BOUND_RISK, binding_path=path)
    assert info.value.code == "forged_account_association"


# ---------------------------------------------------------------------------
# H30.3 three CVaR classes with distinct basis / sample_unit
# ---------------------------------------------------------------------------
def test_h30_3_three_cvar_classes_distinct():
    doc = _build_shared()
    strategy = _row_entries(doc, "gk-s1-base", "cvar95_cny")[0]
    assert strategy["basis"] == "CLOSED_EPISODE_CVAR"
    assert strategy["sample_unit"] == "CLOSED_EPISODE"
    assert strategy["denominator"]["status"] == "EXPLICIT"
    daily = [e for e in doc["field_semantics"]
             if e["metric_name"] == "account_daily_equity_cvar95 (computed, not persisted)"][0]
    assert daily["basis"] == "ACCOUNT_DAILY_EQUITY_CVAR"
    assert daily["sample_unit"] == "ACCOUNT_DAY"
    assert daily["json_pointer"] is None and daily["completeness"] == "KNOWN_NOT_EXPOSED"
    assert daily["denominator"]["status"] == "UNKNOWN"
    proposal = _entries(doc, "loss_cvar95_cny")[0]
    assert proposal["scope_kind"] == "PROPOSAL_MODEL"
    assert proposal["basis"] == "PROPOSAL_MODEL_CVAR"
    assert proposal["aggregation"] == "MAX_OVER_PROPOSALS"
    assert proposal["sample_unit"] == "PROPOSAL"
    assert proposal["denominator"]["status"] == "UNKNOWN"
    assert "不是账户实际或组合已实现 CVaR" in proposal["explanation"]
    bases = {strategy["basis"], daily["basis"], proposal["basis"]}
    samples = {strategy["sample_unit"], daily["sample_unit"], proposal["sample_unit"]}
    assert len(bases) == 3 and len(samples) == 3
    # Same-named merging is explicitly rejected.
    merge_join = [j for j in doc["joins"] if j["join_kind"] == "SAME_NAMED_CVAR_MERGING"][0]
    assert merge_join["status"] == "REJECTED"


def test_h30_3_null_proposal_cvar_means_no_estimates(tmp_path):
    binding = json.loads(FULL_BINDING.read_text(encoding="utf-8"))
    binding["artifact_sha256"] = _sha(EMPTY_RISK)
    path = tmp_path / "binding-empty.json"
    path.write_text(json.dumps(binding), encoding="utf-8")
    doc = build_risk_semantics(metrics_path=SHARED_METRICS,
                               risk_report_path=EMPTY_RISK, binding_path=path)
    proposal = _entries(doc, "loss_cvar95_cny")[0]
    assert proposal["completeness"] == "INCOMPLETE"
    assert "不是零尾损失" in proposal["null_semantics"]


# ---------------------------------------------------------------------------
# H30.4 open positions mark-to-market; identity preserved byte-for-byte
# ---------------------------------------------------------------------------
def test_h30_4_open_episodes_mark_to_market_and_identity_preserved(tmp_path):
    before = _sha(SHARED_METRICS)
    doc = _build_shared()
    assert _sha(SHARED_METRICS) == before
    summary = doc["artifact_bindings"][0]
    assert summary["artifact_sha256"] == before
    open_entry = [e for e in doc["field_semantics"] if e["metric_name"] == "open_episode_ids"][0]
    assert open_entry["denominator"] == {
        "kind": "open_episode_count", "status": "EXPLICIT", "value": 2,
        "json_pointer": "/replay_summaries/0/open_episode_ids",
        "reason": "列表长度由产物显式提供。"}
    assert "不进入任何完整轮（episode）统计" in open_entry["explanation"]
    identity = [e for e in doc["field_semantics"] if e["metric_name"] == "terminal_equity_identity"][0]
    assert identity["json_pointer"] == "/replay_summaries/0"
    assert "不重算、不改字节、不重签发任何 readiness 结果" in identity["explanation"]
    # Account drawdown explicitly includes open-position mark-to-market.
    drawdown = _row_entries(doc, "gk-s1-base", "max_drawdown_ratio")[0]
    assert "含未闭合持仓盯市影响" in drawdown["explanation"]
    # CLI round trip leaves inputs untouched.
    out = tmp_path / "h30-4-out"
    result = _run_cli("--metrics", str(SHARED_METRICS), "--risk-report", str(BOUND_RISK),
                      "--binding", str(FULL_BINDING), "--output-root", str(out))
    assert result.returncode == 0, result.stderr
    manifest = json.loads((out / "output-manifest.json").read_text(encoding="utf-8"))
    assert manifest["inputs_unchanged_after_write"] is True
    assert _sha(SHARED_METRICS) == before


# ---------------------------------------------------------------------------
# H30.5 INCOMPLETE propagation; subtotal and null recovery semantics
# ---------------------------------------------------------------------------
def test_h30_5_incomplete_propagation_and_null_recovery():
    doc = build_risk_semantics(metrics_path=FIXED_METRICS)
    drawdown = _row_entries(doc, "gk-s9-base", "max_drawdown_ratio")[0]
    assert drawdown["completeness"] == "INCOMPLETE"
    closed = [e for e in doc["field_semantics"]
              if e["metric_name"] == "closed_episode_net_pnl_cny"][0]
    assert closed["completeness"] == "INCOMPLETE"
    assert "已知小计、不可作完整风险结论" in closed["explanation"]
    assert "不得升级为完整风险或 PASS" in closed["explanation"]
    recovery = _row_entries(doc, "gk-s9-base", "longest_recovery_days")[0]
    assert "未恢复回撤" in recovery["null_semantics"]
    assert "猜成 0 天" in recovery["null_semantics"]
    assert recovery["denominator"]["status"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# H30.6 rejections: no partial-success completion markers
# ---------------------------------------------------------------------------
def test_h30_6_tampered_artifact_rejected(tmp_path):
    binding = json.loads(FULL_BINDING.read_text(encoding="utf-8"))
    binding["artifact_sha256"] = "0" * 64
    path = tmp_path / "binding-tampered.json"
    path.write_text(json.dumps(binding), encoding="utf-8")
    with pytest.raises(RiskSemanticsError) as info:
        build_risk_semantics(metrics_path=SHARED_METRICS,
                             risk_report_path=BOUND_RISK, binding_path=path)
    assert info.value.code == "artifact_hash_mismatch"


def test_h30_6_unknown_producer_version_rejected(tmp_path):
    root = tmp_path / "producer-copy"
    for relative in (
            "src/ak_trading/v2/validation/metrics.py",
            "src/ak_trading/v2/validation/experiments.py",
            "src/ak_trading/v2/validation/reports.py",
            "src/ak_trading/v2/contracts.py",
            "src/ak_trading/v2/decision/replay.py",
            "src/ak_trading/v2/decision/execution.py"):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / relative, target)
    mutated = root / "src/ak_trading/v2/validation/metrics.py"
    mutated.write_bytes(mutated.read_bytes() + b"\n# drifted\n")
    with pytest.raises(RiskSemanticsError) as info:
        build_risk_semantics(metrics_path=SHARED_METRICS, source_root=root)
    assert info.value.code == "unknown_producer_version"


def test_h30_6_missing_required_pointer_rejected(tmp_path):
    doc = json.loads(SHARED_METRICS.read_text(encoding="utf-8"))
    del doc["strategy_groups"]
    path = tmp_path / "metrics-no-groups.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(RiskSemanticsError) as info:
        build_risk_semantics(metrics_path=path)
    assert info.value.code == "required_pointer_missing"


def test_h30_6_mixed_producer_version_rejected(tmp_path):
    risk = json.loads(BOUND_RISK.read_text(encoding="utf-8"))
    risk["schema_version"] = "ak_probability_risk_report_v2.2.0"
    path = tmp_path / "risk-v2.2.0.json"
    path.write_text(json.dumps(risk), encoding="utf-8")
    with pytest.raises(RiskSemanticsError) as info:
        build_risk_semantics(metrics_path=SHARED_METRICS, risk_report_path=path)
    assert info.value.code == "artifact_schema_mismatch"


def test_h30_6_forged_account_association_rejected(tmp_path):
    binding = json.loads(FULL_BINDING.read_text(encoding="utf-8"))
    binding["account_id"] = "account:someone-else"
    path = tmp_path / "binding-forged.json"
    path.write_text(json.dumps(binding), encoding="utf-8")
    with pytest.raises(RiskSemanticsError) as info:
        build_risk_semantics(metrics_path=SHARED_METRICS,
                             risk_report_path=BOUND_RISK, binding_path=path)
    assert info.value.code == "forged_account_association"


def test_h30_6_path_traversal_rejected():
    traversal = str(FIXTURES / ".." / "fixtures" / "metrics-best-ev-shared.json")
    with pytest.raises(RiskSemanticsError) as info:
        build_risk_semantics(metrics_path=traversal)
    assert info.value.code == "path_traversal_rejected"


def test_h30_6_output_overwrite_rejected(tmp_path):
    existing = tmp_path / "existing-evidence"
    existing.mkdir()
    sidecar = build_risk_semantics(metrics_path=SHARED_METRICS)
    with pytest.raises(RiskSemanticsError) as info:
        write_outputs(sidecar, existing)
    assert info.value.code == "output_root_exists"
    assert list(existing.iterdir()) == []


def test_h30_6_cli_rejection_writes_no_completion_marker(tmp_path):
    out = tmp_path / "never-created"
    binding = tmp_path / "binding-tampered.json"
    payload = json.loads(FULL_BINDING.read_text(encoding="utf-8"))
    payload["artifact_sha256"] = "0" * 64
    binding.write_text(json.dumps(payload), encoding="utf-8")
    result = _run_cli("--metrics", str(SHARED_METRICS), "--risk-report", str(BOUND_RISK),
                      "--binding", str(binding), "--output-root", str(out))
    assert result.returncode == 2
    rejection = json.loads(result.stderr)
    assert rejection["rejected"] == "artifact_hash_mismatch"
    assert not out.exists()


# ---------------------------------------------------------------------------
# H30.7 determinism; no statistical/qualification reissuance
# ---------------------------------------------------------------------------
def test_h30_7_deterministic_and_no_qualification_reissuance(tmp_path):
    metrics_before = _sha(SHARED_METRICS)
    first, second = tmp_path / "run-1", tmp_path / "run-2"
    for out in (first, second):
        result = _run_cli("--metrics", str(SHARED_METRICS), "--risk-report", str(BOUND_RISK),
                          "--binding", str(FULL_BINDING), "--output-root", str(out))
        assert result.returncode == 0, result.stderr
    sidecar_a = (first / "risk-semantics.json").read_bytes()
    sidecar_b = (second / "risk-semantics.json").read_bytes()
    assert sidecar_a == sidecar_b
    doc = json.loads(sidecar_a)
    assert doc["schema_version"] == "ak_v2_risk_semantics_v1"
    assert doc["mapping_status"] in ("MAPPED", "PARTIAL_UNBOUND")

    def _walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                yield key, value
                yield from _walk(value)
        elif isinstance(node, list):
            for item in node:
                yield from _walk(item)

    pairs = list(_walk(doc))
    # No readiness/qualification machinery is re-issued under any key.
    forbidden_keys = {"readiness", "engineering", "statistics", "m5_complete",
                      "runtime", "qualification", "strategies", "blockers"}
    assert not (forbidden_keys & {key for key, _ in pairs})
    # No statistical/trading verdict is issued as a value.
    forbidden_values = {"PASS", "FAIL", "SHADOW_ACCEPTED", "SHADOW_PENDING"}
    assert not (forbidden_values & {value for _, value in pairs
                                   if isinstance(value, str)})
    manifest_a = json.loads((first / "output-manifest.json").read_text(encoding="utf-8"))
    manifest_b = json.loads((second / "output-manifest.json").read_text(encoding="utf-8"))
    assert manifest_a["sidecar_sha256"] == manifest_b["sidecar_sha256"]
    assert set(p.name for p in first.iterdir()) == {
        "risk-semantics.json", "risk-semantics.zh.md",
        "input-manifest.json", "output-manifest.json"}
    assert _sha(SHARED_METRICS) == metrics_before
    walkthrough = (first / "risk-semantics.zh.md").read_text(encoding="utf-8")
    assert "三类 CVaR 对照" in walkthrough and "禁止同名合并" in walkthrough


def test_h30_7_pointer_resolves_to_exact_source_value():
    doc = _build_shared()
    metrics = json.loads(SHARED_METRICS.read_text(encoding="utf-8"))
    entry = _row_entries(doc, "gk-s1-base", "net_pnl_cny")[0]
    resolved, value = resolve_pointer(metrics, entry["json_pointer"])
    assert resolved and value == metrics["strategy_groups"][0]["net_pnl_cny"] == "15230.40"
    risk_entry = _entries(doc, "loss_cvar95_cny")[0]
    risk = json.loads(BOUND_RISK.read_text(encoding="utf-8"))
    resolved, value = resolve_pointer(risk, risk_entry["json_pointer"])
    assert resolved and value == risk["loss_cvar95_cny"]


# ---------------------------------------------------------------------------
# H30.8 stress cohort, empty-stress reason, margin bases, gross lots
# ---------------------------------------------------------------------------
def test_h30_8_stress_cohort_semantics():
    doc = _build_shared()
    for metric, basis in (("net_pnl_cny", "BASE_COHORT_STRESS_PATH_LOSS_TOTAL"),
                          ("maximum_stress_loss_ratio", "BASE_COHORT_STRESS_PATH_WORST_RATIO"),
                          ("margin_shortfall_cny", "BASE_COHORT_STRESS_PATH_SHORTFALL_MAX"),
                          ("peak_margin_cny", "STRESS_PATH_PEAK_MARGIN_MAX")):
        # F01 repair: assert over ALL entries of the field, not only [0] — the
        # contradictory second entry must not exist in the first place.
        entries = _row_entries(doc, "gk-s1-gap", metric)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["scope_kind"] == "RUN_POLICY_BASE_COHORT_STRESS"
        assert entry["basis"] == basis
        assert entry["group_or_account_ref"]["kind"] == "RUN_POLICY_BASE_COHORT_STRESS"
    gap_cvar = _row_entries(doc, "gk-s1-gap", "cvar95_cny")
    assert len(gap_cvar) == 1
    assert gap_cvar[0]["null_semantics"].startswith("null＝生产者对 GAP/NO_STOP 行显式置空")
    stress_join = [j for j in doc["joins"] if j["join_kind"] == "STRESS_GROUP_TO_BASE_GROUP"]
    assert any(j["status"] == "ALLOWED" for j in stress_join)


# ---------------------------------------------------------------------------
# F01 repair counterexamples (NF30-HARD-r1): scenario-gated account entries,
# placeholder vs copy semantics, base/stress value coexistence.
# ---------------------------------------------------------------------------
STRESS_ROWS = ("gk-s1-gap", "gk-s1-nostop")
_ACCOUNT_BASES = ("ACTUAL_PLUS_RESERVED_MARGIN",
                  "ACCOUNT_MARGIN_PLUS_RESERVED_MINUS_EQUITY_MAX",
                  "ACCOUNT_PEAK_TO_TROUGH_EQUITY_RATIO",
                  "DRAWDOWN_RECOVERY_TRADING_DAYS")


def test_f01_stress_rows_carry_no_account_basis_entries():
    """Every entry (not just [0]) of the account fields on GAP/NO_STOP rows
    must be free of normal-row account bases: the producer overwrites the
    margin fields with stress-path maxima (g.update(stress)) and never
    evaluates dd/recovery on stress rows."""
    doc = _build_shared()
    for group_key in STRESS_ROWS:
        for metric in ("peak_margin_cny", "margin_shortfall_cny",
                       "max_drawdown_ratio", "longest_recovery_days"):
            entries = _row_entries(doc, group_key, metric)
            assert entries, (group_key, metric)
            for entry in entries:  # ALL entries, counterexample-proof
                assert entry["basis"] not in _ACCOUNT_BASES, (group_key, metric)
                assert entry["scope_kind"] != "RUN_POLICY_ACCOUNT_EQUITY", (group_key, metric)
                assert "该 RunPolicy 账户全部权益快照（所有策略合并）" != entry["population"]
        # Margin fields: exactly one entry each — the stress-path base.
        assert [e["basis"] for e in _row_entries(doc, group_key, "peak_margin_cny")] == \
            ["STRESS_PATH_PEAK_MARGIN_MAX"]
        assert [e["basis"] for e in _row_entries(doc, group_key, "margin_shortfall_cny")] == \
            ["BASE_COHORT_STRESS_PATH_SHORTFALL_MAX"]


def test_f01_base_account_values_and_stress_values_coexist_correctly():
    """BASE rows keep exactly one account entry per margin field while stress
    rows keep exactly one stress-path entry; pointer-resolved values prove the
    two bases are never conflated on the same record."""
    doc = _build_shared()
    metrics = json.loads(SHARED_METRICS.read_text(encoding="utf-8"))
    expected = {
        ("gk-s1-base", "peak_margin_cny"): ("ACTUAL_PLUS_RESERVED_MARGIN", "215300.00"),
        ("gk-s1-base", "margin_shortfall_cny"): ("ACCOUNT_MARGIN_PLUS_RESERVED_MINUS_EQUITY_MAX", "0.00"),
        ("gk-s1-gap", "peak_margin_cny"): ("STRESS_PATH_PEAK_MARGIN_MAX", "240800.00"),
        ("gk-s1-gap", "margin_shortfall_cny"): ("BASE_COHORT_STRESS_PATH_SHORTFALL_MAX", "5000.00"),
        ("gk-s1-nostop", "peak_margin_cny"): ("STRESS_PATH_PEAK_MARGIN_MAX", "224100.00"),
        ("gk-s1-nostop", "margin_shortfall_cny"): ("BASE_COHORT_STRESS_PATH_SHORTFALL_MAX", "0.00"),
    }
    for (group_key, metric), (basis, value) in expected.items():
        entries = _row_entries(doc, group_key, metric)
        assert [e["basis"] for e in entries] == [basis], (group_key, metric)
        resolved, resolved_value = resolve_pointer(metrics, entries[0]["json_pointer"])
        assert resolved and resolved_value == value, (group_key, metric)
    # The stress value is NOT the account value: joining on the (removed)
    # account entry would read the stress-path 240800.00 as the account's
    # margin+reserved peak (215300.00) — exactly F01's false attribution.
    assert resolve_pointer(metrics, "/strategy_groups/2/peak_margin_cny")[1] == "240800.00"
    assert resolve_pointer(metrics, "/strategy_groups/0/peak_margin_cny")[1] == "215300.00"


def test_f01_stress_placeholder_vs_copy_semantics_distinct():
    """Placeholder seeds (dd/recovery, never written on stress rows by any
    producer path) stay distinct from copy semantics (the 5-key copy loop)
    and from the account lots value carried by max_inventory; the fixture
    keeps the pinned producer placeholder seeds (reports.py:98-101)."""
    doc = _build_shared()
    metrics = json.loads(SHARED_METRICS.read_text(encoding="utf-8"))
    for group_key in STRESS_ROWS:
        for metric, seed in (("max_drawdown_ratio", 0.0), ("longest_recovery_days", None)):
            entries = _row_entries(doc, group_key, metric)
            assert [e["basis"] for e in entries] == ["PLACEHOLDER_SEED_NOT_EVALUATED"]
            entry = entries[0]
            assert entry["scope_kind"] == "RUN_POLICY_BASE_COHORT_STRESS"
            assert entry["aggregation"] == "NONE" and entry["sample_unit"] == "NOT_APPLICABLE"
            assert entry["producer_rule_id"] == "M5:reports.empty_registered_metrics:placeholder_seed"
            assert entry["source_evidence"]["lines"] == "98-101"
            assert "占位种子" in entry["explanation"] and "占位种子" in entry["null_semantics"]
            resolved, value = resolve_pointer(metrics, entry["json_pointer"])
            assert resolved and value == seed  # fixture fidelity: producer seeds
        # The 4 strategy-sample fields stay COPY semantics (copy-loop keys 1-4).
        for metric in ("episodes", "blocks", "admission_rate", "no_trade_rate"):
            entry = _row_entries(doc, group_key, metric)[0]
            assert entry["basis"] == "COPIED_FROM_BASE_GROUP_ROW"
            assert entry["producer_rule_id"] == "M5:experiments.attach_stress:copy_base_fields"
            assert entry["scope_kind"] != "RUN_POLICY_BASE_COHORT_STRESS"
        # max_inventory (copy-loop key 5): account lots value copied from BASE.
        inventory = _row_entries(doc, group_key, "max_inventory")
        assert [e["basis"] for e in inventory] == ["GROSS_LOTS"]
        assert inventory[0]["scope_kind"] == "ACCOUNT_GROSS_LOTS_MAX"
        assert inventory[0]["producer_rule_id"] == (
            "M5:experiments.attach_stress:copy_base_fields"
            "+M5:metrics.evaluate_replay:max_inventory")
        assert inventory[0]["source_evidence"]["lines"] == "1479-1480"
        assert inventory[0]["group_or_account_ref"]["kind"] == "RUN_POLICY_ACCOUNT"
        assert inventory[0]["population"].startswith("BASE 回放")
    # Normal rows keep full account entries incl. the recovery null meaning.
    base_recovery = _row_entries(doc, "gk-s1-base", "longest_recovery_days")
    assert [e["basis"] for e in base_recovery] == ["DRAWDOWN_RECOVERY_TRADING_DAYS"]
    assert "未恢复回撤" in base_recovery[0]["null_semantics"]
    base_dd = _row_entries(doc, "gk-s1-base", "max_drawdown_ratio")
    assert [e["basis"] for e in base_dd] == ["ACCOUNT_PEAK_TO_TROUGH_EQUITY_RATIO"]


def test_h30_8_empty_stress_unknown_reason_and_margin_bases(tmp_path):
    binding = json.loads(FULL_BINDING.read_text(encoding="utf-8"))
    binding["artifact_sha256"] = _sha(EMPTY_RISK)
    path = tmp_path / "binding-empty.json"
    path.write_text(json.dumps(binding), encoding="utf-8")
    doc = build_risk_semantics(metrics_path=SHARED_METRICS,
                               risk_report_path=EMPTY_RISK, binding_path=path)
    losses = _entries(doc, "stress_losses")[0]
    assert "UNKNOWN_EMPTY_REASON" in losses["null_semantics"]
    assert "零压力损失" in losses["null_semantics"]
    assert "NO_BUDGETS" in losses["null_semantics"] and "RULES_NOT_SUPPLIED" in losses["null_semantics"]
    # Margin basis distinction inside one sidecar (risk entry selected by artifact hash).
    metrics_peak = _row_entries(doc, "gk-s1-base", "peak_margin_cny")[0]
    empty_sha = _sha(EMPTY_RISK)
    risk_peak = [e for e in _entries(doc, "peak_margin_cny")
                 if e["artifact_sha256"] == empty_sha][0]
    assert metrics_peak["basis"] == "ACTUAL_PLUS_RESERVED_MARGIN"
    assert risk_peak["basis"] == "ACTUAL_MARGIN_ONLY"
    assert "不得自动合并或互相替换" in risk_peak["explanation"]
    metrics_doc = json.loads(SHARED_METRICS.read_text(encoding="utf-8"))
    empty_doc = json.loads(EMPTY_RISK.read_text(encoding="utf-8"))
    _, metrics_value = resolve_pointer(metrics_doc, metrics_peak["json_pointer"])
    _, risk_value = resolve_pointer(empty_doc, risk_peak["json_pointer"])
    assert metrics_value == "215300.00" and risk_value == "0"
    # Gross lots scope for both inventory fields.
    assert _row_entries(doc, "gk-s1-base", "max_inventory")[0]["scope_kind"] == "ACCOUNT_GROSS_LOTS_MAX"
    inventory = _entries(doc, "maximum_inventory")[0]
    assert inventory["scope_kind"] == "ACCOUNT_GROSS_LOTS_MAX" and inventory["unit"] == "LOTS"
    assert "不是名义金额" in inventory["explanation"]


# ---------------------------------------------------------------------------
# PARTIAL_UNBOUND binding
# ---------------------------------------------------------------------------
def test_partial_unbound_binding_keeps_field_level_only(tmp_path):
    binding = json.loads(FULL_BINDING.read_text(encoding="utf-8"))
    binding["artifact_sha256"] = _sha(BOUND_RISK)
    del binding["account_id"]
    path = tmp_path / "binding-missing-account.json"
    path.write_text(json.dumps(binding), encoding="utf-8")
    doc = build_risk_semantics(metrics_path=SHARED_METRICS,
                               risk_report_path=BOUND_RISK, binding_path=path)
    assert doc["mapping_status"] == "PARTIAL_UNBOUND"
    risk_binding = [b for b in doc["artifact_bindings"] if b["artifact_role"] == "risk_report"][0]
    assert risk_binding["binding"]["binding_status"] == "UNBOUND"
    assert "account_id" in risk_binding["binding"]["missing_fields"]
    proposal = _entries(doc, "loss_cvar95_cny")[0]
    assert proposal["record_identity"] is None and proposal["group_or_account_ref"] is None
    assert "PARTIAL_UNBOUND" in proposal["explanation"]
    unbound_join = [j for j in doc["joins"] if j["join_kind"] == "RISK_REPORT_ACCOUNT_BINDING"][0]
    assert unbound_join["status"] == "UNBOUND"


def test_missing_risk_report_noted_in_limitations():
    doc = build_risk_semantics(metrics_path=SHARED_METRICS)
    assert doc["mapping_status"] == "MAPPED"
    assert any("未提供 risk_report" in item for item in doc["limitations"])
    assert not any(e["metric_name"] == "loss_cvar95_cny" for e in doc["field_semantics"])


# ---------------------------------------------------------------------------
# JSON Pointer (RFC 6901) mechanics
# ---------------------------------------------------------------------------
def test_pointer_escapes_and_failures():
    document = {"a~b": {"c/d": 7}, "list": [10, 20]}
    assert resolve_pointer(document, "/a~0b/c~1d") == (True, 7)
    assert resolve_pointer(document, "/list/1") == (True, 20)
    assert resolve_pointer(document, "") == (True, document)
    assert resolve_pointer(document, "/list/5") == (False, None)
    assert resolve_pointer(document, "/missing") == (False, None)
    assert resolve_pointer(document, "list/0") == (False, None)


def test_every_non_null_pointer_resolves():
    doc = _build_shared()
    documents = {
        _sha(SHARED_METRICS): json.loads(SHARED_METRICS.read_text(encoding="utf-8")),
        _sha(BOUND_RISK): json.loads(BOUND_RISK.read_text(encoding="utf-8")),
    }
    for entry in doc["field_semantics"]:
        if entry["json_pointer"] is None:
            assert entry["completeness"] == "KNOWN_NOT_EXPOSED"
            continue
        resolved, _ = resolve_pointer(documents[entry["artifact_sha256"]], entry["json_pointer"])
        assert resolved, (entry["metric_name"], entry["json_pointer"])


def test_producer_identity_binding_fields():
    doc = _build_shared()
    identity = doc["producer_identity"]
    assert identity["artifact_schema_versions"] == [METRICS_SCHEMA_VERSION, RISK_REPORT_SCHEMA_VERSION]
    assert len(identity["producer_release_sha256"]) == 64
    assert identity["provenance_kind"] == "WORKTREE_SNAPSHOT"
    assert identity["verification"] == "actual_source_hashes_match_catalog"
    assert len(identity["relevant_source_hashes"]) == 6
    roles = [record["role"] for record in doc["inputs"]]
    assert roles.count("producer_source") == 6 and "metrics_artifact" in roles
    for record in doc["inputs"]:
        assert len(record["sha256"]) == 64 and record["bytes"] > 0
