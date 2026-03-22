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
from xrpl.wallet import Wallet

from devtools.live_testnet_support import (
    get_validated_balance,
    get_validated_trustline_balance,
)
from xrpl_mpp_client import (
    XRPLPaymentSigner,
    build_payment_authorization,
    decode_payment_challenges_response,
    decode_payment_receipt_header,
    select_payment_challenge,
)
from xrpl_mpp_core import (
    PaymentChallenge,
    PaymentReceipt,
    XRPLAsset,
    XRPLChargeRequest,
    XRPLSessionRequest,
    canonical_asset_identifier,
    decode_challenge_request,
    decode_charge_payload,
    decode_session_payload,
    parse_asset_identifier,
    xrpl_asset_from_identifier,
)
from xrpl_mpp_core.testnet_rpc import resolve_testnet_rpc_url

DEFAULT_ENV_PATH = Path(".env.quickstart")
DEFAULT_NETWORK = "xrpl:1"
DEFAULT_RPC_URL = "https://s.altnet.rippletest.net:51234"
DEFAULT_TARGET_URL = "http://127.0.0.1:8010/premium"
DEFAULT_TIMEOUT_SECONDS = 30.0
XRP_DROPS_PER_XRP = Decimal("1000000")
ISSUED_ASSET_TOPUP_COMMANDS = {
    "RLUSD": "python -m devtools.rlusd_topup",
    "USDC": "python -m devtools.usdc_topup",
}

PaymentRequest = XRPLChargeRequest | XRPLSessionRequest


class DemoPreflightError(RuntimeError):
    """Raised when the demo wallet state cannot satisfy the requested payment."""


@dataclass(frozen=True)
class DemoTraceConfig:
    wallet_seed: str
    rpc_url: str
    network: str
    target_url: str
    payment_asset: str | None
    timeout_seconds: float


@dataclass(frozen=True)
class WalletSnapshot:
    address: str
    xrp_drops: int
    asset_balance: Decimal | None = None


@dataclass(frozen=True)
class DemoTraceResult:
    challenge_status_code: int
    final_status_code: int
    challenge: PaymentChallenge
    request: PaymentRequest
    initial_payment_amount: str
    fee_drops: int
    wallet_a_before: WalletSnapshot
    wallet_b_before: WalletSnapshot
    wallet_a_after: WalletSnapshot
    wallet_b_after: WalletSnapshot
    payment_receipt: PaymentReceipt | None
    response_text: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a recording-friendly MPP demo trace that shows the payment challenge, "
            "wallet balances, XRPL fee, and final payment receipt."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Optional env file to load instead of relying on process environment",
    )
    parser.add_argument(
        "--target-url",
        default=None,
        help="Override TARGET_URL for the protected resource request",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="HTTP timeout in seconds for the demo requests",
    )
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
    if key in file_values:
        return file_values[key]
    value = os.getenv(key)
    if value is None:
        return None
    return value


def resolve_rpc_url(explicit_rpc_url: str | None, *, network: str) -> str:
    if explicit_rpc_url:
        return explicit_rpc_url
    if network == DEFAULT_NETWORK:
        return resolve_testnet_rpc_url()
    return DEFAULT_RPC_URL


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
    ).strip() or DEFAULT_NETWORK
    rpc_url = resolve_rpc_url(
        (resolve_env_value("XRPL_RPC_URL", file_values) or "").strip() or None,
        network=network,
    )
    resolved_target_url = (
        target_url
        or resolve_env_value("TARGET_URL", file_values)
        or DEFAULT_TARGET_URL
    ).strip()
    payment_asset = (resolve_env_value("PAYMENT_ASSET", file_values) or "").strip() or None
    return DemoTraceConfig(
        wallet_seed=wallet_seed,
        rpc_url=rpc_url,
        network=network,
        target_url=resolved_target_url,
        payment_asset=payment_asset,
        timeout_seconds=timeout_seconds,
    )


