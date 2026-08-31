from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from datetime import UTC, datetime
from functools import wraps
from typing import Any

import pytest
from xrpl.core import binarycodec, keypairs
from xrpl.models.transactions import PaymentChannelCreate
from xrpl.transaction import sign
from xrpl.wallet import Wallet

from xrpl_mpp_core.did import build_xrpl_did
from xrpl_mpp_core.helpers import build_payment_challenge
from xrpl_mpp_core.models import PaymentCredential
from xrpl_mpp_core.paychannel import XRPLSessionMethodDetails, XRPLSessionRequest
from xrpl_mpp_facilitator.paychannel_service import (
    OpenChannelSubmission,
    PayChannelService,
    PayChannelVerificationError,
)
from xrpl_mpp_facilitator.paychannel_store import InMemoryPayChannelStore, PayChannelRecord
from xrpl_mpp_facilitator.replay_store import InMemoryChallengeReplayStore
from xrpl_mpp_facilitator.settlement import SettlementPendingError


SECRET = "paychannel-v02-test-secret"
NETWORK = "testnet"
CHANNEL_ID = "AB" * 32
OPEN_TX_HASH = "CD" * 32
CLOSE_TX_HASH = "EF" * 32
NOW = datetime.now(UTC)
PAYER = Wallet.create()
RECIPIENT = Wallet.create()
OTHER = Wallet.create()


def run_async(test: Callable[[], Coroutine[Any, Any, None]]) -> Callable[[], None]:
    @wraps(test)
    def wrapped() -> None:
        asyncio.run(test())

    return wrapped


def claim_signature(
    amount: str,
    *,
    channel_id: str = CHANNEL_ID,
    wallet: Wallet = PAYER,
) -> str:
    message = bytes.fromhex(
        binarycodec.encode_for_signing_claim(
            {"channel": channel_id, "amount": amount}
        )
    )
    return keypairs.sign(message, wallet.private_key)


def open_blob(
    *,
    wallet: Wallet = PAYER,
    account: str | None = None,
    recipient: str = RECIPIENT.address,
    public_key: str | None = None,
    amount: str = "1000",
    settle_delay: int = 3_600,
    last_ledger_sequence: int | None = 1_000,
    cancel_after: int | None = None,
) -> str:
    transaction = PaymentChannelCreate(
        account=account or wallet.address,
        destination=recipient,
        amount=amount,
        settle_delay=settle_delay,
        public_key=public_key or wallet.public_key,
        fee="12",
        sequence=1,
        flags=0,
        last_ledger_sequence=last_ledger_sequence,
        cancel_after=cancel_after,
    )
    signed = sign(transaction, wallet)
    return binarycodec.encode(signed.to_xrpl())


def challenge(
    *,
    channel_id: str,
    requested: str,
    cumulative: str | None,
    network: str = NETWORK,
    recipient: str = RECIPIENT.address,
    expires_in_seconds: int | None = 300,
) -> Any:
    kwargs: dict[str, Any] = {}
    if expires_in_seconds is not None:
        kwargs["expires_in_seconds"] = expires_in_seconds
    return build_payment_challenge(
        secret=SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="session",
        request_model=XRPLSessionRequest(
            amount=requested,
            currency="XRP",
            channelId=channel_id,
            recipient=recipient,
            methodDetails=XRPLSessionMethodDetails(
                network=network,
                cumulativeAmount=cumulative,
            ),
        ),
        **kwargs,
    )


def credential(*, challenge_value: Any, payload: dict[str, Any], source: str | None = None) -> PaymentCredential:
    return PaymentCredential(
        challenge=challenge_value,
        payload=payload,
        source=source
        or build_xrpl_did(network=NETWORK, address=PAYER.address),
    )


class FakeOpenSubmitter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any]]] = []

    async def __call__(
        self,
        *,
        transaction_blob: str,
        transaction: Mapping[str, Any],
    ) -> OpenChannelSubmission:
        self.calls.append((transaction_blob, transaction))
        return OpenChannelSubmission(channelId=CHANNEL_ID, txHash=OPEN_TX_HASH)


