"""Per-system permission scopes, loaded from config/policy.yaml.

Every tool call goes through Policy.authorize before it reaches an MCP
server. The model never gets to argue with this: denied calls return an
error result and are written to the audit log like any other call.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

Scope = Literal["read", "write"]
# (approval_id, system, tool, args) -> is there an approved action matching exactly this call?
ApprovalCheck = Callable[[str, str, str, dict[str, Any]], bool]


@dataclass(frozen=True)
class Decision:
    allowed: bool
    scope: str  # read | write | unlisted
    reason: str


def _no_approvals(approval_id: str, system: str, tool: str, args: dict[str, Any]) -> bool:
    return False


class Policy:
    def __init__(self, scopes: dict[tuple[str, str], Scope], approval_check: ApprovalCheck = _no_approvals):
        self._scopes = scopes
        self._approval_check = approval_check

    @classmethod
    def load(cls, path: Path, approval_check: ApprovalCheck = _no_approvals) -> Policy:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        scopes: dict[tuple[str, str], Scope] = {}
        for system, rules in (raw.get("systems") or {}).items():
            for scope in ("read", "write"):
                for tool in rules.get(scope) or []:
                    if (system, tool) in scopes:
                        raise ValueError(f"{system}.{tool} is listed under more than one scope in {path.name}")
                    scopes[(system, tool)] = scope
        return cls(scopes, approval_check)

    def scope_of(self, system: str, tool: str) -> Scope | None:
        return self._scopes.get((system, tool))

    def authorize(
        self, system: str, tool: str, args: dict[str, Any], *, actor: str, approval_id: str | None = None
    ) -> Decision:
        scope = self.scope_of(system, tool)
        if scope is None:
            return Decision(False, "unlisted", f"{system}.{tool} is not in the policy (deny by default)")
        if scope == "read":
            return Decision(True, "read", "read scope")
        if actor != "executor":
            return Decision(False, "write", "write tools can only be run by the approval executor, never by the agent")
        if not approval_id or not self._approval_check(approval_id, system, tool, args):
            return Decision(False, "write", "no human-approved action matches this exact call")
        return Decision(True, "write", f"approved action {approval_id}")
