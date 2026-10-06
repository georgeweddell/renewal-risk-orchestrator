"""Command line interface: `rro --help`."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from typing import Annotated, Any

import anthropic
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from rro.agent.llm import ClaudeLLM
from rro.agent.local_tools import LocalTools
from rro.agent.orchestrator import AgentEvent
from rro.governance.approvals import Approval, ApprovalError
from rro.governance.gateway import GatewayConfigError
from rro.governance.trifecta import assess
from rro.live_seed import LiveSeedError, seed_github, seed_hubspot, seed_posthog
from rro.risk import score
from rro.runtime import Runtime, SlackConfigError, build_runtime
from rro.seeding import load_seed, seed_memory, seed_mock_systems
from rro.settings import get_settings
from rro.signals import SignalError, collect_signals

app = typer.Typer(help="Renewal Risk Orchestrator: prep SaaS renewals across CRM, support and usage data.", no_args_is_help=True)
console = Console()

BAND_STYLE = {"healthy": "green", "at-risk": "yellow", "critical": "bold red"}
STATUS_STYLE = {"pending": "yellow", "approved": "cyan", "executed": "green", "rejected": "red", "failed": "bold red"}


@app.callback()
def main(
    live: Annotated[bool, typer.Option("--live", help="Use the live systems (HubSpot, GitHub, PostHog) instead of mock data.")] = False,
) -> None:
    if live:
        os.environ["RRO_MODE"] = "live"  # environment variables override .env
        get_settings.cache_clear()


def _band(band: str | None) -> str:
    return f"[{BAND_STYLE.get(band or '', 'white')}]{band or '-'}[/]"


def _status(status: str) -> str:
    return f"[{STATUS_STYLE.get(status, 'white')}]{status}[/]"


def _runtime() -> Runtime:
    return build_runtime(get_settings())


def _starting() -> None:
    console.print("[dim]Starting MCP servers: crm, tickets, usage, memory…[/]")


def _fail(message: str) -> None:
    console.print(f"[bold red]Error:[/] {message}")
    raise typer.Exit(1)


# --- data ----------------------------------------------------------------------
@app.command()
def seed() -> None:
    """Rebuild the mock systems and the decision memory from seed/."""
    settings = get_settings()
    counts = seed_mock_systems(load_seed(settings.seed_file), settings.mock_db)
    decisions = seed_memory(settings.memory_seed_file, settings.memory_db)
    build_runtime(settings)  # make sure the orchestrator's own database exists too
    console.print("[green]Seeded[/] mock systems: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    console.print(f"[green]Seeded[/] decision memory: {decisions} past decisions")


@app.command("seed-live")
def seed_live(
    only: Annotated[list[str] | None, typer.Option(help="Only these systems: hubspot, github, posthog.")] = None,
    force_usage: Annotated[bool, typer.Option(help="Send PostHog events even if demo events already exist.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
) -> None:
    """Load the demo story into the live systems: HubSpot (test account only), GitHub Issues and PostHog."""
    settings = get_settings()
    systems = only or ["hubspot", "github", "posthog"]
    unknown = set(systems) - {"hubspot", "github", "posthog"}
    if unknown:
        _fail(f"Unknown system(s): {', '.join(sorted(unknown))}")
    if not yes:
        console.print("This creates or updates, in your live accounts:")
        if "hubspot" in systems:
            console.print("  • HubSpot: custom properties, 8 companies, 16 deals (developer test accounts only)")
        if "github" in systems:
            console.print(f"  • GitHub: labels and 17 issues in {settings.github_tickets_repo}")
        if "posthog" in systems:
            console.print("  • PostHog: about 40,000 backdated events, 4% of the free monthly allowance (only if the project has none yet)")
        typer.confirm("Continue?", abort=True)
    seed = load_seed(settings.seed_file)
    steps = {"hubspot": seed_hubspot, "github": seed_github, "posthog": seed_posthog}
    for system in ("hubspot", "github", "posthog"):
        if system not in systems:
            continue
        try:
            if system == "posthog":
                seed_posthog(settings, seed, console.print, force=force_usage)
            else:
                steps[system](settings, seed, console.print)
        except LiveSeedError as exc:
            _fail(str(exc))
    console.print("[green]Done.[/] PostHog can take a few minutes to make new events queryable. Then check parity with: [bold]rro --live score[/]")


@app.command()
def reset(yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False) -> None:
    """Delete runs, approvals, the audit log and briefings, then reseed. For resetting a demo."""
    settings = get_settings()
    if not yes:
        typer.confirm("This deletes all runs, approvals, the audit log and generated briefings. Continue?", abort=True)
    settings.rro_db.unlink(missing_ok=True)
    shutil.rmtree(settings.output_dir / "briefings", ignore_errors=True)
    seed()


@app.command("demo-reset")
def demo_reset(yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False) -> None:
    """Clean slate for a demo (Slack cards, local data, and with --live the HubSpot fields), then a preflight check."""
    from rro import demo

    settings = get_settings()
    live = settings.rro_mode == "live"
    if not yes:
        console.print("This deletes this app's approval cards in Slack, all local runs, approvals, audit log and briefings"
                      + (", and clears the risk and pricing fields on your HubSpot test account's deals." if live else "."))  # fmt: skip
        typer.confirm("Continue?", abort=True)

    steps = [demo.delete_slack_cards(settings), demo.reset_local(settings)]
    if steps[-1].ok is False:
        _fail(steps[-1].detail)
    if live:
        steps.append(demo.reset_hubspot(settings))
    console.print("[dim]Preflight: starting MCP servers…[/]")
    steps += _run_async(lambda: demo.preflight(settings))

    table = Table("", "Step", "Detail", show_header=False, box=None, padding=(0, 1))
    for step in steps:
        mark = {True: "[green]✓[/]", False: "[red]✗[/]", None: "[yellow]•[/]"}[step.ok]
        table.add_row(mark, step.name, step.detail)
    console.print(table)
    if any(step.ok is False for step in steps):
        _fail("Not ready: fix the ✗ items above.")
    console.print(f"[green]Ready.[/] Next: [bold]rro {'--live ' if live else ''}serve[/], then open http://127.0.0.1:8000")


@app.command()
def accounts() -> None:
    """List the seeded demo accounts."""
    data = load_seed(get_settings().seed_file)
    owners = {o.id: f"{o.first_name} {o.last_name}" for o in data.owners}
    table = Table("Account", "Slug", "ARR", "Renewal in", "Owner", "Designed as")
    for a in data.accounts:
        table.add_row(a.name, a.slug, f"${a.arr:,}", f"{a.renewal_in_days} days", owners[a.owner], _band(a.expected_band))
    console.print(table)


@app.command()
def memory(slug: Annotated[str | None, typer.Argument(help="Only this account.")] = None) -> None:
    """Show the decision memory: past proposals, human decisions and outcomes."""
    rt = _runtime()
    decisions = rt.memory.for_account(slug) if slug else rt.memory.all()
    table = Table("ID", "Date", "Account", "Action", "Risk", "Proposal", "Decision", "Outcome")
    for d in decisions:
        decision = f"{_status(d.status)} [dim]{d.approver}[/]"
        if d.note:
            decision += f"\n[dim]{d.note}[/]"
        outcome = d.outcome or "-"
        if d.outcome_note:
            outcome += f"\n[dim]{d.outcome_note}[/]"
        table.add_row(str(d.id), d.decided_on, d.account_name, d.action_type, f"{_band(d.risk_band)} {d.risk_score}", d.proposal, decision, outcome)
    console.print(table)


@app.command("rate-evidence")
def rate_evidence(
    decision_id: Annotated[int, typer.Argument(help="The purchase's ID in `rro memory`.")],
    outcome: Annotated[str, typer.Argument(help="useful or not_useful")],
    note: Annotated[str, typer.Option(help="Why. Required: future runs read it before buying.")],
    by: Annotated[str | None, typer.Option(help="Who is rating. Defaults to RRO_APPROVER_NAME.")] = None,
) -> None:
    """Record whether a paid evidence purchase helped, so later runs can decide whether to buy again."""
    rt = _runtime()
    try:
        rt.approval_service().rate_evidence(decision_id, outcome, by=by or rt.settings.rro_approver_name, note=note)
    except ApprovalError as exc:
        _fail(str(exc))
    console.print(f"[green]Recorded[/] purchase {decision_id} as {outcome}.")


# --- governance ------------------------------------------------------------------
@app.command()
def tools() -> None:
    """Show every tool the MCP servers offer and what the policy lets the agent do with it."""
    rt = _runtime()

    async def main() -> None:
        _starting()
        async with rt.gateway as gateway:
            table = Table("Tool", "Policy scope", "Shown to agent", "Server says")
            for spec in gateway.inventory():
                hint = {True: "read-only", False: "writes", None: "-"}[spec.read_only_hint]
                if spec.hint_conflict:
                    hint += " [red](disagrees with policy)[/]"
                table.add_row(
                    spec.qualified_name,
                    {"read": "[green]read[/]", "write": "[yellow]write (approval)[/]"}.get(spec.scope, "[red]unlisted[/]"),
                    "yes" if spec.exposed_to_model else "[dim]no[/]",
                    hint,
                )
            console.print(table)
            console.print("[dim]Scopes come from config/policy.yaml. Anything not listed there is hidden and denied.[/]")
            report = assess(gateway.inventory(), gateway.policy, LocalTools.effects, LocalTools.names)
            console.print(f"[{'green' if report.safe else 'bold red'}]{report.explain()}[/]")

    _run_async(main)


@app.command()
def audit(
    run_id: Annotated[str | None, typer.Argument(help="Run ID. Defaults to the latest run.")] = None,
    full: Annotated[bool, typer.Option(help="Show full arguments.")] = False,
) -> None:
    """Show the audit log for a run: every tool call and human decision, allowed or denied."""
    store = _runtime().store
    run = store.get_run(run_id) if run_id else store.latest_run()
    if run is None:
        _fail("No such run." if run_id else "No runs yet. Try `rro run \"Prep the renewal for Halcyon Robotics\"`.")
    rows = store.audit_for_run(run["id"])
    console.print(f"Run [bold]{run['id']}[/] · {run['instruction']!r} · {run['status']} · {len(rows)} entries")
    table = Table("#", "Time (UTC)", "Actor", "Tool", "Scope", "Decision", "Arguments", "ms")
    for row in rows:
        args = row["args_json"] if full else _truncate(row["args_json"], 60)
        decision = "[green]allowed[/]" if row["decision"] == "allowed" else f"[red]denied[/] [dim]{row['reason']}[/]"
        if row["is_error"] and row["decision"] == "allowed":
            decision += " [yellow](error)[/]"
        if row["approval_id"]:
            decision += f" [dim]{row['approval_id']}[/]"
        table.add_row(str(row["id"]), row["ts"][11:19], row["actor"], f"{row['system']}.{row['tool']}", row["scope"], decision, args, str(row["latency_ms"] or ""))
    console.print(table)


@app.command()
def approvals(
    all_: Annotated[bool, typer.Option("--all", help="Include decided approvals, not just pending ones.")] = False,
) -> None:
    """List proposed actions waiting for a human (or, with --all, every decision)."""
    items = _runtime().approvals.list(status=None if all_ else "pending")
    if not items:
        console.print("Nothing waiting for approval." if not all_ else "No approvals yet.")
        return
    table = Table("ID", "Account", "Action", "Proposal", "Status", "Decided by")
    for a in items:
        table.add_row(a.id, a.account_name, a.action_type, a.summary, _status(a.status), a.decided_by or "")
    console.print(table)
    if not all_:
        console.print("[dim]rro approve <ID>   ·   rro reject <ID> --note \"why\"[/]")


@app.command()
def approve(
    approval_id: str,
    by: Annotated[str | None, typer.Option(help="Who is approving. Defaults to RRO_APPROVER_NAME.")] = None,
    note: Annotated[str | None, typer.Option(help="Optional note, kept in memory.")] = None,
) -> None:
    """Approve a proposed action. The executor then makes exactly that write, through the gateway."""
    rt = _runtime()
    approver = by or rt.settings.rro_approver_name

    async def main() -> Approval:
        _starting()
        async with rt.gateway:
            approval = await rt.approval_service().approve(approval_id, by=approver, note=note)
            await rt.reflect_decision(approval_id)
            return approval

    result = _run_async(main)
    _print_decision(result)


@app.command()
def reject(
    approval_id: str,
    note: Annotated[str, typer.Option(help="Why. Required: future runs learn from it.")],
    by: Annotated[str | None, typer.Option(help="Who is rejecting. Defaults to RRO_APPROVER_NAME.")] = None,
) -> None:
    """Reject a proposed action. Nothing is written; the reason goes into memory."""
    rt = _runtime()
    try:
        result = rt.approval_service().reject(approval_id, by=by or rt.settings.rro_approver_name, note=note)
    except ApprovalError as exc:
        _fail(str(exc))
    if rt.notifier:
        asyncio.run(rt.reflect_decision(approval_id))
    _print_decision(result)


def _print_decision(a: Approval) -> None:
    console.print(f"{a.id}: {_status(a.status)} by {a.decided_by}. {a.summary}")
    if a.status == "executed":
        console.print("[green]Written to the CRM by the executor.[/] Recorded in memory. See `rro audit " + (a.run_id or "") + "`.")
    elif a.status == "failed":
        console.print(f"[red]The write failed:[/] {a.result_text}")
    elif a.status == "rejected":
        console.print("Nothing was written. The rejection and its reason are now in memory for future runs.")


# --- risk ------------------------------------------------------------------------
@app.command("score")
def score_accounts(
    slug: Annotated[str | None, typer.Argument(help="Account slug. Defaults to every seeded account.")] = None,
) -> None:
    """Score accounts deterministically (no LLM): the ground truth the agent should match."""
    rt = _runtime()
    seeded = {a.slug: a for a in load_seed(rt.settings.seed_file).accounts}
    slugs = [slug] if slug else list(seeded)
    console.print(f"[dim]Backends: " + ", ".join(f"{s}={rt.settings.backend_for(s)}" for s in ("crm", "tickets", "usage")) + "[/]")

    async def main() -> None:
        _starting()
        async with rt.gateway as gateway:
            table = Table("Account", "Usage", "Open P1", "Open P2", "Renewal in", "Score", "Band", "Designed as")
            for s in slugs:
                try:
                    acct = await collect_signals(gateway, s)
                except SignalError as exc:
                    _fail(str(exc))
                result = score(acct.signals, rt.risk_config)
                expected = seeded[s].expected_band if s in seeded else None
                check = "" if expected is None else (" [green]✓[/]" if expected == result.band else " [red]✗[/]")
                sig = acct.signals
                table.add_row(acct.name, f"{sig.usage_pct_change:+.1f}%", str(sig.open_p1), str(sig.open_p2), f"{sig.days_to_renewal} days", str(result.score), _band(result.band), _band(expected) + check)
            console.print(table)

    _run_async(main)


# --- the agent ---------------------------------------------------------------------
@app.command()
def run(
    instruction: Annotated[str, typer.Argument(help='e.g. "Prep the renewal for Halcyon Robotics"')],
    show: Annotated[bool, typer.Option(help="Print the briefing when the run finishes.")] = True,
) -> None:
    """Run the agent on an instruction and write a renewal briefing."""
    rt = _runtime()
    settings = rt.settings
    backends = ", ".join(f"{s}={settings.backend_for(s)}" for s in ("crm", "tickets", "usage"))
    console.print(f"[dim]{settings.anthropic_model} · effort {settings.anthropic_effort} · {backends}[/]")

    async def main():
        _starting()
        async with rt.gateway:
            result = await rt.orchestrator(ClaudeLLM(settings)).run(instruction, on_event=_print_event)
            posted = await rt.announce(result.run_id) if result.approval_ids else []
            return result, posted

    result, posted = _run_async(main)
    if result.status == "failed":
        _fail(f"Run {result.run_id} failed: {result.error}")

    a = result.assessment
    if show and result.briefing_path:
        console.print(Panel(Markdown(result.briefing_path.read_text(encoding="utf-8")), title="Briefing", border_style="dim"))
    tokens = result.usage
    cached = tokens.get("cache_read_input_tokens", 0)
    total_in = tokens.get("input_tokens", 0) + cached + tokens.get("cache_creation_input_tokens", 0)
    # The agent's closing summary was already printed in the trace, so it isn't repeated here.
    console.print(
        Panel.fit(
            f"Risk: {_band(a.band if a else None)} ({a.score if a else '-'}/100)\n"
            f"Briefing: {result.briefing_path.relative_to(settings.rro_home)}\n"
            f"Audit: rro audit {result.run_id}\n"
            f"[dim]{result.turns} turns · {total_in:,} input tokens ({cached / max(total_in, 1):.0%} from cache) · "
            f"{tokens.get('output_tokens', 0):,} output tokens[/]",
            title=f"Run {result.run_id} {'awaiting approval' if result.approval_ids else 'complete'}",
            border_style="yellow" if result.approval_ids else "green",
        )
    )
    if result.approval_ids:
        table = Table("Approval", "Proposal", title="Waiting for a human", title_justify="left")
        for approval_id in result.approval_ids:
            table.add_row(approval_id, rt.approvals.get(approval_id).summary)
        console.print(table)
        if posted:
            console.print(f"[green]Posted {len(posted)} approval request(s) to Slack.[/] Decisions there need a listener running: rro serve, or rro slack.")
        console.print("[dim]rro approve <ID>   ·   rro reject <ID> --note \"why\"   ·   or use the web UI: rro serve[/]")


@app.command("hubspot-login")
def hubspot_login() -> None:
    """Sign in to HubSpot's own MCP server once (browser), then list the tools it offers."""
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    from rro import hubspot_mcp

    settings = get_settings()

    async def main():
        http = hubspot_mcp.http_client(settings, interactive=True)
        async with http, Client(streamable_http_client(hubspot_mcp.SERVER_URL, http_client=http)) as client:
            tools = (await client.list_tools()).tools
            who = None
            if any(t.name == "get_user_details" for t in tools):
                result = await client.call_tool("get_user_details", {})
                who = "\n".join(getattr(c, "text", "") for c in result.content)[:600]
            return tools, who

    try:
        tools, who = asyncio.run(main())
    except hubspot_mcp.HubSpotLoginRequired as exc:
        _fail(str(exc))
    path = hubspot_mcp.save_tool_inventory(settings, tools)
    console.print(f"[green]Signed in.[/] Token saved to {hubspot_mcp.token_path(settings).relative_to(settings.rro_home)} (gitignored).")
    if who:
        console.print(Panel(who, title="Connected as", border_style="dim"))
    table = Table("HubSpot MCP tool", "Server says", "Description")
    for t in tools:
        hint = {True: "read-only", False: "writes", None: "-"}[t.annotations.read_only_hint if t.annotations else None]
        table.add_row(t.name, hint, _truncate((t.description or "").split("\n")[0], 90))
    console.print(table)
    console.print(f"[dim]Full tool schemas saved to {path.relative_to(settings.rro_home)}[/]")


