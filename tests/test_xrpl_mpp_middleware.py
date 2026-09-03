from __future__ import annotations

import asyncio
from collections.abc import Callable
import json

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from xrpl.core import binarycodec
from xrpl.models.transactions import Payment
from xrpl.wallet import Wallet

from xrpl_mpp_client import XRPLPaymentSigner, build_payment_authorization
from xrpl_mpp_core import (
    PAYMENT_AUTHORIZATION_HEADER,
    FacilitatorSupportedMethod,
    FacilitatorSupportedResponse,
    PaymentReceipt,
    XRPLChargeMethodDetails,
    XRPLChargeRequest,
    XRPLSessionMethodDetails,
    XRPLSessionRequest,
    build_payment_challenge,
    decode_charge_payload,
    decode_challenge_request,
    decode_payment_receipt,
    extract_payment_challenges,
)
from xrpl_mpp_middleware.client import XRPLFacilitatorClient
from xrpl_mpp_middleware.exceptions import (
    FacilitatorPaymentError,
    FacilitatorProtocolError,
    RouteConfigurationError,
)
from xrpl_mpp_middleware.middleware import (
    PAYCHANNEL_CUMULATIVE_HEADER,
    PAYCHANNEL_ID_HEADER,
    PAYMENT_RECEIPT_HEADER,
    PaymentMiddlewareASGI,
    require_payment,
    require_session,
)
from xrpl_mpp_middleware.types import ChargeRouteSpec, RouteConfig

FACILITATOR_URL = "https://facilitator.example"
FACILITATOR_TOKEN = "secret-token"
CHALLENGE_SECRET = "middleware-test-secret-minimum-32-bytes"
DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
PAYER = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"


class FakeFacilitatorClient:
    def __init__(
        self,
        *,
        supported: FacilitatorSupportedResponse,
        charge_receipt: PaymentReceipt | None = None,
        session_receipt: PaymentReceipt | None = None,
        charge_error: Exception | None = None,
    ) -> None:
        self.supported = supported
        self.charge_receipt = charge_receipt
        self.session_receipt = session_receipt
        self.charge_error = charge_error
        self.startup_calls = 0
        self.charge_calls = []
        self.session_calls = []

    async def startup(self) -> None:
        self.startup_calls += 1

    async def aclose(self) -> None:
        return None

    async def get_supported(self, *, force_refresh: bool = False):
        return self.supported

    async def charge(self, credential):
        self.charge_calls.append(credential)
        if self.charge_error is not None:
            raise self.charge_error
        assert self.charge_receipt is not None
        return self.charge_receipt

    async def session(self, credential):
        self.session_calls.append(credential)
        assert self.session_receipt is not None
        return self.session_receipt


def _supported(*, currencies: list[str] | None = None, intents: list[str] | None = None):
    return FacilitatorSupportedResponse(
        methods=[
            FacilitatorSupportedMethod(
                method="xrpl",
                intents=intents or ["charge", "session"],
                network="testnet",
                currencies=currencies or ["XRP"],
                settlementMode="validated",
            )
        ]
    )


def _charge_receipt() -> PaymentReceipt:
    return PaymentReceipt(
        status="success",
        method="xrpl",
        timestamp="2026-08-30T12:00:00Z",
        reference="A" * 64,
        network="testnet",
        payer=PAYER,
        recipient=DESTINATION,
        txHash="A" * 64,
        settlementStatus="validated",
    )


def _session_receipt(*, action: str = "voucher") -> PaymentReceipt:
    return PaymentReceipt(
        status="success",
        method="xrpl",
        timestamp="2026-08-30T12:00:00Z",
        reference=f"{'C' * 64}:125",
        network="testnet",
        payer=PAYER,
        recipient=DESTINATION,
        channelId="C" * 64,
        cumulative="125",
        action=action,
    )


def _factory(client: FakeFacilitatorClient) -> Callable[[str, str], FakeFacilitatorClient]:
    def build(url: str, token: str) -> FakeFacilitatorClient:
        assert (url, token) == (FACILITATOR_URL, FACILITATOR_TOKEN)
        return client

    return build


def _charge_route(*, alternate: bool = False) -> RouteConfig:
    route = require_payment(
        facilitator_url=FACILITATOR_URL,
        bearer_token=FACILITATOR_TOKEN,
        pay_to=DESTINATION,
        network="testnet",
        xrp_drops=1000,
        description="Paid route",
    )
    if alternate:
        route = route.model_copy(update={"credential_header": PAYMENT_AUTHORIZATION_HEADER})
    return route


