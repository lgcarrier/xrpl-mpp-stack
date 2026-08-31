"""Explicit opt-in HTTP relay for validated MPP payment outcomes.

This module is application architecture, not an MPP wire requirement.  The
relay accepts a deliberately narrow validated-outcome type, builds a sanitized
receipt projection, and never accepts or serializes Payment credentials,
authorization fields, signed transaction blobs, session tokens, or wallet
secrets.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import ipaddress
import re
import socket
from types import MappingProxyType
from typing import Any, Literal, Protocol, TypeAlias
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


DEFAULT_RELAY_TIMEOUT_SECONDS = 5.0
MAX_RELAY_TIMEOUT_SECONDS = 30.0
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"

_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")
_FORBIDDEN_RELAY_KEYS = frozenset(
    {
        "authorization",
        "credential",
        "credentials",
        "payload",
        "paymentauthorization",
        "privatekey",
        "secret",
        "seed",
        "sessiontoken",
        "signedtransactionblob",
        "signedtxblob",
        "wallet",
        "walletsecret",
    }
)

JsonScalar: TypeAlias = str | int | float | bool | None
FrozenJson: TypeAlias = JsonScalar | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"]


class RelayError(RuntimeError):
    """Base error for the optional payment outcome relay."""


class RelayConfigurationError(RelayError, ValueError):
    """Raised for unsafe endpoints or malformed relay configuration."""


class RelayValidationError(RelayError, ValueError):
    """Raised when input is not a validated, sanitizable payment outcome."""


class RelayTimeoutError(RelayError, TimeoutError):
    """Raised when the sender exceeds the relay's bounded timeout."""


class RelayTransportError(RelayError):
    """Raised when the injected sender fails before an HTTP response."""


