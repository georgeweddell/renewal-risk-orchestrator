"""The approval gate, the executor, proposal validation and decision memory."""

import json

import pytest
from fastapi.testclient import TestClient

from rro.agent.local_tools import LocalTools
from rro.governance.approvals import ApprovalError
from rro.risk import RiskSignals, score

HALCYON, RENEWAL_DEAL, ORIGINAL_DEAL = "halcyon-robotics", "5014", "5013"
SUMMIT_RENEWAL_DEAL = "5016"
RISK_PROPS = {"renewal_risk_level": "critical", "renewal_risk_score": "90", "renewal_risk_reason": "test"}


def halcyon_assessment(rt):
    return score(RiskSignals(usage_pct_change=-45, open_p1=2, open_p2=0, days_to_renewal=28), rt.risk_config)


def propose(rt, properties=RISK_PROPS, run_id="run-1"):
    return rt.approvals.create(
        run_id=run_id, account_slug=HALCYON, account_name="Halcyon Robotics", action_type="crm_risk_update",
        system="crm", tool="manage_crm_objects",
        args={"objectType": "deals", "objectId": RENEWAL_DEAL, "properties": dict(properties)},
        summary="Set renewal risk on deal 5014", rationale="test", assessment=halcyon_assessment(rt),
    )  # fmt: skip


async def deal_properties(gateway, deal_id=RENEWAL_DEAL):
    outcome = await gateway.call(
        "crm__get_crm_objects",
        {"objectType": "deals", "objectIds": [deal_id], "properties": list(RISK_PROPS)},
        run_id=None,
        actor="system",
    )
    return json.loads(outcome.text)["results"][0]["properties"]


async def test_approval_runs_exactly_the_approved_write(rt):
    approval = propose(rt)
    async with rt.gateway as gateway:
        assert (await deal_properties(gateway))["renewal_risk_level"] is None
        result = await rt.approval_service().approve(approval.id, by="Priya Shah")
        assert result.status == "executed" and result.decided_by == "Priya Shah"
        assert await deal_properties(gateway) == RISK_PROPS

        # The same approval can't be used twice.
        replay = await gateway.call(
            "crm__manage_crm_objects", approval.args, run_id="run-1", actor="executor", approval_id=approval.id
        )
        assert replay.decision.allowed is False

    audit = [(r["actor"], r["system"], r["tool"], r["decision"], r["approval_id"]) for r in rt.store.audit_for_run("run-1")]
    assert audit[0] == ("human:Priya Shah", "approvals", "approve", "allowed", approval.id)
    assert audit[1] == ("executor", "crm", "manage_crm_objects", "allowed", approval.id)
    assert audit[2] == ("system", "memory", "record_decision", "allowed", approval.id)
    assert audit[3][3] == "denied"

    latest = rt.memory.for_account(HALCYON)[0]
    assert latest.source == "run:run-1" and latest.status == "approved" and latest.outcome == "pending"


async def test_a_tampered_payload_is_refused(rt):
    approval = propose(rt)
    tampered = {**approval.args, "properties": {**RISK_PROPS, "renewal_risk_level": "healthy"}}
    rt.store.conn.execute("UPDATE approvals SET args_json=? WHERE id=?", (json.dumps(tampered), approval.id))

    async with rt.gateway as gateway:
        result = await rt.approval_service().approve(approval.id, by="Priya Shah")
        assert result.status == "failed" and "Denied by policy" in result.result_text
        assert (await deal_properties(gateway))["renewal_risk_level"] is None
    # A failed write isn't remembered as a decision that happened.
    assert all(d.source == "seed" for d in rt.memory.for_account(HALCYON))


async def test_rejection_needs_a_reason_and_is_remembered(rt):
    approval = propose(rt)
    with pytest.raises(ApprovalError, match="needs a reason"):
        rt.approval_service().reject(approval.id, by="Sam Reyes", note="  ")
    rt.approval_service().reject(approval.id, by="Sam Reyes", note="Wait for the P1 fixes before flagging.")
    with pytest.raises(ApprovalError, match="already rejected"):
        rt.approval_service().reject(approval.id, by="Sam Reyes", note="again")

    # The next run sees it through the memory MCP server.
    async with rt.gateway as gateway:
        history = json.loads((await gateway.call("memory__get_account_history", {"account_id": HALCYON}, run_id=None)).text)
    newest = history["decisions"][0]
    assert newest["status"] == "rejected" and newest["note"] == "Wait for the P1 fixes before flagging."


