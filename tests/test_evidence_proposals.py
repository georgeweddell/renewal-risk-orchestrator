"""Proposing a purchase: refused, approved by policy and bought, or queued for a human. Mock seller, no wallet."""

import json

import pytest
import yaml

from rro.agent.local_tools import LocalTools
from rro.governance.policy import Policy
from rro.governance.trifecta import assess
from rro.runtime import build_runtime

HALCYON = "halcyon-robotics"
SCORE = {"account_slug": HALCYON, "usage_pct_change": -45, "open_p1": 2, "open_p2": 0, "renewal_date": "2026-10-26"}
FIRST, SECOND = "Evidence seller (local, testnet)", "Second seller (testnet)"


def paying_runtime(settings, **payments):
    """Payments on (mock seller), with the policy's payments section adjusted, plus a second seller."""
    path = settings.config_dir / "policy.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["payments"] |= payments
    raw["payments"]["payees"].append(raw["payments"]["payees"][0] | {"name": SECOND, "url": "http://127.0.0.1:8403/news/"})
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return build_runtime(settings.model_copy(update={"rro_payments_enabled": True}))


def tools_for(rt, gateway, run_id="run-1"):
    return LocalTools(rt.settings, rt.risk_config, rt.store, gateway, rt.approvals, run_id, "test", service=rt.approval_service())


async def buy(tools, seller=FIRST):
    return await tools.call("propose_evidence_purchase", {"account_slug": HALCYON, "seller": seller, "reason": "Check for layoffs"})


async def test_the_tool_is_only_offered_when_payments_are_on(rt):
    async with rt.gateway as gateway:
        off = LocalTools(rt.settings, rt.risk_config, rt.store, gateway, rt.approvals, "r", "test", service=rt.approval_service())
        assert "propose_evidence_purchase" not in off.names
        text, is_error = await off.call("propose_evidence_purchase", {})
    assert is_error and rt.approvals.list() == []


async def test_a_small_purchase_is_approved_by_policy_and_bought(settings):
    rt = paying_runtime(settings)
    async with rt.gateway as gateway:
        tools = tools_for(rt, gateway)
        await tools.call("score_renewal_risk", SCORE)
        text, is_error = await buy(tools)

    assert not is_error and '"approved_by": "policy"' in text
    assert '<tool_output source="evidence" trust="untrusted">' in text and "hiring freeze" in text
    (approval,) = rt.approvals.list()
    assert approval.status == "executed" and approval.decided_by == "policy"
    assert approval.args["url"] == "http://127.0.0.1:8402/news/halcyonrobotics.example"  # the CRM's domain, not Claude's
    assert tools.proposed == []  # nothing left for a human
    audit = [(r["actor"], r["tool"], r["decision"]) for r in rt.store.audit_for_run("run-1")]
    assert ("policy", "approve", "allowed") in audit and ("executor", "buy_evidence", "allowed") in audit


async def test_over_the_threshold_waits_for_a_human_then_buys(settings):
    rt = paying_runtime(settings, auto_approve_up_to_usd="0")
    async with rt.gateway as gateway:
        tools = tools_for(rt, gateway)
        await tools.call("score_renewal_risk", SCORE)
        text, is_error = await buy(tools)
        assert not is_error and json.loads(text)["status"] == "pending_human_approval"
        (pending,) = rt.approvals.list(status="pending")
        assert not any(r["tool"] == "buy_evidence" for r in rt.store.audit_for_run("run-1"))  # nothing bought yet

        done = await rt.approval_service().approve(pending.id, by="Priya Shah")
    assert done.status == "executed" and done.decided_by == "Priya Shah" and "hiring freeze" in done.result_text


async def test_over_the_budget_is_refused(settings):
    rt = paying_runtime(settings, budget_per_run_usd="0.005")
    async with rt.gateway as gateway:
        tools = tools_for(rt, gateway)
        await tools.call("score_renewal_risk", SCORE)
        text, is_error = await buy(tools)
    assert is_error and "over its $0.005 budget" in text and rt.approvals.list() == []


async def test_the_same_purchase_is_not_made_twice(settings):
    rt = paying_runtime(settings)
    async with rt.gateway as gateway:
        tools = tools_for(rt, gateway)
        await tools.call("score_renewal_risk", SCORE)
        await buy(tools)
        text, is_error = await buy(tools)
    assert is_error and "Already bought" in text


# --- which statuses count against the budget (your piece) ---------------------------------
async def test_a_bought_purchase_counts_against_the_budget(settings):
    rt = paying_runtime(settings, budget_per_run_usd="0.01")
    async with rt.gateway as gateway:
        tools = tools_for(rt, gateway)
        await tools.call("score_renewal_risk", SCORE)
        first, _ = await buy(tools)
        second, is_error = await buy(tools, SECOND)
    assert '"status": "bought"' in first
    assert is_error and "over its $0.01 budget" in second


async def test_a_pending_purchase_counts_against_the_budget(settings):
    rt = paying_runtime(settings, budget_per_run_usd="0.01", auto_approve_up_to_usd="0")
    async with rt.gateway as gateway:
        tools = tools_for(rt, gateway)
        await tools.call("score_renewal_risk", SCORE)
        await buy(tools)  # waiting for a human: not spent yet, but it could be
        second, is_error = await buy(tools, SECOND)
    assert is_error and "over its $0.01 budget" in second


