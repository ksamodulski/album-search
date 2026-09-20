"""Search every known shop for every album the user asked about.

This is the module the UI and the CLI talk to. Its whole interface is
`search_albums(queries, registry, fetcher)`; fan-out, per-shop term retries,
currency-aware price comparison and failure bookkeeping happen inside.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from decimal import Decimal

from .domain import AlbumQuery, AlbumResult, Availability, Format, Lead, Offer, ShopReport
from .fetching import Fetcher
from .matching import read_availability, score_offer
from .openweb import find_offers, host_of
from .pricing import convert, parse_money
from .recipes import SearchRecipe, run_recipe
from .registry import Registry
from .shipping import landed_cost
from .websearch import SearchProvider

# Stop asking a shop once a term has produced this many confident matches.
ENOUGH = 3
# How many runners-up to show beside the winner.
MAX_ALTERNATIVES = 4


async def search_albums(
    queries: list[AlbumQuery],
    registry: Registry,
    fetcher: Fetcher,
    *,
    max_alternatives: int = MAX_ALTERNATIVES,
    provider: SearchProvider | None = None,
    on_progress: Callable[[AlbumQuery, ShopReport], None] | None = None,
) -> list[AlbumResult]:
    """Find the best offer for each query across every source we have.

    Two sources, one seam: the calibrated shops in the registry, and - when a
    `provider` is configured - the open web, which reaches shops nobody seeded.
    Results come back in the order the queries were given. A source that fails
    is reported per album rather than aborting the search.

    `on_progress` is called with each source's report the moment that source
    finishes, rather than at the end - a search runs for tens of seconds and a
    caller that cannot say what is happening leaves the user watching a
    spinner. It must not raise: a reporting bug is not a reason to lose a
    search that otherwise worked.
    """
    shops = registry.searchable
    if not queries or (not shops and provider is None):
        return [AlbumResult(query=q) for q in queries]

    jobs = [
        _search_one_shop(query, shop, registry.recipes[shop.id], fetcher, registry, on_progress)
        for query in queries
        for shop in shops
    ]
    settled = await asyncio.gather(*jobs)
    web = await _search_open_web(queries, registry, fetcher, provider, on_progress)

    results: list[AlbumResult] = []
    index = 0
    for query in queries:
        offers: list[Offer] = []
        reports: list[ShopReport] = []
        for shop in shops:
            shop_offers, report = settled[index]
            index += 1
            offers.extend(shop_offers)
            reports.append(report)
            if report.ok:
                registry.record_success(shop.id, report.offers_found)
            else:
                registry.record_failure(shop.id, report.error or "unknown")
        web_offers, web_report, leads = web.get(query.raw, ([], None, ()))
        offers.extend(web_offers)
        if web_report is not None:
            reports.append(web_report)
        results.append(_assemble(query, offers, tuple(reports), registry, max_alternatives, leads))
    return results


def _announce(
    on_progress: Callable[[AlbumQuery, ShopReport], None] | None,
    query: AlbumQuery,
    report: ShopReport,
) -> None:
    """Tell the caller one source is done, without letting that break a search."""
    if on_progress is None:
        return
    try:
        on_progress(query, report)
    except Exception:  # noqa: BLE001 - a reporting bug must not lose the results
        pass


async def _search_open_web(
    queries: list[AlbumQuery],
    registry: Registry,
    fetcher: Fetcher,
    provider: SearchProvider | None,
    on_progress: Callable[[AlbumQuery, ShopReport], None] | None = None,
) -> dict[str, tuple[list[Offer], ShopReport | None, tuple[Lead, ...]]]:
    """Ask the open web about every query, keyed by the user's original line.

    Shops the registry already covers are excluded: they have a learned recipe
    that reads their search grid properly, and letting them in twice would put
    the same record on the page under two names.
    """
    if provider is None:
        return {}
    known = frozenset(host_of(shop.base_url) for shop in registry.shops)
    found: dict[str, tuple[list[Offer], ShopReport | None, tuple[Lead, ...]]] = {}
    for query in queries:
        offers, report, findings = await find_offers(
            query,
            provider,
            fetcher,
            location=registry.location,
            currency=registry.currency,
            rates=registry.rates,
            shipping=registry.shipping,
            known_hosts=known,
        )
        found[query.raw] = (offers, report, _leads(findings))
        _announce(on_progress, query, report)
    return found


def _leads(findings) -> tuple[Lead, ...]:
    """Sellers that had the record but refused to show us a price, deduped.

    One per host: Allegro answers 403 for every page we try, and a column of
    identical refusals is noise where a single "Allegro has it" is the fact.
    """
    seen: set[str] = set()
    leads: list[Lead] = []
    for finding in findings:
        if finding.lead is None or finding.lead.host in seen:
            continue
        seen.add(finding.lead.host)
        leads.append(finding.lead)
    return tuple(leads)


async def _search_one_shop(
    query: AlbumQuery,
    shop,
    recipe: SearchRecipe,
    fetcher: Fetcher,
    registry: Registry,
    on_progress: Callable[[AlbumQuery, ShopReport], None] | None = None,
) -> tuple[list[Offer], ShopReport]:
    """Try the query's terms against one shop until something matches."""
    last_error: str | None = None
    for term in query.search_terms:
        run = await run_recipe(recipe, term, fetcher)
        if not run.ok:
            # Plenty of shops answer a fruitless search with a 404 page. That
            # is an empty result, not a shop that needs repairing.
            if "404" not in (run.error or ""):
                last_error = run.error
            continue
        last_error = None
        matched: list[Offer] = []
        for raw in run.offers:
            verdict = score_offer(query, raw)
            if not verdict.matched:
                continue
            price = parse_money(raw.price_text, default_currency=shop.currency)
            if price is None:
                continue
            matched.append(
                Offer(
                    shop_id=shop.id,
                    shop_name=shop.name,
                    title=raw.title_text,
                    price=price,
                    url=raw.url,
                    format=verdict.format,
                    availability=read_availability(raw.availability_text, raw.title_text),
                    confidence=verdict.confidence,
                    image_url=raw.image_url,
                    country=shop.country,
                    # Domestic postage is costed too. A 63 PLN CD that ships
                    # for 12 and an 85 PLN one that ships free are not in the
                    # order their listed prices suggest.
                    landed=landed_cost(
                        price,
                        origin=shop.country,
                        destination=registry.location,
                        fmt=verdict.format,
                        rates=registry.shipping,
                        fx=registry.rates,
                        target_currency=registry.currency,
                    ),
                )
            )
            if len(matched) >= ENOUGH:
                break
        if matched:
            report = ShopReport(shop.id, shop.name, offers_found=len(matched))
            _announce(on_progress, query, report)
            return matched, report
    report = ShopReport(shop.id, shop.name, offers_found=0, error=last_error)
    _announce(on_progress, query, report)
    return [], report


