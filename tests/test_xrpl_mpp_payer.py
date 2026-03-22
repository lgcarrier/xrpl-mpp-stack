from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner
from xrpl.wallet import Wallet

from xrpl_mpp_client import PAYMENT_RECEIPT_HEADER, WWW_AUTHENTICATE_HEADER, XRPLPaymentSigner
from xrpl_mpp_core import (
    PaymentReceipt,
    XRPLChargeMethodDetails,
    XRPLChargeRequest,
    XRPLSessionMethodDetails,
    XRPLSessionRequest,
    build_payment_challenge,
    encode_payment_receipt,
    render_payment_challenge,
)
from xrpl_mpp_payer import ReceiptRecord, create_proxy_app
from xrpl_mpp_payer.cli import app
from xrpl_mpp_payer.mcp import budget_status as mcp_budget_status
from xrpl_mpp_payer.mcp import list_receipts as mcp_list_receipts
from xrpl_mpp_payer.mcp import pay_url as mcp_pay_url
from xrpl_mpp_payer.mcp import proxy_mode as mcp_proxy_mode
from xrpl_mpp_payer.proxy import ProxyManager
from xrpl_mpp_payer.payer import (
    DEFAULT_RPC_URL,
    PayResult,
    XRPLPayer,
    budget_status,
    build_signer_from_env,
    payment_challenge_amount,
    resolve_spend_cap,
)
from xrpl_mpp_payer.receipts import ReceiptStore

DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
RUNNER = CliRunner()
SECRET = "payer-test-secret"


def _charge_challenge():
    request = XRPLChargeRequest(
        amount="1000",
        currency="XRP:native",
        recipient=DESTINATION,
        methodDetails=XRPLChargeMethodDetails(network="xrpl:1", invoiceId="A" * 64),
    )
    return build_payment_challenge(
        secret=SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="charge",
        request_model=request,
        expires_in_seconds=300,
    )


def _session_challenge():
    request = XRPLSessionRequest(
        amount="250",
        currency="XRP:native",
        recipient=DESTINATION,
        methodDetails=XRPLSessionMethodDetails(
            network="xrpl:1",
            sessionId="session-123",
            asset="XRP:native",
            unitAmount="250",
            minPrepayAmount="1000",
        ),
    )
    return build_payment_challenge(
        secret=SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="session",
        request_model=request,
        expires_in_seconds=300,
    )


def _payment_receipt() -> PaymentReceipt:
    return PaymentReceipt(
        method="xrpl",
        timestamp="2026-03-21T12:00:00Z",
        reference="ABC123",
        challengeId="challenge-id",
        intent="charge",
        network="xrpl:1",
        payer="rBuyerAddress123",
        recipient=DESTINATION,
        invoiceId="A" * 64,
        txHash="ABC123",
        settlementStatus="validated",
        asset={"code": "XRP"},
        amount={"value": "1000", "unit": "drops", "asset": {"code": "XRP"}, "drops": 1000},
    )


def _signer() -> XRPLPaymentSigner:
    return XRPLPaymentSigner(Wallet.create(), network="xrpl:1", autofill_enabled=False)


def test_pay_records_receipt_on_success(tmp_path) -> None:
    store = ReceiptStore(tmp_path / "receipts.jsonl")
    payer = XRPLPayer(_signer(), store=store)
    challenge = _charge_challenge()
    payment_receipt = _payment_receipt()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                402,
                headers={WWW_AUTHENTICATE_HEADER: render_payment_challenge(challenge)},
                json={"status": 402, "title": "Payment Required", "detail": "Payment required"},
                request=request,
            )

        return httpx.Response(
            200,
            headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(payment_receipt)},
            text="paid content",
            request=request,
        )

    result = asyncio.run(
        payer.pay(
            url="https://merchant.example/premium",
            amount=0.001,
            transport=httpx.MockTransport(handler),
        )
    )

    assert result.paid is True
    assert result.status_code == 200
    assert result.text == "paid content"
    receipts = store.list(limit=5)
    assert len(receipts) == 1
    assert receipts[0].tx_hash == "ABC123"
    assert attempts == 2


def test_pay_keeps_caller_provided_transport_open_for_retry(tmp_path) -> None:
    store = ReceiptStore(tmp_path / "receipts.jsonl")
    payer = XRPLPayer(_signer(), store=store)
    challenge = _charge_challenge()
    payment_receipt = _payment_receipt()

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
                    json={"status": 402, "title": "Payment Required", "detail": "Payment required"},
                    request=request,
                )

            return httpx.Response(
                200,
                headers={PAYMENT_RECEIPT_HEADER: encode_payment_receipt(payment_receipt)},
                text="paid content",
                request=request,
            )

        async def aclose(self) -> None:
            self.closed = True

    transport = StrictTransport()
    result = asyncio.run(
        payer.pay(
            url="https://merchant.example/premium",
            amount=0.001,
            transport=transport,
        )
    )

    assert result.status_code == 200
    assert result.paid is True
    assert transport.calls == 2
    assert transport.closed is False


