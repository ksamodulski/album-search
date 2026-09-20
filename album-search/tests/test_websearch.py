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
