from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from time import time
from typing import Any, Literal, Protocol, TypeAlias, runtime_checkable
from uuid import uuid4

from pydantic import Field, TypeAdapter, ValidationError, field_validator, model_validator

from xrpl_mpp_core.paychannel import (
    ChannelId,
    Drops,
    PayChannelHighWater,
    XRPLChannelVoucherPayload,
    evaluate_high_water,
)
from xrpl_mpp_core.xrpl import ClassicAddress, XRPLModel, XRPLNetwork


AdvanceStatus: TypeAlias = Literal["advanced", "replay", "regressed", "short"]


class PayChannelRecord(XRPLModel):
    """Durable state required to validate and redeem one XRPL PayChannel."""

    network: XRPLNetwork
    channel_id: ChannelId
    payer: ClassicAddress
    recipient: ClassicAddress
    funded: Drops
    cumulative: Drops = "0"
    signature: str = Field(default="", max_length=144, pattern=r"^[0-9A-Fa-f]*$")
    redeemed: Drops = "0"
    created_at: int = Field(ge=0)
    updated_at: int = Field(ge=0)
    finalized: bool = False
    finalized_at: int | None = Field(default=None, ge=0)
    finalized_reason: str | None = Field(default=None, max_length=256)
    redeemed_at: int | None = Field(default=None, ge=0)
    redemption_reference: str | None = Field(default=None, max_length=512)

    @field_validator("channel_id")
    @classmethod
    def _normalize_channel_id(cls, value: str) -> str:
        return value.upper()

    @field_validator("signature")
    @classmethod
    def _normalize_signature(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _validate_accounting(self) -> "PayChannelRecord":
        funded = int(self.funded)
        cumulative = int(self.cumulative)
        redeemed = int(self.redeemed)
        if funded <= 0:
            raise ValueError("PayChannel funded amount must be greater than zero")
        if cumulative > funded:
            raise ValueError("PayChannel cumulative amount cannot exceed funded amount")
        if redeemed > cumulative:
            raise ValueError("PayChannel redeemed amount cannot exceed cumulative amount")
        if cumulative > 0 and not self.signature:
            raise ValueError("PayChannel cumulative amount requires a claim signature")
        if self.updated_at < self.created_at:
            raise ValueError("PayChannel updated_at cannot precede created_at")
        if self.finalized != (self.finalized_at is not None):
            raise ValueError("PayChannel finalized and finalized_at must be set together")
        if self.finalized_reason is not None and not self.finalized:
            raise ValueError("PayChannel finalized_reason requires a finalized channel")
        if self.finalized_at is not None and not (
            self.created_at <= self.finalized_at <= self.updated_at
        ):
            raise ValueError("PayChannel finalized_at must fall within the record lifetime")
        if redeemed > 0 and self.redeemed_at is None:
            raise ValueError("PayChannel redeemed amount requires redeemed_at")
        if self.redemption_reference is not None and self.redeemed_at is None:
            raise ValueError("PayChannel redemption_reference requires redeemed_at")
        if self.redeemed_at is not None and not (
            self.created_at <= self.redeemed_at <= self.updated_at
        ):
            raise ValueError("PayChannel redeemed_at must fall within the record lifetime")
        return self

    @property
    def high_water(self) -> PayChannelHighWater:
        return PayChannelHighWater(
            cumulative=self.cumulative,
            signature=self.signature,
            timestamp=self.updated_at,
        )


class PayChannelCreateResult(XRPLModel):
    created: bool
    record: PayChannelRecord


class PayChannelAdvanceResult(XRPLModel):
    status: AdvanceStatus
    previous: Drops
    exact_replay: bool = False
    record: PayChannelRecord

    @model_validator(mode="after")
    def _validate_exact_replay(self) -> "PayChannelAdvanceResult":
        if self.exact_replay and self.status != "replay":
            raise ValueError("exact_replay is only valid for replay decisions")
        return self


@dataclass(frozen=True, slots=True)
class PayChannelScanPage:
    cursor: int
    records: tuple[PayChannelRecord, ...]


class PayChannelStoreError(ValueError):
    """Typed fail-closed store error suitable for facilitator error mapping."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}")


@runtime_checkable
class PayChannelStore(Protocol):
    async def create(self, record: PayChannelRecord) -> PayChannelCreateResult:
        ...

    async def get(
        self,
        *,
        network: XRPLNetwork,
        channel_id: str,
    ) -> PayChannelRecord | None:
        ...

    async def advance(
        self,
        *,
        network: XRPLNetwork,
        channel_id: str,
        cumulative: str,
        requested: str,
        signature: str,
        funded: str | None = None,
        timestamp: int | None = None,
    ) -> PayChannelAdvanceResult:
        ...

    async def mark_redeemed(
        self,
        *,
        network: XRPLNetwork,
        channel_id: str,
        cumulative: str,
        reference: str | None = None,
        timestamp: int | None = None,
    ) -> PayChannelRecord:
        ...

    async def finalize(
        self,
        *,
        network: XRPLNetwork,
        channel_id: str,
        reason: str,
        expected_cumulative: str | None = None,
        timestamp: int | None = None,
    ) -> PayChannelRecord:
        ...

    async def scan(
        self,
        *,
        network: XRPLNetwork,
        cursor: int = 0,
        limit: int = 100,
    ) -> PayChannelScanPage:
        ...

    async def claim_redemption(
        self,
        *,
        network: XRPLNetwork,
        channel_id: str,
        lease_seconds: int,
    ) -> str | None:
        ...

    async def release_redemption(
        self,
        *,
        network: XRPLNetwork,
        channel_id: str,
        lease_id: str,
    ) -> None:
        ...


Clock = Callable[[], int]
MutationResult: TypeAlias = tuple[Any, PayChannelRecord | None]
Mutation = Callable[[PayChannelRecord | None], MutationResult]
_NETWORK_ADAPTER = TypeAdapter(XRPLNetwork)
_DROPS_ADAPTER = TypeAdapter(Drops)


def _now_ms() -> int:
    return int(time() * 1_000)


def _channel_key(network: XRPLNetwork, channel_id: str) -> str:
    # Validate both key components before interpolation so no caller-controlled
    # separator can cross network/channel namespaces.
    validated_network = _NETWORK_ADAPTER.validate_python(network)
    validated = XRPLChannelVoucherPayload(
        action="voucher",
        channelId=channel_id,
        amount="0",
        signature="00",
    )
    return f"facilitator:paychannel:{validated_network}:{validated.channel_id.upper()}"


def _redemption_lease_key(network: XRPLNetwork, channel_id: str) -> str:
    channel_key = _channel_key(network, channel_id)
    return channel_key.replace(
        "facilitator:paychannel:",
        "facilitator:paychannel-redemption-lease:",
        1,
    )


def _validate_scan(cursor: int, limit: int) -> tuple[int, int]:
    if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
        raise ValueError("cursor must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    return cursor, limit


def _validate_lease_seconds(lease_seconds: int) -> int:
    if (
        isinstance(lease_seconds, bool)
        or not isinstance(lease_seconds, int)
        or lease_seconds <= 0
    ):
        raise ValueError("lease_seconds must be a positive integer")
    return lease_seconds


def _require_record(
    current: PayChannelRecord | None,
    *,
    network: XRPLNetwork,
    channel_id: str,
) -> PayChannelRecord:
    if current is None:
        raise PayChannelStoreError(
            "CHANNEL_NOT_FOUND",
            f"PayChannel {network}/{channel_id} is not registered",
        )
    if current.network != network or current.channel_id != channel_id.upper():
        raise PayChannelStoreError(
            "CORRUPT_CHANNEL_STATE",
            "Stored PayChannel identity does not match its network/channel key",
        )
    return current


def _advance_mutation(
    *,
    network: XRPLNetwork,
    channel_id: str,
    cumulative: str,
    requested: str,
    signature: str,
    funded: str | None,
    timestamp: int,
) -> Mutation:
    # Validate all voucher fields before entering a potentially replayed CAS
    # callback. The callback itself remains synchronous and side-effect free.
    voucher = XRPLChannelVoucherPayload(
        action="voucher",
        channelId=channel_id,
        amount=cumulative,
        signature=signature,
    )
    if funded is not None and (
        not funded
        or not funded.isascii()
        or not funded.isdigit()
        or int(funded) <= 0
    ):
        raise PayChannelStoreError(
            "INVALID_CHANNEL_STATE",
            "Validated PayChannel funding must be a positive drops string",
        )

    def mutate(current: PayChannelRecord | None) -> MutationResult:
        record = _require_record(current, network=network, channel_id=channel_id)
        if record.finalized:
            raise PayChannelStoreError(
                "CHANNEL_FINALIZED",
                f"PayChannel {network}/{channel_id} is finalized",
            )
        funding_changed = False
        if funded is not None:
            if int(funded) < int(record.funded):
                raise PayChannelStoreError(
                    "FUNDING_REGRESSION",
                    "Validated PayChannel funding is below durable funding",
                )
            if funded != record.funded:
                record = PayChannelRecord.model_validate(
                    record.model_copy(update={"funded": funded}).model_dump()
                )
                funding_changed = True
        if int(voucher.amount) > int(record.funded):
            raise PayChannelStoreError(
                "CHANNEL_EXHAUSTED",
                f"Cumulative {voucher.amount} exceeds funded amount {record.funded}",
            )

        effective_timestamp = max(record.updated_at, timestamp)
        decision = evaluate_high_water(
            record.high_water,
            cumulative=voucher.amount,
            requested=requested,
            signature=voucher.signature,
            timestamp=effective_timestamp,
        )
        if decision.status != "advanced" or decision.state is None:
            exact_replay = (
                decision.status == "replay"
                and record.cumulative == voucher.amount
                and record.signature.upper() == voucher.signature.upper()
            )
            return (
                PayChannelAdvanceResult(
                    status=decision.status,
                    previous=decision.previous,
                    exact_replay=exact_replay,
                    record=record,
                ),
                record if funding_changed else None,
            )

        updated = record.model_copy(
            update={
                "cumulative": decision.state.cumulative,
                "signature": decision.state.signature,
                "funded": record.funded,
                "updated_at": decision.state.timestamp,
            }
        )
        # model_copy does not revalidate updates in Pydantic v2.
        updated = PayChannelRecord.model_validate(updated.model_dump())
        return (
            PayChannelAdvanceResult(
                status="advanced",
                previous=decision.previous,
                record=updated,
            ),
            updated,
        )

    return mutate


def _redeem_mutation(
    *,
    network: XRPLNetwork,
    channel_id: str,
    cumulative: str,
    reference: str | None,
    timestamp: int,
) -> Mutation:
    # Drops validation is shared with the voucher schema. A harmless fixed
    # signature keeps this pre-CAS validation independent from current state.
    amount = XRPLChannelVoucherPayload(
        action="voucher",
        channelId=channel_id,
        amount=cumulative,
        signature="00",
    ).amount

    def mutate(current: PayChannelRecord | None) -> MutationResult:
        record = _require_record(current, network=network, channel_id=channel_id)
        new_redeemed = int(amount)
        old_redeemed = int(record.redeemed)
        if new_redeemed < old_redeemed:
            raise PayChannelStoreError(
                "REDEEMED_REGRESSION",
                f"Redeemed amount {amount} is below stored amount {record.redeemed}",
            )
        if new_redeemed > int(record.cumulative):
            raise PayChannelStoreError(
                "REDEEMED_EXCEEDS_CUMULATIVE",
                f"Redeemed amount {amount} exceeds cumulative {record.cumulative}",
            )
        if new_redeemed == old_redeemed:
            return record, None

        effective_timestamp = max(record.updated_at, timestamp)
        updated = record.model_copy(
            update={
                "redeemed": amount,
                "redeemed_at": effective_timestamp,
                "redemption_reference": reference,
                "updated_at": effective_timestamp,
            }
        )
        updated = PayChannelRecord.model_validate(updated.model_dump())
        return updated, updated

    return mutate


def _finalize_mutation(
    *,
    network: XRPLNetwork,
    channel_id: str,
    reason: str,
    expected_cumulative: str | None,
    timestamp: int,
) -> Mutation:
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise ValueError("finalization reason is required")
    normalized_expected = (
        _DROPS_ADAPTER.validate_python(expected_cumulative)
        if expected_cumulative is not None
        else None
    )

    def mutate(current: PayChannelRecord | None) -> MutationResult:
        record = _require_record(current, network=network, channel_id=channel_id)
        if (
            normalized_expected is not None
            and record.cumulative != normalized_expected
        ):
            raise PayChannelStoreError(
                "CHANNEL_STATE_CHANGED",
                "PayChannel cumulative amount changed before finalization",
            )
        if record.finalized:
            return record, None
        effective_timestamp = max(record.updated_at, timestamp)
        updated = record.model_copy(
            update={
                "finalized": True,
                "finalized_at": effective_timestamp,
                "finalized_reason": normalized_reason,
                "updated_at": effective_timestamp,
            }
        )
        updated = PayChannelRecord.model_validate(updated.model_dump())
        return updated, updated

    return mutate


class InMemoryPayChannelStore:
    """Atomic process-local implementation for tests and single-process development."""

    def __init__(self, *, clock: Clock = _now_ms) -> None:
        self._clock = clock
        self._records: dict[str, PayChannelRecord] = {}
        self._redemption_leases: dict[str, tuple[str, int]] = {}
        self._lock = asyncio.Lock()

    async def create(self, record: PayChannelRecord) -> PayChannelCreateResult:
        key = _channel_key(record.network, record.channel_id)
        async with self._lock:
            existing = self._records.get(key)
            if existing is not None:
                if existing == record:
                    return PayChannelCreateResult(created=False, record=existing)
                raise PayChannelStoreError(
                    "CHANNEL_CONFLICT",
                    f"PayChannel {record.network}/{record.channel_id} already exists",
                )
            self._records[key] = record
            return PayChannelCreateResult(created=True, record=record)

    async def get(
        self,
        *,
        network: XRPLNetwork,
        channel_id: str,
    ) -> PayChannelRecord | None:
        key = _channel_key(network, channel_id)
        async with self._lock:
            return self._records.get(key)

    async def _mutate(self, key: str, mutation: Mutation) -> Any:
        async with self._lock:
            result, updated = mutation(self._records.get(key))
            if updated is not None:
                self._records[key] = updated
            return result

    async def advance(
        self,
        *,
        network: XRPLNetwork,
        channel_id: str,
        cumulative: str,
        requested: str,
        signature: str,
        funded: str | None = None,
        timestamp: int | None = None,
    ) -> PayChannelAdvanceResult:
        key = _channel_key(network, channel_id)
        mutation = _advance_mutation(
            network=network,
            channel_id=channel_id,
            cumulative=cumulative,
            requested=requested,
            signature=signature,
            funded=funded,
            timestamp=timestamp if timestamp is not None else self._clock(),
        )
        return await self._mutate(key, mutation)

    async def mark_redeemed(
        self,
        *,
        network: XRPLNetwork,
        channel_id: str,
        cumulative: str,
        reference: str | None = None,
        timestamp: int | None = None,
    ) -> PayChannelRecord:
        key = _channel_key(network, channel_id)
        mutation = _redeem_mutation(
            network=network,
            channel_id=channel_id,
            cumulative=cumulative,
            reference=reference,
            timestamp=timestamp if timestamp is not None else self._clock(),
        )
        return await self._mutate(key, mutation)

    async def finalize(
        self,
        *,
        network: XRPLNetwork,
        channel_id: str,
        reason: str,
        expected_cumulative: str | None = None,
        timestamp: int | None = None,
    ) -> PayChannelRecord:
        key = _channel_key(network, channel_id)
        mutation = _finalize_mutation(
            network=network,
            channel_id=channel_id,
            reason=reason,
            expected_cumulative=expected_cumulative,
            timestamp=timestamp if timestamp is not None else self._clock(),
        )
        return await self._mutate(key, mutation)

    async def scan(
        self,
        *,
        network: XRPLNetwork,
        cursor: int = 0,
        limit: int = 100,
    ) -> PayChannelScanPage:
        cursor, limit = _validate_scan(cursor, limit)
        prefix = f"facilitator:paychannel:{_NETWORK_ADAPTER.validate_python(network)}:"
        async with self._lock:
            records = [
                self._records[key]
                for key in sorted(self._records)
                if key.startswith(prefix)
            ]
            if cursor >= len(records):
                return PayChannelScanPage(cursor=0, records=())
            end = min(len(records), cursor + limit)
            next_cursor = 0 if end >= len(records) else end
            return PayChannelScanPage(
                cursor=next_cursor,
                records=tuple(records[cursor:end]),
            )

    async def claim_redemption(
        self,
        *,
        network: XRPLNetwork,
        channel_id: str,
        lease_seconds: int,
    ) -> str | None:
        lease_seconds = _validate_lease_seconds(lease_seconds)
        key = _redemption_lease_key(network, channel_id)
        now = self._clock()
        async with self._lock:
            current = self._redemption_leases.get(key)
            if current is not None and current[1] > now:
                return None
            lease_id = uuid4().hex
            self._redemption_leases[key] = (
                lease_id,
                now + lease_seconds * 1_000,
            )
            return lease_id

    async def release_redemption(
        self,
        *,
        network: XRPLNetwork,
        channel_id: str,
        lease_id: str,
    ) -> None:
        key = _redemption_lease_key(network, channel_id)
        async with self._lock:
            current = self._redemption_leases.get(key)
            if current is not None and current[0] == lease_id:
                self._redemption_leases.pop(key, None)


class RedisPayChannelStore:
    """Shared durable implementation using Redis WATCH/MULTI compare-and-set."""

    def __init__(self, redis_client: Any, *, clock: Clock = _now_ms) -> None:
        self._redis = redis_client
        self._clock = clock

    @staticmethod
    def key(network: XRPLNetwork, channel_id: str) -> str:
        return _channel_key(network, channel_id)

    @staticmethod
    def _serialize(record: PayChannelRecord) -> str:
        return record.model_dump_json()

    @staticmethod
    def _deserialize(raw: str | bytes | None) -> PayChannelRecord | None:
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            return PayChannelRecord.model_validate_json(raw)
        except (ValidationError, ValueError, UnicodeDecodeError) as exc:
            raise PayChannelStoreError(
                "CORRUPT_CHANNEL_STATE",
                "Stored PayChannel state failed validation",
            ) from exc

    @staticmethod
    def _is_watch_error(exc: Exception) -> bool:
        try:
            from redis.exceptions import WatchError
        except ModuleNotFoundError:
            return exc.__class__.__name__ == "WatchError"
        return isinstance(exc, WatchError)

    async def _mutate(self, key: str, mutation: Mutation) -> Any:
        while True:
            try:
                async with self._redis.pipeline() as pipe:
                    await pipe.watch(key)
                    current = self._deserialize(await pipe.get(key))
                    result, updated = mutation(current)
                    if updated is None:
                        await pipe.unwatch()
                        return result
                    pipe.multi()
                    pipe.set(key, self._serialize(updated))
                    await pipe.execute()
                    return result
            except PayChannelStoreError:
                raise
            except Exception as exc:
                if self._is_watch_error(exc):
                    continue
                raise

    async def create(self, record: PayChannelRecord) -> PayChannelCreateResult:
        key = _channel_key(record.network, record.channel_id)

        def mutation(current: PayChannelRecord | None) -> MutationResult:
            if current is None:
                return PayChannelCreateResult(created=True, record=record), record
            if current == record:
                return PayChannelCreateResult(created=False, record=current), None
            raise PayChannelStoreError(
                "CHANNEL_CONFLICT",
                f"PayChannel {record.network}/{record.channel_id} already exists",
            )

        return await self._mutate(key, mutation)

    async def get(
        self,
        *,
        network: XRPLNetwork,
        channel_id: str,
    ) -> PayChannelRecord | None:
        key = _channel_key(network, channel_id)
        record = self._deserialize(await self._redis.get(key))
        if record is None:
            return None
        return _require_record(record, network=network, channel_id=channel_id)

    async def advance(
        self,
        *,
        network: XRPLNetwork,
        channel_id: str,
        cumulative: str,
        requested: str,
        signature: str,
        funded: str | None = None,
        timestamp: int | None = None,
    ) -> PayChannelAdvanceResult:
        key = _channel_key(network, channel_id)
        mutation = _advance_mutation(
            network=network,
            channel_id=channel_id,
            cumulative=cumulative,
            requested=requested,
            signature=signature,
            funded=funded,
            timestamp=timestamp if timestamp is not None else self._clock(),
        )
        return await self._mutate(key, mutation)

    async def mark_redeemed(
        self,
        *,
        network: XRPLNetwork,
        channel_id: str,
        cumulative: str,
        reference: str | None = None,
        timestamp: int | None = None,
    ) -> PayChannelRecord:
        key = _channel_key(network, channel_id)
        mutation = _redeem_mutation(
            network=network,
            channel_id=channel_id,
            cumulative=cumulative,
            reference=reference,
            timestamp=timestamp if timestamp is not None else self._clock(),
        )
        return await self._mutate(key, mutation)

    async def finalize(
        self,
        *,
        network: XRPLNetwork,
        channel_id: str,
        reason: str,
        expected_cumulative: str | None = None,
        timestamp: int | None = None,
    ) -> PayChannelRecord:
        key = _channel_key(network, channel_id)
        mutation = _finalize_mutation(
            network=network,
            channel_id=channel_id,
            reason=reason,
            expected_cumulative=expected_cumulative,
            timestamp=timestamp if timestamp is not None else self._clock(),
        )
        return await self._mutate(key, mutation)

    async def scan(
        self,
        *,
        network: XRPLNetwork,
        cursor: int = 0,
        limit: int = 100,
    ) -> PayChannelScanPage:
        cursor, limit = _validate_scan(cursor, limit)
        validated_network = _NETWORK_ADAPTER.validate_python(network)
        next_cursor, keys = await self._redis.scan(
            cursor=cursor,
            match=f"facilitator:paychannel:{validated_network}:*",
            count=limit,
        )
        if not keys:
            return PayChannelScanPage(cursor=int(next_cursor), records=())
        raw_records = await self._redis.mget(*keys)
        records: list[PayChannelRecord] = []
        prefix = f"facilitator:paychannel:{validated_network}:"
        for key, raw in zip(keys, raw_records, strict=True):
            record = self._deserialize(raw)
            if record is None:
                continue
            normalized_key = key.decode("utf-8") if isinstance(key, bytes) else key
            if not isinstance(normalized_key, str) or not normalized_key.startswith(prefix):
                raise PayChannelStoreError(
                    "CORRUPT_CHANNEL_STATE",
                    "Stored PayChannel scan key failed validation",
                )
            channel_id = normalized_key.removeprefix(prefix)
            records.append(
                _require_record(
                    record,
                    network=validated_network,
                    channel_id=channel_id,
                )
            )
        return PayChannelScanPage(
            cursor=int(next_cursor),
            records=tuple(records),
        )

    async def claim_redemption(
        self,
        *,
        network: XRPLNetwork,
        channel_id: str,
        lease_seconds: int,
    ) -> str | None:
        lease_seconds = _validate_lease_seconds(lease_seconds)
        lease_id = uuid4().hex
        claimed = await self._redis.set(
            _redemption_lease_key(network, channel_id),
            lease_id,
            nx=True,
            ex=lease_seconds,
        )
        return lease_id if claimed else None

    async def release_redemption(
        self,
        *,
        network: XRPLNetwork,
        channel_id: str,
        lease_id: str,
    ) -> None:
        # Compare-and-delete prevents a delayed worker from releasing a newer
        # replica's lease after its own lease expired.
        await self._redis.eval(
            """
            if redis.call('get', KEYS[1]) == ARGV[1] then
                return redis.call('del', KEYS[1])
            end
            return 0
            """,
            1,
            _redemption_lease_key(network, channel_id),
            lease_id,
        )
