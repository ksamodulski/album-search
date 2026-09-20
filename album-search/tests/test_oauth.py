"""The login dance: PKCE, the loopback callback, and keeping the token.

Offline throughout: the token endpoint is an `httpx.MockTransport` and the
callback is a real loopback request to our own one-shot listener, so what is
under test is our half of the protocol rather than any service.
"""

import asyncio
import json
import socket
import stat
import time

import httpx
import pytest

from groove_search.oauth import (
    LoginFlow,
    OAuthConfig,
    OAuthError,
    Pkce,
    Token,
    TokenStore,
    authorize_url,
    capture_code,
    ensure_fresh,
    exchange,
    refresh,
)

CONFIG = OAuthConfig(
    service="spotify",
    client_id="client-123",
    authorize_url="https://accounts.example/authorize",
    token_url="https://accounts.example/api/token",
    scopes=("user-library-read",),
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def transport(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- PKCE ----------------------------------------------------------------


def test_challenge_matches_the_rfc_vector():
    """RFC 7636 appendix B. Getting this wrong fails one step later, opaquely."""
    pkce = Pkce.from_verifier("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")

    assert pkce.challenge == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert "=" not in pkce.challenge


def test_generated_verifier_is_long_enough_to_be_legal():
    pkce = Pkce.generate()

    assert 43 <= len(pkce.verifier) <= 128
    assert Pkce.from_verifier(pkce.verifier).challenge == pkce.challenge


def test_authorize_url_carries_the_scope_and_the_loopback_redirect():
    url = authorize_url(CONFIG, Pkce.from_verifier("v" * 43), "state-abc", port=8899)

    assert url.startswith("https://accounts.example/authorize?")
    assert "code_challenge_method=S256" in url
    assert "scope=user-library-read" in url
    assert "state=state-abc" in url
    # The IP literal, not "localhost": Spotify will not register the latter.
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8899%2Fcallback" in url


# --- the loopback listener -----------------------------------------------


@pytest.mark.anyio
async def test_captures_the_code_the_browser_is_redirected_with():
    port = free_port()
    waiting = asyncio.create_task(capture_code(state="s1", port=port, timeout=5))
    await asyncio.sleep(0.05)

    async with httpx.AsyncClient() as client:
        response = await client.get(f"http://127.0.0.1:{port}/callback", params={"code": "abc", "state": "s1"})

    assert await waiting == "abc"
    assert "close this tab" in response.text


@pytest.mark.anyio
async def test_a_browser_asking_for_something_else_is_not_the_user():
    """A favicon request must not be mistaken for the callback."""
    port = free_port()
    waiting = asyncio.create_task(capture_code(state="s1", port=port, timeout=5))
    await asyncio.sleep(0.05)

    async with httpx.AsyncClient() as client:
        stray = await client.get(f"http://127.0.0.1:{port}/favicon.ico")
        assert stray.status_code == 404
        assert not waiting.done()
        await client.get(f"http://127.0.0.1:{port}/callback", params={"code": "real", "state": "s1"})

    assert await waiting == "real"


@pytest.mark.anyio
async def test_a_refusal_is_reported_in_the_services_own_words():
    port = free_port()
    waiting = asyncio.create_task(capture_code(state="s1", port=port, timeout=5))
    await asyncio.sleep(0.05)

    async with httpx.AsyncClient() as client:
        await client.get(f"http://127.0.0.1:{port}/callback", params={"error": "access_denied", "state": "s1"})

    with pytest.raises(OAuthError, match="access_denied"):
        await waiting


@pytest.mark.anyio
async def test_a_callback_from_another_login_is_refused():
    port = free_port()
    waiting = asyncio.create_task(capture_code(state="mine", port=port, timeout=5))
    await asyncio.sleep(0.05)

    async with httpx.AsyncClient() as client:
        await client.get(f"http://127.0.0.1:{port}/callback", params={"code": "abc", "state": "theirs"})

    with pytest.raises(OAuthError, match="state mismatch"):
        await waiting


@pytest.mark.anyio
async def test_waiting_forever_is_not_an_option():
    with pytest.raises(OAuthError, match="timed out"):
        await capture_code(state="s1", port=free_port(), timeout=0.2)


# --- the token calls ------------------------------------------------------


@pytest.mark.anyio
async def test_exchange_sends_the_verifier_and_the_same_redirect_uri():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600})

    async with transport(handler) as client:
        token = await exchange(CONFIG, "the-code", "the-verifier", port=8899, client=client)

    assert seen["grant_type"] == "authorization_code"
    assert seen["code_verifier"] == "the-verifier"
    assert seen["redirect_uri"] == "http://127.0.0.1:8899/callback"
    assert token.access_token == "at"
    assert not token.expired