def _app(
    facilitator: FakeFacilitatorClient,
    *,
    route: RouteConfig | None = None,
    failure: str | None = None,
) -> FastAPI:
    app = FastAPI()

    @app.get("/paid")
    async def paid(request: Request):
        receipt = request.state.mpp_payment
        if failure == "response":
            return JSONResponse(status_code=500, content={"detail": "merchant failure"})
        if failure == "exception":
            raise RuntimeError("boom")
        return {"payer": receipt.payer, "reference": receipt.reference}

    app.add_middleware(
        PaymentMiddlewareASGI,
        route_configs={"GET /paid": route or _charge_route()},
        client_factory=_factory(facilitator),
        challenge_secrets=[CHALLENGE_SECRET, "previous-secret"],
    )
    return app


def test_unpaid_request_returns_native_bound_charge_challenge() -> None:
    facilitator = FakeFacilitatorClient(
        supported=_supported(),
        charge_receipt=_charge_receipt(),
    )

    with TestClient(_app(facilitator)) as client:
        response = client.get("/paid")

    challenge = extract_payment_challenges(response.headers)[0]
    request = decode_challenge_request(challenge)
    assert response.status_code == 402
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["Cache-Control"] == "no-store"
    assert (challenge.method, challenge.intent) == ("xrpl", "charge")
    assert (request.currency, request.method_details.network) == ("XRP", "testnet")


def test_valid_charge_injects_state_and_only_adds_receipt_to_2xx() -> None:
    facilitator = FakeFacilitatorClient(
        supported=_supported(),
        charge_receipt=_charge_receipt(),
    )
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)

    with TestClient(_app(facilitator)) as client:
        challenge = extract_payment_challenges(client.get("/paid").headers)[0]
        response = client.get(
            "/paid",
            headers={
                "Authorization": build_payment_authorization(
                    signer.build_charge_credential(challenge)
                )
            },
        )

    receipt = decode_payment_receipt(response.headers[PAYMENT_RECEIPT_HEADER])
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private"
    assert receipt.reference == "A" * 64
    assert response.json() == {"payer": PAYER, "reference": "A" * 64}
    assert len(facilitator.charge_calls) == 1


def test_challenge_binding_includes_raw_query_string() -> None:
    facilitator = FakeFacilitatorClient(
        supported=_supported(),
        charge_receipt=_charge_receipt(),
    )
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)

    with TestClient(_app(facilitator)) as client:
        challenge = extract_payment_challenges(client.get("/paid?tier=basic").headers)[0]
        response = client.get(
            "/paid?tier=premium",
            headers={
                "Authorization": build_payment_authorization(
                    signer.build_charge_credential(challenge)
                )
            },
        )

    assert response.status_code == 402
    assert response.json()["type"].endswith("/invalid-challenge")
    assert not facilitator.charge_calls


def test_verified_payment_header_is_removed_before_application_dispatch() -> None:
    facilitator = FakeFacilitatorClient(
        supported=_supported(),
        charge_receipt=_charge_receipt(),
    )
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    app = FastAPI()

    @app.get("/paid")
    async def paid(request: Request):
        return {
            "authorization": request.headers.get("authorization"),
            "paymentAuthorization": request.headers.get(PAYMENT_AUTHORIZATION_HEADER),
        }

    app.add_middleware(
        PaymentMiddlewareASGI,
        route_configs={"GET /paid": _charge_route(alternate=True)},
        client_factory=_factory(facilitator),
        challenge_secrets=[CHALLENGE_SECRET],
    )

    with TestClient(app) as client:
        initial = client.get("/paid", headers={"Authorization": "Bearer identity"})
        challenge = extract_payment_challenges(initial.headers)[0]
        response = client.get(
            "/paid",
            headers={
                "Authorization": "Bearer identity",
                PAYMENT_AUTHORIZATION_HEADER: build_payment_authorization(
                    signer.build_charge_credential(challenge)
                ),
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "authorization": "Bearer identity",
        "paymentAuthorization": None,
    }


def test_alternate_header_preserves_bearer_and_rejects_wrong_field() -> None:
    facilitator = FakeFacilitatorClient(
        supported=_supported(),
        charge_receipt=_charge_receipt(),
    )
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)

    with TestClient(_app(facilitator, route=_charge_route(alternate=True))) as client:
        initial = client.get("/paid", headers={"Authorization": "Bearer identity"})
        challenge = extract_payment_challenges(initial.headers)[0]
        credential = build_payment_authorization(signer.build_charge_credential(challenge))
        wrong = client.get("/paid", headers={"Authorization": credential})
        paid = client.get(
            "/paid",
            headers={
                "Authorization": "Bearer identity",
                PAYMENT_AUTHORIZATION_HEADER: credential,
            },
        )

    assert challenge.header == PAYMENT_AUTHORIZATION_HEADER
    assert wrong.status_code == 402
    assert wrong.json()["type"].endswith("/invalid-challenge")
    assert paid.status_code == 200


