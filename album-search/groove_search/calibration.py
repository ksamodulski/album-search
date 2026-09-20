"""Learn how to search a shop, without being told and without an API key.

Calibration is the "learning" step the user can re-trigger whenever a shop
redesigns and its recipe stops working. It runs in two stages:

1. *Find the search endpoint* - read the shop's own search form, falling back
   to the URL shapes used by the common e-commerce platforms.
2. *Infer the result structure* - fetch a probe search, find every node whose
   own text is a price, walk up to the repeating ancestor that wraps price +
   link + title, and turn that repetition into CSS selectors.

The output is a plain `SearchRecipe`: once calibration has run, searching is
fully deterministic and never re-infers anything.
"""

from __future__ import annotations

import re
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from selectolax.parser import HTMLParser, Node

from .domain import Shop
from .fetching import Fetcher
from .pricing import looks_like_price, parse_money
from .recipes import QUERY_TOKEN, SearchRecipe, extract_offers, run_recipe
from .text import fold

# Probe albums a well-stocked record shop is very likely to carry. Calibration
# needs results on the page, so these are deliberately mainstream.
DEFAULT_PROBES: tuple[str, ...] = (
    "radiohead ok computer",
    "nas illmatic",              # a hip-hop shop stocks none of the rock probes
    "metallica master of puppets",
    "daft punk discovery",
    "miles davis kind of blue",
)

# Input names shops use for their search box.
_SEARCH_INPUT_NAMES = (
    "q", "s", "query", "search", "searchquery", "text", "szukaj", "keyword",
    "keywords", "term", "name", "search_query", "wyszukiwarka",
)

# Fallback templates, by platform, tried in order when no form is found.
_TEMPLATE_GUESSES = (
    "/?s={query}",                                        # WordPress / WooCommerce
    "/search?q={query}",                                  # Shopify and friends
    "/szukaj?q={query}",
    "/search?query={query}",
    "/catalogsearch/result/?q={query}",                   # Magento
    "/index.php?route=product/search&search={query}",     # OpenCart
    "/search.php?text={query}",                           # IdoSell
    "/pl/search?controller=search&s={query}",             # PrestaShop
    "/szukaj?szukaj={query}",
    "/products?q={query}",
    "/search?text={query}",                               # Shoper
    "/pl/search?text={query}",
    "/szukaj?text={query}",
    "/szukaj.html?szukaj={query}",
    "/?post_type=product&s={query}",                      # WooCommerce, products only
    "/sklep?s={query}",
    "/shop?q={query}",
    "/search/{query}",
    "/szukaj/{query}",
    "/pl/szukaj?q={query}",
    # Path-style search used by IdoSell, the most common Polish shop engine.
    "/pl/searchquery/{query}",
    "/searchquery/{query}",
    "/pl/menu/searchquery/{query}",
)

# Class names that change between deploys and must not enter a selector.
_VOLATILE_CLASS = re.compile(r"(^|[-_])(css|sc|jsx|styled|ng|v-|elementor-element)|\d{3,}|[0-9a-f]{6,}")

# A query no shop can satisfy. An endpoint that answers it with the same
# products it returns for a real search is ignoring the query entirely.
NONSENSE_PROBE = "qzvxlm nonexistent record 9981"

_MIN_BLOCKS = 3
_MAX_BLOCKS = 80
_MAX_CLIMB = 7


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    """A recipe, or a readable account of why one could not be produced."""

    shop_id: str
    recipe: SearchRecipe | None = None
    error: str | None = None
    log: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.recipe is not None


