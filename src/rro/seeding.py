"""Turn seed/accounts.yaml into the mock systems of record.

Output is deterministic for a given seed file and date: the random noise is
seeded per account, so the same account always tells the same story.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from mcp_servers import mockdata
from mcp_servers.memory.store import Decision, MemoryStore

USAGE_WEEKS = 12
RECENT_WEEKS = 4
# Spread a change across the last 4 weeks so it reads as a trend, not a cliff.
# The weights average to 1, so the 4-week average moves by exactly change_pct.
RECENT_RAMP = (0.6, 0.9, 1.1, 1.4)
NOISE = 0.015
TICKETS_REPO_URL = "https://github.com/example-org/support-tickets"


class Owner(BaseModel):
    id: str
    first_name: str
    last_name: str
    email: str


class TicketSeed(BaseModel):
    priority: Literal["P1", "P2", "P3"]
    title: str
    age_days: int
    state: Literal["open", "closed"] = "open"


class UsageSeed(BaseModel):
    baseline_wau: int
    change_pct: float


class AccountSeed(BaseModel):
    slug: str
    name: str
    domain: str
    industry: str
    employees: int
    arr: int
    seats: int
    owner: str
    renewal_in_days: int
    deal_stage: str
    usage: UsageSeed
    tickets: list[TicketSeed] = []
    expected_band: Literal["healthy", "at-risk", "critical"]


class SeedData(BaseModel):
    owners: list[Owner]
    accounts: list[AccountSeed]


class DecisionSeed(BaseModel):
    account_slug: str
    account_name: str
    days_ago: int
    action_type: Literal["crm_risk_update", "pricing_exception"]
    risk_band: Literal["healthy", "at-risk", "critical"]
    risk_score: int
    drivers: list[Literal["usage_drop", "open_p1", "open_p2", "renewal_soon"]]
    proposal: str
    status: Literal["approved", "rejected"]
    approver: str
    note: str | None = None
    outcome: Literal["renewed", "churned", "downgraded", "pending"] | None = None
    outcome_note: str | None = None


class MemorySeed(BaseModel):
    decisions: list[DecisionSeed]


def seed_memory(seed_path: Path, db_path: Path, today: date | None = None) -> int:
    """Rebuild the decision memory from seed/memory.yaml. Returns the number of decisions."""
    today = today or datetime.now(UTC).date()
    seed = MemorySeed.model_validate(yaml.safe_load(seed_path.read_text(encoding="utf-8")))
    store = MemoryStore.create(db_path)
    for d in sorted(seed.decisions, key=lambda d: -d.days_ago):
        fields = d.model_dump(exclude={"days_ago"})
        store.record(Decision(**fields, decided_on=(today - timedelta(days=d.days_ago)).isoformat(), source="seed"))
    return len(seed.decisions)


def load_seed(path: Path) -> SeedData:
    return SeedData.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _hubspot_datetime(d: date) -> str:
    return datetime.combine(d, time(), tzinfo=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def seed_mock_systems(seed: SeedData, db_path: Path, today: date | None = None) -> dict[str, int]:
    """Rebuild the mock database from the seed. Returns row counts per system."""
    today = today or datetime.now(UTC).date()
    now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    conn = mockdata.create(db_path)
    ticket_number = 100
    deal_id = 5000
    counts = {"companies": 0, "deals": 0, "tickets": 0, "usage_weeks": 0}

    with conn:
        for owner in seed.owners:
            conn.execute(
                "INSERT INTO crm_owners VALUES (?, ?, ?, ?)", (owner.id, owner.email, owner.first_name, owner.last_name)
            )

        for index, acct in enumerate(seed.accounts, start=1):
            rng = random.Random(acct.slug)
            renewal = today + timedelta(days=acct.renewal_in_days)

            # --- CRM: company, the original deal, and the open renewal deal ---
            company_id = str(1000 + index)
            company = {
                "name": acct.name, "domain": acct.domain, "industry": acct.industry,
                "numberofemployees": str(acct.employees), "lifecyclestage": "customer",
                "account_slug": acct.slug, "arr": str(acct.arr), "licensed_seats": str(acct.seats),
                "hubspot_owner_id": acct.owner,
            }  # fmt: skip
            _insert_object(conn, "companies", company_id, company, now)
            counts["companies"] += 1

            deals = [
                {
                    "dealname": f"{acct.name} - New business", "amount": str(acct.arr),
                    "closedate": _hubspot_datetime(renewal - timedelta(days=365)), "dealstage": "closedwon",
                    "pipeline": "default", "dealtype": "newbusiness", "hubspot_owner_id": acct.owner,
                },
                {
                    "dealname": f"{acct.name} - Renewal {renewal.year}", "amount": str(acct.arr),
                    "closedate": _hubspot_datetime(renewal), "dealstage": acct.deal_stage,
                    "pipeline": "default", "dealtype": "existingbusiness", "hubspot_owner_id": acct.owner,
                },
            ]  # fmt: skip
            for deal in deals:
                deal_id += 1
                _insert_object(conn, "deals", str(deal_id), deal, now)
                _associate(conn, "companies", company_id, "deals", str(deal_id))
                counts["deals"] += 1

            # --- Tickets ---
            for t in acct.tickets:
                ticket_number += 1
                created = datetime.now(UTC) - timedelta(days=t.age_days, hours=rng.randint(1, 8))
                closed = created + timedelta(days=min(t.age_days, rng.randint(2, 6))) if t.state == "closed" else None
                labels = [t.priority, "bug" if t.priority != "P3" else "enhancement", f"account:{acct.slug}"]
                conn.execute(
                    "INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ticket_number, acct.slug, t.title, _ticket_body(acct, t), t.state, t.priority,
                        json.dumps(labels), created.isoformat(timespec="seconds"),
                        closed.isoformat(timespec="seconds") if closed else None,
                        f"{TICKETS_REPO_URL}/issues/{ticket_number}",
                    ),
                )  # fmt: skip
                counts["tickets"] += 1

            # --- Usage: 12 complete weeks, oldest first ---
            last_monday = today - timedelta(days=today.weekday() + 7)
            change = acct.usage.change_pct / 100
            for week in range(USAGE_WEEKS):
                week_start = last_monday - timedelta(weeks=USAGE_WEEKS - 1 - week)
                recent_index = week - (USAGE_WEEKS - RECENT_WEEKS)
                factor = 1 + change * RECENT_RAMP[recent_index] if recent_index >= 0 else 1.0
                active = round(acct.usage.baseline_wau * factor * (1 + rng.uniform(-NOISE, NOISE)))
                events = round(active * rng.uniform(38, 46))
                conn.execute(
                    "INSERT INTO usage_weekly VALUES (?, ?, ?, ?)", (acct.slug, week_start.isoformat(), active, events)
                )
                counts["usage_weeks"] += 1

    conn.close()
    return counts


def _insert_object(conn, object_type: str, object_id: str, properties: dict, now: str) -> None:
    conn.execute(
        "INSERT INTO crm_objects VALUES (?, ?, ?, ?, ?)", (object_type, object_id, json.dumps(properties), now, now)
    )


def _associate(conn, from_type: str, from_id: str, to_type: str, to_id: str) -> None:
    conn.executemany(
        "INSERT INTO crm_associations VALUES (?, ?, ?, ?)",
        [(from_type, from_id, to_type, to_id), (to_type, to_id, from_type, from_id)],
    )


def _ticket_body(acct: AccountSeed, t: TicketSeed) -> str:
    impact = {
        "P1": "Business-critical: blocking a core workflow for multiple users. Customer has escalated.",
        "P2": "Major: degraded experience with a workaround available.",
        "P3": "Minor: cosmetic issue or feature request.",
    }[t.priority]
    return f"Reported by {acct.name} ({acct.domain}).\n\n{impact}\n\nSummary: {t.title}."
