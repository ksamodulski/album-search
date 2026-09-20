"""Ask a search engine which pages might be selling an album.

This is the only part of groove-search that needs an account somewhere. It is
kept behind a one-method protocol so the rest of the app never learns which
engine answered, the test suite can stay offline, and swapping Brave for
something else is a class, not a refactor.

What comes back is a list of URLs and titles - never a price. Deciding whether
a result really is the record, and what it costs, is `product` and `matching`'s
job, which is why nothing here tries to be clever about relevance.
"""

from __future__ import annotations

import asyncio
import os
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from selectolax.parser import HTMLParser

# Engines return plenty of chaff - reviews, streaming links, lyrics sites.
# Asking for more than this just buys more pages to fetch and discard.
DEFAULT_LIMIT = 12

# Pages that never sell a record, so never worth fetching.
BLOCKED_HOSTS: frozenset[str] = frozenset(
    """
    spotify.com open.spotify.com music.apple.com youtube.com youtu.be soundcloud.com
    last.fm allmusic.com albumoftheyear.org pitchfork.com wikipedia.org rateyourmusic.com
    genius.com deezer.com tidal.com facebook.com instagram.com twitter.com x.com reddit.com
    """.split()
)


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One result from a search engine: a page that might be selling this."""

    title: str
    url: str
    snippet: str = ""

    @property
    def host(self) -> str:
        from urllib.parse import urlsplit

        host = urlsplit(self.url).netloc.lower()
        return host[4:] if host.startswith("www.") else host


class SearchProvider(Protocol):
    """Anything that can turn a phrase into candidate URLs.

    Like `Fetcher`, an implementation must not raise for network trouble: an
    engine that is down is an empty result, not a failed search. It should
    leave a reason in `last_error` when it comes back empty for a reason the
    user needs to know, so "the engine refused us" is never displayed as
    "this record is not for sale".
    """

    last_error: str | None

    async def search(self, phrase: str, *, limit: int = DEFAULT_LIMIT) -> list[SearchHit]: ...


def usable(hits: list[SearchHit]) -> list[SearchHit]:
    """Drop results that are certainly not shops, keeping order."""
    seen: set[str] = set()
    kept: list[SearchHit] = []
    for hit in hits:
        if not hit.url.startswith("http") or hit.host in BLOCKED_HOSTS:
            continue
        if hit.url in seen:
            continue
        seen.add(hit.url)
        kept.append(hit)
    return kept


@dataclass
class BraveSearch:
    """Brave's Search API. Free tier is ample for one person's want-list."""

    api_key: str = ""
    endpoint: str = "https://api.search.brave.com/res/v1/web/search"
    country: str = "pl"
    timeout: float = 15.0
    last_error: str | None = None
    _client: httpx.AsyncClient | None = field(default=None, repr=False)

    async def search(self, phrase: str, *, limit: int = DEFAULT_LIMIT) -> list[SearchHit]:
        if not self.api_key:
            self.last_error = "no API key configured"
            return []
        client = self._ensure_client()
        try:
            response = await client.get(
                self.endpoint,
                params={"q": phrase, "count": min(limit, 20), "country": self.country},
                headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.last_error = f"search engine unreachable ({type(exc).__name__})"
            return []
        results = (payload.get("web") or {}).get("results") or []
        return [
            SearchHit(title=r.get("title", ""), url=r.get("url", ""), snippet=r.get("description", ""))
            for r in results
            if r.get("url")
        ]

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


@dataclass
class SerperSearch:
    """Serper.dev, a Google proxy - the other key people tend to already have."""

    api_key: str = ""
    endpoint: str = "https://google.serper.dev/search"
    country: str = "pl"
    timeout: float = 15.0
    last_error: str | None = None
    _client: httpx.AsyncClient | None = field(default=None, repr=False)

    async def search(self, phrase: str, *, limit: int = DEFAULT_LIMIT) -> list[SearchHit]:
        if not self.api_key:
            self.last_error = "no API key configured"
            return []
        client = self._ensure_client()
        try:
            response = await client.post(
                self.endpoint,
                json={"q": phrase, "num": min(limit, 20), "gl": self.country},
                headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.last_error = f"search engine unreachable ({type(exc).__name__})"
            return []
        return [
            SearchHit(title=r.get("title", ""), url=r.get("link", ""), snippet=r.get("snippet", ""))
            for r in payload.get("organic") or []
            if r.get("link")
        ]

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


@dataclass
class FakeSearch:
    """Test adapter: canned hits per phrase, and a record of what was asked."""

    hits: dict[str, list[SearchHit]] = field(default_factory=dict)
    default: list[SearchHit] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)
    last_error: str | None = None

    async def search(self, phrase: str, *, limit: int = DEFAULT_LIMIT) -> list[SearchHit]:
        self.asked.append(phrase)
        return list(self.hits.get(phrase, self.default))[:limit]


@dataclass
class DuckDuckGoSearch:
    """DuckDuckGo's no-JavaScript endpoint - the provider that needs no account.

    Every keyed engine wants a registration that can fail, stall or simply be
    unavailable in your country, which leaves the open-web source switched off
    and the app back where it started. This reads the plain HTML results page
    that DuckDuckGo serves to browsers without JavaScript, so it works the
    moment the app is installed.

    The trade is reliability: there is no contract here, it answers 202 or 429
    when asked too often, and its markup can change. Treat it as the default
    that gets you running, and a key as the upgrade.
    """

    endpoint: str = "https://html.duckduckgo.com/html/"
    country: str = "pl"
    timeout: float = 20.0
    # DuckDuckGo throttles hard. One request at a time, spaced out.
    delay: float = 2.0
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    # Once throttled, back right off - hammering only extends the block.
    throttled_delay: float = 15.0
    last_error: str | None = None
    _client: httpx.AsyncClient | None = field(default=None, repr=False)
    _last_call: float = field(default=0.0, repr=False)
    _throttled: bool = field(default=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def search(self, phrase: str, *, limit: int = DEFAULT_LIMIT) -> list[SearchHit]:
        async with self._lock:
            await self._wait_turn()
            try:
                response = await self._ensure_client().post(self.endpoint, data={"q": phrase})
                response.raise_for_status()
            except httpx.HTTPError as exc:
                self.last_error = f"DuckDuckGo unreachable ({type(exc).__name__})"
                return []
            html = response.text
            if _is_throttled(response.status_code, html):
                # 202 plus an "anomaly" notice, not an error status - so this
                # has to be sniffed or it reads as "no shop sells this".
                self._throttled = True
                self.last_error = (
                    "DuckDuckGo is rate-limiting us - wait a few minutes, "
                    "or set a search API key for something sturdier"
                )
                return []
        hits = _parse_ddg(html)[:limit]
        if hits:
            self.last_error = None
            self._throttled = False
        return hits

    async def _wait_turn(self) -> None:
        wait = self.throttled_delay if self._throttled else self.delay
        elapsed = time.monotonic() - self._last_call
        if self._last_call and elapsed < wait:
            await asyncio.sleep(wait - elapsed)
        self._last_call = time.monotonic()

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": self.user_agent},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# DuckDuckGo answers a throttle with 202 and a friendly notice rather than a
# 429, so the status code alone is not enough to tell success from refusal.
_THROTTLE_MARKERS = ("anomaly", "unusual traffic", "captcha")


def _is_throttled(status: int, html: str) -> bool:
    lowered = html[:4000].lower()
    return status == 202 or any(marker in lowered for marker in _THROTTLE_MARKERS)


def _parse_ddg(html: str) -> list[SearchHit]:
    """Pull results out of DuckDuckGo's no-JS page. Pure - no I/O."""
    hits: list[SearchHit] = []
    tree = HTMLParser(html)
    for result in tree.css("div.result, div.web-result"):
        anchor = result.css_first("a.result__a")
        if anchor is None:
            continue
        url = _clean_ddg_url(anchor.attributes.get("href") or "")
        if not url:
            continue
        snippet = result.css_first("a.result__snippet")
        hits.append(
            SearchHit(
                title=" ".join(anchor.text(strip=True).split()),
                url=url,
                snippet=" ".join(snippet.text(strip=True).split()) if snippet else "",
            )
        )
    return hits


