"""Paid evidence MCP server: buys third-party data over x402.

One tool, and it spends money, so config/policy.yaml lists it under `write`:
the model never sees it, and only the approval executor can run it, for a
payment whose exact terms were approved (by a human, or by the spend policy
under its threshold). The tool still re-checks the seller's live price request
against those terms before signing anything (terms.py).
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from mcp_servers.evidence.backends import EvidenceError, load_backend
from mcp_servers.evidence.terms import ApprovedTerms, TermsMismatch

mcp = MCPServer(
    "evidence",
    instructions="Buys third-party evidence (e.g. company news) from x402 sellers, on a testnet, within approved terms.",
)
SPENDS_MONEY = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

_backend = None


@mcp.tool(annotations=SPENDS_MONEY)
async def buy_evidence(
    url: str,
    pay_to: str,
    network: str,
    asset: str,
    max_amount_units: Annotated[int, Field(gt=0)],
) -> dict:
    """Buy the data at `url`, paying at most `max_amount_units` of `asset` to `pay_to` on `network`.

    Fetches the seller's 402, and signs only if one of its payment options
    matches these terms exactly (same payee, token and network; amount at most
    the maximum). Returns the data, what was paid, and the settlement's
    transaction hash.
    """
    global _backend
    try:
        _backend = _backend or load_backend()
        purchase = await _backend.buy(url, ApprovedTerms(pay_to, network, asset, max_amount_units))
    except (TermsMismatch, EvidenceError) as exc:
        raise ToolError(str(exc)) from exc
    return purchase.as_dict()
