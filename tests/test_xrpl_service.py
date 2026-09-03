from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from xrpl.core import binarycodec
from xrpl.models.requests import Ledger, LedgerEntry, SubmitOnly, Tx
from xrpl.models.transactions import Payment, PaymentChannelClaim, PaymentChannelCreate
from xrpl.transaction import sign as sign_transaction
from xrpl.wallet import Wallet

from xrpl_mpp_client import XRPLPaymentSigner
from xrpl_mpp_core import (
    PaymentCredential,
    XRPLChargeMethodDetails,
    XRPLChargeRequest,
    build_payment_challenge,
    build_xrpl_did,
    challenge_invoice_id,
)
from xrpl_mpp_facilitator.config import Settings
from xrpl_mpp_facilitator.paychannel_service import PayChannelVerificationResult
from xrpl_mpp_facilitator.paychannel_store import PayChannelRecord
from xrpl_mpp_facilitator.recipient_signer import LocalSeedRecipientSigner
from xrpl_mpp_facilitator.replay_store import ReplayReservation
from xrpl_mpp_facilitator.xrpl_service import SettlementPendingError, XRPLService


SECRET = "facilitator-v02-secret"
PAYER = Wallet.create()
RECIPIENT = Wallet.create()
CHANNEL_ID = "A" * 64


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "MY_DESTINATION_ADDRESS": RECIPIENT.address,
        "NETWORK_ID": "testnet",
        "SETTLEMENT_MODE": "validated",
        "VALIDATION_TIMEOUT": 1,
        "FACILITATOR_BEARER_TOKEN": "facilitator-token",
        "REDIS_URL": "redis://fake:6379/0",
        "MPP_CHALLENGE_SECRET": SECRET,
        "MIN_XRP_DROPS": 1,
    }
    values.update(overrides)
    return Settings(**values)


class FakeReplayStore:
    def __init__(self) -> None:
        self.reservations: list[ReplayReservation] = []
        self.processed: list[ReplayReservation] = []
        self.released: list[ReplayReservation] = []
        self.guarded: list[tuple[str | None, str]] = []

    async def guard_available(self, invoice_id: str | None, blob_hash: str) -> None:
        self.guarded.append((invoice_id, blob_hash))

    async def reserve(
        self,
        invoice_id: str | None,
        blob_hash: str,
        *,
        retention_seconds: int | None,
    ) -> ReplayReservation:
        reservation = ReplayReservation(
            invoice_id=invoice_id,
            blob_hash=blob_hash,
            reservation_id=f"reservation-{len(self.reservations)}",
            retention_seconds=retention_seconds,
        )
        self.reservations.append(reservation)
        return reservation

    async def mark_processed(self, reservation: ReplayReservation) -> None:
        self.processed.append(reservation)

    async def release_pending(self, reservation: ReplayReservation) -> None:
        self.released.append(reservation)


class FakeRPC:
    def __init__(self, handler=None) -> None:
        self.requests: list[Any] = []
        self.handler = handler

    def request(self, request: Any) -> SimpleNamespace:
        self.requests.append(request)
        if self.handler is not None:
            return self.handler(request)
        if isinstance(request, SubmitOnly):
            return SimpleNamespace(result={"engine_result": "tesSUCCESS"})
        if isinstance(request, Tx):
            return SimpleNamespace(
                result={
                    "validated": True,
                    "meta": {
                        "TransactionResult": "tesSUCCESS",
                        "delivered_amount": "1000",
                    },
                }
            )
        if isinstance(request, Ledger):
            return SimpleNamespace(result={"ledger_index": 1000})
        raise AssertionError(f"unexpected request: {request!r}")


def signer() -> XRPLPaymentSigner:
    return XRPLPaymentSigner(
        PAYER,
        network="testnet",
        autofill_enabled=False,
        default_fee="12",
        default_sequence=1,
        default_last_ledger_sequence=1010,
    )


def charge_challenge(*, amount: str = "1000"):
    return build_payment_challenge(
        secret=SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="charge",
        request_model=XRPLChargeRequest(
            amount=amount,
            currency="XRP",
            recipient=RECIPIENT.address,
            methodDetails=XRPLChargeMethodDetails(network="testnet"),
        ),
        expires_in_seconds=300,
    )


