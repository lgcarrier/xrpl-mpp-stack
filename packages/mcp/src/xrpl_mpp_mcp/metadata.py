from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ValidationError

from xrpl_mpp_mcp.binding import is_supported_paid_operation
from xrpl_mpp_mcp.constants import CREDENTIAL_META_KEY, RECEIPT_META_KEY
from xrpl_mpp_mcp.models import MCPPaymentCredential, MCPPaymentReceipt


MetadataPlacement = Literal["root", "nested"]
ModelT = TypeVar("ModelT", bound=BaseModel)


class PaymentMetadataError(ValueError):
    pass


class ConflictingPaymentMetadataError(PaymentMetadataError):
    pass


class MalformedPaymentMetadataError(PaymentMetadataError):
    pass


def _metadata_object(container: Mapping[str, Any], *, label: str) -> Mapping[str, Any] | None:
    metadata = container.get("_meta")
    if metadata is None:
        return None
    if not isinstance(metadata, Mapping):
        raise MalformedPaymentMetadataError(f"{label}._meta must be a JSON object")
    return metadata


def _extract_value(
    message: Mapping[str, Any],
    *,
    key: str,
    nested_container: str,
    model_type: type[ModelT],
) -> ModelT | None:
    candidates: list[Any] = []
    root_metadata = _metadata_object(message, label="message")
    if root_metadata is not None and key in root_metadata:
        candidates.append(root_metadata[key])

    nested = message.get(nested_container)
    if nested is not None:
        if not isinstance(nested, Mapping):
            raise MalformedPaymentMetadataError(
                f"{nested_container} must be a JSON object for nested payment metadata"
            )
        nested_metadata = _metadata_object(nested, label=nested_container)
        if nested_metadata is not None and key in nested_metadata:
            candidates.append(nested_metadata[key])

    if not candidates:
        return None
    first = candidates[0]
    if any(candidate != first for candidate in candidates[1:]):
        raise ConflictingPaymentMetadataError(
            f"conflicting {key!r} values in root and nested _meta"
        )
    try:
        return model_type.model_validate(first)
    except ValidationError as exc:
        raise MalformedPaymentMetadataError(f"{key!r} does not match the MPP schema") from exc


def extract_payment_credential(
    message: Mapping[str, Any],
) -> MCPPaymentCredential | None:
    return _extract_value(
        message,
        key=CREDENTIAL_META_KEY,
        nested_container="params",
        model_type=MCPPaymentCredential,
    )


def extract_paid_operation_credential(
    message: Mapping[str, Any],
) -> MCPPaymentCredential | None:
    """Extract a credential only when the JSON-RPC method is payment-gated.

    The transport draft requires servers to ignore payment metadata on
    methods that do not require payment.
    """

    if not is_supported_paid_operation(message):
        return None
    return extract_payment_credential(message)


def extract_payment_receipt(
    message: Mapping[str, Any],
) -> MCPPaymentReceipt | None:
    return _extract_value(
        message,
        key=RECEIPT_META_KEY,
        nested_container="result",
        model_type=MCPPaymentReceipt,
    )


def _inject_value(
    message: Mapping[str, Any],
    *,
    key: str,
    value: BaseModel,
    placement: MetadataPlacement,
    nested_container: str,
) -> dict[str, Any]:
    output = deepcopy(dict(message))
    target: dict[str, Any]
    if placement == "root":
        target = output
    else:
        nested = output.setdefault(nested_container, {})
        if not isinstance(nested, dict):
            raise MalformedPaymentMetadataError(
                f"{nested_container} must be a JSON object for nested payment metadata"
            )
        target = nested

    metadata = target.setdefault("_meta", {})
    if not isinstance(metadata, dict):
        raise MalformedPaymentMetadataError("_meta must be a JSON object")
    serialized = value.model_dump(by_alias=True, exclude_none=True)
    if key in metadata and metadata[key] != serialized:
        raise ConflictingPaymentMetadataError(f"{key!r} is already present with another value")
    metadata[key] = serialized
    return output


def with_payment_credential(
    message: Mapping[str, Any],
    credential: MCPPaymentCredential,
    *,
    placement: MetadataPlacement = "nested",
) -> dict[str, Any]:
    return _inject_value(
        message,
        key=CREDENTIAL_META_KEY,
        value=credential,
        placement=placement,
        nested_container="params",
    )


def with_payment_receipt(
    message: Mapping[str, Any],
    receipt: MCPPaymentReceipt,
    *,
    placement: MetadataPlacement = "nested",
) -> dict[str, Any]:
    return _inject_value(
        message,
        key=RECEIPT_META_KEY,
        value=receipt,
        placement=placement,
        nested_container="result",
    )


__all__ = [
    "ConflictingPaymentMetadataError",
    "MalformedPaymentMetadataError",
    "MetadataPlacement",
    "PaymentMetadataError",
    "extract_payment_credential",
    "extract_paid_operation_credential",
    "extract_payment_receipt",
    "with_payment_credential",
    "with_payment_receipt",
]
