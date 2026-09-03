from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from math import ceil
from time import time
from typing import Any, Protocol
from uuid import uuid4

from pydantic import TypeAdapter
from xrpl_mpp_core import XRPLNetwork

from xrpl_mpp_facilitator.config import Settings
from xrpl_mpp_facilitator.redis_utils import create_async_redis_client

REPLAY_PENDING = "pending"
REPLAY_PROCESSED = "processed"
REPLAY_ERROR_MESSAGE = "Transaction already processed (replay attack)"
PAYCHANNEL_CHALLENGE_PREFIX = "facilitator:replay:paychannel-challenge"
REPLAY_RETENTION_CLOCK_SKEW_SECONDS = 60
_NETWORK_ADAPTER = TypeAdapter(XRPLNetwork)


@dataclass(frozen=True)
class ReplayReservation:
    invoice_id: str | None
    blob_hash: str
    reservation_id: str
    retention_seconds: int | None


class ChallengeReplayStore(Protocol):
    async def claim_challenge(
        self,
        challenge_id: str,
        *,
        retention_seconds: int | None,
    ) -> bool:
        """Atomically claim one namespaced PayChannel challenge identifier."""
        ...


class ReplayStore(ChallengeReplayStore, Protocol):
    async def guard_available(self, invoice_id: str | None, blob_hash: str) -> None:
        ...

    async def reserve(
        self,
        invoice_id: str | None,
        blob_hash: str,
        *,
        retention_seconds: int | None,
    ) -> ReplayReservation:
        ...

    async def mark_processed(self, reservation: ReplayReservation) -> None:
        ...

    async def release_pending(self, reservation: ReplayReservation) -> None:
        ...

def _challenge_key(challenge_id: str) -> str:
    normalized = challenge_id.strip()
    if not normalized:
        raise ValueError("challenge_id is required")
    digest = sha256(normalized.encode("utf-8")).hexdigest()
    return f"{PAYCHANNEL_CHALLENGE_PREFIX}:{digest}"


def _validate_retention(retention_seconds: int | None) -> int | None:
    if retention_seconds is None:
        return None
    if (
        isinstance(retention_seconds, bool)
        or not isinstance(retention_seconds, int)
        or retention_seconds <= 0
    ):
        raise ValueError("retention_seconds must be a positive integer or None")
    return retention_seconds


