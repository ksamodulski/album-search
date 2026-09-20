"""Read one product page and say what is actually for sale on it.

The recipe engine learns selectors for a shop's *search grid*. A page found
through the open web is a different animal: we have never seen the shop, so
there is nothing learned to apply. What saves us is that product pages - unlike
search grids - are the part of a webshop most likely to carry machine-readable
markup, because Google Shopping rewards it. So this module reads the page's own
declaration of what it sells, in descending order of trustworthiness, and only
falls back to guessing at the end.

Probing five live shops found the split to be real but partial: two carried
JSON-LD or embedded JSON, one had only a visible price, one had nothing in its
HTML at all, and one answered with a Cloudflare challenge. So every strategy
here earns its place, and `None` is a normal answer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal

from selectolax.parser import HTMLParser

from .pricing import looks_like_price

# schema.org writes availability as a URL; reduce it to words the shared
# stock-wording reader already understands.
_SCHEMA_AVAILABILITY = re.compile(r"schema\.org/(\w+)", re.IGNORECASE)
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")

# Embedded shop JSON writes the price a handful of ways.
_JSON_PRICE = re.compile(r'"(?:price|priceAmount|lowPrice|final_price)"\s*:\s*"?(\d+(?:[.,]\d{1,2})?)"?')

_CURRENCY_ATTR = re.compile(r'"(?:priceCurrency|currency|currency_code)"\s*:\s*"([A-Z]{3})"')


@dataclass(frozen=True, slots=True)
class ProductFacts:
    """What a product page claims about the single thing it sells."""

    title: str
    price_text: str
    availability_text: str = ""
    image_url: str | None = None
    # The performer, when the page names one in a field of its own. Kept
    # beside the title it has already been folded into, so a bad reading can
    # be traced to the field that produced it.
    artist: str = ""
    # Which strategy produced this, so a bad extraction can be traced to the
    # layer that made it rather than to "the scraper".
    source: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.title and self.price_text)


def extract_product(html: str, *, url: str = "") -> ProductFacts | None:
    """Best available reading of a product page, or None if it is not one.

    Strategies run most-trustworthy first and the first complete answer wins:
    a page that declares its price in JSON-LD is never second-guessed by
    scraping its visible text.
    """
    if not html:
        return None
    tree = HTMLParser(html)
    # Script and style bodies are text to the parser but noise to a reader;
    # leaving them in puts inline jQuery inside a product title. The recipe
    # runner learned this the hard way - see recipes.extract_offers.
    tree.strip_tags(["script", "style", "noscript", "template"])
    raw_tree = HTMLParser(html)  # JSON-LD lives *in* a <script>, so read it unstripped
    for strategy in (_from_json_ld, _from_microdata, _from_opengraph, _from_embedded_json, _from_visible_text):
        facts = strategy(html, raw_tree if strategy is _from_json_ld else tree)
        if facts is not None and facts.ok:
            return facts
    return None


# --- strategies, most trustworthy first --------------------------------


def _from_json_ld(html: str, tree: HTMLParser) -> ProductFacts | None:
    """schema.org Product in a <script type="application/ld+json"> block.

    A single-product page is the easy case. A release page is not: Bandcamp
    describes one MusicAlbum holding several Products - the download, the LP,
    a hat and two t-shirts - and the download is listed first. Taking the
    first Product quotes $8 for a $20 record, and taking the cheapest sells a
    t-shirt, so the variants have to be understood rather than skimmed.
    """
    for node in tree.css('script[type="application/ld+json"]'):
        data = _loads(node.text())
        if data is None:
            continue
        artist, album = _album_identity(data)
        products = [o for o in _walk(data) if "product" in str(o.get("@type", "")).lower()]
        for candidate in _ranked_products(products):
            offer = _pick_offer(candidate.get("offers"))
            price = offer.get("price") or offer.get("lowPrice")
            currency = offer.get("priceCurrency")
            name = _text(candidate.get("name"))
            if not (name and price and currency):
                continue
            return ProductFacts(
                title=_full_title(artist, album, name, candidate, offer),
                price_text=f"{price} {currency}",
                availability_text=_availability_words(offer.get("availability")),
                image_url=_first_image(candidate.get("image") or data.get("image")),
                source="json-ld",
                artist=artist,
            )
    return None


def _from_microdata(html: str, tree: HTMLParser) -> ProductFacts | None:
    """itemprop="price" / "priceCurrency" attributes on the page body."""
    price = _itemprop(tree, "price")
    currency = _itemprop(tree, "priceCurrency")
    if not (price and currency):
        return None
    title = _page_title(tree)
    if not title:
        return None
    artist = _page_artist(tree, html)
    return ProductFacts(
        title=_with_artist(artist, title),
        price_text=f"{price} {currency}",
        availability_text=_availability_words(_itemprop(tree, "availability")),
        image_url=_meta(tree, "og:image"),
        source="microdata",
        artist=artist,
    )


def _from_opengraph(html: str, tree: HTMLParser) -> ProductFacts | None:
    """OpenGraph product tags, as used by most Shopify and WooCommerce themes."""
    price = _meta(tree, "product:price:amount") or _meta(tree, "og:price:amount")
    currency = _meta(tree, "product:price:currency") or _meta(tree, "og:price:currency")
    if not (price and currency):
        return None
    title = _meta(tree, "og:title") or _page_title(tree)
    if not title:
        return None
    artist = _page_artist(tree, html)
    return ProductFacts(
        title=_with_artist(artist, title),
        price_text=f"{price} {currency}",
        availability_text=_meta(tree, "product:availability") or "",
        image_url=_meta(tree, "og:image"),
        source="opengraph",
        artist=artist,
    )


def _from_embedded_json(html: str, tree: HTMLParser) -> ProductFacts | None:
    """A price inside the shop's own bootstrap JSON.

    Only trusted when the same blob names a currency: a bare `"price": 2024`
    is as likely to be a catalogue year as an amount.
    """
    currency = _CURRENCY_ATTR.search(html)
    if not currency:
        return None
    prices = [m.group(1) for m in _JSON_PRICE.finditer(html)]
    if not prices:
        return None
    title = _page_title(tree)
    if not title:
        return None
    # The lowest is the selling price; the others are list price, RRP, or the
    # same product in another variant.
    cheapest = min(prices, key=lambda p: float(p.replace(",", ".")))
    artist = _page_artist(tree, html)
    return ProductFacts(
        title=_with_artist(artist, title),
        price_text=f"{cheapest} {currency.group(1)}",
        availability_text=_availability_words(_itemprop(tree, "availability")),
        image_url=_meta(tree, "og:image"),
        source="embedded-json",
        artist=artist,
    )


def _from_visible_text(html: str, tree: HTMLParser) -> ProductFacts | None:
    """Last resort: a visible price with an explicit currency sign.

    Requiring the sign is what keeps a footer's "2024" from being read as a
    price - the single most common false positive in this codebase. Only the
    first few price nodes are considered, because a shop's "you may also like"
    rail sits further down the document and its prices are not this product's.
    """
    title = _page_title(tree)
    if not title:
        return None
    texts = _price_texts(tree, title)
    if not texts:
        return None
    # Among the product's own price nodes the lowest is the selling price and
    # the rest are the struck-through list price - the same reading
    # `parse_money` already applies within a single block.
    best = min(texts, key=lambda t: _amount(t))
    artist = _page_artist(tree, html)
    return ProductFacts(
        title=_with_artist(artist, title),
        price_text=best,
        availability_text=_stock_text(tree),
        image_url=_meta(tree, "og:image"),
        source="visible-text",
        artist=artist,
    )


# --- helpers -----------------------------------------------------------


# how many price-looking nodes to trust before assuming we have wandered into
# a related-products rail.
_PRICE_NODE_LIMIT = 6
# A real price node holds a price and little else; a sentence mentioning a
# price is prose, usually about delivery thresholds.
_MAX_PRICE_NODE_CHARS = 40
_SHIPPING_WORDS = ("shipping", "delivery", "postage", "wysyłk", "dostaw", "przesyłk")
# Polish shops routinely print the ex-VAT price beside the real one. "Cena
# netto: 69,11 zł" is not a price anyone pays - taking it undercuts the true
# 85,00 zł by the VAT rate and hands the shop a "best price" it never offered.
_NET_PRICE_WORDS = ("netto", "net price", "excl. vat", "ex vat", "bez vat", "zzgl.")
# How close a net-price label has to sit to an amount to be about that amount,
# and how far up the tree to look for it.
_NET_LABEL_WINDOW = 14
_NET_LABEL_DEPTH = 4


def _price_texts(tree: HTMLParser, title: str = "") -> list[str]:
    """Prices that plausibly belong to *this* product.

    Document order is not enough. A shop's "customers also bought" rail often
    renders before the product's own block, so the first price on the page can
    belong to a different record entirely - VinylMusic leads with U2 and George
    Harrison before quoting the album you asked for. So we anchor on the node
    holding the title and climb until an ancestor contains a price, which is
    the smallest region guaranteed to be about one product.
    """
    anchor = _title_node(tree, title)
    while anchor is not None:
        found = [_own_text(n) for n in _price_nodes(anchor) if _usable_price(n)]
        found = [t for t in found if _is_price_node(t)]
        if found:
            return found[:_PRICE_NODE_LIMIT]
        anchor = anchor.parent
    # No usable title anchor (some shops put only the shop name in <h1>).
    # Fall back to document order over the whole page.
    loose = [_own_text(n) for n in _price_nodes(tree.body or tree.root) if _usable_price(n)]
    return [t for t in loose if _is_price_node(t)][:_PRICE_NODE_LIMIT]


def _price_nodes(scope):
    """Price-classed nodes inside `scope`, then unclassed short prices."""
    if scope is None:
        return []
    classed = scope.css("[class*=price], [id*=price], .amount, .woocommerce-Price-amount")
    if classed:
        return classed
    return scope.css("span, b, strong, div, td, p, h2, h3")


def _title_node(tree: HTMLParser, title: str = ""):
    """The element that names the product, to scope the price hunt around.

    Not every shop has an <h1>: VinylMusic's product page has none at all, and
    its title only exists in <title> and og:title. So when there is no heading
    to grab, find the smallest element that actually renders the title text.
    """
    for selector in ('[itemprop="name"]', "h1"):
        node = tree.css_first(selector)
        if node is not None and _own_text(node):
            return node
    needle = _fold(title)
    if not needle:
        return None
    best = None
    for node in tree.css("h2, h3, div, span, td, p, a"):
        text = _fold(_own_text(node))
        if needle in text and (best is None or len(text) < len(_fold(_own_text(best)))):
            best = node
    return best


def _usable_price(node) -> bool:
    """Reject an amount that its own label marks as the ex-VAT price.

    The label is a sibling, so the amount's own text is clean - only the
    surroundings give it away. But a block often reads "Cena netto: 69,11 zł
    85,00 zł", where a plain "is 'netto' anywhere above?" test would throw
    away the real price too. What actually distinguishes them is adjacency, so
    the label only counts when it immediately precedes this amount.
    """
    text = _own_text(node)
    ancestor, depth = node.parent, 0
    while ancestor is not None and depth < _NET_LABEL_DEPTH:
        blob = _own_text(ancestor)
        if len(blob) > 200:
            break
        index = blob.find(text)
        if index > 0:
            lead = blob[max(0, index - _NET_LABEL_WINDOW) : index].lower()
            if any(word in lead for word in _NET_PRICE_WORDS):
                return False
        ancestor, depth = ancestor.parent, depth + 1
    return True


def _is_price_node(text: str) -> bool:
    """Is this short text the price a buyer actually pays?"""
    if not text or len(text) > _MAX_PRICE_NODE_CHARS or not looks_like_price(text):
        return False
    lowered = text.lower()
    return not any(word in lowered for word in _SHIPPING_WORDS + _NET_PRICE_WORDS)


def _own_text(node) -> str:
    return " ".join(node.text(separator=" ", strip=True).split())


def _amount(text: str):
    from .pricing import parse_money

    money = parse_money(text)
    return money.amount if money else Decimal("999999")


def _stock_text(tree: HTMLParser) -> str:
    for node in tree.css("[class*=stock], [class*=avail], [id*=stock]"):
        text = _own_text(node)
        if text:
            return text[:80]
    return ""


def _loads(text: str | None):
    try:
        return json.loads((text or "").strip())
    except (ValueError, TypeError):
        return None


def _walk(obj):
    """Yield every dict inside a JSON-LD payload, including @graph members."""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            if isinstance(value, (dict, list)):
                yield from _walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


# Carrier words that mark a variant as a physical record rather than a file.
_PHYSICAL_WORDS = ("vinyl", "lp", "cd", "cassette", "winyl", '12"', '10"', '7"')
# schema.org's musicReleaseFormat values, reduced to a word the matcher reads.
_RELEASE_FORMATS = {
    "vinylformat": "Vinyl",
    "cdformat": "CD",
    "digitalformat": "Digital Album",
    "cassetteformat": "Cassette",
    "dvdformat": "DVD",
}


def _album_identity(data) -> tuple[str, str]:
    """The performer and album name, which live on the release's parent."""
    for obj in _walk(data):
        by_artist = obj.get("byArtist")
        if not by_artist:
            continue
        artist = _text(by_artist if isinstance(by_artist, (dict, str)) else "")
        if artist:
            return artist, _text(obj.get("name"))
    return "", ""


