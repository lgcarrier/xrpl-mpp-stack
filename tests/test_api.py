from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from slowapi import Limiter as SlowLimiter

import xrpl_mpp_facilitator.factory as factory_module
from xrpl_mpp_core import (
    FacilitatorSupportedMethod,
    PaymentCredential,
    PaymentReceipt,
    XRPLChargeMethodDetails,
    XRPLChargeRequest,
    XRPLSessionMethodDetails,
    XRPLSessionRequest,
    build_payment_challenge,
)
from xrpl_mpp_facilitator import __version__ as facilitator_version
from xrpl_mpp_facilitator.config import Settings
from xrpl_mpp_facilitator.factory import create_app
from xrpl_mpp_facilitator.xrpl_service import SettlementPendingError


BEARER_TOKEN = "test-facilitator-token"
CHALLENGE_SECRET = "test-challenge-secret"
DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
CHANNEL_ID = "A" * 64


def build_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "MY_DESTINATION_ADDRESS": DESTINATION,
        "NETWORK_ID": "testnet",
        "FACILITATOR_BEARER_TOKEN": BEARER_TOKEN,
        "REDIS_URL": "redis://fake:6379/0",
        "MPP_CHALLENGE_SECRET": CHALLENGE_SECRET,
    }
    values.update(overrides)
    return Settings(**values)


def build_charge_credential() -> PaymentCredential:
    challenge = build_payment_challenge(
        secret=CHALLENGE_SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="charge",
        request_model=XRPLChargeRequest(
            amount="1000",
            currency="XRP",
            recipient=DESTINATION,
            methodDetails=XRPLChargeMethodDetails(
                network="testnet",
                invoiceId="B" * 64,
            ),
        ),
        expires_in_seconds=300,
    )
    return PaymentCredential(
        challenge=challenge,
        payload={"type": "transaction", "blob": "DEADBEEF"},
    )


def build_session_credential() -> PaymentCredential:
    challenge = build_payment_challenge(
        secret=CHALLENGE_SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="session",
        request_model=XRPLSessionRequest(
            amount="250",
            currency="XRP",
            channelId=CHANNEL_ID,
            recipient=DESTINATION,
            methodDetails=XRPLSessionMethodDetails(
                network="testnet",
                cumulativeAmount="0",
            ),
        ),
        expires_in_seconds=300,
    )
    return PaymentCredential(
        challenge=challenge,
        payload={
            "action": "voucher",
            "channelId": CHANNEL_ID,
            "amount": "250",
            "signature": "DEADBEEF",
        },
    )


class FakeXRPLService:
    def __init__(
        self,
        *,
        charge_error: ValueError | None = None,
        session_error: ValueError | None = None,
    ) -> None:
        self.charge_error = charge_error
        self.session_error = session_error
        self.charge_calls: list[PaymentCredential] = []
        self.session_calls: list[PaymentCredential] = []
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    def supported_methods(self) -> list[FacilitatorSupportedMethod]:
        return [
            FacilitatorSupportedMethod(
                method="xrpl",
                intents=["charge", "session"],
                network="testnet",
                currencies=["XRP"],
                settlementMode="validated",
            )
        ]

    async def charge(self, credential: PaymentCredential) -> PaymentReceipt:
        self.charge_calls.append(credential)
        if self.charge_error is not None:
            raise self.charge_error
        return PaymentReceipt(
            status="success",
            method="xrpl",
            timestamp="2026-08-30T12:00:00Z",
            reference="C" * 64,
            challengeId=credential.challenge.id,
            network="testnet",
            recipient=DESTINATION,
            invoiceId="B" * 64,
            txHash="C" * 64,
            settlementStatus="validated",
        )

    async def session(self, credential: PaymentCredential) -> PaymentReceipt:
        self.session_calls.append(credential)
        if self.session_error is not None:
            raise self.session_error
        return PaymentReceipt(
            status="success",
            method="xrpl",
            timestamp="2026-08-30T12:00:00Z",
            reference=f"{CHANNEL_ID}:250",
            challengeId=credential.challenge.id,
            network="testnet",
            recipient=DESTINATION,
            channelId=CHANNEL_ID,
            cumulative="250",
            action="voucher",
        )

    async def aclose(self) -> None:
        self.closed = True


def create_test_app(settings: Settings, service: FakeXRPLService):
    original = factory_module.build_rate_limiter
    factory_module.build_rate_limiter = lambda _settings: SlowLimiter(
        key_func=factory_module.get_remote_address
    )
    try:
        return create_app(app_settings=settings, xrpl_service=service)
    finally:
        factory_module.build_rate_limiter = original


def build_client(
    *,
    service: FakeXRPLService | None = None,
    token: str | None = BEARER_TOKEN,
    **settings_overrides: object,
) -> tuple[TestClient, FakeXRPLService]:
    active_service = service or FakeXRPLService()
    client = TestClient(
        create_test_app(build_settings(**settings_overrides), active_service)
    )
    if token is not None:
        client.headers["Authorization"] = f"Bearer {token}"
    return client, active_service


