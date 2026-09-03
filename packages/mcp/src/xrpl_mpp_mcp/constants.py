from __future__ import annotations

from enum import IntEnum


CREDENTIAL_META_KEY = "org.paymentauth/credential"
RECEIPT_META_KEY = "org.paymentauth/receipt"
PAYMENT_CAPABILITY_KEY = "payment"

TOOLS_CALL = "tools/call"
RESOURCES_READ = "resources/read"
PROMPTS_GET = "prompts/get"

PAID_MCP_OPERATIONS = frozenset({TOOLS_CALL, RESOURCES_READ, PROMPTS_GET})


class PaymentErrorCode(IntEnum):
    """JSON-RPC error codes assigned by the MPP JSON-RPC transport draft."""

    PAYMENT_REQUIRED = -32042
    PAYMENT_VERIFICATION_FAILED = -32043
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603


__all__ = [
    "CREDENTIAL_META_KEY",
    "PAID_MCP_OPERATIONS",
    "PAYMENT_CAPABILITY_KEY",
    "PROMPTS_GET",
    "RECEIPT_META_KEY",
    "RESOURCES_READ",
    "TOOLS_CALL",
    "PaymentErrorCode",
]