def _ranked_products(products: list[dict]) -> list[dict]:
    """Physical releases first, digital last, so the record wins the page.

    Order only - nothing is discarded here, because a page that genuinely
    sells only a download should still be read and then rejected by the
    matcher, with a reason, rather than vanishing as "no price found".
    """

    def rank(product: dict) -> int:
        fmt = _release_format(product)
        if fmt in ("Vinyl", "CD"):
            return 0
        if _names_a_carrier(product):
            return 1
        return 3 if fmt == "Digital Album" else 2

    return sorted(products, key=rank)


def _release_format(product: dict) -> str:
    raw = str(product.get("musicReleaseFormat") or "").lower()
    for key, word in _RELEASE_FORMATS.items():
        if key in raw:
            return word
    return ""


def _pick_offer(offers) -> dict:
    """The offer attached to one variant; a list means pick the first real one."""
    if isinstance(offers, dict):
        inner = offers.get("offers")
        if isinstance(inner, list) and inner:
            return next((o for o in inner if isinstance(o, dict)), {})
        return offers
    if isinstance(offers, list):
        return next((o for o in offers if isinstance(o, dict)), {})
    return {}


def _names_a_carrier(product: dict) -> bool:
    blob = " ".join(str(product.get(k, "")) for k in ("name", "sku", "description")).lower()
    return any(word in blob for word in _PHYSICAL_WORDS)


