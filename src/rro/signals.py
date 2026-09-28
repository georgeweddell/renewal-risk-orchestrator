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

from rro.governance.gateway import ToolGateway
from rro.risk import RiskSignals, days_until

CLOSED_STAGES = {"closedwon", "closedlost"}


class SignalError(RuntimeError):
    pass


@dataclass
class AccountSignals:
    slug: str
    name: str
    renewal_date: date
    signals: RiskSignals


async def collect_signals(gateway: ToolGateway, slug: str, run_id: str | None = None) -> AccountSignals:
    async def call(tool: str, args: dict) -> dict:
        outcome = await gateway.call(tool, args, run_id=run_id, actor="system")
        if outcome.is_error:
            raise SignalError(f"{tool} failed for {slug}: {outcome.text}")
        return json.loads(outcome.text)

    companies = await call(
        "crm__search_crm_objects",
        {
            "objectType": "companies",
            "filterGroups": [{"filters": [{"propertyName": "account_slug", "operator": "EQ", "value": slug}]}],
        },
    )
    if companies["total"] != 1:
        raise SignalError(f"Expected one company with account_slug '{slug}', found {companies['total']}")
    company = companies["results"][0]

    with_deals = await call(
        "crm__get_crm_objects", {"objectType": "companies", "objectIds": [company["id"]], "associations": ["deals"]}
    )
    deal_ids = with_deals["results"][0]["associations"]["deals"]
    deals = await call("crm__get_crm_objects", {"objectType": "deals", "objectIds": deal_ids})
    open_deals = [d for d in deals["results"] if d["properties"]["dealstage"] not in CLOSED_STAGES]
    if len(open_deals) != 1:
        raise SignalError(f"Expected one open renewal deal for '{slug}', found {len(open_deals)}")
    renewal_date = date.fromisoformat(open_deals[0]["properties"]["closedate"][:10])

    tickets = await call("tickets__list_issues", {"account_id": slug})
    usage = await call("usage__get_usage_trend", {"account_id": slug})

    return AccountSignals(
        slug=slug,
        name=company["properties"]["name"],
        renewal_date=renewal_date,
        signals=RiskSignals(
            usage_pct_change=usage["summary"]["pct_change"],
            open_p1=tickets["summary"]["open_p1"],
            open_p2=tickets["summary"]["open_p2"],
            days_to_renewal=days_until(renewal_date, datetime.now(UTC).date()),
        ),
    )
