"""The open-web source: search engine in, verified offers out.

Fully offline - a fake engine and a fake fetcher stand in for the network, so
the whole pipeline can be exercised without a key or a live shop.
"""

from decimal import Decimal

import pytest

from groove_search.domain import Format
from groove_search.fetching import FakeFetcher
from groove_search.normalize import normalize_lines
from groove_search.openweb import find_offers
from groove_search.websearch import SearchHit, FakeSearch

FX = {"USD": Decimal("3.95"), "GBP": Decimal("5.05")}


def product_page(title: str, price: str, currency: str = "USD") -> str:
    return f"""
    <html><head><script type="application/ld+json">
    {{"@type":"Product","name":"{title}",
      "offers":{{"@type":"Offer","price":"{price}","priceCurrency":"{currency}",
                 "availability":"http://schema.org/InStock"}}}}
    </script></head><body><h1>{title}</h1></body></html>
    """


def query(line: str = "Pet Fox - A Face In Your Life"):
    return normalize_lines(line)[0]


@pytest.fixture
def engine():
    return FakeSearch(
        default=[
            SearchHit("Pet Fox - A Face In Your Life LP", "https://eis.com/products/pet-fox"),
            SearchHit("Pet Fox on Spotify", "https://open.spotify.com/album/x"),
            SearchHit("Some other record", "https://eis.com/products/other"),
        ]
    )


@pytest.mark.anyio
async def test_a_matching_page_becomes_a_costed_offer(engine):
    fetcher = FakeFetcher(
        pages={
            "https://eis.com/products/pet-fox": product_page("Pet Fox - A Face In Your Life LP", "20.00"),
            "https://eis.com/products/other": product_page("Nirvana - Nevermind LP", "25.00"),
        }
    )
    offers, report, _ = await find_offers(query(), engine, fetcher, rates=FX)

    assert len(offers) == 1
    offer = offers[0]
    assert offer.shop_id == "eis.com"
    assert offer.price.amount == Decimal("20.00")
    assert offer.price.currency == "USD"
    assert offer.format is Format.VINYL
    # Listed at $20 (79 PLN) but costed with postage and import VAT.
    assert offer.landed.total.amount > Decimal("200")
    assert report.ok


@pytest.mark.anyio
async def test_streaming_and_review_sites_are_never_fetched(engine):
    fetcher = FakeFetcher(default=product_page("Pet Fox - A Face In Your Life LP", "20.00"))
    await find_offers(query(), engine, fetcher, rates=FX)
    assert not any("spotify" in url for url in fetcher.requested)


@pytest.mark.anyio
async def test_a_page_that_is_the_wrong_record_is_rejected(engine):
    """The engine ranks loosely; the matcher is what keeps the result honest."""
    fetcher = FakeFetcher(default=product_page("Nirvana - Nevermind LP", "25.00"))
    offers, report, findings = await find_offers(query(), engine, fetcher, rates=FX)
    assert offers == []
    assert not report.ok
    assert all(f.offer is None for f in findings)


@pytest.mark.anyio
async def test_a_page_with_no_price_is_not_an_offer(engine):
    fetcher = FakeFetcher(default="<html><body><h1>Pet Fox - A Face In Your Life</h1></body></html>")
    offers, report, findings = await find_offers(query(), engine, fetcher, rates=FX)
    assert offers == []
    assert "no price" in report.error


@pytest.mark.anyio
async def test_shops_we_already_search_are_not_searched_twice(engine):
    fetcher = FakeFetcher(default=product_page("Pet Fox - A Face In Your Life LP", "20.00"))
    offers, _, _ = await find_offers(
        query(), engine, fetcher, rates=FX, known_hosts=frozenset({"eis.com"})
    )
    assert offers == []
    assert fetcher.requested == []


@pytest.mark.anyio
async def test_a_blocked_page_is_reported_rather_than_silently_dropped(engine):
    fetcher = FakeFetcher(pages={}, default=None, miss_status=403, miss_error="HTTP 403")
    offers, report, findings = await find_offers(query(), engine, fetcher, rates=FX)
    assert offers == []
    assert "403" in report.error
    assert any("403" in f.verdict for f in findings)