def test_pay_dry_run_handles_plain_402_without_challenge(tmp_path) -> None:
    store = ReceiptStore(tmp_path / "receipts.jsonl")
    payer = XRPLPayer(_signer(), store=store)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, text="plain 402", request=request)

    result = asyncio.run(
        payer.pay(
            url="https://merchant.example/premium",
            dry_run=True,
            transport=httpx.MockTransport(handler),
        )
    )

    assert result.dry_run is True
    assert result.preview is not None
    assert result.preview["mpp_challenge_present"] is False
    assert store.list(limit=5) == []


def test_pay_raises_on_plain_402_without_challenge(tmp_path) -> None:
    store = ReceiptStore(tmp_path / "receipts.jsonl")
    payer = XRPLPayer(_signer(), store=store)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, text="plain 402", request=request)

    with pytest.raises(ValueError, match="valid MPP challenge"):
        asyncio.run(
            payer.pay(
                url="https://merchant.example/premium",
                transport=httpx.MockTransport(handler),
            )
        )


def test_payment_challenge_amount_uses_session_min_prepay() -> None:
    assert payment_challenge_amount(_session_challenge()) == Decimal("0.001")


def test_payment_receipt_rejects_drops_amount_without_drops_field() -> None:
    with pytest.raises(ValueError, match="Drops amount must include drops"):
        PaymentReceipt(
            method="xrpl",
            timestamp="2026-03-21T12:00:00Z",
            reference="ABC123",
            challengeId="challenge-id",
            intent="charge",
            network="xrpl:1",
            payer="rBuyerAddress123",
            recipient=DESTINATION,
            invoiceId="A" * 64,
            txHash="ABC123",
            settlementStatus="validated",
            asset={"code": "XRP"},
            amount={"value": "1000", "unit": "drops", "asset": {"code": "XRP"}},
        )


def test_build_signer_from_env_resolves_testnet_rpc_when_unset(monkeypatch) -> None:
    wallet = Wallet.create()
    assert wallet.seed is not None

    captured: dict[str, object] = {}

    class FakeSigner:
        def __init__(self, wallet_arg, *, rpc_url: str, network: str) -> None:
            captured["wallet"] = wallet_arg
            captured["rpc_url"] = rpc_url
            captured["network"] = network

    monkeypatch.setenv("XRPL_WALLET_SEED", wallet.seed)
    monkeypatch.setenv("XRPL_NETWORK", "xrpl:1")
    monkeypatch.delenv("XRPL_RPC_URL", raising=False)
    monkeypatch.setattr("xrpl_mpp_payer.payer.XRPLPaymentSigner", FakeSigner)
    monkeypatch.setattr(
        "xrpl_mpp_payer.payer.resolve_testnet_rpc_url",
        lambda: "https://resolved.testnet.rpc/",
    )

    build_signer_from_env()

    assert captured["wallet"].classic_address == wallet.classic_address
    assert captured["rpc_url"] == "https://resolved.testnet.rpc/"
    assert captured["network"] == "xrpl:1"


def test_build_signer_from_env_does_not_auto_resolve_non_testnet_network(monkeypatch) -> None:
    wallet = Wallet.create()
    assert wallet.seed is not None

    captured: dict[str, object] = {}
    resolver_called = {"value": False}

    class FakeSigner:
        def __init__(self, wallet_arg, *, rpc_url: str, network: str) -> None:
            captured["wallet"] = wallet_arg
            captured["rpc_url"] = rpc_url
            captured["network"] = network

    monkeypatch.setenv("XRPL_WALLET_SEED", wallet.seed)
    monkeypatch.setenv("XRPL_NETWORK", "xrpl:0")
    monkeypatch.delenv("XRPL_RPC_URL", raising=False)
    monkeypatch.setattr("xrpl_mpp_payer.payer.XRPLPaymentSigner", FakeSigner)
    monkeypatch.setattr(
        "xrpl_mpp_payer.payer.resolve_testnet_rpc_url",
        lambda: resolver_called.__setitem__("value", True) or "https://resolved.testnet.rpc/",
    )

    build_signer_from_env()

    assert captured["wallet"].classic_address == wallet.classic_address
    assert captured["rpc_url"] == DEFAULT_RPC_URL
    assert captured["network"] == "xrpl:0"
    assert resolver_called["value"] is False


def test_resolve_spend_cap_ignores_comment_only_env_placeholder(monkeypatch) -> None:
    monkeypatch.setenv("XRPL_MPP_MAX_SPEND", "   # optional spend cap")

    assert resolve_spend_cap(amount=Decimal("0.001"), max_spend=None) == Decimal("0.001")


