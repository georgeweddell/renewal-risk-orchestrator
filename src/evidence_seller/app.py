"""The seller: one paid route, GET /news/{domain}, at $0.01 a call on Base Sepolia.

The x402 middleware sits in front of the route. A request without a payment gets
a 402 and a PAYMENT-REQUIRED header (the price list). A request with a valid
PAYMENT-SIGNATURE is verified and settled through the facilitator, and the
response carries the receipt in PAYMENT-RESPONSE. The route itself never sees
an unpaid request.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException
from x402 import x402ResourceServer
from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme

from evidence_seller.news import NEWS

PORT = 8402
NETWORK = "eip155:84532"  # Base Sepolia. Testnet only, and not configurable on purpose.
FACILITATOR_URL = "https://x402.org/facilitator"  # the public testnet facilitator; no API key
PRICE = "$0.01"

# Read only the one value the seller needs. Loading the whole .env would hand the
# buyer's private key to this process too, and a seller has no business holding it.
PAY_TO = dotenv_values(Path(__file__).resolve().parents[2] / ".env").get("X402_SELLER_ADDRESS")
if not PAY_TO:
    raise SystemExit("X402_SELLER_ADDRESS is not set in .env (run learning/make_test_wallet.py).")

server = x402ResourceServer(HTTPFacilitatorClient(FacilitatorConfig(url=FACILITATOR_URL)))
server.register(NETWORK, ExactEvmServerScheme())

routes = {
    "GET /news/*": RouteConfig(
        accepts=PaymentOption(scheme="exact", pay_to=PAY_TO, price=PRICE, network=NETWORK),
        description="Recent news headlines for a company domain",
        mime_type="application/json",
    ),
}

app = FastAPI(title="Evidence seller (testnet)")
app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)


@app.get("/news/{domain}")
async def news(domain: str) -> dict:
    if domain not in NEWS:
        # A 4xx here means the middleware cancels settlement: no data, no charge.
        raise HTTPException(404, f"No news on file for {domain}")
    return {"domain": domain, "articles": NEWS[domain]}
