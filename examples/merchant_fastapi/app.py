from __future__ import annotations

from fastapi import FastAPI, Request

from xrpl_mpp_core import getenv_clean
from xrpl_mpp_middleware import PaymentMiddlewareASGI, RouteConfig, require_payment

DEFAULT_FACILITATOR_URL = "http://127.0.0.1:8000"
DEFAULT_FACILITATOR_TOKEN = "replace-with-your-facilitator-token"
DEFAULT_MERCHANT_XRPL_ADDRESS = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
DEFAULT_XRPL_NETWORK = "xrpl:1"
DEFAULT_PRICE_DROPS = 1000
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


def price_drops_from_env() -> int:
    return int(getenv_clean("PRICE_DROPS", str(DEFAULT_PRICE_DROPS)) or DEFAULT_PRICE_DROPS)


def price_asset_code_from_env() -> str:
    return (getenv_clean("PRICE_ASSET_CODE", "XRP") or "XRP").upper()


def price_asset_issuer_from_env() -> str | None:
    return getenv_clean("PRICE_ASSET_ISSUER")


def price_asset_amount_from_env() -> str | None:
    return getenv_clean("PRICE_ASSET_AMOUNT")


def build_premium_route_config() -> RouteConfig:
    facilitator_url = facilitator_url_from_env()
    facilitator_token = facilitator_token_from_env()
    merchant_xrpl_address = merchant_xrpl_address_from_env()
    xrpl_network = xrpl_network_from_env()
    price_drops = price_drops_from_env()
    price_asset_code = price_asset_code_from_env()
    price_asset_issuer = price_asset_issuer_from_env()
    price_asset_amount = price_asset_amount_from_env()

    uses_issued_asset = (
        price_asset_code != "XRP"
        or price_asset_issuer is not None
        or price_asset_amount is not None
    )
    if not uses_issued_asset:
        return require_payment(
            facilitator_url=facilitator_url,
            bearer_token=facilitator_token,
            pay_to=merchant_xrpl_address,
            network=xrpl_network,
            xrp_drops=price_drops,
            description="One premium XRPL MPP request",
        )

    if price_asset_code == "XRP":
        raise RuntimeError(
            "Issued-asset pricing requires PRICE_ASSET_CODE to be set to a non-XRP asset"
        )
    if price_asset_issuer is None:
        raise RuntimeError("Issued-asset pricing requires PRICE_ASSET_ISSUER")
    if price_asset_amount is None:
        raise RuntimeError("Issued-asset pricing requires PRICE_ASSET_AMOUNT")

    return require_payment(
        facilitator_url=facilitator_url,
        bearer_token=facilitator_token,
        pay_to=merchant_xrpl_address,
        network=xrpl_network,
        amount=price_asset_amount,
        asset_code=price_asset_code,
        asset_issuer=price_asset_issuer,
        description=f"One premium {price_asset_code} XRPL MPP request",
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