class PendingOpenSubmitter(FakeOpenSubmitter):
    async def __call__(
        self,
        *,
        transaction_blob: str,
        transaction: Mapping[str, Any],
    ) -> OpenChannelSubmission:
        self.calls.append((transaction_blob, transaction))
        raise SettlementPendingError(OPEN_TX_HASH)


class FakeLedgerVerifier:
    def __init__(
        self,
        error: Exception | None = None,
        *,
        funded: str | None = None,
    ) -> None:
        self.error = error
        self.funded = funded
        self.calls: list[tuple[PayChannelRecord, str]] = []

    async def __call__(self, *, record: PayChannelRecord, cumulative: str) -> str | None:
        self.calls.append((record, cumulative))
        if self.error is not None:
            raise self.error
        return self.funded


class FakeCloseSettler:
    def __init__(self) -> None:
        self.calls: list[PayChannelRecord] = []

    async def __call__(self, *, record: PayChannelRecord) -> str:
        self.calls.append(record)
        return CLOSE_TX_HASH


class FlakyCloseSettler(FakeCloseSettler):
    async def __call__(self, *, record: PayChannelRecord) -> str:
        self.calls.append(record)
        if len(self.calls) == 1:
            raise OSError("temporary submit failure")
        return CLOSE_TX_HASH


class FakeChannelLoader:
    def __init__(self, record: PayChannelRecord) -> None:
        self.record = record
        self.calls: list[str] = []

    async def __call__(self, *, channel_id: str) -> PayChannelRecord:
        self.calls.append(channel_id)
        return self.record


def service(
    *,
    store: InMemoryPayChannelStore,
    submitter: FakeOpenSubmitter | None = None,
    ledger_verifier: FakeLedgerVerifier | None = None,
    close_settler: FakeCloseSettler | None = None,
    channel_loader: FakeChannelLoader | None = None,
    transaction_decoder: Callable[[str], Mapping[str, Any]] | None = None,
    current_ledger_index: Callable[[], Coroutine[Any, Any, int]] | None = None,
    redemption_lease_seconds: int = 75,
) -> PayChannelService:
    async def default_current_ledger() -> int:
        return 950

    kwargs: dict[str, Any] = {}
    if transaction_decoder is not None:
        kwargs["transaction_decoder"] = transaction_decoder
    return PayChannelService(
        store=store,
        replay_store=InMemoryChallengeReplayStore(),
        challenge_secrets=[SECRET],
        network=NETWORK,
        recipient=RECIPIENT.address,
        payer_public_key=PAYER.public_key,
        open_submitter=submitter,
        ledger_verifier=ledger_verifier,
        close_settler=close_settler,
        channel_loader=channel_loader,
        current_ledger_index=current_ledger_index or default_current_ledger,
        redemption_lease_seconds=redemption_lease_seconds,
        now=lambda: NOW,
        **kwargs,
    )


def open_credential(
    *,
    blob: str | None = None,
    requested: str = "100",
    cumulative: str = "100",
    payload_extra: dict[str, Any] | None = None,
    challenge_value: Any | None = None,
    source: str | None = None,
    signature: str | None = None,
) -> PaymentCredential:
    payload: dict[str, Any] = {
        "action": "open",
        "transaction": blob or open_blob(),
        "amount": cumulative,
        "signature": signature or claim_signature(cumulative),
    }
    payload.update(payload_extra or {})
    return credential(
        challenge_value=challenge_value
        or challenge(
            channel_id="",
            requested=requested,
            cumulative="0",
        ),
        payload=payload,
        source=source,
    )


def claim_credential(
    *,
    action: str = "voucher",
    requested: str,
    prior: str,
    cumulative: str,
    channel_id: str = CHANNEL_ID,
    signature: str | None = None,
    challenge_value: Any | None = None,
) -> PaymentCredential:
    return credential(
        challenge_value=challenge_value
        or challenge(
            channel_id=channel_id,
            requested=requested,
            cumulative=prior,
        ),
        payload={
            "action": action,
            "channelId": channel_id,
            "amount": cumulative,
            "signature": signature or claim_signature(cumulative, channel_id=channel_id),
        },
    )


async def open_channel(
    *,
    store: InMemoryPayChannelStore,
    verifier: PayChannelService,
) -> None:
    result = await verifier.verify(open_credential())
    assert result.action == "open"
    record = await store.get(network=NETWORK, channel_id=CHANNEL_ID)
    assert record is not None


