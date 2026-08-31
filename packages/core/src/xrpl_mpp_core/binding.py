from __future__ import annotations

import hashlib
import re


INVOICE_ID_PATTERN = re.compile(r"^[0-9A-Fa-f]{64}$")


def challenge_invoice_id(challenge_id: str) -> str:
    """Return XRPL's SHA-512Half binding for one MPP challenge ID.

    XRPL ``InvoiceID`` is 256 bits. Ripple's SDK hashes the UTF-8 challenge ID
    with SHA-512, takes the first 32 bytes, and renders uppercase hexadecimal.
    """

    if not isinstance(challenge_id, str):
        raise TypeError("challenge_id must be a string")
    if not challenge_id:
        raise ValueError("challenge_id must not be empty")
    return hashlib.sha512(challenge_id.encode("utf-8")).hexdigest()[:64].upper()


def is_invoice_id(value: object) -> bool:
    """Return whether a value has the 32-byte XRPL ``InvoiceID`` shape."""

    return isinstance(value, str) and INVOICE_ID_PATTERN.fullmatch(value) is not None


def normalize_invoice_id(value: str) -> str:
    """Validate an ``InvoiceID`` and normalize its hex digits to uppercase."""

    if not is_invoice_id(value):
        raise ValueError("InvoiceID must be exactly 64 hexadecimal characters")
    return value.upper()


# Match the name used by Ripple's TypeScript SDK.
challengeInvoiceId = challenge_invoice_id
