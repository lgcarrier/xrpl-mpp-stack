from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from xrpl_mpp_core.assets import normalize_currency_code
from xrpl_mpp_core.helpers import is_valid_xrpl_network

SIGNED_TX_BLOB_MAX_LENGTH = 16_384
INVOICE_ID_MAX_LENGTH = 128
CHALLENGE_ID_MAX_LENGTH = 512
SESSION_ID_MAX_LENGTH = 256
SESSION_TOKEN_MAX_LENGTH = 512


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)


class WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, str_strip_whitespace=True)


class XRPLAsset(StrictModel):
    code: str
    issuer: str | None = None

    @field_validator("code")
    @classmethod
    def _normalize_code(cls, value: str) -> str:
        return normalize_currency_code(value)

    @field_validator("issuer")
    @classmethod
    def _normalize_issuer(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class XRPLAmount(StrictModel):
    value: str
    unit: Literal["drops", "issued"]
    drops: int | None = None

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Amount value is required")
        return normalized

    @model_validator(mode="after")
    def _validate_amount(self) -> "XRPLAmount":
        if self.unit == "drops":
            if self.drops is None:
                try:
                    self.drops = int(self.value)
                except ValueError as exc:
                    raise ValueError("Drops amount must be an integer") from exc
            if self.drops < 0:
                raise ValueError("Drops amount must be zero or greater")
            if str(self.drops) != self.value:
                raise ValueError("Drops amount must match the integer value string")
            return self

        if self.drops is not None:
            raise ValueError("Issued-asset amounts cannot set drops")
        return self


class StructuredAmount(StrictModel):
    value: str
    unit: Literal["drops", "issued"]
    asset: XRPLAsset
    drops: int | None = None

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Amount value is required")
        return normalized

    @model_validator(mode="after")
    def _validate_amount(self) -> "StructuredAmount":
        if self.unit == "drops":
            if self.drops is None:
                raise ValueError("Drops amount must include drops")
            if self.drops < 0:
                raise ValueError("Drops amount must be zero or greater")
            if str(self.drops) != self.value:
                raise ValueError("Drops amount must match the integer value string")
            return self

        if self.drops is not None:
            raise ValueError("Issued-asset amounts cannot set drops")
        return self


class MPPProblemDetails(StrictModel):
    type: str
    title: str
    status: int
    detail: str
    challenge_id: str | None = Field(default=None, alias="challengeId")


class PaymentChallenge(WireModel):
    id: str = Field(max_length=CHALLENGE_ID_MAX_LENGTH)
    realm: str
    method: str
    intent: Literal["charge", "session"]
    request: str
    digest: str | None = None
    expires: str | None = None
    description: str | None = None
    opaque: str | None = None

    @field_validator("id", "realm", "method", "request")
    @classmethod
    def _validate_required_strings(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Value is required")
        return normalized


class XRPLChargeMethodDetails(StrictModel):
    network: str
    invoice_id: str = Field(alias="invoiceId", max_length=INVOICE_ID_MAX_LENGTH)

    @field_validator("network")
    @classmethod
    def _validate_network(cls, value: str) -> str:
        normalized = value.strip()
        if not is_valid_xrpl_network(normalized):
            raise ValueError("network must be a CAIP-2 xrpl:<reference> identifier")
        return normalized


class XRPLChargeRequest(WireModel):
    amount: str
    currency: str
    recipient: str
    description: str | None = None
    external_id: str | None = Field(default=None, alias="externalId")
    method_details: XRPLChargeMethodDetails = Field(alias="methodDetails")

    @field_validator("amount", "currency", "recipient")
    @classmethod
    def _validate_required_strings(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Value is required")
        return normalized


class XRPLSessionMethodDetails(StrictModel):
    network: str
    session_id: str = Field(alias="sessionId", max_length=SESSION_ID_MAX_LENGTH)
    asset: str
    unit_amount: str = Field(alias="unitAmount")
    min_prepay_amount: str = Field(alias="minPrepayAmount")
    idle_timeout_seconds: int | None = Field(default=None, alias="idleTimeoutSeconds")
    metering_hints: dict[str, str] | None = Field(default=None, alias="meteringHints")

    @field_validator("network")
    @classmethod
    def _validate_network(cls, value: str) -> str:
        normalized = value.strip()
        if not is_valid_xrpl_network(normalized):
            raise ValueError("network must be a CAIP-2 xrpl:<reference> identifier")
        return normalized

    @field_validator("session_id", "asset", "unit_amount", "min_prepay_amount")
    @classmethod
    def _validate_required_strings(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Value is required")
        return normalized

    @field_validator("idle_timeout_seconds")
    @classmethod
    def _validate_idle_timeout(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if value <= 0:
            raise ValueError("idleTimeoutSeconds must be greater than zero")
        return value


class XRPLSessionRequest(WireModel):
    amount: str
    currency: str
    recipient: str
    description: str | None = None
    external_id: str | None = Field(default=None, alias="externalId")
    method_details: XRPLSessionMethodDetails = Field(alias="methodDetails")

    @field_validator("amount", "currency", "recipient")
    @classmethod
    def _validate_required_strings(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Value is required")
        return normalized

    @model_validator(mode="after")
    def _validate_unit_amount_matches_amount(self) -> "XRPLSessionRequest":
        if self.method_details.unit_amount != self.amount:
            raise ValueError(
                "methodDetails.unitAmount must match amount in this release; variable metering is not implemented yet"
            )
        return self


class XRPLChargeCredentialPayload(WireModel):
    signed_tx_blob: str = Field(alias="signedTxBlob", max_length=SIGNED_TX_BLOB_MAX_LENGTH)

    @field_validator("signed_tx_blob")
    @classmethod
    def _validate_blob(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("signedTxBlob is required")
        return normalized


class XRPLSessionCredentialPayload(WireModel):
    action: Literal["open", "use", "top_up", "close"]
    session_token: str | None = Field(default=None, alias="sessionToken", max_length=SESSION_TOKEN_MAX_LENGTH)
    signed_tx_blob: str | None = Field(default=None, alias="signedTxBlob", max_length=SIGNED_TX_BLOB_MAX_LENGTH)

    @model_validator(mode="after")
    def _validate_shape(self) -> "XRPLSessionCredentialPayload":
        if self.action in {"open", "top_up"}:
            if self.signed_tx_blob is None:
                raise ValueError("signedTxBlob is required for open and top_up actions")
        if self.action in {"use", "top_up", "close"}:
            if self.session_token is None:
                raise ValueError("sessionToken is required for use, top_up, and close actions")
        return self


class PaymentCredential(WireModel):
    challenge: PaymentChallenge
    payload: dict[str, Any]
    source: str | None = None


class PaymentReceipt(WireModel):
    status: Literal["success"] = "success"
    method: str
    timestamp: str
    reference: str
    challenge_id: str | None = Field(default=None, alias="challengeId")
    intent: Literal["charge", "session"] | None = None
    network: str | None = None
    payer: str | None = None
    recipient: str | None = None
    invoice_id: str | None = Field(default=None, alias="invoiceId")
    session_id: str | None = Field(default=None, alias="sessionId")
    session_token: str | None = Field(default=None, alias="sessionToken")
    tx_hash: str | None = Field(default=None, alias="txHash")
    settlement_status: Literal["submitted", "validated", "session_open", "session_active", "session_closed"] | None = Field(
        default=None,
        alias="settlementStatus",
    )
    asset: XRPLAsset | None = None
    amount: StructuredAmount | None = None
    spent_total: str | None = Field(default=None, alias="spentTotal")
    available_balance: str | None = Field(default=None, alias="availableBalance")
    prepaid_total: str | None = Field(default=None, alias="prepaidTotal")
    last_action: Literal["open", "use", "top_up", "close"] | None = Field(default=None, alias="lastAction")


class FacilitatorChargeRequest(StrictModel):
    credential: PaymentCredential


class FacilitatorSessionRequest(StrictModel):
    credential: PaymentCredential


class FacilitatorSupportedMethod(StrictModel):
    method: str
    intents: list[Literal["charge", "session"]]
    network: str
    assets: list[XRPLAsset]
    settlement_mode: Literal["optimistic", "validated"] = Field(alias="settlementMode")

    @field_validator("network")
    @classmethod
    def _validate_network(cls, value: str) -> str:
        normalized = value.strip()
        if not is_valid_xrpl_network(normalized):
            raise ValueError("network must be a CAIP-2 xrpl:<reference> identifier")
        return normalized


class FacilitatorSupportedResponse(StrictModel):
    methods: list[FacilitatorSupportedMethod]
