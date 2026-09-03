from __future__ import annotations

import asyncio

from xrpl_mpp_facilitator.paychannel_store import (
    InMemoryPayChannelStore,
    PayChannelRecord,
)
from xrpl_mpp_facilitator.paychannel_worker import PayChannelRedemptionWorker


PAYER = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
RECIPIENT = "rf5kMNrUqgLzJT8YUzxM1pptc5r3Lfx1J9"
SIGNATURE = "AB" * 64
TX_HASH = "CD" * 32


def _record(
    *,
    channel_id: str,
    updated_at: int,
    cumulative: str = "100",
    redeemed: str = "0",
    finalized: bool = False,
) -> PayChannelRecord:
    return PayChannelRecord(
        network="testnet",
        channel_id=channel_id,
        payer=PAYER,
        recipient=RECIPIENT,
        funded="1000",
        cumulative=cumulative,
        signature=SIGNATURE if int(cumulative) else "",
        redeemed=redeemed,
        created_at=1,
        updated_at=updated_at,
        finalized=finalized,
        finalized_at=updated_at if finalized else None,
        finalized_reason="closed" if finalized else None,
        redeemed_at=updated_at if int(redeemed) else None,
        redemption_reference="prior" if int(redeemed) else None,
    )


class FakeSettler:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[PayChannelRecord] = []

    async def __call__(self, *, record: PayChannelRecord) -> str:
        self.calls.append(record)
        if self.fail:
            raise OSError("ambiguous submission")
        return TX_HASH


def test_worker_redeems_active_claim_and_idle_finalizes_session() -> None:
    now = [120_000]
    store = InMemoryPayChannelStore(clock=lambda: now[0])
    active_id = "AA" * 32
    idle_id = "BB" * 32

    async def _run() -> None:
        await store.create(_record(channel_id=active_id, updated_at=110_000))
        await store.create(_record(channel_id=idle_id, updated_at=1_000))
        settler = FakeSettler()
        worker = PayChannelRedemptionWorker(
            store=store,
            settler=settler,
            network="testnet",
            interval_seconds=5,
            idle_close_seconds=60,
            batch_size=10,
            lease_seconds=75,
            clock=lambda: now[0],
        )

        result = await worker.run_once()
        active = await store.get(network="testnet", channel_id=active_id)
        idle = await store.get(network="testnet", channel_id=idle_id)

        assert result.inspected == 2
        assert result.redeemed == 2
        assert result.finalized == 1
        assert active is not None and active.redeemed == "100"
        assert not active.finalized
        assert idle is not None and idle.redeemed == "100"
        assert idle.finalized
        assert idle.finalized_reason == "idle-close"
        assert len(settler.calls) == 2

    asyncio.run(_run())


def test_worker_redeems_already_finalized_off_ledger_voucher() -> None:
    now = 120_000
    store = InMemoryPayChannelStore(clock=lambda: now)
    channel_id = "CC" * 32

    async def _run() -> None:
        await store.create(
            _record(
                channel_id=channel_id,
                updated_at=10_000,
                finalized=True,
            )
        )
        worker = PayChannelRedemptionWorker(
            store=store,
            settler=FakeSettler(),
            network="testnet",
            interval_seconds=5,
            batch_size=10,
            lease_seconds=75,
            clock=lambda: now,
        )

        result = await worker.run_once()
        record = await store.get(network="testnet", channel_id=channel_id)

        assert result.redeemed == 1
        assert result.finalized == 0
        assert record is not None and record.finalized
        assert record.redeemed == "100"

    asyncio.run(_run())


def test_worker_holds_lease_after_ambiguous_failure_before_retry() -> None:
    now = [120_000]
    store = InMemoryPayChannelStore(clock=lambda: now[0])
    channel_id = "DD" * 32

    async def _run() -> None:
        await store.create(_record(channel_id=channel_id, updated_at=110_000))
        settler = FakeSettler(fail=True)
        worker = PayChannelRedemptionWorker(
            store=store,
            settler=settler,
            network="testnet",
            interval_seconds=5,
            batch_size=10,
            lease_seconds=75,
            clock=lambda: now[0],
        )

        first = await worker.run_once()
        second = await worker.run_once()
        now[0] += 76_000
        third = await worker.run_once()

        assert first.failed == 1
        assert second.leased_elsewhere == 1
        assert third.failed == 1
        assert len(settler.calls) == 2
        record = await store.get(network="testnet", channel_id=channel_id)
        assert record is not None and record.redeemed == "0"

    asyncio.run(_run())


def test_idle_worker_does_not_finalize_a_concurrently_newer_voucher() -> None:
    class VoucherWinsFinalizeRaceStore(InMemoryPayChannelStore):
        injected = False

        async def finalize(
            self,
            *,
            network: str,
            channel_id: str,
            reason: str,
            expected_cumulative: str | None = None,
            timestamp: int | None = None,
        ) -> PayChannelRecord:
            if not self.injected:
                self.injected = True
                await super().advance(
                    network=network,  # type: ignore[arg-type]
                    channel_id=channel_id,
                    cumulative="125",
                    requested="25",
                    signature="CD" * 64,
                    timestamp=timestamp,
                )
            return await super().finalize(
                network=network,  # type: ignore[arg-type]
                channel_id=channel_id,
                reason=reason,
                expected_cumulative=expected_cumulative,
                timestamp=timestamp,
            )

    now = 120_000
    store = VoucherWinsFinalizeRaceStore(clock=lambda: now)
    channel_id = "EE" * 32

    async def _run() -> None:
        await store.create(_record(channel_id=channel_id, updated_at=1_000))
        worker = PayChannelRedemptionWorker(
            store=store,
            settler=FakeSettler(),
            network="testnet",
            interval_seconds=5,
            idle_close_seconds=60,
            batch_size=10,
            lease_seconds=75,
            clock=lambda: now,
        )

        result = await worker.run_once()
        record = await store.get(network="testnet", channel_id=channel_id)

        assert result.redeemed == 1
        assert result.finalized == 0
        assert result.failed == 1
        assert record is not None
        assert record.cumulative == "125"
        assert record.redeemed == "100"
        assert not record.finalized

    asyncio.run(_run())