def build_signer(config: DemoTraceConfig) -> XRPLPaymentSigner:
    wallet = Wallet.from_seed(config.wallet_seed)
    return XRPLPaymentSigner(
        wallet,
        rpc_url=config.rpc_url,
        network=config.network,
    )


def snapshot_wallet(
    rpc_client: JsonRpcClient,
    *,
    address: str,
    asset: XRPLAsset,
) -> WalletSnapshot:
    xrp_drops = get_validated_balance(rpc_client, address)
    asset_balance: Decimal | None = None
    if asset.issuer is not None:
        asset_balance = get_validated_trustline_balance(
            rpc_client,
            address,
            asset.issuer,
            currency_code=asset.code,
        )
    return WalletSnapshot(
        address=address,
        xrp_drops=xrp_drops,
        asset_balance=asset_balance,
    )


def _emit(printer: Callable[[str], None] | None, text: str) -> None:
    if printer is not None:
        printer(text)


def build_preflight_error(
    *,
    asset: XRPLAsset,
    required_amount: Decimal,
    wallet_a: WalletSnapshot,
    wallet_b: WalletSnapshot,
) -> str | None:
    if asset.issuer is None:
        return None

    buyer_balance = wallet_b.asset_balance or Decimal("0")
    if buyer_balance >= required_amount:
        return None

    asset_code = asset.code
    message = (
        f"Buyer wallet {wallet_b.address} only has {format_decimal(buyer_balance)} {asset_code}, "
        f"but this demo needs {format_decimal(required_amount)} {asset_code}."
    )
    merchant_balance = wallet_a.asset_balance
    if merchant_balance is not None and merchant_balance > 0:
        message += (
            f" Merchant wallet {wallet_a.address} currently holds "
            f"{format_decimal(merchant_balance)} {asset_code}."
        )

    topup_command = ISSUED_ASSET_TOPUP_COMMANDS.get(asset_code.upper())
    if topup_command is not None:
        message += (
            f" Run `{topup_command}` to fund the dedicated {asset_code} buyer wallet, "
            "then retry the demo."
        )
    else:
        message += " Fund the buyer wallet before retrying the demo."
    return message