@app.command("eval")
def eval_agent(
    account: Annotated[list[str] | None, typer.Option("--account", "-a", help="Only these account slugs.")] = None,
    concurrency: Annotated[int, typer.Option(help="Agent runs in parallel.")] = 4,
    payments: Annotated[bool, typer.Option("--payments", help="Payments eval: payments on, an injected ticket asks the agent to pay an attacker (Halcyon by default).")] = False,
) -> None:
    """Run the agent on every seeded account (mock data, isolated) and grade it against ground truth.

    Uses the Claude API: roughly $0.10-0.15 per account.
    """
    from rro.evals import CHECKS, run_eval, save_report

    settings = get_settings()
    console.print(f"[dim]{settings.anthropic_model} · effort {settings.anthropic_effort} · mock data in an isolated copy[/]")
    _starting()
    if payments:
        _payments_eval(settings, account)
        return

    def progress(r) -> None:
        marks = " ".join(("[green]✓[/]" if c.passed else "[red]✗[/]") for c in r.checks)
        console.print(f"  {r.name:<18} {_band(r.band)} {r.score if r.score is not None else '-':>3} {marks}  [dim]{r.seconds}s[/]")

    results, root = _run_async(lambda: run_eval(settings, account, concurrency, on_result=progress))
    report = save_report(results, settings.anthropic_model, settings.anthropic_effort, settings.rro_home / "evals")
    passed = sum(r.passed for r in results)
    console.print(f"\n[bold]{passed}/{len(results)} accounts passed every check.[/] [dim]Checks: {', '.join(CHECKS)}[/]")
    for r in results:
        for c in r.checks:
            if not c.passed:
                console.print(f"  [red]✗[/] {r.name} · {c.name}: {c.detail}")
    console.print(f"Report: {report.relative_to(settings.rro_home)} · run data: {root.relative_to(settings.rro_home)}")


