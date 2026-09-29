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
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx2
from mcp.client.auth import OAuthClientProvider
from mcp.shared._httpx_utils import create_mcp_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from rro.settings import Settings

SERVER_URL = "https://mcp.hubspot.com"
# Must match the redirect URL on the MCP connector exactly.
REDIRECT_URI = "http://localhost:8912/oauth/callback"
LOGIN_TIMEOUT_SECONDS = 300


class HubSpotLoginRequired(RuntimeError):
    pass


class FileTokenStorage:
    """The SDK's TokenStorage: tokens on disk, client credentials from settings."""

    def __init__(self, path: Path, client_id: str, client_secret: str):
        self.path = path
        self.client = OAuthClientInformationFull(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uris=[REDIRECT_URI],
            token_endpoint_auth_method="client_secret_post",  # the only method HubSpot advertises
            grant_types=["authorization_code", "refresh_token"],
        )

    async def get_tokens(self) -> OAuthToken | None:
        if not self.path.exists():
            return None
        return OAuthToken.model_validate_json(self.path.read_text(encoding="utf-8"))

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(tokens.model_dump_json(), encoding="utf-8")

    async def get_client_info(self) -> OAuthClientInformationFull:
        return self.client

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        pass  # pre-registered; nothing to persist


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
    provider = OAuthClientProvider(
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
