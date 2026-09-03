from __future__ import annotations

import asyncio
from decimal import Decimal
import importlib
from pathlib import Path

import httpx
import pytest
from xrpl.core import binarycodec
from xrpl.models.transactions import Payment
from xrpl.wallet import Wallet

from xrpl_mpp_client import PaymentPolicyError, derive_paychannel_open_binding
from xrpl_mpp_core import (
    PAYMENT_AUTHORIZATION_HEADER,
    FacilitatorSupportedMethod,
    FacilitatorSupportedResponse,
    IssuedCurrency,
    PaymentReceipt,
    RLUSD_TESTNET_ISSUER,
    XRPLChargeMethodDetails,
    XRPLChargeRequest,
    XRPLSessionMethodDetails,
    XRPLSessionRequest,
    build_payment_challenge,
    challenge_invoice_id,
    decode_charge_payload,
    decode_challenge_request,
    decode_payment_credential,
    decode_session_payload,
    encode_payment_receipt,
    render_payment_challenge,
    serialize_currency,
    xrpl_currency_code,
)

FACILITATOR_TOKEN = "example-facilitator-token"
DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
TX_HASH = "A" * 64
CHALLENGE_SECRET = "example-challenge-secret"
INVOICE_ID = "B" * 64
CHANNEL_ID = "C" * 64
RLUSD_CURRENCY = serialize_currency(
    IssuedCurrency(
        currency=xrpl_currency_code("RLUSD"),
        issuer=RLUSD_TESTNET_ISSUER,
    )
)


class FakeFacilitatorClient:
    async def startup(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def get_supported(self, *, force_refresh: bool = False) -> FacilitatorSupportedResponse:
        return FacilitatorSupportedResponse(
            methods=[
                FacilitatorSupportedMethod(
                    method="xrpl",
                    intents=["charge", "session"],
                    network="testnet",
                    currencies=["XRP", RLUSD_CURRENCY],
                    settlementMode="validated",
                )
            ]
        )

    async def charge(self, credential):
        payload = decode_charge_payload(credential)
        assert payload.type == "transaction"
        transaction = Payment.from_xrpl(binarycodec.decode(payload.blob))
        terms = decode_challenge_request(credential.challenge)
        details = terms.method_details
        invoice_id = (
            details.invoice_id
            if details is not None and details.invoice_id is not None
            else challenge_invoice_id(credential.challenge.id)
        )
        tx_hash = transaction.get_hash().upper()
        return PaymentReceipt(
            status="success",
            method="xrpl",
            timestamp="2026-03-21T12:00:00Z",
            reference=tx_hash,
            challengeId=credential.challenge.id,
            network="testnet",
            payer=transaction.account,
            recipient=terms.recipient,
            invoiceId=invoice_id,
            txHash=tx_hash,
            settlementStatus="validated",
        )

    async def session(self, credential):
        raise AssertionError("session not used in charge example tests")


def test_merchant_example_uses_canonical_issued_currency(monkeypatch) -> None:
    monkeypatch.setenv("FACILITATOR_URL", "http://facilitator.local")
    monkeypatch.setenv("FACILITATOR_TOKEN", FACILITATOR_TOKEN)
    monkeypatch.setenv("MERCHANT_XRPL_ADDRESS", DESTINATION)
    monkeypatch.setenv("XRPL_NETWORK", "testnet")
    monkeypatch.setenv("PRICE_CURRENCY", RLUSD_CURRENCY)
    monkeypatch.setenv("PRICE_AMOUNT", "1.25")

    merchant = importlib.reload(importlib.import_module("examples.merchant_fastapi.app"))
    option = merchant.build_premium_route_config().charge_options[0]

    assert option.network == "testnet"
    assert option.currency == RLUSD_CURRENCY
    assert option.amount == "1.25"
    assert merchant.build_premium_route_config().allow_insecure_facilitator_http is False


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://localhost:8000", True),
        ("http://localhost.:8000", True),
        ("http://127.0.0.1:8000", True),
        ("http://[::1]:8000", True),
        ("https://127.0.0.1:8000", False),
        ("http://facilitator.example:8000", False),
        ("not-a-url", False),
    ],
)
def test_seller_insecure_facilitator_helper_is_loopback_only(
    url: str,
    expected: bool,
) -> None:
    from examples._facilitator import allow_insecure_loopback_facilitator

    assert allow_insecure_loopback_facilitator(url) is expected


