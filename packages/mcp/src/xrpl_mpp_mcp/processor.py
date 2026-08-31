from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import wraps
import inspect
from typing import Any, Protocol, TypeVar, runtime_checkable

from xrpl_mpp_mcp.binding import (
    OperationBindingError,
    PaidMCPMethod,
    PaidOperationBinding,
    build_operation_binding,
    should_drop_paid_notification,
)
from xrpl_mpp_mcp.errors import (
    internal_payment_error_response,
    invalid_params_response,
    payment_required_response,
    payment_verification_failed_response,
)
from xrpl_mpp_mcp.metadata import (
    PaymentMetadataError,
    extract_paid_operation_credential,
    with_payment_receipt,
)
from xrpl_mpp_mcp.models import (
    MCPPaymentChallenge,
    MCPPaymentCredential,
    MCPPaymentFailure,
    MCPPaymentReceipt,
)


ResultT = TypeVar("ResultT")
MaybeAwaitable = ResultT | Awaitable[ResultT]


class PaymentVerificationFailed(Exception):
    def __init__(self, reason: str | None = None, detail: str | None = None) -> None:
        super().__init__(detail or reason or "payment verification failed")
        self.reason = reason
        self.detail = detail


class PaymentCredentialMalformed(Exception):
    pass


@runtime_checkable
class ReplaySafePaymentProcessor(Protocol):
    """Settlement hook used by :func:`paid_operation`.

    ``validate_and_consume`` must verify challenge binding and payment proof,
    then atomically check-and-mark the challenge as consumed. Implementations
    must ensure concurrent calls with one challenge produce at most one
    successful receipt.
    """

    async def create_challenges(
        self,
        operation: PaidOperationBinding,
    ) -> Sequence[MCPPaymentChallenge]: ...

    async def validate_and_consume(
        self,
        credential: MCPPaymentCredential,
        operation: PaidOperationBinding,
    ) -> MCPPaymentReceipt: ...


ChallengeHook = Callable[
    [PaidOperationBinding],
    MaybeAwaitable[Sequence[MCPPaymentChallenge]],
]
ValidationHook = Callable[
    [MCPPaymentCredential, PaidOperationBinding],
    MaybeAwaitable[MCPPaymentReceipt],
]


async def _resolve(value: MaybeAwaitable[ResultT]) -> ResultT:
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass(frozen=True, slots=True)
class CallbackPaymentProcessor:
    """Adapter for replay-safe challenge and validation callbacks."""

    challenge_hook: ChallengeHook
    validation_hook: ValidationHook

    async def create_challenges(
        self,
        operation: PaidOperationBinding,
    ) -> Sequence[MCPPaymentChallenge]:
        return await _resolve(self.challenge_hook(operation))

    async def validate_and_consume(
        self,
        credential: MCPPaymentCredential,
        operation: PaidOperationBinding,
    ) -> MCPPaymentReceipt:
        return await _resolve(self.validation_hook(credential, operation))


@dataclass(frozen=True, slots=True)
class PaidOperationContext:
    request_id: Any
    operation: PaidOperationBinding
    credential: MCPPaymentCredential
    receipt: MCPPaymentReceipt

    @property
    def params(self) -> Mapping[str, Any]:
        return self.operation.params


PaidHandler = Callable[[PaidOperationContext], MaybeAwaitable[Mapping[str, Any]]]


def paid_operation(
    processor: ReplaySafePaymentProcessor,
    *,
    expected_method: PaidMCPMethod | None = None,
) -> Callable[
    [PaidHandler],
    Callable[[Mapping[str, Any]], Awaitable[dict[str, Any] | None]],
]:
    """Decorate an MCP operation with payment challenge/verification handling.

    The wrapped function accepts a complete JSON-RPC message. The application
    handler is invoked only after ``validate_and_consume`` succeeds and receives
    params with payment metadata removed. Notifications are dropped without
    invoking the processor or application handler.
    """

    def decorator(
        handler: PaidHandler,
    ) -> Callable[[Mapping[str, Any]], Awaitable[dict[str, Any] | None]]:
        @wraps(handler)
        async def wrapped(message: Mapping[str, Any]) -> dict[str, Any] | None:
            if should_drop_paid_notification(message):
                return None

            request_id = message.get("id")
            try:
                operation = build_operation_binding(message)
            except OperationBindingError as exc:
                return invalid_params_response(request_id, detail=str(exc))
            if expected_method is not None and operation.method != expected_method:
                return invalid_params_response(
                    request_id,
                    detail=f"expected {expected_method}, got {operation.method}",
                )

            try:
                credential = extract_paid_operation_credential(message)
            except PaymentMetadataError as exc:
                return invalid_params_response(request_id, detail=str(exc))

            if credential is None:
                try:
                    challenges = await processor.create_challenges(operation)
                    return payment_required_response(request_id, challenges)
                except Exception:
                    return internal_payment_error_response(request_id)

            try:
                receipt = await processor.validate_and_consume(credential, operation)
            except PaymentCredentialMalformed as exc:
                return invalid_params_response(request_id, detail=str(exc))
            except PaymentVerificationFailed as exc:
                try:
                    challenges = await processor.create_challenges(operation)
                except Exception:
                    return internal_payment_error_response(request_id)
                return payment_verification_failed_response(
                    request_id,
                    challenges,
                    failure=MCPPaymentFailure(reason=exc.reason, detail=exc.detail),
                )
            except Exception:
                return internal_payment_error_response(request_id)

            if (
                receipt.challenge_id != credential.challenge.id
                or receipt.method != credential.challenge.method
            ):
                return internal_payment_error_response(request_id)

            context = PaidOperationContext(
                request_id=request_id,
                operation=operation,
                credential=credential,
                receipt=receipt,
            )
            try:
                result = await _resolve(handler(context))
            except Exception:
                return internal_payment_error_response(request_id)
            if not isinstance(result, Mapping):
                return internal_payment_error_response(
                    request_id,
                    detail="Paid MCP handlers must return a JSON object",
                )

            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": dict(result),
            }
            return with_payment_receipt(response, receipt, placement="nested")

        return wrapped

    return decorator


__all__ = [
    "CallbackPaymentProcessor",
    "PaidOperationContext",
    "PaymentCredentialMalformed",
    "PaymentVerificationFailed",
    "ReplaySafePaymentProcessor",
    "paid_operation",
]
