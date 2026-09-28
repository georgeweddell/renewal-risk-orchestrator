"""The seeded SQLite CRM used in mock mode, answering in HubSpot's response shapes."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from mcp.server.mcpserver.exceptions import ToolError

from mcp_servers import mockdata
from mcp_servers.crm.backends import DEFAULT_PROPERTIES, SINGULAR

# Fields `query` matches against, like HubSpot's default searchable properties.
QUERY_FIELDS: dict[str, list[str]] = {
    "companies": ["name", "domain", "account_slug"],
    "deals": ["dealname"],
}


class MockCrmBackend:
    def search(self, object_type, filter_groups, query, properties, limit) -> dict:
        with mockdata.session() as conn:
            rows = conn.execute(
                "SELECT * FROM crm_objects WHERE object_type=? ORDER BY CAST(id AS INTEGER)", (object_type,)
            ).fetchall()
            results = []
            for row in rows:
                props = json.loads(row["properties"])
                if query and not any(query.lower() in (props.get(k) or "").lower() for k in QUERY_FIELDS[object_type]):
                    continue
                if filter_groups and not any(
                    all(_matches(f, _filter_value(conn, object_type, row["id"], props, f["propertyName"])) for f in group["filters"])
                    for group in filter_groups
                ):
                    continue
                results.append(_record(row, properties))
        return {"total": len(results), "results": results[:limit]}

    def get(self, object_type, ids, properties, associations) -> dict:
        with mockdata.session() as conn:
            results, missing = [], []
            for object_id in ids:
                row = conn.execute(
                    "SELECT * FROM crm_objects WHERE object_type=? AND id=?", (object_type, object_id)
                ).fetchone()
                if row is None:
                    missing.append(object_id)
                    continue
                record = _record(row, properties)
                if associations:
                    record["associations"] = {
                        other: _associated_ids(conn, object_type, object_id, other) for other in associations
                    }
                results.append(record)
        response: dict = {"results": results}
        if missing:
            response["errors"] = [{"message": f"No {SINGULAR[object_type]} with ID {i}"} for i in missing]
        return response

    def owners(self, query) -> dict:
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

    def manage(self, object_type, properties, object_id) -> dict:
        now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with mockdata.session() as conn:
            if object_id is None:
                next_id = conn.execute(
                    "SELECT COALESCE(MAX(CAST(id AS INTEGER)), 0) + 1 FROM crm_objects WHERE object_type=?", (object_type,)
                ).fetchone()[0]
                object_id = str(next_id)
                conn.execute(
                    "INSERT INTO crm_objects VALUES (?, ?, ?, ?, ?)",
                    (object_type, object_id, json.dumps(properties), now, now),
                )
            else:
                row = conn.execute(
                    "SELECT properties FROM crm_objects WHERE object_type=? AND id=?", (object_type, object_id)
                ).fetchone()
                if row is None:
                    raise ToolError(f"No {SINGULAR[object_type]} with ID {object_id}")
                merged = json.loads(row["properties"]) | properties
                conn.execute(
                    "UPDATE crm_objects SET properties=?, updated_at=? WHERE object_type=? AND id=?",
                    (json.dumps(merged), now, object_type, object_id),
                )
            row = conn.execute(
                "SELECT * FROM crm_objects WHERE object_type=? AND id=?", (object_type, object_id)
            ).fetchone()
            return _record(row, list(json.loads(row["properties"])))


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


def _matches(f: dict, value: str | None) -> bool:
    op = f["operator"]
    if op == "HAS_PROPERTY":
        return value not in (None, "")
    if op == "NOT_HAS_PROPERTY":
        return value in (None, "")
    if value is None:
        return op in ("NEQ", "NOT_IN", "NOT_CONTAINS_TOKEN")
    if op == "IN":
        return any(_compare(value, v) == 0 for v in f.get("values") or [])
    if op == "NOT_IN":
        return all(_compare(value, v) != 0 for v in f.get("values") or [])
    if op in ("CONTAINS_TOKEN", "NOT_CONTAINS_TOKEN"):
        found = (f.get("value") or "").strip("*").lower() in value.lower()
        return found if op == "CONTAINS_TOKEN" else not found
    if f.get("value") is None:
        raise ToolError(f"Operator {op} on '{f['propertyName']}' needs a value.")
    cmp = _compare(value, f["value"])
    if op == "BETWEEN":
        if f.get("highValue") is None:
            raise ToolError("BETWEEN needs both value and highValue.")
        return cmp >= 0 and _compare(value, f["highValue"]) <= 0
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