@run_async
async def test_open_verifies_real_transaction_and_seeds_atomic_high_water() -> None:
    store = InMemoryPayChannelStore(clock=lambda: int(NOW.timestamp() * 1_000))
    submitter = FakeOpenSubmitter()
    verifier = service(store=store, submitter=submitter)
    payment = open_credential()

    result = await verifier.verify(payment)
    assert result.action == "open"
    assert result.channel_id == CHANNEL_ID
    assert result.cumulative == "100"
    assert result.previous == "0"
    assert result.tx_hash == OPEN_TX_HASH
    assert result.reference == f"open:{CHANNEL_ID}:{OPEN_TX_HASH}"
    assert not result.replayed

    record = await store.get(network=NETWORK, channel_id=CHANNEL_ID)
    assert record is not None
    assert record.payer == PAYER.address
    assert record.recipient == RECIPIENT.address
    assert record.funded == "1000"
    assert record.cumulative == "100"
    assert record.signature == claim_signature("100")

    with pytest.raises(PayChannelVerificationError) as retried:
        await verifier.verify(payment)
    assert retried.value.code == "CHANNEL_REPLAY"
    assert len(submitter.calls) == 2


@run_async
async def test_open_preserves_typed_ambiguous_settlement_reference() -> None:
    store = InMemoryPayChannelStore(clock=lambda: int(NOW.timestamp() * 1_000))
    submitter = PendingOpenSubmitter()
    verifier = service(store=store, submitter=submitter)

    with pytest.raises(SettlementPendingError) as pending:
        await verifier.verify(open_credential())

    assert pending.value.tx_hash == OPEN_TX_HASH
    assert len(submitter.calls) == 1
    assert await store.get(network=NETWORK, channel_id=CHANNEL_ID) is None


@run_async
async def test_zero_open_allows_placeholder_claim_and_missing_expiry() -> None:
    store = InMemoryPayChannelStore(clock=lambda: int(NOW.timestamp() * 1_000))
    verifier = service(store=store, submitter=FakeOpenSubmitter())
    no_expiry = build_payment_challenge(
        secret=SECRET,
        realm="merchant.example",
        method="xrpl",
        intent="session",
        request_model=XRPLSessionRequest(
            amount="0",
            currency="XRP",
            channelId="",
            recipient=RECIPIENT.address,
            methodDetails=XRPLSessionMethodDetails(
                network=NETWORK,
                cumulativeAmount="0",
            ),
        ),
    )
    placeholder_signature = claim_signature("0", channel_id="00" * 32)

    result = await verifier.verify(
        open_credential(
            requested="0",
            cumulative="0",
            signature=placeholder_signature,
            challenge_value=no_expiry,
        )
    )
    assert result.cumulative == "0"
    record = await store.get(network=NETWORK, channel_id=CHANNEL_ID)
    assert record is not None
    assert record.signature == ""


@run_async
async def test_amounts_are_bounded_to_xrpl_uint64_before_crypto_or_submission() -> None:
    store = InMemoryPayChannelStore()
    submitter = FakeOpenSubmitter()
    verifier = service(store=store, submitter=submitter)
    too_large = str(1 << 64)

    with pytest.raises(PayChannelVerificationError) as caught:
        await verifier.verify(
            open_credential(
                requested=too_large,
                cumulative=too_large,
                signature="00",
            )
        )
    assert caught.value.code == "INVALID_AMOUNT"
    assert submitter.calls == []


@run_async
async def test_voucher_is_signature_checked_and_exact_retry_is_rejected() -> None:
    store = InMemoryPayChannelStore(clock=lambda: int(NOW.timestamp() * 1_000))
    submitter = FakeOpenSubmitter()
    ledger = FakeLedgerVerifier()
    verifier = service(store=store, submitter=submitter, ledger_verifier=ledger)
    await open_channel(store=store, verifier=verifier)

    payment = claim_credential(requested="50", prior="100", cumulative="150")
    accepted = await verifier.verify(payment)
    with pytest.raises(PayChannelVerificationError) as replayed:
        await verifier.verify(payment)

    assert accepted.action == "voucher"
    assert accepted.previous == "100"
    assert accepted.cumulative == "150"
    assert not accepted.replayed
    assert replayed.value.code == "CHANNEL_REPLAY"
    assert [cumulative for _, cumulative in ledger.calls] == ["150", "150"]


