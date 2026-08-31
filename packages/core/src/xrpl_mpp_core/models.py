from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from xrpl_mpp_core.helpers import (
    decode_base64url_json,
    encode_json_to_base64url,
)

CHALLENGE_ID_MAX_LENGTH = 512
HEADER_PAYLOAD_MAX_LENGTH = 65_536
REQUEST_PARAMETER_MAX_LENGTH = 16_384
PAYMENT_AUTHORIZATION_HEADER = "Payment-Authorization"
BASE64URL_NOPAD_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
METHOD_PATTERN = re.compile(r"^[a-z]+$")
INTENT_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)


class WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, str_strip_whitespace=True)


class MPPProblemDetails(WireModel):
    type: str
    title: str
    status: int
    detail: str
    challenge_id: str | None = Field(default=None, alias="challengeId")
    payment_reference: str | None = Field(default=None, alias="paymentReference")


class PaymentChallenge(WireModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, str_strip_whitespace=False)

    id: str = Field(max_length=CHALLENGE_ID_MAX_LENGTH)
    realm: str
    method: str
    intent: str
    request: str = Field(max_length=REQUEST_PARAMETER_MAX_LENGTH)
    digest: str | None = None
    expires: str | None = None
    description: str | None = None
    header: Literal["Payment-Authorization"] | None = None
    opaque: str | None = None

    @field_validator("id", "realm", "method", "request")
    @classmethod
    def _validate_required_strings(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Value is required")
        # Authentication values are echoed and HMAC-bound byte-for-byte after
        # RFC quoted-string unescaping. Never normalize legal whitespace here.
        return value

    @field_validator("method")
    @classmethod
    def _validate_method(cls, value: str) -> str:
        if not METHOD_PATTERN.fullmatch(value):
            raise ValueError("method must contain lowercase ASCII letters only")
        return value

    @field_validator("intent")
    @classmethod
    def _validate_intent(cls, value: str) -> str:
        if not INTENT_PATTERN.fullmatch(value):
            raise ValueError("intent must be an HTTP payment intent token")
        return value

    @field_validator("request", "opaque")
    @classmethod
    def _validate_base64url_nopad(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not BASE64URL_NOPAD_PATTERN.fullmatch(value):
            raise ValueError("value must be non-empty base64url without padding")
        return value

    @field_validator("request")
    @classmethod
    def _validate_request_json(cls, value: str) -> str:
        decoded = decode_base64url_json(value)
        if encode_json_to_base64url(decoded) != value:
            raise ValueError("request must contain JCS-canonical JSON")
        return value

    @field_validator("opaque")
    @classmethod
    def _validate_opaque_json(cls, value: str | None) -> str | None:
        if value is None:
            return None
        decoded = decode_base64url_json(value)
        if (
            not isinstance(decoded, dict)
            or any(not isinstance(key, str) or not isinstance(item, str) for key, item in decoded.items())
            or encode_json_to_base64url(decoded) != value
        ):
            raise ValueError("opaque must contain a JCS-canonical flat string map")
        return value

    @field_validator("expires")
    @classmethod
    def _validate_expires(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("expires must be an RFC 3339 date-time") from exc
        if parsed.tzinfo is None:
            raise ValueError("expires must include a time-zone offset")
        return value


class AcceptPaymentRange(StrictModel):
    method: str
    intent: str
    q: Decimal = Decimal("1")

    @field_validator("method")
    @classmethod
    def _validate_method(cls, value: str) -> str:
        if value != "*" and not METHOD_PATTERN.fullmatch(value):
            raise ValueError("method must be '*' or lowercase ASCII letters")
        return value

    @field_validator("intent")
    @classmethod
    def _validate_intent(cls, value: str) -> str:
        if value != "*" and not INTENT_PATTERN.fullmatch(value):
            raise ValueError("intent must be '*' or an HTTP payment intent token")
        return value

    @field_validator("q")
    @classmethod
    def _validate_q(cls, value: Decimal) -> Decimal:
        if value < 0 or value > 1:
            raise ValueError("q must be between 0 and 1")
        if abs(value.as_tuple().exponent) > 3:
            raise ValueError("q must have no more than three decimal places")
        return value

    def matches(self, *, method: str, intent: str) -> bool:
        return self.method in {"*", method} and self.intent in {"*", intent}

    @property
    def specificity(self) -> int:
        return int(self.method != "*") + int(self.intent != "*")


class PaymentCredential(WireModel):
    challenge: PaymentChallenge
    payload: dict[str, Any]
    source: str | None = None


class PaymentReceipt(WireModel):
    # Payment methods and intents are explicitly allowed to extend receipts.
    # Preserve those fields so generic clients can forward them losslessly.
    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)

    status: Literal["success"]
    method: str
    timestamp: str
    reference: str
    challenge_id: str | None = Field(default=None, alias="challengeId")
    network: str | None = None
    payer: str | None = None
    recipient: str | None = None
    invoice_id: str | None = Field(default=None, alias="invoiceId")
    channel_id: str | None = Field(default=None, alias="channelId")
    cumulative: str | None = None
    action: Literal["open", "voucher", "close"] | None = None
    tx_hash: str | None = Field(default=None, alias="txHash")
    settlement_status: Literal["validated"] | None = Field(
        default=None,
        alias="settlementStatus",
    )

    @field_validator("timestamp")
    @classmethod
    def _validate_timestamp(cls, value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("timestamp must be an RFC 3339 date-time") from exc
        if parsed.tzinfo is None:
            raise ValueError("timestamp must include a time-zone offset")
        return value


class FacilitatorChargeRequest(StrictModel):
    credential: PaymentCredential


class FacilitatorSessionRequest(StrictModel):
    credential: PaymentCredential


class FacilitatorSupportedMethod(StrictModel):
    method: str
    intents: list[str]
    network: Literal["mainnet", "testnet", "devnet"]
    currencies: list[str]
    settlement_mode: Literal["validated"] = Field(alias="settlementMode")


class FacilitatorSupportedResponse(StrictModel):
    methods: list[FacilitatorSupportedMethod]
