from __future__ import annotations

import asyncio
from decimal import Decimal
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from typer.testing import CliRunner
from xrpl.core import binarycodec
from xrpl.models.transactions import Transaction
from xrpl.wallet import Wallet

from xrpl_mpp_client import (
    AUTHORIZATION_HEADER,
    PAYMENT_RECEIPT_HEADER,
    PaymentPolicyError,
    WWW_AUTHENTICATE_HEADER,
    XRPLPaymentSigner,
    derive_paychannel_open_binding,
)
from xrpl_mpp_core import (
    ACCEPT_PAYMENT_HEADER,
    PAYMENT_AUTHORIZATION_HEADER,
    PaymentReceipt,
    XRPLChargeMethodDetails,
    XRPLChargeRequest,
    XRPLSessionMethodDetails,
    XRPLSessionRequest,
    build_payment_challenge,
    decode_charge_payload,
    decode_payment_credential,
    decode_session_payload,
    encode_payment_receipt,
    render_payment_challenge,
)
from xrpl_mpp_payer import ReceiptRecord, create_proxy_app
from xrpl_mpp_payer.cli import app
from xrpl_mpp_payer.mcp import budget_status as mcp_budget_status
from xrpl_mpp_payer.mcp import close_channel as mcp_close_channel
from xrpl_mpp_payer.mcp import list_receipts as mcp_list_receipts
from xrpl_mpp_payer.mcp import pay_url as mcp_pay_url
from xrpl_mpp_payer.mcp import proxy_mode as mcp_proxy_mode
from xrpl_mpp_payer.payer import (
    DEFAULT_MAINNET_RPC_URL,
    PayResult,
    XRPLPayer,
    budget_status,
    build_signer_from_env,
    payment_challenge_amount,
    resolve_currency,
    resolve_spend_cap,
)
from xrpl_mpp_payer.proxy import ProxyManager
from xrpl_mpp_payer.receipts import ReceiptStore

DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
PAYER = "rf5kMNrUqgLzJT8YUzxM1pptc5r3Lfx1J9"
URL = "https://merchant.example/premium"
CHANNEL_ID = "C" * 64
OPEN_CHANNEL_ID = "D" * 64
SECRET = "payer-test-secret-at-least-32-bytes"
RUNNER = CliRunner()


def _charge_challenge(
    *,
    amount: str = "1000",
    currency: str = "XRP",
    header: str | None = None,
    expires_in_seconds: int = 300,
):
    return build_payment_challenge(
        secret=SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="charge",
        request_model=XRPLChargeRequest(
            amount=amount,
            currency=currency,
            recipient=DESTINATION,
            methodDetails=XRPLChargeMethodDetails(
                network="testnet",
                invoiceId="A" * 64,
            ),
        ),
        expires_in_seconds=expires_in_seconds,
        header=header,
    )


def _session_challenge(
    *,
    channel_id: str,
    amount: str = "25",
    cumulative: str = "100",
):
    return build_payment_challenge(
        secret=SECRET,
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


def _receipt(
    *,
    challenge_id: str,
    reference: str = "B" * 64,
    action: str | None = None,
    channel_id: str | None = None,
    cumulative: str | None = None,
    payer: str = PAYER,
) -> PaymentReceipt:
    return PaymentReceipt(
        status="success",
        method="xrpl",
        timestamp="2026-08-30T12:00:00Z",
        reference=reference,
        challengeId=challenge_id,
        network="testnet",
        payer=payer,
        recipient=DESTINATION,
        action=action,
        channelId=channel_id,
        cumulative=cumulative,
        txHash=reference if len(reference) == 64 and ":" not in reference else None,
        settlementStatus="validated",
    )


def _signer() -> XRPLPaymentSigner:
    return XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)


def _payer(path) -> XRPLPayer:
    return XRPLPayer(
        _signer(),
        store=ReceiptStore(path),
        expected_recipient=DESTINATION,
    )


def _credential_from_request(request: httpx.Request):
    value = request.headers.get(PAYMENT_AUTHORIZATION_HEADER)
    if value is None:
        value = request.headers[AUTHORIZATION_HEADER]
    return decode_payment_credential(value.removeprefix("Payment "))