def test_example_spend_cap_conversion_uses_wire_units() -> None:
    from examples._policy import spend_cap_to_policy_amount

    assert spend_cap_to_policy_amount(currency="XRP", max_spend="0.01") == "10000"
    assert (
        spend_cap_to_policy_amount(currency=RLUSD_CURRENCY, max_spend="1.25")
        == "1.25"
    )
    with pytest.raises(ValueError, match="whole number of XRP drops"):
        spend_cap_to_policy_amount(currency="XRP", max_spend="0.0000001")


def test_all_seller_examples_reject_remote_plaintext_facilitator_opt_in(
    monkeypatch,
) -> None:
    monkeypatch.setenv("FACILITATOR_URL", "http://facilitator.example:8000")

    merchant = importlib.reload(importlib.import_module("examples.merchant_fastapi.app"))
    minimal = importlib.reload(importlib.import_module("examples.seller_minimal"))
    paychannel = importlib.reload(importlib.import_module("examples.seller_paychannel"))

    assert merchant.build_premium_route_config().allow_insecure_facilitator_http is False
    assert minimal.build_premium_route_config().allow_insecure_facilitator_http is False
    assert (
        paychannel._session_route(amount=0, description="open")
        .allow_insecure_facilitator_http
        is False
    )


def test_buyer_example_passes_currency_preference(monkeypatch) -> None:
    buyer = importlib.reload(importlib.import_module("examples.buyer_httpx"))
    signer = buyer.XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        autofill_enabled=False,
    )
    captured: dict[str, object] = {}

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url: str) -> httpx.Response:
            return httpx.Response(200, request=httpx.Request("GET", url))

    def fake_wrap(
        _signer,
        *,
        currency=None,
        transport=None,
        timeout=None,
        payment_policy=None,
        allow_insecure_localhost=False,
        **_kwargs,
    ):
        captured.update(
            currency=currency,
            transport=transport,
            timeout=timeout,
            payment_policy=payment_policy,
            allow_insecure_localhost=allow_insecure_localhost,
        )
        return DummyClient()

    monkeypatch.setenv("PAYMENT_CURRENCY", RLUSD_CURRENCY)
    monkeypatch.setenv("XRPL_MPP_EXPECTED_RECIPIENT", DESTINATION)
    monkeypatch.setenv("XRPL_MPP_MAX_SPEND", "1.25")
    monkeypatch.setattr(buyer, "wrap_httpx_with_mpp_payment", fake_wrap)

    response = asyncio.run(
        buyer.fetch_paid_resource(
            signer=signer,
            target_url="http://127.0.0.1/premium",
        )
    )
    assert response.status_code == 200
    assert captured["currency"] == RLUSD_CURRENCY
    assert captured["timeout"] == buyer.DEFAULT_REQUEST_TIMEOUT_SECONDS
    policy = captured["payment_policy"]
    assert policy.expected_recipients == frozenset({DESTINATION})
    assert policy.max_amount == Decimal("1.25")
    assert policy.allowed_currencies == frozenset({RLUSD_CURRENCY})
    assert captured["allow_insecure_localhost"] is True


