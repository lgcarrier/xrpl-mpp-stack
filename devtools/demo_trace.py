from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Callable, Sequence

import httpx
from xrpl.clients import JsonRpcClient
from xrpl.core import binarycodec
from xrpl.models.transactions import Payment, Transaction
from xrpl.wallet import Wallet

from devtools.live_testnet_support import (
    get_validated_balance,
    get_validated_trustline_balance,
)
from examples._policy import spend_cap_to_policy_amount
from xrpl_mpp_client import (
    XRPLPaymentPolicy,
    XRPLPaymentSigner,
    build_payment_authorization,
    decode_payment_challenges_response,
    decode_payment_receipt_header,
    select_payment_challenge,
)
from xrpl_mpp_core import (
    IssuedCurrency,
    MPToken,
    PaymentChallenge,
    PaymentCredential,
    PaymentReceipt,
    XRPLChargeRequest,
    challenge_invoice_id,
    decode_challenge_request,
    decode_charge_payload,
    normalize_currency_code,
    parse_currency,
    payment_credential_header,
)
from xrpl_mpp_core.testnet_rpc import resolve_testnet_rpc_url

DEFAULT_ENV_PATH = Path(".env.quickstart")
DEFAULT_NETWORK = "testnet"
DEFAULT_MAINNET_RPC_URL = "https://s1.ripple.com:51234"
DEFAULT_TARGET_URL = "http://127.0.0.1:8010/premium"
DEFAULT_PAYMENT_CURRENCY = "XRP"
DEFAULT_TIMEOUT_SECONDS = 30.0
XRP_DROPS_PER_XRP = Decimal("1000000")
ISSUED_CURRENCY_FUNDING_COMMANDS = {
    "RLUSD": "python -m devtools.rlusd_fund --target-rlusd 10 --max-xrp 35",
    "USDC": "python -m devtools.usdc_topup",
}


class DemoPreflightError(RuntimeError):
    """Raised when the demo wallet cannot satisfy a charge challenge."""


@dataclass(frozen=True)
class DemoTraceConfig:
    wallet_seed: str
    rpc_url: str
    network: str
    target_url: str
    payment_currency: str
    expected_recipient: str
    max_payment_amount: str
    timeout_seconds: float


@dataclass(frozen=True)
class CurrencyView:
    wire: str
    label: str
    issuer: str | None = None


@dataclass(frozen=True)
class WalletSnapshot:
    address: str
    xrp_drops: int
    currency_balance: Decimal | None = None


@dataclass(frozen=True)
class DemoTraceResult:
    challenge_status_code: int
    final_status_code: int
    challenge: PaymentChallenge
    request: XRPLChargeRequest
    fee_drops: int
    merchant_before: WalletSnapshot
    payer_before: WalletSnapshot
    merchant_after: WalletSnapshot
    payer_after: WalletSnapshot
    payment_receipt: PaymentReceipt | None
    response_text: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a recording-friendly MPP 0.2 charge trace showing the challenge, "
            "wallet balances, XRPL fee, and core payment receipt."
        ),
    )
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--target-url", default=None)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    return parser


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def resolve_env_value(key: str, file_values: dict[str, str]) -> str | None:
    return file_values[key] if key in file_values else os.getenv(key)


def resolve_rpc_url(explicit_rpc_url: str | None, *, network: str) -> str:
    if explicit_rpc_url:
        return explicit_rpc_url
    if network == "testnet":
        return resolve_testnet_rpc_url()
    return DEFAULT_MAINNET_RPC_URL


