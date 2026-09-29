"""Slack as an approval surface.

Why not a Slack MCP server: MCP is request/response started by the client.
A person clicking "Approve" is an event that starts on Slack's side, so this
uses Slack's own SDK (Bolt) in Socket Mode: the app opens a websocket to
Slack, so no public URL is needed.

Slack is only a way of asking a human. The decision still goes through the
ApprovalService, so the approval gate, payload hash, executor, audit log and
memory are exactly the same as for the web UI and CLI. What Slack adds is a
real identity: the approver recorded is the Slack user who clicked, not a
typed-in name.

  SlackNotifier            posts approval cards and updates them after a decision
  SlackApprovalHandlers    what happens on Approve / Reject (testable without Slack)
  SlackListener            wires the handlers to Slack over Socket Mode
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from slack_sdk.web.async_client import AsyncWebClient

from rro.db import AuditEntry, Store
from rro.governance.approvals import Approval, ApprovalError, ApprovalService, ApprovalStore
from rro.settings import Settings

APPROVE_ACTION = "rro_approve"
REJECT_ACTION = "rro_reject"
REJECT_MODAL = "rro_reject_modal"
REASON_BLOCK, REASON_INPUT = "reason_block", "reason"
ACTION_LABELS = {"crm_risk_update": "CRM risk update", "pricing_exception": "Pricing exception"}


class SlackNotifier:
    def __init__(self, settings: Settings, approvals: ApprovalStore, store: Store, client: AsyncWebClient | None = None):
        self.settings = settings
        self.approvals = approvals
        self.store = store
        self.channel = settings.slack_approvals_channel
        self.client = client or AsyncWebClient(token=settings.value("SLACK_BOT_TOKEN"))

    async def post_pending_for_run(self, run_id: str) -> list[str]:
        """Post a card for each of the run's pending approvals not yet in Slack. Returns their IDs."""
        posted = []
        for approval in reversed(self.approvals.list(run_id=run_id, status="pending")):
            if not approval.slack_ts:
                await self.post_approval(approval)
                posted.append(approval.id)
        return posted

    async def post_approval(self, approval: Approval) -> None:
        response = await self.client.chat_postMessage(
            channel=self.channel, text=_fallback_text(approval), blocks=approval_blocks(approval, self.settings.rro_base_url),
            # Never let Slack fetch a preview of a link in the card: the card carries model-written text.
            unfurl_links=False, unfurl_media=False,
        )
        self.approvals.set_slack_message(approval.id, response["channel"], response["ts"])
        self._audit(approval, "post_approval", {"approval_id": approval.id, "channel": response["channel"]})

    async def update_card(self, approval: Approval) -> None:
        """Show the decision on the card (whichever surface it was made on) and remove the buttons."""
        approval = self.approvals.get(approval.id)
        if not approval.slack_ts:
            return
        await self.client.chat_update(
            channel=approval.slack_channel, ts=approval.slack_ts, text=_fallback_text(approval),
            blocks=approval_blocks(approval, self.settings.rro_base_url),
        )  # fmt: skip
        self._audit(approval, "update_approval", {"approval_id": approval.id, "status": approval.status})

    async def approver_name(self, user_id: str) -> str:
        """The approver as recorded in the audit log and memory: real name plus Slack member ID."""
        try:
            user = (await self.client.users_info(user=user_id))["user"]
            name = user.get("real_name") or user.get("name") or user_id
        except Exception:  # still record who decided, even if the profile lookup fails
            name = user_id
        return f"{name} (Slack {user_id})"

    def _audit(self, approval: Approval, tool: str, args: dict) -> None:
        self.store.add_audit(
            AuditEntry(run_id=approval.run_id, actor="system", system="slack", tool=tool, scope="notify",
                       decision="allowed", args=args, approval_id=approval.id)  # fmt: skip
        )


class SlackApprovalHandlers:
    """What happens when someone clicks Approve or Reject. Independent of Bolt, so it can be tested."""

    def __init__(self, notifier: SlackNotifier, approvals: ApprovalStore, service: Callable[[], ApprovalService], allowed: set[str]):
        self.notifier = notifier
        self.approvals = approvals
        self.service = service
        self.allowed = allowed

    async def on_approve(self, body: dict, client: Any) -> None:
        user_id, approval_id, channel = body["user"]["id"], body["actions"][0]["value"], body["channel"]["id"]
        if not await self._may_decide(client, user_id, channel):
            return
        name = await self.notifier.approver_name(user_id)
        try:
            await self.service().approve(approval_id, by=name)
        except ApprovalError as exc:
            await client.chat_postEphemeral(channel=channel, user=user_id, text=f"Nothing changed: {exc}")
        await self.notifier.update_card(self.approvals.get(approval_id))

    async def on_reject_click(self, body: dict, client: Any) -> None:
        user_id, approval_id, channel = body["user"]["id"], body["actions"][0]["value"], body["channel"]["id"]
        if not await self._may_decide(client, user_id, channel):
            return
        approval = self.approvals.get(approval_id)
        await client.views_open(trigger_id=body["trigger_id"], view=reject_modal(approval))

    async def on_reject_submit(self, ack: Callable, body: dict, view: dict, client: Any) -> None:
        user_id, approval_id = body["user"]["id"], view["private_metadata"]
        note = view["state"]["values"][REASON_BLOCK][REASON_INPUT]["value"] or ""
        if user_id not in self.allowed:
            await ack(response_action="errors", errors={REASON_BLOCK: "You're not on the approver list for this channel."})
            return
        name = await self.notifier.approver_name(user_id)
        try:
            self.service().reject(approval_id, by=name, note=note)
        except ApprovalError as exc:
            await ack(response_action="errors", errors={REASON_BLOCK: str(exc)})
            return
        await ack()
        await self.notifier.update_card(self.approvals.get(approval_id))

    async def _may_decide(self, client: Any, user_id: str, channel: str) -> bool:
        # Fail closed: an empty list means nobody may decide, not everybody. A rejection
        # note goes into memory that future runs read, so who may write one matters.
        if user_id in self.allowed:
            return True
        await client.chat_postEphemeral(
            channel=channel, user=user_id, text="Only the configured approvers (SLACK_APPROVERS) can decide on these."
        )
        return False


