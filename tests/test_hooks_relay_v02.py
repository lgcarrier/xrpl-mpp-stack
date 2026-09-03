from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

from pydantic import ValidationError
import pytest

from xrpl_mpp_core import PaymentReceipt
from xrpl_mpp_middleware.hooks import (
    ChallengeIssuedEvent,
    CredentialReceivedEvent,
    CredentialRejectedEvent,
    CredentialVerifiedEvent,
    HookContext,
    HookDispatchError,
    HookDispatcher,
    LifecycleEventType,
    PaymentLifecycleEvent,
    ReceiptSummary,
    SettlementFailedEvent,
    SettlementStartedEvent,
    SettlementSucceededEvent,
)
from xrpl_mpp_middleware.relay import (
    HTTPXRelaySender,
    IDEMPOTENCY_KEY_HEADER,
    PaymentOutcomeRelay,
    RelayConfigurationError,
    RelayHTTPError,
    RelayRequest,
    RelayResponse,
    RelayTimeoutError,
    RelayTransportError,
    RelayValidationError,
    ValidatedPaymentOutcome,
)


NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
PAYER = "rPAYER123456789"
RECIPIENT = "rRECIPIENT123456789"


def _context() -> HookContext:
    return HookContext(http_method="post", path="/paid", request_id="request-1")


def _started_event() -> SettlementStartedEvent:
    return SettlementStartedEvent(
        context=_context(),
        event_id="event-1",
        occurred_at=NOW,
        challenge_id="challenge-1",
        payment_method="xrpl",
        intent="charge",
    )


def _payment_receipt() -> PaymentReceipt:
    return PaymentReceipt(
        status="success",
        method="xrpl",
        timestamp="2026-08-30T12:00:00Z",
        reference="transaction-reference",
        challengeId="challenge-1",
        network="testnet",
        payer=PAYER,
        recipient=RECIPIENT,
        channelId="B" * 64,
        cumulative="25",
        action="voucher",
        txHash="A" * 64,
        settlementStatus="validated",
    )


def _outcome() -> ValidatedPaymentOutcome:
    return ValidatedPaymentOutcome.from_receipt(
        _payment_receipt(),
        http_method="post",
        path="/paid",
        validated_at=NOW,
    )


def test_all_lifecycle_events_are_typed_immutable_safe_envelopes() -> None:
    context = _context()
    events = (
        ChallengeIssuedEvent(
            context=context,
            event_id="event-challenge",
            occurred_at=NOW,
            challenge_id="challenge-1",
            payment_method="xrpl",
            intent="charge",
        ),
        CredentialReceivedEvent(
            context=context,
            event_id="event-received",
            occurred_at=NOW,
            credential_header="Payment-Authorization",
            challenge_id="challenge-1",
            payment_method="xrpl",
            intent="charge",
        ),
        CredentialVerifiedEvent(
            context=context,
            event_id="event-verified",
            occurred_at=NOW,
            challenge_id="challenge-1",
            payment_method="xrpl",
            intent="charge",
        ),
        CredentialRejectedEvent(
            context=context,
            event_id="event-rejected",
            occurred_at=NOW,
            reason_code="verification-failed",
            challenge_id="challenge-1",
        ),
        SettlementStartedEvent(
            context=context,
            event_id="event-started",
            occurred_at=NOW,
            challenge_id="challenge-1",
            payment_method="xrpl",
            intent="charge",
        ),
        SettlementSucceededEvent(
            context=context,
            event_id="event-succeeded",
            occurred_at=NOW,
            challenge_id="challenge-1",
            payment_method="xrpl",
            intent="charge",
            receipt=ReceiptSummary(
                reference="transaction-reference",
                settlement_status="validated",
                tx_hash="A" * 64,
            ),
        ),
        SettlementFailedEvent(
            context=context,
            event_id="event-failed",
            occurred_at=NOW,
            challenge_id="challenge-1",
            payment_method="xrpl",
            intent="charge",
            failure_code="ledger-unavailable",
            retryable=True,
        ),
    )

    assert tuple(event.event_type for event in events) == tuple(LifecycleEventType)
    assert all(event.context.http_method == "POST" for event in events)
    with pytest.raises(FrozenInstanceError):
        events[0].challenge_id = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        context.path = "/changed"  # type: ignore[misc]

    rendered = repr(events).lower()
    assert "signedtxblob" not in rendered
    assert "sessiontoken" not in rendered
    assert "wallet" not in rendered
    assert "secret" not in rendered