def _charge_reference_from_request(request: httpx.Request) -> str:
    payload = decode_charge_payload(_credential_from_request(request))
    assert payload.type == "transaction"
    return Transaction.from_xrpl(binarycodec.decode(payload.blob)).get_hash().upper()


def _charge_payer_from_request(request: httpx.Request) -> str:
    payload = decode_charge_payload(_credential_from_request(request))
    assert payload.type == "transaction"
    return Transaction.from_xrpl(binarycodec.decode(payload.blob)).account


def test_charge_uses_accept_payment_selected_header_and_one_retry(tmp_path) -> None:
    store = ReceiptStore(tmp_path / "receipts.jsonl")
    payer = XRPLPayer(_signer(), store=store, expected_recipient=DESTINATION)
    challenge = _charge_challenge(header=PAYMENT_AUTHORIZATION_HEADER)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers[ACCEPT_PAYMENT_HEADER] == "xrpl/charge, xrpl/session"
        assert request.headers[AUTHORIZATION_HEADER] == "Bearer identity"
        if calls == 1:
            return httpx.Response(
                402,
                headers={WWW_AUTHENTICATE_HEADER: render_payment_challenge(challenge)},
                request=request,
            )
        assert request.headers[PAYMENT_AUTHORIZATION_HEADER].startswith("Payment ")
        assert decode_charge_payload(_credential_from_request(request)).type == "transaction"
        receipt = _receipt(
            challenge_id=challenge.id,
            reference=_charge_reference_from_request(request),
            payer=_charge_payer_from_request(request),
        )
        return httpx.Response(
            200,
            headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(receipt)},
            text="paid content",
            request=request,
        )

    result = asyncio.run(
        payer.pay(
            url=URL,
            headers={AUTHORIZATION_HEADER: "Bearer identity"},
            transport=httpx.MockTransport(handler),
        )
    )

    assert result.paid is True
    assert result.text == "paid content"
    assert calls == 2
    [record] = store.list()
    assert record.network == "testnet"
    assert record.currency == "XRP"
    assert record.amount == "0.001"
    assert record.reference == result.payment_response.reference
    assert record.intent == "charge"


def test_pay_keeps_caller_transport_open(tmp_path) -> None:
    payer = _payer(tmp_path / "receipts.jsonl")
    challenge = _charge_challenge()

    class StrictTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.closed = False
            self.calls = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if self.closed:
                raise RuntimeError("transport reused after close")
            self.calls += 1
            if self.calls == 1:
                return httpx.Response(
                    402,
                    headers={WWW_AUTHENTICATE_HEADER: render_payment_challenge(challenge)},
                    request=request,
                )
            receipt = _receipt(
                challenge_id=challenge.id,
                reference=_charge_reference_from_request(request),
                payer=_charge_payer_from_request(request),
            )
            return httpx.Response(
                200,
                headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(receipt)},
                request=request,
            )

        async def aclose(self) -> None:
            self.closed = True

    transport = StrictTransport()
    result = asyncio.run(payer.pay(url=URL, transport=transport))

    assert result.paid is True
    assert transport.calls == 2
    assert transport.closed is False


def test_payer_does_not_record_forged_charge_receipt(tmp_path) -> None:
    store = ReceiptStore(tmp_path / "receipts.jsonl")
    payer = XRPLPayer(_signer(), store=store, expected_recipient=DESTINATION)
    challenge = _charge_challenge()

    def handler(request: httpx.Request) -> httpx.Response:
        if AUTHORIZATION_HEADER not in request.headers:
            return httpx.Response(
                402,
                headers={WWW_AUTHENTICATE_HEADER: render_payment_challenge(challenge)},
                request=request,
            )
        forged = _receipt(challenge_id=challenge.id, reference="F" * 64)
        return httpx.Response(
            200,
            headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(forged)},
            request=request,
        )

    with pytest.raises(ValueError, match="reference does not match"):
        asyncio.run(
            payer.pay(
                url=URL,
                transport=httpx.MockTransport(handler),
            )
        )

    assert store.list() == []


