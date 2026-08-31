from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator


XRPLNetwork: TypeAlias = Literal["mainnet", "testnet", "devnet"]
XRP: Literal["XRP"] = "XRP"

MAX_AMOUNT_LENGTH = 32
MAX_TRANSACTION_BLOB_LENGTH = 8_192
MAX_MEMOS = 32
MAX_MEMO_FIELD_LENGTH = 1_024
MAX_CURRENCY_LENGTH = 512

CLASSIC_ADDRESS_PATTERN = r"^r[1-9A-HJ-NP-Za-km-z]{24,34}$"
HEX_64_PATTERN = r"^[0-9A-Fa-f]{64}$"
CHARGE_AMOUNT_PATTERN = r"^[0-9]+(?:\.[0-9]+)?$"
TRANSACTION_BLOB_PATTERN = r"^[0-9A-Fa-f]+$"

ClassicAddress: TypeAlias = Annotated[
    str,
    Field(pattern=CLASSIC_ADDRESS_PATTERN),
]
ChargeAmount: TypeAlias = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_AMOUNT_LENGTH,
        pattern=CHARGE_AMOUNT_PATTERN,
    ),
]
UInt32: TypeAlias = Annotated[int, Field(ge=0, le=0xFFFFFFFF)]


class XRPLModel(BaseModel):
    """Strict base model for the XRPL 0.2 wire contract."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        str_strip_whitespace=True,
        strict=True,
    )


class IssuedCurrency(XRPLModel):
    """XRPL issued currency descriptor used inside the MPP currency string."""

    currency: str = Field(min_length=1, max_length=40)
    issuer: ClassicAddress


class MPToken(XRPLModel):
    """XRPL Multi-Purpose Token descriptor used inside the MPP currency string."""

    mpt_issuance_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[0-9A-Fa-f]+$",
    )


XRPLCurrency: TypeAlias = Literal["XRP"] | IssuedCurrency | MPToken
XRPLLedgerAmount: TypeAlias = str | dict[str, str]


class XRPLMemo(XRPLModel):
    """UTF-8 memo fields requested by a seller before XRPL hex encoding."""

    type: str | None = Field(default=None, max_length=MAX_MEMO_FIELD_LENGTH)
    format: str | None = Field(default=None, max_length=MAX_MEMO_FIELD_LENGTH)
    data: str | None = Field(default=None, max_length=MAX_MEMO_FIELD_LENGTH)


class XRPLChargeMethodDetails(XRPLModel):
    """XRPL-specific challenge terms for a one-time charge."""

    reference: str | None = None
    network: XRPLNetwork | None = None
    invoice_id: str | None = Field(
        default=None,
        alias="invoiceId",
        pattern=HEX_64_PATTERN,
    )
    destination_tag: UInt32 | None = Field(default=None, alias="destinationTag")
    source_tag: UInt32 | None = Field(default=None, alias="sourceTag")
    memos: list[XRPLMemo] | None = Field(default=None, max_length=MAX_MEMOS)


class XRPLChargeRequest(XRPLModel):
    """Request object carried by an ``xrpl`` / ``charge`` challenge."""

    amount: ChargeAmount
    currency: str = Field(min_length=1, max_length=MAX_CURRENCY_LENGTH)
    recipient: ClassicAddress
    description: str | None = None
    external_id: str | None = Field(default=None, alias="externalId")
    method_details: XRPLChargeMethodDetails | None = Field(
        default=None,
        alias="methodDetails",
    )

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, value: str) -> str:
        parse_currency(value)
        return value


class XRPLTransactionCredentialPayload(XRPLModel):
    """Pull mode: the payer signs and the server submits the transaction."""

    type: Literal["transaction"]
    blob: str = Field(
        min_length=1,
        max_length=MAX_TRANSACTION_BLOB_LENGTH,
        pattern=TRANSACTION_BLOB_PATTERN,
    )


class XRPLHashCredentialPayload(XRPLModel):
    """Push mode: the payer submits and presents the transaction hash."""

    type: Literal["hash"]
    hash: str = Field(pattern=HEX_64_PATTERN)


XRPLChargeCredentialPayload: TypeAlias = Annotated[
    XRPLTransactionCredentialPayload | XRPLHashCredentialPayload,
    Field(discriminator="type"),
]

_CHARGE_PAYLOAD_ADAPTER = TypeAdapter(XRPLChargeCredentialPayload)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    for key, value in pairs:
        if key in decoded:
            raise ValueError(f"Duplicate currency field: {key}")
        decoded[key] = value
    return decoded


def parse_currency(value: str) -> XRPLCurrency:
    """Parse the canonical Ripple XRPL MPP currency string.

    ``XRP`` is represented directly. Issued currencies and MPTs are represented
    as compact JSON objects. The pre-0.2 ``CODE:issuer`` shorthand is
    deliberately not accepted.
    """

    if not isinstance(value, str):
        raise TypeError("XRPL currency must be a string")
    if value == XRP:
        return XRP

    try:
        decoded = json.loads(value, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("XRPL currency must be XRP or a JSON currency descriptor") from exc

    if not isinstance(decoded, dict):
        raise ValueError("XRPL currency JSON must be an object")
    if set(decoded) == {"currency", "issuer"}:
        return IssuedCurrency.model_validate(decoded)
    if set(decoded) == {"mpt_issuance_id"}:
        return MPToken.model_validate(decoded)
    raise ValueError(
        "XRPL currency JSON must contain exactly currency+issuer or mpt_issuance_id"
    )


def serialize_currency(currency: XRPLCurrency | Mapping[str, Any]) -> str:
    """Serialize an XRPL currency using Ripple's compact deterministic shape."""

    if currency == XRP:
        return XRP

    parsed: IssuedCurrency | MPToken
    if isinstance(currency, IssuedCurrency | MPToken):
        parsed = currency
    elif isinstance(currency, Mapping):
        if set(currency) == {"currency", "issuer"}:
            parsed = IssuedCurrency.model_validate(dict(currency))
        elif set(currency) == {"mpt_issuance_id"}:
            parsed = MPToken.model_validate(dict(currency))
        else:
            raise ValueError(
                "XRPL currency mapping must contain exactly currency+issuer or mpt_issuance_id"
            )
    else:
        raise TypeError("Unsupported XRPL currency value")

    return json.dumps(
        parsed.model_dump(),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def build_ledger_amount(amount: str, currency: XRPLCurrency) -> XRPLLedgerAmount:
    """Build the XRPL ``Payment.Amount`` value for the challenge terms."""

    validated_amount = TypeAdapter(ChargeAmount).validate_python(amount)
    if currency == XRP:
        if "." in validated_amount:
            raise ValueError("XRP charge amounts are integer drops")
        return validated_amount
    if isinstance(currency, IssuedCurrency):
        return {
            "currency": currency.currency,
            "issuer": currency.issuer,
            "value": validated_amount,
        }
    if isinstance(currency, MPToken):
        return {
            "mpt_issuance_id": currency.mpt_issuance_id,
            "value": validated_amount,
        }
    raise TypeError("Unsupported XRPL currency value")


def validate_charge_payload(value: Any) -> XRPLChargeCredentialPayload:
    """Validate and discriminate a charge credential payload."""

    return _CHARGE_PAYLOAD_ADAPTER.validate_python(value)


# Descriptive aliases for callers that prefer the full XRPL prefix.
parse_xrpl_currency = parse_currency
serialize_xrpl_currency = serialize_currency
