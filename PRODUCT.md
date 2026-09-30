# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary audience for the web UI: hiring managers and solutions-engineering interviewers watching a roughly 3-minute narrated demo, a 15–20s README GIF, or the repo owner running it live. They should read it at a glance as a credible internal tool, and the governance story (the agent proposes, a human approves the exact payload, everything is audited) should be obvious on camera.

The fictional in-product users are account managers and CS leads prepping renewals; the UI should be plausible for them, but first impressions on screen outrank daily-use density.

## Product Purpose

One instruction ("Prep the renewal for Halcyon Robotics") starts a Claude agent that works across the CRM (HubSpot), the support queue (GitHub Issues) and product analytics (PostHog), scores renewal risk, writes an account-team briefing, and proposes CRM changes. Nothing is written to a system of record until a human approves it in Slack, the web UI or the CLI. It is a portfolio project for SE job applications; success is a reviewer understanding and trusting the governance model within one viewing.

## Positioning

The model cannot write. It can only propose; a human approves the exact payload (SHA-256 hashed), and a deterministic executor makes that one write once. Deny-by-default policy (HubSpot's MCP server offers 29 tools, the agent sees 3), an append-only audit log the database refuses to edit, and a decision memory of past approvals, rejections and outcomes that shapes each recommendation. Claude interprets evidence; deterministic code does the risk arithmetic.

## Operating Context

Runs locally (`rro serve`, localhost only, no login) against mock data or live HubSpot test account, GitHub repo and PostHog project. Pages: Accounts (start a run, account table, recent runs), Run (live trace polled via HTMX, briefing, proposed changes), Approvals (pending cards, decided table), Decision memory, Audit log. Slack is a parallel approval surface.

## Capabilities and Constraints

- Stack: FastAPI + Jinja2 templates + HTMX (polling only). Server-rendered.
- Keep all existing copy and the page structure (Accounts, Approvals, Memory, Audit log, Run); the redesign changes the look, not the wording or information architecture.
- Security behaviour is fixed: briefings are model output, rendered with no images, no raw HTML, and links only to the ticket tracker (others shown as inert text with the address visible); localhost only.
- Terminology: run, trace, briefing, proposal / proposed change, approval, executor, payload hash, audit log, decision memory, risk band (healthy / at-risk / critical), risk score /100.

## Evidence on Hand

- Seed accounts (8) in `seed/accounts.yaml`, decision memory in `seed/memory.yaml`.
- Eval results: 8/8 accounts, 64/64 checks (`evals/results.md`).
- README demo GIF (`docs/demo.gif`) built from real captures of the current UI; it will be rebuilt from new captures after the redesign.
- No customers, testimonials or usage claims exist; do not invent any.

## Product Principles

1. The human decision is the product: proposals, the exact write, and who approved it must be the most legible things on any screen.
2. Show, don't assert governance: the trace and audit log are the evidence, surfaced plainly.
3. Model output is untrusted: never present briefing content with more authority or interactivity than the system gives it.
4. Readable in one viewing: a stranger watching a short video should follow what happened.
