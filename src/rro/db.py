"""The orchestrator's own state: agent runs and the audit log.

The audit log is append-only at the database level: triggers reject any
UPDATE or DELETE, so not even the app can rewrite history.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    instruction     TEXT NOT NULL,
    model           TEXT NOT NULL,
    status          TEXT NOT NULL,         -- running | completed | failed
    account_slug    TEXT,
    risk_score      INTEGER,
    risk_band       TEXT,
    briefing_path   TEXT,
    summary         TEXT,
    error           TEXT,
    usage_json      TEXT                   -- token counts
);

CREATE TABLE IF NOT EXISTS audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT NOT NULL,
    run_id         TEXT,
    actor          TEXT NOT NULL,          -- agent | executor | system
    system         TEXT NOT NULL,          -- crm | tickets | usage | local
    tool           TEXT NOT NULL,
    scope          TEXT NOT NULL,          -- read | write | local | unlisted
    decision       TEXT NOT NULL,          -- allowed | denied
    reason         TEXT,
    args_json      TEXT NOT NULL,
    result_preview TEXT,
    is_error       INTEGER NOT NULL DEFAULT 0,
    latency_ms     INTEGER,
    approval_id    TEXT
);

CREATE INDEX IF NOT EXISTS audit_by_run ON audit_log (run_id, id);

CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


@dataclass
class AuditEntry:
    actor: str
    system: str
    tool: str
    scope: str
    decision: str
    args: dict[str, Any]
    run_id: str | None = None
    reason: str | None = None
    result_preview: str | None = None
    is_error: bool = False
    latency_ms: int | None = None
    approval_id: str | None = None


class Store:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @classmethod
    def open(cls, path: Path) -> Store:
        return cls(connect(path))

    # --- runs -----------------------------------------------------------------
    def create_run(self, instruction: str, model: str) -> str:
        run_id = uuid.uuid4().hex[:12]
        self.conn.execute(
            "INSERT INTO runs (id, started_at, instruction, model, status) VALUES (?, ?, ?, ?, 'running')",
            (run_id, utcnow(), instruction, model),
        )
        return run_id

    def finish_run(self, run_id: str, *, status: str, **fields: Any) -> None:
        if "usage" in fields:
            fields["usage_json"] = json.dumps(fields.pop("usage"))
        fields |= {"status": status, "finished_at": utcnow()}
        assignments = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE runs SET {assignments} WHERE id=?", (*fields.values(), run_id))

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

    def latest_run(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 1").fetchone()

    # --- audit ----------------------------------------------------------------
    def add_audit(self, entry: AuditEntry) -> int:
        cur = self.conn.execute(
            "INSERT INTO audit_log (ts, run_id, actor, system, tool, scope, decision, reason, args_json,"
            " result_preview, is_error, latency_ms, approval_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                utcnow(), entry.run_id, entry.actor, entry.system, entry.tool, entry.scope, entry.decision,
                entry.reason, json.dumps(_redact(entry.args), sort_keys=True), entry.result_preview,
                int(entry.is_error), entry.latency_ms, entry.approval_id,
            ),
        )  # fmt: skip
        return cur.lastrowid

    def audit_for_run(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM audit_log WHERE run_id=? ORDER BY id", (run_id,)).fetchall()

    def recent_audit(self, limit: int = 50) -> list[sqlite3.Row]:
        rows = self.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return rows[::-1]


SENSITIVE_KEYS = ("token", "secret", "password", "api_key", "authorization")


def _redact(value: Any) -> Any:
    """Keep credentials out of the audit log even if one ends up in tool arguments."""
    if isinstance(value, dict):
        return {k: "[redacted]" if any(s in k.lower() for s in SENSITIVE_KEYS) else _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value
