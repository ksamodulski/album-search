"""Deterministic, data-driven shop search.

A `SearchRecipe` is the whole of what groove-search knows about *how* to
search one shop: a URL template and a handful of selectors. Because a shop is
data rather than code, adding or repairing one never touches this module -
`calibration` writes a new recipe, and this runner executes it the same way
it executes every other.

`extract_offers` is pure: give it HTML and it gives back offers, which is how
the whole extraction path is tested without touching the network.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime

from selectolax.parser import HTMLParser, Node

from .domain import RawOffer
from .fetching import Fetcher, Page
from .pricing import looks_like_price, parse_money

# Placeholder a recipe's search_url must contain.
QUERY_TOKEN = "{query}"

# Listing pages that are not buyable products. A block's first link often
# points at the artist or label rather than the record.
# Prices and shop furniture that leak into a title taken from a whole block.
_PRICE_IN_TITLE = re.compile(
    r"\d[\d \u00a0.,]*\s*(?:z[l\u0142]|PLN|EUR|USD|GBP|CZK|\u20ac|\$|\u00a3)"
    r"(?:\s*(?:brutto|netto|z\s*VAT|incl\.?\s*VAT))?",
    re.IGNORECASE,
)
# Unit/tax furniture shops append to a product name.
_UNIT_SUFFIX = re.compile(
    r"(?:[/|,]\s*)?\b(?:szt\.?|sztuk[ai]?|pcs|pc|kpl\.?)\b|\b(?:brutto|netto)\b",
    re.IGNORECASE,
)
_PROMO_PREFIX = re.compile(
    r"^(?:w\s+promocji|promocja|promo|nowo[s\u015b][c\u0107]|nowo[s\u015b]ci|bestseller|"
    r"zapowied[z\u017a]|przedsprzeda[z\u017c]|polecamy|wyprzeda[z\u017c]|sale|new|hit)\b[\s:!-]*",
    re.IGNORECASE,
)

_NON_PRODUCT_PATH = re.compile(
    r"/(artists?|labels?|brands?|category|categories|kategorie?|producent|"
    r"search|szukaj|tag|tags|genre|gatunek|series)(/|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SearchRecipe:
    """How to ask one shop for albums and read its answer.

    `item_selector` picks each product block on the results page; the other
    selectors are resolved *inside* a block. A `None` selector means "fall
    back to the block's own text / first link", which is what calibration
    emits for the many shops whose markup is too flat to be more specific.
    """

    shop_id: str
    search_url: str
    item_selector: str
    title_selector: str | None = None
    price_selector: str | None = None
    link_selector: str | None = None
    image_selector: str | None = None
    availability_selector: str | None = None
    space_encoding: str = "plus"  # plus | percent | dash
    needs_browser: bool = False
    version: int = 1
    calibrated_at: str = ""
    validated_at: str = ""
    probe_hits: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> SearchRecipe:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def stamped(self, **changes) -> SearchRecipe:
        return replace(self, validated_at=datetime.now(UTC).isoformat(timespec="seconds"), **changes)


@dataclass(frozen=True, slots=True)
class RecipeRun:
    """Outcome of running one recipe for one search term."""

    offers: tuple[RawOffer, ...] = ()
    page_url: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def build_search_url(recipe: SearchRecipe, term: str) -> str:
    """Fill the recipe's template with a user term, encoded as the shop wants."""
    cleaned = " ".join(term.split())
    if recipe.space_encoding == "dash":
        encoded = urllib.parse.quote(cleaned.replace(" ", "-"), safe="-")
    elif recipe.space_encoding == "percent":
        encoded = urllib.parse.quote(cleaned, safe="")
    else:
        encoded = urllib.parse.quote_plus(cleaned)
    if QUERY_TOKEN not in recipe.search_url:
        raise ValueError(f"recipe for {recipe.shop_id} has no {QUERY_TOKEN} placeholder")
    return recipe.search_url.replace(QUERY_TOKEN, encoded)


