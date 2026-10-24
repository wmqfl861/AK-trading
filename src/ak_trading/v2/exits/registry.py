"""Parallel ExitRegistry for the M3C exit library (08-M3C §4.1/§4.2, P1+P2).

P1 registered LEGACY / TRAILING_STOP / full-close TIME_DECAY in both
modes; P2 adds PROFIT_LADDER, VOLATILITY_STOP, DRAWDOWN_EXIT and the
PARTIAL_LIQUIDATION sizing combiner, plus the two-stage TIME_DECAY
parameter identity (08 §5.4 ``:params-2``). Fresh registration ids
(``exit_time_decay_{m1,d1}_v2``) keep the registry keyed uniquely while
``parameter_version`` carries the params-2 label; in-flight P1 bindings
keep resolving against their own specs — nothing is migrated in place.

The registry is keyed by ``exit_policy_id`` and never re-derives binding
identity: the single authoritative binding hash lives in M1
``contracts.exit_binding_content_hash`` (OQ-4); ``exit_registry_sha256``
here hashes the sorted full ExitSpec array — the same semantics as M4's
``exit_registry_hash_for``, kept identical by test.

``evaluate_exit`` runs every bound policy and returns the pure candidate
tuple (PARTIAL_LIQUIDATION is a combiner, not a candidate producer);
``arbitrate_exit`` — implemented in :mod:`arbitration` since P2 (D3
order 3: exactly one arbitrator, no divergent P1 copy left behind) —
folds the candidates under the D3 take-min rule. All emitted reasons are
vocabulary-checked to never contain the ``STOP``/``TARGET``/``ROLL``
substrings so the current reason-sniffing purpose router in
``decision/replay.py::intents_for_proposal`` cannot misroute a new-policy
exit into the legacy protective matching path (08 §7.1; the definitive
fix — purpose keyed off ``ExitExecutionLink.execution_kind`` — belongs to
the M4 owner).
"""
from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Literal, Mapping, Protocol

from ak_trading.v2.artifacts import canonical_json_bytes, sha256_hex
from ak_trading.v2.contracts import (
    ExitContext,
    ExitProposal,
    ExitSpec,
    ExitState,
)
from ak_trading.v2.exits.arbitration import arbitrate_exit  # noqa: F401  (single authoritative arbitrator)
from ak_trading.v2.exits.drawdown import DrawdownExitPolicy
from ak_trading.v2.exits.signal_reversal import (  # noqa: F401
    VARIANT_PRICE_ONLY, VARIANT_VOLUME, SignalReversalPolicy,
)
from ak_trading.v2.exits.legacy import LegacyExitPolicy
from ak_trading.v2.exits.profit_ladder import ProfitLadderPolicy
from ak_trading.v2.exits.time_decay import TimeDecayPolicy
from ak_trading.v2.exits.trailing import TrailingStopPolicy
from ak_trading.v2.exits.volatility_stop import VolatilityStopPolicy
from ak_trading.v2.schemas import encode_record

M3C_REGISTRY_VERSION = "1.1.0"
MODE_SUFFIX: Mapping[str, str] = {"MINUTE": "m1", "DAILY": "d1"}
MODE_DURATION: Mapping[str, int] = {"MINUTE": 60, "DAILY": 86400}
P1_ENTRY_FAMILIES: tuple[str, ...] = ("BREAKOUT", "PULLBACK", "TREND")
LEGACY_ENTRY_FAMILIES: tuple[str, ...] = (
    "BREAKOUT", "GRID", "MARTINGALE", "PULLBACK", "REVERSION", "TREND")
LEGACY_RULES_VERSION = "m3b-legacy-rules-v2.1.2"

