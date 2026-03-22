from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import hmac
import json
import re
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from xrpl_mpp_core.assets import asset_identifier_from_parts, normalize_currency_code

if TYPE_CHECKING:
    from xrpl_mpp_core.models import (
        PaymentChallenge,
        PaymentCredential,
        PaymentReceipt,
        StructuredAmount,
        XRPLAmount,
        XRPLAsset,
        XRPLChargeCredentialPayload,
        XRPLChargeRequest,
        XRPLSessionCredentialPayload,
        XRPLSessionRequest,
    )


CAIP_2_NETWORK_PATTERN = re.compile(r"^xrpl:[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
PAYMENT_SCHEME = "payment"
PAYMENT_SCHEME_CANONICAL = "Payment"
ModelType = TypeVar("ModelType", bound=BaseModel)


def is_valid_xrpl_network(network: str) -> bool:
    return bool(CAIP_2_NETWORK_PATTERN.fullmatch(network))


def canonical_asset_identifier(asset: "XRPLAsset") -> str:
    return asset_identifier_from_parts(asset.code, asset.issuer)


def build_xrpl_extra(asset: "XRPLAsset", amount: "XRPLAmount") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "asset": asset.model_dump(exclude_none=True),
        "assetId": canonical_asset_identifier(asset),
        "amount": amount.model_dump(by_alias=True, exclude_none=True),
    }
    return {"xrpl": payload}


def jcs_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def encode_json_to_base64url(value: Any) -> str:
    canonical = jcs_dumps(value).encode("utf-8")
    return base64.urlsafe_b64encode(canonical).decode("ascii").rstrip("=")


def decode_base64url_json(raw_value: str) -> Any:
    padding = "=" * (-len(raw_value) % 4)
    try:
        decoded = base64.urlsafe_b64decode(raw_value + padding)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Value is not valid base64url") from exc
    try:
        return json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Value is not valid UTF-8 JSON") from exc


def encode_model_to_base64url(model: BaseModel) -> str:
    return encode_json_to_base64url(model.model_dump(by_alias=True, exclude_none=True))


def decode_model_from_base64url(raw_value: str, model_type: type[ModelType]) -> ModelType:
    try:
        decoded_json = decode_base64url_json(raw_value)
    except ValueError as exc:
        raise ValueError("Header payload is not valid base64url JSON") from exc

    try:
        return TypeAdapter(model_type).validate_python(decoded_json)
    except ValidationError as exc:
        raise ValueError("Header payload does not match the MPP schema") from exc


def render_payment_challenge(challenge: "PaymentChallenge") -> str:
    parts = [
        f'id="{_escape_header_value(challenge.id)}"',
        f'realm="{_escape_header_value(challenge.realm)}"',
        f'method="{_escape_header_value(challenge.method)}"',
        f'intent="{_escape_header_value(challenge.intent)}"',
        f'request="{_escape_header_value(challenge.request)}"',
    ]
    for key in ("digest", "expires", "description", "opaque"):
        value = getattr(challenge, key)
        if value:
            parts.append(f'{key}="{_escape_header_value(value)}"')
    return f"{PAYMENT_SCHEME_CANONICAL} " + ", ".join(parts)


def parse_payment_challenge(raw_value: str) -> "PaymentChallenge":
    challenges = _parse_payment_challenge_values(raw_value)
    if not challenges:
        raise ValueError("WWW-Authenticate header does not use the Payment scheme")
    if len(challenges) != 1:
        raise ValueError("WWW-Authenticate header contains multiple Payment challenges")
    return challenges[0]


def extract_payment_challenges(headers: Mapping[str, str] | Any) -> list["PaymentChallenge"]:
    raw_values: list[str] = []

    if hasattr(headers, "get_list"):
        raw_values.extend(headers.get_list("WWW-Authenticate"))
    else:
        for key in ("WWW-Authenticate", "www-authenticate"):
            value = headers.get(key)
            if value:
                raw_values.append(value)

    challenges: list["PaymentChallenge"] = []
    for raw_value in raw_values:
        challenges.extend(_parse_payment_challenge_values(raw_value))
    return challenges


def encode_payment_credential(credential: "PaymentCredential") -> str:
    return encode_model_to_base64url(credential)


def decode_payment_credential(raw_value: str) -> "PaymentCredential":
    from xrpl_mpp_core.models import PaymentCredential

    return decode_model_from_base64url(raw_value, PaymentCredential)


def parse_payment_authorization_header(raw_value: str) -> "PaymentCredential":
    normalized = raw_value.strip()
    scheme, separator, token = normalized.partition(" ")
    if not separator:
        scheme, separator, token = normalized.partition("\t")
    if not separator or scheme.lower() != PAYMENT_SCHEME:
        raise ValueError("Authorization header does not use the Payment scheme")
    normalized_token = token.strip()
    if not normalized_token:
        raise ValueError("Payment authorization token is required")
    return decode_payment_credential(normalized_token)