@run_async
async def test_same_challenge_cannot_authorize_a_later_higher_cumulative_claim() -> None:
    store = InMemoryPayChannelStore(clock=lambda: int(NOW.timestamp() * 1_000))
    verifier = service(
        store=store,
        submitter=FakeOpenSubmitter(),
        ledger_verifier=FakeLedgerVerifier(),
    )
    await open_channel(store=store, verifier=verifier)
    reused_challenge = challenge(
        channel_id=CHANNEL_ID,
        requested="50",
        cumulative="100",
    )

    accepted = await verifier.verify(
        claim_credential(
            requested="50",
            prior="100",
            cumulative="150",
            challenge_value=reused_challenge,
        )
    )
    with pytest.raises(PayChannelVerificationError) as replayed:
        await verifier.verify(
            claim_credential(
                requested="50",
                prior="100",
                cumulative="200",
                challenge_value=reused_challenge,
            )
        )

    assert accepted.cumulative == "150"
    assert replayed.value.code == "CHANNEL_REPLAY"
    record = await store.get(network=NETWORK, channel_id=CHANNEL_ID)
    assert record is not None
    assert record.cumulative == "150"


@run_async
async def test_invalid_proof_does_not_burn_the_bound_challenge() -> None:
    store = InMemoryPayChannelStore(clock=lambda: int(NOW.timestamp() * 1_000))
    verifier = service(
        store=store,
        submitter=FakeOpenSubmitter(),
        ledger_verifier=FakeLedgerVerifier(),
    )
    await open_channel(store=store, verifier=verifier)
    reusable_challenge = challenge(
        channel_id=CHANNEL_ID,
        requested="50",
        cumulative="100",
    )

    with pytest.raises(PayChannelVerificationError) as invalid:
        await verifier.verify(
            claim_credential(
                requested="50",
                prior="100",
                cumulative="150",
                signature="00",
                challenge_value=reusable_challenge,
            )
        )
    accepted = await verifier.verify(
        claim_credential(
            requested="50",
            prior="100",
            cumulative="150",
            challenge_value=reusable_challenge,
        )
    )

    assert invalid.value.code == "INVALID_SIGNATURE"
    assert accepted.cumulative == "150"


@run_async
async def test_atomic_store_rejects_short_regressed_and_exhausted_claims() -> None:
    store = InMemoryPayChannelStore(clock=lambda: int(NOW.timestamp() * 1_000))
    verifier = service(store=store, submitter=FakeOpenSubmitter())
    await open_channel(store=store, verifier=verifier)

    cases = [
        (
            claim_credential(requested="50", prior="100", cumulative="149"),
            "SHORT_PAYMENT",
        ),
        (
            claim_credential(requested="1", prior="100", cumulative="99"),
            "CUMULATIVE_REGRESSION",
        ),
        (
            claim_credential(requested="1", prior="100", cumulative="1001"),
            "CHANNEL_EXHAUSTED",
        ),
    ]
    for payment, code in cases:
        with pytest.raises(PayChannelVerificationError) as caught:
            await verifier.verify(payment)
        assert caught.value.code == code

    record = await store.get(network=NETWORK, channel_id=CHANNEL_ID)
    assert record is not None
    assert record.cumulative == "100"


@run_async
async def test_close_is_final_voucher_and_only_settler_finalizes_and_marks_redeemed() -> None:
    store = InMemoryPayChannelStore(clock=lambda: int(NOW.timestamp() * 1_000))
    submitter = FakeOpenSubmitter()
    closer = FakeCloseSettler()
    verifier = service(store=store, submitter=submitter, close_settler=closer)
    await open_channel(store=store, verifier=verifier)

    payment = claim_credential(
        action="close",
        requested="25",
        prior="100",
        cumulative="125",
    )
    closed = await verifier.verify(payment)
    assert closed.action == "close"
    assert closed.finalized
    assert closed.tx_hash == CLOSE_TX_HASH

    record = await store.get(network=NETWORK, channel_id=CHANNEL_ID)
    assert record is not None
    assert record.cumulative == "125"
    assert record.redeemed == "125"
    assert record.finalized

    with pytest.raises(PayChannelVerificationError) as replayed:
        await verifier.verify(payment)
    assert replayed.value.code == "CHANNEL_FINALIZED"
    assert len(closer.calls) == 1
    assert await store.claim_redemption(
        network=NETWORK,
        channel_id=CHANNEL_ID,
        lease_seconds=75,
    ) is not None


