"""OpenAPI 3.x discovery helpers for MPP-protected HTTP operations.

The discovery document is advisory.  A runtime ``402`` Payment challenge is
always authoritative, so the helpers in this module derive payment metadata
from the same route configuration used by the middleware and replace stale
payment extensions when augmenting an OpenAPI document.

The module deliberately has no FastAPI dependency.  ``augment_openapi`` is a
pure transformation over JSON-compatible mappings and can therefore be wired
to any framework's OpenAPI generation hook.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
import re
from typing import Any, Literal, Protocol, TypeAlias, runtime_checkable
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


DiscoveryIntent: TypeAlias = Literal["charge", "session"]
RouteKey: TypeAlias = str | tuple[str, str]

_AMOUNT_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)$")
_METHOD_PATTERN = re.compile(r"^[a-z]+$")
_URI_SCHEME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*$")
_URI_CHARACTERS_PATTERN = re.compile(
    r"^[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+$"
)
_BAD_PERCENT_ESCAPE_PATTERN = re.compile(r"%(?![0-9A-Fa-f]{2})")
_OPENAPI_3_PATTERN = re.compile(r"^3\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_HTTP_OPERATION_METHODS = frozenset(
    {"delete", "get", "head", "options", "patch", "post", "put", "trace"}
)
_MISSING = object()


class DiscoveryError(ValueError):
    """Base error for invalid discovery configuration or documents."""


class DiscoveryConfigurationError(DiscoveryError):
    """Raised when payment discovery metadata is internally inconsistent."""


class DiscoveryDocumentError(DiscoveryError):
    """Raised when an OpenAPI document cannot be augmented safely."""


class _DiscoveryModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        str_strip_whitespace=False,
    )


def _normalize_path(value: str) -> str:
    if not value.startswith("/") or any(character.isspace() for character in value):
        raise ValueError("path must be an absolute HTTP path without whitespace")
    return value


def _normalize_http_method(value: str) -> str:
    normalized = value.lower()
    if normalized not in _HTTP_OPERATION_METHODS:
        raise ValueError(f"unsupported OpenAPI operation method: {value}")
    return normalized


class PaymentOffer(_DiscoveryModel):
    """A draft-payment-discovery-01 payment offer.

    Only the intents standardized by draft-01 are accepted.  In particular,
    ``subscription`` is intentionally rejected even though it is a separate
    MPP intent draft, because the current discovery schema does not include it.
    """

    intent: DiscoveryIntent
    method: str
    amount: str | None
    currency: str | None = None
    description: str | None = None

    @field_validator("method")
    @classmethod
    def _validate_method(cls, value: str) -> str:
        if not _METHOD_PATTERN.fullmatch(value):
            raise ValueError("method must contain lowercase ASCII letters only")
        return value

    @field_validator("amount")
    @classmethod
    def _validate_amount(cls, value: str | None) -> str | None:
        if value is not None and not _AMOUNT_PATTERN.fullmatch(value):
            raise ValueError(
                "amount must be null or a canonical non-negative integer string"
            )
        return value


class ServiceDocs(_DiscoveryModel):
    """Documentation URIs published in ``x-service-info``."""

    api_reference: str | None = Field(default=None, alias="apiReference")
    homepage: str | None = None
    llms: str | None = None

    @field_validator("api_reference", "homepage", "llms")
    @classmethod
    def _validate_uri(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value or not value.isascii() or not _URI_CHARACTERS_PATTERN.fullmatch(value):
            raise ValueError("documentation links must be absolute RFC 3986 URIs")
        if _BAD_PERCENT_ESCAPE_PATTERN.search(value):
            raise ValueError("documentation links contain an invalid percent escape")

        try:
            parsed = urlsplit(value)
            # Accessing port makes urllib reject malformed bracket/port syntax.
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("documentation links must be valid RFC 3986 URIs") from exc

        if not parsed.scheme or not _URI_SCHEME_PATTERN.fullmatch(parsed.scheme):
            raise ValueError("documentation links must be absolute RFC 3986 URIs")
        if value.startswith(f"{parsed.scheme}://") and not parsed.netloc:
            raise ValueError("documentation URI authorities must not be empty")
        return value


class ServiceInfo(_DiscoveryModel):
    """Top-level ``x-service-info`` metadata."""

    categories: tuple[str, ...] | None = None
    docs: ServiceDocs | None = None


class PayableRoute(_DiscoveryModel):
    """Payment offers associated with one concrete OpenAPI operation."""

    path: str
    method: str
    offers: tuple[PaymentOffer, ...]

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return _normalize_path(value)

    @field_validator("method")
    @classmethod
    def _validate_http_method(cls, value: str) -> str:
        return _normalize_http_method(value)

    @field_validator("offers")
    @classmethod
    def _validate_offers(
        cls,
        value: tuple[PaymentOffer, ...],
    ) -> tuple[PaymentOffer, ...]:
        if not value:
            raise ValueError("a payable route must advertise at least one offer")
        return value


@runtime_checkable
class RouteConfigLike(Protocol):
    """Structural subset of ``RouteConfig`` needed for discovery."""

    charge_options: Sequence[Any]
    session_options: Sequence[Any]
    description: str | None


def _read_member(value: object, name: str, default: Any = _MISSING) -> Any:
    if isinstance(value, Mapping):
        result = value.get(name, _MISSING)
    else:
        result = getattr(value, name, _MISSING)
    if result is _MISSING:
        if default is _MISSING:
            raise DiscoveryConfigurationError(f"route option is missing {name}")
        return default
    return result


def _discovery_amount(value: object) -> str | None:
    """Return a draft-01 amount, or dynamic pricing for non-base-unit values."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value >= 0 else None
    if isinstance(value, str) and _AMOUNT_PATTERN.fullmatch(value):
        return value
    return None


