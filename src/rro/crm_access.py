"""CRM operations made by code, not by the agent: checking a proposal against
the CRM, building the approved write, and collecting signals for `rro score`.

The agent reads each server's own tool schemas, so it copes with either CRM
server. Deterministic code can't, so the two dialects live here:

  "rro"          our crm server (mock or HubSpot REST backend)
  "hubspot_mcp"  HubSpot's own MCP server at mcp.hubspot.com

All calls still go through the Tool Gateway, so they're policy-checked and
audited (as actor "system").
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from rro.governance.gateway import ToolGateway

CLOSED_STAGES = {"closedwon", "closedlost"}


class CrmError(RuntimeError):
    pass


@dataclass
class Record:
    id: str
    properties: dict[str, Any]


class CrmAccess:
    def __init__(self, gateway: ToolGateway, *, run_id: str | None = None):
        self.gateway = gateway
        self.run_id = run_id
        self.dialect = "hubspot_mcp" if gateway.settings.backend_for("crm") == "hubspot_mcp" else "rro"

    async def _call(self, tool: str, args: dict) -> dict:
        outcome = await self.gateway.call(f"crm__{tool}", args, run_id=self.run_id, actor="system")
        if outcome.is_error:
            raise CrmError(f"crm.{tool} failed: {outcome.text}")
        return json.loads(outcome.text)

    async def company_by_slug(self, slug: str, properties: list[str]) -> Record | None:
        found = await self._search(
            "companies", {"filters": [{"propertyName": "account_slug", "operator": "EQ", "value": slug}]}, properties
        )
        if len(found) > 1:
            raise CrmError(f"More than one company has account_slug '{slug}'")
        return found[0] if found else None

    async def deals_for_company(self, company_id: str, properties: list[str]) -> list[Record]:
        if self.dialect == "hubspot_mcp":
            group = {"associatedWith": [{"objectType": "companies", "operator": "EQUAL", "objectIdValues": [int(company_id)]}]}
        else:
            group = {"filters": [{"propertyName": "associations.company", "operator": "EQ", "value": company_id}]}
        return await self._search("deals", group, properties)

    async def deal(self, deal_id: str, properties: list[str]) -> Record | None:
        if self.dialect == "hubspot_mcp":
            if not deal_id.isdigit():
                return None
            data = await self._call("get_crm_objects", {"objectType": "deals", "objectIds": [int(deal_id)], "properties": properties})
            found = data.get("objects", [])
        else:
            data = await self._call("get_crm_objects", {"objectType": "deals", "objectIds": [deal_id], "properties": properties})
            found = data.get("results", [])
        return Record(str(found[0]["id"]), found[0]["properties"]) if found else None

    def update_call(self, object_type: str, object_id: str, properties: dict[str, str]) -> tuple[str, str, dict]:
        """The (system, tool, arguments) the approval executor will run for this update."""
        if self.dialect == "hubspot_mcp":
            args = {
                "updateRequest": {"objects": [{"objectType": object_type, "objectId": int(object_id), "properties": properties}]},
                # HubSpot asks the client to confirm writes. Here the confirmation is real: this
                # payload only runs after a human approves it, and the hash covers this field.
                "confirmationStatus": "CONFIRMED",
            }
        else:
            args = {"objectType": object_type, "objectId": object_id, "properties": properties}
        return "crm", "manage_crm_objects", args

    async def _search(self, object_type: str, group: dict, properties: list[str]) -> list[Record]:
        data = await self._call(
            "search_crm_objects", {"objectType": object_type, "filterGroups": [group], "properties": properties, "limit": 100}
        )
        return [Record(str(r["id"]), r["properties"]) for r in data.get("results", [])]


def is_open(deal: Record) -> bool:
    return deal.properties.get("dealstage") not in CLOSED_STAGES
