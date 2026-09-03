from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any

import pytest
from pydantic import ValidationError
from redis.exceptions import WatchError

from xrpl_mpp_facilitator.paychannel_store import (
    InMemoryPayChannelStore,
    PayChannelRecord,
    PayChannelStoreError,
    RedisPayChannelStore,
)


PAYER = "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe"
RECIPIENT = "rf5kMNrUqgLzJT8YUzxM1pptc5r3Lfx1J9"
CHANNEL_ID = "AB" * 32
SIGNATURE_A = "CD" * 64
SIGNATURE_B = "EF" * 64


def run_async(test: Callable[[], Coroutine[Any, Any, None]]) -> Callable[[], None]:
    @wraps(test)
    def wrapped() -> None:
        asyncio.run(test())

    return wrapped


def record(*, network: str = "testnet", channel_id: str = CHANNEL_ID) -> PayChannelRecord:
    return PayChannelRecord(
        network=network,
        channel_id=channel_id,
        payer=PAYER,
        recipient=RECIPIENT,
        funded="1000",
        created_at=1,
        updated_at=1,
    )


@run_async
async def test_in_memory_store_classifies_atomic_high_water_transitions() -> None:
    store = InMemoryPayChannelStore(clock=lambda: 10)
    created = await store.create(record())
    assert created.created
    assert not (await store.create(record())).created

    advanced = await store.advance(
        network="testnet",
        channel_id=CHANNEL_ID,
        cumulative="100",
        requested="100",
        signature=SIGNATURE_A,
    )
    assert advanced.status == "advanced"
    assert advanced.previous == "0"
    assert advanced.record.cumulative == "100"

    replay = await store.advance(
        network="testnet",
        channel_id=CHANNEL_ID,
        cumulative="100",
        requested="100",
        signature=SIGNATURE_A.lower(),
    )
    assert replay.status == "replay"
    assert replay.exact_replay
    assert replay.record.updated_at == 10

    different_proof = await store.advance(
        network="testnet",
        channel_id=CHANNEL_ID,
        cumulative="100",
        requested="100",
        signature=SIGNATURE_B,
    )
    assert different_proof.status == "replay"
    assert not different_proof.exact_replay

    regressed = await store.advance(
        network="testnet",
        channel_id=CHANNEL_ID,
        cumulative="99",
        requested="1",
        signature=SIGNATURE_B,
    )
    assert regressed.status == "regressed"

    short = await store.advance(
        network="testnet",
        channel_id=CHANNEL_ID,
        cumulative="149",
        requested="50",
        signature=SIGNATURE_B,
    )
    assert short.status == "short"
    assert (await store.get(network="testnet", channel_id=CHANNEL_ID)) == advanced.record


@run_async
async def test_concurrent_equal_vouchers_credit_exactly_once() -> None:
    store = InMemoryPayChannelStore(clock=lambda: 20)
    await store.create(record())

    results = await asyncio.gather(
        *(
            store.advance(
                network="testnet",
                channel_id=CHANNEL_ID,
                cumulative="100",
                requested="100",
                signature=SIGNATURE_A,
            )
            for _ in range(20)
        )
    )
    assert [result.status for result in results].count("advanced") == 1
    assert [result.status for result in results].count("replay") == 19


@run_async
async def test_funded_finalized_and_missing_channel_guards_fail_closed() -> None:
    store = InMemoryPayChannelStore(clock=lambda: 30)
    await store.create(record())

    with pytest.raises(PayChannelStoreError) as exhausted:
        await store.advance(
            network="testnet",
            channel_id=CHANNEL_ID,
            cumulative="1001",
            requested="1001",
            signature=SIGNATURE_A,
        )
    assert exhausted.value.code == "CHANNEL_EXHAUSTED"

    with pytest.raises(PayChannelStoreError) as changed:
        await store.finalize(
            network="testnet",
            channel_id=CHANNEL_ID,
            reason="stale-close",
            expected_cumulative="1",
        )
    assert changed.value.code == "CHANNEL_STATE_CHANGED"

    finalized = await store.finalize(
        network="testnet",
        channel_id=CHANNEL_ID,
        reason="idle-close",
        expected_cumulative="0",
    )
    assert finalized.finalized
    assert finalized.finalized_at == 30
    assert await store.finalize(
        network="testnet",
        channel_id=CHANNEL_ID,
        reason="second-close",
    ) == finalized

    with pytest.raises(PayChannelStoreError) as closed:
        await store.advance(
            network="testnet",
            channel_id=CHANNEL_ID,
            cumulative="1",
            requested="1",
            signature=SIGNATURE_A,
        )
    assert closed.value.code == "CHANNEL_FINALIZED"

    with pytest.raises(PayChannelStoreError) as missing:
        await store.advance(
            network="mainnet",
            channel_id=CHANNEL_ID,
            cumulative="1",
            requested="1",
            signature=SIGNATURE_A,
        )
    assert missing.value.code == "CHANNEL_NOT_FOUND"