def test_pull_charge_validates_binding_transaction_terms_source_and_replay() -> None:
    replay = FakeReplayStore()
    rpc = FakeRPC()
    service = XRPLService(settings(), replay_store=replay, client=rpc)
    credential = signer().build_charge_credential(charge_challenge())

    receipt = asyncio.run(service.charge(credential))

    assert receipt.status == "success"
    assert receipt.method == "xrpl"
    assert receipt.network == "testnet"
    assert receipt.payer == PAYER.address
    assert receipt.recipient == RECIPIENT.address
    assert receipt.invoice_id == challenge_invoice_id(credential.challenge.id)
    assert receipt.tx_hash == receipt.reference
    assert receipt.settlement_status == "validated"
    assert len(replay.reservations) == 1
    assert replay.processed == replay.reservations
    assert any(isinstance(request, SubmitOnly) for request in rpc.requests)


def test_charge_rejects_transaction_amount_not_bound_to_challenge() -> None:
    service = XRPLService(
        settings(),
        replay_store=FakeReplayStore(),
        client=FakeRPC(),
    )
    challenge = charge_challenge(amount="2000")
    invoice_id = challenge_invoice_id(challenge.id)
    blob = signer().sign_payment(
        pay_to=RECIPIENT.address,
        currency="XRP",
        amount="1000",
        invoice_id=invoice_id,
    )
    credential = PaymentCredential(
        challenge=challenge,
        payload={"type": "transaction", "blob": blob},
        source=build_xrpl_did(network="testnet", address=PAYER.address),
    )

    with pytest.raises(ValueError, match="amount or currency"):
        asyncio.run(service.charge(credential))


def test_pull_charge_rejects_blob_without_last_ledger_sequence() -> None:
    challenge = charge_challenge()
    invoice_id = challenge_invoice_id(challenge.id)
    unbounded_signer = XRPLPaymentSigner(
        PAYER,
        network="testnet",
        autofill_enabled=False,
        default_fee="12",
        default_sequence=1,
    )
    blob = unbounded_signer.sign_payment(
        pay_to=RECIPIENT.address,
        currency="XRP",
        amount="1000",
        invoice_id=invoice_id,
    )
    credential = PaymentCredential(
        challenge=challenge,
        payload={"type": "transaction", "blob": blob},
        source=build_xrpl_did(network="testnet", address=PAYER.address),
    )
    rpc = FakeRPC()
    service = XRPLService(
        settings(),
        replay_store=FakeReplayStore(),
        client=rpc,
    )

    with pytest.raises(ValueError, match="requires LastLedgerSequence"):
        asyncio.run(service.charge(credential))
    assert not any(isinstance(request, SubmitOnly) for request in rpc.requests)


def test_charge_rejects_tampered_challenge_before_submission() -> None:
    rpc = FakeRPC()
    service = XRPLService(
        settings(),
        replay_store=FakeReplayStore(),
        client=rpc,
    )
    credential = signer().build_charge_credential(charge_challenge())
    tampered = credential.model_copy(
        update={
            "challenge": credential.challenge.model_copy(
                update={"realm": "attacker.example"}
            )
        }
    )

    with pytest.raises(ValueError, match="binding invalid"):
        asyncio.run(service.charge(tampered))
    assert rpc.requests == []


def test_push_charge_polls_until_validated_and_checks_delivered_amount(monkeypatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "xrpl_mpp_facilitator.xrpl_service.asyncio.sleep",
        no_sleep,
    )
    challenge = charge_challenge()
    invoice_id = challenge_invoice_id(challenge.id)
    blob = signer().sign_payment(
        pay_to=RECIPIENT.address,
        currency="XRP",
        amount="1000",
        invoice_id=invoice_id,
    )
    raw = binarycodec.decode(blob)
    tx_hash = Payment.from_xrpl(raw).get_hash().upper()

    tx_calls = 0

    def handler(request: Any) -> SimpleNamespace:
        nonlocal tx_calls
        if not isinstance(request, Tx):
            return SimpleNamespace(result={"ledger_index": 1001})
        tx_calls += 1
        if tx_calls == 1:
            return SimpleNamespace(result={"error": "txnNotFound"})
        return SimpleNamespace(
            result={
                "validated": True,
                "ledger_index": 1000,
                "tx_json": {**raw, "hash": tx_hash},
                "meta": {
                    "TransactionResult": "tesSUCCESS",
                    "delivered_amount": "1000",
                },
            }
        )

    replay = FakeReplayStore()
    service = XRPLService(
        settings(SETTLEMENT_MODE="validated", VALIDATION_TIMEOUT=2),
        replay_store=replay,
        client=FakeRPC(handler),
    )
    credential = PaymentCredential(
        challenge=challenge,
        payload={"type": "hash", "hash": tx_hash},
        source=build_xrpl_did(network="testnet", address=PAYER.address),
    )

    receipt = asyncio.run(service.charge(credential))

    assert receipt.reference == tx_hash
    assert receipt.settlement_status == "validated"
    assert replay.processed == replay.reservations
    assert tx_calls == 2