@pytest.mark.parametrize(
    "duplicate_headers",
    [
        [("Authorization", "Bearer first"), ("Authorization", "Bearer second")],
        [
            (PAYMENT_AUTHORIZATION_HEADER, "Payment first"),
            (PAYMENT_AUTHORIZATION_HEADER, "Payment second"),
        ],
    ],
)
def test_duplicate_authorization_field_lines_are_rejected_before_facilitator_startup(
    duplicate_headers: list[tuple[str, str]],
) -> None:
    facilitator = FakeFacilitatorClient(
        supported=_supported(),
        charge_receipt=_charge_receipt(),
    )

    with TestClient(_app(facilitator, route=_charge_route(alternate=True))) as client:
        response = client.get("/paid", headers=duplicate_headers)

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Duplicate authorization header fields are not allowed"
    }
    assert facilitator.startup_calls == 0
    assert not facilitator.charge_calls


def test_malformed_credential_returns_typed_problem_and_fresh_challenge() -> None:
    facilitator = FakeFacilitatorClient(
        supported=_supported(),
        charge_receipt=_charge_receipt(),
    )

    with TestClient(_app(facilitator, route=_charge_route(alternate=True))) as client:
        response = client.get(
            "/paid",
            headers={PAYMENT_AUTHORIZATION_HEADER: "Payment not-base64!"},
        )

    assert response.status_code == 402
    assert response.json()["type"].endswith("/malformed-credential")
    assert extract_payment_challenges(response.headers)
    assert not facilitator.charge_calls


def test_accept_payment_ranks_multi_offer_route() -> None:
    facilitator = FakeFacilitatorClient(
        supported=_supported(),
        charge_receipt=_charge_receipt(),
        session_receipt=_session_receipt(),
    )
    route = RouteConfig(
        facilitatorUrl=FACILITATOR_URL,
        bearerToken=FACILITATOR_TOKEN,
        chargeOptions=[
            ChargeRouteSpec(
                network="testnet",
                recipient=DESTINATION,
                currency="XRP",
                amount="1000",
            )
        ],
        sessionOptions=[
            {
                "network": "testnet",
                "recipient": DESTINATION,
                "amount": "25",
                "channelId": "C" * 64,
            }
        ],
    )

    with TestClient(_app(facilitator, route=route)) as client:
        response = client.get(
            "/paid",
            headers={"Accept-Payment": "xrpl/session, xrpl/charge;q=0.2"},
        )

    challenges = extract_payment_challenges(response.headers)
    assert [item.intent for item in challenges] == ["session", "charge"]


def test_paychannel_headers_bind_session_challenge_and_voucher() -> None:
    facilitator = FakeFacilitatorClient(
        supported=_supported(),
        session_receipt=_session_receipt(),
    )
    route = require_session(
        facilitator_url=FACILITATOR_URL,
        bearer_token=FACILITATOR_TOKEN,
        pay_to=DESTINATION,
        network="testnet",
        xrp_drops=25,
    )
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    channel_headers = {
        PAYCHANNEL_ID_HEADER: "C" * 64,
        PAYCHANNEL_CUMULATIVE_HEADER: "100",
    }

    with TestClient(_app(facilitator, route=route)) as client:
        initial = client.get("/paid", headers=channel_headers)
        challenge = extract_payment_challenges(initial.headers)[0]
        request = decode_challenge_request(challenge)
        credential = signer.build_session_voucher_credential(challenge)
        paid = client.get(
            "/paid",
            headers={
                **channel_headers,
                "Authorization": build_payment_authorization(credential),
            },
        )

    assert request.channel_id == "C" * 64
    assert request.method_details.cumulative_amount == "100"
    assert paid.status_code == 200
    assert decode_payment_receipt(paid.headers[PAYMENT_RECEIPT_HEADER]).cumulative == "125"
    assert len(facilitator.session_calls) == 1