def test_plain_402_is_previewed_but_rejected_for_payment(tmp_path) -> None:
    payer = _payer(tmp_path / "receipts.jsonl")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(402, text="plain 402", request=request)
    )

    preview = asyncio.run(payer.pay(url=URL, dry_run=True, transport=transport))
    assert preview.preview is not None
    assert preview.preview["mpp_challenge_present"] is False

    with pytest.raises(ValueError, match="valid MPP challenge"):
        asyncio.run(payer.pay(url=URL, transport=transport))


def test_spend_cap_is_checked_before_signing(tmp_path) -> None:
    challenge = _charge_challenge(amount="2000")
    payer = _payer(tmp_path / "receipts.jsonl")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            402,
            headers={WWW_AUTHENTICATE_HEADER: render_payment_challenge(challenge)},
            request=request,
        )
    )

    with pytest.raises(ValueError, match="exceeds configured spend cap"):
        asyncio.run(payer.pay(url=URL, amount=0.001, transport=transport))


def test_automatic_payment_requires_operator_approved_recipient(tmp_path) -> None:
    challenge = _charge_challenge()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            402,
            headers={WWW_AUTHENTICATE_HEADER: render_payment_challenge(challenge)},
            request=request,
        )

    payer = XRPLPayer(_signer(), store=ReceiptStore(tmp_path / "receipts.jsonl"))
    with pytest.raises(PaymentPolicyError, match="expected_recipient is required"):
        asyncio.run(payer.pay(url=URL, transport=httpx.MockTransport(handler)))
    assert calls == 1


def test_payment_policy_rejects_unapproved_recipient_and_long_challenge(tmp_path) -> None:
    wrong_recipient = Wallet.create().classic_address
    payer = XRPLPayer(
        _signer(),
        store=ReceiptStore(tmp_path / "receipts.jsonl"),
        expected_recipient=wrong_recipient,
    )
    challenge = _charge_challenge()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            402,
            headers={WWW_AUTHENTICATE_HEADER: render_payment_challenge(challenge)},
            request=request,
        )
    )
    with pytest.raises(PaymentPolicyError, match="recipient is not allowed"):
        asyncio.run(payer.pay(url=URL, transport=transport))

    payer = _payer(tmp_path / "long-receipts.jsonl")
    long_challenge = _charge_challenge(expires_in_seconds=600)
    long_transport = httpx.MockTransport(
        lambda request: httpx.Response(
            402,
            headers={WWW_AUTHENTICATE_HEADER: render_payment_challenge(long_challenge)},
            request=request,
        )
    )
    with pytest.raises(PaymentPolicyError, match="validity window exceeds"):
        asyncio.run(payer.pay(url=URL, transport=long_transport))


def test_remote_plaintext_is_rejected_before_probe(tmp_path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, request=request)

    payer = _payer(tmp_path / "receipts.jsonl")
    with pytest.raises(ValueError, match="require HTTPS"):
        asyncio.run(
            payer.pay(
                url="http://merchant.example/premium",
                headers={AUTHORIZATION_HEADER: "Bearer secret"},
                transport=httpx.MockTransport(handler),
            )
        )
    assert calls == 0


def test_session_amount_is_incremental_not_legacy_min_prepay() -> None:
    challenge = _session_challenge(channel_id=CHANNEL_ID, amount="250")
    assert payment_challenge_amount(challenge) == Decimal("0.00025")

    with pytest.raises(ValidationError):
        XRPLSessionRequest.model_validate(
            {
                "amount": "250",
                "currency": "XRP:native",
                "recipient": DESTINATION,
                "methodDetails": {
                    "network": "xrpl:1",
                    "sessionId": "session-123",
                    "minPrepayAmount": "1000",
                },
            }
        )


