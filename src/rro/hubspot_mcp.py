"""Connecting to HubSpot's own remote MCP server (https://mcp.hubspot.com).

HubSpot's server uses OAuth 2.1 with PKCE, and has no dynamic client
registration: the client is an "MCP connector" created by hand in a HubSpot
developer account, which gives a client ID and secret. The MCP SDK's
OAuthClientProvider does the rest (discovery, PKCE, token exchange, refresh);
this module supplies it with:

  - the pre-registered client, from .env (HUBSPOT_MCP_CLIENT_ID / _SECRET);
  - a token store in data/ (gitignored), so the browser login happens once;
  - the browser round trip for `rro hubspot-login`: open the consent page,
    catch the redirect on a loopback port, hand the code back to the SDK.

In normal runs there's no browser: if the stored tokens can't be refreshed,
the gateway stops with an instruction to run `rro hubspot-login`.
"""

from __future__ import annotations

import asyncio
import json
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx2
from mcp.client.auth import OAuthClientProvider
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
)

from rro.settings import Settings

SERVER_URL = "https://mcp.hubspot.com"
# Must match the redirect URL on the MCP connector exactly.
REDIRECT_URI = "http://localhost:8912/oauth/callback"
LOGIN_TIMEOUT_SECONDS = 300


class HubSpotLoginRequired(RuntimeError):
    pass


class FileTokenStorage:
    """The SDK's TokenStorage: tokens on disk (with the time they were issued),
    client credentials from settings."""

    EXPIRY_MARGIN_SECONDS = 60

    def __init__(self, path: Path, client_id: str, client_secret: str):
        self.path = path
        self.client = OAuthClientInformationFull(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uris=[REDIRECT_URI],
            token_endpoint_auth_method="client_secret_post",  # the only method HubSpot advertises
            grant_types=["authorization_code", "refresh_token"],
        )

    def _load(self) -> tuple[OAuthToken, float] | None:
        if not self.path.exists():
            return None
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if "token" not in data:  # the first format stored the bare token; fall back to the file's age
            return OAuthToken.model_validate(data), self.path.stat().st_mtime
        return OAuthToken.model_validate(data["token"]), data["obtained_at"]

    @property
    def expires_at(self) -> float | None:
        """When the stored access token stops working (a little early, to be safe)."""
        loaded = self._load()
        if loaded is None or loaded[0].expires_in is None:
            return None
        token, obtained_at = loaded
        return obtained_at + token.expires_in - self.EXPIRY_MARGIN_SECONDS

    async def get_tokens(self) -> OAuthToken | None:
        loaded = self._load()
        return loaded[0] if loaded else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"token": tokens.model_dump(mode="json"), "obtained_at": time.time()}
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    async def get_client_info(self) -> OAuthClientInformationFull:
        return self.client

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        pass  # pre-registered; nothing to persist


class HubSpotOAuthProvider(OAuthClientProvider):
    """The SDK's OAuth provider, with two gaps in its handling of *stored* tokens filled:

    1. A reloaded token has no expiry time, so the SDK treats it as valid, sends it,
       and on the 401 starts a whole new browser login instead of using the refresh
       token. Here the expiry is restored from when the token was issued.
    2. Until it has run discovery, the SDK sends refreshes to <server>/token, but
       HubSpot's token endpoint is /oauth/v3/token. Here the (public) authorization
       server metadata is fetched up front.

    With both, an expired token is refreshed silently at start-up or mid-session,
    and a browser login is only ever needed if the refresh token itself is revoked.
    """

    async def _initialize(self) -> None:
        await super()._initialize()
        storage = self.context.storage
        if isinstance(storage, FileTokenStorage):
            self.context.token_expiry_time = storage.expires_at
        if self.context.oauth_metadata is None:
            try:
                async with httpx2.AsyncClient(timeout=30) as http:
                    response = await http.get(f"{SERVER_URL}/.well-known/oauth-authorization-server")
                if response.status_code == 200:
                    self.context.oauth_metadata = OAuthMetadata.model_validate_json(response.content)
            except httpx2.HTTPError:
                pass  # the SDK discovers it on the first 401 anyway