@pytest.mark.anyio
async def test_a_refresh_that_rotates_the_token_keeps_the_new_one():
    """Both services may rotate it, and the old one then never works again."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "new", "refresh_token": "rotated", "expires_in": 3600})

    async with transport(handler) as client:
        renewed = await refresh(CONFIG, Token("old", "original", 0), client=client)

    assert renewed.refresh_token == "rotated"


@pytest.mark.anyio
async def test_a_refresh_that_says_nothing_keeps_the_token_we_have():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})

    async with transport(handler) as client:
        renewed = await refresh(CONFIG, Token("old", "original", 0, scope="user-library-read"), client=client)

    assert renewed.refresh_token == "original"
    assert renewed.scope == "user-library-read"


@pytest.mark.anyio
async def test_a_refusal_quotes_the_service_rather_than_the_status_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Refresh token revoked"})

    async with transport(handler) as client:
        with pytest.raises(OAuthError, match="Refresh token revoked"):
            await refresh(CONFIG, Token("old", "rt", 0), client=client)


@pytest.mark.anyio
async def test_a_service_that_cannot_be_reached_says_so():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with transport(handler) as client:
        with pytest.raises(OAuthError, match="could not reach spotify"):
            await refresh(CONFIG, Token("old", "rt", 0), client=client)


@pytest.mark.anyio
async def test_no_refresh_token_is_a_sign_in_again_not_a_crash():
    with pytest.raises(OAuthError, match="sign in again"):
        await refresh(CONFIG, Token("old"), client=None)


# --- the store ------------------------------------------------------------


def test_a_token_is_written_for_its_owner_only(tmp_path):
    store = TokenStore(directory=tmp_path / "tokens")
    store.save("spotify", Token("at", "rt", time.time() + 3600, scope="user-library-read"))

    mode = stat.S_IMODE(store.path("spotify").stat().st_mode)

    assert mode == 0o600, "a token grants access to somebody's account"
    assert store.load("spotify").refresh_token == "rt"
    assert store.has("spotify")


def test_forgetting_a_token_is_signing_out(tmp_path):
    store = TokenStore(directory=tmp_path)
    store.save("tidal", Token("at"))

    assert store.forget("tidal") is True
    assert store.forget("tidal") is False
    assert store.load("tidal") is None


def test_a_corrupt_token_file_is_a_login_to_redo_not_an_error(tmp_path):
    store = TokenStore(directory=tmp_path)
    store.directory.mkdir(exist_ok=True)
    store.path("spotify").write_text("{not json", encoding="utf-8")

    assert store.load("spotify") is None


def test_an_expiry_inside_the_skew_counts_as_expired():
    assert Token("at", expires_at=time.time() + 5).expired
    assert not Token("at", expires_at=time.time() + 600).expired
    assert Token("at").expired, "a token with no expiry cannot be trusted to work"


@pytest.mark.anyio
async def test_ensure_fresh_renews_and_re_saves(tmp_path):
    store = TokenStore(directory=tmp_path)
    store.save("spotify", Token("stale", "rt", time.time() - 10))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})

    async with transport(handler) as client:
        token = await ensure_fresh(CONFIG, store, client=client)

    assert token.access_token == "fresh"
    assert json.loads(store.path("spotify").read_text())["access_token"] == "fresh"


@pytest.mark.anyio
async def test_ensure_fresh_on_a_service_never_signed_in_is_none_not_an_error(tmp_path):
    assert await ensure_fresh(CONFIG, TokenStore(directory=tmp_path)) is None


@pytest.mark.anyio
async def test_a_login_flow_stores_the_token_it_completes(tmp_path):
    store = TokenStore(directory=tmp_path)
    flow = LoginFlow(config=CONFIG, port=free_port())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3600})

    async def answer():
        await asyncio.sleep(0.05)
        async with httpx.AsyncClient() as client:
            await client.get(f"http://127.0.0.1:{flow.port}/callback", params={"code": "c", "state": flow.state})

    # The exchange itself is the only network call left, and it is mocked.
    import groove_search.oauth as oauth

    async def fake_exchange(config, code, verifier, *, port=None, client=None):
        async with transport(handler) as mock:
            return await oauth._token_call(config, {"code": code}, client=mock)

    original, oauth.exchange = oauth.exchange, fake_exchange
    try:
        _, token = await asyncio.gather(answer(), flow.complete(store))
    finally:
        oauth.exchange = original

    assert token.access_token == "at"
    assert store.load("spotify").access_token == "at"
