"""The spend policy: plain rules, no network, no model."""

import dataclasses

import pytest

from rro.governance.policy import Policy
from rro.governance.spend import (
    PaymentRequest,
    SpendPolicyError,
    SpendRules,
    authorize_payment,
    units_to_usd,
    usd_to_units,
)
from rro.settings import PROJECT_ROOT

SELLER = "0xaa544d7D4b939Ddf68bF3217F57CBE2AbC95A990"
ATTACKER = "0x000000000000000000000000000000000000dEaD"
USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
CENT = 10_000  # $0.01 in USDC units


@pytest.fixture
def rules() -> SpendRules:
    spend = Policy.load(PROJECT_ROOT / "config" / "policy.yaml").spend
    assert spend is not None
    return spend


def ask(amount_units=CENT, *, url="http://127.0.0.1:8402/news/halcyon.example", pay_to=SELLER, **changes):
    request = PaymentRequest(url=url, network="eip155:84532", asset=USDC, pay_to=pay_to, amount_units=amount_units)
    return dataclasses.replace(request, **changes)


# --- loading the rules ------------------------------------------------------------
def test_the_policy_file_loads_in_exact_units(rules):
    assert rules.budget_units == 50_000 and rules.auto_approve_units == CENT
    assert usd_to_units("0.05") == 50_000 and units_to_usd(50_000) == "$0.05"


@pytest.mark.parametrize("network", ["eip155:8453", "eip155:1", "solana:mainnet", None])
def test_a_mainnet_or_missing_network_refuses_to_load(network):
    with pytest.raises(SpendPolicyError, match="testnet-only"):
        SpendRules.from_config({"network": network, "asset": USDC, "budget_per_run_usd": "1", "auto_approve_up_to_usd": "1"})


@pytest.mark.parametrize("value", [0.05, "-1", "0.0000001", "five"])
def test_dollar_amounts_must_be_exact(value):
    with pytest.raises(SpendPolicyError):
        usd_to_units(value)


def test_no_payments_section_means_no_spend_rules(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text("systems: {}\n", encoding="utf-8")
    assert Policy.load(path).spend is None


# --- what gets refused ---------------------------------------------------------------
def test_a_seller_cannot_redirect_payment_to_another_address(rules):
    decision = authorize_payment(ask(pay_to=ATTACKER), 0, rules)
    assert not decision.allowed and "not its allowlisted address" in decision.reason


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example/news/halcyon.example",
        "http://127.0.0.1:84021/news/halcyon.example",  # lookalike port
        "http://127.0.0.1:8402.evil.example/news/x",  # lookalike host
        "https://127.0.0.1:8402/news/halcyon.example",  # different scheme
        "http://127.0.0.1:8402/admin/pay",  # same seller, path we didn't list
        "http://127.0.0.1:8402/news/../admin/pay",  # passes a prefix check, then becomes /admin/pay
        "http://127.0.0.1:8402/news/x?leak=secret",  # a query string carries extra data out
        "http://user@127.0.0.1:8402/news/x",  # a login part
    ],
)
def test_only_allowlisted_sellers_are_paid(rules, url):
    assert not authorize_payment(ask(url=url), 0, rules).allowed


def test_mainnet_wrong_token_and_zero_are_refused(rules):
    assert not authorize_payment(ask(network="eip155:8453"), 0, rules).allowed
    assert not authorize_payment(ask(asset=ATTACKER), 0, rules).allowed
    assert not authorize_payment(ask(0), 0, rules).allowed


def test_addresses_match_regardless_of_letter_case(rules):
    assert authorize_payment(ask(pay_to=SELLER.lower()), 0, rules).allowed


# --- threshold: policy or a human ------------------------------------------------------
def test_at_or_under_the_threshold_is_auto_approved(rules):
    decision = authorize_payment(ask(CENT), 0, rules)
    assert decision.allowed and not decision.needs_human


def test_over_the_threshold_needs_a_human(rules):
    decision = authorize_payment(ask(CENT + 1), 0, rules)
    assert decision.allowed and decision.needs_human


# --- budget (your piece in spend.py makes these pass) -------------------------------
def test_spending_up_to_exactly_the_budget_is_allowed(rules):
    assert authorize_payment(ask(CENT), 40_000, rules).allowed  # $0.04 committed + $0.01 = $0.05 budget


def test_going_over_the_budget_is_refused(rules):
    decision = authorize_payment(ask(CENT), 40_001, rules)
    assert not decision.allowed and "over its $0.05 budget" in decision.reason


def test_a_human_cannot_approve_past_the_budget(rules):
    big = ask(60_000)  # over the threshold (needs a human) AND over the whole budget
    decision = authorize_payment(big, 0, rules)
    assert not decision.allowed and not decision.needs_human
