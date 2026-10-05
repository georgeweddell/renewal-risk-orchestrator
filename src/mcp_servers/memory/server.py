"""Renewal decision memory, over MCP.

Read-only: the agent consults precedent here before recommending anything.
New decisions are recorded by the orchestrator's approval executor, never by
the agent.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from mcp_servers.memory.store import MemoryStore

Driver = Literal["usage_drop", "open_p1", "open_p2", "renewal_soon"]
Band = Literal["healthy", "at-risk", "critical"]
ActionType = Literal["crm_risk_update", "pricing_exception", "evidence_purchase"]

mcp = MCPServer(
    "memory",
    instructions="Past renewal decisions: what was proposed, what a human decided and why, and what happened next.",
)
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def _store() -> MemoryStore:
    path = Path(os.environ.get("RRO_MEMORY_DB", ""))
    if not path.is_file():
        raise ToolError("Decision memory isn't available (RRO_MEMORY_DB is missing). Run `rro seed`.")
    return MemoryStore(path)


@mcp.tool(annotations=READ_ONLY)
def get_account_history(account_id: str) -> dict:
    """Every past renewal decision for one account, newest first, including
    rejected proposals with the approver's reasoning and the eventual outcome.

    account_id is the account_slug from the CRM, e.g. "halcyon-robotics".
    """
    decisions = _store().for_account(account_id)
    return {"account_id": account_id, "count": len(decisions), "decisions": [d.to_dict() for d in decisions]}


@mcp.tool(annotations=READ_ONLY)
def find_similar_decisions(
    drivers: Annotated[list[Driver], Field(min_length=1)],
    band: Band | None = None,
    action_type: ActionType | None = None,
    exclude_account: str | None = None,
    limit: Annotated[int, Field(ge=1, le=10)] = 5,
) -> dict:
    """Past decisions for any account with a similar risk profile, most similar
    first, with their outcomes. Use the `drivers` returned by
    score_renewal_risk. Similarity is the overlap of risk drivers, plus a bonus
    for the same risk band.
    """
    wanted = set(drivers)
    matches = []
    for d in _store().all():
        if exclude_account and d.account_slug == exclude_account:
            continue
        if action_type and d.action_type != action_type:
            continue
        shared = wanted & set(d.drivers)
        if not shared:
            continue
        similarity = len(shared) / len(wanted | set(d.drivers)) + (0.25 if band and d.risk_band == band else 0)
        matches.append((round(similarity, 2), sorted(shared), d))
    matches.sort(key=lambda m: (m[0], m[2].decided_on), reverse=True)
    return {
        "query": {"drivers": sorted(wanted), "band": band, "action_type": action_type},
        "matches": [
            d.to_dict() | {"similarity": similarity, "shared_drivers": shared}
            for similarity, shared, d in matches[:limit]
        ],
    }
