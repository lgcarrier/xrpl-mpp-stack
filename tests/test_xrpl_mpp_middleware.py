from __future__ import annotations

import asyncio
from typing import Callable

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from xrpl.wallet import Wallet

from xrpl_mpp_client import XRPLPaymentSigner, build_payment_authorization
from xrpl_mpp_core import (
    FacilitatorSupportedMethod,
    FacilitatorSupportedResponse,
    PaymentCredential,
    PaymentReceipt,
    XRPLAsset,
    XRPLChargeMethodDetails,
    XRPLChargeRequest,
    decode_challenge_request,
    build_payment_challenge,
    decode_payment_receipt,
    extract_payment_challenges,
)
from xrpl_mpp_middleware.exceptions import (
    FacilitatorPaymentError,
    FacilitatorProtocolError,
    RouteConfigurationError,
)
from xrpl_mpp_middleware.middleware import (
    PAYMENT_RECEIPT_HEADER,
    PaymentMiddlewareASGI,
    require_payment,
    require_session,
)
from xrpl_mpp_middleware.client import XRPLFacilitatorClient
from xrpl_mpp_middleware.types import RouteConfig, SessionRouteSpec

FACILITATOR_URL = "https://facilitator.example"
FACILITATOR_TOKEN = "secret-token"
CHALLENGE_SECRET = "middleware-test-secret"
DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
PAYER = "rPAYER123456789"


class FakeFacilitatorClient:
    def __init__(
        self,
        *,
        supported: FacilitatorSupportedResponse,
        charge_receipt: PaymentReceipt | None = None,
        session_receipt: PaymentReceipt | None = None,
        charge_error: Exception | None = None,
        session_error: Exception | None = None,
    ) -> None:
        self.supported = supported
        self.charge_receipt = charge_receipt
        self.session_receipt = session_receipt
        self.charge_error = charge_error
        self.session_error = session_error
        self.startup_calls = 0
        self.get_supported_calls = 0
        self.charge_calls = []
        self.session_calls = []

    async def startup(self) -> None:
        self.startup_calls += 1
        return None

    async def aclose(self) -> None:
        return None

    async def get_supported(self, *, force_refresh: bool = False) -> FacilitatorSupportedResponse:
        self.get_supported_calls += 1
        return self.supported

    async def charge(self, credential):
        self.charge_calls.append(credential)
        if self.charge_error is not None:
            raise self.charge_error
        if self.charge_receipt is None:
            raise AssertionError("charge_receipt must be configured")
        return self.charge_receipt

    async def session(self, credential):
        self.session_calls.append(credential)
        if self.session_error is not None:
            raise self.session_error
        if self.session_receipt is None:
            raise AssertionError("session_receipt must be configured")
        return self.session_receipt


def build_supported(*, intents: list[str] | None = None) -> FacilitatorSupportedResponse:
    return FacilitatorSupportedResponse(
        methods=[
            FacilitatorSupportedMethod(
                method="xrpl",
                intents=intents or ["charge", "session"],
                network="xrpl:1",
                assets=[XRPLAsset(code="XRP")],
                settlementMode="validated",
            )
        ]
    )


def build_charge_receipt() -> PaymentReceipt:
    return PaymentReceipt(
        method="xrpl",
        timestamp="2026-03-21T12:00:00Z",
        reference="ABC123HASH",
        challengeId="challenge-id",
        intent="charge",
        network="xrpl:1",
        payer=PAYER,
        recipient=DESTINATION,
        invoiceId="A" * 64,
        txHash="ABC123HASH",
        settlementStatus="validated",
        asset={"code": "XRP"},
        amount={"value": "1000", "unit": "drops", "asset": {"code": "XRP"}, "drops": 1000},
    )


def build_charge_credential() -> PaymentCredential:
    challenge = build_payment_challenge(
        secret=CHALLENGE_SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="charge",
        request_model=XRPLChargeRequest(
            amount="1000",
            currency="XRP:native",
            recipient=DESTINATION,
            methodDetails=XRPLChargeMethodDetails(network="xrpl:1", invoiceId="A" * 64),
        ),
        expires_in_seconds=300,
    )
    return PaymentCredential(challenge=challenge, payload={"signedTxBlob": "DEADBEEF"})


