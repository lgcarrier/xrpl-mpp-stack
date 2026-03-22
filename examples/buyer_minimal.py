from __future__ import annotations

import asyncio

import httpx
from xrpl.wallet import Wallet

from xrpl_mpp_client import XRPLPaymentSigner, wrap_httpx_with_mpp_payment
from xrpl_mpp_core import getenv_clean

try:
    from dotenv import find_dotenv, load_dotenv
except ModuleNotFoundError:  # pragma: no cover - optional convenience import
    find_dotenv = None
    load_dotenv = None

DEFAULT_TARGET_BASE_URL = "http://127.0.0.1:8010"
DEFAULT_TARGET_PATH = "/premium"
DEFAULT_PAYMENT_ASSET = "XRP:native"
DEFAULT_NETWORK = "xrpl:1"
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


def payment_asset_from_env() -> str:
    return getenv_clean("PAYMENT_ASSET", DEFAULT_PAYMENT_ASSET) or DEFAULT_PAYMENT_ASSET


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
    payment_asset: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response:
    active_signer = signer or build_signer_from_env()
    active_base_url = base_url or target_base_url_from_env()
    active_target_path = target_path or target_path_from_env()
    active_payment_asset = payment_asset or payment_asset_from_env()

    async with wrap_httpx_with_mpp_payment(
        active_signer,
        asset=active_payment_asset,
        base_url=active_base_url,
        transport=transport,
    ) as client:
        return await client.get(active_target_path)


async def main() -> None:
    response = await fetch_premium()
    print(response.status_code)
    print(response.text)


if __name__ == "__main__":
    asyncio.run(main())
