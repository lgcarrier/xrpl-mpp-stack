from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from xrpl_mpp_core import StrictModel, XRPLNetwork, parse_currency


class ChargeRouteSpec(StrictModel):
    network: XRPLNetwork
    recipient: str
    currency: str
    amount: str
    description: str | None = None
    external_id: str | None = Field(default=None, alias="externalId")

    @field_validator("recipient", "currency", "amount")
    @classmethod
    def _validate_required_strings(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Value is required")
        return normalized

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, value: str) -> str:
        parse_currency(value)
        return value


class SessionRouteSpec(StrictModel):
    network: XRPLNetwork
    recipient: str
    amount: str
    currency: Literal["XRP"] = "XRP"
    channel_id: str = Field(default="", alias="channelId")
    description: str | None = None
    external_id: str | None = Field(default=None, alias="externalId")

    @field_validator("recipient", "amount")
    @classmethod
    def _validate_required_strings(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Value is required")
        return normalized

    @field_validator("channel_id")
    @classmethod
    def _validate_channel_id(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized and (len(normalized) != 64 or any(char not in "0123456789ABCDEF" for char in normalized)):
            raise ValueError("channelId must be empty or 64 hexadecimal characters")
        return normalized


class RouteConfig(StrictModel):
    facilitator_url: str = Field(alias="facilitatorUrl")
    bearer_token: str = Field(alias="bearerToken", repr=False)
    charge_options: list[ChargeRouteSpec] = Field(default_factory=list, alias="chargeOptions")
    session_options: list[SessionRouteSpec] = Field(default_factory=list, alias="sessionOptions")
    description: str | None = None
    mime_type: str = Field(default="application/json", alias="mimeType")
    realm: str | None = None
    credential_header: Literal["Authorization", "Payment-Authorization"] = Field(
        default="Authorization",
        alias="credentialHeader",
    )
    allow_insecure_facilitator_http: bool = Field(
        default=False,
        alias="allowInsecureFacilitatorHttp",
    )

    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    @field_validator("facilitator_url", "bearer_token")
    @classmethod
    def _validate_required_strings(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Value is required")
        return normalized

    @model_validator(mode="after")
    def _validate_accepts(self) -> "RouteConfig":
        if not self.charge_options and not self.session_options:
            raise ValueError("At least one charge or session option is required")
        return self