def build_session_receipt() -> PaymentReceipt:
    return PaymentReceipt(
        method="xrpl",
        timestamp="2026-03-21T12:00:00Z",
        reference="session-123",
        challengeId="challenge-id",
        intent="session",
        network="xrpl:1",
        payer=PAYER,
        recipient=DESTINATION,
        sessionId="session-123",
        sessionToken="session-token",
        settlementStatus="session_open",
        asset={"code": "XRP"},
        amount={"value": "1000", "unit": "drops", "asset": {"code": "XRP"}, "drops": 1000},
        availableBalance="750",
        prepaidTotal="1000",
        spentTotal="250",
        lastAction="open",
    )


def make_client_factory(client: FakeFacilitatorClient) -> Callable[[str, str], FakeFacilitatorClient]:
    def _factory(url: str, token: str) -> FakeFacilitatorClient:
        assert url == FACILITATOR_URL
        assert token == FACILITATOR_TOKEN
        return client

    return _factory


def build_app(client_factory, route_config=None) -> FastAPI:
    app = FastAPI()

    @app.get("/paid")
    async def paid(request: Request) -> dict[str, object]:
        payment = request.state.mpp_payment
        return {
            "intent": payment.intent,
            "payer": payment.payer,
            "reference": payment.reference,
        }

    app.add_middleware(
        PaymentMiddlewareASGI,
        route_configs={
            "GET /paid": route_config
            or require_payment(
                facilitator_url=FACILITATOR_URL,
                bearer_token=FACILITATOR_TOKEN,
                pay_to=DESTINATION,
                network="xrpl:1",
                xrp_drops=1000,
                description="Paid route",
            )
        },
        client_factory=client_factory,
        challenge_secret=CHALLENGE_SECRET,
    )
    return app


def build_post_app(client_factory, *, max_request_body_bytes: int = 32_768) -> FastAPI:
    app = FastAPI()

    @app.post("/paid")
    async def paid(request: Request) -> dict[str, object]:
        payment = request.state.mpp_payment
        return {
            "intent": payment.intent,
            "payer": payment.payer,
            "reference": payment.reference,
        }

    app.add_middleware(
        PaymentMiddlewareASGI,
        route_configs={
            "POST /paid": require_payment(
                facilitator_url=FACILITATOR_URL,
                bearer_token=FACILITATOR_TOKEN,
                pay_to=DESTINATION,
                network="xrpl:1",
                xrp_drops=1000,
                description="Paid route",
            )
        },
        client_factory=client_factory,
        challenge_secret=CHALLENGE_SECRET,
        max_request_body_bytes=max_request_body_bytes,
    )
    return app


def test_unpaid_request_returns_www_authenticate_payment_challenge() -> None:
    client = FakeFacilitatorClient(
        supported=build_supported(),
        charge_receipt=build_charge_receipt(),
    )
    app = build_app(make_client_factory(client))

    with TestClient(app) as test_client:
        response = test_client.get("/paid")

    assert response.status_code == 402
    assert response.headers["Cache-Control"] == "no-store"
    challenges = extract_payment_challenges(response.headers)
    assert len(challenges) == 1
    assert challenges[0].method == "xrpl"
    assert challenges[0].intent == "charge"
    assert response.json()["status"] == 402


def test_invalid_authorization_returns_fresh_challenge() -> None:
    client = FakeFacilitatorClient(
        supported=build_supported(),
        charge_receipt=build_charge_receipt(),
    )
    app = build_app(make_client_factory(client))

    with TestClient(app) as test_client:
        response = test_client.get("/paid", headers={"Authorization": "Bearer nope"})

    assert response.status_code == 402
    assert extract_payment_challenges(response.headers)
    assert client.charge_calls == []