def _payments_eval(settings, account: list[str] | None) -> None:
    from rro.evals import payments_report_markdown, run_payments_eval

    results, root = _run_async(lambda: run_payments_eval(settings, account))
    for r in results:
        console.print(f"[bold]{r.name}[/] [dim]{r.seconds}s, {r.turns} turns[/]")
        for c in r.checks:
            console.print(f"  {'[green]✓[/]' if c.passed else '[red]✗[/]'} {c.name}: {c.detail}")
    report = settings.rro_home / "evals" / "payments.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(payments_report_markdown(results, settings.anthropic_model, settings.anthropic_effort), encoding="utf-8")
    console.print(f"\n[bold]{sum(r.passed for r in results)}/{len(results)} passed every check.[/] "
                  f"Report: {report.relative_to(settings.rro_home)} · run data: {root.relative_to(settings.rro_home)}")  # fmt: skip


@app.command()
def slack() -> None:
    """Listen for Approve/Reject clicks in Slack (Socket Mode) without the web UI. Ctrl+C to stop."""
    rt = _runtime()
    try:
        listener = rt.slack_listener()
    except SlackConfigError as exc:
        _fail(str(exc))
    if listener is None:
        _fail("Slack isn't configured: set SLACK_BOT_TOKEN, SLACK_APP_TOKEN and SLACK_APPROVALS_CHANNEL in .env.")

    async def main() -> None:
        _starting()
        async with rt.gateway:
            await listener.start()
            pending = [a for a in rt.approvals.list(status="pending") if a.slack_ts]
            console.print(f"[green]Listening to Slack[/] ({len(pending)} approval card(s) waiting). Ctrl+C to stop.")
            try:
                await asyncio.Event().wait()
            finally:
                await listener.stop()

    try:
        _run_async(main)
    except KeyboardInterrupt:
        console.print("Stopped.")


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Interface to bind. Keep it local: the UI has no login.")] = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Start the web UI: run the agent, review briefings, approve or reject proposals."""
    import uvicorn

    try:
        _runtime().slack_listener()  # refuse to start a listener nobody may use (see SLACK_APPROVERS)
    except SlackConfigError as exc:
        _fail(str(exc))
    console.print(f"Renewal Risk Orchestrator ({get_settings().rro_mode} mode) on [bold]http://{host}:{port}[/]")
    uvicorn.run("rro.web.app:app", host=host, port=port, log_level="warning")


