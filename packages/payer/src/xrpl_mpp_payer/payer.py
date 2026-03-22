from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import json
from typing import Any

import httpx
from xrpl.wallet import Wallet

from xrpl_mpp_client import (
    XRPLPaymentTransport,
    XRPLPaymentSigner,
    decode_payment_challenges_response,
    decode_payment_receipt_header,
    select_payment_challenge,
)
from xrpl_mpp_core import (
    NETWORK_RLUSD_ISSUERS,
    NETWORK_USDC_ISSUERS,
    PaymentChallenge,
    PaymentReceipt,
    canonical_asset_identifier,
    getenv_clean,
    decode_challenge_request,
    normalize_currency_code,
)
from xrpl_mpp_core.testnet_rpc import resolve_testnet_rpc_url

from xrpl_mpp_payer.receipts import ReceiptRecord, ReceiptStore

DEFAULT_MAINNET_RPC_URL = "https://s1.ripple.com:51234"
DEFAULT_RPC_URL = DEFAULT_MAINNET_RPC_URL
DEFAULT_NETWORK = "xrpl:1"
DEFAULT_MAX_SPEND_ENV = "XRPL_MPP_MAX_SPEND"
DEFAULT_TIMEOUT = 20.0


@dataclass(slots=True)
class PayResult:
    status_code: int
    body: bytes
    headers: dict[str, str]
    challenge_present: bool
    dry_run: bool
    paid: bool
    preview: dict[str, Any] | None = None
    receipt: ReceiptRecord | None = None
    payment_response: PaymentReceipt | None = None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class _NonClosingTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        return None


