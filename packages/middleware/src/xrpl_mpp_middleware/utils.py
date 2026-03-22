from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from xrpl_mpp_core import (
    canonical_asset_identifier,
    decode_header_model as decode_header_model_core,
    encode_model_to_base64url,
    is_valid_xrpl_network,
    payment_option_matches,
)

from xrpl_mpp_middleware.exceptions import InvalidPaymentHeaderError

ModelType = TypeVar("ModelType", bound=BaseModel)


def decode_header_model(raw_value: str, model_type: type[ModelType]) -> ModelType:
    try:
        return decode_header_model_core(raw_value, model_type)
    except ValueError as exc:
        raise InvalidPaymentHeaderError(str(exc)) from exc


__all__ = [
    "canonical_asset_identifier",
    "decode_header_model",
    "encode_model_to_base64url",
    "is_valid_xrpl_network",
    "payment_option_matches",
]
