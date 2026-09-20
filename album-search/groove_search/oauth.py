"""Authorization Code with PKCE, and somewhere to keep the resulting token.

Every streaming service that will tell you what is in *your* library wants the
same dance: send the user to a consent page, catch the code it redirects back
with, swap that for a token, and refresh the token before it dies. Spotify and
TIDAL differ only in their URLs and their scope names, so the dance is written
once here and `library.py` supplies the differences as data.

Two things in here are load-bearing and easy to get subtly wrong:

* **The redirect URI is a loopback IP literal**, `http://127.0.0.1:<port>/callback`,
  never `localhost` - Spotify refuses to register the latter. One listener,
  shared by the CLI and the web app, means one redirect URI to register per
  service rather than one per surface.
* **A refresh token can be rotated.** Both services may hand back a *new*
  refresh token when you use the old one, and a store that keeps only the
  access token logs the user out a week later for no visible reason.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import httpx

# The loopback port the consent page redirects back to. Registered with the
# service, so it cannot be picked at random - but it can collide with
# something else on the machine, hence the override.
DEFAULT_PORT = 8899
CALLBACK_PATH = "/callback"

# How long before expiry a token is treated as already dead. A token that
# expires mid-request is an error the user cannot act on.
EXPIRY_SKEW = 60.0

# How long to wait for the user to finish logging in before giving up.
LOGIN_TIMEOUT = 300.0


class OAuthError(RuntimeError):
    """The authorization could not be completed, with a reason to show a human."""


def callback_port() -> int:
    raw = os.environ.get("GROOVE_OAUTH_PORT", "").strip()
    return int(raw) if raw.isdigit() else DEFAULT_PORT


def redirect_uri(port: int | None = None) -> str:
    """The address to register with the service, and to send it back to."""
    return f"http://127.0.0.1:{port or callback_port()}{CALLBACK_PATH}"


@dataclass(frozen=True, slots=True)
class OAuthConfig:
    """Everything about one service's authorization that is not shared."""

    service: str
    client_id: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...] = ()
    # How the service spells itself, for anything a human reads. Defaults to
    # the id, which is what a test or a new service gets for free.
    label: str = ""
    # Public clients (PKCE) have no secret. A service that insists on one -
    # TIDAL does for some app types - gets HTTP Basic on the token call.
    client_secret: str = ""

    @property
    def scope(self) -> str:
        return " ".join(self.scopes)

    @property
    def name(self) -> str:
        return self.label or self.service