TRAILING_PARAMETERS: dict[str, object] = {
    "arm_profit_r": "0.5", "trail_atr_multiple": "1",
    "atr_basis": "FROZEN_ATR", "peak_source": "COMPLETED_CLOSE",
}
TIME_DECAY_P1_PARAMETERS: dict[str, object] = {
    "soft_time_fractions": ["0.75"],
    "minimum_progress_r": ["0.50"],
    "cumulative_close_fractions": ["1"],
}
TIME_DECAY_P2_PARAMETERS: dict[str, object] = {
    "soft_time_fractions": ["0.50", "0.75"],
    "minimum_progress_r": ["0.25", "0.50"],
    "cumulative_close_fractions": ["0.50", "1"],
}
PROFIT_LADDER_PARAMETERS: dict[str, object] = {
    "profit_levels_r": ["0.5", "1.0", "1.5"],
    "cumulative_close_fractions": ["0.33", "0.67", "1"],
    "confirmation_bars": 1,
}
VOLATILITY_STOP_PARAMETERS: dict[str, object] = {
    "atr_basis": "BASE_ATR14", "atr_multiple": "1", "confirmation_bars": 1,
}
DRAWDOWN_PARAMETERS: dict[str, object] = {
    "arm_peak_profit_r": "1", "max_giveback_r": "0.5",
}
SCALE_OUT_PARAMETERS: dict[str, object] = {
    "sizing_mode": "CUMULATIVE_ORIGINAL_LOTS", "lot_rounding": "FLOOR_CLOSE",
    "final_step": "ALL_REMAINDER",
}
SIGNAL_REVERSAL_PARAMETERS: dict[str, object] = {"confirmation_bars": 2}

FORBIDDEN_REASON_SUBSTRINGS: tuple[str, ...] = ("STOP", "TARGET", "ROLL")


class ExitPolicy(Protocol):
    """The frozen policy protocol (08 §4.2)."""

    @property
    def spec(self) -> ExitSpec: ...

    def evaluate(self, context: ExitContext,
                 previous: ExitState | None) -> ExitProposal: ...


_CODE_SHA_CACHE: dict[tuple[str, str], str] = {}
_FAMILY_MODULES: Mapping[str, str] = {
    "LEGACY": "legacy.py",
    "TRAILING_STOP": "trailing.py",
    "TIME_DECAY": "time_decay.py",
    "PROFIT_LADDER": "profit_ladder.py",
    "VOLATILITY_STOP": "volatility_stop.py",
    "DRAWDOWN_EXIT": "drawdown.py",
    "PARTIAL_LIQUIDATION": "sizing.py",
    "SIGNAL_REVERSAL": "signal_reversal.py",
}
_SHARED_MODULES: tuple[str, ...] = ("registry.py", "features.py", "state.py",
                                    "arbitration.py")


def _code_sha256(family: str, variant: str = "") -> str:
    """Family code digest.

    Non-P3 families: SHA-256 over the family module plus the shared exits
    modules. SIGNAL_REVERSAL uses the P3-OQ-1 variant-level digest: both
    variants share one module but MUST hash differently, binding the
    actual variant label, its fixed entrypoint and the full sorted source
    set (project-relative paths, no timestamps) into the material.
    """
    cache_key = (family, variant)
    cached = _CODE_SHA_CACHE.get(cache_key)
    if cached is not None:
        return cached
    root = Path(__file__).resolve().parent
    if family != "SIGNAL_REVERSAL":
        digest = sha256()
        for name in (_FAMILY_MODULES[family], *_SHARED_MODULES):
            digest.update((root / name).read_bytes())
            digest.update(b"\n")
        value = digest.hexdigest()
        _CODE_SHA_CACHE[cache_key] = value
        return value
    from ak_trading.v2.exits.signal_reversal import VARIANT_ENTRYPOINTS
    sources = sorted({f"src/ak_trading/v2/exits/{name}"
                      for name in (_FAMILY_MODULES[family], *_SHARED_MODULES)})
    material = {
        "domain": "m3c-signal-reversal-code-v1",
        "family": "SIGNAL_REVERSAL",
        "variant": variant,
        "entrypoint": VARIANT_ENTRYPOINTS[variant],
        "sources": [{"path": path,
                     "sha256": sha256((root / Path(path).name).read_bytes()).hexdigest()}
                    for path in sources],
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False) + "\n"
    value = sha256(canonical.encode("utf-8")).hexdigest()
    _CODE_SHA_CACHE[cache_key] = value
    return value