@run_async
async def test_on_ledger_funding_increase_updates_durable_limit_atomically() -> None:
    store = InMemoryPayChannelStore(clock=lambda: int(NOW.timestamp() * 1_000))
    ledger = FakeLedgerVerifier(funded="2000")
    verifier = service(
        store=store,
        submitter=FakeOpenSubmitter(),
        ledger_verifier=ledger,
    )
    await open_channel(store=store, verifier=verifier)

    result = await verifier.verify(
        claim_credential(
            requested="1100",
            prior="100",
            cumulative="1200",
        )
    )

    assert result.cumulative == "1200"
    record = await store.get(network=NETWORK, channel_id=CHANNEL_ID)
    assert record is not None
    assert record.funded == "2000"


@run_async
async def test_first_voucher_imports_an_out_of_band_validated_channel() -> None:
    store = InMemoryPayChannelStore(clock=lambda: int(NOW.timestamp() * 1_000))
    timestamp = int(NOW.timestamp() * 1_000)
    loader = FakeChannelLoader(
        PayChannelRecord(
            network=NETWORK,
            channel_id=CHANNEL_ID,
            payer=PAYER.address,
            recipient=RECIPIENT.address,
            funded="1000",
            cumulative="100",
            signature="00",
            redeemed="100",
            created_at=timestamp,
            updated_at=timestamp,
            redeemed_at=timestamp,
            redemption_reference="ledger-import",
        )
    )
    verifier = service(
        store=store,
        channel_loader=loader,
        ledger_verifier=FakeLedgerVerifier(funded="1000"),
    )

    result = await verifier.verify(
        claim_credential(
            requested="25",
            prior="100",
            cumulative="125",
        )
    )

    assert result.previous == "100"
    assert result.cumulative == "125"
    assert loader.calls == [CHANNEL_ID]


@run_async
async def test_close_without_settlement_hook_finalizes_unredeemed_voucher() -> None:
    store = InMemoryPayChannelStore(clock=lambda: int(NOW.timestamp() * 1_000))
    verifier = service(store=store, submitter=FakeOpenSubmitter())
    await open_channel(store=store, verifier=verifier)

    result = await verifier.verify(
        claim_credential(
            action="close",
            requested="25",
            prior="100",
            cumulative="125",
        )
    )
    assert result.finalized
    record = await store.get(network=NETWORK, channel_id=CHANNEL_ID)
    assert record is not None
    assert record.cumulative == "125"
    assert record.redeemed == "0"
    assert record.finalized

    with pytest.raises(PayChannelVerificationError) as rejected:
        await verifier.verify(
            claim_credential(
                requested="1",
                prior="125",
                cumulative="126",
            )
        )
    assert rejected.value.code == "CHANNEL_FINALIZED"


@run_async
async def test_close_settlement_failure_remains_retryable_until_redeemed() -> None:
    clock = [int(NOW.timestamp() * 1_000)]
    store = InMemoryPayChannelStore(clock=lambda: clock[0])
    closer = FlakyCloseSettler()
    verifier = service(
        store=store,
        submitter=FakeOpenSubmitter(),
        close_settler=closer,
        redemption_lease_seconds=1,
    )
    await open_channel(store=store, verifier=verifier)

    with pytest.raises(PayChannelVerificationError) as failed:
        await verifier.verify(
            claim_credential(
                action="close",
                requested="25",
                prior="100",
                cumulative="125",
            )
        )
    assert failed.value.code == "CLOSE_SETTLEMENT_FAILED"
    pending = await store.get(network=NETWORK, channel_id=CHANNEL_ID)
    assert pending is not None
    assert pending.cumulative == "125"
    assert not pending.finalized

    with pytest.raises(SettlementPendingError) as settling:
        await verifier.verify(
            claim_credential(
                action="close",
                requested="25",
                prior="100",
                cumulative="125",
            )
        )
    assert settling.value.tx_hash == f"{CHANNEL_ID}:125"
    assert len(closer.calls) == 1

    clock[0] += 1_001
    retried = await verifier.verify(
        claim_credential(
            action="close",
            requested="25",
            prior="100",
            cumulative="125",
        )
    )
    assert retried.finalized
    assert retried.replayed
    assert retried.tx_hash == CLOSE_TX_HASH


