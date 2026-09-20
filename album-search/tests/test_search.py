from decimal import Decimal

import pytest

from groove_search.domain import Shop
from groove_search.fetching import FakeFetcher
from groove_search.normalize import normalize_lines
from groove_search.recipes import SearchRecipe, build_search_url
from groove_search.registry import Registry
from groove_search.search import search_albums
from groove_search.websearch import FakeSearch, SearchHit

ALBUMS = """
-> Pet Fox - A face in your life
-> Abase - Awakening
-> Whitest Boy Alive
"""


def page(*products: tuple[str, str, str]) -> str:
    blocks = "".join(
        f'<li class="product"><a class="pname" href="{href}">{title}</a>'
        f'<span class="price">{price}</span><span class="stock">Dostępny</span></li>'
        for title, price, href in products
    )
    return f"<html><body><ul class='grid'>{blocks}</ul></body></html>"


def recipe(shop_id: str, base: str) -> SearchRecipe:
    return SearchRecipe(
        shop_id=shop_id,
        search_url=f"{base}/search?q={{query}}",
        item_selector="li.product",
        title_selector="a.pname",
        price_selector="span.price",
        link_selector="a.pname",
        availability_selector="span.stock",
    )


@pytest.fixture
def setup():
    shops = [
        Shop(id="alfa", name="Alfa Records", base_url="https://alfa.pl"),
        Shop(id="beta", name="Beta Vinyl", base_url="https://beta.pl"),
        Shop(id="gamma", name="Gamma Music", base_url="https://gamma.pl"),
    ]
    registry = Registry(shops=shops, recipes={s.id: recipe(s.id, s.base_url) for s in shops})

    fetcher = FakeFetcher(default=page())  # nothing found unless stated below
    for shop, price in (("alfa", "149,00 zł"), ("beta", "119,99 zł"), ("gamma", "165,00 zł")):
        for term in ("pet fox face in your life", "Pet Fox A face in your life"):
            url = build_search_url(recipe(shop, f"https://{shop}.pl"), term)
            fetcher.pages[url] = page(
                ("Pet Fox - A Face In Your Life LP", price, "/p/petfox"),
                ("Pet Fox - T-Shirt", "89,00 zł", "/p/shirt"),
            )
    url = build_search_url(recipe("alfa", "https://alfa.pl"), "whitest boy alive")
    fetcher.pages[url] = page(("The Whitest Boy Alive - Dreams LP", "139,00 zł", "/p/wba"))
    return registry, fetcher


@pytest.mark.anyio
async def test_finds_the_cheapest_offer_and_ranks_alternatives(setup):
    registry, fetcher = setup
    results = await search_albums(normalize_lines(ALBUMS), registry, fetcher)

    pet_fox = results[0]
    assert pet_fox.available
    assert pet_fox.best.shop_name == "Beta Vinyl"
    assert pet_fox.best.price.amount == Decimal("119.99")
    assert [o.shop_id for o in pet_fox.alternatives] == ["alfa", "gamma"]
    # Savings are measured on what the buyer pays, not what is listed: with
    # 15 PLN domestic postage on each, 134.99 against a priciest of 180.00 is
    # ~25%. Always a share of the priciest, so never above 100%.
    saving = pet_fox.savings_vs_worst()
    assert Decimal("25") < saving < Decimal("26")
    assert pet_fox.best.landed.total.amount == Decimal("134.99")


@pytest.mark.anyio
async def test_album_no_shop_carries_is_reported_unavailable(setup):
    registry, fetcher = setup
    results = await search_albums(normalize_lines(ALBUMS), registry, fetcher)

    abase = results[1]
    assert not abase.available
    assert abase.best is None
    # All three shops were reached successfully; none of them stocks it.
    assert abase.shops_searched == 3
    assert not abase.shops_failed
    assert all(r.offers_found == 0 for r in abase.reports)


@pytest.mark.anyio
async def test_artist_only_query_matches_any_release(setup):
    registry, fetcher = setup
    results = await search_albums(normalize_lines(ALBUMS), registry, fetcher)

    wba = results[2]
    assert wba.available
    assert "Dreams" in wba.best.title
    assert wba.best.url == "https://alfa.pl/p/wba"


@pytest.mark.anyio
async def test_merchandise_never_becomes_the_best_offer(setup):
    registry, fetcher = setup
    results = await search_albums(normalize_lines(ALBUMS), registry, fetcher)
    assert "Shirt" not in results[0].best.title
    assert all("Shirt" not in o.title for o in results[0].alternatives)


@pytest.mark.anyio
async def test_a_dead_shop_is_recorded_without_breaking_the_search(setup):
    registry, fetcher = setup
    registry.shops.append(Shop(id="dead", name="Dead Shop", base_url="https://dead.pl"))
    registry.recipes["dead"] = recipe("dead", "https://nowhere.invalid")
    # A network-level failure, not a 404: a 404 search page just means
    # "nothing found", which must not mark a shop as broken.
    fetcher.default = None
    fetcher.miss_status = 0
    fetcher.miss_error = "ConnectError: name resolution failed"

    results = await search_albums(normalize_lines(ALBUMS), registry, fetcher)

    assert registry.health_of("dead").consecutive_failures == 3
    assert any(r.shop_id == "dead" for r in results[0].shops_failed)


