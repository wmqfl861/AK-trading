"""Read-only research feasibility preflight for the v2 probability stack (K04).

Usage::

    python scripts/preflight_v2_research.py --input-bundle <bundle-or-H7-metadata>
        --config <research-config.json> --output <fresh-external-directory>
        [--read-scope <read-scope-v1 metadata.json>]

This tool never fits models, never registers experiments, never runs the
research runner and never modifies inputs. It decodes the research config with
production ``experiments.parse_config`` and evaluates structural necessary
conditions per configured risk-taking cell using the production money APIs
(``state.round_loss_cny``, ``state.peak_margin_cny``,
``policy.calculate_stress_loss``), the frozen research policy caps
(.005 round / .02 stress / .10 strategy margin, read from production, never
changed here) and the registered strategy specs.

Payload-read authorization. ``read_frozen_input`` loads complete payloads, so
a mixed bundle must never be loaded and holdout-filtered afterwards. Frozen
payloads are read only after an external ``read-scope-v1`` metadata file
proves authorized NON-holdout coverage: it binds an authorization reference,
the exact config and bundle-manifest hashes, every permitted payload member
with hash and coverage bounds, and the reserved final-holdout intervals. The
permitted coverage must completely cover the bundle request range while being
disjoint from every reserved interval; config split declarations alone are
never read permission. Missing, contradictory or overlapping scope yields
UNKNOWN (exit 4) before any payload read. No schema fields are added to any
production config or manifest; the read-scope file is external metadata.

Verdict semantics per check: POSSIBLE / IMPOSSIBLE / UNKNOWN.
    Capital — enumerates permitted sides and legal integer base lots under
    lot-step/minimum/cap rules with the registered complete layer multipliers
    (GRID keeps all layers, MARTINGALE keeps 1+2+4) and production stop
    geometry. A passing geometry is only a necessary-condition witness.
    Impossibility is claimed ONLY via a price/ATR-independent universal lower
    bound (per-lot fee floor under production stop geometry, where gross loss
    is non-negative because stops always sit adverse of worst entries) or the
    absence of any legal integer lot plan; one failing representative geometry
    proves nothing. Range-family stops require a genuine frozen range, which a
    preflight cannot manufacture, so those cells stay UNKNOWN unless the
    universal bound already proves them impossible. Reference prices and ATR
    are preflight proxies derived from actual authorized-window bars (last
    complete close; 14-bar mean high-low range) and are labeled as proxies —
    they witness possibility but never prove impossibility.
    Calibration — conservative upper bounds from distinct eligible decisions
    per product/frequency/horizon (complete, visible, label-mature bars inside
    the authorized window) and globally unique trading-day blocks: overlapping
    contracts never count as independent copies of the same day. Compared
    against the unchanged confidence-policy minimums (200/500 samples,
    30/60 blocks).
    Payoff support — a defensible opportunity upper bound from the sequential
    episode structure (confirmation observations plus post-exit cooldown per
    episode). Training days divided by maximum holding is explicitly NOT used:
    episodes can close earlier. No provable bound means UNKNOWN.
    Data/calendar — reports actual quality, temporal basis, calendar evidence,
    coverage and availability delays verbatim; SCENARIO is never relabeled as
    historical.
    Confirmation — reuses the accepted K01 calendar semantics through the
    reviewed production helper ``registry._planned_setup_expiry_nano``
    (transferred into this tree by Root; the helper stays owned by the
    critical owner and is never reimplemented or adjusted here): DAILY
    expiry at the bars-th distinct trading-day close with the establishment
    day counting as #1 and strict None on insufficient coverage; MINUTE
    planned slots with the unchanged tail fallback. Passing proves only
    structural timing possibility, never admission; a helper defect is
    reported to its owner with a minimal reproducer.

``structurally_empty``: true only when every configured risk-taking cell is
provably impossible; false only when at least one cell passes ALL checked
necessary conditions; null otherwise. WAIT/CASH never open risk and never
defeat emptiness. This is not a model-fit or statistical PASS.

Exit codes: 0 = a possible cell (structurally_empty false); 3 = proven empty;
4 = unknown/missing prerequisites; 2 = invalid input; 1 = unexpected failure.
Actual-H7 invocations (``selected_input: null`` metadata) are missing
prerequisite (4): this tool never selects r1/r2 inputs and never invents
reference prices or ATR.

Outputs are confined to the fresh output directory: both files are serialized,
round-trip-validated and atomically written; existing paths are refused, and
an output directory nested inside the input bundle is rejected. All inputs are
opened read-only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ak_trading.v2 import policy as m1_policy  # noqa: E402
from ak_trading.v2.contracts import (  # noqa: E402
    FrozenInput,
    Horizon,
    LayerSpec,
    MarketBar,
    SessionWindow,
    TradingRule,
)
from ak_trading.v2.data import read_frozen_input, visible_rule  # noqa: E402
from ak_trading.v2.strategies import state as strategy_state  # noqa: E402
from ak_trading.v2.strategies.registry import strategy_specs_v2  # noqa: E402
from ak_trading.v2.validation.experiments import code_hashes, parse_config  # noqa: E402

SCHEMA = "preflight-v2-research-v1"
READ_SCOPE_SCHEMA = "read-scope-v1"
EXIT_OK_POSSIBLE = 0
EXIT_EMPTY = 3
EXIT_UNKNOWN = 4
EXIT_INVALID = 2
EXIT_UNEXPECTED = 1

RESEARCH_MAXIMUM_ROUND_LOTS = strategy_state.RESEARCH_MAXIMUM_ROUND_LOTS
MAX_EXHAUSTIVE_INSTANTS = 20000
ATR_PROXY_BARS = 14
READ_SCOPE_KEYS = frozenset({
    "schema_version", "authorization_reference", "config_sha256",
    "bundle_manifest_sha256", "permitted_members", "reserved_final_holdout_intervals",
})
POSSIBLE, IMPOSSIBLE, UNKNOWN = "POSSIBLE", "IMPOSSIBLE", "UNKNOWN"
RANGE_FAMILIES = frozenset({"REVERSION", "GRID", "MARTINGALE"})
NANO = 1_000_000_000


class InvalidInput(Exception):
    """Invalid input (exit 2): argument, schema or decode failures."""


class PrerequisiteUnknown(Exception):
    """Unknown/missing prerequisite (exit 4): read scope, payloads, helpers."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_constant(name):
    raise ValueError(f"non-standard JSON constant {name!r}")


def _no_duplicate_keys(pairs):
    seen = set()
    for key, _ in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON key {key!r}")
        seen.add(key)
    return dict(pairs)


def loads_strict(text: str, label: str):
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except ValueError as error:
        raise InvalidInput(f"{label}: invalid JSON ({error})") from error