@pytest.mark.anyio
async def test_the_engine_is_asked_in_the_user_s_own_words(engine):
    fetcher = FakeFetcher(default=product_page("Pet Fox - A Face In Your Life LP", "20.00"))
    await find_offers(query(), engine, fetcher, rates=FX)
    assert any("Pet Fox - A Face In Your Life" in phrase for phrase in engine.asked)


@pytest.mark.anyio
async def test_a_dead_engine_is_an_empty_result_not_a_crash():
    fetcher = FakeFetcher(default=product_page("Pet Fox", "20.00"))
    offers, report, _ = await find_offers(query(), FakeSearch(), fetcher, rates=FX)
    assert offers == []
    assert report.offers_found == 0


@pytest.mark.anyio
async def test_country_is_inferred_from_the_domain_then_the_currency():
    engine = FakeSearch(
        default=[
            SearchHit("x", "https://shop.co.uk/p/pet-fox"),
            SearchHit("y", "https://shop.example/p/pet-fox"),
        ]
    )
    fetcher = FakeFetcher(
        pages={
            "https://shop.co.uk/p/pet-fox": product_page(
                "Pet Fox - A Face In Your Life LP", "22.49", "GBP"
            ),
            "https://shop.example/p/pet-fox": product_page(
                "Pet Fox - A Face In Your Life LP", "20.00", "USD"
            ),
        }
    )
    offers, _, _ = await find_offers(query(), engine, fetcher, rates=FX)
    by_host = {o.shop_id: o.country for o in offers}
    assert by_host["shop.co.uk"] == "GB"  # from the domain
    assert by_host["shop.example"] == "US"  # from the currency


@pytest.mark.parametrize(
    "url,is_listing",
    [
        ("https://us.rarevinyl.com/collections/the-whitest-boy-alive", True),
        ("https://shop.pl/kategoria/winyl", True),
        ("https://shop.com/artists/pet-fox", True),
        # Shopify puts products under a collection; the product segment wins.
        ("https://shop.com/collections/indie/products/pet-fox-lp", False),
        ("https://soundslikevinyl.com/products/whitest-boy-alive-lp", False),
        # A shop's own search results, which engines index freely. Amazon's
        # path gives nothing away; its query string does.
        ("https://www.amazon.com/whitest-boy-alive/s?k=the+whitest+boy+alive", True),
        ("https://shop.pl/szukaj?q=beatles", True),
        # Allegro's search results, which engines return far more often than
        # its product pages: neither the path nor `q` gives these away.
        ("https://allegro.pl/listing?string=the+beatles+rubber+soul", True),
        ("https://allegro.pl/kategoria/plyty-winylowe-279?string=beatles", True),
        ("https://allegrolokalnie.pl/oferty/q/the%20beatles", True),
        ("https://allegro.pl/produkt/the-beatles-rubber-soul-winyl-5fd296d1", False),
        # A variant parameter is not a search.
        ("https://shop.com/products/pet-fox-lp?variant=123", False),
    ],
)
def test_listing_pages_are_never_opened(url, is_listing):
    """A collection page's prices belong to other records.

    us.rarevinyl.com's Whitest Boy Alive collection yielded a "price" of
    18,200 USD - a page like that must not become an offer.
    """
    from groove_search.openweb import _is_listing

    assert _is_listing(url) is is_listing


@pytest.mark.parametrize(
    "host,currency,expected",
    [
        ("shop.pl", "PLN", "PL"),
        ("shop.co.uk", "PLN", "GB"),  # domain beats a localised currency
        ("bigshop.com", "USD", "US"),
        ("bigshop.com", "EUR", "EU"),
        # The dangerous one: a foreign shop showing PLN must NOT read as
        # Polish, or it gets domestic postage and dodges import VAT.
        ("bigshop.com", "PLN", None),
    ],
)
def test_country_inference_never_guesses_its_way_home(host, currency, expected):
    from groove_search.openweb import _country_of

    assert _country_of(host, currency) == expected