class SlackListener:
    """Receives button clicks from Slack over Socket Mode and hands them to SlackApprovalHandlers."""

    def __init__(self, settings: Settings, handlers: SlackApprovalHandlers):
        self.settings = settings
        self.handlers = handlers
        self.handler = None

    async def start(self) -> None:
        # Built here rather than in __init__: the Socket Mode client needs a running event loop.
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        from slack_bolt.async_app import AsyncApp

        settings, handlers = self.settings, self.handlers
        self.app = AsyncApp(token=settings.value("SLACK_BOT_TOKEN"))

        @self.app.action(APPROVE_ACTION)
        async def approve(ack, body, client):
            await ack()  # Slack needs an answer within 3 seconds; the write can take longer
            await handlers.on_approve(body, client)

        @self.app.action(REJECT_ACTION)
        async def reject(ack, body, client):
            await ack()
            await handlers.on_reject_click(body, client)

        @self.app.view(REJECT_MODAL)
        async def reject_submitted(ack, body, view, client):
            await handlers.on_reject_submit(ack, body, view, client)

        self.handler = AsyncSocketModeHandler(self.app, settings.value("SLACK_APP_TOKEN"))
        await self.handler.connect_async()

    async def stop(self) -> None:
        if self.handler:
            await self.handler.close_async()


# --- Block Kit ----------------------------------------------------------------------------
def _esc(text: str) -> str:
    """Make text inert in Slack mrkdwn.

    Summaries, rationales and the write payload contain model-written text, and
    names come from CRM fields. Slack reads <...> as a link or a mention
    (<!channel>, <@U123>, <https://...|text>), so &, < and > are escaped as
    Slack's formatting guide says. The text still reads the same; it just can't
    ping anyone or hide a link behind friendly words.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def approval_blocks(a: Approval, base_url: str) -> list[dict]:
    label = ACTION_LABELS.get(a.action_type, a.action_type)
    run_link = f"<{base_url.rstrip('/')}/runs/{a.run_id}|briefing>" if a.run_id else "n/a"
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{label}: {a.account_name}"[:150]}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Risk*\n{a.risk_band} · {a.risk_score}/100"},
                {"type": "mrkdwn", "text": f"*Drivers*\n{', '.join(a.drivers) or 'none'}"},
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": _esc(a.summary)[:2900]}},
    ]
    if a.action_type == "pricing_exception":
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Rationale:* {_esc(a.rationale)}"[:2900]}})
    write = _esc(json.dumps(a.args, indent=1))
    blocks += [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Exact write the executor will make:* `{a.system}.{a.tool}`\n```{write[:2500]}```"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"`{a.id}` · payload sha256 `{a.payload_hash[:16]}…` · {run_link}"}]},
    ]
    if a.status == "pending":
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button", "action_id": APPROVE_ACTION, "value": a.id, "style": "primary",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "confirm": {
                            "title": {"type": "plain_text", "text": "Approve this write?"},
                            "text": {"type": "mrkdwn", "text": f"The executor will run `{a.system}.{a.tool}` exactly as shown."},
                            "confirm": {"type": "plain_text", "text": "Approve"},
                            "deny": {"type": "plain_text", "text": "Cancel"},
                        },
                    },
                    {"type": "button", "action_id": REJECT_ACTION, "value": a.id, "style": "danger",
                     "text": {"type": "plain_text", "text": "Reject…"}},
                ],
            }
        )  # fmt: skip
    else:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _outcome(a)}]})
    return blocks


def reject_modal(a: Approval) -> dict:
    return {
        "type": "modal",
        "callback_id": REJECT_MODAL,
        "private_metadata": a.id,
        "title": {"type": "plain_text", "text": "Reject proposal"},
        "submit": {"type": "plain_text", "text": "Reject"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{_esc(a.account_name)}*: {_esc(a.summary)[:500]}"}},
            {
                "type": "input",
                "block_id": REASON_BLOCK,
                "label": {"type": "plain_text", "text": "Why? Future runs will read this."},
                "element": {"type": "plain_text_input", "action_id": REASON_INPUT, "multiline": True},
            },
        ],
    }


def _outcome(a: Approval) -> str:
    when = (a.decided_at or "")[:16].replace("T", " ")
    if a.status == "executed":
        return f":white_check_mark: *Approved* by {_esc(a.decided_by or '')} · {when} UTC · written to the CRM by the executor"
    if a.status == "rejected":
        return f":x: *Rejected* by {_esc(a.decided_by or '')} · {when} UTC · “{_esc(a.decision_note or '')}”"
    if a.status == "failed":
        return f":warning: *Approved* by {_esc(a.decided_by or '')}, but the write failed: {_esc((a.result_text or '')[:200])}"
    return f"*{a.status}* by {_esc(a.decided_by or '')}"


def _fallback_text(a: Approval) -> str:
    return _esc(f"{ACTION_LABELS.get(a.action_type, a.action_type)} for {a.account_name}: {a.status}")
