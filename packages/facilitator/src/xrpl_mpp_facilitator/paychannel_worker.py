from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from time import time
from typing import Protocol, TypeAlias

import structlog

from xrpl_mpp_core.xrpl import XRPLNetwork
from xrpl_mpp_facilitator.paychannel_store import (
    PayChannelRecord,
    PayChannelStore,
)


logger = structlog.get_logger()
Clock: TypeAlias = Callable[[], int]


def _now_ms() -> int:
    return int(time() * 1_000)


class RecipientClaimSettler(Protocol):
    async def __call__(self, *, record: PayChannelRecord) -> str:
        ...


@dataclass(frozen=True, slots=True)
class RedemptionSweepResult:
    inspected: int = 0
    redeemed: int = 0
    finalized: int = 0
    leased_elsewhere: int = 0
    failed: int = 0


class PayChannelRedemptionWorker:
    """Bounded Redis-coordinated recipient redemption and idle finalization."""

    def __init__(
        self,
        *,
        store: PayChannelStore,
        settler: RecipientClaimSettler,
        network: XRPLNetwork,
        interval_seconds: int,
        idle_close_seconds: int = 0,
        batch_size: int = 100,
        lease_seconds: int = 60,
        clock: Clock = _now_ms,
    ) -> None:
        for name, value, allow_zero in (
            ("interval_seconds", interval_seconds, False),
            ("idle_close_seconds", idle_close_seconds, True),
            ("batch_size", batch_size, False),
            ("lease_seconds", lease_seconds, False),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < (0 if allow_zero else 1)
            ):
                qualifier = "non-negative" if allow_zero else "positive"
                raise ValueError(f"{name} must be a {qualifier} integer")
        self._store = store
        self._settler = settler
        self._network = network
        self._interval_seconds = interval_seconds
        self._idle_close_ms = idle_close_seconds * 1_000
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._clock = clock
        self._cursor = 0

    def _is_idle(self, record: PayChannelRecord, now_ms: int) -> bool:
        return (
            not record.finalized
            and self._idle_close_ms > 0
            and record.updated_at <= now_ms - self._idle_close_ms
        )

    async def run_once(self) -> RedemptionSweepResult:
        page = await self._store.scan(
            network=self._network,
            cursor=self._cursor,
            limit=self._batch_size,
        )
        self._cursor = page.cursor
        now_ms = self._clock()
        redeemed = finalized = leased_elsewhere = failed = 0

        for record in page.records:
            idle = self._is_idle(record, now_ms)
            needs_redemption = int(record.cumulative) > int(record.redeemed)
            if not needs_redemption:
                if idle:
                    try:
                        await self._store.finalize(
                            network=self._network,
                            channel_id=record.channel_id,
                            reason="idle-close",
                            expected_cumulative=record.cumulative,
                            timestamp=now_ms,
                        )
                    except Exception as exc:
                        failed += 1
                        logger.warning(
                            "paychannel_idle_finalize_failed",
                            channel_id=record.channel_id,
                            error=str(exc),
                        )
                    else:
                        finalized += 1
                continue

            lease_id = await self._store.claim_redemption(
                network=self._network,
                channel_id=record.channel_id,
                lease_seconds=self._lease_seconds,
            )
            if lease_id is None:
                leased_elsewhere += 1
                continue
            try:
                tx_hash = await self._settler(record=record)
                await self._store.mark_redeemed(
                    network=self._network,
                    channel_id=record.channel_id,
                    cumulative=record.cumulative,
                    reference=tx_hash,
                    timestamp=now_ms,
                )
                redeemed += 1
                if idle:
                    await self._store.finalize(
                        network=self._network,
                        channel_id=record.channel_id,
                        reason="idle-close",
                        expected_cumulative=record.cumulative,
                        timestamp=now_ms,
                    )
                    finalized += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Submission may have reached rippled. Retain the lease until
                # expiry so replicas do not immediately submit duplicate claims;
                # the same cumulative claim cannot redeem value twice.
                failed += 1
                logger.warning(
                    "paychannel_redemption_failed",
                    channel_id=record.channel_id,
                    error=str(exc),
                )
            else:
                await self._store.release_redemption(
                    network=self._network,
                    channel_id=record.channel_id,
                    lease_id=lease_id,
                )

        return RedemptionSweepResult(
            inspected=len(page.records),
            redeemed=redeemed,
            finalized=finalized,
            leased_elsewhere=leased_elsewhere,
            failed=failed,
        )

    async def run_forever(self) -> None:
        while True:
            try:
                result = await self.run_once()
                if result.inspected:
                    logger.info(
                        "paychannel_redemption_sweep",
                        inspected=result.inspected,
                        redeemed=result.redeemed,
                        finalized=result.finalized,
                        leased_elsewhere=result.leased_elsewhere,
                        failed=result.failed,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("paychannel_redemption_sweep_failed", error=str(exc))
            await asyncio.sleep(self._interval_seconds)