def payment_offers_from_route_config(
    route_config: RouteConfigLike | Mapping[str, Any],
    *,
    payment_method: str = "xrpl",
) -> tuple[PaymentOffer, ...]:
    """Derive discovery offers from a middleware route configuration.

    Charge offers are emitted before session offers, matching the middleware's
    runtime challenge order.  Runtime decimal values cannot be represented by
    draft-01's integer base-unit schema; those offers are truthfully advertised
    as dynamic (``amount: null``) and resolved by the authoritative 402.
    """

    route_description = _read_member(route_config, "description", None)
    charge_options = _read_member(route_config, "charge_options", ())
    session_options = _read_member(route_config, "session_options", ())

    if not isinstance(charge_options, Sequence) or isinstance(
        charge_options, (str, bytes, bytearray)
    ):
        raise DiscoveryConfigurationError("charge_options must be a sequence")
    if not isinstance(session_options, Sequence) or isinstance(
        session_options, (str, bytes, bytearray)
    ):
        raise DiscoveryConfigurationError("session_options must be a sequence")

    offers: list[PaymentOffer] = []
    for intent, options in (
        ("charge", charge_options),
        ("session", session_options),
    ):
        for option in options:
            currency = _read_member(option, "currency", None)
            description = _read_member(option, "description", None)
            offers.append(
                PaymentOffer(
                    intent=intent,
                    method=payment_method,
                    amount=_discovery_amount(_read_member(option, "amount")),
                    currency=currency,
                    description=(
                        description if description is not None else route_description
                    ),
                )
            )

    if not offers:
        raise DiscoveryConfigurationError(
            "route configuration has no charge or session discovery offers"
        )
    return tuple(offers)


def parse_route_key(route_key: RouteKey) -> tuple[str, str]:
    """Normalize middleware route keys into ``(method, path)``."""

    if isinstance(route_key, tuple):
        if len(route_key) != 2:
            raise DiscoveryConfigurationError(
                "tuple route keys must contain exactly method and path"
            )
        method, path = route_key
    elif isinstance(route_key, str):
        parts = route_key.split(maxsplit=1)
        if len(parts) != 2:
            raise DiscoveryConfigurationError(
                "string route keys must use the form 'METHOD /path'"
            )
        method, path = parts
    else:
        raise DiscoveryConfigurationError("route keys must be strings or 2-tuples")

    try:
        return _normalize_http_method(method), _normalize_path(path)
    except (AttributeError, TypeError, ValueError) as exc:
        raise DiscoveryConfigurationError(str(exc)) from exc


def payable_routes_from_configs(
    route_configs: Mapping[RouteKey, RouteConfigLike | Mapping[str, Any]],
    *,
    payment_method: str = "xrpl",
) -> tuple[PayableRoute, ...]:
    """Build deterministic payable-route metadata from runtime configs."""

    routes: list[PayableRoute] = []
    for route_key, route_config in route_configs.items():
        method, path = parse_route_key(route_key)
        routes.append(
            PayableRoute(
                path=path,
                method=method,
                offers=payment_offers_from_route_config(
                    route_config,
                    payment_method=payment_method,
                ),
            )
        )
    return tuple(sorted(routes, key=lambda route: (route.path, route.method)))


def _validate_openapi_document(document: Mapping[str, Any]) -> None:
    version = document.get("openapi")
    if not isinstance(version, str) or not _OPENAPI_3_PATTERN.fullmatch(version):
        raise DiscoveryDocumentError("document must declare OpenAPI 3.x")

    info = document.get("info")
    if not isinstance(info, Mapping):
        raise DiscoveryDocumentError("document must contain an info object")
    for field in ("title", "version"):
        value = info.get(field)
        if not isinstance(value, str) or not value:
            raise DiscoveryDocumentError(f"document info.{field} must be a string")

    paths = document.get("paths")
    if not isinstance(paths, Mapping) or not paths:
        raise DiscoveryDocumentError("document must contain at least one OpenAPI path")


