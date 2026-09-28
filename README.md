# Renewal Risk Orchestrator

**One instruction ("prep the renewal for Halcyon Robotics") and an agent works across the CRM, the support queue and product analytics to score renewal risk and brief the account team. Every tool call is permission-scoped and audited, and nothing is written to a system of record without a human approving it.**

Built with Claude (Anthropic API), the Model Context Protocol (MCP), FastAPI and SQLite.

---

## Why this exists

A renewal is a good test of whether an agent can do real cross-system work. The data is spread across the CRM, the support queue and product analytics, and the outcome is a change to a system of record that someone has to sign off. Connecting an LLM to a few APIs is the easy part. What makes an agent trustworthy enough to act is three layers that most agent tooling handles poorly, and this project is built around them:

| Layer | What that means here |
|---|---|
| **Governance** | Per-system read/write scopes in a policy file, enforced in code on every call. The model is never even shown a write tool: it can only *propose* one. A human approves the exact payload, and only then does a deterministic executor make the write. Every call and decision goes into an append-only audit log. |
| **Persistent memory** | A store of past renewal decisions: what was proposed, who approved or rejected it and why, and what happened next. The agent consults it before recommending anything, and every new human decision is added automatically. |
| **Depth over breadth** | Four systems with real two-way actions, rather than a long list of shallow integrations. |

## What it does

| Step | Status |
|---|---|
| 1. Pull the account, renewal deal and owner from the CRM (HubSpot) | ✅ mock · ✅ live |
| 2. Pull open support issues (GitHub Issues as a stand-in ticketing system) | ✅ mock · ✅ live |
| 3. Pull the product usage trend (PostHog) | ✅ mock · ✅ live |
| 4. Score renewal risk with explainable reasoning, and check precedent in memory | ✅ |
| 5. Write an account team briefing (markdown) | ✅ |
| 6. Propose a CRM risk update and a pricing exception, routed for human approval | ✅ web UI + CLI · Slack in Phase 4 |
| 7. Write to the CRM only after approval | ✅ mock CRM · HubSpot in Phase 4 |

## Architecture

```
  "Prep the renewal for Halcyon Robotics"   (rro run ...)
                │
┌───────────────▼──────────────────────────────────────────────────────┐
│ ORCHESTRATOR: Claude tool-use loop (src/rro/agent)                   │
│  sees read tools + local tools: score_renewal_risk,                  │
│  propose_crm_update, propose_pricing_exception, write_briefing       │
└───────┬──────────────────────────────────────────┬───────────────────┘
        │ tool calls                               │ propose_* → pending
┌───────▼──────────────────────────────────────────▼───────────────────┐
│ GOVERNANCE (src/rro/governance)                                      │
│  Tool Gateway = MCP client host                                      │
│   ① policy.yaml: system × tool × scope, deny by default              │
│   ② write tools never shown to the model                             │
│   ③ every call → append-only audit_log (SQLite triggers block edits) │
│  Approval gate: pending action + SHA-256 of its exact payload        │
│   human approves (web / CLI; Slack in Phase 4) → EXECUTOR (no LLM)   │
│   → write via gateway, allowed only for that approved hash, once     │
│   → decision recorded in memory                                      │
└──┬─────────────┬─────────────┬─────────────┬─────────────────────────┘
   │ MCP over stdio (four server processes, started in parallel)
┌──▼─────────┐ ┌─▼─────────┐ ┌─▼──────────┐ ┌▼─────────────┐ ┌──────────────┐
│ crm        │ │ tickets   │ │ usage      │ │ memory       │ │ risk engine  │
│ HubSpot    │ │ GitHub    │ │ PostHog    │ │ past         │ │ deterministic│
│ read+write*│ │ read      │ │ read       │ │ decisions    │ │ risk.yaml    │
└────────────┘ └───────────┘ └────────────┘ └──────────────┘ └──────────────┘
  * write only by the executor, for a human-approved payload
```