async def run_demo_trace(
    *,
    signer: XRPLPaymentSigner,
    rpc_client: JsonRpcClient,
    target_url: str,
    payment_asset: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    transport: httpx.AsyncBaseTransport | None = None,
    printer: Callable[[str], None] | None = None,
) -> DemoTraceResult:
    _emit(
        printer,
        "Step 1: Requesting the protected resource to capture the MPP payment challenge...",
    )
    async with httpx.AsyncClient(transport=transport, timeout=timeout_seconds) as client:
        initial_response = await client.get(target_url)
        await initial_response.aread()
        challenges = decode_payment_challenges_response(initial_response.headers)
        if initial_response.status_code != 402 or not challenges:
            raise RuntimeError(
                f"Expected an MPP payment challenge from {target_url}, got HTTP "
                f"{initial_response.status_code}"
            )

        challenge = select_payment_challenge(
            challenges,
            network=signer.network,
            asset=payment_asset,
        )
        request_model = decode_challenge_request(challenge)
        asset = xrpl_asset_from_identifier(request_model.currency)
        initial_payment_amount = request_initial_payment_amount(challenge, request_model)
        _emit(
            printer,
            render_challenge_section(initial_response.status_code, challenge, request_model),
        )

        _emit(
            printer,
            "Step 2: Capturing before balances for wallet A and wallet B...",
        )
        wallet_a_before = await asyncio.to_thread(
            snapshot_wallet,
            rpc_client,
            address=request_model.recipient,
            asset=asset,
        )
        wallet_b_before = await asyncio.to_thread(
            snapshot_wallet,
            rpc_client,
            address=signer.wallet.classic_address,
            asset=asset,
        )
        _emit(
            printer,
            render_wallet_section(
                "Before",
                wallet_a=wallet_a_before,
                wallet_b=wallet_b_before,
                asset=asset,
            ),
        )
        preflight_error = build_preflight_error(
            asset=asset,
            required_amount=request_initial_payment_decimal(challenge, request_model),
            wallet_a=wallet_a_before,
            wallet_b=wallet_b_before,
        )
        if preflight_error is not None:
            _emit(printer, render_preflight_blocked_section(preflight_error))
            raise DemoPreflightError(preflight_error)

        _emit(printer, "Step 3: Signing the XRPL payment...")
        credential = await build_initial_credential(signer, challenge)
        fee_drops = signed_payment_fee_drops(credential)
        _emit(
            printer,
            render_signing_section(
                challenge=challenge,
                request_model=request_model,
                fee_drops=fee_drops,
            ),
        )

        _emit(printer, "Step 4: Submitting the paid retry request...")
        retry_response = await client.get(
            target_url,
            headers={"Authorization": build_payment_authorization(credential)},
        )
        await retry_response.aread()
        payment_receipt = decode_payment_receipt_header(retry_response.headers)
        _emit(
            printer,
            render_response_section(
                status_code=retry_response.status_code,
                response_text=retry_response.text,
                payment_receipt=payment_receipt,
            ),
        )

        _emit(printer, "Step 5: Capturing after balances and deltas...")
        wallet_a_after = await asyncio.to_thread(
            snapshot_wallet,
            rpc_client,
            address=request_model.recipient,
            asset=asset,
        )
        wallet_b_after = await asyncio.to_thread(
            snapshot_wallet,
            rpc_client,
            address=signer.wallet.classic_address,
            asset=asset,
        )

    result = DemoTraceResult(
        challenge_status_code=initial_response.status_code,
        final_status_code=retry_response.status_code,
        challenge=challenge,
        request=request_model,
        initial_payment_amount=initial_payment_amount,
        fee_drops=fee_drops,
        wallet_a_before=wallet_a_before,
        wallet_b_before=wallet_b_before,
        wallet_a_after=wallet_a_after,
        wallet_b_after=wallet_b_after,
        payment_receipt=payment_receipt,
        response_text=retry_response.text,
    )
    _emit(printer, render_after_section(result))
    return result


def render_trace(result: DemoTraceResult) -> str:
    sections = [
        "Step 1: Requesting the protected resource to capture the MPP payment challenge...",
        render_challenge_section(result.challenge_status_code, result.challenge, result.request),
        "Step 2: Capturing before balances for wallet A and wallet B...",
        render_wallet_section(
            "Before",
            wallet_a=result.wallet_a_before,
            wallet_b=result.wallet_b_before,
            asset=request_asset(result.request),
        ),
        "Step 3: Signing the XRPL payment...",
        render_signing_section(
            challenge=result.challenge,
            request_model=result.request,
            fee_drops=result.fee_drops,
        ),
        "Step 4: Submitting the paid retry request...",
        render_response_section(
            status_code=result.final_status_code,
            response_text=result.response_text,
            payment_receipt=result.payment_receipt,
        ),
        "Step 5: Capturing after balances and deltas...",
        render_after_section(result),
    ]
    return "\n\n".join(sections)


def render_challenge_section(
    status_code: int,
    challenge: PaymentChallenge,
    request_model: PaymentRequest,
) -> str:
    lines = [
        "MPP payment challenge",
        f"  HTTP status: {status_code}",
        f"  intent: {challenge.intent}",
        f"  asset: {request_model.currency}",
        f"  amount: {format_request_amount(request_model)}",
        f"  pay to: {request_model.recipient}",
    ]
    if challenge.intent == "charge":
        lines.append(f"  invoice id: {request_model.method_details.invoice_id}")
    else:
        lines.extend(
            [
                f"  session id: {request_model.method_details.session_id}",
                f"  min prepay: {format_initial_payment_amount(challenge, request_model)}",
            ]
        )
    lines.append(f"  network: {request_model.method_details.network}")
    return "\n".join(lines)


def render_wallet_section(
    title: str,
    *,
    wallet_a: WalletSnapshot,
    wallet_b: WalletSnapshot,
    asset: XRPLAsset,
) -> str:
    lines = [title]
    lines.extend(render_wallet_lines("Wallet A (merchant/payTo)", wallet_a, asset))
    lines.extend(render_wallet_lines("Wallet B (buyer/payer)", wallet_b, asset))
    return "\n".join(lines)