@run_async
async def test_close_waits_for_background_redemption_lease_without_resubmitting() -> None:
    store = InMemoryPayChannelStore(clock=lambda: int(NOW.timestamp() * 1_000))
    closer = FakeCloseSettler()
    verifier = service(
        store=store,
        submitter=FakeOpenSubmitter(),
        close_settler=closer,
    )
    await open_channel(store=store, verifier=verifier)
    worker_lease = await store.claim_redemption(
        network=NETWORK,
        channel_id=CHANNEL_ID,
        lease_seconds=75,
    )
    assert worker_lease is not None

    with pytest.raises(SettlementPendingError) as pending:
        await verifier.verify(
            claim_credential(
                action="close",
                requested="25",
                prior="100",
                cumulative="125",
            )
        )

    assert pending.value.tx_hash == f"{CHANNEL_ID}:125"
    assert closer.calls == []
    record = await store.get(network=NETWORK, channel_id=CHANNEL_ID)
    assert record is not None
    assert record.cumulative == "125"
    assert not record.finalized


@run_async
async def test_close_reloads_state_after_worker_finishes_before_lease_claim() -> None:
    class WorkerWinsRaceStore(InMemoryPayChannelStore):
        async def claim_redemption(
            self,
            *,
            network: Any,
            channel_id: str,
            lease_seconds: int,
        ) -> str | None:
            current = await self.get(network=network, channel_id=channel_id)
            assert current is not None
            await self.mark_redeemed(
                network=network,
                channel_id=channel_id,
                cumulative=current.cumulative,
                reference=CLOSE_TX_HASH,
            )
            return await super().claim_redemption(
                network=network,
                channel_id=channel_id,
                lease_seconds=lease_seconds,
            )

    store = WorkerWinsRaceStore(clock=lambda: int(NOW.timestamp() * 1_000))
    closer = FakeCloseSettler()
    verifier = service(
        store=store,
        submitter=FakeOpenSubmitter(),
        close_settler=closer,
    )
    await open_channel(store=store, verifier=verifier)

    result = await verifier.verify(
        claim_credential(
            action="close",
            requested="25",
            prior="100",
            cumulative="125",
        )
    )

    assert result.finalized
    assert result.tx_hash == CLOSE_TX_HASH
    assert closer.calls == []


@run_async
async def test_close_does_not_finalize_a_concurrently_newer_voucher() -> None:
    class VoucherWinsFinalizeRaceStore(InMemoryPayChannelStore):
        injected = False

        async def finalize(
            self,
            *,
            network: Any,
            channel_id: str,
            reason: str,
            expected_cumulative: str | None = None,
            timestamp: int | None = None,
        ) -> PayChannelRecord:
            if not self.injected:
                self.injected = True
                await super().advance(
                    network=network,
                    channel_id=channel_id,
                    cumulative="150",
                    requested="25",
                    signature=claim_signature("150"),
                    timestamp=timestamp,
                )
            return await super().finalize(
                network=network,
                channel_id=channel_id,
                reason=reason,
                expected_cumulative=expected_cumulative,
                timestamp=timestamp,
            )

    store = VoucherWinsFinalizeRaceStore(
        clock=lambda: int(NOW.timestamp() * 1_000)
    )
    closer = FakeCloseSettler()
    verifier = service(
        store=store,
        submitter=FakeOpenSubmitter(),
        close_settler=closer,
    )
    await open_channel(store=store, verifier=verifier)

    with pytest.raises(PayChannelVerificationError) as changed:
        await verifier.verify(
            claim_credential(
                action="close",
                requested="25",
                prior="100",
                cumulative="125",
            )
        )

    assert changed.value.code == "CHANNEL_STATE_CHANGED"
    record = await store.get(network=NETWORK, channel_id=CHANNEL_ID)
    assert record is not None
    assert record.cumulative == "150"
    assert record.redeemed == "125"
    assert not record.finalized