def test_push_charge_rejects_node_payload_whose_hash_does_not_match_credential() -> None:
    challenge = charge_challenge()
    invoice_id = challenge_invoice_id(challenge.id)
    blob = signer().sign_payment(
        pay_to=RECIPIENT.address,
        currency="XRP",
        amount="1000",
        invoice_id=invoice_id,
    )
    raw = binarycodec.decode(blob)
    presented_hash = "E" * 64

    def handler(request: Any) -> SimpleNamespace:
        assert isinstance(request, Tx)
        return SimpleNamespace(
            result={
                "validated": True,
                "ledger_index": 1000,
                "tx_json": {**raw, "hash": presented_hash},
                "meta": {
                    "TransactionResult": "tesSUCCESS",
                    "delivered_amount": "1000",
                },
            }
        )

    service = XRPLService(
        settings(),
        replay_store=FakeReplayStore(),
        client=FakeRPC(handler),
    )
    credential = PaymentCredential(
        challenge=challenge,
        payload={"type": "hash", "hash": presented_hash},
        source=build_xrpl_did(network="testnet", address=PAYER.address),
    )

    with pytest.raises(ValueError, match="hash does not match"):
        asyncio.run(service.charge(credential))


def test_push_charge_rejects_transaction_that_predates_challenge_window() -> None:
    challenge = charge_challenge()
    invoice_id = challenge_invoice_id(challenge.id)
    blob = signer().sign_payment(
        pay_to=RECIPIENT.address,
        currency="XRP",
        amount="1000",
        invoice_id=invoice_id,
    )
    raw = binarycodec.decode(blob)
    tx_hash = Payment.from_xrpl(raw).get_hash().upper()

    def handler(request: Any) -> SimpleNamespace:
        if isinstance(request, Tx):
            return SimpleNamespace(
                result={
                    "validated": True,
                    "ledger_index": 900,
                    "tx_json": {**raw, "hash": tx_hash},
                    "meta": {
                        "TransactionResult": "tesSUCCESS",
                        "delivered_amount": "1000",
                    },
                }
            )
        return SimpleNamespace(result={"ledger_index": 1001})

    service = XRPLService(
        settings(),
        replay_store=FakeReplayStore(),
        client=FakeRPC(handler),
    )
    credential = PaymentCredential(
        challenge=challenge,
        payload={"type": "hash", "hash": tx_hash},
        source=build_xrpl_did(network="testnet", address=PAYER.address),
    )

    with pytest.raises(ValueError, match="predates the challenge"):
        asyncio.run(service.charge(credential))


def test_push_charge_validation_timeout_preserves_payment_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "xrpl_mpp_facilitator.xrpl_service.asyncio.sleep",
        no_sleep,
    )
    challenge = charge_challenge()
    invoice_id = challenge_invoice_id(challenge.id)
    blob = signer().sign_payment(
        pay_to=RECIPIENT.address,
        currency="XRP",
        amount="1000",
        invoice_id=invoice_id,
    )
    raw = binarycodec.decode(blob)
    tx_hash = Payment.from_xrpl(raw).get_hash().upper()
    replay = FakeReplayStore()

    def handler(request: Any) -> SimpleNamespace:
        assert isinstance(request, Tx)
        return SimpleNamespace(result={"error": "txnNotFound"})

    service = XRPLService(
        settings(VALIDATION_TIMEOUT=1),
        replay_store=replay,
        client=FakeRPC(handler),
    )
    credential = PaymentCredential(
        challenge=challenge,
        payload={"type": "hash", "hash": tx_hash},
        source=build_xrpl_did(network="testnet", address=PAYER.address),
    )

    with pytest.raises(SettlementPendingError) as pending:
        asyncio.run(service.charge(credential))

    assert pending.value.tx_hash == tx_hash
    assert replay.reservations == []


