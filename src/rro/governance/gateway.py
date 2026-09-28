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

import json
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import yaml
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
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
class CallOutcome:
    text: str
    is_error: bool
    decision: Decision


def resolve_launch(settings: Settings) -> dict[str, StdioServerParameters]:
    """Read config/servers.yaml and build the launch command for each system's chosen backend."""
    raw = yaml.safe_load((settings.config_dir / "servers.yaml").read_text(encoding="utf-8"))["systems"]
    fill = {"{python}": sys.executable, "{mock_db}": str(settings.mock_db)}
    launches: dict[str, StdioServerParameters] = {}
    for system in SYSTEMS:
        backend = settings.backend_for(system)
        entry = (raw.get(system) or {}).get(backend)
        if entry is None:
            raise GatewayConfigError(
                f"No '{backend}' backend is configured for '{system}' in config/servers.yaml. "
                f"Live connections arrive in Phase 3; set {system.upper()}_BACKEND=mock for now."
            )
        env = {k: _expand(str(v), fill) for k, v in (entry.get("env") or {}).items()}
        launches[system] = StdioServerParameters(
            command=_expand(entry["command"], fill),
            args=[_expand(str(a), fill) for a in entry.get("args", [])],
            env=env,
            cwd=settings.rro_home,
        )
    return launches


def _expand(value: str, fill: dict[str, str]) -> str:
    for placeholder, replacement in fill.items():
        value = value.replace(placeholder, replacement)
    return os.path.expandvars(value)  # ${VAR} references to the orchestrator's environment


class ToolGateway:
    def __init__(self, settings: Settings, policy: Policy, store: Store):
        self.settings = settings
        self.policy = policy
        self.store = store
        self._clients: dict[str, Client] = {}
        self._specs: dict[str, ToolSpec] = {}
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> ToolGateway:
        launches = resolve_launch(self.settings)
        if any(self.settings.backend_for(s) == "mock" for s in launches) and not self.settings.mock_db.exists():
            raise GatewayConfigError(f"Mock data not found at {self.settings.mock_db}. Run `rro seed` first.")
        self.settings.logs_dir.mkdir(parents=True, exist_ok=True)

        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        try:
            for system, params in launches.items():
                await self._connect(system, params)
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._stack is not None:
            await self._stack.__aexit__(*exc_info)

    async def _connect(self, system: str, params: StdioServerParameters) -> None:
        # Server stderr goes to data/logs/<system>.log rather than cluttering the terminal.
        errlog = self._stack.enter_context(open(self.settings.logs_dir / f"{system}.log", "a", encoding="utf-8"))
        client = Client(stdio_client(params, errlog=errlog), client_info=CLIENT_INFO)
        await self._stack.enter_async_context(client)
        self._clients[system] = client

        cursor = None
        while True:
            page = await client.list_tools(cursor=cursor)
            for tool in page.tools:
                spec = ToolSpec(
                    system=system,
                    name=tool.name,
                    description=(tool.description or "").strip(),
                    input_schema=tool.input_schema,
                    scope=self.policy.scope_of(system, tool.name) or "unlisted",
                    read_only_hint=tool.annotations.read_only_hint if tool.annotations else None,
                )
                self._specs[spec.qualified_name] = spec
            if not (cursor := page.next_cursor):
                break

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
        if spec is None:
            decision = Decision(False, "unlisted", f"no tool named '{qualified_name}' on any connected server")
        else:
            decision = self.policy.authorize(system, tool, args, actor=actor, approval_id=approval_id)

        entry = AuditEntry(
            run_id=run_id, actor=actor, system=system or "unknown", tool=tool or qualified_name,
            scope=decision.scope, decision="allowed" if decision.allowed else "denied",
            reason=decision.reason, args=args, approval_id=approval_id,
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
