import asyncio
import os
from decimal import Decimal

from fastapi import FastAPI, Request
import httpx
import pytest
from slowapi import Limiter as SlowLimiter
from xrpl.clients import JsonRpcClient
from xrpl.models.requests import Tx
from xrpl.wallet import Wallet

import xrpl_mpp_facilitator.factory as factory_module
from xrpl_mpp_client import (
    XRPLPaymentSigner,
    XRPLPaymentTransport,
    build_payment_authorization,
    decode_payment_challenges_response,
    decode_payment_receipt_header,
)
from xrpl_mpp_core import USDC_TESTNET_ISSUER, decode_challenge_request, normalize_currency_code
from xrpl_mpp_facilitator.config import Settings
from xrpl_mpp_facilitator.factory import create_app
from xrpl_mpp_facilitator.xrpl_service import XRPLService
from xrpl_mpp_middleware import PaymentMiddlewareASGI, XRPLFacilitatorClient, require_payment, require_session
from devtools.live_testnet_support import (
    DEFAULT_RLUSD_TESTNET_ISSUER,
    DEFAULT_USDC_TESTNET_ISSUER,
    DemoWalletSet,
    LIVE_TEST_FLAG,
    RLUSD_TESTNET_ISSUER_ENV,
    USDC_TESTNET_ISSUER_ENV,
    LiveWalletPair,
    consolidate_rlusd_to_wallet_a,
    consolidate_usdc_to_wallet_a,
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

XRP_PAYMENT_DROPS = 2_000_000
RLUSD_PAYMENT_VALUE = Decimal("3.75")
USDC_PAYMENT_VALUE = Decimal("4.5")
LIVE_TEST_BEARER_TOKEN = "live-test-facilitator-token"
LIVE_TEST_CHALLENGE_SECRET = "live-test-mpp-challenge-secret"
FACILITATOR_BASE_URL = "http://facilitator.local"
MERCHANT_BASE_URL = "http://merchant.local"
SESSION_UNIT_DROPS = 250
SESSION_MIN_PREPAY_DROPS = 1000


def _build_live_test_facilitator_app(app_settings: Settings) -> FastAPI:
    redis_client = FakeRedis()
    xrpl_service = XRPLService(app_settings, redis_client=redis_client)
    original_build_rate_limiter = factory_module.build_rate_limiter

    def _build_in_memory_rate_limiter(_settings: Settings):
        return SlowLimiter(key_func=factory_module.get_remote_address)

    factory_module.build_rate_limiter = _build_in_memory_rate_limiter
    try:
        return create_app(app_settings=app_settings, xrpl_service=xrpl_service)
    finally:
        factory_module.build_rate_limiter = original_build_rate_limiter


def _attach_payment_middleware(
    *,
    merchant_app: FastAPI,
    facilitator_app: FastAPI,
    route_configs: dict[str, object],
) -> httpx.AsyncClient:
    facilitator_async_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=facilitator_app),
        base_url=FACILITATOR_BASE_URL,
    )
    merchant_app.add_middleware(
        PaymentMiddlewareASGI,
        route_configs=route_configs,
        challenge_secret=LIVE_TEST_CHALLENGE_SECRET,
        client_factory=lambda facilitator_url, bearer_token: XRPLFacilitatorClient(
            base_url=facilitator_url,
            bearer_token=bearer_token,
            async_client=facilitator_async_client,
        ),
    )
    return facilitator_async_client


async def _perform_public_charge_flow(
    *,
    merchant_app: FastAPI,
    signer: XRPLPaymentSigner,
    path: str,
) -> tuple[httpx.Response, httpx.Response, httpx.Response]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=merchant_app),
        base_url=MERCHANT_BASE_URL,
    ) as merchant_client:
        challenge_response = await merchant_client.get(path)
        challenges = decode_payment_challenges_response(challenge_response.headers)
        if not challenges:
            raise AssertionError("Expected at least one payment challenge")
        credential = await signer.build_charge_credential_async(challenges[0])
        authorization = build_payment_authorization(credential)
        paid_response = await merchant_client.get(path, headers={"Authorization": authorization})
        replay_response = await merchant_client.get(path, headers={"Authorization": authorization})
    return challenge_response, paid_response, replay_response