@pytest.mark.parametrize("decide", ["approve", "reject"])
async def test_memory_keeps_no_model_written_text(rt, decide):
    # The reason is model-written and could echo a ticket; memory must not carry it into future runs.
    approval = propose(rt, properties={**RISK_PROPS, "renewal_risk_reason": "MODEL-TEXT-CANARY"})
    async with rt.gateway:
        if decide == "approve":
            await rt.approval_service().approve(approval.id, by="Priya Shah")
        else:
            rt.approval_service().reject(approval.id, by="Priya Shah", note="Not yet.")

    latest = rt.memory.for_account(HALCYON)[0]
    assert latest.source == "run:run-1"
    assert latest.proposal == "Set renewal_risk_level = critical, renewal_risk_score = 90 on the renewal deal"
    assert "MODEL-TEXT-CANARY" not in json.dumps(latest.to_dict())

    # Every memory write is in the audit log, tied to the human decision behind it.
    writes = [r for r in rt.store.audit_for_run("run-1") if r["system"] == "memory"]
    assert [(w["tool"], w["approval_id"]) for w in writes] == [("record_decision", approval.id)]


async def test_similar_decisions_surface_the_relevant_precedent(rt):
    async with rt.gateway as gateway:
        outcome = await gateway.call(
            "memory__find_similar_decisions",
            {"drivers": ["usage_drop", "open_p1", "renewal_soon"], "band": "critical", "exclude_account": HALCYON},
            run_id=None,
        )
    matches = json.loads(outcome.text)["matches"]
    assert {m["account_slug"] for m in matches[:2]} == {"quarry-systems", "larkspur-health"}
    assert all(m["account_slug"] != HALCYON for m in matches)


@pytest.mark.parametrize(
    ("tool", "args", "error"),
    [
        ("propose_crm_update", {"deal_id": SUMMIT_RENEWAL_DEAL, "reason": "x"}, "doesn't belong"),
        ("propose_crm_update", {"deal_id": ORIGINAL_DEAL, "reason": "x"}, "is closed"),
        ("propose_crm_update", {"deal_id": "999999", "reason": "x"}, "doesn't exist"),
        ("propose_crm_update", {"deal_id": RENEWAL_DEAL, "reason": "x" * 301}, "300 characters"),
        ("propose_pricing_exception", {"deal_id": RENEWAL_DEAL, "discount_pct": 40, "rationale": "r", "conditions": "c"}, "policy limit"),
    ],
)
async def test_invalid_proposals_are_refused(rt, tool, args, error):
    async with rt.gateway as gateway:
        tools = LocalTools(rt.settings, rt.risk_config, rt.store, gateway, rt.approvals, "run-1", "test")
        await tools.call("score_renewal_risk", {"account_slug": HALCYON, "usage_pct_change": -45, "open_p1": 2, "open_p2": 0, "renewal_date": "2026-10-26"})
        text, is_error = await tools.call(tool, {"account_slug": HALCYON, **args})
    assert is_error and error in text
    assert rt.approvals.list() == []


async def test_valid_pricing_proposal_is_queued_not_written(rt):
    async with rt.gateway as gateway:
        tools = LocalTools(rt.settings, rt.risk_config, rt.store, gateway, rt.approvals, "run-1", "test")
        await tools.call("score_renewal_risk", {"account_slug": HALCYON, "usage_pct_change": -45, "open_p1": 2, "open_p2": 0, "renewal_date": "2026-10-26"})
        text, is_error = await tools.call(
            "propose_pricing_exception",
            {"account_slug": HALCYON, "deal_id": RENEWAL_DEAL, "discount_pct": 10, "rationale": "r", "conditions": "Adoption plan"},
        )
        props = await deal_properties(gateway)
    assert not is_error and json.loads(text)["status"] == "pending_human_approval"
    assert props["renewal_risk_level"] is None  # nothing written
    (approval,) = rt.approvals.list(status="pending")
    assert approval.args["properties"]["pricing_exception_pct"] == "10"


def test_web_ui_approval_flow(rt, monkeypatch):
    import rro.web.app as web

    approval = propose(rt)
    monkeypatch.setattr(web, "get_settings", lambda: rt.settings)
    with TestClient(web.app) as client:
        for page in ("/", "/approvals", "/memory", "/audit"):
            assert client.get(page).status_code == 200, page
        assert approval.id in client.get("/approvals").text

        rejected = client.post(f"/approvals/{approval.id}/decide", data={"action": "reject", "approver": "Sam", "note": ""})
        assert rejected.status_code == 400

        done = client.post(
            f"/approvals/{approval.id}/decide",
            data={"action": "approve", "approver": "Priya Shah", "note": "", "next_url": "/approvals"},
            follow_redirects=False,
        )
        assert done.status_code == 303 and done.headers["location"] == "/approvals"
        assert "executed" in client.get("/approvals").text
    assert rt.approvals.get(approval.id).status == "executed"
