from __future__ import annotations

import base64
from collections.abc import Iterable, Mapping
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import hmac
import json
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from xrpl_mpp_mcp.constants import (
    CREDENTIAL_META_KEY,
    PAID_MCP_OPERATIONS,
    RESOURCES_READ,
)
from xrpl_mpp_mcp.models import MCPPaymentChallenge


PaidMCPMethod = Literal["tools/call", "resources/read", "prompts/get"]


class OperationBindingError(ValueError):
    pass


class UnsupportedPaidOperationError(OperationBindingError):
    pass


class PaidOperationBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    method: PaidMCPMethod
    target: str
    params: dict[str, Any]
    digest: str


def canonical_json(value: Any) -> str:
    """Serialize an I-JSON value using RFC 8785 canonical ordering.

    MCP payment data is JSON-native. Values outside the JSON data model,
    unsafe integers, lone Unicode surrogates, NaN, and infinity are rejected
    rather than normalized. Float rendering follows ECMAScript's thresholds,
    which differ from Python's default JSON encoder around ``1e-6`` and
    ``1e21``.
    """

    return _canonical_json(value)


def _canonical_json(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        _utf16_sort_key(value)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, int):
        if abs(value) > 9_007_199_254_740_991:
            raise ValueError("integers outside the I-JSON safe range are not canonicalizable")
        return str(value)
    if isinstance(value, float):
        return _ecmascript_number(value)
    if isinstance(value, list):
        return "[" + ",".join(_canonical_json(item) for item in value) + "]"
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        keys = sorted(value, key=_utf16_sort_key)
        return "{" + ",".join(
            f"{_canonical_json(key)}:{_canonical_json(value[key])}" for key in keys
        ) + "}"
    raise TypeError(f"value of type {type(value).__name__} is not valid JSON")


def _utf16_sort_key(value: str) -> bytes:
    try:
        return value.encode("utf-16-be")
    except UnicodeEncodeError as exc:
        raise ValueError("lone Unicode surrogates are not valid I-JSON") from exc


def _ecmascript_number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("NaN and infinity are not valid I-JSON numbers")
    if value == 0:
        return "0"

    sign = "-" if value < 0 else ""
    raw = repr(abs(value)).lower()
    mantissa, separator, exponent_text = raw.partition("e")
    exponent = int(exponent_text) if separator else 0
    before, point, after = mantissa.partition(".")
    digits = (before + after).lstrip("0")
    if point:
        exponent -= len(after)
    while len(digits) > 1 and digits.endswith("0"):
        digits = digits[:-1]
        exponent += 1

    digit_count = len(digits)
    decimal_position = digit_count + exponent
    if digit_count <= decimal_position <= 21:
        rendered = digits + ("0" * (decimal_position - digit_count))
    elif 0 < decimal_position <= 21:
        rendered = digits[:decimal_position] + "." + digits[decimal_position:]
    elif -6 < decimal_position <= 0:
        rendered = "0." + ("0" * -decimal_position) + digits
    else:
        coefficient = digits[0]
        if digit_count > 1:
            coefficient += "." + digits[1:]
        scientific_exponent = decimal_position - 1
        exponent_sign = "+" if scientific_exponent >= 0 else ""
        rendered = f"{coefficient}e{exponent_sign}{scientific_exponent}"
    return sign + rendered


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _clean_params(params: Mapping[str, Any]) -> dict[str, Any]:
    cleaned = deepcopy(dict(params))
    metadata = cleaned.get("_meta")
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise OperationBindingError("params._meta must be a JSON object")
        metadata.pop(CREDENTIAL_META_KEY, None)
        if not metadata:
            cleaned.pop("_meta")
    return cleaned


def build_operation_binding(message: Mapping[str, Any]) -> PaidOperationBinding:
    method = message.get("method")
    if method not in PAID_MCP_OPERATIONS:
        raise UnsupportedPaidOperationError(f"unsupported paid MCP operation {method!r}")
    params = message.get("params", {})
    if not isinstance(params, Mapping):
        raise OperationBindingError("params must be a JSON object")
    cleaned = _clean_params(params)

    target_field = "uri" if method == RESOURCES_READ else "name"
    target = cleaned.get(target_field)
    if not isinstance(target, str) or not target.strip():
        raise OperationBindingError(
            f"{method} requires a non-empty params.{target_field} value"
        )

    canonical = canonical_json({"method": method, "params": cleaned}).encode("utf-8")
    digest = _base64url(hashlib.sha256(canonical).digest())
    return PaidOperationBinding(
        method=method,
        target=target,
        params=cleaned,
        digest=digest,
    )


def is_supported_paid_operation(message: Mapping[str, Any]) -> bool:
    return message.get("method") in PAID_MCP_OPERATIONS


def should_drop_paid_notification(message: Mapping[str, Any]) -> bool:
    return "id" not in message and is_supported_paid_operation(message)


def request_hash(request: Mapping[str, Any]) -> str:
    return _base64url(hashlib.sha256(canonical_json(request).encode("utf-8")).digest())


def build_bound_challenge_id(
    *,
    secret: str,
    realm: str,
    payment_method: str,
    intent: str,
    request: Mapping[str, Any],
    operation: PaidOperationBinding,
    expires: str | None = None,
) -> str:
    """Build a project-defined HMAC ID satisfying MCP's binding requirements."""

    slots = [
        realm,
        payment_method,
        intent,
        request_hash(request),
        expires or "",
        operation.method,
        operation.digest,
    ]
    signature = hmac.new(
        secret.encode("utf-8"),
        "|".join(slots).encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return _base64url(signature)


def build_bound_challenge(
    *,
    secret: str,
    realm: str,
    payment_method: str,
    intent: str,
    request: Mapping[str, Any],
    operation: PaidOperationBinding,
    expires: str | None = None,
    description: str | None = None,
) -> MCPPaymentChallenge:
    challenge_id = build_bound_challenge_id(
        secret=secret,
        realm=realm,
        payment_method=payment_method,
        intent=intent,
        request=request,
        operation=operation,
        expires=expires,
    )
    return MCPPaymentChallenge(
        id=challenge_id,
        realm=realm,
        method=payment_method,
        intent=intent,
        request=dict(request),
        expires=expires,
        description=description,
    )


def verify_bound_challenge(
    challenge: MCPPaymentChallenge,
    operation: PaidOperationBinding,
    *,
    secrets: str | Iterable[str],
) -> bool:
    candidates = [secrets] if isinstance(secrets, str) else list(secrets)
    return any(
        hmac.compare_digest(
            challenge.id,
            build_bound_challenge_id(
                secret=secret,
                realm=challenge.realm,
                payment_method=challenge.method,
                intent=challenge.intent,
                request=challenge.request,
                operation=operation,
                expires=challenge.expires,
            ),
        )
        for secret in candidates
    )


def challenge_is_expired(
    challenge: MCPPaymentChallenge,
    *,
    now: datetime | None = None,
) -> bool:
    if challenge.expires is None:
        return False
    normalized = (
        challenge.expires[:-1] + "+00:00"
        if challenge.expires.endswith("Z")
        else challenge.expires
    )
    expiry = datetime.fromisoformat(normalized)
    return expiry <= (now or datetime.now(UTC))


__all__ = [
    "OperationBindingError",
    "PaidMCPMethod",
    "PaidOperationBinding",
    "UnsupportedPaidOperationError",
    "build_bound_challenge",
    "build_bound_challenge_id",
    "build_operation_binding",
    "canonical_json",
    "challenge_is_expired",
    "is_supported_paid_operation",
    "request_hash",
    "should_drop_paid_notification",
    "verify_bound_challenge",
]
