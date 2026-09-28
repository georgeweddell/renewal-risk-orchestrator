"""Where CRM data comes from. The server's tools stay the same whichever backend is used."""

from __future__ import annotations

import os
from typing import Protocol

# The properties returned when a request doesn't name any (HubSpot has its own
# defaults; both backends use this list so results look the same).
DEFAULT_PROPERTIES: dict[str, list[str]] = {
    "companies": ["name", "domain", "industry", "account_slug", "arr", "account_owner_name", "hubspot_owner_id"],
    "deals": ["dealname", "amount", "closedate", "dealstage", "pipeline", "dealtype", "hubspot_owner_id"],
}
SINGULAR = {"companies": "company", "deals": "deal"}


class CrmBackend(Protocol):
    def search(
        self, object_type: str, filter_groups: list[dict] | None, query: str | None, properties: list[str] | None, limit: int
    ) -> dict: ...

    def get(self, object_type: str, ids: list[str], properties: list[str] | None, associations: list[str] | None) -> dict: ...

    def owners(self, query: str | None) -> dict: ...

    def manage(self, object_type: str, properties: dict[str, str], object_id: str | None) -> dict: ...


def load_backend() -> CrmBackend:
    name = os.environ.get("CRM_BACKEND", "mock")
    if name == "mock":
        from mcp_servers.crm.mock_backend import MockCrmBackend

        return MockCrmBackend()
    if name == "hubspot":
        from mcp_servers.crm.hubspot_backend import HubSpotCrmBackend

        return HubSpotCrmBackend.from_env()
    raise RuntimeError(f"Unknown CRM_BACKEND={name!r}; expected 'mock' or 'hubspot'.")
