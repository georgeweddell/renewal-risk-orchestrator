"""Mock CRM MCP server.

Mirrors the subset of HubSpot's remote MCP server (mcp.hubspot.com) that this
project uses: search_crm_objects, get_crm_objects, search_owners and
manage_crm_objects. The tool names match HubSpot's, so the orchestrator,
policy and prompt are identical in mock and live mode. Parameters follow the
HubSpot CRM v3 search API (filterGroups, camelCase names); they get reconciled
with the live server's exact schemas in Phase 3.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from mcp_servers import mockdata

ObjectType = Literal["companies", "deals"]
Operator = Literal[
    "EQ", "NEQ", "LT", "LTE", "GT", "GTE", "BETWEEN", "IN", "NOT_IN",
    "HAS_PROPERTY", "NOT_HAS_PROPERTY", "CONTAINS_TOKEN", "NOT_CONTAINS_TOKEN",
]  # fmt: skip

# The properties HubSpot returns when a request doesn't name any.
DEFAULT_PROPERTIES: dict[str, list[str]] = {
    "companies": ["name", "domain", "industry", "account_slug", "arr", "hubspot_owner_id"],
    "deals": ["dealname", "amount", "closedate", "dealstage", "pipeline", "dealtype", "hubspot_owner_id"],
}
# Fields `query` matches against, like HubSpot's default searchable properties.
QUERY_FIELDS: dict[str, list[str]] = {
    "companies": ["name", "domain", "account_slug"],
    "deals": ["dealname"],
}
SINGULAR = {"companies": "company", "deals": "deal"}


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
    "mock-crm",
    instructions="Mock HubSpot CRM. Companies carry an `account_slug` property: the account's ID in other systems.",
)
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


def _record(row, properties: list[str] | None) -> dict:
    stored: dict[str, str] = json.loads(row["properties"])
    wanted = properties or DEFAULT_PROPERTIES[row["object_type"]]
    return {
        "id": row["id"],
        "properties": {name: stored.get(name) for name in wanted},
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _associated_ids(conn, from_type: str, from_id: str, to_type: str) -> list[str]:
    rows = conn.execute(
        "SELECT to_id FROM crm_associations WHERE from_type=? AND from_id=? AND to_type=? ORDER BY to_id",
        (from_type, from_id, to_type),
    )
    return [r["to_id"] for r in rows]


def _compare(actual: str, expected: str) -> int:
    """Numbers compare numerically; everything else (including ISO dates) as strings."""
    try:
        a, b = float(actual), float(expected)
    except ValueError:
        a, b = actual.lower(), expected.lower()
    return (a > b) - (a < b)


def _matches(f: Filter, value: str | None) -> bool:
    op = f.operator
    if op == "HAS_PROPERTY":
        return value not in (None, "")
    if op == "NOT_HAS_PROPERTY":
        return value in (None, "")
    if value is None:
        return op in ("NEQ", "NOT_IN", "NOT_CONTAINS_TOKEN")
    if op == "IN":
        return any(_compare(value, v) == 0 for v in f.values or [])
    if op == "NOT_IN":
        return all(_compare(value, v) != 0 for v in f.values or [])
    if op in ("CONTAINS_TOKEN", "NOT_CONTAINS_TOKEN"):
        found = (f.value or "").strip("*").lower() in value.lower()
        return found if op == "CONTAINS_TOKEN" else not found
    if f.value is None:
        raise ToolError(f"Operator {op} on '{f.propertyName}' needs a value.")
    cmp = _compare(value, f.value)
    if op == "BETWEEN":
        if f.highValue is None:
            raise ToolError("BETWEEN needs both value and highValue.")
        return cmp >= 0 and _compare(value, f.highValue) <= 0
    return {"EQ": cmp == 0, "NEQ": cmp != 0, "LT": cmp < 0, "LTE": cmp <= 0, "GT": cmp > 0, "GTE": cmp >= 0}[op]


def _filter_value(conn, object_type: str, record_id: str, props: dict, name: str) -> str | None:
    if name.startswith("associations."):
        other = name.split(".", 1)[1]
        other_type = {"company": "companies", "deal": "deals"}.get(other)
        if other_type is None:
            raise ToolError(f"Unsupported association filter '{name}'. Use associations.company or associations.deal.")
        ids = _associated_ids(conn, object_type, record_id, other_type)
        return ids[0] if len(ids) == 1 else (",".join(ids) or None)
    return props.get(name)


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
    `query` is a case-insensitive text match on the main name fields
    (company name, domain, account_slug; deal name). Returns the default
    properties unless `properties` names specific ones.
    """
    with mockdata.session() as conn:
        rows = conn.execute(
            "SELECT * FROM crm_objects WHERE object_type=? ORDER BY CAST(id AS INTEGER)", (objectType,)
        ).fetchall()
        results = []
        for row in rows:
            props = json.loads(row["properties"])
            if query and not any(query.lower() in (props.get(k) or "").lower() for k in QUERY_FIELDS[objectType]):
                continue
            if filterGroups and not any(
                all(_matches(f, _filter_value(conn, objectType, row["id"], props, f.propertyName)) for f in group.filters)
                for group in filterGroups
            ):
                continue
            results.append(_record(row, properties))
    return {"total": len(results), "results": results[:limit]}


