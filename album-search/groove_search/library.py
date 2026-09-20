"""The last albums you added to Spotify or TIDAL, as lines this app can search.

A third *input*, not a third source. The want-list already exists - records get
saved to a streaming account as they are discovered - and retyping it by hand is
the only reason those albums were not being priced. What comes out of here is
`SavedAlbum`s, whose `query_line` goes through the same `normalize` path as
anything typed into the box, so nothing downstream learns that a streaming
service exists.

The seam is deliberately the same shape as `websearch.SearchProvider`, because
the problem is the same: a remote service that can refuse us, behind one method,
with a fake so the test suite stays offline. The same rule applies too - **an
adapter never raises**. A service that is down, a token that expired, a scope
that was never granted: each is an empty list plus a `last_error` the UI shows,
never an exception that loses the rest of the user's session.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from .domain import SavedAlbum
from .oauth import OAuthConfig, TokenStore, OAuthError, ensure_fresh

# How many albums to offer when nobody says. Big enough to hold a month of
# adding, small enough to read in one screen.
DEFAULT_LIMIT = 25
# A ceiling on how far back to page, so a typo cannot walk someone's whole
# library one request at a time.
MAX_LIMIT = 200

SPOTIFY_API = "https://api.spotify.com/v1"
SPOTIFY_AUTHORIZE = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN = "https://accounts.spotify.com/api/token"
# Spotify's saved albums are "your library"; nothing else is needed or asked for.
SPOTIFY_SCOPES = ("user-library-read",)
SPOTIFY_PAGE = 50  # the API maximum

TIDAL_API = "https://openapi.tidal.com/v2"
TIDAL_AUTHORIZE = "https://login.tidal.com/authorize"
TIDAL_TOKEN = "https://auth.tidal.com/v1/oauth2/token"
TIDAL_SCOPES = ("user.read", "collection.read")
TIDAL_PAGE = 50
# JSON:API, not plain JSON - TIDAL answers 406 without this.
TIDAL_ACCEPT = "application/vnd.api+json"

SOURCES = ("spotify", "tidal")
# Services spell their own names; "Connect spotify" reads like a typo.
SERVICE_NAMES = {"spotify": "Spotify", "tidal": "TIDAL"}


def display_name(source: str) -> str:
    return SERVICE_NAMES.get(source, source)


class MusicLibrary(Protocol):
    """Anything that can list the albums somebody recently saved.

    `recent` must not raise for network or authorization trouble: an empty
    list with a `last_error` saying why is the contract, so "you are not
    signed in" can never be displayed as "you have saved nothing".
    """

    name: str
    last_error: str | None

    async def recent(self, limit: int = DEFAULT_LIMIT) -> list[SavedAlbum]: ...


# --- configuration -------------------------------------------------------


def spotify_config() -> OAuthConfig | None:
    """Spotify's half of the OAuth dance, if this machine has a client id."""
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    if not client_id:
        return None
    return OAuthConfig(
        service="spotify",
        client_id=client_id,
        authorize_url=os.environ.get("GROOVE_SPOTIFY_AUTHORIZE", "").strip() or SPOTIFY_AUTHORIZE,
        token_url=os.environ.get("GROOVE_SPOTIFY_TOKEN", "").strip() or SPOTIFY_TOKEN,
        scopes=SPOTIFY_SCOPES,
        label=display_name("spotify"),
    )


def tidal_config() -> OAuthConfig | None:
    """TIDAL's half. Its URLs are overridable for the reason in `TidalLibrary`."""
    client_id = os.environ.get("TIDAL_CLIENT_ID", "").strip()
    if not client_id:
        return None
    return OAuthConfig(
        service="tidal",
        client_id=client_id,
        authorize_url=os.environ.get("GROOVE_TIDAL_AUTHORIZE", "").strip() or TIDAL_AUTHORIZE,
        token_url=os.environ.get("GROOVE_TIDAL_TOKEN", "").strip() or TIDAL_TOKEN,
        scopes=TIDAL_SCOPES,
        # PKCE public clients have none; TIDAL issues a secret for some app
        # types and rejects the token call without it.
        client_secret=os.environ.get("TIDAL_CLIENT_SECRET", "").strip(),
        label=display_name("tidal"),
    )


def config_for(source: str) -> OAuthConfig | None:
    return {"spotify": spotify_config, "tidal": tidal_config}.get(source, lambda: None)()


