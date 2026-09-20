"""The seam between groove-search and the open web.

Everything that needs a page asks a `Fetcher` for one. That keeps politeness
(robots.txt, rate limits, retries) in a single place and lets tests, the
calibrator and the live searcher swap in different adapters without any of
them knowing how a page is really obtained.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable
from typing import Protocol

import httpx

USER_AGENT = (
    "groove-search/0.1 (personal record-price comparison; "
    "+https://github.com/groove-search; contact: local user)"
)


@dataclass(frozen=True, slots=True)
class Page:
    """A fetched document, or the reason there isn't one."""

    url: str
    status: int
    html: str
    elapsed_ms: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300 and bool(self.html)


class Fetcher(Protocol):
    """Anything that can turn a URL into a `Page`.

    Implementations must never raise for network-level problems: a failure is
    a `Page` with `error` set, so one dead shop cannot abort a whole search.
    """

    async def get(self, url: str) -> Page: ...


async def close_fetcher(fetcher: Fetcher | None) -> None:
    """Release whatever a fetcher holds open - sockets, or a whole browser.

    Skipping this leaves Playwright's subprocess dangling and the process
    hangs at exit, so whoever builds a fetcher must close it.
    """
    if fetcher is None:
        return
    closer = getattr(fetcher, "aclose", None)
    if closer is not None:
        await closer()


@dataclass
class RobotsGate:
    """robots.txt policy, cached per host.

    Lives outside any one adapter because politeness must not depend on which
    adapter happens to fetch a page - a browser is still a robot.
    """

    obey: bool = True
    timeout: float = 10.0
    _rules: dict[str, urllib.robotparser.RobotFileParser | None] = field(
        default_factory=dict, init=False, repr=False
    )
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False, repr=False)

    async def allowed(self, url: str) -> bool:
        if not self.obey:
            return True
        host = urllib.parse.urlsplit(url).netloc
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            if host not in self._rules:
                self._rules[host] = await self._load(url)
        parser = self._rules[host]
        # No reachable robots.txt means no stated restriction.
        return True if parser is None else parser.can_fetch(USER_AGENT, url)

    async def _load(self, url: str):
        parts = urllib.parse.urlsplit(url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=self.timeout, headers={"User-Agent": USER_AGENT}
            ) as client:
                response = await client.get(robots_url)
            if not response.is_success:
                return None
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(response.text.splitlines())
            return parser
        except httpx.HTTPError:
            return None


@dataclass
class HttpFetcher:
    """Polite HTTP adapter: per-host rate limiting, retries, robots.txt.

    `delay` is the minimum gap between two requests to the same host, so a
    search across ten shops stays fast while no single shop gets hammered.
    """

    timeout: float = 20.0
    delay: float = 1.0
    retries: int = 2
    obey_robots: bool = True
    max_concurrency: int = 8
    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)
    _last_hit: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _host_locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False, repr=False)
    robots: RobotsGate = field(default_factory=RobotsGate)
    _gate: asyncio.Semaphore | None = field(default=None, init=False, repr=False)

    async def get(self, url: str) -> Page:
        client = self._ensure_client()
        if self._gate is None:
            self._gate = asyncio.Semaphore(self.max_concurrency)
        host = urllib.parse.urlsplit(url).netloc

        self.robots.obey = self.obey_robots
        if not await self.robots.allowed(url):
            return Page(url=url, status=0, html="", error="blocked by robots.txt")

        async with self._gate:
            await self._wait_turn(host)
            started = time.monotonic()
            last_error = "unknown error"
            for attempt in range(self.retries + 1):
                try:
                    response = await client.get(url)
                    # Back off and retry when a shop asks us to slow down,
                    # rather than writing the shop off as unscrapeable.
                    if response.status_code in (429, 503) and attempt < self.retries:
                        await asyncio.sleep(_retry_after(response, attempt, self.delay))
                        continue
                    return Page(
                        url=str(response.url),
                        status=response.status_code,
                        html=response.text,
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                        error=None if response.is_success else f"HTTP {response.status_code}",
                    )
                except httpx.HTTPError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    if attempt < self.retries:
                        await asyncio.sleep(0.5 * (attempt + 1))
            return Page(
                url=url,
                status=0,
                html="",
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=last_error,
            )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                follow_redirects=True,
                timeout=self.timeout,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept-Language": "pl,en;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                },
            )
        return self._client

    async def _wait_turn(self, host: str) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            gap = time.monotonic() - self._last_hit.get(host, 0.0)
            if gap < self.delay:
                await asyncio.sleep(self.delay - gap)
            self._last_hit[host] = time.monotonic()


def _retry_after(response: httpx.Response, attempt: int, delay: float) -> float:
    """Honour a Retry-After header, else back off exponentially."""
    header = response.headers.get("retry-after")
    if header:
        try:
            return min(float(header), 30.0)
        except ValueError:
            pass
    return min(delay * (2 ** (attempt + 1)), 15.0)


