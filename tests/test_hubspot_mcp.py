"""HubSpot's own MCP server: the CRM dialect, argument stripping and configuration (no network)."""

import json
from types import SimpleNamespace

import pytest

from conftest import make_gateway
from rro.crm_access import CrmAccess
from rro.governance.gateway import RemoteServer, _without, resolve_launch


class FakeGateway:
    """Records CrmAccess calls and answers with canned HubSpot MCP responses."""

    def __init__(self, crm_backend, responses):
        self.settings = SimpleNamespace(backend_for=lambda system: crm_backend)
        self.responses = responses
        self.calls = []

    async def call(self, name, args, *, run_id, actor):
        self.calls.append((name, args, actor))
        return SimpleNamespace(is_error=False, text=json.dumps(self.responses[name]))


async def test_hubspot_dialect_finds_deals_by_association_and_builds_a_confirmed_write():
    gateway = FakeGateway("hubspot_mcp", {
        "crm__search_crm_objects": {"results": [{"id": 523364369606, "properties": {"dealname": "Renewal", "dealstage": "contractsent"}}]},
        "crm__get_crm_objects": {"objects": [{"id": 523364369606, "properties": {"dealname": "Renewal"}}], "notFound": []},
    })  # fmt: skip
    crm = CrmAccess(gateway)

    (deal,) = await crm.deals_for_company("449313760490", ["dealname", "dealstage"])
    assert deal.id == "523364369606"  # HubSpot's integer IDs become strings, as in our own server
    name, args, actor = gateway.calls[0]
    assert actor == "system"
    assert args["filterGroups"] == [{"associatedWith": [{"objectType": "companies", "operator": "EQUAL", "objectIdValues": [449313760490]}]}]

    assert (await crm.deal("523364369606", ["dealname"])).properties == {"dealname": "Renewal"}
    assert await crm.deal("not-a-number", ["dealname"]) is None

    system, tool, write = crm.update_call("deals", "523364369606", {"renewal_risk_level": "critical"})
    assert (system, tool) == ("crm", "manage_crm_objects")
    assert write == {
        "updateRequest": {"objects": [{"objectType": "deals", "objectId": 523364369606, "properties": {"renewal_risk_level": "critical"}}]},
        "confirmationStatus": "CONFIRMED",
    }


async def test_our_dialect_is_unchanged():
    gateway = FakeGateway("live", {"crm__search_crm_objects": {"results": []}})
    crm = CrmAccess(gateway)
    await crm.deals_for_company("1007", ["dealname"])
    assert gateway.calls[0][1]["filterGroups"] == [{"filters": [{"propertyName": "associations.company", "operator": "EQ", "value": "1007"}]}]
    assert crm.update_call("deals", "5014", {"x": "y"})[2] == {"objectType": "deals", "objectId": "5014", "properties": {"x": "y"}}


async def test_token_store_remembers_when_a_token_expires(tmp_path):
    import os
    import time

    from mcp.shared.auth import OAuthToken

    from rro.hubspot_mcp import FileTokenStorage

    path = tmp_path / "token.json"
    store = FileTokenStorage(path, "client", "secret")
    assert store.expires_at is None and await store.get_tokens() is None

    await store.set_tokens(OAuthToken(access_token="a", expires_in=1800, refresh_token="r"))
    assert (await store.get_tokens()).refresh_token == "r"
    assert time.time() + 1600 < store.expires_at < time.time() + 1800  # issued now, with a safety margin

    # The first format stored a bare token: its age comes from the file, so an old one reads as expired.
    path.write_text(OAuthToken(access_token="a", expires_in=1800, refresh_token="r").model_dump_json())
    two_hours_ago = time.time() - 7200
    os.utime(path, (two_hours_ago, two_hours_ago))
    assert store.expires_at < time.time()


def test_hubspot_mcp_is_a_remote_server_with_oauth(settings):
    config = settings.model_copy(update={"crm_backend": "hubspot_mcp"})
    assert resolve_launch(config)["crm"] == RemoteServer(url="https://mcp.hubspot.com", auth="hubspot_oauth")


def test_stripped_arguments_are_removed_from_the_schema():
    schema = {"type": "object", "properties": {"query": {"type": "string"}, "chatInsights": {"type": "object"}}, "required": ["query", "chatInsights"]}
    assert _without(schema, {"chatInsights"}) == {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}
    assert _without(schema, set()) is schema


async def test_stripped_arguments_are_never_forwarded(settings, store):
    async with make_gateway(settings, store) as gateway:
        gateway._stripped["crm"] = {"chatInsights"}
        outcome = await gateway.call(
            "crm__search_owners", {"query": "Priya", "chatInsights": {"userIntent": "prep a renewal"}}, run_id="r-strip"
        )
    assert not outcome.is_error
    (row,) = store.audit_for_run("r-strip")
    assert json.loads(row["args_json"]) == {"query": "Priya"}
    assert "removed chatInsights" in row["reason"]
