"""Where tickets come from. The server's tools stay the same whichever backend is used."""

from __future__ import annotations

import json
import os
from typing import Protocol

from mcp_servers import mockdata


class TicketsBackend(Protocol):
    def list_issues(self, account_id: str) -> list[dict]:
        """All issues (open and closed) for an account, newest first."""

    def get_issue(self, number: int) -> dict | None: ...


class MockTicketsBackend:
    """Reads the GitHub Issues-shaped `tickets` table seeded by `rro seed`."""

    def list_issues(self, account_id: str) -> list[dict]:
        with mockdata.session() as conn:
            rows = conn.execute(
                "SELECT * FROM tickets WHERE account_id=? ORDER BY created_at DESC", (account_id,)
            ).fetchall()
        return [self._issue(r) for r in rows]

    def get_issue(self, number: int) -> dict | None:
        with mockdata.session() as conn:
            row = conn.execute("SELECT * FROM tickets WHERE number=?", (number,)).fetchone()
        return self._issue(row) if row else None

    @staticmethod
    def _issue(row) -> dict:
        return {
            "number": row["number"],
            "account_id": row["account_id"],
            "title": row["title"],
            "body": row["body"],
            "state": row["state"],
            "priority": row["priority"],
            "labels": json.loads(row["labels"]),
            "created_at": row["created_at"],
            "closed_at": row["closed_at"],
            "url": row["url"],
        }


def load_backend() -> TicketsBackend:
    name = os.environ.get("TICKETS_BACKEND", "mock")
    if name == "mock":
        return MockTicketsBackend()
    raise RuntimeError(f"TICKETS_BACKEND={name!r} is not available yet; the GitHub backend arrives in Phase 3.")