@dataclass
class CachingFetcher:
    """Disk cache in front of another fetcher.

    Calibration re-reads the same search pages many times while it infers a
    recipe; without this it would be both slow and rude.
    """

    inner: Fetcher
    directory: Path
    ttl_seconds: float = 24 * 3600

    async def get(self, url: str) -> Page:
        path = self._path(url)
        if path.exists() and (time.time() - path.stat().st_mtime) < self.ttl_seconds:
            return Page(url=url, status=200, html=path.read_text(encoding="utf-8"))
        page = await self.inner.get(url)
        if page.ok:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(page.html, encoding="utf-8")
        return page

    async def aclose(self) -> None:
        await close_fetcher(self.inner)

    def _path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        host = urllib.parse.urlsplit(url).netloc or "unknown"
        return self.directory / host / f"{digest}.html"


@dataclass
class FakeFetcher:
    """Test adapter: serves canned HTML and records what was asked for.

    `responder` lets a test model a *real* search endpoint - one whose answer
    depends on the query - which fixed pages cannot express.
    """

    pages: dict[str, str] = field(default_factory=dict)
    requested: list[str] = field(default_factory=list)
    default: str | None = None
    responder: Callable[[str], str | None] | None = None
    miss_status: int = 404
    miss_error: str = "HTTP 404"

    async def get(self, url: str) -> Page:
        self.requested.append(url)
        if self.responder is not None:
            html = self.responder(url)
        else:
            html = self.pages.get(url, self.default)
        if html is None:
            return Page(url=url, status=self.miss_status, html="", error=self.miss_error)
        return Page(url=url, status=200, html=html)


@dataclass
class RoutingFetcher:
    """Sends some hosts to a browser and everything else over plain HTTP.

    Shops that render their results in JavaScript need a real browser, but
    they are the minority and a browser is ~50x slower. Routing by host keeps
    `search_albums` unaware that two adapters exist at all.
    """

    default: Fetcher
    browser: Fetcher
    browser_hosts: frozenset[str] = frozenset()

    async def get(self, url: str) -> Page:
        host = urllib.parse.urlsplit(url).netloc.lower()
        bare = host[4:] if host.startswith("www.") else host
        if host in self.browser_hosts or bare in self.browser_hosts:
            return await self.browser.get(url)
        return await self.default.get(url)

    async def aclose(self) -> None:
        await close_fetcher(self.default)
        await close_fetcher(self.browser)


@dataclass
class BrowserFetcher:
    """Playwright adapter for shops that render their results in JavaScript.

    The browser is started once and reused: launching Chromium per request
    costs seconds, which would make calibration unusable. Optional - importing
    Playwright is deferred so the package works without it.
    """

    timeout: float = 30.0
    wait_until: str = "domcontentloaded"
    settle_ms: int = 1200
    max_concurrency: int = 3
    robots: RobotsGate = field(default_factory=RobotsGate)
    _playwright: object | None = field(default=None, init=False, repr=False)
    _browser: object | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _gate: asyncio.Semaphore | None = field(default=None, init=False, repr=False)

    @staticmethod
    def available() -> bool:
        try:
            import playwright.async_api  # noqa: F401
        except ImportError:
            return False
        return True

    async def get(self, url: str) -> Page:
        started = time.monotonic()
        if not await self.robots.allowed(url):
            return Page(url=url, status=0, html="", error="blocked by robots.txt")
        try:
            browser = await self._ensure_browser()
        except Exception as exc:
            return Page(url=url, status=0, html="", error=f"browser unavailable: {exc}")

        if self._gate is None:
            self._gate = asyncio.Semaphore(self.max_concurrency)
        async with self._gate:
            context = None
            try:
                context = await browser.new_context(
                    user_agent=USER_AGENT, locale="pl-PL", viewport={"width": 1366, "height": 900}
                )
                page = await context.new_page()
                response = await page.goto(url, wait_until=self.wait_until, timeout=self.timeout * 1000)
                # Give client-rendered results a moment to appear.
                await page.wait_for_timeout(self.settle_ms)
                html = await page.content()
                status = response.status if response else 200
                return Page(
                    url=page.url,
                    status=status,
                    html=html,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    error=None if 200 <= status < 300 else f"HTTP {status}",
                )
            except Exception as exc:  # playwright raises a wide family of errors
                return Page(url=url, status=0, html="", error=f"{type(exc).__name__}: {exc}")
            finally:
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass

    async def _ensure_browser(self):
        async with self._lock:
            if self._browser is None:
                from playwright.async_api import async_playwright

                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(headless=True)
            return self._browser

    async def aclose(self) -> None:
        async with self._lock:
            if self._browser is not None:
                await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None