def _clean_ddg_url(href: str) -> str:
    """Unwrap DuckDuckGo's redirect, and drop its adverts.

    Sponsored results are served from duckduckgo.com itself with an `ad_domain`
    parameter. They are adverts for marketplaces, not the shop pages we want.
    """
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlsplit(href)
    query = urllib.parse.parse_qs(parsed.query)
    if "ad_domain" in query or "ad_provider" in query or parsed.path.endswith("/y.js"):
        return ""
    target = query.get("uddg")
    if target:
        return target[0]
    return href if href.startswith("http") else ""


@dataclass
class SearxngSearch:
    """A SearXNG instance's JSON API - keyless *and* unthrottled, if you host it.

    SearXNG is a metasearch front-end you can run yourself:

        docker run -d -p 8888:8080 -e SEARXNG_SETTINGS__SEARCH__FORMATS='["html","json"]' \
            searxng/searxng

    That removes the trade the DuckDuckGo provider forces - no account, and no
    rate limit either, because the instance is yours. Public instances are not
    a substitute: they almost all disable the JSON format, and several answer
    403 or 429 to anything automated.
    """

    base_url: str = "http://127.0.0.1:8888"
    country: str = "pl"
    timeout: float = 20.0
    engines: str = ""
    last_error: str | None = None
    _client: httpx.AsyncClient | None = field(default=None, repr=False)

    async def search(self, phrase: str, *, limit: int = DEFAULT_LIMIT) -> list[SearchHit]:
        params = {"q": phrase, "format": "json", "language": self.country}
        if self.engines:
            params["engines"] = self.engines
        try:
            response = await self._ensure_client().get(f"{self.base_url.rstrip('/')}/search", params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.last_error = (
                f"SearXNG at {self.base_url} did not answer ({type(exc).__name__}) - "
                "is it running, and is the JSON format enabled?"
            )
            return []
        self.last_error = None
        return [
            SearchHit(title=r.get("title", ""), url=r.get("url", ""), snippet=r.get("content", ""))
            for r in payload.get("results") or []
            if r.get("url")
        ][:limit]

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


@dataclass
class MergedSearch:
    """Every configured engine asked at once, their results interleaved.

    Engines differ far more in *coverage* than in ranking: the same album
    comes back found through one and empty through another, so asking only
    one makes "is this record for sale" depend on which engine happens to be
    configured. Asking all of them removes that coin-flip, and it costs no
    extra page fetches - `openweb` still opens the same small budget of
    candidates, just chosen from a wider pool.

    Merging is round-robin rather than concatenation: the first hit from each
    engine, then the second from each, and so on. A page every engine ranks
    highly keeps its place at the top, while a page only one engine found
    still arrives early enough to be worth opening. `limit` is therefore per
    engine, not for the merged list.
    """

    providers: tuple[SearchProvider, ...]
    # How long a straggler may hold up engines that have already answered.
    # A throttled DuckDuckGo sleeps 15s before every call and then returns
    # nothing anyway; without a deadline it would pace the whole search.
    # Comfortably above a healthy engine's politeness delay, below that sleep.
    patience: float = 10.0
    last_error: str | None = None

    async def search(self, phrase: str, *, limit: int = DEFAULT_LIMIT) -> list[SearchHit]:
        if not self.providers:
            return []
        harvests = await asyncio.gather(
            *(self._ask(p, phrase, limit) for p in self.providers),
            # One engine's bug must not take down the engines that worked;
            # the whole point of merging is that no single engine decides.
            return_exceptions=True,
        )
        merged = _interleave([h for h in harvests if isinstance(h, list)])
        # Only a total blank is the user's problem. If one engine answered,
        # another's outage changed nothing they could act on - and reporting
        # it would print "DuckDuckGo is rate-limiting us" beside good results.
        self.last_error = None if merged else self._why_nothing()
        return merged

    async def _ask(self, provider: SearchProvider, phrase: str, limit: int) -> list[SearchHit]:
        try:
            return await asyncio.wait_for(provider.search(phrase, limit=limit), self.patience)
        except TimeoutError:
            return []

    def _why_nothing(self) -> str | None:
        """Why the merged result was empty, in every engine's own words."""
        reasons: list[str] = []
        for provider in self.providers:
            reason = getattr(provider, "last_error", None)
            if reason and reason not in reasons:
                reasons.append(reason)
        return "; ".join(reasons) if reasons else None

    async def aclose(self) -> None:
        for provider in self.providers:
            closer = getattr(provider, "aclose", None)
            if closer is not None:
                await closer()


def _interleave(harvests: list[list[SearchHit]]) -> list[SearchHit]:
    """Round-robin several engines' results, keeping each engine's own order."""
    merged: list[SearchHit] = []
    seen: set[str] = set()
    for rank in range(max((len(h) for h in harvests), default=0)):
        for harvest in harvests:
            if rank >= len(harvest):
                continue
            hit = harvest[rank]
            key = _same_page(hit.url)
            if key in seen:
                continue
            seen.add(key)
            merged.append(hit)
    return merged


def _same_page(url: str) -> str:
    """Key for "two engines returned the same page".

    Host and trailing slash are cosmetic, so they are normalised away. The
    query string is *not*: plenty of shops address a product entirely through
    it, and collapsing those would merge two different records into one.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    return f"{host}{parts.path.rstrip('/') or '/'}?{parts.query}"


PROVIDERS = {
    "brave": BraveSearch,
    "serper": SerperSearch,
    "duckduckgo": DuckDuckGoSearch,
    "searxng": SearxngSearch,
}

# Where a key is looked for. GROOVE_SEARCH_KEY is the generic fallback so a
# user can keep one key for this app without disturbing anything else on the
# machine; an engine's own variable wins, which matters now that two keyed
# engines can be built at once and must not be handed each other's key.
KEY_VARS = ("GROOVE_SEARCH_KEY", "BRAVE_API_KEY", "SERPER_API_KEY")
_OWN_KEY_VAR = {"brave": "BRAVE_API_KEY", "serper": "SERPER_API_KEY"}


def from_env(country: str = "pl") -> SearchProvider | None:
    """Build the open-web source this machine is configured for.

    Unset, this is *every* engine available here, merged - keyless DuckDuckGo
    always among them, so the source works on a fresh install rather than
    staying dark until someone completes a registration. Naming engines in
    `GROOVE_SEARCH_PROVIDER` pins the set instead ("searxng", or
    "searxng,duckduckgo"), which is how you tell one engine's blind spot from
    a record nobody sells. `GROOVE_SEARCH_PROVIDER=none` switches it off.
    """
    setting = os.environ.get("GROOVE_SEARCH_PROVIDER", "").strip().lower()
    if setting in ("none", "off"):
        return None
    names = [n.strip() for n in setting.split(",") if n.strip()] if setting else _configured()
    built = [p for p in (_build(name, country) for name in names) if p is not None]
    if not built:
        return None
    # One engine needs no merging machinery, and stays its own plain provider
    # so a pinned engine's errors reach the user exactly as they always did.
    return built[0] if len(built) == 1 else MergedSearch(tuple(built))


def _configured() -> list[str]:
    """Every engine this machine can actually use, best first.

    DuckDuckGo is always last and always present: it needs no account, so it
    is the one engine that can never be missing. It earns its place even
    beside a sturdier engine - it has found records SearXNG missed - but it
    throttles, which is why it never leads.
    """
    names = []
    if os.environ.get("GROOVE_SEARXNG_URL", "").strip():
        names.append("searxng")
    if os.environ.get("SERPER_API_KEY"):
        names.append("serper")
    if os.environ.get("BRAVE_API_KEY") or os.environ.get("GROOVE_SEARCH_KEY"):
        names.append("brave")
    names.append("duckduckgo")
    return names


def _build(name: str, country: str) -> SearchProvider | None:
    """One engine by name, or None when this machine cannot run it."""
    provider = PROVIDERS.get(name)
    if provider is None:
        return None
    if provider is SearxngSearch:
        url = os.environ.get("GROOVE_SEARXNG_URL", "").strip()
        return provider(base_url=url or SearxngSearch.base_url, country=country)
    if provider is DuckDuckGoSearch:
        return provider(country=country)
    key = os.environ.get(_OWN_KEY_VAR.get(name, ""), "") or os.environ.get("GROOVE_SEARCH_KEY", "")
    return provider(api_key=key, country=country) if key else None