def test_paychannel_open_signs_create_and_captures_channel(tmp_path) -> None:
    challenge = _session_challenge(channel_id="", amount="0", cumulative="0")
    payer = _payer(tmp_path / "receipts.jsonl")
    calls = 0
    opened_channel_id = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls, opened_channel_id
        calls += 1
        if calls == 1:
            return httpx.Response(
                402,
                headers={WWW_AUTHENTICATE_HEADER: render_payment_challenge(challenge)},
                request=request,
            )
        payload = decode_session_payload(_credential_from_request(request))
        transaction = binarycodec.decode(payload.transaction)
        assert payload.action == "open"
        assert transaction["TransactionType"] == "PaymentChannelCreate"
        assert transaction["Destination"] == DESTINATION
        assert transaction["Amount"] == "1000000"
        binding = derive_paychannel_open_binding(payload.transaction)
        opened_channel_id = binding.channel_id
        receipt = PaymentReceipt(
            status="success",
            method="xrpl",
            timestamp="2026-08-30T12:00:00Z",
            reference=f"open:{binding.channel_id}:{binding.tx_hash}",
        )
        return httpx.Response(
            200,
            headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(receipt)},
            request=request,
        )

    result = asyncio.run(
        payer.pay(
            url=URL,
            intent="session",
            amount=1,
            channel_funding_amount="1000000",
            transport=httpx.MockTransport(handler),
        )
    )

    assert result.paid is True
    assert payer.channel_state(URL).channel_id == opened_channel_id
    assert calls == 2


def test_paychannel_funding_is_covered_by_spend_cap(tmp_path) -> None:
    challenge = _session_challenge(channel_id="", amount="0", cumulative="0")
    payer = _payer(tmp_path / "receipts.jsonl")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            402,
            headers={WWW_AUTHENTICATE_HEADER: render_payment_challenge(challenge)},
            request=request,
        )
    )

    with pytest.raises(ValueError, match="funding amount .* exceeds"):
        asyncio.run(
            payer.pay(
                url=URL,
                intent="session",
                amount=0.001,
                channel_funding_amount="1000000",
                transport=transport,
            )
        )


def test_paychannel_voucher_advances_registered_cumulative(tmp_path) -> None:
    challenge = _session_challenge(channel_id=CHANNEL_ID, amount="25", cumulative="100")
    receipt = PaymentReceipt(
        status="success",
        method="xrpl",
        timestamp="2026-08-30T12:00:00Z",
        reference=f"{CHANNEL_ID}:125",
    )
    payer = XRPLPayer(
        _signer(),
        store=ReceiptStore(tmp_path / "receipts.jsonl"),
    )
    payer.register_channel(
        URL,
        channel_id=CHANNEL_ID,
        cumulative_amount="100",
        recipient=DESTINATION,
    )
    captured_action = ""
    captured_amount = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_action, captured_amount
        if AUTHORIZATION_HEADER not in request.headers:
            return httpx.Response(
                402,
                headers={WWW_AUTHENTICATE_HEADER: render_payment_challenge(challenge)},
                request=request,
            )
        payload = decode_session_payload(_credential_from_request(request))
        captured_action = payload.action
        captured_amount = payload.amount
        return httpx.Response(
            200,
            headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(receipt)},
            request=request,
        )

    result = asyncio.run(
        payer.pay(
            url=URL,
            intent="session",
            expected_recipient=DESTINATION,
            transport=httpx.MockTransport(handler),
        )
    )

    assert result.paid is True
    assert captured_action == "voucher"
    assert captured_amount == "125"
    assert payer.channel_state(URL).cumulative_amount == "125"
    assert result.receipt.amount == "0.000025"


def test_paychannel_voucher_requires_registered_channel(tmp_path) -> None:
    challenge = _session_challenge(channel_id=CHANNEL_ID)
    payer = _payer(tmp_path / "receipts.jsonl")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            402,
            headers={WWW_AUTHENTICATE_HEADER: render_payment_challenge(challenge)},
            request=request,
        )
    )

    with pytest.raises(ValueError, match="requires channel_id"):
        asyncio.run(payer.pay(url=URL, intent="session", transport=transport))


def test_paychannel_rejects_server_cumulative_jump(tmp_path) -> None:
    challenge = _session_challenge(channel_id=CHANNEL_ID, cumulative="1000000000")
    payer = XRPLPayer(
        _signer(),
        store=ReceiptStore(tmp_path / "receipts.jsonl"),
    )
    payer.register_channel(
        URL,
        channel_id=CHANNEL_ID,
        cumulative_amount="100",
        recipient=DESTINATION,
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            402,
            headers={WWW_AUTHENTICATE_HEADER: render_payment_challenge(challenge)},
            request=request,
        )
    )

    with pytest.raises(ValueError, match="cumulativeAmount does not match"):
        asyncio.run(
            payer.pay(
                url=URL,
                intent="session",
                expected_recipient=DESTINATION,
                transport=transport,
            )
        )


