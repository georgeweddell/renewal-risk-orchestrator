"""Where usage data comes from. The server's tools stay the same whichever backend is used."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from mcp_servers import mockdata

# The product event that counts as "active". The seeder sends it; a real
# product would pick its own definition of an active user.
ACTIVE_EVENT = "app_session"


class UsageBackend(Protocol):
    def weekly_active_users(self, account_id: str, weeks: int) -> list[dict]:
        """The most recent `weeks` complete weeks, oldest first:
        [{"week_start": "YYYY-MM-DD", "active_users": int, "events": int}, ...].
        Empty if there's no data for the account at all."""


class MockUsageBackend:
    """Reads the weekly aggregates seeded by `rro seed`."""

    def weekly_active_users(self, account_id: str, weeks: int) -> list[dict]:
        with mockdata.session() as conn:
            rows = conn.execute(
                "SELECT week_start, active_users, events FROM usage_weekly "
                "WHERE account_id=? ORDER BY week_start DESC LIMIT ?",
                (account_id, weeks),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]


class PostHogUsageBackend:
    """Weekly active users from PostHog events, via a HogQL query.

    A user is active in a week if they sent at least one `app_session` event
    with `account_id` set to the account. Only complete weeks (Monday to
    Sunday, UTC) count, and weeks with no activity are reported as zero
    rather than skipped.
    """

    QUERY = """
        SELECT toStartOfWeek(timestamp, 1) AS week,
               count(DISTINCT distinct_id) AS active_users,
               count() AS events
        FROM events
        WHERE event = {event}
          AND properties.account_id = {account_id}
          AND timestamp >= toDateTime({start})
          AND timestamp < toDateTime({end})
        GROUP BY week
        ORDER BY week
    """

    def __init__(self, host: str, project_id: str, token: str, client: httpx.Client | None = None):
        self.project_id = project_id
        self.client = client or httpx.Client(
            base_url=host.rstrip("/"), headers={"Authorization": f"Bearer {token}"}, timeout=60
        )

    @classmethod
    def from_env(cls) -> PostHogUsageBackend:
        host, project, token = (os.environ.get(k) for k in ("POSTHOG_HOST", "POSTHOG_PROJECT_ID", "POSTHOG_PERSONAL_API_KEY"))
        if not (host and project and token):
            raise ToolError("POSTHOG_HOST, POSTHOG_PROJECT_ID and POSTHOG_PERSONAL_API_KEY must be set for the usage server.")
        return cls(host, project, token)

    def weekly_active_users(self, account_id: str, weeks: int, today: date | None = None) -> list[dict]:
        today = today or datetime.now(UTC).date()
        end = today - timedelta(days=today.weekday())  # Monday of the current (incomplete) week
        start = end - timedelta(weeks=weeks)
        values = {"event": ACTIVE_EVENT, "account_id": account_id, "start": f"{start} 00:00:00", "end": f"{end} 00:00:00"}
        try:
            response = self.client.post(
                f"/api/projects/{self.project_id}/query/",
                # PostHog caches query results; always recompute so the agent never reasons over stale usage.
                json={"query": {"kind": "HogQLQuery", "query": self.QUERY, "values": values}, "refresh": "force_blocking"},
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"Couldn't reach PostHog: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise ToolError(f"PostHog returned {response.status_code}: {str(response.json().get('detail', ''))[:200]}")

        found = {str(row[0])[:10]: (int(row[1]), int(row[2])) for row in response.json().get("results", [])}
        if not found:
            return []  # no data at all: most likely the wrong account_id, not an account with zero users
        series = []
        for i in range(weeks):
            week = (start + timedelta(weeks=i)).isoformat()
            active, events = found.get(week, (0, 0))
            series.append({"week_start": week, "active_users": active, "events": events})
        return series


def load_backend() -> UsageBackend:
    name = os.environ.get("USAGE_BACKEND", "mock")
    if name == "mock":
        return MockUsageBackend()
    if name == "posthog":
        return PostHogUsageBackend.from_env()
    raise RuntimeError(f"Unknown USAGE_BACKEND={name!r}; expected 'mock' or 'posthog'.")