def test_buyer_httpx_example_round_trips_with_real_transport_policy(monkeypatch) -> None:
    monkeypatch.setenv("FACILITATOR_URL", "https://facilitator.example")
    monkeypatch.setenv("FACILITATOR_TOKEN", FACILITATOR_TOKEN)
    monkeypatch.setenv("MERCHANT_XRPL_ADDRESS", DESTINATION)
    monkeypatch.setenv("XRPL_NETWORK", "testnet")
    monkeypatch.setenv("PRICE_AMOUNT", "1000")
    monkeypatch.setenv("PRICE_CURRENCY", "XRP")
    monkeypatch.setenv("MPP_CHALLENGE_SECRET", CHALLENGE_SECRET)

    merchant = importlib.reload(importlib.import_module("examples.merchant_fastapi.app"))
    buyer = importlib.reload(importlib.import_module("examples.buyer_httpx"))
    app = merchant.create_app(
        client_factory=lambda _url, _token: FakeFacilitatorClient()
    )
    signer = buyer.XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        autofill_enabled=False,
    )

    response = asyncio.run(
        buyer.fetch_paid_resource(
            signer=signer,
            target_url="http://127.0.0.1/premium",
            payment_currency="XRP",
            expected_recipient=DESTINATION,
            max_payment_amount="1000",
            transport=httpx.ASGITransport(app=app),
        )
    )

    assert response.status_code == 200
    assert response.json()["payer"] == signer.wallet.classic_address
    assert "Payment-Receipt" in response.headers


def test_minimal_examples_round_trip_mpp_02_charge(monkeypatch) -> None:
    monkeypatch.setenv("FACILITATOR_URL", "http://facilitator.local")
    monkeypatch.setenv("FACILITATOR_TOKEN", FACILITATOR_TOKEN)
    monkeypatch.setenv("MERCHANT_XRPL_ADDRESS", DESTINATION)
    monkeypatch.setenv("XRPL_NETWORK", "testnet")
    monkeypatch.setenv("PRICE_AMOUNT", "1000")
    monkeypatch.setenv("MPP_CHALLENGE_SECRET", CHALLENGE_SECRET)

    seller = importlib.reload(importlib.import_module("examples.seller_minimal"))
    buyer = importlib.reload(importlib.import_module("examples.buyer_minimal"))
    app = seller.create_app(client_factory=lambda _url, _token: FakeFacilitatorClient())
    signer = buyer.XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        autofill_enabled=False,
    )

    response = asyncio.run(
        buyer.fetch_premium(
            signer=signer,
            base_url="http://127.0.0.1",
            payment_currency="XRP",
            expected_recipient=DESTINATION,
            max_payment_amount="1000",
            transport=httpx.ASGITransport(app=app),
        )
    )
    assert response.status_code == 200
    response_body = response.json()
    assert response_body["message"] == "premium content unlocked"
    assert response_body["payer"] == signer.wallet.classic_address
    assert len(response_body["tx_hash"]) == 64
    assert set(response_body["tx_hash"]) <= set("0123456789ABCDEF")
    assert "Payment-Receipt" in response.headers


def test_buyer_example_loads_named_network_and_currency_from_dotenv(
    monkeypatch,
    tmp_path: Path,
) -> None:
    wallet = Wallet.create()
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                f"XRPL_WALLET_SEED={wallet.seed}",
                "XRPL_NETWORK=mainnet",
                "XRPL_RPC_URL=https://mainnet.example.invalid:51234",
                f"PAYMENT_CURRENCY={RLUSD_CURRENCY}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    for name in ("XRPL_WALLET_SEED", "XRPL_NETWORK", "XRPL_RPC_URL", "PAYMENT_CURRENCY"):
        monkeypatch.delenv(name, raising=False)

    buyer = importlib.reload(importlib.import_module("examples.buyer_httpx"))
    signer = buyer.build_signer_from_env()
    assert signer.network == "mainnet"
    assert buyer.rpc_url_from_env() == "https://mainnet.example.invalid:51234"
    assert buyer.payment_currency_from_env() == RLUSD_CURRENCY


