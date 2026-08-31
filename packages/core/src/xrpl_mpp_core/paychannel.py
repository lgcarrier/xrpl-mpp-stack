from __future__ import annotations

from time import time
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import Field, TypeAdapter, field_validator, model_validator

from xrpl_mpp_core.xrpl import (
    ClassicAddress,
    MAX_TRANSACTION_BLOB_LENGTH,
    TRANSACTION_BLOB_PATTERN,
    XRPLModel,
    XRPLNetwork,
)


MAX_DROPS_DIGITS = 20
MAX_CLAIM_SIGNATURE_LENGTH = 144
CHANNEL_ID_PATTERN = r"^[0-9A-Fa-f]{64}$"
DROPS_PATTERN = r"^[0-9]+$"
SIGNATURE_PATTERN = r"^[0-9A-Fa-f]+$"

Drops: TypeAlias = Annotated[
    str,
    Field(min_length=1, max_length=MAX_DROPS_DIGITS, pattern=DROPS_PATTERN),
]
ChannelId: TypeAlias = Annotated[str, Field(pattern=CHANNEL_ID_PATTERN)]
ClaimSignature: TypeAlias = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_CLAIM_SIGNATURE_LENGTH,
        pattern=SIGNATURE_PATTERN,
    ),
]


class XRPLSessionMethodDetails(XRPLModel):
    """XRPL details attached to a canonical MPP session challenge."""

    reference: str | None = None
    network: XRPLNetwork | None = None
    cumulative_amount: Drops | None = Field(default=None, alias="cumulativeAmount")


class XRPLSessionRequest(XRPLModel):
    """Incremental fixed-cost PayChannel request; cumulative is in method details."""

    amount: Drops
    currency: Literal["XRP"] | None = None
    channel_id: str = Field(alias="channelId")
    recipient: ClassicAddress
    description: str | None = None
    external_id: str | None = Field(default=None, alias="externalId")
    method_details: XRPLSessionMethodDetails | None = Field(
        default=None,
        alias="methodDetails",
    )

    @field_validator("channel_id")
    @classmethod
    def _validate_channel_id(cls, value: str) -> str:
        if value == "":
            return value
        TypeAdapter(ChannelId).validate_python(value)
        return value


class XRPLChannelOpenPayload(XRPLModel):
    """Open action containing a payer-signed ``PaymentChannelCreate`` blob."""

    action: Literal["open"]
    transaction: str = Field(
        min_length=1,
        max_length=MAX_TRANSACTION_BLOB_LENGTH,
        pattern=TRANSACTION_BLOB_PATTERN,
    )
    amount: Drops
    signature: ClaimSignature


class XRPLChannelVoucherPayload(XRPLModel):
    """Cumulative off-ledger PayChannel voucher."""

    action: Literal["voucher"]
    channel_id: ChannelId = Field(alias="channelId")
    amount: Drops
    signature: ClaimSignature


class XRPLChannelClosePayload(XRPLModel):
    """Final cumulative voucher; on-ledger close remains a separate XRPL action."""

    action: Literal["close"]
    channel_id: ChannelId = Field(alias="channelId")
    amount: Drops
    signature: ClaimSignature


XRPLSessionCredentialPayload: TypeAlias = Annotated[
    XRPLChannelOpenPayload | XRPLChannelVoucherPayload | XRPLChannelClosePayload,
    Field(discriminator="action"),
]

_SESSION_PAYLOAD_ADAPTER = TypeAdapter(XRPLSessionCredentialPayload)
_DROPS_ADAPTER = TypeAdapter(Drops)


class PayChannelHighWater(XRPLModel):
    """Highest cumulative voucher durably accepted for one channel."""

    cumulative: Drops
    signature: str = Field(
        max_length=MAX_CLAIM_SIGNATURE_LENGTH,
        pattern=r"^[0-9A-Fa-f]*$",
    )
    timestamp: int = Field(ge=0)

    @model_validator(mode="after")
    def _require_signature_for_value(self) -> "PayChannelHighWater":
        if int(self.cumulative) > 0 and not self.signature:
            raise ValueError("a non-zero cumulative amount requires a claim signature")
        return self


class HighWaterDecision(XRPLModel):
    """Pure compare-and-set decision for a durable store callback."""

    status: Literal["advanced", "replay", "regressed", "short"]
    previous: Drops
    state: PayChannelHighWater | None = None

    @model_validator(mode="after")
    def _validate_state(self) -> "HighWaterDecision":
        if self.status == "advanced" and self.state is None:
            raise ValueError("an advanced decision requires the new high-water state")
        if self.status != "advanced" and self.state is not None:
            raise ValueError("a rejected decision cannot mutate high-water state")
        return self


class PayChannelCumulativeError(ValueError):
    """Raised when a voucher cannot advance the cumulative high-water mark."""

    def __init__(self, decision: HighWaterDecision) -> None:
        self.status = decision.status
        self.previous = decision.previous
        super().__init__(
            f"PayChannel cumulative amount was {decision.status}; previous={decision.previous}"
        )


def _drops_string(value: str | int, *, name: str) -> str:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be an unsigned integer")
    rendered = str(value)
    try:
        return _DROPS_ADAPTER.validate_python(rendered)
    except Exception as exc:
        raise ValueError(f"{name} must be an unsigned drops string") from exc


def evaluate_high_water(
    current: PayChannelHighWater | None,
    *,
    cumulative: str | int,
    requested: str | int,
    signature: str,
    timestamp: int | None = None,
) -> HighWaterDecision:
    """Evaluate Ripple's atomic cumulative-voucher transition without I/O.

    Call this inside a backing store's compare-and-set callback. Network and
    signature verification must happen before the callback, because a CAS
    implementation may replay it.
    """

    cumulative_str = _drops_string(cumulative, name="cumulative")
    requested_str = _drops_string(requested, name="requested")
    new_value = int(cumulative_str)
    requested_value = int(requested_str)
    previous_value = int(current.cumulative) if current is not None else 0
    previous_str = str(previous_value)

    if new_value == previous_value:
        return HighWaterDecision(status="replay", previous=previous_str)
    if new_value < previous_value:
        return HighWaterDecision(status="regressed", previous=previous_str)
    if requested_value > 0 and new_value < previous_value + requested_value:
        return HighWaterDecision(status="short", previous=previous_str)

    state = PayChannelHighWater(
        cumulative=cumulative_str,
        signature=signature,
        timestamp=timestamp if timestamp is not None else int(time() * 1_000),
    )
    return HighWaterDecision(
        status="advanced",
        previous=previous_str,
        state=state,
    )


def require_high_water_advance(
    current: PayChannelHighWater | None,
    *,
    cumulative: str | int,
    requested: str | int,
    signature: str,
    timestamp: int | None = None,
) -> PayChannelHighWater:
    """Return the new state or raise a typed cumulative validation error."""

    decision = evaluate_high_water(
        current,
        cumulative=cumulative,
        requested=requested,
        signature=signature,
        timestamp=timestamp,
    )
    if decision.status != "advanced" or decision.state is None:
        raise PayChannelCumulativeError(decision)
    return decision.state


def validate_session_payload(value: Any) -> XRPLSessionCredentialPayload:
    """Validate and discriminate an XRPL session credential payload."""

    return _SESSION_PAYLOAD_ADAPTER.validate_python(value)


# Name used by the TypeScript reference's store helper.
advance_high_water = evaluate_high_water
