from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from xrpl_mpp_mcp.constants import PaymentErrorCode
from xrpl_mpp_mcp.models import (
    MCPPaymentChallenge,
    MCPPaymentFailure,
    MCPProblemDetails,
)


def _challenge_data(challenges: Sequence[MCPPaymentChallenge]) -> list[dict[str, Any]]:
    if not challenges:
        raise ValueError("at least one payment challenge is required")
    return [
        challenge.model_dump(by_alias=True, exclude_none=True)
        for challenge in challenges
    ]


def _error_response(
    *,
    request_id: Any,
    code: PaymentErrorCode,
    message: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": int(code), "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def payment_required_response(
    request_id: Any,
    challenges: Sequence[MCPPaymentChallenge],
    *,
    problem: MCPProblemDetails | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "httpStatus": 402,
        "challenges": _challenge_data(challenges),
    }
    if problem is not None:
        data["problem"] = problem.model_dump(by_alias=True, exclude_none=True)
    return _error_response(
        request_id=request_id,
        code=PaymentErrorCode.PAYMENT_REQUIRED,
        message="Payment Required",
        data=data,
    )


def payment_verification_failed_response(
    request_id: Any,
    challenges: Sequence[MCPPaymentChallenge],
    *,
    failure: MCPPaymentFailure | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "httpStatus": 402,
        "challenges": _challenge_data(challenges),
    }
    if failure is not None:
        data["failure"] = failure.model_dump(by_alias=True, exclude_none=True)
    return _error_response(
        request_id=request_id,
        code=PaymentErrorCode.PAYMENT_VERIFICATION_FAILED,
        message="Payment Verification Failed",
        data=data,
    )


def invalid_params_response(request_id: Any, *, detail: str) -> dict[str, Any]:
    return _error_response(
        request_id=request_id,
        code=PaymentErrorCode.INVALID_PARAMS,
        message="Invalid params",
        data={"detail": detail},
    )


def internal_payment_error_response(
    request_id: Any,
    *,
    detail: str = "Payment processing failed",
) -> dict[str, Any]:
    return _error_response(
        request_id=request_id,
        code=PaymentErrorCode.INTERNAL_ERROR,
        message="Internal error",
        data={"detail": detail},
    )


__all__ = [
    "internal_payment_error_response",
    "invalid_params_response",
    "payment_required_response",
    "payment_verification_failed_response",
]