async def test_a_rejected_purchase_frees_the_budget(settings):
    rt = paying_runtime(settings, budget_per_run_usd="0.01", auto_approve_up_to_usd="0")
    async with rt.gateway as gateway:
        tools = tools_for(rt, gateway)
        await tools.call("score_renewal_risk", SCORE)
        await buy(tools)
        (pending,) = rt.approvals.list(status="pending")
        rt.approval_service().reject(pending.id, by="Priya Shah", note="Not worth it")
        second, is_error = await buy(tools, SECOND)
    assert not is_error and json.loads(second)["status"] == "pending_human_approval"


# --- the payment receipt in the audit log -------------------------------------------------
def receipts(rt, run_id="run-1"):
    return [json.loads(r["args_json"]) | {"reason": r["reason"], "actor": r["actor"]}
            for r in rt.store.audit_for_run(run_id) if r["system"] == "payments"]  # fmt: skip


async def test_every_payment_leaves_one_receipt_with_its_reason(settings):
    rt = paying_runtime(settings)
    async with rt.gateway as gateway:
        tools = tools_for(rt, gateway)
        await tools.call("score_renewal_risk", SCORE)
        await buy(tools)
    (receipt,) = receipts(rt)
    assert receipt["actor"] == "executor" and receipt["reason"] == "Check for layoffs"
    assert receipt["approved_by"] == "policy" and receipt["status"] == "paid"
    assert receipt["paid_usd"] == "$0.01" and receipt["transaction"].startswith("0xsimulated")


async def test_a_failed_purchase_is_receipted_as_nothing_paid(settings):
    rt = paying_runtime(settings)
    async with rt.gateway as gateway:
        tools = tools_for(rt, gateway)
        await tools.call("score_renewal_risk", SCORE | {"account_slug": "brightwave-health"})  # the seller has no news on it
        await tools.call("propose_evidence_purchase", {"account_slug": "brightwave-health", "seller": FIRST, "reason": "x"})
    (receipt,) = receipts(rt)
    assert receipt["status"] == "failed, nothing paid" and receipt["paid_units"] == 0


# --- memory: did the purchase pay off? (your piece in approvals.py makes these pass) ------
def purchases_in_memory(rt):
    return [d for d in rt.memory.all() if d.action_type == "evidence_purchase"]


async def test_a_purchase_is_remembered_in_code_built_words(settings):
    rt = paying_runtime(settings)
    async with rt.gateway as gateway:
        tools = tools_for(rt, gateway)
        await tools.call("score_renewal_risk", SCORE)
        await buy(tools)
    (memory,) = purchases_in_memory(rt)
    assert memory.proposal == "Bought news on halcyonrobotics.example from 127.0.0.1:8402, up to $0.01 (testnet)"
    assert memory.approver == "policy" and memory.outcome == "pending"
    assert "Check for layoffs" not in str(memory) and "hiring freeze" not in str(memory)  # no model or seller text


async def test_a_person_rates_the_purchase_and_later_runs_can_find_it(settings, monkeypatch):
    rt = paying_runtime(settings)
    async with rt.gateway as gateway:
        tools = tools_for(rt, gateway)
        await tools.call("score_renewal_risk", SCORE)
        await buy(tools)
    (memory,) = purchases_in_memory(rt)
    rt.approval_service().rate_evidence(memory.id, "useful", by="Priya Shah", note="The hiring freeze explained the usage drop")
    (rated,) = purchases_in_memory(rt)
    assert rated.outcome == "useful" and rated.outcome_note == "The hiring freeze explained the usage drop"
    assert any(r["actor"] == "human:Priya Shah" and r["tool"] == "record_outcome" for r in rt.store.audit_for_run("run-1"))

    from mcp_servers.memory import server  # what the agent sees through memory__find_similar_decisions

    monkeypatch.setenv("RRO_MEMORY_DB", str(settings.memory_db))
    found = server.find_similar_decisions(drivers=["usage_drop", "open_p1"], action_type="evidence_purchase")
    assert found["matches"][0]["outcome"] == "useful"


async def test_a_rating_needs_a_known_outcome_and_a_reason(settings):
    from rro.governance.approvals import ApprovalError

    rt = paying_runtime(settings)
    async with rt.gateway as gateway:
        tools = tools_for(rt, gateway)
        await tools.call("score_renewal_risk", SCORE)
        await buy(tools)
    (memory,) = purchases_in_memory(rt)
    with pytest.raises(ApprovalError):
        rt.approval_service().rate_evidence(memory.id, "great", by="Priya Shah", note="x")
    with pytest.raises(ApprovalError):
        rt.approval_service().rate_evidence(memory.id, "useful", by="Priya Shah", note="  ")
    with pytest.raises(ApprovalError):  # only purchases are rated this way, not renewal decisions
        rt.approval_service().rate_evidence(purchases_in_memory(rt)[0].id - 1, "useful", by="Priya Shah", note="x")


# --- the trifecta gate ---------------------------------------------------------------------
def test_a_policy_approved_payment_tool_is_only_safe_with_spend_rules(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text("systems:\n  tickets:\n    content: untrusted\n    read: [list_issues]\n", encoding="utf-8")
    from rro.governance.gateway import ToolSpec

    specs = [ToolSpec("tickets", "list_issues", "", {}, "read", True)]
    report = assess(specs, Policy.load(path), LocalTools.effects, ["propose_evidence_purchase"])
    assert not report.safe and report.unapproved_writes == ["propose_evidence_purchase"]