def resolve_config(
    *,
    env_file: str | None,
    target_url: str | None,
    timeout_seconds: float,
) -> DemoTraceConfig:
    if env_file:
        file_values = load_env_file(Path(env_file))
    elif not os.getenv("XRPL_WALLET_SEED") and DEFAULT_ENV_PATH.exists():
        file_values = load_env_file(DEFAULT_ENV_PATH)
    else:
        file_values = {}
    wallet_seed = (resolve_env_value("XRPL_WALLET_SEED", file_values) or "").strip()
    if not wallet_seed:
        raise RuntimeError("XRPL_WALLET_SEED is required to run the demo trace")
    network = (
        resolve_env_value("XRPL_NETWORK", file_values)
        or resolve_env_value("NETWORK_ID", file_values)
        or DEFAULT_NETWORK
    ).strip()
    rpc_url = resolve_rpc_url(
        (resolve_env_value("XRPL_RPC_URL", file_values) or "").strip() or None,
        network=network,
    )
    resolved_target_url = (
        target_url
        or resolve_env_value("TARGET_URL", file_values)
        or DEFAULT_TARGET_URL
    ).strip()
    payment_currency = (
        resolve_env_value("PAYMENT_CURRENCY", file_values)
        or DEFAULT_PAYMENT_CURRENCY
    ).strip()
    parse_currency(payment_currency)
    expected_recipient = (
        resolve_env_value("XRPL_MPP_EXPECTED_RECIPIENT", file_values)
        or resolve_env_value("MERCHANT_XRPL_ADDRESS", file_values)
        or resolve_env_value("MY_DESTINATION_ADDRESS", file_values)
        or ""
    ).strip()
    configured_spend_cap = (
        resolve_env_value("XRPL_MPP_MAX_SPEND", file_values) or ""
    ).strip()
    max_payment_amount = (
        spend_cap_to_policy_amount(
            currency=payment_currency,
            max_spend=configured_spend_cap,
        )
        if configured_spend_cap
        else (resolve_env_value("PRICE_AMOUNT", file_values) or "").strip()
    )
    if not expected_recipient or not max_payment_amount:
        raise RuntimeError(
            "XRPL_MPP_EXPECTED_RECIPIENT and XRPL_MPP_MAX_SPEND (or the local "
            "PRICE_AMOUNT fallback) are required to run the demo trace"
        )
    return DemoTraceConfig(
        wallet_seed=wallet_seed,
        rpc_url=rpc_url,
        network=network,
        target_url=resolved_target_url,
        payment_currency=payment_currency,
        expected_recipient=expected_recipient,
        max_payment_amount=max_payment_amount,
        timeout_seconds=timeout_seconds,
    )


def build_signer(config: DemoTraceConfig) -> XRPLPaymentSigner:
    return XRPLPaymentSigner(
        Wallet.from_seed(config.wallet_seed),
        rpc_url=config.rpc_url,
        network=config.network,
    )


def currency_view(value: str) -> CurrencyView:
    currency = parse_currency(value)
    if currency == "XRP":
        return CurrencyView(wire=value, label="XRP")
    if isinstance(currency, IssuedCurrency):
        return CurrencyView(
            wire=value,
            label=normalize_currency_code(currency.currency),
            issuer=currency.issuer,
        )
    if isinstance(currency, MPToken):
        return CurrencyView(wire=value, label=f"MPT:{currency.mpt_issuance_id}")
    raise TypeError("Unsupported XRPL currency")


def snapshot_wallet(
    rpc_client: JsonRpcClient,
    *,
    address: str,
    currency: CurrencyView,
) -> WalletSnapshot:
    balance: Decimal | None = None
    if currency.issuer is not None:
        balance = get_validated_trustline_balance(
            rpc_client,
            address,
            currency.issuer,
            currency_code=currency.label,
        )
    return WalletSnapshot(
        address=address,
        xrp_drops=get_validated_balance(rpc_client, address),
        currency_balance=balance,
    )


def build_preflight_error(
    *,
    currency: CurrencyView,
    required_amount: Decimal,
    merchant: WalletSnapshot,
    payer: WalletSnapshot,
) -> str | None:
    if currency.issuer is None:
        return None
    payer_balance = payer.currency_balance or Decimal("0")
    if payer_balance >= required_amount:
        return None
    detail = (
        f"Payer wallet {payer.address} only has {format_decimal(payer_balance)} "
        f"{currency.label}, but this charge needs {format_decimal(required_amount)}."
    )
    merchant_balance = merchant.currency_balance
    if merchant_balance is not None and merchant_balance > 0:
        detail += (
            f" Merchant wallet {merchant.address} holds "
            f"{format_decimal(merchant_balance)} {currency.label}."
        )
    command = ISSUED_CURRENCY_FUNDING_COMMANDS.get(currency.label.upper())
    detail += (
        f" Run `{command}` to fund the payer wallet, then retry."
        if command
        else " Fund the payer wallet before retrying."
    )
    return detail


def _emit(printer: Callable[[str], None] | None, text: str) -> None:
    if printer is not None:
        printer(text)