@dataclass(frozen=True, slots=True)
class Pkce:
    """A verifier the client keeps and the challenge it publishes (RFC 7636)."""

    verifier: str
    challenge: str
    method: str = "S256"

    @classmethod
    def generate(cls) -> Pkce:
        # 32 random bytes -> 43 base64url characters, the RFC's minimum length.
        return cls.from_verifier(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("="))

    @classmethod
    def from_verifier(cls, verifier: str) -> Pkce:
        """The S256 challenge for a verifier: base64url of its SHA-256, unpadded.

        The padding matters - a challenge carrying '=' is rejected, and the
        symptom is an opaque 'invalid_grant' at the *token* call, one step
        after the mistake.
        """
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return cls(verifier=verifier, challenge=base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="))


@dataclass
class Token:
    """An access token, what renews it, and when it stops working."""

    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0
    scope: str = ""
    token_type: str = "Bearer"

    @property
    def expired(self) -> bool:
        return not self.expires_at or self.expires_at - EXPIRY_SKEW <= time.time()

    @property
    def header(self) -> dict[str, str]:
        return {"Authorization": f"{self.token_type or 'Bearer'} {self.access_token}"}

    @classmethod
    def from_payload(cls, payload: dict, previous: Token | None = None) -> Token:
        """Read a token response, keeping what a refresh response leaves out.

        A refresh usually answers without a `refresh_token`, meaning "keep the
        one you have" - but sometimes it rotates it, and then the new one is
        the only one that will ever work again. Both cases are this line.
        """
        expires_in = payload.get("expires_in")
        return cls(
            access_token=str(payload.get("access_token") or ""),
            refresh_token=str(payload.get("refresh_token") or (previous.refresh_token if previous else "")),
            expires_at=time.time() + float(expires_in) if expires_in else 0.0,
            scope=str(payload.get("scope") or (previous.scope if previous else "")),
            token_type=str(payload.get("token_type") or "Bearer"),
        )

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
            "token_type": self.token_type,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Token:
        return cls(
            access_token=str(data.get("access_token") or ""),
            refresh_token=str(data.get("refresh_token") or ""),
            expires_at=float(data.get("expires_at") or 0.0),
            scope=str(data.get("scope") or ""),
            token_type=str(data.get("token_type") or "Bearer"),
        )


@dataclass
class TokenStore:
    """Tokens on disk, one file per service, readable only by their owner.

    This is the one place the app holds a credential that grants access to
    somebody's account, so the file is written 0600 and lives under `var/`,
    which is gitignored along with the registry and the page cache.
    """

    directory: Path

    def path(self, service: str) -> Path:
        return self.directory / f"{service}.json"

    def load(self, service: str) -> Token | None:
        path = self.path(service)
        if not path.exists():
            return None
        try:
            return Token.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            # A corrupt token file is a login the user can simply redo; it is
            # never worth failing a command over.
            return None

    def save(self, service: str, token: Token) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path(service)
        path.write_text(json.dumps(token.to_dict(), indent=2), encoding="utf-8")
        path.chmod(0o600)

    def forget(self, service: str) -> bool:
        path = self.path(service)
        if not path.exists():
            return False
        path.unlink()
        return True

    def has(self, service: str) -> bool:
        return self.path(service).exists()


def authorize_url(config: OAuthConfig, pkce: Pkce, state: str, *, port: int | None = None) -> str:
    """The consent page to send the user to."""
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": redirect_uri(port),
        "code_challenge_method": pkce.method,
        "code_challenge": pkce.challenge,
        "state": state,
    }
    if config.scope:
        params["scope"] = config.scope
    return f"{config.authorize_url}?{urllib.parse.urlencode(params)}"


async def capture_code(
    *, state: str, port: int | None = None, timeout: float = LOGIN_TIMEOUT
) -> str:
    """Run a one-shot loopback listener and return the code it is redirected with.

    Anything that is not the callback gets a 404 and the wait continues - a
    browser asking for /favicon.ico must not be mistaken for the user.
    """
    port = port or callback_port()
    loop = asyncio.get_running_loop()
    answer: asyncio.Future[str] = loop.create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            target = line.decode("latin-1").split(" ")[1] if b" " in line else ""
        except (ValueError, IndexError, UnicodeDecodeError):
            target = ""
        parsed = urllib.parse.urlsplit(target)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        if parsed.path != CALLBACK_PATH:
            await _respond(writer, 404, "Nothing here.")
            return
        if answer.done():
            await _respond(writer, 200, "Already signed in. You can close this tab.")
            return
        if params.get("error"):
            await _respond(writer, 200, f"Authorization refused: {params['error']}. You can close this tab.")
            answer.set_exception(OAuthError(f"authorization refused: {params['error']}"))
            return
        if params.get("state") != state:
            await _respond(writer, 400, "That login did not belong to this session.")
            answer.set_exception(OAuthError("state mismatch - the callback did not belong to this login"))
            return
        code = params.get("code", "")
        if not code:
            await _respond(writer, 400, "No authorization code came back.")
            answer.set_exception(OAuthError("no authorization code in the callback"))
            return
        await _respond(writer, 200, "Signed in. You can close this tab and go back to groove-search.")
        answer.set_result(code)

    try:
        server = await asyncio.start_server(handle, host="127.0.0.1", port=port)
    except OSError as exc:
        raise OAuthError(
            f"cannot listen on 127.0.0.1:{port} ({exc}). Something else is using it - "
            f"set GROOVE_OAUTH_PORT and register the matching redirect URI."
        ) from exc
    try:
        async with server:
            return await asyncio.wait_for(answer, timeout)
    except TimeoutError as exc:
        raise OAuthError(f"timed out after {timeout:.0f}s waiting for the login to finish") from exc


