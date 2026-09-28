"""The live backends against canned HTTP responses (no network), plus live-mode configuration."""

import json
from datetime import date

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_servers.crm.hubspot_backend import HubSpotCrmBackend
from mcp_servers.tickets.backends import GitHubTicketsBackend, normalise_repo
from mcp_servers.usage.backends import PostHogUsageBackend
from rro.governance.gateway import GatewayConfigError, resolve_launch
from rro.settings import Settings


def client(handler, base_url="https://example.test"):
    return httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler))


# --- GitHub -------------------------------------------------------------------------
def github_issue(number, title, labels, state="open", body="Details.", created="2026-09-28T10:00:00Z"):
    return {
        "number": number, "title": title, "state": state, "body": body, "created_at": created,
        "closed_at": "2026-09-28T11:00:00Z" if state == "closed" else None,
        "html_url": f"https://github.com/o/r/issues/{number}", "labels": [{"name": l} for l in labels],
    }  # fmt: skip


def test_github_backend_maps_labels_and_backdated_markers():
    body = "Reported by Halcyon.\n\n<!-- rro:reported_at=2026-09-09T10:00:00+00:00 -->"
    issues = [
        github_issue(1, "SSO login loop", ["P1", "bug", "account:halcyon-robotics"], body=body),
        github_issue(2, "Old request", ["enhancement", "account:halcyon-robotics"], state="closed"),
        {**github_issue(3, "A pull request", ["account:halcyon-robotics"]), "pull_request": {}},
    ]
    seen = {}

    def handler(request):
        seen.update(request.url.params)
        return httpx.Response(200, json=issues)

    backend = GitHubTicketsBackend("o/r", "t", client=client(handler))
    result = backend.list_issues("halcyon-robotics")
    assert seen["labels"] == "account:halcyon-robotics" and seen["state"] == "all"
    assert [i["number"] for i in result] == [2, 1]  # newest first; the pull request is dropped
    sso = next(i for i in result if i["number"] == 1)
    assert sso["priority"] == "P1" and sso["account_id"] == "halcyon-robotics"
    assert sso["created_at"] == "2026-09-09T10:00:00+00:00"  # the marker wins over GitHub's own timestamp
    assert "rro:" not in sso["body"]
    closed = next(i for i in result if i["number"] == 2)
    assert closed["priority"] == "P3" and closed["closed_at"] == "2026-09-28T11:00:00Z"


def test_github_backend_reports_a_missing_repo():
    backend = GitHubTicketsBackend("o/r", "t", client=client(lambda r: httpx.Response(404, json={"message": "Not Found"})))
    with pytest.raises(ToolError, match="not found"):
        backend.list_issues("x")


@pytest.mark.parametrize("value", ["owner/repo", "https://github.com/owner/repo", "https://github.com/owner/repo.git", " owner/repo/ "])
def test_repo_accepts_a_url_or_owner_repo(value):
    assert normalise_repo(value) == "owner/repo"


# --- PostHog --------------------------------------------------------------------------------
def test_posthog_backend_fills_missing_weeks_with_zero():
    sent = {}

    def handler(request):
        payload = json.loads(request.content)
        assert payload["refresh"] == "force_blocking"
        sent.update(payload["query"])
        return httpx.Response(200, json={"results": [["2026-09-07", 90, 200], ["2026-09-21", 67, 150]]})

    backend = PostHogUsageBackend("https://ph.test", "42", "k", client=client(handler))
    series = backend.weekly_active_users("halcyon-robotics", 3, today=date(2026, 9, 30))
    assert sent["values"]["account_id"] == "halcyon-robotics"  # a query parameter, never string-formatted
    assert sent["values"]["end"] == "2026-09-28 00:00:00"  # only complete weeks
    assert series == [
        {"week_start": "2026-09-07", "active_users": 90, "events": 200},
        {"week_start": "2026-09-14", "active_users": 0, "events": 0},
        {"week_start": "2026-09-21", "active_users": 67, "events": 150},
    ]


def test_posthog_backend_treats_no_data_as_unknown_account():
    backend = PostHogUsageBackend("https://ph.test", "42", "k", client=client(lambda r: httpx.Response(200, json={"results": []})))
    assert backend.weekly_active_users("nobody", 12) == []


# --- HubSpot ------------------------------------------------------------------------------
def test_hubspot_backend_trims_properties_and_flattens_associations():
    def handler(request):
        assert request.url.params["associations"] == "deals"
        return httpx.Response(
            200,
            json={
                "id": "449313760490",
                "properties": {"name": "Halcyon Robotics", "account_slug": "halcyon-robotics", "hs_object_id": "449313760490"},
                "associations": {"deals": {"results": [
                    {"id": "101", "type": "company_to_deal"}, {"id": "101", "type": "company_to_deal_unlabeled"}, {"id": "102", "type": "company_to_deal"},
                ]}},
            },
        )  # fmt: skip

    backend = HubSpotCrmBackend("t", client=client(handler))
    result = backend.get("companies", ["449313760490"], ["name", "account_slug"], ["deals"])
    (record,) = result["results"]
    assert record["properties"] == {"name": "Halcyon Robotics", "account_slug": "halcyon-robotics"}
    assert record["associations"] == {"deals": ["101", "102"]}


def test_hubspot_errors_become_tool_errors_without_the_token():
    backend = HubSpotCrmBackend("secret-token", client=client(lambda r: httpx.Response(401, json={"message": "Authentication credentials not found."})))
    with pytest.raises(ToolError) as exc:
        backend.search("companies", None, "x", None, 10)
    assert "401" in str(exc.value) and "secret-token" not in str(exc.value)


# --- live-mode configuration ----------------------------------------------------------------
def test_live_servers_get_only_their_own_credentials(settings):
    live = settings.model_copy(update={
        "rro_mode": "live", "hubspot_access_token": "hs-token", "github_tickets_repo": "o/r", "github_token": "gh-token",
        "posthog_project_id": "42", "posthog_personal_api_key": "phx-key", "posthog_project_api_key": "phc-key",
    })  # fmt: skip
    launches = resolve_launch(live)
    assert launches["crm"].env == {"CRM_BACKEND": "hubspot", "HUBSPOT_ACCESS_TOKEN": "hs-token"}
    assert launches["tickets"].env["GITHUB_TOKEN"] == "gh-token" and "HUBSPOT_ACCESS_TOKEN" not in launches["tickets"].env
    assert launches["usage"].env["POSTHOG_PERSONAL_API_KEY"] == "phx-key"
    assert "POSTHOG_PROJECT_API_KEY" not in launches["usage"].env  # the usage server can read, not send events


def test_a_missing_live_credential_is_a_clear_error(settings):
    live = settings.model_copy(update={"rro_mode": "live", "hubspot_access_token": None})
    with pytest.raises(GatewayConfigError, match="HUBSPOT_ACCESS_TOKEN is not set"):
        resolve_launch(live)


def test_posthog_ingest_host_is_derived():
    assert Settings(_env_file=None, posthog_host="https://eu.posthog.com").posthog_ingest_host == "https://eu.i.posthog.com"