class XRPLPayer:
    def __init__(
        self,
        signer: XRPLPaymentSigner | None,
        *,
        network: str | None = None,
        store: ReceiptStore | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        asset: str | None = None,
    ) -> None:
        self.signer = signer
        self.network = network or (signer.network if signer is not None else None) or DEFAULT_NETWORK
        self.store = store or ReceiptStore()
        self.timeout = timeout
        self.asset = asset

    async def pay(
        self,
        *,
        url: str,
        amount: float | Decimal = Decimal("0.001"),
        asset: str = "XRP",
        issuer: str | None = None,
        max_spend: float | Decimal | None = None,
        dry_run: bool = False,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> PayResult:
        asset_identifier = resolve_asset_identifier(asset=asset, issuer=issuer, network=self.network)
        spend_cap = resolve_spend_cap(amount=amount, max_spend=max_spend)
        request_headers = dict(headers or {})
        shared_transport = (
            _NonClosingTransport(transport)
            if transport is not None
            else None
        )

        async with httpx.AsyncClient(timeout=self.timeout, transport=shared_transport) as probe_client:
            initial_response = await probe_client.request(
                method=method,
                url=url,
                headers=request_headers,
                content=content,
            )
            await initial_response.aread()
            challenges = decode_payment_challenges_response(initial_response.headers)

            if dry_run:
                preview = build_dry_run_preview(
                    response=initial_response,
                    challenges=challenges,
                    network=self.network,
                    asset_identifier=asset_identifier,
                    spend_cap=spend_cap,
                )
                return PayResult(
                    status_code=initial_response.status_code,
                    body=initial_response.content,
                    headers=dict(initial_response.headers),
                    challenge_present=bool(challenges),
                    dry_run=True,
                    paid=False,
                    preview=preview,
                )

        if initial_response.status_code == 402 and not challenges:
            raise ValueError("402 response did not include a valid MPP challenge")

        if not challenges:
            return PayResult(
                status_code=initial_response.status_code,
                body=initial_response.content,
                headers=dict(initial_response.headers),
                challenge_present=False,
                dry_run=False,
                paid=False,
            )

        selected = select_payment_challenge(challenges, network=self.network, asset=asset_identifier)
        challenge_request = decode_challenge_request(selected)
        option_amount = payment_challenge_amount(selected)
        if spend_cap is not None and option_amount > spend_cap:
            raise ValueError(f"Payment amount {option_amount} exceeds configured spend cap {spend_cap}")

        if self.signer is None:
            raise RuntimeError("XRPL_WALLET_SEED is required to pay MPP resources")

        active_transport = XRPLPaymentTransport(
            self.signer,
            network=self.network,
            asset=asset_identifier,
            base_transport=shared_transport,
        ) if shared_transport is not None else XRPLPaymentTransport(
            self.signer,
            network=self.network,
            asset=asset_identifier,
        )
        async with httpx.AsyncClient(transport=active_transport, timeout=self.timeout) as client:
            response = await client.request(
                method=method,
                url=url,
                headers=request_headers,
                content=content,
            )
            await response.aread()
            payment_receipt = decode_payment_receipt_header(response.headers)
            receipt = None
            if payment_receipt is not None:
                receipt = build_receipt_record(
                    url=url,
                    method=method,
                    status_code=response.status_code,
                    payment_receipt=payment_receipt,
                )
                self.store.append(receipt)

            return PayResult(
                status_code=response.status_code,
                body=response.content,
                headers=dict(response.headers),
                challenge_present=True,
                dry_run=False,
                paid=payment_receipt is not None,
                receipt=receipt,
                payment_response=payment_receipt,
            )


async def pay_with_mpp(**kwargs: Any) -> PayResult:
    signer = kwargs.pop("signer", None)
    rpc_url = kwargs.pop("rpc_url", None)
    timeout = float(kwargs.pop("timeout", DEFAULT_TIMEOUT))
    dry_run = bool(kwargs.get("dry_run", False))
    if signer is None and not dry_run:
        signer = build_signer_from_env(rpc_url=rpc_url, network=kwargs.get("network"))
    network = kwargs.pop("network", None) or (signer.network if signer is not None else None) or DEFAULT_NETWORK
    store = kwargs.pop("store", None)
    payer = XRPLPayer(signer, network=network, store=store, timeout=timeout)
    return await payer.pay(**kwargs)


def resolve_signer_rpc_url(
    *,
    rpc_url: str | None = None,
    network: str,
) -> str:
    resolved_rpc_url = (rpc_url or "").strip() or getenv_clean("XRPL_RPC_URL")
    if resolved_rpc_url:
        return resolved_rpc_url

    if network == DEFAULT_NETWORK:
        return resolve_testnet_rpc_url()

    return DEFAULT_RPC_URL


def build_signer_from_env(
    *,
    rpc_url: str | None = None,
    network: str | None = None,
) -> XRPLPaymentSigner:
    wallet_seed = getenv_clean("XRPL_WALLET_SEED")
    if not wallet_seed:
        raise RuntimeError("XRPL_WALLET_SEED is required to pay MPP resources")

    wallet = Wallet.from_seed(wallet_seed)
    resolved_network = network or getenv_clean("XRPL_NETWORK") or getenv_clean("NETWORK_ID") or DEFAULT_NETWORK
    resolved_rpc_url = resolve_signer_rpc_url(rpc_url=rpc_url, network=resolved_network)
    return XRPLPaymentSigner(wallet, rpc_url=resolved_rpc_url, network=resolved_network)


def resolve_asset_identifier(*, asset: str, issuer: str | None, network: str) -> str:
    normalized_asset = normalize_currency_code(asset)
    if normalized_asset == "XRP":
        return "XRP:native"

    normalized_issuer = issuer.strip() if issuer else None
    if normalized_issuer is None and normalized_asset == "RLUSD":
        normalized_issuer = NETWORK_RLUSD_ISSUERS.get(network)
    if normalized_issuer is None and normalized_asset == "USDC":
        normalized_issuer = NETWORK_USDC_ISSUERS.get(network)
    if normalized_issuer is None:
        raise ValueError(f"Issuer is required for asset {normalized_asset}")
    return f"{normalized_asset}:{normalized_issuer}"


def resolve_spend_cap(
    *,
    amount: float | Decimal,
    max_spend: float | Decimal | None,
) -> Decimal | None:
    if max_spend is not None:
        return Decimal(str(max_spend))

    env_cap = getenv_clean(DEFAULT_MAX_SPEND_ENV)
    if env_cap:
        return Decimal(env_cap)
    return Decimal(str(amount))


def payment_request_amount(request: Any) -> Decimal:
    if request.currency == "XRP:native":
        return Decimal(request.amount) / Decimal("1000000")
    return Decimal(request.amount)


def payment_challenge_amount(challenge: PaymentChallenge) -> Decimal:
    request = decode_challenge_request(challenge)
    if challenge.intent == "session":
        if request.currency == "XRP:native":
            return Decimal(request.method_details.min_prepay_amount) / Decimal("1000000")
        return Decimal(request.method_details.min_prepay_amount)
    return payment_request_amount(request)


def build_receipt_record(
    *,
    url: str,
    method: str,
    status_code: int,
    payment_receipt: PaymentReceipt,
) -> ReceiptRecord:
    asset_identifier = canonical_asset_identifier(payment_receipt.asset) if payment_receipt.asset else "UNKNOWN:native"
    tx_hash = payment_receipt.tx_hash or payment_receipt.reference
    return ReceiptRecord(
        created_at=datetime.now(UTC).isoformat(),
        url=url,
        method=method.upper(),
        status_code=status_code,
        network=payment_receipt.network or DEFAULT_NETWORK,
        asset_identifier=asset_identifier,
        amount=payment_receipt_amount(payment_receipt),
        payer=payment_receipt.payer or "",
        tx_hash=tx_hash,
        settlement_status=payment_receipt.settlement_status or "success",
    )


def payment_receipt_amount(payment_receipt: PaymentReceipt) -> str:
    if payment_receipt.amount is None:
        return "0"
    if payment_receipt.amount.unit == "drops":
        return str(Decimal(payment_receipt.amount.drops) / Decimal("1000000"))
    return payment_receipt.amount.value


def build_dry_run_preview(
    *,
    response: httpx.Response,
    challenges: list,
    network: str,
    asset_identifier: str,
    spend_cap: Decimal | None,
) -> dict[str, Any]:
    preview: dict[str, Any] = {
        "mode": "dry_run",
        "status_code": response.status_code,
        "url": str(response.request.url),
        "network": network,
        "asset_identifier": asset_identifier,
        "spend_cap": str(spend_cap) if spend_cap is not None else None,
        "mpp_challenge_present": bool(challenges),
    }
    if not challenges:
        preview["message"] = "No valid MPP challenge detected; no payment attempted."
        return preview

    selected = select_payment_challenge(challenges, network=network, asset=asset_identifier)
    request = decode_challenge_request(selected)
    preview["selected_payment"] = {
        "intent": selected.intent,
        "recipient": request.recipient,
        "amount": str(payment_request_amount(request)),
        "initial_payment_amount": str(payment_challenge_amount(selected)),
        "asset_identifier": request.currency,
    }
    preview["would_pay"] = spend_cap is None or payment_challenge_amount(selected) <= spend_cap
    return preview


def format_pay_result(result: PayResult) -> str:
    if result.preview is not None:
        return json.dumps(result.preview, indent=2, sort_keys=True)
    if result.text.strip():
        return result.text

    summary = {
        "status_code": result.status_code,
        "paid": result.paid,
        "receipt": result.receipt.model_dump() if result.receipt is not None else None,
    }
    return json.dumps(summary, indent=2, sort_keys=True)


def get_receipts(limit: int = 10, *, store: ReceiptStore | None = None) -> list[dict[str, Any]]:
    active_store = store or ReceiptStore()
    return [receipt.model_dump() for receipt in active_store.list(limit=limit)]


def budget_status(
    *,
    asset: str = "XRP",
    issuer: str | None = None,
    network: str | None = None,
    store: ReceiptStore | None = None,
) -> dict[str, str | None]:
    resolved_network = network or getenv_clean("XRPL_NETWORK") or getenv_clean("NETWORK_ID") or DEFAULT_NETWORK
    asset_identifier = resolve_asset_identifier(asset=asset, issuer=issuer, network=resolved_network)
    env_cap = getenv_clean(DEFAULT_MAX_SPEND_ENV)
    max_spend = Decimal(env_cap) if env_cap else None
    active_store = store or ReceiptStore()
    return active_store.budget_summary(asset_identifier=asset_identifier, max_spend=max_spend)