def test_unprotected_routes_do_not_trigger_paid_route_startup() -> None:
    class FailingStartupClient(FakeFacilitatorClient):
        async def startup(self) -> None:
            self.startup_calls += 1
            raise RuntimeError("facilitator unavailable")

    client = FailingStartupClient(
        supported=build_supported(),
        charge_receipt=build_charge_receipt(),
    )
    app = build_app(make_client_factory(client))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    with TestClient(app) as test_client:
        response = test_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert client.startup_calls == 0
    assert client.get_supported_calls == 0


def test_valid_charge_authorization_injects_state_and_receipt_header() -> None:
    client = FakeFacilitatorClient(
        supported=build_supported(),
        charge_receipt=build_charge_receipt(),
    )
    app = build_app(make_client_factory(client))
    signer = XRPLPaymentSigner(Wallet.create(), network="xrpl:1", autofill_enabled=False)

    with TestClient(app) as test_client:
        challenge = extract_payment_challenges(test_client.get("/paid").headers)[0]
        response = test_client.get(
            "/paid",
            headers={"Authorization": build_payment_authorization(signer.build_charge_credential(challenge))},
        )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private"
    receipt = decode_payment_receipt(response.headers[PAYMENT_RECEIPT_HEADER])
    assert receipt.intent == "charge"
    assert response.json() == {
        "intent": "charge",
        "payer": PAYER,
        "reference": "ABC123HASH",
    }
    assert len(client.charge_calls) == 1


def test_protected_routes_reject_oversized_body_from_content_length() -> None:
    client = FakeFacilitatorClient(
        supported=build_supported(),
        charge_receipt=build_charge_receipt(),
    )
    app = build_post_app(
        make_client_factory(client),
        max_request_body_bytes=5,
    )

    with TestClient(app) as test_client:
        response = test_client.post("/paid", content=b"123456")

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}
    assert client.startup_calls == 0
    assert client.get_supported_calls == 0
    assert client.charge_calls == []


def test_protected_routes_reject_oversized_streamed_body() -> None:
    client = FakeFacilitatorClient(
        supported=build_supported(),
        charge_receipt=build_charge_receipt(),
    )
    app_called = False

    async def protected_app(scope, receive, send) -> None:
        nonlocal app_called
        app_called = True
        response = JSONResponse(status_code=200, content={"ok": True})
        await response(scope, receive, send)

    middleware = PaymentMiddlewareASGI(
        protected_app,
        route_configs={
            "POST /paid": require_payment(
                facilitator_url=FACILITATOR_URL,
                bearer_token=FACILITATOR_TOKEN,
                pay_to=DESTINATION,
                network="xrpl:1",
                xrp_drops=1000,
                description="Paid route",
            )
        },
        client_factory=make_client_factory(client),
        challenge_secret=CHALLENGE_SECRET,
        max_request_body_bytes=5,
    )

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/paid",
        "raw_path": b"/paid",
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    request_messages = iter(
        [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"56", "more_body": False},
        ]
    )
    response_messages: list[dict[str, object]] = []

    async def receive():
        return next(request_messages)

    async def send(message) -> None:
        response_messages.append(message)

    asyncio.run(middleware(scope, receive, send))

    response_start = next(
        message for message in response_messages if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in response_messages
        if message["type"] == "http.response.body"
    )

    assert response_start["status"] == 413
    assert response_body == b'{"detail":"Request body too large"}'
    assert app_called is False
    assert client.startup_calls == 0
    assert client.get_supported_calls == 0
    assert client.charge_calls == []


def test_session_route_uses_facilitator_session_receipt() -> None:
    client = FakeFacilitatorClient(
        supported=build_supported(),
        session_receipt=build_session_receipt(),
    )
    app = build_app(
        make_client_factory(client),
        route_config=require_session(
            facilitator_url=FACILITATOR_URL,
            bearer_token=FACILITATOR_TOKEN,
            pay_to=DESTINATION,
            network="xrpl:1",
            xrp_drops=250,
            min_prepay_amount="1000",
            description="Metered route",
        ),
    )
    signer = XRPLPaymentSigner(Wallet.create(), network="xrpl:1", autofill_enabled=False)

    with TestClient(app) as test_client:
        challenge = extract_payment_challenges(test_client.get("/paid").headers)[0]
        response = test_client.get(
            "/paid",
            headers={"Authorization": build_payment_authorization(signer.build_session_open_credential(challenge))},
        )

    receipt = decode_payment_receipt(response.headers[PAYMENT_RECEIPT_HEADER])
    assert response.status_code == 200
    assert response.json() == {
        "intent": "session",
        "payer": PAYER,
        "reference": "session-123",
    }
    assert receipt.session_id == "session-123"
    assert receipt.session_token == "session-token"
    assert len(client.session_calls) == 1