def encode_payment_receipt(receipt: "PaymentReceipt") -> str:
    return encode_model_to_base64url(receipt)


def decode_payment_receipt(raw_value: str) -> "PaymentReceipt":
    from xrpl_mpp_core.models import PaymentReceipt

    return decode_model_from_base64url(raw_value, PaymentReceipt)


def decode_header_model(raw_value: str, model_type: type[ModelType]) -> ModelType:
    return decode_model_from_base64url(raw_value, model_type)


def build_content_digest(body: bytes | None) -> str | None:
    if body in (None, b""):
        return None
    digest = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
    return f"sha-256=:{digest}:"


def build_payment_challenge(
    *,
    secret: str,
    realm: str,
    method: str,
    intent: str,
    request_model: BaseModel,
    expires_in_seconds: int | None = None,
    description: str | None = None,
    digest: str | None = None,
    opaque: dict[str, str] | None = None,
) -> "PaymentChallenge":
    from xrpl_mpp_core.models import PaymentChallenge

    request_b64 = encode_json_to_base64url(
        request_model.model_dump(by_alias=True, exclude_none=True)
    )
    opaque_b64 = encode_json_to_base64url(opaque) if opaque else None
    expires = (
        (datetime.now(UTC) + timedelta(seconds=expires_in_seconds)).isoformat().replace("+00:00", "Z")
        if expires_in_seconds
        else None
    )
    challenge_id = build_challenge_id(
        secret=secret,
        realm=realm,
        method=method,
        intent=intent,
        request_b64=request_b64,
        expires=expires,
        digest=digest,
        opaque=opaque_b64,
    )
    return PaymentChallenge(
        id=challenge_id,
        realm=realm,
        method=method,
        intent=intent,
        request=request_b64,
        digest=digest,
        expires=expires,
        description=description,
        opaque=opaque_b64,
    )


def build_challenge_id(
    *,
    secret: str,
    realm: str,
    method: str,
    intent: str,
    request_b64: str,
    expires: str | None = None,
    digest: str | None = None,
    opaque: str | None = None,
) -> str:
    slots = [
        realm,
        method,
        intent,
        request_b64,
        expires or "",
        digest or "",
        opaque or "",
    ]
    payload = "|".join(slots).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")


def verify_challenge_binding(challenge: "PaymentChallenge", *, secret: str) -> bool:
    expected = build_challenge_id(
        secret=secret,
        realm=challenge.realm,
        method=challenge.method,
        intent=challenge.intent,
        request_b64=challenge.request,
        expires=challenge.expires,
        digest=challenge.digest,
        opaque=challenge.opaque,
    )
    return hmac.compare_digest(expected, challenge.id)


def challenge_is_expired(challenge: "PaymentChallenge", *, now: datetime | None = None) -> bool:
    if not challenge.expires:
        return False
    active_now = now or datetime.now(UTC)
    normalized = challenge.expires.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized) <= active_now


def decode_challenge_request(challenge: "PaymentChallenge") -> "XRPLChargeRequest | XRPLSessionRequest":
    from xrpl_mpp_core.models import XRPLChargeRequest, XRPLSessionRequest

    decoded = decode_base64url_json(challenge.request)
    if challenge.intent == "charge":
        return XRPLChargeRequest.model_validate(decoded)
    if challenge.intent == "session":
        return XRPLSessionRequest.model_validate(decoded)
    raise ValueError(f"Unsupported challenge intent {challenge.intent!r}")


def decode_charge_payload(credential: "PaymentCredential") -> "XRPLChargeCredentialPayload":
    from xrpl_mpp_core.models import XRPLChargeCredentialPayload

    return XRPLChargeCredentialPayload.model_validate(credential.payload)


def decode_session_payload(credential: "PaymentCredential") -> "XRPLSessionCredentialPayload":
    from xrpl_mpp_core.models import XRPLSessionCredentialPayload

    return XRPLSessionCredentialPayload.model_validate(credential.payload)


def payment_option_matches(
    requested_asset: "XRPLAsset",
    requested_amount: "XRPLAmount",
    *,
    destination: str,
    asset: "XRPLAsset",
    amount: "XRPLAmount",
    recipient: str,
) -> bool:
    if recipient != destination:
        return False
    if canonical_asset_identifier(requested_asset) != canonical_asset_identifier(asset):
        return False
    if requested_amount.unit != amount.unit:
        return False
    if requested_amount.drops != amount.drops:
        return False
    if requested_amount.unit == "issued" and Decimal(requested_amount.value) != Decimal(amount.value):
        return False
    if requested_amount.unit != "issued" and requested_amount.value != amount.value:
        return False
    return True