def test_paychannel_close_uses_final_voucher_and_forgets_channel(tmp_path) -> None:
    challenge = _session_challenge(channel_id=CHANNEL_ID, amount="10", cumulative="125")
    receipt = PaymentReceipt(
        status="success",
        method="xrpl",
        timestamp="2026-08-30T12:00:00Z",
        reference=f"{CHANNEL_ID}:135",
    )
    payer = XRPLPayer(
        _signer(),
        store=ReceiptStore(tmp_path / "receipts.jsonl"),
    )
    payer.register_channel(
        URL,
        channel_id=CHANNEL_ID,
        cumulative_amount="125",
        recipient=DESTINATION,
    )
    captured_action = ""
    captured_amount = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_action, captured_amount
        if AUTHORIZATION_HEADER not in request.headers:
            return httpx.Response(
                402,
                headers={WWW_AUTHENTICATE_HEADER: render_payment_challenge(challenge)},
                request=request,
            )
        payload = decode_session_payload(_credential_from_request(request))
        captured_action = payload.action
        captured_amount = payload.amount
        return httpx.Response(
            200,
            headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(receipt)},
            request=request,
        )

    result = asyncio.run(
        payer.close_channel(url=URL, transport=httpx.MockTransport(handler))
    )

    assert result.paid is True
    assert captured_action == "close"
    assert captured_amount == "135"
    assert payer.channel_state(URL) is None


def test_named_network_and_currency_resolution(monkeypatch) -> None:
    wallet = Wallet.create()
    assert wallet.seed is not None
    captured: dict[str, object] = {}

    class FakeSigner:
        def __init__(
            self,
            wallet_arg,
            *,
            rpc_url: str,
            network: str,
            allow_insecure_rpc: bool,
            max_fee_drops: str,
            iou_pathfinding_policy,
        ) -> None:
            captured["wallet"] = wallet_arg
            captured["rpc_url"] = rpc_url
            captured["network"] = network
            captured["allow_insecure_rpc"] = allow_insecure_rpc
            captured["max_fee_drops"] = max_fee_drops
            captured["iou_pathfinding_policy"] = iou_pathfinding_policy

    monkeypatch.setenv("XRPL_WALLET_SEED", wallet.seed)
    monkeypatch.setenv("XRPL_NETWORK", "testnet")
    monkeypatch.setenv("ALLOW_INSECURE_XRPL_RPC", "false")
    monkeypatch.setenv("XRPL_MPP_MAX_FEE_DROPS", "25")
    monkeypatch.delenv("XRPL_MPP_IOU_SOURCE_CURRENCY", raising=False)
    monkeypatch.delenv("XRPL_MPP_IOU_MAX_SOURCE_AMOUNT", raising=False)
    monkeypatch.delenv("XRPL_RPC_URL", raising=False)
    monkeypatch.setattr("xrpl_mpp_payer.payer.XRPLPaymentSigner", FakeSigner)
    monkeypatch.setattr(
        "xrpl_mpp_payer.payer.resolve_testnet_rpc_url",
        lambda: "https://resolved.testnet.rpc/",
    )

    build_signer_from_env()
    assert captured["rpc_url"] == "https://resolved.testnet.rpc/"
    assert captured["network"] == "testnet"
    assert captured["allow_insecure_rpc"] is False
    assert captured["max_fee_drops"] == "25"
    assert captured["iou_pathfinding_policy"] is None

    monkeypatch.setenv("XRPL_NETWORK", "mainnet")
    build_signer_from_env()
    assert captured["rpc_url"] == DEFAULT_MAINNET_RPC_URL
    assert captured["network"] == "mainnet"

    monkeypatch.setenv("XRPL_NETWORK", "xrpl:1")
    with pytest.raises(ValueError, match="mainnet, testnet, or devnet"):
        build_signer_from_env()