def _build_charge_merchant_app(
    *,
    facilitator_app: FastAPI,
    pay_to: str,
    network: str,
    xrp_drops: int | None = None,
    amount: str | None = None,
    asset_code: str = "XRP",
    asset_issuer: str | None = None,
) -> tuple[FastAPI, httpx.AsyncClient]:
    merchant_app = FastAPI()

    @merchant_app.get("/paid")
    async def paid(request: Request) -> dict[str, str]:
        payment = request.state.mpp_payment
        return {
            "intent": payment.intent or "",
            "reference": payment.reference,
            "tx_hash": payment.tx_hash or "",
            "payer": payment.payer or "",
        }

    facilitator_async_client = _attach_payment_middleware(
        merchant_app=merchant_app,
        facilitator_app=facilitator_app,
        route_configs={
            "GET /paid": require_payment(
                facilitator_url=FACILITATOR_BASE_URL,
                bearer_token=LIVE_TEST_BEARER_TOKEN,
                pay_to=pay_to,
                network=network,
                xrp_drops=xrp_drops,
                amount=amount,
                asset_code=asset_code,
                asset_issuer=asset_issuer,
                description="Live public MPP charge route",
            )
        },
    )
    return merchant_app, facilitator_async_client


def _build_session_merchant_app(
    *,
    facilitator_app: FastAPI,
    pay_to: str,
    network: str,
) -> tuple[FastAPI, httpx.AsyncClient]:
    merchant_app = FastAPI()

    @merchant_app.get("/metered")
    async def metered(request: Request) -> dict[str, str]:
        payment = request.state.mpp_payment
        return {
            "intent": payment.intent or "",
            "reference": payment.reference,
            "last_action": payment.last_action or "",
            "prepaid_total": payment.prepaid_total or "",
            "spent_total": payment.spent_total or "",
            "available_balance": payment.available_balance or "",
        }

    facilitator_async_client = _attach_payment_middleware(
        merchant_app=merchant_app,
        facilitator_app=facilitator_app,
        route_configs={
            "GET /metered": require_session(
                facilitator_url=FACILITATOR_BASE_URL,
                bearer_token=LIVE_TEST_BEARER_TOKEN,
                pay_to=pay_to,
                network=network,
                xrp_drops=SESSION_UNIT_DROPS,
                min_prepay_amount=str(SESSION_MIN_PREPAY_DROPS),
                description="Live public MPP session route",
            )
        },
    )
    return merchant_app, facilitator_async_client


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get(LIVE_TEST_FLAG) != "1",
    reason=f"Set {LIVE_TEST_FLAG}=1 to run the XRPL Testnet live integration test.",
)
def test_live_xrp_payment_round_trip() -> None:
    rpc_url = resolve_live_testnet_rpc_url()
    client = JsonRpcClient(rpc_url)
    wallets = get_live_wallet_pair(client)
    sender, receiver = _select_xrp_wallets(
        client,
        wallets,
        amount_drops=XRP_PAYMENT_DROPS,
    )

    app_settings = Settings(
        _env_file=None,
        XRPL_RPC_URL=rpc_url,
        MY_DESTINATION_ADDRESS=receiver.classic_address,
        REDIS_URL="redis://fake:6379/0",
        NETWORK_ID="xrpl:1",
        SETTLEMENT_MODE="validated",
        VALIDATION_TIMEOUT=30,
        MIN_XRP_DROPS=1000,
        FACILITATOR_BEARER_TOKEN=LIVE_TEST_BEARER_TOKEN,
        MPP_CHALLENGE_SECRET=LIVE_TEST_CHALLENGE_SECRET,
    )
    facilitator_app = _build_live_test_facilitator_app(app_settings)
    merchant_app, facilitator_async_client = _build_charge_merchant_app(
        facilitator_app=facilitator_app,
        pay_to=receiver.classic_address,
        network="xrpl:1",
        xrp_drops=XRP_PAYMENT_DROPS,
    )
    signer = XRPLPaymentSigner(
        sender,
        rpc_url=rpc_url,
        network="xrpl:1",
    )

    receiver_balance_before = get_validated_balance(client, receiver.classic_address)
    async def _run_flow() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        try:
            return await _perform_public_charge_flow(
                merchant_app=merchant_app,
                signer=signer,
                path="/paid",
            )
        finally:
            await facilitator_async_client.aclose()

    challenge_response, charge_response, replay_response = asyncio.run(_run_flow())
    challenges = decode_payment_challenges_response(challenge_response.headers)
    assert len(challenges) == 1
    challenge = challenges[0]
    request = decode_challenge_request(challenge)
    receipt = decode_payment_receipt_header(charge_response.headers)
    assert receipt is not None

    receiver_balance_after = get_validated_balance(client, receiver.classic_address)
    tx_hash = receipt.tx_hash or ""
    tx_response = client.request(Tx(transaction=tx_hash)).result
    tx_payload = tx_response.get("tx_json") or tx_response.get("tx") or {}
    ledger_tx_hash = tx_response.get("hash") or tx_payload.get("hash")
    ledger_amount = tx_payload.get("Amount") or tx_payload.get("DeliverMax")

    assert challenge_response.status_code == 402
    assert challenge_response.headers["Cache-Control"] == "no-store"
    assert challenge.intent == "charge"
    assert request.recipient == receiver.classic_address
    assert request.currency == "XRP:native"
    assert request.amount == str(XRP_PAYMENT_DROPS)
    assert charge_response.status_code == 200
    assert charge_response.json()["intent"] == "charge"
    assert charge_response.json()["reference"] == tx_hash
    assert charge_response.json()["tx_hash"] == tx_hash
    assert charge_response.json()["payer"] == sender.classic_address
    assert receipt.intent == "charge"
    assert receipt.invoice_id == request.method_details.invoice_id
    assert receipt.tx_hash == tx_hash
    assert receipt.settlement_status == "validated"
    assert receipt.asset is not None and receipt.asset.code == "XRP"
    assert receipt.asset.issuer is None
    assert receipt.amount is not None and receipt.amount.value == str(XRP_PAYMENT_DROPS)
    assert receipt.amount.unit == "drops"
    assert receipt.amount.drops == XRP_PAYMENT_DROPS
    assert replay_response.status_code == 402
    assert "replay attack" in replay_response.json()["detail"].lower()
    assert receiver_balance_after - receiver_balance_before == XRP_PAYMENT_DROPS
    assert tx_response.get("validated") is True
    assert ledger_tx_hash == tx_hash
    assert tx_payload.get("Destination") == receiver.classic_address
    assert ledger_amount == str(XRP_PAYMENT_DROPS)


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get(LIVE_TEST_FLAG) != "1",
    reason=f"Set {LIVE_TEST_FLAG}=1 to run the XRPL Testnet live integration test.",
)
def test_live_xrp_session_round_trip() -> None:
    rpc_url = resolve_live_testnet_rpc_url()
    client = JsonRpcClient(rpc_url)
    wallets = get_live_wallet_pair(client)
    sender, receiver = _select_xrp_wallets(
        client,
        wallets,
        amount_drops=SESSION_MIN_PREPAY_DROPS * 2,
    )

    app_settings = Settings(
        _env_file=None,
        XRPL_RPC_URL=rpc_url,
        MY_DESTINATION_ADDRESS=receiver.classic_address,
        REDIS_URL="redis://fake:6379/0",
        NETWORK_ID="xrpl:1",
        SETTLEMENT_MODE="validated",
        VALIDATION_TIMEOUT=30,
        MIN_XRP_DROPS=1000,
        FACILITATOR_BEARER_TOKEN=LIVE_TEST_BEARER_TOKEN,
        MPP_CHALLENGE_SECRET=LIVE_TEST_CHALLENGE_SECRET,
    )
    facilitator_app = _build_live_test_facilitator_app(app_settings)
    merchant_app, facilitator_async_client = _build_session_merchant_app(
        facilitator_app=facilitator_app,
        pay_to=receiver.classic_address,
        network="xrpl:1",
    )
    signer = XRPLPaymentSigner(
        sender,
        rpc_url=rpc_url,
        network="xrpl:1",
    )

    receiver_balance_before = get_validated_balance(client, receiver.classic_address)

    async def _run_flow() -> tuple[httpx.Response, list[httpx.Response], httpx.Response]:
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=merchant_app),
                base_url=MERCHANT_BASE_URL,
            ) as unpaid_client:
                unpaid_response = await unpaid_client.get("/metered")

            transport = XRPLPaymentTransport(
                signer,
                network="xrpl:1",
                asset="XRP:native",
                base_transport=httpx.ASGITransport(app=merchant_app),
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url=MERCHANT_BASE_URL,
            ) as paid_client:
                responses = [await paid_client.get("/metered") for _ in range(5)]
                close_response = await transport.close_session(f"{MERCHANT_BASE_URL}/metered")
            return unpaid_response, responses, close_response
        finally:
            await facilitator_async_client.aclose()

    unpaid_response, responses, close_response = asyncio.run(_run_flow())
    challenges = decode_payment_challenges_response(unpaid_response.headers)
    assert len(challenges) == 1
    challenge = challenges[0]
    request = decode_challenge_request(challenge)

    open_receipt = decode_payment_receipt_header(responses[0].headers)
    assert open_receipt is not None
    assert open_receipt.intent == "session"
    assert open_receipt.last_action == "open"
    assert open_receipt.session_id
    assert open_receipt.session_token
    assert open_receipt.prepaid_total == str(SESSION_MIN_PREPAY_DROPS)
    assert open_receipt.spent_total == str(SESSION_UNIT_DROPS)
    assert open_receipt.available_balance == str(SESSION_MIN_PREPAY_DROPS - SESSION_UNIT_DROPS)
    assert responses[0].json() == {
        "intent": "session",
        "reference": open_receipt.session_id,
        "last_action": "open",
        "prepaid_total": str(SESSION_MIN_PREPAY_DROPS),
        "spent_total": str(SESSION_UNIT_DROPS),
        "available_balance": str(SESSION_MIN_PREPAY_DROPS - SESSION_UNIT_DROPS),
    }

    for index, response in enumerate(responses[1:4], start=2):
        receipt = decode_payment_receipt_header(response.headers)
        assert receipt is not None
        assert response.status_code == 200
        assert receipt.intent == "session"
        assert receipt.last_action == "use"
        assert receipt.prepaid_total == str(SESSION_MIN_PREPAY_DROPS)
        assert receipt.spent_total == str(SESSION_UNIT_DROPS * index)
        assert receipt.available_balance == str(SESSION_MIN_PREPAY_DROPS - (SESSION_UNIT_DROPS * index))

    top_up_use_receipt = decode_payment_receipt_header(responses[4].headers)
    assert top_up_use_receipt is not None
    assert top_up_use_receipt.intent == "session"
    assert top_up_use_receipt.last_action == "use"
    assert top_up_use_receipt.session_id == open_receipt.session_id
    assert top_up_use_receipt.prepaid_total == str(SESSION_MIN_PREPAY_DROPS * 2)
    assert top_up_use_receipt.spent_total == str((SESSION_UNIT_DROPS * 4) + SESSION_UNIT_DROPS)
    assert top_up_use_receipt.available_balance == str((SESSION_MIN_PREPAY_DROPS * 2) - (SESSION_UNIT_DROPS * 5))
    assert responses[4].json() == {
        "intent": "session",
        "reference": open_receipt.session_id,
        "last_action": "use",
        "prepaid_total": str(SESSION_MIN_PREPAY_DROPS * 2),
        "spent_total": str(SESSION_UNIT_DROPS * 5),
        "available_balance": str((SESSION_MIN_PREPAY_DROPS * 2) - (SESSION_UNIT_DROPS * 5)),
    }

    close_receipt = decode_payment_receipt_header(close_response.headers)
    assert close_receipt is not None
    assert close_response.status_code == 200
    assert close_response.json()["intent"] == "session"
    assert close_response.json()["lastAction"] == "close"
    assert close_response.json()["settlementStatus"] == "session_closed"
    assert close_response.json()["sessionId"] == open_receipt.session_id
    assert close_receipt.intent == "session"
    assert close_receipt.last_action == "close"
    assert close_receipt.session_id == open_receipt.session_id
    assert close_receipt.prepaid_total == str(SESSION_MIN_PREPAY_DROPS * 2)
    assert close_receipt.spent_total == str(SESSION_UNIT_DROPS * 5)
    assert close_receipt.available_balance == str((SESSION_MIN_PREPAY_DROPS * 2) - (SESSION_UNIT_DROPS * 5))
    assert close_receipt.settlement_status == "session_closed"

    receiver_balance_after = get_validated_balance(client, receiver.classic_address)
    assert unpaid_response.status_code == 402
    assert unpaid_response.headers["Cache-Control"] == "no-store"
    assert challenge.intent == "session"
    assert request.recipient == receiver.classic_address
    assert request.currency == "XRP:native"
    assert request.amount == str(SESSION_UNIT_DROPS)
    assert request.method_details.unit_amount == str(SESSION_UNIT_DROPS)
    assert request.method_details.min_prepay_amount == str(SESSION_MIN_PREPAY_DROPS)
    assert receiver_balance_after - receiver_balance_before == SESSION_MIN_PREPAY_DROPS * 2


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get(LIVE_TEST_FLAG) != "1",
    reason=f"Set {LIVE_TEST_FLAG}=1 to run the XRPL Testnet live integration test.",
)
def test_live_rlusd_payment_round_trip() -> None:
    rpc_url = resolve_live_testnet_rpc_url()
    client = JsonRpcClient(rpc_url)
    issuer = os.environ.get(RLUSD_TESTNET_ISSUER_ENV, DEFAULT_RLUSD_TESTNET_ISSUER)
    wallets = get_demo_wallet_set(client)
    recover_tracked_claim_wallets(client, wallets.merchant_wallet, issuer)

    for wallet in (wallets.merchant_wallet, wallets.buyer_wallet("rlusd")):
        ensure_rlusd_trustline(client, wallet, issuer)

    sender, receiver = _select_rlusd_wallets(
        client,
        wallets,
        issuer,
    )

    app_settings = Settings(
        _env_file=None,
        XRPL_RPC_URL=rpc_url,
        MY_DESTINATION_ADDRESS=receiver.classic_address,
        REDIS_URL="redis://fake:6379/0",
        NETWORK_ID="xrpl:1",
        SETTLEMENT_MODE="validated",
        VALIDATION_TIMEOUT=30,
        MIN_XRP_DROPS=1000,
        ALLOWED_ISSUED_ASSETS=f"RLUSD:{issuer}",
        FACILITATOR_BEARER_TOKEN=LIVE_TEST_BEARER_TOKEN,
        MPP_CHALLENGE_SECRET=LIVE_TEST_CHALLENGE_SECRET,
    )
    facilitator_app = _build_live_test_facilitator_app(app_settings)
    merchant_app, facilitator_async_client = _build_charge_merchant_app(
        facilitator_app=facilitator_app,
        pay_to=receiver.classic_address,
        network="xrpl:1",
        amount=str(RLUSD_PAYMENT_VALUE),
        asset_code="RLUSD",
        asset_issuer=issuer,
    )
    signer = XRPLPaymentSigner(
        sender,
        rpc_url=rpc_url,
        network="xrpl:1",
    )

    receiver_balance_before = get_validated_trustline_balance(
        client,
        receiver.classic_address,
        issuer,
    )
    async def _run_flow() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        try:
            return await _perform_public_charge_flow(
                merchant_app=merchant_app,
                signer=signer,
                path="/paid",
            )
        finally:
            await facilitator_async_client.aclose()

    challenge_response, charge_response, replay_response = asyncio.run(_run_flow())
    challenges = decode_payment_challenges_response(challenge_response.headers)
    assert len(challenges) == 1
    challenge = challenges[0]
    request = decode_challenge_request(challenge)
    receipt = decode_payment_receipt_header(charge_response.headers)
    assert receipt is not None

    receiver_balance_after = get_validated_trustline_balance(
        client,
        receiver.classic_address,
        issuer,
    )
    tx_hash = receipt.tx_hash or ""
    tx_response = client.request(Tx(transaction=tx_hash)).result
    tx_payload = tx_response.get("tx_json") or tx_response.get("tx") or {}
    ledger_tx_hash = tx_response.get("hash") or tx_payload.get("hash")
    ledger_amount = tx_payload.get("Amount") or tx_payload.get("DeliverMax")

    assert challenge_response.status_code == 402
    assert challenge.intent == "charge"
    assert request.recipient == receiver.classic_address
    assert request.currency == f"RLUSD:{issuer}"
    assert request.amount == str(RLUSD_PAYMENT_VALUE)
    assert charge_response.status_code == 200
    assert charge_response.json()["intent"] == "charge"
    assert charge_response.json()["reference"] == tx_hash
    assert charge_response.json()["tx_hash"] == tx_hash
    assert charge_response.json()["payer"] == sender.classic_address
    assert receipt.intent == "charge"
    assert receipt.invoice_id == request.method_details.invoice_id
    assert receipt.tx_hash == tx_hash
    assert receipt.settlement_status == "validated"
    assert receipt.asset is not None
    assert receipt.asset.code == "RLUSD"
    assert receipt.asset.issuer == issuer
    assert receipt.amount is not None and receipt.amount.value == str(RLUSD_PAYMENT_VALUE)
    assert receipt.amount.unit == "issued"
    assert receipt.amount.asset.code == "RLUSD"
    assert receipt.amount.asset.issuer == issuer
    assert replay_response.status_code == 402
    assert "replay attack" in replay_response.json()["detail"].lower()
    assert receiver_balance_after - receiver_balance_before == RLUSD_PAYMENT_VALUE
    assert tx_response.get("validated") is True
    assert ledger_tx_hash == tx_hash
    assert tx_payload.get("Destination") == receiver.classic_address
    assert isinstance(ledger_amount, dict)
    assert normalize_currency_code(str(ledger_amount["currency"])) == "RLUSD"
    assert ledger_amount["issuer"] == issuer
    assert Decimal(str(ledger_amount["value"])) == RLUSD_PAYMENT_VALUE
    settle_pair = LiveWalletPair(
        wallet_a=wallets.merchant_wallet,
        wallet_b=wallets.buyer_wallet("rlusd"),
    )
    consolidate_rlusd_to_wallet_a(client, settle_pair, issuer)
    assert get_validated_trustline_balance(client, settle_pair.wallet_a.classic_address, issuer) >= (
        receiver_balance_after - receiver_balance_before
    )
    assert get_validated_trustline_balance(client, settle_pair.wallet_b.classic_address, issuer) == Decimal(
        "0"
    )


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get(LIVE_TEST_FLAG) != "1",
    reason=f"Set {LIVE_TEST_FLAG}=1 to run the XRPL Testnet live integration test.",
)
def test_live_usdc_payment_round_trip() -> None:
    rpc_url = resolve_live_testnet_rpc_url()
    client = JsonRpcClient(rpc_url)
    issuer = os.environ.get(USDC_TESTNET_ISSUER_ENV, DEFAULT_USDC_TESTNET_ISSUER)
    wallets = get_demo_wallet_set(client)
    recover_tracked_usdc_claim_wallets(client, wallets.merchant_wallet, issuer)

    for wallet in (wallets.merchant_wallet, wallets.buyer_wallet("usdc")):
        ensure_usdc_trustline(client, wallet, issuer)

    sender, receiver = _select_usdc_wallets(
        client,
        wallets,
        issuer,
    )

    allowed_issued_assets = "" if issuer == USDC_TESTNET_ISSUER else f"USDC:{issuer}"
    app_settings = Settings(
        _env_file=None,
        XRPL_RPC_URL=rpc_url,
        MY_DESTINATION_ADDRESS=receiver.classic_address,
        REDIS_URL="redis://fake:6379/0",
        NETWORK_ID="xrpl:1",
        SETTLEMENT_MODE="validated",
        VALIDATION_TIMEOUT=30,
        MIN_XRP_DROPS=1000,
        ALLOWED_ISSUED_ASSETS=allowed_issued_assets,
        FACILITATOR_BEARER_TOKEN=LIVE_TEST_BEARER_TOKEN,
        MPP_CHALLENGE_SECRET=LIVE_TEST_CHALLENGE_SECRET,
    )
    facilitator_app = _build_live_test_facilitator_app(app_settings)
    merchant_app, facilitator_async_client = _build_charge_merchant_app(
        facilitator_app=facilitator_app,
        pay_to=receiver.classic_address,
        network="xrpl:1",
        amount=str(USDC_PAYMENT_VALUE),
        asset_code="USDC",
        asset_issuer=issuer,
    )
    signer = XRPLPaymentSigner(
        sender,
        rpc_url=rpc_url,
        network="xrpl:1",
    )

    receiver_balance_before = get_validated_usdc_trustline_balance(
        client,
        receiver.classic_address,
        issuer,
    )
    async def _run_flow() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        try:
            return await _perform_public_charge_flow(
                merchant_app=merchant_app,
                signer=signer,
                path="/paid",
            )
        finally:
            await facilitator_async_client.aclose()

    challenge_response, charge_response, replay_response = asyncio.run(_run_flow())
    challenges = decode_payment_challenges_response(challenge_response.headers)
    assert len(challenges) == 1
    challenge = challenges[0]
    request = decode_challenge_request(challenge)
    receipt = decode_payment_receipt_header(charge_response.headers)
    assert receipt is not None

    receiver_balance_after = get_validated_usdc_trustline_balance(
        client,
        receiver.classic_address,
        issuer,
    )
    tx_hash = receipt.tx_hash or ""
    tx_response = client.request(Tx(transaction=tx_hash)).result
    tx_payload = tx_response.get("tx_json") or tx_response.get("tx") or {}
    ledger_tx_hash = tx_response.get("hash") or tx_payload.get("hash")
    ledger_amount = tx_payload.get("Amount") or tx_payload.get("DeliverMax")

    assert challenge_response.status_code == 402
    assert challenge.intent == "charge"
    assert request.recipient == receiver.classic_address
    assert request.currency == f"USDC:{issuer}"
    assert request.amount == str(USDC_PAYMENT_VALUE)
    assert charge_response.status_code == 200
    assert charge_response.json()["intent"] == "charge"
    assert charge_response.json()["reference"] == tx_hash
    assert charge_response.json()["tx_hash"] == tx_hash
    assert charge_response.json()["payer"] == sender.classic_address
    assert receipt.intent == "charge"
    assert receipt.invoice_id == request.method_details.invoice_id
    assert receipt.tx_hash == tx_hash
    assert receipt.settlement_status == "validated"
    assert receipt.asset is not None
    assert receipt.asset.code == "USDC"
    assert receipt.asset.issuer == issuer
    assert receipt.amount is not None and receipt.amount.value == str(USDC_PAYMENT_VALUE)
    assert receipt.amount.unit == "issued"
    assert receipt.amount.asset.code == "USDC"
    assert receipt.amount.asset.issuer == issuer
    assert replay_response.status_code == 402
    assert "replay attack" in replay_response.json()["detail"].lower()
    assert receiver_balance_after - receiver_balance_before == USDC_PAYMENT_VALUE
    assert tx_response.get("validated") is True
    assert ledger_tx_hash == tx_hash
    assert tx_payload.get("Destination") == receiver.classic_address
    assert isinstance(ledger_amount, dict)
    assert normalize_currency_code(str(ledger_amount["currency"])) == "USDC"
    assert ledger_amount["issuer"] == issuer
    assert Decimal(str(ledger_amount["value"])) == USDC_PAYMENT_VALUE
    settle_pair = LiveWalletPair(
        wallet_a=wallets.merchant_wallet,
        wallet_b=wallets.buyer_wallet("usdc"),
    )
    consolidate_usdc_to_wallet_a(client, settle_pair, issuer)
    assert get_validated_usdc_trustline_balance(client, settle_pair.wallet_a.classic_address, issuer) >= (
        receiver_balance_after - receiver_balance_before
    )
    assert get_validated_usdc_trustline_balance(client, settle_pair.wallet_b.classic_address, issuer) == Decimal(
        "0"
    )


