from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


_METHOD_PATTERN = re.compile(r"^[a-z]+$")
_INTENT_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")


def _require_non_empty(value: str, *, name: str) -> str:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value


def _validate_rfc3339(value: str, *, name: str) -> str:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{name} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return value


class MCPWireModel(BaseModel):
    """Forward-compatible JSON wire model.

    Extra fields are retained so a credential can echo a challenge unchanged.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class MCPPaymentChallenge(MCPWireModel):
    id: str = Field(max_length=512)
    realm: str
    method: str
    intent: str
    request: dict[str, Any]
    expires: str | None = None
    description: str | None = None

    @field_validator("id", "realm")
    @classmethod
    def _validate_non_empty(cls, value: str, info: Any) -> str:
        return _require_non_empty(value, name=info.field_name)

    @field_validator("method")
    @classmethod
    def _validate_method(cls, value: str) -> str:
        if not _METHOD_PATTERN.fullmatch(value):
            raise ValueError("method must contain lowercase ASCII letters only")
        return value

    @field_validator("intent")
    @classmethod
    def _validate_intent(cls, value: str) -> str:
        if not _INTENT_PATTERN.fullmatch(value):
            raise ValueError("intent must contain only letters, digits, and hyphens")
        return value

    @field_validator("expires")
    @classmethod
    def _validate_expires(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_rfc3339(value, name="expires")


class MCPPaymentCredential(MCPWireModel):
    challenge: MCPPaymentChallenge
    payload: dict[str, Any]
    source: str | None = None

    @field_validator("source")
    @classmethod
    def _validate_source(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _require_non_empty(value, name="source")


class MCPPaymentReceipt(MCPWireModel):
    status: Literal["success"] = "success"
    method: str
    timestamp: str
    challenge_id: str = Field(alias="challengeId")
    reference: str | None = None

    @field_validator("method", "challenge_id")
    @classmethod
    def _validate_non_empty(cls, value: str, info: Any) -> str:
        return _require_non_empty(value, name=info.field_name)

    @field_validator("timestamp")
    @classmethod
    def _validate_timestamp(cls, value: str) -> str:
        return _validate_rfc3339(value, name="timestamp")


class PaymentMethodCapability(MCPWireModel):
    intents: list[str] = Field(min_length=1)

    @field_validator("intents")
    @classmethod
    def _validate_intents(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("intents must not contain duplicates")
        for value in values:
            if not _INTENT_PATTERN.fullmatch(value):
                raise ValueError("intent must contain only letters, digits, and hyphens")
        return values


class PaymentCapabilities(MCPWireModel):
    methods: dict[str, PaymentMethodCapability] = Field(min_length=1)

    @field_validator("methods")
    @classmethod
    def _validate_methods(
        cls,
        values: dict[str, PaymentMethodCapability],
    ) -> dict[str, PaymentMethodCapability]:
        for method in values:
            if not _METHOD_PATTERN.fullmatch(method):
                raise ValueError("payment method keys must contain lowercase ASCII letters only")
        return values


class MCPProblemDetails(MCPWireModel):
    type: str
    title: str
    status: int
    detail: str
    challenge_id: str | None = Field(default=None, alias="challengeId")


class MCPPaymentFailure(MCPWireModel):
    reason: str | None = None
    detail: str | None = None


__all__ = [
    "MCPPaymentChallenge",
    "MCPPaymentCredential",
    "MCPPaymentFailure",
    "MCPPaymentReceipt",
    "MCPProblemDetails",
    "MCPWireModel",
    "PaymentCapabilities",
    "PaymentMethodCapability",
]
