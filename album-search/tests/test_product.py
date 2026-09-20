"""Reading a product page we have never seen before.

Each trap here was found on a live shop while building the open-web source;
the fixtures are reductions of real pages, not invented ones.
"""

import pytest

from groove_search.product import extract_product

JSON_LD = """
<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"THE BEATLES Rubber Soul CD",
 "image":["https://shop.pl/img/1.jpg"],
 "offers":{"@type":"Offer","price":"63.64","priceCurrency":"PLN",
           "availability":"http://schema.org/InStock"}}
</script></head><body><h1>Shop</h1></body></html>
"""

MICRODATA = """
<html><body itemscope itemtype="http://schema.org/Product">
  <h1 itemprop="name">Radiohead - OK Computer LP</h1>
  <span itemprop="price" content="149.00">149,00</span>
  <meta itemprop="priceCurrency" content="PLN"/>
  <link itemprop="availability" href="http://schema.org/OutOfStock"/>
</body></html>
"""

OPENGRAPH = """
<html><head>
  <meta property="og:title" content="Slowdive - Souvlaki"/>
  <meta property="product:price:amount" content="119.99"/>
  <meta property="product:price:currency" content="PLN"/>
  <meta property="og:image" content="https://shop.pl/s.jpg"/>
</head><body><h1>Sklep</h1></body></html>
"""


def test_json_ld_is_preferred_and_complete():
    facts = extract_product(JSON_LD)
    assert facts.source == "json-ld"
    assert facts.title == "THE BEATLES Rubber Soul CD"
    assert facts.price_text == "63.64 PLN"
    assert facts.image_url == "https://shop.pl/img/1.jpg"
    # schema.org writes availability as a URL; it has to reach the stock
    # reader as words or every product looks like it has unknown stock.
    assert "In Stock" in facts.availability_text


def test_microdata_is_read_when_there_is_no_json_ld():
    facts = extract_product(MICRODATA)
    assert facts.source == "microdata"
    assert facts.price_text == "149.00 PLN"
    assert "Out Of Stock" in facts.availability_text


def test_opengraph_is_read_when_there_is_no_markup_on_the_body():
    facts = extract_product(OPENGRAPH)
    assert facts.source == "opengraph"
    assert facts.title == "Slowdive - Souvlaki"
    assert facts.price_text == "119.99 PLN"


def test_a_price_without_a_currency_is_never_accepted():
    """A footer's "2024" parses as a price unless a currency is required."""
    html = """
    <html><body><h1>Some Record</h1>
      <div class="price">2024</div>
      <footer>2024 Shop</footer>
    </body></html>
    """
    assert extract_product(html) is None


def test_the_ex_vat_price_is_not_taken_as_the_price():
    """VinylMusic prints "Cena netto: 69,11 zł" beside the real 85,00 zł.

    Taking the net price undercuts the truth by the VAT rate and invents a
    best offer the shop never made.
    """
    html = """
    <html><head><title>The Beatles - Rubber Soul | VinylMusic</title></head><body>
      <h2>The Beatles - Rubber Soul</h2>
      <div class="product">
        <div class="price_minor">Cena netto: <span>69,11 zł</span></div>
        <div class="price"><span>85,00 zł</span></div>
      </div>
    </body></html>
    """
    facts = extract_product(html)
    assert facts.price_text == "85,00 zł"


def test_a_related_products_rail_does_not_supply_the_price():
    """A shop can render "you may also like" before the product's own block."""
    html = """
    <html><body>
      <ul class="related">
        <li><span class="price">19,00 zł</span> U2 - Zooropa</li>
        <li><span class="price">29,00 zł</span> George Harrison</li>
      </ul>
      <div class="main"><h1>The Beatles - Rubber Soul</h1>
        <span class="price">85,00 zł</span></div>
    </body></html>
    """
    facts = extract_product(html)
    assert facts.price_text == "85,00 zł"


def test_inline_script_never_leaks_into_the_title():
    html = """
    <html><body><h1>Pet Fox - A Face In Your Life</h1>
      <script>jQuery(function($){ var price = "1,00 zł"; });</script>
      <span class="price">$20.00</span>
    </body></html>
    """
    facts = extract_product(html)
    assert facts.title == "Pet Fox - A Face In Your Life"
    assert "jQuery" not in facts.price_text
    assert facts.price_text == "$20.00"


def test_the_shop_name_in_the_only_h1_is_not_the_product():
    """Exploding In Sound's <h1> is the shop; the product is only in <title>."""
    html = """
    <html><head><title>Exploding In Sound Records - Pet Fox - A Face In Your Life</title>
      <meta property="og:site_name" content="Exploding In Sound Records"/>
    </head>
    <body><h1>Exploding In Sound Records</h1>
      <table><tr><td>LP (Black Vinyl) - $20.00</td></tr></table>
    </body></html>
    """
    facts = extract_product(html)
    assert "Pet Fox" in facts.title
    assert "20.00" in facts.price_text


def test_a_sale_price_beats_the_struck_through_one():
    html = """
    <html><body><h1>Nirvana - Nevermind</h1>
      <div class="price"><del>159,99 zł</del> <ins>129,99 zł</ins></div>
    </body></html>
    """
    assert "129,99" in extract_product(html).price_text


@pytest.mark.parametrize("html", ["", "<html><body><p>Not a shop at all</p></body></html>"])
def test_a_page_that_sells_nothing_yields_nothing(html):
    assert extract_product(html) is None


def test_duckduckgo_results_are_parsed_and_adverts_dropped():
    """The keyless provider reads DuckDuckGo's no-JS results page.

    Sponsored rows are served from duckduckgo.com with an ad_domain parameter
    and point at marketplaces, not the shop page we want.
    """
    from groove_search.websearch import _parse_ddg

    html = """
    <html><body>
      <div class="result">
        <a class="result__a" href="https://duckduckgo.com/y.js?ad_domain=ebay.com&amp;ad_provider=bingv7aa">
          Buy Vinyl On eBay</a>
      </div>
      <div class="result">
        <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fshop.com%2Fproducts%2Fpet-fox&amp;rut=x">
          Pet Fox - A Face In Your Life LP</a>
        <a class="result__snippet">Black vinyl, in stock</a>
      </div>
      <div class="result">
        <a class="result__a" href="https://other.example/p/1">Direct link</a>
      </div>
    </body></html>
    """
    hits = _parse_ddg(html)
    assert [h.url for h in hits] == ["https://shop.com/products/pet-fox", "https://other.example/p/1"]
    assert hits[0].title == "Pet Fox - A Face In Your Life LP"
    assert hits[0].snippet == "Black vinyl, in stock"


def test_the_keyless_provider_is_the_default_without_a_key(monkeypatch):
    from groove_search.websearch import DuckDuckGoSearch, from_env

    for var in ("GROOVE_SEARCH_KEY", "BRAVE_API_KEY", "SERPER_API_KEY", "GROOVE_SEARCH_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    assert isinstance(from_env(), DuckDuckGoSearch)

    monkeypatch.setenv("GROOVE_SEARCH_PROVIDER", "none")
    assert from_env() is None
