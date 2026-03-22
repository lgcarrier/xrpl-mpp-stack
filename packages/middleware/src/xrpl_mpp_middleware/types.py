from __future__ import annotations

from pydantic import ConfigDict, Field, field_validator, model_validator

from xrpl_mpp_core import StrictModel, canonical_asset_identifier, xrpl_asset_from_identifier


class ChargeRouteSpec(StrictModel):
    network: str
    recipient: str
    asset_identifier: str = Field(alias="assetIdentifier")
    amount: str
    description: str | None = None
    external_id: str | None = Field(default=None, alias="externalId")

    @field_validator("network", "recipient", "asset_identifier", "amount")
    @classmethod
    def _validate_required_strings(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Value is required")
        return normalized

    @field_validator("asset_identifier")
    @classmethod
    def _normalize_asset_identifier(cls, value: str) -> str:
        return canonical_asset_identifier(xrpl_asset_from_identifier(value))


class SessionRouteSpec(StrictModel):
    network: str
    recipient: str
    asset_identifier: str = Field(alias="assetIdentifier")
    amount: str
    min_prepay_amount: str = Field(alias="minPrepayAmount")
    unit_amount: str | None = Field(default=None, alias="unitAmount")
    description: str | None = None
    external_id: str | None = Field(default=None, alias="externalId")
    idle_timeout_seconds: int | None = Field(default=None, alias="idleTimeoutSeconds")
    metering_hints: dict[str, str] | None = Field(default=None, alias="meteringHints")

    @field_validator("network", "recipient", "asset_identifier", "amount", "min_prepay_amount")
    @classmethod
    def _validate_required_strings(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Value is required")
        return normalized

    @field_validator("asset_identifier")
    @classmethod
    def _normalize_asset_identifier(cls, value: str) -> str:
        return canonical_asset_identifier(xrpl_asset_from_identifier(value))

    @model_validator(mode="after")
    def _sync_unit_amount(self) -> "SessionRouteSpec":
        if self.unit_amount is None:
            self.unit_amount = self.amount
        elif self.unit_amount != self.amount:
            raise ValueError(
                "unitAmount must match amount in this release; variable metering is not implemented yet"
            )
        return self


class RouteConfig(StrictModel):
    facilitator_url: str = Field(alias="facilitatorUrl")
    bearer_token: str = Field(alias="bearerToken", repr=False)
    charge_options: list[ChargeRouteSpec] = Field(default_factory=list, alias="chargeOptions")
    session_options: list[SessionRouteSpec] = Field(default_factory=list, alias="sessionOptions")
    description: str | None = None
    mime_type: str = Field(default="application/json", alias="mimeType")
    realm: str | None = None

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