def _full_title(artist: str, album: str, name: str, product: dict, offer: dict) -> str:
    """Everything the matcher needs to judge this variant, in one string.

    schema.org keeps the performer in `byArtist` and the carrier in
    `musicReleaseFormat`, so a bare name of "LP (Black Vinyl)" names neither
    the artist nor the album and matches nothing.
    """
    parts: list[str] = []
    if artist:
        parts.append(artist)
    for piece in (album, name, _release_format(product), _text(offer.get("name"))):
        if piece and piece.lower() not in " ".join(parts).lower():
            parts.append(piece)
    return " - ".join(parts[:2]) + (" " + " ".join(parts[2:]) if len(parts) > 2 else "")


def _text(value) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        return _text(value.get("@value") or value.get("name") or "")
    return ""


def _first_image(value) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        return _first_image(value[0])
    if isinstance(value, dict):
        return _first_image(value.get("url") or value.get("contentUrl"))
    return None


def _availability_words(value) -> str:
    """Turn "http://schema.org/InStock" into "In Stock"."""
    if not isinstance(value, str):
        return ""
    match = _SCHEMA_AVAILABILITY.search(value)
    word = match.group(1) if match else value
    return _CAMEL.sub(" ", word)


def _meta(tree: HTMLParser, name: str) -> str | None:
    for node in tree.css(f'meta[property="{name}"], meta[name="{name}"]'):
        content = (node.attributes.get("content") or "").strip()
        if content:
            return content
    return None