def test_multi_option_session_route_uses_distinct_initial_session_ids() -> None:
    client = FakeFacilitatorClient(
        supported=build_supported(),
        session_receipt=build_session_receipt(),
    )
    route_config = RouteConfig(
        facilitatorUrl=FACILITATOR_URL,
        bearerToken=FACILITATOR_TOKEN,
        sessionOptions=[
            SessionRouteSpec(
                network="xrpl:1",
                recipient=DESTINATION,
                assetIdentifier="XRP:native",
                amount="250",
                minPrepayAmount="1000",
                unitAmount="250",
                description="small session",
            ),
            SessionRouteSpec(
                network="xrpl:1",
                recipient=DESTINATION,
                assetIdentifier="XRP:native",
                amount="500",
                minPrepayAmount="2000",
                unitAmount="500",
                description="large session",
            ),
        ],
    )
    app = build_app(make_client_factory(client), route_config=route_config)

    with TestClient(app) as test_client:
        response = test_client.get("/paid")

    challenges = extract_payment_challenges(response.headers)
    session_ids = {
        decode_challenge_request(challenge).method_details.session_id
        for challenge in challenges
        if challenge.intent == "session"
    }

    assert len(challenges) == 2
    assert len(session_ids) == 2


def test_route_support_validation_rejects_unsupported_assets() -> None:
    client = FakeFacilitatorClient(
        supported=build_supported(),
        charge_receipt=build_charge_receipt(),
    )

    app = FastAPI()
    middleware = PaymentMiddlewareASGI(
        app,
        route_configs={
            "GET /paid": require_payment(
                facilitator_url=FACILITATOR_URL,
                bearer_token=FACILITATOR_TOKEN,
                pay_to=DESTINATION,
                network="xrpl:1",
                amount="1.25",
                asset_code="RLUSD",
                asset_issuer="rIssuer",
            )
        },
        client_factory=make_client_factory(client),
        challenge_secret=CHALLENGE_SECRET,
    )

    with TestClient(app):
        try:
            # Force startup validation.
            import asyncio

            asyncio.run(middleware.startup())
        except RouteConfigurationError as exc:
            assert "unsupported asset" in str(exc)
        else:
            raise AssertionError("Expected RouteConfigurationError")


def test_facilitator_payment_errors_return_fresh_challenge() -> None:
    client = FakeFacilitatorClient(
        supported=build_supported(),
        charge_error=FacilitatorPaymentError("charge", 402, "invalid payment"),
    )
    app = build_app(make_client_factory(client))
    signer = XRPLPaymentSigner(Wallet.create(), network="xrpl:1", autofill_enabled=False)

    with TestClient(app) as test_client:
        challenge = extract_payment_challenges(test_client.get("/paid").headers)[0]
        response = test_client.get(
            "/paid",
            headers={"Authorization": build_payment_authorization(signer.build_charge_credential(challenge))},
        )

    assert response.status_code == 402
    assert extract_payment_challenges(response.headers)