def test_default_received_event_does_not_accept_raw_credential_data() -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument 'credential'"):
        CredentialReceivedEvent(
            context=_context(),
            credential_header="Authorization",
            credential={"payload": {"signedTxBlob": "SECRET"}},  # type: ignore[call-arg]
        )

    with pytest.raises(ValueError, match="must name a supported HTTP field"):
        CredentialReceivedEvent(
            context=_context(),
            credential_header="Payment raw-secret",  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="safe identifier"):
        CredentialRejectedEvent(
            context=_context(),
            reason_code="verification failed: signedTxBlob=SECRET",
        )

    with pytest.raises(TypeError, match="immutable ReceiptSummary"):
        SettlementSucceededEvent(
            context=_context(),
            challenge_id="challenge-1",
            payment_method="xrpl",
            intent="session",
            receipt={"sessionToken": "SECRET"},  # type: ignore[arg-type]
        )


def test_dispatcher_runs_sync_and_async_hooks_and_reports_success() -> None:
    seen: list[tuple[str, str]] = []

    def sync_hook(event) -> None:
        seen.append(("sync", event.event_id))

    async def async_hook(event) -> None:
        await asyncio.sleep(0)
        seen.append(("async", event.event_id))

    dispatcher = HookDispatcher(default_timeout_seconds=0.5)
    dispatcher.register(sync_hook, name="sync")
    dispatcher.register(async_hook, name="async")

    report = asyncio.run(dispatcher.dispatch(_started_event()))

    assert report.ok is True
    assert report.invoked == 2
    assert report.succeeded == ("sync", "async")
    assert report.failures == ()
    assert sorted(seen) == [("async", "event-1"), ("sync", "event-1")]


def test_dispatcher_isolates_exceptions_and_per_hook_timeout_by_default() -> None:
    completed: list[str] = []

    async def healthy(_) -> None:
        completed.append("healthy")

    async def failing(_) -> None:
        raise RuntimeError("signedTxBlob=DO-NOT-EXPOSE")

    async def slow(_) -> None:
        await asyncio.sleep(0.2)

    dispatcher = HookDispatcher(default_timeout_seconds=0.1)
    dispatcher.register(healthy, name="healthy")
    dispatcher.register(failing, name="failing")
    dispatcher.register(slow, name="slow", timeout_seconds=0.01)

    report = asyncio.run(dispatcher.dispatch(_started_event()))

    assert completed == ["healthy"]
    assert report.succeeded == ("healthy",)
    failures = [
        (failure.hook_name, failure.error_type, failure.timed_out)
        for failure in report.failures
    ]
    assert failures == [
        ("failing", "RuntimeError", False),
        ("slow", "TimeoutError", True),
    ]
    assert "DO-NOT-EXPOSE" not in repr(report)


def test_dispatcher_optional_fail_closed_policy_raises_after_isolated_dispatch() -> None:
    reached: list[str] = []

    async def failing(_) -> None:
        raise ValueError("failure detail")

    async def healthy(_) -> None:
        reached.append("healthy")

    dispatcher = HookDispatcher([failing, healthy], fail_closed=True)

    with pytest.raises(HookDispatchError) as exc_info:
        asyncio.run(dispatcher.dispatch(_started_event()))

    assert reached == ["healthy"]
    assert exc_info.value.report.invoked == 2
    assert len(exc_info.value.report.failures) == 1
    assert "failure detail" not in str(exc_info.value)


def test_dispatcher_validates_timeouts_and_unique_names() -> None:
    dispatcher = HookDispatcher()
    dispatcher.register(lambda _: None, name="audit")
    with pytest.raises(ValueError, match="already registered"):
        dispatcher.register(lambda _: None, name="audit")
    with pytest.raises(ValueError, match="at most 30"):
        HookDispatcher(default_timeout_seconds=31)


def test_dispatcher_rejects_untyped_base_envelopes() -> None:
    dispatcher = HookDispatcher()
    untyped = PaymentLifecycleEvent(context=_context())

    with pytest.raises(TypeError, match="supported typed lifecycle event"):
        asyncio.run(dispatcher.dispatch(untyped))  # type: ignore[arg-type]


def test_lifecycle_timestamps_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        SettlementStartedEvent(
            context=_context(),
            occurred_at=datetime(2026, 8, 30, 12, 0),
            challenge_id="challenge-1",
            payment_method="xrpl",
            intent="charge",
        )


class CapturingSender:
    def __init__(self, response: RelayResponse | None = None) -> None:
        self.requests: list[RelayRequest] = []
        self.response = response or RelayResponse(status_code=202)

    async def __call__(self, request: RelayRequest) -> RelayResponse:
        self.requests.append(request)
        return self.response


