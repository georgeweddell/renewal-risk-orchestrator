# Architecture and design decisions

## The principle

Claude decides **what to look at and what to recommend**. Deterministic code decides **what's allowed, what gets written and what gets recorded**. Every governance claim in this project comes from code the model cannot get around, never from instructions in a prompt.

## A run, step by step

1. `rro run` starts the **Tool Gateway**, which launches one MCP server per system (`crm`, `tickets`, `usage`) as a subprocess.
2. The gateway asks each server for its tools, namespaces them (`crm__search_crm_objects`) and labels each one with its scope from `config/policy.yaml`.
3. The **orchestrator** sends Claude the instruction, the system prompt and the tools: read-scope MCP tools plus two local tools, `score_renewal_risk` and `write_briefing`.
4. Claude calls tools. Each call goes through the gateway, which checks the policy, runs the call and writes an audit row. Calls that don't depend on each other run in parallel.
5. `score_renewal_risk` runs the deterministic engine. `write_briefing` saves markdown to `output/briefings/`. It refuses to run until the account has been scored.
6. The run's outcome (band, score, briefing path, token usage) is saved in the `runs` table.

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

**Two databases.** `data/mock_systems.db` stands in for the systems of record, and the agent can reach it only through MCP servers. `data/rro.db` is the orchestrator's own state (runs and the audit log; memory and approvals follow in Phase 2).

**Append-only audit.** SQLite triggers reject UPDATE and DELETE on `audit_log`, so the app itself can't rewrite history. Credentials are redacted from logged arguments, and briefing bodies are logged by size only.

**Model settings.** Defaults to `claude-opus-5-5` at `medium` effort, with thinking always on (effort sets how much). Progress notes between tool calls stream into the run trace. The stable prefix (tools plus system prompt) is prompt-cached across the turns of a run. Today's date goes in the first user message rather than the system prompt so the cache stays valid. Refusals are handled explicitly, and server-side fallbacks are on by default.

## Known trade-offs

- The agent passes signal values to `score_renewal_risk` itself, so a transcription error is possible. The inputs are recorded in the audit log, and the Phase 5 eval compares them with `rro score`.
- Starting the three servers takes a few seconds, because each is a cold Python process. Tool calls afterwards take milliseconds.