def test_budget_status_sums_matching_asset(monkeypatch, tmp_path) -> None:
    store = ReceiptStore(tmp_path / "receipts.jsonl")
    store.append(
        ReceiptRecord(
            created_at="2025-01-01T00:00:00+00:00",
            url="https://merchant.example/a",
            method="GET",
            status_code=200,
            network="xrpl:1",
            asset_identifier="XRP:native",
            amount="0.001",
            payer="rA",
            tx_hash="A1",
            settlement_status="validated",
        )
    )
    store.append(
        ReceiptRecord(
            created_at="2025-01-01T00:00:01+00:00",
            url="https://merchant.example/b",
            method="GET",
            status_code=200,
            network="xrpl:1",
            asset_identifier="XRP:native",
            amount="0.002",
            payer="rB",
            tx_hash="B1",
            settlement_status="validated",
        )
    )
    monkeypatch.setenv("XRPL_MPP_MAX_SPEND", "0.01")

    summary = budget_status(asset="XRP", network="xrpl:1", store=store)

    assert summary["spent"] == "0.003"
    assert summary["remaining"] == "0.007"


def test_create_proxy_app_forwards_mpp_receipt_header(monkeypatch) -> None:
    async def fake_pay(**_: object) -> PayResult:
        return PayResult(
            status_code=200,
            body=b"paid content",
            headers={PAYMENT_RECEIPT_HEADER: "receipt-token"},
            challenge_present=True,
            dry_run=False,
            paid=True,
        )

    payer = SimpleNamespace(pay=fake_pay)
    app = create_proxy_app(target_base_url="https://merchant.example", payer=payer)
    client = TestClient(app)
    response = client.get("/premium")

    assert response.status_code == 200
    assert response.headers[PAYMENT_RECEIPT_HEADER] == "receipt-token"


def test_proxy_manager_rejects_reuse_with_changed_payment_settings(monkeypatch) -> None:
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
        port=8787,
        max_spend=0.01,
    )

    assert bind_url == "http://127.0.0.1:8787"
    assert manager.start(
        target_base_url="https://merchant.example/",
        port=8787,
        max_spend=0.01,
    ) == bind_url

    with pytest.raises(RuntimeError, match="different configuration"):
        manager.start(
            target_base_url="https://merchant.example",
            port=8787,
            max_spend=0.02,
        )


def test_cli_pay_command_uses_pay_with_mpp(monkeypatch) -> None:
    async def fake_pay_with_mpp(**_: object) -> PayResult:
        return PayResult(
            status_code=200,
            body=b"paid content",
            headers={},
            challenge_present=True,
            dry_run=False,
            paid=True,
        )

    monkeypatch.setattr("xrpl_mpp_payer.cli.pay_with_mpp", fake_pay_with_mpp)

    result = RUNNER.invoke(app, ["pay", "https://merchant.example/premium"])

    assert result.exit_code == 0
    assert "paid content" in result.stdout


def test_mcp_tools_delegate_to_mpp_helpers(monkeypatch, tmp_path) -> None:
    async def fake_pay_with_mpp(**_: object) -> PayResult:
        return PayResult(
            status_code=200,
            body=b"paid content",
            headers={},
            challenge_present=True,
            dry_run=False,
            paid=True,
        )

    store = ReceiptStore(tmp_path / "receipts.jsonl")
    store.append(
        ReceiptRecord(
            created_at="2025-01-01T00:00:00+00:00",
            url="https://merchant.example/a",
            method="GET",
            status_code=200,
            network="xrpl:1",
            asset_identifier="XRP:native",
            amount="0.001",
            payer="rA",
            tx_hash="A1",
            settlement_status="validated",
        )
    )

    monkeypatch.setattr("xrpl_mpp_payer.mcp.pay_with_mpp", fake_pay_with_mpp)
    monkeypatch.setattr("xrpl_mpp_payer.mcp.get_receipts", lambda limit=10: [{"url": "https://merchant.example/a", "amount": "0.001", "asset_identifier": "XRP:native", "tx_hash": "A1"}])
    monkeypatch.setattr("xrpl_mpp_payer.mcp.get_budget_status", lambda asset='XRP', issuer=None: {"spent": "0.001", "remaining": "0.009"})
    monkeypatch.setattr("xrpl_mpp_payer.mcp.proxy_manager.start", lambda **_: "http://127.0.0.1:8787")

    assert asyncio.run(mcp_pay_url("https://merchant.example/premium")) == "paid content"
    assert "https://merchant.example/a" in asyncio.run(mcp_list_receipts())
    assert json.loads(asyncio.run(mcp_budget_status())) == {"spent": "0.001", "remaining": "0.009"}
    assert "http://127.0.0.1:8787" in asyncio.run(mcp_proxy_mode("https://merchant.example"))