def test_relay_forwards_sanitized_validated_outcome_with_idempotency_and_timeout() -> None:
    sender = CapturingSender()
    relay = PaymentOutcomeRelay(
        endpoint="https://relay.example.test/mpp/outcomes",
        sender=sender,
        timeout_seconds=0.5,
    )

    response = asyncio.run(
        relay.forward(_outcome(), idempotency_key="challenge-1:transaction-reference")
    )

    assert response.status_code == 202
    assert len(sender.requests) == 1
    request = sender.requests[0]
    assert request.url == "https://relay.example.test/mpp/outcomes"
    assert request.timeout_seconds == 0.5
    assert request.headers == {
        "Accept": "application/json",
        "Content-Type": "application/json",
        IDEMPOTENCY_KEY_HEADER: "challenge-1:transaction-reference",
    }
    assert "Authorization" not in request.headers
    assert "Payment-Authorization" not in request.headers
    assert request.mutable_json_body() == {
        "event": "payment.validated",
        "validatedAt": "2026-08-30T12:00:00+00:00",
        "operation": {"method": "POST", "path": "/paid"},
        "receipt": {
            "status": "success",
            "method": "xrpl",
            "timestamp": "2026-08-30T12:00:00Z",
            "reference": "transaction-reference",
            "challengeId": "challenge-1",
            "network": "testnet",
            "payer": PAYER,
            "recipient": RECIPIENT,
            "channelId": "B" * 64,
            "cumulative": "25",
            "action": "voucher",
            "txHash": "A" * 64,
            "settlementStatus": "validated",
        },
    }


def test_relay_projection_drops_credentials_tokens_blobs_and_wallet_secrets() -> None:
    receipt = {
        "status": "success",
        "method": "xrpl",
        "timestamp": "2026-08-30T12:00:00Z",
        "reference": "safe-reference",
        "challengeId": "challenge-1",
        "sessionToken": "SESSION-SECRET",
        "credential": {"payload": {"signedTxBlob": "SIGNED-BLOB"}},
        "privateKey": "PRIVATE-KEY",
        "seed": "WALLET-SEED",
        "wallet": {"secret": "WALLET-SECRET"},
    }
    outcome = ValidatedPaymentOutcome.from_receipt(
        receipt,
        http_method="GET",
        path="/paid",
        validated_at=NOW,
    )
    relay = PaymentOutcomeRelay(endpoint="https://relay.example.test")

    body = relay.build_request(outcome, idempotency_key="safe-key").mutable_json_body()
    rendered = repr(body)

    assert body["receipt"] == {
        "status": "success",
        "method": "xrpl",
        "timestamp": "2026-08-30T12:00:00Z",
        "reference": "safe-reference",
        "challengeId": "challenge-1",
    }
    for secret in (
        "SESSION-SECRET",
        "SIGNED-BLOB",
        "PRIVATE-KEY",
        "WALLET-SEED",
        "WALLET-SECRET",
    ):
        assert secret not in rendered


def test_relay_request_is_immutable_and_returns_fresh_mutable_json_copies() -> None:
    relay = PaymentOutcomeRelay(endpoint="https://relay.example.test")
    request = relay.build_request(_outcome(), idempotency_key="event-1")

    with pytest.raises(TypeError):
        request.headers["Authorization"] = "secret"  # type: ignore[index]
    with pytest.raises(TypeError):
        request.json_body["credential"] = "secret"  # type: ignore[index]
    with pytest.raises(TypeError):
        request.json_body["receipt"]["sessionToken"] = "secret"  # type: ignore[index]

    copy = request.mutable_json_body()
    copy["receipt"]["reference"] = "changed"
    assert request.mutable_json_body()["receipt"]["reference"] == "transaction-reference"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://relay.example.test/events",
        "http://localhost:8080/events",
        "http://127.0.0.1:8080/events",
        "https://user:password@relay.example.test/events",
        "https://relay.example.test/events#fragment",
        "https://127.0.0.1/events",
        "https://10.0.0.1/events",
        "https://169.254.169.254/latest/meta-data",
        "https://224.0.0.1/events",
        "https://[::1]/events",
        "https://[fc00::1]/events",
        "https://[ff02::1]/events",
    ],
)
def test_relay_requires_safe_https_endpoint_by_default(endpoint: str) -> None:
    with pytest.raises(RelayConfigurationError):
        PaymentOutcomeRelay(endpoint=endpoint)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:8080/events",
        "http://worker.localhost:8080/events",
        "http://127.0.0.1:8080/events",
        "http://[::1]:8080/events",
    ],
)
def test_relay_allows_explicit_insecure_localhost_test_endpoints(endpoint: str) -> None:
    relay = PaymentOutcomeRelay(
        endpoint=endpoint,
        allow_insecure_localhost=True,
    )
    assert relay.endpoint == endpoint


