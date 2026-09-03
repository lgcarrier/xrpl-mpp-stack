"""Optional application lifecycle hooks for MPP middleware.

These hooks are an operational integration surface, not part of the MPP wire
protocol.  Events intentionally contain identifiers and outcomes only.  Raw
credentials, signed transaction blobs, session tokens, wallet seeds, and
authorization fields are not accepted by the default event types.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
import inspect
import re
from typing import ClassVar, Literal, Protocol, TypeAlias
from uuid import uuid4


DEFAULT_HOOK_TIMEOUT_SECONDS = 1.0
MAX_HOOK_TIMEOUT_SECONDS = 30.0
_SAFE_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

CredentialHeader: TypeAlias = Literal["Authorization", "Payment-Authorization"]


class LifecycleEventType(StrEnum):
    CHALLENGE_ISSUED = "challenge_issued"
    CREDENTIAL_RECEIVED = "credential_received"
    CREDENTIAL_VERIFIED = "credential_verified"
    CREDENTIAL_REJECTED = "credential_rejected"
    SETTLEMENT_STARTED = "settlement_started"
    SETTLEMENT_SUCCEEDED = "settlement_succeeded"
    SETTLEMENT_FAILED = "settlement_failed"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _event_id() -> str:
    return uuid4().hex


def _required_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty trimmed string")


def _safe_code(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _SAFE_CODE_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a safe identifier, not error detail")


@dataclass(frozen=True, slots=True, kw_only=True)
class HookContext:
    """Safe HTTP request coordinates shared by all lifecycle events."""

    http_method: str
    path: str
    request_id: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.http_method, "http_method")
        if not isinstance(self.path, str) or not self.path.startswith("/") or any(
            char.isspace() for char in self.path
        ):
            raise ValueError("path must be an absolute HTTP path without whitespace")
        if self.request_id is not None:
            _required_text(self.request_id, "request_id")
        object.__setattr__(self, "http_method", self.http_method.upper())


@dataclass(frozen=True, slots=True, kw_only=True)
class ReceiptSummary:
    """Non-secret receipt fields suitable for lifecycle telemetry."""

    reference: str
    settlement_status: str | None = None
    tx_hash: str | None = None
    channel_id: str | None = None
    cumulative: str | None = None
    action: Literal["open", "voucher", "close"] | None = None

    def __post_init__(self) -> None:
        _required_text(self.reference, "reference")


@dataclass(frozen=True, slots=True, kw_only=True)
class PaymentLifecycleEvent:
    """Immutable base envelope for a typed lifecycle event."""

    context: HookContext
    event_id: str = field(default_factory=_event_id)
    occurred_at: datetime = field(default_factory=_utc_now)

    event_type: ClassVar[LifecycleEventType]

    def __post_init__(self) -> None:
        _required_text(self.event_id, "event_id")
        if not isinstance(self.context, HookContext):
            raise TypeError("context must be an immutable HookContext")
        if not isinstance(self.occurred_at, datetime) or (
            self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None
        ):
            raise ValueError("occurred_at must be timezone-aware")


@dataclass(frozen=True, slots=True, kw_only=True)
class ChallengeIssuedEvent(PaymentLifecycleEvent):
    event_type: ClassVar[LifecycleEventType] = LifecycleEventType.CHALLENGE_ISSUED

    challenge_id: str
    payment_method: str
    intent: str
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        super(ChallengeIssuedEvent, self).__post_init__()
        _required_text(self.challenge_id, "challenge_id")
        _required_text(self.payment_method, "payment_method")
        _required_text(self.intent, "intent")
        if self.expires_at is not None:
            if not isinstance(self.expires_at, datetime) or (
                self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None
            ):
                raise ValueError("expires_at must be timezone-aware")


@dataclass(frozen=True, slots=True, kw_only=True)
class CredentialReceivedEvent(PaymentLifecycleEvent):
    event_type: ClassVar[LifecycleEventType] = LifecycleEventType.CREDENTIAL_RECEIVED

    credential_header: CredentialHeader
    challenge_id: str | None = None
    payment_method: str | None = None
    intent: str | None = None

    def __post_init__(self) -> None:
        super(CredentialReceivedEvent, self).__post_init__()
        if self.credential_header not in {"Authorization", "Payment-Authorization"}:
            raise ValueError("credential_header must name a supported HTTP field")


@dataclass(frozen=True, slots=True, kw_only=True)
class CredentialVerifiedEvent(PaymentLifecycleEvent):
    event_type: ClassVar[LifecycleEventType] = LifecycleEventType.CREDENTIAL_VERIFIED

    challenge_id: str
    payment_method: str
    intent: str

    def __post_init__(self) -> None:
        super(CredentialVerifiedEvent, self).__post_init__()
        _required_text(self.challenge_id, "challenge_id")
        _required_text(self.payment_method, "payment_method")
        _required_text(self.intent, "intent")


@dataclass(frozen=True, slots=True, kw_only=True)
class CredentialRejectedEvent(PaymentLifecycleEvent):
    event_type: ClassVar[LifecycleEventType] = LifecycleEventType.CREDENTIAL_REJECTED

    reason_code: str
    challenge_id: str | None = None
    payment_method: str | None = None
    intent: str | None = None

    def __post_init__(self) -> None:
        super(CredentialRejectedEvent, self).__post_init__()
        _safe_code(self.reason_code, "reason_code")


@dataclass(frozen=True, slots=True, kw_only=True)
class SettlementStartedEvent(PaymentLifecycleEvent):
    event_type: ClassVar[LifecycleEventType] = LifecycleEventType.SETTLEMENT_STARTED

    challenge_id: str
    payment_method: str
    intent: str

    def __post_init__(self) -> None:
        super(SettlementStartedEvent, self).__post_init__()
        _required_text(self.challenge_id, "challenge_id")
        _required_text(self.payment_method, "payment_method")
        _required_text(self.intent, "intent")


@dataclass(frozen=True, slots=True, kw_only=True)
class SettlementSucceededEvent(PaymentLifecycleEvent):
    event_type: ClassVar[LifecycleEventType] = LifecycleEventType.SETTLEMENT_SUCCEEDED

    challenge_id: str
    payment_method: str
    intent: str
    receipt: ReceiptSummary

    def __post_init__(self) -> None:
        super(SettlementSucceededEvent, self).__post_init__()
        _required_text(self.challenge_id, "challenge_id")
        _required_text(self.payment_method, "payment_method")
        _required_text(self.intent, "intent")
        if not isinstance(self.receipt, ReceiptSummary):
            raise TypeError("receipt must be an immutable ReceiptSummary")


@dataclass(frozen=True, slots=True, kw_only=True)
class SettlementFailedEvent(PaymentLifecycleEvent):
    event_type: ClassVar[LifecycleEventType] = LifecycleEventType.SETTLEMENT_FAILED

    challenge_id: str
    payment_method: str
    intent: str
    failure_code: str
    retryable: bool = False

    def __post_init__(self) -> None:
        super(SettlementFailedEvent, self).__post_init__()
        _required_text(self.challenge_id, "challenge_id")
        _required_text(self.payment_method, "payment_method")
        _required_text(self.intent, "intent")
        _safe_code(self.failure_code, "failure_code")
        if not isinstance(self.retryable, bool):
            raise TypeError("retryable must be a boolean")


LifecycleEvent: TypeAlias = (
    ChallengeIssuedEvent
    | CredentialReceivedEvent
    | CredentialVerifiedEvent
    | CredentialRejectedEvent
    | SettlementStartedEvent
    | SettlementSucceededEvent
    | SettlementFailedEvent
)
_LIFECYCLE_EVENT_CLASSES = (
    ChallengeIssuedEvent,
    CredentialReceivedEvent,
    CredentialVerifiedEvent,
    CredentialRejectedEvent,
    SettlementStartedEvent,
    SettlementSucceededEvent,
    SettlementFailedEvent,
)


class AsyncHook(Protocol):
    async def __call__(self, event: LifecycleEvent) -> None: ...


HookCallback: TypeAlias = Callable[
    [LifecycleEvent],
    Awaitable[None] | None,
]


@dataclass(frozen=True, slots=True)
class RegisteredHook:
    callback: HookCallback
    name: str
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class HookFailure:
    hook_name: str
    error_type: str
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class HookDispatchReport:
    event_id: str
    invoked: int
    succeeded: tuple[str, ...]
    failures: tuple[HookFailure, ...]

    @property
    def ok(self) -> bool:
        return not self.failures


class HookDispatchError(RuntimeError):
    """Fail-closed hook error without potentially sensitive exception text."""

    def __init__(self, report: HookDispatchReport) -> None:
        self.report = report
        super().__init__(
            f"{len(report.failures)} lifecycle hook(s) failed for event {report.event_id}"
        )


def _callback_name(callback: HookCallback) -> str:
    module = getattr(callback, "__module__", None)
    name = getattr(callback, "__qualname__", None) or getattr(
        callback, "__name__", None
    )
    if name is None:
        name = callback.__class__.__qualname__
    return f"{module}.{name}" if module else name


def _is_async_callable(callback: HookCallback) -> bool:
    if inspect.iscoroutinefunction(callback):
        return True
    call = getattr(callback, "__call__", None)
    return call is not None and inspect.iscoroutinefunction(call)


async def _invoke_hook(callback: HookCallback, event: LifecycleEvent) -> None:
    if _is_async_callable(callback):
        result = callback(event)
    else:
        result = await asyncio.to_thread(callback, event)
    if inspect.isawaitable(result):
        await result


class HookDispatcher:
    """Dispatch lifecycle hooks concurrently with isolated bounded failures."""

    def __init__(
        self,
        hooks: Iterable[HookCallback] = (),
        *,
        default_timeout_seconds: float = DEFAULT_HOOK_TIMEOUT_SECONDS,
        fail_closed: bool = False,
    ) -> None:
        self._default_timeout_seconds = self._validate_timeout(
            default_timeout_seconds
        )
        self._fail_closed = fail_closed
        self._hooks: list[RegisteredHook] = []
        for callback in hooks:
            self.register(callback)

    @staticmethod
    def _validate_timeout(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError("hook timeout must be a number")
        normalized = float(value)
        if not 0 < normalized <= MAX_HOOK_TIMEOUT_SECONDS:
            raise ValueError(
                f"hook timeout must be greater than 0 and at most "
                f"{MAX_HOOK_TIMEOUT_SECONDS:g} seconds"
            )
        return normalized

    @property
    def fail_closed(self) -> bool:
        return self._fail_closed

    @property
    def hooks(self) -> tuple[RegisteredHook, ...]:
        return tuple(self._hooks)

    def register(
        self,
        callback: HookCallback,
        *,
        name: str | None = None,
        timeout_seconds: float | None = None,
    ) -> RegisteredHook:
        if not callable(callback):
            raise TypeError("hook callback must be callable")
        hook_name = name or _callback_name(callback)
        _required_text(hook_name, "hook name")
        if any(hook.name == hook_name for hook in self._hooks):
            raise ValueError(f"hook name is already registered: {hook_name}")
        registration = RegisteredHook(
            callback=callback,
            name=hook_name,
            timeout_seconds=(
                self._default_timeout_seconds
                if timeout_seconds is None
                else self._validate_timeout(timeout_seconds)
            ),
        )
        self._hooks.append(registration)
        return registration

    async def _run_registered(
        self,
        hook: RegisteredHook,
        event: LifecycleEvent,
    ) -> str | HookFailure:
        try:
            await asyncio.wait_for(
                _invoke_hook(hook.callback, event),
                timeout=hook.timeout_seconds,
            )
        except TimeoutError:
            return HookFailure(
                hook_name=hook.name,
                error_type="TimeoutError",
                timed_out=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolation is the feature here.
            return HookFailure(
                hook_name=hook.name,
                error_type=type(exc).__name__,
            )
        return hook.name

    async def dispatch(self, event: LifecycleEvent) -> HookDispatchReport:
        if type(event) not in _LIFECYCLE_EVENT_CLASSES:
            raise TypeError("event must be a supported typed lifecycle event")

        snapshot = tuple(self._hooks)
        results = await asyncio.gather(
            *(self._run_registered(hook, event) for hook in snapshot)
        )
        succeeded = tuple(result for result in results if isinstance(result, str))
        failures = tuple(
            result for result in results if isinstance(result, HookFailure)
        )
        report = HookDispatchReport(
            event_id=event.event_id,
            invoked=len(snapshot),
            succeeded=succeeded,
            failures=failures,
        )
        if self._fail_closed and failures:
            raise HookDispatchError(report)
        return report


__all__ = [
    "AsyncHook",
    "ChallengeIssuedEvent",
    "CredentialHeader",
    "CredentialReceivedEvent",
    "CredentialRejectedEvent",
    "CredentialVerifiedEvent",
    "DEFAULT_HOOK_TIMEOUT_SECONDS",
    "HookCallback",
    "HookContext",
    "HookDispatchError",
    "HookDispatcher",
    "HookDispatchReport",
    "HookFailure",
    "LifecycleEvent",
    "LifecycleEventType",
    "MAX_HOOK_TIMEOUT_SECONDS",
    "PaymentLifecycleEvent",
    "ReceiptSummary",
    "RegisteredHook",
    "SettlementFailedEvent",
    "SettlementStartedEvent",
    "SettlementSucceededEvent",
]