def _itemprop(tree: HTMLParser, name: str) -> str | None:
    for node in tree.css(f'[itemprop="{name}"]'):
        value = (node.attributes.get("content") or node.attributes.get("href") or "").strip()
        if not value:
            value = " ".join(node.text(separator=" ", strip=True).split())
        if value:
            return value
    return None


_SHOPIFY_VENDOR = re.compile(r'"vendor"\s*:\s*"([^"]{1,60})"')


def _page_artist(tree: HTMLParser, html: str) -> str:
    """The performer, when the page keeps it out of the product name.

    Shops routinely name the product "Akhenaten - Vinyl LP" and file the
    artist separately, which leaves a matcher judging the title alone with no
    artist to find - so a shop that really is selling the record is rejected
    as "different artist". Mr Bongo was live proof: in stock at $34.99,
    thrown away, while the page said `itemprop="brand"` = "Nat Birchall".

    Ordered by how specifically each field means *performer*. The shop's own
    <h1> is deliberately absent: it is the artist on some shops and the shop's
    own name on others, and there is no way to tell which from the markup.
    """
    for value in (
        _itemprop(tree, "byArtist"),
        _meta(tree, "music:musician"),
        _itemprop(tree, "brand"),
        _itemprop(tree, "author"),
        _meta(tree, "product:brand"),
        _meta(tree, "og:brand"),
    ):
        text = " ".join((value or "").split())
        if text:
            return text
    # Shopify keeps the performer in `vendor`, and its themes rarely surface
    # that anywhere a selector can reach.
    match = _SHOPIFY_VENDOR.search(html)
    return " ".join(match.group(1).split()) if match else ""


