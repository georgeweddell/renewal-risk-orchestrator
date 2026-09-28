"""Tools implemented inside the orchestrator rather than by an MCP server.

They aren't systems of record, so they don't need MCP. They still go
through the audit log, like every other call the agent makes.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from pydantic import ValidationError

from rro.db import AuditEntry, Store
from rro.risk import RiskAssessment, RiskConfig, RiskSignals, days_until, score
from rro.settings import Settings

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

SCORE_TOOL = {
    "name": "score_renewal_risk",
    "description": (
        "Score an account's renewal risk with the deterministic risk engine. Pass the numbers exactly "
        "as the source systems reported them. Returns the score (0-100), the band "
        "(healthy / at-risk / critical) and the factors behind it."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "account_slug": {"type": "string", "description": "The company's account_slug from the CRM."},
            "usage_pct_change": {
                "type": "number",
                "description": "summary.pct_change from usage__get_usage_trend.",
            },
            "open_p1": {"type": "integer", "description": "summary.open_p1 from tickets__list_issues."},
            "open_p2": {"type": "integer", "description": "summary.open_p2 from tickets__list_issues."},
            "renewal_date": {
                "type": "string",
                "description": "closedate of the open renewal deal from the CRM, as returned (ISO 8601).",
            },
        },
        "required": ["account_slug", "usage_pct_change", "open_p1", "open_p2", "renewal_date"],
        "additionalProperties": False,
    },
}

BRIEFING_TOOL = {
    "name": "write_briefing",
    "description": (
        "Save the account team briefing as a markdown file. Call once, after score_renewal_risk, "
        "with the complete briefing."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "account_slug": {"type": "string"},
            "markdown": {"type": "string", "description": "The full briefing in markdown."},
        },
        "required": ["account_slug", "markdown"],
        "additionalProperties": False,
    },
}


class LocalTools:
    definitions = [SCORE_TOOL, BRIEFING_TOOL]
    names = frozenset(t["name"] for t in definitions)

    def __init__(self, settings: Settings, risk_config: RiskConfig, store: Store, run_id: str, model: str):
        self.settings = settings
        self.risk_config = risk_config
        self.store = store
        self.run_id = run_id
        self.model = model
        self.assessments: dict[str, RiskAssessment] = {}
        self.briefing: tuple[str, Path] | None = None  # (account_slug, path)

    async def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        started = perf_counter()
        try:
            handler = {"score_renewal_risk": self._score, "write_briefing": self._write_briefing}[name]
            text, is_error = handler(args), False
        except (ToolInputError, ValidationError) as exc:
            text, is_error = f"Error: {exc}", True
        except KeyError as exc:
            text, is_error = f"Error: missing argument {exc}", True
        self.store.add_audit(
            AuditEntry(
                run_id=self.run_id, actor="agent", system="local", tool=name, scope="local",
                decision="allowed", args=_audit_args(name, args), result_preview=text[:600],
                is_error=is_error, latency_ms=round((perf_counter() - started) * 1000),
            )  # fmt: skip
        )
        return text, is_error

    def _score(self, args: dict[str, Any]) -> str:
        slug = _slug(args["account_slug"])
        try:
            renewal = date.fromisoformat(str(args["renewal_date"])[:10])
        except ValueError as exc:
            raise ToolInputError(f"renewal_date '{args['renewal_date']}' isn't an ISO date") from exc
        signals = RiskSignals(
            usage_pct_change=args["usage_pct_change"],
            open_p1=args["open_p1"],
            open_p2=args["open_p2"],
            days_to_renewal=days_until(renewal, datetime.now(UTC).date()),
        )
        assessment = score(signals, self.risk_config)
        self.assessments[slug] = assessment
        return assessment.model_dump_json()

    def _write_briefing(self, args: dict[str, Any]) -> str:
        slug = _slug(args["account_slug"])
        if slug not in self.assessments:
            raise ToolInputError(f"Call score_renewal_risk for '{slug}' before writing its briefing.")
        stamp = datetime.now(UTC)
        path = self.settings.output_dir / "briefings" / f"{slug}-{stamp:%Y%m%d-%H%M%S}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        footer = (
            f"\n\n---\n_Generated by Renewal Risk Orchestrator · run `{self.run_id}` · {self.model} · "
            f"{stamp:%Y-%m-%d %H:%M} UTC. Every data access behind this briefing is in the audit log: "
            f"`rro audit {self.run_id}`._\n"
        )
        path.write_text(args["markdown"].rstrip() + footer, encoding="utf-8")
        self.briefing = (slug, path)
        return json.dumps({"saved_to": str(path.relative_to(self.settings.rro_home)), "chars": len(args["markdown"])})


class ToolInputError(ValueError):
    pass


def _slug(value: str) -> str:
    if not SLUG.match(value):
        raise ToolInputError(f"'{value}' isn't a valid account_slug")
    return value


def _audit_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """The briefing body is saved to disk; the audit log records its size, not the text."""
    if name == "write_briefing":
        return {**args, "markdown": f"<{len(args.get('markdown', ''))} chars>"}
    return args