**Claude decides what to look at and what to recommend. Deterministic code decides what's allowed, what gets written and what gets recorded.** The design decisions, including the MCP-specific ones, are explained in [docs/architecture.md](docs/architecture.md).

## Quickstart (mock mode, about 2 minutes)

Mock mode runs on seeded local data, so you need only a Claude API key. There are no HubSpot, GitHub or PostHog accounts to set up.

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/) (`pip install uv`).

```bash
uv sync
cp .env.example .env        # then set ANTHROPIC_API_KEY
uv run rro seed             # build the mock CRM, ticket queue, analytics and decision memory
uv run rro serve            # web UI on http://127.0.0.1:8000
```

Or from the terminal:

```bash
uv run rro run "Prep the renewal for Halcyon Robotics"
uv run rro approvals        # what's waiting for a human
uv run rro audit            # every call the agent made
```

## Live mode

The same agent, policy and prompts, pointed at real systems. Every account is on a free tier:

| System | Account | What goes in `.env` |
|---|---|---|
| HubSpot | Developer account → **developer test account**, with a private app / service key (companies and deals read/write, owners read, deal and company schemas read/write) | `HUBSPOT_ACCESS_TOKEN` |
| GitHub Issues | A repo for tickets, plus a fine-grained token with **Issues: read and write** on that repo only | `GITHUB_TICKETS_REPO`, `GITHUB_TOKEN` |
| PostHog | A project, plus a personal API key with **Query: read** and **Project: read** | `POSTHOG_HOST`, `POSTHOG_PROJECT_ID`, `POSTHOG_PROJECT_API_KEY`, `POSTHOG_PERSONAL_API_KEY` |

```bash
uv run rro seed-live         # load the same 8-account story into HubSpot, GitHub and PostHog
uv run rro --live score      # should match `rro score` in mock mode, account for account
uv run rro --live serve      # or: rro --live run "Prep the renewal for Halcyon Robotics"
```

`seed-live` only writes to a HubSpot developer test account or sandbox, and is safe to re-run. Each system can also be switched on its own, e.g. `CRM_BACKEND=live` with everything else on mock data.

## The 3-minute demo

Run `rro reset -y` first, then `rro serve`.

1. **Accounts page**: eight customers, designed as three healthy, three at-risk and two critical. Click **Prep renewal** on Halcyon Robotics.
2. **The run page**: the trace (read straight from the audit log) shows the agent find the company, then query tickets, usage and memory in parallel. It scores the account critical, 90/100.
3. **The briefing's Precedent section**: last year Halcyon got a 15% discount and usage fell anyway. A similar account churned despite a discount; another renewed at full price once its P1s were fixed. So the agent proposes the CRM risk update and *argues against* a discount.
4. **Approve the proposal**: open "Exact write the executor will make" to see the payload and its hash, then approve. The executor, not the model, writes it to the CRM.
5. **Audit log**: the human decision and the executor's write, each tied to the approval ID. Try editing a row in SQLite: the database refuses.
6. **Optional second act**: prep Summit Analytics, reject the proposal with a reason, and run it again. The agent reads the rejection from memory and won't re-propose until the reason no longer applies.

From the terminal, `rro tools` shows the policy at work (`crm.manage_crm_objects` exists but is hidden from the agent), and `rro score` computes every score with no LLM involved: the ground truth the agent is checked against.

## Commands

| Command | What it does |
|---|---|
| `rro seed` | Rebuild the mock systems and memory from `seed/` (dates are relative to today) |
| `rro seed-live` | Load the same story into HubSpot (test account only), GitHub Issues and PostHog |
| `rro --live <command>` | Run any command against the live systems instead of mock data |
| `rro accounts` | List the demo accounts |
| `rro tools` | Tool inventory: policy scope, whether the agent sees it, and what the server claims about itself |
| `rro run "<instruction>"` | Run the agent: briefing plus proposals for approval |
| `rro serve` | Web UI: start runs, watch the trace, read briefings, approve or reject |
| `rro approvals [--all]` | Proposals waiting for a human (or every decision) |
| `rro approve ID` / `rro reject ID --note "…"` | Decide a proposal. Approving runs the write through the executor |
| `rro memory [SLUG]` | Past decisions, approvers' reasons and outcomes |
| `rro audit [RUN_ID]` | Audit log for a run (defaults to the latest) |
| `rro score [SLUG]` | Deterministic risk scores via the MCP servers, with no LLM |
| `rro reset` | Delete runs, approvals, the audit log and briefings, then reseed |

