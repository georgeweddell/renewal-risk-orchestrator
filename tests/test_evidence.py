"""The paid-evidence executor: the terms check, the mock seller, and the gateway path. No network, no wallet."""

import json
from types import SimpleNamespace

import pytest

from rro.db import Store
from rro.governance.gateway import ToolGateway
from rro.governance.policy import Policy
from rro.settings import Settings

from mcp_servers.evidence.backends import MockBackend, EvidenceError
from mcp_servers.evidence.terms import ApprovedTerms, TermsMismatch, check_terms

SELLER = "0xaa544d7D4b939Ddf68bF3217F57CBE2AbC95A990"
ATTACKER = "0x000000000000000000000000000000000000dEaD"
USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
URL = "http://127.0.0.1:8402/news/halcyonrobotics.example"
APPROVED = ApprovedTerms(pay_to=SELLER, network="eip155:84532", asset=USDC, max_amount_units=10_000)
BUY_ARGS = {"url": URL, "pay_to": SELLER, "network": "eip155:84532", "asset": USDC, "max_amount_units": 10_000}


def option(**changes):
    base = {"scheme": "exact", "network": "eip155:84532", "asset": USDC, "amount": "10000", "pay_to": SELLER}
    return SimpleNamespace(**(base | changes))


# --- the seller's live terms against the approved ones ------------------------------
def test_matching_terms_are_accepted():
    assert check_terms([option()], APPROVED).pay_to == SELLER
    assert check_terms([option(amount="5000")], APPROVED).amount == "5000"  # cheaper than approved is fine
    assert check_terms([option(pay_to=SELLER.lower())], APPROVED)  # address case is only a checksum


@pytest.mark.parametrize(
    "changed",
    [
        {"pay_to": ATTACKER},  # the seller swapped the payee after approval
        {"amount": "10001"},  # the price went up
        {"network": "eip155:8453"},  # mainnet
        {"asset": ATTACKER},  # a different token
        {"scheme": "upto"},  # a scheme we never approved
        {"amount": "0"},
    ],
)
def test_anything_else_is_refused_before_signing(changed):
    with pytest.raises(TermsMismatch, match="Nothing was signed"):
        check_terms([option(**changed)], APPROVED)


def test_the_matching_option_is_picked_from_several():
    chosen = check_terms([option(pay_to=ATTACKER), option(network="eip155:8453"), option()], APPROVED)
    assert chosen.pay_to == SELLER and chosen.network == "eip155:84532"


def test_an_approval_for_mainnet_is_refused_by_the_executor_itself():
    with pytest.raises(TermsMismatch, match="isn't a testnet"):
        check_terms([option(network="eip155:8453")], ApprovedTerms(SELLER, "eip155:8453", USDC, 10_000))


# --- the mock seller -------------------------------------------------------------------
async def test_the_mock_seller_sells_news_with_a_simulated_receipt():
    purchase = await MockBackend().buy(URL, APPROVED)
    assert purchase.simulated and purchase.paid_units == 10_000 and purchase.transaction.startswith("0xsimulated")
    assert "hiring freeze" in json.dumps(purchase.data)


async def test_the_mock_seller_charges_nothing_for_an_unknown_domain():
    with pytest.raises(EvidenceError, match="Nothing was paid"):
        await MockBackend().buy("http://127.0.0.1:8402/news/nobody.example", APPROVED)


async def test_the_mock_seller_respects_the_approved_maximum():
    with pytest.raises(TermsMismatch):
        await MockBackend().buy(URL, ApprovedTerms(SELLER, "eip155:84532", USDC, max_amount_units=9_999))


# --- the flag ----------------------------------------------------------------------------
def test_payments_are_off_by_default():
    s = Settings(_env_file=None)
    assert not s.rro_payments_enabled and "evidence" not in s.systems


def test_the_evidence_backend_follows_the_mode():
    assert Settings(_env_file=None, rro_payments_enabled=True).backend_for("evidence") == "mock"
    assert Settings(_env_file=None, rro_mode="live").backend_for("evidence") == "x402"
    assert Settings(_env_file=None, rro_mode="live", evidence_backend="mock").backend_for("evidence") == "mock"


# --- through the real gateway (mock seller) -------------------------------------------------
def paying_gateway(settings: Settings, store: Store, approved: set[str] = frozenset()) -> ToolGateway:
    def check(approval_id, system, tool, args):
        return approval_id in approved and args == BUY_ARGS

    settings = settings.model_copy(update={"rro_payments_enabled": True})
    return ToolGateway(settings, Policy.load(settings.config_dir / "policy.yaml", approval_check=check), store)


async def test_buying_is_hidden_from_the_model_and_only_the_executor_can_pay(settings, store):
    async with paying_gateway(settings, store, approved={"appr-pay"}) as gateway:
        assert "evidence__buy_evidence" in {s.qualified_name for s in gateway.inventory()}
        assert "evidence__buy_evidence" not in {t["name"] for t in gateway.model_tools()}

        by_agent = await gateway.call("evidence__buy_evidence", BUY_ARGS, run_id="p1", actor="agent")
        unapproved = await gateway.call("evidence__buy_evidence", BUY_ARGS, run_id="p1", actor="executor")
        tampered = await gateway.call(
            "evidence__buy_evidence", BUY_ARGS | {"pay_to": ATTACKER}, run_id="p1", actor="executor", approval_id="appr-pay"
        )
        paid = await gateway.call("evidence__buy_evidence", BUY_ARGS, run_id="p1", actor="executor", approval_id="appr-pay")

    assert by_agent.text.startswith("Denied by policy") and unapproved.text.startswith("Denied by policy")
    assert tampered.text.startswith("Denied by policy")  # the payload no longer matches what was approved
    assert not paid.is_error and json.loads(paid.text)["transaction"].startswith("0xsimulated")
    assert [r["decision"] for r in store.audit_for_run("p1")] == ["denied", "denied", "denied", "allowed"]


async def test_with_payments_off_the_evidence_server_is_not_started(settings, store):
    async with ToolGateway(settings, Policy.load(settings.config_dir / "policy.yaml"), store) as gateway:
        assert not any(s.system == "evidence" for s in gateway.inventory())