async def _respond(writer: asyncio.StreamWriter, status: int, message: str) -> None:
    """Say something a human can read in the tab the service redirected."""
    body = (
        "<!doctype html><meta charset='utf-8'><title>groove-search</title>"
        "<body style=\"font:16px/1.6 system-ui;margin:12vh auto;max-width:34rem;text-align:center\">"
        f"<h1 style='font-size:19px'>groove-search</h1><p>{message}</p></body>"
    ).encode("utf-8")
    reason = {200: "OK", 400: "Bad Request", 404: "Not Found"}.get(status, "OK")
    writer.write(
        f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
        + body
    )
    try:
        await writer.drain()
    except OSError:  # the browser hung up first; the code is still ours
        pass
    writer.close()


async def exchange(
    config: OAuthConfig,
    code: str,
    verifier: str,
    *,
    port: int | None = None,
    client: httpx.AsyncClient | None = None,
) -> Token:
    """Swap the authorization code for a token."""
    return await _token_call(
        config,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(port),
            "client_id": config.client_id,
            "code_verifier": verifier,
        },
        client=client,
    )


async def refresh(
    config: OAuthConfig, token: Token, *, client: httpx.AsyncClient | None = None
) -> Token:
    """Renew an expired token, keeping whatever the answer leaves out."""
    if not token.refresh_token:
        raise OAuthError(f"{config.name} did not give us a refresh token - sign in again")
    return await _token_call(
        config,
        {
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
            "client_id": config.client_id,
        },
        previous=token,
        client=client,
    )


async def _token_call(
    config: OAuthConfig,
    data: dict[str, str],
    *,
    previous: Token | None = None,
    client: httpx.AsyncClient | None = None,
) -> Token:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if config.client_secret:
        pair = f"{config.client_id}:{config.client_secret}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(pair).decode("ascii")
    owned = client is None
    http = client or httpx.AsyncClient(timeout=20.0)
    try:
        response = await http.post(config.token_url, data=data, headers=headers)
        if response.status_code >= 400:
            raise OAuthError(f"{config.name} refused the token request: {_why(response)}")
        payload = response.json()
    except httpx.HTTPError as exc:
        raise OAuthError(f"could not reach {config.name}: {exc}") from exc
    except ValueError as exc:
        raise OAuthError(f"{config.name} answered the token request with something that is not JSON") from exc
    finally:
        if owned:
            await http.aclose()
    token = Token.from_payload(payload, previous)
    if not token.access_token:
        raise OAuthError(f"{config.name} returned no access token")
    return token


def _why(response: httpx.Response) -> str:
    """The service's own words about a refusal, which are usually the fix."""
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    detail = payload.get("error_description") or payload.get("error") or payload.get("message")
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("reason")
    return f"HTTP {response.status_code}: {detail}" if detail else f"HTTP {response.status_code}"


async def ensure_fresh(
    config: OAuthConfig, store: TokenStore, *, client: httpx.AsyncClient | None = None
) -> Token | None:
    """The usable token for a service, renewed and re-saved if it had expired.

    None means "not signed in", which is a normal state and not a failure -
    the caller offers a login rather than reporting an error.
    """
    token = store.load(config.service)
    if token is None or not token.access_token:
        return None
    if not token.expired:
        return token
    renewed = await refresh(config, token, client=client)
    store.save(config.service, renewed)
    return renewed


@dataclass
class LoginFlow:
    """One login in progress: the URL to visit, and the wait for the callback.

    Split in two because the two surfaces start it differently - the CLI opens
    a browser itself, while the web app hands the user a link in the page they
    are already looking at - but both then wait on the same listener.
    """

    config: OAuthConfig
    port: int = field(default_factory=callback_port)
    pkce: Pkce = field(default_factory=Pkce.generate)
    state: str = field(default_factory=lambda: secrets.token_urlsafe(16))

    @property
    def url(self) -> str:
        return authorize_url(self.config, self.pkce, self.state, port=self.port)

    async def complete(self, store: TokenStore, *, timeout: float = LOGIN_TIMEOUT) -> Token:
        code = await capture_code(state=self.state, port=self.port, timeout=timeout)
        token = await exchange(self.config, code, self.pkce.verifier, port=self.port)
        store.save(self.config.service, token)
        return token