def configured_sources() -> list[str]:
    """The services this machine has a client id for, in a stable order."""
    return [name for name in SOURCES if config_for(name) is not None]


def build_library(
    source: str, store: TokenStore, *, country: str = "PL", client: httpx.AsyncClient | None = None
) -> MusicLibrary | None:
    """One library adapter by name, or None when it is not configured here.

    None is a normal state, exactly as it is for the open web: the UI offers
    the setting rather than reporting a failure.
    """
    config = config_for(source)
    if config is None:
        return None
    if source == "spotify":
        return SpotifyLibrary(config=config, store=store, client=client)
    return TidalLibrary(config=config, store=store, country=country.upper(), client=client)


def library_hint() -> str:
    """How to switch a library on, for a UI to show where one would have been."""
    return (
        "Register an app with the service, set SPOTIFY_CLIENT_ID (or TIDAL_CLIENT_ID), "
        f"and give the app the redirect URI http://127.0.0.1:{os.environ.get('GROOVE_OAUTH_PORT', '8899')}"
        "/callback - the 127.0.0.1 literal, not 'localhost', which Spotify refuses to "
        "register. No client secret is needed: the login is Authorization Code with PKCE."
    )


# --- the adapters --------------------------------------------------------


@dataclass
class _HttpLibrary:
    """What the two adapters share: a token, a client, and failure discipline."""

    config: OAuthConfig
    store: TokenStore
    last_error: str | None = None
    timeout: float = 20.0
    client: httpx.AsyncClient | None = field(default=None, repr=False)
    _owned: bool = field(default=False, repr=False)

    @property
    def name(self) -> str:
        return self.config.service

    @property
    def label(self) -> str:
        """The service's own spelling of itself, for anything a human reads."""
        return self.config.name

    @property
    def signed_in(self) -> bool:
        return self.store.has(self.config.service)

    def _http(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=self.timeout)
            self._owned = True
        return self.client

    async def aclose(self) -> None:
        if self.client is not None and self._owned:
            await self.client.aclose()
            self.client = None
            self._owned = False

    async def _token_header(self) -> dict[str, str] | None:
        """The auth header, refreshing the token first, or None with a reason."""
        try:
            token = await ensure_fresh(self.config, self.store, client=self._http())
        except OAuthError as exc:
            self.last_error = f"{exc} - sign in again"
            return None
        if token is None:
            self.last_error = f"not signed in to {self.label}"
            return None
        return token.header

    async def _get(self, url: str, headers: dict[str, str], params: dict | None = None) -> dict | None:
        """One GET, turning every failure into a reason rather than an exception."""
        try:
            response = await self._http().get(url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            self.last_error = f"could not reach {self.label}: {exc}"
            return None
        if response.status_code == 401:
            self.last_error = f"{self.label} rejected the token - sign in again"
            return None
        if response.status_code == 403:
            # Almost always a scope the app was never granted, which no amount
            # of retrying fixes and which the user can actually act on.
            self.last_error = f"{self.label} refused access ({_detail(response)}) - check the app's scopes"
            return None
        if response.status_code == 429:
            wait = response.headers.get("Retry-After", "")
            self.last_error = f"{self.label} is rate-limiting us" + (f"; retry after {wait}s" if wait else "")
            return None
        if response.status_code >= 400:
            self.last_error = f"{self.label}: {_detail(response)}"
            return None
        try:
            payload = response.json()
        except ValueError:
            self.last_error = f"{self.label} answered with something that is not JSON"
            return None
        return payload if isinstance(payload, dict) else None


@dataclass
class SpotifyLibrary(_HttpLibrary):
    """Spotify's saved albums, newest first.

    `GET /me/albums` is paged at 50, and the order it returns is not documented
    anywhere - so the sort by `added_at` is done here rather than trusted, or
    the day Spotify changes it "the last 20 I added" would quietly become
    twenty arbitrary albums.
    """

    api: str = field(default_factory=lambda: os.environ.get("GROOVE_SPOTIFY_API", "").strip() or SPOTIFY_API)

    async def recent(self, limit: int = DEFAULT_LIMIT) -> list[SavedAlbum]:
        self.last_error = None
        limit = max(1, min(limit, MAX_LIMIT))
        headers = await self._token_header()
        if headers is None:
            return []
        albums: list[SavedAlbum] = []
        offset = 0
        while len(albums) < limit:
            payload = await self._get(
                f"{self.api}/me/albums",
                headers,
                {"limit": min(SPOTIFY_PAGE, limit - len(albums)), "offset": offset},
            )
            if payload is None:
                # Keep whatever earlier pages gave us: half a list with a
                # reason beside it beats nothing with the same reason.
                break
            items = payload.get("items") or []
            albums.extend(saved for saved in (_spotify_album(i) for i in items) if saved)
            if not payload.get("next") or not items:
                break
            offset += len(items)
        albums.sort(key=lambda a: a.added_at, reverse=True)
        return albums[:limit]


def _spotify_album(item: dict) -> SavedAlbum | None:
    album = item.get("album") if isinstance(item, dict) else None
    if not isinstance(album, dict):
        return None
    name = str(album.get("name") or "").strip()
    if not name:
        return None
    artists = [str(a.get("name") or "").strip() for a in album.get("artists") or [] if isinstance(a, dict)]
    images = [i for i in album.get("images") or [] if isinstance(i, dict) and i.get("url")]
    return SavedAlbum(
        source="spotify",
        id=str(album.get("id") or ""),
        # A compilation credited to six artists searches far better as the
        # first one: shops name the headliner, not the guest list.
        artist=next((a for a in artists if a), ""),
        title=name,
        added_at=str(item.get("added_at") or ""),
        url=str((album.get("external_urls") or {}).get("spotify") or ""),
        image_url=str(images[-1]["url"]) if images else None,
    )


@dataclass
class TidalLibrary(_HttpLibrary):
    """TIDAL's collection: `/userCollections/{id}/relationships/albums`.

    Written against documentation that could not be read end to end - TIDAL's
    reference is JavaScript-rendered and its collection API rolled out
    recently - so every base URL here is an environment override and the
    parsing is defensive about shapes it has not seen. Correcting a path is
    then configuration rather than a code change. The one thing not guessed at
    is the order: when the collection says `addedAt`, that is what "recent"
    means; when it does not, the API's own order is kept rather than a
    plausible-looking one being invented.
    """

    country: str = "PL"
    api: str = field(default_factory=lambda: os.environ.get("GROOVE_TIDAL_API", "").strip() or TIDAL_API)

    async def recent(self, limit: int = DEFAULT_LIMIT) -> list[SavedAlbum]:
        self.last_error = None
        limit = max(1, min(limit, MAX_LIMIT))
        headers = await self._token_header()
        if headers is None:
            return []
        headers = {**headers, "Accept": TIDAL_ACCEPT}

        me = await self._get(f"{self.api}/users/me", headers)
        if me is None:
            return []
        user_id = str((me.get("data") or {}).get("id") or "")
        if not user_id:
            self.last_error = "TIDAL did not say who the token belongs to"
            return []

        url = f"{self.api}/userCollections/{user_id}/relationships/albums"
        params: dict | None = {
            "countryCode": self.country,
            "include": "albums,albums.artists",
            "page[limit]": min(TIDAL_PAGE, limit),
        }
        albums: list[SavedAlbum] = []
        seen: set[str] = set()
        while url and len(albums) < limit:
            payload = await self._get(url, headers, params)
            if payload is None:
                break
            included = _tidal_index(payload.get("included"))
            for entry in payload.get("data") or []:
                saved = _tidal_album(entry, included)
                if saved and saved.id not in seen:
                    seen.add(saved.id)
                    albums.append(saved)
            url, params = _tidal_next(payload, self.api), None
        if any(a.added_at for a in albums):
            albums.sort(key=lambda a: a.added_at, reverse=True)
        return albums[:limit]


def _tidal_index(included) -> dict[tuple[str, str], dict]:
    """JSON:API's `included` list, keyed the way relationships point into it."""
    index: dict[tuple[str, str], dict] = {}
    for resource in included or []:
        if isinstance(resource, dict) and resource.get("id"):
            index[(str(resource.get("type") or ""), str(resource["id"]))] = resource
    return index


def _tidal_album(entry, included: dict[tuple[str, str], dict]) -> SavedAlbum | None:
    if not isinstance(entry, dict) or not entry.get("id"):
        return None
    album_id = str(entry["id"])
    resource = included.get((str(entry.get("type") or "albums"), album_id), entry)
    attributes = resource.get("attributes") or {}
    title = str(attributes.get("title") or attributes.get("name") or "").strip()
    if not title:
        return None
    return SavedAlbum(
        source="tidal",
        id=album_id,
        artist=_tidal_artist(resource, included),
        title=title,
        # The collection carries when it was added; the album resource does not.
        added_at=str((entry.get("meta") or {}).get("addedAt") or ""),
        url=str((attributes.get("externalLinks") or [{}])[0].get("href") or "")
        if isinstance(attributes.get("externalLinks"), list)
        else "",
        image_url=_tidal_cover(attributes),
    )


def _tidal_artist(resource: dict, included: dict[tuple[str, str], dict]) -> str:
    """The headline artist, resolved through the relationship when it is there."""
    relationships = resource.get("relationships") or {}
    data = (relationships.get("artists") or {}).get("data") or []
    for ref in data if isinstance(data, list) else [data]:
        if not isinstance(ref, dict) or not ref.get("id"):
            continue
        artist = included.get((str(ref.get("type") or "artists"), str(ref["id"])))
        name = str(((artist or {}).get("attributes") or {}).get("name") or "").strip()
        if name:
            return name
    # Some shapes inline the credit on the album itself.
    attributes = resource.get("attributes") or {}
    for key in ("artistName", "artist"):
        value = attributes.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _tidal_cover(attributes: dict) -> str | None:
    art = attributes.get("imageLinks") or attributes.get("coverArt") or []
    if isinstance(art, list):
        for link in art:
            if isinstance(link, dict) and link.get("href"):
                return str(link["href"])
    return None


def _tidal_next(payload: dict, api: str) -> str:
    """JSON:API's cursor, as an absolute URL. Empty string means this was the end."""
    nxt = (payload.get("links") or {}).get("next")
    if not isinstance(nxt, str) or not nxt:
        return ""
    if nxt.startswith("http"):
        return nxt
    return api.rstrip("/") + "/" + nxt.lstrip("/") if not nxt.startswith("/v2") else "https://openapi.tidal.com" + nxt


def _detail(response: httpx.Response) -> str:
    """The service's own words about a refusal, when it offers any."""
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        detail = error.get("message") or error.get("status")
    elif isinstance(error, str):
        detail = payload.get("error_description") or error
    else:
        errors = payload.get("errors") if isinstance(payload, dict) else None
        first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
        detail = first.get("detail") or first.get("title")
    return f"HTTP {response.status_code}: {detail}" if detail else f"HTTP {response.status_code}"


# --- picking -------------------------------------------------------------


def parse_picks(spec: str, count: int) -> list[int]:
    """Turn "1,3,5-8" (or "all") into zero-based indices into a listing.

    Forgiving on purpose - this is a human typing numbers they just read off a
    screen: whitespace, repeats, reversed ranges and out-of-range numbers are
    all quietly handled, and the order the user asked for is kept.
    """
    spec = spec.strip().lower()
    if spec in ("all", "*"):
        return list(range(count))
    picked: list[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part[1:]:
            head, _, tail = part.partition("-")
            if head.isdigit() and tail.isdigit():
                first, last = sorted((int(head), int(tail)))
                picked.extend(range(first, last + 1))
            continue
        if part.isdigit():
            picked.append(int(part))
    seen: set[int] = set()
    ordered: list[int] = []
    for number in picked:
        index = number - 1  # the listing is 1-based, as printed
        if 0 <= index < count and index not in seen:
            seen.add(index)
            ordered.append(index)
    return ordered


def as_lines(albums: list[SavedAlbum]) -> str:
    """The chosen albums as the text the search box would have contained."""
    return "\n".join(album.query_line for album in albums if album.query_line)


@dataclass
class FakeLibrary:
    """Test adapter: a canned library, and a record of what was asked for."""

    name: str = "fake"
    albums: list[SavedAlbum] = field(default_factory=list)
    last_error: str | None = None
    asked: list[int] = field(default_factory=list)
    signed_in: bool = True

    async def recent(self, limit: int = DEFAULT_LIMIT) -> list[SavedAlbum]:
        self.asked.append(limit)
        if not self.signed_in:
            self.last_error = f"not signed in to {self.name}"
            return []
        return self.albums[:limit]

    async def aclose(self) -> None:
        return None
