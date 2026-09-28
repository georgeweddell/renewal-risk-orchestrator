"""Where usage data comes from. The server's tools stay the same whichever backend is used."""

from __future__ import annotations

import os
from typing import Protocol

from mcp_servers import mockdata


class UsageBackend(Protocol):
    def weekly_active_users(self, account_id: str, weeks: int) -> list[dict]:
        """The most recent `weeks` complete weeks, oldest first:
        [{"week_start": "YYYY-MM-DD", "active_users": int, "events": int}, ...]"""


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


def load_backend() -> UsageBackend:
    name = os.environ.get("USAGE_BACKEND", "mock")
    if name == "mock":
        return MockUsageBackend()
    raise RuntimeError(f"USAGE_BACKEND={name!r} is not available yet; the PostHog backend arrives in Phase 3.")
