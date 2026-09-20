"""Reading a streaming library, and turning it into lines this app can search.

Offline: every service call is an `httpx.MockTransport`, so what is under test
is our reading of the payloads and - just as much - what happens when the
service refuses us, which is the state a user is most likely to hit.
"""

import time

import httpx
import pytest

from groove_search.domain import SavedAlbum
from groove_search.library import (
    FakeLibrary,
    SpotifyLibrary,
    TidalLibrary,
    as_lines,
    build_library,
    configured_sources,
    parse_picks,
)
from groove_search.normalize import normalize_query
from groove_search.oauth import OAuthConfig, Token, TokenStore

SPOTIFY = OAuthConfig(
    service="spotify",
    client_id="cid",
    authorize_url="https://accounts.example/authorize",
    token_url="https://accounts.example/token",
    scopes=("user-library-read",),
    label="Spotify",
)
TIDAL = OAuthConfig(
    service="tidal",
    client_id="cid",
    authorize_url="https://login.example/authorize",
    token_url="https://auth.example/token",
    scopes=("user.read", "collection.read"),
    label="TIDAL",
)


def signed_in(tmp_path, service: str = "spotify") -> TokenStore:
    store = TokenStore(directory=tmp_path / "tokens")
    store.save(service, Token("at", "rt", time.time() + 3600))
    return store


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def spotify_item(name: str, artist: str, added_at: str, album_id: str = "a1") -> dict:
    return {
        "added_at": added_at,
        "album": {
            "id": album_id,
            "name": name,
            "artists": [{"name": artist}],
            "images": [{"url": f"https://i.example/{album_id}-small.jpg"}, {"url": f"https://i.example/{album_id}.jpg"}],
            "external_urls": {"spotify": f"https://open.spotify.com/album/{album_id}"},
        },
    }


# --- Spotify --------------------------------------------------------------


@pytest.mark.anyio
async def test_reads_saved_albums(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer at"
        return httpx.Response(200, json={"items": [spotify_item("Untrue", "Burial", "2026-09-01T10:00:00Z")], "next": None})

    async with client(handler) as http:
        library = SpotifyLibrary(config=SPOTIFY, store=signed_in(tmp_path), client=http)
        albums = await library.recent(10)

    assert albums == [
        SavedAlbum(
            source="spotify",
            id="a1",
            artist="Burial",
            title="Untrue",
            added_at="2026-09-01T10:00:00Z",
            url="https://open.spotify.com/album/a1",
            image_url="https://i.example/a1.jpg",
        )
    ]
    assert library.last_error is None


@pytest.mark.anyio
async def test_newest_first_is_enforced_not_assumed(tmp_path):
    """Spotify does not document the order, so relying on it would be a bug."""
    items = [
        spotify_item("Older", "A", "2026-01-01T00:00:00Z", "a1"),
        spotify_item("Newest", "B", "2026-09-19T00:00:00Z", "a2"),
        spotify_item("Middle", "C", "2026-05-05T00:00:00Z", "a3"),
    ]

    async with client(lambda r: httpx.Response(200, json={"items": items, "next": None})) as http:
        albums = await SpotifyLibrary(config=SPOTIFY, store=signed_in(tmp_path), client=http).recent(10)

    assert [a.title for a in albums] == ["Newest", "Middle", "Older"]


@pytest.mark.anyio
async def test_pages_past_the_fifty_the_api_will_give(tmp_path):
    asked: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        asked.append(params)
        offset = int(params["offset"])
        items = [
            spotify_item(f"Album {n}", "Artist", f"2026-09-{(n % 28) + 1:02d}T00:00:00Z", f"a{n}")
            for n in range(offset, offset + 50)
        ]
        return httpx.Response(200, json={"items": items, "next": "…" if offset < 50 else None})

    async with client(handler) as http:
        albums = await SpotifyLibrary(config=SPOTIFY, store=signed_in(tmp_path), client=http).recent(60)

    assert len(albums) == 60
    assert [a["offset"] for a in asked] == ["0", "50"]
    assert asked[0]["limit"] == "50", "50 is the API maximum; asking for more is an error"


@pytest.mark.anyio
async def test_a_limit_beyond_the_ceiling_cannot_walk_the_whole_library(tmp_path):
    pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        pages.append(1)
        offset = int(dict(request.url.params)["offset"])
        return httpx.Response(
            200,
            json={
                "items": [spotify_item(f"A{n}", "X", "2026-09-01T00:00:00Z", f"a{n}") for n in range(offset, offset + 50)],
                "next": "more",
            },
        )

    async with client(handler) as http:
        albums = await SpotifyLibrary(config=SPOTIFY, store=signed_in(tmp_path), client=http).recent(10_000)

    assert len(albums) == 200, "MAX_LIMIT caps it"
    assert len(pages) == 4


@pytest.mark.anyio
async def test_a_compilation_searches_under_its_headline_artist(tmp_path):
    item = spotify_item("Now That's What I Call Jazz", "Miles Davis", "2026-09-01T00:00:00Z")
    item["album"]["artists"].append({"name": "John Coltrane"})

    async with client(lambda r: httpx.Response(200, json={"items": [item], "next": None})) as http:
        albums = await SpotifyLibrary(config=SPOTIFY, store=signed_in(tmp_path), client=http).recent(5)

    assert albums[0].artist == "Miles Davis"


# --- failure is a reason, never an exception ------------------------------


@pytest.mark.anyio
async def test_not_signed_in_is_a_reason_not_an_empty_library(tmp_path):
    library = SpotifyLibrary(config=SPOTIFY, store=TokenStore(directory=tmp_path))

    assert await library.recent(10) == []
    assert "not signed in" in library.last_error


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response,expected",
    [
        (httpx.Response(401, json={"error": {"message": "The access token expired"}}), "sign in again"),
        (httpx.Response(403, json={"error": {"message": "Insufficient client scope"}}), "scopes"),
        (httpx.Response(429, headers={"Retry-After": "30"}, json={}), "rate-limiting"),
        (httpx.Response(503, json={"error": {"message": "Service unavailable"}}), "Service unavailable"),
    ],
)
async def test_a_refusal_is_reported_in_words_a_user_can_act_on(tmp_path, response, expected):
    async with client(lambda r: response) as http:
        library = SpotifyLibrary(config=SPOTIFY, store=signed_in(tmp_path), client=http)
        albums = await library.recent(10)

    assert albums == []
    assert expected in library.last_error