@run_async
async def test_redeemed_amount_is_monotonic_bounded_and_idempotent() -> None:
    store = InMemoryPayChannelStore(clock=lambda: 40)
    await store.create(record())
    await store.advance(
        network="testnet",
        channel_id=CHANNEL_ID,
        cumulative="400",
        requested="400",
        signature=SIGNATURE_A,
    )

    redeemed = await store.mark_redeemed(
        network="testnet",
        channel_id=CHANNEL_ID,
        cumulative="300",
        reference="tx-1",
    )
    assert redeemed.redeemed == "300"
    assert redeemed.redemption_reference == "tx-1"
    assert await store.mark_redeemed(
        network="testnet",
        channel_id=CHANNEL_ID,
        cumulative="300",
        reference="ignored-on-idempotent-retry",
    ) == redeemed

    with pytest.raises(PayChannelStoreError) as regressed:
        await store.mark_redeemed(
            network="testnet",
            channel_id=CHANNEL_ID,
            cumulative="299",
        )
    assert regressed.value.code == "REDEEMED_REGRESSION"

    with pytest.raises(PayChannelStoreError) as excessive:
        await store.mark_redeemed(
            network="testnet",
            channel_id=CHANNEL_ID,
            cumulative="401",
        )
    assert excessive.value.code == "REDEEMED_EXCEEDS_CUMULATIVE"


def test_record_rejects_impossible_accounting_state() -> None:
    with pytest.raises(ValidationError, match="cannot exceed funded"):
        PayChannelRecord(
            network="testnet",
            channel_id=CHANNEL_ID,
            payer=PAYER,
            recipient=RECIPIENT,
            funded="10",
            cumulative="11",
            signature=SIGNATURE_A,
            created_at=1,
            updated_at=1,
        )


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.fail_next_execute = False

    def pipeline(self) -> "FakePipeline":
        return FakePipeline(self)

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def mget(self, *keys: str) -> list[str | None]:
        return [self.data.get(key) for key in keys]

    async def scan(
        self,
        *,
        cursor: int,
        match: str,
        count: int,
    ) -> tuple[int, list[str]]:
        prefix = match.removesuffix("*")
        keys = sorted(key for key in self.data if key.startswith(prefix))
        end = min(len(keys), cursor + count)
        return (0 if end >= len(keys) else end, keys[cursor:end])

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool:
        del ex
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def eval(
        self,
        _script: str,
        _key_count: int,
        key: str,
        expected: str,
    ) -> int:
        if self.data.get(key) != expected:
            return 0
        self.data.pop(key, None)
        return 1


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.commands: list[tuple[str, str]] = []

    async def __aenter__(self) -> "FakePipeline":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def watch(self, *_keys: str) -> None:
        return None

    async def get(self, key: str) -> str | None:
        return self.redis.data.get(key)

    async def unwatch(self) -> None:
        return None

    def multi(self) -> None:
        return None

    def set(self, key: str, value: str) -> None:
        self.commands.append((key, value))

    async def execute(self) -> list[bool]:
        if self.redis.fail_next_execute:
            self.redis.fail_next_execute = False
            raise WatchError("simulated conflict")
        for key, value in self.commands:
            self.redis.data[key] = value
        return [True] * len(self.commands)


@run_async
async def test_redis_store_uses_network_key_and_retries_watch_conflict() -> None:
    redis = FakeRedis()
    store = RedisPayChannelStore(redis, clock=lambda: 50)
    redis.fail_next_execute = True

    assert (await store.create(record())).created
    expected_key = f"facilitator:paychannel:testnet:{CHANNEL_ID}"
    assert expected_key in redis.data

    advanced = await store.advance(
        network="testnet",
        channel_id=CHANNEL_ID.lower(),
        cumulative="100",
        requested="100",
        signature=SIGNATURE_A,
    )
    assert advanced.status == "advanced"
    persisted = await store.get(network="testnet", channel_id=CHANNEL_ID)
    assert persisted is not None
    assert persisted.cumulative == "100"

    assert await store.get(network="mainnet", channel_id=CHANNEL_ID) is None


@run_async
async def test_redis_store_fails_closed_on_corrupt_state() -> None:
    redis = FakeRedis()
    store = RedisPayChannelStore(redis)
    redis.data[store.key("testnet", CHANNEL_ID)] = "not-json"

    with pytest.raises(PayChannelStoreError) as caught:
        await store.get(network="testnet", channel_id=CHANNEL_ID)
    assert caught.value.code == "CORRUPT_CHANNEL_STATE"

    redis.data[store.key("testnet", CHANNEL_ID)] = record(network="mainnet").model_dump_json()
    with pytest.raises(PayChannelStoreError) as mismatched:
        await store.get(network="testnet", channel_id=CHANNEL_ID)
    assert mismatched.value.code == "CORRUPT_CHANNEL_STATE"


@run_async
async def test_redis_store_scans_network_and_coordinates_redemption_lease() -> None:
    redis = FakeRedis()
    store = RedisPayChannelStore(redis)
    await store.create(record(network="testnet", channel_id=CHANNEL_ID))
    await store.create(record(network="mainnet", channel_id="CD" * 32))

    page = await store.scan(network="testnet", limit=10)
    assert page.cursor == 0
    assert [item.channel_id for item in page.records] == [CHANNEL_ID]

    first = await store.claim_redemption(
        network="testnet",
        channel_id=CHANNEL_ID,
        lease_seconds=60,
    )
    assert first is not None
    assert await store.claim_redemption(
        network="testnet",
        channel_id=CHANNEL_ID,
        lease_seconds=60,
    ) is None
    await store.release_redemption(
        network="testnet",
        channel_id=CHANNEL_ID,
        lease_id=first,
    )
    assert await store.claim_redemption(
        network="testnet",
        channel_id=CHANNEL_ID,
        lease_seconds=60,
    ) is not None