def read_json_strict(path: Path, label: str):
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise InvalidInput(f"{label}: cannot read {path}: {error}") from error
    try:
        return loads_strict(raw.decode("utf-8"), f"{label} ({path})")
    except UnicodeDecodeError as error:
        raise InvalidInput(f"{label}: not UTF-8 ({error})") from error


def write_new_json(path: Path, payload: dict, label: str) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    loads_strict(text, f"{label} (serialized round-trip)")
    temporary = path.parent / f"{path.name}.tmp-{uuid.uuid4().hex}"
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_new_text(path: Path, text: str, label: str) -> None:
    temporary = path.parent / f"{path.name}.tmp-{uuid.uuid4().hex}"
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _is_hex64(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _is_plain_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Read-scope validation (metadata only; precedes every payload read)
# ---------------------------------------------------------------------------

def _validate_read_scope_shape(scope, label: str) -> None:
    if not isinstance(scope, dict) or set(scope) != READ_SCOPE_KEYS:
        raise InvalidInput(f"{label}: must be an object with exactly {sorted(READ_SCOPE_KEYS)}")
    if scope["schema_version"] != READ_SCOPE_SCHEMA:
        raise InvalidInput(f"{label}: schema_version must be {READ_SCOPE_SCHEMA!r}")
    if not isinstance(scope["authorization_reference"], str) or not scope["authorization_reference"]:
        raise InvalidInput(f"{label}: authorization_reference must be a non-empty string")
    for key in ("config_sha256", "bundle_manifest_sha256"):
        if not _is_hex64(scope[key]):
            raise InvalidInput(f"{label}: {key} must be 64 lowercase hex characters")
    members = scope["permitted_members"]
    if not isinstance(members, list) or not members:
        raise InvalidInput(f"{label}: permitted_members must be a non-empty list")
    for member in members:
        if not isinstance(member, dict) or set(member) != {"path", "sha256", "coverage"}:
            raise InvalidInput(f"{label}: permitted member must have path/sha256/coverage")
        if not isinstance(member["path"], str) or not member["path"] or "\\" in member["path"] \
                or member["path"].startswith("/") or ".." in Path(member["path"]).parts:
            raise InvalidInput(f"{label}: permitted member path must be package-relative posix")
        if not _is_hex64(member["sha256"]):
            raise InvalidInput(f"{label}: permitted member sha256 must be hex64")
        coverage = member["coverage"]
        if not isinstance(coverage, dict) or set(coverage) != {"start_nano", "end_nano"} \
                or not _is_plain_int(coverage["start_nano"]) or not _is_plain_int(coverage["end_nano"]) \
                or coverage["start_nano"] >= coverage["end_nano"]:
            raise InvalidInput(f"{label}: member coverage must be start_nano < end_nano integers")
    reserved = scope["reserved_final_holdout_intervals"]
    if not isinstance(reserved, list):
        raise InvalidInput(f"{label}: reserved_final_holdout_intervals must be a list")
    for interval in reserved:
        if not isinstance(interval, dict) or set(interval) != {"start_nano", "end_nano"} \
                or not _is_plain_int(interval["start_nano"]) or not _is_plain_int(interval["end_nano"]) \
                or interval["start_nano"] >= interval["end_nano"]:
            raise InvalidInput(f"{label}: reserved interval must be start_nano < end_nano integers")


def _merge_intervals(intervals):
    ordered = sorted(intervals)
    merged = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _covers(merged, start: int, end: int) -> bool:
    cursor = start
    for lo, hi in merged:
        if lo > cursor:
            return False
        cursor = max(cursor, hi)
        if cursor >= end:
            return True
    return cursor >= end


def validate_read_scope(scope, *, config_sha: str, bundle_dir: Path, splits, label: str) -> list[tuple[int, int]]:
    """Semantic validation; raises PrerequisiteUnknown on any contradiction."""
    _validate_read_scope_shape(scope, label)
    problems: list[str] = []
    if scope["config_sha256"] != config_sha:
        problems.append(
            f"config hash contradiction: scope declares {scope['config_sha256']}, actual {config_sha}"
        )
    manifest_path = bundle_dir / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        problems.append("bundle manifest.json missing (metadata read only)")
    else:
        actual_manifest_sha = sha256_file(manifest_path)
        if scope["bundle_manifest_sha256"] != actual_manifest_sha:
            problems.append(
                "bundle manifest hash contradiction: scope declares "
                f"{scope['bundle_manifest_sha256']}, actual {actual_manifest_sha}"
            )
        manifest = read_json_strict(manifest_path, f"{label}: bundle manifest (metadata only)")
        if isinstance(manifest, dict):
            files = manifest.get("files")
            request = manifest.get("request")
            permitted = {member["path"]: member for member in scope["permitted_members"]}
            if not isinstance(files, dict):
                problems.append("bundle manifest files must be an object")
            else:
                if set(permitted) != set(files):
                    problems.append(
                        f"permitted members do not match manifest files exactly "
                        f"(missing {sorted(set(files) - set(permitted))}, unexpected {sorted(set(permitted) - set(files))})"
                    )
                for name, member in permitted.items():
                    if name in files and member["sha256"] != files[name]:
                        problems.append(f"permitted member hash mismatch for {name}")
            if not isinstance(request, dict) or not _is_plain_int(request.get("start_nano")) \
                    or not _is_plain_int(request.get("end_nano")):
                problems.append("bundle manifest request bounds malformed")
            else:
                coverage_union = _merge_intervals([
                    (m["coverage"]["start_nano"], m["coverage"]["end_nano"]) for m in scope["permitted_members"]
                ])
                if not _covers(coverage_union, request["start_nano"], request["end_nano"]):
                    problems.append("permitted coverage is incomplete for the bundle request range")
    reserved = [(i["start_nano"], i["end_nano"]) for i in scope["reserved_final_holdout_intervals"]]
    merged_reserved = _merge_intervals(reserved)
    if len(merged_reserved) != len(reserved):
        problems.append("reserved final-holdout intervals overlap each other")
    for member in scope["permitted_members"]:
        for lo, hi in merged_reserved:
            if member["coverage"]["start_nano"] < hi and lo < member["coverage"]["end_nano"]:
                problems.append(
                    f"permitted member {member['path']} coverage overlaps the reserved final holdout"
                )
                break
    coverage_union = _merge_intervals([
        (m["coverage"]["start_nano"], m["coverage"]["end_nano"]) for m in scope["permitted_members"]
    ])
    for split in splits:
        if getattr(split, "final_holdout", False):
            for lo, hi in coverage_union:
                if split.test_start_nano < hi and lo < split.test_end_nano:
                    problems.append(
                        f"config final-holdout split {split.split_id!r} intersects authorized coverage"
                    )
                    break
    if problems:
        raise PrerequisiteUnknown(
            "authorized non-holdout coverage cannot be established; payload read refused: "
            + "; ".join(problems)
        )
    return coverage_union


# ---------------------------------------------------------------------------
# Derivations over actual payload data
# ---------------------------------------------------------------------------

def _decimal(value, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise InvalidInput(f"{label}: not a decimal ({value!r})") from error
    return result


def _product_day_spans(sessions, product: str) -> dict:
    spans: dict = {}
    for session in sessions:
        if session.product != product:
            continue
        lo, hi = spans.get(session.trading_day, (session.start_nano, session.end_nano))
        spans[session.trading_day] = (min(lo, session.start_nano), max(hi, session.end_nano))
    return spans


def _in_window(nano: int, windows) -> bool:
    return any(lo <= nano < hi for lo, hi in windows)


def _eligible_bars(bars, contract_id: str, duration: int, windows) -> list:
    """Complete, visible bars of one contract/frequency inside the window.

    Visibility requires ``available_at_nano`` to be recorded; no availability
    delay cap is applied — a late-but-visible bar is still a permitted
    decision, and an undocumented cap could only understate upper bounds.
    """
    return sorted(
        (bar for bar in bars
         if bar.contract_id == contract_id
         and bar.duration_seconds == duration
         and bar.complete
         and bar.available_at_nano is not None
         and _in_window(bar.end_nano, windows)),
        key=lambda bar: bar.end_nano,
    )


def _matured_minute(bar: MarketBar, horizon, day_spans: dict) -> bool:
    if bar.trading_day is None or horizon.unit != "TRADING_MINUTE":
        return True
    span = day_spans.get(bar.trading_day)
    if span is None:
        return True
    remaining = max(0, span[1] - bar.end_nano)
    return remaining >= horizon.value * 60 * NANO


def _atr_proxy(history: list) -> Decimal | None:
    """Preflight ATR proxy: mean high-low range over the recent complete bars.

    This is NOT the production feature pipeline; it may witness possibility
    but never proves impossibility.
    """
    if not history:
        return None
    ranges = []
    for bar in history[-ATR_PROXY_BARS:]:
        if bar.high is None or bar.low is None:
            continue
        span = bar.high - bar.low
        if span > 0:
            ranges.append(span)
    if not ranges:
        return None
    return sum(ranges) / len(ranges)


def _admissible_lot_plans(spec, rule: TradingRule) -> list[tuple[int, ...]]:
    """Legal integer base-lot plans with the registered complete layer multipliers.

    Mirrors state._layer_plan: REVERSION trades a single layer; GRID and
    MARTINGALE use their registered multipliers (1,1,1 and 1,2,4); every other
    trading family is single-layer.
    """
    if spec.family in ("GRID", "MARTINGALE"):
        multipliers = tuple(int(v) for v in spec.parameters["layer_multipliers"])
    else:
        multipliers = (1,)
    total_units = sum(multipliers)
    hard_cap = min(spec.max_total_lots, RESEARCH_MAXIMUM_ROUND_LOTS)
    base_cap = hard_cap // total_units
    minimum_lots = max(rule.lot_step, rule.min_open_lots)
    plans = []
    for base in range(1, base_cap + 1):
        if base < minimum_lots or base % rule.lot_step != 0:
            continue
        lots = tuple(multiplier * base for multiplier in multipliers)
        if sum(lots) > hard_cap:
            continue
        plans.append(lots)
    return plans


def _universal_fee_floor(rule: TradingRule, plan: tuple[int, ...]) -> Decimal | None:
    """Price/ATR-independent lower bound on any round loss for one lot plan.

    gross loss >= 0 under production stop geometry (stops always sit adverse
    of worst allowed entries); notional fee components are non-negative, so
    the per-lot fee legs alone lower-bound the planned round loss.
    """
    legs = [rule.open_fee, rule.close_fee, rule.close_today_fee]
    if any(leg is None for leg in legs):
        return None
    open_floor = sum(rule.open_fee.per_lot_cny * lots for lots in plan)
    close_floor = max(leg.per_lot_cny for leg in legs) * sum(plan)
    return open_floor + close_floor


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _cell_specs(config_strategies) -> dict:
    specs = {}
    for mode in ("MINUTE", "DAILY"):
        for spec in strategy_specs_v2(mode):
            specs[spec.strategy_id] = spec
    unknown = [sid for sid in config_strategies if sid not in specs]
    if unknown:
        raise InvalidInput(f"config strategy_ids not in the registered v2 registry: {sorted(unknown)}")
    return specs


def check_capital(cells, bundle, equity: Decimal, research, evaluation_as_of: int) -> dict:
    evidence = {}
    verdicts = []
    round_ratio = _decimal(research["maximum_round_loss_ratio"], "maximum_round_loss_ratio")
    stress_ratio = _decimal(research["maximum_stress_loss_to_equity"], "maximum_stress_loss_to_equity")
    margin_ratio = _decimal(research["maximum_strategy_margin_ratio"], "maximum_strategy_margin_ratio")
    stress_policy = m1_policy.stress_policy()
    for cell in cells:
        spec, contract_id = cell["spec"], cell["contract_id"]
        label = f"{spec.strategy_id}|{contract_id}"
        record = {"scope": label, "family": spec.family}
        verdict = UNKNOWN
        rule = visible_rule(bundle, contract_id, evaluation_as_of)
        if rule is None:
            record["reason"] = "no trading rule visible at evaluation_as_of_nano"
            record["verdict"] = UNKNOWN
            evidence[label] = record
            verdicts.append(UNKNOWN)
            continue
        record["rule_id"] = rule.rule_id
        if rule.evidence == "UNKNOWN":
            record["reason"] = "rule evidence UNKNOWN; fees/margin cannot be priced"
            record["verdict"] = UNKNOWN
            evidence[label] = record
            verdicts.append(UNKNOWN)
            continue
        plans = _admissible_lot_plans(spec, rule)
        record["admissible_lot_plans"] = [list(plan) for plan in plans]
        if not plans:
            record["reason"] = "no admissible integer base-lot plan under lot-step/minimum/cap rules"
            record["verdict"] = IMPOSSIBLE
            evidence[label] = record
            verdicts.append(IMPOSSIBLE)
            continue
        round_cap = equity * _decimal(spec.parameters["round_risk_ratio"], "round_risk_ratio")
        stress_cap = equity * stress_ratio
        margin_cap = equity * margin_ratio
        record["caps_cny"] = {
            "round": str(round_cap), "stress": str(stress_cap), "margin": str(margin_cap),
        }
        floor = _universal_fee_floor(rule, plans[0])
        record["universal_fee_floor_cny"] = None if floor is None else str(floor)
        if floor is not None and floor > round_cap:
            record["reason"] = (
                "universal lower bound: smallest legal plan's per-lot fee floor already exceeds the "
                "round cap at any price/ATR under production stop geometry"
            )
            record["verdict"] = IMPOSSIBLE
            evidence[label] = record
            verdicts.append(IMPOSSIBLE)
            continue
        missing = [name for name, value in (
            ("open_fee", rule.open_fee), ("close_fee", rule.close_fee),
            ("close_today_fee", rule.close_today_fee),
            ("long_margin_rate", rule.long_margin_rate), ("short_margin_rate", rule.short_margin_rate),
        ) if value is None]
        if missing:
            record["reason"] = f"missing fees/margin inputs: {missing}"
            record["verdict"] = UNKNOWN
            evidence[label] = record
            verdicts.append(UNKNOWN)
            continue
        if spec.family in RANGE_FAMILIES:
            record["reason"] = (
                "range-family stop geometry requires a genuine frozen range; a preflight cannot "
                "manufacture one, and the universal fee floor did not prove impossibility"
            )
            record["verdict"] = UNKNOWN
            evidence[label] = record
            verdicts.append(UNKNOWN)
            continue
        usable = [bar for bar in _eligible_bars(
            bundle.bars, contract_id, 60 if spec.mode == "MINUTE" else 86400, cell["windows"]
        ) if bar.close is not None]
        instants = [bar.end_nano for bar in usable]
        if len(instants) > MAX_EXHAUSTIVE_INSTANTS:
            record["reason"] = (
                f"authorized window carries {len(instants)} candidate decision instants, above the "
                f"exhaustive bound {MAX_EXHAUSTIVE_INSTANTS}; no witness search performed"
            )
            record["verdict"] = UNKNOWN
            evidence[label] = record
            verdicts.append(UNKNOWN)
            continue
        if not instants:
            record["reason"] = "no complete bars in the authorized window for this contract/frequency"
            record["verdict"] = UNKNOWN
            evidence[label] = record
            verdicts.append(UNKNOWN)
            continue
        stop_multiple = _decimal(spec.parameters["stop_loss_multiple"], "stop_loss_multiple")
        witness = None
        tried = 0
        for index, instant in enumerate(instants):
            history = usable[: index + 1]
            reference = history[-1].close
            atr = _atr_proxy(history)
            if reference is None or reference <= 0 or atr is None or atr <= 0:
                continue
            for side in (1, -1):
                stop = reference - side * stop_multiple * atr
                if stop <= 0:
                    continue
                for plan in plans:
                    layers = tuple(
                        LayerSpec(index=i, trigger_price=reference, reference_entry_price=reference, lots=lots)
                        for i, lots in enumerate(plan)
                    )
                    tried += 1
                    try:
                        planned = strategy_state.round_loss_cny(layers, side, stop, rule, atr)
                        stress = m1_policy.calculate_stress_loss(layers, side, stop, atr, rule, stress_policy)
                        margin = strategy_state.peak_margin_cny(layers, side, rule, atr, stress_policy)
                    except (ValueError, strategy_state.StateTransitionError):
                        continue
                    if margin is None:
                        continue
                    if planned <= round_cap and stress <= stress_cap and stress <= equity and margin <= margin_cap:
                        witness = {
                            "instant_end_nano": instant,
                            "side": side,
                            "lots": list(plan),
                            "reference_price": str(reference),
                            "atr_proxy": str(atr),
                            "stop_price": str(stop),
                            "planned_loss_cny": str(planned),
                            "stress_loss_cny": str(stress),
                            "peak_margin_cny": str(margin),
                            "required_equity_cny": str(max(
                                planned / _decimal(spec.parameters["round_risk_ratio"], "rr"),
                                stress / stress_ratio,
                                margin / margin_ratio,
                                stress,
                            )),
                        }
                        break
                if witness:
                    break
            if witness:
                break
        record["geometries_evaluated"] = tried
        record["reference_atr_method"] = (
            f"preflight proxy: last complete close; mean high-low range over <= {ATR_PROXY_BARS} bars"
        )
        if witness:
            record["witness"] = witness
            record["verdict"] = POSSIBLE
            verdicts.append(POSSIBLE)
        else:
            record["reason"] = (
                "no admissible geometry passed at the proxy inputs over the whole authorized window; "
                "impossibility is NOT claimed because the proxy is not the production feature pipeline"
            )
            record["verdict"] = UNKNOWN
            verdicts.append(UNKNOWN)
        evidence[label] = record
    overall = IMPOSSIBLE if all(v == IMPOSSIBLE for v in verdicts) and verdicts else (
        POSSIBLE if any(v == POSSIBLE for v in verdicts) else UNKNOWN
    )
    return {
        "verdict": overall,
        "scope": "every configured risk-taking cell (strategy x contract) at config evaluation_as_of_nano",
        "evidence": evidence,
        "bound": (
            "POSSIBLE requires one fully specified admissible geometry passing all three caps "
            "(necessary-condition witness only). IMPOSSIBLE requires a price/ATR-independent universal "
            "lower bound (per-lot fee floor under production stop geometry) or no legal integer lot "
            "plan. Cap ratios read from production research policy: round "
            f"{research['maximum_round_loss_ratio']}, stress {research['maximum_stress_loss_to_equity']}, "
            f"strategy margin {research['maximum_strategy_margin_ratio']}."
        ),
    }


def check_calibration(cells, bundle, windows, config) -> dict:
    """Conservative calibration bounds per product/frequency/horizon.

    The sample bound aggregates distinct eligible decisions over ALL
    configured contracts of the product (plan §6: sum over configured
    contracts); each (contract, bar) identity counts once, so a contract
    configured under several strategies never inflates the aggregate.
    Trading-day blocks are the unique days of the population's in-window
    bars: overlapping contracts sharing a day are one block, never
    independent copies. Day blocks ignore label maturity (conservative);
    sample counts apply visibility, window and label maturity.
    """
    contracts_by_product: dict[str, list[str]] = {}
    for cell in cells:
        contracts_by_product.setdefault(cell["product"], []).append(cell["contract_id"])
    mode_horizons = {
        "MINUTE": [("TRADING_MINUTE", value) for value in config["minute_horizons"]],
        "DAILY": [("TRADING_DAY", value) for value in config["daily_horizons"]],
    }
    populations: dict[tuple, dict] = {}
    for product, contract_ids in contracts_by_product.items():
        day_spans = _product_day_spans(bundle.sessions, product)
        product_days = sorted(day_spans)
        unique_contracts = sorted(set(contract_ids))
        for mode, horizons in mode_horizons.items():
            duration = 60 if mode == "MINUTE" else 86400
            in_window_bars: list = []
            for contract_id in unique_contracts:
                in_window_bars.extend(_eligible_bars(bundle.bars, contract_id, duration, windows))
            day_bound = len({bar.trading_day for bar in in_window_bars if bar.trading_day is not None})
            for unit, value in horizons:
                horizon = Horizon(unit=unit, value=value)
                per_contract = {}
                sample_bound = 0
                for contract_id in unique_contracts:
                    eligible = [
                        bar for bar in _eligible_bars(bundle.bars, contract_id, duration, windows)
                        if (_matured_minute(bar, horizon, day_spans) if unit == "TRADING_MINUTE"
                            else _day_matured(bar, horizon, product_days))
                    ]
                    per_contract[contract_id] = len(eligible)
                    sample_bound += len(eligible)
                populations[(product, mode, unit, value)] = {
                    "sample_bound": sample_bound,
                    "day_blocks": day_bound,
                    "per_contract": per_contract,
                    "contract_count": len(unique_contracts),
                }
    by_cell = {}
    verdicts = []
    seen = set()
    for cell in cells:
        spec, product = cell["spec"], cell["product"]
        horizon = spec.max_holding
        label = f"{spec.strategy_id}|{product}|{spec.mode}|{horizon.unit}={horizon.value}"
        if label in seen:
            continue
        seen.add(label)
        population = populations[(product, spec.mode, horizon.unit, horizon.value)]
        confidence = m1_policy.confidence_policy(spec.family)
        verdict = (
            POSSIBLE
            if population["sample_bound"] >= confidence.minimum_samples
            and population["day_blocks"] >= confidence.minimum_blocks
            else IMPOSSIBLE
        )
        by_cell[label] = {
            "scope": label,
            "sample_upper_bound": population["sample_bound"],
            "unique_trading_day_blocks": population["day_blocks"],
            "per_contract_counts": population["per_contract"],
            "configured_contract_count": population["contract_count"],
            "minimum_samples": confidence.minimum_samples,
            "minimum_blocks": confidence.minimum_blocks,
            "policy_id": confidence.policy_id,
            "counting": (
                "sample bound sums distinct eligible decisions over every configured contract of "
                "the product (no contract counted twice); trading-day blocks are unique days of "
                "the population's in-window bars, so overlapping contracts are not independent "
                "copies of one day"
            ),
            "bound_scope": (
                "whole authorized non-holdout window: a conservative overstatement relative to "
                "any single split's calibration window; embargo/purge are not subtracted and no "
                "exclusion beyond visibility/maturity/window is invented"
            ),
            "verdict": verdict,
        }
        verdicts.append(verdict)
    overall = IMPOSSIBLE if verdicts and all(v == IMPOSSIBLE for v in verdicts) else (
        POSSIBLE if any(v == POSSIBLE for v in verdicts) else UNKNOWN
    )
    population_bounds = {
        f"{product}|{mode}|{unit}={value}": {
            "sample_bound": population["sample_bound"],
            "day_blocks": population["day_blocks"],
            "per_contract": population["per_contract"],
        }
        for (product, mode, unit, value), population in sorted(populations.items())
    }
    return {
        "verdict": overall,
        "scope": "product/frequency/horizon decision populations against unchanged policy minimums",
        "evidence": by_cell,
        "population_bounds": population_bounds,
        "bound": (
            "conservative upper bound: distinct eligible decisions per product/frequency/horizon "
            "aggregated over configured contracts and globally unique trading-day blocks "
            "(overlapping contracts never count as independent copies of one day); compared with "
            "200/500 samples and 30/60 blocks, unchanged"
        ),
    }


def _day_matured(bar: MarketBar, horizon, product_days: list) -> bool:
    if bar.trading_day is None:
        return True
    try:
        index = product_days.index(bar.trading_day)
    except ValueError:
        return True
    return index + horizon.value < len(product_days)


def check_payoff(cells, bundle, windows, research) -> dict:
    cooldown_minutes = int(research["cooldown_minutes"])
    cooldown_days = int(research["daily_cooldown_trading_days"])
    by_cell = {}
    verdicts = []
    for cell in cells:
        spec, product = cell["spec"], cell["product"]
        label = f"{spec.strategy_id}|{product}"
        if label in by_cell:
            continue
        confidence = m1_policy.confidence_policy(spec.family)
        confirmation = int(spec.parameters["entry_confirmation_bars"])
        day_spans = _product_day_spans(bundle.sessions, product)
        if not day_spans:
            by_cell[label] = {"scope": label, "verdict": UNKNOWN, "reason": "no calendar sessions for product"}
            verdicts.append(UNKNOWN)
            continue
        bar_seconds = 60 if spec.mode == "MINUTE" else 86400
        cooldown_seconds = (cooldown_minutes * 60 if spec.mode == "MINUTE" else cooldown_days * 86400) * NANO
        confirmation_seconds = confirmation * bar_seconds * NANO
        cycle = cooldown_seconds + confirmation_seconds
        worst_day_seconds = max(hi - lo for lo, hi in day_spans.values())
        per_day = 1 + max(0, worst_day_seconds - confirmation_seconds) // cycle if cycle > 0 else None
        if per_day is None:
            by_cell[label] = {"scope": label, "verdict": UNKNOWN, "reason": "cycle length not positive"}
            verdicts.append(UNKNOWN)
            continue
        product_days = len(day_spans)
        bound = per_day * product_days
        verdict = POSSIBLE if bound >= confidence.minimum_samples else IMPOSSIBLE
        by_cell[label] = {
            "scope": label,
            "episodes_per_day_upper_bound": per_day,
            "trading_days": product_days,
            "opportunity_upper_bound": bound,
            "minimum_samples": confidence.minimum_samples,
            "formula": (
                "per day <= 1 + floor((longest session span - confirmation span) / (cooldown + "
                "confirmation span)); sequential episodes need fresh confirmation observations after "
                "each post-exit cooldown. Training days / maximum holding is deliberately NOT used "
                "(episodes can close earlier, so that quotient is not an upper bound)."
            ),
            "cooldown": {"minutes": cooldown_minutes, "trading_days": cooldown_days},
            "confirmation_bars": confirmation,
            "verdict": verdict,
        }
        verdicts.append(verdict)
    overall = IMPOSSIBLE if verdicts and all(v == IMPOSSIBLE for v in verdicts) else (
        POSSIBLE if any(v == POSSIBLE for v in verdicts) else UNKNOWN
    )
    return {
        "verdict": overall,
        "scope": "sequential opportunity structure per configured cell",
        "evidence": by_cell,
        "bound": (
            "defensible upper bound from episode structure (confirmation observations plus post-exit "
            "cooldown per episode); insufficient only when even this bound cannot reach the "
            "qualification minimum; no valid bound means UNKNOWN"
        ),
    }


def check_data_calendar(bundle, dataset_kind: str, manifest: dict, windows) -> dict:
    limitations = manifest.get("limitations", []) if isinstance(manifest, dict) else []
    sources = {}
    statuses = {}
    temporal_bases = {}
    for bar in bundle.bars:
        sources[bar.source] = sources.get(bar.source, 0) + 1
        statuses[bar.quality.status] = statuses.get(bar.quality.status, 0) + 1
        temporal_bases[bar.quality.temporal_basis] = temporal_bases.get(bar.quality.temporal_basis, 0) + 1
    session_evidence = {}
    for session in bundle.sessions:
        session_evidence[session.evidence_status] = session_evidence.get(session.evidence_status, 0) + 1
    delays = [
        bar.available_at_nano - bar.end_nano
        for bar in bundle.bars
        if bar.available_at_nano is not None and _in_window(bar.end_nano, windows)
    ]
    delay_median = statistics.median(delays) if delays else None
    evidence = {
        "dataset_kind": dataset_kind,
        "limitations": limitations,
        "bar_count": len(bundle.bars),
        "bar_sources": sources,
        "bar_quality_statuses": statuses,
        "bar_temporal_bases": temporal_bases,
        "session_evidence_status": session_evidence,
        "calendar_versions": sorted({s.calendar_version for s in bundle.sessions}),
        "coverage": {
            "first_bar_end_nano": min((b.end_nano for b in bundle.bars), default=None),
            "last_bar_end_nano": max((b.end_nano for b in bundle.bars), default=None),
        },
        "availability_delay_nanos": {
            "min": min(delays) if delays else None,
            "median": delay_median,
            "max": max(delays) if delays else None,
        },
        "relabeling_guard": "dataset_kind, sources and temporal bases are reported verbatim; SCENARIO is never relabeled historical",
    }
    if not bundle.bars:
        verdict, reason = IMPOSSIBLE, "the frozen bundle carries no bars at all"
    elif session_evidence.get("UNKNOWN"):
        verdict, reason = UNKNOWN, "calendar contains UNKNOWN-evidence sessions"
    else:
        verdict, reason = POSSIBLE, "data and calendar metadata reported as retained"
    return {
        "verdict": verdict,
        "scope": "whole authorized bundle window",
        "evidence": {**evidence, "reason": reason},
        "bound": "descriptive provenance report; verdict IMPOSSIBLE only when the bundle has no bars",
    }


def check_confirmation(cells, bundle, windows) -> dict:
    """Structural timing possibility via the accepted K01 calendar semantics.

    Calls the reviewed production helper
    ``ak_trading.v2.strategies.registry._planned_setup_expiry_nano`` exactly
    as production callers do: start at an eligible observation bar's END with
    the strategy's confirmation-bar count. DAILY semantics: close of the
    bars-th distinct trading day (establishment day counts as #1, day closes
    at its latest session end; nights/breaks/weekends never add observations;
    insufficient coverage returns None). MINUTE keeps the planned-slot
    computation with the historical tail fallback. The helper is owned by the
    critical owner; a defect is reported to them, never substituted locally.
    A POSSIBLE verdict proves only structural timing possibility, never
    admission.
    """
    from ak_trading.v2.strategies import registry as strategy_registry

    helper = getattr(strategy_registry, "_planned_setup_expiry_nano", None)
    if helper is None:
        return {
            "verdict": UNKNOWN,
            "scope": "every configured risk-taking cell",
            "evidence": {
                "reason": (
                    "K01 planned-expiry calendar helper (_planned_setup_expiry_nano) is not "
                    "present in this source revision; no local timing alternative is "
                    "manufactured and no timing acceptance is claimed"
                )
            },
            "bound": "none; helper absent",
        }
    registry_path = Path(strategy_registry.__file__).resolve()
    helper_sha = sha256_file(registry_path)
    sessions = tuple(bundle.sessions)
    evidence: dict = {}
    verdicts = []
    for cell in cells:
        spec, product, contract_id = cell["spec"], cell["product"], cell["contract_id"]
        key = f"{spec.strategy_id}|{product}"
        if key in evidence:
            continue
        duration = 60 if spec.mode == "MINUTE" else 86400
        eligible = _eligible_bars(bundle.bars, contract_id, duration, windows)
        confirmation = int(spec.parameters["entry_confirmation_bars"])
        delays = [bar.available_at_nano - bar.end_nano for bar in eligible]
        record: dict = {
            "scope": key,
            "helper": "ak_trading.v2.strategies.registry._planned_setup_expiry_nano",
            "helper_source_sha256": helper_sha,
            "mode": spec.mode,
            "confirmation_bars": confirmation,
            "eligible_observation_bars": len(eligible),
            "product_calendar_days": len({s.trading_day for s in sessions if s.product == product}),
            "availability_delay_nanos": {
                "min": min(delays) if delays else None,
                "median": statistics.median(delays) if delays else None,
                "max": max(delays) if delays else None,
            },
        }
        evidence[key] = record
        if not eligible:
            record["verdict"] = UNKNOWN
            record["reason"] = "no eligible observation bars in the authorized window"
            verdicts.append(UNKNOWN)
            continue
        if len(eligible) > MAX_EXHAUSTIVE_INSTANTS:
            record["verdict"] = UNKNOWN
            record["reason"] = (
                f"{len(eligible)} eligible bars exceeds the bounded structural scan "
                f"{MAX_EXHAUSTIVE_INSTANTS}"
            )
            verdicts.append(UNKNOWN)
            continue
        witness = None
        helper_error = None
        for bar in eligible:
            try:
                expiry = helper(product, spec.mode, bar.end_nano, confirmation, sessions)
            except Exception as error:  # reviewed helper; surface, never bypass
                helper_error = f"{type(error).__name__}: {error}"
                break
            if expiry is not None:
                witness = {
                    "establishment_bar_end_nano": bar.end_nano,
                    "planned_expiry_nano": expiry,
                }
                break
        if helper_error:
            record["verdict"] = UNKNOWN
            record["reason"] = f"K01 helper raised {helper_error}"
            verdicts.append(UNKNOWN)
        elif witness:
            record["witness"] = witness
            record["verdict"] = POSSIBLE
            verdicts.append(POSSIBLE)
        else:
            record["verdict"] = UNKNOWN
            record["reason"] = (
                "no eligible establishment admits a completable setup span under the accepted "
                "K01 calendar semantics (DAILY insufficient coverage returns None; no "
                "extrapolation)"
            )
            verdicts.append(UNKNOWN)
    overall = POSSIBLE if any(v == POSSIBLE for v in verdicts) else UNKNOWN
    return {
        "verdict": overall,
        "scope": "structural timing possibility per configured cell",
        "evidence": evidence,
        "bound": "passing proves only structural timing possibility, never admission",
    }


# ---------------------------------------------------------------------------
# Aggregation and reporting
# ---------------------------------------------------------------------------

def _cell_verdict_rows(cells, checks) -> list[dict]:
    """Per-cell rows joined EXACTLY to the cell's own evidence scope.

    Calibration evidence is keyed strategy|product|mode|horizon and payoff
    evidence strategy|product; a row never borrows another product's or
    another horizon's verdict — a missing exact match stays UNKNOWN.
    """
    rows = []
    for cell in cells:
        spec = cell["spec"]
        label = f"{spec.strategy_id}|{cell['contract_id']}"
        horizon = spec.max_holding
        calibration_key = f"{spec.strategy_id}|{cell['product']}|{spec.mode}|{horizon.unit}={horizon.value}"
        payoff_key = f"{spec.strategy_id}|{cell['product']}"
        verdicts = {
            "capital": checks["capital"]["evidence"].get(label, {}).get("verdict", UNKNOWN),
            "calibration": checks["calibration"]["evidence"].get(calibration_key, {}).get("verdict", UNKNOWN),
            "payoff_support": checks["payoff_support"]["evidence"].get(payoff_key, {}).get("verdict", UNKNOWN),
            "confirmation": checks["confirmation"]["evidence"].get(payoff_key, {}).get("verdict", UNKNOWN),
            "data_calendar": checks["data_calendar"]["verdict"],
        }
        rows.append({
            "cell": label,
            "family": spec.family,
            "verdicts": verdicts,
            "proven_impossible": any(v == IMPOSSIBLE for v in verdicts.values()),
            "fully_possible": all(v == POSSIBLE for v in verdicts.values()),
        })
    return rows


def _aggregate_structural_empty(cells, checks) -> tuple[object, list[dict], list[str]]:
    notes = []
    if not cells:
        notes.append("no risk-taking cells configured (only WAIT/CASH or no contracts); vacuously empty")
        return True, [], notes
    rows = _cell_verdict_rows(cells, checks)
    if any(entry["fully_possible"] for entry in rows):
        return False, rows, notes
    if all(entry["proven_impossible"] for entry in rows):
        return True, rows, notes
    if checks["confirmation"]["verdict"] != POSSIBLE:
        notes.append(
            "structurally_empty=false requires every check (including confirmation) to pass on at "
            "least one cell; the confirmation check is pending accepted K01 semantics, so this run "
            "cannot conclude false"
        )
    return None, rows, notes


def _render_markdown(report: dict) -> str:
    lines = [
        "# v2 research preflight (K04)",
        "",
        f"- generated_at: {report['generated_at']}",
        f"- schema: {report['schema']}",
        f"- config: {report['inputs']['config']['path']} (sha256 {report['inputs']['config']['sha256'][:16]}…, run {report['inputs']['config']['run_id']})",
        f"- bundle: {report['inputs']['bundle'].get('path')} (manifest sha256 {str(report['inputs']['bundle'].get('manifest_sha256'))[:16]}…, dataset_kind {report['inputs']['bundle'].get('dataset_kind')})",
        f"- payload_read: {report['inputs']['bundle']['loaded']}",
        f"- structurally_empty: {report['structurally_empty']} (not a model-fit or statistical PASS)",
        f"- exit: {report['exit']}",
        "",
        "## Checks",
        "",
        "| check | verdict | note |",
        "| --- | --- | --- |",
    ]
    for name in ("capital", "calibration", "payoff_support", "data_calendar", "confirmation"):
        check = report["checks"][name]
        note = check.get("evidence", {}).get("reason", "")
        if not isinstance(note, str):
            note = ""
        lines.append(f"| {name} | {check['verdict']} | {note} |")
    lines += ["", "## Cells", "", "| cell | capital | calibration | payoff | confirmation | data | status |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for cell in report.get("cells", []):
        verdicts = cell["verdicts"]
        status = "proven_impossible" if cell["proven_impossible"] else (
            "fully_possible" if cell["fully_possible"] else "unresolved")
        lines.append(
            f"| {cell['cell']} | {verdicts['capital']} | {verdicts['calibration']} | "
            f"{verdicts['payoff_support']} | {verdicts['confirmation']} | "
            f"{verdicts['data_calendar']} | {status} |"
        )
    if report["inputs"].get("config", {}).get("synthetic") or report["inputs"]["bundle"].get("dataset_kind") == "SYNTHETIC":
        lines += ["", "Inputs are SYNTHETIC; nothing here is historical PIT evidence."]
    lines += ["", "## Notes", ""]
    lines += [f"- {note}" for note in report["notes"]] or ["- none"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="preflight_v2_research.py",
        description="Read-only structural feasibility preflight for v2 research (K04).",
    )
    parser.add_argument("--input-bundle", required=True,
                        help="frozen input package directory, or an H7 task metadata JSON with selected_input null")
    parser.add_argument("--config", required=True, help="research config JSON (decoded by experiments.parse_config)")
    parser.add_argument("--output", required=True, help="fresh external output directory (must not exist)")
    parser.add_argument("--read-scope", help="external read-scope-v1 authorization metadata (required for payload reads)")
    return parser


def _resolve_input_bundle(path_value: str):
    """Classify the input-bundle argument: H7 null selection vs frozen package."""
    path = Path(path_value)
    if path.is_dir():
        return {"kind": "directory", "path": path}
    if path.is_file() and path.suffix == ".json":
        payload = read_json_strict(path, "input bundle metadata")
        if isinstance(payload, dict) and "selected_input" in payload and payload["selected_input"] is None:
            raise PrerequisiteUnknown(
                "actual-H7 metadata has selected_input=null: the preflight input bundle is a missing "
                "prerequisite; this tool never selects r1/r2 inputs and never invents reference prices/ATR"
            )
        raise InvalidInput(
            "input bundle is a JSON file but not an H7 null-selection metadata record; "
            "expected a frozen input package directory"
        )
    raise InvalidInput(f"input bundle path is neither a directory nor a JSON file: {path_value}")


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_argument_parser()
    options = parser.parse_args(arguments)

    generated_at = _now()
    script_sha = sha256_file(Path(__file__).resolve())

    try:
        config_path = Path(options.config)
        if not config_path.is_file():
            raise InvalidInput(f"config file not found: {options.config}")
        config_sha = sha256_file(config_path)
        config_payload = read_json_strict(config_path, "research config")
        try:
            splits, policies, holdout_scope = parse_config(config_payload)
        except InvalidInput:
            raise
        except Exception as error:
            raise InvalidInput(
                f"research config rejected by experiments.parse_config: "
                f"{type(error).__name__}: {error}"
            ) from error
        research = m1_policy.research_policy()
        bundle_info = _resolve_input_bundle(options.input_bundle)

        output_path = Path(options.output)
        if output_path.exists() or output_path.is_symlink():
            raise InvalidInput(f"output path already exists, refusing to overwrite: {options.output}")
        resolved_output = output_path.resolve()
        if bundle_info["kind"] == "directory":
            resolved_bundle = bundle_info["path"].resolve()
            if resolved_output == resolved_bundle or resolved_bundle in resolved_output.parents:
                raise InvalidInput("output directory must not live inside the input bundle")
        if not output_path.parent.exists():
            raise InvalidInput(f"output parent directory does not exist: {output_path.parent}")

        inputs_block = {
            "config": {
                "path": str(config_path), "sha256": config_sha,
                "run_id": config_payload.get("run_id"),
                "strategy_ids": list(config_payload.get("strategy_ids", [])),
                "contract_ids": list(config_payload.get("contract_ids", [])),
                "evaluation_as_of_nano": config_payload.get("evaluation_as_of_nano"),
                "initial_cash_cny": config_payload.get("initial_cash_cny"),
                "synthetic": config_payload.get("synthetic"),
            },
            "bundle": {
                "path": str(bundle_info["path"]), "loaded": False,
                "dataset_kind": None, "manifest_sha256": None,
            },
            "read_scope": {
                "provided": options.read_scope is not None,
                "path": options.read_scope, "sha256": None,
                "authorization_reference": None,
                "status": None,
            },
        }

        checks = {}
        notes = []
        cells = []
        windows: list[tuple[int, int]] = []
        bundle = None
        manifest_meta = None

        specs = _cell_specs(config_payload.get("strategy_ids", []))
        scope_problem = None
        if options.read_scope is None:
            scope_problem = "no read-scope authorization supplied; config split declarations alone are not read permission"
        else:
            scope_path = Path(options.read_scope)
            if not scope_path.is_file():
                raise InvalidInput(f"read-scope file not found: {options.read_scope}")
            scope_sha = sha256_file(scope_path)
            scope = read_json_strict(scope_path, "read scope")
            inputs_block["read_scope"].update({
                "sha256": scope_sha,
                "authorization_reference": scope.get("authorization_reference") if isinstance(scope, dict) else None,
            })
            try:
                windows = validate_read_scope(
                    scope, config_sha=config_sha, bundle_dir=bundle_info["path"], splits=splits,
                    label="read scope",
                )
                inputs_block["read_scope"]["status"] = "AUTHORIZED_NON_HOLDOUT"
            except PrerequisiteUnknown as error:
                scope_problem = str(error)
                inputs_block["read_scope"]["status"] = "REFUSED"

        if scope_problem is None:
            bundle = read_frozen_input(bundle_info["path"])
            manifest_path = bundle_info["path"] / "manifest.json"
            manifest_meta = read_json_strict(manifest_path, "bundle manifest")
            inputs_block["bundle"].update({
                "loaded": True,
                "dataset_kind": manifest_meta.get("dataset_kind"),
                "manifest_sha256": sha256_file(manifest_path),
                "bar_count": len(bundle.bars),
                "session_count": len(bundle.sessions),
                "instrument_count": len(bundle.instruments),
            })

        # Build risk cells (WAIT never opens risk and is excluded).
        configured = list(config_payload["strategy_ids"])
        wait_ids = [sid for sid in configured if specs[sid].family == "WAIT"]
        if wait_ids:
            notes.append(f"WAIT strategies configured and excluded from risk cells: {sorted(wait_ids)}")
        if bundle is not None:
            whitelisted = [i for i in bundle.instruments if i.contract_id in set(config_payload["contract_ids"])]
            if not whitelisted:
                notes.append("no whitelisted contracts present in the bundle; no risk cells")
            for instrument in whitelisted:
                for sid in sorted(configured):
                    spec = specs[sid]
                    if spec.family == "WAIT":
                        continue
                    cells.append({
                        "spec": spec, "product": instrument.product,
                        "contract_id": instrument.contract_id, "windows": windows,
                    })

        equity = _decimal(config_payload["initial_cash_cny"], "initial_cash_cny")
        evaluation_as_of = config_payload["evaluation_as_of_nano"]

        if bundle is None:
            reason = scope_problem or "payload not loaded"
            checks = {name: {
                "verdict": UNKNOWN, "scope": "payload-dependent check",
                "evidence": {"reason": reason}, "bound": "no authorized payload; UNKNOWN before read_frozen_input",
            } for name in ("capital", "calibration", "payoff_support", "data_calendar", "confirmation")}
            structurally_empty = None
            report_cells = []
            notes.append(f"payload checks UNKNOWN before any payload read: {reason}")
        else:
            checks["capital"] = check_capital(cells, bundle, equity, research, evaluation_as_of)
            checks["calibration"] = check_calibration(cells, bundle, windows, config_payload)
            checks["payoff_support"] = check_payoff(cells, bundle, windows, research)
            checks["data_calendar"] = check_data_calendar(bundle, manifest_meta["dataset_kind"], manifest_meta, windows)
            checks["confirmation"] = check_confirmation(cells, bundle, windows)
            structurally_empty, report_cells, aggregate_notes = _aggregate_structural_empty(cells, checks)
            notes.extend(aggregate_notes)

        exit_code = {
            False: EXIT_OK_POSSIBLE, True: EXIT_EMPTY, None: EXIT_UNKNOWN,
        }[structurally_empty]
        report = {
            "schema": SCHEMA,
            "generated_at": generated_at,
            "tool": {
                "script": "scripts/preflight_v2_research.py",
                "script_sha256": script_sha,
                "v2_code_file_count": len(code_hashes()),
            },
            "inputs": inputs_block,
            "checks": checks,
            "cells": report_cells,
            "structurally_empty": structurally_empty,
            "exit": exit_code,
            "notes": notes,
        }
        output_path.mkdir(parents=True, exist_ok=False)
        write_new_json(output_path / "preflight.json", report, "preflight.json")
        write_new_text(output_path / "preflight.md", _render_markdown(report), "preflight.md")
        print(
            f"preflight complete: structurally_empty={report['structurally_empty']} exit={exit_code}; "
            f"output={output_path}"
        )
        return exit_code
    except InvalidInput as error:
        print(f"INVALID INPUT: {error}", file=sys.stderr)
        return EXIT_INVALID
    except PrerequisiteUnknown as error:
        print(f"UNKNOWN PREREQUISITE: {error}", file=sys.stderr)
        return EXIT_UNKNOWN
    except Exception as error:  # unexpected failure; never silenced
        print(f"UNEXPECTED FAILURE: {type(error).__name__}: {error}", file=sys.stderr)
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
