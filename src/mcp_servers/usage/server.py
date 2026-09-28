"""Product usage MCP server.

One task-shaped tool: the usage trend for an account, with the week-by-week
series and a summary computed the same way every time. The agent never
writes analytics queries itself, so the numbers it reasons about are
consistent between runs and between mock and live mode.
"""

from __future__ import annotations

from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from mcp_servers.usage.backends import load_backend

mcp = MCPServer(
    "usage",
    instructions="Product usage by account. Accounts are identified by their account_slug from the CRM.",
)
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

RECENT_WEEKS = 4
PRIOR_WEEKS = 8
STABLE_BAND_PCT = 5.0


@mcp.tool(annotations=READ_ONLY)
def get_usage_trend(account_id: str, weeks: Annotated[int, Field(ge=12, le=26)] = 12) -> dict:
    """Weekly active users for an account, plus a trend summary: the average
    of the last 4 weeks compared with the 8 weeks before.

    account_id is the account_slug from the CRM, e.g. "halcyon-robotics".
    """
    series = load_backend().weekly_active_users(account_id, weeks)
    if len(series) < RECENT_WEEKS + PRIOR_WEEKS:
        raise ToolError(
            f"Only {len(series)} weeks of usage data for '{account_id}'; "
            f"the trend needs {RECENT_WEEKS + PRIOR_WEEKS}. Check the account_id."
        )
    users = [w["active_users"] for w in series]
    recent = sum(users[-RECENT_WEEKS:]) / RECENT_WEEKS
    prior = sum(users[-(RECENT_WEEKS + PRIOR_WEEKS) : -RECENT_WEEKS]) / PRIOR_WEEKS
    pct_change = round((recent - prior) / prior * 100, 1) if prior else 0.0
    if pct_change <= -STABLE_BAND_PCT:
        direction = "declining"
    elif pct_change >= STABLE_BAND_PCT:
        direction = "growing"
    else:
        direction = "stable"
    return {
        "account_id": account_id,
        "metric": "weekly_active_users",
        "weeks": series,
        "summary": {
            "recent_4wk_avg": round(recent, 1),
            "prior_8wk_avg": round(prior, 1),
            "pct_change": pct_change,
            "direction": direction,
            "peak_week": max(series, key=lambda w: w["active_users"])["week_start"],
            "definition": "pct_change = (avg WAU last 4 weeks - avg WAU prior 8 weeks) / avg WAU prior 8 weeks",
        },
    }
