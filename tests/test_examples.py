from __future__ import annotations

import asyncio
from decimal import Decimal
import importlib
from pathlib import Path

import httpx
import pytest
from xrpl.wallet import Wallet

from xrpl_mpp_core import (
    FacilitatorSupportedMethod,
    FacilitatorSupportedResponse,
    PaymentReceipt,
    RLUSD_TESTNET_ISSUER,
    XRPLAsset,
    XRPLChargeMethodDetails,
    XRPLChargeRequest,
    build_payment_challenge,
    encode_payment_receipt,
    render_payment_challenge,
)

FACILITATOR_TOKEN = "example-facilitator-token"
DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
PAYER = "rEXAMPLEPAYER123456789"
TX_HASH = "EXAMPLE-TX-HASH-123"
CHALLENGE_SECRET = "example-challenge-secret"


class FakeFacilitatorClient:
    def __init__(self) -> None:
        self.receipt = PaymentReceipt(
            method="xrpl",
            timestamp="2026-03-21T12:00:00Z",
            reference=TX_HASH,
            challengeId="challenge-id",
            intent="charge",
            network="xrpl:1",
            payer=PAYER,
            recipient=DESTINATION,
            invoiceId="A" * 64,
            txHash=TX_HASH,
            settlementStatus="validated",
            asset={"code": "XRP"},
            amount={"value": "1000", "unit": "drops", "asset": {"code": "XRP"}, "drops": 1000},
        )

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
                    network="xrpl:1",
                    assets=[XRPLAsset(code="XRP"), XRPLAsset(code="RLUSD", issuer=RLUSD_TESTNET_ISSUER)],
                    settlementMode="validated",
                )
            ]
        )

    async def charge(self, credential):
        return self.receipt.model_copy(update={"challengeId": credential.challenge.id})

    async def session(self, credential):
        raise AssertionError("session not used in example tests")


def test_merchant_example_supports_issued_asset_pricing(monkeypatch) -> None:
    monkeypatch.setenv("FACILITATOR_URL", "http://facilitator.local")
    monkeypatch.setenv("FACILITATOR_TOKEN", FACILITATOR_TOKEN)
    monkeypatch.setenv("MERCHANT_XRPL_ADDRESS", DESTINATION)
    monkeypatch.setenv("XRPL_NETWORK", "xrpl:1")
    monkeypatch.setenv("PRICE_ASSET_CODE", "RLUSD")
    monkeypatch.setenv("PRICE_ASSET_ISSUER", "rRLUSDISSUER")
    monkeypatch.setenv("PRICE_ASSET_AMOUNT", "1.25")

    merchant_example = importlib.import_module("examples.merchant_fastapi.app")
    merchant_example = importlib.reload(merchant_example)

    route_config = merchant_example.build_premium_route_config()
    option = route_config.charge_options[0]

    assert option.asset_identifier == "RLUSD:rRLUSDISSUER"
    assert option.amount == "1.25"


def test_merchant_example_ignores_comment_only_placeholders(monkeypatch) -> None:
    monkeypatch.setenv("FACILITATOR_URL", "http://facilitator.local")
    monkeypatch.setenv("FACILITATOR_TOKEN", FACILITATOR_TOKEN)
    monkeypatch.setenv("MERCHANT_XRPL_ADDRESS", DESTINATION)
    monkeypatch.setenv("XRPL_NETWORK", "xrpl:1")
    monkeypatch.setenv("PRICE_ASSET_CODE", "   # set to RLUSD or USDC for issued-asset demos")
    monkeypatch.setenv("PRICE_ASSET_ISSUER", "   # issuer for issued-asset demos")
    monkeypatch.setenv("PRICE_ASSET_AMOUNT", "   # issued-asset amount for merchant pricing")

    merchant_example = importlib.import_module("examples.merchant_fastapi.app")
    merchant_example = importlib.reload(merchant_example)

    route_config = merchant_example.build_premium_route_config()
    option = route_config.charge_options[0]

    assert option.asset_identifier == "XRP:native"
    assert option.amount == "1000"


