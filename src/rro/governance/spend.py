"""Spend rules for paid evidence, loaded from the `payments:` section of config/policy.yaml.

The model can only propose a payment. Before anything is signed, the executor
checks the seller's live payment requirements against these rules with
authorize_payment(): plain code, no network, no model. The answer is one of:

  refuse         - breaks a rule; never paid, whoever approves it
  auto-approve   - within every rule and at or under the threshold
  needs a human  - within every rule but over the threshold

Money is counted in atomic units (USDC has 6 decimals: 1 USDC = 1,000,000
units), never floats. The policy file is written in dollars for the people who
own it, and converted here exactly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

USDC_DECIMALS = 6

# Networks payments may ever use. Hard-coded on purpose: the policy file can narrow
# this, never widen it. Anything else, mainnets included, refuses to load.
TESTNETS = {"eip155:84532": "Base Sepolia"}


class SpendPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Payee:
    """A seller we may pay, and the one address it may be paid at."""

    name: str
    url: str  # where its data lives; requests must be under this
    pay_to: str
    max_price_units: int  # the most one purchase from this seller may cost; what an approval allows

    def serves(self, url: str) -> bool:
        """Same scheme and host:port exactly, and the path under ours.

        A plain startswith() would let http://127.0.0.1:84021 or
        http://127.0.0.1:8402.evil.example pass for http://127.0.0.1:8402.
        """
        ours, theirs = urlsplit(self.url), urlsplit(url)
        return (
            (theirs.scheme, theirs.netloc) == (ours.scheme, ours.netloc)
            and theirs.path.startswith(ours.path.rstrip("/") + "/")
        )


@dataclass(frozen=True)
class SpendRules:
    network: str
    asset: str
    budget_units: int  # per run, hard cap, human-approved payments included
    auto_approve_units: int  # at or under this, policy approves; above it, a human must
    payees: tuple[Payee, ...]

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> SpendRules:
        network = raw.get("network")
        if network not in TESTNETS:
            raise SpendPolicyError(
                f"payments.network is {network!r}. Payments are testnet-only: allowed networks are "
                f"{', '.join(f'{k} ({v})' for k, v in TESTNETS.items())}."
            )
        payees = tuple(
            Payee(
                name=str(p["name"]),
                url=str(p["url"]),
                pay_to=str(p["pay_to"]),
                max_price_units=usd_to_units(p["max_price_usd"], f"payees.{p['name']}.max_price_usd"),
            )
            for p in raw.get("payees") or []
        )
        return cls(
            network=network,
            asset=str(raw["asset"]),
            budget_units=usd_to_units(raw["budget_per_run_usd"], "budget_per_run_usd"),
            auto_approve_units=usd_to_units(raw["auto_approve_up_to_usd"], "auto_approve_up_to_usd"),
            payees=payees,
        )


@dataclass(frozen=True)
class PaymentRequest:
    """What a seller's 402 asks for, for one URL."""

    url: str
    network: str
    asset: str
    pay_to: str
    amount_units: int


@dataclass(frozen=True)
class PaymentDecision:
    allowed: bool
    needs_human: bool
    reason: str

    @classmethod
    def refuse(cls, reason: str) -> PaymentDecision:
        return cls(False, False, reason)


def authorize_payment(request: PaymentRequest, committed_units: int, rules: SpendRules) -> PaymentDecision:
    """Check one payment against the rules.

    committed_units is what this run has already paid plus what's pending
    approval, so two proposals can't each fit the budget and together break it.
    """
    if request.network not in TESTNETS or request.network != rules.network:
        return PaymentDecision.refuse(f"network {request.network} isn't the allowed testnet ({rules.network})")
    if not _same_address(request.asset, rules.asset):
        return PaymentDecision.refuse(f"asset {request.asset} isn't the allowed token ({rules.asset})")
    if request.amount_units <= 0:
        return PaymentDecision.refuse("amount must be above zero")
    if not _plain_url(request.url):
        return PaymentDecision.refuse(f"{request.url!r} isn't a plain URL (no ../, query, fragment or login part)")

    payee = next((p for p in rules.payees if p.serves(request.url)), None)
    if payee is None:
        return PaymentDecision.refuse(f"{request.url} isn't from an allowlisted seller")
    if not _same_address(request.pay_to, payee.pay_to):
        return PaymentDecision.refuse(
            f"{payee.name} asked to be paid at {request.pay_to}, not its allowlisted address {payee.pay_to}"
        )

    # The budget is a hard cap, checked before the threshold: a human can't approve past it.
    # Spending exactly the budget is allowed.
    if (committed_units+request.amount_units)>rules.budget_units:
        return PaymentDecision.refuse(
            f"{units_to_usd(request.amount_units)} would take this run to "
            f"{units_to_usd(committed_units + request.amount_units)}, over its {units_to_usd(rules.budget_units)} budget"
        )

    if request.amount_units <= rules.auto_approve_units:
        return PaymentDecision(True, False, f"{units_to_usd(request.amount_units)} to {payee.name}: within policy, auto-approved")
    return PaymentDecision(
        True, True, f"{units_to_usd(request.amount_units)} to {payee.name}: over the {units_to_usd(rules.auto_approve_units)} auto-approve threshold"
    )


def usd_to_units(value: Any, field: str = "amount") -> int:
    """'0.05' -> 50000, exactly. Floats are refused: 0.1 + 0.2 isn't 0.3 in binary."""
    if isinstance(value, float):
        raise SpendPolicyError(f"{field}: write dollar amounts in quotes (\"0.05\"), not as bare numbers")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise SpendPolicyError(f"{field}: {value!r} isn't a dollar amount") from exc
    if amount < 0 or amount.as_tuple().exponent < -USDC_DECIMALS:
        raise SpendPolicyError(f"{field}: {value!r} must be zero or more, with at most {USDC_DECIMALS} decimal places")
    return int(amount.scaleb(USDC_DECIMALS))


def units_to_usd(units: int) -> str:
    return f"${Decimal(units).scaleb(-USDC_DECIMALS).normalize():f}"


_URL_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]+$")


def _plain_url(url: str) -> bool:
    """http(s), a host, and simple path segments only. "/news/../admin" would pass a prefix
    check and then be tidied into "/admin" by the HTTP client, so dot segments are refused."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or "@" in parts.netloc:
        return False
    if parts.query or parts.fragment or "?" in url or "#" in url:
        return False
    segments = [s for s in parts.path.split("/") if s]
    return all(_URL_SEGMENT.match(s) and s not in (".", "..") for s in segments)


def _same_address(a: str, b: str) -> bool:
    # EVM addresses are hex; mixed case is only a checksum, so compare case-insensitively.
    return a.lower() == b.lower()
