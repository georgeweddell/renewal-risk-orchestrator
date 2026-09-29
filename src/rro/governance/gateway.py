"""The Tool Gateway: this app's MCP client host, and its governance choke point.

How it works:
  1. On startup it launches each system's MCP server as a subprocess and
     connects over stdio (JSON-RPC on the child's stdin/stdout).
  2. It asks every server for its tools (`tools/list`), namespaces them as
     `<system>__<tool>`, and labels each one with its scope from the policy.
  3. The model is only ever shown read-scope tools. Write tools stay hidden.
  4. Every call, allowed or denied, goes through Policy.authorize and is
     written to the append-only audit log.

We run the MCP client ourselves, rather than handing server URLs to the
Claude API's built-in MCP connector, precisely so that steps 3 and 4 happen
on every call. With the connector, tool calls would run on Anthropic's side
and never pass through this code.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import yaml
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CallToolResult, Implementation, TextContent

from rro.db import AuditEntry, Store
from rro.governance.policy import Decision, Policy
from rro.settings import SYSTEMS, Settings

SEP = "__"
CLIENT_INFO = Implementation(name="renewal-risk-orchestrator", version="0.1.0")
PREVIEW_CHARS = 600


class GatewayConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolSpec:
    system: str
    name: str
    description: str
    input_schema: dict[str, Any]
    scope: str  # read | write | unlisted
    read_only_hint: bool | None  # what the server says about itself

    @property
    def qualified_name(self) -> str:
        return f"{self.system}{SEP}{self.name}"

    @property
    def exposed_to_model(self) -> bool:
        return self.scope == "read"

    @property
    def hint_conflict(self) -> bool:
        """The server's self-description disagrees with our policy. Worth a look, never trusted."""
        return (self.scope == "read" and self.read_only_hint is False) or (
            self.scope == "write" and self.read_only_hint is True
        )


@dataclass(frozen=True)
class RemoteServer:
    """An MCP server reached over Streamable HTTP rather than launched as a subprocess."""

    url: str
    auth: str | None = None  # "hubspot_oauth" is the only scheme so far


@dataclass(frozen=True)
class CallOutcome:
    text: str
    is_error: bool
    decision: Decision


def _server_entries(settings: Settings) -> dict[str, dict]:
    """The config/servers.yaml entry for each system's chosen backend."""
    raw = yaml.safe_load((settings.config_dir / "servers.yaml").read_text(encoding="utf-8"))["systems"]
    entries = {}
    for system in SYSTEMS:
        backend = settings.backend_for(system)
        system_config = raw.get(system) or {}
        entry = system_config if "command" in system_config else system_config.get(backend)
        if entry is None:
            raise GatewayConfigError(
                f"No '{backend}' backend is configured for '{system}' in config/servers.yaml. "
                f"Set {system.upper()}_BACKEND=mock, or add a '{backend}' entry."
            )
        entries[system] = entry
    return entries


def resolve_launch(settings: Settings) -> dict[str, StdioServerParameters | RemoteServer]:
    """Build the launch command (or remote URL) for each system's chosen backend."""
    fill = {"{python}": sys.executable, "{mock_db}": str(settings.mock_db), "{memory_db}": str(settings.memory_db)}
    launches: dict[str, StdioServerParameters | RemoteServer] = {}
    for system, entry in _server_entries(settings).items():
        if "url" in entry:
            launches[system] = RemoteServer(url=entry["url"], auth=entry.get("auth"))
            continue
        expand = lambda value: _expand(str(value), fill, settings, system)  # noqa: E731
        launches[system] = StdioServerParameters(
            command=expand(entry["command"]),
            args=[expand(a) for a in entry.get("args", [])],
            env={k: expand(v) for k, v in (entry.get("env") or {}).items()},
            cwd=settings.rro_home,
        )
    return launches


def _expand(value: str, fill: dict[str, str], settings: Settings, system: str) -> str:
    """Fill {placeholders}, then ${NAME} references to settings from .env (or the environment)."""
    for placeholder, replacement in fill.items():
        value = value.replace(placeholder, replacement)

    def lookup(match: re.Match) -> str:
        name = match.group(1)
        resolved = settings.value(name) or os.environ.get(name)
        if not resolved:
            raise GatewayConfigError(f"{name} is not set. The '{system}' server needs it in live mode; add it to .env.")
        return resolved

    return re.sub(r"\$\{([A-Z0-9_]+)\}", lookup, value)