def test_ambiguous_pull_submission_keeps_replay_reservation_pending(monkeypatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "xrpl_mpp_facilitator.xrpl_service.asyncio.sleep",
        no_sleep,
    )

    def handler(request: Any) -> SimpleNamespace:
        if isinstance(request, Ledger):
            return SimpleNamespace(result={"ledger_index": 1000})
        if isinstance(request, SubmitOnly):
            return SimpleNamespace(result={"engine_result": "tesSUCCESS"})
        assert isinstance(request, Tx)
        return SimpleNamespace(result={"error": "txnNotFound"})

    replay = FakeReplayStore()
    service = XRPLService(
        settings(VALIDATION_TIMEOUT=1),
        replay_store=replay,
        client=FakeRPC(handler),
    )

    with pytest.raises(SettlementPendingError):
        asyncio.run(service.charge(signer().build_charge_credential(charge_challenge())))

    assert len(replay.reservations) == 1
    assert replay.processed == []
    assert replay.released == []


def test_definitive_pull_submission_rejection_releases_replay_reservation() -> None:
    def handler(request: Any) -> SimpleNamespace:
        if isinstance(request, Ledger):
            return SimpleNamespace(result={"ledger_index": 1000})
        assert isinstance(request, SubmitOnly)
        return SimpleNamespace(
            result={
                "engine_result": "temMALFORMED",
                "engine_result_message": "Malformed transaction",
            }
        )

    replay = FakeReplayStore()
    service = XRPLService(
        settings(),
        replay_store=replay,
        client=FakeRPC(handler),
    )

    with pytest.raises(ValueError, match="submission rejected"):
        asyncio.run(service.charge(signer().build_charge_credential(charge_challenge())))

    assert replay.released == replay.reservations


def test_supported_method_uses_named_network_and_canonical_currencies() -> None:
    mpt_id = "AB" * 32
    service = XRPLService(
        settings(
            ALLOWED_ISSUED_ASSETS=(
                "EUR:rPEPPER7kfTD9w2To4CQk6UCfuHM9c6GDY"
            ),
            ALLOWED_MPT_ISSUANCE_IDS=mpt_id,
        ),
        replay_store=FakeReplayStore(),
        client=FakeRPC(),
    )

    method = service.supported_methods()[0]
    assert method.network == "testnet"
    assert method.intents == ["charge"]
    assert method.currencies[0] == "XRP"
    assert any('"currency":"EUR"' in currency for currency in method.currencies)
    assert f'{{"mpt_issuance_id":"{mpt_id}"}}' in method.currencies


def test_configured_paychannel_is_wired_to_redis_and_advertised() -> None:
    service = XRPLService(
        settings(PAYCHANNEL_PAYER_PUBLIC_KEY=PAYER.public_key),
        replay_store=FakeReplayStore(),
        redis_client=object(),
        client=FakeRPC(),
    )

    assert service.supported_methods()[0].intents == ["charge", "session"]


def test_background_redemption_requires_recipient_signer() -> None:
    with pytest.raises(ValueError, match="requires a recipient signer"):
        XRPLService(
            settings(
                PAYCHANNEL_PAYER_PUBLIC_KEY=PAYER.public_key,
                PAYCHANNEL_REDEEM_INTERVAL_SECONDS=5,
            ),
            replay_store=FakeReplayStore(),
            redis_client=object(),
            client=FakeRPC(),
        )


class FakeRecipientSigner:
    def __init__(self, *, set_tf_close: bool = False) -> None:
        self.account = RECIPIENT.address
        self.set_tf_close = set_tf_close
        self.calls: list[PaymentChannelClaim] = []

    async def sign_claim(
        self,
        transaction: PaymentChannelClaim,
    ) -> PaymentChannelClaim:
        self.calls.append(transaction)
        if self.set_tf_close:
            transaction = PaymentChannelClaim.from_xrpl(
                {**transaction.to_xrpl(), "Flags": 0x00010000}
            )
        return sign_transaction(transaction, RECIPIENT)