def test_build_signer_from_env_requires_explicit_loopback_http_opt_in(monkeypatch) -> None:
    wallet = Wallet.create()
    assert wallet.seed is not None
    monkeypatch.setenv("XRPL_WALLET_SEED", wallet.seed)
    monkeypatch.setenv("XRPL_NETWORK", "testnet")
    monkeypatch.setenv("XRPL_RPC_URL", "http://127.0.0.1:5005")
    monkeypatch.delenv("ALLOW_INSECURE_XRPL_RPC", raising=False)

    with pytest.raises(ValueError, match="must use HTTPS"):
        build_signer_from_env()

    monkeypatch.setenv("ALLOW_INSECURE_XRPL_RPC", "true")
    signer = build_signer_from_env()
    assert signer.rpc_url == "http://127.0.0.1:5005"

    monkeypatch.setenv("XRPL_RPC_URL", "http://rpc.example")
    with pytest.raises(ValueError, match="must use HTTPS"):
        build_signer_from_env()


def test_build_signer_from_env_rejects_invalid_insecure_rpc_flag(monkeypatch) -> None:
    wallet = Wallet.create()
    assert wallet.seed is not None
    monkeypatch.setenv("XRPL_WALLET_SEED", wallet.seed)
    monkeypatch.setenv("XRPL_RPC_URL", "https://rpc.example")
    monkeypatch.setenv("ALLOW_INSECURE_XRPL_RPC", "sometimes")

    with pytest.raises(ValueError, match="ALLOW_INSECURE_XRPL_RPC must be one of"):
        build_signer_from_env()


def test_build_signer_from_env_configures_max_fee(monkeypatch) -> None:
    wallet = Wallet.create()
    assert wallet.seed is not None
    monkeypatch.setenv("XRPL_WALLET_SEED", wallet.seed)
    monkeypatch.setenv("XRPL_RPC_URL", "https://rpc.example")
    monkeypatch.setenv("XRPL_MPP_MAX_FEE_DROPS", "25")

    signer = build_signer_from_env()

    assert signer._max_fee_drops == 25


def test_build_signer_from_env_configures_bounded_iou_source_policy(monkeypatch) -> None:
    wallet = Wallet.create()
    assert wallet.seed is not None
    monkeypatch.setenv("XRPL_WALLET_SEED", wallet.seed)
    monkeypatch.setenv("XRPL_RPC_URL", "https://rpc.example")
    monkeypatch.setenv("XRPL_MPP_IOU_SOURCE_CURRENCY", "XRP")
    monkeypatch.setenv("XRPL_MPP_IOU_MAX_SOURCE_AMOUNT", "1005")
    monkeypatch.setenv("XRPL_MPP_IOU_SLIPPAGE_BPS", "75")

    signer = build_signer_from_env()

    assert signer._iou_pathfinding_policy.source_currency == "XRP"
    assert signer._iou_pathfinding_policy.max_source_amount == Decimal("1005")
    assert signer._iou_pathfinding_policy.slippage_bps == 75


def test_build_signer_from_env_requires_complete_iou_source_policy(monkeypatch) -> None:
    wallet = Wallet.create()
    assert wallet.seed is not None
    monkeypatch.setenv("XRPL_WALLET_SEED", wallet.seed)
    monkeypatch.setenv("XRPL_RPC_URL", "https://rpc.example")
    monkeypatch.setenv("XRPL_MPP_IOU_SOURCE_CURRENCY", "XRP")
    monkeypatch.delenv("XRPL_MPP_IOU_MAX_SOURCE_AMOUNT", raising=False)

    with pytest.raises(ValueError, match="must be configured together"):
        build_signer_from_env()


def test_currency_resolution_uses_mpp_02_wire_shapes() -> None:
    issued = resolve_currency(asset="USD", issuer=DESTINATION, network="testnet")
    assert issued == f'{{"currency":"USD","issuer":"{DESTINATION}"}}'
    assert resolve_currency(asset="XRP", issuer=None, network="testnet") == "XRP"
    assert resolve_currency(asset=issued, issuer=None, network="testnet") == issued

    with pytest.raises(ValueError):
        resolve_currency(asset=f"USD:{DESTINATION}", issuer=None, network="testnet")
    with pytest.raises(ValueError, match="does not use an issuer"):
        resolve_currency(asset="XRP", issuer=DESTINATION, network="testnet")


