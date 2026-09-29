"""Slack approvals against a fake Slack client (nothing is posted)."""

import json

from rro.notify.slack import (
    APPROVE_ACTION,
    REASON_BLOCK,
    REASON_INPUT,
    REJECT_ACTION,
    SlackApprovalHandlers,
    SlackNotifier,
    approval_blocks,
)
from test_approvals import RISK_PROPS, deal_properties, propose


class FakeSlack:
    """Records Web API calls and answers the ones the app reads."""

    def __init__(self):
        self.calls = []

    def _record(self, method, **kwargs):
        self.calls.append((method, kwargs))

    async def chat_postMessage(self, **kwargs):
        self._record("chat_postMessage", **kwargs)
        return {"channel": kwargs["channel"], "ts": "1727600000.000100"}

    async def chat_update(self, **kwargs):
        self._record("chat_update", **kwargs)
        return {"ok": True}

    async def chat_postEphemeral(self, **kwargs):
        self._record("chat_postEphemeral", **kwargs)

    async def views_open(self, **kwargs):
        self._record("views_open", **kwargs)

    async def users_info(self, user):
        return {"user": {"id": user, "real_name": "Priya Shah"}}

    def methods(self):
        return [m for m, _ in self.calls]


def slack_setup(rt, allowed=frozenset()):
    settings = rt.settings.model_copy(update={"slack_approvals_channel": "C123", "rro_base_url": "http://demo.test"})
    fake = FakeSlack()
    notifier = SlackNotifier(settings, rt.approvals, rt.store, client=fake)
    handlers = SlackApprovalHandlers(notifier, rt.approvals, rt.approval_service, set(allowed))
    return fake, notifier, handlers


def click(approval_id, action, user="U1"):
    return {"user": {"id": user}, "channel": {"id": "C123"}, "trigger_id": "trig", "actions": [{"action_id": action, "value": approval_id}]}


def action_ids(blocks):
    return [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]


def test_the_listener_can_be_built_outside_an_event_loop(rt):
    from rro.notify.slack import SlackListener

    _, _, handlers = slack_setup(rt)
    listener = SlackListener(rt.settings.model_copy(update={"slack_app_token": "xapp-test"}), handlers)
    assert listener.handler is None  # the Socket Mode client is only created in start()


def test_card_shows_the_exact_write_and_buttons_only_while_pending(rt):
    approval = propose(rt)
    blocks = approval_blocks(approval, "http://demo.test")
    text = json.dumps(blocks)
    assert "crm.manage_crm_objects" in text and approval.payload_hash[:16] in text
    assert "http://demo.test/runs/run-1" in text
    assert action_ids(blocks) == [APPROVE_ACTION, REJECT_ACTION]


async def test_pending_approvals_are_posted_once(rt):
    propose(rt)
    fake, notifier, _ = slack_setup(rt)
    assert len(await notifier.post_pending_for_run("run-1")) == 1
    assert await notifier.post_pending_for_run("run-1") == []  # already posted
    assert fake.methods() == ["chat_postMessage"]
    (approval,) = rt.approvals.list()
    assert (approval.slack_channel, approval.slack_ts) == ("C123", "1727600000.000100")
    assert ("system", "slack", "post_approval") in [(r["actor"], r["system"], r["tool"]) for r in rt.store.audit_for_run("run-1")]


async def test_approve_in_slack_runs_the_write_as_the_slack_user(rt):
    approval = propose(rt)
    fake, notifier, handlers = slack_setup(rt)
    await notifier.post_approval(approval)
    async with rt.gateway as gateway:
        await handlers.on_approve(click(approval.id, APPROVE_ACTION, user="U42"), fake)
        assert await deal_properties(gateway) == RISK_PROPS

    decided = rt.approvals.get(approval.id)
    assert decided.status == "executed" and decided.decided_by == "Priya Shah (Slack U42)"
    assert rt.memory.for_account("halcyon-robotics")[0].approver == "Priya Shah (Slack U42)"
    method, update = fake.calls[-1]
    assert method == "chat_update" and action_ids(update["blocks"]) == []  # buttons gone
    assert "Approved" in json.dumps(update["blocks"])


async def test_only_listed_approvers_can_decide(rt):
    approval = propose(rt)
    fake, notifier, handlers = slack_setup(rt, allowed={"U_BOSS"})
    await handlers.on_approve(click(approval.id, APPROVE_ACTION, user="U_INTERN"), fake)
    await handlers.on_reject_click(click(approval.id, REJECT_ACTION, user="U_INTERN"), fake)
    assert fake.methods() == ["chat_postEphemeral", "chat_postEphemeral"]
    assert rt.approvals.get(approval.id).status == "pending"


async def test_reject_opens_a_form_and_records_the_reason(rt):
    approval = propose(rt)
    fake, notifier, handlers = slack_setup(rt)
    await notifier.post_approval(approval)
    await handlers.on_reject_click(click(approval.id, REJECT_ACTION), fake)
    assert fake.calls[-1][0] == "views_open" and fake.calls[-1][1]["view"]["private_metadata"] == approval.id

    acks = []

    async def ack(**kwargs):
        acks.append(kwargs)

    view = {"private_metadata": approval.id, "state": {"values": {REASON_BLOCK: {REASON_INPUT: {"value": "Wait for the P1 fix."}}}}}
    await handlers.on_reject_submit(ack, {"user": {"id": "U7"}}, view, fake)
    assert acks == [{}]
    decided = rt.approvals.get(approval.id)
    assert decided.status == "rejected" and decided.decision_note == "Wait for the P1 fix."
    assert "Rejected" in json.dumps(fake.calls[-1][1]["blocks"])

    # Deciding twice is refused, and the form says why.
    acks.clear()
    await handlers.on_reject_submit(ack, {"user": {"id": "U7"}}, view, fake)
    assert acks[0]["response_action"] == "errors" and "already rejected" in acks[0]["errors"][REASON_BLOCK]


async def test_a_blank_reason_is_refused_in_the_form(rt):
    approval = propose(rt)
    fake, _, handlers = slack_setup(rt)
    acks = []

    async def ack(**kwargs):
        acks.append(kwargs)

    view = {"private_metadata": approval.id, "state": {"values": {REASON_BLOCK: {REASON_INPUT: {"value": "   "}}}}}
    await handlers.on_reject_submit(ack, {"user": {"id": "U7"}}, view, fake)
    assert acks[0]["response_action"] == "errors"
    assert rt.approvals.get(approval.id).status == "pending"


async def test_a_decision_made_elsewhere_updates_the_card(rt):
    approval = propose(rt)
    fake, notifier, _ = slack_setup(rt)
    await notifier.post_approval(approval)
    rt.approval_service().reject(approval.id, by="Sam Reyes", note="Not yet")
    await notifier.update_card(approval)
    assert fake.calls[-1][0] == "chat_update" and "Sam Reyes" in json.dumps(fake.calls[-1][1]["blocks"])
