"""CRM MCP server.

Exposes the subset of HubSpot's CRM tools this project uses, with the same
names HubSpot's own MCP server uses: search_crm_objects, get_crm_objects,
search_owners and manage_crm_objects. Parameters follow the HubSpot CRM v3
API (filterGroups, camelCase names), so the orchestrator, policy and prompt
are identical whichever backend is behind it:

  CRM_BACKEND=mock     seeded SQLite data (mock mode)
  CRM_BACKEND=hubspot  a HubSpot account, via the REST API and a private app token
"""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from mcp_servers.crm.backends import load_backend

ObjectType = Literal["companies", "deals"]
Operator = Literal[
    "EQ", "NEQ", "LT", "LTE", "GT", "GTE", "BETWEEN", "IN", "NOT_IN",
    "HAS_PROPERTY", "NOT_HAS_PROPERTY", "CONTAINS_TOKEN", "NOT_CONTAINS_TOKEN",
]  # fmt: skip


class Filter(BaseModel):
    propertyName: str = Field(
        description="Property to filter on. Use 'associations.company' or 'associations.deal' "
        "to match records associated with a given record ID."
    )
    operator: Operator
    value: str | None = None
    highValue: str | None = Field(default=None, description="Upper bound, for BETWEEN.")
    values: list[str] | None = Field(default=None, description="Candidate values, for IN / NOT_IN.")


class FilterGroup(BaseModel):
    filters: list[Filter]


mcp = MCPServer(
    "crm",
    instructions="HubSpot CRM. Companies carry an `account_slug` property: the account's ID in other systems.",
)
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


@mcp.tool(annotations=READ_ONLY)
def search_crm_objects(
    objectType: ObjectType,
    filterGroups: list[FilterGroup] | None = None,
    query: str | None = None,
    properties: list[str] | None = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 10,
) -> dict:
    """Search CRM records of one type.

    Filter groups are OR'ed together; filters inside a group are AND'ed.
    `query` is a text search on the main name fields (company name and
    domain; deal name). Returns the default properties unless `properties`
    names specific ones.
    """
    groups = [g.model_dump(exclude_none=True) for g in filterGroups] if filterGroups else None
    return load_backend().search(objectType, groups, query, properties, limit)


@mcp.tool(annotations=READ_ONLY)
def get_crm_objects(
    objectType: ObjectType,
    objectIds: Annotated[list[str], Field(min_length=1, max_length=100)],
    properties: list[str] | None = None,
    associations: list[ObjectType] | None = None,
) -> dict:
    """Fetch CRM records by ID, optionally with the IDs of associated records
    (for example a company's deals)."""
    return load_backend().get(objectType, objectIds, properties, associations)


@mcp.tool(annotations=READ_ONLY)
def search_owners(query: str | None = None) -> dict:
    """List CRM owners (users who can own records), optionally filtered by name or email."""
    return load_backend().owners(query)


@mcp.tool(annotations=WRITES)
def manage_crm_objects(
    objectType: ObjectType,
    properties: dict[str, str],
    objectId: str | None = None,
) -> dict:
    """Update a CRM record's properties (or create a record when objectId is omitted)."""
    return load_backend().manage(objectType, properties, objectId)
