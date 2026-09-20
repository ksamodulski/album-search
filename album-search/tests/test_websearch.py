"""Merging several engines into one source.

Offline: every engine here is a fake or a deliberately broken stand-in, so
what is under test is the merge itself - order, deduplication, and what
happens when one engine fails while another answers.
"""

import asyncio
import os

import pytest

from groove_search.websearch import (
    DuckDuckGoSearch,
    FakeSearch,
    MergedSearch,
    SearchHit,
    SearxngSearch,
    from_env,
)

PHRASE = "pet fox vinyl buy"


def hits(*urls: str) -> list[SearchHit]:
    return [SearchHit(title=u, url=u) for u in urls]


def engine(*urls: str) -> FakeSearch:
    return FakeSearch(default=hits(*urls))


class Broken:
    """An engine that raises rather than returning empty, against the protocol."""

    last_error = "engine exploded"

    async def search(self, phrase, *, limit=12):
        raise RuntimeError("boom")


class Slow:
    """An engine that never answers in time - a throttled DuckDuckGo, in effect."""

    last_error = "too slow"

    async def search(self, phrase, *, limit=12):
        await asyncio.sleep(30)
        return hits("https://slow.example/p/1")


@pytest.mark.anyio
async def test_merges_round_robin_not_concatenated():
    """Each engine's best result outranks every engine's second."""
    merged = MergedSearch((engine("https://a.example/1", "https://a.example/2"),
                           engine("https://b.example/1", "https://b.example/2")))

    found = [h.url for h in await merged.search(PHRASE)]

    assert found == [
        "https://a.example/1",
        "https://b.example/1",
        "https://a.example/2",
        "https://b.example/2",
    ]


@pytest.mark.anyio
async def test_a_record_only_one_engine_knows_still_surfaces():
    """The whole point: coverage differs, so the union is what matters."""
    merged = MergedSearch((engine("https://common.example/1"),
                           engine("https://common.example/1", "https://rare.example/niche")))

    found = [h.url for h in await merged.search(PHRASE)]

    assert "https://rare.example/niche" in found


@pytest.mark.anyio
async def test_the_same_page_from_two_engines_appears_once():
    merged = MergedSearch((engine("https://shop.example/p/1"),
                           engine("https://www.shop.example/p/1/")))

    assert len(await merged.search(PHRASE)) == 1


@pytest.mark.anyio
async def test_a_query_string_still_distinguishes_products():
    """Some shops address a product entirely through the query string."""
    merged = MergedSearch((engine("https://shop.example/p?id=1"),
                           engine("https://shop.example/p?id=2")))

    assert len(await merged.search(PHRASE)) == 2


@pytest.mark.anyio
async def test_one_engine_failing_does_not_lose_the_other():
    merged = MergedSearch((Broken(), engine("https://good.example/p/1")))

    found = [h.url for h in await merged.search(PHRASE)]

    assert found == ["https://good.example/p/1"]


@pytest.mark.anyio
async def test_a_working_engine_means_no_error_is_reported():
    """An outage beside good results is not the user's problem to act on."""
    merged = MergedSearch((Broken(), engine("https://good.example/p/1")))

    await merged.search(PHRASE)

    assert merged.last_error is None


@pytest.mark.anyio
async def test_every_engine_failing_reports_all_their_reasons():
    merged = MergedSearch((Broken(), FakeSearch(default=[])))
    merged.providers[1].last_error = "rate limited"

    await merged.search(PHRASE)

    assert "engine exploded" in merged.last_error
    assert "rate limited" in merged.last_error


@pytest.mark.anyio
async def test_a_straggler_does_not_hold_up_engines_that_answered():
    """A throttled engine sleeps 15s and then returns nothing anyway."""
    merged = MergedSearch((Slow(), engine("https://fast.example/p/1")), patience=0.05)

    found = [h.url for h in await merged.search(PHRASE)]

    assert found == ["https://fast.example/p/1"]


@pytest.mark.anyio
async def test_closing_the_merge_closes_every_engine():
    closed = []

    class Closeable(FakeSearch):
        async def aclose(self):
            closed.append(self)

    merged = MergedSearch((Closeable(), Closeable()))
    await merged.aclose()

    assert len(closed) == 2


# --- from_env: which engines a machine ends up asking ------------------------