async def calibrate(
    shop: Shop,
    fetcher: Fetcher,
    probes: tuple[str, ...] = DEFAULT_PROBES,
    *,
    min_offers: int = 2,
    max_templates: int | None = None,
) -> CalibrationResult:
    """Work out how to search `shop`, and prove it works before returning.

    The returned recipe has already produced at least `min_offers` plausible
    offers for at least one probe, so a successful result is a working one.
    """
    log: list[str] = []
    templates = await _find_templates(shop, fetcher, log)
    if max_templates is not None:
        # Guessing endpoints in a real browser is slow; spend the budget on
        # the shop's own form and the likeliest platform defaults.
        templates = templates[:max_templates]
    if not templates:
        return CalibrationResult(shop.id, error="no search endpoint found", log=tuple(log))

    best: tuple[int, SearchRecipe] | None = None
    for template in templates:
        for probe in probes:
            page = await fetcher.get(_fill(template, probe))
            if not page.ok:
                log.append(f"{template}: fetch failed ({page.error})")
                break  # a dead endpoint stays dead for the other probes
            candidate = _infer_recipe(shop, template, page.html, page.url, probe, log)
            if candidate is None:
                log.append(f"{template} + probe '{probe}': no repeating product blocks")
                continue
            offers = extract_offers(candidate, page.html, page.url)
            hits = len(offers)
            if not _mentions_probe(offers, probe):
                log.append(f"{template} + probe '{probe}': {hits} blocks, none mentioning the probe")
                continue
            if await _ignores_query(candidate, offers, fetcher):
                log.append(f"{template}: endpoint ignores the query (same results for nonsense)")
                break
            log.append(f"{template} + probe '{probe}': {hits} offers via {candidate.item_selector}")
            if hits >= min_offers and (best is None or hits > best[0]):
                best = (hits, candidate)
                break
        if best:
            break

    if not best:
        return CalibrationResult(shop.id, error="no product blocks recognised", log=tuple(log))

    hits, recipe = best
    now = datetime.now(UTC).isoformat(timespec="seconds")
    return CalibrationResult(
        shop.id,
        recipe=recipe.stamped(calibrated_at=now, probe_hits=hits),
        log=tuple(log),
    )


async def _find_templates(shop: Shop, fetcher: Fetcher, log: list[str]) -> list[str]:
    """Search URL templates to try, the shop's own form first."""
    templates: list[str] = []
    if shop.search_hint:
        hint = shop.search_hint
        templates.append(hint if hint.startswith("http") else shop.base_url.rstrip("/") + hint)
        log.append(f"using configured search hint: {templates[0]}")
    home = await fetcher.get(shop.base_url)
    if home.ok:
        from_form = _templates_from_forms(home.html, home.url)
        templates.extend(from_form)
        if from_form:
            log.append(f"search form found: {from_form[0]}")
    else:
        log.append(f"homepage unreachable: {home.error}")
    base = shop.base_url.rstrip("/")
    templates.extend(base + guess for guess in _TEMPLATE_GUESSES)
    seen: set[str] = set()
    return [t for t in templates if not (t in seen or seen.add(t))]


def _templates_from_forms(html: str, base_url: str) -> list[str]:
    """Turn every plausible search form on the page into a URL template."""
    tree = HTMLParser(html)
    templates: list[str] = []
    for form in tree.css("form"):
        # POST search forms are the norm in several Polish shop engines, and
        # their endpoints almost always answer a GET too - worth a try.
        field_name, hidden = None, []
        for node in form.css("input"):
            name = node.attributes.get("name")
            if not name:
                continue
            input_type = (node.attributes.get("type") or "text").lower()
            if input_type == "hidden" or "hidden" in node.attributes:
                value = node.attributes.get("value") or ""
                hidden.append((name, value))
            elif input_type in ("search", "text") and (
                name.lower() in _SEARCH_INPUT_NAMES or _looks_like_search(node)
            ):
                field_name = name
        if not field_name:
            continue
        action = urllib.parse.urljoin(base_url, form.attributes.get("action") or base_url)
        parts = urllib.parse.urlsplit(action)
        params = urllib.parse.parse_qsl(parts.query)
        params.extend(h for h in hidden if h[0] != field_name)
        query = urllib.parse.urlencode(params)
        query = f"{query}&{field_name}={QUERY_TOKEN}" if query else f"{field_name}={QUERY_TOKEN}"
        templates.append(urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, "")))
    return templates