class RelayHTTPError(RelayError):
    """Raised for a non-success response without exposing its response body."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"payment outcome relay returned HTTP {status_code}")


class _RelayModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        str_strip_whitespace=False,
    )


class RelayReceipt(_RelayModel):
    """Allowlisted MPP core plus XRPL receipt projection."""

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
    action: str | None = None
    tx_hash: str | None = Field(default=None, alias="txHash")
    settlement_status: str | None = Field(default=None, alias="settlementStatus")

    @field_validator("method", "timestamp", "reference")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("receipt fields must be non-empty trimmed strings")
        return value

    @classmethod
    def from_payment_receipt(cls, receipt: object) -> "RelayReceipt":
        """Project a receipt-like object through an explicit safe allowlist."""

        if isinstance(receipt, cls):
            return receipt
        if isinstance(receipt, Mapping):
            raw = dict(receipt)
        else:
            dump = getattr(receipt, "model_dump", None)
            if not callable(dump):
                raise RelayValidationError("relay input must be a payment receipt")
            raw = dump(mode="json", by_alias=True, exclude_none=True)
            if not isinstance(raw, Mapping):
                raise RelayValidationError("payment receipt did not serialize to an object")

        safe_fields = {
            "status",
            "method",
            "timestamp",
            "reference",
            "challengeId",
            "network",
            "payer",
            "recipient",
            "invoiceId",
            "channelId",
            "cumulative",
            "action",
            "txHash",
            "settlementStatus",
        }
        sanitized = {key: value for key, value in raw.items() if key in safe_fields}
        try:
            return cls.model_validate(sanitized)
        except ValidationError as exc:
            raise RelayValidationError(
                "relay input is not a valid successful payment receipt"
            ) from exc


class RelayOperation(_RelayModel):
    http_method: str = Field(alias="method")
    path: str

    @field_validator("http_method")
    @classmethod
    def _validate_method(cls, value: str) -> str:
        if not value or not value.isascii() or not value.isalpha():
            raise ValueError("HTTP method must contain ASCII letters only")
        return value.upper()

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if not value.startswith("/") or any(char.isspace() for char in value):
            raise ValueError("path must be an absolute HTTP path without whitespace")
        return value


class ValidatedPaymentOutcome(_RelayModel):
    """Explicit assertion that a receipt has passed payment validation."""

    operation: RelayOperation
    receipt: RelayReceipt
    validated_at: datetime = Field(default_factory=lambda: datetime.now(UTC), alias="validatedAt")

    @field_validator("validated_at")
    @classmethod
    def _validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("validatedAt must be timezone-aware")
        return value

    @classmethod
    def from_receipt(
        cls,
        receipt: object,
        *,
        http_method: str,
        path: str,
        validated_at: datetime | None = None,
    ) -> "ValidatedPaymentOutcome":
        values: dict[str, Any] = {
            "operation": {"method": http_method, "path": path},
            "receipt": RelayReceipt.from_payment_receipt(receipt),
        }
        if validated_at is not None:
            values["validatedAt"] = validated_at
        return cls.model_validate(values)


def _freeze_json(value: Any) -> FrozenJson:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(item) for item in value)
    raise TypeError(f"unsupported relay JSON value: {type(value).__name__}")


def _thaw_json(value: FrozenJson) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class RelayRequest:
    url: str
    headers: Mapping[str, str]
    json_body: Mapping[str, FrozenJson]
    timeout_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))
        frozen = _freeze_json(self.json_body)
        if not isinstance(frozen, Mapping):
            raise TypeError("relay JSON body must be an object")
        object.__setattr__(self, "json_body", frozen)

    def mutable_json_body(self) -> dict[str, Any]:
        """Return a fresh JSON-serializable copy for an HTTP library."""

        return _thaw_json(self.json_body)


@dataclass(frozen=True, slots=True)
class RelayResponse:
    status_code: int
    headers: Mapping[str, str] = MappingProxyType({})

    def __post_init__(self) -> None:
        if isinstance(self.status_code, bool) or not 100 <= self.status_code <= 599:
            raise ValueError("relay status_code must be a valid HTTP status")
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))


class RelaySender(Protocol):
    async def __call__(self, request: RelayRequest) -> RelayResponse: ...


class HTTPXRelaySender:
    """Default no-redirect HTTP sender used only when a relay is instantiated."""

    async def __call__(self, request: RelayRequest) -> RelayResponse:
        resolved = await _resolve_safe_endpoint(request.url)
        headers = dict(request.headers)
        headers["Host"] = resolved.host_header
        async with httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                resolved.url,
                headers=headers,
                json=request.mutable_json_body(),
                timeout=request.timeout_seconds,
                extensions={"sni_hostname": resolved.sni_hostname},
            )
        return RelayResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
        )


def _is_localhost(hostname: str) -> bool:
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _literal_address(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(hostname.rstrip("."))
    except ValueError:
        return None


def _is_global_unicast(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    # ``ipaddress.is_global`` also classifies multicast as global on supported
    # Python versions; relay targets must be routable unicast hosts.
    return bool(
        address.is_global
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_reserved
    )


@dataclass(frozen=True, slots=True)
class _ResolvedRelayEndpoint:
    url: httpx.URL
    host_header: str
    sni_hostname: str


def _host_header(hostname: str, port: int | None, scheme: str) -> str:
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    if port is None or port == default_port:
        return rendered_host
    return f"{rendered_host}:{port}"


async def _resolve_safe_endpoint(endpoint: str) -> _ResolvedRelayEndpoint:
    parsed = urlsplit(endpoint)
    hostname = parsed.hostname
    if hostname is None:
        raise RelayConfigurationError("relay endpoint has no hostname")
    normalized_hostname = hostname.rstrip(".").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise RelayTransportError("relay endpoint DNS resolution failed") from exc
    resolved = {
        ipaddress.ip_address(address[4][0])
        for address in addresses
    }
    localhost_http = parsed.scheme == "http" and _is_localhost(hostname)
    if localhost_http:
        safe = bool(resolved) and all(address.is_loopback for address in resolved)
    else:
        safe = bool(resolved) and all(_is_global_unicast(address) for address in resolved)
    if not safe:
        raise RelayConfigurationError(
            "relay endpoint resolved to a disallowed network address"
        )

    # Connect to the exact address that passed policy validation. Sending the
    # hostname to HTTPX would trigger a second DNS lookup and permit a rebinding
    # answer to steer the actual socket to loopback, link-local, or RFC1918.
    selected = sorted(resolved, key=lambda address: (address.version, int(address)))[0]
    pinned_url = httpx.URL(endpoint).copy_with(host=selected.compressed)
    return _ResolvedRelayEndpoint(
        url=pinned_url,
        host_header=_host_header(normalized_hostname, parsed.port, parsed.scheme),
        sni_hostname=normalized_hostname,
    )


def _validate_endpoint(endpoint: str, *, allow_insecure_localhost: bool) -> str:
    if (
        not endpoint
        or not endpoint.isascii()
        or endpoint != endpoint.strip()
        or any(char.isspace() for char in endpoint)
    ):
        raise RelayConfigurationError("relay endpoint must be an absolute ASCII URL")
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise RelayConfigurationError("relay endpoint is malformed") from exc
    if not hostname or parsed.username is not None or parsed.password is not None:
        raise RelayConfigurationError(
            "relay endpoint must have a host and must not contain userinfo"
        )
    if parsed.fragment:
        raise RelayConfigurationError("relay endpoint must not contain a fragment")
    if parsed.scheme == "https":
        literal = _literal_address(hostname)
        if _is_localhost(hostname) or (
            literal is not None and not _is_global_unicast(literal)
        ):
            raise RelayConfigurationError(
                "relay endpoint must not target a local or private network address"
            )
        return endpoint
    if (
        parsed.scheme == "http"
        and allow_insecure_localhost
        and _is_localhost(hostname)
    ):
        return endpoint
    raise RelayConfigurationError(
        "relay endpoint must use HTTPS; HTTP localhost requires explicit test opt-in"
    )


def _validate_timeout(timeout_seconds: float) -> float:
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int | float):
        raise TypeError("relay timeout must be a number")
    normalized = float(timeout_seconds)
    if not 0 < normalized <= MAX_RELAY_TIMEOUT_SECONDS:
        raise RelayConfigurationError(
            f"relay timeout must be greater than 0 and at most "
            f"{MAX_RELAY_TIMEOUT_SECONDS:g} seconds"
        )
    return normalized


def _validate_idempotency_key(value: str) -> str:
    if not isinstance(value, str) or not _IDEMPOTENCY_KEY_PATTERN.fullmatch(value):
        raise RelayValidationError(
            "idempotency key must be 1-255 safe visible ASCII characters"
        )
    return value


def _normalized_key(value: str) -> str:
    return "".join(char for char in value.lower() if char.isalnum())


def _assert_no_sensitive_keys(value: FrozenJson) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _normalized_key(key) in _FORBIDDEN_RELAY_KEYS:
                raise RelayValidationError(
                    "relay payload contains a prohibited secret-bearing field"
                )
            _assert_no_sensitive_keys(item)
    elif isinstance(value, tuple):
        for item in value:
            _assert_no_sensitive_keys(item)


class PaymentOutcomeRelay:
    """Opt-in relay for sanitized, already-validated payment outcomes."""

    def __init__(
        self,
        *,
        endpoint: str,
        sender: RelaySender | None = None,
        timeout_seconds: float = DEFAULT_RELAY_TIMEOUT_SECONDS,
        allow_insecure_localhost: bool = False,
    ) -> None:
        self._endpoint = _validate_endpoint(
            endpoint,
            allow_insecure_localhost=allow_insecure_localhost,
        )
        self._timeout_seconds = _validate_timeout(timeout_seconds)
        self._sender: RelaySender = sender or HTTPXRelaySender()

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    def build_request(
        self,
        outcome: ValidatedPaymentOutcome,
        *,
        idempotency_key: str,
    ) -> RelayRequest:
        if not isinstance(outcome, ValidatedPaymentOutcome):
            raise RelayValidationError(
                "relay accepts only an explicit ValidatedPaymentOutcome"
            )
        normalized_key = _validate_idempotency_key(idempotency_key)
        payload = {
            "event": "payment.validated",
            "validatedAt": outcome.validated_at.isoformat(),
            "operation": outcome.operation.model_dump(mode="json", by_alias=True),
            "receipt": outcome.receipt.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            ),
        }
        frozen_payload = _freeze_json(payload)
        _assert_no_sensitive_keys(frozen_payload)
        if not isinstance(frozen_payload, Mapping):
            raise RelayValidationError("relay payload must be an object")
        return RelayRequest(
            url=self._endpoint,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                IDEMPOTENCY_KEY_HEADER: normalized_key,
            },
            json_body=frozen_payload,
            timeout_seconds=self._timeout_seconds,
        )

    async def forward(
        self,
        outcome: ValidatedPaymentOutcome,
        *,
        idempotency_key: str,
    ) -> RelayResponse:
        request = self.build_request(
            outcome,
            idempotency_key=idempotency_key,
        )
        try:
            response = await asyncio.wait_for(
                self._sender(request),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as exc:
            raise RelayTimeoutError("payment outcome relay timed out") from exc
        except asyncio.CancelledError:
            raise
        except RelayError:
            raise
        except Exception as exc:  # Sender errors are isolated behind one type.
            raise RelayTransportError("payment outcome relay sender failed") from exc

        if not isinstance(response, RelayResponse):
            raise RelayTransportError("payment outcome relay sender returned an invalid response")
        if not 200 <= response.status_code < 300:
            raise RelayHTTPError(response.status_code)
        return response


__all__ = [
    "DEFAULT_RELAY_TIMEOUT_SECONDS",
    "HTTPXRelaySender",
    "IDEMPOTENCY_KEY_HEADER",
    "MAX_RELAY_TIMEOUT_SECONDS",
    "PaymentOutcomeRelay",
    "RelayConfigurationError",
    "RelayError",
    "RelayHTTPError",
    "RelayReceipt",
    "RelayRequest",
    "RelayResponse",
    "RelaySender",
    "RelayTimeoutError",
    "RelayTransportError",
    "RelayValidationError",
    "ValidatedPaymentOutcome",
]
