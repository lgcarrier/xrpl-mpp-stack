from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import re
import rfc8785
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

if TYPE_CHECKING:
    from xrpl_mpp_core.models import (
        AcceptPaymentRange,
        PaymentChallenge,
        PaymentCredential,
        PaymentReceipt,
    )
    from xrpl_mpp_core.paychannel import XRPLSessionCredentialPayload, XRPLSessionRequest
    from xrpl_mpp_core.xrpl import XRPLChargeCredentialPayload, XRPLChargeRequest


XRPL_NETWORKS = frozenset({"mainnet", "testnet", "devnet"})
PAYMENT_SCHEME = "payment"
PAYMENT_SCHEME_CANONICAL = "Payment"
AUTHORIZATION_HEADER = "Authorization"
PAYMENT_AUTHORIZATION_HEADER = "Payment-Authorization"
ACCEPT_PAYMENT_HEADER = "Accept-Payment"
PAYMENT_RECEIPT_HEADER = "Payment-Receipt"
BASE64URL_NOPAD_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
PAYMENT_RANGE_PATTERN = re.compile(
    r"^(?P<method>\*|[a-z]+)/(?P<intent>\*|[A-Za-z0-9-]+)"
    r"(?:\s*;\s*q=(?P<q>0(?:\.\d{1,3})?|1(?:\.0{1,3})?))?$"
)
MAX_ENCODED_HEADER_VALUE_LENGTH = 65_536
ModelType = TypeVar("ModelType", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class ChallengeKeyRing:
    """Ordered challenge-binding secrets; the first signs and all verify."""

    secrets: tuple[str, ...]

    def __init__(self, secrets: Sequence[str]) -> None:
        normalized = tuple(secret.strip() for secret in secrets if secret.strip())
        if not normalized:
            raise ValueError("At least one challenge-binding secret is required")
        object.__setattr__(self, "secrets", normalized)

    @property
    def active(self) -> str:
        return self.secrets[0]

    def verifies(self, challenge: "PaymentChallenge") -> bool:
        return any(
            hmac.compare_digest(
                build_challenge_id(
                    secret=secret,
                    realm=challenge.realm,
                    method=challenge.method,
                    intent=challenge.intent,
                    request_b64=challenge.request,
                    expires=challenge.expires,
                    digest=challenge.digest,
                    opaque=challenge.opaque,
                    header=challenge.header,
                ),
                challenge.id,
            )
            for secret in self.secrets
        )


def is_valid_xrpl_network(network: str) -> bool:
    """Return whether *network* uses the XRPL MPP 0.2 named-network form."""

    return network in XRPL_NETWORKS


def jcs_dumps(value: Any) -> str:
    return rfc8785.dumps(value).decode("utf-8")


def encode_json_to_base64url(value: Any) -> str:
    canonical = jcs_dumps(value).encode("utf-8")
    return base64.urlsafe_b64encode(canonical).decode("ascii").rstrip("=")


def encode_base64url_text(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("Value must be a string")
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def decode_base64url_text(raw_value: str) -> str:
    if raw_value == "":
        return ""
    if len(raw_value) > MAX_ENCODED_HEADER_VALUE_LENGTH:
        raise ValueError("Value has an invalid base64url length")
    if not BASE64URL_NOPAD_PATTERN.fullmatch(raw_value):
        raise ValueError("Value is not unpadded base64url")
    padding = "=" * (-len(raw_value) % 4)
    try:
        decoded = base64.b64decode(raw_value + padding, altchars=b"-_", validate=True)
        value = decoded.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Value is not valid base64url UTF-8") from exc
    if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != raw_value:
        raise ValueError("Value is not canonical base64url")
    return value


def decode_base64url_json(raw_value: str) -> Any:
    if not raw_value or len(raw_value) > MAX_ENCODED_HEADER_VALUE_LENGTH:
        raise ValueError("Value has an invalid base64url length")
    if not BASE64URL_NOPAD_PATTERN.fullmatch(raw_value):
        raise ValueError("Value is not unpadded base64url")
    padding = "=" * (-len(raw_value) % 4)
    try:
        decoded = base64.b64decode(raw_value + padding, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Value is not valid base64url") from exc
    if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != raw_value:
        raise ValueError("Value is not canonical base64url")
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
    for key in ("digest", "expires", "description", "header", "opaque"):
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


def payment_credential_header(challenge: "PaymentChallenge") -> str:
    return challenge.header or AUTHORIZATION_HEADER


def build_payment_authorization_value(credential: "PaymentCredential") -> str:
    return f"{PAYMENT_SCHEME_CANONICAL} {encode_payment_credential(credential)}"


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
    header: Literal["Payment-Authorization"] | None = None,
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
        header=header,
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
        header=header,
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
    header: Literal["Payment-Authorization"] | None = None,
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
    if header is not None:
        if header != PAYMENT_AUTHORIZATION_HEADER:
            raise ValueError("Unsupported payment credential header")
        slots.append(header)
    payload = "|".join(slots).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")


def verify_challenge_binding(
    challenge: "PaymentChallenge",
    *,
    secret: str | None = None,
    secrets: Sequence[str] | None = None,
) -> bool:
    configured = tuple(secrets or (() if secret is None else (secret,)))
    if not configured:
        raise ValueError("A challenge-binding secret is required")
    return ChallengeKeyRing(configured).verifies(challenge)


def parse_accept_payment(raw_value: str) -> list["AcceptPaymentRange"]:
    from xrpl_mpp_core.models import AcceptPaymentRange

    if not raw_value.strip():
        raise ValueError("Accept-Payment cannot be empty")
    ranges: list[AcceptPaymentRange] = []
    for entry in raw_value.split(","):
        match = PAYMENT_RANGE_PATTERN.fullmatch(entry.strip())
        if match is None:
            raise ValueError("Accept-Payment contains an invalid payment range")
        ranges.append(
            AcceptPaymentRange(
                method=match.group("method"),
                intent=match.group("intent"),
                q=match.group("q") or "1",
            )
        )
    return ranges


def render_accept_payment(ranges: Sequence["AcceptPaymentRange"]) -> str:
    rendered: list[str] = []
    for payment_range in ranges:
        token = f"{payment_range.method}/{payment_range.intent}"
        if payment_range.q != 1:
            q = format(payment_range.q, "f").rstrip("0").rstrip(".")
            token = f"{token};q={q or '0'}"
        rendered.append(token)
    return ", ".join(rendered)


def rank_payment_challenges(
    challenges: Sequence["PaymentChallenge"],
    ranges: Sequence["AcceptPaymentRange"],
) -> list["PaymentChallenge"]:
    ranked: list[tuple[Any, int, "PaymentChallenge"]] = []
    for server_index, challenge in enumerate(challenges):
        matching = [
            (item.specificity, range_index, item)
            for range_index, item in enumerate(ranges)
            if item.matches(method=challenge.method, intent=challenge.intent)
        ]
        if not matching:
            continue
        _, _, selected = max(matching, key=lambda item: (item[0], -item[1]))
        if selected.q == 0:
            continue
        ranked.append((selected.q, server_index, challenge))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [challenge for _, _, challenge in ranked]


def challenge_is_expired(challenge: "PaymentChallenge", *, now: datetime | None = None) -> bool:
    if not challenge.expires:
        return False
    active_now = now or datetime.now(UTC)
    normalized = challenge.expires.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized) <= active_now


def decode_challenge_request(challenge: "PaymentChallenge") -> "XRPLChargeRequest | XRPLSessionRequest":
    from xrpl_mpp_core.paychannel import XRPLSessionRequest
    from xrpl_mpp_core.xrpl import XRPLChargeRequest

    decoded = decode_base64url_json(challenge.request)
    if challenge.intent == "charge":
        return XRPLChargeRequest.model_validate(decoded)
    if challenge.intent == "session":
        return XRPLSessionRequest.model_validate(decoded)
    raise ValueError(f"Unsupported challenge intent {challenge.intent!r}")


def decode_charge_payload(credential: "PaymentCredential") -> "XRPLChargeCredentialPayload":
    from xrpl_mpp_core.xrpl import validate_charge_payload

    return validate_charge_payload(credential.payload)


def decode_session_payload(credential: "PaymentCredential") -> "XRPLSessionCredentialPayload":
    from xrpl_mpp_core.paychannel import validate_session_payload

    return validate_session_payload(credential.payload)


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
        param_value, index = _parse_auth_param_value(value, index)
        normalized_name = name.lower()
        if normalized_name in auth_params:
            raise ValueError("WWW-Authenticate Payment header contains a duplicate auth param")
        auth_params[normalized_name] = param_value
        index = _consume_header_whitespace(value, index)
        if index >= len(value):
            break
        if value[index] != ",":
            if normalized_name == "description":
                # Description is display-only and excluded from challenge binding.
                # Be tolerant of a legacy sender's unescaped quote without
                # interpreting the remaining text as security-sensitive params.
                next_scheme = _find_next_payment_scheme(value, index)
                index = len(value) if next_scheme is None else next_scheme
                break
            raise ValueError("WWW-Authenticate Payment header has malformed auth params")
        next_index = _consume_header_whitespace(value, index + 1)
        if _starts_payment_scheme(value, next_index):
            index = next_index
            break
        if _starts_other_auth_scheme(value, next_index):
            index = next_index
            break
        index = next_index

    if not auth_params:
        raise ValueError("WWW-Authenticate Payment header has no auth params")
    return auth_params, index


def _parse_auth_param_name(value: str, start: int) -> tuple[str, int]:
    if start >= len(value) or value[start] not in _AUTH_PARAM_TOKEN_CHARACTERS:
        raise ValueError("WWW-Authenticate Payment header has malformed auth params")
    index = start
    while index < len(value) and value[index] in _AUTH_PARAM_TOKEN_CHARACTERS:
        index += 1
    return value[start:index], index


def _starts_other_auth_scheme(value: str, start: int) -> bool:
    token_end = start
    while token_end < len(value) and value[token_end] in _AUTH_PARAM_TOKEN_CHARACTERS:
        token_end += 1
    if token_end == start:
        return False
    after_whitespace = _consume_header_whitespace(value, token_end)
    return after_whitespace > token_end and (
        after_whitespace >= len(value) or value[after_whitespace] != "="
    )


_AUTH_PARAM_TOKEN_CHARACTERS = frozenset(
    "!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)


def _parse_auth_param_value(value: str, start: int) -> tuple[str, int]:
    if start < len(value) and value[start] == '"':
        return _parse_quoted_string(value, start)
    index = start
    while index < len(value) and value[index] in _AUTH_PARAM_TOKEN_CHARACTERS:
        index += 1
    if index == start:
        raise ValueError(
            "WWW-Authenticate Payment header auth params must use token or quoted-string values"
        )
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
