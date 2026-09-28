# Architecture and design decisions

## The principle

Claude decides **what to look at and what to recommend**. Deterministic code decides **what's allowed, what gets written and what gets recorded**. Every governance claim in this project comes from code the model cannot get around, never from instructions in a prompt.

## A run, step by step

1. The **Tool Gateway** launches one MCP server per system (`crm`, `tickets`, `usage`, `memory`) as a subprocess, all in parallel.
2. The gateway asks each server for its tools, namespaces them (`crm__search_crm_objects`) and labels each one with its scope from `config/policy.yaml`.
3. The **orchestrator** sends Claude the instruction, the system prompt and the tools: read-scope MCP tools plus four local tools (`score_renewal_risk`, `propose_crm_update`, `propose_pricing_exception`, `write_briefing`).
4. Claude calls tools. Each call goes through the gateway, which checks the policy, runs the call and writes an audit row. Calls that don't depend on each other run in parallel.
5. `score_renewal_risk` runs the deterministic engine. Claude then checks memory: the account's own history, and similar cases matched on the engine's risk drivers.
6. The `propose_*` tools queue writes for a human (see *The approval gate* below). `write_briefing` saves markdown to `output/briefings/`. Both refuse to run until the account has been scored.
7. The run ends as `awaiting_approval` if it proposed anything, and becomes `completed` once every proposal is decided.

## The approval gate

The model can ask for a write but can never make one. The sequence:

1. **Propose.** `propose_crm_update` checks with the CRM (through the gateway, as actor `system`) that the deal exists, is open and belongs to the account. It then builds the exact `manage_crm_objects` call and stores it as a pending approval with a SHA-256 hash of `(system, tool, arguments)`. The risk level and score in that call come from the engine's assessment, not from anything the model typed. `propose_pricing_exception` does the same, and also enforces `limits.pricing_exception_max_pct` from the policy.
2. **Decide.** A human approves or rejects the proposal in the web UI or the CLI (Slack in Phase 4). Both are audited as actor `human:<name>`. A rejection needs a reason.
3. **Execute.** On approval, the executor (plain code, no LLM) replays the stored call through the gateway as actor `executor`, with the approval ID. The policy allows a write only if that approval is in state `approved` and the arguments hash to the stored value. After the write, the approval becomes `executed`, so it can't be replayed. A payload edited after approval fails the hash check and is refused.
4. **Remember.** Approvals and rejections are written to the decision memory, with the approver's name and reason.

There is no path from the model to a write tool: the model isn't shown write tools, the policy refuses write tools for any actor but `executor`, and the executor only acts on stored, human-approved payloads.

## Decision memory

`data/memory.db` holds past renewal decisions: the account, the risk band and drivers at the time, what was proposed, who approved or rejected it and why, and the eventual outcome (renewed, churned, pending). It is seeded from `seed/memory.yaml`, which includes former customers, because the most useful precedents are accounts whose renewals have already happened.

The agent reads memory through its own MCP server (`get_account_history`, `find_similar_decisions`), so memory reads are scoped and audited like any system. Only the approval executor writes to it, directly, so the agent can't rewrite the record it learns from.

Similarity is structured, not semantic: the overlap between risk drivers (`usage_drop`, `open_p1`, `open_p2`, `renewal_soon`) plus a bonus for the same band. The records are structured, and a precedent should be easy to explain ("same drivers, same band"). Embeddings would add infrastructure and make matches harder to justify.

In testing, this changes behaviour in the ways you'd want. For Halcyon, the agent declined to propose a discount, citing Halcyon's own failed discount last year and a similar account that churned despite one. For Summit, after a human rejected the CRM update with a reason, the next run read that rejection and didn't re-propose it.

## MCP decisions

**What MCP is doing here.** MCP is a standard way for an AI application (the *client*) to discover and call tools offered by *servers*. Our gateway is the client. Each system is a server, a separate process we talk to over stdin/stdout (the "stdio transport"). At startup the gateway calls `tools/list` on each server. For each tool call from Claude, it calls `tools/call` on the right server. Swapping a mock server for a live one is a change to `config/servers.yaml`. The orchestrator doesn't change.

**We host the MCP client ourselves rather than using the Claude API's MCP connector.** The API can connect to remote MCP servers directly (`mcp_servers=`), but then the tool calls run on Anthropic's side and never pass through our code. We'd lose per-call policy checks, the audit log and the approval gate, which are the point of the project. The connector also needs publicly reachable servers.

