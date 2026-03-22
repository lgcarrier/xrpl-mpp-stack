from __future__ import annotations

from fastapi import FastAPI, Request

from xrpl_mpp_core import getenv_clean
from xrpl_mpp_middleware import PaymentMiddlewareASGI, require_payment

DEFAULT_FACILITATOR_URL = "http://127.0.0.1:8000"
DEFAULT_FACILITATOR_TOKEN = "replace-with-your-facilitator-token"
DEFAULT_MERCHANT_XRPL_ADDRESS = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
DEFAULT_XRPL_NETWORK = "xrpl:1"
DEFAULT_PRICE_DROPS = 1000
DEFAULT_MPP_CHALLENGE_SECRET = "replace-with-a-long-random-secret"
DEFAULT_MPP_REALM = "merchant.example"


def setting(name: str, default: str) -> str:
    return getenv_clean(name, default) or default


def create_app(*, client_factory=None) -> FastAPI:
    app = FastAPI(title="XRPL MPP Minimal Seller Example")
    middleware_kwargs = {}
    if client_factory is not None:
        middleware_kwargs["client_factory"] = client_factory

    app.add_middleware(
        PaymentMiddlewareASGI,
        route_configs={
            "GET /premium": require_payment(
                facilitator_url=setting("FACILITATOR_URL", DEFAULT_FACILITATOR_URL),
                bearer_token=setting("FACILITATOR_TOKEN", DEFAULT_FACILITATOR_TOKEN),
                pay_to=setting("MERCHANT_XRPL_ADDRESS", DEFAULT_MERCHANT_XRPL_ADDRESS),
                network=setting("XRPL_NETWORK", DEFAULT_XRPL_NETWORK),
                xrp_drops=int(setting("PRICE_DROPS", str(DEFAULT_PRICE_DROPS))),
                description="One premium XRPL MPP request",
            )
        },
        challenge_secret=setting("MPP_CHALLENGE_SECRET", DEFAULT_MPP_CHALLENGE_SECRET),
        default_realm=setting("MPP_DEFAULT_REALM", DEFAULT_MPP_REALM),
        **middleware_kwargs,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/premium")
    async def premium(request: Request) -> dict[str, str]:
        receipt = request.state.mpp_payment
        return {
            "message": "premium content unlocked",
            "payer": receipt.payer or "",
            "tx_hash": receipt.tx_hash or "",
        }

    return app


app = create_app()