def render_wallet_lines(label: str, snapshot: WalletSnapshot, asset: XRPLAsset) -> list[str]:
    lines = [
        f"{label}: {snapshot.address}",
        f"  XRP: {format_xrp_balance(snapshot.xrp_drops)}",
    ]
    if snapshot.asset_balance is not None and asset.issuer is not None:
        lines.append(f"  {asset.code}: {format_decimal(snapshot.asset_balance)}")
    return lines


def render_signing_section(
    *,
    challenge: PaymentChallenge,
    request_model: PaymentRequest,
    fee_drops: int,
) -> str:
    lines = [
        "Payment being signed",
        f"  intent: {challenge.intent}",
        f"  XRPL fee: {fee_drops} drops ({format_xrp_balance(fee_drops)} XRP)",
    ]
    if challenge.intent == "charge":
        lines.append(f"  invoice id: {request_model.method_details.invoice_id}")
    else:
        lines.append(f"  session id: {request_model.method_details.session_id}")
    lines.append(f"  initial payment amount: {format_initial_payment_amount(challenge, request_model)}")
    return "\n".join(lines)


def render_preflight_blocked_section(detail: str) -> str:
    return "\n".join(
        [
            "Preflight check",
            "  status: blocked",
            f"  detail: {detail}",
        ]
    )


def render_response_section(
    *,
    status_code: int,
    response_text: str,
    payment_receipt: PaymentReceipt | None,
) -> str:
    lines = [
        "Merchant response",
        f"  HTTP status: {status_code}",
    ]
    formatted_body = format_response_body(response_text)
    if formatted_body:
        lines.append(f"  body: {formatted_body}")
    if payment_receipt is not None:
        lines.extend(
            [
                "",
                "MPP payment receipt",
                f"  intent: {payment_receipt.intent}",
                f"  reference: {payment_receipt.reference}",
                f"  tx hash: {payment_receipt.tx_hash or payment_receipt.reference}",
                f"  settlement: {payment_receipt.settlement_status}",
            ]
        )
        if payment_receipt.invoice_id:
            lines.append(f"  invoice id: {payment_receipt.invoice_id}")
        if payment_receipt.session_id:
            lines.append(f"  session id: {payment_receipt.session_id}")
    return "\n".join(lines)


def render_after_section(result: DemoTraceResult) -> str:
    sections = [
        render_wallet_section(
            "After",
            wallet_a=result.wallet_a_after,
            wallet_b=result.wallet_b_after,
            asset=request_asset(result.request),
        ),
        render_delta_section(result),
        render_summary_section(result),
    ]
    return "\n\n".join(sections)


def render_delta_section(result: DemoTraceResult) -> str:
    wallet_a_xrp_delta = result.wallet_a_after.xrp_drops - result.wallet_a_before.xrp_drops
    wallet_b_xrp_delta = result.wallet_b_after.xrp_drops - result.wallet_b_before.xrp_drops
    wallet_a_asset_delta = asset_delta(result.wallet_a_before, result.wallet_a_after)
    wallet_b_asset_delta = asset_delta(result.wallet_b_before, result.wallet_b_after)
    asset = request_asset(result.request)

    lines = [
        "Delta",
        "Wallet A: "
        + format_delta_line(
            xrp_drops=wallet_a_xrp_delta,
            asset_code=asset.code,
            asset_delta=wallet_a_asset_delta,
        ),
        "Wallet B: "
        + format_delta_line(
            xrp_drops=wallet_b_xrp_delta,
            asset_code=asset.code,
            asset_delta=wallet_b_asset_delta,
        ),
    ]
    return "\n".join(lines)


