from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from slowapi import Limiter as SlowLimiter

import xrpl_mpp_facilitator.factory as factory_module
from xrpl_mpp_facilitator import __version__ as facilitator_version
from xrpl_mpp_core import (
    FacilitatorSupportedMethod,
    PaymentCredential,
    PaymentReceipt,
    XRPLAsset,
    XRPLChargeMethodDetails,
    XRPLChargeRequest,
    XRPLSessionMethodDetails,
    XRPLSessionRequest,
    build_payment_challenge,
)
from xrpl_mpp_facilitator.config import Settings
from xrpl_mpp_facilitator.factory import create_app

DEFAULT_BEARER_TOKEN = "test-facilitator-token"
CHALLENGE_SECRET = "test-challenge-secret"
DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"


class FakeXRPLService:
    def __init__(
        self,
        *,
        charge_receipt: PaymentReceipt | None = None,
        session_receipt: PaymentReceipt | None = None,
        charge_error: Exception | None = None,
        session_error: Exception | None = None,
    ) -> None:
        self.charge_receipt = charge_receipt or build_charge_receipt()
        self.session_receipt = session_receipt or build_session_receipt()
        self.charge_error = charge_error
        self.session_error = session_error
        self.charge_calls: list[PaymentCredential] = []
        self.session_calls: list[PaymentCredential] = []

    def supported_methods(self) -> list[FacilitatorSupportedMethod]:
        return [
            FacilitatorSupportedMethod(
                method="xrpl",
                intents=["charge", "session"],
                network="xrpl:1",
                assets=[XRPLAsset(code="XRP")],
                settlementMode="validated",
            )
        ]

    async def charge(self, credential: PaymentCredential) -> PaymentReceipt:
        self.charge_calls.append(credential)
        if self.charge_error is not None:
            raise self.charge_error
        return self.charge_receipt.model_copy(update={"challengeId": credential.challenge.id})

    async def session(self, credential: PaymentCredential) -> PaymentReceipt:
        self.session_calls.append(credential)
        if self.session_error is not None:
            raise self.session_error
        return self.session_receipt.model_copy(update={"challengeId": credential.challenge.id})


def build_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "_env_file": None,
        "MY_DESTINATION_ADDRESS": DESTINATION,
        "NETWORK_ID": "xrpl:1",
        "FACILITATOR_BEARER_TOKEN": DEFAULT_BEARER_TOKEN,
        "REDIS_URL": "redis://fake:6379/0",
        "MPP_CHALLENGE_SECRET": CHALLENGE_SECRET,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def create_app_with_in_memory_rate_limiter(
    *,
    app_settings: Settings,
    xrpl_service: FakeXRPLService | None = None,
):
    original_build_rate_limiter = factory_module.build_rate_limiter

    def _build_in_memory_rate_limiter(_settings: Settings):
        return SlowLimiter(key_func=factory_module.get_remote_address)

    factory_module.build_rate_limiter = _build_in_memory_rate_limiter
    try:
        return create_app(
            app_settings=app_settings,
            xrpl_service=xrpl_service,
        )
    finally:
        factory_module.build_rate_limiter = original_build_rate_limiter


def build_client(
    *,
    service: FakeXRPLService | None = None,
    authorization_token: str | None = DEFAULT_BEARER_TOKEN,
    **settings_overrides: object,
) -> TestClient:
    settings = build_settings(**settings_overrides)
    client = TestClient(
        create_app_with_in_memory_rate_limiter(
            app_settings=settings,
            xrpl_service=service or FakeXRPLService(),
        )
    )
    if authorization_token is not None:
        client.headers.update({"Authorization": f"Bearer {authorization_token}"})
    return client


def build_charge_receipt() -> PaymentReceipt:
    return PaymentReceipt(
        method="xrpl",
        timestamp="2026-03-21T12:00:00Z",
        reference="ABC123HASH",
        challengeId="challenge-id",
        intent="charge",
        network="xrpl:1",
        payer="rBuyer",
        recipient=DESTINATION,
        invoiceId="A" * 64,
        txHash="ABC123HASH",
        settlementStatus="validated",
        asset={"code": "XRP"},
        amount={"value": "1000", "unit": "drops", "asset": {"code": "XRP"}, "drops": 1000},
    )


def build_session_receipt() -> PaymentReceipt:
    return PaymentReceipt(
        method="xrpl",
        timestamp="2026-03-21T12:00:00Z",
        reference="session-123",
        challengeId="challenge-id",
        intent="session",
        network="xrpl:1",
        payer="rBuyer",
        recipient=DESTINATION,
        sessionId="A" * 64,
        sessionToken="session-token",
        settlementStatus="session_open",
        asset={"code": "XRP"},
        amount={"value": "250", "unit": "drops", "asset": {"code": "XRP"}, "drops": 250},
        availableBalance="750",
        prepaidTotal="1000",
        spentTotal="250",
        lastAction="open",
    )