@mcp.tool(annotations=READ_ONLY)
def get_crm_objects(
    objectType: ObjectType,
    objectIds: Annotated[list[str], Field(min_length=1, max_length=100)],
    properties: list[str] | None = None,
    associations: list[ObjectType] | None = None,
) -> dict:
    """Fetch CRM records by ID, optionally with the IDs of associated records
    (for example a company's deals)."""
    with mockdata.session() as conn:
        results, missing = [], []
        for object_id in objectIds:
            row = conn.execute(
                "SELECT * FROM crm_objects WHERE object_type=? AND id=?", (objectType, object_id)
            ).fetchone()
            if row is None:
                missing.append(object_id)
                continue
            record = _record(row, properties)
            if associations:
                record["associations"] = {
                    other: _associated_ids(conn, objectType, object_id, other) for other in associations
                }
            results.append(record)
    response: dict = {"results": results}
    if missing:
        response["errors"] = [{"message": f"No {SINGULAR[objectType]} with ID {i}"} for i in missing]
    return response


@mcp.tool(annotations=READ_ONLY)
def search_owners(query: str | None = None) -> dict:
    """List CRM owners (account managers), optionally filtered by name or email."""
    with mockdata.session() as conn:
        rows = conn.execute("SELECT * FROM crm_owners ORDER BY id").fetchall()
    owners = [
        {"id": r["id"], "email": r["email"], "firstName": r["first_name"], "lastName": r["last_name"]}
        for r in rows
    ]
    if query:
        q = query.lower()
        owners = [o for o in owners if q in f"{o['firstName']} {o['lastName']} {o['email']}".lower()]
    return {"results": owners}


@mcp.tool(annotations=WRITES)
def manage_crm_objects(
    objectType: ObjectType,
    properties: dict[str, str],
    objectId: str | None = None,
) -> dict:
    """Update a CRM record's properties (or create a record when objectId is omitted)."""
    now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    with mockdata.session() as conn:
        if objectId is None:
            next_id = conn.execute(
                "SELECT COALESCE(MAX(CAST(id AS INTEGER)), 0) + 1 FROM crm_objects WHERE object_type=?", (objectType,)
            ).fetchone()[0]
            objectId = str(next_id)
            conn.execute(
                "INSERT INTO crm_objects VALUES (?, ?, ?, ?, ?)",
                (objectType, objectId, json.dumps(properties), now, now),
            )
        else:
            row = conn.execute(
                "SELECT properties FROM crm_objects WHERE object_type=? AND id=?", (objectType, objectId)
            ).fetchone()
            if row is None:
                raise ToolError(f"No {SINGULAR[objectType]} with ID {objectId}")
            merged = json.loads(row["properties"]) | properties
            conn.execute(
                "UPDATE crm_objects SET properties=?, updated_at=? WHERE object_type=? AND id=?",
                (json.dumps(merged), now, objectType, objectId),
            )
        row = conn.execute("SELECT * FROM crm_objects WHERE object_type=? AND id=?", (objectType, objectId)).fetchone()
        return _record(row, list(json.loads(row["properties"])))