@pytest.mark.parametrize(
    "headers",
    [
        [(PAYCHANNEL_ID_HEADER, "not-a-channel"), (PAYCHANNEL_CUMULATIVE_HEADER, "100")],
        [(PAYCHANNEL_ID_HEADER, "C" * 64), (PAYCHANNEL_CUMULATIVE_HEADER, "-1")],
        [(PAYCHANNEL_ID_HEADER, "C" * 64)],
        [
            (PAYCHANNEL_ID_HEADER, "C" * 64),
            (PAYCHANNEL_ID_HEADER, "D" * 64),
            (PAYCHANNEL_CUMULATIVE_HEADER, "100"),
        ],
    ],
)
def test_malformed_paychannel_hints_return_controlled_client_error(headers) -> None:
    facilitator = FakeFacilitatorClient(supported=_supported())
    route = require_session(
        facilitator_url=FACILITATOR_URL,
        bearer_token=FACILITATOR_TOKEN,
        pay_to=DESTINATION,
        network="testnet",
        xrp_drops=25,
    )

    with TestClient(_app(facilitator, route=route)) as client:
        response = client.get("/paid", headers=headers)

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid PayChannel session hint headers"}
    assert "WWW-Authenticate" not in response.headers
    assert facilitator.startup_calls == 0


@pytest.mark.parametrize("failure", ["response", "exception"])
def test_paid_application_failure_never_gets_a_receipt(failure: str) -> None:
    facilitator = FakeFacilitatorClient(
        supported=_supported(),
        charge_receipt=_charge_receipt(),
    )
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)

    with TestClient(
        _app(facilitator, route=_charge_route(alternate=True), failure=failure),
        raise_server_exceptions=False,
    ) as client:
        challenge = extract_payment_challenges(client.get("/paid").headers)[0]
        response = client.get(
            "/paid",
            headers={
                "Authorization": "Bearer identity",
                PAYMENT_AUTHORIZATION_HEADER: build_payment_authorization(
                    signer.build_charge_credential(challenge)
                ),
            },
        )

    assert response.status_code == 500
    assert PAYMENT_RECEIPT_HEADER not in response.headers
    if failure == "exception":
        assert response.json()["paymentReference"] == "A" * 64


def test_facilitator_payment_error_returns_fresh_challenge() -> None:
    facilitator = FakeFacilitatorClient(
        supported=_supported(),
        charge_error=FacilitatorPaymentError("charge", 402, "invalid payment"),
    )
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)

    with TestClient(_app(facilitator)) as client:
        challenge = extract_payment_challenges(client.get("/paid").headers)[0]
        response = client.get(
            "/paid",
            headers={
                "Authorization": build_payment_authorization(
                    signer.build_charge_credential(challenge)
                )
            },
        )

    assert response.status_code == 402
    assert extract_payment_challenges(response.headers)


def test_route_startup_rejects_unadvertised_currency() -> None:
    facilitator = FakeFacilitatorClient(
        supported=_supported(currencies=["XRP"]),
        charge_receipt=_charge_receipt(),
    )
    route = require_payment(
        facilitator_url=FACILITATOR_URL,
        bearer_token=FACILITATOR_TOKEN,
        pay_to=DESTINATION,
        network="testnet",
        amount="1.25",
        asset_code="USD",
        asset_issuer=DESTINATION,
    )
    middleware = PaymentMiddlewareASGI(
        FastAPI(),
        route_configs={"GET /paid": route},
        client_factory=_factory(facilitator),
        challenge_secrets=[CHALLENGE_SECRET],
    )

    with pytest.raises(RouteConfigurationError, match="unsupported currency"):
        asyncio.run(middleware.startup())


def test_protected_route_rejects_oversized_body_before_facilitator_startup() -> None:
    facilitator = FakeFacilitatorClient(
        supported=_supported(),
        charge_receipt=_charge_receipt(),
    )
    app = FastAPI()

    @app.post("/paid")
    async def paid():
        return {"ok": True}

    app.add_middleware(
        PaymentMiddlewareASGI,
        route_configs={"POST /paid": _charge_route()},
        client_factory=_factory(facilitator),
        challenge_secrets=[CHALLENGE_SECRET],
        max_request_body_bytes=5,
    )

    with TestClient(app) as client:
        response = client.post("/paid", content=b"123456")

    assert response.status_code == 413
    assert facilitator.startup_calls == 0


