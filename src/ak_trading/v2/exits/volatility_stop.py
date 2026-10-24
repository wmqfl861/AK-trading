"""VOLATILITY_STOP At-based protection line (08-M3C §5.2/§5.3, P2; D1/D2).

Rulings implemented:

* ``At`` is the CURRENT same-mode ``BASE:atr14:v2.1.0:d{d}`` (arithmetic
  mean of the last 14 TRs) taken — together with the same snapshot's
  ``BASE:close`` — straight from the M2 FeatureSnapshot original selected
  by the D2 whole-snapshot rule (:func:`features.resolve_market_snapshot`).
  It is a DIFFERENT scale from the entry-frozen Wilder ``A0`` and the two
  are never mixed or substituted. BASE values are never wrapped into the
  EXITSTATE namespace.
* NO profit arm threshold (D1): with a real remaining position, a legal
  frozen S0/side/tick and one qualifying close+At observation, the line
  initializes on the first valid observation and the shared
  ``ExitState.armed`` latch goes True — a candidate that is not breached
  still yields KEEP. ``confirmation_bars=1`` gates the breach request,
  not the enablement.
* Monotone merged line in direction space:
  ``b_t = max(b0, previous_committed_line, x_t - k*At)``; the restored
  actual price is tick-rounded UP for longs / DOWN for shorts and the
  completed-observation breach comparison includes equality. ATR
  expansion can only stop NEW candidates — it never loosens, disarms or
  resets the committed line to S0.
* Missing At still allows the breach check against the COMMITTED line
  whenever the same-frequency close itself qualifies (the comparison
  needs no new At); a missing close resets the soft confirmation and
  keeps the line. Neither ever fabricates an intra-bar protective fill —
  a breach only produces a next-legal-window close request.
* Version discipline (D1): when the line and its basis are unchanged the
  previous ``protection_line_version`` / ``effective_from`` are kept; a
  new version is written only when the merged stop actually moves.
* Until M1 lands the D2 type refreeze (``ExitContext.market_features``,
  currently typed for exit snapshots), :meth:`evaluate` degrades to KEEP
  with an explicit channel reason while keeping the committed line; the
  full math is exercised through :meth:`evaluate_with_facts`.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from ak_trading.v2.contracts import (
    ExitConfirmation, ExitContext, ExitProtectionRecord, ExitProposal,
    ExitSpec, ExitState, exit_observation_slot_id,
)
from ak_trading.v2.exits.features import (
    MarketChannelUnavailable, MarketKeyFacts, collect_exit_facts, exit_digest,
    next_window_start, resolve_market_snapshot,
)
from ak_trading.v2.exits.state import advance_state, base_state_for

VOLATILITY_REASON_VOCABULARY = frozenset({
    "VOL_LINE_TIGHTENED", "VOL_LINE_BREACHED", "VOL_ATR_UNAVAILABLE",
    "VOL_LINE_KEPT", "VOL_MARKET_CHANNEL_PENDING", "VOL_NO_REMAINING_LOTS",
    "MARKET_CHANNEL_PENDING_M1_D2_TYPE", "MARKET_SNAPSHOT_MISSING",
    "MARKET_SNAPSHOT_AMBIGUOUS", "MARKET_SNAPSHOT_DUPLICATED",
    "MARKET_KEY_MISSING_ATR14", "MARKET_KEY_NOT_READY_ATR14",
    "MARKET_VALUE_INVALID_ATR14", "MARKET_KEY_NOT_VISIBLE_ATR14",
    "MARKET_KEY_DUPLICATED_ATR14", "MARKET_CLOSE_MISSING",
})


def _atr_reason(reasons: tuple[str, ...]) -> tuple[str, ...]:
    """Normalize resolver reasons into the policy vocabulary (atr-only)."""
    out = []
    for reason in reasons:
        kind, _, name = reason.partition(":")
        if name == "atr14":
            out.append(f"{kind}_ATR14")
        elif ":" not in reason:
            out.append(reason)
    return tuple(out)


@dataclass(frozen=True)
class VolatilityStopPolicy:
    """The P2 volatility-adjusted protection ExitPolicy implementation."""

    _spec: ExitSpec

    def __post_init__(self) -> None:
        if self._spec.family != "VOLATILITY_STOP":
            raise ValueError("spec_family_must_be_VOLATILITY_STOP")
        if self._spec.parameters["atr_basis"] != "BASE_ATR14":
            raise ValueError("spec_atr_basis_must_be_BASE_ATR14")

    @property
    def spec(self) -> ExitSpec:
        return self._spec

    def market_keys(self) -> tuple[str, str]:
        duration = self._spec.observation_duration_seconds
        return (f"BASE:atr14:v2.1.0:d{duration}",
                f"BASE:close:v2.1.0:d{duration}")

    def evaluate(self, context: ExitContext,
                 previous: ExitState | None) -> ExitProposal:
        base = base_state_for(context, previous)
        atr_key, close_key = self.market_keys()
        try:
            market = resolve_market_snapshot(
                context.market_features, context.contract_id,
                self._spec.observation_duration_seconds, (atr_key, close_key),
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
        """Full policy math over an explicit market-facts payload.

        ``market=None`` encodes "no legal M2 original for this
        observation" (channel pending, snapshot missing/ambiguous, or an
        entirely stale window) — the committed line is carried unchanged.
        """
        base = base_state_for(context, previous)
        facts = collect_exit_facts(context, base)
        budget = context.budget
        side = budget.side
        now = facts.now_nano
        multiple = Decimal(str(self._spec.parameters["atr_multiple"]))
        atr_key, close_key = self.market_keys()
        atr_reasons = tuple(_atr_reason(channel_reasons))

        close_value: Decimal | None = None
        atr_value: Decimal | None = None
        slot_end: int | None = None
        if market is not None:
            slot_end = market.slot_end_nano
            close_item = market.values.get(close_key)
            if close_item is not None and isinstance(close_item.value, Decimal):
                close_value = close_item.value
            atr_item = market.values.get(atr_key)
            if atr_item is not None and isinstance(atr_item.value, Decimal):
                atr_value = atr_item.value
            if atr_value is None and close_value is not None:
                atr_reasons = atr_reasons + _atr_reason(market.reasons) \
                    or ("VOL_ATR_UNAVAILABLE",)
            if close_value is None and atr_value is not None:
                atr_reasons = atr_reasons + \
                    ("VOL_ATR_UNAVAILABLE", "MARKET_CLOSE_MISSING")
        else:
            atr_reasons = atr_reasons or ("VOL_ATR_UNAVAILABLE",)
            if channel_reasons and channel_reasons[0] == \
                    "MARKET_CHANNEL_PENDING_M1_D2_TYPE":
                atr_reasons = ("VOL_MARKET_CHANNEL_PENDING",)

        reasons: list[str] = []
        previous_stop = base.protection_stop_price if base.armed else None
        # No position: nothing to protect; keep everything committed.
        if facts.remaining_lots <= 0:
            return self._keep(context, base, {}, ("VOL_NO_REMAINING_LOTS",),
                              now, confirmations=base.confirmations)

        # 1) New candidate only when BOTH the close and At are legal.
        b0 = side * budget.stop_price
        previous_line = side * previous_stop if previous_stop is not None else b0
        stop_price = previous_stop
        line_version = base.protection_line_version if base.armed else None
        effective_from = base.protection_effective_from_nano if base.armed else None
        armed = base.armed
        breach = False
        if close_value is not None and atr_value is not None:
            x = side * close_value
            candidate = x - multiple * atr_value
            best = max(b0, previous_line, candidate)
            tick = facts.rule.price_tick if facts.rule is not None else None
            actual = side * best
            if tick is not None and tick > 0:
                steps = (actual / tick).to_integral_value(
                    rounding=ROUND_CEILING if side == 1 else ROUND_FLOOR)
                actual = tick * steps
            armed = True
            if previous_stop is None:
                # First line initialization: the shared armed latch goes True
                # even when the merged line happens to equal S0 (D1).
                stop_price = actual
                line_version = f"{self._spec.exit_policy_id}:line:{actual}"
                effective_from = now
                reasons.append("VOL_LINE_TIGHTENED")
            elif actual == previous_stop:
                # Unchanged line and basis: keep the original version and
                # effective boundary (D1 — never refresh the line's age).
                stop_price = previous_stop
                line_version = base.protection_line_version
                effective_from = base.protection_effective_from_nano
                reasons.append("VOL_LINE_KEPT")
            elif (side == 1 and actual > previous_stop) or \
                    (side == -1 and actual < previous_stop):
                stop_price = actual
                line_version = f"{self._spec.exit_policy_id}:line:{actual}"
                effective_from = now
                reasons.append("VOL_LINE_TIGHTENED")
            else:
                # Tick rounding cannot loosen the committed line; keep it.
                stop_price = previous_stop
                line_version = base.protection_line_version
                effective_from = base.protection_effective_from_nano
                reasons.append("VOL_LINE_KEPT")
            breach = side * close_value <= side * stop_price
        elif close_value is not None and previous_stop is not None:
            # D1: a missing At never blocks the breach check against the
            # committed line; only new candidates need a fresh At.
            reasons.append("VOL_ATR_UNAVAILABLE")
            breach = side * close_value <= side * previous_stop
        else:
            reasons.append("VOL_ATR_UNAVAILABLE" if atr_value is None
                           else "VOL_LINE_KEPT")

        # 2) Soft confirmation (OQ-6 slot dedup, (policy, rule) isolated).
        rule_id = "trigger"
        previous_count = 0
        previous_slot = None
        for item in base.confirmations:
            if item.exit_policy_id == self._spec.exit_policy_id \
                    and item.rule_id == rule_id:
                previous_count, previous_slot = item.count, item.last_observation_id
        confirmation = None
        if slot_end is not None:
            slot = exit_observation_slot_id(context.contract_id,
                                            self._spec.observation_duration_seconds,
                                            slot_end)
            if breach and slot != previous_slot:
                count = min(previous_count + 1,
                            int(self._spec.parameters["confirmation_bars"]))
            elif breach:
                count = previous_count
            else:
                count = 0
            confirmation = ExitConfirmation(
                exit_policy_id=self._spec.exit_policy_id, rule_id=rule_id,
                count=count, last_observation_id=slot if count else previous_slot)
        elif not breach:
            confirmation = ExitConfirmation(
                exit_policy_id=self._spec.exit_policy_id, rule_id=rule_id,
                count=0, last_observation_id=previous_slot)
        confirmations = base.confirmations
        if confirmation is not None:
            merged = [item for item in base.confirmations
                      if (item.exit_policy_id, item.rule_id)
                      != (confirmation.exit_policy_id, confirmation.rule_id)]
            merged.append(confirmation)
            confirmations = tuple(sorted(
                merged, key=lambda item: (item.exit_policy_id, item.rule_id)))

        changes: dict[str, object] = {"confirmations": confirmations}
        if armed:
            protection_record = ExitProtectionRecord(
                policy_id=self._spec.exit_policy_id,
                stop_price=stop_price, line_version=line_version or "",
                effective_from_nano=effective_from or base.first_fill_nano) \
                if stop_price is not None else None
        else:
            protection_record = None
        if armed and stop_price is not None:
            changes.update({"armed": True,
                            "protection_stop_price": stop_price,
                            "protection_line_version": line_version,
                            "protection_effective_from_nano": effective_from})

        confirmed = confirmation is not None and \
            confirmation.count >= int(self._spec.parameters["confirmation_bars"])
        action = "EXIT" if (breach and confirmed) else "KEEP"
        if action == "EXIT":
            reasons.append("VOL_LINE_BREACHED")
        reasons.extend(atr_reasons)
        input_core = [
            context.account_id, context.episode_id,
            context.exit_binding.binding_sha256, self._spec.exit_policy_id,
            now, str(atr_value) if atr_value is not None else None,
            str(close_value) if close_value is not None else None,
            str(multiple), str(budget.stop_price),
            market.snapshot.snapshot_id if market is not None else None,
            base.state_prefix_hash]
        input_sha256 = exit_digest(["vol-input", input_core])
        proposal_id = exit_digest(["exit-proposal",
                                   context.exit_binding.binding_sha256,
                                   self._spec.exit_policy_id, now, input_sha256])
        next_state = advance_state(base, proposal_id, **changes) if changes \
            else base
        target = 0 if action == "EXIT" else side * facts.remaining_lots
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
            earliest_execution_nano=max(earliest, now),
            protection=protection_record, next_state=next_state,
            reasons=tuple(r for r in dict.fromkeys(reasons)
                          if r in VOLATILITY_REASON_VOCABULARY))
