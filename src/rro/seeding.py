"""Turn seed/ into demo data: the mock systems of record and the decision memory.

The story each account tells (its usage series, its tickets and their dates)
is generated here once and shared with the live seeder, so mock and live
mode show the same numbers. Output is deterministic for a given seed file and
date: random noise is seeded per account.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
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
MOCK_TICKETS_REPO_URL = "https://github.com/example-org/support-tickets"


class Owner(BaseModel):
    id: str
    first_name: str
    last_name: str
    email: str

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}"


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

    def owner(self, owner_id: str) -> Owner:
        return next(o for o in self.owners if o.id == owner_id)


def load_seed(path: Path) -> SeedData:
    return SeedData.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


# --- the story, shared by mock and live seeding ------------------------------------
@dataclass(frozen=True)
class Week:
    start: date  # Monday
    active_users: int


@dataclass(frozen=True)
class Ticket:
    priority: str
    title: str
    body: str
    state: str
    created_at: datetime
    closed_at: datetime | None
    labels: list[str]


def usage_series(acct: AccountSeed, today: date) -> list[Week]:
    """12 complete weeks of weekly active users, oldest first, ending last week."""
    rng = random.Random(f"{acct.slug}:usage")
    last_monday = today - timedelta(days=today.weekday() + 7)
    change = acct.usage.change_pct / 100
    weeks = []
    for i in range(USAGE_WEEKS):
        recent_index = i - (USAGE_WEEKS - RECENT_WEEKS)
        factor = 1 + change * RECENT_RAMP[recent_index] if recent_index >= 0 else 1.0
        active = round(acct.usage.baseline_wau * factor * (1 + rng.uniform(-NOISE, NOISE)))
        weeks.append(Week(last_monday - timedelta(weeks=USAGE_WEEKS - 1 - i), active))
    return weeks


def tickets_for(acct: AccountSeed, now: datetime) -> list[Ticket]:
    tickets = []
    for i, t in enumerate(acct.tickets):
        rng = random.Random(f"{acct.slug}:ticket:{i}")
        created = (now - timedelta(days=t.age_days, hours=rng.randint(1, 8))).replace(microsecond=0)
        closed = created + timedelta(days=min(t.age_days, rng.randint(2, 6))) if t.state == "closed" else None
        tickets.append(
            Ticket(
                priority=t.priority,
                title=t.title,
                body=_ticket_body(acct, t),
                state=t.state,
                created_at=created,
                closed_at=closed,
                labels=[t.priority, "bug" if t.priority != "P3" else "enhancement", f"account:{acct.slug}"],
            )
        )
    return tickets


def hubspot_datetime(d: date) -> str:
    return datetime.combine(d, time(), tzinfo=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def renewal_date(acct: AccountSeed, today: date) -> date:
    return today + timedelta(days=acct.renewal_in_days)


def deals_for(acct: AccountSeed, today: date) -> list[dict[str, str]]:
    """The original closed-won deal and the open renewal deal, as HubSpot properties."""
    renewal = renewal_date(acct, today)
    return [
        {
            "dealname": f"{acct.name} - New business", "amount": str(acct.arr),
            "closedate": hubspot_datetime(renewal - timedelta(days=365)), "dealstage": "closedwon",
            "pipeline": "default", "dealtype": "newbusiness",
        },
        {
            "dealname": f"{acct.name} - Renewal {renewal.year}", "amount": str(acct.arr),
            "closedate": hubspot_datetime(renewal), "dealstage": acct.deal_stage,
            "pipeline": "default", "dealtype": "existingbusiness",
        },
    ]  # fmt: skip


def _ticket_body(acct: AccountSeed, t: TicketSeed) -> str:
    impact = {
        "P1": "Business-critical: blocking a core workflow for multiple users. Customer has escalated.",
        "P2": "Major: degraded experience with a workaround available.",
        "P3": "Minor: cosmetic issue or feature request.",
    }[t.priority]
    return f"Reported by {acct.name} ({acct.domain}).\n\n{impact}\n\nSummary: {t.title}."


# --- mock systems ---------------------------------------------------------------------
def seed_mock_systems(seed: SeedData, db_path: Path, today: date | None = None) -> dict[str, int]:
    """Rebuild the mock database from the seed. Returns row counts per system."""
    today = today or datetime.now(UTC).date()
    now = datetime.now(UTC)
    stamp = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    conn = mockdata.create(db_path)
    ticket_number, deal_id = 100, 5000
    counts = {"companies": 0, "deals": 0, "tickets": 0, "usage_weeks": 0}

    with conn:
        for owner in seed.owners:
            conn.execute(
                "INSERT INTO crm_owners VALUES (?, ?, ?, ?)", (owner.id, owner.email, owner.first_name, owner.last_name)
            )

        for index, acct in enumerate(seed.accounts, start=1):
            company_id = str(1000 + index)
            _insert_object(conn, "companies", company_id, company_properties(seed, acct) | {"hubspot_owner_id": acct.owner}, stamp)
            counts["companies"] += 1

            for deal in deals_for(acct, today):
                deal_id += 1
                _insert_object(conn, "deals", str(deal_id), deal | {"hubspot_owner_id": acct.owner}, stamp)
                _associate(conn, "companies", company_id, "deals", str(deal_id))
                counts["deals"] += 1

            for t in tickets_for(acct, now):
                ticket_number += 1
                conn.execute(
                    "INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ticket_number, acct.slug, t.title, t.body, t.state, t.priority, json.dumps(t.labels),
                        t.created_at.isoformat(), t.closed_at.isoformat() if t.closed_at else None,
                        f"{MOCK_TICKETS_REPO_URL}/issues/{ticket_number}",
                    ),
                )  # fmt: skip
                counts["tickets"] += 1

            rng = random.Random(f"{acct.slug}:events")
            for week in usage_series(acct, today):
                events = round(week.active_users * rng.uniform(1.8, 2.6))
                conn.execute(
                    "INSERT INTO usage_weekly VALUES (?, ?, ?, ?)", (acct.slug, week.start.isoformat(), week.active_users, events)
                )
                counts["usage_weeks"] += 1

    conn.close()
    return counts


def company_properties(seed: SeedData, acct: AccountSeed) -> dict[str, str]:
    return {
        "name": acct.name, "domain": acct.domain, "industry": acct.industry,
        "numberofemployees": str(acct.employees), "lifecyclestage": "customer",
        "account_slug": acct.slug, "arr": str(acct.arr), "licensed_seats": str(acct.seats),
        "account_owner_name": seed.owner(acct.owner).name,
    }  # fmt: skip


def _insert_object(conn, object_type: str, object_id: str, properties: dict, now: str) -> None:
    conn.execute(
        "INSERT INTO crm_objects VALUES (?, ?, ?, ?, ?)", (object_type, object_id, json.dumps(properties), now, now)
    )


def _associate(conn, from_type: str, from_id: str, to_type: str, to_id: str) -> None:
    conn.executemany(
        "INSERT INTO crm_associations VALUES (?, ?, ?, ?)",
        [(from_type, from_id, to_type, to_id), (to_type, to_id, from_type, from_id)],
    )


# --- decision memory --------------------------------------------------------------
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
