"""Current-regime identification for M3A (devplan-v2.1.0 03-M3A §4.3).

The regime assessment uses only the decision-time price/volume prefix carried
by the ``FeatureSnapshot`` plus the frozen ``RegimeReference`` volatility
distribution. Future evidence never enters here: the assessment is a current
state, not a label. Macro only toggles risk flags and M4 eligibility; it
never recomputes the structural base, so PRICE_ONLY and WITH_MACRO share the
same base classification.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import isfinite
from types import MappingProxyType
from typing import Mapping

from ak_trading.v2.artifacts import sha256_hex, stable_id
from ak_trading.v2.contracts import (
    DataQuality,
    FeatureSnapshot,
    FrozenRange,
    MacroContext,
    MarketForecast,
    RegimeAssessment,
)
from ak_trading.v2.schemas import canonical_json_bytes

REGIME_REFERENCE_VERSION = "regime-reference-v2.1.0"
ER_HIGH = Decimal("0.5")
ER_LOW = Decimal("0.3")
WIDTH_ATR_LOW = Decimal(2)
WIDTH_ATR_HIGH = Decimal(6)
VOLATILITY_PERCENTILE_CAP = 0.8
PULLBACK_ATR = Decimal("0.5")
MINUTE_CONFIRMATION_FRESHNESS_NANO = 120 * 1_000_000_000
DAILY_CONFIRMATION_FRESHNESS_NANO = 36 * 3600 * 1_000_000_000

_FREQUENCY_LABELS = {60: "m1", 300: "m5", 900: "m15", 1800: "m30", 3600: "m60"}


def baseline_factor_key(name: str, duration_seconds: int) -> str:
    """M2 baseline factor key, e.g. ``BASE:er20:v2.1.0:d60`` (M2 §4.3)."""
    return f"BASE:{name}:v2.1.0:d{duration_seconds}"


def vol001_factor_key(duration_seconds: int) -> str:
    """M2 VOL001 key with the registered parameter version for this duration."""
    if duration_seconds == 86400:
        return "VOL001:v2.1.0:d86400:d1-n20"
    if duration_seconds in _FREQUENCY_LABELS:
        return f"VOL001:v2.1.0:d{duration_seconds}:{_FREQUENCY_LABELS[duration_seconds]}-n60"
    raise ValueError("duration_seconds_must_be_60_300_900_1800_3600_86400")


def volatility_sample_key(product: str, session_kind: str, duration_seconds: int) -> str:
    """``volatility_samples`` key: ``product|session_kind|duration_seconds``."""
    return f"{product}|{session_kind}|{duration_seconds}"


def _factor_decimal(features: FeatureSnapshot, factor_key: str) -> Decimal | None:
    for factor in features.values:
        if factor.factor_key == factor_key:
            value = factor.value
            if isinstance(value, Decimal) and value.is_finite():
                return value
            if isinstance(value, float) and isfinite(value):
                return Decimal(str(value))
            return None
    return None


GAP_ATR_MULTIPLE = Decimal("2")


def _opening_gap_points(features: FeatureSnapshot) -> tuple[Decimal | None, str | None]:
    """Decision-time opening gap from the M2 baseline factor (006 #15).

    Consumes only ``BASE:opening_gap_points:v2.1.0:d<dur>`` rows: the row
    must be unique, same-duration, READY and PIT-legal; anything else is an
    explicit UNKNOWN reason — never a silent "no gap" pass-through.
    """
    key = baseline_factor_key("opening_gap_points", features.duration_seconds)
    rows = [factor for factor in features.values if factor.factor_key == key]
    if not rows:
        return None, "opening_gap_points_missing"
    if len(rows) > 1:
        return None, "opening_gap_points_conflicting_sources"
    row = rows[0]
    if row.quality.status != "READY":
        return None, "opening_gap_points_not_ready"
    if row.available_at_nano is None or row.available_at_nano > features.decision_nano:
        return None, "opening_gap_points_not_visible"
    value = row.value
    if value is None:
        return None, "opening_gap_points_missing_value"
    if isinstance(value, Decimal) and value.is_finite():
        return value, None
    if isinstance(value, float) and isfinite(value):
        return Decimal(str(value)), None
    return None, "opening_gap_points_invalid_value"


def _factor_float(features: FeatureSnapshot, factor_key: str) -> float | None:
    value = _factor_decimal(features, factor_key)
    return None if value is None else float(value)


class RegimeError(ValueError):
    """Fixed-reason regime-reference or assessment violation."""


@dataclass(frozen=True)
class RegimeReference:
    reference_id: str
    training_end_nano: int
    available_at_nano: int
    volatility_samples: Mapping[str, tuple[float, ...]]
    source_hashes: Mapping[str, str]

    def __post_init__(self) -> None:
        if not isinstance(self.reference_id, str) or not self.reference_id:
            raise TypeError("reference_id_must_be_str")
        if isinstance(self.training_end_nano, bool) or not isinstance(self.training_end_nano, int):
            raise TypeError("training_end_nano_must_be_int")
        if isinstance(self.available_at_nano, bool) or not isinstance(self.available_at_nano, int):
            raise TypeError("available_at_nano_must_be_int")
        if self.available_at_nano > self.training_end_nano:
            raise RegimeError("available_at_must_not_be_after_training_end")
        if not isinstance(self.volatility_samples, Mapping):
            raise TypeError("volatility_samples_must_be_mapping")
        frozen_samples: dict[str, tuple[float, ...]] = {}
        for key, values in self.volatility_samples.items():
            if not isinstance(key, str) or not key:
                raise TypeError("volatility_samples_keys_must_be_str")
            if not isinstance(values, tuple) or not values:
                raise RegimeError("volatility_samples_values_must_be_non_empty_tuples")
            checked: list[float] = []
            for item in values:
                if isinstance(item, bool) or not isinstance(item, float) or not isfinite(item):
                    raise RegimeError("volatility_samples_values_must_be_finite_floats")
                checked.append(item)
            if any(checked[i] > checked[i + 1] for i in range(len(checked) - 1)):
                raise RegimeError("volatility_samples_must_be_ascending")
            frozen_samples[key] = tuple(checked)
        if not isinstance(self.source_hashes, Mapping):
            raise TypeError("source_hashes_must_be_mapping")
        frozen_hashes: dict[str, str] = {}
        for key, value in self.source_hashes.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise TypeError("source_hashes_must_be_str_to_str")
            frozen_hashes[key] = value
        object.__setattr__(self, "volatility_samples", MappingProxyType(frozen_samples))
        object.__setattr__(self, "source_hashes", MappingProxyType(frozen_hashes))

    def content_hash(self) -> str:
        """Canonical SHA-256 over the public fields minus ``reference_id``.

        变更申请-004 §3: producers hash only their public projection with M1
        canonical JSON — no private ``version`` constant, no double hashing;
        ``reference_id`` is ``"regime_reference:" + content_hash()``.
        ``volatility_samples`` keeps the 03 ascending-value rule and the
        source hashes stay sorted.
        """
        payload = {
            "training_end_nano": self.training_end_nano,
            "available_at_nano": self.available_at_nano,
            "volatility_samples": {key: list(values)
                                   for key, values in sorted(self.volatility_samples.items())},
            "source_hashes": dict(sorted(self.source_hashes.items())),
        }
        return sha256_hex(canonical_json_bytes(payload))


def fit_regime_reference(training_features: tuple[FeatureSnapshot, ...],
                         training_end_nano: int, available_at_nano: int) -> RegimeReference:
    """Freeze the training volatility reference from a legal prefix only.

    Snapshots decided after ``training_end_nano`` are future leakage and are
    rejected outright; calibration/test data must never reach this function.
    """
    if not isinstance(training_features, tuple) or any(
            not isinstance(item, FeatureSnapshot) for item in training_features):
        raise TypeError("training_features_must_be_tuple_of_FeatureSnapshot")
    if isinstance(training_end_nano, bool) or not isinstance(training_end_nano, int):
        raise TypeError("training_end_nano_must_be_int")
    if isinstance(available_at_nano, bool) or not isinstance(available_at_nano, int):
        raise TypeError("available_at_nano_must_be_int")
    return _reference_from_snapshots(training_features, training_end_nano,
                                     available_at_nano)


def _fit_regime_reference_from_view(training_features, training_end_nano: int,
                                    available_at_nano: int) -> RegimeReference:
    """Internal view entry (MEMORY-DISPOSITION §3): same core and checks as
    the public tuple entry; records are materialized one at a time through
    the view's bounded cache, never held as a full population."""
    from ak_trading.v2._fold_records import RecordView

    if not isinstance(training_features, RecordView) \
            or training_features.record_kind != "FeatureSnapshot":
        raise TypeError("training_features_must_be_snapshot_RecordView")
    if isinstance(training_end_nano, bool) or not isinstance(training_end_nano, int):
        raise TypeError("training_end_nano_must_be_int")
    if isinstance(available_at_nano, bool) or not isinstance(available_at_nano, int):
        raise TypeError("available_at_nano_must_be_int")
    return _reference_from_snapshots(training_features, training_end_nano,
                                     available_at_nano)


def _reference_from_snapshots(snapshots, training_end_nano: int,
                              available_at_nano: int) -> RegimeReference:
    for snapshot in snapshots:
        if snapshot.decision_nano > training_end_nano:
            raise RegimeError("reference_rejects_features_after_training_end")

    samples: dict[str, list[float]] = {}
    snapshot_ids: list[str] = []
    for snapshot in snapshots:
        snapshot_ids.append(snapshot.snapshot_id)
        value = _factor_float(snapshot, vol001_factor_key(snapshot.duration_seconds))
        if value is None:
            continue
        key = volatility_sample_key(
            snapshot.product, snapshot.session_kind, snapshot.duration_seconds)
        samples.setdefault(key, []).append(value)
    frozen = {key: tuple(sorted(values)) for key, values in sorted(samples.items())}

    source_hashes = {
        "snapshots": sha256_hex("\n".join(sorted(snapshot_ids)).encode("utf-8")),
        "regime_reference_version": REGIME_REFERENCE_VERSION,
    }
    provisional = RegimeReference("pending", training_end_nano, available_at_nano,
                                  frozen, source_hashes)
    # 004 §3: identity = prefix + canonical content hash of the public fields;
    # no second SHA-256 layer (stable_id would re-hash the hash).
    return RegimeReference(f"regime_reference:{provisional.content_hash()}",
                           training_end_nano, available_at_nano, frozen, source_hashes)


def training_volatility_percentile(reference: RegimeReference,
                                   features: FeatureSnapshot) -> float | None:
    """Share of training VOL001 values at or below the current value.

    Missing target bucket keys never borrow another product's distribution.
    """
    if not isinstance(reference, RegimeReference):
        raise TypeError("reference_must_be_RegimeReference")
    if not isinstance(features, FeatureSnapshot):
        raise TypeError("features_must_be_FeatureSnapshot")
    key = volatility_sample_key(
        features.product, features.session_kind, features.duration_seconds)
    training_values = reference.volatility_samples.get(key)
    if not training_values:
        return None
    current = _factor_float(features, vol001_factor_key(features.duration_seconds))
    if current is None:
        return None
    below = sum(1 for value in training_values if value <= current)
    return below / len(training_values)


def freeze_range(features: FeatureSnapshot) -> FrozenRange | None:
    """Frozen L/U from ``BASE:prior_low20/BASE:prior_high20`` (prior 20 bars,
    current excluded by the M2 window definition); M=(L+U)/2, stops one ATR
    outside. Returns None whenever ingredients are missing or degenerate.

    The t-time boundaries come from the previous 20 bars, never the current
    one; an existing episode keeps passing its own frozen range back and must
    never re-draw boundaries for its failure tests.
    """
    if not isinstance(features, FeatureSnapshot):
        raise TypeError("features_must_be_FeatureSnapshot")
    lower = _factor_decimal(
        features, baseline_factor_key("prior_low20", features.duration_seconds))
    upper = _factor_decimal(
        features, baseline_factor_key("prior_high20", features.duration_seconds))
    atr = features.atr_points
    if lower is None or upper is None or atr is None or atr <= 0 or lower >= upper:
        return None
    center = (lower + upper) / Decimal(2)
    identity = "|".join((
        features.snapshot_id, str(features.duration_seconds),
        str(lower), str(upper), str(atr), str(features.decision_nano)))
    return FrozenRange(
        range_id=stable_id("frozen_range", identity),
        frozen_at_nano=features.decision_nano,
        lower=lower,
        center=center,
        upper=upper,
        stop_lower=lower - atr,
        stop_upper=upper + atr,
        source_snapshot_id=features.snapshot_id,
    )


def _background_trend(background: MarketForecast | None) -> str | None:
    """``UP``/``DOWN`` when the published daily background trend is clear."""
    if background is None:
        return None
    probabilities = background.direction_probabilities
    up, down, neutral = probabilities["UP"], probabilities["DOWN"], probabilities["NEUTRAL"]
    if up > neutral and up > down:
        return "UP"
    if down > neutral and down > up:
        return "DOWN"
    return None


def _structural_base(features: FeatureSnapshot, frozen_range: FrozenRange | None,
                     round_trip_cost_points: Decimal,
                     reference: RegimeReference) -> tuple[str, tuple[str, ...]]:
    """Base classification plus reasons; UNKNOWN always carries its cause."""
    reference_price = features.reference_price
    atr = features.atr_points
    if reference_price is None or atr is None or atr <= 0:
        return "UNKNOWN", ("missing_reference_price_or_atr",)
    if features.quality.status in ("MISSING", "INVALID"):
        return "UNKNOWN", ("feature_quality_failed",)
    er = _factor_decimal(features, baseline_factor_key("er20", features.duration_seconds))
    ma20 = _factor_decimal(features, baseline_factor_key("ma20", features.duration_seconds))
    ma60 = _factor_decimal(features, baseline_factor_key("ma60", features.duration_seconds))
    if er is None or ma20 is None or ma60 is None:
        return "UNKNOWN", ("missing_structural_factors",)

    ma_diff = ma20 - ma60
    if er >= ER_HIGH:
        if ma_diff > 0:
            return "UP_TREND", ("er20_at_least_0.5_ma20_above_ma60",)
        if ma_diff < 0:
            return "DOWN_TREND", ("er20_at_least_0.5_ma20_below_ma60",)
        return "UNKNOWN", ("trend_sign_unclear",)
    if ER_LOW < er < ER_HIGH:
        return "UNKNOWN", ("middle_er20",)

    # er <= 0.3: range family; cost insufficiency forces COMPRESSION/waiting first.
    if frozen_range is None:
        return "UNKNOWN", ("no_frozen_range_for_low_er",)
    width = frozen_range.upper - frozen_range.lower
    width_atr = width / atr
    if width <= Decimal(2) * round_trip_cost_points:
        return "COMPRESSION", ("width_does_not_cover_cost",)
    if width_atr < WIDTH_ATR_LOW:
        return "COMPRESSION", ("width_below_2_atr",)
    if not frozen_range.lower <= reference_price <= frozen_range.upper:
        return "UNKNOWN", ("decision_price_outside_frozen_range",)
    percentile = training_volatility_percentile(reference, features)
    if percentile is None:
        return "UNKNOWN", ("no_training_volatility_evidence",)
    if width_atr > WIDTH_ATR_HIGH or percentile > VOLATILITY_PERCENTILE_CAP:
        return "WIDE_RANGE", ("wide_width_or_high_volatility_percentile",)
    return "ORDERED_RANGE", ("ordered_range_conditions_met",)


def assess_regime(features: FeatureSnapshot, macro: MacroContext,
                  background: MarketForecast | None, frozen_range: FrozenRange | None,
                  round_trip_cost_points: Decimal, reference: RegimeReference,
                  previous: RegimeAssessment | None) -> RegimeAssessment:
    """Identify the current regime from the decision-time prefix only.

    Fixed order: required-field/quality failure first, then hard risk flags
    (EVENT_SHOCK via valid macro risk tags, ILLIQUID via missing tradability
    evidence), then the structural base; flags are never erased by the base
    classification. ``MacroContext.mode == "BLOCKED"`` never recomputes base.
    """
    if not isinstance(features, FeatureSnapshot):
        raise TypeError("features_must_be_FeatureSnapshot")
    if not isinstance(macro, MacroContext):
        raise TypeError("macro_must_be_MacroContext")
    if background is not None and not isinstance(background, MarketForecast):
        raise TypeError("background_must_be_MarketForecast_or_None")
    if frozen_range is not None and not isinstance(frozen_range, FrozenRange):
        raise TypeError("frozen_range_must_be_FrozenRange_or_None")
    if not isinstance(round_trip_cost_points, Decimal) or not round_trip_cost_points.is_finite() \
            or round_trip_cost_points < 0:
        raise TypeError("round_trip_cost_points_must_be_non_negative_Decimal")
    if not isinstance(reference, RegimeReference):
        raise TypeError("reference_must_be_RegimeReference")
    if previous is not None and not isinstance(previous, RegimeAssessment):
        raise TypeError("previous_must_be_RegimeAssessment_or_None")

    flags: list[str] = []
    reasons: list[str] = []

    if macro.mode == "WITH_MACRO" and macro.risk_tags:
        flags.append("EVENT_SHOCK")
        reasons.append("macro_risk_tag_active")

    # Opening-gap shock from the M2 baseline (006): missing/conflicting input
    # forces UNKNOWN with its reason — the branch is never judged false and
    # allowed through as an ordinary regime.
    gap, gap_reason = _opening_gap_points(features)
    if gap_reason is not None:
        base = "UNKNOWN"
        reasons.append(gap_reason)
    else:
        atr_for_gap = features.atr_points
        if atr_for_gap is not None and atr_for_gap > 0                 and abs(gap) >= GAP_ATR_MULTIPLE * atr_for_gap:
            flags.append("EVENT_SHOCK")
            reasons.append("opening_gap_at_least_2_atr")
    volume_ratio = _factor_float(
        features, baseline_factor_key("volume_ratio20", features.duration_seconds))
    if volume_ratio is None or volume_ratio <= 0.0:
        flags.append("ILLIQUID")
        reasons.append("missing_tradability_evidence")

    if gap_reason is None:
        base, base_reasons = _structural_base(
            features, frozen_range, round_trip_cost_points, reference)
        reasons.extend(base_reasons)
    else:
        base_reasons = ()

    reference_price = features.reference_price
    atr = features.atr_points
    if reference_price is not None and frozen_range is not None \
            and (reference_price > frozen_range.upper or reference_price < frozen_range.lower):
        flags.append("BREAKOUT")
        reasons.append("decision_price_outside_frozen_range_flag")
    trend = _background_trend(background)
    if trend is not None and reference_price is not None and atr is not None and atr > 0:
        ma20 = _factor_decimal(features, baseline_factor_key("ma20", features.duration_seconds))
        if ma20 is not None and abs(reference_price - ma20) <= PULLBACK_ATR * atr:
            flags.append("PULLBACK")
            reasons.append("near_ma20_with_clear_background_trend")

    if trend is None or base not in ("UP_TREND", "DOWN_TREND"):
        cross_timeframe = "UNKNOWN"
    elif (trend == "UP" and base == "UP_TREND") or (trend == "DOWN" and base == "DOWN_TREND"):
        cross_timeframe = "ALIGNED"
    else:
        cross_timeframe = "COUNTERTREND"

    if base == "UNKNOWN":
        confirmed = 0
    elif previous is not None and previous.base == base \
            and previous.quality.status == "READY" \
            and previous.decision_nano < features.decision_nano:
        freshness = (MINUTE_CONFIRMATION_FRESHNESS_NANO
                     if features.duration_seconds != 86400 else DAILY_CONFIRMATION_FRESHNESS_NANO)
        confirmed = previous.confirmed_observations + 1 \
            if features.decision_nano - previous.decision_nano <= freshness else 1
    else:
        confirmed = 1

    degraded = features.quality.status in ("MISSING", "INVALID") \
        or any(reason.startswith("missing") for reason in base_reasons)
    quality = DataQuality(
        status="DEGRADED" if degraded else "READY",
        reasons=tuple(reasons),
        temporal_basis=features.quality.temporal_basis,
        input_hash=sha256_hex(
            f"{features.snapshot_id}|{reference.reference_id}|{features.decision_nano}".encode()),
    )
    identity = "|".join((
        features.snapshot_id, reference.reference_id, str(features.decision_nano),
        base, ",".join(flags), frozen_range.range_id if frozen_range is not None else "NONE",
        cross_timeframe, str(confirmed), quality.input_hash))
    return RegimeAssessment(
        assessment_id=stable_id("regime_assessment", identity),
        snapshot_id=features.snapshot_id,
        reference_id=reference.reference_id,
        decision_nano=features.decision_nano,
        base=base,
        flags=tuple(flags),
        frozen_range=frozen_range,
        cross_timeframe=cross_timeframe,
        confirmed_observations=confirmed,
        reasons=tuple(reasons),
        quality=quality,
    )