def test_facilitator_client_maps_401_to_protocol_error() -> None:
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

    async def run() -> None:
        try:
            with pytest.raises(FacilitatorProtocolError, match="authentication failed"):
                await client.charge(
                    XRPLPaymentSigner(
                        Wallet.create(),
                        network="testnet",
                        autofill_enabled=False,
                    ).build_charge_credential(
                        extract_payment_challenges(
                            httpx.Headers(
                                {
                                    "WWW-Authenticate": (
                                        'Payment id="x", realm="r", method="xrpl", '
                                        'intent="charge", request="e30"'
                                    )
                                }
                            )
                        )[0]
                    )
                )
        finally:
            await async_client.aclose()

    # The client performs the HTTP status mapping before response-model parsing.
    with pytest.raises((FacilitatorProtocolError, ValueError)):
        asyncio.run(run())


def test_facilitator_client_requires_tls_unless_development_opt_in() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        XRPLFacilitatorClient(
            base_url="http://127.0.0.1:8000",
            bearer_token=FACILITATOR_TOKEN,
        )

    client = XRPLFacilitatorClient(
        base_url="http://127.0.0.1:8000",
        bearer_token=FACILITATOR_TOKEN,
        allow_insecure_http=True,
    )
    assert client is not None


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"method": "other"}, "method"),
        ({"challengeId": "different-challenge"}, "challengeId"),
        ({"reference": "F" * 64, "txHash": "F" * 64}, "reference"),
    ],
)
def test_facilitator_client_rejects_model_valid_unbound_charge_receipt(
    override: dict[str, str],
    message: str,
) -> None:
    challenge = build_payment_challenge(
        secret=CHALLENGE_SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="charge",
        request_model=XRPLChargeRequest(
            amount="1000",
            currency="XRP",
            recipient=DESTINATION,
            methodDetails=XRPLChargeMethodDetails(network="testnet"),
        ),
        expires_in_seconds=300,
    )
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    credential = signer.build_charge_credential(challenge)
    payload = decode_charge_payload(credential)
    reference = Payment.from_xrpl(binarycodec.decode(payload.blob)).get_hash().upper()
    receipt: dict[str, object] = {
        "status": "success",
        "method": "xrpl",
        "timestamp": "2026-08-31T12:00:00Z",
        "reference": reference,
        "challengeId": challenge.id,
        "txHash": reference,
    }
    receipt.update(override)

    async_client = httpx.AsyncClient(
        base_url=FACILITATOR_URL,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=receipt, request=request)
        ),
    )
    client = XRPLFacilitatorClient(
        base_url=FACILITATOR_URL,
        bearer_token=FACILITATOR_TOKEN,
        async_client=async_client,
    )

    async def run() -> None:
        try:
            with pytest.raises(FacilitatorProtocolError, match=message):
                await client.charge(credential)
        finally:
            await async_client.aclose()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"action": "close"}, "action"),
        ({"reference": f"{'D' * 64}:125"}, "reference"),
        ({"channelId": "D" * 64}, "channelId"),
        ({"cumulative": "126"}, "cumulative"),
    ],
)
def test_facilitator_client_rejects_model_valid_unbound_session_receipt(
    override: dict[str, str],
    message: str,
) -> None:
    channel_id = "C" * 64
    challenge = build_payment_challenge(
        secret=CHALLENGE_SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="session",
        request_model=XRPLSessionRequest(
            amount="25",
            currency="XRP",
            channelId=channel_id,
            recipient=DESTINATION,
            methodDetails=XRPLSessionMethodDetails(
                network="testnet",
                cumulativeAmount="100",
            ),
        ),
        expires_in_seconds=300,
    )
    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    credential = signer.build_session_voucher_credential(challenge)
    receipt: dict[str, object] = {
        "status": "success",
        "method": "xrpl",
        "timestamp": "2026-08-31T12:00:00Z",
        "reference": f"{channel_id}:125",
        "challengeId": challenge.id,
        "channelId": channel_id,
        "cumulative": "125",
        "action": "voucher",
    }
    receipt.update(override)

    async_client = httpx.AsyncClient(
        base_url=FACILITATOR_URL,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=receipt, request=request)
        ),
    )
    client = XRPLFacilitatorClient(
        base_url=FACILITATOR_URL,
        bearer_token=FACILITATOR_TOKEN,
        async_client=async_client,
    )

    async def run() -> None:
        try:
            with pytest.raises(FacilitatorProtocolError, match=message):
                await client.session(credential)
        finally:
            await async_client.aclose()

    asyncio.run(run())


