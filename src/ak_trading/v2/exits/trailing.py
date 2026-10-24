"""Fixed-ATR completed-close trailing protection (08-M3C §5.2/§5.3, P1).

Rulings implemented:

* ``atr_basis="FROZEN_ATR"`` — the distance scale is ``A0``, the
  entry-frozen ``RoundBudget.initial_atr_points`` (VOL004 Wilder scale at
  entry). No runtime ATR key is read: the current ``BASE:atr14``
  arithmetic ATR belongs to the P2 VOLATILITY_STOP branch and mixing the
  two scales would silently re-base the risk unit mid-flight. A constant
  A0 also makes line loosening on ATR expansion impossible by
  construction.
* Direction transform ``x = s*C``; ``b0 = s*S0`` from the frozen budget
  stop. ``h_t`` is the favorable peak over completed, visible closes
  after the first real fill only. Effective line
  ``b_t = max(b0, b_(t-1), h_t - k*A0)`` — monotone tightening, never
  below the original stop envelope (the entry ``stop_loss_multiple``
  geometry is not replaced, only tightened; RoundBudget bytes never
  change).
* Tick landing: long rounds the restored actual price UP to tick, short
  DOWN (the tightening direction). A line tightened through the current
  price only forms an exit request for the next legal window — nothing
  is back-filled as an intra-bar fill.
* Arm latch: armed once ``episode_net_liquidation_r >= arm_profit_r``
  with a qualified peak; profit falling back below the arm threshold
  never disarms the already effective line.
* Unknown valuations never guess: without a CURRENT completed close or a
  computable ``Nt`` the policy emits KEEP with explicit reasons and
  leaves the committed state untouched.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Mapping

from ak_trading.v2.contracts import (
    ExitContext,
    ExitProtectionRecord,
    ExitProposal,
    ExitSpec,
    ExitState,
)
from ak_trading.v2.exits.features import (
    collect_exit_facts,
    exit_digest,
    next_window_start,
)
from ak_trading.v2.exits.state import advance_state, base_state_for

TRAILING_REASON_VOCABULARY = frozenset({
    "TRAILING_ARMED", "TRAILING_LINE_BREACHED", "TRAILING_LINE_TIGHTENED",
    "TRAILING_KEEP", "NO_QUALIFIED_COMPLETED_CLOSE", "NLI_UNVALUED",
    "NLI_NOT_AT_ARM_THRESHOLD", "PEAK_WARMUP", "INITIAL_ATR_NON_POSITIVE",
    # facts-origin quality words that may surface verbatim (08 §6.5):
    "MARK_UNVALUED", "MARK_MISSING", "MARK_STALE", "RULE_MISSING",
    "CLOSE_FEE_UNKNOWN", "CLOSE_TODAY_FEE_UNKNOWN", "VALUATION_INCOMPLETE",
    "PLANNED_LOSS_NON_POSITIVE", "NO_REAL_FILL", "OBSERVATION_GAP",
})


@dataclass(frozen=True)
class TrailingStopPolicy:
    """The P1 fixed-ATR trailing protection ExitPolicy implementation."""

    _spec: ExitSpec

    def __post_init__(self) -> None:
        if self._spec.family != "TRAILING_STOP":
            raise ValueError("spec_family_must_be_TRAILING_STOP")
        if self._spec.parameters["atr_basis"] != "FROZEN_ATR":
            raise ValueError("spec_atr_basis_must_be_FROZEN_ATR")
        if self._spec.parameters["peak_source"] != "COMPLETED_CLOSE":
            raise ValueError("spec_peak_source_must_be_COMPLETED_CLOSE")

    @property
    def spec(self) -> ExitSpec:
        return self._spec

    def evaluate(self, context: ExitContext,
                 previous: ExitState | None) -> ExitProposal:
        base = base_state_for(context, previous)
        facts = collect_exit_facts(context, base)
        budget = context.budget
        side = budget.side
        a0 = facts.a0
        arm_threshold = Decimal(str(self._spec.parameters["arm_profit_r"]))
        multiple = Decimal(str(self._spec.parameters["trail_atr_multiple"]))
        now = facts.now_nano
        duration = facts.duration_seconds

        close_price = facts.mark.price if (facts.mark_current
                                           and facts.mark is not None
                                           and facts.mark.price is not None) else None
        qualifying_close = (close_price is not None and facts.mark is not None
                            and facts.mark.as_of_nano is not None
                            and facts.mark.as_of_nano >= base.first_fill_nano)

        # 1) peak: forward-only, completed closes after the first fill only.
        peak = base.price_peak
        peak_nano = base.price_peak_source_nano
        peak_hash = base.price_peak_source_hash
        if qualifying_close and close_price is not None and facts.mark is not None:
            if peak is None:
                peak, peak_nano, peak_hash = (
                    close_price, facts.mark.as_of_nano, facts.mark.source_bar_sha256)
            elif (close_price > peak if side == 1 else close_price < peak):
                peak, peak_nano, peak_hash = (
                    close_price, facts.mark.as_of_nano, facts.mark.source_bar_sha256)

        # 2) net-liquidation peak: only CURRENT, fully computable Nt counts.
        net_peak = base.net_peak_r
        net_peak_nano = base.net_peak_source_nano
        net_peak_hash = base.net_peak_source_hash
        if facts.net_liquidation_r is not None:
            candidate = max(Decimal("0"), facts.net_liquidation_r)
            if net_peak is None or candidate > net_peak:
                digest = exit_digest(["nli", now, facts.net_liquidation_r])
                net_peak, net_peak_nano, net_peak_hash = candidate, now, digest

        # 3) D1-derived profit gate. TRAILING_STOP keeps its OWN profit
        #    threshold: enabled iff the (monotone) net peak has reached
        #    arm_profit_r and a qualified price peak exists. The shared
        #    ExitState.armed latch only records that SOME protection family
        #    initialized a line — never that trailing crossed its gate.
        reasons: list[str] = []
        previously_gated = (base.net_peak_r is not None
                            and base.net_peak_r >= arm_threshold
                            and base.price_peak is not None)
        profit_gated = (facts.net_peak_r is not None
                        and facts.net_peak_r >= arm_threshold
                        and peak is not None)
        if profit_gated and not previously_gated:
            reasons.append("TRAILING_ARMED")
        elif not profit_gated:
            if peak is None and base.price_peak is None:
                reasons.append("PEAK_WARMUP")
            elif facts.net_liquidation_r is None and qualifying_close:
                reasons.extend(facts.nli_reasons or ("NLI_UNVALUED",))
            elif peak is None or base.price_peak is None:
                reasons.append("PEAK_WARMUP")
            else:
                reasons.append("NLI_NOT_AT_ARM_THRESHOLD")
        armed = base.armed  # shared line-existence latch (D1)

        # 4) line: b_t = max(b0, b_(t-1), h_t - k*A0) in x-space, then the
        #    tightening tick landing on the restored actual price. When the
        #    profit gate is not met, any previously committed line — possibly
        #    set by another protection family — is carried unchanged; a
        #    carried line still participates in the breach comparison.
        protection: ExitProtectionRecord | None = None
        stop_price: Decimal | None = None
        line_version: str | None = None
        effective_from: int | None = None
        breached = False

        def _carry_committed_line() -> None:
            nonlocal stop_price, line_version, effective_from, protection
            if base.armed and base.protection_stop_price is not None:
                stop_price = base.protection_stop_price
                line_version = base.protection_line_version
                effective_from = base.protection_effective_from_nano
                protection = ExitProtectionRecord(
                    policy_id=self._spec.exit_policy_id,
                    stop_price=stop_price,
                    line_version=line_version or "",
                    effective_from_nano=effective_from or base.first_fill_nano)

        if not profit_gated:
            _carry_committed_line()
            armed = base.armed
            if qualifying_close and close_price is not None \
                    and stop_price is not None:
                breached = side * close_price <= side * stop_price
        elif a0 <= 0:
            # Degenerate scale (guard only — the budget validator rejects
            # non-positive A0 at freeze time): keep the committed line with
            # its original version; never initialize on an invalid basis.
            reasons.append("INITIAL_ATR_NON_POSITIVE")
            _carry_committed_line()
            armed = base.armed
        else:
            b0 = side * budget.stop_price
            previous_line = (side * base.protection_stop_price
                             if base.armed and base.protection_stop_price is not None
                             else b0)
            h = side * peak if peak is not None else None
            candidate_line = h - multiple * a0 if h is not None else None
            best = max(b0, previous_line,
                       *([candidate_line] if candidate_line is not None else []))
            tick = facts.rule.price_tick if facts.rule is not None else None
            actual = side * best
            if tick is not None and tick > 0:
                steps = (actual / tick).to_integral_value(
                    rounding=ROUND_CEILING if side == 1 else ROUND_FLOOR)
                actual = tick * steps
            if not base.armed or base.protection_stop_price is None:
                stop_price = actual
                line_version = f"{self._spec.exit_policy_id}:line:{actual}"
                effective_from = now
                armed = True
                reasons.append("TRAILING_LINE_TIGHTENED")
            elif actual == base.protection_stop_price:
                # Unchanged line and basis: keep the original version and
                # effective boundary (D1 — no age refresh).
                stop_price = base.protection_stop_price
                line_version = base.protection_line_version
                effective_from = base.protection_effective_from_nano
            elif (side == 1 and actual > base.protection_stop_price) or \
                    (side == -1 and actual < base.protection_stop_price):
                stop_price = actual
                line_version = f"{self._spec.exit_policy_id}:line:{actual}"
                effective_from = now
                reasons.append("TRAILING_LINE_TIGHTENED")
            else:
                # Tick rounding cannot loosen the committed line: carry it.
                stop_price = base.protection_stop_price
                line_version = base.protection_line_version
                effective_from = base.protection_effective_from_nano
            if protection is None and stop_price is not None:
                protection = ExitProtectionRecord(
                    policy_id=self._spec.exit_policy_id, stop_price=stop_price,
                    line_version=line_version or "",
                    effective_from_nano=effective_from or now)
            if qualifying_close and close_price is not None:
                # Causality is structural: a candidate contributed by the
                # current close is strictly below x_now (k > 0), so only a
                # drop from an earlier peak can breach here.
                breached = side * close_price <= side * stop_price

        # 5) candidate next_state: observation facts only (OQ-3).
        changes: dict[str, object] = {
            "price_peak": peak, "price_peak_source_nano": peak_nano,
            "price_peak_source_hash": peak_hash,
            "net_peak_r": net_peak, "net_peak_source_nano": net_peak_nano,
            "net_peak_source_hash": net_peak_hash,
            "armed": armed,
            "protection_stop_price": stop_price if armed else None,
            "protection_line_version": line_version if armed else None,
            "protection_effective_from_nano": effective_from if armed else None,
        }
        input_core = [
            context.account_id, context.episode_id,
            context.exit_binding.binding_sha256, self._spec.exit_policy_id,
            now, str(a0), str(budget.stop_price), str(facts.r0),
            str(multiple), str(arm_threshold),
            str(close_price) if close_price is not None else None,
            str(facts.net_liquidation_r)
            if facts.net_liquidation_r is not None else None,
            str(peak) if peak is not None else None,
            str(base.protection_stop_price)
            if base.protection_stop_price is not None else None,
            base.state_prefix_hash]
        input_sha256 = exit_digest(["trailing-input", input_core])
        proposal_id = exit_digest([
            "exit-proposal", context.exit_binding.binding_sha256,
            self._spec.exit_policy_id, now, input_sha256])
        next_state = advance_state(base, proposal_id, **changes)

        action = "EXIT" if breached else "KEEP"
        target = 0 if breached else side * facts.remaining_lots
        if not reasons:
            reasons.append("TRAILING_KEEP" if not breached
                           else "TRAILING_LINE_BREACHED")
        if breached and "TRAILING_LINE_BREACHED" not in reasons:
            reasons.append("TRAILING_LINE_BREACHED")
        earliest = next_window_start(context.sessions, now, duration)
        return ExitProposal(
            proposal_id=proposal_id,
            strategy_id=context.strategy_id, episode_id=context.episode_id,
            contract_id=context.contract_id,
            exit_binding_sha256=context.exit_binding.binding_sha256,
            action=action, target_lots=target,
            triggering_policy_id=self._spec.exit_policy_id,
            input_sha256=input_sha256, observed_at_nano=now,
            earliest_execution_nano=max(earliest, now),
            protection=protection, next_state=next_state,
            reasons=tuple(reason for reason in reasons
                          if reason in TRAILING_REASON_VOCABULARY))