def test_insecure_localhost_opt_in_does_not_allow_non_loopback_http() -> None:
    with pytest.raises(RelayConfigurationError, match="must use HTTPS"):
        PaymentOutcomeRelay(
            endpoint="http://relay.example.test/events",
            allow_insecure_localhost=True,
        )


@pytest.mark.parametrize("timeout", [0, -1, 30.01])
def test_relay_timeout_is_positive_and_bounded(timeout: float) -> None:
    with pytest.raises(RelayConfigurationError, match="at most 30"):
        PaymentOutcomeRelay(
            endpoint="https://relay.example.test",
            timeout_seconds=timeout,
        )


@pytest.mark.parametrize("key", ["", "has space", "\nnewline", "x" * 256])
def test_relay_requires_safe_idempotency_key(key: str) -> None:
    relay = PaymentOutcomeRelay(endpoint="https://relay.example.test")
    with pytest.raises(RelayValidationError, match="idempotency key"):
        relay.build_request(_outcome(), idempotency_key=key)


def test_relay_enforces_timeout_even_if_injected_sender_ignores_request_timeout() -> None:
    class SlowSender:
        async def __call__(self, request: RelayRequest) -> RelayResponse:
            await asyncio.sleep(0.2)
            return RelayResponse(status_code=204)

    relay = PaymentOutcomeRelay(
        endpoint="https://relay.example.test",
        sender=SlowSender(),
        timeout_seconds=0.01,
    )

    with pytest.raises(RelayTimeoutError, match="timed out"):
        asyncio.run(relay.forward(_outcome(), idempotency_key="event-1"))


def test_relay_surfaces_http_status_without_forwarding_response_body() -> None:
    relay = PaymentOutcomeRelay(
        endpoint="https://relay.example.test",
        sender=CapturingSender(RelayResponse(status_code=503)),
    )

    with pytest.raises(RelayHTTPError) as exc_info:
        asyncio.run(relay.forward(_outcome(), idempotency_key="event-1"))

    assert exc_info.value.status_code == 503
    assert str(exc_info.value) == "payment outcome relay returned HTTP 503"


def test_relay_wraps_sender_exception_without_leaking_exception_detail() -> None:
    class FailingSender:
        async def __call__(self, request: RelayRequest) -> RelayResponse:
            raise RuntimeError("Authorization: Payment SECRET-CREDENTIAL")

    relay = PaymentOutcomeRelay(
        endpoint="https://relay.example.test",
        sender=FailingSender(),
    )

    with pytest.raises(RelayTransportError) as exc_info:
        asyncio.run(relay.forward(_outcome(), idempotency_key="event-1"))

    assert "SECRET-CREDENTIAL" not in str(exc_info.value)


def test_default_relay_sender_pins_the_vetted_address_against_dns_rebinding(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def resolve_once(hostname: str, port: int, *, type: int):
        captured["resolved"] = (hostname, port, type)
        return [(2, 1, 6, "", ("93.184.216.34", port))]

    class FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, url, **kwargs: object):
            captured["url"] = url
            captured["request_kwargs"] = kwargs
            return type("Response", (), {"status_code": 204, "headers": {}})()

    monkeypatch.setattr(
        "xrpl_mpp_middleware.relay.socket.getaddrinfo",
        resolve_once,
    )
    monkeypatch.setattr(
        "xrpl_mpp_middleware.relay.httpx.AsyncClient",
        FakeAsyncClient,
    )
    request = RelayRequest(
        url="https://relay.example.test:8443/mpp/outcomes",
        headers={"Content-Type": "application/json"},
        json_body={"event": "payment.validated"},
        timeout_seconds=1,
    )

    response = asyncio.run(HTTPXRelaySender()(request))

    assert response.status_code == 204
    assert str(captured["url"]) == "https://93.184.216.34:8443/mpp/outcomes"
    assert captured["client_kwargs"] == {
        "follow_redirects": False,
        "trust_env": False,
    }
    request_kwargs = captured["request_kwargs"]
    assert isinstance(request_kwargs, dict)
    assert request_kwargs["headers"]["Host"] == "relay.example.test:8443"
    assert request_kwargs["extensions"] == {"sni_hostname": "relay.example.test"}


def test_relay_rejects_unvalidated_arbitrary_objects() -> None:
    relay = PaymentOutcomeRelay(endpoint="https://relay.example.test")

    with pytest.raises(RelayValidationError, match="ValidatedPaymentOutcome"):
        relay.build_request(  # type: ignore[arg-type]
            {"credential": {"payload": {"signedTxBlob": "SECRET"}}},
            idempotency_key="event-1",
        )


def test_validated_outcome_is_frozen() -> None:
    outcome = _outcome()

    with pytest.raises(ValidationError, match="frozen"):
        outcome.receipt = outcome.receipt  # type: ignore[misc]
