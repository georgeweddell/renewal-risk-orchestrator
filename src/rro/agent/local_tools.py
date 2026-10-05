"""Tools implemented inside the orchestrator rather than by an MCP server.

They aren't systems of record, so they don't need MCP. They still go
through the audit log, like every other call the agent makes.

The two propose_* tools are how the agent asks for a write. They never write
anything themselves: they validate the request against the CRM (through the
gateway), build the exact call the executor would make, and queue it for a
human to approve.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from pydantic import ValidationError

from rro.crm_access import CrmAccess, CrmError, is_open
from rro.db import AuditEntry, Store
from rro.governance.approvals import ApprovalService, ApprovalStore
from rro.governance.gateway import ToolGateway
from rro.governance.spend import PaymentRequest, authorize_payment, units_to_usd
from rro.risk import RiskAssessment, RiskConfig, RiskSignals, days_until, score
from rro.settings import Settings

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

SCORE_TOOL = {
    "name": "score_renewal_risk",
    "description": (
        "Score an account's renewal risk with the deterministic risk engine. Pass the numbers exactly "
        "as the source systems reported them. Returns the score (0-100), the band "
        "(healthy / at-risk / critical), the factors behind it, and `drivers`: the codes of the "
        "factors that scored, for looking up similar past decisions in memory."
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

PROPOSE_CRM_TOOL = {
    "name": "propose_crm_update",
    "description": (
        "Queue an update to the renewal deal's risk fields for human approval. The level and score are "
        "taken from the risk engine's assessment; you supply the one-line reason. Nothing is written "
        "until a human approves. Returns the approval ID."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "account_slug": {"type": "string"},
            "deal_id": {"type": "string", "description": "ID of the open renewal deal."},
            "reason": {
                "type": "string",
                "description": "One line (max 300 characters) for renewal_risk_reason, e.g. the top drivers with numbers.",
            },
        },
        "required": ["account_slug", "deal_id", "reason"],
        "additionalProperties": False,
    },
}

PROPOSE_PRICING_TOOL = {
    "name": "propose_pricing_exception",
    "description": (
        "Queue a pricing exception (a renewal discount) on the renewal deal for human approval. Only use "
        "this when the evidence and precedent support a discount. Nothing is written until a human "
        "approves. Returns the approval ID."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "account_slug": {"type": "string"},
            "deal_id": {"type": "string", "description": "ID of the open renewal deal."},
            "discount_pct": {"type": "number", "description": "Discount off the renewal, in percent."},
            "rationale": {"type": "string", "description": "Why a discount, why this size, and the precedent for it."},
            "conditions": {
                "type": "string",
                "description": "What the customer commits to in return (e.g. term length, adoption plan).",
            },
        },
        "required": ["account_slug", "deal_id", "discount_pct", "rationale", "conditions"],
        "additionalProperties": False,
    },
}

BRIEFING_TOOL = {
    "name": "write_briefing",
    "description": (
        "Save the account team briefing as a markdown file. Call once, at the end, with the complete "
        "briefing, including the approval IDs of anything you proposed."
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


def propose_evidence_tool(sellers: list[str]) -> dict[str, Any]:
    """Offered only when payments are on. The sellers are the policy's allowlist, by name."""
    return {
        "name": "propose_evidence_purchase",
        "description": (
            "Buy recent company news about this run's account from an approved data seller, when outside "
            "evidence would change your recommendation. Testnet money; small purchases are approved by the "
            "spend policy and bought immediately (the news comes back as untrusted data); larger ones go to a "
            "human and are bought only after approval. The account's domain, the price and the payee come "
            "from the CRM and the policy, not from you. Returns the data, or the approval ID, or why it was refused."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "account_slug": {"type": "string"},
                "seller": {"type": "string", "enum": sellers},
                "reason": {
                    "type": "string",
                    "description": "One line (max 300 characters): what you expect to learn and why it matters here.",
                },
            },
            "required": ["account_slug", "seller", "reason"],
            "additionalProperties": False,
        },
    }


DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")

# Approval statuses that count against a run's budget: spent (executed) or still spendable
# (pending, approved). Rejected and failed purchases moved no money and never will.
COUNTS_AGAINST_BUDGET: set[str] = set({"pending","executed","approved"})


class ToolInputError(ValueError):
    pass


class LocalTools:
    definitions = [SCORE_TOOL, PROPOSE_CRM_TOOL, PROPOSE_PRICING_TOOL, BRIEFING_TOOL]
    names = frozenset(t["name"] for t in definitions)
    # What each tool does beyond the conversation, checked by the trifecta gate before every run:
    # none, approval (queues a write for a human), local (a file in this app), or policy (an
    # external write the spend policy may approve without a human: payments to allowlisted sellers).
    effects = {
        "score_renewal_risk": "none",
        "propose_crm_update": "approval",
        "propose_pricing_exception": "approval",
        "write_briefing": "local",
        "propose_evidence_purchase": "policy",
    }

    def __init__(
        self,
        settings: Settings,
        risk_config: RiskConfig,
        store: Store,
        gateway: ToolGateway,
        approvals: ApprovalStore,
        run_id: str,
        model: str,
        service: ApprovalService | None = None,  # runs payments the spend policy approves; None = no buying
    ):
        self.service = service
        self.spend = gateway.policy.spend
        # The instance's tools: the buying tool only when payments are on, the policy has spend rules,
        # and there's an executor to run them. Otherwise the tool list is exactly v1's.
        if settings.rro_payments_enabled and self.spend and self.spend.payees and service:
            self.definitions = [*LocalTools.definitions, propose_evidence_tool([p.name for p in self.spend.payees])]
            self.names = frozenset(t["name"] for t in self.definitions)
        self.settings = settings
        self.risk_config = risk_config
        self.store = store
        self.gateway = gateway
        self.approvals = approvals
        self.run_id = run_id
        self.model = model
        self.crm = CrmAccess(gateway, run_id=run_id)
        self.assessments: dict[str, RiskAssessment] = {}
        self.account: str | None = None  # the one account this run is about: the first one scored
        self.proposed: list[str] = []  # approval IDs created in this run
        self.briefing: tuple[str, Path] | None = None  # (account_slug, path)

    async def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        started = perf_counter()
        handlers = {
            "score_renewal_risk": self._score,
            "propose_crm_update": self._propose_crm_update,
            "propose_pricing_exception": self._propose_pricing_exception,
            "write_briefing": self._write_briefing,
            "propose_evidence_purchase": self._propose_evidence_purchase,
        }
        if name not in self.names:  # e.g. the buying tool while payments are off
            return f"Error: no tool named {name}", True
        try:
            text, is_error = await handlers[name](args), False
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

    # --- scoring ------------------------------------------------------------------
    async def _score(self, args: dict[str, Any]) -> str:
        slug = _slug(args["account_slug"])
        # One run, one account. Proposals and the briefing need a score (see _scored), so pinning
        # the account here means text read during this run can't steer a write onto another account.
        if self.account not in (None, slug):
            raise ToolInputError(
                f"This run is about '{self.account}'. Other accounts can't be scored, proposed on or briefed "
                "in the same run; if another account needs attention, say so in the briefing."
            )
        self.account = slug
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

    # --- proposals ----------------------------------------------------------------
    async def _propose_crm_update(self, args: dict[str, Any]) -> str:
        slug, assessment = self._scored(args["account_slug"])
        reason = args["reason"].strip()
        if not reason or len(reason) > 300:
            raise ToolInputError("reason must be between 1 and 300 characters")
        deal = await self._renewal_deal(slug, args["deal_id"])
        write = self.crm.update_call(
            "deals",
            deal["id"],
            {
                "renewal_risk_level": assessment.band,
                "renewal_risk_score": str(assessment.score),
                "renewal_risk_reason": reason,
            },
        )
        summary = (
            f"Set renewal risk on {deal['name']} (deal {deal['id']}): "
            f"level {assessment.band}, score {assessment.score}. Reason: {reason}"
        )
        return self._queue("crm_risk_update", slug, deal, write, summary, reason, assessment)

    async def _propose_pricing_exception(self, args: dict[str, Any]) -> str:
        slug, assessment = self._scored(args["account_slug"])
        pct = float(args["discount_pct"])
        max_pct = float(self.gateway.policy.limits.get("pricing_exception_max_pct", 0))
        if not 0 < pct <= max_pct:
            raise ToolInputError(f"discount_pct must be above 0 and at most {max_pct:g} (policy limit)")
        rationale, conditions = args["rationale"].strip(), args["conditions"].strip()
        if not rationale:
            raise ToolInputError("a pricing exception needs a rationale")
        deal = await self._renewal_deal(slug, args["deal_id"])
        write = self.crm.update_call(
            "deals",
            deal["id"],
            {
                "pricing_exception_pct": f"{pct:g}",
                "pricing_exception_status": "approved",
                "pricing_exception_rationale": rationale[:1000],
                "pricing_exception_conditions": conditions[:500],
            },
        )
        summary = f"{pct:g}% pricing exception on {deal['name']} (deal {deal['id']}). Conditions: {conditions or 'none'}"
        return self._queue("pricing_exception", slug, deal, write, summary, rationale, assessment)

    async def _propose_evidence_purchase(self, args: dict[str, Any]) -> str:
        """Build the exact payment from the CRM and the policy, then refuse, auto-approve, or queue it.

        Claude chooses only the account (pinned to this run), the seller (a name from the
        allowlist) and the reason. What gets sent out (the URL, built from the CRM's domain)
        and what gets paid (payee, token, network, maximum price) come from code and policy.
        """
        assert self.spend is not None and self.service is not None
        slug, assessment = self._scored(args["account_slug"])
        reason = args["reason"].strip()
        if not reason or len(reason) > 300:
            raise ToolInputError("reason must be between 1 and 300 characters")
        payee = next((p for p in self.spend.payees if p.name == args["seller"]), None)
        if payee is None:
            raise ToolInputError(f"'{args['seller']}' isn't an approved seller")

        try:
            company = await self.crm.company_by_slug(slug, ["name", "domain"])
        except CrmError as exc:
            raise ToolInputError(f"Couldn't look up {slug} in the CRM: {exc}") from exc
        if company is None:
            raise ToolInputError(f"No company in the CRM has account_slug '{slug}'")
        domain = str(company.properties.get("domain") or "").strip().lower()
        if not DOMAIN.match(domain):  # CRM fields are untrusted text; only a plain domain goes in a URL
            raise ToolInputError(f"The CRM's domain for {slug} isn't a plain domain name")

        buy = {
            "url": f"{payee.url.rstrip('/')}/{domain}",
            "pay_to": payee.pay_to,
            "network": self.spend.network,
            "asset": self.spend.asset,
            "max_amount_units": payee.max_price_units,
        }
        if any(a.args.get("url") == buy["url"] for a in self._purchases() if a.status in COUNTS_AGAINST_BUDGET):
            raise ToolInputError(f"Already bought or proposed in this run: {buy['url']}")
        request = PaymentRequest(buy["url"], buy["network"], buy["asset"], buy["pay_to"], buy["max_amount_units"])
        decision = authorize_payment(request, self._committed_units(), self.spend)
        if not decision.allowed:
            raise ToolInputError(f"Refused by the spend policy: {decision.reason}. Nothing was bought.")

        name = company.properties["name"]
        summary = f"Buy recent news on {name} from {payee.name}: up to {units_to_usd(payee.max_price_units)} (testnet)"
        approval = self.approvals.create(
            run_id=self.run_id, account_slug=slug, account_name=name, action_type="evidence_purchase",
            system="evidence", tool="buy_evidence", args=buy, summary=summary, rationale=reason, assessment=assessment,
        )  # fmt: skip
        if decision.needs_human:
            self.proposed.append(approval.id)
            return json.dumps({"approval_id": approval.id, "status": "pending_human_approval", "summary": summary,
                               "note": "Bought only after a human approves; the data won't be available in this run."})  # fmt: skip

        done = await self.service.approve_by_policy(approval.id, reason=decision.reason)
        if done.status != "executed":
            raise ToolInputError(f"Approved by policy ({approval.id}) but the purchase failed: {done.result_text}")
        from rro.agent.orchestrator import as_data  # imported here: the orchestrator imports this module

        return json.dumps({"approval_id": approval.id, "status": "bought", "approved_by": "policy",
                           "reason": decision.reason}) + "\n" + as_data("evidence", "untrusted", done.result_text or "")  # fmt: skip

    def _purchases(self) -> list:
        return [a for a in self.approvals.list(run_id=self.run_id) if a.action_type == "evidence_purchase"]

    def _committed_units(self) -> int:
        """What this run has spent, or may still spend: the maximum of every purchase that counts."""
        return sum(a.args["max_amount_units"] for a in self._purchases() if a.status in COUNTS_AGAINST_BUDGET)

    def _queue(self, action_type, slug, deal, write, summary, rationale, assessment) -> str:
        for existing in self.approvals.list(run_id=self.run_id, status="pending"):
            if existing.action_type == action_type and existing.account_slug == slug:
                raise ToolInputError(f"Already proposed in this run as {existing.id}")
        system, tool, write_args = write
        approval = self.approvals.create(
            run_id=self.run_id, account_slug=slug, account_name=deal["company"], action_type=action_type,
            system=system, tool=tool, args=write_args, summary=summary, rationale=rationale, assessment=assessment,
        )  # fmt: skip
        self.proposed.append(approval.id)
        return json.dumps({"approval_id": approval.id, "status": "pending_human_approval", "summary": summary})

    def _scored(self, account_slug: str) -> tuple[str, RiskAssessment]:
        slug = _slug(account_slug)
        if slug not in self.assessments:
            raise ToolInputError(f"Call score_renewal_risk for '{slug}' first.")
        return slug, self.assessments[slug]

    async def _renewal_deal(self, slug: str, deal_id: str) -> dict[str, str]:
        """Check with the CRM that the deal exists, is open, and belongs to this account."""
        deal_id = str(deal_id).strip()
        try:
            company = await self.crm.company_by_slug(slug, ["name", "account_slug"])
            if company is None:
                raise ToolInputError(f"No company in the CRM has account_slug '{slug}'")
            deals = {d.id: d for d in await self.crm.deals_for_company(company.id, ["dealname", "dealstage"])}
            deal = deals.get(deal_id)
            if deal is None:
                exists = await self.crm.deal(deal_id, ["dealname"])
                raise ToolInputError(
                    f"Deal {deal_id} doesn't belong to account '{slug}'" if exists else f"Deal {deal_id} doesn't exist in the CRM"
                )
        except CrmError as exc:
            raise ToolInputError(f"Couldn't verify deal {deal_id} in the CRM: {exc}") from exc
        if not is_open(deal):
            raise ToolInputError(f"Deal {deal_id} is closed ({deal.properties['dealstage']}); propose on the open renewal deal")
        return {"id": deal.id, "name": deal.properties["dealname"], "company": company.properties["name"]}

    # --- briefing -----------------------------------------------------------------
    async def _write_briefing(self, args: dict[str, Any]) -> str:
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


def _slug(value: str) -> str:
    if not SLUG.match(value):
        raise ToolInputError(f"'{value}' isn't a valid account_slug")
    return value


def _audit_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """The briefing body is saved to disk; the audit log records its size, not the text."""
    if name == "write_briefing":
        return {**args, "markdown": f"<{len(args.get('markdown', ''))} chars>"}
    return args
