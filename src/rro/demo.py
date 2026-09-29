"""Reset everything a demo touches, then check the demo will work.

`rro demo-reset` (add --live to include the live systems):
  1. deletes the approval cards this app posted to Slack (the bot can delete its own messages);
  2. wipes local runs, approvals, audit log and briefings, and reseeds mock data and memory;
  3. with --live: re-runs the HubSpot seed, which clears the risk and pricing fields
     on every renewal deal and refreshes renewal dates;
  4. runs a preflight: Claude key, every MCP server (HubSpot OAuth included when
     configured), the demo account's numbers, live usage freshness and Slack.

GitHub issues don't change during a demo, and PostHog events can't be deleted,
so neither is touched. The preflight says when live usage has gone stale.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import anthropic

from rro.db import Store
from rro.live_seed import LiveSeedError, seed_hubspot
from rro.risk import score
from rro.runtime import build_runtime
from rro.seeding import load_seed, seed_memory, seed_mock_systems, usage_series
from rro.settings import Settings
from rro.signals import SignalError, collect_signals

DEMO_ACCOUNT = "halcyon-robotics"


@dataclass
class Step:
    name: str
    ok: bool | None  # None = skipped, or a warning worth reading
    detail: str = ""


def delete_slack_cards(settings: Settings) -> Step:
    if not settings.slack_enabled or not settings.rro_db.exists():
        return Step("Slack cards", None, "Slack not configured" if not settings.slack_enabled else "nothing posted yet")
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    store = Store.open(settings.rro_db)
    cards = store.conn.execute("SELECT slack_channel, slack_ts FROM approvals WHERE slack_ts IS NOT NULL").fetchall()
    client = WebClient(token=settings.value("SLACK_BOT_TOKEN"))
    deleted = 0
    for card in cards:
        try:
            client.chat_delete(channel=card["slack_channel"], ts=card["slack_ts"])
            deleted += 1
        except SlackApiError as exc:
            if exc.response.get("error") != "message_not_found":  # already gone is fine
                return Step("Slack cards", False, f"couldn't delete a card: {exc.response.get('error')}")
    store.conn.close()
    return Step("Slack cards", True, f"deleted {deleted} approval card(s)")


def reset_local(settings: Settings) -> Step:
    try:
        settings.rro_db.unlink(missing_ok=True)
    except PermissionError:
        return Step("Local data", False, "the database is in use: stop `rro serve` / `rro slack` first, then run this again")
    shutil.rmtree(settings.output_dir / "briefings", ignore_errors=True)
    counts = seed_mock_systems(load_seed(settings.seed_file), settings.mock_db)
    decisions = seed_memory(settings.memory_seed_file, settings.memory_db)
    build_runtime(settings)  # recreate the orchestrator database
    return Step("Local data", True, f"runs, approvals, audit and briefings cleared; {counts['companies']} accounts and {decisions} past decisions reseeded")


def reset_hubspot(settings: Settings) -> Step:
    lines: list[str] = []
    try:
        seed_hubspot(settings, load_seed(settings.seed_file), lines.append)
    except LiveSeedError as exc:
        return Step("HubSpot", False, str(exc))
    return Step("HubSpot", True, "risk and pricing fields cleared, renewal dates refreshed (8 accounts)")


async def preflight(settings: Settings) -> list[Step]:
    steps: list[Step] = []

    # Claude: a free model lookup proves the key works and the model is available.
    try:
        key = settings.value("ANTHROPIC_API_KEY")
        client = anthropic.AsyncAnthropic(api_key=key) if key else anthropic.AsyncAnthropic()
        model = await client.models.retrieve(settings.anthropic_model)
        steps.append(Step("Claude", True, f"{model.id} available"))
    except Exception as exc:
        steps.append(Step("Claude", False, f"{type(exc).__name__}: {str(exc)[:160]}"))

    # Every MCP server, in the configured mode, plus the demo account's real numbers.
    backends = ", ".join(f"{s}={settings.backend_for(s)}" for s in ("crm", "tickets", "usage"))
    rt = build_runtime(settings)
    try:
        async with rt.gateway as gateway:
            shown = sum(spec.exposed_to_model for spec in gateway.inventory())
            steps.append(Step("MCP servers", True, f"{backends}; {len(gateway.inventory())} tools offered, {shown} shown to the agent"))
            truth = await collect_signals(gateway, DEMO_ACCOUNT)
            result = score(truth.signals, rt.risk_config)
            sig = truth.signals
            steps.append(Step(
                "Demo account", result.band == "critical",
                f"{truth.name}: {result.band} {result.score}/100 · usage {sig.usage_pct_change:+.1f}% · "
                f"{sig.open_p1} open P1s (#{', #'.join(map(str, truth.open_p1_issues))}) · renews in {sig.days_to_renewal} days",
            ))  # fmt: skip
            if settings.backend_for("usage") != "mock":
                steps.append(await _usage_freshness(settings, truth.slug))
    except SignalError as exc:
        steps.append(Step("Demo account", False, str(exc)))
    except Exception as exc:
        while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
            exc = exc.exceptions[0]
        steps.append(Step("MCP servers", False, f"{type(exc).__name__}: {str(exc)[:200]}"))

    steps.append(_slack_check(settings))
    return steps


async def _usage_freshness(settings: Settings, slug: str) -> Step:
    """Live usage matches mock only if it was seeded for the current week."""
    from mcp_servers.usage.backends import PostHogUsageBackend

    today = datetime.now(UTC).date()
    backend = PostHogUsageBackend(settings.posthog_host, settings.value("POSTHOG_PROJECT_ID"), settings.value("POSTHOG_PERSONAL_API_KEY"))
    live = backend.weekly_active_users(slug, 12, today=today)
    acct = next(a for a in load_seed(settings.seed_file).accounts if a.slug == slug)
    expected = usage_series(acct, today)
    next_monday = today + timedelta(days=7 - today.weekday())
    if live and [w["active_users"] for w in live] == [w.active_users for w in expected]:
        return Step("Live usage", True, f"matches the seeded story until {next_monday:%a %d %b}")
    return Step(
        "Live usage", None,
        "PostHog data was seeded for a different week, so live trends won't match mock mode. "
        "Record in mock mode, or point .env at a new PostHog project and run `rro seed-live --only posthog`.",
    )  # fmt: skip


def _slack_check(settings: Settings) -> Step:
    if not settings.slack_enabled:
        return Step("Slack", None, "not configured; approvals happen in the web UI")
    from slack_sdk import WebClient

    auth = WebClient(token=settings.value("SLACK_BOT_TOKEN")).auth_test()
    listener = "listener will run inside `rro serve`" if settings.slack_app_token else "SLACK_APP_TOKEN missing: clicks won't be received"
    return Step("Slack", bool(settings.slack_app_token), f"bot {auth['user']} in {auth['team']}; {listener}")
