from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
import httpx
from slowapi import Limiter as SlowLimiter
from xrpl.wallet import Wallet

import xrpl_mpp_facilitator.factory as factory_module
from xrpl_mpp_client import XRPLPaymentSigner, decode_payment_receipt_header, wrap_httpx_with_mpp_payment
from xrpl_mpp_core import FacilitatorSupportedMethod, PaymentReceipt, XRPLAsset
from xrpl_mpp_facilitator.config import Settings
from xrpl_mpp_facilitator.factory import create_app
from xrpl_mpp_middleware import XRPLFacilitatorClient
from xrpl_mpp_middleware.middleware import PaymentMiddlewareASGI, require_payment

FACILITATOR_TOKEN = "local-facilitator-token"
DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
PAYER = "rLOCALPAYER123456789"
TX_HASH = "LOCAL-TX-HASH-123"
CHALLENGE_SECRET = "integration-test-secret"


class RecordingXRPLService:
    def __init__(self) -> None:
        self.charge_calls = []

    def supported_methods(self):
        return [
            FacilitatorSupportedMethod(
                method="xrpl",
                intents=["charge", "session"],
                network="xrpl:1",
                assets=[XRPLAsset(code="XRP")],
                settlementMode="validated",
            )
        ]

    async def charge(self, credential) -> PaymentReceipt:
        self.charge_calls.append(credential)
        return PaymentReceipt(
            method="xrpl",
            timestamp="2026-03-21T12:00:00Z",
            reference=TX_HASH,
            challengeId=credential.challenge.id,
            intent="charge",
            network="xrpl:1",
            payer=PAYER,
            recipient=DESTINATION,
            invoiceId="A" * 32,
            txHash=TX_HASH,
            settlementStatus="validated",
            asset={"code": "XRP"},
            amount={"value": "1000", "unit": "drops", "asset": {"code": "XRP"}, "drops": 1000},
        )

    async def session(self, credential) -> PaymentReceipt:
        raise AssertionError("session should not be used in this integration test")


def _create_app_with_in_memory_rate_limiter(
    *,
    app_settings: Settings,
    xrpl_service: RecordingXRPLService,
) -> FastAPI:
    original_build_rate_limiter = factory_module.build_rate_limiter

    def _build_in_memory_rate_limiter(_settings: Settings):
        return SlowLimiter(key_func=factory_module.get_remote_address)

    factory_module.build_rate_limiter = _build_in_memory_rate_limiter
    try:
        return create_app(
            app_settings=app_settings,
            xrpl_service=xrpl_service,
        )
    finally:
        factory_module.build_rate_limiter = original_build_rate_limiter


def test_middleware_uses_real_local_facilitator_instance() -> None:
    facilitator_service = RecordingXRPLService()
    facilitator_settings = Settings(
        _env_file=None,
        XRPL_RPC_URL="https://s.altnet.rippletest.net:51234",
        MY_DESTINATION_ADDRESS=DESTINATION,
        REDIS_URL="redis://fake:6379/0",
        NETWORK_ID="xrpl:1",
        SETTLEMENT_MODE="validated",
        FACILITATOR_BEARER_TOKEN=FACILITATOR_TOKEN,
        MPP_CHALLENGE_SECRET=CHALLENGE_SECRET,
    )
    facilitator_app = _create_app_with_in_memory_rate_limiter(
        app_settings=facilitator_settings,
        xrpl_service=facilitator_service,
    )

    middleware_app = FastAPI()

    @middleware_app.get("/paid")
    async def paid(request: Request) -> dict[str, str]:
        payment = request.state.mpp_payment
        return {
            "reference": payment.reference,
            "tx_hash": payment.tx_hash or "",
            "payer": payment.payer or "",
        }

    async_facilitator_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=facilitator_app),
        base_url="http://facilitator.local",
    )

    middleware_app.add_middleware(
        PaymentMiddlewareASGI,
        route_configs={
            "GET /paid": require_payment(
                facilitator_url="http://facilitator.local",
                bearer_token=FACILITATOR_TOKEN,
                pay_to=DESTINATION,
                network="xrpl:1",
                xrp_drops=1000,
                description="Local facilitator integration route",
            )
        },
        challenge_secret=CHALLENGE_SECRET,
        client_factory=lambda facilitator_url, bearer_token: XRPLFacilitatorClient(
            base_url=facilitator_url,
            bearer_token=bearer_token,
            async_client=async_facilitator_client,
        ),
    )

    signer = XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
        autofill_enabled=False,
    )

    async def _make_paid_request() -> httpx.Response:
        transport = httpx.ASGITransport(app=middleware_app)
        async with wrap_httpx_with_mpp_payment(
            signer,
            transport=transport,
            base_url="http://merchant.local",
            asset="XRP:native",
        ) as client:
            response = await client.get("/paid")
        await async_facilitator_client.aclose()
        return response

    response = asyncio.run(_make_paid_request())

    assert response.status_code == 200
    assert response.json() == {
        "reference": TX_HASH,
        "tx_hash": TX_HASH,
        "payer": PAYER,
    }
    payment_receipt = decode_payment_receipt_header(response.headers)
    assert payment_receipt is not None
    assert payment_receipt.tx_hash == TX_HASH
    assert len(facilitator_service.charge_calls) == 1
