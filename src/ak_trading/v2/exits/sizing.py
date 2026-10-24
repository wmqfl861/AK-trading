"""PARTIAL_LIQUIDATION sizing combiner (08-M3C §5.2/§5.3, D3 ruling).

Pure quantity algebra only — this module is a *combiner*, not a predictive
family and not an independent ExitPolicy that issues a second synonymous
reduce ticket. Trigger families (PROFIT_LADDER, TIME_DECAY params-2)
determine the highest cumulative stage; this module converts the frozen
``Qbase`` and the stage fraction into the three DISTINCT quantities frozen
by D3:

* ``Qbase``  — the real original lots frozen once every OPEN/ADD promise
  has reached a terminal state and late fills have been absorbed (the
  freeze itself is an M4-managed protocol; the M3C state transform lives
  in :func:`ak_trading.v2.exits.state.freeze_qbase`). Never the planned
  ``maximum_lots``, never a single layer, never the current remainder,
  never re-based after partial closes.
* ``D_i``    — the cumulative actual close target of stage i:
  ``floor(Qbase * f_i / l) * l`` for intermediate stages and exactly
  ``Qbase`` for the ALL_REMAINDER final stage (no zombie tail lots). A
  pure intermediate value — it is not the order size and not a target
  position.
* ``target_lots`` — the signed FINAL REMAINING position of the selected
  action: ``s * (Qbase - D_i)`` for REDUCE, exactly ``0`` for EXIT.

``issue_deficit`` implements the M4-side one-shot difference
``max(0, D* - closed - pending_commitments)``; D3 requires M4 to compute
it exactly once against the single arbitrated target — never per rule,
never by re-multiplying the current remainder, never by adding two 50%
rules into 75%.

The closing lot step ``l`` is the legal CLOSING step of the then-effective
``TradingRule`` (``lot_step``); ``min_open_lots`` can never masquerade as
a minimum close size, and an unknown rule never yields a guessed value.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR
from typing import Mapping

from ak_trading.v2.contracts import TradingRule

SCALE_OUT_DEFAULTS: Mapping[str, str] = {
    "sizing_mode": "CUMULATIVE_ORIGINAL_LOTS",
    "lot_rounding": "FLOOR_CLOSE",
    "final_step": "ALL_REMAINDER",
}


class SizingError(ValueError):
    """Raised on any illegal size computation (never guess a value)."""


def scale_out_enums(scale_out_spec_parameters: Mapping[str, object] | None
                    ) -> dict[str, str]:
    """The three frozen PARTIAL_LIQUIDATION enums from the bound spec.

    Without a bound scale-out spec the same fixed literals apply (08 §5.4
    whitelist is closed over exactly these values); a bound spec carrying
    any other value was already rejected by the M1 whitelist validator.
    """
    if scale_out_spec_parameters is None:
        return dict(SCALE_OUT_DEFAULTS)
    enums = {}
    for key, value in SCALE_OUT_DEFAULTS.items():
        enums[key] = scale_out_spec_parameters.get(key, value)
    return enums


def closing_lot_step(rule: TradingRule | None) -> int:
    """The legal closing step of the then-effective rule; unknown -> error.

    Uses ``TradingRule.lot_step``. ``min_open_lots`` is an opening minimum
    and can never substitute for a closing step (08 §5.3); a missing rule
    never yields a guessed step.
    """
    if rule is None:
        raise SizingError("RULE_MISSING")
    if rule.lot_step < 1:
        raise SizingError("LOT_STEP_MUST_BE_POSITIVE")
    return rule.lot_step


def cumulative_target_lots(qbase: int, fraction: Decimal, lot_step: int,
                           final_step: bool) -> int:
    """D_i = floor(Qbase*f/l)*l; the ALL_REMAINDER final stage is Qbase."""
    if not isinstance(qbase, int) or isinstance(qbase, bool) or qbase < 1:
        raise SizingError("QBASE_MUST_BE_POSITIVE_INT")
    if not isinstance(fraction, Decimal) or not fraction.is_finite():
        raise SizingError("FRACTION_MUST_BE_FINITE_DECIMAL")
    if fraction <= 0 or fraction > 1:
        raise SizingError("FRACTION_MUST_BE_WITHIN_0_1")
    if not isinstance(lot_step, int) or isinstance(lot_step, bool) or lot_step < 1:
        raise SizingError("LOT_STEP_MUST_BE_POSITIVE_INT")
    if final_step:
        return qbase
    scaled = (Decimal(qbase) * fraction / Decimal(lot_step)).to_integral_value(
        rounding=ROUND_FLOOR)
    return int(scaled) * lot_step


def remaining_target_lots(qbase: int, cumulative_target: int) -> int:
    """r_i = Qbase - D_i, the unsigned final remaining target."""
    if cumulative_target < 0 or cumulative_target > qbase:
        raise SizingError("CUMULATIVE_TARGET_OUT_OF_RANGE")
    return qbase - cumulative_target


def reduce_target_lots(side: int, qbase: int, cumulative_target: int) -> int:
    """target_i = s * (Qbase - D_i); the final stage is converted by the
    caller into action=EXIT / target 0 (the contract forbids EXIT != 0)."""
    if side not in (-1, 1):
        raise SizingError("SIDE_MUST_BE_MINUS1_OR_1")
    return side * remaining_target_lots(qbase, cumulative_target)


def issue_deficit(cumulative_target: int, closed_lots: int,
                  active_commitments: int) -> int:
    """M4's one-shot difference max(0, D* - C - P) for the UNIQUE target.

    ``active_commitments`` is the absolute remaining close-side promise
    volume of the same scope (non-terminal CLOSE/CLOSE_TODAY intents);
    OPEN/ADD never enters P. CANCEL_REQUESTED is still non-terminal and
    stays in P. M4 computes this once per issuance boundary — never once
    per rule and never twice for the same fill.
    """
    for name, value in (("cumulative_target", cumulative_target),
                        ("closed_lots", closed_lots),
                        ("active_commitments", active_commitments)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise SizingError(f"{name.upper()}_MUST_BE_NON_NEGATIVE_INT")
    return max(0, cumulative_target - closed_lots - active_commitments)


def stage_is_final(fractions: tuple[Decimal, ...], stage_index: int) -> bool:
    """The whitelist guarantees strictly increasing fractions ending at 1;
    the final stage is the last index."""
    if not fractions or stage_index != len(fractions) - 1:
        return False
    return fractions[stage_index] == Decimal("1")