def credential_body(credential: PaymentCredential) -> dict[str, object]:
    return {
        "credential": credential.model_dump(by_alias=True, exclude_none=True)
    }


def test_health_and_supported_use_v02_contracts() -> None:
    client, _ = build_client()

    assert client.get("/health").json() == {
        "status": "healthy",
        "network": "testnet",
    }
    assert client.get("/supported").json() == {
        "methods": [
            {
                "method": "xrpl",
                "intents": ["charge", "session"],
                "network": "testnet",
                "currencies": ["XRP"],
                "settlementMode": "validated",
            }
        ]
    }


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_routes_disabled_by_default(path: str) -> None:
    client, _ = build_client()
    assert client.get(path).status_code == 404


def test_openapi_uses_package_version_when_enabled() -> None:
    client, _ = build_client(ENABLE_API_DOCS=True)
    assert client.get("/openapi.json").json()["info"]["version"] == facilitator_version


def test_build_rate_limiter_uses_configured_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, object] = {}

    class RecordingLimiter:
        def __init__(self, **kwargs: object) -> None:
            recorded.update(kwargs)
            self._storage = SimpleNamespace(check=lambda: True)

    monkeypatch.setattr(factory_module, "Limiter", RecordingLimiter)
    limiter = factory_module.build_rate_limiter(build_settings())

    assert isinstance(limiter, RecordingLimiter)
    assert recorded["storage_uri"] == "redis://fake:6379/0"
    assert recorded["key_prefix"] == factory_module.RATE_LIMIT_STORAGE_KEY_PREFIX


def test_create_app_rejects_unhealthy_rate_limit_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnhealthyLimiter:
        def __init__(self, **_kwargs: object) -> None:
            self._storage = SimpleNamespace(check=lambda: False)

    monkeypatch.setattr(factory_module, "Limiter", UnhealthyLimiter)
    with pytest.raises(RuntimeError, match="storage is unavailable"):
        create_app(
            app_settings=build_settings(),
            xrpl_service=FakeXRPLService(),
        )


@pytest.mark.parametrize(
    ("endpoint", "credential"),
    [("/charge", build_charge_credential()), ("/session", build_session_credential())],
)
def test_payment_routes_require_gateway_bearer_auth(
    endpoint: str,
    credential: PaymentCredential,
) -> None:
    client, _ = build_client(token=None)
    response = client.post(endpoint, json=credential_body(credential))

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_charge_and_session_return_minimal_extensible_receipts() -> None:
    client, service = build_client()

    charge = client.post("/charge", json=credential_body(build_charge_credential()))
    session = client.post("/session", json=credential_body(build_session_credential()))

    assert charge.status_code == 200
    assert charge.json()["status"] == "success"
    assert charge.json()["settlementStatus"] == "validated"
    assert session.status_code == 200
    assert session.json()["channelId"] == CHANNEL_ID
    assert session.json()["action"] == "voucher"
    assert len(service.charge_calls) == 1
    assert len(service.session_calls) == 1


def test_payment_failure_returns_mpp_problem_details() -> None:
    client, _ = build_client(
        service=FakeXRPLService(charge_error=ValueError("invalid payment"))
    )
    credential = build_charge_credential()
    response = client.post("/charge", json=credential_body(credential))

    assert response.status_code == 402
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "https://paymentauth.org/problems/verification-failed",
        "title": "Payment verification failed",
        "status": 402,
        "detail": "invalid payment",
        "challengeId": credential.challenge.id,
    }


def test_ambiguous_submission_returns_retryable_non_receipt_problem() -> None:
    tx_hash = "D" * 64
    client, _ = build_client(
        service=FakeXRPLService(
            charge_error=SettlementPendingError(tx_hash),  # type: ignore[arg-type]
        )
    )
    credential = build_charge_credential()
    response = client.post("/charge", json=credential_body(credential))

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["retry-after"] == "4"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["paymentReference"] == tx_hash
    assert "Payment-Receipt" not in response.headers


def test_ambiguous_channel_open_returns_retryable_non_receipt_problem() -> None:
    tx_hash = "E" * 64
    client, _ = build_client(
        service=FakeXRPLService(
            session_error=SettlementPendingError(tx_hash),  # type: ignore[arg-type]
        )
    )
    credential = build_session_credential()
    response = client.post("/session", json=credential_body(credential))

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["retry-after"] == "4"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["paymentReference"] == tx_hash
    assert response.json()["challengeId"] == credential.challenge.id
    assert "Payment-Receipt" not in response.headers


def test_lifespan_closes_injected_service() -> None:
    client, service = build_client()
    with client:
        assert client.get("/health").status_code == 200
        assert service.started is True
    assert service.closed is True