@run_async
async def test_challenge_binding_expiry_source_and_exact_payload_fail_closed() -> None:
    store = InMemoryPayChannelStore()
    verifier = service(store=store, submitter=FakeOpenSubmitter())

    valid_challenge = challenge(channel_id="", requested="100", cumulative="0")
    tampered = valid_challenge.model_copy(update={"realm": "attacker.example"})
    expired = challenge(
        channel_id="",
        requested="100",
        cumulative="0",
        expires_in_seconds=-3600,
    )
    wrong_network_source = build_xrpl_did(network="mainnet", address=PAYER.address)
    wrong_address_source = build_xrpl_did(network=NETWORK, address=OTHER.address)
    wrong_network_challenge = challenge(
        channel_id="",
        requested="100",
        cumulative="0",
        network="mainnet",
    )

    cases = [
        (open_credential(challenge_value=tampered), "INVALID_CHALLENGE_BINDING"),
        (open_credential(challenge_value=expired), "CHALLENGE_EXPIRED"),
        (open_credential(source=wrong_network_source), "INVALID_SOURCE"),
        (open_credential(source=wrong_address_source), "SOURCE_MISMATCH"),
        (open_credential(challenge_value=wrong_network_challenge), "NETWORK_MISMATCH"),
        (open_credential(payload_extra={"unexpected": True}), "INVALID_SESSION_PAYLOAD"),
    ]
    for payment, code in cases:
        with pytest.raises(PayChannelVerificationError) as caught:
            await verifier.verify(payment)
        assert caught.value.code == code


@run_async
async def test_open_transaction_party_key_funding_signature_and_delay_invariants() -> None:
    valid_blob = open_blob()
    decoded = binarycodec.decode(valid_blob)
    invalid_funding = dict(decoded)
    invalid_funding["Amount"] = {
        "currency": "USD",
        "issuer": OTHER.address,
        "value": "1",
    }
    invalid_signature = dict(decoded)
    invalid_signature["TxnSignature"] = "00" * 64

    cases: list[tuple[str, Callable[[str], Mapping[str, Any]] | None, str]] = [
        (
            open_blob(wallet=OTHER, public_key=PAYER.public_key),
            None,
            "SOURCE_MISMATCH",
        ),
        (
            open_blob(recipient=OTHER.address),
            None,
            "RECIPIENT_MISMATCH",
        ),
        (
            open_blob(public_key=OTHER.public_key),
            None,
            "PUBLIC_KEY_MISMATCH",
        ),
        (
            valid_blob,
            lambda _blob: invalid_funding,
            "INVALID_FUNDING",
        ),
        (
            valid_blob,
            lambda _blob: invalid_signature,
            "INVALID_OPEN_SIGNATURE",
        ),
        (
            open_blob(settle_delay=60),
            None,
            "SETTLE_DELAY_TOO_SHORT",
        ),
    ]

    for blob, decoder, code in cases:
        store = InMemoryPayChannelStore()
        verifier = service(
            store=store,
            submitter=FakeOpenSubmitter(),
            transaction_decoder=decoder,
        )
        with pytest.raises(PayChannelVerificationError) as caught:
            await verifier.verify(open_credential(blob=blob))
        assert caught.value.code == code