def test_demo_trace_config_converts_operator_xrp_cap_to_drops(monkeypatch) -> None:
    trace = importlib.reload(importlib.import_module("devtools.demo_trace"))
    monkeypatch.setenv("XRPL_WALLET_SEED", Wallet.create().seed or "")
    monkeypatch.setenv("XRPL_NETWORK", "testnet")
    monkeypatch.setenv("PAYMENT_CURRENCY", "XRP")
    monkeypatch.setenv("XRPL_MPP_EXPECTED_RECIPIENT", DESTINATION)
    monkeypatch.setenv("XRPL_MPP_MAX_SPEND", "0.01")

    config = trace.resolve_config(
        env_file=None,
        target_url="http://127.0.0.1/premium",
        timeout_seconds=1,
    )

    assert config.expected_recipient == DESTINATION
    assert config.max_payment_amount == "10000"


def _charge_challenge(
    *,
    header: str | None = None,
    amount: str = "1.25",
    recipient: str = DESTINATION,
) -> object:
    return build_payment_challenge(
        secret=CHALLENGE_SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="charge",
        request_model=XRPLChargeRequest(
            amount=amount,
            currency=RLUSD_CURRENCY,
            recipient=recipient,
            methodDetails=XRPLChargeMethodDetails(
                network="testnet",
                invoiceId=INVOICE_ID,
            ),
        ),
        expires_in_seconds=300,
        header=header,
    )


def test_demo_trace_renders_core_receipt_and_canonical_currency(monkeypatch) -> None:
    trace = importlib.reload(importlib.import_module("devtools.demo_trace"))
    wallet = Wallet.create()
    signer = trace.XRPLPaymentSigner(
        wallet,
        network="testnet",
        autofill_enabled=False,
    )
    challenge = _charge_challenge(header=PAYMENT_AUTHORIZATION_HEADER)
    xrp = {
        DESTINATION: [2_000_000, 2_000_000],
        wallet.classic_address: [10_000_000, 9_999_988],
    }
    issued = {
        DESTINATION: [Decimal("4"), Decimal("5.25")],
        wallet.classic_address: [Decimal("7"), Decimal("5.75")],
    }
    monkeypatch.setattr(trace, "get_validated_balance", lambda _c, address: xrp[address].pop(0))
    monkeypatch.setattr(
        trace,
        "get_validated_trustline_balance",
        lambda _c, address, issuer, *, currency_code: issued[address].pop(0),
    )

    retry_seen = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if PAYMENT_AUTHORIZATION_HEADER not in request.headers:
            return httpx.Response(
                402,
                headers={"WWW-Authenticate": render_payment_challenge(challenge)},
                request=request,
            )
        retry_seen["value"] = True
        assert "authorization" not in request.headers
        credential = decode_payment_credential(
            request.headers[PAYMENT_AUTHORIZATION_HEADER].removeprefix("Payment ")
        )
        payload = decode_charge_payload(credential)
        assert payload.type == "transaction"
        transaction = Payment.from_xrpl(binarycodec.decode(payload.blob))
        reference = transaction.get_hash().upper()
        receipt = PaymentReceipt(
            status="success",
            method="xrpl",
            timestamp="2026-03-21T12:00:00Z",
            reference=reference,
            challengeId=challenge.id,
            network="testnet",
            payer=wallet.classic_address,
            recipient=DESTINATION,
            invoiceId=INVOICE_ID,
            txHash=reference,
            settlementStatus="validated",
        )
        return httpx.Response(
            200,
            json={"message": "premium content unlocked"},
            headers={"Payment-Receipt": encode_payment_receipt(receipt)},
            request=request,
        )

    result = asyncio.run(
        trace.run_demo_trace(
            signer=signer,
            rpc_client=object(),
            target_url="http://127.0.0.1/premium",
            payment_currency=RLUSD_CURRENCY,
            expected_recipient=DESTINATION,
            max_payment_amount="1.25",
            transport=httpx.MockTransport(handler),
        )
    )
    output = trace.render_trace(result)
    assert retry_seen["value"] is True
    assert result.payment_receipt is not None
    assert "amount: 1.25 RLUSD" in output
    assert f"invoice id: {INVOICE_ID}" in output
    assert f"tx hash: {result.payment_receipt.reference}" in output
    assert "MPP payment receipt" in output


