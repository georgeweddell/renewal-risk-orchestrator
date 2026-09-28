"""A deliberately simple web UI: start runs, watch the trace, read briefings, approve or reject.

All handlers are async so they run on the event loop, alongside the MCP clients and the
SQLite connection they share.

Server-rendered Jinja templates. HTMX is used only to poll a running agent's
panel. The live trace *is* the audit log, so what you watch is exactly what
gets recorded. The UI has no login, so `rro serve` binds to localhost only.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from rro.agent.llm import ClaudeLLM
from rro.governance.approvals import ApprovalError
from rro.runtime import Runtime, build_runtime
from rro.seeding import load_seed
from rro.settings import get_settings

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
# Briefings are model output: raw HTML in them is escaped, never rendered.
MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("table")
APPROVER_COOKIE = "rro_approver"


@asynccontextmanager
async def lifespan(app: FastAPI):
    rt = build_runtime(get_settings())
    app.state.rt = rt
    app.state.tasks = set()
    async with rt.gateway:
        yield
        for task in app.state.tasks:  # stop any runs still going before the MCP servers shut down
            task.cancel()
        await asyncio.gather(*app.state.tasks, return_exceptions=True)


app = FastAPI(title="Renewal Risk Orchestrator", lifespan=lifespan)
TEMPLATES.env.filters["tojson_pretty"] = lambda value: json.dumps(value, indent=2, sort_keys=True)


def _rt(request: Request) -> Runtime:
    return request.app.state.rt


def _render(request: Request, template: str, **context) -> HTMLResponse:
    rt = _rt(request)
    context |= {
        "pending_count": len(rt.approvals.list(status="pending")),
        "approver": request.cookies.get(APPROVER_COOKIE) or rt.settings.rro_approver_name,
        "model": rt.settings.anthropic_model,
    }
    return TEMPLATES.TemplateResponse(request, template, context)


# --- pages ----------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    rt = _rt(request)
    seed = load_seed(rt.settings.seed_file)
    pending = rt.approvals.list(status="pending")
    accounts = [
        {
            "account": a,
            "latest": rt.store.latest_run(a.slug),
            "pending": sum(p.account_slug == a.slug for p in pending),
        }
        for a in seed.accounts
    ]
    return _render(request, "index.html", accounts=accounts, runs=rt.store.recent_runs(8))


@app.post("/runs")
async def start_run(request: Request, instruction: str = Form(...)):
    rt = _rt(request)
    instruction = instruction.strip()
    if not instruction:
        raise HTTPException(400, "Give the agent an instruction.")
    llm = ClaudeLLM(rt.settings)
    run_id = rt.store.create_run(instruction, llm.model)
    task = asyncio.create_task(_run_in_background(rt, llm, instruction, run_id))
    request.app.state.tasks.add(task)
    task.add_done_callback(request.app.state.tasks.discard)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


async def _run_in_background(rt: Runtime, llm: ClaudeLLM, instruction: str, run_id: str) -> None:
    try:
        await rt.orchestrator(llm).run(instruction, run_id=run_id)
    except Exception:
        pass  # the orchestrator has already recorded the failure on the run, which the run page shows


@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_page(request: Request, run_id: str):
    return _render(request, "run.html", **_run_context(request, run_id))


@app.get("/runs/{run_id}/panel", response_class=HTMLResponse)
async def run_panel(request: Request, run_id: str):
    """Polled by HTMX while the run is in progress."""
    return _render(request, "_run_panel.html", **_run_context(request, run_id))


def _run_context(request: Request, run_id: str) -> dict:
    rt = _rt(request)
    run = rt.store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "No such run")
    briefing_html = None
    if run["briefing_path"] and Path(run["briefing_path"]).exists():
        briefing_html = MARKDOWN.render(Path(run["briefing_path"]).read_text(encoding="utf-8"))
    return {
        "run": run,
        "trace": rt.store.audit_for_run(run_id),
        "approvals": rt.approvals.list(run_id=run_id),
        "briefing_html": briefing_html,
        "usage": json.loads(run["usage_json"]) if run["usage_json"] else None,
    }


@app.get("/approvals", response_class=HTMLResponse)
async def approvals_page(request: Request):
    rt = _rt(request)
    all_approvals = rt.approvals.list()
    return _render(
        request,
        "approvals.html",
        pending=[a for a in all_approvals if a.status == "pending"],
        decided=[a for a in all_approvals if a.status != "pending"],
    )


@app.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request):
    return _render(request, "audit.html", rows=_rt(request).store.recent_audit(300)[::-1])


@app.get("/memory", response_class=HTMLResponse)
async def memory_page(request: Request):
    return _render(request, "memory.html", decisions=_rt(request).memory.all())


# --- decisions --------------------------------------------------------------------
@app.post("/approvals/{approval_id}/decide")
async def decide(
    request: Request,
    approval_id: str,
    action: str = Form(...),
    approver: str = Form(...),
    note: str = Form(""),
    next_url: str = Form("/approvals"),
):
    service = _rt(request).approval_service()
    approver = approver.strip() or _rt(request).settings.rro_approver_name
    try:
        if action == "approve":
            await service.approve(approval_id, by=approver, note=note.strip() or None)
        elif action == "reject":
            service.reject(approval_id, by=approver, note=note)
        else:
            raise HTTPException(400, "Unknown action")
    except ApprovalError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Only redirect within this app.
    response = RedirectResponse(next_url if next_url.startswith("/") and not next_url.startswith("//") else "/approvals", status_code=303)
    response.set_cookie(APPROVER_COOKIE, approver, samesite="strict")
    return response