def test_injected_recipient_signer_redeems_without_tf_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer_backend = FakeRecipientSigner()
    submitted: list[PaymentChannelClaim] = []

    def fake_autofill(transaction: PaymentChannelClaim, _client: Any) -> PaymentChannelClaim:
        return PaymentChannelClaim.from_xrpl(
            {
                **transaction.to_xrpl(),
                "Fee": "12",
                "Sequence": 1,
                "LastLedgerSequence": 1010,
            }
        )

    def fake_submit_and_wait(
        transaction: PaymentChannelClaim,
        _client: Any,
        *,
        autofill: bool,
    ) -> SimpleNamespace:
        assert autofill is False
        submitted.append(transaction)
        return SimpleNamespace(
            result={
                "validated": True,
                "hash": transaction.get_hash().upper(),
                "meta": {"TransactionResult": "tesSUCCESS"},
            }
        )

    monkeypatch.setattr("xrpl_mpp_facilitator.xrpl_service.autofill", fake_autofill)
    monkeypatch.setattr(
        "xrpl_mpp_facilitator.xrpl_service.submit_and_wait",
        fake_submit_and_wait,
    )
    service = XRPLService(
        settings(PAYCHANNEL_PAYER_PUBLIC_KEY=PAYER.public_key),
        replay_store=FakeReplayStore(),
        paychannel_service=object(),  # type: ignore[arg-type]
        client=FakeRPC(),
        recipient_signer=signer_backend,
    )
    record = PayChannelRecord(
        network="testnet",
        channel_id=CHANNEL_ID,
        payer=PAYER.address,
        recipient=RECIPIENT.address,
        funded="1000",
        cumulative="250",
        signature="AA",
        created_at=1,
        updated_at=1,
    )

    tx_hash = asyncio.run(service._settle_paychannel_claim(record=record))

    assert len(signer_backend.calls) == 1
    assert len(submitted) == 1
    assert submitted[0].to_xrpl()["Flags"] == 0
    assert not (submitted[0].to_xrpl()["Flags"] & 0x00010000)
    assert tx_hash == submitted[0].get_hash().upper()


def test_recipient_signer_cannot_add_tf_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer_backend = FakeRecipientSigner(set_tf_close=True)

    monkeypatch.setattr(
        "xrpl_mpp_facilitator.xrpl_service.autofill",
        lambda transaction, _client: PaymentChannelClaim.from_xrpl(
            {
                **transaction.to_xrpl(),
                "Fee": "12",
                "Sequence": 1,
                "LastLedgerSequence": 1010,
            }
        ),
    )
    service = XRPLService(
        settings(PAYCHANNEL_PAYER_PUBLIC_KEY=PAYER.public_key),
        replay_store=FakeReplayStore(),
        paychannel_service=object(),  # type: ignore[arg-type]
        client=FakeRPC(),
        recipient_signer=signer_backend,
    )
    record = PayChannelRecord(
        network="testnet",
        channel_id=CHANNEL_ID,
        payer=PAYER.address,
        recipient=RECIPIENT.address,
        funded="1000",
        cumulative="250",
        signature="AA",
        created_at=1,
        updated_at=1,
    )

    with pytest.raises(ValueError, match="changed the prepared"):
        asyncio.run(service._settle_paychannel_claim(record=record))


def test_local_seed_recipient_signer_is_the_config_adapter() -> None:
    adapter = LocalSeedRecipientSigner(RECIPIENT.seed)
    transaction = PaymentChannelClaim(
        account=RECIPIENT.address,
        channel=CHANNEL_ID,
        balance="250",
        amount="250",
        signature="AA",
        public_key=PAYER.public_key,
        flags=0,
        fee="12",
        sequence=1,
        last_ledger_sequence=1010,
    )

    signed = asyncio.run(adapter.sign_claim(transaction))

    assert adapter.account == RECIPIENT.address
    assert signed.is_signed()
    assert signed.to_xrpl()["Flags"] == 0


def test_validated_paychannel_ledger_check_binds_parties_key_and_funding() -> None:
    def handler(request: Any) -> SimpleNamespace:
        assert isinstance(request, LedgerEntry)
        return SimpleNamespace(
            result={
                "node": {
                    "Account": PAYER.address,
                    "Destination": RECIPIENT.address,
                    "PublicKey": PAYER.public_key,
                    "Amount": "1000",
                    "Balance": "0",
                    "SettleDelay": 3600,
                }
            }
        )

    service = XRPLService(
        settings(PAYCHANNEL_PAYER_PUBLIC_KEY=PAYER.public_key),
        replay_store=FakeReplayStore(),
        paychannel_service=object(),  # type: ignore[arg-type]
        client=FakeRPC(handler),
    )
    record = PayChannelRecord(
        network="testnet",
        channel_id=CHANNEL_ID,
        payer=PAYER.address,
        recipient=RECIPIENT.address,
        funded="1000",
        created_at=1,
        updated_at=1,
    )

    asyncio.run(service._verify_channel_ledger(record=record, cumulative="500"))


