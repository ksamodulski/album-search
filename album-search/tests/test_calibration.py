from pathlib import Path

import pytest

from groove_search.calibration import calibrate
from groove_search.domain import Shop
from groove_search.fetching import FakeFetcher
from groove_search.recipes import build_search_url, extract_offers

FIXTURES = Path(__file__).parent / "fixtures"


EMPTY = "<html><body><p>Brak wyników</p></body></html>"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def shop_serving(name: str, *keywords: str):
    """A fetcher that returns products only when the URL carries the query.

    Calibration rejects endpoints that answer everything identically, so a
    fixture has to behave like a real search to be calibratable.
    """
    html = fixture(name)

    def responder(url: str) -> str:
        if "?" not in url and "searchquery" not in url:
            return html  # the homepage, for search-form discovery
        return html if any(k in url.lower() for k in keywords) else EMPTY

    return FakeFetcher(responder=responder)


@pytest.mark.anyio
async def test_learns_a_woocommerce_shop_from_its_markup():
    shop = Shop(id="woo", name="Woo Records", base_url="https://woo.example.pl")
    fetcher = shop_serving("shop_woo.html", "radiohead")

    result = await calibrate(shop, fetcher, probes=("radiohead ok computer",))

    assert result.ok, result.error
    recipe = result.recipe
    # It read the shop's own form, including the hidden post_type field.
    assert "s={query}" in recipe.search_url
    assert "post_type=product" in recipe.search_url
    offers = extract_offers(recipe, fixture("shop_woo.html"), "https://woo.example.pl/")
    titles = [o.title_text for o in offers]
    assert any("OK Computer (2LP)" in t for t in titles)
    assert len(offers) == 4  # the sidebar bestseller is not a product block
    assert all(o.url.startswith("https://woo.example.pl/produkt/") for o in offers)


@pytest.mark.anyio
async def test_learns_a_prestashop_shop_with_different_markup():
    shop = Shop(id="presta", name="Presta Vinyl", base_url="https://presta.example.pl")
    fetcher = shop_serving("shop_presta.html", "pink", "floyd")

    result = await calibrate(shop, fetcher, probes=("pink floyd wish you were here",))

    assert result.ok, f"{result.error}: {result.log}"
    offers = extract_offers(result.recipe, fixture("shop_presta.html"), "https://presta.example.pl/")
    assert len(offers) == 3
    assert any("Wish You Were Here" in o.title_text for o in offers)
    assert any("149,00" in o.price_text for o in offers)


@pytest.mark.anyio
async def test_reports_failure_when_the_page_has_no_products():
    shop = Shop(id="empty", name="Empty", base_url="https://empty.example.pl")
    fetcher = FakeFetcher(default=EMPTY)

    result = await calibrate(shop, fetcher)

    assert not result.ok
    assert result.error == "no product blocks recognised"
    assert result.log  # the log explains what was tried


@pytest.mark.anyio
async def test_unreachable_shop_fails_cleanly():
    shop = Shop(id="dead", name="Dead", base_url="https://dead.example.pl")
    result = await calibrate(shop, FakeFetcher())
    assert not result.ok


@pytest.mark.anyio
async def test_rejects_a_search_url_that_ignores_the_query():
    """A shop whose "search" always shows the same grid must not calibrate."""
    shop = Shop(id="lazy", name="Lazy Catalogue", base_url="https://lazy.example.pl")
    # Same products no matter what is asked - including nonsense.
    fetcher = FakeFetcher(default=fixture("shop_woo.html"))

    result = await calibrate(shop, fetcher, probes=("radiohead ok computer",))

    assert not result.ok
    assert any("ignores the query" in line for line in result.log)