def build_charge_body() -> dict[str, object]:
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
    credential = PaymentCredential(challenge=challenge, payload={"signedTxBlob": "DEADBEEF"})
    return {"credential": credential.model_dump(by_alias=True, exclude_none=True)}


def build_session_body(*, action: str = "use") -> dict[str, object]:
    challenge = build_payment_challenge(
        secret=CHALLENGE_SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="session",
        request_model=XRPLSessionRequest(
            amount="250",
            currency="XRP:native",
            recipient=DESTINATION,
            methodDetails=XRPLSessionMethodDetails(
                network="xrpl:1",
                sessionId="A" * 64,
                asset="XRP:native",
                unitAmount="250",
                minPrepayAmount="1000",
            ),
        ),
        expires_in_seconds=300,
    )
    payload = {"action": action, "sessionToken": "session-token"}
    if action in {"open", "top_up"}:
        payload["signedTxBlob"] = "DEADBEEF"
    credential = PaymentCredential(challenge=challenge, payload=payload)
    return {"credential": credential.model_dump(by_alias=True, exclude_none=True)}


def test_health_reports_network() -> None:
    client = build_client()

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "network": "xrpl:1"}


def test_supported_reports_current_method_shape() -> None:
    client = build_client()

    response = client.get("/supported")

    assert response.status_code == 200
    assert response.json() == {
        "methods": [
            {
                "method": "xrpl",
                "intents": ["charge", "session"],
                "network": "xrpl:1",
                "assets": [{"code": "XRP", "issuer": None}],
                "settlementMode": "validated",
            }
        ]
    }


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_routes_disabled_by_default(path: str) -> None:
    client = build_client()

    response = client.get(path)

    assert response.status_code == 404


def test_openapi_reports_current_package_version_when_docs_are_enabled() -> None:
    client = build_client(ENABLE_API_DOCS=True)

    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["version"] == facilitator_version


def test_build_rate_limiter_uses_redis_storage_when_redis_url_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_kwargs: dict[str, object] = {}

    class RecordingLimiter:
        def __init__(self, **kwargs: object) -> None:
            recorded_kwargs.update(kwargs)
            self._storage = SimpleNamespace(check=lambda: True)

    monkeypatch.setattr(factory_module, "Limiter", RecordingLimiter)

    limiter = factory_module.build_rate_limiter(
        build_settings(REDIS_URL="redis://redis:6379/0")
    )

    assert isinstance(limiter, RecordingLimiter)
    assert recorded_kwargs == {
        "key_func": factory_module.get_remote_address,
        "storage_uri": "redis://redis:6379/0",
        "key_prefix": factory_module.RATE_LIMIT_STORAGE_KEY_PREFIX,
    }


def test_create_app_rejects_unhealthy_redis_backed_rate_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnhealthyLimiter:
        def __init__(self, **_kwargs: object) -> None:
            self._storage = SimpleNamespace(check=lambda: False)

    monkeypatch.setattr(factory_module, "Limiter", UnhealthyLimiter)

    with pytest.raises(RuntimeError, match="rate limiter storage is unavailable"):
        create_app(
            app_settings=build_settings(REDIS_URL="redis://redis:6379/0"),
            xrpl_service=FakeXRPLService(),
        )


@pytest.mark.parametrize("endpoint,body", [("/charge", build_charge_body()), ("/session", build_session_body())])
def test_payment_routes_require_bearer_auth(endpoint: str, body: dict[str, object]) -> None:
    client = build_client(authorization_token=None)

    response = client.post(endpoint, json=body)

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid authentication credentials"}
    assert response.headers["www-authenticate"] == "Bearer"


def test_charge_endpoint_returns_receipt() -> None:
    service = FakeXRPLService()
    client = build_client(service=service)

    response = client.post("/charge", json=build_charge_body())

    assert response.status_code == 200
    assert response.json()["intent"] == "charge"
    assert response.json()["txHash"] == "ABC123HASH"
    assert len(service.charge_calls) == 1


def test_session_endpoint_returns_receipt() -> None:
    service = FakeXRPLService()
    client = build_client(service=service)

    response = client.post("/session", json=build_session_body())

    assert response.status_code == 200
    assert response.json()["intent"] == "session"
    assert response.json()["sessionToken"] == "session-token"
    assert len(service.session_calls) == 1


def test_charge_endpoint_translates_service_value_error_to_402() -> None:
    service = FakeXRPLService(charge_error=ValueError("invalid payment"))
    client = build_client(service=service)

    response = client.post("/charge", json=build_charge_body())

    assert response.status_code == 402
    assert response.json() == {"detail": "invalid payment"}
