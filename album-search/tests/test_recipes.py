"""Extraction rules: pure, no network, no fixtures beyond the HTML inline here."""

import pytest

from groove_search.recipes import SearchRecipe, build_search_url, extract_offers

BASE = "https://shop.pl/search?q=x"


def recipe(**overrides) -> SearchRecipe:
    defaults = dict(
        shop_id="shop",
        search_url="https://shop.pl/search?q={query}",
        item_selector="li.product",
        title_selector="a.name",
        price_selector="span.price",
    )
    return SearchRecipe(**{**defaults, **overrides})


def test_prefers_the_title_link_over_an_artist_link():
    """A block's first anchor is often the artist; the buyer wants the record."""
    html = """<ul>
      <li class="product">
        <a href="/artists/Pet+Fox/">Pet Fox</a>
        <a class="name" href="/products/a-face-in-your-life">Pet Fox - A Face In Your Life</a>
        <span class="price">119,99 zł</span>
      </li></ul>"""
    offers = extract_offers(recipe(), html, BASE)
    assert len(offers) == 1
    assert offers[0].url == "https://shop.pl/products/a-face-in-your-life"


def test_drops_blocks_that_only_link_to_a_taxonomy_page():
    html = """<ul>
      <li class="product"><a class="name" href="/labels/Bubbles/">The Whitest Boy Alive</a>
        <span class="price">97,46 zł</span></li>
      <li class="product"><a class="name" href="/products/dreams-lp">Whitest Boy Alive - Dreams</a>
        <span class="price">139,00 zł</span></li></ul>"""
    offers = extract_offers(recipe(), html, BASE)
    assert [o.url for o in offers] == ["https://shop.pl/products/dreams-lp"]


def test_a_fallback_price_needs_an_explicit_currency():
    """Without this, a footer's "2024" becomes a 2024 zl price tag."""
    html = """<ul>
      <li class="product"><a class="name" href="/p/1">Informacje o firmie 2024</a></li>
      <li class="product"><a class="name" href="/p/2">Abase - Awakening</a> 89,00 zł</li></ul>"""
    offers = extract_offers(recipe(price_selector=None), html, BASE)
    assert [o.url for o in offers] == ["https://shop.pl/p/2"]


def test_blocks_without_a_price_are_skipped():
    html = """<ul>
      <li class="product"><a class="name" href="/p/1">No price here</a></li>
      <li class="product"><a class="name" href="/p/2">Priced</a><span class="price">49 zł</span></li>
      </ul>"""
    assert len(extract_offers(recipe(), html, BASE)) == 1


def test_duplicate_products_are_collapsed():
    block = '<li class="product"><a class="name" href="/p/1">X</a><span class="price">49 zł</span></li>'
    assert len(extract_offers(recipe(), f"<ul>{block}{block}</ul>", BASE)) == 1


def test_relative_urls_and_images_resolve_against_the_page():
    html = """<ul><li class="product"><img src="/img/1.jpg"/>
      <a class="name" href="../p/1">X</a><span class="price">49 zł</span></li></ul>"""
    offer = extract_offers(recipe(), html, "https://shop.pl/search/results")[0]
    assert offer.url == "https://shop.pl/p/1"
    assert offer.image_url == "https://shop.pl/img/1.jpg"


def test_availability_falls_back_to_the_block_text():
    html = """<ul><li class="product"><a class="name" href="/p/1">X</a>
      <span class="price">49 zł</span><span>Chwilowo niedostępny</span></li></ul>"""
    assert "niedost" in extract_offers(recipe(), html, BASE)[0].availability_text


@pytest.mark.parametrize(
    "encoding,expected",
    [
        ("plus", "https://shop.pl/search?q=pet+fox"),
        ("percent", "https://shop.pl/search?q=pet%20fox"),
        ("dash", "https://shop.pl/search?q=pet-fox"),
    ],
)
def test_query_encodings(encoding, expected):
    assert build_search_url(recipe(space_encoding=encoding), " pet   fox ") == expected


