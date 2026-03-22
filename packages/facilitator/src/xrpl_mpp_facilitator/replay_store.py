from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from xrpl_mpp_facilitator.config import Settings
from xrpl_mpp_facilitator.redis_utils import create_async_redis_client

REPLAY_PENDING = "pending"
REPLAY_PROCESSED = "processed"
REPLAY_ERROR_MESSAGE = "Transaction already processed (replay attack)"


@dataclass(frozen=True)
class ReplayReservation:
    invoice_id: str | None
    blob_hash: str
    reservation_id: str


class ReplayStore(Protocol):
    async def guard_available(self, invoice_id: str | None, blob_hash: str) -> None:
        ...

    async def reserve(self, invoice_id: str | None, blob_hash: str) -> ReplayReservation:
        ...

    async def mark_processed(self, reservation: ReplayReservation) -> None:
        ...

    async def release_pending(self, reservation: ReplayReservation) -> None:
        ...


class RedisReplayStore:
    def __init__(
        self,
        redis_client: Any,
        *,
        processed_ttl_seconds: int,
        pending_ttl_seconds: int,
    ) -> None:
        self._redis = redis_client
        self._processed_ttl_seconds = processed_ttl_seconds
        self._pending_ttl_seconds = pending_ttl_seconds

    @staticmethod
    def _invoice_key(invoice_id: str) -> str:
        return f"facilitator:replay:invoice:{invoice_id}"

    @staticmethod
    def _blob_key(blob_hash: str) -> str:
        return f"facilitator:replay:blob:{blob_hash}"

    @staticmethod
    def _pending_value(reservation_id: str) -> str:
        return f"{REPLAY_PENDING}:{reservation_id}"

    @staticmethod
    def _processed_value() -> str:
        return REPLAY_PROCESSED

    @staticmethod
    def _matches_pending(record: Any, reservation_id: str) -> bool:
        return record == f"{REPLAY_PENDING}:{reservation_id}"

    async def guard_available(self, invoice_id: str | None, blob_hash: str) -> None:
        keys = [self._blob_key(blob_hash)]
        if invoice_id is not None:
            keys.insert(0, self._invoice_key(invoice_id))
        values = await self._redis.mget(*keys)
        if any(value is not None for value in values):
            raise ValueError(REPLAY_ERROR_MESSAGE)

    async def reserve(self, invoice_id: str | None, blob_hash: str) -> ReplayReservation:
        reservation = ReplayReservation(
            invoice_id=invoice_id,
            blob_hash=blob_hash,
            reservation_id=uuid4().hex,
        )
        invoice_key = self._invoice_key(invoice_id) if invoice_id is not None else None
        blob_key = self._blob_key(blob_hash)
        pending_value = self._pending_value(reservation.reservation_id)
        watched_keys = tuple(
            key
            for key in (invoice_key, blob_key)
            if key is not None
        )

        while True:
            try:
                async with self._redis.pipeline() as pipe:
                    await pipe.watch(*watched_keys)
                    existing_values = await pipe.mget(*watched_keys)
                    if any(value is not None for value in existing_values):
                        raise ValueError(REPLAY_ERROR_MESSAGE)
                    pipe.multi()
                    if invoice_key is not None:
                        pipe.set(invoice_key, pending_value, ex=self._pending_ttl_seconds)
                    pipe.set(blob_key, pending_value, ex=self._pending_ttl_seconds)
                    await pipe.execute()
                    return reservation
            except ValueError:
                raise
            except Exception as exc:
                watch_error_type = getattr(self._redis, "WatchError", None)
                if watch_error_type is not None and isinstance(exc, watch_error_type):
                    continue
                try:
                    from redis.exceptions import WatchError
                except ModuleNotFoundError:
                    raise
                if isinstance(exc, WatchError):
                    continue
                raise

    async def mark_processed(self, reservation: ReplayReservation) -> None:
        async with self._redis.pipeline() as pipe:
            pipe.multi()
            if reservation.invoice_id is not None:
                pipe.set(
                    self._invoice_key(reservation.invoice_id),
                    self._processed_value(),
                    ex=self._processed_ttl_seconds,
                )
            pipe.set(
                self._blob_key(reservation.blob_hash),
                self._processed_value(),
                ex=self._processed_ttl_seconds,
            )
            await pipe.execute()

    async def release_pending(self, reservation: ReplayReservation) -> None:
        invoice_key = self._invoice_key(reservation.invoice_id) if reservation.invoice_id is not None else None
        blob_key = self._blob_key(reservation.blob_hash)
        pending_value = self._pending_value(reservation.reservation_id)
        watched_keys = tuple(
            key
            for key in (invoice_key, blob_key)
            if key is not None
        )

        while True:
            try:
                async with self._redis.pipeline() as pipe:
                    await pipe.watch(*watched_keys)
                    current_values = await pipe.mget(*watched_keys)
                    current_invoice = current_values[0] if invoice_key is not None else None
                    current_blob = current_values[-1]
                    pipe.multi()
                    if invoice_key is not None and self._matches_pending(current_invoice, reservation.reservation_id):
                        pipe.delete(invoice_key)
                    if self._matches_pending(current_blob, reservation.reservation_id):
                        pipe.delete(blob_key)
                    await pipe.execute()
                    return
            except Exception as exc:
                watch_error_type = getattr(self._redis, "WatchError", None)
                if watch_error_type is not None and isinstance(exc, watch_error_type):
                    continue
                try:
                    from redis.exceptions import WatchError
                except ModuleNotFoundError:
                    raise
                if isinstance(exc, WatchError):
                    continue
                raise


def replay_pending_ttl_seconds(settings: Settings) -> int:
    return max(settings.VALIDATION_TIMEOUT + 60, 300)


def build_replay_store(
    settings: Settings,
    redis_client: Any | None = None,
) -> ReplayStore:
    pending_ttl_seconds = replay_pending_ttl_seconds(settings)
    if redis_client is None:
        redis_client = create_async_redis_client(settings.REDIS_URL.get_secret_value())

    return RedisReplayStore(
        redis_client,
        processed_ttl_seconds=settings.REPLAY_PROCESSED_TTL_SECONDS,
        pending_ttl_seconds=pending_ttl_seconds,
    )
