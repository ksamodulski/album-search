"""Find offers on shops nobody taught us about.

The recipe engine can only search shops that were seeded, calibrated and kept
working. That is a hard ceiling: a record pressed by a small US label is not in
any Polish generalist's catalogue, so the honest answer is "Not available" even
though four shops in the world are selling it right now.

This module lifts the ceiling by going the other way round. Instead of asking
every known shop about the album, it asks a search engine where the album is
sold, then opens each candidate page and reads it. Nothing is trusted on the
way in: a hit is only an offer once `product` finds a price with a currency and
`matching` agrees the page is really the record the user asked for - the same
gatekeeper the shop path uses, and deliberately so.

The offers this produces are mostly foreign, which is why they are costed with
`shipping` before they are allowed to compete: a listed price from another
continent is not comparable to a domestic one.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from .domain import (
    OPEN_WEB_SOURCE_ID,
    AlbumQuery,
    EngineReport,
    Format,
    Lead,
    Offer,
    RawOffer,
    ShopReport,
)
from .fetching import Fetcher
from .matching import read_availability, score_offer
from .pricing import parse_money
from .product import extract_product
from .shipping import ShippingRates, landed_cost
from .websearch import DEFAULT_LIMIT, SearchProvider, reports_of, usable

# How many candidate pages to actually open per album. Each is a live request
# to a stranger's server, so this is a politeness budget as much as a speed one.
MAX_PAGES = 8
# Pages fetched at once. Low: these are many different hosts, but a search
# result page often lists several URLs from the same shop.
CONCURRENCY = 4
# Candidates to take from any one shop. Engines happily fill a page with eight
# eBay listings; spending the whole budget there buys one shop's opinion
# instead of the market's.
MAX_PER_HOST = 2

SOURCE_ID = OPEN_WEB_SOURCE_ID
SOURCE_NAME = "web search"

# Country of a shop, guessed from its domain. Only the suffixes that change
# the postage zone are worth listing.
_TLD_COUNTRY: dict[str, str] = {
    "pl": "PL", "de": "DE", "uk": "GB", "fr": "FR", "nl": "NL", "cz": "CZ",
    "it": "IT", "es": "ES", "se": "SE", "dk": "DK", "be": "BE", "at": "AT",
    "ie": "IE", "fi": "FI", "pt": "PT", "gr": "GR", "hu": "HU", "ro": "RO",
    "sk": "SK", "si": "SI", "hr": "HR", "bg": "BG", "lt": "LT", "lv": "LV",
    "ee": "EE", "us": "US", "ca": "CA", "jp": "JP", "au": "AU", "nz": "NZ",
}
# Failing a country-coded domain, the currency hints at which customs regime
# the parcel crosses. Note what is *not* here: the destination's own currency.
# Plenty of foreign shops localise prices, and reading "PLN" as "ships from
# Poland" hands a shop in Los Angeles free domestic delivery and no import
# VAT - the precise error the shipping model exists to prevent. Guessing a
# currency can only ever make an offer dearer, never cheaper.
_CURRENCY_COUNTRY: dict[str, str] = {"GBP": "GB", "USD": "US", "EUR": "EU", "CZK": "CZ"}

# Hosts whose country no suffix can give. Amazon is the case that matters: it
# quotes a US record in PLN to a Polish visitor, so neither the .com nor the
# currency says where the parcel starts. The arithmetic does not change - an
# unknown origin is already costed as the world zone plus import VAT, which is
# exactly what a US parcel costs - but the reader is shown "from US" instead
# of nothing, and a fact beats a blank.
#
# Only retailers that really do ship from one country belong here. eBay,
# Discogs and Bandcamp are deliberately absent: their sellers are scattered
# across the world, so a country here would be an invention displayed to the
# user as a fact, and "unknown, costed as worst case" is the honest answer.
_HOST_COUNTRY: dict[str, str] = {"amazon.com": "US"}

# Paths that list many records rather than sell one. A Shopify product lives
# at /collections/<x>/products/<y>, so a product segment overrides this.
_TAXONOMY_PATH = re.compile(
    r"/(collections?|artists?|labels?|brands?|category|categories|kategori[ae]|"
    r"producent|search|szukaj|listing|oferty|tag|tags|genre|gatunek|series)(/|$)",
    re.IGNORECASE,
)
_PRODUCT_PATH = re.compile(
    r"/(products?|produkt|item|itm|release|album|dp|gp/product|p)(/|$)", re.IGNORECASE
)
# A shop's own search results, which engines index freely. Amazon answers
# /whitest-boy-alive/s?k=... - a path test alone never catches that, but the
# query string always gives it away.
_SEARCH_PARAMS = frozenset(
    {
        "k", "q", "s", "query", "search", "keyword", "keywords", "text", "string",
        "phrase", "phrases",
        # The marketplaces spell it their own way: Amazon's refinement
        # parameter and eBay's keyword one both mark a results grid that no
        # path test catches. Both were live false positives - a "price" read
        # off such a page belongs to whichever record happened to be first.
        "rh", "_nkw", "field-keywords",
    }
)
# Marketplace result grids whose *path* is the giveaway. Amazon's /s and
# eBay's /sch and /b are search and browse pages; Amazon's /clp is a curated
# list. None of them sell the one record, and all of them carry prices.
_GRID_PATH = re.compile(r"/(s|sch|b|clp|bn_\w+|shop)(/|$)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class WebFinding:
    """One candidate that was opened and judged, kept for diagnostics.

    Rejections are as interesting as acceptances here: when the open web
    returns nothing, the user deserves to know whether the pages were blocked,
    unreadable or simply the wrong record.
    """

    url: str
    verdict: str
    offer: Offer | None = None
    engine: str = ""
    # Set when the page was refused but the engine's own title says this is
    # the record: the seller is worth naming even without a price.
    lead: Lead | None = None


async def find_offers(
    query: AlbumQuery,
    provider: SearchProvider,
    fetcher: Fetcher,
    *,
    location: str = "PL",
    currency: str = "PLN",
    rates: dict | None = None,
    shipping: ShippingRates | None = None,
    max_pages: int = MAX_PAGES,
    known_hosts: frozenset[str] = frozenset(),
) -> tuple[list[Offer], ShopReport, list[WebFinding]]:
    """Search the open web for one album and return the offers that survive.

    `known_hosts` are shops the recipe engine already covers; they are skipped
    so the same shop is not searched twice and cannot appear twice in a result.
    """
    phrases = _phrases(query)
    hits: list = []
    engines: list = []
    for phrase in phrases:
        hits.extend(await provider.search(phrase, limit=DEFAULT_LIMIT))
        engines = _merge_engine_reports(engines, reports_of(provider))
    candidates = _spread(
        [h for h in usable(hits) if h.host not in known_hosts and not _is_listing(h.url)],
        max_pages,
        home=location,
    )
    if not candidates:
        # An engine that refused us must not be reported as an album nobody
        # sells - those are opposite facts and only one of them is the user's
        # problem to act on.
        refusal = getattr(provider, "last_error", None)
        report = ShopReport(SOURCE_ID, SOURCE_NAME, offers_found=0, error=refusal, engines=tuple(engines))
        return [], report, []

    gate = asyncio.Semaphore(CONCURRENCY)

    async def visit(hit) -> WebFinding:
        async with gate:
            return await _judge(hit, query, fetcher, location, currency, rates or {}, shipping)

    findings = list(await asyncio.gather(*(visit(h) for h in candidates)))
    offers = [f.offer for f in findings if f.offer is not None]
    error = None if offers else _why_nothing(findings)
    report = ShopReport(
        SOURCE_ID, SOURCE_NAME, offers_found=len(offers), error=error, engines=tuple(engines)
    )
    return offers, report, findings


def _merge_engine_reports(existing: list, latest) -> list:
    """Keep a running per-engine tally across the two phrases we search.

    Each phrase is a fresh call, so an engine's own report describes only the
    last one. Summing them is what lets the result say "DuckDuckGo: 24 hits"
    for the work actually done, and keeps a throttle that struck on the second
    phrase from erasing the first phrase's hits.
    """
    tallies = {r.name: r for r in existing}
    for report in latest:
        prior = tallies.get(report.name)
        if prior is None:
            tallies[report.name] = report
            continue
        tallies[report.name] = EngineReport(
            report.name,
            prior.hits + report.hits,
            # A refusal is worth reporting even if the other phrase worked;
            # hits above say plainly that it was not a total outage.
            report.note or prior.note,
        )
    return list(tallies.values())


async def _judge(hit, query, fetcher, location, currency, rates, shipping) -> WebFinding:
    """Open one candidate page and decide whether it is really this record."""
    page = await fetcher.get(hit.url)
    if not page.ok:
        reason = page.error or f"HTTP {page.status}"
        return WebFinding(hit.url, reason, engine=hit.engine, lead=_lead(hit, query, reason))

    facts = extract_product(page.html, url=hit.url)
    if facts is None:
        return WebFinding(hit.url, "no price found on the page", engine=hit.engine)

    raw = RawOffer(
        shop_id=hit.host,
        title_text=facts.title,
        price_text=facts.price_text,
        url=hit.url,
        availability_text=facts.availability_text,
        image_url=facts.image_url,
    )
    verdict = score_offer(query, raw)
    if not verdict.matched:
        return WebFinding(hit.url, verdict.reason, engine=hit.engine)

    price = parse_money(facts.price_text, default_currency="")
    if price is None or not price.currency:
        # A price with no currency is unusable across borders and we have no
        # shop record to borrow a default from.
        return WebFinding(hit.url, "price without a currency", engine=hit.engine)

    country = _country_of(hit.host, price.currency)
    cost = landed_cost(
        price,
        origin=country,
        destination=location,
        fmt=verdict.format,
        rates=shipping,
        fx=rates,
        target_currency=currency,
    )
    offer = Offer(
        shop_id=hit.host,
        shop_name=hit.host,
        title=facts.title,
        price=price,
        url=hit.url,
        format=verdict.format,
        availability=read_availability(facts.availability_text, facts.title),
        confidence=verdict.confidence,
        image_url=facts.image_url,
        country=country,
        landed=cost,
        from_open_web=True,
        found_via=hit.engine,
    )
    return WebFinding(hit.url, f"matched ({facts.source})", offer, engine=hit.engine)


def _lead(hit, query: AlbumQuery, reason: str) -> Lead | None:
    """Name a seller we were refused by, but only if this really is the record.

    The only evidence available is the engine's own title for the page, so the
    bar is the same strict matcher an offer has to pass - a blocked page that
    merely mentions the artist stays unmentioned. The search-result title also
    has to look like a product rather than a listing grid ("Radiohead In
    Rainbows - Niska cena na Allegro" is an advert for a search page).
    """
    if not _is_blocked(reason):
        return None
    verdict = score_offer(query, RawOffer(shop_id=hit.host, title_text=hit.title, price_text="", url=hit.url))
    if not verdict.matched:
        return None
    return Lead(host=hit.host, url=hit.url, title=hit.title, reason=reason, format=verdict.format)


# What "they would not let us read it" looks like coming back from a fetcher.
# A 404 is not here on purpose: that is a page that does not exist, not a
# seller hiding a price, and there is nothing to send the user to.
_BLOCKED = ("403", "401", "429", "blocked by robots.txt", "browser unavailable")


def _is_blocked(reason: str) -> bool:
    lowered = reason.lower()
    return any(marker in lowered for marker in _BLOCKED)


def _spread(hits: list, max_pages: int, *, home: str = "") -> list:
    """Take the best candidates while keeping the field broad.

    Order is mostly preserved - the engine's ranking is still respected - but
    no single shop may supply more than `MAX_PER_HOST` of the pages we open,
    and a shop in the buyer's own country goes first.

    Domestic first is not a patriotic preference, it is the ranking rule
    working backwards. Postage and import VAT add ~100 PLN to a parcel from
    outside the EU, so a domestic listing wins the delivered-cost comparison
    at prices a foreign one cannot touch. The fetch budget is eight pages;
    spending it on eight foreign candidates while a Polish marketplace sits
    at rank nine buys pages that were never going to win.
    """
    if home:
        hits = sorted(hits, key=lambda h: 0 if _country_of(h.host, "") == home.upper() else 1)
    per_host: dict[str, int] = {}
    kept = []
    for hit in hits:
        seen = per_host.get(hit.host, 0)
        if seen >= MAX_PER_HOST:
            continue
        per_host[hit.host] = seen + 1
        kept.append(hit)
        if len(kept) >= max_pages:
            break
    return kept


def _phrases(query: AlbumQuery) -> list[str]:
    """What to type into the search engine.

    Two phrasings, not one: "buy vinyl" pulls shops to the top, while the bare
    artist and title catches shops whose page titles are terse. An artist-only
    line gets only the general phrasing, since there is no album to pin.
    """
    label = query.label
    if query.is_artist_only:
        return [f"{label} vinyl LP buy"]
    carrier = {Format.VINYL: "vinyl LP", Format.CD: "CD"}.get(query.format, "vinyl OR CD")
    return [f'"{label}" {carrier} buy', f"{label} album buy online"]


def _is_listing(url: str) -> bool:
    """Is this a page listing many records rather than selling one?

    An artist or collection page carries prices belonging to other records -
    us.rarevinyl.com/collections/the-whitest-boy-alive yielded a "price" of
    18,200 USD - so it must never become an offer.
    """
    parts = urlsplit(url)
    if _SEARCH_PARAMS & set(parse_qs(parts.query)):
        return True
    if _PRODUCT_PATH.search(parts.path):
        # A product segment settles it: a Shopify product lives under
        # /collections/<x>/products/<y>, and an Amazon record under
        # /<slug>/dp/<asin>, both of which read as taxonomy otherwise.
        return False
    return bool(_TAXONOMY_PATH.search(parts.path) or _GRID_PATH.search(parts.path))


def _country_of(host: str, currency: str) -> str | None:
    """Where a shop probably ships from: its domain first, its prices second.

    Returning None is a real answer - `shipping` costs an unknown origin as
    the worst case, which is the safe direction to be wrong in.
    """
    suffix = host.rsplit(".", 1)[-1] if "." in host else ""
    if suffix in _TLD_COUNTRY:
        return _TLD_COUNTRY[suffix]
    if host in _HOST_COUNTRY:
        return _HOST_COUNTRY[host]
    return _CURRENCY_COUNTRY.get(currency)


def _why_nothing(findings: list[WebFinding]) -> str:
    """One line explaining an empty open-web result."""
    if not findings:
        return "no candidate pages"
    reasons: dict[str, int] = {}
    for finding in findings:
        reasons[finding.verdict] = reasons.get(finding.verdict, 0) + 1
    top = sorted(reasons.items(), key=lambda kv: -kv[1])[:2]
    return "; ".join(f"{reason} ({count})" for reason, count in top)


def host_of(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host
