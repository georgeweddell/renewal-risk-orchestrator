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
2. **Decide.** A human approves or rejects the proposal in Slack, the web UI or the CLI. Every decision is audited as actor `human:<name>`; from Slack, the name is the real Slack user and member ID. A rejection needs a reason.
3. **Execute.** On approval, the executor (plain code, no LLM) replays the stored call through the gateway as actor `executor`, with the approval ID. The policy allows a write only if that approval is in state `approved` and the arguments hash to the stored value. After the write, the approval becomes `executed`, so it can't be replayed. A payload edited after approval fails the hash check and is refused.
4. **Remember.** Approvals and rejections are written to the decision memory, with the approver's name and reason.

There is no path from the model to a write tool: the model isn't shown write tools, the policy refuses write tools for any actor but `executor`, and the executor only acts on stored, human-approved payloads.

## Decision memory

`data/memory.db` holds past renewal decisions: the account, the risk band and drivers at the time, what was proposed, who approved or rejected it and why, and the eventual outcome (renewed, churned, pending). It is seeded from `seed/memory.yaml`, which includes former customers, because the most useful precedents are accounts whose renewals have already happened.

The agent reads memory through its own MCP server (`get_account_history`, `find_similar_decisions`), so memory reads are scoped and audited like any system. Only the approval executor writes to it, directly, so the agent can't rewrite the record it learns from. A record holds what code built (the action and its values) and the approver's note, never the model's own wording: a proposal's reason could echo a support ticket, and memory would keep that text in front of every later run. Each memory write has its own audit entry.

Similarity is structured, not semantic: the overlap between risk drivers (`usage_drop`, `open_p1`, `open_p2`, `renewal_soon`) plus a bonus for the same band. The records are structured, and a precedent should be easy to explain ("same drivers, same band"). Embeddings would add infrastructure and make matches harder to justify.

In testing, this changes behaviour in the ways you'd want. For Halcyon, the agent declined to propose a discount, citing Halcyon's own failed discount last year and a similar account that churned despite one. For Summit, after a human rejected the CRM update with a reason, the next run read that rejection and didn't re-propose it.

## MCP decisions

**What MCP is doing here.** MCP is a standard way for an AI application (the *client*) to discover and call tools offered by *servers*. Our gateway is the client. Each system is a server, a separate process we talk to over stdin/stdout (the "stdio transport"). At startup the gateway calls `tools/list` on each server. For each tool call from Claude, it calls `tools/call` on the right server. Swapping a mock server for a live one is a change to `config/servers.yaml`. The orchestrator doesn't change.

**We host the MCP client ourselves rather than using the Claude API's MCP connector.** The API can connect to remote MCP servers directly (`mcp_servers=`), but then the tool calls run on Anthropic's side and never pass through our code. We'd lose per-call policy checks, the audit log and the approval gate, which are the point of the project. The connector also needs publicly reachable servers.

**Write tools are hidden, not just refused.** The model only sees read-scope tools, so it can't plan around a write it doesn't know exists. If it tries anyway (for example by guessing a name), the gateway refuses the call, returns an error result and audits the attempt. The integration tests cover both cases.

**Server self-descriptions aren't trusted.** MCP lets a server annotate its tools as read-only or destructive. `rro tools` shows those annotations and flags any that disagree with the policy, but permission comes only from `policy.yaml`. A server's claims can make the app stricter, never looser: the trifecta gate refuses to run if a tool the model can see isn't labelled read-only by its server (see [SECURITY.md](../SECURITY.md)).

**Least-privilege environments.** `servers.yaml` lists the exact environment variables each server receives. A server never inherits the orchestrator's environment, so in live mode the tickets server can see the GitHub token and nothing else.

**Our own servers expose task-shaped tools.** `tickets.list_issues(account_id)` returns issues plus a count by priority. `usage.get_usage_trend(account_id)` returns the series plus a summary computed the same way every time. The agent doesn't write HogQL or GitHub search syntax, so it makes fewer, more reliable calls. Switching the ticketing system (GitHub to Jira or Zendesk) only needs a new backend class.

**One CRM server, two backends, HubSpot's tool names.** The `crm` server exposes `search_crm_objects`, `get_crm_objects`, `search_owners` and `manage_crm_objects`, the same names HubSpot's own MCP server uses, with HubSpot CRM v3 parameters (`filterGroups` and so on). Behind it, `CRM_BACKEND=mock` reads the seeded SQLite file and `CRM_BACKEND=hubspot` calls the HubSpot REST API with a private app token. Both return the same shapes (only the requested properties; associations as lists of IDs), so the agent, policy and prompt don't change between modes.

