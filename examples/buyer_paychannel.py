"""Buyer-side XRPL PayChannel open, cumulative voucher, and close example."""

from __future__ import annotations

import asyncio

import httpx
from xrpl.wallet import Wallet

from xrpl_mpp_client import (
    XRPLPaymentPolicy,
    XRPLPaymentSigner,
    XRPLPaymentTransport,
    decode_payment_receipt_header,
)
from xrpl_mpp_core import (
    PaymentReceipt,
    decode_challenge_request,
    extract_payment_challenges,
    getenv_clean,
)

DEFAULT_TARGET_BASE_URL = "http://127.0.0.1:8010"
DEFAULT_RPC_URL = "https://s.altnet.rippletest.net:51234/"
DEFAULT_NETWORK = "testnet"
DEFAULT_FUNDING_DROPS = "1000000"
DEFAULT_SETTLE_DELAY = 3600


def _required(name: str) -> str:
    value = getenv_clean(name)
    if not value:
        raise RuntimeError(f"{name} is required to run the PayChannel buyer example")
    return value


def build_signer_from_env() -> XRPLPaymentSigner:
    return XRPLPaymentSigner(
        Wallet.from_seed(_required("XRPL_WALLET_SEED")),
        rpc_url=getenv_clean("XRPL_RPC_URL", DEFAULT_RPC_URL) or DEFAULT_RPC_URL,
        network=getenv_clean("XRPL_NETWORK", DEFAULT_NETWORK) or DEFAULT_NETWORK,
        expected_recipient=_required("MERCHANT_XRPL_ADDRESS"),
        allowed_currencies={"XRP"},
    )


def _require_receipt(response: httpx.Response, *, action: str) -> PaymentReceipt:
    receipt = decode_payment_receipt_header(response.headers)
    if response.status_code < 200 or response.status_code >= 300 or receipt is None:
        raise RuntimeError(f"PayChannel {action} failed with HTTP {response.status_code}")
    return receipt


async def run_paychannel_flow(
    *,
    signer: XRPLPaymentSigner,
    merchant_address: str,
    target_base_url: str = DEFAULT_TARGET_BASE_URL,
    funding_drops: str = DEFAULT_FUNDING_DROPS,
    settle_delay: int = DEFAULT_SETTLE_DELAY,
    request_count: int = 2,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[PaymentReceipt]:
    """Open one channel, issue cumulative vouchers, then finalize the last claim."""

    if request_count < 1:
        raise ValueError("request_count must be at least one")
    base = target_base_url.rstrip("/")
    open_url = f"{base}/channel/open"
    metered_url = f"{base}/metered"
    close_url = f"{base}/channel/close"

    base_transport = transport or httpx.AsyncHTTPTransport()
    probe = await base_transport.handle_async_request(httpx.Request("GET", open_url))
    await probe.aread()
    open_challenge = next(
        (
            challenge
            for challenge in extract_payment_challenges(probe.headers)
            if challenge.method == "xrpl"
            and challenge.intent == "session"
            and decode_challenge_request(challenge).channel_id == ""
        ),
        None,
    )
    if open_challenge is None:
        await base_transport.aclose()
        raise RuntimeError("PayChannel open endpoint returned no xrpl/session challenge")

    payment_transport = XRPLPaymentTransport(
        signer,
        network=signer.network,
        currency="XRP",
        base_transport=base_transport,
        payment_policy=XRPLPaymentPolicy(
            expected_recipients=merchant_address,
            max_amount=funding_drops,
            allowed_currencies={"XRP"},
        ),
        allow_insecure_localhost=True,
    )
    open_transaction = await signer.sign_channel_create_async(
        destination=merchant_address,
        funding_amount=funding_drops,
        settle_delay=settle_delay,
        challenge_expires=open_challenge.expires,
    )
    payment_transport.register_open_transaction(open_url, transaction=open_transaction)

    receipts: list[PaymentReceipt] = []
    async with httpx.AsyncClient(transport=payment_transport) as client:
        open_receipt = _require_receipt(await client.get(open_url), action="open")
        receipts.append(open_receipt)
        open_state = payment_transport.channel_state(open_url)
        if open_state is None:
            raise RuntimeError("PayChannel open receipt was not bound to the signed transaction")

        payment_transport.register_channel(
            metered_url,
            channel_id=open_state.channel_id,
            cumulative_amount=open_state.cumulative_amount,
            recipient=merchant_address,
            network=signer.network,
        )
        for _ in range(request_count):
            receipts.append(
                _require_receipt(await client.get(metered_url), action="voucher")
            )

        latest_state = payment_transport.channel_state(metered_url)
        if latest_state is None:
            raise RuntimeError("PayChannel voucher receipts did not preserve channel state")

        payment_transport.register_channel(
            close_url,
            channel_id=latest_state.channel_id,
            cumulative_amount=latest_state.cumulative_amount,
            recipient=merchant_address,
            network=signer.network,
        )
        receipts.append(
            _require_receipt(
                await payment_transport.close_session(close_url),
                action="close",
            )
        )
    return receipts


async def main() -> None:
    signer = build_signer_from_env()
    receipts = await run_paychannel_flow(
        signer=signer,
        merchant_address=_required("MERCHANT_XRPL_ADDRESS"),
        target_base_url=(
            getenv_clean("TARGET_BASE_URL", DEFAULT_TARGET_BASE_URL)
            or DEFAULT_TARGET_BASE_URL
        ),
        funding_drops=(
            getenv_clean("PAYCHANNEL_FUNDING_DROPS", DEFAULT_FUNDING_DROPS)
            or DEFAULT_FUNDING_DROPS
        ),
        settle_delay=int(
            getenv_clean("PAYCHANNEL_SETTLE_DELAY", str(DEFAULT_SETTLE_DELAY))
            or DEFAULT_SETTLE_DELAY
        ),
    )
    for receipt in receipts:
        print(
            f"action={receipt.action} channel={receipt.channel_id} "
            f"cumulative={receipt.cumulative} reference={receipt.reference}"
        )


if __name__ == "__main__":
    asyncio.run(main())
