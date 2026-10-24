"""SIGNAL_REVERSAL price-volume reversal exit (08-M3C §5.2/§5.4, P3; OQ-1—4).

A deterministic directional-evidence trigger — NOT a price line, NOT an
arm latch, NOT an economic value: the only persistent state is the
``(exit_policy_id, rule_id="trigger")`` soft confirmation inside
``ExitState.confirmations``.

Two fixed entrypoints under one family (P3-OQ-1 — same ``SIGNAL_REVERSAL``
family, independent registrations; no ``use_volume``-style runtime key
may ever appear, the whitelist is exactly ``{"confirmation_bars"}``):

* :meth:`SignalReversalPolicy.evaluate_volume_confirmed` — per completed
  observation: direction leg ``s*(C - MA20) < 0`` (STRICT; equality at the
  MA is not a reversal) AND volume leg ``volume_ratio20 >= 1.5`` (equality
  counts, 08 §10-7) — the no-subtraction BASE ratio, never VOI001.
* :meth:`SignalReversalPolicy.evaluate_price_only` — the no-volume-filter
  ablation: the direction leg alone; a missing/invalid volume field is
  irrelevant to it and never blocks confirmation.

Trigger: ``confirmation_bars`` (first round: 2) consecutive DISTINCT
qualifying completed observations of the same mode -> ``EXIT`` /
``target_lots=0`` / earliest next legal window. No runtime confidence
threshold exists (08 §7.5: exits need no 0.70/0.85 gate; the legacy
0.65/0.80/UNKNOWN/false-break channels run in M4 without waiting for
this family's second bar).

Slot discipline (P3-OQ-2, on top of OQ-6):

* Slot id = ``exit_observation_slot_id(contract, duration,
  snapshot.decision_nano)``; a repeated evaluation of the same slot never
  re-counts, never resets and never advances the chain.
* "Consecutive" means ADJACACENT legal slots per the verifiable session
  calendar — never ``new_id != old_id`` alone. A covered-but-missing
  expected slot between the last counted slot and the current one is a
  gap: the counter resets to 0 FIRST, then the current qualifying
  observation starts a new sequence at 1.
* Expected-slot-due but channel pending / snapshot missing / required
  evidence invalid -> count = 0 (evidence interruption); the real slot id
  is recorded only when a verifiable observation slot exists, otherwise
  the previous id is kept. Normal closures (lunch, close, weekend —
  proven by the calendar) create no expected slot and never reset.
* Unprovable calendar: the current slot may at most start a sequence at
  1; it can never continue an unverifiable adjacency to 2.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from ak_trading.v2.contracts import (
    ExitConfirmation, ExitContext, ExitProposal, ExitSpec, ExitState,
    SessionWindow, exit_observation_slot_id,
)
from ak_trading.v2.exits.features import (
    MarketChannelUnavailable, MarketKeyFacts, collect_exit_facts,
    exit_digest, next_window_start, resolve_market_snapshot,
)
from ak_trading.v2.exits.state import advance_state, base_state_for

# 08 §5.4: rule-version constants — NOT parameters (the whitelist locks
# parameters to confirmation_bars only).
VOLUME_RATIO_THRESHOLD = Decimal("1.5")

VARIANT_VOLUME = "PRICE_MA20_VOLUME_RATIO20_GE_1_5"
VARIANT_PRICE_ONLY = "PRICE_MA20_ONLY"
VARIANTS = frozenset({VARIANT_VOLUME, VARIANT_PRICE_ONLY})
VARIANT_ENTRYPOINTS: Mapping[str, str] = {
    VARIANT_VOLUME: "evaluate_volume_confirmed",
    VARIANT_PRICE_ONLY: "evaluate_price_only",
}

SIGNAL_REASON_VOCABULARY = frozenset({
    "SIGNAL_REVERSAL_CONFIRMED", "SIGNAL_REVERSAL_CONFIRMING",
    "SIGNAL_REVERSAL_RESET", "SIGNAL_REVERSAL_KEPT",
    "SIGNAL_NO_REMAINING_LOTS", "SIGNAL_MARKET_CHANNEL_PENDING",
    "SIGNAL_CALENDAR_UNKNOWN", "SIGNAL_GAP_RESET",
    "MARKET_CHANNEL_PENDING_M1_D2_TYPE", "MARKET_SNAPSHOT_MISSING",
    "MARKET_SNAPSHOT_AMBIGUOUS", "MARKET_SNAPSHOT_DUPLICATED",
    "MARKET_KEY_MISSING_CLOSE", "MARKET_KEY_NOT_READY_CLOSE",
    "MARKET_VALUE_INVALID_CLOSE", "MARKET_KEY_NOT_VISIBLE_CLOSE",
    "MARKET_KEY_DUPLICATED_CLOSE", "MARKET_KEY_MISSING_MA20",
    "MARKET_KEY_NOT_READY_MA20", "MARKET_VALUE_INVALID_MA20",
    "MARKET_KEY_NOT_VISIBLE_MA20", "MARKET_KEY_DUPLICATED_MA20",
    "MARKET_KEY_MISSING_VOLUMERATIO20", "MARKET_KEY_NOT_READY_VOLUMERATIO20",
    "MARKET_VALUE_INVALID_VOLUMERATIO20", "MARKET_KEY_NOT_VISIBLE_VOLUMERATIO20",
    "MARKET_KEY_DUPLICATED_VOLUMERATIO20",
})

MINUTE_NANO = 60_000_000_000


def _market_names(spec: ExitSpec) -> tuple[str, ...]:
    duration = spec.observation_duration_seconds
    names = ["close", "ma20"]
    # Variant consistency is enforced in __post_init__; the key set is read
    # from the spec itself, not from id substrings (OQ-1 order 1).
    keys = {key.split(":")[1] for key in spec.required_market_keys}
    if "volume_ratio20" in keys:
        names.append("volume_ratio20")
    return tuple(f"BASE:{name}:v2.1.0:d{duration}" for name in names)


def _expected_slots_between(sessions: tuple[SessionWindow, ...], prev_end: int,
                            boundary_end: int, duration_seconds: int) -> int:
    """Legal completed slot ends in the OPEN interval (prev_end, boundary_end).

    Minute mode: a boundary e is an expected slot end iff the minute
    (e-60s, e] intersects an open session. Daily mode: each distinct
    trading day whose latest session end falls in the interval. Normal
    closures (lunch/weekend, proven by the calendar) create no expected
    slot; a covered-but-missing boundary does (the gap case, P3-OQ-2).
    """
    if boundary_end <= prev_end:
        return 0
    if duration_seconds == 60:
        count = 0
        step = MINUTE_NANO
        boundary = (prev_end // step + 1) * step
        while boundary < boundary_end:
            if any(session.start_nano < boundary
                   and session.end_nano > boundary - step
                   for session in sessions):
                count += 1
            boundary += step
        return count
    ends: dict[str, int] = {}
    for session in sessions:
        marker = session.trading_day.isoformat()
        ends[marker] = max(ends.get(marker, 0), session.end_nano)
    return sum(1 for end_nano in ends.values()
               if prev_end < end_nano < boundary_end)


def _slot_id(context: ExitContext, duration: int, slot_end: int) -> str:
    return exit_observation_slot_id(context.contract_id, duration, slot_end)


def _previous_confirmation(base: ExitState, policy_id: str):
    for item in base.confirmations:
        if item.exit_policy_id == policy_id and item.rule_id == "trigger":
            return item.count, item.last_observation_id
    return 0, None


def _parse_slot(last_observation_id: str | None, contract_id: str):
    """Recover the slot end nano from a canonical OQ-6 slot id."""
    if last_observation_id is None:
        return None
    import json
    try:
        parsed = json.loads(last_observation_id)
    except Exception:
        return None
    if (isinstance(parsed, list) and len(parsed) == 3
            and parsed[0] == contract_id and isinstance(parsed[2], int)):
        return parsed[2]
    return None


@dataclass(frozen=True)
class SignalReversalPolicy:
    """The P3 price-volume reversal ExitPolicy (both fixed variants)."""

    _spec: ExitSpec
    variant: str

    def __post_init__(self) -> None:
        if self._spec.family != "SIGNAL_REVERSAL":
            raise ValueError("spec_family_must_be_SIGNAL_REVERSAL")
        if self.variant not in VARIANTS:
            raise ValueError("spec_variant_unknown")
        keys = {key.split(":")[1] for key in self._spec.required_market_keys}
        has_volume_key = "volume_ratio20" in keys
        if self.variant == VARIANT_VOLUME and not has_volume_key:
            raise ValueError("spec_variant_key_mismatch")
        if self.variant == VARIANT_PRICE_ONLY and has_volume_key:
            raise ValueError("spec_variant_key_mismatch")
        if set(self._spec.parameters) != {"confirmation_bars"}:
            raise ValueError("spec_parameters_must_be_exactly_confirmation_bars")
        if not {"close", "ma20"} <= keys:
            raise ValueError("spec_requires_close_and_ma20_keys")

    @property
    def spec(self) -> ExitSpec:
        return self._spec

    @property
    def confirmation_bars(self) -> int:
        return int(self._spec.parameters["confirmation_bars"])

    # -- fixed entrypoints (OQ-1) ------------------------------------------

    def evaluate(self, context: ExitContext,
                 previous: ExitState | None) -> ExitProposal:
        """Dispatch to the registered variant's fixed entrypoint."""
        if self.variant == VARIANT_VOLUME:
            return self.evaluate_volume_confirmed(context, previous)
        return self.evaluate_price_only(context, previous)

    def evaluate_volume_confirmed(self, context: ExitContext,
                                  previous: ExitState | None) -> ExitProposal:
        if self.variant != VARIANT_VOLUME:
            raise ValueError("entrypoint_variant_mismatch")
        return self._evaluate(context, previous)

    def evaluate_price_only(self, context: ExitContext,
                            previous: ExitState | None) -> ExitProposal:
        if self.variant != VARIANT_PRICE_ONLY:
            raise ValueError("entrypoint_variant_mismatch")
        return self._evaluate(context, previous)

    # -- shared machinery ---------------------------------------------------

    def market_keys(self) -> tuple[str, ...]:
        return tuple(sorted(_market_names(self._spec)))

    def _evaluate(self, context: ExitContext,
                  previous: ExitState | None) -> ExitProposal:
        try:
            market = resolve_market_snapshot(
                context.market_features, context.contract_id,
                self._spec.observation_duration_seconds, self.market_keys(),
                context.now_nano)
        except MarketChannelUnavailable as unavailable:
            return self.evaluate_with_facts(
                context, previous, None, unavailable.reasons)
        return self.evaluate_with_facts(context, previous, market, ())

    def evaluate_with_facts(self, context: ExitContext,
                            previous: ExitState | None,
                            market: MarketKeyFacts | None,
                            channel_reasons: tuple[str, ...] = ()
                            ) -> ExitProposal:
        """Full policy over an explicit market payload (vol-stop pattern)."""
        base = base_state_for(context, previous)
        facts = collect_exit_facts(context, base)
        now = facts.now_nano
        side = context.budget.side
        duration = self._spec.observation_duration_seconds
        policy_id = self._spec.exit_policy_id
        bars = self.confirmation_bars
        reasons: list[str] = []

        if facts.remaining_lots <= 0:
            return self._proposal(context, base, {}, "KEEP", now, reasons
                                  + ["SIGNAL_NO_REMAINING_LOTS"], side,
                                  facts.remaining_lots, base.confirmations)

        count, last_id = _previous_confirmation(base, policy_id)
        last_slot_end = _parse_slot(last_id, context.contract_id)

        # 1) No legal market original for this evaluation.
        if market is None:
            channel = tuple(channel_reasons)
            if channel and channel[0] == "MARKET_CHANNEL_PENDING_M1_D2_TYPE":
                reasons.append("SIGNAL_MARKET_CHANNEL_PENDING")
            else:
                reasons.extend(channel or ("MARKET_SNAPSHOT_MISSING",))
            new_count = count
            if last_slot_end is not None and _expected_slots_between(
                    context.sessions, last_slot_end, now, duration) > 0:
                # An expected slot has come due and the evidence is
                # unavailable: interruption reset, the OLD id is kept
                # (no real observation slot exists to record, OQ-2 row 3).
                new_count = 0
                reasons.append("SIGNAL_REVERSAL_RESET")
                return self._proposal(
                    context, base, {}, "KEEP", now, reasons, side,
                    facts.remaining_lots, base.confirmations,
                    count_override=0, keep_previous_slot=True)
            return self._proposal(context, base, {}, "KEEP", now, reasons,
                                  side, facts.remaining_lots,
                                  base.confirmations)

        slot_end = market.slot_end_nano
        slot = _slot_id(context, duration, slot_end)

        # 2) Same slot re-evaluated: idempotent, no recount, no reset.
        if slot == last_id:
            action = "EXIT" if count >= bars else "KEEP"
            if action == "KEEP":
                reasons.append("SIGNAL_REVERSAL_CONFIRMING" if count else
                               "SIGNAL_REVERSAL_KEPT")
            return self._proposal(context, base, {}, action, now, reasons,
                                  side, facts.remaining_lots,
                                  base.confirmations)

        # 3) Extract this variant's required keys (field-level quality).
        values: dict[str, Decimal] = {}
        missing: list[str] = []
        for key in self.market_keys():
            item = market.values.get(key)
            if item is None or not isinstance(item.value, Decimal):
                missing.append(key.split(":")[1])
                continue
            values[key.split(":")[1]] = item.value
        if missing:
            # A real observation slot exists but required evidence failed:
            # not a qualifying bar; reset with the REAL slot id recorded.
            consumed = set(missing)
            for reason in market.reasons:
                kind, _, target = reason.partition(":")
                word = f"{kind}_{target.upper()}"
                if target in consumed and word in SIGNAL_REASON_VOCABULARY:
                    reasons.append(word)
            for name in missing:
                fallback = f"MARKET_KEY_MISSING_{name.upper()}"
                if fallback in SIGNAL_REASON_VOCABULARY                         and fallback not in reasons:
                    reasons.append(fallback)
            reasons.append("SIGNAL_REVERSAL_RESET")
            return self._proposal(context, base, {}, "KEEP", now, reasons,
                                  side, facts.remaining_lots,
                                  base.confirmations, slot, 0)

        # 4) The per-bar conjunction (variant legs).
        direction_holds = side * (values["close"] - values["ma20"]) < 0
        if self.variant == VARIANT_VOLUME:
            volume_holds = values["volume_ratio20"] >= VOLUME_RATIO_THRESHOLD
            predicate = direction_holds and volume_holds
        else:
            predicate = direction_holds

        if predicate:
            gap = 0
            if last_slot_end is not None:
                if context.sessions:
                    gap = _expected_slots_between(context.sessions,
                                                  last_slot_end, slot_end,
                                                  duration)
                else:
                    # Unprovable calendar: adjacency cannot be claimed.
                    new_count = min(1, bars)
                    reasons.append("SIGNAL_CALENDAR_UNKNOWN")
                    return self._proposal(
                        context, base, {}, "KEEP" if new_count < bars
                        else "EXIT", now, reasons
                        + ["SIGNAL_REVERSAL_CONFIRMING"], side,
                        facts.remaining_lots, base.confirmations, slot,
                        new_count)
            if gap > 0:
                new_count = min(1, bars)     # interrupted, restart the sequence
                reasons.append("SIGNAL_GAP_RESET")
            else:
                new_count = min(count + 1, bars)
            reasons.append("SIGNAL_REVERSAL_CONFIRMED" if new_count >= bars
                           else "SIGNAL_REVERSAL_CONFIRMING")
            action = "EXIT" if new_count >= bars else "KEEP"
            return self._proposal(context, base, {}, action, now, reasons,
                                  side, facts.remaining_lots,
                                  base.confirmations, slot, new_count)

        # Predicate fails on a complete valid slot: interruption reset.
        reasons.append("SIGNAL_REVERSAL_RESET")
        return self._proposal(context, base, {}, "KEEP", now, reasons,
                              side, facts.remaining_lots, base.confirmations,
                              slot, 0)

    def _proposal(self, context: ExitContext, base: ExitState,
                  changes: dict[str, object], action: str, now: int,
                  reasons: list[str], side: int, remaining: int,
                  confirmations=None, slot: str | None = None,
                  count_override: int | None = None,
                  keep_previous_slot: bool = False) -> ExitProposal:
        if keep_previous_slot and count_override is not None:
            # Reset with the previous real slot id preserved (OQ-2 row 3).
            _, previous_id = _previous_confirmation(
                base, self._spec.exit_policy_id)
            confirmation = ExitConfirmation(
                exit_policy_id=self._spec.exit_policy_id, rule_id="trigger",
                count=count_override, last_observation_id=previous_id)
            merged = [item for item in (confirmations or base.confirmations)
                      if (item.exit_policy_id, item.rule_id)
                      != (confirmation.exit_policy_id, "trigger")]
            merged.append(confirmation)
            changes = dict(changes)
            changes["confirmations"] = tuple(sorted(
                merged, key=lambda item: (item.exit_policy_id,
                                          item.rule_id)))
        elif slot is not None and count_override is not None:
            confirmation = ExitConfirmation(
                exit_policy_id=self._spec.exit_policy_id, rule_id="trigger",
                count=count_override, last_observation_id=slot)
            merged = [item for item in (confirmations or base.confirmations)
                      if (item.exit_policy_id, item.rule_id)
                      != (confirmation.exit_policy_id, "trigger")]
            merged.append(confirmation)
            changes = dict(changes)
            changes["confirmations"] = tuple(sorted(
                merged, key=lambda item: (item.exit_policy_id, item.rule_id)))
        input_core = [
            context.account_id, context.episode_id,
            context.exit_binding.binding_sha256, self._spec.exit_policy_id,
            self.variant, now, action, base.state_prefix_hash]
        input_sha256 = exit_digest(["signal-input", input_core])
        proposal_id = exit_digest(["exit-proposal",
                                   context.exit_binding.binding_sha256,
                                   self._spec.exit_policy_id, now,
                                   input_sha256])
        next_state = advance_state(base, proposal_id, **changes) if changes \
            else base
        target = 0 if action == "EXIT" else side * remaining
        earliest = next_window_start(context.sessions, now,
                                     self._spec.observation_duration_seconds)
        return ExitProposal(
            proposal_id=proposal_id,
            strategy_id=context.strategy_id, episode_id=context.episode_id,
            contract_id=context.contract_id,
            exit_binding_sha256=context.exit_binding.binding_sha256,
            action=action, target_lots=target,
            triggering_policy_id=self._spec.exit_policy_id,
            input_sha256=input_sha256, observed_at_nano=now,
            earliest_execution_nano=max(earliest, now), protection=None,
            next_state=next_state,
            reasons=tuple(reason for reason in dict.fromkeys(reasons)
                          if reason in SIGNAL_REASON_VOCABULARY))
