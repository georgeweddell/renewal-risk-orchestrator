"""Support tickets MCP server.

The tools answer the questions the agent actually has ("what's open for this
account, and how bad is it?") rather than wrapping a raw issue-search API.
The agent makes fewer, more reliable calls, and replacing the ticketing
system (GitHub Issues today, Jira or Zendesk tomorrow) only means writing a
new backend.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from mcp_servers.tickets.backends import load_backend

mcp = MCPServer(
    "tickets",
    instructions="Support tickets by account. Accounts are identified by their account_slug from the CRM.",
)
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
PRIORITY_ORDER = {"P1": 0, "P2": 1, "P3": 2}


def _age_days(created_at: str) -> int:
    return (datetime.now(UTC) - datetime.fromisoformat(created_at)).days


def _summarise(issue: dict) -> dict:
    return {
        "number": issue["number"],
        "title": issue["title"],
        "priority": issue["priority"],
        "state": issue["state"],
        "age_days": _age_days(issue["created_at"]),
        "labels": issue["labels"],
        "url": issue["url"],
    }


@mcp.tool(annotations=READ_ONLY)
def list_issues(account_id: str, state: Literal["open", "closed", "all"] = "open") -> dict:
    """List an account's support issues, most severe first, with a count of
    open issues by priority (P1 = critical, P2 = major, P3 = minor).

    account_id is the account_slug from the CRM, e.g. "halcyon-robotics".
    """
    issues = load_backend().list_issues(account_id)
    open_issues = [i for i in issues if i["state"] == "open"]
    shown = issues if state == "all" else [i for i in issues if i["state"] == state]
    shown.sort(key=lambda i: (PRIORITY_ORDER.get(i["priority"], 9), i["created_at"]))
    open_p1 = [i for i in open_issues if i["priority"] == "P1"]
    return {
        "account_id": account_id,
        "summary": {
            "open_p1": len(open_p1),
            "open_p2": sum(i["priority"] == "P2" for i in open_issues),
            "open_p3": sum(i["priority"] == "P3" for i in open_issues),
            "oldest_open_p1_days": max((_age_days(i["created_at"]) for i in open_p1), default=None),
        },
        "issues": [_summarise(i) for i in shown],
    }


@mcp.tool(annotations=READ_ONLY)
def get_issue(number: int) -> dict:
    """Get one issue in full, including its description."""
    issue = load_backend().get_issue(number)
    if issue is None:
        raise ToolError(f"No issue #{number}")
    return _summarise(issue) | {"body": issue["body"], "created_at": issue["created_at"], "closed_at": issue["closed_at"]}