@pytest.fixture
def clean_env(monkeypatch):
    for var in ("GROOVE_SEARCH_PROVIDER", "GROOVE_SEARXNG_URL", "GROOVE_SEARCH_KEY",
                "BRAVE_API_KEY", "SERPER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_a_bare_machine_still_gets_the_keyless_engine(clean_env):
    assert isinstance(from_env(), DuckDuckGoSearch)


def test_searxng_is_merged_with_duckduckgo_rather_than_replacing_it(clean_env):
    """The regression this feature exists to prevent: one engine deciding."""
    clean_env.setenv("GROOVE_SEARXNG_URL", "http://127.0.0.1:8888")

    provider = from_env()

    assert isinstance(provider, MergedSearch)
    assert [type(p) for p in provider.providers] == [SearxngSearch, DuckDuckGoSearch]


def test_naming_one_engine_pins_it(clean_env):
    """The escape hatch for telling an engine's blind spot from a dead record."""
    clean_env.setenv("GROOVE_SEARXNG_URL", "http://127.0.0.1:8888")
    clean_env.setenv("GROOVE_SEARCH_PROVIDER", "searxng")

    assert isinstance(from_env(), SearxngSearch)


def test_a_named_list_merges_exactly_those(clean_env):
    clean_env.setenv("GROOVE_SEARXNG_URL", "http://127.0.0.1:8888")
    clean_env.setenv("GROOVE_SEARCH_PROVIDER", "duckduckgo,searxng")

    provider = from_env()

    assert [type(p) for p in provider.providers] == [DuckDuckGoSearch, SearxngSearch]


def test_none_still_switches_the_open_web_off(clean_env):
    clean_env.setenv("GROOVE_SEARXNG_URL", "http://127.0.0.1:8888")
    clean_env.setenv("GROOVE_SEARCH_PROVIDER", "none")

    assert from_env() is None


def test_each_keyed_engine_gets_its_own_key(clean_env):
    """Built together, they must not be handed each other's key."""
    clean_env.setenv("BRAVE_API_KEY", "brave-key")
    clean_env.setenv("SERPER_API_KEY", "serper-key")

    keys = {type(p).__name__: getattr(p, "api_key", None) for p in from_env().providers}

    assert keys["BraveSearch"] == "brave-key"
    assert keys["SerperSearch"] == "serper-key"


def test_the_generic_key_still_works_alone(clean_env):
    clean_env.setenv("GROOVE_SEARCH_KEY", "one-key")

    provider = from_env()

    assert [type(p).__name__ for p in provider.providers] == ["BraveSearch", "DuckDuckGoSearch"]


# --- Which engine answered, and what it said -------------------------------
#
# A merged search shows a union, which hides the one thing a surprised user
# needs: whether an engine found nothing or was never really asked. These
# cover the per-engine accounting that makes the union readable.


@pytest.mark.anyio
async def test_every_hit_carries_the_engine_that_found_it():
    """An offer's provenance starts here: no tag on the hit, none anywhere."""
    merged = MergedSearch((FakeSearch(name="alpha", default=hits("https://a.example/1")),
                           FakeSearch(name="beta", default=hits("https://b.example/1"))))

    found = await merged.search(PHRASE)

    assert {h.url: h.engine for h in found} == {
        "https://a.example/1": "alpha",
        "https://b.example/1": "beta",
    }


@pytest.mark.anyio
async def test_reports_name_every_engine_asked_with_its_tally():
    merged = MergedSearch((FakeSearch(name="alpha", default=hits("https://a.example/1", "https://a.example/2")),
                           FakeSearch(name="beta", default=hits("https://b.example/1"))))

    await merged.search(PHRASE)

    assert [(r.name, r.hits, r.note) for r in merged.reports()] == [
        ("alpha", 2, None),
        ("beta", 1, None),
    ]


@pytest.mark.anyio
async def test_a_refusing_engine_is_named_beside_the_one_that_worked():
    """The whole point: a good result must not hide a throttled engine."""
    refused = FakeSearch(name="throttled")
    refused.last_error = "rate-limiting us"
    merged = MergedSearch((FakeSearch(name="working", default=hits("https://a.example/1")), refused))

    await merged.search(PHRASE)
    reports = {r.name: r for r in merged.reports()}

    assert reports["working"].ok and reports["working"].hits == 1
    assert not reports["throttled"].ok
    assert "rate-limiting" in reports["throttled"].note
    # ... and the merged search itself stays quiet, because a user with
    # results in hand can do nothing about another engine's outage.
    assert merged.last_error is None


@pytest.mark.anyio
async def test_an_abandoned_engine_says_so_rather_than_reporting_nothing():
    """A straggler is dropped for speed; silence about it would be a lie."""
    merged = MergedSearch((engine("https://a.example/1"), Slow()), patience=0.05)

    await merged.search(PHRASE)
    slow = [r for r in merged.reports() if "Slow" in r.name or "too slow" in (r.note or "")]

    assert slow, "the abandoned engine must appear in the reports"
    assert "abandoned" in slow[0].note


@pytest.mark.anyio
async def test_searxng_reports_its_upstream_engines_not_itself(monkeypatch):
    """SearXNG is a dozen engines wearing one coat; the coat is not the fact."""
    payload = {
        "results": [
            {"url": "https://a.example/1", "title": "one", "engine": "bing"},
            {"url": "https://a.example/2", "title": "two", "engine": "bing"},
            {"url": "https://b.example/1", "title": "three", "engine": "qwant"},
        ],
        "unresponsive_engines": [["google", "CAPTCHA"], ["brave", "too many requests"]],
    }
    provider = SearxngSearch()
    monkeypatch.setattr(provider, "_ensure_client", lambda: _Canned(payload))

    found = await provider.search(PHRASE)

    assert [h.engine for h in found] == ["searxng/bing", "searxng/bing", "searxng/qwant"]
    assert [(r.name, r.hits, r.note) for r in provider.reports()] == [
        ("searxng/bing", 2, None),
        ("searxng/qwant", 1, None),
        ("searxng/google", 0, "CAPTCHA"),
        ("searxng/brave", 0, "too many requests"),
    ]


@pytest.mark.anyio
async def test_an_empty_searxng_blames_its_engines_by_name():
    """"Nothing for sale" and "every engine refused" must never read alike."""
    payload = {"results": [], "unresponsive_engines": [["google", "CAPTCHA"]]}
    provider = SearxngSearch()
    provider._ensure_client = lambda: _Canned(payload)  # type: ignore[method-assign]

    assert await provider.search(PHRASE) == []
    assert "CAPTCHA" in provider.last_error


class _Canned:
    """The smallest thing that behaves like the httpx client SearXNG uses."""

    def __init__(self, payload):
        self.payload = payload

    async def get(self, url, params=None):
        return _CannedResponse(self.payload)


class _CannedResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload
