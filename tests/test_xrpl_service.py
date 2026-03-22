from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

import pytest

from xrpl_mpp_core import (
    AssetKey,
    NormalizedAmount,
    PaymentCredential,
    XRPLChargeMethodDetails,
    XRPLChargeRequest,
    XRPLSessionMethodDetails,
    XRPLSessionRequest,
    build_payment_challenge,
)
from xrpl_mpp_facilitator.config import Settings
from xrpl_mpp_facilitator.replay_store import ReplayReservation
from xrpl_mpp_facilitator.session_store import SessionState
from xrpl_mpp_facilitator.xrpl_service import ValidatedPayment, XRPLService

TEST_DESTINATION = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
DEFAULT_BEARER_TOKEN = "test-facilitator-token"
CHALLENGE_SECRET = "test-challenge-secret"


@dataclass
class FakePaymentTx:
    account: str
    destination: str

    def get_hash(self) -> str:
        return "ABC123HASH"


class FakeSessionStore:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.begin_open_calls: list[dict[str, object]] = []
        self.commit_open_calls: list[dict[str, object]] = []
        self.abort_open_calls: list[dict[str, object]] = []
        self.consume_calls: list[dict[str, object]] = []
        self.begin_top_up_calls: list[dict[str, object]] = []
        self.commit_top_up_calls: list[dict[str, object]] = []
        self.abort_top_up_calls: list[dict[str, object]] = []
        self.close_calls: list[dict[str, object]] = []

    async def begin_open_session(self, **kwargs) -> SessionState:
        self.events.append("begin_open_session")
        self.begin_open_calls.append(kwargs)
        return SessionState(
            session_id=str(kwargs["session_id"]),
            session_token_hash="hashed-token",
            status="pending_open",
            payer=str(kwargs["payer"]),
            recipient=str(kwargs["recipient"]),
            asset_identifier=str(kwargs["asset_identifier"]),
            network=str(kwargs["network"]),
            unit_amount=str(kwargs["unit_amount"]),
            min_prepay_amount=str(kwargs["min_prepay_amount"]),
            idle_timeout_seconds=int(kwargs["idle_timeout_seconds"]),
            prepaid_total=str(kwargs["prepaid_total"]),
            spent_total="0",
            available_balance=str(kwargs["prepaid_total"]),
            last_activity_at="2026-03-21T12:00:00Z",
            pending_action_id=str(kwargs["action_id"]),
        )

    async def commit_open_session(self, **kwargs) -> SessionState:
        self.events.append("commit_open_session")
        self.commit_open_calls.append(kwargs)
        begin_kwargs = self.begin_open_calls[-1]
        return SessionState(
            session_id=str(begin_kwargs["session_id"]),
            session_token_hash="hashed-token",
            status="open",
            payer=str(begin_kwargs["payer"]),
            recipient=str(begin_kwargs["recipient"]),
            asset_identifier=str(begin_kwargs["asset_identifier"]),
            network=str(begin_kwargs["network"]),
            unit_amount=str(begin_kwargs["unit_amount"]),
            min_prepay_amount=str(begin_kwargs["min_prepay_amount"]),
            idle_timeout_seconds=int(begin_kwargs["idle_timeout_seconds"]),
            prepaid_total=str(begin_kwargs["prepaid_total"]),
            spent_total=str(kwargs["initial_spend"]),
            available_balance=str(
                Decimal(str(begin_kwargs["prepaid_total"])) - Decimal(str(kwargs["initial_spend"]))
            ),
            last_activity_at="2026-03-21T12:00:01Z",
        )

    async def abort_open_session(self, **kwargs) -> None:
        self.events.append("abort_open_session")
        self.abort_open_calls.append(kwargs)

    async def consume(self, **kwargs) -> SessionState:
        self.events.append("consume")
        self.consume_calls.append(kwargs)
        return SessionState(
            session_id=str(kwargs["session_id"]),
            session_token_hash="hashed-token",
            status="open",
            payer="rBuyer",
            recipient=str(kwargs["recipient"]),
            asset_identifier=str(kwargs["asset_identifier"]),
            network=str(kwargs["network"]),
            unit_amount=str(kwargs["unit_amount"]),
            min_prepay_amount=str(kwargs["min_prepay_amount"]),
            idle_timeout_seconds=900,
            prepaid_total="1000",
            spent_total="500",
            available_balance="500",
            last_activity_at="2026-03-21T12:01:00Z",
        )

    async def begin_top_up(self, **kwargs) -> SessionState:
        self.events.append("begin_top_up")
        self.begin_top_up_calls.append(kwargs)
        return SessionState(
            session_id=str(kwargs["session_id"]),
            session_token_hash="hashed-token",
            status="open",
            payer="rBuyer",
            recipient=str(kwargs["recipient"]),
            asset_identifier=str(kwargs["asset_identifier"]),
            network=str(kwargs["network"]),
            unit_amount=str(kwargs["unit_amount"]),
            min_prepay_amount=str(kwargs["min_prepay_amount"]),
            idle_timeout_seconds=900,
            prepaid_total="1000",
            spent_total="250",
            available_balance="750",
            last_activity_at="2026-03-21T12:02:00Z",
            pending_action_id=str(kwargs["action_id"]),
            pending_top_up_amount=str(kwargs["amount"]),
        )

    async def commit_top_up(self, **kwargs) -> SessionState:
        self.events.append("commit_top_up")
        self.commit_top_up_calls.append(kwargs)
        begin_kwargs = self.begin_top_up_calls[-1]
        return SessionState(
            session_id=str(begin_kwargs["session_id"]),
            session_token_hash="hashed-token",
            status="open",
            payer="rBuyer",
            recipient=str(begin_kwargs["recipient"]),
            asset_identifier=str(begin_kwargs["asset_identifier"]),
            network=str(begin_kwargs["network"]),
            unit_amount=str(begin_kwargs["unit_amount"]),
            min_prepay_amount=str(begin_kwargs["min_prepay_amount"]),
            idle_timeout_seconds=900,
            prepaid_total="2000",
            spent_total="250",
            available_balance="1750",
            last_activity_at="2026-03-21T12:02:00Z",
        )

    async def abort_top_up(self, **kwargs) -> None:
        self.events.append("abort_top_up")
        self.abort_top_up_calls.append(kwargs)

    async def close_session(self, **kwargs) -> SessionState:
        self.events.append("close_session")
        self.close_calls.append(kwargs)
        return SessionState(
            session_id=str(kwargs["session_id"]),
            session_token_hash="hashed-token",
            status="closed",
            payer="rBuyer",
            recipient=str(kwargs["recipient"]),
            asset_identifier=str(kwargs["asset_identifier"]),
            network=str(kwargs["network"]),
            unit_amount=str(kwargs["unit_amount"]),
            min_prepay_amount=str(kwargs["min_prepay_amount"]),
            idle_timeout_seconds=900,
            prepaid_total="1000",
            spent_total="250",
            available_balance="750",
            last_activity_at="2026-03-21T12:03:00Z",
        )


class FakeReplayStore:
    def __init__(self) -> None:
        self.mark_processed_calls: list[ReplayReservation] = []
        self.release_pending_calls: list[ReplayReservation] = []

    async def mark_processed(self, reservation: ReplayReservation) -> None:
        self.mark_processed_calls.append(reservation)

    async def release_pending(self, reservation: ReplayReservation) -> None:
        self.release_pending_calls.append(reservation)


def build_service(
    *,
    session_store: FakeSessionStore | None = None,
    replay_store: object | None = None,
    settlement_mode: str = "validated",
) -> XRPLService:
    settings = Settings(
        _env_file=None,
        MY_DESTINATION_ADDRESS=TEST_DESTINATION,
        NETWORK_ID="xrpl:1",
        SETTLEMENT_MODE=settlement_mode,
        VALIDATION_TIMEOUT=1,
        FACILITATOR_BEARER_TOKEN=DEFAULT_BEARER_TOKEN,
        REDIS_URL="redis://fake:6379/0",
        MPP_CHALLENGE_SECRET=CHALLENGE_SECRET,
    )
    return XRPLService(
        settings,
        replay_store=replay_store or object(),
        session_store=session_store or FakeSessionStore(),
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
            recipient=TEST_DESTINATION,
            methodDetails=XRPLChargeMethodDetails(network="xrpl:1", invoiceId="A" * 64),
        ),
        expires_in_seconds=300,
    )
    return PaymentCredential(
        challenge=challenge,
        payload={"signedTxBlob": "DEADBEEF"},
    )


def build_session_credential(*, action: str, idle_timeout_seconds: int | None = None) -> PaymentCredential:
    challenge = build_payment_challenge(
        secret=CHALLENGE_SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="session",
        request_model=XRPLSessionRequest(
            amount="250",
            currency="XRP:native",
            recipient=TEST_DESTINATION,
            methodDetails=XRPLSessionMethodDetails(
                network="xrpl:1",
                sessionId="A" * 64,
                asset="XRP:native",
                unitAmount="250",
                minPrepayAmount="1000",
                idleTimeoutSeconds=idle_timeout_seconds,
            ),
        ),
        expires_in_seconds=300,
    )
    payload: dict[str, str] = {"action": action, "sessionToken": "session-token"}
    if action in {"open", "top_up"}:
        payload["signedTxBlob"] = "DEADBEEF"
    return PaymentCredential(challenge=challenge, payload=payload)


def validated_payment(
    *,
    amount_value: Decimal,
    drops: int | None = None,
    invoice_id: str = "A" * 64,
    replay_reservation: ReplayReservation | None = None,
) -> ValidatedPayment:
    return ValidatedPayment(
        signed_tx_blob="DEADBEEF",
        tx=FakePaymentTx(account="rBuyer", destination=TEST_DESTINATION),
        invoice_id=invoice_id,
        blob_hash="blob-hash",
        amount=NormalizedAmount(
            asset=AssetKey(code="XRP", issuer=None),
            value=amount_value,
            drops=drops,
        ),
        replay_reservation=replay_reservation,
    )


def test_supported_methods_advertise_charge_and_session() -> None:
    service = build_service()

    methods = service.supported_methods()

    assert len(methods) == 1
    assert methods[0].method == "xrpl"
    assert methods[0].intents == ["charge", "session"]
    assert methods[0].network == "xrpl:1"


def test_ensure_signing_address_authorized_accepts_regular_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service()

    async def _client_request(_request) -> SimpleNamespace:
        return SimpleNamespace(
            result={
                "account_data": {
                    "RegularKey": "rRegularSigner",
                    "account_flags": {"disableMasterKey": True},
                }
            }
        )

    monkeypatch.setattr(service, "_client_request", _client_request)

    asyncio.run(
        service._ensure_signing_address_authorized(
            account="rBuyer",
            signing_address="rRegularSigner",
        )
    )


def test_ensure_signing_address_authorized_rejects_disabled_master_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = build_service()

    async def _client_request(_request) -> SimpleNamespace:
        return SimpleNamespace(
            result={
                "account_data": {
                    "account_flags": {"disableMasterKey": True},
                }
            }
        )

    monkeypatch.setattr(service, "_client_request", _client_request)

    with pytest.raises(ValueError, match="not authorized"):
        asyncio.run(
            service._ensure_signing_address_authorized(
                account="rBuyer",
                signing_address="rBuyer",
            )
        )


def test_finalize_submission_accepts_queued_result_in_validated_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay_store = FakeReplayStore()
    service = build_service(replay_store=replay_store)
    reservation = ReplayReservation(
        invoice_id="A" * 64,
        blob_hash="blob-hash",
        reservation_id="reservation-1",
    )

    async def _client_request(_request) -> SimpleNamespace:
        return SimpleNamespace(
            result={
                "validated": True,
                "meta": {"delivered_amount": "1000"},
            }
        )

    monkeypatch.setattr(service, "_client_request", _client_request)

    tx_hash, settlement_status = asyncio.run(
        service._finalize_submission(
            validated_payment(
                amount_value=Decimal("1000"),
                drops=1000,
                replay_reservation=reservation,
            ),
            SimpleNamespace(
                status="success",
                result={"engine_result": "terQUEUED", "queued": True},
            ),
        )
    )

    assert (tx_hash, settlement_status) == ("ABC123HASH", "validated")
    assert replay_store.mark_processed_calls == [reservation]


def test_finalize_submission_accepts_queued_result_in_optimistic_mode() -> None:
    replay_store = FakeReplayStore()
    service = build_service(
        replay_store=replay_store,
        settlement_mode="optimistic",
    )
    reservation = ReplayReservation(
        invoice_id="A" * 64,
        blob_hash="blob-hash",
        reservation_id="reservation-1",
    )

    tx_hash, settlement_status = asyncio.run(
        service._finalize_submission(
            validated_payment(
                amount_value=Decimal("1000"),
                drops=1000,
                replay_reservation=reservation,
            ),
            SimpleNamespace(
                status="success",
                result={"engine_result": "terQUEUED", "queued": True},
            ),
        )
    )

    assert (tx_hash, settlement_status) == ("ABC123HASH", "submitted")
    assert replay_store.mark_processed_calls == [reservation]


def test_charge_builds_receipt_from_validated_payment(monkeypatch: pytest.MonkeyPatch) -> None:
    service = build_service()
    credential = build_charge_credential()

    async def _validate_payment(*_args, **_kwargs) -> ValidatedPayment:
        return validated_payment(amount_value=Decimal("1000"), drops=1000)

    async def _settle_validated_payment(_payment: ValidatedPayment) -> tuple[str, str]:
        return "ABC123HASH", "validated"

    monkeypatch.setattr(service, "_validate_payment", _validate_payment)
    monkeypatch.setattr(service, "_settle_validated_payment", _settle_validated_payment)

    receipt = asyncio.run(service.charge(credential))

    assert receipt.intent == "charge"
    assert receipt.payer == "rBuyer"
    assert receipt.recipient == TEST_DESTINATION
    assert receipt.invoice_id == "A" * 64
    assert receipt.tx_hash == "ABC123HASH"
    assert receipt.settlement_status == "validated"


def test_session_open_records_initial_spend_and_returns_session_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_store = FakeSessionStore()
    service = build_service(session_store=session_store)
    credential = build_session_credential(action="open", idle_timeout_seconds=60)

    async def _validate_payment(*_args, **_kwargs) -> ValidatedPayment:
        return validated_payment(amount_value=Decimal("1000"), drops=1000)

    async def _settle_validated_payment(_payment: ValidatedPayment) -> tuple[str, str]:
        session_store.events.append("settle")
        return "ABC123HASH", "validated"

    monkeypatch.setattr(service, "_validate_payment", _validate_payment)
    monkeypatch.setattr(service, "_settle_validated_payment", _settle_validated_payment)

    receipt = asyncio.run(service.session(credential))

    assert session_store.events == ["begin_open_session", "settle", "commit_open_session"]
    assert session_store.begin_open_calls[0]["prepaid_total"] == Decimal("1000")
    assert session_store.begin_open_calls[0]["initial_spend"] == Decimal("250")
    assert session_store.begin_open_calls[0]["idle_timeout_seconds"] == 60
    assert receipt.intent == "session"
    assert receipt.session_id == "A" * 64
    assert receipt.session_token is not None
    assert receipt.last_action == "open"
    assert receipt.tx_hash == "ABC123HASH"


def test_session_use_consumes_balance() -> None:
    session_store = FakeSessionStore()
    service = build_service(session_store=session_store)
    credential = build_session_credential(action="use")

    receipt = asyncio.run(service.session(credential))

    assert session_store.consume_calls[0]["recipient"] == TEST_DESTINATION
    assert session_store.consume_calls[0]["asset_identifier"] == "XRP:native"
    assert session_store.consume_calls[0]["network"] == "xrpl:1"
    assert session_store.consume_calls[0]["unit_amount"] == "250"
    assert session_store.consume_calls[0]["min_prepay_amount"] == "1000"
    assert session_store.consume_calls[0]["amount"] == Decimal("250")
    assert receipt.intent == "session"
    assert receipt.last_action == "use"
    assert receipt.available_balance == "500"


