#!/usr/bin/env python3
"""Expose xrpl-mpp-core through the mpp-tools adapter JSON ABI."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import sys
from typing import Any

from xrpl_mpp_core import (
    PaymentChallenge,
    PaymentCredential,
    PaymentReceipt,
    build_challenge_id,
    build_payment_authorization_value,
    decode_base64url_json,
    decode_base64url_text,
    decode_payment_receipt,
    encode_base64url_text,
    encode_json_to_base64url,
    encode_payment_receipt,
    parse_payment_authorization_header,
    parse_payment_challenge,
    render_payment_challenge,
)


MINIMUM_SECRET_KEY_BYTES = 32
AdapterHandler = Callable[[Mapping[str, Any]], Any]


def _success(value: Any) -> dict[str, Any]:
    return {"ok": True, "value": value}


def _error(error_type: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"type": error_type, "message": message}}


def _require_mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    return value


def _challenge_from_canonical(value: Any) -> PaymentChallenge:
    canonical = dict(_require_mapping(value, label="challenge"))
    request = _require_mapping(canonical.get("request"), label="challenge.request")
    canonical["request"] = encode_json_to_base64url(dict(request))
    return PaymentChallenge.model_validate(canonical)


def _challenge_to_canonical(challenge: PaymentChallenge) -> dict[str, Any]:
    canonical = challenge.model_dump(by_alias=True, exclude_none=True)
    request = decode_base64url_json(challenge.request)
    canonical["request"] = dict(_require_mapping(request, label="challenge.request"))
    return canonical


def _credential_from_canonical(value: Any) -> PaymentCredential:
    canonical = dict(_require_mapping(value, label="credential"))
    canonical["challenge"] = _challenge_from_canonical(canonical.get("challenge"))
    return PaymentCredential.model_validate(canonical)


def _credential_to_canonical(credential: PaymentCredential) -> dict[str, Any]:
    canonical = credential.model_dump(by_alias=True, exclude_none=True)
    canonical["challenge"] = _challenge_to_canonical(credential.challenge)
    return canonical


def _receipt_to_canonical(receipt: PaymentReceipt) -> dict[str, Any]:
    return receipt.model_dump(by_alias=True, exclude_none=True)


def _challenge_parse(value: Mapping[str, Any]) -> dict[str, Any]:
    challenge = parse_payment_challenge(str(value["header"]))
    return _challenge_to_canonical(challenge)


def _challenge_format(value: Mapping[str, Any]) -> dict[str, str]:
    return {"header": render_payment_challenge(_challenge_from_canonical(value))}


def _credential_parse(value: Mapping[str, Any]) -> dict[str, Any]:
    credential = parse_payment_authorization_header(str(value["header"]))
    return _credential_to_canonical(credential)


def _credential_format(value: Mapping[str, Any]) -> dict[str, str]:
    credential = _credential_from_canonical(value)
    return {"header": build_payment_authorization_value(credential)}


def _receipt_parse(value: Mapping[str, Any]) -> dict[str, Any]:
    receipt = decode_payment_receipt(str(value["header"]))
    return _receipt_to_canonical(receipt)


def _receipt_format(value: Mapping[str, Any]) -> dict[str, str]:
    receipt = PaymentReceipt.model_validate(value)
    return {"header": encode_payment_receipt(receipt)}


def _base64url_encode(value: Mapping[str, Any]) -> dict[str, str]:
    return {"text": encode_base64url_text(value["text"])}


def _base64url_decode(value: Mapping[str, Any]) -> dict[str, str]:
    return {"text": decode_base64url_text(value["text"])}


def _challenge_id(value: Mapping[str, Any]) -> dict[str, str]:
    secret_key = str(value["secretKey"])
    if len(secret_key.encode("utf-8")) < MINIMUM_SECRET_KEY_BYTES:
        raise ValueError(
            f"secretKey must be at least {MINIMUM_SECRET_KEY_BYTES} bytes"
        )
    request = dict(_require_mapping(value.get("request"), label="request"))
    challenge_id = build_challenge_id(
        secret=secret_key,
        realm=str(value["realm"]),
        method=str(value["method"]),
        intent=str(value["intent"]),
        request_b64=encode_json_to_base64url(request),
        expires=value.get("expires"),
        digest=value.get("digest"),
        opaque=value.get("opaque"),
        header=value.get("header"),
    )
    return {"id": challenge_id}


HANDLERS: dict[str, tuple[AdapterHandler, str]] = {
    "challenge.parse": (_challenge_parse, "parse_error"),
    "challenge.format": (_challenge_format, "format_error"),
    "credential.parse": (_credential_parse, "parse_error"),
    "credential.format": (_credential_format, "format_error"),
    "receipt.parse": (_receipt_parse, "parse_error"),
    "receipt.format": (_receipt_format, "format_error"),
    "base64url.encode": (_base64url_encode, "encoding_error"),
    "base64url.decode": (_base64url_decode, "encoding_error"),
    "challenge.id": (_challenge_id, "generation_error"),
}


def run_adapter_request(request: Any) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        return _error("unknown_error", "Adapter request must be an object")

    operation = request.get("op")
    if not isinstance(operation, str) or operation not in HANDLERS:
        return _error("unsupported_operation", f"Unsupported operation: {operation}")

    handler, error_type = HANDLERS[operation]
    try:
        input_value = _require_mapping(request.get("input"), label="input")
        return _success(handler(input_value))
    except Exception as exc:
        return _error(error_type, str(exc) or type(exc).__name__)


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        response = run_adapter_request(request)
    except Exception as exc:
        response = _error("unknown_error", str(exc) or type(exc).__name__)
    sys.stdout.write(json.dumps(response, separators=(",", ":"), ensure_ascii=False))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
