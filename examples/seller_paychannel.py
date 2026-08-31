"""Minimal seller exposing the three XRPL PayChannel lifecycle routes.

The facilitator must be configured with ``PAYCHANNEL_PAYER_PUBLIC_KEY`` for the
buyer's claim key. Opening carries a signed ``PaymentChannelCreate``; subsequent
requests carry cumulative vouchers, never prepaid bearer tokens.
"""

from __future__ import annotations

from fastapi import FastAPI, Request

from examples._facilitator import allow_insecure_loopback_facilitator
from xrpl_mpp_core import getenv_clean
from xrpl_mpp_middleware import PaymentMiddlewareASGI, RouteConfig, SessionRouteSpec

DEFAULT_FACILITATOR_URL = "http://127.0.0.1:8000"
DEFAULT_FACILITATOR_TOKEN = "replace-with-your-facilitator-token"
DEFAULT_MERCHANT_XRPL_ADDRESS = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
DEFAULT_XRPL_NETWORK = "testnet"
DEFAULT_UNIT_DROPS = 250
DEFAULT_MPP_CHALLENGE_SECRET = "replace-with-a-long-random-secret"


def setting(name: str, default: str) -> str:
    return getenv_clean(name, default) or default


def _session_route(*, amount: int, description: str) -> RouteConfig:
    facilitator_url = setting("FACILITATOR_URL", DEFAULT_FACILITATOR_URL)
    return RouteConfig(
        facilitatorUrl=facilitator_url,
        bearerToken=setting("FACILITATOR_TOKEN", DEFAULT_FACILITATOR_TOKEN),
        allowInsecureFacilitatorHttp=allow_insecure_loopback_facilitator(
            facilitator_url
        ),
        sessionOptions=[
            SessionRouteSpec(
                network=setting("XRPL_NETWORK", DEFAULT_XRPL_NETWORK),
                recipient=setting(
                    "MERCHANT_XRPL_ADDRESS",
                    DEFAULT_MERCHANT_XRPL_ADDRESS,
                ),
                amount=str(amount),
                currency="XRP",
                description=description,
            )
        ],
        description=description,
    )


def create_app(*, client_factory=None) -> FastAPI:
    unit_drops = int(setting("SESSION_UNIT_DROPS", str(DEFAULT_UNIT_DROPS)))
    app = FastAPI(title="XRPL MPP PayChannel Seller Example")
    middleware_kwargs = {}
    if client_factory is not None:
        middleware_kwargs["client_factory"] = client_factory

    app.add_middleware(
        PaymentMiddlewareASGI,
        route_configs={
            "GET /channel/open": _session_route(
                amount=0,
                description="Open an XRPL PayChannel",
            ),
            "GET /metered": _session_route(
                amount=unit_drops,
                description="One cumulative XRPL PayChannel unit",
            ),
            "GET /channel/close": _session_route(
                amount=0,
                description="Finalize the last cumulative voucher",
            ),
        },
        challenge_secret=setting(
            "MPP_CHALLENGE_SECRET",
            DEFAULT_MPP_CHALLENGE_SECRET,
        ),
        **middleware_kwargs,
    )

    @app.get("/channel/open")
    async def opened(request: Request) -> dict[str, str]:
        receipt = request.state.mpp_payment
        return {
            "channel_id": receipt.channel_id or "",
            "cumulative": receipt.cumulative or "0",
            "action": receipt.action or "",
        }

    @app.get("/metered")
    async def metered(request: Request) -> dict[str, str]:
        receipt = request.state.mpp_payment
        return {
            "message": "metered content unlocked",
            "channel_id": receipt.channel_id or "",
            "cumulative": receipt.cumulative or "0",
            "action": receipt.action or "",
        }

    @app.get("/channel/close")
    async def close_fallback() -> dict[str, str]:  # pragma: no cover - middleware closes first
        return {"status": "close credential required"}

    return app


app = create_app()
