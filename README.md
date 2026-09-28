# Renewal Risk Orchestrator

**One instruction ("prep the renewal for Halcyon Robotics") and an agent works across the CRM, the support queue and product analytics to score renewal risk and brief the account team. Every tool call is permission-scoped and audited, and nothing is written to a system of record without a human approving it.**

Built with Claude (Anthropic API), the Model Context Protocol (MCP), FastAPI and SQLite.

---

## Why this exists

Everest Group's research on *work orchestration platforms* uses a renewal as its example workflow. It argues that most agent tooling is weakest on three layers. This project is built around those three:

| Layer | What that means here |
|---|---|
| **Governance** | Per-system read/write scopes in a policy file, enforced in code on every call. The model is never even shown a write tool. Every call, allowed or denied, goes into an append-only audit log. Writes go through a human approval gate *(Phase 2)*. |
| **Persistent memory** | A store of past renewal decisions and their outcomes that the agent consults before recommending anything *(Phase 2)*. |
| **Depth over breadth** | Four systems with real two-way actions, rather than a long list of shallow integrations. |

## What it does

| Step | Status |
|---|---|
| 1. Pull the account, renewal deal and owner from the CRM (HubSpot) | ✅ mock · live in Phase 3 |
| 2. Pull open support issues (GitHub Issues as a stand-in ticketing system) | ✅ mock · live in Phase 3 |
| 3. Pull the product usage trend (PostHog) | ✅ mock · live in Phase 3 |
| 4. Score renewal risk with explainable reasoning | ✅ |
| 5. Write an account team briefing (markdown) | ✅ |
| 6. Propose a CRM risk update and a pricing exception, routed for human approval | Phase 2 (web) · Phase 4 (Slack) |
| 7. Write to the CRM only after approval | Phase 2 (mock) · Phase 4 (HubSpot) |

## Architecture

```
  "Prep the renewal for Halcyon Robotics"   (rro run ...)
                │
┌───────────────▼──────────────────────────────────────────────────────┐
│ ORCHESTRATOR: Claude tool-use loop (src/rro/agent)                   │
│  sees read tools + local tools: score_renewal_risk, write_briefing   │
└───────┬──────────────────────────────────────────┬───────────────────┘
        │ tool calls                               │ local tools
┌───────▼──────────────────────────────────────────▼───────────────────┐
│ GOVERNANCE: Tool Gateway = MCP client host (src/rro/governance)      │
│  ① policy.yaml: system × tool × scope, deny by default               │
│  ② write tools never shown to the model; only the approval executor  │
│     can run them, for a human-approved call                          │
│  ③ every call → append-only audit_log (SQLite triggers block edits)  │
└──┬────────────────┬─────────────────┬────────────────────────────────┘
   │ MCP over stdio │                 │
┌──▼─────────┐ ┌────▼──────┐ ┌────────▼───┐      ┌──────────────────────┐
│ crm        │ │ tickets   │ │ usage      │      │ risk engine          │
│ HubSpot    │ │ GitHub    │ │ PostHog    │      │ deterministic,       │
│ read+write │ │ read      │ │ read       │      │ config/risk.yaml     │
└────────────┘ └───────────┘ └────────────┘      └──────────────────────┘
```

**Claude decides what to look at and what to recommend. Deterministic code decides what's allowed, what gets written and what gets recorded.** The design decisions, including the MCP-specific ones, are explained in [docs/architecture.md](docs/architecture.md).

## Quickstart (mock mode, about 2 minutes)

Mock mode runs on seeded local data, so you need only a Claude API key. There are no HubSpot, GitHub or PostHog accounts to set up.

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/) (`pip install uv`).

```bash
uv sync
cp .env.example .env        # then set ANTHROPIC_API_KEY
uv run rro seed             # build the mock CRM, ticket queue and analytics
uv run rro run "Prep the renewal for Halcyon Robotics"
```

The briefing is saved under `output/briefings/`. To see every call the agent made:

```bash
uv run rro audit
```

## The 3-minute demo

1. **`rro accounts`**: eight customers, designed as three healthy, three at-risk and two critical.
2. **`rro tools`**: what the MCP servers offer, and what the policy allows. `crm.manage_crm_objects` exists, but it is write-scoped and hidden from the agent.
3. **`rro run "Prep the renewal for Halcyon Robotics"`**: watch the agent find the company, then fan out to tickets and usage in parallel, score the account (critical, 90/100) and write the briefing.
4. **`rro audit`**: every call with its scope, the policy decision, the arguments and the latency. Try editing a row in SQLite: the database refuses.
5. **`rro score`**: the same scores computed with no LLM involved. This is the ground truth the agent is checked against.

## Commands

| Command | What it does |
|---|---|
| `rro seed` | Rebuild the mock systems from `seed/accounts.yaml` (dates are relative to today) |
| `rro accounts` | List the demo accounts |
| `rro tools` | Tool inventory: policy scope, whether the agent sees it, and what the server claims about itself |
| `rro run "<instruction>"` | Run the agent and write a briefing |
| `rro audit [RUN_ID]` | Audit log for a run (defaults to the latest) |
| `rro score [SLUG]` | Deterministic risk scores via the MCP servers, with no LLM |
| `rro reset` | Delete run history, the audit log and briefings, then reseed |

## Configuration

Everything is set through `.env` (see [.env.example](.env.example)) and three files under `config/`:

- [`config/policy.yaml`](config/policy.yaml): which tools each system exposes, and at which scope.
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

The agent supplies the signals. A deterministic engine turns them into a score, so the result is reproducible and every point is traceable to a factor.

## Project layout

```
config/            policy, risk weights, MCP server launch config
seed/              accounts.yaml: the demo story for every system
src/rro/
  agent/           Claude tool-use loop, system prompt, local tools
  governance/      policy + Tool Gateway (MCP client host, audit)
  risk.py          deterministic scoring engine
  signals.py       LLM-free signal collection (ground truth)
  db.py            runs + append-only audit log
  cli.py           the `rro` command
src/mcp_servers/
  mock_crm/        mirrors the HubSpot MCP tools this project uses
  tickets/         task-shaped ticket tools (mock backend; GitHub in Phase 3)
  usage/           task-shaped usage tools (mock backend; PostHog in Phase 3)
tests/             unit + integration tests (real MCP servers, scripted LLM)
```

## Tests

```bash
uv run pytest
```

The integration tests start the real MCP servers over stdio and drive the agent loop with a scripted stand-in for Claude, so they need no API key. They check that:

- all 8 accounts land in their designed bands;
- write tools are never shown to the model, and calls to them are refused and audited;
- the audit log can't be edited or deleted;
- the full loop produces a briefing.

## Roadmap

- [x] **Phase 1**: mock mode end to end: seeded data, read-only MCP tools, governance gateway, risk score, briefing.
- [ ] **Phase 2**: memory of past decisions, proposed actions, web approval UI, approval executor.
- [ ] **Phase 3**: live reads from HubSpot, GitHub Issues and PostHog.
- [ ] **Phase 4**: Slack approvals (Socket Mode) and approved writes to HubSpot.
- [ ] **Phase 5**: polish: demo recording, evals, CI.

## License

MIT