def token_path(settings: Settings) -> Path:
    return settings.data_dir / "hubspot_mcp_oauth.json"


def http_client(settings: Settings, *, interactive: bool = False) -> httpx2.AsyncClient:
    """An HTTP client that authenticates to HubSpot's MCP server."""
    client_id, secret = settings.value("HUBSPOT_MCP_CLIENT_ID"), settings.value("HUBSPOT_MCP_CLIENT_SECRET")
    if not (client_id and secret):
        raise HubSpotLoginRequired(
            "HUBSPOT_MCP_CLIENT_ID and HUBSPOT_MCP_CLIENT_SECRET must be set in .env (from your HubSpot MCP connector)."
        )
    callback = _LoopbackCallback()
    provider = HubSpotOAuthProvider(
        server_url=SERVER_URL,
        client_metadata=OAuthClientMetadata(
            client_name="Renewal Risk Orchestrator",
            redirect_uris=[REDIRECT_URI],
            token_endpoint_auth_method="client_secret_post",
            grant_types=["authorization_code", "refresh_token"],
        ),
        storage=FileTokenStorage(token_path(settings), client_id, secret),
        redirect_handler=callback.open_browser if interactive else _login_required,
        callback_handler=callback.wait if interactive else _login_required,
    )
    return create_mcp_http_client(auth=provider)


async def _login_required(*_args) -> AuthorizationCodeResult:
    raise HubSpotLoginRequired("HubSpot's MCP server needs you to sign in. Run `rro hubspot-login` once, then retry.")


class _LoopbackCallback:
    """Receives HubSpot's redirect on localhost and returns the authorization code."""

    def __init__(self):
        self._result: asyncio.Future[AuthorizationCodeResult] | None = None
        self._server: asyncio.base_events.Server | None = None

    async def open_browser(self, authorization_url: str) -> None:
        loop = asyncio.get_running_loop()
        self._result = loop.create_future()
        port = urlparse(REDIRECT_URI).port
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", port)
        print(f"Opening HubSpot in your browser. If it doesn't open, visit:\n{authorization_url}\n")
        webbrowser.open(authorization_url)

    async def wait(self) -> AuthorizationCodeResult:
        try:
            return await asyncio.wait_for(self._result, LOGIN_TIMEOUT_SECONDS)
        finally:
            self._server.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = (await reader.readline()).decode(errors="replace")
        while (await reader.readline()) not in (b"\r\n", b"\n", b""):
            pass  # skip headers
        target = request_line.split(" ")[1] if " " in request_line else "/"
        url = urlparse(target)
        if url.path != urlparse(REDIRECT_URI).path:
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        else:
            params = {k: v[0] for k, v in parse_qs(url.query).items()}
            ok = "code" in params
            body = (
                "<h2>Signed in to HubSpot.</h2><p>You can close this tab and return to the terminal.</p>"
                if ok
                else f"<h2>HubSpot sign-in failed</h2><p>{params.get('error_description') or params.get('error') or 'No code returned.'}</p>"
            ).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
            if not self._result.done():
                if ok:
                    self._result.set_result(AuthorizationCodeResult(code=params["code"], state=params.get("state"), iss=params.get("iss")))
                else:
                    self._result.set_exception(HubSpotLoginRequired(f"HubSpot sign-in failed: {params}"))
        await writer.drain()
        writer.close()


def describe_tools(tools) -> list[dict]:
    """A compact view of a server's tools, for inspecting HubSpot's schemas."""
    return [
        {
            "name": t.name,
            "read_only": t.annotations.read_only_hint if t.annotations else None,
            "description": (t.description or "")[:300],
            "input_schema": t.input_schema,
        }
        for t in tools
    ]


def save_tool_inventory(settings: Settings, tools) -> Path:
    path = settings.data_dir / "hubspot_mcp_tools.json"
    path.write_text(json.dumps(describe_tools(tools), indent=2), encoding="utf-8")
    return path
