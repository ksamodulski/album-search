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
from ..library import DEFAULT_LIMIT as LIBRARY_LIMIT
from ..library import MAX_LIMIT as LIBRARY_MAX
from ..library import SERVICE_NAMES, config_for, display_name
from ..normalize import normalize_lines
from ..oauth import LoginFlow, OAuthError
from ..registry import RECALIBRATE_AFTER, Registry
from ..fetching import BrowserFetcher, close_fetcher
from ..runtime import (
    build_library,
    build_search_provider,
    configured_sources,
    library_hint,
    search_provider_hint,
    token_store,
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
templates.env.filters["service"] = display_name
templates.env.globals["library_limit"] = LIBRARY_LIMIT
templates.env.globals["library_max"] = LIBRARY_MAX

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
    # album -> source -> one line per engine behind that source. Only the open
    # web fills this in, and it is the difference between "the web found
    # nothing" and "Google served a captcha while Bing answered".
    engines: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    results: list = field(default_factory=list)
    web_enabled: bool = True
    error: str | None = None

    def record(self, album: str, report) -> None:
        if report.engines:
            self.engines.setdefault(album, {})[report.shop_name] = [
                e.summary for e in report.engines
            ]
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

    def engines_of(self, album: str, source: str) -> list[str]:
        return self.engines.get(album, {}).get(source, [])

    @property
    def answered(self) -> int:
        return sum(len(by_source) for by_source in self.done.values())

    @property
    def expected(self) -> int:
        return len(self.albums) * len(self.sources)


@dataclass
class LoginJob:
    """One streaming-service sign-in waiting on the user's browser.

    The consent page redirects to a loopback listener rather than back into
    this app, so that the CLI and the web UI share one redirect URI - one
    line to register with the service instead of one per surface. The cost is
    that the page has to be told when the listener caught the code, which is
    what this exists for.
    """

    source: str
    url: str = ""
    running: bool = False
    error: str | None = None
    # Held so the wait can be called off - by a second Connect click, by a
    # sign-out, or by the server shutting down. A login that cannot be
    # cancelled keeps `groove serve` alive for its full five-minute timeout
    # after Ctrl-C, which looks exactly like a hang.
    task: asyncio.Task | None = field(default=None, repr=False)

    def cancel(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.running = False


JOB = SetupJob()
SEARCH = SearchJob()
LOGINS: dict[str, LoginJob] = {}


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
            "library_sources": configured_sources(),
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


@app.get("/library/{source}", response_class=HTMLResponse)
async def library(request: Request, source: str, limit: int = LIBRARY_LIMIT):
    """The albums recently added to one account - or the way to connect it."""
    return await _library_panel(request, source, limit)


@app.post("/library/{source}/connect", response_class=HTMLResponse)
async def library_connect(request: Request, source: str):
    """Start the consent flow and hand the user the link to open."""
    config = config_for(source)
    if config is None:
        return await _library_panel(request, source, LIBRARY_LIMIT)
    # A second click replaces the first attempt, which otherwise sits on the
    # loopback port and makes the new one fail to bind.
    previous = LOGINS.get(source)
    if previous is not None:
        previous.cancel()
    job = LoginJob(source=source, running=True)
    flow = LoginFlow(config=config)
    job.url = flow.url
    LOGINS[source] = job
    job.task = asyncio.create_task(_run_login(flow, job))
    return await _library_panel(request, source, LIBRARY_LIMIT)


@app.post("/library/{source}/logout", response_class=HTMLResponse)
async def library_logout(request: Request, source: str):
    token_store().forget(source)
    job = LOGINS.pop(source, None)
    if job is not None:
        job.cancel()
    return await _library_panel(request, source, LIBRARY_LIMIT)


@app.post("/library/pick", response_class=HTMLResponse)
async def library_pick(request: Request, source: str = Form(""), picked: list[str] = Form(default=[])):
    """Put the ticked albums in the search box - and stop there, deliberately.

    Importing a library must not start a search on its own: the user still
    reads the lines, edits them, adds a "(vinyl)" and presses the button. A
    list from a streaming account is a suggestion, not an instruction.
    """
    lines = [line.strip() for line in picked if line.strip()]
    flash = (
        f"{len(lines)} album{'' if len(lines) == 1 else 's'} added from {display_name(source)}"
        if lines
        else "Nothing was ticked, so the search box is unchanged."
    )
    return templates.TemplateResponse(
        request,
        "partials/search_form.html",
        {"albums_text": "\n".join(lines) if lines else EXAMPLE, "flash": flash},
    )


async def _library_panel(request: Request, source: str, limit: int):
    """One rendering of the import panel, in whichever state that account is in."""
    context: dict = {
        "source": source,
        "limit": max(1, min(limit, LIBRARY_MAX)),
        "albums": [],
        "hint": library_hint(),
        "error": None,
        "login": LOGINS.get(source),
    }
    if config_for(source) is None:
        context["state"] = "unconfigured"
        return templates.TemplateResponse(request, "partials/library.html", context)

    job = LOGINS.get(source)
    if job is not None and job.running:
        context["state"] = "connecting"
        return templates.TemplateResponse(request, "partials/library.html", context)

    registry = load()
    library_source = build_library(source, registry)
    if library_source is None or not library_source.signed_in:
        context["state"] = "disconnected"
        context["error"] = job.error if job else None
        return templates.TemplateResponse(request, "partials/library.html", context)

    try:
        context["albums"] = await library_source.recent(context["limit"])
    finally:
        await library_source.aclose()
    context["error"] = library_source.last_error
    # A reason with no albums is a failure to report; a reason *with* albums is
    # a partial answer, and hiding the second would make a short list look whole.
    context["state"] = "albums" if context["albums"] else "empty"
    return templates.TemplateResponse(request, "partials/library.html", context)


async def _run_login(flow: LoginFlow, job: LoginJob) -> None:
    """Wait for the loopback callback, then keep the token."""
    try:
        await flow.complete(token_store())
    except OAuthError as exc:
        job.error = str(exc)
    except asyncio.CancelledError:
        # Called off deliberately: another attempt, a sign-out, or shutdown.
        raise
    except Exception as exc:  # a failed login must not wedge the page
        job.error = f"{type(exc).__name__}: {exc}"
    finally:
        job.running = False


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