**HubSpot's own MCP server, under the same governance.** With `CRM_BACKEND=hubspot_mcp`, the CRM is HubSpot's official remote server at `mcp.hubspot.com`, reached over Streamable HTTP instead of a subprocess. What that involved:

- *OAuth.* HubSpot's server uses OAuth 2.1 with PKCE and has no dynamic client registration, so the client is an "MCP connector" created by hand in the developer account. The MCP SDK's `OAuthClientProvider` does discovery, PKCE, token exchange and refresh. `rro/hubspot_mcp.py` supplies the pre-registered client from `.env`, a token store in `data/`, and a loopback redirect for the one-off `rro hubspot-login`. Normal runs never open a browser: if the token can't be refreshed, start-up stops and says to log in again.
- *29 tools, 3 shown.* HubSpot's server offers 29 tools, from CRM search to marketing emails, landing pages and custom pipelines (11 of them write). The policy lists the same three read tools as for our own server, so the other 26 are hidden and denied without any extra configuration. That's deny-by-default earning its keep.
- *"Confirm before writing" is a parameter.* HubSpot's `manage_crm_objects` requires `confirmationStatus: "CONFIRMED"`. Any client, or a model calling the tool directly, can simply set it; HubSpot can only *ask* for confirmation. Here it's enforced: the model never sees the write tool, and only the executor sends `CONFIRMED`, as part of a payload a human approved and whose hash the policy checks.
- *Stripped arguments.* HubSpot's tools accept an optional `chatInsights` field that reports the user's intent back to HubSpot. Whether to share that is a governance decision, not the model's, so `servers.yaml` lists it under `strip_arguments`. The gateway removes it from the schemas the model sees and from any call that includes it, and says so in the audit log.
- *Two dialects for code, none for the agent.* The agent reads each server's own tool definitions and adapts without prompt changes (HubSpot's server uses `associatedWith` filters and integer IDs, for example). The code that validates proposals, builds the approved write and computes `rro score` can't adapt on its own, so it goes through `rro/crm_access.py`, which speaks both dialects.

Our own `crm` server remains the default live backend: it needs only a token, and its tool shapes are fixed by this project rather than by a third party.

**Why Slack approvals don't go through MCP.** MCP is request and response started by the client. A human clicking "Approve" is an *inbound* event that starts on Slack's side, so it's handled by Slack's Bolt SDK in Socket Mode (the app opens a websocket to Slack, so no public URL is needed). Most Slack MCP servers can't send interactive buttons either.

## Slack approvals

Slack is one more place to ask a human, not a second approval system. A click goes to the same `ApprovalService` as the web UI and CLI, so the payload hash, one-shot executor, audit log and memory behave exactly the same.

- *Posting.* When a run ends with proposals, each one is posted as a Block Kit card: proposal, reasoning, risk, the exact write with its payload hash, and Approve / Reject buttons. Approve asks for confirmation. Reject opens a form, because memory needs a reason. Posts and updates are audited (`slack.post_approval`, `slack.update_approval`).
- *Identity.* The approver is resolved from the click (`users.info`) and recorded as "Real Name (Slack U…)". That closes the web UI's gap, where the approver is whatever name is typed. `SLACK_APPROVERS` lists the member IDs who may decide, and is required: an empty list means nobody, not everybody, and the listener won't start. Anyone else gets a private "you're not on the approver list" reply, and nothing changes.
- *One decision, many surfaces.* Whichever surface decides first wins; the others see an already-decided approval. The Slack card is updated after a decision made anywhere, so it never shows stale buttons.
- *Least privilege.* The app has two bot scopes: `chat:write` (post and update cards) and `users:read` (approver names). It can't read channel history.
- *Where it runs.* The Socket Mode listener runs inside `rro serve`, or on its own with `rro slack`. `rro run` posts its cards from the terminal; clicks are then handled by whichever listener is running.

## Live mode

**Least privilege, per server.** `config/servers.yaml` hands each server only its own credentials through `${NAME}` references to `.env`. The CRM server gets the HubSpot token, the tickets server the GitHub token, and the usage server PostHog's *read* key, but not the key that can send events. A missing value stops start-up with a message naming it.

**Same story everywhere.** `rro seed-live` uses the same generators as mock seeding, so live and mock give identical usage figures and ticket counts. `rro --live score` is the parity check.