def test_buyer_example_passes_env_asset_selection(monkeypatch) -> None:
    buyer_example = importlib.import_module("examples.buyer_httpx")
    buyer_example = importlib.reload(buyer_example)

    signer = buyer_example.XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
        autofill_enabled=False,
    )
    captured: dict[str, object] = {}

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url: str) -> httpx.Response:
            return httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", url))

    def fake_wrap_httpx_with_mpp_payment(
        _signer,
        *,
        asset=None,
        transport=None,
        timeout=None,
        **_kwargs,
    ):
        captured["asset"] = asset
        captured["transport"] = transport
        captured["timeout"] = timeout
        return DummyClient()

    monkeypatch.setenv("PAYMENT_ASSET", "RLUSD:rRLUSDISSUER")
    monkeypatch.setattr(
        buyer_example,
        "wrap_httpx_with_mpp_payment",
        fake_wrap_httpx_with_mpp_payment,
    )

    response = asyncio.run(
        buyer_example.fetch_paid_resource(
            signer=signer,
            target_url="http://merchant.local/premium",
        )
    )

    assert response.status_code == 200
    assert captured["asset"] == "RLUSD:rRLUSDISSUER"
    assert captured["timeout"] == buyer_example.DEFAULT_REQUEST_TIMEOUT_SECONDS


def test_buyer_example_ignores_comment_only_asset_placeholder(monkeypatch) -> None:
    buyer_example = importlib.import_module("examples.buyer_httpx")
    buyer_example = importlib.reload(buyer_example)

    signer = buyer_example.XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
        autofill_enabled=False,
    )
    captured: dict[str, object] = {}

    class DummyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url: str) -> httpx.Response:
            return httpx.Response(200, json={"ok": True}, request=httpx.Request("GET", url))

    def fake_wrap_httpx_with_mpp_payment(
        _signer,
        *,
        asset=None,
        transport=None,
        timeout=None,
        **_kwargs,
    ):
        captured["asset"] = asset
        captured["transport"] = transport
        captured["timeout"] = timeout
        return DummyClient()

    monkeypatch.setenv("PAYMENT_ASSET", "   # buyer-side asset identifier")
    monkeypatch.setattr(
        buyer_example,
        "wrap_httpx_with_mpp_payment",
        fake_wrap_httpx_with_mpp_payment,
    )

    response = asyncio.run(
        buyer_example.fetch_paid_resource(
            signer=signer,
            target_url="http://merchant.local/premium",
        )
    )

    assert response.status_code == 200
    assert captured["asset"] is None
    assert captured["timeout"] == buyer_example.DEFAULT_REQUEST_TIMEOUT_SECONDS


def test_buyer_example_uses_mainnet_rpc_fallback_for_non_testnet_network(monkeypatch) -> None:
    buyer_example = importlib.import_module("examples.buyer_httpx")
    buyer_example = importlib.reload(buyer_example)

    monkeypatch.delenv("XRPL_RPC_URL", raising=False)
    monkeypatch.setenv("XRPL_NETWORK", "xrpl:0")

    assert buyer_example.rpc_url_from_env() == buyer_example.DEFAULT_MAINNET_RPC_URL