def amount_from_structured_amount(amount: "StructuredAmount") -> "XRPLAmount":
    from xrpl_mpp_core.models import XRPLAmount

    return XRPLAmount(value=amount.value, unit=amount.unit, drops=amount.drops)


def xrpl_asset_from_identifier(identifier: str) -> "XRPLAsset":
    from xrpl_mpp_core.models import XRPLAsset

    asset = asset_identifier_from_parts(*_parse_identifier_parts(identifier))
    code, _, issuer = asset.partition(":")
    if issuer == "native":
        return XRPLAsset(code=code)
    return XRPLAsset(code=code, issuer=issuer)


def _parse_identifier_parts(identifier: str) -> tuple[str, str | None]:
    code, separator, issuer = identifier.partition(":")
    normalized_code = normalize_currency_code(code)
    if not separator:
        raise ValueError("Asset identifier must use CODE:ISSUER or CODE:native")
    normalized_issuer = issuer.strip()
    if normalized_issuer == "native":
        return normalized_code, None
    if not normalized_issuer:
        raise ValueError("Asset identifier issuer is required")
    return normalized_code, normalized_issuer


def _escape_header_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _parse_payment_challenge_values(raw_value: str) -> list["PaymentChallenge"]:
    from xrpl_mpp_core.models import PaymentChallenge

    normalized = raw_value.strip()
    challenges: list[PaymentChallenge] = []
    index = 0
    while True:
        scheme_index = _find_next_payment_scheme(normalized, index)
        if scheme_index is None:
            break
        auth_params, index = _parse_auth_params(normalized, scheme_index + len(PAYMENT_SCHEME))
        try:
            challenges.append(PaymentChallenge.model_validate(auth_params))
        except ValidationError:
            continue
    return challenges


def _find_next_payment_scheme(value: str, start: int) -> int | None:
    lowered_value = value.lower()
    index = start
    while True:
        scheme_index = lowered_value.find(PAYMENT_SCHEME, index)
        if scheme_index == -1:
            return None
        if _starts_payment_scheme(value, scheme_index):
            return scheme_index
        index = scheme_index + len(PAYMENT_SCHEME)


def _starts_payment_scheme(value: str, index: int) -> bool:
    if value[index : index + len(PAYMENT_SCHEME)].lower() != PAYMENT_SCHEME:
        return False
    scheme_end = index + len(PAYMENT_SCHEME)
    if scheme_end >= len(value) or value[scheme_end] not in {" ", "\t"}:
        return False

    prefix_index = index - 1
    while prefix_index >= 0 and value[prefix_index] in {" ", "\t"}:
        prefix_index -= 1
    return prefix_index < 0 or value[prefix_index] == ","


def _parse_auth_params(value: str, start: int) -> tuple[dict[str, str], int]:
    index = _consume_header_whitespace(value, start)
    auth_params: dict[str, str] = {}

    while index < len(value):
        name, index = _parse_auth_param_name(value, index)
        index = _consume_header_whitespace(value, index)
        if index >= len(value) or value[index] != "=":
            raise ValueError("WWW-Authenticate Payment header has malformed auth params")
        index += 1
        index = _consume_header_whitespace(value, index)
        param_value, index = _parse_quoted_string(value, index)
        auth_params[name] = param_value
        index = _consume_header_whitespace(value, index)
        if index >= len(value):
            break
        if value[index] != ",":
            raise ValueError("WWW-Authenticate Payment header has malformed auth params")
        next_index = _consume_header_whitespace(value, index + 1)
        if _starts_payment_scheme(value, next_index):
            index = next_index
            break
        index = next_index

    if not auth_params:
        raise ValueError("WWW-Authenticate Payment header has no auth params")
    return auth_params, index


def _parse_auth_param_name(value: str, start: int) -> tuple[str, int]:
    if start >= len(value) or not value[start].isalpha():
        raise ValueError("WWW-Authenticate Payment header has malformed auth params")
    index = start + 1
    while index < len(value) and (value[index].isalnum() or value[index] in {"_", "-"}):
        index += 1
    return value[start:index], index


def _parse_quoted_string(value: str, start: int) -> tuple[str, int]:
    if start >= len(value) or value[start] != '"':
        raise ValueError("WWW-Authenticate Payment header auth params must be quoted")

    index = start + 1
    buffer: list[str] = []
    while index < len(value):
        char = value[index]
        if char == "\\":
            index += 1
            if index >= len(value):
                raise ValueError("WWW-Authenticate Payment header has an unterminated escape")
            buffer.append(value[index])
            index += 1
            continue
        if char == '"':
            return "".join(buffer), index + 1
        buffer.append(char)
        index += 1
    raise ValueError("WWW-Authenticate Payment header has an unterminated quoted string")


def _consume_header_whitespace(value: str, start: int) -> int:
    index = start
    while index < len(value) and value[index] in {" ", "\t"}:
        index += 1
    return index
