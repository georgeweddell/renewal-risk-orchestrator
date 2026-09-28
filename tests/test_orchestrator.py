"""The agent loop, driven by a scripted stand-in for Claude against real MCP servers."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from conftest import make_gateway
from rro.agent.orchestrator import Orchestrator
from rro.risk import RiskConfig


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


def renewal_date(days: int) -> str:
    return (datetime.now(UTC).date() + timedelta(days=days)).isoformat() + "T00:00:00.000Z"


async def run_agent(settings, store, llm):
    config = RiskConfig.load(settings.config_dir / "risk.yaml")
    async with make_gateway(settings, store) as gateway:
        return await Orchestrator(settings, gateway, llm, store, config).run("Prep the renewal for Halcyon Robotics")


async def test_full_run_writes_a_briefing_and_audits_every_call(settings, store):
    llm = ScriptedLLM(
        response(tool_use("t1", "crm__search_crm_objects", objectType="companies", query="Halcyon")),
        response(
            tool_use("t2", "tickets__list_issues", account_id="halcyon-robotics"),
            tool_use("t3", "usage__get_usage_trend", account_id="halcyon-robotics"),
            # An attempt to write directly: must be refused without ending the run.
            tool_use("t4", "crm__manage_crm_objects", objectType="deals", objectId="5014", properties={"renewal_risk_level": "critical"}),
        ),
        response(tool_use("t5", "score_renewal_risk", account_slug="halcyon-robotics", usage_pct_change=-45.0, open_p1=2, open_p2=0, renewal_date=renewal_date(28))),
        response(tool_use("t6", "write_briefing", account_slug="halcyon-robotics", markdown=BRIEFING)),
        response(text("Halcyon is critical: usage is down 45% with two open P1s, 28 days out."), stop_reason="end_turn"),
    )  # fmt: skip

    result = await run_agent(settings, store, llm)

    assert result.status == "completed", result.error
    assert result.assessment.band == "critical" and result.assessment.score == 90
    assert result.briefing_path.read_text(encoding="utf-8").startswith(BRIEFING)
    assert f"rro audit {result.run_id}" in result.briefing_path.read_text(encoding="utf-8")
    assert result.summary.startswith("Halcyon is critical")

    # The model was never offered the write tool...
    assert "crm__manage_crm_objects" not in llm.requests[0]["tools"]
    # ...and when it tried anyway, it got a policy error back as the tool result.
    tool_results = llm.requests[2]["messages"][-1]["content"]
    denied = next(r for r in tool_results if r["tool_use_id"] == "t4")
    assert denied["is_error"] and denied["content"].startswith("Denied by policy")

    audit = [(r["system"], r["tool"], r["decision"]) for r in store.audit_for_run(result.run_id)]
    assert audit[0] == ("crm", "search_crm_objects", "allowed")
    assert set(audit[1:4]) == {
        ("tickets", "list_issues", "allowed"),
        ("usage", "get_usage_trend", "allowed"),
        ("crm", "manage_crm_objects", "denied"),
    }
    assert audit[4:] == [("local", "score_renewal_risk", "allowed"), ("local", "write_briefing", "allowed")]

    run = store.get_run(result.run_id)
    assert run["status"] == "completed" and run["risk_band"] == "critical" and run["account_slug"] == "halcyon-robotics"


async def test_briefing_requires_a_score_first(settings, store):
    llm = ScriptedLLM(
        response(tool_use("t1", "write_briefing", account_slug="halcyon-robotics", markdown=BRIEFING)),
        response(text("Done."), stop_reason="end_turn"),
        response(text("Still done."), stop_reason="end_turn"),
    )
    result = await run_agent(settings, store, llm)
    first_result = llm.requests[1]["messages"][-1]["content"][0]
    assert first_result["is_error"] and "score_renewal_risk" in first_result["content"]
    assert result.status == "failed" and "without writing a briefing" in result.error


async def test_agent_is_nudged_once_if_it_stops_without_a_briefing(settings, store):
    llm = ScriptedLLM(
        response(tool_use("t1", "score_renewal_risk", account_slug="halcyon-robotics", usage_pct_change=-45.0, open_p1=2, open_p2=0, renewal_date=renewal_date(28))),
        response(text("Halcyon looks critical."), stop_reason="end_turn"),
        response(tool_use("t2", "write_briefing", account_slug="halcyon-robotics", markdown=BRIEFING)),
        response(text("Briefing saved."), stop_reason="end_turn"),
    )  # fmt: skip
    result = await run_agent(settings, store, llm)
    assert result.status == "completed"
    assert "write_briefing" in llm.requests[2]["messages"][-1]["content"]


async def test_refusal_fails_the_run_cleanly(settings, store):
    refusal = response(stop_reason="refusal")
    refusal.stop_details = SimpleNamespace(category="cyber")
    result = await run_agent(settings, store, ScriptedLLM(refusal))
    assert result.status == "failed" and "declined" in result.error
    assert store.get_run(result.run_id)["status"] == "failed"