@pytest.mark.anyio
async def test_one_shop_cannot_fill_the_whole_candidate_budget():
    """Engines happily return eight eBay listings; we want the market's view."""
    from groove_search.openweb import MAX_PER_HOST

    engine = FakeSearch(
        default=[SearchHit(f"listing {i}", f"https://ebay.com/itm/{i}") for i in range(6)]
        + [SearchHit("label", "https://label.example/products/pet-fox")]
    )
    fetcher = FakeFetcher(default=product_page("Pet Fox - A Face In Your Life LP", "20.00"))
    await find_offers(query(), engine, fetcher, rates=FX)

    from_ebay = [u for u in fetcher.requested if "ebay.com" in u]
    assert len(from_ebay) == MAX_PER_HOST
    assert any("label.example" in u for u in fetcher.requested)


@pytest.mark.anyio
async def test_an_engine_refusal_is_not_reported_as_an_unavailable_album():
    """"The engine rate-limited us" and "no shop sells this" are opposite
    facts, and only one of them is the user's problem to act on."""
    engine = FakeSearch()
    engine.last_error = "DuckDuckGo is rate-limiting us"
    offers, report, _ = await find_offers(query(), engine, FakeFetcher(), rates=FX)

    assert offers == []
    assert not report.ok
    assert "rate-limiting" in report.error


def test_duckduckgo_throttling_is_detected_despite_a_2xx_status():
    """DuckDuckGo answers a throttle with 202 and a notice, not a 429."""
    from groove_search.websearch import _is_throttled

    assert _is_throttled(202, "<html>anything</html>")
    assert _is_throttled(200, "<html>If this error persists... anomaly detected</html>")
    assert not _is_throttled(200, "<html><div class='result'>a shop</div></html>")


def test_a_searxng_url_adds_that_provider(monkeypatch):
    """Self-hosted SearXNG: keyless like DuckDuckGo, but not rate-limited.

    It joins the engines asked rather than replacing them. Coverage differs
    between engines more than ranking does - `Abase - Awakening` came back
    empty through SearXNG while DuckDuckGo found it - so configuring a
    sturdier engine must not cost the records the old one could still see.
    """
    from groove_search.websearch import SearxngSearch, from_env

    for var in ("GROOVE_SEARCH_KEY", "BRAVE_API_KEY", "SERPER_API_KEY", "GROOVE_SEARCH_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GROOVE_SEARXNG_URL", "http://127.0.0.1:8888")
    provider = from_env()
    searxng = next(p for p in provider.providers if isinstance(p, SearxngSearch))
    assert searxng.base_url == "http://127.0.0.1:8888"


# --- Marketplaces: the ones that answer, and the ones that refuse ----------


@pytest.mark.parametrize(
    "url",
    [
        # Live false positives, all of them: a grid of many records whose
        # price belongs to whichever one happened to be first.
        "https://www.amazon.com/CDs-Vinyl-Radiohead/s?rh=n%3A5174",
        "https://www.amazon.com/whitest-boy-alive/s?k=whitest+boy",
        "https://www.amazon.com/clp/B000YIXBV8",
        "https://www.ebay.com/shop/in-rainbows-vinyl?_nkw=in+rainbows",
        "https://www.ebay.pl/sch/P-yty-CD/176984/i.html?_nkw=daft+punk",
        "https://www.ebay.co.uk/b/bn_7024916481",
        "https://allegro.pl/listing?string=daft+punk+discovery",
        "https://allegro.pl/kategoria/muzyka?string=radiohead",
    ],
)
def test_a_marketplace_results_grid_is_not_a_product(url):
    from groove_search.openweb import _is_listing

    assert _is_listing(url), f"{url} would have been priced as if it sold one record"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.amazon.com/Bookends-Vinyl-Simon-Garfunkel/dp/B001EQP9UK",
        "https://www.amazon.co.uk/Rainbows-VINYL-Radiohead/dp/B000YIXBV8",
        "https://allegro.pl/produkt/discovery-2lp-daft-punk-winyl-03a65af4",
        "https://www.ebay.com/itm/126298853123",
        # Shopify: a product living under a collection path.
        "https://shop.example.com/collections/rock/products/in-rainbows",
    ],
)
def test_a_marketplace_product_page_survives_the_listing_filter(url):
    from groove_search.openweb import _is_listing

    assert not _is_listing(url), f"{url} is a single record and was thrown away"