def test_buyer_example_loads_dotenv_from_current_working_directory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    wallet = Wallet.create()
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "\n".join(
            [
                f"XRPL_WALLET_SEED={wallet.seed}",
                "XRPL_NETWORK=xrpl:0",
                "XRPL_RPC_URL=https://mainnet.example.invalid:51234",
                "PAYMENT_ASSET=RLUSD:rIssuerFromDotenv",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("XRPL_WALLET_SEED", raising=False)
    monkeypatch.delenv("XRPL_NETWORK", raising=False)
    monkeypatch.delenv("XRPL_RPC_URL", raising=False)
    monkeypatch.delenv("PAYMENT_ASSET", raising=False)

    buyer_example = importlib.import_module("examples.buyer_httpx")
    buyer_example = importlib.reload(buyer_example)

    signer = buyer_example.build_signer_from_env()

    assert signer.network == "xrpl:0"
    assert buyer_example.rpc_url_from_env() == "https://mainnet.example.invalid:51234"
    assert buyer_example.payment_asset_from_env() == "RLUSD:rIssuerFromDotenv"


def test_example_quickstart_flow_returns_paid_response(monkeypatch) -> None:
    monkeypatch.setenv("FACILITATOR_URL", "http://facilitator.local")
    monkeypatch.setenv("FACILITATOR_TOKEN", FACILITATOR_TOKEN)
    monkeypatch.setenv("MERCHANT_XRPL_ADDRESS", DESTINATION)
    monkeypatch.setenv("XRPL_NETWORK", "xrpl:1")
    monkeypatch.setenv("PRICE_DROPS", "1000")
    monkeypatch.setenv("MPP_CHALLENGE_SECRET", CHALLENGE_SECRET)

    merchant_example = importlib.import_module("examples.merchant_fastapi.app")
    merchant_example = importlib.reload(merchant_example)
    buyer_example = importlib.import_module("examples.buyer_httpx")
    buyer_example = importlib.reload(buyer_example)

    facilitator_client = FakeFacilitatorClient()
    merchant_app = merchant_example.create_app(
        client_factory=lambda _url, _token: facilitator_client,
    )

    signer = buyer_example.XRPLPaymentSigner(
        Wallet.create(),
        network="xrpl:1",
        autofill_enabled=False,
    )

    async def _run() -> httpx.Response:
        response = await buyer_example.fetch_paid_resource(
            signer=signer,
            target_url="http://merchant.local/premium",
            transport=httpx.ASGITransport(app=merchant_app),
        )
        return response

    response = asyncio.run(_run())

    assert response.status_code == 200
    assert response.json() == {
        "message": "premium content unlocked",
        "payer": PAYER,
        "invoice_id": "A" * 64,
        "tx_hash": TX_HASH,
    }


def test_demo_trace_renders_recording_friendly_summary(monkeypatch) -> None:
    demo_trace = importlib.import_module("devtools.demo_trace")
    demo_trace = importlib.reload(demo_trace)

    buyer_wallet = Wallet.create()
    signer = demo_trace.XRPLPaymentSigner(
        buyer_wallet,
        network="xrpl:1",
        autofill_enabled=False,
    )
    challenge = build_payment_challenge(
        secret=CHALLENGE_SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="charge",
        request_model=XRPLChargeRequest(
            amount="1.25",
            currency=f"RLUSD:{RLUSD_TESTNET_ISSUER}",
            recipient=DESTINATION,
            methodDetails=XRPLChargeMethodDetails(network="xrpl:1", invoiceId="A" * 64),
        ),
        expires_in_seconds=300,
    )
    payment_receipt = PaymentReceipt(
        method="xrpl",
        timestamp="2026-03-21T12:00:00Z",
        reference=TX_HASH,
        challengeId=challenge.id,
        intent="charge",
        network="xrpl:1",
        payer=buyer_wallet.classic_address,
        recipient=DESTINATION,
        invoiceId="A" * 64,
        txHash=TX_HASH,
        settlementStatus="validated",
        asset={"code": "RLUSD", "issuer": RLUSD_TESTNET_ISSUER},
        amount={"value": "1.25", "unit": "issued", "asset": {"code": "RLUSD", "issuer": RLUSD_TESTNET_ISSUER}},
    )

    balance_snapshots = {
        DESTINATION: [2_000_000, 2_000_000],
        buyer_wallet.classic_address: [10_000_000, 9_999_988],
    }
    asset_snapshots = {
        DESTINATION: [Decimal("4"), Decimal("5.25")],
        buyer_wallet.classic_address: [Decimal("7"), Decimal("5.75")],
    }

    def fake_get_validated_balance(_client, address: str) -> int:
        return balance_snapshots[address].pop(0)

    def fake_get_validated_trustline_balance(_client, address: str, issuer: str, *, currency_code: str = "RLUSD") -> Decimal:
        assert issuer == RLUSD_TESTNET_ISSUER
        assert currency_code == "RLUSD"
        return asset_snapshots[address].pop(0)

    def handler(request: httpx.Request) -> httpx.Response:
        if "authorization" not in request.headers:
            return httpx.Response(
                402,
                headers={"WWW-Authenticate": render_payment_challenge(challenge)},
                request=request,
            )
        return httpx.Response(
            200,
                json={
                    "message": "premium content unlocked",
                    "payer": buyer_wallet.classic_address,
                    "invoice_id": "A" * 64,
                    "tx_hash": TX_HASH,
                },
            headers={"Payment-Receipt": encode_payment_receipt(payment_receipt)},
            request=request,
        )

    monkeypatch.setattr(demo_trace, "get_validated_balance", fake_get_validated_balance)
    monkeypatch.setattr(
        demo_trace,
        "get_validated_trustline_balance",
        fake_get_validated_trustline_balance,
    )

    result = asyncio.run(
        demo_trace.run_demo_trace(
                signer=signer,
                rpc_client=object(),
                target_url="http://merchant.local/premium",
                payment_asset=f"RLUSD:{RLUSD_TESTNET_ISSUER}",
                timeout_seconds=1.0,
                transport=httpx.MockTransport(handler),
            )
    )

    output = demo_trace.render_trace(result)
    assert result.challenge.intent == "charge"
    assert result.payment_receipt is not None
    assert result.payment_receipt.tx_hash == TX_HASH
    assert "MPP payment challenge" in output
    assert "MPP payment receipt" in output
    assert "amount: 1.25 RLUSD" in output
    assert f"invoice id: {'A' * 32}" in output
    assert f"tx hash: {TX_HASH}" in output


def test_demo_trace_blocks_unfunded_issued_asset_buyer(monkeypatch) -> None:
    demo_trace = importlib.import_module("devtools.demo_trace")
    demo_trace = importlib.reload(demo_trace)

    buyer_wallet = Wallet.create()
    signer = demo_trace.XRPLPaymentSigner(
        buyer_wallet,
        network="xrpl:1",
        autofill_enabled=False,
    )
    challenge = build_payment_challenge(
        secret=CHALLENGE_SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="charge",
        request_model=XRPLChargeRequest(
            amount="1.25",
            currency=f"RLUSD:{RLUSD_TESTNET_ISSUER}",
            recipient=DESTINATION,
            methodDetails=XRPLChargeMethodDetails(network="xrpl:1", invoiceId="A" * 32),
        ),
        expires_in_seconds=300,
    )

    xrp_balances = {
        DESTINATION: 2_000_000,
        buyer_wallet.classic_address: 10_000_000,
    }
    rlusd_balances = {
        DESTINATION: Decimal("30"),
        buyer_wallet.classic_address: Decimal("0"),
    }
    printed_sections: list[str] = []

    def fake_get_validated_balance(_client, address: str) -> int:
        return xrp_balances[address]

    def fake_get_validated_trustline_balance(_client, address: str, issuer: str, *, currency_code: str = "RLUSD") -> Decimal:
        assert issuer == RLUSD_TESTNET_ISSUER
        assert currency_code == "RLUSD"
        return rlusd_balances[address]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402,
            headers={"WWW-Authenticate": render_payment_challenge(challenge)},
            request=request,
        )

    monkeypatch.setattr(demo_trace, "get_validated_balance", fake_get_validated_balance)
    monkeypatch.setattr(
        demo_trace,
        "get_validated_trustline_balance",
        fake_get_validated_trustline_balance,
    )

    with pytest.raises(
        demo_trace.DemoPreflightError,
        match="Buyer wallet .* only has 0 RLUSD",
    ):
        asyncio.run(
            demo_trace.run_demo_trace(
                signer=signer,
                rpc_client=object(),
                target_url="http://merchant.local/premium",
                payment_asset=f"RLUSD:{RLUSD_TESTNET_ISSUER}",
                timeout_seconds=1.0,
                transport=httpx.MockTransport(handler),
                printer=printed_sections.append,
            )
        )

    assert any("Preflight check" in section for section in printed_sections)
    assert any("python -m devtools.rlusd_topup" in section for section in printed_sections)