@pytest.mark.anyio
async def test_cheaper_foreign_offer_wins_only_after_conversion(setup):
    registry, fetcher = setup
    registry.shops.append(Shop(id="uk", name="UK Sounds", base_url="https://uk.pl", currency="GBP"))
    registry.recipes["uk"] = recipe("uk", "https://uk.pl")
    for term in ("pet fox face in your life", "Pet Fox A face in your life"):
        url = build_search_url(recipe("uk", "https://uk.pl"), term)
        # £20 is a smaller number than 119,99 but ~101 zl - still the winner.
        fetcher.pages[url] = page(("Pet Fox - A Face In Your Life LP", "£20.00", "/p/uk"))

    results = await search_albums(normalize_lines(ALBUMS), registry, fetcher)
    assert results[0].best.shop_id == "uk"
    assert results[0].best.price.currency == "GBP"  # displayed as the shop priced it


@pytest.mark.anyio
async def test_a_404_search_page_means_no_results_not_a_broken_shop(setup):
    """Several shops answer a fruitless search with a 404; that is not a fault."""
    registry, fetcher = setup
    fetcher.default = None  # unknown URLs 404

    await search_albums(normalize_lines("Abase - Awakening"), registry, fetcher)

    assert registry.health_of("alfa").consecutive_failures == 0
    assert registry.health_of("alfa").status == "healthy"


@pytest.mark.anyio
async def test_the_open_web_supplies_offers_no_shop_carries(setup):
    """The reason the open-web source exists: a record no seeded shop stocks.

    The three test albums are US/EU indie that Polish generalists do not
    carry, so before this source the honest answer was "Not available" while
    the record was on sale abroad.
    """
    from groove_search.websearch import FakeSearch, SearchHit

    registry, fetcher = setup
    engine = FakeSearch(
        default=[SearchHit("Abase - Awakening LP", "https://label.example/p/abase-awakening")]
    )
    fetcher.pages["https://label.example/p/abase-awakening"] = (
        '<html><head><script type="application/ld+json">'
        '{"@type":"Product","name":"Abase - Awakening LP",'
        '"offers":{"@type":"Offer","price":"18.00","priceCurrency":"EUR",'
        '"availability":"http://schema.org/InStock"}}'
        "</script></head><body></body></html>"
    )

    results = await search_albums(normalize_lines(ALBUMS), registry, fetcher, provider=engine)
    abase = next(r for r in results if "Abase" in r.query.label)

    assert abase.available
    assert abase.best.shop_id == "label.example"
    assert abase.best.price.currency == "EUR"
    # Priced delivered, not listed: 18 EUR is 77.40 PLN plus EU postage.
    assert abase.best.landed.total.amount > abase.best.price.amount


@pytest.mark.anyio
async def test_a_foreign_bargain_does_not_beat_a_domestic_offer_on_postage(setup):
    """A cheaper listed price from abroad must not win once shipped."""
    from groove_search.websearch import FakeSearch, SearchHit

    registry, fetcher = setup
    engine = FakeSearch(
        default=[SearchHit("Pet Fox LP", "https://faraway.example/p/pet-fox")]
    )
    fetcher.pages["https://faraway.example/p/pet-fox"] = (
        '<html><head><script type="application/ld+json">'
        '{"@type":"Product","name":"Pet Fox - A Face In Your Life LP",'
        '"offers":{"@type":"Offer","price":"20.00","priceCurrency":"USD",'
        '"availability":"http://schema.org/InStock"}}'
        "</script></head><body></body></html>"
    )

    results = await search_albums(normalize_lines(ALBUMS), registry, fetcher, provider=engine)
    pet_fox = results[0]

    foreign = next(o for o in [pet_fox.best, *pet_fox.alternatives] if o.shop_id == "faraway.example")
    # $20 is ~79 PLN, cheaper than every Polish offer here on the listed price.
    assert foreign.price.amount < pet_fox.best.price.amount
    # But it lands dearer, so a domestic shop still wins.
    assert pet_fox.best.shop_id != "faraway.example"


@pytest.mark.anyio
async def test_without_a_provider_nothing_reaches_the_open_web(setup):
    registry, fetcher = setup
    results = await search_albums(normalize_lines(ALBUMS), registry, fetcher)
    assert all(r.best is None or r.best.shop_id in {"alfa", "beta", "gamma"} for r in results)


