"""C1 identity: dated universe, symbol classification, denominators (H02/H05).

Classification rules (decision.md H02/H05):

- ``KQ.m@EX.code`` / ``KQ.i@EX.code`` are continuous/index *references only*;
  they never count toward concrete contract coverage and are never re-prefixed
  to look like delivery months.
- Concrete delivery months: four-digit YYMM outside CZCE; CZCE uses three-digit
  YMM codes whose decade is ambiguous, so a full ``delivery_yyyymm`` requires
  source expiry evidence - without it the delivery month stays unknown
  (``CZCE_3DIGIT_YEAR_AMBIGUOUS_REQUIRES_SOURCE_MAPPING``). No guessing.
- Any trailing ``F`` suffix after an otherwise contract-shaped code is
  ``F_SUFFIX_QUARANTINED`` (QUARANTINED_PENDING_CLASSIFICATION, H05). The
  suffix is never stripped, and 42 such cached symbols (DCE.l/pp/v x 14) are
  expected from the baseline cache; a normal-looking FUTURE ins_class from the
  source does not clear the quarantine by itself.
- Unknown shapes and unknown lifecycle never leave the candidate denominator:
  they are registered ``UNKNOWN_UNCLASSIFIED`` / ``LIFECYCLE_UNKNOWN``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

from .contracts import (
    ContractError,
    InstrumentKind,
    LifecycleStatus,
    SHANGHAI_TZ,
    SourceIdentity,
    canonical_sha256,
)

BASELINE_VENUE_COUNTS = {"SHFE": 19, "INE": 5, "DCE": 20, "CZCE": 24, "CFFEX": 8, "GFEX": 5}
BASELINE_PRODUCT_TOTAL = 81
EXPECTED_F_SUFFIX_COUNTS = {"DCE.l": 14, "DCE.pp": 14, "DCE.v": 14}
F_SUFFIX_TOTAL = 42

INVENTORY_SCHEMA_VERSION = "acq_w2_inventory_v1"

_MAIN_CONTINUOUS_RE = re.compile(r"^KQ\.m@([A-Z]+)\.(.+)$")
_INDEX_RE = re.compile(r"^KQ\.i@([A-Z]+)\.(.+)$")
_F_SUFFIX_RE = re.compile(r"^([A-Z]+)\.([A-Za-z0-9]+?[0-9]{2,4})F$")
_CONTRACT_4DIGIT_RE = re.compile(r"^([A-Z]+)\.([A-Za-z][A-Za-z0-9]*)([0-9]{4})$")
_CONTRACT_3DIGIT_RE = re.compile(r"^([A-Z]+)\.([A-Za-z][A-Za-z0-9]*)([0-9])(0[1-9]|1[0-2])$")

# Four-digit YYMM century pivot: listed Chinese futures contracts. Contracts
# with YY >= 70 are interpreted as 19YY, otherwise 20YY. This is a documented
# derivation; delivery provenance stays "derived_from_code" unless source
# expiry evidence upgrades it.
_YY_CENTURY_PIVOT = 70


@dataclass(frozen=True)
class ProductEntry:
    product_key: str  # normalized join key, e.g. "SHFE.rb"
    exchange: str
    source_product_code: str  # preserves source case
    status: str  # e.g. HISTORICAL_CATALOG_MEMBER_CURRENT_MEMBERSHIP_UNKNOWN
    priority: str  # P0..P5 from the dated plan, or "NEW"
    is_baseline: bool
    notes: tuple[str, ...] = ()

    def canonical(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "is_baseline": self.is_baseline,
            "notes": list(self.notes),
            "priority": self.priority,
            "product_key": self.product_key,
            "source_product_code": self.source_product_code,
            "status": self.status,
        }


@dataclass(frozen=True)
class SymbolEntry:
    source_symbol: str  # preserves source case
    product_key: str
    expired: Optional[bool]
    expire_datetime_nano: Optional[int]  # source evidence, integer ns
    observed_at_nano: int


class Universe:
    """Dated inventory of products and cached symbols (candidate inputs only)."""

    def __init__(
        self,
        products: Sequence[ProductEntry],
        symbols: Sequence[SymbolEntry],
        observed_at_nano: int,
        provenance: str,
    ) -> None:
        self.products: tuple[ProductEntry, ...] = tuple(products)
        self.symbols: tuple[SymbolEntry, ...] = tuple(symbols)
        self.observed_at_nano = observed_at_nano
        self.provenance = provenance
        keys = [p.product_key for p in self.products]
        if len(set(keys)) != len(keys):
            raise ContractError("duplicate product keys in inventory")
        symbol_keys = [s.source_symbol for s in self.symbols]
        if len(set(symbol_keys)) != len(symbol_keys):
            raise ContractError("duplicate source symbols in inventory")

    # -- construction -------------------------------------------------------
    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any], *, provenance: str) -> "Universe":
        if payload.get("schema_version") != INVENTORY_SCHEMA_VERSION:
            raise ContractError(
                f"inventory schema_version must be {INVENTORY_SCHEMA_VERSION}"
            )
        observed_at = int(payload["observed_at_nano"])
        products: list[ProductEntry] = []
        for item in payload.get("products", []):
            products.append(
                ProductEntry(
                    product_key=str(item["product_key"]),
                    exchange=str(item["exchange"]),
                    source_product_code=str(item["source_product_code"]),
                    status=str(item.get("status", "UNKNOWN")),
                    priority=str(item.get("priority", "UNKNOWN")),
                    is_baseline=bool(item.get("is_baseline", False)),
                    notes=tuple(str(n) for n in item.get("notes", ())),
                )
            )
        symbols: list[SymbolEntry] = []
        for item in payload.get("symbols", []):
            expire_nano = item.get("expire_datetime_nano")
            symbols.append(
                SymbolEntry(
                    source_symbol=str(item["source_symbol"]),
                    product_key=str(item["product_key"]),
                    expired=item.get("expired") if item.get("expired") is not None else None,
                    expire_datetime_nano=(
                        int(expire_nano) if expire_nano is not None else None
                    ),
                    observed_at_nano=observed_at,
                )
            )
        return cls(products, symbols, observed_at, provenance)

    # -- denominators (H02/H14) ---------------------------------------------
    def baseline_product_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for product in self.products:
            if product.is_baseline:
                counts[product.exchange] = counts.get(product.exchange, 0) + 1
        return counts

    def check_baseline_conservation(self) -> None:
        """Baseline partitions must sum to exactly 81 in the frozen split."""
        counts = self.baseline_product_counts()
        total = sum(counts.values())
        if total != BASELINE_PRODUCT_TOTAL or counts != BASELINE_VENUE_COUNTS:
            raise ContractError(
                "baseline denominator broken: "
                f"counts={counts} total={total}, expected {BASELINE_VENUE_COUNTS}"
            )

    def universe_version(self) -> str:
        payload = {
            "observed_at_nano": str(self.observed_at_nano),
            "products": [p.canonical() for p in self.products],
            "symbols": sorted(
                {
                    s.source_symbol: {
                        "expired": s.expired,
                        "expire_datetime_nano": (
                            str(s.expire_datetime_nano)
                            if s.expire_datetime_nano is not None
                            else None
                        ),
                        "product_key": s.product_key,
                    }
                    for s in self.symbols
                }.values(),
                key=lambda item: item["product_key"],
            ),
        }
        return canonical_sha256(payload)

    def product_for_symbol(self, source_symbol: str) -> Optional[ProductEntry]:
        """Match ``EX.code<digits>[F]`` back to its product entry by code."""
        parts = source_symbol.split(".", 1)
        if len(parts) != 2:
            return None
        exchange, code = parts
        code = re.sub(r"[0-9]{2,4}F?$", "", code)
        if not code:
            return None
        for product in self.products:
            if (
                product.exchange.upper() == exchange.upper()
                and product.source_product_code.upper() == code.upper()
            ):
                return product
        return None


class SymbolClassifier:
    """Classify source symbols into SourceIdentity records (no deletion)."""

    def __init__(self, universe: Universe) -> None:
        self._universe = universe

    def classify(
        self,
        source_symbol: str,
        *,
        expire_datetime_nano: Optional[int] = None,
        expired: Optional[bool] = None,
    ) -> SourceIdentity:
        observed_at = self._universe.observed_at_nano
        product = self._universe.product_for_symbol(source_symbol)
        product_key = product.product_key if product else source_symbol
        exchange = source_symbol.split(".", 1)[0] if "." in source_symbol else ""
        product_code = product.source_product_code if product else ""

        # Reference-only symbols (H02): counted separately, never as coverage.
        match = _MAIN_CONTINUOUS_RE.match(source_symbol)
        if match:
            return self._identity(
                source_symbol,
                InstrumentKind.MAIN_CONTINUOUS_REFERENCE,
                exchange=match.group(1),
                product_key=f"{match.group(1)}.{match.group(2)}",
                product_code=match.group(2),
                delivery=None,
                provenance="unknown",
                reason="continuous reference symbol; never counts as concrete coverage",
                lifecycle=LifecycleStatus.LIFECYCLE_UNKNOWN,
                observed_at=observed_at,
            )
        match = _INDEX_RE.match(source_symbol)
        if match:
            return self._identity(
                source_symbol,
                InstrumentKind.INDEX_REFERENCE,
                exchange=match.group(1),
                product_key=f"{match.group(1)}.{match.group(2)}",
                product_code=match.group(2),
                delivery=None,
                provenance="unknown",
                reason="index reference symbol; never counts as concrete coverage",
                lifecycle=LifecycleStatus.LIFECYCLE_UNKNOWN,
                observed_at=observed_at,
            )

        # H05: F suffix -> quarantine; the suffix is never stripped.
        if _F_SUFFIX_RE.match(source_symbol):
            return self._identity(
                source_symbol,
                InstrumentKind.F_SUFFIX_QUARANTINED,
                exchange=exchange,
                product_key=product_key,
                product_code=product_code,
                delivery=None,
                provenance="unknown",
                reason=(
                    "QUARANTINED_PENDING_CLASSIFICATION: F suffix meaning "
                    "unproven; stripping the suffix is forbidden (H05)"
                ),
                lifecycle=LifecycleStatus.LIFECYCLE_UNKNOWN,
                observed_at=observed_at,
            )

        # Concrete delivery months.
        match = _CONTRACT_4DIGIT_RE.match(source_symbol)
        if match and match.group(1) != "CZCE":
            exchange, code, digits = match.group(1), match.group(2), match.group(3)
            yymm = int(digits)
            yy, mm = divmod(yymm, 100)
            if not 1 <= mm <= 12:
                return self._unclassified(source_symbol, observed_at, "invalid_month")
            century = 1900 if yy >= _YY_CENTURY_PIVOT else 2000
            yyyymm = f"{century + yy:04d}-{mm:02d}"
            provenance = "derived_from_code"
            if expire_datetime_nano is not None:
                provenance = "source_expire_evidence"
            return self._identity(
                source_symbol,
                InstrumentKind.CONCRETE_FUTURE,
                exchange=exchange,
                product_key=f"{exchange}.{code}",
                product_code=code,
                delivery=yyyymm,
                provenance=provenance,
                reason=None,
                lifecycle=self._lifecycle(expired),
                observed_at=observed_at,
            )

        match = _CONTRACT_3DIGIT_RE.match(source_symbol)
        if match and match.group(1) == "CZCE":
            code, year_digit, month = match.group(2), match.group(3), match.group(4)
            if expire_datetime_nano is None:
                # H02: three-digit month codes need a full-year source mapping.
                return self._identity(
                    source_symbol,
                    InstrumentKind.CONCRETE_FUTURE,
                    exchange="CZCE",
                    product_key=f"CZCE.{code}",
                    product_code=code,
                    delivery=None,
                    provenance="unknown",
                    reason=(
                        "CZCE_3DIGIT_YEAR_AMBIGUOUS_REQUIRES_SOURCE_MAPPING: "
                        "decade cannot be inferred without source expiry evidence"
                    ),
                    lifecycle=self._lifecycle(expired),
                    observed_at=observed_at,
                )
            yyyymm = _yyyymm_from_expire_nano(expire_datetime_nano)
            return self._identity(
                source_symbol,
                InstrumentKind.CONCRETE_FUTURE,
                exchange="CZCE",
                product_key=f"CZCE.{code}",
                product_code=code,
                delivery=yyyymm,
                provenance="source_expire_evidence",
                reason=None,
                lifecycle=self._lifecycle(expired),
                observed_at=observed_at,
            )

        # Anything else stays in the candidate set as unknown.
        return self._unclassified(source_symbol, observed_at, "unrecognized_shape")

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _lifecycle(expired: Optional[bool]) -> LifecycleStatus:
        if expired is None:
            return LifecycleStatus.LIFECYCLE_UNKNOWN
        if expired:
            return LifecycleStatus.EXPIRED_IN_WINDOW
        return LifecycleStatus.ACTIVE

    @staticmethod
    def _identity(
        source_symbol: str,
        kind: InstrumentKind,
        *,
        exchange: str,
        product_key: str,
        product_code: str,
        delivery: Optional[str],
        provenance: str,
        reason: Optional[str],
        lifecycle: LifecycleStatus,
        observed_at: int,
    ) -> SourceIdentity:
        return SourceIdentity(
            exchange=exchange,
            product_key=product_key,
            source_product_code=product_code,
            source_symbol=source_symbol,
            instrument_kind=kind,
            delivery_yyyymm=delivery,
            delivery_provenance=provenance,
            delivery_unknown_reason=reason,
            lifecycle_status=lifecycle,
            classification_evidence=("classifier_v1",),
            observed_at_nano=observed_at,
        )

    @staticmethod
    def _unclassified(
        source_symbol: str, observed_at: int, reason: str
    ) -> SourceIdentity:
        exchange = source_symbol.split(".", 1)[0] if "." in source_symbol else ""
        return SourceIdentity(
            exchange=exchange,
            product_key=source_symbol,
            source_product_code="",
            source_symbol=source_symbol,
            instrument_kind=InstrumentKind.UNKNOWN_UNCLASSIFIED,
            delivery_yyyymm=None,
            delivery_provenance="unknown",
            delivery_unknown_reason=f"UNKNOWN_UNCLASSIFIED:{reason}",
            lifecycle_status=LifecycleStatus.LIFECYCLE_UNKNOWN,
            classification_evidence=("classifier_v1",),
            observed_at_nano=observed_at,
        )


def _yyyymm_from_expire_nano(expire_nano: int) -> str:
    """Render expiry evidence (integer ns) as ``YYYY-MM`` in Shanghai time."""
    from datetime import datetime, timedelta, timezone

    seconds = expire_nano // 1_000_000_000
    moment = (
        datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    ).astimezone(SHANGHAI_TZ)
    return f"{moment.year:04d}-{moment.month:02d}"


@dataclass(frozen=True)
class DenominatorExpansion:
    """H02 two-layer denominator: product baseline + concrete contract sets."""

    universe_version: str
    product_count_baseline: int
    product_count_new: int
    concrete_by_product: dict[str, tuple[str, ...]]
    reference_only: tuple[str, ...]  # KQ.m / KQ.i, never counted as coverage
    quarantined: tuple[str, ...]  # H05 F-suffix queue
    unclassified: tuple[str, ...]
    lifecycle_unknown: tuple[str, ...]
    notes: tuple[str, ...] = ()

    def canonical(self) -> dict[str, Any]:
        return {
            "concrete_by_product": {
                key: list(value) for key, value in sorted(self.concrete_by_product.items())
            },
            "lifecycle_unknown": sorted(self.lifecycle_unknown),
            "notes": list(self.notes),
            "product_count_baseline": self.product_count_baseline,
            "product_count_new": self.product_count_new,
            "quarantined": sorted(self.quarantined),
            "reference_only": sorted(self.reference_only),
            "unclassified": sorted(self.unclassified),
            "universe_version": self.universe_version,
        }


def expand_denominator(
    universe: Universe,
    classifier: Optional[SymbolClassifier] = None,
) -> DenominatorExpansion:
    """Expand per-product concrete contract sets; preserve every candidate."""
    classifier = classifier or SymbolClassifier(universe)
    concrete: dict[str, list[str]] = {}
    reference: list[str] = []
    quarantined: list[str] = []
    unclassified: list[str] = []
    lifecycle_unknown: list[str] = []

    for entry in universe.symbols:
        identity = classifier.classify(
            entry.source_symbol,
            expire_datetime_nano=entry.expire_datetime_nano,
            expired=entry.expired,
        )
        if identity.instrument_kind in (
            InstrumentKind.MAIN_CONTINUOUS_REFERENCE,
            InstrumentKind.INDEX_REFERENCE,
        ):
            reference.append(entry.source_symbol)
        elif identity.instrument_kind == InstrumentKind.F_SUFFIX_QUARANTINED:
            quarantined.append(entry.source_symbol)
        elif identity.instrument_kind == InstrumentKind.CONCRETE_FUTURE:
            concrete.setdefault(identity.product_key, []).append(entry.source_symbol)
            if identity.delivery_yyyymm is None:
                lifecycle_unknown.append(entry.source_symbol)
        else:
            unclassified.append(entry.source_symbol)
        if identity.lifecycle_status == LifecycleStatus.LIFECYCLE_UNKNOWN:
            if entry.source_symbol not in lifecycle_unknown:
                lifecycle_unknown.append(entry.source_symbol)

    baseline = sum(1 for p in universe.products if p.is_baseline)
    new = sum(1 for p in universe.products if not p.is_baseline)
    notes: list[str] = []
    f_counts: dict[str, int] = {}
    for symbol in quarantined:
        key = symbol.rsplit(".", 1)[0]
        f_counts[key] = f_counts.get(key, 0) + 1
    if f_counts and f_counts != EXPECTED_F_SUFFIX_COUNTS:
        notes.append(
            "F_SUFFIX_COUNT_SHIFTED_FROM_BASELINE: observed "
            f"{f_counts} vs baseline {EXPECTED_F_SUFFIX_COUNTS}; shift recorded, "
            "quarantine preserved"
        )
    return DenominatorExpansion(
        universe_version=universe.universe_version(),
        product_count_baseline=baseline,
        product_count_new=new,
        concrete_by_product={k: tuple(sorted(v)) for k, v in concrete.items()},
        reference_only=tuple(sorted(set(reference))),
        quarantined=tuple(sorted(set(quarantined))),
        unclassified=tuple(sorted(set(unclassified))),
        lifecycle_unknown=tuple(sorted(set(lifecycle_unknown))),
        notes=tuple(notes),
    )


class MetadataQuota:
    """H10 metadata discovery budget: <=12 calls/run, <=100 explicit symbols/call."""

    def __init__(self, max_calls: int, max_symbols_per_call: int) -> None:
        self.max_calls = max_calls
        self.max_symbols_per_call = max_symbols_per_call
        self.calls_made = 0

    def charge_discovery(self, venue: str, product_filter: Optional[str] = None) -> None:
        if self.calls_made >= self.max_calls:
            raise ContractError("metadata discovery call budget exhausted (H10)")
        if not venue or "." in venue or not venue.isupper():
            raise ContractError(
                "discovery must target exactly one venue (optional product filter)"
            )
        self.calls_made += 1

    def charge_symbol_details(self, symbols: Sequence[str]) -> None:
        if self.calls_made >= self.max_calls:
            raise ContractError("metadata call budget exhausted (H10)")
        if not symbols or len(symbols) > self.max_symbols_per_call:
            raise ContractError(
                f"symbol detail batches must be 1..{self.max_symbols_per_call} symbols"
            )
        self.calls_made += 1


def register_new_product(
    universe: Universe,
    product: ProductEntry,
    symbols: Iterable[SymbolEntry],
) -> Universe:
    """New verified products enter a NEW dated denominator, never silently into 81."""
    if any(p.product_key == product.product_key for p in universe.products):
        raise ContractError("product already registered")
    merged = Universe(
        products=[*universe.products, product],
        symbols=[*universe.symbols, *symbols],
        observed_at_nano=universe.observed_at_nano,
        provenance=universe.provenance + "+new_product",
    )
    return merged


__all__ = [
    "BASELINE_PRODUCT_TOTAL",
    "BASELINE_VENUE_COUNTS",
    "EXPECTED_F_SUFFIX_COUNTS",
    "F_SUFFIX_TOTAL",
    "DenominatorExpansion",
    "INVENTORY_SCHEMA_VERSION",
    "MetadataQuota",
    "ProductEntry",
    "SymbolClassifier",
    "SymbolEntry",
    "Universe",
    "expand_denominator",
    "register_new_product",
]