def _select_xrp_wallets(
    client: JsonRpcClient,
    wallets: LiveWalletPair,
    *,
    amount_drops: int,
) -> tuple[Wallet, Wallet]:
    wallet_balances = [
        (wallet, get_validated_balance(client, wallet.classic_address))
        for wallet in wallets.as_list()
    ]
    wallet_balances.sort(key=lambda entry: entry[1], reverse=True)
    sender, sender_balance = wallet_balances[0]
    receiver, _receiver_balance = wallet_balances[1]
    if sender_balance <= amount_drops:
        pytest.skip(
            "Cached XRPL Testnet wallets do not have enough XRP left. "
            f"Delete {wallet_cache_path()} to mint a fresh wallet pair."
        )
    return sender, receiver


def _select_rlusd_wallets(
    client: JsonRpcClient,
    wallets: DemoWalletSet,
    issuer: str,
) -> tuple[Wallet, Wallet]:
    sender, receiver = _wallet_with_rlusd_liquidity(client, wallets, issuer)
    if sender is not None and receiver is not None:
        return sender, receiver

    pytest.skip(
        "Cached RLUSD test wallets do not have enough balance after tracked-wallet recovery. "
        "Run `python -m devtools.rlusd_topup` to replenish the accumulator and retry."
    )


def _wallet_with_rlusd_liquidity(
    client: JsonRpcClient,
    wallets: DemoWalletSet,
    issuer: str,
) -> tuple[Wallet | None, Wallet | None]:
    wallet_balances = [
        (wallet, get_validated_trustline_balance(client, wallet.classic_address, issuer))
        for wallet in (wallets.merchant_wallet, wallets.buyer_wallet("rlusd"))
    ]
    wallet_balances.sort(key=lambda entry: entry[1], reverse=True)
    sender, sender_balance = wallet_balances[0]
    receiver, _receiver_balance = wallet_balances[1]
    if sender_balance >= RLUSD_PAYMENT_VALUE:
        return sender, receiver
    return None, None


def _select_usdc_wallets(
    client: JsonRpcClient,
    wallets: DemoWalletSet,
    issuer: str,
) -> tuple[Wallet, Wallet]:
    sender, receiver = _wallet_with_usdc_liquidity(client, wallets, issuer)
    if sender is not None and receiver is not None:
        return sender, receiver

    pytest.skip(
        "Cached USDC test wallets do not have enough balance after tracked-wallet recovery. "
        "Run `python -m devtools.usdc_topup` to prepare a manual Circle faucet claim and retry."
    )


def _wallet_with_usdc_liquidity(
    client: JsonRpcClient,
    wallets: DemoWalletSet,
    issuer: str,
) -> tuple[Wallet | None, Wallet | None]:
    wallet_balances = [
        (wallet, get_validated_usdc_trustline_balance(client, wallet.classic_address, issuer))
        for wallet in (wallets.merchant_wallet, wallets.buyer_wallet("usdc"))
    ]
    wallet_balances.sort(key=lambda entry: entry[1], reverse=True)
    sender, sender_balance = wallet_balances[0]
    receiver, _receiver_balance = wallet_balances[1]
    if sender_balance >= USDC_PAYMENT_VALUE:
        return sender, receiver
    return None, None