def test_demo_trace_rejects_challenge_outside_operator_policy(monkeypatch) -> None:
    trace = importlib.reload(importlib.import_module("devtools.demo_trace"))
    signer = trace.XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        autofill_enabled=False,
    )
    challenge = _charge_challenge(amount="1.25")
    monkeypatch.setattr(
        trace,
        "snapshot_wallet",
        lambda *_args, **_kwargs: pytest.fail("policy must run before wallet inspection"),
    )

    with pytest.raises(PaymentPolicyError, match="exceeds"):
        asyncio.run(
            trace.run_demo_trace(
                signer=signer,
                rpc_client=object(),
                target_url="http://127.0.0.1/premium",
                payment_currency=RLUSD_CURRENCY,
                expected_recipient=DESTINATION,
                max_payment_amount="1.00",
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        402,
                        headers={
                            "WWW-Authenticate": render_payment_challenge(challenge)
                        },
                        request=request,
                    )
                ),
            )
        )


def test_demo_trace_rejects_forged_receipt_reference(monkeypatch) -> None:
    trace = importlib.reload(importlib.import_module("devtools.demo_trace"))
    wallet = Wallet.create()
    signer = trace.XRPLPaymentSigner(
        wallet,
        network="testnet",
        autofill_enabled=False,
    )
    challenge = _charge_challenge()
    xrp = {DESTINATION: 2_000_000, wallet.classic_address: 10_000_000}
    issued = {DESTINATION: Decimal("5"), wallet.classic_address: Decimal("5")}
    monkeypatch.setattr(trace, "get_validated_balance", lambda _c, address: xrp[address])
    monkeypatch.setattr(
        trace,
        "get_validated_trustline_balance",
        lambda _c, address, _issuer, *, currency_code: issued[address],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "authorization" not in request.headers:
            return httpx.Response(
                402,
                headers={"WWW-Authenticate": render_payment_challenge(challenge)},
                request=request,
            )
        forged = PaymentReceipt(
            status="success",
            method="xrpl",
            timestamp="2026-03-21T12:00:00Z",
            reference=TX_HASH,
        )
        return httpx.Response(
            200,
            headers={"Payment-Receipt": encode_payment_receipt(forged)},
            request=request,
        )

    with pytest.raises(ValueError, match="reference does not match"):
        asyncio.run(
            trace.run_demo_trace(
                signer=signer,
                rpc_client=object(),
                target_url="http://127.0.0.1/premium",
                payment_currency=RLUSD_CURRENCY,
                expected_recipient=DESTINATION,
                max_payment_amount="1.25",
                transport=httpx.MockTransport(handler),
            )
        )


def test_demo_trace_blocks_unfunded_issued_currency(monkeypatch) -> None:
    trace = importlib.reload(importlib.import_module("devtools.demo_trace"))
    wallet = Wallet.create()
    signer = trace.XRPLPaymentSigner(
        wallet,
        network="testnet",
        autofill_enabled=False,
    )
    challenge = _charge_challenge()
    xrp = {DESTINATION: 2_000_000, wallet.classic_address: 10_000_000}
    issued = {DESTINATION: Decimal("30"), wallet.classic_address: Decimal("0")}
    monkeypatch.setattr(trace, "get_validated_balance", lambda _c, address: xrp[address])
    monkeypatch.setattr(
        trace,
        "get_validated_trustline_balance",
        lambda _c, address, _issuer, *, currency_code: issued[address],
    )

    with pytest.raises(trace.DemoPreflightError, match="only has 0 RLUSD"):
        asyncio.run(
            trace.run_demo_trace(
                signer=signer,
                rpc_client=object(),
                target_url="http://127.0.0.1/premium",
                payment_currency=RLUSD_CURRENCY,
                expected_recipient=DESTINATION,
                max_payment_amount="1.25",
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        402,
                        headers={"WWW-Authenticate": render_payment_challenge(challenge)},
                        request=request,
                    )
                ),
            )
        )


