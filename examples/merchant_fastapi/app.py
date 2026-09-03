from __future__ import annotations

from fastapi import FastAPI, Request

from examples._facilitator import allow_insecure_loopback_facilitator
from xrpl_mpp_core import getenv_clean, parse_currency
from xrpl_mpp_middleware import ChargeRouteSpec, PaymentMiddlewareASGI, RouteConfig

DEFAULT_FACILITATOR_URL = "http://127.0.0.1:8000"
DEFAULT_FACILITATOR_TOKEN = "replace-with-your-facilitator-token"
DEFAULT_MERCHANT_XRPL_ADDRESS = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
DEFAULT_XRPL_NETWORK = "testnet"
DEFAULT_PRICE_AMOUNT = "1000"
DEFAULT_PRICE_CURRENCY = "XRP"
DEFAULT_MPP_CHALLENGE_SECRET = "replace-with-a-long-random-secret"


def facilitator_url_from_env() -> str:
    return getenv_clean("FACILITATOR_URL", DEFAULT_FACILITATOR_URL) or DEFAULT_FACILITATOR_URL


def facilitator_token_from_env() -> str:
    return getenv_clean("FACILITATOR_TOKEN", DEFAULT_FACILITATOR_TOKEN) or DEFAULT_FACILITATOR_TOKEN


def merchant_xrpl_address_from_env() -> str:
    return getenv_clean("MERCHANT_XRPL_ADDRESS", DEFAULT_MERCHANT_XRPL_ADDRESS) or DEFAULT_MERCHANT_XRPL_ADDRESS


def xrpl_network_from_env() -> str:
    return getenv_clean("XRPL_NETWORK", DEFAULT_XRPL_NETWORK) or DEFAULT_XRPL_NETWORK


def mpp_challenge_secret_from_env() -> str:
    return getenv_clean("MPP_CHALLENGE_SECRET", DEFAULT_MPP_CHALLENGE_SECRET) or DEFAULT_MPP_CHALLENGE_SECRET


def mpp_default_realm_from_env() -> str | None:
    return getenv_clean("MPP_DEFAULT_REALM")


def price_amount_from_env() -> str:
    return getenv_clean("PRICE_AMOUNT", DEFAULT_PRICE_AMOUNT) or DEFAULT_PRICE_AMOUNT


def price_currency_from_env() -> str:
    currency = (
        getenv_clean("PRICE_CURRENCY", DEFAULT_PRICE_CURRENCY)
        or DEFAULT_PRICE_CURRENCY
    )
    parse_currency(currency)
    return currency


def build_premium_route_config() -> RouteConfig:
    facilitator_url = facilitator_url_from_env()
    facilitator_token = facilitator_token_from_env()
    merchant_xrpl_address = merchant_xrpl_address_from_env()
    xrpl_network = xrpl_network_from_env()
    price_amount = price_amount_from_env()
    price_currency = price_currency_from_env()
    description = "One premium XRPL MPP 0.2 charge"

    return RouteConfig(
        facilitatorUrl=facilitator_url,
        bearerToken=facilitator_token,
        allowInsecureFacilitatorHttp=allow_insecure_loopback_facilitator(
            facilitator_url
        ),
        chargeOptions=[
            ChargeRouteSpec(
                network=xrpl_network,
                recipient=merchant_xrpl_address,
                amount=price_amount,
                currency=price_currency,
                description=description,
            )
        ],
        description=description,
    )


def create_app(*, client_factory=None) -> FastAPI:
    app = FastAPI(title="XRPL MPP Merchant Example")
    middleware_kwargs = {}
    if client_factory is not None:
        middleware_kwargs["client_factory"] = client_factory

    app.add_middleware(
        PaymentMiddlewareASGI,
        route_configs={"GET /premium": build_premium_route_config()},
        challenge_secret=mpp_challenge_secret_from_env(),
        default_realm=mpp_default_realm_from_env(),
        **middleware_kwargs,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/premium")
    async def premium(request: Request) -> dict[str, str]:
        payment = request.state.mpp_payment
        return {
            "message": "premium content unlocked",
            "payer": payment.payer or "",
            "invoice_id": payment.invoice_id or "",
            "tx_hash": payment.tx_hash or "",
        }

    return app


app = create_app()