def _looks_like_search(node: Node) -> bool:
    blob = fold(
        " ".join(
            filter(None, (node.attributes.get("placeholder"), node.attributes.get("id"),
                          node.attributes.get("class"), node.attributes.get("aria-label")))
        )
    )
    return any(word in blob for word in ("search", "szukaj", "wyszuk", "find"))


def _infer_recipe(
    shop: Shop, template: str, html: str, page_url: str, probe: str, log: list[str]
) -> SearchRecipe | None:
    """Derive selectors from the repeating structure around price nodes."""
    tree = HTMLParser(html)
    price_nodes = [n for n in tree.css("*") if _own_text_is_price(n)]
    if not price_nodes:
        return None

    # Count how often each ancestor signature wraps a price node.
    signature_counts: Counter[str] = Counter()
    signature_depth: dict[str, int] = {}
    for node in price_nodes:
        ancestor, climbed = node, 0
        while ancestor is not None and climbed <= _MAX_CLIMB:
            signature = _signature(ancestor)
            if signature:
                signature_counts[signature] += 1
                signature_depth.setdefault(signature, climbed)
            ancestor = ancestor.parent
            climbed += 1

    best: tuple[float, SearchRecipe] | None = None
    for signature, count in signature_counts.most_common(40):
        if not _MIN_BLOCKS <= count <= _MAX_BLOCKS:
            continue
        blocks = tree.css(signature)
        if not _MIN_BLOCKS <= len(blocks) <= _MAX_BLOCKS:
            continue
        recipe = _recipe_for(shop, template, signature, blocks)
        if recipe is None:
            continue
        offers = extract_offers(recipe, html, page_url)
        score = _score(offers, probe, signature_depth.get(signature, 0))
        if score > 0 and (best is None or score > best[0]):
            best = (score, recipe)

    if best is None:
        return None
    return best[1]


def _recipe_for(shop: Shop, template: str, item_selector: str, blocks: list[Node]) -> SearchRecipe | None:
    """Pick title / price / link selectors shared by most blocks."""
    price_selector = _shared_selector(blocks, _own_text_is_price)
    title_selector = _shared_title_selector(blocks)
    link_selector = _shared_selector(
        blocks, lambda n: n.tag == "a" and bool(n.attributes.get("href")) and bool(n.text().strip())
    )
    if price_selector is None:
        return None
    return SearchRecipe(
        shop_id=shop.id,
        search_url=template,
        item_selector=item_selector,
        title_selector=title_selector,
        price_selector=price_selector,
        link_selector=link_selector,
        needs_browser=shop.needs_browser,
    )


def _nearest_signature(node: Node, block: Node) -> str | None:
    """Signature of `node`, or of its closest classed ancestor inside `block`.

    Shops routinely wrap a bare `<span>45,00 zl</span>` in `div.price`; the
    wrapper is the only addressable thing, and its text is the price anyway.
    """
    current, climbed = node, 0
    while current is not None and climbed <= 3:
        found = _signature(current)
        if found:
            return found
        if current is block:
            return None
        current = current.parent
        climbed += 1
    return None