def test_session_top_up_records_pending_state_before_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_store = FakeSessionStore()
    service = build_service(session_store=session_store)
    credential = build_session_credential(action="top_up")
    captured: dict[str, object] = {}

    async def _validate_payment(
        signed_tx_blob: str,
        provided_invoice_id: str | None,
        replay_mode: str,
        replay_scope: str = "invoice_and_blob",
    ) -> ValidatedPayment:
        captured.update(
            signed_tx_blob=signed_tx_blob,
            provided_invoice_id=provided_invoice_id,
            replay_mode=replay_mode,
            replay_scope=replay_scope,
        )
        return validated_payment(amount_value=Decimal("1000"), drops=1000)

    async def _settle_validated_payment(_payment: ValidatedPayment) -> tuple[str, str]:
        session_store.events.append("settle")
        return "ABC123HASH", "validated"

    monkeypatch.setattr(service, "_validate_payment", _validate_payment)
    monkeypatch.setattr(service, "_settle_validated_payment", _settle_validated_payment)

    receipt = asyncio.run(service.session(credential))

    assert session_store.events == ["begin_top_up", "settle", "commit_top_up"]
    assert session_store.begin_top_up_calls[0]["recipient"] == TEST_DESTINATION
    assert session_store.begin_top_up_calls[0]["asset_identifier"] == "XRP:native"
    assert session_store.begin_top_up_calls[0]["unit_amount"] == "250"
    assert session_store.begin_top_up_calls[0]["min_prepay_amount"] == "1000"
    assert session_store.begin_top_up_calls[0]["amount"] == Decimal("1000")
    assert captured == {
        "signed_tx_blob": "DEADBEEF",
        "provided_invoice_id": "A" * 64,
        "replay_mode": "settle",
        "replay_scope": "blob_only",
    }
    assert receipt.last_action == "top_up"
    assert receipt.tx_hash == "ABC123HASH"


def test_session_open_aborts_pending_state_when_settlement_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_store = FakeSessionStore()
    service = build_service(session_store=session_store)
    credential = build_session_credential(action="open")

    async def _validate_payment(*_args, **_kwargs) -> ValidatedPayment:
        return validated_payment(amount_value=Decimal("1000"), drops=1000)

    async def _settle_validated_payment(_payment: ValidatedPayment) -> tuple[str, str]:
        session_store.events.append("settle")
        raise ValueError("XRPL unavailable")

    monkeypatch.setattr(service, "_validate_payment", _validate_payment)
    monkeypatch.setattr(service, "_settle_validated_payment", _settle_validated_payment)

    with pytest.raises(ValueError, match="XRPL unavailable"):
        asyncio.run(service.session(credential))

    assert session_store.events == ["begin_open_session", "settle", "abort_open_session"]
    assert session_store.abort_open_calls[0]["session_id"] == "A" * 64


def test_session_top_up_aborts_pending_state_when_settlement_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_store = FakeSessionStore()
    service = build_service(session_store=session_store)
    credential = build_session_credential(action="top_up")

    async def _validate_payment(*_args, **_kwargs) -> ValidatedPayment:
        return validated_payment(amount_value=Decimal("1000"), drops=1000)

    async def _settle_validated_payment(_payment: ValidatedPayment) -> tuple[str, str]:
        session_store.events.append("settle")
        raise ValueError("XRPL unavailable")

    monkeypatch.setattr(service, "_validate_payment", _validate_payment)
    monkeypatch.setattr(service, "_settle_validated_payment", _settle_validated_payment)

    with pytest.raises(ValueError, match="XRPL unavailable"):
        asyncio.run(service.session(credential))

    assert session_store.events == ["begin_top_up", "settle", "abort_top_up"]
    assert session_store.abort_top_up_calls[0]["session_id"] == "A" * 64


def test_session_close_returns_closed_receipt() -> None:
    session_store = FakeSessionStore()
    service = build_service(session_store=session_store)
    credential = build_session_credential(action="close")

    receipt = asyncio.run(service.session(credential))

    assert session_store.close_calls[0]["session_id"] == "A" * 64
    assert session_store.close_calls[0]["unit_amount"] == "250"
    assert session_store.close_calls[0]["min_prepay_amount"] == "1000"
    assert receipt.intent == "session"
    assert receipt.last_action == "close"
    assert receipt.settlement_status == "session_closed"
    assert receipt.session_token is None