def render_summary_section(result: DemoTraceResult) -> str:
    lines = [
        "Payment summary",
        f"  HTTP status: {result.final_status_code}",
        f"  intent: {result.challenge.intent}",
    ]
    if result.payment_receipt is not None:
        if result.payment_receipt.invoice_id:
            lines.append(f"  invoice id: {result.payment_receipt.invoice_id}")
        if result.payment_receipt.session_id:
            lines.append(f"  session id: {result.payment_receipt.session_id}")
        lines.append(f"  tx hash: {result.payment_receipt.tx_hash or result.payment_receipt.reference}")
    return "\n".join(lines)


async def build_initial_credential(signer: XRPLPaymentSigner, challenge: PaymentChallenge):
    if challenge.intent == "charge":
        return await signer.build_charge_credential_async(challenge)
    return await signer.build_session_open_credential_async(challenge)


def signed_payment_fee_drops(credential) -> int:
    if credential.challenge.intent == "charge":
        payload = decode_charge_payload(credential)
        return int(str(binarycodec.decode(payload.signed_tx_blob)["Fee"]))
    payload = decode_session_payload(credential)
    return int(str(binarycodec.decode(payload.signed_tx_blob or "")["Fee"]))


def request_asset(request_model: PaymentRequest) -> XRPLAsset:
    return xrpl_asset_from_identifier(request_model.currency)


def request_initial_payment_amount(
    challenge: PaymentChallenge,
    request_model: PaymentRequest,
) -> str:
    if challenge.intent == "session":
        return request_model.method_details.min_prepay_amount
    return request_model.amount


def request_initial_payment_decimal(
    challenge: PaymentChallenge,
    request_model: PaymentRequest,
) -> Decimal:
    asset = parse_asset_identifier(request_model.currency)
    raw_value = request_initial_payment_amount(challenge, request_model)
    if asset.issuer is None:
        return Decimal(raw_value) / XRP_DROPS_PER_XRP
    return Decimal(raw_value)


def format_initial_payment_amount(
    challenge: PaymentChallenge,
    request_model: PaymentRequest,
) -> str:
    asset = request_asset(request_model)
    raw_value = request_initial_payment_amount(challenge, request_model)
    if asset.issuer is None:
        return format_asset_amount(asset, raw_value, "drops")
    return format_asset_amount(asset, raw_value, "issued")


def format_request_amount(request_model: PaymentRequest) -> str:
    asset = request_asset(request_model)
    if asset.issuer is None:
        return format_asset_amount(asset, request_model.amount, "drops")
    return format_asset_amount(asset, request_model.amount, "issued")


def asset_delta(before: WalletSnapshot, after: WalletSnapshot) -> Decimal | None:
    if before.asset_balance is None or after.asset_balance is None:
        return None
    return after.asset_balance - before.asset_balance


def format_delta_line(
    *,
    xrp_drops: int,
    asset_code: str,
    asset_delta: Decimal | None,
) -> str:
    line = f"XRP {format_signed_xrp_delta(xrp_drops)}"
    if asset_delta is not None:
        line += f", {asset_code} {format_signed_decimal(asset_delta)}"
    return line


def format_asset_amount(asset: XRPLAsset, raw_value: str, unit: str) -> str:
    if unit == "drops":
        drops = int(raw_value)
        return f"{format_xrp_balance(drops)} XRP ({drops} drops)"
    return f"{format_decimal(Decimal(raw_value))} {asset.code}"


def format_xrp_balance(drops: int) -> str:
    return format((Decimal(drops) / XRP_DROPS_PER_XRP).quantize(Decimal("0.000001")), "f")


def format_signed_xrp_delta(drops: int) -> str:
    prefix = "+" if drops >= 0 else "-"
    return f"{prefix}{format_xrp_balance(abs(drops))}"


def format_decimal(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


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
    signer = build_signer(config)
    rpc_client = JsonRpcClient(config.rpc_url)
    try:
        asyncio.run(
            run_demo_trace(
                signer=signer,
                rpc_client=rpc_client,
                target_url=config.target_url,
                payment_asset=config.payment_asset,
                timeout_seconds=config.timeout_seconds,
                printer=lambda text: print(text, flush=True),
            )
        )
    except DemoPreflightError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