def _coerce_route(route: PayableRoute | Mapping[str, Any]) -> PayableRoute:
    if isinstance(route, PayableRoute):
        return route
    return PayableRoute.model_validate(route)


def _coerce_service_info(
    service_info: ServiceInfo | Mapping[str, Any],
) -> ServiceInfo:
    if isinstance(service_info, ServiceInfo):
        return service_info
    return ServiceInfo.model_validate(service_info)


def _validate_existing_402(response: object, *, method: str, path: str) -> None:
    if not isinstance(response, Mapping):
        raise DiscoveryDocumentError(
            f"{method.upper()} {path} has an invalid 402 response"
        )
    description = response.get("description")
    reference = response.get("$ref")
    if not (
        (isinstance(description, str) and description)
        or (isinstance(reference, str) and reference)
    ):
        raise DiscoveryDocumentError(
            f"{method.upper()} {path} 402 response needs description or $ref"
        )


def _offer_document(offer: PaymentOffer) -> dict[str, str | None]:
    document: dict[str, str | None] = {
        "intent": offer.intent,
        "method": offer.method,
        "amount": offer.amount,
    }
    if offer.currency is not None:
        document["currency"] = offer.currency
    if offer.description is not None:
        document["description"] = offer.description
    return document


def augment_openapi(
    document: Mapping[str, Any],
    *,
    routes: Iterable[PayableRoute | Mapping[str, Any]],
    service_info: ServiceInfo | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return an OpenAPI 3.x document augmented with MPP discovery metadata.

    The input mapping and all nested values are left untouched.  Existing
    non-payment fields are preserved.  An existing valid 402 response is also
    preserved, while payment extensions supplied here replace older values so
    the result reflects the runtime configuration used for this call.
    """

    if not isinstance(document, Mapping):
        raise DiscoveryDocumentError("OpenAPI document must be a mapping")
    _validate_openapi_document(document)

    normalized_routes = sorted(
        (_coerce_route(route) for route in routes),
        key=lambda route: (route.path, route.method),
    )
    seen: set[tuple[str, str]] = set()
    for route in normalized_routes:
        key = (route.method, route.path)
        if key in seen:
            raise DiscoveryConfigurationError(
                f"duplicate payable operation: {route.method.upper()} {route.path}"
            )
        seen.add(key)

    augmented: dict[str, Any] = deepcopy(dict(document))
    if service_info is not None:
        augmented["x-service-info"] = _coerce_service_info(service_info).model_dump(
            mode="json",
            by_alias=True,
            exclude_none=True,
        )

    paths = augmented["paths"]
    for route in normalized_routes:
        path_item = paths.get(route.path)
        if not isinstance(path_item, dict):
            raise DiscoveryDocumentError(
                f"OpenAPI document has no path item for {route.path}"
            )
        operation = path_item.get(route.method)
        if not isinstance(operation, dict):
            raise DiscoveryDocumentError(
                f"OpenAPI document has no {route.method.upper()} operation for {route.path}"
            )

        operation["x-payment-info"] = {
            "offers": [_offer_document(offer) for offer in route.offers]
        }

        responses = operation.get("responses")
        if responses is None:
            responses = {}
            operation["responses"] = responses
        if not isinstance(responses, dict):
            raise DiscoveryDocumentError(
                f"{route.method.upper()} {route.path} responses must be an object"
            )
        if "402" in responses:
            _validate_existing_402(
                responses["402"],
                method=route.method,
                path=route.path,
            )
        else:
            responses["402"] = {"description": "Payment Required"}

    return augmented


def augment_openapi_from_route_configs(
    document: Mapping[str, Any],
    *,
    route_configs: Mapping[RouteKey, RouteConfigLike | Mapping[str, Any]],
    service_info: ServiceInfo | Mapping[str, Any] | None = None,
    payment_method: str = "xrpl",
) -> dict[str, Any]:
    """Augment OpenAPI directly from middleware runtime route configs."""

    return augment_openapi(
        document,
        routes=payable_routes_from_configs(
            route_configs,
            payment_method=payment_method,
        ),
        service_info=service_info,
    )


__all__ = [
    "DiscoveryConfigurationError",
    "DiscoveryDocumentError",
    "DiscoveryError",
    "DiscoveryIntent",
    "PayableRoute",
    "PaymentOffer",
    "RouteConfigLike",
    "ServiceDocs",
    "ServiceInfo",
    "augment_openapi",
    "augment_openapi_from_route_configs",
    "parse_route_key",
    "payable_routes_from_configs",
    "payment_offers_from_route_config",
]
