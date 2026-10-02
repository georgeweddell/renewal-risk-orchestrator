"""Where evidence is bought: a simulated seller (mock) or a real x402 seller on a testnet (x402).

Both run the same check_terms() before "paying", so the rules can be demonstrated
without a wallet. Only the x402 backend touches a key, and only this process
ever receives it (see config/servers.yaml).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from types import SimpleNamespace
from typing import Any, Protocol

from evidence_seller.news import NEWS
from mcp_servers.evidence.terms import ApprovedTerms, check_terms

BASE_SEPOLIA = "eip155:84532"
USDC_BASE_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"


class EvidenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Purchase:
    url: str
    data: Any  # what the seller returned: outside text, untrusted
    paid_units: int
    pay_to: str
    network: str
    transaction: str | None  # the settlement's transaction hash
    simulated: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Backend(Protocol):
    async def buy(self, url: str, approved: ApprovedTerms) -> Purchase: ...


class MockBackend:
    """Plays the allowlisted seller: $0.01 in test USDC, to the approved address. No network, no key."""

    PRICE_UNITS = 10_000

    async def buy(self, url: str, approved: ApprovedTerms) -> Purchase:
        domain = url.rstrip("/").rsplit("/", 1)[-1]
        quote = SimpleNamespace(
            scheme="exact", network=BASE_SEPOLIA, asset=USDC_BASE_SEPOLIA,
            amount=str(self.PRICE_UNITS), pay_to=approved.pay_to,
        )  # fmt: skip
        option = check_terms([quote], approved)
        if domain not in NEWS:  # like the real seller: no data, no charge
            raise EvidenceError(f"The seller has no news for {domain}. Nothing was paid.")
        return Purchase(
            url=url,
            data={"domain": domain, "articles": NEWS[domain]},
            paid_units=int(option.amount),
            pay_to=option.pay_to,
            network=option.network,
            transaction="0xsimulated" + hashlib.sha256(url.encode()).hexdigest()[:54],
            simulated=True,
        )


class X402Backend:
    """Pays a real x402 seller on a testnet, with the buyer key from this process's environment."""

    def __init__(self) -> None:
        # Imported here so the default install (no `payments` extra) never needs the SDK.
        from eth_account import Account
        from x402 import x402Client
        from x402.http import x402HTTPClient
        from x402.mechanisms.evm import EthAccountSigner
        from x402.mechanisms.evm.exact import ExactEvmScheme

        key = os.environ.get("X402_BUYER_PRIVATE_KEY")
        if not key:
            raise EvidenceError("X402_BUYER_PRIVATE_KEY isn't set for the evidence server.")
        signer = EthAccountSigner(Account.from_key(key))
        del key
        self._client = x402Client()
        # One testnet only, not the SDK's "eip155:*" wildcard (whose helper also registers every legacy v1 network).
        self._client.register(BASE_SEPOLIA, ExactEvmScheme(signer))
        self._http = x402HTTPClient(self._client)

    async def buy(self, url: str, approved: ApprovedTerms) -> Purchase:
        import httpx

        # No redirects: an approved URL must not be able to bounce the request (and its data) elsewhere.
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as http:
            first = await http.get(url)
            if first.status_code != 402:
                raise EvidenceError(f"Expected the seller to ask for payment (402), got {first.status_code}. Nothing was paid.")
            required = self._http.get_payment_required_response(first.headers.get, _json_or_none(first))

            option = check_terms(required.accepts, approved)  # raises before anything is signed
            narrowed = required.model_copy(update={"accepts": [option]})  # the SDK may only pay this option
            payload = await self._client.create_payment_payload(narrowed)
            paid = await http.get(url, headers=self._http.encode_payment_signature_header(payload))

        receipt = _decode(paid.headers.get("payment-response"))
        if paid.status_code != 200 or not receipt.get("success"):
            reason = receipt.get("errorReason") or receipt.get("error_reason") or "no receipt"
            raise EvidenceError(f"The seller answered {paid.status_code} ({reason}). Treat as not bought.")
        return Purchase(
            url=url,
            data=_json_or_none(paid) or paid.text[:5000],
            paid_units=int(option.amount),
            pay_to=option.pay_to,
            network=option.network,
            transaction=receipt.get("transaction"),
            simulated=False,
        )


def _decode(header: str | None) -> dict[str, Any]:
    """An x402 header is base64-encoded JSON."""
    return json.loads(base64.b64decode(header)) if header else {}


def _json_or_none(response: Any) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def load_backend() -> Backend:
    backend = os.environ.get("EVIDENCE_BACKEND", "mock")
    if backend == "mock":
        return MockBackend()
    if backend == "x402":
        return X402Backend()
    raise EvidenceError(f"EVIDENCE_BACKEND must be 'mock' or 'x402', not {backend!r}")