def _market_keys(mode: str, names: tuple[str, ...]) -> tuple[str, ...]:
    duration = MODE_DURATION[mode]
    return tuple(sorted(f"BASE:{name}:v2.1.0:d{duration}" for name in names))


def _state_keys(mode: str, names: tuple[str, ...]) -> tuple[str, ...]:
    duration = MODE_DURATION[mode]
    return tuple(sorted(f"EXITSTATE:{name}:v1.0.0:d{duration}" for name in names))


def _exit_policy_id(family_id: str, mode: str) -> str:
    return f"{family_id}_{MODE_SUFFIX[mode]}_v1"


def _spec(policy_id: str, family: str, mode: str, parameters: dict[str, object],
          market_names: tuple[str, ...], state_names: tuple[str, ...],
          entry_families: tuple[str, ...], execution_timing: str,
          parameter_suffix: str = ":params-1",
          variant: str = "") -> ExitSpec:
    return ExitSpec(
        exit_policy_id=policy_id, family=family, mode=mode,  # type: ignore[arg-type]
        version="1.0.0", parameter_version=f"{policy_id}{parameter_suffix}",
        code_sha256=_code_sha256(family, variant),
        parameters={key: (list(value) if isinstance(value, list) else value)
                    for key, value in parameters.items()},
        required_market_keys=_market_keys(mode, market_names),
        required_state_keys=_state_keys(mode, state_names),
        applicable_entry_families=entry_families,  # type: ignore[arg-type]
        observation_duration_seconds=MODE_DURATION[mode],
        execution_timing=execution_timing,  # type: ignore[arg-type]
        validation_stage="RESEARCH")


def legacy_exit_spec(mode: Literal["MINUTE", "DAILY"]) -> ExitSpec:
    return _spec(_exit_policy_id("exit_legacy", mode), "LEGACY", mode, {},
                 (), (), LEGACY_ENTRY_FAMILIES, "LEGACY_PROTECTIVE")


def trailing_exit_spec(mode: Literal["MINUTE", "DAILY"]) -> ExitSpec:
    return _spec(
        _exit_policy_id("exit_trailing_atr", mode), "TRAILING_STOP", mode,
        TRAILING_PARAMETERS, ("close",),
        ("effective_stop_distance_atr", "episode_net_liquidation_r",
         "peak_favorable_price"), P1_ENTRY_FAMILIES, "NEXT_LEGAL_WINDOW")


def time_decay_exit_spec(mode: Literal["MINUTE", "DAILY"]) -> ExitSpec:
    return _spec(
        _exit_policy_id("exit_time_decay", mode), "TIME_DECAY", mode,
        TIME_DECAY_P1_PARAMETERS, (),
        ("elapsed_fraction", "episode_net_liquidation_r"),
        P1_ENTRY_FAMILIES, "NEXT_LEGAL_WINDOW")


def time_decay_stage_two_spec(mode: Literal["MINUTE", "DAILY"]) -> ExitSpec:
    """08 §5.4 two-stage TIME_DECAY ``:params-2`` identity.

    A fresh registration id keeps the id-keyed registry unique; the
    parameter_version carries the params-2 label. In-flight P1 bindings
    keep their params-1 identity — nothing is overwritten.
    """
    return _spec(
        f"exit_time_decay_{MODE_SUFFIX[mode]}_v2", "TIME_DECAY", mode,
        TIME_DECAY_P2_PARAMETERS, (),
        ("elapsed_fraction", "episode_net_liquidation_r"),
        P1_ENTRY_FAMILIES, "NEXT_LEGAL_WINDOW", parameter_suffix=":params-2")


def profit_ladder_exit_spec(mode: Literal["MINUTE", "DAILY"]) -> ExitSpec:
    return _spec(
        _exit_policy_id("exit_profit_ladder", mode), "PROFIT_LADDER", mode,
        PROFIT_LADDER_PARAMETERS, (),
        ("closed_fraction", "episode_net_liquidation_r"),
        P1_ENTRY_FAMILIES, "NEXT_LEGAL_WINDOW")