**Details worth knowing:**
- *HubSpot.* Seeding refuses any account that isn't a developer test account or sandbox. It creates custom properties in a "Renewal Risk Orchestrator" group, matches records on `account_slug` so re-runs update rather than duplicate, and clears the risk and pricing fields on re-seed so each demo starts clean. HubSpot owners are real user logins, so the demo's account owners are kept in an `account_owner_name` company field.
- *GitHub Issues.* An issue belongs to an account through an `account:<slug>` label, and its priority is a P1/P2/P3 label. GitHub can't backdate issues, so seeded issues carry their original report date in a hidden `<!-- rro:reported_at=… -->` comment, which the backend reads. Real issues fall back to GitHub's own timestamps.
- *PostHog.* A user is active in a week if they sent an `app_session` event with `account_id` set. The backend counts only complete Monday-to-Sunday weeks, reports weeks with no activity as zero rather than skipping them, and treats "no events at all" as an unknown account. Account IDs go into HogQL as query parameters, never by string formatting. PostHog caches query results, so the backend always asks for a fresh computation; a stale cache briefly reported zero events during testing.

**Known limitation: live usage data ages.** Seeded usage covers the 12 weeks before the seed date. Because only complete weeks count, a week later the newest week has no data and the trends shift. PostHog events can't be deleted, so `seed-live` won't send usage twice to the same project. To refresh live usage for a later demo, point `.env` at a new PostHog project and run `rro seed-live --only posthog`. Mock mode doesn't have this problem, which is why it's the default for demos.

## Prompt-injection defences

The agent reads text strangers can write, so the design assumes the model can be fooled and makes sure a fooled model can't do damage: tool results arrive labelled as untrusted data, every output channel (briefings, Slack cards, memory) is made inert, each run is pinned to one account, and a trifecta gate refuses to run if untrusted text could reach an unapproved write. The audit, the findings and the residual risks are in [SECURITY.md](../SECURITY.md).

## Other decisions

**Deterministic scoring.** The model gathers the signals and explains them. `src/rro/risk.py` does the arithmetic from `config/risk.yaml`. Scores are reproducible and every point is traceable to a factor. `rro score` computes the same scores with no LLM involved, which gives a ground truth to check the agent against.

**Our own loop, not the SDK's tool runner.** The loop is short, easy to walk through in an interview, and routes every call through the gateway. The message history is append-only, and thinking blocks are passed back unchanged, which the current Claude models require.

**Three databases.** `data/mock_systems.db` stands in for the systems of record, and the agent can reach it only through MCP servers. `data/memory.db` is the decision memory. `data/rro.db` is the orchestrator's own state: runs, approvals and the audit log.

**Append-only audit.** SQLite triggers reject UPDATE and DELETE on `audit_log`, so the app itself can't rewrite history. Credentials are redacted from logged arguments, and briefing bodies are logged by size only.

**Web UI.** FastAPI with server-rendered Jinja templates, plus HTMX to poll a running agent's panel. The live trace is read from the audit log, so what you watch is exactly what's recorded. One gateway is shared for the app's lifetime, and runs execute as background tasks. Briefings are model output, so they render with raw HTML and images off, and links work only to the ticket tracker. There's no login, so `rro serve` binds to localhost.

**Parallel server start-up.** Each MCP client's task groups must be entered and exited in the same task, so each connection is held open by its own long-lived task. That lets all four servers start at once (about 2.5 seconds instead of 8), while tool calls from any task share the connections.

**Model settings.** Defaults to `claude-opus-5-5` at `medium` effort, with thinking always on (effort sets how much). Progress notes between tool calls stream into the run trace. The stable prefix (tools plus system prompt) is prompt-cached across the turns of a run. Today's date goes in the first user message rather than the system prompt so the cache stays valid. Refusals are handled explicitly, and server-side fallbacks are on by default.

## Known trade-offs

- The agent passes signal values to `score_renewal_risk` itself, so a transcription error is possible. The inputs are recorded in the audit log, and `rro eval` compares them with the ground truth from `rro score`; the latest eval found none across all 8 accounts. The alternative, having the scoring tool fetch its own inputs, is more robust but hides the cross-system work that makes the agent's reasoning visible.
- Evals are a fixed set of 8 accounts with deterministic checks. They catch regressions in correctness and discipline. They don't grade the quality of the writing; a rubric graded by a second model would be the next step.
- Starting the servers takes about 2.5 seconds, because each is a cold Python process. Tool calls afterwards take milliseconds.
- Approver identity in the web UI and CLI is whatever name is typed in. Slack records the real user; a production web UI would sit behind single sign-on.
- Slack approvals need a listener running (`rro serve` or `rro slack`). A click made while none is running gets an error from Slack, and can simply be repeated once one starts.
- Memory outcomes (renewed / churned) for new decisions start as `pending`. Recording the eventual outcome is a manual step for now.