def _with_artist(artist: str, title: str) -> str:
    """Put the performer back in front of a product name that omits it.

    schema.org's `byArtist` was already folded in this way for JSON-LD
    release pages; every other strategy needs it just as much. Adding the
    artist can only ever help a comparison - the strict title gate still has
    to pass separately - so a page that names its brand rather than its
    performer costs nothing.
    """
    if not artist or not title or _fold(artist) in _fold(title):
        return title
    return f"{artist} - {title}"


def _page_title(tree: HTMLParser) -> str:
    """The name of the thing being sold, not the name of the shop.

    Plenty of small webshops put their own name in the only <h1> on the page
    and leave the product name to <title>. Taking the h1 on faith there yields
    a "product" called "Exploding In Sound Records", which no query can match.
    """
    site = _fold(_meta(tree, "og:site_name") or "")
    candidates = [
        _meta(tree, "og:title"),
        _itemprop(tree, "name"),
        _node_text(tree, "h1"),
        _node_text(tree, "title"),
    ]
    document_title = candidates[-1] or ""
    for candidate in candidates:
        if not candidate:
            continue
        folded = _fold(candidate)
        # A heading that is just the shop's name is furniture, not a product.
        if site and folded and (folded in site or site in folded) and len(folded) <= len(site) + 4:
            continue
        if folded and document_title and folded != _fold(document_title):
            return candidate
        if not site and candidate is document_title:
            return candidate
        if folded:
            return candidate
    return document_title


def _node_text(tree: HTMLParser, selector: str) -> str:
    node = tree.css_first(selector)
    if not node:
        return ""
    return " ".join(node.text(separator=" ", strip=True).split())


def _fold(text: str) -> str:
    return " ".join(text.lower().split())
