"""The human approval gate and the executor.

Flow:
  1. The agent calls a propose_* tool. The exact write it wants (system, tool,
     arguments) is stored as a pending approval, with a SHA-256 hash of that
     payload.
  2. A human approves or rejects it (web UI or CLI now, Slack in Phase 4).
  3. On approval, the executor (plain code, no LLM) replays the stored call
     through the Tool Gateway as actor "executor". The policy lets a write
     through only if a human approved this exact payload and it hasn't run yet.
  4. Either way, the decision is recorded in memory for future runs to learn from.

The model is not involved after step 1, so what the human approved is exactly
what gets written.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from mcp_servers.memory.store import Decision, MemoryStore
from rro.db import AuditEntry, Store, utcnow
from rro.governance.gateway import SEP, ToolGateway
from rro.risk import RiskAssessment


class ApprovalError(RuntimeError):
    pass


@dataclass(frozen=True)
class Approval:
    id: str
    run_id: str | None
    created_at: str
    account_slug: str
    account_name: str
    action_type: str
    system: str
    tool: str
    args: dict[str, Any]
    payload_hash: str
    summary: str
    rationale: str
    risk_band: str
    risk_score: int
    drivers: list[str]
    status: str  # pending | approved | rejected | executed | failed
    decided_by: str | None
    decided_at: str | None
    decision_note: str | None
    executed_at: str | None
    result_text: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Approval:
        data = dict(row)
        data["args"] = json.loads(data.pop("args_json"))
        data["drivers"] = json.loads(data.pop("drivers_json"))
        return cls(**data)


def payload_hash(system: str, tool: str, args: dict[str, Any]) -> str:
    canonical = json.dumps({"system": system, "tool": tool, "args": args}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class ApprovalStore:
    """Pending and decided actions, in the orchestrator's database."""

    def __init__(self, store: Store):
        self.conn = store.conn

    def create(
        self,
        *,
        run_id: str | None,
        account_slug: str,
        account_name: str,
        action_type: str,
        system: str,
        tool: str,
        args: dict[str, Any],
        summary: str,
        rationale: str,
        assessment: RiskAssessment,
    ) -> Approval:
        approval_id = f"appr-{uuid.uuid4().hex[:8]}"
        self.conn.execute(
            "INSERT INTO approvals (id, run_id, created_at, account_slug, account_name, action_type, system, tool,"
            " args_json, payload_hash, summary, rationale, risk_band, risk_score, drivers_json, status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
            (
                approval_id, run_id, utcnow(), account_slug, account_name, action_type, system, tool,
                json.dumps(args, sort_keys=True), payload_hash(system, tool, args), summary, rationale,
                assessment.band, assessment.score, json.dumps(assessment.drivers),
            ),
        )  # fmt: skip
        return self.get(approval_id)

    def get(self, approval_id: str) -> Approval:
        row = self.conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            raise ApprovalError(f"No approval with ID {approval_id}")
        return Approval.from_row(row)

    def list(self, *, status: str | None = None, run_id: str | None = None) -> list[Approval]:
        query, params = "SELECT * FROM approvals WHERE 1=1", []
        if status:
            query, params = query + " AND status=?", [*params, status]
        if run_id:
            query, params = query + " AND run_id=?", [*params, run_id]
        rows = self.conn.execute(query + " ORDER BY created_at DESC, rowid DESC", params).fetchall()
        return [Approval.from_row(r) for r in rows]

    def check(self, approval_id: str, system: str, tool: str, args: dict[str, Any]) -> bool:
        """The policy hook: is there a human-approved, not-yet-executed action for exactly this call?"""
        row = self.conn.execute("SELECT status, payload_hash FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return row is not None and row["status"] == "approved" and row["payload_hash"] == payload_hash(system, tool, args)

    def _update(self, approval_id: str, **fields: Any) -> None:
        assignments = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE approvals SET {assignments} WHERE id=?", (*fields.values(), approval_id))


class ApprovalService:
    """Records human decisions and runs approved actions."""

    def __init__(self, store: Store, approvals: ApprovalStore, memory: MemoryStore, gateway: ToolGateway):
        self.store = store
        self.approvals = approvals
        self.memory = memory
        self.gateway = gateway

    async def approve(self, approval_id: str, *, by: str, note: str | None = None) -> Approval:
        self._decide(approval_id, "approved", by=by, note=note)
        approval = self.approvals.get(approval_id)

        outcome = await self.gateway.call(
            f"{approval.system}{SEP}{approval.tool}",
            approval.args,
            run_id=approval.run_id,
            actor="executor",
            approval_id=approval.id,
        )
        status = "failed" if outcome.is_error else "executed"
        self.approvals._update(approval.id, status=status, executed_at=utcnow(), result_text=outcome.text[:2000])
        if status == "executed":
            self._remember(approval, "approved", by, note, outcome="pending")
        return self.approvals.get(approval.id)

    def reject(self, approval_id: str, *, by: str, note: str) -> Approval:
        if not note.strip():
            raise ApprovalError("A rejection needs a reason. It's what future runs learn from.")
        self._decide(approval_id, "rejected", by=by, note=note)
        approval = self.approvals.get(approval_id)
        self._remember(approval, "rejected", by, note, outcome=None)
        return approval

    def _decide(self, approval_id: str, status: str, *, by: str, note: str | None) -> None:
        approval = self.approvals.get(approval_id)
        if approval.status != "pending":
            raise ApprovalError(f"{approval_id} is already {approval.status}")
        self.approvals._update(approval_id, status=status, decided_by=by, decided_at=utcnow(), decision_note=note)
        if approval.run_id:
            self.store.complete_if_decided(approval.run_id)
        self.store.add_audit(
            AuditEntry(
                run_id=approval.run_id, actor=f"human:{by}", system="approvals",
                tool="approve" if status == "approved" else "reject", scope="decision", decision="allowed",
                reason=note, args={"approval_id": approval_id, "summary": approval.summary}, approval_id=approval_id,
            )  # fmt: skip
        )

    def _remember(self, approval: Approval, status: str, by: str, note: str | None, outcome: str | None) -> None:
        self.memory.record(
            Decision(
                account_slug=approval.account_slug,
                account_name=approval.account_name,
                decided_on=datetime.now(UTC).date().isoformat(),
                action_type=approval.action_type,
                risk_band=approval.risk_band,
                risk_score=approval.risk_score,
                drivers=approval.drivers,
                proposal=approval.summary,
                status=status,
                approver=by,
                note=note,
                outcome=outcome,
                source=f"run:{approval.run_id}",
            )
        )