def test_open_submission_waits_for_validation_and_extracts_channel_id() -> None:
    blob = signer().sign_channel_create(
        destination=RECIPIENT.address,
        funding_amount="1000",
        settle_delay=3600,
    )

    def handler(request: Any) -> SimpleNamespace:
        if isinstance(request, SubmitOnly):
            return SimpleNamespace(result={"engine_result": "tesSUCCESS"})
        assert isinstance(request, Tx)
        return SimpleNamespace(
            result={
                "validated": True,
                "meta": {
                    "TransactionResult": "tesSUCCESS",
                    "AffectedNodes": [
                        {
                            "CreatedNode": {
                                "LedgerEntryType": "PayChannel",
                                "LedgerIndex": CHANNEL_ID,
                            }
                        }
                    ],
                },
            }
        )

    service = XRPLService(
        settings(),
        replay_store=FakeReplayStore(),
        client=FakeRPC(handler),
    )
    result = asyncio.run(
        service._submit_open_channel(
            transaction_blob=blob,
            transaction=binarycodec.decode(blob),
        )
    )

    assert result.channel_id == CHANNEL_ID
    assert len(result.tx_hash) == 64


def test_open_submission_connection_loss_preserves_transaction_reference() -> None:
    blob = signer().sign_channel_create(
        destination=RECIPIENT.address,
        funding_amount="1000",
        settle_delay=3600,
    )
    transaction = binarycodec.decode(blob)
    tx_hash = PaymentChannelCreate.from_xrpl(transaction).get_hash().upper()

    def handler(request: Any) -> SimpleNamespace:
        assert isinstance(request, SubmitOnly)
        raise OSError("connection lost after submission")

    service = XRPLService(
        settings(),
        replay_store=FakeReplayStore(),
        client=FakeRPC(handler),
    )

    with pytest.raises(SettlementPendingError) as pending:
        asyncio.run(
            service._submit_open_channel(
                transaction_blob=blob,
                transaction=transaction,
            )
        )

    assert pending.value.tx_hash == tx_hash


def test_open_submission_validation_timeout_preserves_transaction_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "xrpl_mpp_facilitator.xrpl_service.asyncio.sleep",
        no_sleep,
    )
    blob = signer().sign_channel_create(
        destination=RECIPIENT.address,
        funding_amount="1000",
        settle_delay=3600,
    )
    transaction = binarycodec.decode(blob)
    tx_hash = PaymentChannelCreate.from_xrpl(transaction).get_hash().upper()

    def handler(request: Any) -> SimpleNamespace:
        if isinstance(request, SubmitOnly):
            return SimpleNamespace(result={"engine_result": "tesSUCCESS"})
        assert isinstance(request, Tx)
        return SimpleNamespace(result={"error": "txnNotFound"})

    service = XRPLService(
        settings(VALIDATION_TIMEOUT=1),
        replay_store=FakeReplayStore(),
        client=FakeRPC(handler),
    )

    with pytest.raises(SettlementPendingError) as pending:
        asyncio.run(
            service._submit_open_channel(
                transaction_blob=blob,
                transaction=transaction,
            )
        )

    assert pending.value.tx_hash == tx_hash


class FakePayChannelService:
    async def verify(self, credential: PaymentCredential) -> PayChannelVerificationResult:
        return PayChannelVerificationResult(
            action="voucher",
            challengeId=credential.challenge.id,
            network="testnet",
            payer=PAYER.address,
            recipient=RECIPIENT.address,
            channelId=CHANNEL_ID,
            cumulative="250",
            previous="0",
            reference=f"{CHANNEL_ID}:250",
        )


def test_session_delegates_to_paychannel_and_returns_receipt_extensions() -> None:
    service = XRPLService(
        settings(),
        replay_store=FakeReplayStore(),
        paychannel_service=FakePayChannelService(),  # type: ignore[arg-type]
        client=FakeRPC(),
    )
    challenge = build_payment_challenge(
        secret=SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="session",
        request_model=XRPLChargeRequest(
            amount="250",
            currency="XRP",
            recipient=RECIPIENT.address,
        ),
    )
    credential = PaymentCredential(challenge=challenge, payload={})

    receipt = asyncio.run(service.session(credential))

    assert receipt.channel_id == CHANNEL_ID
    assert receipt.cumulative == "250"
    assert receipt.action == "voucher"
