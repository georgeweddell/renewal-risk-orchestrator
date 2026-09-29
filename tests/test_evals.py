"""The eval's grading, using a scripted stand-in for Claude (no API calls)."""

from rro.evals import CHECKS, REQUIRED_SECTIONS, evaluate_account, report_markdown
from rro.seeding import load_seed
from rro.signals import collect_signals
from test_orchestrator import HALCYON, RENEWAL_DEAL, ScriptedLLM, response, score_call, text, tool_use

GOOD_BRIEFING = "# Renewal briefing: Halcyon Robotics\n\n" + "\n\n".join(
    f"{section}\nSee #112 and #113." for section in REQUIRED_SECTIONS
)
SHORT_BRIEFING = "# Renewal briefing\n\n## Summary\nShort."


def halcyon(settings):
    return next(a for a in load_seed(settings.seed_file).accounts if a.slug == HALCYON)


async def grade(rt, script):
    """`script(truth)` returns the scripted responses, so a faithful run can use the true numbers."""
    async with rt.gateway as gateway:
        truth = await collect_signals(gateway, HALCYON)
        return await evaluate_account(rt, ScriptedLLM(*script(truth)), halcyon(rt.settings), truth)


def score_with(usage_pct_change, id="t1"):
    call = score_call(id)
    call.input["usage_pct_change"] = usage_pct_change
    return call


async def test_a_faithful_run_passes_every_check(rt):
    def script(truth):
        return [
            response(score_with(truth.signals.usage_pct_change)),
            response(tool_use("t2", "propose_crm_update", account_slug=HALCYON, deal_id=RENEWAL_DEAL, reason="WAU down, 2 P1s")),
            response(tool_use("t3", "write_briefing", account_slug=HALCYON, markdown=GOOD_BRIEFING)),
            response(text("Done."), stop_reason="end_turn"),
        ]

    result = await grade(rt, script)
    assert [c.name for c in result.checks] == CHECKS
    assert result.passed, [c for c in result.checks if not c.passed]


async def test_the_eval_catches_a_mistranscribed_number_and_a_discount(rt):
    def script(truth):
        return [
            response(score_with(truth.signals.usage_pct_change - 9)),  # a transcription slip
            response(tool_use("t2", "propose_pricing_exception", account_slug=HALCYON, deal_id=RENEWAL_DEAL,
                              discount_pct=15, rationale="Save the deal", conditions="none")),  # fmt: skip
            response(tool_use("t3", "write_briefing", account_slug=HALCYON, markdown=SHORT_BRIEFING)),
            response(text("Done."), stop_reason="end_turn"),
        ]

    result = await grade(rt, script)
    failed = {c.name for c in result.checks if not c.passed}
    assert failed == {"inputs", "briefing_sections", "cites_p1_issues", "crm_proposal", "discount_discipline"}
    assert "| Halcyon Robotics |" in report_markdown([result], "scripted", "medium")