class ToolGateway:
    def __init__(self, settings: Settings, policy: Policy, store: Store):
        self.settings = settings
        self.policy = policy
        self.store = store
        self._clients: dict[str, Client] = {}
        self._specs: dict[str, ToolSpec] = {}
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._stripped: dict[str, set[str]] = {}

    async def __aenter__(self) -> ToolGateway:
        launches = resolve_launch(self.settings)
        # Arguments a server accepts but the policy never lets through (see strip_arguments in servers.yaml).
        self._stripped = {s: set(e.get("strip_arguments") or []) for s, e in _server_entries(self.settings).items()}
        needs_mock = any(self.settings.backend_for(s) == "mock" for s in launches)
        for path, needed in ((self.settings.mock_db, needs_mock), (self.settings.memory_db, True)):
            if needed and not path.exists():
                raise GatewayConfigError(f"{path.name} not found in {path.parent}. Run `rro seed` first.")
        self.settings.logs_dir.mkdir(parents=True, exist_ok=True)

        # Servers start in parallel (each is a cold Python process). Each connection lives in its
        # own task, because an MCP client's task groups must be entered and exited in one task.
        self._stop = asyncio.Event()
        ready = {system: asyncio.get_running_loop().create_future() for system in launches}
        self._tasks = [
            asyncio.create_task(self._hold_connection(system, params, ready[system]), name=f"mcp-{system}")
            for system, params in launches.items()
        ]
        try:
            for system, listed in zip(ready, await asyncio.gather(*ready.values()), strict=True):
                self._register(system, listed)
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc_info) -> None:
        self._stop.set()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _hold_connection(
        self, system: str, params: StdioServerParameters | RemoteServer, ready: asyncio.Future
    ) -> None:
        """Connect, report the server's tools, then keep the connection open until the gateway closes."""
        try:
            async with AsyncExitStack() as stack:
                if isinstance(params, RemoteServer):
                    http = None
                    if params.auth == "hubspot_oauth":
                        from rro import hubspot_mcp  # only needed when HubSpot's own server is configured

                        http = await stack.enter_async_context(hubspot_mcp.http_client(self.settings))
                    transport = streamable_http_client(params.url, http_client=http)
                else:
                    # Server stderr goes to data/logs/<system>.log rather than cluttering the terminal.
                    errlog = stack.enter_context(open(self.settings.logs_dir / f"{system}.log", "a", encoding="utf-8"))
                    transport = stdio_client(params, errlog=errlog)
                client = await stack.enter_async_context(Client(transport, client_info=CLIENT_INFO))
                self._clients[system] = client
                tools, cursor = [], None
                while True:
                    page = await client.list_tools(cursor=cursor)
                    tools.extend(page.tools)
                    if not (cursor := page.next_cursor):
                        break
                ready.set_result(tools)
                await self._stop.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            elif not isinstance(exc, asyncio.CancelledError):
                raise

    def _register(self, system: str, tools: list) -> None:
        for tool in tools:
            spec = ToolSpec(
                system=system,
                name=tool.name,
                description=(tool.description or "").strip(),
                input_schema=_without(tool.input_schema, self._stripped.get(system, set())),
                scope=self.policy.scope_of(system, tool.name) or "unlisted",
                read_only_hint=tool.annotations.read_only_hint if tool.annotations else None,
            )
            self._specs[spec.qualified_name] = spec

    # --- what the model sees ----------------------------------------------------
    def inventory(self) -> list[ToolSpec]:
        return sorted(self._specs.values(), key=lambda s: s.qualified_name)

    def model_tools(self) -> list[dict[str, Any]]:
        """Claude tool definitions for read-scope tools only, in a stable order (keeps the prompt cache warm)."""
        return [
            {
                "name": spec.qualified_name,
                "description": f"[{spec.system}] {spec.description}",
                "input_schema": spec.input_schema,
            }
            for spec in self.inventory()
            if spec.exposed_to_model
        ]

    # --- calls ------------------------------------------------------------------
    async def call(
        self,
        qualified_name: str,
        args: dict[str, Any],
        *,
        run_id: str | None,
        actor: str = "agent",
        approval_id: str | None = None,
    ) -> CallOutcome:
        spec = self._specs.get(qualified_name)
        system, _, tool = qualified_name.partition(SEP)
        # Arguments the policy never forwards are removed before anything else, even if sent.
        removed = sorted(set(args) & self._stripped.get(system, set()))
        if removed:
            args = {k: v for k, v in args.items() if k not in removed}
        if spec is None:
            decision = Decision(False, "unlisted", f"no tool named '{qualified_name}' on any connected server")
        else:
            decision = self.policy.authorize(system, tool, args, actor=actor, approval_id=approval_id)

        entry = AuditEntry(
            run_id=run_id, actor=actor, system=system or "unknown", tool=tool or qualified_name,
            scope=decision.scope, decision="allowed" if decision.allowed else "denied",
            reason=decision.reason + (f"; removed {', '.join(removed)} (never forwarded)" if removed else ""),
            args=args, approval_id=approval_id,
        )  # fmt: skip

        if not decision.allowed:
            entry.is_error = True
            self.store.add_audit(entry)
            return CallOutcome(f"Denied by policy: {decision.reason}", True, decision)

        started = perf_counter()
        try:
            result = await self._clients[system].call_tool(tool, args)
            text, is_error = _render(result), bool(result.is_error)
        except Exception as exc:  # a crashed or unreachable server is a failed call, not a crashed run
            text, is_error = f"{type(exc).__name__}: {exc}", True
        entry.latency_ms = round((perf_counter() - started) * 1000)
        entry.is_error = is_error
        entry.result_preview = text[:PREVIEW_CHARS]
        self.store.add_audit(entry)
        return CallOutcome(text, is_error, decision)


def _render(result: CallToolResult) -> str:
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    if texts:
        return "\n".join(texts)
    if result.structured_content is not None:
        return json.dumps(result.structured_content)
    return "(no content)"


def _without(schema: dict[str, Any], names: set[str]) -> dict[str, Any]:
    """A tool's input schema minus stripped arguments, so the model never sees them."""
    if not names or "properties" not in schema:
        return schema
    return schema | {
        "properties": {k: v for k, v in schema["properties"].items() if k not in names},
        "required": [r for r in schema.get("required", []) if r not in names],
    }
