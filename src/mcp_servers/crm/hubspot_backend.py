"""A real HubSpot account via the CRM v3 REST API, authenticated with a private app token.

Responses are trimmed to the same shapes the mock backend returns (only the
requested properties; associations as lists of IDs), so the agent sees
identical data structures in mock and live mode.
"""

from __future__ import annotations

import os

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_servers.crm.backends import DEFAULT_PROPERTIES, SINGULAR

API = "https://api.hubapi.com"


class HubSpotCrmBackend:
    def __init__(self, token: str, client: httpx.Client | None = None):
        self.client = client or httpx.Client(
            base_url=API, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )

    @classmethod
    def from_env(cls) -> HubSpotCrmBackend:
        token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
        if not token:
            raise ToolError("HUBSPOT_ACCESS_TOKEN isn't set for the CRM server. Add it to .env.")
        return cls(token)

    def _request(self, method: str, path: str, **kwargs) -> dict | None:
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ToolError(f"Couldn't reach HubSpot: {type(exc).__name__}") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            try:
                message = response.json().get("message", "")
            except ValueError:
                message = response.text[:200]
            raise ToolError(f"HubSpot returned {response.status_code}: {message}")
        return response.json() if response.content else {}

    def search(self, object_type, filter_groups, query, properties, limit) -> dict:
        wanted = properties or DEFAULT_PROPERTIES[object_type]
        body: dict = {"properties": wanted, "limit": limit}
        if filter_groups:
            body["filterGroups"] = filter_groups
        if query:
            body["query"] = query
        data = self._request("POST", f"/crm/v3/objects/{object_type}/search", json=body) or {}
        return {"total": data.get("total", 0), "results": [_record(r, wanted) for r in data.get("results", [])]}

    def get(self, object_type, ids, properties, associations) -> dict:
        wanted = properties or DEFAULT_PROPERTIES[object_type]
        params = {"properties": ",".join(wanted)}
        if associations:
            params["associations"] = ",".join(associations)
        results, missing = [], []
        for object_id in ids:
            data = self._request("GET", f"/crm/v3/objects/{object_type}/{object_id}", params=params)
            if data is None:
                missing.append(object_id)
                continue
            record = _record(data, wanted)
            if associations:
                found = data.get("associations") or {}
                # HubSpot lists one entry per association type (e.g. primary and unlabelled); keep unique IDs.
                record["associations"] = {
                    other: sorted({a["id"] for a in (found.get(other) or {}).get("results", [])}) for other in associations
                }
            results.append(record)
        response: dict = {"results": results}
        if missing:
            response["errors"] = [{"message": f"No {SINGULAR[object_type]} with ID {i}"} for i in missing]
        return response

    def owners(self, query) -> dict:
        data = self._request("GET", "/crm/v3/owners", params={"limit": 100}) or {}
        owners = [
            {"id": o["id"], "email": o.get("email"), "firstName": o.get("firstName"), "lastName": o.get("lastName")}
            for o in data.get("results", [])
        ]
        if query:
            q = query.lower()
            owners = [o for o in owners if q in f"{o['firstName']} {o['lastName']} {o['email']}".lower()]
        return {"results": owners}

    def manage(self, object_type, properties, object_id) -> dict:
        if object_id is None:
            data = self._request("POST", f"/crm/v3/objects/{object_type}", json={"properties": properties})
        else:
            data = self._request("PATCH", f"/crm/v3/objects/{object_type}/{object_id}", json={"properties": properties})
            if data is None:
                raise ToolError(f"No {SINGULAR[object_type]} with ID {object_id}")
        return _record(data, list(properties))


def _record(data: dict, wanted: list[str]) -> dict:
    props = data.get("properties") or {}
    return {
        "id": data["id"],
        "properties": {name: props.get(name) for name in wanted},
        "createdAt": data.get("createdAt"),
        "updatedAt": data.get("updatedAt"),
    }