def replay_retention_seconds(
    expires_iso: str | None,
    *,
    validation_timeout_seconds: int,
    now: datetime | None = None,
) -> int:
    """Derive a replay-marker lifetime from the authenticated challenge expiry.

    A marker must outlive both the remaining credential presentation window and
    a validation attempt already in flight. The skew margin covers clocks on
    separate merchant and facilitator hosts. Missing or unusable expiry data is
    rejected because no finite replay lifetime can be justified for an MPP
    charge without an authenticated presentation bound.
    """

    if not isinstance(expires_iso, str) or not expires_iso.strip():
        raise ValueError("Charge challenge carries no usable expires timestamp")
    if (
        isinstance(validation_timeout_seconds, bool)
        or not isinstance(validation_timeout_seconds, int)
        or validation_timeout_seconds <= 0
    ):
        raise ValueError("validation_timeout_seconds must be a positive integer")
    try:
        expires_at = datetime.fromisoformat(expires_iso.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Charge challenge carries no usable expires timestamp") from exc
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("Charge challenge expires timestamp must include a time zone")

    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Replay-retention clock must be timezone-aware")
    remaining_seconds = (expires_at - current).total_seconds()
    if remaining_seconds <= 0:
        raise ValueError("Charge challenge expired")
    return (
        ceil(remaining_seconds)
        + validation_timeout_seconds
        + REPLAY_RETENTION_CLOCK_SKEW_SECONDS
    )


class InMemoryChallengeReplayStore:
    """Process-local challenge claims for tests and single-process development."""

    def __init__(self, *, clock: Any = time) -> None:
        self._clock = clock
        self._challenge_claims: dict[str, float | None] = {}
        self._lock = asyncio.Lock()

    async def claim_challenge(
        self,
        challenge_id: str,
        *,
        retention_seconds: int | None,
    ) -> bool:
        key = _challenge_key(challenge_id)
        retention = _validate_retention(retention_seconds)
        now = float(self._clock())
        async with self._lock:
            expired = [
                claim_key
                for claim_key, expires_at in self._challenge_claims.items()
                if expires_at is not None and expires_at <= now
            ]
            for claim_key in expired:
                self._challenge_claims.pop(claim_key, None)
            if key in self._challenge_claims:
                return False
            self._challenge_claims[key] = (
                None if retention is None else now + retention
            )
            return True


class RedisReplayStore:
    def __init__(
        self,
        redis_client: Any,
        *,
        processed_ttl_seconds: int,
        pending_ttl_seconds: int,
        network: XRPLNetwork = "mainnet",
    ) -> None:
        self._redis = redis_client
        self._processed_ttl_seconds = processed_ttl_seconds
        self._pending_ttl_seconds = pending_ttl_seconds
        self._network = _NETWORK_ADAPTER.validate_python(network)

    def _invoice_key(self, invoice_id: str) -> str:
        return f"facilitator:replay:{self._network}:invoice:{invoice_id}"

    def _blob_key(self, blob_hash: str) -> str:
        return f"facilitator:replay:{self._network}:blob:{blob_hash}"

    @staticmethod
    def _pending_value(reservation_id: str) -> str:
        return f"{REPLAY_PENDING}:{reservation_id}"

    @staticmethod
    def _processed_value() -> str:
        return REPLAY_PROCESSED

    @staticmethod
    def _matches_pending(record: Any, reservation_id: str) -> bool:
        return record == f"{REPLAY_PENDING}:{reservation_id}"

    @staticmethod
    def _is_watch_error(exc: Exception, redis_client: Any) -> bool:
        watch_error_type = getattr(redis_client, "WatchError", None)
        if watch_error_type is not None and isinstance(exc, watch_error_type):
            return True
        try:
            from redis.exceptions import WatchError
        except ModuleNotFoundError:
            return False
        return isinstance(exc, WatchError)

    async def claim_challenge(
        self,
        challenge_id: str,
        *,
        retention_seconds: int | None,
    ) -> bool:
        key = _challenge_key(challenge_id)
        retention = _validate_retention(retention_seconds)
        while True:
            try:
                async with self._redis.pipeline() as pipe:
                    await pipe.watch(key)
                    existing = (await pipe.mget(key))[0]
                    if existing is not None:
                        return False
                    pipe.multi()
                    pipe.set(key, REPLAY_PROCESSED, ex=retention)
                    await pipe.execute()
                    return True
            except Exception as exc:
                if self._is_watch_error(exc, self._redis):
                    continue
                raise

    async def guard_available(self, invoice_id: str | None, blob_hash: str) -> None:
        keys = [self._blob_key(blob_hash)]
        if invoice_id is not None:
            keys.insert(0, self._invoice_key(invoice_id))
        values = await self._redis.mget(*keys)
        if any(value is not None for value in values):
            raise ValueError(REPLAY_ERROR_MESSAGE)

    async def reserve(
        self,
        invoice_id: str | None,
        blob_hash: str,
        *,
        retention_seconds: int | None,
    ) -> ReplayReservation:
        retention = _validate_retention(retention_seconds)
        reservation = ReplayReservation(
            invoice_id=invoice_id,
            blob_hash=blob_hash,
            reservation_id=uuid4().hex,
            retention_seconds=retention,
        )
        invoice_key = self._invoice_key(invoice_id) if invoice_id is not None else None
        blob_key = self._blob_key(blob_hash)
        pending_value = self._pending_value(reservation.reservation_id)
        pending_ttl = (
            None
            if retention is None
            else max(retention, self._pending_ttl_seconds)
        )
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
                        pipe.set(invoice_key, pending_value, ex=pending_ttl)
                    pipe.set(blob_key, pending_value, ex=pending_ttl)
                    await pipe.execute()
                    return reservation
            except ValueError:
                raise
            except Exception as exc:
                if self._is_watch_error(exc, self._redis):
                    continue
                raise

    async def mark_processed(self, reservation: ReplayReservation) -> None:
        retention = _validate_retention(reservation.retention_seconds)
        processed_ttl = (
            None
            if retention is None
            else max(retention, self._processed_ttl_seconds)
        )
        async with self._redis.pipeline() as pipe:
            pipe.multi()
            if reservation.invoice_id is not None:
                pipe.set(
                    self._invoice_key(reservation.invoice_id),
                    self._processed_value(),
                    ex=processed_ttl,
                )
            pipe.set(
                self._blob_key(reservation.blob_hash),
                self._processed_value(),
                ex=processed_ttl,
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
                if self._is_watch_error(exc, self._redis):
                    continue
                raise


def replay_pending_ttl_seconds(settings: Settings) -> int:
    # An accepted submission can become validated after the HTTP polling window.
    # Keep its reservation at least as long as a processed marker so an
    # ambiguous response can never turn into an automatic double payment.
    return max(
        settings.REPLAY_PROCESSED_TTL_SECONDS,
        settings.VALIDATION_TIMEOUT + 60,
        300,
    )


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
        network=settings.NETWORK_ID,
    )