@pytest.mark.anyio
async def test_a_dead_network_never_raises_into_the_caller(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with client(handler) as http:
        library = SpotifyLibrary(config=SPOTIFY, store=signed_in(tmp_path), client=http)

        assert await library.recent(10) == []
    assert "could not reach Spotify" in library.last_error


@pytest.mark.anyio
async def test_a_page_that_fails_halfway_keeps_what_came_before_it(tmp_path):
    """Half a list with a reason beside it beats nothing with the same reason."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] > 1:
            return httpx.Response(429, json={})
        items = [spotify_item(f"A{n}", "X", f"2026-09-{n + 1:02d}T00:00:00Z", f"a{n}") for n in range(50)]
        return httpx.Response(200, json={"items": items, "next": "more"})

    async with client(handler) as http:
        library = SpotifyLibrary(config=SPOTIFY, store=signed_in(tmp_path), client=http)
        albums = await library.recent(60)

    assert len(albums) == 50
    assert "rate-limiting" in library.last_error


# --- TIDAL ----------------------------------------------------------------


def tidal_handler(payloads: dict[str, httpx.Response]):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept"] == "application/vnd.api+json"
        for fragment, response in payloads.items():
            if fragment in str(request.url):
                return response
        return httpx.Response(404, json={"errors": [{"detail": f"no stub for {request.url}"}]})

    return handler


@pytest.mark.anyio
async def test_reads_the_tidal_collection_through_its_relationships(tmp_path):
    collection = {
        "data": [
            {"type": "albums", "id": "101", "meta": {"addedAt": "2026-09-10T08:00:00Z"}},
            {"type": "albums", "id": "102", "meta": {"addedAt": "2026-09-18T08:00:00Z"}},
        ],
        "included": [
            {
                "type": "albums",
                "id": "101",
                "attributes": {"title": "Discovery", "externalLinks": [{"href": "https://tidal.com/album/101"}]},
                "relationships": {"artists": {"data": [{"type": "artists", "id": "7"}]}},
            },
            {
                "type": "albums",
                "id": "102",
                "attributes": {"title": "In Rainbows"},
                "relationships": {"artists": {"data": [{"type": "artists", "id": "8"}]}},
            },
            {"type": "artists", "id": "7", "attributes": {"name": "Daft Punk"}},
            {"type": "artists", "id": "8", "attributes": {"name": "Radiohead"}},
        ],
        "links": {},
    }
    handler = tidal_handler(
        {
            "/users/me": httpx.Response(200, json={"data": {"id": "u-1", "type": "users"}}),
            "/userCollections/u-1/relationships/albums": httpx.Response(200, json=collection),
        }
    )

    async with client(handler) as http:
        library = TidalLibrary(config=TIDAL, store=signed_in(tmp_path, "tidal"), client=http, country="PL")
        albums = await library.recent(10)

    assert [(a.artist, a.title) for a in albums] == [("Radiohead", "In Rainbows"), ("Daft Punk", "Discovery")]
    assert albums[1].url == "https://tidal.com/album/101"
    assert library.last_error is None


@pytest.mark.anyio
async def test_a_collection_that_never_says_when_keeps_the_apis_own_order(tmp_path):
    """An invented "recent" would be worse than the order the service gave."""
    collection = {
        "data": [{"type": "albums", "id": "1"}, {"type": "albums", "id": "2"}],
        "included": [
            {"type": "albums", "id": "1", "attributes": {"title": "First"}},
            {"type": "albums", "id": "2", "attributes": {"title": "Second"}},
        ],
    }
    handler = tidal_handler(
        {
            "/users/me": httpx.Response(200, json={"data": {"id": "u-1"}}),
            "/relationships/albums": httpx.Response(200, json=collection),
        }
    )

    async with client(handler) as http:
        albums = await TidalLibrary(config=TIDAL, store=signed_in(tmp_path, "tidal"), client=http).recent(10)

    assert [a.title for a in albums] == ["First", "Second"]


@pytest.mark.anyio
async def test_follows_the_cursor_to_the_next_page(tmp_path):
    first = {
        "data": [{"type": "albums", "id": "1", "meta": {"addedAt": "2026-09-01T00:00:00Z"}}],
        "included": [{"type": "albums", "id": "1", "attributes": {"title": "One"}}],
        "links": {"next": "/v2/userCollections/u-1/relationships/albums?page%5Bcursor%5D=abc"},
    }
    second = {
        "data": [{"type": "albums", "id": "2", "meta": {"addedAt": "2026-09-02T00:00:00Z"}}],
        "included": [{"type": "albums", "id": "2", "attributes": {"title": "Two"}}],
        "links": {},
    }
    pages = [httpx.Response(200, json=first), httpx.Response(200, json=second)]

    def handler(request: httpx.Request) -> httpx.Response:
        if "/users/me" in str(request.url):
            return httpx.Response(200, json={"data": {"id": "u-1"}})
        return pages.pop(0)

    async with client(handler) as http:
        albums = await TidalLibrary(config=TIDAL, store=signed_in(tmp_path, "tidal"), client=http).recent(10)

    assert [a.title for a in albums] == ["Two", "One"]
    assert not pages


@pytest.mark.anyio
async def test_a_token_that_does_not_say_who_it_belongs_to(tmp_path):
    handler = tidal_handler({"/users/me": httpx.Response(200, json={"data": {}})})

    async with client(handler) as http:
        library = TidalLibrary(config=TIDAL, store=signed_in(tmp_path, "tidal"), client=http)

        assert await library.recent(10) == []
    assert "who the token belongs to" in library.last_error


# --- the seam and the picking --------------------------------------------


def test_an_album_from_a_library_folds_exactly_like_a_typed_line():
    """Both sides of a comparison must fold alike - see finding 15."""
    saved = SavedAlbum(source="spotify", id="1", artist="Radiohead", title="In Rainbows (Deluxe Edition)")

    from_library = normalize_query(saved.query_line)
    typed = normalize_query("Radiohead - In Rainbows")

    assert from_library.artist_tokens == typed.artist_tokens
    assert from_library.title_tokens == typed.title_tokens


def test_a_format_hint_in_a_saved_title_still_works():
    saved = SavedAlbum(source="tidal", id="1", artist="Abase", title="Awakening (Vinyl)")

    assert normalize_query(saved.query_line).format.value == "vinyl"


def test_an_album_with_no_artist_is_still_searchable():
    """TIDAL does not always resolve the performer; a title alone is honest."""
    saved = SavedAlbum(source="tidal", id="1", artist="", title="Bookends")

    assert saved.query_line == "Bookends"
    assert normalize_query(saved.query_line).artist == "Bookends"


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("1,3,5-8", [0, 2, 4, 5, 6, 7]),
        ("all", [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]),
        ("2 , 2, 2", [1]),
        ("8-5", [4, 5, 6, 7]),
        ("3,999", [2]),
        ("0", []),
        ("nonsense", []),
        ("", []),
    ],
)
def test_picking_by_the_numbers_on_the_screen(spec, expected):
    assert parse_picks(spec, 10) == expected


def test_picks_keep_the_order_the_user_asked_for():
    assert parse_picks("5,1", 10) == [4, 0]


def test_picked_albums_become_the_text_of_a_search_box():
    albums = [
        SavedAlbum("spotify", "1", "Burial", "Untrue"),
        SavedAlbum("spotify", "2", "Pet Fox", "A face in your life"),
    ]

    assert as_lines(albums) == "Burial - Untrue\nPet Fox - A face in your life"


def test_an_unconfigured_service_is_absent_not_broken(monkeypatch, tmp_path):
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("TIDAL_CLIENT_ID", raising=False)

    assert configured_sources() == []
    assert build_library("spotify", TokenStore(directory=tmp_path)) is None


def test_a_configured_service_builds(monkeypatch, tmp_path):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.delenv("TIDAL_CLIENT_ID", raising=False)

    assert configured_sources() == ["spotify"]
    assert isinstance(build_library("spotify", TokenStore(directory=tmp_path)), SpotifyLibrary)


@pytest.mark.anyio
async def test_the_fake_stands_in_for_a_real_library():
    library = FakeLibrary(albums=[SavedAlbum("fake", "1", "A", "B")])

    assert await library.recent(5) == library.albums
    assert library.asked == [5]