async def run_recipe(recipe: SearchRecipe, term: str, fetcher: Fetcher) -> RecipeRun:
    """Search one shop for one term. Never raises; failures come back as `error`."""
    try:
        url = build_search_url(recipe, term)
    except ValueError as exc:
        return RecipeRun(error=str(exc))
    page = await fetcher.get(url)
    if not page.ok:
        return RecipeRun(page_url=url, error=page.error or f"HTTP {page.status}")
    offers = extract_offers(recipe, page.html, page.url)
    return RecipeRun(offers=offers, page_url=page.url)


def extract_offers(recipe: SearchRecipe, html: str, base_url: str) -> tuple[RawOffer, ...]:
    """Pull every product block out of a results page. Pure - no I/O."""
    tree = HTMLParser(html)
    # Script and style bodies are text to a parser but noise to a reader, and
    # they end up inside any title taken from a whole block.
    tree.strip_tags(["script", "style", "noscript", "template"])
    offers: list[RawOffer] = []
    seen: set[str] = set()
    for node in tree.css(recipe.item_selector):
        offer = _offer_from(node, recipe, base_url)
        if offer and offer.url not in seen:
            seen.add(offer.url)
            offers.append(offer)
    return tuple(offers)


def _offer_from(node: Node, recipe: SearchRecipe, base_url: str) -> RawOffer | None:
    link = _first(node, recipe.link_selector) if recipe.link_selector else None
    title_node = _first(node, recipe.title_selector)

    # The product's own title link is the one a buyer wants. Falling back to
    # the block's first anchor lands on an artist or label page instead.
    href = link.attributes.get("href") if link is not None else None
    if not href and title_node is not None:
        href = _own_href(title_node)
    if not href:
        href = _own_href(node)
    if not href or _NON_PRODUCT_PATH.search(urllib.parse.urlsplit(href).path):
        return None

    title = _clean(title_node.text()) if title_node is not None else ""
    if not title and link is not None:
        title = _clean(link.text())
    if not title:
        title = _clean(node.text())
    if not title:
        return None

    price_text = _text(node, recipe.price_selector)
    if price_text:
        if parse_money(price_text) is None:
            return None
    else:
        # Falling back to the whole block's text, so demand an explicit
        # currency: otherwise a year or an item count reads as a price.
        price_text = _clean(node.text())
        if not looks_like_price(price_text):
            return None
    title = _tidy_title(title)
    if not title:
        return None

    image = _first(node, recipe.image_selector) if recipe.image_selector else node.css_first("img")
    image_url = None
    if image is not None:
        raw_src = image.attributes.get("src") or image.attributes.get("data-src")
        if raw_src:
            image_url = urllib.parse.urljoin(base_url, raw_src)

    return RawOffer(
        shop_id=recipe.shop_id,
        title_text=title[:300],
        price_text=price_text[:120],
        url=urllib.parse.urljoin(base_url, href),
        # With no selector, the block's own text is the best stock signal
        # available - "do koszyka" and "niedostepne" both live inside it.
        availability_text=(_text(node, recipe.availability_selector) or _clean(node.text()))[:200],
        image_url=image_url,
    )


def _tidy_title(title: str) -> str:
    """Strip prices and promo banners a block-level title drags along.

    "W promocji THE BEATLES Rubber Soul CD66,99 zl brutto" -> "THE BEATLES
    Rubber Soul CD".
    """
    cleaned = _UNIT_SUFFIX.sub(" ", _PRICE_IN_TITLE.sub(" ", title))
    previous = None
    while previous != cleaned:  # banners sometimes stack: "Nowosc Promocja ..."
        previous = cleaned
        cleaned = _PROMO_PREFIX.sub("", cleaned.strip())
    return _clean(cleaned).strip(" -–—:|,./")


def _own_href(node: Node) -> str | None:
    if node.tag == "a" and node.attributes.get("href"):
        return node.attributes["href"]
    inner = node.css_first("a[href]")
    return inner.attributes.get("href") if inner is not None else None


def _first(node: Node, selector: str | None) -> Node | None:
    return node.css_first(selector) if selector else None


def _text(node: Node, selector: str | None) -> str:
    if not selector:
        return ""
    found = node.css_first(selector)
    return _clean(found.text()) if found is not None else ""


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())