def test_receipt_record_rejects_legacy_embedded_asset_shape() -> None:
    with pytest.raises(ValidationError):
        ReceiptRecord.model_validate(
            {
                "created_at": "2026-08-30T12:00:00Z",
                "url": URL,
                "method": "GET",
                "status_code": 200,
                "network": "xrpl:1",
                "asset_identifier": "XRP:native",
                "amount": "0.001",
                "payer": PAYER,
                "tx_hash": "A" * 64,
                "settlement_status": "validated",
            }
        )


def test_budget_status_sums_matching_currency(monkeypatch, tmp_path) -> None:
    store = ReceiptStore(tmp_path / "receipts.jsonl")
    for index, value in enumerate(("0.001", "0.002")):
        store.append(
            ReceiptRecord(
                created_at=f"2026-08-30T12:00:0{index}Z",
                url=f"https://merchant.example/{index}",
                method="GET",
                status_code=200,
                network="testnet",
                currency="XRP",
                amount=value,
                payer=PAYER,
                reference=str(index),
                settlement_status="validated",
                intent="charge",
            )
        )
    monkeypatch.setenv("XRPL_MPP_MAX_SPEND", "0.01")

    summary = budget_status(asset="XRP", network="testnet", store=store)
    assert summary == {
        "currency": "XRP",
        "spent": "0.003",
        "max_spend": "0.01",
        "remaining": "0.007",
    }


def test_resolve_spend_cap_ignores_comment_placeholder(monkeypatch) -> None:
    monkeypatch.setenv("XRPL_MPP_MAX_SPEND", "   # optional spend cap")
    assert resolve_spend_cap(amount=Decimal("0.001"), max_spend=None) == Decimal("0.001")


def test_resolve_spend_cap_never_raises_operator_environment_ceiling(monkeypatch) -> None:
    monkeypatch.setenv("XRPL_MPP_MAX_SPEND", "0.01")

    assert resolve_spend_cap(amount=Decimal("0.005"), max_spend=Decimal("1")) == Decimal(
        "0.01"
    )
    assert resolve_spend_cap(
        amount=Decimal("0.005"),
        max_spend=Decimal("0.002"),
    ) == Decimal("0.002")


def test_create_proxy_app_forwards_receipt_header() -> None:
    async def fake_pay(**_: object) -> PayResult:
        return PayResult(
            status_code=200,
            body=b"paid content",
            headers={PAYMENT_RECEIPT_HEADER: "receipt-token"},
            challenge_present=True,
            dry_run=False,
            paid=True,
        )

    client = TestClient(
        create_proxy_app(
            target_base_url="https://merchant.example",
            payer=SimpleNamespace(pay=fake_pay),
        )
    )
    response = client.get("/premium")
    assert response.status_code == 200
    assert response.headers[PAYMENT_RECEIPT_HEADER] == "receipt-token"


def test_proxy_manager_rejects_changed_paychannel_settings(monkeypatch) -> None:
    class FakeServer:
        def __init__(self, _config) -> None:
            self.started = True

        def run(self) -> None:
            return None

    class FakeThread:
        def __init__(self, *, target, daemon: bool) -> None:
            self.target = target
            self.daemon = daemon

        def start(self) -> None:
            return None

    monkeypatch.setattr("xrpl_mpp_payer.proxy.create_proxy_app", lambda **_: object())
    monkeypatch.setattr("xrpl_mpp_payer.proxy.uvicorn.Server", FakeServer)
    monkeypatch.setattr("xrpl_mpp_payer.proxy.threading.Thread", FakeThread)
    manager = ProxyManager()
    bind_url = manager.start(
        target_base_url="https://merchant.example",
        intent="session",
        channel_id=CHANNEL_ID,
    )
    assert bind_url == "http://127.0.0.1:8787"
    assert manager.start(
        target_base_url="https://merchant.example/",
        intent="session",
        channel_id=CHANNEL_ID,
    ) == bind_url
    with pytest.raises(RuntimeError, match="different configuration"):
        manager.start(
            target_base_url="https://merchant.example",
            intent="session",
            channel_id="D" * 64,
        )


