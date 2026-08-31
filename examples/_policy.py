from __future__ import annotations

from decimal import Decimal, InvalidOperation

from xrpl_mpp_core import parse_currency

XRP_DROPS_PER_XRP = Decimal("1000000")


def spend_cap_to_policy_amount(*, currency: str, max_spend: str) -> str:
    """Convert a user-unit spend cap to the MPP request's wire amount units."""

    try:
        cap = Decimal(max_spend)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("XRPL_MPP_MAX_SPEND must be a finite amount") from exc
    if not cap.is_finite() or cap < 0:
        raise ValueError("XRPL_MPP_MAX_SPEND must be a non-negative finite amount")
    if parse_currency(currency) == "XRP":
        drops = cap * XRP_DROPS_PER_XRP
        if drops != drops.to_integral_value():
            raise ValueError("XRPL_MPP_MAX_SPEND must resolve to a whole number of XRP drops")
        return str(int(drops))
    return format(cap, "f")
