from __future__ import annotations

from fastapi import FastAPI, Request

from examples._facilitator import allow_insecure_loopback_facilitator
from xrpl_mpp_core import getenv_clean
from xrpl_mpp_middleware import ChargeRouteSpec, PaymentMiddlewareASGI, RouteConfig

DEFAULT_FACILITATOR_URL = "http://127.0.0.1:8000"
DEFAULT_FACILITATOR_TOKEN = "replace-with-your-facilitator-token"
DEFAULT_MERCHANT_XRPL_ADDRESS = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
DEFAULT_XRPL_NETWORK = "testnet"
DEFAULT_PRICE_AMOUNT = "1000"
DEFAULT_MPP_CHALLENGE_SECRET = "replace-with-a-long-random-secret"
DEFAULT_MPP_REALM = "merchant.example"


def setting(name: str, default: str) -> str:
    return getenv_clean(name, default) or default


def build_premium_route_config() -> RouteConfig:
    facilitator_url = setting("FACILITATOR_URL", DEFAULT_FACILITATOR_URL)
    return RouteConfig(
        facilitatorUrl=facilitator_url,
        bearerToken=setting("FACILITATOR_TOKEN", DEFAULT_FACILITATOR_TOKEN),
        allowInsecureFacilitatorHttp=allow_insecure_loopback_facilitator(
            facilitator_url
        ),
        chargeOptions=[
            ChargeRouteSpec(
                recipient=setting(
                    "MERCHANT_XRPL_ADDRESS",
                    DEFAULT_MERCHANT_XRPL_ADDRESS,
                ),
                network=setting("XRPL_NETWORK", DEFAULT_XRPL_NETWORK),
                currency="XRP",
                amount=setting("PRICE_AMOUNT", DEFAULT_PRICE_AMOUNT),
                description="One premium XRPL MPP 0.2 charge",
            )
        ],
    )


def create_app(*, client_factory=None) -> FastAPI:
    app = FastAPI(title="XRPL MPP Minimal Seller Example")
    middleware_kwargs = {}
    if client_factory is not None:
        middleware_kwargs["client_factory"] = client_factory

    app.add_middleware(
        PaymentMiddlewareASGI,
        route_configs={
            "GET /premium": build_premium_route_config()
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