async def run_demo_trace(
    *,
    signer: XRPLPaymentSigner,
    rpc_client: JsonRpcClient,
    target_url: str,
    payment_currency: str = DEFAULT_PAYMENT_CURRENCY,
    expected_recipient: str,
    max_payment_amount: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    transport: httpx.AsyncBaseTransport | None = None,
    printer: Callable[[str], None] | None = None,
) -> DemoTraceResult:
    _emit(printer, "Step 1: requesting the protected resource and parsing MPP 0.2")
    async with httpx.AsyncClient(transport=transport, timeout=timeout_seconds) as client:
        initial_response = await client.get(target_url)
        await initial_response.aread()
        challenges = decode_payment_challenges_response(initial_response.headers)
        if initial_response.status_code != 402 or not challenges:
            raise RuntimeError(
                f"Expected an MPP challenge from {target_url}, got HTTP "
                f"{initial_response.status_code}"
            )
        challenge = select_payment_challenge(
            challenges,
            intent="charge",
            network=signer.network,
            currency=payment_currency,
        )
        decoded = decode_challenge_request(challenge)
        if not isinstance(decoded, XRPLChargeRequest):
            raise RuntimeError("Selected challenge is not an XRPL charge")
        request_model = decoded
        policy = XRPLPaymentPolicy(
            expected_recipients=expected_recipient,
            max_amount=max_payment_amount,
            allowed_currencies={payment_currency},
        )
        policy.authorize(challenge)
        view = currency_view(request_model.currency)
        _emit(
            printer,
            render_challenge_section(
                initial_response.status_code,
                challenge,
                request_model,
            ),
        )

        merchant_before = await asyncio.to_thread(
            snapshot_wallet,
            rpc_client,
            address=request_model.recipient,
            currency=view,
        )
        payer_before = await asyncio.to_thread(
            snapshot_wallet,
            rpc_client,
            address=signer.wallet.classic_address,
            currency=view,
        )
        detail = build_preflight_error(
            currency=view,
            required_amount=(
                Decimal(request_model.amount) / XRP_DROPS_PER_XRP
                if view.label == "XRP"
                else Decimal(request_model.amount)
            ),
            merchant=merchant_before,
            payer=payer_before,
        )
        if detail is not None:
            _emit(printer, f"Preflight check\n  status: blocked\n  detail: {detail}")
            raise DemoPreflightError(detail)

        credential = await signer.build_charge_credential_async(challenge)
        fee_drops = signed_payment_fee_drops(credential)
        _emit(printer, f"Step 2: signed challenge-bound charge (fee={fee_drops} drops)")
        retry_response = await client.get(
            target_url,
            headers={
                payment_credential_header(challenge): build_payment_authorization(
                    credential
                )
            },
        )
        await retry_response.aread()
        receipt = (
            decode_payment_receipt_header(retry_response.headers)
            if retry_response.is_success
            else None
        )
        if receipt is not None:
            validate_charge_receipt(
                challenge=challenge,
                credential=credential,
                receipt=receipt,
            )

        merchant_after = await asyncio.to_thread(
            snapshot_wallet,
            rpc_client,
            address=request_model.recipient,
            currency=view,
        )
        payer_after = await asyncio.to_thread(
            snapshot_wallet,
            rpc_client,
            address=signer.wallet.classic_address,
            currency=view,
        )

    result = DemoTraceResult(
        challenge_status_code=initial_response.status_code,
        final_status_code=retry_response.status_code,
        challenge=challenge,
        request=request_model,
        fee_drops=fee_drops,
        merchant_before=merchant_before,
        payer_before=payer_before,
        merchant_after=merchant_after,
        payer_after=payer_after,
        payment_receipt=receipt,
        response_text=retry_response.text,
    )
    _emit(printer, render_trace(result))
    return result


def render_challenge_section(
    status_code: int,
    challenge: PaymentChallenge,
    request: XRPLChargeRequest,
) -> str:
    details = request.method_details
    return "\n".join(
        [
            "MPP payment challenge",
            f"  HTTP status: {status_code}",
            f"  intent: {challenge.intent}",
            f"  currency: {request.currency}",
            f"  amount: {format_request_amount(request)}",
            f"  recipient: {request.recipient}",
            f"  network: {details.network if details else ''}",
            f"  invoice id: {details.invoice_id if details else ''}",
        ]
    )


def render_trace(result: DemoTraceResult) -> str:
    view = currency_view(result.request.currency)
    lines = [
        render_challenge_section(result.challenge_status_code, result.challenge, result.request),
        "Signed payment",
        f"  XRPL fee: {result.fee_drops} drops",
        "Merchant response",
        f"  HTTP status: {result.final_status_code}",
        f"  body: {format_response_body(result.response_text)}",
        "Balance deltas",
        "  merchant: " + format_delta(result.merchant_before, result.merchant_after, view),
        "  payer: " + format_delta(result.payer_before, result.payer_after, view),
    ]
    receipt = result.payment_receipt
    if receipt is not None:
        lines.extend(
            [
                "MPP payment receipt",
                f"  status: {receipt.status}",
                f"  method: {receipt.method}",
                f"  reference: {receipt.reference}",
                f"  tx hash: {receipt.tx_hash or receipt.reference}",
                f"  settlement: {receipt.settlement_status or ''}",
                f"  invoice id: {receipt.invoice_id or ''}",
            ]
        )
    return "\n".join(lines)