def test_cli_pay_and_close_commands_delegate(monkeypatch) -> None:
    async def fake_result(**_: object) -> PayResult:
        return PayResult(
            status_code=200,
            body=b"paid content",
            headers={},
            challenge_present=True,
            dry_run=False,
            paid=True,
        )

    monkeypatch.setattr("xrpl_mpp_payer.cli.pay_with_mpp", fake_result)
    monkeypatch.setattr("xrpl_mpp_payer.cli.close_with_mpp", fake_result)
    assert RUNNER.invoke(app, ["pay", URL]).exit_code == 0
    close = RUNNER.invoke(app, ["close", URL, "--channel-id", CHANNEL_ID])
    assert close.exit_code == 0
    assert "paid content" in close.stdout


def test_mcp_tools_delegate_to_mpp_helpers(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_result(**kwargs: object) -> PayResult:
        calls.append(kwargs)
        return PayResult(
            status_code=200,
            body=b"paid content",
            headers={},
            challenge_present=True,
            dry_run=False,
            paid=True,
        )

    monkeypatch.setattr("xrpl_mpp_payer.mcp.pay_with_mpp", fake_result)
    monkeypatch.setattr("xrpl_mpp_payer.mcp.close_with_mpp", fake_result)
    monkeypatch.setattr(
        "xrpl_mpp_payer.mcp.get_receipts",
        lambda limit=10: [
            {
                "url": URL,
                "amount": "0.001",
                "currency": "XRP",
                "reference": "A1",
            }
        ],
    )
    monkeypatch.setattr(
        "xrpl_mpp_payer.mcp.get_budget_status",
        lambda asset="XRP", issuer=None: {"spent": "0.001", "remaining": "0.009"},
    )
    monkeypatch.setattr(
        "xrpl_mpp_payer.mcp.proxy_manager.start",
        lambda **_: "http://127.0.0.1:8787",
    )
    monkeypatch.setenv("XRPL_MPP_EXPECTED_RECIPIENT", DESTINATION)
    monkeypatch.setenv("XRPL_MPP_MAX_SPEND", "0.01")

    assert asyncio.run(mcp_pay_url(URL)) == "paid content"
    assert asyncio.run(mcp_close_channel(URL, CHANNEL_ID)) == "paid content"
    assert calls[0]["expected_recipient"] == DESTINATION
    assert calls[0]["max_spend"] == Decimal("0.01")
    assert calls[1]["expected_recipient"] == DESTINATION
    assert calls[1]["max_spend"] == Decimal("0.01")
    assert URL in asyncio.run(mcp_list_receipts())
    assert json.loads(asyncio.run(mcp_budget_status())) == {
        "spent": "0.001",
        "remaining": "0.009",
    }
    assert "http://127.0.0.1:8787" in asyncio.run(
        mcp_proxy_mode("https://merchant.example")
    )


def test_mcp_automatic_payment_requires_operator_environment(monkeypatch) -> None:
    monkeypatch.delenv("XRPL_MPP_EXPECTED_RECIPIENT", raising=False)
    monkeypatch.delenv("XRPL_MPP_MAX_SPEND", raising=False)

    with pytest.raises(RuntimeError, match="XRPL_MPP_EXPECTED_RECIPIENT.*XRPL_MPP_MAX_SPEND"):
        asyncio.run(mcp_pay_url(URL))
    with pytest.raises(RuntimeError, match="XRPL_MPP_EXPECTED_RECIPIENT.*XRPL_MPP_MAX_SPEND"):
        asyncio.run(mcp_close_channel(URL, CHANNEL_ID))
    with pytest.raises(RuntimeError, match="XRPL_MPP_EXPECTED_RECIPIENT.*XRPL_MPP_MAX_SPEND"):
        asyncio.run(mcp_proxy_mode("https://merchant.example"))


def test_mcp_tool_signatures_do_not_expose_operator_policy_overrides() -> None:
    for tool in (mcp_pay_url, mcp_close_channel, mcp_proxy_mode):
        parameters = inspect.signature(tool).parameters
        assert "expected_recipient" not in parameters
        assert "max_spend" not in parameters


def test_payer_package_metadata_is_02() -> None:
    pyproject = Path("packages/payer/pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "0.2.0"' in pyproject
    assert 'xrpl-mpp-client>=0.2.0,<0.3.0' in pyproject
    assert 'xrpl-mpp-core>=0.2.0,<0.3.0' in pyproject