def _print_event(event: AgentEvent) -> None:
    d = event.data
    if event.kind == "progress":
        console.print(f"  [dim italic]{d['text'].strip()}[/]")
    elif event.kind == "text":
        console.print(f"  {d['text'].strip()}")
    elif event.kind == "tool_call":
        name = d["name"].replace("__", ".")
        console.print(f"[cyan]→ {name}[/] [dim]{_truncate(json.dumps(d['input']), 90)}[/]")
    elif event.kind == "tool_result":
        name = d["name"].replace("__", ".")
        if d["denied"]:
            console.print(f"[red]  ✗ {name}: {d['text']}[/]")
        elif d["is_error"]:
            console.print(f"[yellow]  ! {name}: {_truncate(d['text'], 120)}[/]")
        else:
            console.print(f"[green]  ✓ {name}[/] [dim]{len(d['text']):,} chars[/]")
    elif event.kind == "fallback":
        console.print(f"[yellow]  ↪ {d['from']} declined; continued on {d['to']}[/]")
    elif event.kind == "nudge":
        console.print(f"[yellow]  ↻ {d['message']}[/]")


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _run_async(main: Any) -> Any:
    try:
        return asyncio.run(main())
    except Exception as exc:
        # Errors raised inside the MCP clients' task groups arrive wrapped in exception groups.
        while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
            exc = exc.exceptions[0]
        if isinstance(exc, GatewayConfigError | ApprovalError):
            _fail(str(exc))
        if type(exc).__name__ == "HubSpotLoginRequired":
            _fail(str(exc))
        if isinstance(exc, anthropic.AuthenticationError):
            _fail("Claude rejected the API key. Check ANTHROPIC_API_KEY in .env.")
        if isinstance(exc, TypeError) and "authentication" in str(exc).lower():
            # The SDK raises this when it finds no credentials at all.
            _fail("No Claude credentials found. Set ANTHROPIC_API_KEY in .env (see .env.example).")
        if isinstance(exc, anthropic.APIConnectionError):
            _fail("Couldn't reach the Claude API. Check your network connection.")
        raise exc


if __name__ == "__main__":
    app()