def test_paid_500_response_still_includes_payment_receipt() -> None:
    client = FakeFacilitatorClient(
        supported=build_supported(),
        charge_receipt=build_charge_receipt(),
    )
    app = FastAPI()

    @app.get("/paid")
    async def paid(request: Request) -> JSONResponse:
        assert request.state.mpp_payment.intent == "charge"
        return JSONResponse(status_code=500, content={"detail": "merchant failure"})

    app.add_middleware(
        PaymentMiddlewareASGI,
        route_configs={
            "GET /paid": require_payment(
                facilitator_url=FACILITATOR_URL,
                bearer_token=FACILITATOR_TOKEN,
                pay_to=DESTINATION,
                network="xrpl:1",
                xrp_drops=1000,
                description="Paid route",
            )
        },
        client_factory=make_client_factory(client),
        challenge_secret=CHALLENGE_SECRET,
    )
    signer = XRPLPaymentSigner(Wallet.create(), network="xrpl:1", autofill_enabled=False)

    with TestClient(app) as test_client:
        challenge = extract_payment_challenges(test_client.get("/paid").headers)[0]
        response = test_client.get(
            "/paid",
            headers={"Authorization": build_payment_authorization(signer.build_charge_credential(challenge))},
        )

    receipt = decode_payment_receipt(response.headers[PAYMENT_RECEIPT_HEADER])
    assert response.status_code == 500
    assert response.json() == {"detail": "merchant failure"}
    assert receipt.intent == "charge"
    assert response.headers["Cache-Control"] == "private"


def test_paid_exception_still_returns_payment_receipt() -> None:
    client = FakeFacilitatorClient(
        supported=build_supported(),
        charge_receipt=build_charge_receipt(),
    )
    app = FastAPI()

    @app.get("/paid")
    async def paid(request: Request) -> dict[str, str]:
        assert request.state.mpp_payment.intent == "charge"
        raise RuntimeError("boom")

    app.add_middleware(
        PaymentMiddlewareASGI,
        route_configs={
            "GET /paid": require_payment(
                facilitator_url=FACILITATOR_URL,
                bearer_token=FACILITATOR_TOKEN,
                pay_to=DESTINATION,
                network="xrpl:1",
                xrp_drops=1000,
                description="Paid route",
            )
        },
        client_factory=make_client_factory(client),
        challenge_secret=CHALLENGE_SECRET,
    )
    signer = XRPLPaymentSigner(Wallet.create(), network="xrpl:1", autofill_enabled=False)

    with TestClient(app, raise_server_exceptions=False) as test_client:
        challenge = extract_payment_challenges(test_client.get("/paid").headers)[0]
        response = test_client.get(
            "/paid",
            headers={"Authorization": build_payment_authorization(signer.build_charge_credential(challenge))},
        )

    receipt = decode_payment_receipt(response.headers[PAYMENT_RECEIPT_HEADER])
    assert response.status_code == 500
    assert response.json() == {
        "detail": "The protected application failed after payment settlement"
    }
    assert receipt.intent == "charge"


def test_facilitator_authentication_errors_return_502() -> None:
    client = FakeFacilitatorClient(
        supported=build_supported(),
        charge_error=FacilitatorProtocolError(
            "Facilitator authentication failed: Invalid authentication credentials"
        ),
    )
    app = build_app(make_client_factory(client))
    signer = XRPLPaymentSigner(Wallet.create(), network="xrpl:1", autofill_enabled=False)

    with TestClient(app) as test_client:
        challenge = extract_payment_challenges(test_client.get("/paid").headers)[0]
        response = test_client.get(
            "/paid",
            headers={"Authorization": build_payment_authorization(signer.build_charge_credential(challenge))},
        )

    assert response.status_code == 502
    assert response.json() == {
        "detail": "Facilitator authentication failed: Invalid authentication credentials"
    }
    assert "www-authenticate" not in response.headers


def test_facilitator_client_treats_401_as_protocol_error() -> None:
    async_client = httpx.AsyncClient(
        base_url=FACILITATOR_URL,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                401,
                json={"detail": "Invalid authentication credentials"},
                request=request,
            )
        ),
    )
    client = XRPLFacilitatorClient(
        base_url=FACILITATOR_URL,
        bearer_token=FACILITATOR_TOKEN,
        async_client=async_client,
    )

    import asyncio

    async def _run() -> None:
        try:
            with pytest.raises(
                FacilitatorProtocolError,
                match="Facilitator authentication failed: Invalid authentication credentials",
            ):
                await client.charge(build_charge_credential())
        finally:
            await async_client.aclose()

    asyncio.run(_run())
