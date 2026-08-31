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
from xrpl_mpp_core.testnet_rpc import resolve_testnet_rpc_url

try:
    from dotenv import find_dotenv, load_dotenv
except ModuleNotFoundError:  # pragma: no cover - optional convenience import
    find_dotenv = None
    load_dotenv = None

DEFAULT_MAINNET_RPC_URL = "https://s1.ripple.com:51234"
DEFAULT_RPC_URL = DEFAULT_MAINNET_RPC_URL
DEFAULT_NETWORK = "testnet"
DEFAULT_PAYMENT_CURRENCY = "XRP"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0


def _load_repo_dotenv() -> None:
    if find_dotenv is None or load_dotenv is None:
        return
    dotenv_path = find_dotenv(".env", usecwd=True)
    if dotenv_path:
        # Repo-level convenience for `python -m examples.buyer_httpx`.
        load_dotenv(dotenv_path, override=False)


_load_repo_dotenv()


def payment_currency_from_env() -> str:
    """Return the explicitly authorized MPP currency for automatic payment."""

    return (
        getenv_clean("PAYMENT_CURRENCY")
        or getenv_clean("PRICE_CURRENCY")
        or DEFAULT_PAYMENT_CURRENCY
    )


def rpc_url_from_env() -> str:
    explicit_rpc_url = getenv_clean("XRPL_RPC_URL")
    if explicit_rpc_url:
        return explicit_rpc_url

    if (getenv_clean("XRPL_NETWORK", DEFAULT_NETWORK) or DEFAULT_NETWORK) == "testnet":
        return resolve_testnet_rpc_url()

    return DEFAULT_RPC_URL


def request_timeout_seconds() -> float:
    return DEFAULT_REQUEST_TIMEOUT_SECONDS


def build_signer_from_env() -> XRPLPaymentSigner:
    wallet_seed = getenv_clean("XRPL_WALLET_SEED")
    if not wallet_seed:
        raise RuntimeError("XRPL_WALLET_SEED is required to run the buyer example")

    wallet = Wallet.from_seed(wallet_seed)
    return XRPLPaymentSigner(
        wallet,
        rpc_url=rpc_url_from_env(),
        network=getenv_clean("XRPL_NETWORK", DEFAULT_NETWORK) or DEFAULT_NETWORK,
    )


async def fetch_paid_resource(
    *,
    signer: XRPLPaymentSigner | None = None,
    target_url: str | None = None,
    payment_currency: str | None = None,
    expected_recipient: str | None = None,
    max_payment_amount: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response:
    active_signer = signer or build_signer_from_env()
    active_target_url = (
        target_url
        or getenv_clean("TARGET_URL", "http://127.0.0.1:8010/premium")
        or "http://127.0.0.1:8010/premium"
    )
    active_payment_currency = (
        payment_currency if payment_currency is not None else payment_currency_from_env()
    )
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
        transport=transport,
        timeout=request_timeout_seconds(),
        payment_policy=policy,
        # The library still limits this opt-in to localhost/loopback; HTTPS
        # targets remain the production default.
        allow_insecure_localhost=True,
    ) as client:
        return await client.get(active_target_url)


async def main() -> None:
    response = await fetch_paid_resource()
    print(f"status={response.status_code}")
    print(response.text)


if __name__ == "__main__":
    asyncio.run(main())