@run_async
async def test_claim_signature_channel_binding_and_ledger_hook_fail_closed() -> None:
    store = InMemoryPayChannelStore(clock=lambda: int(NOW.timestamp() * 1_000))
    ledger = FakeLedgerVerifier(RuntimeError("channel closing"))
    verifier = service(
        store=store,
        submitter=FakeOpenSubmitter(),
        ledger_verifier=ledger,
    )
    await open_channel(store=store, verifier=verifier)

    invalid_signature = claim_credential(
        requested="50",
        prior="100",
        cumulative="150",
        signature=claim_signature("150", wallet=OTHER),
    )
    with pytest.raises(PayChannelVerificationError) as signature_error:
        await verifier.verify(invalid_signature)
    assert signature_error.value.code == "INVALID_SIGNATURE"
    assert ledger.calls == []

    valid = claim_credential(requested="50", prior="100", cumulative="150")
    with pytest.raises(PayChannelVerificationError) as ledger_error:
        await verifier.verify(valid)
    assert ledger_error.value.code == "CHANNEL_LEDGER_CHECK_FAILED"

    mismatched_channel = credential(
        challenge_value=challenge(
            channel_id="12" * 32,
            requested="50",
            cumulative="100",
        ),
        payload={
            "action": "voucher",
            "channelId": CHANNEL_ID,
            "amount": "150",
            "signature": claim_signature("150"),
        },
    )
    with pytest.raises(PayChannelVerificationError) as channel_error:
        await verifier.verify(mismatched_channel)
    assert channel_error.value.code == "CHANNEL_MISMATCH"


@run_async
async def test_last_ledger_sequence_cannot_outlive_bound_challenge() -> None:
    async def current_ledger() -> int:
        return 100

    store = InMemoryPayChannelStore()
    verifier = service(
        store=store,
        submitter=FakeOpenSubmitter(),
        current_ledger_index=current_ledger,
    )
    with pytest.raises(PayChannelVerificationError) as caught:
        await verifier.verify(open_credential(blob=open_blob(last_ledger_sequence=10_000)))
    assert caught.value.code == "OPEN_TRANSACTION_OUTLIVES_CHALLENGE"


@run_async
async def test_expiring_open_requires_last_ledger_sequence() -> None:
    store = InMemoryPayChannelStore()
    verifier = service(store=store, submitter=FakeOpenSubmitter())

    with pytest.raises(PayChannelVerificationError) as caught:
        await verifier.verify(
            open_credential(blob=open_blob(last_ledger_sequence=None))
        )
    assert caught.value.code == "OPEN_TRANSACTION_UNBOUNDED"


@run_async
async def test_unexpiring_open_still_requires_fresh_last_ledger_sequence() -> None:
    store = InMemoryPayChannelStore()
    submitter = FakeOpenSubmitter()
    verifier = service(store=store, submitter=submitter)
    no_expiry = challenge(
        channel_id="",
        requested="100",
        cumulative="0",
        expires_in_seconds=None,
    )

    cases = (
        (None, "OPEN_TRANSACTION_UNBOUNDED"),
        (950, "OPEN_TRANSACTION_EXPIRED"),
    )
    for last_ledger_sequence, code in cases:
        with pytest.raises(PayChannelVerificationError) as caught:
            await verifier.verify(
                open_credential(
                    blob=open_blob(last_ledger_sequence=last_ledger_sequence),
                    challenge_value=no_expiry,
                )
            )
        assert caught.value.code == code

    assert submitter.calls == []


@run_async
async def test_open_rejects_cancel_after_inside_settlement_margin() -> None:
    store = InMemoryPayChannelStore()
    verifier = service(store=store, submitter=FakeOpenSubmitter())
    ripple_now = int(NOW.timestamp()) - 946_684_800

    with pytest.raises(PayChannelVerificationError) as caught:
        await verifier.verify(
            open_credential(blob=open_blob(cancel_after=ripple_now + 30))
        )
    assert caught.value.code == "CHANNEL_CLOSING"


@run_async
async def test_concurrent_equal_vouchers_credit_once_and_reject_replays() -> None:
    store = InMemoryPayChannelStore(clock=lambda: int(NOW.timestamp() * 1_000))
    verifier = service(store=store, submitter=FakeOpenSubmitter())
    await open_channel(store=store, verifier=verifier)
    payment = claim_credential(requested="50", prior="100", cumulative="150")

    results = await asyncio.gather(
        *(verifier.verify(payment) for _ in range(20)),
        return_exceptions=True,
    )
    accepted = [result for result in results if not isinstance(result, Exception)]
    rejected = [result for result in results if isinstance(result, PayChannelVerificationError)]
    assert len(accepted) == 1
    assert len(rejected) == 19
    assert all(result.code == "CHANNEL_REPLAY" for result in rejected)
    record = await store.get(network=NETWORK, channel_id=CHANNEL_ID)
    assert record is not None
    assert record.cumulative == "150"
