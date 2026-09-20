"""Shared wiring: where state lives and how fetchers are built.

The CLI and the web app both need the same registry path and the same polite,
cached fetcher, so the choice is made once here.
"""

from __future__ import annotations

import os
from pathlib import Path

from .fetching import BrowserFetcher, CachingFetcher, Fetcher, HttpFetcher, RoutingFetcher
from .websearch import KEY_VARS, SearchProvider, from_env

VAR_DIR = Path(os.environ.get("GROOVE_VAR", "var"))
REGISTRY_PATH = VAR_DIR / "registry.json"
CACHE_DIR = VAR_DIR / "cache"

# Setup re-reads the same pages repeatedly, so it caches for longer than a
# search, where a stale price is worse than a slow one.
SETUP_TTL = 6 * 3600
SEARCH_TTL = 30 * 60


def build_fetcher(
    *,
    ttl: float = SEARCH_TTL,
    delay: float = 1.0,
    cache: bool = True,
    obey_robots: bool = True,
    browser_hosts: frozenset[str] | set[str] = frozenset(),
) -> Fetcher:
    """The fetcher the app should use.

    `browser_hosts` routes JavaScript-rendered shops through Playwright while
    everything else stays on fast, polite HTTP.
    """
    http: Fetcher = HttpFetcher(delay=delay, obey_robots=obey_robots)
    if cache:
        http = CachingFetcher(inner=http, directory=CACHE_DIR, ttl_seconds=ttl)
    if not browser_hosts or not BrowserFetcher.available():
        return http
    browser: Fetcher = BrowserFetcher()
    if cache:
        browser = CachingFetcher(inner=browser, directory=CACHE_DIR / "browser", ttl_seconds=ttl)
    return RoutingFetcher(default=http, browser=browser, browser_hosts=frozenset(browser_hosts))


def build_browser_fetcher(*, ttl: float = SETUP_TTL, cache: bool = True) -> Fetcher:
    """A browser-only fetcher, used when HTTP calibration comes up empty."""
    browser: Fetcher = BrowserFetcher()
    if cache:
        browser = CachingFetcher(inner=browser, directory=CACHE_DIR / "browser", ttl_seconds=ttl)
    return browser


def browser_hosts_for(registry) -> frozenset[str]:
    """Hosts whose learned recipe says they need a real browser."""
    import urllib.parse

    hosts = set()
    for shop in registry.shops:
        recipe = registry.recipes.get(shop.id)
        if (recipe and recipe.needs_browser) or shop.needs_browser:
            host = urllib.parse.urlsplit(shop.base_url).netloc.lower()
            hosts.add(host)
            hosts.add(host[4:] if host.startswith("www.") else host)
    return frozenset(hosts)


def browser_available() -> bool:
    return BrowserFetcher.available()


def build_search_provider(registry=None, *, enabled: bool = True) -> SearchProvider | None:
    """The open-web source, if this machine is configured for one.

    Returns None when no key is set. That is a normal state, not a failure:
    the app falls back to the calibrated shops and says so.
    """
    if not enabled:
        return None
    country = (getattr(registry, "location", None) or "PL").lower()
    return from_env(country=country)


def search_provider_hint() -> str:
    """How to change the open-web source, for a UI to show alongside results."""
    return (
        "The open web is searched through every engine configured here at once - "
        "keyless DuckDuckGo always, plus your own SearXNG if GROOVE_SEARXNG_URL is "
        "set, plus Brave or Serper if one of " + ", ".join(KEY_VARS) + " is. Engines "
        "differ more in coverage than in ranking, so asking several finds records a "
        "single one misses. GROOVE_SEARCH_PROVIDER names the engines to use instead "
        "(e.g. 'searxng', or 'searxng,duckduckgo'); 'none' switches the open web off."
    )
