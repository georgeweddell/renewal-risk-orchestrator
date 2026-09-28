"""Push the demo story into the live systems: HubSpot, GitHub Issues and PostHog.

The numbers come from the same generators as mock mode (rro.seeding), so
live mode tells the same story. Each step is safe to re-run:
  - HubSpot: records are matched on account_slug / deal name and updated.
    Only a developer test account or sandbox is accepted.
  - GitHub: issues are matched on title + account label and skipped if present.
  - PostHog: events can't be deleted, so usage is only sent to a project with
    no demo events yet (override with force=True).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import httpx

from mcp_servers.tickets.backends import normalise_repo
from mcp_servers.usage.backends import ACTIVE_EVENT
from rro.seeding import SeedData, company_properties, deals_for, tickets_for, usage_series
from rro.settings import Settings

Log = Callable[[str], None]
GROUP = "renewal_risk_orchestrator"
SAFE_HUBSPOT_ACCOUNTS = {"DEVELOPER_TEST", "SANDBOX"}
EVENT_NAMESPACE = uuid.UUID("5d1f0e6a-3c1b-4c55-9d5e-2f0b8e7f6a01")

CUSTOM_PROPERTIES = {
    "companies": [
        {"name": "account_slug", "label": "Account slug", "type": "string", "fieldType": "text", "hasUniqueValue": True,
         "description": "The account's ID in the ticketing and analytics systems."},
        {"name": "arr", "label": "ARR", "type": "number", "fieldType": "number"},
        {"name": "licensed_seats", "label": "Licensed seats", "type": "number", "fieldType": "number"},
        {"name": "account_owner_name", "label": "Account owner", "type": "string", "fieldType": "text"},
    ],
    "deals": [
        {"name": "renewal_risk_level", "label": "Renewal risk level", "type": "enumeration", "fieldType": "select",
         "options": [{"label": "Healthy", "value": "healthy"}, {"label": "At risk", "value": "at-risk"},
                     {"label": "Critical", "value": "critical"}]},
        {"name": "renewal_risk_score", "label": "Renewal risk score", "type": "number", "fieldType": "number"},
        {"name": "renewal_risk_reason", "label": "Renewal risk reason", "type": "string", "fieldType": "textarea"},
        {"name": "pricing_exception_pct", "label": "Pricing exception (%)", "type": "number", "fieldType": "number"},
        {"name": "pricing_exception_status", "label": "Pricing exception status", "type": "string", "fieldType": "text"},
        {"name": "pricing_exception_rationale", "label": "Pricing exception rationale", "type": "string", "fieldType": "textarea"},
        {"name": "pricing_exception_conditions", "label": "Pricing exception conditions", "type": "string", "fieldType": "textarea"},
    ],
}  # fmt: skip
# Fields the approval executor writes; cleared on re-seed so each demo starts clean.
DEMO_DEAL_FIELDS = [p["name"] for p in CUSTOM_PROPERTIES["deals"]]

INDUSTRY_CODES = {
    "Healthcare": "HOSPITAL_HEALTH_CARE", "Logistics": "LOGISTICS_AND_SUPPLY_CHAIN",
    "Financial Services": "FINANCIAL_SERVICES", "Retail": "RETAIL", "Media": "MEDIA_PRODUCTION",
    "Insurance": "INSURANCE", "Manufacturing": "INDUSTRIAL_AUTOMATION", "Software": "COMPUTER_SOFTWARE",
}  # fmt: skip


class LiveSeedError(RuntimeError):
    pass


def _check(response: httpx.Response, what: str, ok: tuple[int, ...] = ()) -> httpx.Response:
    if response.status_code >= 400 and response.status_code not in ok:
        try:
            detail = response.json()
            detail = detail.get("message") or detail.get("detail") or detail
        except ValueError:
            detail = response.text
        raise LiveSeedError(f"{what} failed: HTTP {response.status_code}: {str(detail)[:300]}")
    return response


# --- HubSpot ------------------------------------------------------------------------
def seed_hubspot(settings: Settings, seed: SeedData, log: Log, today: date | None = None) -> None:
    token = settings.value("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise LiveSeedError("HUBSPOT_ACCESS_TOKEN is not set in .env")
    today = today or datetime.now(UTC).date()
    hs = httpx.Client(base_url="https://api.hubapi.com", headers={"Authorization": f"Bearer {token}"}, timeout=30)

    account = _check(hs.get("/account-info/v3/details"), "Reading HubSpot account details").json()
    if account.get("accountType") not in SAFE_HUBSPOT_ACCOUNTS:
        raise LiveSeedError(
            f"HubSpot account {account.get('portalId')} is a {account.get('accountType')} account. "
            "Demo data only goes into a developer test account or sandbox."
        )
    log(f"HubSpot: test account {account.get('portalId')}")

    for object_type, props in CUSTOM_PROPERTIES.items():
        _check(hs.post(f"/crm/v3/properties/{object_type}/groups", json={"name": GROUP, "label": "Renewal Risk Orchestrator"}),
               f"Creating the {object_type} property group", ok=(409,))  # fmt: skip
        for prop in props:
            _check(hs.post(f"/crm/v3/properties/{object_type}", json=prop | {"groupName": GROUP}),
                   f"Creating property {object_type}.{prop['name']}", ok=(409,))  # fmt: skip
    log("  custom properties ready")

    owners = _check(hs.get("/crm/v3/owners", params={"limit": 1}), "Reading owners").json().get("results", [])
    owner_id = owners[0]["id"] if owners else None

    for acct in seed.accounts:
        props = company_properties(seed, acct)
        props["industry"] = INDUSTRY_CODES.get(acct.industry, "")
        if owner_id:
            props["hubspot_owner_id"] = owner_id
        company_id = _upsert(hs, "companies", "account_slug", acct.slug, props)

        existing = _search(hs, "deals", "associations.company", company_id, ["dealname"])
        by_name = {d["properties"]["dealname"]: d["id"] for d in existing}
        for deal in deals_for(acct, today):
            deal = deal | ({"hubspot_owner_id": owner_id} if owner_id else {})
            if deal["dealname"] in by_name:
                cleared = {name: "" for name in DEMO_DEAL_FIELDS} if deal["dealstage"] != "closedwon" else {}
                _check(hs.patch(f"/crm/v3/objects/deals/{by_name[deal['dealname']]}", json={"properties": deal | cleared}),
                       f"Updating deal {deal['dealname']}")  # fmt: skip
            else:
                deal_id = _check(hs.post("/crm/v3/objects/deals", json={"properties": deal}), f"Creating deal {deal['dealname']}").json()["id"]
                _check(hs.put(f"/crm/v4/objects/deals/{deal_id}/associations/default/companies/{company_id}"),
                       f"Associating {deal['dealname']} with {acct.name}")  # fmt: skip
        log(f"  {acct.name}: company {company_id} + 2 deals")


def _search(hs: httpx.Client, object_type: str, prop: str, value: str, properties: list[str]) -> list[dict]:
    body = {"filterGroups": [{"filters": [{"propertyName": prop, "operator": "EQ", "value": value}]}], "properties": properties, "limit": 100}
    return _check(hs.post(f"/crm/v3/objects/{object_type}/search", json=body), f"Searching {object_type}").json().get("results", [])


def _upsert(hs: httpx.Client, object_type: str, key: str, value: str, props: dict) -> str:
    found = _search(hs, object_type, key, value, [key])
    if found:
        object_id = found[0]["id"]
        _check(hs.patch(f"/crm/v3/objects/{object_type}/{object_id}", json={"properties": props}), f"Updating {value}")
        return object_id
    return _check(hs.post(f"/crm/v3/objects/{object_type}", json={"properties": props}), f"Creating {value}").json()["id"]


# --- GitHub Issues --------------------------------------------------------------------
PRIORITY_COLOURS = {"P1": "b60205", "P2": "d93f0b", "P3": "fbca04"}


def seed_github(settings: Settings, seed: SeedData, log: Log, now: datetime | None = None) -> None:
    repo, token = settings.value("GITHUB_TICKETS_REPO"), settings.value("GITHUB_TOKEN")
    if not (repo and token):
        raise LiveSeedError("GITHUB_TICKETS_REPO and GITHUB_TOKEN must be set in .env")
    repo = normalise_repo(repo)
    now = now or datetime.now(UTC)
    gh = httpx.Client(
        base_url=f"https://api.github.com/repos/{repo}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        timeout=30,
    )
    log(f"GitHub: {repo}")

    labels = {name: colour for name, colour in PRIORITY_COLOURS.items()} | {f"account:{a.slug}": "c5def5" for a in seed.accounts}
    for name, colour in labels.items():
        _check(gh.post("/labels", json={"name": name, "color": colour}), f"Creating label {name}", ok=(422,))  # 422 = exists

    existing, page = set(), 1
    while True:
        batch = _check(gh.get("/issues", params={"state": "all", "per_page": 100, "page": page}), "Listing issues").json()
        existing |= {(i["title"], l["name"]) for i in batch for l in i["labels"] if l["name"].startswith("account:")}
        if len(batch) < 100:
            break
        page += 1

    created = 0
    for acct in seed.accounts:
        for t in tickets_for(acct, now):
            if (t.title, f"account:{acct.slug}") in existing:
                continue
            markers = f"\n\n<!-- rro:reported_at={t.created_at.isoformat()} -->"
            if t.closed_at:
                markers += f"\n<!-- rro:closed_at={t.closed_at.isoformat()} -->"
            issue = _check(gh.post("/issues", json={"title": t.title, "body": t.body + markers, "labels": t.labels}),
                           f"Creating issue {t.title!r}").json()  # fmt: skip
            if t.state == "closed":
                _check(gh.patch(f"/issues/{issue['number']}", json={"state": "closed", "state_reason": "completed"}),
                       f"Closing issue #{issue['number']}")  # fmt: skip
            created += 1
            time.sleep(0.5)  # stay well clear of GitHub's content-creation rate limit
    log(f"  {created} issues created, {sum(len(a.tickets) for a in seed.accounts) - created} already there")


# --- PostHog ------------------------------------------------------------------------------
def seed_posthog(settings: Settings, seed: SeedData, log: Log, *, force: bool = False, today: date | None = None) -> int:
    project, capture_key, read_key = (
        settings.value("POSTHOG_PROJECT_ID"), settings.value("POSTHOG_PROJECT_API_KEY"), settings.value("POSTHOG_PERSONAL_API_KEY")
    )
    if not (project and capture_key and read_key):
        raise LiveSeedError("POSTHOG_PROJECT_ID, POSTHOG_PROJECT_API_KEY and POSTHOG_PERSONAL_API_KEY must be set in .env")
    today = today or datetime.now(UTC).date()
    log(f"PostHog: project {project} ({settings.posthog_host})")

    existing = count_demo_events(settings)
    if existing and not force:
        log(f"  {existing:,} demo events already in this project; not sending more (events can't be deleted).")
        return 0

    events = list(_usage_events(seed, today))
    ingest = httpx.Client(base_url=settings.posthog_ingest_host, timeout=60)
    for i in range(0, len(events), 500):
        body = {"api_key": capture_key, "historical_migration": True, "batch": events[i : i + 500]}
        _check(ingest.post("/batch/", json=body), "Sending events to PostHog")
    log(f"  {len(events):,} events sent for {len(seed.accounts)} accounts, 12 weeks each")
    return len(events)


def count_demo_events(settings: Settings) -> int:
    read = httpx.Client(
        base_url=settings.posthog_host.rstrip("/"),
        headers={"Authorization": f"Bearer {settings.value('POSTHOG_PERSONAL_API_KEY')}"},
        timeout=60,
    )
    query = {"kind": "HogQLQuery", "query": "SELECT count() FROM events WHERE event = {event}", "values": {"event": ACTIVE_EVENT}}
    body = {"query": query, "refresh": "force_blocking"}  # bypass PostHog's query cache
    response = _check(read.post(f"/api/projects/{settings.value('POSTHOG_PROJECT_ID')}/query/", json=body), "Querying PostHog")
    return int(response.json()["results"][0][0])


def _usage_events(seed: SeedData, today: date):
    """One `app_session` event per active user per session, reproducing each account's weekly active users."""
    import random

    for acct in seed.accounts:
        weeks = usage_series(acct, today)
        pool = [f"{acct.slug}-user-{n:03d}" for n in range(max(w.active_users for w in weeks) + 10)]
        for week in weeks:
            rng = random.Random(f"{acct.slug}:{week.start}")
            for user in rng.sample(pool, week.active_users):
                for session in range(rng.randint(1, 3)):
                    at = datetime.combine(week.start, datetime.min.time(), tzinfo=UTC) + timedelta(
                        days=rng.randint(0, 4), hours=rng.randint(9, 17), minutes=rng.randint(0, 59)
                    )
                    yield {
                        "event": ACTIVE_EVENT,
                        "distinct_id": user,
                        "timestamp": at.isoformat(),
                        # Deterministic, so a re-send of the same event is deduplicated.
                        "uuid": str(uuid.uuid5(EVENT_NAMESPACE, f"{user}|{week.start}|{session}")),
                        "properties": {"account_id": acct.slug, "$process_person_profile": False, "$lib": "rro-seed"},
                    }