def volatility_stop_exit_spec(mode: Literal["MINUTE", "DAILY"]) -> ExitSpec:
    return _spec(
        _exit_policy_id("exit_volatility_stop", mode), "VOLATILITY_STOP",
        mode, VOLATILITY_STOP_PARAMETERS, ("atr14", "close"), (),
        P1_ENTRY_FAMILIES, "NEXT_LEGAL_WINDOW")


def drawdown_exit_spec(mode: Literal["MINUTE", "DAILY"]) -> ExitSpec:
    return _spec(
        _exit_policy_id("exit_episode_drawdown", mode), "DRAWDOWN_EXIT", mode,
        DRAWDOWN_PARAMETERS, (),
        ("episode_net_liquidation_r", "peak_net_liquidation_r",
         "profit_giveback_r"), P1_ENTRY_FAMILIES, "NEXT_LEGAL_WINDOW")


def scale_out_exit_spec(mode: Literal["MINUTE", "DAILY"]) -> ExitSpec:
    return _spec(
        _exit_policy_id("exit_scale_out", mode), "PARTIAL_LIQUIDATION", mode,
        SCALE_OUT_PARAMETERS, (), ("closed_fraction",),
        P1_ENTRY_FAMILIES, "NEXT_LEGAL_WINDOW")


def signal_reversal_exit_spec(mode: Literal["MINUTE", "DAILY"],
                                volume_confirmed: bool = True) -> ExitSpec:
    """P3-OQ-1: two independent registrations under one family.

    Volume-confirmed requires close/ma20/volume_ratio20; the no-volume
    ablation requires close/ma20 only and never reads volume. Both lock
    ``parameters={"confirmation_bars": 2}`` — a ``use_volume``-style key
    can never appear (whitelist-exact). Applicable families per OQ-4.
    """
    suffix = MODE_SUFFIX[mode]
    label = "" if volume_confirmed else "nomvol_"
    variant = VARIANT_VOLUME if volume_confirmed else VARIANT_PRICE_ONLY
    market_names = ("close", "ma20", "volume_ratio20") if volume_confirmed \
        else ("close", "ma20")
    return _spec(
        f"exit_signal_reversal_{label}{suffix}_v1", "SIGNAL_REVERSAL", mode,
        SIGNAL_REVERSAL_PARAMETERS, market_names, (), P1_ENTRY_FAMILIES,
        "NEXT_LEGAL_WINDOW", variant=variant)


def exit_registry_specs(mode: Literal["MINUTE", "DAILY"]) -> tuple[ExitSpec, ...]:
    """Every registered spec of one mode, sorted by exit_policy_id.

    P1: LEGACY / TRAILING_STOP / TIME_DECAY (params-1). P2 adds
    PROFIT_LADDER / VOLATILITY_STOP / DRAWDOWN_EXIT / PARTIAL_LIQUIDATION
    and the TIME_DECAY params-2 identity.
    """
    return tuple(sorted(
        (legacy_exit_spec(mode), trailing_exit_spec(mode),
         time_decay_exit_spec(mode), time_decay_stage_two_spec(mode),
         profit_ladder_exit_spec(mode), volatility_stop_exit_spec(mode),
         drawdown_exit_spec(mode), scale_out_exit_spec(mode),
         signal_reversal_exit_spec(mode, volume_confirmed=True),
         signal_reversal_exit_spec(mode, volume_confirmed=False)),
        key=lambda spec: spec.exit_policy_id))