def test_a_recipe_without_a_placeholder_is_rejected():
    with pytest.raises(ValueError, match="placeholder"):
        build_search_url(recipe(search_url="https://shop.pl/search"), "x")


def test_recipe_survives_a_json_round_trip():
    original = recipe(needs_browser=True, probe_hits=7)
    assert SearchRecipe.from_dict(original.to_dict()) == original


def test_from_dict_ignores_unknown_fields_from_an_older_registry():
    data = recipe().to_dict() | {"legacy_field": "gone"}
    assert SearchRecipe.from_dict(data).shop_id == "shop"


class TestRobotsGate:
    """Politeness must not depend on which adapter fetches the page."""

    @pytest.mark.anyio
    async def test_disallowed_path_is_refused(self, monkeypatch):
        import urllib.robotparser

        from groove_search.fetching import RobotsGate

        parser = urllib.robotparser.RobotFileParser()
        parser.parse(["User-agent: *", "Disallow: /search"])
        gate = RobotsGate()

        async def fake_load(url):
            return parser

        monkeypatch.setattr(gate, "_load", fake_load)
        assert not await gate.allowed("https://shop.pl/search?q=x")
        assert await gate.allowed("https://shop.pl/products/1")

    @pytest.mark.anyio
    async def test_missing_robots_file_means_no_restriction(self, monkeypatch):
        from groove_search.fetching import RobotsGate

        gate = RobotsGate()

        async def fake_load(url):
            return None

        monkeypatch.setattr(gate, "_load", fake_load)
        assert await gate.allowed("https://shop.pl/search?q=x")

    @pytest.mark.anyio
    async def test_the_browser_adapter_checks_robots_too(self, monkeypatch):
        """A headless browser is still a robot."""
        from groove_search.fetching import BrowserFetcher, RobotsGate

        gate = RobotsGate()

        async def refuse(url):
            return False

        monkeypatch.setattr(gate, "allowed", refuse)
        page = await BrowserFetcher(robots=gate).get("https://shop.pl/search?q=x")
        assert page.error == "blocked by robots.txt"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("W promocji THE BEATLES Rubber Soul CD66,99 zł brutto", "THE BEATLES Rubber Soul CD"),
        ("Nowość Abase - Awakening LP 129,00 zł", "Abase - Awakening LP"),
        ("Pet Fox - A Face In Your Life", "Pet Fox - A Face In Your Life"),
        ("Promocja Nowość Slowdive Souvlaki", "Slowdive Souvlaki"),
    ],
)
def test_titles_are_tidied_of_prices_and_promo_banners(raw, expected):
    html = f'<ul><li class="product"><a class="name" href="/p/1">{raw}</a>'\
           f'<span class="price">63,64 zł</span></li></ul>'
    assert extract_offers(recipe(), html, BASE)[0].title_text == expected


def test_script_bodies_never_reach_a_title():
    """A block-level title otherwise swallows inline JavaScript."""
    html = """<ul><li class="product">
      <a class="name" href="/p/1">The Beatles – Rubber Soul (Vinyl, LP)</a>
      <script>jQuery(function($){ $("body").on("click", ".add_to_basket"); });</script>
      <span class="price">85,00 zł</span></li></ul>"""
    offer = extract_offers(recipe(title_selector=None, price_selector=None), html, BASE)[0]
    assert "jQuery" not in offer.title_text
    assert "Rubber Soul" in offer.title_text


def test_unit_and_tax_suffixes_are_stripped():
    html = """<ul><li class="product">
      <a class="name" href="/p/1">THE BEATLES Rubber Soul CD / szt. brutto</a>
      <span class="price">63,64 zł</span></li></ul>"""
    assert extract_offers(recipe(), html, BASE)[0].title_text == "THE BEATLES Rubber Soul CD"
