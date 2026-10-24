from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest

from ak_trading.exchange_rule_sources import (
    build_exchange_sources_from_catalog,
    validate_sources_against_catalog,
)
from ak_trading.futures_catalog import (
    FuturesCatalogError,
    InstrumentKind,
    load_futures_catalog,
)


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "config" / "china_futures_catalog.json"


def _catalog_payload() -> dict[str, object]:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def test_production_catalog_covers_all_exchanges_and_only_futures():
    catalog = load_futures_catalog(CATALOG, require_all_exchanges=True)

    assert catalog.exchanges == ("SHFE", "INE", "DCE", "CZCE", "CFFEX", "GFEX")
    assert len(catalog.future_products) == 81
    assert all(product.instrument_kind is InstrumentKind.FUTURE for product in catalog.future_products)
    assert len(catalog.excluded_options) == 6
    assert {item.exchange for item in catalog.excluded_options} == set(catalog.exchanges)


def test_production_catalog_spec_urls_are_official_and_unique():
    catalog = load_futures_catalog(CATALOG, require_all_exchanges=True)
    domains = {source.exchange: source.official_domains for source in catalog.sources}

    with_spec = [product for product in catalog.products if product.spec_urls]
    assert {product.exchange for product in with_spec} == {
        "CFFEX",
        "CZCE",
        "DCE",
        "GFEX",
        "INE",
    }
    # CZCE AP's current contract page moved from spec_urls to source_url on
    # 2026-09-04 (the old /sspz/ycz/ page was stale), so the spec count is 56.
    # 2026-09-04: CZCE AP's spec moved into source_url (-1); GFEX PD/PT/SI
    # gained official margin-notice seed URLs (+3).
    # 2026-09-04: CZCE AP's spec moved into source_url (-1); GFEX gained
    # official notice/product-page seed URLs for PD/PT/SI/LC/PS (+4).
    assert len(with_spec) == 61
    for product in with_spec:
        assert product.source_url not in product.spec_urls
        assert len(set(product.spec_urls)) == len(product.spec_urls)
        for url in product.spec_urls:
            host = urlparse(url).hostname or ""
            assert any(
                host == domain or host.endswith("." + domain)
                for domain in domains[product.exchange]
            )


def test_spec_urls_reject_duplicates_and_invalid_entries(tmp_path: Path):
    raw = _catalog_payload()
    first = next(p for p in raw["products"] if not p.get("spec_urls"))
    first["spec_urls"] = [first["source_url"]]
    with pytest.raises(FuturesCatalogError, match="unique and exclude source_url"):
        load_futures_catalog(_write_catalog(tmp_path, raw))

    raw = _catalog_payload()
    first = next(p for p in raw["products"] if not p.get("spec_urls"))
    first["spec_urls"] = ["not-a-url"]
    with pytest.raises(FuturesCatalogError, match="spec_urls"):
        load_futures_catalog(_write_catalog(tmp_path, raw))

    raw = _catalog_payload()
    first = next(p for p in raw["products"] if not p.get("spec_urls"))
    first["spec_urls"] = []
    catalog = load_futures_catalog(_write_catalog(tmp_path, raw))
    assert all(not product.spec_urls for product in catalog.products if product.code == first["code"])


def test_catalog_builder_seeds_spec_urls_after_source_url():
    sources = build_exchange_sources_from_catalog(CATALOG)
    by_code = {
        (source.code, product.code): product
        for source in sources
        for product in source.products
    }
    czce_sa = by_code[("CZCE", "SA")]
    assert any("cjqh" in url for url in czce_sa.seed_urls)
    dce_m = by_code[("DCE", "M")]
    assert any("1391326" in url for url in dce_m.seed_urls)
    shfe_rb = by_code[("SHFE", "RB")]
    assert len(shfe_rb.seed_urls) == 1


def test_catalog_rejects_cross_exchange_source_url(tmp_path: Path):
    raw = _catalog_payload()
    raw["products"][0]["source_url"] = "https://www.dce.com.cn/not-shfe"

    with pytest.raises(FuturesCatalogError, match="official exchange domain"):
        load_futures_catalog(_write_catalog(tmp_path, raw))


def test_catalog_rejects_option_in_the_futures_universe(tmp_path: Path):
    raw = _catalog_payload()
    raw["products"][0]["instrument_kind"] = "option"

    with pytest.raises(FuturesCatalogError, match="only future products"):
        load_futures_catalog(_write_catalog(tmp_path, raw))


def test_catalog_source_builder_has_exact_exchange_product_pairs():
    sources = build_exchange_sources_from_catalog(CATALOG)

    assert tuple(source.code for source in sources) == ("SHFE", "INE", "DCE", "CZCE", "CFFEX", "GFEX")
    assert sum(len(source.products) for source in sources) == 81
    assert all(product.contract is None for source in sources for product in source.products)
    validate_sources_against_catalog(sources, CATALOG)


def test_catalog_rejects_source_url_with_credentials(tmp_path: Path):
    raw = _catalog_payload()
    raw["sources"][0]["catalog_url"] = "https://user:pass@www.shfe.com.cn/products"

    with pytest.raises(FuturesCatalogError, match="must not contain credentials"):
        load_futures_catalog(_write_catalog(tmp_path, raw))


def _write_catalog(tmp_path: Path, raw: dict[str, object]) -> Path:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path
