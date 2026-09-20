"""Setup: pick the best-supplied shops for a location and learn how to search them.

"Best supplied" is measured, not assumed. Every candidate from the location's
seed list is calibrated and then asked for a fixed benchmark basket of albums;
the share it can actually sell is its coverage, and the highest-covering shops
are adopted into the registry. A shop that is dead, unscrapeable or thinly
stocked drops out without anyone having to curate a list by hand.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace

from .calibration import CalibrationResult, calibrate
from .domain import Shop
from .fetching import Fetcher
from .matching import score_offer
from .normalize import normalize_query
from .recipes import SearchRecipe, run_recipe
from .registry import Registry
from .seeds import load_seeds

# A deliberately broad basket: a shop that only stocks one genre should not
# outrank a generalist, and a shop that stocks nothing should score zero.
BENCHMARK: tuple[str, ...] = (
    "Radiohead - OK Computer",
    "Pink Floyd - Wish You Were Here",
    "Miles Davis - Kind of Blue",
    "Nirvana - Nevermind",
    "Daft Punk - Discovery",
    "Kendrick Lamar - good kid, m.A.A.d city",
    "Metallica - Master of Puppets",
    "Slowdive - Souvlaki",
    "Kult - Posluchaj to do ciebie",
    "The Beatles - Rubber Soul",
    # A recent Brazilian indie release: separates shops that really carry
    # imports from shops that only stock the classics.
    "Sessa - Pequena Vertigem de Amor",
)

TOP_N = 10


@dataclass
class ShopScore:
    """How one candidate shop performed during setup."""

    shop: Shop
    recipe: SearchRecipe | None = None
    coverage: float = 0.0
    offers: int = 0
    error: str | None = None
    log: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        """Calibrated shops are kept even at low coverage.

        A specialist that stocks none of a mainstream basket may still be the
        only shop carrying the record the user actually wants; coverage ranks
        it far down the list rather than throwing it away.
        """
        return self.recipe is not None

    @property
    def summary(self) -> str:
        if self.error:
            return f"{self.shop.name}: {self.error}"
        basket = f"{self.coverage:.0%} of the basket, {self.offers} offers"
        via = " (via browser)" if self.recipe and self.recipe.needs_browser else ""
        return f"{self.shop.name}: {basket}{via}"


@dataclass
class DiscoveryOutcome:
    """The result of a setup run - what was adopted, what was not, and why."""

    adopted: list[ShopScore] = field(default_factory=list)
    rejected: list[ShopScore] = field(default_factory=list)

    @property
    def scores(self) -> list[ShopScore]:
        return self.adopted + self.rejected


async def discover(
    fetcher: Fetcher,
    location: str = "PL",
    *,
    benchmark: tuple[str, ...] = BENCHMARK,
    limit: int = TOP_N,
    concurrency: int = 4,
    shops: list[Shop] | None = None,
    browser_fetcher: Fetcher | None = None,
    on_progress=None,
) -> DiscoveryOutcome:
    """Calibrate and rank candidate shops, keeping the `limit` best supplied.

    `on_progress` is called with each finished `ShopScore` so a UI can report
    a long setup as it happens. When `browser_fetcher` is given, a shop that
    cannot be calibrated over plain HTTP is retried in a real browser, which
    recovers shops that render their results in JavaScript.
    """
    if shops is None:
        shops, _ = load_seeds(location)
    gate = asyncio.Semaphore(concurrency)

    async def evaluate(shop: Shop) -> ShopScore:
        async with gate:
            score = await _evaluate_shop(shop, fetcher, benchmark, browser_fetcher)
        if on_progress:
            on_progress(score)
        return score

    scores = await asyncio.gather(*(evaluate(shop) for shop in shops))
    usable = sorted(
        (s for s in scores if s.usable),
        key=lambda s: (s.coverage, s.offers),
        reverse=True,
    )
    rejected = [s for s in scores if not s.usable]
    return DiscoveryOutcome(adopted=usable[:limit], rejected=rejected + usable[limit:])


async def recalibrate(
    shop: Shop,
    fetcher: Fetcher,
    *,
    benchmark: tuple[str, ...] = BENCHMARK,
    browser_fetcher: Fetcher | None = None,
) -> ShopScore:
    """Re-learn a single shop - the repair path when a recipe stops working."""
    return await _evaluate_shop(shop, fetcher, benchmark, browser_fetcher)


def apply(outcome: DiscoveryOutcome, registry: Registry, location: str = "PL") -> Registry:
    """Write an adopted set of shops into the registry, replacing what was there."""
    registry.location = location
    for score in outcome.adopted:
        registry.adopt(score.shop, score.recipe, coverage=score.coverage)
    adopted_ids = {s.shop.id for s in outcome.adopted}
    for shop in list(registry.shops):
        if shop.id not in adopted_ids:
            registry.drop(shop.id)
    return registry


async def _evaluate_shop(
    shop: Shop,
    fetcher: Fetcher,
    benchmark: tuple[str, ...],
    browser_fetcher: Fetcher | None = None,
) -> ShopScore:
    """Calibrate and score a shop, falling back to a real browser if needed."""
    score = await _evaluate_over(shop, fetcher, benchmark)
    if score.usable or browser_fetcher is None:
        return score
    # The shop may simply render its results in JavaScript; try a browser.
    retry = await _evaluate_over(
        replace(shop, needs_browser=True), browser_fetcher, benchmark, max_templates=8
    )
    if retry.usable:
        return retry
    return replace(score, error=f"{score.error} (browser retry: {retry.error})")


async def _evaluate_over(
    shop: Shop, fetcher: Fetcher, benchmark: tuple[str, ...], max_templates: int | None = None
) -> ShopScore:
    result: CalibrationResult = await calibrate(shop, fetcher, max_templates=max_templates)
    if not result.ok:
        return ShopScore(shop=shop, error=result.error, log=result.log)

    recipe = result.recipe
    hits = 0
    offers = 0
    for line in benchmark:
        query = normalize_query(line)
        found = await _stocks(recipe, query, fetcher)
        hits += 1 if found else 0
        offers += found
    coverage = hits / len(benchmark) if benchmark else 0.0
    return ShopScore(
        shop=shop,
        recipe=recipe.stamped(),
        coverage=coverage,
        offers=offers,
        log=result.log,
    )


async def _stocks(recipe: SearchRecipe, query, fetcher: Fetcher) -> int:
    """How many confident matches a shop returns for one benchmark album."""
    for term in query.search_terms[:2]:
        run = await run_recipe(recipe, term, fetcher)
        if not run.ok:
            continue
        matches = sum(1 for raw in run.offers if score_offer(query, raw).matched)
        if matches:
            return matches
    return 0