def _assemble(
    query: AlbumQuery,
    offers: list[Offer],
    reports: tuple[ShopReport, ...],
    registry: Registry,
    max_alternatives: int,
    leads: tuple[Lead, ...] = (),
) -> AlbumResult:
    """Pick the winner and its rivals from everything the shops returned."""
    buyable = [o for o in offers if o.availability is not Availability.OUT_OF_STOCK]
    if not buyable:
        return AlbumResult(query=query, reports=reports, leads=leads)

    # One offer per shop *and carrier*, so a shop cannot crowd out the field
    # and a CD never hides the vinyl the user might want.
    cheapest: dict[tuple[str, Format], Offer] = {}
    for offer in buyable:
        key = (offer.shop_id, offer.format)
        current = cheapest.get(key)
        if current is None or _comparable(offer, registry) < _comparable(current, registry):
            cheapest[key] = offer

    ranked = sorted(cheapest.values(), key=lambda o: (_comparable(o, registry), -o.confidence))
    return AlbumResult(
        query=query,
        best=ranked[0],
        alternatives=tuple(ranked[1 : 1 + max_alternatives]),
        reports=reports,
        leads=leads,
    )


def _comparable(offer: Offer, registry: Registry) -> Decimal:
    """What the buyer actually pays, in the registry's currency, for ranking.

    Falls back to the converted list price for an offer that was never costed,
    so an uncosted offer still ranks somewhere sensible rather than first.
    """
    if offer.landed is not None:
        return offer.landed.total.amount
    return convert(offer.price, registry.currency, registry.rates).amount
