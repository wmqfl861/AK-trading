"""Strict M5 reports and scope-specific qualifications; engineering is not statistics."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import hashlib
from pathlib import Path
from typing import Mapping

from ak_trading.v2.contracts import Horizon, JsonValue, StrategyQualification, StrategySpec
from ak_trading.v2.schemas import canonical_json_bytes, parse_json_bytes
from .experiments import (COST_SCENARIOS, ExperimentSpec, ExperimentError, content, digest,
                          experiment_bytes, require_hash, atomic_write)
from .metrics import forecast_group_key, strategy_group_key, worst_status

FORECAST_FIELDS = frozenset("group_key split_id model_bundle_sha256 product contract_id mode horizon_unit horizon_value session year regime event policy_id barrier_key geometry_key bucket calibration_error n blocks brier log_loss ece pinball coverage ci_lower ci_upper status".split())
STRATEGY_FIELDS = frozenset("group_key split_id model_bundle_sha256 run_policy_sha256 routing_mode fixed_strategy_id account_id strategy_id product contract_id mode horizon_unit horizon_value macro_mode cost_scenario exit_binding_sha256 exit_spec_sha256s fixed_exit_binding_sha256 episodes blocks admission_rate no_trade_rate net_pnl_cny conservative_ev_cny max_drawdown_ratio cvar95_cny maximum_stress_loss_ratio margin_shortfall_cny peak_margin_cny max_inventory longest_recovery_days status reasons".split())
METRICS_FIELDS = frozenset("schema_version research_policy_version macro_policy_version cost_scenarios required_group_keys model_bundles forecast_groups strategy_groups exit_groups exit_bindings exit_registry_sha256 exclusions bootstrap replay_summaries".split())
READINESS_FIELDS = frozenset("schema_version engineering statistics runtime m5_complete products strategies blockers".split())


def validate_metrics(metrics: Mapping, experiment: ExperimentSpec) -> None:
    if set(metrics) != METRICS_FIELDS or metrics["schema_version"] != "ak_probability_metrics_v2.1.4":
        raise ExperimentError("metrics_schema_mismatch")
    if (metrics["research_policy_version"], metrics["macro_policy_version"], tuple(metrics["cost_scenarios"]), tuple(metrics["required_group_keys"])) != (
        experiment.research_policy_version, experiment.macro_policy_version, COST_SCENARIOS, experiment.required_group_keys
    ):
        raise ExperimentError("metrics_registration_mismatch")
    if metrics["bootstrap"] != {"method": "TRADING_DAY_BLOCK", "replicates": 1000, "seed": experiment.seed}:
        raise ExperimentError("bootstrap_policy_changed")
    for model_hash, bundle in metrics["model_bundles"].items():
        if model_hash != digest(bundle):
            raise ExperimentError("model_bundle_hash_mismatch")
        if set(bundle) - {"forecast", "payoff", "regime_reference", "seasonality"} or not {"forecast", "payoff", "regime_reference"} <= set(bundle):
            raise ExperimentError("model_bundle_fields_mismatch")
        for value in bundle.values():
            require_hash(value)
    seen = set()
    for name, fields, key_builder in (("forecast_groups", FORECAST_FIELDS, forecast_group_key),
                                      ("strategy_groups", STRATEGY_FIELDS, strategy_group_key)):
        for g in metrics[name]:
            if set(g) != fields or g["group_key"] != key_builder(g):
                raise ExperimentError("metric_group_schema_or_key_mismatch")
            pair = g["group_key"], g["model_bundle_sha256"]
            if pair in seen or g["model_bundle_sha256"] not in metrics["model_bundles"]:
                raise ExperimentError("duplicate_or_unknown_model_group")
            seen.add(pair)
            worst_status([g["status"]])
            if name == "forecast_groups":
                if g["n"] < 0 or g["blocks"] < 0 or g["blocks"] > g["n"]:
                    raise ExperimentError("invalid_sample_counts")
                if g["geometry_key"] != "UNAVAILABLE" and not g["geometry_key"].startswith("geometry-v2.1.1|"):
                    raise ExperimentError("obsolete_geometry_evidence")
            elif g["cost_scenario"] not in COST_SCENARIOS or g["episodes"] < 0 or g["blocks"] > g["episodes"]:
                raise ExperimentError("invalid_economic_group")
    required = set(experiment.required_group_keys)
    if {g["group_key"] for name in ("forecast_groups", "strategy_groups") for g in metrics[name]} != required:
        raise ExperimentError("required_groups_incomplete_or_unregistered")
    for summary in metrics["replay_summaries"]:
        expected=frozenset("account_id initial_equity_cny terminal_equity_cny closed_episode_net_pnl_cny open_episode_realized_net_pnl_cny terminal_unrealized_cny open_episode_ids pending_intent_ids reconciliation_status status reasons split_id run_policy_sha256 model_bundle_sha256".split())
        if set(summary)!=expected or summary["status"] not in ("COMPLETE","INCOMPLETE","FAILED"):
            raise ExperimentError("replay_summary_schema_mismatch")
        if summary["status"]=="COMPLETE":
            amounts=[summary[k] for k in ("initial_equity_cny","terminal_equity_cny","closed_episode_net_pnl_cny","open_episode_realized_net_pnl_cny","terminal_unrealized_cny")]
            if any(v is None for v in amounts):raise ExperimentError("complete_summary_unknown_amount")
            initial,terminal,closed,open_net,unrealized=map(Decimal,amounts)
            if terminal-initial!=closed+open_net+unrealized:raise ExperimentError("summary_cashflow_reconciliation_failed")
    canonical_json_bytes(content(metrics))  # rejects non-finite values


def empty_registered_metrics(experiment: ExperimentSpec, model_hashes: Mapping[str, str]) -> dict:
    """Every pre-registered cell survives, including completely unobserved cells."""
    model = digest(model_hashes)
    forecasts, strategies = [], []
    policies = {digest(p): p for p in experiment.run_policies}
    for key in experiment.required_group_keys:
        values = parse_json_bytes(key.encode())
        if values[0] == "FORECAST":
            names = ("split_id", "product", "contract_id", "mode", "horizon_unit", "horizon_value", "session", "year", "regime", "event", "policy_id", "bucket", "barrier_key", "geometry_key")
            g = dict(zip(names, values[1:]))
            g.update(group_key=key, model_bundle_sha256=model, calibration_error=None, n=0, blocks=0,
                     brier=None, log_loss=None, ece=None, pinball=None, coverage=None, ci_lower=None, ci_upper=None, status="INSUFFICIENT")
            forecasts.append(g)
        else:
            names = ("split_id", "run_policy_sha256", "strategy_id", "product", "contract_id", "mode", "horizon_unit", "horizon_value", "macro_mode", "cost_scenario")
            g = dict(zip(names, values[1:]))
            p = policies.get(g["run_policy_sha256"])
            if p is None:
                raise ExperimentError("required_group_policy_not_registered")
            _binding = next((b for b in experiment.exit_bindings if b.strategy_id == g["strategy_id"]), None)
            _exit_hash = (values[10] if len(values) > 10 and isinstance(values[10], str)
                          and len(values[10]) == 64 else None) or (_binding.binding_sha256 if _binding else None)
            g.update(group_key=key, model_bundle_sha256=model, routing_mode=p.routing_mode, fixed_strategy_id=p.fixed_strategy_id,
                     exit_binding_sha256=_exit_hash,
                     exit_spec_sha256s=list(_binding.exit_spec_sha256s) if _binding else [],
                     fixed_exit_binding_sha256=p.fixed_exit_binding_sha256 if p.routing_mode=="FIXED" else None,
                     account_id="account:"+digest([experiment.experiment_id,g["run_policy_sha256"]]),
                     episodes=0, blocks=0, admission_rate=0.0, no_trade_rate=0.0, net_pnl_cny="0",
                     conservative_ev_cny=None, max_drawdown_ratio=0.0, cvar95_cny=None, maximum_stress_loss_ratio=0.0,
                     margin_shortfall_cny="0", peak_margin_cny="0", max_inventory=0, longest_recovery_days=None,
                     status="INSUFFICIENT", reasons=["NO_MATURE_ECONOMIC_EVIDENCE"])
            strategies.append(g)
    return {"schema_version":"ak_probability_metrics_v2.1.4", "research_policy_version":experiment.research_policy_version,
            "macro_policy_version":experiment.macro_policy_version, "cost_scenarios":list(COST_SCENARIOS),
            "required_group_keys":list(experiment.required_group_keys), "model_bundles":{model:dict(model_hashes)},
            "forecast_groups":forecasts, "strategy_groups":strategies,
            "exit_groups":[], "exit_registry_sha256":experiment.exit_registry_sha256,
            "exit_bindings":{b.binding_sha256:content(b) for b in experiment.exit_bindings},
            "exclusions":{}, "replay_summaries":[],
            "bootstrap":{"method":"TRADING_DAY_BLOCK","replicates":1000,"seed":experiment.seed}}


def build_readiness(metrics: Mapping, products: list[dict], specs: tuple[StrategySpec, ...],
                    engineering: str, runtime: str, blockers: tuple[str, ...], complete: bool = False) -> dict:
    status = worst_status([g["status"] for name in ("forecast_groups","strategy_groups") for g in metrics[name]])
    strategies = []
    for spec in specs:
        groups = [g for g in metrics["strategy_groups"] if g["strategy_id"] == spec.strategy_id]
        stat = worst_status(g["status"] for g in groups)
        if spec.family == "WAIT":
            stat = "INSUFFICIENT"
        strategies.append({"strategy_id":spec.strategy_id,"engineering":engineering,"statistics":stat,
            "stage":"RESEARCH" if stat!="PASS" else "VALIDATED",
                           "reasons":[] if stat == "PASS" else ["STATISTICS_NOT_PASSED","NO_TRADE"]})
    if complete and (engineering != "PASS" or runtime != "SHADOW_ACCEPTED" or blockers):
        raise ExperimentError("m5_complete_requires_engineering_real_shadow_and_maturity")
    return {"schema_version":"ak_probability_readiness_v2.1.0","engineering":engineering,"statistics":status,
            "runtime":runtime,"m5_complete":complete,"products":products,"strategies":strategies,"blockers":list(blockers)}


def write_validation_report(output: Path, experiment: ExperimentSpec,
                             metrics: Mapping[str, JsonValue], readiness: Mapping[str, JsonValue]) -> str:
    validate_metrics(metrics, experiment)
    if set(readiness) != READINESS_FIELDS or readiness["schema_version"] != "ak_probability_readiness_v2.1.0":
        raise ExperimentError("readiness_schema_mismatch")
    if readiness["engineering"] not in ("PASS","FAIL") or readiness["runtime"] not in ("OFFLINE_ONLY","SHADOW_PENDING","SHADOW_ACCEPTED"):
        raise ExperimentError("readiness_status_invalid")
    actual = worst_status(g["status"] for name in ("forecast_groups","strategy_groups") for g in metrics[name])
    if readiness["statistics"] != actual:
        raise ExperimentError("statistics_must_propagate_worst_required_group")
    if readiness["m5_complete"] and (readiness["engineering"] != "PASS" or readiness["runtime"] != "SHADOW_ACCEPTED" or readiness["blockers"]):
        raise ExperimentError("m5_completion_without_evidence")
    output.mkdir(parents=True,exist_ok=False)
    raw = canonical_json_bytes(content(metrics))
    metric_hash = hashlib.sha256(raw).hexdigest()
    ready = canonical_json_bytes(content(readiness))
    for name, payload in (("experiment.json",experiment_bytes(experiment)),("metrics.json",raw),("readiness.json",ready)):
        atomic_write(output/name,payload)
    text = (f"# v2.1 验证记录\n\n试验 {experiment.experiment_id}。工程 {readiness['engineering']}；统计 {actual}；运行 {readiness['runtime']}。"
            f"M5完整验收：{'完成' if readiness['m5_complete'] else '未完成'}。\n\n"
            "分钟1/5/15/30/60与日线1/5/20按独立期限评估。相邻分钟和加仓不增加独立日期块或整轮样本。"
            "研究结果不授权真实下单；统计失败或不足时新增风险为NO_TRADE。"
            "最终留出尾段只评分；未成熟标签与缺失行情分别记录。\n\n"
            f"冻结风险政策：{experiment.research_policy_version}；宏观政策：{experiment.macro_policy_version}；宏观来源许可：{experiment.macro_mode}。"
            "普通/马丁持续概率门槛0.70/0.85，马丁止损触达上限0.10；压力亏损不要求盈利，无法退出保留EXIT_PENDING。\n\n"
            f"metrics.json SHA-256 `{metric_hash}`\n\nreadiness.json SHA-256 `{hashlib.sha256(ready).hexdigest()}`\n\n"
            "未完成项\n\n" + "\n".join("- "+b for b in readiness["blockers"]) + "\n")
    if metrics["replay_summaries"]:
        text+="\n完整轮与期末账户分别列示\n\n"
        for summary in metrics["replay_summaries"]:
            text+=(f"- 账户 {summary['account_id']}，状态 {summary['status']}：完整轮净损益 {summary['closed_episode_net_pnl_cny']}；"
                   f"未闭合轮已实现净损益 {summary['open_episode_realized_net_pnl_cny']}；期末未实现 {summary['terminal_unrealized_cny']}；"
                   f"期末权益 {summary['terminal_equity_cny']}。未知金额为null，不将小计当完整NAV。\n")
    atomic_write(output/"report.md",text.encode("utf-8"))
    return metric_hash


def build_strategy_qualifications(specs: tuple[StrategySpec, ...],
                                   experiment: ExperimentSpec,
                                   model_hashes: Mapping[str, str],
                                   metrics: Mapping[str, JsonValue],
                                   report_sha256: str, review_sha256: str,
                                   issued_at_nano: int) -> tuple[StrategyQualification, ...]:
    validate_metrics(metrics,experiment)
    require_hash(report_sha256); require_hash(review_sha256)
    if report_sha256 != digest(metrics):
        raise ExperimentError("qualification_report_hash_mismatch")
    model = digest(model_hashes)
    if metrics["model_bundles"].get(model) != dict(model_hashes):
        raise ExperimentError("qualification_model_not_evaluated")
    result = []
    for spec in specs:
        if spec.family == "WAIT":
            continue
        spec_bindings = tuple(b for b in experiment.exit_bindings if b.strategy_id == spec.strategy_id)
        if not spec_bindings:
            raise ExperimentError("qualification_requires_registered_exit_binding")
        for binding in spec_bindings:
            for product in experiment.holdout_scope.products:
                for macro in sorted({p.macro_mode for p in experiment.run_policies}):
                    groups = [g for g in metrics["strategy_groups"] if g["strategy_id"] == spec.strategy_id
                          and g["exit_binding_sha256"] == binding.binding_sha256
                          and g["product"] == product and g["mode"] == spec.mode and g["macro_mode"] == macro
                          and g["model_bundle_sha256"] == model and g["routing_mode"] == "FIXED"
                          and g["fixed_strategy_id"] == spec.strategy_id]
                    horizons = sorted({(g["horizon_unit"],g["horizon_value"]) for g in groups})
                for unit,value in horizons:
                    economics = [g for g in groups if (g["horizon_unit"],g["horizon_value"]) == (unit,value)]
                    forecasts = [g for g in metrics["forecast_groups"] if g["product"] == product
                                 and g["mode"] == spec.mode and (g["horizon_unit"],g["horizon_value"]) == (unit,value)
                                 and g["model_bundle_sha256"] == model
                                 and g["policy_id"] == spec.confidence_policy_id]
                    status = worst_status(g["status"] for g in economics+forecasts)
                    reasons = []
                    contracts = sorted({g["contract_id"] for g in economics if g["contract_id"] is not None})
                    if not forecasts or not any(g["contract_id"] is None for g in economics):
                        reasons.append("MISSING_PRODUCT_PRIMARY_GROUP")
                    for contract in (None,*contracts):
                        if {g["cost_scenario"] for g in economics if g["contract_id"] == contract} != set(COST_SCENARIOS):
                            reasons.append("SIX_SCENARIOS_REQUIRED")
                        if not any(g["contract_id"] == contract for g in forecasts):
                            reasons.append("CONTRACT_FORECAST_SCOPE_MISSING")
                    events={g["event"] for g in forecasts}
                    required_events={"DIRECTION","PATH","AMPLITUDE"}
                    if spec.family in ("TREND","PULLBACK"):
                        required_events|={"PERSIST:UP_TREND","PERSIST:DOWN_TREND"}
                    elif spec.family=="BREAKOUT":
                        required_events|={"PERSIST:UP_BREAKOUT","PERSIST:DOWN_BREAKOUT"}
                    elif spec.family in ("REVERSION","GRID","MARTINGALE"):
                        required_events|={"PERSIST:ORDERED_RANGE","RETURN_BEFORE_BREAK"}
                        if spec.family=="MARTINGALE":required_events|={"STOP_TOUCH:LONG","STOP_TOUCH:SHORT"}
                    if not required_events<=events:reasons.append("STRATEGY_REQUIRED_EVENT_GROUPS_MISSING")
                    for g in forecasts:
                        if g["event"].startswith(("PERSIST:","STOP_TOUCH:")) and g["bucket"]=="ALL":
                            if not any(other["event"]==g["event"] and other["bucket"]!="ALL" and other["contract_id"]==g["contract_id"] for other in forecasts):
                                reasons.append("HIGH_CONFIDENCE_BUCKET_EVIDENCE_MISSING")
                    whitelist = set(experiment.holdout_scope.contract_ids)
                    registered_contracts={parse_json_bytes(k.encode())[5] for k in experiment.required_group_keys
                                          if parse_json_bytes(k.encode())[0]=="STRATEGY" and parse_json_bytes(k.encode())[4]==product
                                          and parse_json_bytes(k.encode())[5] is not None}
                    if set(contracts)!=registered_contracts:reasons.append("REGISTERED_CONTRACT_SCOPE_INCOMPLETE")
                    if not contracts or not set(contracts) <= whitelist:
                        raise ExperimentError("qualification_contract_whitelist_mismatch")
                    martingale = spec.family == "MARTINGALE"
                    for g in economics:
                        if g["episodes"] < (200 if martingale else 100) or g["blocks"] < (60 if martingale else 30):
                            reasons.append("INSUFFICIENT_ECONOMIC_EPISODES_OR_BLOCKS")
                        if Decimal(g["margin_shortfall_cny"]) > 0 or g["maximum_stress_loss_ratio"] > .02:
                            status = "FAIL"
                        if g["cost_scenario"] in COST_SCENARIOS[:4] and (g["conservative_ev_cny"] is None or Decimal(g["conservative_ev_cny"]) <= 0):
                            status = "FAIL" if g["episodes"] >= (200 if martingale else 100) else worst_status((status,"INSUFFICIENT"))
                    if reasons:
                        status = worst_status((status,"INSUFFICIENT"))
                    if status != "PASS":
                        reasons += ["STATISTICS_NOT_PASSED","NO_TRADE"]
                    q = StrategyQualification("pending",spec.strategy_id,spec.version,spec.parameter_version,spec.code_sha256,
                                              experiment.exit_registry_sha256,binding.binding_sha256,binding.exit_spec_sha256s,
                                              product,tuple(contracts),spec.mode,(Horizon(unit,value),),macro,
                                              experiment.research_policy_version,experiment.macro_policy_version,COST_SCENARIOS,
                                              model_hashes,hashlib.sha256(experiment_bytes(experiment)).hexdigest(),status,
                                              report_sha256,review_sha256,issued_at_nano,issued_at_nano+30*86400*1_000_000_000,
                                              tuple(sorted(set(reasons))))
                    result.append(replace(q,qualification_id="qualification:"+digest(q)))
    grouped={}
    for q in result:
        key=(q.strategy_id,q.product,q.mode,q.macro_mode,q.statistics,q.contract_ids,q.exit_binding_sha256)
        grouped.setdefault(key,[]).append(q)
    merged=[]
    for _,records in sorted(grouped.items()):
        first=records[0]
        horizons=tuple(sorted({h for q in records for h in q.horizons},key=lambda h:(h.unit,h.value)))
        reasons=tuple(sorted({r for q in records for r in q.reasons}))
        joined=replace(first,qualification_id="pending",horizons=horizons,reasons=reasons)
        merged.append(replace(joined,qualification_id="qualification:"+digest(joined)))
    return tuple(merged)
