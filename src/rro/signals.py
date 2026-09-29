"""Collect risk signals for an account without the LLM.

This walks the same MCP tools the agent uses, through the same gateway (so it
is audited too, as actor "system"). It gives a ground truth to check the
agent against, and powers `rro score` for checking the risk engine without an
API key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime

from rro.crm_access import CrmAccess, CrmError, is_open
from rro.governance.gateway import ToolGateway
from rro.risk import RiskSignals, days_until


class SignalError(RuntimeError):
    pass


@dataclass
class AccountSignals:
    slug: str
    name: str
    renewal_date: date
    signals: RiskSignals
    renewal_deal_id: str
    open_p1_issues: list[int]


async def collect_signals(gateway: ToolGateway, slug: str, run_id: str | None = None) -> AccountSignals:
    async def call(tool: str, args: dict) -> dict:
        outcome = await gateway.call(tool, args, run_id=run_id, actor="system")
        if outcome.is_error:
            raise SignalError(f"{tool} failed for {slug}: {outcome.text}")
        return json.loads(outcome.text)

    crm = CrmAccess(gateway, run_id=run_id)
    try:
        company = await crm.company_by_slug(slug, ["name", "account_slug"])
        if company is None:
            raise SignalError(f"No company with account_slug '{slug}' in the CRM")
        open_deals = [d for d in await crm.deals_for_company(company.id, ["dealname", "dealstage", "closedate"]) if is_open(d)]
    except CrmError as exc:
        raise SignalError(str(exc)) from exc
    if len(open_deals) != 1:
        raise SignalError(f"Expected one open renewal deal for '{slug}', found {len(open_deals)}")
    renewal_date = date.fromisoformat(open_deals[0].properties["closedate"][:10])

    tickets = await call("tickets__list_issues", {"account_id": slug})
    usage = await call("usage__get_usage_trend", {"account_id": slug})

    return AccountSignals(
        slug=slug,
        name=company.properties["name"],
        renewal_date=renewal_date,
        renewal_deal_id=open_deals[0].id,
        open_p1_issues=[i["number"] for i in tickets["issues"] if i["priority"] == "P1" and i["state"] == "open"],
        signals=RiskSignals(
            usage_pct_change=usage["summary"]["pct_change"],
            open_p1=tickets["summary"]["open_p1"],
            open_p2=tickets["summary"]["open_p2"],
            days_to_renewal=days_until(renewal_date, datetime.now(UTC).date()),
        ),
    )