def test_amazons_country_comes_from_the_host_since_it_localises_prices():
    """Amazon quotes a US record in PLN here, so the currency proves nothing."""
    from groove_search.openweb import _country_of

    assert _country_of("amazon.com", "PLN") == "US"
    # A marketplace of scattered sellers gets no invented country.
    assert _country_of("ebay.com", "PLN") is None


def test_the_fetch_budget_goes_to_domestic_shops_first():
    """Postage and VAT decide the ranking, so foreign pages are the long shot."""
    from groove_search.openweb import _spread

    ordered = _spread(
        [
            SearchHit("a", "https://us-shop.com/products/x"),
            SearchHit("b", "https://uk-shop.co.uk/products/x"),
            SearchHit("c", "https://sklep.pl/products/x"),
            SearchHit("d", "https://another.pl/products/x"),
        ],
        max_pages=2,
        home="PL",
    )

    assert [h.host for h in ordered] == ["sklep.pl", "another.pl"]


@pytest.mark.anyio
async def test_a_blocked_seller_becomes_a_lead_not_a_silence():
    """Allegro answers 403 to everyone; saying nothing would misreport it."""
    engine = FakeSearch(
        default=[SearchHit("Pet Fox - A Face In Your Life LP - Allegro", "https://allegro.pl/produkt/pet-fox")]
    )
    fetcher = FakeFetcher(pages={}, miss_status=403, miss_error="HTTP 403")

    offers, report, findings = await find_offers(query(), engine, fetcher)

    assert offers == []
    leads = [f.lead for f in findings if f.lead]
    assert len(leads) == 1
    assert leads[0].host == "allegro.pl"
    assert "403" in leads[0].reason


@pytest.mark.anyio
async def test_a_blocked_page_for_another_record_is_not_a_lead():
    """The bar is the same strict matcher an offer has to clear."""
    engine = FakeSearch(default=[SearchHit("Nirvana - Nevermind LP", "https://allegro.pl/produkt/nirvana")])
    fetcher = FakeFetcher(pages={}, miss_status=403, miss_error="HTTP 403")

    _, _, findings = await find_offers(query(), engine, fetcher)

    assert [f.lead for f in findings if f.lead] == []


@pytest.mark.anyio
async def test_a_missing_page_is_not_a_lead():
    """A 404 is a page that does not exist - there is nowhere to send anyone."""
    engine = FakeSearch(
        default=[SearchHit("Pet Fox - A Face In Your Life LP", "https://shop.example/products/pet-fox")]
    )
    fetcher = FakeFetcher(pages={})  # defaults to 404

    _, _, findings = await find_offers(query(), engine, fetcher)

    assert [f.lead for f in findings if f.lead] == []


@pytest.mark.anyio
async def test_the_report_names_the_engines_that_were_asked():
    engine = FakeSearch(
        name="alpha",
        default=[SearchHit("Pet Fox - A Face In Your Life LP", "https://shop.example/products/pet-fox")],
    )
    fetcher = FakeFetcher(pages={})

    _, report, _ = await find_offers(query(), engine, fetcher)

    assert [r.name for r in report.engines] == ["alpha"]
    # Two phrases are searched per album, so the tally is the sum of both.
    assert report.engines[0].hits == 2


@pytest.mark.anyio
async def test_an_offer_remembers_which_engine_found_it():
    engine = FakeSearch(
        name="alpha",
        default=[SearchHit("Pet Fox - A Face In Your Life LP", "https://shop.example/products/pet-fox")],
    )
    fetcher = FakeFetcher(
        pages={
            "https://shop.example/products/pet-fox": product_page(
                "Pet Fox - A Face In Your Life LP", "20.00"
            )
        }
    )

    offers, _, _ = await find_offers(query(), engine, fetcher, rates=FX)

    assert offers[0].found_via == "alpha"
    assert offers[0].engine_label == "alpha"
