"""Command line interface: `rro --help`."""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Annotated, Any

import anthropic
import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from rro.agent.llm import ClaudeLLM
from rro.agent.orchestrator import AgentEvent, Orchestrator
from rro.db import Store
from rro.governance.gateway import GatewayConfigError, ToolGateway
from rro.governance.policy import Policy
from rro.risk import RiskConfig, score
from rro.seeding import load_seed, seed_mock_systems
from rro.settings import Settings, get_settings
from rro.signals import SignalError, collect_signals

app = typer.Typer(help="Renewal Risk Orchestrator: prep SaaS renewals across CRM, support and usage data.", no_args_is_help=True)
console = Console()

BAND_STYLE = {"healthy": "green", "at-risk": "yellow", "critical": "bold red"}


def _band(band: str | None) -> str:
    return f"[{BAND_STYLE.get(band or '', 'white')}]{band or '-'}[/]"


def _gateway(settings: Settings, store: Store) -> ToolGateway:
    console.print("[dim]Starting MCP servers: crm, tickets, usage…[/]")
    return ToolGateway(settings, Policy.load(settings.config_dir / "policy.yaml"), store)


def _fail(message: str) -> None:
    console.print(f"[bold red]Error:[/] {message}")
    raise typer.Exit(1)


# --- data ----------------------------------------------------------------------
@app.command()
def seed() -> None:
    """Rebuild the mock systems (CRM, tickets, usage) from seed/accounts.yaml."""
    settings = get_settings()
    counts = seed_mock_systems(load_seed(settings.seed_file), settings.mock_db)
    Store.open(settings.rro_db)  # make sure the orchestrator's own database exists too
    console.print(f"[green]Seeded[/] {settings.mock_db.relative_to(settings.rro_home)}: " + ", ".join(f"{v} {k}" for k, v in counts.items()))


@app.command()
def reset(yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False) -> None:
    """Delete run history, the audit log and generated briefings, then reseed. For resetting a demo."""
    settings = get_settings()
    if not yes:
        typer.confirm("This deletes all runs, the audit log and generated briefings. Continue?", abort=True)
    settings.rro_db.unlink(missing_ok=True)
    shutil.rmtree(settings.output_dir / "briefings", ignore_errors=True)
    seed()


@app.command()
def accounts() -> None:
    """List the seeded demo accounts."""
    data = load_seed(get_settings().seed_file)
    owners = {o.id: f"{o.first_name} {o.last_name}" for o in data.owners}
    table = Table("Account", "Slug", "ARR", "Renewal in", "Owner", "Designed as")
    for a in data.accounts:
        table.add_row(a.name, a.slug, f"${a.arr:,}", f"{a.renewal_in_days} days", owners[a.owner], _band(a.expected_band))
    console.print(table)


# --- governance ------------------------------------------------------------------
@app.command()
def tools() -> None:
    """Show every tool the MCP servers offer and what the policy lets the agent do with it."""
    settings = get_settings()

    async def main() -> None:
        async with _gateway(settings, Store.open(settings.rro_db)) as gateway:
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

    _run_async(main)


@app.command()
def audit(
    run_id: Annotated[str | None, typer.Argument(help="Run ID. Defaults to the latest run.")] = None,
    full: Annotated[bool, typer.Option(help="Show full arguments.")] = False,
) -> None:
    """Show the audit log for a run: every tool call, allowed or denied."""
    store = Store.open(get_settings().rro_db)
    run = store.get_run(run_id) if run_id else store.latest_run()
    if run is None:
        _fail("No such run." if run_id else "No runs yet. Try `rro run \"Prep the renewal for Halcyon Robotics\"`.")
    rows = store.audit_for_run(run["id"])
    console.print(f"Run [bold]{run['id']}[/] · {run['instruction']!r} · {run['status']} · {len(rows)} tool calls")
    table = Table("#", "Time (UTC)", "Actor", "Tool", "Scope", "Decision", "Arguments", "ms")
    for row in rows:
        args = row["args_json"] if full else _truncate(row["args_json"], 60)
        decision = "[green]allowed[/]" if row["decision"] == "allowed" else f"[red]denied[/] [dim]{row['reason']}[/]"
        if row["is_error"] and row["decision"] == "allowed":
            decision += " [yellow](error)[/]"
        table.add_row(str(row["id"]), row["ts"][11:19], row["actor"], f"{row['system']}.{row['tool']}", row["scope"], decision, args, str(row["latency_ms"] or ""))
    console.print(table)


# --- risk ------------------------------------------------------------------------
@app.command("score")
def score_accounts(
    slug: Annotated[str | None, typer.Argument(help="Account slug. Defaults to every seeded account.")] = None,
) -> None:
    """Score accounts deterministically (no LLM): the ground truth the agent should match."""
    settings = get_settings()
    config = RiskConfig.load(settings.config_dir / "risk.yaml")
    seeded = {a.slug: a for a in load_seed(settings.seed_file).accounts}
    slugs = [slug] if slug else list(seeded)

    async def main() -> None:
        async with _gateway(settings, Store.open(settings.rro_db)) as gateway:
            table = Table("Account", "Usage", "Open P1", "Open P2", "Renewal in", "Score", "Band", "Designed as")
            for s in slugs:
                try:
                    acct = await collect_signals(gateway, s)
                except SignalError as exc:
                    _fail(str(exc))
                result = score(acct.signals, config)
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
    settings = get_settings()
    store = Store.open(settings.rro_db)
    risk_config = RiskConfig.load(settings.config_dir / "risk.yaml")
    backends = ", ".join(f"{s}={settings.backend_for(s)}" for s in ("crm", "tickets", "usage"))
    console.print(f"[dim]{settings.anthropic_model} · effort {settings.anthropic_effort} · {backends}[/]")

    async def main():
        async with _gateway(settings, store) as gateway:
            orchestrator = Orchestrator(settings, gateway, ClaudeLLM(settings), store, risk_config)
            return await orchestrator.run(instruction, on_event=_print_event)

    result = _run_async(main)
    if result.status != "completed":
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
            title=f"Run {result.run_id} complete",
            border_style="green",
        )
    )


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
        if isinstance(exc, GatewayConfigError):
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
