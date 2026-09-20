"""FastAPI + HTMX front end.

Deliberately thin: it owns page state and HTML, and delegates every decision
to the core modules. The only real machinery here is the setup job, which has
to run for minutes while the page stays responsive.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..discovery import TOP_N, apply, discover, recalibrate
from ..normalize import normalize_lines
from ..registry import RECALIBRATE_AFTER, Registry
from ..fetching import BrowserFetcher, close_fetcher
from ..runtime import (
    build_search_provider,
    search_provider_hint,
    REGISTRY_PATH,
    SETUP_TTL,
    build_browser_fetcher,
    build_fetcher,
    browser_hosts_for,
)
from ..openweb import MAX_PAGES as WEB_MAX_PAGES
from ..openweb import SOURCE_NAME as WEB_SOURCE_NAME
from ..search import search_albums
from ..seeds import available_locations

HERE = Path(__file__).parent
app = FastAPI(title="groove-search")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals["recalibrate_after_days"] = RECALIBRATE_AFTER.days
# The open web is one source but many live requests, and it is reliably the
# slowest. The progress list says so rather than leaving a bare "…".
templates.env.globals["web_source_name"] = WEB_SOURCE_NAME
templates.env.globals["web_max_pages"] = WEB_MAX_PAGES

EXAMPLE = "Pet Fox - A face in your life\nAbase - Awakening\nWhitest Boy Alive"


@dataclass
class SetupJob:
    """Progress of a running setup, polled by the page while it works."""

    running: bool = False
    location: str = "PL"
    started_at: str = ""
    finished_at: str = ""
    lines: list[str] = field(default_factory=list)
    adopted: int = 0
    error: str | None = None

    @property
    def idle(self) -> bool:
        return not self.running and not self.finished_at


@dataclass
class SearchJob:
    """Progress of a running search, polled by the page while it works.

    A search runs for tens of seconds across a dozen sources, and a page that
    can only say "searching…" leaves the user unable to tell a slow shop from
    a stuck one. Every source reports itself here the moment it finishes.
    """

    running: bool = False
    started_at: str = ""
    finished_at: str = ""
    albums: list[str] = field(default_factory=list)
    # Every source that will report, in the order they are listed to the user.
    sources: list[str] = field(default_factory=list)
    # album -> source -> what that source found, filled in as answers arrive.
    done: dict[str, dict[str, str]] = field(default_factory=dict)
    results: list = field(default_factory=list)
    web_enabled: bool = True
    error: str | None = None

    def record(self, album: str, report) -> None:
        outcome = (
            report.error
            if not report.ok
            else f"{report.offers_found} offer{'' if report.offers_found == 1 else 's'}"
            if report.offers_found
            else "no match"
        )
        self.done.setdefault(album, {})[report.shop_name] = outcome

    def outcome(self, album: str, source: str) -> str:
        return self.done.get(album, {}).get(source, "")

    @property
    def answered(self) -> int:
        return sum(len(by_source) for by_source in self.done.values())

    @property
    def expected(self) -> int:
        return len(self.albums) * len(self.sources)


JOB = SetupJob()
SEARCH = SearchJob()


def load() -> Registry:
    return Registry.load(REGISTRY_PATH)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    registry = load()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "registry": registry,
            "job": JOB,
            "locations": available_locations(),
            "example": EXAMPLE,
            "top_n": TOP_N,
        },
    )


@app.post("/search", response_class=HTMLResponse)
async def search(request: Request, background: BackgroundTasks, albums: str = Form("")):
    registry = load()
    queries = normalize_lines(albums)
    if not queries:
        return templates.TemplateResponse(
            request,
            "partials/results.html",
            {"results": [], "registry": registry, "empty": True, "web_enabled": True, "web_hint": ""},
        )
    # One search at a time: the job is module-level state, so a second search
    # would overwrite the first's progress halfway through. Say so rather than
    # showing someone another search's results under their own album titles.
    busy = SEARCH.running and SEARCH.albums != [q.label for q in queries]
    if not SEARCH.running:
        provider_enabled = build_search_provider(registry) is not None
        SEARCH.running = True
        SEARCH.started_at = datetime.now(UTC).isoformat(timespec="seconds")
        SEARCH.finished_at = ""
        SEARCH.albums = [q.label for q in queries]
        SEARCH.sources = [s.name for s in registry.searchable] + (
            [WEB_SOURCE_NAME] if provider_enabled else []
        )
        SEARCH.done = {}
        SEARCH.results = []
        SEARCH.web_enabled = provider_enabled
        SEARCH.error = None
        background.add_task(_run_search, albums)
    return templates.TemplateResponse(
        request, "partials/searching.html", {"job": SEARCH, "registry": registry, "busy": busy}
    )


@app.get("/search/progress", response_class=HTMLResponse)
async def search_progress(request: Request):
    """The running search, or its results once there are some."""
    registry = load()
    if SEARCH.running:
        return templates.TemplateResponse(
            request, "partials/searching.html", {"job": SEARCH, "registry": registry}
        )
    return templates.TemplateResponse(
        request,
        "partials/results.html",
        {
            "results": SEARCH.results,
            "registry": registry,
            "empty": not SEARCH.results and not SEARCH.error,
            "web_enabled": SEARCH.web_enabled,
            "web_hint": search_provider_hint(),
            "error": SEARCH.error,
        },
    )


async def _run_search(albums: str) -> None:
    """The search itself, reporting into `SEARCH` as each source answers."""
    try:
        registry = load()
        queries = normalize_lines(albums)
        provider = build_search_provider(registry)
        fetcher = build_fetcher(browser_hosts=browser_hosts_for(registry))

        def progress(query, report) -> None:
            SEARCH.record(query.label, report)

        try:
            SEARCH.results = await search_albums(
                queries, registry, fetcher, provider=provider, on_progress=progress
            )
        finally:
            await close_fetcher(fetcher)
            await close_fetcher(provider)
        registry.save()
    except Exception as exc:  # a failed search must not wedge the page
        SEARCH.error = f"{type(exc).__name__}: {exc}"
    finally:
        SEARCH.running = False
        SEARCH.finished_at = datetime.now(UTC).isoformat(timespec="seconds")


@app.get("/shops", response_class=HTMLResponse)
async def shops(request: Request):
    return templates.TemplateResponse(request, "partials/shops.html", {"registry": load()})


@app.post("/shops/{shop_id}/recalibrate", response_class=HTMLResponse)
async def recalibrate_shop(request: Request, shop_id: str):
    """Re-learn one shop - the repair button next to a broken recipe."""
    registry = load()
    shop = registry.shop(shop_id)
    if shop is None:
        return HTMLResponse("<p class='error'>Unknown shop.</p>", status_code=404)
    if registry.repair_blocked(shop_id):
        # A recipe this young is not the reason the shop is failing; re-probing
        # it would cost minutes and deepen a rate limit. Force it from the CLI.
        return templates.TemplateResponse(
            request,
            "partials/shops.html",
            {
                "registry": registry,
                "flash": (
                    f"{shop.name}: recipe is newer than {RECALIBRATE_AFTER.days} days, so the shop "
                    f"is refusing us rather than having changed - left as not working. "
                    f"Force with: groove recalibrate {shop_id} --force"
                ),
            },
        )
    fetcher = build_fetcher(cache=False)  # a repair must see the live page
    browser = build_browser_fetcher(cache=False) if BrowserFetcher.available() else None
    try:
        score = await recalibrate(shop, fetcher, browser_fetcher=browser)
    finally:
        await close_fetcher(browser)
        await close_fetcher(fetcher)
    if score.usable:
        registry.adopt(shop, score.recipe, coverage=score.coverage)
    else:
        registry.health_of(shop_id).last_error = score.error
    registry.save()
    return templates.TemplateResponse(
        request, "partials/shops.html", {"registry": registry, "flash": score.summary}
    )


@app.post("/setup")
async def start_setup(background: BackgroundTasks, location: str = Form("PL")):
    if not JOB.running:
        JOB.running = True
        JOB.location = location
        JOB.started_at = datetime.now(UTC).isoformat(timespec="seconds")
        JOB.finished_at = ""
        JOB.lines = []
        JOB.adopted = 0
        JOB.error = None
        background.add_task(_run_setup, location)
    return RedirectResponse("/", status_code=303)


@app.get("/setup/progress", response_class=HTMLResponse)
async def setup_progress(request: Request):
    return templates.TemplateResponse(
        request, "partials/setup.html", {"job": JOB, "registry": load()}
    )


async def _run_setup(location: str) -> None:
    """Discovery is slow; it runs here and reports into `JOB` as it goes."""
    try:
        fetcher = build_fetcher(ttl=SETUP_TTL, cache=True)
        registry = load()

        def progress(score) -> None:
            mark = "ok" if score.usable else "skip"
            JOB.lines.append(f"{mark}|{score.summary}")

        browser = build_browser_fetcher() if BrowserFetcher.available() else None
        try:
            outcome = await discover(fetcher, location, browser_fetcher=browser, on_progress=progress)
        finally:
            await close_fetcher(browser)
            await close_fetcher(fetcher)
        registry = apply(outcome, registry, location)
        registry.path = REGISTRY_PATH
        registry.save()
        JOB.adopted = len(outcome.adopted)
    except Exception as exc:  # a failed setup must not wedge the page
        JOB.error = f"{type(exc).__name__}: {exc}"
    finally:
        JOB.running = False
        JOB.finished_at = datetime.now(UTC).isoformat(timespec="seconds")


@app.get("/healthz")
async def healthz():
    registry = load()
    return {
        "configured": registry.is_configured,
        "shops": len(registry.searchable),
        "broken": [s.id for s in registry.broken],
    }