def _session_challenge(*, path: str, amount: str, channel_id: str, cumulative: str | None):
    return build_payment_challenge(
        secret=CHALLENGE_SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="session",
        request_model=XRPLSessionRequest(
            amount=amount,
            currency="XRP",
            channelId=channel_id,
            recipient=DESTINATION,
            methodDetails=XRPLSessionMethodDetails(
                network="testnet",
                cumulativeAmount=cumulative,
            ),
        ),
        expires_in_seconds=300,
    )


def test_paychannel_example_runs_open_voucher_voucher_close() -> None:
    buyer = importlib.reload(importlib.import_module("examples.buyer_paychannel"))
    signer = buyer.XRPLPaymentSigner(
        Wallet.create(),
        network="testnet",
        autofill_enabled=False,
        expected_recipient=DESTINATION,
        allowed_currencies={"XRP"},
    )
    cumulative = {"value": 0}
    channel = {"value": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "authorization" not in request.headers:
            if path == "/channel/open":
                challenge = _session_challenge(
                    path=path,
                    amount="0",
                    channel_id="",
                    cumulative=None,
                )
            elif path == "/metered":
                challenge = _session_challenge(
                    path=path,
                    amount="250",
                    channel_id=channel["value"],
                    cumulative=str(cumulative["value"]),
                )
            else:
                challenge = _session_challenge(
                    path=path,
                    amount="0",
                    channel_id=channel["value"],
                    cumulative=str(cumulative["value"]),
                )
            return httpx.Response(
                402,
                headers={"WWW-Authenticate": render_payment_challenge(challenge)},
                request=request,
            )

        credential = decode_payment_credential(
            request.headers["authorization"].removeprefix("Payment ")
        )
        payload = decode_session_payload(credential)
        if path == "/channel/open":
            binding = derive_paychannel_open_binding(payload.transaction)
            channel["value"] = binding.channel_id
            reference = f"open:{binding.channel_id}:{binding.tx_hash}"
        else:
            cumulative["value"] = int(payload.amount)
            reference = f"{channel['value']}:{payload.amount}"
        receipt = PaymentReceipt(
            status="success",
            method="xrpl",
            timestamp="2026-03-21T12:00:00Z",
            reference=reference,
        )
        return httpx.Response(
            200,
            headers={"Payment-Receipt": encode_payment_receipt(receipt)},
            request=request,
        )

    receipts = asyncio.run(
        buyer.run_paychannel_flow(
            signer=signer,
            merchant_address=DESTINATION,
            target_base_url="http://127.0.0.1",
            funding_drops="1000000",
            request_count=2,
            transport=httpx.MockTransport(handler),
        )
    )
    assert len(receipts) == 4
    assert receipts[0].reference.startswith(f"open:{channel['value']}:")
    assert [receipt.reference for receipt in receipts[1:]] == [
        f"{channel['value']}:250",
        f"{channel['value']}:500",
        f"{channel['value']}:500",
    ]


def test_paychannel_seller_uses_zero_cost_lifecycle_and_cumulative_unit(monkeypatch) -> None:
    monkeypatch.setenv("MERCHANT_XRPL_ADDRESS", DESTINATION)
    monkeypatch.setenv("XRPL_NETWORK", "testnet")
    monkeypatch.setenv("SESSION_UNIT_DROPS", "250")
    seller = importlib.reload(importlib.import_module("examples.seller_paychannel"))
    assert seller._session_route(amount=0, description="open").session_options[0].amount == "0"
    assert seller._session_route(amount=250, description="unit").session_options[0].amount == "250"
