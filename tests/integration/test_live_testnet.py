"""Opt-in XRPL Testnet verification for MPP 0.2 settlement paths.

Charge and PayChannel round trips remain gated by ``RUN_XRPL_TESTNET_LIVE=1``;
the default suite never submits an XRPL transaction.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import os
from time import sleep
from typing import Callable

from fastapi import FastAPI, Request
import httpx
import pytest
from slowapi import Limiter as SlowLimiter
from xrpl.clients import JsonRpcClient
from xrpl.models.requests import AccountChannels, Tx
from xrpl.models.transactions import PaymentChannelClaim, PaymentChannelClaimFlag
from xrpl.transaction import submit_and_wait
from xrpl.wallet import Wallet

import xrpl_mpp_facilitator.factory as factory_module
from devtools.live_testnet_support import (
    DEFAULT_RLUSD_TESTNET_ISSUER,
    DEFAULT_USDC_TESTNET_ISSUER,
    DemoWalletSet,
    LIVE_TEST_FLAG,
    RLUSD_TESTNET_ISSUER_ENV,
    USDC_TESTNET_ISSUER_ENV,
    LiveWalletPair,
    ensure_rlusd_trustline,
    ensure_usdc_trustline,
    get_demo_wallet_set,
    get_live_wallet_pair,
    get_validated_balance,
    get_validated_trustline_balance,
    get_validated_usdc_trustline_balance,
    recover_tracked_claim_wallets,
    recover_tracked_usdc_claim_wallets,
    resolve_live_testnet_rpc_url,
    wallet_cache_path,
)
from tests.fakes import FakeRedis
from xrpl_mpp_client import (
    XRPLPaymentPolicy,
    XRPLPaymentSigner,
    XRPLPaymentTransport,
    build_payment_authorization,
    decode_payment_challenges_response,
    decode_payment_receipt_header,
)
from xrpl_mpp_core import (
    IssuedCurrency,
    PaymentReceipt,
    XRPLChargeRequest,
    challenge_invoice_id,
    decode_challenge_request,
    normalize_currency_code,
    serialize_currency,
    xrpl_currency_code,
)
from xrpl_mpp_facilitator.config import Settings
from xrpl_mpp_facilitator.factory import create_app
from xrpl_mpp_facilitator.xrpl_service import XRPLService
from xrpl_mpp_middleware import (
    ChargeRouteSpec,
    PaymentMiddlewareASGI,
    RouteConfig,
    SessionRouteSpec,
    XRPLFacilitatorClient,
)

XRP_PAYMENT_DROPS = 2_000_000
RLUSD_PAYMENT_VALUE = Decimal("3.75")
USDC_PAYMENT_VALUE = Decimal("4.5")
LIVE_TEST_BEARER_TOKEN = "live-test-facilitator-token"
LIVE_TEST_CHALLENGE_SECRET = "live-test-mpp-challenge-secret"
FACILITATOR_BASE_URL = "https://facilitator.local"
MERCHANT_BASE_URL = "http://127.0.0.1"
PAYCHANNEL_FUNDING_DROPS = "1000000"
PAYCHANNEL_UNIT_DROPS = 250
# Keep the opt-in cleanup bounded. Production defaults and examples retain the
# one-hour safety window; this live test explicitly configures the facilitator
# to accept a one-second channel before exercising the funder's close lifecycle.
PAYCHANNEL_SETTLE_DELAY = 1
TF_CLOSE = PaymentChannelClaimFlag.TF_CLOSE.value
LIVE_SKIP = pytest.mark.skipif(
    os.environ.get(LIVE_TEST_FLAG) != "1",
    reason=f"Set {LIVE_TEST_FLAG}=1 to run the XRPL Testnet live integration test.",
)


@dataclass(frozen=True)
class LiveChargeResult:
    challenge_response: httpx.Response
    paid_response: httpx.Response
    replay_response: httpx.Response
    request: XRPLChargeRequest
    tx_hash: str


def _build_facilitator(settings: Settings) -> FastAPI:
    service = XRPLService(settings, redis_client=FakeRedis())
    original = factory_module.build_rate_limiter
    factory_module.build_rate_limiter = lambda _settings: SlowLimiter(
        key_func=factory_module.get_remote_address
    )
    try:
        return create_app(app_settings=settings, xrpl_service=service)
    finally:
        factory_module.build_rate_limiter = original


def _build_merchant(
    *,
    facilitator_app: FastAPI,
    recipient: str,
    amount: str,
    currency: str,
) -> tuple[FastAPI, httpx.AsyncClient]:
    merchant = FastAPI()

    @merchant.get("/paid")
    async def paid(request: Request) -> dict[str, str]:
        receipt = request.state.mpp_payment
        return {
            "reference": receipt.reference,
            "tx_hash": receipt.tx_hash or "",
            "payer": receipt.payer or "",
        }

    facilitator_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=facilitator_app),
        base_url=FACILITATOR_BASE_URL,
    )
    merchant.add_middleware(
        PaymentMiddlewareASGI,
        route_configs={
            "GET /paid": RouteConfig(
                facilitatorUrl=FACILITATOR_BASE_URL,
                bearerToken=LIVE_TEST_BEARER_TOKEN,
                chargeOptions=[
                    ChargeRouteSpec(
                        network="testnet",
                        recipient=recipient,
                        amount=amount,
                        currency=currency,
                        description="Live MPP 0.2 XRPL charge",
                    )
                ],
            )
        },
        challenge_secret=LIVE_TEST_CHALLENGE_SECRET,
        client_factory=lambda _url, _token: XRPLFacilitatorClient(
            base_url=FACILITATOR_BASE_URL,
            bearer_token=LIVE_TEST_BEARER_TOKEN,
            async_client=facilitator_client,
        ),
    )
    return merchant, facilitator_client


def _build_paychannel_merchant(
    *,
    facilitator_app: FastAPI,
    recipient: str,
) -> tuple[FastAPI, httpx.AsyncClient]:
    merchant = FastAPI()

    def route(amount: int, description: str) -> RouteConfig:
        return RouteConfig(
            facilitatorUrl=FACILITATOR_BASE_URL,
            bearerToken=LIVE_TEST_BEARER_TOKEN,
            allowInsecureFacilitatorHttp=True,
            sessionOptions=[
                SessionRouteSpec(
                    network="testnet",
                    recipient=recipient,
                    amount=str(amount),
                    currency="XRP",
                    description=description,
                )
            ],
        )

    facilitator_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=facilitator_app),
        base_url=FACILITATOR_BASE_URL,
    )
    merchant.add_middleware(
        PaymentMiddlewareASGI,
        route_configs={
            "GET /channel/open": route(0, "Open a live XRPL PayChannel"),
            "GET /metered": route(
                PAYCHANNEL_UNIT_DROPS,
                "One live cumulative PayChannel unit",
            ),
            "GET /channel/close": route(0, "Finalize the live PayChannel voucher"),
        },
        challenge_secret=LIVE_TEST_CHALLENGE_SECRET,
        client_factory=lambda _url, _token: XRPLFacilitatorClient(
            base_url=FACILITATOR_BASE_URL,
            bearer_token=LIVE_TEST_BEARER_TOKEN,
            async_client=facilitator_client,
        ),
    )

    @merchant.get("/channel/open")
    async def opened(request: Request) -> dict[str, str]:
        receipt = request.state.mpp_payment
        return {"channel_id": receipt.channel_id or ""}

    @merchant.get("/metered")
    async def metered(request: Request) -> dict[str, str]:
        receipt = request.state.mpp_payment
        return {"cumulative": receipt.cumulative or "0"}

    @merchant.get("/channel/close")
    async def closed(request: Request) -> dict[str, str]:
        receipt = request.state.mpp_payment
        return {"tx_hash": receipt.tx_hash or ""}

    return merchant, facilitator_client


async def _perform_charge(
    *,
    merchant: FastAPI,
    facilitator_client: httpx.AsyncClient,
    signer: XRPLPaymentSigner,
) -> LiveChargeResult:
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=merchant),
            base_url=MERCHANT_BASE_URL,
        ) as client:
            challenge_response = await client.get("/paid")
            challenges = decode_payment_challenges_response(challenge_response.headers)
            assert len(challenges) == 1
            decoded = decode_challenge_request(challenges[0])
            assert isinstance(decoded, XRPLChargeRequest)
            credential = await signer.build_charge_credential_async(challenges[0])
            authorization = build_payment_authorization(credential)
            paid_response = await client.get(
                "/paid",
                headers={"Authorization": authorization},
            )
            replay_response = await client.get(
                "/paid",
                headers={"Authorization": authorization},
            )
    finally:
        await facilitator_client.aclose()
    receipt = decode_payment_receipt_header(paid_response.headers)
    assert receipt is not None and receipt.tx_hash
    return LiveChargeResult(
        challenge_response=challenge_response,
        paid_response=paid_response,
        replay_response=replay_response,
        request=decoded,
        tx_hash=receipt.tx_hash,
    )


def _settings(
    *,
    rpc_url: str,
    recipient: str,
    allowed_issued_assets: str = "",
    payer_public_key: str | None = None,
    recipient_seed: str | None = None,
    paychannel_min_settle_delay: int = 3_600,
) -> Settings:
    return Settings(
        _env_file=None,
        XRPL_RPC_URL=rpc_url,
        MY_DESTINATION_ADDRESS=recipient,
        REDIS_URL="redis://fake:6379/0",
        NETWORK_ID="testnet",
        SETTLEMENT_MODE="validated",
        VALIDATION_TIMEOUT=30,
        MIN_XRP_DROPS=1000,
        ALLOWED_ISSUED_ASSETS=allowed_issued_assets,
        FACILITATOR_BEARER_TOKEN=LIVE_TEST_BEARER_TOKEN,
        MPP_CHALLENGE_SECRET=LIVE_TEST_CHALLENGE_SECRET,
        PAYCHANNEL_PAYER_PUBLIC_KEY=payer_public_key,
        PAYCHANNEL_RECIPIENT_SEED=recipient_seed,
        PAYCHANNEL_MIN_SETTLE_DELAY=paychannel_min_settle_delay,
    )


async def _perform_paychannel_round_trip(
    *,
    merchant: FastAPI,
    facilitator_client: httpx.AsyncClient,
    signer: XRPLPaymentSigner,
    recipient: str,
) -> list[PaymentReceipt]:
    base_url = "https://merchant.local"
    open_url = f"{base_url}/channel/open"
    metered_url = f"{base_url}/metered"
    close_url = f"{base_url}/channel/close"
    ripple_now = int(datetime.now(UTC).timestamp()) - 946_684_800
    open_blob = await signer.sign_channel_create_async(
        destination=recipient,
        funding_amount=PAYCHANNEL_FUNDING_DROPS,
        settle_delay=PAYCHANNEL_SETTLE_DELAY,
        cancel_after=ripple_now + 7_200,
    )
    transport = XRPLPaymentTransport(
        signer,
        network="testnet",
        currency="XRP",
        base_transport=httpx.ASGITransport(app=merchant),
        payment_policy=XRPLPaymentPolicy(
            expected_recipients=recipient,
            max_amount=PAYCHANNEL_FUNDING_DROPS,
            allowed_currencies={"XRP"},
        ),
        allow_insecure_localhost=True,
    )
    transport.register_open_transaction(open_url, transaction=open_blob)
    receipts: list[PaymentReceipt] = []
    try:
        async with httpx.AsyncClient(transport=transport) as client:
            open_response = await client.get(open_url)
            assert open_response.status_code == 200
            open_receipt = decode_payment_receipt_header(open_response.headers)
            assert open_receipt is not None and open_receipt.channel_id
            receipts.append(open_receipt)

            transport.register_channel(
                metered_url,
                channel_id=open_receipt.channel_id,
                cumulative_amount=open_receipt.cumulative or "0",
                recipient=recipient,
                network="testnet",
            )
            for _ in range(2):
                response = await client.get(metered_url)
                assert response.status_code == 200
                receipt = decode_payment_receipt_header(response.headers)
                assert receipt is not None
                receipts.append(receipt)

            latest = receipts[-1]
            transport.register_channel(
                close_url,
                channel_id=latest.channel_id or "",
                cumulative_amount=latest.cumulative or "0",
                recipient=recipient,
                network="testnet",
            )
            close_response = await transport.close_session(close_url)
            assert close_response.status_code == 200
            close_receipt = decode_payment_receipt_header(close_response.headers)
            assert close_receipt is not None
            receipts.append(close_receipt)
    finally:
        await facilitator_client.aclose()
    return receipts


def _assert_common(
    result: LiveChargeResult,
    *,
    sender: Wallet,
    receiver: Wallet,
    currency: str,
    amount: str,
) -> dict:
    receipt = decode_payment_receipt_header(result.paid_response.headers)
    assert receipt is not None
    assert result.challenge_response.status_code == 402
    assert result.challenge_response.headers["Cache-Control"] == "no-store"
    assert result.request.recipient == receiver.classic_address
    assert result.request.currency == currency
    assert result.request.amount == amount
    assert result.paid_response.status_code == 200
    assert result.paid_response.json() == {
        "reference": result.tx_hash,
        "tx_hash": result.tx_hash,
        "payer": sender.classic_address,
    }
    assert receipt.challenge_id is not None
    explicit_invoice_id = (
        result.request.method_details.invoice_id
        if result.request.method_details is not None
        else None
    )
    assert receipt.invoice_id == (
        explicit_invoice_id or challenge_invoice_id(receipt.challenge_id)
    )
    assert receipt.network == "testnet"
    assert receipt.payer == sender.classic_address
    assert receipt.recipient == receiver.classic_address
    assert receipt.settlement_status == "validated"
    assert result.replay_response.status_code == 402

    client = JsonRpcClient(resolve_live_testnet_rpc_url())
    tx_response = client.request(Tx(transaction=result.tx_hash)).result
    assert tx_response.get("validated") is True
    tx_payload = tx_response.get("tx_json") or tx_response.get("tx") or {}
    assert tx_payload.get("Destination") == receiver.classic_address
    return tx_payload


def _account_channel_ids(client: JsonRpcClient, account: str) -> set[str]:
    response = client.request(
        AccountChannels(
            account=account,
            ledger_index="validated",
            limit=200,
        )
    ).result
    channels = response.get("channels", [])
    return {
        str(channel["channel_id"]).upper()
        for channel in channels
        if isinstance(channel, dict) and channel.get("channel_id")
    }


def _close_and_refund_live_paychannel(
    *,
    client: JsonRpcClient,
    funder: Wallet,
    channel_id: str,
) -> list[str]:
    """Start funder close, then delete the channel after its short test delay."""

    normalized_channel_id = channel_id.upper()
    balance_while_locked = get_validated_balance(client, funder.classic_address)
    transaction_hashes: list[str] = []
    for _attempt in range(4):
        if normalized_channel_id not in _account_channel_ids(
            client,
            funder.classic_address,
        ):
            break
        response = submit_and_wait(
            PaymentChannelClaim(
                account=funder.classic_address,
                channel=normalized_channel_id,
                flags=TF_CLOSE,
            ),
            client,
            funder,
            fail_hard=True,
        ).result
        metadata = response.get("meta") or response.get("metaData")
        result_code = (
            metadata.get("TransactionResult")
            if isinstance(metadata, dict)
            else None
        )
        if result_code == "tesSUCCESS":
            tx_hash = response.get("hash")
            assert isinstance(tx_hash, str) and len(tx_hash) == 64
            transaction_hashes.append(tx_hash.upper())
        else:
            # A close submitted just before the one-second Expiration is
            # validated can be refused until the next ledger. A missing entry
            # means another validated close already completed cleanup.
            assert result_code in {"tecNO_PERMISSION", "tecNO_ENTRY"}
        if normalized_channel_id in _account_channel_ids(
            client,
            funder.classic_address,
        ):
            sleep(5)

    assert normalized_channel_id not in _account_channel_ids(
        client,
        funder.classic_address,
    )
    assert get_validated_balance(client, funder.classic_address) > balance_while_locked
    return transaction_hashes


@pytest.mark.live
@LIVE_SKIP
def test_live_xrp_charge_round_trip() -> None:
    rpc_url = resolve_live_testnet_rpc_url()
    rpc_client = JsonRpcClient(rpc_url)
    sender, receiver = _select_xrp_wallets(
        rpc_client,
        get_live_wallet_pair(rpc_client),
        amount_drops=XRP_PAYMENT_DROPS,
    )
    facilitator = _build_facilitator(
        _settings(rpc_url=rpc_url, recipient=receiver.classic_address)
    )
    merchant, client = _build_merchant(
        facilitator_app=facilitator,
        recipient=receiver.classic_address,
        amount=str(XRP_PAYMENT_DROPS),
        currency="XRP",
    )
    before = get_validated_balance(rpc_client, receiver.classic_address)
    result = asyncio.run(
        _perform_charge(
            merchant=merchant,
            facilitator_client=client,
            signer=XRPLPaymentSigner(sender, rpc_url=rpc_url, network="testnet"),
        )
    )
    tx = _assert_common(
        result,
        sender=sender,
        receiver=receiver,
        currency="XRP",
        amount=str(XRP_PAYMENT_DROPS),
    )
    assert (tx.get("Amount") or tx.get("DeliverMax")) == str(XRP_PAYMENT_DROPS)
    assert get_validated_balance(rpc_client, receiver.classic_address) - before == XRP_PAYMENT_DROPS


@pytest.mark.live
@LIVE_SKIP
def test_live_xrp_paychannel_open_voucher_close_and_recipient_redeem() -> None:
    rpc_url = resolve_live_testnet_rpc_url()
    rpc_client = JsonRpcClient(rpc_url)
    payer, recipient = _select_xrp_wallets(
        rpc_client,
        get_live_wallet_pair(rpc_client),
        amount_drops=int(PAYCHANNEL_FUNDING_DROPS),
    )
    preexisting_channels = _account_channel_ids(
        rpc_client,
        payer.classic_address,
    )
    facilitator = _build_facilitator(
        _settings(
            rpc_url=rpc_url,
            recipient=recipient.classic_address,
            payer_public_key=payer.public_key,
            recipient_seed=recipient.seed,
            paychannel_min_settle_delay=PAYCHANNEL_SETTLE_DELAY,
        )
    )
    merchant, facilitator_client = _build_paychannel_merchant(
        facilitator_app=facilitator,
        recipient=recipient.classic_address,
    )
    cleanup_hashes: dict[str, list[str]] = {}
    try:
        receipts = asyncio.run(
            _perform_paychannel_round_trip(
                merchant=merchant,
                facilitator_client=facilitator_client,
                signer=XRPLPaymentSigner(
                    payer,
                    rpc_url=rpc_url,
                    network="testnet",
                    expected_recipient=recipient.classic_address,
                    max_amount=PAYCHANNEL_FUNDING_DROPS,
                    allowed_currencies={"XRP"},
                ),
                recipient=recipient.classic_address,
            )
        )

        assert [receipt.action for receipt in receipts] == [
            "open",
            "voucher",
            "voucher",
            "close",
        ]
        close_receipt = receipts[-1]
        assert close_receipt.cumulative == str(2 * PAYCHANNEL_UNIT_DROPS)
        assert close_receipt.settlement_status == "validated"
        assert close_receipt.tx_hash
        tx_result = rpc_client.request(Tx(transaction=close_receipt.tx_hash)).result
        assert tx_result.get("validated") is True
        tx = tx_result.get("tx_json") or tx_result.get("tx") or tx_result
        assert tx.get("TransactionType") == "PaymentChannelClaim"
        assert tx.get("Account") == recipient.classic_address
        assert tx.get("Channel") == close_receipt.channel_id
        assert tx.get("Balance") == close_receipt.cumulative
        assert int(tx.get("Flags", 0)) & TF_CLOSE == 0
    finally:
        new_channels = _account_channel_ids(
            rpc_client,
            payer.classic_address,
        ) - preexisting_channels
        for channel_id in new_channels:
            cleanup_hashes[channel_id] = _close_and_refund_live_paychannel(
                client=rpc_client,
                funder=payer,
                channel_id=channel_id,
            )

    assert close_receipt.channel_id in cleanup_hashes
    assert cleanup_hashes[close_receipt.channel_id]


@pytest.mark.live
@LIVE_SKIP
def test_live_rlusd_charge_round_trip() -> None:
    rpc_url = resolve_live_testnet_rpc_url()
    rpc_client = JsonRpcClient(rpc_url)
    issuer = os.environ.get(RLUSD_TESTNET_ISSUER_ENV, DEFAULT_RLUSD_TESTNET_ISSUER)
    wallets = get_demo_wallet_set(rpc_client)
    recover_tracked_claim_wallets(rpc_client, wallets.merchant_wallet, issuer)
    for wallet in (wallets.merchant_wallet, wallets.buyer_wallet("rlusd")):
        ensure_rlusd_trustline(rpc_client, wallet, issuer)
    sender, receiver = _select_issued_wallets(
        wallets,
        balance=lambda wallet: get_validated_trustline_balance(
            rpc_client,
            wallet.classic_address,
            issuer,
            currency_code="RLUSD",
        ),
        symbol="rlusd",
        required=RLUSD_PAYMENT_VALUE,
    )
    currency = serialize_currency(
        IssuedCurrency(currency=xrpl_currency_code("RLUSD"), issuer=issuer)
    )
    facilitator = _build_facilitator(
        _settings(
            rpc_url=rpc_url,
            recipient=receiver.classic_address,
            allowed_issued_assets=f"RLUSD:{issuer}",
        )
    )
    merchant, client = _build_merchant(
        facilitator_app=facilitator,
        recipient=receiver.classic_address,
        amount=str(RLUSD_PAYMENT_VALUE),
        currency=currency,
    )
    before = get_validated_trustline_balance(
        rpc_client,
        receiver.classic_address,
        issuer,
        currency_code="RLUSD",
    )
    result = asyncio.run(
        _perform_charge(
            merchant=merchant,
            facilitator_client=client,
            signer=XRPLPaymentSigner(sender, rpc_url=rpc_url, network="testnet"),
        )
    )
    tx = _assert_common(
        result,
        sender=sender,
        receiver=receiver,
        currency=currency,
        amount=str(RLUSD_PAYMENT_VALUE),
    )
    amount = tx.get("Amount") or tx.get("DeliverMax")
    assert normalize_currency_code(str(amount["currency"])) == "RLUSD"
    assert Decimal(str(amount["value"])) == RLUSD_PAYMENT_VALUE
    after = get_validated_trustline_balance(
        rpc_client,
        receiver.classic_address,
        issuer,
        currency_code="RLUSD",
    )
    assert after - before == RLUSD_PAYMENT_VALUE


@pytest.mark.live
@LIVE_SKIP
def test_live_usdc_charge_round_trip() -> None:
    rpc_url = resolve_live_testnet_rpc_url()
    rpc_client = JsonRpcClient(rpc_url)
    issuer = os.environ.get(USDC_TESTNET_ISSUER_ENV, DEFAULT_USDC_TESTNET_ISSUER)
    wallets = get_demo_wallet_set(rpc_client)
    recover_tracked_usdc_claim_wallets(rpc_client, wallets.merchant_wallet, issuer)
    for wallet in (wallets.merchant_wallet, wallets.buyer_wallet("usdc")):
        ensure_usdc_trustline(rpc_client, wallet, issuer)
    sender, receiver = _select_issued_wallets(
        wallets,
        balance=lambda wallet: get_validated_usdc_trustline_balance(
            rpc_client,
            wallet.classic_address,
            issuer,
        ),
        symbol="usdc",
        required=USDC_PAYMENT_VALUE,
    )
    currency = serialize_currency(
        IssuedCurrency(currency=xrpl_currency_code("USDC"), issuer=issuer)
    )
    facilitator = _build_facilitator(
        _settings(
            rpc_url=rpc_url,
            recipient=receiver.classic_address,
            allowed_issued_assets=f"USDC:{issuer}",
        )
    )
    merchant, client = _build_merchant(
        facilitator_app=facilitator,
        recipient=receiver.classic_address,
        amount=str(USDC_PAYMENT_VALUE),
        currency=currency,
    )
    before = get_validated_usdc_trustline_balance(
        rpc_client,
        receiver.classic_address,
        issuer,
    )
    result = asyncio.run(
        _perform_charge(
            merchant=merchant,
            facilitator_client=client,
            signer=XRPLPaymentSigner(sender, rpc_url=rpc_url, network="testnet"),
        )
    )
    tx = _assert_common(
        result,
        sender=sender,
        receiver=receiver,
        currency=currency,
        amount=str(USDC_PAYMENT_VALUE),
    )
    amount = tx.get("Amount") or tx.get("DeliverMax")
    assert normalize_currency_code(str(amount["currency"])) == "USDC"
    assert Decimal(str(amount["value"])) == USDC_PAYMENT_VALUE
    after = get_validated_usdc_trustline_balance(
        rpc_client,
        receiver.classic_address,
        issuer,
    )
    assert after - before == USDC_PAYMENT_VALUE


def _select_xrp_wallets(
    client: JsonRpcClient,
    wallets: LiveWalletPair,
    *,
    amount_drops: int,
) -> tuple[Wallet, Wallet]:
    ranked = sorted(
        (
            (wallet, get_validated_balance(client, wallet.classic_address))
            for wallet in wallets.as_list()
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    sender, balance = ranked[0]
    if balance <= amount_drops:
        pytest.skip(
            "Cached Testnet wallets need more XRP; delete "
            f"{wallet_cache_path()} to mint fresh wallets."
        )
    return sender, ranked[1][0]


def _select_issued_wallets(
    wallets: DemoWalletSet,
    *,
    balance: Callable[[Wallet], Decimal],
    symbol: str,
    required: Decimal,
) -> tuple[Wallet, Wallet]:
    ranked = sorted(
        (
            (wallet, balance(wallet))
            for wallet in (wallets.merchant_wallet, wallets.buyer_wallet(symbol))
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    if ranked[0][1] < required:
        funding_command = (
            "python -m devtools.rlusd_fund --target-rlusd 10 --max-xrp 35"
            if symbol.lower() == "rlusd"
            else f"python -m devtools.{symbol}_topup"
        )
        pytest.skip(
            f"Cached {symbol.upper()} wallets need funding; run "
            f"`{funding_command}`."
        )
    return ranked[0][0], ranked[1][0]
