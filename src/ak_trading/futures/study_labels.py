"""Contract-local reference-price labels and descriptive event summaries.

Indexes always refer to the unfiltered stored observations.  A label is a
reference price change; it does not represent an executable trade or account.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from decimal import Context, Decimal, DecimalException, localcontext
from math import isfinite
from statistics import mean

from ak_trading.futures.spec import ContractBar
from ak_trading.futures.study_spec import StudyContract, StudySignal

__all__ = ["label_study_outcomes", "summarize_study_outcomes"]

_HORIZONS = (1, 5, 20)
_DAY_NANO = 86_400_000_000_000
_PARTITIONS = ("train", "development", "holdout")


def _integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_direction(direction: object) -> None:
    if direction is not None and (
        not _integer(direction) or direction not in (-1, 0, 1)
    ):
        raise ValueError("candidate_direction must be -1, 0, 1 or None, never bool")


def _validate_inputs(
    contracts: Sequence[StudyContract], signals: Sequence[StudySignal]
) -> dict[str, StudyContract]:
    by_id: dict[str, StudyContract] = {}
    for contract in contracts:
        for name in ("contract_instance_id", "raw_symbol", "product"):
            if not isinstance(getattr(contract, name), str) or not getattr(contract, name):
                raise ValueError(f"contract {name} must be a nonempty string")
        if contract.contract_instance_id in by_id:
            raise ValueError("duplicate contract_instance_id")
        by_id[contract.contract_instance_id] = contract
        previous_nano: int | None = None
        for bar in contract.bars:
            if bar.symbol != contract.raw_symbol:
                raise ValueError("bar raw_symbol does not match contract")
            if not _integer(bar.datetime_nano):
                raise ValueError("bar datetime_nano must be an integer, never bool")
            if previous_nano is not None and bar.datetime_nano <= previous_nano:
                raise ValueError("contract timestamps must be strictly increasing; no duplicates")
            previous_nano = bar.datetime_nano

    seen: set[tuple[str, str, int]] = set()
    for signal in signals:
        contract = by_id.get(signal.contract_instance_id)
        if contract is None:
            raise ValueError("signal has unknown contract_instance_id")
        if signal.raw_symbol != contract.raw_symbol or signal.product != contract.product:
            raise ValueError("signal identity does not match contract raw_symbol/product")
        if not _integer(signal.index) or not 0 <= signal.index < len(contract.bars):
            raise ValueError("signal index must identify an original contract observation")
        if not _integer(signal.datetime_nano) or (
            signal.datetime_nano != contract.bars[signal.index].datetime_nano
        ):
            raise ValueError("signal datetime_nano does not match its observation index")
        if not isinstance(signal.strategy_id, str) or not signal.strategy_id:
            raise ValueError("signal strategy_id must be a nonempty string")
        if not isinstance(signal.feature_status, str) or not signal.feature_status:
            raise ValueError("signal feature_status must be a nonempty string")
        key = (signal.contract_instance_id, signal.strategy_id, signal.index)
        if key in seen:
            raise ValueError("duplicate signal contract/strategy/index")
        seen.add(key)
        _validate_direction(signal.candidate_direction)
    return by_id


def _ohlc(bar: ContractBar) -> tuple[tuple[Decimal, ...] | None, str]:
    values: list[Decimal] = []
    for value in (bar.open, bar.high, bar.low, bar.close):
        if value is None:
            return None, "missing_ohlc"
        if isinstance(value, bool) or not isinstance(value, (Decimal, int, float)):
            return None, "invalid_ohlc_type"
        number = value if isinstance(value, Decimal) else Decimal(str(value))
        if not number.is_finite():
            return None, "non_finite_ohlc"
        values.append(number)
    opening, high, low, close = values
    if not (low <= opening <= high and low <= close <= high):
        return None, "invalid_ohlc_order"
    return tuple(values), "ok"


def _partition(index: int, length: int) -> tuple[str, int, int]:
    cuts = (0, 3 * length // 5, 4 * length // 5, length)
    for name, start, end in zip(_PARTITIONS, cuts, cuts[1:]):
        if start <= index < end:
            return name, start, end
    raise ValueError("index is outside contract observations")


def _price_change(entry: Decimal, terminal: Decimal) -> float | None:
    try:
        with localcontext(Context(prec=50)):
            # Subtract first to retain small changes near one without cancellation.
            change = float((terminal - entry) / entry)
    except (DecimalException, OverflowError, ValueError):
        return None
    return change if isfinite(change) else None


def label_study_outcomes(
    contracts: Sequence[StudyContract],
    signals: Sequence[StudySignal],
    *,
    horizons: Sequence[int] = (1, 5, 20),
) -> tuple[dict, ...]:
    """Emit one row per signal/horizon, including censored and invalid labels.

    Partitions are half-open original-index intervals with cuts 3*N//5 and
    4*N//5. Decision i, entry i+1 and terminal i+h must all lie in that interval.
    The entire inclusive path i..i+h must have finite, consistent OHLC.
    """
    horizons = tuple(horizons)
    if any(not _integer(h) or h not in _HORIZONS for h in horizons):
        raise ValueError("horizons must contain only integer 1, 5 or 20, never bool")
    if len(set(horizons)) != len(horizons):
        raise ValueError("duplicate horizons")
    by_id = _validate_inputs(contracts, signals)
    prices = {
        instance_id: tuple(_ohlc(bar) for bar in contract.bars)
        for instance_id, contract in by_id.items()
    }
    outcomes: list[dict] = []
    for signal in signals:
        contract = by_id[signal.contract_instance_id]
        bars = contract.bars
        index = signal.index
        partition, start, end = _partition(index, len(bars))
        for horizon in horizons:
            entry_index, exit_index = index + 1, index + horizon
            row = {
                "contract_instance_id": contract.contract_instance_id,
                "raw_symbol": contract.raw_symbol,
                "product": contract.product,
                "strategy_id": signal.strategy_id,
                "index": index,
                "horizon": horizon,
                "partition": partition,
                "partition_start": start,
                "partition_end": end,
                "entry_index": entry_index,
                "exit_index": exit_index,
                "decision_datetime_nano": signal.datetime_nano,
                "entry_datetime_nano": (
                    bars[entry_index].datetime_nano if entry_index < len(bars) else None
                ),
                "exit_datetime_nano": (
                    bars[exit_index].datetime_nano if exit_index < len(bars) else None
                ),
                "candidate_direction": signal.candidate_direction,
                "feature_status": signal.feature_status,
                "label_status": "invalid",
                "reason": "unavailable",
                "entry_open": None,
                "exit_close": None,
                "raw_change": None,
                "directional_change": None,
                "eligible_directional": False,
                "elapsed_calendar_days": None,
                "gap_status": "unavailable",
            }
            outcomes.append(row)
            if entry_index >= end or exit_index >= end:
                row["label_status"] = "unmatured" if end == len(bars) else "boundary"
                row["reason"] = (
                    "insufficient_future_observations"
                    if end == len(bars)
                    else "cross_partition"
                )
                continue

            path_prices = prices[contract.contract_instance_id]
            row["gap_status"] = (
                "calendar_gap"
                if any(
                    bars[j].datetime_nano - bars[j - 1].datetime_nano > _DAY_NANO
                    for j in range(index + 1, exit_index + 1)
                )
                else "no_calendar_gap"
            )
            path_reason = next(
                (path_prices[j][1] for j in range(index, exit_index + 1)
                 if path_prices[j][0] is None),
                None,
            )
            if path_reason is not None:
                row["reason"] = path_reason
                continue
            entry_prices = path_prices[entry_index][0]
            exit_prices = path_prices[exit_index][0]
            assert entry_prices is not None and exit_prices is not None
            entry, terminal = entry_prices[0], exit_prices[3]
            if entry <= 0:
                row["reason"] = "non_positive_entry_open"
                continue
            raw_change = _price_change(entry, terminal)
            if raw_change is None:
                row["reason"] = "non_finite_change"
                continue
            try:
                elapsed = (bars[exit_index].datetime_nano - signal.datetime_nano) / _DAY_NANO
            except OverflowError:
                elapsed = float("inf")
            if not isfinite(elapsed):
                row["reason"] = "non_finite_elapsed_time"
                continue
            eligible = (
                signal.feature_status == "ready" and signal.candidate_direction in (-1, 1)
            )
            row.update(
                label_status="valid",
                reason="ok",
                entry_open=str(entry),
                exit_close=str(terminal),
                raw_change=raw_change,
                directional_change=(signal.candidate_direction * raw_change if eligible else None),
                eligible_directional=eligible,
                elapsed_calendar_days=elapsed,
            )
    return tuple(outcomes)


def _finite_change(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("a valid outcome must contain finite numeric changes")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError("a valid outcome must contain finite numeric changes") from exc
    if not isfinite(number):
        raise ValueError("a valid outcome must contain finite numeric changes")
    return number


def _eligible(row: Mapping) -> bool:
    return (
        row["label_status"] == "valid"
        and row["feature_status"] == "ready"
        and row["candidate_direction"] in (-1, 1)
        and row["eligible_directional"] is True
    )


def _counts(rows: Sequence[Mapping]) -> dict:
    valid = [row for row in rows if row["label_status"] == "valid"]
    eligible = [row for row in rows if _eligible(row)]
    return {
        "n_total": len(rows),
        "n_signals": len({(r["contract_instance_id"], r["strategy_id"], r["index"]) for r in rows}),
        "n_valid": len(valid),
        "n_eligible": len(eligible),
        "n_flat": sum(row["candidate_direction"] == 0 for row in rows),
        "n_unavailable": sum(row["candidate_direction"] is None for row in rows),
        "n_feature_not_ready": sum(row["feature_status"] != "ready" for row in rows),
        "n_contracts": len({row["contract_instance_id"] for row in rows}),
        "n_valid_contracts": len({row["contract_instance_id"] for row in valid}),
        "n_contributing_contracts": len({row["contract_instance_id"] for row in eligible}),
        "label_status_counts": dict(sorted(Counter(row["label_status"] for row in rows).items())),
        "reason_counts": dict(sorted(Counter(row["reason"] for row in rows).items())),
        "feature_status_counts": dict(sorted(Counter(row["feature_status"] for row in rows).items())),
        "gap_status_counts": dict(sorted(Counter(row["gap_status"] for row in rows).items())),
    }


def _statistics(values: Sequence[float]) -> dict:
    ordered = sorted(values)
    n = len(ordered)
    successes = sum(value > 0 for value in ordered)
    ties = sum(value == 0 for value in ordered)
    losses = sum(value < 0 for value in ordered)
    middle = n // 2
    median = None if not n else (
        ordered[middle] if n % 2 else mean(ordered[middle - 1:middle + 1])
    )
    return {
        "n": n,
        "mean": mean(ordered) if n else None,
        "median": median,
        "success_count": successes,
        "tie_count": ties,
        "loss_count": losses,
        "success_rate": successes / n if n else None,
        "tie_rate": ties / n if n else None,
        "loss_rate": losses / n if n else None,
    }


def _paired_mean(candidate: Sequence[float], baseline: Sequence[float]) -> float | None:
    if not candidate:
        return None
    # Decimal avoids intermediate float overflow when a short label reverses a
    # large raw change. An unrepresentable final difference remains null.
    with localcontext(Context(prec=50)):
        difference = sum(
            (Decimal.from_float(a) - Decimal.from_float(b) for a, b in zip(candidate, baseline)),
            Decimal(0),
        ) / len(candidate)
    result = float(difference)
    return result if isfinite(result) else None


def _group_summary(rows: Sequence[Mapping]) -> dict:
    eligible = [row for row in rows if _eligible(row)]
    candidate = [_finite_change(row["directional_change"]) for row in eligible]
    baseline = [_finite_change(row["raw_change"]) for row in eligible]
    paired = _paired_mean(candidate, baseline)
    by_contract: dict[tuple[str, str], list[Mapping]] = defaultdict(list)
    for row in rows:
        by_contract[(row["contract_instance_id"], row["raw_symbol"])].append(row)
    contributions = []
    for (instance_id, raw_symbol), contract_rows in sorted(by_contract.items()):
        counts = _counts(contract_rows)
        contributions.append({
            "contract_instance_id": instance_id,
            "raw_symbol": raw_symbol,
            "n_total": counts["n_total"],
            "n_valid": counts["n_valid"],
            "n_eligible": counts["n_eligible"],
        })
    return {
        "counts": _counts(rows),
        "eligible_n": len(eligible),
        "candidate": _statistics(candidate),
        "buy_direction_baseline": _statistics(baseline),
        "paired_mean_difference": paired,
        "paired_mean_difference_status": (
            "valid" if paired is not None else "non_finite" if candidate else "unavailable"
        ),
        "contribution_contracts": contributions,
    }


def summarize_study_outcomes(outcomes: Iterable[Mapping]) -> dict:
    """Describe contract and product groups using equally weighted eligible events.

    The buy-direction comparison uses exactly the same eligible event rows as
    the candidate; flat, unavailable and unready directions never dilute either
    denominator. Empty cohorts have null means, medians and rates.
    """
    rows = tuple(outcomes)
    seen = set()
    for row in rows:
        _validate_direction(row["candidate_direction"])
        key = (row["contract_instance_id"], row["strategy_id"], row["index"], row["horizon"])
        if key in seen:
            raise ValueError("duplicate outcome contract/strategy/index/horizon")
        seen.add(key)
        if row["label_status"] == "valid":
            _finite_change(row["raw_change"])
            if _eligible(row):
                _finite_change(row["directional_change"])

    groups = []
    for level in ("contract", "product"):
        fields = ("product", "strategy_id", "horizon", "partition")
        if level == "contract":
            fields += ("contract_instance_id", "raw_symbol")
        buckets: dict[tuple, list[Mapping]] = defaultdict(list)
        for row in rows:
            buckets[tuple(row[field] for field in fields)].append(row)
        for key, group_rows in sorted(buckets.items()):
            groups.append({
                "level": level,
                "groupkeys": dict(zip(fields, key)),
                **_group_summary(group_rows),
            })
    return {
        "counts": _counts(rows),
        "methodology": {
            "study_type": "retrospective_reference_price_event_study",
            "horizons": sorted({row["horizon"] for row in rows}),
            "horizon_unit": "original_stored_observation_rows",
            "partitions": {
                "train": "[0, 3*N//5)",
                "development": "[3*N//5, 4*N//5)",
                "holdout": "[4*N//5, N)",
            },
            "partition_end": "exclusive; N is each contract's original observation count",
            "label_window": "decision i, entry i+1 and exit i+h must share one partition",
            "price_validation": "finite consistent OHLC on every original row i..i+h; entry open > 0",
            "raw_change": "(close[i+h] / open[i+1]) - 1; finite negative terminal prices allowed",
            "directional_eligibility": "valid raw label, feature_status ready, integer direction -1 or +1",
            "directional_change": "candidate_direction * raw_change, only for eligible events",
            "flat_unavailable_counts": "direction 0 and null respectively, across all output rows",
            "count_unit": "one signal-horizon row; feature readiness does not affect n_valid",
            "buy_direction_baseline": "raw_change on exactly the same eligible events as candidate",
            "paired_mean_difference": "mean(candidate directional change - paired raw change)",
            "aggregation": "event_weighted; no averaging of contract means",
            "contributing_contracts": "contracts with at least one eligible directional event",
            "elapsed_calendar_days": "(exit nano - decision nano) / 86400000000000, for valid labels",
            "gap_status": "calendar_gap means a consecutive observed timestamp gap > 24 hours",
            "gap_interpretation": "calendar gaps are reported without inferring missing trading sessions",
            "invalid_rows": "retained at original indexes; prices and numeric labels remain null",
            "overlapping_labels": True,
            "overlap_disclosure": "successive label windows can overlap; observations are not assumed independent",
            "inference": "descriptive only; no significance tests or annualized risk statistics",
            "interpretation": "reference price events without execution, fees, margin or account assumptions",
        },
        "groups": groups,
    }