def _shared_selector(blocks: list[Node], predicate) -> str | None:
    """The most common descendant signature satisfying `predicate`."""
    counts: Counter[str] = Counter()
    for block in blocks:
        signatures = {
            _nearest_signature(node, block)
            for node in block.css("*")
            if predicate(node)
        }
        counts.update(s for s in signatures if s)
    if not counts:
        return None
    signature, count = counts.most_common(1)[0]
    return signature if count >= max(2, len(blocks) // 2) else None


_TITLE_TAGS = ("h1", "h2", "h3", "h4", "h5")
_TITLE_CLASS = re.compile(r"title|name|nazwa", re.IGNORECASE)


def _is_title_node(node: Node) -> bool:
    """A heading or an explicitly title-classed element holding real text."""
    classes = node.attributes.get("class") or ""
    if node.tag not in _TITLE_TAGS and not _TITLE_CLASS.search(classes):
        return False
    text = " ".join(node.text().split())
    return 3 <= len(text) <= 200 and not looks_like_price(text)


def _shared_title_selector(blocks: list[Node]) -> str | None:
    """Prefer a heading; fall back to the link that reads like a product name."""
    heading = _shared_selector(blocks, _is_title_node)
    if heading:
        return heading
    counts: Counter[str] = Counter()
    lengths: defaultdict[str, list[int]] = defaultdict(list)
    for block in blocks:
        for node in block.css("a[href]"):
            text = " ".join(node.text().split())
            if not (6 <= len(text) <= 200) or looks_like_price(text):
                continue
            signature = _nearest_signature(node, block)
            if signature:
                counts[signature] += 1
                lengths[signature].append(len(text))
    if not counts:
        return None
    threshold = max(2, len(blocks) // 2)
    viable = [(s, c) for s, c in counts.items() if c >= threshold]
    if not viable:
        return None
    # Longest average text wins: product names beat "buy" and "details".
    return max(viable, key=lambda item: sum(lengths[item[0]]) / len(lengths[item[0]]))[0]


def _score(offers, probe: str, depth: int) -> float:
    """Prefer many offers, real prices, and blocks that mention the probe."""
    if len(offers) < _MIN_BLOCKS:
        return 0.0
    priced = [o for o in offers if parse_money(o.price_text)]
    if len(priced) < _MIN_BLOCKS:
        return 0.0
    probe_tokens = set(fold(probe).split())
    mentions = sum(
        1 for o in offers if probe_tokens & set(fold(o.title_text).split())
    )
    distinct_prices = len({o.price_text for o in priced})
    distinct_urls = len({o.url for o in offers})
    return (
        len(priced)
        + 3.0 * mentions
        + 2.0 * distinct_prices
        + 2.0 * distinct_urls
        - 0.5 * depth
    )


async def _ignores_query(recipe: SearchRecipe, offers, fetcher: Fetcher) -> bool:
    """True when a nonsense search returns substantially the same products.

    Catches shops whose "search" URL silently falls back to a catalogue page,
    which would otherwise calibrate cleanly and then match albums at random.
    """
    if not offers:
        return False
    control = await run_recipe(recipe, NONSENSE_PROBE, fetcher)
    if not control.ok or not control.offers:
        return False
    real_urls = {o.url for o in offers}
    control_urls = {o.url for o in control.offers}
    overlap = len(real_urls & control_urls) / max(1, len(real_urls | control_urls))
    return overlap > 0.5


def _mentions_probe(offers, probe: str) -> bool:
    """Did the page actually answer our query, or just show a generic grid?"""
    tokens = {t for t in fold(probe).split() if len(t) > 2}
    if not tokens:
        return True
    return any(tokens & set(fold(o.title_text).split()) for o in offers)


def _own_text_is_price(node: Node) -> bool:
    own = node.text(deep=False)
    return bool(own and own.strip()) and looks_like_price(own)


def _signature(node: Node) -> str | None:
    """A stable CSS selector for a node's shape: tag plus durable classes."""
    if node.tag in ("html", "body", "[document]", "-undef"):
        return None
    raw_classes = (node.attributes.get("class") or "").split()
    classes = [c for c in raw_classes if c and not _VOLATILE_CLASS.search(c)][:3]
    if not classes:
        item_prop = node.attributes.get("itemtype") or node.attributes.get("data-testid")
        if item_prop:
            attr = "itemtype" if node.attributes.get("itemtype") else "data-testid"
            return f'{node.tag}[{attr}="{item_prop}"]'
        return None
    return node.tag + "".join(f".{_escape(c)}" for c in classes)


def _escape(value: str) -> str:
    return re.sub(r"([^\w-])", r"\\\1", value)


def _fill(template: str, term: str) -> str:
    return template.replace(QUERY_TOKEN, urllib.parse.quote_plus(" ".join(term.split())))
