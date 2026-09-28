"""The fake systems of record used in mock mode.

One SQLite file stands in for HubSpot, GitHub Issues and PostHog. It is
deliberately separate from the orchestrator's own database (runs, audit log,
memory): the agent should only ever reach this data through MCP servers,
exactly as it would reach the real systems.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
-- CRM (HubSpot-shaped: every property value is a string, like the HubSpot API)
CREATE TABLE crm_objects (
    object_type TEXT NOT NULL,          -- companies | deals
    id          TEXT NOT NULL,
    properties  TEXT NOT NULL,          -- JSON object of string -> string
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (object_type, id)
);

CREATE TABLE crm_associations (
    from_type TEXT NOT NULL,
    from_id   TEXT NOT NULL,
    to_type   TEXT NOT NULL,
    to_id     TEXT NOT NULL,
    PRIMARY KEY (from_type, from_id, to_type, to_id)
);

CREATE TABLE crm_owners (
    id         TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    first_name TEXT NOT NULL,
    last_name  TEXT NOT NULL
);

-- Ticketing (GitHub Issues-shaped)
CREATE TABLE tickets (
    number     INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    state      TEXT NOT NULL,           -- open | closed
    priority   TEXT NOT NULL,           -- P1 | P2 | P3
    labels     TEXT NOT NULL,           -- JSON array
    created_at TEXT NOT NULL,
    closed_at  TEXT,
    url        TEXT NOT NULL
);

-- Product analytics (weekly aggregates of PostHog events)
CREATE TABLE usage_weekly (
    account_id   TEXT NOT NULL,
    week_start   TEXT NOT NULL,         -- ISO date (Monday)
    active_users INTEGER NOT NULL,
    events       INTEGER NOT NULL,
    PRIMARY KEY (account_id, week_start)
);
"""


def db_path_from_env() -> Path:
    """Mock servers are told where the data lives via RRO_MOCK_DB."""
    value = os.environ.get("RRO_MOCK_DB")
    if not value:
        raise RuntimeError("RRO_MOCK_DB is not set; the gateway passes it when it starts a mock server.")
    path = Path(value)
    if not path.exists():
        raise RuntimeError(f"Mock database not found at {path}. Run `rro seed` first.")
    return path


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """A connection that commits on success and is always closed."""
    conn = connect(path or db_path_from_env())
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def create(path: Path) -> sqlite3.Connection:
    """Create a fresh, empty mock database (replacing any existing one)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    conn = connect(path)
    conn.executescript(SCHEMA)
    return conn


def loads(value: str | None):
    return json.loads(value) if value else None