def signed_payment_fee_drops(credential) -> int:
    payload = decode_charge_payload(credential)
    if payload.type != "transaction":
        raise ValueError("Demo trace requires a pull-mode transaction credential")
    return int(str(binarycodec.decode(payload.blob)["Fee"]))


def validate_charge_receipt(
    *,
    challenge: PaymentChallenge,
    credential: PaymentCredential,
    receipt: PaymentReceipt,
) -> None:
    """Bind a successful demo receipt to the exact signed charge credential."""

    payload = decode_charge_payload(credential)
    if payload.type != "transaction":
        raise ValueError("Demo trace requires a pull-mode transaction credential")
    try:
        transaction = Transaction.from_xrpl(binarycodec.decode(payload.blob))
    except Exception as exc:
        raise ValueError("Demo trace signed an invalid XRPL transaction") from exc
    if not isinstance(transaction, Payment):
        raise ValueError("Demo trace signed transaction is not an XRPL Payment")

    request = decode_challenge_request(challenge)
    details = request.method_details
    expected_reference = transaction.get_hash().upper()
    expected_network = details.network if details is not None else None
    expected_invoice_id = (
        details.invoice_id
        if details is not None and details.invoice_id is not None
        else challenge_invoice_id(challenge.id)
    )
    if receipt.method != challenge.method:
        raise ValueError("Payment-Receipt method does not match the demo challenge")
    if receipt.reference.upper() != expected_reference:
        raise ValueError(
            "Payment-Receipt reference does not match the demo's signed transaction"
        )
    if receipt.challenge_id is not None and receipt.challenge_id != challenge.id:
        raise ValueError("Payment-Receipt challengeId does not match the demo challenge")
    if receipt.network is not None and receipt.network != expected_network:
        raise ValueError("Payment-Receipt network does not match the demo challenge")
    if receipt.payer is not None and receipt.payer != transaction.account:
        raise ValueError("Payment-Receipt payer does not match the demo transaction")
    if receipt.recipient is not None and receipt.recipient != request.recipient:
        raise ValueError("Payment-Receipt recipient does not match the demo challenge")
    if (
        receipt.invoice_id is not None
        and receipt.invoice_id.upper() != expected_invoice_id.upper()
    ):
        raise ValueError("Payment-Receipt invoiceId does not match the demo challenge")
    if receipt.tx_hash is not None and receipt.tx_hash.upper() != expected_reference:
        raise ValueError("Payment-Receipt txHash does not match the demo transaction")
    if (
        receipt.action is not None
        or receipt.channel_id is not None
        or receipt.cumulative is not None
    ):
        raise ValueError("Payment-Receipt contains session fields for a demo charge")


def format_request_amount(request: XRPLChargeRequest) -> str:
    view = currency_view(request.currency)
    if view.label == "XRP":
        drops = int(request.amount)
        return f"{format_xrp_balance(drops)} XRP ({drops} drops)"
    return f"{format_decimal(Decimal(request.amount))} {view.label}"


def format_delta(
    before: WalletSnapshot,
    after: WalletSnapshot,
    currency: CurrencyView,
) -> str:
    xrp_delta = after.xrp_drops - before.xrp_drops
    rendered = f"XRP {format_signed_xrp_delta(xrp_delta)}"
    if before.currency_balance is not None and after.currency_balance is not None:
        rendered += (
            f", {currency.label} "
            f"{format_signed_decimal(after.currency_balance - before.currency_balance)}"
        )
    return rendered


def format_xrp_balance(drops: int) -> str:
    return format(
        (Decimal(drops) / XRP_DROPS_PER_XRP).quantize(Decimal("0.000001")),
        "f",
    )


def format_signed_xrp_delta(drops: int) -> str:
    prefix = "+" if drops >= 0 else "-"
    return f"{prefix}{format_xrp_balance(abs(drops))}"


def format_decimal(value: Decimal) -> str:
    rendered = format(value.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def format_signed_decimal(value: Decimal) -> str:
    prefix = "+" if value >= 0 else "-"
    return f"{prefix}{format_decimal(abs(value))}"


def format_response_body(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped
    return json.dumps(parsed, sort_keys=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = resolve_config(
        env_file=args.env_file,
        target_url=args.target_url,
        timeout_seconds=args.timeout,
    )
    try:
        asyncio.run(
            run_demo_trace(
                signer=build_signer(config),
                rpc_client=JsonRpcClient(config.rpc_url),
                target_url=config.target_url,
                payment_currency=config.payment_currency,
                expected_recipient=config.expected_recipient,
                max_payment_amount=config.max_payment_amount,
                timeout_seconds=config.timeout_seconds,
                printer=lambda value: print(value, flush=True),
            )
        )
    except DemoPreflightError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
