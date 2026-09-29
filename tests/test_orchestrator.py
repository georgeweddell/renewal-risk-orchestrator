"""The agent loop, driven by a scripted stand-in for Claude against real MCP servers."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace


def tool_use(id, name, **input):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def text(value):
    return SimpleNamespace(type="text", text=value)


def response(*content, stop_reason="tool_use"):
    usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return SimpleNamespace(content=list(content), stop_reason=stop_reason, usage=usage)


class ScriptedLLM:
    """Returns canned responses in order and records what it was sent."""

    model = "scripted"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    async def create(self, *, system, tools, messages):
        self.requests.append({"tools": [t["name"] for t in tools], "messages": list(messages)})
        return self.responses.pop(0)


BRIEFING = "# Renewal briefing: Halcyon Robotics\n\n**Risk: critical (90/100)**"
HALCYON = "halcyon-robotics"
RENEWAL_DEAL, ORIGINAL_DEAL = "5014", "5013"  # Halcyon's open renewal and its closed-won original deal


def renewal_date(days: int) -> str:
    return (datetime.now(UTC).date() + timedelta(days=days)).isoformat() + "T00:00:00.000Z"


def score_call(id="ts"):
    return tool_use(id, "score_renewal_risk", account_slug=HALCYON, usage_pct_change=-45.0, open_p1=2, open_p2=0, renewal_date=renewal_date(28))


def result_for(llm, request_index, tool_use_id):
    return next(r for r in llm.requests[request_index]["messages"][-1]["content"] if r["tool_use_id"] == tool_use_id)


async def run_agent(rt, llm):
    async with rt.gateway:
        return await rt.orchestrator(llm).run("Prep the renewal for Halcyon Robotics")


async def test_full_run_proposes_changes_and_audits_every_call(rt):
    llm = ScriptedLLM(
        response(tool_use("t1", "crm__search_crm_objects", objectType="companies", query="Halcyon")),
        response(
            tool_use("t2", "tickets__list_issues", account_id=HALCYON),
            tool_use("t3", "usage__get_usage_trend", account_id=HALCYON),
            # An attempt to write directly: must be refused without ending the run.
            tool_use("t4", "crm__manage_crm_objects", objectType="deals", objectId=RENEWAL_DEAL, properties={"renewal_risk_level": "critical"}),
        ),
        response(score_call("t5"), tool_use("t6", "memory__get_account_history", account_id=HALCYON)),
        response(
            tool_use("t7", "propose_crm_update", account_slug=HALCYON, deal_id=RENEWAL_DEAL, reason="WAU -45%, 2 open P1s, renews in 28 days"),
            tool_use("t8", "propose_pricing_exception", account_slug=HALCYON, deal_id=ORIGINAL_DEAL, discount_pct=10, rationale="r", conditions="c"),
        ),
        response(tool_use("t9", "write_briefing", account_slug=HALCYON, markdown=BRIEFING)),
        response(text("Halcyon is critical: usage is down 45% with two open P1s, 28 days out."), stop_reason="end_turn"),
    )  # fmt: skip

    result = await run_agent(rt, llm)

    assert result.status == "awaiting_approval", result.error
    assert result.assessment.band == "critical" and result.assessment.score == 90
    assert result.briefing_path.read_text(encoding="utf-8").startswith(BRIEFING)

    # The model was never offered the write tool, and its direct attempt came back as a policy error.
    assert "crm__manage_crm_objects" not in llm.requests[0]["tools"]
    denied = result_for(llm, 2, "t4")
    assert denied["is_error"] and denied["content"].startswith("Denied by policy")

    # Memory was consulted.
    assert "15% discount" in result_for(llm, 3, "t6")["content"]

    # One proposal queued; the pricing exception on a closed deal was refused.
    assert "is closed" in result_for(llm, 4, "t8")["content"]
    (approval_id,) = result.approval_ids
    approval = rt.approvals.get(approval_id)
    assert approval.status == "pending" and approval.action_type == "crm_risk_update"
    # Level and score come from the engine, not the model.
    assert approval.args == {
        "objectType": "deals",
        "objectId": RENEWAL_DEAL,
        "properties": {"renewal_risk_level": "critical", "renewal_risk_score": "90", "renewal_risk_reason": "WAU -45%, 2 open P1s, renews in 28 days"},
    }  # fmt: skip

    audit = [(r["actor"], r["system"], r["tool"], r["decision"]) for r in rt.store.audit_for_run(result.run_id)]
    assert ("agent", "crm", "manage_crm_objects", "denied") in audit
    assert ("agent", "memory", "get_account_history", "allowed") in audit
    assert ("system", "crm", "search_crm_objects", "allowed") in audit  # the proposal's deal check
    assert not any(tool == "manage_crm_objects" and decision == "allowed" for _, _, tool, decision in audit)

    run = rt.store.get_run(result.run_id)
    assert run["status"] == "awaiting_approval" and run["risk_band"] == "critical"

    # Once its only proposal is decided, the run is complete.
    rt.approval_service().reject(approval_id, by="Sam Reyes", note="Not yet")
    assert rt.store.get_run(result.run_id)["status"] == "completed"


async def test_briefing_and_proposals_require_a_score_first(rt):
    llm = ScriptedLLM(
        response(
            tool_use("t1", "write_briefing", account_slug=HALCYON, markdown=BRIEFING),
            tool_use("t2", "propose_crm_update", account_slug=HALCYON, deal_id=RENEWAL_DEAL, reason="x"),
        ),
        response(text("Done."), stop_reason="end_turn"),
        response(text("Still done."), stop_reason="end_turn"),
    )
    result = await run_agent(rt, llm)
    for tool_use_id in ("t1", "t2"):
        refused = result_for(llm, 1, tool_use_id)
        assert refused["is_error"] and "score_renewal_risk" in refused["content"]
    assert result.status == "failed" and "without writing a briefing" in result.error
    assert rt.approvals.list() == []


async def test_agent_is_nudged_once_if_it_stops_without_a_briefing(rt):
    llm = ScriptedLLM(
        response(score_call()),
        response(text("Halcyon looks critical."), stop_reason="end_turn"),
        response(tool_use("t2", "write_briefing", account_slug=HALCYON, markdown=BRIEFING)),
        response(text("Briefing saved."), stop_reason="end_turn"),
    )
    result = await run_agent(rt, llm)
    assert result.status == "completed"
    assert "write_briefing" in llm.requests[2]["messages"][-1]["content"]


async def test_refusal_fails_the_run_cleanly(rt):
    refusal = response(stop_reason="refusal")
    refusal.stop_details = SimpleNamespace(category="cyber")
    result = await run_agent(rt, ScriptedLLM(refusal))
    assert result.status == "failed" and "declined" in result.error
    assert rt.store.get_run(result.run_id)["status"] == "failed"
