from __future__ import annotations

import asyncio

import httpx
from xrpl.wallet import Wallet

from examples._policy import spend_cap_to_policy_amount
from xrpl_mpp_client import (
    XRPLPaymentPolicy,
    XRPLPaymentSigner,
    wrap_httpx_with_mpp_payment,
)
from xrpl_mpp_core import getenv_clean

try:
    from dotenv import find_dotenv, load_dotenv
except ModuleNotFoundError:  # pragma: no cover - optional convenience import
    find_dotenv = None
    load_dotenv = None

DEFAULT_TARGET_BASE_URL = "http://127.0.0.1:8010"
DEFAULT_TARGET_PATH = "/premium"
DEFAULT_PAYMENT_CURRENCY = "XRP"
DEFAULT_NETWORK = "testnet"
DEFAULT_RPC_URL = "https://s.altnet.rippletest.net:51234/"


def _load_repo_dotenv() -> None:
    if find_dotenv is None or load_dotenv is None:
        return
    dotenv_path = find_dotenv(".env", usecwd=True)
    if dotenv_path:
        load_dotenv(dotenv_path, override=False)


_load_repo_dotenv()


def target_base_url_from_env() -> str:
    return getenv_clean("TARGET_BASE_URL", DEFAULT_TARGET_BASE_URL) or DEFAULT_TARGET_BASE_URL


def target_path_from_env() -> str:
    return getenv_clean("TARGET_PATH", DEFAULT_TARGET_PATH) or DEFAULT_TARGET_PATH


def payment_currency_from_env() -> str:
    return (
        getenv_clean("PAYMENT_CURRENCY", DEFAULT_PAYMENT_CURRENCY)
        or DEFAULT_PAYMENT_CURRENCY
    )


def rpc_url_from_env() -> str:
    return getenv_clean("XRPL_RPC_URL", DEFAULT_RPC_URL) or DEFAULT_RPC_URL


def build_signer_from_env() -> XRPLPaymentSigner:
    wallet_seed = getenv_clean("XRPL_WALLET_SEED")
    if not wallet_seed:
        raise RuntimeError("XRPL_WALLET_SEED is required to run the minimal buyer example")

    return XRPLPaymentSigner(
        Wallet.from_seed(wallet_seed),
        rpc_url=rpc_url_from_env(),
        network=getenv_clean("XRPL_NETWORK", DEFAULT_NETWORK) or DEFAULT_NETWORK,
    )


async def fetch_premium(
    *,
    signer: XRPLPaymentSigner | None = None,
    base_url: str | None = None,
    target_path: str | None = None,
    payment_currency: str | None = None,
    expected_recipient: str | None = None,
    max_payment_amount: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response:
    active_signer = signer or build_signer_from_env()
    active_base_url = base_url or target_base_url_from_env()
    active_target_path = target_path or target_path_from_env()
    active_payment_currency = payment_currency or payment_currency_from_env()
    active_recipient = (
        expected_recipient
        or getenv_clean("XRPL_MPP_EXPECTED_RECIPIENT")
        or getenv_clean("MERCHANT_XRPL_ADDRESS")
    )
    configured_spend_cap = getenv_clean("XRPL_MPP_MAX_SPEND")
    active_maximum = max_payment_amount or (
        spend_cap_to_policy_amount(
            currency=active_payment_currency,
            max_spend=configured_spend_cap,
        )
        if configured_spend_cap is not None
        else getenv_clean("PRICE_AMOUNT")
    )
    if not active_recipient or not active_maximum:
        raise RuntimeError(
            "XRPL_MPP_EXPECTED_RECIPIENT and XRPL_MPP_MAX_SPEND (or the local "
            "PRICE_AMOUNT fallback) are required for automatic payment"
        )
    policy = XRPLPaymentPolicy(
        expected_recipients=active_recipient,
        max_amount=active_maximum,
        allowed_currencies={active_payment_currency},
    )

    async with wrap_httpx_with_mpp_payment(
        active_signer,
        currency=active_payment_currency,
        base_url=active_base_url,
        transport=transport,
        payment_policy=policy,
        allow_insecure_localhost=True,
    ) as client:
        return await client.get(active_target_path)


async def main() -> None:
    response = await fetch_premium()
    print(response.status_code)
    print(response.text)


if __name__ == "__main__":
    asyncio.run(main())
