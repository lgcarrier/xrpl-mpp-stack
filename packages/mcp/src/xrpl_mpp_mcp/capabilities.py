from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from xrpl_mpp_mcp.constants import PAYMENT_CAPABILITY_KEY
from xrpl_mpp_mcp.models import PaymentCapabilities, PaymentMethodCapability


class CapabilityError(ValueError):
    pass


def build_payment_capabilities(
    methods: Mapping[str, Iterable[str]],
) -> PaymentCapabilities:
    return PaymentCapabilities(
        methods={
            method: PaymentMethodCapability(intents=list(intents))
            for method, intents in methods.items()
        }
    )


def with_payment_capabilities(
    message: Mapping[str, Any],
    payment: PaymentCapabilities,
) -> dict[str, Any]:
    """Return an Initialize request/result with MPP capabilities advertised.

    The input is not mutated and unrelated MCP capabilities are preserved.
    """

    output = deepcopy(dict(message))
    capabilities = output.setdefault("capabilities", {})
    if not isinstance(capabilities, dict):
        raise CapabilityError("capabilities must be a JSON object")
    experimental = capabilities.setdefault("experimental", {})
    if not isinstance(experimental, dict):
        raise CapabilityError("capabilities.experimental must be a JSON object")
    experimental[PAYMENT_CAPABILITY_KEY] = payment.model_dump(
        by_alias=True,
        exclude_none=True,
    )
    return output


def extract_payment_capabilities(
    message: Mapping[str, Any],
) -> PaymentCapabilities | None:
    capabilities = message.get("capabilities")
    if capabilities is None:
        return None
    if not isinstance(capabilities, Mapping):
        raise CapabilityError("capabilities must be a JSON object")
    experimental = capabilities.get("experimental")
    if experimental is None:
        return None
    if not isinstance(experimental, Mapping):
        raise CapabilityError("capabilities.experimental must be a JSON object")
    payment = experimental.get(PAYMENT_CAPABILITY_KEY)
    if payment is None:
        return None
    return PaymentCapabilities.model_validate(payment)


__all__ = [
    "CapabilityError",
    "build_payment_capabilities",
    "extract_payment_capabilities",
    "with_payment_capabilities",
]
