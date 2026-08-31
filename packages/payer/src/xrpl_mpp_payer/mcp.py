from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json

from xrpl_mpp_core import getenv_clean
from xrpl_mpp_payer.payer import DEFAULT_MAX_SPEND_ENV, EXPECTED_RECIPIENT_ENV
from xrpl_mpp_payer.payer import budget_status as get_budget_status
from xrpl_mpp_payer.payer import close_with_mpp, format_pay_result, get_receipts, pay_with_mpp
from xrpl_mpp_payer.proxy import proxy_manager

try:
    from fastmcp import FastMCP
except ModuleNotFoundError:  # pragma: no cover - exercised through the CLI help path instead
    FastMCP = None


def _operator_policy() -> tuple[str, Decimal]:
    """Load policy only from operator-controlled process configuration."""

    recipient = getenv_clean(EXPECTED_RECIPIENT_ENV)
    raw_cap = getenv_clean(DEFAULT_MAX_SPEND_ENV)
    if recipient is None or raw_cap is None:
        raise RuntimeError(
            f"{EXPECTED_RECIPIENT_ENV} and {DEFAULT_MAX_SPEND_ENV} are required "
            "for MCP automatic payment"
        )
    try:
        cap = Decimal(raw_cap)
    except InvalidOperation as exc:
        raise RuntimeError(f"{DEFAULT_MAX_SPEND_ENV} must be a finite amount") from exc
    if not cap.is_finite() or cap < 0:
        raise RuntimeError(f"{DEFAULT_MAX_SPEND_ENV} must be a non-negative finite amount")
    return recipient, cap


async def pay_url(
    url: str,
    amount: float = 0.001,
    asset: str = "XRP",
    issuer: str | None = None,
    dry_run: bool = False,
    intent: str | None = None,
    channel_id: str | None = None,
    cumulative_amount: str = "0",
    open_transaction: str | None = None,
    channel_funding_amount: str | None = None,
) -> str:
    """Pay for a URL with XRPL MPP and return the response body."""

    recipient, spend_cap = (None, None) if dry_run else _operator_policy()
    result = await pay_with_mpp(
        url=url,
        amount=amount,
        asset=asset,
        issuer=issuer,
        max_spend=spend_cap,
        dry_run=dry_run,
        intent=intent,
        channel_id=channel_id,
        cumulative_amount=cumulative_amount,
        open_transaction=open_transaction,
        channel_funding_amount=channel_funding_amount,
        expected_recipient=recipient,
    )
    return format_pay_result(result)


async def close_channel(
    url: str,
    channel_id: str,
    cumulative_amount: str = "0",
) -> str:
    """Send a final cumulative PayChannel voucher."""

    recipient, spend_cap = _operator_policy()
    result = await close_with_mpp(
        url=url,
        channel_id=channel_id,
        cumulative_amount=cumulative_amount,
        max_spend=spend_cap,
        expected_recipient=recipient,
    )
    return format_pay_result(result)


async def list_receipts(limit: int = 10) -> str:
    """List recent MPP payment receipts."""

    receipts = get_receipts(limit=limit)
    if not receipts:
        return "No receipts recorded yet."
    return "\n".join(
        f"- {receipt['url']} -> {receipt['amount']} {receipt['currency']} ({receipt['reference']})"
        for receipt in receipts
    )


async def budget_status(asset: str = "XRP", issuer: str | None = None) -> str:
    """Show local spend totals and remaining budget for an asset."""

    summary = get_budget_status(asset=asset, issuer=issuer)
    return json.dumps(summary, indent=2, sort_keys=True)


async def proxy_mode(
    target_base_url: str,
    local_port: int = 8787,
    asset: str = "XRP",
    issuer: str | None = None,
    dry_run: bool = False,
    intent: str | None = None,
    channel_id: str | None = None,
    cumulative_amount: str = "0",
    open_transaction: str | None = None,
    channel_funding_amount: str | None = None,
) -> str:
    """Start or reuse the local MPP payer forward proxy."""

    recipient, spend_cap = (None, None) if dry_run else _operator_policy()
    bind_url = proxy_manager.start(
        target_base_url=target_base_url,
        port=local_port,
        asset=asset,
        issuer=issuer,
        max_spend=spend_cap,
        dry_run=dry_run,
        intent=intent,
        channel_id=channel_id,
        cumulative_amount=cumulative_amount,
        open_transaction=open_transaction,
        channel_funding_amount=channel_funding_amount,
        expected_recipient=recipient,
    )
    return f"Proxy ready at {bind_url} -> {target_base_url}"


if FastMCP is not None:
    mcp = FastMCP(
        name="xrpl-mpp-payer",
        instructions=(
            "Pay for 402-protected XRPL MPP resources. "
            "Use pay_url for charges or PayChannel open/voucher requests, "
            "close_channel for final vouchers, list_receipts for audit history, "
            "budget_status for local spend tracking, and proxy_mode to launch a local proxy. "
            "Automatic payment requires an operator-approved expected_recipient; never infer "
            "it from a payment challenge."
        ),
    )
    mcp.tool(pay_url)
    mcp.tool(close_channel)
    mcp.tool(list_receipts)
    mcp.tool(budget_status)
    mcp.tool(proxy_mode)
else:
    mcp = None


def main() -> None:
    if mcp is None:
        raise RuntimeError(
            "FastMCP is not installed. Reinstall with: pip install \"xrpl-mpp-payer[mcp]\""
        )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
