"""The trifecta gate: no path from untrusted text to a write that no human approves."""

from rro.agent.local_tools import LocalTools
from rro.governance.gateway import ToolSpec
from rro.governance.trifecta import assess
from rro.runtime import build_runtime
from test_orchestrator import ScriptedLLM, response, text


def spec(system, name, scope="read", read_only=True):
    return ToolSpec(system=system, name=name, description="", input_schema={}, scope=scope, read_only_hint=read_only)


async def test_the_shipped_configuration_passes(rt):
    async with rt.gateway as gateway:
        report = assess(gateway.inventory(), gateway.policy, LocalTools.effects, LocalTools.names)
    assert report.safe and report.unapproved_writes == []
    assert "tickets__get_issue" in report.untrusted and "memory__get_account_history" not in report.untrusted


async def test_a_write_tool_listed_as_read_stops_the_run_before_the_model_is_called(settings):
    # A plausible mistake: moving the CRM write tool to `read` "to save a step".
    policy = settings.config_dir / "policy.yaml"
    config = policy.read_text(encoding="utf-8")
    config = config.replace("read: [search_crm_objects, get_crm_objects, search_owners]\n    write: [manage_crm_objects]",
                            "read: [search_crm_objects, get_crm_objects, search_owners, manage_crm_objects]")  # fmt: skip
    policy.write_text(config, encoding="utf-8")
    rt = build_runtime(settings)
    llm = ScriptedLLM(response(text("never reached"), stop_reason="end_turn"))

    async with rt.gateway:
        result = await rt.orchestrator(llm).run("Prep the renewal for Halcyon Robotics")

    assert result.status == "failed" and "Trifecta gate: refusing to run" in result.error
    assert "crm__manage_crm_objects" in result.error  # the server itself says this tool writes
    assert llm.requests == []  # nothing was read, nothing was sent to the model


def test_server_annotations_can_only_make_the_gate_stricter(rt):
    ticket = spec("tickets", "get_issue")
    silent = spec("crm", "search_crm_objects", read_only=None)  # the server doesn't say
    assert not assess([ticket, silent], rt.policy, {}, []).safe


def test_a_local_tool_must_declare_its_effect(rt):
    ticket = spec("tickets", "get_issue")
    assert assess([ticket], rt.policy, {"write_briefing": "local"}, ["write_briefing"]).safe
    report = assess([ticket], rt.policy, {"post_to_slack": "external"}, ["post_to_slack", "undeclared"])
    assert not report.safe and report.unapproved_writes == ["post_to_slack", "undeclared"]