@pytest.mark.anyio
async def test_an_offer_knows_which_source_found_it(setup):
    """A shop missing a record and the open web missing it are different
    faults - a broken recipe versus engine coverage - so a reader who cannot
    tell the two apart cannot diagnose a surprising result."""
    registry, fetcher = setup
    url = "https://stranger.example/products/pet-fox"
    fetcher.pages[url] = (
        '<html><head><script type="application/ld+json">'
        '{"@type":"Product","name":"Pet Fox - A Face In Your Life LP",'
        '"offers":{"@type":"Offer","price":"99.00","priceCurrency":"PLN",'
        '"availability":"http://schema.org/InStock"}}</script></head><body></body></html>'
    )
    engine = FakeSearch(default=[SearchHit("Pet Fox LP", url)])

    results = await search_albums(
        normalize_lines("Pet Fox - A Face In Your Life"), registry, fetcher, provider=engine
    )

    by_source = {o.shop_name: o.from_open_web for o in results[0].offers}
    assert by_source, "expected at least one offer"
    assert any(web for web in by_source.values()), "the open-web offer is not marked"
    assert all(o.source_label in ("known shop", "web search") for o in results[0].offers)


@pytest.mark.anyio
async def test_the_open_web_is_reported_apart_from_the_shops(setup):
    """It is a source, not a shop - counting it as one is what made a search
    of seven shops report "checked 8 shops"."""
    registry, fetcher = setup

    results = await search_albums(
        normalize_lines("Pet Fox - A Face In Your Life"),
        registry,
        fetcher,
        provider=FakeSearch(default=[]),
    )

    result = results[0]
    assert result.web_report is not None
    assert all(r.shop_id != "openweb" for r in result.shop_reports)
    assert result.shops_searched == len([r for r in result.shop_reports if r.ok])


@pytest.mark.anyio
async def test_each_source_reports_itself_as_it_finishes(setup):
    """A search runs for tens of seconds; a caller that only hears at the end
    cannot tell a slow shop from a stuck one."""
    registry, fetcher = setup
    seen: list[tuple[str, str]] = []

    await search_albums(
        normalize_lines("Pet Fox - A Face In Your Life"),
        registry,
        fetcher,
        provider=FakeSearch(default=[]),
        on_progress=lambda query, report: seen.append((query.label, report.shop_name)),
    )

    reported = {name for _, name in seen}
    assert {s.name for s in registry.searchable} <= reported
    assert "web search" in reported


@pytest.mark.anyio
async def test_a_broken_progress_callback_does_not_lose_the_search(setup):
    """Reporting is a convenience; results are the point."""
    registry, fetcher = setup

    def explode(query, report):
        raise RuntimeError("the page went away")

    results = await search_albums(
        normalize_lines("Pet Fox - A Face In Your Life"), registry, fetcher, on_progress=explode
    )

    assert results[0].available


# --- Sellers that refuse us, reported rather than dropped -------------------


@pytest.mark.anyio
async def test_a_blocked_marketplace_reaches_the_result_as_a_lead(setup):
    """Allegro sells the record and blocks us; "Not available" would be false."""
    registry, fetcher = setup
    blocked = "https://allegro.pl/produkt/pet-fox"
    fetcher.pages[blocked] = None  # a FakeFetcher miss is a 404, so force a 403
    engine = FakeSearch(default=[SearchHit("Pet Fox - A Face In Your Life LP", blocked)])
    fetcher.miss_status, fetcher.miss_error = 403, "HTTP 403"

    results = await search_albums(
        normalize_lines("Pet Fox - A face in your life"), registry, fetcher, provider=engine
    )

    leads = results[0].leads
    assert [lead.host for lead in leads] == ["allegro.pl"]
    # A lead is not an offer: it has no price and never competes for best.
    assert all(offer.shop_id != "allegro.pl" for offer in results[0].offers)


@pytest.mark.anyio
async def test_one_lead_per_seller_however_many_pages_it_blocked(setup):
    """Allegro answers 403 for every URL; a column of them is noise."""
    registry, fetcher = setup
    pages = ["https://allegro.pl/produkt/one", "https://allegro.pl/produkt/two"]
    engine = FakeSearch(
        default=[
            SearchHit("Pet Fox - A Face In Your Life LP", pages[0]),
            SearchHit("Pet Fox - A Face In Your Life 2LP", pages[1]),
        ]
    )
    # The fixture's fetcher answers unknown URLs with an empty grid, so the
    # refusal has to be stated for both pages.
    for url in pages:
        fetcher.pages[url] = None
    fetcher.miss_status, fetcher.miss_error = 403, "HTTP 403"

    results = await search_albums(
        normalize_lines("Pet Fox - A face in your life"), registry, fetcher, provider=engine
    )

    assert len(results[0].leads) == 1


@pytest.mark.anyio
async def test_the_result_says_which_engines_were_asked(setup):
    registry, fetcher = setup
    engine = FakeSearch(name="alpha", default=[SearchHit("nothing here", "https://nowhere.example/products/x")])

    results = await search_albums(
        normalize_lines("Pet Fox - A face in your life"), registry, fetcher, provider=engine
    )

    assert "alpha" in results[0].web_report.engines_used