def test_settlement_pending_survives_facilitator_client_and_middleware_boundaries() -> None:
    expected_reference = ""
    charge_calls = 0
    application_calls = 0

    def facilitator_handler(request: httpx.Request) -> httpx.Response:
        nonlocal charge_calls
        if request.url.path == "/supported":
            return httpx.Response(
                200,
                json=_supported().model_dump(by_alias=True),
                request=request,
            )
        assert request.url.path == "/charge"
        charge_calls += 1
        credential_body = json.loads(request.content)["credential"]
        return httpx.Response(
            503,
            headers={
                "Content-Type": "application/problem+json",
                "Retry-After": "4",
            },
            json={
                "type": "https://paymentauth.org/problems/settlement-pending",
                "title": "Payment settlement pending",
                "status": 503,
                "detail": "The transaction is still awaiting validated settlement.",
                "challengeId": credential_body["challenge"]["id"],
                "paymentReference": expected_reference,
                "untrustedExtra": "must not cross the middleware boundary",
            },
            request=request,
        )

    upstream_http = httpx.AsyncClient(
        base_url=FACILITATOR_URL,
        transport=httpx.MockTransport(facilitator_handler),
    )
    facilitator = XRPLFacilitatorClient(
        base_url=FACILITATOR_URL,
        bearer_token=FACILITATOR_TOKEN,
        async_client=upstream_http,
    )
    app = FastAPI()

    @app.get("/paid")
    async def paid() -> dict[str, bool]:
        nonlocal application_calls
        application_calls += 1
        return {"paid": True}

    app.add_middleware(
        PaymentMiddlewareASGI,
        route_configs={"GET /paid": _charge_route()},
        client_factory=lambda _url, _token: facilitator,
        challenge_secrets=[CHALLENGE_SECRET],
    )

    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    try:
        with TestClient(app) as client:
            challenge = extract_payment_challenges(client.get("/paid").headers)[0]
            credential = signer.build_charge_credential(challenge)
            payload = decode_charge_payload(credential)
            expected_reference = Payment.from_xrpl(
                binarycodec.decode(payload.blob)
            ).get_hash().upper()
            response = client.get(
                "/paid",
                headers={"Authorization": build_payment_authorization(credential)},
            )
    finally:
        asyncio.run(upstream_http.aclose())

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["retry-after"] == "4"
    assert response.headers["cache-control"] == "private, no-store"
    assert "WWW-Authenticate" not in response.headers
    assert response.json()["paymentReference"] == expected_reference
    assert response.json()["type"].endswith("/settlement-pending")
    assert "fresh payment" in response.json()["detail"]
    assert "untrustedExtra" not in response.json()
    assert charge_calls == 1
    assert application_calls == 0


def test_ambiguous_facilitator_timeout_carries_locally_derived_payment_reference() -> None:
    application_calls = 0

    def facilitator_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/supported":
            return httpx.Response(
                200,
                json=_supported().model_dump(by_alias=True),
                request=request,
            )
        assert request.url.path == "/charge"
        raise httpx.ReadTimeout("response lost after dispatch", request=request)

    upstream_http = httpx.AsyncClient(
        base_url=FACILITATOR_URL,
        transport=httpx.MockTransport(facilitator_handler),
    )
    facilitator = XRPLFacilitatorClient(
        base_url=FACILITATOR_URL,
        bearer_token=FACILITATOR_TOKEN,
        async_client=upstream_http,
    )
    app = FastAPI()

    @app.get("/paid")
    async def paid() -> dict[str, bool]:
        nonlocal application_calls
        application_calls += 1
        return {"paid": True}

    app.add_middleware(
        PaymentMiddlewareASGI,
        route_configs={"GET /paid": _charge_route()},
        client_factory=lambda _url, _token: facilitator,
        challenge_secrets=[CHALLENGE_SECRET],
    )

    signer = XRPLPaymentSigner(Wallet.create(), network="testnet", autofill_enabled=False)
    try:
        with TestClient(app) as client:
            challenge = extract_payment_challenges(client.get("/paid").headers)[0]
            credential = signer.build_charge_credential(challenge)
            payload = decode_charge_payload(credential)
            expected_reference = Payment.from_xrpl(
                binarycodec.decode(payload.blob)
            ).get_hash().upper()
            response = client.get(
                "/paid",
                headers={"Authorization": build_payment_authorization(credential)},
            )
    finally:
        asyncio.run(upstream_http.aclose())

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["cache-control"] == "private, no-store"
    assert "WWW-Authenticate" not in response.headers
    assert response.json()["type"].endswith("/settlement-unknown")
    assert response.json()["paymentReference"] == expected_reference
    assert "Do not initiate another payment" in response.json()["detail"]
    assert application_calls == 0
