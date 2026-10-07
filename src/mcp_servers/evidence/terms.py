"""The executor's last check: does the seller's live 402 match what was approved?

An approval says "up to N units of this token, to this address, on this
network". In x402 the seller names the price and the payee at request time, so
between approval and payment it could ask for more, or to be paid somewhere
else. Nothing is signed unless one of the seller's options fits the approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Checked here as well as in the policy (rro.governance.spend.TESTNETS), on purpose:
# this process holds the key, so it refuses mainnet on its own account.
TESTNETS = {"eip155:84532"}


class TermsMismatch(ValueError):
    pass


@dataclass(frozen=True)
class ApprovedTerms:
    pay_to: str
    network: str
    asset: str
    max_amount_units: int


def check_terms(accepts: list[Any], approved: ApprovedTerms) -> Any:
    """Return the first payment option that fits the approval, or raise TermsMismatch.

    Each option is the SDK's PaymentRequirements (scheme, network, asset, amount, pay_to).
    """
    if approved.network not in TESTNETS:
        raise TermsMismatch(f"{approved.network} isn't a testnet; this executor only pays on {', '.join(TESTNETS)}")
    for option in accepts:
        if (
            option.scheme == "exact"
            and option.network == approved.network
            and option.asset.lower() == approved.asset.lower()
            and option.pay_to.lower() == approved.pay_to.lower()
            and 0 < int(option.amount) <= approved.max_amount_units
        ):
            return option
    asked = "; ".join(f"{o.amount} units of {o.asset} to {o.pay_to} on {o.network}" for o in accepts) or "nothing"
    raise TermsMismatch(
        f"The seller asked for {asked}. Approved: up to {approved.max_amount_units} units of {approved.asset} "
        f"to {approved.pay_to} on {approved.network}. Nothing was signed."
    )