**Write tools are hidden, not just refused.** The model only sees read-scope tools, so it can't plan around a write it doesn't know exists. If it tries anyway (for example by guessing a name), the gateway refuses the call, returns an error result and audits the attempt. The integration tests cover both cases.

**Server self-descriptions aren't trusted.** MCP lets a server annotate its tools as read-only or destructive. `rro tools` shows those annotations and flags any that disagree with the policy, but enforcement comes only from `policy.yaml`. A server's claims about itself aren't a security control.

**Least-privilege environments.** `servers.yaml` lists the exact environment variables each server receives. A server never inherits the orchestrator's environment, so in live mode the tickets server can see the GitHub token and nothing else.

**Our own servers expose task-shaped tools.** `tickets.list_issues(account_id)` returns issues plus a count by priority. `usage.get_usage_trend(account_id)` returns the series plus a summary computed the same way every time. The agent doesn't write HogQL or GitHub search syntax, so it makes fewer, more reliable calls. Switching the ticketing system (GitHub to Jira or Zendesk) only needs a new backend class.

**The mock CRM mirrors HubSpot's MCP tools.** HubSpot's remote MCP server (`mcp.hubspot.com`, GA with write support since April 2026) is what live mode will use. The mock server copies the names and HubSpot-style parameters of the tools we use (`search_crm_objects`, `get_crm_objects`, `search_owners`, `manage_crm_objects`), so prompts and policy are identical in both modes. The exact live schemas are reconciled in Phase 3. HubSpot's server uses OAuth 2.1 with PKCE, which the MCP Python SDK's client supports.

**Why Slack approvals won't go through MCP (Phase 4).** MCP is client-initiated request and response. A human clicking "Approve" is an *inbound* event, which is a job for Slack's Bolt SDK in Socket Mode (a websocket, so no public URL is needed). Most Slack MCP servers can't send interactive buttons either.

## Other decisions

**Deterministic scoring.** The model gathers the signals and explains them. `src/rro/risk.py` does the arithmetic from `config/risk.yaml`. Scores are reproducible and every point is traceable to a factor. `rro score` computes the same scores with no LLM involved, which gives a ground truth to check the agent against.

**Our own loop, not the SDK's tool runner.** The loop is short, easy to walk through in an interview, and routes every call through the gateway. The message history is append-only, and thinking blocks are passed back unchanged, which the current Claude models require.

**Three databases.** `data/mock_systems.db` stands in for the systems of record, and the agent can reach it only through MCP servers. `data/memory.db` is the decision memory. `data/rro.db` is the orchestrator's own state: runs, approvals and the audit log.

**Append-only audit.** SQLite triggers reject UPDATE and DELETE on `audit_log`, so the app itself can't rewrite history. Credentials are redacted from logged arguments, and briefing bodies are logged by size only.

**Web UI.** FastAPI with server-rendered Jinja templates, plus HTMX to poll a running agent's panel. The live trace is read from the audit log, so what you watch is exactly what's recorded. One gateway is shared for the app's lifetime, and runs execute as background tasks. Briefings are rendered with raw HTML disabled, since they're model output. There's no login, so `rro serve` binds to localhost.

**Parallel server start-up.** Each MCP client's task groups must be entered and exited in the same task, so each connection is held open by its own long-lived task. That lets all four servers start at once (about 2.5 seconds instead of 8), while tool calls from any task share the connections.

**Model settings.** Defaults to `claude-opus-5-5` at `medium` effort, with thinking always on (effort sets how much). Progress notes between tool calls stream into the run trace. The stable prefix (tools plus system prompt) is prompt-cached across the turns of a run. Today's date goes in the first user message rather than the system prompt so the cache stays valid. Refusals are handled explicitly, and server-side fallbacks are on by default.

## Known trade-offs

- The agent passes signal values to `score_renewal_risk` itself, so a transcription error is possible. The inputs are recorded in the audit log, and the Phase 5 eval compares them with `rro score`.
- Starting the servers takes about 2.5 seconds, because each is a cold Python process. Tool calls afterwards take milliseconds.
- Approver identity in the web UI and CLI is whatever name is typed in. Real identities come with Slack in Phase 4.
- Memory outcomes (renewed / churned) for new decisions start as `pending`. Recording the eventual outcome is a manual step for now.