def exit_registry_v1(mode: Literal["MINUTE", "DAILY"]) -> Mapping[str, ExitPolicy]:
    """Registry keyed by exit_policy_id (08 §4.2 signature).

    PARTIAL_LIQUIDATION is deliberately absent from the policy mapping:
    it is a sizing combiner consumed inside the trigger families, not a
    candidate producer (D3 — no second synonymous reduce ticket).
    """
    policies: dict[str, ExitPolicy] = {}
    for spec in exit_registry_specs(mode):
        if spec.family == "PARTIAL_LIQUIDATION":
            continue
        if spec.family == "LEGACY":
            policies[spec.exit_policy_id] = LegacyExitPolicy(spec)
        elif spec.family == "TRAILING_STOP":
            policies[spec.exit_policy_id] = TrailingStopPolicy(spec)
        elif spec.family == "TIME_DECAY":
            policies[spec.exit_policy_id] = TimeDecayPolicy(spec)
        elif spec.family == "PROFIT_LADDER":
            policies[spec.exit_policy_id] = ProfitLadderPolicy(spec)
        elif spec.family == "VOLATILITY_STOP":
            policies[spec.exit_policy_id] = VolatilityStopPolicy(spec)
        elif spec.family == "DRAWDOWN_EXIT":
            policies[spec.exit_policy_id] = DrawdownExitPolicy(spec)
        elif spec.family == "SIGNAL_REVERSAL":
            has_volume = any(":volume_ratio20:" in key
                             for key in spec.required_market_keys)
            policies[spec.exit_policy_id] = SignalReversalPolicy(
                spec, VARIANT_VOLUME if has_volume else VARIANT_PRICE_ONLY)
        else:
            raise ValueError(f"registry_does_not_implement:{spec.family}")
    return policies


def exit_spec_sha256(spec: ExitSpec) -> str:
    """Canonical content digest of one ExitSpec (binding reference form)."""
    return sha256_hex(canonical_json_bytes([encode_record(spec)]))


def exit_registry_sha256(exit_specs: tuple[ExitSpec, ...]) -> str:
    """sha256 over the exit_policy_id-sorted full ExitSpec record array."""
    if len({spec.exit_policy_id for spec in exit_specs}) != len(exit_specs):
        raise ValueError("duplicate_exit_specs")
    ordered = sorted(exit_specs, key=lambda spec: spec.exit_policy_id)
    return sha256_hex(canonical_json_bytes([encode_record(spec)
                                            for spec in ordered]))


def assert_reason_vocabulary(reasons: tuple[str, ...]) -> None:
    """New-policy reasons must never collide with legacy purpose words.

    ``intents_for_proposal`` currently sniffs the substrings
    STOP/TARGET/ROLL out of proposal reasons to pick the intent purpose;
    an M3C reason containing them would misroute a NEXT_WINDOW_CLOSE exit
    into the legacy protective matching path (08 §7.1 mis-routing hazard).
    """
    for reason in reasons:
        for word in FORBIDDEN_REASON_SUBSTRINGS:
            if word in reason:
                raise ValueError(
                    f"exit_reason_forbidden_substring:{word}:{reason}")


def evaluate_exit(context: ExitContext,
                  previous: ExitState | None) -> tuple[ExitProposal, ...]:
    """Evaluate every bound policy; pure candidates only (08 §4.2)."""
    if not context.exit_specs:
        raise ValueError("exit_context_requires_bound_specs")
    durations = {spec.observation_duration_seconds for spec in context.exit_specs}
    if len(durations) != 1:
        raise ValueError("bound_exit_specs_must_share_one_observation_duration")
    duration = durations.pop()
    mode = "MINUTE" if duration == 60 else "DAILY"
    registry = exit_registry_v1(mode)  # type: ignore[arg-type]
    spec_by_id = {spec.exit_policy_id: spec
                  for spec in exit_registry_specs(mode)}  # type: ignore[arg-type]
    bound_hashes = set(context.exit_binding.exit_spec_sha256s)
    proposals: list[ExitProposal] = []
    for spec in context.exit_specs:
        if exit_spec_sha256(spec) not in bound_hashes:
            raise ValueError("bound_spec_hash_missing_from_binding")
        registered = spec_by_id.get(spec.exit_policy_id)
        if registered is None:
            raise ValueError(f"exit_policy_not_in_registry:{spec.exit_policy_id}")
        if registered != spec:
            raise ValueError("bound_spec_must_equal_registry_spec")
        if spec.family == "PARTIAL_LIQUIDATION":
            # Sizing combiner: validated above, produces no candidate.
            continue
        policy = registry.get(spec.exit_policy_id)
        if policy is None:
            raise ValueError(f"exit_policy_not_implemented:{spec.exit_policy_id}")
        proposal = policy.evaluate(context, previous)
        assert_reason_vocabulary(proposal.reasons)
        proposals.append(proposal)
    return tuple(proposals)

