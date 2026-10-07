"""The renewal decision memory: past proposals, what a human decided, and what happened next.

The orchestrator's approval executor is the only writer. The agent reads it
through the memory MCP server, with the same scopes and audit as any system.
Matching is on structured fields (risk band and drivers), not embeddings:
the records are structured, and a precedent should be explainable.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_slug  TEXT NOT NULL,
    account_name  TEXT NOT NULL,
    decided_on    TEXT NOT NULL,        -- ISO date
    action_type   TEXT NOT NULL,        -- crm_risk_update | pricing_exception | evidence_purchase
    risk_band     TEXT NOT NULL,
    risk_score    INTEGER NOT NULL,
    drivers       TEXT NOT NULL,        -- JSON array of driver codes
    proposal      TEXT NOT NULL,        -- what was proposed, in words
    status        TEXT NOT NULL,        -- approved | rejected
    approver      TEXT NOT NULL,
    note          TEXT,                 -- the approver's reasoning
    outcome       TEXT,                 -- renewed | churned | downgraded | pending | NULL; purchases: useful | not_useful
    outcome_note  TEXT,
    source        TEXT NOT NULL         -- seed | run:<run_id>
);
CREATE INDEX IF NOT EXISTS decisions_by_account ON decisions (account_slug, decided_on);
"""


@dataclass
class Decision:
    account_slug: str
    account_name: str
    decided_on: str
    action_type: str
    risk_band: str
    risk_score: int
    drivers: list[str]
    proposal: str
    status: str
    approver: str
    source: str
    note: str | None = None
    outcome: str | None = None
    outcome_note: str | None = None
    id: int | None = field(default=None)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["days_ago"] = (datetime.now(UTC).date() - date.fromisoformat(self.decided_on)).days
        return data


class MemoryStore:
    def __init__(self, path: Path):
        self.path = path

    @classmethod
    def create(cls, path: Path) -> MemoryStore:
        """A fresh, empty memory (replacing any existing one)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        store = cls(path)
        with store._session() as conn:
            conn.executescript(SCHEMA)
        return store

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def record(self, decision: Decision) -> int:
        values = asdict(decision)
        values.pop("id")
        values["drivers"] = json.dumps(values["drivers"])
        with self._session() as conn:
            cur = conn.execute(
                f"INSERT INTO decisions ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
                tuple(values.values()),
            )
            return cur.lastrowid

    def get(self, decision_id: int) -> Decision | None:
        with self._session() as conn:
            row = conn.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
        return _decision(row) if row else None

    def set_outcome(self, decision_id: int, outcome: str, note: str | None) -> None:
        with self._session() as conn:
            conn.execute("UPDATE decisions SET outcome=?, outcome_note=? WHERE id=?", (outcome, note, decision_id))

    def all(self) -> list[Decision]:
        with self._session() as conn:
            rows = conn.execute("SELECT * FROM decisions ORDER BY decided_on DESC, id DESC").fetchall()
        return [_decision(r) for r in rows]

    def for_account(self, account_slug: str) -> list[Decision]:
        return [d for d in self.all() if d.account_slug == account_slug]


def _decision(row: sqlite3.Row) -> Decision:
    data = dict(row)
    data["drivers"] = json.loads(data["drivers"])
    return Decision(**data)