## Configuration

Everything is set through `.env` (see [.env.example](.env.example)) and three files under `config/`:

- [`config/policy.yaml`](config/policy.yaml): which tools each system exposes, at which scope, and limits on proposals (e.g. the maximum discount).
- [`config/risk.yaml`](config/risk.yaml): scoring weights, thresholds and bands.
- [`config/servers.yaml`](config/servers.yaml): how each MCP server is started in mock and live mode, and exactly which environment variables each one receives.

The model defaults to `claude-opus-5-5` at `medium` effort. If a safety classifier declines a request, it is retried on Anthropic's recommended fallback model (`ANTHROPIC_FALLBACKS=true`).

## Risk scoring

| Factor | Points |
|---|---|
| Weekly active users, last 4 weeks vs the prior 8 | ≤ -40% → 40 · -20 to -40% → 25 · -10 to -20% → 10 |
| Open P1 / P2 issues | 15 each, max 30 / 5 each, max 10 |
| Days to renewal | < 30 → 20 · < 60 → 12 · < 90 → 6 |
| **Band** | 0–34 healthy · 35–64 at-risk · 65+ critical |

The agent supplies the signals. A deterministic engine turns them into a score, so the result is reproducible and every point is traceable to a factor. The CRM update the agent proposes takes its level and score from the engine, not from the model.

## Project layout

```
config/            policy, risk weights, MCP server launch config
seed/              accounts.yaml (the demo story for every system), memory.yaml (past decisions)
src/rro/
  agent/           Claude tool-use loop, system prompt, local tools (score, propose, brief)
  governance/      policy, Tool Gateway (MCP client host, audit), approval gate + executor
  web/             FastAPI + Jinja web UI
  risk.py          deterministic scoring engine
  signals.py       LLM-free signal collection (ground truth)
  live_seed.py     loads the demo story into HubSpot, GitHub and PostHog
  runtime.py       wires it all together for the CLI, web app and tests
  db.py            runs, approvals, append-only audit log
  cli.py           the `rro` command
src/mcp_servers/
  crm/             HubSpot-named CRM tools; backends: mock, HubSpot REST
  tickets/         task-shaped ticket tools; backends: mock, GitHub Issues
  usage/           task-shaped usage tools; backends: mock, PostHog (HogQL)
  memory/          past renewal decisions, read-only over MCP
tests/             unit + integration tests (real MCP servers, scripted LLM)
```

## Tests

```bash
uv run pytest
```

The integration tests start the real MCP servers over stdio and drive the agent loop with a scripted stand-in for Claude, so they need no API key. They check that:

- all 8 accounts land in their designed bands;
- write tools are never shown to the model, and calls to them are refused and audited;
- an approved write runs exactly once, and a payload tampered with after approval is refused;
- proposals are checked against the CRM (right account, open deal, discount within the policy limit);
- rejections need a reason, land in memory, and are visible to the next run;
- the audit log can't be edited or deleted;
- the full loop and the web approval flow work end to end.

## Roadmap

- [x] **Phase 1**: mock mode end to end: seeded data, read-only MCP tools, governance gateway, risk score, briefing.
- [x] **Phase 2**: decision memory, proposals, approval gate + executor, web UI.
- [x] **Phase 3**: live HubSpot, GitHub Issues and PostHog backends, live seeding, mock/live parity check.
- [ ] **Phase 3b**: HubSpot's own remote MCP server as an alternative CRM backend (OAuth).
- [ ] **Phase 4**: Slack approvals (Socket Mode) and approved writes to HubSpot.
- [ ] **Phase 5**: polish: demo recording, evals, CI.

## License

MIT
