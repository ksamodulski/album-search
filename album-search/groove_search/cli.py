"""Command line entry point: setup, search, doctor, recalibrate, serve."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .discovery import TOP_N, apply, discover, recalibrate
from .normalize import normalize_lines
from .registry import RECALIBRATE_AFTER, Registry
from .runtime import (
    REGISTRY_PATH,
    SETUP_TTL,
    build_browser_fetcher,
    build_fetcher,
    build_search_provider,
    browser_hosts_for,
    search_provider_hint,
)
from .fetching import BrowserFetcher, close_fetcher
from .search import search_albums

BOLD, DIM, GREEN, YELLOW, RED, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="groove", description="Find the best CD/vinyl offers across record shops.")
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH, help="registry file (default: var/registry.json)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="discover and learn the best-supplied shops")
    p_setup.add_argument("--location", default="PL")
    p_setup.add_argument("--limit", type=int, default=TOP_N)
    p_setup.add_argument("--no-cache", action="store_true")
    p_setup.add_argument("--concurrency", type=int, default=4)
    p_setup.add_argument("--no-browser", action="store_true", help="skip the Playwright retry for JS-only shops")

    p_search = sub.add_parser("search", help="search for albums")
    p_search.add_argument("albums", nargs="*", help='e.g. "Pet Fox - A face in your life"')
    p_search.add_argument("--file", type=Path, help="read one album per line from a file")
    p_search.add_argument(
        "--no-web", action="store_true", help="search only the calibrated shops, not the open web"
    )
    p_search.add_argument(
        "--shop-price", action="store_true", help="rank on the listed price instead of the delivered cost"
    )

    sub.add_parser("doctor", help="show shop health")

    p_recal = sub.add_parser("recalibrate", help="re-learn a shop whose recipe broke")
    p_recal.add_argument("shop_id", nargs="?", help="shop to repair (default: every broken shop)")
    p_recal.add_argument(
        "--force", action="store_true", help="re-learn even a recipe younger than the cooldown"
    )

    p_serve = sub.add_parser("serve", help="run the web app")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)
    if args.command == "serve":
        import uvicorn

        uvicorn.run("groove_search.web.app:app", host=args.host, port=args.port, reload=False)
        return 0
    return asyncio.run(_dispatch(args))


async def _dispatch(args) -> int:
    registry = Registry.load(args.registry)
    match args.command:
        case "setup":
            return await _setup(args, registry)
        case "search":
            return await _search(args, registry)
        case "doctor":
            return _doctor(registry)
        case "recalibrate":
            return await _recalibrate(args, registry)
    return 1


async def _setup(args, registry: Registry) -> int:
    fetcher = build_fetcher(ttl=SETUP_TTL, cache=not args.no_cache)
    use_browser = not args.no_browser and BrowserFetcher.available()
    browser = build_browser_fetcher(cache=not args.no_cache) if use_browser else None
    note = "" if use_browser else "  (no browser fallback: pip install playwright)"
    print(f"{BOLD}Calibrating candidate shops for {args.location}...{RESET}{DIM}{note}{RESET}\n")

    def report(score) -> None:
        mark = f"{GREEN}ok  {RESET}" if score.usable else f"{RED}skip{RESET}"
        print(f"  {mark} {score.summary}")

    try:
        outcome = await discover(
            fetcher,
            args.location,
            limit=args.limit,
            concurrency=args.concurrency,
            browser_fetcher=browser,
            on_progress=report,
        )
    finally:
        await close_fetcher(browser)
        await close_fetcher(fetcher)
    if not outcome.adopted:
        print(f"\n{RED}No shop could be calibrated.{RESET} Check connectivity, or add shops to the seed list.")
        return 1

    registry = apply(outcome, registry, args.location)
    registry.path = args.registry
    registry.save()
    print(f"\n{BOLD}Top {len(outcome.adopted)} shops adopted{RESET} -> {args.registry}")
    for rank, score in enumerate(outcome.adopted, 1):
        via = " via browser" if score.recipe.needs_browser else ""
        print(
            f"  {rank:2}. {score.shop.name:24} {score.coverage:>5.0%} coverage  "
            f"{DIM}{score.recipe.item_selector}{via}{RESET}"
        )
    return 0


async def _search(args, registry: Registry) -> int:
    if not registry.is_configured:
        print(f"{RED}No shops configured.{RESET} Run: groove setup")
        return 1
    text = args.file.read_text(encoding="utf-8") if args.file else "\n".join(args.albums)
    queries = normalize_lines(text)
    if not queries:
        print("Nothing to search for.")
        return 1

    provider = build_search_provider(registry, enabled=not args.no_web)
    if provider is None and not args.no_web:
        print(f"{DIM}Searching calibrated shops only. {search_provider_hint()}{RESET}\n")
    fetcher = build_fetcher(browser_hosts=browser_hosts_for(registry))
    try:
        results = await search_albums(queries, registry, fetcher, provider=provider)
    finally:
        await close_fetcher(fetcher)
        await close_fetcher(provider)
    registry.save()

    for result in results:
        print(f"\n{BOLD}{result.query.label}{RESET}")
        if not result.available:
            print(f"  {YELLOW}Not available{RESET} - checked {result.shops_searched} shop(s)")
            for failed in result.shops_failed:
                print(f"    {DIM}{failed.shop_name}: {failed.error}{RESET}")
            continue
        best = result.best
        print(f"  {GREEN}BEST{RESET} {_headline(best, args):>12}  {best.shop_name:20} {best.format:5} {best.title[:56]}")
        _print_breakdown(best, args)
        print(f"       {DIM}{best.url}{RESET}")
        for alt in result.alternatives:
            print(f"       {_headline(alt, args):>12}  {alt.shop_name:20} {alt.format:5} {_delta(alt, best, args)}")
            _print_breakdown(alt, args)
            print(f"       {DIM}{alt.url}{RESET}")
        saved = result.savings_vs_worst()
        if saved and saved > 0:
            basis = "listed price" if args.shop_price else "delivered cost"
            print(f"       {DIM}best offer saves {saved:.0f}% on {basis} against the priciest found{RESET}")
    return 0


def _headline(offer, args) -> str:
    """The number the user should compare: delivered, unless they asked not to."""
    if args.shop_price or offer.landed is None:
        return str(offer.price)
    return str(offer.landed.total)


def _print_breakdown(offer, args) -> None:
    """Show the parts of a delivered cost, so an estimate is never mistaken
    for a quote and a foreign bargain cannot hide its postage."""
    cost = offer.landed
    if args.shop_price or cost is None or not cost.has_extras:
        return
    parts = [f"{cost.item.amount:.2f} item"]
    if cost.shipping and cost.shipping.amount:
        parts.append(f"{cost.shipping.amount:.2f} shipping")
    if cost.import_tax and cost.import_tax.amount:
        parts.append(f"{cost.import_tax.amount:.2f} import tax")
    where = f" from {offer.country}" if offer.country and offer.country != "PL" else ""
    print(f"       {DIM}est. {' + '.join(parts)}{where}{RESET}")


def _delta(alt, best, args) -> str:
    """How much dearer an alternative is, on the same basis as the headline."""
    if args.shop_price or alt.landed is None or best.landed is None:
        if alt.price.currency != best.price.currency:
            return "(other currency)"
        return f"(+{alt.price.percent_above(best.price):.0f}%)"
    return f"(+{alt.landed.total.percent_above(best.landed.total):.0f}%)"


def _doctor(registry: Registry) -> int:
    if not registry.shops:
        print("Registry is empty. Run: groove setup")
        return 1
    print(f"{BOLD}{'shop':24} {'status':10} {'coverage':>9} {'last ok':22} error{RESET}")
    for shop in registry.shops:
        health = registry.health_of(shop.id)
        colour = {"healthy": GREEN, "degraded": YELLOW, "broken": RED}[health.status]
        print(
            f"{shop.name:24} {colour}{health.status:10}{RESET} {health.coverage:>8.0%} "
            f"{(health.last_ok or '-'):22} {health.last_error or ''}"
        )
    repairable = registry.repairable
    if repairable:
        print(f"\n{YELLOW}Repair with:{RESET} groove recalibrate {repairable[0].id}   (or: groove recalibrate)")
    elif registry.broken:
        names = ", ".join(s.name for s in registry.broken)
        print(
            f"\n{DIM}Broken but not worth re-learning yet ({names}): the recipe is newer than "
            f"{RECALIBRATE_AFTER.days} days, so the shop is blocking us rather than having changed. "
            f"Override with: groove recalibrate <shop> --force{RESET}"
        )
    return 0


async def _recalibrate(args, registry: Registry) -> int:
    targets = [registry.shop(args.shop_id)] if args.shop_id else registry.repairable
    targets = [t for t in targets if t]
    if not targets:
        if args.shop_id:
            print(f"Unknown shop: {args.shop_id}")
            return 1
        if registry.broken:
            # Broken, but every one of them is inside the cooldown: a
            # deliberate no-op, not a failure.
            print(_cooldown_note(registry, registry.broken))
            return 0
        print("Nothing to recalibrate.")
        return 1
    if not args.force:
        skipped = [t for t in targets if registry.repair_blocked(t.id)]
        targets = [t for t in targets if t not in skipped]
        if skipped:
            print(_cooldown_note(registry, skipped))
        if not targets:
            return 0
    fetcher = build_fetcher(ttl=0, cache=False)  # a repair must see the live page
    browser = build_browser_fetcher(cache=False) if BrowserFetcher.available() else None
    failed = 0
    for shop in targets:
        print(f"Re-learning {shop.name}...")
        score = await recalibrate(shop, fetcher, browser_fetcher=browser)
        if score.usable:
            registry.adopt(shop, score.recipe, coverage=score.coverage)
            print(f"  {GREEN}fixed{RESET}: {score.summary}")
        else:
            failed += 1
            print(f"  {RED}still broken{RESET}: {score.error}")
            for line in score.log[:6]:
                print(f"    {DIM}{line}{RESET}")
    await close_fetcher(browser)
    await close_fetcher(fetcher)
    registry.path = args.registry
    registry.save()
    return 1 if failed == len(targets) else 0


def _cooldown_note(registry: Registry, shops) -> str:
    """Explain why a broken shop is being left alone rather than re-learned."""
    lines = []
    for shop in shops:
        age = registry.calibrated_age(shop.id)
        days = "recently" if age is None else "today" if age.days == 0 else f"{age.days}d ago"
        error = registry.health_of(shop.id).last_error or "failing"
        lines.append(f"  {DIM}{shop.name}: calibrated {days}, {error}{RESET}")
    return (
        f"{YELLOW}Skipping{RESET} - recipe newer than {RECALIBRATE_AFTER.days} days, so the shop is "
        f"refusing us rather than having changed. Assuming it is simply not working.\n"
        + "\n".join(lines)
        + f"\n  {DIM}Re-learn anyway with --force.{RESET}"
    )


if __name__ == "__main__":
    sys.exit(main())
